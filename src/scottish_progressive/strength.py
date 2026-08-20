from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import random
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

from .league import (
    OPENING_SUITE,
    OPENING_SUITE_VERSION,
    GameJob,
    GameRecord,
    OpeningCase,
    _play_game,
    runtime_provenance,
)
from .model import ENGINE_SOURCE_FINGERPRINT, ENGINE_VERSION
from .profiles import EngineProfile, baseline_profile, load_profile
from .resources import ResourceBudget, detect_resource_budget


STRENGTH_REPORT_FORMAT = "spc-fixed-suite-strength-v1"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stable_seed(*parts: object) -> int:
    encoded = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big") & 0x7FFFFFFF


def _stable_id(*parts: object) -> str:
    encoded = "|".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class StrengthMatchConfig:
    """Deterministic limits for one isolated, color-swapped profile match."""

    pairs: int = 10
    seed: int = 20260820
    search_depth: int = 2
    max_series_per_node: int = 32
    max_generation_positions: int = 250_000
    max_game_work_positions: int = 5_000_000
    emergency_max_series: int | None = None
    opening_suite_version: str = OPENING_SUITE_VERSION
    opening_case_ids: tuple[str, ...] = tuple(
        case.case_id for case in OPENING_SUITE
    )

    def __post_init__(self) -> None:
        if self.opening_suite_version != OPENING_SUITE_VERSION:
            raise ValueError(
                f"unsupported opening suite {self.opening_suite_version}"
            )
        available = {case.case_id for case in OPENING_SUITE}
        if not self.opening_case_ids:
            raise ValueError("opening_case_ids cannot be empty")
        if len(set(self.opening_case_ids)) != len(self.opening_case_ids):
            raise ValueError("opening_case_ids cannot contain duplicates")
        if not set(self.opening_case_ids) <= available:
            raise ValueError("opening_case_ids must name cases in the active suite")
        if not 1 <= self.pairs <= len(self.opening_case_ids):
            raise ValueError(
                "pairs must be between 1 and the number of unique opening cases"
            )
        if not 1 <= self.search_depth <= 8:
            raise ValueError("search_depth must be between 1 and 8")
        if not 1 <= self.max_series_per_node <= 512:
            raise ValueError("max_series_per_node must be between 1 and 512")
        if self.max_generation_positions < 1_000:
            raise ValueError("max_generation_positions must be at least 1000")
        if self.max_game_work_positions < 1_000:
            raise ValueError("max_game_work_positions must be at least 1000")
        if self.emergency_max_series is not None and self.emergency_max_series < 18:
            raise ValueError("emergency_max_series must be at least 18")

    @classmethod
    def smoke(cls, *, seed: int = 7) -> StrengthMatchConfig:
        return cls(
            pairs=1,
            seed=seed,
            search_depth=1,
            max_series_per_node=2,
            max_generation_positions=5_000,
            max_game_work_positions=10_000,
            emergency_max_series=None,
            opening_case_ids=tuple(case.case_id for case in OPENING_SUITE[:3]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "pairs": self.pairs,
            "games": self.pairs * 2,
            "seed": self.seed,
            "opening_suite_version": self.opening_suite_version,
            "opening_case_ids": list(self.opening_case_ids),
            "deterministic_limits": {
                "depth_series": self.search_depth,
                "branch_cap_complete_series_per_node": self.max_series_per_node,
                "max_work_positions_per_search": (
                    self.max_generation_positions
                ),
                "max_game_work_positions": self.max_game_work_positions,
                "game_work_definition": (
                    "deterministic logical positions across complete-series "
                    "generation, evaluation reach, and quiet adjudication over "
                    "the whole game"
                ),
                "emergency_max_series": self.emergency_max_series,
                "emergency_series_note": (
                    "null means unbounded by series number; any configured value "
                    "is a technical watchdog, never a chess rule or draw cutoff"
                ),
                "time_limit_seconds": None,
                "node_limit": None,
                "node_note": (
                    "nodes are measured, not capped; both profiles receive the "
                    "same deterministic depth, branch, and generation-work limits"
                ),
                "fresh_searcher_each_series": True,
                "collect_all_root_scores": False,
                "root_score_mode": "best-only-play-optimized",
                "same_for_both_profiles": True,
            },
        }


def resolve_match_profile(reference: str | Path) -> EngineProfile:
    """Loads an EngineProfile JSON/envelope, or the named built-in baseline."""

    if str(reference).strip().lower() == "baseline":
        return baseline_profile()
    return load_profile(reference)


def _ordered_openings(config: StrengthMatchConfig) -> tuple[OpeningCase, ...]:
    by_id = {case.case_id: case for case in OPENING_SUITE}
    cases = [by_id[case_id] for case_id in config.opening_case_ids]
    ordering_seed = _stable_seed(
        STRENGTH_REPORT_FORMAT,
        config.opening_suite_version,
        config.seed,
        "opening-order",
    )
    random.Random(ordering_seed).shuffle(cases)
    return tuple(cases[: config.pairs])


def _build_jobs(
    candidate: EngineProfile,
    reference: EngineProfile,
    config: StrengthMatchConfig,
) -> tuple[GameJob, ...]:
    if candidate.profile_id == reference.profile_id:
        raise ValueError("strength match requires two different engine profiles")
    openings = _ordered_openings(config)
    run_id = "strength-" + _stable_id(
        STRENGTH_REPORT_FORMAT,
        candidate.profile_id,
        reference.profile_id,
        json.dumps(config.as_dict(), sort_keys=True, separators=(",", ":")),
    )[:20]
    jobs: list[GameJob] = []
    for pair_index, opening in enumerate(openings):
        pair_seed = _stable_seed(
            config.seed,
            config.opening_suite_version,
            pair_index,
            opening.case_id,
        )
        for swap, (white, black) in enumerate(
            ((candidate, reference), (reference, candidate))
        ):
            opening_index = pair_index * 2 + swap
            jobs.append(
                GameJob(
                    job_key=_stable_id(
                        run_id,
                        opening_index,
                        opening.case_id,
                        pair_seed,
                        white.profile_id,
                        black.profile_id,
                    ),
                    run_id=run_id,
                    generation=0,
                    stage="strength-fixed-suite",
                    opening_index=opening_index,
                    opening=opening,
                    seed=pair_seed,
                    white_profile=white,
                    black_profile=black,
                    search_depth=config.search_depth,
                    max_series_per_node=config.max_series_per_node,
                    max_generation_positions=config.max_generation_positions,
                    max_game_work_positions=config.max_game_work_positions,
                    emergency_max_series=config.emergency_max_series,
                )
            )
    return tuple(jobs)


def _profile_points(record: GameRecord, profile_id: str) -> float | None:
    if record.result == "1/2-1/2":
        return 0.5
    if record.result == "1-0":
        return 1.0 if record.white_profile_id == profile_id else 0.0
    if record.result == "0-1":
        return 1.0 if record.black_profile_id == profile_id else 0.0
    return None


def _game_payload(record: GameRecord, opening: OpeningCase) -> dict[str, Any]:
    payload = asdict(record)
    payload["trace"] = [dict(item) for item in record.trace]
    payload["opening"] = opening.as_dict()
    return payload


def _worker_failure(job: GameJob, error: BaseException) -> GameRecord:
    state = job.opening.state()
    return GameRecord(
        job.job_key,
        job.run_id,
        job.generation,
        job.stage,
        job.opening_index,
        job.opening.case_id,
        OPENING_SUITE_VERSION,
        job.seed,
        job.white_profile.profile_id,
        job.black_profile.profile_id,
        "*",
        "worker-exception",
        None,
        None,
        state.pfen,
        state.pfen,
        0,
        (),
        f"{type(error).__name__}: {error}",
    )


def _execute_jobs(
    jobs: Sequence[GameJob],
    resources: ResourceBudget,
    progress: Callable[[str], None] | None,
) -> tuple[GameRecord, ...]:
    completed: dict[str, GameRecord] = {}

    def report(count: int) -> None:
        if progress is not None:
            progress(f"strength match: finished {count}/{len(jobs)} games")

    if resources.workers == 1:
        for count, job in enumerate(jobs, 1):
            completed[job.job_key] = _play_game(job)
            report(count)
    else:
        with ProcessPoolExecutor(max_workers=resources.workers) as executor:
            future_jobs = {executor.submit(_play_game, job): job for job in jobs}
            for count, future in enumerate(as_completed(future_jobs), 1):
                job = future_jobs[future]
                try:
                    completed[job.job_key] = future.result()
                except BaseException as error:
                    completed[job.job_key] = _worker_failure(job, error)
                report(count)
    # Completion order is intentionally discarded. The serialized match is
    # stable by pair and color even when workers finish in a different order.
    return tuple(completed[job.job_key] for job in jobs)


def _descriptive_elo(score_rate: float | None) -> dict[str, Any]:
    estimate: int | None = None
    status = "unavailable"
    if score_rate is not None:
        if 0.0 < score_rate < 1.0:
            estimate = round(400.0 * math.log10(score_rate / (1.0 - score_rate)))
            status = "finite"
        else:
            status = "saturated-at-suite-boundary"
    return {
        "value": estimate,
        "unit": "descriptive Elo-like points",
        "status": status,
        "basis": (
            "completed fixed-suite legal results only; technical and budget "
            "incompletes are excluded"
        ),
        "warning": (
            "This is a descriptive performance-difference transform only, not a "
            "calibrated Elo rating or confidence bound. It is not comparable to "
            "orthodox Stockfish Elo."
        ),
    }


def _summarize(
    records: Sequence[GameRecord],
    candidate: EngineProfile,
    reference: EngineProfile,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    game_wins = game_draws = game_losses = incomplete_games = 0
    game_points = 0.0
    completed_games = 0
    failure_reasons: dict[str, int] = {}
    profile_failures = {candidate.profile_id: 0, reference.profile_id: 0}
    worker_failures = 0
    shared_limit_failures = 0
    pairs: list[dict[str, Any]] = []

    for record in records:
        points = _profile_points(record, candidate.profile_id)
        if points is None:
            incomplete_games += 1
        else:
            completed_games += 1
            game_points += points
            if points == 1.0:
                game_wins += 1
            elif points == 0.5:
                game_draws += 1
            else:
                game_losses += 1
        if record.engine_failure_profile_id is not None:
            profile_failures[record.engine_failure_profile_id] = (
                profile_failures.get(record.engine_failure_profile_id, 0) + 1
            )
            failure_reasons[record.terminal_reason] = (
                failure_reasons.get(record.terminal_reason, 0) + 1
            )
        if record.terminal_reason == "worker-exception":
            worker_failures += 1
            failure_reasons[record.terminal_reason] = (
                failure_reasons.get(record.terminal_reason, 0) + 1
            )
        elif (
            record.engine_failure_profile_id is None
            and record.terminal_reason.startswith("technical-")
        ):
            shared_limit_failures += 1
            failure_reasons[record.terminal_reason] = (
                failure_reasons.get(record.terminal_reason, 0) + 1
            )

    pair_wins = pair_draws = pair_losses = incomplete_pairs = 0
    for pair_index in range(0, len(records), 2):
        paired = records[pair_index : pair_index + 2]
        case_id = paired[0].opening_case_id
        points = [_profile_points(record, candidate.profile_id) for record in paired]
        if len(paired) != 2 or any(value is None for value in points):
            pair_result = "incomplete"
            total_points: float | None = None
            incomplete_pairs += 1
        else:
            total_points = sum(value for value in points if value is not None)
            if total_points > 1.0:
                pair_result = "win"
                pair_wins += 1
            elif total_points == 1.0:
                pair_result = "draw"
                pair_draws += 1
            else:
                pair_result = "loss"
                pair_losses += 1
        pairs.append(
            {
                "pair_index": pair_index // 2,
                "opening_case_id": case_id,
                "candidate_points": total_points,
                "result": pair_result,
                "game_job_keys": [record.job_key for record in paired],
                "technical_failures": [
                    {
                        "profile_id": record.engine_failure_profile_id,
                        "reason": record.terminal_reason,
                    }
                    for record in paired
                    if record.engine_failure_profile_id is not None
                    or record.terminal_reason == "worker-exception"
                ],
            }
        )

    score_rate = game_points / completed_games if completed_games else None
    completed_pairs = pair_wins + pair_draws + pair_losses
    pair_score_rate = (
        (pair_wins + pair_draws * 0.5) / completed_pairs
        if completed_pairs
        else None
    )
    summary = {
        "scheduled_games": len(records),
        "completed_games": completed_games,
        "incomplete_games": incomplete_games,
        "candidate_game_wdl": {
            "wins": game_wins,
            "draws": game_draws,
            "losses": game_losses,
        },
        "candidate_game_points": game_points,
        "candidate_game_score_rate": score_rate,
        "scheduled_pairs": len(records) // 2,
        "completed_pairs": completed_pairs,
        "incomplete_pairs": incomplete_pairs,
        "candidate_pair_wdl": {
            "wins": pair_wins,
            "draws": pair_draws,
            "losses": pair_losses,
        },
        "candidate_pair_score_rate": pair_score_rate,
        "technical_failures": {
            "total_profile_failures": sum(profile_failures.values()),
            "candidate": profile_failures.get(candidate.profile_id, 0),
            "reference": profile_failures.get(reference.profile_id, 0),
            "unattributed_worker_failures": worker_failures,
            "unattributed_match_limit_failures": shared_limit_failures,
            "by_reason": dict(sorted(failure_reasons.items())),
        },
        "fixed_suite_performance_difference": _descriptive_elo(score_rate),
    }
    return summary, tuple(pairs)


def run_strength_match(
    candidate: EngineProfile,
    reference: EngineProfile,
    *,
    config: StrengthMatchConfig | None = None,
    requested_workers: int | None = None,
    memory_per_worker_mb: int = 512,
    reserve_memory_mb: int = 512,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Runs an isolated fixed-suite match and returns a JSON-safe report.

    No league or champion database is opened or modified. Each selected boundary
    is used exactly twice with colors swapped, and every move-selection search
    receives the same deterministic limits regardless of profile metadata.
    """

    config = config or StrengthMatchConfig()
    jobs = _build_jobs(candidate, reference, config)
    detected_resources = detect_resource_budget(
        requested_workers,
        memory_per_worker_mb=memory_per_worker_mb,
        reserve_memory_mb=reserve_memory_mb,
    )
    resources = replace(
        detected_resources, workers=min(detected_resources.workers, len(jobs))
    )
    started = time.perf_counter()
    records = _execute_jobs(jobs, resources, progress)
    elapsed_seconds = time.perf_counter() - started

    summary, pair_payload = _summarize(records, candidate, reference)
    opening_by_id = {job.opening.case_id: job.opening for job in jobs}
    selected_case_ids = tuple(job.opening.case_id for job in jobs[::2])
    report_id = jobs[0].run_id
    return {
        "format": STRENGTH_REPORT_FORMAT,
        "report_id": report_id,
        "created_at": _now(),
        "engine": {
            "version": ENGINE_VERSION,
            "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
            "runtime": runtime_provenance(),
        },
        "candidate": candidate.as_dict(),
        "reference": reference.as_dict(),
        "config": config.as_dict(),
        "resources": resources.as_dict(),
        "execution": {
            "wall_elapsed_seconds": elapsed_seconds,
            "completed_games_per_second": (
                summary["completed_games"] / elapsed_seconds
                if elapsed_seconds > 0.0
                else None
            ),
            "result_order": "opening-pair-then-color-swap",
        },
        "selected_openings": [
            opening_by_id[case_id].as_dict() for case_id in selected_case_ids
        ],
        "summary": summary,
        "pairs": list(pair_payload),
        "games": [
            _game_payload(record, opening_by_id[record.opening_case_id])
            for record in records
        ],
        "claim_scope": {
            "fixed_suite_only": True,
            "statement": (
                "Results apply only to these versioned Scottish Progressive "
                "boundaries and exact deterministic search limits."
            ),
            "promotion_effect": "none; this harness never changes the champion",
            "stockfish_comparison": (
                "This report does not establish Stockfish-level strength. Orthodox "
                "Stockfish is not a Scottish Progressive rules engine and was not "
                "a participant."
            ),
        },
    }


def write_strength_report(
    report: Mapping[str, Any], destination: str | Path
) -> Path:
    """Atomically writes a complete, indented strength report."""

    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return target
