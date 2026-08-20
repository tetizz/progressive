from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
from pathlib import Path
from typing import Iterable

import chess
import chess.polyglot


ENGINE_VERSION = "spc-0.6.0"
RULESET_VERSION = "scottish-modern-common-v1"
QUIET_DRAW_POLICY = "manual-proof-required"
MASK_64 = (1 << 64) - 1


def _source_fingerprint() -> str:
    digest = hashlib.sha256()
    package = Path(__file__).resolve().parent
    paths = (
        path
        for pattern in ("*.py", "*.cpp", "*.hpp", "*.h")
        for path in package.rglob(pattern)
    )
    for path in sorted(paths, key=lambda item: item.relative_to(package).as_posix()):
        relative = path.relative_to(package).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


ENGINE_SOURCE_FINGERPRINT = _source_fingerprint()


def _splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & MASK_64
    value = (value ^ (value >> 30)) * 0xBF58476D1CE4E5B9 & MASK_64
    value = (value ^ (value >> 27)) * 0x94D049BB133111EB & MASK_64
    return (value ^ (value >> 31)) & MASK_64


def boundary_fen(
    board: chess.Board, ep_targets: Iterable[int] | None = None
) -> str:
    """Returns the rule-relevant orthodox portion of a boundary position."""

    turn = "w" if board.turn == chess.WHITE else "b"
    castling = board.castling_xfen() or "-"
    targets = tuple(ep_targets) if ep_targets is not None else (
        (board.ep_square,) if board.ep_square is not None else ()
    )
    ep = ",".join(chess.square_name(square) for square in sorted(targets)) or "-"
    return f"{board.board_fen()} {turn} {castling} {ep}"


def progressive_zobrist(
    board: chess.Board,
    series_number: int,
    quiet_series: int,
    ep_targets: Iterable[int] = (),
) -> int:
    """Polyglot Zobrist board key mixed with progressive state fields."""

    base = chess.polyglot.zobrist_hash(board)
    value = (
        base
        ^ _splitmix64(series_number ^ 0x535043)
        ^ _splitmix64(quiet_series ^ 0x5155494554)
    ) & MASK_64
    for index, square in enumerate(sorted(ep_targets)):
        value ^= _splitmix64(0x455000 + index * 64 + square)
    return value & MASK_64


class Outcome(StrEnum):
    CHECKMATE = "checkmate"
    STALEMATE = "stalemate"
    TEN_SERIES_DRAW = "ten-series-draw"


@dataclass(slots=True)
class ProgressiveState:
    """A Scottish Progressive position at a complete-series boundary."""

    board: chess.Board = field(repr=False)
    series_number: int = 1
    quiet_series: int = 0
    ep_targets: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        self.board = self.board.copy(stack=False)
        inherited_ep = self.board.ep_square
        # python-chess validates only orthodox single-ply e.p. semantics. The
        # progressive set is validated and canonicalized below instead.
        self.board.ep_square = None
        if not self.board.is_valid():
            raise ValueError(
                f"invalid orthodox board state (status={self.board.status()})"
            )
        if self.series_number < 1:
            raise ValueError("series_number must be at least 1")
        expected = chess.WHITE if self.series_number % 2 else chess.BLACK
        if self.board.turn != expected:
            color = "White" if expected else "Black"
            raise ValueError(
                f"series {self.series_number} belongs to {color}, but FEN has "
                f"{'White' if self.board.turn else 'Black'} to move"
            )
        if self.quiet_series < 0:
            raise ValueError("quiet_series cannot be negative")
        if not self.ep_targets and inherited_ep is not None:
            self.ep_targets = (inherited_ep,)
        supplied_targets = tuple(sorted(set(self.ep_targets)))
        expected_rank = 5 if self.board.turn == chess.WHITE else 2
        pawn_offset = -8 if self.board.turn == chess.WHITE else 8
        pawn_color = not self.board.turn
        canonical_targets: list[int] = []
        for target in supplied_targets:
            if chess.square_rank(target) != expected_rank:
                raise ValueError(
                    f"invalid progressive en-passant target {chess.square_name(target)}"
                )
            pawn = self.board.piece_at(target + pawn_offset)
            if pawn != chess.Piece(chess.PAWN, pawn_color):
                raise ValueError(
                    f"progressive en-passant target {chess.square_name(target)} "
                    "has no matching double-stepped pawn"
                )
            if self.board.piece_at(target) is not None:
                raise ValueError(
                    f"progressive en-passant target {chess.square_name(target)} is occupied"
                )
            probe = self.board.copy(stack=False)
            probe.ep_square = target
            if any(probe.is_en_passant(move) for move in probe.legal_moves):
                canonical_targets.append(target)
        self.ep_targets = tuple(canonical_targets)
        self.board.ep_square = self.ep_targets[0] if len(self.ep_targets) == 1 else None

    @classmethod
    def initial(cls) -> ProgressiveState:
        return cls(chess.Board(), series_number=1)

    @classmethod
    def from_fen(
        cls,
        fen: str,
        series_number: int,
        *,
        quiet_series: int = 0,
        ep_targets: Iterable[int] = (),
    ) -> ProgressiveState:
        return cls(
            chess.Board(fen),
            series_number=series_number,
            quiet_series=quiet_series,
            ep_targets=tuple(ep_targets),
        )

    def copy(self) -> ProgressiveState:
        return ProgressiveState(
            self.board.copy(stack=False),
            self.series_number,
            self.quiet_series,
            self.ep_targets,
        )

    @property
    def moves_available(self) -> int:
        return self.series_number

    @property
    def quiet_draw_pending(self) -> bool:
        return self.quiet_series >= 10

    @property
    def side_name(self) -> str:
        return "White" if self.board.turn == chess.WHITE else "Black"

    @property
    def boundary_key(self) -> str:
        return boundary_fen(self.board, self.ep_targets)

    @property
    def zobrist(self) -> int:
        return progressive_zobrist(
            self.board, self.series_number, self.quiet_series, self.ep_targets
        )

    @property
    def position_hash(self) -> str:
        verification = hashlib.blake2b(
            (
                f"{self.boundary_key}|{self.series_number}|{self.quiet_series}|"
                f"{RULESET_VERSION}"
            ).encode("ascii"),
            digest_size=8,
        ).hexdigest()
        return f"{self.zobrist:016x}{verification}"

    @property
    def transposition_key(self) -> tuple[int, str, int, int]:
        # The full boundary string verifies the 64-bit Zobrist bucket, so a
        # rare collision cannot silently merge two distinct positions.
        return (
            self.zobrist,
            self.boundary_key,
            self.series_number,
            self.quiet_series,
        )

    @property
    def pfen(self) -> str:
        ep = ",".join(chess.square_name(square) for square in self.ep_targets) or "-"
        base = self.board.fen(en_passant="fen")
        return (
            f"{base} | series={self.series_number} quiet={self.quiet_series} "
            f"progressive_ep={ep} rules={RULESET_VERSION} "
            f"quiet_draw={QUIET_DRAW_POLICY}"
        )

@dataclass(frozen=True, slots=True)
class SeriesResult:
    moves: tuple[str, ...]
    san: tuple[str, ...]
    final_state: ProgressiveState
    ended_by_check: bool = False
    outcome: Outcome | None = None
    unused_moves: int = 0
    transposition_count: int = 1

    @property
    def used_moves(self) -> int:
        return len(self.moves)

    @property
    def notation(self) -> str:
        if not self.san:
            return "(no legal move)"
        return " / ".join(self.san)

    @property
    def machine_notation(self) -> str:
        return "/".join(self.moves)

    @property
    def is_terminal(self) -> bool:
        return self.outcome is not None

    def with_transposition_count(self, count: int) -> SeriesResult:
        return SeriesResult(
            self.moves,
            self.san,
            self.final_state,
            self.ended_by_check,
            self.outcome,
            self.unused_moves,
            count,
        )


@dataclass(frozen=True, slots=True)
class AnalysisStamp:
    engine_version: str = ENGINE_VERSION
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
