from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import math
import os
from pathlib import Path

import chess

from .model import Outcome, ProgressiveState, SeriesResult
from .rules import play_series


_NATIVE_MATE_SOURCE_FILES = ("_native_mate.cpp",)
_SIGNED_64_MAX = (1 << 63) - 1
_UNSIGNED_64_MAX = (1 << 64) - 1


def _native_mate_source_identity() -> str | None:
    """Digest the separately packaged sources accepted by this adapter."""

    package = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    try:
        for filename in _NATIVE_MATE_SOURCE_FILES:
            digest.update(filename.encode("utf-8"))
            digest.update((package / filename).read_bytes())
    except OSError:
        return None
    return digest.hexdigest()


def _validated_native_mate_module(candidate: object | None) -> object | None:
    expected = _native_mate_source_identity()
    if expected is None or getattr(candidate, "SOURCE_IDENTITY", None) != expected:
        return None
    return candidate


if os.environ.get("SPC_DISABLE_NATIVE_MATE") == "1":
    _native_mate = None
else:
    try:
        from . import _native_mate as _native_mate_candidate
    except ImportError:
        _native_mate_candidate = None
    _native_mate = _validated_native_mate_module(_native_mate_candidate)
    del _native_mate_candidate


def native_mate_runtime_identity() -> str:
    """Return the exact accepted extension identity or an unavailable marker."""

    expected = _native_mate_source_identity()
    actual = getattr(_native_mate, "SOURCE_IDENTITY", None)
    if expected is not None and actual == expected:
        return expected
    return "unavailable"


class SeriesMateStatus(StrEnum):
    FOUND = "found"
    EXHAUSTED = "exhausted"
    WORK_LIMIT = "work-limit"
    DEADLINE = "deadline"
    UNSUPPORTED = "unsupported"


_NATIVE_STATUSES = (
    SeriesMateStatus.FOUND,
    SeriesMateStatus.EXHAUSTED,
    SeriesMateStatus.WORK_LIMIT,
    SeriesMateStatus.DEADLINE,
    SeriesMateStatus.UNSUPPORTED,
)


@dataclass(frozen=True, slots=True)
class SeriesMateProbe:
    status: SeriesMateStatus
    message: str
    series: SeriesResult | None = None
    positions_visited: int = 0
    moves_generated: int = 0
    transpositions_merged: int = 0
    checking_series: int = 0
    checkmates: int = 0
    peak_frontier: int = 0
    max_depth_reached: int = 0

    @property
    def complete(self) -> bool:
        """Whether the result settles one-series mate existence."""

        return self.status in (
            SeriesMateStatus.FOUND,
            SeriesMateStatus.EXHAUSTED,
        )

    @property
    def exhausted(self) -> bool:
        return self.status is SeriesMateStatus.EXHAUSTED

    @property
    def cancelled(self) -> bool:
        return self.status is SeriesMateStatus.DEADLINE

    @property
    def work_limit_reached(self) -> bool:
        return self.status is SeriesMateStatus.WORK_LIMIT


def _unsupported(message: str) -> SeriesMateProbe:
    return SeriesMateProbe(SeriesMateStatus.UNSUPPORTED, message)


def find_native_series_mate(
    state: ProgressiveState,
    *,
    max_positions: int | None = 250_000,
    max_work: int | None = None,
    time_limit_seconds: float | None = None,
) -> SeriesMateProbe:
    """Find one same-series mate with uncapped isolated native search.

    ``EXHAUSTED`` is the only negative proof. A work cap, deadline, unavailable
    extension, or unsupported board returns an explicit unknown status. Every
    positive result is replayed through the Python rules oracle before it
    crosses this boundary.
    """

    if max_positions is not None and (
        type(max_positions) is not int or max_positions < 1
    ):
        raise ValueError("max_positions must be a positive integer or None")
    if max_work is not None and (
        type(max_work) is not int or max_work < 1
    ):
        raise ValueError("max_work must be a positive integer or None")
    if time_limit_seconds is not None and (
        isinstance(time_limit_seconds, bool)
        or not isinstance(time_limit_seconds, (int, float))
        or not math.isfinite(time_limit_seconds)
        or time_limit_seconds < 0
    ):
        raise ValueError("time_limit_seconds must be finite and nonnegative")
    if state.board.chess960:
        return _unsupported("Chess960 is outside the native mate-search contract")
    if state.series_number > _SIGNED_64_MAX:
        return _unsupported("series number is outside the signed 64-bit contract")
    if max_positions is not None and max_positions > _UNSIGNED_64_MAX:
        return _unsupported("position limit is outside the unsigned 64-bit contract")
    if max_work is not None and max_work > _UNSIGNED_64_MAX:
        return _unsupported("work limit is outside the unsigned 64-bit contract")

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
        return _unsupported("board word is outside the unsigned 64-bit contract")

    native = _native_mate
    if native is None or not hasattr(native, "find_series_mate"):
        return _unsupported("source-matched isolated native mate search is unavailable")
    if getattr(native, "SOURCE_IDENTITY", None) != _native_mate_source_identity():
        return _unsupported("isolated native mate-search source identity does not match")

    if time_limit_seconds is None:
        remaining_nanoseconds = None
    elif time_limit_seconds >= _UNSIGNED_64_MAX / 1_000_000_000:
        remaining_nanoseconds = _UNSIGNED_64_MAX
    else:
        remaining_nanoseconds = int(time_limit_seconds * 1_000_000_000)
    raw = tuple(
        native.find_series_mate(
            *native_words,
            board.turn,
            state.series_number,
            state.ep_targets,
            max_positions,
            remaining_nanoseconds,
            max_work,
        )
    )
    if len(raw) != 4:
        raise RuntimeError("native series-mate result shape mismatch")
    status_index, message, stats, moves = raw
    if type(status_index) is not int or not 0 <= status_index < len(_NATIVE_STATUSES):
        raise RuntimeError("native series-mate status is invalid")
    status = _NATIVE_STATUSES[status_index]
    stats = tuple(stats)
    moves = tuple(moves)
    if len(stats) != 7 or any(type(value) is not int or value < 0 for value in stats):
        raise RuntimeError("native series-mate statistics are invalid")
    if any(type(move) is not str for move in moves):
        raise RuntimeError("native series-mate line is invalid")

    series: SeriesResult | None = None
    if status is SeriesMateStatus.FOUND:
        series = play_series(state, moves)
        if series.outcome is not Outcome.CHECKMATE or not series.ended_by_check:
            raise RuntimeError("native series-mate line failed authoritative replay")
    elif moves:
        raise RuntimeError("incomplete native series-mate result carried a line")

    return SeriesMateProbe(status, str(message), series, *stats)
