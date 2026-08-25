from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import hashlib
import json
import re
import struct
from typing import Any, Iterable, Mapping
import zlib

import chess

from .model import Outcome, ProgressiveState, RULESET_VERSION
from .rules import SeriesLegalityError, play_series


NATIVE_BATCH_MAGIC = b"SPCFGB01"
NATIVE_BATCH_VERSION = 1
NATIVE_BATCH_HEADER = struct.Struct("<8sHHIQQ")
NATIVE_RECORD_PREFIX = struct.Struct("<QQBBBBQQQ")

NATIVE_V2_REQUEST_MAGIC = b"SPCFGR02"
NATIVE_V2_RESPONSE_MAGIC = b"SPCFGB02"
NATIVE_V2_VERSION = 2
NATIVE_V2_REQUEST_HEADER = struct.Struct("<8sHHI8QIIHHI6H32sI")
NATIVE_V2_PROFILE = struct.Struct("<32s5q")
NATIVE_V2_RESPONSE_HEADER = struct.Struct("<8sHHHHQQ32sIHHQ")
NATIVE_V2_RECORD_PREFIX = struct.Struct("<QQBBBBIIIQQQQ")
NATIVE_V2_MAX_PROFILES = 4096
NATIVE_V2_POLICY_UNIFORM = 1
NATIVE_V2_POLICY_RANK_MIXTURE = 2
NATIVE_V2_SCHEDULE_SELF_ROUND_ROBIN = 1
NATIVE_V2_SCHEDULE_ORDERED_PAIR_ROUND_ROBIN = 2
NATIVE_V2_POLICY_PRESERVE_MATE = 1
NATIVE_V2_PROFILE_DIGEST_DOMAIN = b"SPC-FAST-WEIGHTS-V1\0"
NATIVE_V2_CONFIG_DIGEST_DOMAIN = b"SPC-FULLGAME-CONFIG-V2\0"
NATIVE_V2_RANGE_OFFSET = 24
NATIVE_V2_RANGE_END = 40
NATIVE_V2_CONFIG_DIGEST_OFFSET = 108
NATIVE_V2_CONFIG_DIGEST_END = 140

CHUNK_MAGIC = b"SPCFGC02"
CHUNK_SCHEMA = "spc-fullgame-chunk-v2"
CHUNK_HEADER_PREFIX = struct.Struct("<8sI")
CHUNK_FRAME_PREFIX = struct.Struct("<I")
CHUNK_FRAME_CRC = struct.Struct("<I")
FINAL_RECORD_PREFIX = struct.Struct("<BQQIIQ")

MAX_U32 = (1 << 32) - 1
MAX_U64 = (1 << 64) - 1
SIMULATION_ID_PATTERN = re.compile(r"spc-fullgame-[0-9a-f]{64}\Z")


class Terminal(IntEnum):
    NONE = 0
    CHECKMATE_WHITE = 1
    CHECKMATE_BLACK = 2
    STALEMATE = 3
    TEN_SERIES_DRAW = 4


class RejectReason(IntEnum):
    NONE = 0
    MANUAL_PROOF_REQUIRED = 1
    WORK_LIMIT = 2
    CANCELLED = 3
    OVERFLOW = 4
    TECHNICAL_SERIES_WATCHDOG = 5
    INTERNAL_ERROR = 6
    DUPLICATE_TRACE = 7


@dataclass(frozen=True, slots=True)
class FullGameRecord:
    attempt_index: int
    terminal: Terminal
    series: tuple[tuple[str, ...], ...]
    logical_work: int = 0
    white_profile_index: int = 0
    black_profile_index: int = 0
    path_count_saturations: int = 0

    def __post_init__(self) -> None:
        if type(self.attempt_index) is not int:
            raise ValueError("attempt_index must be an exact integer")
        if type(self.terminal) is not Terminal:
            raise ValueError("terminal must be an exact Terminal enum")
        if not 0 <= self.attempt_index <= MAX_U64:
            raise ValueError("attempt_index must fit uint64")
        if type(self.logical_work) is not int or not 0 <= self.logical_work <= MAX_U64:
            raise ValueError("logical_work must fit uint64")
        if any(
            type(value) is not int or not 0 <= value <= MAX_U32
            for value in (self.white_profile_index, self.black_profile_index)
        ):
            raise ValueError("profile indexes must fit uint32")
        if (
            type(self.path_count_saturations) is not int
            or not 0 <= self.path_count_saturations <= MAX_U64
        ):
            raise ValueError("path_count_saturations must fit uint64")
        if self.terminal == Terminal.NONE:
            raise ValueError("an accepted game requires an authoritative terminal")
        if type(self.series) is not tuple or not self.series:
            raise ValueError("an accepted game must contain at least one series")
        if len(self.series) > MAX_U32:
            raise ValueError("series count exceeds the chunk codec")
        for moves in self.series:
            if type(moves) is not tuple or not moves:
                raise ValueError("stored series cannot be empty")
            if len(moves) > MAX_U32:
                raise ValueError("move count exceeds the chunk codec")
            for move in moves:
                if type(move) is not str:
                    raise ValueError("stored moves must be exact UCI strings")
                pack_move(move)

    @property
    def move_count(self) -> int:
        return sum(len(moves) for moves in self.series)

    @property
    def result(self) -> str:
        if self.terminal == Terminal.CHECKMATE_WHITE:
            return "1-0"
        if self.terminal == Terminal.CHECKMATE_BLACK:
            return "0-1"
        return "1/2-1/2"


@dataclass(frozen=True, slots=True)
class RejectedAttempt:
    attempt_index: int
    reason: RejectReason
    logical_work: int = 0
    white_profile_index: int = 0
    black_profile_index: int = 0
    path_count_saturations: int = 0

    def __post_init__(self) -> None:
        if type(self.attempt_index) is not int:
            raise ValueError("attempt_index must be an exact integer")
        if type(self.reason) is not RejectReason:
            raise ValueError("reason must be an exact RejectReason enum")
        if not 0 <= self.attempt_index <= MAX_U64:
            raise ValueError("attempt_index must fit uint64")
        if type(self.logical_work) is not int or not 0 <= self.logical_work <= MAX_U64:
            raise ValueError("logical_work must fit uint64")
        if any(
            type(value) is not int or not 0 <= value <= MAX_U32
            for value in (self.white_profile_index, self.black_profile_index)
        ):
            raise ValueError("profile indexes must fit uint32")
        if (
            type(self.path_count_saturations) is not int
            or not 0 <= self.path_count_saturations <= MAX_U64
        ):
            raise ValueError("path_count_saturations must fit uint64")
        if self.reason in {
            RejectReason.NONE,
            RejectReason.CANCELLED,
            RejectReason.DUPLICATE_TRACE,
        }:
            raise ValueError("native rejection requires a native reject reason")


@dataclass(frozen=True, slots=True)
class NativeBatch:
    first_attempt: int
    attempt_count: int
    accepted: tuple[FullGameRecord, ...]
    rejected: tuple[RejectedAttempt, ...]

    @property
    def records(self) -> tuple[FullGameRecord | RejectedAttempt, ...]:
        return tuple(
            sorted(
                (*self.accepted, *self.rejected),
                key=lambda item: item.attempt_index,
            )
        )


@dataclass(frozen=True, slots=True)
class NativeV2Profile:
    digest: bytes
    material: int
    king_space: int
    promotion_corridors: int
    immediate_vulnerability: int
    boundary_check: int

    def __post_init__(self) -> None:
        if type(self.digest) is not bytes or len(self.digest) != 32 or not any(self.digest):
            raise ValueError("native v2 profile digest must be 32 nonzero bytes")
        weights = self.weights
        if any(type(value) is not int or not 25 <= value <= 300 for value in weights):
            raise ValueError("native v2 profile weights must be exact integers from 25 to 300")

    @property
    def weights(self) -> tuple[int, int, int, int, int]:
        return (
            self.material,
            self.king_space,
            self.promotion_corridors,
            self.immediate_vulnerability,
            self.boundary_check,
        )


@dataclass(frozen=True, slots=True)
class NativeBatchV2:
    first_attempt: int
    attempt_count: int
    config_digest: bytes
    profile_count: int
    policy_kind: int
    schedule_kind: int
    total_path_count_saturations: int
    records: tuple[FullGameRecord | RejectedAttempt, ...]


def native_v2_profile_digest(
    weights: tuple[int, int, int, int, int],
) -> bytes:
    if type(weights) is not tuple or len(weights) != 5 or any(
        type(value) is not int or not 25 <= value <= 300 for value in weights
    ):
        raise ValueError("native v2 profile digest weights are invalid")
    return hashlib.sha256(
        NATIVE_V2_PROFILE_DIGEST_DOMAIN + struct.pack("<5q", *weights)
    ).digest()


def native_v2_semantic_digest(request: bytes) -> bytes:
    """Recompute the frozen range-independent digest of a canonical request."""

    if type(request) is not bytes or len(request) < NATIVE_V2_REQUEST_HEADER.size:
        raise ValueError("native v2 semantic digest requires a complete bytes request")
    fields = NATIVE_V2_REQUEST_HEADER.unpack_from(request)
    if (
        fields[0] != NATIVE_V2_REQUEST_MAGIC
        or fields[1] != NATIVE_V2_VERSION
        or fields[2] != NATIVE_V2_REQUEST_HEADER.size
        or fields[3] != 0
        or fields[4] != len(request)
        or len(request)
        != NATIVE_V2_REQUEST_HEADER.size + fields[13] * NATIVE_V2_PROFILE.size
    ):
        raise ValueError("native v2 request envelope is not canonical")
    canonical = bytearray(request)
    canonical[NATIVE_V2_RANGE_OFFSET:NATIVE_V2_RANGE_END] = bytes(
        NATIVE_V2_RANGE_END - NATIVE_V2_RANGE_OFFSET
    )
    canonical[
        NATIVE_V2_CONFIG_DIGEST_OFFSET:NATIVE_V2_CONFIG_DIGEST_END
    ] = bytes(NATIVE_V2_CONFIG_DIGEST_END - NATIVE_V2_CONFIG_DIGEST_OFFSET)
    return hashlib.sha256(NATIVE_V2_CONFIG_DIGEST_DOMAIN + canonical).digest()


@dataclass(frozen=True, slots=True)
class ReplayEvidence:
    result: str
    terminal_reason: str
    final_pfen: str
    final_position_hash: str
    series_played: int
    micro_moves_played: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "terminal_reason": self.terminal_reason,
            "final_pfen": self.final_pfen,
            "final_position_hash": self.final_position_hash,
            "series_played": self.series_played,
            "micro_moves_played": self.micro_moves_played,
        }


@dataclass(frozen=True, slots=True)
class DecodedChunk:
    header: Mapping[str, Any]
    records: tuple[FullGameRecord, ...]


def pack_move(uci: str) -> int:
    try:
        move = chess.Move.from_uci(uci)
    except ValueError as error:
        raise ValueError(f"invalid UCI move {uci!r}") from error
    if move.drop is not None or move == chess.Move.null():
        raise ValueError("drop and null moves are not supported by the full-game codec")
    promotion = move.promotion or 0
    if promotion not in {0, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN}:
        raise ValueError(f"unsupported promotion in {uci!r}")
    return move.from_square | (move.to_square << 6) | (promotion << 12)


def unpack_move(word: int) -> str:
    if not 0 <= word <= 0xFFFF:
        raise ValueError("packed move must fit uint16")
    if word & 0x8000:
        raise ValueError("packed move reserved bit must be zero")
    from_square = word & 0x3F
    to_square = (word >> 6) & 0x3F
    promotion = (word >> 12) & 0x7
    if promotion not in {0, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN}:
        raise ValueError("packed move promotion code is invalid")
    if from_square == to_square:
        raise ValueError("packed move cannot be null")
    return chess.Move(
        from_square,
        to_square,
        promotion=promotion or None,
    ).uci()


def _encode_varuint(value: int) -> bytes:
    if not 0 <= value <= MAX_U64:
        raise ValueError("varuint value must fit uint64")
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _decode_varuint(payload: memoryview, offset: int) -> tuple[int, int]:
    start = offset
    value = 0
    shift = 0
    for _ in range(10):
        if offset >= len(payload):
            raise ValueError("truncated varuint")
        byte = int(payload[offset])
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if value > MAX_U64:
                raise ValueError("varuint exceeds uint64")
            if offset - start != len(_encode_varuint(value)):
                raise ValueError("varuint is not minimally encoded")
            return value, offset
        shift += 7
    raise ValueError("overlong varuint")


def encode_record(record: FullGameRecord) -> bytes:
    payload = bytearray(
        FINAL_RECORD_PREFIX.pack(
            int(record.terminal),
            record.attempt_index,
            record.logical_work,
            record.white_profile_index,
            record.black_profile_index,
            record.path_count_saturations,
        )
    )
    payload.extend(_encode_varuint(len(record.series)))
    for moves in record.series:
        payload.extend(_encode_varuint(len(moves)))
        for move in moves:
            payload.extend(struct.pack("<H", pack_move(move)))
    return bytes(payload)


def decode_record(payload: bytes | bytearray | memoryview) -> FullGameRecord:
    view = memoryview(payload)
    if len(view) < FINAL_RECORD_PREFIX.size:
        raise ValueError("truncated full-game record")
    (
        terminal_raw,
        attempt_index,
        logical_work,
        white_profile_index,
        black_profile_index,
        path_count_saturations,
    ) = FINAL_RECORD_PREFIX.unpack_from(view)
    try:
        terminal = Terminal(terminal_raw)
    except ValueError as error:
        raise ValueError("full-game terminal code is invalid") from error
    if terminal == Terminal.NONE:
        raise ValueError("accepted full-game record has no terminal")
    offset = FINAL_RECORD_PREFIX.size
    series_count, offset = _decode_varuint(view, offset)
    if not series_count or series_count > MAX_U32:
        raise ValueError("full-game series count is invalid")
    series: list[tuple[str, ...]] = []
    for _ in range(series_count):
        move_count, offset = _decode_varuint(view, offset)
        if not move_count or move_count > MAX_U32:
            raise ValueError("full-game move count is invalid")
        byte_count = move_count * 2
        if offset + byte_count > len(view):
            raise ValueError("truncated packed moves")
        moves = tuple(
            unpack_move(struct.unpack_from("<H", view, offset + index * 2)[0])
            for index in range(move_count)
        )
        offset += byte_count
        series.append(moves)
    if offset != len(view):
        raise ValueError("full-game record has trailing bytes")
    return FullGameRecord(
        attempt_index,
        terminal,
        tuple(series),
        logical_work,
        white_profile_index,
        black_profile_index,
        path_count_saturations,
    )


def trace_identity_bytes(record: FullGameRecord) -> bytes:
    payload = bytearray(RULESET_VERSION.encode("ascii"))
    payload.append(0)
    payload.append(int(record.terminal))
    payload.extend(_encode_varuint(len(record.series)))
    for moves in record.series:
        payload.extend(_encode_varuint(len(moves)))
        for move in moves:
            payload.extend(struct.pack("<H", pack_move(move)))
    return bytes(payload)


def trace_sha256(record: FullGameRecord) -> str:
    return hashlib.sha256(trace_identity_bytes(record)).hexdigest()


def replay_record(record: FullGameRecord) -> ReplayEvidence:
    state = ProgressiveState.initial()
    last_outcome: Outcome | None = None
    for series_index, moves in enumerate(record.series):
        if state.series_number != series_index + 1:
            raise ValueError("full-game series numbering does not start at 1")
        mover = state.board.turn
        try:
            result = play_series(state, moves)
        except SeriesLegalityError as error:
            raise ValueError(
                f"illegal series {state.series_number}: {error}"
            ) from error
        if series_index + 1 < len(record.series) and result.outcome is not None:
            raise ValueError("full-game trace continues after an authoritative terminal")
        last_outcome = result.outcome
        state = result.final_state

    if last_outcome is None:
        raise ValueError("full-game trace ends without an authoritative terminal")
    if record.terminal in {Terminal.CHECKMATE_WHITE, Terminal.CHECKMATE_BLACK}:
        if last_outcome != Outcome.CHECKMATE:
            raise ValueError("record is labeled checkmate but replay is not")
        expected_winner = (
            chess.WHITE
            if record.terminal == Terminal.CHECKMATE_WHITE
            else chess.BLACK
        )
        # A checking mover wins; a side already unable to answer check loses.
        actual_winner = mover if result.ended_by_check else not mover
        if actual_winner != expected_winner:
            raise ValueError("record checkmate winner does not match replay")
        terminal_reason = Outcome.CHECKMATE.value
    elif record.terminal == Terminal.STALEMATE:
        if last_outcome != Outcome.STALEMATE:
            raise ValueError("record is labeled stalemate but replay is not")
        terminal_reason = Outcome.STALEMATE.value
    elif record.terminal == Terminal.TEN_SERIES_DRAW:
        if last_outcome != Outcome.TEN_SERIES_DRAW:
            raise ValueError("record is labeled ten-series draw but replay is not")
        terminal_reason = Outcome.TEN_SERIES_DRAW.value
    else:  # pragma: no cover - guarded by enum validation
        raise ValueError("unsupported terminal")

    return ReplayEvidence(
        result=record.result,
        terminal_reason=terminal_reason,
        final_pfen=state.pfen,
        final_position_hash=state.position_hash,
        series_played=len(record.series),
        micro_moves_played=record.move_count,
    )


def decode_native_batch(payload: bytes | bytearray | memoryview) -> NativeBatch:
    view = memoryview(payload)
    if len(view) < NATIVE_BATCH_HEADER.size:
        raise ValueError("truncated native full-game batch")
    (
        magic,
        version,
        header_size,
        record_count,
        first_attempt,
        attempt_count,
    ) = NATIVE_BATCH_HEADER.unpack_from(view)
    if magic != NATIVE_BATCH_MAGIC:
        raise ValueError("native full-game batch magic is invalid")
    if version != NATIVE_BATCH_VERSION or header_size != NATIVE_BATCH_HEADER.size:
        raise ValueError("native full-game batch version/header is unsupported")
    if record_count != attempt_count:
        raise ValueError("native batch must contain exactly one record per attempt")
    if not 1 <= attempt_count <= MAX_U32:
        raise ValueError("native batch attempt_count must fit its positive counter")
    if first_attempt + attempt_count > MAX_U64 + 1:
        raise ValueError("native batch attempt range overflows uint64")

    accepted: list[FullGameRecord] = []
    rejected: list[RejectedAttempt] = []
    offset = header_size
    expected_attempt = first_attempt
    for _ in range(record_count):
        if offset + NATIVE_RECORD_PREFIX.size > len(view):
            raise ValueError("truncated native full-game record")
        (
            record_size,
            attempt_index,
            status,
            terminal_raw,
            reject_raw,
            reserved,
            series_count,
            move_count,
            logical_work,
        ) = NATIVE_RECORD_PREFIX.unpack_from(view, offset)
        if record_size < NATIVE_RECORD_PREFIX.size:
            raise ValueError("native record size is invalid")
        record_end = offset + record_size
        if record_end > len(view):
            raise ValueError("truncated native full-game record body")
        if attempt_index != expected_attempt:
            raise ValueError("native attempts are missing, duplicated, or out of order")
        expected_attempt += 1
        if reserved != 0:
            raise ValueError("native record reserved byte must be zero")

        body_offset = offset + NATIVE_RECORD_PREFIX.size
        if series_count > MAX_U32 or move_count > MAX_U32:
            raise ValueError("native record counters exceed the v1 Python codec")
        offsets_bytes = series_count * 8
        moves_bytes = move_count * 2
        expected_size = NATIVE_RECORD_PREFIX.size + offsets_bytes + moves_bytes
        if record_size != expected_size:
            raise ValueError("native record counts do not match its byte size")
        cumulative: list[int] = []
        for index in range(series_count):
            cumulative.append(
                struct.unpack_from("<Q", view, body_offset + index * 8)[0]
            )
        body_offset += offsets_bytes
        if cumulative and cumulative[-1] != move_count:
            raise ValueError("native series offsets do not cover every move")
        if any(
            end <= (cumulative[index - 1] if index else 0)
            for index, end in enumerate(cumulative)
        ):
            raise ValueError("native series offsets must be strictly increasing")

        if status == 0:
            if reject_raw != 0:
                raise ValueError("accepted native record carries a reject reason")
            try:
                terminal = Terminal(terminal_raw)
            except ValueError as error:
                raise ValueError("native terminal code is invalid") from error
            if terminal == Terminal.NONE or not series_count:
                raise ValueError("accepted native record is not a full terminal game")
            flat_moves = tuple(
                unpack_move(
                    struct.unpack_from("<H", view, body_offset + index * 2)[0]
                )
                for index in range(move_count)
            )
            series: list[tuple[str, ...]] = []
            begin = 0
            for end in cumulative:
                series.append(flat_moves[begin:end])
                begin = end
            accepted.append(
                FullGameRecord(attempt_index, terminal, tuple(series), logical_work)
            )
        elif status == 1:
            if terminal_raw != 0 or series_count != 0 or move_count != 0:
                raise ValueError("rejected native record carries WDL or trace data")
            try:
                reason = RejectReason(reject_raw)
            except ValueError as error:
                raise ValueError("native reject code is invalid") from error
            if reason in {
                RejectReason.NONE,
                RejectReason.CANCELLED,
                RejectReason.DUPLICATE_TRACE,
            }:
                raise ValueError("native rejected record has no native reject reason")
            rejected.append(RejectedAttempt(attempt_index, reason, logical_work))
        else:
            raise ValueError("native record status is invalid")
        offset = record_end

    if offset != len(view):
        raise ValueError("native full-game batch has trailing bytes")
    return NativeBatch(
        first_attempt,
        attempt_count,
        tuple(accepted),
        tuple(rejected),
    )


def _require_exact_uint(value: object, bits: int, name: str) -> int:
    limit = (1 << bits) - 1
    if type(value) is not int or not 0 <= value <= limit:
        raise ValueError(f"{name} must fit uint{bits}")
    return value


def expected_v2_profile_pair(
    attempt_index: int,
    profile_count: int,
    schedule_kind: int,
) -> tuple[int, int]:
    _require_exact_uint(attempt_index, 64, "attempt_index")
    if type(profile_count) is not int or not 1 <= profile_count <= NATIVE_V2_MAX_PROFILES:
        raise ValueError("profile_count is invalid")
    if schedule_kind == NATIVE_V2_SCHEDULE_SELF_ROUND_ROBIN:
        index = attempt_index % profile_count
        return index, index
    if schedule_kind == NATIVE_V2_SCHEDULE_ORDERED_PAIR_ROUND_ROBIN:
        white = attempt_index % profile_count
        round_index = (attempt_index // profile_count) % profile_count
        return white, (white + round_index) % profile_count
    raise ValueError("native v2 schedule kind is invalid")


def encode_native_v2_request(
    *,
    first_attempt: int,
    attempt_count: int,
    seed: int,
    max_attempt_series: int,
    max_frontier_states: int,
    max_positions_per_series: int,
    max_positions_per_game: int,
    candidate_count: int,
    profiles: Iterable[NativeV2Profile],
    policy_kind: int,
    schedule_kind: int,
    config_digest: bytes | None = None,
    preserve_returned_mate: bool = True,
    rank_mixture: tuple[int, int, int, int, int] = (0, 0, 0, 0, 0),
) -> bytes:
    """Build one canonical v2 request.

    Digest bytes are binding tags, not authentication. The production wrapper
    must derive them from the exact semantic configuration and profile data.
    This builder deliberately refuses the unsafe no-mate-preservation mode.
    """

    first_attempt = _require_exact_uint(first_attempt, 64, "first_attempt")
    attempt_count = _require_exact_uint(attempt_count, 32, "attempt_count")
    if attempt_count == 0:
        raise ValueError("attempt_count must be positive")
    if first_attempt + attempt_count > MAX_U64 + 1:
        raise ValueError("native v2 attempt range overflows uint64")
    seed = _require_exact_uint(seed, 64, "seed")
    max_attempt_series = _require_exact_uint(
        max_attempt_series, 64, "max_attempt_series"
    )
    max_frontier_states = _require_exact_uint(
        max_frontier_states, 64, "max_frontier_states"
    )
    max_positions_per_series = _require_exact_uint(
        max_positions_per_series, 64, "max_positions_per_series"
    )
    max_positions_per_game = _require_exact_uint(
        max_positions_per_game, 64, "max_positions_per_game"
    )
    candidate_count = _require_exact_uint(candidate_count, 32, "candidate_count")
    if (
        max_frontier_states == 0
        or max_positions_per_series == 0
        or max_positions_per_game == 0
        or candidate_count == 0
        or candidate_count > max_frontier_states
    ):
        raise ValueError("native v2 work and candidate bounds are invalid")
    if config_digest is not None and (
        type(config_digest) is not bytes
        or len(config_digest) != 32
        or not any(config_digest)
    ):
        raise ValueError("native v2 config digest must be 32 nonzero bytes")
    if type(preserve_returned_mate) is not bool or not preserve_returned_mate:
        raise ValueError("native v2 production requests must preserve returned mate")

    canonical_profiles = tuple(profiles)
    if (
        not 1 <= len(canonical_profiles) <= NATIVE_V2_MAX_PROFILES
        or any(type(profile) is not NativeV2Profile for profile in canonical_profiles)
    ):
        raise ValueError("native v2 profile pool is invalid")
    digests = [profile.digest for profile in canonical_profiles]
    if len(set(digests)) != len(digests):
        raise ValueError("native v2 profile digests must be unique")
    if any(
        profile.digest != native_v2_profile_digest(profile.weights)
        for profile in canonical_profiles
    ):
        raise ValueError("native v2 profile digest does not match its weights")

    policy_kind = _require_exact_uint(policy_kind, 16, "policy_kind")
    schedule_kind = _require_exact_uint(schedule_kind, 16, "schedule_kind")
    if schedule_kind not in {
        NATIVE_V2_SCHEDULE_SELF_ROUND_ROBIN,
        NATIVE_V2_SCHEDULE_ORDERED_PAIR_ROUND_ROBIN,
    }:
        raise ValueError("native v2 schedule kind is invalid")
    if type(rank_mixture) is not tuple or len(rank_mixture) != 5:
        raise ValueError("native v2 rank mixture is invalid")
    if any(type(value) is not int or not 0 <= value <= 0xFFFF for value in rank_mixture):
        raise ValueError("native v2 rank mixture fields must fit uint16")
    top_weight, near_weight, tail_weight, top_count, near_count = rank_mixture
    if policy_kind == NATIVE_V2_POLICY_UNIFORM:
        if rank_mixture != (0, 0, 0, 0, 0):
            raise ValueError("uniform native v2 policy fields must be zero")
    elif policy_kind == NATIVE_V2_POLICY_RANK_MIXTURE:
        if (
            top_weight + near_weight + tail_weight != 10_000
            or top_weight == 0
            or top_count == 0
            or top_count + near_count > candidate_count
            or (near_count == 0 and near_weight != 0)
            or (top_count + near_count == candidate_count and tail_weight != 0)
        ):
            raise ValueError("native v2 rank mixture policy is invalid")
    else:
        raise ValueError("native v2 policy kind is invalid")

    profile_payload = b"".join(
        NATIVE_V2_PROFILE.pack(profile.digest, *profile.weights)
        for profile in canonical_profiles
    )
    request_size = NATIVE_V2_REQUEST_HEADER.size + len(profile_payload)
    if request_size > MAX_U64:
        raise ValueError("native v2 request exceeds uint64 framing")
    def pack_header(digest: bytes) -> bytes:
        return NATIVE_V2_REQUEST_HEADER.pack(
            NATIVE_V2_REQUEST_MAGIC,
            NATIVE_V2_VERSION,
            NATIVE_V2_REQUEST_HEADER.size,
            0,
            request_size,
            first_attempt,
            attempt_count,
            seed,
            max_attempt_series,
            max_frontier_states,
            max_positions_per_series,
            max_positions_per_game,
            candidate_count,
            len(canonical_profiles),
            policy_kind,
            schedule_kind,
            NATIVE_V2_POLICY_PRESERVE_MATE,
            top_weight,
            near_weight,
            tail_weight,
            top_count,
            near_count,
            0,
            digest,
            0,
        )

    provisional = pack_header(bytes([1]) * 32) + profile_payload
    derived_digest = native_v2_semantic_digest(provisional)
    if config_digest is not None and config_digest != derived_digest:
        raise ValueError("native v2 config digest does not match its canonical request")
    return pack_header(derived_digest) + profile_payload


def decode_native_batch_v2(
    payload: bytes | bytearray | memoryview,
    *,
    expected_first_attempt: int,
    expected_attempt_count: int,
    expected_config_digest: bytes,
    expected_profile_count: int,
    expected_policy_kind: int,
    expected_schedule_kind: int,
) -> NativeBatchV2:
    """Decode v2 only when every request binding is supplied independently."""

    expected_first_attempt = _require_exact_uint(
        expected_first_attempt, 64, "expected_first_attempt"
    )
    expected_attempt_count = _require_exact_uint(
        expected_attempt_count, 32, "expected_attempt_count"
    )
    if expected_attempt_count == 0:
        raise ValueError("expected_attempt_count must be positive")
    if (
        type(expected_config_digest) is not bytes
        or len(expected_config_digest) != 32
        or not any(expected_config_digest)
    ):
        raise ValueError("expected_config_digest must be 32 nonzero bytes")
    if (
        type(expected_profile_count) is not int
        or not 1 <= expected_profile_count <= NATIVE_V2_MAX_PROFILES
    ):
        raise ValueError("expected_profile_count is invalid")
    if type(expected_policy_kind) is not int or expected_policy_kind not in {
        NATIVE_V2_POLICY_UNIFORM,
        NATIVE_V2_POLICY_RANK_MIXTURE,
    }:
        raise ValueError("expected_policy_kind is invalid")
    if type(expected_schedule_kind) is not int or expected_schedule_kind not in {
        NATIVE_V2_SCHEDULE_SELF_ROUND_ROBIN,
        NATIVE_V2_SCHEDULE_ORDERED_PAIR_ROUND_ROBIN,
    }:
        raise ValueError("expected_schedule_kind is invalid")

    view = memoryview(payload)
    if len(view) < NATIVE_V2_RESPONSE_HEADER.size:
        raise ValueError("truncated native full-game v2 batch")
    (
        magic,
        version,
        header_size,
        record_header_size,
        header_flags,
        first_attempt,
        attempt_count,
        config_digest,
        profile_count,
        policy_kind,
        schedule_kind,
        total_saturations,
    ) = NATIVE_V2_RESPONSE_HEADER.unpack_from(view)
    if magic != NATIVE_V2_RESPONSE_MAGIC:
        raise ValueError("native v2 response magic is invalid")
    if (
        version != NATIVE_V2_VERSION
        or header_size != NATIVE_V2_RESPONSE_HEADER.size
        or record_header_size != NATIVE_V2_RECORD_PREFIX.size
    ):
        raise ValueError("native v2 response version/header is unsupported")
    if header_flags not in {0, 1}:
        raise ValueError("native v2 response flags are invalid")
    if (
        first_attempt != expected_first_attempt
        or attempt_count != expected_attempt_count
    ):
        raise ValueError("native v2 response range does not match its request")
    if config_digest != expected_config_digest:
        raise ValueError("native v2 response config digest does not match its request")
    if profile_count != expected_profile_count:
        raise ValueError("native v2 response profile count does not match its request")
    if policy_kind != expected_policy_kind:
        raise ValueError("native v2 response policy does not match its request")
    if schedule_kind != expected_schedule_kind:
        raise ValueError("native v2 response schedule does not match its request")
    if first_attempt + attempt_count > MAX_U64 + 1:
        raise ValueError("native v2 response attempt range overflows uint64")

    records: list[FullGameRecord | RejectedAttempt] = []
    offset = header_size
    saturation_sum = 0
    for expected_attempt in range(first_attempt, first_attempt + attempt_count):
        if offset + NATIVE_V2_RECORD_PREFIX.size > len(view):
            raise ValueError("truncated native full-game v2 record")
        (
            record_size,
            attempt_index,
            status,
            terminal_raw,
            reject_raw,
            reserved,
            flags,
            white_profile_index,
            black_profile_index,
            series_count,
            move_count,
            logical_work,
            path_count_saturations,
        ) = NATIVE_V2_RECORD_PREFIX.unpack_from(view, offset)
        if record_size < NATIVE_V2_RECORD_PREFIX.size:
            raise ValueError("native v2 record size is invalid")
        record_end = offset + record_size
        if record_end > len(view):
            raise ValueError("truncated native full-game v2 record body")
        if attempt_index != expected_attempt:
            raise ValueError("native v2 attempts are missing, duplicated, or out of order")
        if reserved != 0 or status not in {0, 1}:
            raise ValueError("native v2 record status/reserved byte is invalid")
        if flags not in {0, 1} or flags != int(path_count_saturations > 0):
            raise ValueError("native v2 record saturation flags are invalid")
        expected_pair = expected_v2_profile_pair(
            attempt_index, profile_count, schedule_kind
        )
        if (white_profile_index, black_profile_index) != expected_pair:
            raise ValueError("native v2 record profile pair violates its schedule")
        if series_count > MAX_U32 or move_count > MAX_U32:
            raise ValueError("native v2 record counters exceed the Python codec")
        offsets_bytes = series_count * 8
        moves_bytes = move_count * 2
        expected_size = NATIVE_V2_RECORD_PREFIX.size + offsets_bytes + moves_bytes
        if record_size != expected_size:
            raise ValueError("native v2 record counts do not match its byte size")

        body_offset = offset + NATIVE_V2_RECORD_PREFIX.size
        cumulative = tuple(
            struct.unpack_from("<Q", view, body_offset + index * 8)[0]
            for index in range(series_count)
        )
        body_offset += offsets_bytes
        if cumulative and cumulative[-1] != move_count:
            raise ValueError("native v2 series offsets do not cover every move")
        if any(
            end <= (cumulative[index - 1] if index else 0)
            for index, end in enumerate(cumulative)
        ):
            raise ValueError("native v2 series offsets must be strictly increasing")

        if status == 0:
            if reject_raw != 0:
                raise ValueError("accepted native v2 record carries a reject reason")
            try:
                terminal = Terminal(terminal_raw)
            except ValueError as error:
                raise ValueError("native v2 terminal code is invalid") from error
            if terminal == Terminal.NONE or not series_count:
                raise ValueError("accepted native v2 record is not a full terminal game")
            flat_moves = tuple(
                unpack_move(
                    struct.unpack_from("<H", view, body_offset + index * 2)[0]
                )
                for index in range(move_count)
            )
            series: list[tuple[str, ...]] = []
            begin = 0
            for end in cumulative:
                series.append(flat_moves[begin:end])
                begin = end
            records.append(
                FullGameRecord(
                    attempt_index,
                    terminal,
                    tuple(series),
                    logical_work,
                    white_profile_index,
                    black_profile_index,
                    path_count_saturations,
                )
            )
        else:
            if terminal_raw != 0 or series_count != 0 or move_count != 0:
                raise ValueError("rejected native v2 record carries WDL or trace data")
            try:
                reason = RejectReason(reject_raw)
            except ValueError as error:
                raise ValueError("native v2 reject code is invalid") from error
            if reason in {
                RejectReason.NONE,
                RejectReason.CANCELLED,
                RejectReason.DUPLICATE_TRACE,
            }:
                raise ValueError("native v2 rejected record has no native reject reason")
            records.append(
                RejectedAttempt(
                    attempt_index,
                    reason,
                    logical_work,
                    white_profile_index,
                    black_profile_index,
                    path_count_saturations,
                )
            )
        saturation_sum = min(
            MAX_U64, saturation_sum + path_count_saturations
        )
        offset = record_end

    if offset != len(view):
        raise ValueError("native full-game v2 batch has trailing bytes")
    if total_saturations != saturation_sum:
        raise ValueError("native v2 response saturation total is invalid")
    if header_flags != int(total_saturations > 0):
        raise ValueError("native v2 response saturation flag is invalid")
    return NativeBatchV2(
        first_attempt,
        attempt_count,
        bytes(config_digest),
        profile_count,
        policy_kind,
        schedule_kind,
        total_saturations,
        tuple(records),
    )


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def encode_chunk(
    records: Iterable[FullGameRecord],
    *,
    simulation_id: str,
    first_attempt: int,
    attempt_count: int,
) -> bytes:
    canonical = tuple(records)
    if len(canonical) > MAX_U32 or any(
        type(record) is not FullGameRecord for record in canonical
    ):
        raise ValueError("chunk records must be exact accepted full-game records")
    if not SIMULATION_ID_PATTERN.fullmatch(simulation_id):
        raise ValueError("simulation_id is invalid")
    if type(first_attempt) is not int or not 0 <= first_attempt <= MAX_U64:
        raise ValueError("first_attempt must fit uint64")
    if type(attempt_count) is not int or not 1 <= attempt_count <= MAX_U64:
        raise ValueError("attempt_count must fit positive uint64")
    limit = first_attempt + attempt_count
    if limit > MAX_U64 + 1:
        raise ValueError("chunk attempt range overflows uint64")
    attempts = [record.attempt_index for record in canonical]
    if attempts != sorted(set(attempts)):
        raise ValueError("chunk records must be unique and ordered by attempt")
    if any(not first_attempt <= attempt < limit for attempt in attempts):
        raise ValueError("chunk record falls outside its committed attempt range")
    header = {
        "accepted_records": len(canonical),
        "attempt_count": attempt_count,
        "first_attempt": first_attempt,
        "schema": CHUNK_SCHEMA,
        "simulation_id": simulation_id,
    }
    encoded_header = _canonical_json(header)
    if len(encoded_header) > MAX_U32:
        raise ValueError("full-game chunk header exceeds uint32 framing")
    payload = bytearray(
        CHUNK_HEADER_PREFIX.pack(CHUNK_MAGIC, len(encoded_header))
    )
    payload.extend(encoded_header)
    for record in canonical:
        body = encode_record(record)
        if len(body) > MAX_U32:
            raise ValueError("full-game record exceeds uint32 framing")
        payload.extend(CHUNK_FRAME_PREFIX.pack(len(body)))
        payload.extend(body)
        payload.extend(CHUNK_FRAME_CRC.pack(zlib.crc32(body) & 0xFFFFFFFF))
    return bytes(payload)


def decode_chunk(payload: bytes | bytearray | memoryview) -> DecodedChunk:
    view = memoryview(payload)
    if len(view) < CHUNK_HEADER_PREFIX.size:
        raise ValueError("truncated full-game chunk")
    magic, header_length = CHUNK_HEADER_PREFIX.unpack_from(view)
    if magic != CHUNK_MAGIC:
        raise ValueError("full-game chunk magic is invalid")
    header_start = CHUNK_HEADER_PREFIX.size
    header_end = header_start + header_length
    if header_end > len(view):
        raise ValueError("truncated full-game chunk header")
    try:
        header = json.loads(bytes(view[header_start:header_end]).decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("full-game chunk header JSON is invalid") from error
    if not isinstance(header, Mapping) or header.get("schema") != CHUNK_SCHEMA:
        raise ValueError("full-game chunk schema is unsupported")
    if _canonical_json(header) != bytes(view[header_start:header_end]):
        raise ValueError("full-game chunk header is not canonical")
    if set(header) != {
        "accepted_records",
        "attempt_count",
        "first_attempt",
        "schema",
        "simulation_id",
    }:
        raise ValueError("full-game chunk header keys are invalid")
    try:
        first_attempt = header["first_attempt"]
        attempt_count = header["attempt_count"]
        accepted_count = header["accepted_records"]
        simulation_id = header["simulation_id"]
    except KeyError as error:  # pragma: no cover - exact key check above
        raise ValueError("full-game chunk header fields are invalid") from error
    if any(type(value) is not int for value in (
        first_attempt,
        attempt_count,
        accepted_count,
    )) or type(simulation_id) is not str:
        raise ValueError("full-game chunk header field types are invalid")
    if not 1 <= attempt_count <= MAX_U64:
        raise ValueError("full-game chunk attempt count is invalid")
    if not 0 <= accepted_count <= attempt_count:
        raise ValueError("full-game chunk accepted count is invalid")

    records: list[FullGameRecord] = []
    offset = header_end
    while offset < len(view):
        if offset + CHUNK_FRAME_PREFIX.size > len(view):
            raise ValueError("truncated full-game frame length")
        body_length = CHUNK_FRAME_PREFIX.unpack_from(view, offset)[0]
        offset += CHUNK_FRAME_PREFIX.size
        body_end = offset + body_length
        crc_end = body_end + CHUNK_FRAME_CRC.size
        if crc_end > len(view):
            raise ValueError("truncated full-game frame")
        body = bytes(view[offset:body_end])
        expected_crc = CHUNK_FRAME_CRC.unpack_from(view, body_end)[0]
        if zlib.crc32(body) & 0xFFFFFFFF != expected_crc:
            raise ValueError("full-game frame CRC mismatch")
        records.append(decode_record(body))
        offset = crc_end
    if len(records) != accepted_count:
        raise ValueError("full-game chunk record count does not match its header")
    attempts = [record.attempt_index for record in records]
    if attempts != sorted(set(attempts)):
        raise ValueError("full-game chunk attempts are duplicated or unordered")
    if not 0 <= first_attempt <= MAX_U64 or not 0 <= attempt_count <= MAX_U64:
        raise ValueError("full-game chunk attempt range is invalid")
    limit = first_attempt + attempt_count
    if limit > MAX_U64 + 1 or any(
        not first_attempt <= attempt < limit for attempt in attempts
    ):
        raise ValueError("full-game chunk record lies outside its attempt range")
    if not SIMULATION_ID_PATTERN.fullmatch(simulation_id):
        raise ValueError("full-game chunk simulation_id is invalid")
    return DecodedChunk(dict(header), tuple(records))


def chunk_sha256(payload: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(payload).hexdigest()
