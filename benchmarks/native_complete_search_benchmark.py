from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
from typing import Any


CASES = {
    "S1": (None, 1, 0),
    "S3": (
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        3,
        0,
    ),
    "S4": (
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R "
        "b KQkq - 1 3",
        4,
        0,
    ),
    "S22": ("8/8/8/8/6Q1/2K5/6k1/8 b - - 144 109", 22, 8),
}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_native() -> object:
    import scottish_progressive

    override = os.environ.get("SPC_BENCHMARK_NATIVE_PACKAGE")
    if override:
        native_path = Path(override).resolve()
        if not native_path.is_dir():
            raise RuntimeError("SPC_BENCHMARK_NATIVE_PACKAGE is not a directory")
        scottish_progressive.__path__.insert(0, str(native_path))
    from scottish_progressive import evaluation

    native = evaluation._native_eval
    if native is None or not hasattr(native, "generate_complete_series"):
        raise RuntimeError("source-matched native complete-series kernel is required")
    return native


class _NativeMode:
    def __init__(self, native: object, *, enable_bulk: bool) -> None:
        self._native = native
        self._enable_bulk = enable_bulk
        self.bulk_calls = 0

    def __getattr__(self, name: str) -> Any:
        if name == "generate_complete_series":
            if not self._enable_bulk:
                raise AttributeError(name)
            return self._generate_complete_series
        return getattr(self._native, name)

    def _generate_complete_series(self, *args: Any) -> Any:
        self.bulk_calls += 1
        return self._native.generate_complete_series(*args)


def _native_metadata(native: object) -> dict[str, str]:
    module_path = Path(native.__file__).resolve()
    return {
        "module_filename": module_path.name,
        "module_sha256": _file_sha256(module_path),
        "source_identity": str(native.SOURCE_IDENTITY),
    }


def _semantic_signature(result: Any) -> dict[str, Any]:
    alternatives = [
        {
            "score": item.score,
            "series": item.series.machine_notation,
            "series_outcome": (
                item.series.outcome.value if item.series.outcome is not None else None
            ),
            "series_ended_by_check": item.series.ended_by_check,
            "series_unused_moves": item.series.unused_moves,
            "series_transposition_count": item.series.transposition_count,
            "pv": [series.machine_notation for series in item.principal_variation],
            "proof": item.proof,
            "proof_bounds": list(item.proof_bounds),
        }
        for item in result.alternatives
    ]
    return {
        "score": result.score,
        "best_series": (
            result.best_series.machine_notation if result.best_series else None
        ),
        "principal_variation": [
            series.machine_notation for series in result.principal_variation
        ],
        "alternatives_sha256": hashlib.sha256(
            json.dumps(alternatives, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "completed_depth": result.completed_depth,
        "exact_width": result.exact_width,
        "timed_out": result.timed_out,
        "work_limit_reached": result.work_limit_reached,
        "root_scores_complete": result.root_scores_complete,
        "proof": result.proof,
        "adjudication_status": result.adjudication_status,
        "work": asdict(result.stats),
    }


def _worker(
    case_name: str,
    mode: str,
    depth: int,
    branch_cap: int,
    max_work: int,
) -> int:
    native = _load_native()
    from scottish_progressive import evaluation
    from scottish_progressive.model import (
        ENGINE_SOURCE_FINGERPRINT,
        ENGINE_VERSION,
        ProgressiveState,
    )
    from scottish_progressive.profiles import baseline_profile
    from scottish_progressive.search import SearchLimits, analyze

    proxy = _NativeMode(native, enable_bulk=mode == "bulk-native")
    evaluation._native_eval = proxy
    fen, series_number, quiet_series = CASES[case_name]
    state = (
        ProgressiveState.initial()
        if fen is None
        else ProgressiveState.from_fen(
            fen,
            series_number,
            quiet_series=quiet_series,
        )
    )
    started = time.perf_counter()
    result = analyze(
        state,
        SearchLimits(
            depth_series=depth,
            max_series_per_node=branch_cap,
            max_generation_positions=max_work,
            collect_all_root_scores=False,
        ),
        baseline_profile(),
    )
    elapsed = time.perf_counter() - started
    print(
        json.dumps(
            {
                "case": case_name,
                "mode": mode,
                "elapsed_seconds": elapsed,
                "bulk_calls": proxy.bulk_calls,
                "engine": {
                    "version": ENGINE_VERSION,
                    "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
                },
                "native_runtime": _native_metadata(native),
                "signature": _semantic_signature(result),
            },
            sort_keys=True,
        )
    )
    return 0


def _fresh_sample(
    case_name: str,
    mode: str,
    depth: int,
    branch_cap: int,
    max_work: int,
) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            case_name,
            "--mode",
            mode,
            "--depth",
            str(depth),
            "--branch-cap",
            str(branch_cap),
            "--max-work",
            str(max_work),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    return json.loads(completed.stdout)


def _run(args: argparse.Namespace) -> dict[str, Any]:
    native = _load_native()
    from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT, ENGINE_VERSION

    engine = {
        "version": ENGINE_VERSION,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
    }
    native_runtime = _native_metadata(native)
    generated_at = datetime.now(UTC).isoformat()
    configuration = {
        "depth_series": args.depth,
        "branch_cap": args.branch_cap,
        "max_generation_positions": args.max_work,
        "fresh_process_samples": args.samples,
        "sampling_order": "paired-interleaved-alternating",
        "baseline": "same-native-micro-kernel-with-bulk-symbol-hidden",
        "native_runtime_source": (
            "override-package-dir"
            if os.environ.get("SPC_BENCHMARK_NATIVE_PACKAGE")
            else "installed-package"
        ),
        "cases": CASES,
    }
    artifact_seed = json.dumps(
        {
            "schema": "spc-native-complete-search-benchmark-v1",
            "generated_at": generated_at,
            "engine": engine,
            "native_runtime": native_runtime,
            "configuration": configuration,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    payload: dict[str, Any] = {
        "schema": "spc-native-complete-search-benchmark-v1",
        "artifact_id": "spc-native-complete-search-"
        + hashlib.sha256(artifact_seed).hexdigest()[:16],
        "generated_at": generated_at,
        "engine": engine,
        "native_runtime": native_runtime,
        "configuration": configuration,
        "environment": {
            "python_version": platform.python_version(),
            "python_compiler": platform.python_compiler(),
            "platform": platform.platform(),
            "cpu": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER"),
            "logical_cpu_count": os.cpu_count(),
        },
        "cases": {},
    }
    modes = ("micro-native", "bulk-native")
    for case_name in CASES:
        timings = {mode: [] for mode in modes}
        signatures: dict[str, dict[str, Any]] = {}
        raw_samples: list[dict[str, Any]] = []
        bulk_calls: list[int] = []
        for repetition in range(args.samples):
            order = modes if repetition % 2 == 0 else tuple(reversed(modes))
            for order_index, mode in enumerate(order):
                sample = _fresh_sample(
                    case_name,
                    mode,
                    args.depth,
                    args.branch_cap,
                    args.max_work,
                )
                if sample["engine"] != engine:
                    raise RuntimeError(f"{case_name} {mode} engine identity drift")
                if sample["native_runtime"] != native_runtime:
                    raise RuntimeError(f"{case_name} {mode} native identity drift")
                signature = sample["signature"]
                prior = signatures.setdefault(mode, signature)
                if signature != prior:
                    raise RuntimeError(f"{case_name} {mode} is non-deterministic")
                elapsed = float(sample["elapsed_seconds"])
                timings[mode].append(elapsed)
                calls = int(sample["bulk_calls"])
                if mode == "micro-native" and calls != 0:
                    raise RuntimeError("micro-native baseline invoked the bulk kernel")
                if mode == "bulk-native":
                    bulk_calls.append(calls)
                raw_samples.append(
                    {
                        "repetition": repetition,
                        "order_index": order_index,
                        "mode": mode,
                        "elapsed_seconds": elapsed,
                        "bulk_calls": calls,
                    }
                )
        if signatures["micro-native"] != signatures["bulk-native"]:
            raise RuntimeError(f"{case_name} output/work drift")
        if not bulk_calls or min(bulk_calls) < 1:
            raise RuntimeError(f"{case_name} did not exercise the bulk kernel")
        baseline_median = statistics.median(timings["micro-native"])
        bulk_median = statistics.median(timings["bulk-native"])
        payload["cases"][case_name] = {
            "micro_native_seconds": timings["micro-native"],
            "bulk_native_seconds": timings["bulk-native"],
            "micro_native_median_seconds": baseline_median,
            "bulk_native_median_seconds": bulk_median,
            "speedup": baseline_median / bulk_median,
            "identical_output_and_work": True,
            "signature": signatures["bulk-native"],
            "raw_samples": raw_samples,
        }
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--branch-cap", type=int, default=32)
    parser.add_argument("--max-work", type=int, default=250_000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=tuple(CASES))
    parser.add_argument(
        "--mode",
        choices=("micro-native", "bulk-native"),
        default="bulk-native",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.samples < 1:
        raise SystemExit("--samples must be positive")
    if args.worker:
        return _worker(
            args.worker,
            args.mode,
            args.depth,
            args.branch_cap,
            args.max_work,
        )
    payload = _run(args)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
