from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import hashlib
import json
import re
import struct
from typing import Iterable, Sequence

import chess

from . import evaluation
from .model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    RULESET_VERSION,
    Outcome,
    ProgressiveState,
    SeriesResult,
)
from .profiles import EngineProfile
from .rules import SeriesLegalityError, play_series


FULL_GAME_V2_REQUEST_MAGIC = b"SPCFGR02"
FULL_GAME_V2_RESPONSE_MAGIC = b"SPCFGB02"
FULL_GAME_V2_VERSION = 2
FULL_GAME_V2_REQUEST_HEADER_SIZE = 144
FULL_GAME_V2_RESPONSE_HEADER_SIZE = 80
FULL_GAME_V2_RECORD_HEADER_SIZE = 64
FULL_GAME_V2_PROFILE_SIZE = 72
FULL_GAME_V2_MAX_PROFILES = 4096
FULL_GAME_V2_MAX_ATTEMPTS = (1 << 32) - 1
FULL_GAME_POLICY_PRESERVE_MATE = 1
NATIVE_CORPUS_PROFILE_SCHEMA = "spc-native-corpus-profile-v1"
POLICY_BASIS_POINTS = 10_000
UINT16_MAX = (1 << 16) - 1
UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1

_REQUEST_HEADER = struct.Struct("<8sHHIQQQQQQQQIIHHIHHHHHH32sI")
_PROFILE_RECORD = struct.Struct("<32sqqqqq")
_RESPONSE_HEADER = struct.Struct("<8sHHHHQQ32sIHHQ")
_RESPONSE_RECORD = struct.Struct("<QQBBBBIIIQQQQ")
_PROFILE_ID_RE = re.compile(r"spc-[0-9a-f]{16}\Z")
_SOURCE_FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}\Z")
_NATIVE_WEIGHT_NAMES = (
    "material",
    "king_space",
    "promotion_corridors",
    "immediate_vulnerability",
    "boundary_check",
)

assert _REQUEST_HEADER.size == FULL_GAME_V2_REQUEST_HEADER_SIZE
assert _PROFILE_RECORD.size == FULL_GAME_V2_PROFILE_SIZE
assert _RESPONSE_HEADER.size == FULL_GAME_V2_RESPONSE_HEADER_SIZE
assert _RESPONSE_RECORD.size == FULL_GAME_V2_RECORD_HEADER_SIZE


class NativeCorpusError(RuntimeError):
    """Base error for the fail-closed native corpus boundary."""


class NativeCorpusUnavailable(NativeCorpusError):
    """The exact source-matched native batch kernel is unavailable."""


class NativeCorpusProtocolError(NativeCorpusError, ValueError):
    """A request or response violates the canonical v2 binary protocol."""


class NativeCorpusIdentityError(NativeCorpusError, ValueError):
    """A generation request claims a different engine or rules identity."""


class NativeCorpusReplayError(NativeCorpusError, ValueError):
    """A native trace does not replay under the authoritative Python rules."""


class NativeTerminal(IntEnum):
    NONE = 0
    CHECKMATE_WHITE = 1
    CHECKMATE_BLACK = 2
    STALEMATE = 3
    TEN_SERIES_DRAW = 4


class NativeReject(IntEnum):
    NONE = 0
    MANUAL_PROOF_REQUIRED = 1
    WORK_LIMIT = 2
    COORDINATOR_CANCELLED = 3
    OVERFLOW = 4
    TECHNICAL_SERIES_WATCHDOG = 5
    INTERNAL_ERROR = 6


class NativePolicyKind(IntEnum):
    UNIFORM = 1
    RANK_MIXTURE_BASIS_POINTS = 2


class NativeProfileSchedule(IntEnum):
    SELF_ROUND_ROBIN = 1
    ORDERED_PAIR_ROUND_ROBIN = 2


def _require_integer(name: str, value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _require_identity_text(name: str, value: object, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{name} must be a nonempty string of at most {maximum} characters")
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        raise ValueError(f"{name} must contain printable ASCII only")
    return value


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


@dataclass(frozen=True, slots=True)
class NativeRankPolicy:
    """Deterministic sampling policy over the native ordered candidate list.

    Mate preservation is deliberately not configurable here. Production corpus
    generation always pins the native v2 safety flag so a returned mating
    series cannot be discarded by exploratory sampling.
    """

    kind: NativePolicyKind = NativePolicyKind.RANK_MIXTURE_BASIS_POINTS
    top_weight_basis_points: int = 8_000
    near_weight_basis_points: int = 1_500
    tail_weight_basis_points: int = 500
    top_rank_count: int = 1
    near_rank_count: int = 3

    def __post_init__(self) -> None:
        try:
            kind = NativePolicyKind(self.kind)
        except (TypeError, ValueError) as error:
            raise ValueError("unsupported native corpus policy kind") from error
        object.__setattr__(self, "kind", kind)
        for name in (
            "top_weight_basis_points",
            "near_weight_basis_points",
            "tail_weight_basis_points",
            "top_rank_count",
            "near_rank_count",
        ):
            _require_integer(name, getattr(self, name), 0, UINT16_MAX)
        if kind is NativePolicyKind.UNIFORM:
            if any(
                (
                    self.top_weight_basis_points,
                    self.near_weight_basis_points,
                    self.tail_weight_basis_points,
                    self.top_rank_count,
                    self.near_rank_count,
                )
            ):
                raise ValueError("uniform native corpus policy fields must all be zero")
            return
        if (
            self.top_weight_basis_points
            + self.near_weight_basis_points
            + self.tail_weight_basis_points
            != POLICY_BASIS_POINTS
        ):
            raise ValueError("rank policy weights must total 10000 basis points")
        if self.top_weight_basis_points == 0 or self.top_rank_count == 0:
            raise ValueError("rank policy must allocate a nonempty top band")
        if self.near_rank_count == 0 and self.near_weight_basis_points != 0:
            raise ValueError("an empty near band cannot have sampling weight")

    @classmethod
    def uniform(cls) -> NativeRankPolicy:
        return cls(
            kind=NativePolicyKind.UNIFORM,
            top_weight_basis_points=0,
            near_weight_basis_points=0,
            tail_weight_basis_points=0,
            top_rank_count=0,
            near_rank_count=0,
        )

    def validate_candidate_count(self, candidate_count: int) -> None:
        if self.kind is NativePolicyKind.UNIFORM:
            return
        ranked = self.top_rank_count + self.near_rank_count
        if ranked > candidate_count:
            raise ValueError("rank policy bands exceed candidate_count")
        if ranked == candidate_count and self.tail_weight_basis_points != 0:
            raise ValueError("rank policy tail has weight but no candidate lane")

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "kind": int(self.kind),
            "preserve_returned_mate": True,
            "top_weight_basis_points": self.top_weight_basis_points,
            "near_weight_basis_points": self.near_weight_basis_points,
            "tail_weight_basis_points": self.tail_weight_basis_points,
            "top_rank_count": self.top_rank_count,
            "near_rank_count": self.near_rank_count,
        }


@dataclass(frozen=True, slots=True)
class NativeCorpusConfig:
    seed: int = 0
    max_attempt_series: int = 64
    max_frontier_states: int = 96
    max_positions_per_series: int = 1_000_000
    max_positions_per_game: int = 50_000_000
    candidate_count: int = 32
    policy: NativeRankPolicy = field(default_factory=NativeRankPolicy)
    schedule: NativeProfileSchedule = NativeProfileSchedule.SELF_ROUND_ROBIN
    engine_version: str = ENGINE_VERSION
    engine_source_fingerprint: str = ENGINE_SOURCE_FINGERPRINT
    ruleset_version: str = RULESET_VERSION

    def __post_init__(self) -> None:
        _require_integer("seed", self.seed, 0, UINT64_MAX)
        _require_integer("max_attempt_series", self.max_attempt_series, 0, UINT64_MAX)
        _require_integer("max_frontier_states", self.max_frontier_states, 1, UINT64_MAX)
        _require_integer(
            "max_positions_per_series", self.max_positions_per_series, 1, UINT64_MAX
        )
        _require_integer(
            "max_positions_per_game", self.max_positions_per_game, 1, UINT64_MAX
        )
        _require_integer("candidate_count", self.candidate_count, 1, UINT32_MAX)
        if self.candidate_count > self.max_frontier_states:
            raise ValueError("candidate_count cannot exceed max_frontier_states")
        if not isinstance(self.policy, NativeRankPolicy):
            raise ValueError("policy must be a NativeRankPolicy")
        self.policy.validate_candidate_count(self.candidate_count)
        try:
            schedule = NativeProfileSchedule(self.schedule)
        except (TypeError, ValueError) as error:
            raise ValueError("unsupported native profile schedule") from error
        object.__setattr__(self, "schedule", schedule)
        _require_identity_text("engine_version", self.engine_version)
        _require_identity_text("ruleset_version", self.ruleset_version)
        if not isinstance(self.engine_source_fingerprint, str) or not (
            _SOURCE_FINGERPRINT_RE.fullmatch(self.engine_source_fingerprint)
        ):
            raise ValueError(
                "engine_source_fingerprint must be 16 lowercase hexadecimal characters"
            )

    def as_semantic_dict(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "max_attempt_series": self.max_attempt_series,
            "max_frontier_states": self.max_frontier_states,
            "max_positions_per_series": self.max_positions_per_series,
            "max_positions_per_game": self.max_positions_per_game,
            "candidate_count": self.candidate_count,
            "policy": self.policy.as_dict(),
            "schedule": int(self.schedule),
            "engine_version": self.engine_version,
            "engine_source_fingerprint": self.engine_source_fingerprint,
            "ruleset_version": self.ruleset_version,
        }


@dataclass(frozen=True, slots=True)
class NativeCorpusProfile:
    profile_id: str
    digest: bytes
    material: int
    king_space: int
    promotion_corridors: int
    immediate_vulnerability: int
    boundary_check: int

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not _PROFILE_ID_RE.fullmatch(
            self.profile_id
        ):
            raise ValueError("native corpus profile_id is not canonical")
        if not isinstance(self.digest, bytes) or len(self.digest) != 32 or not any(
            self.digest
        ):
            raise ValueError("native corpus profile digest must be 32 nonzero bytes")
        for name in _NATIVE_WEIGHT_NAMES:
            _require_integer(name, getattr(self, name), 25, 300)
        expected_digest = hashlib.sha256(
            _canonical_json(
                {
                    "schema": NATIVE_CORPUS_PROFILE_SCHEMA,
                    "profile_id": self.profile_id,
                    "native_weights": {
                        name: getattr(self, name) for name in _NATIVE_WEIGHT_NAMES
                    },
                }
            )
        ).digest()
        if self.digest != expected_digest:
            raise ValueError(
                "native corpus profile digest does not match its canonical preimage"
            )

    @property
    def weights(self) -> tuple[int, int, int, int, int]:
        return tuple(getattr(self, name) for name in _NATIVE_WEIGHT_NAMES)  # type: ignore[return-value]

    def as_semantic_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "digest_sha256": self.digest.hex(),
            "native_weights": {
                name: getattr(self, name) for name in _NATIVE_WEIGHT_NAMES
            },
        }

    @classmethod
    def from_engine_profile(cls, profile: EngineProfile) -> NativeCorpusProfile:
        if not isinstance(profile, EngineProfile):
            raise TypeError("profile must be an EngineProfile")
        payload = {
            "schema": NATIVE_CORPUS_PROFILE_SCHEMA,
            "profile_id": profile.profile_id,
            "native_weights": {
                name: getattr(profile.weights, name) for name in _NATIVE_WEIGHT_NAMES
            },
        }
        digest = hashlib.sha256(_canonical_json(payload)).digest()
        return cls(
            profile_id=profile.profile_id,
            digest=digest,
            **{
                name: getattr(profile.weights, name) for name in _NATIVE_WEIGHT_NAMES
            },
        )


def bind_native_profiles(
    profiles: Sequence[EngineProfile | NativeCorpusProfile],
) -> tuple[NativeCorpusProfile, ...]:
    if isinstance(profiles, (str, bytes, bytearray)):
        raise TypeError("profiles must be a sequence of engine profiles")
    bound = tuple(
        profile
        if isinstance(profile, NativeCorpusProfile)
        else NativeCorpusProfile.from_engine_profile(profile)
        for profile in profiles
    )
    if not 1 <= len(bound) <= FULL_GAME_V2_MAX_PROFILES:
        raise ValueError(
            f"profile count must be between 1 and {FULL_GAME_V2_MAX_PROFILES}"
        )
    digests = {profile.digest for profile in bound}
    if len(digests) != len(bound):
        raise ValueError("native corpus profile digests must be unique")
    return bound


def semantic_config_digest(
    config: NativeCorpusConfig,
    profiles: Sequence[EngineProfile | NativeCorpusProfile],
) -> bytes:
    if not isinstance(config, NativeCorpusConfig):
        raise TypeError("config must be a NativeCorpusConfig")
    bound = bind_native_profiles(profiles)
    payload = {
        "schema": "spc-native-full-game-v2-semantic-config-v1",
        "abi_version": FULL_GAME_V2_VERSION,
        "config": config.as_semantic_dict(),
        "profiles": [profile.as_semantic_dict() for profile in bound],
    }
    digest = hashlib.sha256(_canonical_json(payload)).digest()
    if not any(digest):  # Defensive completeness for the native nonzero contract.
        raise AssertionError("SHA-256 unexpectedly produced an all-zero digest")
    return digest


def _validate_attempt_range(first_attempt: int, attempt_count: int) -> None:
    _require_integer("first_attempt", first_attempt, 0, UINT64_MAX)
    _require_integer("attempt_count", attempt_count, 1, FULL_GAME_V2_MAX_ATTEMPTS)
    if first_attempt > UINT64_MAX - (attempt_count - 1):
        raise ValueError("native corpus attempt range overflows uint64")


def encode_full_game_v2_request(
    config: NativeCorpusConfig,
    profiles: Sequence[EngineProfile | NativeCorpusProfile],
    *,
    first_attempt: int,
    attempt_count: int,
) -> bytes:
    """Build one canonical immutable SPCFGR02 request.

    The semantic digest intentionally excludes ``first_attempt`` and
    ``attempt_count``. Adjacent shards therefore share one corpus identity while
    the response still echoes and validates each exact attempt interval.
    """

    if not isinstance(config, NativeCorpusConfig):
        raise TypeError("config must be a NativeCorpusConfig")
    _validate_attempt_range(first_attempt, attempt_count)
    bound = bind_native_profiles(profiles)
    digest = semantic_config_digest(config, bound)
    request_size = FULL_GAME_V2_REQUEST_HEADER_SIZE + len(bound) * FULL_GAME_V2_PROFILE_SIZE
    header = _REQUEST_HEADER.pack(
        FULL_GAME_V2_REQUEST_MAGIC,
        FULL_GAME_V2_VERSION,
        FULL_GAME_V2_REQUEST_HEADER_SIZE,
        0,
        request_size,
        first_attempt,
        attempt_count,
        config.seed,
        config.max_attempt_series,
        config.max_frontier_states,
        config.max_positions_per_series,
        config.max_positions_per_game,
        config.candidate_count,
        len(bound),
        int(config.policy.kind),
        int(config.schedule),
        FULL_GAME_POLICY_PRESERVE_MATE,
        config.policy.top_weight_basis_points,
        config.policy.near_weight_basis_points,
        config.policy.tail_weight_basis_points,
        config.policy.top_rank_count,
        config.policy.near_rank_count,
        0,
        digest,
        0,
    )
    records = b"".join(
        _PROFILE_RECORD.pack(profile.digest, *profile.weights) for profile in bound
    )
    request = header + records
    if len(request) != request_size:
        raise AssertionError("native corpus request encoder size drifted")
    return request


def _unpack_enum(enum_type: type[IntEnum], value: int, name: str) -> IntEnum:
    try:
        return enum_type(value)
    except ValueError as error:
        raise NativeCorpusProtocolError(f"response has unknown {name} {value}") from error


def unpack_native_move(value: int) -> str:
    _require_integer("packed native move", value, 0, UINT16_MAX)
    from_square = value & 0x3F
    to_square = (value >> 6) & 0x3F
    promotion_code = (value >> 12) & 0x0F
    promotions = {0: "", 2: "n", 3: "b", 4: "r", 5: "q"}
    if promotion_code not in promotions:
        raise NativeCorpusProtocolError(
            f"response has noncanonical promotion code {promotion_code}"
        )
    if from_square == to_square:
        raise NativeCorpusProtocolError("response has a zero-length packed move")
    return (
        chess.square_name(from_square)
        + chess.square_name(to_square)
        + promotions[promotion_code]
    )


@dataclass(frozen=True, slots=True)
class NativeFullGameRecord:
    attempt_index: int
    terminal: NativeTerminal
    reject: NativeReject
    white_profile_index: int
    black_profile_index: int
    logical_work: int
    path_count_saturations: int
    series: tuple[tuple[str, ...], ...]

    @property
    def accepted(self) -> bool:
        return self.terminal is not NativeTerminal.NONE and self.reject is NativeReject.NONE

    @property
    def move_count(self) -> int:
        return sum(len(moves) for moves in self.series)


@dataclass(frozen=True, slots=True)
class NativeFullGameBatch:
    first_attempt: int
    attempt_count: int
    semantic_config_digest: bytes
    profile_count: int
    policy_kind: NativePolicyKind
    schedule: NativeProfileSchedule
    total_saturations: int
    records: tuple[NativeFullGameRecord, ...]
    payload_size: int

    @property
    def accepted_count(self) -> int:
        return sum(record.accepted for record in self.records)

    @property
    def rejected_count(self) -> int:
        return self.attempt_count - self.accepted_count

    @property
    def logical_work(self) -> int:
        return sum(record.logical_work for record in self.records)


def _expected_profile_pair(
    attempt_index: int,
    profile_count: int,
    schedule: NativeProfileSchedule,
) -> tuple[int, int]:
    seat = attempt_index % profile_count
    if schedule is NativeProfileSchedule.SELF_ROUND_ROBIN:
        return seat, seat
    round_index = (attempt_index // profile_count) % profile_count
    return seat, (seat + round_index) % profile_count


def decode_full_game_v2_response(
    payload: bytes | bytearray | memoryview,
    *,
    config: NativeCorpusConfig,
    profiles: Sequence[EngineProfile | NativeCorpusProfile],
    first_attempt: int,
    attempt_count: int,
) -> NativeFullGameBatch:
    """Decode and fully bind one SPCFGB02 response to its request semantics."""

    if not isinstance(config, NativeCorpusConfig):
        raise TypeError("config must be a NativeCorpusConfig")
    _validate_attempt_range(first_attempt, attempt_count)
    bound = bind_native_profiles(profiles)
    expected_digest = semantic_config_digest(config, bound)
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("native corpus response must be bytes-like")
    data = bytes(payload)
    if len(data) < FULL_GAME_V2_RESPONSE_HEADER_SIZE:
        raise NativeCorpusProtocolError("truncated native full-game v2 response")
    (
        magic,
        version,
        header_size,
        record_header_size,
        response_flags,
        echoed_first_attempt,
        echoed_attempt_count,
        echoed_digest,
        profile_count,
        policy_kind_value,
        schedule_value,
        total_saturations,
    ) = _RESPONSE_HEADER.unpack_from(data)
    if magic != FULL_GAME_V2_RESPONSE_MAGIC:
        raise NativeCorpusProtocolError("native full-game v2 response magic is invalid")
    if version != FULL_GAME_V2_VERSION:
        raise NativeCorpusProtocolError("native full-game v2 response version is invalid")
    if header_size != FULL_GAME_V2_RESPONSE_HEADER_SIZE:
        raise NativeCorpusProtocolError("native full-game v2 response header size drifted")
    if record_header_size != FULL_GAME_V2_RECORD_HEADER_SIZE:
        raise NativeCorpusProtocolError("native full-game v2 record header size drifted")
    if response_flags not in (0, 1):
        raise NativeCorpusProtocolError("native full-game v2 response flags are invalid")
    if echoed_first_attempt != first_attempt or echoed_attempt_count != attempt_count:
        raise NativeCorpusProtocolError("native full-game v2 response attempt range drifted")
    if echoed_digest != expected_digest:
        raise NativeCorpusProtocolError("native full-game v2 semantic digest drifted")
    if profile_count != len(bound):
        raise NativeCorpusProtocolError("native full-game v2 profile count drifted")
    policy_kind = _unpack_enum(
        NativePolicyKind, policy_kind_value, "policy kind"
    )
    schedule = _unpack_enum(
        NativeProfileSchedule, schedule_value, "profile schedule"
    )
    if policy_kind is not config.policy.kind or schedule is not config.schedule:
        raise NativeCorpusProtocolError("native full-game v2 policy binding drifted")

    records: list[NativeFullGameRecord] = []
    offset = FULL_GAME_V2_RESPONSE_HEADER_SIZE
    saturation_sum = 0
    for record_offset in range(attempt_count):
        if len(data) - offset < FULL_GAME_V2_RECORD_HEADER_SIZE:
            raise NativeCorpusProtocolError("truncated native full-game v2 record header")
        (
            record_size,
            attempt_index,
            status,
            terminal_value,
            reject_value,
            reserved,
            record_flags,
            white_profile_index,
            black_profile_index,
            series_count,
            move_count,
            logical_work,
            path_count_saturations,
        ) = _RESPONSE_RECORD.unpack_from(data, offset)
        expected_attempt = first_attempt + record_offset
        if attempt_index != expected_attempt:
            raise NativeCorpusProtocolError("native full-game v2 record order drifted")
        if status not in (0, 1) or reserved != 0 or record_flags not in (0, 1):
            raise NativeCorpusProtocolError("native full-game v2 record flags are invalid")
        if record_flags != int(path_count_saturations != 0):
            raise NativeCorpusProtocolError("native full-game v2 saturation flag drifted")
        if white_profile_index >= len(bound) or black_profile_index >= len(bound):
            raise NativeCorpusProtocolError("native full-game v2 profile index is invalid")
        expected_pair = _expected_profile_pair(attempt_index, len(bound), schedule)
        if (white_profile_index, black_profile_index) != expected_pair:
            raise NativeCorpusProtocolError("native full-game v2 profile schedule drifted")
        expected_record_size = (
            FULL_GAME_V2_RECORD_HEADER_SIZE + series_count * 8 + move_count * 2
        )
        if record_size != expected_record_size or record_size > len(data) - offset:
            raise NativeCorpusProtocolError("native full-game v2 record size is invalid")

        terminal = _unpack_enum(NativeTerminal, terminal_value, "terminal")
        reject = _unpack_enum(NativeReject, reject_value, "reject reason")
        cursor = offset + FULL_GAME_V2_RECORD_HEADER_SIZE
        ends: list[int] = []
        for _ in range(series_count):
            (end,) = struct.unpack_from("<Q", data, cursor)
            ends.append(end)
            cursor += 8
        packed_moves: list[int] = []
        for _ in range(move_count):
            (move,) = struct.unpack_from("<H", data, cursor)
            packed_moves.append(move)
            cursor += 2
        if cursor != offset + record_size:
            raise NativeCorpusProtocolError("native full-game v2 record cursor drifted")

        accepted = status == 0
        if accepted:
            if (
                terminal is NativeTerminal.NONE
                or reject is not NativeReject.NONE
                or not ends
                or not packed_moves
            ):
                raise NativeCorpusProtocolError("accepted native full-game record is incomplete")
            prior = 0
            series: list[tuple[str, ...]] = []
            for end in ends:
                if end <= prior or end > len(packed_moves):
                    raise NativeCorpusProtocolError(
                        "native full-game v2 series offsets are invalid"
                    )
                series.append(
                    tuple(unpack_native_move(value) for value in packed_moves[prior:end])
                )
                prior = end
            if prior != len(packed_moves):
                raise NativeCorpusProtocolError(
                    "native full-game v2 final series offset is invalid"
                )
        else:
            if (
                terminal is not NativeTerminal.NONE
                or reject is NativeReject.NONE
                or ends
                or packed_moves
            ):
                raise NativeCorpusProtocolError("rejected native full-game record has a trace")
            if reject is NativeReject.COORDINATOR_CANCELLED:
                raise NativeCorpusProtocolError(
                    "native kernel emitted a coordinator-only cancellation record"
                )
            series = []
        saturation_sum = min(
            UINT64_MAX, saturation_sum + path_count_saturations
        )
        records.append(
            NativeFullGameRecord(
                attempt_index=attempt_index,
                terminal=terminal,  # type: ignore[arg-type]
                reject=reject,  # type: ignore[arg-type]
                white_profile_index=white_profile_index,
                black_profile_index=black_profile_index,
                logical_work=logical_work,
                path_count_saturations=path_count_saturations,
                series=tuple(series),
            )
        )
        offset += record_size
    if offset != len(data):
        raise NativeCorpusProtocolError("native full-game v2 response has trailing bytes")
    if total_saturations != saturation_sum:
        raise NativeCorpusProtocolError("native full-game v2 saturation total drifted")
    if response_flags != int(total_saturations != 0):
        raise NativeCorpusProtocolError("native full-game v2 response saturation flag drifted")
    return NativeFullGameBatch(
        first_attempt=first_attempt,
        attempt_count=attempt_count,
        semantic_config_digest=expected_digest,
        profile_count=len(bound),
        policy_kind=policy_kind,  # type: ignore[arg-type]
        schedule=schedule,  # type: ignore[arg-type]
        total_saturations=total_saturations,
        records=tuple(records),
        payload_size=len(data),
    )


def generate_native_full_game_batch(
    config: NativeCorpusConfig,
    profiles: Sequence[EngineProfile | NativeCorpusProfile],
    *,
    first_attempt: int,
    attempt_count: int,
) -> NativeFullGameBatch:
    """Generate one source-bound batch without silently falling back to Python."""

    validate_current_native_generation_config(config)
    request = encode_full_game_v2_request(
        config,
        profiles,
        first_attempt=first_attempt,
        attempt_count=attempt_count,
    )
    native = evaluation._native_eval
    generate = (
        None
        if native is None
        else getattr(native, "generate_full_game_batch_v2", None)
    )
    if not callable(generate):
        raise NativeCorpusUnavailable(
            "the exact source-matched native full-game v2 kernel is unavailable"
        )
    try:
        response = generate(request)
    except (MemoryError, OverflowError, RuntimeError, ValueError) as error:
        raise NativeCorpusError(f"native full-game v2 generation failed: {error}") from error
    return decode_full_game_v2_response(
        response,
        config=config,
        profiles=profiles,
        first_attempt=first_attempt,
        attempt_count=attempt_count,
    )


def validate_current_native_generation_config(config: NativeCorpusConfig) -> None:
    """Reject forged provenance before production native generation begins."""

    if not isinstance(config, NativeCorpusConfig):
        raise TypeError("config must be a NativeCorpusConfig")
    expected = (
        ("engine_version", ENGINE_VERSION),
        ("engine_source_fingerprint", ENGINE_SOURCE_FINGERPRINT),
        ("ruleset_version", RULESET_VERSION),
    )
    mismatches = tuple(
        f"{name}={getattr(config, name)!r} (current {value!r})"
        for name, value in expected
        if getattr(config, name) != value
    )
    if mismatches:
        raise NativeCorpusIdentityError(
            "native corpus generation identity does not match the current runtime: "
            + "; ".join(mismatches)
        )


@dataclass(frozen=True, slots=True)
class ReplayedNativeGame:
    record: NativeFullGameRecord
    states: tuple[ProgressiveState, ...]
    results: tuple[SeriesResult, ...]
    outcome: Outcome
    winner: chess.Color | None

    @property
    def boundary_count(self) -> int:
        return len(self.states)


def replay_native_full_game(record: NativeFullGameRecord) -> ReplayedNativeGame:
    """Replay an accepted trace through the authoritative progressive rules."""

    if not isinstance(record, NativeFullGameRecord):
        raise TypeError("record must be a NativeFullGameRecord")
    if not record.accepted:
        raise NativeCorpusReplayError("cannot replay a rejected native full-game record")
    state = ProgressiveState.initial()
    states = [state]
    results: list[SeriesResult] = []
    for index, moves in enumerate(record.series):
        mover = state.board.turn
        try:
            result = play_series(state, moves)
        except SeriesLegalityError as error:
            raise NativeCorpusReplayError(
                f"attempt {record.attempt_index} series {index + 1} is illegal: {error}"
            ) from error
        is_final = index == len(record.series) - 1
        if not is_final and result.outcome is not None:
            raise NativeCorpusReplayError(
                f"attempt {record.attempt_index} continued after a terminal series"
            )
        if is_final and result.outcome is None:
            raise NativeCorpusReplayError(
                f"attempt {record.attempt_index} trace ended without a terminal result"
            )
        results.append(result)
        state = result.final_state
        states.append(state)

    final_result = results[-1]
    outcome = final_result.outcome
    if outcome is None:
        raise NativeCorpusReplayError("native trace has no authoritative terminal outcome")
    winner: chess.Color | None = None
    if outcome is Outcome.CHECKMATE:
        final_mover = states[-2].board.turn
        winner = final_mover if final_result.ended_by_check else not final_mover
        expected_terminal = (
            NativeTerminal.CHECKMATE_WHITE
            if winner == chess.WHITE
            else NativeTerminal.CHECKMATE_BLACK
        )
    elif outcome is Outcome.STALEMATE:
        expected_terminal = NativeTerminal.STALEMATE
    elif outcome is Outcome.TEN_SERIES_DRAW:
        expected_terminal = NativeTerminal.TEN_SERIES_DRAW
    else:  # pragma: no cover - exhaustive for the versioned rules model.
        raise NativeCorpusReplayError(f"unsupported authoritative outcome {outcome}")
    if record.terminal is not expected_terminal:
        raise NativeCorpusReplayError(
            f"attempt {record.attempt_index} terminal disagrees with authoritative replay"
        )
    return ReplayedNativeGame(
        record=record,
        states=tuple(states),
        results=tuple(results),
        outcome=outcome,
        winner=winner,
    )


def replay_native_batch(
    batch: NativeFullGameBatch,
) -> tuple[ReplayedNativeGame, ...]:
    if not isinstance(batch, NativeFullGameBatch):
        raise TypeError("batch must be a NativeFullGameBatch")
    return tuple(
        replay_native_full_game(record)
        for record in batch.records
        if record.accepted
    )


def iter_unique_replayed_boundaries(
    games: Iterable[ReplayedNativeGame],
) -> Iterable[ProgressiveState]:
    """Yield first-seen full rule states, including promotion and e.p. provenance."""

    # The ordinary model transposition key intentionally omits promoted-piece
    # provenance and Chess960 mode because the current search product does not
    # enable Chess960. A durable training corpus cannot make that assumption:
    # its state identity must remain complete across future consumers.
    from .corpus_shards import progressive_state_dedup_key

    seen: set[bytes] = set()
    for game in games:
        for state in game.states:
            key = progressive_state_dedup_key(state)
            if key in seen:
                continue
            seen.add(key)
            yield state
