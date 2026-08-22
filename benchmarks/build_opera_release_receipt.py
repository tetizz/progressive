from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping


HEX_64 = re.compile(r"[0-9a-f]{64}")
FINAL_RESULT_FIELDS = (
    "candidate_identity",
    "move",
    "score",
    "proof_bounds",
    "principal_variation",
    "work",
    "safety_status",
    "safety_revision",
    "owner_worker_id",
    "root_bounds",
    "retained_manifest_sha256",
    "order_shape_sha256",
    "coverage_complete",
    "root_scores_complete",
    "width_complete",
)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _worker(receipt: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    worker = receipt.get("worker_receipt")
    if (
        receipt.get("schema") != "spc-opera-root-session-cdp-receipt-v1"
        or receipt.get("status") != "passed-not-certified"
        or not isinstance(worker, dict)
        or worker.get("schema") != "spc-opera-root-d5-benchmark-v1"
        or worker.get("status") != "passed-not-certified"
    ):
        raise ValueError(f"{label} is not a passing raw Opera benchmark")
    return worker


def _selected(result: Mapping[str, Any]) -> dict[str, Any]:
    selected = {
        key: result.get(key)
        for key in (
            "candidate_identity",
            "move",
            "score",
            "proof_bounds",
            "principal_variation",
        )
    }
    selected["principal_variation_sha256"] = _canonical_sha256(
        selected["principal_variation"]
    )
    return selected


def _normalize_bounds(
    result: Mapping[str, Any],
    oracle: Mapping[str, Any],
    label: str,
) -> list[dict[str, Any]]:
    oracle_rivals = oracle.get("rival_bounds")
    if not isinstance(oracle_rivals, dict) or not isinstance(oracle_rivals.get("bounds"), list):
        raise ValueError("signed D5 oracle lacks canonical rival bounds")
    expected_ids = {
        item.get("candidate_identity")
        for item in oracle_rivals["bounds"]
        if isinstance(item, dict)
    }
    if len(expected_ids) != 20 or not all(isinstance(item, str) and item for item in expected_ids):
        raise ValueError("signed D5 oracle has an invalid candidate universe")

    raw_bounds = result.get("root_bounds")
    if not isinstance(raw_bounds, list) or len(raw_bounds) != 20:
        raise ValueError(f"{label} does not retain all 20 root bounds")
    bounds: list[dict[str, Any]] = []
    for raw in raw_bounds:
        if not isinstance(raw, dict):
            raise ValueError(f"{label} contains a malformed root bound")
        normalized = {
            "candidate_identity": raw.get("candidate_identity"),
            "bound": raw.get("bound"),
            "score": raw.get("score"),
            "proof_bounds": raw.get("proof_bounds"),
        }
        if (
            not isinstance(normalized["candidate_identity"], str)
            or not normalized["candidate_identity"]
            or normalized["bound"] not in {"exact", "lower", "upper"}
            or isinstance(normalized["score"], bool)
            or not isinstance(normalized["score"], int)
            or not isinstance(normalized["proof_bounds"], list)
            or len(normalized["proof_bounds"]) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in normalized["proof_bounds"]
            )
        ):
            raise ValueError(f"{label} contains an incomplete or Unknown root bound")
        bounds.append(normalized)
    bounds.sort(key=lambda item: item["candidate_identity"])
    candidate_ids = [item["candidate_identity"] for item in bounds]
    if len(set(candidate_ids)) != 20 or set(candidate_ids) != expected_ids:
        raise ValueError(f"{label} does not cover the oracle's exact candidate universe")

    selected = _selected(result)
    selected_bounds = [
        item for item in bounds if item["candidate_identity"] == selected["candidate_identity"]
    ]
    if len(selected_bounds) != 1:
        raise ValueError(f"{label} does not cover its selected candidate exactly once")
    selected_bound = selected_bounds[0]
    if (
        selected_bound["bound"] != "exact"
        or selected_bound["score"] != selected["score"]
        or selected_bound["proof_bounds"] != selected["proof_bounds"]
    ):
        raise ValueError(f"{label} does not exactly certify its selected candidate")
    for item in bounds:
        if item["candidate_identity"] == selected["candidate_identity"]:
            continue
        if item["bound"] == "lower" or item["score"] > selected["score"]:
            raise ValueError(f"{label} contains a rival bound that does not prove the selection")
    return bounds


def _assert_run(
    worker: Mapping[str, Any],
    *,
    label: str,
    expected_depths: list[int],
    expected_mode: str,
    oracle: Mapping[str, Any],
    require_oracle_coverage: bool,
) -> tuple[Mapping[str, Any], Mapping[str, Any], list[dict[str, Any]]]:
    geometry = worker.get("geometry")
    result = worker.get("result")
    iterations = worker.get("iterations")
    if not isinstance(geometry, dict) or not isinstance(result, dict) or not isinstance(iterations, list):
        raise ValueError(f"{label} lacks geometry, result, or iterations")
    if geometry.get("mode") != expected_mode or [item.get("depth") for item in iterations] != expected_depths:
        raise ValueError(f"{label} did not use the expected {expected_mode} depth schedule")
    if (
        result.get("completed_depth") != 5
        or result.get("coverage_complete") is not True
        or result.get("safety_status") not in {"exhausted", "terminal"}
        or _selected(result) != {
            key: oracle["selected"][key]
            for key in (
                "candidate_identity",
                "move",
                "score",
                "proof_bounds",
                "principal_variation",
                "principal_variation_sha256",
            )
        }
    ):
        raise ValueError(f"{label} differs from the signed D5 oracle")
    bounds = _normalize_bounds(result, oracle, label)
    retained_manifest = result.get("retained_manifest_sha256")
    if not isinstance(retained_manifest, str) or not HEX_64.fullmatch(retained_manifest):
        raise ValueError(f"{label} lacks its actual retained-manifest digest")
    if require_oracle_coverage and (
        bounds != oracle["rival_bounds"]["bounds"]
        or retained_manifest != oracle.get("retained_manifest_sha256")
    ):
        raise ValueError(f"{label} does not carry the oracle's signed rival coverage")
    final = iterations[-1]
    if not isinstance(final, dict):
        raise ValueError(f"{label} has a malformed final iteration")
    _assert_final_result_identity(final, result, label)
    if (
        final.get("owner_certification_count") != 1
    ):
        raise ValueError(f"{label} lacks one exact selected-owner certification")
    order_shape = result.get("order_shape_sha256")
    if not isinstance(order_shape, str) or not HEX_64.fullmatch(order_shape):
        raise ValueError(f"{label} lacks a real task-order digest")
    return geometry, result, bounds


def _elapsed(worker: Mapping[str, Any], label: str) -> float:
    timings = worker.get("timings_ms")
    if not isinstance(timings, dict):
        raise ValueError(f"{label} has no timing receipt")
    value = timings.get("total_to_completed_depth")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{label} has an invalid elapsed time")
    return float(value)


def _assert_final_result_identity(
    final: Mapping[str, Any],
    result: Mapping[str, Any],
    label: str,
) -> None:
    if final.get("depth") != result.get("completed_depth") or any(
        final.get(key) != result.get(key) for key in FINAL_RESULT_FIELDS
    ):
        raise ValueError(f"{label} final iteration differs from its published result")


def _oracle_run(
    worker: Mapping[str, Any],
    result: Mapping[str, Any],
    bounds: list[dict[str, Any]],
    label: str,
) -> dict[str, Any]:
    selected = _selected(result)
    retained_manifest = result["retained_manifest_sha256"]
    semantic = {
        "selected": selected,
        "retained_manifest_sha256": retained_manifest,
        "rival_bounds": bounds,
    }
    return {
        "status": "complete",
        "selected_signature_sha256": _canonical_sha256(selected),
        "run_signature_sha256": _canonical_sha256(semantic),
        "selected_candidate_identity": result["candidate_identity"],
        "unknown_or_limit_count": 0,
        "selected_owner_certification_count": worker["iterations"][-1]["owner_certification_count"],
        "elapsed_ms": _elapsed(worker, label),
        "retained_manifest_sha256": retained_manifest,
        "rival_bounds": bounds,
        "root_coverage_sha256": _canonical_sha256(bounds),
    }


def _schedule_trial(
    worker: Mapping[str, Any],
    geometry: Mapping[str, Any],
    result: Mapping[str, Any],
    bounds: list[dict[str, Any]],
    label: str,
) -> dict[str, Any]:
    binding = _oracle_run(worker, result, bounds, label)
    trial_semantic = {
        "run_signature_sha256": binding["run_signature_sha256"],
        "workers": geometry["workers"],
        "initial_full_wave": geometry["initial_full_wave"],
        "order_shape_sha256": result["order_shape_sha256"],
    }
    return {
        "workers": geometry["workers"],
        "initial_full_wave": geometry["initial_full_wave"],
        "order_shape_sha256": result["order_shape_sha256"],
        **binding,
        "trial_signature_sha256": _canonical_sha256(trial_semantic),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bind real Opera runs to the D5 release oracle")
    parser.add_argument("--warm-wave4", type=Path, required=True)
    parser.add_argument("--warm-other", type=Path, required=True)
    parser.add_argument("--cold", type=Path, required=True)
    parser.add_argument("--root-oracle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    warm4_cdp = _load(args.warm_wave4)
    other_cdp = _load(args.warm_other)
    cold_cdp = _load(args.cold)
    oracle = _load(args.root_oracle)
    if oracle.get("schema") != "spc-root-d5-oracle-v1" or oracle.get("status") != "passed":
        raise ValueError("root D5 oracle did not pass")
    oracle_signature = oracle.get("oracle_signature_sha256")
    if not isinstance(oracle_signature, str) or not HEX_64.fullmatch(oracle_signature):
        raise ValueError("root D5 oracle signature is invalid")

    warm4 = _worker(warm4_cdp, "warm wave-4 run")
    other = _worker(other_cdp, "alternate warm run")
    cold = _worker(cold_cdp, "fresh D5 run")
    warm4_geometry, warm4_result, warm4_bounds = _assert_run(
        warm4,
        label="warm wave-4 run",
        expected_depths=[1, 2, 3, 4, 5],
        expected_mode="warm",
        oracle=oracle,
        require_oracle_coverage=True,
    )
    other_geometry, other_result, other_bounds = _assert_run(
        other,
        label="alternate warm run",
        expected_depths=[1, 2, 3, 4, 5],
        expected_mode="warm",
        oracle=oracle,
        require_oracle_coverage=False,
    )
    cold_geometry, cold_result, cold_bounds = _assert_run(
        cold,
        label="fresh D5 run",
        expected_depths=[5],
        expected_mode="cold",
        oracle=oracle,
        require_oracle_coverage=False,
    )
    if (
        warm4_geometry.get("workers") != 8
        or warm4_geometry.get("initial_full_wave") != 4
        or warm4_geometry.get("config") != oracle.get("session_config")
        or other_geometry.get("workers") != 8
        or other_geometry.get("initial_full_wave") == 4
        or other_geometry.get("config") != oracle.get("session_config")
        or cold_geometry.get("workers") != 8
        or cold_geometry.get("config") != oracle.get("session_config")
    ):
        raise ValueError("Opera release runs used the wrong Worker, wave, or config geometry")
    if warm4.get("artifact") != other.get("artifact") or warm4.get("artifact") != cold.get("artifact"):
        raise ValueError("Opera release runs did not execute the same exact artifact")

    schedule_trials = [
        _schedule_trial(
            warm4,
            warm4_geometry,
            warm4_result,
            warm4_bounds,
            "warm wave-4 run",
        ),
        _schedule_trial(
            other,
            other_geometry,
            other_result,
            other_bounds,
            "alternate warm run",
        ),
    ]
    if schedule_trials[0]["order_shape_sha256"] == schedule_trials[1]["order_shape_sha256"]:
        raise ValueError("alternate Opera schedules produced the same task-order shape")

    output = copy.deepcopy(warm4_cdp)
    output_worker = output["worker_receipt"]
    output_worker["schema"] = "spc-opera-root-d5-benchmark-v2"
    output_worker["oracle"] = {
        "schema": "spc-opera-root-d5-oracle-binding-v1",
        "oracle_signature_sha256": oracle_signature,
        "selected_signature_sha256": _canonical_sha256(oracle["selected"]),
        "cold_selected_matches_oracle": True,
        "warm_full_matches_oracle": True,
        "cold_d5": _oracle_run(cold, cold_result, cold_bounds, "fresh D5 run"),
        "warm_d1_through_d5": _oracle_run(
            warm4,
            warm4_result,
            warm4_bounds,
            "warm wave-4 run",
        ),
    }
    output_worker["schedule_trials"] = schedule_trials
    output_worker["gates"].update(
        {
            "cold_d5_selected_matches_oracle": True,
            "warm_d1_d5_full_matches_oracle": True,
            "alternate_schedule_selected_matches_oracle": True,
            "multiple_seed_wave_order_shapes": True,
            "no_unknown_or_limit_results": True,
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
