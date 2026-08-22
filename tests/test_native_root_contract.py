from __future__ import annotations

from dataclasses import asdict, replace

import chess
import pytest

import scottish_progressive.evaluation as evaluation
import scottish_progressive.search as search_module
from scottish_progressive.model import Outcome, ProgressiveState
from scottish_progressive.native_subtree import (
    SUBTREE_STAT_FIELDS,
    NativeRootEnumerationResult,
    NativeSubtreeBound,
    NativeSubtreeSession,
    native_subtree_available,
)
from scottish_progressive.profiles import baseline_profile
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
    assert actual.enumeration_identity.startswith("spc-root-enumeration-v1|")
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
        child_depth=3,
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
        child_depth=3,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )

    assert seeded.status == deepened.status == expected.status == 0
    # This fixture grows the table from 9 to 25 entries on the current exact
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
    assert "|root-policycanonical-boundary-v1|root-tactical0" in (
        early_manifest.enumeration_identity
    )
    assert "|root-policycanonical-boundary-v1|root-tactical1" in (
        late_manifest.enumeration_identity
    )


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
    assert capped.work.eval_entries == capped.work.eval_entries_peak == 1
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
    assert tt_capped.work.tt_entries == tt_capped.work.tt_entries_peak == 1
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
    assert first.score == -666
    assert first.child_principal_variation
    assert first.child_principal_variation[0].machine_notation == "e7e5/f8b4"
    assert first.child_principal_variation[0].outcome is None
    assert hinted.child_principal_variation == ()
    assert hinted.score == first.score
    assert hinted.proof_bounds == first.proof_bounds
    assert hinted.root_series == first.root_series
    assert first.work.call_native_work == 139
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
    assert scout.work.call_native_work == 16_273
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
    assert seeded.work.call_native_work == 16_273
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
            152,
        ),
        (
            "6K1/1q6/8/8/8/3k4/8/8 b - - 0 1",
            4,
            -1,
            0,
            -MATE_SCORE + 1,
            (-1, -1),
            988,
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
            152,
        ),
        (
            "6K1/1q6/8/8/8/3k4/8/8 b - - 0 1",
            4,
            -1,
            0,
            -MATE_SCORE + 1,
            (-1, -1),
            988,
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
                max_work=4_956,
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
    assert serial.score == 1_627
    assert tuple(
        item.machine_notation for item in serial.principal_variation
    ) == ("c3f6", "f8g8/b5b3", "f6f5/f5c2/c2b3")
    assert not serial.evaluation_work_limit_reached
    stats = dict(zip(SUBTREE_STAT_FIELDS, serial.stats, strict=True))
    assert stats["generation_positions"] == 4_956
    assert stats["generation_work_limit_hits"] == 0


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
        call_work_credit=child_work + 1,
    )
    assert exact.status == 0
    assert exact.score == measured.score
    assert exact.work.call_native_work == child_work
    assert exact.work.call_work_credit == child_work + 1

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
        call_work_credit=child_work,
    )
    assert interrupted.status == 1
    assert interrupted.bound is NativeSubtreeBound.UNKNOWN
    assert interrupted.work.call_native_work <= child_work
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
        call_work_credit=child_work + 1,
    )
    assert retry.status == 0
    assert retry.score == measured.score
    assert (
        retry.candidate_identity
        == interrupted_manifest.candidates[0].candidate_identity
    )
