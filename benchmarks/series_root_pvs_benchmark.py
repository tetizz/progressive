from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import statistics
import time

import scottish_progressive.search as search_module
from scottish_progressive.model import ProgressiveState
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.search import SearchLimits, analyze


def _replay(history: tuple[tuple[str, ...], ...]) -> ProgressiveState:
    state = ProgressiveState.initial()
    for series in history:
        state = play_series(state, series).final_state
    return state


CASES = {
    "initial-d3": (
        ProgressiveState.initial(),
        SearchLimits(
            depth_series=3,
            max_series_per_node=32,
            max_generation_positions=2_000_000,
            time_limit_seconds=180.0,
            collect_all_root_scores=False,
            native_threads=1,
        ),
    ),
    "initial-d4": (
        ProgressiveState.initial(),
        SearchLimits(
            depth_series=4,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            time_limit_seconds=180.0,
            collect_all_root_scores=False,
            native_threads=16,
        ),
    ),
    "initial-d5": (
        ProgressiveState.initial(),
        SearchLimits(
            depth_series=5,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            time_limit_seconds=180.0,
            collect_all_root_scores=False,
            native_threads=1,
        ),
    ),
    "after-e4-d3": (
        _replay((("e2e4",),)),
        SearchLimits(
            depth_series=3,
            max_series_per_node=32,
            max_generation_positions=2_000_000,
            time_limit_seconds=180.0,
            collect_all_root_scores=False,
            native_threads=1,
        ),
    ),
    "after-e4-d4": (
        _replay((("e2e4",),)),
        SearchLimits(
            depth_series=4,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            time_limit_seconds=180.0,
            collect_all_root_scores=False,
            native_threads=16,
        ),
    ),
    "series3-d3": (
        _replay((("e2e4",), ("f7f5", "e8f7"))),
        SearchLimits(
            depth_series=3,
            max_series_per_node=32,
            max_generation_positions=3_000_000,
            time_limit_seconds=180.0,
            collect_all_root_scores=False,
            native_threads=1,
        ),
    ),
    "hard-s4-d5": (
        _replay(
            (
                ("g1f3",),
                ("e7e6", "d8f6"),
                ("d2d4", "c1g5", "g5f6"),
            )
        ),
        SearchLimits(
            depth_series=5,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            time_limit_seconds=180.0,
            collect_all_root_scores=False,
            native_threads=16,
        ),
    ),
    "s7-d4": (
        ProgressiveState.from_fen(
            "rnk3nr/pp3ppp/8/8/8/1Pp1P3/P1PP1PPP/R1b1K1NR w K - 0 13",
            7,
        ),
        SearchLimits(
            depth_series=4,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            time_limit_seconds=180.0,
            collect_all_root_scores=False,
            native_threads=16,
        ),
    ),
}


def _signature(result: object) -> tuple[object, ...]:
    return (
        result.score,
        result.best_series.machine_notation if result.best_series else None,
        tuple(item.machine_notation for item in result.principal_variation),
        tuple(
            (
                item.series.machine_notation,
                item.score,
                tuple(pv.machine_notation for pv in item.principal_variation),
                item.proof_bounds,
            )
            for item in result.alternatives
        ),
        result.completed_depth,
        result.proof,
        result.forced,
        result.exact_width,
        result.timed_out,
        result.work_limit_reached,
        result.root_scores_complete,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=tuple(CASES), required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    args = parser.parse_args()
    state, limits = CASES[args.case]
    rows: list[dict[str, object]] = []
    try:
        for repetition in range(args.repetitions):
            variants = (False, True) if repetition % 2 == 0 else (True, False)
            for enabled in variants:
                search_module.ROOT_PVS_ENABLED = enabled
                started = time.perf_counter()
                result = analyze(state, limits, baseline_profile())
                rows.append(
                    {
                        "repetition": repetition,
                        "root_pvs": enabled,
                        "seconds": time.perf_counter() - started,
                        "work": result.stats.work_positions,
                        "signature": _signature(result),
                        "stats": asdict(result.stats),
                    }
                )
    finally:
        search_module.ROOT_PVS_ENABLED = True

    baseline = [row for row in rows if not row["root_pvs"]]
    candidate = [row for row in rows if row["root_pvs"]]
    work_deltas = [
        int(right["work"]) - int(left["work"])
        for left, right in zip(baseline, candidate)
    ]
    print(
        json.dumps(
            {
                "case": args.case,
                "semantic_match": all(
                    left["signature"] == right["signature"]
                    for left, right in zip(baseline, candidate)
                ),
                "baseline_median_seconds": statistics.median(
                    row["seconds"] for row in baseline
                ),
                "candidate_median_seconds": statistics.median(
                    row["seconds"] for row in candidate
                ),
                "baseline_work": [row["work"] for row in baseline],
                "candidate_work": [row["work"] for row in candidate],
                "paired_work_deltas": work_deltas,
                "paired_work_percent": [
                    100.0 * delta / int(left["work"])
                    for left, delta in zip(baseline, work_deltas)
                ],
                "candidate_root_pvs": [
                    {
                        key: row["stats"][key]
                        for key in (
                            "root_pvs_zero_window_searches",
                            "root_pvs_researches",
                            "root_pvs_tt_writes_rolled_back",
                        )
                    }
                    for row in candidate
                ],
                "baseline_signature": baseline[0]["signature"],
                "candidate_signature": candidate[0]["signature"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
