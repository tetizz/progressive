from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from benchmarks.release_engine_gate import (
    BUDGETS,
    SCENARIOS,
    GateError,
    build_browser_probe_plan,
    certified_baseline,
    evaluate_browser_release,
    evaluate_stockfish_progress,
    evaluate_strength_report,
    native_runtime_identity,
    normalize_browser_receipt,
    run_native_case,
    run_tactical_gate,
    scenario_state,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (
    ROOT
    / "src"
    / "scottish_progressive"
    / "web"
    / "static"
    / "engine"
    / "browser-engine-manifest.json"
)


def _browser_receipt(*, scenario: str = "initial", mode: str = "faster") -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    variant = manifest["variants"]["single"]
    certificate = variant["root_session_certificate"]
    boundary = SCENARIOS[scenario]
    budget = BUDGETS[mode]
    selected = "f2f3" if scenario == "initial" else "a7a6/b7b6"
    root_pv = {
        "moves": selected.split("/"),
        "machine_notation": selected,
        "outcome": None,
        "child_boundary": {"series": boundary.series + 1},
    }
    work = {
        "max_work": budget.max_work,
        "committed_work": 40_000,
        "reserved_work": 0,
        "remaining_work": budget.max_work - 40_000,
        "safety_reserve_work": 1_000_000,
        "safety_committed_work": 100,
        "exact_at_cap": False,
        "within_cap": True,
    }
    return {
        "schema": "spc-opera-root-session-cdp-receipt-v1",
        "status": "passed-not-certified",
        "product_publishable": False,
        "safety_certified": False,
        "cdp": {
            "browser": "Chrome/150.0.0.0",
            "protocol_version": "1.3",
            "user_agent": "Mozilla/5.0 OPR/136.0.0.0",
            "web_socket_debugger_url_recorded": True,
        },
        "page_environment": {
            "userAgent": "Mozilla/5.0 OPR/136.0.0.0",
            "hardwareConcurrency": 8,
            "crossOriginIsolated": False,
            "location": (
                "http://127.0.0.1/benchmark?timeout_ms="
                f"{int(budget.seconds * 1000)}"
            ),
        },
        "worker_receipt": {
            "schema": "spc-opera-root-d5-benchmark-v1",
            "status": "passed-not-certified",
            "product_publishable": False,
            "safety_certified": True,
            "artifact": {
                "source_revision": "a" * 40,
                "source_fingerprint": certificate["source_fingerprint"],
                "kernel_sha256": certificate["kernel_sha256"],
                "wasm_sha256": certificate["wasm_sha256"],
                "module_js_sha256": certificate["module_js_sha256"],
                "artifact_set_sha256": "2" * 64,
                "exception_strategy": "emscripten",
                "wasm_simd": False,
                "allocator": "dlmalloc",
            },
            "boundary": {
                "fen": boundary.fen,
                "series": boundary.series,
                "quiet_series": boundary.quiet_series,
                "ep_targets": list(boundary.ep_targets),
                "promoted_hex": boundary.promoted_hex,
            },
            "geometry": {
                "workers": 8,
                "initial_full_wave": 8,
                "depth": 5,
                "width": 32,
                "max_work": budget.max_work,
                "safety_reserve_work": 1_000_000,
                "mode": "warm",
                "config": {
                    "max_depth": 5,
                    "width": 32,
                    "max_work": budget.max_work,
                    "weights": {
                        "material": 100,
                        "king_space": 100,
                        "series_reach": 100,
                        "promotion_corridors": 100,
                        "immediate_vulnerability": 100,
                        "useful_mobility": 100,
                        "boundary_check": 100,
                    },
                },
            },
            "timings_ms": {
                "pool_ready": 100.0,
                "iterative_d1_through_d5": 3900.0,
                "total_to_completed_depth": 4000.0,
                "completed_depth_iteration": 2500.0,
            },
            "result": {
                "completed_depth": 5,
                "move": selected,
                "score": 617,
                "proof_bounds": [-1, 1],
                "principal_variation": [root_pv],
                "work": work,
                "retained_manifest_sha256": "1" * 64,
                "coverage_complete": True,
                "root_scores_complete": True,
                "width_complete": False,
                "safety_status": "exhausted",
            },
            "iterations": [
                {
                    "depth": depth,
                    "move": selected,
                    "score": 617,
                    "proof_bounds": [-1, 1],
                    "principal_variation": [root_pv],
                    "work": work,
                    "elapsed_ms": depth * 100.0,
                    "retained_manifest_sha256": "1" * 64,
                    "coverage_complete": True,
                    "root_scores_complete": True,
                    "safety_status": "exhausted",
                }
                for depth in range(1, 6)
            ],
            "environment": {
                "ordinary_module_workers": True,
                "worker_count": 8,
                "hardware_concurrency": 8,
            },
            "gates": {
                "exact_artifact_identity_all_workers": True,
                "ordinary_module_workers": True,
                "pthreads_disabled": True,
                "combined_prefix_root_mate_abi": True,
                "persistent_d1_through_d5_sessions": True,
                "exact_manifest_import_all_workers": True,
                "canonical_root_tactical_policy": True,
                "canonical_root_tactical_boundary_echoes": True,
                "global_work_cap_enforced": True,
                "common_monotonic_deadline": True,
                "dynamic_work_pool_certified": True,
                "final_bound_coverage": True,
                "selected_owner_warm_exact_certification": True,
                "compiled_root_prefix_replay": True,
                "compiled_reply_mate_safety": True,
                "memory_envelope_observed": True,
            },
        },
    }


def test_certified_baseline_is_bound_to_checked_in_wasm_certificate() -> None:
    baseline = certified_baseline(MANIFEST)

    assert baseline["certificate_id"] == "spc-root-session-a7ee2880fae4203f"
    assert baseline["profile_id"] == "spc-68942034c41b4cc4"
    assert baseline["status"] == "certified"
    assert baseline["wasm_sha256"] == (
        "25a2a89518dfb67793deb72df20cc4c491e43ea4c9558d1a458aa215a95901f6"
    )


def test_browser_receipt_normalizes_real_engine_metrics_and_identity() -> None:
    sample = normalize_browser_receipt(
        _browser_receipt(),
        scenario="initial",
        mode="faster",
        manifest_path=MANIFEST,
    )

    assert sample["backend"] == "browser-wasm"
    assert sample["scenario"] == "initial"
    assert sample["mode"] == "faster"
    assert sample["requested_depth"] == 5
    assert sample["completed_depth"] == 5
    assert sample["wall_time_seconds"] == 4.0
    assert sample["nodes"] is None
    assert sample["work_positions"] == 40_000
    assert sample["nps"] is None
    assert sample["selected_series"] == "f2f3"
    assert sample["principal_variation"] == ["f2f3"]
    assert sample["evaluation"]["score_white_heuristic_points"] == 617
    assert sample["timeout_reason"] is None
    assert sample["artifact"]["certificate_id"] == (
        "spc-root-session-a7ee2880fae4203f"
    )
    assert sample["artifact"]["source_revision"] == "a" * 40


def test_browser_receipt_rejects_a_budget_or_boundary_mismatch() -> None:
    wrong_budget = _browser_receipt()
    wrong_budget["worker_receipt"]["geometry"]["max_work"] -= 1
    with pytest.raises(GateError, match="max_work"):
        normalize_browser_receipt(
            wrong_budget,
            scenario="initial",
            mode="faster",
            manifest_path=MANIFEST,
        )

    wrong_boundary = copy.deepcopy(_browser_receipt())
    wrong_boundary["worker_receipt"]["boundary"]["series"] = 2
    with pytest.raises(GateError, match="boundary"):
        normalize_browser_receipt(
            wrong_boundary,
            scenario="initial",
            mode="faster",
            manifest_path=MANIFEST,
        )


def test_browser_receipt_rejects_stale_or_unbound_artifact_identity() -> None:
    receipt = _browser_receipt()
    receipt["worker_receipt"]["artifact"]["wasm_sha256"] = "0" * 64

    with pytest.raises(GateError, match="wasm_sha256"):
        normalize_browser_receipt(
            receipt,
            scenario="initial",
            mode="faster",
            manifest_path=MANIFEST,
        )


def test_browser_receipt_rejects_non_opera_or_failed_worker_safety_gate() -> None:
    non_opera = _browser_receipt()
    non_opera["cdp"]["user_agent"] = "Mozilla/5.0 Chrome/150.0.0.0"
    non_opera["page_environment"]["userAgent"] = "Mozilla/5.0 Chrome/150.0.0.0"
    with pytest.raises(GateError, match="Opera"):
        normalize_browser_receipt(
            non_opera,
            scenario="initial",
            mode="faster",
            manifest_path=MANIFEST,
        )

    failed_gate = _browser_receipt()
    failed_gate["worker_receipt"]["gates"]["final_bound_coverage"] = False
    with pytest.raises(GateError, match="final_bound_coverage"):
        normalize_browser_receipt(
            failed_gate,
            scenario="initial",
            mode="faster",
            manifest_path=MANIFEST,
        )


def test_browser_receipt_rejects_an_unsettled_work_ledger() -> None:
    receipt = _browser_receipt()
    receipt["worker_receipt"]["result"]["work"]["reserved_work"] = 1

    with pytest.raises(GateError, match="settle"):
        normalize_browser_receipt(
            receipt,
            scenario="initial",
            mode="faster",
            manifest_path=MANIFEST,
        )


def test_native_gate_requires_a_source_matched_compiled_engine() -> None:
    identity = native_runtime_identity()

    assert identity["backend"] == "native-cpython"
    assert identity["module_filename"].endswith(".pyd")
    assert len(identity["module_sha256"]) == 64
    assert identity["source_identity"] == identity["expected_source_identity"]


def test_black_after_e4_scenario_is_an_authoritative_series_boundary() -> None:
    state = scenario_state("black-after-e4")

    assert state.series_number == 2
    assert state.board.turn is False
    assert state.quiet_series == 0
    assert state.ep_targets == ()
    assert state.board.fen() == SCENARIOS["black-after-e4"].fen


def test_native_faster_sample_reports_real_partial_or_completed_search() -> None:
    sample = run_native_case(
        scenario="initial",
        mode="faster",
        measurement_quality="contended-functional-only",
    )

    assert sample["backend"] == "native-cpython"
    assert sample["scenario"] == "initial"
    assert sample["mode"] == "faster"
    assert sample["requested_depth"] == 5
    assert 0 <= sample["completed_depth"] <= 5
    assert sample["wall_time_seconds"] > 0
    assert sample["nodes"] >= 0
    assert sample["work_positions"] >= 0
    assert sample["nps"] is None or sample["nps"] >= 0
    assert sample["selected_series"] is not None
    assert sample["principal_variation"]
    assert sample["timeout_reason"] in {None, "deadline", "work-limit"}
    assert sample["measurement_quality"] == "contended-functional-only"


def test_tactical_gate_uses_real_search_on_every_published_mate_anchor() -> None:
    report = run_tactical_gate("baseline")

    assert report["schema"] == "spc-release-tactical-anchor-gate-v1"
    assert report["status"] == "passed"
    assert report["scripted_moves"] is False
    assert len(report["anchors"]) == 5
    assert all(anchor["selected_outcome"] == "checkmate" for anchor in report["anchors"])
    assert all(anchor["completed_depth"] == 2 for anchor in report["anchors"])


def _passing_strength_report() -> dict:
    baseline = certified_baseline(MANIFEST)
    return {
        "format": "spc-fixed-suite-strength-v1",
        "candidate": {"profile_id": "spc-candidate"},
        "reference": {"profile_id": baseline["profile_id"]},
        "config": {
            "deterministic_limits": {
                "depth_series": 2,
                "branch_cap_complete_series_per_node": 32,
                "max_work_positions_per_search": 250_000,
                "max_game_work_positions": 5_000_000,
                "time_limit_seconds": None,
                "same_for_both_profiles": True,
            }
        },
        "summary": {
            "scheduled_games": 4,
            "completed_games": 4,
            "incomplete_games": 0,
            "candidate_game_score_rate": 0.5,
            "scheduled_pairs": 2,
            "completed_pairs": 2,
            "incomplete_pairs": 0,
            "candidate_pair_score_rate": 0.5,
            "technical_failures": {
                "total_profile_failures": 0,
                "unattributed_worker_failures": 0,
                "unattributed_match_limit_failures": 0,
            },
        },
    }


def test_equal_budget_strength_decision_is_bound_to_certified_baseline() -> None:
    decision = evaluate_strength_report(
        _passing_strength_report(),
        certified_baseline(MANIFEST),
        minimum_score_rate=0.5,
    )

    assert decision["passed"] is True
    assert decision["equal_budget"] is True
    assert decision["candidate_not_worse_on_fixed_suite"] is True
    assert decision["binary_baseline_participant"] is False


def test_strength_decision_rejects_an_uncertified_reference() -> None:
    report = _passing_strength_report()
    report["reference"]["profile_id"] = "spc-not-certified"

    with pytest.raises(GateError, match="certified baseline"):
        evaluate_strength_report(
            report,
            certified_baseline(MANIFEST),
            minimum_score_rate=0.5,
        )


def test_browser_release_requires_all_four_d5_mode_cases_within_budget() -> None:
    samples = [
        normalize_browser_receipt(
            _browser_receipt(scenario=scenario, mode=mode),
            scenario=scenario,
            mode=mode,
            manifest_path=MANIFEST,
        )
        for scenario in SCENARIOS
        for mode in BUDGETS
    ]

    decision = evaluate_browser_release(samples)

    assert decision["passed"] is True
    assert decision["required_case_count"] == 4
    assert decision["all_completed_depth_5"] is True
    assert decision["all_within_mode_budget"] is True


def test_browser_release_fails_a_slow_or_incomplete_required_case() -> None:
    samples = [
        normalize_browser_receipt(
            _browser_receipt(scenario=scenario, mode=mode),
            scenario=scenario,
            mode=mode,
            manifest_path=MANIFEST,
        )
        for scenario in SCENARIOS
        for mode in BUDGETS
    ]
    samples[0]["wall_time_seconds"] = BUDGETS[samples[0]["mode"]].seconds + 0.001
    samples[1]["completed_depth"] = 4

    decision = evaluate_browser_release(samples)

    assert decision["passed"] is False
    assert decision["all_completed_depth_5"] is False
    assert decision["all_within_mode_budget"] is False


def test_browser_release_rejects_mixed_artifact_commits() -> None:
    samples = [
        normalize_browser_receipt(
            _browser_receipt(scenario=scenario, mode=mode),
            scenario=scenario,
            mode=mode,
            manifest_path=MANIFEST,
        )
        for scenario in SCENARIOS
        for mode in BUDGETS
    ]
    samples[-1]["artifact"]["source_revision"] = "b" * 40

    with pytest.raises(GateError, match="artifact identity"):
        evaluate_browser_release(samples)


def _quiet_native_suite(seconds: tuple[float, float, float] = (8.0, 9.0, 9.5)) -> dict:
    return {
        "schema": "spc-release-engine-search-gate-v1",
        "measurement_quality": "quiet-controlled",
        "reportable_performance": True,
        "cases": [
            {
                "scenario": scenario,
                "mode": "strong",
                "backend": "native-cpython",
                "budget": BUDGETS["strong"].as_dict(),
                "samples": [
                    {
                        "backend": "native-cpython",
                        "scenario": scenario,
                        "mode": "strong",
                        "budget": BUDGETS["strong"].as_dict(),
                        "requested_depth": 5,
                        "completed_depth": 5,
                        "wall_time_seconds": elapsed,
                        "timed_out": False,
                        "work_limit_reached": False,
                        "timeout_reason": None,
                        "selected_series": selected,
                        "principal_variation": [selected, "a7a6/b7b6"],
                        "measurement_quality": "quiet-controlled",
                        "artifact": {
                            "module_sha256": "1" * 64,
                            "source_identity": "3" * 64,
                        },
                        "git": {"commit": "a" * 40, "dirty": False},
                    }
                    for elapsed in seconds
                ],
            }
            for scenario, selected in (
                ("initial", "e2e4"),
                ("black-after-e4", "a7a6/b7b6"),
            )
        ],
    }


def test_stockfish_progress_verdict_requires_stable_quiet_sub10_d5() -> None:
    decision = evaluate_stockfish_progress(_quiet_native_suite())

    assert decision["passed"] is True
    assert decision["sub10_d5_achieved"] is True
    assert decision["stockfish_level_achieved"] is False
    assert all(case["stable_selected_series_and_pv"] for case in decision["cases"])


def test_stockfish_progress_verdict_fails_slow_or_unstable_samples() -> None:
    slow = evaluate_stockfish_progress(_quiet_native_suite((9.0, 11.0, 12.0)))
    assert slow["passed"] is False
    assert slow["sub10_d5_achieved"] is False

    unstable_suite = _quiet_native_suite()
    unstable_suite["cases"][0]["samples"][-1]["selected_series"] = "d2d4"
    unstable_suite["cases"][0]["samples"][-1]["principal_variation"] = ["d2d4"]
    unstable = evaluate_stockfish_progress(unstable_suite)
    assert unstable["passed"] is False
    assert unstable["cases"][0]["stable_selected_series_and_pv"] is False


def test_stockfish_progress_rejects_contended_timings() -> None:
    suite = _quiet_native_suite()
    suite["measurement_quality"] = "contended-functional-only"
    suite["reportable_performance"] = False

    with pytest.raises(GateError, match="quiet-controlled"):
        evaluate_stockfish_progress(suite)


def test_stockfish_progress_rejects_wrong_backend_or_mixed_engine_identity() -> None:
    wrong_backend = _quiet_native_suite()
    wrong_backend["cases"][0]["backend"] = "browser-wasm"
    with pytest.raises(GateError, match="native-cpython"):
        evaluate_stockfish_progress(wrong_backend)

    mixed_identity = _quiet_native_suite()
    mixed_identity["cases"][0]["samples"][-1]["artifact"]["module_sha256"] = "2" * 64
    with pytest.raises(GateError, match="identity"):
        evaluate_stockfish_progress(mixed_identity)


def test_browser_probe_plan_contains_exact_four_mode_boundaries() -> None:
    plan = build_browser_probe_plan(
        origin="http://127.0.0.1:8879",
        module_url="/build/spc-engine.js",
        wasm_url="/build/spc-root-session.wasm",
        build_receipt_url="/build/root-receipt.json",
    )

    assert plan["schema"] == "spc-browser-d5-probe-plan-v1"
    assert len(plan["cases"]) == 4
    assert {case["scenario"] for case in plan["cases"]} == set(SCENARIOS)
    assert {case["mode"] for case in plan["cases"]} == set(BUDGETS)
    assert all("depth=5" in case["probe_url"] for case in plan["cases"])
    assert all("max_work=4000000000" in case["probe_url"] for case in plan["cases"])
    assert any("timeout_ms=5000" in case["probe_url"] for case in plan["cases"])
    assert any("timeout_ms=30000" in case["probe_url"] for case in plan["cases"])
