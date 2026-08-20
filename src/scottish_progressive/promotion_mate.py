from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import chess

from .model import Outcome, ProgressiveState, SeriesResult
from .rules import _board_position_key, _legal_move_variants, play_series


MAX_PROMOTION_MATE_POSITIONS = 50_000
MAX_SETUP_MOVES = 2
REUSE_MOVE_ROUNDS = (2, 8, 16)
COMPLETION_CANDIDATE_BATCH = 96
MAX_COMPLETION_MOVES = 2
MIN_NON_PAWN_MOVES = 2


@dataclass(frozen=True, slots=True)
class PromotionMateProbe:
    """Outcome and fully metered evidence from one selective tactical lane."""

    series: SeriesResult | None = None
    eligible: bool = False
    positions_visited: int = 0
    setup_states: int = 0
    promotion_candidates: int = 0
    completion_probes: int = 0
    work_limit_reached: bool = False
    cancelled: bool = False
    replay_rejects: int = 0


@dataclass(slots=True)
class _Partial:
    board: chess.Board
    moves: tuple[str, ...]


@dataclass(slots=True)
class _PromotionCandidate:
    board: chess.Board
    moves: tuple[str, ...]
    remaining_moves: int
    promoted_square: int
    score: int = 0


@dataclass(slots=True)
class _LaneVariant:
    move: chess.Move
    board: chess.Board
    delivered_check: bool


class _ProbeWorkLimit(Exception):
    pass


class _ProbeCancelled(Exception):
    pass


class _Meter:
    __slots__ = ("limit", "positions", "should_stop")

    def __init__(
        self,
        limit: int,
        should_stop: Callable[[], bool] | None,
    ) -> None:
        self.limit = limit
        self.positions = 0
        self.should_stop = should_stop

    def visit(self) -> None:
        self.check_cancelled()
        if self.positions >= self.limit:
            raise _ProbeWorkLimit
        self.positions += 1

    def check_cancelled(self) -> None:
        if self.should_stop is not None and self.should_stop():
            raise _ProbeCancelled


def promotion_mate_eligible(
    state: ProgressiveState,
    *,
    required_prefix: tuple[str, ...] = (),
) -> bool:
    """Cheap structural gate for the promotion-and-reuse tactical lane.

    A promotion consumes at least one move per remaining rank. The lane is
    useful only when the series also has room for promoted-piece reuse; extra
    series moves are harmless because checkmate may end the series early.
    Ordinary early-game positions therefore pay only this bitboard/rank scan.
    """

    if len(required_prefix) >= state.moves_available:
        return False
    mover = state.board.turn
    for square in state.board.pieces(chess.PAWN, mover):
        rank = chess.square_rank(square)
        distance = 7 - rank if mover == chess.WHITE else rank
        spare_moves = state.moves_available - distance
        if 0 < distance and MIN_NON_PAWN_MOVES <= spare_moves:
            return True
    return False


def _position_key(board: chess.Board) -> tuple[object, ...]:
    return _board_position_key(board) + (board.promoted,)


def find_promotion_series_mate(
    state: ProgressiveState,
    *,
    required_prefix: tuple[str, ...] = (),
    max_positions: int = MAX_PROMOTION_MATE_POSITIONS,
    should_stop: Callable[[], bool] | None = None,
    promotion_score: Callable[[chess.Board], int] | None = None,
) -> PromotionMateProbe:
    """Finds one replay-proven mate in the current progressive series.

    This is deliberately a one-sided existential search, not an exhaustive
    no-mate proof. It explores a small number of arbitrary setup moves, exact
    same-pawn routes to a check-avoiding underpromotion (including captures),
    then a fair bounded promoted-piece-reuse completion search. A nonmating
    check ends its branch exactly as it ends a Scottish series. Exhaustion
    returns no claim.
    """

    prefix = tuple(required_prefix)
    if max_positions < 1 or not promotion_mate_eligible(
        state,
        required_prefix=prefix,
    ):
        return PromotionMateProbe()

    meter = _Meter(min(max_positions, MAX_PROMOTION_MATE_POSITIONS), should_stop)
    mover = state.board.turn
    move_budget = state.moves_available
    move_cache: dict[
        tuple[tuple[object, ...], bool],
        tuple[tuple[chess.Move, int | None], ...],
    ] = {}
    expansion_cache: dict[
        tuple[tuple[tuple[object, ...], bool], int | None],
        tuple[_LaneVariant, ...],
    ] = {}
    replay_rejects = 0
    setup_states = 0
    completion_probes = 0

    def raw_moves(
        partial: _Partial,
        *,
        charge: bool = True,
    ) -> tuple[tuple[chess.Move, int | None], ...]:
        first_move = not partial.moves
        key = (_position_key(partial.board), first_move)
        cached = move_cache.get(key)
        if cached is not None:
            meter.check_cancelled()
            return cached
        if charge:
            meter.visit()
        else:
            meter.check_cancelled()
        canonical = tuple(
            _legal_move_variants(
                partial.board,
                state.ep_targets if first_move else (),
            )
        )
        move_cache[key] = canonical
        return canonical

    def expanded(
        partial: _Partial,
        *,
        charge: bool = True,
        from_square: int | None = None,
    ) -> tuple[_LaneVariant, ...]:
        first_move = not partial.moves
        base_key = (_position_key(partial.board), first_move)
        key = (base_key, from_square)
        cached = expansion_cache.get(key)
        if cached is not None:
            meter.check_cancelled()
            return cached
        variants: list[_LaneVariant] = []
        for move, required_ep in raw_moves(partial, charge=charge):
            meter.check_cancelled()
            if from_square is not None and move.from_square != from_square:
                continue
            board = partial.board.copy(stack=False)
            board.ep_square = required_ep
            board.push(move)
            variants.append(_LaneVariant(move, board, board.is_check()))
        variants.sort(key=lambda item: item.move.uci())
        canonical = tuple(variants)
        expansion_cache[key] = canonical
        return canonical

    def _expanded_without_charge(
        partial: _Partial,
        *,
        from_square: int | None = None,
    ) -> tuple[_LaneVariant, ...]:
        return expanded(
            partial,
            charge=False,
            from_square=from_square,
        )

    def replay_mate(
        moves: tuple[str, ...],
        standard_terminal: chess.Board,
    ) -> SeriesResult | None:
        nonlocal replay_rejects
        meter.check_cancelled()
        # Standard checkmate is necessary but not sufficient: a Scottish
        # boundary can expose additional progressive en-passant replies.  Use
        # the cheap necessary condition before the authoritative full replay.
        if not standard_terminal.is_checkmate():
            return None
        try:
            result = play_series(state, moves)
        except ValueError:
            replay_rejects += 1
            return None
        meter.check_cancelled()
        if result.ended_by_check and result.outcome == Outcome.CHECKMATE:
            return result
        replay_rejects += 1
        return None

    def advance(partial: _Partial, variant: _LaneVariant) -> _Partial:
        board = variant.board.copy(stack=False)
        board.turn = mover
        board.ep_square = None
        return _Partial(board, partial.moves + (variant.move.uci(),))

    def forced_prefix() -> tuple[_Partial | None, SeriesResult | None, int | None]:
        partial = _Partial(state.board.copy(stack=False), ())
        promoted_square: int | None = None
        for uci in prefix:
            if len(partial.moves) >= move_budget:
                return None, None, promoted_square
            match = next(
                (
                    variant
                    for variant in expanded(partial)
                    if variant.move.uci() == uci
                ),
                None,
            )
            if match is None:
                return None, None, promoted_square
            moves = partial.moves + (uci,)
            if match.move.promotion is not None:
                promoted_square = match.move.to_square
            elif promoted_square == match.move.from_square:
                promoted_square = match.move.to_square
            if match.delivered_check:
                return None, replay_mate(moves, match.board), promoted_square
            partial = advance(partial, match)
        return partial, None, promoted_square

    def collect_promotions(
        partial: _Partial,
        pawn_square: int,
        collected: dict[tuple[object, ...], _PromotionCandidate],
        seen: set[tuple[tuple[object, ...], int, int]],
    ) -> SeriesResult | None:
        used = len(partial.moves)
        remaining = move_budget - used
        if remaining <= 1:
            return None
        rank = chess.square_rank(pawn_square)
        distance = 7 - rank if mover == chess.WHITE else rank
        if distance <= 0 or distance >= remaining:
            return None
        seen_key = (_position_key(partial.board), pawn_square, used)
        if seen_key in seen:
            return None
        seen.add(seen_key)

        variants = expanded(partial, from_square=pawn_square)
        checking_promotion_targets = {
            variant.move.to_square
            for variant in variants
            if variant.move.from_square == pawn_square
            and variant.move.promotion is not None
            and variant.delivered_check
        }
        for variant in variants:
            meter.check_cancelled()
            move = variant.move
            if move.from_square != pawn_square:
                continue
            piece = partial.board.piece_at(move.from_square)
            if piece is None or piece.color != mover or piece.piece_type != chess.PAWN:
                continue
            moves = partial.moves + (move.uci(),)
            if variant.delivered_check:
                mate = replay_mate(moves, variant.board)
                if mate is not None:
                    return mate
                continue
            next_partial = advance(partial, variant)
            if move.promotion is not None:
                if (
                    move.promotion == chess.QUEEN
                    or move.to_square not in checking_promotion_targets
                ):
                    continue
                moves_left = move_budget - len(moves)
                if moves_left < 1:
                    continue
                candidate = _PromotionCandidate(
                    next_partial.board,
                    moves,
                    moves_left,
                    move.to_square,
                )
                key = (_position_key(candidate.board), moves_left)
                incumbent = collected.get(key)
                if incumbent is None or moves < incumbent.moves:
                    collected[key] = candidate
                continue
            mate = collect_promotions(
                next_partial,
                move.to_square,
                collected,
                seen,
            )
            if mate is not None:
                return mate
        return None

    def collect_setup_promotions(
        root: _Partial,
    ) -> tuple[dict[tuple[object, ...], _PromotionCandidate], SeriesResult | None]:
        nonlocal setup_states
        collected: dict[tuple[object, ...], _PromotionCandidate] = {}
        layer = [root]
        for setup_depth in range(MAX_SETUP_MOVES + 1):
            following: dict[tuple[object, ...], _Partial] = {}
            for partial in sorted(layer, key=lambda item: item.moves):
                setup_states += 1
                for pawn_square in sorted(partial.board.pieces(chess.PAWN, mover)):
                    mate = collect_promotions(
                        partial,
                        pawn_square,
                        collected,
                        set(),
                    )
                    if mate is not None:
                        return collected, mate
                if setup_depth == MAX_SETUP_MOVES:
                    continue
                for variant in expanded(partial):
                    meter.check_cancelled()
                    moves = partial.moves + (variant.move.uci(),)
                    if variant.delivered_check:
                        mate = replay_mate(moves, variant.board)
                        if mate is not None:
                            return collected, mate
                        continue
                    if len(moves) >= move_budget - 1:
                        continue
                    child = advance(partial, variant)
                    key = _position_key(child.board)
                    incumbent = following.get(key)
                    if incumbent is None or child.moves < incumbent.moves:
                        following[key] = child
            layer = sorted(following.values(), key=lambda item: item.moves)
            if not layer:
                break
        return collected, None

    def score_promotions(
        candidates: dict[tuple[object, ...], _PromotionCandidate],
    ) -> list[_PromotionCandidate]:
        ordered: list[_PromotionCandidate] = []
        for candidate in candidates.values():
            if promotion_score is not None:
                meter.visit()
                candidate.score = promotion_score(candidate.board)
                meter.check_cancelled()
            ordered.append(candidate)
        return sorted(
            ordered,
            key=lambda item: (
                -item.remaining_moves,
                -item.score if mover == chess.WHITE else item.score,
                item.moves,
            ),
        )

    def complete(
        candidate: _PromotionCandidate,
        first_reuse_move: int,
        max_reuse_moves: int,
    ) -> SeriesResult | None:
        nonlocal completion_probes
        completion_probes += 1
        partial = _Partial(candidate.board.copy(stack=False), candidate.moves)
        if first_reuse_move == 0:
            meter.visit()
        else:
            meter.check_cancelled()
        reuse_variants = [
            variant
            for variant in sorted(
                _expanded_without_charge(
                    partial,
                    from_square=candidate.promoted_square,
                ),
                key=lambda item: (not item.delivered_check, item.move.uci()),
            )
        ]
        for variant in reuse_variants[first_reuse_move:max_reuse_moves]:
            meter.check_cancelled()
            moves = partial.moves + (variant.move.uci(),)
            if variant.delivered_check:
                mate = replay_mate(moves, variant.board)
                if mate is not None:
                    return mate
                continue
            if len(moves) >= move_budget:
                continue
            child = advance(partial, variant)
            meter.visit()
            for finishing in sorted(
                _expanded_without_charge(child),
                key=lambda item: (not item.delivered_check, item.move.uci()),
            ):
                meter.check_cancelled()
                if not finishing.delivered_check:
                    continue
                mate = replay_mate(
                    child.moves + (finishing.move.uci(),),
                    finishing.board,
                )
                if mate is not None:
                    return mate
        return None

    candidate_count = 0
    try:
        base, prefix_mate, prefix_promoted_square = forced_prefix()
        if prefix_mate is not None:
            return PromotionMateProbe(
                prefix_mate,
                True,
                meter.positions,
                setup_states,
                candidate_count,
                completion_probes,
                replay_rejects=replay_rejects,
            )
        if base is None:
            return PromotionMateProbe(
                eligible=True,
                positions_visited=meter.positions,
                replay_rejects=replay_rejects,
            )

        if prefix_promoted_square is not None and move_budget - len(base.moves) >= 1:
            prefixed_candidate = _PromotionCandidate(
                base.board.copy(stack=False),
                base.moves,
                move_budget - len(base.moves),
                prefix_promoted_square,
            )
            first_reuse_move = 0
            for reuse_limit in REUSE_MOVE_ROUNDS:
                mate = complete(
                    prefixed_candidate,
                    first_reuse_move,
                    reuse_limit,
                )
                if mate is not None:
                    return PromotionMateProbe(
                        mate,
                        True,
                        meter.positions,
                        setup_states,
                        1,
                        completion_probes,
                        replay_rejects=replay_rejects,
                    )
                first_reuse_move = reuse_limit

        candidates, route_mate = collect_setup_promotions(base)
        candidate_count = len(candidates)
        if route_mate is not None:
            return PromotionMateProbe(
                route_mate,
                True,
                meter.positions,
                setup_states,
                candidate_count,
                completion_probes,
                replay_rejects=replay_rejects,
            )
        ordered_candidates = score_promotions(candidates)
        first_reuse_move = 0
        for reuse_limit in REUSE_MOVE_ROUNDS:
            for offset in range(
                0,
                len(ordered_candidates),
                COMPLETION_CANDIDATE_BATCH,
            ):
                batch = ordered_candidates[
                    offset : offset + COMPLETION_CANDIDATE_BATCH
                ]
                for candidate in batch:
                    mate = complete(candidate, first_reuse_move, reuse_limit)
                    if mate is not None:
                        return PromotionMateProbe(
                            mate,
                            True,
                            meter.positions,
                            setup_states,
                            candidate_count,
                            completion_probes,
                            replay_rejects=replay_rejects,
                        )
            first_reuse_move = reuse_limit
    except _ProbeCancelled:
        return PromotionMateProbe(
            eligible=True,
            positions_visited=meter.positions,
            setup_states=setup_states,
            promotion_candidates=candidate_count,
            completion_probes=completion_probes,
            cancelled=True,
            replay_rejects=replay_rejects,
        )
    except _ProbeWorkLimit:
        return PromotionMateProbe(
            eligible=True,
            positions_visited=meter.positions,
            setup_states=setup_states,
            promotion_candidates=candidate_count,
            completion_probes=completion_probes,
            work_limit_reached=True,
            replay_rejects=replay_rejects,
        )
    return PromotionMateProbe(
        eligible=True,
        positions_visited=meter.positions,
        setup_states=setup_states,
        promotion_candidates=candidate_count,
        completion_probes=completion_probes,
        replay_rejects=replay_rejects,
    )
