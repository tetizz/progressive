from __future__ import annotations

import chess
import pytest

import scottish_progressive.search as search_module
from scottish_progressive.model import Outcome, ProgressiveState, SeriesResult
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.search import (
    MATE_SCORE,
    MAX_EVALUATION_OVERLAY_SCORE,
    ScoredSeries,
    SearchLimits,
    SeriesSearcher,
    analyze,
)


def _root_and_candidates(
    mover: chess.Color,
) -> tuple[ProgressiveState, tuple[SeriesResult, ...]]:
    if mover == chess.WHITE:
        root = ProgressiveState.initial()
        moves = (("e2e4",), ("d2d4",), ("g1f3",))
    else:
        root = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
        moves = (
            ("e7e5", "g8f6"),
            ("d7d5", "c8g4"),
            ("c7c5", "b8c6"),
        )
    candidates = tuple(play_series(root, item) for item in moves)
    assert all(item.outcome is None for item in candidates)
    return root, candidates


def _scored(
    candidate: SeriesResult,
    score: int,
    proof_bounds: tuple[int, int],
) -> ScoredSeries:
    return ScoredSeries(candidate, score, (), proof_bounds)


@pytest.mark.parametrize("mover", (chess.WHITE, chess.BLACK))
def test_proof_safe_order_is_mirrored_stable_and_exact(mover: chess.Color) -> None:
    _root, candidates = _root_and_candidates(mover)
    safe_score = 40 if mover == chess.WHITE else -40
    adverse_score = 500 if mover == chess.WHITE else -500
    partial_bounds = (-1, 0) if mover == chess.WHITE else (0, 1)
    adverse_bounds = (-1, -1) if mover == chess.WHITE else (1, 1)
    safe = [
        _scored(candidates[0], safe_score, partial_bounds),
        _scored(candidates[1], safe_score, (-1, 1)),
    ]
    adverse = _scored(candidates[2], adverse_score, adverse_bounds)

    ordered = search_module._proof_safe_root_order(
        mover,
        (safe[1], adverse, safe[0]),
    )

    assert [item.series.machine_notation for item in ordered[:2]] == sorted(
        item.series.machine_notation for item in safe
    )
    assert ordered[2] is adverse


@pytest.mark.parametrize("mover", (chess.WHITE, chess.BLACK))
def test_all_proven_losing_root_keeps_best_resistance(mover: chess.Color) -> None:
    _root, candidates = _root_and_candidates(mover)
    adverse_bounds = (-1, -1) if mover == chess.WHITE else (1, 1)
    scores = (100, 200) if mover == chess.WHITE else (-100, -200)
    options = (
        _scored(candidates[0], scores[0], adverse_bounds),
        _scored(candidates[1], scores[1], adverse_bounds),
    )

    ordered = search_module._proof_safe_root_order(mover, options)

    assert ordered[0] is options[1]


@pytest.mark.parametrize("mover", (chess.WHITE, chess.BLACK))
def test_full_root_and_best_only_choose_same_proof_safe_line(
    monkeypatch: pytest.MonkeyPatch,
    mover: chess.Color,
) -> None:
    root, candidates = _root_and_candidates(mover)
    adverse_bounds = (-1, -1) if mover == chess.WHITE else (1, 1)
    adverse_score = 500 if mover == chess.WHITE else -500
    safe_score = 100 if mover == chess.WHITE else -100
    trailing_score = 50 if mover == chess.WHITE else 50
    child_results = {
        candidates[0].final_state.transposition_key: (
            adverse_score,
            adverse_bounds,
        ),
        candidates[1].final_state.transposition_key: (safe_score, (-1, 1)),
        candidates[2].final_state.transposition_key: (
            trailing_score,
            adverse_bounds,
        ),
    }

    def run_root(collect_all_root_scores: bool):
        searcher = SeriesSearcher(
            SearchLimits(
                depth_series=1,
                collect_all_root_scores=collect_all_root_scores,
            )
        )
        monkeypatch.setattr(
            searcher,
            "_ordered_generated",
            lambda *_args, **_kwargs: search_module._GeneratedSeriesList(
                list(candidates),
                width_complete=True,
            ),
        )
        monkeypatch.setattr(searcher, "_start_native_subtree", lambda _state: None)

        def fake_minimax(
            state: ProgressiveState,
            _depth: int,
            _alpha: int,
            _beta: int,
            _ply_from_root: int,
        ) -> tuple[int, tuple[SeriesResult, ...], tuple[int, int]]:
            score, bounds = child_results[state.transposition_key]
            return score, (), bounds

        monkeypatch.setattr(searcher, "_minimax", fake_minimax)
        return searcher._search_root_pass(root, 1, (), {})

    full = run_root(True)
    best_only = run_root(False)

    assert full[0] == best_only[0] == safe_score
    assert full[1][0].machine_notation == candidates[1].machine_notation
    assert best_only[1][0].machine_notation == candidates[1].machine_notation
    assert full[2][0].series.machine_notation == candidates[1].machine_notation
    assert best_only[2][0].series.machine_notation == candidates[1].machine_notation


@pytest.mark.parametrize("mover", (chess.WHITE, chess.BLACK))
def test_interrupted_root_uses_best_scored_non_adverse_fallback(
    monkeypatch: pytest.MonkeyPatch,
    mover: chess.Color,
) -> None:
    root, candidates = _root_and_candidates(mover)
    adverse_bounds = (-1, -1) if mover == chess.WHITE else (1, 1)
    adverse_score = 500 if mover == chess.WHITE else -500
    safe_score = 100 if mover == chess.WHITE else -100
    child_results = {
        candidates[0].final_state.transposition_key: (
            adverse_score,
            adverse_bounds,
        ),
        candidates[1].final_state.transposition_key: (safe_score, (-1, 1)),
    }
    searcher = SeriesSearcher(
        SearchLimits(depth_series=1, collect_all_root_scores=False)
    )
    monkeypatch.setattr(
        searcher,
        "_ordered_generated",
        lambda *_args, **_kwargs: search_module._GeneratedSeriesList(
            list(candidates),
            width_complete=True,
        ),
    )
    monkeypatch.setattr(searcher, "_start_native_subtree", lambda _state: None)
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: False,
    )

    def fake_minimax(
        state: ProgressiveState,
        _depth: int,
        _alpha: int,
        _beta: int,
        _ply_from_root: int,
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[int, int]]:
        if state.transposition_key == candidates[2].final_state.transposition_key:
            raise search_module._WorkLimit
        score, bounds = child_results[state.transposition_key]
        return score, (), bounds

    monkeypatch.setattr(searcher, "_minimax", fake_minimax)

    result = searcher.run(root)

    assert result.completed_depth == 0
    assert result.work_limit_reached
    assert result.proof is None
    assert result.score == safe_score
    assert result.best_series is not None
    assert result.best_series.machine_notation == candidates[1].machine_notation
    assert result.alternatives[0].series.machine_notation == (
        candidates[1].machine_notation
    )


class _ExtremeOverlay:
    variant_id = "unit-extreme-overlay"
    name = "Unit extreme overlay"

    def __init__(self, base_profile_id: str, value: int) -> None:
        self.base_profile_id = base_profile_id
        self.value = value

    def score(self, state: ProgressiveState, hand_score: int) -> int:
        del state, hand_score
        return self.value


@pytest.mark.parametrize("sign", (-1, 1))
def test_extreme_overlay_stays_outside_reserved_mate_proof_band(sign: int) -> None:
    profile = baseline_profile()
    overlay = _ExtremeOverlay(profile.profile_id, sign * MATE_SCORE * 10)
    searcher = SeriesSearcher(SearchLimits(), profile, overlay)

    score = searcher._evaluate(ProgressiveState.initial()).total

    assert score == sign * MAX_EVALUATION_OVERLAY_SCORE
    assert abs(score) < MATE_SCORE - 10_000


@pytest.mark.parametrize(
    ("mover", "fen", "series_number"),
    (
        (chess.WHITE, "7k/8/5KQ1/8/8/8/8/8 w - - 0 1", 1),
        (chess.BLACK, "8/8/8/8/8/5kq1/8/7K b - - 0 1", 2),
    ),
)
def test_extreme_overlay_cannot_outrank_mirrored_immediate_mate(
    mover: chess.Color,
    fen: str,
    series_number: int,
) -> None:
    profile = baseline_profile()
    overlay_score = MATE_SCORE * 10 if mover == chess.WHITE else -MATE_SCORE * 10
    overlay = _ExtremeOverlay(profile.profile_id, overlay_score)
    state = ProgressiveState.from_fen(fen, series_number)

    result = analyze(
        state,
        SearchLimits(depth_series=1),
        profile,
        evaluation_overlay=overlay,
    )

    assert result.score == (MATE_SCORE - 1 if mover == chess.WHITE else -MATE_SCORE + 1)
    assert result.best_series is not None
    assert result.best_series.outcome == Outcome.CHECKMATE
    assert result.proof == ("white" if mover == chess.WHITE else "black")
