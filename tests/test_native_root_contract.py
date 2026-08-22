from __future__ import annotations

from dataclasses import asdict, replace

import chess
import pytest

import scottish_progressive.evaluation as evaluation
import scottish_progressive.search as search_module
from scottish_progressive.model import ProgressiveState
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
    tt_capacity: int = 262_144,
    eval_capacity: int = 262_144,
) -> NativeSubtreeSession:
    _require_contract()
    return NativeSubtreeSession(
        max_series_per_node=width,
        max_work=max_work,
        requested_depth=depth,
        mate_score=MATE_SCORE,
        cache_capacity=16_384,
        external_cache_weight=0,
        native_threads=1,
        root_tactical_protection=False,
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
