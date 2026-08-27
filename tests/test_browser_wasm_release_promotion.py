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


def _checked_horizon_evidence() -> dict[str, object]:
    def case(
        *,
        root_side: str,
        root_order_key: str,
        proof_order: list[str],
        proof_path_lengths: list[int],
        child_depth: int,
        score: int,
        prior_score: int,
        hit_mask: int,
        disposition: str,
        prior_schema: str = "spc-root-candidate-result-v1",
        hits: int = 1,
        exact_tt_hits: int = 0,
        candidate_sha256: str = promoter.WHITE_HORIZON_CANDIDATE_SHA256,
        proof_set_sha256: str = promoter.WHITE_HORIZON_PROOF_SET_SHA256,
        root_pv_sha256: str | None = None,
    ) -> dict[str, object]:
        evidence = {
            "root_side": root_side,
            "root_order_key": root_order_key,
            "request_proof_count": len(proof_order),
            "request_proof_order": proof_order,
            "request_proof_path_lengths": proof_path_lengths,
            "newest_proof_anchor": proof_order[-1],
            "child_depth": child_depth,
            "schema": "spc-root-horizon-research-result-v1",
            "status": "complete",
            "bound": "exact",
            "score": score,
            "horizon_proofs_validated": len(proof_order),
            "horizon_proof_hits": hits,
            "horizon_proof_hit_mask": hit_mask,
            "horizon_proof_set_identity_sha256": proof_set_sha256,
            "candidate_identity_sha256": candidate_sha256,
            "exact_tt_hits": exact_tt_hits,
            "prior_same_root_schema": prior_schema,
            "prior_same_root_status": "complete",
            "prior_same_root_bound": "exact",
            "prior_same_root_score": prior_score,
            "prior_same_root_candidate_identity_sha256": candidate_sha256,
            "disposition": disposition,
        }
        if root_pv_sha256 is not None:
            evidence["root_pv_sha256"] = root_pv_sha256
            evidence["prior_same_root_root_pv_sha256"] = root_pv_sha256
        return evidence

    return {
        "schema": promoter.CHECKED_HORIZON_EVIDENCE_SCHEMA,
        "white_deep_two_proof": case(
            root_side="white",
            root_order_key="h4g2",
            proof_order=["alternate", "deep"],
            proof_path_lengths=[3, 3],
            child_depth=2,
            score=179,
            prior_score=336,
            hit_mask=0b10,
            disposition="same-root-repaired",
        ),
        "white_deep_warm_exact": case(
            root_side="white",
            root_order_key="h4g2",
            proof_order=["alternate", "deep"],
            proof_path_lengths=[3, 3],
            child_depth=2,
            score=179,
            prior_score=179,
            hit_mask=0,
            disposition="warm-exact-recertified",
            prior_schema="spc-root-horizon-research-result-v1",
            hits=0,
            exact_tt_hits=1,
            root_pv_sha256=promoter.WHITE_HORIZON_ROOT_PV_SHA256,
        ),
        "white_deep_reversed_order": case(
            root_side="white",
            root_order_key="h4g2",
            proof_order=["deep", "alternate"],
            proof_path_lengths=[3, 3],
            child_depth=2,
            score=179,
            prior_score=336,
            hit_mask=0b01,
            disposition="newest-proof-not-hit",
        ),
        "black_parity": case(
            root_side="black",
            root_order_key="f7f5/b8b1",
            proof_order=["black-mate"],
            proof_path_lengths=[1],
            child_depth=0,
            score=999_998,
            prior_score=-235,
            hit_mask=0b1,
            disposition="same-root-repaired",
            candidate_sha256=promoter.BLACK_HORIZON_CANDIDATE_SHA256,
            proof_set_sha256=promoter.BLACK_HORIZON_PROOF_SET_SHA256,
        ),
    }


def _valid_fixture(tmp_path: Path) -> dict[str, object]:
    repository = tmp_path / "repository"
    package = repository / "src" / "scottish_progressive"
    package.mkdir(parents=True)
    for index, relative in enumerate(promoter.KERNEL_SOURCES):
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"// source {index}: {relative}\n", encoding="utf-8")
    static_directory = package / "web" / "static"
    static_directory.mkdir(parents=True, exist_ok=True)
    for index, filename in enumerate(promoter.CHECKED_HORIZON_STATIC_ASSETS.values()):
        (static_directory / filename).write_text(
            f"// checked-horizon browser asset {index}: {filename}\n",
            encoding="utf-8",
        )
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
            "aspiration_windows": True,
            "selected_owner_certification": True,
            "canonical_root_tactical_policy": True,
            "checked_horizon_proof_research": True,
        },
        "request_schemas": {
            "search": "spc-root-candidate-task-v1",
            "horizon_research": "spc-root-horizon-research-task-v1",
        },
        "result_schemas": {
            "search": "spc-root-candidate-result-v1",
            "horizon_research": "spc-root-horizon-research-result-v1",
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
            "minimum_aspiration_initial_delta": 2_048,
            "maximum_aspiration_attempts": 4,
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
            "maximum_horizon_proofs": 16,
            "maximum_horizon_proof_path": 8,
        },
        "manifest": {
            "root_tactical_policy": "canonical-boundary-policy-v1",
        },
        "horizon_research": {
            "task_schema": "spc-root-horizon-research-task-v1",
            "result_schema": "spc-root-horizon-research-result-v1",
            "proof_schema": "spc-retained-root-horizon-proof-v1",
            "purpose": "horizon-research",
            "full_window": True,
            "tt_persistence": "commit",
            "hit_mask_order": "request-order",
            "warm_exact_zero_hit_allowed": True,
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
            "aspiration_fail_soft_window": True,
            "aspiration_fail_high_low_white_black": True,
            "selected_owner_warm_exact_certification": True,
            "checked_horizon_proof_research": True,
            "checked_horizon_newest_proof_hit": True,
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
        "checked_horizon_proof_research": _checked_horizon_evidence(),
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
            "moves": ["f2f3"],
            "machine_notation": "f2f3",
            "child_boundary": {"series": 2, "side_to_move": "black"},
        }
    ]
    selected = {
        "candidate_identity": "candidate-00",
        "move": "f2f3",
        "score": 617,
        "proof_bounds": [-1, 1],
        "principal_variation": principal_variation,
        "principal_variation_sha256": promoter._canonical_sha256(principal_variation),
    }
    bounds = [
        {
            "candidate_identity": f"candidate-{index:02d}",
            "bound": "exact" if index == 0 else "upper",
            "score": 617 - index,
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
            "work": 45_694,
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
            "work": 214,
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
        aspiration_enabled = depth > 1
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
                "aspiration": {
                    "enabled": aspiration_enabled,
                    "center_score": selected["score"] if aspiration_enabled else None,
                    "initial_delta": 2_048 if aspiration_enabled else None,
                    "maximum_attempts": 4,
                    "candidate_count": 8 if aspiration_enabled else 0,
                    "attempts": 8 if aspiration_enabled else 0,
                    "fail_highs": 0,
                    "fail_lows": 0,
                    "exact_hits": 8 if aspiration_enabled else 0,
                    "full_window_fallbacks": 0,
                    "owner_worker_id": "root-0" if aspiration_enabled else None,
                    "owner_worker_ids": [f"root-{index}" for index in range(8)]
                    if aspiration_enabled
                    else [],
                    "owner_worker_count": 8 if aspiration_enabled else 0,
                    "warm_owner_reused": aspiration_enabled,
                    "warm_owner_reused_count": 8 if aspiration_enabled else 0,
                },
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

    warm_aspiration = [
        {
            "depth": iteration["depth"],
            "selected_score": iteration["score"],
            "selected_owner_worker_id": iteration["owner_worker_id"],
            **copy.deepcopy(iteration["aspiration"]),
        }
        for iteration in iterations
    ]
    cold_aspiration = [
        {
            "depth": 5,
            "selected_score": selected["score"],
            "selected_owner_worker_id": "root-0",
            "enabled": False,
            "center_score": None,
            "initial_delta": None,
            "maximum_attempts": 4,
            "candidate_count": 0,
            "attempts": 0,
            "fail_highs": 0,
            "fail_lows": 0,
            "exact_hits": 0,
            "full_window_fallbacks": 0,
            "owner_worker_id": None,
            "owner_worker_ids": [],
            "owner_worker_count": 0,
            "warm_owner_reused": False,
            "warm_owner_reused_count": 0,
        }
    ]

    def run_binding(
        elapsed_ms: float,
        aspiration_iterations: list[dict[str, object]],
    ) -> dict[str, object]:
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
            "aspiration_iterations": copy.deepcopy(aspiration_iterations),
            "aspiration_sha256": promoter._canonical_sha256(aspiration_iterations),
        }

    cold_binding = run_binding(40_000.0, cold_aspiration)
    warm_binding = run_binding(49_000.0, warm_aspiration)

    def schedule_binding(
        wave: int,
        order_shape_sha256: str,
        elapsed_ms: float,
    ) -> dict[str, object]:
        schedule_aspiration = copy.deepcopy(warm_aspiration)
        for aspiration in schedule_aspiration:
            if aspiration["enabled"] is not True:
                continue
            aspiration.update(
                {
                    "candidate_count": wave,
                    "attempts": wave,
                    "exact_hits": wave,
                    "owner_worker_ids": [f"root-{index}" for index in range(wave)],
                    "owner_worker_count": wave,
                    "warm_owner_reused_count": wave,
                }
            )
        binding = run_binding(elapsed_ms, schedule_aspiration)
        semantic = {
            "run_signature_sha256": binding["run_signature_sha256"],
            "workers": 8,
            "initial_full_wave": wave,
            "order_shape_sha256": order_shape_sha256,
            "aspiration_sha256": binding["aspiration_sha256"],
        }
        return {
            "workers": 8,
            "initial_full_wave": wave,
            "order_shape_sha256": order_shape_sha256,
            **binding,
            "trial_signature_sha256": promoter._canonical_sha256(semantic),
        }

    wave_eight_binding = schedule_binding(8, "8" * 64, 49_000.0)
    wave_four_binding = schedule_binding(4, "9" * 64, 51_000.0)
    opera_worker = {
        "schema": promoter.OPERA_WORKER_SCHEMA,
        "status": "passed-not-certified",
        "product_publishable": False,
        "safety_certified": True,
        "artifact": oracle_artifact,
        "geometry": {
            "workers": 8,
            "initial_full_wave": 8,
            "depth": 5,
            "width": 32,
            "max_work": config["max_work"],
            "safety_reserve_work": safety_reserve,
            "config": config,
            "mode": "warm",
            "aspiration_enabled": True,
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
            wave_eight_binding,
            wave_four_binding,
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
            "aspiration_iteration_lifecycle": True,
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
                "&depth=5&width=32&workers=8&wave=8"
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
    principal_variation = [{"moves": ["f2f3"], "machine_notation": "f2f3"}]
    bounds = [
        {
            "candidate_identity": f"candidate-{index:02d}",
            "bound": "exact" if index == 0 else "upper",
            "score": 617 - index,
            "proof_bounds": [-1, 1],
        }
        for index in range(20)
    ]
    result = {
        "candidate_identity": "candidate-00",
        "move": "f2f3",
        "score": 617,
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


def test_rejects_untracked_release_verifier_outside_engine_package(
    tmp_path: Path,
) -> None:
    fixture = _valid_fixture(tmp_path)
    verifier = fixture["repository"] / "benchmarks" / "untracked-release-verifier.mjs"
    verifier.parent.mkdir(parents=True)
    verifier.write_text("export const forged = true;\n", encoding="utf-8")

    with pytest.raises(promoter.ReleaseGateError, match="release checkout is dirty"):
        _validate(fixture)


def _opera_checked_horizon_fixture(
    fixture: dict[str, object],
    evidence: promoter.ValidatedEvidence,
    certificates: dict[str, dict[str, object]],
    candidate: Path,
) -> tuple[Path, dict[str, object]]:
    origin = "http://127.0.0.1:8879"
    page_url = f"{origin}/?release-candidate"
    bundle = candidate / "browser-engine"
    manifest_path = bundle / "browser-engine-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    variant = manifest["variants"]["single"]
    build = evidence.build
    root_certificate = certificates["root_session"]
    mate_certificate = certificates["mate"]
    prefix_certificate = certificates["prefix"]
    root_identity = {
        "source_fingerprint": build.identity["source_fingerprint"],
        "kernel_sha256": build.identity["kernel_sha256"],
        "module_js_sha256": build.identity["module_js_sha256"],
        "certificate_id": root_certificate["certificate_id"],
        "runtime_variant": "single",
        "thread_count": 1,
        "engine_version": build.engine["engine_version"],
        "ruleset_version": build.engine["ruleset_version"],
        "profile_id": build.engine["profile_id"],
    }
    prefix_identity = {
        "source_fingerprint": build.identity["source_fingerprint"],
        "wasm_sha256": build.identity["wasm_sha256"],
        "module_js_sha256": build.identity["module_js_sha256"],
        "certificate_id": prefix_certificate["certificate_id"],
        "engine_version": build.engine["engine_version"],
        "ruleset_version": build.engine["ruleset_version"],
    }

    def boundary(series: int, fen: str | None = None) -> dict[str, object]:
        mover = "w" if series % 2 == 1 else "b"
        canonical_fen = fen or f"8/8/8/8/8/8/8/K6k {mover} - - 0 {series}"
        return {
            "board_fen": canonical_fen,
            "chess960": False,
            "ep_targets": [],
            "fen": canonical_fen,
            "progressive_ep": [],
            "promoted_hex": "0000000000000000",
            "quiet_draw_pending": False,
            "quiet_series": 0,
            "series": series,
            "series_number": series,
            "side_to_move": "white" if mover == "w" else "black",
        }

    def boundary_from_state(state: promoter.ProgressiveState) -> dict[str, object]:
        fen = state.board.fen(en_passant="fen")
        ep_targets = [promoter.chess.square_name(square) for square in state.ep_targets]
        return {
            "board_fen": fen,
            "chess960": bool(state.board.chess960),
            "ep_targets": ep_targets,
            "fen": fen,
            "progressive_ep": ep_targets,
            "promoted_hex": f"{state.board.promoted:016x}",
            "quiet_draw_pending": state.quiet_draw_pending,
            "quiet_series": state.quiet_series,
            "series": state.series_number,
            "series_number": state.series_number,
            "side_to_move": "white" if state.board.turn == promoter.chess.WHITE else "black",
        }

    def series(
        moves: list[str],
        child: dict[str, object],
        *,
        ended_by_check: bool = False,
        outcome: str | None = None,
    ) -> dict[str, object]:
        return {
            "child_boundary": child,
            "ended_by_check": ended_by_check,
            "machine_notation": "/".join(moves),
            "moves": moves,
            "outcome": outcome,
            "transposition_count": 1,
        }

    worker_url = f"{origin}/browser-engine-worker.js?checked-pv-horizon"

    def worker(index: int) -> dict[str, object]:
        return {
            "factory_sequence": index + 2,
            "name": f"scottish-progressive-root-root-{index}",
            "channel_id": f"root-{index}",
            "url": worker_url,
            "type": "module",
        }

    def prefix_replay(
        *,
        request_id: str,
        prefix: list[str],
        child: dict[str, object],
        mate: bool,
    ) -> dict[str, object]:
        value = {
            **prefix_identity,
            "schema": "spc-boundary-prefix-v1",
            "abi_version": 1,
            "ok": True,
            "status": "complete",
            "request_id": request_id,
            "boundary_state": child,
            "prefix": prefix,
            "current_prefix": prefix,
            "complete": True,
            "outcome": "checkmate" if mate else None,
            "completion_reason": "checkmate" if mate else "check",
            "ended_by_check": True,
            "check": True,
            "in_check": True,
            "next_state": child,
        }
        if mate:
            final_state = boundary(int(child["series"]) + 1)
            final_fen = final_state["fen"]
            remaining = int(child["series"]) - len(prefix)
            value.update(
                {
                    "san": ["Qh1#" for _ in prefix],
                    "frames": [
                        {
                            "index": index + 1,
                            "uci": move,
                            "san": "Qh1#",
                            "board_fen": final_fen,
                        }
                        for index, move in enumerate(prefix)
                    ],
                    "fen": final_fen,
                    "board_fen": final_fen,
                    "remaining": remaining,
                    "moves_remaining": remaining,
                    "unused_moves": remaining,
                    "legal_next": [],
                    "legal_moves": [],
                    "next_state": final_state,
                }
            )
        return value

    def work(*, native: int, tt_hits: int = 0) -> dict[str, object]:
        return {
            "external_work": 0,
            "native_work_before": 0,
            "call_work_credit": 1_000,
            "native_work_after": native,
            "call_native_work": native,
            "total_accounted_work": native,
            "call_stats": {"tt_hits": tt_hits},
            "cumulative_stats": {"tt_hits": tt_hits},
            "tt_capacity": 10_000,
            "tt_entries": 1,
            "tt_entries_peak": 1,
            "eval_capacity": 10_000,
            "eval_entries": 1,
            "eval_entries_peak": 1,
            "series_cache_capacity": 10_000,
            "series_cache_weight_peak": 1,
            "series_cache_entries_peak": 1,
        }

    fixture_specs = {
        "f3": {
            "root_move": "f2f3",
            "child_moves": [
                ["e7e5", "f8b4"],
                ["a2a3", "a3b4", "e2e4"],
                ["a7a5", "d8g5", "g5g2", "g2h1"],
            ],
            "unsafe_moves": ["d1e2", "e2c4", "c4c7", "f1c4", "c7c8"],
            "child_fen": "rnQ1k1nr/1p1p1ppp/8/p3p3/1PB1P3/5P2/1PPP3P/RNB1K1Nq b Qkq - 0 7",
            "candidate": "candidate-f3",
            "worker": 0,
        },
        "b3": {
            "root_move": "b2b3",
            "child_moves": [
                ["f7f5", "e8f7"],
                ["c1b2", "e2e3", "f1c4"],
                ["e7e6", "f5f4", "f4e3", "e3f2"],
            ],
            "unsafe_moves": ["e1f2", "d1g4", "f2e3", "g1h3", "g4h5"],
            "child_fen": "rnbq1bnr/pppp1kpp/4p3/7Q/2B5/1P2K2N/PBPP2PP/RN5R b - - 4 7",
            "candidate": "candidate-b3",
            "worker": 1,
        },
    }
    safety_traces: dict[str, object] = {}
    repair_traces: dict[str, object] = {}
    research_traces: list[dict[str, object]] = []
    for fixture_name, spec in fixture_specs.items():
        state = promoter.ProgressiveState.initial()
        rooted_path = []
        path_moves = [
            [str(spec["root_move"])],
            *copy.deepcopy(spec["child_moves"]),
            list(spec["unsafe_moves"]),
        ]
        for moves in path_moves:
            result = promoter.play_series(state, moves)
            outcome = (
                None
                if result.outcome is None
                else str(result.outcome.value).replace("-", "_")
            )
            rooted_path.append(
                series(
                    list(moves),
                    boundary_from_state(result.final_state),
                    ended_by_check=result.ended_by_check,
                    outcome=outcome,
                )
            )
            state = result.final_state
        root, *raw_child = rooted_path
        unsafe = raw_child[-1]
        child = unsafe["child_boundary"]
        assert child["fen"] == spec["child_fen"]
        iteration_id = f"checked-{fixture_name}:d5"
        request_id = f"checked-{fixture_name}"
        safety_revision = 3
        mate_moves = ["h8h1"]
        checked_mate = prefix_replay(
            request_id=f"{iteration_id}:{safety_revision}:mate-replay",
            prefix=mate_moves,
            child=child,
            mate=True,
        )
        root_replay = prefix_replay(
            request_id=f"{iteration_id}:{safety_revision}:pv-horizon-replay-4",
            prefix=unsafe["moves"],
            child=child,
            mate=False,
        )
        root_replay["boundary_state"] = raw_child[-2]["child_boundary"]
        candidate_record = {
            "candidate_identity": spec["candidate"],
            "order_index": int(spec["worker"]),
            "order_key": spec["root_move"],
            "owner_worker_id": f"root-{spec['worker']}",
            "terminal": False,
            "score": 200,
            "proof_bounds": [-1, 1],
            "root_series": unsafe,
            "child_pv": raw_child,
        }
        safety_request = {
            **root_identity,
            "schema": "spc-root-safety-task-v1",
            "session_id": f"session-{fixture_name}",
            "request_id": request_id,
            "iteration_id": iteration_id,
            "generation": 1,
            "safety_revision": safety_revision,
            "incumbent_epoch": 7,
            "deadline_monotonic_ms": 10_000.0,
            "deadline_epoch_ms": 1_000_000.0,
            "remaining_time_ms": 50_000,
            "call_work_credit": 1_000,
            "candidate_identity": spec["candidate"],
            "candidate": candidate_record,
            "authoritative_child_boundary": child,
            "authoritative_root_replay": root_replay,
        }
        safety_response = {
            **copy.deepcopy(safety_request),
            "status": "found",
            "work_used": 10,
            "memory_bytes": 1,
            "memory_peak_bytes": 1,
            "override_score": -999_998,
            "proof_bounds": [-1, -1],
            "reply_mate": {
                "checked_prefix": checked_mate,
                "ended_by_check": True,
                "machine_notation": "/".join(mate_moves),
                "moves": mate_moves,
                "outcome": "checkmate",
            },
        }
        trace_worker = worker(int(spec["worker"]))
        safety_trace = {
            "worker": trace_worker,
            "request_sequence": 1 + 4 * int(spec["worker"]),
            "posted_monotonic_ms": 100.0 + 300 * int(spec["worker"]),
            "received_monotonic_ms": 150.0 + 300 * int(spec["worker"]),
            "request": safety_request,
            "ok": True,
            "response": safety_response,
        }
        proof_mate = {
            "child_boundary": checked_mate["next_state"],
            "ended_by_check": True,
            "machine_notation": "/".join(mate_moves),
            "moves": mate_moves,
            "outcome": "checkmate",
            "transposition_count": 1,
        }
        proof = {
            "schema": "spc-retained-root-horizon-proof-v1",
            "rooted_path": [root, *raw_child],
            "mate_reply": proof_mate,
        }
        repair_request = {
            **root_identity,
            "schema": "spc-root-horizon-research-task-v1",
            "session_id": safety_request["session_id"],
            "request_id": request_id,
            "iteration_id": iteration_id,
            "generation": 1,
            "deadline_monotonic_ms": safety_request["deadline_monotonic_ms"],
            "deadline_epoch_ms": safety_request["deadline_epoch_ms"],
            "remaining_time_ms": 49_000,
            "external_work": 0,
            "native_work_before": 0,
            "call_work_credit": 1_000,
            "safety_revision": safety_revision + 1,
            "incumbent_epoch": safety_request["incumbent_epoch"],
            "task_id": f"repair-{fixture_name}",
            "enumeration_identity": "enumeration-start",
            "candidate_identity": spec["candidate"],
            "order_index": int(spec["worker"]),
            "order_key": spec["root_move"],
            "purpose": "horizon-research",
            "mate_score": 1_000_000,
            "child_depth": 4,
            "alpha": -2_000_000,
            "beta": 2_000_000,
            "tt_persistence": "commit",
            "mover": "white",
            "horizon_proofs": [proof],
        }
        repair_response = {
            **{key: copy.deepcopy(repair_request[key]) for key in promoter._HORIZON_ECHO_KEYS},
            **root_identity,
            "schema": "spc-root-horizon-research-result-v1",
            "abi_version": 2,
            "product_publishable": False,
            "safety_certified": False,
            "status": "complete",
            "bound": "exact",
            "memory_bytes": 1,
            "memory_peak_bytes": 1,
            "root_series": root,
            "child_pv": [],
            "score": 100,
            "proof_bounds": [-1, 1],
            "configured_max_depth": 5,
            "horizon_proofs_validated": 1,
            "horizon_proof_hits": 1,
            "horizon_proof_hit_mask": 1,
            "horizon_proof_set_identity": (
                f"spc-horizon-proof-set-v1|candidate{len(str(spec['candidate']))}:"
                f"{spec['candidate']}|proofs1:{fixture_name}"
            ),
            "work": work(native=10),
        }
        repair_trace = {
            "worker": trace_worker,
            "request_sequence": 2 + 4 * int(spec["worker"]),
            "posted_monotonic_ms": safety_trace["received_monotonic_ms"],
            "received_monotonic_ms": safety_trace["received_monotonic_ms"] + 50,
            "request": repair_request,
            "ok": True,
            "response": repair_response,
        }
        safety_traces[fixture_name] = safety_trace
        repair_traces[fixture_name] = repair_trace
        research_traces.append(copy.deepcopy(repair_trace))

    static_directory = Path(fixture["package"]) / "web" / "static"

    def asset(path: Path, url: str) -> dict[str, object]:
        return {
            "url": url,
            "byte_length": path.stat().st_size,
            "sha256": promoter._sha256_file(path),
        }

    assets = {
        label: asset(
            static_directory / filename,
            f"{origin}/{filename}"
            + ("?checked-pv-horizon" if label in {"browser_engine_worker", "wasm_kernel_adapter"} else ""),
        )
        for label, filename in promoter.CHECKED_HORIZON_STATIC_ASSETS.items()
    }
    assets["compiled_module"] = asset(
        bundle / "single" / variant["module_js"],
        f"{origin}/engine/single/{variant['module_js']}?sha256={variant['module_js_sha256']}",
    )
    assets["compiled_wasm"] = asset(
        bundle / "single" / variant["wasm"],
        f"{origin}/engine/single/{variant['wasm']}?sha256={variant['wasm_sha256']}",
    )
    manifest_asset = asset(
        manifest_path,
        f"{origin}/engine/browser-engine-manifest.json?checked-pv-horizon",
    )
    asset_set_sha256 = promoter._canonical_sha256(
        sorted(
            [
                ["browser_engine_manifest", manifest_asset],
                *[[label, value] for label, value in assets.items()],
            ],
            key=lambda item: item[0],
        )
    )
    worker_calls = [
        {
            "factory_sequence": 1,
            "name": "scottish-progressive-engine",
            "channel_id": None,
            "url": worker_url,
            "type": "module",
        },
        *[worker(index) for index in range(8)],
    ]
    same_root_repair_policy = {
        "schema": "spc-same-root-horizon-repair-policy-v1",
        "maximum_successful_same_root_repairs": 1,
    }
    policy_veto = {
        "schema": "spc-pv-horizon-candidate-veto-v1",
        "candidate_identity": fixture_specs["f3"]["candidate"],
        "reason": "same-root-repair-limit",
        "maximum_successful_same_root_repairs": 1,
        "repairs_before_veto": 1,
        "retained_proofs_before_veto": 1,
        "distinct_proofs_observed": 2,
    }
    second_f3_safety = copy.deepcopy(safety_traces["f3"])
    second_f3_safety["request_sequence"] = 3
    second_f3_safety["posted_monotonic_ms"] = 250.0
    second_f3_safety["received_monotonic_ms"] = 300.0
    second_request = second_f3_safety["request"]
    second_request["safety_revision"] = repair_traces["f3"]["request"]["safety_revision"]
    second_request["incumbent_epoch"] = repair_traces["f3"]["request"]["incumbent_epoch"] + 1
    second_request["remaining_time_ms"] = 48_000
    second_request["authoritative_root_replay"]["request_id"] = (
        f"{second_request['iteration_id']}:{second_request['safety_revision']}:pv-horizon-replay-4"
    )
    second_response = copy.deepcopy(second_request)
    second_response.update(copy.deepcopy(safety_traces["f3"]["response"]))
    for key, value in second_request.items():
        second_response[key] = copy.deepcopy(value)
    second_response["reply_mate"]["moves"] = ["h8h2"]
    second_response["reply_mate"]["machine_notation"] = "h8h2"
    second_checked = second_response["reply_mate"]["checked_prefix"]
    second_checked["request_id"] = (
        f"{second_request['iteration_id']}:{second_request['safety_revision']}:mate-replay"
    )
    second_checked["prefix"] = ["h8h2"]
    second_checked["current_prefix"] = ["h8h2"]
    second_checked["frames"][0]["uci"] = "h8h2"
    second_f3_safety["response"] = second_response
    first_proof = repair_traces["f3"]["request"]["horizon_proofs"][0]
    second_proof = {
        "schema": "spc-retained-root-horizon-proof-v1",
        "rooted_path": [
            copy.deepcopy(first_proof["rooted_path"][0]),
            *copy.deepcopy(second_request["candidate"]["child_pv"]),
        ],
        "mate_reply": {
            "child_boundary": copy.deepcopy(second_checked["next_state"]),
            "ended_by_check": True,
            "machine_notation": "h8h2",
            "moves": ["h8h2"],
            "outcome": "checkmate",
            "transposition_count": 1,
        },
    }
    threshold_veto_witness = {
        "schema": "spc-opera-same-root-repair-limit-witness-v1",
        "root_series": "f2f3",
        "candidate_identity": fixture_specs["f3"]["candidate"],
        "first_repair_request_sequence": repair_traces["f3"]["request_sequence"],
        "second_safety_request_sequence": second_f3_safety["request_sequence"],
        "first_proof_sha256": promoter._canonical_sha256(first_proof),
        "second_proof_sha256": promoter._canonical_sha256(second_proof),
        "proof_count_2_research_dispatched": False,
        "policy_veto": copy.deepcopy(policy_veto),
        "second_safety_trace": second_f3_safety,
    }
    warm_b3 = copy.deepcopy(repair_traces["b3"])
    warm_b3["request_sequence"] = 7
    warm_b3["posted_monotonic_ms"] = repair_traces["b3"]["received_monotonic_ms"]
    warm_b3["received_monotonic_ms"] = warm_b3["posted_monotonic_ms"] + 20
    warm_b3["request"]["task_id"] = "warm-b3"
    warm_b3["request"]["incumbent_epoch"] += 1
    warm_b3["request"]["remaining_time_ms"] -= 100
    warm_b3["request"]["native_work_before"] = 10
    for key in promoter._HORIZON_ECHO_KEYS:
        warm_b3["response"][key] = copy.deepcopy(warm_b3["request"][key])
    warm_b3["response"]["horizon_proof_hits"] = 0
    warm_b3["response"]["horizon_proof_hit_mask"] = 0
    warm_b3["response"]["work"].update(
        {
            "native_work_before": 10,
            "native_work_after": 20,
            "call_native_work": 10,
            "total_accounted_work": 20,
            "call_stats": {"tt_hits": 1},
            "cumulative_stats": {"tt_hits": 1},
        }
    )
    research_traces.append(copy.deepcopy(warm_b3))
    line_rejections = 3
    work_count = 20_000
    result_summary = {
        "ok": True,
        "status": "complete",
        "requested_depth": 5,
        "completed_depth": 5,
        "publishable": True,
        "safety_certified": True,
        "coverage_complete": True,
        "coverage_scope": "selection-eligible-candidates",
        "root_scores_complete": True,
        "width_complete": True,
        "legal_series_certified": True,
        "authoritative_replay_certified": True,
        "legal_validation_runtime": "compiled-wasm",
        "root_search_mode": "streaming-root-iteration",
        "selection_policy": "repair-once-then-veto-adverse-checked-pv-mates-v1",
        "selection_policy_filtered": True,
        "unfiltered_score_winner_selected": False,
        "pv_horizon_line_rejections": line_rejections,
        "pv_horizon_native_repairs": 2,
        "pv_horizon_candidate_vetoes": 1,
        "same_root_repair_policy": same_root_repair_policy,
        "pv_horizon_policy_vetoes": [policy_veto],
        "timed_out": False,
        "work_limit_reached": False,
        "work": work_count,
        "source_fingerprint": build.identity["source_fingerprint"],
        "wasm_sha256": build.identity["wasm_sha256"],
        "kernel_sha256": build.identity["kernel_sha256"],
        "module_js_sha256": build.identity["module_js_sha256"],
        "certificate_id": root_certificate["certificate_id"],
        "mate_certificate_id": mate_certificate["certificate_id"],
        "prefix_certificate_id": prefix_certificate["certificate_id"],
        "runtime_variant": "single",
        "thread_count": 1,
        "engine_profile_id": build.engine["profile_id"],
        "engine_version": build.engine["engine_version"],
        "ruleset_version": build.engine["ruleset_version"],
        "best_full_series": ["b2b3"],
        "score": 250,
        "proof_bounds": [-1, 1],
    }
    runtime_receipt = {
        "runtime": "browser-wasm",
        "search_mode": "streaming-root-iteration",
        "requested_depth": 5,
        "completed_depth": 5,
        "worker_count": 8,
        "initial_full_wave": 8,
        "canonical_replay_certified": True,
        "mate_safety_certified": True,
        "root_bound_coverage_complete": True,
        "root_bound_coverage_scope": "selection-eligible-candidates",
        "selection_policy": result_summary["selection_policy"],
        "selection_policy_filtered": True,
        "unfiltered_score_winner_selected": False,
        "pv_horizon_line_rejections": line_rejections,
        "pv_horizon_native_repairs": 2,
        "pv_horizon_candidate_vetoes": 1,
        "same_root_repair_policy": same_root_repair_policy,
        "pv_horizon_policy_vetoes": [policy_veto],
        "work": work_count,
        "source_fingerprint": build.identity["source_fingerprint"],
        "artifact_fingerprint": build.identity["wasm_sha256"],
        "kernel_fingerprint": build.identity["kernel_sha256"],
        "module_fingerprint": build.identity["module_js_sha256"],
        "certificate_id": root_certificate["certificate_id"],
        "mate_certificate_id": mate_certificate["certificate_id"],
        "runtime_variant": "single",
        "thread_count": 1,
    }
    stats = {
        "coverage_complete": True,
        "generation_positions": work_count,
        "pv_horizon_line_rejections": line_rejections,
        "pv_horizon_native_repairs": 2,
        "pv_horizon_candidate_vetoes": 1,
    }
    manifest_binding = {
        "source_fingerprint": build.identity["source_fingerprint"],
        "runtime_variant": "single",
        "thread_count": 1,
        "module_js": variant["module_js"],
        "wasm": variant["wasm"],
        "module_js_sha256": build.identity["module_js_sha256"],
        "wasm_sha256": build.identity["wasm_sha256"],
        "kernel_sha256": build.identity["kernel_sha256"],
        "analysis_certificate_id": None,
        "root_session_certificate_id": root_certificate["certificate_id"],
        "mate_certificate_id": mate_certificate["certificate_id"],
        "prefix_certificate_id": prefix_certificate["certificate_id"],
        "root_contract_sha256": promoter._canonical_sha256(root_certificate["root_session_contract"]),
        "root_geometry_sha256": promoter._canonical_sha256(root_certificate["geometry"]),
        "root_evidence_sha256": promoter._canonical_sha256(root_certificate["evidence"]),
        "prefix_contract_sha256": promoter._canonical_sha256(prefix_certificate["prefix_contract"]),
    }
    preflight = {
        "ready": True,
        "analysis_ready": False,
        "root_iteration_ready": True,
        "root_session_ready": True,
        "mate_ready": True,
        "prefix_ready": True,
        "safety_certified": False,
        "source_fingerprint": build.identity["source_fingerprint"],
        "runtime_variant": "single",
        "thread_count": 1,
        "module_js_sha256": build.identity["module_js_sha256"],
        "wasm_sha256": build.identity["wasm_sha256"],
        "kernel_sha256": build.identity["kernel_sha256"],
        "certificate_id": None,
        "root_session_certificate_id": root_certificate["certificate_id"],
        "mate_certificate_id": mate_certificate["certificate_id"],
        "prefix_certificate_id": prefix_certificate["certificate_id"],
        "engine_profile_id": build.engine["profile_id"],
        "engine_version": build.engine["engine_version"],
        "ruleset_version": build.engine["ruleset_version"],
        "root_contract_sha256": manifest_binding["root_contract_sha256"],
        "root_geometry_sha256": manifest_binding["root_geometry_sha256"],
        "prefix_contract_sha256": manifest_binding["prefix_contract_sha256"],
    }
    payload = {
        "schema": "spc-opera-checked-pv-horizon-receipt-v4",
        "status": "passed-not-certified",
        "product_publishable": False,
        "safety_certified": False,
        "certificate_id": None,
        "authenticity": {
            "scope": "local-checkout-hash-bound-unsigned-v1",
            "standalone_signature_verified": False,
            "limitation": "fixture",
            "local_origin": origin,
            "local_checkout_asset_set_sha256": asset_set_sha256,
            "manifest": manifest_asset,
            "assets": assets,
            "worker_factory_calls": worker_calls,
            "trusted_worker_events_only": True,
        },
        "page_environment": {
            "location": page_url,
            "userAgent": "Mozilla/5.0 fixture OPR/134.0.0.0",
            "hardwareConcurrency": 16,
            "crossOriginIsolated": False,
        },
        "manifest_binding": manifest_binding,
        "preflight_identity": preflight,
        "result_summary": result_summary,
        "checks": {key: True for key in promoter.OPERA_CHECKED_HORIZON_CHECKS},
        "elapsed_seconds": 5.0,
        "best_full_series": result_summary["best_full_series"],
        "principal_variation": [],
        "score": result_summary["score"],
        "work": result_summary["work"],
        "source_fingerprint": result_summary["source_fingerprint"],
        "wasm_sha256": result_summary["wasm_sha256"],
        "kernel_sha256": result_summary["kernel_sha256"],
        "module_js_sha256": result_summary["module_js_sha256"],
        "selection_policy": result_summary["selection_policy"],
        "pv_horizon_line_rejections": line_rejections,
        "pv_horizon_native_repairs": 2,
        "pv_horizon_candidate_vetoes": 1,
        "same_root_repair_policy": same_root_repair_policy,
        "pv_horizon_policy_vetoes": [policy_veto],
        "threshold_veto_witness": threshold_veto_witness,
        "horizon_safety_traces": safety_traces,
        "horizon_research_traces": research_traces,
        "certified_repair_traces": repair_traces,
        "final_winner_warm_recertification": {"f3": None, "b3": warm_b3},
        "runtime_receipt": runtime_receipt,
        "stats": stats,
        "cdp": {
            "browser": "Chrome/150.0.7871.187",
            "protocol_version": "1.3",
            "user_agent": "Mozilla/5.0 fixture OPR/134.0.0.0",
        },
        "page_url": page_url,
    }
    receipt_path = candidate.parent / "opera-checked-pv-horizon.json"
    _write_json(receipt_path, payload)
    return receipt_path, payload


def _checked_promotion_fixture(tmp_path: Path) -> dict[str, object]:
    fixture = _valid_fixture(tmp_path)
    evidence = _validate(fixture)
    certificates = promoter.build_certificates(
        evidence,
        maximum_seconds=60,
        default_seconds=60,
    )
    candidate = tmp_path / "candidate"
    staged = promoter.stage_release_candidate(
        evidence,
        certificates,
        source_package=fixture["package"],
        output=candidate,
        maximum_seconds=60,
        default_seconds=60,
    )
    receipt_path, receipt = _opera_checked_horizon_fixture(
        fixture,
        evidence,
        certificates,
        candidate,
    )
    return {
        "fixture": fixture,
        "evidence": evidence,
        "certificates": certificates,
        "candidate": candidate,
        "staged": staged,
        "receipt_path": receipt_path,
        "receipt": receipt,
    }


def _validate_checked_fixture(context: dict[str, object]) -> promoter.OperaCheckedHorizonEvidence:
    fixture = context["fixture"]
    assert isinstance(fixture, dict)
    return promoter.validate_opera_checked_horizon_receipt(
        receipt_path=Path(context["receipt_path"]),
        evidence=context["evidence"],
        certificates=context["certificates"],
        repository=fixture["repository"],
        source_package=fixture["package"],
        candidate_bundle=Path(context["candidate"]) / "browser-engine",
    )


def _rewrite_checked(context: dict[str, object], mutate) -> None:
    payload = copy.deepcopy(context["receipt"])
    mutate(payload)
    _write_json(Path(context["receipt_path"]), payload)


def test_promotes_only_the_verified_bytes_and_emits_a_digest_receipt(tmp_path: Path) -> None:
    context = _checked_promotion_fixture(tmp_path)
    fixture = context["fixture"]
    evidence = context["evidence"]
    certificates = context["certificates"]
    candidate = Path(context["candidate"])
    staged = context["staged"]
    opera_checked_receipt = Path(context["receipt_path"])
    assert certificates["root_session"]["checked_horizon_proof_research"] == (
        evidence.checked_horizon_proof_research
    )
    assert staged["status"] == "staged-for-local-opera-attestation"
    assert staged["product_publishable"] is False
    output = tmp_path / "release"
    release = promoter.promote_release(
        evidence,
        certificates,
        source_package=fixture["package"],
        repository=fixture["repository"],
        opera_checked_horizon_receipt=opera_checked_receipt,
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
    assert release["gates"]["opera_checked_horizon_raw_trace_attested"] is True
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
    assert promoter._sha256_file(
        output / "evidence" / promoter.OPERA_CHECKED_HORIZON_FILENAME
    ) == promoter._sha256_file(opera_checked_receipt)
    assert {
        label: item["certificate_id"]
        for label, item in staged["certificates"].items()
    } == {
        label: item["certificate_id"]
        for label, item in release["certificates"].items()
    }
    manifest = promoter.bundle_builder.validate_existing_bundle(
        output / "browser-engine",
        fixture["package"],
    )
    variant = manifest["variants"]["single"]
    assert variant["wasm_sha256"] == fixture["identity"]["wasm_sha256"]
    assert variant["root_session_certificate"][
        "checked_horizon_proof_research"
    ] == evidence.checked_horizon_proof_research
    assert promoter._sha256_file(
        output / "browser-engine" / "single" / variant["wasm"]
    ) == promoter._sha256_file(fixture["wasm"])
    assert json.loads((output / "release-receipt.json").read_text(encoding="utf-8"))["release_id"] == release["release_id"]
    with pytest.raises(FileExistsError):
        promoter.promote_release(
            evidence,
            certificates,
            source_package=fixture["package"],
            repository=fixture["repository"],
            opera_checked_horizon_receipt=opera_checked_receipt,
            output=output,
            authorized_by="tetizz",
            maximum_seconds=60,
            default_seconds=60,
        )


def test_checked_horizon_receipt_omission_fails_closed(tmp_path: Path) -> None:
    context = _checked_promotion_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["checks"].pop("f3_exact_raw_mate_and_same_root_repair")

    _rewrite_checked(context, mutate)
    with pytest.raises(promoter.ReleaseGateError, match="every exact passing check"):
        _validate_checked_fixture(context)


def test_checked_horizon_rejects_identity_and_asset_hash_drift(tmp_path: Path) -> None:
    context = _checked_promotion_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["result_summary"]["wasm_sha256"] = "f" * 64
        payload["wasm_sha256"] = "f" * 64
        payload["runtime_receipt"]["artifact_fingerprint"] = "f" * 64
        payload["authenticity"]["assets"]["compiled_wasm"]["sha256"] = "f" * 64

    _rewrite_checked(context, mutate)
    with pytest.raises(
        promoter.ReleaseGateError,
        match="bytes differ|release-safe|identity",
    ):
        _validate_checked_fixture(context)


def test_checked_horizon_rejects_resigned_accounting_drift(tmp_path: Path) -> None:
    context = _checked_promotion_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["pv_horizon_native_repairs"] = 3
        payload["result_summary"]["pv_horizon_native_repairs"] = 3
        payload["runtime_receipt"]["pv_horizon_native_repairs"] = 3
        payload["stats"]["pv_horizon_native_repairs"] = 3

    _rewrite_checked(context, mutate)
    with pytest.raises(promoter.ReleaseGateError, match="accounting is not balanced"):
        _validate_checked_fixture(context)


def test_checked_horizon_rejects_legacy_v3_receipt(tmp_path: Path) -> None:
    context = _checked_promotion_fixture(tmp_path)
    _rewrite_checked(
        context,
        lambda payload: payload.update(
            schema="spc-opera-checked-pv-horizon-receipt-v3"
        ),
    )

    with pytest.raises(promoter.ReleaseGateError, match="pre-certification evidence"):
        _validate_checked_fixture(context)


def test_checked_horizon_rejects_resigned_repair_policy_drift(tmp_path: Path) -> None:
    context = _checked_promotion_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        for container in (
            payload,
            payload["result_summary"],
            payload["runtime_receipt"],
        ):
            container["same_root_repair_policy"][
                "maximum_successful_same_root_repairs"
            ] = 2

    _rewrite_checked(context, mutate)
    with pytest.raises(promoter.ReleaseGateError, match="bounded same-root repair policy"):
        _validate_checked_fixture(context)


def test_checked_horizon_rejects_resigned_policy_veto_drift(tmp_path: Path) -> None:
    context = _checked_promotion_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        for veto in (
            payload["pv_horizon_policy_vetoes"][0],
            payload["result_summary"]["pv_horizon_policy_vetoes"][0],
            payload["runtime_receipt"]["pv_horizon_policy_vetoes"][0],
            payload["threshold_veto_witness"]["policy_veto"],
        ):
            veto["repairs_before_veto"] = 0

    _rewrite_checked(context, mutate)
    with pytest.raises(promoter.ReleaseGateError, match="one-repair threshold veto"):
        _validate_checked_fixture(context)


def test_checked_horizon_rejects_re_signed_duplicate_second_f3_proof(
    tmp_path: Path,
) -> None:
    context = _checked_promotion_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        witness = payload["threshold_veto_witness"]
        second = witness["second_safety_trace"]
        second_mate = copy.deepcopy(
            payload["horizon_safety_traces"]["f3"]["response"]["reply_mate"]
        )
        second_mate["checked_prefix"]["request_id"] = (
            f"{second['request']['iteration_id']}:"
            f"{second['request']['safety_revision']}:mate-replay"
        )
        second["response"]["reply_mate"] = second_mate
        witness["second_proof_sha256"] = witness["first_proof_sha256"]

    _rewrite_checked(context, mutate)
    with pytest.raises(promoter.ReleaseGateError, match="not distinct and hash-bound"):
        _validate_checked_fixture(context)


def test_checked_horizon_rejects_re_signed_same_length_second_f3_path_tamper(
    tmp_path: Path,
) -> None:
    context = _checked_promotion_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        witness = payload["threshold_veto_witness"]
        second = witness["second_safety_trace"]
        request = second["request"]
        response = second["response"]
        first_child_series = request["candidate"]["child_pv"][0]
        first_child_series["moves"][0] = "a7a6"
        first_child_series["machine_notation"] = "/".join(first_child_series["moves"])
        response["candidate"] = copy.deepcopy(request["candidate"])
        first_root = payload["certified_repair_traces"]["f3"]["request"][
            "horizon_proofs"
        ][0]["rooted_path"][0]
        mate = response["reply_mate"]
        second_proof = {
            "schema": "spc-retained-root-horizon-proof-v1",
            "rooted_path": [
                copy.deepcopy(first_root),
                *copy.deepcopy(request["candidate"]["child_pv"]),
            ],
            "mate_reply": {
                "child_boundary": copy.deepcopy(mate["checked_prefix"]["next_state"]),
                "ended_by_check": mate["ended_by_check"],
                "machine_notation": mate["machine_notation"],
                "moves": copy.deepcopy(mate["moves"]),
                "outcome": mate["outcome"],
                "transposition_count": 1,
            },
        }
        witness["second_proof_sha256"] = promoter._canonical_sha256(second_proof)

    _rewrite_checked(context, mutate)
    with pytest.raises(promoter.ReleaseGateError, match="failed authoritative replay"):
        _validate_checked_fixture(context)


def test_checked_horizon_rejects_f3_proof_count_2_dispatch(tmp_path: Path) -> None:
    context = _checked_promotion_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        dispatched = copy.deepcopy(payload["certified_repair_traces"]["f3"])
        dispatched["request_sequence"] = 8
        dispatched["request"]["horizon_proofs"].append(
            copy.deepcopy(dispatched["request"]["horizon_proofs"][0])
        )
        payload["horizon_research_traces"].append(dispatched)

    _rewrite_checked(context, mutate)
    with pytest.raises(promoter.ReleaseGateError, match="proof-count-2 research"):
        _validate_checked_fixture(context)


def test_checked_horizon_rejects_cross_fixture_trace_substitution(tmp_path: Path) -> None:
    context = _checked_promotion_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        substituted = copy.deepcopy(payload["certified_repair_traces"]["f3"])
        payload["certified_repair_traces"]["b3"] = substituted
        payload["horizon_research_traces"][1] = copy.deepcopy(substituted)

    _rewrite_checked(context, mutate)
    with pytest.raises(promoter.ReleaseGateError, match="b3.*root|candidate|fixture"):
        _validate_checked_fixture(context)


def test_checked_horizon_rejects_warm_recertification_drift(tmp_path: Path) -> None:
    context = _checked_promotion_fixture(tmp_path)
    assert _validate_checked_fixture(context).selected_root_series == "b2b3"

    def drift(payload: dict[str, object]) -> None:
        payload["final_winner_warm_recertification"]["b3"]["response"]["score"] += 1
        payload["horizon_research_traces"][-1]["response"]["score"] += 1

    _rewrite_checked(context, drift)
    with pytest.raises(promoter.ReleaseGateError, match="warm exact result drifted"):
        _validate_checked_fixture(context)


def test_checked_horizon_outer_attestation_does_not_cycle_certificate_ids(
    tmp_path: Path,
) -> None:
    context = _checked_promotion_fixture(tmp_path)
    before = {
        label: certificate["certificate_id"]
        for label, certificate in context["certificates"].items()
    }
    checked = _validate_checked_fixture(context)
    assert checked.receipt.payload["certificate_id"] is None
    rebuilt = promoter.build_certificates(
        context["evidence"],
        maximum_seconds=60,
        default_seconds=60,
    )
    after = {
        label: certificate["certificate_id"]
        for label, certificate in rebuilt.items()
    }
    assert after == before


def _promotion_cli_args(context: dict[str, object]) -> list[str]:
    fixture = context["fixture"]
    flags = {
        "build": "--build-receipt",
        "root_smoke": "--root-smoke-receipt",
        "root_parity": "--root-parity-receipt",
        "prefix_parity": "--prefix-parity-receipt",
        "browser_prefix": "--browser-prefix-receipt",
        "mate_parity": "--mate-parity-receipt",
        "opera": "--opera-receipt",
    }
    arguments: list[str] = []
    for label, flag in flags.items():
        arguments.extend([flag, str(fixture["paths"][label])])
    arguments.extend(
        [
            "--repository",
            str(fixture["repository"]),
            "--source-package",
            str(fixture["package"]),
        ]
    )
    return arguments


def test_cli_stages_core_seven_then_requires_outer_opera_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = _checked_promotion_fixture(tmp_path)
    arguments = _promotion_cli_args(context)
    staged_output = tmp_path / "cli-staged"
    assert promoter.main([*arguments, "--stage-candidate", "--output", str(staged_output)]) == 0
    staged_payload = json.loads(capsys.readouterr().out)
    assert staged_payload["product_publishable"] is False
    assert staged_payload["next_required_gate"] == promoter.OPERA_CHECKED_HORIZON_SCHEMA

    with pytest.raises(SystemExit) as missing:
        promoter.parse_args([*arguments, "--check-only"])
    assert missing.value.code == 2

    assert promoter.main(
        [
            *arguments,
            "--check-only",
            "--opera-checked-horizon-receipt",
            str(context["receipt_path"]),
        ]
    ) == 0
    checked_payload = json.loads(capsys.readouterr().out)
    assert checked_payload["status"] == "validated-not-promoted"
    assert checked_payload["product_publishable"] is False


def test_final_cli_promotion_requires_and_copies_outer_attestation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = _checked_promotion_fixture(tmp_path)
    output = tmp_path / "cli-release"
    assert promoter.main(
        [
            *_promotion_cli_args(context),
            "--opera-checked-horizon-receipt",
            str(context["receipt_path"]),
            "--authorized-by",
            "tetizz",
            "--output",
            str(output),
        ]
    ) == 0
    release = json.loads(capsys.readouterr().out)
    assert release["product_publishable"] is True
    assert release["gates"]["opera_checked_horizon_raw_trace_attested"] is True
    assert promoter._sha256_file(
        output / "evidence" / promoter.OPERA_CHECKED_HORIZON_FILENAME
    ) == promoter._sha256_file(Path(context["receipt_path"]))


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


def test_rejects_missing_two_color_aspiration_parity_gate(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["gates"].pop("aspiration_fail_high_low_white_black")

    _rewrite(fixture, "root_smoke", mutate)
    with pytest.raises(
        promoter.ReleaseGateError,
        match="aspiration_fail_high_low_white_black",
    ):
        _validate(fixture)


def test_rejects_unproven_or_drifted_checked_horizon_research(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def remove_newest_hit(payload: dict[str, object]) -> None:
        payload["gates"].pop("checked_horizon_newest_proof_hit")

    _rewrite(fixture, "root_smoke", remove_newest_hit)
    with pytest.raises(
        promoter.ReleaseGateError,
        match="checked_horizon_newest_proof_hit",
    ):
        _validate(fixture)

    fixture = _valid_fixture(tmp_path / "policy-drift")

    def drift_hit_order(payload: dict[str, object]) -> None:
        payload["root_session_contract"]["horizon_research"][
            "hit_mask_order"
        ] = "canonical-order"

    _rewrite(fixture, "root_smoke", drift_hit_order)
    with pytest.raises(
        promoter.ReleaseGateError,
        match="checked-horizon policy",
    ):
        _validate(fixture)

    fixture = _valid_fixture(tmp_path / "mask-drift")

    def drift_newest_mask(payload: dict[str, object]) -> None:
        payload["checked_horizon_proof_research"]["white_deep_two_proof"][
            "horizon_proof_hit_mask"
        ] = 0b01

    _rewrite(fixture, "root_smoke", drift_newest_mask)
    with pytest.raises(
        promoter.ReleaseGateError,
        match="exact checked-horizon evidence",
    ):
        _validate(fixture)

    fixture = _valid_fixture(tmp_path / "disposition-drift")

    def fake_reversed_repair(payload: dict[str, object]) -> None:
        payload["checked_horizon_proof_research"]["white_deep_reversed_order"][
            "disposition"
        ] = "same-root-repaired"

    _rewrite(fixture, "root_smoke", fake_reversed_repair)
    with pytest.raises(
        promoter.ReleaseGateError,
        match="exact checked-horizon evidence",
    ):
        _validate(fixture)

    fixture = _valid_fixture(tmp_path / "proof-set-drift")

    def drift_reversed_set(payload: dict[str, object]) -> None:
        payload["checked_horizon_proof_research"]["white_deep_reversed_order"][
            "horizon_proof_set_identity_sha256"
        ] = "0" * 64

    _rewrite(fixture, "root_smoke", drift_reversed_set)
    with pytest.raises(
        promoter.ReleaseGateError,
        match="exact checked-horizon evidence",
    ):
        _validate(fixture)

    fixture = _valid_fixture(tmp_path / "coordinated-identity-drift")

    def drift_all_white_identities(payload: dict[str, object]) -> None:
        evidence = payload["checked_horizon_proof_research"]
        for case_name in (
            "white_deep_two_proof",
            "white_deep_warm_exact",
            "white_deep_reversed_order",
        ):
            case = evidence[case_name]
            case["candidate_identity_sha256"] = "1" * 64
            case["prior_same_root_candidate_identity_sha256"] = "1" * 64
            case["horizon_proof_set_identity_sha256"] = "2" * 64

    _rewrite(fixture, "root_smoke", drift_all_white_identities)
    with pytest.raises(promoter.ReleaseGateError, match="exact checked-horizon evidence"):
        _validate(fixture)

    fixture = _valid_fixture(tmp_path / "black-identity-drift")

    def drift_black_identity(payload: dict[str, object]) -> None:
        case = payload["checked_horizon_proof_research"]["black_parity"]
        case["candidate_identity_sha256"] = "3" * 64
        case["prior_same_root_candidate_identity_sha256"] = "3" * 64

    _rewrite(fixture, "root_smoke", drift_black_identity)
    with pytest.raises(promoter.ReleaseGateError, match="exact checked-horizon evidence"):
        _validate(fixture)

    fixture = _valid_fixture(tmp_path / "warm-tt-drift")

    def remove_warm_tt_hit(payload: dict[str, object]) -> None:
        payload["checked_horizon_proof_research"]["white_deep_warm_exact"][
            "exact_tt_hits"
        ] = 0

    _rewrite(fixture, "root_smoke", remove_warm_tt_hit)
    with pytest.raises(promoter.ReleaseGateError, match="exact checked-horizon evidence"):
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


def test_receipt_builder_requires_d1_off_and_d2_through_d5_on(
    tmp_path: Path,
) -> None:
    fixture = _valid_fixture(tmp_path)
    iterations = fixture["payloads"]["opera"]["worker_receipt"]["iterations"]

    normalized = opera_receipt_builder._aspiration_iterations(
        iterations,
        label="warm fixture",
        expected_depths=[1, 2, 3, 4, 5],
        expected_mode="warm",
        expected_candidate_count=8,
    )

    assert [item["enabled"] for item in normalized] == [False, True, True, True, True]
    assert all(item["warm_owner_reused"] is True for item in normalized[1:])


def test_receipt_builder_rejects_cold_d5_aspiration(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)
    cold = copy.deepcopy(
        fixture["payloads"]["opera"]["worker_receipt"]["iterations"][-1]
    )

    with pytest.raises(ValueError, match="D5 aspiration must be disabled"):
        opera_receipt_builder._aspiration_iterations(
            [cold],
            label="cold fixture",
            expected_depths=[5],
            expected_mode="cold",
            expected_candidate_count=0,
        )


def test_rejects_enabled_d1_aspiration(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        aspiration = payload["worker_receipt"]["iterations"][0]["aspiration"]
        aspiration.update(
            {
                "enabled": True,
                "center_score": 0,
                "initial_delta": 2_048,
                "attempts": 1,
                "exact_hits": 1,
                "owner_worker_id": "root-0",
                "warm_owner_reused": True,
            }
        )

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="D1 aspiration must be disabled"):
        _validate(fixture)


def test_rejects_disabled_d2_aspiration(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        aspiration = payload["worker_receipt"]["iterations"][1]["aspiration"]
        aspiration.update(
            {
                "enabled": False,
                "center_score": None,
                "initial_delta": None,
                "attempts": 0,
                "exact_hits": 0,
                "owner_worker_id": None,
                "warm_owner_reused": False,
            }
        )

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="D2 aspiration must be enabled"):
        _validate(fixture)


def test_rejects_aspiration_warm_owner_drift(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["worker_receipt"]["iterations"][1]["aspiration"][
            "owner_worker_id"
        ] = "root-7"

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="did not reuse its warm owner"):
        _validate(fixture)


def test_rejects_aspiration_exact_fallback_contradiction(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["worker_receipt"]["iterations"][1]["aspiration"][
            "full_window_fallbacks"
        ] = 1

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="accounting contradicts itself"):
        _validate(fixture)


def test_rejects_enabled_cold_d5_aspiration_binding(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        aspiration = payload["worker_receipt"]["oracle"]["cold_d5"][
            "aspiration_iterations"
        ][0]
        aspiration.update(
            {
                "enabled": True,
                "center_score": 617,
                "initial_delta": 2_048,
                "attempts": 1,
                "exact_hits": 1,
                "owner_worker_id": "root-0",
                "warm_owner_reused": True,
            }
        )

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="D5 aspiration must be disabled"):
        _validate(fixture)


def test_rejects_alternate_schedule_aspiration_owner_claim(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["worker_receipt"]["schedule_trials"][1]["aspiration_iterations"][1][
            "warm_owner_reused"
        ] = False

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="did not reuse its warm owner"):
        _validate(fixture)


def test_rejects_tampered_aspiration_digest(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["worker_receipt"]["oracle"]["warm_d1_through_d5"][
            "aspiration_sha256"
        ] = "f" * 64

    _rewrite(fixture, "opera", mutate)
    with pytest.raises(promoter.ReleaseGateError, match="invalid aspiration digest"):
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
        ("score", "617", "candidate score"),
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


def test_rejects_re_signed_wave_eight_order_shape_drift(tmp_path: Path) -> None:
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
                "aspiration_sha256": trial["aspiration_sha256"],
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
