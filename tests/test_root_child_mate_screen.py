from __future__ import annotations

import time

import pytest

import scottish_progressive.search as search_module
from scottish_progressive.model import Outcome, ProgressiveState
from scottish_progressive.profiles import load_profile
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
PROFILE = load_profile("profiles/champion.json")
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


def test_ordinary_early_search_never_starts_the_wide_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = search_module._native_complete_series_batch
    wide_calls = 0

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal wide_calls
        if kwargs.get("max_frontier_states") == 832:
            wide_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(search_module, "_native_complete_series_batch", counted)
    state = ProgressiveState.from_fen(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        3,
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
        ),
        PROFILE,
    )

    assert result.best_series is not None
    assert wide_calls == 0
    assert result.stats.tactical_frontier_states_retained == 0
    assert result.stats.tactical_frontier_reserve_drops == 0
    assert result.stats.tactical_final_series_retained == 0
    assert result.stats.tactical_final_reserve_drops == 0


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
