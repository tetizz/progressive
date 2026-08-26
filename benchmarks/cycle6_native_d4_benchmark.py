"""Paired native D4 benchmark for the accepted cycle-6 move-sort change.

Each sample runs in a fresh process. Baseline and candidate order alternates,
and acceptance requires the complete semantic and charged-work signatures to
match before timing medians are reported.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any


CASES = ("initial", "after-e4")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_state(case: str) -> object:
    from scottish_progressive.model import ProgressiveState
    from scottish_progressive.rules import play_series

    state = ProgressiveState.initial()
    if case == "after-e4":
        state = play_series(state, ("e2e4",)).final_state
    return state


def _semantic_signature(result: Any) -> dict[str, Any]:
    return {
        "score": result.score,
        "best_series": (
            result.best_series.machine_notation if result.best_series else None
        ),
        "principal_variation": [
            item.machine_notation for item in result.principal_variation
        ],
        "alternatives": [
            {
                "series": item.series.machine_notation,
                "score": item.score,
                "principal_variation": [
                    pv.machine_notation for pv in item.principal_variation
                ],
                "proof_bounds": list(item.proof_bounds),
            }
            for item in result.alternatives
        ],
        "completed_depth": result.completed_depth,
        "proof": result.proof,
        "forced": result.forced,
        "exact_width": result.exact_width,
        "timed_out": result.timed_out,
        "work_limit_reached": result.work_limit_reached,
        "root_scores_complete": result.root_scores_complete,
        "stats": asdict(result.stats),
    }


def _worker(package: Path, case: str) -> int:
    sys.path.insert(0, str(package.resolve()))
    from scottish_progressive import _native_eval
    from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT
    from scottish_progressive.profiles import baseline_profile
    from scottish_progressive.search import SearchLimits, analyze

    state = _load_state(case)
    started = time.perf_counter()
    result = analyze(
        state,
        SearchLimits(
            depth_series=4,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        baseline_profile(),
    )
    elapsed = time.perf_counter() - started
    module_path = Path(_native_eval.__file__).resolve()
    print(
        json.dumps(
            {
                "case": case,
                "elapsed_seconds": elapsed,
                "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
                "native_source_identity": _native_eval.SOURCE_IDENTITY,
                "native_sha256": _sha256(module_path),
                "signature": _semantic_signature(result),
            },
            sort_keys=True,
        )
    )
    return 0


def _sample(package: Path, case: str) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--package",
            str(package.resolve()),
            "--case",
            case,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(completed.stdout)


def _run(args: argparse.Namespace) -> dict[str, Any]:
    packages = {"baseline": args.baseline_package.resolve()}
    if args.candidate_package is not None:
        packages["candidate"] = args.candidate_package.resolve()
    for name, package in packages.items():
        if not package.is_dir():
            raise RuntimeError(f"{name} package directory is missing: {package}")

    payload: dict[str, Any] = {
        "schema": "spc-cycle6-native-d4-benchmark-v1",
        "configuration": {
            "optimization": (
                "sort compact pseudo Move entries before expansion instead "
                "of sorting ExpandedMove child states afterward"
            ),
            "depth_series": 4,
            "branch_cap": 32,
            "max_generation_positions": 10_000_000,
            "native_threads": 1,
            "fresh_process_samples": args.samples,
            "sampling_order": "paired-interleaved-alternating",
        },
        "packages": {name: str(path) for name, path in packages.items()},
        "cases": {},
    }
    for case in CASES:
        rows: list[dict[str, Any]] = []
        by_name: dict[str, list[dict[str, Any]]] = {
            name: [] for name in packages
        }
        names = tuple(packages)
        for repetition in range(args.samples):
            order = names if repetition % 2 == 0 else tuple(reversed(names))
            for order_index, name in enumerate(order):
                row = _sample(packages[name], case)
                row["repetition"] = repetition
                row["order_index"] = order_index
                row["runtime"] = name
                rows.append(row)
                by_name[name].append(row)

        baseline_signature = by_name["baseline"][0]["signature"]
        for name, samples in by_name.items():
            if any(row["signature"] != samples[0]["signature"] for row in samples):
                raise RuntimeError(f"{case} {name} was non-deterministic")
            if name != "baseline" and samples[0]["signature"] != baseline_signature:
                raise RuntimeError(f"{case} candidate output or work drifted")

        medians = {
            name: statistics.median(
                float(row["elapsed_seconds"]) for row in samples
            )
            for name, samples in by_name.items()
        }
        case_result: dict[str, Any] = {
            "rows": rows,
            "median_seconds": medians,
            "signature": baseline_signature,
        }
        if "candidate" in medians:
            case_result["speedup"] = medians["baseline"] / medians["candidate"]
            case_result["percent_faster"] = (
                (medians["baseline"] - medians["candidate"])
                / medians["baseline"]
                * 100.0
            )
            case_result["identical_output_and_work"] = True
        payload["cases"][case] = case_result
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure the cycle-6 native move-sort optimization with paired "
            "fresh-process D4 searches and exact semantic/work comparison."
        )
    )
    parser.add_argument("--baseline-package", type=Path)
    parser.add_argument("--candidate-package", type=Path)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--package", type=Path)
    parser.add_argument("--case", choices=CASES)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.worker:
        if args.package is None or args.case is None:
            raise SystemExit("--worker requires --package and --case")
        return _worker(args.package, args.case)
    if args.baseline_package is None:
        raise SystemExit("--baseline-package is required")
    if args.samples < 1:
        raise SystemExit("--samples must be positive")
    payload = _run(args)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
