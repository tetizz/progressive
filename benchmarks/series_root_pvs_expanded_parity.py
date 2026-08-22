from __future__ import annotations

import argparse
import json
import random

import chess

import scottish_progressive.search as search_module
from scottish_progressive.model import ProgressiveState
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.search import SearchLimits, analyze


def _states(series_number: int, count: int) -> tuple[ProgressiveState, ...]:
    rng = random.Random(20260821 + series_number)
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
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count-per-series", type=int, default=32)
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument("--width", type=int, default=4)
    args = parser.parse_args()
    limits = SearchLimits(
        depth_series=args.depth,
        max_series_per_node=args.width,
        collect_all_root_scores=False,
        native_threads=1,
    )
    summaries: list[dict[str, object]] = []
    all_mismatches: list[dict[str, object]] = []
    try:
        for series_number in range(1, 9):
            baseline_work = 0
            candidate_work = 0
            probes = 0
            mismatches: list[dict[str, object]] = []
            for index, state in enumerate(
                _states(series_number, args.count_per_series)
            ):
                search_module.ROOT_PVS_ENABLED = False
                baseline = analyze(state, limits, baseline_profile())
                search_module.ROOT_PVS_ENABLED = True
                candidate = analyze(state, limits, baseline_profile())
                baseline_work += baseline.stats.work_positions
                candidate_work += candidate.stats.work_positions
                probes += candidate.stats.root_pvs_zero_window_searches
                if _signature(candidate) != _signature(baseline):
                    mismatch = {
                        "series": series_number,
                        "index": index,
                        "pfen": state.pfen,
                    }
                    mismatches.append(mismatch)
                    all_mismatches.append(mismatch)
            summaries.append(
                {
                    "series": series_number,
                    "mismatches": len(mismatches),
                    "baseline_work": baseline_work,
                    "candidate_work": candidate_work,
                    "work_delta": candidate_work - baseline_work,
                    "work_percent": 100.0
                    * (candidate_work - baseline_work)
                    / baseline_work,
                    "root_zero_window_searches": probes,
                }
            )
    finally:
        search_module.ROOT_PVS_ENABLED = True

    baseline_total = sum(int(row["baseline_work"]) for row in summaries)
    candidate_total = sum(int(row["candidate_work"]) for row in summaries)
    print(
        json.dumps(
            {
                "count_per_series": args.count_per_series,
                "count": args.count_per_series * 8,
                "mismatch_count": len(all_mismatches),
                "mismatches": all_mismatches[:16],
                "baseline_work": baseline_total,
                "candidate_work": candidate_total,
                "work_delta": candidate_total - baseline_total,
                "work_percent": 100.0
                * (candidate_total - baseline_total)
                / baseline_total,
                "by_series": summaries,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
