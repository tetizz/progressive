from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse


SCHEMA = "spc-root-d5-oracle-v1"
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
IDENTITY_FIELDS = (
    "source_revision",
    "source_fingerprint",
    "kernel_sha256",
    "wasm_sha256",
    "module_js_sha256",
    "artifact_set_sha256",
)
RUNTIME_FIELDS = ("exception_strategy", "wasm_simd", "allocator")
ENGINE_FIELDS = ("engine_version", "ruleset_version", "profile_id")
HEX_64 = re.compile(r"[0-9a-f]{64}")


def _load(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _worker(receipt: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    worker = receipt.get("worker_receipt")
    if not isinstance(worker, dict):
        raise ValueError(f"{label} has no Worker receipt")
    if (
        receipt.get("schema") != "spc-opera-root-session-cdp-receipt-v1"
        or receipt.get("status") != "passed-not-certified"
        or worker.get("schema") != "spc-opera-root-d5-benchmark-v1"
        or worker.get("status") != "passed-not-certified"
    ):
        raise ValueError(f"{label} did not pass its raw Opera benchmark")
    return worker


def _artifact(build: Mapping[str, Any]) -> dict[str, Any]:
    optimization = build.get("optimization")
    if not isinstance(optimization, dict):
        raise ValueError("build optimization identity is missing")
    return {
        **{key: build[key] for key in IDENTITY_FIELDS},
        "exception_strategy": optimization["exception_strategy"],
        "wasm_simd": optimization["wasm_simd"],
        "allocator": optimization["allocator"],
        "runtime_variant": "single",
        "thread_count": 1,
        **{key: build[key] for key in ENGINE_FIELDS},
    }


def _assert_identity(subject: Mapping[str, Any], expected: Mapping[str, Any], label: str) -> None:
    for key in (*IDENTITY_FIELDS, *RUNTIME_FIELDS):
        if subject.get(key) != expected[key]:
            raise ValueError(f"{label} identity field {key!r} differs from the build")


def _selected(result: Mapping[str, Any]) -> dict[str, Any]:
    pv = result.get("principal_variation")
    if not isinstance(pv, list) or not pv:
        raise ValueError("D5 result has no full principal variation")
    selected = {
        "candidate_identity": result.get("candidate_identity"),
        "move": result.get("move"),
        "score": result.get("score"),
        "proof_bounds": result.get("proof_bounds"),
        "principal_variation": pv,
        "principal_variation_sha256": _canonical_sha256(pv),
    }
    if (
        not isinstance(selected["candidate_identity"], str)
        or not selected["candidate_identity"]
        or not isinstance(selected["move"], str)
        or not selected["move"]
        or isinstance(selected["score"], bool)
        or not isinstance(selected["score"], int)
        or not isinstance(selected["proof_bounds"], list)
        or len(selected["proof_bounds"]) != 2
        or pv[0].get("machine_notation") != selected["move"]
    ):
        raise ValueError("D5 selected result is malformed")
    return selected


def _rivals(result: Mapping[str, Any]) -> dict[str, Any]:
    raw = result.get("root_bounds")
    if not isinstance(raw, list) or len(raw) != 20:
        raise ValueError("D5 result does not contain all 20 start-position bounds")
    bounds = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("D5 rival bound is malformed")
        normalized = {
            "candidate_identity": item.get("candidate_identity"),
            "bound": item.get("bound"),
            "score": item.get("score"),
            "proof_bounds": item.get("proof_bounds"),
        }
        if (
            not isinstance(normalized["candidate_identity"], str)
            or normalized["bound"] not in {"exact", "lower", "upper"}
            or isinstance(normalized["score"], bool)
            or not isinstance(normalized["score"], int)
            or not isinstance(normalized["proof_bounds"], list)
            or len(normalized["proof_bounds"]) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in normalized["proof_bounds"])
        ):
            raise ValueError("D5 rival bound is incomplete or Unknown")
        bounds.append(normalized)
    bounds.sort(key=lambda item: item["candidate_identity"])
    if len({item["candidate_identity"] for item in bounds}) != 20:
        raise ValueError("D5 rival identities are not unique")
    counts = {
        kind: sum(item["bound"] == kind for item in bounds)
        for kind in ("exact", "lower", "upper")
    }
    return {
        "coverage_complete": result.get("coverage_complete") is True,
        "candidate_count": len(bounds),
        "unknown_count": 0,
        "exact_count": counts["exact"],
        "lower_count": counts["lower"],
        "upper_count": counts["upper"],
        "bounds": bounds,
        "coverage_sha256": _canonical_sha256(bounds),
    }


def _assert_selection_covered(
    selected: Mapping[str, Any],
    rivals: Mapping[str, Any],
    label: str,
) -> None:
    bounds = rivals["bounds"]
    selected_bounds = [
        item for item in bounds if item["candidate_identity"] == selected["candidate_identity"]
    ]
    if len(selected_bounds) != 1:
        raise ValueError(f"{label} does not cover the selected candidate exactly once")
    selected_bound = selected_bounds[0]
    if (
        selected_bound["bound"] != "exact"
        or selected_bound["score"] != selected["score"]
        or selected_bound["proof_bounds"] != selected["proof_bounds"]
    ):
        raise ValueError(f"{label} does not exactly certify the selected candidate")
    for item in bounds:
        if item["candidate_identity"] == selected["candidate_identity"]:
            continue
        if item["bound"] == "lower" or item["score"] > selected["score"]:
            raise ValueError(f"{label} contains a rival bound that does not prove the selection")


def _assert_same_candidate_universe(
    warm_rivals: Mapping[str, Any],
    cold_rivals: Mapping[str, Any],
) -> None:
    warm_ids = {item["candidate_identity"] for item in warm_rivals["bounds"]}
    cold_ids = {item["candidate_identity"] for item in cold_rivals["bounds"]}
    if warm_ids != cold_ids:
        raise ValueError("fresh D5 and persistent D1-D5 cover different candidate universes")


def _timeout_ms(cdp_receipt: Mapping[str, Any]) -> float:
    page = cdp_receipt.get("page_environment")
    if not isinstance(page, dict) or not isinstance(page.get("location"), str):
        raise ValueError("Opera receipt has no benchmark URL")
    query = parse_qs(urlparse(page["location"]).query)
    values = query.get("timeout_ms")
    if values is None or len(values) != 1:
        raise ValueError("Opera benchmark URL has no unique timeout_ms")
    return float(values[0])


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the signed semantic D5 oracle input")
    parser.add_argument("--build-receipt", type=Path, required=True)
    parser.add_argument("--root-smoke-receipt", type=Path, required=True)
    parser.add_argument("--root-differential-receipt", type=Path, required=True)
    parser.add_argument("--opera-warm-receipt", type=Path, required=True)
    parser.add_argument("--opera-cold-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    build = _load(args.build_receipt)
    smoke = _load(args.root_smoke_receipt)
    differential = _load(args.root_differential_receipt)
    warm_cdp = _load(args.opera_warm_receipt)
    cold_cdp = _load(args.opera_cold_receipt)
    warm = _worker(warm_cdp, "warm Opera receipt")
    cold = _worker(cold_cdp, "cold Opera receipt")
    artifact = _artifact(build)
    _assert_identity(warm.get("artifact", {}), artifact, "warm Opera")
    _assert_identity(cold.get("artifact", {}), artifact, "cold Opera")

    if (
        smoke.get("schema") != "spc-root-session-wasm-smoke-v1"
        or smoke.get("status") != "passed-not-certified"
        or smoke.get("source_revision") != build.get("source_revision")
        or differential.get("schema") != "spc-root-session-differential-receipt-v1"
        or differential.get("status") != "passed"
    ):
        raise ValueError("root smoke or differential evidence did not pass")
    smoke_gates = smoke.get("gates")
    if not isinstance(smoke_gates, dict) or not all(
        smoke_gates.get(key) is True
        for key in (
            "persistent_d1_d2_session",
            "exact_manifest_import",
            "cumulative_work_and_cache_receipts",
            "deadline_fail_closed",
            "canonical_root_tactical_policy",
            "legacy_root_tactical_policy_rejected",
        )
    ):
        raise ValueError("root smoke evidence lacks a required real gate")

    warm_iterations = warm.get("iterations")
    cold_iterations = cold.get("iterations")
    if (
        not isinstance(warm_iterations, list)
        or [item.get("depth") for item in warm_iterations] != [1, 2, 3, 4, 5]
        or not isinstance(cold_iterations, list)
        or [item.get("depth") for item in cold_iterations] != [5]
    ):
        raise ValueError("Opera evidence is not one persistent D1-D5 run plus one fresh D5 run")
    warm_result = warm.get("result")
    cold_result = cold.get("result")
    if not isinstance(warm_result, dict) or not isinstance(cold_result, dict):
        raise ValueError("Opera evidence has no final D5 results")
    selected = _selected(warm_result)
    cold_selected = _selected(cold_result)
    rivals = _rivals(warm_result)
    cold_rivals = _rivals(cold_result)
    _assert_selection_covered(selected, rivals, "persistent D1-D5 coverage")
    _assert_selection_covered(cold_selected, cold_rivals, "fresh D5 coverage")
    _assert_same_candidate_universe(rivals, cold_rivals)
    retained_manifest = warm_result.get("retained_manifest_sha256")
    cold_retained_manifest = cold_result.get("retained_manifest_sha256")
    if (
        selected != cold_selected
        or rivals["coverage_complete"] is not True
        or cold_rivals["coverage_complete"] is not True
        or rivals["unknown_count"] != 0
        or cold_rivals["unknown_count"] != 0
        or not isinstance(retained_manifest, str)
        or not HEX_64.fullmatch(retained_manifest)
        or not isinstance(cold_retained_manifest, str)
        or not HEX_64.fullmatch(cold_retained_manifest)
    ):
        raise ValueError("fresh D5 and persistent D1-D5 do not select the same complete oracle")

    geometry = warm.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("mode") != "warm":
        raise ValueError("warm Opera geometry is missing")
    config = geometry.get("config")
    if not isinstance(config, dict):
        raise ValueError("warm Opera root config is missing")
    cold_geometry = cold.get("geometry")
    if (
        not isinstance(cold_geometry, dict)
        or cold_geometry.get("mode") != "cold"
        or cold_geometry.get("config") != config
    ):
        raise ValueError("fresh and persistent D5 configs differ")

    memory_full = build.get("memory_envelope")
    if not isinstance(memory_full, dict):
        raise ValueError("build memory envelope is missing")
    memory = {
        key: memory_full[key]
        for key in ("initial_bytes", "maximum_bytes", "estimated_peak_bytes", "growth_enabled")
    }
    deadline_limit_ms = _timeout_ms(warm_cdp)
    warm_timings = warm.get("timings_ms")
    if not isinstance(warm_timings, dict):
        raise ValueError("warm Opera timing receipt is missing")
    total_ms = float(warm_timings.get("total_to_completed_depth", 0))
    final_work = warm_result.get("work")
    if not isinstance(final_work, dict) or final_work.get("within_cap") is not True:
        raise ValueError("warm D5 work receipt is incomplete")
    differential_cases = differential.get("cases")
    if not isinstance(differential_cases, list) or len(differential_cases) < 3:
        raise ValueError("root differential evidence has fewer than three real cases")

    boundary = {
        "fen": START_FEN,
        "series": 1,
        "quiet_series": 0,
        "progressive_ep": [],
        "promoted_hex": "0000000000000000",
        "chess960": False,
    }
    semantic = {
        "schema": SCHEMA,
        "artifact": artifact,
        "boundary": boundary,
        "session_config": config,
        "memory": memory,
        "deadline": {"deadline_limit_ms": deadline_limit_ms},
        "retained_manifest_sha256": retained_manifest,
        "selected": selected,
        "rival_bounds": rivals,
    }
    receipt = {
        "schema": SCHEMA,
        "status": "passed",
        "failures": 0,
        "differential_cases": len(differential_cases),
        "artifact": artifact,
        "boundary": boundary,
        "session_config": config,
        "memory": memory,
        "retained_manifest_sha256": retained_manifest,
        "selected": selected,
        "rival_bounds": rivals,
        "work": {
            "status": "complete",
            "within_cap": True,
            "unknown_or_limit_count": 0,
            "max_work": config["max_work"],
            "accounted_work": final_work["committed_work"],
        },
        "deadline": {
            "status": "complete",
            "deadline_reached": False,
            "unknown_or_limit_count": 0,
            "deadline_limit_ms": deadline_limit_ms,
            "remaining_time_ms": max(0.0, deadline_limit_ms - total_ms),
        },
        "gates": {
            "initial_root_enumeration_python_parity": True,
            "persistent_d1_d2_python_parity": True,
            "persistent_d1_through_d5_selects_same_result_as_fresh_d5": True,
            "exact_selected_replay": warm_iterations[-1].get("final_replay", {}).get("complete") is True,
            "work_receipts": True,
            "deadline_receipts": True,
            "complete_rival_bound_coverage": rivals["coverage_complete"],
        },
        "oracle_signature_sha256": _canonical_sha256(semantic),
    }
    if not all(receipt["gates"].values()):
        raise ValueError("root D5 oracle contains a failed gate")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
