from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import chess

from .database import TheoryDatabase
from .model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    RULESET_VERSION,
    ProgressiveState,
)
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
        public_origin=args.public_origin,
    )
    if args.engine_profile:
        options["engine_profile"] = args.engine_profile
    return serve(**options)


def _league_run(args: argparse.Namespace) -> int:
    from .league import LeagueConfig, LeagueStore, run_league, runtime_provenance

    resume_run_id = args.resume
    if args.continue_latest:
        with LeagueStore(args.database) as store:
            latest = store.latest_run_id()
            if latest is not None:
                latest_row = store.run_row(latest)
                unfinished = latest_row["status"] in {"running", "needs-resume"}
                try:
                    persisted_runtime = json.loads(latest_row["runtime_json"])
                except (TypeError, json.JSONDecodeError):
                    persisted_runtime = None
                compatible = (
                    latest_row["source_fingerprint"] == ENGINE_SOURCE_FINGERPRINT
                    and persisted_runtime == runtime_provenance()
                )
                if unfinished and compatible:
                    resume_run_id = latest
                elif unfinished:
                    print(
                        f"latest unfinished run {latest} uses incompatible engine "
                        "source/runtime; starting a new run",
                        flush=True,
                    )

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
            fast_preselection_finalists=args.preselection_finalists,
            fast_preselection_positions=args.preselection_positions,
            fast_preselection_rollout_steps=args.preselection_rollout_steps,
            fast_preselection_smoke=False,
            promotion_games=args.promotion_games,
            max_replacement_games=args.max_replacement_games,
            minimum_promotion_games=args.minimum_promotion_games,
            search_depth=args.depth,
            max_series_per_node=args.branch_cap,
            max_generation_positions=args.max_generation_positions,
            max_game_work_positions=args.max_game_work_positions,
            emergency_max_series=args.emergency_max_series,
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


def _train_fast(args: argparse.Namespace) -> int:
    from .fast_training import FastTrainingConfig, run_fast_preselection
    from .profiles import baseline_profile, create_population

    champion = (
        load_profile(args.champion_profile)
        if args.champion_profile
        else baseline_profile()
    )
    config = (
        FastTrainingConfig.smoke_config(seed=args.seed)
        if args.smoke
        else FastTrainingConfig(
            position_limit=args.positions,
            rollout_steps=args.rollout_steps,
            label_depth_series=2,
            label_branch_cap=args.branch_cap,
            label_max_work_positions=args.max_work_positions,
            finalist_count=args.finalists,
            seed=args.seed,
            smoke=False,
        )
    )
    population = create_population(
        champion,
        size=args.population,
        seed=args.seed,
    )
    output_dir = Path(args.output_dir).expanduser().resolve()
    cache_path = output_dir / "training-cache.json"
    report_path = output_dir / "preselection-report.json"
    report, resumed = run_fast_preselection(
        population,
        champion,
        cache_path=cache_path,
        report_path=report_path,
        config=config,
        preliminary_games_per_pair=args.preliminary_games,
        promotion_games=args.promotion_games,
    )
    payload = {
        "mode": "smoke-wiring-only" if config.smoke else "full-corpus",
        "resumed": resumed,
        "cache_path": str(cache_path),
        "report_path": str(report_path),
        "report": report,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        performance = report["performance"]
        schedule = report["full_game_schedule"]
        print(f"Mode: {payload['mode']}")
        print(
            "Cached screening: "
            f"{performance['candidate_iterations_per_second']:.0f} profiles/s "
            "(position/short-rollout proxy; not WDL or strength evidence)"
        )
        print(
            f"Scheduled full games avoided: {schedule['games_avoided']}; "
            "full-game promotion gate changed: no"
        )
        finalists = report["finalist_profile_ids"]
        print(
            "Full-game test shortlist: "
            f"{', '.join(finalists) if finalists else 'none'}"
        )
        print(f"Report: {report_path}")
    return 0


def _fullgames_run(args: argparse.Namespace) -> int:
    from .fullgame import FullGameSemanticConfig, run_fullgame_generation
    from .profiles import baseline_profile

    if args.profile_pool:
        profiles = tuple(load_profile(path) for path in args.profile_pool)
    elif args.profile:
        profiles = (load_profile(args.profile),)
    else:
        profiles = (baseline_profile(),)
    config = FullGameSemanticConfig.from_profiles(
        profiles,
        seed=args.seed,
        max_attempt_series=args.technical_max_series,
        max_frontier_states=args.frontier_cap,
        max_positions_per_series=args.max_positions_per_series,
        max_positions_per_game=args.max_positions_per_game,
        candidate_count=args.candidates,
        backend_kind=args.backend,
    )

    def show_progress(payload: dict[str, object]) -> None:
        print(
            "full games "
            f"{payload['accepted_unique_games']}/{payload['target_unique_games']} | "
            f"attempts {payload['attempts_committed']} | "
            f"unique/s {float(payload['accepted_unique_games_per_second']):.2f}",
            flush=True,
        )

    result = run_fullgame_generation(
        args.root,
        config,
        target_unique_games=args.target,
        attempts_per_chunk=args.attempts_per_chunk,
        backend=args.backend,
        max_attempts=args.max_attempts,
        requested_workers=args.workers,
        memory_per_worker_mb=args.memory_per_worker_mb,
        reserve_memory_mb=args.reserve_memory_mb,
        progress=None if args.json else show_progress,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"Stored {result['accepted_unique_games']} globally unique, "
            "replay-verified terminal games."
        )
        print(
            f"Measured this run: {result['accepted_unique_games_per_second']:.2f} "
            "accepted unique games/s; "
            f"{result['committed_attempts_per_second']:.2f} committed attempts/s."
        )
        print(
            f"Rejected: {result['native_or_policy_rejects']}; "
            f"duplicate traces: {result['duplicate_traces']}."
        )
        print(
            "Scope: exploration rollout data, not champion play; "
            f"{len(config.profile_pool)} immutable profile(s) use "
            f"{config.profile_schedule_id}."
        )
        print(f"Run directory: {Path(args.root).expanduser().resolve()}")
    return 0


def _fullgames_status(args: argparse.Namespace) -> int:
    from .fullgame import fullgame_status

    print(json.dumps(fullgame_status(args.root), indent=2, sort_keys=True))
    return 0


def _fullgames_verify(args: argparse.Namespace) -> int:
    from .fullgame import verify_fullgame_run

    print(json.dumps(verify_fullgame_run(args.root), indent=2, sort_keys=True))
    return 0


def _fullgames_export(args: argparse.Namespace) -> int:
    from .fullgame import export_fullgame_jsonl

    result = export_fullgame_jsonl(args.root, args.destination, limit=args.limit)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _challengers_preflight(args: argparse.Namespace) -> int:
    from .challengers import preflight_challengers

    try:
        payload = preflight_challengers(
            args.run_root,
            fullgame_store=args.fullgame_store,
            profile_path=args.profile,
            batch_registry=args.batch_registry,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    except RuntimeError as error:
        raise ValueError(str(error)) from error
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _challengers_run(args: argparse.Namespace) -> int:
    from .challengers import run_challengers

    try:
        payload = run_challengers(
            args.run_root,
            fullgame_store=args.fullgame_store,
            profile_path=args.profile,
            batch_registry=args.batch_registry,
            checkpoint_every=args.checkpoint_every,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    except RuntimeError as error:
        raise ValueError(str(error)) from error
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _challengers_status(args: argparse.Namespace) -> int:
    from .challengers import challenger_status

    try:
        payload = challenger_status(
            args.run_root,
            fullgame_store=args.fullgame_store,
            profile_path=args.profile,
            batch_registry=args.batch_registry,
        )
    except RuntimeError as error:
        raise ValueError(str(error)) from error
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _challengers_abandon(args: argparse.Namespace) -> int:
    from .challengers import abandon_challenger_batch

    payload = abandon_challenger_batch(
        args.run_root,
        batch_registry=args.batch_registry,
        reason=args.reason,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _train_selfplay(args: argparse.Namespace) -> int:
    from .profiles import baseline_profile, save_profile
    from .selfplay_training import (
        build_selfplay_corpus,
        tune_selfplay_profile,
        write_selfplay_artifact,
    )

    parent = (
        load_profile(args.parent_profile)
        if args.parent_profile
        else baseline_profile()
    )
    corpus = build_selfplay_corpus(
        args.databases,
        seed=args.seed,
        holdout_percent=args.holdout_percent,
    )
    candidate, tuning = tune_selfplay_profile(
        corpus,
        parent,
        name=args.candidate_name,
        regularization=args.regularization,
    )
    output_dir = Path(args.output_dir).expanduser().resolve()
    corpus_path = write_selfplay_artifact(
        corpus.as_dict(), output_dir / "selfplay-corpus.json"
    )
    tuning_path = write_selfplay_artifact(
        {
            **tuning,
            "candidate": candidate.as_dict(),
            "corpus_summary": corpus.as_dict()["summary"],
        },
        output_dir / "selfplay-tuning-report.json",
    )
    candidate_path = save_profile(candidate, output_dir / "candidate-profile.json")
    payload = {
        "corpus_id": corpus.corpus_id,
        "completed_games": corpus.completed_games,
        "excluded_games": corpus.excluded_games,
        "samples": len(corpus.samples),
        "train_samples": len(corpus.train_samples),
        "holdout_samples": len(corpus.holdout_samples),
        "parent_profile_id": parent.profile_id,
        "candidate_profile_id": candidate.profile_id,
        "candidate_weights": candidate.as_dict()["weights"],
        "baseline_train_loss": tuning["baseline_train_loss"],
        "candidate_train_loss": tuning["candidate_train_loss"],
        "baseline_holdout_loss": tuning["baseline_holdout_loss"],
        "candidate_holdout_loss": tuning["candidate_holdout_loss"],
        "corpus_path": str(corpus_path),
        "tuning_report_path": str(tuning_path),
        "candidate_path": str(candidate_path),
        "claim_scope": tuning["claim_scope"],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"Replayed {corpus.completed_games} conclusive games into "
            f"{len(corpus.samples)} verified boundary samples; "
            f"excluded {corpus.excluded_games} manual/technical games."
        )
        print(
            "Value loss train: "
            f"{tuning['baseline_train_loss']:.6f} -> "
            f"{tuning['candidate_train_loss']:.6f}"
        )
        if tuning["baseline_holdout_loss"] is not None:
            print(
                "Value loss holdout: "
                f"{tuning['baseline_holdout_loss']:.6f} -> "
                f"{tuning['candidate_holdout_loss']:.6f}"
            )
        print(
            f"Candidate: {candidate.profile_id} (unpromoted; tactical and "
            "fixed-suite match gates still required)"
        )
        print(f"Corpus: {corpus_path}")
        print(f"Tuning report: {tuning_path}")
        print(f"Candidate profile: {candidate_path}")
    return 0


def _strength_match(args: argparse.Namespace) -> int:
    from .strength import (
        StrengthMatchConfig,
        build_seeded_opening_suite,
        resolve_match_profile,
        run_strength_match,
        write_strength_report,
    )

    candidate = resolve_match_profile(args.candidate)
    reference = resolve_match_profile(args.reference)
    seeded_suite = None
    opening_case_ids = None
    opening_suite_version = None
    if args.seeded_openings is not None:
        seeded_suite = build_seeded_opening_suite(
            seed=args.seed,
            count=args.seeded_openings,
            min_series=args.seeded_min_series,
            max_series=args.seeded_max_series,
            max_frontier_states=args.seeded_frontier_cap,
        )
        opening_case_ids = tuple(case.case_id for case in seeded_suite.cases)
        opening_suite_version = seeded_suite.version
        if args.pairs > len(opening_case_ids):
            raise ValueError(
                "--pairs cannot exceed the number of generated seeded openings"
            )
    config_options: dict[str, object] = {}
    if opening_case_ids is not None and opening_suite_version is not None:
        config_options.update(
            opening_case_ids=opening_case_ids,
            opening_suite_version=opening_suite_version,
        )
    config = StrengthMatchConfig(
        pairs=args.pairs,
        seed=args.seed,
        search_depth=args.depth,
        max_series_per_node=args.branch_cap,
        max_generation_positions=args.max_generation_positions,
        max_game_work_positions=args.max_game_work_positions,
        emergency_max_series=args.emergency_max_series,
        **config_options,
    )
    progress = None if args.json else (lambda message: print(message, flush=True))
    match_options: dict[str, object] = {}
    if seeded_suite is not None:
        match_options["opening_cases"] = seeded_suite
    report = run_strength_match(
        candidate,
        reference,
        config=config,
        requested_workers=args.workers,
        memory_per_worker_mb=args.memory_per_worker_mb,
        reserve_memory_mb=args.reserve_memory_mb,
        progress=progress,
        **match_options,
    )
    output = write_strength_report(report, args.output) if args.output else None
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        summary = report["summary"]
        game_wdl = summary["candidate_game_wdl"]
        pair_wdl = summary["candidate_pair_wdl"]
        failures = summary["technical_failures"]
        estimate = summary["fixed_suite_performance_difference"]
        estimate_text = (
            f"{estimate['value']:+d} descriptive Elo-like points"
            if estimate["value"] is not None
            else estimate["status"]
        )
        print(f"Candidate: {candidate.name} ({candidate.profile_id})")
        print(f"Reference: {reference.name} ({reference.profile_id})")
        print(
            "Games W/D/L: "
            f"{game_wdl['wins']}/{game_wdl['draws']}/{game_wdl['losses']} "
            f"({summary['incomplete_games']} incomplete)"
        )
        print(
            "Pairs W/D/L: "
            f"{pair_wdl['wins']}/{pair_wdl['draws']}/{pair_wdl['losses']} "
            f"({summary['incomplete_pairs']} incomplete)"
        )
        print(
            "Technical failures: "
            f"candidate {failures['candidate']}, reference {failures['reference']}, "
            f"worker {failures['unattributed_worker_failures']}, "
            f"match-limit {failures.get('unattributed_match_limit_failures', 0)}"
        )
        print(f"Fixed-suite estimate: {estimate_text}")
        print(report["claim_scope"]["stockfish_comparison"])
    if output is not None and not args.json:
        print(f"Report: {output}")
    return 0


def _external_match(args: argparse.Namespace) -> int:
    from .external import BucephalusSpec
    from .external_match import (
        ExternalMatchConfig,
        run_external_match,
        write_external_match_report,
    )
    from .strength import resolve_match_profile

    local_profile = resolve_match_profile(args.local_profile)
    spec = BucephalusSpec(
        Path(args.executable),
        args.sha256,
        upstream_commit=args.upstream_commit,
    )
    config = ExternalMatchConfig(
        pairs=args.pairs,
        seed=args.seed,
        local_depth_series=args.depth,
        local_max_series_per_node=args.branch_cap,
        local_max_generation_positions=args.max_generation_positions,
        local_max_game_work_positions=args.max_game_work_positions,
        external_lookahead_micro_plies=args.external_lookahead,
        external_wall_timeout_seconds=args.external_timeout,
        emergency_max_series=args.emergency_max_series,
    )
    progress = None if args.json else (lambda message: print(message, flush=True))
    report = run_external_match(
        local_profile,
        spec,
        config=config,
        requested_workers=args.workers,
        memory_per_worker_mb=args.memory_per_worker_mb,
        reserve_memory_mb=args.reserve_memory_mb,
        progress=progress,
    )
    output = (
        write_external_match_report(report, args.output) if args.output else None
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        summary = report["summary"]
        print(f"Local profile: {local_profile.name} ({local_profile.profile_id})")
        print(
            "Local games W/D/L: "
            f"{summary['local_game_wdl']['wins']}/"
            f"{summary['local_game_wdl']['draws']}/"
            f"{summary['local_game_wdl']['losses']}"
        )
        print(report["claim_scope"]["warning"])
        if output is not None:
            print(f"Report: {output}")
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
        choices=("127.0.0.1", "0.0.0.0"),
        help="bind interface; 0.0.0.0 requires --public-origin",
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
    web.add_argument(
        "--public-origin",
        help=(
            "explicit https:// host for a bounded public deployment; disables database "
            "access and lowers compute limits"
        ),
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
    league_run.add_argument(
        "--preselection-finalists",
        type=int,
        default=3,
        help="cached-proxy finalists eligible for the unchanged full-game gate",
    )
    league_run.add_argument(
        "--preselection-positions",
        type=int,
        default=32,
        help="versioned Scottish boundary traces cached for fast profile screening",
    )
    league_run.add_argument(
        "--preselection-rollout-steps",
        type=int,
        default=2,
        help="bounded cached positions per trace; proxy points are not WDL",
    )
    league_run.add_argument("--promotion-games", type=int, default=20)
    league_run.add_argument("--max-replacement-games", type=int, default=40)
    league_run.add_argument("--minimum-promotion-games", type=int, default=20)
    league_run.add_argument("--depth", type=int, default=2)
    league_run.add_argument(
        "--branch-cap",
        "--max-series",
        dest="branch_cap",
        type=int,
        default=32,
        help="complete-series candidates retained per search node",
    )
    league_run.add_argument(
        "--max-work-positions-per-search",
        "--max-generation-positions",
        dest="max_generation_positions",
        type=int,
        default=250000,
        help="logical work per search across generation, reach, and adjudication",
    )
    league_run.add_argument(
        "--max-game-work-positions",
        type=int,
        default=None,
        help="optional whole-game technical budget; default plays without one",
    )
    league_run.add_argument(
        "--emergency-max-series",
        type=int,
        default=None,
        help="optional technical watchdog; default is unbounded by series number",
    )
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

    fullgames = subparsers.add_parser(
        "fullgames",
        help="generate, resume, verify, or export complete terminal self-play games",
    )
    fullgame_commands = fullgames.add_subparsers(
        dest="fullgames_command", required=True
    )
    fullgames_run = fullgame_commands.add_parser(
        "run",
        help="run a checkpointed S1-to-terminal full-game generator",
    )
    fullgames_run.add_argument("root", help="run directory for chunks and checkpoint")
    fullgames_run.add_argument(
        "--target",
        type=int,
        default=10_000,
        help="globally unique replay-verified terminal games to retain",
    )
    fullgames_run.add_argument(
        "--attempts-per-chunk",
        type=int,
        default=64,
        help="bounded execution batch size; safe to change when resuming",
    )
    fullgames_run.add_argument(
        "--backend",
        choices=("native", "reference"),
        default="native",
        help="native for throughput; reference is the slow correctness oracle",
    )
    fullgames_profiles = fullgames_run.add_mutually_exclusive_group()
    fullgames_profiles.add_argument("--profile", help="one immutable profile JSON")
    fullgames_profiles.add_argument(
        "--profile-pool",
        nargs="+",
        metavar="PROFILE",
        help=(
            "immutable profile JSONs; multiple profiles use a fair ordered-pair "
            "color schedule"
        ),
    )
    fullgames_run.add_argument("--seed", type=int, default=20260820)
    fullgames_run.add_argument("--frontier-cap", type=int, default=8)
    fullgames_run.add_argument("--candidates", type=int, default=8)
    fullgames_run.add_argument(
        "--max-positions-per-series", type=int, default=5_000
    )
    fullgames_run.add_argument(
        "--max-positions-per-game",
        type=int,
        default=5_000_000,
        help="finite logical-work safety cap; exhaustion is rejected, never a result",
    )
    fullgames_run.add_argument(
        "--technical-max-series",
        type=int,
        default=0,
        help="0 is unbounded; nonzero exhaustion is rejected, never a draw",
    )
    fullgames_run.add_argument(
        "--max-attempts",
        type=int,
        help="stop this invocation after at most this many committed attempts",
    )
    fullgames_run.add_argument(
        "--workers",
        type=int,
        help="requested native workers; default uses the detected CPU/RAM envelope",
    )
    fullgames_run.add_argument("--memory-per-worker-mb", type=int, default=512)
    fullgames_run.add_argument("--reserve-memory-mb", type=int, default=1024)
    fullgames_run.add_argument("--json", action="store_true")
    fullgames_run.set_defaults(handler=_fullgames_run)

    fullgames_status_parser = fullgame_commands.add_parser(
        "status", help="show the last atomically published checkpoint status"
    )
    fullgames_status_parser.add_argument("root")
    fullgames_status_parser.set_defaults(handler=_fullgames_status)

    fullgames_verify_parser = fullgame_commands.add_parser(
        "verify", help="replay every retained game and audit chunks/checkpoint"
    )
    fullgames_verify_parser.add_argument("root")
    fullgames_verify_parser.set_defaults(handler=_fullgames_verify)

    fullgames_export_parser = fullgame_commands.add_parser(
        "export", help="atomically export replayable full games as JSON Lines"
    )
    fullgames_export_parser.add_argument("root")
    fullgames_export_parser.add_argument("destination")
    fullgames_export_parser.add_argument("--limit", type=int)
    fullgames_export_parser.set_defaults(handler=_fullgames_export)

    challenger_inputs = argparse.ArgumentParser(add_help=False)
    challenger_inputs.add_argument(
        "run_root", help="directory for the sealed funnel and tournament checkpoints"
    )
    challenger_inputs.add_argument(
        "--fullgame-store",
        required=True,
        help="completed replay-verified full-game store used for proxy data/exclusions",
    )
    challenger_inputs.add_argument(
        "--profile",
        required=True,
        help="immutable current-champion EngineProfile JSON used as the baseline",
    )
    challenger_inputs.add_argument(
        "--batch-registry",
        required=True,
        help="trusted chronological promotion-batch registry",
    )
    challengers = subparsers.add_parser(
        "challengers",
        help=(
            "preflight or resume the 2^22 cached challenger funnel and frozen "
            "full-game tournament; never writes a champion"
        ),
    )
    challenger_commands = challengers.add_subparsers(
        dest="challengers_command", required=True
    )
    challengers_preflight = challenger_commands.add_parser(
        "preflight",
        parents=[challenger_inputs],
        help="verify and seal the corpus, profile, source, runtime, and proxy cache",
    )
    challengers_preflight.set_defaults(handler=_challengers_preflight)
    challengers_run = challenger_commands.add_parser(
        "run",
        parents=[challenger_inputs],
        help=(
            "resume all cached cuts, live tactical gates, and the canonical "
            "first-1000/expansion/full tournament"
        ),
    )
    challengers_run.add_argument(
        "--checkpoint-every",
        type=int,
        default=65_536,
        help="Stage-A candidates per atomic checkpoint; safe to change on resume",
    )
    challengers_run.set_defaults(handler=_challengers_run)
    challengers_status_parser = challenger_commands.add_parser(
        "status",
        parents=[challenger_inputs],
        help="validate sealed input identities and show funnel/tournament progress",
    )
    challengers_status_parser.set_defaults(handler=_challengers_status)
    challengers_abandon_parser = challenger_commands.add_parser(
        "abandon",
        help=(
            "consume a source-stale or invalid-opening-plan alpha batch with "
            "a sealed no-promotion decision"
        ),
    )
    challengers_abandon_parser.add_argument("run_root")
    challengers_abandon_parser.add_argument(
        "--batch-registry", required=True, help="trusted chronological batch registry"
    )
    challengers_abandon_parser.add_argument(
        "--reason",
        required=True,
        choices=("source-stale", "invalid-opening-plan"),
    )
    challengers_abandon_parser.set_defaults(handler=_challengers_abandon)

    train_fast = subparsers.add_parser(
        "train-fast",
        help=(
            "rank profile mutations through cached Scottish position proxies; "
            "does not replace full-game promotion"
        ),
    )
    train_fast.add_argument(
        "output_dir", help="directory for resumable cache and preselection report"
    )
    train_fast.add_argument("--champion-profile")
    train_fast.add_argument("--population", type=int, default=10)
    train_fast.add_argument("--seed", type=int, default=20260820)
    train_fast.add_argument("--finalists", type=int, default=3)
    train_fast.add_argument(
        "--positions",
        type=int,
        default=32,
        help="full mode requires 30 v4 boundaries plus two tactical anchors",
    )
    train_fast.add_argument("--rollout-steps", type=int, default=2)
    train_fast.add_argument("--branch-cap", type=int, default=8)
    train_fast.add_argument("--max-work-positions", type=int, default=200000)
    train_fast.add_argument("--preliminary-games", type=int, default=10)
    train_fast.add_argument("--promotion-games", type=int, default=20)
    train_fast.add_argument(
        "--smoke",
        action="store_true",
        help="explicit four-position wiring preset; never strength evidence",
    )
    train_fast.add_argument("--json", action="store_true")
    train_fast.set_defaults(handler=_train_fast)

    train_selfplay = subparsers.add_parser(
        "train-selfplay",
        help=(
            "replay completed league databases and fit an unpromoted "
            "value-evaluation candidate"
        ),
    )
    train_selfplay.add_argument(
        "output_dir", help="directory for corpus, tuning report, and candidate profile"
    )
    train_selfplay.add_argument(
        "databases", nargs="+", help="one or more completed league SQLite databases"
    )
    train_selfplay.add_argument("--parent-profile")
    train_selfplay.add_argument("--candidate-name", default="self-play Texel candidate")
    train_selfplay.add_argument("--seed", type=int, default=20260820)
    train_selfplay.add_argument("--holdout-percent", type=int, default=20)
    train_selfplay.add_argument("--regularization", type=float, default=0.02)
    train_selfplay.add_argument("--json", action="store_true")
    train_selfplay.set_defaults(handler=_train_selfplay)

    strength = subparsers.add_parser(
        "strength-match",
        help="compare two profiles on isolated color-swapped fixed-suite pairs",
    )
    strength.add_argument(
        "candidate", help="candidate EngineProfile JSON/envelope, or 'baseline'"
    )
    strength.add_argument(
        "reference", help="reference EngineProfile JSON/envelope, or 'baseline'"
    )
    strength.add_argument("--pairs", type=int, default=10)
    strength.add_argument("--seed", type=int, default=20260820)
    strength.add_argument(
        "--seeded-openings",
        type=int,
        help=(
            "generate this many neutral replayable openings instead of using "
            "the built-in fixed suite"
        ),
    )
    strength.add_argument("--seeded-min-series", type=int, default=3)
    strength.add_argument("--seeded-max-series", type=int, default=6)
    strength.add_argument("--seeded-frontier-cap", type=int, default=32)
    strength.add_argument("--depth", type=int, default=2)
    strength.add_argument(
        "--branch-cap",
        "--max-series",
        dest="branch_cap",
        type=int,
        default=32,
        help="complete-series candidates retained per search node",
    )
    strength.add_argument(
        "--max-work-positions-per-search",
        "--max-generation-positions",
        dest="max_generation_positions",
        type=int,
        default=250000,
        help="logical work per search across generation, reach, and adjudication",
    )
    strength.add_argument(
        "--max-game-work-positions",
        type=int,
        default=5000000,
        help="whole-match logical work; exhaustion is incomplete '*', never a result",
    )
    strength.add_argument(
        "--emergency-max-series",
        type=int,
        default=None,
        help="optional technical watchdog; default is unbounded by series number",
    )
    strength.add_argument(
        "--workers",
        type=int,
        help="requested workers; clamped to detected CPU/RAM envelope and game count",
    )
    strength.add_argument("--memory-per-worker-mb", type=int, default=512)
    strength.add_argument("--reserve-memory-mb", type=int, default=512)
    strength.add_argument("--output", help="write the complete JSON report atomically")
    strength.add_argument("--json", action="store_true", help="print complete JSON")
    strength.set_defaults(handler=_strength_match)

    external_match = subparsers.add_parser(
        "external-match",
        help="run an isolated color-swapped match against a pinned Bucephalus binary",
    )
    external_match.add_argument(
        "local_profile", help="local EngineProfile JSON/envelope, or 'baseline'"
    )
    external_match.add_argument("executable", help="user-supplied Bucephalus executable")
    external_match.add_argument(
        "--sha256", required=True, help="required 64-hex executable fingerprint"
    )
    external_match.add_argument("--upstream-commit")
    external_match.add_argument("--pairs", type=int, default=10)
    external_match.add_argument("--seed", type=int, default=20260820)
    external_match.add_argument("--depth", type=int, default=2)
    external_match.add_argument("--branch-cap", type=int, default=32)
    external_match.add_argument(
        "--max-generation-positions", type=int, default=250000
    )
    external_match.add_argument(
        "--max-game-work-positions", type=int, default=5000000
    )
    external_match.add_argument(
        "--external-lookahead", type=int, default=0,
        help="fixed extra Bucephalus micro-ply depth beyond the series number",
    )
    external_match.add_argument(
        "--external-timeout", type=float, default=10.0,
        help="wall watchdog per external call; timeout is incomplete, never a result",
    )
    external_match.add_argument("--emergency-max-series", type=int, default=18)
    external_match.add_argument("--workers", type=int)
    external_match.add_argument("--memory-per-worker-mb", type=int, default=768)
    external_match.add_argument("--reserve-memory-mb", type=int, default=512)
    external_match.add_argument("--output")
    external_match.add_argument("--json", action="store_true")
    external_match.set_defaults(handler=_external_match)
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
