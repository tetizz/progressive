from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from benchmarks import build_opera_release_receipt as opera_receipt_builder
from benchmarks import build_root_d5_oracle as root_oracle_builder
from scripts import promote_browser_wasm_release as promoter


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _record(path: Path, base: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(base).as_posix(),
        "sha256": promoter._sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _hashed_case(name: str, **extra: object) -> dict[str, object]:
    input_sha256 = hashlib.sha256(f"input:{name}".encode()).hexdigest()
    output_sha256 = hashlib.sha256(f"output:{name}".encode()).hexdigest()
    return {
        "name": name,
        "input_sha256": input_sha256,
        "wasm_output_sha256": output_sha256,
        "oracle_output_sha256": output_sha256,
        "exact_match": True,
        **extra,
    }


def _valid_fixture(tmp_path: Path) -> dict[str, object]:
    repository = tmp_path / "repository"
    package = repository / "src" / "scottish_progressive"
    package.mkdir(parents=True)
    for index, relative in enumerate(promoter.KERNEL_SOURCES):
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"// source {index}: {relative}\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "tetizz")
    _git(repository, "config", "user.email", "tetizz@users.noreply.github.com")
    _git(repository, "add", "src/scottish_progressive")
    _git(repository, "commit", "-q", "-m", "fixture source")
    revision = _git(repository, "rev-parse", "HEAD")
    source_fingerprint = promoter.bundle_builder.engine_source_fingerprint(package)

    compiler = Path(sys.executable).resolve()
    compiler_version = subprocess.run(
        [str(compiler), "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    artifact_directory = tmp_path / "artifact"
    artifact_directory.mkdir()
    module_js = artifact_directory / "spc-root-session.mjs"
    wasm = artifact_directory / "spc-root-session.wasm"
    module_js.write_text(
        'export default async () => ({ wasm: locateFile("spc-root-session.wasm") });\n',
        encoding="utf-8",
    )
    wasm.write_bytes(b"\0asm\x01\0\0\0")

    source_records = [
        _record(repository / relative, repository)
        for relative in sorted(promoter.KERNEL_SOURCES)
    ]
    artifact_records = sorted(
        (_record(module_js, artifact_directory), _record(wasm, artifact_directory)),
        key=lambda item: str(item["path"]),
    )
    kernel_sha256 = promoter._canonical_sha256(source_records)
    artifact_set_sha256 = promoter._canonical_sha256(artifact_records)
    identity = {
        "source_revision": revision,
        "source_fingerprint": source_fingerprint,
        "kernel_sha256": kernel_sha256,
        "wasm_sha256": promoter._sha256_file(wasm),
        "module_js_sha256": promoter._sha256_file(module_js),
        "artifact_set_sha256": artifact_set_sha256,
    }
    runtime = {
        "exception_strategy": "emscripten",
        "wasm_simd": False,
        "allocator": "dlmalloc",
    }
    runtime_requirements = {
        "ordinary_module_worker": True,
        "pthreads": False,
        "cross_origin_isolated": False,
        "native_wasm_exception_handling": False,
        "wasm_simd": False,
    }
    full_memory = {
        "initial_bytes": 16 * 1024 * 1024,
        "estimated_peak_bytes": 96 * 1024 * 1024,
        "maximum_bytes": 128 * 1024 * 1024,
        "growth_enabled": True,
        "stack_bytes": 1024 * 1024,
        "hard_maximum_linked": True,
        "runtime_peak_verified": False,
    }
    memory = {
        key: full_memory[key]
        for key in (
            "initial_bytes",
            "maximum_bytes",
            "estimated_peak_bytes",
            "growth_enabled",
        )
    }
    engine = {
        "engine_version": "spc-test-v1",
        "ruleset_version": "scottish-modern-common-v1",
        "profile_id": "spc-release-test",
    }
    build_command = [
        str(compiler),
        str(package / "_native_eval.cpp"),
        str(package / "native_subtree.cpp"),
        str(package / "native_subtree_wasm.cpp"),
        str(package / "native_root_session_wasm.cpp"),
        str(package / "_native_mate.cpp"),
        "-I",
        str(package),
        "-std=c++20",
        "-O3",
        "-flto",
        "-fexceptions",
        "-DSPC_NATIVE_CORE_ONLY=1",
        "-DSPC_NATIVE_MATE_CORE_ONLY=1",
        "-sALLOW_MEMORY_GROWTH=1",
        f"-sINITIAL_MEMORY={full_memory['initial_bytes']}",
        f"-sMAXIMUM_MEMORY={full_memory['maximum_bytes']}",
        f"-sSTACK_SIZE={full_memory['stack_bytes']}",
        "-sABORTING_MALLOC=0",
        "-sMALLOC=dlmalloc",
        "-sUSE_PTHREADS=0",
        "-sWASM_WORKERS=0",
        "-sENVIRONMENT=worker,node",
        "-sMODULARIZE=1",
        "-sEXPORT_ES6=1",
        "-sFILESYSTEM=0",
        "-sDYNAMIC_EXECUTION=0",
        f"-sEXPORTED_FUNCTIONS={','.join(promoter.EXPORTED_FUNCTIONS)}",
        "-sEXPORTED_RUNTIME_METHODS=UTF8ToString,stringToNewUTF8,HEAPU8",
        "-o",
        str(module_js),
    ]
    build_receipt = {
        "schema": promoter.BUILD_SCHEMA,
        "status": "built-not-certified",
        "product_publishable": False,
        "certificate_id": None,
        **identity,
        "support_files": [],
        **engine,
        "source_inputs": source_records,
        "runtime_variant": "single",
        "thread_count": 1,
        "pthreads": False,
        "optimization": {
            "level": "O3",
            "lto": True,
            "exception_strategy": "emscripten",
            "exception_flag": "-fexceptions",
            "wasm_simd": False,
            "simd_flag": None,
            "allocator": "dlmalloc",
        },
        "runtime_requirements": runtime_requirements,
        "session_geometry": {
            "desktop_series_cache_capacity": 65_536,
            "root_contract_tt_capacity": 262_144,
            "root_contract_eval_capacity": 262_144,
        },
        "memory_envelope": full_memory,
        "abi": {
            "root_session_version": 2,
            "prefix_kernel_version": 1,
            "series_mate_version": 1,
            "exports": list(promoter.EXPORTED_FUNCTIONS),
            "reply_mate_safety": False,
            "canonical_root_tactical_policy": "canonical-boundary-policy-v1",
            "legacy_root_tactical_protection": False,
        },
        "toolchain": {
            "path": str(compiler),
            "sha256": promoter._sha256_file(compiler),
            "version": compiler_version,
        },
        "command": build_command,
        "artifacts": artifact_records,
        "artifact_set_sha256": artifact_set_sha256,
    }
    build_path = artifact_directory / "root-session-build-receipt.json"
    _write_json(build_path, build_receipt)

    root_contract = {
        "schema": promoter.bundle_builder.ROOT_SESSION_CONTRACT_SCHEMA,
        "abi_version": 2,
        "worker_threads": 1,
        "pthreads_required": False,
        "one_active_session_per_worker": True,
        "product_publishable": False,
        "reply_mate_safety": False,
        "capabilities": {
            "enumerate": True,
            "import": True,
            "search": True,
            "call_work_credit": True,
            "hard_memory_limit": True,
            "tt_scout_rollback": True,
            "persistent_depth_reuse": True,
            "selected_owner_certification": True,
            "canonical_root_tactical_policy": True,
        },
        "hard_limits": {
            "minimum_depth": 1,
            "maximum_depth": 8,
            "minimum_width": 1,
            "maximum_width": 512,
            "minimum_max_work": 1,
            "maximum_max_work": 9_007_199_254_740_991,
            "minimum_mate_score": 1,
            "maximum_mate_score": 1_000_000_000,
            "minimum_series_cache_capacity": 1,
            "maximum_series_cache_capacity": 1_048_576,
            "minimum_external_cache_weight": 0,
            "external_cache_weight_lte_series_cache_capacity": True,
            "worker_threads": 1,
            "root_tactical_protection_values": [False],
            "root_tactical_policy": "canonical-boundary-policy-v1",
            "minimum_tt_capacity": 1,
            "maximum_tt_capacity": 1_048_576,
            "minimum_eval_capacity": 1,
            "maximum_eval_capacity": 1_048_576,
            "minimum_weight": 25,
            "maximum_weight": 300,
        },
        "manifest": {
            "root_tactical_policy": "canonical-boundary-policy-v1",
        },
    }
    config = {
        "max_depth": 5,
        "width": 32,
        "max_work": 100_000_000,
        "mate_score": 1_000_000,
        "series_cache_capacity": 65_536,
        "external_cache_weight": 0,
        "worker_threads": 1,
        "root_tactical_protection": False,
        "root_contract_tt_capacity": 262_144,
        "root_contract_eval_capacity": 262_144,
        "weights": {
            "material": 100,
            "king_space": 100,
            "series_reach": 100,
            "promotion_corridors": 100,
            "immediate_vulnerability": 100,
            "useful_mobility": 100,
            "boundary_check": 100,
        },
    }
    prefix_contract = {
        "schema": promoter.bundle_builder.PREFIX_CONTRACT_SCHEMA,
        "result_schema": promoter.bundle_builder.PREFIX_RESULT_SCHEMA,
        "abi_version": 1,
        "chess960": False,
        "promoted_hex_required_for_product": True,
        "hard_limits": dict(promoter.bundle_builder.PREFIX_HARD_LIMITS),
    }
    root_smoke = {
        "schema": promoter.ROOT_SMOKE_SCHEMA,
        "status": "passed-not-certified",
        "product_publishable": False,
        "safety_certified": False,
        "certificate_id": None,
        **identity,
        **runtime,
        "runtime_requirements": runtime_requirements,
        "runtime_variant": "single",
        "thread_count": 1,
        "memory": {
            "configured": full_memory,
            "observed_bytes": full_memory["initial_bytes"],
            "native_peak_bytes": full_memory["initial_bytes"],
        },
        "gates": {
            "combined_exports": True,
            "root_contract_reply_mate_safety_false": True,
            "persistent_d1_d2_session": True,
            "selected_owner_warm_exact_certification": True,
            "cumulative_work_and_cache_receipts": True,
            "exact_manifest_import": True,
            "configured_max_depth_rejected": True,
            "work_limit_fail_closed": True,
            "deadline_fail_closed": True,
            "prefix_smoke": True,
            "mate_found_exhausted_unknown": True,
            "canonical_root_tactical_policy": True,
            "legacy_root_tactical_policy_rejected": True,
            "canonical_root_tactical_boundary_echoes": True,
        },
        "root_session_contract": root_contract,
        "prefix_contract": prefix_contract,
        "mate_receipts": {
            "found": {
                "kernel_status": "found",
                "proof_status": "found",
                "complete": True,
                "stats": {"positions_visited": 10, "moves_generated": 20},
            },
            "exhausted": {
                "kernel_status": "exhausted",
                "proof_status": "exhausted",
                "complete": True,
                "stats": {"positions_visited": 5, "moves_generated": 10},
            },
            "work_limit": {
                "kernel_status": "work_limit",
                "proof_status": "unknown",
                "complete": False,
                "stats": {"positions_visited": 1, "moves_generated": 2},
            },
            "deadline": {
                "kernel_status": "deadline",
                "proof_status": "unknown",
                "complete": False,
                "stats": {"positions_visited": 1, "moves_generated": 2},
            },
        },
    }

    principal_variation = [
        {
            "moves": ["b2b3"],
            "machine_notation": "b2b3",
            "child_boundary": {"series": 2, "side_to_move": "black"},
        }
    ]
    selected = {
        "candidate_identity": "candidate-00",
        "move": "b2b3",
        "score": 951,
        "proof_bounds": [-1, 1],
        "principal_variation": principal_variation,
        "principal_variation_sha256": promoter._canonical_sha256(principal_variation),
    }
    bounds = [
        {
            "candidate_identity": f"candidate-{index:02d}",
            "bound": "exact" if index == 0 else "upper",
            "score": 951 - index,
            "proof_bounds": [-1, 1],
        }
        for index in range(20)
    ]
    rivals = {
        "coverage_complete": True,
        "candidate_count": 20,
        "unknown_count": 0,
        "exact_count": 1,
        "lower_count": 0,
        "upper_count": 19,
        "bounds": bounds,
        "coverage_sha256": promoter._canonical_sha256(bounds),
    }
    oracle_artifact = {
        **identity,
        **runtime,
        "runtime_variant": "single",
        "thread_count": 1,
        **engine,
    }
    boundary = {
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "series": 1,
        "quiet_series": 0,
        "progressive_ep": [],
        "promoted_hex": "0000000000000000",
        "chess960": False,
    }
    retained_manifest_sha256 = "7" * 64
    semantic = {
        "schema": promoter.ROOT_PARITY_SCHEMA,
        "artifact": oracle_artifact,
        "boundary": boundary,
        "session_config": config,
        "memory": memory,
        "deadline": {"deadline_limit_ms": 300_000.0},
        "retained_manifest_sha256": retained_manifest_sha256,
        "selected": selected,
        "rival_bounds": rivals,
    }
    oracle_signature = promoter._canonical_sha256(semantic)
    root_oracle = {
        "schema": promoter.ROOT_PARITY_SCHEMA,
        "status": "passed",
        "failures": 0,
        "differential_cases": 512,
        "artifact": oracle_artifact,
        "boundary": boundary,
        "session_config": config,
        "memory": memory,
        "retained_manifest_sha256": retained_manifest_sha256,
        "selected": selected,
        "rival_bounds": rivals,
        "work": {
            "status": "complete",
            "within_cap": True,
            "unknown_or_limit_count": 0,
            "max_work": config["max_work"],
            "accounted_work": 61_000_000,
        },
        "deadline": {
            "status": "complete",
            "deadline_reached": False,
            "unknown_or_limit_count": 0,
            "deadline_limit_ms": 300_000,
            "remaining_time_ms": 240_000,
        },
        "gates": {
            "initial_root_enumeration_python_parity": True,
            "persistent_d1_d2_python_parity": True,
            "persistent_d1_through_d5_selects_same_result_as_fresh_d5": True,
            "exact_selected_replay": True,
            "work_receipts": True,
            "deadline_receipts": True,
            "complete_rival_bound_coverage": True,
        },
        "oracle_signature_sha256": oracle_signature,
    }

    prefix_cases = [_hashed_case(f"prefix-{index:02d}") for index in range(14)]
    prefix_parity = {
        "schema": promoter.PREFIX_PARITY_SCHEMA,
        "status": "passed",
        "failures": 0,
        "artifact": identity,
        "cases": prefix_cases,
        "case_set_sha256": promoter._canonical_sha256(prefix_cases),
        "progressive_san_corrections": 0,
        "progressive_san_exact_parity": 2,
        "fail_closed_errors": 3,
        "mate_replay": "checkmate",
        "multi_ep": "covered",
        "gates": {
            "exact_python_parity": True,
            "compiled_prefix_replay": True,
            "multi_ep_san": True,
            "illegal_prefix_fail_closed": True,
            "case_input_output_hashes": True,
        },
    }
    browser_prefix = {
        "schema": promoter.BROWSER_PREFIX_SCHEMA,
        "status": "passed",
        "artifact": identity,
        "exact_identity": True,
        "chess960_rejected": True,
        "certified_limits_enforced": True,
        "full_next_state_enforced": True,
        "same_series_terminal_covered": True,
        "final_frame_consistency_enforced": True,
        "malformed_local_fallback": True,
        "original_request_preserved": True,
        "remote_authority_bound": True,
        "cancellation_fallback_suppressed": True,
    }
    mate_cases = [
        _hashed_case("white-found", side_to_move="white", proof_status="found"),
        _hashed_case("black-found", side_to_move="black", proof_status="found"),
        _hashed_case("exhausted", side_to_move="white", proof_status="exhausted"),
        _hashed_case("work-unknown", side_to_move="white", proof_status="unknown"),
        _hashed_case("deadline-unknown", side_to_move="black", proof_status="unknown"),
    ]
    accelerated_specs = {
        "s7-staged-root-found": {
            "kernel_status": "found",
            "proof_status": "found",
            "complete": True,
            "moves": [
                "d2c3",
                "e1e2",
                "g1f3",
                "f3g5",
                "h1d1",
                "g5e6",
                "d1d8",
            ],
            "work": 48_777,
            "checkmates": 1,
            "max_depth_reached": 7,
        },
        "s7-staged-root-work-limit": {
            "kernel_status": "work_limit",
            "proof_status": "unknown",
            "complete": False,
            "moves": [],
            "work": 10,
            "checkmates": 0,
            "max_depth_reached": 0,
        },
        "s7-staged-root-exhausted": {
            "kernel_status": "exhausted",
            "proof_status": "exhausted",
            "complete": True,
            "moves": [],
            "work": 302,
            "checkmates": 0,
            "max_depth_reached": 0,
        },
        "s7-nonchecking-stuck-is-not-mate": {
            "kernel_status": "exhausted",
            "proof_status": "exhausted",
            "complete": True,
            "moves": [],
            "work": 1,
            "checkmates": 0,
            "max_depth_reached": 0,
        },
    }
    accelerated_cases = [
        {
            "name": name,
            "input_sha256": promoter._canonical_sha256({"input": name}),
            "wasm_output_sha256": promoter._canonical_sha256({"output": name}),
            **spec,
        }
        for name, spec in accelerated_specs.items()
    ]
    mate_parity = {
        "schema": promoter.MATE_PARITY_SCHEMA,
        "status": "passed",
        "failures": 0,
        "artifact": identity,
        "cases": mate_cases,
        "case_set_sha256": promoter._canonical_sha256(mate_cases),
        "accelerated_cases": accelerated_cases,
        "accelerated_case_set_sha256": promoter._canonical_sha256(
            accelerated_cases
        ),
        "gates": {
            "python_parity": True,
            "authoritative_replay": True,
            "white_found": True,
            "black_found": True,
            "exhausted": True,
            "work_limit_unknown": True,
            "deadline_unknown": True,
            "signed_mate_distance_overrides": True,
            "proof_bounds": True,
            "work_receipts": True,
            "deadline_receipts": True,
            "prefix_replay": True,
            "case_input_output_hashes": True,
            "late_series_staged_root": True,
        },
    }

    safety_reserve = 1_000_000
    iterations = []
    for depth in range(1, 6):
        committed = depth * 10_000_000
        iterations.append(
            {
                "depth": depth,
                "elapsed_ms": depth * 1_000.0,
                "candidate_identity": selected["candidate_identity"],
                "move": selected["move"],
                "score": selected["score"],
                "proof_bounds": selected["proof_bounds"],
                "principal_variation": principal_variation,
                "work": {
                    "max_work": config["max_work"],
                    "committed_work": committed,
                    "reserved_work": 0,
                    "remaining_work": config["max_work"] - committed,
                    "safety_reserve_work": safety_reserve,
                    "safety_committed_work": 10_000,
                    "within_cap": True,
                },
                "safety_status": "exhausted",
                "safety_revision": 1,
                "owner_worker_id": "root-0",
                "owner_certification_count": 1,
                "coverage_complete": True,
                "root_bounds": copy.deepcopy(bounds),
                "retained_manifest_sha256": retained_manifest_sha256,
                "order_shape_sha256": "8" * 64,
                "root_scores_complete": True,
                "width_complete": True,
                "final_replay": {
                    "complete": True,
                    "outcome": None,
                    "next_state": {"series": 2, "side_to_move": "black"},
                },
            }
        )
    result = {
        "completed_depth": 5,
        "candidate_identity": selected["candidate_identity"],
        "move": selected["move"],
        "score": selected["score"],
        "proof_bounds": selected["proof_bounds"],
        "principal_variation": principal_variation,
        "work": iterations[-1]["work"],
        "safety_status": "exhausted",
        "safety_revision": 1,
        "owner_worker_id": "root-0",
        "coverage_complete": True,
        "root_bounds": copy.deepcopy(bounds),
        "retained_manifest_sha256": retained_manifest_sha256,
        "order_shape_sha256": "8" * 64,
        "root_scores_complete": True,
        "width_complete": True,
    }
    worker_memory = [
        {
            "id": f"root-{index}",
            "peak_bytes": full_memory["initial_bytes"],
            "native_work_after": 100 + index,
        }
        for index in range(8)
    ]
    worker_identity = {
        "source_fingerprint": source_fingerprint,
        "kernel_sha256": kernel_sha256,
        "module_js_sha256": identity["module_js_sha256"],
        "certificate_id": "lab-not-certified-fixture",
        "runtime_variant": "single",
        "thread_count": 1,
        **engine,
    }
    worker_artifact = {
        **identity,
        **runtime,
    }
    environment_workers = [
        {
            "worker_id": f"root-{index}",
            "identity": worker_identity,
            "artifact": worker_artifact,
            "ordinary_module_worker": True,
            "worker_global_scope": "DedicatedWorkerGlobalScope",
        }
        for index in range(8)
    ]
    selected_signature = promoter._canonical_sha256(selected)

    def run_binding(elapsed_ms: float) -> dict[str, object]:
        run_bounds = copy.deepcopy(bounds)
        semantic = {
            "selected": selected,
            "retained_manifest_sha256": retained_manifest_sha256,
            "rival_bounds": run_bounds,
        }
        return {
            "status": "complete",
            "selected_signature_sha256": selected_signature,
            "run_signature_sha256": promoter._canonical_sha256(semantic),
            "selected_candidate_identity": selected["candidate_identity"],
            "unknown_or_limit_count": 0,
            "selected_owner_certification_count": 1,
            "elapsed_ms": elapsed_ms,
            "retained_manifest_sha256": retained_manifest_sha256,
            "rival_bounds": run_bounds,
            "root_coverage_sha256": promoter._canonical_sha256(run_bounds),
        }

    cold_binding = run_binding(40_000.0)
    warm_binding = run_binding(49_000.0)

    def schedule_binding(
        wave: int,
        order_shape_sha256: str,
        elapsed_ms: float,
    ) -> dict[str, object]:
        binding = run_binding(elapsed_ms)
        semantic = {
            "run_signature_sha256": binding["run_signature_sha256"],
            "workers": 8,
            "initial_full_wave": wave,
            "order_shape_sha256": order_shape_sha256,
        }
        return {
            "workers": 8,
            "initial_full_wave": wave,
            "order_shape_sha256": order_shape_sha256,
            **binding,
            "trial_signature_sha256": promoter._canonical_sha256(semantic),
        }

    wave_four_binding = schedule_binding(4, "8" * 64, 49_000.0)
    wave_two_binding = schedule_binding(2, "9" * 64, 51_000.0)
    opera_worker = {
        "schema": promoter.OPERA_WORKER_SCHEMA,
        "status": "passed-not-certified",
        "product_publishable": False,
        "safety_certified": True,
        "artifact": oracle_artifact,
        "geometry": {
            "workers": 8,
            "initial_full_wave": 4,
            "depth": 5,
            "width": 32,
            "max_work": config["max_work"],
            "safety_reserve_work": safety_reserve,
            "config": config,
        },
        "timings_ms": {
            "pool_ready": 500.0,
            "iterative_d1_through_d5": 49_000.0,
            "total_to_completed_depth": 49_500.0,
            "completed_depth_iteration": 5_000.0,
        },
        "result": result,
        "iterations": iterations,
        "oracle": {
            "schema": "spc-opera-root-d5-oracle-binding-v1",
            "oracle_signature_sha256": oracle_signature,
            "selected_signature_sha256": selected_signature,
            "cold_selected_matches_oracle": True,
            "warm_full_matches_oracle": True,
            "cold_d5": cold_binding,
            "warm_d1_through_d5": warm_binding,
        },
        "schedule_trials": [
            wave_four_binding,
            wave_two_binding,
        ],
        "memory": {
            "per_worker_hard_maximum_bytes": full_memory["maximum_bytes"],
            "aggregate_hard_maximum_bytes": 8 * full_memory["maximum_bytes"],
            "aggregate_observed_peak_bytes": sum(
                item["peak_bytes"] for item in worker_memory
            ),
            "workers": worker_memory,
        },
        "environment": {
            "ordinary_module_workers": True,
            "worker_count": 8,
            "worker_global_scope": "DedicatedWorkerGlobalScope",
            "hardware_concurrency": 16,
            "cross_origin_isolated": False,
            "workers": environment_workers,
        },
        "gates": {
            "exact_artifact_identity_all_workers": True,
            "ordinary_module_workers": True,
            "pthreads_disabled": True,
            "combined_prefix_root_mate_abi": True,
            "persistent_d1_through_d5_sessions": True,
            "exact_manifest_import_all_workers": True,
            "global_work_cap_enforced": True,
            "common_monotonic_deadline": True,
            "dynamic_work_pool_certified": True,
            "final_bound_coverage": True,
            "selected_owner_warm_exact_certification": True,
            "compiled_root_prefix_replay": True,
            "compiled_reply_mate_safety": True,
            "memory_envelope_observed": True,
            "d5_w32_anchor": True,
            "under_60_seconds_total": True,
            "cold_d5_selected_matches_oracle": True,
            "warm_d1_d5_full_matches_oracle": True,
            "alternate_schedule_selected_matches_oracle": True,
            "multiple_seed_wave_order_shapes": True,
            "no_unknown_or_limit_results": True,
            "release_certificate_present": False,
        },
    }
    opera_receipt = {
        "schema": promoter.OPERA_CDP_SCHEMA,
        "status": "passed-not-certified",
        "product_publishable": False,
        "safety_certified": False,
        "cdp": {
            "browser": "Chrome/150.0.7871.187",
            "protocol_version": "1.3",
            "user_agent": "Mozilla/5.0 fixture OPR/134.0.0.0",
            "web_socket_debugger_url_recorded": True,
        },
        "page_environment": {
            "title": "Progressive Opera root D5 benchmark",
            "userAgent": "Mozilla/5.0 fixture OPR/134.0.0.0",
            "hardwareConcurrency": 16,
            "crossOriginIsolated": False,
            "location": (
                "http://127.0.0.1:8876/benchmarks/opera_root_d5_probe.html"
                "?module=/artifact/spc-root-session.mjs"
                "&wasm=/artifact/spc-root-session.wasm"
                "&receipt=/artifact/root-session-build-receipt.json"
                "&depth=5&width=32&workers=8&wave=4"
                "&max_work=100000000&safety_work=1000000&timeout_ms=300000"
            ),
        },
        "worker_receipt": opera_worker,
    }

    payloads = {
        "build": build_receipt,
        "root_smoke": root_smoke,
        "root_parity": root_oracle,
        "prefix_parity": prefix_parity,
        "browser_prefix": browser_prefix,
        "mate_parity": mate_parity,
        "opera": opera_receipt,
    }
    paths = {"build": build_path}
    for label, payload in payloads.items():
        if label == "build":
            continue
        path = tmp_path / "evidence" / f"{label}.json"
        _write_json(path, payload)
        paths[label] = path
    return {
        "repository": repository,
        "package": package,
        "paths": paths,
        "payloads": payloads,
        "identity": identity,
        "wasm": wasm,
        "module_js": module_js,
    }


def _root_bound_fixture() -> tuple[dict[str, object], dict[str, object]]:
    principal_variation = [{"moves": ["b2b3"], "machine_notation": "b2b3"}]
    bounds = [
        {
            "candidate_identity": f"candidate-{index:02d}",
            "bound": "exact" if index == 0 else "upper",
            "score": 951 - index,
            "proof_bounds": [-1, 1],
        }
        for index in range(20)
    ]
    result = {
        "candidate_identity": "candidate-00",
        "move": "b2b3",
        "score": 951,
        "proof_bounds": [-1, 1],
        "principal_variation": principal_variation,
        "root_bounds": bounds,
    }
    oracle = {"rival_bounds": {"bounds": copy.deepcopy(bounds)}}
    return result, oracle


def _rewrite(fixture: dict[str, object], label: str, mutate) -> None:
    payloads = fixture["payloads"]
    paths = fixture["paths"]
    assert isinstance(payloads, dict) and isinstance(paths, dict)
    payload = copy.deepcopy(payloads[label])
    mutate(payload)
    _write_json(paths[label], payload)


def _validate(fixture: dict[str, object]) -> promoter.ValidatedEvidence:
    return promoter.validate_evidence(
        repository=fixture["repository"],
        source_package=fixture["package"],
        receipt_paths=fixture["paths"],
    )


def test_promotes_only_the_verified_bytes_and_emits_a_digest_receipt(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)
    evidence = _validate(fixture)
    certificates = promoter.build_certificates(
        evidence,
        maximum_seconds=60,
        default_seconds=60,
    )
    output = tmp_path / "release"
    release = promoter.promote_release(
        evidence,
        certificates,
        source_package=fixture["package"],
        output=output,
        authorized_by="tetizz",
        maximum_seconds=60,
        default_seconds=60,
    )

    assert release["status"] == "promoted"
    assert release["product_publishable"] is True
    assert release["authorization"]["authorized_by"] == "tetizz"
    assert release["gates"]["w32_d1_through_d5_under_60_seconds"] is True
    assert release["gates"]["canonical_root_tactical_boundary_policy"] is True
    assert release["root_tactical_policy"] == {
        "capability": True,
        "policy": "canonical-boundary-policy-v1",
        "legacy_wire_root_tactical_protection": False,
    }
    assert release["toolchain"]["sha256"] == promoter._sha256_file(
        Path(sys.executable)
    )
    for label, filename in promoter.RECEIPT_FILENAMES.items():
        assert promoter._sha256_file(output / "evidence" / filename) == promoter._sha256_file(
            fixture["paths"][label]
        )
    manifest = promoter.bundle_builder.validate_existing_bundle(
        output / "browser-engine",
        fixture["package"],
    )
    variant = manifest["variants"]["single"]
    assert variant["wasm_sha256"] == fixture["identity"]["wasm_sha256"]
    assert promoter._sha256_file(
        output / "browser-engine" / "single" / variant["wasm"]
    ) == promoter._sha256_file(fixture["wasm"])
    assert json.loads((output / "release-receipt.json").read_text(encoding="utf-8"))["release_id"] == release["release_id"]
    with pytest.raises(FileExistsError):
        promoter.promote_release(
            evidence,
            certificates,
            source_package=fixture["package"],
            output=output,
            authorized_by="tetizz",
            maximum_seconds=60,
            default_seconds=60,
        )


def test_rejects_legacy_unbound_prefix_receipt(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["schema"] = "spc-prefix-parity-receipt-v1"
        payload.pop("artifact")

    _rewrite(fixture, "prefix_parity", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="prefix parity receipt did not pass"):
        _validate(fixture)


def test_rejects_legacy_opera_worker_receipt(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["worker_receipt"]["schema"] = "spc-opera-root-d5-benchmark-v1"

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="Worker D1-D5 receipt did not pass"):
        _validate(fixture)


def test_rejects_missing_accelerated_mate_evidence(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload.pop("accelerated_cases")

    _rewrite(fixture, "mate_parity", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="accelerated mate cases"):
        _validate(fixture)


def test_rejects_resigned_accelerated_mate_work_drift(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        accelerated = payload["accelerated_cases"]
        accelerated[0]["work"] = 48_778
        payload["accelerated_case_set_sha256"] = promoter._canonical_sha256(
            accelerated
        )

    _rewrite(fixture, "mate_parity", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="result changed"):
        _validate(fixture)


def test_rejects_accelerated_mate_digest_tampering(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["accelerated_case_set_sha256"] = "f" * 64

    _rewrite(fixture, "mate_parity", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="case-set digest"):
        _validate(fixture)


def test_rejects_sixty_second_or_slower_opera_run(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["worker_receipt"]["timings_ms"]["total_to_completed_depth"] = 60_000

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="under 60 seconds"):
        _validate(fixture)


def test_rejects_schedule_shapes_that_do_not_reproduce_the_oracle(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["worker_receipt"]["schedule_trials"][1]["selected_signature_sha256"] = "f" * 64

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="did not reproduce the selected oracle result"):
        _validate(fixture)


def test_rejects_duplicate_alternate_root_candidates(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        trial = payload["worker_receipt"]["schedule_trials"][1]
        trial["rival_bounds"] = [copy.deepcopy(trial["rival_bounds"][0]) for _ in range(20)]

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="candidate universe exactly"):
        _validate(fixture)


def test_receipt_builder_rejects_duplicate_root_candidates() -> None:
    result, oracle = _root_bound_fixture()
    result["root_bounds"] = [copy.deepcopy(result["root_bounds"][0]) for _ in range(20)]

    with pytest.raises(ValueError, match="exact candidate universe"):
        opera_receipt_builder._normalize_bounds(result, oracle, "alternate run")


def test_receipt_builder_rejects_final_iteration_result_drift() -> None:
    result, _ = _root_bound_fixture()
    result["completed_depth"] = 5
    final = {
        "depth": 5,
        **{
            key: copy.deepcopy(result.get(key))
            for key in opera_receipt_builder.FINAL_RESULT_FIELDS
        },
    }
    final["score"] = result["score"] - 1

    with pytest.raises(ValueError, match="final iteration differs"):
        opera_receipt_builder._assert_final_result_identity(final, result, "alternate run")


def test_rejects_disjoint_cold_root_candidates(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        cold = payload["worker_receipt"]["oracle"]["cold_d5"]
        for index, bound in enumerate(cold["rival_bounds"]):
            bound["candidate_identity"] = f"fake-candidate-{index:02d}"

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="candidate universe exactly"):
        _validate(fixture)


def test_root_oracle_builder_rejects_disjoint_candidate_universes() -> None:
    result, oracle = _root_bound_fixture()
    warm = {"bounds": copy.deepcopy(oracle["rival_bounds"]["bounds"])}
    cold = {"bounds": copy.deepcopy(result["root_bounds"])}
    for index, bound in enumerate(cold["bounds"]):
        bound["candidate_identity"] = f"fake-candidate-{index:02d}"

    with pytest.raises(ValueError, match="different candidate universes"):
        root_oracle_builder._assert_same_candidate_universe(warm, cold)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("score", "951", "candidate score"),
        ("proof_bounds", [-1, True], "candidate proof bounds"),
    ],
)
def test_rejects_malformed_cold_bound_payloads(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["worker_receipt"]["oracle"]["cold_d5"]["rival_bounds"][0][field] = value

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match=message):
        _validate(fixture)


def test_rejects_tampered_alternate_coverage_digest(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["worker_receipt"]["schedule_trials"][1]["root_coverage_sha256"] = "f" * 64

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="root-coverage digest"):
        _validate(fixture)


def test_rejects_re_signed_wave_four_order_shape_drift(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        trial = payload["worker_receipt"]["schedule_trials"][0]
        trial["order_shape_sha256"] = "a" * 64
        trial["trial_signature_sha256"] = promoter._canonical_sha256(
            {
                "run_signature_sha256": trial["run_signature_sha256"],
                "workers": trial["workers"],
                "initial_full_wave": trial["initial_full_wave"],
                "order_shape_sha256": trial["order_shape_sha256"],
            }
        )

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="differs from the signed warm run"):
        _validate(fixture)


def test_rejects_false_full_oracle_signature_on_cold_run(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        worker = payload["worker_receipt"]
        worker["oracle"]["cold_d5"]["run_signature_sha256"] = worker["oracle"][
            "oracle_signature_sha256"
        ]

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="actual-run signature"):
        _validate(fixture)


def test_rejects_observed_worker_memory_overrun(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        memory = payload["worker_receipt"]["memory"]
        maximum = memory["per_worker_hard_maximum_bytes"]
        memory["workers"][0]["peak_bytes"] = maximum + 65_536
        memory["aggregate_observed_peak_bytes"] = sum(
            item["peak_bytes"] for item in memory["workers"]
        )

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="exceeded its hard memory"):
        _validate(fixture)


def test_rejects_oracle_signature_or_policy_tampering(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["session_config"]["root_tactical_protection"] = True

    _rewrite(fixture, "root_parity", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="canonical per-boundary"):
        _validate(fixture)


def test_rejects_a_single_worker_artifact_substitution(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["worker_receipt"]["environment"]["workers"][7]["artifact"]["wasm_sha256"] = "e" * 64

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="artifact 'wasm_sha256' drifted"):
        _validate(fixture)


def test_rejects_build_command_provenance_drift(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["command"].remove("-sDYNAMIC_EXECUTION=0")

    _rewrite(fixture, "build", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="omits required flags"):
        _validate(fixture)


def test_rejects_conflicting_extra_build_flag(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["command"].insert(-2, "-O0")

    _rewrite(fixture, "build", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="exact canonical builder invocation"):
        _validate(fixture)


def test_rejects_toolchain_version_drift(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["toolchain"]["version"] = "forged compiler version"

    _rewrite(fixture, "build", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="compiler version output changed"):
        _validate(fixture)


def test_rejects_incomplete_root_weight_config(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["session_config"]["weights"].pop("boundary_check")

    _rewrite(fixture, "root_parity", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="exactly bind all seven weights"):
        _validate(fixture)


def test_rejects_duplicate_prefix_case_inputs(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        cases = payload["cases"]
        cases[1]["input_sha256"] = cases[0]["input_sha256"]
        payload["case_set_sha256"] = promoter._canonical_sha256(cases)

    _rewrite(fixture, "prefix_parity", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="duplicates a case input"):
        _validate(fixture)
