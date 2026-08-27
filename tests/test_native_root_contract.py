from __future__ import annotations

from dataclasses import asdict, replace

import chess
import pytest

import scottish_progressive.evaluation as evaluation
import scottish_progressive.search as search_module
from scottish_progressive.model import Outcome, ProgressiveState
from scottish_progressive.native_subtree import (
    SUBTREE_STAT_FIELDS,
    NativeDeepTeacherValueModel,
    NativeRootEnumerationResult,
    NativeSubtreeBound,
    NativeSubtreeResult,
    NativeSubtreeSession,
    native_subtree_available,
)
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.search import (
    MATE_SCORE,
    SearchLimits,
    SeriesSearcher,
    analyze,
)


def _require_contract() -> None:
    if not native_subtree_available():
        pytest.skip("source-matched native retained-root contract is unavailable")


def _session(
    *,
    width: int = 8,
    depth: int = 3,
    max_work: int = 2_000_000,
    cache_capacity: int = 16_384,
    tt_capacity: int = 262_144,
    eval_capacity: int = 262_144,
    root_tactical_protection: bool = False,
    native_threads: int = 1,
    deep_teacher_value_model: NativeDeepTeacherValueModel | None = None,
) -> NativeSubtreeSession:
    _require_contract()
    return NativeSubtreeSession(
        max_series_per_node=width,
        max_work=max_work,
        requested_depth=depth,
        mate_score=MATE_SCORE,
        cache_capacity=cache_capacity,
        external_cache_weight=0,
        native_threads=native_threads,
        root_tactical_protection=root_tactical_protection,
        profile=baseline_profile(),
        root_contract_tt_capacity=tt_capacity,
        root_contract_eval_capacity=eval_capacity,
        deep_teacher_value_model=deep_teacher_value_model,
    )


def _deep_teacher_model(
    coefficients: tuple[int, ...],
) -> NativeDeepTeacherValueModel:
    return NativeDeepTeacherValueModel(
        base_profile_id=baseline_profile().profile_id,
        variant_id="spc-dtv-variant-native-contract-test",
        model_id="spc-dtv-native-contract-test",
        model_sha256="a" * 64,
        native_source_identity=evaluation._native_source_identity(),  # noqa: SLF001
        coefficients=coefficients,
    )


def _series_signature(item: object) -> tuple[object, ...]:
    return (
        item.machine_notation,
        item.final_state.pfen,
        item.final_state.board.promoted,
        item.ended_by_check,
        item.outcome,
        item.unused_moves,
        item.transposition_count,
    )


def _analysis_signature(result: object) -> tuple[object, ...]:
    return (
        result.score,
        _series_signature(result.best_series) if result.best_series else None,
        tuple(_series_signature(item) for item in result.principal_variation),
        tuple(
            (
                _series_signature(item.series),
                item.score,
                tuple(
                    _series_signature(series)
                    for series in item.principal_variation
                ),
                item.proof_bounds,
                item.proof,
            )
            for item in result.alternatives
        ),
        result.requested_depth,
        result.completed_depth,
        result.exact_width,
        result.timed_out,
        result.work_limit_reached,
        result.root_scores_complete,
        result.proof,
        result.forced,
        result.adjudication_status,
        result.classification,
        result.confidence,
        asdict(result.stats),
    )


def _candidate_search_signature(result: object) -> tuple[object, ...]:
    return (
        result.status,
        result.bound,
        result.score,
        result.terminal,
        _series_signature(result.root_series) if result.root_series else None,
        tuple(
            _series_signature(item) for item in result.child_principal_variation
        ),
        result.proof_bounds,
    )


@pytest.mark.parametrize(
    ("fen", "expected_score"),
    [
        ("7k/8/8/8/8/8/P7/7K w - - 0 1", 1),
        ("7k/p7/8/8/8/8/8/7K w - - 0 1", -1),
    ],
)
def test_native_deep_teacher_rounds_half_away_and_accounts_exact_work(
    fen: str,
    expected_score: int,
) -> None:
    state = ProgressiveState.from_fen(fen, 1)
    # The material feature is +/-100. A 5,000,000 coefficient therefore
    # produces exactly +/-0.5 after the frozen 1e9 scale. The boundary feature
    # is zero in both fixtures and carries the required normalized coefficient.
    model = _deep_teacher_model(
        (5_000_000, 0, 0, 0, 0, 0, 1_000_000_000)
    )
    baseline = _session(width=4, depth=1).search(
        state,
        depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )
    modeled_session = _session(
        width=4,
        depth=1,
        deep_teacher_value_model=model,
    )
    modeled = modeled_session.search(
        state,
        depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )
    baseline_stats = dict(zip(SUBTREE_STAT_FIELDS, baseline.stats, strict=True))
    modeled_stats = dict(zip(SUBTREE_STAT_FIELDS, modeled.stats, strict=True))

    assert modeled.status == 0
    assert modeled.score == expected_score
    assert baseline_stats["overlay_evaluations"] == 0
    assert modeled_stats["overlay_evaluations"] == 1
    assert modeled_stats["overlay_reach_positions"] > 0
    assert modeled_stats["overlay_direct_move_variants"] == 0
    assert modeled_stats["overlay_two_move_variants"] == 0
    assert modeled_stats["evaluation_reach_positions"] == (
        baseline_stats["evaluation_reach_positions"]
        + modeled_stats["overlay_reach_positions"]
    )
    assert modeled_stats["generation_positions"] == (
        baseline_stats["generation_positions"]
        + modeled_stats["overlay_reach_positions"]
    )

    cached = modeled_session.search(
        state,
        depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert cached.score == expected_score
    cached_stats = dict(zip(SUBTREE_STAT_FIELDS, cached.stats, strict=True))
    assert cached_stats["nodes"] == modeled_stats["nodes"] + 1
    for field in (
        "leaf_evaluations",
        "overlay_evaluations",
        "overlay_reach_positions",
        "generation_positions",
    ):
        assert cached_stats[field] == modeled_stats[field]


def test_native_deep_teacher_work_exhaustion_fails_closed() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/P7/7K w - - 0 1",
        1,
    )
    baseline = _session(width=4, depth=1).search(
        state,
        depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )
    baseline_stats = dict(zip(SUBTREE_STAT_FIELDS, baseline.stats, strict=True))
    exact_base_work = baseline_stats["generation_positions"]
    stopped = _session(
        width=4,
        depth=1,
        max_work=exact_base_work,
        deep_teacher_value_model=_deep_teacher_model(
            (1_000_000_000, 0, 0, 0, 0, 0, 0)
        ),
    ).search(
        state,
        depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )
    stopped_stats = dict(zip(SUBTREE_STAT_FIELDS, stopped.stats, strict=True))

    assert stopped.status == 1
    assert stopped.selective
    assert stopped.evaluation_work_limit_reached
    assert stopped_stats["overlay_evaluations"] == 1
    assert stopped_stats["generation_positions"] == exact_base_work
    assert stopped_stats["generation_work_limit_hits"] == 1


def test_native_deep_teacher_charges_direct_and_two_move_variants() -> None:
    state = ProgressiveState.from_fen(
        "r1bq1rk1/ppp2ppp/2np1n2/2b1p3/2B1P3/2NP1N2/"
        "PPP2PPP/R1BQ1RK1 w - - 0 8",
        5,
    )
    baseline = _session(width=4, depth=1).search(
        state,
        depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )
    modeled = _session(
        width=4,
        depth=1,
        deep_teacher_value_model=_deep_teacher_model(
            (1_000_000_000,) + (0,) * 46
        ),
    ).search(
        state,
        depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )
    baseline_stats = dict(zip(SUBTREE_STAT_FIELDS, baseline.stats, strict=True))
    modeled_stats = dict(zip(SUBTREE_STAT_FIELDS, modeled.stats, strict=True))
    overlay_work = (
        modeled_stats["overlay_reach_positions"]
        + modeled_stats["overlay_direct_move_variants"]
        + modeled_stats["overlay_two_move_variants"]
    )

    assert modeled.status == 0
    assert modeled_stats["overlay_evaluations"] == 1
    assert modeled_stats["overlay_direct_move_variants"] > 0
    assert modeled_stats["overlay_two_move_variants"] > 0
    assert modeled_stats["generation_positions"] == (
        baseline_stats["generation_positions"] + overlay_work
    )


def test_native_root_deep_teacher_never_overspends_call_credit() -> None:
    root = ProgressiveState.from_fen(
        "rnb1kbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        1,
    )
    model = _deep_teacher_model(
        (1_000_000_000, 0, 0, 0, 0, 0, 0)
    )

    def prepared() -> tuple[NativeSubtreeSession, NativeRootEnumerationResult]:
        session = _session(
            width=4,
            depth=1,
            deep_teacher_value_model=model,
        )
        manifest = session.enumerate_root(
            root,
            preferred_series=None,
            external_work=0,
            remaining_nanoseconds=None,
            call_work_credit=500_000,
        )
        assert manifest.status == 0
        return session, manifest

    measured_session, measured_manifest = prepared()
    measured = measured_session.search_root_candidate(
        enumeration_identity=measured_manifest.enumeration_identity,
        candidate_identity=measured_manifest.candidates[0].candidate_identity,
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
        call_work_credit=500_000,
    )
    assert measured.status == 0
    assert measured.work.call_native_work > 1

    short_session, short_manifest = prepared()
    credit = measured.work.call_native_work - 1
    stopped = short_session.search_root_candidate(
        enumeration_identity=short_manifest.enumeration_identity,
        candidate_identity=short_manifest.candidates[0].candidate_identity,
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
        call_work_credit=credit,
    )
    assert stopped.status == 1
    assert stopped.work.call_native_work <= credit
    assert stopped.work.call_stats[
        SUBTREE_STAT_FIELDS.index("overlay_evaluations")
    ] == 0


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(ProgressiveState.initial(), id="S1"),
        pytest.param(
            ProgressiveState.from_fen(
                "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/"
                "RNBQK2R b KQkq - 1 3",
                4,
            ),
            id="S4",
        ),
        pytest.param(
            ProgressiveState.from_fen(
                "rnk3nr/pp3ppp/8/8/8/1Pp1P3/P1PP1PPP/"
                "R1b1K1NR w K - 0 13",
                7,
            ),
            id="S7",
        ),
    ],
)
def test_s1_s4_s7_full_product_signature_and_stats_parity(
    state: ProgressiveState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = SearchLimits(
        depth_series=4,
        max_series_per_node=4,
        max_generation_positions=2_000_000,
        collect_all_root_scores=False,
        native_threads=1,
    )
    monkeypatch.setattr(search_module, "NATIVE_SUBTREE_ENABLED", False)
    expected = analyze(state, limits, baseline_profile())
    monkeypatch.setattr(search_module, "NATIVE_SUBTREE_ENABLED", True)
    actual = analyze(state, limits, baseline_profile())
    assert _analysis_signature(actual) == _analysis_signature(expected)


def test_legacy_subtree_stats_do_not_regress_after_path_count_saturation() -> None:
    """A high-series two-king boundary must keep cumulative stats monotonic."""

    _require_contract()
    state = ProgressiveState.from_fen(
        "8/8/8/1K6/8/8/1k6/8 b - - 112 109",
        22,
        quiet_series=6,
    )

    result = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        baseline_profile(),
    )

    assert result.best_series is not None
    assert result.completed_depth == 2
    # The Python-owned root frontier is added to a native descendant counter
    # that has reached UINT64_MAX. Seeing at least that lower bound proves the
    # native cumulative value clamped instead of wrapping to a smaller number.
    assert result.stats.generated_raw_series >= (1 << 64) - 1
    assert result.stats.intra_series_transpositions >= (1 << 64) - 1


def _python_root(
    state: ProgressiveState,
    *,
    width: int,
    preferred: str | None,
) -> tuple[SeriesSearcher, tuple[object, ...]]:
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=3,
            max_series_per_node=width,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        baseline_profile(),
    )
    prior = evaluation._native_eval  # noqa: SLF001
    evaluation._native_eval = None  # noqa: SLF001
    try:
        generated = searcher._ordered_generated(  # noqa: SLF001
            state,
            ply_from_root=1,
            preferred_series=preferred,
        )
        return searcher, tuple(
            searcher._materialize_series(item)  # noqa: SLF001
            for item in generated
        )
    finally:
        evaluation._native_eval = prior  # noqa: SLF001


@pytest.mark.parametrize("preferred", [None, "e2e4"])
def test_retained_root_enumeration_matches_python_order_state_and_work(
    preferred: str | None,
) -> None:
    state = ProgressiveState.initial()
    oracle, expected = _python_root(state, width=8, preferred=preferred)
    actual = _session().enumerate_root(
        state,
        preferred_series=preferred,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert actual.status == 0
    assert actual.enumeration_identity.startswith("spc-root-enumeration-v2|")
    assert actual.root_white_to_move
    assert actual.requested_width == 8
    assert actual.retained_count == len(actual.candidates) == len(expected)
    assert actual.width_complete is False
    assert tuple(
        _series_signature(item.series) for item in actual.candidates
    ) == tuple(_series_signature(item) for item in expected)
    assert tuple(item.order_index for item in actual.candidates) == tuple(
        range(len(expected))
    )
    assert tuple(item.order_key for item in actual.candidates) == tuple(
        item.machine_notation for item in expected
    )
    call = dict(zip(SUBTREE_STAT_FIELDS, actual.work.call_stats, strict=True))
    for field in (
        "generated_raw_series",
        "generated_unique_series",
        "intra_series_transpositions",
        "series_generation_positions",
        "frontier_score_positions",
        "generation_positions",
        "frontier_prunes",
        "frontier_states_pruned",
        "frontier_paths_pruned",
    ):
        assert call[field] == getattr(oracle.stats, field)
    assert actual.work.call_native_work == call["generation_positions"]
    assert actual.work.total_accounted_work == actual.work.native_work_after


def test_cached_root_enumeration_keeps_canonical_storage_and_exact_preference() -> None:
    state = ProgressiveState.initial()
    session = _session()
    canonical = session.enumerate_root(
        state,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    preferred = session.enumerate_root(
        state,
        preferred_series="e2e4",
        external_work=0,
        remaining_nanoseconds=None,
    )
    canonical_again = session.enumerate_root(
        state,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )

    assert canonical.status == preferred.status == canonical_again.status == 0
    canonical_keys = tuple(item.order_key for item in canonical.candidates)
    preferred_index = canonical_keys.index("e2e4")
    assert tuple(item.order_key for item in preferred.candidates) == (
        canonical_keys[preferred_index],
        *canonical_keys[:preferred_index],
        *canonical_keys[preferred_index + 1 :],
    )
    assert tuple(item.order_key for item in canonical_again.candidates) == canonical_keys
    preferred_call = dict(
        zip(SUBTREE_STAT_FIELDS, preferred.work.call_stats, strict=True)
    )
    repeated_call = dict(
        zip(SUBTREE_STAT_FIELDS, canonical_again.work.call_stats, strict=True)
    )
    assert preferred_call["generation_positions"] == 0
    assert preferred_call["series_generation_cache_hits"] == 1
    assert repeated_call["generation_positions"] == 0
    assert repeated_call["series_generation_cache_hits"] == 1


def test_terminal_mate_scan_stages_are_root_only_cache_isolated_and_fail_closed() -> None:
    state = ProgressiveState.from_fen(
        "rnk3nr/pp3ppp/8/8/8/1Pp1P3/P1PP1PPP/R1b1K1NR w K - 0 13",
        7,
    )
    target = "d2c3/e1e2/g1f3/f3g5/h1d1/g5e6/d1d8"
    session = _session(width=32, depth=5, max_work=2_000_000)

    ordinary = session.enumerate_root(
        state,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert ordinary.status == 4
    assert "promotion-mate lane" in ordinary.message

    stages: list[NativeRootEnumerationResult] = []
    for root_width in (32, 64, 128, 256):
        result = session.enumerate_root(
            state,
            preferred_series=None,
            external_work=0,
            remaining_nanoseconds=None,
            requested_width=root_width,
            terminal_mate_scan=True,
        )
        assert result.status == 0
        assert result.terminal_mate_scan
        assert result.requested_width == root_width
        assert (
            f"|descendant-width32|root-width{root_width}|terminal-scan1|"
            in result.enumeration_identity
        )
        assert result.work.call_native_work > 0
        stages.append(result)

    assert all(not result.candidates for result in stages[:3])
    assert all(not result.width_complete for result in stages[:3])
    assert tuple(item.order_key for item in stages[3].candidates) == (target,)
    mate = stages[3].candidates[0]
    assert mate.terminal_score == MATE_SCORE - 1
    assert mate.terminal_proof_bounds == (1, 1)
    assert mate.series.outcome is Outcome.CHECKMATE
    assert mate.series.ended_by_check

    # A terminal scan is evidence-only. Its widened manifest can never become
    # a searchable retained root or silently widen descendant generation.
    rejected = session.search_root_candidate(
        enumeration_identity=stages[3].enumeration_identity,
        candidate_identity=mate.candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert rejected.status == 4
    assert rejected.bound is NativeSubtreeBound.UNKNOWN


def test_terminal_mate_scan_width_cache_key_and_work_credit_are_exact() -> None:
    state = ProgressiveState.from_fen(
        "rnk3nr/pp3ppp/8/8/8/1Pp1P3/P1PP1PPP/R1b1K1NR w K - 0 13",
        7,
    )
    probe = _session(width=32, depth=5, max_work=2_000_000)
    for invalid_width in (0, 31, 833):
        invalid = probe.enumerate_root(
            state,
            preferred_series=None,
            external_work=0,
            remaining_nanoseconds=None,
            requested_width=invalid_width,
            terminal_mate_scan=True,
        )
        assert invalid.status == 4
        assert not invalid.enumeration_identity
        assert not invalid.candidates
    mismatched_ordinary = probe.enumerate_root(
        state,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        requested_width=64,
        terminal_mate_scan=False,
    )
    assert mismatched_ordinary.status == 4
    preferred_scan = probe.enumerate_root(
        state,
        preferred_series="d2c3/e1e2/g1f3/f3g5/h1d1/g5e6/d1d8",
        external_work=0,
        remaining_nanoseconds=None,
        requested_width=64,
        terminal_mate_scan=True,
    )
    assert preferred_scan.status == 4
    width32 = probe.enumerate_root(
        state,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        requested_width=32,
        terminal_mate_scan=True,
    )
    width64 = probe.enumerate_root(
        state,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        requested_width=64,
        terminal_mate_scan=True,
    )
    assert width32.status == width64.status == 0
    assert width32.work.call_native_work > 0
    assert width64.work.call_native_work > 0
    assert width32.enumeration_identity != width64.enumeration_identity

    width832 = probe.enumerate_root(
        state,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        requested_width=832,
        terminal_mate_scan=True,
    )
    assert width832.status == 0
    assert width832.terminal_mate_scan
    assert tuple(item.order_key for item in width832.candidates) == (
        "a1c1/c1d1/d2c3/g1f3/f3g5/g5e6/d1d8",
    )

    measured = _session(width=32, depth=5, max_work=2_000_000).enumerate_root(
        state,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        requested_width=64,
        terminal_mate_scan=True,
    )
    interrupted = _session(width=32, depth=5, max_work=2_000_000).enumerate_root(
        state,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        requested_width=64,
        terminal_mate_scan=True,
        call_work_credit=measured.work.call_native_work - 1,
    )
    exact = _session(width=32, depth=5, max_work=2_000_000).enumerate_root(
        state,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        requested_width=64,
        terminal_mate_scan=True,
        call_work_credit=measured.work.call_native_work,
    )
    assert interrupted.status == 1
    assert interrupted.work.call_native_work == measured.work.call_native_work - 1
    assert not interrupted.enumeration_identity
    assert exact.status == 0
    assert exact.work.call_native_work == measured.work.call_native_work
    assert exact.enumeration_identity == measured.enumeration_identity


def test_iterative_tt_growth_owns_preferred_pv_and_matches_cold_search() -> None:
    root = ProgressiveState.initial()
    warm = _session(width=8, depth=5, max_work=10_000_000)
    warm_manifest = warm.enumerate_root(
        root,
        preferred_series="e2e4",
        external_work=0,
        remaining_nanoseconds=None,
    )
    warm_candidate = warm_manifest.candidates[0]

    seeded = warm.search_root_candidate(
        enumeration_identity=warm_manifest.enumeration_identity,
        candidate_identity=warm_candidate.candidate_identity,
        child_depth=2,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    deepened = warm.search_root_candidate(
        enumeration_identity=warm_manifest.enumeration_identity,
        candidate_identity=warm_candidate.candidate_identity,
        child_depth=4,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )

    cold = _session(width=8, depth=5, max_work=10_000_000)
    cold_manifest = cold.enumerate_root(
        root,
        preferred_series="e2e4",
        external_work=0,
        remaining_nanoseconds=None,
    )
    cold_candidate = cold_manifest.candidates[0]
    expected = cold.search_root_candidate(
        enumeration_identity=cold_manifest.enumeration_identity,
        candidate_identity=cold_candidate.candidate_identity,
        child_depth=4,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )

    assert seeded.status == deepened.status == expected.status == 0
    # This fixture grows the table from 9 to 55 entries on the current exact
    # search. More generally, require enough recursive inserts to put real
    # rehash pressure on references captured from the shallower TT entry.
    assert seeded.work.tt_entries >= 8
    assert deepened.work.tt_entries > seeded.work.tt_entries * 2
    assert deepened.score == expected.score
    assert deepened.bound is expected.bound
    assert deepened.proof_bounds == expected.proof_bounds
    assert tuple(
        map(_series_signature, deepened.child_principal_variation)
    ) == tuple(map(_series_signature, expected.child_principal_variation))


def test_tt_separates_same_state_reached_at_different_root_plies() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
        1,
    )
    shared = _session(width=64, depth=4, max_work=2_000_000)
    window = (-2 * MATE_SCORE, 2 * MATE_SCORE)

    shallow_root = shared.search(
        state,
        depth=1,
        alpha=window[0],
        beta=window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    deeper_root = shared.search(
        state,
        depth=1,
        alpha=window[0],
        beta=window[1],
        ply_from_root=3,
        external_work=0,
        remaining_nanoseconds=None,
    )

    assert shallow_root.status == deeper_root.status == 0
    assert shallow_root.score == MATE_SCORE - 2
    assert deeper_root.score == MATE_SCORE - 4
    assert tuple(
        map(_series_signature, shallow_root.principal_variation)
    ) == tuple(map(_series_signature, deeper_root.principal_variation))


def test_root_contract_derives_canonical_tactical_policy_from_boundary() -> None:
    opening = ProgressiveState.initial()
    late = ProgressiveState.from_fen(opening.board.fen(), 5)

    early_manifest = _session(
        width=4,
        root_tactical_protection=True,
    ).enumerate_root(
        opening,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    late_manifest = _session(width=4).enumerate_root(
        late,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )

    assert early_manifest.status == late_manifest.status == 0
    assert (
        "|root-policycanonical-boundary-v1|root-order-hand-v1|root-tactical0"
        in early_manifest.enumeration_identity
    )
    assert (
        "|root-policycanonical-boundary-v1|root-order-hand-v1|root-tactical1"
        in late_manifest.enumeration_identity
    )


def test_series_two_terminal_mate_scan_never_activates_neural_ordering() -> None:
    series_two = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
    assert series_two.series_number == 2
    assert series_two.board.turn == chess.BLACK
    session = _session(width=32, depth=2, max_work=250_000)

    ordinary = session.enumerate_root(
        series_two,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    terminal_scan = session.enumerate_root(
        series_two,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        requested_width=32,
        terminal_mate_scan=True,
    )

    assert ordinary.status == terminal_scan.status == 0
    assert "|root-order-s3-neural-model1-blend75|" in ordinary.enumeration_identity
    assert "|root-order-hand-v1|" in terminal_scan.enumeration_identity


def test_root_import_ignores_legacy_policy_and_preserves_canonical_identity() -> None:
    root = ProgressiveState.initial()
    coordinator = _session(width=4, root_tactical_protection=False)
    manifest = coordinator.enumerate_root(
        root,
        preferred_series="e2e4",
        external_work=0,
        remaining_nanoseconds=None,
    )
    worker = _session(width=4, root_tactical_protection=True)
    imported = worker.import_root(
        root,
        manifest,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
    )

    assert manifest.status == imported.status == 0
    assert imported.enumeration_identity == manifest.enumeration_identity
    assert tuple(item.transport for item in imported.candidates) == tuple(
        item.transport for item in manifest.candidates
    )


def test_tt_separates_rebound_root_tactical_policies() -> None:
    opening = ProgressiveState.initial()
    target = ProgressiveState.from_fen(
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        2,
    )
    shared = _session(width=4, depth=3, max_work=2_000_000)
    early = shared.enumerate_root(
        opening,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    first = shared.search(
        target,
        depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        ply_from_root=2,
        external_work=0,
        remaining_nanoseconds=None,
    )
    late = shared.enumerate_root(
        ProgressiveState.from_fen(opening.board.fen(), 5),
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    second = shared.search(
        target,
        depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        ply_from_root=2,
        external_work=0,
        remaining_nanoseconds=None,
    )
    first_stats = dict(zip(SUBTREE_STAT_FIELDS, first.stats, strict=True))
    second_stats = dict(zip(SUBTREE_STAT_FIELDS, second.stats, strict=True))

    assert early.status == first.status == late.status == second.status == 0
    assert first_stats["tt_hits"] == second_stats["tt_hits"] == 0
    assert second_stats["generation_positions"] > first_stats["generation_positions"]


def test_manifest_import_replays_exact_state_and_matches_candidate_search() -> None:
    root = ProgressiveState.initial()
    coordinator = _session(width=4)
    manifest = coordinator.enumerate_root(
        root,
        preferred_series="e2e4",
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert manifest.status == 0
    worker = _session(width=4)
    imported = worker.import_root(
        root,
        manifest,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
    )
    assert imported.status == 0
    assert imported.enumeration_identity == manifest.enumeration_identity
    assert tuple(
        _series_signature(item.series) for item in imported.candidates
    ) == tuple(_series_signature(item.series) for item in manifest.candidates)
    assert imported.work.call_native_work <= sum(
        len(item.series.moves) for item in manifest.candidates
    )

    candidate = manifest.candidates[0]
    direct = coordinator.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    remote = worker.search_root_candidate(
        enumeration_identity=imported.enumeration_identity,
        candidate_identity=imported.candidates[0].candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert direct.status == remote.status == 0
    assert direct.bound is remote.bound is NativeSubtreeBound.EXACT
    assert direct.score == remote.score
    assert tuple(map(_series_signature, direct.principal_variation)) == tuple(
        map(_series_signature, remote.principal_variation)
    )
    assert direct.proof_bounds == remote.proof_bounds

    oracle = SeriesSearcher(
        SearchLimits(
            depth_series=3,
            max_series_per_node=4,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        baseline_profile(),
    )
    prior = evaluation._native_eval  # noqa: SLF001
    evaluation._native_eval = None  # noqa: SLF001
    try:
        score, child_pv, proof = oracle._minimax(  # noqa: SLF001
            candidate.series.final_state,
            1,
            -2 * MATE_SCORE,
            2 * MATE_SCORE,
            1,
        )
    finally:
        evaluation._native_eval = prior  # noqa: SLF001
    assert direct.score == score
    assert tuple(map(_series_signature, direct.child_principal_variation)) == tuple(
        map(_series_signature, child_pv)
    )
    assert direct.proof_bounds == proof


def test_small_root_corpus_cold_full_matches_warm_iterative_per_candidate() -> None:
    root = ProgressiveState.initial()
    warm = _session(width=4, depth=3, max_work=4_000_000)
    warm_manifest = warm.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidates = warm_manifest.candidates[:3]
    assert len(candidates) == 3

    warm_final: dict[str, object] = {}
    for child_depth in range(3):
        for candidate in candidates:
            result = warm.search_root_candidate(
                enumeration_identity=warm_manifest.enumeration_identity,
                candidate_identity=candidate.candidate_identity,
                child_depth=child_depth,
                alpha=-2 * MATE_SCORE,
                beta=2 * MATE_SCORE,
                external_work=0,
                remaining_nanoseconds=None,
                rollback_tt=False,
            )
            assert result.status == 0
            assert result.bound is NativeSubtreeBound.EXACT
            if child_depth == 2:
                warm_final[candidate.candidate_identity] = result

    for candidate in candidates:
        cold = _session(width=4, depth=3, max_work=4_000_000)
        cold_manifest = cold.enumerate_root(
            root,
            preferred_series=None,
            external_work=0,
            remaining_nanoseconds=None,
        )
        cold_candidate = next(
            item
            for item in cold_manifest.candidates
            if item.candidate_identity == candidate.candidate_identity
        )
        cold_result = cold.search_root_candidate(
            enumeration_identity=cold_manifest.enumeration_identity,
            candidate_identity=cold_candidate.candidate_identity,
            child_depth=2,
            alpha=-2 * MATE_SCORE,
            beta=2 * MATE_SCORE,
            external_work=0,
            remaining_nanoseconds=None,
            rollback_tt=False,
        )
        assert _candidate_search_signature(
            warm_final[candidate.candidate_identity]
        ) == _candidate_search_signature(cold_result)


def test_scout_rollback_then_full_matches_cold_full_window() -> None:
    root = ProgressiveState.initial()
    cold = _session(width=4, depth=3)
    cold_manifest = cold.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = cold_manifest.candidates[1]
    expected = cold.search_root_candidate(
        enumeration_identity=cold_manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=2,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert expected.status == 0

    warm = _session(width=4, depth=3)
    warm_manifest = warm.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    warm_candidate = next(
        item
        for item in warm_manifest.candidates
        if item.candidate_identity == candidate.candidate_identity
    )
    for alpha, beta, expected_bound in (
        (expected.score, expected.score + 1, NativeSubtreeBound.UPPER),
        (expected.score - 1, expected.score, NativeSubtreeBound.LOWER),
    ):
        scout = warm.search_root_candidate(
            enumeration_identity=warm_manifest.enumeration_identity,
            candidate_identity=warm_candidate.candidate_identity,
            child_depth=2,
            alpha=alpha,
            beta=beta,
            external_work=0,
            remaining_nanoseconds=None,
            rollback_tt=True,
        )
        assert scout.status == 0
        assert scout.bound is expected_bound

    actual = warm.search_root_candidate(
        enumeration_identity=warm_manifest.enumeration_identity,
        candidate_identity=warm_candidate.candidate_identity,
        child_depth=2,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert _candidate_search_signature(actual) == _candidate_search_signature(expected)


def test_committed_work_limit_then_retry_matches_cold_full_window() -> None:
    root = ProgressiveState.initial()
    coordinator = _session(width=4, depth=3, max_work=4_000_000)
    manifest = coordinator.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = manifest.candidates[0]

    def imported_worker() -> tuple[NativeSubtreeSession, NativeRootEnumerationResult]:
        worker = _session(width=4, depth=3, max_work=4_000_000)
        imported = worker.import_root(
            root,
            manifest,
            external_work=manifest.work.native_work_after,
            remaining_nanoseconds=None,
        )
        assert imported.status == 0
        return worker, imported

    cold, cold_manifest = imported_worker()
    expected = cold.search_root_candidate(
        enumeration_identity=cold_manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=2,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert expected.status == 0
    assert expected.work.call_native_work > 2

    warm, warm_manifest = imported_worker()
    interrupted = warm.search_root_candidate(
        enumeration_identity=warm_manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=2,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
        call_work_credit=max(1, expected.work.call_native_work // 2),
    )
    assert interrupted.status == 1
    assert interrupted.bound is NativeSubtreeBound.UNKNOWN
    retry = warm.search_root_candidate(
        enumeration_identity=warm_manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=2,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
        call_work_credit=expected.work.call_native_work + 1,
    )
    assert _candidate_search_signature(retry) == _candidate_search_signature(expected)


def test_valid_manifest_replacement_rejects_stale_enumeration_identity() -> None:
    root = ProgressiveState.initial()
    session = _session(width=4)
    first = session.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    preferred = first.candidates[-1].order_key
    replacement = session.enumerate_root(
        root,
        preferred_series=preferred,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert first.status == replacement.status == 0
    assert first.enumeration_identity != replacement.enumeration_identity
    assert replacement.candidates[0].order_key == preferred

    stale = session.search_root_candidate(
        enumeration_identity=first.enumeration_identity,
        candidate_identity=first.candidates[0].candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert stale.status == 4
    assert stale.bound is NativeSubtreeBound.UNKNOWN
    current = session.search_root_candidate(
        enumeration_identity=replacement.enumeration_identity,
        candidate_identity=replacement.candidates[0].candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert current.status == 0
    assert current.bound is NativeSubtreeBound.EXACT


def test_manifest_import_rejects_tampering_and_leaves_no_searchable_root() -> None:
    root = ProgressiveState.initial()
    manifest = _session(width=4).enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = manifest.candidates[0]
    raw = list(candidate.transport)
    state = list(raw[5])
    state[11] = int(state[11]) + 1  # halfmove clock
    raw[5] = tuple(state)
    corrupted = replace(candidate, transport=tuple(raw))
    bad_manifest = replace(
        manifest,
        candidates=(corrupted,) + manifest.candidates[1:],
    )
    worker = _session(width=4)
    rejected = worker.import_root(
        root,
        bad_manifest,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
    )
    assert rejected.status == 4
    assert rejected.enumeration_identity == ""
    stale = worker.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert stale.status == 4
    assert stale.bound is NativeSubtreeBound.UNKNOWN

    mismatched_ceiling = _session(width=4, eval_capacity=131_072).import_root(
        root,
        manifest,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
    )
    assert mismatched_ceiling.status == 4
    assert not mismatched_ceiling.enumeration_identity


def test_failed_reimport_preserves_prior_verified_manifest_transactionally() -> None:
    root = ProgressiveState.initial()
    manifest = _session(width=4).enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    worker = _session(width=4)
    imported = worker.import_root(
        root,
        manifest,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
    )
    assert imported.status == 0

    candidate = manifest.candidates[0]
    raw = list(candidate.transport)
    raw[2] = "a1a1"  # canonical order key
    corrupted = replace(candidate, transport=tuple(raw))
    rejected = worker.import_root(
        root,
        replace(
            manifest,
            candidates=(corrupted,) + manifest.candidates[1:],
        ),
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
    )
    assert rejected.status == 4
    assert not rejected.enumeration_identity

    still_searchable = worker.search_root_candidate(
        enumeration_identity=imported.enumeration_identity,
        candidate_identity=imported.candidates[0].candidate_identity,
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert still_searchable.status == 0
    assert still_searchable.candidate_identity == candidate.candidate_identity


def test_raw_boundary_contract_rejects_invalid_state_fields() -> None:
    root = ProgressiveState.initial()
    board = root.board
    canonical = [
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
        board.occupied_co[chess.WHITE],
        board.occupied_co[chess.BLACK],
        board.promoted,
        board.castling_rights,
        board.turn,
        board.halfmove_clock,
        board.fullmove_number,
        root.series_number,
        root.quiet_series,
        root.ep_targets,
    ]
    invalid_states: list[tuple[object, ...]] = []
    for index, value in (
        (10, chess.BLACK),
        (9, board.castling_rights | chess.BB_B1),
        (15, (chess.E3,)),
        (11, -1),
        (12, 0),
        (8, board.kings),
    ):
        changed = canonical.copy()
        changed[index] = value
        invalid_states.append(tuple(changed))

    for raw_state in invalid_states:
        session = _session(width=4)
        response = session._native.subtree_enumerate_root(  # noqa: SLF001
            session._capsule,  # noqa: SLF001
            raw_state,
            (),
            4,
            False,
            0,
            None,
            None,
        )
        assert int(response[0]) == 4
        assert response[2] == ""
        assert not response[8]


@pytest.mark.parametrize(
    ("fen", "series_number", "expected_score"),
    [
        ("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1", 1, MATE_SCORE - 1),
        ("8/8/8/8/8/6k1/5q2/7K b - - 0 1", 2, -MATE_SCORE + 1),
    ],
)
def test_terminal_root_candidate_scoring_is_mover_aware_and_exact(
    fen: str,
    series_number: int,
    expected_score: int,
) -> None:
    root = ProgressiveState.from_fen(fen, series_number)
    session = _session(width=64, depth=2)
    manifest = session.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = next(
        item for item in manifest.candidates
        if item.terminal_score == expected_score
    )
    result = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=1,
        alpha=-1,
        beta=1,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=True,
        call_work_credit=0,
    )
    assert result.status == 0
    assert result.terminal
    assert result.bound is NativeSubtreeBound.EXACT
    assert result.score == expected_score
    assert result.proof_bounds == ((1, 1) if expected_score > 0 else (-1, -1))
    assert result.work.call_native_work == 0
    assert result.work.call_work_credit == 0


def test_root_contract_deadline_work_and_adjudication_fail_closed() -> None:
    root = ProgressiveState.initial()
    deadline = _session().enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=0,
    )
    assert deadline.status == 2
    assert not deadline.enumeration_identity
    assert not deadline.candidates

    work = _session(max_work=10).enumerate_root(
        root,
        preferred_series=None,
        external_work=10,
        remaining_nanoseconds=None,
    )
    assert work.status == 1
    assert work.work.total_accounted_work == 10
    assert not work.enumeration_identity

    quiet = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K7 w - - 0 1",
        1,
        quiet_series=10,
    )
    unknown = _session().enumerate_root(
        quiet,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert unknown.status == 3
    assert not unknown.enumeration_identity

    bounded = _session(width=4, eval_capacity=1)
    manifest = bounded.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    capped = bounded.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=manifest.candidates[0].candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert capped.status == 4
    assert capped.bound is NativeSubtreeBound.UNKNOWN
    # Failed root calls discard partial fast-path state but preserve the peak
    # receipt so capacity enforcement remains auditable.
    assert capped.work.eval_entries == 0
    assert capped.work.eval_entries_peak == 1
    assert capped.work.eval_capacity == 1
    assert capped.work.tt_entries <= capped.work.tt_entries_peak
    assert capped.work.tt_entries_peak <= capped.work.tt_capacity

    tt_bounded = _session(width=4, depth=3, tt_capacity=1)
    tt_manifest = tt_bounded.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    tt_capped = tt_bounded.search_root_candidate(
        enumeration_identity=tt_manifest.enumeration_identity,
        candidate_identity=tt_manifest.candidates[0].candidate_identity,
        child_depth=2,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert tt_capped.status == 4
    assert tt_capped.bound is NativeSubtreeBound.UNKNOWN
    assert tt_capped.work.tt_entries == 0
    assert tt_capped.work.tt_entries_peak == 1
    assert tt_capped.tt_writes_rolled_back == 1
    assert tt_capped.work.tt_capacity == 1


def test_exact_boundary_manifest_preserves_promoted_ep_and_clocks() -> None:
    promoted_board = chess.Board("7k/8/8/8/8/8/Q7/K7 w - - 12 34")
    promoted_board.promoted = chess.BB_A2
    promoted = ProgressiveState(promoted_board, series_number=1)
    ep = ProgressiveState.from_fen(
        "7k/8/8/pPpP4/8/8/8/K7 w - - 7 19",
        3,
        ep_targets=(chess.A6, chess.C6),
    )
    for root in (promoted, ep):
        manifest = _session(width=4).enumerate_root(
            root,
            preferred_series=None,
            external_work=0,
            remaining_nanoseconds=None,
        )
        assert manifest.status == 0
        imported = _session(width=4).import_root(
            root,
            manifest,
            external_work=manifest.work.native_work_after,
            remaining_nanoseconds=None,
        )
        assert imported.status == 0
        assert tuple(
            _series_signature(item.series) for item in imported.candidates
        ) == tuple(_series_signature(item.series) for item in manifest.candidates)


def test_transactional_candidate_call_reports_bound_and_rolls_back() -> None:
    root = ProgressiveState.initial()
    session = _session(width=4)
    manifest = session.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = manifest.candidates[0]
    exact = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert exact.bound is NativeSubtreeBound.EXACT
    probe = _session(width=4)
    imported = probe.import_root(
        root,
        manifest,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
    )
    upper = probe.search_root_candidate(
        enumeration_identity=imported.enumeration_identity,
        candidate_identity=imported.candidates[0].candidate_identity,
        child_depth=1,
        alpha=exact.score,
        beta=exact.score + 1,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=True,
    )
    assert upper.status == 0
    assert upper.bound is NativeSubtreeBound.UPPER
    assert upper.score <= exact.score
    assert upper.tt_writes_rolled_back >= 0


@pytest.mark.parametrize("native_threads", (1, 4))
def test_transactional_ordinary_cutoff_hint_reproves_bound_without_generation(
    native_threads: int,
) -> None:
    root = ProgressiveState.initial()

    oracle = _session(
        width=4,
        depth=5,
        cache_capacity=1,
        native_threads=native_threads,
    )
    oracle_manifest = oracle.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    oracle_candidate = oracle_manifest.candidates[0]
    exact = oracle.search_root_candidate(
        enumeration_identity=oracle_manifest.enumeration_identity,
        candidate_identity=oracle_candidate.candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=oracle_manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert exact.bound is NativeSubtreeBound.EXACT

    session = _session(
        width=4,
        depth=5,
        cache_capacity=1,
        native_threads=native_threads,
    )
    manifest = session.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = manifest.candidates[0]
    calls = []
    for credit in (None, 0):
        calls.append(
            session.search_root_candidate(
                enumeration_identity=manifest.enumeration_identity,
                candidate_identity=candidate.candidate_identity,
                child_depth=1,
                alpha=exact.score,
                beta=exact.score + 1,
                external_work=manifest.work.native_work_after,
                call_work_credit=credit,
                remaining_nanoseconds=None,
                rollback_tt=True,
            )
        )

    first, hinted = calls
    assert first.bound is hinted.bound is NativeSubtreeBound.UPPER
    assert first.score == -406
    assert first.child_principal_variation
    assert first.child_principal_variation[0].machine_notation == "e7e5/f8b4"
    assert first.child_principal_variation[0].outcome is None
    assert hinted.child_principal_variation == ()
    assert hinted.score == first.score
    assert hinted.proof_bounds == first.proof_bounds
    assert hinted.root_series == first.root_series
    assert first.work.call_native_work == 26
    assert hinted.work.call_native_work == 0
    hinted_stats = dict(
        zip(SUBTREE_STAT_FIELDS, hinted.work.call_stats, strict=True)
    )
    assert hinted_stats["generated_raw_series"] == 0
    assert hinted_stats["generated_unique_series"] == 0
    assert hinted_stats["generation_positions"] == 0
    assert hinted.tt_writes_rolled_back > 0

    reconstructed = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert _candidate_search_signature(reconstructed) == _candidate_search_signature(
        exact
    )

    cold = _session(
        width=4,
        depth=5,
        cache_capacity=1,
        native_threads=native_threads,
    )
    cold_manifest = cold.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    interrupted = cold.search_root_candidate(
        enumeration_identity=cold_manifest.enumeration_identity,
        candidate_identity=cold_manifest.candidates[0].candidate_identity,
        child_depth=1,
        alpha=exact.score,
        beta=exact.score + 1,
        external_work=cold_manifest.work.native_work_after,
        call_work_credit=0,
        remaining_nanoseconds=None,
        rollback_tt=True,
    )
    assert interrupted.status != 0
    assert interrupted.bound is NativeSubtreeBound.UNKNOWN
    assert interrupted.child_principal_variation == ()
    assert interrupted.work.call_native_work == 0


@pytest.mark.parametrize("native_threads", (1, 4))
def test_transactional_bound_overlay_reuses_lower_bound_without_pv(
    native_threads: int,
) -> None:
    root = ProgressiveState.initial()
    oracle = _session(
        width=4,
        depth=5,
        cache_capacity=1,
        native_threads=native_threads,
    )
    oracle_manifest = oracle.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    oracle_candidate = oracle_manifest.candidates[0]
    exact = oracle.search_root_candidate(
        enumeration_identity=oracle_manifest.enumeration_identity,
        candidate_identity=oracle_candidate.candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=oracle_manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert exact.bound is NativeSubtreeBound.EXACT

    session = _session(
        width=4,
        depth=5,
        cache_capacity=1,
        native_threads=native_threads,
    )
    manifest = session.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = manifest.candidates[0]
    scout_args = dict(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=1,
        alpha=exact.score - 1,
        beta=exact.score,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=True,
    )
    first = session.search_root_candidate(**scout_args)
    reused = session.search_root_candidate(
        **scout_args,
        call_work_credit=0,
    )

    assert first.bound is reused.bound is NativeSubtreeBound.LOWER
    assert first.score == reused.score == exact.score
    assert first.work.call_native_work > 0
    assert first.child_principal_variation
    assert reused.work.call_native_work == 0
    assert reused.child_principal_variation == ()
    assert reused.proof_bounds == first.proof_bounds
    reused_stats = dict(
        zip(SUBTREE_STAT_FIELDS, reused.work.call_stats, strict=True)
    )
    assert reused_stats["nodes"] == 1
    assert reused_stats["tt_hits"] == 1
    assert reused_stats["generation_positions"] == 0
    assert reused.tt_writes_rolled_back == 0

    reconstructed = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert _candidate_search_signature(reconstructed) == _candidate_search_signature(
        exact
    )


def _scout_tail_order_fixture(
    mover: str,
) -> tuple[
    ProgressiveState,
    tuple[str, ...],
    tuple[int, int],
    tuple[int, int],
    int,
    tuple[str, ...],
    tuple[str, ...],
]:
    if mover == "white":
        return (
            ProgressiveState.initial(),
            ("e2e3",),
            (519, 520),
            (600, 601),
            542,
            ("e2e3", "f7f5/e8f7", "f1b5/b5d7/d1h5"),
            ("d2d3",),
        )
    if mover == "black":
        return (
            play_series(ProgressiveState.initial(), ("e2e4",)).final_state,
            ("d7d5", "d5e4"),
            (-1000, -999),
            (-1200, -1199),
            -1183,
            (
                "d7d5/d5e4",
                "d1e2/e2e4/e4a4",
                "c8d7/d7a4/d8d4/a4c2",
            ),
            ("d7d5", "c8g4"),
        )
    raise AssertionError(f"unknown mover fixture {mover!r}")


def _seed_child_scout_bound(
    session: NativeSubtreeSession,
    child: ProgressiveState,
    *,
    depth: int,
    ply_from_root: int,
    window: tuple[int, int],
    transactional: bool,
) -> NativeSubtreeResult:
    if transactional:
        session.begin_transaction()
    result = session.search(
        child,
        depth=depth,
        alpha=window[0],
        beta=window[1],
        ply_from_root=ply_from_root,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert result.status == 0
    if transactional:
        assert session.rollback_transaction() > 0
    return result


@pytest.mark.parametrize(
    "bound_source",
    ("committed-bound", "transactional-bound", "committed-exact"),
)
@pytest.mark.parametrize("mover", ("white", "black"))
def test_scout_orders_a_cutoff_proving_tail_child_from_a_bound(
    mover: str,
    bound_source: str,
) -> None:
    (
        parent,
        target_moves,
        window,
        _wrong_window,
        expected_score,
        _pv,
        preserved_head_moves,
    ) = (
        _scout_tail_order_fixture(mover)
    )
    child = play_series(parent, target_moves).final_state
    transactional = bound_source == "transactional-bound"
    seed_window = (
        (-2 * MATE_SCORE, 2 * MATE_SCORE)
        if bound_source == "committed-exact"
        else window
    )

    seed_meter = _session(width=8, depth=5, max_work=20_000_000)
    measured_seed = _seed_child_scout_bound(
        seed_meter,
        child,
        depth=2,
        ply_from_root=1,
        window=seed_window,
        transactional=transactional,
    )
    seed_work = dict(
        zip(SUBTREE_STAT_FIELDS, measured_seed.stats, strict=True)
    )["generation_positions"]
    if bound_source == "committed-exact":
        assert measured_seed.score == expected_score
    else:
        assert (
            measured_seed.score >= window[1]
            if mover == "white"
            else measured_seed.score <= window[0]
        )

    root_meter = _session(width=8, depth=5, max_work=20_000_000)
    manifest = root_meter.enumerate_root(
        parent,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert manifest.status == 0
    target_key = "/".join(target_moves)
    target = next(item for item in manifest.candidates if item.order_key == target_key)
    assert target.order_index >= 2
    preserved_head_key = "/".join(preserved_head_moves)
    # Black's public root manifest uses its existing S3 neural ordering, while
    # this generic minimax fixture retains the non-root traversal head.
    preserved_head = next(
        item for item in manifest.candidates if item.order_key == preserved_head_key
    )
    assert preserved_head.candidate_identity != target.candidate_identity
    parent_generation_work = manifest.work.call_native_work
    assert parent_generation_work > 0

    head_meter = _session(width=8, depth=5, max_work=20_000_000)
    head_seed = _seed_child_scout_bound(
        head_meter,
        child,
        depth=2,
        ply_from_root=1,
        window=seed_window,
        transactional=transactional,
    )
    head_before = dict(zip(SUBTREE_STAT_FIELDS, head_seed.stats, strict=True))
    head_result = head_meter.search(
        preserved_head.series.final_state,
        depth=2,
        alpha=window[0],
        beta=window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert head_result.status == 0
    head_after = dict(zip(SUBTREE_STAT_FIELDS, head_result.stats, strict=True))
    preserved_head_work = (
        head_after["generation_positions"] - head_before["generation_positions"]
    )
    assert preserved_head_work > 0

    bounded = _session(
        width=8,
        depth=5,
        max_work=(
            seed_work + parent_generation_work + preserved_head_work + 1
        ),
    )
    seeded = _seed_child_scout_bound(
        bounded,
        child,
        depth=2,
        ply_from_root=1,
        window=seed_window,
        transactional=transactional,
    )
    before = dict(zip(SUBTREE_STAT_FIELDS, seeded.stats, strict=True))
    assert before["generation_positions"] == seed_work

    result = bounded.search(
        parent,
        depth=3,
        alpha=window[0],
        beta=window[1],
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )
    after = dict(zip(SUBTREE_STAT_FIELDS, result.stats, strict=True))

    assert result.status == 0
    assert result.score == seeded.score
    assert result.principal_variation == ()
    assert (
        after["generation_positions"] - before["generation_positions"]
        == parent_generation_work + preserved_head_work
    )
    assert (
        after["tt_hits"] - before["tt_hits"]
        == head_after["tt_hits"] - head_before["tt_hits"] + 1
    )
    assert (
        after["generation_work_limit_hits"]
        == before["generation_work_limit_hits"]
    )


def test_scout_keeps_multiple_cutoff_proving_tail_children_stable() -> None:
    parent = ProgressiveState.initial()
    window = (519, 520)
    session = _session(width=8, depth=5, max_work=20_000_000)
    seeded_scores = []
    for moves in (("e2e3",), ("e2e4",)):
        child = play_series(parent, moves).final_state
        seeded = _seed_child_scout_bound(
            session,
            child,
            depth=2,
            ply_from_root=1,
            window=(-2 * MATE_SCORE, 2 * MATE_SCORE),
            transactional=False,
        )
        seeded_scores.append(seeded.score)
    assert seeded_scores == [542, 530]

    result = session.search(
        parent,
        depth=3,
        alpha=window[0],
        beta=window[1],
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )

    assert result.status == 0
    assert result.score == seeded_scores[0]
    assert result.principal_variation == ()


def _alter_scout_child_exact_context(
    child: ProgressiveState,
    context: str,
) -> ProgressiveState:
    board = child.board.copy(stack=False)
    quiet_series = child.quiet_series
    if context == "promoted-provenance":
        board.promoted |= chess.BB_B1
    elif context == "halfmove-clock":
        board.halfmove_clock += 1
    elif context == "fullmove-number":
        board.fullmove_number += 1
    elif context == "quiet-series":
        quiet_series += 1
    elif context == "castling-rights":
        board.castling_rights &= ~chess.BB_H1
    else:  # pragma: no cover - the parametrization is the closed test contract.
        raise AssertionError(f"unknown exact child context {context!r}")
    return ProgressiveState(
        board,
        series_number=child.series_number,
        quiet_series=quiet_series,
        ep_targets=child.ep_targets,
    )


@pytest.mark.parametrize(
    ("context", "expected_parent_work"),
    (
        ("promoted-provenance", 3_503),
        ("halfmove-clock", 1_709),
        ("fullmove-number", 3_503),
        ("quiet-series", 1_709),
        ("castling-rights", 4_297),
    ),
)
@pytest.mark.parametrize(
    "transactional",
    (False, True),
    ids=("committed-tt", "transactional-overlay"),
)
def test_scout_child_bound_never_aliases_exact_state_context(
    context: str,
    expected_parent_work: int,
    transactional: bool,
) -> None:
    parent = ProgressiveState.initial()
    child = play_series(parent, ("e2e3",)).final_state
    mismatched_child = _alter_scout_child_exact_context(child, context)
    window = (519, 520)
    session = _session(width=8, depth=5, max_work=20_000_000)
    seeded = _seed_child_scout_bound(
        session,
        mismatched_child,
        depth=2,
        ply_from_root=1,
        window=window,
        transactional=transactional,
    )
    assert seeded.score >= window[1]
    before = dict(zip(SUBTREE_STAT_FIELDS, seeded.stats, strict=True))

    result = session.search(
        parent,
        depth=3,
        alpha=window[0],
        beta=window[1],
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )
    after = dict(zip(SUBTREE_STAT_FIELDS, result.stats, strict=True))

    assert result.status == 0
    assert (
        after["generation_positions"] - before["generation_positions"]
        == expected_parent_work
    )
    # If any exact-state field above were dropped from the child TT key, the
    # seeded e3 bound would be treated as authoritative and this search would
    # take the 980-position proof-only reorder path instead.
    assert expected_parent_work != 980


@pytest.mark.parametrize(
    "transactional",
    (False, True),
    ids=("committed-tt", "transactional-overlay"),
)
@pytest.mark.parametrize(
    "context",
    ("series-number", "progressive-ep"),
)
def test_scout_child_bound_never_aliases_progressive_context(
    context: str,
    transactional: bool,
) -> None:
    if context == "series-number":
        parent = ProgressiveState.initial()
        child = play_series(parent, ("e2e3",)).final_state
        mismatched_child = ProgressiveState(
            child.board,
            series_number=child.series_number + 2,
            quiet_series=child.quiet_series,
            ep_targets=child.ep_targets,
        )
        window = (-355, -354)
        expected_parent_work = 2_149
    else:
        parent = ProgressiveState.from_fen(
            "rnbqkbnr/ppp1pppp/8/8/3p4/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            1,
        )
        child = play_series(parent, ("e2e4",)).final_state
        assert child.ep_targets == (chess.E3,)
        without_ep = child.board.copy(stack=False)
        without_ep.ep_square = None
        mismatched_child = ProgressiveState(
            without_ep,
            series_number=child.series_number,
            quiet_series=child.quiet_series,
            ep_targets=(),
        )
        window = (-258, -257)
        expected_parent_work = 1_664

    session = _session(width=8, depth=5, max_work=20_000_000)
    seeded = _seed_child_scout_bound(
        session,
        mismatched_child,
        depth=2,
        ply_from_root=1,
        window=window,
        transactional=transactional,
    )
    assert seeded.score >= window[1]
    before = dict(zip(SUBTREE_STAT_FIELDS, seeded.stats, strict=True))

    result = session.search(
        parent,
        depth=3,
        alpha=window[0],
        beta=window[1],
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )
    after = dict(zip(SUBTREE_STAT_FIELDS, result.stats, strict=True))

    assert result.status == 0
    assert tuple(
        item.machine_notation for item in result.principal_variation
    ) == ("d2d3", "e7e5/f8b4", "c2c3/c1g5/g5d8")
    assert (
        after["generation_positions"] - before["generation_positions"]
        == expected_parent_work
    )
    # The mismatched child is a later tail candidate and its seeded bound
    # proves this window. Dropping either field would activate proof-only tail
    # ordering and suppress the canonical PV above, even though the preserved
    # head itself still cuts off.


@pytest.mark.parametrize("mover", ("white", "black"))
@pytest.mark.parametrize(
    "transactional",
    (False, True),
    ids=("committed-tt", "transactional-overlay"),
)
@pytest.mark.parametrize(
    ("bound_case", "expected_parent_work"),
    (
        pytest.param(
            "mismatched-context",
            {"white": 3503, "black": 6234},
            id="mismatched-context",
        ),
        pytest.param(
            "shallow",
            {"white": 4268, "black": 5993},
            id="shallow",
        ),
        pytest.param(
            "wrong-direction",
            {"white": 3393, "black": 3685},
            id="wrong-direction",
        ),
    ),
)
def test_scout_does_not_order_a_tail_child_from_an_unusable_bound(
    mover: str,
    transactional: bool,
    bound_case: str,
    expected_parent_work: dict[str, int],
) -> None:
    parent, target_moves, window, wrong_window, _score, _pv, _head = (
        _scout_tail_order_fixture(mover)
    )
    child = play_series(parent, target_moves).final_state
    seed_depth = 1 if bound_case == "shallow" else 2
    seed_ply = 2 if bound_case == "mismatched-context" else 1
    seed_window = wrong_window if bound_case == "wrong-direction" else window
    session = _session(width=8, depth=5, max_work=20_000_000)
    seeded = _seed_child_scout_bound(
        session,
        child,
        depth=seed_depth,
        ply_from_root=seed_ply,
        window=seed_window,
        transactional=transactional,
    )
    before = dict(zip(SUBTREE_STAT_FIELDS, seeded.stats, strict=True))

    result = session.search(
        parent,
        depth=3,
        alpha=window[0],
        beta=window[1],
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )
    after = dict(zip(SUBTREE_STAT_FIELDS, result.stats, strict=True))

    assert result.status == 0
    assert (
        after["generation_positions"] - before["generation_positions"]
        == expected_parent_work[mover]
    )


@pytest.mark.parametrize("mover", ("white", "black"))
@pytest.mark.parametrize(
    "transactional",
    (False, True),
    ids=("committed-tt", "transactional-overlay"),
)
def test_scout_child_bound_never_changes_the_canonical_full_window_pv(
    mover: str,
    transactional: bool,
) -> None:
    (
        parent,
        target_moves,
        window,
        _wrong_window,
        expected_score,
        expected_pv,
        _head,
    ) = (
        _scout_tail_order_fixture(mover)
    )
    child = play_series(parent, target_moves).final_state
    full_window = (-2 * MATE_SCORE, 2 * MATE_SCORE)
    cold = _session(width=8, depth=5, max_work=20_000_000).search(
        parent,
        depth=3,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )

    warm_session = _session(width=8, depth=5, max_work=20_000_000)
    _seed_child_scout_bound(
        warm_session,
        child,
        depth=2,
        ply_from_root=1,
        window=window,
        transactional=transactional,
    )
    warm = warm_session.search(
        parent,
        depth=3,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )

    assert cold.status == warm.status == 0
    assert cold.score == warm.score == expected_score
    assert cold.proof_bounds == warm.proof_bounds
    assert tuple(item.machine_notation for item in cold.principal_variation) == (
        expected_pv
    )
    assert tuple(
        _series_signature(item) for item in warm.principal_variation
    ) == tuple(_series_signature(item) for item in cold.principal_variation)


def test_deep_losing_scout_stops_after_mover_mate_proves_upper_bound() -> None:
    root = ProgressiveState.initial()
    session = _session(width=32, depth=5, max_work=5_000_000)
    manifest = session.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = next(
        item for item in manifest.candidates if item.order_key == "g1f3"
    )

    scout = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=4,
        alpha=951,
        beta=952,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=True,
    )

    assert scout.status == 0
    assert scout.bound is NativeSubtreeBound.UPPER
    assert scout.score == -MATE_SCORE + 4
    assert scout.child_principal_variation == ()
    assert scout.proof_bounds == (-1, 1)
    assert scout.work.call_native_work == 15_316
    assert scout.tt_writes_rolled_back > 0


def test_proof_only_hint_cannot_escape_root_candidate_pv() -> None:
    root = ProgressiveState.initial()
    session = _session(width=32, depth=5, max_work=5_000_000)
    manifest = session.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = next(
        item for item in manifest.candidates if item.order_key == "g1f3"
    )

    seeded = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=3,
        alpha=951,
        beta=952,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    deepened = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=4,
        alpha=951,
        beta=952,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )

    assert seeded.status == deepened.status == 0
    assert seeded.bound is deepened.bound is NativeSubtreeBound.UPPER
    assert seeded.score == deepened.score == -MATE_SCORE + 4
    assert seeded.proof_bounds == deepened.proof_bounds == (-1, 1)
    assert seeded.child_principal_variation == ()
    assert deepened.child_principal_variation == ()
    assert seeded.principal_variation == deepened.principal_variation == (
        candidate.series,
    )
    assert seeded.work.call_native_work == 15_316
    assert deepened.work.call_native_work == 0


@pytest.mark.parametrize(
    ("fen", "series", "alpha", "beta", "score", "proof", "work"),
    (
        (
            "3k2K1/Q7/8/8/8/8/8/8 w - - 0 1",
            3,
            0,
            1,
            MATE_SCORE - 1,
            (1, 1),
            134,
        ),
        (
            "6K1/1q6/8/8/8/3k4/8/8 b - - 0 1",
            4,
            -1,
            0,
            -MATE_SCORE + 1,
            (-1, -1),
            789,
        ),
    ),
)
def test_mover_mate_exit_is_work_identical_across_native_thread_counts(
    fen: str,
    series: int,
    alpha: int,
    beta: int,
    score: int,
    proof: tuple[int, int],
    work: int,
) -> None:
    mate_bound = ProgressiveState.from_fen(fen, series)
    results = []
    for native_threads in (1, 4):
        results.append(
            _session(
                width=128,
                depth=5,
                max_work=5_000,
                native_threads=native_threads,
            ).search(
                mate_bound,
                depth=1,
                alpha=alpha,
                beta=beta,
                ply_from_root=0,
                external_work=0,
                remaining_nanoseconds=None,
            )
        )

    serial, parallel = results
    assert serial == parallel
    assert serial.status == 0
    assert serial.score == score
    assert serial.principal_variation == ()
    assert serial.proof_bounds == proof
    stats = dict(zip(SUBTREE_STAT_FIELDS, serial.stats, strict=True))
    assert stats["generation_positions"] == work
    assert stats["generation_work_limit_hits"] == 0


def test_mover_mate_bound_reconstructs_canonical_full_window_pv() -> None:
    fixtures = (
        (
            ProgressiveState.from_fen(
                "3k2K1/Q7/8/8/8/8/8/8 w - - 0 1",
                3,
            ),
            0,
            1,
            ("g8f7/f7e6/a7b8",),
        ),
        (
            ProgressiveState.from_fen(
                "6K1/1q6/8/8/8/3k4/8/8 b - - 0 1",
                4,
            ),
            -1,
            0,
            ("d3d4/d4e5/e5f6/b7g7",),
        ),
    )
    for state, alpha, beta, expected_pv in fixtures:
        cold = _session(width=128, depth=5, max_work=20_000).search(
            state,
            depth=1,
            alpha=-2 * MATE_SCORE,
            beta=2 * MATE_SCORE,
            ply_from_root=0,
            external_work=0,
            remaining_nanoseconds=None,
        )
        warm_session = _session(width=128, depth=5, max_work=20_000)
        bound = warm_session.search(
            state,
            depth=1,
            alpha=alpha,
            beta=beta,
            ply_from_root=0,
            external_work=0,
            remaining_nanoseconds=None,
        )
        warm = warm_session.search(
            state,
            depth=1,
            alpha=-2 * MATE_SCORE,
            beta=2 * MATE_SCORE,
            ply_from_root=0,
            external_work=0,
            remaining_nanoseconds=None,
        )

        assert bound.status == 0
        assert bound.principal_variation == ()
        assert warm.status == cold.status == 0
        assert warm.score == cold.score == bound.score
        assert warm.proof_bounds == cold.proof_bounds == bound.proof_bounds
        assert tuple(
            item.machine_notation for item in warm.principal_variation
        ) == expected_pv
        assert warm.principal_variation == cold.principal_variation


def test_warm_mate_bound_convergence_reconstructs_canonical_pv() -> None:
    state = ProgressiveState.from_fen(
        "4k1nB/2pB3p/8/8/P3n3/4K3/2P2P1P/7r b - - 1 21",
        10,
    )
    session = _session(
        width=32,
        depth=5,
        max_work=25_000_000,
        cache_capacity=65_536,
        root_tactical_protection=True,
    )
    full_window = (-2 * MATE_SCORE, 2 * MATE_SCORE)

    depth_two = session.search(
        state,
        depth=2,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    mate_bound = session.search(
        state,
        depth=3,
        alpha=-2068,
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    exact = session.search(
        state,
        depth=3,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )

    assert depth_two.status == mate_bound.status == exact.status == 0
    assert depth_two.score == -1762
    assert mate_bound.score == exact.score == -MATE_SCORE + 4
    assert mate_bound.principal_variation == ()
    assert tuple(
        item.machine_notation for item in exact.principal_variation
    ) == (
        "e8d7/h1c1/c1c2/c2a2/g8f6/e4f2/a2a4/a4a8/a8h8/h8e8",
        "e3f3/f3f4/f4f5/f5f6/f6g5/g5f6/f6g7/g7f6/f6g7/g7h7/h7g6",
        "c7c5/e8c8/f2d3/c8h8/c5c4/c4c3/c3c2/c2c1q/c1b1/d7e6/b1c2/c2g2",
    )


def test_canonical_mate_lower_bound_preserves_exact_pv() -> None:
    state = ProgressiveState.from_fen(
        "4k1nB/2pB3p/8/8/P3n3/4K3/2P2P1P/7r b - - 1 21",
        10,
    )
    full_window = (-2 * MATE_SCORE, 2 * MATE_SCORE)
    cold = _session(
        width=32,
        depth=5,
        max_work=25_000_000,
        cache_capacity=65_536,
        root_tactical_protection=True,
    ).search(
        state,
        depth=3,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert cold.status == 0
    assert cold.score == -MATE_SCORE + 4
    assert cold.principal_variation

    warm = _session(
        width=32,
        depth=5,
        max_work=25_000_000,
        cache_capacity=65_536,
        root_tactical_protection=True,
    )
    canonical_lower = warm.search(
        state,
        depth=3,
        alpha=cold.score - 1,
        beta=cold.score,
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert canonical_lower.status == 0
    assert canonical_lower.score >= cold.score
    assert canonical_lower.principal_variation

    exact = warm.search(
        state,
        depth=3,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )

    assert exact.status == 0
    assert exact.message == ""
    assert exact.score == cold.score
    assert exact.proof_bounds == cold.proof_bounds
    assert tuple(
        _series_signature(item) for item in exact.principal_variation
    ) == tuple(_series_signature(item) for item in cold.principal_variation)


def test_interrupted_warm_mate_bound_recertification_retries_canonically() -> None:
    state = ProgressiveState.from_fen(
        "4k1nB/2pB3p/8/8/P3n3/4K3/2P2P1P/7r b - - 1 21",
        10,
    )
    session = _session(
        width=32,
        depth=5,
        max_work=25_000_000,
        cache_capacity=65_536,
        root_tactical_protection=True,
    )
    full_window = (-2 * MATE_SCORE, 2 * MATE_SCORE)
    session.search(
        state,
        depth=2,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    bound = session.search(
        state,
        depth=3,
        alpha=-2068,
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    work_before = dict(
        zip(SUBTREE_STAT_FIELDS, bound.stats, strict=True)
    )["generation_positions"]

    interrupted = session.search(
        state,
        depth=3,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=1_000_000_000,
    )
    retry = session.search(
        state,
        depth=3,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    cold = _session(
        width=32,
        depth=5,
        max_work=25_000_000,
        cache_capacity=65_536,
        root_tactical_protection=True,
    ).search(
        state,
        depth=3,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )

    interrupted_stats = dict(
        zip(SUBTREE_STAT_FIELDS, interrupted.stats, strict=True)
    )
    assert interrupted.status == 2
    assert interrupted.principal_variation == ()
    assert interrupted_stats["generation_positions"] > work_before
    assert retry.status == cold.status == 0
    assert retry.score == cold.score == -MATE_SCORE + 4
    assert retry.proof_bounds == cold.proof_bounds
    assert tuple(
        _series_signature(item) for item in retry.principal_variation
    ) == tuple(_series_signature(item) for item in cold.principal_variation)


def test_warm_mate_bound_recertification_work_limit_fails_closed() -> None:
    state = ProgressiveState.from_fen(
        "4k1nB/2pB3p/8/8/P3n3/4K3/2P2P1P/7r b - - 1 21",
        10,
    )
    session = _session(
        width=32,
        depth=5,
        max_work=1_000_000,
        cache_capacity=65_536,
        root_tactical_protection=True,
    )
    full_window = (-2 * MATE_SCORE, 2 * MATE_SCORE)
    session.search(
        state,
        depth=2,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    bound = session.search(
        state,
        depth=3,
        alpha=-2068,
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    interrupted = session.search(
        state,
        depth=3,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )

    assert bound.status == 0
    assert bound.score == -MATE_SCORE + 4
    assert bound.principal_variation == ()
    assert interrupted.status == 1
    assert interrupted.score == 0
    assert interrupted.principal_variation == ()
    assert interrupted.message == "native subtree generation work limit reached"
    interrupted_stats = dict(
        zip(SUBTREE_STAT_FIELDS, interrupted.stats, strict=True)
    )
    assert interrupted_stats["generation_positions"] == 1_000_000


def test_warm_mate_bound_recertification_respects_outer_tt_rollback() -> None:
    state = ProgressiveState.from_fen(
        "4k1nB/2pB3p/8/8/P3n3/4K3/2P2P1P/7r b - - 1 21",
        10,
    )
    session = _session(
        width=32,
        depth=5,
        max_work=25_000_000,
        cache_capacity=65_536,
        root_tactical_protection=True,
    )
    full_window = (-2 * MATE_SCORE, 2 * MATE_SCORE)
    session.search(
        state,
        depth=2,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    session.search(
        state,
        depth=3,
        alpha=-2068,
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )

    session.begin_transaction()
    transactional = session.search(
        state,
        depth=3,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    rolled_back = session.rollback_transaction()
    replayed = session.search(
        state,
        depth=3,
        alpha=full_window[0],
        beta=full_window[1],
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )

    assert transactional.status == replayed.status == 0
    assert transactional.score == replayed.score == -MATE_SCORE + 4
    assert transactional.proof_bounds == replayed.proof_bounds
    assert tuple(
        _series_signature(item) for item in transactional.principal_variation
    ) == tuple(
        _series_signature(item) for item in replayed.principal_variation
    )
    assert rolled_back > 0


def test_warm_mate_bound_recertification_is_native_thread_deterministic() -> None:
    state = ProgressiveState.from_fen(
        "4k1nB/2pB3p/8/8/P3n3/4K3/2P2P1P/7r b - - 1 21",
        10,
    )
    full_window = (-2 * MATE_SCORE, 2 * MATE_SCORE)
    results = []
    for native_threads in (1, 16):
        session = _session(
            width=32,
            depth=5,
            max_work=25_000_000,
            cache_capacity=65_536,
            root_tactical_protection=True,
            native_threads=native_threads,
        )
        session.search(
            state,
            depth=2,
            alpha=full_window[0],
            beta=full_window[1],
            ply_from_root=1,
            external_work=0,
            remaining_nanoseconds=None,
        )
        session.search(
            state,
            depth=3,
            alpha=-2068,
            beta=full_window[1],
            ply_from_root=1,
            external_work=0,
            remaining_nanoseconds=None,
        )
        results.append(
            session.search(
                state,
                depth=3,
                alpha=full_window[0],
                beta=full_window[1],
                ply_from_root=1,
                external_work=0,
                remaining_nanoseconds=None,
            )
        )

    serial, parallel = results
    assert serial.status == parallel.status == 0
    assert serial.score == parallel.score == -MATE_SCORE + 4
    assert serial.proof_bounds == parallel.proof_bounds
    assert tuple(
        _series_signature(item) for item in serial.principal_variation
    ) == tuple(
        _series_signature(item) for item in parallel.principal_variation
    )
    assert dict(zip(SUBTREE_STAT_FIELDS, serial.stats, strict=True))[
        "generation_positions"
    ] == dict(zip(SUBTREE_STAT_FIELDS, parallel.stats, strict=True))[
        "generation_positions"
    ]


@pytest.mark.parametrize(
    ("fen", "series", "alpha", "beta", "score", "proof", "seed_work"),
    (
        (
            "3k2K1/Q7/8/8/8/8/8/8 w - - 0 1",
            3,
            0,
            1,
            MATE_SCORE - 1,
            (1, 1),
            134,
        ),
        (
            "6K1/1q6/8/8/8/3k4/8/8 b - - 0 1",
            4,
            -1,
            0,
            -MATE_SCORE + 1,
            (-1, -1),
            789,
        ),
    ),
)
def test_mover_mate_bound_hint_skips_deeper_scout_generation(
    fen: str,
    series: int,
    alpha: int,
    beta: int,
    score: int,
    proof: tuple[int, int],
    seed_work: int,
) -> None:
    state = ProgressiveState.from_fen(fen, series)
    results = []
    for native_threads in (1, 4):
        session = _session(
            width=128,
            depth=5,
            max_work=20_000,
            native_threads=native_threads,
        )
        seeded = session.search(
            state,
            depth=1,
            alpha=alpha,
            beta=beta,
            ply_from_root=0,
            external_work=0,
            remaining_nanoseconds=None,
        )
        deepened = session.search(
            state,
            depth=2,
            alpha=alpha,
            beta=beta,
            ply_from_root=0,
            external_work=0,
            remaining_nanoseconds=None,
        )
        warm_exact = session.search(
            state,
            depth=2,
            alpha=-2 * MATE_SCORE,
            beta=2 * MATE_SCORE,
            ply_from_root=0,
            external_work=0,
            remaining_nanoseconds=None,
        )
        cold_exact = _session(
            width=128,
            depth=5,
            max_work=20_000,
            native_threads=native_threads,
        ).search(
            state,
            depth=2,
            alpha=-2 * MATE_SCORE,
            beta=2 * MATE_SCORE,
            ply_from_root=0,
            external_work=0,
            remaining_nanoseconds=None,
        )
        retry_cap = seed_work * 4 + 500
        retry_session = _session(
            width=128,
            depth=5,
            max_work=retry_cap,
            native_threads=native_threads,
        )
        retry_session.search(
            state,
            depth=1,
            alpha=alpha,
            beta=beta,
            ply_from_root=0,
            external_work=0,
            remaining_nanoseconds=None,
        )
        retry_session.search(
            state,
            depth=2,
            alpha=alpha,
            beta=beta,
            ply_from_root=0,
            external_work=0,
            remaining_nanoseconds=None,
        )
        interrupted = retry_session.search(
            state,
            depth=2,
            alpha=-2 * MATE_SCORE,
            beta=2 * MATE_SCORE,
            ply_from_root=0,
            external_work=retry_cap - seed_work - 100,
            remaining_nanoseconds=None,
        )
        retried = retry_session.search(
            state,
            depth=2,
            alpha=-2 * MATE_SCORE,
            beta=2 * MATE_SCORE,
            ply_from_root=0,
            external_work=0,
            remaining_nanoseconds=None,
        )
        results.append(
            (
                seeded,
                deepened,
                warm_exact,
                cold_exact,
                interrupted,
                retried,
            )
        )

    assert results[0] == results[1]
    for (
        seeded,
        deepened,
        warm_exact,
        cold_exact,
        interrupted,
        retried,
    ) in results:
        seeded_stats = dict(zip(SUBTREE_STAT_FIELDS, seeded.stats, strict=True))
        deepened_stats = dict(zip(SUBTREE_STAT_FIELDS, deepened.stats, strict=True))
        assert seeded.status == deepened.status == 0
        assert seeded.score == deepened.score == score
        assert seeded.proof_bounds == deepened.proof_bounds == proof
        assert seeded.principal_variation == deepened.principal_variation == ()
        assert seeded_stats["generation_positions"] == seed_work
        assert deepened_stats["generation_positions"] == seed_work
        assert warm_exact.status == cold_exact.status == 0
        assert warm_exact.score == cold_exact.score == score
        assert warm_exact.proof_bounds == cold_exact.proof_bounds == proof
        assert warm_exact.principal_variation == cold_exact.principal_variation
        assert len(warm_exact.principal_variation) == 1
        assert interrupted.status == 1
        assert interrupted.principal_variation == ()
        assert retried.status == cold_exact.status == 0
        assert retried.score == cold_exact.score
        assert retried.proof_bounds == cold_exact.proof_bounds
        assert retried.principal_variation == cold_exact.principal_variation


def test_mover_mate_exit_preserves_capped_parallel_search_result() -> None:
    capped_exact = ProgressiveState.from_fen(
        "5k2/8/3K4/1r3b2/8/1NQ5/8/8 w - - 0 1",
        1,
    )
    results = []
    for native_threads in (1, 4):
        results.append(
            _session(
                width=8,
                depth=5,
                max_work=5_231,
                native_threads=native_threads,
                root_tactical_protection=True,
            ).search(
                capped_exact,
                depth=3,
                alpha=-2 * MATE_SCORE,
                beta=2 * MATE_SCORE,
                ply_from_root=0,
                external_work=0,
                remaining_nanoseconds=None,
            )
        )

    serial, parallel = results
    assert serial == parallel
    assert serial.status == 0
    assert serial.score == 1_367
    assert tuple(
        item.machine_notation for item in serial.principal_variation
    ) == ("c3f6", "f8g8/b5b3", "f6f5/f5c2/c2b3")
    assert not serial.evaluation_work_limit_reached
    stats = dict(zip(SUBTREE_STAT_FIELDS, serial.stats, strict=True))
    assert stats["generation_positions"] == 5_231
    assert stats["generation_work_limit_hits"] == 0

    one_below = _session(
        width=8,
        depth=5,
        max_work=5_230,
        native_threads=1,
        root_tactical_protection=True,
    ).search(
        capped_exact,
        depth=3,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert one_below.status == 0
    assert one_below.selective
    assert one_below.evaluation_work_limit_reached
    one_below_stats = dict(
        zip(SUBTREE_STAT_FIELDS, one_below.stats, strict=True)
    )
    assert one_below_stats["generation_positions"] == 5_230
    assert one_below_stats["generation_work_limit_hits"] == 1


def test_checkmated_mover_is_not_misclassified_as_delivering_mate() -> None:
    state = ProgressiveState.from_fen(
        "8/8/8/8/8/5k2/6q1/7K w - - 0 1",
        1,
    )
    result = _session(width=8, depth=5, max_work=1_000).search(
        state,
        depth=1,
        alpha=-1,
        beta=0,
        ply_from_root=0,
        external_work=0,
        remaining_nanoseconds=None,
    )

    assert result.status == 0
    assert result.score == -MATE_SCORE + 1
    assert result.proof_bounds == (-1, -1)
    assert len(result.principal_variation) == 1
    terminal = result.principal_variation[0]
    assert terminal.moves == ()
    assert terminal.outcome is Outcome.CHECKMATE
    assert not terminal.ended_by_check


def test_per_call_work_credit_exact_one_over_and_retry_are_fail_closed() -> None:
    root = ProgressiveState.initial()
    baseline = _session(width=4).enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    root_work = baseline.work.call_native_work
    assert root_work > 0

    exact_root = _session(width=4).enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        call_work_credit=root_work,
    )
    assert exact_root.status == 0
    assert exact_root.work.call_native_work == root_work
    assert exact_root.work.call_work_credit == root_work
    assert exact_root.enumeration_identity == baseline.enumeration_identity

    interrupted_root_session = _session(width=4)
    interrupted_root = interrupted_root_session.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        call_work_credit=root_work - 1,
    )
    assert interrupted_root.status == 1
    assert interrupted_root.work.call_native_work <= root_work - 1
    assert not interrupted_root.enumeration_identity
    retried_root = interrupted_root_session.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        call_work_credit=root_work,
    )
    assert retried_root.status == 0
    assert retried_root.enumeration_identity == baseline.enumeration_identity

    def imported_worker() -> tuple[NativeSubtreeSession, NativeRootEnumerationResult]:
        worker = _session(width=4)
        imported = worker.import_root(
            root,
            baseline,
            external_work=baseline.work.native_work_after,
            remaining_nanoseconds=None,
            call_work_credit=sum(
                len(item.series.moves) for item in baseline.candidates
            ),
        )
        assert imported.status == 0
        return worker, imported

    measuring_worker, measured_manifest = imported_worker()
    measured = measuring_worker.search_root_candidate(
        enumeration_identity=measured_manifest.enumeration_identity,
        candidate_identity=measured_manifest.candidates[0].candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=baseline.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=True,
    )
    assert measured.status == 0
    child_work = measured.work.call_native_work
    assert child_work > 0

    exact_worker, exact_manifest = imported_worker()
    exact = exact_worker.search_root_candidate(
        enumeration_identity=exact_manifest.enumeration_identity,
        candidate_identity=exact_manifest.candidates[0].candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=baseline.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=True,
        call_work_credit=child_work,
    )
    assert exact.status == 0
    assert exact.score == measured.score
    assert exact.work.call_native_work == child_work
    assert exact.work.call_work_credit == child_work

    interrupted_worker, interrupted_manifest = imported_worker()
    tt_before = interrupted_manifest.work.tt_entries
    interrupted = interrupted_worker.search_root_candidate(
        enumeration_identity=interrupted_manifest.enumeration_identity,
        candidate_identity=interrupted_manifest.candidates[0].candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=baseline.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=True,
        call_work_credit=child_work - 1,
    )
    assert interrupted.status == 1
    assert interrupted.bound is NativeSubtreeBound.UNKNOWN
    assert interrupted.work.call_native_work <= child_work - 1
    assert interrupted.work.tt_entries == tt_before

    retry = interrupted_worker.search_root_candidate(
        enumeration_identity=interrupted_manifest.enumeration_identity,
        candidate_identity=interrupted_manifest.candidates[0].candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=baseline.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=True,
        call_work_credit=child_work,
    )
    assert retry.status == 0
    assert retry.score == measured.score
    assert (
        retry.candidate_identity
        == interrupted_manifest.candidates[0].candidate_identity
    )
