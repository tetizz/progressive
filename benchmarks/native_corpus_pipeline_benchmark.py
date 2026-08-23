from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from scottish_progressive import evaluation
from scottish_progressive.corpus_shards import progressive_state_dedup_key
from scottish_progressive.native_corpus import (
    NativeCorpusConfig,
    NativeFullGameBatch,
    NativeRankPolicy,
    generate_native_full_game_batch,
    replay_native_batch,
    semantic_config_digest,
)
from scottish_progressive.profiles import baseline_profile


FORMAT = "spc-native-corpus-pipeline-benchmark-v1"


def _positive(name: str, value: int) -> int:
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def _ranges(first_attempt: int, attempts: int, batch_size: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    offset = 0
    while offset < attempts:
        count = min(batch_size, attempts - offset)
        ranges.append((first_attempt + offset, count))
        offset += count
    return ranges


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
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


def run(args: argparse.Namespace) -> dict[str, object]:
    attempts = _positive("attempts", args.attempts)
    batch_size = _positive("batch_size", args.batch_size)
    workers = _positive("workers", args.workers)
    profile = baseline_profile()
    config = NativeCorpusConfig(
        seed=args.seed,
        max_attempt_series=args.max_attempt_series,
        max_frontier_states=args.frontier,
        max_positions_per_series=args.max_positions_per_series,
        max_positions_per_game=args.max_positions_per_game,
        candidate_count=args.candidates,
        policy=(
            NativeRankPolicy.uniform()
            if args.uniform
            else NativeRankPolicy()
        ),
    )
    profiles = (profile,)
    ranges = _ranges(args.first_attempt, attempts, batch_size)
    batches: list[NativeFullGameBatch] = []
    generation_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = {
            executor.submit(
                generate_native_full_game_batch,
                config,
                profiles,
                first_attempt=first,
                attempt_count=count,
            ): (first, count)
            for first, count in ranges
        }
        for future in as_completed(pending):
            batches.append(future.result())
    generation_seconds = time.perf_counter() - generation_started

    replay_started = time.perf_counter()
    terminal_counts: Counter[str] = Counter()
    rejection_counts: Counter[str] = Counter()
    unique_boundaries: set[bytes] = set()
    boundary_occurrences = 0
    accepted_games = 0
    logical_work = 0
    encoded_response_bytes = 0
    path_count_saturations = 0
    for batch in sorted(batches, key=lambda item: item.first_attempt):
        logical_work += batch.logical_work
        encoded_response_bytes += batch.payload_size
        path_count_saturations += batch.total_saturations
        for record in batch.records:
            if record.accepted:
                terminal_counts[record.terminal.name.lower()] += 1
            else:
                rejection_counts[record.reject.name.lower()] += 1
        games = replay_native_batch(batch)
        accepted_games += len(games)
        for game in games:
            for state in game.states:
                boundary_occurrences += 1
                unique_boundaries.add(progressive_state_dedup_key(state))
    replay_seconds = time.perf_counter() - replay_started
    total_seconds = generation_seconds + replay_seconds

    unique_state_set_digest = hashlib.sha256(
        b"spc-native-corpus-benchmark-unique-state-set-v1\0"
    )
    for state_key in sorted(unique_boundaries):
        unique_state_set_digest.update(state_key)
    native = evaluation._native_eval
    native_identity = getattr(native, "SOURCE_IDENTITY", None)
    attempts_per_second = attempts / total_seconds
    accepted_per_second = accepted_games / total_seconds
    result: dict[str, object] = {
        "format": FORMAT,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "native_source_identity": native_identity,
        "semantic_config_digest": semantic_config_digest(config, profiles).hex(),
        "profile_id": profile.profile_id,
        "configuration": {
            **config.as_semantic_dict(),
            "first_attempt": args.first_attempt,
            "attempts": attempts,
            "batch_size": batch_size,
            "workers": workers,
        },
        "result": {
            "accepted_games": accepted_games,
            "rejected_attempts": attempts - accepted_games,
            "acceptance_rate": accepted_games / attempts,
            "terminal_counts": dict(sorted(terminal_counts.items())),
            "rejection_counts": dict(sorted(rejection_counts.items())),
            "boundary_occurrences": boundary_occurrences,
            "unique_progressive_states": len(unique_boundaries),
            "unique_state_yield_per_attempt": len(unique_boundaries) / attempts,
            "deduplication_ratio": (
                0.0
                if boundary_occurrences == 0
                else 1.0 - len(unique_boundaries) / boundary_occurrences
            ),
            "unique_state_set_sha256": unique_state_set_digest.hexdigest(),
            "logical_work": logical_work,
            "path_count_saturations": path_count_saturations,
            "encoded_response_bytes": encoded_response_bytes,
        },
        "timing": {
            "native_generation_seconds": generation_seconds,
            "authoritative_replay_and_dedup_seconds": replay_seconds,
            "end_to_end_seconds": total_seconds,
            "attempts_per_second": attempts_per_second,
            "accepted_games_per_second": accepted_per_second,
        },
        "linear_capacity_projection": {
            "warning": "Short-run linear extrapolation; unique-state yield will not remain linear as the corpus saturates.",
            "one_billion_attempts_seconds": 1_000_000_000 / attempts_per_second,
            "four_billion_attempts_seconds": 4_000_000_000 / attempts_per_second,
        },
    }
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure native generation, authoritative replay, and full-state dedup."
    )
    parser.add_argument("--attempts", type=int, default=512)
    parser.add_argument("--first-attempt", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=64)
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
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = run(args)
    if args.output is not None:
        _atomic_json(args.output.resolve(), payload)
    print(json.dumps(payload, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
