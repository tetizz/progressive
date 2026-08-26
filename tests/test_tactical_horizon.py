from __future__ import annotations

from scottish_progressive.model import ProgressiveState
from scottish_progressive.search import SearchLimits, SearchResult, analyze


TACTICAL_HORIZON_FEN = "r5k1/6B1/8/8/7Q/8/8/1K6 w - - 0 1"
TACTICAL_HORIZON_SERIES = 1


def _analyze_anchor(
    state: ProgressiveState,
    depth: int,
    *,
    required_prefix: tuple[str, ...] = (),
) -> SearchResult:
    return analyze(
        state,
        SearchLimits(
            depth_series=depth,
            max_series_per_node=64,
            max_generation_positions=3_000_000,
            collect_all_root_scores=False,
        ),
        required_prefix=required_prefix,
    )


def test_two_series_search_exposes_the_current_tactical_horizon_gap() -> None:
    state = ProgressiveState.from_fen(
        TACTICAL_HORIZON_FEN,
        TACTICAL_HORIZON_SERIES,
    )

    shallow = _analyze_anchor(state, 1)
    deep = _analyze_anchor(state, 2)
    forced_check = _analyze_anchor(
        state,
        2,
        required_prefix=("h4h8",),
    )

    # Characterization only: Qh8+ is the current shallow horizon choice, not
    # the desired contract. A bounded tactical extension should replace these
    # shallow literals while retaining the deeper refutation as its oracle.
    assert shallow.completed_depth == 1
    assert shallow.exact_width
    assert not shallow.timed_out
    assert not shallow.work_limit_reached
    assert shallow.proof is None
    assert shallow.score == 1_104
    assert shallow.best_series is not None
    assert shallow.best_series.machine_notation == "h4h8"
    assert tuple(
        item.machine_notation for item in shallow.principal_variation
    ) == ("h4h8",)

    assert deep.completed_depth == 2
    assert not deep.exact_width
    assert not deep.timed_out
    assert not deep.work_limit_reached
    assert deep.proof is None
    assert deep.score == 518
    assert deep.best_series is not None
    assert deep.best_series.machine_notation == "g7d4"
    assert tuple(
        item.machine_notation for item in deep.principal_variation
    ) == ("g7d4", "g8f7/a8b8")

    assert forced_check.completed_depth == 2
    assert forced_check.exact_width
    assert not forced_check.timed_out
    assert not forced_check.work_limit_reached
    assert forced_check.proof is None
    assert forced_check.required_prefix == ("h4h8",)
    assert forced_check.score == 292
    assert forced_check.best_series is not None
    assert forced_check.best_series.machine_notation == "h4h8"
    assert tuple(
        item.machine_notation for item in forced_check.principal_variation
    ) == ("h4h8", "g8f7/a8h8")
