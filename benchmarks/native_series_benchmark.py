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
    "S1": {
        "fen": None,
        "series_number": 1,
        "quiet_series": 0,
        "frontier_cap": 32,
    },
    "S3": {
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "series_number": 3,
        "quiet_series": 0,
        "frontier_cap": 32,
    },
    "S4": {
        "fen": (
            "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R "
            "b KQkq - 1 3"
        ),
        "series_number": 4,
        "quiet_series": 0,
        "frontier_cap": 32,
    },
    "S22": {
        "fen": "8/8/8/8/6Q1/2K5/6k1/8 b - - 144 109",
        "series_number": 22,
        "quiet_series": 8,
        "frontier_cap": 32,
    },
    "S101": {
        "fen": "7k/8/8/8/8/8/6N1/K7 w - - 0 1",
        "series_number": 101,
        "quiet_series": 10,
        "frontier_cap": 1,
    },
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
    module = evaluation._native_eval
    if not (
        hasattr(module, "expand_legal_move_variants")
        and hasattr(module, "has_legal_move")
    ):
        return None
    module_path = Path(module.__file__).resolve()
    return {
        "module_filename": module_path.name,
        "module_sha256": _file_sha256(module_path),
        "source_identity": module.SOURCE_IDENTITY,
    }


def _semantic_signature(results: Any, stats: Any) -> dict[str, Any]:
    series = [
        {
            "moves": result.machine_notation,
            "san": list(result.san),
            "pfen": result.final_state.pfen,
            "ended_by_check": result.ended_by_check,
            "outcome": result.outcome.value if result.outcome is not None else None,
            "unused_moves": result.unused_moves,
            "transposition_count": result.transposition_count,
        }
        for result in results
    ]
    return {
        "result_count": len(series),
        "series_sha256": hashlib.sha256(
            json.dumps(series, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "work": asdict(stats),
    }


def _worker(case_name: str, max_work: int) -> int:
    from scottish_progressive.evaluation import native_acceleration_available
    from scottish_progressive.model import (
        ENGINE_SOURCE_FINGERPRINT,
        ENGINE_VERSION,
        ProgressiveState,
    )
    from scottish_progressive.rules import GenerationStats, generate_series

    case = CASES[case_name]
    state = (
        ProgressiveState.initial()
        if case["fen"] is None
        else ProgressiveState.from_fen(
            case["fen"],
            case["series_number"],
            quiet_series=case["quiet_series"],
        )
    )
    stats = GenerationStats()
    started = time.perf_counter()
    results = generate_series(
        state,
        stats=stats,
        max_frontier_states=case["frontier_cap"],
        max_positions=max_work,
        frontier_score=lambda _board: 0,
    )
    elapsed = time.perf_counter() - started
    print(
        json.dumps(
            {
                "case": case_name,
                "elapsed_seconds": elapsed,
                "native_loaded": native_acceleration_available(),
                "native_runtime": _native_runtime_metadata(),
                "engine": {
                    "version": ENGINE_VERSION,
                    "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
                },
                "signature": _semantic_signature(results, stats),
            },
            sort_keys=True,
        )
    )
    return 0


def _fresh_sample(case_name: str, mode: str, max_work: int) -> dict[str, Any]:
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
            "--max-work",
            str(max_work),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(completed.stdout)


def _run(args: argparse.Namespace) -> dict[str, Any]:
    from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT, ENGINE_VERSION

    native_runtime = _native_runtime_metadata()
    if native_runtime is None:
        raise RuntimeError("a source-matched native series kernel is required")
    engine = {
        "version": ENGINE_VERSION,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
    }
    generated_at = datetime.now(UTC).isoformat()
    configuration = {
        "max_generation_positions": args.max_work,
        "fresh_process_samples": args.samples,
        "frontier_score": "constant-zero-isolates-series-kernel",
        "sampling_order": "paired-interleaved-alternating",
        "cases": CASES,
    }
    payload: dict[str, Any] = {
        "schema": "spc-native-series-benchmark-v1",
        "artifact_id": "spc-native-series-"
        + hashlib.sha256(
            json.dumps(
                {
                    "generated_at": generated_at,
                    "engine": engine,
                    "native_runtime": native_runtime,
                    "configuration": configuration,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16],
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
        "sample_provenance_validated": True,
        "cases": {},
    }
    for case_name in CASES:
        measurements: dict[str, list[float]] = {"python": [], "native": []}
        signatures: dict[str, dict[str, Any]] = {}
        raw_samples: list[dict[str, Any]] = []
        for repetition in range(args.samples):
            order = (
                ("python", "native")
                if repetition % 2 == 0
                else ("native", "python")
            )
            for order_index, mode in enumerate(order):
                sample = _fresh_sample(case_name, mode, args.max_work)
                expected_native = mode == "native"
                if bool(sample["native_loaded"]) != expected_native:
                    raise RuntimeError(f"{case_name} {mode} loaded wrong kernel mode")
                if sample["engine"] != engine:
                    raise RuntimeError(f"{case_name} {mode} engine identity drift")
                expected_runtime = native_runtime if expected_native else None
                if sample["native_runtime"] != expected_runtime:
                    raise RuntimeError(f"{case_name} {mode} native identity drift")
                signature = sample["signature"]
                prior = signatures.setdefault(mode, signature)
                if signature != prior:
                    raise RuntimeError(f"{case_name} {mode} output is non-deterministic")
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
        if signatures["python"] != signatures["native"]:
            raise RuntimeError(f"{case_name} native output or work drift")
        python_median = statistics.median(measurements["python"])
        native_median = statistics.median(measurements["native"])
        payload["cases"][case_name] = {
            "python_seconds": measurements["python"],
            "native_seconds": measurements["native"],
            "python_median_seconds": python_median,
            "native_median_seconds": native_median,
            "speedup": python_median / native_median,
            "identical_output_and_work": True,
            "signature": signatures["native"],
            "raw_samples": raw_samples,
        }
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--max-work", type=int, default=250_000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=tuple(CASES))
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.samples < 1:
        raise SystemExit("--samples must be positive")
    if args.worker:
        return _worker(args.worker, args.max_work)
    payload = _run(args)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
