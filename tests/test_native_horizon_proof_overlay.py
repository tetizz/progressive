from __future__ import annotations

from dataclasses import replace

import pytest

from scottish_progressive.model import ProgressiveState
from scottish_progressive.native_subtree import (
    NATIVE_MAX_HORIZON_PROOFS,
    NATIVE_MAX_HORIZON_PROOF_PATH,
    SUBTREE_STAT_FIELDS,
    NativeHorizonProof,
    NativeSubtreeBound,
    NativeSubtreeSession,
    native_subtree_available,
)
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.search import MATE_SCORE


ROOT = ProgressiveState.from_fen(
    "6k1/8/8/q7/8/8/5PPP/1R4K1 w - - 0 1",
    1,
)
CHECKED_ROOT = play_series(ROOT, ("b1b8",))
BLACK_MATE = play_series(CHECKED_ROOT.final_state, ("g8f7", "a5e1"))

BLACK_ROOT = ProgressiveState.from_fen(
    "1r4k1/5ppp/8/8/Q7/8/8/6K1 b - - 0 1",
    2,
)
CHECKED_BLACK_ROOT = play_series(BLACK_ROOT, ("f7f5", "b8b1"))
WHITE_MATE = play_series(CHECKED_BLACK_ROOT.final_state, ("g1f2", "a4e8"))

DEEP_ROOT = ProgressiveState.from_fen(
    "2k5/4pr2/8/8/3K3N/7R/2qP2b1/1B6 w - - 0 1",
    1,
)
_deep_cursor = DEEP_ROOT
_deep_results = []
for _deep_moves in (
    ("h4g2",),
    ("c2c7", "e7e5"),
    ("d4e4", "b1a2", "a2e6"),
    ("c7d7", "d7e6", "e6d6", "d6d4"),
):
    _deep_result = play_series(_deep_cursor, _deep_moves)
    _deep_results.append(_deep_result)
    _deep_cursor = _deep_result.final_state
DEEP_PROOF = NativeHorizonProof(
    rooted_path=tuple(_deep_results[:3]),
    mate_reply=_deep_results[3],
)


def _deep_root_and_proof(
    fullmove_number: int,
) -> tuple[ProgressiveState, NativeHorizonProof]:
    root = ProgressiveState.from_fen(
        f"2k5/4pr2/8/8/3K3N/7R/2qP2b1/1B6 w - - 0 {fullmove_number}",
        1,
    )
    cursor = root
    results = []
    for moves in (
        ("h4g2",),
        ("c2c7", "e7e5"),
        ("d4e4", "b1a2", "a2e6"),
        ("c7d7", "d7e6", "e6d6", "d6d4"),
    ):
        result = play_series(cursor, moves)
        results.append(result)
        cursor = result.final_state
    return root, NativeHorizonProof(
        rooted_path=tuple(results[:3]),
        mate_reply=results[3],
    )

_alternate_cursor = _deep_results[0].final_state
_alternate_results = [_deep_results[0]]
for _alternate_moves, _alternate_count in (
    (("c2a2", "a2a1"), 1),
    (("d4c4", "b1c2", "c2f5"), 9),
    (("c8b8", "e7e5", "f7b7", "a1d4"), 1),
):
    _alternate_result = play_series(
        _alternate_cursor,
        _alternate_moves,
    ).with_transposition_count(_alternate_count)
    _alternate_results.append(_alternate_result)
    _alternate_cursor = _alternate_result.final_state
ALTERNATE_DEEP_PROOF = NativeHorizonProof(
    rooted_path=tuple(_alternate_results[:3]),
    mate_reply=_alternate_results[3],
)


def _session() -> NativeSubtreeSession:
    if not native_subtree_available():
        pytest.skip("source-matched native retained-root contract is unavailable")
    return NativeSubtreeSession(
        max_series_per_node=4,
        max_work=2_000_000,
        requested_depth=1,
        mate_score=MATE_SCORE,
        cache_capacity=16_384,
        external_cache_weight=0,
        native_threads=1,
        root_tactical_protection=False,
        profile=baseline_profile(),
    )


def _deep_session(*, max_work: int = 2_000_000) -> NativeSubtreeSession:
    if not native_subtree_available():
        pytest.skip("source-matched native retained-root contract is unavailable")
    return NativeSubtreeSession(
        max_series_per_node=4,
        max_work=max_work,
        requested_depth=3,
        mate_score=MATE_SCORE,
        cache_capacity=16_384,
        external_cache_weight=0,
        native_threads=1,
        root_tactical_protection=False,
        profile=baseline_profile(),
    )


def _deep_manifest_and_candidate(session: NativeSubtreeSession) -> tuple[object, object]:
    manifest = session.enumerate_root(
        DEEP_ROOT,
        preferred_series="h4g2",
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = next(
        item for item in manifest.candidates if item.order_key == "h4g2"
    )
    return manifest, candidate


def _deep_search(
    session: NativeSubtreeSession,
    manifest: object,
    candidate: object,
    *,
    external_work: int,
    proofs: tuple[NativeHorizonProof, ...] = (),
    rollback_tt: bool = False,
    call_work_credit: int | None = None,
) -> object:
    return session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=2,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=external_work,
        remaining_nanoseconds=None,
        rollback_tt=rollback_tt,
        call_work_credit=call_work_credit,
        horizon_proofs=proofs,
    )


def test_exact_checked_horizon_proof_substitutes_the_reply_mate() -> None:
    session = _session()
    manifest = session.enumerate_root(
        ROOT,
        preferred_series="b1b8",
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = next(
        item for item in manifest.candidates if item.order_key == "b1b8"
    )

    result = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
        horizon_proofs=(
            NativeHorizonProof(
                rooted_path=(CHECKED_ROOT,),
                mate_reply=BLACK_MATE,
            ),
        ),
    )

    assert result.status == 0
    assert result.bound is NativeSubtreeBound.EXACT
    assert result.score == -MATE_SCORE + 2
    assert tuple(item.moves for item in result.principal_variation) == (
        ("b1b8",),
        ("g8f7", "a5e1"),
    )
    assert result.proof_bounds == (-1, -1)
    assert result.horizon_proofs_validated == 1
    assert result.horizon_proof_hits == 1
    assert result.horizon_proof_hit_mask == 0b1
    assert result.horizon_proof_set_identity.startswith("spc-horizon-proof-set-v1|")
    assert result.work.call_native_work == 3


def test_no_proof_search_preserves_the_pre_overlay_result_and_work() -> None:
    session = _session()
    manifest = session.enumerate_root(
        ROOT,
        preferred_series="b1b8",
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = next(
        item for item in manifest.candidates if item.order_key == "b1b8"
    )

    result = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )

    # Literals captured from the source-matched pre-overlay native binary.
    assert result.status == 0
    assert result.bound is NativeSubtreeBound.EXACT
    assert result.score == -353
    assert tuple(item.moves for item in result.principal_variation) == (("b1b8",),)
    assert result.proof_bounds == (-1, 1)
    assert result.work.call_native_work == 5
    assert result.horizon_proof_set_identity == ""
    assert result.horizon_proofs_validated == 0
    assert result.horizon_proof_hits == 0
    assert result.horizon_proof_hit_mask == 0


def test_ordinary_search_keeps_realistic_large_root_relative_ply_compatible() -> None:
    session = _session()
    result = session.search(
        ROOT,
        depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        ply_from_root=32,
        external_work=0,
        remaining_nanoseconds=None,
    )

    assert result.status == 0


def test_forged_proof_fails_before_proof_or_tt_state_is_installed() -> None:
    session = _session()
    manifest = session.enumerate_root(
        ROOT,
        preferred_series="b1b8",
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = next(
        item for item in manifest.candidates if item.order_key == "b1b8"
    )
    forged = replace(BLACK_MATE, final_state=CHECKED_ROOT.final_state)

    rejected = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
        horizon_proofs=(
            NativeHorizonProof(
                rooted_path=(CHECKED_ROOT,),
                mate_reply=forged,
            ),
        ),
    )

    assert rejected.status == 4
    assert rejected.bound is NativeSubtreeBound.UNKNOWN
    assert rejected.child_principal_variation == ()
    assert rejected.work.tt_entries == manifest.work.tt_entries
    assert rejected.horizon_proof_set_identity == ""
    assert rejected.horizon_proofs_validated == 0
    assert rejected.horizon_proof_hits == 0
    assert rejected.horizon_proof_hit_mask == 0

    retry = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=rejected.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
    )
    assert retry.status == 0
    assert retry.score == -353
    assert tuple(item.moves for item in retry.principal_variation) == (("b1b8",),)


def test_proof_changes_ancestor_choice_and_isolates_tt_in_both_call_orders() -> None:
    warm = _deep_session()
    warm_manifest, warm_candidate = _deep_manifest_and_candidate(warm)
    baseline = _deep_search(
        warm,
        warm_manifest,
        warm_candidate,
        external_work=warm_manifest.work.native_work_after,
    )
    overlay = _deep_search(
        warm,
        warm_manifest,
        warm_candidate,
        external_work=baseline.work.native_work_after,
        proofs=(DEEP_PROOF,),
    )

    assert baseline.score == 336
    assert tuple(item.machine_notation for item in baseline.principal_variation) == (
        "h4g2",
        "c2c7/e7e5",
        "d4e4/b1a2/a2e6",
    )
    assert overlay.score == 179
    assert tuple(item.machine_notation for item in overlay.principal_variation) == (
        "h4g2",
        "c2c7/e7e5",
        "d4d3/d3e2/h3h8",
    )
    assert overlay.horizon_proofs_validated == 1
    assert overlay.horizon_proof_hits == 1
    assert overlay.horizon_proof_hit_mask == 0b1
    assert overlay.work.tt_entries > baseline.work.tt_entries
    assert overlay.work.call_stats[SUBTREE_STAT_FIELDS.index("tt_hits")] == 0

    reused = _deep_search(
        warm,
        warm_manifest,
        warm_candidate,
        external_work=overlay.work.native_work_after,
        proofs=(DEEP_PROOF,),
    )
    ordinary_again = _deep_search(
        warm,
        warm_manifest,
        warm_candidate,
        external_work=reused.work.native_work_after,
    )
    assert reused.horizon_proof_set_identity == overlay.horizon_proof_set_identity
    assert reused.score == overlay.score
    assert reused.principal_variation == overlay.principal_variation
    assert reused.horizon_proof_hits == 0
    assert reused.horizon_proof_hit_mask == 0
    assert reused.work.call_stats[SUBTREE_STAT_FIELDS.index("tt_hits")] == 1
    assert ordinary_again.score == baseline.score
    assert ordinary_again.principal_variation == baseline.principal_variation
    assert ordinary_again.work.call_native_work == 0
    assert ordinary_again.work.call_stats[SUBTREE_STAT_FIELDS.index("tt_hits")] == 1

    cold = _deep_session()
    cold_manifest, cold_candidate = _deep_manifest_and_candidate(cold)
    proof_first = _deep_search(
        cold,
        cold_manifest,
        cold_candidate,
        external_work=cold_manifest.work.native_work_after,
        proofs=(DEEP_PROOF,),
    )
    ordinary_after = _deep_search(
        cold,
        cold_manifest,
        cold_candidate,
        external_work=proof_first.work.native_work_after,
    )
    assert proof_first.horizon_proof_set_identity == overlay.horizon_proof_set_identity
    assert proof_first.score == overlay.score
    assert proof_first.principal_variation == overlay.principal_variation
    assert proof_first.horizon_proof_hits == 1
    assert proof_first.horizon_proof_hit_mask == 0b1
    assert ordinary_after.score == baseline.score
    assert ordinary_after.principal_variation == baseline.principal_variation
    assert ordinary_after.horizon_proof_set_identity == ""
    assert ordinary_after.work.tt_entries > proof_first.work.tt_entries


def test_public_proof_limits_and_mate_reply_transport_are_canonical() -> None:
    assert NATIVE_MAX_HORIZON_PROOFS == 16
    assert NATIVE_MAX_HORIZON_PROOF_PATH == 8

    proof = NativeHorizonProof(
        rooted_path=(CHECKED_ROOT.with_transposition_count(7),),
        mate_reply=BLACK_MATE.with_transposition_count(999),
    )
    rooted_path, mate_reply = proof.transport()
    assert rooted_path[0][1] == 7
    assert mate_reply[1] == 1


def test_proof_validation_obeys_call_credit_and_can_retry_cleanly() -> None:
    session = _session()
    manifest = session.enumerate_root(
        ROOT,
        preferred_series="b1b8",
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = next(
        item for item in manifest.candidates if item.order_key == "b1b8"
    )
    proof = NativeHorizonProof(
        rooted_path=(CHECKED_ROOT,),
        mate_reply=BLACK_MATE,
    )

    exhausted = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
        call_work_credit=2,
        horizon_proofs=(proof,),
    )
    assert exhausted.status == 1
    assert exhausted.work.call_native_work == 2
    assert exhausted.work.tt_entries == manifest.work.tt_entries
    assert exhausted.horizon_proof_set_identity == ""
    assert exhausted.horizon_proofs_validated == 0
    assert exhausted.horizon_proof_hits == 0
    assert exhausted.horizon_proof_hit_mask == 0

    retry = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=exhausted.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
        call_work_credit=3,
        horizon_proofs=(proof,),
    )
    assert retry.status == 0
    assert retry.work.call_native_work == 3
    assert retry.horizon_proof_hit_mask == 0b1


def test_interrupted_proof_search_cannot_poison_an_exact_retry() -> None:
    session = _deep_session()
    manifest, candidate = _deep_manifest_and_candidate(session)
    stopped = _deep_search(
        session,
        manifest,
        candidate,
        external_work=manifest.work.native_work_after,
        proofs=(DEEP_PROOF,),
        call_work_credit=300,
    )

    assert stopped.status == 1
    assert stopped.work.call_native_work == 300
    assert stopped.work.tt_entries == 0
    assert stopped.tt_writes_rolled_back > 0
    assert stopped.horizon_proof_set_identity == ""

    retry = _deep_search(
        session,
        manifest,
        candidate,
        external_work=stopped.work.native_work_after,
        proofs=(DEEP_PROOF,),
    )
    assert retry.status == 0
    assert retry.bound is NativeSubtreeBound.EXACT
    assert retry.score == 179
    assert tuple(item.machine_notation for item in retry.principal_variation) == (
        "h4g2",
        "c2c7/e7e5",
        "d4d3/d3e2/h3h8",
    )
    assert retry.horizon_proof_hits == 1
    assert retry.horizon_proof_hit_mask == 0b1


@pytest.mark.parametrize("rollback_tt", [False, True])
def test_interrupted_ordinary_search_cannot_poison_an_exact_retry(
    rollback_tt: bool,
) -> None:
    session = _deep_session()
    manifest, candidate = _deep_manifest_and_candidate(session)
    stopped = _deep_search(
        session,
        manifest,
        candidate,
        external_work=manifest.work.native_work_after,
        rollback_tt=rollback_tt,
        call_work_credit=300,
    )

    assert stopped.status == 1
    assert stopped.work.call_native_work == 300
    assert stopped.work.tt_entries == 0
    assert stopped.tt_writes_rolled_back > 0

    retry = _deep_search(
        session,
        manifest,
        candidate,
        external_work=stopped.work.native_work_after,
    )
    assert retry.status == 0
    assert retry.bound is NativeSubtreeBound.EXACT
    assert retry.score == 336
    assert tuple(item.machine_notation for item in retry.principal_variation) == (
        "h4g2",
        "c2c7/e7e5",
        "d4e4/b1a2/a2e6",
    )


def test_failed_distinct_proof_sets_do_not_exhaust_session_capacity() -> None:
    session = _deep_session()
    manifest, candidate = _deep_manifest_and_candidate(session)
    external_work = manifest.work.native_work_after

    for index in range(300):
        rooted_path = list(DEEP_PROOF.rooted_path)
        rooted_path[1] = rooted_path[1].with_transposition_count(1_000 + index)
        stopped = _deep_search(
            session,
            manifest,
            candidate,
            external_work=external_work,
            proofs=(
                NativeHorizonProof(
                    rooted_path=tuple(rooted_path),
                    mate_reply=DEEP_PROOF.mate_reply,
                ),
            ),
            call_work_credit=10,
        )
        assert stopped.status == 1
        assert stopped.work.call_native_work == 10
        assert stopped.horizon_proof_set_identity == ""
        external_work = stopped.work.native_work_after

    retry = _deep_search(
        session,
        manifest,
        candidate,
        external_work=external_work,
        proofs=(DEEP_PROOF,),
    )
    assert retry.status == 0
    assert retry.score == 179
    assert retry.horizon_proof_hit_mask == 0b1


def test_descendant_path_counts_cannot_manufacture_proof_namespaces() -> None:
    session = _deep_session()
    manifest, candidate = _deep_manifest_and_candidate(session)
    external_work = manifest.work.native_work_after
    expected_identity = ""
    expected_tt_entries = 0

    for index in range(300):
        rooted_path = list(DEEP_PROOF.rooted_path)
        rooted_path[1] = rooted_path[1].with_transposition_count(10_000 + index)
        result = _deep_search(
            session,
            manifest,
            candidate,
            external_work=external_work,
            proofs=(
                NativeHorizonProof(
                    rooted_path=tuple(rooted_path),
                    mate_reply=DEEP_PROOF.mate_reply,
                ),
            ),
        )
        assert result.status == 0
        assert result.score == 179
        if index == 0:
            expected_identity = result.horizon_proof_set_identity
            expected_tt_entries = result.work.tt_entries
        else:
            assert result.horizon_proof_set_identity == expected_identity
            assert result.work.tt_entries == expected_tt_entries
        external_work = result.work.native_work_after


def test_genuine_257th_proof_set_reclaims_a_namespace_without_aliasing() -> None:
    session = _deep_session(max_work=1_000_000_000)
    external_work = 0
    first_identity = ""
    first_root: ProgressiveState | None = None
    first_proof: NativeHorizonProof | None = None

    for fullmove_number in range(1, 258):
        root, proof = _deep_root_and_proof(fullmove_number)
        manifest = session.enumerate_root(
            root,
            preferred_series="h4g2",
            external_work=external_work,
            remaining_nanoseconds=None,
        )
        assert manifest.status == 0, (
            fullmove_number,
            manifest.status,
            manifest.message,
        )
        candidate = next(
            item for item in manifest.candidates if item.order_key == "h4g2"
        )
        result = _deep_search(
            session,
            manifest,
            candidate,
            external_work=manifest.work.native_work_after,
            proofs=(proof,),
        )
        assert result.status == 0
        assert result.score == 179
        assert result.horizon_proof_hits == 1
        external_work = result.work.native_work_after
        if fullmove_number == 1:
            first_identity = result.horizon_proof_set_identity
            first_root = root
            first_proof = proof

    assert first_root is not None
    assert first_proof is not None
    revisit_manifest = session.enumerate_root(
        first_root,
        preferred_series="h4g2",
        external_work=external_work,
        remaining_nanoseconds=None,
    )
    revisit_candidate = next(
        item
        for item in revisit_manifest.candidates
        if item.order_key == "h4g2"
    )
    revisit = _deep_search(
        session,
        revisit_manifest,
        revisit_candidate,
        external_work=revisit_manifest.work.native_work_after,
        proofs=(first_proof,),
    )
    assert revisit.status == 0
    assert revisit.score == 179
    assert revisit.horizon_proof_hits == 1
    assert revisit.horizon_proof_set_identity == first_identity


def test_black_root_proof_uses_white_relative_mate_score_and_bounds() -> None:
    session = _session()
    manifest = session.enumerate_root(
        BLACK_ROOT,
        preferred_series="f7f5/b8b1",
        external_work=0,
        remaining_nanoseconds=None,
    )
    candidate = next(
        item for item in manifest.candidates if item.order_key == "f7f5/b8b1"
    )
    result = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=manifest.work.native_work_after,
        remaining_nanoseconds=None,
        rollback_tt=False,
        horizon_proofs=(
            NativeHorizonProof(
                rooted_path=(CHECKED_BLACK_ROOT,),
                mate_reply=WHITE_MATE,
            ),
        ),
    )

    assert result.status == 0
    assert result.score == MATE_SCORE - 2
    assert result.proof_bounds == (1, 1)
    assert tuple(item.moves for item in result.principal_variation) == (
        ("f7f5", "b8b1"),
        ("g1f2", "a4e8"),
    )
    assert result.horizon_proof_hit_mask == 0b1


def test_superset_receipt_uses_request_order_while_identity_is_order_independent() -> None:
    appended = _deep_session()
    appended_manifest, appended_candidate = _deep_manifest_and_candidate(appended)
    old_only = _deep_search(
        appended,
        appended_manifest,
        appended_candidate,
        external_work=appended_manifest.work.native_work_after,
        proofs=(ALTERNATE_DEEP_PROOF,),
    )
    superset = _deep_search(
        appended,
        appended_manifest,
        appended_candidate,
        external_work=old_only.work.native_work_after,
        proofs=(ALTERNATE_DEEP_PROOF, DEEP_PROOF),
    )

    assert old_only.horizon_proof_hit_mask == 0
    assert superset.horizon_proofs_validated == 2
    assert superset.horizon_proof_hit_mask == 0b10

    reversed_session = _deep_session()
    reversed_manifest, reversed_candidate = _deep_manifest_and_candidate(
        reversed_session
    )
    reversed_superset = _deep_search(
        reversed_session,
        reversed_manifest,
        reversed_candidate,
        external_work=reversed_manifest.work.native_work_after,
        proofs=(DEEP_PROOF, ALTERNATE_DEEP_PROOF),
    )
    assert (
        reversed_superset.horizon_proof_set_identity
        == superset.horizon_proof_set_identity
    )
    assert reversed_superset.score == superset.score
    assert reversed_superset.principal_variation == superset.principal_variation
    assert reversed_superset.horizon_proof_hit_mask == 0b01
