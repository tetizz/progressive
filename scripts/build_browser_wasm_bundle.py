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
MIN_PREFIX_DIFFERENTIAL_CASES = 14
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
    support_paths: tuple[Path, ...],
    source_fingerprint: str,
    destination: Path,
) -> Mapping[str, Any]:
    if runtime_variant == "single" and support_paths:
        raise ValueError(
            "the verified single-thread lane may not load external support files"
        )
    if certificate_path is None and prefix_certificate_path is None:
        raise ValueError(
            f"{runtime_variant} requires a search or prefix certificate"
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
    for path, label in required_paths:
        if not path.is_file():
            raise FileNotFoundError(f"{runtime_variant} {label} is missing: {path}")
    certificate = load_certificate(certificate_path) if certificate_path else None
    prefix_certificate = (
        load_certificate(prefix_certificate_path)
        if prefix_certificate_path
        else None
    )
    certificate_thread_counts = [
        value.get("thread_count")
        for value in (certificate, prefix_certificate)
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
    return variant


def build_bundle(
    *,
    single_wasm: Path,
    single_module_js: Path,
    single_certificate_path: Path | None = None,
    single_prefix_certificate_path: Path | None = None,
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
    if certificate_value is None and prefix_certificate_value is None:
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
    ):
        parser.error(
            "building a bundle requires --single-certificate, "
            "--single-prefix-certificate, or both"
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
