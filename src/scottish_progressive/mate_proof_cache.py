from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Mapping

from .model import (
    ENGINE_SOURCE_FINGERPRINT,
    QUIET_DRAW_POLICY,
    RULESET_VERSION,
    Outcome,
    ProgressiveState,
    SeriesResult,
)
from .rules import play_series


CACHE_SCHEMA_VERSION = 1
DEFAULT_MATE_PROOF_CACHE_CAPACITY = 4_096
MAX_CACHE_FILE_BYTES = 64 * 1024 * 1024
MAX_STATE_IDENTITY_LENGTH = 4_096
MAX_PROOF_MOVES = 512
MAX_PROOF_WORK = (1 << 64) - 1


class MateProofStatus(StrEnum):
    FOUND = "found"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class MateProofIdentity:
    engine_source: str
    ruleset: str
    quiet_draw_policy: str
    native_mate: str

    def __post_init__(self) -> None:
        for field_name, value in asdict(self).items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} identity must be a nonempty string")

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def current_mate_proof_identity() -> MateProofIdentity:
    """Return the exact source/rules/native namespace for reusable proofs.

    The native adapter import is deliberately local. Ordinary search, league,
    training and full-game imports therefore retain their existing non-import
    contract unless a persistent proof cache is explicitly constructed.
    """

    from .series_mate import native_mate_runtime_identity

    return MateProofIdentity(
        engine_source=ENGINE_SOURCE_FINGERPRINT,
        ruleset=RULESET_VERSION,
        quiet_draw_policy=QUIET_DRAW_POLICY,
        native_mate=native_mate_runtime_identity(),
    )


@dataclass(frozen=True, slots=True)
class MateProofHit:
    status: MateProofStatus
    series: SeriesResult | None
    proof_work: int


@dataclass(frozen=True, slots=True)
class MateProofCacheStats:
    capacity: int
    entries: int
    hits: int
    found_hits: int
    exhausted_hits: int
    misses: int
    stores: int
    evictions: int
    work_saved: int
    replay_rejects: int
    load_failures: int
    identity_rejects: int
    write_failures: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Entry:
    key: str
    state_identity: str
    status: MateProofStatus
    moves: tuple[str, ...]
    proof_work: int
    sequence: int


class MateProofCache:
    """A bounded, atomic, identity-bound cache of exact one-series proofs.

    Positive records are replayed through the Python rules oracle on every
    lookup. Negative records can only be inserted through ``store_exhausted``;
    callers must use it exclusively for an authoritative native EXHAUSTED
    result. Resource and compatibility statuses have no representation here.

    Eviction is deterministic FIFO by insertion sequence, then exact key. A
    hit never rewrites the file or changes eviction order, keeping ordinary
    opening lookups cheap and restart behavior reproducible.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        capacity: int = DEFAULT_MATE_PROOF_CACHE_CAPACITY,
        identity: MateProofIdentity | None = None,
    ) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("mate proof cache capacity must be a positive integer")
        self.path = Path(path).expanduser().resolve() if path is not None else None
        self.capacity = capacity
        self.identity = identity or current_mate_proof_identity()
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._next_sequence = 1
        self._hits = 0
        self._found_hits = 0
        self._exhausted_hits = 0
        self._misses = 0
        self._stores = 0
        self._evictions = 0
        self._work_saved = 0
        self._replay_rejects = 0
        self._load_failures = 0
        self._identity_rejects = 0
        self._write_failures = 0
        if self.path is not None and self.path.exists():
            self._load()

    def snapshot(self) -> MateProofCacheStats:
        with self._lock:
            return MateProofCacheStats(
                capacity=self.capacity,
                entries=len(self._entries),
                hits=self._hits,
                found_hits=self._found_hits,
                exhausted_hits=self._exhausted_hits,
                misses=self._misses,
                stores=self._stores,
                evictions=self._evictions,
                work_saved=self._work_saved,
                replay_rejects=self._replay_rejects,
                load_failures=self._load_failures,
                identity_rejects=self._identity_rejects,
                write_failures=self._write_failures,
            )

    def lookup(self, state: ProgressiveState) -> MateProofHit | None:
        state_identity = self._state_identity(state)
        key = self._digest(state_identity)
        with self._lock:
            entry = self._entries.get((key, state_identity))
            if entry is None:
                self._misses += 1
                return None
            if entry.status is MateProofStatus.FOUND:
                try:
                    replayed = play_series(state, entry.moves)
                except Exception:
                    replayed = None
                if (
                    replayed is None
                    or replayed.outcome is not Outcome.CHECKMATE
                    or not replayed.ended_by_check
                ):
                    self._entries.pop((key, state_identity), None)
                    self._replay_rejects += 1
                    self._misses += 1
                    self._persist_locked()
                    return None
                self._hits += 1
                self._found_hits += 1
                self._work_saved += entry.proof_work
                return MateProofHit(entry.status, replayed, entry.proof_work)
            if entry.status is not MateProofStatus.EXHAUSTED:
                # The loader and insertion paths reject this, but fail closed
                # if an in-process adversarial mutation bypasses both.
                self._entries.pop((key, state_identity), None)
                self._replay_rejects += 1
                self._misses += 1
                self._persist_locked()
                return None
            self._hits += 1
            self._exhausted_hits += 1
            self._work_saved += entry.proof_work
            return MateProofHit(entry.status, None, entry.proof_work)

    def store_found(
        self,
        state: ProgressiveState,
        series: SeriesResult,
        *,
        proof_work: int,
    ) -> int:
        """Store one positive witness after authoritative replay.

        Returns the number of entries evicted by this insertion (zero or one).
        An invalid witness is rejected and never reaches memory or disk.
        """

        work = self._validate_work(proof_work)
        try:
            replayed = play_series(state, series.moves)
        except Exception:
            replayed = None
        if (
            replayed is None
            or replayed.outcome is not Outcome.CHECKMATE
            or not replayed.ended_by_check
        ):
            with self._lock:
                self._replay_rejects += 1
            return 0
        return self._store(
            state,
            MateProofStatus.FOUND,
            replayed.moves,
            work,
        )

    def store_exhausted(
        self,
        state: ProgressiveState,
        *,
        proof_work: int,
    ) -> int:
        """Store an authoritative native EXHAUSTED result.

        Selective misses and Unknown/WorkLimit/Deadline/Unsupported outcomes
        must never call this method and cannot be serialized as cache states.
        """

        return self._store(
            state,
            MateProofStatus.EXHAUSTED,
            (),
            self._validate_work(proof_work),
        )

    @staticmethod
    def _validate_work(value: int) -> int:
        if type(value) is not int or not 0 <= value <= MAX_PROOF_WORK:
            raise ValueError("proof_work must be an unsigned 64-bit integer")
        return value

    def _store(
        self,
        state: ProgressiveState,
        status: MateProofStatus,
        moves: tuple[str, ...],
        proof_work: int,
    ) -> int:
        state_identity = self._state_identity(state)
        if len(state_identity) > MAX_STATE_IDENTITY_LENGTH:
            raise ValueError("progressive state identity is too long for the proof cache")
        key = self._digest(state_identity)
        cache_key = (key, state_identity)
        with self._lock:
            old = self._entries.get(cache_key)
            # A replayed mate dominates a contradictory negative. A negative
            # can never overwrite a concrete witness under the same identity.
            if old is not None and old.status is MateProofStatus.FOUND:
                if status is MateProofStatus.EXHAUSTED:
                    return 0
                if moves < old.moves:
                    chosen_moves = moves
                    chosen_work = proof_work
                elif moves > old.moves:
                    chosen_moves = old.moves
                    chosen_work = old.proof_work
                else:
                    chosen_moves = moves
                    chosen_work = min(old.proof_work, proof_work)
                if chosen_moves == old.moves and chosen_work == old.proof_work:
                    return 0
                self._entries[cache_key] = _Entry(
                    key,
                    state_identity,
                    MateProofStatus.FOUND,
                    chosen_moves,
                    chosen_work,
                    old.sequence,
                )
                self._stores += 1
                self._persist_locked()
                return 0
            if old is not None and old.status is status:
                # Saved-work reporting stays conservative when two equivalent
                # authoritative proofs are observed at different costs.
                if proof_work >= old.proof_work:
                    return 0
                self._entries[cache_key] = _Entry(
                    key,
                    state_identity,
                    status,
                    moves,
                    proof_work,
                    old.sequence,
                )
                self._stores += 1
                self._persist_locked()
                return 0

            evicted = 0
            if old is None and len(self._entries) >= self.capacity:
                victim_key = min(
                    self._entries,
                    key=lambda item: (
                        self._entries[item].sequence,
                        item[0],
                        item[1],
                    ),
                )
                del self._entries[victim_key]
                self._evictions += 1
                evicted = 1
            sequence = self._next_sequence
            self._next_sequence += 1
            self._entries[cache_key] = _Entry(
                key,
                state_identity,
                status,
                moves,
                proof_work,
                sequence,
            )
            self._stores += 1
            self._persist_locked()
            return evicted

    @staticmethod
    def _state_identity(state: ProgressiveState) -> str:
        # ``ProgressiveState.pfen`` intentionally follows ordinary display FEN
        # and therefore omits python-chess's promoted-piece provenance. The
        # native mate ABI consumes that bitboard, so it must be present in an
        # exact reusable proof key. Chess960 mode is included for the same
        # fail-closed reason even though the current native solver rejects it.
        return (
            f"{state.pfen} promoted={state.board.promoted:016x} "
            f"chess960={int(state.board.chess960)}"
        )

    def _digest(self, state_identity: str) -> str:
        identity = json.dumps(
            self.identity.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(
            f"{identity}\0{state_identity}".encode("utf-8")
        ).hexdigest()

    def _payload_locked(self) -> dict[str, Any]:
        return {
            "schema": CACHE_SCHEMA_VERSION,
            "identity": self.identity.as_dict(),
            "next_sequence": self._next_sequence,
            "entries": [
                {
                    "key": entry.key,
                    "state_identity": entry.state_identity,
                    "status": entry.status.value,
                    "moves": list(entry.moves),
                    "proof_work": entry.proof_work,
                    "sequence": entry.sequence,
                }
                for entry in sorted(
                    self._entries.values(),
                    key=lambda item: (
                        item.sequence,
                        item.key,
                        item.state_identity,
                    ),
                )
            ],
        }

    @staticmethod
    def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")

    def _persist_locked(self) -> None:
        if self.path is None:
            return
        payload = self._payload_locked()
        document = dict(payload)
        document["checksum"] = hashlib.sha256(
            self._canonical_bytes(payload)
        ).hexdigest()
        data = self._canonical_bytes(document)
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        except OSError:
            self._write_failures += 1
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _load(self) -> None:
        assert self.path is not None
        try:
            if self.path.stat().st_size > MAX_CACHE_FILE_BYTES:
                raise ValueError("mate proof cache file exceeds the size limit")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries, next_sequence = self._validated_document(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            with self._lock:
                self._load_failures += 1
            return
        if entries is None:
            with self._lock:
                self._identity_rejects += 1
            return
        with self._lock:
            self._entries = entries
            self._next_sequence = next_sequence

    def _validated_document(
        self,
        raw: object,
    ) -> tuple[dict[tuple[str, str], _Entry] | None, int]:
        if not isinstance(raw, dict):
            raise ValueError("mate proof cache document must be an object")
        if set(raw) != {
            "schema",
            "identity",
            "next_sequence",
            "entries",
            "checksum",
        }:
            raise ValueError("mate proof cache document shape is invalid")
        checksum = raw["checksum"]
        if not isinstance(checksum, str) or len(checksum) != 64:
            raise ValueError("mate proof cache checksum is invalid")
        payload = {key: value for key, value in raw.items() if key != "checksum"}
        expected_checksum = hashlib.sha256(self._canonical_bytes(payload)).hexdigest()
        if checksum != expected_checksum:
            raise ValueError("mate proof cache checksum does not match")
        if raw["schema"] != CACHE_SCHEMA_VERSION:
            raise ValueError("mate proof cache schema is unsupported")
        if raw["identity"] != self.identity.as_dict():
            return None, 1
        next_sequence = raw["next_sequence"]
        if type(next_sequence) is not int or next_sequence < 1:
            raise ValueError("mate proof cache sequence is invalid")
        raw_entries = raw["entries"]
        if not isinstance(raw_entries, list) or len(raw_entries) > self.capacity:
            raise ValueError("mate proof cache entry count is invalid")
        entries: dict[tuple[str, str], _Entry] = {}
        sequences: set[int] = set()
        for raw_entry in raw_entries:
            entry = self._validated_entry(raw_entry)
            cache_key = (entry.key, entry.state_identity)
            if cache_key in entries or entry.sequence in sequences:
                raise ValueError("mate proof cache contains duplicate identities")
            entries[cache_key] = entry
            sequences.add(entry.sequence)
        if sequences and next_sequence <= max(sequences):
            raise ValueError("mate proof cache next sequence is stale")
        return entries, next_sequence

    def _validated_entry(self, raw: object) -> _Entry:
        if not isinstance(raw, dict) or set(raw) != {
            "key",
            "state_identity",
            "status",
            "moves",
            "proof_work",
            "sequence",
        }:
            raise ValueError("mate proof cache entry shape is invalid")
        key = raw["key"]
        state_identity = raw["state_identity"]
        if not isinstance(key, str) or len(key) != 64:
            raise ValueError("mate proof cache key is invalid")
        if (
            not isinstance(state_identity, str)
            or not state_identity
            or len(state_identity) > MAX_STATE_IDENTITY_LENGTH
        ):
            raise ValueError("mate proof cache state identity is invalid")
        if key != self._digest(state_identity):
            raise ValueError("mate proof cache key does not match its state")
        try:
            status = MateProofStatus(raw["status"])
        except (TypeError, ValueError) as error:
            raise ValueError("mate proof cache status is invalid") from error
        raw_moves = raw["moves"]
        if (
            not isinstance(raw_moves, list)
            or len(raw_moves) > MAX_PROOF_MOVES
            or any(not isinstance(move, str) for move in raw_moves)
        ):
            raise ValueError("mate proof cache moves are invalid")
        moves = tuple(raw_moves)
        if status is MateProofStatus.FOUND and not moves:
            raise ValueError("mate proof cache Found entry has no line")
        if status is MateProofStatus.EXHAUSTED and moves:
            raise ValueError("mate proof cache Exhausted entry carries a line")
        proof_work = self._validate_work(raw["proof_work"])
        sequence = raw["sequence"]
        if type(sequence) is not int or sequence < 1:
            raise ValueError("mate proof cache entry sequence is invalid")
        return _Entry(key, state_identity, status, moves, proof_work, sequence)
