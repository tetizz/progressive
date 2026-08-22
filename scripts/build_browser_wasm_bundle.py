from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Mapping


MANIFEST_SCHEMA = "spc-browser-wasm-manifest-v1"
CERTIFICATE_SCHEMA = "spc-browser-wasm-certificate-v1"
PREFIX_CONTRACT_SCHEMA = "spc-boundary-prefix-contract-v1"
PREFIX_RESULT_SCHEMA = "spc-boundary-prefix-v1"
ROOT_SESSION_CERTIFICATE_SCHEMA = "spc-root-session-certificate-v1"
ROOT_SESSION_CONTRACT_SCHEMA = "spc-root-session-contract-v1"
MATE_CERTIFICATE_SCHEMA = "spc-series-mate-certificate-v1"
MIN_PREFIX_DIFFERENTIAL_CASES = 14
MIN_MATE_DIFFERENTIAL_CASES = 5
PREFIX_HARD_LIMITS = {
    "maximum_fen_utf8_bytes": 512,
    "maximum_series_number": 256,
    "maximum_quiet_series": 1_000_000,
    "maximum_ep_targets": 8,
    "maximum_ep_utf8_bytes": 23,
    "maximum_prefix_moves": 256,
    "maximum_prefix_utf8_bytes": 1_535,
    "maximum_uci_move_bytes": 5,
    "maximum_promoted_hex_bytes": 18,
}
HEX_16 = re.compile(r"[0-9a-f]{16}")
HEX_64 = re.compile(r"[0-9a-f]{64}")
MAX_INITIAL_MEMORY_BYTES = 128 * 1024 * 1024
MAXIMUM_MEMORY_BYTES = 256 * 1024 * 1024
MAX_ESTIMATED_PEAK_MEMORY_BYTES = 192 * 1024 * 1024
COMBINED_EXPORTS = [
    "_spc_start_kernel_search_json",
    "_spc_boundary_kernel_search_json",
    "_spc_boundary_prefix_json",
    "_spc_boundary_prefix_contract_json",
    "_spc_start_kernel_abi_version",
    "_spc_root_session_contract_json",
    "_spc_root_session_create_json",
    "_spc_root_session_enumerate_json",
    "_spc_root_session_import_json",
    "_spc_root_session_search_json",
    "_spc_root_session_destroy",
    "_spc_root_session_abi_version",
    "_spc_series_mate_search_json",
    "_spc_series_mate_abi_version",
    "_malloc",
    "_free",
]
ROOT_SESSION_CONFIG_KEYS = {
    "max_depth",
    "width",
    "max_work",
    "mate_score",
    "series_cache_capacity",
    "external_cache_weight",
    "worker_threads",
    "root_tactical_protection",
    "root_contract_tt_capacity",
    "root_contract_eval_capacity",
    "weights",
}
ROOT_SESSION_WEIGHT_KEYS = {
    "material",
    "king_space",
    "series_reach",
    "promotion_corridors",
    "immediate_vulnerability",
    "useful_mobility",
    "boundary_check",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def engine_source_fingerprint(package: Path) -> str:
    digest = hashlib.sha256()
    paths = (
        path
        for pattern in ("*.py", "*.cpp", "*.hpp", "*.h")
        for path in package.rglob(pattern)
    )
    for path in sorted(paths, key=lambda item: item.relative_to(package).as_posix()):
        digest.update(path.relative_to(package).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return value


def load_certificate(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read safety certificate: {error}") from error
    return _require_mapping(payload, "safety certificate")


def validate_memory_limits(value: object) -> dict[str, int | bool]:
    memory = _require_mapping(value, "certificate memory")
    expected_keys = {
        "initial_bytes",
        "maximum_bytes",
        "estimated_peak_bytes",
        "growth_enabled",
    }
    if set(memory) != expected_keys:
        raise ValueError("certificate memory must exactly name the memory envelope")
    normalized: dict[str, int | bool] = {}
    for key, cap in (
        ("initial_bytes", MAX_INITIAL_MEMORY_BYTES),
        ("maximum_bytes", MAXIMUM_MEMORY_BYTES),
        ("estimated_peak_bytes", MAX_ESTIMATED_PEAK_MEMORY_BYTES),
    ):
        number = memory.get(key)
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
            or number % 65_536
            or number > cap
        ):
            raise ValueError(
                f"certificate memory {key} must be a positive 64KiB-aligned "
                f"integer no larger than {cap}"
            )
        normalized[key] = number
    growth_enabled = memory.get("growth_enabled")
    if not isinstance(growth_enabled, bool):
        raise ValueError("certificate memory growth_enabled must be a boolean")
    normalized["growth_enabled"] = growth_enabled
    if normalized["initial_bytes"] > normalized["estimated_peak_bytes"]:
        raise ValueError("certificate initial memory exceeds estimated peak memory")
    if normalized["estimated_peak_bytes"] > normalized["maximum_bytes"]:
        raise ValueError("certificate estimated peak memory exceeds maximum memory")
    if not growth_enabled and normalized["initial_bytes"] != normalized["maximum_bytes"]:
        raise ValueError("fixed-memory certificates require equal initial and maximum memory")
    return normalized


def validate_prefix_contract(value: object) -> dict[str, object]:
    contract = _require_mapping(value, "prefix contract")
    expected = {
        "schema": PREFIX_CONTRACT_SCHEMA,
        "result_schema": PREFIX_RESULT_SCHEMA,
        "abi_version": 1,
        "chess960": False,
        "promoted_hex_required_for_product": True,
    }
    for key, expected_value in expected.items():
        if contract.get(key) != expected_value:
            raise ValueError(
                f"prefix contract {key!r} must be {expected_value!r}"
            )
    raw_limits = _require_mapping(contract.get("limits"), "prefix contract limits")
    if set(raw_limits) != set(PREFIX_HARD_LIMITS):
        raise ValueError("prefix contract limits must exactly name the hard ABI envelope")
    limits: dict[str, int] = {}
    for key, hard_maximum in PREFIX_HARD_LIMITS.items():
        value = raw_limits.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= hard_maximum
        ):
            raise ValueError(
                f"prefix contract limit {key} must be from 1 through {hard_maximum}"
            )
        limits[key] = value
    if limits["maximum_prefix_moves"] > limits["maximum_series_number"]:
        raise ValueError("prefix move limit exceeds the certified series-number limit")
    return {**expected, "limits": limits}


def validate_prefix_certificate(
    certificate: Mapping[str, Any],
    *,
    source_fingerprint: str,
    wasm_sha256: str,
    module_js_sha256: str,
    runtime_variant: str,
    thread_count: int,
    support_files: list[dict[str, str]],
) -> tuple[dict[str, object], dict[str, int | bool], dict[str, str]]:
    expected = {
        "status": "certified",
        "contract_version": 1,
        "source_fingerprint": source_fingerprint,
        "wasm_sha256": wasm_sha256,
        "module_js_sha256": module_js_sha256,
        "runtime_variant": runtime_variant,
        "thread_count": thread_count,
        "support_files": support_files,
    }
    for key, expected_value in expected.items():
        if certificate.get(key) != expected_value:
            raise ValueError(
                f"prefix certificate {key!r} does not match the artifact: "
                f"expected {expected_value!r}, found {certificate.get(key)!r}"
            )
    certificate_id = certificate.get("certificate_id")
    if not isinstance(certificate_id, str) or not certificate_id.strip():
        raise ValueError("prefix certificate requires a non-empty certificate_id")
    evidence = _require_mapping(certificate.get("evidence"), "prefix evidence")
    required_evidence = {
        "failures": 0,
        "compiled_prefix_replay": True,
        "multi_ep_san": True,
        "illegal_prefix_fail_closed": True,
    }
    for key, expected_value in required_evidence.items():
        if evidence.get(key) != expected_value:
            raise ValueError(
                f"prefix certificate evidence {key!r} must be {expected_value!r}"
            )
    differential_cases = evidence.get("differential_cases")
    if (
        isinstance(differential_cases, bool)
        or not isinstance(differential_cases, int)
        or differential_cases < MIN_PREFIX_DIFFERENTIAL_CASES
    ):
        raise ValueError(
            "prefix certificate requires at least "
            f"{MIN_PREFIX_DIFFERENTIAL_CASES} differential cases"
        )
    engine = _require_mapping(certificate.get("engine"), "prefix engine identity")
    engine_identity: dict[str, str] = {}
    for key in ("engine_version", "ruleset_version"):
        value = engine.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"prefix certificate engine requires non-empty {key}")
        engine_identity[key] = value
    contract = validate_prefix_contract(certificate.get("prefix_contract"))
    memory = validate_memory_limits(certificate.get("memory"))
    return contract, memory, engine_identity


def _validate_combined_capability_identity(
    certificate: Mapping[str, Any],
    *,
    schema: str,
    abi_version: int,
    source_fingerprint: str,
    wasm_sha256: str,
    module_js_sha256: str,
    runtime_variant: str,
    thread_count: int,
    support_files: list[dict[str, str]],
) -> tuple[dict[str, int | bool], dict[str, str], str, str]:
    expected = {
        "schema": schema,
        "status": "certified",
        "contract_version": 1,
        "abi_version": abi_version,
        "product_publishable": False,
        "source_fingerprint": source_fingerprint,
        "wasm_sha256": wasm_sha256,
        "module_js_sha256": module_js_sha256,
        "runtime_variant": runtime_variant,
        "thread_count": thread_count,
        "support_files": support_files,
        "exports": COMBINED_EXPORTS,
    }
    for key, expected_value in expected.items():
        if certificate.get(key) != expected_value:
            raise ValueError(
                f"{schema} {key!r} does not match the combined artifact: "
                f"expected {expected_value!r}, found {certificate.get(key)!r}"
            )
    certificate_id = certificate.get("certificate_id")
    if not isinstance(certificate_id, str) or not certificate_id.strip():
        raise ValueError(f"{schema} requires a non-empty certificate_id")
    kernel_sha256 = certificate.get("kernel_sha256")
    if not isinstance(kernel_sha256, str) or not HEX_64.fullmatch(kernel_sha256):
        raise ValueError(f"{schema} requires a lowercase kernel_sha256")
    exception_strategy = certificate.get("exception_strategy")
    if exception_strategy not in {"emscripten", "wasm"}:
        raise ValueError(f"{schema} must bind the compiled exception strategy")
    wasm_simd = certificate.get("wasm_simd")
    if not isinstance(wasm_simd, bool):
        raise ValueError(f"{schema} must bind whether Wasm SIMD was compiled")
    allocator = certificate.get("allocator")
    if allocator not in {"dlmalloc", "emmalloc"}:
        raise ValueError(f"{schema} must bind the compiled allocator")
    expected_runtime = {
        "ordinary_module_worker": True,
        "pthreads": False,
        "cross_origin_isolated": False,
        "native_wasm_exception_handling": exception_strategy == "wasm",
        "wasm_simd": wasm_simd,
    }
    if certificate.get("runtime_requirements") != expected_runtime:
        raise ValueError(f"{schema} has inconsistent runtime requirements")
    engine = _require_mapping(certificate.get("engine"), f"{schema} engine")
    engine_identity: dict[str, str] = {}
    for key in ("engine_version", "ruleset_version", "profile_id"):
        value = engine.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{schema} engine requires non-empty {key}")
        engine_identity[key] = value
    return (
        validate_memory_limits(certificate.get("memory")),
        engine_identity,
        kernel_sha256,
        exception_strategy,
    )


def _validate_root_session_config(
    value: object,
    contract: Mapping[str, Any],
) -> dict[str, object]:
    config = _require_mapping(value, "root-session certified config")
    if set(config) != ROOT_SESSION_CONFIG_KEYS:
        raise ValueError("root-session config must exactly bind every native field")
    hard_limits = _require_mapping(
        contract.get("hard_limits"),
        "root-session contract hard limits",
    )

    def bounded_integer(
        key: str,
        minimum: int,
        maximum: int,
    ) -> int:
        candidate = config.get(key)
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or not minimum <= candidate <= maximum
        ):
            raise ValueError(f"root-session config {key} is outside its hard limit")
        return candidate

    integer_limits: dict[str, int] = {}
    for key in (
        "minimum_depth",
        "maximum_depth",
        "minimum_width",
        "maximum_width",
        "minimum_max_work",
        "maximum_max_work",
        "minimum_mate_score",
        "maximum_mate_score",
        "minimum_series_cache_capacity",
        "maximum_series_cache_capacity",
        "minimum_external_cache_weight",
        "worker_threads",
        "minimum_tt_capacity",
        "maximum_tt_capacity",
        "minimum_eval_capacity",
        "maximum_eval_capacity",
        "minimum_weight",
        "maximum_weight",
    ):
        limit = hard_limits.get(key)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError(f"root-session contract has invalid {key}")
        integer_limits[key] = limit

    max_depth = bounded_integer(
        "max_depth",
        integer_limits["minimum_depth"],
        integer_limits["maximum_depth"],
    )
    width = bounded_integer(
        "width",
        integer_limits["minimum_width"],
        integer_limits["maximum_width"],
    )
    max_work = bounded_integer(
        "max_work",
        integer_limits["minimum_max_work"],
        integer_limits["maximum_max_work"],
    )
    mate_score = bounded_integer(
        "mate_score",
        integer_limits["minimum_mate_score"],
        integer_limits["maximum_mate_score"],
    )
    series_cache = bounded_integer(
        "series_cache_capacity",
        integer_limits["minimum_series_cache_capacity"],
        integer_limits["maximum_series_cache_capacity"],
    )
    external_cache = bounded_integer(
        "external_cache_weight",
        integer_limits["minimum_external_cache_weight"],
        series_cache,
    )
    worker_threads = bounded_integer(
        "worker_threads",
        integer_limits["worker_threads"],
        integer_limits["worker_threads"],
    )
    if worker_threads != 1 or hard_limits.get(
        "external_cache_weight_lte_series_cache_capacity"
    ) is not True:
        raise ValueError("ordinary-Worker root sessions require worker_threads=1")
    tactical = config.get("root_tactical_protection")
    if (
        tactical is not False
        or hard_limits.get("root_tactical_protection_values") != [False]
        or hard_limits.get("root_tactical_policy")
        != "canonical-boundary-policy-v1"
    ):
        raise ValueError(
            "root-session config must use canonical boundary tactical policy"
        )
    tt_capacity = bounded_integer(
        "root_contract_tt_capacity",
        integer_limits["minimum_tt_capacity"],
        integer_limits["maximum_tt_capacity"],
    )
    eval_capacity = bounded_integer(
        "root_contract_eval_capacity",
        integer_limits["minimum_eval_capacity"],
        integer_limits["maximum_eval_capacity"],
    )
    weights = _require_mapping(config.get("weights"), "root-session weights")
    if set(weights) != ROOT_SESSION_WEIGHT_KEYS:
        raise ValueError("root-session config must exactly bind all seven weights")
    normalized_weights: dict[str, int] = {}
    for key in sorted(ROOT_SESSION_WEIGHT_KEYS):
        candidate = weights.get(key)
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or not integer_limits["minimum_weight"]
            <= candidate
            <= integer_limits["maximum_weight"]
        ):
            raise ValueError(f"root-session weight {key} is invalid")
        normalized_weights[key] = candidate
    return {
        "max_depth": max_depth,
        "width": width,
        "max_work": max_work,
        "mate_score": mate_score,
        "series_cache_capacity": series_cache,
        "external_cache_weight": external_cache,
        "worker_threads": worker_threads,
        "root_tactical_protection": tactical,
        "root_contract_tt_capacity": tt_capacity,
        "root_contract_eval_capacity": eval_capacity,
        "weights": normalized_weights,
    }


def _validate_root_geometry(
    value: object,
    memory: Mapping[str, int | bool],
    contract: Mapping[str, Any],
) -> dict[str, object]:
    geometry = _require_mapping(value, "root-session geometry")
    expected_keys = {
        "desktop_workers",
        "desktop_initial_full_wave",
        "aggregate_maximum_bytes",
        "supported_lower_geometries",
        "session_config",
        "play_limits",
    }
    if set(geometry) != expected_keys:
        raise ValueError("root-session geometry must exactly name its pool envelope")
    workers = geometry.get("desktop_workers")
    wave = geometry.get("desktop_initial_full_wave")
    aggregate = geometry.get("aggregate_maximum_bytes")
    maximum = int(memory["maximum_bytes"])
    if workers != 8 or wave != 4 or aggregate != workers * maximum:
        raise ValueError("desktop root geometry must certify workers=8 and wave=4")
    lower = geometry.get("supported_lower_geometries")
    if not isinstance(lower, list):
        raise ValueError("supported lower root geometries must be an array")
    normalized_lower: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    for item in lower:
        candidate = _require_mapping(item, "lower root geometry")
        if set(candidate) != {
            "workers",
            "initial_full_wave",
            "aggregate_maximum_bytes",
        }:
            raise ValueError("lower root geometry has unknown or missing fields")
        lower_workers = candidate.get("workers")
        lower_wave = candidate.get("initial_full_wave")
        lower_aggregate = candidate.get("aggregate_maximum_bytes")
        if (
            isinstance(lower_workers, bool)
            or not isinstance(lower_workers, int)
            or not 1 <= lower_workers < workers
            or isinstance(lower_wave, bool)
            or not isinstance(lower_wave, int)
            or not 1 <= lower_wave <= lower_workers
            or lower_aggregate != lower_workers * maximum
            or (lower_workers, lower_wave) in seen
        ):
            raise ValueError("lower root geometry is invalid or duplicated")
        seen.add((lower_workers, lower_wave))
        normalized_lower.append(dict(candidate))
    if normalized_lower != sorted(
        normalized_lower,
        key=lambda item: (item["workers"], item["initial_full_wave"]),
        reverse=True,
    ):
        raise ValueError("lower root geometries must use fastest-first canonical order")
    session_config = _validate_root_session_config(
        geometry.get("session_config"),
        contract,
    )
    play_limits = _require_mapping(
        geometry.get("play_limits"),
        "root-session play limits",
    )
    if set(play_limits) != {
        "maximum_seconds",
        "default_seconds",
        "default_generation_positions",
        "safety_reserve_positions",
    }:
        raise ValueError("root-session play limits must exactly bind four fields")
    maximum_seconds = play_limits.get("maximum_seconds")
    default_seconds = play_limits.get("default_seconds")
    if (
        isinstance(maximum_seconds, bool)
        or not isinstance(maximum_seconds, (int, float))
        or not 0 < float(maximum_seconds) <= 0xFFFFFFFF / 1000
        or isinstance(default_seconds, bool)
        or not isinstance(default_seconds, (int, float))
        or not 0 < float(default_seconds) <= float(maximum_seconds)
    ):
        raise ValueError("root-session play seconds are invalid")
    default_work = play_limits.get("default_generation_positions")
    safety_reserve = play_limits.get("safety_reserve_positions")
    if (
        isinstance(default_work, bool)
        or not isinstance(default_work, int)
        or not 1_000 <= default_work <= session_config["max_work"]
        or isinstance(safety_reserve, bool)
        or not isinstance(safety_reserve, int)
        or not 1 <= safety_reserve <= session_config["max_work"]
    ):
        raise ValueError("root-session play work limits are invalid")
    return {
        "desktop_workers": workers,
        "desktop_initial_full_wave": wave,
        "aggregate_maximum_bytes": aggregate,
        "supported_lower_geometries": normalized_lower,
        "session_config": session_config,
        "play_limits": {
            "maximum_seconds": maximum_seconds,
            "default_seconds": default_seconds,
            "default_generation_positions": default_work,
            "safety_reserve_positions": safety_reserve,
        },
    }


def validate_root_session_certificate(
    certificate: Mapping[str, Any],
    *,
    source_fingerprint: str,
    wasm_sha256: str,
    module_js_sha256: str,
    runtime_variant: str,
    thread_count: int,
    support_files: list[dict[str, str]],
) -> tuple[
    dict[str, int | bool],
    dict[str, str],
    str,
    str,
    dict[str, object],
    dict[str, object],
]:
    memory, engine, kernel_sha256, exception_strategy = (
        _validate_combined_capability_identity(
            certificate,
            schema=ROOT_SESSION_CERTIFICATE_SCHEMA,
            abi_version=2,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant=runtime_variant,
            thread_count=thread_count,
            support_files=support_files,
        )
    )
    if (
        certificate.get("root_session_certified") is not True
        or certificate.get("reply_mate_safety") is not False
    ):
        raise ValueError("root-session certificate must not claim reply-mate safety")
    contract = _require_mapping(
        certificate.get("root_session_contract"),
        "root-session contract",
    )
    if (
        contract.get("schema") != ROOT_SESSION_CONTRACT_SCHEMA
        or contract.get("abi_version") != 2
        or contract.get("worker_threads") != 1
        or contract.get("pthreads_required") is not False
        or contract.get("one_active_session_per_worker") is not True
        or contract.get("product_publishable") is not False
        or contract.get("reply_mate_safety") is not False
    ):
        raise ValueError("root-session certificate carries an invalid native contract")
    capabilities = _require_mapping(
        contract.get("capabilities"),
        "root-session contract capabilities",
    )
    if any(
        capabilities.get(key) is not True
        for key in (
            "enumerate",
            "import",
            "search",
            "call_work_credit",
            "hard_memory_limit",
            "tt_scout_rollback",
            "persistent_depth_reuse",
            "selected_owner_certification",
            "canonical_root_tactical_policy",
        )
    ):
        raise ValueError("root-session contract lacks coordinator capabilities")
    evidence = _require_mapping(certificate.get("evidence"), "root-session evidence")
    required_true = (
        "deterministic_node_smoke",
        "combined_artifact",
        "enumerate_import_search",
        "exact_manifest_import",
        "persistent_d1_d2_session",
        "cumulative_work_and_cache_receipts",
        "configured_max_depth_rejected",
        "per_call_work_credit",
        "selected_owner_warm_exact_certification",
        "deadline_fail_closed",
        "work_limit_fail_closed",
        "browser_worker_smoke",
        "opera_worker_smoke",
    )
    if evidence.get("failures") != 0 or any(
        evidence.get(key) is not True for key in required_true
    ):
        raise ValueError("root-session certificate is missing required passing evidence")
    cases = evidence.get("differential_cases")
    elapsed = evidence.get("start_w32_d5_elapsed_seconds")
    if (
        isinstance(cases, bool)
        or not isinstance(cases, int)
        or cases < 1
        or evidence.get("start_w32_d5_completed_depth") != 5
        or evidence.get("start_w32_d5_width") != 32
        or isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not 0 <= float(elapsed) < 60
    ):
        raise ValueError("root-session certificate lacks the exact W32 D5 gate")
    geometry = _validate_root_geometry(
        certificate.get("geometry"),
        memory,
        contract,
    )
    return (
        memory,
        engine,
        kernel_sha256,
        exception_strategy,
        dict(contract),
        geometry,
    )


def validate_mate_certificate(
    certificate: Mapping[str, Any],
    *,
    source_fingerprint: str,
    wasm_sha256: str,
    module_js_sha256: str,
    runtime_variant: str,
    thread_count: int,
    support_files: list[dict[str, str]],
) -> tuple[dict[str, int | bool], dict[str, str], str, str]:
    memory, engine, kernel_sha256, exception_strategy = (
        _validate_combined_capability_identity(
            certificate,
            schema=MATE_CERTIFICATE_SCHEMA,
            abi_version=1,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant=runtime_variant,
            thread_count=thread_count,
            support_files=support_files,
        )
    )
    if (
        certificate.get("mate_capability_certified") is not True
        or certificate.get("reply_mate_safety") is not True
    ):
        raise ValueError("mate certificate must explicitly certify reply-mate safety")
    evidence = _require_mapping(certificate.get("evidence"), "mate evidence")
    required_true = (
        "combined_artifact",
        "python_parity",
        "authoritative_replay",
        "white_found",
        "black_found",
        "exhausted",
        "work_limit_unknown",
        "deadline_unknown",
        "signed_mate_distance_overrides",
        "proof_bounds",
        "work_receipts",
        "deadline_receipts",
        "browser_worker_smoke",
    )
    cases = evidence.get("differential_cases")
    if (
        evidence.get("failures") != 0
        or any(evidence.get(key) is not True for key in required_true)
        or isinstance(cases, bool)
        or not isinstance(cases, int)
        or cases < MIN_MATE_DIFFERENTIAL_CASES
    ):
        raise ValueError("mate certificate is missing required parity evidence")
    return memory, engine, kernel_sha256, exception_strategy


def validate_certificate(
    certificate: Mapping[str, Any],
    *,
    source_fingerprint: str,
    wasm_sha256: str,
    module_js_sha256: str,
    runtime_variant: str,
    thread_count: int,
    support_files: list[dict[str, str]],
) -> dict[str, int | bool]:
    expected = {
        "schema": CERTIFICATE_SCHEMA,
        "status": "certified",
        "safety_certified": True,
        "contract_version": 1,
        "abi_version": 1,
        "source_fingerprint": source_fingerprint,
        "wasm_sha256": wasm_sha256,
        "module_js_sha256": module_js_sha256,
        "runtime_variant": runtime_variant,
        "thread_count": thread_count,
        "support_files": support_files,
    }
    for key, value in expected.items():
        if certificate.get(key) != value:
            raise ValueError(
                f"safety certificate {key!r} does not match the artifact: "
                f"expected {value!r}, found {certificate.get(key)!r}"
            )
    certificate_id = certificate.get("certificate_id")
    if not isinstance(certificate_id, str) or not certificate_id.strip():
        raise ValueError("safety certificate requires a non-empty certificate_id")
    evidence = _require_mapping(certificate.get("evidence"), "certificate evidence")
    required_evidence = {
        "failures": 0,
        "start_position_parity": True,
        "s4_mate_safety": True,
        "interrupted_depth_publication": True,
        "compiled_legal_series_validation": True,
        "compiled_authoritative_replay": True,
        "start_w32_d5_completed_depth": 5,
        "start_w32_d5_width": 32,
    }
    for key, value in required_evidence.items():
        if evidence.get(key) != value:
            raise ValueError(
                f"safety certificate evidence {key!r} must be {value!r}"
            )
    differential_cases = evidence.get("differential_cases")
    if (
        isinstance(differential_cases, bool)
        or not isinstance(differential_cases, int)
        or differential_cases < 1
    ):
        raise ValueError("safety certificate requires positive differential_cases")
    elapsed = evidence.get("start_w32_d5_elapsed_seconds")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not 0 <= float(elapsed) < 60
    ):
        raise ValueError(
            "safety certificate requires a completed under-60-second W32 D5 receipt"
        )
    engine = _require_mapping(certificate.get("engine"), "certificate engine")
    for key in (
        "engine_profile_id",
        "engine_profile_name",
        "engine_version",
        "ruleset_version",
    ):
        value = engine.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"safety certificate engine requires non-empty {key}")
    limits = _require_mapping(engine.get("analysis_limits"), "engine analysis limits")
    integer_limits = {
        "maximum_depth": (1, 64),
        "maximum_max_series": (1, 16_384),
        "maximum_generation_positions": (1_000, 0xFFFFFFFF),
        "default_depth": (1, 64),
        "default_max_series": (1, 16_384),
        "default_generation_positions": (1_000, 0xFFFFFFFF),
    }
    for key, (minimum, maximum) in integer_limits.items():
        value = limits.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise ValueError(
                f"engine analysis limit {key} must be an integer from "
                f"{minimum} through {maximum}"
            )
    for key in ("maximum_seconds", "default_seconds"):
        value = limits.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 < float(value) <= 0xFFFFFFFF / 1000
        ):
            raise ValueError(f"engine analysis limit {key} must be positive")
    if limits["default_depth"] > limits["maximum_depth"]:
        raise ValueError("default_depth exceeds maximum_depth")
    if limits["default_max_series"] > limits["maximum_max_series"]:
        raise ValueError("default_max_series exceeds maximum_max_series")
    if limits["default_seconds"] > limits["maximum_seconds"]:
        raise ValueError("default_seconds exceeds maximum_seconds")
    if (
        limits["default_generation_positions"]
        > limits["maximum_generation_positions"]
    ):
        raise ValueError(
            "default_generation_positions exceeds maximum_generation_positions"
        )
    return validate_memory_limits(certificate.get("memory"))


def _safe_support_file(path: Path) -> None:
    if (
        not re.fullmatch(r"[A-Za-z0-9._-]+\.js", path.name)
        or ".." in path.name
    ):
        raise ValueError(f"unsafe WebAssembly support-file name: {path.name!r}")


def _build_variant(
    *,
    runtime_variant: str,
    wasm: Path,
    module_js: Path,
    certificate_path: Path | None,
    prefix_certificate_path: Path | None,
    root_session_certificate_path: Path | None,
    mate_certificate_path: Path | None,
    support_paths: tuple[Path, ...],
    source_fingerprint: str,
    destination: Path,
) -> Mapping[str, Any]:
    if runtime_variant == "single" and support_paths:
        raise ValueError(
            "the verified single-thread lane may not load external support files"
        )
    if all(
        path is None
        for path in (
            certificate_path,
            prefix_certificate_path,
            root_session_certificate_path,
            mate_certificate_path,
        )
    ):
        raise ValueError(
            f"{runtime_variant} requires at least one capability certificate"
        )
    required_paths = [
        (wasm, "WebAssembly binary"),
        (module_js, "Emscripten module"),
        *((path, "WebAssembly support file") for path in support_paths),
    ]
    if certificate_path is not None:
        required_paths.append((certificate_path, "safety certificate"))
    if prefix_certificate_path is not None:
        required_paths.append((prefix_certificate_path, "prefix certificate"))
    if root_session_certificate_path is not None:
        required_paths.append((root_session_certificate_path, "root-session certificate"))
    if mate_certificate_path is not None:
        required_paths.append((mate_certificate_path, "mate certificate"))
    for path, label in required_paths:
        if not path.is_file():
            raise FileNotFoundError(f"{runtime_variant} {label} is missing: {path}")
    certificate = load_certificate(certificate_path) if certificate_path else None
    prefix_certificate = (
        load_certificate(prefix_certificate_path)
        if prefix_certificate_path
        else None
    )
    root_session_certificate = (
        load_certificate(root_session_certificate_path)
        if root_session_certificate_path
        else None
    )
    mate_certificate = (
        load_certificate(mate_certificate_path)
        if mate_certificate_path
        else None
    )
    certificate_thread_counts = [
        value.get("thread_count")
        for value in (
            certificate,
            prefix_certificate,
            root_session_certificate,
            mate_certificate,
        )
        if value is not None
    ]
    if any(value != certificate_thread_counts[0] for value in certificate_thread_counts):
        raise ValueError("search and prefix certificates disagree on thread_count")
    thread_count = certificate_thread_counts[0]
    if (
        isinstance(thread_count, bool)
        or not isinstance(thread_count, int)
        or thread_count < 1
        or (runtime_variant == "single" and thread_count != 1)
        or (runtime_variant == "pthread" and thread_count < 2)
    ):
        raise ValueError(
            f"{runtime_variant} certificate has invalid thread_count"
        )
    wasm_sha256 = sha256_file(wasm)
    module_js_sha256 = sha256_file(module_js)
    support_files: list[dict[str, str]] = []
    seen_support_names: set[str] = set()
    for path in support_paths:
        _safe_support_file(path)
        if path.name in seen_support_names or path.name == "spc-engine.js":
            raise ValueError(f"duplicate WebAssembly support-file name: {path.name}")
        seen_support_names.add(path.name)
        support_files.append({"name": path.name, "sha256": sha256_file(path)})
    support_files.sort(key=lambda item: item["name"])
    search_memory = (
        validate_certificate(
            certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant=runtime_variant,
            thread_count=thread_count,
            support_files=support_files,
        )
        if certificate is not None
        else None
    )
    prefix_contract: dict[str, object] | None = None
    prefix_memory: dict[str, int | bool] | None = None
    prefix_engine: dict[str, str] | None = None
    if prefix_certificate is not None:
        prefix_contract, prefix_memory, prefix_engine = validate_prefix_certificate(
            prefix_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant=runtime_variant,
            thread_count=thread_count,
            support_files=support_files,
        )
    root_memory: dict[str, int | bool] | None = None
    root_engine: dict[str, str] | None = None
    root_kernel_sha256: str | None = None
    root_exception_strategy: str | None = None
    root_contract: dict[str, object] | None = None
    root_geometry: dict[str, object] | None = None
    if root_session_certificate is not None:
        (
            root_memory,
            root_engine,
            root_kernel_sha256,
            root_exception_strategy,
            root_contract,
            root_geometry,
        ) = validate_root_session_certificate(
            root_session_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant=runtime_variant,
            thread_count=thread_count,
            support_files=support_files,
        )
    mate_memory: dict[str, int | bool] | None = None
    mate_engine: dict[str, str] | None = None
    mate_kernel_sha256: str | None = None
    mate_exception_strategy: str | None = None
    if mate_certificate is not None:
        (
            mate_memory,
            mate_engine,
            mate_kernel_sha256,
            mate_exception_strategy,
        ) = validate_mate_certificate(
            mate_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant=runtime_variant,
            thread_count=thread_count,
            support_files=support_files,
        )
    if search_memory is not None and prefix_memory is not None:
        if search_memory != prefix_memory:
            raise ValueError(
                "search and prefix certificates require identical memory envelopes"
            )
        assert certificate is not None
        assert prefix_engine is not None
        search_engine = _require_mapping(certificate.get("engine"), "certificate engine")
        for key in ("engine_version", "ruleset_version"):
            if search_engine.get(key) != prefix_engine[key]:
                raise ValueError(
                    f"search and prefix certificates disagree on {key}"
                )
    capability_memories = [
        (name, memory)
        for name, memory in (
            ("search", search_memory),
            ("prefix", prefix_memory),
            ("root-session", root_memory),
            ("mate", mate_memory),
        )
        if memory is not None
    ]
    reference_memory = capability_memories[0][1]
    if any(memory != reference_memory for _, memory in capability_memories[1:]):
        raise ValueError("combined capability certificates have different memory envelopes")
    engine_versions: list[tuple[str, str, str]] = []
    if certificate is not None:
        search_engine_value = _require_mapping(certificate.get("engine"), "search engine")
        engine_versions.append(
            (
                "search",
                str(search_engine_value.get("engine_version", "")),
                str(search_engine_value.get("ruleset_version", "")),
            )
        )
    for name, engine in (
        ("prefix", prefix_engine),
        ("root-session", root_engine),
        ("mate", mate_engine),
    ):
        if engine is not None:
            engine_versions.append(
                (name, engine["engine_version"], engine["ruleset_version"])
            )
    reference_engine = engine_versions[0][1:]
    if any(identity[1:] != reference_engine for identity in engine_versions[1:]):
        raise ValueError("combined capability certificates have different engine identities")
    if root_engine is not None and mate_engine is not None:
        if root_engine["profile_id"] != mate_engine["profile_id"]:
            raise ValueError("root-session and mate certificates have different profiles")
    if certificate is not None and root_engine is not None:
        search_profile = _require_mapping(certificate.get("engine"), "search engine").get(
            "engine_profile_id"
        )
        if search_profile != root_engine["profile_id"]:
            raise ValueError("search and root-session certificates have different profiles")
    if (
        root_kernel_sha256 is not None
        and mate_kernel_sha256 is not None
        and root_kernel_sha256 != mate_kernel_sha256
    ):
        raise ValueError("root-session and mate certificates have different kernels")
    if (
        root_exception_strategy is not None
        and mate_exception_strategy is not None
        and root_exception_strategy != mate_exception_strategy
    ):
        raise ValueError("root-session and mate certificates have different exception strategies")
    if (
        root_session_certificate is not None
        and mate_certificate is not None
        and (
            root_session_certificate.get("wasm_simd")
            != mate_certificate.get("wasm_simd")
            or root_session_certificate.get("runtime_requirements")
            != mate_certificate.get("runtime_requirements")
            or root_session_certificate.get("allocator")
            != mate_certificate.get("allocator")
        )
    ):
        raise ValueError("root-session and mate certificates have different runtimes")

    destination.mkdir(parents=True)
    shutil.copyfile(wasm, destination / "spc-engine.wasm")
    shutil.copyfile(module_js, destination / "spc-engine.js")
    by_name = {path.name: path for path in support_paths}
    for item in support_files:
        shutil.copyfile(by_name[item["name"]], destination / item["name"])
    variant: dict[str, Any] = {
        "thread_count": thread_count,
        "wasm": "spc-engine.wasm",
        "wasm_sha256": wasm_sha256,
        "module_js": "spc-engine.js",
        "module_js_sha256": module_js_sha256,
        "support_files": support_files,
    }
    if root_kernel_sha256 is not None or mate_kernel_sha256 is not None:
        variant["kernel_sha256"] = root_kernel_sha256 or mate_kernel_sha256
    if certificate is not None:
        assert search_memory is not None
        variant["safety_certificate"] = {
            "schema": CERTIFICATE_SCHEMA,
            "status": "certified",
            "safety_certified": True,
            "certificate_id": certificate["certificate_id"],
            "contract_version": 1,
            "abi_version": 1,
            "source_fingerprint": source_fingerprint,
            "runtime_variant": runtime_variant,
            "thread_count": thread_count,
            "wasm_sha256": wasm_sha256,
            "module_js_sha256": module_js_sha256,
            "support_files": support_files,
            "memory": search_memory,
            "evidence": dict(_require_mapping(certificate["evidence"], "evidence")),
            "engine": dict(_require_mapping(certificate["engine"], "engine")),
        }
    if prefix_certificate is not None:
        assert prefix_contract is not None
        assert prefix_memory is not None
        assert prefix_engine is not None
        variant["prefix_certificate"] = {
            "status": "certified",
            "contract_version": 1,
            "certificate_id": prefix_certificate["certificate_id"],
            "source_fingerprint": source_fingerprint,
            "runtime_variant": runtime_variant,
            "thread_count": thread_count,
            "wasm_sha256": wasm_sha256,
            "module_js_sha256": module_js_sha256,
            "support_files": support_files,
            "memory": prefix_memory,
            "evidence": dict(
                _require_mapping(prefix_certificate["evidence"], "prefix evidence")
            ),
            "engine": prefix_engine,
            "prefix_contract": prefix_contract,
        }
    if root_session_certificate is not None:
        assert root_memory is not None
        assert root_engine is not None
        assert root_kernel_sha256 is not None
        assert root_exception_strategy is not None
        assert root_contract is not None
        assert root_geometry is not None
        variant["root_session_certificate"] = {
            "schema": ROOT_SESSION_CERTIFICATE_SCHEMA,
            "status": "certified",
            "certificate_id": root_session_certificate["certificate_id"],
            "contract_version": 1,
            "abi_version": 2,
            "root_session_certified": True,
            "reply_mate_safety": False,
            "product_publishable": False,
            "source_fingerprint": source_fingerprint,
            "kernel_sha256": root_kernel_sha256,
            "runtime_variant": runtime_variant,
            "thread_count": thread_count,
            "wasm_sha256": wasm_sha256,
            "module_js_sha256": module_js_sha256,
            "support_files": support_files,
            "exports": COMBINED_EXPORTS,
            "exception_strategy": root_exception_strategy,
            "wasm_simd": root_session_certificate["wasm_simd"],
            "allocator": root_session_certificate["allocator"],
            "runtime_requirements": dict(
                _require_mapping(
                    root_session_certificate["runtime_requirements"],
                    "root runtime requirements",
                )
            ),
            "memory": root_memory,
            "engine": root_engine,
            "root_session_contract": root_contract,
            "geometry": root_geometry,
            "evidence": dict(
                _require_mapping(root_session_certificate["evidence"], "root evidence")
            ),
        }
    if mate_certificate is not None:
        assert mate_memory is not None
        assert mate_engine is not None
        assert mate_kernel_sha256 is not None
        assert mate_exception_strategy is not None
        variant["mate_certificate"] = {
            "schema": MATE_CERTIFICATE_SCHEMA,
            "status": "certified",
            "certificate_id": mate_certificate["certificate_id"],
            "contract_version": 1,
            "abi_version": 1,
            "mate_capability_certified": True,
            "reply_mate_safety": True,
            "product_publishable": False,
            "source_fingerprint": source_fingerprint,
            "kernel_sha256": mate_kernel_sha256,
            "runtime_variant": runtime_variant,
            "thread_count": thread_count,
            "wasm_sha256": wasm_sha256,
            "module_js_sha256": module_js_sha256,
            "support_files": support_files,
            "exports": COMBINED_EXPORTS,
            "exception_strategy": mate_exception_strategy,
            "wasm_simd": mate_certificate["wasm_simd"],
            "allocator": mate_certificate["allocator"],
            "runtime_requirements": dict(
                _require_mapping(
                    mate_certificate["runtime_requirements"],
                    "mate runtime requirements",
                )
            ),
            "memory": mate_memory,
            "engine": mate_engine,
            "evidence": dict(
                _require_mapping(mate_certificate["evidence"], "mate evidence")
            ),
        }
    return variant


def build_bundle(
    *,
    single_wasm: Path,
    single_module_js: Path,
    single_certificate_path: Path | None = None,
    single_prefix_certificate_path: Path | None = None,
    single_root_session_certificate_path: Path | None = None,
    single_mate_certificate_path: Path | None = None,
    single_support_paths: tuple[Path, ...] = (),
    pthread_wasm: Path | None = None,
    pthread_module_js: Path | None = None,
    pthread_certificate_path: Path | None = None,
    pthread_support_paths: tuple[Path, ...] = (),
    source_package: Path,
    output: Path,
) -> Mapping[str, Any]:
    if not source_package.is_dir():
        raise FileNotFoundError(f"engine source package is missing: {source_package}")
    if output.exists():
        raise FileExistsError(f"browser engine output already exists: {output}")
    pthread_inputs = (
        pthread_wasm,
        pthread_module_js,
        pthread_certificate_path,
    )
    if any(value is not None for value in pthread_inputs) and not all(
        value is not None for value in pthread_inputs
    ):
        raise ValueError("pthread wasm, module, and certificate are all-or-none")
    if pthread_support_paths and not all(value is not None for value in pthread_inputs):
        raise ValueError("pthread support files require a pthread artifact")
    if any(value is not None for value in pthread_inputs) or pthread_support_paths:
        raise ValueError(
            "pthread publishing is disabled until its wrapper and worker support "
            "code execute from verified bytes"
        )

    source_fingerprint = engine_source_fingerprint(source_package)
    if not HEX_16.fullmatch(source_fingerprint):
        raise ValueError("calculated engine source fingerprint is invalid")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}.", dir=output.parent
    ) as temporary_name:
        staging = Path(temporary_name) / "bundle"
        staging.mkdir()
        variants: dict[str, Mapping[str, Any]] = {
            "single": _build_variant(
                runtime_variant="single",
                wasm=single_wasm,
                module_js=single_module_js,
                certificate_path=single_certificate_path,
                prefix_certificate_path=single_prefix_certificate_path,
                root_session_certificate_path=single_root_session_certificate_path,
                mate_certificate_path=single_mate_certificate_path,
                support_paths=single_support_paths,
                source_fingerprint=source_fingerprint,
                destination=staging / "single",
            )
        }
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "contract_version": 1,
            "abi_version": 1,
            "source_fingerprint": source_fingerprint,
            "variants": variants,
        }

        (staging / "browser-engine-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        validate_existing_bundle(staging, source_package)
        staging.replace(output)
    return manifest


def _manifest_asset_name(value: object, extension: str) -> str:
    if (
        not isinstance(value, str)
        or not value.endswith(extension)
        or not re.fullmatch(r"[A-Za-z0-9._-]+", value)
        or ".." in value
    ):
        raise ValueError(f"unsafe browser bundle asset name: {value!r}")
    return value


def validate_existing_bundle(bundle: Path, source_package: Path) -> Mapping[str, Any]:
    if not bundle.is_dir():
        raise FileNotFoundError(f"browser engine bundle is missing: {bundle}")
    if not source_package.is_dir():
        raise FileNotFoundError(f"engine source package is missing: {source_package}")
    manifest_path = bundle / "browser-engine-manifest.json"
    try:
        manifest = _require_mapping(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            "browser engine manifest",
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read browser engine manifest: {error}") from error
    source_fingerprint = engine_source_fingerprint(source_package)
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("contract_version") != 1
        or manifest.get("abi_version") != 1
        or manifest.get("source_fingerprint") != source_fingerprint
    ):
        raise ValueError("browser engine manifest does not match the checked-out source")
    variants = _require_mapping(manifest.get("variants"), "browser engine variants")
    if set(variants) != {"single"}:
        raise ValueError("only the verified single-thread browser lane may be published")
    variant = _require_mapping(variants["single"], "single browser engine variant")
    if variant.get("thread_count") != 1:
        raise ValueError("single browser engine variant must use exactly one thread")
    if variant.get("support_files") != []:
        raise ValueError("single browser engine variant may not load support files")
    wasm_name = _manifest_asset_name(variant.get("wasm"), ".wasm")
    module_name = _manifest_asset_name(variant.get("module_js"), ".js")
    lane = bundle / "single"
    wasm = lane / wasm_name
    module_js = lane / module_name
    if not wasm.is_file() or not module_js.is_file():
        raise FileNotFoundError("certified single-lane WASM artifacts are missing")
    wasm_sha256 = sha256_file(wasm)
    module_js_sha256 = sha256_file(module_js)
    if (
        not HEX_64.fullmatch(str(variant.get("wasm_sha256", "")))
        or not HEX_64.fullmatch(str(variant.get("module_js_sha256", "")))
        or variant.get("wasm_sha256") != wasm_sha256
        or variant.get("module_js_sha256") != module_js_sha256
    ):
        raise ValueError("browser engine bundle artifact hash mismatch")
    certificate_value = variant.get("safety_certificate")
    prefix_certificate_value = variant.get("prefix_certificate")
    root_certificate_value = variant.get("root_session_certificate")
    mate_certificate_value = variant.get("mate_certificate")
    if all(
        value is None
        for value in (
            certificate_value,
            prefix_certificate_value,
            root_certificate_value,
            mate_certificate_value,
        )
    ):
        raise ValueError("single browser lane has no certified capability")
    search_memory: dict[str, int | bool] | None = None
    search_engine: Mapping[str, Any] | None = None
    if certificate_value is not None:
        certificate = _require_mapping(
            certificate_value,
            "single safety certificate",
        )
        search_memory = validate_certificate(
            certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant="single",
            thread_count=1,
            support_files=[],
        )
        search_engine = _require_mapping(certificate.get("engine"), "certificate engine")
    if prefix_certificate_value is not None:
        prefix_certificate = _require_mapping(
            prefix_certificate_value,
            "single prefix certificate",
        )
        _, prefix_memory, prefix_engine = validate_prefix_certificate(
            prefix_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant="single",
            thread_count=1,
            support_files=[],
        )
        if search_memory is not None and prefix_memory != search_memory:
            raise ValueError(
                "search and prefix certificates require identical memory envelopes"
            )
        if search_engine is not None:
            for key in ("engine_version", "ruleset_version"):
                if search_engine.get(key) != prefix_engine[key]:
                    raise ValueError(
                        f"search and prefix certificates disagree on {key}"
                    )
    root_memory: dict[str, int | bool] | None = None
    root_engine: dict[str, str] | None = None
    root_kernel: str | None = None
    root_exception: str | None = None
    if root_certificate_value is not None:
        root_certificate = _require_mapping(
            root_certificate_value,
            "single root-session certificate",
        )
        (
            root_memory,
            root_engine,
            root_kernel,
            root_exception,
            _root_contract,
            _root_geometry,
        ) = validate_root_session_certificate(
            root_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant="single",
            thread_count=1,
            support_files=[],
        )
    mate_memory: dict[str, int | bool] | None = None
    mate_engine: dict[str, str] | None = None
    mate_kernel: str | None = None
    mate_exception: str | None = None
    if mate_certificate_value is not None:
        mate_certificate = _require_mapping(
            mate_certificate_value,
            "single mate certificate",
        )
        (
            mate_memory,
            mate_engine,
            mate_kernel,
            mate_exception,
        ) = validate_mate_certificate(
            mate_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant="single",
            thread_count=1,
            support_files=[],
        )
    memories = [
        value
        for value in (search_memory, prefix_memory if prefix_certificate_value else None, root_memory, mate_memory)
        if value is not None
    ]
    if any(value != memories[0] for value in memories[1:]):
        raise ValueError("combined capability certificates have different memory envelopes")
    kernels = [value for value in (root_kernel, mate_kernel) if value is not None]
    if kernels and (
        any(value != kernels[0] for value in kernels[1:])
        or variant.get("kernel_sha256") != kernels[0]
    ):
        raise ValueError("combined root/mate kernel identity mismatch")
    exceptions = [value for value in (root_exception, mate_exception) if value is not None]
    if any(value != exceptions[0] for value in exceptions[1:]):
        raise ValueError("combined root/mate exception strategy mismatch")
    if (
        root_certificate_value is not None
        and mate_certificate_value is not None
        and (
            root_certificate.get("wasm_simd") != mate_certificate.get("wasm_simd")
            or root_certificate.get("runtime_requirements")
            != mate_certificate.get("runtime_requirements")
            or root_certificate.get("allocator") != mate_certificate.get("allocator")
        )
    ):
        raise ValueError("combined root/mate runtime requirements mismatch")
    if root_engine is not None and mate_engine is not None and root_engine != mate_engine:
        raise ValueError("combined root/mate engine identity mismatch")
    if search_engine is not None and root_engine is not None:
        if (
            search_engine.get("engine_version") != root_engine["engine_version"]
            or search_engine.get("ruleset_version") != root_engine["ruleset_version"]
            or search_engine.get("engine_profile_id") != root_engine["profile_id"]
        ):
            raise ValueError("combined search/root engine identity mismatch")
    expected_files = {
        "browser-engine-manifest.json",
        f"single/{wasm_name}",
        f"single/{module_name}",
    }
    actual_files = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file()
    }
    if actual_files != expected_files:
        raise ValueError(
            "browser engine bundle contains missing or uncertified files: "
            f"expected {sorted(expected_files)!r}, found {sorted(actual_files)!r}"
        )
    return manifest


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Build a fail-closed, identity-bound browser WASM bundle."
    )
    parser.add_argument("--single-wasm", type=Path)
    parser.add_argument("--single-module-js", type=Path)
    parser.add_argument("--single-certificate", type=Path)
    parser.add_argument("--single-prefix-certificate", type=Path)
    parser.add_argument("--single-root-session-certificate", type=Path)
    parser.add_argument("--single-mate-certificate", type=Path)
    parser.add_argument("--single-support-file", type=Path, action="append", default=[])
    parser.add_argument("--pthread-wasm", type=Path)
    parser.add_argument("--pthread-module-js", type=Path)
    parser.add_argument("--pthread-certificate", type=Path)
    parser.add_argument("--pthread-support-file", type=Path, action="append", default=[])
    parser.add_argument(
        "--source-package",
        type=Path,
        default=root / "src" / "scottish_progressive",
    )
    parser.add_argument("--validate-existing", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.validate_existing is not None:
        if any(
            value is not None
            for value in (
                arguments.single_wasm,
                arguments.single_module_js,
                arguments.single_certificate,
                arguments.single_prefix_certificate,
                arguments.single_root_session_certificate,
                arguments.single_mate_certificate,
                arguments.pthread_wasm,
                arguments.pthread_module_js,
                arguments.pthread_certificate,
            )
        ) or arguments.single_support_file or arguments.pthread_support_file:
            parser.error("--validate-existing cannot be combined with build inputs")
        manifest = validate_existing_bundle(
            arguments.validate_existing.resolve(),
            arguments.source_package.resolve(),
        )
        print(json.dumps(manifest, sort_keys=True))
        return
    missing = [
        name
        for name, value in (
            ("--single-wasm", arguments.single_wasm),
            ("--single-module-js", arguments.single_module_js),
            ("--output", arguments.output),
        )
        if value is None
    ]
    if missing:
        parser.error(f"building a bundle requires {', '.join(missing)}")
    if (
        arguments.single_certificate is None
        and arguments.single_prefix_certificate is None
        and arguments.single_root_session_certificate is None
        and arguments.single_mate_certificate is None
    ):
        parser.error(
            "building a bundle requires --single-certificate, "
            "--single-prefix-certificate, --single-root-session-certificate, "
            "or --single-mate-certificate"
        )
    assert arguments.single_wasm is not None
    assert arguments.single_module_js is not None
    assert arguments.output is not None
    manifest = build_bundle(
        single_wasm=arguments.single_wasm.resolve(),
        single_module_js=arguments.single_module_js.resolve(),
        single_certificate_path=(
            arguments.single_certificate.resolve()
            if arguments.single_certificate
            else None
        ),
        single_prefix_certificate_path=(
            arguments.single_prefix_certificate.resolve()
            if arguments.single_prefix_certificate
            else None
        ),
        single_root_session_certificate_path=(
            arguments.single_root_session_certificate.resolve()
            if arguments.single_root_session_certificate
            else None
        ),
        single_mate_certificate_path=(
            arguments.single_mate_certificate.resolve()
            if arguments.single_mate_certificate
            else None
        ),
        single_support_paths=tuple(path.resolve() for path in arguments.single_support_file),
        pthread_wasm=(
            arguments.pthread_wasm.resolve() if arguments.pthread_wasm else None
        ),
        pthread_module_js=(
            arguments.pthread_module_js.resolve()
            if arguments.pthread_module_js
            else None
        ),
        pthread_certificate_path=(
            arguments.pthread_certificate.resolve()
            if arguments.pthread_certificate
            else None
        ),
        pthread_support_paths=tuple(
            path.resolve() for path in arguments.pthread_support_file
        ),
        source_package=arguments.source_package.resolve(),
        output=arguments.output.resolve(),
    )
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
