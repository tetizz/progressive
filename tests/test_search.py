from __future__ import annotations

from dataclasses import replace

import chess
import pytest

import scottish_progressive.search as search_module
from scottish_progressive.evaluation import evaluate, probe_series_reach
from scottish_progressive.model import Outcome, ProgressiveState
from scottish_progressive.rules import (
    GenerationCancelled,
    generate_series,
    play_series,
)
from scottish_progressive.search import (
    MATE_SCORE,
    QUIET_ADJUDICATION_POSITION_LIMIT,
    SERIES_GENERATION_CACHE_CAPACITY,
    SearchLimits,
    SeriesSearcher,
    analyze,
)


def test_search_finds_immediate_seriesmate() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1", 1
    )
    result = analyze(state, SearchLimits(depth_series=1))
    assert result.score == MATE_SCORE - 1
    assert result.best_series is not None
    assert result.best_series.moves == ("g6g7",)
    assert result.best_series.outcome == Outcome.CHECKMATE
    assert result.forced == "white"


def test_best_only_root_mode_keeps_legal_mate_dominant() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1", 1
    )
    result = analyze(
        state,
        SearchLimits(depth_series=2, collect_all_root_scores=False),
    )

    assert result.score == MATE_SCORE - 1
    assert result.best_series is not None
    assert result.best_series.moves == ("g6g7",)
    assert result.best_series.outcome == Outcome.CHECKMATE
    assert result.proof == "white"
    assert result.forced == "white"
    assert not result.root_scores_complete


def test_terminal_mate_distance_prefers_faster_win_and_slower_loss() -> None:
    white_state = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1", 1
    )
    white_mate = next(
        item
        for item in generate_series(white_state)
        if item.outcome == Outcome.CHECKMATE
    )
    black_state = ProgressiveState.from_fen(
        "8/8/8/8/8/5kq1/8/7K b - - 0 1", 2
    )
    black_mate = next(
        item
        for item in generate_series(black_state)
        if item.outcome == Outcome.CHECKMATE
    )

    white_fast = SeriesSearcher._terminal_score(white_mate, chess.WHITE, 1)
    white_slow = SeriesSearcher._terminal_score(white_mate, chess.WHITE, 3)
    black_fast = SeriesSearcher._terminal_score(black_mate, chess.BLACK, 1)
    black_slow = SeriesSearcher._terminal_score(black_mate, chess.BLACK, 3)
    assert white_fast == MATE_SCORE - 1 > white_slow == MATE_SCORE - 3
    assert black_fast == -MATE_SCORE + 1 < black_slow == -MATE_SCORE + 3


def test_search_scores_already_checkmated_side_as_loser() -> None:
    state = ProgressiveState.from_fen(
        "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1", 2
    )
    result = analyze(state, SearchLimits(depth_series=1))
    assert result.score == MATE_SCORE - 1
    assert result.forced == "white"


def test_series_reach_is_explicit_in_evaluation() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/4Q3/K7 w - - 0 1", 1
    )
    probe = probe_series_reach(state, True, max_moves=1, node_limit=512)
    breakdown = evaluate(state)
    assert probe.distance == 1
    assert breakdown.white_check_distance == 1


def test_branch_cap_marks_search_selective() -> None:
    state = ProgressiveState.initial()
    result = analyze(
        state, SearchLimits(depth_series=1, max_series_per_node=3)
    )
    assert not result.exact_width
    assert result.stats.branch_caps == 1
    assert len(result.alternatives) == 3


def test_selective_terminal_mate_proof_is_labeled_forced() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1", 1
    )
    result = analyze(
        state, SearchLimits(depth_series=1, max_series_per_node=1)
    )
    assert result.score == MATE_SCORE - 1
    assert not result.exact_width
    assert result.proof == "white"
    assert result.forced == "white"
    assert result.confidence == "forced/proven by sound search proof bounds"


def test_search_is_reproducible() -> None:
    state = ProgressiveState.initial()
    first = analyze(state, SearchLimits(depth_series=1))
    second = analyze(state, SearchLimits(depth_series=1))
    assert first.score == second.score
    assert first.best_series is not None and second.best_series is not None
    assert first.best_series.moves == second.best_series.moves


def test_pv_ordering_keeps_generation_cache_canonical() -> None:
    state = ProgressiveState.initial()
    searcher = SeriesSearcher(
        SearchLimits(depth_series=1, max_series_per_node=8)
    )

    canonical = searcher._ordered_generated(state, ply_from_root=1)
    preferred = canonical[-1].machine_notation
    reordered = searcher._ordered_generated(
        state,
        ply_from_root=1,
        preferred_series=preferred,
    )
    canonical_again = searcher._ordered_generated(state, ply_from_root=1)

    assert reordered[0].machine_notation == preferred
    assert [item.machine_notation for item in canonical_again] == [
        item.machine_notation for item in canonical
    ]


def test_shallow_tt_entry_cannot_answer_a_deeper_search() -> None:
    state = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
    limits = SearchLimits(depth_series=2, max_series_per_node=4)
    warm = SeriesSearcher(limits)
    window = (-2 * MATE_SCORE, 2 * MATE_SCORE)

    shallow = warm._minimax(state, 1, *window, 1)
    assert warm._tt[state.transposition_key].depth == 1
    deep_after_shallow = warm._minimax(state, 2, *window, 1)
    assert warm._tt[state.transposition_key].depth == 2

    fresh = SeriesSearcher(limits)
    deep_fresh = fresh._minimax(state, 2, *window, 1)

    assert deep_after_shallow == deep_fresh
    assert deep_after_shallow[0] != shallow[0]


def test_iterative_pv_ordering_preserves_result_with_less_work(monkeypatch) -> None:
    state = ProgressiveState.initial()
    limits = SearchLimits(
        depth_series=3,
        max_series_per_node=6,
        max_generation_positions=250_000,
        collect_all_root_scores=False,
    )
    ordered = analyze(state, limits)

    monkeypatch.setattr(
        SeriesSearcher,
        "_prefer_series",
        staticmethod(lambda series, preferred_series: None),
    )
    static_only = analyze(state, limits)

    assert ordered.score == static_only.score
    assert ordered.proof == static_only.proof
    assert ordered.completed_depth == static_only.completed_depth == 3
    assert ordered.best_series is not None and static_only.best_series is not None
    assert ordered.best_series.moves == static_only.best_series.moves
    assert [item.machine_notation for item in ordered.principal_variation] == [
        item.machine_notation for item in static_only.principal_variation
    ]
    assert ordered.stats.nodes < static_only.stats.nodes
    assert ordered.stats.work_positions < static_only.stats.work_positions


def test_best_only_root_returns_only_scores_exact_under_full_root_search() -> None:
    state = ProgressiveState.initial()
    full = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=8,
            collect_all_root_scores=True,
        ),
    )
    best_only = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=8,
            collect_all_root_scores=False,
        ),
    )

    assert full.root_scores_complete
    assert full.stats.root_bound_candidates == 0
    assert not best_only.root_scores_complete
    assert best_only.stats.root_bound_candidates > 0
    assert best_only.score == full.score
    assert best_only.best_series is not None and full.best_series is not None
    assert best_only.best_series.moves == full.best_series.moves
    assert best_only.proof == full.proof
    full_evidence = {
        item.series.machine_notation: (item.score, item.proof_bounds)
        for item in full.alternatives
    }
    assert len(best_only.alternatives) < len(full.alternatives)
    for item in best_only.alternatives:
        assert full_evidence[item.series.machine_notation] == (
            item.score,
            item.proof_bounds,
        )


def test_best_only_black_root_preserves_minimizing_result_and_exact_evidence() -> None:
    state = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
    full = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=8,
            collect_all_root_scores=True,
        ),
    )
    best_only = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=8,
            collect_all_root_scores=False,
        ),
    )

    assert best_only.score == full.score
    assert best_only.best_series is not None and full.best_series is not None
    assert best_only.best_series.moves == full.best_series.moves
    assert best_only.proof == full.proof
    assert best_only.stats.root_bound_candidates > 0
    full_evidence = {
        item.series.machine_notation: (item.score, item.proof_bounds)
        for item in full.alternatives
    }
    for item in best_only.alternatives:
        assert full_evidence[item.series.machine_notation] == (
            item.score,
            item.proof_bounds,
        )


def test_incomplete_reach_probe_does_not_create_na3_scoring_artifact() -> None:
    initial = ProgressiveState.initial()
    na3 = play_series(initial, ("b1a3",)).final_state
    e4 = play_series(initial, ("e2e4",)).final_state

    na3_evaluation = evaluate(na3)
    e4_evaluation = evaluate(e4)

    assert na3_evaluation.white_check_distance == 2
    assert not na3_evaluation.reach_complete
    assert not e4_evaluation.reach_complete
    assert na3_evaluation.series_reach == e4_evaluation.series_reach == 0
    assert e4_evaluation.total > na3_evaluation.total


def test_evaluation_reach_probe_respects_shared_deterministic_budget() -> None:
    state = ProgressiveState.initial()
    breakdown = evaluate(state, max_reach_positions=7)

    assert breakdown.white_reach_nodes + breakdown.black_reach_nodes == 7
    assert not breakdown.reach_complete
    assert breakdown.series_reach == 0

    result = analyze(
        state,
        SearchLimits(
            depth_series=1,
            max_series_per_node=8,
            max_generation_positions=7,
        ),
    )
    assert result.work_limit_reached
    assert result.stats.static_evaluation_positions == 1
    assert result.stats.evaluation_reach_positions == 6
    assert result.stats.series_generation_positions == 0
    assert result.stats.quiet_adjudication_positions == 0
    assert result.stats.generation_positions == 7
    assert result.stats.incomplete_reach_evaluations == 1


def test_frontier_cap_finds_known_series_four_mate_before_full_materialization() -> None:
    state = ProgressiveState.from_fen(
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3",
        4,
    )
    cap = 64
    result = analyze(
        state,
        SearchLimits(depth_series=1, max_series_per_node=cap),
    )

    assert result.best_series is not None
    assert result.best_series.moves == (
        "c7c6",
        "d8b6",
        "f6e4",
        "b6f2",
    )
    assert result.best_series.outcome == Outcome.CHECKMATE
    assert result.stats.frontier_prunes > 0
    assert result.stats.frontier_states_pruned > 0
    assert result.stats.peak_frontier_states > cap
    assert result.stats.series_generation_positions <= (
        1 + cap * (state.moves_available - 1)
    )
    assert result.stats.generation_positions == (
        result.stats.series_generation_positions
        + result.stats.frontier_score_positions
        + result.stats.promotion_mate_positions
        + result.stats.static_evaluation_positions
        + result.stats.evaluation_reach_positions
        + result.stats.quiet_adjudication_positions
    )
    assert result.stats.work_positions == result.stats.generation_positions
    assert not result.exact_width


def test_wide_frontier_scoring_cannot_overshoot_combined_work_cap() -> None:
    state = ProgressiveState.from_fen(
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3",
        4,
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=1,
            max_series_per_node=64,
            max_generation_positions=1_736,
        ),
    )

    assert result.work_limit_reached
    assert not result.timed_out
    assert result.completed_depth == 0
    assert result.best_series is not None
    replayed = play_series(state, result.best_series.moves)
    assert replayed.final_state.transposition_key == (
        result.best_series.final_state.transposition_key
    )
    assert result.score == result.root_evaluation.total
    assert result.principal_variation == (result.best_series,)
    assert result.alternatives == ()
    assert result.proof is None
    assert result.forced is None
    assert not result.root_scores_complete
    assert result.stats.frontier_prunes > 0
    assert result.stats.peak_frontier_states > 64
    assert result.stats.work_positions == 1_736
    assert result.stats.series_generation_positions == 134
    assert result.stats.frontier_score_positions == 1_473
    assert result.stats.static_evaluation_positions == 1
    assert result.stats.evaluation_reach_positions == 128
    assert result.stats.quiet_adjudication_positions == 0
    assert result.stats.work_positions == (
        result.stats.series_generation_positions
        + result.stats.frontier_score_positions
        + result.stats.promotion_mate_positions
        + result.stats.static_evaluation_positions
        + result.stats.evaluation_reach_positions
        + result.stats.quiet_adjudication_positions
    )


def test_iterative_deepening_reuses_bounded_complete_series_frontier() -> None:
    state = ProgressiveState.from_fen(
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3",
        4,
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=3,
            max_series_per_node=64,
            max_generation_positions=500_000,
        ),
    )

    assert result.completed_depth == 3
    assert result.best_series is not None
    assert result.best_series.moves == (
        "c7c6",
        "d8b6",
        "f6e4",
        "b6f2",
    )
    # The immediate root mate means every iteration asks for the identical
    # expensive series-four frontier. Generate it once, then reuse it twice.
    assert result.stats.series_generation_cache_hits == 2
    assert result.stats.series_generation_positions == 135
    assert result.stats.frontier_score_positions == 1_473
    assert result.stats.static_evaluation_positions == 1
    assert result.stats.evaluation_reach_positions == 128
    assert result.stats.generation_positions == 1_737
    assert result.stats.series_generation_cache_peak <= (
        SERIES_GENERATION_CACHE_CAPACITY
    )


def test_complete_series_cache_enforces_weighted_lru_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(search_module, "SERIES_GENERATION_CACHE_CAPACITY", 2)
    searcher = SeriesSearcher(
        SearchLimits(depth_series=1, max_series_per_node=1)
    )
    states = (
        ProgressiveState.initial(),
        ProgressiveState.from_fen("7k/8/8/8/8/8/4Q3/K7 w - - 0 1", 1),
        ProgressiveState.from_fen("7k/8/8/8/8/8/4R3/K7 w - - 0 1", 1),
    )

    for state in states:
        assert searcher._ordered_generated(state, ply_from_root=1)

    assert searcher._series_generation_cache_weight == 2
    assert len(searcher._series_generation_cache) == 2
    assert searcher.stats.series_generation_cache_peak == 2
    assert searcher.stats.series_generation_cache_evictions == 1


def test_analysis_can_constrain_root_to_nonempty_series_prefix() -> None:
    state = ProgressiveState.from_fen(
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3",
        4,
    )
    prefix = ("c7c6", "d8b6")
    result = analyze(
        state,
        SearchLimits(depth_series=1, max_series_per_node=64),
        required_prefix=prefix,
    )

    assert result.required_prefix == prefix
    assert result.best_series is not None
    assert result.best_series.moves[: len(prefix)] == prefix
    assert all(
        alternative.series.moves[: len(prefix)] == prefix
        for alternative in result.alternatives
    )
    assert result.best_series.outcome == Outcome.CHECKMATE


def test_prefix_cache_reuse_cannot_bypass_generation_work_payment() -> None:
    state = ProgressiveState.from_fen(
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3",
        4,
    )
    prefix = ("c7c6", "d8b6")

    unpaid = analyze(
        state,
        SearchLimits(
            depth_series=3,
            max_series_per_node=64,
            max_generation_positions=195,
        ),
        required_prefix=prefix,
    )
    assert unpaid.work_limit_reached
    assert unpaid.completed_depth == 0
    assert unpaid.best_series is not None
    assert unpaid.best_series.moves[: len(prefix)] == prefix
    assert unpaid.principal_variation == (unpaid.best_series,)
    assert unpaid.alternatives == ()
    assert unpaid.proof is None
    assert unpaid.forced is None
    assert not unpaid.root_scores_complete
    assert unpaid.stats.work_positions == 195
    assert unpaid.stats.series_generation_cache_hits == 0

    paid = analyze(
        state,
        SearchLimits(
            depth_series=3,
            max_series_per_node=64,
            # Four positions stay reserved until root generation succeeds,
            # enough to produce one legal series if the ordinary frontier
            # exhausts its share of the deterministic budget.
            max_generation_positions=200,
        ),
        required_prefix=prefix,
    )
    assert not paid.work_limit_reached
    assert paid.completed_depth == 3
    assert paid.required_prefix == prefix
    assert paid.best_series is not None
    assert paid.best_series.moves == prefix + ("f6e4", "b6f2")
    assert paid.proof == "black"
    assert paid.stats.series_generation_positions == 35
    assert paid.stats.frontier_score_positions == 32
    assert paid.stats.static_evaluation_positions == 1
    assert paid.stats.evaluation_reach_positions == 128
    assert paid.stats.generation_positions == 196
    assert paid.stats.series_generation_cache_hits == 2


def test_timeout_keeps_best_fully_scored_root_candidate(monkeypatch) -> None:
    original = search_module.SeriesSearcher._minimax
    calls = 0

    def interrupt_second_candidate(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise search_module._Timeout
        return original(self, *args, **kwargs)

    monkeypatch.setattr(
        search_module.SeriesSearcher,
        "_minimax",
        interrupt_second_candidate,
    )
    result = analyze(ProgressiveState.initial(), SearchLimits(depth_series=1))

    assert result.timed_out
    assert result.completed_depth == 0
    assert result.best_series is not None
    assert len(result.alternatives) == 1
    assert not result.exact_width
    assert result.confidence == "partial root candidates only; incomplete/selective"


@pytest.mark.parametrize(
    ("interruption", "timed_out", "work_limited"),
    (
        (search_module._Timeout, True, False),
        (search_module._WorkLimit, False, True),
    ),
)
def test_first_root_interruption_keeps_unscored_legal_fallback(
    monkeypatch,
    interruption,
    timed_out: bool,
    work_limited: bool,
) -> None:
    state = ProgressiveState.initial()

    def interrupt_first_candidate(self, *args, **kwargs):
        raise interruption

    monkeypatch.setattr(
        search_module.SeriesSearcher,
        "_minimax",
        interrupt_first_candidate,
    )
    result = analyze(state, SearchLimits(depth_series=1))

    assert result.best_series is not None
    replayed = play_series(state, result.best_series.moves)
    assert replayed.final_state.transposition_key == (
        result.best_series.final_state.transposition_key
    )
    assert result.score == result.root_evaluation.total
    assert result.principal_variation == (result.best_series,)
    assert result.alternatives == ()
    assert result.completed_depth == 0
    assert result.timed_out is timed_out
    assert result.work_limit_reached is work_limited
    assert result.proof is None
    assert result.forced is None
    assert not result.exact_width
    assert not result.root_scores_complete


def test_deeper_pending_adjudication_keeps_last_completed_iteration(
    monkeypatch,
) -> None:
    state = ProgressiveState.initial()
    selected = play_series(state, ("e2e4",))
    scored = search_module.ScoredSeries(
        selected,
        321,
        (),
        (1, 1),
    )

    def complete_then_pending(self, state, depth, required_prefix):
        if depth == 1:
            self._root_scores_complete = True
            return 321, (selected,), (scored,), "white"
        raise search_module._AdjudicationPending

    monkeypatch.setattr(
        search_module.SeriesSearcher,
        "_search_root",
        complete_then_pending,
    )
    result = analyze(state, SearchLimits(depth_series=2))

    assert result.score == 321
    assert result.best_series == selected
    assert result.principal_variation == (selected,)
    assert result.alternatives == (scored,)
    assert result.proof == "white"
    assert result.completed_depth == 1
    assert result.requested_depth == 2
    assert result.adjudication_status == "manual-proof-required"
    assert not result.exact_width
    assert not result.timed_out
    assert not result.work_limit_reached
    assert result.root_scores_complete
    assert result.forced is None


def test_generation_work_limit_is_distinct_from_wall_clock_timeout() -> None:
    state = ProgressiveState.from_fen(
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3",
        4,
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=1,
            max_series_per_node=64,
            max_generation_positions=1,
        ),
    )

    assert result.work_limit_reached
    assert not result.timed_out
    assert result.best_series is None
    assert result.stats.generation_positions == 1
    assert result.stats.generation_work_limit_hits == 1
    assert "work limit" in result.confidence


def test_time_limit_cancels_inside_series_generation() -> None:
    state = ProgressiveState.from_fen(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 3
    )
    result = analyze(
        state, SearchLimits(depth_series=1, time_limit_seconds=0.01)
    )
    assert result.timed_out
    assert result.completed_depth == 0
    assert not result.root_scores_complete
    assert result.elapsed_seconds < 0.5


def test_time_limit_covers_quiet_draw_mating_series_probe() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/6R1/K7 w - - 0 1", 101, quiet_series=10
    )
    result = analyze(
        state, SearchLimits(depth_series=1, time_limit_seconds=0.000001)
    )
    assert result.timed_out
    assert result.completed_depth == 0
    assert result.elapsed_seconds < 0.5


def test_high_series_quiet_transition_is_charged_to_global_work_budget() -> None:
    # Black's only legal move is the quiet countercheck 20...Kb8+, producing a
    # series-21 child at quiet=10. Historically its mating-series exception
    # probe ignored max_generation_positions and could dominate a whole league
    # batch. Static evaluation, one reach node, and generation cost three
    # positions; the proof probe gets the remaining 29 and must conservatively
    # stop as manual-proof-required.
    state = ProgressiveState.from_fen(
        "r7/k6R/8/K7/8/8/8/8 b - - 0 1", 20, quiet_series=9
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=8,
            max_generation_positions=32,
        ),
    )

    assert result.adjudication_status == "manual-proof-required"
    assert result.work_limit_reached
    assert not result.timed_out
    assert result.best_series is not None
    assert result.best_series.moves == ("a7b8",)
    assert result.principal_variation == (result.best_series,)
    assert result.alternatives == ()
    assert result.score == result.root_evaluation.total
    assert result.proof is None
    assert result.forced is None
    assert not result.root_scores_complete
    assert result.stats.generation_positions == 32
    assert result.stats.series_generation_positions == 1
    assert result.stats.frontier_score_positions == 0
    assert result.stats.static_evaluation_positions == 1
    assert result.stats.evaluation_reach_positions == 1
    assert result.stats.quiet_adjudication_positions == 29
    assert result.stats.quiet_adjudication_limit_hits == 1
    assert result.stats.generation_work_limit_hits == 1
    assert result.elapsed_seconds < 0.5


def test_quiet_adjudication_has_search_wide_hard_ceiling_without_work_limit(
    monkeypatch,
) -> None:
    calls = 0

    def unbounded_probe(state, *, should_stop=None):
        nonlocal calls
        assert should_stop is not None
        while True:
            calls += 1
            if should_stop():
                raise GenerationCancelled

    monkeypatch.setattr(
        search_module, "quiet_adjudication_status", unbounded_probe
    )
    state = ProgressiveState.from_fen(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1",
        20,
        quiet_series=10,
    )
    searcher = SeriesSearcher(SearchLimits(depth_series=1))

    assert searcher._quiet_adjudication(state) == "manual-proof-required"
    assert calls == QUIET_ADJUDICATION_POSITION_LIMIT + 1
    assert (
        searcher.stats.quiet_adjudication_positions
        == QUIET_ADJUDICATION_POSITION_LIMIT
    )
    assert searcher.stats.generation_positions == QUIET_ADJUDICATION_POSITION_LIMIT
    assert searcher.stats.quiet_adjudication_limit_hits == 1
    assert not searcher._quiet_work_limit_reached

    # A transposition must reuse the conservative adjudication instead of
    # restarting another bounded proof search.
    assert searcher._quiet_adjudication(state.copy()) == "manual-proof-required"
    assert calls == QUIET_ADJUDICATION_POSITION_LIMIT + 1
    assert searcher.stats.quiet_adjudication_cache_hits == 1


def test_quiet_draw_with_bare_kings_is_proven_not_heuristically_scored() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K7 w - - 0 1", 1, quiet_series=10
    )
    result = analyze(state, SearchLimits(depth_series=1))
    assert result.score == 0
    assert result.classification == "Drawn"
    assert result.forced == "draw"
    assert result.adjudication_status == "proven-draw-no-mating-material"
    assert result.root_scores_complete


def test_unresolved_quiet_draw_exception_returns_pending_not_theory_score() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/6R1/K7 w - - 0 1", 1, quiet_series=10
    )
    result = analyze(state, SearchLimits(depth_series=1))
    assert result.score == 0
    assert result.classification == "Adjudication Pending"
    assert result.adjudication_status == "manual-proof-required"
    assert not result.root_scores_complete


def test_immediate_mate_exception_is_searched_after_ten_quiet_series() -> None:
    state = ProgressiveState.from_fen(
        "8/8/8/8/8/8/K7/2kq4 b - - 0 1", 2, quiet_series=10
    )
    result = analyze(state, SearchLimits(depth_series=1))
    assert result.adjudication_status == "mate-exception-immediate"
    assert result.score <= -MATE_SCORE + 1


def test_mate_later_in_current_series_satisfies_quiet_draw_exception() -> None:
    state = ProgressiveState.from_fen(
        "8/8/8/8/8/8/8/K2kq3 b - - 0 1", 2, quiet_series=10
    )
    result = analyze(state, SearchLimits(depth_series=1))
    assert result.adjudication_status == "mate-exception-immediate"
    assert result.best_series is not None
    assert result.best_series.moves == ("d1c1", "e1a5")
    assert result.best_series.outcome == Outcome.CHECKMATE
    assert result.forced == "black"


@pytest.mark.parametrize(
    "fen",
    [
        "7k/8/8/8/8/8/6B1/K7 w - - 0 1",
        "7k/8/8/8/8/8/6N1/K7 w - - 0 1",
    ],
)
def test_insufficient_mating_material_proves_quiet_draw(fen: str) -> None:
    state = ProgressiveState.from_fen(fen, 1, quiet_series=10)
    result = analyze(state, SearchLimits(depth_series=1))
    assert result.score == 0
    assert result.forced == "draw"
    assert result.classification == "Drawn"


def test_proven_quiet_draw_is_adjudicated_at_child_node() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K7 w - - 0 1", 1, quiet_series=9
    )
    result = analyze(state, SearchLimits(depth_series=2))
    assert result.score == 0
    assert result.forced == "draw"
    assert result.best_series is not None
    assert result.best_series.outcome == Outcome.TEN_SERIES_DRAW


def test_draw_pv_without_root_proof_is_not_called_forced() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K7 w - - 0 1", 1, quiet_series=9
    )
    proven = analyze(state, SearchLimits(depth_series=1))
    assert proven.best_series is not None
    assert proven.best_series.outcome == Outcome.TEN_SERIES_DRAW
    assert replace(proven, proof=None).forced is None


def test_stalemate_option_proves_at_least_draw_not_exact_game_value() -> None:
    state = ProgressiveState.from_fen(
        "3N4/8/8/8/1K6/8/1k6/2Q5 b - - 0 1", 2
    )
    result = analyze(state, SearchLimits(depth_series=1))
    assert result.best_series is not None
    assert result.best_series.outcome == Outcome.STALEMATE
    assert result.proof is None
    assert result.forced is None
    assert result.classification == "Unclear"


def test_unresolved_quiet_draw_at_child_aborts_ordinary_minimax_score() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/6R1/K7 w - - 0 1", 1, quiet_series=9
    )
    result = analyze(state, SearchLimits(depth_series=1))
    assert result.score == result.root_evaluation.total
    assert result.classification == "Adjudication Pending"
    assert result.adjudication_status == "manual-proof-required"
    assert result.best_series is not None
    assert result.principal_variation == (result.best_series,)
    assert result.alternatives == ()
    assert result.proof is None
    assert result.forced is None
    assert not result.root_scores_complete
    replayed = play_series(state, result.best_series.moves)
    assert replayed.final_state.transposition_key == (
        result.best_series.final_state.transposition_key
    )


def test_quiet_adjudication_in_deeper_pass_keeps_fast_kqk_fallback() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/6Q1/K7 w - - 0 1",
        1,
        quiet_series=8,
    )
    shallow = analyze(
        state,
        SearchLimits(
            depth_series=1,
            max_series_per_node=8,
            max_generation_positions=20_000,
            collect_all_root_scores=False,
        ),
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=8,
            max_generation_positions=20_000,
            collect_all_root_scores=False,
        ),
    )

    assert shallow.best_series is not None
    assert shallow.best_series.moves == ("g2h2",)
    assert result.score == shallow.score
    assert result.best_series == shallow.best_series
    assert result.principal_variation == shallow.principal_variation
    assert result.alternatives == shallow.alternatives
    assert result.proof == shallow.proof
    assert result.completed_depth == 1
    assert result.adjudication_status == "manual-proof-required"
    assert not result.exact_width
    assert result.forced is None


def test_live_series_22_kqk_pending_pass_keeps_production_fallback() -> None:
    state = ProgressiveState.from_fen(
        "8/8/8/8/6Q1/2K5/6k1/8 b - - 144 109",
        22,
        quiet_series=8,
    )
    production_limits = {
        "max_series_per_node": 32,
        "max_generation_positions": 250_000,
        "collect_all_root_scores": False,
    }
    shallow = analyze(
        state,
        SearchLimits(depth_series=1, **production_limits),
    )
    result = analyze(
        state,
        SearchLimits(depth_series=2, **production_limits),
    )

    assert shallow.best_series is not None
    assert shallow.best_series.machine_notation == (
        "g2f1/f1e1/e1f1/f1e1/e1f1/f1e1/e1f1/f1e1/e1f1/f1e1/e1f1/"
        "f1e1/e1f1/f1e1/e1f1/f1e1/e1f1/f1e1/e1f1/f1e1/e1f2/f2e3"
    )
    assert result.score == shallow.score == 1_411
    assert result.best_series == shallow.best_series
    assert result.principal_variation == shallow.principal_variation
    assert result.alternatives == shallow.alternatives
    assert result.proof == shallow.proof
    assert result.completed_depth == 1
    assert result.adjudication_status == "manual-proof-required"
    assert not result.exact_width
    assert not result.timed_out
    assert not result.work_limit_reached
    assert result.root_scores_complete == shallow.root_scores_complete
    assert result.forced is None


def test_live_series_24_pending_first_pass_keeps_legal_root_fallback() -> None:
    state = ProgressiveState.from_fen(
        "8/8/5n1b/8/3n4/8/1k4K1/8 b - - 129 89",
        24,
        quiet_series=9,
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
        ),
    )

    assert result.best_series is not None
    assert result.best_series.machine_notation == (
        "b2b3/d4f5/f6d5/b3c3/h6c1/c1a3/a3c5/c3b2/b2b3/b3b2/b2b3/"
        "b3b2/b2b3/b3b2/b2b3/b3b2/b2b3/b3b2/b2b3/b3b2/b2b3/b3b2/"
        "b2b3/d5f4"
    )
    assert result.best_series.used_moves == 24
    replayed = play_series(state, result.best_series.moves)
    assert replayed.final_state.transposition_key == (
        result.best_series.final_state.transposition_key
    )
    assert result.score == result.root_evaluation.total
    assert result.score != 0
    assert result.principal_variation == (result.best_series,)
    assert result.alternatives == ()
    assert result.completed_depth == 0
    assert result.requested_depth == 2
    assert result.adjudication_status == "manual-proof-required"
    assert result.proof is None
    assert result.forced is None
    assert not result.exact_width
    assert not result.timed_out
    assert not result.work_limit_reached
    assert not result.root_scores_complete
    assert result.stats.series_generation_positions == 736
    assert result.stats.quiet_adjudication_positions == 4_096
    assert result.stats.work_positions <= 250_000


def test_series_101_pending_fallback_is_legal_deterministic_and_bounded() -> None:
    state = ProgressiveState.from_fen(
        "8/8/5n1b/8/3n4/8/1k4K1/8 w - - 129 89",
        101,
        quiet_series=9,
    )
    limits = SearchLimits(
        depth_series=1,
        max_series_per_node=1,
        max_generation_positions=50_000,
        collect_all_root_scores=False,
    )

    first = analyze(state, limits)
    second = analyze(state, limits)

    assert first.best_series is not None
    assert second.best_series is not None
    assert first.best_series.moves == second.best_series.moves
    assert first.best_series.used_moves == 101
    replayed = play_series(state, first.best_series.moves)
    assert replayed.final_state.transposition_key == (
        first.best_series.final_state.transposition_key
    )
    assert first.score == first.root_evaluation.total
    assert first.principal_variation == (first.best_series,)
    assert first.alternatives == ()
    assert first.completed_depth == 0
    assert first.adjudication_status == "manual-proof-required"
    assert first.proof is None
    assert first.forced is None
    assert not first.exact_width
    assert not first.timed_out
    assert not first.work_limit_reached
    assert not first.root_scores_complete
    assert first.stats.series_generation_positions == 101
    assert first.stats.quiet_adjudication_positions == 4_096
    assert first.stats.work_positions == second.stats.work_positions
    assert first.stats.work_positions <= 50_000


def test_series_101_root_generation_limit_keeps_budgeted_seed_series() -> None:
    state = ProgressiveState.from_fen(
        "8/8/5n1b/8/3n4/8/1k4K1/8 w - - 129 89",
        101,
        quiet_series=0,
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=1,
            max_series_per_node=1,
            max_generation_positions=500,
            collect_all_root_scores=False,
        ),
    )

    assert result.best_series is not None
    assert result.best_series.used_moves == 101
    replayed = play_series(state, result.best_series.moves)
    assert replayed.final_state.transposition_key == (
        result.best_series.final_state.transposition_key
    )
    assert result.score == result.root_evaluation.total
    assert result.principal_variation == (result.best_series,)
    assert result.alternatives == ()
    assert result.completed_depth == 0
    assert result.work_limit_reached
    assert not result.timed_out
    assert result.adjudication_status is None
    assert result.proof is None
    assert result.forced is None
    assert not result.exact_width
    assert not result.root_scores_complete
    assert result.stats.work_positions <= 500


def test_series_101_seed_never_overruns_insufficient_total_work() -> None:
    state = ProgressiveState.from_fen(
        "8/8/5n1b/8/3n4/8/1k4K1/8 w - - 129 89",
        101,
        quiet_series=0,
    )
    # Root evaluation consumes 167 deterministic positions here. The 100
    # remaining positions cannot pay for a non-checking 101-move seed, so the
    # sound result is still no move rather than one unit of unmetered work.
    result = analyze(
        state,
        SearchLimits(
            depth_series=1,
            max_series_per_node=1,
            max_generation_positions=267,
            collect_all_root_scores=False,
        ),
    )

    assert result.best_series is None
    assert result.completed_depth == 0
    assert result.work_limit_reached
    assert not result.timed_out
    assert result.proof is None
    assert result.forced is None
    assert result.stats.work_positions == 267
    assert result.stats.series_generation_positions == 100


@pytest.mark.parametrize(("quiet_series", "depth"), [(8, 2), (7, 3)])
def test_proven_draw_kind_propagates_through_multiple_series(
    quiet_series: int, depth: int
) -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K7 w - - 0 1",
        1,
        quiet_series=quiet_series,
    )
    result = analyze(state, SearchLimits(depth_series=depth))
    assert result.score == 0
    assert result.proof == "draw"
    assert result.forced == "draw"
    assert result.classification == "Drawn"
    assert result.principal_variation[-1].outcome == Outcome.TEN_SERIES_DRAW
