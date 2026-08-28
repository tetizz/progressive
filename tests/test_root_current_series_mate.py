from __future__ import annotations

import time

import pytest

import scottish_progressive.evaluation as evaluation
import scottish_progressive.series_mate as series_mate
from scottish_progressive.model import Outcome, ProgressiveState, SeriesResult
from scottish_progressive.rules import play_series
from scottish_progressive.search import (
    ScoredSeries,
    SearchLimits,
    SeriesSearcher,
    analyze,
)
from scottish_progressive.series_mate import SeriesMateProbe, SeriesMateStatus
from scottish_progressive.teacher_value_features import state_from_pfen


BUCEPHALUS_MISSED_S5_MATE_PFEN = (
    "rn1q1bnr/2pppk1p/b7/pp3pB1/3P4/8/PPPNPPPP/R1Q1KBNR "
    "w KQ - 1 7 | series=5 quiet=0 progressive_ep=- "
    "rules=scottish-modern-common-v1 quiet_draw=manual-proof-required"
)
HISTORICAL_LOSING_FALLBACK = "d4d5/d5d6/d6e7/e7d8q/a2a4"


def _require_source_matched_native() -> None:
    native = evaluation._native_eval
    if native is None or not hasattr(native, "prepare_complete_series"):
        pytest.skip("source-matched native search is not built")
    assert native.SOURCE_IDENTITY == evaluation._native_source_identity()
    mate = series_mate._native_mate
    if mate is None or not hasattr(mate, "find_series_mate"):
        pytest.skip("source-matched native mate search is not built")
    assert mate.SOURCE_IDENTITY == series_mate._native_mate_source_identity()


def test_bucephalus_loss_position_plays_the_available_root_mate() -> None:
    """A proven mate-now must outrank every ordinary or D0 fallback."""

    _require_source_matched_native()
    state = state_from_pfen(BUCEPHALUS_MISSED_S5_MATE_PFEN)
    result = analyze(
        state,
        SearchLimits(
            depth_series=8,
            max_series_per_node=32,
            time_limit_seconds=30.0,
            max_generation_positions=4_000_000_000,
            collect_all_root_scores=False,
            native_threads=16,
        ),
    )

    assert result.best_series is not None
    assert result.best_series.machine_notation != HISTORICAL_LOSING_FALLBACK
    assert result.best_series.outcome is Outcome.CHECKMATE
    assert result.best_series.ended_by_check
    assert result.proof == "white"
    assert result.score == 999_999
    assert result.principal_variation == (result.best_series,)
    assert result.completed_depth == 8
    assert result.stats.root_current_series_mate_probes == 1
    assert result.stats.root_current_series_mate_found == 1
    assert result.stats.root_current_series_mate_work == 505
    assert result.stats.root_safety_screen_positions == 0


def test_root_mate_priority_is_color_symmetric() -> None:
    """Black receives the same exact mate-now priority as White."""

    _require_source_matched_native()
    state = ProgressiveState.from_fen(
        "8/8/8/8/8/5kq1/8/7K b - - 0 1",
        6,
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=3,
            max_series_per_node=32,
            max_generation_positions=1_000_000,
            collect_all_root_scores=False,
        ),
    )

    assert result.best_series is not None
    assert result.best_series.outcome is Outcome.CHECKMATE
    assert result.score == -999_999
    assert result.proof == "black"
    assert result.completed_depth == 3
    assert result.stats.root_current_series_mate_found == 1


@pytest.mark.parametrize(
    ("status", "counter"),
    (
        (SeriesMateStatus.EXHAUSTED, "root_current_series_mate_exhausted"),
        (SeriesMateStatus.WORK_LIMIT, "root_current_series_mate_unknown"),
        (SeriesMateStatus.DEADLINE, "root_current_series_mate_unknown"),
        (SeriesMateStatus.UNSUPPORTED, "root_current_series_mate_unknown"),
    ),
)
def test_nonproof_probe_status_never_changes_selection_policy(
    monkeypatch: pytest.MonkeyPatch,
    status: SeriesMateStatus,
    counter: str,
) -> None:
    state = state_from_pfen(BUCEPHALUS_MISSED_S5_MATE_PFEN)
    observed: dict[str, object] = {}

    def injected_probe(*_args: object, **kwargs: object) -> SeriesMateProbe:
        observed.update(kwargs)
        return SeriesMateProbe(
            status,
            "injected nonproof",
            positions_visited=7,
            moves_generated=6,
        )

    monkeypatch.setattr(series_mate, "find_native_series_mate", injected_probe)
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=640_000,
            collect_all_root_scores=False,
        )
    )

    assert searcher._root_current_series_mate(state, required_prefix=()) is None
    assert observed["max_work"] == 10_000
    assert observed["time_limit_seconds"] == 1.0
    assert searcher.stats.root_current_series_mate_probes == 1
    assert getattr(searcher.stats, counter) == 1
    assert searcher.stats.root_current_series_mate_work == 13
    assert searcher.stats.generation_positions == 13
    assert searcher.stats.root_safety_screen_positions == 0


def test_proactive_probe_skips_tractable_and_contract_sensitive_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_from_pfen(BUCEPHALUS_MISSED_S5_MATE_PFEN)

    def unexpected_probe(*_args: object, **_kwargs: object) -> SeriesMateProbe:
        raise AssertionError("proactive mate solver should have been skipped")

    monkeypatch.setattr(series_mate, "find_native_series_mate", unexpected_probe)

    full_root = SeriesSearcher(
        SearchLimits(collect_all_root_scores=True),
    )
    assert full_root._root_current_series_mate(state, required_prefix=()) is None

    prefixed = SeriesSearcher(
        SearchLimits(collect_all_root_scores=False),
    )
    assert prefixed._root_current_series_mate(
        state,
        required_prefix=("d4d5",),
    ) is None

    early = SeriesSearcher(
        SearchLimits(collect_all_root_scores=False),
    )
    assert early._root_current_series_mate(
        ProgressiveState.initial(),
        required_prefix=(),
    ) is None

    low_work = SeriesSearcher(
        SearchLimits(
            collect_all_root_scores=False,
            max_generation_positions=250_000,
        ),
    )
    assert low_work._root_current_series_mate(
        state,
        required_prefix=(),
    ) is None


def test_probe_deadline_reserves_time_for_ordinary_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_from_pfen(BUCEPHALUS_MISSED_S5_MATE_PFEN)
    fallback = play_series(state, tuple(HISTORICAL_LOSING_FALLBACK.split("/")))
    observed: dict[str, float] = {}
    ordinary_calls = 0

    def deadline_probe(*_args: object, **kwargs: object) -> SeriesMateProbe:
        seconds = float(kwargs["time_limit_seconds"])
        observed["probe_seconds"] = seconds
        time.sleep(seconds)
        return SeriesMateProbe(SeriesMateStatus.DEADLINE, "probe slice expired")

    def immediate_ordinary_search(
        _self: SeriesSearcher,
        _state: ProgressiveState,
        _depth: int,
        _required_prefix: tuple[str, ...],
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[ScoredSeries, ...], None]:
        nonlocal ordinary_calls
        ordinary_calls += 1
        scored = ScoredSeries(fallback, 0)
        return 0, (fallback,), (scored,), None

    monkeypatch.setattr(series_mate, "find_native_series_mate", deadline_probe)
    monkeypatch.setattr(SeriesSearcher, "_search_root", immediate_ordinary_search)
    result = analyze(
        state,
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            time_limit_seconds=0.2,
            max_generation_positions=640_000,
            collect_all_root_scores=False,
        ),
    )

    assert 0 < observed["probe_seconds"] <= 0.03
    assert ordinary_calls == 1
    assert result.best_series == fallback
    assert result.completed_depth == 1
    assert not result.timed_out
