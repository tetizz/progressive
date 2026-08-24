from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import chess

from .evaluation import PIECE_VALUES
from .fast_training import FEATURE_NAMES
from .model import ProgressiveState
from .rules import _legal_move_variants


TEACHER_VALUE_FEATURE_SCHEMA = "spc-teacher-value-features-v3"
TEACHER_VALUE_FEATURE_NAMES = (
    *FEATURE_NAMES,
    *(f"{name}_x_centered_phase" for name in FEATURE_NAMES),
    "king_ring_attack_balance",
    "promotable_next_series_balance",
    "king_edge_safety_balance",
    "check_route_balance",
    "reach_complete",
    "developed_minor_balance",
    "center_occupancy_balance",
    "center_control_balance",
    "extended_center_control_balance",
    "pawn_space_balance",
    "passed_pawn_balance",
    "passed_pawn_advance_balance",
    "connected_passed_pawn_balance",
    "isolated_pawn_liability_balance",
    "doubled_pawn_liability_balance",
    "pawn_island_liability_balance",
    "bishop_pair_balance",
    "rook_open_file_balance",
    "rook_seventh_rank_balance",
    "king_pawn_shelter_balance",
    "attacked_material_balance",
    "hanging_material_balance",
    "pinned_material_balance",
    "queen_exposure_balance",
    "direct_capture_count_for_mover",
    "direct_capture_value_for_mover",
    "direct_max_capture_value_for_mover",
    "direct_check_count_for_mover",
    "direct_mate_count_for_mover",
    "direct_promotion_count_for_mover",
    "two_move_capture_value_for_mover",
    "two_move_check_routes_for_mover",
    "two_move_mate_routes_for_mover",
)


def state_from_pfen(
    pfen: str,
    *,
    promoted_bitboard: int | None = None,
    chess960: bool | None = None,
) -> ProgressiveState:
    try:
        fen, metadata = pfen.split(" | ", 1)
        values = dict(
            token.split("=", 1)
            for token in metadata.split()
            if "=" in token
        )
        series_number = int(values["series"])
        quiet_series = int(values.get("quiet", "0"))
        progressive_ep = values.get("progressive_ep", "-")
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid progressive FEN: {pfen}") from error
    ep_targets = (
        ()
        if progressive_ep == "-"
        else tuple(chess.parse_square(item) for item in progressive_ep.split(","))
    )
    if promoted_bitboard is not None and (
        type(promoted_bitboard) is not int
        or not 0 <= promoted_bitboard < (1 << 64)
    ):
        raise ValueError("promoted bitboard must be an unsigned 64-bit integer")
    if chess960 is not None and type(chess960) is not bool:
        raise TypeError("chess960 must be a boolean")
    board = chess.Board(fen, chess960=bool(chess960))
    if promoted_bitboard is not None:
        parsed_promoted = board.promoted
        if "~" in fen.split()[0] and parsed_promoted != promoted_bitboard:
            raise ValueError("semantic FEN and promoted bitboard disagree")
        invalid_promoted = promoted_bitboard & (
            ~board.occupied | board.pawns | board.kings
        )
        if invalid_promoted:
            raise ValueError("promoted bitboard marks an impossible square")
        board.promoted = promoted_bitboard
    return ProgressiveState(
        board,
        series_number=series_number,
        quiet_series=quiet_series,
        ep_targets=ep_targets,
    )


def _route_value(distance: Any) -> int:
    if distance is None:
        return 0
    return 4 - min(4, max(0, int(distance)))


def _minor_development(board: chess.Board, color: chess.Color) -> int:
    home_rank = 0 if color == chess.WHITE else 7
    return sum(
        chess.square_rank(square) != home_rank
        for piece_type in (chess.KNIGHT, chess.BISHOP)
        for square in board.pieces(piece_type, color)
    )


def _control(board: chess.Board, color: chess.Color, squares: tuple[int, ...]) -> int:
    return sum(len(board.attackers(color, square)) for square in squares)


def _pawn_files(board: chess.Board, color: chess.Color) -> tuple[int, ...]:
    return tuple(sorted(chess.square_file(square) for square in board.pieces(chess.PAWN, color)))


def _pawn_islands(files: tuple[int, ...]) -> int:
    distinct = sorted(set(files))
    return sum(index == 0 or file_index != distinct[index - 1] + 1 for index, file_index in enumerate(distinct))


def _passed_pawns(board: chess.Board, color: chess.Color) -> tuple[int, ...]:
    enemy = board.pieces(chess.PAWN, not color)
    passed: list[int] = []
    for square in board.pieces(chess.PAWN, color):
        file_index = chess.square_file(square)
        rank = chess.square_rank(square)
        blocked = False
        for enemy_square in enemy:
            enemy_file = chess.square_file(enemy_square)
            enemy_rank = chess.square_rank(enemy_square)
            if abs(enemy_file - file_index) > 1:
                continue
            if (color == chess.WHITE and enemy_rank > rank) or (
                color == chess.BLACK and enemy_rank < rank
            ):
                blocked = True
                break
        if not blocked:
            passed.append(square)
    return tuple(sorted(passed))


def _passed_advance(square: int, color: chess.Color) -> int:
    rank = chess.square_rank(square)
    return rank - 1 if color == chess.WHITE else 6 - rank


def _connected_passed(passed: tuple[int, ...]) -> int:
    files = {chess.square_file(square) for square in passed}
    return sum(
        file_index - 1 in files or file_index + 1 in files
        for file_index in files
    )


def _rook_open_files(board: chess.Board, color: chess.Color) -> int:
    pawn_files = {
        chess.square_file(square)
        for pawn_color in chess.COLORS
        for square in board.pieces(chess.PAWN, pawn_color)
    }
    return sum(
        chess.square_file(square) not in pawn_files
        for square in board.pieces(chess.ROOK, color)
    )


def _rook_seventh(board: chess.Board, color: chess.Color) -> int:
    target = 6 if color == chess.WHITE else 1
    return sum(
        chess.square_rank(square) == target
        for square in board.pieces(chess.ROOK, color)
    )


def _king_shelter(board: chess.Board, color: chess.Color) -> int:
    king = board.king(color)
    if king is None:
        return 0
    king_file = chess.square_file(king)
    king_rank = chess.square_rank(king)
    direction = 1 if color == chess.WHITE else -1
    shield = 0
    for file_index in range(max(0, king_file - 1), min(7, king_file + 1) + 1):
        for distance, value in ((1, 2), (2, 1)):
            rank = king_rank + direction * distance
            if 0 <= rank <= 7 and board.piece_at(chess.square(file_index, rank)) == chess.Piece(chess.PAWN, color):
                shield += value
    return shield


def _material_under_attack(board: chess.Board, color: chess.Color) -> tuple[int, int, int, int]:
    attacked = 0
    hanging = 0
    pinned = 0
    queen_exposed = 0
    for piece_type in range(chess.PAWN, chess.KING):
        value = PIECE_VALUES[piece_type]
        for square in board.pieces(piece_type, color):
            attackers = board.attackers(not color, square)
            defenders = board.attackers(color, square)
            if attackers:
                attacked += value
                if not defenders:
                    hanging += value
                if piece_type == chess.QUEEN:
                    queen_exposed = 1
            if board.is_pinned(color, square):
                pinned += value
    return attacked, hanging, pinned, queen_exposed


def _capture_value(board: chess.Board, move: chess.Move) -> int:
    if board.is_en_passant(move):
        return PIECE_VALUES[chess.PAWN]
    victim = board.piece_at(move.to_square)
    return 0 if victim is None else PIECE_VALUES[victim.piece_type]


def _direct_and_two_move_threats(state: ProgressiveState) -> tuple[int, ...]:
    board = state.board.copy(stack=False)
    mover = board.turn
    sign = 1 if mover == chess.WHITE else -1
    direct_capture_count = 0
    direct_capture_value = 0
    direct_max_capture = 0
    direct_checks = 0
    direct_mates = 0
    direct_promotions = 0
    two_capture_value = 0
    two_check_routes = 0
    two_mate_routes = 0
    variants = tuple(_legal_move_variants(board, state.ep_targets))
    for move, required_ep in variants:
        board.ep_square = required_ep
        capture_value = _capture_value(board, move)
        if capture_value:
            direct_capture_count += 1
            direct_capture_value += capture_value
            direct_max_capture = max(direct_max_capture, capture_value)
        gives_check = board.gives_check(move)
        direct_checks += int(gives_check)
        direct_promotions += int(move.promotion is not None)
        child = board.copy(stack=False)
        child.push(move)
        if gives_check:
            direct_mates += int(child.is_checkmate())
            continue
        if state.moves_available < 2:
            continue
        child.turn = mover
        child.ep_square = None
        route_has_check = False
        route_has_mate = False
        route_capture = 0
        for reply, reply_required_ep in _legal_move_variants(child, ()):
            child.ep_square = reply_required_ep
            route_capture = max(route_capture, _capture_value(child, reply))
            if child.gives_check(reply):
                route_has_check = True
                grandchild = child.copy(stack=False)
                grandchild.push(reply)
                route_has_mate = route_has_mate or grandchild.is_checkmate()
        two_capture_value = max(two_capture_value, route_capture)
        two_check_routes += int(route_has_check)
        two_mate_routes += int(route_has_mate)
    board.ep_square = state.ep_targets[0] if len(state.ep_targets) == 1 else None
    return tuple(
        sign * value
        for value in (
            direct_capture_count,
            direct_capture_value,
            direct_max_capture,
            direct_checks,
            direct_mates,
            direct_promotions,
            two_capture_value,
            two_check_routes,
            two_mate_routes,
        )
    )


@dataclass(frozen=True, slots=True)
class TeacherValueFeaturesV3:
    values: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.values) != len(TEACHER_VALUE_FEATURE_NAMES):
            raise ValueError(
                "teacher-value feature vector must contain exactly "
                f"{len(TEACHER_VALUE_FEATURE_NAMES)} values"
            )
        if any(type(value) is not int for value in self.values):
            raise TypeError("teacher-value features must be exact integers")

    @classmethod
    def from_state_and_cached(
        cls,
        state: ProgressiveState,
        cached: Mapping[str, Any],
    ) -> TeacherValueFeaturesV3:
        base = tuple(int(cached[name]) for name in FEATURE_NAMES)
        centered_phase = min(state.series_number, 10) - 4
        cached_context = (
            int(cached["white_king_ring_attack_multiplicity"])
            - int(cached["black_king_ring_attack_multiplicity"]),
            int(cached["white_promotable_next_series"])
            - int(cached["black_promotable_next_series"]),
            int(cached["black_king_edge_distance"])
            - int(cached["white_king_edge_distance"]),
            _route_value(cached["white_check_distance"])
            - _route_value(cached["black_check_distance"]),
            int(bool(cached["reach_complete"])),
        )
        board = state.board
        center = (chess.D4, chess.E4, chess.D5, chess.E5)
        extended_center = tuple(
            chess.square(file_index, rank)
            for file_index in range(2, 6)
            for rank in range(2, 6)
        )
        white_files = _pawn_files(board, chess.WHITE)
        black_files = _pawn_files(board, chess.BLACK)
        white_passed = _passed_pawns(board, chess.WHITE)
        black_passed = _passed_pawns(board, chess.BLACK)
        white_attack = _material_under_attack(board, chess.WHITE)
        black_attack = _material_under_attack(board, chess.BLACK)
        board_context = (
            _minor_development(board, chess.WHITE)
            - _minor_development(board, chess.BLACK),
            sum(
                (1 if piece.color == chess.WHITE else -1)
                * (2 if piece.piece_type == chess.PAWN else 1)
                for square in center
                if (piece := board.piece_at(square)) is not None
            ),
            _control(board, chess.WHITE, center)
            - _control(board, chess.BLACK, center),
            _control(board, chess.WHITE, extended_center)
            - _control(board, chess.BLACK, extended_center),
            sum(chess.square_rank(square) - 1 for square in board.pieces(chess.PAWN, chess.WHITE))
            - sum(6 - chess.square_rank(square) for square in board.pieces(chess.PAWN, chess.BLACK)),
            len(white_passed) - len(black_passed),
            sum(_passed_advance(square, chess.WHITE) for square in white_passed)
            - sum(_passed_advance(square, chess.BLACK) for square in black_passed),
            _connected_passed(white_passed) - _connected_passed(black_passed),
            -sum(
                white_files.count(file_index)
                for file_index in set(white_files)
                if file_index - 1 not in white_files
                and file_index + 1 not in white_files
            )
            + sum(
                black_files.count(file_index)
                for file_index in set(black_files)
                if file_index - 1 not in black_files
                and file_index + 1 not in black_files
            ),
            sum(max(0, white_files.count(file_index) - 1) for file_index in set(white_files))
            * -1
            + sum(max(0, black_files.count(file_index) - 1) for file_index in set(black_files)),
            -_pawn_islands(white_files) + _pawn_islands(black_files),
            int(len(board.pieces(chess.BISHOP, chess.WHITE)) >= 2)
            - int(len(board.pieces(chess.BISHOP, chess.BLACK)) >= 2),
            _rook_open_files(board, chess.WHITE) - _rook_open_files(board, chess.BLACK),
            _rook_seventh(board, chess.WHITE) - _rook_seventh(board, chess.BLACK),
            _king_shelter(board, chess.WHITE) - _king_shelter(board, chess.BLACK),
            black_attack[0] - white_attack[0],
            black_attack[1] - white_attack[1],
            black_attack[2] - white_attack[2],
            black_attack[3] - white_attack[3],
        )
        return cls(
            base
            + tuple(value * centered_phase for value in base)
            + cached_context
            + board_context
            + _direct_and_two_move_threats(state)
        )

    @classmethod
    def from_pfen_and_cached(
        cls,
        pfen: str,
        cached: Mapping[str, Any],
    ) -> TeacherValueFeaturesV3:
        return cls.from_state_and_cached(state_from_pfen(pfen), cached)

    def as_dict(self) -> dict[str, int]:
        return dict(zip(TEACHER_VALUE_FEATURE_NAMES, self.values, strict=True))
