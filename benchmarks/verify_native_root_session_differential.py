from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import chess

from scottish_progressive.model import ProgressiveState, SeriesResult
from scottish_progressive.native_subtree import (
    SUBTREE_STAT_FIELDS,
    NativeSubtreeSession,
    NativeSubtreeWorkReceipt,
    native_subtree_available,
)
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.search import MATE_SCORE


ROOT = Path(__file__).resolve().parents[1]
NODE_GATE = ROOT / "benchmarks" / "verify_native_root_session_wasm.mjs"


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


def _session() -> NativeSubtreeSession:
    return NativeSubtreeSession(
        max_series_per_node=4,
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
        ["node", str(NODE_GATE), str(module)],
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

    receipt = {
        "schema": "spc-root-session-differential-receipt-v1",
        "status": "passed",
        "cases": [
            "initial-root-enumeration",
            "persistent-depth-1-candidate",
            "persistent-depth-2-candidate",
        ],
        "enumeration_identity": manifest.enumeration_identity,
        "candidate_identity": candidate.candidate_identity,
        "depth_1_score": depth_one.score,
        "depth_2_score": depth_two.score,
        "depth_2_work": depth_two.work.call_native_work,
    }
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
