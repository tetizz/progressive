from __future__ import annotations

import argparse
import json
from pathlib import Path

from scottish_progressive.native_teacher import (
    NativeTeacherConfig,
    merge_native_teacher_tiers,
    write_native_teacher_artifact,
)


def _read(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"teacher artifact is not an object: {path}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge frozen quiet-D2 and tactical-D3 teacher tiers."
    )
    parser.add_argument("quiet_depth2", type=Path)
    parser.add_argument("tactical_depth3", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--quiet-target-roots", type=int, default=144)
    parser.add_argument("--quiet-train-roots", type=int, default=96)
    parser.add_argument("--tactical-target-roots", type=int, default=48)
    parser.add_argument("--tactical-train-roots", type=int, default=32)
    parser.add_argument("--minimum-series", type=int, default=4)
    parser.add_argument("--maximum-series", type=int, default=9)
    parser.add_argument("--branch-cap", type=int, default=32)
    parser.add_argument("--max-work", type=int, default=10_000_000)
    parser.add_argument("--hard-negatives", type=int, default=4)
    parser.add_argument("--seed", type=int, default=2_026_082_303)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--train-attempts", type=int, default=8_192)
    parser.add_argument("--holdout-attempts", type=int, default=4_096)
    args = parser.parse_args()

    common_config = {
        "minimum_series": args.minimum_series,
        "maximum_series": args.maximum_series,
        "branch_cap": args.branch_cap,
        "max_generation_positions": args.max_work,
        "hard_negative_count": args.hard_negatives,
        "seed": args.seed,
        "workers": args.workers,
        "expected_train_attempts": args.train_attempts,
        "expected_holdout_attempts": args.holdout_attempts,
    }
    payload = merge_native_teacher_tiers(
        _read(args.quiet_depth2),
        _read(args.tactical_depth3),
        quiet_config=NativeTeacherConfig(
            **common_config,
            target_roots=args.quiet_target_roots,
            train_roots=args.quiet_train_roots,
            depth_series=2,
            selection_mode="quiet-nonterminal",
        ),
        tactical_config=NativeTeacherConfig(
            **common_config,
            target_roots=args.tactical_target_roots,
            train_roots=args.tactical_train_roots,
            depth_series=3,
            selection_mode="tactical-low-complexity",
        ),
    )
    path = write_native_teacher_artifact(payload, args.output)
    print(
        json.dumps(
            {
                "artifact_path": str(path),
                "corpus_id": payload["corpus_id"],
                "accepted_roots": payload["quality"]["accepted_roots"],
                "train_roots": payload["quality"]["train_roots"],
                "holdout_roots": payload["quality"]["holdout_roots"],
                "tier_metrics": payload["quality"]["tier_metrics"],
                "selection": payload["selection"],
            },
            sort_keys=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
