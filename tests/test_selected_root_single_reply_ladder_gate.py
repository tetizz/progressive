from __future__ import annotations

from types import SimpleNamespace
import time

import chess
import pytest

from scottish_progressive import search as search_module
from scottish_progressive import series_mate
from scottish_progressive import single_reply_mate_ladder as ladder_module
from scottish_progressive.model import ProgressiveState
from scottish_progressive.rules import generate_series, play_series
from scottish_progressive.search import ScoredSeries, SearchLimits, SeriesSearcher
from scottish_progressive.selected_pv_horizon import (
    SelectedPvHorizonCertification,
    SelectedPvHorizonStatus,
)
from scottish_progressive.series_mate import SeriesMateStatus
from scottish_progressive.single_reply_mate_ladder import (
    SingleReplyMateLadderProbe,
    SingleReplyMateLadderStats,
    SingleReplyMateLadderStatus,
)
from scottish_progressive.teacher_value_features import state_from_pfen


BUCEPHALUS_3DFD_S6_PFEN = (
    "Nnb1kbnr/pppp2pp/4p3/5p2/8/3P4/PPPKPPPP/3R1BNR b k - 1 7 "
    "| series=6 quiet=0 progressive_ep=- "
    "rules=scottish-modern-common-v1 quiet_draw=manual-proof-required"
)
BUCEPHALUS_3DFD_LOSING_ROOT = (
    "f5f4",
    "f4f3",
    "f3g2",
    "g2h1q",
    "h1h2",
    "h2f4",
)
BUCEPHALUS_3DFD_ELIGIBLE_ROOT = (
    "f5f4",
    "f4f3",
    "f3g2",
    "g2h1q",
    "h1h2",
    "h2g1",
)
BUCEPHALUS_ATTACK = (
    "d2c3",
    "d3d4",
    "d4d5",
    "d5e6",
    "d1d7",
    "a8c7",
)
BUCEPHALUS_FORCED_REPLY = ("f4c7",)
BUCEPHALUS_MATE = (
    "c3b3",
    "a2a4",
    "c2c4",
    "c4c5",
    "c5c6",
    "e2e4",
    "d7f7",
    "e6e7",
    "e7f8q",
)
TINY_FOUND_FEN = "6Q1/6R1/4Q3/1q6/8/5K2/8/4r2k w - - 0 1"


def _require_native_ladder() -> None:
    native = series_mate._native_mate  # noqa: SLF001
    if native is None or not hasattr(native, "find_single_reply_mate_ladder"):
        pytest.skip("source-matched native ladder extension is unavailable")
    assert native.SOURCE_IDENTITY == (  # noqa: SLF001
        series_mate._native_mate_source_identity()  # noqa: SLF001
    )


def _not_applicable() -> SelectedPvHorizonCertification:
    return SelectedPvHorizonCertification(
        SelectedPvHorizonStatus.NOT_APPLICABLE,
        True,
        None,
    )


def _exhausted_probe(*, work: int = 0) -> SingleReplyMateLadderProbe:
    return SingleReplyMateLadderProbe(
        SingleReplyMateLadderStatus.EXHAUSTED,
        SeriesMateStatus.EXHAUSTED,
        "exact narrow ladder absent",
        SingleReplyMateLadderStats(attack_positions_visited=work),
    )


def _mark_exact_immediate_miss(
    searcher: SeriesSearcher,
    state: ProgressiveState,
) -> None:
    cache_key = searcher._tt_key(state)  # noqa: SLF001
    searcher._root_child_mate_screen_cache[cache_key] = None  # noqa: SLF001
    searcher._root_child_native_mate_cache_keys.add(cache_key)  # noqa: SLF001
    searcher._mark_root_child_exact_exhausted(state.transposition_key)  # noqa: SLF001


def _mark_full_state_exact_immediate_miss(
    searcher: SeriesSearcher,
    state: ProgressiveState,
) -> None:
    """Matches final safe reselection's full_state_only evidence contract."""

    cache_key = searcher._tt_key(state)  # noqa: SLF001
    searcher._root_child_mate_screen_cache[cache_key] = None  # noqa: SLF001
    searcher._root_child_native_mate_cache_keys.add(cache_key)  # noqa: SLF001


def test_recorded_black_s6_candidate_is_vetoed_after_immediate_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_native_ladder()
    root = state_from_pfen(BUCEPHALUS_3DFD_S6_PFEN)
    losing = play_series(root, BUCEPHALUS_3DFD_LOSING_ROOT)
    eligible = play_series(root, BUCEPHALUS_3DFD_ELIGIBLE_ROOT)
    candidates = {
        losing.machine_notation: ScoredSeries(losing, 400),
        eligible.machine_notation: ScoredSeries(eligible, 300),
    }
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=3_000_000,
        )
    )
    events: list[tuple[str, str]] = []

    def root_pass(
        _root,
        _depth,
        _prefix,
        _mate_overrides,
        _horizon_overrides,
        exclusions,
        _frontier,
    ):
        selected = next(
            candidate
            for notation, candidate in candidates.items()
            if notation not in exclusions
        )
        return selected.score, (selected.series,), (selected,), None

    def immediate(child: ProgressiveState):
        events.append(("immediate", child.pfen))
        _mark_exact_immediate_miss(searcher, child)
        return None

    original_ladder = searcher._selected_root_single_reply_ladder_probe  # noqa: SLF001

    def ladder(child: ProgressiveState):
        events.append(("ladder", child.pfen))
        return original_ladder(child)

    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(searcher, "_root_child_immediate_mate", immediate)
    monkeypatch.setattr(
        searcher,
        "_selected_root_single_reply_ladder_probe",
        ladder,
    )

    score, pv, _alternatives, _proof = searcher._search_root(  # noqa: SLF001
        root, 1, ()
    )

    assert score == 300
    assert pv == (eligible,)
    assert events == [
        ("immediate", losing.final_state.pfen),
        ("ladder", losing.final_state.pfen),
        ("immediate", eligible.final_state.pfen),
        ("ladder", eligible.final_state.pfen),
    ]
    cached = searcher._selected_root_ladder_cache[  # noqa: SLF001
        searcher._tt_key(losing.final_state)  # noqa: SLF001
    ]
    assert cached.proof is not None
    assert cached.proof.attack.moves == BUCEPHALUS_ATTACK
    assert cached.proof.forced_reply.moves == BUCEPHALUS_FORCED_REPLY
    assert cached.proof.mate.moves == BUCEPHALUS_MATE
    assert searcher.stats.selected_root_ladder_candidate_vetoes == 1
    assert searcher.stats.selected_root_ladder_found == 1
    assert searcher.stats.selected_root_ladder_unknown == 1
    assert searcher.stats.selected_root_ladder_work == 1_628_052
    assert searcher.stats.generation_positions == 1_628_052
    assert losing.machine_notation in searcher._selected_pv_root_vetoes  # noqa: SLF001


def test_unknown_ladder_probe_keeps_selected_root_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    candidate = play_series(root, ("e2e4",))
    searcher = SeriesSearcher(
        SearchLimits(depth_series=1, max_series_per_node=32)
    )
    def exact_immediate_miss(state: ProgressiveState):
        _mark_exact_immediate_miss(searcher, state)
        return None

    monkeypatch.setattr(
        searcher,
        "_root_child_immediate_mate",
        exact_immediate_miss,
    )
    monkeypatch.setattr(
        searcher,
        "_selected_root_single_reply_ladder_probe",
        lambda _state: None,
    )
    monkeypatch.setattr(
        searcher,
        "_selected_root_single_reply_ladder_required",
        lambda _state: True,
    )
    monkeypatch.setattr(
        searcher,
        "_search_root_pass",
        lambda *_args: (51, (candidate,), (ScoredSeries(candidate, 51),), None),
    )

    score, pv, _alternatives, _proof = searcher._search_root(  # noqa: SLF001
        root, 1, ()
    )

    assert score == 51
    assert pv == (candidate,)
    assert searcher.stats.selected_root_ladder_candidate_vetoes == 0
    assert searcher._selected_pv_root_vetoes == set()  # noqa: SLF001


def test_recorded_black_s6_d0_fallback_is_rejected_at_final_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual work-limited 3dfd move cannot bypass the root-loop gate."""

    _require_native_ladder()
    root = state_from_pfen(BUCEPHALUS_3DFD_S6_PFEN)
    losing = play_series(root, BUCEPHALUS_3DFD_LOSING_ROOT)
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=512,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
        )
    )

    monkeypatch.setattr(
        searcher,
        "_root_current_series_mate",
        lambda *_args, **_kwargs: None,
    )

    def interrupted(*_args, **_kwargs):
        raise search_module._RootInterrupted(  # noqa: SLF001
            (),
            search_module._WorkLimit(),  # noqa: SLF001
            losing,
        )

    def exact_immediate_miss(child: ProgressiveState, **_kwargs):
        assert child.pfen == losing.final_state.pfen
        _mark_full_state_exact_immediate_miss(searcher, child)
        return SeriesMateStatus.EXHAUSTED

    monkeypatch.setattr(searcher, "_search_root", interrupted)
    monkeypatch.setattr(
        searcher,
        "_certify_final_fallback_reply_mate",
        exact_immediate_miss,
    )

    result = searcher.run(root)

    assert result.best_series is None
    assert result.principal_variation == ()
    assert result.alternatives == ()
    assert result.proof is None
    assert result.completed_depth == 0
    assert result.work_limit_reached
    assert not result.root_scores_complete
    assert searcher.stats.selected_root_ladder_found == 1
    assert searcher.stats.selected_root_ladder_candidate_vetoes == 1
    assert searcher.stats.selected_root_ladder_final_rejections == 1
    assert searcher.stats.selected_root_ladder_work == 628_052
    assert losing.machine_notation in searcher._selected_pv_root_vetoes  # noqa: SLF001


def test_final_safe_reselection_skips_recorded_ladder_and_publishes_no_rescue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An immediate-safe D0 rescue is still rejected by A/B/C inside 40M."""

    _require_native_ladder()
    root = state_from_pfen(BUCEPHALUS_3DFD_S6_PFEN)
    selected = play_series(root, BUCEPHALUS_3DFD_ELIGIBLE_ROOT)
    losing_rescue = play_series(root, BUCEPHALUS_3DFD_LOSING_ROOT)
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=512,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
        )
    )
    _mark_full_state_exact_immediate_miss(
        searcher,
        losing_rescue.final_state,
    )

    monkeypatch.setattr(
        searcher,
        "_root_current_series_mate",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        searcher,
        "_search_root",
        lambda *_args, **_kwargs: (
            400,
            (selected,),
            (
                ScoredSeries(selected, 400),
                ScoredSeries(losing_rescue, 300),
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        searcher,
        "_certify_final_fallback_reply_mate",
        lambda child, **_kwargs: (
            SeriesMateStatus.FOUND
            if child.pfen == selected.final_state.pfen
            else pytest.fail("unexpected immediate-mate probe")
        ),
    )

    result = searcher.run(root)

    assert result.best_series is None
    assert result.principal_variation == ()
    assert result.alternatives == ()
    assert result.proof is None
    assert result.completed_depth == 0
    assert not result.root_scores_complete
    assert searcher.stats.final_fallback_reply_mate_rejections == 1
    assert searcher.stats.final_fallback_safe_reselection_candidates == 1
    assert searcher.stats.final_fallback_safe_reselection_rescues == 0
    assert searcher.stats.final_fallback_safe_reselection_work == 628_052
    assert searcher.stats.selected_root_ladder_found == 1
    assert searcher.stats.selected_root_ladder_candidate_vetoes == 1
    assert searcher.stats.selected_root_ladder_final_rejections == 1
    assert losing_rescue.machine_notation in searcher._selected_pv_root_vetoes  # noqa: SLF001


def test_ladder_gate_skips_a_mover_with_only_a_king() -> None:
    state = ProgressiveState.from_fen(
        "8/8/8/8/1K6/8/1k6/8 w - - 0 1",
        25,
    )
    searcher = SeriesSearcher(SearchLimits())
    _mark_full_state_exact_immediate_miss(searcher, state)

    assert not searcher._selected_root_single_reply_ladder_required(state)  # noqa: SLF001
    assert searcher.stats.selected_root_ladder_probe_calls == 0


def test_exact_negative_cache_binds_clocks_and_progressive_ep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def exact_negative(state: ProgressiveState, **_kwargs):
        calls.append(state.pfen)
        return _exhausted_probe()

    monkeypatch.setattr(
        ladder_module,
        "find_native_single_reply_mate_ladder",
        exact_negative,
    )
    ordinary = ProgressiveState.initial()
    clock_board = chess.Board()
    clock_board.halfmove_clock = 19
    clock_board.fullmove_number = 23
    clocked = ProgressiveState(clock_board, 1)
    ep_board = "7k/8/8/1Pp5/8/8/8/K7 w - - 0 1"
    no_ep = ProgressiveState.from_fen(ep_board, 3)
    with_ep = ProgressiveState.from_fen(
        ep_board,
        3,
        ep_targets=(chess.C6,),
    )
    searcher = SeriesSearcher(SearchLimits())

    for state in (ordinary, clocked, no_ep, with_ep):
        first = searcher._selected_root_single_reply_ladder_probe(state)  # noqa: SLF001
        second = searcher._selected_root_single_reply_ladder_probe(  # noqa: SLF001
            state
        )
        assert first is second

    assert len(calls) == 4
    assert len(searcher._selected_root_ladder_cache) == 4  # noqa: SLF001
    assert searcher.stats.selected_root_ladder_probe_calls == 4
    assert searcher.stats.selected_root_ladder_cache_hits == 4
    assert searcher._root_child_mate_screen_cache == {}  # noqa: SLF001
    assert searcher._root_child_native_mate_cache_keys == set()  # noqa: SLF001
    assert searcher._root_child_proven_mate_keys == set()  # noqa: SLF001
    assert searcher._root_child_native_mate_exhausted_keys == set()  # noqa: SLF001


def test_ladder_work_uses_remaining_global_budget_and_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, float | int | None] = {}

    def bounded_probe(
        _state: ProgressiveState,
        *,
        max_work: int,
        time_limit_seconds: float | None,
    ) -> SingleReplyMateLadderProbe:
        observed["max_work"] = max_work
        observed["time_limit_seconds"] = time_limit_seconds
        return _exhausted_probe(work=max_work)

    monkeypatch.setattr(
        ladder_module,
        "find_native_single_reply_mate_ladder",
        bounded_probe,
    )
    searcher = SeriesSearcher(
        SearchLimits(max_generation_positions=250)
    )
    searcher.stats.generation_positions = 200
    searcher._deadline = time.perf_counter() + 10  # noqa: SLF001

    probe = searcher._selected_root_single_reply_ladder_probe(  # noqa: SLF001
        ProgressiveState.initial()
    )

    assert probe is not None
    assert observed["max_work"] == 50
    assert 0 < float(observed["time_limit_seconds"]) <= 10
    assert searcher.stats.selected_root_ladder_work == 50
    assert searcher.stats.generation_positions == 250


def test_work_limit_deadline_and_replay_exception_never_cache_or_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProgressiveState.initial()
    work_limited = SeriesSearcher(
        SearchLimits(max_generation_positions=1)
    )
    unknown = SingleReplyMateLadderProbe(
        SingleReplyMateLadderStatus.UNKNOWN,
        SeriesMateStatus.WORK_LIMIT,
        "work limit",
        SingleReplyMateLadderStats(attack_positions_visited=1),
    )
    monkeypatch.setattr(
        ladder_module,
        "find_native_single_reply_mate_ladder",
        lambda *_args, **_kwargs: unknown,
    )

    assert (  # noqa: SLF001
        work_limited._selected_root_single_reply_ladder_probe(state) is None
    )
    assert work_limited.stats.generation_positions == 1
    assert work_limited.stats.selected_root_ladder_unknown == 1
    assert work_limited._selected_root_ladder_cache == {}  # noqa: SLF001

    deadline = SeriesSearcher(SearchLimits())
    deadline._deadline = time.perf_counter()  # noqa: SLF001
    assert (  # noqa: SLF001
        deadline._selected_root_single_reply_ladder_probe(state) is None
    )
    assert deadline.stats.selected_root_ladder_probe_calls == 0
    assert deadline.stats.selected_root_ladder_unknown == 1
    assert deadline._selected_root_ladder_cache == {}  # noqa: SLF001

    replay_failure = SeriesSearcher(SearchLimits())
    monkeypatch.setattr(
        ladder_module,
        "find_native_single_reply_mate_ladder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("authoritative replay failed")
        ),
    )
    assert (  # noqa: SLF001
        replay_failure._selected_root_single_reply_ladder_probe(state) is None
    )
    assert replay_failure.stats.selected_root_ladder_unknown == 1
    assert replay_failure.stats.selected_root_ladder_work == 1_000_000
    assert replay_failure.stats.generation_positions == 1_000_000
    assert replay_failure._selected_root_ladder_cache == {}  # noqa: SLF001


def test_selected_ladder_gate_is_color_symmetric_and_cache_is_separate() -> None:
    _require_native_ladder()
    white = ProgressiveState.from_fen(TINY_FOUND_FEN, 1)
    black = ProgressiveState(chess.Board(TINY_FOUND_FEN).mirror(), 2)
    searcher = SeriesSearcher(SearchLimits(max_generation_positions=200_000))

    white_probe = searcher._selected_root_single_reply_ladder_probe(  # noqa: SLF001
        white
    )
    black_probe = searcher._selected_root_single_reply_ladder_probe(  # noqa: SLF001
        black
    )

    assert white_probe is not None and white_probe.proven_losing
    assert black_probe is not None and black_probe.proven_losing
    assert white_probe.proof is not None
    assert black_probe.proof is not None
    assert white_probe.proof.mate.final_state.board.turn == chess.BLACK
    assert black_probe.proof.mate.final_state.board.turn == chess.WHITE
    assert len(searcher._selected_root_ladder_cache) == 2  # noqa: SLF001
    assert searcher._root_child_mate_screen_cache == {}  # noqa: SLF001
    assert searcher._root_child_native_mate_cache_keys == set()  # noqa: SLF001


@pytest.mark.parametrize("width_complete", (True, False))
def test_all_ladder_vetoes_require_exact_frontier_for_least_bad_resistance(
    monkeypatch: pytest.MonkeyPatch,
    width_complete: bool,
) -> None:
    root = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/r7/K7 w - - 0 1",
        1,
    )
    legal = tuple(generate_series(root, merge_transpositions=False))
    assert {item.machine_notation for item in legal} == {"a1a2", "a1b1"}
    scored = {
        item.machine_notation: ScoredSeries(
            item,
            80 if item.machine_notation == "a1b1" else 20,
        )
        for item in legal
    }
    searcher = SeriesSearcher(
        SearchLimits(depth_series=1, max_series_per_node=2)
    )
    widening_exclusions: list[frozenset[str]] = []
    for item in legal:
        _mark_exact_immediate_miss(searcher, item.final_state)

    def root_pass(
        _root,
        _depth,
        _prefix,
        _overrides,
        _horizon_overrides,
        exclusions,
        _frontier,
    ):
        remaining = tuple(
            item for notation, item in scored.items() if notation not in exclusions
        )
        if not remaining:
            return 0, (), (), None
        best = max(remaining, key=lambda item: item.score)
        return best.score, (best.series,), remaining, None

    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: False,
    )
    monkeypatch.setattr(
        searcher,
        "_selected_root_single_reply_ladder_probe",
        lambda _state: SimpleNamespace(proven_losing=True),
    )
    monkeypatch.setattr(
        searcher,
        "_selected_root_single_reply_ladder_required",
        lambda _state: True,
    )

    def widened_frontier(
        _state: ProgressiveState,
        _required_prefix: tuple[str, ...],
        exclusions: frozenset[str],
    ):
        widening_exclusions.append(exclusions)
        return search_module._GeneratedSeriesList(  # noqa: SLF001
            [], width_complete=width_complete
        )

    monkeypatch.setattr(
        searcher,
        "_selected_pv_horizon_widened_frontier",
        widened_frontier,
    )
    monkeypatch.setattr(
        searcher,
        "_certify_selected_pv_horizon",
        lambda *_args: _not_applicable(),
    )

    result = searcher.run(root)
    assert widening_exclusions == [frozenset()]

    if not width_complete:
        assert result.best_series is None
        assert result.principal_variation == ()
        assert result.alternatives == ()
        assert result.proof is None
        assert result.completed_depth == 0
        assert not result.root_scores_complete
        assert searcher.stats.selected_root_ladder_candidate_vetoes == 2
        assert searcher.stats.selected_root_ladder_all_vetoed_fallbacks == 0
        assert searcher._selected_root_ladder_emergency_fallback is None  # noqa: SLF001
        return

    assert result.best_series is not None
    assert result.best_series.machine_notation == "a1b1"
    assert result.principal_variation == (result.best_series,)
    assert result.alternatives == ()
    assert result.proof is None
    assert result.completed_depth == 0
    assert not result.exact_width
    assert not result.root_scores_complete
    assert not result.timed_out
    assert not result.work_limit_reached
    assert searcher.stats.selected_root_ladder_candidate_vetoes == 2
    assert searcher.stats.selected_root_ladder_all_vetoed_fallbacks == 1
    assert searcher._root_scores_complete is False  # noqa: SLF001
    assert "a1b1" not in searcher._selected_pv_root_vetoes  # noqa: SLF001
    assert searcher._selected_root_ladder_emergency_fallback == "a1b1"  # noqa: SLF001
