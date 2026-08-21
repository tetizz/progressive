from __future__ import annotations

import os
import time

import pytest

import scottish_progressive.evaluation as evaluation_module
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
PRIOR_SAFE_S4 = ("d7d5", "d5e4", "d8d6", "g8h6")
ROOT_TACTICAL_S4 = ("e7e5", "f6f5", "f5e4", "f8b4")
HOSTED_SHALLOW_S4 = ("d7d5", "d5e4", "c8g4", "g4f3")
LIVE_LOSS_S4_HISTORY = (
    ("e2e4",),
    ("f7f6", "e8f7"),
    ("d2d4", "b1c3", "f1d3"),
)
LIVE_LOSS_S4 = ("d7d5", "c8g4", "d5e4", "g4d1")
LIVE_LOSS_S5_MATE = ("d3e4", "e4h7", "f2f4", "f4f5", "h7g6")
LIVE_LOSS_PUBLIC_EVIDENCE = {
    "source_fingerprint": "70f4e529539a7241",
    "runtime": "0.15 CPU / native1",
    "requested_depth": 5,
    "completed_depth": 2,
    "branch_cap": 32,
    "time_limit_seconds": 30.0,
    "max_generation_positions": 10_000_000,
}


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


def _live_loss_s4_state() -> ProgressiveState:
    state = ProgressiveState.initial()
    for series in LIVE_LOSS_S4_HISTORY:
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
    assert searcher.stats.root_safety_screen_calls == 1
    assert searcher.stats.root_safety_screen_cache_hits == 0
    charged_work = searcher.stats.work_positions
    assert searcher._root_child_immediate_mate(blunder.final_state) == mate
    assert searcher.stats.work_positions == charged_work
    assert searcher.stats.root_safety_screen_calls == 1
    assert searcher.stats.root_safety_screen_cache_hits == 1


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


def test_adaptive_early_s5_screen_rejects_the_second_live_loss() -> None:
    """A quiet-prefix mate must survive beyond the old width-832 screen."""

    root = _live_loss_s4_state()
    assert root.pfen == (
        "rnbq1bnr/pppppkpp/5p2/8/3PP3/2NB4/PPP2PPP/R1BQK1NR "
        "b KQ - 2 3 | series=4 quiet=0 progressive_ep=- "
        "rules=scottish-modern-common-v1 quiet_draw=manual-proof-required"
    )
    blunder = play_series(root, LIVE_LOSS_S4)
    human_mate = play_series(blunder.final_state, LIVE_LOSS_S5_MATE)
    assert human_mate.machine_notation == "/".join(LIVE_LOSS_S5_MATE)
    assert human_mate.outcome == Outcome.CHECKMATE
    assert human_mate.ended_by_check

    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            time_limit_seconds=30.0,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        PROFILE,
    )
    searcher._deadline = time.perf_counter() + 30.0
    screened = searcher._root_child_immediate_mate(blunder.final_state)

    assert screened is not None
    assert play_series(blunder.final_state, screened.moves).outcome == Outcome.CHECKMATE
    assert 0 < searcher.stats.work_positions <= ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT


def test_wide_reply_screen_does_not_materialize_without_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing native ABI must not turn the safety screen into a huge Python job."""

    def unexpected_python_generation(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("wide reply screen entered the Python generator")

    monkeypatch.setattr(evaluation_module, "_native_eval", None)
    monkeypatch.setattr(search_module, "generate_series", unexpected_python_generation)
    monkeypatch.setattr(
        search_module,
        "_native_complete_series_generation",
        unexpected_python_generation,
    )
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            time_limit_seconds=30.0,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        PROFILE,
    )

    mate, completed = searcher._root_child_mate_screen_stage(
        _live_loss_s4_state(),
        frontier=4096,
        tactical_protection=False,
    )

    assert mate is None
    assert not completed
    assert searcher.stats.root_safety_screen_positions == 0


def test_early_promotion_risk_keeps_the_historical_width832_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = play_series(
        play_series(ProgressiveState.initial(), ("e2e4",)).final_state,
        ("f7f6", "e8f7"),
    ).final_state
    assert state.series_number == 3
    calls: list[tuple[int, bool]] = []

    def fake_stage(
        _searcher: SeriesSearcher,
        _state: ProgressiveState,
        *,
        frontier: int,
        tactical_protection: bool,
    ) -> tuple[None, bool]:
        calls.append((frontier, tactical_protection))
        return None, True

    monkeypatch.setattr(
        search_module,
        "_tactical_frontier_protection_eligible",
        lambda _state: True,
    )
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_mate_screen_stage",
        fake_stage,
    )
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            collect_all_root_scores=False,
        ),
        PROFILE,
    )

    assert searcher._root_child_immediate_mate(state) is None
    assert calls == [(32, True), (832, False)]


def test_interrupted_safety_retry_keeps_the_last_completed_root_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _live_loss_s4_state()
    safe = play_series(root, ("f6f5", "f5e4", "e4d3", "d3d2"))
    unsafe = play_series(root, LIVE_LOSS_S4)
    reply_mate = play_series(unsafe.final_state, LIVE_LOSS_S5_MATE)
    safe_scored = search_module.ScoredSeries(safe, 100)
    unsafe_scored = search_module.ScoredSeries(unsafe, 243)

    def fake_pass(
        _searcher: SeriesSearcher,
        _state: ProgressiveState,
        depth: int,
        _required_prefix: tuple[str, ...],
        overrides: object,
    ) -> tuple[
        int,
        tuple[SeriesResult, ...],
        tuple[search_module.ScoredSeries, ...],
        None,
    ]:
        if depth == 1:
            return 100, (safe,), (safe_scored,), None
        if not overrides:
            return 243, (unsafe,), (unsafe_scored,), None
        raise search_module._RootInterrupted(
            (unsafe_scored,),
            search_module._Timeout(),
            unsafe,
        )

    def fake_screen(
        _searcher: SeriesSearcher,
        state: ProgressiveState,
    ) -> SeriesResult | None:
        return (
            reply_mate
            if state.position_hash == unsafe.final_state.position_hash
            else None
        )

    monkeypatch.setattr(SeriesSearcher, "_search_root_pass", fake_pass)
    monkeypatch.setattr(SeriesSearcher, "_root_child_immediate_mate", fake_screen)
    result = SeriesSearcher(
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            time_limit_seconds=30.0,
            collect_all_root_scores=False,
        ),
        PROFILE,
    ).run(root)

    assert result.completed_depth == 1
    assert result.timed_out
    assert result.best_series == safe
    assert result.score == 100
    assert result.alternatives == (safe_scored,)
    assert result.stats.root_safety_passes == 3
    assert result.stats.root_safety_retries == 1


@pytest.mark.parametrize(
    ("cause_type", "timed_out", "work_limit_reached"),
    (
        (search_module._Timeout, True, False),
        (search_module._WorkLimit, False, True),
    ),
)
def test_raw_retry_interruption_at_depth_zero_keeps_only_a_legal_move_fallback(
    monkeypatch: pytest.MonkeyPatch,
    cause_type: type[Exception],
    timed_out: bool,
    work_limit_reached: bool,
) -> None:
    root = _live_loss_s4_state()
    unsafe = play_series(root, LIVE_LOSS_S4)
    reply_mate = play_series(unsafe.final_state, LIVE_LOSS_S5_MATE)
    unsafe_scored = search_module.ScoredSeries(
        unsafe,
        243,
        (reply_mate,),
        (1, 1),
    )
    calls = 0

    def complete_then_interrupt_raw(
        searcher: SeriesSearcher,
        _state: ProgressiveState,
        _depth: int,
        _required_prefix: tuple[str, ...],
        _overrides: object,
    ) -> tuple[
        int,
        tuple[SeriesResult, ...],
        tuple[search_module.ScoredSeries, ...],
        str | None,
    ]:
        nonlocal calls
        calls += 1
        if calls == 1:
            searcher._root_scores_complete = True
            return 243, (unsafe,), (unsafe_scored,), "white"
        searcher._root_scores_complete = False
        raise cause_type()

    monkeypatch.setattr(
        SeriesSearcher,
        "_search_root_pass",
        complete_then_interrupt_raw,
    )
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_immediate_mate",
        lambda _searcher, state: (
            reply_mate
            if state.transposition_key == unsafe.final_state.transposition_key
            else None
        ),
    )
    result = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            time_limit_seconds=30.0,
            collect_all_root_scores=False,
        ),
        PROFILE,
    ).run(root)

    assert calls == 2
    assert result.completed_depth == 0
    assert result.best_series == unsafe
    assert result.principal_variation == (unsafe,)
    assert result.score == result.root_evaluation.total
    assert result.alternatives == ()
    assert result.proof is None
    assert not result.root_scores_complete
    assert result.timed_out is timed_out
    assert result.work_limit_reached is work_limit_reached
    assert result.stats.root_safety_passes == 2
    assert result.stats.root_safety_retries == 1


def test_raw_retry_timeout_restores_last_completed_root_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _live_loss_s4_state()
    safe = play_series(root, ("f6f5", "f5e4", "e4d3", "d3d2"))
    unsafe = play_series(root, LIVE_LOSS_S4)
    reply_mate = play_series(unsafe.final_state, LIVE_LOSS_S5_MATE)
    safe_scored = search_module.ScoredSeries(safe, 100, (), (-1, 1))
    unsafe_scored = search_module.ScoredSeries(
        unsafe,
        243,
        (reply_mate,),
        (1, 1),
    )

    def complete_then_interrupt_raw(
        searcher: SeriesSearcher,
        _state: ProgressiveState,
        depth: int,
        _required_prefix: tuple[str, ...],
        overrides: object,
    ) -> tuple[
        int,
        tuple[SeriesResult, ...],
        tuple[search_module.ScoredSeries, ...],
        str | None,
    ]:
        if depth == 1:
            searcher._root_scores_complete = True
            return 100, (safe,), (safe_scored,), None
        searcher._root_scores_complete = False
        if not overrides:
            return 243, (unsafe,), (unsafe_scored,), "white"
        raise search_module._Timeout()

    monkeypatch.setattr(
        SeriesSearcher,
        "_search_root_pass",
        complete_then_interrupt_raw,
    )
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_immediate_mate",
        lambda _searcher, state: (
            reply_mate
            if state.transposition_key == unsafe.final_state.transposition_key
            else None
        ),
    )
    result = SeriesSearcher(
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            time_limit_seconds=30.0,
            collect_all_root_scores=False,
        ),
        PROFILE,
    ).run(root)

    assert result.completed_depth == 1
    assert result.timed_out
    assert result.best_series == safe
    assert result.score == 100
    assert result.alternatives == (safe_scored,)
    assert result.proof is None
    assert result.root_scores_complete
    assert result.stats.root_safety_passes == 3
    assert result.stats.root_safety_retries == 1


def test_hosted_depth_two_selects_a_screened_reply_to_the_second_live_loss() -> None:
    assert LIVE_LOSS_PUBLIC_EVIDENCE["source_fingerprint"] == "70f4e529539a7241"
    assert LIVE_LOSS_PUBLIC_EVIDENCE["requested_depth"] == 5
    assert LIVE_LOSS_PUBLIC_EVIDENCE["completed_depth"] == 2
    root = _live_loss_s4_state()
    result = analyze(
        root,
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            time_limit_seconds=30.0,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        PROFILE,
    )

    assert result.best_series is not None
    assert result.best_series.moves == ("f6f5", "f5e4", "e4d3", "d3d2")
    assert result.best_series.moves != LIVE_LOSS_S4
    assert result.completed_depth == 2
    assert result.score == 639
    assert result.stats.work_positions < 300_000
    assert result.stats.root_safety_passes == 4
    assert result.stats.root_safety_retries == 2
    assert result.stats.root_safety_screen_calls == 4
    assert result.stats.root_safety_screen_stages == 7
    assert result.stats.root_safety_screen_positions < 175_000
    rejected = next(
        item for item in result.alternatives if item.series.moves == LIVE_LOSS_S4
    )
    assert rejected.score == MATE_SCORE - 2
    assert rejected.proof == "white"
    assert rejected.principal_variation
    assert rejected.principal_variation[0].outcome == Outcome.CHECKMATE
    selected_child = play_series(root, result.best_series.moves).final_state
    verifier = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            time_limit_seconds=30.0,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        PROFILE,
    )
    verifier._deadline = time.perf_counter() + 30.0
    assert verifier._root_child_immediate_mate(selected_child) is None
    assert 0 < verifier.stats.work_positions <= ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT


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


def test_root_tactical_protection_retains_the_stronger_s4_candidate() -> None:
    """Every root gets tactical lanes even when descendants stay low-risk."""

    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        PROFILE,
    )
    retained = searcher._ordered_generated(
        _early_s4_state(),
        ply_from_root=1,
    )

    assert any(candidate.moves == ROOT_TACTICAL_S4 for candidate in retained)
    assert searcher.stats.tactical_frontier_states_retained > 0
    assert searcher.stats.tactical_final_series_retained > 0
    assert searcher.stats.tactical_final_reserve_drops > 0


def test_root_tactical_protection_keeps_depth_three_opening_result() -> None:
    result = analyze(
        ProgressiveState.initial(),
        SearchLimits(
            depth_series=3,
            max_series_per_node=32,
            max_generation_positions=1_000_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        PROFILE,
    )

    assert result.best_series is not None
    assert result.best_series.moves == ("e2e4",)
    assert result.score == 848
    assert result.completed_depth == 3
    assert result.stats.work_positions == 341_820
    assert result.stats.tactical_frontier_states_retained == 0
    assert result.stats.tactical_frontier_reserve_drops == 0


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
    assert result.best_series.moves == HOSTED_SHALLOW_S4
    assert result.completed_depth == 2
    selected_child = play_series(root, result.best_series.moves).final_state
    reply_mate, verifier_work = _ordinary_cap832_reply_mate(selected_child)
    assert reply_mate is None
    assert 0 < verifier_work < 50_000


def test_website_depth_four_selects_stronger_safe_root_tactical_line() -> None:
    """Root protection changes the live choice, not merely its mate screen."""

    root = _early_s4_state()
    limits = SearchLimits(
        depth_series=4,
        max_series_per_node=32,
        max_generation_positions=10_000_000,
        time_limit_seconds=30.0,
        collect_all_root_scores=False,
        native_threads=16,
    )
    result = analyze(root, limits, PROFILE)

    assert result.best_series is not None
    assert result.best_series.moves == ROOT_TACTICAL_S4
    assert result.score == 1_078
    assert result.completed_depth == 4
    assert result.proof is None
    assert not result.exact_width
    assert result.stats.tactical_frontier_states_retained > 0
    assert result.stats.tactical_frontier_reserve_drops > 0

    prior = analyze(
        root,
        limits,
        PROFILE,
        required_prefix=PRIOR_SAFE_S4,
    )
    assert prior.best_series is not None
    assert prior.best_series.moves == PRIOR_SAFE_S4
    assert prior.score == 1_885
    assert prior.completed_depth == 4
    # Scores are white-centric heuristic values, so Black prefers the lower
    # score. Neither selective result is a proof of a win.
    assert result.score < prior.score

    selected_child = play_series(root, result.best_series.moves).final_state
    reply_mate, verifier_work = _ordinary_cap832_reply_mate(
        selected_child,
        native_threads=16,
    )
    assert reply_mate is None
    assert 0 < verifier_work < 50_000


def test_early_s4_depth_five_avoids_every_cap832_replay_mate() -> None:
    """Opt-in 10m-work depth-five gate, expected to complete depth four."""

    if os.environ.get("SPC_RUN_EARLY_S4_GATE") != "1":
        pytest.skip("set SPC_RUN_EARLY_S4_GATE=1 for the early S4 strength gate")

    root = _early_s4_state()
    limits = SearchLimits(
        depth_series=5,
        max_series_per_node=32,
        max_generation_positions=10_000_000,
        time_limit_seconds=30.0,
        collect_all_root_scores=False,
        native_threads=16,
    )
    result = analyze(root, limits, PROFILE)

    assert result.best_series is not None
    assert result.best_series.moves == ROOT_TACTICAL_S4
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
