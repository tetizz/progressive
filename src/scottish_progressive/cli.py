from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import chess

from .database import TheoryDatabase
from .model import ENGINE_VERSION, RULESET_VERSION, ProgressiveState
from .notation import format_principal_variation, format_series_turn
from .rules import GenerationStats, generate_series
from .search import SearchLimits, analyze
from .theory import (
    compare_published_replies,
    deepen_initial_moves,
    rank_initial_moves,
    write_deepening,
    write_reply_comparison,
    write_ranking,
)


RULE_SOURCES = (
    "https://users.ics.aalto.fi/tho/chess.html",
    "https://users.ics.aalto.fi/tho/wipcc96final.html",
    "https://doi.org/10.1016/j.tcs.2016.06.028",
    "https://www.chessvariants.org/multimove.dir/progressive.html",
)


def _state_from_args(args: argparse.Namespace) -> ProgressiveState:
    fen = args.fen or chess.STARTING_FEN
    targets: list[int] = []
    if getattr(args, "progressive_ep", None):
        targets = [
            chess.parse_square(value.strip())
            for value in args.progressive_ep.split(",")
            if value.strip() and value.strip() != "-"
        ]
    return ProgressiveState.from_fen(
        fen,
        args.series,
        quiet_series=getattr(args, "quiet_series", 0),
        ep_targets=targets,
    )


def _print_rules(_: argparse.Namespace) -> int:
    print(f"Rules profile: {RULESET_VERSION}")
    print("- Series budgets are 1, 2, 3, ...; odd White, even Black.")
    print("- Any legal checking move ends the series immediately.")
    print("- A checked player must escape on the first move; a countercheck is legal.")
    print("- Running out of legal moves mid-series is progressive stalemate (draw).")
    print("- Multiple previous-series en-passant targets are supported on reply move 1.")
    print("- Castling is one move; promotion is immediate; promoted pieces may move again.")
    print("- Ten quiet series set a proof-required draw-adjudication flag; they do not erase an impending mate.")
    print("- No orthodox repetition draw: a different series budget is a different state.")
    print("Sources:")
    for source in RULE_SOURCES:
        print(f"  {source}")
    return 0


def _print_series(args: argparse.Namespace) -> int:
    state = _state_from_args(args)
    stats = GenerationStats()
    series = generate_series(
        state, merge_transpositions=not args.raw, stats=stats
    )
    shown = series[: args.limit] if args.limit is not None else series
    if args.json:
        print(
            json.dumps(
                {
                    "state": state.pfen,
                    "budget": state.moves_available,
                    "raw_series": stats.raw_series,
                    "unique_series": stats.unique_series,
                    "transpositions_merged": stats.transpositions_merged,
                    "series": [
                        {
                            "uci": item.machine_notation,
                            "notation": item.notation,
                            "ended_by_check": item.ended_by_check,
                            "unused_moves": item.unused_moves,
                            "outcome": item.outcome.value if item.outcome else None,
                            "transposition_count": item.transposition_count,
                            "pfen": item.final_state.pfen,
                        }
                        for item in shown
                    ],
                },
                indent=2,
            )
        )
    else:
        print(state.pfen)
        print(
            f"raw={stats.raw_series} unique={stats.unique_series} "
            f"merged={stats.transpositions_merged}"
        )
        for index, item in enumerate(shown, start=1):
            aliases = (
                f" (x{item.transposition_count} move orders)"
                if item.transposition_count > 1
                else ""
            )
            print(f"{index:>6}. {format_series_turn(state.series_number, item)}{aliases}")
    return 0


def _print_analysis(args: argparse.Namespace) -> int:
    state = _state_from_args(args)
    result = analyze(
        state,
        SearchLimits(
            depth_series=args.depth,
            max_series_per_node=args.max_series,
            time_limit_seconds=args.time_limit,
        ),
    )
    payload = {
        "engine_version": result.engine_version,
        "source_fingerprint": result.source_fingerprint,
        "state": state.pfen,
        "score": result.score,
        "classification": result.classification,
        "proven_result": result.forced,
        "confidence": result.confidence,
        "adjudication_status": result.adjudication_status,
        "requested_depth": result.requested_depth,
        "completed_depth": result.completed_depth,
        "exact_width": result.exact_width,
        "timed_out": result.timed_out,
        "max_series_per_node": result.max_series_per_node,
        "time_limit_seconds": result.time_limit_seconds,
        "elapsed_seconds": result.elapsed_seconds,
        "best_series": result.best_series.machine_notation if result.best_series else None,
        "best_notation": result.best_series.notation if result.best_series else None,
        "principal_variation": format_principal_variation(
            state.series_number, result.principal_variation
        ),
        "evaluation": result.root_evaluation.as_dict(),
        "stats": {
            "nodes": result.stats.nodes,
            "leaf_evaluations": result.stats.leaf_evaluations,
            "generated_raw_series": result.stats.generated_raw_series,
            "generated_unique_series": result.stats.generated_unique_series,
            "intra_series_transpositions": result.stats.intra_series_transpositions,
            "tt_hits": result.stats.tt_hits,
            "alpha_beta_cutoffs": result.stats.alpha_beta_cutoffs,
            "branch_caps": result.stats.branch_caps,
        },
        "alternatives": [
            {
                "series": item.series.machine_notation,
                "notation": item.series.notation,
                "score": item.score,
            }
            for item in result.alternatives[: args.alternatives]
        ],
    }
    print(json.dumps(payload, indent=2) if args.json else _analysis_text(payload))
    if args.database:
        with TheoryDatabase(args.database) as database:
            analysis_id = database.save_analysis(state, result)
        print(f"saved analysis #{analysis_id} to {Path(args.database).resolve()}")
    return 0


def _analysis_text(payload: dict[str, object]) -> str:
    return "\n".join(
        [
            f"score: {payload['score']:+d} ({payload['classification']})",
            f"confidence: {payload['confidence']}",
            f"depth: {payload['completed_depth']}/{payload['requested_depth']} series",
            f"best: {payload['best_notation'] or 'none'}",
            f"PV: {payload['principal_variation'] or 'none'}",
            f"elapsed: {payload['elapsed_seconds']:.3f}s",
            f"stats: {json.dumps(payload['stats'], sort_keys=True)}",
        ]
    )


def _rank_openings(args: argparse.Namespace) -> int:
    ranking = rank_initial_moves(
        reply_depth=args.reply_depth,
        max_series_per_node=args.max_series,
        time_limit_per_move=args.time_limit_per_move,
    )
    json_path, markdown_path = write_ranking(ranking, args.output_dir)
    for item in ranking.results:
        print(
            f"{item.rank:>2}. {item.move_san:<4} {item.score:+6d}  "
            f"{item.classification:<20}  {item.best_black_notation or '—'}"
        )
    print(f"JSON: {json_path.resolve()}")
    print(f"Report: {markdown_path.resolve()}")
    return 0


def _deepen_openings(args: argparse.Namespace) -> int:
    moves = tuple(value.strip() for value in args.moves.split(",") if value.strip())
    payload = deepen_initial_moves(
        moves,
        reply_depth=args.reply_depth,
        max_series_per_node=args.max_series,
        time_limit_per_move=args.time_limit_per_move,
    )
    json_path, markdown_path = write_deepening(payload, args.output_dir)
    for row in payload["results"]:
        print(
            f"{row['move_san']:<4} {row['score']:+6d}  {row['classification']:<20} "
            f"{row['principal_variation']}"
        )
    print(f"JSON: {json_path.resolve()}")
    print(f"Report: {markdown_path.resolve()}")
    return 0


def _compare_replies(args: argparse.Namespace) -> int:
    payload = compare_published_replies()
    json_path, markdown_path = write_reply_comparison(payload, args.output_dir)
    for opening in payload["openings"]:
        print(f"1.{opening['first_move_san']}")
        for row in opening["results"]:
            print(
                f"  {row['black_notation']:<18} "
                f"{row['score_after_best_white_response']:+6d}  "
                f"{row['best_white_notation']}"
            )
    print(f"JSON: {json_path.resolve()}")
    print(f"Report: {markdown_path.resolve()}")
    return 0


def _init_database(args: argparse.Namespace) -> int:
    with TheoryDatabase(args.path):
        pass
    print(Path(args.path).resolve())
    return 0


def _position_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fen", help="standard FEN at a series boundary")
    parser.add_argument("--series", type=int, required=True, help="series number/budget")
    parser.add_argument(
        "--progressive-ep",
        help="comma-separated progressive en-passant targets (for example d3,f3)",
    )
    parser.add_argument("--quiet-series", type=int, default=0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spc", description="Scottish Progressive Chess research engine"
    )
    parser.add_argument("--version", action="version", version=ENGINE_VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    rules = subparsers.add_parser("rules", help="show the active rules profile")
    rules.set_defaults(handler=_print_rules)

    series = subparsers.add_parser("series", help="generate complete legal series")
    _position_arguments(series)
    series.add_argument("--raw", action="store_true", help="do not merge transpositions")
    series.add_argument("--limit", type=int, help="display only the first N results")
    series.add_argument("--json", action="store_true")
    series.set_defaults(handler=_print_series)

    analysis = subparsers.add_parser("analyze", help="series-level alpha-beta analysis")
    _position_arguments(analysis)
    analysis.add_argument("--depth", type=int, default=1)
    analysis.add_argument("--max-series", type=int)
    analysis.add_argument("--time-limit", type=float)
    analysis.add_argument("--alternatives", type=int, default=5)
    analysis.add_argument("--database")
    analysis.add_argument("--json", action="store_true")
    analysis.set_defaults(handler=_print_analysis)

    ranking = subparsers.add_parser(
        "rank-openings", help="analyze all 20 legal first moves"
    )
    ranking.add_argument("--reply-depth", type=int, default=1)
    ranking.add_argument("--max-series", type=int)
    ranking.add_argument("--time-limit-per-move", type=float)
    ranking.add_argument("--output-dir", default="reports")
    ranking.set_defaults(handler=_rank_openings)

    deepening = subparsers.add_parser(
        "deepen-openings", help="selectively extend named first moves"
    )
    deepening.add_argument(
        "--moves", required=True, help="comma-separated initial UCI moves"
    )
    deepening.add_argument("--reply-depth", type=int, default=2)
    deepening.add_argument("--max-series", type=int, default=16)
    deepening.add_argument("--time-limit-per-move", type=float)
    deepening.add_argument("--output-dir", default="reports")
    deepening.set_defaults(handler=_deepen_openings)

    comparison = subparsers.add_parser(
        "compare-replies", help="exhaust White replies to published Black candidates"
    )
    comparison.add_argument("--output-dir", default="reports")
    comparison.set_defaults(handler=_compare_replies)

    database = subparsers.add_parser("init-db", help="create the SQLite theory schema")
    database.add_argument("path")
    database.set_defaults(handler=_init_database)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ValueError, chess.InvalidMoveError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    sys.exit(main())
