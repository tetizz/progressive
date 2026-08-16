from __future__ import annotations

from dataclasses import replace

import pytest

from scottish_progressive.evaluation import evaluate, probe_series_reach
from scottish_progressive.model import Outcome, ProgressiveState
from scottish_progressive.search import MATE_SCORE, SearchLimits, analyze


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


def test_selective_mate_score_is_not_labeled_forced() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1", 1
    )
    result = analyze(
        state, SearchLimits(depth_series=1, max_series_per_node=1)
    )
    assert result.score == MATE_SCORE - 1
    assert not result.exact_width
    assert result.forced is None
    assert result.confidence == "selective depth-limited heuristic"


def test_search_is_reproducible() -> None:
    state = ProgressiveState.initial()
    first = analyze(state, SearchLimits(depth_series=1))
    second = analyze(state, SearchLimits(depth_series=1))
    assert first.score == second.score
    assert first.best_series is not None and second.best_series is not None
    assert first.best_series.moves == second.best_series.moves


def test_time_limit_cancels_inside_series_generation() -> None:
    state = ProgressiveState.from_fen(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 3
    )
    result = analyze(
        state, SearchLimits(depth_series=1, time_limit_seconds=0.01)
    )
    assert result.timed_out
    assert result.completed_depth == 0
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


def test_quiet_draw_with_bare_kings_is_proven_not_heuristically_scored() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K7 w - - 0 1", 1, quiet_series=10
    )
    result = analyze(state, SearchLimits(depth_series=1))
    assert result.score == 0
    assert result.classification == "Drawn"
    assert result.forced == "draw"
    assert result.adjudication_status == "proven-draw-no-mating-material"


def test_unresolved_quiet_draw_exception_returns_pending_not_theory_score() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/6R1/K7 w - - 0 1", 1, quiet_series=10
    )
    result = analyze(state, SearchLimits(depth_series=1))
    assert result.score == 0
    assert result.classification == "Adjudication Pending"
    assert result.adjudication_status == "manual-proof-required"


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
    assert result.score == 0
    assert result.classification == "Adjudication Pending"
    assert result.adjudication_status == "manual-proof-required"
    assert result.best_series is None


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
