"""Deterministic cross-package strength gate for cycle 7.

The referee process owns every authoritative rules transition.  Engine code is
isolated in persistent package-specific JSONL helpers: a helper may nominate a
complete series, but it cannot mutate the match state or declare an outcome.
This lets two incompatible native builds play one fixed-work match without
ever importing both versions of ``scottish_progressive`` into one interpreter.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import random
import subprocess
import sys
import tempfile
import threading
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
REFEREE_SOURCE = REPO_ROOT / "src"
DEFAULT_SUITE_REPORT = (
    REPO_ROOT
    / "benchmarks"
    / "results"
    / "selfplay-fresh-seeded-100-v0.9.0.json"
)
REPORT_FORMAT = "spc-cycle7-binary-strength-gate-v1"


class WorkerIntegrityError(RuntimeError):
    """A helper crossed its pinned identity or JSONL protocol boundary."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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


def _activate_referee_source() -> None:
    source = str(REFEREE_SOURCE.resolve())
    if source not in sys.path:
        sys.path.insert(0, source)


def _state_payload(state: Any) -> dict[str, Any]:
    import chess

    board = state.board
    return {
        "fen": board.fen(en_passant="fen", promoted=True),
        "series_number": int(state.series_number),
        "quiet_series": int(state.quiet_series),
        "ep_targets": [
            chess.square_name(square) for square in state.ep_targets
        ],
        "promoted_bitboard": int(board.promoted),
        "chess960": bool(board.chess960),
    }


def _state_from_payload(payload: Mapping[str, Any]) -> Any:
    import chess

    from scottish_progressive.model import ProgressiveState

    required = {
        "fen",
        "series_number",
        "quiet_series",
        "ep_targets",
        "promoted_bitboard",
        "chess960",
    }
    if set(payload) != required:
        raise ValueError("state payload fields do not match the protocol")
    fen = payload["fen"]
    series_number = payload["series_number"]
    quiet_series = payload["quiet_series"]
    ep_names = payload["ep_targets"]
    promoted = payload["promoted_bitboard"]
    chess960 = payload["chess960"]
    if type(fen) is not str or not fen:
        raise TypeError("state fen must be a nonempty string")
    if type(series_number) is not int or series_number < 1:
        raise TypeError("state series_number must be a positive integer")
    if type(quiet_series) is not int or quiet_series < 0:
        raise TypeError("state quiet_series must be a nonnegative integer")
    if (
        type(ep_names) is not list
        or any(type(item) is not str for item in ep_names)
        or ep_names != sorted(set(ep_names))
    ):
        raise TypeError("state ep_targets must be unique sorted square names")
    if type(promoted) is not int or not 0 <= promoted < (1 << 64):
        raise TypeError("state promoted_bitboard must be an unsigned 64-bit integer")
    if type(chess960) is not bool:
        raise TypeError("state chess960 must be a boolean")
    board = chess.Board(fen, chess960=chess960)
    if int(board.promoted) != promoted:
        raise ValueError("state FEN and promoted bitboard disagree")
    board.promoted = promoted
    return ProgressiveState(
        board,
        series_number=series_number,
        quiet_series=quiet_series,
        ep_targets=tuple(chess.parse_square(item) for item in ep_names),
    )


def _load_verified_openings(
    report_path: Path,
) -> tuple[dict[str, Any], tuple[Any, ...]]:
    """Loads and authoritatively replays the report's full neutral suite."""

    _activate_referee_source()
    from scottish_progressive.league import OpeningCase
    from scottish_progressive.strength import (
        SEEDED_OPENING_SUITE_FORMAT,
        SeededOpeningHistory,
        SeededOpeningSuite,
        verify_seeded_opening_suite,
    )

    raw_bytes = report_path.read_bytes()
    report = json.loads(raw_bytes)
    if report.get("format") != "spc-fixed-suite-strength-v1":
        raise ValueError("suite report has an unsupported format")
    raw_suite = report.get("opening_suite")
    if type(raw_suite) is not dict:
        raise ValueError("suite report does not contain an embedded opening suite")
    if raw_suite.get("format") != SEEDED_OPENING_SUITE_FORMAT:
        raise ValueError("embedded opening suite has an unsupported format")
    raw_cases = raw_suite.get("cases")
    raw_histories = raw_suite.get("histories")
    if type(raw_cases) is not list or type(raw_histories) is not list:
        raise ValueError("embedded opening suite cases/histories are malformed")
    if raw_suite.get("count") != len(raw_cases):
        raise ValueError("embedded opening suite count does not match its cases")

    cases = tuple(
        OpeningCase(
            case_id=str(row["case_id"]),
            fen=str(row["fen"]),
            series_number=int(row["series_number"]),
            quiet_series=int(row.get("quiet_series", 0)),
            ep_targets=tuple(str(item) for item in row.get("ep_targets", ())),
            source=str(row["source"]),
        )
        for row in raw_cases
    )
    histories = tuple(
        SeededOpeningHistory(
            case_id=str(row["case_id"]),
            target_series=int(row["target_series"]),
            attempt=int(row["attempt"]),
            series=tuple(
                tuple(str(move) for move in moves)
                for moves in row["series"]
            ),
        )
        for row in raw_histories
    )
    suite = SeededOpeningSuite(
        version=str(raw_suite["version"]),
        seed=int(raw_suite["seed"]),
        min_series=int(raw_suite["min_series"]),
        max_series=int(raw_suite["max_series"]),
        max_frontier_states=int(raw_suite["max_frontier_states"]),
        cases=cases,
        histories=histories,
    )
    verify_seeded_opening_suite(suite)

    # The serialized boundaries are part of the evidence. Re-serialization
    # through the current referee catches altered PFENs and position hashes.
    for raw_case, case in zip(raw_cases, cases, strict=True):
        if _canonical_json(raw_case) != _canonical_json(case.as_dict()):
            raise ValueError(
                f"opening {case.case_id} disagrees with authoritative reconstruction"
            )
    if _canonical_json(raw_suite) != _canonical_json(suite.as_dict()):
        raise ValueError("embedded suite content does not match its declared version")
    configured_ids = report.get("config", {}).get("opening_case_ids")
    case_ids = [case.case_id for case in cases]
    if configured_ids != case_ids:
        raise ValueError("suite report opening_case_ids do not match the full suite")
    if report.get("config", {}).get("opening_suite_version") != suite.version:
        raise ValueError("suite report version does not match the embedded suite")

    metadata = {
        "path": str(report_path.resolve()),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "source_report_id": report.get("report_id"),
        "format": raw_suite["format"],
        "version": suite.version,
        "seed": suite.seed,
        "count": len(cases),
        "authoritative_history_replay": True,
        "unique_case_ids": len({case.case_id for case in cases}),
        "unique_position_hashes": len({case.state().position_hash for case in cases}),
    }
    return metadata, cases


def _referee_identity() -> dict[str, Any]:
    _activate_referee_source()
    import chess
    import scottish_progressive
    from scottish_progressive import rules
    from scottish_progressive.model import (
        ENGINE_SOURCE_FINGERPRINT,
        ENGINE_VERSION,
        RULESET_VERSION,
    )

    package_path = Path(scottish_progressive.__file__).resolve()
    rules_path = Path(rules.__file__).resolve()
    if not _path_is_within(package_path, REFEREE_SOURCE):
        raise RuntimeError(f"referee package escaped the checkout source: {package_path}")
    if not _path_is_within(rules_path, REFEREE_SOURCE):
        raise RuntimeError(f"referee rules escaped the checkout source: {rules_path}")
    return {
        "package_module": str(package_path),
        "rules_module": str(rules_path),
        "rules_module_sha256": _sha256(rules_path),
        "engine_source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "engine_version": ENGINE_VERSION,
        "ruleset_version": RULESET_VERSION,
        "python_version": sys.version.split()[0],
        "python_chess_version": chess.__version__,
    }


def _worker_ready_payload(package: Path, runtime: str) -> dict[str, Any]:
    import chess
    import scottish_progressive
    from scottish_progressive import _native_eval, evaluation
    from scottish_progressive.model import (
        ENGINE_SOURCE_FINGERPRINT,
        ENGINE_VERSION,
    )
    from scottish_progressive.native_subtree import native_subtree_available
    from scottish_progressive.profiles import baseline_profile

    module_path = Path(scottish_progressive.__file__).resolve()
    native_path = Path(_native_eval.__file__).resolve()
    expected_identity = evaluation._native_source_identity()  # noqa: SLF001
    actual_identity = getattr(_native_eval, "SOURCE_IDENTITY", None)
    if not _path_is_within(module_path, package):
        raise RuntimeError(
            f"{runtime} Python package escaped its package root: {module_path}"
        )
    if not _path_is_within(native_path, package):
        raise RuntimeError(
            f"{runtime} native module escaped its package root: {native_path}"
        )
    if actual_identity != expected_identity:
        raise RuntimeError(
            f"{runtime} native SOURCE_IDENTITY does not match packaged sources"
        )
    if not evaluation.native_acceleration_available():
        raise RuntimeError(f"{runtime} native evaluator is unavailable")
    if not native_subtree_available():
        raise RuntimeError(f"{runtime} native subtree search is unavailable")
    profile = baseline_profile()
    return {
        "type": "ready",
        "protocol": REPORT_FORMAT,
        "runtime": runtime,
        "package": str(package),
        "package_module": str(module_path),
        "native_module": str(native_path),
        "native_module_sha256": _sha256(native_path),
        "native_source_identity": str(actual_identity),
        "engine_source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "engine_version": ENGINE_VERSION,
        "engine_profile_id": profile.profile_id,
        "engine_profile_name": profile.name,
        "python_version": sys.version.split()[0],
        "python_chess_version": chess.__version__,
        "native_evaluator_available": True,
        "native_subtree_available": True,
    }


def _worker_analyze(request: Mapping[str, Any], ready: Mapping[str, Any]) -> dict[str, Any]:
    from scottish_progressive.profiles import baseline_profile
    from scottish_progressive.search import SearchLimits, analyze

    request_id = request.get("request_id")
    if type(request_id) is not int or request_id < 0:
        raise ValueError("request_id must be a nonnegative integer")
    if request.get("op") != "analyze":
        raise ValueError("unsupported worker operation")
    depth = request.get("depth")
    branch_cap = request.get("branch_cap")
    max_work = request.get("max_work")
    if type(depth) is not int or not 1 <= depth <= 8:
        raise ValueError("depth must be between 1 and 8")
    if type(branch_cap) is not int or not 1 <= branch_cap <= 512:
        raise ValueError("branch_cap must be between 1 and 512")
    if type(max_work) is not int or max_work < 1:
        raise ValueError("max_work must be positive")
    state = _state_from_payload(request.get("state", {}))
    result = analyze(
        state,
        SearchLimits(
            depth_series=depth,
            max_series_per_node=branch_cap,
            time_limit_seconds=None,
            max_generation_positions=max_work,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        baseline_profile(),
    )
    stats = asdict(result.stats)
    stats["work_positions"] = int(result.stats.work_positions)
    selected = result.best_series
    selected_payload = None
    if selected is not None:
        selected_payload = {
            "moves": list(selected.moves),
            "machine_notation": selected.machine_notation,
            "notation": selected.notation,
            "outcome": selected.outcome.value if selected.outcome else None,
            "ended_by_check": bool(selected.ended_by_check),
            "unused_moves": int(selected.unused_moves),
            "final_state": _state_payload(selected.final_state),
        }
    return {
        "type": "analysis",
        "request_id": request_id,
        "runtime": ready["runtime"],
        "native_source_identity": ready["native_source_identity"],
        "engine_source_fingerprint": result.source_fingerprint,
        "engine_profile_id": result.engine_profile_id,
        "score": int(result.score),
        "requested_depth": int(result.requested_depth),
        "completed_depth": int(result.completed_depth),
        "exact_width": bool(result.exact_width),
        "timed_out": bool(result.timed_out),
        "work_limit_reached": bool(result.work_limit_reached),
        "proof": result.proof,
        "adjudication_status": result.adjudication_status,
        "root_scores_complete": bool(result.root_scores_complete),
        "selected": selected_payload,
        "stats": stats,
    }


def _emit_jsonl(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(_canonical_json(payload) + "\n")
    sys.stdout.flush()


def _worker_main(package: Path, runtime: str) -> int:
    package = package.expanduser().resolve()
    if not package.is_dir():
        _emit_jsonl(
            {
                "type": "fatal",
                "runtime": runtime,
                "error": f"package directory is missing: {package}",
            }
        )
        return 2

    # Do not allow the benchmark checkout or an inherited PYTHONPATH to win
    # over the explicitly selected package-under-test.
    os.environ.pop("PYTHONPATH", None)
    referee = REFEREE_SOURCE.resolve()
    filtered: list[str] = []
    for entry in sys.path:
        candidate = Path(entry or os.getcwd())
        try:
            if candidate.resolve() == referee:
                continue
        except OSError:
            pass
        filtered.append(entry)
    sys.path[:] = [str(package), *filtered]
    try:
        ready = _worker_ready_payload(package, runtime)
    except BaseException as error:
        _emit_jsonl(
            {
                "type": "fatal",
                "runtime": runtime,
                "error": f"{type(error).__name__}: {error}",
            }
        )
        return 2
    _emit_jsonl(ready)

    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("op") == "shutdown":
                _emit_jsonl({"type": "bye", "runtime": runtime})
                return 0
            response = _worker_analyze(request, ready)
        except BaseException as error:
            response = {
                "type": "error",
                "request_id": (
                    request.get("request_id")
                    if isinstance(locals().get("request"), dict)
                    else None
                ),
                "runtime": runtime,
                "error": f"{type(error).__name__}: {error}",
            }
        _emit_jsonl(response)
    return 0


class JsonlWorker:
    """One persistent, source-pinned helper process."""

    def __init__(self, package: Path, runtime: str, ordinal: int) -> None:
        self.package = package.resolve()
        self.runtime = runtime
        self.ordinal = ordinal
        self._next_request_id = 0
        self._stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        self._process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--package",
                str(self.package),
                "--runtime",
                runtime,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=environment,
        )
        try:
            first = self._readline()
        except BaseException:
            self.close()
            raise
        if first.get("type") != "ready":
            self.close()
            raise RuntimeError(
                f"{runtime} helper {ordinal} failed startup: {first.get('error', first)}"
            )
        if first.get("protocol") != REPORT_FORMAT or first.get("runtime") != runtime:
            self.close()
            raise RuntimeError(f"{runtime} helper {ordinal} handshake drifted")
        self.ready = first

    def _stderr_text(self) -> str:
        try:
            self._stderr.flush()
            self._stderr.seek(0)
            return self._stderr.read()[-4000:]
        except (OSError, ValueError):
            return ""

    def _readline(self) -> dict[str, Any]:
        if self._process.stdout is None:
            raise RuntimeError("worker stdout is unavailable")
        line = self._process.stdout.readline()
        if not line:
            code = self._process.poll()
            stderr = self._stderr_text()
            raise RuntimeError(
                f"{self.runtime} helper exited (code={code}); stderr={stderr!r}"
            )
        payload = json.loads(line)
        if type(payload) is not dict:
            raise RuntimeError("worker returned a non-object JSONL message")
        return payload

    def analyze(
        self,
        state: Any,
        *,
        depth: int,
        branch_cap: int,
        max_work: int,
    ) -> dict[str, Any]:
        request_id = self._next_request_id
        self._next_request_id += 1
        request = {
            "request_id": request_id,
            "op": "analyze",
            "state": _state_payload(state),
            "depth": depth,
            "branch_cap": branch_cap,
            "max_work": max_work,
        }
        if self._process.stdin is None:
            raise RuntimeError("worker stdin is unavailable")
        self._process.stdin.write(_canonical_json(request) + "\n")
        self._process.stdin.flush()
        response = self._readline()
        if response.get("request_id") != request_id:
            raise WorkerIntegrityError("worker response request_id does not match")
        if response.get("type") == "error":
            raise RuntimeError(str(response.get("error", "worker error")))
        if response.get("type") != "analysis":
            raise WorkerIntegrityError("worker returned an unexpected response type")
        if response.get("runtime") != self.runtime:
            raise WorkerIntegrityError("worker response runtime identity drifted")
        if (
            response.get("native_source_identity")
            != self.ready["native_source_identity"]
        ):
            raise WorkerIntegrityError("worker native SOURCE_IDENTITY drifted")
        if (
            response.get("engine_source_fingerprint")
            != self.ready["engine_source_fingerprint"]
        ):
            raise WorkerIntegrityError("worker engine source fingerprint drifted")
        if response.get("engine_profile_id") != self.ready["engine_profile_id"]:
            raise WorkerIntegrityError("worker engine profile identity drifted")
        if response.get("requested_depth") != depth:
            raise WorkerIntegrityError("worker requested-depth receipt drifted")
        return response

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if process is None:
            return
        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.write('{"op":"shutdown"}\n')
                    process.stdin.flush()
                process.wait(timeout=5)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()
        self._stderr.close()


class WorkerPool:
    def __init__(self, package: Path, runtime: str, size: int) -> None:
        if size < 1:
            raise ValueError("worker pool size must be positive")
        self.package = package.resolve()
        self.runtime = runtime
        self._queue: queue.Queue[JsonlWorker] = queue.Queue()
        self._workers: list[JsonlWorker] = []
        self._lock = threading.Lock()
        try:
            for ordinal in range(size):
                worker = JsonlWorker(self.package, runtime, ordinal)
                self._workers.append(worker)
                self._queue.put(worker)
        except BaseException:
            self.close()
            raise
        self._canonical_ready = dict(self._workers[0].ready)
        try:
            self._validate_identities()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _invariant_fields() -> tuple[str, ...]:
        return (
            "package",
            "package_module",
            "native_module",
            "native_module_sha256",
            "native_source_identity",
            "engine_source_fingerprint",
            "engine_version",
            "engine_profile_id",
            "engine_profile_name",
            "python_version",
            "python_chess_version",
        )

    def _validate_identities(self) -> None:
        for worker in self._workers:
            if any(
                worker.ready[field] != self._canonical_ready[field]
                for field in self._invariant_fields()
            ):
                raise WorkerIntegrityError(
                    f"{self.runtime} helper identities disagree"
                )

    @property
    def ready(self) -> dict[str, Any]:
        self._validate_identities()
        return dict(self._canonical_ready)

    def analyze(self, state: Any, **limits: int) -> dict[str, Any]:
        worker = self._queue.get()
        replacement: JsonlWorker | None = None
        try:
            return worker.analyze(state, **limits)
        except BaseException as original_error:
            worker.close()
            try:
                replacement = JsonlWorker(
                    self.package,
                    self.runtime,
                    worker.ordinal,
                )
                if any(
                    replacement.ready[field] != self._canonical_ready[field]
                    for field in self._invariant_fields()
                ):
                    replacement.close()
                    raise WorkerIntegrityError(
                        f"{self.runtime} replacement helper identity drifted"
                    )
                with self._lock:
                    self._workers.remove(worker)
                    self._workers.append(replacement)
            except BaseException as replacement_error:
                # Put the failed worker back so future leases fail promptly
                # rather than deadlocking after the queue loses capacity.
                replacement = worker
                if isinstance(replacement_error, WorkerIntegrityError):
                    raise replacement_error from original_error
            raise original_error
        finally:
            self._queue.put(replacement or worker)

    def close(self) -> None:
        for worker in getattr(self, "_workers", ()):
            worker.close()
        self._workers.clear()


def _incomplete_game(
    job: Mapping[str, Any],
    state: Any,
    trace: Sequence[Mapping[str, Any]],
    reason: str,
    *,
    category: str,
    failing_runtime: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        **job,
        "result": "*",
        "terminal_reason": reason,
        "completion": "incomplete",
        "incomplete_category": category,
        "failing_runtime": failing_runtime,
        "error": error,
        "final_state": _state_payload(state),
        "series_played": sum(bool(row.get("played")) for row in trace),
        "trace": [dict(row) for row in trace],
    }


def _complete_game(
    job: Mapping[str, Any],
    state: Any,
    trace: Sequence[Mapping[str, Any]],
    result: str,
    reason: str,
) -> dict[str, Any]:
    return {
        **job,
        "result": result,
        "terminal_reason": reason,
        "completion": "complete",
        "incomplete_category": None,
        "failing_runtime": None,
        "error": None,
        "final_state": _state_payload(state),
        "series_played": sum(bool(row.get("played")) for row in trace),
        "trace": [dict(row) for row in trace],
    }


def _play_game(
    job: Mapping[str, Any],
    opening: Any,
    pools: Mapping[str, WorkerPool],
    *,
    depth: int,
    branch_cap: int,
    max_search_work: int,
    max_game_work: int,
    emergency_max_series: int | None,
) -> dict[str, Any]:
    import chess

    from scottish_progressive.model import Outcome
    from scottish_progressive.rules import play_series

    state = opening.state()
    trace: list[dict[str, Any]] = []
    game_work = 0
    while emergency_max_series is None or state.series_number <= emergency_max_series:
        mover = state.board.turn
        runtime = (
            job["white_runtime"] if mover == chess.WHITE else job["black_runtime"]
        )
        remaining = max_game_work - game_work
        if remaining <= 0:
            return _incomplete_game(
                job,
                state,
                trace,
                "technical-game-work-budget-exhausted",
                category="technical",
            )
        search_work_limit = min(max_search_work, remaining)
        try:
            response = pools[runtime].analyze(
                state,
                depth=depth,
                branch_cap=branch_cap,
                max_work=search_work_limit,
            )
        except BaseException as error:
            integrity_failure = isinstance(error, WorkerIntegrityError)
            return _incomplete_game(
                job,
                state,
                trace,
                (
                    "integrity-worker-identity-or-protocol"
                    if integrity_failure
                    else "technical-worker-error"
                ),
                category="integrity" if integrity_failure else "technical",
                failing_runtime=runtime,
                error=f"{type(error).__name__}: {error}",
            )

        stats = response.get("stats")
        work = stats.get("work_positions") if type(stats) is dict else None
        if type(work) is not int or not 0 <= work <= search_work_limit:
            return _incomplete_game(
                job,
                state,
                trace,
                "integrity-work-receipt-invalid",
                category="integrity",
                failing_runtime=runtime,
                error=f"work_positions={work!r}, limit={search_work_limit}",
            )
        game_work += work
        selected = response.get("selected")
        attempted = {
            "series_number": int(state.series_number),
            "mover": "white" if mover == chess.WHITE else "black",
            "runtime": runtime,
            "native_source_identity": response["native_source_identity"],
            "series": (
                selected.get("machine_notation")
                if type(selected) is dict
                else None
            ),
            "notation": selected.get("notation") if type(selected) is dict else None,
            "score_white_heuristic_points": response.get("score"),
            "requested_depth": response.get("requested_depth"),
            "completed_depth": response.get("completed_depth"),
            "exact_width": response.get("exact_width"),
            "root_scores_complete": response.get("root_scores_complete"),
            "work_limit_reached": response.get("work_limit_reached"),
            "search_work_limit": search_work_limit,
            "work_positions": work,
            "game_work_positions": game_work,
            "stats": stats,
            "played": False,
        }
        if (
            search_work_limit < max_search_work
            and response.get("work_limit_reached") is True
        ):
            trace.append(attempted)
            return _incomplete_game(
                job,
                state,
                trace,
                "technical-game-work-budget-exhausted",
                category="technical",
            )
        if response.get("timed_out") is not False:
            trace.append(attempted)
            return _incomplete_game(
                job,
                state,
                trace,
                "technical-unexpected-search-timeout",
                category="technical",
                failing_runtime=runtime,
            )
        adjudication = response.get("adjudication_status")
        if adjudication == "manual-proof-required":
            trace.append(attempted)
            return _incomplete_game(
                job,
                state,
                trace,
                "technical-manual-adjudication-pending",
                category="technical",
            )
        if selected is None:
            trace.append(attempted)
            if (
                response.get("proof") == "draw"
                and adjudication == "proven-draw-no-mating-material"
            ):
                return _complete_game(
                    job,
                    state,
                    trace,
                    "1/2-1/2",
                    "proven-draw-no-mating-material",
                )
            return _incomplete_game(
                job,
                state,
                trace,
                "technical-search-no-move",
                category="technical",
                failing_runtime=runtime,
            )
        moves = selected.get("moves") if type(selected) is dict else None
        if (
            type(moves) is not list
            or not moves
            or any(type(move) is not str for move in moves)
            or selected.get("machine_notation") != "/".join(moves)
        ):
            trace.append(attempted)
            return _incomplete_game(
                job,
                state,
                trace,
                "integrity-selected-series-malformed",
                category="integrity",
                failing_runtime=runtime,
            )
        try:
            replayed = play_series(state, tuple(moves))
        except BaseException as error:
            trace.append(attempted)
            return _incomplete_game(
                job,
                state,
                trace,
                "integrity-authoritative-replay-failed",
                category="integrity",
                failing_runtime=runtime,
                error=f"{type(error).__name__}: {error}",
            )
        replay_signature = {
            "outcome": replayed.outcome.value if replayed.outcome else None,
            "ended_by_check": bool(replayed.ended_by_check),
            "unused_moves": int(replayed.unused_moves),
            "final_state": _state_payload(replayed.final_state),
        }
        child_signature = {
            "outcome": selected.get("outcome"),
            "ended_by_check": selected.get("ended_by_check"),
            "unused_moves": selected.get("unused_moves"),
            "final_state": selected.get("final_state"),
        }
        if child_signature != replay_signature:
            trace.append(attempted)
            return _incomplete_game(
                job,
                state,
                trace,
                "integrity-child-replay-mismatch",
                category="integrity",
                failing_runtime=runtime,
                error=(
                    f"child={_canonical_json(child_signature)}; "
                    f"referee={_canonical_json(replay_signature)}"
                ),
            )

        attempted["played"] = True
        trace.append(attempted)
        state = replayed.final_state
        if replayed.outcome == Outcome.CHECKMATE:
            winner = mover if replayed.ended_by_check else not mover
            return _complete_game(
                job,
                state,
                trace,
                "1-0" if winner == chess.WHITE else "0-1",
                "checkmate",
            )
        if replayed.outcome in {Outcome.STALEMATE, Outcome.TEN_SERIES_DRAW}:
            return _complete_game(
                job,
                state,
                trace,
                "1/2-1/2",
                replayed.outcome.value,
            )
        if game_work >= max_game_work:
            return _incomplete_game(
                job,
                state,
                trace,
                "technical-game-work-budget-exhausted",
                category="technical",
            )

    return _incomplete_game(
        job,
        state,
        trace,
        "technical-emergency-series-watchdog-exhausted",
        category="technical",
    )


def exact_one_sided_sign_test(wins: int, losses: int) -> dict[str, Any]:
    """Exact P[X >= wins] for X~Binomial(wins+losses, 1/2)."""

    if type(wins) is not int or type(losses) is not int or wins < 0 or losses < 0:
        raise ValueError("sign-test wins and losses must be nonnegative integers")
    decisive = wins + losses
    if decisive == 0:
        probability = Fraction(1, 1)
    else:
        numerator = sum(math.comb(decisive, value) for value in range(wins, decisive + 1))
        probability = Fraction(numerator, 1 << decisive)
    return {
        "method": "exact one-sided binomial sign test",
        "null_pair_win_probability": 0.5,
        "alternative": "candidate pair-win probability is greater than 0.5",
        "pair_wins": wins,
        "pair_losses": losses,
        "decisive_pairs": decisive,
        "tail": "P[X >= observed pair wins]",
        "exact_fraction": f"{probability.numerator}/{probability.denominator}",
        "p_value": float(probability),
    }


def _candidate_points(game: Mapping[str, Any]) -> float | None:
    if game.get("completion") != "complete":
        return None
    result = game.get("result")
    if result == "1/2-1/2":
        return 0.5
    candidate_is_white = game.get("candidate_color") == "white"
    if result == "1-0":
        return 1.0 if candidate_is_white else 0.0
    if result == "0-1":
        return 0.0 if candidate_is_white else 1.0
    return None


def _is_exact_color_swap(pair_games: Sequence[Mapping[str, Any]]) -> bool:
    if len(pair_games) != 2:
        return False
    first, second = sorted(pair_games, key=lambda row: int(row["swap_index"]))
    return bool(
        first.get("swap_index") == 0
        and second.get("swap_index") == 1
        and first.get("candidate_color") == "white"
        and second.get("candidate_color") == "black"
        and first.get("white_runtime") == "candidate"
        and first.get("black_runtime") == "baseline"
        and second.get("white_runtime") == "baseline"
        and second.get("black_runtime") == "candidate"
        and first.get("opening_case_id") == second.get("opening_case_id")
        and first.get("opening_position_hash")
        == second.get("opening_position_hash")
        and first.get("opening_state") == second.get("opening_state")
    )


def summarize_games(games: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    completed = [game for game in games if _candidate_points(game) is not None]
    points = [_candidate_points(game) for game in completed]
    wins = sum(value == 1.0 for value in points)
    draws = sum(value == 0.5 for value in points)
    losses = sum(value == 0.0 for value in points)
    incomplete_reasons: dict[str, int] = {}
    technical = integrity = 0
    by_runtime = {"candidate": 0, "baseline": 0, "unattributed": 0}
    for game in games:
        if _candidate_points(game) is not None:
            continue
        reason = str(game.get("terminal_reason", "unknown"))
        incomplete_reasons[reason] = incomplete_reasons.get(reason, 0) + 1
        category = game.get("incomplete_category")
        technical += int(category == "technical")
        integrity += int(category == "integrity")
        failing = game.get("failing_runtime")
        key = failing if failing in {"candidate", "baseline"} else "unattributed"
        by_runtime[key] += 1

    pair_rows: list[dict[str, Any]] = []
    pair_wins = pair_draws = pair_losses = incomplete_pairs = 0
    pair_integrity_failures = 0
    by_pair: dict[int, list[Mapping[str, Any]]] = {}
    for game in games:
        by_pair.setdefault(int(game["pair_index"]), []).append(game)
    for pair_index in sorted(by_pair):
        pair_games = sorted(by_pair[pair_index], key=lambda row: int(row["swap_index"]))
        pair_points = [_candidate_points(game) for game in pair_games]
        exact_color_swap = _is_exact_color_swap(pair_games)
        pair_integrity_failures += int(not exact_color_swap)
        if not exact_color_swap or any(value is None for value in pair_points):
            result = "incomplete"
            total = None
            incomplete_pairs += 1
        else:
            total = sum(value for value in pair_points if value is not None)
            if total > 1.0:
                result = "win"
                pair_wins += 1
            elif total == 1.0:
                result = "draw"
                pair_draws += 1
            else:
                result = "loss"
                pair_losses += 1
        pair_rows.append(
            {
                "pair_index": pair_index,
                "opening_case_id": pair_games[0]["opening_case_id"],
                "candidate_points": total,
                "result": result,
                "exact_color_swap": exact_color_swap,
                "game_indices": [int(game["game_index"]) for game in pair_games],
            }
        )

    completed_count = len(completed)
    completed_pairs = pair_wins + pair_draws + pair_losses
    game_score = sum(value for value in points if value is not None) / completed_count if completed_count else None
    pair_score = (
        (pair_wins + pair_draws * 0.5) / completed_pairs
        if completed_pairs
        else None
    )
    sign_test = exact_one_sided_sign_test(pair_wins, pair_losses)
    all_games_complete = completed_count == len(games)
    all_pairs_complete = incomplete_pairs == 0
    exact_color_swaps = pair_integrity_failures == 0
    non_regression = bool(
        all_games_complete
        and all_pairs_complete
        and exact_color_swaps
        and game_score is not None
        and game_score >= 0.5
        and pair_score is not None
        and pair_score >= 0.5
    )
    stronger = bool(
        non_regression
        and pair_wins - pair_losses >= 10
        and sign_test["decisive_pairs"] >= 20
        and sign_test["p_value"] <= 0.05
    )
    return {
        "scheduled_games": len(games),
        "completed_games": completed_count,
        "incomplete_games": len(games) - completed_count,
        "candidate_game_wdl": {"wins": wins, "draws": draws, "losses": losses},
        "candidate_game_points": sum(value for value in points if value is not None),
        "candidate_game_score_rate": game_score,
        "scheduled_pairs": len(by_pair),
        "completed_pairs": completed_pairs,
        "incomplete_pairs": incomplete_pairs,
        "pair_integrity_failures": pair_integrity_failures,
        "candidate_pair_wdl": {
            "wins": pair_wins,
            "draws": pair_draws,
            "losses": pair_losses,
        },
        "candidate_pair_score_rate": pair_score,
        "technical_incomplete_accounting": {
            "technical": technical,
            "integrity": integrity,
            "by_runtime": by_runtime,
            "by_reason": dict(sorted(incomplete_reasons.items())),
        },
        "sign_test": sign_test,
        "acceptance": {
            "all_games_complete": all_games_complete,
            "all_pairs_complete": all_pairs_complete,
            "all_pairs_are_exact_color_swaps": exact_color_swaps,
            "required_completed_games": len(games),
            "game_score_at_least_half": game_score is not None and game_score >= 0.5,
            "pair_score_at_least_half": pair_score is not None and pair_score >= 0.5,
            "non_regression_passed": non_regression,
            "stronger_claim_requirements": {
                "pair_win_minus_loss_at_least_10": pair_wins - pair_losses >= 10,
                "at_least_20_decisive_pairs": sign_test["decisive_pairs"] >= 20,
                "one_sided_exact_sign_p_at_most_0_05": sign_test["p_value"] <= 0.05,
            },
            "stronger_claim_passed": stronger,
        },
        "pairs": pair_rows,
    }


def _stable_match_id(configuration: Mapping[str, Any], identities: Mapping[str, Any]) -> str:
    encoded = _canonical_json(
        {"configuration": configuration, "identities": identities}
    ).encode("utf-8")
    return "cycle7-binary-" + hashlib.sha256(encoded).hexdigest()[:20]


def run_gate(args: argparse.Namespace) -> dict[str, Any]:
    _activate_referee_source()
    referee = _referee_identity()
    baseline_package = args.baseline_package.expanduser().resolve()
    candidate_package = args.candidate_package.expanduser().resolve()
    suite_report = args.suite_report.expanduser().resolve()
    for label, path in (
        ("baseline", baseline_package),
        ("candidate", candidate_package),
    ):
        if not path.is_dir():
            raise ValueError(f"{label} package directory is missing: {path}")
    if not suite_report.is_file():
        raise ValueError(f"suite report is missing: {suite_report}")
    suite_metadata, all_openings = _load_verified_openings(suite_report)
    if not 1 <= args.pairs <= len(all_openings):
        raise ValueError(
            f"pairs must be between 1 and the suite size ({len(all_openings)})"
        )

    ordered = list(all_openings)
    random.Random(args.match_seed).shuffle(ordered)
    openings = tuple(ordered[: args.pairs])
    baseline_helpers = max(1, args.workers // 2)
    candidate_helpers = max(1, args.workers - baseline_helpers)
    pools: dict[str, WorkerPool] = {}
    try:
        pools["baseline"] = WorkerPool(
            baseline_package, "baseline", baseline_helpers
        )
        pools["candidate"] = WorkerPool(
            candidate_package, "candidate", candidate_helpers
        )
        identities = {
            runtime: {
                **pool.ready,
                "helper_processes": (
                    baseline_helpers if runtime == "baseline" else candidate_helpers
                ),
            }
            for runtime, pool in pools.items()
        }
        jobs: list[tuple[dict[str, Any], Any]] = []
        for pair_index, opening in enumerate(openings):
            for swap_index, candidate_color in enumerate(("white", "black")):
                white_runtime = "candidate" if candidate_color == "white" else "baseline"
                black_runtime = "baseline" if candidate_color == "white" else "candidate"
                game_index = pair_index * 2 + swap_index
                jobs.append(
                    (
                        {
                            "game_index": game_index,
                            "pair_index": pair_index,
                            "swap_index": swap_index,
                            "opening_case_id": opening.case_id,
                            "opening_position_hash": opening.state().position_hash,
                            "opening_state": _state_payload(opening.state()),
                            "candidate_color": candidate_color,
                            "white_runtime": white_runtime,
                            "black_runtime": black_runtime,
                        },
                        opening,
                    )
                )
        game_results: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(args.workers, len(jobs))) as executor:
            futures = {
                executor.submit(
                    _play_game,
                    job,
                    opening,
                    pools,
                    depth=args.depth,
                    branch_cap=args.branch_cap,
                    max_search_work=args.max_search_work,
                    max_game_work=args.max_game_work,
                    emergency_max_series=args.emergency_max_series,
                ): int(job["game_index"])
                for job, opening in jobs
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    game_results[index] = future.result()
                except BaseException as error:
                    job = jobs[index][0]
                    game_results[index] = _incomplete_game(
                        job,
                        jobs[index][1].state(),
                        (),
                        "technical-referee-exception",
                        category="technical",
                        error=f"{type(error).__name__}: {error}",
                    )
        games = [game_results[index] for index in range(len(jobs))]
    finally:
        for pool in pools.values():
            pool.close()

    summary = summarize_games(games)
    configuration = {
        "pairs": args.pairs,
        "games": args.pairs * 2,
        "match_seed": args.match_seed,
        "opening_order": "Python Random(match_seed) shuffle without replacement",
        "depth_series": args.depth,
        "branch_cap_complete_series_per_node": args.branch_cap,
        "max_work_positions_per_search": args.max_search_work,
        "max_game_work_positions": args.max_game_work,
        "emergency_max_series": args.emergency_max_series,
        "time_limit_seconds": None,
        "native_threads_per_search": 1,
        "fresh_searcher_each_series": True,
        "collect_all_root_scores": False,
        "parent_authoritative_rules_replay": True,
        "worker_transport": "persistent package-specific JSONL helpers",
        "requested_workers": args.workers,
        "helper_processes": baseline_helpers + candidate_helpers,
    }
    identity_projection = {
        runtime: {
            key: value
            for key, value in identity.items()
            if key
            in {
                "native_source_identity",
                "engine_source_fingerprint",
                "native_module_sha256",
            }
        }
        for runtime, identity in identities.items()
    }
    identity_projection["referee"] = {
        "engine_source_fingerprint": referee["engine_source_fingerprint"],
        "rules_module_sha256": referee["rules_module_sha256"],
        "ruleset_version": referee["ruleset_version"],
    }
    identity_projection["suite"] = {
        "sha256": suite_metadata["sha256"],
        "version": suite_metadata["version"],
        "selected_case_ids": [opening.case_id for opening in openings],
    }
    return {
        "format": REPORT_FORMAT,
        "report_id": _stable_match_id(configuration, identity_projection),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "configuration": configuration,
        "referee": referee,
        "suite": suite_metadata,
        "selected_openings": [opening.as_dict() for opening in openings],
        "runtimes": identities,
        "summary": {key: value for key, value in summary.items() if key != "pairs"},
        "pairs": summary["pairs"],
        "games": games,
        "claim_scope": {
            "fixed_suite_only": True,
            "non_regression": (
                "requires every scheduled game complete and both game and paired "
                "candidate score rates at least 0.500"
            ),
            "stronger": (
                "additionally requires pair wins minus losses at least 10, at "
                "least 20 decisive pairs, and one-sided exact sign p <= 0.05"
            ),
            "stockfish_comparison": (
                "This gate does not establish Stockfish-level strength."
            ),
        },
    }


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
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


def compact_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Retains release evidence while keeping per-series traces out of Git."""

    games = payload.get("games")
    if not isinstance(games, list):
        raise ValueError("full strength report has no games array")
    compact = {key: value for key, value in payload.items() if key != "games"}
    compact["game_receipt"] = {
        "count": len(games),
        "canonical_sha256": hashlib.sha256(
            _canonical_json(games).encode("utf-8")
        ).hexdigest(),
        "full_traces_committed": False,
    }
    return compact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a deterministic, color-swapped strength gate between two "
            "source-pinned package directories."
        )
    )
    parser.add_argument("--baseline-package", type=Path)
    parser.add_argument("--candidate-package", type=Path)
    parser.add_argument("--suite-report", type=Path, default=DEFAULT_SUITE_REPORT)
    parser.add_argument("--pairs", type=int, default=100)
    parser.add_argument("--match-seed", type=int, default=2026082607)
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--branch-cap", type=int, default=32)
    parser.add_argument("--max-search-work", type=int, default=250_000)
    parser.add_argument("--max-game-work", type=int, default=5_000_000)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--emergency-max-series", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--package", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--runtime", choices=("baseline", "candidate"), help=argparse.SUPPRESS)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.worker:
        if args.package is None or args.runtime is None:
            raise ValueError("--worker requires --package and --runtime")
        return
    if args.baseline_package is None or args.candidate_package is None:
        raise ValueError("--baseline-package and --candidate-package are required")
    if args.output is None:
        raise ValueError("--output is required")
    if args.pairs < 1:
        raise ValueError("--pairs must be positive")
    if not 1 <= args.depth <= 8:
        raise ValueError("--depth must be between 1 and 8")
    if not 1 <= args.branch_cap <= 512:
        raise ValueError("--branch-cap must be between 1 and 512")
    if args.max_search_work < 1_000:
        raise ValueError("--max-search-work must be at least 1000")
    if args.max_game_work < 1_000:
        raise ValueError("--max-game-work must be at least 1000")
    if args.max_game_work < args.max_search_work:
        raise ValueError("--max-game-work cannot be below --max-search-work")
    if not 1 <= args.workers <= 64:
        raise ValueError("--workers must be between 1 and 64")
    if args.emergency_max_series is not None and args.emergency_max_series < 18:
        raise ValueError("--emergency-max-series must be at least 18")


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        _validate_args(args)
        if args.worker:
            return _worker_main(args.package, args.runtime)
        payload = run_gate(args)
        _write_atomic(args.output, payload)
        if args.summary_output is not None:
            _write_atomic(args.summary_output, compact_report(payload))
        print(json.dumps(payload["summary"], indent=2, sort_keys=True))
        return 0 if payload["summary"]["acceptance"]["non_regression_passed"] else 2
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
