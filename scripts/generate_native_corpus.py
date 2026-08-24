from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile

from scottish_progressive.corpus_pipeline import (
    CorpusGenerationPlan,
    generate_corpus,
    verify_native_boundary_corpus,
)
from scottish_progressive.corpus_shards import CorpusStore
from scottish_progressive.native_corpus import (
    NativeCorpusConfig,
    NativeProfileSchedule,
    NativeRankPolicy,
)
from scottish_progressive.profiles import baseline_profile, load_profile


def _atomic_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate resumable source-bound progressive training shards."
    )
    parser.add_argument("root", type=Path)
    parser.add_argument("--attempts", type=int, required=True)
    parser.add_argument("--first-attempt", type=int, default=0)
    parser.add_argument("--shard-size", type=int, default=10_000)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument(
        "--workers", type=int, default=max(1, min(8, os.cpu_count() or 1))
    )
    parser.add_argument("--seed", type=int, default=730_194_821)
    parser.add_argument("--max-attempt-series", type=int, default=64)
    parser.add_argument("--frontier", type=int, default=32)
    parser.add_argument("--candidates", type=int, default=16)
    parser.add_argument("--max-positions-per-series", type=int, default=250_000)
    parser.add_argument("--max-positions-per-game", type=int, default=10_000_000)
    parser.add_argument("--uniform", action="store_true")
    parser.add_argument(
        "--ordered-pairs",
        action="store_true",
        help=(
            "cycle every ordered white/black profile pairing instead of "
            "self-play only; with N profiles each N**2-attempt block is balanced"
        ),
    )
    parser.add_argument(
        "--profile",
        action="append",
        type=Path,
        help=(
            "EngineProfile JSON to schedule; repeat for multiple profiles. "
            "The built-in baseline is used when omitted."
        ),
    )
    parser.add_argument("--skip-payload-verification", action="store_true")
    parser.add_argument("--skip-unique-count", action="store_true")
    parser.add_argument("--receipt", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    profiles = (
        tuple(load_profile(path) for path in args.profile)
        if args.profile
        else (baseline_profile(),)
    )
    config = NativeCorpusConfig(
        seed=args.seed,
        max_attempt_series=args.max_attempt_series,
        max_frontier_states=args.frontier,
        max_positions_per_series=args.max_positions_per_series,
        max_positions_per_game=args.max_positions_per_game,
        candidate_count=args.candidates,
        policy=NativeRankPolicy.uniform() if args.uniform else NativeRankPolicy(),
        schedule=(
            NativeProfileSchedule.ORDERED_PAIR_ROUND_ROBIN
            if args.ordered_pairs
            else NativeProfileSchedule.SELF_ROUND_ROBIN
        ),
    )
    plan = CorpusGenerationPlan(
        root=args.root,
        config=config,
        profiles=profiles,
        first_attempt=args.first_attempt,
        attempt_count=args.attempts,
        shard_size=args.shard_size,
        batch_size=args.batch_size,
        workers=args.workers,
    )
    receipt = generate_corpus(plan)
    if not args.skip_payload_verification:
        store = CorpusStore(plan.root, plan.identity)
        receipt["payload_verification"] = verify_native_boundary_corpus(
            store,
            count_unique_states=not args.skip_unique_count,
        )
    receipt["completed_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    if args.receipt is not None:
        _atomic_receipt(args.receipt.resolve(), receipt)
    print(json.dumps(receipt, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
