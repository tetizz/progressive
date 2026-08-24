from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
from typing import Any, BinaryIO, Callable, Iterator, Mapping, Sequence

import chess

from .model import ProgressiveState, QUIET_DRAW_POLICY, RULESET_VERSION


MANIFEST_FORMAT = "spc-sharded-corpus-manifest-v1"
CLAIM_FORMAT = "spc-corpus-attempt-claim-v1"
CORPUS_SCHEMA_VERSION = 1
SHARD_MAGIC = b"SPCCSH01"
SHARD_VERSION = 1

_SHARD_HEADER = struct.Struct("<8sHHIQQQ32s32s")
_RECORD_HEADER = struct.Struct("<QI32sI")
_MAX_RECORD_PAYLOAD = 64 * 1024 * 1024
_ZERO_DIGEST = b"\0" * 32

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


class CorpusStoreError(ValueError):
    """Base class for deterministic corpus storage failures."""


class CorpusIdentityError(CorpusStoreError):
    """The store or shard belongs to a different corpus identity."""


class AttemptRangeConflict(CorpusStoreError):
    """Two writers or finalized shards claim overlapping attempts."""


class ShardCorruptionError(CorpusStoreError):
    """A finalized shard failed its binary or content-addressed contract."""


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _domain_digest(domain: bytes, payload: bytes) -> bytes:
    return hashlib.sha256(domain + b"\0" + payload).digest()


def _validate_text(name: str, value: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CorpusIdentityError(f"{name} must be a non-empty string up to {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise CorpusIdentityError(f"{name} contains control characters")
    return value


@dataclass(frozen=True, slots=True)
class CorpusIdentity:
    """Immutable identity shared by a manifest and all of its shards."""

    record_schema: str
    source_fingerprint: str
    generator_config_sha256: str
    profile_ids: tuple[str, ...]
    ruleset_version: str = RULESET_VERSION

    def __post_init__(self) -> None:
        _validate_text("record_schema", self.record_schema)
        _validate_text("source_fingerprint", self.source_fingerprint)
        _validate_text("ruleset_version", self.ruleset_version)
        if (
            not isinstance(self.generator_config_sha256, str)
            or len(self.generator_config_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.generator_config_sha256
            )
        ):
            raise CorpusIdentityError(
                "generator_config_sha256 must be a lowercase SHA-256 digest"
            )
        profiles = tuple(self.profile_ids)
        if not profiles:
            raise CorpusIdentityError("profile_ids must contain at least one profile")
        if len(set(profiles)) != len(profiles):
            raise CorpusIdentityError("profile_ids must be unique")
        for profile_id in profiles:
            _validate_text("profile_id", profile_id)
        object.__setattr__(self, "profile_ids", profiles)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generator_config_sha256": self.generator_config_sha256,
            "profile_ids": list(self.profile_ids),
            "record_schema": self.record_schema,
            "ruleset_version": self.ruleset_version,
            "source_fingerprint": self.source_fingerprint,
        }

    @property
    def digest(self) -> bytes:
        return _domain_digest(b"spc-corpus-identity-v1", _canonical_json(self.as_dict()))

    @property
    def digest_hex(self) -> str:
        return self.digest.hex()


@dataclass(frozen=True, slots=True, order=True)
class AttemptRange:
    """Half-open deterministic attempt range owned by exactly one shard."""

    start: int
    stop: int

    def __post_init__(self) -> None:
        if (
            type(self.start) is not int
            or type(self.stop) is not int
            or not 0 <= self.start < self.stop <= (1 << 64)
        ):
            raise ValueError("attempt range must satisfy 0 <= start < stop <= 2**64")

    def contains(self, attempt_index: int) -> bool:
        return self.start <= attempt_index < self.stop

    def overlaps(self, other: AttemptRange) -> bool:
        return self.start < other.stop and other.start < self.stop


def _owner_digest(owner_id: str) -> bytes:
    owner = _validate_text("owner_id", owner_id, maximum=512)
    return _domain_digest(b"spc-corpus-owner-v1", owner.encode("utf-8"))


def progressive_state_dedup_key(
    state: ProgressiveState,
    *,
    ruleset_version: str = RULESET_VERSION,
) -> bytes:
    """Returns the full semantic progressive-state SHA-256 key.

    This intentionally includes fields omitted by ordinary/display FEN: the
    series and quiet-series counters, every progressive en-passant target,
    promoted-piece provenance, Chess960 mode, and the rules/quiet-draw policy.
    Orthodox halfmove/fullmove display clocks are excluded because Scottish
    draw semantics are represented by ``quiet_series``.
    """

    if not isinstance(state, ProgressiveState):
        raise TypeError("state must be a ProgressiveState")
    rules = _validate_text("ruleset_version", ruleset_version).encode("utf-8")
    quiet_policy = QUIET_DRAW_POLICY.encode("utf-8")
    board = state.board
    if not 0 <= state.series_number < (1 << 64):
        raise ValueError("series_number exceeds the dedup-key encoding")
    if not 0 <= state.quiet_series < (1 << 64):
        raise ValueError("quiet_series exceeds the dedup-key encoding")

    encoded = bytearray(b"SPCPST01")
    for color in (chess.WHITE, chess.BLACK):
        for piece_type in range(chess.PAWN, chess.KING + 1):
            encoded.extend(struct.pack("<Q", board.pieces_mask(piece_type, color)))
    encoded.extend(
        struct.pack(
            "<BBQQQQ",
            int(board.turn),
            int(board.chess960),
            board.clean_castling_rights(),
            board.promoted,
            state.series_number,
            state.quiet_series,
        )
    )
    targets = tuple(sorted(state.ep_targets))
    if len(targets) > 64 or any(not 0 <= square < 64 for square in targets):
        raise ValueError("progressive en-passant targets are not canonical squares")
    encoded.extend(struct.pack("<B", len(targets)))
    encoded.extend(bytes(targets))
    for text in (rules, quiet_policy):
        if len(text) > 65535:
            raise ValueError("state identity string exceeds the dedup-key encoding")
        encoded.extend(struct.pack("<H", len(text)))
        encoded.extend(text)
    return _domain_digest(b"spc-progressive-state-dedup-v1", bytes(encoded))


@dataclass(frozen=True, slots=True)
class CorpusRecord:
    attempt_index: int
    sequence_index: int
    state_key: bytes
    payload: bytes

    def __post_init__(self) -> None:
        if type(self.attempt_index) is not int or not 0 <= self.attempt_index < (1 << 64):
            raise ValueError("attempt_index must fit unsigned 64 bits")
        if type(self.sequence_index) is not int or not 0 <= self.sequence_index < (1 << 32):
            raise ValueError("sequence_index must fit unsigned 32 bits")
        state_key = bytes(self.state_key)
        payload = bytes(self.payload)
        if len(state_key) != 32 or state_key == _ZERO_DIGEST:
            raise ValueError("state_key must be a nonzero 32-byte digest")
        if len(payload) > _MAX_RECORD_PAYLOAD:
            raise ValueError("record payload exceeds the 64 MiB safety limit")
        object.__setattr__(self, "state_key", state_key)
        object.__setattr__(self, "payload", payload)

    @classmethod
    def from_state(
        cls,
        attempt_index: int,
        sequence_index: int,
        state: ProgressiveState,
        payload: bytes,
        *,
        ruleset_version: str = RULESET_VERSION,
    ) -> CorpusRecord:
        return cls(
            attempt_index,
            sequence_index,
            progressive_state_dedup_key(state, ruleset_version=ruleset_version),
            payload,
        )


@dataclass(frozen=True, slots=True)
class ShardMetadata:
    attempt_range: AttemptRange
    owner_sha256: str
    file: str
    sha256: str
    size_bytes: int
    record_count: int
    producer_receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.producer_receipt_sha256 is not None and (
            not isinstance(self.producer_receipt_sha256, str)
            or len(self.producer_receipt_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.producer_receipt_sha256
            )
            or self.producer_receipt_sha256 == "0" * 64
        ):
            raise CorpusStoreError(
                "producer_receipt_sha256 must be a nonzero lowercase SHA-256"
            )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "attempt_start": self.attempt_range.start,
            "attempt_stop": self.attempt_range.stop,
            "file": self.file,
            "owner_sha256": self.owner_sha256,
            "record_count": self.record_count,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }
        if self.producer_receipt_sha256 is not None:
            payload["producer_receipt_sha256"] = self.producer_receipt_sha256
        return payload


@dataclass(frozen=True, slots=True)
class _RangeClaim:
    attempt_range: AttemptRange
    owner_digest: bytes
    identity_digest: bytes
    producer_receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.producer_receipt_sha256 is not None and (
            not isinstance(self.producer_receipt_sha256, str)
            or len(self.producer_receipt_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.producer_receipt_sha256
            )
            or self.producer_receipt_sha256 == "0" * 64
        ):
            raise CorpusStoreError("claim producer receipt digest is invalid")

    def has_same_owner(self, other: _RangeClaim) -> bool:
        return (
            self.attempt_range == other.attempt_range
            and self.owner_digest == other.owner_digest
            and self.identity_digest == other.identity_digest
        )

    @property
    def file_name(self) -> str:
        return (
            f"claim-{self.attempt_range.start:020d}-{self.attempt_range.stop:020d}-"
            f"{self.owner_digest.hex()[:16]}.json"
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "attempt_start": self.attempt_range.start,
            "attempt_stop": self.attempt_range.stop,
            "format": CLAIM_FORMAT,
            "identity_sha256": self.identity_digest.hex(),
            "owner_sha256": self.owner_digest.hex(),
        }
        if self.producer_receipt_sha256 is not None:
            payload["producer_receipt_sha256"] = self.producer_receipt_sha256
        return payload


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).casefold()
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _exclusive_store_lock(root: Path) -> Iterator[None]:
    local_lock = _thread_lock(root)
    with local_lock:
        lock_path = root / ".corpus.lock"
        with lock_path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as error:
                        if error.errno not in {13, 36}:
                            raise
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _durable_replace(source: str | Path, destination: str | Path) -> None:
    if os.name == "nt":
        import ctypes

        move_file_replace_existing = 0x1
        move_file_write_through = 0x8
        move_file_ex = ctypes.windll.kernel32.MoveFileExW
        move_file_ex.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        move_file_ex.restype = ctypes.c_int
        if not move_file_ex(
            str(source),
            str(destination),
            move_file_replace_existing | move_file_write_through,
        ):
            raise ctypes.WinError()
        return
    os.replace(source, destination)
    _fsync_directory(Path(destination).parent)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    content = _canonical_json(payload) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _durable_replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _reject_duplicate_json_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise CorpusStoreError(f"JSON contains duplicate key {key!r}")
        payload[key] = value
    return payload


def _read_canonical_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CorpusStoreError(f"could not read canonical JSON {path.name}: {error}") from error
    if not isinstance(payload, dict) or raw != _canonical_json(payload) + b"\n":
        raise CorpusStoreError(f"{path.name} is not canonical corpus JSON")
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_exact(stream: BinaryIO, size: int, context: str) -> bytes:
    value = stream.read(size)
    if len(value) != size:
        raise ShardCorruptionError(f"truncated shard {context}")
    return value


def _unpack_shard_header(
    raw: bytes, path: Path
) -> tuple[AttemptRange, int, bytes]:
    (
        magic,
        version,
        header_size,
        flags,
        attempt_start,
        attempt_count,
        record_count,
        identity_digest,
        reserved_digest,
    ) = _SHARD_HEADER.unpack(raw)
    if (
        magic != SHARD_MAGIC
        or version != SHARD_VERSION
        or header_size != _SHARD_HEADER.size
        or flags != 0
        or attempt_count == 0
        or attempt_start + attempt_count > (1 << 64)
        or identity_digest == _ZERO_DIGEST
        or reserved_digest != _ZERO_DIGEST
    ):
        raise ShardCorruptionError(f"invalid shard header in {path.name}")
    try:
        attempt_range = AttemptRange(attempt_start, attempt_start + attempt_count)
    except ValueError as error:
        raise ShardCorruptionError(f"invalid attempt range in {path.name}") from error
    return attempt_range, record_count, identity_digest


def _scan_shard(
    path: Path,
    *,
    expected_identity: bytes,
    owner_sha256: str,
    producer_receipt_sha256: str | None = None,
) -> ShardMetadata:
    if (
        len(owner_sha256) != 64
        or any(character not in "0123456789abcdef" for character in owner_sha256)
        or owner_sha256 == "0" * 64
    ):
        raise CorpusStoreError("shard owner digest is invalid")
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as stream:
        raw_header = _read_exact(stream, _SHARD_HEADER.size, "header")
        digest.update(raw_header)
        size_bytes += len(raw_header)
        attempt_range, declared_count, identity_digest = (
            _unpack_shard_header(raw_header, path)
        )
        if identity_digest != expected_identity:
            raise CorpusIdentityError(
                f"shard {path.name} has a different corpus identity"
            )
        prior_order: tuple[int, int] | None = None
        for _ in range(declared_count):
            raw = _read_exact(stream, _RECORD_HEADER.size, "record header")
            digest.update(raw)
            size_bytes += len(raw)
            attempt_index, sequence_index, state_key, payload_size = _RECORD_HEADER.unpack(raw)
            if not attempt_range.contains(attempt_index):
                raise ShardCorruptionError(
                    f"record attempt {attempt_index} is outside {attempt_range}"
                )
            order = (attempt_index, sequence_index)
            if prior_order is not None and order <= prior_order:
                raise ShardCorruptionError("shard records are duplicated or not deterministic")
            if state_key == _ZERO_DIGEST or payload_size > _MAX_RECORD_PAYLOAD:
                raise ShardCorruptionError("shard record header is invalid")
            payload = _read_exact(stream, payload_size, "record payload")
            digest.update(payload)
            size_bytes += len(payload)
            prior_order = order
        if stream.read(1):
            raise ShardCorruptionError(f"shard {path.name} has trailing bytes")
    if path.stat().st_size != size_bytes:
        raise ShardCorruptionError(f"shard {path.name} changed while it was verified")
    sha256 = digest.hexdigest()
    expected_name = (
        f"shard-{attempt_range.start:020d}-{attempt_range.stop:020d}-"
        f"{sha256[:16]}.spcbin"
    )
    if path.name != expected_name:
        raise ShardCorruptionError(f"shard filename does not match its content: {path.name}")
    return ShardMetadata(
        attempt_range=attempt_range,
        owner_sha256=owner_sha256,
        file=f"shards/{path.name}",
        sha256=sha256,
        size_bytes=size_bytes,
        record_count=declared_count,
        producer_receipt_sha256=producer_receipt_sha256,
    )


def _metadata_from_dict(payload: Mapping[str, Any]) -> ShardMetadata:
    required = {
        "attempt_start",
        "attempt_stop",
        "file",
        "owner_sha256",
        "record_count",
        "sha256",
        "size_bytes",
    }
    if set(payload) not in (required, required | {"producer_receipt_sha256"}):
        raise CorpusStoreError("manifest shard entry has an invalid schema")
    numeric_names = ("attempt_start", "attempt_stop", "record_count", "size_bytes")
    if any(type(payload[name]) is not int for name in numeric_names):
        raise CorpusStoreError("manifest shard entry has invalid numbers")
    try:
        attempt_range = AttemptRange(payload["attempt_start"], payload["attempt_stop"])
        record_count = payload["record_count"]
        size_bytes = payload["size_bytes"]
    except ValueError as error:
        raise CorpusStoreError("manifest shard entry has invalid numbers") from error
    file = str(payload["file"])
    sha256 = str(payload["sha256"])
    owner_sha256 = str(payload["owner_sha256"])
    producer_receipt = payload.get("producer_receipt_sha256")
    expected_prefix = "shards/"
    if (
        not file.startswith(expected_prefix)
        or Path(file).as_posix() != file
        or len(Path(file).parts) != 2
        or len(sha256) != 64
        or len(owner_sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256 + owner_sha256)
        or ("producer_receipt_sha256" in payload and producer_receipt is None)
        or (
            producer_receipt is not None
            and (
                not isinstance(producer_receipt, str)
                or len(producer_receipt) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in producer_receipt
                )
                or producer_receipt == "0" * 64
            )
        )
        or record_count < 0
        or size_bytes < _SHARD_HEADER.size
    ):
        raise CorpusStoreError("manifest shard entry is invalid")
    return ShardMetadata(
        attempt_range,
        owner_sha256,
        file,
        sha256,
        size_bytes,
        record_count,
        producer_receipt,
    )


def _validate_nonoverlapping(shards: Sequence[ShardMetadata]) -> tuple[ShardMetadata, ...]:
    ordered = tuple(
        sorted(
            shards,
            key=lambda shard: (
                shard.attempt_range.start,
                shard.attempt_range.stop,
                shard.sha256,
            ),
        )
    )
    files: set[str] = set()
    digests: set[str] = set()
    prior: ShardMetadata | None = None
    for shard in ordered:
        if shard.file in files or shard.sha256 in digests:
            raise AttemptRangeConflict("manifest contains a duplicate shard")
        if prior is not None and prior.attempt_range.overlaps(shard.attempt_range):
            raise AttemptRangeConflict(
                f"overlapping finalized ranges {prior.attempt_range} and {shard.attempt_range}"
            )
        files.add(shard.file)
        digests.add(shard.sha256)
        prior = shard
    return ordered


def _manifest_payload(
    identity: CorpusIdentity,
    shards: Sequence[ShardMetadata],
) -> dict[str, Any]:
    ordered = _validate_nonoverlapping(shards)
    core: dict[str, Any] = {
        "format": MANIFEST_FORMAT,
        "identity": identity.as_dict(),
        "identity_sha256": identity.digest_hex,
        "schema_version": CORPUS_SCHEMA_VERSION,
        "shards": [shard.as_dict() for shard in ordered],
    }
    content_root = {
        **core,
        "shards": [
            {
                key: value
                for key, value in shard.as_dict().items()
                if key != "owner_sha256"
            }
            for shard in ordered
        ],
    }
    corpus_sha256 = _domain_digest(
        b"spc-corpus-manifest-root-v1", _canonical_json(content_root)
    ).hex()
    return {
        **core,
        "corpus_sha256": corpus_sha256,
        "totals": {
            "attempt_count": sum(
                shard.attempt_range.stop - shard.attempt_range.start
                for shard in ordered
            ),
            "record_count": sum(shard.record_count for shard in ordered),
            "shard_count": len(ordered),
        },
    }


class CorpusStore:
    """Content-addressed, shard-atomic corpus root.

    A shard file is made durable and atomically renamed before its manifest
    entry is published. If a process dies in that small two-file window, the
    next opener verifies and adopts the orphan shard. Partial ``.tmp`` files are
    never visible as corpus data, so a stable owner may safely restart a range.
    """

    def __init__(
        self,
        root: str | Path,
        identity: CorpusIdentity,
        *,
        protocol_root_binding_sha256: str | None = None,
        _protocol_read_only: bool = False,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.identity = identity
        if protocol_root_binding_sha256 is not None and (
            not isinstance(protocol_root_binding_sha256, str)
            or len(protocol_root_binding_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in protocol_root_binding_sha256
            )
        ):
            raise CorpusStoreError("protocol root-binding SHA-256 is malformed")
        self._protocol_root_binding_sha256 = protocol_root_binding_sha256
        self._protocol_read_only = bool(_protocol_read_only)
        self.shards_directory = self.root / "shards"
        self.claims_directory = self.root / "claims"
        self.manifest_path = self.root / "manifest.json"
        self._assert_protocol_root_access(write=not self._protocol_read_only)
        if self._protocol_read_only:
            if (
                not self.root.is_dir()
                or not self.shards_directory.is_dir()
                or not self.claims_directory.is_dir()
            ):
                raise CorpusStoreError(
                    "read-only corpus open requires a complete existing store"
                )
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self.shards_directory.mkdir(parents=True, exist_ok=True)
            self.claims_directory.mkdir(parents=True, exist_ok=True)
        with _exclusive_store_lock(self.root):
            self._assert_protocol_root_access(write=not self._protocol_read_only)
            if not self.manifest_path.exists():
                if self._protocol_read_only:
                    raise CorpusStoreError("read-only corpus open cannot create a manifest")
                _atomic_write_json(self.manifest_path, _manifest_payload(identity, ()))
            if self._protocol_read_only:
                self._load_manifest_locked()
            else:
                self._synchronize_locked()
        self.verify()

    @classmethod
    def open(cls, root: str | Path) -> CorpusStore:
        """Open an existing store from its canonical identity-bound manifest."""

        resolved = Path(root).expanduser().resolve()
        manifest_path = resolved / "manifest.json"
        payload = _read_canonical_json(manifest_path)
        identity_payload = payload.get("identity")
        required = {
            "generator_config_sha256",
            "profile_ids",
            "record_schema",
            "ruleset_version",
            "source_fingerprint",
        }
        if not isinstance(identity_payload, dict) or set(identity_payload) != required:
            raise CorpusIdentityError("manifest identity schema is invalid")
        profile_ids = identity_payload["profile_ids"]
        if not isinstance(profile_ids, list) or not all(
            isinstance(profile_id, str) for profile_id in profile_ids
        ):
            raise CorpusIdentityError("manifest profile_ids are invalid")
        text_fields = (
            "generator_config_sha256",
            "record_schema",
            "ruleset_version",
            "source_fingerprint",
        )
        if any(not isinstance(identity_payload[name], str) for name in text_fields):
            raise CorpusIdentityError("manifest identity strings are invalid")
        identity = CorpusIdentity(
            record_schema=identity_payload["record_schema"],
            source_fingerprint=identity_payload["source_fingerprint"],
            generator_config_sha256=identity_payload["generator_config_sha256"],
            profile_ids=tuple(profile_ids),
            ruleset_version=identity_payload["ruleset_version"],
        )
        return cls(resolved, identity, _protocol_read_only=True)

    def _protocol_root_binding_digest(self) -> str | None:
        start_path = self.root.with_name(
            self.root.name + ".cycle4-preregistration-generation-start.json"
        )
        binding_path = self.root / "cycle4-preregistration-root-binding.json"
        start_exists = start_path.exists()
        binding_exists = binding_path.exists()
        if not start_exists and not binding_exists:
            return None
        if not start_exists or not binding_exists:
            raise CorpusStoreError(
                "protocol corpus root has an incomplete external ownership binding"
            )
        try:
            start_raw = start_path.read_bytes()
            binding_raw = binding_path.read_bytes()
            start = json.loads(
                start_raw, object_pairs_hook=_reject_duplicate_json_pairs
            )
            binding = json.loads(
                binding_raw, object_pairs_hook=_reject_duplicate_json_pairs
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CorpusStoreError(
                f"protocol corpus ownership binding is unreadable: {error}"
            ) from error
        expected = {
            "schema": "spc-cycle4-trajectory-root-binding-v1",
            "root": str(self.root),
            "generation_start": {
                "path": str(start_path),
                "raw_artifact_sha256": hashlib.sha256(start_raw).hexdigest(),
            },
        }
        if binding != expected:
            raise CorpusStoreError("protocol corpus ownership binding differs")
        return hashlib.sha256(binding_raw).hexdigest()

    def _assert_protocol_root_access(self, *, write: bool) -> None:
        digest = self._protocol_root_binding_digest()
        if digest is None:
            if self._protocol_root_binding_sha256 is not None:
                raise CorpusStoreError("protocol root-binding token has no binding")
            return
        if self._protocol_root_binding_sha256 is not None:
            if self._protocol_root_binding_sha256 != digest:
                raise CorpusStoreError("protocol root-binding token differs")
            return
        if write:
            raise CorpusStoreError(
                "protocol-owned corpus root requires its exact root-binding token"
            )

    def _load_manifest_locked(self) -> tuple[ShardMetadata, ...]:
        payload = _read_canonical_json(self.manifest_path)
        if (
            payload.get("format") != MANIFEST_FORMAT
            or payload.get("schema_version") != CORPUS_SCHEMA_VERSION
            or payload.get("identity") != self.identity.as_dict()
            or payload.get("identity_sha256") != self.identity.digest_hex
            or not isinstance(payload.get("shards"), list)
        ):
            raise CorpusIdentityError("manifest identity or schema does not match")
        shards = _validate_nonoverlapping(
            tuple(_metadata_from_dict(item) for item in payload["shards"])
        )
        expected = _manifest_payload(self.identity, shards)
        if payload != expected:
            raise CorpusStoreError("manifest root digest or totals are invalid")
        return shards

    def _publish_manifest_locked(self, shards: Sequence[ShardMetadata]) -> None:
        self._assert_protocol_root_access(write=True)
        _atomic_write_json(self.manifest_path, _manifest_payload(self.identity, shards))

    def _claim_from_path(self, path: Path) -> _RangeClaim:
        payload = _read_canonical_json(path)
        required = {
            "attempt_start",
            "attempt_stop",
            "format",
            "identity_sha256",
            "owner_sha256",
        }
        if (
            set(payload)
            not in (required, required | {"producer_receipt_sha256"})
            or payload["format"] != CLAIM_FORMAT
        ):
            raise CorpusStoreError(f"claim {path.name} has an invalid schema")
        producer_receipt = payload.get("producer_receipt_sha256")
        if (
            type(payload["attempt_start"]) is not int
            or type(payload["attempt_stop"]) is not int
            or not isinstance(payload["owner_sha256"], str)
            or not isinstance(payload["identity_sha256"], str)
            or len(payload["owner_sha256"]) != 64
            or len(payload["identity_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in payload["owner_sha256"] + payload["identity_sha256"]
            )
            or payload["owner_sha256"] == "0" * 64
            or ("producer_receipt_sha256" in payload and producer_receipt is None)
            or (
                producer_receipt is not None
                and (
                    not isinstance(producer_receipt, str)
                    or len(producer_receipt) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in producer_receipt
                    )
                    or producer_receipt == "0" * 64
                )
            )
        ):
            raise CorpusStoreError(f"claim {path.name} is invalid")
        try:
            claim = _RangeClaim(
                AttemptRange(payload["attempt_start"], payload["attempt_stop"]),
                bytes.fromhex(payload["owner_sha256"]),
                bytes.fromhex(payload["identity_sha256"]),
                producer_receipt,
            )
        except ValueError as error:
            raise CorpusStoreError(f"claim {path.name} is invalid") from error
        if (
            len(claim.owner_digest) != 32
            or len(claim.identity_digest) != 32
            or path.name != claim.file_name
            or claim.identity_digest != self.identity.digest
        ):
            raise CorpusIdentityError(f"claim {path.name} has a different identity")
        return claim

    def _claims_locked(self) -> tuple[tuple[Path, _RangeClaim], ...]:
        claims = tuple(
            (path, self._claim_from_path(path))
            for path in sorted(self.claims_directory.glob("claim-*.json"))
        )
        for index, (_, claim) in enumerate(claims):
            for _, other in claims[index + 1 :]:
                if claim.attempt_range.overlaps(other.attempt_range):
                    raise AttemptRangeConflict(
                        f"overlapping active claims {claim.attempt_range} and {other.attempt_range}"
                    )
        return claims

    def _synchronize_locked(self) -> tuple[ShardMetadata, ...]:
        self._assert_protocol_root_access(write=True)
        shards = list(self._load_manifest_locked())
        claims = self._claims_locked()
        known_files = {shard.file for shard in shards}
        changed = False
        for path in sorted(self.shards_directory.glob("*.spcbin")):
            relative = f"shards/{path.name}"
            if relative in known_files:
                continue
            with path.open("rb") as stream:
                raw_header = _read_exact(stream, _SHARD_HEADER.size, "header")
            orphan_range, _, orphan_identity = _unpack_shard_header(raw_header, path)
            matching = [
                claim
                for _, claim in claims
                if claim.attempt_range == orphan_range
                and claim.identity_digest == orphan_identity
            ]
            if len(matching) != 1:
                raise CorpusStoreError(
                    f"orphan shard {path.name} has no exact active attempt claim"
                )
            recovered = _scan_shard(
                path,
                expected_identity=self.identity.digest,
                owner_sha256=matching[0].owner_digest.hex(),
                producer_receipt_sha256=matching[0].producer_receipt_sha256,
            )
            shards.append(recovered)
            known_files.add(relative)
            changed = True
        ordered = _validate_nonoverlapping(shards)
        if changed:
            self._publish_manifest_locked(ordered)

        completed = {
            (
                shard.attempt_range,
                shard.owner_sha256,
            )
            for shard in ordered
        }
        for path, claim in claims:
            if (claim.attempt_range, claim.owner_digest.hex()) in completed:
                path.unlink()
                _fsync_directory(self.claims_directory)
        return ordered

    @property
    def manifest(self) -> dict[str, Any]:
        with _exclusive_store_lock(self.root):
            self._assert_protocol_root_access(write=not self._protocol_read_only)
            if self._protocol_read_only:
                self._load_manifest_locked()
            else:
                self._synchronize_locked()
            return _read_canonical_json(self.manifest_path)

    @property
    def shards(self) -> tuple[ShardMetadata, ...]:
        return self._verified_shards()

    def begin_shard(
        self,
        attempt_start: int,
        attempt_stop: int,
        *,
        owner_id: str,
    ) -> CorpusShardWriter:
        self._assert_protocol_root_access(write=True)
        attempt_range = AttemptRange(attempt_start, attempt_stop)
        claim = _RangeClaim(attempt_range, _owner_digest(owner_id), self.identity.digest)
        with _exclusive_store_lock(self.root):
            shards = self._synchronize_locked()
            for shard in shards:
                if shard.attempt_range.overlaps(attempt_range):
                    raise AttemptRangeConflict(
                        f"attempt range {attempt_range} overlaps finalized {shard.attempt_range}"
                    )
            claims = self._claims_locked()
            matching_claim: _RangeClaim | None = None
            for _, active in claims:
                if not active.attempt_range.overlaps(attempt_range):
                    continue
                if active.has_same_owner(claim):
                    matching_claim = active
                    continue
                raise AttemptRangeConflict(
                    f"attempt range {attempt_range} overlaps active {active.attempt_range}"
                )
            if matching_claim is None:
                _atomic_write_json(self.claims_directory / claim.file_name, claim.as_dict())
            else:
                claim = matching_claim
        return CorpusShardWriter(self, claim)

    def release_claim(
        self,
        attempt_start: int,
        attempt_stop: int,
        *,
        owner_id: str,
    ) -> None:
        claim = _RangeClaim(
            AttemptRange(attempt_start, attempt_stop),
            _owner_digest(owner_id),
            self.identity.digest,
        )
        self._release_claim(claim)

    def _release_claim(self, claim: _RangeClaim) -> None:
        self._assert_protocol_root_access(write=True)
        path = self.claims_directory / claim.file_name
        with _exclusive_store_lock(self.root):
            self._assert_protocol_root_access(write=True)
            if path.exists():
                existing = self._claim_from_path(path)
                if not existing.has_same_owner(claim):
                    raise CorpusIdentityError("attempt claim identity changed")
                path.unlink()
                _fsync_directory(self.claims_directory)

    def _finalize(
        self,
        claim: _RangeClaim,
        temporary_path: Path,
        record_count: int,
        before_publish: Callable[[ShardMetadata], str | None] | None = None,
    ) -> ShardMetadata:
        self._assert_protocol_root_access(write=True)
        sha256 = _sha256_file(temporary_path)
        final_name = (
            f"shard-{claim.attempt_range.start:020d}-{claim.attempt_range.stop:020d}-"
            f"{sha256[:16]}.spcbin"
        )
        final_path = self.shards_directory / final_name
        with _exclusive_store_lock(self.root):
            shards = list(self._synchronize_locked())
            claim_path = self.claims_directory / claim.file_name
            if not claim_path.is_file() or self._claim_from_path(claim_path) != claim:
                raise CorpusStoreError("attempt range is no longer owned by this writer")
            for shard in shards:
                if shard.attempt_range.overlaps(claim.attempt_range):
                    raise AttemptRangeConflict(
                        f"attempt range overlaps finalized {shard.attempt_range}"
                    )
            prepared = ShardMetadata(
                attempt_range=claim.attempt_range,
                owner_sha256=claim.owner_digest.hex(),
                file=f"shards/{final_name}",
                sha256=sha256,
                size_bytes=temporary_path.stat().st_size,
                record_count=record_count,
                producer_receipt_sha256=claim.producer_receipt_sha256,
            )
            if final_path.exists():
                existing = _scan_shard(
                    final_path,
                    expected_identity=self.identity.digest,
                    owner_sha256=claim.owner_digest.hex(),
                    producer_receipt_sha256=claim.producer_receipt_sha256,
                )
                if existing.sha256 != sha256 or existing.record_count != record_count:
                    raise AttemptRangeConflict("content-addressed shard path is already occupied")
            published_receipt = (
                None if before_publish is None else before_publish(prepared)
            )
            if published_receipt is not None and (
                not isinstance(published_receipt, str)
                or len(published_receipt) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in published_receipt
                )
                or published_receipt == "0" * 64
            ):
                raise CorpusStoreError(
                    "before_publish returned an invalid producer receipt digest"
                )
            if (
                claim.producer_receipt_sha256 is not None
                and published_receipt is not None
                and claim.producer_receipt_sha256 != published_receipt
            ):
                raise CorpusStoreError(
                    "producer receipt digest changed while retrying a claimed range"
                )
            bound_receipt = claim.producer_receipt_sha256 or published_receipt
            if bound_receipt != claim.producer_receipt_sha256:
                claim = replace(claim, producer_receipt_sha256=bound_receipt)
                _atomic_write_json(claim_path, claim.as_dict())
                if self._claim_from_path(claim_path) != claim:
                    raise CorpusStoreError("producer receipt claim binding changed")
            prepared = replace(
                prepared,
                producer_receipt_sha256=bound_receipt,
            )
            if final_path.exists():
                temporary_path.unlink()
            else:
                _durable_replace(temporary_path, final_path)
            metadata = _scan_shard(
                final_path,
                expected_identity=self.identity.digest,
                owner_sha256=claim.owner_digest.hex(),
                producer_receipt_sha256=bound_receipt,
            )
            if metadata != prepared:
                raise ShardCorruptionError("finalized shard metadata changed")
            shards.append(metadata)
            ordered = _validate_nonoverlapping(shards)
            self._publish_manifest_locked(ordered)
            claim_path.unlink()
            _fsync_directory(self.claims_directory)
            return metadata

    def _verified_shards(self) -> tuple[ShardMetadata, ...]:
        with _exclusive_store_lock(self.root):
            self._assert_protocol_root_access(write=not self._protocol_read_only)
            shards = (
                self._load_manifest_locked()
                if self._protocol_read_only
                else self._synchronize_locked()
            )
        verified: list[ShardMetadata] = []
        for expected in shards:
            path = self.root / expected.file
            if not path.is_file():
                raise ShardCorruptionError(f"manifest shard is missing: {expected.file}")
            actual = _scan_shard(
                path,
                expected_identity=self.identity.digest,
                owner_sha256=expected.owner_sha256,
                producer_receipt_sha256=expected.producer_receipt_sha256,
            )
            if actual != expected:
                raise ShardCorruptionError(f"manifest metadata changed for {expected.file}")
            verified.append(actual)
        return tuple(verified)

    def verify(self) -> dict[str, Any]:
        payload = _manifest_payload(self.identity, self._verified_shards())
        return {
            "corpus_sha256": payload["corpus_sha256"],
            **payload["totals"],
        }

    def verified_snapshot(
        self,
    ) -> tuple[dict[str, Any], tuple[ShardMetadata, ...]]:
        """Freeze one verified manifest view for a bounded corpus consumer.

        The returned shard tuple is content-addressed. Consumers that need the
        records behind this exact manifest must pass it to
        ``iter_snapshot_records`` and then compare a second snapshot before
        publishing any result. This prevents a growing store from mixing the
        identity/window from one manifest with records from another.
        """

        shards = self._verified_shards()
        payload = _manifest_payload(self.identity, shards)
        return (
            {
                "corpus_sha256": payload["corpus_sha256"],
                **payload["totals"],
            },
            shards,
        )

    def iter_snapshot_records(
        self,
        shards: Sequence[ShardMetadata],
        *,
        deduplicate_states: bool = False,
    ) -> Iterator[CorpusRecord]:
        """Yield records parsed and hashed from the exact frozen shard bytes."""

        ordered = _validate_nonoverlapping(tuple(shards))
        seen: set[bytes] | None = set() if deduplicate_states else None
        for expected in ordered:
            path = self.root / expected.file
            digest = hashlib.sha256()
            size_bytes = 0
            records: list[CorpusRecord] = []
            with path.open("rb") as stream:
                raw_header = _read_exact(stream, _SHARD_HEADER.size, "header")
                digest.update(raw_header)
                size_bytes += len(raw_header)
                attempt_range, declared_count, identity_digest = _unpack_shard_header(
                    raw_header, path
                )
                if identity_digest != self.identity.digest:
                    raise CorpusIdentityError(
                        f"shard {path.name} has a different corpus identity"
                    )
                if attempt_range != expected.attempt_range:
                    raise ShardCorruptionError(
                        f"shard {path.name} attempt range changed from the snapshot"
                    )
                if declared_count != expected.record_count:
                    raise ShardCorruptionError(
                        f"shard {path.name} record count changed from the snapshot"
                    )
                prior_order: tuple[int, int] | None = None
                for _ in range(declared_count):
                    raw = _read_exact(stream, _RECORD_HEADER.size, "record header")
                    digest.update(raw)
                    size_bytes += len(raw)
                    attempt, sequence, state_key, payload_size = _RECORD_HEADER.unpack(raw)
                    if not attempt_range.contains(attempt):
                        raise ShardCorruptionError(
                            f"record attempt {attempt} is outside {attempt_range}"
                        )
                    order = (attempt, sequence)
                    if prior_order is not None and order <= prior_order:
                        raise ShardCorruptionError(
                            "shard records are duplicated or not deterministic"
                        )
                    if state_key == _ZERO_DIGEST or payload_size > _MAX_RECORD_PAYLOAD:
                        raise ShardCorruptionError("shard record header is invalid")
                    payload = _read_exact(stream, payload_size, "record payload")
                    digest.update(payload)
                    size_bytes += len(payload)
                    prior_order = order
                    records.append(CorpusRecord(attempt, sequence, state_key, payload))
                if stream.read(1):
                    raise ShardCorruptionError(f"shard {path.name} has trailing bytes")
                if os.fstat(stream.fileno()).st_size != size_bytes:
                    raise ShardCorruptionError(
                        f"shard {path.name} changed while its snapshot was read"
                    )
            if size_bytes != expected.size_bytes or digest.hexdigest() != expected.sha256:
                raise ShardCorruptionError(
                    f"shard {path.name} bytes changed from the verified snapshot"
                )
            for record in records:
                if seen is not None:
                    if record.state_key in seen:
                        continue
                    seen.add(record.state_key)
                yield record

    def iter_records(self, *, deduplicate_states: bool = False) -> Iterator[CorpusRecord]:
        """Yields deterministic range/attempt order, optionally keeping first state."""

        shards = self._verified_shards()
        seen: set[bytes] | None = set() if deduplicate_states else None
        for shard in shards:
            path = self.root / shard.file
            with path.open("rb") as stream:
                _read_exact(stream, _SHARD_HEADER.size, "header")
                for _ in range(shard.record_count):
                    raw = _read_exact(stream, _RECORD_HEADER.size, "record header")
                    attempt, sequence, state_key, payload_size = _RECORD_HEADER.unpack(raw)
                    payload = _read_exact(stream, payload_size, "record payload")
                    if seen is not None:
                        if state_key in seen:
                            continue
                        seen.add(state_key)
                    yield CorpusRecord(attempt, sequence, state_key, payload)


class CorpusShardWriter:
    """Streaming binary writer for one claimed attempt range."""

    def __init__(self, store: CorpusStore, claim: _RangeClaim) -> None:
        self._store = store
        self._claim = claim
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{claim.file_name}.", suffix=".tmp", dir=store.shards_directory
        )
        self._temporary_path = Path(temporary_name)
        self._stream: BinaryIO | None = os.fdopen(descriptor, "w+b")
        self._record_count = 0
        self._prior_order: tuple[int, int] | None = None
        self._finalized: ShardMetadata | None = None
        self._write_header(record_count=0)

    @property
    def attempt_range(self) -> AttemptRange:
        return self._claim.attempt_range

    @property
    def record_count(self) -> int:
        return self._record_count

    def _write_header(self, *, record_count: int) -> None:
        assert self._stream is not None
        self._stream.seek(0)
        self._stream.write(
            _SHARD_HEADER.pack(
                SHARD_MAGIC,
                SHARD_VERSION,
                _SHARD_HEADER.size,
                0,
                self.attempt_range.start,
                self.attempt_range.stop - self.attempt_range.start,
                record_count,
                self._store.identity.digest,
                _ZERO_DIGEST,
            )
        )
        self._stream.seek(0, os.SEEK_END)

    def add(self, record: CorpusRecord) -> None:
        if self._stream is None:
            raise CorpusStoreError("shard writer is closed")
        if not self.attempt_range.contains(record.attempt_index):
            raise ValueError("record attempt is outside this writer's claimed range")
        order = (record.attempt_index, record.sequence_index)
        if self._prior_order is not None and order <= self._prior_order:
            raise ValueError("records must be added in strict attempt/sequence order")
        self._stream.write(
            _RECORD_HEADER.pack(
                record.attempt_index,
                record.sequence_index,
                record.state_key,
                len(record.payload),
            )
        )
        self._stream.write(record.payload)
        self._prior_order = order
        self._record_count += 1

    def add_state(
        self,
        attempt_index: int,
        sequence_index: int,
        state: ProgressiveState,
        payload: bytes,
    ) -> None:
        if self._store.identity.ruleset_version != RULESET_VERSION:
            raise CorpusIdentityError(
                "the active ProgressiveState ruleset does not match this writable store"
            )
        self.add(
            CorpusRecord.from_state(
                attempt_index,
                sequence_index,
                state,
                payload,
                ruleset_version=self._store.identity.ruleset_version,
            )
        )

    def finalize(
        self,
        *,
        before_publish: Callable[[ShardMetadata], str | None] | None = None,
    ) -> ShardMetadata:
        """Durably finalize this shard, optionally publishing bound side data first.

        ``before_publish`` runs under the store lock after the final shard hash is
        known but before the shard can be renamed or entered in the manifest.
        The callback must be deterministic and durable; when it returns a
        SHA-256, that digest is saved in the active claim and manifest content
        root.  This ordering lets a format-specific layer publish a bound
        sidecar without leaving a finalized shard that loses provenance after
        a crash.
        """

        if self._finalized is not None:
            return self._finalized
        if self._stream is None:
            raise CorpusStoreError("shard writer is closed")
        self._write_header(record_count=self._record_count)
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        self._stream = None
        self._finalized = self._store._finalize(
            self._claim,
            self._temporary_path,
            self._record_count,
            before_publish,
        )
        return self._finalized

    def abort(self, *, release_claim: bool = False) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        try:
            self._temporary_path.unlink()
        except FileNotFoundError:
            pass
        if release_claim:
            self._store._release_claim(self._claim)

    def __enter__(self) -> CorpusShardWriter:
        return self

    def __exit__(self, *_: object) -> None:
        if self._finalized is None:
            self.abort()
