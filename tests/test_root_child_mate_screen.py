from __future__ import annotations

import os
import time

import pytest

import scottish_progressive.search as search_module
from scottish_progressive.model import Outcome, ProgressiveState, SeriesResult
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.search import (
    MATE_SCORE,
    ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT,
    SearchLimits,
    SeriesSearcher,
    analyze,
)


S7_STATE = ProgressiveState.from_fen(
    "rnk3nr/pp3ppp/8/8/8/1Pp1P3/P1PP1PPP/R1b1K1NR w K - 0 13",
    7,
)
BLUNDERING_S7 = (
    "e3e4",
    "g1e2",
    "e4e5",
    "e5e6",
    "e6f7",
    "a1c1",
    "f7g8q",
)
HUMAN_S8_MATE = (
    "h8g8",
    "a7a5",
    "a5a4",
    "a4b3",
    "b3b2",
    "a8a2",
    "g8e8",
    "b2c1q",
)
PROFILE = baseline_profile()
S16_STATE = ProgressiveState.from_fen(
    "5Q1Q/8/3k4/8/8/8/4K3/8 b - - 0 57",
    16,
)
BLUNDERING_S16 = (
    "d6c6",
    "c6b5",
    "b5a4",
    "a4a5",
    "a5a4",
    "a4a5",
    "a5a4",
    "a4a5",
    "a5a4",
    "a4a5",
    "a5a4",
    "a4a5",
    "a5a4",
    "a4a5",
    "a5a4",
    "a4a5",
)
HUMAN_S17_MATE = ("h8b2", "f8a8")
EARLY_S4_HISTORY = (
    ("e2e4",),
    ("f7f6", "e8f7"),
    ("d2d4", "e1d2", "g1f3"),
)
BLUNDERING_S4 = ("f6f5", "f5e4", "c7c5", "d8b6")
HUMAN_S5_MATE = ("b1c3", "c3e4", "f1b5", "b5d7", "f3e5")
SAFE_S4 = ("d7d5", "d5e4", "d8d6", "g8h6")


def _play_limits() -> SearchLimits:
    return SearchLimits(
        depth_series=4,
        max_series_per_node=32,
        max_generation_positions=10_000_000,
        time_limit_seconds=30.0,
        collect_all_root_scores=False,
        native_threads=16,
    )


def _live_searcher() -> SeriesSearcher:
    searcher = SeriesSearcher(_play_limits(), PROFILE)
    searcher._deadline = time.perf_counter() + 30.0
    return searcher


def _early_s4_state() -> ProgressiveState:
    state = ProgressiveState.initial()
    for series in EARLY_S4_HISTORY:
        state = play_series(state, series).final_state
    return state


def _ordinary_cap832_reply_mate(
    state: ProgressiveState,
    *,
    native_threads: int = 1,
) -> tuple[SeriesResult | None, int]:
    """Returns a replay-proven mate from the historical ordinary wide beam."""

    verifier = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=832,
            max_generation_positions=250_000,
            time_limit_seconds=30.0,
            collect_all_root_scores=False,
            native_threads=native_threads,
        ),
        PROFILE,
    )
    verifier._deadline = time.perf_counter() + 30.0
    generated, _width_complete = verifier._generate(
        state,
        ply_from_root=2,
        tactical_protection=False,
    )
    candidates = (
        generated.references()
        if hasattr(generated, "references")
        else generated
    )
    for candidate in candidates:
        if (
            candidate.outcome != Outcome.CHECKMATE
            or not candidate.ended_by_check
        ):
            continue
        replayed = play_series(state, candidate.moves)
        if replayed.outcome == Outcome.CHECKMATE and replayed.ended_by_check:
            return replayed, verifier.stats.work_positions
    return None, verifier.stats.work_positions


def test_wide_native_child_screen_replays_the_human_mate() -> None:
    blunder = play_series(S7_STATE, BLUNDERING_S7)
    human_mate = play_series(blunder.final_state, HUMAN_S8_MATE)
    assert human_mate.outcome == Outcome.CHECKMATE
    assert human_mate.ended_by_check

    searcher = _live_searcher()
    mate = searcher._root_child_immediate_mate(blunder.final_state)

    assert mate is not None
    assert mate.outcome == Outcome.CHECKMATE
    assert mate.ended_by_check
    assert play_series(blunder.final_state, mate.moves).outcome == Outcome.CHECKMATE
    assert searcher.stats.work_positions <= ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT
    charged_work = searcher.stats.work_positions
    assert searcher._root_child_immediate_mate(blunder.final_state) == mate
    assert searcher.stats.work_positions == charged_work


def test_early_s4_child_screen_replays_the_exact_s5_mate() -> None:
    """A concrete early mate must not be hidden by the Series-7 risk gate."""

    root = _early_s4_state()
    blunder = play_series(root, BLUNDERING_S4)
    human_mate = play_series(blunder.final_state, HUMAN_S5_MATE)
    assert blunder.final_state.series_number == 5
    assert human_mate.outcome == Outcome.CHECKMATE
    assert human_mate.ended_by_check
    assert not search_module._tactical_frontier_protection_eligible(
        blunder.final_state
    )

    searcher = _live_searcher()
    screened = searcher._root_child_immediate_mate(blunder.final_state)

    assert screened is not None
    assert (
        play_series(blunder.final_state, screened.moves).outcome
        == Outcome.CHECKMATE
    )
    assert searcher.stats.work_positions < 10_000


def test_cap32_generation_retains_the_human_promotion_mate_for_minimax() -> None:
    """The ordinary beam must not need a separate width-832 rescue here.

    The mating route is discarded after ``a5a4`` by the old static cap even
    though ``a4b3`` is an immediately available capture leading to promotion.
    Tactical opportunity retention must carry at least one such route into the
    complete-series candidates that minimax actually receives.
    """

    blunder = play_series(S7_STATE, BLUNDERING_S7)
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
        ),
        PROFILE,
    )

    retained = searcher._ordered_generated(
        blunder.final_state,
        ply_from_root=2,
    )
    mates = [
        candidate
        for candidate in retained
        if candidate.outcome == Outcome.CHECKMATE
        and candidate.ended_by_check
    ]

    assert mates
    assert any(
        any(
            len(move) == 5 and move[-1] in "qrbn"
            for move in candidate.moves
        )
        for candidate in mates
    )
    assert any(
        play_series(blunder.final_state, candidate.moves).outcome
        == Outcome.CHECKMATE
        for candidate in mates
    )
    assert len(retained) <= 32
    assert searcher.stats.work_positions <= 250_000
    assert searcher.stats.tactical_frontier_states_retained > 0
    assert searcher.stats.tactical_frontier_reserve_drops > 0


def test_ordinary_opening_screen_is_staged_and_keeps_the_canonical_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = search_module._native_complete_series_batch
    cheap_calls = 0
    wide_calls = 0

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal cheap_calls
        nonlocal wide_calls
        frontier = kwargs.get("max_frontier_states")
        score = kwargs.get("frontier_score")
        if frontier == 32 and getattr(score, "tactical_protection", False):
            cheap_calls += 1
        if frontier == 832:
            wide_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(search_module, "_native_complete_series_batch", counted)
    result = analyze(
        ProgressiveState.initial(),
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
        ),
        PROFILE,
    )

    assert result.best_series is not None
    assert result.best_series.moves == ("g2g3",)
    assert result.score == -150
    # The screen is called only for successive exact root contenders. At S2,
    # each protected width-32 probe is tiny; the entire opening stays under the
    # prior 30k deterministic-work envelope without widening to 832.
    assert 0 < cheap_calls <= 8
    assert wide_calls == 0
    assert result.stats.work_positions < 30_000
    assert result.stats.tactical_frontier_states_retained == 0
    assert result.stats.tactical_frontier_reserve_drops == 0
    assert result.stats.tactical_final_series_retained == 0
    assert result.stats.tactical_final_reserve_drops == 0


def test_hosted_shallow_s4_selection_avoids_cap832_reply_mate() -> None:
    """The single-thread hosted fast path must reject the live blunder."""

    root = _early_s4_state()
    result = analyze(
        root,
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=250_000,
            time_limit_seconds=10.0,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        PROFILE,
    )

    assert result.best_series is not None
    assert result.best_series.moves == SAFE_S4
    assert result.completed_depth == 2
    selected_child = play_series(root, result.best_series.moves).final_state
    reply_mate, verifier_work = _ordinary_cap832_reply_mate(selected_child)
    assert reply_mate is None
    assert 0 < verifier_work < 50_000


@pytest.mark.parametrize("requested_depth", (4, 5))
def test_early_s4_play_avoids_every_cap832_replay_mate(
    requested_depth: int,
) -> None:
    """Opt-in live-strength gate for the 10m-work, 16-thread play profile."""

    if os.environ.get("SPC_RUN_EARLY_S4_GATE") != "1":
        pytest.skip("set SPC_RUN_EARLY_S4_GATE=1 for the early S4 strength gate")

    root = _early_s4_state()
    limits = SearchLimits(
        depth_series=requested_depth,
        max_series_per_node=32,
        max_generation_positions=10_000_000,
        time_limit_seconds=30.0,
        collect_all_root_scores=False,
        native_threads=16,
    )
    result = analyze(root, limits, PROFILE)

    assert result.best_series is not None
    assert result.best_series.moves == SAFE_S4
    assert result.completed_depth == 4
    selected_child = play_series(root, result.best_series.moves).final_state
    reply_mate, verifier_work = _ordinary_cap832_reply_mate(
        selected_child,
        native_threads=16,
    )
    assert reply_mate is None
    assert 0 < verifier_work < 50_000


def test_depth_four_play_avoids_every_cap832_replay_mate() -> None:
    result = analyze(S7_STATE, _play_limits(), PROFILE)

    assert result.best_series is not None
    assert result.best_series.moves != BLUNDERING_S7
    selected_child = play_series(S7_STATE, result.best_series.moves).final_state
    verifier = _live_searcher()
    assert verifier._root_child_immediate_mate(selected_child) is None
    assert verifier.stats.work_positions > 0
    assert verifier.stats.work_positions <= ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT


def test_nonpromotion_queen_mate_is_screened_and_reported() -> None:
    blunder = play_series(S16_STATE, BLUNDERING_S16)
    human_mate = play_series(blunder.final_state, HUMAN_S17_MATE)
    assert human_mate.outcome == Outcome.CHECKMATE
    assert human_mate.ended_by_check

    direct = _live_searcher()
    screened = direct._root_child_immediate_mate(blunder.final_state)
    assert screened is not None
    assert play_series(blunder.final_state, screened.moves).outcome == Outcome.CHECKMATE

    result = analyze(S16_STATE, _play_limits(), PROFILE)
    assert result.best_series is not None
    assert result.score == MATE_SCORE - 2
    assert len(result.principal_variation) >= 2
    selected_child = play_series(S16_STATE, result.best_series.moves).final_state
    selected_mate = result.principal_variation[1]
    replayed = play_series(selected_child, selected_mate.moves)
    assert replayed.outcome == Outcome.CHECKMATE
    assert replayed.ended_by_check
