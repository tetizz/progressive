from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.build_root_session_wasm import (  # noqa: E402
    EXPORTED_FUNCTIONS,
    SOURCES as KERNEL_SOURCES,
)
from benchmarks.check_wasm_dependency_closure import REQUIRED as CLOSURE_SOURCES  # noqa: E402
from scripts import build_browser_wasm_bundle as bundle_builder  # noqa: E402


RELEASE_SCHEMA = "spc-browser-wasm-release-promotion-v1"
BUILD_SCHEMA = "spc-root-session-build-receipt-v1"
ROOT_SMOKE_SCHEMA = "spc-root-session-wasm-smoke-v1"
ROOT_PARITY_SCHEMA = "spc-root-d5-oracle-v1"
PREFIX_PARITY_SCHEMA = "spc-prefix-parity-receipt-v2"
BROWSER_PREFIX_SCHEMA = "spc-browser-prefix-contract-receipt-v1"
MATE_PARITY_SCHEMA = "spc-mate-wasm-receipt-v2"
OPERA_CDP_SCHEMA = "spc-opera-root-session-cdp-receipt-v1"
OPERA_WORKER_SCHEMA = "spc-opera-root-d5-benchmark-v2"
HEX_64 = re.compile(r"[0-9a-f]{64}")
GIT_REVISION = re.compile(r"[0-9a-f]{40,64}")
ARTIFACT_IDENTITY_FIELDS = (
    "source_revision",
    "source_fingerprint",
    "kernel_sha256",
    "wasm_sha256",
    "module_js_sha256",
    "artifact_set_sha256",
)
RUNTIME_IDENTITY_FIELDS = (
    "exception_strategy",
    "wasm_simd",
    "allocator",
)
RECEIPT_FILENAMES = {
    "build": "root-session-build-receipt.json",
    "root_smoke": "root-session-smoke-receipt.json",
    "root_parity": "root-session-parity-receipt.json",
    "prefix_parity": "prefix-parity-receipt.json",
    "browser_prefix": "browser-prefix-receipt.json",
    "mate_parity": "mate-parity-receipt.json",
    "opera": "opera-d1-d5-receipt.json",
}


class ReleaseGateError(ValueError):
    """Raised when release evidence cannot support a promotion."""


@dataclass(frozen=True)
class Receipt:
    label: str
    path: Path
    raw: bytes
    payload: Mapping[str, Any]
    sha256: str


@dataclass(frozen=True)
class BuildEvidence:
    receipt: Receipt
    identity: dict[str, Any]
    runtime_identity: dict[str, Any]
    runtime_requirements: dict[str, Any]
    memory: dict[str, int | bool]
    full_memory: dict[str, Any]
    engine: dict[str, str]
    toolchain: dict[str, str]
    wasm: Path
    module_js: Path
    source_fingerprint: str
    dependency_closure: dict[str, Any]


@dataclass(frozen=True)
class ValidatedEvidence:
    build: BuildEvidence
    receipts: dict[str, Receipt]
    root_contract: dict[str, Any]
    prefix_contract: dict[str, Any]
    oracle_signature_sha256: str
    root_config: dict[str, Any]
    root_differential_cases: int
    prefix_differential_cases: int
    mate_differential_cases: int
    opera_elapsed_seconds: float
    opera_result: dict[str, Any]
    opera_memory: dict[str, Any]
    safety_reserve_positions: int


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseGateError(f"{label} must be a JSON object")
    return value


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ReleaseGateError(f"{label} must be a JSON array")
    return value


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReleaseGateError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: object, label: str, minimum: float = 0.0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise ReleaseGateError(f"{label} must be a finite number >= {minimum}")
    return float(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ReleaseGateError(f"{label} must be non-empty canonical text")
    return value


def _true(mapping: Mapping[str, Any], key: str, label: str) -> None:
    if mapping.get(key) is not True:
        raise ReleaseGateError(f"{label} gate {key!r} did not pass")


def _false(mapping: Mapping[str, Any], key: str, label: str) -> None:
    if mapping.get(key) is not False:
        raise ReleaseGateError(f"{label} field {key!r} must be false")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _load_receipt(label: str, path: Path) -> Receipt:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseGateError(f"could not read {label} receipt: {error}") from error
    return Receipt(
        label=label,
        path=path.resolve(),
        raw=raw,
        payload=_mapping(payload, f"{label} receipt"),
        sha256=_sha256_bytes(raw),
    )


def _run_git(repository: Path, *arguments: str, text: bool = True) -> Any:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=text,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseGateError(f"git {' '.join(arguments)} failed: {error}") from error
    return completed.stdout


def _relative_to(path: Path, parent: Path, label: str) -> Path:
    try:
        return path.resolve().relative_to(parent.resolve())
    except ValueError as error:
        raise ReleaseGateError(f"{label} must stay within {parent}") from error


def _safe_receipt_path(value: object, label: str) -> PurePosixPath:
    text = _text(value, label)
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseGateError(f"{label} is not a safe relative path")
    return path


def _validate_record(
    record: object,
    *,
    base: Path,
    label: str,
) -> tuple[dict[str, object], Path]:
    item = _mapping(record, label)
    if set(item) != {"path", "sha256", "bytes"}:
        raise ReleaseGateError(f"{label} must exactly contain path, sha256, and bytes")
    relative = _safe_receipt_path(item.get("path"), f"{label} path")
    digest = item.get("sha256")
    if not isinstance(digest, str) or not HEX_64.fullmatch(digest):
        raise ReleaseGateError(f"{label} has an invalid SHA-256")
    size = _integer(item.get("bytes"), f"{label} bytes", 0)
    path = base.joinpath(*relative.parts).resolve()
    _relative_to(path, base, label)
    if not path.is_file():
        raise ReleaseGateError(f"{label} file is missing: {path}")
    if path.stat().st_size != size or _sha256_file(path) != digest:
        raise ReleaseGateError(f"{label} file bytes do not match the receipt")
    return {"path": relative.as_posix(), "sha256": digest, "bytes": size}, path


def _artifact_identity(build: Mapping[str, Any]) -> dict[str, Any]:
    identity = {key: build.get(key) for key in ARTIFACT_IDENTITY_FIELDS}
    revision = identity["source_revision"]
    if not isinstance(revision, str) or not GIT_REVISION.fullmatch(revision):
        raise ReleaseGateError("build receipt has an invalid source revision")
    fingerprint = identity["source_fingerprint"]
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{16}", fingerprint):
        raise ReleaseGateError("build receipt has an invalid source fingerprint")
    for key in (
        "kernel_sha256",
        "wasm_sha256",
        "module_js_sha256",
        "artifact_set_sha256",
    ):
        value = identity[key]
        if not isinstance(value, str) or not HEX_64.fullmatch(value):
            raise ReleaseGateError(f"build receipt has an invalid {key}")
    return identity


def _require_identity(
    value: object,
    expected: Mapping[str, Any],
    label: str,
    *,
    fields: Sequence[str] = ARTIFACT_IDENTITY_FIELDS,
) -> Mapping[str, Any]:
    subject = _mapping(value, f"{label} artifact identity")
    for key in fields:
        if subject.get(key) != expected[key]:
            raise ReleaseGateError(
                f"{label} artifact identity {key!r} does not match the build"
            )
    return subject


def _runtime_identity(build: Mapping[str, Any]) -> dict[str, Any]:
    optimization = _mapping(build.get("optimization"), "build optimization")
    if optimization.get("level") != "O3" or optimization.get("lto") is not True:
        raise ReleaseGateError("release WASM must be an O3 LTO build")
    exception_strategy = optimization.get("exception_strategy")
    if exception_strategy not in {"emscripten", "wasm"}:
        raise ReleaseGateError("build exception strategy is unsupported")
    expected_exception_flag = (
        "-fwasm-exceptions" if exception_strategy == "wasm" else "-fexceptions"
    )
    if optimization.get("exception_flag") != expected_exception_flag:
        raise ReleaseGateError("build exception flag disagrees with its strategy")
    wasm_simd = optimization.get("wasm_simd")
    if not isinstance(wasm_simd, bool):
        raise ReleaseGateError("build wasm_simd must be boolean")
    expected_simd_flag = "-msimd128" if wasm_simd else None
    if optimization.get("simd_flag") != expected_simd_flag:
        raise ReleaseGateError("build SIMD flag disagrees with wasm_simd")
    allocator = optimization.get("allocator")
    if allocator not in {"dlmalloc", "emmalloc"}:
        raise ReleaseGateError("build allocator is unsupported")
    return {
        "exception_strategy": exception_strategy,
        "wasm_simd": wasm_simd,
        "allocator": allocator,
    }


def _validate_memory(build: Mapping[str, Any]) -> tuple[dict[str, int | bool], dict[str, Any]]:
    full = dict(_mapping(build.get("memory_envelope"), "build memory envelope"))
    if set(full) != {
        "initial_bytes",
        "estimated_peak_bytes",
        "maximum_bytes",
        "growth_enabled",
        "stack_bytes",
        "hard_maximum_linked",
        "runtime_peak_verified",
    }:
        raise ReleaseGateError("build memory envelope has unknown or missing fields")
    normalized = bundle_builder.validate_memory_limits(
        {
            key: full[key]
            for key in (
                "initial_bytes",
                "maximum_bytes",
                "estimated_peak_bytes",
                "growth_enabled",
            )
        }
    )
    stack = _integer(full.get("stack_bytes"), "build stack bytes", 1)
    if stack % 65_536 or stack > int(normalized["initial_bytes"]):
        raise ReleaseGateError("build stack must be 64KiB-aligned within initial memory")
    if full.get("hard_maximum_linked") is not True:
        raise ReleaseGateError("build did not link a hard maximum memory")
    if full.get("runtime_peak_verified") is not False:
        raise ReleaseGateError("build-stage runtime_peak_verified must remain false")
    return normalized, full


def _validate_source_checkout(
    repository: Path,
    source_package: Path,
    build: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    repository = repository.resolve()
    source_package = source_package.resolve()
    relative_package = _relative_to(source_package, repository, "source package")
    head = str(_run_git(repository, "rev-parse", "HEAD")).strip()
    if build.get("source_revision") != head:
        raise ReleaseGateError("build source revision is not the checked-out HEAD")
    status = _run_git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        relative_package.as_posix(),
    )
    if str(status).strip():
        raise ReleaseGateError("engine source package is dirty or contains untracked inputs")
    source_paths = sorted(
        (
            path
            for pattern in ("*.py", "*.cpp", "*.hpp", "*.h")
            for path in source_package.rglob(pattern)
        ),
        key=lambda item: item.relative_to(source_package).as_posix(),
    )
    if not source_paths:
        raise ReleaseGateError("engine source package has no fingerprintable inputs")
    tracked_raw = _run_git(repository, "ls-files", "-z", "--", relative_package.as_posix(), text=False)
    tracked = {
        item.decode("utf-8").replace("\\", "/")
        for item in tracked_raw.split(b"\0")
        if item
    }
    relative_sources = {
        path.relative_to(repository).as_posix() for path in source_paths
    }
    missing_tracked = sorted(relative_sources - tracked)
    if missing_tracked:
        raise ReleaseGateError(
            f"engine fingerprint includes untracked inputs: {missing_tracked}"
        )
    calculated_fingerprint = bundle_builder.engine_source_fingerprint(source_package)
    if build.get("source_fingerprint") != calculated_fingerprint:
        raise ReleaseGateError("build source fingerprint does not match the checkout")

    inputs = _list(build.get("source_inputs"), "build source inputs")
    records: list[dict[str, object]] = []
    record_paths: list[str] = []
    for index, value in enumerate(inputs):
        record, _ = _validate_record(
            value,
            base=repository,
            label=f"build source input {index}",
        )
        records.append(record)
        record_paths.append(str(record["path"]))
    expected_paths = sorted(KERNEL_SOURCES)
    if record_paths != expected_paths:
        raise ReleaseGateError("build source inputs are not the canonical kernel closure")
    if _canonical_sha256(records) != build.get("kernel_sha256"):
        raise ReleaseGateError("kernel source-set digest does not match the build")
    missing = [path for path in CLOSURE_SOURCES if not (repository / path).is_file()]
    untracked = [path for path in CLOSURE_SOURCES if path not in tracked]
    if missing or untracked:
        raise ReleaseGateError("WASM dependency closure is incomplete or untracked")
    closure = {
        "schema": "spc-wasm-dependency-closure-v2",
        "target": "ordinary-worker-root-session-prefix-mate",
        "ok": True,
        "source_revision": head,
        "required": list(CLOSURE_SOURCES),
        "missing_from_worktree": [],
        "missing_from_clean_checkout": [],
    }
    return calculated_fingerprint, closure


def _validate_build_receipt(
    receipt: Receipt,
    *,
    repository: Path,
    source_package: Path,
) -> BuildEvidence:
    build = receipt.payload
    if build.get("schema") != BUILD_SCHEMA:
        raise ReleaseGateError("unexpected root-session build receipt schema")
    if build.get("status") != "built-not-certified":
        raise ReleaseGateError("build receipt is not in built-not-certified state")
    _false(build, "product_publishable", "build")
    if build.get("certificate_id") is not None:
        raise ReleaseGateError("build receipt must not arrive pre-certified")
    identity = _artifact_identity(build)
    runtime_identity = _runtime_identity(build)
    memory, full_memory = _validate_memory(build)
    source_fingerprint, closure = _validate_source_checkout(
        repository,
        source_package,
        build,
    )

    if (
        build.get("runtime_variant") != "single"
        or build.get("thread_count") != 1
        or build.get("pthreads") is not False
        or build.get("support_files") != []
    ):
        raise ReleaseGateError("only the single-thread ordinary-Worker build can ship")
    expected_runtime = {
        "ordinary_module_worker": True,
        "pthreads": False,
        "cross_origin_isolated": False,
        "native_wasm_exception_handling": runtime_identity["exception_strategy"] == "wasm",
        "wasm_simd": runtime_identity["wasm_simd"],
    }
    runtime_requirements = dict(
        _mapping(build.get("runtime_requirements"), "build runtime requirements")
    )
    if runtime_requirements != expected_runtime:
        raise ReleaseGateError("build runtime requirements are internally inconsistent")
    abi = _mapping(build.get("abi"), "build ABI")
    if (
        abi.get("root_session_version") != 2
        or abi.get("prefix_kernel_version") != 1
        or abi.get("series_mate_version") != 1
        or abi.get("exports") != list(EXPORTED_FUNCTIONS)
        or abi.get("reply_mate_safety") is not False
        or abi.get("canonical_root_tactical_policy")
        != "canonical-boundary-policy-v1"
        or abi.get("legacy_root_tactical_protection") is not False
    ):
        raise ReleaseGateError("build does not carry the combined root/prefix/mate ABI")

    records = _list(build.get("artifacts"), "build artifacts")
    if len(records) != 2:
        raise ReleaseGateError("combined build must contain exactly module and WASM artifacts")
    normalized_records: list[dict[str, object]] = []
    paths: list[Path] = []
    for index, value in enumerate(records):
        record, path = _validate_record(
            value,
            base=receipt.path.parent,
            label=f"build artifact {index}",
        )
        normalized_records.append(record)
        paths.append(path)
    if normalized_records != sorted(normalized_records, key=lambda item: str(item["path"])):
        raise ReleaseGateError("build artifact records are not in canonical order")
    if _canonical_sha256(normalized_records) != identity["artifact_set_sha256"]:
        raise ReleaseGateError("build artifact-set digest is invalid")
    wasm_matches = [path for path in paths if _sha256_file(path) == identity["wasm_sha256"]]
    module_matches = [
        path for path in paths if _sha256_file(path) == identity["module_js_sha256"]
    ]
    if len(wasm_matches) != 1 or len(module_matches) != 1 or wasm_matches[0] == module_matches[0]:
        raise ReleaseGateError("build artifact hashes do not identify one module and one WASM")
    wasm = wasm_matches[0]
    module_js = module_matches[0]
    if wasm.suffix != ".wasm" or module_js.suffix not in {".js", ".mjs"}:
        raise ReleaseGateError("build artifact extensions do not match their roles")

    toolchain_raw = _mapping(build.get("toolchain"), "build toolchain")
    if set(toolchain_raw) != {"path", "sha256", "version"}:
        raise ReleaseGateError("build toolchain subject has unknown or missing fields")
    compiler = Path(_text(toolchain_raw.get("path"), "toolchain path")).resolve()
    compiler_digest = toolchain_raw.get("sha256")
    if (
        not compiler.is_file()
        or not isinstance(compiler_digest, str)
        or not HEX_64.fullmatch(compiler_digest)
        or _sha256_file(compiler) != compiler_digest
    ):
        raise ReleaseGateError("build toolchain executable is missing or changed")
    try:
        compiler_version = subprocess.run(
            [str(compiler), "--version"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseGateError(f"could not re-identify the build compiler: {error}") from error
    toolchain = {
        "path": str(compiler),
        "sha256": compiler_digest,
        "version": _text(toolchain_raw.get("version"), "toolchain version"),
    }
    if toolchain["version"] != compiler_version:
        raise ReleaseGateError("build compiler version output changed from its receipt")

    command = _list(build.get("command"), "build command")
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise ReleaseGateError("build command must be a non-empty string array")
    if Path(str(command[0])).resolve() != compiler:
        raise ReleaseGateError("build command compiler differs from the toolchain subject")
    expected_exception_flag = (
        "-fwasm-exceptions"
        if runtime_identity["exception_strategy"] == "wasm"
        else "-fexceptions"
    )
    required_flags = {
        "-std=c++20",
        "-O3",
        "-flto",
        expected_exception_flag,
        "-DSPC_NATIVE_CORE_ONLY=1",
        "-DSPC_NATIVE_MATE_CORE_ONLY=1",
        "-sALLOW_MEMORY_GROWTH=1",
        f"-sINITIAL_MEMORY={full_memory['initial_bytes']}",
        f"-sMAXIMUM_MEMORY={full_memory['maximum_bytes']}",
        f"-sSTACK_SIZE={full_memory['stack_bytes']}",
        "-sABORTING_MALLOC=0",
        f"-sMALLOC={runtime_identity['allocator']}",
        "-sUSE_PTHREADS=0",
        "-sWASM_WORKERS=0",
        "-sENVIRONMENT=worker,node",
        "-sMODULARIZE=1",
        "-sEXPORT_ES6=1",
        "-sFILESYSTEM=0",
        "-sDYNAMIC_EXECUTION=0",
        f"-sEXPORTED_FUNCTIONS={','.join(EXPORTED_FUNCTIONS)}",
        "-sEXPORTED_RUNTIME_METHODS=UTF8ToString,stringToNewUTF8,HEAPU8",
    }
    missing_flags = sorted(required_flags - set(command))
    if missing_flags:
        raise ReleaseGateError(f"build command omits required flags: {missing_flags}")
    if ("-msimd128" in command) is not bool(runtime_identity["wasm_simd"]):
        raise ReleaseGateError("build command SIMD flag differs from its runtime identity")
    compiled_sources = {
        str((source_package / name).resolve())
        for name in (
            "_native_eval.cpp",
            "native_subtree.cpp",
            "native_subtree_wasm.cpp",
            "native_root_session_wasm.cpp",
            "_native_mate.cpp",
        )
    }
    command_paths = {
        str(Path(item).resolve())
        for item in command
        if item.lower().endswith((".cpp", ".cc", ".cxx"))
    }
    if command_paths != compiled_sources:
        raise ReleaseGateError("build command compiled-source closure is incomplete or expanded")
    if command.count("-I") != 1:
        raise ReleaseGateError("build command must carry one source include path")
    include_index = command.index("-I")
    if include_index + 1 >= len(command) or Path(command[include_index + 1]).resolve() != source_package.resolve():
        raise ReleaseGateError("build command include path differs from the engine package")
    if command.count("-o") != 1:
        raise ReleaseGateError("build command must carry one output module path")
    output_index = command.index("-o")
    if output_index + 1 >= len(command) or Path(command[output_index + 1]).resolve() != module_js:
        raise ReleaseGateError("build command output path differs from the verified module")

    expected_command = [
        str(compiler),
        *(
            str((source_package / name).resolve())
            for name in (
                "_native_eval.cpp",
                "native_subtree.cpp",
                "native_subtree_wasm.cpp",
                "native_root_session_wasm.cpp",
                "_native_mate.cpp",
            )
        ),
        "-I",
        str(source_package.resolve()),
        "-std=c++20",
        "-O3",
        "-flto",
        expected_exception_flag,
        "-DSPC_NATIVE_CORE_ONLY=1",
        "-DSPC_NATIVE_MATE_CORE_ONLY=1",
        "-sALLOW_MEMORY_GROWTH=1",
        f"-sINITIAL_MEMORY={full_memory['initial_bytes']}",
        f"-sMAXIMUM_MEMORY={full_memory['maximum_bytes']}",
        f"-sSTACK_SIZE={full_memory['stack_bytes']}",
        "-sABORTING_MALLOC=0",
        f"-sMALLOC={runtime_identity['allocator']}",
        "-sUSE_PTHREADS=0",
        "-sWASM_WORKERS=0",
        "-sENVIRONMENT=worker,node",
        "-sMODULARIZE=1",
        "-sEXPORT_ES6=1",
        "-sFILESYSTEM=0",
        "-sDYNAMIC_EXECUTION=0",
        f"-sEXPORTED_FUNCTIONS={','.join(EXPORTED_FUNCTIONS)}",
        "-sEXPORTED_RUNTIME_METHODS=UTF8ToString,stringToNewUTF8,HEAPU8",
        "-o",
        str(module_js),
    ]
    if runtime_identity["wasm_simd"]:
        expected_command.insert(
            expected_command.index(expected_exception_flag) + 1,
            "-msimd128",
        )
    normalized_command = list(command)
    path_indexes = set(range(0, 6)) | {7, len(normalized_command) - 1}
    for index in path_indexes:
        if 0 <= index < len(normalized_command):
            normalized_command[index] = str(Path(normalized_command[index]).resolve())
    if normalized_command != expected_command:
        raise ReleaseGateError("build command is not the exact canonical builder invocation")

    engine = {
        "engine_version": _text(build.get("engine_version"), "engine version"),
        "ruleset_version": _text(build.get("ruleset_version"), "ruleset version"),
        "profile_id": _text(build.get("profile_id"), "profile id"),
    }
    return BuildEvidence(
        receipt=receipt,
        identity=identity,
        runtime_identity=runtime_identity,
        runtime_requirements=runtime_requirements,
        memory=memory,
        full_memory=full_memory,
        engine=engine,
        toolchain=toolchain,
        wasm=wasm,
        module_js=module_js,
        source_fingerprint=source_fingerprint,
        dependency_closure=closure,
    )


def _validate_root_smoke(receipt: Receipt, build: BuildEvidence) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = receipt.payload
    if payload.get("schema") != ROOT_SMOKE_SCHEMA or payload.get("status") != "passed-not-certified":
        raise ReleaseGateError("root smoke receipt did not pass its verifier")
    _false(payload, "product_publishable", "root smoke")
    _false(payload, "safety_certified", "root smoke")
    if payload.get("certificate_id") is not None:
        raise ReleaseGateError("root smoke receipt must not be pre-certified")
    _require_identity(payload, build.identity, "root smoke")
    for key in RUNTIME_IDENTITY_FIELDS:
        if payload.get(key) != build.runtime_identity[key]:
            raise ReleaseGateError(f"root smoke runtime identity {key!r} drifted")
    if payload.get("runtime_requirements") != build.runtime_requirements:
        raise ReleaseGateError("root smoke runtime requirements drifted")
    if payload.get("runtime_variant") != "single" or payload.get("thread_count") != 1:
        raise ReleaseGateError("root smoke did not execute the single-thread lane")
    memory = _mapping(payload.get("memory"), "root smoke memory")
    if memory.get("configured") != build.full_memory:
        raise ReleaseGateError("root smoke memory configuration drifted from the build")
    maximum = int(build.memory["maximum_bytes"])
    for key in ("observed_bytes", "native_peak_bytes"):
        value = _integer(memory.get(key), f"root smoke memory {key}", 1)
        if value > maximum:
            raise ReleaseGateError(f"root smoke {key} exceeded the hard memory maximum")
    gates = _mapping(payload.get("gates"), "root smoke gates")
    for key in (
        "combined_exports",
        "root_contract_reply_mate_safety_false",
        "persistent_d1_d2_session",
        "selected_owner_warm_exact_certification",
        "cumulative_work_and_cache_receipts",
        "exact_manifest_import",
        "configured_max_depth_rejected",
        "work_limit_fail_closed",
        "deadline_fail_closed",
        "prefix_smoke",
        "mate_found_exhausted_unknown",
        "canonical_root_tactical_policy",
        "legacy_root_tactical_policy_rejected",
        "canonical_root_tactical_boundary_echoes",
    ):
        _true(gates, key, "root smoke")
    contract = dict(_mapping(payload.get("root_session_contract"), "root session contract"))
    capabilities = _mapping(contract.get("capabilities"), "root session capabilities")
    hard_limits = _mapping(contract.get("hard_limits"), "root session hard limits")
    if capabilities.get("canonical_root_tactical_policy") is not True:
        raise ReleaseGateError("root session contract lacks canonical tactical policy")
    if (
        hard_limits.get("root_tactical_policy") != "canonical-boundary-policy-v1"
        or hard_limits.get("root_tactical_protection_values") != [False]
    ):
        raise ReleaseGateError("root session contract permits the legacy tactical policy")
    manifest_contract = _mapping(contract.get("manifest"), "root session manifest contract")
    if manifest_contract.get("root_tactical_policy") != "canonical-boundary-policy-v1":
        raise ReleaseGateError("root session manifest omits the canonical tactical policy")
    prefix_raw = _mapping(payload.get("prefix_contract"), "prefix contract")
    hard_limits = prefix_raw.get("hard_limits", prefix_raw.get("limits"))
    prefix_contract = {
        "schema": prefix_raw.get("schema"),
        "result_schema": prefix_raw.get("result_schema"),
        "abi_version": prefix_raw.get("abi_version"),
        "chess960": prefix_raw.get("chess960"),
        "promoted_hex_required_for_product": prefix_raw.get(
            "promoted_hex_required_for_product"
        ),
        "limits": dict(_mapping(hard_limits, "prefix contract hard limits")),
    }
    bundle_builder.validate_prefix_contract(prefix_contract)
    mate_receipts = _mapping(payload.get("mate_receipts"), "root smoke mate receipts")
    expected_mate = {
        "found": ("found", "found", True),
        "exhausted": ("exhausted", "exhausted", True),
        "work_limit": ("work_limit", "unknown", False),
        "deadline": ("deadline", "unknown", False),
    }
    for key, (kernel_status, proof_status, complete) in expected_mate.items():
        item = _mapping(mate_receipts.get(key), f"root smoke mate {key}")
        if (
            item.get("kernel_status") != kernel_status
            or item.get("proof_status") != proof_status
            or item.get("complete") is not complete
        ):
            raise ReleaseGateError(f"root smoke mate {key} receipt is not fail-closed")
        stats = _mapping(item.get("stats"), f"root smoke mate {key} stats")
        _integer(stats.get("positions_visited"), f"root smoke mate {key} positions")
        _integer(stats.get("moves_generated"), f"root smoke mate {key} moves")
    return contract, prefix_contract


def _validate_root_parity(
    receipt: Receipt,
    build: BuildEvidence,
    root_contract: Mapping[str, Any],
) -> tuple[int, dict[str, Any], dict[str, Any], list[dict[str, Any]], str, str]:
    payload = receipt.payload
    if payload.get("schema") != ROOT_PARITY_SCHEMA or payload.get("status") != "passed":
        raise ReleaseGateError("root D5 oracle receipt did not pass")
    artifact = _require_identity(payload.get("artifact"), build.identity, "root D5 oracle")
    for key in RUNTIME_IDENTITY_FIELDS:
        if artifact.get(key) != build.runtime_identity[key]:
            raise ReleaseGateError(f"root D5 oracle runtime identity {key!r} drifted")
    oracle_subject_extras = {
        "runtime_variant": "single",
        "thread_count": 1,
        **build.engine,
    }
    for key, expected in oracle_subject_extras.items():
        if artifact.get(key) != expected:
            raise ReleaseGateError(f"root D5 oracle identity {key!r} drifted")
    if payload.get("failures") != 0:
        raise ReleaseGateError("root D5 oracle reports failures")
    differential_cases = _integer(
        payload.get("differential_cases"),
        "root D5 oracle differential cases",
        3,
    )
    boundary = dict(_mapping(payload.get("boundary"), "root D5 oracle boundary"))
    expected_boundary = {
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "series": 1,
        "quiet_series": 0,
        "progressive_ep": [],
        "promoted_hex": "0000000000000000",
        "chess960": False,
    }
    for key, expected in expected_boundary.items():
        if boundary.get(key) != expected:
            raise ReleaseGateError(f"root D5 oracle boundary {key!r} is not the start position")
    config = dict(_mapping(payload.get("session_config"), "root D5 oracle config"))
    if config.get("max_depth") != 5 or config.get("width") != 32:
        raise ReleaseGateError("root oracle session config must be exactly W32 D5")
    if config.get("root_tactical_protection") is not False:
        raise ReleaseGateError("root oracle must use canonical per-boundary tactical protection")
    try:
        normalized_config = bundle_builder._validate_root_session_config(
            config,
            root_contract,
        )
    except ValueError as error:
        raise ReleaseGateError(f"root D5 oracle session config is invalid: {error}") from error
    if config != normalized_config:
        raise ReleaseGateError("root D5 oracle session config is not canonical")
    if payload.get("memory") != build.memory:
        raise ReleaseGateError("root D5 oracle memory envelope differs from the build")
    retained_manifest_sha256 = payload.get("retained_manifest_sha256")
    if not isinstance(retained_manifest_sha256, str) or not HEX_64.fullmatch(
        retained_manifest_sha256
    ):
        raise ReleaseGateError("root D5 oracle lacks a retained-manifest digest")

    selected = dict(_mapping(payload.get("selected"), "root D5 oracle selection"))
    _text(selected.get("candidate_identity"), "root D5 oracle candidate identity")
    move = _text(selected.get("move"), "root D5 oracle move")
    _integer(selected.get("score"), "root D5 oracle score", -2_000_000_000)
    proof = _list(selected.get("proof_bounds"), "root D5 oracle proof bounds")
    if len(proof) != 2 or any(isinstance(item, bool) or not isinstance(item, int) for item in proof):
        raise ReleaseGateError("root D5 oracle proof bounds are invalid")
    principal_variation = _list(
        selected.get("principal_variation"),
        "root D5 oracle principal variation",
    )
    if not principal_variation:
        raise ReleaseGateError("root D5 oracle must retain the full principal variation")
    first_series = _mapping(principal_variation[0], "root D5 oracle first PV series")
    if first_series.get("machine_notation") != move:
        raise ReleaseGateError("root D5 oracle move differs from its full principal variation")
    pv_sha256 = selected.get("principal_variation_sha256")
    if pv_sha256 != _canonical_sha256(principal_variation):
        raise ReleaseGateError("root D5 oracle principal-variation digest is invalid")

    rivals = dict(_mapping(payload.get("rival_bounds"), "root D5 oracle rival bounds"))
    if rivals.get("coverage_complete") is not True or rivals.get("candidate_count") != 20:
        raise ReleaseGateError("root D5 oracle does not cover all 20 start candidates")
    if rivals.get("unknown_count") != 0:
        raise ReleaseGateError("root D5 oracle contains an Unknown rival bound")
    bounds = _list(rivals.get("bounds"), "root D5 oracle bounds")
    if len(bounds) != 20:
        raise ReleaseGateError("root D5 oracle must retain exactly 20 candidate bounds")
    normalized_bound_ids: list[str] = []
    bound_counts = {"exact": 0, "lower": 0, "upper": 0}
    for raw_bound in bounds:
        bound = _mapping(raw_bound, "root D5 oracle candidate bound")
        candidate_identity = _text(
            bound.get("candidate_identity"),
            "root D5 oracle rival candidate identity",
        )
        normalized_bound_ids.append(candidate_identity)
        bound_type = bound.get("bound")
        if bound_type not in bound_counts:
            raise ReleaseGateError("root D5 oracle contains an invalid candidate bound")
        bound_counts[str(bound_type)] += 1
        _integer(bound.get("score"), "root D5 oracle rival score", -2_000_000_000)
        rival_proof = _list(bound.get("proof_bounds"), "root D5 oracle rival proof bounds")
        if len(rival_proof) != 2 or any(
            isinstance(item, bool) or not isinstance(item, int) for item in rival_proof
        ):
            raise ReleaseGateError("root D5 oracle rival proof bounds are invalid")
    if len(set(normalized_bound_ids)) != 20 or normalized_bound_ids != sorted(normalized_bound_ids):
        raise ReleaseGateError("root D5 oracle bounds must be unique and canonical by identity")
    for key, count in bound_counts.items():
        if rivals.get(f"{key}_count") != count:
            raise ReleaseGateError(f"root D5 oracle {key} bound count is inconsistent")
    if rivals.get("coverage_sha256") != _canonical_sha256(bounds):
        raise ReleaseGateError("root D5 oracle rival coverage digest is invalid")

    work = _mapping(payload.get("work"), "root D5 oracle work")
    if (
        work.get("status") != "complete"
        or work.get("within_cap") is not True
        or work.get("unknown_or_limit_count") != 0
        or work.get("max_work") != config.get("max_work")
    ):
        raise ReleaseGateError("root D5 oracle work proof is incomplete or limited")
    accounted_work = _integer(work.get("accounted_work"), "root D5 oracle accounted work")
    if accounted_work > _integer(config.get("max_work"), "root D5 oracle max work", 1):
        raise ReleaseGateError("root D5 oracle exceeded its work cap")
    deadline = _mapping(payload.get("deadline"), "root D5 oracle deadline")
    if (
        deadline.get("status") != "complete"
        or deadline.get("deadline_reached") is not False
        or deadline.get("unknown_or_limit_count") != 0
    ):
        raise ReleaseGateError("root D5 oracle deadline proof is incomplete or limited")
    deadline_limit_ms = _number(
        deadline.get("deadline_limit_ms"),
        "root D5 oracle deadline limit",
        0.000001,
    )
    _number(deadline.get("remaining_time_ms"), "root D5 oracle remaining time")

    gates = _mapping(payload.get("gates"), "root D5 oracle gates")
    for key in (
        "initial_root_enumeration_python_parity",
        "persistent_d1_d2_python_parity",
        "persistent_d1_through_d5_selects_same_result_as_fresh_d5",
        "exact_selected_replay",
        "work_receipts",
        "deadline_receipts",
        "complete_rival_bound_coverage",
    ):
        _true(gates, key, "root D5 oracle")

    semantic = {
        "schema": ROOT_PARITY_SCHEMA,
        "artifact": {
            key: artifact[key]
            for key in (
                ARTIFACT_IDENTITY_FIELDS
                + RUNTIME_IDENTITY_FIELDS
                + tuple(oracle_subject_extras)
            )
        },
        "boundary": boundary,
        "session_config": config,
        "memory": build.memory,
        "deadline": {"deadline_limit_ms": deadline_limit_ms},
        "retained_manifest_sha256": retained_manifest_sha256,
        "selected": selected,
        "rival_bounds": rivals,
    }
    oracle_signature = payload.get("oracle_signature_sha256")
    if oracle_signature != _canonical_sha256(semantic):
        raise ReleaseGateError("root D5 oracle semantic signature is invalid")
    return (
        differential_cases,
        config,
        selected,
        [dict(_mapping(item, "root D5 oracle candidate bound")) for item in bounds],
        str(retained_manifest_sha256),
        str(oracle_signature),
    )


def _validate_prefix_parity(receipt: Receipt, build: BuildEvidence) -> int:
    payload = receipt.payload
    if payload.get("schema") != PREFIX_PARITY_SCHEMA or payload.get("status") != "passed":
        raise ReleaseGateError("prefix parity receipt did not pass")
    _require_identity(payload.get("artifact"), build.identity, "prefix parity")
    if payload.get("failures") != 0:
        raise ReleaseGateError("prefix parity receipt reports failures")
    cases = _list(payload.get("cases"), "prefix parity cases")
    if len(cases) < bundle_builder.MIN_PREFIX_DIFFERENTIAL_CASES:
        raise ReleaseGateError("prefix parity receipt has too few cases")
    names: set[str] = set()
    input_hashes: set[str] = set()
    for raw_case in cases:
        case = _mapping(raw_case, "prefix parity case")
        name = _text(case.get("name"), "prefix parity case name")
        if name in names:
            raise ReleaseGateError("prefix parity receipt duplicates a case")
        names.add(name)
        for key in ("input_sha256", "wasm_output_sha256", "oracle_output_sha256"):
            value = case.get(key)
            if not isinstance(value, str) or not HEX_64.fullmatch(value):
                raise ReleaseGateError(f"prefix parity case {name!r} has an invalid {key}")
        if (
            case.get("exact_match") is not True
            or case.get("wasm_output_sha256") != case.get("oracle_output_sha256")
        ):
            raise ReleaseGateError(f"prefix parity case {name!r} is not an exact match")
        input_hash = str(case["input_sha256"])
        if input_hash in input_hashes:
            raise ReleaseGateError("prefix parity receipt duplicates a case input")
        input_hashes.add(input_hash)
    if payload.get("case_set_sha256") != _canonical_sha256(cases):
        raise ReleaseGateError("prefix parity case-set digest is invalid")
    gates = _mapping(payload.get("gates"), "prefix parity gates")
    for key in (
        "exact_python_parity",
        "compiled_prefix_replay",
        "multi_ep_san",
        "illegal_prefix_fail_closed",
        "case_input_output_hashes",
    ):
        _true(gates, key, "prefix parity")
    if (
        payload.get("progressive_san_corrections") != 0
        or _integer(payload.get("progressive_san_exact_parity"), "prefix SAN parity", 1) < 2
        or _integer(payload.get("fail_closed_errors"), "prefix fail-closed cases", 1) < 3
        or payload.get("mate_replay") != "checkmate"
        or payload.get("multi_ep") != "covered"
    ):
        raise ReleaseGateError("prefix parity receipt lacks SAN, mate, or multi-EP proof")
    return len(cases)


def _validate_browser_prefix(receipt: Receipt, build: BuildEvidence) -> None:
    payload = receipt.payload
    if payload.get("schema") != BROWSER_PREFIX_SCHEMA or payload.get("status") != "passed":
        raise ReleaseGateError("browser prefix receipt did not pass")
    _require_identity(payload.get("artifact"), build.identity, "browser prefix")
    for key in (
        "exact_identity",
        "chess960_rejected",
        "certified_limits_enforced",
        "full_next_state_enforced",
        "same_series_terminal_covered",
        "final_frame_consistency_enforced",
        "malformed_local_fallback",
        "original_request_preserved",
        "remote_authority_bound",
        "cancellation_fallback_suppressed",
    ):
        _true(payload, key, "browser prefix")


def _validate_mate_parity(receipt: Receipt, build: BuildEvidence) -> int:
    payload = receipt.payload
    if payload.get("schema") != MATE_PARITY_SCHEMA or payload.get("status") != "passed":
        raise ReleaseGateError("mate parity receipt did not pass")
    _require_identity(payload.get("artifact"), build.identity, "mate parity")
    if payload.get("failures") != 0:
        raise ReleaseGateError("mate parity receipt reports failures")
    cases = _list(payload.get("cases"), "mate parity cases")
    if len(cases) < bundle_builder.MIN_MATE_DIFFERENTIAL_CASES:
        raise ReleaseGateError("mate parity receipt has too few cases")
    names: set[str] = set()
    input_hashes: set[str] = set()
    proof_counts = {"found": 0, "exhausted": 0, "unknown": 0}
    found_sides: set[str] = set()
    for raw_case in cases:
        case = _mapping(raw_case, "mate parity case")
        name = _text(case.get("name"), "mate parity case name")
        if name in names:
            raise ReleaseGateError("mate parity receipt duplicates a case")
        names.add(name)
        for key in ("input_sha256", "wasm_output_sha256", "oracle_output_sha256"):
            value = case.get(key)
            if not isinstance(value, str) or not HEX_64.fullmatch(value):
                raise ReleaseGateError(f"mate parity case {name!r} has an invalid {key}")
        if (
            case.get("exact_match") is not True
            or case.get("wasm_output_sha256") != case.get("oracle_output_sha256")
        ):
            raise ReleaseGateError(f"mate parity case {name!r} is not an exact match")
        input_hash = str(case["input_sha256"])
        if input_hash in input_hashes:
            raise ReleaseGateError("mate parity receipt duplicates a case input")
        input_hashes.add(input_hash)
        proof_status = case.get("proof_status")
        if proof_status not in proof_counts:
            raise ReleaseGateError(f"mate parity case {name!r} has an invalid proof status")
        proof_counts[str(proof_status)] += 1
        side = case.get("side_to_move")
        if side not in {"white", "black"}:
            raise ReleaseGateError(f"mate parity case {name!r} has an invalid side")
        if proof_status == "found":
            found_sides.add(str(side))
    if payload.get("case_set_sha256") != _canonical_sha256(cases):
        raise ReleaseGateError("mate parity case-set digest is invalid")
    if (
        proof_counts["found"] < 2
        or proof_counts["exhausted"] < 1
        or proof_counts["unknown"] < 2
        or found_sides != {"white", "black"}
    ):
        raise ReleaseGateError("mate parity proof-state accounting is incomplete")
    accelerated = _list(
        payload.get("accelerated_cases"),
        "accelerated mate cases",
    )
    expected_accelerated = {
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
            "work": 48_733,
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
    accelerated_names: set[str] = set()
    for raw_case in accelerated:
        case = _mapping(raw_case, "accelerated mate case")
        name = _text(case.get("name"), "accelerated mate case name")
        if name in accelerated_names:
            raise ReleaseGateError("accelerated mate receipt duplicates a case")
        accelerated_names.add(name)
        expected = expected_accelerated.get(name)
        if expected is None:
            raise ReleaseGateError("accelerated mate receipt has an unknown case")
        for key in ("input_sha256", "wasm_output_sha256"):
            value = case.get(key)
            if not isinstance(value, str) or not HEX_64.fullmatch(value):
                raise ReleaseGateError(
                    f"accelerated mate case {name!r} has an invalid {key}"
                )
        if (
            case.get("kernel_status") != expected["kernel_status"]
            or case.get("proof_status") != expected["proof_status"]
            or case.get("complete") is not expected["complete"]
            or _list(case.get("moves"), f"accelerated mate case {name!r} moves")
                != expected["moves"]
            or _integer(case.get("work"), f"accelerated mate case {name!r} work")
                != expected["work"]
            or _integer(
                case.get("checkmates"),
                f"accelerated mate case {name!r} checkmates",
            ) != expected["checkmates"]
            or _integer(
                case.get("max_depth_reached"),
                f"accelerated mate case {name!r} max depth",
            ) != expected["max_depth_reached"]
        ):
            raise ReleaseGateError(
                f"accelerated mate case {name!r} result changed"
            )
    if accelerated_names != set(expected_accelerated):
        raise ReleaseGateError("accelerated mate receipt lacks required cases")
    if payload.get("accelerated_case_set_sha256") != _canonical_sha256(accelerated):
        raise ReleaseGateError("accelerated mate case-set digest is invalid")
    gates = _mapping(payload.get("gates"), "mate parity gates")
    for key in (
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
        "prefix_replay",
        "case_input_output_hashes",
        "late_series_staged_root",
    ):
        _true(gates, key, "mate parity")
    return len(cases)


def _query_value(query: Mapping[str, list[str]], key: str) -> str:
    values = query.get(key)
    if values is None or len(values) != 1:
        raise ReleaseGateError(f"Opera benchmark URL must bind one {key!r} value")
    return values[0]


def _normalize_opera_bounds(
    value: object,
    *,
    label: str,
    expected_candidate_ids: set[str],
    selected: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_bounds = _list(value, f"{label} rival bounds")
    if len(raw_bounds) != 20:
        raise ReleaseGateError(f"{label} must retain exactly 20 candidate bounds")
    bounds: list[dict[str, Any]] = []
    for raw_bound in raw_bounds:
        bound = _mapping(raw_bound, f"{label} candidate bound")
        candidate_identity = _text(
            bound.get("candidate_identity"),
            f"{label} candidate identity",
        )
        bound_type = bound.get("bound")
        if bound_type not in {"exact", "lower", "upper"}:
            raise ReleaseGateError(f"{label} contains an invalid or Unknown candidate bound")
        score = _integer(bound.get("score"), f"{label} candidate score", -2_000_000_000)
        proof_bounds = _list(bound.get("proof_bounds"), f"{label} candidate proof bounds")
        if len(proof_bounds) != 2 or any(
            isinstance(item, bool) or not isinstance(item, int) for item in proof_bounds
        ):
            raise ReleaseGateError(f"{label} contains invalid candidate proof bounds")
        bounds.append(
            {
                "candidate_identity": candidate_identity,
                "bound": bound_type,
                "score": score,
                "proof_bounds": proof_bounds,
            }
        )
    bounds.sort(key=lambda item: item["candidate_identity"])
    candidate_ids = [item["candidate_identity"] for item in bounds]
    if len(set(candidate_ids)) != 20 or set(candidate_ids) != expected_candidate_ids:
        raise ReleaseGateError(f"{label} does not cover the oracle candidate universe exactly")

    selected_identity = selected.get("candidate_identity")
    selected_bounds = [
        item for item in bounds if item["candidate_identity"] == selected_identity
    ]
    if len(selected_bounds) != 1:
        raise ReleaseGateError(f"{label} does not cover the selected candidate exactly once")
    selected_bound = selected_bounds[0]
    if (
        selected_bound["bound"] != "exact"
        or selected_bound["score"] != selected.get("score")
        or selected_bound["proof_bounds"] != selected.get("proof_bounds")
    ):
        raise ReleaseGateError(f"{label} does not exactly certify the selected candidate")
    for bound in bounds:
        if bound["candidate_identity"] == selected_identity:
            continue
        if bound["bound"] == "lower" or bound["score"] > selected.get("score"):
            raise ReleaseGateError(f"{label} contains a rival bound that does not prove the selection")
    return bounds


def _validate_opera_run_binding(
    value: object,
    *,
    label: str,
    selected: Mapping[str, Any],
    expected_candidate_ids: set[str],
) -> tuple[list[dict[str, Any]], str, str]:
    run = _mapping(value, label)
    selected_signature = _canonical_sha256(selected)
    if (
        run.get("status") != "complete"
        or run.get("selected_signature_sha256") != selected_signature
        or run.get("selected_candidate_identity") != selected.get("candidate_identity")
        or run.get("unknown_or_limit_count") != 0
        or run.get("selected_owner_certification_count") != 1
    ):
        raise ReleaseGateError(f"{label} did not reproduce the selected oracle result")
    _number(run.get("elapsed_ms"), f"{label} elapsed", 0.000001)
    retained_manifest = run.get("retained_manifest_sha256")
    if not isinstance(retained_manifest, str) or not HEX_64.fullmatch(retained_manifest):
        raise ReleaseGateError(f"{label} has an invalid retained-manifest digest")
    bounds = _normalize_opera_bounds(
        run.get("rival_bounds"),
        label=label,
        expected_candidate_ids=expected_candidate_ids,
        selected=selected,
    )
    coverage_sha256 = _canonical_sha256(bounds)
    if run.get("root_coverage_sha256") != coverage_sha256:
        raise ReleaseGateError(f"{label} has an invalid root-coverage digest")
    semantic = {
        "selected": dict(selected),
        "retained_manifest_sha256": retained_manifest,
        "rival_bounds": bounds,
    }
    run_signature = _canonical_sha256(semantic)
    if run.get("run_signature_sha256") != run_signature:
        raise ReleaseGateError(f"{label} has an invalid actual-run signature")
    return bounds, retained_manifest, run_signature


def _validate_opera(
    receipt: Receipt,
    build: BuildEvidence,
    *,
    expected_config: Mapping[str, Any],
    oracle_selected: Mapping[str, Any],
    oracle_rival_bounds: list[dict[str, Any]],
    oracle_retained_manifest_sha256: str,
    oracle_signature_sha256: str,
) -> tuple[dict[str, Any], float, dict[str, Any], dict[str, Any], int]:
    payload = receipt.payload
    if payload.get("schema") != OPERA_CDP_SCHEMA or payload.get("status") != "passed-not-certified":
        raise ReleaseGateError("Opera CDP receipt did not pass")
    _false(payload, "product_publishable", "Opera CDP")
    _false(payload, "safety_certified", "Opera CDP")
    cdp = _mapping(payload.get("cdp"), "Opera CDP identity")
    browser = _text(cdp.get("browser"), "Opera CDP browser")
    user_agent = _text(cdp.get("user_agent"), "Opera CDP user agent")
    if not browser.startswith("Chrome/") or " OPR/" not in user_agent:
        raise ReleaseGateError("CDP receipt is not from an Opera runtime")
    _text(cdp.get("protocol_version"), "Opera CDP protocol version")
    _true(cdp, "web_socket_debugger_url_recorded", "Opera CDP")
    page = _mapping(payload.get("page_environment"), "Opera page environment")
    if page.get("userAgent") != user_agent or page.get("crossOriginIsolated") is not False:
        raise ReleaseGateError("Opera page runtime identity drifted from CDP")
    _integer(page.get("hardwareConcurrency"), "Opera hardware concurrency", 8)
    parsed_url = urlparse(_text(page.get("location"), "Opera benchmark URL"))
    if parsed_url.hostname != "127.0.0.1" or not parsed_url.path.endswith("/benchmarks/opera_root_d5_probe.html"):
        raise ReleaseGateError("Opera benchmark was not captured from the local D5 harness")
    query = parse_qs(parsed_url.query, keep_blank_values=True)

    worker = _mapping(payload.get("worker_receipt"), "Opera Worker receipt")
    if worker.get("schema") != OPERA_WORKER_SCHEMA or worker.get("status") != "passed-not-certified":
        raise ReleaseGateError("Opera Worker D1-D5 receipt did not pass")
    _false(worker, "product_publishable", "Opera Worker")
    _true(worker, "safety_certified", "Opera Worker")
    artifact = _require_identity(worker.get("artifact"), build.identity, "Opera Worker")
    for key in RUNTIME_IDENTITY_FIELDS:
        if artifact.get(key) != build.runtime_identity[key]:
            raise ReleaseGateError(f"Opera Worker runtime identity {key!r} drifted")

    geometry = dict(_mapping(worker.get("geometry"), "Opera geometry"))
    if (
        geometry.get("workers") != 8
        or geometry.get("initial_full_wave") != 4
        or geometry.get("depth") != 5
        or geometry.get("width") != 32
    ):
        raise ReleaseGateError("Opera release geometry must be exactly 8 Workers, wave 4, W32 D5")
    max_work = _integer(geometry.get("max_work"), "Opera maximum work", 1_000)
    safety_reserve = _integer(
        geometry.get("safety_reserve_work"),
        "Opera safety reserve work",
        1,
    )
    if safety_reserve > max_work:
        raise ReleaseGateError("Opera safety reserve exceeds the global work cap")
    expected_query = {
        "depth": "5",
        "width": "32",
        "workers": "8",
        "wave": "4",
        "max_work": str(max_work),
        "safety_work": str(safety_reserve),
    }
    for key, expected in expected_query.items():
        if _query_value(query, key) != expected:
            raise ReleaseGateError(f"Opera benchmark URL {key!r} does not match its receipt")
    if (
        PurePosixPath(urlparse(_query_value(query, "module")).path).name
        != build.module_js.name
        or PurePosixPath(urlparse(_query_value(query, "wasm")).path).name
        != build.wasm.name
        or PurePosixPath(urlparse(_query_value(query, "receipt")).path).name
        != build.receipt.path.name
    ):
        raise ReleaseGateError("Opera benchmark URL does not name the verified build bytes")
    config = dict(_mapping(geometry.get("config"), "Opera root config"))
    if (
        config.get("max_depth") != 5
        or config.get("width") != 32
        or config.get("max_work") != max_work
        or config.get("worker_threads") != 1
    ):
        raise ReleaseGateError("Opera root config differs from its W32 D5 geometry")
    if config != expected_config:
        raise ReleaseGateError("Opera root config differs from the signed D5 oracle")
    session_geometry = _mapping(
        build.receipt.payload.get("session_geometry"),
        "build session geometry",
    )
    expected_build_geometry = {
        "series_cache_capacity": "desktop_series_cache_capacity",
        "root_contract_tt_capacity": "root_contract_tt_capacity",
        "root_contract_eval_capacity": "root_contract_eval_capacity",
    }
    for config_key, build_key in expected_build_geometry.items():
        if config.get(config_key) != session_geometry.get(build_key):
            raise ReleaseGateError(f"Opera config {config_key!r} drifted from the build")

    timings = _mapping(worker.get("timings_ms"), "Opera timings")
    pool_ms = _number(timings.get("pool_ready"), "Opera pool-ready time")
    iterative_ms = _number(
        timings.get("iterative_d1_through_d5"),
        "Opera D1-D5 time",
        0.000001,
    )
    total_ms = _number(
        timings.get("total_to_completed_depth"),
        "Opera total D5 time",
        0.000001,
    )
    final_iteration_ms = _number(
        timings.get("completed_depth_iteration"),
        "Opera final-depth time",
        0.000001,
    )
    if total_ms >= 60_000 or iterative_ms >= 60_000:
        raise ReleaseGateError("Opera W32 D1-D5 did not complete in under 60 seconds")
    try:
        timeout_value = float(_query_value(query, "timeout_ms"))
    except ValueError as error:
        raise ReleaseGateError("Opera benchmark timeout is not numeric") from error
    timeout_ms = _number(timeout_value, "Opera benchmark timeout", 0.000001)
    if timeout_ms < total_ms:
        raise ReleaseGateError("Opera elapsed time exceeds the benchmark timeout")
    if total_ms < iterative_ms or total_ms + 1e-6 < pool_ms:
        raise ReleaseGateError("Opera timing accounting is internally inconsistent")

    iterations = _list(worker.get("iterations"), "Opera iterations")
    if [item.get("depth") if isinstance(item, Mapping) else None for item in iterations] != [1, 2, 3, 4, 5]:
        raise ReleaseGateError("Opera receipt must contain exact persistent D1-D5 iterations")
    for index, raw_iteration in enumerate(iterations, start=1):
        iteration = _mapping(raw_iteration, f"Opera D{index} iteration")
        _number(iteration.get("elapsed_ms"), f"Opera D{index} elapsed", 0.000001)
        _text(iteration.get("candidate_identity"), f"Opera D{index} candidate identity")
        if not _list(iteration.get("principal_variation"), f"Opera D{index} principal variation"):
            raise ReleaseGateError(f"Opera D{index} did not retain a principal variation")
        if (
            iteration.get("coverage_complete") is not True
            or iteration.get("safety_status") not in {"exhausted", "terminal"}
            or iteration.get("owner_certification_count") != 1
        ):
            raise ReleaseGateError(f"Opera D{index} did not publish a safe exact owner result")
        replay = _mapping(iteration.get("final_replay"), f"Opera D{index} replay")
        if replay.get("complete") is not True or replay.get("next_state") is None:
            raise ReleaseGateError(f"Opera D{index} compiled replay did not complete")
        work = _mapping(iteration.get("work"), f"Opera D{index} work")
        if work.get("max_work") != max_work or work.get("within_cap") is not True:
            raise ReleaseGateError(f"Opera D{index} exceeded or changed the global work cap")
        committed = _integer(work.get("committed_work"), f"Opera D{index} committed work")
        reserved = _integer(work.get("reserved_work"), f"Opera D{index} reserved work")
        remaining = _integer(work.get("remaining_work"), f"Opera D{index} remaining work")
        if reserved != 0 or committed + remaining != max_work:
            raise ReleaseGateError(f"Opera D{index} work ledger does not settle exactly")
        if work.get("safety_reserve_work") != safety_reserve:
            raise ReleaseGateError(f"Opera D{index} did not use the certified safety reserve")
        safety_committed = _integer(
            work.get("safety_committed_work"),
            f"Opera D{index} safety work",
        )
        if safety_committed > safety_reserve:
            raise ReleaseGateError(f"Opera D{index} safety work exceeded its reserve")
    final_iteration = _mapping(iterations[-1], "Opera D5 iteration")
    if abs(_number(final_iteration.get("elapsed_ms"), "Opera D5 elapsed") - final_iteration_ms) > 1e-6:
        raise ReleaseGateError("Opera final-depth timing differs from the D5 iteration")

    result = dict(_mapping(worker.get("result"), "Opera result"))
    if (
        result.get("completed_depth") != 5
        or result.get("coverage_complete") is not True
        or result.get("safety_status") not in {"exhausted", "terminal"}
        or result.get("move") != final_iteration.get("move")
        or result.get("score") != final_iteration.get("score")
        or result.get("proof_bounds") != final_iteration.get("proof_bounds")
        or result.get("candidate_identity") != final_iteration.get("candidate_identity")
        or result.get("principal_variation") != final_iteration.get("principal_variation")
    ):
        raise ReleaseGateError("Opera final result is not the completed safe D5 iteration")
    for key in (
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
    ):
        if result.get(key) != final_iteration.get(key):
            raise ReleaseGateError(f"Opera final result {key!r} differs from its D5 iteration")
    expected_result_fields = {
        "candidate_identity": oracle_selected.get("candidate_identity"),
        "move": oracle_selected.get("move"),
        "score": oracle_selected.get("score"),
        "proof_bounds": oracle_selected.get("proof_bounds"),
        "principal_variation": oracle_selected.get("principal_variation"),
    }
    for key, expected in expected_result_fields.items():
        if result.get(key) != expected:
            raise ReleaseGateError(f"Opera warm D1-D5 result {key!r} differs from the oracle")
    expected_candidate_ids = {
        _text(item.get("candidate_identity"), "root D5 oracle candidate identity")
        for item in oracle_rival_bounds
    }
    if len(expected_candidate_ids) != 20:
        raise ReleaseGateError("root D5 oracle candidate universe is incomplete")
    warm_result_bounds = _normalize_opera_bounds(
        result.get("root_bounds"),
        label="Opera warm D1-D5 result",
        expected_candidate_ids=expected_candidate_ids,
        selected=oracle_selected,
    )
    warm_result_manifest = result.get("retained_manifest_sha256")
    warm_result_order_shape = result.get("order_shape_sha256")
    if (
        warm_result_bounds != oracle_rival_bounds
        or warm_result_manifest != oracle_retained_manifest_sha256
        or not isinstance(warm_result_order_shape, str)
        or not HEX_64.fullmatch(warm_result_order_shape)
    ):
        raise ReleaseGateError("Opera warm D1-D5 rival coverage differs from the signed oracle")

    oracle = _mapping(worker.get("oracle"), "Opera oracle binding")
    selected_signature = _canonical_sha256(oracle_selected)
    if (
        oracle.get("schema") != "spc-opera-root-d5-oracle-binding-v1"
        or oracle.get("oracle_signature_sha256") != oracle_signature_sha256
        or oracle.get("selected_signature_sha256") != selected_signature
        or oracle.get("cold_selected_matches_oracle") is not True
        or oracle.get("warm_full_matches_oracle") is not True
    ):
        raise ReleaseGateError("Opera receipt is not bound to the signed root D5 oracle")
    _validate_opera_run_binding(
        oracle.get("cold_d5"),
        label="Opera cold D5 oracle run",
        selected=oracle_selected,
        expected_candidate_ids=expected_candidate_ids,
    )
    warm_bounds, warm_manifest, warm_run_signature = _validate_opera_run_binding(
        oracle.get("warm_d1_through_d5"),
        label="Opera warm D1-D5 oracle run",
        selected=oracle_selected,
        expected_candidate_ids=expected_candidate_ids,
    )
    if warm_bounds != oracle_rival_bounds or warm_manifest != oracle_retained_manifest_sha256:
        raise ReleaseGateError("Opera warm oracle run does not carry the signed full coverage")
    schedule_trials = _list(worker.get("schedule_trials"), "Opera schedule trials")
    if len(schedule_trials) < 2:
        raise ReleaseGateError("Opera receipt needs at least two real schedule shapes")
    schedule_shapes: set[tuple[int, str]] = set()
    order_shapes: set[str] = set()
    saw_wave_four = False
    for raw_trial in schedule_trials:
        trial = _mapping(raw_trial, "Opera schedule trial")
        workers = _integer(trial.get("workers"), "Opera schedule trial workers", 1)
        wave = _integer(trial.get("initial_full_wave"), "Opera schedule trial wave", 1)
        order_shape = trial.get("order_shape_sha256")
        if workers != 8 or wave > workers:
            raise ReleaseGateError("Opera schedule trial used the wrong Worker geometry")
        if not isinstance(order_shape, str) or not HEX_64.fullmatch(order_shape):
            raise ReleaseGateError("Opera schedule trial has an invalid order-shape digest")
        trial_bounds, trial_manifest, trial_signature = _validate_opera_run_binding(
            trial,
            label=f"Opera wave-{wave} schedule trial",
            selected=oracle_selected,
            expected_candidate_ids=expected_candidate_ids,
        )
        trial_semantic = {
            "run_signature_sha256": trial_signature,
            "workers": workers,
            "initial_full_wave": wave,
            "order_shape_sha256": order_shape,
        }
        if trial.get("trial_signature_sha256") != _canonical_sha256(trial_semantic):
            raise ReleaseGateError("Opera schedule trial has an invalid schedule signature")
        if wave == 4 and (
            trial_bounds != oracle_rival_bounds
            or trial_manifest != oracle_retained_manifest_sha256
            or trial_signature != warm_run_signature
            or order_shape != warm_result_order_shape
        ):
            raise ReleaseGateError("Opera wave-4 schedule trial differs from the signed warm run")
        schedule_shapes.add((wave, order_shape))
        order_shapes.add(order_shape)
        saw_wave_four = saw_wave_four or wave == 4
    if len(schedule_shapes) < 2 or len(order_shapes) < 2 or not saw_wave_four:
        raise ReleaseGateError("Opera schedule trials do not prove two distinct real order shapes")

    memory = dict(_mapping(worker.get("memory"), "Opera memory"))
    maximum = int(build.memory["maximum_bytes"])
    aggregate_maximum = 8 * maximum
    if (
        memory.get("per_worker_hard_maximum_bytes") != maximum
        or memory.get("aggregate_hard_maximum_bytes") != aggregate_maximum
    ):
        raise ReleaseGateError("Opera memory envelope differs from the linked build")
    worker_memory = _list(memory.get("workers"), "Opera Worker memory")
    if len(worker_memory) != 8:
        raise ReleaseGateError("Opera memory receipt must cover all 8 Workers")
    memory_ids: set[str] = set()
    observed_sum = 0
    for item in worker_memory:
        entry = _mapping(item, "Opera Worker memory entry")
        worker_id = _text(entry.get("id"), "Opera Worker memory id")
        if worker_id in memory_ids:
            raise ReleaseGateError("Opera memory receipt duplicates a Worker")
        memory_ids.add(worker_id)
        peak = _integer(entry.get("peak_bytes"), f"Opera {worker_id} peak memory", 1)
        if peak > maximum:
            raise ReleaseGateError(f"Opera {worker_id} exceeded its hard memory maximum")
        _integer(entry.get("native_work_after"), f"Opera {worker_id} native work")
        observed_sum += peak
    if (
        memory.get("aggregate_observed_peak_bytes") != observed_sum
        or observed_sum > aggregate_maximum
    ):
        raise ReleaseGateError("Opera aggregate memory receipt is inconsistent")

    environment = _mapping(worker.get("environment"), "Opera Worker environment")
    if (
        environment.get("ordinary_module_workers") is not True
        or environment.get("worker_count") != 8
        or environment.get("worker_global_scope") != "DedicatedWorkerGlobalScope"
        or environment.get("cross_origin_isolated") is not False
    ):
        raise ReleaseGateError("Opera did not prove 8 ordinary dedicated module Workers")
    _integer(environment.get("hardware_concurrency"), "Opera Worker hardware concurrency", 8)
    workers = _list(environment.get("workers"), "Opera Worker identities")
    if len(workers) != 8:
        raise ReleaseGateError("Opera identity receipt must cover all 8 Workers")
    worker_ids: set[str] = set()
    certificate_ids: set[str] = set()
    worker_identity_fields = (
        "source_fingerprint",
        "kernel_sha256",
        "module_js_sha256",
    )
    expected_worker_identity = {
        **build.identity,
        **build.engine,
        "runtime_variant": "single",
        "thread_count": 1,
    }
    for raw_worker in workers:
        entry = _mapping(raw_worker, "Opera Worker identity entry")
        worker_id = _text(entry.get("worker_id"), "Opera Worker id")
        if worker_id in worker_ids:
            raise ReleaseGateError("Opera receipt duplicates a Worker identity")
        worker_ids.add(worker_id)
        identity = _mapping(entry.get("identity"), f"Opera {worker_id} identity")
        for key in worker_identity_fields + (
            "runtime_variant",
            "thread_count",
            "engine_version",
            "ruleset_version",
            "profile_id",
        ):
            if identity.get(key) != expected_worker_identity[key]:
                raise ReleaseGateError(f"Opera {worker_id} identity {key!r} drifted")
        certificate_ids.add(_text(identity.get("certificate_id"), f"Opera {worker_id} certificate id"))
        worker_artifact = _mapping(entry.get("artifact"), f"Opera {worker_id} artifact")
        for key in ARTIFACT_IDENTITY_FIELDS:
            if worker_artifact.get(key) != build.identity[key]:
                raise ReleaseGateError(f"Opera {worker_id} artifact {key!r} drifted")
        for key in RUNTIME_IDENTITY_FIELDS:
            if worker_artifact.get(key) != build.runtime_identity[key]:
                raise ReleaseGateError(f"Opera {worker_id} runtime {key!r} drifted")
        if (
            entry.get("ordinary_module_worker") is not True
            or entry.get("worker_global_scope") != "DedicatedWorkerGlobalScope"
        ):
            raise ReleaseGateError(f"Opera {worker_id} is not an ordinary module Worker")
    if len(certificate_ids) != 1 or worker_ids != memory_ids:
        raise ReleaseGateError("Opera Worker identity and memory membership differ")

    gates = _mapping(worker.get("gates"), "Opera gates")
    for key in (
        "exact_artifact_identity_all_workers",
        "ordinary_module_workers",
        "pthreads_disabled",
        "combined_prefix_root_mate_abi",
        "persistent_d1_through_d5_sessions",
        "exact_manifest_import_all_workers",
        "global_work_cap_enforced",
        "common_monotonic_deadline",
        "dynamic_work_pool_certified",
        "final_bound_coverage",
        "selected_owner_warm_exact_certification",
        "compiled_root_prefix_replay",
        "compiled_reply_mate_safety",
        "memory_envelope_observed",
        "d5_w32_anchor",
        "under_60_seconds_total",
        "cold_d5_selected_matches_oracle",
        "warm_d1_d5_full_matches_oracle",
        "alternate_schedule_selected_matches_oracle",
        "multiple_seed_wave_order_shapes",
        "no_unknown_or_limit_results",
    ):
        _true(gates, key, "Opera")
    if gates.get("release_certificate_present") is not False:
        raise ReleaseGateError("Opera benchmark must precede release certification")
    return config, total_ms / 1000.0, result, memory, safety_reserve


def validate_evidence(
    *,
    repository: Path,
    source_package: Path,
    receipt_paths: Mapping[str, Path],
) -> ValidatedEvidence:
    required = set(RECEIPT_FILENAMES)
    if set(receipt_paths) != required:
        raise ReleaseGateError("release evidence must provide exactly all seven receipt types")
    receipts = {
        label: _load_receipt(label, receipt_paths[label])
        for label in RECEIPT_FILENAMES
    }
    build = _validate_build_receipt(
        receipts["build"],
        repository=repository,
        source_package=source_package,
    )
    root_contract, prefix_contract = _validate_root_smoke(receipts["root_smoke"], build)
    (
        root_cases,
        root_config,
        canonical_d5,
        oracle_rival_bounds,
        oracle_retained_manifest,
        oracle_signature,
    ) = _validate_root_parity(
        receipts["root_parity"],
        build,
        root_contract,
    )
    root_config, elapsed, opera_result, opera_memory, safety_reserve = _validate_opera(
        receipts["opera"],
        build,
        expected_config=root_config,
        oracle_selected=canonical_d5,
        oracle_rival_bounds=oracle_rival_bounds,
        oracle_retained_manifest_sha256=oracle_retained_manifest,
        oracle_signature_sha256=oracle_signature,
    )
    prefix_cases = _validate_prefix_parity(receipts["prefix_parity"], build)
    _validate_browser_prefix(receipts["browser_prefix"], build)
    mate_cases = _validate_mate_parity(receipts["mate_parity"], build)
    return ValidatedEvidence(
        build=build,
        receipts=receipts,
        root_contract=root_contract,
        prefix_contract=prefix_contract,
        oracle_signature_sha256=oracle_signature,
        root_config=root_config,
        root_differential_cases=root_cases,
        prefix_differential_cases=prefix_cases,
        mate_differential_cases=mate_cases,
        opera_elapsed_seconds=elapsed,
        opera_result=opera_result,
        opera_memory=opera_memory,
        safety_reserve_positions=safety_reserve,
    )


def _certificate_id(capability: str, evidence: ValidatedEvidence) -> str:
    seed = {
        "capability": capability,
        "identity": evidence.build.identity,
        "receipts": {
            label: receipt.sha256
            for label, receipt in sorted(evidence.receipts.items())
        },
    }
    return f"spc-{capability}-{_canonical_sha256(seed)[:16]}"


def _common_certificate(evidence: ValidatedEvidence) -> dict[str, Any]:
    build = evidence.build
    return {
        "status": "certified",
        "contract_version": 1,
        "source_fingerprint": build.source_fingerprint,
        "wasm_sha256": build.identity["wasm_sha256"],
        "module_js_sha256": build.identity["module_js_sha256"],
        "runtime_variant": "single",
        "thread_count": 1,
        "support_files": [],
        "memory": dict(build.memory),
    }


def build_certificates(
    evidence: ValidatedEvidence,
    *,
    maximum_seconds: float,
    default_seconds: float,
) -> dict[str, dict[str, Any]]:
    if (
        not math.isfinite(maximum_seconds)
        or not math.isfinite(default_seconds)
        or not 0 < default_seconds <= maximum_seconds <= 60
    ):
        raise ReleaseGateError("release play seconds must satisfy 0 < default <= maximum <= 60")
    if default_seconds + 1e-9 < evidence.opera_elapsed_seconds:
        raise ReleaseGateError("default play time is shorter than the proven Opera D1-D5 run")
    build = evidence.build
    common = _common_certificate(evidence)
    combined = {
        **common,
        "product_publishable": False,
        "kernel_sha256": build.identity["kernel_sha256"],
        "exports": list(bundle_builder.COMBINED_EXPORTS),
        **build.runtime_identity,
        "runtime_requirements": dict(build.runtime_requirements),
        "engine": dict(build.engine),
    }
    prefix = {
        **common,
        "certificate_id": _certificate_id("prefix", evidence),
        "evidence": {
            "failures": 0,
            "compiled_prefix_replay": True,
            "multi_ep_san": True,
            "illegal_prefix_fail_closed": True,
            "differential_cases": evidence.prefix_differential_cases,
        },
        "engine": {
            "engine_version": build.engine["engine_version"],
            "ruleset_version": build.engine["ruleset_version"],
        },
        "prefix_contract": dict(evidence.prefix_contract),
    }
    root = {
        **combined,
        "schema": bundle_builder.ROOT_SESSION_CERTIFICATE_SCHEMA,
        "certificate_id": _certificate_id("root-session", evidence),
        "abi_version": 2,
        "root_session_certified": True,
        "reply_mate_safety": False,
        "root_session_contract": dict(evidence.root_contract),
        "geometry": {
            "desktop_workers": 8,
            "desktop_initial_full_wave": 4,
            "aggregate_maximum_bytes": 8 * int(build.memory["maximum_bytes"]),
            "supported_lower_geometries": [],
            "session_config": dict(evidence.root_config),
            "play_limits": {
                "maximum_seconds": maximum_seconds,
                "default_seconds": default_seconds,
                "default_generation_positions": evidence.root_config["max_work"],
                "safety_reserve_positions": evidence.safety_reserve_positions,
            },
        },
        "evidence": {
            "failures": 0,
            "differential_cases": evidence.root_differential_cases,
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
            "canonical_root_tactical_policy": True,
            "start_w32_d5_completed_depth": 5,
            "start_w32_d5_width": 32,
            "start_w32_d5_elapsed_seconds": evidence.opera_elapsed_seconds,
        },
    }
    mate = {
        **combined,
        "schema": bundle_builder.MATE_CERTIFICATE_SCHEMA,
        "certificate_id": _certificate_id("mate", evidence),
        "abi_version": 1,
        "mate_capability_certified": True,
        "reply_mate_safety": True,
        "evidence": {
            "failures": 0,
            "differential_cases": evidence.mate_differential_cases,
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
    bundle_builder.validate_prefix_certificate(
        prefix,
        source_fingerprint=build.source_fingerprint,
        wasm_sha256=build.identity["wasm_sha256"],
        module_js_sha256=build.identity["module_js_sha256"],
        runtime_variant="single",
        thread_count=1,
        support_files=[],
    )
    bundle_builder.validate_root_session_certificate(
        root,
        source_fingerprint=build.source_fingerprint,
        wasm_sha256=build.identity["wasm_sha256"],
        module_js_sha256=build.identity["module_js_sha256"],
        runtime_variant="single",
        thread_count=1,
        support_files=[],
    )
    bundle_builder.validate_mate_certificate(
        mate,
        source_fingerprint=build.source_fingerprint,
        wasm_sha256=build.identity["wasm_sha256"],
        module_js_sha256=build.identity["module_js_sha256"],
        runtime_variant="single",
        thread_count=1,
        support_files=[],
    )
    return {"prefix": prefix, "root_session": root, "mate": mate}


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _directory_records(directory: Path) -> tuple[list[dict[str, object]], str]:
    records = [
        {
            "path": path.relative_to(directory).as_posix(),
            "sha256": _sha256_file(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(
            (path for path in directory.rglob("*") if path.is_file()),
            key=lambda item: item.relative_to(directory).as_posix(),
        )
    ]
    return records, _canonical_sha256(records)


def promote_release(
    evidence: ValidatedEvidence,
    certificates: Mapping[str, Mapping[str, Any]],
    *,
    source_package: Path,
    output: Path,
    authorized_by: str,
    maximum_seconds: float,
    default_seconds: float,
) -> Mapping[str, Any]:
    authorized_by = _text(authorized_by, "promotion authorizer")
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"release output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}.", dir=output.parent) as name:
        staging = Path(name) / "release"
        staging.mkdir()
        certificate_directory = staging / "certificates"
        evidence_directory = staging / "evidence"
        certificate_directory.mkdir()
        evidence_directory.mkdir()
        certificate_paths: dict[str, Path] = {}
        for label, certificate in certificates.items():
            path = certificate_directory / f"{label.replace('_', '-')}-certificate.json"
            _write_json(path, certificate)
            certificate_paths[label] = path
        receipt_records = []
        for label, filename in RECEIPT_FILENAMES.items():
            receipt = evidence.receipts[label]
            destination = evidence_directory / filename
            destination.write_bytes(receipt.raw)
            receipt_records.append(
                {
                    "label": label,
                    "path": destination.relative_to(staging).as_posix(),
                    "schema": receipt.payload.get("schema"),
                    "sha256": receipt.sha256,
                    "bytes": len(receipt.raw),
                }
            )
        bundle_directory = staging / "browser-engine"
        bundle_builder.build_bundle(
            single_wasm=evidence.build.wasm,
            single_module_js=evidence.build.module_js,
            single_prefix_certificate_path=certificate_paths["prefix"],
            single_root_session_certificate_path=certificate_paths["root_session"],
            single_mate_certificate_path=certificate_paths["mate"],
            source_package=source_package.resolve(),
            output=bundle_directory,
        )
        bundle_builder.validate_existing_bundle(bundle_directory, source_package.resolve())
        bundle_records, bundle_set_sha256 = _directory_records(bundle_directory)
        certificate_records, certificate_set_sha256 = _directory_records(certificate_directory)
        release_seed = {
            "artifact": evidence.build.identity,
            "bundle_set_sha256": bundle_set_sha256,
            "certificate_set_sha256": certificate_set_sha256,
            "receipts": [
                {key: item[key] for key in ("label", "sha256")}
                for item in receipt_records
            ],
            "policy": {
                "maximum_seconds": maximum_seconds,
                "default_seconds": default_seconds,
            },
        }
        release_id = f"spc-browser-wasm-release-{_canonical_sha256(release_seed)[:16]}"
        release_receipt = {
            "schema": RELEASE_SCHEMA,
            "status": "promoted",
            "product_publishable": True,
            "release_id": release_id,
            "authorization": {
                "authorized_by": authorized_by,
                "transition": "verified-combined-wasm-to-pages-ready",
                "mechanism": "explicit-command-line",
            },
            "source_revision": evidence.build.identity["source_revision"],
            "artifact": {
                **evidence.build.identity,
                **evidence.build.runtime_identity,
                "runtime_variant": "single",
                "thread_count": 1,
            },
            "toolchain": dict(evidence.build.toolchain),
            "build_command_sha256": _canonical_sha256(
                evidence.build.receipt.payload["command"]
            ),
            "dependency_closure": evidence.build.dependency_closure,
            "root_tactical_policy": {
                "capability": True,
                "policy": "canonical-boundary-policy-v1",
                "legacy_wire_root_tactical_protection": False,
            },
            "certificates": {
                label: {
                    "certificate_id": certificate["certificate_id"],
                    "path": certificate_paths[label].relative_to(staging).as_posix(),
                    "sha256": _sha256_file(certificate_paths[label]),
                }
                for label, certificate in certificates.items()
            },
            "evidence_receipts": receipt_records,
            "browser_bundle": {
                "path": "browser-engine",
                "files": bundle_records,
                "artifact_set_sha256": bundle_set_sha256,
            },
            "certificate_set_sha256": certificate_set_sha256,
            "promotion_policy": {
                "maximum_seconds": maximum_seconds,
                "default_seconds": default_seconds,
                "default_generation_positions": evidence.root_config["max_work"],
                "safety_reserve_positions": evidence.safety_reserve_positions,
            },
            "measured": {
                "root_d5_oracle_signature_sha256": evidence.oracle_signature_sha256,
                "opera_total_d1_through_d5_seconds": evidence.opera_elapsed_seconds,
                "completed_depth": 5,
                "width": 32,
                "workers": 8,
                "initial_full_wave": 4,
                "result": evidence.opera_result,
                "memory": evidence.opera_memory,
            },
            "gates": {
                "exact_source_and_artifact_identity": True,
                "clean_tracked_dependency_closure": True,
                "root_python_parity": True,
                "persistent_matches_fresh_d5": True,
                "multiple_opera_schedule_shapes": True,
                "prefix_python_and_browser_parity": True,
                "mate_python_and_proof_parity": True,
                "combined_root_prefix_mate_abi": True,
                "canonical_root_tactical_boundary_policy": True,
                "opera_ordinary_worker_proof": True,
                "memory_envelope_observed": True,
                "w32_d1_through_d5_under_60_seconds": True,
                "existing_bundle_revalidated": True,
                "immutable_copy_by_digest": True,
            },
        }
        _write_json(staging / "release-receipt.json", release_receipt)
        staging.replace(output)
    return release_receipt


def _receipt_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--build-receipt", type=Path, required=True)
    parser.add_argument("--root-smoke-receipt", type=Path, required=True)
    parser.add_argument("--root-parity-receipt", type=Path, required=True)
    parser.add_argument("--prefix-parity-receipt", type=Path, required=True)
    parser.add_argument("--browser-prefix-receipt", type=Path, required=True)
    parser.add_argument("--mate-parity-receipt", type=Path, required=True)
    parser.add_argument("--opera-receipt", type=Path, required=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed promotion of one identity-bound combined root/prefix/mate "
            "WASM artifact after exact parity, memory, Opera Worker, and W32 D1-D5 gates."
        )
    )
    _receipt_arguments(parser)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument(
        "--source-package",
        type=Path,
        default=ROOT / "src" / "scottish_progressive",
    )
    parser.add_argument("--maximum-seconds", type=float, default=60.0)
    parser.add_argument("--default-seconds", type=float, default=60.0)
    parser.add_argument("--authorized-by")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    if args.check_only:
        if args.output is not None or args.authorized_by is not None:
            parser.error("--check-only cannot be combined with --output or --authorized-by")
    elif args.output is None or args.authorized_by is None:
        parser.error("promotion requires --output and --authorized-by")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    receipt_paths = {
        "build": args.build_receipt.resolve(),
        "root_smoke": args.root_smoke_receipt.resolve(),
        "root_parity": args.root_parity_receipt.resolve(),
        "prefix_parity": args.prefix_parity_receipt.resolve(),
        "browser_prefix": args.browser_prefix_receipt.resolve(),
        "mate_parity": args.mate_parity_receipt.resolve(),
        "opera": args.opera_receipt.resolve(),
    }
    try:
        evidence = validate_evidence(
            repository=args.repository.resolve(),
            source_package=args.source_package.resolve(),
            receipt_paths=receipt_paths,
        )
        certificates = build_certificates(
            evidence,
            maximum_seconds=args.maximum_seconds,
            default_seconds=args.default_seconds,
        )
        if args.check_only:
            result: Mapping[str, Any] = {
                "schema": RELEASE_SCHEMA,
                "status": "validated-not-promoted",
                "product_publishable": False,
                "artifact": evidence.build.identity,
                "root_tactical_policy": {
                    "capability": True,
                    "policy": "canonical-boundary-policy-v1",
                    "legacy_wire_root_tactical_protection": False,
                },
                "receipt_sha256": {
                    label: receipt.sha256
                    for label, receipt in sorted(evidence.receipts.items())
                },
                "opera_total_d1_through_d5_seconds": evidence.opera_elapsed_seconds,
                "certificate_ids": {
                    label: certificate["certificate_id"]
                    for label, certificate in certificates.items()
                },
            }
        else:
            assert args.output is not None and args.authorized_by is not None
            result = promote_release(
                evidence,
                certificates,
                source_package=args.source_package.resolve(),
                output=args.output.resolve(),
                authorized_by=args.authorized_by,
                maximum_seconds=args.maximum_seconds,
                default_seconds=args.default_seconds,
            )
    except (FileNotFoundError, ReleaseGateError, ValueError) as error:
        print(f"release gate failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
