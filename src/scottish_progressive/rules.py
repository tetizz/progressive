from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

import chess

from .model import Outcome, ProgressiveState, SeriesResult, boundary_fen


@dataclass(slots=True)
class GenerationStats:
    positions_visited: int = 0
    raw_series: int = 0
    unique_series: int = 0
    transpositions_merged: int = 0
    checking_series: int = 0
    checkmates: int = 0
    stalemates: int = 0


class SeriesLegalityError(ValueError):
    pass


class GenerationCancelled(Exception):
    pass


@dataclass(slots=True)
class _FrontierState:
    board: chess.Board
    moves: tuple[str, ...]
    sans: tuple[str, ...]
    ep_candidates: dict[int, int]
    made_progress: bool
    path_count: int = 1


def _legal_move_variants(
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


def _has_legal_move(board: chess.Board, ep_targets: Iterable[int] = ()) -> bool:
    return bool(_legal_move_variants(board, ep_targets))


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


def _merged_series_generation(
    state: ProgressiveState,
    counters: GenerationStats,
    should_stop: Callable[[], bool] | None = None,
) -> list[SeriesResult]:
    """Dynamic-programming generator that merges intra-series states early."""

    mover = state.board.turn
    root = state.board.copy(stack=False)
    root.ep_square = state.ep_targets[0] if len(state.ep_targets) == 1 else None
    frontier = [_FrontierState(root, (), (), {}, False)]
    completed: list[SeriesResult] = []

    def record(result: SeriesResult, path_count: int) -> None:
        weighted = result.with_transposition_count(path_count)
        counters.raw_series += path_count
        if result.ended_by_check:
            counters.checking_series += path_count
        if result.outcome == Outcome.CHECKMATE:
            counters.checkmates += path_count
        elif result.outcome == Outcome.STALEMATE:
            counters.stalemates += path_count
        completed.append(weighted)

    while frontier:
        if should_stop is not None and should_stop():
            raise GenerationCancelled
        following: dict[tuple[object, ...], _FrontierState] = {}
        for item in frontier:
            if should_stop is not None and should_stop():
                raise GenerationCancelled
            counters.positions_visited += 1
            variants = _legal_move_variants(
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
                )
                continue

            for move, required_ep in variants:
                current = item.board.copy(stack=False)
                current.ep_square = required_ep
                san = current.san(move)
                piece = current.piece_at(move.from_square)
                is_pawn_move = (
                    piece is not None and piece.piece_type == chess.PAWN
                )
                is_capture = current.is_capture(move)

                next_candidates = dict(item.ep_candidates)
                if move.from_square in next_candidates:
                    del next_candidates[move.from_square]
                if is_pawn_move and abs(move.to_square - move.from_square) == 16:
                    next_candidates[move.to_square] = (
                        move.from_square + move.to_square
                    ) // 2

                current.push(move)
                delivered_check = current.is_check()
                next_moves = item.moves + (move.uci(),)
                next_sans = item.sans + (san,)
                next_progress = item.made_progress or is_pawn_move or is_capture
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
                    )
                    continue

                current.turn = mover
                current.ep_square = None
                key = (
                    boundary_fen(current, ()),
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
                )
                incumbent = following.get(key)
                if incumbent is None:
                    following[key] = candidate
                    continue
                total_paths = incumbent.path_count + candidate.path_count
                chosen = candidate if candidate.moves < incumbent.moves else incumbent
                following[key] = _FrontierState(
                    chosen.board,
                    chosen.moves,
                    chosen.sans,
                    chosen.ep_candidates,
                    chosen.made_progress,
                    total_paths,
                )
        frontier = list(following.values())

    merged: dict[tuple[object, ...], SeriesResult] = {}
    counts: dict[tuple[object, ...], int] = {}
    for result in completed:
        key = (
            result.final_state.transposition_key,
            result.outcome,
            result.ended_by_check,
        )
        counts[key] = counts.get(key, 0) + result.transposition_count
        incumbent = merged.get(key)
        candidate_rank = (-result.used_moves, result.machine_notation)
        incumbent_rank = (
            (-incumbent.used_moves, incumbent.machine_notation)
            if incumbent is not None
            else None
        )
        if incumbent is None or candidate_rank < incumbent_rank:
            merged[key] = result

    unique = [result.with_transposition_count(counts[key]) for key, result in merged.items()]
    counters.unique_series = len(unique)
    counters.transpositions_merged = counters.raw_series - len(unique)
    return sorted(unique, key=lambda result: result.machine_notation)


def generate_series(
    state: ProgressiveState,
    *,
    merge_transpositions: bool = True,
    stats: GenerationStats | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[SeriesResult]:
    """Generates complete legal series from a boundary position.

    A checking move, checkmate, or progressive stalemate may produce a result
    shorter than the nominal series budget. When ``merge_transpositions`` is
    true, different move orders reaching the same full progressive state are
    represented once and counted on ``SeriesResult.transposition_count``.
    """

    counters = stats if stats is not None else GenerationStats()
    if merge_transpositions:
        return _merged_series_generation(state, counters, should_stop)
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
        counters.positions_visited += 1
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

    root_board = state.board.copy(stack=False)
    root_board.ep_square = state.ep_targets[0] if len(state.ep_targets) == 1 else None
    walk(root_board, (), (), {}, False, first_move=True)

    counters.unique_series = len(results)
    return sorted(results, key=lambda result: result.machine_notation)


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
                boundary_fen(child, ()),
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
