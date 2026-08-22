from __future__ import annotations

from dataclasses import asdict
import os
import random
from unittest.mock import patch

import chess
import pytest

from scottish_progressive.model import ProgressiveState, SeriesResult
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.search import (
    UNKNOWN_PROOF_BOUNDS,
    Bound,
    SearchLimits,
    SeriesSearcher,
    _AdjudicationPending,
    _Timeout,
    _TTEntry,
    _WorkLimit,
    analyze,
)


WHITE_ANCHOR = ProgressiveState.from_fen(
    "3n4/5k2/5N2/K7/R7/8/3q4/8 w - - 0 1",
    1,
)
BLACK_ANCHOR = ProgressiveState.from_fen(
    "8/1Bn5/8/8/4R2K/k7/1q6/8 b - - 0 1",
    2,
)
BEST_ONLY_ANCHOR = ProgressiveState.from_fen(
    "8/6R1/5K2/8/1n2B3/1k6/8/4r3 w - - 0 1",
    1,
)


def _series_signature(series: SeriesResult) -> tuple[object, ...]:
    return (
        series.machine_notation,
        series.san,
        series.final_state.pfen,
        series.ended_by_check,
        series.outcome,
        series.unused_moves,
        series.transposition_count,
    )


def _semantic_signature(result: object) -> tuple[object, ...]:
    return (
        result.score,
        _series_signature(result.best_series) if result.best_series else None,
        tuple(_series_signature(item) for item in result.principal_variation),
        tuple(
            (
                _series_signature(item.series),
                item.score,
                tuple(_series_signature(series) for series in item.principal_variation),
                item.proof_bounds,
                item.proof,
            )
            for item in result.alternatives
        ),
        result.requested_depth,
        result.completed_depth,
        result.exact_width,
        result.timed_out,
        result.work_limit_reached,
        result.root_scores_complete,
        result.proof,
        result.forced,
        result.adjudication_status,
        result.classification,
        result.confidence,
    )


def _full_window_child(
    self: SeriesSearcher,
    state: ProgressiveState,
    depth: int,
    alpha: int,
    beta: int,
    ply_from_root: int,
    *,
    parent_mover: chess.Color,
    has_prior_child: bool,
) -> tuple[int, tuple[SeriesResult, ...], tuple[int, int]]:
    del parent_mover, has_prior_child
    return self._minimax(state, depth, alpha, beta, ply_from_root)


def _search(
    state: ProgressiveState,
    *,
    pvs: bool,
    collect_all: bool,
) -> object:
    limits = SearchLimits(
        depth_series=4,
        max_series_per_node=4,
        collect_all_root_scores=collect_all,
        native_threads=1,
    )
    if pvs:
        return analyze(state, limits, baseline_profile())
    with patch.object(SeriesSearcher, "_search_child_with_pvs", _full_window_child):
        return analyze(state, limits, baseline_profile())


def _random_sparse_states(count: int) -> tuple[ProgressiveState, ...]:
    rng = random.Random(20260821)
    states: list[ProgressiveState] = []
    seen: set[str] = set()
    while len(states) < count:
        squares = rng.sample(list(chess.SQUARES), 6)
        board = chess.Board(None)
        board.turn = chess.WHITE if len(states) % 2 == 0 else chess.BLACK
        board.set_piece_at(squares[0], chess.Piece(chess.KING, chess.WHITE))
        board.set_piece_at(squares[1], chess.Piece(chess.KING, chess.BLACK))
        board.set_piece_at(
            squares[2],
            chess.Piece(rng.choice((chess.ROOK, chess.QUEEN)), chess.WHITE),
        )
        board.set_piece_at(
            squares[3],
            chess.Piece(rng.choice((chess.ROOK, chess.QUEEN)), chess.BLACK),
        )
        board.set_piece_at(
            squares[4],
            chess.Piece(rng.choice((chess.BISHOP, chess.KNIGHT)), chess.WHITE),
        )
        board.set_piece_at(
            squares[5],
            chess.Piece(rng.choice((chess.BISHOP, chess.KNIGHT)), chess.BLACK),
        )
        if not board.is_valid() or board.is_game_over(claim_draw=False):
            continue
        state = ProgressiveState(board, 1 if board.turn == chess.WHITE else 2)
        if state.pfen in seen:
            continue
        seen.add(state.pfen)
        states.append(state)
    return tuple(states)


def test_fixed_exact_parity_anchors() -> None:
    cases = (
        (
            WHITE_ANCHOR,
            True,
            -837,
            "a5b5",
            (
                "a5b5",
                "d2d4/d4f6",
                "a4f4/b5b4/f4f6",
                "f7e7/e7f6/d8c6",
            ),
        ),
        (BLACK_ANCHOR, True, 426, "b2b1/b1e4", None),
    )
    for state, collect_all, score, best, pv in cases:
        baseline = _search(state, pvs=False, collect_all=collect_all)
        candidate = _search(state, pvs=True, collect_all=collect_all)
        assert _semantic_signature(candidate) == _semantic_signature(baseline)
        assert candidate.score == score
        assert candidate.best_series.machine_notation == best
        if pv is not None:
            assert tuple(
                item.machine_notation for item in candidate.principal_variation
            ) == pv

    baseline = _search(BEST_ONLY_ANCHOR, pvs=False, collect_all=False)
    candidate = _search(BEST_ONLY_ANCHOR, pvs=True, collect_all=False)
    assert _semantic_signature(candidate) == _semantic_signature(baseline)
    second = candidate.alternatives[1]
    assert second.series.machine_notation == "e4c2"
    assert second.score == -837


def test_512_sparse_boundaries_match_full_alpha_beta_exactly() -> None:
    candidate_work = 0
    baseline_work = 0
    zero_window_searches = 0
    for index, state in enumerate(_random_sparse_states(512)):
        collect_all = bool(index % 2)
        baseline = _search(state, pvs=False, collect_all=collect_all)
        candidate = _search(state, pvs=True, collect_all=collect_all)
        assert _semantic_signature(candidate) == _semantic_signature(baseline), (
            index,
            state.pfen,
        )
        baseline_work += baseline.stats.work_positions
        candidate_work += candidate.stats.work_positions
        zero_window_searches += candidate.stats.pvs_zero_window_searches

    assert zero_window_searches > 0
    assert candidate_work <= baseline_work


def test_tt_journals_restore_repeated_and_nested_writes() -> None:
    searcher = SeriesSearcher(SearchLimits())
    first_key = searcher._tt_key(WHITE_ANCHOR)
    second_key = searcher._tt_key(BLACK_ANCHOR)
    original = _TTEntry(1, 10, Bound.EXACT, (), UNKNOWN_PROOF_BOUNDS)
    outer_value = _TTEntry(2, 20, Bound.LOWER, (), UNKNOWN_PROOF_BOUNDS)
    inner_value = _TTEntry(3, 30, Bound.UPPER, (), UNKNOWN_PROOF_BOUNDS)
    searcher._tt[first_key] = original

    outer = searcher._begin_tt_transaction()
    searcher._write_tt(first_key, outer_value)
    searcher._write_tt(first_key, inner_value)
    searcher._write_tt(second_key, outer_value)
    inner = searcher._begin_tt_transaction()
    searcher._write_tt(first_key, original)
    searcher._write_tt(second_key, inner_value)

    assert searcher._rollback_tt_transaction(inner) == 2
    assert searcher._tt == {first_key: inner_value, second_key: outer_value}
    assert searcher._rollback_tt_transaction(outer) == 3
    assert searcher._tt == {first_key: original}
    assert searcher._tt_transaction_stack == []


@pytest.mark.parametrize(
    "interruption",
    (_Timeout, _WorkLimit, _AdjudicationPending),
)
def test_zero_window_interruption_rolls_back_every_tt_write(
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[Exception],
) -> None:
    searcher = SeriesSearcher(SearchLimits(depth_series=3))
    original_key = searcher._tt_key(WHITE_ANCHOR)
    added_key = searcher._tt_key(BLACK_ANCHOR)
    original = _TTEntry(1, 10, Bound.EXACT, (), UNKNOWN_PROOF_BOUNDS)
    replacement = _TTEntry(2, 20, Bound.LOWER, (), UNKNOWN_PROOF_BOUNDS)
    nested = _TTEntry(3, 30, Bound.UPPER, (), UNKNOWN_PROOF_BOUNDS)
    searcher._tt[original_key] = original

    def interrupted_minimax(*args: object, **kwargs: object) -> object:
        del args, kwargs
        searcher._write_tt(original_key, replacement)
        searcher._write_tt(added_key, replacement)
        inner = searcher._begin_tt_transaction()
        searcher._write_tt(original_key, nested)
        searcher._write_tt(added_key, nested)
        assert searcher._rollback_tt_transaction(inner) == 2
        raise interruption()

    monkeypatch.setattr(searcher, "_minimax", interrupted_minimax)
    with pytest.raises(interruption):
        searcher._search_child_with_pvs(
            WHITE_ANCHOR,
            2,
            -100,
            100,
            1,
            parent_mover=chess.WHITE,
            has_prior_child=True,
        )

    assert searcher._tt == {original_key: original}
    assert searcher._tt_transaction_stack == []
    assert searcher.stats.pvs_zero_window_searches == 1
    assert searcher.stats.pvs_researches == 0
    assert searcher.stats.pvs_tt_writes_rolled_back == 2


@pytest.mark.parametrize("probe_score", (-5, 5))
def test_zero_window_rolls_back_before_bound_return_or_full_research(
    monkeypatch: pytest.MonkeyPatch,
    probe_score: int,
) -> None:
    searcher = SeriesSearcher(SearchLimits(depth_series=3))
    original_key = searcher._tt_key(WHITE_ANCHOR)
    added_key = searcher._tt_key(BLACK_ANCHOR)
    original = _TTEntry(1, 10, Bound.EXACT, (), UNKNOWN_PROOF_BOUNDS)
    speculative = _TTEntry(2, 20, Bound.LOWER, (), UNKNOWN_PROOF_BOUNDS)
    persistent = _TTEntry(3, 30, Bound.EXACT, (), UNKNOWN_PROOF_BOUNDS)
    searcher._tt[original_key] = original
    calls = 0

    def staged_minimax(*args: object, **kwargs: object) -> object:
        nonlocal calls
        del args, kwargs
        calls += 1
        if calls == 1:
            searcher._write_tt(original_key, speculative)
            searcher._write_tt(added_key, speculative)
            return probe_score, (), UNKNOWN_PROOF_BOUNDS
        assert searcher._tt == {original_key: original}
        assert searcher._tt_transaction_stack == []
        searcher._write_tt(added_key, persistent)
        return 4, (), UNKNOWN_PROOF_BOUNDS

    monkeypatch.setattr(searcher, "_minimax", staged_minimax)
    result = searcher._search_child_with_pvs(
        WHITE_ANCHOR,
        2,
        0,
        10,
        1,
        parent_mover=chess.WHITE,
        has_prior_child=True,
    )

    assert searcher.stats.pvs_tt_writes_rolled_back == 2
    if probe_score < 0:
        assert calls == 1
        assert result[0] == probe_score
        assert searcher._tt == {original_key: original}
        assert searcher.stats.pvs_researches == 0
    else:
        assert calls == 2
        assert result[0] == 4
        assert searcher._tt == {original_key: original, added_key: persistent}
        assert searcher.stats.pvs_researches == 1


def test_pvs_stats_keep_actual_work_and_report_rolled_back_writes() -> None:
    baseline = _search(WHITE_ANCHOR, pvs=False, collect_all=True)
    candidate = _search(WHITE_ANCHOR, pvs=True, collect_all=True)
    baseline_stats = asdict(baseline.stats)
    candidate_stats = asdict(candidate.stats)

    assert baseline_stats["pvs_zero_window_searches"] == 0
    assert baseline_stats["pvs_researches"] == 0
    assert baseline_stats["pvs_tt_writes_rolled_back"] == 0
    assert candidate_stats["pvs_zero_window_searches"] > 0
    assert candidate_stats["pvs_tt_writes_rolled_back"] > 0
    assert candidate.stats.generation_positions == candidate.stats.work_positions


def _hard_s4() -> ProgressiveState:
    state = ProgressiveState.initial()
    for series in (
        ("g1f3",),
        ("e7e6", "d8f6"),
        ("d2d4", "c1g5", "g5f6"),
    ):
        state = play_series(state, series).final_state
    return state


def test_hard_s4_depth_five_completes_with_exact_safety_under_production_work() -> None:
    if os.environ.get("SPC_RUN_TRANSACTIONAL_PVS_GATES") != "1":
        pytest.skip("set SPC_RUN_TRANSACTIONAL_PVS_GATES=1 for the hard PVS gate")

    result = analyze(
        _hard_s4(),
        SearchLimits(
            depth_series=5,
            max_series_per_node=32,
            time_limit_seconds=180.0,
            max_generation_positions=10_000_000,
            collect_all_root_scores=False,
            native_threads=16,
        ),
        baseline_profile(),
    )
    # Later-root transactional scouts reject 31 non-improving S4 candidates
    # without changing the exact D5 result.
    assert result.stats.work_positions == 8_663_967
    assert result.stats.root_pvs_zero_window_searches == 31
    assert result.stats.root_safety_screen_positions == 831_549
    assert (
        result.stats.native_series_mate_positions
        + result.stats.native_series_mate_edges
        == 794_493
    )
    assert result.stats.native_series_mate_calls == 2
    assert result.stats.native_series_mate_exhausted == 2
    assert result.stats.native_series_mate_cache_hits == 3
    assert result.completed_depth == 5
    assert not result.work_limit_reached
    assert result.score == -1808
    assert result.best_series.machine_notation == "b8c6/c6d4/g8f6/d4f3"
    assert tuple(
        item.machine_notation for item in result.principal_variation
    ) == (
        "b8c6/c6d4/g8f6/d4f3",
        "g2f3/d1d6/e1d2/h1g1/d6f8",
        "e8f8/a7a5/a8a6/a6c6/f8e7/c6d6",
        "d2e1/e2e4/e4e5/b1c3/g1g7/f1h3/e5d6",
        "e7f8/f6d5/d5b4/c7d6/h8g8/g8g7/g7g5/b4c2",
    )


def test_s7_work_and_semantics_do_not_regress() -> None:
    if os.environ.get("SPC_RUN_TRANSACTIONAL_PVS_GATES") != "1":
        pytest.skip("set SPC_RUN_TRANSACTIONAL_PVS_GATES=1 for the S7 PVS gate")

    state = ProgressiveState.from_fen(
        "rnk3nr/pp3ppp/8/8/8/1Pp1P3/P1PP1PPP/R1b1K1NR w K - 0 13",
        7,
    )
    limits = SearchLimits(
        depth_series=4,
        max_series_per_node=32,
        time_limit_seconds=180.0,
        max_generation_positions=10_000_000,
        collect_all_root_scores=False,
        native_threads=16,
    )
    baseline = _search_with_limits(state, limits=limits, pvs=False)
    candidate = _search_with_limits(state, limits=limits, pvs=True)
    assert _semantic_signature(candidate) == _semantic_signature(baseline)
    assert candidate.stats.work_positions <= baseline.stats.work_positions


def _search_with_limits(
    state: ProgressiveState,
    *,
    limits: SearchLimits,
    pvs: bool,
) -> object:
    if pvs:
        return analyze(state, limits, baseline_profile())
    with patch.object(SeriesSearcher, "_search_child_with_pvs", _full_window_child):
        return analyze(state, limits, baseline_profile())
