from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import chess

import scottish_progressive.evaluation as evaluation
from scottish_progressive.model import ProgressiveState, SeriesResult
from scottish_progressive.native_subtree import (
    SUBTREE_STAT_FIELDS,
    NativeDeepTeacherValueModel,
    NativeRootCandidateResult,
    NativeRootEnumerationResult,
    NativeRetainedRootCandidate,
    NativeSubtreeSession,
    NativeSubtreeWorkReceipt,
    native_subtree_available,
)
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.search import MATE_SCORE


ROOT = Path(__file__).resolve().parents[1]
NODE_GATE = ROOT / "benchmarks" / "verify_native_root_session_wasm.mjs"
NEURAL_S2_ORDER_POLICY = "root-order-s3-neural-model1-blend75"


def _work(receipt: NativeSubtreeWorkReceipt) -> dict[str, Any]:
    return {
        "call_work_credit": receipt.call_work_credit,
        "external_work": receipt.external_work,
        "native_work_before": receipt.native_work_before,
        "native_work_after": receipt.native_work_after,
        "call_native_work": receipt.call_native_work,
        "total_accounted_work": receipt.total_accounted_work,
        "tt_entries": receipt.tt_entries,
        "tt_entries_peak": receipt.tt_entries_peak,
        "tt_capacity": receipt.tt_capacity,
        "eval_entries": receipt.eval_entries,
        "eval_entries_peak": receipt.eval_entries_peak,
        "eval_capacity": receipt.eval_capacity,
        "series_cache_capacity": 16_384,
        "series_cache_weight_peak": dict(
            zip(SUBTREE_STAT_FIELDS, receipt.cumulative_stats, strict=True)
        )["series_generation_cache_peak"],
        "series_cache_entries_peak": dict(
            zip(SUBTREE_STAT_FIELDS, receipt.cumulative_stats, strict=True)
        )["series_generation_cache_entries_peak"],
        "call_stats": dict(
            zip(SUBTREE_STAT_FIELDS, receipt.call_stats, strict=True)
        ),
        "cumulative_stats": dict(
            zip(SUBTREE_STAT_FIELDS, receipt.cumulative_stats, strict=True)
        ),
    }


def _boundary(series: SeriesResult) -> dict[str, Any]:
    state = series.final_state
    return {
        "fen": state.board.fen(en_passant="fen"),
        "board_fen": state.board.fen(en_passant="fen"),
        "series": state.series_number,
        "series_number": state.series_number,
        "side_to_move": "white" if state.board.turn else "black",
        "quiet_series": state.quiet_series,
        "quiet_draw_pending": state.quiet_draw_pending,
        "ep_targets": [chess.square_name(square) for square in state.ep_targets],
        "progressive_ep": [
            chess.square_name(square) for square in state.ep_targets
        ],
        "promoted_hex": f"{state.board.promoted:016x}",
        "chess960": False,
    }


def _series(series: SeriesResult) -> dict[str, Any]:
    return {
        "moves": list(series.moves),
        "machine_notation": series.machine_notation,
        "transposition_count": series.transposition_count,
        "child_boundary": _boundary(series),
        "outcome": (
            None
            if series.outcome is None
            else series.outcome.value.replace("ten-series-draw", "ten_series_draw")
        ),
        "ended_by_check": series.ended_by_check,
    }


def _candidate(candidate: NativeRetainedRootCandidate) -> dict[str, Any]:
    return {
        "candidate_identity": candidate.candidate_identity,
        "order_index": candidate.order_index,
        "order_key": candidate.order_key,
        "terminal_score": candidate.terminal_score,
        "terminal_proof_bounds": list(candidate.terminal_proof_bounds),
        "root_series": _series(candidate.series),
    }


def _manifest(manifest: NativeRootEnumerationResult) -> dict[str, Any]:
    return {
        "enumeration_identity": manifest.enumeration_identity,
        "root_white_to_move": manifest.root_white_to_move,
        "requested_width": manifest.requested_width,
        "retained_count": manifest.retained_count,
        "width_complete": manifest.width_complete,
        "preferred_series": list(manifest.preferred_series),
        "candidates": [_candidate(candidate) for candidate in manifest.candidates],
    }


def _candidate_result(result: NativeRootCandidateResult) -> dict[str, Any]:
    return {
        "score": result.score,
        "proof_bounds": list(result.proof_bounds),
        "root_series": (
            None if result.root_series is None else _series(result.root_series)
        ),
        "child_pv": [_series(item) for item in result.child_principal_variation],
        "work": _work(result.work),
    }


def _session(
    *,
    width: int = 4,
    deep_teacher_value_model: NativeDeepTeacherValueModel | None = None,
) -> NativeSubtreeSession:
    return NativeSubtreeSession(
        max_series_per_node=width,
        max_work=2_000_000,
        requested_depth=2,
        mate_score=MATE_SCORE,
        cache_capacity=16_384,
        external_cache_weight=0,
        native_threads=1,
        root_tactical_protection=False,
        profile=baseline_profile(),
        root_contract_tt_capacity=16_384,
        root_contract_eval_capacity=16_384,
        deep_teacher_value_model=deep_teacher_value_model,
    )


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: python verify_native_root_session_differential.py <module.js>"
        )
    if not native_subtree_available():
        raise RuntimeError(
            "source-matched Python native extension is unavailable; run "
            "`python setup.py build_ext --inplace` first"
        )
    module = Path(sys.argv[1]).resolve()
    completed = subprocess.run(
        [
            "node",
            str(NODE_GATE),
            str(module),
            evaluation._native_source_identity(),  # noqa: SLF001
            baseline_profile().profile_id,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    actual = json.loads(completed.stdout)
    assert actual["status"] == "passed"

    session = _session()
    root = ProgressiveState.initial()
    manifest = session.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        call_work_credit=500_000,
    )
    assert manifest.status == 0
    assert manifest.enumeration_identity == actual["enumeration_identity"]
    assert [item.candidate_identity for item in manifest.candidates] == [
        item["candidate_identity"] for item in actual["manifest"]["candidates"]
    ]
    assert [item.order_key for item in manifest.candidates] == [
        item["order_key"] for item in actual["manifest"]["candidates"]
    ]
    assert [_series(item.series) for item in manifest.candidates] == [
        item["root_series"] for item in actual["manifest"]["candidates"]
    ]

    candidate = manifest.candidates[0]
    depth_one = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
        call_work_credit=500_000,
    )
    depth_two = session.search_root_candidate(
        enumeration_identity=manifest.enumeration_identity,
        candidate_identity=candidate.candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
        call_work_credit=500_000,
    )
    for expected, wasm_result in (
        (depth_one, actual["depth_one"]),
        (depth_two, actual["depth_two"]),
    ):
        assert expected.status == 0
        assert expected.score == wasm_result["score"]
        assert list(expected.proof_bounds) == wasm_result["proof_bounds"]
        assert [
            _series(item) for item in expected.child_principal_variation
        ] == wasm_result["child_pv"]
        assert _work(expected.work) == wasm_result["work"]

    deep_actual = actual["deep_teacher"]
    material_root = ProgressiveState.from_fen(
        deep_actual["boundary"]["fen"],
        deep_actual["boundary"]["series"],
        quiet_series=deep_actual["boundary"]["quiet_series"],
    )
    material_baseline_session = _session()
    material_baseline_manifest = material_baseline_session.enumerate_root(
        material_root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        call_work_credit=500_000,
    )
    assert (
        material_baseline_manifest.enumeration_identity
        == deep_actual["baseline_enumeration_identity"]
    )
    material_baseline = material_baseline_session.search_root_candidate(
        enumeration_identity=material_baseline_manifest.enumeration_identity,
        candidate_identity=(
            material_baseline_manifest.candidates[0].candidate_identity
        ),
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
        call_work_credit=500_000,
    )
    model_payload = deep_actual["config"]["deep_teacher_value_model"]
    model = NativeDeepTeacherValueModel(
        base_profile_id=model_payload["base_profile_id"],
        variant_id=model_payload["variant_id"],
        model_id=model_payload["model_id"],
        model_sha256=model_payload["model_sha256"],
        native_source_identity=model_payload["native_source_identity"],
        coefficients=tuple(model_payload["coefficients"]),
        fixed_point_scale=model_payload["fixed_point_scale"],
    )
    material_model_session = _session(deep_teacher_value_model=model)
    material_model_manifest = material_model_session.enumerate_root(
        material_root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        call_work_credit=500_000,
    )
    assert material_model_manifest.enumeration_identity == (
        deep_actual["enumeration_identity"]
    )
    assert [
        item.candidate_identity for item in material_model_manifest.candidates
    ] == [
        item["candidate_identity"]
        for item in deep_actual["manifest"]["candidates"]
    ]
    material_modeled = material_model_session.search_root_candidate(
        enumeration_identity=material_model_manifest.enumeration_identity,
        candidate_identity=material_model_manifest.candidates[0].candidate_identity,
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
        call_work_credit=500_000,
    )
    for expected, wasm_result in (
        (material_baseline, deep_actual["baseline"]),
        (material_modeled, deep_actual["modeled"]),
    ):
        assert expected.status == 0
        assert expected.score == wasm_result["score"]
        assert list(expected.proof_bounds) == wasm_result["proof_bounds"]
        assert [
            _series(item) for item in expected.child_principal_variation
        ] == wasm_result["child_pv"]
        assert _work(expected.work) == wasm_result["work"]

    neural_actual = actual["neural_s2"]
    assert neural_actual["ordering_policy"] == NEURAL_S2_ORDER_POLICY
    e4_series = play_series(root, ("e2e4",))
    neural_root = e4_series.final_state
    assert neural_root.series_number == 2
    assert neural_root.board.turn == chess.BLACK
    assert neural_actual["boundary"] == {
        "fen": neural_root.board.fen(en_passant="fen"),
        "series": neural_root.series_number,
        "quiet_series": neural_root.quiet_series,
        "ep_targets": [
            chess.square_name(square) for square in neural_root.ep_targets
        ],
        "promoted_hex": f"{neural_root.board.promoted:016x}",
        "chess960": False,
    }

    neural_session = _session(width=32)
    neural_manifest = neural_session.enumerate_root(
        neural_root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
        call_work_credit=500_000,
    )
    assert neural_manifest.status == 0
    assert neural_manifest.requested_width == 32
    assert neural_manifest.retained_count == 32
    assert NEURAL_S2_ORDER_POLICY in neural_manifest.enumeration_identity
    assert neural_manifest.enumeration_identity == neural_actual[
        "enumeration_identity"
    ]
    assert _manifest(neural_manifest) == neural_actual["manifest"]

    neural_candidate = neural_manifest.candidates[0]
    assert neural_candidate.candidate_identity == neural_actual[
        "candidate_identity"
    ]
    assert neural_candidate.order_key == neural_actual["candidate_order_key"]
    neural_depth_one = neural_session.search_root_candidate(
        enumeration_identity=neural_manifest.enumeration_identity,
        candidate_identity=neural_candidate.candidate_identity,
        child_depth=0,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
        call_work_credit=500_000,
    )
    neural_depth_two = neural_session.search_root_candidate(
        enumeration_identity=neural_manifest.enumeration_identity,
        candidate_identity=neural_candidate.candidate_identity,
        child_depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        external_work=0,
        remaining_nanoseconds=None,
        rollback_tt=False,
        call_work_credit=500_000,
    )
    for expected, wasm_result in (
        (neural_depth_one, neural_actual["depth_one"]),
        (neural_depth_two, neural_actual["depth_two"]),
    ):
        assert expected.status == 0
        assert expected.root_series is not None
        assert _candidate_result(expected) == wasm_result

    receipt = {
        "schema": "spc-root-session-differential-receipt-v1",
        "status": "passed",
        "cases": [
            "initial-root-enumeration",
            "persistent-depth-1-candidate",
            "persistent-depth-2-candidate",
            "neural-s2-e4-root-enumeration-width-32",
            "neural-s2-e4-persistent-depth-1-candidate",
            "neural-s2-e4-persistent-depth-2-candidate",
        ],
        "enumeration_identity": manifest.enumeration_identity,
        "candidate_identity": candidate.candidate_identity,
        "depth_1_score": depth_one.score,
        "depth_2_score": depth_two.score,
        "depth_2_work": depth_two.work.call_native_work,
        "neural_s2": {
            "ordering_policy": NEURAL_S2_ORDER_POLICY,
            "enumeration_identity": neural_manifest.enumeration_identity,
            "candidate_identity": neural_candidate.candidate_identity,
            "candidate_order_key": neural_candidate.order_key,
            "retained_count": neural_manifest.retained_count,
            "depth_1_score": neural_depth_one.score,
            "depth_1_work": neural_depth_one.work.call_native_work,
            "depth_2_score": neural_depth_two.score,
            "depth_2_work": neural_depth_two.work.call_native_work,
        },
    }
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
