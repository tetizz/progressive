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

import chess

from .league import (
    OPENING_SUITE,
    OPENING_SUITE_VERSION,
    GameJob,
    GameRecord,
    OpeningCase,
    _play_game,
    runtime_provenance,
)
from .model import ENGINE_SOURCE_FINGERPRINT, ENGINE_VERSION, ProgressiveState
from .neural_evaluator import NeuralBlend
from .profiles import EngineProfile, baseline_profile, load_profile
from .resources import ResourceBudget, detect_resource_budget
from .rules import generate_series, play_series


STRENGTH_REPORT_FORMAT = "spc-fixed-suite-strength-v1"
SEEDED_OPENING_SUITE_FORMAT = "spc-neutral-seeded-openings-v1"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stable_seed(*parts: object) -> int:
    encoded = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big") & 0x7FFFFFFF


def _stable_id(*parts: object) -> str:
    encoded = "|".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SeededOpeningHistory:
    """Replayable neutral sampling trace for one generated boundary."""

    case_id: str
    target_series: int
    attempt: int
    series: tuple[tuple[str, ...], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "target_series": self.target_series,
            "attempt": self.attempt,
            "series": [list(moves) for moves in self.series],
        }


@dataclass(frozen=True, slots=True)
class SeededOpeningSuite:
    """A content-addressed set of legal, nonterminal opening boundaries."""

    version: str
    seed: int
    min_series: int
    max_series: int
    max_frontier_states: int
    cases: tuple[OpeningCase, ...]
    histories: tuple[SeededOpeningHistory, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": SEEDED_OPENING_SUITE_FORMAT,
            "version": self.version,
            "seed": self.seed,
            "count": len(self.cases),
            "min_series": self.min_series,
            "max_series": self.max_series,
            "max_frontier_states": self.max_frontier_states,
            "cases": [case.as_dict() for case in self.cases],
            "histories": [history.as_dict() for history in self.histories],
        }


def _seeded_suite_version(
    *,
    seed: int,
    min_series: int,
    max_series: int,
    max_frontier_states: int,
    cases: Sequence[OpeningCase],
    histories: Sequence[SeededOpeningHistory],
) -> str:
    payload = {
        "format": SEEDED_OPENING_SUITE_FORMAT,
        "seed": seed,
        "min_series": min_series,
        "max_series": max_series,
        "max_frontier_states": max_frontier_states,
        "boundaries": [
            {
                "opening": case.as_dict(),
                "history": history.as_dict(),
            }
            for case, history in zip(cases, histories, strict=True)
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:20]
    return f"{SEEDED_OPENING_SUITE_FORMAT}-{digest}"


def verify_seeded_opening_suite(suite: SeededOpeningSuite) -> None:
    """Raises if any generated boundary cannot be reproduced from its history."""

    if not suite.cases or len(suite.cases) != len(suite.histories):
        raise ValueError("seeded opening suite cases and histories must align")
    case_ids = [case.case_id for case in suite.cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("seeded opening suite contains duplicate case ids")
    hashes = [case.state().position_hash for case in suite.cases]
    if len(set(hashes)) != len(hashes):
        raise ValueError("seeded opening suite contains duplicate positions")

    for case, history in zip(suite.cases, suite.histories, strict=True):
        if history.case_id != case.case_id:
            raise ValueError("seeded opening history names the wrong case")
        state = ProgressiveState.initial()
        for moves in history.series:
            result = play_series(state, moves)
            if result.is_terminal:
                raise ValueError(
                    f"seeded opening {case.case_id} crosses a terminal result"
                )
            state = result.final_state
        if state.series_number != history.target_series:
            raise ValueError(
                f"seeded opening {case.case_id} has the wrong target series"
            )
        if state.pfen != case.state().pfen:
            raise ValueError(
                f"seeded opening {case.case_id} does not replay to its boundary"
            )

    expected_version = _seeded_suite_version(
        seed=suite.seed,
        min_series=suite.min_series,
        max_series=suite.max_series,
        max_frontier_states=suite.max_frontier_states,
        cases=suite.cases,
        histories=suite.histories,
    )
    if suite.version != expected_version:
        raise ValueError("seeded opening suite version does not match its content")


def seeded_opening_suite_from_dict(
    payload: Mapping[str, Any],
) -> SeededOpeningSuite:
    """Reconstructs and verifies the canonical content-addressed suite payload."""

    if payload.get("format") != SEEDED_OPENING_SUITE_FORMAT:
        raise ValueError("unsupported seeded opening suite")
    raw_cases = payload.get("cases")
    raw_histories = payload.get("histories")
    if not isinstance(raw_cases, list) or not isinstance(raw_histories, list):
        raise ValueError("seeded opening suite cases/histories are missing")
    cases: list[OpeningCase] = []
    for raw in raw_cases:
        if not isinstance(raw, Mapping):
            raise ValueError("seeded opening case is invalid")
        case = OpeningCase(
            case_id=str(raw["case_id"]),
            fen=str(raw["fen"]),
            series_number=int(raw["series_number"]),
            quiet_series=int(raw.get("quiet_series", 0)),
            ep_targets=tuple(str(value) for value in raw.get("ep_targets", ())),
            source=str(raw.get("source", "curated")),
        )
        canonical = case.as_dict()
        if any(
            key in raw and raw[key] != canonical[key]
            for key in ("pfen", "position_hash")
        ):
            raise ValueError("seeded opening case hash does not replay")
        cases.append(case)
    histories: list[SeededOpeningHistory] = []
    for raw in raw_histories:
        if not isinstance(raw, Mapping):
            raise ValueError("seeded opening history is invalid")
        histories.append(
            SeededOpeningHistory(
                case_id=str(raw["case_id"]),
                target_series=int(raw["target_series"]),
                attempt=int(raw["attempt"]),
                series=tuple(
                    tuple(str(move) for move in moves)
                    for moves in raw.get("series", ())
                ),
            )
        )
    suite = SeededOpeningSuite(
        version=str(payload["version"]),
        seed=int(payload["seed"]),
        min_series=int(payload["min_series"]),
        max_series=int(payload["max_series"]),
        max_frontier_states=int(payload["max_frontier_states"]),
        cases=tuple(cases),
        histories=tuple(histories),
    )
    verify_seeded_opening_suite(suite)
    canonical_payload = suite.as_dict()
    if json.dumps(
        canonical_payload, sort_keys=True, separators=(",", ":")
    ) != json.dumps(payload, sort_keys=True, separators=(",", ":")):
        raise ValueError("seeded opening suite payload is not canonical")
    return suite


def subset_seeded_opening_suite(
    suite: SeededOpeningSuite,
    case_ids: Sequence[str],
    *,
    seed: int | None = None,
) -> SeededOpeningSuite:
    """Builds a verified content-addressed subset in the declared case order.

    Tournament replacement waves use this to run only unresolved logical pairs.
    The source suite remains immutable; the returned suite gets its own version
    binding the exact ordered cases, histories, and (optionally derived) seed.
    """

    verify_seeded_opening_suite(suite)
    selected_ids = tuple(str(case_id) for case_id in case_ids)
    if not selected_ids or len(set(selected_ids)) != len(selected_ids):
        raise ValueError("seeded opening subset case ids must be non-empty and unique")
    by_id = {
        case.case_id: (case, history)
        for case, history in zip(suite.cases, suite.histories, strict=True)
    }
    missing = [case_id for case_id in selected_ids if case_id not in by_id]
    if missing:
        raise ValueError(
            "seeded opening subset contains a case outside its source suite: "
            + ", ".join(missing)
        )
    cases = tuple(by_id[case_id][0] for case_id in selected_ids)
    histories = tuple(by_id[case_id][1] for case_id in selected_ids)
    selected_seed = suite.seed if seed is None else seed
    if type(selected_seed) is not int or not 0 <= selected_seed < 1 << 64:
        raise ValueError("seeded opening subset seed must fit u64")
    subset = SeededOpeningSuite(
        version=_seeded_suite_version(
            seed=selected_seed,
            min_series=suite.min_series,
            max_series=suite.max_series,
            max_frontier_states=suite.max_frontier_states,
            cases=cases,
            histories=histories,
        ),
        seed=selected_seed,
        min_series=suite.min_series,
        max_series=suite.max_series,
        max_frontier_states=suite.max_frontier_states,
        cases=cases,
        histories=histories,
    )
    verify_seeded_opening_suite(subset)
    return subset


def compose_seeded_opening_suite(
    selections: Sequence[tuple[SeededOpeningSuite, str]],
    *,
    seed: int,
) -> SeededOpeningSuite:
    """Composes ordered cases from frozen source suites into one verified suite."""

    if not selections:
        raise ValueError("composed seeded opening suite cannot be empty")
    if type(seed) is not int or not 0 <= seed < 1 << 64:
        raise ValueError("composed seeded opening suite seed must fit u64")
    cases: list[OpeningCase] = []
    histories: list[SeededOpeningHistory] = []
    settings: tuple[int, int, int] | None = None
    indexed_sources: dict[
        int,
        tuple[
            SeededOpeningSuite,
            dict[str, tuple[OpeningCase, SeededOpeningHistory]],
        ],
    ] = {}
    for suite, case_id in selections:
        source_key = id(suite)
        indexed = indexed_sources.get(source_key)
        if indexed is None or indexed[0] is not suite:
            verify_seeded_opening_suite(suite)
            indexed = (
                suite,
                {
                    case.case_id: (case, history)
                    for case, history in zip(
                        suite.cases, suite.histories, strict=True
                    )
                },
            )
            indexed_sources[source_key] = indexed
        actual_settings = (
            suite.min_series,
            suite.max_series,
            suite.max_frontier_states,
        )
        if settings is None:
            settings = actual_settings
        elif settings != actual_settings:
            raise ValueError("composed seeded opening suites use different settings")
        by_id = indexed[1]
        if case_id not in by_id:
            raise ValueError("composed opening case is outside its source suite")
        case, history = by_id[case_id]
        cases.append(case)
        histories.append(history)
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("composed seeded opening suite repeats a case id")
    if len({case.state().position_hash for case in cases}) != len(cases):
        raise ValueError("composed seeded opening suite repeats a position")
    assert settings is not None
    suite = SeededOpeningSuite(
        version=_seeded_suite_version(
            seed=seed,
            min_series=settings[0],
            max_series=settings[1],
            max_frontier_states=settings[2],
            cases=cases,
            histories=histories,
        ),
        seed=seed,
        min_series=settings[0],
        max_series=settings[1],
        max_frontier_states=settings[2],
        cases=tuple(cases),
        histories=tuple(histories),
    )
    verify_seeded_opening_suite(suite)
    return suite


def build_seeded_opening_suite(
    *,
    seed: int,
    count: int,
    min_series: int = 3,
    max_series: int = 6,
    max_frontier_states: int = 32,
) -> SeededOpeningSuite:
    """Builds neutral deterministic boundaries without consulting an engine.

    Complete legal series are generated by the public rules API. Selection is
    based only on the declared seed, attempt number, and position identity; no
    profile, evaluation, result label, or trained parameter influences it.
    """

    if not 1 <= count <= 512:
        raise ValueError("count must be between 1 and 512")
    if not 2 <= min_series <= max_series <= 16:
        raise ValueError("series range must satisfy 2 <= min <= max <= 16")
    if not 2 <= max_frontier_states <= 512:
        raise ValueError("max_frontier_states must be between 2 and 512")

    cases: list[OpeningCase] = []
    histories: list[SeededOpeningHistory] = []
    seen_hashes: set[str] = set()
    span = max_series - min_series + 1
    target_offset = _stable_seed(
        SEEDED_OPENING_SUITE_FORMAT, seed, "target-series-offset"
    ) % span
    max_attempts = max(256, count * 128)

    for attempt in range(max_attempts):
        target_series = min_series + ((attempt + target_offset) % span)
        state = ProgressiveState.initial()
        history: list[tuple[str, ...]] = []
        usable = True
        while state.series_number < target_series:
            generated = generate_series(
                state,
                merge_transpositions=True,
                max_frontier_states=max_frontier_states,
            )
            continuations = [result for result in generated if not result.is_terminal]
            if not continuations:
                usable = False
                break
            choice_seed = _stable_seed(
                SEEDED_OPENING_SUITE_FORMAT,
                seed,
                attempt,
                state.series_number,
                state.position_hash,
            )
            selected = continuations[choice_seed % len(continuations)]
            replayed = play_series(state, selected.moves)
            if (
                replayed.is_terminal
                or replayed.final_state.pfen != selected.final_state.pfen
            ):
                raise RuntimeError("generated series failed exact public-API replay")
            history.append(selected.moves)
            state = replayed.final_state

        if not usable or state.position_hash in seen_hashes:
            continue
        seen_hashes.add(state.position_hash)
        case_id = (
            f"seeded-{len(cases) + 1:03d}-s{state.series_number}-"
            f"{state.position_hash[:12]}"
        )
        history_text = " | ".join("/".join(moves) for moves in history)
        source = (
            f"neutral deterministic legal-series sampling v1; seed={seed}; "
            f"attempt={attempt}; history={history_text}"
        )
        case = OpeningCase(
            case_id=case_id,
            fen=state.board.fen(en_passant="fen"),
            series_number=state.series_number,
            quiet_series=state.quiet_series,
            ep_targets=tuple(
                chess.square_name(square) for square in state.ep_targets
            ),
            source=source,
        )
        trace = SeededOpeningHistory(
            case_id=case_id,
            target_series=target_series,
            attempt=attempt,
            series=tuple(history),
        )
        cases.append(case)
        histories.append(trace)
        if len(cases) == count:
            break

    if len(cases) != count:
        raise RuntimeError(
            f"could only build {len(cases)}/{count} unique nonterminal openings"
        )
    version = _seeded_suite_version(
        seed=seed,
        min_series=min_series,
        max_series=max_series,
        max_frontier_states=max_frontier_states,
        cases=cases,
        histories=histories,
    )
    suite = SeededOpeningSuite(
        version=version,
        seed=seed,
        min_series=min_series,
        max_series=max_series,
        max_frontier_states=max_frontier_states,
        cases=tuple(cases),
        histories=tuple(histories),
    )
    verify_seeded_opening_suite(suite)
    return suite


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
        if not self.opening_suite_version.strip():
            raise ValueError("opening_suite_version cannot be empty")
        if not self.opening_case_ids:
            raise ValueError("opening_case_ids cannot be empty")
        if len(set(self.opening_case_ids)) != len(self.opening_case_ids):
            raise ValueError("opening_case_ids cannot contain duplicates")
        if self.opening_suite_version == OPENING_SUITE_VERSION:
            available = {case.case_id for case in OPENING_SUITE}
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


@dataclass(frozen=True, slots=True)
class StrengthParticipant:
    """One actual search identity: hand profile plus an optional neural leaf layer."""

    profile: EngineProfile
    evaluation_overlay: NeuralBlend | None = None

    def __post_init__(self) -> None:
        if (
            self.evaluation_overlay is not None
            and self.evaluation_overlay.base_profile_id != self.profile.profile_id
        ):
            raise ValueError("strength participant overlay is bound to another profile")

    @property
    def participant_id(self) -> str:
        return (
            self.profile.profile_id
            if self.evaluation_overlay is None
            else self.evaluation_overlay.variant_id
        )

    @property
    def name(self) -> str:
        return (
            self.profile.name
            if self.evaluation_overlay is None
            else self.evaluation_overlay.name
        )

    def as_dict(self) -> dict[str, Any]:
        if self.evaluation_overlay is None:
            return self.profile.as_dict()
        return {
            "format": "spc-strength-participant-v1",
            "profile_id": self.participant_id,
            "name": self.name,
            "base_profile_id": self.profile.profile_id,
            "base_profile": self.profile.as_dict(),
            "evaluation_overlay": self.evaluation_overlay.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> StrengthParticipant:
        if not isinstance(payload, Mapping):
            raise ValueError("strength participant must be an object")
        if payload.get("format") != "spc-strength-participant-v1":
            return cls(EngineProfile.from_dict(payload))
        try:
            raw_profile = payload["base_profile"]
            raw_overlay = payload["evaluation_overlay"]
            if not isinstance(raw_profile, Mapping) or not isinstance(raw_overlay, Mapping):
                raise ValueError("neural participant payload is incomplete")
            profile = EngineProfile.from_dict(raw_profile)
            participant = cls(
                profile,
                NeuralBlend.from_dict(raw_overlay, profile=profile),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid strength participant: {error}") from error
        if str(payload.get("profile_id", participant.participant_id)) != participant.participant_id:
            raise ValueError("strength participant effective id does not match")
        if str(payload.get("name", participant.name)) != participant.name:
            raise ValueError("strength participant name does not match")
        return participant


def _participant(value: EngineProfile | StrengthParticipant) -> StrengthParticipant:
    return value if isinstance(value, StrengthParticipant) else StrengthParticipant(value)


def resolve_match_profile(reference: str | Path) -> EngineProfile:
    """Loads an EngineProfile JSON/envelope, or the named built-in baseline."""

    if str(reference).strip().lower() == "baseline":
        return baseline_profile()
    return load_profile(reference)


def _ordered_openings(
    config: StrengthMatchConfig,
    opening_cases: SeededOpeningSuite | Sequence[OpeningCase] | None = None,
) -> tuple[OpeningCase, ...]:
    if isinstance(opening_cases, SeededOpeningSuite):
        verify_seeded_opening_suite(opening_cases)
        if config.opening_suite_version != opening_cases.version:
            raise ValueError(
                "config opening_suite_version does not match the verified suite"
            )
        available_cases = opening_cases.cases
    elif opening_cases is None:
        if config.opening_suite_version != OPENING_SUITE_VERSION:
            raise ValueError(
                "a verified SeededOpeningSuite is required for a non-default suite"
            )
        available_cases = OPENING_SUITE
    else:
        if config.opening_suite_version != OPENING_SUITE_VERSION:
            raise ValueError(
                "a verified SeededOpeningSuite is required for a non-default suite"
            )
        available_cases = tuple(opening_cases)
        if not available_cases:
            raise ValueError("opening_cases cannot be empty")
        case_ids = [case.case_id for case in available_cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("opening_cases cannot contain duplicate case ids")
        hashes = [case.state().position_hash for case in available_cases]
        if len(set(hashes)) != len(hashes):
            raise ValueError("opening_cases cannot contain duplicate positions")
        if config.opening_suite_version == OPENING_SUITE_VERSION:
            canonical = {case.case_id: case.as_dict() for case in OPENING_SUITE}
            if any(
                canonical.get(case.case_id) != case.as_dict()
                for case in available_cases
            ):
                raise ValueError(
                    "custom opening content cannot use the default suite version"
                )

    by_id = {case.case_id: case for case in available_cases}
    missing = [case_id for case_id in config.opening_case_ids if case_id not in by_id]
    if missing:
        raise ValueError(
            "opening_case_ids are missing from the supplied suite: "
            + ", ".join(missing)
        )
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
    candidate: EngineProfile | StrengthParticipant,
    reference: EngineProfile | StrengthParticipant,
    config: StrengthMatchConfig,
    opening_cases: SeededOpeningSuite | Sequence[OpeningCase] | None = None,
) -> tuple[GameJob, ...]:
    candidate = _participant(candidate)
    reference = _participant(reference)
    if candidate.participant_id == reference.participant_id:
        raise ValueError("strength match requires two different engine profiles or variants")
    openings = _ordered_openings(config, opening_cases)
    opening_identity = json.dumps(
        [opening.as_dict() for opening in openings],
        sort_keys=True,
        separators=(",", ":"),
    )
    run_id = "strength-" + _stable_id(
        STRENGTH_REPORT_FORMAT,
        candidate.participant_id,
        reference.participant_id,
        json.dumps(config.as_dict(), sort_keys=True, separators=(",", ":")),
        opening_identity,
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
            opening_payload = json.dumps(
                opening.as_dict(), sort_keys=True, separators=(",", ":")
            )
            jobs.append(
                GameJob(
                    job_key=_stable_id(
                        run_id,
                        opening_index,
                        opening.case_id,
                        opening_payload,
                        pair_seed,
                        white.participant_id,
                        black.participant_id,
                    ),
                    run_id=run_id,
                    generation=0,
                    stage="strength-fixed-suite",
                    opening_index=opening_index,
                    opening=opening,
                    seed=pair_seed,
                    white_profile=white.profile,
                    black_profile=black.profile,
                    search_depth=config.search_depth,
                    max_series_per_node=config.max_series_per_node,
                    max_generation_positions=config.max_generation_positions,
                    max_game_work_positions=config.max_game_work_positions,
                    emergency_max_series=config.emergency_max_series,
                    opening_suite_version=config.opening_suite_version,
                    white_evaluation_overlay=white.evaluation_overlay,
                    black_evaluation_overlay=black.evaluation_overlay,
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
    white_id = (
        job.white_profile.profile_id
        if job.white_evaluation_overlay is None
        else job.white_evaluation_overlay.variant_id
    )
    black_id = (
        job.black_profile.profile_id
        if job.black_evaluation_overlay is None
        else job.black_evaluation_overlay.variant_id
    )
    return GameRecord(
        job.job_key,
        job.run_id,
        job.generation,
        job.stage,
        job.opening_index,
        job.opening.case_id,
        job.opening_suite_version,
        job.seed,
        white_id,
        black_id,
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
    candidate: EngineProfile | StrengthParticipant,
    reference: EngineProfile | StrengthParticipant,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    candidate = _participant(candidate)
    reference = _participant(reference)
    game_wins = game_draws = game_losses = incomplete_games = 0
    game_points = 0.0
    completed_games = 0
    failure_reasons: dict[str, int] = {}
    profile_failures = {candidate.participant_id: 0, reference.participant_id: 0}
    worker_failures = 0
    shared_limit_failures = 0
    pairs: list[dict[str, Any]] = []

    for record in records:
        points = _profile_points(record, candidate.participant_id)
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
        points = [
            _profile_points(record, candidate.participant_id) for record in paired
        ]
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
            "candidate": profile_failures.get(candidate.participant_id, 0),
            "reference": profile_failures.get(reference.participant_id, 0),
            "unattributed_worker_failures": worker_failures,
            "unattributed_match_limit_failures": shared_limit_failures,
            "by_reason": dict(sorted(failure_reasons.items())),
        },
        "fixed_suite_performance_difference": _descriptive_elo(score_rate),
    }
    return summary, tuple(pairs)


def run_strength_match(
    candidate: EngineProfile | StrengthParticipant,
    reference: EngineProfile | StrengthParticipant,
    *,
    config: StrengthMatchConfig | None = None,
    opening_cases: SeededOpeningSuite | Sequence[OpeningCase] | None = None,
    requested_workers: int | None = None,
    memory_per_worker_mb: int = 512,
    reserve_memory_mb: int = 512,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Runs an isolated fixed-suite match and returns a JSON-safe report.

    No league or champion database is opened or modified. Each selected boundary
    is used exactly twice with colors swapped, and every move-selection search
    receives the same deterministic limits regardless of profile metadata. A
    non-default version requires its verified ``SeededOpeningSuite`` through
    ``opening_cases``.
    """

    config = config or StrengthMatchConfig()
    candidate_participant = _participant(candidate)
    reference_participant = _participant(reference)
    jobs = _build_jobs(
        candidate_participant,
        reference_participant,
        config,
        opening_cases,
    )
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

    summary, pair_payload = _summarize(
        records,
        candidate_participant,
        reference_participant,
    )
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
        "candidate": candidate_participant.as_dict(),
        "reference": reference_participant.as_dict(),
        "config": config.as_dict(),
        "opening_suite": (
            opening_cases.as_dict()
            if isinstance(opening_cases, SeededOpeningSuite)
            else {
                "format": "built-in-opening-suite",
                "version": OPENING_SUITE_VERSION,
            }
        ),
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
