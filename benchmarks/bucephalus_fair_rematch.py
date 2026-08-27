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
    ExternalEngineConfigurationError,
    ExternalEngineError,
    ExternalEngineProtocolError,
    ExternalEngineTimeout,
    SeriesHistory,
    analyze_bucephalus,
    analyze_bucephalus_timed_iterative,
    replay_series_history,
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
from scottish_progressive.rules import SeriesLegalityError, play_series
from scottish_progressive.search import SearchLimits, SearchResult, analyze


EXTERNAL_MATCH_FORMAT = "spc-bucephalus-fixed-suite-v1"
EXTERNAL_MATCH_JOURNAL_FORMAT = "spc-bucephalus-match-journal-v1"
EXTERNAL_PLY_POLICY = "series-number-plus-fixed-lookahead-v1"
TIMED_ITERATIVE_PLY_POLICY = "maximum-ply-best-completed-under-wall-budget-v1"
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

ExternalAdapter = Callable[..., ExternalAnalysis]
LocalAnalyzer = Callable[..., SearchResult]


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
    }
)
BUCEPHALUS_OPENING_HISTORIES: Mapping[
    str, Mapping[str, SeriesHistory]
] = MappingProxyType(
    {
        OPENING_SUITE_VERSION: BUCEPHALUS_OPENING_HISTORIES_V1,
        BUCEPHALUS_FAIR_OPENING_SUITE_VERSION: BUCEPHALUS_FAIR_OPENING_HISTORIES,
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
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stable_digest(*parts: object) -> str:
    encoded = "|".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_seed(*parts: object) -> int:
    return int(_stable_digest(*parts)[:16], 16) & 0x7FFFFFFF


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
    opening_suite_version: str = OPENING_SUITE_VERSION
    opening_case_ids: tuple[str, ...] = tuple(
        case.case_id for case in OPENING_SUITE
    )
    local_depth_series: int = 2
    local_max_series_per_node: int = 32
    local_max_generation_positions: int = 250_000
    local_max_game_work_positions: int = 5_000_000
    external_ply_policy: str = EXTERNAL_PLY_POLICY
    external_lookahead_micro_plies: int = 0
    external_wall_timeout_seconds: float = 10.0
    common_wall_timeout_seconds: float | None = None
    emergency_max_series: int = 18

    def __post_init__(self) -> None:
        if self.opening_suite_version not in BUCEPHALUS_OPENING_SUITES:
            raise ValueError(
                f"unsupported opening suite {self.opening_suite_version}"
            )
        available = {
            case.case_id
            for case in BUCEPHALUS_OPENING_SUITES[self.opening_suite_version]
        }
        if not self.opening_case_ids:
            raise ValueError("opening_case_ids cannot be empty")
        if len(set(self.opening_case_ids)) != len(self.opening_case_ids):
            raise ValueError("opening_case_ids cannot contain duplicates")
        if not set(self.opening_case_ids) <= available:
            raise ValueError("opening_case_ids must name active canonical openings")
        if not 1 <= self.pairs <= len(self.opening_case_ids):
            raise ValueError(
                "pairs must be between 1 and the number of unique openings"
            )
        if not 1 <= self.local_depth_series <= 8:
            raise ValueError("local_depth_series must be between 1 and 8")
        if not 1 <= self.local_max_series_per_node <= 512:
            raise ValueError(
                "local_max_series_per_node must be between 1 and 512"
            )
        if self.local_max_generation_positions < 1:
            raise ValueError("local_max_generation_positions must be positive")
        if self.local_max_game_work_positions < 1:
            raise ValueError("local_max_game_work_positions must be positive")
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

    def external_search_ply(self, series_number: int) -> int:
        if self.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY:
            return BUCEPHALUS_MAX_PLY
        return series_number + self.external_lookahead_micro_plies

    @property
    def expected_external_adapter_version(self) -> str:
        if self.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY:
            return BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION
        return BUCEPHALUS_ADAPTER_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "pairs": self.pairs,
            "games": self.pairs * 2,
            "seed": self.seed,
            "opening_suite_version": self.opening_suite_version,
            "opening_suite_canonical_sha256": (
                BUCEPHALUS_OPENING_SUITE_SHA256[self.opening_suite_version]
            ),
            "opening_case_ids": list(self.opening_case_ids),
            "local_limits": {
                "depth_series": self.local_depth_series,
                "branch_cap_complete_series_per_node": (
                    self.local_max_series_per_node
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
                    "deepest-fully-emitted-legal-iteration"
                    if self.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY
                    else "technical-incomplete-*"
                ),
                "node_limit": None,
                "native_time_control": None,
                "timeout_without_complete_iteration": "technical-incomplete-*",
            },
            "common_control": {
                "enabled": self.common_wall_timeout_seconds is not None,
                "wall_seconds_per_move": self.common_wall_timeout_seconds,
                "policy": (
                    "equal-end-to-end-call-wall-engine-native-return-v2"
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


def _ordered_openings(config: ExternalMatchConfig) -> tuple[OpeningCase, ...]:
    by_id = {
        case.case_id: case
        for case in BUCEPHALUS_OPENING_SUITES[config.opening_suite_version]
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
) -> tuple[ExternalGameJob, ...]:
    config_json = json.dumps(
        config.as_dict(), sort_keys=True, separators=(",", ":")
    )
    match_id = "external-" + _stable_digest(
        EXTERNAL_MATCH_FORMAT,
        local_profile.profile_id,
        external_spec.sha256,
        external_spec.upstream_commit,
        config_json,
    )[:20]
    jobs: list[ExternalGameJob] = []
    histories = BUCEPHALUS_OPENING_HISTORIES[config.opening_suite_version]
    for pair_index, opening in enumerate(_ordered_openings(config)):
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
                or int(getattr(stats, "generation_work_limit_hits", 0)) > 0
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
                )
                or external_analysis.executable_sha256.lower()
                != job.external_spec.sha256
                or external_analysis.upstream_commit
                != job.external_spec.upstream_commit
                or external_analysis.adapter_version
                != job.config.expected_external_adapter_version
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
    failure_reasons: dict[str, int] = {}
    failure_owners: dict[str, int] = {}
    for record in records:
        for entry in record.trace:
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
        },
        tuple(pairs),
    )


def _superiority_gate(
    config: ExternalMatchConfig,
    summary: Mapping[str, Any],
    *,
    approved_bucephalus_identity: bool,
    identity_stable: bool,
) -> tuple[bool, bool]:
    fair_equal_wall_protocol = (
        config.opening_suite_version == BUCEPHALUS_FAIR_OPENING_SUITE_VERSION
        and config.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY
        and config.common_wall_timeout_seconds is not None
        and config.pairs == 50
        and len(config.opening_case_ids) == 50
    )
    strict_protocol_complete = (
        fair_equal_wall_protocol
        and summary["scheduled_games"] == 100
        and summary["completed_games"] == 100
        and summary["scheduled_pairs"] == 50
        and summary["completed_pairs"] == 50
    )
    pair_wdl = summary["local_pair_wdl"]
    paired_p = summary["paired_sign_test"]["two_sided_exact_binomial_p"]
    superiority_supported = (
        strict_protocol_complete
        and approved_bucephalus_identity
        and identity_stable
        and pair_wdl["wins"] > pair_wdl["losses"]
        and paired_p is not None
        and paired_p < 0.05
    )
    return strict_protocol_complete, superiority_supported


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
                    "Bucephalus is externally stopped and may return only its "
                    "deepest fully emitted legal iteration. No legal output is an "
                    "incomplete game. Every fallback is counted and disclosed."
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
) -> dict[str, Any]:
    identity = dict(identity_snapshot or _run_identity_snapshot())
    return {
        "format": EXTERNAL_MATCH_JOURNAL_FORMAT,
        "match_id": jobs[0].game_id[:20],
        "local_engine": {
            "engine_version": identity["engine_version"],
            "source_fingerprint": identity["source_fingerprint"],
            "profile": local_profile.as_dict(),
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
        "opening_suite_canonical_sha256": BUCEPHALUS_OPENING_SUITE_SHA256[
            config.opening_suite_version
        ],
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
    return record


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
) -> dict[str, Any]:
    """Runs a fixed, color-swapped local-profile versus Bucephalus match.

    The harness never mutates league/champion state. Only authoritative legal
    checkmate, stalemate, or proven dead-material draw can complete a game;
    every adapter, replay, resource, or emergency limit is serialized as `*`.
    """

    config = config or ExternalMatchConfig()
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
    jobs = _build_jobs(local_profile, external_spec, config)
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
    identity_drift = _identity_drift(identity_snapshot)
    strict_protocol_complete, superiority_supported = _superiority_gate(
        config,
        summary,
        approved_bucephalus_identity=approved_bucephalus_identity,
        identity_stable=not identity_drift["detected"],
    )
    fair_equal_wall_protocol = (
        config.opening_suite_version == BUCEPHALUS_FAIR_OPENING_SUITE_VERSION
        and config.external_ply_policy == TIMED_ITERATIVE_PLY_POLICY
        and config.common_wall_timeout_seconds is not None
        and config.pairs == 50
        and len(config.opening_case_ids) == 50
    )
    summary["strict_100_game_protocol_complete"] = strict_protocol_complete
    summary["fair_equal_wall_protocol"] = fair_equal_wall_protocol
    selected_openings = [job.opening for job in jobs[::2]]
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
            "version": config.opening_suite_version,
            "canonical_sha256": BUCEPHALUS_OPENING_SUITE_SHA256[
                config.opening_suite_version
            ],
            "content_addressed": (
                config.opening_suite_version
                == BUCEPHALUS_FAIR_OPENING_SUITE_VERSION
            ),
            "generator": (
                {
                    "seed": BUCEPHALUS_FAIR_OPENING_METADATA["seed"],
                    "count": BUCEPHALUS_FAIR_OPENING_METADATA["count"],
                    "min_series": BUCEPHALUS_FAIR_OPENING_METADATA["min_series"],
                    "max_series": BUCEPHALUS_FAIR_OPENING_METADATA["max_series"],
                    "max_frontier_states": BUCEPHALUS_FAIR_OPENING_METADATA[
                        "max_frontier_states"
                    ],
                }
                if config.opening_suite_version
                == BUCEPHALUS_FAIR_OPENING_SUITE_VERSION
                else None
            ),
        },
        "selected_openings": [
            {
                **opening.as_dict(),
                "canonical_series_history": [
                    list(series)
                    for series in BUCEPHALUS_OPENING_HISTORIES[
                        config.opening_suite_version
                    ][opening.case_id]
                ],
            }
            for opening in selected_openings
        ],
        "summary": summary,
        "pairs": list(pairs),
        "games": [record.as_dict() for record in records],
        "rule_and_protocol_gaps": _rule_protocol_gaps(config),
        "claim_scope": {
            "independent_opponent": approved_bucephalus_identity,
            "exact_approved_bucephalus_baseline": approved_bucephalus_identity,
            "fixed_suite_only": True,
            "promotion_effect": "none",
            "native_research_engine_only": True,
            "browser_release_equivalence_claim": False,
            "statement": (
                "Results apply only to the selected canonical Scottish "
                "Progressive openings and the exact equal end-to-end call-wall, "
                "engine-native return policy in this report. A disclosed legal "
                "local move-only fallback can be played when no series-depth "
                "iteration completes. Search depth, work units, startup, and "
                "replay overhead remain engine-specific."
                if config.common_wall_timeout_seconds is not None
                else (
                    "Results apply only to the selected canonical Scottish "
                    "Progressive openings and the exact asymmetric policies in "
                    "this report."
                )
            ),
            "stockfish_level_claim": False,
            "rating_claim": False,
            "all_scheduled_results_required": True,
            "selective_reruns_forbidden": True,
            "local_engine_superiority_supported": superiority_supported,
            "local_engine_superiority_gate": {
                "requires_content_addressed_fair_suite": True,
                "requires_equal_end_to_end_common_wall": True,
                "requires_timed_iterative_external_adapter": True,
                "requires_exact_approved_bucephalus_binary_and_build_receipt": True,
                "requires_no_start_to_finish_identity_drift": True,
                "requires_100_completed_games": True,
                "requires_50_completed_color_swapped_pairs": True,
                "requires_local_pair_wins_above_losses": True,
                "paired_two_sided_p_below": 0.05,
            },
            "warning": (
                "This is independent-engine game evidence, not calibrated Elo, "
                "SPRT, equal-node evidence, or proof of Stockfish-level strength."
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
    parser.add_argument("--depth", type=int, default=8)
    parser.add_argument("--branch-cap", type=int, default=32)
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
        config = ExternalMatchConfig(
            pairs=args.pairs,
            seed=args.seed,
            opening_suite_version=BUCEPHALUS_FAIR_OPENING_SUITE_VERSION,
            opening_case_ids=tuple(
                case.case_id for case in BUCEPHALUS_FAIR_OPENING_SUITE
            ),
            local_depth_series=args.depth,
            local_max_series_per_node=args.branch_cap,
            local_max_generation_positions=args.max_generation_positions,
            local_max_game_work_positions=args.max_game_work_positions,
            external_ply_policy=TIMED_ITERATIVE_PLY_POLICY,
            external_lookahead_micro_plies=0,
            external_wall_timeout_seconds=args.common_move_seconds,
            common_wall_timeout_seconds=args.common_move_seconds,
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
            journal_directory=args.journal_directory,
            resume=args.resume,
            external_build_receipt_path=args.external_build_receipt,
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
