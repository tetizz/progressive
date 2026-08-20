from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import hashlib
import json
import platform
from pathlib import Path
import random
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

import chess

from .model import ENGINE_SOURCE_FINGERPRINT, ENGINE_VERSION, Outcome, ProgressiveState
from .profiles import (
    EngineProfile,
    baseline_profile,
    create_population,
    crossover_profile,
    load_profile,
    mutate_profile,
    save_profile,
)
from .resources import ResourceBudget, detect_resource_budget
from .rules import generate_series, play_series
from .search import SearchLimits, analyze


LEAGUE_SCHEMA_VERSION = 2
OPENING_SUITE_VERSION = "spc-league-boundaries-v3"
PROMOTION_METHOD = "deterministic-fixed-suite-pairs-v1"

PUBLISHED_RULE_ANCHORS: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    (
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3",
        4,
        ("c7c6", "d8b6", "f6e4", "b6f2"),
    ),
    (
        "rn1qkb1r/ppp1pppp/5n2/3P4/8/5N2/PPPP1PPP/RNBbK2R w KQkq - 0 7",
        5,
        ("f3e5", "g2g4", "g4g5", "g5g6", "g6f7"),
    ),
    (
        "bnq1nr2/p1pp1pk1/8/4PP2/1P2P1p1/8/P1P2KP1/BNbBN2r w - - 0 1",
        7,
        ("e1f3", "f3d4", "e5e6", "e6e7", "e7f8r", "f8h8", "d4e6"),
    ),
    (
        "7R/pp3p1p/1p3k2/3P4/1b6/5P2/PPP2P1P/RNK5 b - - 0 1",
        8,
        ("b4d6", "b6b5", "b5b4", "b4b3", "b3a2", "a2b1n", "b1c3", "d6f4"),
    ),
    (
        "rnbqkb1r/ppp1pppp/5n2/3p2B1/3P4/2N2N2/PPP1PPPP/R2QKB1R b KQkq - 4 3",
        4,
        ("f6e4", "d8d6", "d6g3", "g3f2"),
    ),
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def runtime_provenance() -> dict[str, str]:
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_chess_version": chess.__version__,
    }


@dataclass(frozen=True, slots=True)
class OpeningCase:
    case_id: str
    fen: str
    series_number: int
    quiet_series: int = 0
    ep_targets: tuple[str, ...] = ()
    source: str = "curated"

    def state(self) -> ProgressiveState:
        return ProgressiveState.from_fen(
            self.fen,
            self.series_number,
            quiet_series=self.quiet_series,
            ep_targets=tuple(chess.parse_square(value) for value in self.ep_targets),
        )

    def as_dict(self) -> dict[str, Any]:
        state = self.state()
        return {
            **asdict(self),
            "pfen": state.pfen,
            "position_hash": state.position_hash,
        }


def _case_from_state(
    case_id: str, state: ProgressiveState, *, source: str
) -> OpeningCase:
    return OpeningCase(
        case_id=case_id,
        fen=state.board.fen(en_passant="fen"),
        series_number=state.series_number,
        quiet_series=state.quiet_series,
        ep_targets=tuple(chess.square_name(square) for square in state.ep_targets),
        source=source,
    )


def _build_opening_suite() -> tuple[OpeningCase, ...]:
    """Builds 30 exact legal boundaries from versioned move prefixes."""

    initial = ProgressiveState.initial()
    cases: list[OpeningCase] = [
        _case_from_state(
            "initial", initial, source="orthodox initial boundary"
        )
    ]
    first_states: dict[str, ProgressiveState] = {}
    for uci in sorted(move.uci() for move in initial.board.legal_moves):
        result = play_series(initial, (uci,))
        first_states[uci] = result.final_state
        san = result.san[0].replace("+", "").replace("#", "").lower()
        cases.append(
            _case_from_state(
                f"after-1-{san}",
                result.final_state,
                source=f"complete legal prefix: 1.{result.san[0]}",
            )
        )

    # Seven exact third-series boundaries add different reply structures. The
    # prefixes are deliberately stored in the source text and replayed through
    # the public rules API, so an illegal suite cannot silently load.
    for first_uci in ("b1a3", "b1c3", "b2b3", "c2c4", "d2d4", "e2e4", "g1f3"):
        black = play_series(first_states[first_uci], ("a7a6", "b7b6"))
        cases.append(
            _case_from_state(
                f"after-{first_uci}-a6-b6",
                black.final_state,
                source=f"complete legal prefix: {first_uci} / a7a6,b7b6",
            )
        )

    cases.extend(
        (
            OpeningCase(
                "published-bishop-pressure",
                "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3",
                4,
                source="published Scottish tactical anchor",
            ),
            OpeningCase(
                "published-central-pressure",
                "rnbqkb1r/ppp1pppp/5n2/3p2B1/3P4/2N2N2/PPP1PPPP/R2QKB1R b KQkq - 4 3",
                4,
                source="published Scottish reply anchor",
            ),
        )
    )
    if len(cases) != 30:
        raise RuntimeError(f"opening suite must contain 30 cases, got {len(cases)}")
    hashes = [case.state().position_hash for case in cases]
    if len(set(hashes)) != len(hashes):
        raise RuntimeError("opening suite contains duplicate boundary positions")
    return tuple(cases)


# A paired match uses the exact same boundary once with each profile
# controlling each orthodox color. Seeded ordering never samples with
# replacement, and the exact boundary metadata is persisted with every game.
OPENING_SUITE = _build_opening_suite()


@dataclass(frozen=True, slots=True)
class LeagueConfig:
    population_size: int = 10
    generations: int = 2
    seed: int = 20260820
    preliminary_games_per_pair: int = 10
    promotion_games: int = 20
    max_replacement_games: int = 40
    minimum_promotion_games: int = 20
    search_depth: int = 2
    max_series_per_node: int = 32
    max_generation_positions: int = 250_000
    max_game_series: int = 12
    requested_workers: int | None = None
    memory_per_worker_mb: int = 512
    reserve_memory_mb: int = 512
    opening_suite_version: str = OPENING_SUITE_VERSION
    opening_case_ids: tuple[str, ...] = tuple(
        case.case_id for case in OPENING_SUITE
    )

    def __post_init__(self) -> None:
        if not 2 <= self.population_size <= 64:
            raise ValueError("population_size must be between 2 and 64")
        if not 1 <= self.generations <= 100:
            raise ValueError("generations must be between 1 and 100")
        for name, value in (
            ("preliminary_games_per_pair", self.preliminary_games_per_pair),
            ("promotion_games", self.promotion_games),
        ):
            if value < 2 or value % 2:
                raise ValueError(f"{name} must be a positive even number")
        if self.max_replacement_games < 0 or self.max_replacement_games % 2:
            raise ValueError("max_replacement_games must be a non-negative even number")
        if self.minimum_promotion_games < 20 or self.minimum_promotion_games % 2:
            raise ValueError(
                "minimum_promotion_games must be an even number of at least 20"
            )
        if not 1 <= self.search_depth <= 8:
            raise ValueError("search_depth must be between 1 and 8")
        if not 1 <= self.max_series_per_node <= 512:
            raise ValueError("max_series_per_node must be between 1 and 512")
        if self.max_generation_positions < 1_000:
            raise ValueError("max_generation_positions must be at least 1000")
        if self.max_game_series < 2:
            raise ValueError("max_game_series must be at least 2")
        if self.opening_suite_version != OPENING_SUITE_VERSION:
            raise ValueError(
                f"unsupported opening suite {self.opening_suite_version}"
            )
        available = {case.case_id for case in OPENING_SUITE}
        if not self.opening_case_ids or not set(self.opening_case_ids) <= available:
            raise ValueError("opening_case_ids must name cases in the active suite")
        available_pairs = len(self.opening_case_ids)
        if self.preliminary_games_per_pair // 2 > available_pairs:
            raise ValueError("opening suite is too small for preliminary pairs")
        if (
            self.promotion_games // 2 + self.max_replacement_games // 2
            > available_pairs
        ):
            raise ValueError("opening suite is too small for promotion and replacements")

    @classmethod
    def smoke(cls, *, seed: int = 7) -> LeagueConfig:
        """Tiny wiring check.  It can never satisfy the 20-game promotion gate."""

        return cls(
            population_size=2,
            generations=1,
            seed=seed,
            preliminary_games_per_pair=2,
            promotion_games=2,
            max_replacement_games=4,
            minimum_promotion_games=20,
            search_depth=1,
            max_series_per_node=2,
            max_generation_positions=5_000,
            max_game_series=2,
            requested_workers=1,
            memory_per_worker_mb=128,
            reserve_memory_mb=128,
            opening_case_ids=tuple(case.case_id for case in OPENING_SUITE[:3]),
        )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["opening_case_ids"] = list(self.opening_case_ids)
        payload["deterministic_match_limits"] = {
            "depth_series": self.search_depth,
            "max_series_per_node": self.max_series_per_node,
            "max_generation_positions": self.max_generation_positions,
            "time_limit_seconds": None,
            "fresh_searcher_each_series": True,
            "same_for_every_profile": True,
        }
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> LeagueConfig:
        allowed = {
            field_name
            for field_name in cls.__dataclass_fields__
            if field_name != "opening_case_ids"
        }
        values = {name: payload[name] for name in allowed if name in payload}
        values["opening_case_ids"] = tuple(payload.get("opening_case_ids", ()))
        return cls(**values)


@dataclass(frozen=True, slots=True)
class GameJob:
    job_key: str
    run_id: str
    generation: int
    stage: str
    opening_index: int
    opening: OpeningCase
    seed: int
    white_profile: EngineProfile
    black_profile: EngineProfile
    search_depth: int
    max_series_per_node: int
    max_generation_positions: int
    max_game_series: int


@dataclass(frozen=True, slots=True)
class GameRecord:
    job_key: str
    run_id: str
    generation: int
    stage: str
    opening_index: int
    opening_case_id: str
    opening_suite_version: str
    seed: int
    white_profile_id: str
    black_profile_id: str
    result: str
    terminal_reason: str
    decisive_profile_id: str | None
    engine_failure_profile_id: str | None
    start_pfen: str
    final_pfen: str
    series_played: int
    trace: tuple[dict[str, Any], ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class GateReport:
    passed: bool
    checks: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "checks": list(self.checks)}


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    promoted: bool
    wins: int
    draws: int
    losses: int
    technical_failures: int
    games: int
    pairs: int
    score_rate: float
    lower_confidence_bound: None
    minimum_games: int
    gate_passed: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def promotion_decision(
    *,
    wins: int,
    draws: int,
    losses: int,
    technical_failures: int,
    gate_passed: bool,
    minimum_games: int = 20,
) -> PromotionDecision:
    pairs = wins + draws + losses
    games = pairs * 2
    points = wins + draws * 0.5
    rate = points / pairs if pairs else 0.0
    minimum_pairs = max(10, (minimum_games + 1) // 2)
    required_pair_wins = max(9, (minimum_pairs * 9 + 9) // 10)
    reasons: list[str] = []
    if not gate_passed:
        reasons.append("rules/tactical gate failed")
    if technical_failures:
        reasons.append(f"{technical_failures} technical match failure(s)")
    if pairs < minimum_pairs:
        reasons.append(
            f"only {pairs}/{minimum_pairs} completed unique color-swapped pairs"
        )
    if rate <= 0.5:
        reasons.append(f"fixed-suite pair score {rate:.3f} is not above 0.500")
    if wins < required_pair_wins:
        reasons.append(
            f"only {wins}/{required_pair_wins} required fixed-suite pair wins"
        )
    if losses:
        reasons.append(f"{losses} fixed-suite pair loss(es); none are allowed")
    promoted = not reasons
    return PromotionDecision(
        promoted=promoted,
        wins=wins,
        draws=draws,
        losses=losses,
        technical_failures=technical_failures,
        games=games,
        pairs=pairs,
        score_rate=rate,
        lower_confidence_bound=None,
        minimum_games=minimum_games,
        gate_passed=gate_passed,
        reason=(
            f"promoted by {PROMOTION_METHOD}: pair score={rate:.3f}, "
            f"pair W/D/L={wins}/{draws}/{losses}; fixed-suite evidence only"
            if promoted
            else f"not promoted by {PROMOTION_METHOD}: " + "; ".join(reasons)
        ),
    )


def _winner_for_terminal(result: Any, mover: chess.Color) -> chess.Color | None:
    if result.outcome != Outcome.CHECKMATE:
        return None
    return mover if result.ended_by_check else not mover


def _technical_forfeit(
    job: GameJob,
    state: ProgressiveState,
    trace: Sequence[dict[str, Any]],
    failing_profile_id: str,
    reason: str,
    *,
    error: str | None = None,
) -> GameRecord:
    failing_white = failing_profile_id == job.white_profile.profile_id
    winner_id = (
        job.black_profile.profile_id if failing_white else job.white_profile.profile_id
    )
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
        "0-1" if failing_white else "1-0",
        reason,
        winner_id,
        failing_profile_id,
        job.opening.state().pfen,
        state.pfen,
        len(trace),
        tuple(trace),
        error,
    )


def _play_game(job: GameJob) -> GameRecord:
    state = job.opening.state()
    start_pfen = state.pfen
    trace: list[dict[str, Any]] = []
    while state.series_number <= job.max_game_series:
        mover = state.board.turn
        profile = job.white_profile if mover == chess.WHITE else job.black_profile
        try:
            result = analyze(
                state,
                SearchLimits(
                    depth_series=job.search_depth,
                    max_series_per_node=job.max_series_per_node,
                    time_limit_seconds=None,
                    max_generation_positions=job.max_generation_positions,
                ),
                profile=profile,
            )
        except BaseException as error:
            return _technical_forfeit(
                job,
                state,
                trace,
                profile.profile_id,
                "engine-exception",
                error=f"{type(error).__name__}: {error}",
            )

        if result.timed_out or result.work_limit_reached or result.best_series is None:
            proven_draw = result.proof == "draw" and result.adjudication_status == "proven-draw-no-mating-material"
            if not proven_draw:
                reason = (
                    "engine-timeout"
                    if result.timed_out
                    else "engine-work-limit"
                    if result.work_limit_reached
                    else "engine-no-move"
                )
                return _technical_forfeit(
                    job, state, trace, profile.profile_id, reason
                )
            return GameRecord(
                job.job_key, job.run_id, job.generation, job.stage,
                job.opening_index, job.opening.case_id, OPENING_SUITE_VERSION,
                job.seed, job.white_profile.profile_id, job.black_profile.profile_id,
                "1/2-1/2", "proven-draw-no-mating-material", None, None,
                start_pfen, state.pfen, len(trace), tuple(trace),
            )

        selected = result.best_series
        trace.append(
            {
                "series_number": state.series_number,
                "profile_id": profile.profile_id,
                "series": selected.machine_notation,
                "notation": selected.notation,
                "score_white_heuristic_points": result.score,
                "completed_depth": result.completed_depth,
                "exact_width": result.exact_width,
                "nodes": result.stats.nodes,
                "generation_positions": result.stats.generation_positions,
                "work_limit_reached": result.work_limit_reached,
                "outcome": selected.outcome.value if selected.outcome else None,
            }
        )
        winner = _winner_for_terminal(selected, mover)
        state = selected.final_state
        if winner is not None:
            winner_id = (
                job.white_profile.profile_id
                if winner == chess.WHITE
                else job.black_profile.profile_id
            )
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
                "1-0" if winner == chess.WHITE else "0-1",
                "checkmate",
                winner_id,
                None,
                start_pfen,
                state.pfen,
                len(trace),
                tuple(trace),
            )
        if selected.outcome in {Outcome.STALEMATE, Outcome.TEN_SERIES_DRAW}:
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
                "1/2-1/2",
                selected.outcome.value,
                None,
                None,
                start_pfen,
                state.pfen,
                len(trace),
                tuple(trace),
            )

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
        "max-series-adjudication-not-proven-draw",
        None,
        None,
        start_pfen,
        state.pfen,
        len(trace),
        tuple(trace),
    )


def run_rules_tactical_gate(
    profile: EngineProfile,
    *,
    search_depth: int,
    max_series_per_node: int,
    max_generation_positions: int = 250_000,
) -> GateReport:
    checks: list[dict[str, Any]] = []
    try:
        initial_count = len(generate_series(ProgressiveState.initial()))
        checks.append(
            {
                "name": "initial-legal-series",
                "passed": initial_count == 20,
                "evidence": {"count": initial_count, "expected": 20},
            }
        )
    except BaseException as error:
        checks.append(
            {"name": "initial-legal-series", "passed": False, "error": str(error)}
        )

    for index, (fen, series_number, moves) in enumerate(PUBLISHED_RULE_ANCHORS, 1):
        try:
            anchor = play_series(
                ProgressiveState.from_fen(fen, series_number), moves
            )
            checks.append(
                {
                    "name": f"published-long-anchor-{index}",
                    "passed": (
                        anchor.outcome == Outcome.CHECKMATE
                        and anchor.ended_by_check
                        and anchor.used_moves == series_number
                    ),
                    "evidence": {
                        "series_number": series_number,
                        "series": anchor.machine_notation,
                        "outcome": anchor.outcome.value if anchor.outcome else None,
                    },
                }
            )
        except BaseException as error:
            checks.append(
                {
                    "name": f"published-long-anchor-{index}",
                    "passed": False,
                    "error": str(error),
                }
            )

    try:
        checked = ProgressiveState.from_fen(
            "4k3/q3R3/8/8/8/8/8/4K3 b - - 0 1", 2
        )
        countercheck = play_series(checked, ("a7e7",))
        passed = countercheck.ended_by_check and countercheck.unused_moves == 1
        checks.append(
            {
                "name": "countercheck-truncates-series",
                "passed": passed,
                "evidence": {
                    "series": countercheck.machine_notation,
                    "unused_moves": countercheck.unused_moves,
                },
            }
        )
    except BaseException as error:
        checks.append(
            {
                "name": "countercheck-truncates-series",
                "passed": False,
                "error": str(error),
            }
        )

    try:
        mate_state = ProgressiveState.from_fen(
            "7k/8/5KQ1/8/8/8/8/8 w - - 0 1", 1
        )
        mate = analyze(
            mate_state,
            SearchLimits(
                depth_series=search_depth,
                max_series_per_node=max_series_per_node,
                time_limit_seconds=None,
                max_generation_positions=max_generation_positions,
            ),
            profile=profile,
        )
        passed = (
            not mate.timed_out
            and not mate.work_limit_reached
            and mate.best_series is not None
            and mate.best_series.outcome == Outcome.CHECKMATE
        )
        checks.append(
            {
                "name": "immediate-mate-selection",
                "passed": passed,
                "evidence": {
                    "best_series": (
                        mate.best_series.machine_notation if mate.best_series else None
                    ),
                    "timed_out": mate.timed_out,
                    "work_limit_reached": mate.work_limit_reached,
                    "proven_result": mate.forced,
                },
            }
        )
    except BaseException as error:
        checks.append(
            {"name": "immediate-mate-selection", "passed": False, "error": str(error)}
        )

    try:
        published = ProgressiveState.from_fen(
            "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3",
            4,
        )
        published_result = analyze(
            published,
            SearchLimits(
                depth_series=max(1, min(search_depth, 2)),
                max_series_per_node=max(64, max_series_per_node),
                time_limit_seconds=None,
                max_generation_positions=max(250_000, max_generation_positions),
            ),
            profile=profile,
        )
        passed = (
            not published_result.timed_out
            and not published_result.work_limit_reached
            and published_result.best_series is not None
            and published_result.best_series.outcome == Outcome.CHECKMATE
        )
        checks.append(
            {
                "name": "published-s4-mate-selection",
                "passed": passed,
                "evidence": {
                    "best_series": (
                        published_result.best_series.machine_notation
                        if published_result.best_series
                        else None
                    ),
                    "timed_out": published_result.timed_out,
                    "work_limit_reached": published_result.work_limit_reached,
                    "branch_cap": max(64, max_series_per_node),
                    "generation_positions": published_result.stats.generation_positions,
                },
            }
        )
    except BaseException as error:
        checks.append(
            {
                "name": "published-s4-mate-selection",
                "passed": False,
                "error": str(error),
            }
        )
    return GateReport(
        passed=bool(checks) and all(bool(item["passed"]) for item in checks),
        checks=tuple(checks),
    )


class LeagueStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

    def __enter__(self) -> LeagueStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS league_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profiles (
                profile_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                generation INTEGER NOT NULL,
                profile_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                seed INTEGER NOT NULL,
                current_generation INTEGER NOT NULL,
                champion_profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                config_json TEXT NOT NULL,
                resource_json TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                runtime_json TEXT NOT NULL,
                decisive_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_population (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                generation INTEGER NOT NULL,
                profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                role TEXT NOT NULL,
                preliminary_rank INTEGER,
                fitness_points REAL,
                fitness_games INTEGER,
                PRIMARY KEY(run_id, generation, profile_id)
            );
            CREATE TABLE IF NOT EXISTS games (
                game_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_key TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                generation INTEGER NOT NULL,
                stage TEXT NOT NULL,
                opening_index INTEGER NOT NULL,
                opening_suite_version TEXT NOT NULL,
                opening_case_id TEXT NOT NULL,
                opening_json TEXT NOT NULL,
                seed INTEGER NOT NULL,
                white_profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                black_profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                result TEXT NOT NULL,
                terminal_reason TEXT NOT NULL,
                decisive_profile_id TEXT,
                engine_failure_profile_id TEXT,
                start_pfen TEXT NOT NULL,
                final_pfen TEXT NOT NULL,
                series_played INTEGER NOT NULL,
                trace_json TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS games_run_stage_idx
                ON games(run_id, generation, stage);
            CREATE TABLE IF NOT EXISTS matches (
                match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                generation INTEGER NOT NULL,
                stage TEXT NOT NULL,
                candidate_profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                opponent_profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                wins INTEGER NOT NULL,
                draws INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                technical_failures INTEGER NOT NULL,
                score_rate REAL NOT NULL,
                lower_confidence_bound REAL,
                confidence_method TEXT NOT NULL,
                gate_json TEXT NOT NULL,
                promoted INTEGER NOT NULL,
                decisive_reason TEXT NOT NULL,
                fixed_limits_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, generation, stage, candidate_profile_id, opponent_profile_id)
            );
            CREATE TABLE IF NOT EXISTS champion_lineage (
                lineage_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                generation INTEGER NOT NULL,
                previous_champion_id TEXT NOT NULL REFERENCES profiles(profile_id),
                champion_profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                match_id INTEGER REFERENCES matches(match_id),
                decisive_reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, generation)
            );
            """
        )
        run_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(runs)")
        }
        if "runtime_json" not in run_columns:
            self.connection.execute(
                "ALTER TABLE runs ADD COLUMN runtime_json TEXT NOT NULL DEFAULT '{}'"
            )
        self.connection.execute(
            "INSERT OR REPLACE INTO league_metadata(key,value) VALUES('schema_version',?)",
            (str(LEAGUE_SCHEMA_VERSION),),
        )
        self.connection.commit()

    def save_profile(self, profile: EngineProfile) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO profiles(profile_id,name,generation,profile_json,created_at)
            VALUES(?,?,?,?,?)
            """,
            (
                profile.profile_id,
                profile.name,
                profile.generation,
                json.dumps(profile.as_dict(), sort_keys=True, separators=(",", ":")),
                _now(),
            ),
        )
        self.connection.commit()

    def load_profile(self, profile_id: str) -> EngineProfile:
        row = self.connection.execute(
            "SELECT profile_json FROM profiles WHERE profile_id=?", (profile_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown profile {profile_id}")
        return EngineProfile.from_dict(json.loads(row["profile_json"]))

    def active_champion(self) -> EngineProfile | None:
        row = self.connection.execute(
            "SELECT value FROM league_metadata WHERE key='active_champion_profile_id'"
        ).fetchone()
        return self.load_profile(row["value"]) if row is not None else None

    def set_active_champion(self, profile: EngineProfile) -> None:
        self.save_profile(profile)
        self.connection.execute(
            "INSERT OR REPLACE INTO league_metadata(key,value) VALUES('active_champion_profile_id',?)",
            (profile.profile_id,),
        )
        self.connection.commit()

    def create_run(
        self,
        config: LeagueConfig,
        resources: ResourceBudget,
        champion: EngineProfile,
    ) -> str:
        self.save_profile(champion)
        run_id = str(uuid.uuid4())
        now = _now()
        self.connection.execute(
            """
            INSERT INTO runs(
                run_id,status,seed,current_generation,champion_profile_id,
                config_json,resource_json,engine_version,source_fingerprint,runtime_json,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                "running",
                config.seed,
                1,
                champion.profile_id,
                json.dumps(config.as_dict(), sort_keys=True, separators=(",", ":")),
                json.dumps(resources.as_dict(), sort_keys=True, separators=(",", ":")),
                ENGINE_VERSION,
                ENGINE_SOURCE_FINGERPRINT,
                json.dumps(runtime_provenance(), sort_keys=True, separators=(",", ":")),
                now,
                now,
            ),
        )
        self.connection.commit()
        return run_id

    def run_row(self, run_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown league run {run_id}")
        return row

    def latest_run_id(self) -> str | None:
        row = self.connection.execute(
            "SELECT run_id FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return str(row["run_id"]) if row else None

    def save_population(
        self,
        run_id: str,
        generation: int,
        profiles: Sequence[EngineProfile],
        champion_id: str,
    ) -> None:
        for profile in profiles:
            self.save_profile(profile)
            self.connection.execute(
                """
                INSERT OR IGNORE INTO run_population(run_id,generation,profile_id,role)
                VALUES(?,?,?,?)
                """,
                (
                    run_id,
                    generation,
                    profile.profile_id,
                    "champion" if profile.profile_id == champion_id else "challenger",
                ),
            )
        self.connection.commit()

    def population(self, run_id: str, generation: int) -> tuple[EngineProfile, ...]:
        rows = self.connection.execute(
            """
            SELECT p.profile_json FROM run_population rp
            JOIN profiles p ON p.profile_id=rp.profile_id
            WHERE rp.run_id=? AND rp.generation=?
            ORDER BY CASE rp.role WHEN 'champion' THEN 0 ELSE 1 END, p.profile_id
            """,
            (run_id, generation),
        ).fetchall()
        return tuple(EngineProfile.from_dict(json.loads(row["profile_json"])) for row in rows)

    def completed_job_keys(self, run_id: str, generation: int, stage: str) -> set[str]:
        return {
            str(row["job_key"])
            for row in self.connection.execute(
                """
                SELECT job_key FROM games
                WHERE run_id=? AND generation=? AND stage=?
                  AND terminal_reason!='worker-exception'
                """,
                (run_id, generation, stage),
            )
        }

    def save_game(self, record: GameRecord, opening: OpeningCase) -> None:
        self.connection.execute(
            """
            INSERT INTO games(
                job_key,run_id,generation,stage,opening_index,
                opening_suite_version,opening_case_id,opening_json,seed,
                white_profile_id,black_profile_id,result,terminal_reason,
                decisive_profile_id,engine_failure_profile_id,start_pfen,final_pfen,
                series_played,trace_json,error,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_key) DO UPDATE SET
                result=excluded.result,
                terminal_reason=excluded.terminal_reason,
                decisive_profile_id=excluded.decisive_profile_id,
                engine_failure_profile_id=excluded.engine_failure_profile_id,
                start_pfen=excluded.start_pfen,
                final_pfen=excluded.final_pfen,
                series_played=excluded.series_played,
                trace_json=excluded.trace_json,
                error=excluded.error,
                created_at=excluded.created_at
            """,
            (
                record.job_key,
                record.run_id,
                record.generation,
                record.stage,
                record.opening_index,
                record.opening_suite_version,
                record.opening_case_id,
                json.dumps(opening.as_dict(), sort_keys=True, separators=(",", ":")),
                record.seed,
                record.white_profile_id,
                record.black_profile_id,
                record.result,
                record.terminal_reason,
                record.decisive_profile_id,
                record.engine_failure_profile_id,
                record.start_pfen,
                record.final_pfen,
                record.series_played,
                json.dumps(record.trace, sort_keys=True, separators=(",", ":")),
                record.error,
                _now(),
            ),
        )
        self.connection.commit()

    def stage_rows(self, run_id: str, generation: int, stage: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM games WHERE run_id=? AND generation=? AND stage=? ORDER BY game_id",
                (run_id, generation, stage),
            )
        )

    def save_fitness(
        self,
        run_id: str,
        generation: int,
        ranking: Sequence[dict[str, Any]],
    ) -> None:
        for rank, item in enumerate(ranking, 1):
            self.connection.execute(
                """
                UPDATE run_population SET preliminary_rank=?,fitness_points=?,fitness_games=?
                WHERE run_id=? AND generation=? AND profile_id=?
                """,
                (
                    rank,
                    item["points"],
                    item["games"],
                    run_id,
                    generation,
                    item["profile_id"],
                ),
            )
        self.connection.commit()

    def save_match(
        self,
        run_id: str,
        generation: int,
        candidate_id: str,
        champion_id: str,
        gate: GateReport,
        decision: PromotionDecision,
        config: LeagueConfig,
    ) -> int:
        limits = {
            "depth_series": config.search_depth,
            "max_series_per_node": config.max_series_per_node,
            "max_generation_positions": config.max_generation_positions,
            "time_limit_seconds": None,
            "same_limits_for_both_profiles": True,
            "fresh_searcher_each_series": True,
        }
        self.connection.execute(
            """
            INSERT INTO matches(
                run_id,generation,stage,candidate_profile_id,opponent_profile_id,
                wins,draws,losses,technical_failures,score_rate,
                lower_confidence_bound,confidence_method,gate_json,promoted,
                decisive_reason,fixed_limits_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id,generation,stage,candidate_profile_id,opponent_profile_id)
            DO UPDATE SET
                wins=excluded.wins,
                draws=excluded.draws,
                losses=excluded.losses,
                technical_failures=excluded.technical_failures,
                score_rate=excluded.score_rate,
                lower_confidence_bound=excluded.lower_confidence_bound,
                confidence_method=excluded.confidence_method,
                gate_json=excluded.gate_json,
                promoted=excluded.promoted,
                decisive_reason=excluded.decisive_reason,
                fixed_limits_json=excluded.fixed_limits_json,
                created_at=excluded.created_at
            """,
            (
                run_id,
                generation,
                "promotion",
                candidate_id,
                champion_id,
                decision.wins,
                decision.draws,
                decision.losses,
                decision.technical_failures,
                decision.score_rate,
                decision.lower_confidence_bound,
                PROMOTION_METHOD,
                json.dumps(gate.as_dict(), sort_keys=True, separators=(",", ":")),
                int(decision.promoted),
                decision.reason,
                json.dumps(limits, sort_keys=True, separators=(",", ":")),
                _now(),
            ),
        )
        row = self.connection.execute(
            """
            SELECT match_id FROM matches
            WHERE run_id=? AND generation=? AND stage='promotion'
              AND candidate_profile_id=? AND opponent_profile_id=?
            """,
            (run_id, generation, candidate_id, champion_id),
        ).fetchone()
        self.connection.commit()
        return int(row["match_id"])

    def record_lineage(
        self,
        run_id: str,
        generation: int,
        previous_id: str,
        champion_id: str,
        match_id: int,
        reason: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO champion_lineage(
                run_id,generation,previous_champion_id,champion_profile_id,
                match_id,decisive_reason,created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (run_id, generation, previous_id, champion_id, match_id, reason, _now()),
        )
        self.connection.commit()

    def hall_of_fame(self, limit: int = 2) -> tuple[EngineProfile, ...]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT previous_champion_id FROM champion_lineage
            ORDER BY lineage_id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return tuple(self.load_profile(str(row[0])) for row in rows)

    def update_run(
        self,
        run_id: str,
        *,
        status: str,
        generation: int,
        champion_id: str,
        reason: str,
    ) -> None:
        self.connection.execute(
            """
            UPDATE runs SET status=?,current_generation=?,champion_profile_id=?,
                decisive_reason=?,updated_at=? WHERE run_id=?
            """,
            (status, generation, champion_id, reason, _now(), run_id),
        )
        self.connection.commit()

    def update_resources(self, run_id: str, resources: ResourceBudget) -> None:
        self.connection.execute(
            "UPDATE runs SET resource_json=?,updated_at=? WHERE run_id=?",
            (
                json.dumps(resources.as_dict(), sort_keys=True, separators=(",", ":")),
                _now(),
                run_id,
            ),
        )
        self.connection.commit()

    def champion_provenance(
        self, run_id: str, champion_id: str
    ) -> dict[str, Any]:
        run = self.run_row(run_id)
        promotion = self.connection.execute(
            """
            SELECT m.match_id,m.run_id,m.generation,m.confidence_method,
                   m.gate_json,m.decisive_reason,m.fixed_limits_json
            FROM champion_lineage l
            JOIN matches m ON m.match_id=l.match_id
            WHERE l.champion_profile_id=?
            ORDER BY l.lineage_id DESC LIMIT 1
            """,
            (champion_id,),
        ).fetchone()
        current_match = self.connection.execute(
            """
            SELECT match_id,generation,candidate_profile_id,opponent_profile_id,
                   confidence_method,gate_json,promoted,decisive_reason
            FROM matches WHERE run_id=? ORDER BY match_id DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()

        def match_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
            if row is None:
                return None
            payload = dict(row)
            if "gate_json" in payload:
                payload["gate"] = json.loads(payload.pop("gate_json"))
            if "fixed_limits_json" in payload:
                payload["fixed_limits"] = json.loads(
                    payload.pop("fixed_limits_json")
                )
            return payload

        return {
            "format": "spc-champion-provenance-v1",
            "profile_id": champion_id,
            "engine_version": run["engine_version"],
            "source_fingerprint": run["source_fingerprint"],
            "runtime": json.loads(run["runtime_json"]),
            "publishing_run_id": run_id,
            "publishing_run_status": run["status"],
            "opening_suite_version": json.loads(run["config_json"])[
                "opening_suite_version"
            ],
            "promotion_evidence": match_payload(promotion),
            "publishing_run_last_match": match_payload(current_match),
        }

    def status(self, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or self.latest_run_id()
        if run_id is None:
            return {"database": str(self.path), "run": None}
        row = self.run_row(run_id)
        counts = {
            item["stage"]: int(item["count"])
            for item in self.connection.execute(
                "SELECT stage,COUNT(*) AS count FROM games WHERE run_id=? GROUP BY stage",
                (run_id,),
            )
        }
        match_rows = self.connection.execute(
            """
            SELECT candidate_profile_id,opponent_profile_id,wins,draws,losses,
                   technical_failures,score_rate,lower_confidence_bound,
                   confidence_method,promoted,
                   decisive_reason
            FROM matches WHERE run_id=? ORDER BY match_id
            """,
            (run_id,),
        ).fetchall()
        return {
            "database": str(self.path),
            "run_id": run_id,
            "status": row["status"],
            "seed": row["seed"],
            "current_generation": row["current_generation"],
            "champion_profile_id": row["champion_profile_id"],
            "decisive_reason": row["decisive_reason"],
            "games_by_stage": counts,
            "config": json.loads(row["config_json"]),
            "resources": json.loads(row["resource_json"]),
            "runtime": json.loads(row["runtime_json"]),
            "promotion_method": PROMOTION_METHOD,
            "matches": [dict(item) for item in match_rows],
        }


def _stable_seed(*parts: object) -> int:
    encoded = "|".join(str(part) for part in parts).encode()
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big") & 0x7FFFFFFF


def _job_key(*parts: object) -> str:
    encoded = "|".join(str(part) for part in parts).encode()
    return hashlib.sha256(encoded).hexdigest()


def _opening_cases(config: LeagueConfig) -> tuple[OpeningCase, ...]:
    by_id = {case.case_id: case for case in OPENING_SUITE}
    return tuple(by_id[case_id] for case_id in config.opening_case_ids)


def _paired_jobs(
    *,
    run_id: str,
    generation: int,
    stage: str,
    first: EngineProfile,
    second: EngineProfile,
    game_count: int,
    config: LeagueConfig,
    pair_offset: int = 0,
) -> list[GameJob]:
    cases = _opening_cases(config)
    if game_count < 0 or game_count % 2:
        raise ValueError("paired game_count must be a non-negative even number")
    if pair_offset < 0:
        raise ValueError("pair_offset cannot be negative")
    pair_count = game_count // 2
    if pair_offset + pair_count > len(cases):
        raise ValueError(
            "opening suite exhausted; paired jobs never sample with replacement"
        )
    ordering_seed = _stable_seed(
        config.seed,
        generation,
        stage,
        first.profile_id,
        second.profile_id,
        OPENING_SUITE_VERSION,
        "opening-order",
    )
    ordered_cases = list(cases)
    random.Random(ordering_seed).shuffle(ordered_cases)
    jobs: list[GameJob] = []
    for local_pair_index in range(pair_count):
        pair_index = pair_offset + local_pair_index
        case = ordered_cases[pair_index]
        seed = _stable_seed(ordering_seed, pair_index, case.case_id)
        for swap, (white, black) in enumerate(((first, second), (second, first))):
            key = _job_key(
                run_id,
                generation,
                stage,
                pair_index,
                case.case_id,
                seed,
                white.profile_id,
                black.profile_id,
            )
            jobs.append(
                GameJob(
                    key,
                    run_id,
                    generation,
                    stage,
                    pair_index * 2 + swap,
                    case,
                    seed,
                    white,
                    black,
                    config.search_depth,
                    config.max_series_per_node,
                    config.max_generation_positions,
                    config.max_game_series,
                )
            )
    return jobs


def _preliminary_jobs(
    run_id: str,
    generation: int,
    population: Sequence[EngineProfile],
    config: LeagueConfig,
) -> list[GameJob]:
    jobs: list[GameJob] = []
    stage = f"preliminary-g{generation}"
    for first_index, first in enumerate(population):
        for second in population[first_index + 1 :]:
            jobs.extend(
                _paired_jobs(
                    run_id=run_id,
                    generation=generation,
                    stage=stage,
                    first=first,
                    second=second,
                    game_count=config.preliminary_games_per_pair,
                    config=config,
                )
            )
    return jobs


def _execute_jobs(
    store: LeagueStore,
    jobs: Sequence[GameJob],
    resources: ResourceBudget,
    progress: Callable[[str], None] | None = None,
) -> None:
    if not jobs:
        return
    openings = {job.job_key: job.opening for job in jobs}
    total = len(jobs)
    interval = max(1, min(10, total // 10 or 1))

    def report(finished: int) -> None:
        if progress is not None and (finished == total or finished % interval == 0):
            progress(f"{jobs[0].stage}: finished {finished}/{total} scheduled games")

    if resources.workers == 1:
        for finished, job in enumerate(jobs, 1):
            store.save_game(_play_game(job), job.opening)
            report(finished)
        return
    with ProcessPoolExecutor(max_workers=resources.workers) as executor:
        future_jobs = {executor.submit(_play_game, job): job for job in jobs}
        for finished, future in enumerate(as_completed(future_jobs), 1):
            job = future_jobs[future]
            try:
                record = future.result()
            except BaseException as error:
                state = job.opening.state()
                record = GameRecord(
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
            store.save_game(record, openings[job.job_key])
            report(finished)


@dataclass(frozen=True, slots=True)
class PairEvidence:
    wins: int
    draws: int
    losses: int
    completed_pairs: int
    candidate_failures: int
    worker_failures: int
    case_ids: tuple[str, ...]

    @property
    def points(self) -> float:
        return self.wins + self.draws * 0.5

    @property
    def score_rate(self) -> float:
        return self.points / self.completed_pairs if self.completed_pairs else 0.0


def _row_game_points(row: Mapping[str, Any], profile_id: str) -> float:
    if row["result"] == "1/2-1/2":
        return 0.5
    if row["result"] == "1-0":
        return 1.0 if row["white_profile_id"] == profile_id else 0.0
    if row["result"] == "0-1":
        return 1.0 if row["black_profile_id"] == profile_id else 0.0
    raise ValueError("incomplete game has no match points")


def _pair_evidence(
    rows: Iterable[Mapping[str, Any]],
    profile_id: str,
    *,
    maximum_pairs: int | None = None,
) -> PairEvidence:
    materialized = list(rows)
    candidate_failures = sum(
        row["engine_failure_profile_id"] == profile_id for row in materialized
    )
    worker_failures = sum(
        row["terminal_reason"] == "worker-exception" for row in materialized
    )
    groups: dict[tuple[object, ...], list[Mapping[str, Any]]] = {}
    for row in materialized:
        opponents = tuple(
            sorted((str(row["white_profile_id"]), str(row["black_profile_id"])))
        )
        key = (
            str(row["stage"]),
            opponents,
            int(row["opening_index"]) // 2,
            str(row["opening_case_id"]),
            int(row["seed"]),
        )
        groups.setdefault(key, []).append(row)

    completed: list[tuple[int, str, float]] = []
    for key, paired in groups.items():
        if len(paired) != 2 or any(
            row["result"] not in {"1-0", "0-1", "1/2-1/2"}
            for row in paired
        ):
            continue
        first, second = paired
        if not (
            first["white_profile_id"] == second["black_profile_id"]
            and first["black_profile_id"] == second["white_profile_id"]
        ):
            continue
        pair_points = sum(_row_game_points(row, profile_id) for row in paired)
        completed.append((int(first["opening_index"]) // 2, str(key[3]), pair_points))
    completed.sort(key=lambda item: (item[0], item[1]))
    if maximum_pairs is not None:
        completed = completed[:maximum_pairs]
    case_ids = tuple(item[1] for item in completed)
    # A versioned fixed-suite boundary may contribute at most one pair.
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("fixed-suite evidence contains a duplicate opening case")
    wins = sum(points > 1.0 for _, _, points in completed)
    draws = sum(points == 1.0 for _, _, points in completed)
    losses = sum(points < 1.0 for _, _, points in completed)
    return PairEvidence(
        wins=wins,
        draws=draws,
        losses=losses,
        completed_pairs=len(completed),
        candidate_failures=candidate_failures,
        worker_failures=worker_failures,
        case_ids=case_ids,
    )


def _fitness(
    rows: Iterable[sqlite3.Row], profiles: Sequence[EngineProfile]
) -> list[dict[str, Any]]:
    materialized = list(rows)
    ranking: list[dict[str, Any]] = []
    for profile in profiles:
        evidence = _pair_evidence(materialized, profile.profile_id)
        ranking.append(
            {
                "profile_id": profile.profile_id,
                "points": evidence.points,
                "games": evidence.completed_pairs * 2,
                "pairs": evidence.completed_pairs,
                "wins": evidence.wins,
                "draws": evidence.draws,
                "losses": evidence.losses,
                "technical_failures": evidence.candidate_failures,
                "score_rate": evidence.score_rate,
            }
        )
    ranking.sort(
        key=lambda item: (
            -item["score_rate"],
            -item["pairs"],
            -item["wins"],
            item["losses"],
            item["profile_id"],
        )
    )
    return ranking


def _next_population(
    champion: EngineProfile,
    runner_up: EngineProfile,
    hall_of_fame: Sequence[EngineProfile],
    *,
    size: int,
    seed: int,
    generation: int,
) -> tuple[EngineProfile, ...]:
    profiles: list[EngineProfile] = [champion]
    used = {champion.profile_id}
    if runner_up.profile_id != champion.profile_id and len(profiles) < size:
        child = crossover_profile(
            champion,
            runner_up,
            seed=_stable_seed(seed, generation, "crossover"),
            name=f"generation {generation} champion-partner crossover",
        )
        if child.profile_id not in used:
            profiles.append(child)
            used.add(child.profile_id)
    for profile in hall_of_fame:
        if profile.profile_id not in used and len(profiles) < size:
            profiles.append(profile)
            used.add(profile.profile_id)
    index = 1
    while len(profiles) < size:
        child = mutate_profile(
            champion,
            seed=_stable_seed(seed, generation, "mutation", index),
            name=f"generation {generation} candidate {index}",
            generation=generation,
        )
        index += 1
        if child.profile_id not in used:
            profiles.append(child)
            used.add(child.profile_id)
    return tuple(profiles)


def run_league(
    database: str | Path,
    *,
    config: LeagueConfig | None = None,
    resume_run_id: str | None = None,
    initial_champion: EngineProfile | str | Path | None = None,
    champion_output: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Runs or resumes a deterministic, transactionally checkpointed league."""

    with LeagueStore(database) as store:
        def emit(message: str) -> None:
            if progress is not None:
                progress(message)

        def publish(profile: EngineProfile, run_id: str) -> None:
            if champion_output is not None:
                save_profile(
                    profile,
                    champion_output,
                    provenance=store.champion_provenance(run_id, profile.profile_id),
                )

        if resume_run_id:
            row = store.run_row(resume_run_id)
            if row["source_fingerprint"] != ENGINE_SOURCE_FINGERPRINT:
                raise ValueError(
                    "cannot resume: engine source fingerprint changed; start a new run"
                )
            if row["engine_version"] != ENGINE_VERSION:
                raise ValueError(
                    "cannot resume: engine version changed; start a new run"
                )
            persisted_runtime = json.loads(row["runtime_json"])
            if persisted_runtime != runtime_provenance():
                raise ValueError(
                    "cannot resume: Python or python-chess runtime changed; start a new run"
                )
            stored_config = LeagueConfig.from_dict(json.loads(row["config_json"]))
            if config is not None and config.as_dict() != stored_config.as_dict():
                raise ValueError("resume config does not match the persisted run config")
            config = stored_config
            resources_payload = json.loads(row["resource_json"])
            # Re-detect on resume: hardware availability can shrink.  The new
            # budget may only reduce, never exceed, the persisted plan.
            detected = detect_resource_budget(
                config.requested_workers,
                memory_per_worker_mb=config.memory_per_worker_mb,
                reserve_memory_mb=config.reserve_memory_mb,
            )
            persisted_workers = int(resources_payload["workers"])
            resources = replace(detected, workers=min(detected.workers, persisted_workers))
            run_id = resume_run_id
            store.update_resources(run_id, resources)
            champion = store.load_profile(str(row["champion_profile_id"]))
            start_generation = int(row["current_generation"])
            if row["status"] == "complete":
                publish(champion, run_id)
                emit(f"run {run_id} is complete; champion profile refreshed")
                return store.status(run_id)
        else:
            config = config or LeagueConfig()
            resources = detect_resource_budget(
                config.requested_workers,
                memory_per_worker_mb=config.memory_per_worker_mb,
                reserve_memory_mb=config.reserve_memory_mb,
            )
            if isinstance(initial_champion, (str, Path)):
                champion = load_profile(initial_champion)
            elif isinstance(initial_champion, EngineProfile):
                champion = initial_champion
            else:
                champion = store.active_champion() or baseline_profile()
            store.set_active_champion(champion)
            run_id = store.create_run(config, resources, champion)
            start_generation = 1

        emit(
            f"run {run_id}: {resources.workers} worker(s) from detected CPU and "
            "estimated RAM envelope"
        )
        # Always refresh, even when a stale file already exists.
        publish(champion, run_id)

        last_reason = "no generation completed"
        for generation in range(start_generation, config.generations + 1):
            emit(f"generation {generation}/{config.generations}: preparing population")
            population = store.population(run_id, generation)
            if not population:
                population = create_population(
                    champion, size=config.population_size, seed=config.seed + generation
                )
                store.save_population(
                    run_id, generation, population, champion.profile_id
                )

            stage = f"preliminary-g{generation}"
            jobs = _preliminary_jobs(run_id, generation, population, config)
            completed = store.completed_job_keys(run_id, generation, stage)
            _execute_jobs(
                store,
                [job for job in jobs if job.job_key not in completed],
                resources,
                progress,
            )
            preliminary_rows = store.stage_rows(run_id, generation, stage)
            if any(row["terminal_reason"] == "worker-exception" for row in preliminary_rows):
                last_reason = (
                    "unattributed worker exception in preliminary stage; "
                    "resume will retry the affected job"
                )
                store.update_run(
                    run_id,
                    status="needs-resume",
                    generation=generation,
                    champion_id=champion.profile_id,
                    reason=last_reason,
                )
                break
            ranking = _fitness(preliminary_rows, population)
            store.save_fitness(run_id, generation, ranking)
            candidates = [item for item in ranking if item["profile_id"] != champion.profile_id]
            if not candidates:
                last_reason = "no eligible challenger"
                store.update_run(
                    run_id,
                    status="complete",
                    generation=generation,
                    champion_id=champion.profile_id,
                    reason=last_reason,
                )
                break
            candidate_row = candidates[0]
            candidate = next(
                profile for profile in population if profile.profile_id == candidate_row["profile_id"]
            )
            promotion_stage = f"promotion-g{generation}"
            promotion_jobs = _paired_jobs(
                run_id=run_id,
                generation=generation,
                stage=promotion_stage,
                first=candidate,
                second=champion,
                game_count=config.promotion_games,
                config=config,
            )
            completed = store.completed_job_keys(run_id, generation, promotion_stage)
            _execute_jobs(
                store,
                [job for job in promotion_jobs if job.job_key not in completed],
                resources,
                progress,
            )
            target_pairs = max(
                config.promotion_games, config.minimum_promotion_games
            ) // 2
            replacement_pairs = config.max_replacement_games // 2
            for replacement_index in range(replacement_pairs):
                current_rows = store.stage_rows(
                    run_id, generation, promotion_stage
                )
                evidence = _pair_evidence(
                    current_rows,
                    candidate.profile_id,
                    maximum_pairs=target_pairs,
                )
                if evidence.worker_failures or evidence.completed_pairs >= target_pairs:
                    break
                replacement_jobs = _paired_jobs(
                    run_id=run_id,
                    generation=generation,
                    stage=promotion_stage,
                    first=candidate,
                    second=champion,
                    game_count=2,
                    config=config,
                    pair_offset=config.promotion_games // 2 + replacement_index,
                )
                completed = store.completed_job_keys(
                    run_id, generation, promotion_stage
                )
                _execute_jobs(
                    store,
                    [job for job in replacement_jobs if job.job_key not in completed],
                    resources,
                    progress,
                )
            promotion_rows = store.stage_rows(
                run_id, generation, promotion_stage
            )
            evidence = _pair_evidence(
                promotion_rows,
                candidate.profile_id,
                maximum_pairs=target_pairs,
            )
            if evidence.worker_failures:
                last_reason = (
                    "unattributed worker exception in promotion stage; "
                    "resume will retry the affected job"
                )
                store.update_run(
                    run_id,
                    status="needs-resume",
                    generation=generation,
                    champion_id=champion.profile_id,
                    reason=last_reason,
                )
                break
            gate = run_rules_tactical_gate(
                candidate,
                search_depth=config.search_depth,
                max_series_per_node=config.max_series_per_node,
                max_generation_positions=config.max_generation_positions,
            )
            decision = promotion_decision(
                wins=evidence.wins,
                draws=evidence.draws,
                losses=evidence.losses,
                technical_failures=evidence.candidate_failures,
                gate_passed=gate.passed,
                minimum_games=config.minimum_promotion_games,
            )
            match_id = store.save_match(
                run_id,
                generation,
                candidate.profile_id,
                champion.profile_id,
                gate,
                decision,
                config,
            )
            previous_champion = champion
            if decision.promoted:
                champion = candidate
                store.set_active_champion(champion)
                store.record_lineage(
                    run_id,
                    generation,
                    previous_champion.profile_id,
                    champion.profile_id,
                    match_id,
                    decision.reason,
                )
            last_reason = decision.reason

            next_generation = generation + 1
            if next_generation <= config.generations:
                breeding_partner = (
                    previous_champion if decision.promoted else candidate
                )
                next_population = _next_population(
                    champion,
                    breeding_partner,
                    store.hall_of_fame(),
                    size=config.population_size,
                    seed=config.seed,
                    generation=next_generation,
                )
                store.save_population(
                    run_id,
                    next_generation,
                    next_population,
                    champion.profile_id,
                )
                store.update_run(
                    run_id,
                    status="running",
                    generation=next_generation,
                    champion_id=champion.profile_id,
                    reason=last_reason,
                )
                publish(champion, run_id)
            else:
                store.update_run(
                    run_id,
                    status="complete",
                    generation=generation,
                    champion_id=champion.profile_id,
                    reason=last_reason,
                )

        publish(champion, run_id)
        return store.status(run_id)


def league_status(database: str | Path, run_id: str | None = None) -> dict[str, Any]:
    with LeagueStore(database) as store:
        return store.status(run_id)
