from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser root tests")
def test_browser_root_worker_pool_contract() -> None:
    completed = subprocess.run(
        [str(NODE), str(ROOT / "benchmarks" / "verify_browser_root_iteration.mjs")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    receipt = json.loads(completed.stdout)

    assert receipt == {
        "schema": "spc-browser-root-iteration-mock-receipt-v1",
        "desktop_initial_full_wave": "8-of-8",
        "all_initial_wave_aspiration": True,
        "aggregate_aspiration_accounting": True,
        "exact_owner_affinity": True,
        "exact_owner_priority_stable": True,
        "unavailable_claimed_owner_fails_closed": True,
        "persistent_worker_pool": True,
        "fresh_sessions_per_turn": True,
        "pooled_native_prefix": True,
        "preflight_heap_released": True,
        "white_black_mate_mapping": True,
        "unproved_mate_claim_quarantine_white_black": True,
        "pruned_bounds_publish": True,
        "immediate_mate_with_alternatives": True,
        "all_mating_frontier_terminal_mate_rescue": True,
        "unproven_terminal_mate_rescue_fails_closed": True,
        "native_promotion_mate_deferral_terminal_mate_rescue": True,
        "unproven_promotion_mate_deferral_fails_closed": True,
        "incomplete_bound_coverage_fails_closed": True,
        "complete_mate_proof_cache": True,
        "unknown_mate_proof_not_cached": True,
        "unknown_checked_pv_horizon_fails_closed": True,
        "unprobed_checked_pv_horizon_fails_closed": True,
        "mate_cache_identity_boundary_bound": True,
        "crash_last_safe_and_reprobe": True,
        "absolute_deadline_epoch_transport": True,
        "canonical_root_policy_drift_fails_closed": True,
        "canonical_root_policy_selects_late_and_promotion_boundaries": True,
        "mismatched_worker_time_origin_clamped": True,
        "unknown_memory_uses_lower_geometry": True,
        "checked_pv_horizon_mate_rejected": True,
        "checked_pv_horizon_native_repaired": True,
        "checked_pv_second_distinct_mate_policy_veto": True,
        "checked_pv_horizon_dedicated_schema": True,
        "checked_pv_horizon_root_chain_fail_closed": True,
        "stale_horizon_safety_reply_fail_closed": True,
        "favorable_checked_horizon_not_vetoed": True,
    }


@pytest.mark.skipif(NODE is None, reason="Node.js is required for root coordinator tests")
def test_root_coordinator_browser_copy_matches_and_contract_suite_passes() -> None:
    assert (ROOT / "root-iteration-coordinator.js").read_bytes() == (
        ROOT
        / "src"
        / "scottish_progressive"
        / "web"
        / "static"
        / "root-iteration-coordinator.js"
    ).read_bytes()
    completed = subprocess.run(
        [str(NODE), str(ROOT / "benchmarks" / "verify_root_iteration_coordinator.mjs")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    receipt = json.loads(completed.stdout)
    assert receipt["schema"] == "spc-root-iteration-coordinator-verifier-v1"
    assert receipt["root_series_boundary_mutations_fail_closed"] is True
