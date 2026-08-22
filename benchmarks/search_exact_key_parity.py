from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import time

from benchmarks.series_root_pvs_expanded_parity import _signature, _states
from scottish_progressive.model import ProgressiveState
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.search import SearchLimits, analyze


_EXACT_SEARCH_KEY = ProgressiveState.search_key


def _use_public_text_key() -> None:
    ProgressiveState.search_key = property(  # type: ignore[assignment]
        lambda state: state.transposition_key
    )


def _use_exact_bitboard_key() -> None:
    ProgressiveState.search_key = _EXACT_SEARCH_KEY  # type: ignore[assignment]


def _exact_result(result: object) -> tuple[object, ...]:
    return (_signature(result), asdict(result.stats))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count-per-series", type=int, default=64)
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument("--width", type=int, default=4)
    args = parser.parse_args()
    limits = SearchLimits(
        depth_series=args.depth,
        max_series_per_node=args.width,
        collect_all_root_scores=False,
        native_threads=1,
    )
    mismatches: list[dict[str, object]] = []
    public_elapsed = 0.0
    exact_elapsed = 0.0
    count = 0
    try:
        for series_number in range(1, 9):
            for index, state in enumerate(
                _states(series_number, args.count_per_series)
            ):
                if count % 2:
                    _use_exact_bitboard_key()
                    started = time.perf_counter()
                    candidate = analyze(state.copy(), limits, baseline_profile())
                    exact_elapsed += time.perf_counter() - started
                    _use_public_text_key()
                    started = time.perf_counter()
                    baseline = analyze(state.copy(), limits, baseline_profile())
                    public_elapsed += time.perf_counter() - started
                else:
                    _use_public_text_key()
                    started = time.perf_counter()
                    baseline = analyze(state.copy(), limits, baseline_profile())
                    public_elapsed += time.perf_counter() - started
                    _use_exact_bitboard_key()
                    started = time.perf_counter()
                    candidate = analyze(state.copy(), limits, baseline_profile())
                    exact_elapsed += time.perf_counter() - started
                if _exact_result(candidate) != _exact_result(baseline):
                    mismatches.append(
                        {
                            "series": series_number,
                            "index": index,
                            "pfen": state.pfen,
                        }
                    )
                count += 1
    finally:
        _use_exact_bitboard_key()

    print(
        json.dumps(
            {
                "count": count,
                "count_per_series": args.count_per_series,
                "depth": args.depth,
                "width": args.width,
                "mismatch_count": len(mismatches),
                "mismatches": mismatches[:16],
                "public_key_elapsed_seconds": public_elapsed,
                "exact_key_elapsed_seconds": exact_elapsed,
                "speedup_percent": 100.0
                * (public_elapsed / exact_elapsed - 1.0),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
