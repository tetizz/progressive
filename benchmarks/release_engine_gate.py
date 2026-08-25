from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import time
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urljoin, urlparse


ROOT = Path(__file__).resolve().parents[1]
CHECKOUT_SRC = (ROOT / "src").resolve()
CHECKOUT_PACKAGE = CHECKOUT_SRC / "scottish_progressive"
NATIVE_BUILD_ROOT_ENV = "SPC_BENCHMARK_NATIVE_BUILD_ROOT"

# The benchmark may run through a shared virtual environment with an editable
# install pointing at another worktree. Put this checkout ahead of site-packages
# before any engine module can be imported.
sys.path.insert(0, str(CHECKOUT_SRC))

DEFAULT_BROWSER_MANIFEST = (
    ROOT
    / "src"
    / "scottish_progressive"
    / "web"
    / "static"
    / "engine"
    / "browser-engine-manifest.json"
)
ZERO_PROMOTED = "0000000000000000"
BASELINE_WEIGHTS = {
    "material": 100,
    "king_space": 100,
    "series_reach": 100,
    "promotion_corridors": 100,
    "immediate_vulnerability": 100,
    "useful_mobility": 100,
    "boundary_check": 100,
}


class GateError(RuntimeError):
    """The evidence does not satisfy the release benchmark contract."""


@dataclass(frozen=True, slots=True)
class Scenario:
    fen: str
    series: int
    quiet_series: int = 0
    ep_targets: tuple[str, ...] = ()
    promoted_hex: str = ZERO_PROMOTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "fen": self.fen,
            "series": self.series,
            "quiet_series": self.quiet_series,
            "ep_targets": list(self.ep_targets),
            "promoted_hex": self.promoted_hex,
        }


@dataclass(frozen=True, slots=True)
class Budget:
    seconds: float
    max_work: int = 4_000_000_000
    depth: int = 5
    width: int = 32

    def as_dict(self) -> dict[str, Any]:
        return {
            "seconds": self.seconds,
            "max_work": self.max_work,
            "requested_depth": self.depth,
            "retained_series_width": self.width,
        }


SCENARIOS = {
    "initial": Scenario(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        1,
    ),
    "black-after-e4": Scenario(
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        2,
    ),
}
BUDGETS = {
    "faster": Budget(seconds=5.0),
    "strong": Budget(seconds=30.0),
}
REQUIRED_BROWSER_GATES = (
    "exact_artifact_identity_all_workers",
    "ordinary_module_workers",
    "pthreads_disabled",
    "combined_prefix_root_mate_abi",
    "persistent_d1_through_d5_sessions",
    "exact_manifest_import_all_workers",
    "canonical_root_tactical_policy",
    "canonical_root_tactical_boundary_echoes",
    "global_work_cap_enforced",
    "common_monotonic_deadline",
    "dynamic_work_pool_certified",
    "final_bound_coverage",
    "selected_owner_warm_exact_certification",
    "compiled_root_prefix_replay",
    "compiled_reply_mate_safety",
    "memory_envelope_observed",
)


def build_browser_probe_plan(
    *,
    origin: str,
    module_url: str,
    wasm_url: str,
    build_receipt_url: str,
    workers: int = 8,
) -> dict[str, Any]:
    """Builds the exact four real Opera probe URLs without running a browser."""

    parsed = urlparse(origin)
    _require(parsed.scheme in {"http", "https"} and bool(parsed.netloc), "origin must be an HTTP(S) URL")
    _require(isinstance(workers, int) and workers >= 1, "workers must be positive")
    base = origin.rstrip("/") + "/"
    module = urljoin(base, module_url)
    wasm = urljoin(base, wasm_url)
    receipt = urljoin(base, build_receipt_url)
    cases: list[dict[str, Any]] = []
    for scenario, boundary in SCENARIOS.items():
        for mode, budget in BUDGETS.items():
            query = urlencode(
                {
                    "module": module,
                    "wasm": wasm,
                    "receipt": receipt,
                    "depth": budget.depth,
                    "width": budget.width,
                    "workers": workers,
                    "wave": workers,
                    "mode": "warm",
                    "max_work": budget.max_work,
                    "safety_work": 1_000_000,
                    "timeout_ms": int(budget.seconds * 1_000),
                    "fen": boundary.fen,
                    "series": boundary.series,
                    "quiet_series": boundary.quiet_series,
                    "ep_targets": ",".join(boundary.ep_targets),
                    "promoted_hex": boundary.promoted_hex,
                }
            )
            probe_url = urljoin(base, "benchmarks/opera_root_d5_probe.html") + "?" + query
            output = f"build/release-gate/browser-{scenario}-{mode}.json"
            cases.append(
                {
                    "scenario": scenario,
                    "mode": mode,
                    "budget": budget.as_dict(),
                    "probe_url": probe_url,
                    "capture_output": output,
                    "capture_command": (
                        "node benchmarks/capture_opera_root_session_probe.mjs "
                        f"--endpoint http://127.0.0.1:9222 --url '{probe_url}' "
                        f"--output '{output}' --timeout-ms 120000"
                    ),
                }
            )
    return {
        "schema": "spc-browser-d5-probe-plan-v1",
        "origin": origin.rstrip("/"),
        "workers": workers,
        "cases": cases,
        "execution_note": (
            "Serve this checkout, launch Opera with remote debugging on 9222, "
            "then run each capture_command during a quiet measurement window."
        ),
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise GateError(message)


def _object(value: Any, label: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{label} must be an object")
    return value


def _finite_positive(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise GateError(f"{label} must be a number") from error
    _require(math.isfinite(number) and number > 0.0, f"{label} must be positive")
    return number


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _checkout_module_path(module: Any, label: str) -> Path:
    raw_path = getattr(module, "__file__", None)
    _require(isinstance(raw_path, str) and bool(raw_path), f"{label} module has no file path")
    path = Path(raw_path).resolve()
    _require(path.is_file(), f"{label} module file is missing: {path}")
    _require(
        _path_is_within(path, CHECKOUT_PACKAGE),
        f"{label} module is outside the benchmark checkout: {path}",
    )
    return path


def _native_extension_roots() -> tuple[Path, ...]:
    roots = [CHECKOUT_SRC]
    raw_build_root = os.environ.get(NATIVE_BUILD_ROOT_ENV)
    if raw_build_root:
        build_root = Path(raw_build_root).resolve()
        _require(build_root.is_dir(), f"{NATIVE_BUILD_ROOT_ENV} is not a directory")
        _require(
            _path_is_within(build_root, ROOT),
            f"{NATIVE_BUILD_ROOT_ENV} must name a checkout-local build path",
        )
        roots.append(build_root)
    return tuple(roots)


def native_runtime_identity() -> dict[str, Any]:
    """Returns a source-matched CPython engine identity or fails closed."""

    import scottish_progressive as package
    from scottish_progressive import evaluation, model

    package_path = _checkout_module_path(package, "package")
    evaluation_path = _checkout_module_path(evaluation, "evaluation")
    model_path = _checkout_module_path(model, "model")
    package_search_paths = tuple(Path(value).resolve() for value in package.__path__)
    _require(
        bool(package_search_paths)
        and all(_path_is_within(value, CHECKOUT_PACKAGE) for value in package_search_paths),
        f"package search path is outside the benchmark checkout: {package_search_paths}",
    )

    native = evaluation._native_eval  # noqa: SLF001 - provenance gate
    expected = evaluation._native_source_identity()  # noqa: SLF001
    _require(expected is not None, "native source identity cannot be computed")
    _require(native is not None, "source-matched native engine is required; Python fallback is forbidden")
    actual = getattr(native, "SOURCE_IDENTITY", None)
    _require(actual == expected, "compiled native engine does not match packaged sources")
    module_path = Path(str(getattr(native, "__file__", ""))).resolve()
    _require(module_path.is_file(), "native engine module file is missing")
    _require(module_path.suffix.lower() in {".pyd", ".so"}, "native backend is not a compiled extension")
    extension_roots = _native_extension_roots()
    _require(
        any(_path_is_within(module_path, root) for root in extension_roots),
        f"compiled extension is outside the benchmark checkout: {module_path}",
    )
    return {
        "backend": "native-cpython",
        "engine_version": model.ENGINE_VERSION,
        "ruleset_version": model.RULESET_VERSION,
        "source_fingerprint": model.ENGINE_SOURCE_FINGERPRINT,
        "source_identity": actual,
        "expected_source_identity": expected,
        "checkout_root": str(ROOT),
        "checkout_src": str(CHECKOUT_SRC),
        "package_file": str(package_path),
        "package_search_paths": [str(value) for value in package_search_paths],
        "evaluation_file": str(evaluation_path),
        "model_file": str(model_path),
        "module_filename": module_path.name,
        "module_path": str(module_path),
        "module_sha256": _file_sha256(module_path),
        "allowed_extension_roots": [str(value) for value in extension_roots],
    }


def scenario_state(name: str):
    """Builds a named benchmark boundary through authoritative rules replay."""

    _require(name in SCENARIOS, f"unknown scenario {name!r}")
    from scottish_progressive.model import ProgressiveState
    from scottish_progressive.rules import play_series

    if name == "initial":
        state = ProgressiveState.initial()
    elif name == "black-after-e4":
        state = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
    else:  # pragma: no cover - guarded above and explicit for future scenarios
        raise AssertionError(name)
    expected = SCENARIOS[name]
    _require(state.board.fen() == expected.fen, f"{name} FEN replay drifted")
    _require(state.series_number == expected.series, f"{name} series replay drifted")
    _require(state.quiet_series == expected.quiet_series, f"{name} quiet-series replay drifted")
    _require(
        tuple(sorted(state.ep_targets))
        == tuple(sorted(__import__("chess").parse_square(value) for value in expected.ep_targets)),
        f"{name} en-passant replay drifted",
    )
    return state


def _git_identity() -> dict[str, Any]:
    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    try:
        commit = git("rev-parse", "HEAD")
        branch = git("branch", "--show-current") or None
        status = git("status", "--porcelain=v1", "--untracked-files=all")
    except (OSError, subprocess.CalledProcessError) as error:
        raise GateError(f"cannot establish git benchmark identity: {error}") from error
    return {
        "commit": commit,
        "branch": branch,
        "dirty": bool(status),
        "dirty_paths": status.splitlines(),
    }


def _stop_reason(*, timed_out: bool, work_limit_reached: bool, completed_depth: int, requested_depth: int) -> str | None:
    if timed_out:
        return "deadline"
    if work_limit_reached:
        return "work-limit"
    if completed_depth < requested_depth:
        return "incomplete-without-reported-limit"
    return None


def run_native_case(
    *,
    scenario: str,
    mode: str,
    measurement_quality: str = "contended-functional-only",
) -> dict[str, Any]:
    """Runs one real fixed-budget native search and returns measured evidence."""

    _require(scenario in SCENARIOS, f"unknown scenario {scenario!r}")
    _require(mode in BUDGETS, f"unknown mode {mode!r}")
    _require(
        measurement_quality in {"contended-functional-only", "quiet-controlled"},
        "measurement_quality must name the contention state",
    )
    artifact = native_runtime_identity()
    budget = BUDGETS[mode]
    state = scenario_state(scenario)

    from dataclasses import asdict
    from scottish_progressive.profiles import baseline_profile
    from scottish_progressive.rules import play_series
    from scottish_progressive.search import SearchLimits, analyze

    profile = baseline_profile()
    limits = SearchLimits(
        depth_series=budget.depth,
        max_series_per_node=budget.width,
        time_limit_seconds=budget.seconds,
        max_generation_positions=budget.max_work,
        collect_all_root_scores=False,
        native_threads=1,
    )
    started = time.perf_counter()
    result = analyze(state, limits, profile)
    wall_seconds = time.perf_counter() - started
    _require(math.isfinite(wall_seconds) and wall_seconds > 0.0, "native wall time is invalid")
    _require(result.requested_depth == budget.depth, "native requested depth drifted")
    _require(result.max_series_per_node == budget.width, "native retained width drifted")
    _require(result.max_generation_positions == budget.max_work, "native max_work drifted")
    _require(result.time_limit_seconds == budget.seconds, "native deadline budget drifted")
    _require(result.engine_profile_id == profile.profile_id, "native search did not use the baseline profile")
    _require(result.source_fingerprint == artifact["source_fingerprint"], "native result source identity drifted")
    _require(result.best_series is not None, "native search produced no playable series")
    replay = play_series(state, result.best_series.moves)
    _require(
        replay.final_state.position_hash == result.best_series.final_state.position_hash,
        "native selected series failed authoritative replay",
    )
    _require(
        0 <= result.completed_depth <= result.requested_depth,
        "native completed depth is invalid",
    )
    stats = asdict(result.stats)
    nodes = int(result.stats.nodes)
    work_positions = int(result.stats.work_positions)
    _require(nodes >= 0, "native node count is invalid")
    _require(0 <= work_positions <= budget.max_work, "native work count is invalid")
    timeout_reason = _stop_reason(
        timed_out=result.timed_out,
        work_limit_reached=result.work_limit_reached,
        completed_depth=result.completed_depth,
        requested_depth=result.requested_depth,
    )
    return {
        "backend": "native-cpython",
        "scenario": scenario,
        "mode": mode,
        "budget": budget.as_dict(),
        "requested_depth": result.requested_depth,
        "completed_depth": result.completed_depth,
        "wall_time_seconds": wall_seconds,
        "engine_reported_elapsed_seconds": result.elapsed_seconds,
        "nodes": nodes,
        "work_positions": work_positions,
        "generated_unique_series": result.stats.generated_unique_series,
        "unique_retained_states": None,
        "peak_frontier_states": result.stats.peak_frontier_states,
        "nps": nodes / wall_seconds if nodes else None,
        "selected_series": result.best_series.machine_notation,
        "principal_variation": [item.machine_notation for item in result.principal_variation],
        "evaluation": {
            "score_white_heuristic_points": result.score,
            "classification": result.classification,
            "proof": result.proof,
            "forced": result.forced,
            "exact_width": result.exact_width,
            "root_scores_complete": result.root_scores_complete,
        },
        "timed_out": result.timed_out,
        "work_limit_reached": result.work_limit_reached,
        "timeout_reason": timeout_reason,
        "artifact": artifact,
        "git": _git_identity(),
        "environment": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "processor": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER"),
            "logical_cpu_count": os.cpu_count(),
            "native_threads": 1,
        },
        "measurement_quality": measurement_quality,
        "stats": stats,
        "measurement_note": (
            "one fresh real native search; unique_retained_states is null because "
            "the native SearchResult contract exposes generated unique series and "
            "peak frontier states, not a retained-state cardinality"
        ),
    }


def _native_worker(scenario: str, mode: str, measurement_quality: str) -> int:
    print(
        json.dumps(
            run_native_case(
                scenario=scenario,
                mode=mode,
                measurement_quality=measurement_quality,
            ),
            sort_keys=True,
        )
    )
    return 0


def run_native_suite(
    *,
    samples: int,
    scenarios: tuple[str, ...] = tuple(SCENARIOS),
    modes: tuple[str, ...] = tuple(BUDGETS),
    measurement_quality: str = "contended-functional-only",
) -> dict[str, Any]:
    """Runs every sample in a fresh process and summarizes without hiding raw data."""

    _require(samples >= 1, "samples must be positive")
    _require(all(name in SCENARIOS for name in scenarios), "unknown native suite scenario")
    _require(all(name in BUDGETS for name in modes), "unknown native suite mode")
    all_samples: dict[tuple[str, str], list[dict[str, Any]]] = {
        (scenario, mode): [] for scenario in scenarios for mode in modes
    }
    ordered = tuple(all_samples)
    for repetition in range(samples):
        order = ordered if repetition % 2 == 0 else tuple(reversed(ordered))
        for scenario, mode in order:
            worker_environment = os.environ.copy()
            inherited_python_path = worker_environment.get("PYTHONPATH")
            worker_environment["PYTHONPATH"] = os.pathsep.join(
                value
                for value in (str(CHECKOUT_SRC), inherited_python_path)
                if value
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "_native-worker",
                    "--scenario",
                    scenario,
                    "--mode",
                    mode,
                    "--measurement-quality",
                    measurement_quality,
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=ROOT,
                env=worker_environment,
            )
            sample = json.loads(completed.stdout)
            sample["repetition"] = repetition
            all_samples[(scenario, mode)].append(sample)

    cases: list[dict[str, Any]] = []
    identities: set[str] = set()
    commits: set[str] = set()
    for (scenario, mode), values in all_samples.items():
        identities.update(value["artifact"]["module_sha256"] for value in values)
        commits.update(value["git"]["commit"] for value in values)
        wall = [float(value["wall_time_seconds"]) for value in values]
        nps_values = [float(value["nps"]) for value in values if value["nps"] is not None]
        cases.append(
            {
                "scenario": scenario,
                "mode": mode,
                "backend": "native-cpython",
                "budget": BUDGETS[mode].as_dict(),
                "samples": values,
                "summary": {
                    "sample_count": len(values),
                    "median_wall_time_seconds": statistics.median(wall),
                    "minimum_wall_time_seconds": min(wall),
                    "maximum_wall_time_seconds": max(wall),
                    "median_nps": statistics.median(nps_values) if nps_values else None,
                    "completed_depths": [value["completed_depth"] for value in values],
                    "all_completed_depth_5": all(value["completed_depth"] == 5 for value in values),
                    "timeout_reasons": [value["timeout_reason"] for value in values],
                },
            }
        )
    _require(len(identities) == 1, "native module identity drifted between samples")
    _require(len(commits) == 1, "git commit drifted between samples")
    return {
        "schema": "spc-release-engine-search-gate-v1",
        "status": "measured",
        "measurement_quality": measurement_quality,
        "reportable_performance": measurement_quality == "quiet-controlled",
        "samples_per_case": samples,
        "sampling_order": "paired-interleaved-alternating-fresh-process",
        "cases": cases,
        "claim_scope": {
            "real_engine": True,
            "scripted_moves": False,
            "debug_build": None,
            "binary_compile_flags_attested": False,
            "depth_unit": "complete progressive series",
            "performance_numbers_publishable": measurement_quality == "quiet-controlled",
        },
    }


def evaluate_stockfish_progress(
    suite: Mapping[str, Any],
    *,
    target_seconds: float = 10.0,
) -> dict[str, Any]:
    """Reports the sub-10 D5 milestone without turning it into a strength claim."""

    _require(math.isfinite(target_seconds) and target_seconds > 0.0, "target_seconds must be positive")
    payload = _object(suite, "native search suite")
    _require(payload.get("schema") == "spc-release-engine-search-gate-v1", "native search suite schema drifted")
    _require(
        payload.get("measurement_quality") == "quiet-controlled"
        and payload.get("reportable_performance") is True,
        "stockfish-progress timings must come from a quiet-controlled reportable run",
    )
    raw_cases = payload.get("cases")
    _require(isinstance(raw_cases, list), "native search suite cases are missing")
    required = set(SCENARIOS)
    keyed: dict[str, Mapping[str, Any]] = {}
    for raw in raw_cases:
        case = _object(raw, "native search case")
        if case.get("mode") != "strong":
            continue
        scenario = str(case.get("scenario"))
        _require(scenario in required, f"unexpected Strong scenario {scenario!r}")
        _require(scenario not in keyed, f"duplicate Strong scenario {scenario!r}")
        _require(case.get("backend") == "native-cpython", f"{scenario} is not native-cpython evidence")
        _require(case.get("budget") == BUDGETS["strong"].as_dict(), f"{scenario} Strong budget drifted")
        keyed[scenario] = case
    _require(set(keyed) == required, "stockfish-progress evidence is missing a required Strong scenario")

    decisions: list[dict[str, Any]] = []
    engine_identities: set[tuple[str, str]] = set()
    git_commits: set[str] = set()
    for scenario in SCENARIOS:
        case = keyed[scenario]
        samples = case.get("samples")
        _require(isinstance(samples, list) and len(samples) >= 3, f"{scenario} needs at least three quiet samples")
        wall_times: list[float] = []
        signatures: set[tuple[Any, ...]] = set()
        clean = True
        for raw_sample in samples:
            sample = _object(raw_sample, f"{scenario} sample")
            _require(sample.get("backend") == "native-cpython", f"{scenario} sample is not native-cpython")
            _require(sample.get("scenario") == scenario, f"{scenario} sample boundary drifted")
            _require(sample.get("mode") == "strong", f"{scenario} sample mode drifted")
            _require(sample.get("budget") == BUDGETS["strong"].as_dict(), f"{scenario} sample budget drifted")
            _require(sample.get("requested_depth") == 5, f"{scenario} sample did not request D5")
            _require(
                sample.get("measurement_quality") == "quiet-controlled",
                f"{scenario} sample is not quiet-controlled",
            )
            wall_times.append(_finite_positive(sample.get("wall_time_seconds"), f"{scenario} wall time"))
            principal_variation = sample.get("principal_variation")
            _require(isinstance(principal_variation, list) and principal_variation, f"{scenario} PV is missing")
            selected_series = sample.get("selected_series")
            _require(
                isinstance(selected_series, str)
                and bool(selected_series)
                and principal_variation[0] == selected_series,
                f"{scenario} selected series and PV differ",
            )
            signatures.add((selected_series, tuple(principal_variation)))
            artifact = _object(sample.get("artifact"), f"{scenario} artifact")
            module_sha256 = artifact.get("module_sha256")
            source_identity = artifact.get("source_identity")
            _require(
                isinstance(module_sha256, str)
                and re.fullmatch(r"[0-9a-f]{64}", module_sha256) is not None
                and isinstance(source_identity, str)
                and re.fullmatch(r"[0-9a-f]{64}", source_identity) is not None,
                f"{scenario} engine identity is invalid",
            )
            engine_identities.add((module_sha256, source_identity))
            git = _object(sample.get("git"), f"{scenario} git identity")
            commit = git.get("commit")
            _require(
                isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) is not None,
                f"{scenario} git commit is invalid",
            )
            _require(git.get("dirty") is False, f"{scenario} quiet timing came from a dirty checkout")
            git_commits.add(commit)
            clean = clean and (
                sample.get("completed_depth") == 5
                and sample.get("timed_out") is False
                and sample.get("work_limit_reached") is False
                and sample.get("timeout_reason") is None
            )
        median_seconds = statistics.median(wall_times)
        stable = len(signatures) == 1
        decisions.append(
            {
                "scenario": scenario,
                "backend": case.get("backend"),
                "sample_count": len(samples),
                "median_d5_wall_time_seconds": median_seconds,
                "completed_without_timeout_or_work_limit": clean,
                "stable_selected_series_and_pv": stable,
                "median_within_target": median_seconds <= target_seconds,
                "passed": clean and stable and median_seconds <= target_seconds,
            }
        )
    _require(len(engine_identities) == 1, "stockfish-progress engine identity drifted between samples")
    _require(len(git_commits) == 1, "stockfish-progress git identity drifted between samples")
    achieved = all(case["passed"] for case in decisions)
    return {
        "schema": "spc-stockfish-progress-verdict-v1",
        "passed": achieved,
        "sub10_d5_achieved": achieved and target_seconds == 10.0,
        "target_seconds": target_seconds,
        "required_scenarios": list(SCENARIOS),
        "cases": decisions,
        "stockfish_level_achieved": False,
        "strength_claim": False,
        "scope_warning": (
            "This verdict measures the sub-10-second D5 speed milestone only. "
            "It is separate from Faster/Strong product budgets and cannot establish "
            "Stockfish-level playing strength."
        ),
    }


def run_tactical_gate(candidate: str | Path) -> dict[str, Any]:
    """Requires real search to find a mate on every published tactical anchor."""

    artifact = native_runtime_identity()
    from scottish_progressive.league import PUBLISHED_RULE_ANCHORS
    from scottish_progressive.model import Outcome, ProgressiveState
    from scottish_progressive.rules import play_series
    from scottish_progressive.search import SearchLimits, analyze
    from scottish_progressive.strength import resolve_match_profile

    profile = resolve_match_profile(candidate)
    anchors: list[dict[str, Any]] = []
    for index, (fen, series_number, published_moves) in enumerate(PUBLISHED_RULE_ANCHORS):
        state = ProgressiveState.from_fen(fen, series_number)
        fixture = play_series(state, published_moves)
        _require(fixture.outcome == Outcome.CHECKMATE, f"tactical fixture {index} is no longer mate")
        result = analyze(
            state,
            SearchLimits(
                depth_series=2,
                max_series_per_node=32,
                max_generation_positions=250_000,
                collect_all_root_scores=False,
            ),
            profile,
        )
        _require(result.best_series is not None, f"tactical anchor {index} returned no move")
        selected = play_series(state, result.best_series.moves)
        _require(selected.outcome == Outcome.CHECKMATE, f"tactical anchor {index} missed checkmate")
        _require(result.completed_depth == 2, f"tactical anchor {index} did not complete depth 2")
        _require(not result.timed_out, f"tactical anchor {index} timed out")
        _require(not result.work_limit_reached, f"tactical anchor {index} exhausted work")
        anchors.append(
            {
                "anchor_index": index,
                "fen": fen,
                "series_number": series_number,
                "published_fixture_series": "/".join(published_moves),
                "selected_series": result.best_series.machine_notation,
                "selected_outcome": selected.outcome.value,
                "score_white_heuristic_points": result.score,
                "proof": result.proof,
                "completed_depth": result.completed_depth,
                "nodes": result.stats.nodes,
                "work_positions": result.stats.work_positions,
            }
        )
    return {
        "schema": "spc-release-tactical-anchor-gate-v1",
        "status": "passed",
        "scripted_moves": False,
        "candidate": profile.as_dict(),
        "limits": {
            "depth_series": 2,
            "retained_series_width": 32,
            "max_work_positions": 250_000,
            "time_limit_seconds": None,
        },
        "artifact": artifact,
        "git": _git_identity(),
        "anchors": anchors,
        "claim_scope": (
            "Published moves validate each fixture only; the selected series is "
            "produced independently by the real engine search."
        ),
    }


def evaluate_strength_report(
    report: Mapping[str, Any],
    baseline_certificate: Mapping[str, Any],
    *,
    minimum_score_rate: float,
    candidate_overlay_present: bool = False,
) -> dict[str, Any]:
    """Evaluates equal-budget fixed-suite evidence without overstating its scope."""

    _require(
        math.isfinite(minimum_score_rate) and 0.0 <= minimum_score_rate <= 1.0,
        "minimum_score_rate must be between zero and one",
    )
    payload = _object(report, "strength report")
    _require(payload.get("format") == "spc-fixed-suite-strength-v1", "strength report format drifted")
    candidate = _object(payload.get("candidate"), "strength candidate")
    reference = _object(payload.get("reference"), "strength reference")
    _require(
        reference.get("profile_id") == baseline_certificate.get("profile_id"),
        "strength reference is not the certified baseline profile",
    )
    config = _object(payload.get("config"), "strength config")
    limits = _object(config.get("deterministic_limits"), "strength deterministic limits")
    _require(limits.get("same_for_both_profiles") is True, "strength budgets are not equal")
    _require(limits.get("time_limit_seconds") is None, "strength match must not use unequal wall timing")
    for field in (
        "depth_series",
        "branch_cap_complete_series_per_node",
        "max_work_positions_per_search",
        "max_game_work_positions",
    ):
        value = limits.get(field)
        _require(isinstance(value, int) and value > 0, f"strength {field} is invalid")
    summary = _object(payload.get("summary"), "strength summary")
    scheduled_games = summary.get("scheduled_games")
    completed_games = summary.get("completed_games")
    scheduled_pairs = summary.get("scheduled_pairs")
    completed_pairs = summary.get("completed_pairs")
    _require(
        all(isinstance(value, int) and value >= 0 for value in (scheduled_games, completed_games, scheduled_pairs, completed_pairs)),
        "strength completion counts are invalid",
    )
    technical = _object(summary.get("technical_failures"), "strength technical failures")
    no_failures = all(
        technical.get(field) == 0
        for field in (
            "total_profile_failures",
            "unattributed_worker_failures",
            "unattributed_match_limit_failures",
        )
    )
    complete = (
        scheduled_games > 0
        and completed_games == scheduled_games
        and summary.get("incomplete_games") == 0
        and scheduled_pairs > 0
        and completed_pairs == scheduled_pairs
        and summary.get("incomplete_pairs") == 0
    )
    game_rate = summary.get("candidate_game_score_rate")
    pair_rate = summary.get("candidate_pair_score_rate")
    _require(
        isinstance(game_rate, (int, float)) and math.isfinite(game_rate) and 0.0 <= game_rate <= 1.0,
        "candidate game score rate is invalid",
    )
    _require(
        isinstance(pair_rate, (int, float)) and math.isfinite(pair_rate) and 0.0 <= pair_rate <= 1.0,
        "candidate pair score rate is invalid",
    )
    not_worse = game_rate >= minimum_score_rate and pair_rate >= minimum_score_rate
    return {
        "passed": no_failures and complete and not_worse,
        "equal_budget": True,
        "complete_without_technical_failures": no_failures and complete,
        "candidate_not_worse_on_fixed_suite": not_worse,
        "minimum_score_rate": minimum_score_rate,
        "candidate_game_score_rate": game_rate,
        "candidate_pair_score_rate": pair_rate,
        "certified_baseline_profile_id": baseline_certificate.get("profile_id"),
        "certified_baseline_certificate_id": baseline_certificate.get("certificate_id"),
        "sanity_only": (
            candidate.get("profile_id") == baseline_certificate.get("profile_id")
            and not candidate_overlay_present
        ),
        "binary_baseline_participant": False,
        "scope_warning": (
            "The certificate binds the reference profile and browser baseline identity, "
            "but both match seats execute the current candidate search core. This is an "
            "equal-budget evaluator/profile gate, not a candidate-binary versus old-WASM game match."
        ),
    }


def run_strength_gate(
    candidate_reference: str | Path,
    *,
    candidate_value_model: str | Path | None = None,
    pairs: int = 10,
    seed: int = 20260820,
    depth: int = 2,
    width: int = 32,
    max_search_work: int = 250_000,
    max_game_work: int = 5_000_000,
    workers: int | None = None,
    minimum_score_rate: float = 0.5,
    manifest_path: str | Path = DEFAULT_BROWSER_MANIFEST,
) -> dict[str, Any]:
    """Runs the existing color-swapped match and binds its reference to certification."""

    artifact = native_runtime_identity()
    baseline = certified_baseline(manifest_path)
    from scottish_progressive.deep_teacher_overlay import load_deep_teacher_overlay_payload
    from scottish_progressive.profiles import baseline_profile
    from scottish_progressive.strength import (
        StrengthMatchConfig,
        resolve_match_profile,
        run_strength_match,
    )

    candidate = resolve_match_profile(candidate_reference)
    reference = baseline_profile()
    _require(reference.profile_id == baseline["profile_id"], "built-in reference lost certified identity")
    overlay = (
        None
        if candidate_value_model is None
        else load_deep_teacher_overlay_payload(candidate_value_model, candidate)
    )
    config = StrengthMatchConfig(
        pairs=pairs,
        seed=seed,
        search_depth=depth,
        max_series_per_node=width,
        max_generation_positions=max_search_work,
        max_game_work_positions=max_game_work,
    )
    report = run_strength_match(
        candidate,
        reference,
        config=config,
        requested_workers=workers,
        candidate_value_model=overlay,
    )
    decision = evaluate_strength_report(
        report,
        baseline,
        minimum_score_rate=minimum_score_rate,
        candidate_overlay_present=overlay is not None,
    )
    return {
        "schema": "spc-certified-baseline-strength-gate-v1",
        "status": "passed" if decision["passed"] else "failed",
        "artifact": artifact,
        "git": _git_identity(),
        "certified_baseline": baseline,
        "decision": decision,
        "match": report,
    }


def certified_baseline(manifest_path: str | Path = DEFAULT_BROWSER_MANIFEST) -> dict[str, Any]:
    """Returns the checked-in browser baseline only after byte/certificate binding."""

    path = Path(manifest_path).resolve()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"cannot load browser manifest: {error}") from error
    root = _object(manifest, "browser manifest")
    _require(root.get("schema") == "spc-browser-wasm-manifest-v1", "browser manifest schema drifted")
    variants = _object(root.get("variants"), "browser variants")
    variant = _object(variants.get("single"), "single browser variant")
    certificate = _object(
        variant.get("root_session_certificate"),
        "root-session certificate",
    )
    _require(
        certificate.get("schema") == "spc-root-session-certificate-v1",
        "root-session certificate schema drifted",
    )
    _require(certificate.get("status") == "certified", "root-session certificate is not certified")
    _require(certificate.get("root_session_certified") is True, "root session is not certified")
    _require(certificate.get("runtime_variant") == "single", "baseline is not the single-worker variant")
    _require(certificate.get("thread_count") == 1, "baseline certificate thread count drifted")
    engine = _object(certificate.get("engine"), "certificate engine")
    source_fingerprint = certificate.get("source_fingerprint")
    _require(
        isinstance(source_fingerprint, str)
        and re.fullmatch(r"[0-9a-f]{16}", source_fingerprint) is not None,
        "certificate source_fingerprint is invalid",
    )
    _require(
        root.get("source_fingerprint") == source_fingerprint,
        "manifest and certificate source_fingerprint differ",
    )
    from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT

    _require(
        source_fingerprint == ENGINE_SOURCE_FINGERPRINT,
        "stale browser assets: certified source_fingerprint "
        f"{source_fingerprint!r} does not match current engine "
        f"{ENGINE_SOURCE_FINGERPRINT!r}; rebuild and recertify the browser engine",
    )
    for field in ("wasm_sha256", "module_js_sha256", "kernel_sha256"):
        digest = certificate.get(field)
        _require(
            isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
            f"certificate {field} is invalid",
        )
        _require(
            variant.get(field) == digest,
            f"manifest and certificate {field} differ",
        )
    artifact_root = path.parent / "single"
    wasm_path = artifact_root / str(variant.get("wasm", ""))
    module_path = artifact_root / str(variant.get("module_js", ""))
    _require(wasm_path.is_file(), "certified WASM file is missing")
    _require(module_path.is_file(), "certified module JS file is missing")
    _require(_file_sha256(wasm_path) == certificate.get("wasm_sha256"), "certified wasm_sha256 does not match bytes")
    _require(
        _file_sha256(module_path) == certificate.get("module_js_sha256"),
        "certified module_js_sha256 does not match bytes",
    )
    try:
        from scottish_progressive.profiles import baseline_profile

        expected_profile = baseline_profile().profile_id
    except ImportError as error:
        raise GateError("scottish_progressive must be importable to verify the baseline profile") from error
    _require(engine.get("profile_id") == expected_profile, "certificate profile is not the built-in baseline")
    for field in ("engine_version", "ruleset_version", "profile_id"):
        _require(isinstance(engine.get(field), str) and bool(engine[field]), f"certificate {field} is invalid")
    _require(
        isinstance(certificate.get("certificate_id"), str) and bool(certificate["certificate_id"]),
        "root-session certificate_id is invalid",
    )
    return {
        "certificate_id": certificate.get("certificate_id"),
        "status": certificate.get("status"),
        "engine_version": engine.get("engine_version"),
        "ruleset_version": engine.get("ruleset_version"),
        "profile_id": engine.get("profile_id"),
        "source_fingerprint": certificate.get("source_fingerprint"),
        "kernel_sha256": certificate.get("kernel_sha256"),
        "wasm_sha256": certificate.get("wasm_sha256"),
        "module_js_sha256": certificate.get("module_js_sha256"),
        "runtime_variant": certificate.get("runtime_variant"),
        "thread_count": certificate.get("thread_count"),
    }


def _query_boundary(location: Any) -> dict[str, Any] | None:
    if not isinstance(location, str) or not location:
        return None
    query = parse_qs(urlparse(location).query, keep_blank_values=True)
    if "fen" not in query:
        return None
    return {
        "fen": query["fen"][-1],
        "series": int(query.get("series", ["1"])[-1]),
        "quiet_series": int(query.get("quiet_series", ["0"])[-1]),
        "ep_targets": [value for value in query.get("ep_targets", [""])[-1].split(",") if value],
        "promoted_hex": query.get("promoted_hex", [ZERO_PROMOTED])[-1]
        .lower()
        .removeprefix("0x")
        .zfill(16),
    }


def _query_timeout_seconds(location: Any) -> float | None:
    if not isinstance(location, str) or not location:
        return None
    query = parse_qs(urlparse(location).query)
    if "timeout_ms" not in query:
        return None
    try:
        return int(query["timeout_ms"][-1]) / 1_000.0
    except (TypeError, ValueError):
        return None


def _browser_work_positions(
    value: Any,
    *,
    max_work: int,
    safety_reserve_work: int,
    label: str,
) -> int:
    work = _object(value, label)
    _require(work.get("max_work") == max_work, f"{label} max_work drifted")
    committed = work.get("committed_work")
    reserved = work.get("reserved_work")
    remaining = work.get("remaining_work")
    safety_committed = work.get("safety_committed_work")
    _require(type(committed) is int and committed >= 0, f"{label} committed_work is invalid")
    _require(type(reserved) is int and reserved >= 0, f"{label} reserved_work is invalid")
    _require(type(remaining) is int and remaining >= 0, f"{label} remaining_work is invalid")
    _require(
        type(safety_committed) is int and 0 <= safety_committed <= safety_reserve_work,
        f"{label} safety_committed_work is invalid",
    )
    _require(
        work.get("safety_reserve_work") == safety_reserve_work,
        f"{label} safety_reserve_work drifted",
    )
    _require(work.get("within_cap") is True, f"{label} did not remain within the work cap")
    _require(reserved == 0, f"{label} did not settle reserved work to zero")
    _require(committed + remaining == max_work, f"{label} did not settle exactly")
    expected_exact_at_cap = committed == max_work
    _require(
        type(work.get("exact_at_cap")) is bool
        and work.get("exact_at_cap") == expected_exact_at_cap,
        f"{label} exact_at_cap is inconsistent",
    )
    return committed


def _browser_principal_variation(value: Any, *, label: str) -> list[str]:
    _require(isinstance(value, list) and bool(value), f"{label} is missing")
    normalized: list[str] = []
    for index, raw in enumerate(value):
        item = _object(raw, f"{label} item {index}")
        moves = item.get("moves")
        notation = item.get("machine_notation")
        _require(
            isinstance(moves, list)
            and bool(moves)
            and all(isinstance(move, str) and bool(move) for move in moves),
            f"{label} item {index} moves are invalid",
        )
        _require(
            isinstance(notation, str) and notation == "/".join(moves),
            f"{label} item {index} machine_notation drifted",
        )
        normalized.append(notation)
    return normalized


def normalize_browser_receipt(
    receipt: Mapping[str, Any],
    *,
    scenario: str,
    mode: str,
    manifest_path: str | Path = DEFAULT_BROWSER_MANIFEST,
) -> dict[str, Any]:
    """Validates and normalizes a raw Opera/WASM benchmark receipt."""

    _require(scenario in SCENARIOS, f"unknown scenario {scenario!r}")
    _require(mode in BUDGETS, f"unknown mode {mode!r}")
    expected_boundary = SCENARIOS[scenario]
    budget = BUDGETS[mode]
    top = _object(receipt, "browser receipt")
    _require(
        top.get("schema") == "spc-opera-root-session-cdp-receipt-v1",
        "browser receipt must be the raw CDP v1 receipt; derived v2 receipts are not accepted",
    )
    _require(top.get("status") == "passed-not-certified", "browser receipt did not pass")
    _require(top.get("product_publishable") is False, "raw browser receipt claims publishability")
    _require(top.get("safety_certified") is False, "raw CDP wrapper safety status drifted")
    cdp = _object(top.get("cdp"), "CDP environment")
    page_environment = _object(top.get("page_environment"), "page environment")
    browser_name = cdp.get("browser")
    cdp_user_agent = cdp.get("user_agent")
    page_user_agent = page_environment.get("userAgent")
    _require(
        all(isinstance(value, str) and bool(value) for value in (browser_name, cdp_user_agent, page_user_agent)),
        "Opera browser identity is incomplete",
    )
    _require(
        str(browser_name).startswith("Opera/")
        or "OPR/" in str(cdp_user_agent)
        or "OPR/" in str(page_user_agent),
        "CDP receipt is not from Opera",
    )
    _require(cdp.get("web_socket_debugger_url_recorded") is True, "Opera CDP endpoint was not recorded")
    _require(
        isinstance(cdp.get("protocol_version"), str) and bool(cdp["protocol_version"]),
        "Opera CDP protocol version is missing",
    )
    worker = _object(top.get("worker_receipt"), "worker receipt")
    _require(worker.get("schema") == "spc-opera-root-d5-benchmark-v1", "worker receipt schema drifted")
    _require(worker.get("status") == "passed-not-certified", "browser worker did not pass")
    _require(worker.get("product_publishable") is False, "browser worker claims publishability")
    _require(worker.get("safety_certified") is True, "browser worker safety gate did not pass")

    baseline = certified_baseline(manifest_path)
    artifact = _object(worker.get("artifact"), "worker artifact")
    source_revision = artifact.get("source_revision")
    _require(
        isinstance(source_revision, str)
        and re.fullmatch(r"[0-9a-f]{40}", source_revision) is not None,
        "worker source_revision is not an exact lowercase git commit",
    )
    for field in ("source_fingerprint", "kernel_sha256", "wasm_sha256", "module_js_sha256"):
        _require(artifact.get(field) == baseline[field], f"worker {field} is not certificate-bound")
    artifact_set_sha256 = artifact.get("artifact_set_sha256")
    _require(
        isinstance(artifact_set_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", artifact_set_sha256) is not None,
        "worker artifact_set_sha256 is invalid",
    )

    geometry = _object(worker.get("geometry"), "worker geometry")
    workers = geometry.get("workers")
    initial_full_wave = geometry.get("initial_full_wave")
    _require(type(workers) is int and workers >= 1, "browser worker count is invalid")
    _require(
        type(initial_full_wave) is int and 1 <= initial_full_wave <= workers,
        "browser initial worker wave is invalid",
    )
    _require(geometry.get("depth") == budget.depth, "browser requested depth is not 5")
    _require(geometry.get("width") == budget.width, "browser retained width drifted")
    _require(geometry.get("max_work") == budget.max_work, "browser max_work does not match play mode")
    _require(geometry.get("mode") == "warm", "browser benchmark must use production-like warm iterative deepening")
    config = _object(geometry.get("config"), "worker config")
    _require(config.get("max_depth") == budget.depth, "browser config max_depth drifted")
    _require(config.get("width") == budget.width, "browser config width drifted")
    _require(config.get("max_work") == budget.max_work, "browser config max_work drifted")
    _require(config.get("weights") == BASELINE_WEIGHTS, "browser benchmark profile is not the certified baseline")
    safety_reserve_work = geometry.get("safety_reserve_work")
    _require(safety_reserve_work == 1_000_000, "browser safety reserve drifted")
    worker_environment = _object(worker.get("environment"), "browser worker environment")
    _require(
        worker_environment.get("ordinary_module_workers") is True,
        "browser run did not use ordinary module Workers",
    )
    _require(worker_environment.get("worker_count") == workers, "browser worker count echo drifted")
    gates = _object(worker.get("gates"), "browser worker gates")
    for gate in REQUIRED_BROWSER_GATES:
        _require(gates.get(gate) is True, f"browser worker gate {gate} did not pass")

    raw_boundary = worker.get("boundary")
    if raw_boundary is None:
        raw_boundary = _query_boundary(page_environment.get("location"))
    boundary = _object(raw_boundary, "browser boundary")
    _require(dict(boundary) == expected_boundary.as_dict(), "browser boundary does not match the named scenario")
    timeout_seconds = _query_timeout_seconds(page_environment.get("location"))
    _require(timeout_seconds == budget.seconds, "browser timeout does not match play mode")

    timings = _object(worker.get("timings_ms"), "worker timings")
    wall_seconds = _finite_positive(
        timings.get("total_to_completed_depth"),
        "browser total_to_completed_depth",
    ) / 1_000.0
    _finite_positive(timings.get("iterative_d1_through_d5"), "browser iterative D1-D5 timing")
    _finite_positive(timings.get("completed_depth_iteration"), "browser D5 iteration timing")
    result = _object(worker.get("result"), "worker result")
    completed_depth = result.get("completed_depth")
    _require(completed_depth == budget.depth, "browser worker did not actually complete requested D5")
    principal_variation = _browser_principal_variation(
        result.get("principal_variation"),
        label="browser principal variation",
    )
    work_positions = _browser_work_positions(
        result.get("work"),
        max_work=budget.max_work,
        safety_reserve_work=safety_reserve_work,
        label="browser final work ledger",
    )
    selected = result.get("move")
    _require(isinstance(selected, str) and bool(selected), "browser selected series is missing")
    _require(principal_variation and principal_variation[0] == selected, "browser PV does not start with the selected series")
    _require(result.get("coverage_complete") is True, "browser final bound coverage is incomplete")
    _require(result.get("root_scores_complete") is True, "browser final root scores are incomplete")
    _require(result.get("safety_status") in {"exhausted", "terminal"}, "browser final mate safety is incomplete")
    retained_manifest_sha256 = result.get("retained_manifest_sha256")
    _require(
        isinstance(retained_manifest_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", retained_manifest_sha256) is not None,
        "browser retained manifest digest is invalid",
    )
    proof_bounds = result.get("proof_bounds")
    _require(
        isinstance(proof_bounds, list)
        and len(proof_bounds) == 2
        and all(type(bound) is int for bound in proof_bounds),
        "browser proof bounds are invalid",
    )
    iterations = worker.get("iterations")
    _require(isinstance(iterations, list) and len(iterations) == budget.depth, "browser D1-D5 iterations are incomplete")
    previous_work = -1
    normalized_iterations: list[dict[str, Any]] = []
    for depth, raw_iteration in enumerate(iterations, start=1):
        iteration = _object(raw_iteration, f"browser D{depth} iteration")
        _require(iteration.get("depth") == depth, f"browser D{depth} iteration depth drifted")
        iteration_pv = _browser_principal_variation(
            iteration.get("principal_variation"),
            label=f"browser D{depth} principal variation",
        )
        iteration_selected = iteration.get("move")
        _require(
            isinstance(iteration_selected, str)
            and bool(iteration_selected)
            and iteration_pv[0] == iteration_selected,
            f"browser D{depth} selected series and PV differ",
        )
        iteration_work = _browser_work_positions(
            iteration.get("work"),
            max_work=budget.max_work,
            safety_reserve_work=safety_reserve_work,
            label=f"browser D{depth} work ledger",
        )
        _require(iteration_work >= previous_work, f"browser D{depth} work moved backwards")
        previous_work = iteration_work
        _finite_positive(iteration.get("elapsed_ms"), f"browser D{depth} elapsed time")
        _require(iteration.get("coverage_complete") is True, f"browser D{depth} coverage is incomplete")
        _require(iteration.get("root_scores_complete") is True, f"browser D{depth} root scores are incomplete")
        _require(
            iteration.get("safety_status") in {"exhausted", "terminal"},
            f"browser D{depth} mate safety is incomplete",
        )
        normalized_iterations.append(
            {
                "depth": depth,
                "selected_series": iteration_selected,
                "principal_variation": iteration_pv,
                "evaluation": iteration.get("score"),
                "work_positions": iteration_work,
            }
        )
    final_iteration = _object(iterations[-1], "browser D5 iteration")
    for field in (
        "move",
        "score",
        "proof_bounds",
        "work",
        "retained_manifest_sha256",
        "coverage_complete",
        "root_scores_complete",
        "safety_status",
    ):
        _require(result.get(field) == final_iteration.get(field), f"browser final result {field} differs from D5")
    _require(
        principal_variation
        == _browser_principal_variation(final_iteration.get("principal_variation"), label="browser final D5 PV"),
        "browser final result principal variation differs from D5",
    )
    from scottish_progressive.rules import SeriesLegalityError, play_series

    try:
        play_series(scenario_state(scenario), tuple(selected.split("/")))
    except SeriesLegalityError as error:
        raise GateError(f"browser selected series failed authoritative replay: {error}") from error

    return {
        "backend": "browser-wasm",
        "scenario": scenario,
        "mode": mode,
        "budget": budget.as_dict(),
        "requested_depth": budget.depth,
        "completed_depth": completed_depth,
        "wall_time_seconds": wall_seconds,
        "nodes": None,
        "work_positions": work_positions,
        "generated_unique_series": None,
        "unique_retained_states": None,
        "retained_manifest_sha256": retained_manifest_sha256,
        "nps": None,
        "selected_series": selected,
        "principal_variation": list(principal_variation),
        "evaluation": {
            "score_white_heuristic_points": result.get("score"),
            "proof_bounds": proof_bounds,
            "coverage_complete": result.get("coverage_complete"),
            "root_scores_complete": result.get("root_scores_complete"),
            "width_complete": result.get("width_complete"),
        },
        "timed_out": False,
        "timeout_reason": None,
        "artifact": {
            **baseline,
            "source_revision": source_revision,
            "artifact_set_sha256": artifact_set_sha256,
            "exception_strategy": artifact.get("exception_strategy"),
            "wasm_simd": artifact.get("wasm_simd"),
            "allocator": artifact.get("allocator"),
        },
        "iterations": normalized_iterations,
        "measurement_harness_git": _git_identity(),
        "environment": {
            "cdp": dict(cdp),
            "page": dict(page_environment),
            "worker": dict(worker_environment),
        },
        "measurement_note": (
            "wall time is accepted only from a passing real Opera Worker receipt; "
            "nodes/NPS are null because this WASM contract does not expose node count"
        ),
    }


def evaluate_browser_release(samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Fails unless every public play mode reaches D5 on both required boundaries."""

    required = {(scenario, mode) for scenario in SCENARIOS for mode in BUDGETS}
    keyed: dict[tuple[str, str], Mapping[str, Any]] = {}
    artifact_identities: set[tuple[Any, ...]] = set()
    for raw in samples:
        sample = _object(raw, "normalized browser sample")
        key = (str(sample.get("scenario")), str(sample.get("mode")))
        _require(key in required, f"unexpected browser release case {key}")
        _require(key not in keyed, f"duplicate browser release case {key}")
        _require(sample.get("backend") == "browser-wasm", f"browser release case {key} used the wrong backend")
        _require(sample.get("requested_depth") == 5, f"browser release case {key} did not request D5")
        _require(sample.get("budget") == BUDGETS[key[1]].as_dict(), f"browser release case {key} budget drifted")
        artifact = _object(sample.get("artifact"), f"browser release case {key} artifact")
        artifact_identities.add(
            tuple(
                artifact.get(field)
                for field in (
                    "source_revision",
                    "source_fingerprint",
                    "certificate_id",
                    "artifact_set_sha256",
                    "kernel_sha256",
                    "wasm_sha256",
                    "module_js_sha256",
                )
            )
        )
        keyed[key] = sample
    _require(set(keyed) == required, "browser release evidence is missing a required scenario/mode case")
    _require(len(artifact_identities) == 1, "browser release artifact identity drifted between cases")

    results: list[dict[str, Any]] = []
    for scenario, mode in sorted(required):
        sample = keyed[(scenario, mode)]
        wall = _finite_positive(sample.get("wall_time_seconds"), f"{scenario}/{mode} wall time")
        completed = sample.get("completed_depth") == 5
        within = wall <= BUDGETS[mode].seconds
        clean_stop = sample.get("timed_out") is False and sample.get("timeout_reason") is None
        results.append(
            {
                "scenario": scenario,
                "mode": mode,
                "completed_depth_5": completed,
                "within_mode_budget": within,
                "clean_stop": clean_stop,
                "passed": completed and within and clean_stop,
            }
        )
    return {
        "schema": "spc-browser-d5-release-decision-v1",
        "passed": all(result["passed"] for result in results),
        "required_case_count": len(required),
        "all_completed_depth_5": all(result["completed_depth_5"] for result in results),
        "all_within_mode_budget": all(result["within_mode_budget"] for result in results),
        "all_clean_stop": all(result["clean_stop"] for result in results),
        "cases": results,
        "policy": (
            "Initial and Black-after-e4 must each complete requested D5 in both "
            "Faster (5s) and Strong (30s) real browser-WASM budgets."
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    browser = subparsers.add_parser(
        "normalize-browser",
        help="validate and normalize one real Opera/WASM benchmark receipt",
    )
    browser.add_argument("--receipt", type=Path, required=True)
    browser.add_argument("--scenario", choices=tuple(SCENARIOS), required=True)
    browser.add_argument("--mode", choices=tuple(BUDGETS), required=True)
    browser.add_argument("--manifest", type=Path, default=DEFAULT_BROWSER_MANIFEST)
    browser.add_argument("--output", type=Path)
    browser_plan = subparsers.add_parser(
        "browser-plan",
        help="emit the exact four Opera probe URLs and capture commands",
    )
    browser_plan.add_argument("--origin", required=True)
    browser_plan.add_argument("--module-url", required=True)
    browser_plan.add_argument("--wasm-url", required=True)
    browser_plan.add_argument("--build-receipt-url", required=True)
    browser_plan.add_argument("--workers", type=int, default=8)
    browser_plan.add_argument("--output", type=Path)
    browser_release = subparsers.add_parser(
        "browser-release",
        help="require all four real browser D5 mode/scenario receipts",
    )
    for scenario in SCENARIOS:
        for mode in BUDGETS:
            browser_release.add_argument(
                f"--{scenario}-{mode}",
                dest=f"{scenario.replace('-', '_')}_{mode}",
                type=Path,
                required=True,
            )
    browser_release.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_BROWSER_MANIFEST,
    )
    browser_release.add_argument("--output", type=Path)
    native = subparsers.add_parser(
        "native",
        help="run fixed D5 Faster/Strong searches in fresh native processes",
    )
    native.add_argument("--samples", type=int, default=3)
    native.add_argument("--scenario", action="append", choices=tuple(SCENARIOS))
    native.add_argument("--mode", action="append", choices=tuple(BUDGETS))
    native.add_argument(
        "--measurement-quality",
        choices=("contended-functional-only", "quiet-controlled"),
        default="contended-functional-only",
    )
    native.add_argument("--output", type=Path)
    progress = subparsers.add_parser(
        "stockfish-progress",
        help="evaluate the separate quiet-controlled sub-10 D5 milestone",
    )
    progress.add_argument("--suite", type=Path, required=True)
    progress.add_argument("--target-seconds", type=float, default=10.0)
    progress.add_argument("--output", type=Path)
    tactical = subparsers.add_parser(
        "tactical",
        help="run every published tactical mate anchor through real search",
    )
    tactical.add_argument("--candidate", default="baseline")
    tactical.add_argument("--output", type=Path)
    strength = subparsers.add_parser(
        "strength",
        help="run an equal-budget candidate versus certified baseline-profile match",
    )
    strength.add_argument("--candidate", required=True)
    strength.add_argument("--candidate-value-model", type=Path)
    strength.add_argument("--pairs", type=int, default=10)
    strength.add_argument("--seed", type=int, default=20260820)
    strength.add_argument("--depth", type=int, default=2)
    strength.add_argument("--width", type=int, default=32)
    strength.add_argument("--max-search-work", type=int, default=250_000)
    strength.add_argument("--max-game-work", type=int, default=5_000_000)
    strength.add_argument("--workers", type=int)
    strength.add_argument("--minimum-score-rate", type=float, default=0.5)
    strength.add_argument("--manifest", type=Path, default=DEFAULT_BROWSER_MANIFEST)
    strength.add_argument("--output", type=Path)
    worker = subparsers.add_parser("_native-worker", help=argparse.SUPPRESS)
    worker.add_argument("--scenario", choices=tuple(SCENARIOS), required=True)
    worker.add_argument("--mode", choices=tuple(BUDGETS), required=True)
    worker.add_argument(
        "--measurement-quality",
        choices=("contended-functional-only", "quiet-controlled"),
        required=True,
    )
    return parser


def _write(payload: Mapping[str, Any], output: Path | None) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"cannot load {label}: {error}") from error
    return _object(value, label)


def main() -> int:
    args = _parser().parse_args()
    if args.command == "_native-worker":
        return _native_worker(args.scenario, args.mode, args.measurement_quality)
    if args.command == "normalize-browser":
        raw = _load_json(args.receipt, "browser receipt")
        _write(
            normalize_browser_receipt(
                raw,
                scenario=args.scenario,
                mode=args.mode,
                manifest_path=args.manifest,
            ),
            args.output,
        )
        return 0
    if args.command == "browser-plan":
        _write(
            build_browser_probe_plan(
                origin=args.origin,
                module_url=args.module_url,
                wasm_url=args.wasm_url,
                build_receipt_url=args.build_receipt_url,
                workers=args.workers,
            ),
            args.output,
        )
        return 0
    if args.command == "browser-release":
        samples = []
        for scenario in SCENARIOS:
            for mode in BUDGETS:
                path = getattr(args, f"{scenario.replace('-', '_')}_{mode}")
                samples.append(
                    normalize_browser_receipt(
                        _load_json(path, f"{scenario}/{mode} browser receipt"),
                        scenario=scenario,
                        mode=mode,
                        manifest_path=args.manifest,
                    )
                )
        decision = evaluate_browser_release(samples)
        payload = {
            "schema": "spc-browser-d5-release-gate-v1",
            "status": "passed" if decision["passed"] else "failed",
            "decision": decision,
            "samples": samples,
        }
        _write(payload, args.output)
        return 0 if decision["passed"] else 2
    if args.command == "native":
        _write(
            run_native_suite(
                samples=args.samples,
                scenarios=tuple(args.scenario or SCENARIOS),
                modes=tuple(args.mode or BUDGETS),
                measurement_quality=args.measurement_quality,
            ),
            args.output,
        )
        return 0
    if args.command == "stockfish-progress":
        decision = evaluate_stockfish_progress(
            _load_json(args.suite, "native search suite"),
            target_seconds=args.target_seconds,
        )
        _write(decision, args.output)
        return 0 if decision["passed"] else 2
    if args.command == "tactical":
        _write(run_tactical_gate(args.candidate), args.output)
        return 0
    if args.command == "strength":
        payload = run_strength_gate(
            args.candidate,
            candidate_value_model=args.candidate_value_model,
            pairs=args.pairs,
            seed=args.seed,
            depth=args.depth,
            width=args.width,
            max_search_work=args.max_search_work,
            max_game_work=args.max_game_work,
            workers=args.workers,
            minimum_score_rate=args.minimum_score_rate,
            manifest_path=args.manifest,
        )
        _write(payload, args.output)
        return 0 if payload["status"] == "passed" else 2
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
