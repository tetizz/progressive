from __future__ import annotations

import os
import time

import pytest

import scottish_progressive.evaluation as evaluation_module
import scottish_progressive.search as search_module
import scottish_progressive.series_mate as series_mate_module
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
from scottish_progressive.series_mate import SeriesMateProbe, SeriesMateStatus


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
    assert blunder.final_state.transposition_key in (
        searcher._root_child_proven_mate_keys
    )
    assert searcher._root_safety_fallback(blunder) is None
    assert searcher.stats.root_safety_exhausted_fallbacks == 0
    assert searcher.stats.root_safety_unknown_fallbacks == 0
    charged_work = searcher.stats.work_positions
    assert searcher._root_child_immediate_mate(blunder.final_state) == mate
    assert searcher.stats.work_positions == charged_work
    assert searcher.stats.root_safety_screen_calls == 1
    assert searcher.stats.root_safety_screen_cache_hits == 1


def test_process_local_mate_cache_separates_exact_fen_clocks() -> None:
    first = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
        1,
    )
    second = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 9 7",
        1,
    )
    assert first.transposition_key == second.transposition_key

    searcher = _live_searcher()
    first_mate = searcher._root_child_immediate_mate(first)
    second_mate = searcher._root_child_immediate_mate(second)

    assert first_mate is not None
    assert second_mate is not None
    assert first_mate.moves == second_mate.moves == ("g6g7",)
    assert first_mate.final_state.pfen != second_mate.final_state.pfen
    assert second_mate.final_state.pfen == play_series(
        second,
        second_mate.moves,
    ).final_state.pfen
    assert searcher.stats.root_safety_screen_calls == 2
    assert searcher.stats.root_safety_screen_cache_hits == 0


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
    """The exact lane finds the quiet-prefix mate below the old 81,476 work."""

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
    assert searcher.stats.native_series_mate_calls == 1
    assert searcher.stats.native_series_mate_found == 1
    assert searcher.stats.native_series_mate_positions == 600
    assert searcher.stats.native_series_mate_edges == 24_006
    assert searcher.stats.root_safety_screen_stages == 1
    assert searcher.stats.work_positions == 30_837
    assert searcher.stats.work_positions < 81_476 // 2


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


def test_exact_exhaustion_is_cached_by_child_transposition_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K7 w - - 0 1",
        5,
    )
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_mate_screen_stage",
        lambda *_args, **_kwargs: (None, True),
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
    charged = searcher.stats.work_positions
    assert searcher.stats.native_series_mate_calls == 1
    assert searcher.stats.native_series_mate_exhausted == 1
    assert searcher._root_child_immediate_mate(state) is None
    assert searcher.stats.work_positions == charged
    assert searcher.stats.native_series_mate_cache_hits == 1

    exhausted_candidate = SeriesResult(("a1a2",), ("Ka2",), state)
    unknown_state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/1K6 w - - 0 1",
        5,
    )
    unknown_candidate = SeriesResult(("a1b1",), ("Kb1",), unknown_state)
    assert (
        searcher._root_safety_fallback(unknown_candidate, exhausted_candidate)
        == exhausted_candidate
    )
    assert searcher.stats.root_safety_exhausted_fallbacks == 1
    assert searcher.stats.root_safety_unknown_fallbacks == 0


def test_all_mating_widening_compares_every_exact_safe_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    first_safe = play_series(root, ("e2e4",))
    later_stronger = play_series(root, ("d2d4",))
    scores = {
        first_safe.final_state.transposition_key: 100,
        later_stronger.final_state.transposition_key: 400,
    }

    monkeypatch.setattr(
        SeriesSearcher,
        "_generate",
        lambda *_args, **_kwargs: ([first_safe, later_stronger], False),
    )

    def exact_safe(
        searcher: SeriesSearcher,
        state: ProgressiveState,
    ) -> None:
        searcher._mark_root_child_exact_exhausted(state.transposition_key)
        return None

    def scored_child(
        _searcher: SeriesSearcher,
        state: ProgressiveState,
        _depth: int,
        _alpha: int,
        _beta: int,
        _ply_from_root: int,
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[int, int]]:
        return scores[state.transposition_key], (), search_module.UNKNOWN_PROOF_BOUNDS

    monkeypatch.setattr(SeriesSearcher, "_root_child_immediate_mate", exact_safe)
    monkeypatch.setattr(SeriesSearcher, "_minimax", scored_child)
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            collect_all_root_scores=False,
        ),
        PROFILE,
    )

    score, pv, alternatives, proof = searcher._root_all_mating_widening(
        root,
        1,
        (),
        (),
    )

    assert score == 400
    assert pv == (later_stronger,)
    assert [item.series for item in alternatives] == [later_stronger, first_safe]
    assert proof is None
    assert not searcher._root_scores_complete
    assert searcher.stats.root_safety_widened_exact_children == 2


def test_all_mating_widening_interruption_keeps_best_safe_move_without_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    safe = play_series(root, ("e2e4",))
    unknown = play_series(root, ("d2d4",))
    monkeypatch.setattr(
        SeriesSearcher,
        "_generate",
        lambda *_args, **_kwargs: ([safe, unknown], False),
    )

    def screen(
        searcher: SeriesSearcher,
        state: ProgressiveState,
    ) -> None:
        if state.transposition_key == safe.final_state.transposition_key:
            searcher._mark_root_child_exact_exhausted(state.transposition_key)
            return None
        raise search_module._WorkLimit

    monkeypatch.setattr(SeriesSearcher, "_root_child_immediate_mate", screen)
    monkeypatch.setattr(
        SeriesSearcher,
        "_minimax",
        lambda *_args, **_kwargs: (250, (), search_module.UNKNOWN_PROOF_BOUNDS),
    )
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            collect_all_root_scores=False,
        ),
        PROFILE,
    )

    with pytest.raises(search_module._RootInterrupted) as captured:
        searcher._root_all_mating_widening(root, 1, (), ())

    interrupted = captured.value
    assert interrupted.fallback == safe
    assert interrupted.scored == ()
    assert isinstance(interrupted.cause, search_module._WorkLimit)
    assert searcher.stats.root_safety_widened_exact_children == 1
    assert searcher.stats.root_safety_exhausted_fallbacks == 1
    assert searcher.stats.root_safety_unknown_fallbacks == 0


def test_series_two_cap32_miss_cannot_hide_authoritative_native_mate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProgressiveState.from_fen(
        "8/8/8/8/8/5k2/4q3/7K b - - 0 1",
        2,
    )
    reply_mate = play_series(state, ("e2g2",))
    assert reply_mate.outcome is Outcome.CHECKMATE
    stages = 0

    def malicious_cap32_miss(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[None, bool]:
        nonlocal stages
        stages += 1
        return None, True

    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_mate_screen_stage",
        malicious_cap32_miss,
    )
    monkeypatch.setattr(
        series_mate_module,
        "find_native_series_mate",
        lambda *_args, **_kwargs: SeriesMateProbe(
            SeriesMateStatus.FOUND,
            "authoritative early mate",
            series=reply_mate,
            positions_visited=1,
            moves_generated=19,
        ),
    )
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            collect_all_root_scores=False,
        ),
        PROFILE,
    )

    assert searcher._root_child_immediate_mate(state) == reply_mate
    assert searcher._root_child_immediate_mate(state) == reply_mate
    assert stages == 1
    assert searcher.stats.native_series_mate_calls == 1
    assert searcher.stats.native_series_mate_found == 1
    assert searcher.stats.native_series_mate_cache_hits == 1
    assert searcher.stats.root_safety_proven_mate_children == 1
    assert searcher.stats.root_safety_exact_exhausted_children == 0


def test_series_two_exact_exhaustion_is_the_only_cached_safe_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
    assert state.series_number == 2
    stages = 0

    def completed_cap32_miss(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[None, bool]:
        nonlocal stages
        stages += 1
        return None, True

    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_mate_screen_stage",
        completed_cap32_miss,
    )
    monkeypatch.setattr(
        series_mate_module,
        "find_native_series_mate",
        lambda *_args, **_kwargs: SeriesMateProbe(
            SeriesMateStatus.EXHAUSTED,
            "authoritative early no-mate",
            positions_visited=21,
            moves_generated=466,
        ),
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
    assert searcher._root_child_immediate_mate(state) is None
    assert stages == 1
    assert searcher.stats.native_series_mate_calls == 1
    assert searcher.stats.native_series_mate_exhausted == 1
    assert searcher.stats.native_series_mate_cache_hits == 1
    assert searcher.stats.root_safety_exact_exhausted_children == 1
    assert searcher.stats.root_safety_proven_mate_children == 0
    assert state.transposition_key in searcher._root_child_native_mate_exhausted_keys


@pytest.mark.parametrize("remaining", (0, 1, 2))
@pytest.mark.parametrize("series_number", (1, 2, 4, 5, 8))
@pytest.mark.parametrize(
    "native_status",
    (
        SeriesMateStatus.EXHAUSTED,
        SeriesMateStatus.UNSUPPORTED,
        SeriesMateStatus.WORK_LIMIT,
        SeriesMateStatus.DEADLINE,
    ),
)
def test_exact_lane_no_call_or_unknown_status_never_caches_a_selective_miss(
    monkeypatch: pytest.MonkeyPatch,
    remaining: int,
    series_number: int,
    native_status: SeriesMateStatus,
) -> None:
    state = ProgressiveState.from_fen(
        (
            "7k/8/8/8/8/8/8/K7 w - - 0 1"
            if series_number % 2
            else "7k/8/8/8/8/8/8/K7 b - - 0 1"
        ),
        series_number,
    )
    stage_calls = 0
    exact_work_limits: list[int | None] = []

    def completed_selective_miss(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[None, bool]:
        nonlocal stage_calls
        stage_calls += 1
        return None, True

    def exact_probe(*_args: object, **kwargs: object) -> SeriesMateProbe:
        exact_work_limits.append(kwargs.get("max_work"))
        return SeriesMateProbe(native_status, f"forced {native_status.value}")

    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_promotion_mate",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_mate_screen_stage",
        completed_selective_miss,
    )
    # Series 8 normally selects a tactical recovery stage. Force the
    # non-tactical branch so this table covers both early and late children.
    monkeypatch.setattr(
        search_module,
        "_tactical_frontier_protection_eligible",
        lambda _state: False,
    )
    monkeypatch.setattr(
        series_mate_module,
        "find_native_series_mate",
        exact_probe,
    )
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            collect_all_root_scores=False,
        ),
        PROFILE,
    )
    searcher._root_child_mate_screen_work = (
        searcher._root_child_mate_screen_budget - remaining
    )

    for _attempt in range(2):
        if remaining <= 1 or native_status is SeriesMateStatus.WORK_LIMIT:
            with pytest.raises(search_module._WorkLimit):
                searcher._root_child_immediate_mate(state)
        elif native_status is SeriesMateStatus.UNSUPPORTED:
            with pytest.raises(search_module._WorkLimit):
                searcher._root_child_immediate_mate(state)
        elif native_status is SeriesMateStatus.DEADLINE:
            with pytest.raises(search_module._Timeout):
                searcher._root_child_immediate_mate(state)
        else:
            assert searcher._root_child_immediate_mate(state) is None

    position_key = state.transposition_key
    cache_key = searcher._tt_key(state)
    if remaining == 2 and native_status is SeriesMateStatus.EXHAUSTED:
        assert stage_calls == 1
        assert exact_work_limits == [1]
        assert cache_key in searcher._root_child_mate_screen_cache
        assert cache_key in searcher._root_child_native_mate_cache_keys
        assert position_key in searcher._root_child_native_mate_exhausted_keys
        assert searcher.stats.native_series_mate_cache_hits == 1
        assert searcher.stats.root_safety_exact_exhausted_children == 1
    else:
        assert stage_calls >= 2
        assert cache_key not in searcher._root_child_mate_screen_cache
        assert cache_key not in searcher._root_child_native_mate_cache_keys
        assert position_key not in searcher._root_child_native_mate_exhausted_keys
        assert searcher.stats.native_series_mate_cache_hits == 0
        assert searcher.stats.root_safety_exact_exhausted_children == 0
        assert searcher.stats.root_safety_proven_mate_children == 0
        if remaining <= 1:
            assert exact_work_limits == []
            assert searcher.stats.root_safety_budget_interruptions == 2
        else:
            assert exact_work_limits == [1, 1]


def test_incomplete_exact_lane_cannot_omit_unknown_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_promotion_mate",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_mate_screen_stage",
        lambda *_args, **_kwargs: (None, True),
    )
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_exact_native_mate",
        lambda *_args, **_kwargs: (False, None, False),
    )
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            collect_all_root_scores=False,
        ),
        PROFILE,
    )

    with pytest.raises(RuntimeError, match="must be classified as unknown"):
        searcher._root_child_immediate_mate(state)
    assert searcher._tt_key(state) not in searcher._root_child_mate_screen_cache
    assert (
        state.transposition_key
        not in searcher._root_child_native_mate_exhausted_keys
    )


@pytest.mark.parametrize(
    ("status", "counter"),
    (
        (SeriesMateStatus.WORK_LIMIT, "native_series_mate_work_limit_hits"),
        (SeriesMateStatus.UNSUPPORTED, "native_series_mate_unsupported"),
    ),
)
def test_unknown_exact_status_cannot_certify_a_root_child(
    monkeypatch: pytest.MonkeyPatch,
    status: SeriesMateStatus,
    counter: str,
) -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K7 w - - 0 1",
        5,
    )
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_mate_screen_stage",
        lambda *_args, **_kwargs: (None, True),
    )
    monkeypatch.setattr(
        series_mate_module,
        "find_native_series_mate",
        lambda *_args, **_kwargs: SeriesMateProbe(
            status,
            "forced unknown",
            positions_visited=2,
            moves_generated=3,
        ),
    )
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            collect_all_root_scores=False,
        ),
        PROFILE,
    )

    with pytest.raises(search_module._WorkLimit):
        searcher._root_child_immediate_mate(state)
    assert getattr(searcher.stats, counter) == 1
    assert searcher.stats.root_safety_unknown_interruptions == 1
    assert searcher.stats.work_positions == 5


def test_exact_deadline_interrupts_instead_of_falling_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K7 w - - 0 1",
        5,
    )
    stages = 0

    def completed_stage(*_args: object, **_kwargs: object) -> tuple[None, bool]:
        nonlocal stages
        stages += 1
        return None, True

    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_mate_screen_stage",
        completed_stage,
    )
    monkeypatch.setattr(
        series_mate_module,
        "find_native_series_mate",
        lambda *_args, **_kwargs: SeriesMateProbe(
            SeriesMateStatus.DEADLINE,
            "forced deadline",
        ),
    )
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            collect_all_root_scores=False,
        ),
        PROFILE,
    )

    with pytest.raises(search_module._Timeout):
        searcher._root_child_immediate_mate(state)
    assert stages == 1
    assert searcher.stats.native_series_mate_deadline_hits == 1


def test_early_promotion_risk_uses_width832_only_to_recover_unknown_native_mate(
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
    monkeypatch.setattr(
        series_mate_module,
        "find_native_series_mate",
        lambda *_args, **_kwargs: SeriesMateProbe(
            SeriesMateStatus.UNSUPPORTED,
            "forced early unknown",
        ),
    )
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            collect_all_root_scores=False,
        ),
        PROFILE,
    )

    with pytest.raises(search_module._WorkLimit):
        searcher._root_child_immediate_mate(state)
    assert calls == [(32, True), (832, False)]
    assert searcher.stats.native_series_mate_calls == 1
    assert searcher.stats.native_series_mate_unsupported == 1
    assert searcher.stats.root_safety_unknown_interruptions == 1
    assert searcher._tt_key(state) not in searcher._root_child_mate_screen_cache


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
def test_raw_retry_interruption_never_falls_back_to_a_proven_mate_child(
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
    assert result.best_series is None
    assert result.principal_variation == ()
    assert result.score == result.root_evaluation.total
    assert result.alternatives == ()
    assert result.proof is None
    assert not result.root_scores_complete
    assert result.timed_out is timed_out
    assert result.work_limit_reached is work_limit_reached
    assert result.stats.root_safety_passes == 2
    assert result.stats.root_safety_retries == 1
    assert result.stats.root_safety_proven_mate_children == 1
    assert result.stats.root_safety_exhausted_fallbacks == 0
    assert result.stats.root_safety_unknown_fallbacks == 0


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
    assert result.stats.work_positions < 2_000_000
    assert result.stats.root_safety_passes == 4
    assert result.stats.root_safety_retries == 2
    assert result.stats.root_safety_screen_calls == 4
    assert result.stats.root_safety_screen_stages == 4
    assert result.stats.root_safety_screen_positions < 1_750_000
    assert result.stats.native_series_mate_calls == 3
    assert result.stats.native_series_mate_found == 1
    assert result.stats.native_series_mate_exhausted == 2
    assert result.stats.native_series_mate_work_limit_hits == 0
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
    # each authoritative native exhaustion is tiny; exact safety adds 972
    # position/edge units and the opening remains near its prior 30k envelope
    # without widening to 832.
    assert 0 < cheap_calls <= 8
    assert wide_calls == 0
    assert result.stats.work_positions == 30_610
    assert result.stats.native_series_mate_calls == 2
    assert result.stats.native_series_mate_exhausted == 2
    assert result.stats.root_safety_exact_exhausted_children == 2
    assert result.stats.tactical_frontier_states_retained == 0
    assert result.stats.tactical_frontier_reserve_drops == 0
    assert result.stats.tactical_final_series_retained == 0
    assert result.stats.tactical_final_reserve_drops == 0


@pytest.mark.parametrize(
    (
        "state",
        "expected_moves",
        "expected_score",
        "expected_work",
        "expected_safety_work",
        "expected_native_work",
    ),
    (
        (
            ProgressiveState.initial(),
            ("g2g3",),
            -150,
            30_610,
            1_054,
            972,
        ),
        (
            play_series(ProgressiveState.initial(), ("e2e4",)).final_state,
            ("f7f5", "e8f7"),
            848,
            68_537,
            31_162,
            30_046,
        ),
    ),
)
def test_public_faster_opening_contract_settles_every_reply_exactly(
    state: ProgressiveState,
    expected_moves: tuple[str, ...],
    expected_score: int,
    expected_work: int,
    expected_safety_work: int,
    expected_native_work: int,
) -> None:
    result = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            time_limit_seconds=5.0,
            max_generation_positions=500_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        PROFILE,
    )

    assert result.best_series is not None
    assert result.best_series.moves == expected_moves
    assert play_series(state, result.best_series.moves) == result.best_series
    assert result.score == expected_score
    assert result.completed_depth == 2
    assert not result.timed_out
    assert not result.work_limit_reached
    assert result.stats.work_positions == expected_work
    assert result.stats.root_safety_screen_positions == expected_safety_work
    assert (
        result.stats.native_series_mate_positions
        + result.stats.native_series_mate_edges
        == expected_native_work
    )
    assert result.stats.native_series_mate_calls == 2
    assert result.stats.native_series_mate_exhausted == 2
    assert result.stats.native_series_mate_found == 0
    assert result.stats.native_series_mate_work_limit_hits == 0
    assert result.stats.native_series_mate_deadline_hits == 0
    assert result.stats.native_series_mate_unsupported == 0
    assert result.stats.root_safety_exact_exhausted_children == 2
    assert result.stats.root_safety_unknown_interruptions == 0
    assert result.stats.root_safety_budget_interruptions == 0


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
    assert result.stats.work_positions == 245_028
    assert result.stats.root_pvs_zero_window_searches > 0
    assert result.stats.native_series_mate_calls == 3
    assert result.stats.native_series_mate_exhausted == 3
    assert result.stats.root_safety_exact_exhausted_children == 3
    assert result.stats.tactical_frontier_states_retained == 0
    assert result.stats.tactical_frontier_reserve_drops == 0


def test_shallow_s4_unknown_returns_only_a_move_liveness_fallback() -> None:
    """A 250k cap cannot certify the S5 negative and must not fake depth."""

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
    assert result.completed_depth == 0
    assert result.work_limit_reached
    assert not result.timed_out
    assert result.score == result.root_evaluation.total
    assert result.principal_variation == (result.best_series,)
    assert result.alternatives == ()
    assert result.proof is None
    assert not result.root_scores_complete
    assert result.stats.native_series_mate_work_limit_hits == 1
    assert result.stats.root_safety_unknown_interruptions == 1


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


def test_s7_small_safety_budget_discards_root_metadata_as_unknown() -> None:
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=250_000,
            time_limit_seconds=30.0,
            collect_all_root_scores=False,
            native_threads=16,
        ),
        PROFILE,
    )
    result = searcher.run(S7_STATE)

    assert result.best_series is not None
    assert result.best_series.moves != BLUNDERING_S7
    assert result.completed_depth == 0
    assert not result.timed_out
    assert result.work_limit_reached
    assert result.score == result.root_evaluation.total
    assert result.principal_variation == (result.best_series,)
    assert result.alternatives == ()
    assert result.proof is None
    assert not result.root_scores_complete
    assert result.stats.root_safety_budget_interruptions == 1
    assert result.stats.root_safety_unknown_fallbacks == 1
    assert result.stats.root_safety_exhausted_fallbacks == 0
    assert result.stats.root_safety_proven_mate_children > 0
    assert (
        result.stats.root_safety_screen_positions
        == 250_000 // 3
    )

    # This is intentionally only a move-liveness fallback. A fresh exact
    # verifier exposes the real S8 mate that the old width-832 negative missed;
    # no score, proof, or alternative from the discarded depth may leak.
    selected_child = play_series(S7_STATE, result.best_series.moves).final_state
    assert selected_child.transposition_key not in searcher._root_child_proven_mate_keys
    assert (
        selected_child.transposition_key
        not in searcher._root_child_native_mate_exhausted_keys
    )
    verifier = _live_searcher()
    mate = verifier._root_child_immediate_mate(selected_child)
    assert mate is not None
    assert play_series(selected_child, mate.moves).outcome == Outcome.CHECKMATE
    assert verifier.stats.work_positions <= ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT


def test_s7_cap32_misses_terminal_mate_that_cap832_ranks_first() -> None:
    target = (
        "a1c1",
        "c1d1",
        "d2c3",
        "g1f3",
        "f3g5",
        "g5e6",
        "d1d8",
    )
    narrow = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        PROFILE,
    )
    narrow_retained = narrow._ordered_generated(S7_STATE, ply_from_root=1)
    wide = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=832,
            max_generation_positions=10_000_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        PROFILE,
    )
    wide_retained = wide._ordered_generated(S7_STATE, ply_from_root=1)

    assert target not in {candidate.moves for candidate in narrow_retained}
    assert narrow.stats.work_positions == 7_502
    assert wide_retained[0].moves == target
    assert wide_retained[0].outcome == Outcome.CHECKMATE
    assert wide_retained[0].ended_by_check
    assert wide.stats.work_positions == 41_928


def test_hosted_s7_widens_all_mating_cap32_and_plays_the_root_mate() -> None:
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=5,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            time_limit_seconds=30.0,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        PROFILE,
    )
    result = searcher.run(S7_STATE)

    assert result.best_series is not None
    assert result.best_series.moves == (
        "a1c1",
        "c1d1",
        "d2c3",
        "g1f3",
        "f3g5",
        "g5e6",
        "d1d8",
    )
    assert result.best_series.outcome == Outcome.CHECKMATE
    assert result.best_series.ended_by_check
    assert result.completed_depth == 5
    assert not result.timed_out
    assert not result.work_limit_reached
    assert result.score == MATE_SCORE - 1
    assert result.proof == "white"
    assert result.principal_variation == (result.best_series,)
    assert result.alternatives == (
        search_module.ScoredSeries(
            result.best_series,
            MATE_SCORE - 1,
            (),
            (1, 1),
        ),
    )
    assert not result.root_scores_complete
    assert result.stats.root_safety_retries == 32
    assert result.stats.root_safety_proven_mate_children == 32
    assert result.stats.root_safety_exact_exhausted_children == 0
    assert result.stats.root_safety_budget_interruptions == 0
    assert result.stats.root_safety_unknown_interruptions == 0
    assert result.stats.root_safety_exhausted_fallbacks == 0
    assert result.stats.root_safety_unknown_fallbacks == 0
    assert result.stats.root_safety_screen_positions < 1_000_000
    assert result.stats.root_safety_all_mating_widenings == 1
    assert result.stats.root_safety_widened_candidates == 832
    assert result.stats.root_safety_widening_positions == 41_928
    assert result.stats.root_safety_widened_terminal_mates == 1
    assert result.stats.root_safety_widened_exact_children == 0
    assert result.stats.work_positions < 1_000_000


def test_s8_underpromotion_proof_runs_before_general_native_search() -> None:
    state = ProgressiveState.from_fen(
        "7R/pp3p1p/1p3k2/3P4/1b6/5P2/PPP2P1P/RNK5 b - - 0 1",
        8,
    )
    searcher = _live_searcher()
    mate = searcher._root_child_immediate_mate(state)

    assert mate is not None
    assert mate.moves == (
        "b4d6",
        "b6b5",
        "b5b4",
        "b4b3",
        "b3a2",
        "a2b1n",
        "b1c3",
        "d6f4",
    )
    assert play_series(state, mate.moves).outcome == Outcome.CHECKMATE
    assert searcher.stats.promotion_mate_mates == 1
    assert searcher.stats.native_series_mate_calls == 0
    assert searcher.stats.root_safety_screen_stages == 0


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
