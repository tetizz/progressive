from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import random
import time

import chess

import scottish_progressive.search as search_module
from scottish_progressive.model import ProgressiveState
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.search import SearchLimits, analyze


def _states(series_number: int, count: int) -> tuple[ProgressiveState, ...]:
    rng = random.Random(20260822 + series_number)
    states: list[ProgressiveState] = []
    seen: set[str] = set()
    while len(states) < count:
        squares = rng.sample(list(chess.SQUARES), 6)
        board = chess.Board(None)
        board.turn = chess.WHITE if series_number % 2 else chess.BLACK
        board.set_piece_at(squares[0], chess.Piece(chess.KING, chess.WHITE))
        board.set_piece_at(squares[1], chess.Piece(chess.KING, chess.BLACK))
        board.set_piece_at(
            squares[2],
            chess.Piece(rng.choice((chess.ROOK, chess.QUEEN)), chess.WHITE),
        )
        board.set_piece_at(
            squares[3],
            chess.Piece(rng.choice((chess.ROOK, chess.QUEEN)), chess.BLACK),
        )
        board.set_piece_at(
            squares[4],
            chess.Piece(rng.choice((chess.BISHOP, chess.KNIGHT)), chess.WHITE),
        )
        board.set_piece_at(
            squares[5],
            chess.Piece(rng.choice((chess.BISHOP, chess.KNIGHT)), chess.BLACK),
        )
        board.halfmove_clock = rng.randrange(0, 18)
        board.fullmove_number = rng.randrange(1, 20)
        if not board.is_valid() or board.is_game_over(claim_draw=False):
            continue
        state = ProgressiveState.from_fen(board.fen(), series_number)
        if state.pfen in seen:
            continue
        seen.add(state.pfen)
        states.append(state)
    return tuple(states)


def _series(item: object) -> tuple[object, ...]:
    return (
        item.machine_notation,
        item.san,
        item.final_state.pfen,
        item.ended_by_check,
        item.outcome,
        item.unused_moves,
        item.transposition_count,
    )


def _signature(result: object) -> tuple[object, ...]:
    return (
        result.score,
        _series(result.best_series) if result.best_series else None,
        tuple(_series(item) for item in result.principal_variation),
        tuple(
            (
                _series(item.series),
                item.score,
                tuple(_series(pv) for pv in item.principal_variation),
                item.proof_bounds,
                item.proof,
            )
            for item in result.alternatives
        ),
        result.requested_depth,
        result.completed_depth,
        result.exact_width,
        result.timed_out,
        result.work_limit_reached,
        result.root_scores_complete,
        result.proof,
        result.forced,
        result.adjudication_status,
        result.classification,
        result.confidence,
        asdict(result.stats),
    )


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
    baseline_seconds = 0.0
    candidate_seconds = 0.0
    by_series: list[dict[str, object]] = []
    try:
        for series_number in range(1, 9):
            series_mismatches = 0
            for index, state in enumerate(
                _states(series_number, args.count_per_series)
            ):
                search_module.NATIVE_SUBTREE_ENABLED = False
                started = time.perf_counter()
                baseline = analyze(state, limits, baseline_profile())
                baseline_seconds += time.perf_counter() - started
                search_module.NATIVE_SUBTREE_ENABLED = True
                started = time.perf_counter()
                candidate = analyze(state, limits, baseline_profile())
                candidate_seconds += time.perf_counter() - started
                if _signature(candidate) != _signature(baseline):
                    series_mismatches += 1
                    if len(mismatches) < 32:
                        baseline_stats = asdict(baseline.stats)
                        candidate_stats = asdict(candidate.stats)
                        mismatches.append(
                            {
                                "series": series_number,
                                "index": index,
                                "pfen": state.pfen,
                                "baseline": _signature(baseline)[:-1],
                                "candidate": _signature(candidate)[:-1],
                                "stat_differences": {
                                    key: (baseline_stats[key], candidate_stats[key])
                                    for key in baseline_stats
                                    if baseline_stats[key] != candidate_stats[key]
                                },
                            }
                        )
            by_series.append(
                {
                    "series": series_number,
                    "mismatches": series_mismatches,
                }
            )
    finally:
        search_module.NATIVE_SUBTREE_ENABLED = True
    print(
        json.dumps(
            {
                "count": args.count_per_series * 8,
                "depth": args.depth,
                "width": args.width,
                "mismatch_count": sum(
                    int(item["mismatches"]) for item in by_series
                ),
                "mismatches": mismatches,
                "baseline_seconds": baseline_seconds,
                "candidate_seconds": candidate_seconds,
                "speedup": baseline_seconds / candidate_seconds,
                "by_series": by_series,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
