from __future__ import annotations

import chess
import pytest

import scottish_progressive.search as search_module
from scottish_progressive.model import ProgressiveState
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.search import (
    SearchLimits,
    SeriesSearcher,
    UNKNOWN_PROOF_BOUNDS,
    analyze,
)


def _searcher() -> SeriesSearcher:
    return SeriesSearcher(
        SearchLimits(
            depth_series=4,
            max_series_per_node=32,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        baseline_profile(),
    )


def test_later_white_root_child_uses_one_point_probe_without_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = _searcher()
    state = ProgressiveState.initial()
    windows: list[tuple[int, int]] = []

    def probe(
        _state: ProgressiveState,
        _depth: int,
        alpha: int,
        beta: int,
        _ply: int,
    ) -> tuple[int, tuple[object, ...], tuple[int, int]]:
        windows.append((alpha, beta))
        return 100, (), UNKNOWN_PROOF_BOUNDS

    monkeypatch.setattr(searcher, "_minimax", probe)
    score, _, _ = searcher._search_root_child_with_pvs(
        state,
        2,
        100,
        2_000_000,
        1,
        parent_mover=True,
        has_prior_child=True,
    )

    assert score == 100
    assert windows == [(100, 101)]
    assert searcher.stats.root_pvs_zero_window_searches == 1
    assert searcher.stats.root_pvs_researches == 0


def test_later_black_root_child_uses_one_point_probe_without_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = _searcher()
    windows: list[tuple[int, int]] = []

    def probe(
        _state: ProgressiveState,
        _depth: int,
        alpha: int,
        beta: int,
        _ply: int,
    ) -> tuple[int, tuple[object, ...], tuple[int, int]]:
        windows.append((alpha, beta))
        return 100, (), UNKNOWN_PROOF_BOUNDS

    monkeypatch.setattr(searcher, "_minimax", probe)
    score, _, _ = searcher._search_root_child_with_pvs(
        ProgressiveState.initial(),
        2,
        -2_000_000,
        100,
        1,
        parent_mover=False,
        has_prior_child=True,
    )

    assert score == 100
    assert windows == [(99, 100)]
    assert searcher.stats.root_pvs_zero_window_searches == 1
    assert searcher.stats.root_pvs_researches == 0


def test_improving_root_probe_rolls_back_then_researches_full_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = _searcher()
    state = ProgressiveState.initial()
    key = searcher._tt_key(state)
    original = search_module._TTEntry(
        depth=1,
        score=7,
        bound=search_module.Bound.EXACT,
        pv=(),
        proof_bounds=UNKNOWN_PROOF_BOUNDS,
    )
    speculative = search_module._TTEntry(
        depth=2,
        score=101,
        bound=search_module.Bound.LOWER,
        pv=(),
        proof_bounds=UNKNOWN_PROOF_BOUNDS,
    )
    exact = search_module._TTEntry(
        depth=2,
        score=450,
        bound=search_module.Bound.EXACT,
        pv=(),
        proof_bounds=UNKNOWN_PROOF_BOUNDS,
    )
    searcher._tt[key] = original
    windows: list[tuple[int, int]] = []

    def probe(
        _state: ProgressiveState,
        _depth: int,
        alpha: int,
        beta: int,
        _ply: int,
    ) -> tuple[int, tuple[object, ...], tuple[int, int]]:
        windows.append((alpha, beta))
        if len(windows) == 1:
            assert searcher._tt[key] is original
            searcher._write_tt(key, speculative)
            return 101, (), UNKNOWN_PROOF_BOUNDS
        assert searcher._tt[key] is original
        searcher._write_tt(key, exact)
        return 450, (), UNKNOWN_PROOF_BOUNDS

    monkeypatch.setattr(searcher, "_minimax", probe)
    score, _, _ = searcher._search_root_child_with_pvs(
        state,
        2,
        100,
        2_000_000,
        1,
        parent_mover=True,
        has_prior_child=True,
    )

    assert score == 450
    assert windows == [(100, 101), (100, 2_000_000)]
    assert searcher._tt[key] is exact
    assert searcher.stats.root_pvs_researches == 1
    assert searcher.stats.root_pvs_tt_writes_rolled_back == 1
    assert searcher._tt_transaction_stack == []


def test_interrupted_root_probe_restores_tt_and_transaction_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searcher = _searcher()
    state = ProgressiveState.initial()
    key = searcher._tt_key(state)
    original = search_module._TTEntry(
        depth=1,
        score=7,
        bound=search_module.Bound.EXACT,
        pv=(),
        proof_bounds=UNKNOWN_PROOF_BOUNDS,
    )
    replacement = search_module._TTEntry(
        depth=2,
        score=101,
        bound=search_module.Bound.LOWER,
        pv=(),
        proof_bounds=UNKNOWN_PROOF_BOUNDS,
    )
    searcher._tt[key] = original

    def interrupt(*_args: object) -> tuple[int, tuple[object, ...], tuple[int, int]]:
        searcher._write_tt(key, replacement)
        raise search_module._Timeout

    monkeypatch.setattr(searcher, "_minimax", interrupt)
    with pytest.raises(search_module._Timeout):
        searcher._search_root_child_with_pvs(
            state,
            2,
            100,
            2_000_000,
            1,
            parent_mover=True,
            has_prior_child=True,
        )

    assert searcher._tt[key] is original
    assert searcher._tt_transaction_stack == []


def _signature(result: object) -> tuple[object, ...]:
    return (
        result.score,
        result.best_series.machine_notation if result.best_series else None,
        tuple(item.machine_notation for item in result.principal_variation),
        tuple(
            (
                item.series.machine_notation,
                item.score,
                tuple(pv.machine_notation for pv in item.principal_variation),
                item.proof_bounds,
            )
            for item in result.alternatives
        ),
        result.completed_depth,
        result.proof,
        result.forced,
        result.exact_width,
        result.timed_out,
        result.work_limit_reached,
        result.root_scores_complete,
    )


def test_initial_depth_three_preserves_exact_search_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = SearchLimits(
        depth_series=3,
        max_series_per_node=32,
        max_generation_positions=2_000_000,
        collect_all_root_scores=False,
        native_threads=1,
    )
    monkeypatch.setattr(search_module, "ROOT_PVS_ENABLED", False)
    baseline = analyze(ProgressiveState.initial(), limits, baseline_profile())
    monkeypatch.setattr(search_module, "ROOT_PVS_ENABLED", True)
    candidate = analyze(ProgressiveState.initial(), limits, baseline_profile())

    assert _signature(candidate) == _signature(baseline)
    assert candidate.stats.root_pvs_zero_window_searches > 0


@pytest.mark.parametrize("series_number", range(1, 9))
def test_root_pvs_is_eligible_for_final_iteration_at_later_roots(
    series_number: int,
) -> None:
    searcher = _searcher()
    all_scores_searcher = SeriesSearcher(
        SearchLimits(depth_series=4, collect_all_root_scores=True),
        baseline_profile(),
    )
    turn = "w" if series_number % 2 else "b"
    state = ProgressiveState.from_fen(
        f"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR {turn} KQkq - 0 1",
        series_number,
    )

    assert not searcher._root_pvs_eligible(state, 3)
    assert searcher._root_pvs_eligible(state, 4)
    assert not all_scores_searcher._root_pvs_eligible(state, 4)


@pytest.mark.parametrize(
    "history",
    (
        (("e2e4",),),
        (("e2e4",), ("f7f5", "e8f7")),
    ),
)
def test_later_progressive_root_preserves_exact_search_signature(
    monkeypatch: pytest.MonkeyPatch,
    history: tuple[tuple[str, ...], ...],
) -> None:
    state = ProgressiveState.initial()
    for series in history:
        state = play_series(state, series).final_state
    limits = SearchLimits(
        depth_series=3,
        max_series_per_node=32,
        max_generation_positions=3_000_000,
        collect_all_root_scores=False,
        native_threads=1,
    )
    monkeypatch.setattr(search_module, "ROOT_PVS_ENABLED", False)
    baseline = analyze(state, limits, baseline_profile())
    monkeypatch.setattr(search_module, "ROOT_PVS_ENABLED", True)
    candidate = analyze(state, limits, baseline_profile())

    assert _signature(candidate) == _signature(baseline)
    assert candidate.stats.root_pvs_zero_window_searches > 0


def test_later_required_prefix_preserves_exact_search_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
    limits = SearchLimits(
        depth_series=3,
        max_series_per_node=32,
        max_generation_positions=3_000_000,
        collect_all_root_scores=False,
        native_threads=1,
    )
    monkeypatch.setattr(search_module, "ROOT_PVS_ENABLED", False)
    baseline = analyze(
        state,
        limits,
        baseline_profile(),
        required_prefix=("f7f5",),
    )
    monkeypatch.setattr(search_module, "ROOT_PVS_ENABLED", True)
    candidate = analyze(
        state,
        limits,
        baseline_profile(),
        required_prefix=("f7f5",),
    )

    assert _signature(candidate) == _signature(baseline)
    assert candidate.stats.root_pvs_zero_window_searches > 0


def test_generation_cache_separates_exact_fen_clocks() -> None:
    searcher = _searcher()
    first = ProgressiveState.from_fen(chess.STARTING_FEN, 1)
    second = ProgressiveState.from_fen(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 9 7",
        1,
    )

    assert first.transposition_key == second.transposition_key
    first_series = searcher._ordered_generated(first, ply_from_root=1)
    second_series = searcher._ordered_generated(second, ply_from_root=1)
    first_knight = next(
        item.materialize() if hasattr(item, "materialize") else item
        for item in first_series
        if item.machine_notation == "g1f3"
    )
    second_knight = next(
        item.materialize() if hasattr(item, "materialize") else item
        for item in second_series
        if item.machine_notation == "g1f3"
    )

    assert first_knight.final_state.board.halfmove_clock == 1
    assert second_knight.final_state.board.halfmove_clock == 10
    assert first_knight.final_state.board.fullmove_number == 1
    assert second_knight.final_state.board.fullmove_number == 7
    assert searcher.stats.series_generation_cache_hits == 0


@pytest.mark.parametrize(
    "fen",
    (
        "8/B7/k7/8/3q4/8/8/3bQ2K w - - 0 1",
        "8/7B/K1b5/8/3R4/2k5/6r1/8 w - - 0 1",
    ),
)
def test_root_pvs_transposition_pv_replays_from_exact_clock_state(
    monkeypatch: pytest.MonkeyPatch,
    fen: str,
) -> None:
    state = ProgressiveState.from_fen(fen, 1)
    limits = SearchLimits(
        depth_series=4,
        max_series_per_node=4,
        collect_all_root_scores=False,
        native_threads=1,
    )
    results = []
    for enabled in (False, True):
        monkeypatch.setattr(search_module, "ROOT_PVS_ENABLED", enabled)
        result = analyze(state, limits, baseline_profile())
        replay_state = state
        for stored in result.principal_variation:
            replayed = play_series(replay_state, stored.moves)
            assert replayed.final_state.pfen == stored.final_state.pfen
            assert replayed.san == stored.san
            assert replayed.ended_by_check == stored.ended_by_check
            assert replayed.outcome == stored.outcome
            replay_state = replayed.final_state
        results.append(result)

    assert _signature(results[1]) == _signature(results[0])
