from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
from typing import Any, Mapping


HEX_64 = re.compile(r"[0-9a-f]{64}")


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
    return {
        key: result.get(key)
        for key in (
            "candidate_identity",
            "move",
            "score",
            "proof_bounds",
            "principal_variation",
        )
    }


def _unknown_count(result: Mapping[str, Any]) -> int:
    bounds = result.get("root_bounds")
    if not isinstance(bounds, list) or len(bounds) != 20:
        raise ValueError("Opera result does not retain all 20 root bounds")
    return sum(
        not isinstance(item, dict) or item.get("bound") not in {"exact", "lower", "upper"}
        for item in bounds
    )


def _assert_run(
    worker: Mapping[str, Any],
    *,
    label: str,
    expected_depths: list[int],
    expected_mode: str,
    oracle: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
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
        or _unknown_count(result) != 0
        or _selected(result) != {
            key: oracle["selected"][key]
            for key in (
                "candidate_identity",
                "move",
                "score",
                "proof_bounds",
                "principal_variation",
            )
        }
        or result.get("root_bounds") != oracle["rival_bounds"]["bounds"]
        or result.get("retained_manifest_sha256") != oracle.get("retained_manifest_sha256")
    ):
        raise ValueError(f"{label} differs from the signed D5 oracle")
    final = iterations[-1]
    if (
        final.get("owner_certification_count") != 1
        or final.get("candidate_identity") != result.get("candidate_identity")
        or final.get("principal_variation") != result.get("principal_variation")
    ):
        raise ValueError(f"{label} lacks one exact selected-owner certification")
    order_shape = result.get("order_shape_sha256")
    if not isinstance(order_shape, str) or not HEX_64.fullmatch(order_shape):
        raise ValueError(f"{label} lacks a real task-order digest")
    return geometry, result


def _elapsed(worker: Mapping[str, Any], label: str) -> float:
    timings = worker.get("timings_ms")
    if not isinstance(timings, dict):
        raise ValueError(f"{label} has no timing receipt")
    value = timings.get("total_to_completed_depth")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{label} has an invalid elapsed time")
    return float(value)


def _oracle_run(
    worker: Mapping[str, Any],
    result: Mapping[str, Any],
    oracle_signature: str,
    label: str,
) -> dict[str, Any]:
    return {
        "status": "complete",
        "result_signature_sha256": oracle_signature,
        "selected_candidate_identity": result["candidate_identity"],
        "unknown_or_limit_count": _unknown_count(result),
        "selected_owner_certification_count": worker["iterations"][-1]["owner_certification_count"],
        "elapsed_ms": _elapsed(worker, label),
    }


def _schedule_trial(
    worker: Mapping[str, Any],
    geometry: Mapping[str, Any],
    result: Mapping[str, Any],
    oracle_signature: str,
    label: str,
) -> dict[str, Any]:
    return {
        "workers": geometry["workers"],
        "initial_full_wave": geometry["initial_full_wave"],
        "order_shape_sha256": result["order_shape_sha256"],
        "result_signature_sha256": oracle_signature,
        "status": "complete",
        "unknown_or_limit_count": _unknown_count(result),
        "selected_owner_certification_count": worker["iterations"][-1]["owner_certification_count"],
        "elapsed_ms": _elapsed(worker, label),
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
    warm4_geometry, warm4_result = _assert_run(
        warm4,
        label="warm wave-4 run",
        expected_depths=[1, 2, 3, 4, 5],
        expected_mode="warm",
        oracle=oracle,
    )
    other_geometry, other_result = _assert_run(
        other,
        label="alternate warm run",
        expected_depths=[1, 2, 3, 4, 5],
        expected_mode="warm",
        oracle=oracle,
    )
    cold_geometry, cold_result = _assert_run(
        cold,
        label="fresh D5 run",
        expected_depths=[5],
        expected_mode="cold",
        oracle=oracle,
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
            oracle_signature,
            "warm wave-4 run",
        ),
        _schedule_trial(
            other,
            other_geometry,
            other_result,
            oracle_signature,
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
        "cold_matches_oracle": True,
        "warm_matches_oracle": True,
        "cold_d5": _oracle_run(cold, cold_result, oracle_signature, "fresh D5 run"),
        "warm_d1_through_d5": _oracle_run(
            warm4,
            warm4_result,
            oracle_signature,
            "warm wave-4 run",
        ),
    }
    output_worker["schedule_trials"] = schedule_trials
    output_worker["gates"].update(
        {
            "cold_d5_matches_oracle": True,
            "warm_d1_d5_matches_oracle": True,
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
