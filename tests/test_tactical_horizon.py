from __future__ import annotations

from dataclasses import asdict

import chess
import pytest

from scottish_progressive.evaluation import evaluate
from scottish_progressive.model import ProgressiveState
from scottish_progressive.native_subtree import (
    SUBTREE_STAT_FIELDS,
    NativeSubtreeSession,
    native_subtree_available,
)
from scottish_progressive.profiles import EvaluationWeights, baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.search import (
    MATE_SCORE,
    UNKNOWN_PROOF_BOUNDS,
    SearchLimits,
    SearchResult,
    SeriesSearcher,
    analyze,
)


TACTICAL_HORIZON_FEN = "r5k1/6B1/8/8/7Q/8/8/1K6 w - - 0 1"
TACTICAL_HORIZON_SERIES = 1
DELAYED_QUEEN_CAPTURE_FEN = "k5n1/8/8/7p/8/8/8/3QK3 w - - 0 1"
FAKE_PROMOTION_CORRIDOR_FEN = "3kr3/6n1/P7/7P/8/8/8/1K6 w - - 0 1"
CERTIFIED_CHECKED_MIDDLEGAME_FEN = (
    "rnbq1bnr/pppp1kpp/4p3/4B2Q/2B5/1P5N/P1PP1KPP/RN5R b - - 4 7"
)
CERTIFIED_CHECKED_MIDDLEGAME_SERIES = 6
CERTIFIED_CHECKED_MIDDLEGAME_REPLY = (
    "f7e7",
    "d8e8",
    "e8h5",
    "e7e8",
    "h5e5",
    "e5a1",
)


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


def test_next_series_capture_reach_prices_two_move_tactical_threats() -> None:
    state = ProgressiveState.from_fen(
        TACTICAL_HORIZON_FEN,
        TACTICAL_HORIZON_SERIES,
    )

    checking_queen = play_series(state, ("h4h8",)).final_state
    exposed_queen = play_series(state, ("h4e7",)).final_state
    exposed_bishop = play_series(state, ("g7d4",)).final_state

    # Black can first evade/maneuver and then capture each piece during its
    # two-move series.  The boundary evaluator must price the best reachable
    # capture, not only captures legal on Black's first micro-move.
    assert evaluate(checking_queen).immediate_vulnerability == -975
    assert evaluate(exposed_queen).immediate_vulnerability == -975
    assert evaluate(exposed_bishop).immediate_vulnerability == -340


def test_next_series_capture_reach_closes_the_tactical_horizon_gap() -> None:
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

    # The shallow evaluator now sees Black's two-move capture route and rejects
    # the flashy Qh8+ blunder. The exact replacement is deliberately not frozen:
    # a stronger future evaluator may improve on this candidate too.
    assert shallow.completed_depth == 1
    assert not shallow.timed_out
    assert not shallow.work_limit_reached
    assert shallow.proof is None
    assert shallow.best_series is not None
    assert shallow.best_series.machine_notation != "h4h8"

    assert deep.completed_depth == 2
    assert not deep.exact_width
    assert not deep.timed_out
    assert not deep.work_limit_reached
    assert deep.proof is None
    assert deep.best_series is not None
    assert deep.best_series.machine_notation != "h4h8"

    assert forced_check.completed_depth == 2
    assert not forced_check.timed_out
    assert not forced_check.work_limit_reached
    assert forced_check.proof is None
    assert forced_check.required_prefix == ("h4h8",)
    assert forced_check.best_series is not None
    assert forced_check.best_series.machine_notation == "h4h8"
    assert tuple(
        item.machine_notation for item in forced_check.principal_variation
    ) == ("h4h8", "g8f7/a8h8")
    assert forced_check.score < shallow.score


def test_delayed_queen_capture_is_visible_before_the_search_horizon() -> None:
    state = ProgressiveState.from_fen(DELAYED_QUEEN_CAPTURE_FEN, 1)
    exposed = play_series(state, ("d1h5",)).final_state

    assert evaluate(exposed).immediate_vulnerability == -975

    shallow = _analyze_anchor(state, 1)
    forced = _analyze_anchor(state, 2, required_prefix=("d1h5",))

    assert shallow.exact_width
    assert shallow.best_series is not None
    assert shallow.best_series.machine_notation != "d1h5"
    assert forced.exact_width
    assert tuple(
        item.machine_notation for item in forced.principal_variation
    ) == ("d1h5", "g8f6/f6h5")
    assert forced.score < shallow.score


def test_capturable_pawn_does_not_keep_a_fake_promotion_corridor() -> None:
    state = ProgressiveState.from_fen(FAKE_PROMOTION_CORRIDOR_FEN, 1)
    exposed = play_series(state, ("a6a7",)).final_state
    breakdown = evaluate(exposed)

    # Black has Re7/Rxa7 in its two-move series. Keep the static corridor term:
    # another Black route can capture h5 instead, so erasing both passers would
    # conflate mutually exclusive continuations. The selective leaf extension
    # must resolve the route coupling by searching one complete Black series.
    assert breakdown.immediate_vulnerability == -100
    assert breakdown.promotion_corridors == 760
    assert breakdown.tactical_unstable

    shallow = _analyze_anchor(state, 1)
    assert not shallow.work_limit_reached
    assert shallow.best_series is not None
    assert shallow.best_series.machine_notation != "a6a7"
    assert shallow.proof is None
    assert shallow.forced is None
    assert len(shallow.principal_variation) == 1
    assert shallow.stats.tactical_leaf_extensions > 0
    assert shallow.stats.branch_caps > 0


def test_endgame_extension_uses_the_opponents_real_next_series_budget() -> None:
    state = ProgressiveState.from_fen(
        "3kr3/6n1/8/P6P/8/8/8/1K6 b - - 0 1",
        2,
    )

    breakdown = evaluate(state)
    assert breakdown.capture_reach_complete
    assert breakdown.tactical_unstable

    later = ProgressiveState.from_fen(
        "3kr3/6n1/8/P6P/8/8/8/1K6 b - - 0 1",
        4,
    )
    assert evaluate(later).tactical_unstable


def test_tactical_extension_preserves_quiet_draw_adjudication() -> None:
    state = ProgressiveState.from_fen(
        FAKE_PROMOTION_CORRIDOR_FEN,
        1,
        quiet_series=8,
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=True,
        ),
        required_prefix=("b1b2",),
    )

    assert result.completed_depth == 0
    assert result.adjudication_status == "manual-proof-required"
    assert result.stats.tactical_leaf_extensions == 1
    assert result.stats.quiet_adjudication_positions > 0


def test_tactical_extension_is_one_token_without_tt_or_hidden_pv() -> None:
    state = ProgressiveState.from_fen(FAKE_PROMOTION_CORRIDOR_FEN, 1)
    leaf = play_series(state, ("a6a7",)).final_state
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=250_000,
        )
    )

    _, pv, proof_bounds = searcher._minimax(
        leaf,
        0,
        -MATE_SCORE * 2,
        MATE_SCORE * 2,
        1,
    )

    assert pv == ()
    assert proof_bounds == UNKNOWN_PROOF_BOUNDS
    assert searcher.stats.tactical_leaf_extensions == 1
    assert searcher._tt == {}


def test_checked_middlegame_leaf_searches_one_real_reply_series() -> None:
    """The certified b3 D5 horizon must see Black evade check and win Ra1."""

    leaf = ProgressiveState.from_fen(
        CERTIFIED_CHECKED_MIDDLEGAME_FEN,
        CERTIFIED_CHECKED_MIDDLEGAME_SERIES,
    )
    breakdown = evaluate(leaf)
    assert breakdown.total == 522
    assert breakdown.boundary_check == 170
    assert breakdown.total - breakdown.boundary_check == 352
    assert breakdown.tactical_unstable
    reply = play_series(leaf, CERTIFIED_CHECKED_MIDDLEGAME_REPLY)
    assert reply.machine_notation == "/".join(CERTIFIED_CHECKED_MIDDLEGAME_REPLY)
    assert evaluate(reply.final_state).total == -2641

    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=250_000,
        )
    )
    assert searcher._native_subtree_session is None
    score, pv, proof_bounds = searcher._minimax(
        leaf,
        0,
        -MATE_SCORE * 2,
        MATE_SCORE * 2,
        1,
    )

    assert score == -2641
    assert pv == ()
    assert proof_bounds == UNKNOWN_PROOF_BOUNDS
    assert searcher.stats.tactical_leaf_extensions == 1
    assert 0 < searcher.stats.work_positions <= 3_000
    assert searcher._tt == {}


def test_checked_middlegame_trigger_is_color_symmetric() -> None:
    leaf = ProgressiveState.from_fen(
        CERTIFIED_CHECKED_MIDDLEGAME_FEN,
        CERTIFIED_CHECKED_MIDDLEGAME_SERIES,
    )
    mirrored = ProgressiveState(leaf.board.mirror(), series_number=7)

    original = evaluate(leaf)
    opposite = evaluate(mirrored)
    assert opposite.total == -original.total == -522
    assert opposite.material == -original.material == 100
    assert opposite.boundary_check == -original.boundary_check == -170
    assert opposite.total - opposite.boundary_check == -352
    assert original.tactical_unstable
    assert opposite.tactical_unstable

    scaled_material = evaluate(leaf, EvaluationWeights(material=73))
    assert scaled_material.material == -73
    assert scaled_material.total == 549
    assert scaled_material.tactical_unstable


def test_checked_middlegame_extension_matches_native_depth_zero() -> None:
    if not native_subtree_available():
        pytest.skip("source-matched native subtree API is unavailable")
    leaf = ProgressiveState.from_fen(
        CERTIFIED_CHECKED_MIDDLEGAME_FEN,
        CERTIFIED_CHECKED_MIDDLEGAME_SERIES,
    )
    session = NativeSubtreeSession(
        max_series_per_node=32,
        max_work=250_000,
        requested_depth=1,
        mate_score=MATE_SCORE,
        cache_capacity=16_384,
        external_cache_weight=0,
        native_threads=1,
        root_tactical_protection=False,
        profile=baseline_profile(),
    )
    native = session.search(
        leaf,
        depth=0,
        alpha=-MATE_SCORE * 2,
        beta=MATE_SCORE * 2,
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    stats = dict(zip(SUBTREE_STAT_FIELDS, native.stats, strict=True))

    assert native.status == 0
    assert native.score == -2641
    assert native.principal_variation == ()
    assert native.proof_bounds == UNKNOWN_PROOF_BOUNDS
    assert native.selective
    assert stats["tactical_leaf_extensions"] == 1
    assert 0 < stats["generation_positions"] <= 3_000


def test_tactical_extension_native_and_python_subtrees_match(monkeypatch) -> None:
    state = ProgressiveState.from_fen(FAKE_PROMOTION_CORRIDOR_FEN, 1)
    limits = SearchLimits(
        depth_series=1,
        max_series_per_node=32,
        max_generation_positions=250_000,
        collect_all_root_scores=True,
    )
    native_searcher = SeriesSearcher(limits)
    native = native_searcher.run(state)
    assert native_searcher._native_subtree_session is not None
    monkeypatch.setattr(
        SeriesSearcher,
        "_start_native_subtree",
        lambda self, root: None,
    )
    python = analyze(state, limits)

    assert (
        native.score,
        native.best_series,
        native.principal_variation,
        native.proof,
        native.forced,
        native.exact_width,
        asdict(native.stats),
    ) == (
        python.score,
        python.best_series,
        python.principal_variation,
        python.proof,
        python.forced,
        python.exact_width,
        asdict(python.stats),
    )
