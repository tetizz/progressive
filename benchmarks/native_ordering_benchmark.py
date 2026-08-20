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
    "S1": (None, 1),
    "S3": ("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 3),
    "S4": (
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R "
        "b KQkq - 1 3",
        4,
    ),
}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _native_runtime_metadata() -> dict[str, str] | None:
    from scottish_progressive import evaluation

    if not evaluation.native_acceleration_available():
        return None
    module_path = Path(evaluation._native_eval.__file__).resolve()
    return {
        "module_filename": module_path.name,
        "module_sha256": _file_sha256(module_path),
        "source_identity": evaluation._native_eval.SOURCE_IDENTITY,
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
    alternatives_digest = hashlib.sha256(
        json.dumps(alternatives, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    stats = result.stats
    return {
        "score": result.score,
        "best_series": (
            result.best_series.machine_notation if result.best_series else None
        ),
        "principal_variation": [
            series.machine_notation for series in result.principal_variation
        ],
        "alternatives_sha256": alternatives_digest,
        "completed_depth": result.completed_depth,
        "exact_width": result.exact_width,
        "timed_out": result.timed_out,
        "work_limit_reached": result.work_limit_reached,
        "root_scores_complete": result.root_scores_complete,
        "proof": result.proof,
        "adjudication_status": result.adjudication_status,
        "work": asdict(stats),
    }


def _worker(case_name: str, depth: int, branch_cap: int, max_work: int) -> int:
    import chess

    from scottish_progressive.evaluation import native_acceleration_available
    from scottish_progressive.model import (
        ENGINE_SOURCE_FINGERPRINT,
        ENGINE_VERSION,
        ProgressiveState,
    )
    from scottish_progressive.profiles import baseline_profile
    from scottish_progressive.search import SearchLimits, analyze

    fen, series_number = CASES[case_name]
    state = (
        ProgressiveState.initial()
        if fen is None
        else ProgressiveState.from_fen(fen, series_number)
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
                "native_loaded": native_acceleration_available(),
                "native_runtime": _native_runtime_metadata(),
                "engine": {
                    "version": ENGINE_VERSION,
                    "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
                },
                "elapsed_seconds": elapsed,
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
    environment = os.environ.copy()
    if mode == "python":
        environment["SPC_DISABLE_NATIVE"] = "1"
    else:
        environment.pop("SPC_DISABLE_NATIVE", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            case_name,
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
        env=environment,
    )
    payload = json.loads(completed.stdout)
    expected_native = mode == "native"
    if bool(payload["native_loaded"]) != expected_native:
        raise RuntimeError(
            f"{mode} sample loaded native={payload['native_loaded']}; "
            "build the extension before benchmarking"
        )
    return payload


def _run(args: argparse.Namespace) -> dict[str, Any]:
    from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT, ENGINE_VERSION

    native_runtime = _native_runtime_metadata()
    if native_runtime is None:
        raise RuntimeError("a source-matched native extension is required")
    engine = {
        "version": ENGINE_VERSION,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
    }
    configuration = {
        "depth_series": args.depth,
        "branch_cap": args.branch_cap,
        "max_generation_positions": args.max_work,
        "fresh_process_samples": args.samples,
        "sampling_order": "paired-interleaved-alternating",
    }
    generated_at = datetime.now(UTC).isoformat()
    artifact_seed = json.dumps(
        {
            "schema": "spc-native-ordering-benchmark-v2",
            "generated_at": generated_at,
            "engine": engine,
            "native_runtime": native_runtime,
            "configuration": configuration,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload: dict[str, Any] = {
        "schema": "spc-native-ordering-benchmark-v2",
        "artifact_id": (
            "spc-native-ordering-" + hashlib.sha256(artifact_seed).hexdigest()[:16]
        ),
        "generated_at": generated_at,
        "engine": engine,
        "native_runtime": native_runtime,
        "configuration": configuration,
        "environment": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "python_compiler": platform.python_compiler(),
            "platform": platform.platform(),
            "operating_system": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
            },
            "cpu": {
                "identifier": (
                    platform.processor()
                    or os.environ.get("PROCESSOR_IDENTIFIER")
                    or "unknown"
                ),
                "logical_count": os.cpu_count(),
            },
        },
        "sample_provenance_validated": True,
        "cases": {},
    }
    for case_name in CASES:
        measurements: dict[str, list[float]] = {"python": [], "native": []}
        signatures: dict[str, dict[str, Any]] = {}
        raw_samples: list[dict[str, Any]] = []
        for repetition in range(args.samples):
            mode_order = (
                ("python", "native")
                if repetition % 2 == 0
                else ("native", "python")
            )
            for order_index, mode in enumerate(mode_order):
                sample = _fresh_sample(
                    case_name,
                    mode,
                    args.depth,
                    args.branch_cap,
                    args.max_work,
                )
                if sample["engine"] != engine:
                    raise RuntimeError(f"engine identity drift for {case_name} {mode}")
                expected_runtime = native_runtime if mode == "native" else None
                if sample["native_runtime"] != expected_runtime:
                    raise RuntimeError(
                        f"native artifact identity drift for {case_name} {mode}"
                    )
                elapsed = float(sample["elapsed_seconds"])
                measurements[mode].append(elapsed)
                raw_samples.append(
                    {
                        "repetition": repetition,
                        "order_index": order_index,
                        "mode": mode,
                        "elapsed_seconds": elapsed,
                    }
                )
                signature = sample["signature"]
                prior = signatures.setdefault(mode, signature)
                if signature != prior:
                    raise RuntimeError(f"non-deterministic {case_name} {mode} output")
        if signatures["python"] != signatures["native"]:
            raise RuntimeError(f"native output/work drift for {case_name}")
        python_median = statistics.median(measurements["python"])
        native_median = statistics.median(measurements["native"])
        payload["cases"][case_name] = {
            "python_seconds": measurements["python"],
            "native_seconds": measurements["native"],
            "python_median_seconds": python_median,
            "native_median_seconds": native_median,
            "speedup": python_median / native_median,
            "identical_output_and_work": True,
            "raw_samples": raw_samples,
            "signature": signatures["native"],
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
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.samples < 1:
        raise SystemExit("--samples must be positive")
    if args.worker:
        return _worker(args.worker, args.depth, args.branch_cap, args.max_work)
    payload = _run(args)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
