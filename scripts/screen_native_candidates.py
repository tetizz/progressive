from __future__ import annotations

import argparse
import json
from pathlib import Path

from scottish_progressive.league import run_rules_tactical_gate
from scottish_progressive.profiles import baseline_profile, load_profile
from scottish_progressive.selfplay_training import write_selfplay_artifact
from scottish_progressive.strength import StrengthMatchConfig, run_strength_match


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run tactical safety and a reusable fixed-suite development screen "
            "over candidate profile files."
        )
    )
    parser.add_argument("candidate_dir", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--glob", default="candidate-*.json")
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--seed", type=int, default=730_194_827)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--branch-cap", type=int, default=32)
    parser.add_argument("--max-work", type=int, default=250_000)
    parser.add_argument("--max-game-work", type=int, default=5_000_000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    candidate_dir = args.candidate_dir.expanduser().resolve()
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    paths = tuple(sorted(candidate_dir.glob(args.glob)))
    if not paths:
        raise ValueError(f"no candidate profiles matched {args.glob!r}")

    reference = baseline_profile()
    config = StrengthMatchConfig(
        pairs=args.pairs,
        seed=args.seed,
        search_depth=args.depth,
        max_series_per_node=args.branch_cap,
        max_generation_positions=args.max_work,
        max_game_work_positions=args.max_game_work,
    )
    rows: list[dict[str, object]] = []
    for path in paths:
        candidate = load_profile(path)
        tactical = run_rules_tactical_gate(
            candidate,
            search_depth=args.depth,
            max_series_per_node=args.branch_cap,
            max_generation_positions=args.max_work,
        )
        row: dict[str, object] = {
            "candidate_path": str(path),
            "candidate_profile_id": candidate.profile_id,
            "tactical_passed": tactical.passed,
            "tactical_checks": [dict(check) for check in tactical.checks],
        }
        if tactical.passed:
            report = run_strength_match(
                candidate,
                reference,
                config=config,
                requested_workers=args.workers,
            )
            report_path = write_selfplay_artifact(
                report,
                output / f"screen-{candidate.profile_id}.json",
            )
            summary = report["summary"]
            row.update(
                game_wdl=summary["candidate_game_wdl"],
                game_score_rate=summary["candidate_game_score_rate"],
                pair_wdl=summary["candidate_pair_wdl"],
                pair_score_rate=summary["candidate_pair_score_rate"],
                incomplete_games=summary["incomplete_games"],
                technical_failures=summary["technical_failures"],
                report_id=report["report_id"],
                report_path=str(report_path),
            )
        rows.append(row)
        print(json.dumps(row, sort_keys=True), flush=True)

    payload = {
        "format": "spc-native-candidate-development-screen-v1",
        "seed": args.seed,
        "pairs_per_candidate": args.pairs,
        "reference_profile_id": reference.profile_id,
        "rows": rows,
        "selection_scope": (
            "reused fixed-suite development screen only; an independently "
            "seeded final match remains mandatory"
        ),
    }
    report_path = write_selfplay_artifact(payload, output / "candidate-screen.json")
    print(json.dumps({"report_path": str(report_path), **payload}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
