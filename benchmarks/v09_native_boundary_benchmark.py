from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator, Mapping, Sequence


SCHEMA = "spc-v09-native-boundary-benchmark-v1"

CASES: dict[str, tuple[str | None, int, int]] = {
    "S1": (None, 1, 0),
    "S3": (
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        3,
        0,
    ),
    "S4": (
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R "
        "b KQkq - 1 3",
        4,
        0,
    ),
    "S22": ("8/8/8/8/6Q1/2K5/6k1/8 b - - 144 109", 22, 8),
}

# This is the exact candidate used by the frozen v0.8 fixed-suite report.
# Keeping it in the harness makes wheel-to-wheel runs independent of build/
# scratch files while preserving the historical run/job identities.
POOL_CANDIDATE_PROFILE: dict[str, Any] = {
    "generation": 1,
    "mutation_seed": None,
    "name": "self-play Texel candidate",
    "notes": (
        "deterministic-texel-coordinate-v1 candidate from "
        "spc-selfplay-corpus-fa0ae60cadbd7a9e4c98; not strength-verified until "
        "tactical and fixed-suite match gates pass."
    ),
    "parent_profile_ids": ["spc-68942034c41b4cc4"],
    "profile_id": "spc-19916d80f2a58da3",
    "recommended_branch_cap": 32,
    "recommended_depth": 2,
    "schema_version": 1,
    "weights": {
        "boundary_check": 25,
        "immediate_vulnerability": 84,
        "king_space": 70,
        "material": 56,
        "promotion_corridors": 25,
        "series_reach": 72,
        "useful_mobility": 25,
    },
}

POOL_CONFIG = {
    "pairs": 10,
    "games": 20,
    "seed": 20260822,
    "min_series": 3,
    "max_series": 6,
    "opening_frontier_cap": 32,
    "depth_series": 2,
    "branch_cap": 32,
    "max_generation_positions": 250_000,
    "max_game_work_positions": 5_000_000,
    "emergency_max_series": None,
}

EXPECTED_POOL_RUN_ID = "strength-0c4daca0fb61596c4bb3"
EXPECTED_POOL_SUITE_VERSION = (
    "spc-neutral-seeded-openings-v1-ed3b19260cfda8691da5"
)
EXPECTED_POOL_OPENING_IDS = (
    "seeded-009-s4-f1d41fb39710",
    "seeded-008-s3-aadb7fa9521d",
    "seeded-012-s3-d49c003124a7",
    "seeded-001-s4-225b1b19241d",
    "seeded-005-s4-fa763395d844",
    "seeded-013-s4-dac469bd75b2",
    "seeded-020-s3-f0a0c2a626dc",
    "seeded-007-s6-f420a0360b9d",
    "seeded-015-s6-3bb1d17e0415",
    "seeded-017-s4-0a16bc0051e4",
)

EXPECTED_RELEASES = {
    "baseline": {
        "engine_version": "spc-0.8.0",
        "distribution_version": "0.8.0",
        "source_fingerprint": "f369b5da69c17c5f",
        "native_source_identity": (
            "4b3ed236917abdfb0939ce63d567d811913f6e6a86548d99011ed3a10c645627"
        ),
    },
    "candidate": {
        "engine_version": "spc-0.9.0",
        "distribution_version": "0.9.0",
        "source_fingerprint": "806aa0d679f6d1ef",
        "native_source_identity": (
            "e7d36c5fc755cca2ae8877f4e73d8f9aa161a405a4c91361a6866d2f6463ca4f"
        ),
    },
}

V09_ZERO_PROMOTION_STATS = {
    "promotion_mate_positions",
    "promotion_mate_setup_states",
    "promotion_mate_candidates",
    "promotion_mate_completion_probes",
    "promotion_mate_limit_hits",
    "promotion_mate_replay_rejects",
    "promotion_mate_mates",
}

KNOWN_SEARCH_RESULT_FIELDS = {
    "score",
    "best_series",
    "principal_variation",
    "alternatives",
    "requested_depth",
    "completed_depth",
    "exact_width",
    "timed_out",
    "elapsed_seconds",
    "stats",
    "root_evaluation",
    "proof",
    "adjudication_status",
    "max_series_per_node",
    "time_limit_seconds",
    "engine_version",
    "source_fingerprint",
    "engine_profile_id",
    "engine_profile_name",
    "required_prefix",
    "work_limit_reached",
    "max_generation_positions",
    "root_scores_complete",
}

POOL_ACCOUNTING_TRACE_FIELDS = {
    "nodes",
    "root_bound_candidates",
    "work_positions",
    "series_generation_positions",
    "promotion_mate_positions",
    "promotion_mate_setup_states",
    "promotion_mate_candidates",
    "promotion_mate_completion_probes",
    "promotion_mate_mates",
    "promotion_mate_limit_hits",
    "promotion_mate_replay_rejects",
    "evaluation_reach_positions",
    "evaluation_capture_positions",
    "tactical_leaf_extensions",
    "quiet_adjudication_positions",
    "game_work_positions",
    "search_work_limit",
    "reduced_for_game_budget",
}

MAX_DIFFERENCE_EXAMPLES = 40


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    label: str
    python: Path
    artifact: dict[str, Any]
    native_package: Path | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _prepare_package() -> None:
    """Optionally inject a source-matched native package for local smoke runs."""

    import scottish_progressive

    override = os.environ.get("SPC_BENCHMARK_NATIVE_PACKAGE")
    if override:
        native_package = Path(override).resolve()
        if not native_package.is_dir():
            raise RuntimeError("SPC_BENCHMARK_NATIVE_PACKAGE is not a directory")
        rendered = str(native_package)
        if rendered not in scottish_progressive.__path__:
            scottish_progressive.__path__.insert(0, rendered)


def _module_metadata(label: str, module: Any) -> dict[str, str]:
    module_path = Path(module.__file__).resolve()
    return {
        "label": label,
        "filename": module_path.name,
        "sha256": _sha256(module_path),
    }


def _runtime_metadata() -> dict[str, Any]:
    _prepare_package()
    import scottish_progressive
    import chess
    from scottish_progressive import evaluation, model, rules, search

    native = evaluation._native_eval
    if native is None:
        raise RuntimeError("a native runtime is required for this benchmark")
    native_identity = str(getattr(native, "SOURCE_IDENTITY", ""))
    if not native_identity:
        raise RuntimeError("the native runtime does not expose SOURCE_IDENTITY")
    expected_identity = str(evaluation._native_source_identity())
    if native_identity != expected_identity:
        raise RuntimeError(
            "native binary/source identity mismatch: "
            f"loaded={native_identity}, expected={expected_identity}"
        )

    module_rows = [
        _module_metadata("package", scottish_progressive),
        _module_metadata("model", model),
        _module_metadata("evaluation", evaluation),
        _module_metadata("rules", rules),
        _module_metadata("search", search),
        _module_metadata("chess-dependency", chess),
    ]
    module_hashes = {row["label"]: row["sha256"] for row in module_rows}
    native_path = Path(native.__file__).resolve()
    dependencies: dict[str, str] = {}
    for distribution in ("scottish-progressive", "python-chess", "chess"):
        try:
            dependencies[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            dependencies[distribution] = "not-installed-as-distribution"

    return {
        "engine": {
            "version": model.ENGINE_VERSION,
            "source_fingerprint": model.ENGINE_SOURCE_FINGERPRINT,
        },
        "native": {
            "filename": native_path.name,
            "sha256": _sha256(native_path),
            "source_identity": native_identity,
            "capabilities": sorted(
                name
                for name in (
                    "expand_legal_move_variants",
                    "full_evaluate",
                    "generate_complete_series",
                    "prepare_complete_series",
                    "complete_series_candidate",
                )
                if hasattr(native, name)
            ),
        },
        "modules": module_rows,
        "module_set_sha256": _canonical_sha256(module_hashes),
        "distributions": dependencies,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "compiler": platform.python_compiler(),
            "executable_filename": Path(sys.executable).name,
        },
        "platform": {
            "description": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "cpu": platform.processor()
            or os.environ.get("PROCESSOR_IDENTIFIER")
            or "unknown",
            "logical_cpu_count": os.cpu_count(),
        },
    }


def _series_payload(series: Any | None) -> dict[str, Any] | None:
    if series is None:
        return None
    return {
        "moves": list(series.moves),
        "san": list(series.san),
        "final_pfen": series.final_state.pfen,
        "ended_by_check": series.ended_by_check,
        "outcome": series.outcome.value if series.outcome is not None else None,
        "unused_moves": series.unused_moves,
        "transposition_count": series.transposition_count,
    }


def _search_result_payload(result: Any) -> dict[str, Any]:
    result_fields = {field.name for field in fields(result)}
    if result_fields != KNOWN_SEARCH_RESULT_FIELDS:
        missing = sorted(KNOWN_SEARCH_RESULT_FIELDS - result_fields)
        unknown = sorted(result_fields - KNOWN_SEARCH_RESULT_FIELDS)
        raise RuntimeError(
            "SearchResult schema drift; update the benchmark serializer "
            f"(missing={missing}, unknown={unknown})"
        )
    alternatives = [
        {
            "series": _series_payload(item.series),
            "score": item.score,
            "principal_variation": [
                _series_payload(series) for series in item.principal_variation
            ],
            "proof_bounds": list(item.proof_bounds),
            "proof": item.proof,
        }
        for item in result.alternatives
    ]
    return {
        "dataclass_fields": sorted(result_fields),
        "score": result.score,
        "best_series": _series_payload(result.best_series),
        "principal_variation": [
            _series_payload(series) for series in result.principal_variation
        ],
        "alternatives": alternatives,
        "requested_depth": result.requested_depth,
        "completed_depth": result.completed_depth,
        "exact_width": result.exact_width,
        "timed_out": result.timed_out,
        "elapsed_seconds": result.elapsed_seconds,
        "stats": asdict(result.stats),
        "root_evaluation": asdict(result.root_evaluation),
        "proof": result.proof,
        "adjudication_status": result.adjudication_status,
        "max_series_per_node": result.max_series_per_node,
        "time_limit_seconds": result.time_limit_seconds,
        "engine_version": result.engine_version,
        "source_fingerprint": result.source_fingerprint,
        "engine_profile_id": result.engine_profile_id,
        "engine_profile_name": result.engine_profile_name,
        "required_prefix": list(result.required_prefix),
        "work_limit_reached": result.work_limit_reached,
        "max_generation_positions": result.max_generation_positions,
        "root_scores_complete": result.root_scores_complete,
        "forced": result.forced,
    }


def _search_worker(
    case_name: str,
    depth: int,
    branch_cap: int,
    max_work: int,
) -> int:
    metadata = _runtime_metadata()
    from scottish_progressive.model import ProgressiveState
    from scottish_progressive.profiles import baseline_profile
    from scottish_progressive.search import SearchLimits, analyze

    fen, series_number, quiet_series = CASES[case_name]
    state = (
        ProgressiveState.initial()
        if fen is None
        else ProgressiveState.from_fen(
            fen,
            series_number,
            quiet_series=quiet_series,
        )
    )
    started = time.perf_counter()
    result = analyze(
        state,
        SearchLimits(
            depth_series=depth,
            max_series_per_node=branch_cap,
            max_generation_positions=max_work,
            collect_all_root_scores=False,
        ),
        baseline_profile(),
    )
    wall_seconds = time.perf_counter() - started
    print(
        _canonical(
            {
                "case": case_name,
                "wall_seconds": wall_seconds,
                "runtime": metadata,
                "search_result": _search_result_payload(result),
            }
        )
    )
    return 0


def _pool_initializer(expected_native_identity: str) -> None:
    """Load the native override before Windows workers unpickle GameJob."""

    _prepare_package()
    from scottish_progressive import evaluation

    native = evaluation._native_eval
    actual_identity = str(getattr(native, "SOURCE_IDENTITY", ""))
    if actual_identity != expected_native_identity:
        raise RuntimeError(
            "pool worker native identity drift: "
            f"loaded={actual_identity}, expected={expected_native_identity}"
        )


def _pool_game(job: Any, expected_native_identity: str) -> tuple[Any, dict[str, Any]]:
    _pool_initializer(expected_native_identity)
    from scottish_progressive.league import _play_game

    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    record = _play_game(job)
    return record, {
        "pid": os.getpid(),
        "wall_seconds": time.perf_counter() - wall_started,
        "cpu_seconds": time.process_time() - cpu_started,
    }


def _distribution(values: Sequence[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    percentiles = statistics.quantiles(ordered, n=100, method="inclusive")
    return {
        "count": len(ordered),
        "minimum": ordered[0],
        "p25": percentiles[24],
        "median": statistics.median(ordered),
        "mean": statistics.fmean(ordered),
        "p75": percentiles[74],
        "p90": percentiles[89],
        "p95": percentiles[94],
        "maximum": ordered[-1],
    }


def _pool_semantic_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["trace"] = [
        {
            key: value
            for key, value in item.items()
            if key not in POOL_ACCOUNTING_TRACE_FIELDS
        }
        for item in payload["trace"]
    ]
    return normalized


def _pool_worker(workers: int) -> int:
    metadata = _runtime_metadata()
    from scottish_progressive.profiles import EngineProfile, baseline_profile
    import scottish_progressive.strength as strength

    candidate = EngineProfile.from_dict(POOL_CANDIDATE_PROFILE)
    reference = baseline_profile()
    suite = strength.build_seeded_opening_suite(
        seed=POOL_CONFIG["seed"],
        count=POOL_CONFIG["games"],
        min_series=POOL_CONFIG["min_series"],
        max_series=POOL_CONFIG["max_series"],
        max_frontier_states=POOL_CONFIG["opening_frontier_cap"],
    )
    config = strength.StrengthMatchConfig(
        pairs=POOL_CONFIG["pairs"],
        seed=POOL_CONFIG["seed"],
        search_depth=POOL_CONFIG["depth_series"],
        max_series_per_node=POOL_CONFIG["branch_cap"],
        max_generation_positions=POOL_CONFIG["max_generation_positions"],
        max_game_work_positions=POOL_CONFIG["max_game_work_positions"],
        emergency_max_series=POOL_CONFIG["emergency_max_series"],
        opening_case_ids=tuple(case.case_id for case in suite.cases),
        opening_suite_version=suite.version,
    )
    jobs = strength._build_jobs(candidate, reference, config, suite)
    if suite.version != EXPECTED_POOL_SUITE_VERSION:
        raise RuntimeError(
            "fixed pool opening-suite drift: "
            f"loaded={suite.version}, expected={EXPECTED_POOL_SUITE_VERSION}"
        )
    if len(jobs) != 20 or jobs[0].run_id != EXPECTED_POOL_RUN_ID:
        raise RuntimeError(
            "fixed pool job identity drift: "
            f"count={len(jobs)}, run_id={jobs[0].run_id if jobs else None}"
        )
    selected_opening_ids = tuple(job.opening.case_id for job in jobs[::2])
    if selected_opening_ids != EXPECTED_POOL_OPENING_IDS:
        raise RuntimeError(
            "fixed pool opening order drift: "
            f"loaded={selected_opening_ids}, expected={EXPECTED_POOL_OPENING_IDS}"
        )
    expected_native_identity = metadata["native"]["source_identity"]

    records: dict[str, Any] = {}
    measurements: dict[str, dict[str, Any]] = {}
    submitted_at: dict[str, float] = {}
    pool_started = time.perf_counter()
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_pool_initializer,
        initargs=(expected_native_identity,),
    ) as executor:
        futures = {}
        for job in jobs:
            submitted_at[job.job_key] = time.perf_counter()
            future = executor.submit(_pool_game, job, expected_native_identity)
            futures[future] = job
        for future in as_completed(futures):
            job = futures[future]
            record, timing = future.result()
            timing["submission_to_completion_seconds"] = (
                time.perf_counter() - submitted_at[job.job_key]
            )
            records[job.job_key] = record
            measurements[job.job_key] = timing
    pool_wall_seconds = time.perf_counter() - pool_started

    ordered_records = tuple(records[job.job_key] for job in jobs)
    summary, _ = strength._summarize(ordered_records, candidate, reference)
    game_rows: list[dict[str, Any]] = []
    for job, record in zip(jobs, ordered_records, strict=True):
        full_payload = strength._game_payload(record, job.opening)
        semantic_payload = _pool_semantic_payload(full_payload)
        timing = measurements[job.job_key]
        game_rows.append(
            {
                "job_key": job.job_key,
                "opening_index": job.opening_index,
                "opening_case_id": job.opening.case_id,
                "full_payload_sha256": _canonical_sha256(full_payload),
                "semantic_payload_sha256": _canonical_sha256(semantic_payload),
                "semantic_payload": semantic_payload,
                "measurement": {
                    "worker_wall_seconds": timing["wall_seconds"],
                    "worker_cpu_seconds": timing["cpu_seconds"],
                    "submission_to_completion_seconds": timing[
                        "submission_to_completion_seconds"
                    ],
                },
                "worker_pid": timing["pid"],
            }
        )

    worker_wall = [
        row["measurement"]["worker_wall_seconds"] for row in game_rows
    ]
    worker_cpu = [
        row["measurement"]["worker_cpu_seconds"] for row in game_rows
    ]
    deterministic_games = [
        {
            key: value
            for key, value in row.items()
            if key not in {"measurement", "worker_pid"}
        }
        for row in game_rows
    ]
    workload = {
        **POOL_CONFIG,
        "candidate_profile": candidate.as_dict(),
        "reference_profile": reference.as_dict(),
        "opening_suite_version": suite.version,
        "selected_openings": [job.opening.as_dict() for job in jobs[::2]],
        "job_keys": [job.job_key for job in jobs],
        "run_id": jobs[0].run_id,
        "workers": workers,
        "sampling_order": "opening-pair-then-color-swap",
    }
    print(
        _canonical(
            {
                "runtime": metadata,
                "workload": workload,
                "pool_wall_seconds": pool_wall_seconds,
                "scheduled_games_per_second": len(jobs) / pool_wall_seconds,
                "completed_games_per_second": (
                    summary["completed_games"] / pool_wall_seconds
                ),
                "summary": summary,
                "deterministic_payload_sha256": _canonical_sha256(
                    {"workload": workload, "summary": summary, "games": deterministic_games}
                ),
                "per_game_worker_wall_seconds": _distribution(worker_wall),
                "per_game_worker_cpu_seconds": _distribution(worker_cpu),
                "distinct_worker_processes": len(
                    {row["worker_pid"] for row in game_rows}
                ),
                "games": game_rows,
            }
        )
    )
    return 0


def _clean_environment(native_package: Path | None) -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "PYTHONHOME",
        "PYTHONPATH",
        "SPC_BENCHMARK_NATIVE_PACKAGE",
        "SPC_DISABLE_NATIVE",
    ):
        environment.pop(key, None)
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONNOUSERSITE"] = "1"
    if native_package is not None:
        environment["SPC_BENCHMARK_NATIVE_PACKAGE"] = str(native_package.resolve())
    return environment


def _runtime_artifact(kind: str, path: Path) -> dict[str, Any]:
    return {
        "kind": kind,
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _venv_python(root: Path) -> Path:
    return (
        root / "Scripts" / "python.exe"
        if os.name == "nt"
        else root / "bin" / "python"
    )


def _run_checked(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        env=_clean_environment(None),
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"runtime preparation failed: {detail}")
    return completed


def _install_wheel_runtime(
    *,
    label: str,
    wheel: Path,
    root: Path,
    base_python: Path,
    wheelhouses: Sequence[Path],
    no_index: bool,
) -> RuntimeSpec:
    runtime_root = root / label
    _run_checked(
        [str(base_python), "-m", "venv", str(runtime_root)],
        cwd=root,
    )
    python = _venv_python(runtime_root)
    install_command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
    ]
    if no_index:
        install_command.append("--no-index")
    for wheelhouse in wheelhouses:
        install_command.extend(("--find-links", str(wheelhouse)))
    install_command.append(str(wheel))
    _run_checked(install_command, cwd=root)
    return RuntimeSpec(
        label=label,
        python=python,
        artifact=_runtime_artifact("wheel", wheel),
    )


@contextmanager
def _runtimes(args: argparse.Namespace) -> Iterator[tuple[RuntimeSpec, RuntimeSpec, Path]]:
    with tempfile.TemporaryDirectory(prefix="spc-v09-benchmark-") as temporary:
        root = Path(temporary)
        if args.baseline_wheel is not None:
            baseline = _install_wheel_runtime(
                label="baseline",
                wheel=args.baseline_wheel.resolve(),
                root=root,
                base_python=args.venv_python.resolve(),
                wheelhouses=tuple(path.resolve() for path in args.wheelhouse),
                no_index=args.no_index,
            )
            candidate = _install_wheel_runtime(
                label="candidate",
                wheel=args.candidate_wheel.resolve(),
                root=root,
                base_python=args.venv_python.resolve(),
                wheelhouses=tuple(path.resolve() for path in args.wheelhouse),
                no_index=args.no_index,
            )
        else:
            baseline = RuntimeSpec(
                label="baseline",
                python=args.baseline_python.resolve(),
                artifact=_runtime_artifact(
                    "existing-interpreter", args.baseline_python.resolve()
                ),
                native_package=(
                    args.baseline_native_package.resolve()
                    if args.baseline_native_package is not None
                    else None
                ),
            )
            candidate = RuntimeSpec(
                label="candidate",
                python=args.candidate_python.resolve(),
                artifact=_runtime_artifact(
                    "existing-interpreter", args.candidate_python.resolve()
                ),
                native_package=(
                    args.candidate_native_package.resolve()
                    if args.candidate_native_package is not None
                    else None
                ),
            )
        yield baseline, candidate, root


def _invoke_json(
    runtime: RuntimeSpec,
    arguments: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    completed = subprocess.run(
        [runtime.python, Path(__file__).resolve(), *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        env=_clean_environment(runtime.native_package),
        timeout=timeout_seconds,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"{runtime.label} worker failed: {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{runtime.label} worker emitted invalid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"{runtime.label} worker JSON root is not an object")
    return payload


def _without_search_timing(payload: Mapping[str, Any]) -> dict[str, Any]:
    deterministic = dict(payload)
    deterministic.pop("elapsed_seconds", None)
    return deterministic


def _json_differences(
    baseline: Any,
    candidate: Any,
    path: str = "",
) -> Iterator[dict[str, Any]]:
    if isinstance(baseline, Mapping) and isinstance(candidate, Mapping):
        for key in sorted(set(baseline) | set(candidate)):
            child_path = f"{path}/{key}"
            if key not in baseline:
                yield {
                    "path": child_path,
                    "baseline": {"missing": True},
                    "candidate": candidate[key],
                }
            elif key not in candidate:
                yield {
                    "path": child_path,
                    "baseline": baseline[key],
                    "candidate": {"missing": True},
                }
            else:
                yield from _json_differences(
                    baseline[key], candidate[key], child_path
                )
        return
    if isinstance(baseline, list) and isinstance(candidate, list):
        shared = min(len(baseline), len(candidate))
        for index in range(shared):
            yield from _json_differences(
                baseline[index], candidate[index], f"{path}/{index}"
            )
        for index in range(shared, len(baseline)):
            yield {
                "path": f"{path}/{index}",
                "baseline": baseline[index],
                "candidate": {"missing": True},
            }
        for index in range(shared, len(candidate)):
            yield {
                "path": f"{path}/{index}",
                "baseline": {"missing": True},
                "candidate": candidate[index],
            }
        return
    if type(baseline) is not type(candidate) or baseline != candidate:
        yield {"path": path or "/", "baseline": baseline, "candidate": candidate}


def _difference_reason(
    case_name: str,
    difference: Mapping[str, Any],
) -> str | None:
    path = str(difference["path"])
    baseline = difference["baseline"]
    candidate = difference["candidate"]
    if path == "/engine_version" and (
        baseline,
        candidate,
    ) == (
        EXPECTED_RELEASES["baseline"]["engine_version"],
        EXPECTED_RELEASES["candidate"]["engine_version"],
    ):
        return "pinned_release_identity"
    if path == "/source_fingerprint" and (
        baseline,
        candidate,
    ) == (
        EXPECTED_RELEASES["baseline"]["source_fingerprint"],
        EXPECTED_RELEASES["candidate"]["source_fingerprint"],
    ):
        return "pinned_release_identity"
    if (
        case_name == "S4"
        and path == "/forced"
        and baseline is None
        and candidate == "black"
    ):
        return "pinned_sound_proof_correction"
    stats_prefix = "/stats/"
    if path.startswith(stats_prefix):
        stat_name = path.removeprefix(stats_prefix)
        if (
            stat_name in V09_ZERO_PROMOTION_STATS
            and baseline == {"missing": True}
            and candidate == 0
        ):
            return "pinned_v09_zero_counter_addition"
    return None


def _compare_search_results(
    case_name: str,
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    differences = list(_json_differences(baseline, candidate))
    by_reason: dict[str, list[dict[str, Any]]] = {}
    unallowed: list[dict[str, Any]] = []
    for difference in differences:
        reason = _difference_reason(case_name, difference)
        if reason is None:
            unallowed.append(difference)
        else:
            by_reason.setdefault(reason, []).append(difference)
    return {
        "full_payload_exact": not differences,
        "semantic_equal_under_declared_allowances": not unallowed,
        "difference_count": len(differences),
        "allowed_difference_counts": {
            reason: len(items) for reason, items in sorted(by_reason.items())
        },
        "allowed_difference_examples": {
            reason: items[:MAX_DIFFERENCE_EXAMPLES]
            for reason, items in sorted(by_reason.items())
        },
        "unallowed_difference_count": len(unallowed),
        "unallowed_difference_examples": unallowed[:MAX_DIFFERENCE_EXAMPLES],
        "difference_examples_truncated": any(
            len(items) > MAX_DIFFERENCE_EXAMPLES for items in by_reason.values()
        )
        or len(unallowed) > MAX_DIFFERENCE_EXAMPLES,
    }


def _validate_runtime_version(
    runtime: RuntimeSpec,
    metadata: Mapping[str, Any],
    expected: Mapping[str, str],
) -> None:
    actual_engine = metadata["engine"]["version"]
    if actual_engine != expected["engine_version"]:
        raise RuntimeError(
            f"{runtime.label} engine version is {actual_engine}, expected "
            f"{expected['engine_version']}"
        )
    actual_distribution = metadata["distributions"]["scottish-progressive"]
    if actual_distribution != expected["distribution_version"]:
        raise RuntimeError(
            f"{runtime.label} distribution version is {actual_distribution}, "
            f"expected {expected['distribution_version']}"
        )
    actual_fingerprint = metadata["engine"]["source_fingerprint"]
    if actual_fingerprint != expected["source_fingerprint"]:
        raise RuntimeError(
            f"{runtime.label} source fingerprint is {actual_fingerprint}, "
            f"expected {expected['source_fingerprint']}"
        )
    actual_native = metadata["native"]["source_identity"]
    if actual_native != expected["native_source_identity"]:
        raise RuntimeError(
            f"{runtime.label} native identity is {actual_native}, "
            f"expected {expected['native_source_identity']}"
        )


def _run_search_benchmarks(
    args: argparse.Namespace,
    baseline: RuntimeSpec,
    candidate: RuntimeSpec,
    cwd: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    runtime_order = (baseline, candidate)
    metadata: dict[str, dict[str, Any]] = {}
    cases: dict[str, Any] = {}
    for case_index, case_name in enumerate(args.case):
        timings = {runtime.label: [] for runtime in runtime_order}
        signatures: dict[str, dict[str, Any]] = {}
        raw_samples: list[dict[str, Any]] = []
        for repetition in range(args.samples):
            ordered_runtimes = (
                runtime_order
                if (repetition + case_index) % 2 == 0
                else tuple(reversed(runtime_order))
            )
            for order_index, runtime in enumerate(ordered_runtimes):
                sample = _invoke_json(
                    runtime,
                    (
                        "--worker-search",
                        case_name,
                        "--depth",
                        str(args.depth),
                        "--branch-cap",
                        str(args.branch_cap),
                        "--max-work",
                        str(args.max_work),
                    ),
                    cwd=cwd,
                    timeout_seconds=args.worker_timeout_seconds,
                )
                prior_metadata = metadata.setdefault(runtime.label, sample["runtime"])
                if sample["runtime"] != prior_metadata:
                    raise RuntimeError(f"{runtime.label} runtime identity drift")
                signature = sample["search_result"]
                deterministic = _without_search_timing(signature)
                prior_signature = signatures.setdefault(runtime.label, deterministic)
                if deterministic != prior_signature:
                    raise RuntimeError(
                        f"{case_name} {runtime.label} search output is non-deterministic"
                    )
                elapsed = float(sample["wall_seconds"])
                timings[runtime.label].append(elapsed)
                raw_samples.append(
                    {
                        "repetition": repetition,
                        "order_index": order_index,
                        "runtime": runtime.label,
                        "wall_seconds": elapsed,
                    }
                )

        baseline_median = statistics.median(timings[baseline.label])
        candidate_median = statistics.median(timings[candidate.label])
        baseline_result = signatures[baseline.label]
        candidate_result = signatures[candidate.label]
        comparison = _compare_search_results(
            case_name, baseline_result, candidate_result
        )
        if not comparison["semantic_equal_under_declared_allowances"]:
            examples = comparison["unallowed_difference_examples"]
            raise RuntimeError(
                f"{case_name} has undeclared semantic drift: {examples}"
            )
        cases[case_name] = {
            "baseline_seconds": timings[baseline.label],
            "candidate_seconds": timings[candidate.label],
            "baseline_median_seconds": baseline_median,
            "candidate_median_seconds": candidate_median,
            "speedup": baseline_median / candidate_median,
            "raw_samples": raw_samples,
            "baseline_result_sha256": _canonical_sha256(baseline_result),
            "candidate_result_sha256": _canonical_sha256(candidate_result),
            "comparison": comparison,
            "baseline_search_result": baseline_result,
            "candidate_search_result": candidate_result,
        }

    if set(metadata) != {baseline.label, candidate.label}:
        raise RuntimeError("both runtime identities were not sampled")
    speedups = [float(case["speedup"]) for case in cases.values()]
    aggregate = {
        "case_count": len(cases),
        "geometric_mean_speedup": math.prod(speedups) ** (1 / len(speedups)),
        "sum_of_case_medians_speedup": sum(
            case["baseline_median_seconds"] for case in cases.values()
        )
        / sum(case["candidate_median_seconds"] for case in cases.values()),
        "all_cases_semantically_equal_under_declared_allowances": all(
            case["comparison"]["semantic_equal_under_declared_allowances"]
            for case in cases.values()
        ),
    }
    return cases, metadata, aggregate


def _pool_deterministic_payload(sample: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "workload": sample["workload"],
        "summary": sample["summary"],
        "games": [
            {
                key: value
                for key, value in row.items()
                if key not in {"measurement", "worker_pid"}
            }
            for row in sample["games"]
        ],
    }


def _compare_pool_samples(
    baseline_sample: Mapping[str, Any],
    candidate_sample: Mapping[str, Any],
) -> dict[str, Any]:
    if baseline_sample["workload"] != candidate_sample["workload"]:
        differences = list(
            _json_differences(
                baseline_sample["workload"], candidate_sample["workload"]
            )
        )
        raise RuntimeError(
            "pool workload drift between runtimes: "
            f"{differences[:MAX_DIFFERENCE_EXAMPLES]}"
        )
    baseline_games = {
        row["job_key"]: row for row in baseline_sample["games"]
    }
    candidate_games = {
        row["job_key"]: row for row in candidate_sample["games"]
    }
    if set(baseline_games) != set(candidate_games):
        raise RuntimeError("pool job keys differ between runtimes")

    full_mismatches: list[str] = []
    semantic_mismatches: list[dict[str, Any]] = []
    for job_key in baseline_sample["workload"]["job_keys"]:
        baseline_game = baseline_games[job_key]
        candidate_game = candidate_games[job_key]
        if (
            baseline_game["full_payload_sha256"]
            != candidate_game["full_payload_sha256"]
        ):
            full_mismatches.append(job_key)
        if baseline_game["semantic_payload"] != candidate_game["semantic_payload"]:
            differences = list(
                _json_differences(
                    baseline_game["semantic_payload"],
                    candidate_game["semantic_payload"],
                )
            )
            semantic_mismatches.append(
                {
                    "job_key": job_key,
                    "difference_count": len(differences),
                    "difference_examples": differences[:MAX_DIFFERENCE_EXAMPLES],
                }
            )
    return {
        "same_exact_20_jobs": True,
        "full_payload_exact_game_count": 20 - len(full_mismatches),
        "full_payload_mismatched_job_keys": full_mismatches,
        "game_semantics_exact_game_count": 20 - len(semantic_mismatches),
        "game_semantics_mismatches": semantic_mismatches,
        "all_20_game_semantics_exact": not semantic_mismatches,
        "summary_exact": baseline_sample["summary"] == candidate_sample["summary"],
    }


def _run_pool_benchmark(
    args: argparse.Namespace,
    baseline: RuntimeSpec,
    candidate: RuntimeSpec,
    cwd: Path,
    search_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    runtimes = (baseline, candidate)
    samples: dict[str, list[dict[str, Any]]] = {
        runtime.label: [] for runtime in runtimes
    }
    deterministic: dict[str, dict[str, Any]] = {}
    raw_order: list[dict[str, Any]] = []
    for repetition in range(args.pool_samples):
        ordered = runtimes if repetition % 2 == 0 else tuple(reversed(runtimes))
        for order_index, runtime in enumerate(ordered):
            sample = _invoke_json(
                runtime,
                ("--worker-pool", str(args.pool_workers)),
                cwd=cwd,
                timeout_seconds=args.worker_timeout_seconds,
            )
            if sample["runtime"] != search_metadata[runtime.label]:
                raise RuntimeError(f"{runtime.label} pool/search runtime identity drift")
            current = _pool_deterministic_payload(sample)
            prior = deterministic.setdefault(runtime.label, current)
            if current != prior:
                raise RuntimeError(f"{runtime.label} pool output is non-deterministic")
            samples[runtime.label].append(sample)
            raw_order.append(
                {
                    "repetition": repetition,
                    "order_index": order_index,
                    "runtime": runtime.label,
                    "pool_wall_seconds": sample["pool_wall_seconds"],
                }
            )

    baseline_wall = [
        float(sample["pool_wall_seconds"]) for sample in samples[baseline.label]
    ]
    candidate_wall = [
        float(sample["pool_wall_seconds"]) for sample in samples[candidate.label]
    ]
    comparison = _compare_pool_samples(
        samples[baseline.label][0], samples[candidate.label][0]
    )
    if not comparison["all_20_game_semantics_exact"]:
        raise RuntimeError(
            "fixed 20-game trajectories differ between runtimes; throughput is "
            f"not comparable: {comparison['game_semantics_mismatches']}"
        )
    if not comparison["summary_exact"]:
        raise RuntimeError(
            "fixed 20-game summary differs between runtimes; throughput is not "
            "comparable"
        )
    baseline_median = statistics.median(baseline_wall)
    candidate_median = statistics.median(candidate_wall)
    return {
        "configuration": {
            **POOL_CONFIG,
            "workers": args.pool_workers,
            "fresh_pool_samples": args.pool_samples,
            "sampling_order": "paired-interleaved-alternating-by-runtime",
            "timing_scope": "process-pool-start-through-all-20-games-complete",
        },
        "baseline_pool_seconds": baseline_wall,
        "candidate_pool_seconds": candidate_wall,
        "baseline_pool_median_seconds": baseline_median,
        "candidate_pool_median_seconds": candidate_median,
        "pool_speedup": baseline_median / candidate_median,
        "candidate_scheduled_games_per_second": 20 / candidate_median,
        "throughput_comparable": True,
        "raw_samples": raw_order,
        "comparison": comparison,
        "baseline": samples[baseline.label][0],
        "candidate": samples[candidate.label][0],
    }


def _assert_no_absolute_paths(value: Any, *, key: str | None = None) -> None:
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _assert_no_absolute_paths(child, key=str(child_key))
        return
    if isinstance(value, list):
        for child in value:
            _assert_no_absolute_paths(child, key=key)
        return
    if not isinstance(value, str) or key == "path":
        return
    if PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute():
        raise RuntimeError("benchmark payload contains an absolute filesystem path")


def _run(args: argparse.Namespace) -> dict[str, Any]:
    with _runtimes(args) as (baseline, candidate, cwd):
        cases, metadata, aggregate = _run_search_benchmarks(
            args, baseline, candidate, cwd
        )
        if args.baseline_wheel is not None:
            _validate_runtime_version(
                baseline,
                metadata[baseline.label],
                EXPECTED_RELEASES[baseline.label],
            )
            _validate_runtime_version(
                candidate,
                metadata[candidate.label],
                EXPECTED_RELEASES[candidate.label],
            )
        pool = (
            _run_pool_benchmark(
                args, baseline, candidate, cwd, metadata
            )
            if args.include_pool
            else None
        )
        configuration = {
            "cases": list(args.case),
            "depth_series": args.depth,
            "branch_cap": args.branch_cap,
            "max_generation_positions": args.max_work,
            "fresh_process_samples": args.samples,
            "sampling_order": "paired-interleaved-alternating-by-runtime-and-case",
            "search_timing_scope": "analyze-call-only-after-import-and-position-setup",
            "subprocess_startup_excluded_from_search_timing": True,
            "include_exact_20_game_pool": args.include_pool,
            "declared_cross_release_allowances": {
                "pinned_release_identity": {
                    "engine_version": ["spc-0.8.0", "spc-0.9.0"],
                    "source_fingerprint": [
                        EXPECTED_RELEASES["baseline"]["source_fingerprint"],
                        EXPECTED_RELEASES["candidate"]["source_fingerprint"],
                    ],
                },
                "pinned_sound_proof_correction": {
                    "case": "S4",
                    "field": "SearchResult.forced",
                    "transition": [None, "black"],
                    "proof_field_remains": "black",
                },
                "pinned_v09_zero_counter_addition": [
                    f"SearchResult.stats.{name}: missing -> 0"
                    for name in sorted(V09_ZERO_PROMOTION_STATS)
                ],
            },
        }
        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "generated_at": datetime.now(UTC).isoformat(),
            "artifacts": {
                baseline.label: baseline.artifact,
                candidate.label: candidate.artifact,
            },
            "configuration": configuration,
            "runtimes": metadata,
            "cases": cases,
            "aggregate": aggregate,
            "exact_20_game_pool": pool,
            "measurement_limits": [
                "Fresh-process medians reduce warm-cache and import-state bias but do not "
                "remove operating-system scheduling noise.",
                "Search timing excludes interpreter startup, imports, and position setup.",
                "Pool timing includes worker startup and measures throughput under fixed "
                "16-worker contention by default.",
                "Game strength is not inferred from speed; semantic and promotion gates "
                "remain separate evidence.",
            ],
        }
        artifact_seed = {
            "schema": payload["schema"],
            "generated_at": payload["generated_at"],
            "artifacts": payload["artifacts"],
            "configuration": configuration,
            "runtimes": metadata,
        }
        payload["artifact_id"] = (
            "spc-v09-native-boundary-"
            + _canonical_sha256(artifact_seed)[:16]
        )
        return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare frozen v0.8 and candidate v0.9 native wheels with fresh, "
            "paired processes and optional exact 20-game pool throughput."
        )
    )
    wheel_group = parser.add_argument_group("isolated wheel runtimes")
    wheel_group.add_argument("--baseline-wheel", type=Path)
    wheel_group.add_argument("--candidate-wheel", type=Path)
    wheel_group.add_argument("--venv-python", type=Path, default=Path(sys.executable))
    wheel_group.add_argument("--wheelhouse", type=Path, action="append", default=[])
    wheel_group.add_argument("--no-index", action="store_true")

    direct_group = parser.add_argument_group("existing runtimes (smoke checks)")
    direct_group.add_argument("--baseline-python", type=Path)
    direct_group.add_argument("--candidate-python", type=Path)
    direct_group.add_argument("--baseline-native-package", type=Path)
    direct_group.add_argument("--candidate-native-package", type=Path)

    parser.add_argument("--case", choices=tuple(CASES), action="append")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--branch-cap", type=int, default=32)
    parser.add_argument("--max-work", type=int, default=250_000)
    parser.add_argument("--include-pool", action="store_true")
    parser.add_argument("--pool-samples", type=int, default=1)
    parser.add_argument("--pool-workers", type=int, default=16)
    parser.add_argument("--worker-timeout-seconds", type=float, default=3600.0)
    parser.add_argument("--output", type=Path)

    parser.add_argument("--worker-search", choices=tuple(CASES), help=argparse.SUPPRESS)
    parser.add_argument("--worker-pool", type=int, help=argparse.SUPPRESS)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.samples < 1:
        raise SystemExit("--samples must be positive")
    if args.pool_samples < 1:
        raise SystemExit("--pool-samples must be positive")
    if args.pool_workers < 1:
        raise SystemExit("--pool-workers must be positive")
    if args.depth < 1:
        raise SystemExit("--depth must be positive")
    if args.branch_cap < 1:
        raise SystemExit("--branch-cap must be positive")
    if args.max_work < 1:
        raise SystemExit("--max-work must be positive")
    if args.worker_timeout_seconds <= 0:
        raise SystemExit("--worker-timeout-seconds must be positive")
    if args.case is None:
        args.case = list(CASES)

    wheel_mode = args.baseline_wheel is not None or args.candidate_wheel is not None
    direct_mode = args.baseline_python is not None or args.candidate_python is not None
    if wheel_mode == direct_mode:
        raise SystemExit(
            "supply exactly one complete pair: --baseline-wheel/--candidate-wheel "
            "or --baseline-python/--candidate-python"
        )
    if wheel_mode:
        if args.baseline_wheel is None or args.candidate_wheel is None:
            raise SystemExit("both wheel paths are required")
        for wheel in (args.baseline_wheel, args.candidate_wheel):
            if not wheel.is_file():
                raise SystemExit(f"wheel does not exist: {wheel}")
        if not args.venv_python.is_file():
            raise SystemExit("--venv-python does not exist")
        if args.baseline_native_package or args.candidate_native_package:
            raise SystemExit("native package overrides are only valid in direct mode")
        for wheelhouse in args.wheelhouse:
            if not wheelhouse.is_dir():
                raise SystemExit(f"wheelhouse does not exist: {wheelhouse}")
    else:
        if args.baseline_python is None or args.candidate_python is None:
            raise SystemExit("both interpreter paths are required")
        for python in (args.baseline_python, args.candidate_python):
            if not python.is_file():
                raise SystemExit(f"interpreter does not exist: {python}")
        for package in (
            args.baseline_native_package,
            args.candidate_native_package,
        ):
            if package is not None and not package.is_dir():
                raise SystemExit(f"native package directory does not exist: {package}")


def main() -> int:
    args = _parser().parse_args()
    if args.worker_search is not None:
        return _search_worker(
            args.worker_search,
            args.depth,
            args.branch_cap,
            args.max_work,
        )
    if args.worker_pool is not None:
        if args.worker_pool < 1:
            raise SystemExit("worker count must be positive")
        return _pool_worker(args.worker_pool)

    _validate_args(args)
    payload = _run(args)
    _assert_no_absolute_paths(payload)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
