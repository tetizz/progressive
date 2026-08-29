from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "scottish_progressive"
PAGE_BYTES = 65_536
MIB = 1024 * 1024
MAX_INITIAL_MEMORY = 128 * MIB
MAX_ESTIMATED_PEAK_MEMORY = 192 * MIB
MAXIMUM_MEMORY = 256 * MIB

SOURCES = (
    "src/scottish_progressive/_native_eval.cpp",
    "src/scottish_progressive/native_eval.hpp",
    "src/scottish_progressive/native_subtree.cpp",
    "src/scottish_progressive/native_subtree.hpp",
    "src/scottish_progressive/native_subtree_wasm.cpp",
    "src/scottish_progressive/native_subtree_wasm.hpp",
    "src/scottish_progressive/native_subtree_wasm_support.hpp",
    "src/scottish_progressive/native_root_session_wasm.cpp",
    "src/scottish_progressive/native_root_session_wasm.hpp",
    "src/scottish_progressive/_native_mate.cpp",
)

EXPORTED_FUNCTIONS = (
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
    "_spc_single_reply_mate_ladder_search_json",
    "_spc_single_reply_mate_ladder_abi_version",
    "_malloc",
    "_free",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_records(
    paths: Iterable[Path],
    *,
    relative_to: Path = ROOT,
) -> tuple[list[dict[str, object]], str]:
    records: list[dict[str, object]] = []
    for path in sorted(
        paths,
        key=lambda item: item.relative_to(relative_to).as_posix(),
    ):
        records.append(
            {
                "path": path.relative_to(relative_to).as_posix(),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    encoded = json.dumps(
        records,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return records, hashlib.sha256(encoded).hexdigest()


def engine_source_fingerprint() -> str:
    digest = hashlib.sha256()
    paths = (
        path
        for pattern in ("*.py", "*.cpp", "*.hpp", "*.h")
        for path in PACKAGE.rglob(pattern)
    )
    for path in sorted(paths, key=lambda item: item.relative_to(PACKAGE).as_posix()):
        digest.update(path.relative_to(PACKAGE).as_posix().encode("utf-8"))
        digest.update(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    return digest.hexdigest()[:16]


def git_output(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def validate_page_aligned(name: str, value: int, maximum: int) -> None:
    if value <= 0 or value % PAGE_BYTES or value > maximum:
        raise ValueError(
            f"{name} must be a positive 64KiB-aligned value no larger than {maximum}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the ordinary-Worker Progressive root-session WASM bundle."
    )
    parser.add_argument("--em-plus-plus", required=True, type=Path)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=ROOT / "build" / "root-session-wasm",
    )
    parser.add_argument("--module-name", default="spc-root-session.mjs")
    parser.add_argument(
        "--exception-strategy",
        choices=("emscripten", "wasm"),
        default="emscripten",
    )
    parser.add_argument(
        "--wasm-simd",
        action="store_true",
        help="Compile the measured -msimd128 variant (off by default).",
    )
    parser.add_argument(
        "--allocator",
        choices=("dlmalloc", "emmalloc"),
        default="dlmalloc",
        help="Select the explicitly receipt-bound Emscripten allocator.",
    )
    parser.add_argument("--initial-memory-bytes", type=int, default=64 * MIB)
    parser.add_argument("--estimated-peak-memory-bytes", type=int, default=128 * MIB)
    parser.add_argument("--maximum-memory-bytes", type=int, default=128 * MIB)
    parser.add_argument("--stack-bytes", type=int, default=1 * MIB)
    parser.add_argument("--desktop-series-cache-capacity", type=int, default=65_536)
    parser.add_argument("--root-tt-capacity", type=int, default=262_144)
    parser.add_argument("--root-eval-capacity", type=int, default=262_144)
    parser.add_argument("--engine-version", default="spc-0.9.0")
    parser.add_argument("--ruleset-version", default="scottish-modern-common-v1")
    parser.add_argument("--profile-id", default="spc-68942034c41b4cc4")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    compiler = args.em_plus_plus.resolve()
    if not compiler.is_file():
        raise FileNotFoundError(f"Emscripten compiler is missing: {compiler}")
    if not args.module_name.endswith((".mjs", ".js")) or Path(args.module_name).name != args.module_name:
        raise ValueError("module-name must be a plain .mjs or .js file name")
    validate_page_aligned("initial memory", args.initial_memory_bytes, MAX_INITIAL_MEMORY)
    validate_page_aligned(
        "estimated peak memory",
        args.estimated_peak_memory_bytes,
        MAX_ESTIMATED_PEAK_MEMORY,
    )
    validate_page_aligned("maximum memory", args.maximum_memory_bytes, MAXIMUM_MEMORY)
    validate_page_aligned("stack", args.stack_bytes, args.initial_memory_bytes)
    if not (
        args.initial_memory_bytes
        <= args.estimated_peak_memory_bytes
        <= args.maximum_memory_bytes
    ):
        raise ValueError("memory envelope must satisfy initial <= estimated peak <= maximum")
    for name, value in (
        ("desktop series cache capacity", args.desktop_series_cache_capacity),
        ("root TT capacity", args.root_tt_capacity),
        ("root eval capacity", args.root_eval_capacity),
    ):
        if value <= 0 or value > 0xFFFFFFFF:
            raise ValueError(f"{name} must be from 1 through 2^32-1")
    for name, value in (
        ("engine version", args.engine_version),
        ("ruleset version", args.ruleset_version),
        ("profile id", args.profile_id),
    ):
        if not value or value != value.strip() or len(value.encode("utf-8")) > 128:
            raise ValueError(f"{name} must be non-empty canonical UTF-8 text")

    source_paths = tuple(ROOT / item for item in SOURCES)
    missing = [str(path) for path in source_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"root-session dependency closure is missing: {missing}")

    output_directory = args.output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    module_path = output_directory / args.module_name
    wasm_path = module_path.with_suffix(".wasm")
    receipt_path = output_directory / "root-session-build-receipt.json"
    source_records, kernel_sha256 = hash_records(source_paths)
    source_fingerprint = engine_source_fingerprint()
    compiler_version = subprocess.run(
        [str(compiler), "--version"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    exception_flag = (
        "-fwasm-exceptions"
        if args.exception_strategy == "wasm"
        else "-fexceptions"
    )
    command = [
        str(compiler),
        str(PACKAGE / "_native_eval.cpp"),
        str(PACKAGE / "native_subtree.cpp"),
        str(PACKAGE / "native_subtree_wasm.cpp"),
        str(PACKAGE / "native_root_session_wasm.cpp"),
        str(PACKAGE / "_native_mate.cpp"),
        "-I",
        str(PACKAGE),
        "-std=c++20",
        "-O3",
        "-flto",
        exception_flag,
        "-DSPC_NATIVE_CORE_ONLY=1",
        "-DSPC_NATIVE_MATE_CORE_ONLY=1",
        "-sALLOW_MEMORY_GROWTH=1",
        f"-sINITIAL_MEMORY={args.initial_memory_bytes}",
        f"-sMAXIMUM_MEMORY={args.maximum_memory_bytes}",
        f"-sSTACK_SIZE={args.stack_bytes}",
        "-sABORTING_MALLOC=0",
        f"-sMALLOC={args.allocator}",
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
        str(module_path),
    ]
    if args.wasm_simd:
        command.insert(command.index(exception_flag) + 1, "-msimd128")
    subprocess.run(command, cwd=ROOT, check=True)
    if not module_path.is_file() or not wasm_path.is_file():
        raise RuntimeError("Emscripten did not produce both module and WASM artifacts")

    artifact_records, artifact_set_sha256 = hash_records(
        (module_path, wasm_path),
        relative_to=output_directory,
    )
    module_js_sha256 = sha256_file(module_path)
    wasm_sha256 = sha256_file(wasm_path)
    receipt = {
        "schema": "spc-root-session-build-receipt-v1",
        "status": "built-not-certified",
        "product_publishable": False,
        "certificate_id": None,
        "source_revision": git_output("rev-parse", "HEAD"),
        "source_fingerprint": source_fingerprint,
        "kernel_sha256": kernel_sha256,
        "wasm_sha256": wasm_sha256,
        "module_js_sha256": module_js_sha256,
        "support_files": [],
        "engine_version": args.engine_version,
        "ruleset_version": args.ruleset_version,
        "profile_id": args.profile_id,
        "source_inputs": source_records,
        "runtime_variant": "single",
        "thread_count": 1,
        "pthreads": False,
        "optimization": {
            "level": "O3",
            "lto": True,
            "exception_strategy": args.exception_strategy,
            "exception_flag": exception_flag,
            "wasm_simd": args.wasm_simd,
            "simd_flag": "-msimd128" if args.wasm_simd else None,
            "allocator": args.allocator,
        },
        "runtime_requirements": {
            "ordinary_module_worker": True,
            "pthreads": False,
            "cross_origin_isolated": False,
            "native_wasm_exception_handling": args.exception_strategy == "wasm",
            "wasm_simd": args.wasm_simd,
        },
        "session_geometry": {
            "desktop_series_cache_capacity": args.desktop_series_cache_capacity,
            "root_contract_tt_capacity": args.root_tt_capacity,
            "root_contract_eval_capacity": args.root_eval_capacity,
        },
        "memory_envelope": {
            "initial_bytes": args.initial_memory_bytes,
            "estimated_peak_bytes": args.estimated_peak_memory_bytes,
            "maximum_bytes": args.maximum_memory_bytes,
            "growth_enabled": True,
            "stack_bytes": args.stack_bytes,
            "hard_maximum_linked": True,
            "runtime_peak_verified": False,
        },
        "abi": {
            "root_session_version": 2,
            "prefix_kernel_version": 1,
            "series_mate_version": 1,
            "exports": list(EXPORTED_FUNCTIONS),
            "reply_mate_safety": False,
            "canonical_root_tactical_policy": "canonical-boundary-policy-v1",
            "legacy_root_tactical_protection": False,
        },
        "toolchain": {
            "path": str(compiler),
            "sha256": sha256_file(compiler),
            "version": compiler_version,
        },
        "command": command,
        "artifacts": artifact_records,
        "artifact_set_sha256": artifact_set_sha256,
        "gates": {
            "clean_dependency_closure": False,
            "root_session_smoke": False,
            "persistent_iteration_reuse": False,
            "mate_parity": False,
            "deadline_and_work_receipts": False,
            "browser_runtime": False,
            "under_60_seconds_w32_d5": False,
        },
    }
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"root-session WASM build failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
