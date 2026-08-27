from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import subprocess
import tempfile
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

import chess

from benchmarks.bucephalus_timed_adapter import (
    BUCEPHALUS_ADAPTER_VERSION,
    BUCEPHALUS_MAX_GAME_RECORD,
    BUCEPHALUS_MAX_PLY,
    BUCEPHALUS_OPENING_HISTORIES_V1,
    BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
    BucephalusSpec,
    ExternalAnalysis,
    ExternalAnalysisStage,
    ExternalEngineConfigurationError,
    ExternalEngineError,
    ExternalEngineProtocolError,
    ExternalEngineTimeout,
    SeriesHistory,
    analyze_bucephalus,
    analyze_bucephalus_timed_iterative,
    replay_series_history,
    _parse_deepest_completed_ply,
    _parse_deepest_continuation_progress,
    _parse_deepest_legal_incomplete_prefix,
    _parse_requested_ply,
    _request_script,
    _validate_output_identity_and_boundary,
)
from scottish_progressive.league import OPENING_SUITE, OPENING_SUITE_VERSION, OpeningCase
from scottish_progressive.model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    Outcome,
    ProgressiveState,
    SeriesResult,
)
from scottish_progressive.profiles import EngineProfile
from scottish_progressive.resources import MIB, ResourceBudget, detect_resource_budget
from scottish_progressive.rules import SeriesLegalityError, generate_series, play_series
from scottish_progressive.search import SearchLimits, SearchResult, analyze
from scottish_progressive.strength import (
    SeededOpeningSuite,
    SeededOpeningHistory,
    _seeded_suite_version,
    build_seeded_opening_suite,
    verify_seeded_opening_suite,
)


EXTERNAL_MATCH_FORMAT = "spc-bucephalus-fixed-suite-v1"
EXTERNAL_MATCH_JOURNAL_FORMAT = "spc-bucephalus-match-journal-v1"
EXTERNAL_PLY_POLICY = "series-number-plus-fixed-lookahead-v1"
TIMED_ITERATIVE_PLY_POLICY = "maximum-ply-best-completed-under-wall-budget-v1"
FAIR_EQUAL_WALL_MATCH_INTENT = "fair-equal-wall"
BEST_SETTINGS_MATCH_INTENT = "best-settings-head-to-head"
OPENING_POLICY_FIXED_SUITE = "canonical-color-swapped-suite"
OPENING_POLICY_INITIAL = "initial-position-no-preplayed-series"
INITIAL_POSITION_SUITE_VERSION = "spc-initial-position-no-preplayed-series-v1"
ENGAGED_OPENING_QUALIFICATION_VERSION = "spc-engaged-openings-v2"
BUCEPHALUS_FAIR_OPENING_SEED = 20260827
BUCEPHALUS_FAIR_OPENING_SUITE_VERSION = (
    "spc-neutral-seeded-openings-v1-a292fa4db4e8b7d98248"
)
BUCEPHALUS_FAIR_OPENING_SUITE_CANONICAL_SHA256 = (
    "53fe7d10b5e31d93e0b9b75374832c2e319a691b710c34c4e4a75b5db2cb6ff1"
)
BUCEPHALUS_PROCESS_MEMORY_ESTIMATE_MB = 191
LOCAL_WORKER_MEMORY_ESTIMATE_MB = 512
WORKER_OVERHEAD_MEMORY_ESTIMATE_MB = 65
DEFAULT_EXTERNAL_MATCH_MEMORY_PER_WORKER_MB = (
    BUCEPHALUS_PROCESS_MEMORY_ESTIMATE_MB
    + LOCAL_WORKER_MEMORY_ESTIMATE_MB
    + WORKER_OVERHEAD_MEMORY_ESTIMATE_MB
)
BUCEPHALUS_MAX_LEGAL_MOVES = 100
COMMON_WALL_OVERRUN_GRACE_SECONDS = 0.25
BUCEPHALUS_BUILD_RECEIPT_SCHEMA = "spc-bucephalus-flushed-build-receipt-v1"
BUCEPHALUS_BUILD_RECEIPT_RELATIVE_PATH = (
    "benchmarks/protocols/bucephalus-flushed-0e11fcdc-build-receipt.json"
)
APPROVED_BUCEPHALUS_EXECUTABLE_SHA256 = (
    "9d7b0b2c75d9cc01577e116a4afd0f17075339b242ff47e859bcf1adb7f7a7e0"
)
APPROVED_BUCEPHALUS_UPSTREAM_COMMIT = (
    "0e11fcdc84e65122fd8b91cada71dad6323db417"
)
APPROVED_BUCEPHALUS_PATCH_SHA256 = (
    "286ecd99c18fda0e7a85f2488bb6006385bee216b27c6bcfefdd0d27a63efb55"
)
APPROVED_BUCEPHALUS_BUILD_RECEIPT_SHA256 = (
    "d880fe4b623e9d7993f4699e4625f46e6ec64ae73872d336c62713f8d64ddcb7"
)
APPROVED_BEST_SETTINGS_LOCAL_PROFILE_ID = "spc-68942034c41b4cc4"
APPROVED_BEST_SETTINGS_LOCAL_PROFILE_SHA256 = (
    "a8c698997f3a0acfa1777bae9723fa119684745be7ed2706269ac16905d500f8"
)

_INITIAL_STATE = ProgressiveState.initial()
INITIAL_POSITION_CASE = OpeningCase(
    case_id="initial-position",
    fen=_INITIAL_STATE.board.fen(en_passant="fen"),
    series_number=1,
    source="standard-initial-position",
)
INITIAL_POSITION_SUITE_CANONICAL_SHA256 = hashlib.sha256(
    json.dumps(
        {
            "format": INITIAL_POSITION_SUITE_VERSION,
            "opening_policy": OPENING_POLICY_INITIAL,
            "case": INITIAL_POSITION_CASE.as_dict(),
            "canonical_history": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()

ExternalAdapter = Callable[..., ExternalAnalysis]
LocalAnalyzer = Callable[..., SearchResult]


@dataclass(frozen=True, slots=True)
class RejectedOpeningCandidate:
    candidate_index: int
    case_id: str
    position_hash: str
    reason: str


@dataclass(frozen=True, slots=True)
class OpeningQualification:
    version: str
    candidate_seed: int
    candidate_pool_count: int
    candidate_pool_canonical_sha256: str
    candidate_max_frontier_states: int
    eligible_pool_count: int
    selected_count: int
    target_series: int
    rejected_material_imbalance: int
    rejected_immediate_terminal: int
    last_selected_candidate_index: int
    rejected_candidates: tuple[RejectedOpeningCandidate, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "material_gate": "equal-white-black-count-for-each-piece-type",
            "engagement_gate": (
                "exhaustive-complete-series-generation-no-immediate-terminal"
            ),
            "generation_merge_transpositions": True,
            "generation_selective_frontier_cap": None,
            "guarantee": (
                "both engines receive at least one post-opening turn in each "
                "color-swapped game"
            ),
        }


def _load_fair_opening_suite() -> tuple[
    Mapping[str, Any], tuple[OpeningCase, ...], Mapping[str, SeriesHistory]
]:
    payload = json.loads(
        Path(__file__)
        .with_name("protocols")
        .joinpath("bucephalus_fair_openings_v1.json")
        .read_text(encoding="utf-8")
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != BUCEPHALUS_FAIR_OPENING_SUITE_CANONICAL_SHA256:
        raise RuntimeError("fair Bucephalus opening suite digest mismatch")
    if (
        payload.get("version") != BUCEPHALUS_FAIR_OPENING_SUITE_VERSION
        or payload.get("seed") != BUCEPHALUS_FAIR_OPENING_SEED
        or payload.get("count") != 50
    ):
        raise RuntimeError("fair Bucephalus opening suite identity mismatch")

    cases = tuple(
        OpeningCase(
            case_id=item["case_id"],
            fen=item["fen"],
            series_number=item["series_number"],
            quiet_series=item["quiet_series"],
            ep_targets=tuple(item["ep_targets"]),
            source=item["source"],
        )
        for item in payload["cases"]
    )
    histories: dict[str, SeriesHistory] = {
        item["case_id"]: tuple(tuple(series) for series in item["series"])
        for item in payload["histories"]
    }
    if len(cases) != 50 or len(histories) != 50:
        raise RuntimeError("fair Bucephalus suite must contain exactly 50 boundaries")
    if len({case.state().position_hash for case in cases}) != len(cases):
        raise RuntimeError("fair Bucephalus suite contains duplicate boundaries")
    for case in cases:
        replayed = replay_series_history(histories[case.case_id])
        if replayed.pfen != case.state().pfen:
            raise RuntimeError(
                f"fair Bucephalus opening {case.case_id} failed exact replay"
            )
    return MappingProxyType(payload), cases, MappingProxyType(histories)


(
    BUCEPHALUS_FAIR_OPENING_METADATA,
    BUCEPHALUS_FAIR_OPENING_SUITE,
    BUCEPHALUS_FAIR_OPENING_HISTORIES,
) = _load_fair_opening_suite()
BUCEPHALUS_OPENING_SUITES: Mapping[str, tuple[OpeningCase, ...]] = MappingProxyType(
    {
        OPENING_SUITE_VERSION: OPENING_SUITE,
        BUCEPHALUS_FAIR_OPENING_SUITE_VERSION: BUCEPHALUS_FAIR_OPENING_SUITE,
        INITIAL_POSITION_SUITE_VERSION: (INITIAL_POSITION_CASE,),
    }
)
BUCEPHALUS_OPENING_HISTORIES: Mapping[
    str, Mapping[str, SeriesHistory]
] = MappingProxyType(
    {
        OPENING_SUITE_VERSION: BUCEPHALUS_OPENING_HISTORIES_V1,
        BUCEPHALUS_FAIR_OPENING_SUITE_VERSION: BUCEPHALUS_FAIR_OPENING_HISTORIES,
        INITIAL_POSITION_SUITE_VERSION: MappingProxyType(
            {INITIAL_POSITION_CASE.case_id: ()}
        ),
    }
)


def _legacy_opening_suite_digest() -> str:
    payload = {
        "format": "spc-bucephalus-canonical-openings-v1",
        "version": OPENING_SUITE_VERSION,
        "cases": [case.as_dict() for case in OPENING_SUITE],
        "histories": {
            case_id: [list(series) for series in history]
            for case_id, history in sorted(
                BUCEPHALUS_OPENING_HISTORIES_V1.items()
            )
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


BUCEPHALUS_OPENING_SUITE_SHA256: Mapping[str, str] = MappingProxyType(
    {
        OPENING_SUITE_VERSION: _legacy_opening_suite_digest(),
        BUCEPHALUS_FAIR_OPENING_SUITE_VERSION: (
            BUCEPHALUS_FAIR_OPENING_SUITE_CANONICAL_SHA256
        ),
        INITIAL_POSITION_SUITE_VERSION: INITIAL_POSITION_SUITE_CANONICAL_SHA256,
    }
)


@dataclass(frozen=True, slots=True)
class _OpeningContext:
    cases: tuple[OpeningCase, ...]
    histories: Mapping[str, SeriesHistory]
    canonical_sha256: str
    generator: Mapping[str, int] | None
    content_addressed: bool


def _has_equal_per_piece_material(state: ProgressiveState) -> bool:
    return all(
        len(state.board.pieces(piece_type, chess.WHITE))
        == len(state.board.pieces(piece_type, chess.BLACK))
        for piece_type in range(chess.PAWN, chess.KING + 1)
    )


def _has_immediate_terminal_series(state: ProgressiveState) -> bool:
    return any(
        result.is_terminal
        for result in generate_series(
            state,
            merge_transpositions=True,
            max_frontier_states=None,
        )
    )


def build_engaged_opening_suite(
    *,
    seed: int,
    count: int,
    candidate_pool_count: int,
    max_frontier_states: int = 32,
) -> tuple[SeededOpeningSuite, OpeningQualification]:
    """Build an early, neutral suite that guarantees both engines a turn."""

    if not 1 <= count <= candidate_pool_count <= 512:
        raise ValueError(
            "engaged opening counts must satisfy 1 <= selected <= pool <= 512"
        )
    candidates = build_seeded_opening_suite(
        seed=seed,
        count=candidate_pool_count,
        min_series=3,
        max_series=3,
        max_frontier_states=max_frontier_states,
    )
    accepted: list[tuple[int, OpeningCase, SeededOpeningHistory]] = []
    rejected_material = 0
    rejected_terminal = 0
    rejected_candidates: list[RejectedOpeningCandidate] = []
    for candidate_index, (case, history) in enumerate(
        zip(candidates.cases, candidates.histories, strict=True),
        1,
    ):
        state = case.state()
        if not _has_equal_per_piece_material(state):
            rejected_material += 1
            rejected_candidates.append(
                RejectedOpeningCandidate(
                    candidate_index,
                    case.case_id,
                    state.position_hash,
                    "material-imbalance",
                )
            )
            continue
        if _has_immediate_terminal_series(state):
            rejected_terminal += 1
            rejected_candidates.append(
                RejectedOpeningCandidate(
                    candidate_index,
                    case.case_id,
                    state.position_hash,
                    "immediate-terminal-series",
                )
            )
            continue
        accepted.append((candidate_index, case, history))
    if len(accepted) < count:
        raise RuntimeError(
            f"only {len(accepted)}/{candidate_pool_count} candidates passed "
            "the engagement qualification"
        )

    selected_cases: list[OpeningCase] = []
    selected_histories: list[SeededOpeningHistory] = []
    for selected_index, (candidate_index, case, history) in enumerate(
        accepted[:count],
        1,
    ):
        case_id = (
            f"engaged-{selected_index:03d}-candidate-{candidate_index:03d}-"
            f"s3-{case.state().position_hash[:12]}"
        )
        selected_cases.append(
            OpeningCase(
                case_id=case_id,
                fen=case.fen,
                series_number=case.series_number,
                quiet_series=case.quiet_series,
                ep_targets=case.ep_targets,
                source=(
                    f"{case.source}; qualification="
                    f"{ENGAGED_OPENING_QUALIFICATION_VERSION}; "
                    f"candidate_index={candidate_index}"
                ),
            )
        )
        selected_histories.append(
            SeededOpeningHistory(
                case_id=case_id,
                target_series=history.target_series,
                attempt=history.attempt,
                series=history.series,
            )
        )
    version = _seeded_suite_version(
        seed=seed,
        min_series=3,
        max_series=3,
        max_frontier_states=max_frontier_states,
        cases=selected_cases,
        histories=selected_histories,
    )
    suite = SeededOpeningSuite(
        version=version,
        seed=seed,
        min_series=3,
        max_series=3,
        max_frontier_states=max_frontier_states,
        cases=tuple(selected_cases),
        histories=tuple(selected_histories),
    )
    verify_seeded_opening_suite(suite)
    qualification = OpeningQualification(
        version=ENGAGED_OPENING_QUALIFICATION_VERSION,
        candidate_seed=seed,
        candidate_pool_count=candidate_pool_count,
        candidate_pool_canonical_sha256=_canonical_sha256(candidates.as_dict()),
        candidate_max_frontier_states=max_frontier_states,
        eligible_pool_count=len(accepted),
        selected_count=count,
        target_series=3,
        rejected_material_imbalance=rejected_material,
        rejected_immediate_terminal=rejected_terminal,
        last_selected_candidate_index=accepted[count - 1][0],
        rejected_candidates=tuple(rejected_candidates),
    )
    return suite, qualification


def _resolve_opening_context(
    config: ExternalMatchConfig,
    opening_suite: SeededOpeningSuite | None,
) -> _OpeningContext:
    if config.opening_policy == OPENING_POLICY_INITIAL and opening_suite is not None:
        raise ValueError("initial-position mode forbids a supplied opening suite")
    if opening_suite is None:
        if config.opening_suite_version not in BUCEPHALUS_OPENING_SUITES:
            raise ValueError(
                "custom opening suite configuration requires opening_suite"
            )
        generator = (
            MappingProxyType(
                {
                    "seed": BUCEPHALUS_FAIR_OPENING_METADATA["seed"],
                    "count": BUCEPHALUS_FAIR_OPENING_METADATA["count"],
                    "min_series": BUCEPHALUS_FAIR_OPENING_METADATA["min_series"],
                    "max_series": BUCEPHALUS_FAIR_OPENING_METADATA["max_series"],
                    "max_frontier_states": BUCEPHALUS_FAIR_OPENING_METADATA[
                        "max_frontier_states"
                    ],
                }
            )
            if config.opening_suite_version
            == BUCEPHALUS_FAIR_OPENING_SUITE_VERSION
            else None
        )
        return _OpeningContext(
            cases=BUCEPHALUS_OPENING_SUITES[config.opening_suite_version],
            histories=BUCEPHALUS_OPENING_HISTORIES[
                config.opening_suite_version
            ],
            canonical_sha256=config.opening_suite_sha256,
            generator=generator,
            content_addressed=(
                config.opening_suite_version
                in {
                    BUCEPHALUS_FAIR_OPENING_SUITE_VERSION,
                    INITIAL_POSITION_SUITE_VERSION,
                }
            ),
        )

    verify_seeded_opening_suite(opening_suite)
    payload = opening_suite.as_dict()
    digest = _canonical_sha256(payload)
    if opening_suite.version != config.opening_suite_version:
        raise ValueError("opening suite version does not match match configuration")
    if digest != config.opening_suite_sha256:
        raise ValueError("opening suite digest does not match match configuration")
    cases_by_id = {case.case_id: case for case in opening_suite.cases}
    if set(config.opening_case_ids) != set(cases_by_id):
        raise ValueError(
            "opening suite cases do not match configured opening case ids"
        )
    histories = MappingProxyType(
        {history.case_id: history.series for history in opening_suite.histories}
    )
    if config.opening_qualification is not None:
        if any(case.series_number != 3 for case in opening_suite.cases):
            raise ValueError("engaged opening suite must start immediately after S2")
        qualification = config.opening_qualification
        expected_suite, expected_qualification = build_engaged_opening_suite(
            seed=qualification.candidate_seed,
            count=qualification.selected_count,
            candidate_pool_count=qualification.candidate_pool_count,
            max_frontier_states=qualification.candidate_max_frontier_states,
        )
        if qualification != expected_qualification:
            raise ValueError("opening qualification receipt is not reproducible")
        if opening_suite.as_dict() != expected_suite.as_dict():
            raise ValueError("engaged opening suite is not the qualified first-N set")
    return _OpeningContext(
        cases=opening_suite.cases,
        histories=histories,
        canonical_sha256=digest,
        generator=MappingProxyType(
            {
                "seed": opening_suite.seed,
                "count": len(opening_suite.cases),
                "min_series": opening_suite.min_series,
                "max_series": opening_suite.max_series,
                "max_frontier_states": opening_suite.max_frontier_states,
            }
        ),
        content_addressed=True,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stable_digest(*parts: object) -> str:
    encoded = "|".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_seed(*parts: object) -> int:
    return int(_stable_digest(*parts)[:16], 16) & 0x7FFFFFFF


def _profile_canonical_sha256(profile: EngineProfile) -> str:
    return _canonical_sha256(profile.as_dict())


def _approved_best_settings_profile(profile: EngineProfile) -> bool:
    return (
        profile.profile_id == APPROVED_BEST_SETTINGS_LOCAL_PROFILE_ID
        and _profile_canonical_sha256(profile)
        == APPROVED_BEST_SETTINGS_LOCAL_PROFILE_SHA256
    )


def _runtime_provenance() -> dict[str, str]:
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_chess_version": chess.__version__,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
    }


def _module_binary_identity(candidate: object | None) -> dict[str, Any]:
    if candidate is None:
        return {"loaded": False, "path": None, "sha256": None, "source_identity": None}
    raw_path = getattr(candidate, "__file__", None)
    path = Path(raw_path).resolve() if isinstance(raw_path, str) else None
    digest: str | None = None
    if path is not None and path.is_file():
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
    return {
        "loaded": True,
        "path": str(path) if path is not None else None,
        "sha256": digest,
        "source_identity": getattr(candidate, "SOURCE_IDENTITY", None),
    }


def _local_backend_provenance() -> dict[str, Any]:
    from benchmarks.release_engine_gate import native_runtime_identity
    from scottish_progressive import evaluation, search as search_module, series_mate

    relevant_environment = (
        "SPC_DISABLE_NATIVE",
        "SPC_DISABLE_NATIVE_MATE",
        "SPC_NATIVE_NEURAL_S3",
        "PYTHONHASHSEED",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
    )
    return {
        "evaluation_native_available": evaluation.native_acceleration_available(),
        "evaluation_native_module": _module_binary_identity(evaluation._native_eval),
        "mate_native_runtime_identity": series_mate.native_mate_runtime_identity(),
        "mate_native_module": _module_binary_identity(series_mate._native_mate),
        "native_subtree_enabled": bool(search_module.NATIVE_SUBTREE_ENABLED),
        "release_native_runtime": native_runtime_identity(),
        "environment": {
            name: os.environ.get(name) for name in relevant_environment
        },
    }


def _resource_execution_controls(resources: ResourceBudget) -> dict[str, Any]:
    return {
        "workers": resources.workers,
        "requested_workers": resources.requested_workers,
        "memory_per_worker_bytes": resources.memory_per_worker_bytes,
        "reserved_memory_bytes": resources.reserved_memory_bytes,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_external_build_receipt(
    receipt_path: str | Path,
    *,
    external_spec: BucephalusSpec,
    executable: Path,
    executable_hash: str,
) -> dict[str, Any]:
    try:
        path = Path(receipt_path).expanduser().resolve(strict=True)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read external build receipt: {path}") from error
    if not isinstance(payload, dict):
        raise ValueError("external build receipt must be a JSON object")
    if payload.get("schema") != BUCEPHALUS_BUILD_RECEIPT_SCHEMA:
        raise ValueError("unsupported external build receipt schema")

    opponent = payload.get("opponent")
    instrumentation = payload.get("instrumentation")
    output = payload.get("output")
    if not all(isinstance(item, dict) for item in (opponent, instrumentation, output)):
        raise ValueError("external build receipt is missing identity sections")
    assert isinstance(opponent, dict)
    assert isinstance(instrumentation, dict)
    assert isinstance(output, dict)

    receipt_upstream = opponent.get("upstream_commit")
    if receipt_upstream != external_spec.upstream_commit:
        raise ValueError(
            "external build receipt upstream commit does not match --upstream-commit"
        )
    if output.get("sha256") != executable_hash:
        raise ValueError(
            "external build receipt output SHA-256 does not match the executable"
        )
    if output.get("bytes") != executable.stat().st_size:
        raise ValueError(
            "external build receipt output size does not match the executable"
        )

    patch_relative = instrumentation.get("patch_path")
    patch_expected_hash = instrumentation.get("patch_sha256")
    if not isinstance(patch_relative, str) or not isinstance(
        patch_expected_hash, str
    ):
        raise ValueError("external build receipt has no pinned instrumentation patch")
    repository = Path(__file__).resolve().parent.parent
    patch = (repository / patch_relative).resolve()
    try:
        patch.relative_to(repository)
    except ValueError as error:
        raise ValueError("external build receipt patch escapes the repository") from error
    if not patch.is_file() or _file_sha256(patch) != patch_expected_hash:
        raise ValueError("external build receipt instrumentation patch mismatch")

    canonical_sha256 = _canonical_sha256(payload)
    approved = (
        canonical_sha256 == APPROVED_BUCEPHALUS_BUILD_RECEIPT_SHA256
        and executable_hash == APPROVED_BUCEPHALUS_EXECUTABLE_SHA256
        and external_spec.sha256 == APPROVED_BUCEPHALUS_EXECUTABLE_SHA256
        and receipt_upstream == APPROVED_BUCEPHALUS_UPSTREAM_COMMIT
        and patch_expected_hash == APPROVED_BUCEPHALUS_PATCH_SHA256
    )
    return {
        "canonical_sha256": canonical_sha256,
        "approved_for_named_bucephalus_claim": approved,
        "receipt": payload,
    }


def _run_identity_snapshot() -> dict[str, Any]:
    return {
        "engine_version": ENGINE_VERSION,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "git": _git_source_provenance(),
        "runtime": _runtime_provenance(),
        "backend": _local_backend_provenance(),
        "benchmark_harness": _benchmark_harness_identity(),
    }


def _identity_drift(start: Mapping[str, Any]) -> dict[str, Any]:
    try:
        end = _run_identity_snapshot()
    except Exception as error:
        return {
            "detected": True,
            "changed_fields": ["identity-recheck-failed"],
            "end_identity": None,
            "recheck_error": f"{type(error).__name__}: {error}",
        }
    compared_fields = (
        "engine_version",
        "source_fingerprint",
        "git",
        "backend",
        "benchmark_harness",
    )
    changed_fields = [field for field in compared_fields if start[field] != end[field]]
    return {
        "detected": bool(changed_fields),
        "changed_fields": changed_fields,
        "end_identity": end,
    }


def _benchmark_harness_identity() -> dict[str, Any]:
    repository = Path(__file__).resolve().parent.parent
    relative_paths = (
        ".gitattributes",
        "benchmarks/__init__.py",
        "benchmarks/bucephalus_timed_adapter.py",
        "benchmarks/bucephalus_fair_rematch.py",
        "benchmarks/protocols/bucephalus_fair_openings_v1.json",
        BUCEPHALUS_BUILD_RECEIPT_RELATIVE_PATH,
        "benchmarks/patches/bucephalus-0e11fcdc-stdout-flush.patch",
    )
    records: list[dict[str, Any]] = []
    for relative in relative_paths:
        path = repository / relative
        raw = path.read_bytes()
        records.append(
            {
                "path": relative,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return {
        "schema": "spc-bucephalus-fair-harness-identity-v1",
        "files": records,
        "artifact_set_sha256": _canonical_sha256({"files": records}),
    }


@dataclass(frozen=True, slots=True)
class ExternalMatchConfig:
    """Versioned controls for local-profile versus Bucephalus evidence."""

    pairs: int = 10
    seed: int = 20260820
    match_intent: str = FAIR_EQUAL_WALL_MATCH_INTENT
    opening_policy: str = OPENING_POLICY_FIXED_SUITE
    opening_suite_version: str = OPENING_SUITE_VERSION
    opening_suite_canonical_sha256: str | None = None
    opening_qualification: OpeningQualification | None = None
    opening_case_ids: tuple[str, ...] = tuple(
        case.case_id for case in OPENING_SUITE
    )
    local_depth_series: int = 2
    local_max_series_per_node: int = 32
    local_native_threads: int = 1
    local_max_generation_positions: int = 250_000
    local_max_game_work_positions: int = 5_000_000
    requested_match_workers: int = 1
    external_ply_policy: str = EXTERNAL_PLY_POLICY
    external_lookahead_micro_plies: int = 0
    external_wall_timeout_seconds: float = 10.0
    common_wall_timeout_seconds: float | None = None
    emergency_max_series: int = 18

    def __post_init__(self) -> None:
        supported_match_intents = {
            FAIR_EQUAL_WALL_MATCH_INTENT,
            BEST_SETTINGS_MATCH_INTENT,
        }
        if self.match_intent not in supported_match_intents:
            raise ValueError(f"unsupported match intent {self.match_intent}")
        if self.opening_policy not in {
            OPENING_POLICY_FIXED_SUITE,
            OPENING_POLICY_INITIAL,
        }:
            raise ValueError(f"unsupported opening policy {self.opening_policy}")
        initial_position_mode = self.opening_policy == OPENING_POLICY_INITIAL
        if initial_position_mode and (
            self.opening_suite_version != INITIAL_POSITION_SUITE_VERSION
            or self.opening_case_ids != (INITIAL_POSITION_CASE.case_id,)
            or self.opening_qualification is not None
        ):
            raise ValueError(
                "initial-position mode requires its frozen empty-history case "
                "and forbids opening qualification"
            )
        if not initial_position_mode and (
            self.opening_suite_version == INITIAL_POSITION_SUITE_VERSION
        ):
            raise ValueError("initial-position suite requires its explicit opening policy")
        built_in_suite = self.opening_suite_version in BUCEPHALUS_OPENING_SUITES
        if (
            not built_in_suite
            and not self.opening_suite_version.startswith(
                "spc-neutral-seeded-openings-v1-"
            )
        ):
            raise ValueError(
                f"unsupported opening suite {self.opening_suite_version}"
            )
        expected_suite_digest = BUCEPHALUS_OPENING_SUITE_SHA256.get(
            self.opening_suite_version
        )
        if expected_suite_digest is not None:
            if (
                self.opening_suite_canonical_sha256 is not None
                and self.opening_suite_canonical_sha256 != expected_suite_digest
            ):
                raise ValueError("built-in opening suite digest mismatch")
        elif (
            self.opening_suite_canonical_sha256 is None
            or len(self.opening_suite_canonical_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.opening_suite_canonical_sha256.lower()
            )
        ):
            raise ValueError(
                "custom opening suite requires its canonical SHA-256"
            )
        available = (
            {
                case.case_id
                for case in BUCEPHALUS_OPENING_SUITES[
                    self.opening_suite_version
                ]
            }
            if built_in_suite
            else set(self.opening_case_ids)
        )
        if not self.opening_case_ids:
            raise ValueError("opening_case_ids cannot be empty")
        if len(set(self.opening_case_ids)) != len(self.opening_case_ids):
            raise ValueError("opening_case_ids cannot contain duplicates")
        if not set(self.opening_case_ids) <= available:
            raise ValueError("opening_case_ids must name active canonical openings")
        if not 1 <= self.pairs or (
            not initial_position_mode and self.pairs > len(self.opening_case_ids)
        ):
            raise ValueError(
                "pairs must be positive and fixed-suite pairs cannot exceed unique openings"
            )
        if not 1 <= self.local_depth_series <= 8:
            raise ValueError("local_depth_series must be between 1 and 8")
        if not 1 <= self.local_max_series_per_node <= 512:
            raise ValueError(
                "local_max_series_per_node must be between 1 and 512"
            )
        if (
            type(self.local_native_threads) is not int
            or not 1 <= self.local_native_threads <= 64
        ):
            raise ValueError("local_native_threads must be between 1 and 64")
        if self.local_max_generation_positions < 1:
            raise ValueError("local_max_generation_positions must be positive")
        if self.local_max_game_work_positions < 1:
            raise ValueError("local_max_game_work_positions must be positive")
        if type(self.requested_match_workers) is not int or self.requested_match_workers < 1:
            raise ValueError("requested_match_workers must be a positive integer")
        supported_ply_policies = {
            EXTERNAL_PLY_POLICY,
            TIMED_ITERATIVE_PLY_POLICY,
        }
        if self.external_ply_policy not in supported_ply_policies:
            raise ValueError(
                f"unsupported external ply policy {self.external_ply_policy}"
            )
        if (
            self.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY
            and self.common_wall_timeout_seconds is None
        ):
            raise ValueError(
                "timed iterative Bucephalus search requires a common wall timeout"
            )
        if (
            self.common_wall_timeout_seconds is not None
            and self.external_ply_policy != TIMED_ITERATIVE_PLY_POLICY
        ):
            raise ValueError(
                "a common wall timeout requires timed iterative Bucephalus search"
            )
        if (
            self.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY
            and self.external_lookahead_micro_plies != 0
        ):
            raise ValueError(
                "external lookahead is not used by timed iterative Bucephalus search"
            )
        if not 0 <= self.external_lookahead_micro_plies <= 20:
            raise ValueError(
                "external_lookahead_micro_plies must be between 0 and 20"
            )
        if (
            not math.isfinite(self.external_wall_timeout_seconds)
            or self.external_wall_timeout_seconds <= 0
        ):
            raise ValueError(
                "external_wall_timeout_seconds must be finite and positive"
            )
        if self.common_wall_timeout_seconds is not None:
            if (
                not math.isfinite(self.common_wall_timeout_seconds)
                or self.common_wall_timeout_seconds <= 0
            ):
                raise ValueError(
                    "common_wall_timeout_seconds must be finite and positive"
                )
            if not math.isclose(
                self.external_wall_timeout_seconds,
                self.common_wall_timeout_seconds,
                rel_tol=0.0,
                abs_tol=0.0,
            ):
                raise ValueError(
                    "external wall timeout must equal the common wall timeout"
                )
        if not 1 <= self.emergency_max_series <= BUCEPHALUS_MAX_PLY:
            raise ValueError(
                f"emergency_max_series must be between 1 and "
                f"{BUCEPHALUS_MAX_PLY}"
            )
        if (
            self.match_intent == BEST_SETTINGS_MATCH_INTENT
            and not initial_position_mode
            and self.opening_suite_version
            == BUCEPHALUS_FAIR_OPENING_SUITE_VERSION
        ):
            raise ValueError(
                "best-settings matches require a fresh content-addressed "
                "opening suite"
            )
        if self.match_intent == BEST_SETTINGS_MATCH_INTENT:
            if (
                not initial_position_mode
                and (
                    self.opening_qualification is None
                    or self.opening_qualification.version
                    != ENGAGED_OPENING_QUALIFICATION_VERSION
                    or self.opening_qualification.target_series != 3
                    or self.opening_qualification.selected_count != self.pairs
                )
            ):
                raise ValueError(
                    "best-settings matches require the engaged S3 opening "
                    "qualification receipt"
                )
            if (
                self.external_ply_policy != TIMED_ITERATIVE_PLY_POLICY
                or self.common_wall_timeout_seconds is None
            ):
                raise ValueError(
                    "best-settings matches require timed iterative Bucephalus "
                    "search under a common wall control"
                )
            if self.common_wall_timeout_seconds <= 30.0:
                raise ValueError(
                    "best-settings matches cannot relabel the legacy 30-second "
                    "control or a weaker control"
                )
            if self.requested_match_workers != 1:
                raise ValueError(
                    "best-settings matches require exactly one match worker"
                )
            if (
                self.local_depth_series != 8
                or self.local_max_series_per_node != 32
                or self.local_native_threads != 16
                or self.local_max_generation_positions != 4_000_000_000
                or self.local_max_game_work_positions != 100_000_000_000
                or self.common_wall_timeout_seconds != 120.0
                or self.external_wall_timeout_seconds != 120.0
                or self.emergency_max_series != 18
            ):
                raise ValueError(
                    "best-settings matches require frozen D8/width32/16-thread/"
                    "4B-search/100B-game/120-second controls"
                )

    def external_search_ply(self, series_number: int) -> int:
        if self.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY:
            return BUCEPHALUS_MAX_PLY
        return series_number + self.external_lookahead_micro_plies

    @property
    def expected_external_adapter_version(self) -> str:
        if self.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY:
            return BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION
        return BUCEPHALUS_ADAPTER_VERSION

    @property
    def opening_suite_sha256(self) -> str:
        return (
            self.opening_suite_canonical_sha256
            or BUCEPHALUS_OPENING_SUITE_SHA256[self.opening_suite_version]
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "pairs": self.pairs,
            "games": self.pairs * 2,
            "seed": self.seed,
            "match_intent": self.match_intent,
            "opening_policy": self.opening_policy,
            "opening_series_played": 0 if self.opening_policy == OPENING_POLICY_INITIAL else None,
            "engine_play_begins_series": 1 if self.opening_policy == OPENING_POLICY_INITIAL else None,
            "opening_suite_version": self.opening_suite_version,
            "opening_suite_canonical_sha256": self.opening_suite_sha256,
            "opening_qualification": (
                self.opening_qualification.as_dict()
                if self.opening_qualification is not None
                else None
            ),
            "opening_case_ids": list(self.opening_case_ids),
            "local_limits": {
                "depth_series": self.local_depth_series,
                "branch_cap_complete_series_per_node": (
                    self.local_max_series_per_node
                ),
                "native_threads": self.local_native_threads,
                "native_threads_policy": (
                    "frozen-16-thread-stable-host-configuration"
                    if self.match_intent == BEST_SETTINGS_MATCH_INTENT
                    and self.local_native_threads == 16
                    else "explicit-configured-value"
                ),
                "max_work_positions_per_search": (
                    self.local_max_generation_positions
                ),
                "max_work_positions_per_game": (
                    self.local_max_game_work_positions
                ),
                "time_limit_seconds": self.common_wall_timeout_seconds,
                "deadline_result": (
                    "deepest-completed-or-legal-move-only-liveness-fallback"
                    if self.common_wall_timeout_seconds is not None
                    else "technical-incomplete-*"
                ),
                "collect_all_root_scores": False,
                "root_score_mode": "best-only-play-optimized",
                "fresh_searcher_each_series": True,
            },
            "external_limits": {
                "ply_policy": self.external_ply_policy,
                "formula": (
                    "maximum-supported-micro-ply"
                    if self.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY
                    else "series_number + fixed_lookahead_micro_plies"
                ),
                "fixed_lookahead_micro_plies": (
                    self.external_lookahead_micro_plies
                ),
                "maximum_supported_micro_plies": BUCEPHALUS_MAX_PLY,
                "wall_watchdog_seconds_per_call": (
                    self.external_wall_timeout_seconds
                ),
                "deadline_result": (
                    "bucephalus-only-live-complete-or-validated-stitched-or-anchor"
                    if self.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY
                    else "technical-incomplete-*"
                ),
                "completion_controller": (
                    {
                        "version": BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
                        "label": "Bucephalus-output-only completion controller",
                        "anchor_reserve_seconds": 12.0,
                        "soft_checkpoint_fraction": 0.75,
                        "hard_wall_seconds": self.common_wall_timeout_seconds,
                        "cleanup_reserve_seconds": 1.0,
                        "restart_semantics": "fresh-process-replay-clears-transposition-table",
                        "moves_source": "pinned-bucephalus-output-only",
                        "anchor": {
                            "method": "repeated-exact-ply1",
                            "phase_fraction": 0.10,
                            "phase_ceiling_seconds": 12.0,
                        },
                        "deep": {
                            "requested_ply": BUCEPHALUS_MAX_PLY,
                            "soft_checkpoint_fraction_of_searchable_wall": 0.75,
                            "complete_at_soft_action": "same-pid-continue-to-hard-deadline",
                            "incomplete_at_soft_action": "kill-drain-then-suffix",
                        },
                        "suffix": {
                            "method": "repeated-live-max-ply-then-exact-ply1-rescue",
                            "allocation": "remaining-wall-divided-by-remaining-root-moves",
                            "complete_at_soft_action": "same-pid-continue-to-global-hard-deadline",
                            "restart_clears_transposition_table": True,
                        },
                        "fallback": "precomputed-bucephalus-ply1-anchor",
                    }
                    if self.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY
                    else None
                ),
                "node_limit": None,
                "native_time_control": None,
                "threads": 1,
                "thread_control": "none-in-upstream-bucephalus",
                "timeout_without_complete_iteration": (
                    "validated-suffix-or-bucephalus-anchor; technical-only-if-anchor-unavailable"
                    if self.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY
                    else "technical-incomplete-*"
                ),
            },
            "common_control": {
                "enabled": self.common_wall_timeout_seconds is not None,
                "wall_seconds_per_move": self.common_wall_timeout_seconds,
                "policy": (
                    "equal-end-to-end-call-wall-bucephalus-output-only-controller-v4"
                    if self.common_wall_timeout_seconds is not None
                    else None
                ),
                "equal_depth_claim": False,
                "equal_work_claim": False,
                "equal_search_time_claim": False,
                "clock_scope": (
                    "local analyze call versus Bucephalus process start, "
                    "history replay, and search"
                    if self.common_wall_timeout_seconds is not None
                    else None
                ),
                "wall_overrun_grace_seconds": (
                    COMMON_WALL_OVERRUN_GRACE_SECONDS
                    if self.common_wall_timeout_seconds is not None
                    else None
                ),
            },
            "emergency_max_series": self.emergency_max_series,
            "requested_match_workers": self.requested_match_workers,
            "emergency_max_series_kind": "technical-watchdog-not-chess-rule",
        }


@dataclass(frozen=True, slots=True)
class ExternalGameJob:
    game_id: str
    pair_id: str
    pair_index: int
    swap_index: int
    opening: OpeningCase
    history: SeriesHistory
    local_color: chess.Color
    local_profile: EngineProfile
    external_spec: BucephalusSpec
    config: ExternalMatchConfig


@dataclass(frozen=True, slots=True)
class ExternalGameRecord:
    game_id: str
    pair_id: str
    pair_index: int
    swap_index: int
    opening_case_id: str
    local_color: str
    external_color: str
    result: str
    terminal_reason: str
    winner: str | None
    winner_color: str | None
    technical_failure_owner: str | None
    start_pfen: str
    final_pfen: str
    series_played: int
    local_work_positions: int
    external_calls: int
    trace: tuple[dict[str, Any], ...]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "trace": [dict(item) for item in self.trace],
        }


def _ordered_openings(
    config: ExternalMatchConfig,
    opening_cases: Sequence[OpeningCase] | None = None,
) -> tuple[OpeningCase, ...]:
    if config.opening_policy == OPENING_POLICY_INITIAL:
        if opening_cases is not None and tuple(opening_cases) != (INITIAL_POSITION_CASE,):
            raise ValueError("initial-position mode forbids supplied opening fixtures")
        return (INITIAL_POSITION_CASE,) * config.pairs
    by_id = {
        case.case_id: case
        for case in (
            opening_cases
            if opening_cases is not None
            else BUCEPHALUS_OPENING_SUITES[config.opening_suite_version]
        )
    }
    cases = [by_id[case_id] for case_id in config.opening_case_ids]
    random.Random(
        _stable_seed(
            EXTERNAL_MATCH_FORMAT,
            config.opening_suite_version,
            config.seed,
            "opening-order",
        )
    ).shuffle(cases)
    return tuple(cases[: config.pairs])


def _git_source_provenance() -> dict[str, Any]:
    repository = Path(__file__).resolve().parent.parent

    def git(*arguments: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=repository,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                timeout=10.0,
                check=False,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    return {
        "repository_detected": head is not None,
        "head_commit": head,
        "tracked_worktree_clean": status == "" if status is not None else None,
        "tracked_status_sha256": (
            hashlib.sha256(status.encode("utf-8")).hexdigest()
            if status is not None
            else None
        ),
    }


def _build_jobs(
    local_profile: EngineProfile,
    external_spec: BucephalusSpec,
    config: ExternalMatchConfig,
    *,
    opening_suite: SeededOpeningSuite | None = None,
    opening_context: _OpeningContext | None = None,
) -> tuple[ExternalGameJob, ...]:
    resolved_openings = opening_context or _resolve_opening_context(
        config, opening_suite
    )
    if config.opening_policy == OPENING_POLICY_INITIAL and (
        resolved_openings.cases != (INITIAL_POSITION_CASE,)
        or dict(resolved_openings.histories) != {INITIAL_POSITION_CASE.case_id: ()}
    ):
        raise ValueError(
            "initial-position mode requires exactly one standard initial case "
            "with empty canonical history"
        )
    config_json = json.dumps(
        config.as_dict(), sort_keys=True, separators=(",", ":")
    )
    match_id = "external-" + _stable_digest(
        EXTERNAL_MATCH_FORMAT,
        ENGINE_VERSION,
        ENGINE_SOURCE_FINGERPRINT,
        local_profile.profile_id,
        _profile_canonical_sha256(local_profile),
        external_spec.sha256,
        external_spec.upstream_commit,
        config_json,
    )[:20]
    jobs: list[ExternalGameJob] = []
    histories = resolved_openings.histories
    for pair_index, opening in enumerate(
        _ordered_openings(config, resolved_openings.cases)
    ):
        history = histories[opening.case_id]
        pair_id = _stable_digest(match_id, pair_index, opening.case_id)[:24]
        for swap_index, local_color in enumerate((chess.WHITE, chess.BLACK)):
            game_id = _stable_digest(
                pair_id,
                swap_index,
                "white" if local_color == chess.WHITE else "black",
            )[:32]
            jobs.append(
                ExternalGameJob(
                    game_id=game_id,
                    pair_id=pair_id,
                    pair_index=pair_index,
                    swap_index=swap_index,
                    opening=opening,
                    history=history,
                    local_color=local_color,
                    local_profile=local_profile,
                    external_spec=external_spec,
                    config=config,
                )
            )
    return tuple(jobs)


def _result_string(winner: chess.Color) -> str:
    return "1-0" if winner == chess.WHITE else "0-1"


def _color_name(color: chess.Color) -> str:
    return "white" if color == chess.WHITE else "black"


def _terminal_winner(result: SeriesResult, mover: chess.Color) -> chess.Color | None:
    if result.outcome != Outcome.CHECKMATE:
        return None
    return mover if result.ended_by_check else not mover


def _boundary_terminal(state: ProgressiveState) -> SeriesResult | None:
    try:
        return play_series(state, ())
    except SeriesLegalityError:
        return None


def _record_capacity_supported(history: SeriesHistory) -> bool:
    for series_number, moves in enumerate(history, 1):
        if not moves:
            continue
        final_index = series_number * (series_number - 1) // 2 + len(moves) - 1
        if final_index >= BUCEPHALUS_MAX_GAME_RECORD:
            return False
    return True


def _root_legal_move_count(state: ProgressiveState) -> int:
    board = state.board.copy(stack=False)
    saved_ep = board.ep_square
    moves: set[str] = set()
    try:
        board.ep_square = None
        moves.update(move.uci() for move in board.legal_moves)
        for target in state.ep_targets:
            board.ep_square = target
            moves.update(
                move.uci()
                for move in board.legal_moves
                if board.is_en_passant(move)
            )
    finally:
        board.ep_square = saved_ep
    return len(moves)


def _revalidate_selected(
    state: ProgressiveState, selected: SeriesResult
) -> SeriesResult:
    try:
        authoritative = play_series(state, selected.moves)
    except SeriesLegalityError as error:
        raise ExternalEngineProtocolError(
            f"selected series failed authoritative replay: {error}"
        ) from error
    if (
        authoritative.final_state.position_hash
        != selected.final_state.position_hash
        or authoritative.outcome != selected.outcome
        or authoritative.ended_by_check != selected.ended_by_check
    ):
        raise ExternalEngineProtocolError(
            "selected series metadata disagrees with authoritative replay"
        )
    return authoritative


def _continuation_chain_selects_analysis(
    state: ProgressiveState,
    history: Sequence[Sequence[str]],
    analysis: ExternalAnalysis,
    wall_seconds: float,
) -> bool:
    if wall_seconds < 12.0:
        return True
    if (
        not analysis.continuation_stages
        or analysis.global_deadline_seconds != wall_seconds
        or analysis.terminal_stage_score != analysis.score_text
        or analysis.process_count != len(analysis.continuation_stages)
        or type(analysis.selected_terminal_stage_index) is not int
        or not 1
        <= analysis.selected_terminal_stage_index
        <= len(analysis.continuation_stages)
    ):
        return False
    anchor: tuple[str, ...] = ()
    stitched: tuple[str, ...] = ()
    anchor_terminal_score: str | None = None
    stitched_terminal_score: str | None = None
    elapsed = 0.0
    for index, stage in enumerate(analysis.continuation_stages, 1):
        expected = (
            anchor if stage.purpose == "anchor-ply1"
            else () if stage.purpose in {"deep-max-ply", "deep-refinement"}
            else stitched if stage.purpose in {"suffix-max-ply", "suffix-ply1"}
            else None
        )
        if (
            stage.stage_index != index
            or expected is None
            or stage.starting_prefix != expected
            or stage.elapsed_seconds < 0
            or stage.wall_timeout_seconds <= 0
            or stage.request_script
            != _request_script(
                history, stage.requested_ply, prefix=stage.starting_prefix
            )
        ):
            return False
        if stage.purpose in {"deep-max-ply", "suffix-max-ply"}:
            if (
                type(stage.process_id) is not int
                or stage.process_id <= 0
                or stage.soft_checkpoint_seconds is None
                or stage.hard_deadline_seconds is None
                or not 0 < stage.soft_checkpoint_seconds < stage.hard_deadline_seconds
                or stage.hard_deadline_seconds > wall_seconds
                or stage.wall_timeout_seconds != stage.hard_deadline_seconds
                or stage.stop_reason not in {
                    "soft-checkpoint-incomplete", "hard-deadline", "process-exit"
                }
                or (
                    stage.same_process_continued
                    and stage.stop_reason == "soft-checkpoint-incomplete"
                )
            ):
                return False
        elif (
            stage.soft_checkpoint_seconds is not None
            or stage.hard_deadline_seconds is not None
            or stage.same_process_continued
        ):
            return False
        if (
            stage.stop_reason == "hard-deadline"
            and (not stage.same_process_continued or not stage.deadline_reached)
        ) or (
            stage.stop_reason == "soft-checkpoint-incomplete"
            and (stage.same_process_continued or not stage.deadline_reached)
        ) or (
            stage.stop_reason == "process-exit" and stage.deadline_reached
        ) or (
            stage.stop_reason == "stage-deadline" and not stage.deadline_reached
        ) or (
            stage.process_exit_recovered
            and (
                stage.stop_reason != "process-exit"
                or stage.process_exit_code in (None, 0)
                or not stage.usable
            )
        ):
            return False
        elapsed += stage.elapsed_seconds
        if stage.usable:
            try:
                _validate_output_identity_and_boundary(
                    stage.stdout,
                    state,
                    count_in_series=len(stage.starting_prefix) + 1,
                )
                if stage.purpose in {"anchor-ply1", "suffix-ply1"}:
                    parsed_score, emitted = _parse_requested_ply(stage.stdout, 1)
                    if emitted != stage.emitted_prefix or len(emitted) != 1:
                        return False
                elif stage.purpose == "deep-max-ply":
                    try:
                        parsed_ply, parsed_score, parsed_result = _parse_deepest_completed_ply(
                            stage.stdout,
                            requested_ply=stage.requested_ply,
                            state=state,
                        )
                        emitted = tuple(parsed_result.moves)
                    except ExternalEngineProtocolError:
                        parsed_ply, parsed_score, emitted = _parse_deepest_legal_incomplete_prefix(
                            stage.stdout,
                            requested_ply=stage.requested_ply,
                            state=state,
                        )
                    if parsed_ply != stage.completed_ply or emitted != stage.emitted_prefix:
                        return False
                elif stage.purpose == "deep-refinement":
                    parsed_ply, parsed_score, parsed_result = _parse_deepest_completed_ply(
                        stage.stdout,
                        requested_ply=stage.requested_ply,
                        state=state,
                    )
                    emitted = tuple(parsed_result.moves)
                    if parsed_ply != stage.completed_ply or emitted != stage.emitted_prefix:
                        return False
                else:
                    parsed_ply, parsed_score, emitted, _ = _parse_deepest_continuation_progress(
                        stage.stdout,
                        requested_ply=stage.requested_ply,
                        state=state,
                        prefix=stage.starting_prefix,
                    )
                    if parsed_ply != stage.completed_ply or emitted != stage.emitted_prefix:
                        return False
            except ExternalEngineProtocolError:
                return False
        if stage.usable and stage.purpose == "anchor-ply1":
            anchor += stage.emitted_prefix
            anchor_terminal_score = parsed_score
        elif stage.usable and stage.purpose == "deep-max-ply":
            stitched = stage.emitted_prefix
            stitched_terminal_score = parsed_score
        elif (
            stage.usable
            and stage.purpose == "deep-refinement"
            and analysis.selection_mode == "deep-refined"
        ):
            stitched = stage.emitted_prefix
            stitched_terminal_score = parsed_score
        elif stage.usable and stage.purpose in {"suffix-max-ply", "suffix-ply1"}:
            stitched += stage.emitted_prefix
            stitched_terminal_score = parsed_score
    evidence = (
        anchor
        if analysis.selection_mode in {"anchor-fallback", "anchor-terminal"}
        else stitched
    )
    evidence_score = (
        anchor_terminal_score
        if analysis.selection_mode in {"anchor-fallback", "anchor-terminal"}
        else stitched_terminal_score
    )
    selected_stage = analysis.continuation_stages[
        analysis.selected_terminal_stage_index - 1
    ]
    expected_selected_purpose = (
        "anchor-ply1"
        if analysis.selection_mode in {"anchor-fallback", "anchor-terminal"}
        else "deep-max-ply"
        if analysis.selection_mode == "deep-complete-live"
        else None
    )
    return (
        tuple(analysis.best_series.moves) == evidence
        and analysis.terminal_stage_score == evidence_score
        and selected_stage.usable
        and analysis.terminal_stage_ply == selected_stage.completed_ply
        and (
            expected_selected_purpose is None
            or selected_stage.purpose == expected_selected_purpose
        )
        and elapsed <= wall_seconds + COMMON_WALL_OVERRUN_GRACE_SECONDS
        and analysis.elapsed_seconds + 1e-9 >= elapsed
        and analysis.elapsed_seconds
        <= wall_seconds + COMMON_WALL_OVERRUN_GRACE_SECONDS
        and analysis.deadline_reached
        == any(stage.deadline_reached for stage in analysis.continuation_stages)
        and analysis.process_exit_recovered
        == any(stage.process_exit_recovered for stage in analysis.continuation_stages)
        and analysis.global_deadline_reached
        == (analysis.elapsed_seconds >= wall_seconds)
    )


def _stats_dict(stats: object | None) -> dict[str, Any]:
    if stats is None:
        return {}
    if is_dataclass(stats):
        return asdict(stats)
    try:
        values = vars(stats)
    except TypeError:
        return {}
    return {
        key: value
        for key, value in values.items()
        if value is None or isinstance(value, (bool, int, float, str))
    }


def _technical_record(
    job: ExternalGameJob,
    state: ProgressiveState,
    start_pfen: str,
    trace: Sequence[dict[str, Any]],
    local_work_positions: int,
    external_calls: int,
    reason: str,
    *,
    owner: str | None,
    error: str | None = None,
) -> ExternalGameRecord:
    return ExternalGameRecord(
        game_id=job.game_id,
        pair_id=job.pair_id,
        pair_index=job.pair_index,
        swap_index=job.swap_index,
        opening_case_id=job.opening.case_id,
        local_color=_color_name(job.local_color),
        external_color=_color_name(not job.local_color),
        result="*",
        terminal_reason=reason,
        winner=None,
        winner_color=None,
        technical_failure_owner=owner,
        start_pfen=start_pfen,
        final_pfen=state.pfen,
        series_played=sum(bool(item.get("played")) for item in trace),
        local_work_positions=local_work_positions,
        external_calls=external_calls,
        trace=tuple(trace),
        error=error,
    )


def _terminal_record(
    job: ExternalGameJob,
    state: ProgressiveState,
    start_pfen: str,
    trace: Sequence[dict[str, Any]],
    local_work_positions: int,
    external_calls: int,
    terminal: SeriesResult,
    mover: chess.Color,
) -> ExternalGameRecord:
    winner_color = _terminal_winner(terminal, mover)
    if winner_color is None:
        result = "1/2-1/2"
        winner = None
        winner_name = None
    else:
        result = _result_string(winner_color)
        winner = "local" if winner_color == job.local_color else "bucephalus"
        winner_name = _color_name(winner_color)
    return ExternalGameRecord(
        game_id=job.game_id,
        pair_id=job.pair_id,
        pair_index=job.pair_index,
        swap_index=job.swap_index,
        opening_case_id=job.opening.case_id,
        local_color=_color_name(job.local_color),
        external_color=_color_name(not job.local_color),
        result=result,
        terminal_reason=terminal.outcome.value,
        winner=winner,
        winner_color=winner_name,
        technical_failure_owner=None,
        start_pfen=start_pfen,
        final_pfen=state.pfen,
        series_played=sum(bool(item.get("played")) for item in trace),
        local_work_positions=local_work_positions,
        external_calls=external_calls,
        trace=tuple(trace),
    )


def _play_external_game(
    job: ExternalGameJob,
    *,
    external_adapter: ExternalAdapter | None = None,
    local_analyzer: LocalAnalyzer = analyze,
) -> ExternalGameRecord:
    state = job.opening.state()
    start_pfen = state.pfen
    history = tuple(tuple(series) for series in job.history)
    trace: list[dict[str, Any]] = []
    local_work_positions = 0
    external_calls = 0

    try:
        replayed = replay_series_history(history)
    except ExternalEngineError as error:
        return _technical_record(
            job,
            state,
            start_pfen,
            trace,
            local_work_positions,
            external_calls,
            "technical-opening-replay-invalid",
            owner="shared",
            error=f"{type(error).__name__}: {error}",
        )
    if replayed.position_hash != state.position_hash:
        return _technical_record(
            job,
            state,
            start_pfen,
            trace,
            local_work_positions,
            external_calls,
            "technical-opening-replay-mismatch",
            owner="shared",
        )

    while state.series_number <= job.config.emergency_max_series:
        boundary_terminal = _boundary_terminal(state)
        if boundary_terminal is not None:
            return _terminal_record(
                job,
                state,
                start_pfen,
                trace,
                local_work_positions,
                external_calls,
                boundary_terminal,
                state.board.turn,
            )
        if state.quiet_draw_pending and state.board.is_insufficient_material():
            proven_draw = SeriesResult(
                moves=(),
                san=(),
                final_state=state,
                outcome=Outcome.TEN_SERIES_DRAW,
            )
            return _terminal_record(
                job,
                state,
                start_pfen,
                trace,
                local_work_positions,
                external_calls,
                proven_draw,
                state.board.turn,
            )
        mover = state.board.turn
        before_pfen = state.pfen
        played_by_local = mover == job.local_color
        if not played_by_local and not _record_capacity_supported(history):
            return _technical_record(
                job,
                state,
                start_pfen,
                trace,
                local_work_positions,
                external_calls,
                "technical-external-replay-record-limit",
                owner="bucephalus",
            )
        if (
            not played_by_local
            and _root_legal_move_count(state) >= BUCEPHALUS_MAX_LEGAL_MOVES
        ):
            return _technical_record(
                job,
                state,
                start_pfen,
                trace,
                local_work_positions,
                external_calls,
                "technical-external-root-move-array-limit",
                owner="bucephalus",
            )
        if played_by_local:
            remaining_game_work = (
                job.config.local_max_game_work_positions - local_work_positions
            )
            if remaining_game_work <= 0:
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-local-game-work-budget-exhausted",
                    owner="local",
                )
            search_work_limit = min(
                job.config.local_max_generation_positions,
                remaining_game_work,
            )
            local_search_started = time.perf_counter()
            try:
                analysis = local_analyzer(
                    state,
                    SearchLimits(
                        depth_series=job.config.local_depth_series,
                        max_series_per_node=(
                            job.config.local_max_series_per_node
                        ),
                        time_limit_seconds=(
                            job.config.common_wall_timeout_seconds
                        ),
                        native_threads=job.config.local_native_threads,
                        max_generation_positions=search_work_limit,
                        collect_all_root_scores=False,
                    ),
                    profile=job.local_profile,
                )
            except Exception as error:
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-local-engine-exception",
                    owner="local",
                    error=f"{type(error).__name__}: {error}",
                )
            local_wall_elapsed = time.perf_counter() - local_search_started
            stats = getattr(analysis, "stats", None)
            search_work = int(
                getattr(stats, "work_positions", getattr(stats, "generation_positions", 0))
            )
            analysis_work_limit_reached = bool(
                getattr(analysis, "work_limit_reached", False)
            )
            hard_work_reserve_reached = analysis_work_limit_reached and (
                search_work >= search_work_limit
            )
            local_work_positions += search_work
            selected = getattr(analysis, "best_series", None)
            attempted_trace: dict[str, Any] = {
                "series_number": state.series_number,
                "side": _color_name(mover),
                "engine": "local",
                "profile_id": job.local_profile.profile_id,
                "before_pfen": before_pfen,
                "selected_series": (
                    selected.machine_notation if selected is not None else None
                ),
                "selected_notation": (
                    selected.notation if selected is not None else None
                ),
                "score_white_heuristic_points": getattr(analysis, "score", None),
                "requested_depth_series": job.config.local_depth_series,
                "completed_depth_series": getattr(
                    analysis, "completed_depth", 0
                ),
                "branch_cap": job.config.local_max_series_per_node,
                "native_threads": job.config.local_native_threads,
                "search_work_limit": search_work_limit,
                "search_work_positions": search_work,
                "wall_budget_seconds": (
                    job.config.common_wall_timeout_seconds
                ),
                "local_wall_elapsed_seconds": local_wall_elapsed,
                "engine_reported_elapsed_seconds": getattr(
                    analysis, "elapsed_seconds", None
                ),
                "promotion_mate_positions": int(
                    getattr(stats, "promotion_mate_positions", 0)
                ),
                "promotion_mate_setup_states": int(
                    getattr(stats, "promotion_mate_setup_states", 0)
                ),
                "promotion_mate_candidates": int(
                    getattr(stats, "promotion_mate_candidates", 0)
                ),
                "promotion_mate_completion_probes": int(
                    getattr(stats, "promotion_mate_completion_probes", 0)
                ),
                "promotion_mate_mates": int(
                    getattr(stats, "promotion_mate_mates", 0)
                ),
                "promotion_mate_limit_hits": int(
                    getattr(stats, "promotion_mate_limit_hits", 0)
                ),
                "promotion_mate_replay_rejects": int(
                    getattr(stats, "promotion_mate_replay_rejects", 0)
                ),
                "game_local_work_positions": local_work_positions,
                "work_limit_reached": analysis_work_limit_reached,
                "hard_work_reserve_reached": hard_work_reserve_reached,
                "internal_selective_limit_reached": (
                    analysis_work_limit_reached and not hard_work_reserve_reached
                ),
                "timed_out": getattr(analysis, "timed_out", False),
                "deadline_completed_iteration_used": False,
                "move_only_liveness_fallback": False,
                "exact_width": getattr(analysis, "exact_width", False),
                "root_scores_complete": getattr(
                    analysis, "root_scores_complete", False
                ),
                "stats": _stats_dict(stats),
                "played": False,
            }
            if (
                job.config.common_wall_timeout_seconds is not None
                and local_wall_elapsed
                > job.config.common_wall_timeout_seconds
                + COMMON_WALL_OVERRUN_GRACE_SECONDS
            ):
                trace.append(attempted_trace)
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-local-common-wall-overrun",
                    owner="local",
                )
            if (
                job.config.common_wall_timeout_seconds is not None
                and hard_work_reserve_reached
            ):
                trace.append(attempted_trace)
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-local-hard-work-reserve-reached",
                    owner="local",
                )
            if getattr(analysis, "timed_out", False):
                completed_depth = int(getattr(analysis, "completed_depth", 0))
                if (
                    job.config.common_wall_timeout_seconds is None
                    or selected is None
                ):
                    trace.append(attempted_trace)
                    reason = (
                        "technical-local-deadline-no-complete-iteration"
                        if job.config.common_wall_timeout_seconds is not None
                        else "technical-local-unexpected-timeout"
                    )
                    return _technical_record(
                        job,
                        state,
                        start_pfen,
                        trace,
                        local_work_positions,
                        external_calls,
                        reason,
                        owner="local",
                )
                if completed_depth >= 1:
                    attempted_trace["deadline_completed_iteration_used"] = True
                else:
                    attempted_trace["move_only_liveness_fallback"] = True
            elif (
                job.config.common_wall_timeout_seconds is not None
                and selected is not None
                and int(getattr(analysis, "completed_depth", 0)) < 1
            ):
                attempted_trace["move_only_liveness_fallback"] = True
            if selected is None:
                if (
                    getattr(analysis, "proof", None) == "draw"
                    and getattr(analysis, "adjudication_status", None)
                    == "proven-draw-no-mating-material"
                ):
                    proven_draw = SeriesResult(
                        moves=(),
                        san=(),
                        final_state=state,
                        outcome=Outcome.TEN_SERIES_DRAW,
                    )
                    trace.append(attempted_trace)
                    return _terminal_record(
                        job,
                        state,
                        start_pfen,
                        trace,
                        local_work_positions,
                        external_calls,
                        proven_draw,
                        mover,
                    )
                trace.append(attempted_trace)
                reason = (
                    "technical-local-work-limit-no-series"
                    if getattr(analysis, "work_limit_reached", False)
                    else "technical-local-no-series"
                )
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    reason,
                    owner="local",
                )
        else:
            requested_ply = job.config.external_search_ply(state.series_number)
            attempted_trace = {
                "series_number": state.series_number,
                "side": _color_name(mover),
                "engine": "bucephalus",
                "before_pfen": before_pfen,
                "selected_series": None,
                "requested_micro_ply": requested_ply,
                "ply_policy": job.config.external_ply_policy,
                "fixed_lookahead_micro_plies": (
                    job.config.external_lookahead_micro_plies
                ),
                "wall_watchdog_seconds": (
                    job.config.external_wall_timeout_seconds
                ),
                "played": False,
            }
            if requested_ply > BUCEPHALUS_MAX_PLY:
                trace.append(attempted_trace)
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-external-micro-ply-limit",
                    owner="bucephalus",
                )
            external_calls += 1
            external_call_started = time.perf_counter()
            try:
                selected_external_adapter = external_adapter
                if selected_external_adapter is None:
                    selected_external_adapter = (
                        analyze_bucephalus_timed_iterative
                        if job.config.external_ply_policy
                        == TIMED_ITERATIVE_PLY_POLICY
                        else analyze_bucephalus
                    )
                if (
                    job.config.external_ply_policy
                    == TIMED_ITERATIVE_PLY_POLICY
                ):
                    external_analysis = selected_external_adapter(
                        state,
                        history,
                        job.external_spec,
                        wall_timeout_seconds=(
                            job.config.external_wall_timeout_seconds
                        ),
                    )
                else:
                    external_analysis = selected_external_adapter(
                        state,
                        history,
                        job.external_spec,
                        search_ply=requested_ply,
                        wall_timeout_seconds=(
                            job.config.external_wall_timeout_seconds
                        ),
                    )
            except ExternalEngineTimeout as error:
                trace.append(attempted_trace)
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-external-timeout",
                    owner="bucephalus",
                    error=f"{type(error).__name__}: {error}",
                )
            except ExternalEngineConfigurationError as error:
                trace.append(attempted_trace)
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-external-configuration-or-replay-limit",
                    owner="bucephalus",
                    error=f"{type(error).__name__}: {error}",
                )
            except ExternalEngineProtocolError as error:
                trace.append(attempted_trace)
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-external-protocol",
                    owner="bucephalus",
                    error=f"{type(error).__name__}: {error}",
                )
            except ExternalEngineError as error:
                trace.append(attempted_trace)
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-external-adapter",
                    owner="bucephalus",
                    error=f"{type(error).__name__}: {error}",
                )
            except Exception as error:
                trace.append(attempted_trace)
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-external-adapter-exception",
                    owner="bucephalus",
                    error=f"{type(error).__name__}: {error}",
                )
            external_wall_elapsed = time.perf_counter() - external_call_started
            selected = external_analysis.best_series
            attempted_trace.update(
                {
                    "selected_series": selected.machine_notation,
                    "selected_notation": selected.notation,
                    "completed_micro_ply": external_analysis.completed_ply,
                    "score_text": external_analysis.score_text,
                    "external_elapsed_seconds": (
                        external_analysis.elapsed_seconds
                    ),
                    "external_call_wall_elapsed_seconds": external_wall_elapsed,
                    "executable_sha256": (
                        external_analysis.executable_sha256
                    ),
                    "upstream_commit": external_analysis.upstream_commit,
                    "adapter_version": external_analysis.adapter_version,
                    "request_script": external_analysis.request_script,
                    "stdout": external_analysis.stdout,
                    "stderr": external_analysis.stderr,
                    "deadline_reached": external_analysis.deadline_reached,
                    "deadline_completed_iteration_used": (
                        external_analysis.deadline_reached
                    ),
                    "process_exit_code": external_analysis.process_exit_code,
                    "process_exit_recovered": (
                        external_analysis.process_exit_recovered
                    ),
                    "selection_mode": external_analysis.selection_mode,
                    "terminal_stage_score": external_analysis.terminal_stage_score,
                    "global_deadline_seconds": external_analysis.global_deadline_seconds,
                    "global_deadline_reached": external_analysis.global_deadline_reached,
                    "external_process_count": external_analysis.process_count,
                    "selection_root_prefix_ply": external_analysis.selection_root_prefix_ply,
                    "terminal_stage_ply": external_analysis.terminal_stage_ply,
                    "selected_terminal_stage_index": (
                        external_analysis.selected_terminal_stage_index
                    ),
                    "continuation_stages": [
                        {
                            "stage_index": stage.stage_index,
                            "purpose": stage.purpose,
                            "starting_prefix": list(stage.starting_prefix),
                            "emitted_prefix": list(stage.emitted_prefix),
                            "requested_ply": stage.requested_ply,
                            "completed_ply": stage.completed_ply,
                            "wall_timeout_seconds": stage.wall_timeout_seconds,
                            "elapsed_seconds": stage.elapsed_seconds,
                            "request_script": stage.request_script,
                            "stdout": stage.stdout,
                            "stderr": stage.stderr,
                            "deadline_reached": stage.deadline_reached,
                            "process_exit_code": stage.process_exit_code,
                            "process_exit_recovered": stage.process_exit_recovered,
                            "usable": stage.usable,
                            "error": stage.error,
                            "stop_reason": stage.stop_reason,
                            "process_id": stage.process_id,
                            "same_process_continued": stage.same_process_continued,
                            "soft_checkpoint_seconds": stage.soft_checkpoint_seconds,
                            "hard_deadline_seconds": stage.hard_deadline_seconds,
                        }
                        for stage in external_analysis.continuation_stages
                    ],
                }
            )
            if (
                job.config.common_wall_timeout_seconds is not None
                and external_wall_elapsed
                > job.config.common_wall_timeout_seconds
                + COMMON_WALL_OVERRUN_GRACE_SECONDS
            ):
                trace.append(attempted_trace)
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-external-common-wall-overrun",
                    owner="bucephalus",
                )
            completed_ply_valid = (
                external_analysis.completed_ply == requested_ply
                if job.config.external_ply_policy == EXTERNAL_PLY_POLICY
                else 1 <= external_analysis.completed_ply <= requested_ply
            )
            if (
                external_analysis.requested_ply != requested_ply
                or not completed_ply_valid
                or (
                    external_analysis.completed_ply < requested_ply
                    and not external_analysis.deadline_reached
                    and not external_analysis.process_exit_recovered
                    and external_analysis.selection_mode == "single-stage"
                )
                or (
                    external_analysis.process_exit_recovered
                    and (
                        external_analysis.process_exit_code in (None, 0)
                        or (
                            external_analysis.deadline_reached
                            and external_analysis.selection_mode == "single-stage"
                        )
                    )
                )
                or (
                    external_analysis.process_exit_code not in (None, 0)
                    and not external_analysis.process_exit_recovered
                )
                or external_analysis.executable_sha256.lower()
                != job.external_spec.sha256
                or external_analysis.upstream_commit
                != job.external_spec.upstream_commit
                or external_analysis.adapter_version
                != job.config.expected_external_adapter_version
                or (
                    (job.config.common_wall_timeout_seconds or 0) >= 12.0
                    and not external_analysis.continuation_stages
                )
                or external_analysis.selection_mode not in {
                    "single-stage",
                    "anchor-fallback",
                    "anchor-terminal",
                    "deep-complete-live",
                    "deep-prefix-continuation",
                }
                or not _continuation_chain_selects_analysis(
                    state,
                    history,
                    external_analysis,
                    job.config.common_wall_timeout_seconds
                    or job.config.external_wall_timeout_seconds,
                )
            ):
                trace.append(attempted_trace)
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-external-provenance-mismatch",
                    owner="bucephalus",
                )

        try:
            authoritative = _revalidate_selected(state, selected)
        except ExternalEngineProtocolError as error:
            trace.append(attempted_trace)
            owner = "local" if played_by_local else "bucephalus"
            return _technical_record(
                job,
                state,
                start_pfen,
                trace,
                local_work_positions,
                external_calls,
                f"technical-{owner}-illegal-or-inconsistent-series",
                owner=owner,
                error=f"{type(error).__name__}: {error}",
            )

        next_history = history + (authoritative.moves,)
        if authoritative.outcome is None:
            try:
                replayed_after = replay_series_history(next_history)
            except ExternalEngineError as error:
                trace.append(attempted_trace)
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-appended-history-invalid",
                    owner="shared",
                    error=f"{type(error).__name__}: {error}",
                )
            if (
                replayed_after.position_hash
                != authoritative.final_state.position_hash
            ):
                trace.append(attempted_trace)
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-appended-history-state-mismatch",
                    owner="shared",
                )

        attempted_trace["played"] = True
        attempted_trace["authoritative_series"] = authoritative.machine_notation
        attempted_trace["authoritative_notation"] = authoritative.notation
        attempted_trace["after_pfen"] = authoritative.final_state.pfen
        attempted_trace["canonical_history_after"] = [
            list(series) for series in next_history
        ]
        attempted_trace["outcome"] = (
            authoritative.outcome.value if authoritative.outcome else None
        )
        trace.append(attempted_trace)
        history = next_history
        state = authoritative.final_state

        if authoritative.outcome is not None:
            return _terminal_record(
                job,
                state,
                start_pfen,
                trace,
                local_work_positions,
                external_calls,
                authoritative,
                mover,
            )
        if local_work_positions >= job.config.local_max_game_work_positions:
            return _technical_record(
                job,
                state,
                start_pfen,
                trace,
                local_work_positions,
                external_calls,
                "technical-local-game-work-budget-exhausted",
                owner="local",
            )

    return _technical_record(
        job,
        state,
        start_pfen,
        trace,
        local_work_positions,
        external_calls,
        "technical-emergency-series-watchdog-exhausted",
        owner="shared",
    )


def _worker_failure(job: ExternalGameJob, error: BaseException) -> ExternalGameRecord:
    state = job.opening.state()
    return _technical_record(
        job,
        state,
        state.pfen,
        (),
        0,
        0,
        "technical-worker-exception",
        owner="shared",
        error=f"{type(error).__name__}: {error}",
    )


def _execute_jobs(
    jobs: Sequence[ExternalGameJob],
    resources: ResourceBudget,
    progress: Callable[[str], None] | None,
    *,
    existing_records: Mapping[str, ExternalGameRecord] | None = None,
    record_callback: Callable[[ExternalGameRecord], None] | None = None,
) -> tuple[ExternalGameRecord, ...]:
    completed: dict[str, ExternalGameRecord] = dict(existing_records or {})
    pending = [job for job in jobs if job.game_id not in completed]

    def report(count: int) -> None:
        if progress is not None:
            progress(f"external match: finished {count}/{len(jobs)} games")

    if completed:
        report(len(completed))
    if resources.workers == 1:
        for count, job in enumerate(pending, len(completed) + 1):
            try:
                record = _play_external_game(job)
            except Exception as error:
                record = _worker_failure(job, error)
            completed[job.game_id] = record
            if record_callback is not None:
                record_callback(record)
            report(count)
    else:
        with ProcessPoolExecutor(max_workers=resources.workers) as executor:
            future_jobs = {
                executor.submit(_play_external_game, job): job for job in jobs
                if job.game_id not in completed
            }
            for count, future in enumerate(
                as_completed(future_jobs), len(completed) + 1
            ):
                job = future_jobs[future]
                try:
                    record = future.result()
                except Exception as error:
                    record = _worker_failure(job, error)
                completed[job.game_id] = record
                if record_callback is not None:
                    record_callback(record)
                report(count)
    return tuple(completed[job.game_id] for job in jobs)


def _local_points(record: ExternalGameRecord) -> float | None:
    if record.result == "1/2-1/2":
        return 0.5
    if record.result == "*":
        return None
    local_won = record.winner == "local"
    return 1.0 if local_won else 0.0


def _paired_sign_test_two_sided(wins: int, losses: int) -> float | None:
    decisive = wins + losses
    if decisive == 0:
        return None
    lower_tail = sum(
        math.comb(decisive, outcome)
        for outcome in range(min(wins, losses) + 1)
    ) / (2**decisive)
    return min(1.0, 2.0 * lower_tail)


def _summarize(
    records: Sequence[ExternalGameRecord],
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    wins = draws = losses = incomplete = 0
    local_move_only_fallbacks = 0
    local_internal_selective_limit_moves = 0
    local_deadline_completed_iteration_moves = 0
    external_process_exit_recoveries = 0
    external_processes = 0
    external_selection_modes: dict[str, int] = {}
    external_stage_stop_reasons: dict[str, int] = {}
    external_soft_cutoffs = 0
    external_same_pid_continuations = 0
    external_anchor_fallbacks = 0
    failure_reasons: dict[str, int] = {}
    failure_owners: dict[str, int] = {}
    for record in records:
        for entry in record.trace:
            if entry.get("engine") == "bucephalus" and entry.get("played"):
                external_process_exit_recoveries += int(
                    bool(entry.get("process_exit_recovered"))
                )
                external_processes += int(entry.get("external_process_count") or 0)
                mode = str(entry.get("selection_mode") or "single-stage")
                external_selection_modes[mode] = (
                    external_selection_modes.get(mode, 0) + 1
                )
                external_anchor_fallbacks += int(mode == "anchor-fallback")
                for stage in entry.get("continuation_stages") or ():
                    reason = str(stage.get("stop_reason") or "unknown")
                    external_stage_stop_reasons[reason] = (
                        external_stage_stop_reasons.get(reason, 0) + 1
                    )
                    external_soft_cutoffs += int(
                        reason == "soft-checkpoint-incomplete"
                    )
                    external_same_pid_continuations += int(
                        bool(stage.get("same_process_continued"))
                    )
            if entry.get("engine") != "local" or not entry.get("played"):
                continue
            local_move_only_fallbacks += int(
                bool(entry.get("move_only_liveness_fallback"))
            )
            local_internal_selective_limit_moves += int(
                bool(entry.get("internal_selective_limit_reached"))
            )
            local_deadline_completed_iteration_moves += int(
                bool(entry.get("deadline_completed_iteration_used"))
            )
        points = _local_points(record)
        if points is None:
            incomplete += 1
            failure_reasons[record.terminal_reason] = (
                failure_reasons.get(record.terminal_reason, 0) + 1
            )
            owner = record.technical_failure_owner or "unattributed"
            failure_owners[owner] = failure_owners.get(owner, 0) + 1
        elif points == 1.0:
            wins += 1
        elif points == 0.5:
            draws += 1
        else:
            losses += 1

    pairs: list[dict[str, Any]] = []
    pair_wins = pair_draws = pair_losses = incomplete_pairs = 0
    for offset in range(0, len(records), 2):
        games = records[offset : offset + 2]
        points = [_local_points(record) for record in games]
        if len(games) != 2 or any(value is None for value in points):
            pair_result = "incomplete"
            pair_points: float | None = None
            incomplete_pairs += 1
        else:
            pair_points = sum(value for value in points if value is not None)
            if pair_points > 1.0:
                pair_result = "win"
                pair_wins += 1
            elif pair_points == 1.0:
                pair_result = "draw"
                pair_draws += 1
            else:
                pair_result = "loss"
                pair_losses += 1
        pairs.append(
            {
                "pair_id": games[0].pair_id,
                "pair_index": games[0].pair_index,
                "opening_case_id": games[0].opening_case_id,
                "local_points": pair_points,
                "result": pair_result,
                "game_ids": [record.game_id for record in games],
                "technical_failures": [
                    {
                        "game_id": record.game_id,
                        "owner": record.technical_failure_owner,
                        "reason": record.terminal_reason,
                    }
                    for record in games
                    if record.result == "*"
                ],
            }
        )
    completed_games = wins + draws + losses
    completed_pairs = pair_wins + pair_draws + pair_losses
    paired_sign_test_p = _paired_sign_test_two_sided(pair_wins, pair_losses)
    return (
        {
            "scheduled_games": len(records),
            "completed_games": completed_games,
            "incomplete_games": incomplete,
            "local_game_wdl": {
                "wins": wins,
                "draws": draws,
                "losses": losses,
            },
            "local_game_score_rate": (
                (wins + draws * 0.5) / completed_games
                if completed_games
                else None
            ),
            "game_score_rate_denominator": "completed-games-only",
            "scheduled_pairs": len(records) // 2,
            "completed_pairs": completed_pairs,
            "incomplete_pairs": incomplete_pairs,
            "local_pair_wdl": {
                "wins": pair_wins,
                "draws": pair_draws,
                "losses": pair_losses,
            },
            "local_pair_score_rate": (
                (pair_wins + pair_draws * 0.5) / completed_pairs
                if completed_pairs
                else None
            ),
            "pair_score_rate_denominator": "completed-pairs-only",
            "paired_sign_test": {
                "unit": "color-swapped-opening-pair",
                "decisive_pairs": pair_wins + pair_losses,
                "two_sided_exact_binomial_p": paired_sign_test_p,
            },
            "technical_failures": {
                "by_reason": dict(sorted(failure_reasons.items())),
                "by_owner": dict(sorted(failure_owners.items())),
            },
            "local_search_fallbacks": {
                "move_only_liveness": local_move_only_fallbacks,
                "internal_selective_limit_moves": (
                    local_internal_selective_limit_moves
                ),
                "deadline_completed_iteration_moves": (
                    local_deadline_completed_iteration_moves
                ),
            },
            "external_process_exit_recoveries": (
                external_process_exit_recoveries
            ),
            "external_completion_controller": {
                "processes": external_processes,
                "selection_modes": dict(sorted(external_selection_modes.items())),
                "stage_stop_reasons": dict(
                    sorted(external_stage_stop_reasons.items())
                ),
                "soft_checkpoint_cutoffs": external_soft_cutoffs,
                "same_pid_hard_continuations": external_same_pid_continuations,
                "anchor_fallbacks": external_anchor_fallbacks,
            },
        },
        tuple(pairs),
    )


def _protocol_eligibility(
    config: ExternalMatchConfig,
    *,
    local_profile: EngineProfile,
) -> tuple[bool, bool]:
    fair_equal_wall_protocol = (
        config.opening_suite_version == BUCEPHALUS_FAIR_OPENING_SUITE_VERSION
        and config.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY
        and config.common_wall_timeout_seconds is not None
        and config.pairs == 50
        and len(config.opening_case_ids) == 50
    )
    best_settings_protocol = (
        config.match_intent == BEST_SETTINGS_MATCH_INTENT
        and config.opening_policy == OPENING_POLICY_INITIAL
        and config.opening_suite_version == INITIAL_POSITION_SUITE_VERSION
        and config.opening_qualification is None
        and config.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY
        and config.common_wall_timeout_seconds == 120.0
        and config.external_wall_timeout_seconds == 120.0
        and config.local_depth_series == 8
        and config.local_max_series_per_node == 32
        and config.local_native_threads == 16
        and config.local_max_generation_positions == 4_000_000_000
        and config.local_max_game_work_positions == 100_000_000_000
        and config.emergency_max_series == 18
        and config.requested_match_workers == 1
        and config.pairs == 50
        and config.opening_case_ids == (INITIAL_POSITION_CASE.case_id,)
        and _approved_best_settings_profile(local_profile)
    )
    return fair_equal_wall_protocol, best_settings_protocol


def _superiority_gate(
    config: ExternalMatchConfig,
    summary: Mapping[str, Any],
    *,
    local_profile: EngineProfile,
    approved_bucephalus_identity: bool,
    identity_stable: bool,
) -> tuple[bool, bool]:
    fair_equal_wall_protocol, best_settings_protocol = _protocol_eligibility(
        config,
        local_profile=local_profile,
    )
    strict_protocol_complete = (
        (fair_equal_wall_protocol or best_settings_protocol)
        and summary["scheduled_games"] == 100
        and summary["completed_games"] == 100
        and summary["scheduled_pairs"] == 50
        and summary["completed_pairs"] == 50
    )
    pair_wdl = summary["local_pair_wdl"]
    paired_p = summary["paired_sign_test"]["two_sided_exact_binomial_p"]
    superiority_supported = (
        strict_protocol_complete
        and config.opening_policy != OPENING_POLICY_INITIAL
        and approved_bucephalus_identity
        and identity_stable
        and pair_wdl["wins"] > pair_wdl["losses"]
        and paired_p is not None
        and paired_p < 0.05
    )
    return strict_protocol_complete, superiority_supported


def _apply_opening_policy_summary(
    config: ExternalMatchConfig, summary: dict[str, Any]
) -> None:
    if config.opening_policy == OPENING_POLICY_INITIAL:
        summary["paired_sign_test"] = {
            "unit": "repeated-initial-position-color-swapped-pair",
            "decisive_pairs": None,
            "two_sided_exact_binomial_p": None,
            "applicable": False,
            "reason": "repeated-identical-initial-state-is-not-independent",
        }


def _claim_scope_statement(config: ExternalMatchConfig) -> str:
    if config.opening_policy == OPENING_POLICY_INITIAL:
        return (
            "All games begin from the standard initial Progressive Series 1 "
            "with empty history; the engines own every played series. The "
            f"{config.pairs * 2}-game schedule repeats one deterministic initial "
            "state, so its games are not independent opening samples."
        )
    if config.common_wall_timeout_seconds is not None:
        return (
            "Results apply only to the selected canonical Scottish Progressive "
            "openings and the exact equal end-to-end call-wall, engine-native "
            "return policy in this report. A disclosed legal local move-only "
            "fallback can be played when no series-depth iteration completes. "
            "Search depth, work units, startup, and replay overhead remain "
            "engine-specific."
        )
    return (
        "Results apply only to the selected canonical Scottish Progressive "
        "openings and the exact asymmetric policies in this report."
    )


def _rule_protocol_gaps(config: ExternalMatchConfig) -> list[dict[str, str]]:
    timed = config.common_wall_timeout_seconds is not None
    return [
        {
            "gap": "no-position-command",
            "impact": (
                "No FEN/setboard protocol; every call starts a new process and "
                "replays a canonical history from the orthodox initial board."
            ),
        },
        {
            "gap": "no-native-clock-or-node-limit",
            "impact": (
                (
                    "Both engines receive the same per-move wall ceiling. The "
                    "local engine returns its deepest completed series-depth "
                    "iteration, or its explicit legal move-only liveness fallback "
                    "when an internal conservative safety proof remains unknown. "
                    "Bucephalus uses the frozen output-only v4 completion "
                    "controller: a replay-validated PLY1 anchor, live deep search, "
                    "and replay-validated suffix continuation. Every selected "
                    "move was emitted by the pinned engine; process stops, "
                    "restarts, stitching, and anchor fallback are disclosed."
                )
                if timed
                else (
                    "Bucephalus receives a declared micro-ply depth and an "
                    "external wall watchdog. A timeout is an incomplete game, "
                    "not a result."
                )
            ),
        },
        {
            "gap": "asymmetric-search-units",
            "impact": (
                "Local depth counts complete progressive series; Bucephalus "
                "depth counts individual micro-moves. Results are fixed-policy "
                "performance evidence, not equal-node or equal-depth evidence."
            ),
        },
        {
            "gap": "draw-evaluation",
            "impact": (
                "Bucephalus has no ten-quiet-series draw and scores an internally "
                "detected stalemate by material. The harness adjudicates only the "
                "authoritative result after replay, but its search choices retain "
                "that rule-evaluation mismatch."
            ),
        },
        {
            "gap": "fixed-arrays",
            "impact": (
                f"Upstream uses an unchecked {BUCEPHALUS_MAX_LEGAL_MOVES}-move "
                f"array, a {BUCEPHALUS_MAX_GAME_RECORD}-entry replay record, and "
                f"a {BUCEPHALUS_MAX_PLY}-micro-ply ceiling. Root/replay limits are "
                "guarded; deeper search-array overflow remains an upstream risk."
            ),
        },
        {
            "gap": "ram-not-hard-capped",
            "impact": (
                f"Worker planning reserves about "
                f"{BUCEPHALUS_PROCESS_MEMORY_ESTIMATE_MB} MiB for each external "
                "process, but the operating system does not enforce a per-process "
                "RAM ceiling."
            ),
        },
    ]


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _journal_protocol(
    local_profile: EngineProfile,
    external_spec: BucephalusSpec,
    config: ExternalMatchConfig,
    jobs: Sequence[ExternalGameJob],
    *,
    executable: Path,
    executable_hash: str,
    resources: ResourceBudget,
    identity_snapshot: Mapping[str, Any] | None = None,
    external_build_receipt: Mapping[str, Any] | None = None,
    opening_suite: SeededOpeningSuite | None = None,
) -> dict[str, Any]:
    identity = dict(identity_snapshot or _run_identity_snapshot())
    return {
        "format": EXTERNAL_MATCH_JOURNAL_FORMAT,
        "match_id": jobs[0].game_id[:20],
        "local_engine": {
            "engine_version": identity["engine_version"],
            "source_fingerprint": identity["source_fingerprint"],
            "profile": local_profile.as_dict(),
            "profile_canonical_sha256": _profile_canonical_sha256(local_profile),
            "approved_best_settings_profile": (
                _approved_best_settings_profile(local_profile)
            ),
            "git": identity["git"],
            "runtime": identity["runtime"],
            "backend": identity["backend"],
        },
        "external_engine": {
            "name": "Bucephalus",
            "resolved_executable": str(executable),
            "executable_sha256": executable_hash,
            "upstream_commit": external_spec.upstream_commit,
            "build_provenance": (
                external_build_receipt.get("canonical_sha256")
                if external_build_receipt is not None
                else external_spec.build_provenance
            ),
            "build_receipt": external_build_receipt,
            "adapter_version": config.expected_external_adapter_version,
        },
        "config": config.as_dict(),
        "resource_execution_controls": _resource_execution_controls(resources),
        "benchmark_harness": identity["benchmark_harness"],
        "opening_suite_canonical_sha256": config.opening_suite_sha256,
        "opening_suite_payload": (
            opening_suite.as_dict() if opening_suite is not None else None
        ),
        "schedule": [
            {
                "game_id": job.game_id,
                "pair_id": job.pair_id,
                "pair_index": job.pair_index,
                "swap_index": job.swap_index,
                "opening_case_id": job.opening.case_id,
                "local_color": _color_name(job.local_color),
                "canonical_history": [list(series) for series in job.history],
            }
            for job in jobs
        ],
    }


def _record_from_journal(
    payload: Mapping[str, Any], job: ExternalGameJob
) -> ExternalGameRecord:
    values = dict(payload)
    values["trace"] = tuple(values.get("trace", ()))
    record = ExternalGameRecord(**values)
    expected = {
        "game_id": job.game_id,
        "pair_id": job.pair_id,
        "pair_index": job.pair_index,
        "swap_index": job.swap_index,
        "opening_case_id": job.opening.case_id,
        "local_color": _color_name(job.local_color),
        "external_color": _color_name(not job.local_color),
    }
    for field, expected_value in expected.items():
        if getattr(record, field) != expected_value:
            raise ValueError(
                f"journal record {job.game_id} has mismatched {field}"
            )
    if record.result not in {"1-0", "0-1", "1/2-1/2", "*"}:
        raise ValueError(f"journal record {job.game_id} has invalid result")
    _validate_journal_record_replay(record, job)
    return record


def _validate_journal_record_replay(
    record: ExternalGameRecord,
    job: ExternalGameJob,
) -> None:
    try:
        replayed = replay_series_history(job.history)
    except ExternalEngineError as error:
        raise ValueError("journal opening history is not replayable") from error
    state = job.opening.state()
    if replayed.position_hash != state.position_hash:
        raise ValueError("journal opening history does not reach its opening")
    if record.start_pfen != state.pfen:
        raise ValueError("journal record start PFEN does not match its opening")

    history = tuple(tuple(series) for series in job.history)
    played_count = 0
    external_calls = 0
    last_local_work = 0
    played_engines: set[str] = set()
    terminal: tuple[SeriesResult, chess.Color] | None = None
    for index, item in enumerate(record.trace):
        if not isinstance(item, dict):
            raise ValueError("journal trace entry must be an object")
        mover = state.board.turn
        expected_engine = "local" if mover == job.local_color else "bucephalus"
        if item.get("before_pfen") != state.pfen:
            raise ValueError("journal trace before PFEN is not authoritative")
        if item.get("series_number") != state.series_number:
            raise ValueError("journal trace series number is not authoritative")
        if item.get("side") != _color_name(mover):
            raise ValueError("journal trace side is not authoritative")
        if item.get("engine") != expected_engine:
            raise ValueError("journal trace engine assignment is not authoritative")
        if expected_engine == "bucephalus":
            external_calls += 1
            expected_requested_ply = job.config.external_search_ply(
                state.series_number
            )
            if (
                item.get("requested_micro_ply") != expected_requested_ply
                or item.get("ply_policy") != job.config.external_ply_policy
                or item.get("fixed_lookahead_micro_plies")
                != job.config.external_lookahead_micro_plies
                or item.get("wall_watchdog_seconds")
                != job.config.external_wall_timeout_seconds
            ):
                raise ValueError(
                    "journal Bucephalus request controls are not authoritative"
                )
        else:
            if (
                item.get("profile_id") != job.local_profile.profile_id
                or item.get("requested_depth_series")
                != job.config.local_depth_series
                or item.get("branch_cap")
                != job.config.local_max_series_per_node
                or item.get("native_threads")
                != job.config.local_native_threads
                or item.get("wall_budget_seconds")
                != job.config.common_wall_timeout_seconds
            ):
                raise ValueError(
                    "journal local engine profile or search controls are not "
                    "authoritative"
                )
            expected_search_work_limit = min(
                job.config.local_max_generation_positions,
                job.config.local_max_game_work_positions - last_local_work,
            )
            search_work_limit = item.get("search_work_limit")
            search_work = item.get("search_work_positions")
            work = item.get("game_local_work_positions")
            if (
                search_work_limit != expected_search_work_limit
                or type(search_work) is not int
                or not 0 <= search_work <= search_work_limit
                or type(work) is not int
                or work != last_local_work + search_work
            ):
                raise ValueError("journal local work accounting is invalid")
            last_local_work = work

        if not item.get("played"):
            if index != len(record.trace) - 1 or record.result != "*":
                raise ValueError("journal has a non-final unplayed trace entry")
            continue
        machine = item.get("authoritative_series")
        if not isinstance(machine, str) or not machine:
            raise ValueError("journal played trace has no authoritative series")
        if item.get("selected_series") != machine:
            raise ValueError(
                "journal selected series does not match authoritative replay"
            )
        if expected_engine == "bucephalus":
            completed_ply = item.get("completed_micro_ply")
            deadline_reached = item.get("deadline_reached")
            process_exit_recovered = item.get("process_exit_recovered")
            process_exit_code = item.get("process_exit_code")
            if (
                type(completed_ply) is not int
                or not 1 <= completed_ply <= expected_requested_ply
                or type(deadline_reached) is not bool
                or type(process_exit_recovered) is not bool
                or (
                    process_exit_code is not None
                    and type(process_exit_code) is not int
                )
            ):
                raise ValueError(
                    "journal Bucephalus completion metadata is invalid"
                )
            if (
                job.config.external_ply_policy == EXTERNAL_PLY_POLICY
                and completed_ply != expected_requested_ply
            ):
                raise ValueError(
                    "journal Bucephalus fixed-ply completion is inconsistent"
                )
            if (
                completed_ply < expected_requested_ply
                and not deadline_reached
                and not process_exit_recovered
                and item.get("selection_mode", "single-stage") == "single-stage"
            ):
                raise ValueError(
                    "journal Bucephalus partial iteration lacks a stop reason"
                )
            if process_exit_recovered and (
                process_exit_code in (None, 0)
                or (
                    deadline_reached
                    and item.get("selection_mode", "single-stage")
                    == "single-stage"
                )
            ):
                raise ValueError(
                    "journal Bucephalus process-exit recovery is inconsistent"
                )
            if process_exit_code not in (None, 0) and not process_exit_recovered:
                raise ValueError(
                    "journal Bucephalus process exit was not accounted for"
                )
            if (
                item.get("executable_sha256", "").lower()
                != job.external_spec.sha256
                or item.get("upstream_commit")
                != job.external_spec.upstream_commit
                or item.get("adapter_version")
                != job.config.expected_external_adapter_version
            ):
                raise ValueError(
                    "journal Bucephalus engine provenance is not authoritative"
                )
            if (
                not isinstance(item.get("score_text"), str)
                or not isinstance(item.get("request_script"), str)
                or not isinstance(item.get("stdout"), str)
                or not isinstance(item.get("stderr"), str)
            ):
                raise ValueError(
                    "journal Bucephalus evidence transcript is incomplete"
                )
            continuation_stages = item.get("continuation_stages")
            if not isinstance(continuation_stages, list):
                raise ValueError(
                    "journal Bucephalus continuation provenance is missing"
                )
            if (
                (job.config.common_wall_timeout_seconds or 0) >= 12.0
                and not continuation_stages
            ):
                raise ValueError(
                    "journal long Bucephalus series has no continuation stages"
                )
            selection_mode = item.get("selection_mode", "single-stage")
            if selection_mode not in {
                "single-stage", "anchor-fallback", "anchor-terminal",
                "deep-complete-live",
                "deep-prefix-continuation",
            }:
                raise ValueError("journal Bucephalus selection mode is invalid")
            anchor_prefix: list[str] = []
            stitched_prefix: list[str] = []
            parsed_stages: list[ExternalAnalysisStage] = []
            stage_elapsed = 0.0
            for stage_index, stage in enumerate(continuation_stages, 1):
                if not isinstance(stage, dict):
                    raise ValueError("journal Bucephalus stage is not an object")
                starting_prefix = stage.get("starting_prefix")
                emitted_prefix = stage.get("emitted_prefix")
                stage_wall = stage.get("wall_timeout_seconds")
                elapsed = stage.get("elapsed_seconds")
                purpose = stage.get("purpose")
                usable = stage.get("usable")
                stop_reason = stage.get("stop_reason")
                process_id = stage.get("process_id")
                same_process_continued = stage.get("same_process_continued")
                soft_checkpoint = stage.get("soft_checkpoint_seconds")
                hard_deadline = stage.get("hard_deadline_seconds")
                expected_prefix = (
                    anchor_prefix if purpose == "anchor-ply1"
                    else [] if purpose in {"deep-max-ply", "deep-refinement"}
                    else stitched_prefix if purpose in {"suffix-max-ply", "suffix-ply1"}
                    else None
                )
                if (
                    stage.get("stage_index") != stage_index
                    or expected_prefix is None
                    or starting_prefix != expected_prefix
                    or not isinstance(emitted_prefix, list)
                    or any(not isinstance(move, str) for move in emitted_prefix)
                    or type(stage.get("requested_ply")) is not int
                    or type(stage.get("completed_ply")) is not int
                    or stage.get("requested_ply") not in {
                        1, expected_requested_ply - 1, expected_requested_ply
                    }
                    or (
                        purpose in {"anchor-ply1", "suffix-ply1"}
                        and stage.get("requested_ply") != 1
                    )
                    or (
                        purpose in {"deep-max-ply", "deep-refinement", "suffix-max-ply"}
                        and stage.get("requested_ply") != expected_requested_ply
                    )
                    or not 0 <= stage.get("completed_ply") <= stage.get("requested_ply")
                    or type(usable) is not bool
                    or stop_reason not in {
                        "process-exit", "stage-deadline",
                        "soft-checkpoint-incomplete", "hard-deadline",
                    }
                    or (
                        process_id is not None
                        and (type(process_id) is not int or process_id <= 0)
                    )
                    or type(same_process_continued) is not bool
                    or (
                        soft_checkpoint is not None
                        and not isinstance(soft_checkpoint, (int, float))
                    )
                    or (
                        hard_deadline is not None
                        and not isinstance(hard_deadline, (int, float))
                    )
                    or (
                        stage.get("error") is not None
                        and not isinstance(stage.get("error"), str)
                    )
                    or (usable and stage.get("completed_ply") < 1)
                    or (not usable and stage.get("completed_ply") != 0)
                    or not isinstance(stage_wall, (int, float))
                    or not isinstance(elapsed, (int, float))
                    or stage_wall <= 0
                    or elapsed < 0
                    or not isinstance(stage.get("request_script"), str)
                    or not isinstance(stage.get("stdout"), str)
                    or not isinstance(stage.get("stderr"), str)
                    or type(stage.get("deadline_reached")) is not bool
                    or type(stage.get("process_exit_recovered")) is not bool
                    or (
                        stage.get("process_exit_code") is not None
                        and type(stage.get("process_exit_code")) is not int
                    )
                ):
                    raise ValueError(
                        "journal Bucephalus continuation stage is invalid"
                    )
                stage_elapsed += float(elapsed)
                parsed_stages.append(
                    ExternalAnalysisStage(
                        stage_index=stage_index,
                        purpose=purpose,
                        starting_prefix=tuple(starting_prefix),
                        emitted_prefix=tuple(emitted_prefix),
                        requested_ply=stage["requested_ply"],
                        completed_ply=stage["completed_ply"],
                        wall_timeout_seconds=float(stage_wall),
                        elapsed_seconds=float(elapsed),
                        request_script=stage["request_script"],
                        stdout=stage["stdout"],
                        stderr=stage["stderr"],
                        deadline_reached=stage["deadline_reached"],
                        process_exit_code=stage.get("process_exit_code"),
                        process_exit_recovered=stage["process_exit_recovered"],
                        usable=usable,
                        error=stage.get("error"),
                        stop_reason=stop_reason,
                        process_id=process_id,
                        same_process_continued=same_process_continued,
                        soft_checkpoint_seconds=soft_checkpoint,
                        hard_deadline_seconds=hard_deadline,
                    )
                )
                if usable and purpose == "anchor-ply1":
                    anchor_prefix = starting_prefix + emitted_prefix
                elif usable and purpose == "deep-max-ply":
                    stitched_prefix = starting_prefix + emitted_prefix
                elif (
                    usable
                    and purpose == "deep-refinement"
                    and selection_mode == "deep-refined"
                ):
                    stitched_prefix = starting_prefix + emitted_prefix
                elif usable and purpose in {"suffix-max-ply", "suffix-ply1"}:
                    stitched_prefix = starting_prefix + emitted_prefix
            selected_moves = machine.split("/")
            selected_evidence = (
                anchor_prefix
                if selection_mode in {"anchor-fallback", "anchor-terminal"}
                else stitched_prefix
            )
            if continuation_stages and selected_evidence != selected_moves:
                raise ValueError(
                    "journal Bucephalus stage chain does not produce the selected series"
                )
            if (
                job.config.common_wall_timeout_seconds is not None
                and stage_elapsed
                > job.config.common_wall_timeout_seconds
                + COMMON_WALL_OVERRUN_GRACE_SECONDS
            ):
                raise ValueError(
                    "journal Bucephalus continuation stages exceed the common wall"
                )
            if (job.config.common_wall_timeout_seconds or 0) >= 12.0:
                external_elapsed = item.get("external_elapsed_seconds")
                if (
                    item.get("global_deadline_seconds")
                    != job.config.common_wall_timeout_seconds
                    or type(item.get("global_deadline_reached")) is not bool
                    or type(item.get("external_process_count")) is not int
                    or item.get("external_process_count") != len(parsed_stages)
                    or item.get("terminal_stage_score") != item.get("score_text")
                    or not isinstance(external_elapsed, (int, float))
                    or external_elapsed < 0
                    or external_elapsed
                    > job.config.common_wall_timeout_seconds
                    + COMMON_WALL_OVERRUN_GRACE_SECONDS
                ):
                    raise ValueError(
                        "journal Bucephalus global deadline accounting is invalid"
                    )
                try:
                    selected_result = play_series(state, tuple(selected_moves))
                except SeriesLegalityError as error:
                    raise ValueError(
                        "journal Bucephalus selected series is illegal"
                    ) from error
                reconstructed = ExternalAnalysis(
                    best_series=selected_result,
                    requested_ply=expected_requested_ply,
                    completed_ply=completed_ply,
                    score_text=item["score_text"],
                    elapsed_seconds=float(external_elapsed),
                    executable_sha256=item["executable_sha256"],
                    upstream_commit=item.get("upstream_commit"),
                    adapter_version=item["adapter_version"],
                    request_script=item["request_script"],
                    stdout=item["stdout"],
                    stderr=item["stderr"],
                    deadline_reached=deadline_reached,
                    process_exit_code=process_exit_code,
                    process_exit_recovered=process_exit_recovered,
                    continuation_stages=tuple(parsed_stages),
                    selection_mode=selection_mode,
                    terminal_stage_score=item.get("terminal_stage_score"),
                    global_deadline_seconds=item.get("global_deadline_seconds"),
                    global_deadline_reached=item["global_deadline_reached"],
                    process_count=item["external_process_count"],
                    selection_root_prefix_ply=item.get("selection_root_prefix_ply"),
                    terminal_stage_ply=item.get("terminal_stage_ply"),
                    selected_terminal_stage_index=item.get(
                        "selected_terminal_stage_index"
                    ),
                )
                if not _continuation_chain_selects_analysis(
                    state,
                    history,
                    reconstructed,
                    job.config.common_wall_timeout_seconds,
                ):
                    raise ValueError(
                        "journal Bucephalus continuation transcript is not authoritative"
                    )
        try:
            authoritative = play_series(state, tuple(machine.split("/")))
        except SeriesLegalityError as error:
            raise ValueError("journal trace series is illegal") from error
        if item.get("after_pfen") != authoritative.final_state.pfen:
            raise ValueError("journal trace after PFEN is not authoritative")
        history = history + (authoritative.moves,)
        if item.get("canonical_history_after") != [
            list(series) for series in history
        ]:
            raise ValueError("journal trace canonical history is inconsistent")
        expected_outcome = (
            authoritative.outcome.value if authoritative.outcome is not None else None
        )
        if item.get("outcome") != expected_outcome:
            raise ValueError("journal trace outcome is not authoritative")
        played_count += 1
        played_engines.add(expected_engine)
        state = authoritative.final_state
        if authoritative.is_terminal:
            if index != len(record.trace) - 1:
                raise ValueError("journal trace continues after a terminal series")
            terminal = (authoritative, mover)

    if record.final_pfen != state.pfen:
        raise ValueError("journal final PFEN is not authoritative")
    if record.series_played != played_count:
        raise ValueError("journal series count is inconsistent")
    if record.external_calls != external_calls:
        raise ValueError("journal external call count is inconsistent")
    if record.local_work_positions != last_local_work:
        raise ValueError("journal local work total is inconsistent")

    if record.result == "*":
        if terminal is not None:
            raise ValueError("journal marks a terminal game incomplete")
        if not record.terminal_reason.startswith("technical-"):
            raise ValueError("journal incomplete record lacks a technical reason")
        if record.winner is not None or record.winner_color is not None:
            raise ValueError("journal incomplete record cannot name a winner")
        if record.technical_failure_owner not in {
            "local",
            "bucephalus",
            "shared",
        }:
            raise ValueError(
                "journal incomplete record has an invalid technical owner"
            )
        return
    if terminal is None:
        raise ValueError("journal completed record has no terminal replay")
    terminal_result, terminal_mover = terminal
    winner_color = _terminal_winner(terminal_result, terminal_mover)
    expected_result = (
        "1/2-1/2" if winner_color is None else _result_string(winner_color)
    )
    expected_winner = (
        None
        if winner_color is None
        else "local" if winner_color == job.local_color else "bucephalus"
    )
    expected_winner_color = (
        None if winner_color is None else _color_name(winner_color)
    )
    if (
        record.result != expected_result
        or record.winner != expected_winner
        or record.winner_color != expected_winner_color
        or record.terminal_reason != terminal_result.outcome.value
        or record.technical_failure_owner is not None
    ):
        raise ValueError("journal terminal result metadata is not authoritative")
    if (
        (
            job.config.opening_qualification is not None
            or job.config.opening_policy == OPENING_POLICY_INITIAL
        )
        and played_engines != {"local", "bucephalus"}
    ):
        raise ValueError("engaged completed game did not include both engines")


def _prepare_journal(
    journal_directory: str | Path,
    protocol: Mapping[str, Any],
    jobs: Sequence[ExternalGameJob],
    *,
    resume: bool,
) -> tuple[Path, str, dict[str, ExternalGameRecord]]:
    root = Path(journal_directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    games_directory = root / "games"
    games_directory.mkdir(parents=True, exist_ok=True)
    protocol_path = root / "protocol.json"
    protocol_sha256 = _canonical_sha256(protocol)
    if protocol_path.exists():
        existing_protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        if _canonical_sha256(existing_protocol) != protocol_sha256:
            raise ValueError(
                "journal protocol does not match the requested match identity"
            )
        if not resume:
            raise ValueError(
                "journal already exists; pass resume=True to preserve and continue it"
            )
    else:
        if any(games_directory.glob("*.json")):
            raise ValueError("journal games exist without a matching protocol")
        write_external_match_report(protocol, protocol_path)

    jobs_by_id = {job.game_id: job for job in jobs}
    existing_records: dict[str, ExternalGameRecord] = {}
    for path in sorted(games_directory.glob("*.json")):
        if path.stem not in jobs_by_id:
            raise ValueError(f"journal contains unexpected game {path.stem}")
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        if wrapper.get("protocol_sha256") != protocol_sha256:
            raise ValueError(f"journal game {path.stem} has the wrong protocol")
        record_payload = wrapper.get("record")
        if not isinstance(record_payload, dict):
            raise ValueError(f"journal game {path.stem} has no record payload")
        existing_records[path.stem] = _record_from_journal(
            record_payload, jobs_by_id[path.stem]
        )
    return root, protocol_sha256, existing_records


def run_external_match(
    local_profile: EngineProfile,
    external_spec: BucephalusSpec,
    *,
    config: ExternalMatchConfig | None = None,
    requested_workers: int | None = None,
    memory_per_worker_mb: int = DEFAULT_EXTERNAL_MATCH_MEMORY_PER_WORKER_MB,
    reserve_memory_mb: int = 512,
    progress: Callable[[str], None] | None = None,
    journal_directory: str | Path | None = None,
    resume: bool = False,
    external_build_receipt_path: str | Path | None = None,
    opening_suite: SeededOpeningSuite | None = None,
) -> dict[str, Any]:
    """Runs a fixed, color-swapped local-profile versus Bucephalus match.

    The harness never mutates league/champion state. Only authoritative legal
    checkmate, stalemate, or proven dead-material draw can complete a game;
    every adapter, replay, resource, or emergency limit is serialized as `*`.
    """

    config = config or ExternalMatchConfig()
    approved_local_profile = _approved_best_settings_profile(local_profile)
    if (
        config.match_intent == BEST_SETTINGS_MATCH_INTENT
        and not approved_local_profile
    ):
        raise ValueError(
            "best-settings matches require the exact approved local profile "
            f"{APPROVED_BEST_SETTINGS_LOCAL_PROFILE_ID} with canonical SHA-256 "
            f"{APPROVED_BEST_SETTINGS_LOCAL_PROFILE_SHA256}"
        )
    if resume and journal_directory is None:
        raise ValueError("resume requires a journal_directory")
    if config.common_wall_timeout_seconds is not None and (
        external_build_receipt_path is None
    ):
        raise ValueError(
            "timed rematches require a machine-checked external build receipt"
        )
    if memory_per_worker_mb < DEFAULT_EXTERNAL_MATCH_MEMORY_PER_WORKER_MB:
        raise ValueError(
            f"memory_per_worker_mb must be at least "
            f"{DEFAULT_EXTERNAL_MATCH_MEMORY_PER_WORKER_MB} for local search plus "
            "the external process"
        )
    executable, executable_hash = external_spec.verify()
    external_build_receipt = (
        _load_external_build_receipt(
            external_build_receipt_path,
            external_spec=external_spec,
            executable=executable,
            executable_hash=executable_hash,
        )
        if external_build_receipt_path is not None
        else None
    )
    approved_bucephalus_identity = bool(
        external_build_receipt
        and external_build_receipt["approved_for_named_bucephalus_claim"]
        and config.expected_external_adapter_version
        == BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION
    )
    identity_snapshot = _run_identity_snapshot()
    opening_context = _resolve_opening_context(config, opening_suite)
    jobs = _build_jobs(
        local_profile,
        external_spec,
        config,
        opening_suite=opening_suite,
        opening_context=opening_context,
    )
    for job in jobs[::2]:
        replayed = replay_series_history(job.history)
        if replayed.position_hash != job.opening.state().position_hash:
            raise ExternalEngineConfigurationError(
                f"canonical history mismatch for opening {job.opening.case_id}"
            )

    detected = detect_resource_budget(
        requested_workers,
        memory_per_worker_mb=memory_per_worker_mb,
        reserve_memory_mb=reserve_memory_mb,
    )
    resources = replace(detected, workers=min(detected.workers, len(jobs)))
    if config.match_intent == BEST_SETTINGS_MATCH_INTENT:
        if (
            requested_workers is not None
            and requested_workers != config.requested_match_workers
        ):
            raise ValueError(
                "requested workers do not match the frozen best-settings config"
            )
        if resources.workers != 1:
            raise ValueError(
                "best-settings head-to-head requires exactly one match worker"
            )
        if resources.detected_logical_cpus < config.local_native_threads:
            raise ValueError(
                "best-settings local native threads exceed detected logical CPUs"
            )
    journal_root: Path | None = None
    protocol_sha256: str | None = None
    existing_records: dict[str, ExternalGameRecord] = {}
    record_callback: Callable[[ExternalGameRecord], None] | None = None
    if journal_directory is not None:
        protocol = _journal_protocol(
            local_profile,
            external_spec,
            config,
            jobs,
            executable=executable,
            executable_hash=executable_hash,
            resources=resources,
            identity_snapshot=identity_snapshot,
            external_build_receipt=external_build_receipt,
            opening_suite=opening_suite,
        )
        journal_root, protocol_sha256, existing_records = _prepare_journal(
            journal_directory,
            protocol,
            jobs,
            resume=resume,
        )

        def persist_record(record: ExternalGameRecord) -> None:
            assert journal_root is not None
            assert protocol_sha256 is not None
            write_external_match_report(
                {
                    "format": EXTERNAL_MATCH_JOURNAL_FORMAT,
                    "protocol_sha256": protocol_sha256,
                    "record": record.as_dict(),
                },
                journal_root / "games" / f"{record.game_id}.json",
            )

        record_callback = persist_record
    started = time.perf_counter()
    records = _execute_jobs(
        jobs,
        resources,
        progress,
        existing_records=existing_records,
        record_callback=record_callback,
    )
    elapsed_seconds = time.perf_counter() - started
    summary, pairs = _summarize(records)
    _apply_opening_policy_summary(config, summary)
    identity_drift = _identity_drift(identity_snapshot)
    strict_protocol_complete, superiority_supported = _superiority_gate(
        config,
        summary,
        local_profile=local_profile,
        approved_bucephalus_identity=approved_bucephalus_identity,
        identity_stable=not identity_drift["detected"],
    )
    fair_equal_wall_protocol, best_settings_protocol = _protocol_eligibility(
        config,
        local_profile=local_profile,
    )
    summary["strict_100_game_protocol_complete"] = strict_protocol_complete
    summary["fair_equal_wall_protocol"] = fair_equal_wall_protocol
    summary["best_settings_protocol"] = best_settings_protocol
    selected_openings = (
        [INITIAL_POSITION_CASE]
        if config.opening_policy == OPENING_POLICY_INITIAL
        else [job.opening for job in jobs[::2]]
    )
    match_id = jobs[0].game_id[:20]
    report = {
        "format": EXTERNAL_MATCH_FORMAT,
        "report_id": "external-report-" + match_id,
        "created_at": _now(),
        "benchmark_harness": identity_snapshot["benchmark_harness"],
        "local_engine": {
            "engine_version": identity_snapshot["engine_version"],
            "source_fingerprint": identity_snapshot["source_fingerprint"],
            "git": identity_snapshot["git"],
            "runtime": identity_snapshot["runtime"],
            "backend": identity_snapshot["backend"],
            "profile": local_profile.as_dict(),
            "profile_canonical_sha256": _profile_canonical_sha256(local_profile),
            "approved_best_settings_profile": approved_local_profile,
        },
        "external_engine": {
            "name": "Bucephalus",
            "resolved_executable": str(executable),
            "executable_sha256": executable_hash,
            "upstream_commit": external_spec.upstream_commit,
            "build_provenance": (
                external_build_receipt.get("canonical_sha256")
                if external_build_receipt is not None
                else external_spec.build_provenance
            ),
            "build_receipt": external_build_receipt,
            "approved_named_baseline": approved_bucephalus_identity,
            "adapter_version": config.expected_external_adapter_version,
            "license": "GPL-3.0-or-later",
            "bundled_by_project": False,
            "binary_source_policy": "user-supplied-pinned-executable",
        },
        "config": config.as_dict(),
        "resources": {
            **resources.as_dict(),
            "external_process_memory_estimate_bytes": (
                BUCEPHALUS_PROCESS_MEMORY_ESTIMATE_MB * MIB
            ),
            "local_worker_memory_estimate_bytes": (
                LOCAL_WORKER_MEMORY_ESTIMATE_MB * MIB
            ),
            "worker_overhead_memory_estimate_bytes": (
                WORKER_OVERHEAD_MEMORY_ESTIMATE_MB * MIB
            ),
            "combined_memory_per_worker_estimate_bytes": (
                DEFAULT_EXTERNAL_MATCH_MEMORY_PER_WORKER_MB * MIB
            ),
            "default_concurrency_accounts_for_external_process": True,
            "best_settings_resource_gate": {
                "single_match_worker": resources.workers == 1,
                "local_native_threads_fit_detected_logical_cpus": (
                    config.local_native_threads <= resources.detected_logical_cpus
                ),
                "local_native_threads": config.local_native_threads,
                "detected_logical_cpus": resources.detected_logical_cpus,
            },
        },
        "execution": {
            "wall_elapsed_seconds": elapsed_seconds,
            "result_order": "opening-pair-then-color-swap",
            "worker_completion_order_discarded": True,
            "journal": {
                "enabled": journal_root is not None,
                "directory": str(journal_root) if journal_root else None,
                "protocol_sha256": protocol_sha256,
                "resumed_games": len(existing_records),
                "new_games": len(records) - len(existing_records),
                "per_game_atomic": journal_root is not None,
            },
            "identity_drift": identity_drift,
        },
        "opening_suite": {
            "opening_policy": config.opening_policy,
            "opening_series_played": (
                0 if config.opening_policy == OPENING_POLICY_INITIAL else None
            ),
            "engine_play_begins_series": (
                1 if config.opening_policy == OPENING_POLICY_INITIAL else None
            ),
            "unique_starting_positions": (
                1 if config.opening_policy == OPENING_POLICY_INITIAL else len(selected_openings)
            ),
            "scheduled_repetitions_per_color": (
                config.pairs if config.opening_policy == OPENING_POLICY_INITIAL else 1
            ),
            "seed_effect": (
                "none-fixed-initial-position"
                if config.opening_policy == OPENING_POLICY_INITIAL
                else "deterministic-opening-order"
            ),
            "version": config.opening_suite_version,
            "canonical_sha256": opening_context.canonical_sha256,
            "content_addressed": opening_context.content_addressed,
            "generator": (
                dict(opening_context.generator)
                if opening_context.generator is not None
                else None
            ),
            "qualification": (
                config.opening_qualification.as_dict()
                if config.opening_qualification is not None
                else None
            ),
            "payload": opening_suite.as_dict() if opening_suite is not None else None,
        },
        "selected_openings": [
            {
                **opening.as_dict(),
                "canonical_series_history": [
                    list(series)
                    for series in opening_context.histories[opening.case_id]
                ],
            }
            for opening in selected_openings
        ],
        "summary": summary,
        "pairs": list(pairs),
        "games": [record.as_dict() for record in records],
        "rule_and_protocol_gaps": _rule_protocol_gaps(config),
        "claim_scope": {
            "match_intent": config.match_intent,
            "independent_opponent": approved_bucephalus_identity,
            "exact_approved_bucephalus_baseline": approved_bucephalus_identity,
            "fixed_suite_only": True,
            "repeated_initial_state_games": (
                config.pairs * 2
                if config.opening_policy == OPENING_POLICY_INITIAL
                else 0
            ),
            "statistical_independence_limited_by_deterministic_repetition": (
                config.opening_policy == OPENING_POLICY_INITIAL
            ),
            "independent_sample_claim": False,
            "opening_generalization_claim": False,
            "universal_superiority_claim": False,
            "promotion_effect": "none",
            "native_research_engine_only": True,
            "browser_release_equivalence_claim": False,
            "statement": _claim_scope_statement(config),
            "stockfish_level_claim": False,
            "rating_claim": False,
            "all_scheduled_results_required": True,
            "selective_reruns_forbidden": True,
            "local_engine_superiority_supported": superiority_supported,
            "local_engine_superiority_gate": {
                "eligible_protocol_classes": [
                    "fair-equal-wall",
                    "best-settings-head-to-head",
                ],
                "requires_content_addressed_opening_suite": True,
                "requires_equal_end_to_end_common_wall": True,
                "requires_timed_iterative_external_adapter": True,
                "requires_exact_approved_bucephalus_binary_and_build_receipt": True,
                "requires_exact_approved_local_profile": True,
                "requires_no_start_to_finish_identity_drift": True,
                "requires_100_completed_games": True,
                "requires_50_completed_color_swapped_pairs": True,
                "inferential_superiority_eligible": (
                    config.opening_policy != OPENING_POLICY_INITIAL
                ),
                "paired_test_applicable": (
                    config.opening_policy != OPENING_POLICY_INITIAL
                ),
                "inferential_ineligibility_reason": (
                    "repeated-identical-initial-state-is-not-independent"
                    if config.opening_policy == OPENING_POLICY_INITIAL
                    else None
                ),
                "requires_local_pair_wins_above_losses": (
                    config.opening_policy != OPENING_POLICY_INITIAL
                ),
                "paired_two_sided_p_below": (
                    None if config.opening_policy == OPENING_POLICY_INITIAL else 0.05
                ),
                "best_settings_additional_requirements": {
                    "opening_policy": OPENING_POLICY_INITIAL,
                    "opening_series_played": 0,
                    "engine_play_begins_series": 1,
                    "minimum_wall_seconds_per_move": 120.0,
                    "local_depth_series": 8,
                    "local_branch_cap": 32,
                    "local_native_threads": 16,
                    "local_profile_id": APPROVED_BEST_SETTINGS_LOCAL_PROFILE_ID,
                    "local_profile_canonical_sha256": (
                        APPROVED_BEST_SETTINGS_LOCAL_PROFILE_SHA256
                    ),
                    "local_max_work_positions_per_search": 4_000_000_000,
                    "local_max_work_positions_per_game": 100_000_000_000,
                    "bucephalus_maximum_micro_ply": BUCEPHALUS_MAX_PLY,
                },
            },
            "warning": (
                "This is independent-engine game evidence, not calibrated Elo, "
                "a confidence interval, SPRT, equal-node evidence, an "
                "opening-general claim, or proof of universal Stockfish-level strength."
            ),
        },
    }
    if journal_root is not None:
        write_external_match_report(report, journal_root / "report.json")
    return report


def write_external_match_report(
    report: Mapping[str, Any], destination: str | Path
) -> Path:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.bucephalus_fair_rematch",
        description=(
            "Run the content-addressed, color-swapped, equal-wall Scottish "
            "Progressive rematch against a pinned flushed Bucephalus build."
        ),
    )
    parser.add_argument("executable", help="path to the flushed Bucephalus executable")
    parser.add_argument(
        "--sha256", required=True, help="required SHA-256 of the executable"
    )
    parser.add_argument("--upstream-commit", required=True)
    parser.add_argument(
        "--external-build-receipt",
        required=True,
        help="machine-checked JSON build receipt for the supplied executable",
    )
    parser.add_argument("--local-profile", default="baseline")
    parser.add_argument("--pairs", type=int, default=50)
    parser.add_argument("--seed", type=int, default=BUCEPHALUS_FAIR_OPENING_SEED)
    parser.add_argument(
        "--match-intent",
        choices=(FAIR_EQUAL_WALL_MATCH_INTENT, BEST_SETTINGS_MATCH_INTENT),
        default=FAIR_EQUAL_WALL_MATCH_INTENT,
        help=(
            "label the frozen protocol honestly; pair with --opening-policy "
            "to choose fixtures or genuine play from the initial position"
        ),
    )
    parser.add_argument(
        "--opening-policy",
        choices=(OPENING_POLICY_FIXED_SUITE, OPENING_POLICY_INITIAL),
        default=OPENING_POLICY_FIXED_SUITE,
        help=(
            "use canonical fixtures or start every pair from standard Series 1 "
            "with no pre-played series"
        ),
    )
    parser.add_argument(
        "--fresh-opening-seed",
        type=int,
        help=(
            "generate a new content-addressed neutral opening suite with one "
            "unique boundary per color-swapped pair"
        ),
    )
    parser.add_argument("--fresh-opening-min-series", type=int, default=3)
    parser.add_argument("--fresh-opening-max-series", type=int, default=6)
    parser.add_argument("--fresh-opening-frontier-cap", type=int, default=32)
    parser.add_argument("--fresh-opening-candidate-pool", type=int, default=80)
    parser.add_argument("--depth", type=int, default=8)
    parser.add_argument("--branch-cap", type=int, default=32)
    parser.add_argument("--local-native-threads", type=int, default=1)
    parser.add_argument(
        "--max-generation-positions", type=int, default=4_000_000_000
    )
    parser.add_argument(
        "--max-game-work-positions", type=int, default=100_000_000_000
    )
    parser.add_argument("--common-move-seconds", type=float, default=30.0)
    parser.add_argument("--emergency-max-series", type=int, default=18)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--memory-per-worker-mb",
        type=int,
        default=DEFAULT_EXTERNAL_MATCH_MEMORY_PER_WORKER_MB,
    )
    parser.add_argument("--reserve-memory-mb", type=int, default=512)
    parser.add_argument(
        "--journal-directory",
        required=True,
        help="directory for the frozen protocol and per-game atomic records",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", help="optional second copy of the final report")
    parser.add_argument("--json", action="store_true")
    return parser


def _config_and_opening_suite_from_args(
    args: argparse.Namespace,
) -> tuple[ExternalMatchConfig, SeededOpeningSuite | None]:
    opening_qualification: OpeningQualification | None = None
    if args.opening_policy == OPENING_POLICY_INITIAL:
        if args.fresh_opening_seed is not None:
            raise ValueError(
                "initial-position mode forbids fresh or pre-played opening fixtures"
            )
        opening_suite = None
    elif (
        args.match_intent == BEST_SETTINGS_MATCH_INTENT
        and args.fresh_opening_seed is not None
    ):
        opening_suite, opening_qualification = build_engaged_opening_suite(
            seed=args.fresh_opening_seed,
            count=args.pairs,
            candidate_pool_count=args.fresh_opening_candidate_pool,
            max_frontier_states=args.fresh_opening_frontier_cap,
        )
    elif args.fresh_opening_seed is not None:
        opening_suite = build_seeded_opening_suite(
            seed=args.fresh_opening_seed,
            count=args.pairs,
            min_series=args.fresh_opening_min_series,
            max_series=args.fresh_opening_max_series,
            max_frontier_states=args.fresh_opening_frontier_cap,
        )
    else:
        opening_suite = None
    if args.opening_policy == OPENING_POLICY_INITIAL:
        opening_suite_version = INITIAL_POSITION_SUITE_VERSION
        opening_case_ids = (INITIAL_POSITION_CASE.case_id,)
        opening_suite_canonical_sha256 = None
    elif opening_suite is None:
        opening_suite_version = BUCEPHALUS_FAIR_OPENING_SUITE_VERSION
        opening_case_ids = tuple(
            case.case_id for case in BUCEPHALUS_FAIR_OPENING_SUITE
        )
        opening_suite_canonical_sha256 = None
    else:
        opening_suite_version = opening_suite.version
        opening_case_ids = tuple(case.case_id for case in opening_suite.cases)
        opening_suite_canonical_sha256 = _canonical_sha256(
            opening_suite.as_dict()
        )
    config = ExternalMatchConfig(
        pairs=args.pairs,
        seed=args.seed,
        match_intent=args.match_intent,
        opening_policy=args.opening_policy,
        opening_suite_version=opening_suite_version,
        opening_suite_canonical_sha256=opening_suite_canonical_sha256,
        opening_qualification=opening_qualification,
        opening_case_ids=opening_case_ids,
        local_depth_series=args.depth,
        local_max_series_per_node=args.branch_cap,
        local_native_threads=args.local_native_threads,
        local_max_generation_positions=args.max_generation_positions,
        local_max_game_work_positions=args.max_game_work_positions,
        requested_match_workers=args.workers,
        external_ply_policy=TIMED_ITERATIVE_PLY_POLICY,
        external_lookahead_micro_plies=0,
        external_wall_timeout_seconds=args.common_move_seconds,
        common_wall_timeout_seconds=args.common_move_seconds,
        emergency_max_series=args.emergency_max_series,
    )
    return config, opening_suite


def main(argv: Sequence[str] | None = None) -> int:
    from scottish_progressive.strength import resolve_match_profile

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        local_profile = resolve_match_profile(args.local_profile)
        spec = BucephalusSpec(
            Path(args.executable),
            args.sha256,
            upstream_commit=args.upstream_commit,
        )
        config, opening_suite = _config_and_opening_suite_from_args(args)
        progress = None if args.json else (lambda message: print(message, flush=True))
        report = run_external_match(
            local_profile,
            spec,
            config=config,
            requested_workers=args.workers,
            memory_per_worker_mb=args.memory_per_worker_mb,
            reserve_memory_mb=args.reserve_memory_mb,
            progress=progress,
            journal_directory=args.journal_directory,
            resume=args.resume,
            external_build_receipt_path=args.external_build_receipt,
            opening_suite=opening_suite,
        )
        output = (
            write_external_match_report(report, args.output)
            if args.output
            else None
        )
    except ValueError as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        summary = report["summary"]
        print(f"Local profile: {local_profile.name} ({local_profile.profile_id})")
        print(
            "Local games W/D/L: "
            f"{summary['local_game_wdl']['wins']}/"
            f"{summary['local_game_wdl']['draws']}/"
            f"{summary['local_game_wdl']['losses']} "
            f"({summary['incomplete_games']} incomplete)"
        )
        print(report["claim_scope"]["warning"])
        if output is not None:
            print(f"Report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
