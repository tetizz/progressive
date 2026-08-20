from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import chess

from .database import TheoryDatabase
from .model import ENGINE_VERSION, RULESET_VERSION, ProgressiveState
from .notation import format_principal_variation, format_series_turn
from .profiles import load_profile
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
    profile = load_profile(args.engine_profile) if args.engine_profile else None
    result = analyze(
        state,
        SearchLimits(
            depth_series=args.depth,
            max_series_per_node=args.max_series,
            max_generation_positions=args.max_generation_positions,
            time_limit_seconds=args.time_limit,
        ),
        profile=profile,
    )
    payload = {
        "engine_version": result.engine_version,
        "source_fingerprint": result.source_fingerprint,
        "engine_profile_id": result.engine_profile_id,
        "engine_profile_name": result.engine_profile_name,
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


def _serve_web(args: argparse.Namespace) -> int:
    # Lazy import keeps non-web CLI startup and library imports lightweight.
    from .webapp import serve

    options: dict[str, object] = dict(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        database=args.database,
    )
    if args.engine_profile:
        options["engine_profile"] = args.engine_profile
    return serve(**options)


def _league_run(args: argparse.Namespace) -> int:
    from .league import LeagueConfig, LeagueStore, run_league

    resume_run_id = args.resume
    if args.continue_latest:
        with LeagueStore(args.database) as store:
            latest = store.latest_run_id()
            if latest is not None and store.run_row(latest)["status"] in {
                "running",
                "needs-resume",
            }:
                resume_run_id = latest

    if resume_run_id:
        config = None
    elif args.smoke:
        config = LeagueConfig.smoke(seed=args.seed if args.seed is not None else 7)
    else:
        config = LeagueConfig(
            population_size=args.population,
            generations=args.generations,
            seed=args.seed if args.seed is not None else 20260820,
            preliminary_games_per_pair=args.preliminary_games,
            promotion_games=args.promotion_games,
            max_replacement_games=args.max_replacement_games,
            minimum_promotion_games=args.minimum_promotion_games,
            search_depth=args.depth,
            max_series_per_node=args.max_series,
            max_generation_positions=args.max_generation_positions,
            max_game_series=args.max_game_series,
            requested_workers=args.workers,
            memory_per_worker_mb=args.memory_per_worker_mb,
            reserve_memory_mb=args.reserve_memory_mb,
        )
    champion_output = args.champion_output or str(
        Path(args.database).with_suffix(".champion.json")
    )
    status = run_league(
        args.database,
        config=config,
        resume_run_id=resume_run_id,
        initial_champion=args.champion_profile,
        champion_output=champion_output,
        progress=lambda message: print(message, flush=True),
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    print(f"Champion profile: {Path(champion_output).resolve()}")
    return 0


def _league_status(args: argparse.Namespace) -> int:
    from .league import league_status

    payload = league_status(args.database, args.run_id)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _league_resources(args: argparse.Namespace) -> int:
    from .resources import detect_resource_budget

    budget = detect_resource_budget(
        args.workers,
        memory_per_worker_mb=args.memory_per_worker_mb,
        reserve_memory_mb=args.reserve_memory_mb,
    )
    print(json.dumps(budget.as_dict(), indent=2, sort_keys=True))
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
    analysis.add_argument(
        "--max-generation-positions",
        type=int,
        help="deterministic series-generation work budget",
    )
    analysis.add_argument("--time-limit", type=float)
    analysis.add_argument("--alternatives", type=int, default=5)
    analysis.add_argument("--database")
    analysis.add_argument(
        "--engine-profile",
        help="single promoted champion profile JSON used by the shared search core",
    )
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

    web = subparsers.add_parser(
        "web", help="run the local interactive analysis board"
    )
    web.add_argument(
        "--host",
        default="127.0.0.1",
        choices=("127.0.0.1",),
        help="loopback interface (remote binding is intentionally disabled)",
    )
    web.add_argument("--port", type=int, default=8765)
    web.add_argument(
        "--no-browser", action="store_true", help="do not open the board automatically"
    )
    web.add_argument(
        "--database", help="SQLite theory database used when the UI requests Save"
    )
    web.add_argument(
        "--engine-profile",
        help="promoted champion JSON used as the board's single analysis engine",
    )
    web.set_defaults(handler=_serve_web)

    league = subparsers.add_parser(
        "league", help="run or inspect deterministic evolutionary self-play"
    )
    league_commands = league.add_subparsers(dest="league_command", required=True)

    league_run = league_commands.add_parser(
        "run", help="start or resume a checkpointed local league"
    )
    league_run.add_argument("database", help="league SQLite database")
    resume_group = league_run.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", metavar="RUN_ID")
    resume_group.add_argument(
        "--continue-latest",
        action="store_true",
        help="resume the latest unfinished run, otherwise start a new run",
    )
    league_run.add_argument("--champion-profile", help="initial champion profile JSON")
    league_run.add_argument(
        "--champion-output",
        help="published champion JSON (default: database name with .champion.json)",
    )
    league_run.add_argument("--population", type=int, default=10)
    league_run.add_argument("--generations", type=int, default=2)
    league_run.add_argument("--seed", type=int)
    league_run.add_argument("--preliminary-games", type=int, default=10)
    league_run.add_argument("--promotion-games", type=int, default=20)
    league_run.add_argument("--max-replacement-games", type=int, default=40)
    league_run.add_argument("--minimum-promotion-games", type=int, default=20)
    league_run.add_argument("--depth", type=int, default=2)
    league_run.add_argument("--max-series", type=int, default=32)
    league_run.add_argument("--max-generation-positions", type=int, default=250000)
    league_run.add_argument("--max-game-series", type=int, default=12)
    league_run.add_argument(
        "--workers",
        type=int,
        help="requested workers; omitted uses the detected CPU and estimated RAM envelope",
    )
    league_run.add_argument("--memory-per-worker-mb", type=int, default=512)
    league_run.add_argument("--reserve-memory-mb", type=int, default=512)
    league_run.add_argument(
        "--smoke",
        action="store_true",
        help="two-bot wiring check; deliberately cannot promote a champion",
    )
    league_run.set_defaults(handler=_league_run)

    league_status_parser = league_commands.add_parser(
        "status", help="show persisted games, matches, and champion"
    )
    league_status_parser.add_argument("database")
    league_status_parser.add_argument("--run-id")
    league_status_parser.set_defaults(handler=_league_status)

    league_resources = league_commands.add_parser(
        "resources", help="show the detected CPU and estimated RAM envelope"
    )
    league_resources.add_argument("--workers", type=int)
    league_resources.add_argument("--memory-per-worker-mb", type=int, default=512)
    league_resources.add_argument("--reserve-memory-mb", type=int, default=512)
    league_resources.set_defaults(handler=_league_resources)
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
