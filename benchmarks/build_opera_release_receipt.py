from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping


HEX_64 = re.compile(r"[0-9a-f]{64}")
ASPIRATION_INITIAL_DELTA = 2_048
MAX_ASPIRATION_ATTEMPTS = 4
ASPIRATION_COUNTER_FIELDS = (
    "attempts",
    "fail_highs",
    "fail_lows",
    "exact_hits",
    "full_window_fallbacks",
)
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


def _aspiration_iterations(
    iterations: object,
    *,
    label: str,
    expected_depths: list[int],
    expected_mode: str,
    expected_candidate_count: int,
) -> list[dict[str, Any]]:
    if not isinstance(iterations, list) or len(iterations) != len(expected_depths):
        raise ValueError(f"{label} lacks its exact aspiration depth schedule")
    normalized: list[dict[str, Any]] = []
    previous_score: int | None = None
    previous_owner: str | None = None
    for expected_depth, raw_iteration in zip(expected_depths, iterations, strict=True):
        if not isinstance(raw_iteration, dict) or raw_iteration.get("depth") != expected_depth:
            raise ValueError(f"{label} has a malformed D{expected_depth} aspiration iteration")
        score = raw_iteration.get("score")
        selected_owner = raw_iteration.get("owner_worker_id")
        if (
            isinstance(score, bool)
            or not isinstance(score, int)
            or not isinstance(selected_owner, str)
            or not selected_owner
        ):
            raise ValueError(f"{label} D{expected_depth} lacks an exact selected owner")
        raw_aspiration = raw_iteration.get("aspiration")
        if not isinstance(raw_aspiration, dict):
            raise ValueError(f"{label} D{expected_depth} lacks aspiration telemetry")
        expected_enabled = expected_mode == "warm" and previous_score is not None
        if raw_aspiration.get("enabled") is not expected_enabled:
            state = "enabled" if expected_enabled else "disabled"
            raise ValueError(f"{label} D{expected_depth} aspiration must be {state}")
        if raw_aspiration.get("maximum_attempts") != MAX_ASPIRATION_ATTEMPTS:
            raise ValueError(f"{label} D{expected_depth} aspiration attempt limit drifted")
        candidate_count = raw_aspiration.get("candidate_count")
        if (
            isinstance(candidate_count, bool)
            or not isinstance(candidate_count, int)
            or candidate_count < 0
            or candidate_count > 8
        ):
            raise ValueError(f"{label} D{expected_depth} aspiration candidate count is invalid")
        counters: dict[str, int] = {}
        for field in ASPIRATION_COUNTER_FIELDS:
            value = raw_aspiration.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"{label} D{expected_depth} aspiration counter {field!r} is invalid"
                )
            counters[field] = value
        if (
            counters["attempts"] > candidate_count * MAX_ASPIRATION_ATTEMPTS
            or counters["exact_hits"] > candidate_count
            or counters["full_window_fallbacks"] > candidate_count
            or counters["fail_highs"] + counters["fail_lows"]
            + counters["exact_hits"] != counters["attempts"]
            or counters["exact_hits"] + counters["full_window_fallbacks"]
            > candidate_count
        ):
            raise ValueError(f"{label} D{expected_depth} aspiration accounting contradicts itself")

        aspiration_owner = raw_aspiration.get("owner_worker_id")
        owner_worker_ids = raw_aspiration.get("owner_worker_ids")
        owner_worker_count = raw_aspiration.get("owner_worker_count")
        warm_owner_reused = raw_aspiration.get("warm_owner_reused")
        warm_owner_reused_count = raw_aspiration.get("warm_owner_reused_count")
        if expected_enabled:
            if (
                candidate_count != expected_candidate_count
                or raw_aspiration.get("center_score") != previous_score
                or raw_aspiration.get("initial_delta") != ASPIRATION_INITIAL_DELTA
            ):
                raise ValueError(f"{label} D{expected_depth} aspiration window drifted")
            if (
                not isinstance(aspiration_owner, str)
                or not aspiration_owner
                or aspiration_owner != previous_owner
                or warm_owner_reused is not True
                or not isinstance(owner_worker_ids, list)
                or len(owner_worker_ids) != candidate_count
                or any(not isinstance(owner, str) or not owner for owner in owner_worker_ids)
                or len(set(owner_worker_ids)) != candidate_count
                or owner_worker_ids[0] != aspiration_owner
                or owner_worker_count != candidate_count
                or warm_owner_reused_count != candidate_count
            ):
                raise ValueError(f"{label} D{expected_depth} did not reuse its warm owner")
            if candidate_count < 1 or counters["attempts"] < candidate_count or (
                counters["exact_hits"] + counters["full_window_fallbacks"]
                != candidate_count
            ):
                raise ValueError(
                    f"{label} D{expected_depth} aspiration has no exact result or fallback"
                )
            failure_count = counters["fail_highs"] + counters["fail_lows"]
            if counters["full_window_fallbacks"] > 0 and (
                failure_count
                < counters["full_window_fallbacks"] * MAX_ASPIRATION_ATTEMPTS
                or failure_count > (
                    counters["full_window_fallbacks"] * MAX_ASPIRATION_ATTEMPTS
                    + counters["exact_hits"] * (MAX_ASPIRATION_ATTEMPTS - 1)
                )
            ):
                raise ValueError(
                    f"{label} D{expected_depth} aspiration fallback accounting is invalid"
                )
        elif (
            raw_aspiration.get("center_score") is not None
            or raw_aspiration.get("initial_delta") is not None
            or candidate_count != 0
            or aspiration_owner is not None
            or owner_worker_ids != []
            or owner_worker_count != 0
            or warm_owner_reused is not False
            or warm_owner_reused_count != 0
            or any(counters.values())
        ):
            raise ValueError(f"{label} D{expected_depth} disabled aspiration did work")

        normalized.append(
            {
                "depth": expected_depth,
                "selected_score": score,
                "selected_owner_worker_id": selected_owner,
                "enabled": expected_enabled,
                "center_score": raw_aspiration.get("center_score"),
                "initial_delta": raw_aspiration.get("initial_delta"),
                "maximum_attempts": MAX_ASPIRATION_ATTEMPTS,
                "candidate_count": candidate_count,
                **counters,
                "owner_worker_id": aspiration_owner,
                "owner_worker_ids": list(owner_worker_ids),
                "owner_worker_count": owner_worker_count,
                "warm_owner_reused": warm_owner_reused,
                "warm_owner_reused_count": warm_owner_reused_count,
            }
        )
        previous_score = score
        previous_owner = selected_owner
    return normalized


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
) -> tuple[
    Mapping[str, Any],
    Mapping[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    geometry = worker.get("geometry")
    result = worker.get("result")
    iterations = worker.get("iterations")
    if not isinstance(geometry, dict) or not isinstance(result, dict) or not isinstance(iterations, list):
        raise ValueError(f"{label} lacks geometry, result, or iterations")
    if geometry.get("mode") != expected_mode or [item.get("depth") for item in iterations] != expected_depths:
        raise ValueError(f"{label} did not use the expected {expected_mode} depth schedule")
    if geometry.get("aspiration_enabled") is not True:
        raise ValueError(f"{label} did not enable the aspiration-capable harness")
    aspiration = _aspiration_iterations(
        iterations,
        label=label,
        expected_depths=expected_depths,
        expected_mode=expected_mode,
        expected_candidate_count=int(geometry.get("initial_full_wave", 0)),
    )
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
    return geometry, result, bounds, aspiration


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
    aspiration: list[dict[str, Any]],
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
        "aspiration_iterations": copy.deepcopy(aspiration),
        "aspiration_sha256": _canonical_sha256(aspiration),
    }


def _schedule_trial(
    worker: Mapping[str, Any],
    geometry: Mapping[str, Any],
    result: Mapping[str, Any],
    bounds: list[dict[str, Any]],
    aspiration: list[dict[str, Any]],
    label: str,
) -> dict[str, Any]:
    binding = _oracle_run(worker, result, bounds, aspiration, label)
    trial_semantic = {
        "run_signature_sha256": binding["run_signature_sha256"],
        "workers": geometry["workers"],
        "initial_full_wave": geometry["initial_full_wave"],
        "order_shape_sha256": result["order_shape_sha256"],
        "aspiration_sha256": binding["aspiration_sha256"],
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
    parser.add_argument("--warm-primary", type=Path, required=True)
    parser.add_argument("--warm-other", type=Path, required=True)
    parser.add_argument("--cold", type=Path, required=True)
    parser.add_argument("--root-oracle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    warm_cdp = _load(args.warm_primary)
    other_cdp = _load(args.warm_other)
    cold_cdp = _load(args.cold)
    oracle = _load(args.root_oracle)
    if oracle.get("schema") != "spc-root-d5-oracle-v1" or oracle.get("status") != "passed":
        raise ValueError("root D5 oracle did not pass")
    oracle_signature = oracle.get("oracle_signature_sha256")
    if not isinstance(oracle_signature, str) or not HEX_64.fullmatch(oracle_signature):
        raise ValueError("root D5 oracle signature is invalid")

    warm = _worker(warm_cdp, "warm wave-8 run")
    other = _worker(other_cdp, "alternate warm run")
    cold = _worker(cold_cdp, "fresh D5 run")
    warm_geometry, warm_result, warm_bounds, warm_aspiration = _assert_run(
        warm,
        label="warm wave-8 run",
        expected_depths=[1, 2, 3, 4, 5],
        expected_mode="warm",
        oracle=oracle,
        require_oracle_coverage=True,
    )
    other_geometry, other_result, other_bounds, other_aspiration = _assert_run(
        other,
        label="alternate warm run",
        expected_depths=[1, 2, 3, 4, 5],
        expected_mode="warm",
        oracle=oracle,
        require_oracle_coverage=False,
    )
    cold_geometry, cold_result, cold_bounds, cold_aspiration = _assert_run(
        cold,
        label="fresh D5 run",
        expected_depths=[5],
        expected_mode="cold",
        oracle=oracle,
        require_oracle_coverage=False,
    )
    if (
        warm_geometry.get("workers") != 8
        or warm_geometry.get("initial_full_wave") != 8
        or warm_geometry.get("config") != oracle.get("session_config")
        or other_geometry.get("workers") != 8
        or other_geometry.get("initial_full_wave") == 8
        or other_geometry.get("config") != oracle.get("session_config")
        or cold_geometry.get("workers") != 8
        or cold_geometry.get("config") != oracle.get("session_config")
    ):
        raise ValueError("Opera release runs used the wrong Worker, wave, or config geometry")
    if warm.get("artifact") != other.get("artifact") or warm.get("artifact") != cold.get("artifact"):
        raise ValueError("Opera release runs did not execute the same exact artifact")

    schedule_trials = [
        _schedule_trial(
            warm,
            warm_geometry,
            warm_result,
            warm_bounds,
            warm_aspiration,
            "warm wave-8 run",
        ),
        _schedule_trial(
            other,
            other_geometry,
            other_result,
            other_bounds,
            other_aspiration,
            "alternate warm run",
        ),
    ]
    if schedule_trials[0]["order_shape_sha256"] == schedule_trials[1]["order_shape_sha256"]:
        raise ValueError("alternate Opera schedules produced the same task-order shape")

    output = copy.deepcopy(warm_cdp)
    output_worker = output["worker_receipt"]
    output_worker["schema"] = "spc-opera-root-d5-benchmark-v2"
    output_worker["oracle"] = {
        "schema": "spc-opera-root-d5-oracle-binding-v1",
        "oracle_signature_sha256": oracle_signature,
        "selected_signature_sha256": _canonical_sha256(oracle["selected"]),
        "cold_selected_matches_oracle": True,
        "warm_full_matches_oracle": True,
        "cold_d5": _oracle_run(
            cold,
            cold_result,
            cold_bounds,
            cold_aspiration,
            "fresh D5 run",
        ),
        "warm_d1_through_d5": _oracle_run(
            warm,
            warm_result,
            warm_bounds,
            warm_aspiration,
            "warm wave-8 run",
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
            "aspiration_iteration_lifecycle": True,
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
