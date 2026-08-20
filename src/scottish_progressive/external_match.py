from __future__ import annotations

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
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

import chess

from .external import (
    BUCEPHALUS_ADAPTER_VERSION,
    BUCEPHALUS_MAX_GAME_RECORD,
    BUCEPHALUS_MAX_PLY,
    BUCEPHALUS_OPENING_HISTORIES_V1,
    BucephalusSpec,
    ExternalAnalysis,
    ExternalEngineConfigurationError,
    ExternalEngineError,
    ExternalEngineProtocolError,
    ExternalEngineTimeout,
    SeriesHistory,
    analyze_bucephalus,
    replay_series_history,
)
from .league import OPENING_SUITE, OPENING_SUITE_VERSION, OpeningCase
from .model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    Outcome,
    ProgressiveState,
    SeriesResult,
)
from .profiles import EngineProfile
from .resources import MIB, ResourceBudget, detect_resource_budget
from .rules import SeriesLegalityError, play_series
from .search import SearchLimits, SearchResult, analyze


EXTERNAL_MATCH_FORMAT = "spc-bucephalus-fixed-suite-v1"
EXTERNAL_PLY_POLICY = "series-number-plus-fixed-lookahead-v1"
BUCEPHALUS_PROCESS_MEMORY_ESTIMATE_MB = 191
LOCAL_WORKER_MEMORY_ESTIMATE_MB = 512
WORKER_OVERHEAD_MEMORY_ESTIMATE_MB = 65
DEFAULT_EXTERNAL_MATCH_MEMORY_PER_WORKER_MB = (
    BUCEPHALUS_PROCESS_MEMORY_ESTIMATE_MB
    + LOCAL_WORKER_MEMORY_ESTIMATE_MB
    + WORKER_OVERHEAD_MEMORY_ESTIMATE_MB
)
BUCEPHALUS_MAX_LEGAL_MOVES = 100

ExternalAdapter = Callable[..., ExternalAnalysis]
LocalAnalyzer = Callable[..., SearchResult]


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
    }


@dataclass(frozen=True, slots=True)
class ExternalMatchConfig:
    """Exact asymmetric limits for local-profile versus Bucephalus evidence."""

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
    emergency_max_series: int = 18

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
        if self.external_ply_policy != EXTERNAL_PLY_POLICY:
            raise ValueError(
                f"unsupported external ply policy {self.external_ply_policy}"
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
        if not 1 <= self.emergency_max_series <= BUCEPHALUS_MAX_PLY:
            raise ValueError(
                f"emergency_max_series must be between 1 and "
                f"{BUCEPHALUS_MAX_PLY}"
            )

    def external_search_ply(self, series_number: int) -> int:
        return series_number + self.external_lookahead_micro_plies

    def as_dict(self) -> dict[str, Any]:
        return {
            "pairs": self.pairs,
            "games": self.pairs * 2,
            "seed": self.seed,
            "opening_suite_version": self.opening_suite_version,
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
                "time_limit_seconds": None,
                "collect_all_root_scores": False,
                "root_score_mode": "best-only-play-optimized",
                "fresh_searcher_each_series": True,
            },
            "external_limits": {
                "ply_policy": self.external_ply_policy,
                "formula": "series_number + fixed_lookahead_micro_plies",
                "fixed_lookahead_micro_plies": (
                    self.external_lookahead_micro_plies
                ),
                "maximum_supported_micro_plies": BUCEPHALUS_MAX_PLY,
                "wall_watchdog_seconds_per_call": (
                    self.external_wall_timeout_seconds
                ),
                "node_limit": None,
                "native_time_control": None,
                "timeout_result": "technical-incomplete-*",
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
    by_id = {case.case_id: case for case in OPENING_SUITE}
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
    for pair_index, opening in enumerate(_ordered_openings(config)):
        history = BUCEPHALUS_OPENING_HISTORIES_V1[opening.case_id]
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
    external_adapter: ExternalAdapter = analyze_bucephalus,
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
        if not _record_capacity_supported(history):
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
        if _root_legal_move_count(state) >= BUCEPHALUS_MAX_LEGAL_MOVES:
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

        mover = state.board.turn
        before_pfen = state.pfen
        played_by_local = mover == job.local_color
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
            try:
                analysis = local_analyzer(
                    state,
                    SearchLimits(
                        depth_series=job.config.local_depth_series,
                        max_series_per_node=(
                            job.config.local_max_series_per_node
                        ),
                        time_limit_seconds=None,
                        max_generation_positions=search_work_limit,
                        collect_all_root_scores=False,
                    ),
                    profile=job.local_profile,
                )
            except BaseException as error:
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
            stats = getattr(analysis, "stats", None)
            search_work = int(
                getattr(stats, "work_positions", getattr(stats, "generation_positions", 0))
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
                "game_local_work_positions": local_work_positions,
                "work_limit_reached": getattr(
                    analysis, "work_limit_reached", False
                ),
                "timed_out": getattr(analysis, "timed_out", False),
                "exact_width": getattr(analysis, "exact_width", False),
                "root_scores_complete": getattr(
                    analysis, "root_scores_complete", False
                ),
                "stats": _stats_dict(stats),
                "played": False,
            }
            if getattr(analysis, "timed_out", False):
                trace.append(attempted_trace)
                return _technical_record(
                    job,
                    state,
                    start_pfen,
                    trace,
                    local_work_positions,
                    external_calls,
                    "technical-local-unexpected-timeout",
                    owner="local",
                )
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
            try:
                external_analysis = external_adapter(
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
            except BaseException as error:
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
                    "executable_sha256": (
                        external_analysis.executable_sha256
                    ),
                    "upstream_commit": external_analysis.upstream_commit,
                    "adapter_version": external_analysis.adapter_version,
                    "request_script": external_analysis.request_script,
                    "stdout": external_analysis.stdout,
                    "stderr": external_analysis.stderr,
                }
            )
            if (
                external_analysis.requested_ply != requested_ply
                or external_analysis.completed_ply != requested_ply
                or external_analysis.executable_sha256.lower()
                != job.external_spec.sha256
                or external_analysis.upstream_commit
                != job.external_spec.upstream_commit
                or external_analysis.adapter_version
                != BUCEPHALUS_ADAPTER_VERSION
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
) -> tuple[ExternalGameRecord, ...]:
    completed: dict[str, ExternalGameRecord] = {}

    def report(count: int) -> None:
        if progress is not None:
            progress(f"external match: finished {count}/{len(jobs)} games")

    if resources.workers == 1:
        for count, job in enumerate(jobs, 1):
            try:
                completed[job.game_id] = _play_external_game(job)
            except BaseException as error:
                completed[job.game_id] = _worker_failure(job, error)
            report(count)
    else:
        with ProcessPoolExecutor(max_workers=resources.workers) as executor:
            future_jobs = {
                executor.submit(_play_external_game, job): job for job in jobs
            }
            for count, future in enumerate(as_completed(future_jobs), 1):
                job = future_jobs[future]
                try:
                    completed[job.game_id] = future.result()
                except BaseException as error:
                    completed[job.game_id] = _worker_failure(job, error)
                report(count)
    return tuple(completed[job.game_id] for job in jobs)


def _local_points(record: ExternalGameRecord) -> float | None:
    if record.result == "1/2-1/2":
        return 0.5
    if record.result == "*":
        return None
    local_won = record.winner == "local"
    return 1.0 if local_won else 0.0


def _summarize(
    records: Sequence[ExternalGameRecord],
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    wins = draws = losses = incomplete = 0
    failure_reasons: dict[str, int] = {}
    failure_owners: dict[str, int] = {}
    for record in records:
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
            "technical_failures": {
                "by_reason": dict(sorted(failure_reasons.items())),
                "by_owner": dict(sorted(failure_owners.items())),
            },
        },
        tuple(pairs),
    )


def _rule_protocol_gaps() -> list[dict[str, str]]:
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
                "Bucephalus receives a declared micro-ply depth and an external "
                "wall watchdog. A timeout is an incomplete game, not a result."
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


def run_external_match(
    local_profile: EngineProfile,
    external_spec: BucephalusSpec,
    *,
    config: ExternalMatchConfig | None = None,
    requested_workers: int | None = None,
    memory_per_worker_mb: int = DEFAULT_EXTERNAL_MATCH_MEMORY_PER_WORKER_MB,
    reserve_memory_mb: int = 512,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Runs a fixed, color-swapped local-profile versus Bucephalus match.

    The harness never mutates league/champion state. Only authoritative legal
    checkmate, stalemate, or proven dead-material draw can complete a game;
    every adapter, replay, resource, or emergency limit is serialized as `*`.
    """

    config = config or ExternalMatchConfig()
    if memory_per_worker_mb < DEFAULT_EXTERNAL_MATCH_MEMORY_PER_WORKER_MB:
        raise ValueError(
            f"memory_per_worker_mb must be at least "
            f"{DEFAULT_EXTERNAL_MATCH_MEMORY_PER_WORKER_MB} for local search plus "
            "the external process"
        )
    executable, executable_hash = external_spec.verify()
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
    started = time.perf_counter()
    records = _execute_jobs(jobs, resources, progress)
    elapsed_seconds = time.perf_counter() - started
    summary, pairs = _summarize(records)
    selected_openings = [job.opening for job in jobs[::2]]
    match_id = jobs[0].game_id[:20]
    return {
        "format": EXTERNAL_MATCH_FORMAT,
        "report_id": "external-report-" + match_id,
        "created_at": _now(),
        "local_engine": {
            "engine_version": ENGINE_VERSION,
            "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
            "runtime": _runtime_provenance(),
            "profile": local_profile.as_dict(),
        },
        "external_engine": {
            "name": "Bucephalus",
            "resolved_executable": str(executable),
            "executable_sha256": executable_hash,
            "upstream_commit": external_spec.upstream_commit,
            "adapter_version": BUCEPHALUS_ADAPTER_VERSION,
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
        },
        "selected_openings": [
            {
                **opening.as_dict(),
                "canonical_series_history": [
                    list(series)
                    for series in BUCEPHALUS_OPENING_HISTORIES_V1[
                        opening.case_id
                    ]
                ],
            }
            for opening in selected_openings
        ],
        "summary": summary,
        "pairs": list(pairs),
        "games": [record.as_dict() for record in records],
        "rule_and_protocol_gaps": _rule_protocol_gaps(),
        "claim_scope": {
            "independent_opponent": True,
            "fixed_suite_only": True,
            "promotion_effect": "none",
            "statement": (
                "Results apply only to the selected canonical Scottish "
                "Progressive openings and the exact asymmetric policies in "
                "this report."
            ),
            "stockfish_level_claim": False,
            "rating_claim": False,
            "warning": (
                "This is independent-engine game evidence, not calibrated Elo, "
                "SPRT, equal-node evidence, or proof of Stockfish-level strength."
            ),
        },
    }


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
