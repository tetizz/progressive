from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import chess
import pytest

from scottish_progressive.webapp import APIError, inspect_prefix, state_from_payload


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "scottish_progressive" / "web" / "static"
NODE = shutil.which("node")


def _load_bundle_builder():
    path = ROOT / "scripts" / "build_browser_wasm_bundle.py"
    spec = importlib.util.spec_from_file_location("build_browser_wasm_bundle", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _certificate(
    builder,
    *,
    source_package: Path,
    wasm: Path,
    module_js: Path,
) -> dict[str, object]:
    return {
        "schema": builder.CERTIFICATE_SCHEMA,
        "status": "certified",
        "safety_certified": True,
        "contract_version": 1,
        "abi_version": 1,
        "certificate_id": "gate-20260822-single",
        "source_fingerprint": builder.engine_source_fingerprint(source_package),
        "wasm_sha256": builder.sha256_file(wasm),
        "module_js_sha256": builder.sha256_file(module_js),
        "runtime_variant": "single",
        "thread_count": 1,
        "support_files": [],
        "memory": {
            "initial_bytes": 16 * 1024 * 1024,
            "maximum_bytes": 128 * 1024 * 1024,
            "estimated_peak_bytes": 96 * 1024 * 1024,
            "growth_enabled": True,
        },
        "evidence": {
            "failures": 0,
            "differential_cases": 256,
            "start_position_parity": True,
            "s4_mate_safety": True,
            "interrupted_depth_publication": True,
            "compiled_legal_series_validation": True,
            "compiled_authoritative_replay": True,
            "start_w32_d5_completed_depth": 5,
            "start_w32_d5_width": 32,
            "start_w32_d5_elapsed_seconds": 42.5,
        },
        "engine": {
            "engine_profile_id": "spc-browser-test",
            "engine_profile_name": "Browser test champion",
            "engine_version": "test-engine-v1",
            "ruleset_version": "test-rules-v1",
            "analysis_limits": {
                "maximum_depth": 8,
                "maximum_max_series": 64,
                "maximum_seconds": 60,
                "maximum_generation_positions": 25_000_000,
                "default_depth": 5,
                "default_max_series": 32,
                "default_seconds": 45,
                "default_generation_positions": 20_000_000,
            },
        },
    }


def _prefix_certificate(
    builder,
    *,
    source_package: Path,
    wasm: Path,
    module_js: Path,
) -> dict[str, object]:
    return {
        "status": "certified",
        "contract_version": 1,
        "certificate_id": "prefix-gate-20260822-single",
        "source_fingerprint": builder.engine_source_fingerprint(source_package),
        "wasm_sha256": builder.sha256_file(wasm),
        "module_js_sha256": builder.sha256_file(module_js),
        "runtime_variant": "single",
        "thread_count": 1,
        "support_files": [],
        "memory": {
            "initial_bytes": 16 * 1024 * 1024,
            "maximum_bytes": 128 * 1024 * 1024,
            "estimated_peak_bytes": 96 * 1024 * 1024,
            "growth_enabled": True,
        },
        "evidence": {
            "failures": 0,
            "compiled_prefix_replay": True,
            "multi_ep_san": True,
            "illegal_prefix_fail_closed": True,
            "differential_cases": builder.MIN_PREFIX_DIFFERENTIAL_CASES,
        },
        "engine": {
            "engine_version": "test-engine-v1",
            "ruleset_version": "test-rules-v1",
        },
        "prefix_contract": {
            "schema": builder.PREFIX_CONTRACT_SCHEMA,
            "result_schema": builder.PREFIX_RESULT_SCHEMA,
            "abi_version": 1,
            "chess960": False,
            "promoted_hex_required_for_product": True,
            "limits": dict(builder.PREFIX_HARD_LIMITS),
        },
    }


def _root_session_certificate(
    builder,
    *,
    source_package: Path,
    wasm: Path,
    module_js: Path,
) -> dict[str, object]:
    maximum = 128 * 1024 * 1024
    return {
        "schema": builder.ROOT_SESSION_CERTIFICATE_SCHEMA,
        "status": "certified",
        "certificate_id": "root-session-gate-20260822-single",
        "contract_version": 1,
        "abi_version": 2,
        "root_session_certified": True,
        "reply_mate_safety": False,
        "product_publishable": False,
        "source_fingerprint": builder.engine_source_fingerprint(source_package),
        "kernel_sha256": "d" * 64,
        "wasm_sha256": builder.sha256_file(wasm),
        "module_js_sha256": builder.sha256_file(module_js),
        "runtime_variant": "single",
        "thread_count": 1,
        "support_files": [],
        "exports": list(builder.COMBINED_EXPORTS),
        "exception_strategy": "emscripten",
        "wasm_simd": False,
        "allocator": "dlmalloc",
        "runtime_requirements": {
            "ordinary_module_worker": True,
            "pthreads": False,
            "cross_origin_isolated": False,
            "native_wasm_exception_handling": False,
            "wasm_simd": False,
        },
        "memory": {
            "initial_bytes": 16 * 1024 * 1024,
            "maximum_bytes": maximum,
            "estimated_peak_bytes": 96 * 1024 * 1024,
            "growth_enabled": True,
        },
        "engine": {
            "engine_version": "test-engine-v1",
            "ruleset_version": "test-rules-v1",
            "profile_id": "spc-browser-test",
        },
        "root_session_contract": {
            "schema": builder.ROOT_SESSION_CONTRACT_SCHEMA,
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
                "root_tactical_protection_values": [False, True],
                "minimum_tt_capacity": 1,
                "maximum_tt_capacity": 1_048_576,
                "minimum_eval_capacity": 1,
                "maximum_eval_capacity": 1_048_576,
                "minimum_weight": 25,
                "maximum_weight": 300,
            },
        },
        "geometry": {
            "desktop_workers": 8,
            "desktop_initial_full_wave": 4,
            "aggregate_maximum_bytes": 8 * maximum,
            "supported_lower_geometries": [],
            "play_limits": {
                "maximum_seconds": 60,
                "default_seconds": 45,
                "default_generation_positions": 100_000_000,
                "safety_reserve_positions": 1_000_000,
            },
            "session_config": {
                "max_depth": 5,
                "width": 32,
                "max_work": 100_000_000,
                "mate_score": 1_000_000,
                "series_cache_capacity": 65_536,
                "external_cache_weight": 0,
                "worker_threads": 1,
                "root_tactical_protection": True,
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
            },
        },
        "evidence": {
            "failures": 0,
            "differential_cases": 12,
            "deterministic_node_smoke": True,
            "combined_artifact": True,
            "enumerate_import_search": True,
            "exact_manifest_import": True,
            "persistent_d1_d2_session": True,
            "cumulative_work_and_cache_receipts": True,
            "configured_max_depth_rejected": True,
            "per_call_work_credit": True,
            "selected_owner_warm_exact_certification": True,
            "deadline_fail_closed": True,
            "work_limit_fail_closed": True,
            "browser_worker_smoke": True,
            "opera_worker_smoke": True,
            "start_w32_d5_completed_depth": 5,
            "start_w32_d5_width": 32,
            "start_w32_d5_elapsed_seconds": 42.5,
        },
    }


def _mate_certificate(
    builder,
    *,
    source_package: Path,
    wasm: Path,
    module_js: Path,
) -> dict[str, object]:
    return {
        "schema": builder.MATE_CERTIFICATE_SCHEMA,
        "status": "certified",
        "certificate_id": "mate-gate-20260822-single",
        "contract_version": 1,
        "abi_version": 1,
        "mate_capability_certified": True,
        "reply_mate_safety": True,
        "product_publishable": False,
        "source_fingerprint": builder.engine_source_fingerprint(source_package),
        "kernel_sha256": "d" * 64,
        "wasm_sha256": builder.sha256_file(wasm),
        "module_js_sha256": builder.sha256_file(module_js),
        "runtime_variant": "single",
        "thread_count": 1,
        "support_files": [],
        "exports": list(builder.COMBINED_EXPORTS),
        "exception_strategy": "emscripten",
        "wasm_simd": False,
        "allocator": "dlmalloc",
        "runtime_requirements": {
            "ordinary_module_worker": True,
            "pthreads": False,
            "cross_origin_isolated": False,
            "native_wasm_exception_handling": False,
            "wasm_simd": False,
        },
        "memory": {
            "initial_bytes": 16 * 1024 * 1024,
            "maximum_bytes": 128 * 1024 * 1024,
            "estimated_peak_bytes": 96 * 1024 * 1024,
            "growth_enabled": True,
        },
        "engine": {
            "engine_version": "test-engine-v1",
            "ruleset_version": "test-rules-v1",
            "profile_id": "spc-browser-test",
        },
        "evidence": {
            "failures": 0,
            "differential_cases": builder.MIN_MATE_DIFFERENTIAL_CASES,
            "combined_artifact": True,
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
            "browser_worker_smoke": True,
        },
    }


def test_bundle_builder_stages_only_a_certified_identity_bound_single_lane(
    tmp_path: Path,
) -> None:
    builder = _load_bundle_builder()
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.cpp").write_text("int engine = 1;\n", encoding="utf-8")
    wasm = tmp_path / "kernel.wasm"
    module_js = tmp_path / "kernel.mjs"
    certificate_path = tmp_path / "certificate.json"
    wasm.write_bytes(b"\0asm\x01\0\0\0")
    module_js.write_text("export default async () => ({});\n", encoding="utf-8")
    certificate_path.write_text(
        json.dumps(
            _certificate(
                builder,
                source_package=package,
                wasm=wasm,
                module_js=module_js,
            )
        ),
        encoding="utf-8",
    )
    output = tmp_path / "engine"

    manifest = builder.build_bundle(
        single_wasm=wasm,
        single_module_js=module_js,
        single_certificate_path=certificate_path,
        source_package=package,
        output=output,
    )

    assert set(manifest["variants"]) == {"single"}
    assert manifest["variants"]["single"]["thread_count"] == 1
    assert (
        manifest["variants"]["single"]["safety_certificate"]["engine"]
        ["analysis_limits"]["default_depth"]
        == 5
    )
    assert (output / "single" / "spc-engine.wasm").read_bytes() == wasm.read_bytes()
    assert (output / "browser-engine-manifest.json").is_file()


def test_bundle_builder_stages_an_independently_certified_prefix_lane(
    tmp_path: Path,
) -> None:
    builder = _load_bundle_builder()
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.cpp").write_text("int engine = 1;\n", encoding="utf-8")
    wasm = tmp_path / "kernel.wasm"
    module_js = tmp_path / "kernel.mjs"
    prefix_certificate_path = tmp_path / "prefix-certificate.json"
    wasm.write_bytes(b"\0asm\x01\0\0\0")
    module_js.write_text("export default async () => ({});\n", encoding="utf-8")
    prefix_certificate_path.write_text(
        json.dumps(
            _prefix_certificate(
                builder,
                source_package=package,
                wasm=wasm,
                module_js=module_js,
            )
        ),
        encoding="utf-8",
    )
    output = tmp_path / "engine"

    manifest = builder.build_bundle(
        single_wasm=wasm,
        single_module_js=module_js,
        single_prefix_certificate_path=prefix_certificate_path,
        source_package=package,
        output=output,
    )

    variant = manifest["variants"]["single"]
    assert "safety_certificate" not in variant
    assert variant["prefix_certificate"]["certificate_id"] == (
        "prefix-gate-20260822-single"
    )
    assert variant["prefix_certificate"]["memory"]["maximum_bytes"] == (
        128 * 1024 * 1024
    )
    builder.validate_existing_bundle(output, package)


def test_bundle_builder_stages_one_identity_bound_root_and_mate_artifact(
    tmp_path: Path,
) -> None:
    builder = _load_bundle_builder()
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.cpp").write_text("int engine = 1;\n", encoding="utf-8")
    wasm = tmp_path / "kernel.wasm"
    module_js = tmp_path / "kernel.mjs"
    root_path = tmp_path / "root-certificate.json"
    mate_path = tmp_path / "mate-certificate.json"
    wasm.write_bytes(b"\0asm\x01\0\0\0")
    module_js.write_text("export default async () => ({});\n", encoding="utf-8")
    root_path.write_text(
        json.dumps(
            _root_session_certificate(
                builder,
                source_package=package,
                wasm=wasm,
                module_js=module_js,
            )
        ),
        encoding="utf-8",
    )
    mate_path.write_text(
        json.dumps(
            _mate_certificate(
                builder,
                source_package=package,
                wasm=wasm,
                module_js=module_js,
            )
        ),
        encoding="utf-8",
    )
    output = tmp_path / "engine"

    manifest = builder.build_bundle(
        single_wasm=wasm,
        single_module_js=module_js,
        single_root_session_certificate_path=root_path,
        single_mate_certificate_path=mate_path,
        source_package=package,
        output=output,
    )

    variant = manifest["variants"]["single"]
    assert variant["kernel_sha256"] == "d" * 64
    assert variant["root_session_certificate"]["reply_mate_safety"] is False
    assert variant["mate_certificate"]["reply_mate_safety"] is True
    assert variant["root_session_certificate"]["exports"] == list(
        builder.COMBINED_EXPORTS
    )
    assert variant["root_session_certificate"]["geometry"]["play_limits"] == {
        "maximum_seconds": 60,
        "default_seconds": 45,
        "default_generation_positions": 100_000_000,
        "safety_reserve_positions": 1_000_000,
    }
    assert (
        variant["root_session_certificate"]["memory"]
        == variant["mate_certificate"]["memory"]
    )
    builder.validate_existing_bundle(output, package)


def test_root_and_mate_certificates_fail_closed_on_contract_and_identity_drift(
    tmp_path: Path,
) -> None:
    builder = _load_bundle_builder()
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.cpp").write_text("int engine = 1;\n", encoding="utf-8")
    wasm = tmp_path / "kernel.wasm"
    module_js = tmp_path / "kernel.mjs"
    root_path = tmp_path / "root-certificate.json"
    mate_path = tmp_path / "mate-certificate.json"
    wasm.write_bytes(b"\0asm\x01\0\0\0")
    module_js.write_text("export default async () => ({});\n", encoding="utf-8")
    root = _root_session_certificate(
        builder,
        source_package=package,
        wasm=wasm,
        module_js=module_js,
    )
    mate = _mate_certificate(
        builder,
        source_package=package,
        wasm=wasm,
        module_js=module_js,
    )

    root["reply_mate_safety"] = True
    root_path.write_text(json.dumps(root), encoding="utf-8")
    mate_path.write_text(json.dumps(mate), encoding="utf-8")
    with pytest.raises(ValueError, match="must not claim reply-mate safety"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_root_session_certificate_path=root_path,
            single_mate_certificate_path=mate_path,
            source_package=package,
            output=tmp_path / "unsafe-root",
        )

    root["reply_mate_safety"] = False
    root["evidence"]["start_w32_d5_elapsed_seconds"] = 60
    root_path.write_text(json.dumps(root), encoding="utf-8")
    with pytest.raises(ValueError, match="exact W32 D5 gate"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_root_session_certificate_path=root_path,
            single_mate_certificate_path=mate_path,
            source_package=package,
            output=tmp_path / "slow-root",
        )

    root["evidence"]["start_w32_d5_elapsed_seconds"] = 42.5
    root["geometry"]["session_config"].pop("root_contract_eval_capacity")
    root_path.write_text(json.dumps(root), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly bind every native field"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_root_session_certificate_path=root_path,
            single_mate_certificate_path=mate_path,
            source_package=package,
            output=tmp_path / "unbound-config",
        )

    root["geometry"]["session_config"]["root_contract_eval_capacity"] = 262_144
    root["geometry"]["play_limits"].pop("safety_reserve_positions")
    root_path.write_text(json.dumps(root), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly bind four fields"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_root_session_certificate_path=root_path,
            single_mate_certificate_path=mate_path,
            source_package=package,
            output=tmp_path / "unbound-play-limits",
        )

    root["geometry"]["play_limits"]["safety_reserve_positions"] = 1_000_000
    root["geometry"]["play_limits"]["default_generation_positions"] = 100_000_001
    root_path.write_text(json.dumps(root), encoding="utf-8")
    with pytest.raises(ValueError, match="play work limits are invalid"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_root_session_certificate_path=root_path,
            single_mate_certificate_path=mate_path,
            source_package=package,
            output=tmp_path / "over-cap-play-limits",
        )

    root["geometry"]["play_limits"]["default_generation_positions"] = 100_000_000
    mate["kernel_sha256"] = "e" * 64
    root_path.write_text(json.dumps(root), encoding="utf-8")
    mate_path.write_text(json.dumps(mate), encoding="utf-8")
    with pytest.raises(ValueError, match="different kernels"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_root_session_certificate_path=root_path,
            single_mate_certificate_path=mate_path,
            source_package=package,
            output=tmp_path / "kernel-drift",
        )


def test_prefix_certificate_rejects_weak_evidence_and_limits_above_native_abi(
    tmp_path: Path,
) -> None:
    builder = _load_bundle_builder()
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.cpp").write_text("int engine = 1;\n", encoding="utf-8")
    wasm = tmp_path / "kernel.wasm"
    module_js = tmp_path / "kernel.mjs"
    certificate_path = tmp_path / "prefix-certificate.json"
    wasm.write_bytes(b"\0asm\x01\0\0\0")
    module_js.write_text("export default async () => ({});\n", encoding="utf-8")
    certificate = _prefix_certificate(
        builder,
        source_package=package,
        wasm=wasm,
        module_js=module_js,
    )

    certificate["evidence"]["differential_cases"] = (
        builder.MIN_PREFIX_DIFFERENTIAL_CASES - 1
    )
    certificate_path.write_text(json.dumps(certificate), encoding="utf-8")
    with pytest.raises(ValueError, match="at least 14 differential cases"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_prefix_certificate_path=certificate_path,
            source_package=package,
            output=tmp_path / "weak-evidence",
        )

    certificate["evidence"]["differential_cases"] = (
        builder.MIN_PREFIX_DIFFERENTIAL_CASES
    )
    certificate["prefix_contract"]["limits"]["maximum_fen_utf8_bytes"] = 513
    certificate_path.write_text(json.dumps(certificate), encoding="utf-8")
    with pytest.raises(ValueError, match="maximum_fen_utf8_bytes"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_prefix_certificate_path=certificate_path,
            source_package=package,
            output=tmp_path / "broad-contract",
        )


def test_prefix_certificate_cannot_bypass_memory_caps_or_search_identity(
    tmp_path: Path,
) -> None:
    builder = _load_bundle_builder()
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.cpp").write_text("int engine = 1;\n", encoding="utf-8")
    wasm = tmp_path / "kernel.wasm"
    module_js = tmp_path / "kernel.mjs"
    search_path = tmp_path / "search-certificate.json"
    prefix_path = tmp_path / "prefix-certificate.json"
    wasm.write_bytes(b"\0asm\x01\0\0\0")
    module_js.write_text("export default async () => ({});\n", encoding="utf-8")
    search = _certificate(
        builder,
        source_package=package,
        wasm=wasm,
        module_js=module_js,
    )
    prefix = _prefix_certificate(
        builder,
        source_package=package,
        wasm=wasm,
        module_js=module_js,
    )
    search_path.write_text(json.dumps(search), encoding="utf-8")

    prefix["memory"]["maximum_bytes"] = builder.MAXIMUM_MEMORY_BYTES + 65_536
    prefix["memory"]["estimated_peak_bytes"] = builder.MAXIMUM_MEMORY_BYTES
    prefix_path.write_text(json.dumps(prefix), encoding="utf-8")
    with pytest.raises(ValueError, match="maximum_bytes"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_prefix_certificate_path=prefix_path,
            source_package=package,
            output=tmp_path / "oversized-prefix",
        )

    prefix["memory"] = {
        **search["memory"],
        "estimated_peak_bytes": 64 * 1024 * 1024,
    }
    prefix_path.write_text(json.dumps(prefix), encoding="utf-8")
    with pytest.raises(ValueError, match="identical memory envelopes"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_certificate_path=search_path,
            single_prefix_certificate_path=prefix_path,
            source_package=package,
            output=tmp_path / "memory-mismatch",
        )

    prefix["memory"] = dict(search["memory"])
    prefix["engine"]["ruleset_version"] = "different-rules"
    prefix_path.write_text(json.dumps(prefix), encoding="utf-8")
    with pytest.raises(ValueError, match="disagree on ruleset_version"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_certificate_path=search_path,
            single_prefix_certificate_path=prefix_path,
            source_package=package,
            output=tmp_path / "engine-mismatch",
        )


def test_bundle_builder_rejects_a_depth_five_receipt_at_the_sixty_second_gate(
    tmp_path: Path,
) -> None:
    builder = _load_bundle_builder()
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.cpp").write_text("int engine = 1;\n", encoding="utf-8")
    wasm = tmp_path / "kernel.wasm"
    module_js = tmp_path / "kernel.mjs"
    certificate_path = tmp_path / "certificate.json"
    wasm.write_bytes(b"\0asm\x01\0\0\0")
    module_js.write_text("export default async () => ({});\n", encoding="utf-8")
    certificate = _certificate(
        builder,
        source_package=package,
        wasm=wasm,
        module_js=module_js,
    )
    certificate["evidence"]["start_w32_d5_elapsed_seconds"] = 60
    certificate_path.write_text(json.dumps(certificate), encoding="utf-8")
    output = tmp_path / "engine"

    with pytest.raises(ValueError, match="under-60-second W32 D5"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_certificate_path=certificate_path,
            source_package=package,
            output=output,
        )

    assert not output.exists()


def test_bundle_builder_rejects_excessive_memory_and_pthread_publication(
    tmp_path: Path,
) -> None:
    builder = _load_bundle_builder()
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.cpp").write_text("int engine = 1;\n", encoding="utf-8")
    wasm = tmp_path / "kernel.wasm"
    module_js = tmp_path / "kernel.mjs"
    certificate_path = tmp_path / "certificate.json"
    wasm.write_bytes(b"\0asm\x01\0\0\0")
    module_js.write_text("export default async () => ({});\n", encoding="utf-8")
    certificate = _certificate(
        builder,
        source_package=package,
        wasm=wasm,
        module_js=module_js,
    )
    certificate["memory"]["maximum_bytes"] = builder.MAXIMUM_MEMORY_BYTES + 65_536
    certificate_path.write_text(json.dumps(certificate), encoding="utf-8")

    with pytest.raises(ValueError, match="maximum_bytes"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_certificate_path=certificate_path,
            source_package=package,
            output=tmp_path / "oversized",
        )

    certificate["memory"]["maximum_bytes"] = 128 * 1024 * 1024
    certificate_path.write_text(json.dumps(certificate), encoding="utf-8")
    with pytest.raises(ValueError, match="pthread publishing is disabled"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_certificate_path=certificate_path,
            pthread_wasm=wasm,
            pthread_module_js=module_js,
            pthread_certificate_path=certificate_path,
            source_package=package,
            output=tmp_path / "pthread",
        )


def test_existing_bundle_validator_rejects_artifact_drift(tmp_path: Path) -> None:
    builder = _load_bundle_builder()
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.cpp").write_text("int engine = 1;\n", encoding="utf-8")
    wasm = tmp_path / "kernel.wasm"
    module_js = tmp_path / "kernel.mjs"
    certificate_path = tmp_path / "certificate.json"
    wasm.write_bytes(b"\0asm\x01\0\0\0")
    module_js.write_text("export default async () => ({});\n", encoding="utf-8")
    certificate_path.write_text(
        json.dumps(
            _certificate(
                builder,
                source_package=package,
                wasm=wasm,
                module_js=module_js,
            )
        ),
        encoding="utf-8",
    )
    output = tmp_path / "engine"
    builder.build_bundle(
        single_wasm=wasm,
        single_module_js=module_js,
        single_certificate_path=certificate_path,
        source_package=package,
        output=output,
    )
    builder.validate_existing_bundle(output, package)

    (output / "single" / "spc-engine.js").write_text(
        "export default async () => ({ changed: true });\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        builder.validate_existing_bundle(output, package)


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser contract tests")
def test_adapter_loads_verified_prefix_only_artifact_and_checks_native_contract() -> None:
    script = r"""
import { createHash, webcrypto } from "node:crypto";
import { pathToFileURL } from "node:url";

if (!globalThis.crypto) globalThis.crypto = webcrypto;
const api = await import(pathToFileURL(process.argv[1]).href);
const wasmBytes = Uint8Array.from([0, 97, 115, 109, 1, 0, 0, 0]);
const moduleBytes = new TextEncoder().encode("export default async () => ({});\n");
const hash = (bytes) => createHash("sha256").update(bytes).digest("hex");
const source = "a".repeat(16);
const memory = {
  initial_bytes: 16777216,
  maximum_bytes: 134217728,
  estimated_peak_bytes: 100663296,
  growth_enabled: true,
};
const limits = {
  maximum_fen_utf8_bytes: 512,
  maximum_series_number: 256,
  maximum_quiet_series: 1000000,
  maximum_ep_targets: 8,
  maximum_ep_utf8_bytes: 23,
  maximum_prefix_moves: 256,
  maximum_prefix_utf8_bytes: 1535,
  maximum_uci_move_bytes: 5,
  maximum_promoted_hex_bytes: 18,
};
const manifest = {
  schema: "spc-browser-wasm-manifest-v1",
  contract_version: 1,
  abi_version: 1,
  source_fingerprint: source,
  variants: {
    single: {
      thread_count: 1,
      wasm: "spc-engine.wasm",
      wasm_sha256: hash(wasmBytes),
      module_js: "spc-engine.js",
      module_js_sha256: hash(moduleBytes),
      support_files: [],
      prefix_certificate: {
        status: "certified",
        contract_version: 1,
        certificate_id: "prefix-cert-1",
        source_fingerprint: source,
        runtime_variant: "single",
        thread_count: 1,
        wasm_sha256: hash(wasmBytes),
        module_js_sha256: hash(moduleBytes),
        support_files: [],
        memory,
        evidence: {
          failures: 0,
          compiled_prefix_replay: true,
          multi_ep_san: true,
          illegal_prefix_fail_closed: true,
          differential_cases: 14,
        },
        engine: { engine_version: "engine-v1", ruleset_version: "rules-v1" },
        prefix_contract: {
          schema: "spc-boundary-prefix-contract-v1",
          result_schema: "spc-boundary-prefix-v1",
          abi_version: 1,
          chess960: false,
          promoted_hex_required_for_product: true,
          limits,
        },
      },
    },
  },
};
const copyBuffer = (bytes) => bytes.buffer.slice(
  bytes.byteOffset,
  bytes.byteOffset + bytes.byteLength,
);
globalThis.fetch = async (url) => {
  const text = String(url);
  if (text.includes("browser-engine-manifest.json")) {
    return { ok: true, status: 200, json: async () => manifest };
  }
  const bytes = text.includes("spc-engine.wasm") ? wasmBytes : moduleBytes;
  return { ok: true, status: 200, arrayBuffer: async () => copyBuffer(bytes) };
};

const strings = new Map();
let nextPointer = 10;
let freed = 0;
const put = (value) => { const pointer = nextPointer++; strings.set(pointer, value); return pointer; };
const nativeContractPointer = put(JSON.stringify({
  schema: "spc-boundary-prefix-contract-v1",
  abi_version: 1,
  result_schema: "spc-boundary-prefix-v1",
  chess960: false,
  promoted_hex_required_for_product: true,
  hard_limits: limits,
}));
const module = {
  HEAPU8: new Uint8Array(memory.initial_bytes),
  _spc_start_kernel_abi_version: () => 1,
  stringToNewUTF8: put,
  UTF8ToString: (pointer) => strings.get(pointer),
  _free: (pointer) => { if (strings.delete(pointer)) freed += 1; },
  _spc_boundary_prefix_contract_json: () => nativeContractPointer,
  _spc_boundary_prefix_json: (fen, series, quiet, ep, promoted, prefix) => {
    if (
      strings.get(fen) !== "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
      || series !== 1 || quiet !== 0 || strings.get(ep) !== "-"
      || strings.get(promoted) !== "0000000000000000" || strings.get(prefix) !== ""
    ) throw new Error("prefix ABI arguments drifted");
    return put(JSON.stringify({
      schema: "spc-boundary-prefix-v1", abi_version: 1, ok: true, status: "complete",
    }));
  },
};
let importedVerifiedBytes = false;
const kernel = await api.loadCertifiedBrowserKernel({
  expectedSourceFingerprint: source,
  manifestUrl: new URL("https://example.test/engine/browser-engine-manifest.json"),
  moduleImporter: async (bytes) => {
    importedVerifiedBytes = Buffer.from(bytes).equals(Buffer.from(moduleBytes));
    return { default: async () => module };
  },
});
if (!importedVerifiedBytes) throw new Error("unverified wrapper bytes executed");
if (kernel.identity.analysis_ready !== false || kernel.identity.prefix_ready !== true) {
  throw new Error("prefix-only capability drifted");
}
if (kernel.identity.certificate_id !== null || kernel.identity.safety_certified !== false) {
  throw new Error("prefix certificate became a search certificate");
}
const result = kernel.inspectPrefix({
  contract_version: 1,
  operation: "prefix-replay",
  request_id: "prefix-1",
  boundary: {
    fen: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    series: 1,
    quiet_series: 0,
    ep_targets: [],
    promoted_hex: "0000000000000000",
    chess960: false,
  },
  prefix: [],
});
if (result.certificate_id !== "prefix-cert-1" || result.memory_bytes !== memory.initial_bytes) {
  throw new Error("prefix runtime receipt drifted");
}
let searchRejected = false;
try { kernel.analyze({}); } catch (error) {
  searchRejected = error.code === "browser-analysis-unavailable";
}
if (!searchRejected) throw new Error("prefix-only artifact became searchable");
if (freed !== 4) throw new Error(`prefix input allocations leaked: ${freed}`);
strings.set(nativeContractPointer, JSON.stringify({
  schema: "spc-boundary-prefix-contract-v1",
  abi_version: 1,
  result_schema: "spc-boundary-prefix-v1",
  chess960: false,
  promoted_hex_required_for_product: true,
  hard_limits: { ...limits, maximum_fen_utf8_bytes: 511 },
}));
let nativeMismatchRejected = false;
try {
  await api.loadCertifiedBrowserKernel({
    expectedSourceFingerprint: source,
    manifestUrl: new URL("https://example.test/engine/browser-engine-manifest.json"),
    moduleImporter: async () => ({ default: async () => module }),
  });
} catch (error) {
  nativeMismatchRejected = error.code === "browser-prefix-abi-mismatch";
}
if (!nativeMismatchRejected) throw new Error("native prefix contract drift was accepted");
process.stdout.write(JSON.stringify({
  prefixReady: kernel.identity.prefix_ready,
  analysisReady: kernel.identity.analysis_ready,
  searchRejected,
  nativeMismatchRejected,
  freed,
}));
"""
    completed = subprocess.run(
        [
            str(NODE),
            "--input-type=module",
            "-e",
            script,
            str(STATIC / "wasm-kernel-adapter.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "prefixReady": True,
        "analysisReady": False,
        "searchRejected": True,
        "nativeMismatchRejected": True,
        "freed": 4,
    }


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser contract tests")
def test_browser_client_accepts_certified_completed_depth_and_rejects_fake_legality() -> None:
    script = r"""
const api = require(process.argv[1]);
const source = "a".repeat(16);
const artifact = "b".repeat(64);
const moduleHash = "c".repeat(64);
const start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const after = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1";
const identity = {
  ready: true,
  analysis_ready: true,
  prefix_ready: false,
  source_fingerprint: source,
  wasm_sha256: artifact,
  module_js_sha256: moduleHash,
  certificate_schema: "spc-browser-wasm-certificate-v1",
  certificate_status: "certified",
  contract_version: 1,
  abi_version: 1,
  safety_certified: true,
  certificate_id: "cert-1",
  prefix_certificate_id: null,
  prefix_contract: null,
  runtime_variant: "single",
  thread_count: 1,
  engine_profile_id: "spc-test",
  engine_profile_name: "Test champion",
  engine_version: "engine-v1",
  ruleset_version: "rules-v1",
  analysis_limits: {
    maximum_depth: 8,
    maximum_max_series: 64,
    maximum_seconds: 60,
    maximum_generation_positions: 25000000,
    default_depth: 5,
    default_max_series: 32,
    default_seconds: 45,
    default_generation_positions: 20000000,
  },
  memory_limits: {
    initial_bytes: 16777216,
    maximum_bytes: 134217728,
    estimated_peak_bytes: 100663296,
    growth_enabled: true,
  },
};
const requestPayload = {
  fen: start,
  series: 1,
  quiet_series: 0,
  ep_targets: [],
  promoted_hex: "0",
  chess960: false,
  prefix: [],
  depth: 5,
  max_series: 32,
  time_limit: 45,
  max_generation_positions: 20000000,
  alternatives: 0,
  best_move_only: true,
  rate_move: false,
  save: false,
};

function resultFor(request) {
  return {
    ok: true,
    publishable: true,
    safety_certified: true,
    legal_series_certified: true,
    authoritative_replay_certified: true,
    legal_validation_runtime: "compiled-wasm",
    source_fingerprint: source,
    wasm_sha256: artifact,
    module_js_sha256: moduleHash,
    certificate_id: "cert-1",
    runtime_variant: "single",
    thread_count: 1,
    requested_depth: 5,
    completed_depth: 4,
    best_full_series: ["e2e4"],
    score: 12,
    work: 123456,
    memory_bytes: 16777216,
    stats: { generation_positions: 123456 },
    checked_prefix: {
      boundary_state: {
        fen: start,
        series: 1,
        quiet_series: 0,
        ep_targets: [],
        promoted_hex: "0000000000000000",
        chess960: false,
      },
      prefix: ["e2e4"],
      san: ["e4"],
      frames: [{ index: 1, uci: "e2e4", san: "e4", board_fen: after }],
      complete: true,
      board_fen: after,
      outcome: null,
      next_state: {
        fen: after,
        series: 2,
        quiet_series: 0,
        ep_targets: ["e3"],
        promoted_hex: "0000000000000000",
        chess960: false,
      },
    },
  };
}

class FakeWorker {
  constructor() {
    this.listeners = new Map();
    this.terminated = false;
  }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
  postMessage(message) {
    const payload = message.type === "probe" ? identity : resultFor(message.payload);
    queueMicrotask(() => {
      for (const listener of this.listeners.get("message") || []) {
        listener({ data: { id: message.id, ok: true, payload } });
      }
    });
  }
  terminate() { this.terminated = true; }
}

(async () => {
  const worker = new FakeWorker();
  const client = api.createClient({ workerFactory: () => worker });
  const ready = await client.preflight({});
  if (!ready.ready || ready.source_fingerprint !== source) throw new Error("local preflight failed");
  const result = await client.analyze(requestPayload);
  if (result.requested_depth !== 5 || result.completed_depth !== 4) throw new Error("depth receipt drifted");
  if (result.runtime_receipt.completed_depth !== 4) throw new Error("receipt inflated depth");
  if (result.runtime_receipt.artifact_fingerprint !== artifact) throw new Error("artifact receipt missing");
  if (result.runtime_receipt.thread_count !== 1) throw new Error("thread receipt missing");
  const request = api.normalizedKernelRequest(requestPayload, "unsafe-check");
  const unsafe = { ...resultFor(request), legal_series_certified: false };
  let rejected = false;
  try {
    api.validatePublishedAnalysis(unsafe, request, identity);
  } catch (error) {
    rejected = error.code === "browser-legality-unverified";
  }
  if (!rejected) throw new Error("uncertified legality was published");
  process.stdout.write(JSON.stringify({ ready, receipt: result.runtime_receipt }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "browser-engine-client.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["ready"]["runtime_variant"] == "single"
    assert payload["receipt"]["requested_depth"] == 5
    assert payload["receipt"]["completed_depth"] == 4
    assert payload["receipt"]["work"] == 123_456


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser contract tests")
def test_browser_client_cancellation_terminates_the_synchronous_worker() -> None:
    script = r"""
const api = require(process.argv[1]);
const identity = {
  ready: true,
  analysis_ready: true,
  prefix_ready: false,
  source_fingerprint: "a".repeat(16),
  wasm_sha256: "b".repeat(64),
  module_js_sha256: "c".repeat(64),
  certificate_schema: "spc-browser-wasm-certificate-v1",
  certificate_status: "certified",
  contract_version: 1,
  abi_version: 1,
  safety_certified: true,
  certificate_id: "cert-1",
  prefix_certificate_id: null,
  prefix_contract: null,
  runtime_variant: "single",
  thread_count: 1,
  engine_profile_id: "spc-test",
  engine_profile_name: "Test champion",
  engine_version: "engine-v1",
  ruleset_version: "rules-v1",
  analysis_limits: {
    maximum_depth: 8,
    maximum_max_series: 64,
    maximum_seconds: 60,
    maximum_generation_positions: 25000000,
    default_depth: 5,
    default_max_series: 32,
    default_seconds: 30,
    default_generation_positions: 10000000,
  },
  memory_limits: {
    initial_bytes: 16777216,
    maximum_bytes: 134217728,
    estimated_peak_bytes: 100663296,
    growth_enabled: true,
  },
};
class BlockingWorker {
  constructor() { this.listeners = new Map(); this.terminated = false; }
  addEventListener(type, listener) {
    const values = this.listeners.get(type) || [];
    values.push(listener);
    this.listeners.set(type, values);
  }
  postMessage(message) {
    if (message.type !== "probe") return;
    queueMicrotask(() => {
      for (const listener of this.listeners.get("message") || []) {
        listener({ data: { id: message.id, ok: true, payload: identity } });
      }
    });
  }
  terminate() { this.terminated = true; }
}
(async () => {
  const worker = new BlockingWorker();
  const client = api.createClient({ workerFactory: () => worker });
  await client.preflight({});
  const controller = new AbortController();
  const pending = client.analyze({
    fen: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    series: 1, quiet_series: 0, ep_targets: [], promoted_hex: "0", chess960: false,
    prefix: [], depth: 5, max_series: 32, time_limit: 30,
    max_generation_positions: 10000000, alternatives: 0,
    best_move_only: true, rate_move: false, save: false,
  }, { signal: controller.signal });
  controller.abort();
  let name = null;
  try { await pending; } catch (error) { name = error.name; }
  if (name !== "AbortError") throw new Error(`unexpected cancellation ${name}`);
  if (!worker.terminated) throw new Error("worker survived cancellation");
  if (client.ready !== false) throw new Error("cancelled worker remained ready");
  process.stdout.write(JSON.stringify({ name, terminated: worker.terminated }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "browser-engine-client.js")],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {"name": "AbortError", "terminated": True}


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser contract tests")
def test_browser_client_rejects_uncertified_limits_and_incomplete_replay() -> None:
    script = r"""
const api = require(process.argv[1]);
const source = "a".repeat(16);
const artifact = "b".repeat(64);
const moduleHash = "c".repeat(64);
const start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const after = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1";
const limits = {
  maximum_depth: 8, maximum_max_series: 64, maximum_seconds: 60,
  maximum_generation_positions: 25000000, default_depth: 5,
  default_max_series: 32, default_seconds: 30,
  default_generation_positions: 10000000,
};
const identity = {
  source_fingerprint: source, wasm_sha256: artifact, module_js_sha256: moduleHash,
  analysis_ready: true, prefix_ready: false,
  certificate_schema: "spc-browser-wasm-certificate-v1",
  certificate_status: "certified", contract_version: 1, abi_version: 1,
  safety_certified: true, certificate_id: "cert-1", runtime_variant: "single",
  prefix_certificate_id: null, prefix_contract: null,
  thread_count: 1, engine_profile_id: "spc-test", engine_profile_name: "Test",
  engine_version: "engine-v1", ruleset_version: "rules-v1",
  analysis_limits: limits,
  memory_limits: {
    initial_bytes: 16777216, maximum_bytes: 134217728,
    estimated_peak_bytes: 100663296, growth_enabled: true,
  },
};
const base = {
  fen: start, series: 1, quiet_series: 0, ep_targets: [],
  promoted_hex: "0", chess960: false, prefix: [], depth: 5,
  max_series: 32, time_limit: 30, max_generation_positions: 10000000,
  alternatives: 0, best_move_only: true, rate_move: false, save: false,
};
const outside = {
  ...base, depth: 64, max_series: 4096, time_limit: 1000,
  max_generation_positions: 4000000000,
};
if (api.isLocalBestMoveRequest(outside, limits)) {
  throw new Error("request outside certificate was selectable");
}
const request = api.normalizedKernelRequest(base, "replay-check", limits);
const common = {
  ok: true, publishable: true, safety_certified: true,
  legal_series_certified: true, authoritative_replay_certified: true,
  legal_validation_runtime: "compiled-wasm", source_fingerprint: source,
  wasm_sha256: artifact, module_js_sha256: moduleHash, certificate_id: "cert-1",
  runtime_variant: "single", thread_count: 1, memory_bytes: 16777216,
  requested_depth: 5, completed_depth: 4, best_full_series: ["e2e4"], stats: {},
};
const replay = {
  boundary_state: {
    fen: start, series: 1, quiet_series: 0, ep_targets: [],
    promoted_hex: "0000000000000000", chess960: false,
  },
  prefix: ["e2e4"], san: ["e4"],
  frames: [{ index: 1, uci: "e2e4", san: "e4", board_fen: after }],
  complete: true, board_fen: after, outcome: null,
  next_state: { fen: after, series: 2, promoted_hex: "0000000000000000" },
};
let missingStateRejected = false;
try {
  api.validatePublishedAnalysis({ ...common, checked_prefix: replay }, request, identity);
} catch (error) {
  missingStateRejected = error.code === "browser-replay-invalid";
}
if (!missingStateRejected) throw new Error("incomplete next state was accepted");
const completeNext = {
  fen: after, series: 2, quiet_series: 0, ep_targets: ["e3"],
  promoted_hex: "0000000000000000", chess960: false,
};
let finalFrameRejected = false;
try {
  api.validatePublishedAnalysis({
    ...common,
    checked_prefix: {
      ...replay,
      frames: [{ index: 1, uci: "e2e4", san: "e4", board_fen: start }],
      next_state: completeNext,
    },
  }, request, identity);
} catch (error) {
  finalFrameRejected = error.code === "browser-replay-invalid";
}
if (!finalFrameRejected) throw new Error("mismatched final frame was accepted");
process.stdout.write(JSON.stringify({ missingStateRejected, finalFrameRejected }));
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "browser-engine-client.js")],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "missingStateRejected": True,
        "finalFrameRejected": True,
    }


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser contract tests")
def test_browser_client_reprobes_after_an_unexpected_worker_crash() -> None:
    script = r"""
const api = require(process.argv[1]);
const source = "a".repeat(16), artifact = "b".repeat(64), moduleHash = "c".repeat(64);
const start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const after = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1";
const identity = {
  ready: true, source_fingerprint: source, wasm_sha256: artifact,
  analysis_ready: true, prefix_ready: false,
  module_js_sha256: moduleHash,
  certificate_schema: "spc-browser-wasm-certificate-v1",
  certificate_status: "certified", contract_version: 1, abi_version: 1,
  safety_certified: true, certificate_id: "cert-1",
  prefix_certificate_id: null, prefix_contract: null,
  runtime_variant: "single", thread_count: 1, engine_profile_id: "spc-test",
  engine_profile_name: "Test", engine_version: "engine-v1", ruleset_version: "rules-v1",
  analysis_limits: {
    maximum_depth: 8, maximum_max_series: 64, maximum_seconds: 60,
    maximum_generation_positions: 25000000, default_depth: 5,
    default_max_series: 32, default_seconds: 30,
    default_generation_positions: 10000000,
  },
  memory_limits: {
    initial_bytes: 16777216, maximum_bytes: 134217728,
    estimated_peak_bytes: 100663296, growth_enabled: true,
  },
};
function resultFor(request) {
  return {
    ok: true, publishable: true, safety_certified: true,
    legal_series_certified: true, authoritative_replay_certified: true,
    legal_validation_runtime: "compiled-wasm", source_fingerprint: source,
    wasm_sha256: artifact, module_js_sha256: moduleHash, certificate_id: "cert-1",
    runtime_variant: "single", thread_count: 1, memory_bytes: 16777216,
    requested_depth: 5, completed_depth: 4, best_full_series: ["e2e4"], stats: {},
    checked_prefix: {
      boundary_state: {
        fen: start, series: 1, quiet_series: 0, ep_targets: [],
        promoted_hex: "0000000000000000", chess960: false,
      },
      prefix: ["e2e4"], san: ["e4"],
      frames: [{ index: 1, uci: "e2e4", san: "e4", board_fen: after }],
      complete: true, board_fen: after, outcome: null,
      next_state: {
        fen: after, series: 2, quiet_series: 0, ep_targets: ["e3"],
        promoted_hex: "0000000000000000", chess960: false,
      },
    },
  };
}
class WorkerDouble {
  constructor() { this.listeners = new Map(); this.messages = []; this.terminated = false; }
  addEventListener(type, listener) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
  }
  emit(type, event) { for (const listener of this.listeners.get(type) || []) listener(event); }
  postMessage(message) {
    this.messages.push(message.type);
    const payload = message.type === "probe" ? identity : resultFor(message.payload);
    queueMicrotask(() => this.emit("message", { data: { id: message.id, ok: true, payload } }));
  }
  terminate() { this.terminated = true; }
}
(async () => {
  const workers = [];
  const client = api.createClient({ workerFactory: () => {
    const worker = new WorkerDouble(); workers.push(worker); return worker;
  } });
  await client.preflight({});
  workers[0].emit("error", { error: new Error("boom") });
  if (client.ready !== false) throw new Error("crashed worker remained ready");
  await client.analyze({
    fen: start, series: 1, quiet_series: 0, ep_targets: [], promoted_hex: "0",
    chess960: false, prefix: [], depth: 5, max_series: 32, time_limit: 30,
    max_generation_positions: 10000000, alternatives: 0, best_move_only: true,
    rate_move: false, save: false,
  });
  if (workers.length !== 2) throw new Error(`expected replacement worker, got ${workers.length}`);
  if (workers[1].messages.join(",") !== "probe,analyze") {
    throw new Error(`replacement was not reprobed: ${workers[1].messages}`);
  }
  process.stdout.write(JSON.stringify({ ready: client.ready, messages: workers[1].messages }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "browser-engine-client.js")],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "ready": True,
        "messages": ["probe", "analyze"],
    }


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser contract tests")
def test_prefix_client_cancels_without_fallback_then_reprobes_prefix_only_worker() -> None:
    script = r"""
const prefixApi = require(process.argv[1]);
const clientApi = require(process.argv[2]);
const source = "a".repeat(16), wasm = "b".repeat(64), moduleHash = "c".repeat(64);
const start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const prefixContract = {
  schema: prefixApi.CONTRACT_SCHEMA,
  result_schema: prefixApi.RESULT_SCHEMA,
  abi_version: 1,
  chess960: false,
  promoted_hex_required_for_product: true,
  limits: { ...prefixApi.HARD_LIMITS },
};
const identity = {
  ready: true,
  certificate_schema: null,
  certificate_status: null,
  contract_version: 1,
  abi_version: 1,
  source_fingerprint: source,
  wasm_sha256: wasm,
  module_js_sha256: moduleHash,
  analysis_ready: false,
  prefix_ready: true,
  safety_certified: false,
  certificate_id: null,
  prefix_certificate_id: "prefix-cert-1",
  runtime_variant: "single",
  thread_count: 1,
  engine_profile_id: null,
  engine_profile_name: null,
  engine_version: "engine-v1",
  ruleset_version: "rules-v1",
  analysis_limits: null,
  prefix_contract: prefixContract,
  memory_limits: {
    initial_bytes: 16777216,
    maximum_bytes: 134217728,
    estimated_peak_bytes: 100663296,
    growth_enabled: true,
  },
};
const payload = {
  fen: start,
  series: 1,
  quiet_series: 0,
  ep_targets: [],
  progressive_ep: [],
  promoted_hex: "0000000000000000",
  chess960: false,
  prefix: [],
};
function resultFor(request) {
  const legal = [{ uci: "e2e4", san: "e4" }];
  return {
    schema: prefixApi.RESULT_SCHEMA,
    abi_version: 1,
    ok: true,
    status: "complete",
    request_id: request.request_id,
    source_fingerprint: source,
    wasm_sha256: wasm,
    module_js_sha256: moduleHash,
    certificate_id: "prefix-cert-1",
    engine_version: "engine-v1",
    ruleset_version: "rules-v1",
    runtime_variant: "single",
    thread_count: 1,
    memory_bytes: 16777216,
    boundary_state: {
      fen: start,
      board_fen: start,
      series: 1,
      series_number: 1,
      side_to_move: "white",
      quiet_series: 0,
      ep_targets: [],
      progressive_ep: [],
      promoted_hex: "0000000000000000",
      chess960: false,
    },
    fen: start,
    board_fen: start,
    prefix: [],
    current_prefix: [],
    san: [],
    frames: [],
    complete: false,
    completion_reason: null,
    check: false,
    ended_by_check: false,
    in_check: false,
    outcome: null,
    remaining: 1,
    moves_remaining: 1,
    unused_moves: 0,
    legal_next: legal,
    legal_moves: legal,
    next_state: null,
  };
}
class WorkerDouble {
  constructor(index) {
    this.index = index;
    this.listeners = new Map();
    this.messages = [];
    this.terminated = false;
  }
  addEventListener(type, listener) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
  }
  emit(type, event) {
    for (const listener of this.listeners.get(type) || []) listener(event);
  }
  postMessage(message) {
    this.messages.push(message.type);
    if (message.type === "prefix" && this.index === 0) return;
    const response = message.type === "probe" ? identity : resultFor(message.payload);
    queueMicrotask(() => this.emit("message", {
      data: { id: message.id, ok: true, payload: response },
    }));
  }
  terminate() { this.terminated = true; }
}
(async () => {
  const workers = [];
  const client = clientApi.createClient({ workerFactory: () => {
    const worker = new WorkerDouble(workers.length);
    workers.push(worker);
    return worker;
  } });
  await client.preflight({});
  if (client.canAnalyze({})) throw new Error("prefix-only artifact became searchable");
  let remoteCalls = 0;
  const remote = {
    identity: {
      source_fingerprint: source,
      engine_version: "engine-v1",
      ruleset_version: "rules-v1",
    },
    request: async () => { remoteCalls += 1; throw new Error("unexpected fallback"); },
  };
  const controller = new AbortController();
  const cancelled = prefixApi.routePrefixRequest({
    payload,
    signal: controller.signal,
    localClient: client,
    remote,
  });
  controller.abort();
  let cancelledName = null;
  try { await cancelled; } catch (error) { cancelledName = error.name; }
  if (cancelledName !== "AbortError") throw new Error(`unexpected abort ${cancelledName}`);
  if (remoteCalls !== 0) throw new Error("aborted prefix request reached hosted fallback");
  if (!workers[0].terminated || client.ready !== false) {
    throw new Error("cancelled prefix worker stayed ready");
  }
  const recovered = await prefixApi.routePrefixRequest({
    payload,
    localClient: client,
    remote,
  });
  if (recovered.status !== "complete" || client.ready !== true) {
    throw new Error("replacement prefix worker did not recover");
  }
  if (workers.length !== 2 || workers[1].messages.join(",") !== "probe,prefix") {
    throw new Error(`replacement prefix worker was not reprobed: ${workers.length}`);
  }
  if (remoteCalls !== 0) throw new Error("successful local replay reached hosted fallback");
  process.stdout.write(JSON.stringify({
    cancelledName,
    remoteCalls,
    ready: client.ready,
    replacementMessages: workers[1].messages,
  }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [
            str(NODE),
            "-e",
            script,
            str(STATIC / "browser-prefix-contract.js"),
            str(STATIC / "browser-engine-client.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "cancelledName": "AbortError",
        "remoteCalls": 0,
        "ready": True,
        "replacementMessages": ["probe", "prefix"],
    }


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser contract tests")
def test_browser_client_deadline_terminates_without_starting_a_fallback_search() -> None:
    script = r"""
const api = require(process.argv[1]);
const identity = {
  ready: true,
  analysis_ready: true,
  prefix_ready: false,
  certificate_schema: "spc-browser-wasm-certificate-v1",
  certificate_status: "certified",
  contract_version: 1,
  abi_version: 1,
  source_fingerprint: "a".repeat(16),
  wasm_sha256: "b".repeat(64),
  module_js_sha256: "c".repeat(64),
  safety_certified: true,
  certificate_id: "cert-1",
  prefix_certificate_id: null,
  prefix_contract: null,
  runtime_variant: "single",
  thread_count: 1,
  engine_profile_id: "spc-test",
  engine_profile_name: "Test",
  engine_version: "engine-v1",
  ruleset_version: "rules-v1",
  analysis_limits: {
    maximum_depth: 8, maximum_max_series: 64, maximum_seconds: 60,
    maximum_generation_positions: 25000000, default_depth: 5,
    default_max_series: 32, default_seconds: 30,
    default_generation_positions: 10000000,
  },
  memory_limits: {
    initial_bytes: 16777216, maximum_bytes: 134217728,
    estimated_peak_bytes: 100663296, growth_enabled: true,
  },
};
class BlockingWorker {
  constructor() { this.listeners = new Map(); this.terminated = false; this.analyzeCalls = 0; }
  addEventListener(type, listener) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
  }
  postMessage(message) {
    if (message.type === "analyze") { this.analyzeCalls += 1; return; }
    queueMicrotask(() => {
      for (const listener of this.listeners.get("message") || []) {
        listener({ data: { id: message.id, ok: true, payload: identity } });
      }
    });
  }
  terminate() { this.terminated = true; }
}
(async () => {
  const worker = new BlockingWorker();
  const client = api.createClient({ workerFactory: () => worker });
  await client.preflight({});
  let code = null;
  try {
    await client.analyze({
      fen: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
      series: 1, quiet_series: 0, ep_targets: [], promoted_hex: "0",
      chess960: false, prefix: [], depth: 5, max_series: 32, time_limit: 30,
      max_generation_positions: 10000000, alternatives: 0,
      best_move_only: true, rate_move: false, save: false,
    }, { deadlineMs: performance.now() + 100 });
  } catch (error) {
    code = error.code;
  }
  if (code !== "browser-analysis-deadline") throw new Error(`unexpected deadline ${code}`);
  if (!worker.terminated || client.ready !== false) throw new Error("deadline left worker ready");
  if (worker.analyzeCalls !== 1) throw new Error("deadline started another search");
  process.stdout.write(JSON.stringify({ code, terminated: worker.terminated, calls: worker.analyzeCalls }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "browser-engine-client.js")],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "code": "browser-analysis-deadline",
        "terminated": True,
        "calls": 1,
    }


def test_web_boundary_contract_round_trips_promoted_provenance() -> None:
    fen = "7k/8/8/8/8/8/Q7/7K w - - 0 1"
    promoted = f"{chess.BB_A2:016x}"
    state = state_from_payload(
        {
            "fen": fen,
            "series": 1,
            "quiet_series": 0,
            "ep_targets": [],
            "promoted_hex": promoted,
            "chess960": False,
        }
    )

    boundary = inspect_prefix(state, ())["boundary_state"]

    assert state.board.promoted == chess.BB_A2
    assert boundary["promoted_hex"] == promoted
    assert boundary["chess960"] is False


def test_web_boundary_contract_rejects_promoted_pawns() -> None:
    with pytest.raises(APIError, match="occupied non-pawn"):
        state_from_payload(
            {
                "fen": chess.STARTING_FEN,
                "series": 1,
                "quiet_series": 0,
                "ep_targets": [],
                "promoted_hex": f"{chess.BB_A2:016x}",
                "chess960": False,
            }
        )
