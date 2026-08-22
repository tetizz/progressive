from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import random

import chess

import scottish_progressive.search as search_module
from scottish_progressive.model import ProgressiveState
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.search import SearchLimits, analyze


def _states(count: int, series_number: int | None = None) -> tuple[ProgressiveState, ...]:
    rng = random.Random(20260821)
    states: list[ProgressiveState] = []
    seen: set[str] = set()
    while len(states) < count:
        squares = rng.sample(list(chess.SQUARES), 6)
        board = chess.Board(None)
        if series_number is None:
            board.turn = chess.WHITE if len(states) % 2 == 0 else chess.BLACK
        else:
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
        if not board.is_valid() or board.is_game_over(claim_draw=False):
            continue
        state = ProgressiveState(board, 1 if board.turn == chess.WHITE else 2)
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
    parser.add_argument("--count", type=int, default=512)
    parser.add_argument("--detail-index", type=int)
    parser.add_argument("--series", type=int, choices=(1, 2))
    args = parser.parse_args()
    limits = SearchLimits(
        depth_series=4,
        max_series_per_node=4,
        collect_all_root_scores=False,
        native_threads=1,
    )
    baseline_work = 0
    candidate_work = 0
    root_probes = 0
    mismatches: list[dict[str, object]] = []
    headline_mismatches = 0
    pv_mismatches = 0
    alternative_mismatches = 0
    try:
        for index, state in enumerate(_states(args.count, args.series)):
            search_module.ROOT_PVS_ENABLED = False
            baseline = analyze(state, limits, baseline_profile())
            search_module.ROOT_PVS_ENABLED = True
            candidate = analyze(state, limits, baseline_profile())
            baseline_work += baseline.stats.work_positions
            candidate_work += candidate.stats.work_positions
            root_probes += candidate.stats.root_pvs_zero_window_searches
            baseline_signature = _signature(baseline)
            candidate_signature = _signature(candidate)
            if candidate_signature != baseline_signature:
                headline_changed = candidate_signature[:2] != baseline_signature[:2]
                pv_changed = candidate_signature[2] != baseline_signature[2]
                alternatives_changed = candidate_signature[3] != baseline_signature[3]
                headline_mismatches += int(headline_changed)
                pv_mismatches += int(pv_changed)
                alternative_mismatches += int(alternatives_changed)
                mismatches.append(
                    {
                        "index": index,
                        "pfen": state.pfen,
                        "headline_changed": headline_changed,
                        "pv_changed": pv_changed,
                        "alternatives_changed": alternatives_changed,
                    }
                )
                if args.detail_index == index:
                    print(
                        json.dumps(
                            {
                                "detail_index": index,
                                "baseline": baseline_signature,
                                "candidate": candidate_signature,
                                "baseline_stats": asdict(baseline.stats),
                                "candidate_stats": asdict(candidate.stats),
                            },
                            default=str,
                            sort_keys=True,
                        )
                    )
    finally:
        search_module.ROOT_PVS_ENABLED = True

    print(
        json.dumps(
            {
                "count": args.count,
                "mismatch_count": len(mismatches),
                "headline_mismatch_count": headline_mismatches,
                "pv_mismatch_count": pv_mismatches,
                "alternative_mismatch_count": alternative_mismatches,
                "mismatches": mismatches[:8],
                "baseline_work": baseline_work,
                "candidate_work": candidate_work,
                "work_delta": candidate_work - baseline_work,
                "work_percent": 100.0
                * (candidate_work - baseline_work)
                / baseline_work,
                "root_zero_window_searches": root_probes,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
