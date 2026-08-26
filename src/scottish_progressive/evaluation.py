from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import os
from pathlib import Path

import chess

from .model import ProgressiveState
from .profiles import EngineProfile, EvaluationWeights
from .rules import _board_position_key, _legal_move_variants


_NATIVE_SOURCE_FILES = (
    "_native_eval.cpp",
    "native_eval.hpp",
    "native_subtree.cpp",
    "native_subtree.hpp",
    "native_selfplay.cpp",
    "native_selfplay.hpp",
)


def _native_source_identity() -> str | None:
    """Digest the packaged sources that an accepted extension must contain."""

    package = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    try:
        for filename in _NATIVE_SOURCE_FILES:
            digest.update(filename.encode("utf-8"))
            digest.update(
                (package / filename)
                .read_bytes()
                .replace(b"\r\n", b"\n")
                .replace(b"\r", b"\n")
            )
    except OSError:
        return None
    return digest.hexdigest()


def _validated_native_module(candidate: object | None) -> object | None:
    expected = _native_source_identity()
    if expected is None or getattr(candidate, "SOURCE_IDENTITY", None) != expected:
        return None
    return candidate


if os.environ.get("SPC_DISABLE_NATIVE") == "1":
    _native_eval = None
else:
    try:
        from . import _native_eval as _native_candidate
    except ImportError:  # A source checkout or unsupported wheel keeps the oracle path.
        _native_candidate = None
    _native_eval = _validated_native_module(_native_candidate)
    del _native_candidate


PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 325,
    chess.BISHOP: 340,
    chess.ROOK: 525,
    chess.QUEEN: 975,
    chess.KING: 0,
}
_NATIVE_INT64_MAX = (1 << 63) - 1
# EvaluationWeights.scale() is the public Python oracle and divides through a
# binary float. Keeping every pre-division integer within 53 bits guarantees
# the C++ integer implementation cannot diverge from that established result.
_NATIVE_EXACT_FLOAT_INTEGER_MAX = 1 << 53
_NATIVE_WEIGHT_MIN = 25
_NATIVE_WEIGHT_MAX = 300
_CHECK_REACH_POSITION_LIMIT = 256
_CAPTURE_REACH_POSITION_LIMIT = 256
_EVALUATION_PROBE_POSITION_LIMIT = (
    _CHECK_REACH_POSITION_LIMIT + _CAPTURE_REACH_POSITION_LIMIT
)


@dataclass(frozen=True, slots=True)
class ReachProbe:
    distance: int | None
    nodes: int
    complete: bool


@dataclass(frozen=True, slots=True)
class _CaptureReach:
    value: int
    targets: frozenset[int]
    positions: int
    complete: bool


@dataclass(frozen=True, slots=True)
class EvaluationBreakdown:
    total: int
    material: int
    king_space: int
    series_reach: int
    promotion_corridors: int
    immediate_vulnerability: int
    useful_mobility: int
    boundary_check: int
    white_check_distance: int | None
    black_check_distance: int | None
    reach_complete: bool
    white_reach_nodes: int = 0
    black_reach_nodes: int = 0
    capture_reach_positions: int = 0
    capture_reach_complete: bool = True
    tactical_unstable: bool = False

    def as_dict(self) -> dict[str, int | bool | None]:
        return asdict(self)


def _board_from_key(key: str) -> chess.Board:
    placement, turn, castling, ep = key.split()
    orthodox_ep = ep if "," not in ep else "-"
    return chess.Board(f"{placement} {turn} {castling} {orthodox_ep} 0 1")


def _ordered_variants(
    board: chess.Board, ep_targets: tuple[int, ...]
) -> list[tuple[chess.Move, int | None]]:
    variants = _legal_move_variants(board, ep_targets)

    def rank(item: tuple[chess.Move, int | None]) -> tuple[int, int, int, str]:
        move, required_ep = item
        board.ep_square = required_ep
        gives_check = board.gives_check(move)
        capture = board.is_capture(move)
        board.ep_square = None
        return (
            0 if gives_check else 1,
            0 if move.promotion else 1,
            0 if capture else 1,
            move.uci(),
        )

    return sorted(variants, key=rank)


@lru_cache(maxsize=100_000)
def _probe_shortest_check_cached(
    key: str,
    ep_targets: tuple[int, ...],
    color: chess.Color,
    max_moves: int,
    node_limit: int,
) -> ReachProbe:
    board = _board_from_key(key)
    enemy_king = board.king(not color)
    if enemy_king is None:
        return ReachProbe(0, 0, True)
    if board.is_attacked_by(color, enemy_king):
        return ReachProbe(0, 0, True)

    board.turn = color
    frontier = [board]
    seen = {_board_position_key(board, ep_targets)}
    nodes = 0
    for distance in range(1, max_moves + 1):
        following: list[chess.Board] = []
        for position in frontier:
            first = distance == 1
            for move, required_ep in _ordered_variants(
                position, ep_targets if first else ()
            ):
                nodes += 1
                if nodes > node_limit:
                    return ReachProbe(None, nodes - 1, False)
                child = position.copy(stack=False)
                child.ep_square = required_ep
                child.push(move)
                if child.is_check():
                    return ReachProbe(distance, nodes, True)
                child.turn = color
                child.ep_square = None
                child_key = _board_position_key(child)
                if child_key not in seen:
                    seen.add(child_key)
                    following.append(child)
        frontier = following
        if not frontier:
            break
    return ReachProbe(None, nodes, True)


def probe_series_reach(
    state: ProgressiveState,
    color: chess.Color,
    *,
    max_moves: int | None = None,
    node_limit: int = 128,
) -> ReachProbe:
    """Finds a bounded shortest same-side sequence that gives check.

    This is an explicit tactical-reach feature, not a proof of a forced check:
    the opponent does not reply inside a Scottish series. ``complete=False``
    says the deterministic node budget was exhausted before the bounded search
    finished.
    """

    if max_moves is None:
        max_moves = state.series_number if state.board.turn == color else state.series_number + 1
    max_moves = max(0, min(max_moves, 3))
    return _probe_shortest_check_cached(
        state.boundary_key,
        state.ep_targets if state.board.turn == color else (),
        color,
        max_moves,
        node_limit,
    )


def _material(board: chess.Board) -> int:
    score = 0
    for piece_type, value in PIECE_VALUES.items():
        score += len(board.pieces(piece_type, chess.WHITE)) * value
        score -= len(board.pieces(piece_type, chess.BLACK)) * value
    return score


def _king_flight_squares(board: chess.Board, color: chess.Color) -> int:
    king = board.king(color)
    if king is None:
        return 0
    friendly = board.occupied_co[color]
    count = 0
    for target in chess.SquareSet(chess.BB_KING_ATTACKS[king]):
        if friendly & chess.BB_SQUARES[target]:
            continue
        trial = board.copy(stack=False)
        trial.turn = color
        trial.ep_square = None
        move = chess.Move(king, target)
        if move in trial.legal_moves:
            count += 1
    return count


def _promotion_distance(
    board: chess.Board, square: int, color: chess.Color
) -> int | None:
    rank = chess.square_rank(square)
    file_index = chess.square_file(square)
    direction = 1 if color == chess.WHITE else -1
    target_rank = 7 if color == chess.WHITE else 0
    distance = abs(target_rank - rank)
    if distance == 0:
        return 0
    for next_rank in range(rank + direction, target_rank + direction, direction):
        if board.piece_at(chess.square(file_index, next_rank)) is not None:
            return None
    start_rank = 1 if color == chess.WHITE else 6
    if rank == start_rank and distance >= 2:
        distance -= 1
    return distance


def _promotion_score(
    state: ProgressiveState,
    color: chess.Color,
) -> int:
    budget = state.series_number if state.board.turn == color else state.series_number + 1
    best: int | None = None
    for square in state.board.pieces(chess.PAWN, color):
        distance = _promotion_distance(state.board, square, color)
        if distance is not None and (best is None or distance < best):
            best = distance
    if best is None:
        return 0
    if best <= budget:
        # A large late-game series budget should establish that promotion is
        # possible, not grow this one heuristic without bound.
        return 650 + min(4, budget - best) * 55
    return max(0, 180 - (best - budget) * 45)


def _direct_capture_targets(
    board: chess.Board,
    victim: chess.Color,
    *,
    validate_check_evasions: bool,
) -> set[int]:
    """Returns occupied targets capturable without enumerating quiet moves."""

    attacker = not victim
    probe = board.copy(stack=False)
    probe.turn = attacker
    probe.ep_square = None
    probe.castling_rights = chess.BB_EMPTY
    attacker_in_check = probe.is_check()
    captured: set[int] = set()
    for target in chess.scan_forward(board.occupied_co[victim]):
        for source in chess.scan_forward(int(board.attackers(attacker, target))):
            source_piece = board.piece_at(source)
            if source_piece is None:
                continue
            requires_legal_replay = (
                (validate_check_evasions and attacker_in_check)
                or source_piece.piece_type == chess.KING
                or probe.is_pinned(attacker, source)
            )
            if not requires_legal_replay:
                captured.add(target)
                break
            promotion = (
                chess.QUEEN
                if source_piece.piece_type == chess.PAWN
                and chess.square_rank(target) in (0, 7)
                else None
            )
            if probe.is_legal(chess.Move(source, target, promotion=promotion)):
                captured.add(target)
                break
    return captured


def _attacked_material(board: chess.Board, victim: chess.Color) -> int:
    """Values distinct pieces with a legal one-move capture against them."""

    attacker = not victim
    probe = board.copy(stack=False)
    probe.turn = attacker
    probe.ep_square = None
    probe.castling_rights = chess.BB_EMPTY
    # At a checked boundary this is only an ordering proxy for the following
    # complete series. Requiring every pseudo-capture to be the first evasion
    # erased forcing routes that the exact search subsequently proves. Keep
    # the historical attack-map signal there; away from check, enforce king
    # safety so absolute pins cannot invent material.
    if probe.is_check():
        return sum(
            PIECE_VALUES[piece.piece_type]
            for square in chess.scan_forward(board.occupied_co[victim])
            if (piece := board.piece_at(square)) is not None
            and board.is_attacked_by(attacker, square)
        )
    return sum(
        PIECE_VALUES[piece.piece_type]
        for square in _direct_capture_targets(
            board,
            victim,
            validate_check_evasions=False,
        )
        if (piece := board.piece_at(square)) is not None
    )


def _immediate_capture_reach(state: ProgressiveState) -> _CaptureReach:
    """Finds distinct pieces the actual mover can legally capture now."""

    board = state.board.copy(stack=False)
    captured = _direct_capture_targets(
        board,
        not board.turn,
        validate_check_evasions=True,
    )
    for ep_target in state.ep_targets:
        board.ep_square = ep_target
        if any(board.generate_legal_ep()):
            captured.add(ep_target + (-8 if board.turn == chess.WHITE else 8))
    value = sum(
        PIECE_VALUES[piece.piece_type]
        for square in captured
        if (piece := state.board.piece_at(square)) is not None
    )
    return _CaptureReach(value, frozenset(captured), 0, True)


def _immediately_capturable_material(state: ProgressiveState) -> int:
    """Values distinct pieces the actual mover can legally capture now."""

    return _immediate_capture_reach(state).value


def _two_move_capture_reach(
    state: ProgressiveState,
    *,
    position_limit: int = _CAPTURE_REACH_POSITION_LIMIT,
) -> _CaptureReach:
    """Finds captures reachable on move two of the current series.

    A Scottish player can use a quiet first micro-move (including a forced
    king evasion) to expose a capture that an orthodox one-move attack map
    cannot see. A checking first move is excluded because it ends the series.
    """

    if state.moves_available < 2:
        return _CaptureReach(0, frozenset(), 0, True)
    position_limit = max(0, position_limit)
    board = state.board.copy(stack=False)
    mover = board.turn
    best = 0
    targets: set[int] = set()
    first_variants = _legal_move_variants(board, state.ep_targets)
    first_count = len(first_variants)
    if first_count > position_limit:
        return _CaptureReach(0, frozenset(), position_limit, False)
    # Each legal first-move child is one metered probe position.  Capture
    # targets in that child come from the legal attack map directly; there is
    # no reason to enumerate every quiet second move merely to find captures.
    positions = first_count
    for first, required_ep in first_variants:
        board.ep_square = required_ep
        if board.gives_check(first):
            continue
        child = board.copy(stack=False)
        child.push(first)
        child.turn = mover
        child.ep_square = None
        child_targets = _direct_capture_targets(
            child,
            not mover,
            validate_check_evasions=True,
        )
        targets.update(child_targets)
        for target in child_targets:
            victim = child.piece_at(target)
            if victim is not None:
                best = max(best, PIECE_VALUES[victim.piece_type])
    return _CaptureReach(best, frozenset(targets), positions, True)


def _two_move_capture_value(state: ProgressiveState) -> int:
    """Returns the best capture reachable on move two of the current series."""

    return _two_move_capture_reach(state).value


def _promotable_pawn_is_reachable(
    state: ProgressiveState,
    targets: frozenset[int],
) -> bool:
    victim = not state.board.turn
    next_budget = state.series_number + 1
    for square in targets:
        piece = state.board.piece_at(square)
        if piece is None or piece.color != victim or piece.piece_type != chess.PAWN:
            continue
        distance = _promotion_distance(state.board, square, victim)
        if distance is not None and distance <= next_budget:
            return True
    return False


def _first_move_mobility(state: ProgressiveState, color: chess.Color) -> int:
    board = state.board.copy(stack=False)
    board.turn = color
    board.ep_square = None
    ep = state.ep_targets if color == state.board.turn else ()
    useful = 0
    for move, required_ep in _legal_move_variants(board, ep):
        board.ep_square = required_ep
        if board.gives_check(move) or board.is_capture(move) or move.promotion:
            useful += 3
        else:
            useful += 1
    return useful


def _reach_value(probe: ReachProbe, budget: int) -> int:
    if probe.distance is None:
        return 0
    if probe.distance == 0:
        return 260
    if probe.distance <= budget:
        return max(60, 230 - (probe.distance - 1) * 80)
    return 0


def _weights(profile: EngineProfile | EvaluationWeights | None) -> EvaluationWeights:
    if isinstance(profile, EngineProfile):
        return profile.weights
    return profile or EvaluationWeights()


def _python_evaluate(
    state: ProgressiveState,
    profile: EngineProfile | EvaluationWeights | None = None,
    *,
    max_reach_positions: int | None = None,
) -> EvaluationBreakdown:
    """Returns a White-centric progressive-specific heuristic score."""

    board = state.board
    weights = _weights(profile)
    material = weights.scale("material", _material(board))
    king_space = weights.scale("king_space", (
        _king_flight_squares(board, chess.WHITE)
        - _king_flight_squares(board, chess.BLACK)
    ) * 28)

    white_budget = state.series_number if board.turn == chess.WHITE else state.series_number + 1
    black_budget = state.series_number if board.turn == chess.BLACK else state.series_number + 1
    probe_remaining = (
        _EVALUATION_PROBE_POSITION_LIMIT
        if max_reach_positions is None
        else max(0, max_reach_positions)
    )
    if board.is_check():
        # The checked side must evade before either color can execute an
        # arbitrary same-side checking route.  Record the checker's direct
        # distance without paying for or scoring an unreachable continuation.
        checker = not board.turn
        white_reach = (
            ReachProbe(0, 0, True)
            if checker == chess.WHITE
            else ReachProbe(None, 0, True)
        )
        black_reach = (
            ReachProbe(0, 0, True)
            if checker == chess.BLACK
            else ReachProbe(None, 0, True)
        )
    else:
        white_reach = probe_series_reach(
            state,
            chess.WHITE,
            max_moves=min(2, white_budget),
            node_limit=min(128, probe_remaining),
        )
        probe_remaining = max(0, probe_remaining - white_reach.nodes)
        black_reach = probe_series_reach(
            state,
            chess.BLACK,
            max_moves=min(2, black_budget),
            node_limit=min(128, probe_remaining),
        )
        probe_remaining = max(0, probe_remaining - black_reach.nodes)
    # A failed bounded probe is unknown, not proof that a checking route does
    # not exist. Scoring one side's found route against the other side's
    # incomplete search created large move-order artifacts (notably 1.Na3 over
    # 1.e4 from the initial position). Retain the diagnostic distances but do
    # not turn an asymmetric unknown into evaluation points.
    if board.is_check():
        series_reach = 0
    elif white_reach.complete and black_reach.complete:
        series_reach = weights.scale(
            "series_reach",
            _reach_value(white_reach, white_budget) - _reach_value(
                black_reach, black_budget
            ),
        )
    else:
        series_reach = 0

    immediate_capture = _immediate_capture_reach(state)
    low_material = chess.popcount(board.occupied) <= 10
    capture_limit = (
        _CAPTURE_REACH_POSITION_LIMIT
        if low_material and probe_remaining >= _CAPTURE_REACH_POSITION_LIMIT
        else 0
    )
    two_move_capture = (
        _two_move_capture_reach(
            state,
            position_limit=capture_limit,
        )
        if low_material
        else _CaptureReach(0, frozenset(), 0, True)
    )
    promotion_corridors = weights.scale(
        "promotion_corridors",
        _promotion_score(
            state,
            chess.WHITE,
        )
        - _promotion_score(
            state,
            chess.BLACK,
        ),
    )
    # In low-material positions, value the strongest capture available in the
    # mover's first two micro-moves. ``max`` preserves the established direct
    # attacked-material signal without perturbing ordinary opening ordering.
    capturable_material = max(
        immediate_capture.value,
        two_move_capture.value if two_move_capture.complete else 0,
    )
    vulnerability_raw = (
        capturable_material if board.turn == chess.WHITE else -capturable_material
    )
    immediate_vulnerability = weights.scale(
        "immediate_vulnerability",
        vulnerability_raw,
    )
    useful_mobility = weights.scale(
        "useful_mobility",
        (
            _first_move_mobility(state, chess.WHITE)
            - _first_move_mobility(state, chess.BLACK)
        )
        * 2,
    )

    boundary_check = 0
    if board.is_check():
        boundary_check = weights.scale(
            "boundary_check", 170 if board.turn == chess.BLACK else -170
        )

    capture_targets = immediate_capture.targets | two_move_capture.targets
    # Ordinary capture swings are already represented by the bounded
    # two-move vulnerability term above.  Extend only when a capture route and
    # a promotion corridor are mutually exclusive: a single static score
    # cannot safely combine those alternatives.  Full-series continuation is
    # confined to low-material promotion races, where branching is bounded;
    # ordinary middlegames retain the metered static signal.
    tactical_unstable = (
        low_material and _promotable_pawn_is_reachable(state, capture_targets)
    )

    total = (
        material
        + king_space
        + series_reach
        + promotion_corridors
        + immediate_vulnerability
        + useful_mobility
        + boundary_check
    )
    return EvaluationBreakdown(
        total=total,
        material=material,
        king_space=king_space,
        series_reach=series_reach,
        promotion_corridors=promotion_corridors,
        immediate_vulnerability=immediate_vulnerability,
        useful_mobility=useful_mobility,
        boundary_check=boundary_check,
        white_check_distance=white_reach.distance,
        black_check_distance=black_reach.distance,
        reach_complete=white_reach.complete and black_reach.complete,
        white_reach_nodes=white_reach.nodes,
        black_reach_nodes=black_reach.nodes,
        capture_reach_positions=two_move_capture.positions,
        capture_reach_complete=two_move_capture.complete,
        tactical_unstable=tactical_unstable,
    )


def _native_full_evaluation_is_safe(
    state: ProgressiveState,
    weights: EvaluationWeights,
    max_reach_positions: int | None,
) -> bool:
    """Whether the exact full evaluator can preserve Python's contract."""

    if (
        _native_eval is None
        or not hasattr(_native_eval, "full_evaluate")
        or state.board.chess960
        or type(weights) is not EvaluationWeights
        or not _native_fast_evaluation_is_safe(state, weights)
        or any(
            type(value) is not int or not _NATIVE_WEIGHT_MIN <= value <= _NATIVE_WEIGHT_MAX
            for value in (weights.series_reach, weights.useful_mobility)
        )
    ):
        return False
    if max_reach_positions is None:
        return True
    return (
        type(max_reach_positions) is int
        and -(1 << 64) < max_reach_positions <= (1 << 64) - 1
    )


def evaluate(
    state: ProgressiveState,
    profile: EngineProfile | EvaluationWeights | None = None,
    *,
    max_reach_positions: int | None = None,
) -> EvaluationBreakdown:
    """Returns the exact full evaluation through native code when supported."""

    weights = _weights(profile)
    if not _native_full_evaluation_is_safe(
        state,
        weights,
        max_reach_positions,
    ):
        return _python_evaluate(
            state,
            profile,
            max_reach_positions=max_reach_positions,
        )

    board = state.board
    reach_limit = (
        _EVALUATION_PROBE_POSITION_LIMIT
        if max_reach_positions is None
        else max(0, max_reach_positions)
    )
    try:
        raw = tuple(
            _native_eval.full_evaluate(
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
                board.turn,
                state.series_number,
                state.ep_targets,
                reach_limit,
                weights.material,
                weights.king_space,
                weights.series_reach,
                weights.promotion_corridors,
                weights.immediate_vulnerability,
                weights.useful_mobility,
                weights.boundary_check,
            )
        )
        if len(raw) != len(EvaluationBreakdown.__dataclass_fields__):
            raise ValueError("native full-evaluation result shape mismatch")
        return EvaluationBreakdown(*raw)
    except (OverflowError, TypeError, ValueError):
        # The Python evaluator is deliberately unbounded and remains the
        # authority for hostile inputs or future terms outside the native ABI.
        return _python_evaluate(
            state,
            weights,
            max_reach_positions=max_reach_positions,
        )


def _python_fast_evaluate(
    state: ProgressiveState,
    profile: EngineProfile | EvaluationWeights | None = None,
) -> int:
    """Cheap White-centric score used only for deterministic move ordering.

    It deliberately omits the bounded reach search and first-move mobility.
    Full leaf values always use :func:`evaluate`.
    """

    board = state.board
    weights = _weights(profile)
    score = weights.scale("material", _material(board))
    score += weights.scale(
        "king_space",
        (
            _king_flight_squares(board, chess.WHITE)
            - _king_flight_squares(board, chess.BLACK)
        )
        * 20,
    )
    score += weights.scale(
        "promotion_corridors",
        _promotion_score(state, chess.WHITE) - _promotion_score(state, chess.BLACK),
    )
    score += weights.scale(
        "immediate_vulnerability",
        (
            _attacked_material(board, chess.BLACK)
            - _attacked_material(board, chess.WHITE)
        )
        // 6,
    )
    if board.is_check():
        score += weights.scale(
            "boundary_check", 140 if board.turn == chess.BLACK else -140
        )
    return score


def native_acceleration_available() -> bool:
    """Whether this runtime loaded the optional C++20 ordering evaluator."""

    return _native_eval is not None


def _native_fast_evaluation_is_safe(
    state: ProgressiveState,
    weights: EvaluationWeights,
) -> bool:
    """Conservatively bounds every signed 64-bit native arithmetic term.

    Progressive series numbers are arbitrary Python integers. Falling back is
    an acceleration choice only; it never limits the legal series budget.
    """

    series_number = state.series_number
    native_weights = (
        weights.material,
        weights.king_space,
        weights.promotion_corridors,
        weights.immediate_vulnerability,
        weights.boundary_check,
    )
    if (
        not isinstance(series_number, int)
        or not 1 <= series_number < _NATIVE_INT64_MAX
        or any(
            not isinstance(weight, int)
            or not _NATIVE_WEIGHT_MIN <= weight <= _NATIVE_WEIGHT_MAX
            for weight in native_weights
        )
    ):
        return False

    # Board material, flight squares, attacked material, and check are bounded
    # by 64 squares. Only the progressive promotion budget grows with series.
    max_promotion_score = 650 + 4 * 55
    raw_bounds = (
        64 * PIECE_VALUES[chess.QUEEN],
        8 * 20,
        max_promotion_score,
        64 * PIECE_VALUES[chess.QUEEN],
        140,
    )
    products = [
        raw_bound * weight
        for raw_bound, weight in zip(raw_bounds, native_weights, strict=True)
    ]
    if any(
        product > _NATIVE_INT64_MAX
        or product > _NATIVE_EXACT_FLOAT_INTEGER_MAX
        for product in products
    ):
        return False
    rounded_term_bound = sum((product + 50) // 100 for product in products)
    return rounded_term_bound <= _NATIVE_INT64_MAX


def fast_evaluate(
    state: ProgressiveState,
    profile: EngineProfile | EvaluationWeights | None = None,
) -> int:
    """Cheap deterministic ordering score with a Python oracle fallback."""

    if _native_eval is None:
        return _python_fast_evaluate(state, profile)
    board = state.board
    weights = _weights(profile)
    if not _native_fast_evaluation_is_safe(state, weights):
        return _python_fast_evaluate(state, weights)
    try:
        return _native_eval.fast_evaluate(
            board.pawns,
            board.knights,
            board.bishops,
            board.rooks,
            board.queens,
            board.kings,
            board.occupied_co[chess.WHITE],
            board.occupied_co[chess.BLACK],
            board.turn,
            state.series_number,
            weights.material,
            weights.king_space,
            weights.promotion_corridors,
            weights.immediate_vulnerability,
            weights.boundary_check,
        )
    except OverflowError:
        # The C++ API also checks every operation. Keep the unbounded Python
        # contract if a future evaluation term exceeds this conservative gate.
        return _python_fast_evaluate(state, weights)


def classify_score(score: int, *, forced: str | None = None) -> str:
    if forced == "white":
        return "Forced Win"
    if forced == "black":
        return "Forced Loss"
    if forced == "draw":
        return "Drawn"
    if score >= 700:
        return "Likely Win"
    if score >= 250:
        return "Advantage"
    if score > 80:
        return "Slight Advantage"
    if score >= -80:
        return "Unclear"
    if score > -250:
        return "Slight Disadvantage"
    if score > -700:
        return "Disadvantage"
    return "Likely Loss"
