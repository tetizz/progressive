from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math

import chess

from . import rules, series_mate
from .model import (
    Outcome,
    ProgressiveState,
    QUIET_DRAW_POLICY,
    RULESET_VERSION,
    SeriesResult,
)
from .rules import play_series
from .series_mate import SeriesMateStatus


SINGLE_REPLY_MATE_LADDER_PROOF_SCHEMA = (
    "spc-single-reply-mate-ladder-proof-v1"
)
_SIGNED_64_MAX = (1 << 63) - 1
_UNSIGNED_64_MAX = (1 << 64) - 1


class SingleReplyMateLadderStatus(StrEnum):
    """Tri-state result for the narrow A/check, B/countercheck, C/mate proof."""

    FOUND = "found"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SingleReplyMateLadderStats:
    attack_positions_visited: int = 0
    attack_moves_generated: int = 0
    reply_positions_visited: int = 0
    reply_moves_generated: int = 0
    mate_positions_visited: int = 0
    mate_moves_generated: int = 0
    attack_transpositions_merged: int = 0
    mate_transpositions_merged: int = 0
    checking_series: int = 0
    forced_counterchecks: int = 0
    mate_probes: int = 0
    peak_attack_frontier: int = 0
    attack_max_depth_reached: int = 0
    mate_max_depth_reached: int = 0

    @property
    def work_used(self) -> int:
        """Combined, non-overlapping native positions-plus-edges work."""

        return (
            self.attack_positions_visited
            + self.attack_moves_generated
            + self.reply_positions_visited
            + self.reply_moves_generated
            + self.mate_positions_visited
            + self.mate_moves_generated
        )


def _state_payload(state: ProgressiveState) -> dict[str, object]:
    board = state.board
    return {
        "fen": board.fen(en_passant="fen"),
        "series_number": state.series_number,
        "quiet_series": state.quiet_series,
        "ep_targets": [
            chess.square_name(square) for square in sorted(state.ep_targets)
        ],
        "promoted_hex": f"{board.promoted:016x}",
        "chess960": board.chess960,
        "rules": RULESET_VERSION,
        "quiet_draw": QUIET_DRAW_POLICY,
    }


def _series_payload(series: SeriesResult) -> dict[str, object]:
    return {
        "moves": list(series.moves),
        "san": list(series.san),
        "final_state": _state_payload(series.final_state),
        "ended_by_check": series.ended_by_check,
        "outcome": None if series.outcome is None else series.outcome.value,
        "unused_moves": series.unused_moves,
        "transposition_count": series.transposition_count,
    }


def _canonical_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _root_identity_sha256(root: ProgressiveState) -> str:
    return _canonical_sha256(
        {
            "schema": "spc-full-progressive-state-v1",
            "state": _state_payload(root),
        }
    )


def _proof_identity_sha256(
    root: ProgressiveState,
    attack: SeriesResult,
    forced_reply: SeriesResult,
    mate: SeriesResult,
) -> str:
    return _canonical_sha256(
        {
            "schema": SINGLE_REPLY_MATE_LADDER_PROOF_SCHEMA,
            "root": _state_payload(root),
            "attack": _series_payload(attack),
            "forced_reply": _series_payload(forced_reply),
            "mate": _series_payload(mate),
        }
    )


@dataclass(frozen=True, slots=True)
class SingleReplyMateLadderProof:
    root_identity_sha256: str
    attack: SeriesResult
    forced_reply: SeriesResult
    mate: SeriesResult
    identity_sha256: str
    schema: str = SINGLE_REPLY_MATE_LADDER_PROOF_SCHEMA

    @classmethod
    def create(
        cls,
        root: ProgressiveState,
        attack: SeriesResult,
        forced_reply: SeriesResult,
        mate: SeriesResult,
    ) -> SingleReplyMateLadderProof:
        return cls(
            _root_identity_sha256(root),
            attack,
            forced_reply,
            mate,
            _proof_identity_sha256(root, attack, forced_reply, mate),
        )

    def recomputed_identity_sha256(self, root: ProgressiveState) -> str:
        return _proof_identity_sha256(
            root,
            self.attack,
            self.forced_reply,
            self.mate,
        )


@dataclass(frozen=True, slots=True)
class SingleReplyMateLadderProbe:
    status: SingleReplyMateLadderStatus
    native_status: SeriesMateStatus
    message: str
    stats: SingleReplyMateLadderStats = SingleReplyMateLadderStats()
    proof: SingleReplyMateLadderProof | None = None

    @property
    def proven_losing(self) -> bool:
        """Only an exact, replayed FOUND proof is eligible to veto a move."""

        return (
            self.status is SingleReplyMateLadderStatus.FOUND
            and self.native_status is SeriesMateStatus.FOUND
            and self.proof is not None
        )

    @property
    def work_used(self) -> int:
        return self.stats.work_used


def _unknown(
    native_status: SeriesMateStatus,
    message: str,
    stats: SingleReplyMateLadderStats = SingleReplyMateLadderStats(),
) -> SingleReplyMateLadderProbe:
    return SingleReplyMateLadderProbe(
        SingleReplyMateLadderStatus.UNKNOWN,
        native_status,
        message,
        stats,
    )


def _validate_native_found(
    root: ProgressiveState,
    attack_moves: tuple[str, ...],
    forced_reply_moves: tuple[str, ...],
    mate_moves: tuple[str, ...],
) -> SingleReplyMateLadderProof:
    """Replay every proof edge through the authoritative Python rules oracle."""

    try:
        attack = play_series(root, attack_moves)
    except Exception as error:
        raise RuntimeError(
            "native ladder attack failed authoritative replay"
        ) from error
    if attack.outcome is not None or not attack.ended_by_check:
        raise RuntimeError("native ladder attack failed authoritative replay")

    # The proof subclass is deliberately narrower than "one complete reply":
    # there must be exactly one legal *first* move at the checked boundary.
    first_variants = rules._legal_move_variants(  # noqa: SLF001
        attack.final_state.board,
        attack.final_state.ep_targets,
    )
    if len(first_variants) != 1:
        raise RuntimeError("native ladder reply was not the unique legal first move")
    only_uci = first_variants[0][0].uci()
    if forced_reply_moves != (only_uci,):
        raise RuntimeError("native ladder returned a different forced reply")

    try:
        forced_reply = play_series(attack.final_state, forced_reply_moves)
    except Exception as error:
        raise RuntimeError(
            "native ladder forced reply failed countercheck replay"
        ) from error
    if forced_reply.outcome is not None or not forced_reply.ended_by_check:
        raise RuntimeError("native ladder forced reply failed countercheck replay")

    try:
        mate = play_series(forced_reply.final_state, mate_moves)
    except Exception as error:
        raise RuntimeError(
            "native ladder mate failed authoritative replay"
        ) from error
    if mate.outcome is not Outcome.CHECKMATE or not mate.ended_by_check:
        raise RuntimeError("native ladder mate failed authoritative replay")

    attacker = root.board.turn
    if (
        attack.final_state.board.turn == attacker
        or forced_reply.final_state.board.turn != attacker
        or mate.final_state.board.turn == attacker
    ):
        raise RuntimeError("native ladder color sequence failed replay")
    return SingleReplyMateLadderProof.create(root, attack, forced_reply, mate)


def find_native_single_reply_mate_ladder(
    state: ProgressiveState,
    *,
    max_work: int | None = 50_000_000,
    time_limit_seconds: float | None = None,
) -> SingleReplyMateLadderProbe:
    """Prove one narrow three-series forced-mate ladder in native code.

    The attacker must find a checking series ``A``. At that exact boundary the
    defender must have one legal first move, which itself checks and ends
    series ``B``. The attacker must then have an exact immediate series mate
    ``C``. Native work limits, deadlines, unavailable kernels, and unsupported
    states are all UNKNOWN. Only a native FOUND result that survives complete
    Python replay can be consumed as a losing-move veto.
    """

    if max_work is not None and (type(max_work) is not int or max_work < 1):
        raise ValueError("max_work must be a positive integer or None")
    if time_limit_seconds is not None and (
        isinstance(time_limit_seconds, bool)
        or not isinstance(time_limit_seconds, (int, float))
        or not math.isfinite(time_limit_seconds)
        or time_limit_seconds < 0
    ):
        raise ValueError("time_limit_seconds must be finite and nonnegative")
    if state.board.chess960:
        return _unknown(
            SeriesMateStatus.UNSUPPORTED,
            "Chess960 is outside the native ladder-search contract",
        )
    if state.series_number > _SIGNED_64_MAX:
        return _unknown(
            SeriesMateStatus.UNSUPPORTED,
            "series number is outside the signed 64-bit contract",
        )
    if state.series_number > 254:
        return _unknown(
            SeriesMateStatus.UNSUPPORTED,
            "three-series ladder exceeds the native series contract",
        )
    if max_work is not None and max_work > _UNSIGNED_64_MAX:
        return _unknown(
            SeriesMateStatus.UNSUPPORTED,
            "work limit is outside the unsigned 64-bit contract",
        )

    board = state.board
    native_words = (
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
        board.occupied_co[chess.WHITE],
        board.occupied_co[chess.BLACK],
        board.promoted,
        board.clean_castling_rights(),
    )
    if any(
        type(value) is not int or not 0 <= value <= _UNSIGNED_64_MAX
        for value in native_words
    ):
        return _unknown(
            SeriesMateStatus.UNSUPPORTED,
            "board word is outside the unsigned 64-bit contract",
        )

    native = series_mate._native_mate  # noqa: SLF001
    if native is None or not hasattr(native, "find_single_reply_mate_ladder"):
        return _unknown(
            SeriesMateStatus.UNSUPPORTED,
            "source-matched native ladder search is unavailable",
        )
    if (
        getattr(native, "SOURCE_IDENTITY", None)
        != series_mate._native_mate_source_identity()  # noqa: SLF001
    ):
        return _unknown(
            SeriesMateStatus.UNSUPPORTED,
            "native ladder-search source identity does not match",
        )

    if time_limit_seconds is None:
        remaining_nanoseconds = None
    elif time_limit_seconds >= _UNSIGNED_64_MAX / 1_000_000_000:
        remaining_nanoseconds = _UNSIGNED_64_MAX
    else:
        remaining_nanoseconds = int(time_limit_seconds * 1_000_000_000)

    raw = tuple(
        native.find_single_reply_mate_ladder(
            *native_words,
            board.turn,
            state.series_number,
            state.ep_targets,
            remaining_nanoseconds,
            max_work,
        )
    )
    if len(raw) != 6:
        raise RuntimeError("native single-reply ladder result shape mismatch")
    status_index, message, raw_stats, attack_moves, reply_moves, mate_moves = raw
    native_statuses = tuple(SeriesMateStatus)
    if type(status_index) is not int or not 0 <= status_index < len(native_statuses):
        raise RuntimeError("native single-reply ladder status is invalid")
    native_status = native_statuses[status_index]

    values = tuple(raw_stats)
    if len(values) != 14 or any(type(value) is not int or value < 0 for value in values):
        raise RuntimeError("native single-reply ladder statistics are invalid")
    stats = SingleReplyMateLadderStats(*values)
    paths = tuple(tuple(path) for path in (attack_moves, reply_moves, mate_moves))
    if any(any(type(move) is not str for move in path) for path in paths):
        raise RuntimeError("native single-reply ladder line is invalid")

    if native_status is SeriesMateStatus.FOUND:
        if any(not path for path in paths):
            raise RuntimeError("native FOUND ladder omitted a proof path")
        proof = _validate_native_found(state, *paths)
        return SingleReplyMateLadderProbe(
            SingleReplyMateLadderStatus.FOUND,
            native_status,
            str(message),
            stats,
            proof,
        )
    if any(paths):
        raise RuntimeError("incomplete native ladder result carried a proof path")
    if native_status is SeriesMateStatus.EXHAUSTED:
        return SingleReplyMateLadderProbe(
            SingleReplyMateLadderStatus.EXHAUSTED,
            native_status,
            str(message),
            stats,
        )
    return _unknown(native_status, str(message), stats)
