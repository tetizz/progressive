"""Cross-package semantic/work gate for native scout-tail ordering changes.

The coordinator never imports ``scottish_progressive``. Each runtime is loaded
in a fresh child process from one explicit package directory, and every loaded
engine module is checked to be inside that directory. This permits source and
native-extension candidates with incompatible identities to be compared
without contaminating either interpreter.

Timing is reported but is not an acceptance criterion unless an explicit
threshold is supplied. Deterministic work and the complete semantic search
result are the primary ordering-change evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence


REPORT_FORMAT = "spc-scout-tail-ordering-differential-v1"
WORKER_FORMAT = "spc-scout-tail-ordering-worker-v1"
CORPUS_SEED_BASE = 20260821
EXPECTED_BASELINE_SOURCE_FINGERPRINT = "61b9cafa4a8bccc5"
EXPECTED_BASELINE_NATIVE_SOURCE_IDENTITY = (
    "89014134eabd4589a980d024c053fd4cfe2c1da21a3369eb8012eb74057c98df"
)
EXPECTED_CANDIDATE_SOURCE_COMMIT = (
    "83f7bff6a6d31cacb5d8e501eab6a4564111d6a4"
)

ANCHOR_NAMES = (
    "white",
    "black",
    "best-only",
    "promoted-clocks-quiet",
    "progressive-ep",
    "castling-rights",
)
HEADLINE_NAMES = (
    "white-anchor-d5",
    "initial-d3",
    "initial-d4",
    "initial-d5",
    "after-e4-d3",
    "after-e4-d4",
    "series3-d3",
    "hard-s4-d5",
    "s7-d4",
)
REQUIRED_D5_HEADLINES = ("white-anchor-d5", "hard-s4-d5")


class WorkerIntegrityError(RuntimeError):
    """A child loaded engine code outside its pinned package directory."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _series_payload(series: Any) -> dict[str, Any]:
    outcome = series.outcome
    return {
        "machine_notation": series.machine_notation,
        "moves": list(series.moves),
        "san": list(series.san),
        "final_pfen": series.final_state.pfen,
        "final_promoted": int(series.final_state.board.promoted),
        "final_halfmove_clock": int(series.final_state.board.halfmove_clock),
        "final_fullmove_number": int(series.final_state.board.fullmove_number),
        "final_series_number": int(series.final_state.series_number),
        "final_quiet_series": int(series.final_state.quiet_series),
        "final_ep_targets": list(series.final_state.ep_targets),
        "ended_by_check": bool(series.ended_by_check),
        "outcome": None if outcome is None else outcome.value,
        "unused_moves": int(series.unused_moves),
        "transposition_count": int(series.transposition_count),
    }


def _semantic_payload(result: Any) -> dict[str, Any]:
    """Return every decision/proof field while excluding work and timing."""

    return {
        "score": int(result.score),
        "best_series": (
            None if result.best_series is None else _series_payload(result.best_series)
        ),
        "principal_variation": [
            _series_payload(item) for item in result.principal_variation
        ],
        "alternatives": [
            {
                "series": _series_payload(item.series),
                "score": int(item.score),
                "principal_variation": [
                    _series_payload(pv) for pv in item.principal_variation
                ],
                "proof_bounds": list(item.proof_bounds),
                "proof": item.proof,
            }
            for item in result.alternatives
        ],
        "requested_depth": int(result.requested_depth),
        "completed_depth": int(result.completed_depth),
        "exact_width": bool(result.exact_width),
        "timed_out": bool(result.timed_out),
        "work_limit_reached": bool(result.work_limit_reached),
        "root_scores_complete": bool(result.root_scores_complete),
        "proof": result.proof,
        "forced": result.forced,
        "adjudication_status": result.adjudication_status,
        "classification": result.classification,
        "confidence": result.confidence,
        "required_prefix": list(result.required_prefix),
        "root_evaluation": result.root_evaluation.as_dict(),
    }


def _assert_package_modules_isolated(package_root: Path) -> None:
    for name, module in tuple(sys.modules.items()):
        if name != "scottish_progressive" and not name.startswith(
            "scottish_progressive."
        ):
            continue
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            continue
        resolved = Path(module_file).resolve()
        if not _path_is_within(resolved, package_root):
            raise WorkerIntegrityError(
                f"{name} escaped package isolation: {resolved} is not inside "
                f"{package_root}"
            )


def _activate_package(package: Path) -> tuple[dict[str, Any], Any, Any, Any, Any]:
    package = package.expanduser().resolve(strict=True)
    expected_root = package / "scottish_progressive"
    if not (expected_root / "__init__.py").is_file():
        raise WorkerIntegrityError(
            f"package directory has no scottish_progressive package: {package}"
        )
    sys.path.insert(0, str(package))

    import scottish_progressive
    import scottish_progressive.search as search_module
    from scottish_progressive import _native_eval
    from scottish_progressive import evaluation
    from scottish_progressive.model import (
        ENGINE_SOURCE_FINGERPRINT,
        ENGINE_VERSION,
    )
    from scottish_progressive.native_subtree import native_subtree_available
    from scottish_progressive.profiles import baseline_profile
    from scottish_progressive.search import SearchLimits, analyze

    loaded_root = Path(scottish_progressive.__file__).resolve().parent
    if loaded_root != expected_root.resolve():
        raise WorkerIntegrityError(
            f"requested package {expected_root.resolve()} but loaded {loaded_root}"
        )
    native_path = Path(_native_eval.__file__).resolve()
    if not _path_is_within(native_path, loaded_root):
        raise WorkerIntegrityError(
            f"native extension escaped package isolation: {native_path}"
        )
    expected_native_identity = evaluation._native_source_identity()  # noqa: SLF001
    actual_native_identity = getattr(_native_eval, "SOURCE_IDENTITY", None)
    if expected_native_identity != actual_native_identity:
        raise WorkerIntegrityError(
            "native extension identity does not match its packaged sources: "
            f"expected {expected_native_identity}, got {actual_native_identity}"
        )
    if not native_subtree_available():
        raise WorkerIntegrityError(
            "source-matched native subtree search is unavailable"
        )

    profile = baseline_profile()
    identity = {
        "requested_package": str(package),
        "loaded_package": str(loaded_root),
        "engine_version": ENGINE_VERSION,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "native_source_identity": actual_native_identity,
        "native_module": str(native_path),
        "native_module_sha256": _sha256(native_path),
        "native_subtree_enabled": bool(search_module.NATIVE_SUBTREE_ENABLED),
        "root_pvs_enabled": bool(search_module.ROOT_PVS_ENABLED),
        "profile": profile.as_dict(),
    }
    _assert_package_modules_isolated(loaded_root)
    return identity, SearchLimits, analyze, profile, loaded_root


def _replay(initial: Any, play_series: Any, history: Sequence[Sequence[str]]) -> Any:
    state = initial()
    for series in history:
        state = play_series(state, tuple(series)).final_state
    return state


def _case(
    case_id: str,
    group: str,
    state: Any,
    limits: Any,
) -> tuple[dict[str, Any], Any, Any]:
    return (
        {
            "case_id": case_id,
            "group": group,
            "pfen": state.pfen,
            "promoted": int(state.board.promoted),
            "halfmove_clock": int(state.board.halfmove_clock),
            "fullmove_number": int(state.board.fullmove_number),
            "series_number": int(state.series_number),
            "quiet_series": int(state.quiet_series),
            "ep_targets": list(state.ep_targets),
            "limits": asdict(limits),
        },
        state,
        limits,
    )


def _anchor_cases(SearchLimits: Any) -> list[tuple[dict[str, Any], Any, Any]]:
    import chess

    from scottish_progressive.model import ProgressiveState

    rows = (
        (
            "white",
            "3n4/5k2/5N2/K7/R7/8/3q4/8 w - - 0 1",
            1,
            True,
        ),
        (
            "black",
            "8/1Bn5/8/8/4R2K/k7/1q6/8 b - - 0 1",
            2,
            True,
        ),
        (
            "best-only",
            "8/6R1/5K2/8/1n2B3/1k6/8/4r3 w - - 0 1",
            1,
            False,
        ),
    )
    cases = [
        _case(
            f"anchor/{name}",
            "anchors",
            ProgressiveState.from_fen(fen, series_number),
            SearchLimits(
                depth_series=4,
                max_series_per_node=4,
                collect_all_root_scores=collect_all,
                native_threads=1,
            ),
        )
        for name, fen, series_number, collect_all in rows
    ]
    compact_limits = SearchLimits(
        depth_series=5,
        max_series_per_node=4,
        max_generation_positions=10_000_000,
        time_limit_seconds=180.0,
        collect_all_root_scores=False,
        native_threads=1,
    )
    promoted_board = chess.Board("7k/8/8/8/8/8/Q7/K7 w - - 12 34")
    promoted_board.promoted = chess.BB_A2
    cases.extend(
        (
            _case(
                "anchor/promoted-clocks-quiet",
                "anchors",
                ProgressiveState(
                    promoted_board,
                    series_number=1,
                    quiet_series=4,
                ),
                compact_limits,
            ),
            _case(
                "anchor/progressive-ep",
                "anchors",
                ProgressiveState.from_fen(
                    "7k/8/8/pPpP4/8/8/8/K7 w - - 7 19",
                    3,
                    quiet_series=2,
                    ep_targets=(chess.A6, chess.C6),
                ),
                compact_limits,
            ),
            _case(
                "anchor/castling-rights",
                "anchors",
                ProgressiveState.from_fen(
                    "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 5 9",
                    1,
                    quiet_series=1,
                ),
                compact_limits,
            ),
        )
    )
    return cases


def _sparse_states(series_number: int, count: int) -> tuple[Any, ...]:
    import chess

    from scottish_progressive.model import ProgressiveState

    rng = random.Random(CORPUS_SEED_BASE + series_number)
    states: list[Any] = []
    seen: set[str] = set()
    while len(states) < count:
        squares = rng.sample(list(chess.SQUARES), 6)
        board = chess.Board(None)
        board.turn = chess.WHITE if series_number % 2 else chess.BLACK
        board.set_piece_at(squares[0], chess.Piece(chess.KING, chess.WHITE))
        board.set_piece_at(squares[1], chess.Piece(chess.KING, chess.BLACK))
        board.set_piece_at(
            squares[2],
            chess.Piece(rng.choice((chess.ROOK, chess.QUEEN)), chess.WHITE),
        )
        board.set_piece_at(
            squares[3],
            chess.Piece(rng.choice((chess.ROOK, chess.QUEEN)), chess.BLACK),
        )
        board.set_piece_at(
            squares[4],
            chess.Piece(rng.choice((chess.BISHOP, chess.KNIGHT)), chess.WHITE),
        )
        board.set_piece_at(
            squares[5],
            chess.Piece(rng.choice((chess.BISHOP, chess.KNIGHT)), chess.BLACK),
        )
        board.halfmove_clock = rng.randrange(0, 18)
        board.fullmove_number = rng.randrange(1, 20)
        if not board.is_valid() or board.is_game_over(claim_draw=False):
            continue
        state = ProgressiveState.from_fen(board.fen(), series_number)
        if state.pfen in seen:
            continue
        seen.add(state.pfen)
        states.append(state)
    return tuple(states)


def _corpus_cases(
    SearchLimits: Any,
    count_per_series: int,
) -> list[tuple[dict[str, Any], Any, Any]]:
    cases: list[tuple[dict[str, Any], Any, Any]] = []
    for series_number in range(1, 9):
        for index, state in enumerate(_sparse_states(series_number, count_per_series)):
            cases.append(
                _case(
                    f"corpus/s{series_number}/{index:03d}",
                    "corpus",
                    state,
                    SearchLimits(
                        depth_series=4,
                        max_series_per_node=4,
                        collect_all_root_scores=False,
                        native_threads=1,
                    ),
                )
            )
    return cases


def _headline_case(
    name: str,
    SearchLimits: Any,
) -> tuple[dict[str, Any], Any, Any]:
    from scottish_progressive.model import ProgressiveState
    from scottish_progressive.rules import play_series

    initial = ProgressiveState.initial
    if name == "white-anchor-d5":
        state = ProgressiveState.from_fen(
            "3n4/5k2/5N2/K7/R7/8/3q4/8 w - - 0 1",
            1,
        )
    elif name.startswith("initial-"):
        state = initial()
    elif name.startswith("after-e4-"):
        state = _replay(initial, play_series, (("e2e4",),))
    elif name == "series3-d3":
        state = _replay(initial, play_series, (("e2e4",), ("f7f5", "e8f7")))
    elif name == "hard-s4-d5":
        state = _replay(
            initial,
            play_series,
            (
                ("g1f3",),
                ("e7e6", "d8f6"),
                ("d2d4", "c1g5", "g5f6"),
            ),
        )
    elif name == "s7-d4":
        state = ProgressiveState.from_fen(
            "rnk3nr/pp3ppp/8/8/8/1Pp1P3/P1PP1PPP/R1b1K1NR w K - 0 13",
            7,
        )
    else:  # pragma: no cover - argparse and coordinator validate this first.
        raise ValueError(f"unknown headline case: {name}")

    settings: dict[str, tuple[int, int, int, int]] = {
        "white-anchor-d5": (5, 10_000_000, 1, 4),
        "initial-d3": (3, 2_000_000, 1, 32),
        "initial-d4": (4, 10_000_000, 16, 32),
        "initial-d5": (5, 10_000_000, 1, 32),
        "after-e4-d3": (3, 2_000_000, 1, 32),
        "after-e4-d4": (4, 10_000_000, 16, 32),
        "series3-d3": (3, 3_000_000, 1, 32),
        "hard-s4-d5": (5, 10_000_000, 16, 32),
        "s7-d4": (4, 10_000_000, 16, 32),
    }
    depth, max_work, native_threads, width = settings[name]
    limits = SearchLimits(
        depth_series=depth,
        max_series_per_node=width,
        max_generation_positions=max_work,
        time_limit_seconds=180.0,
        collect_all_root_scores=False,
        native_threads=native_threads,
    )
    return _case(f"headline/{name}", "headlines", state, limits)


def _worker_cases(args: argparse.Namespace, SearchLimits: Any) -> list[Any]:
    selected = args.headline_case or []
    if args.group == "anchors":
        if selected:
            raise ValueError("anchor worker cannot select a headline case")
        return _anchor_cases(SearchLimits)
    if args.group == "corpus":
        if selected:
            raise ValueError("corpus worker cannot select a headline case")
        if args.corpus_count_per_series < 1:
            raise ValueError("corpus worker requires a positive corpus count")
        return _corpus_cases(SearchLimits, args.corpus_count_per_series)
    if args.group == "headline":
        if len(selected) != 1 or selected[0] not in HEADLINE_NAMES:
            raise ValueError("headline worker requires one valid headline case")
        return [_headline_case(selected[0], SearchLimits)]
    raise ValueError(f"unknown worker group: {args.group}")


def _worker_main(args: argparse.Namespace) -> int:
    identity, SearchLimits, analyze, profile, loaded_root = _activate_package(
        args.package
    )
    cases = _worker_cases(args, SearchLimits)
    rows: list[dict[str, Any]] = []
    group_started = time.perf_counter()
    for metadata, state, limits in cases:
        started = time.perf_counter()
        result = analyze(state, limits, profile)
        elapsed = time.perf_counter() - started
        stats = asdict(result.stats)
        semantic = _semantic_payload(result)
        complete = bool(
            result.best_series is not None
            and not result.timed_out
            and not result.work_limit_reached
            and result.completed_depth == result.requested_depth
        )
        rows.append(
            {
                "case": metadata,
                "elapsed_seconds": elapsed,
                "work_positions": int(result.stats.work_positions),
                "complete": complete,
                "semantic": semantic,
                "semantic_sha256": _canonical_sha256(semantic),
                "stats": stats,
                "stats_sha256": _canonical_sha256(stats),
            }
        )
    group_elapsed = time.perf_counter() - group_started
    _assert_package_modules_isolated(loaded_root)
    payload = {
        "format": WORKER_FORMAT,
        "runtime": args.runtime,
        "group": args.group,
        "headline_case": (
            args.headline_case[0] if args.group == "headline" else None
        ),
        "corpus_count_per_series": args.corpus_count_per_series,
        "identity": identity,
        "group_elapsed_seconds": group_elapsed,
        "cases": rows,
    }
    print(_canonical_json(payload))
    return 0


def _worker_command(
    args: argparse.Namespace,
    package: Path,
    runtime: str,
    group: str,
    headline_case: str | None,
) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--package",
        str(package),
        "--runtime",
        runtime,
        "--group",
        group,
        "--corpus-count-per-series",
        str(args.corpus_count_per_series),
    ]
    if headline_case is not None:
        command.extend(("--headline-case", headline_case))
    return command


def _sample(
    args: argparse.Namespace,
    package: Path,
    runtime: str,
    group: str,
    headline_case: str | None,
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    for name in tuple(environment):
        if name.startswith("SPC_"):
            environment.pop(name)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    completed = subprocess.run(
        _worker_command(args, package, runtime, group, headline_case),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=args.worker_timeout_seconds,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()[-4000:]
        stdout = completed.stdout.strip()[-2000:]
        raise RuntimeError(
            f"{runtime} {group} worker failed with {completed.returncode}; "
            f"stderr={stderr!r}; stdout={stdout!r}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{runtime} {group} worker emitted malformed JSON: "
            f"{completed.stdout[-2000:]!r}"
        ) from error
    expected_headline = headline_case if group == "headline" else None
    if (
        payload.get("format") != WORKER_FORMAT
        or payload.get("runtime") != runtime
        or payload.get("group") != group
        or payload.get("headline_case") != expected_headline
        or not isinstance(payload.get("cases"), list)
    ):
        raise RuntimeError(f"{runtime} {group} worker protocol mismatch")
    return payload


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values))


def _percent_delta(baseline: int | float, candidate: int | float) -> float | None:
    if baseline == 0:
        return 0.0 if candidate == 0 else None
    return 100.0 * (candidate - baseline) / baseline


def _unique(values: Sequence[object]) -> bool:
    return all(value == values[0] for value in values[1:])


def _summarize_group(
    task_name: str,
    samples: Mapping[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    runtime_case_maps: dict[str, list[dict[str, dict[str, Any]]]] = {}
    identities: dict[str, dict[str, Any]] = {}
    for runtime in ("baseline", "candidate"):
        payloads = samples[runtime]
        runtime_identities = [payload["identity"] for payload in payloads]
        if not _unique(runtime_identities):
            failures.append(f"{task_name}: {runtime} identity changed between samples")
        identities[runtime] = runtime_identities[0]
        maps: list[dict[str, dict[str, Any]]] = []
        for payload in payloads:
            case_map: dict[str, dict[str, Any]] = {}
            for row in payload["cases"]:
                case_id = row.get("case", {}).get("case_id")
                if not isinstance(case_id, str) or case_id in case_map:
                    raise RuntimeError(f"{task_name}: malformed or duplicate case id")
                case_map[case_id] = row
            maps.append(case_map)
        if any(tuple(item) != tuple(maps[0]) for item in maps[1:]):
            failures.append(
                f"{task_name}: {runtime} case order changed between samples"
            )
        runtime_case_maps[runtime] = maps

    baseline_order = tuple(runtime_case_maps["baseline"][0])
    candidate_order = tuple(runtime_case_maps["candidate"][0])
    if baseline_order != candidate_order:
        raise RuntimeError(f"{task_name}: baseline/candidate case sets differ")

    cases: list[dict[str, Any]] = []
    for case_id in baseline_order:
        baseline_rows = [rows[case_id] for rows in runtime_case_maps["baseline"]]
        candidate_rows = [rows[case_id] for rows in runtime_case_maps["candidate"]]
        baseline_metadata = [row["case"] for row in baseline_rows]
        candidate_metadata = [row["case"] for row in candidate_rows]
        if not _unique(baseline_metadata) or not _unique(candidate_metadata):
            failures.append(f"{case_id}: state/limits changed between samples")
        if baseline_metadata[0] != candidate_metadata[0]:
            failures.append(f"{case_id}: baseline/candidate state or limits differ")

        baseline_semantics = [row["semantic"] for row in baseline_rows]
        candidate_semantics = [row["semantic"] for row in candidate_rows]
        baseline_semantic_deterministic = _unique(baseline_semantics)
        candidate_semantic_deterministic = _unique(candidate_semantics)
        semantic_match = baseline_semantics[0] == candidate_semantics[0]
        if not baseline_semantic_deterministic:
            failures.append(f"{case_id}: baseline semantics are non-deterministic")
        if not candidate_semantic_deterministic:
            failures.append(f"{case_id}: candidate semantics are non-deterministic")
        if not semantic_match:
            failures.append(f"{case_id}: exact semantic mismatch")

        baseline_work = [int(row["work_positions"]) for row in baseline_rows]
        candidate_work = [int(row["work_positions"]) for row in candidate_rows]
        baseline_work_deterministic = _unique(baseline_work)
        candidate_work_deterministic = _unique(candidate_work)
        if not baseline_work_deterministic:
            failures.append(f"{case_id}: baseline work is non-deterministic")
        if not candidate_work_deterministic:
            failures.append(f"{case_id}: candidate work is non-deterministic")

        baseline_complete = all(bool(row["complete"]) for row in baseline_rows)
        candidate_complete = all(bool(row["complete"]) for row in candidate_rows)
        if not baseline_complete:
            failures.append(f"{case_id}: baseline search did not complete")
        if not candidate_complete:
            failures.append(f"{case_id}: candidate search did not complete")

        baseline_seconds = [float(row["elapsed_seconds"]) for row in baseline_rows]
        candidate_seconds = [float(row["elapsed_seconds"]) for row in candidate_rows]
        baseline_median = _median(baseline_seconds)
        candidate_median = _median(candidate_seconds)
        work_delta = candidate_work[0] - baseline_work[0]
        work_percent = _percent_delta(baseline_work[0], candidate_work[0])
        timing_percent = _percent_delta(baseline_median, candidate_median)
        semantic_payload: object
        if semantic_match:
            semantic_payload = baseline_semantics[0]
        else:
            semantic_payload = {
                "baseline": baseline_semantics[0],
                "candidate": candidate_semantics[0],
            }
        cases.append(
            {
                "case": baseline_metadata[0],
                "semantic_match": semantic_match,
                "semantic_sha256": {
                    "baseline": _canonical_sha256(baseline_semantics[0]),
                    "candidate": _canonical_sha256(candidate_semantics[0]),
                },
                "semantic": semantic_payload,
                "completion": {
                    "baseline": baseline_complete,
                    "candidate": candidate_complete,
                },
                "work": {
                    "baseline_samples": baseline_work,
                    "candidate_samples": candidate_work,
                    "baseline_deterministic": baseline_work_deterministic,
                    "candidate_deterministic": candidate_work_deterministic,
                    "delta": work_delta,
                    "candidate_percent_delta": work_percent,
                    "candidate_percent_reduction": (
                        None if work_percent is None else -work_percent
                    ),
                },
                "timing": {
                    "baseline_seconds": baseline_seconds,
                    "candidate_seconds": candidate_seconds,
                    "baseline_median_seconds": baseline_median,
                    "candidate_median_seconds": candidate_median,
                    "candidate_percent_delta": timing_percent,
                },
                "stats": {
                    "baseline": baseline_rows[0]["stats"],
                    "candidate": candidate_rows[0]["stats"],
                    "baseline_sha256": baseline_rows[0]["stats_sha256"],
                    "candidate_sha256": candidate_rows[0]["stats_sha256"],
                },
                "determinism": {
                    "baseline_semantic": baseline_semantic_deterministic,
                    "candidate_semantic": candidate_semantic_deterministic,
                },
            }
        )

    baseline_total_work = sum(row["work"]["baseline_samples"][0] for row in cases)
    candidate_total_work = sum(row["work"]["candidate_samples"][0] for row in cases)
    baseline_group_seconds = [
        float(payload["group_elapsed_seconds"])
        for payload in samples["baseline"]
    ]
    candidate_group_seconds = [
        float(payload["group_elapsed_seconds"])
        for payload in samples["candidate"]
    ]
    baseline_group_median = _median(baseline_group_seconds)
    candidate_group_median = _median(candidate_group_seconds)
    return (
        {
            "task": task_name,
            "runtime_identities": identities,
            "case_count": len(cases),
            "cases": cases,
            "work": {
                "baseline_total": baseline_total_work,
                "candidate_total": candidate_total_work,
                "delta": candidate_total_work - baseline_total_work,
                "candidate_percent_delta": _percent_delta(
                    baseline_total_work, candidate_total_work
                ),
            },
            "timing": {
                "baseline_group_seconds": baseline_group_seconds,
                "candidate_group_seconds": candidate_group_seconds,
                "baseline_median_seconds": baseline_group_median,
                "candidate_median_seconds": candidate_group_median,
                "candidate_percent_delta": _percent_delta(
                    baseline_group_median, candidate_group_median
                ),
                "acceptance_relevant": False,
            },
        },
        failures,
    )


def _selected_headlines(args: argparse.Namespace) -> tuple[str, ...]:
    requested = args.headline_case or []
    if "all" in requested:
        return HEADLINE_NAMES
    return tuple(name for name in HEADLINE_NAMES if name in requested)


def _is_lower_hex(value: str | None, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_coordinator_args(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    if args.baseline_package is None or args.candidate_package is None:
        raise ValueError("--baseline-package and --candidate-package are required")
    if args.output is None:
        raise ValueError("--output is required")
    baseline = args.baseline_package.expanduser().resolve(strict=True)
    candidate = args.candidate_package.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    for name, package in (("baseline", baseline), ("candidate", candidate)):
        if not (package / "scottish_progressive" / "__init__.py").is_file():
            raise ValueError(f"{name} package is invalid: {package}")
        if _path_is_within(output, package):
            raise ValueError(f"output cannot be written inside the {name} package")
    if args.self_smoke != (baseline == candidate):
        raise ValueError(
            "identical package paths require --self-smoke, and --self-smoke "
            "requires identical paths"
        )
    if not 2 <= args.samples <= 20:
        raise ValueError("--samples must be between 2 and 20")
    if not 0 <= args.corpus_count_per_series <= 512:
        raise ValueError("--corpus-count-per-series must be between 0 and 512")
    if not 1 <= args.worker_timeout_seconds <= 86_400:
        raise ValueError("--worker-timeout-seconds must be between 1 and 86400")
    for name in (
        "max_group_work_regression_percent",
        "max_headline_work_regression_percent",
        "minimum_required_work_improvement_percent",
    ):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0:
            raise ValueError(
                f"--{name.replace('_', '-')} must be finite and non-negative"
            )
    if (
        args.max_timing_regression_percent is not None
        and (
            not math.isfinite(args.max_timing_regression_percent)
            or args.max_timing_regression_percent < 0
        )
    ):
        raise ValueError(
            "--max-timing-regression-percent must be finite and non-negative"
        )
    headlines = _selected_headlines(args)
    missing_required = set(args.require_headline_improvement) - set(headlines)
    if missing_required:
        raise ValueError(
            "required improvement cases were not selected: "
            + ", ".join(sorted(missing_required))
        )
    if args.self_smoke and args.require_headline_improvement:
        raise ValueError("self-smoke cannot require a work improvement")
    if not args.self_smoke:
        missing_d5 = set(REQUIRED_D5_HEADLINES) - set(headlines)
        if missing_d5:
            raise ValueError(
                "differential mode requires both White and Black D5 headlines: "
                + ", ".join(sorted(missing_d5))
            )
        if not set(args.require_headline_improvement) & set(REQUIRED_D5_HEADLINES):
            raise ValueError(
                "differential mode requires a strict improvement gate on at least "
                "one selected D5 headline"
            )
        if not _is_lower_hex(args.expected_candidate_source_fingerprint, 16):
            raise ValueError(
                "--expected-candidate-source-fingerprint must be 16 lowercase "
                "hexadecimal characters"
            )
        if not _is_lower_hex(args.expected_candidate_native_source_identity, 64):
            raise ValueError(
                "--expected-candidate-native-source-identity must be 64 lowercase "
                "hexadecimal characters"
            )
    if args.skip_anchors and args.corpus_count_per_series == 0 and not headlines:
        raise ValueError("at least one anchor, corpus, or headline group is required")
    return baseline, candidate, output


def _tasks(args: argparse.Namespace) -> list[tuple[str, str, str | None]]:
    tasks: list[tuple[str, str, str | None]] = []
    if not args.skip_anchors:
        tasks.append(("anchors", "anchors", None))
    if args.corpus_count_per_series:
        tasks.append(("corpus", "corpus", None))
    tasks.extend(
        (f"headline:{name}", "headline", name)
        for name in _selected_headlines(args)
    )
    return tasks


def _apply_acceptance(
    args: argparse.Namespace,
    groups: Sequence[dict[str, Any]],
    failures: list[str],
) -> None:
    epsilon = 1e-12
    for group in groups:
        task = str(group["task"])
        group_work_delta = group["work"]["candidate_percent_delta"]
        if group_work_delta is None:
            failures.append(f"{task}: baseline group work was zero")
        elif group_work_delta > args.max_group_work_regression_percent + epsilon:
            failures.append(
                f"{task}: aggregate work regressed by {group_work_delta:.6f}% "
                f"(limit {args.max_group_work_regression_percent:.6f}%)"
            )
        if args.max_timing_regression_percent is not None:
            timing_delta = group["timing"]["candidate_percent_delta"]
            group["timing"]["acceptance_relevant"] = True
            if timing_delta is None:
                failures.append(f"{task}: baseline timing was zero")
            elif timing_delta > args.max_timing_regression_percent + epsilon:
                failures.append(
                    f"{task}: median timing regressed by {timing_delta:.6f}% "
                    f"(limit {args.max_timing_regression_percent:.6f}%)"
                )

        if not task.startswith("headline:"):
            continue
        name = task.removeprefix("headline:")
        case = group["cases"][0]
        work_delta = case["work"]["candidate_percent_delta"]
        if work_delta is None:
            failures.append(f"{task}: baseline case work was zero")
        elif work_delta > args.max_headline_work_regression_percent + epsilon:
            failures.append(
                f"{task}: work regressed by {work_delta:.6f}% "
                f"(limit {args.max_headline_work_regression_percent:.6f}%)"
            )
        if name in args.require_headline_improvement:
            baseline_work = case["work"]["baseline_samples"][0]
            candidate_work = case["work"]["candidate_samples"][0]
            reduction = case["work"]["candidate_percent_reduction"]
            if (
                candidate_work >= baseline_work
                or reduction is None
                or reduction + epsilon
                < args.minimum_required_work_improvement_percent
            ):
                failures.append(
                    f"{task}: required strict work improvement of at least "
                    f"{args.minimum_required_work_improvement_percent:.6f}% "
                    f"was not observed ({baseline_work} -> {candidate_work})"
                )


def _runtime_identity(groups: Sequence[dict[str, Any]], runtime: str) -> dict[str, Any]:
    identities = [group["runtime_identities"][runtime] for group in groups]
    if not _unique(identities):
        raise RuntimeError(f"{runtime} identity differs between task groups")
    return identities[0]


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _receipt_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value.expanduser().resolve())
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return [_receipt_value(item) for item in value]
    return value


def _configuration_receipt(args: argparse.Namespace) -> dict[str, Any]:
    return {
        name: _receipt_value(value)
        for name, value in sorted(vars(args).items())
        if name not in {"package", "runtime", "worker"}
    }


def _coordinator_output_target(args: argparse.Namespace) -> Path | None:
    if args.worker or args.output is None:
        return None
    output = args.output.expanduser().resolve()
    for package in (args.baseline_package, args.candidate_package):
        if package is not None and _path_is_within(
            output, package.expanduser().resolve()
        ):
            return None
    return output


def _preflight_payload(
    args: argparse.Namespace,
    *,
    run_state: str,
) -> dict[str, Any]:
    harness = Path(__file__).resolve()
    return {
        "format": REPORT_FORMAT,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "run_state": run_state,
        "harness": {"path": str(harness), "sha256": _sha256(harness)},
        "configuration": _configuration_receipt(args),
        "requested_packages": {
            "baseline": _receipt_value(args.baseline_package),
            "candidate": _receipt_value(args.candidate_package),
        },
        "required_candidate_identity": {
            "source_commit": EXPECTED_CANDIDATE_SOURCE_COMMIT,
            "source_fingerprint": args.expected_candidate_source_fingerprint,
            "native_source_identity": (
                args.expected_candidate_native_source_identity
            ),
        },
        "summary": {
            "passed": False,
            "failures": ["gate did not complete"],
            "claim_scope": "No benchmark result is established.",
        },
    }


def _write_preflight_receipt(
    args: argparse.Namespace,
    *,
    run_state: str,
) -> Path | None:
    output = _coordinator_output_target(args)
    if output is None:
        return None
    _write_atomic(output, _preflight_payload(args, run_state=run_state))
    return output


def _write_raw_argument_preflight(argv: Sequence[str]) -> Path | None:
    if "-h" in argv or "--help" in argv or "--worker" in argv:
        return None
    preliminary = argparse.ArgumentParser(
        add_help=False,
        allow_abbrev=False,
        exit_on_error=False,
    )
    preliminary.add_argument("--baseline-package", type=Path)
    preliminary.add_argument("--candidate-package", type=Path)
    preliminary.add_argument("--output", type=Path)
    try:
        extracted, _unknown = preliminary.parse_known_args(argv)
    except argparse.ArgumentError:
        return None
    if extracted.output is None:
        return None
    output = extracted.output.expanduser().resolve()
    requested_packages: dict[str, str | None] = {}
    for label, raw_package in (
        ("baseline", extracted.baseline_package),
        ("candidate", extracted.candidate_package),
    ):
        requested_packages[label] = (
            None if raw_package is None else str(raw_package)
        )
        if raw_package is not None and _path_is_within(
            output, raw_package.expanduser().resolve()
        ):
            return None
    harness = Path(__file__).resolve()
    _write_atomic(
        output,
        {
            "format": REPORT_FORMAT,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "run_state": "validating-arguments",
            "harness": {"path": str(harness), "sha256": _sha256(harness)},
            "raw_cli": list(argv),
            "raw_cli_sha256": hashlib.sha256(
                _canonical_json(list(argv)).encode("utf-8")
            ).hexdigest(),
            "requested_packages": requested_packages,
            "summary": {
                "passed": False,
                "failures": ["arguments were not validated"],
                "claim_scope": "No benchmark result is established.",
            },
        },
    )
    return output


def _finalize_failed_receipt_path(path: Path | None, error: BaseException) -> None:
    if path is None:
        return
    try:
        existing = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return
    if (
        existing.get("format") != REPORT_FORMAT
        or existing.get("run_state")
        not in {"validating-arguments", "validating", "in-progress"}
    ):
        return
    existing["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    existing["run_state"] = "failed"
    existing["summary"] = {
        "passed": False,
        "failures": [f"{type(error).__name__}: {error}"],
        "claim_scope": "No benchmark result is established.",
    }
    _write_atomic(path, existing)


def _finalize_failed_receipt(args: argparse.Namespace, error: BaseException) -> None:
    """Turn this run's preflight receipt into a terminal non-pass receipt."""

    _finalize_failed_receipt_path(_coordinator_output_target(args), error)


def run_gate(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    baseline, candidate, output = _validate_coordinator_args(args)
    # Replace any older green receipt before launching workers. If a worker
    # times out, crashes, or violates the protocol, this atomic non-pass receipt
    # remains instead of letting stale success masquerade as the current run.
    _write_atomic(output, _preflight_payload(args, run_state="in-progress"))
    tasks = _tasks(args)
    collected: dict[str, dict[str, list[dict[str, Any]]]] = {
        task_name: {"baseline": [], "candidate": []}
        for task_name, _, _ in tasks
    }
    packages = {"baseline": baseline, "candidate": candidate}
    for repetition in range(args.samples):
        for task_index, (task_name, group, headline_case) in enumerate(tasks):
            runtime_order = (
                ("baseline", "candidate")
                if (repetition + task_index) % 2 == 0
                else ("candidate", "baseline")
            )
            for runtime in runtime_order:
                collected[task_name][runtime].append(
                    _sample(
                        args,
                        packages[runtime],
                        runtime,
                        group,
                        headline_case,
                    )
                )

    groups: list[dict[str, Any]] = []
    failures: list[str] = []
    for task_name, _, _ in tasks:
        group, group_failures = _summarize_group(task_name, collected[task_name])
        groups.append(group)
        failures.extend(group_failures)
    _apply_acceptance(args, groups, failures)

    baseline_identity = _runtime_identity(groups, "baseline")
    candidate_identity = _runtime_identity(groups, "candidate")
    if (
        baseline_identity["source_fingerprint"]
        != EXPECTED_BASELINE_SOURCE_FINGERPRINT
    ):
        failures.append(
            "baseline source fingerprint is not the frozen fix-only identity: "
            f"identity: {baseline_identity['source_fingerprint']}"
        )
    if (
        baseline_identity["native_source_identity"]
        != EXPECTED_BASELINE_NATIVE_SOURCE_IDENTITY
    ):
        failures.append(
            "baseline native source identity is not the frozen fix-only identity: "
            f"identity: {baseline_identity['native_source_identity']}"
        )
    if not args.self_smoke:
        if (
            candidate_identity["source_fingerprint"]
            != args.expected_candidate_source_fingerprint
        ):
            failures.append(
                "candidate source fingerprint differs from the required identity: "
                f"{candidate_identity['source_fingerprint']}"
            )
        if (
            candidate_identity["native_source_identity"]
            != args.expected_candidate_native_source_identity
        ):
            failures.append(
                "candidate native source identity differs from the required "
                f"identity: {candidate_identity['native_source_identity']}"
            )
    for runtime, identity in (
        ("baseline", baseline_identity),
        ("candidate", candidate_identity),
    ):
        if not identity["native_subtree_enabled"]:
            failures.append(f"{runtime} native subtree search is disabled")
        if not identity["root_pvs_enabled"]:
            failures.append(f"{runtime} root PVS is disabled")
    if baseline_identity["profile"] != candidate_identity["profile"]:
        failures.append("baseline/candidate profiles differ")
    if baseline_identity["engine_version"] != candidate_identity["engine_version"]:
        failures.append("baseline/candidate engine versions differ")
    harness = Path(__file__).resolve()
    configuration = {
        "self_smoke": bool(args.self_smoke),
        "samples": args.samples,
        "determinism_verified": args.samples >= 2,
        "sampling_order": "fresh-process-paired-interleaved-alternating",
        "anchors": [] if args.skip_anchors else list(ANCHOR_NAMES),
        "corpus": {
            "seed_formula": "20260821 + series_number",
            "series_numbers": list(range(1, 9)),
            "count_per_series": args.corpus_count_per_series,
            "depth_series": 4,
            "max_series_per_node": 4,
        },
        "headline_cases": list(_selected_headlines(args)),
        "acceptance_thresholds": {
            "exact_semantic_equality": True,
            "complete_searches": True,
            "deterministic_semantics_and_work": True,
            "max_group_work_regression_percent": (
                args.max_group_work_regression_percent
            ),
            "max_headline_work_regression_percent": (
                args.max_headline_work_regression_percent
            ),
            "required_headline_improvements": list(
                args.require_headline_improvement
            ),
            "minimum_required_work_improvement_percent": (
                args.minimum_required_work_improvement_percent
            ),
            "max_timing_regression_percent": args.max_timing_regression_percent,
        },
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "required_candidate_identity": {
            "source_commit": EXPECTED_CANDIDATE_SOURCE_COMMIT,
            "source_fingerprint": args.expected_candidate_source_fingerprint,
            "native_source_identity": (
                args.expected_candidate_native_source_identity
            ),
        },
    }
    expected_baseline_identity = {
        "source_commit": "726041e5c434fc9f51c62cb404aa754fb0748b23",
        "base_commit": "00a336ba36b5b3b302357ce4397bf4fe67dd5452",
        "source_scope": "canonical-PV recertification fix only; scout ordering absent",
        "source_fingerprint": EXPECTED_BASELINE_SOURCE_FINGERPRINT,
        "native_source_identity": EXPECTED_BASELINE_NATIVE_SOURCE_IDENTITY,
        "commit_fields_are_declared_provenance": True,
        "package_fingerprints_are_verified": True,
    }
    receipt_projection = {
        "format": REPORT_FORMAT,
        "harness_sha256": _sha256(harness),
        "configuration": configuration,
        "runtime_identities": {
            "baseline": baseline_identity,
            "candidate": candidate_identity,
        },
        "expected_baseline_identity": expected_baseline_identity,
        "required_candidate_identity": configuration[
            "required_candidate_identity"
        ],
        "cases": [
            {
                "case": case["case"],
                "semantic_sha256": case["semantic_sha256"],
                "work": case["work"],
                "completion": case["completion"],
            }
            for group in groups
            for case in group["cases"]
        ],
    }
    report_id = "spc-scout-tail-ordering-" + _canonical_sha256(
        receipt_projection
    )[:16]
    payload = {
        "format": REPORT_FORMAT,
        "report_id": report_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "harness": {
            "path": str(harness),
            "sha256": _sha256(harness),
        },
        "environment": {
            "python_executable": sys.executable,
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "logical_cpu_count": os.cpu_count(),
        },
        "configuration": configuration,
        "expected_baseline_identity": expected_baseline_identity,
        "runtime_identities": {
            "baseline": baseline_identity,
            "candidate": candidate_identity,
        },
        "groups": groups,
        "summary": {
            "case_count": sum(group["case_count"] for group in groups),
            "semantic_mismatch_count": sum(
                not case["semantic_match"]
                for group in groups
                for case in group["cases"]
            ),
            "failures": failures,
            "passed": not failures,
            "timing_is_acceptance_relevant": (
                args.max_timing_regression_percent is not None
            ),
            "determinism_verified": args.samples >= 2,
            "claim_scope": (
                "Exact semantics, completion, deterministic charged work, and "
                "the configured frozen cases only. This is not a playing-strength "
                "or browser-release claim."
            ),
        },
    }
    return payload, output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=(
            "Compare isolated native engine packages on frozen scout-tail "
            "semantic and deterministic-work cases."
        )
    )
    parser.add_argument("--baseline-package", type=Path)
    parser.add_argument("--candidate-package", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--corpus-count-per-series", type=int, default=1)
    parser.add_argument("--skip-anchors", action="store_true")
    parser.add_argument(
        "--headline-case",
        action="append",
        choices=HEADLINE_NAMES + ("all",),
        help="Add a frozen headline; repeat the option or use 'all'.",
    )
    parser.add_argument(
        "--require-headline-improvement",
        action="append",
        choices=HEADLINE_NAMES,
        default=[],
        help="Require strictly lower deterministic work on this selected headline.",
    )
    parser.add_argument(
        "--minimum-required-work-improvement-percent",
        type=float,
        default=0.0,
    )
    parser.add_argument(
        "--max-group-work-regression-percent",
        type=float,
        default=0.0,
    )
    parser.add_argument(
        "--max-headline-work-regression-percent",
        type=float,
        default=0.0,
    )
    parser.add_argument(
        "--max-timing-regression-percent",
        type=float,
        help="Opt-in noisy wall-time gate; timing is report-only when omitted.",
    )
    parser.add_argument(
        "--expected-candidate-source-fingerprint",
        help="Required 16-hex candidate source fingerprint in differential mode.",
    )
    parser.add_argument(
        "--expected-candidate-native-source-identity",
        help="Required 64-hex candidate native identity in differential mode.",
    )
    parser.add_argument("--worker-timeout-seconds", type=float, default=1200.0)
    parser.add_argument(
        "--self-smoke",
        action="store_true",
        help="Require baseline and candidate to be the same package.",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--package", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--runtime",
        choices=("baseline", "candidate"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--group",
        choices=("anchors", "corpus", "headline"),
        help=argparse.SUPPRESS,
    )
    return parser


def _validate_worker_args(args: argparse.Namespace) -> None:
    if args.package is None or args.runtime is None or args.group is None:
        raise ValueError("worker requires --package, --runtime, and --group")
    if args.group == "corpus" and not 1 <= args.corpus_count_per_series <= 512:
        raise ValueError("worker corpus count must be between 1 and 512")


def main() -> int:
    parser = _parser()
    raw_preflight = _write_raw_argument_preflight(sys.argv[1:])
    try:
        args = parser.parse_args()
    except SystemExit as error:
        if error.code:
            _finalize_failed_receipt_path(
                raw_preflight,
                ValueError("argument parsing failed"),
            )
        raise
    try:
        if args.worker:
            _validate_worker_args(args)
            return _worker_main(args)
        _write_preflight_receipt(args, run_state="validating")
        payload, output = run_gate(args)
        _write_atomic(output, payload)
        print(json.dumps(payload["summary"], indent=2, sort_keys=True))
        if not payload["summary"]["passed"]:
            for failure in payload["summary"]["failures"]:
                print(f"FAIL: {failure}", file=sys.stderr)
            return 2
        return 0
    except subprocess.TimeoutExpired as error:
        _finalize_failed_receipt(args, error)
        parser.error(
            f"worker exceeded {error.timeout}s: {' '.join(map(str, error.cmd))}"
        )
    except (
        OSError,
        TypeError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
    ) as error:
        _finalize_failed_receipt(args, error)
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
