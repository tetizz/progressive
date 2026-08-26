from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Callable, Iterable

import chess

from .model import Outcome, ProgressiveState, SeriesResult


@dataclass(slots=True)
class GenerationStats:
    positions_visited: int = 0
    frontier_score_positions: int = 0
    raw_series: int = 0
    unique_series: int = 0
    transpositions_merged: int = 0
    checking_series: int = 0
    checkmates: int = 0
    stalemates: int = 0
    frontier_prunes: int = 0
    frontier_states_pruned: int = 0
    frontier_paths_pruned: int = 0
    tactical_frontier_states_retained: int = 0
    tactical_frontier_reserve_drops: int = 0
    tactical_final_series_retained: int = 0
    tactical_final_reserve_drops: int = 0
    peak_frontier_states: int = 0
    required_prefix_moves: int = 0
    work_limit_reached: bool = False


class SeriesLegalityError(ValueError):
    pass


class GenerationCancelled(Exception):
    pass


class GenerationWorkLimit(GenerationCancelled):
    """Raised when a deterministic generation-position budget is exhausted."""


def _board_position_key(
    board: chess.Board,
    ep_targets: Iterable[int] = (),
) -> tuple[object, ...]:
    """Exact in-memory equivalent of the rule-relevant boundary FEN.

    Hot search paths only need equality, not a printable FEN. The six piece
    bitboards, color occupancy, turn, cleaned castling rights, and explicit
    progressive e.p. targets capture the same rule state without repeatedly
    serializing all 64 squares. Move clocks and ``board.ep_square`` are
    deliberately excluded, matching ``boundary_fen(board, ep_targets)``.
    """

    return (
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
        board.occupied_co[chess.WHITE],
        board.occupied_co[chess.BLACK],
        board.turn,
        board.clean_castling_rights(),
        tuple(sorted(ep_targets)),
    )


def _progressive_position_key(state: ProgressiveState) -> tuple[object, ...]:
    """Exact equality key for one in-memory progressive boundary state."""

    return (
        _board_position_key(state.board, state.ep_targets),
        state.series_number,
        state.quiet_series,
    )


FrontierScore = Callable[[chess.Board], int]
TacticalOpportunity = tuple[int, str]

_TACTICAL_MATE = 0
_TACTICAL_CHECK = 1
_TACTICAL_PROMOTION = 2
_TACTICAL_CAPTURE = 3
# The configured frontier cap remains the ordinary/quiet beam.  Search may
# retain at most twice the ordinary cap, up to this many additional visible
# tactical opportunity representatives. Thus cap-32 search has a hard
# intermediate width of 96; cap 1 can grow only to 3, and larger caps add at
# most 64. If more
# distinct opportunity representatives exist, the ordinary pruning counters
# remain nonzero and proof bounds stay UNKNOWN/selective.
TACTICAL_FRONTIER_RESERVE_MAX = 64


@dataclass(frozen=True, slots=True)
class NativeFrontierScoreConfig:
    """Structured form of the exact bounded-frontier ordering heuristic.

    Arbitrary Python scorers remain supported by the oracle generator. This
    marker exposes only the fixed fast-evaluation plus tactical formula that
    the native complete-series kernel can reproduce without Python callbacks.
    """

    series_number: int
    quiet_series: int
    material: int
    king_space: int
    promotion_corridors: int
    immediate_vulnerability: int
    boundary_check: int
    tactical_protection: bool = False

    @classmethod
    def from_profile(
        cls,
        state: ProgressiveState,
        profile: object,
        *,
        tactical_protection: bool = False,
    ) -> "NativeFrontierScoreConfig":
        weights = getattr(profile, "weights")
        return cls(
            state.series_number,
            state.quiet_series,
            weights.material,
            weights.king_space,
            weights.promotion_corridors,
            weights.immediate_vulnerability,
            weights.boundary_check,
            tactical_protection,
        )

    def inspect(
        self,
        board: chess.Board,
    ) -> tuple[int, tuple[TacticalOpportunity, ...]]:
        """Returns the ordering score and visible forcing opportunities.

        Opportunity keys deliberately include the move UCI.  A global
        ``has-capture`` bit is too coarse in Progressive Chess: hundreds of
        partial positions can all attack the same loose piece while the one
        pawn capture that opens a promotion route sits outside a width-32
        static beam.  Keeping one deterministic representative per visible
        tactical move gives those distinct routes a bounded lane without
        pretending that the selective search became exhaustive.
        """

        from .evaluation import fast_evaluate
        from .profiles import EvaluationWeights

        partial = ProgressiveState(
            board,
            self.series_number,
            quiet_series=self.quiet_series,
        )
        weights = EvaluationWeights(
            material=self.material,
            king_space=self.king_space,
            promotion_corridors=self.promotion_corridors,
            immediate_vulnerability=self.immediate_vulnerability,
            boundary_check=self.boundary_check,
        )
        score = fast_evaluate(partial, weights)
        checks = 0
        immediate_mates = 0
        captures = 0
        promotions = 0
        opportunities: set[TacticalOpportunity] = set()
        saved_ep = board.ep_square
        try:
            for move, required_ep in _legal_move_variants(board):
                board.ep_square = required_ep
                uci = move.uci()
                gives_check = board.gives_check(move)
                is_capture = board.is_capture(move)
                is_promotion = move.promotion is not None
                checks += int(gives_check)
                captures += int(is_capture)
                promotions += int(is_promotion)
                if gives_check:
                    child = board.copy(stack=False)
                    child.push(move)
                    is_mate = child.is_checkmate()
                    immediate_mates += int(is_mate)
                    opportunities.add(
                        (
                            _TACTICAL_MATE if is_mate else _TACTICAL_CHECK,
                            uci,
                        )
                    )
                if is_promotion:
                    opportunities.add((_TACTICAL_PROMOTION, uci))
                if is_capture:
                    opportunities.add((_TACTICAL_CAPTURE, uci))
        finally:
            board.ep_square = saved_ep
        tactical = (
            immediate_mates * 5_000_000
            + checks * 50_000
            + promotions * 2_000
            + captures * 100
        )
        score += tactical if board.turn == chess.WHITE else -tactical
        return score, tuple(sorted(opportunities))

    def __call__(self, board: chess.Board) -> int:
        return self.inspect(board)[0]


@dataclass(frozen=True, slots=True)
class NativeFinalSeriesScoreConfig:
    """Internal search hint for native static ordering and return capping.

    The Python rules oracle deliberately ignores this hint and still returns
    every merged result. Search may opt in only when the source-matched bulk
    kernel can reproduce its exact terminal/fast-evaluation ordering.
    """

    max_returned_series: int
    ply_from_root: int
    mate_score: int
    material: int
    king_space: int
    promotion_corridors: int
    immediate_vulnerability: int
    boundary_check: int

    def __post_init__(self) -> None:
        if self.max_returned_series < 1:
            raise ValueError("max_returned_series must be positive")
        if self.ply_from_root < 0:
            raise ValueError("ply_from_root cannot be negative")
        if self.mate_score < 1:
            raise ValueError("mate_score must be positive")

    @classmethod
    def from_profile(
        cls,
        profile: object,
        *,
        max_returned_series: int,
        ply_from_root: int,
        mate_score: int,
    ) -> "NativeFinalSeriesScoreConfig":
        weights = getattr(profile, "weights")
        return cls(
            max_returned_series,
            ply_from_root,
            mate_score,
            weights.material,
            weights.king_space,
            weights.promotion_corridors,
            weights.immediate_vulnerability,
            weights.boundary_check,
        )


@dataclass(slots=True)
class _FrontierState:
    board: chess.Board
    moves: tuple[str, ...]
    sans: tuple[str, ...]
    ep_candidates: dict[int, int]
    made_progress: bool
    path_count: int = 1
    # Capture/promotion opportunities remain protected after they are played,
    # through the rest of the same series.  The signature is category plus
    # one-based micro-ply and UCI (for example ``(3, "4:a4b3")``).
    tactical_provenance: tuple[TacticalOpportunity, ...] = ()


class _TacticalSeriesResults(list[SeriesResult]):
    """Merged results plus path-unioned tactical evidence for final capping."""

    __slots__ = ("tactical_provenance_by_notation",)

    def __init__(
        self,
        values: list[SeriesResult],
        tactical_provenance_by_notation: dict[
            str,
            tuple[TacticalOpportunity, ...],
        ],
    ) -> None:
        super().__init__(values)
        self.tactical_provenance_by_notation = tactical_provenance_by_notation


def _merge_frontier_states(
    incumbent: _FrontierState,
    candidate: _FrontierState,
) -> _FrontierState:
    """Merges a partial transposition without losing path tactical evidence."""

    total_paths = incumbent.path_count + candidate.path_count
    chosen = candidate if candidate.moves < incumbent.moves else incumbent
    combined_tactical_provenance = tuple(
        sorted(
            set(incumbent.tactical_provenance)
            | set(candidate.tactical_provenance)
        )
    )
    return _FrontierState(
        chosen.board,
        chosen.moves,
        chosen.sans,
        chosen.ep_candidates,
        chosen.made_progress,
        total_paths,
        combined_tactical_provenance,
    )


@dataclass(slots=True)
class _ExpandedVariant:
    move: chess.Move
    required_ep: int | None
    board: chess.Board
    san: str
    is_pawn_move: bool
    is_capture: bool
    delivered_check: bool


def _visit_generation_position(
    counters: GenerationStats,
    max_positions: int | None,
) -> None:
    if (
        max_positions is not None
        and counters.positions_visited + counters.frontier_score_positions
        >= max_positions
    ):
        counters.work_limit_reached = True
        raise GenerationWorkLimit
    counters.positions_visited += 1


def _visit_frontier_score_position(
    counters: GenerationStats,
    max_positions: int | None,
) -> None:
    if (
        max_positions is not None
        and counters.positions_visited + counters.frontier_score_positions
        >= max_positions
    ):
        counters.work_limit_reached = True
        raise GenerationWorkLimit
    counters.frontier_score_positions += 1


def _frontier_order_key(
    item: _FrontierState,
    mover: chess.Color,
    frontier_score: FrontierScore | None,
) -> tuple[int, tuple[str, ...]]:
    score = frontier_score(item.board) if frontier_score is not None else 0
    return (-score if mover == chess.WHITE else score, item.moves)


def _bound_frontier(
    frontier: dict[tuple[object, ...], _FrontierState],
    *,
    mover: chess.Color,
    prefix_length: int,
    max_frontier_states: int | None,
    frontier_score: FrontierScore | None,
    tactical_opportunities: (
        Callable[[chess.Board], tuple[TacticalOpportunity, ...]] | None
    ),
    counters: GenerationStats,
) -> list[_FrontierState]:
    counters.peak_frontier_states = max(
        counters.peak_frontier_states,
        len(frontier),
    )
    if max_frontier_states is None:
        # Preserve the exact generator's historical traversal and cost when
        # no selective bound was requested.
        return list(frontier.values())
    ordered = sorted(
        frontier.values(),
        key=lambda item: _frontier_order_key(item, mover, frontier_score),
    )
    if len(ordered) <= max_frontier_states:
        return ordered

    # Keep deterministic root-choice diversity instead of letting one flashy
    # first micro-move consume the entire beam. At later layers the tactical
    # rank still decides which descendants of each first suffix move survive.
    groups: dict[str, list[_FrontierState]] = {}
    for item in ordered:
        group_index = min(prefix_length, len(item.moves) - 1)
        groups.setdefault(item.moves[group_index], []).append(item)
    quota = max(1, max_frontier_states // len(groups))
    selected = [
        item
        for group in groups.values()
        for item in group[:quota]
    ]
    selected.sort(key=lambda item: _frontier_order_key(item, mover, frontier_score))
    selected = selected[:max_frontier_states]
    selected_moves = {item.moves for item in selected}
    if len(selected) < max_frontier_states:
        for item in ordered:
            if item.moves in selected_moves:
                continue
            selected.append(item)
            selected_moves.add(item.moves)
            if len(selected) == max_frontier_states:
                break

    # The static beam is an ordering heuristic, not tactical authority.  Give
    # one best-scored position exposing each distinct mate/check/promotion/
    # capture move a small deterministic reserve.  The reserve is bounded and
    # does not claim a no-tactic proof when it fills: discarded states still
    # increment the ordinary pruning counters below.
    if tactical_opportunities is not None:
        ranked_representatives: dict[
            TacticalOpportunity,
            tuple[int, _FrontierState],
        ] = {}
        for rank, item in enumerate(ordered):
            opportunities = set(item.tactical_provenance)
            next_ply = len(item.moves) + 1
            opportunities.update(
                (kind, f"{next_ply}:{uci}")
                for kind, uci in tactical_opportunities(item.board)
            )
            for opportunity in sorted(opportunities):
                ranked_representatives.setdefault(opportunity, (rank, item))
        reserve_limit = min(
            max_frontier_states * 2,
            TACTICAL_FRONTIER_RESERVE_MAX,
        )
        extras = 0
        reserve_candidates: set[tuple[str, ...]] = set()
        protected = sorted(
            ranked_representatives.items(),
            key=lambda entry: (
                entry[0][0],
                entry[1][0],
                entry[0][1],
                entry[1][1].moves,
            ),
        )
        for _opportunity, (_rank, item) in protected:
            if item.moves in selected_moves:
                continue
            reserve_candidates.add(item.moves)
        for _opportunity, (_rank, item) in protected:
            if item.moves in selected_moves:
                continue
            selected.append(item)
            selected_moves.add(item.moves)
            extras += 1
            if extras == reserve_limit:
                break
        counters.tactical_frontier_states_retained += extras
        counters.tactical_frontier_reserve_drops += max(
            0,
            len(reserve_candidates) - extras,
        )
    selected.sort(key=lambda item: _frontier_order_key(item, mover, frontier_score))
    discarded = [item for item in ordered if item.moves not in selected_moves]
    counters.frontier_prunes += 1
    counters.frontier_states_pruned += len(discarded)
    counters.frontier_paths_pruned += sum(item.path_count for item in discarded)
    return selected


def _python_legal_move_variants(
    board: chess.Board, ep_targets: Iterable[int] = ()
) -> list[tuple[chess.Move, int | None]]:
    """Returns legal moves and the e.p. square needed to apply each move.

    Orthodox FEN can encode only one en-passant target. Progressive Chess can
    carry several targets out of one series, so first-move generation unions
    the legal en-passant captures for every target.
    """

    saved_ep = board.ep_square
    variants: dict[str, tuple[chess.Move, int | None]] = {}
    try:
        board.ep_square = None
        for move in board.legal_moves:
            variants[move.uci()] = (move, None)

        for target in sorted(set(ep_targets)):
            board.ep_square = target
            for move in board.legal_moves:
                if board.is_en_passant(move):
                    variants[move.uci()] = (move, target)
    finally:
        board.ep_square = saved_ep
    return [variants[key] for key in sorted(variants)]


def _native_legal_move_variants(
    board: chess.Board,
    ep_targets: tuple[int, ...],
) -> list[tuple[chess.Move, int | None]] | None:
    """Runs the source-matched native move generator when it is available.

    The optional extension is owned and identity-checked by ``evaluation``.
    Importing it lazily avoids a rules/evaluation import cycle and guarantees
    that ``SPC_DISABLE_NATIVE`` selects the Python oracle for both kernels.
    Chess960 is deliberately left to python-chess: Scottish research positions
    use orthodox castling, while the native kernel has an exact standard-chess
    castling contract.
    """

    if board.chess960:
        return None
    from . import evaluation

    native = evaluation._native_eval
    if native is None or not hasattr(native, "legal_move_variants"):
        return None
    raw = native.legal_move_variants(
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
        ep_targets,
    )
    return [
        (
            chess.Move(
                from_square,
                to_square,
                promotion=promotion or None,
            ),
            required_ep if required_ep >= 0 else None,
        )
        for _uci, from_square, to_square, promotion, required_ep in raw
    ]


def _legal_move_variants(
    board: chess.Board, ep_targets: Iterable[int] = ()
) -> list[tuple[chess.Move, int | None]]:
    """Returns canonical legal moves through native code or the Python oracle."""

    targets = tuple(sorted(set(ep_targets)))
    native = _native_legal_move_variants(board, targets)
    if native is not None:
        return native
    return _python_legal_move_variants(board, targets)


def _native_expanded_move_variants(
    board: chess.Board,
    ep_targets: tuple[int, ...],
) -> list[_ExpandedVariant] | None:
    if board.chess960:
        return None
    from . import evaluation

    native = evaluation._native_eval
    if native is None or not hasattr(native, "expand_legal_move_variants"):
        return None
    raw = native.expand_legal_move_variants(
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
        ep_targets,
    )
    expanded: list[_ExpandedVariant] = []
    saved_ep = board.ep_square
    try:
        for (
            _uci,
            from_square,
            to_square,
            promotion,
            required_ep,
            pawns,
            knights,
            bishops,
            rooks,
            queens,
            kings,
            white_occupied,
            black_occupied,
            promoted,
            castling_rights,
            is_pawn_move,
            is_capture,
            delivered_check,
        ) in raw:
            required_ep = required_ep if required_ep >= 0 else None
            move = chess.Move(
                from_square,
                to_square,
                promotion=promotion or None,
            )
            board.ep_square = required_ep
            san = board.san(move)
            current = board.copy(stack=False)
            current.pawns = pawns
            current.knights = knights
            current.bishops = bishops
            current.rooks = rooks
            current.queens = queens
            current.kings = kings
            current.occupied_co[chess.WHITE] = white_occupied
            current.occupied_co[chess.BLACK] = black_occupied
            current.occupied = white_occupied | black_occupied
            current.promoted = promoted
            current.castling_rights = castling_rights
            current.turn = not board.turn
            current.ep_square = (
                (move.from_square + move.to_square) // 2
                if is_pawn_move
                and abs(move.to_square - move.from_square) == 16
                else None
            )
            current.halfmove_clock = (
                0 if is_pawn_move or is_capture else board.halfmove_clock + 1
            )
            current.fullmove_number = board.fullmove_number + int(
                board.turn == chess.BLACK
            )
            expanded.append(
                _ExpandedVariant(
                    move,
                    required_ep,
                    current,
                    san,
                    bool(is_pawn_move),
                    bool(is_capture),
                    bool(delivered_check),
                )
            )
    finally:
        board.ep_square = saved_ep
    return expanded


def _expanded_move_variants(
    board: chess.Board,
    ep_targets: Iterable[int] = (),
) -> list[_ExpandedVariant]:
    """Returns legal moves with exact post-push boards and tactical flags."""

    targets = tuple(sorted(set(ep_targets)))
    native = _native_expanded_move_variants(board, targets)
    if native is not None:
        return native
    expanded: list[_ExpandedVariant] = []
    for move, required_ep in _python_legal_move_variants(board, targets):
        current = board.copy(stack=False)
        current.ep_square = required_ep
        san = current.san(move)
        piece = current.piece_at(move.from_square)
        is_pawn_move = piece is not None and piece.piece_type == chess.PAWN
        is_capture = current.is_capture(move)
        current.push(move)
        expanded.append(
            _ExpandedVariant(
                move,
                required_ep,
                current,
                san,
                is_pawn_move,
                is_capture,
                current.is_check(),
            )
        )
    return expanded


def _has_legal_move(board: chess.Board, ep_targets: Iterable[int] = ()) -> bool:
    targets = tuple(sorted(set(ep_targets)))
    if not board.chess960:
        from . import evaluation

        native = evaluation._native_eval
        if native is not None and hasattr(native, "has_legal_move"):
            return bool(
                native.has_legal_move(
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
                    targets,
                )
            )
    return bool(_python_legal_move_variants(board, targets))


def _series_outcome(
    board: chess.Board,
    ep_targets: tuple[int, ...],
    quiet_series: int,
    *,
    delivered_check: bool,
) -> Outcome | None:
    if delivered_check and not _has_legal_move(board, ep_targets):
        return Outcome.CHECKMATE
    if not delivered_check and not _has_legal_move(board, ep_targets):
        return Outcome.STALEMATE
    if quiet_series >= 10 and board.is_insufficient_material():
        # This orthodox predicate is a sound dead-material subset here: no
        # sequence of same-side moves can create mating material that does not
        # exist. More complicated dead positions remain proof-required.
        return Outcome.TEN_SERIES_DRAW
    return None


def _finish_series(
    root: ProgressiveState,
    board: chess.Board,
    moves: tuple[str, ...],
    sans: tuple[str, ...],
    ep_candidates: dict[int, int],
    made_progress: bool,
    *,
    delivered_check: bool,
) -> SeriesResult:
    targets = tuple(
        sorted(
            target
            for target in ep_candidates.values()
            if board.piece_at(target) is None
        )
    )
    board.ep_square = targets[0] if len(targets) == 1 else None
    quiet_series = 0 if made_progress else root.quiet_series + 1
    child = ProgressiveState(
        board=board,
        series_number=root.series_number + 1,
        quiet_series=quiet_series,
        ep_targets=targets,
    )
    outcome = _series_outcome(
        child.board,
        child.ep_targets,
        child.quiet_series,
        delivered_check=delivered_check,
    )
    if delivered_check and sans:
        # python-chess only knows its single orthodox en-passant square when it
        # formats SAN. Scottish Progressive can hand several double-step
        # targets to the opponent, so an orthodox ``#`` can still have a legal
        # progressive en-passant reply. The completed boundary is the source
        # of truth for the suffix.
        suffix = "#" if outcome == Outcome.CHECKMATE else "+"
        sans = (*sans[:-1], sans[-1].rstrip("+#") + suffix)
    return SeriesResult(
        moves=moves,
        san=sans,
        final_state=child,
        ended_by_check=delivered_check,
        outcome=outcome,
        unused_moves=max(0, root.moves_available - len(moves)),
    )


def _stuck_result(
    root: ProgressiveState,
    board: chess.Board,
    moves: tuple[str, ...],
    sans: tuple[str, ...],
) -> SeriesResult:
    # The game ends during the current series, so there is no handoff and no
    # next-series en-passant state to preserve.
    stuck = ProgressiveState(
        board=board,
        series_number=root.series_number,
        quiet_series=root.quiet_series,
        ep_targets=(),
    )
    return SeriesResult(
        moves=moves,
        san=sans,
        final_state=stuck,
        outcome=Outcome.CHECKMATE if board.is_check() else Outcome.STALEMATE,
        unused_moves=max(0, root.moves_available - len(moves)),
    )


def _replay_required_prefix(
    state: ProgressiveState,
    required_prefix: tuple[str, ...],
    counters: GenerationStats,
    *,
    max_positions: int | None,
    should_stop: Callable[[], bool] | None,
    track_tactical_provenance: bool = False,
) -> tuple[_FrontierState | None, SeriesResult | None]:
    """Replays a fixed move-order prefix before any transposition merging.

    Filtering merged complete series after generation is not equivalent: the
    representative retained for a transposition may use a different move
    order. Replaying first keeps every descendant inside the requested line.
    """

    if len(required_prefix) > state.moves_available:
        raise SeriesLegalityError(
            f"required prefix has {len(required_prefix)} moves but series "
            f"budget is {state.moves_available}"
        )
    counters.required_prefix_moves = len(required_prefix)

    mover = state.board.turn
    board = state.board.copy(stack=False)
    board.ep_square = state.ep_targets[0] if len(state.ep_targets) == 1 else None
    moves: tuple[str, ...] = ()
    sans: tuple[str, ...] = ()
    ep_candidates: dict[int, int] = {}
    made_progress = False
    tactical_provenance: set[TacticalOpportunity] = set()

    for index, uci in enumerate(required_prefix):
        if should_stop is not None and should_stop():
            raise GenerationCancelled
        _visit_generation_position(counters, max_positions)
        variants = {
            move.uci(): (move, required_ep)
            for move, required_ep in _legal_move_variants(
                board,
                state.ep_targets if index == 0 else (),
            )
        }
        selected = variants.get(uci)
        if selected is None:
            raise SeriesLegalityError(
                f"illegal required-prefix move {uci} at series index {index + 1}"
            )

        move, required_ep = selected
        board.ep_square = required_ep
        san = board.san(move)
        piece = board.piece_at(move.from_square)
        is_pawn_move = piece is not None and piece.piece_type == chess.PAWN
        is_capture = board.is_capture(move)
        if track_tactical_provenance and move.promotion is not None:
            tactical_provenance.add(
                (_TACTICAL_PROMOTION, f"{index + 1}:{move.uci()}")
            )
        if track_tactical_provenance and is_capture:
            tactical_provenance.add(
                (_TACTICAL_CAPTURE, f"{index + 1}:{move.uci()}")
            )
        if move.from_square in ep_candidates:
            del ep_candidates[move.from_square]
        if is_pawn_move and abs(move.to_square - move.from_square) == 16:
            ep_candidates[move.to_square] = (
                move.from_square + move.to_square
            ) // 2

        board.push(move)
        moves += (move.uci(),)
        sans += (san,)
        made_progress = made_progress or is_pawn_move or is_capture
        delivered_check = board.is_check()
        complete = delivered_check or len(moves) == state.moves_available
        if complete:
            if index + 1 != len(required_prefix):
                raise SeriesLegalityError(
                    "required prefix continues after check or series-budget completion"
                )
            return None, _finish_series(
                state,
                board,
                moves,
                sans,
                ep_candidates,
                made_progress,
                delivered_check=delivered_check,
            )

        board.turn = mover
        board.ep_square = None
        if not _has_legal_move(board):
            if index + 1 != len(required_prefix):
                raise SeriesLegalityError(
                    "required prefix continues after progressive stalemate"
                )
            return None, _stuck_result(state, board, moves, sans)

    return (
        _FrontierState(
            board=board,
            moves=moves,
            sans=sans,
            ep_candidates=ep_candidates,
            made_progress=made_progress,
            tactical_provenance=tuple(sorted(tactical_provenance)),
        ),
        None,
    )


def _merged_series_generation(
    state: ProgressiveState,
    counters: GenerationStats,
    *,
    required_prefix: tuple[str, ...],
    max_frontier_states: int | None,
    max_positions: int | None,
    frontier_score: FrontierScore | None,
    tactical_opportunities: (
        Callable[[chess.Board], tuple[TacticalOpportunity, ...]] | None
    ),
    should_stop: Callable[[], bool] | None,
) -> list[SeriesResult]:
    """Dynamic-programming generator that merges intra-series states early."""

    mover = state.board.turn
    protect_tactics = tactical_opportunities is not None
    root, prefix_result = _replay_required_prefix(
        state,
        required_prefix,
        counters,
        max_positions=max_positions,
        should_stop=should_stop,
        track_tactical_provenance=protect_tactics,
    )
    frontier = [root] if root is not None else []
    completed: list[
        tuple[SeriesResult, tuple[TacticalOpportunity, ...]]
    ] = []

    def record(
        result: SeriesResult,
        path_count: int,
        tactical_provenance: tuple[TacticalOpportunity, ...] = (),
    ) -> None:
        weighted = result.with_transposition_count(path_count)
        counters.raw_series += path_count
        if result.ended_by_check:
            counters.checking_series += path_count
        if result.outcome == Outcome.CHECKMATE:
            counters.checkmates += path_count
        elif result.outcome == Outcome.STALEMATE:
            counters.stalemates += path_count
        combined = set(tactical_provenance)
        if protect_tactics and result.ended_by_check and result.moves:
            combined.add(
                (
                    _TACTICAL_MATE
                    if result.outcome == Outcome.CHECKMATE
                    else _TACTICAL_CHECK,
                    f"{len(result.moves)}:{result.moves[-1]}",
                )
            )
        completed.append((weighted, tuple(sorted(combined))))

    if prefix_result is not None:
        record(
            prefix_result,
            1,
            (
                _series_tactical_provenance(state, prefix_result)
                if protect_tactics
                else ()
            ),
        )

    while frontier:
        if should_stop is not None and should_stop():
            raise GenerationCancelled
        following: dict[tuple[object, ...], _FrontierState] = {}
        for item in frontier:
            if should_stop is not None and should_stop():
                raise GenerationCancelled
            _visit_generation_position(counters, max_positions)
            variants = _expanded_move_variants(
                item.board, state.ep_targets if not item.moves else ()
            )
            if not variants:
                record(
                    _stuck_result(
                        state,
                        item.board.copy(stack=False),
                        item.moves,
                        item.sans,
                    ),
                    item.path_count,
                    item.tactical_provenance,
                )
                continue

            for expanded in variants:
                move = expanded.move
                current = expanded.board
                san = expanded.san
                is_pawn_move = expanded.is_pawn_move
                is_capture = expanded.is_capture

                next_candidates = dict(item.ep_candidates)
                if move.from_square in next_candidates:
                    del next_candidates[move.from_square]
                if is_pawn_move and abs(move.to_square - move.from_square) == 16:
                    next_candidates[move.to_square] = (
                        move.from_square + move.to_square
                    ) // 2

                delivered_check = expanded.delivered_check
                next_moves = item.moves + (move.uci(),)
                next_sans = item.sans + (san,)
                next_progress = item.made_progress or is_pawn_move or is_capture
                next_tactical_provenance = set(item.tactical_provenance)
                if protect_tactics and move.promotion is not None:
                    next_tactical_provenance.add(
                        (
                            _TACTICAL_PROMOTION,
                            f"{len(next_moves)}:{move.uci()}",
                        )
                    )
                if protect_tactics and is_capture:
                    next_tactical_provenance.add(
                        (
                            _TACTICAL_CAPTURE,
                            f"{len(next_moves)}:{move.uci()}",
                        )
                    )
                if delivered_check or len(next_moves) == state.moves_available:
                    record(
                        _finish_series(
                            state,
                            current,
                            next_moves,
                            next_sans,
                            next_candidates,
                            next_progress,
                            delivered_check=delivered_check,
                        ),
                        item.path_count,
                        tuple(sorted(next_tactical_provenance)),
                    )
                    continue

                current.turn = mover
                current.ep_square = None
                key = (
                    _board_position_key(current),
                    tuple(sorted(next_candidates.items())),
                    next_progress,
                )
                candidate = _FrontierState(
                    current,
                    next_moves,
                    next_sans,
                    next_candidates,
                    next_progress,
                    item.path_count,
                    tuple(sorted(next_tactical_provenance)),
                )
                incumbent = following.get(key)
                if incumbent is None:
                    following[key] = candidate
                    continue
                following[key] = _merge_frontier_states(incumbent, candidate)
        frontier = _bound_frontier(
            following,
            mover=mover,
            prefix_length=len(required_prefix),
            max_frontier_states=max_frontier_states,
            frontier_score=frontier_score,
            tactical_opportunities=tactical_opportunities,
            counters=counters,
        )

    merged: dict[tuple[object, ...], SeriesResult] = {}
    counts: dict[tuple[object, ...], int] = {}
    merged_tactical_provenance: dict[
        tuple[object, ...],
        set[TacticalOpportunity],
    ] = {}
    for result, tactical_provenance in completed:
        key = (
            _progressive_position_key(result.final_state),
            result.outcome,
            result.ended_by_check,
        )
        counts[key] = counts.get(key, 0) + result.transposition_count
        if protect_tactics:
            merged_tactical_provenance.setdefault(key, set()).update(
                tactical_provenance
            )
        incumbent = merged.get(key)
        candidate_rank = (-result.used_moves, result.machine_notation)
        incumbent_rank = (
            (-incumbent.used_moves, incumbent.machine_notation)
            if incumbent is not None
            else None
        )
        if incumbent is None or candidate_rank < incumbent_rank:
            merged[key] = result

    keyed_unique = [
        (key, result.with_transposition_count(counts[key]))
        for key, result in merged.items()
    ]
    keyed_unique.sort(key=lambda item: item[1].machine_notation)
    unique = [result for _key, result in keyed_unique]
    counters.unique_series = len(unique)
    counters.transpositions_merged = counters.raw_series - len(unique)
    if not protect_tactics:
        return unique
    return _TacticalSeriesResults(
        unique,
        {
            result.machine_notation: tuple(
                sorted(merged_tactical_provenance.get(key, set()))
            )
            for key, result in keyed_unique
        },
    )


_NATIVE_SIGNED_MAX = (1 << 63) - 1
_NATIVE_UNSIGNED_MAX = (1 << 64) - 1
_NATIVE_S3_NEURAL_MODEL = 1
_NATIVE_S3_NEURAL_BLEND_PERCENT = 75
_NATIVE_GENERATION_IDENTITY_INITIALIZED = False
_NATIVE_GENERATION_SOURCE_IDENTITY: str | None = None
_NATIVE_GENERATION_STATS_FIELDS = (
    "positions_visited",
    "frontier_score_positions",
    "raw_series",
    "unique_series",
    "transpositions_merged",
    "checking_series",
    "checkmates",
    "stalemates",
    "frontier_prunes",
    "frontier_states_pruned",
    "frontier_paths_pruned",
    "tactical_frontier_states_retained",
    "tactical_frontier_reserve_drops",
    "tactical_final_series_retained",
    "tactical_final_reserve_drops",
    "peak_frontier_states",
    "required_prefix_moves",
    "work_limit_reached",
)


@dataclass(frozen=True, slots=True)
class _NativeEvaluationSafetyProbe:
    """Minimal input used by evaluation's series-number-only safety gate."""

    series_number: int


def _apply_native_generation_stats(
    counters: GenerationStats,
    raw: tuple[object, ...],
) -> None:
    if len(raw) != len(_NATIVE_GENERATION_STATS_FIELDS):
        raise RuntimeError("native complete-series stats shape mismatch")
    for field_name, value in zip(
        _NATIVE_GENERATION_STATS_FIELDS,
        raw,
        strict=True,
    ):
        setattr(
            counters,
            field_name,
            bool(value) if field_name == "work_limit_reached" else int(value),
        )


def _native_complete_series_call(
    state: ProgressiveState,
    counters: GenerationStats,
    *,
    required_prefix: tuple[str, ...],
    max_frontier_states: int | None,
    max_positions: int | None,
    frontier_score: FrontierScore | None,
    native_final_score: NativeFinalSeriesScoreConfig | None,
    should_stop: Callable[[], bool] | None,
    symbols: tuple[str, ...],
    native_time_budget_ns: int | None = None,
    native_threads: int = 1,
) -> tuple[object, tuple[object, ...]] | None:
    """Builds one strictly gated source-matched native generation call."""

    if (
        state.series_number == 1
        or state.board.chess960
        or (should_stop is not None and native_time_budget_ns is None)
        or (
            native_time_budget_ns is not None
            and (type(native_time_budget_ns) is not int or native_time_budget_ns < 0)
        )
        or type(native_threads) is not int
        or not 1 <= native_threads <= 64
        or (native_threads > 1 and native_time_budget_ns is None)
        or any(type(move) is not str for move in required_prefix)
    ):
        return None
    if any(
        getattr(counters, field_name)
        for field_name in _NATIVE_GENERATION_STATS_FIELDS
    ):
        # The native response is a self-contained delta. Preserve the rarer
        # public case of callers reusing a counters object through the oracle.
        return None

    from . import evaluation

    native = evaluation._native_eval
    if native is None or any(not hasattr(native, symbol) for symbol in symbols):
        return None
    global _NATIVE_GENERATION_IDENTITY_INITIALIZED
    global _NATIVE_GENERATION_SOURCE_IDENTITY
    if not _NATIVE_GENERATION_IDENTITY_INITIALIZED:
        _NATIVE_GENERATION_SOURCE_IDENTITY = evaluation._native_source_identity()
        _NATIVE_GENERATION_IDENTITY_INITIALIZED = True
    if (
        _NATIVE_GENERATION_SOURCE_IDENTITY is None
        or getattr(native, "SOURCE_IDENTITY", None)
        != _NATIVE_GENERATION_SOURCE_IDENTITY
    ):
        return None
    if not (
        1 <= state.series_number < _NATIVE_SIGNED_MAX
        and 0 <= state.quiet_series < _NATIVE_SIGNED_MAX
        and 0 <= state.board.halfmove_clock <= _NATIVE_SIGNED_MAX
        and 1 <= state.board.fullmove_number <= _NATIVE_SIGNED_MAX
    ):
        return None
    if any(
        value is not None and value > _NATIVE_UNSIGNED_MAX
        for value in (max_frontier_states, max_positions)
    ):
        return None
    if frontier_score is not None or native_final_score is not None:
        from .profiles import EvaluationWeights

    native_weights: tuple[int, int, int, int, int, int] | None = None
    if frontier_score is not None:
        if type(frontier_score) is not NativeFrontierScoreConfig:
            return None
        if (
            frontier_score.series_number != state.series_number
            or frontier_score.quiet_series != state.quiet_series
        ):
            return None
        try:
            weights = EvaluationWeights(
                material=frontier_score.material,
                king_space=frontier_score.king_space,
                promotion_corridors=frontier_score.promotion_corridors,
                immediate_vulnerability=frontier_score.immediate_vulnerability,
                boundary_check=frontier_score.boundary_check,
            )
        except (TypeError, ValueError):
            return None
        if not evaluation._native_fast_evaluation_is_safe(state, weights):
            return None
        native_weights = (
            weights.material,
            weights.king_space,
            weights.promotion_corridors,
            weights.immediate_vulnerability,
            weights.boundary_check,
            int(frontier_score.tactical_protection),
        )

    native_final: tuple[int, int, int, int, int, int, int, int] | None = None
    if native_final_score is not None:
        if type(native_final_score) is not NativeFinalSeriesScoreConfig:
            return None
        if not (
            1 <= native_final_score.max_returned_series <= _NATIVE_UNSIGNED_MAX
            and 0 <= native_final_score.ply_from_root <= _NATIVE_SIGNED_MAX
            and 1 <= native_final_score.mate_score <= _NATIVE_SIGNED_MAX
        ):
            return None
        try:
            final_weights = EvaluationWeights(
                material=native_final_score.material,
                king_space=native_final_score.king_space,
                promotion_corridors=native_final_score.promotion_corridors,
                immediate_vulnerability=(
                    native_final_score.immediate_vulnerability
                ),
                boundary_check=native_final_score.boundary_check,
            )
        except (TypeError, ValueError):
            return None
        safety_probe = _NativeEvaluationSafetyProbe(state.series_number + 1)
        if not evaluation._native_fast_evaluation_is_safe(
            safety_probe,  # type: ignore[arg-type]
            final_weights,
        ):
            return None
        native_final = (
            native_final_score.max_returned_series,
            native_final_score.ply_from_root,
            native_final_score.mate_score,
            final_weights.material,
            final_weights.king_space,
            final_weights.promotion_corridors,
            final_weights.immediate_vulnerability,
            final_weights.boundary_check,
        )
        if (
            os.environ.get("SPC_NATIVE_NEURAL_S3") == "1"
            and state.series_number == 2
            and state.board.turn == chess.BLACK
        ):
            # This frozen student was trained only on complete boundaries
            # entering Series 3 after Black's Series 2. Keep the opt-in
            # experiment fail-closed outside that exact scope; the native
            # kernel independently revalidates it.
            native_final += (
                _NATIVE_S3_NEURAL_MODEL,
                _NATIVE_S3_NEURAL_BLEND_PERCENT,
            )

    arguments: tuple[object, ...] = (
        state.board.pawns,
        state.board.knights,
        state.board.bishops,
        state.board.rooks,
        state.board.queens,
        state.board.kings,
        state.board.occupied_co[chess.WHITE],
        state.board.occupied_co[chess.BLACK],
        state.board.promoted,
        state.board.clean_castling_rights(),
        state.board.turn,
        state.board.halfmove_clock,
        state.board.fullmove_number,
        state.series_number,
        state.quiet_series,
        state.ep_targets,
        required_prefix,
        max_frontier_states,
        max_positions,
        native_weights,
        native_final,
    )
    if native_time_budget_ns is not None:
        arguments += (native_time_budget_ns,)
    if native_threads > 1:
        arguments += (native_threads,)
    return native, arguments


def _native_complete_series_generation(
    state: ProgressiveState,
    counters: GenerationStats,
    *,
    required_prefix: tuple[str, ...],
    max_frontier_states: int | None,
    max_positions: int | None,
    frontier_score: FrontierScore | None,
    native_final_score: NativeFinalSeriesScoreConfig | None,
    should_stop: Callable[[], bool] | None,
) -> list[SeriesResult] | None:
    """Uses the all-native frontier kernel when its exact contract applies."""

    call = _native_complete_series_call(
        state,
        counters,
        required_prefix=required_prefix,
        max_frontier_states=max_frontier_states,
        max_positions=max_positions,
        frontier_score=frontier_score,
        native_final_score=native_final_score,
        should_stop=should_stop,
        symbols=("generate_complete_series",),
    )
    if call is None:
        return None
    native, arguments = call
    try:
        status, _message, raw_stats, raw_series = (
            native.generate_complete_series(*arguments)
        )
    except (OverflowError, TypeError, ValueError):
        return None

    status = int(status)
    if status in {2, 3}:
        # Invalid-prefix text and arbitrary-precision overflow are both rerun
        # from pristine Python counters so the public exception/state contract
        # remains byte-for-byte authoritative.
        return None
    if status == 1:
        _apply_native_generation_stats(counters, tuple(raw_stats))
        raise GenerationWorkLimit
    if status != 0:
        raise RuntimeError(f"unknown native complete-series status {status}")

    materialized: list[SeriesResult] = []
    try:
        for moves, transposition_count in raw_series:
            result = play_series(state, tuple(str(move) for move in moves))
            materialized.append(
                result.with_transposition_count(int(transposition_count))
            )
    except (SeriesLegalityError, TypeError, ValueError):
        return None
    _apply_native_generation_stats(counters, tuple(raw_stats))
    return materialized


class _NativeSeriesReference:
    """One cheap path whose trusted final state is decoded only on demand."""

    __slots__ = (
        "_capsule",
        "_decoded",
        "_index",
        "_materialized",
        "_native",
        "_root",
        "moves",
        "transposition_count",
    )

    def __init__(
        self,
        batch: "_NativeSeriesBatch",
        root: ProgressiveState,
        index: int,
        moves: tuple[str, ...],
        transposition_count: int,
    ) -> None:
        self._native = batch._native  # noqa: SLF001
        self._capsule = batch._capsule  # noqa: SLF001
        self._root = root
        self._index = index
        self.moves = moves
        self.transposition_count = transposition_count
        self._decoded: tuple[ProgressiveState, Outcome | None, bool] | None = None
        self._materialized: SeriesResult | None = None

    @property
    def machine_notation(self) -> str:
        return "/".join(self.moves)

    def _decode(self) -> tuple[ProgressiveState, Outcome | None, bool]:
        if self._decoded is not None:
            return self._decoded
        raw = tuple(
            self._native.complete_series_candidate(
                self._capsule,
                self._index,
            )
        )
        if len(raw) != 18:
            raise RuntimeError("native complete-series candidate shape mismatch")
        board = chess.Board(None)
        board.pawns = int(raw[0])
        board.knights = int(raw[1])
        board.bishops = int(raw[2])
        board.rooks = int(raw[3])
        board.queens = int(raw[4])
        board.kings = int(raw[5])
        board.occupied_co[chess.WHITE] = int(raw[6])
        board.occupied_co[chess.BLACK] = int(raw[7])
        board.occupied = board.occupied_co[chess.WHITE] | board.occupied_co[chess.BLACK]
        board.promoted = int(raw[8])
        board.castling_rights = int(raw[9])
        board.turn = bool(raw[10])
        board.halfmove_clock = int(raw[11])
        board.fullmove_number = int(raw[12])
        final_state = ProgressiveState(
            board,
            series_number=int(raw[13]),
            quiet_series=int(raw[14]),
            ep_targets=tuple(int(square) for square in raw[15]),
        )
        outcomes: tuple[Outcome | None, ...] = (
            None,
            Outcome.CHECKMATE,
            Outcome.STALEMATE,
            Outcome.TEN_SERIES_DRAW,
        )
        outcome_code = int(raw[16])
        if not 0 <= outcome_code < len(outcomes):
            raise RuntimeError("native complete-series outcome is invalid")
        self._decoded = final_state, outcomes[outcome_code], bool(raw[17])
        return self._decoded

    @property
    def final_state(self) -> ProgressiveState:
        return self._decode()[0]

    @property
    def outcome(self) -> Outcome | None:
        return self._decode()[1]

    @property
    def ended_by_check(self) -> bool:
        return self._decode()[2]

    def materialize(self) -> SeriesResult:
        if self._materialized is None:
            self._materialized = play_series(
                self._root,
                self.moves,
            ).with_transposition_count(self.transposition_count)
        return self._materialized


class _NativeSeriesBatch:
    """Opaque native storage plus lightweight deterministically ordered paths."""

    __slots__ = ("_capsule", "_native", "_references")

    def __init__(
        self,
        native: object,
        capsule: object,
        root: ProgressiveState,
        raw_series: object,
    ) -> None:
        self._native = native
        self._capsule = capsule
        references: list[_NativeSeriesReference] = []
        for index, item in enumerate(raw_series):
            moves, count = item
            canonical_moves = tuple(moves)
            if any(type(move) is not str for move in canonical_moves):
                raise TypeError("native complete-series move is not an exact string")
            canonical_count = int(count)
            if canonical_count < 1:
                raise ValueError("native complete-series count must be positive")
            references.append(
                _NativeSeriesReference(
                    self,
                    root,
                    index,
                    canonical_moves,
                    canonical_count,
                )
            )
        self._references = tuple(references)

    def __len__(self) -> int:
        return len(self._references)

    def references(self) -> list[_NativeSeriesReference]:
        return list(self._references)


def _native_complete_series_batch(
    state: ProgressiveState,
    counters: GenerationStats,
    *,
    required_prefix: tuple[str, ...],
    max_frontier_states: int | None,
    max_positions: int | None,
    frontier_score: FrontierScore | None,
    native_final_score: NativeFinalSeriesScoreConfig | None,
    should_stop: Callable[[], bool] | None,
    native_time_budget_ns: int | None = None,
    native_threads: int = 1,
) -> _NativeSeriesBatch | None:
    """Prepares a native batch without replaying any retained series."""

    call = _native_complete_series_call(
        state,
        counters,
        required_prefix=required_prefix,
        max_frontier_states=max_frontier_states,
        max_positions=max_positions,
        frontier_score=frontier_score,
        native_final_score=native_final_score,
        should_stop=should_stop,
        symbols=(
            ("prepare_complete_series_timed_parallel", "complete_series_candidate")
            if native_threads > 1
            else ("prepare_complete_series_timed", "complete_series_candidate")
            if native_time_budget_ns is not None
            else ("prepare_complete_series", "complete_series_candidate")
        ),
        native_time_budget_ns=native_time_budget_ns,
        native_threads=native_threads,
    )
    if call is None:
        return None
    native, arguments = call
    try:
        prepare = (
            native.prepare_complete_series_timed_parallel
            if native_threads > 1
            else native.prepare_complete_series_timed
            if native_time_budget_ns is not None
            else native.prepare_complete_series
        )
        status, _message, raw_stats, raw_series, capsule = prepare(*arguments)
    except (OverflowError, TypeError, ValueError):
        return None

    status = int(status)
    if status in {2, 3}:
        return None
    if status == 1:
        _apply_native_generation_stats(counters, tuple(raw_stats))
        raise GenerationWorkLimit
    if status == 4:
        _apply_native_generation_stats(counters, tuple(raw_stats))
        raise GenerationCancelled
    if status != 0:
        raise RuntimeError(f"unknown native complete-series status {status}")
    try:
        batch = _NativeSeriesBatch(native, capsule, state, raw_series)
    except (TypeError, ValueError):
        return None
    _apply_native_generation_stats(counters, tuple(raw_stats))
    return batch


def generate_series(
    state: ProgressiveState,
    *,
    merge_transpositions: bool = True,
    stats: GenerationStats | None = None,
    required_prefix: Iterable[str] = (),
    max_frontier_states: int | None = None,
    max_positions: int | None = None,
    frontier_score: FrontierScore | None = None,
    native_final_score: NativeFinalSeriesScoreConfig | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[SeriesResult]:
    """Generates complete legal series from a boundary position.

    A checking move, checkmate, or progressive stalemate may produce a result
    shorter than the nominal series budget. When ``merge_transpositions`` is
    true, different move orders reaching the same full progressive state are
    represented once and counted on ``SeriesResult.transposition_count``.

    ``required_prefix`` is replayed before any merge, so a requested move
    order cannot disappear merely because a lexicographically smaller route
    reaches the same partial state. ``max_frontier_states`` bounds every
    intermediate same-side layer before complete series are materialized;
    any such pruning is recorded in ``GenerationStats`` and makes the caller's
    result selective. ``max_positions`` is a deterministic combined watchdog
    for expanded generation states and distinct partial states evaluated by
    ``frontier_score``. ``native_final_score`` is an internal search-only
    acceleration hint: the Python oracle ignores it and returns every merged
    result, while a compatible native kernel may return only its exact static
    top-K without changing any full-generation counters.
    """

    counters = stats if stats is not None else GenerationStats()
    prefix = tuple(required_prefix)
    if max_frontier_states is not None and max_frontier_states < 1:
        raise ValueError("max_frontier_states must be positive")
    if max_positions is not None and max_positions < 1:
        raise ValueError("max_positions must be positive")
    if merge_transpositions:
        native_results = _native_complete_series_generation(
            state,
            counters,
            required_prefix=prefix,
            max_frontier_states=max_frontier_states,
            max_positions=max_positions,
            frontier_score=frontier_score,
            native_final_score=native_final_score,
            should_stop=should_stop,
        )
        if native_results is not None:
            return native_results
        metered_frontier_score = frontier_score
        metered_tactical_opportunities: (
            Callable[[chess.Board], tuple[TacticalOpportunity, ...]] | None
        ) = None
        if frontier_score is not None:
            cached_frontier_inspections: dict[
                tuple[object, ...],
                tuple[int, tuple[TacticalOpportunity, ...]],
            ] = {}

            def inspect_frontier(
                board: chess.Board,
            ) -> tuple[int, tuple[TacticalOpportunity, ...]]:
                # Frontier inspection evaluates only board geometry: material,
                # attacks, legal checks/mates, captures, and promotions. Move
                # clocks affect boundary adjudication, but not this ordering
                # score, so clock-distinct transpositions can share it exactly.
                key = _board_position_key(board)
                cached = cached_frontier_inspections.get(key)
                if cached is not None:
                    return cached
                _visit_frontier_score_position(counters, max_positions)
                inspected = (
                    frontier_score.inspect(board)
                    if isinstance(frontier_score, NativeFrontierScoreConfig)
                    else (frontier_score(board), ())
                )
                cached_frontier_inspections[key] = inspected
                return inspected

            def metered_frontier_score(board: chess.Board) -> int:
                return inspect_frontier(board)[0]

            if (
                isinstance(frontier_score, NativeFrontierScoreConfig)
                and frontier_score.tactical_protection
            ):

                def metered_tactical_opportunities(
                    board: chess.Board,
                ) -> tuple[TacticalOpportunity, ...]:
                    return inspect_frontier(board)[1]

        return _merged_series_generation(
            state,
            counters,
            required_prefix=prefix,
            max_frontier_states=max_frontier_states,
            max_positions=max_positions,
            frontier_score=metered_frontier_score,
            tactical_opportunities=metered_tactical_opportunities,
            should_stop=should_stop,
        )
    if max_frontier_states is not None:
        raise ValueError(
            "max_frontier_states requires merge_transpositions=True"
        )
    mover = state.board.turn
    budget = state.moves_available
    results: list[SeriesResult] = []

    def walk(
        board: chess.Board,
        moves: tuple[str, ...],
        sans: tuple[str, ...],
        ep_candidates: dict[int, int],
        made_progress: bool,
        *,
        first_move: bool,
    ) -> None:
        if should_stop is not None and should_stop():
            raise GenerationCancelled
        _visit_generation_position(counters, max_positions)
        variants = _legal_move_variants(
            board, state.ep_targets if first_move else ()
        )
        if not variants:
            result = _stuck_result(state, board.copy(stack=False), moves, sans)
            counters.raw_series += 1
            if result.outcome == Outcome.CHECKMATE:
                counters.checkmates += 1
            else:
                counters.stalemates += 1
            results.append(result)
            return

        for move, required_ep in variants:
            current = board.copy(stack=False)
            current.ep_square = required_ep
            san = current.san(move)
            piece = current.piece_at(move.from_square)
            is_pawn_move = piece is not None and piece.piece_type == chess.PAWN
            is_capture = current.is_capture(move)

            next_candidates = dict(ep_candidates)
            if move.from_square in next_candidates:
                del next_candidates[move.from_square]
            if is_pawn_move and abs(move.to_square - move.from_square) == 16:
                next_candidates[move.to_square] = (
                    move.from_square + move.to_square
                ) // 2

            current.push(move)
            delivered_check = current.is_check()
            next_moves = moves + (move.uci(),)
            next_sans = sans + (san,)
            next_progress = made_progress or is_pawn_move or is_capture

            if delivered_check or len(next_moves) == budget:
                result = _finish_series(
                    state,
                    current,
                    next_moves,
                    next_sans,
                    next_candidates,
                    next_progress,
                    delivered_check=delivered_check,
                )
                counters.raw_series += 1
                if delivered_check:
                    counters.checking_series += 1
                if result.outcome == Outcome.CHECKMATE:
                    counters.checkmates += 1
                elif result.outcome == Outcome.STALEMATE:
                    counters.stalemates += 1
                results.append(result)
                continue

            # python-chess correctly flipped to the opponent to detect check.
            # Scottish intra-series play now restores the original mover and
            # removes all old e.p. rights until the next series boundary.
            current.turn = mover
            current.ep_square = None
            walk(
                current,
                next_moves,
                next_sans,
                next_candidates,
                next_progress,
                first_move=False,
            )

    root, prefix_result = _replay_required_prefix(
        state,
        prefix,
        counters,
        max_positions=max_positions,
        should_stop=should_stop,
    )
    if prefix_result is not None:
        counters.raw_series = 1
        counters.unique_series = 1
        counters.checking_series = int(prefix_result.ended_by_check)
        counters.checkmates = int(prefix_result.outcome == Outcome.CHECKMATE)
        counters.stalemates = int(prefix_result.outcome == Outcome.STALEMATE)
        return [prefix_result]
    assert root is not None
    walk(
        root.board,
        root.moves,
        root.sans,
        root.ep_candidates,
        root.made_progress,
        first_move=not root.moves,
    )

    counters.unique_series = len(results)
    return sorted(results, key=lambda result: result.machine_notation)


def _series_tactical_provenance(
    state: ProgressiveState,
    result: SeriesResult,
) -> tuple[TacticalOpportunity, ...]:
    """Classifies already-played tactics using the same per-ply UCI keys.

    This is the Python final-selection oracle.  Native generation carries the
    same provenance while expanding, avoiding a second replay in production.
    """

    mover = state.board.turn
    board = state.board.copy(stack=False)
    provenance: set[TacticalOpportunity] = set()
    for index, uci in enumerate(result.moves):
        variants = {
            move.uci(): (move, required_ep)
            for move, required_ep in _legal_move_variants(
                board,
                state.ep_targets if index == 0 else (),
            )
        }
        selected = variants.get(uci)
        if selected is None:  # pragma: no cover - generated evidence is legal
            raise RuntimeError("generated series cannot be replayed tactically")
        move, required_ep = selected
        board.ep_square = required_ep
        signature = f"{index + 1}:{uci}"
        if move.promotion is not None:
            provenance.add((_TACTICAL_PROMOTION, signature))
        if board.is_capture(move):
            provenance.add((_TACTICAL_CAPTURE, signature))
        board.push(move)
        if index + 1 != len(result.moves):
            board.turn = mover
            board.ep_square = None

    if result.ended_by_check and result.moves:
        provenance.add(
            (
                _TACTICAL_MATE
                if result.outcome == Outcome.CHECKMATE
                else _TACTICAL_CHECK,
                f"{len(result.moves)}:{result.moves[-1]}",
            )
        )
    return tuple(sorted(provenance))


def play_series(
    state: ProgressiveState,
    moves: Iterable[str],
) -> SeriesResult:
    """Validates and applies one explicitly supplied complete Scottish series."""

    requested = tuple(moves)
    board = state.board.copy(stack=False)
    mover = board.turn
    ep_candidates: dict[int, int] = {}
    made_progress = False
    sans: tuple[str, ...] = ()
    played: tuple[str, ...] = ()

    if not requested:
        variants = _legal_move_variants(board, state.ep_targets)
        if variants:
            raise SeriesLegalityError("series is incomplete: a legal move is available")
        return _stuck_result(state, board, (), ())

    for index, uci in enumerate(requested):
        if index >= state.moves_available:
            raise SeriesLegalityError(
                f"series budget is {state.moves_available}; extra move {uci} supplied"
            )
        variants = {
            move.uci(): (move, required_ep)
            for move, required_ep in _legal_move_variants(
                board, state.ep_targets if index == 0 else ()
            )
        }
        if uci not in variants:
            raise SeriesLegalityError(f"illegal move {uci} at series index {index + 1}")
        move, required_ep = variants[uci]
        board.ep_square = required_ep
        san = board.san(move)
        piece = board.piece_at(move.from_square)
        is_pawn_move = piece is not None and piece.piece_type == chess.PAWN
        is_capture = board.is_capture(move)

        if move.from_square in ep_candidates:
            del ep_candidates[move.from_square]
        if is_pawn_move and abs(move.to_square - move.from_square) == 16:
            ep_candidates[move.to_square] = (
                move.from_square + move.to_square
            ) // 2

        board.push(move)
        delivered_check = board.is_check()
        played += (move.uci(),)
        sans += (san,)
        made_progress = made_progress or is_pawn_move or is_capture

        if delivered_check or len(played) == state.moves_available:
            if index != len(requested) - 1:
                reason = "check ended the series" if delivered_check else "budget exhausted"
                raise SeriesLegalityError(
                    f"extra moves supplied after {reason} on move {index + 1}"
                )
            return _finish_series(
                state,
                board,
                played,
                sans,
                ep_candidates,
                made_progress,
                delivered_check=delivered_check,
            )

        board.turn = mover
        board.ep_square = None
        if not _has_legal_move(board):
            if index != len(requested) - 1:
                raise SeriesLegalityError(
                    f"extra moves supplied after progressive stalemate on move {index + 1}"
                )
            return _stuck_result(state, board, played, sans)

    raise SeriesLegalityError(
        f"series is incomplete: used {len(played)} of {state.moves_available} moves "
        "without giving check or reaching stalemate"
    )


def has_mating_series(
    state: ProgressiveState,
    *,
    node_limit: int = 100_000,
    should_stop: Callable[[], bool] | None = None,
) -> bool | None:
    """Finds mate anywhere in the current allotted series.

    ``None`` means the deterministic safety budget was exhausted. That result
    remains manual/proof-required, so the helper can never manufacture a draw
    by failing to finish a large series tree.
    """

    mover = state.board.turn
    visited: set[tuple[str, int, tuple[tuple[int, int], ...]]] = set()
    nodes = 0

    def walk(
        board: chess.Board,
        used: int,
        ep_candidates: dict[int, int],
        *,
        first_move: bool,
    ) -> bool | None:
        nonlocal nodes
        variants = _legal_move_variants(
            board, state.ep_targets if first_move else ()
        )

        def order(item: tuple[chess.Move, int | None]) -> tuple[int, int, str]:
            move, required_ep = item
            board.ep_square = required_ep
            gives_check = board.gives_check(move)
            tactical = board.is_capture(move) or move.promotion is not None
            board.ep_square = None
            return (0 if gives_check else 1, 0 if tactical else 1, move.uci())

        for move, required_ep in sorted(variants, key=order):
            if should_stop is not None and should_stop():
                raise GenerationCancelled
            nodes += 1
            if nodes > node_limit:
                return None
            child = board.copy(stack=False)
            child.ep_square = required_ep
            piece = child.piece_at(move.from_square)
            next_candidates = dict(ep_candidates)
            if move.from_square in next_candidates:
                del next_candidates[move.from_square]
            if (
                piece is not None
                and piece.piece_type == chess.PAWN
                and abs(move.to_square - move.from_square) == 16
            ):
                next_candidates[move.to_square] = (
                    move.from_square + move.to_square
                ) // 2
            child.push(move)
            if child.is_check():
                if not _has_legal_move(child, next_candidates.values()):
                    return True
                continue  # A non-mating check still ends the Scottish series.
            if used + 1 >= state.moves_available:
                continue
            child.turn = mover
            child.ep_square = None
            if not _has_legal_move(child):
                continue  # Progressive stalemate.
            key = (
                _board_position_key(child),
                used + 1,
                tuple(sorted(next_candidates.items())),
            )
            if key in visited:
                continue
            visited.add(key)
            found = walk(
                child,
                used + 1,
                next_candidates,
                first_move=False,
            )
            if found is not False:
                return found
        return False

    root = state.board.copy(stack=False)
    root.ep_square = state.ep_targets[0] if len(state.ep_targets) == 1 else None
    return walk(root, 0, {}, first_move=True)


def quiet_adjudication_status(
    state: ProgressiveState,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> str | None:
    """Returns a conservative status for the ten-series draw convention."""

    if not state.quiet_draw_pending:
        return None
    if not _has_legal_move(state.board, state.ep_targets):
        return None  # Ordinary checkmate/stalemate takes precedence.
    if state.board.is_insufficient_material():
        return "proven-draw-no-mating-material"
    if has_mating_series(state, should_stop=should_stop) is True:
        return "mate-exception-immediate"
    return "manual-proof-required"
