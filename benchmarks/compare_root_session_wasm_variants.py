from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "benchmarks" / "build_root_session_wasm.py"
VERIFY = ROOT / "benchmarks" / "verify_root_session_wasm.mjs"
VARIANTS = (
    ("baseline", "emscripten", False, "dlmalloc"),
    ("native-wasm-eh", "wasm", False, "dlmalloc"),
    ("wasm-simd", "emscripten", True, "dlmalloc"),
    ("emmalloc", "emscripten", False, "emmalloc"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and compare exact root-session WASM compiler factors."
    )
    parser.add_argument("--em-plus-plus", required=True, type=Path)
    parser.add_argument("--node", default=shutil.which("node") or "node")
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=ROOT / "build" / "root-session-wasm-matrix",
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout-ms", type=int, default=120_000)
    parser.add_argument("--series-cache-capacity", type=int, default=65_536)
    parser.add_argument("--tt-capacity", type=int, default=262_144)
    parser.add_argument("--eval-capacity", type=int, default=262_144)
    parser.add_argument("--max-work", type=int, default=20_000_000)
    return parser.parse_args()


def run(command: list[str]) -> None:
    subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def semantic_signature(receipt: Mapping[str, Any]) -> dict[str, Any]:
    mate = receipt["mate_receipts"]
    return {
        "persistent_results": receipt["persistent_results"],
        "root_session_contract": receipt["root_session_contract"],
        "prefix_contract": receipt["prefix_contract"],
        "mate_found": mate["found"],
        "mate_exhausted": mate["exhausted"],
        "mate_work_limit": mate["work_limit"],
        "mate_deadline_status": {
            key: mate["deadline"][key]
            for key in ("kernel_status", "proof_status", "complete")
        },
    }


def medians(receipts: list[Mapping[str, Any]]) -> dict[str, float]:
    keys = receipts[0]["timings_ms"].keys()
    return {
        key: statistics.median(
            float(receipt["timings_ms"][key]) for receipt in receipts
        )
        for key in keys
    }


def main() -> int:
    args = parse_args()
    if args.runs < 2:
        raise ValueError("--runs must be at least 2 so timing noise is visible")
    for name, value in (
        ("timeout", args.timeout_ms),
        ("series cache", args.series_cache_capacity),
        ("TT capacity", args.tt_capacity),
        ("eval capacity", args.eval_capacity),
        ("max work", args.max_work),
    ):
        if value < 1:
            raise ValueError(f"{name} must be positive")
    output = args.output_directory.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    reference_signature: dict[str, Any] | None = None

    for name, exception_strategy, simd, allocator in VARIANTS:
        lane = output / name
        build_command = [
            sys.executable,
            str(BUILD),
            "--em-plus-plus",
            str(args.em_plus_plus.resolve()),
            "--output-directory",
            str(lane),
            "--exception-strategy",
            exception_strategy,
            "--allocator",
            allocator,
            "--desktop-series-cache-capacity",
            str(args.series_cache_capacity),
            "--root-tt-capacity",
            str(args.tt_capacity),
            "--root-eval-capacity",
            str(args.eval_capacity),
        ]
        if simd:
            build_command.append("--wasm-simd")
        run(build_command)
        module = lane / "spc-root-session.mjs"
        wasm = lane / "spc-root-session.wasm"
        build_receipt_path = lane / "root-session-build-receipt.json"
        build_receipt = json.loads(build_receipt_path.read_text(encoding="utf-8"))
        smoke_receipts: list[Mapping[str, Any]] = []
        for index in range(args.runs):
            smoke_path = lane / f"smoke-{index + 1}.json"
            run(
                [
                    str(args.node),
                    str(VERIFY),
                    "--module",
                    str(module),
                    "--wasm",
                    str(wasm),
                    "--build-receipt",
                    str(build_receipt_path),
                    "--output",
                    str(smoke_path),
                    "--timeout-ms",
                    str(args.timeout_ms),
                    "--series-cache-capacity",
                    str(args.series_cache_capacity),
                    "--tt-capacity",
                    str(args.tt_capacity),
                    "--eval-capacity",
                    str(args.eval_capacity),
                    "--max-work",
                    str(args.max_work),
                ]
            )
            smoke = json.loads(smoke_path.read_text(encoding="utf-8"))
            if smoke.get("status") != "passed-not-certified":
                raise AssertionError(f"{name} smoke did not pass fail-closed gates")
            signature = semantic_signature(smoke)
            if reference_signature is None:
                reference_signature = signature
            elif signature != reference_signature:
                raise AssertionError(f"{name} changed the exact semantic/work signature")
            smoke_receipts.append(smoke)
        results[name] = {
            "exception_strategy": exception_strategy,
            "wasm_simd": simd,
            "allocator": allocator,
            "wasm_sha256": build_receipt["wasm_sha256"],
            "module_js_sha256": build_receipt["module_js_sha256"],
            "artifact_set_sha256": build_receipt["artifact_set_sha256"],
            "wasm_bytes": wasm.stat().st_size,
            "module_js_bytes": module.stat().st_size,
            "runs": args.runs,
            "median_timings_ms": medians(smoke_receipts),
            "individual_timings_ms": [
                receipt["timings_ms"] for receipt in smoke_receipts
            ],
            "observed_memory_bytes": [
                receipt["memory"]["observed_bytes"] for receipt in smoke_receipts
            ],
        }

    receipt = {
        "schema": "spc-root-session-wasm-compiler-matrix-v1",
        "status": "passed-not-certified",
        "product_publishable": False,
        "safety_certified": False,
        "semantic_and_work_signature_equal": True,
        "selected_variant": None,
        "selection_blocker": (
            "Opera ordinary-Worker feature/runtime measurements and the exact "
            "W32 D5 gate are still required"
        ),
        "factors_are_one_at_a_time": True,
        "variants": results,
        "gates": {
            "node_parity": True,
            "opera_worker_parity": False,
            "opera_feature_support": False,
            "w32_d5_under_60_seconds": False,
        },
    }
    receipt_path = output / "compiler-matrix-receipt.json"
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
        print(f"root-session WASM matrix failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
