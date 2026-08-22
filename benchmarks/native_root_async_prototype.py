from __future__ import annotations

import argparse
from collections import deque
import ctypes
from dataclasses import asdict, dataclass
import json
import multiprocessing
import os
import queue
import threading
import time
from typing import Any

import chess

import scottish_progressive.search as search_module
from scottish_progressive.model import ProgressiveState, SeriesResult
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.search import MATE_SCORE, SearchLimits, SeriesSearcher


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: int
    machine: str
    fen: str
    series_number: int
    quiet_series: int
    ep_targets: tuple[int, ...]

    @classmethod
    def from_result(cls, candidate_id: int, result: SeriesResult) -> Candidate:
        state = result.final_state
        return cls(
            candidate_id,
            result.machine_notation,
            state.board.fen(en_passant="fen"),
            state.series_number,
            state.quiet_series,
            state.ep_targets,
        )

    def state(self) -> ProgressiveState:
        return ProgressiveState.from_fen(
            self.fen,
            self.series_number,
            quiet_series=self.quiet_series,
            ep_targets=self.ep_targets,
        )


@dataclass(frozen=True, slots=True)
class ExactCandidate:
    candidate: Candidate
    score: int
    pv: tuple[str, ...]
    proof_bounds: tuple[int, int]


class _ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("page_fault_count", ctypes.c_ulong),
        ("peak_working_set_size", ctypes.c_size_t),
        ("working_set_size", ctypes.c_size_t),
        ("quota_peak_paged_pool_usage", ctypes.c_size_t),
        ("quota_paged_pool_usage", ctypes.c_size_t),
        ("quota_peak_nonpaged_pool_usage", ctypes.c_size_t),
        ("quota_nonpaged_pool_usage", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
        ("private_usage", ctypes.c_size_t),
    ]


def _working_set_bytes(pid: int) -> int:
    if os.name != "nt":
        return 0
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        return 0
    try:
        counters = _ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            handle,
            ctypes.byref(counters),
            counters.cb,
        ):
            return 0
        return int(counters.working_set_size)
    finally:
        kernel32.CloseHandle(handle)


def _limits(
    *,
    depth: int,
    width: int,
    max_work: int,
    native_threads: int,
    time_limit_seconds: float,
) -> SearchLimits:
    return SearchLimits(
        depth_series=depth,
        max_series_per_node=width,
        max_generation_positions=max_work,
        time_limit_seconds=time_limit_seconds,
        collect_all_root_scores=False,
        native_threads=native_threads,
    )


def _stats_delta(
    before: dict[str, int],
    after: dict[str, int],
) -> dict[str, int]:
    peak_fields = {
        "peak_frontier_states",
        "series_generation_cache_peak",
        "series_generation_cache_entries_peak",
    }
    return {
        key: (after[key] if key in peak_fields else after[key] - value)
        for key, value in before.items()
    }


def _worker_loop(
    worker_id: int,
    command_queue: Any,
    result_queue: Any,
    *,
    depth: int,
    width: int,
    work_budget: int,
    cache_capacity: int,
    native_threads: int,
    common_deadline: float,
    time_limit_seconds: float,
) -> None:
    try:
        search_module.SERIES_GENERATION_CACHE_CAPACITY = cache_capacity
        root = ProgressiveState.initial()
        searcher = SeriesSearcher(
            _limits(
                depth=depth,
                width=width,
                max_work=work_budget,
                native_threads=native_threads,
                time_limit_seconds=time_limit_seconds,
            ),
            baseline_profile(),
        )
        searcher._deadline = common_deadline
        searcher._tactical_frontier_protection_enabled(root, ply_from_root=1)
        searcher._start_native_subtree(root)
        if searcher._native_subtree_session is None:
            raise RuntimeError("native descendant session is unavailable")
    except Exception as error:
        result_queue.put(
            {
                "kind": "ready",
                "worker_id": worker_id,
                "pid": os.getpid(),
                "status": "UNKNOWN",
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
        return

    result_queue.put(
        {
            "kind": "ready",
            "worker_id": worker_id,
            "pid": os.getpid(),
            "status": "COMPLETE",
        }
    )
    while True:
        command = command_queue.get()
        if command["kind"] == "shutdown":
            result_queue.put(
                {
                    "kind": "shutdown",
                    "worker_id": worker_id,
                    "pid": os.getpid(),
                    "status": "COMPLETE",
                    "work": searcher.stats.work_positions,
                    "stats": asdict(searcher.stats),
                }
            )
            return
        if command["kind"] != "search":
            result_queue.put(
                {
                    "kind": "result",
                    "worker_id": worker_id,
                    "status": "UNKNOWN",
                    "error_type": "ProtocolError",
                    "error": f"unknown command {command['kind']!r}",
                }
            )
            continue

        candidate = Candidate(**command["candidate"])
        before_stats = asdict(searcher.stats)
        before_work = searcher.stats.work_positions
        started = time.perf_counter()
        response: dict[str, object] = {
            "kind": "result",
            "worker_id": worker_id,
            "pid": os.getpid(),
            "candidate_id": candidate.candidate_id,
            "candidate": candidate.machine,
            "mode": command["mode"],
            "incumbent_epoch": command["incumbent_epoch"],
            "alpha_snapshot": command["alpha_snapshot"],
            "beta_snapshot": command["beta_snapshot"],
        }
        try:
            if time.perf_counter() >= common_deadline:
                raise TimeoutError("common prototype deadline expired")
            alpha = int(command["alpha_snapshot"])
            beta = int(command["beta_snapshot"])
            if command["mode"] == "FULL":
                score, pv, proof_bounds = searcher._minimax(
                    candidate.state(),
                    depth - 1,
                    -MATE_SCORE * 2,
                    MATE_SCORE * 2,
                    1,
                )
                bound_kind = "EXACT"
            elif command["mode"] == "PVS":
                score, pv, proof_bounds = searcher._search_root_child_with_pvs(
                    candidate.state(),
                    depth - 1,
                    alpha,
                    beta,
                    1,
                    parent_mover=chess.WHITE,
                    has_prior_child=True,
                )
                if score <= alpha:
                    bound_kind = "UPPER"
                elif score >= beta:
                    bound_kind = "LOWER"
                else:
                    bound_kind = "EXACT"
                if (
                    bound_kind == "UPPER"
                    and score == alpha
                    and candidate.machine < command["incumbent_machine"]
                ):
                    score, pv, proof_bounds = searcher._minimax(
                        candidate.state(),
                        depth - 1,
                        -MATE_SCORE * 2,
                        MATE_SCORE * 2,
                        1,
                    )
                    bound_kind = "EXACT"
            else:
                raise RuntimeError(f"unknown search mode {command['mode']!r}")
            response.update(
                {
                    "status": "COMPLETE",
                    "bound_kind": bound_kind,
                    "score": score,
                    "pv": tuple(item.machine_notation for item in pv),
                    "proof_bounds": proof_bounds,
                }
            )
        except Exception as error:
            response.update(
                {
                    "status": "UNKNOWN",
                    "bound_kind": "UNKNOWN",
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
        after_stats = asdict(searcher.stats)
        response.update(
            {
                "seconds": time.perf_counter() - started,
                "work_delta": searcher.stats.work_positions - before_work,
                "work_cumulative": searcher.stats.work_positions,
                "stats_delta": _stats_delta(before_stats, after_stats),
            }
        )
        result_queue.put(response)


def _better(
    left: ExactCandidate,
    right: ExactCandidate,
    mover: chess.Color,
) -> bool:
    if left.score != right.score:
        return left.score > right.score if mover == chess.WHITE else left.score < right.score
    return left.candidate.machine < right.candidate.machine


def _exact_from_result(
    result: dict[str, object],
    candidates: dict[int, Candidate],
) -> ExactCandidate:
    candidate_id = int(result["candidate_id"])
    return ExactCandidate(
        candidates[candidate_id],
        int(result["score"]),
        tuple(result["pv"]),
        tuple(result["proof_bounds"]),
    )


def _get_message(
    result_queue: Any,
    processes: list[Any],
    common_deadline: float,
) -> dict[str, object]:
    while True:
        remaining = common_deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("common prototype deadline expired")
        try:
            return result_queue.get(timeout=min(1.0, remaining))
        except queue.Empty:
            dead = [process.pid for process in processes if not process.is_alive()]
            if dead:
                raise RuntimeError(f"worker process exited unexpectedly: {dead}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start-only asynchronous native root-worker feasibility run."
    )
    parser.add_argument("--depth", type=int, choices=(5,), default=5)
    parser.add_argument("--width", type=int, default=32)
    parser.add_argument("--workers", type=int, choices=(4, 8), default=4)
    parser.add_argument("--threads-per-worker", type=int, default=4)
    parser.add_argument("--max-work", type=int, default=100_000_000)
    parser.add_argument("--cache-capacity", type=int, default=65_536)
    parser.add_argument("--time-limit", type=float, default=300.0)
    parser.add_argument(
        "--initial-full-wave",
        type=int,
        default=4,
        help="number of exact full-window root seeds dispatched before scouts",
    )
    parser.add_argument(
        "--stream-first-wave",
        action="store_true",
        help=(
            "reuse each completed first-wave worker immediately instead of "
            "waiting for the other exact seeds"
        ),
    )
    args = parser.parse_args()
    if not 1 <= args.initial_full_wave <= args.workers:
        raise SystemExit("initial-full-wave must be between 1 and workers")

    session_count = args.workers + 1
    worker_work_budget = args.max_work // session_count
    coordinator_work_budget = args.max_work - (
        worker_work_budget * args.workers
    )
    if worker_work_budget < 1:
        raise SystemExit("max-work must provide at least one unit per session")

    overall_started = time.perf_counter()
    common_deadline = overall_started + args.time_limit
    search_module.SERIES_GENERATION_CACHE_CAPACITY = args.cache_capacity
    root = ProgressiveState.initial()
    coordinator = SeriesSearcher(
        _limits(
            depth=args.depth,
            width=args.width,
            max_work=coordinator_work_budget,
            native_threads=1,
            time_limit_seconds=args.time_limit,
        ),
        baseline_profile(),
    )
    coordinator._deadline = common_deadline
    generated = coordinator._ordered_generated(
        root,
        ply_from_root=1,
        reserve_positions=root.moves_available,
        preferred_series="e2e3",
    )
    materialized = tuple(
        coordinator._materialize_series(candidate) for candidate in generated
    )
    candidates = tuple(
        Candidate.from_result(candidate_id, result)
        for candidate_id, result in enumerate(materialized)
    )
    expected_first_wave = ("e2e3", "d2d3", "d2d4", "e2e4")
    actual_first_wave = tuple(candidate.machine for candidate in candidates[:4])
    if actual_first_wave != expected_first_wave:
        raise RuntimeError(
            "production preferred-root order mismatch: "
            f"expected {expected_first_wave!r}, got {actual_first_wave!r}"
        )
    candidate_by_id = {
        candidate.candidate_id: candidate for candidate in candidates
    }

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    command_queues = [context.Queue() for _ in range(args.workers)]
    processes = [
        context.Process(
            target=_worker_loop,
            args=(worker_id, command_queues[worker_id], result_queue),
            kwargs={
                "depth": args.depth,
                "width": args.width,
                "work_budget": worker_work_budget,
                "cache_capacity": args.cache_capacity,
                "native_threads": args.threads_per_worker,
                "common_deadline": common_deadline,
                "time_limit_seconds": args.time_limit,
            },
        )
        for worker_id in range(args.workers)
    ]
    for process in processes:
        process.start()

    memory_stop = threading.Event()
    peak_total_rss = [0]
    peak_rss_by_pid: dict[int, int] = {}
    worker_pids: dict[int, int] = {}
    startup_started = time.perf_counter()
    task_receipts: list[dict[str, object]] = []
    dispatch_sequence: list[dict[str, object]] = []
    completion_sequence: list[dict[str, object]] = []
    final_worker_receipts: dict[int, dict[str, object]] = {}
    completed_candidate_ids: set[int] = set()
    failure: BaseException | None = None

    try:
        while len(worker_pids) < args.workers:
            message = _get_message(result_queue, processes, common_deadline)
            if message.get("kind") != "ready":
                raise RuntimeError(f"unexpected startup message: {message!r}")
            worker_id = int(message["worker_id"])
            if worker_id in worker_pids:
                raise RuntimeError(f"duplicate ready worker {worker_id}")
            if message.get("status") != "COMPLETE":
                raise RuntimeError(f"worker {worker_id} failed: {message!r}")
            worker_pids[worker_id] = int(message["pid"])
        worker_startup_seconds = time.perf_counter() - startup_started

        def sample_memory() -> None:
            monitored_pids = (os.getpid(),) + tuple(worker_pids.values())
            while not memory_stop.wait(0.02):
                samples = {
                    pid: _working_set_bytes(pid) for pid in monitored_pids
                }
                peak_total_rss[0] = max(
                    peak_total_rss[0],
                    sum(samples.values()),
                )
                for pid, rss in samples.items():
                    peak_rss_by_pid[pid] = max(
                        peak_rss_by_pid.get(pid, 0),
                        rss,
                    )

        memory_thread = threading.Thread(target=sample_memory, daemon=True)
        memory_thread.start()

        in_flight: dict[int, dict[str, object]] = {}
        dispatch_counter = 0

        def dispatch(
            worker_id: int,
            candidate: Candidate,
            *,
            mode: str,
            incumbent_epoch: int,
            incumbent: ExactCandidate | None,
        ) -> None:
            nonlocal dispatch_counter
            alpha = -MATE_SCORE * 2 if incumbent is None else incumbent.score
            incumbent_machine = "" if incumbent is None else incumbent.candidate.machine
            command = {
                "kind": "search",
                "mode": mode,
                "candidate": asdict(candidate),
                "incumbent_epoch": incumbent_epoch,
                "incumbent_machine": incumbent_machine,
                "alpha_snapshot": alpha,
                "beta_snapshot": MATE_SCORE * 2,
            }
            if worker_id in in_flight:
                raise RuntimeError(f"worker {worker_id} already has an in-flight task")
            in_flight[worker_id] = command
            dispatch_counter += 1
            dispatch_sequence.append(
                {
                    "sequence": dispatch_counter,
                    "worker_id": worker_id,
                    "candidate_id": candidate.candidate_id,
                    "candidate": candidate.machine,
                    "mode": mode,
                    "incumbent_epoch": incumbent_epoch,
                    "alpha_snapshot": alpha,
                }
            )
            command_queues[worker_id].put(command)

        search_started = time.perf_counter()
        for worker_id, candidate in enumerate(
            candidates[: args.initial_full_wave]
        ):
            dispatch(
                worker_id,
                candidate,
                mode="FULL",
                incumbent_epoch=0,
                incumbent=None,
            )

        first_wave_candidate_ids = {
            candidate.candidate_id
            for candidate in candidates[: args.initial_full_wave]
        }
        first_wave_results: list[dict[str, object]] = []
        completion_counter = 0
        incumbent: ExactCandidate | None = None
        incumbent_epoch = 0
        remaining = deque(candidates[args.initial_full_wave :])
        first_wave_seconds: float | None = None
        dynamic_started: float | None = None

        while in_flight or remaining:
            result = _get_message(result_queue, processes, common_deadline)
            if result.get("kind") != "result":
                raise RuntimeError(f"unexpected root-worker message: {result!r}")
            worker_id = int(result["worker_id"])
            expected = in_flight.pop(worker_id, None)
            if expected is None:
                raise RuntimeError(f"unsolicited result from worker {worker_id}")
            if result.get("status") != "COMPLETE":
                raise RuntimeError(f"worker result is unknown: {result!r}")
            candidate_id = int(result["candidate_id"])
            expected_id = int(expected["candidate"]["candidate_id"])
            if candidate_id != expected_id:
                raise RuntimeError(
                    "candidate identity mismatch: "
                    f"expected {expected_id}, got {candidate_id}"
                )
            if result["candidate"] != expected["candidate"]["machine"]:
                raise RuntimeError(f"candidate notation mismatch: {result!r}")
            if result["mode"] != expected["mode"]:
                raise RuntimeError(f"search mode mismatch: {result!r}")
            if candidate_id in completed_candidate_ids:
                raise RuntimeError(f"duplicate candidate result {candidate_id}")
            completed_candidate_ids.add(candidate_id)
            response_epoch = int(result["incumbent_epoch"])
            expected_epoch = int(expected["incumbent_epoch"])
            if response_epoch != expected_epoch or response_epoch > incumbent_epoch:
                raise RuntimeError(f"invalid incumbent epoch: {result!r}")
            alpha_snapshot = int(result["alpha_snapshot"])
            if alpha_snapshot != int(expected["alpha_snapshot"]):
                raise RuntimeError(f"alpha snapshot mismatch: {result!r}")
            if int(result["beta_snapshot"]) != int(expected["beta_snapshot"]):
                raise RuntimeError(f"beta snapshot mismatch: {result!r}")
            bound_kind = str(result["bound_kind"])
            score = int(result["score"])
            is_first_wave = candidate_id in first_wave_candidate_ids
            if is_first_wave:
                if expected["mode"] != "FULL" or bound_kind != "EXACT":
                    raise RuntimeError(
                        f"first-wave result is not exact: {result!r}"
                    )
                if response_epoch != 0:
                    raise RuntimeError(f"first-wave epoch mismatch: {result!r}")
                first_wave_results.append(result)
                if args.stream_first_wave:
                    exact = _exact_from_result(result, candidate_by_id)
                    if incumbent is None or _better(exact, incumbent, chess.WHITE):
                        incumbent = exact
                        incumbent_epoch += 1
            elif bound_kind == "UPPER":
                if incumbent is None:
                    raise RuntimeError("PVS result arrived before an exact incumbent")
                if score > alpha_snapshot or alpha_snapshot > incumbent.score:
                    raise RuntimeError(f"invalid stale upper bound: {result!r}")
                snapshot_machine = str(expected["incumbent_machine"])
                if score == alpha_snapshot and result["candidate"] < snapshot_machine:
                    raise RuntimeError(f"canonical tie was not re-searched: {result!r}")
            elif bound_kind == "LOWER":
                raise RuntimeError(f"unresolved lower-bound root result: {result!r}")
            elif bound_kind == "EXACT":
                exact = _exact_from_result(result, candidate_by_id)
                if incumbent is None or _better(exact, incumbent, chess.WHITE):
                    incumbent = exact
                    incumbent_epoch += 1
            else:
                raise RuntimeError(f"unknown bound kind: {result!r}")

            if (
                len(first_wave_results) == args.initial_full_wave
                and first_wave_seconds is None
            ):
                first_wave_seconds = time.perf_counter() - search_started
                if not args.stream_first_wave:
                    exact_first_wave = [
                        _exact_from_result(item, candidate_by_id)
                        for item in sorted(
                            first_wave_results,
                            key=lambda item: int(item["candidate_id"]),
                        )
                    ]
                    incumbent = exact_first_wave[0]
                    for exact in exact_first_wave[1:]:
                        if _better(exact, incumbent, chess.WHITE):
                            incumbent = exact
                    incumbent_epoch = 1

            completion_counter += 1
            completion_sequence.append(
                {
                    "sequence": completion_counter,
                    "phase": "first-wave" if is_first_wave else "dynamic",
                    "worker_id": worker_id,
                    "candidate_id": candidate_id,
                    "candidate": result["candidate"],
                    "incumbent_epoch": response_epoch,
                    "stale_at_completion": response_epoch < incumbent_epoch,
                    "bound_kind": bound_kind,
                }
            )
            task_receipts.append(result)

            barrier_open = (
                args.stream_first_wave
                or len(first_wave_results) == args.initial_full_wave
            )
            if barrier_open:
                for idle_worker in range(args.workers):
                    if idle_worker in in_flight or not remaining:
                        continue
                    if incumbent is None:
                        raise RuntimeError("barrier release has no incumbent")
                    if dynamic_started is None:
                        dynamic_started = time.perf_counter()
                    dispatch(
                        idle_worker,
                        remaining.popleft(),
                        mode="PVS",
                        incumbent_epoch=incumbent_epoch,
                        incumbent=incumbent,
                    )

        if incumbent is None or first_wave_seconds is None:
            raise RuntimeError("root schedule produced no exact first wave")
        dynamic_seconds = (
            0.0
            if dynamic_started is None
            else time.perf_counter() - dynamic_started
        )
        search_seconds = time.perf_counter() - search_started

        expected_ids = {candidate.candidate_id for candidate in candidates}
        if completed_candidate_ids != expected_ids:
            missing = sorted(expected_ids - completed_candidate_ids)
            extra = sorted(completed_candidate_ids - expected_ids)
            raise RuntimeError(f"candidate set mismatch: missing={missing}, extra={extra}")

        for command_queue in command_queues:
            command_queue.put({"kind": "shutdown"})
        while len(final_worker_receipts) < args.workers:
            message = _get_message(result_queue, processes, common_deadline)
            if message.get("kind") != "shutdown":
                raise RuntimeError(f"unexpected shutdown message: {message!r}")
            worker_id = int(message["worker_id"])
            if worker_id in final_worker_receipts:
                raise RuntimeError(f"duplicate worker shutdown {worker_id}")
            final_worker_receipts[worker_id] = message
        memory_stop.set()
        memory_thread.join()

        expected_pv = (
            "f7f5/e8f7",
            "c1b2/e2e3/f1c4",
            "e7e6/f5f4/f4e3/e3f2",
            "e1f2/d1g4/f2e2/g1h3/g4g7",
        )
        expected_match = (
            incumbent.candidate.machine == "b2b3"
            and incumbent.score == 951
            and incumbent.pv == expected_pv
        )
        coordinator_stats = asdict(coordinator.stats)
        aggregate_stats = coordinator_stats.copy()
        for worker in final_worker_receipts.values():
            for key, value in worker["stats"].items():
                aggregate_stats[key] += int(value)
        total_work = coordinator.stats.work_positions + sum(
            int(worker["work"]) for worker in final_worker_receipts.values()
        )
        if total_work >= args.max_work:
            raise RuntimeError(
                f"aggregate work {total_work} reached public cap {args.max_work}"
            )
        if any(
            int(worker["work"]) > worker_work_budget
            for worker in final_worker_receipts.values()
        ):
            raise RuntimeError("a worker exceeded its cumulative work allocation")

        output = {
            "prototype": (
                "async-streaming-first-wave-root-workers-v2"
                if args.stream_first_wave
                else "async-no-seed-root-workers-v1"
            ),
            "publishable": False,
            "safety_certified": False,
            "legal_series_certified": False,
            "authoritative_replay_certified": False,
            "reason": "start-only final-iteration feasibility; no root mate safety",
            "completion_order_nondeterministic": True,
            "stream_first_wave": args.stream_first_wave,
            "initial_full_wave": args.initial_full_wave,
            "deterministic_result": expected_match,
            "root_proof": None,
            "depth": args.depth,
            "width": args.width,
            "workers": args.workers,
            "threads_per_worker": args.threads_per_worker,
            "cache_capacity": args.cache_capacity,
            "max_work": args.max_work,
            "coordinator_work_budget": coordinator_work_budget,
            "worker_work_budget": worker_work_budget,
            "common_deadline_seconds": args.time_limit,
            "worker_startup_seconds": worker_startup_seconds,
            "first_wave_seconds": first_wave_seconds,
            "dynamic_seconds": dynamic_seconds,
            "search_seconds": search_seconds,
            "total_seconds": time.perf_counter() - overall_started,
            "peak_total_rss_bytes": peak_total_rss[0],
            "peak_coordinator_rss_bytes": peak_rss_by_pid.get(os.getpid(), 0),
            "peak_worker_rss_bytes": {
                str(worker_id): peak_rss_by_pid.get(pid, 0)
                for worker_id, pid in sorted(worker_pids.items())
            },
            "max_peak_worker_rss_bytes": max(
                (
                    peak_rss_by_pid.get(pid, 0)
                    for pid in worker_pids.values()
                ),
                default=0,
            ),
            "root_order": [candidate.machine for candidate in candidates],
            "winner": asdict(incumbent),
            "expected_signature_match": expected_match,
            "coordinator_work": coordinator.stats.work_positions,
            "worker_work": {
                str(worker_id): int(receipt["work"])
                for worker_id, receipt in sorted(final_worker_receipts.items())
            },
            "total_work": total_work,
            "aggregate_work_within_limit": total_work < args.max_work,
            "aggregate_stats_sum": aggregate_stats,
            "dispatch_sequence": dispatch_sequence,
            "completion_sequence": completion_sequence,
            "task_receipts": task_receipts,
            "worker_receipts": final_worker_receipts,
            "worker_pids": worker_pids,
        }
        print(json.dumps(output, sort_keys=True, default=str))
        if not expected_match:
            raise RuntimeError("async prototype result did not match D5 anchor")
    except BaseException as error:
        failure = error
        raise
    finally:
        memory_stop.set()
        if "memory_thread" in locals():
            memory_thread.join()
        for command_queue in command_queues:
            try:
                command_queue.put_nowait({"kind": "shutdown"})
            except Exception:
                pass
        for process in processes:
            process.join(timeout=2.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2.0)
        if failure is not None:
            print(
                json.dumps(
                    {
                        "prototype": "async-no-seed-root-workers-v1",
                        "publishable": False,
                        "safety_certified": False,
                        "status": "UNKNOWN",
                        "error_type": type(failure).__name__,
                        "error": str(failure),
                    },
                    sort_keys=True,
                )
            )


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
