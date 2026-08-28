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

from .model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    Outcome,
    ProgressiveState,
    SeriesResult,
)
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
from .rules import _series_tactical_provenance, generate_series, play_series
from .search import (
    TACTICAL_FINAL_ORDINARY_QUOTA_DENOMINATOR,
    EvaluationOverlay,
    SearchLimits,
    analyze,
)
from .deep_teacher_overlay import (
    DeepTeacherOverlayPayload,
    build_deep_teacher_overlay,
)


LEAGUE_SCHEMA_VERSION = 3
OPENING_SUITE_VERSION = "spc-league-boundaries-v4"
PROMOTION_METHOD = "deterministic-fixed-suite-pairs-v1"

# The human-game regression asks an existential one-series mate question, not
# for a move to publish.  Give the exact native solver enough deterministic
# work to settle the measured fixtures without coupling this internal oracle to
# the public SearchResult publication policy.
HUMAN_FIRST_GAME_REPLY_VERIFIER_WORK_LIMIT = 8_000_000
HUMAN_FIRST_GAME_ROOT_WIDTH = 32
# A complete collect-all depth-2 search of this fixed S4 fixture currently
# consumes 2,139,260 generated positions. Keep a measured safety margin so the
# gate fails on chess evidence instead of its obsolete pre-screening budget.
HUMAN_FIRST_GAME_ROOT_WORK_LIMIT = 3_000_000


@dataclass(frozen=True, slots=True)
class TacticalRefutationAnchor:
    anchor_id: str
    history: tuple[tuple[str, ...], ...]
    blundering_series: tuple[str, ...]
    immediate_reply_mate: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TacticalContenderHypothesis:
    hypothesis_id: str
    series: tuple[str, ...]
    evidence_label: str


HUMAN_FIRST_GAME_REFUTATION = TacticalRefutationAnchor(
    anchor_id="human-e4-kf7-qb6-s5-mate-v1",
    history=(
        ("e2e4",),
        ("f7f6", "e8f7"),
        ("d2d4", "e1d2", "g1f3"),
        ("f6f5", "f5e4", "c7c5", "d8b6"),
    ),
    blundering_series=("f6f5", "f5e4", "c7c5", "d8b6"),
    immediate_reply_mate=("b1c3", "c3e4", "f1b5", "b5d7", "f3e5"),
)

HUMAN_FIRST_GAME_CONTENDER_HYPOTHESES = (
    TacticalContenderHypothesis(
        hypothesis_id="A",
        series=("e7e5", "f6f5", "f5e4", "f8b4"),
        evidence_label=(
            "current-live bounded contender; heuristic hypothesis, not a win proof"
        ),
    ),
    TacticalContenderHypothesis(
        hypothesis_id="B",
        series=("e7e5", "f6f5", "g8f6", "f6e4"),
        evidence_label=(
            "unstable immediate-mate hypothesis; heuristic hypothesis, not a win proof"
        ),
    ),
    TacticalContenderHypothesis(
        hypothesis_id="E",
        series=("d7d5", "d5e4", "e4f3", "g7g5"),
        evidence_label=(
            "root64 depth4 +664 with no bounded immediate reply mate observed; "
            "heuristic hypothesis, not a win proof"
        ),
    ),
)

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
                quiet_series=1,
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
    fast_preselection_finalists: int = 3
    fast_preselection_positions: int = 32
    fast_preselection_rollout_steps: int = 2
    fast_preselection_smoke: bool = False
    promotion_games: int = 20
    max_replacement_games: int = 40
    minimum_promotion_games: int = 20
    search_depth: int = 2
    max_series_per_node: int = 32
    max_generation_positions: int = 250_000
    max_game_work_positions: int | None = None
    emergency_max_series: int | None = None
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
        if not 1 <= self.fast_preselection_finalists <= 16:
            raise ValueError("fast_preselection_finalists must be between 1 and 16")
        if not 1 <= self.fast_preselection_positions <= 64:
            raise ValueError("fast_preselection_positions must be between 1 and 64")
        if (
            not self.fast_preselection_smoke
            and self.fast_preselection_positions < 32
        ):
            raise ValueError(
                "non-smoke fast preselection requires 30 opening boundaries "
                "plus two tactical anchors (32 corpus positions)"
            )
        if not 1 <= self.fast_preselection_rollout_steps <= 4:
            raise ValueError("fast_preselection_rollout_steps must be between 1 and 4")
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
        if (
            self.max_game_work_positions is not None
            and self.max_game_work_positions < 1_000
        ):
            raise ValueError("max_game_work_positions must be at least 1000")
        if self.emergency_max_series is not None and self.emergency_max_series < 18:
            raise ValueError("emergency_max_series must be at least 18")
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
            fast_preselection_finalists=1,
            fast_preselection_positions=4,
            fast_preselection_rollout_steps=1,
            fast_preselection_smoke=True,
            promotion_games=2,
            max_replacement_games=4,
            minimum_promotion_games=20,
            search_depth=1,
            max_series_per_node=2,
            max_generation_positions=5_000,
            max_game_work_positions=None,
            emergency_max_series=None,
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
            "branch_cap_complete_series_per_node": self.max_series_per_node,
            "max_work_positions_per_search": self.max_generation_positions,
            "max_game_work_positions": self.max_game_work_positions,
            "game_work_definition": (
                "deterministic logical positions across complete-series generation, "
                "evaluation reach, and quiet adjudication over the whole game"
            ),
            "emergency_max_series": self.emergency_max_series,
            "series_number_limit": (
                "unbounded" if self.emergency_max_series is None else "technical-watchdog"
            ),
            "time_limit_seconds": None,
            "fresh_searcher_each_series": True,
            "collect_all_root_scores": False,
            "root_score_mode": "best-only-play-optimized",
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
    max_game_work_positions: int | None
    emergency_max_series: int | None
    opening_suite_version: str = OPENING_SUITE_VERSION
    white_evaluation_overlay: DeepTeacherOverlayPayload | None = None
    black_evaluation_overlay: DeepTeacherOverlayPayload | None = None


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
    engine_failure_engine_id: str | None = None


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


def _technical_failure(
    job: GameJob,
    state: ProgressiveState,
    trace: Sequence[dict[str, Any]],
    failing_profile_id: str,
    reason: str,
    *,
    error: str | None = None,
    failing_engine_id: str | None = None,
) -> GameRecord:
    return GameRecord(
        job.job_key,
        job.run_id,
        job.generation,
        job.stage,
        job.opening_index,
        job.opening.case_id,
        job.opening_suite_version,
        job.seed,
        job.white_profile.profile_id,
        job.black_profile.profile_id,
        "*",
        reason,
        None,
        failing_profile_id,
        job.opening.state().pfen,
        state.pfen,
        sum(bool(item.get("played", True)) for item in trace),
        tuple(trace),
        error,
        failing_engine_id,
    )


def _play_game(job: GameJob) -> GameRecord:
    state = job.opening.state()
    start_pfen = state.pfen
    trace: list[dict[str, Any]] = []
    game_work_positions = 0

    try:
        white_overlay = (
            None
            if job.white_evaluation_overlay is None
            else build_deep_teacher_overlay(
                job.white_evaluation_overlay,
                job.white_profile,
            )
        )
        black_overlay = (
            None
            if job.black_evaluation_overlay is None
            else build_deep_teacher_overlay(
                job.black_evaluation_overlay,
                job.black_profile,
            )
        )
    except BaseException as error:
        failing_profile = (
            job.white_profile
            if job.white_evaluation_overlay is not None
            else job.black_profile
        )
        return _technical_failure(
            job,
            state,
            trace,
            failing_profile.profile_id,
            "engine-overlay-invalid",
            error=f"{type(error).__name__}: {error}",
            failing_engine_id=(
                job.white_evaluation_overlay.variant_id
                if job.white_evaluation_overlay is not None
                else job.black_evaluation_overlay.variant_id
            ),
        )

    def technical_incomplete(reason: str) -> GameRecord:
        return GameRecord(
            job.job_key,
            job.run_id,
            job.generation,
            job.stage,
            job.opening_index,
            job.opening.case_id,
            job.opening_suite_version,
            job.seed,
            job.white_profile.profile_id,
            job.black_profile.profile_id,
            "*",
            reason,
            None,
            None,
            start_pfen,
            state.pfen,
            sum(bool(item.get("played", True)) for item in trace),
            tuple(trace),
        )

    while (
        job.emergency_max_series is None
        or state.series_number <= job.emergency_max_series
    ):
        mover = state.board.turn
        profile = job.white_profile if mover == chess.WHITE else job.black_profile
        evaluation_overlay = white_overlay if mover == chess.WHITE else black_overlay
        remaining_game_work = (
            None
            if job.max_game_work_positions is None
            else job.max_game_work_positions - game_work_positions
        )
        if remaining_game_work is not None and remaining_game_work <= 0:
            return technical_incomplete("technical-game-work-budget-exhausted")
        search_work_limit = (
            job.max_generation_positions
            if remaining_game_work is None
            else min(job.max_generation_positions, remaining_game_work)
        )
        reduced_for_game_budget = (
            remaining_game_work is not None
            and search_work_limit < job.max_generation_positions
        )
        try:
            result = analyze(
                state,
                SearchLimits(
                    depth_series=job.search_depth,
                    max_series_per_node=job.max_series_per_node,
                    time_limit_seconds=None,
                    max_generation_positions=search_work_limit,
                    collect_all_root_scores=False,
                ),
                profile=profile,
                evaluation_overlay=evaluation_overlay,
            )
        except BaseException as error:
            return _technical_failure(
                job,
                state,
                trace,
                profile.profile_id,
                "engine-exception",
                error=f"{type(error).__name__}: {error}",
                failing_engine_id=(
                    profile.profile_id
                    if evaluation_overlay is None
                    else evaluation_overlay.variant_id
                ),
            )

        expected_engine_profile_id = (
            profile.profile_id
            if evaluation_overlay is None
            else evaluation_overlay.variant_id
        )
        if (
            evaluation_overlay is not None
            and result.engine_profile_id != expected_engine_profile_id
        ):
            return _technical_failure(
                job,
                state,
                trace,
                profile.profile_id,
                "engine-identity-mismatch",
                error=(
                    f"expected {expected_engine_profile_id}, "
                    f"received {result.engine_profile_id}"
                ),
                failing_engine_id=expected_engine_profile_id,
            )

        stats = getattr(result, "stats", None)
        search_work = int(
            getattr(stats, "work_positions", getattr(stats, "generation_positions", 0))
        )
        game_work_positions += search_work
        selected = result.best_series
        attempted_trace = {
            "series_number": state.series_number,
            "profile_id": profile.profile_id,
            "series": selected.machine_notation if selected else None,
            "notation": selected.notation if selected else None,
            "score_white_heuristic_points": getattr(result, "score", None),
            "completed_depth": getattr(result, "completed_depth", 0),
            "exact_width": getattr(result, "exact_width", False),
            "nodes": int(getattr(stats, "nodes", 0)),
            "root_scores_complete": getattr(result, "root_scores_complete", False),
            "root_bound_candidates": int(
                getattr(stats, "root_bound_candidates", 0)
            ),
            "work_positions": search_work,
            "series_generation_positions": int(
                getattr(stats, "series_generation_positions", 0)
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
            "evaluation_reach_positions": int(
                getattr(stats, "evaluation_reach_positions", 0)
            ),
            "evaluation_capture_positions": int(
                getattr(stats, "evaluation_capture_positions", 0)
            ),
            "tactical_leaf_extensions": int(
                getattr(stats, "tactical_leaf_extensions", 0)
            ),
            "quiet_adjudication_positions": int(
                getattr(stats, "quiet_adjudication_positions", 0)
            ),
            "game_work_positions": game_work_positions,
            "search_work_limit": search_work_limit,
            "reduced_for_game_budget": reduced_for_game_budget,
            "work_limit_reached": result.work_limit_reached,
            "outcome": (
                selected.outcome.value if selected and selected.outcome else None
            ),
            "played": False,
        }
        if evaluation_overlay is not None:
            attempted_trace.update(
                {
                    "engine_variant_id": evaluation_overlay.variant_id,
                    "deep_teacher_model_id": evaluation_overlay.payload.model_id,
                    "deep_teacher_model_sha256": (
                        evaluation_overlay.payload.model_sha256
                    ),
                    "deep_teacher_corpus_semantic_sha256": (
                        evaluation_overlay.payload.teacher_corpus_semantic_sha256
                    ),
                    "deep_teacher_corpus_raw_artifact_sha256": (
                        evaluation_overlay.payload.teacher_corpus_raw_artifact_sha256
                    ),
                    "deep_teacher_native_source_identity": (
                        evaluation_overlay.payload.native_source_identity
                    ),
                    "deep_teacher_score_policy": (
                        evaluation_overlay.payload.score_policy
                    ),
                    "deep_teacher_work_policy": (
                        evaluation_overlay.payload.work_policy
                    ),
                    "overlay_evaluations": int(
                        getattr(stats, "overlay_evaluations", 0)
                    ),
                    "overlay_reach_positions": int(
                        getattr(stats, "overlay_reach_positions", 0)
                    ),
                    "overlay_direct_move_variants": int(
                        getattr(stats, "overlay_direct_move_variants", 0)
                    ),
                    "overlay_two_move_variants": int(
                        getattr(stats, "overlay_two_move_variants", 0)
                    ),
                }
            )
        if evaluation_overlay is not None and result.work_limit_reached:
            trace.append(attempted_trace)
            return _technical_failure(
                job,
                state,
                trace,
                profile.profile_id,
                "engine-work-limit",
                failing_engine_id=evaluation_overlay.variant_id,
            )
        if result.adjudication_status == "manual-proof-required":
            # A legal move-only fallback keeps interactive engine play live,
            # but it is not a scored minimax choice. A self-play game that
            # needed one cannot become strength or promotion evidence later,
            # even if the fallback resets the quiet clock and play continues.
            trace.append(attempted_trace)
            return technical_incomplete("manual-adjudication-pending")
        if reduced_for_game_budget and result.work_limit_reached:
            trace.append(attempted_trace)
            return technical_incomplete("technical-game-work-budget-exhausted")

        if result.best_series is None:
            proven_draw = result.proof == "draw" and result.adjudication_status == "proven-draw-no-mating-material"
            if not proven_draw:
                if stats is not None:
                    trace.append(attempted_trace)
                reason = (
                    "engine-timeout"
                    if result.timed_out
                    else "engine-work-limit"
                    if result.work_limit_reached
                    else "engine-no-move"
                )
                return _technical_failure(
                    job,
                    state,
                    trace,
                    profile.profile_id,
                    reason,
                    failing_engine_id=expected_engine_profile_id,
                )
            return GameRecord(
                job.job_key, job.run_id, job.generation, job.stage,
                job.opening_index, job.opening.case_id, job.opening_suite_version,
                job.seed, job.white_profile.profile_id, job.black_profile.profile_id,
                "1/2-1/2", "proven-draw-no-mating-material", None, None,
                start_pfen, state.pfen, len(trace), tuple(trace),
            )

        attempted_trace["played"] = True
        trace.append(attempted_trace)
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
                job.opening_suite_version,
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
                job.opening_suite_version,
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

        if (
            job.max_game_work_positions is not None
            and game_work_positions >= job.max_game_work_positions
        ):
            return technical_incomplete("technical-game-work-budget-exhausted")

    return technical_incomplete("technical-emergency-series-watchdog-exhausted")


def _replay_tactical_refutation_anchor(
    anchor: TacticalRefutationAnchor = HUMAN_FIRST_GAME_REFUTATION,
) -> tuple[ProgressiveState, SeriesResult, SeriesResult, tuple[str, ...]]:
    """Authoritatively replays the stored game through its immediate mate."""

    if not anchor.history or anchor.history[-1] != anchor.blundering_series:
        raise AssertionError("tactical refutation must end its history at the blunder")

    state = ProgressiveState.initial()
    replayed_history: list[str] = []
    state_before_blunder: ProgressiveState | None = None
    blunder_result: SeriesResult | None = None
    for index, moves in enumerate(anchor.history):
        if index == len(anchor.history) - 1:
            state_before_blunder = state
        result = play_series(state, moves)
        if result.machine_notation != "/".join(moves):
            raise AssertionError("tactical refutation history is not canonical")
        replayed_history.append(result.machine_notation)
        state = result.final_state
        if index == len(anchor.history) - 1:
            blunder_result = result

    assert state_before_blunder is not None and blunder_result is not None
    if (
        state_before_blunder.series_number != 4
        or state_before_blunder.board.turn != chess.BLACK
        or blunder_result.outcome is not None
        or state.series_number != 5
        or state.board.turn != chess.WHITE
    ):
        raise AssertionError("tactical refutation must reach Black S4 then White S5")

    mate_result = play_series(state, anchor.immediate_reply_mate)
    if (
        mate_result.machine_notation != "/".join(anchor.immediate_reply_mate)
        or mate_result.outcome != Outcome.CHECKMATE
        or not mate_result.ended_by_check
        or mate_result.used_moves != 5
    ):
        raise AssertionError("tactical refutation reply must be an immediate S5 mate")
    return (
        state_before_blunder,
        blunder_result,
        mate_result,
        tuple(replayed_history),
    )


def _analyze_gate_position(
    state: ProgressiveState,
    limits: SearchLimits,
    profile: EngineProfile,
    evaluation_overlay: EvaluationOverlay | None,
):
    return analyze(
        state,
        limits,
        profile=profile,
        evaluation_overlay=evaluation_overlay,
    )


def _one_series_reply_verifier(
    state: ProgressiveState,
    profile: EngineProfile,
    evaluation_overlay: EvaluationOverlay | None,
) -> dict[str, Any]:
    """Settles the gate's one-series mate question without publishing a move."""

    del profile, evaluation_overlay
    from .series_mate import SeriesMateStatus, find_native_series_mate

    probe = find_native_series_mate(
        state,
        max_positions=None,
        max_work=HUMAN_FIRST_GAME_REPLY_VERIFIER_WORK_LIMIT,
        time_limit_seconds=None,
    )
    selected = probe.series
    replayed: SeriesResult | None = None
    replay_error: str | None = None
    if selected is not None:
        try:
            replayed = play_series(state, selected.moves)
        except BaseException as error:  # pragma: no cover - engine evidence is legal
            replay_error = f"{type(error).__name__}: {error}"
    completed = bool(
        replay_error is None
        and (
            probe.status is SeriesMateStatus.EXHAUSTED
            or probe.status is SeriesMateStatus.FOUND
            and replayed is not None
            and replayed.outcome is Outcome.CHECKMATE
            and replayed.ended_by_check
        )
    )
    mate_found = bool(
        probe.status is SeriesMateStatus.FOUND
        and completed
        and replayed is not None
        and replayed.outcome == Outcome.CHECKMATE
        and replayed.ended_by_check
    )
    return {
        "completed": completed,
        "mate_found": mate_found,
        "selected_reply": (
            None if selected is None else selected.machine_notation
        ),
        "replayed_outcome": (
            None
            if replayed is None or replayed.outcome is None
            else replayed.outcome.value
        ),
        "replay_error": replay_error,
        "requested_depth": 1,
        "completed_depth": 1 if completed else 0,
        "timed_out": probe.status is SeriesMateStatus.DEADLINE,
        "work_limit_reached": probe.status is SeriesMateStatus.WORK_LIMIT,
        "work_positions": probe.positions_visited + probe.moves_generated,
        "proof": (
            "white" if state.board.turn == chess.WHITE else "black"
        ) if mate_found else None,
        "verifier_kind": "exact-native-one-series",
        "verifier_status": str(probe.status),
        "verifier_positions": probe.positions_visited,
        "verifier_edges": probe.moves_generated,
        "verifier_width": None,
        "verifier_work_limit": HUMAN_FIRST_GAME_REPLY_VERIFIER_WORK_LIMIT,
        "exact_width": probe.status is SeriesMateStatus.EXHAUSTED,
    }


def _evaluate_human_first_game_refutation(
    profile: EngineProfile,
    *,
    evaluation_overlay: EvaluationOverlay | None = None,
) -> dict[str, Any]:
    """Rejects the live S4 blunder and certifies retained-set move quality.

    Immediate reply-mate existence is settled by the exact native one-series
    solver. The heuristic comparison is scoped to the fully scored retained
    root set; that move-quality comparison is not a forced-win proof.
    """

    if (
        evaluation_overlay is not None
        and evaluation_overlay.base_profile_id != profile.profile_id
    ):
        raise ValueError(
            "first-game refutation overlay is bound to a different profile"
        )

    root_state, blunder, canonical_mate, replayed_history = (
        _replay_tactical_refutation_anchor()
    )
    contender_hypotheses: list[dict[str, Any]] = []
    for hypothesis in HUMAN_FIRST_GAME_CONTENDER_HYPOTHESES:
        replayed_hypothesis = play_series(root_state, hypothesis.series)
        if replayed_hypothesis.machine_notation != "/".join(hypothesis.series):
            raise AssertionError("first-game contender hypothesis is not canonical")
        contender_hypotheses.append(
            {
                "hypothesis_id": hypothesis.hypothesis_id,
                "series": replayed_hypothesis.machine_notation,
                "final_position_hash": replayed_hypothesis.final_state.position_hash,
                "evidence_label": hypothesis.evidence_label,
                "pass_required": False,
            }
        )
    blunder_verifier = _one_series_reply_verifier(
        blunder.final_state,
        profile,
        evaluation_overlay,
    )
    root_limits = SearchLimits(
        depth_series=2,
        max_series_per_node=HUMAN_FIRST_GAME_ROOT_WIDTH,
        time_limit_seconds=None,
        max_generation_positions=HUMAN_FIRST_GAME_ROOT_WORK_LIMIT,
        collect_all_root_scores=True,
        native_threads=1,
    )
    root_result = _analyze_gate_position(
        root_state,
        root_limits,
        profile,
        evaluation_overlay,
    )
    selected = root_result.best_series
    selected_notation = None if selected is None else selected.machine_notation
    retained = tuple(root_result.alternatives)
    selected_entry = next(
        (
            item
            for item in retained
            if selected is not None and item.series.moves == selected.moves
        ),
        None,
    )
    root_complete = (
        root_result.requested_depth == 2
        and root_result.completed_depth == 2
        and not root_result.timed_out
        and not root_result.work_limit_reached
        and root_result.root_scores_complete
        and selected_entry is not None
        and bool(retained)
    )

    tactical_count = sum(
        bool(_series_tactical_provenance(root_state, item.series))
        for item in retained
    )
    non_tactical_count = len(retained) - tactical_count
    ordinary_quota_slots = min(
        HUMAN_FIRST_GAME_ROOT_WIDTH,
        max(
            1,
            (
                HUMAN_FIRST_GAME_ROOT_WIDTH
                + TACTICAL_FINAL_ORDINARY_QUOTA_DENOMINATOR
                - 1
            )
            // TACTICAL_FINAL_ORDINARY_QUOTA_DENOMINATOR,
        ),
    )
    tactical_reserve_slots = HUMAN_FIRST_GAME_ROOT_WIDTH - ordinary_quota_slots
    mover_is_white = root_state.board.turn == chess.WHITE
    selected_score = None if selected_entry is None else selected_entry.score
    best_retained_score = (
        None
        if not retained
        else (
            max(item.score for item in retained)
            if mover_is_white
            else min(item.score for item in retained)
        )
    )
    selected_is_best_retained = bool(
        root_complete and selected_score == best_retained_score
    )

    strictly_better = ()
    if selected_score is not None:
        strictly_better = tuple(
            item
            for item in retained
            if (
                item.score > selected_score
                if mover_is_white
                else item.score < selected_score
            )
        )
    reply_screens: list[dict[str, Any]] = []
    candidates_to_screen = tuple(
        (item.series, item.score) for item in strictly_better
    ) + (((selected, selected_score),) if selected is not None else ())
    for candidate, score in candidates_to_screen:
        assert candidate is not None
        replayed_candidate = play_series(root_state, candidate.moves)
        reply_screens.append(
            {
                "series": candidate.machine_notation,
                "score_white_heuristic_points": score,
                "is_selected": candidate.moves == selected.moves,
                "reply_verifier": _one_series_reply_verifier(
                    replayed_candidate.final_state,
                    profile,
                    evaluation_overlay,
                ),
            }
        )

    selected_screen = next(
        (item for item in reply_screens if item["is_selected"]),
        None,
    )
    selected_reply_safe = bool(
        selected_screen is not None
        and selected_screen["reply_verifier"]["completed"]
        and not selected_screen["reply_verifier"]["mate_found"]
    )
    better_screen_incomplete = any(
        not item["reply_verifier"]["completed"]
        for item in reply_screens
        if not item["is_selected"]
    )
    better_safe_candidate = any(
        item["reply_verifier"]["completed"]
        and not item["reply_verifier"]["mate_found"]
        for item in reply_screens
        if not item["is_selected"]
    )
    selected_is_best_retained_safe = bool(
        selected_reply_safe
        and not better_screen_incomplete
        and not better_safe_candidate
        and selected_is_best_retained
    )
    fixture_calibrated = bool(
        blunder_verifier["completed"] and blunder_verifier["mate_found"]
    )
    avoided_blunder = bool(
        selected is not None and selected.moves != blunder.moves
    )
    reply_safety_passed = bool(
        fixture_calibrated
        and root_complete
        and avoided_blunder
        and selected_reply_safe
    )
    retained_move_quality_passed = bool(
        root_complete and selected_is_best_retained_safe
    )

    return {
        "name": HUMAN_FIRST_GAME_REFUTATION.anchor_id,
        "passed": reply_safety_passed and retained_move_quality_passed,
        "evidence": {
            "canonical_history": replayed_history,
            "root_position_hash": root_state.position_hash,
            "blundering_series": blunder.machine_notation,
            "canonical_immediate_mate": canonical_mate.machine_notation,
            "canonical_terminal": canonical_mate.outcome.value,
            "contender_hypotheses": contender_hypotheses,
            "blunder_reply_verifier": blunder_verifier,
            "selected_series": selected_notation,
            "selected_score_white_heuristic_points": selected_score,
            "best_retained_score_white_heuristic_points": best_retained_score,
            "avoided_blunder": avoided_blunder,
            "root_search_complete": root_complete,
            "root_scores_complete": root_result.root_scores_complete,
            "retained_candidates": len(retained),
            "retained_tactical_provenance_candidates": tactical_count,
            "retained_non_tactical_provenance_candidates": non_tactical_count,
            "both_provenance_classes_present": (
                tactical_count > 0 and non_tactical_count > 0
            ),
            "selector_ordinary_static_quota_slots": ordinary_quota_slots,
            "selector_tactical_reserve_slots": tactical_reserve_slots,
            "selected_is_best_retained_by_score": selected_is_best_retained,
            "selected_is_best_retained_safe": selected_is_best_retained_safe,
            "reply_safety_passed": reply_safety_passed,
            "retained_move_quality_passed": retained_move_quality_passed,
            "screened_selected_and_strictly_better": reply_screens,
            "quality_scope": (
                "best exact-one-series-mate-screened heuristic continuation "
                "among the fully scored retained width-32 root set; not a "
                "forced-win proof"
            ),
            "class_coverage_scope": (
                "the 16/16 slot counts are the mixed-selector contract; candidate "
                "provenance is observed here, while per-candidate lane attribution "
                "belongs to selector unit tests because SearchResult omits it"
            ),
            "proof": root_result.proof,
            "exact_width": root_result.exact_width,
            "root_work_limit": HUMAN_FIRST_GAME_ROOT_WORK_LIMIT,
            "work_positions": root_result.stats.work_positions,
        },
    }


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

    try:
        checks.append(
            _evaluate_human_first_game_refutation(profile)
        )
    except BaseException as error:
        checks.append(
            {
                "name": HUMAN_FIRST_GAME_REFUTATION.anchor_id,
                "passed": False,
                "error": f"{type(error).__name__}: {error}",
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
                population_slot INTEGER,
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
        population_columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(run_population)")
        }
        if "population_slot" not in population_columns:
            self.connection.execute(
                "ALTER TABLE run_population ADD COLUMN population_slot INTEGER"
            )
        # Old databases still retain insertion order through SQLite rowid.  Capture
        # that order once so reopening a stopped run regenerates the exact same
        # profile pair orientation, opening order, and deterministic job keys.
        missing_slots = self.connection.execute(
            """
            SELECT rowid,run_id,generation FROM run_population
            WHERE population_slot IS NULL
            ORDER BY run_id,generation,rowid
            """
        ).fetchall()
        group: tuple[str, int] | None = None
        slot = 0
        for row in missing_slots:
            row_group = (str(row["run_id"]), int(row["generation"]))
            if row_group != group:
                group = row_group
                slot = 0
            self.connection.execute(
                "UPDATE run_population SET population_slot=? WHERE rowid=?",
                (slot, int(row["rowid"])),
            )
            slot += 1
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
        profile_ids = [profile.profile_id for profile in profiles]
        if not profiles:
            raise ValueError("population cannot be empty")
        if len(set(profile_ids)) != len(profile_ids):
            raise ValueError("population profile_ids must be unique")
        if champion_id not in profile_ids:
            raise ValueError("population must contain its champion")
        # One transaction replaces the whole generation. A crash can leave
        # either the previous complete population or the new complete one,
        # never a prefix that resume mistakes for a finished population.
        with self.connection:
            for profile in profiles:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO profiles(
                        profile_id,name,generation,profile_json,created_at
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        profile.profile_id,
                        profile.name,
                        profile.generation,
                        json.dumps(
                            profile.as_dict(),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        _now(),
                    ),
                )
            self.connection.execute(
                "DELETE FROM run_population WHERE run_id=? AND generation=?",
                (run_id, generation),
            )
            self.connection.executemany(
                """
                INSERT INTO run_population(
                    run_id,generation,profile_id,role,population_slot
                ) VALUES(?,?,?,?,?)
                """,
                [
                    (
                        run_id,
                        generation,
                        profile.profile_id,
                        (
                            "champion"
                            if profile.profile_id == champion_id
                            else "challenger"
                        ),
                        population_slot,
                    )
                    for population_slot, profile in enumerate(profiles)
                ],
            )

    def population(
        self,
        run_id: str,
        generation: int,
        *,
        expected_size: int | None = None,
        champion_id: str | None = None,
    ) -> tuple[EngineProfile, ...]:
        rows = self.connection.execute(
            """
            SELECT rp.profile_id,rp.role,rp.population_slot,p.profile_json
            FROM run_population rp
            JOIN profiles p ON p.profile_id=rp.profile_id
            WHERE rp.run_id=? AND rp.generation=?
            ORDER BY rp.population_slot,rp.rowid
            """,
            (run_id, generation),
        ).fetchall()
        if not rows:
            return ()
        slots = [int(row["population_slot"]) for row in rows]
        profile_ids = [str(row["profile_id"]) for row in rows]
        expected_slots = list(range(len(rows)))
        if slots != expected_slots:
            raise ValueError(
                "persisted population slots are not contiguous; refusing unsafe resume"
            )
        if len(set(profile_ids)) != len(profile_ids):
            raise ValueError(
                "persisted population contains duplicate profile_ids; refusing resume"
            )
        if expected_size is not None and len(rows) != expected_size:
            raise ValueError(
                f"persisted population is incomplete ({len(rows)}/{expected_size}); "
                "refusing unsafe resume"
            )
        if champion_id is not None:
            champions = [
                str(row["profile_id"]) for row in rows if row["role"] == "champion"
            ]
            if champions != [champion_id]:
                raise ValueError(
                    "persisted population champion role is inconsistent; "
                    "refusing unsafe resume"
                )
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
            "branch_cap_complete_series_per_node": config.max_series_per_node,
            "max_work_positions_per_search": config.max_generation_positions,
            "max_game_work_positions": config.max_game_work_positions,
            "game_work_definition": (
                "complete-series generation plus evaluation reach plus quiet adjudication"
            ),
            "emergency_max_series": config.emergency_max_series,
            "time_limit_seconds": None,
            "same_limits_for_both_profiles": True,
            "fresh_searcher_each_series": True,
            "collect_all_root_scores": False,
            "root_score_mode": "best-only-play-optimized",
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
        config.opening_suite_version,
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
                    config.max_game_work_positions,
                    config.emergency_max_series,
                    config.opening_suite_version,
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
                    job.opening_suite_version,
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
    materialized = [
        row
        for row in rows
        if profile_id
        in (str(row["white_profile_id"]), str(row["black_profile_id"]))
    ]
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

    completed: list[tuple[str, tuple[str, ...], int, str, float]] = []
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
        completed.append(
            (
                str(key[0]),
                key[1],
                int(first["opening_index"]) // 2,
                str(key[3]),
                pair_points,
            )
        )
    completed.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    if maximum_pairs is not None:
        completed = completed[:maximum_pairs]
    case_ids = tuple(item[3] for item in completed)
    # Within one head-to-head stage, a fixed-suite boundary contributes at most
    # one swapped pair. Round-robin fitness may reuse it against another rival.
    boundaries = tuple((item[0], item[1], item[3]) for item in completed)
    if len(set(boundaries)) != len(boundaries):
        raise ValueError("fixed-suite evidence contains a duplicate opening case")
    wins = sum(item[4] > 1.0 for item in completed)
    draws = sum(item[4] == 1.0 for item in completed)
    losses = sum(item[4] < 1.0 for item in completed)
    return PairEvidence(
        wins=wins,
        draws=draws,
        losses=losses,
        completed_pairs=len(completed),
        candidate_failures=candidate_failures,
        worker_failures=worker_failures,
        case_ids=case_ids,
    )


def _mate_efficiency(
    rows: Iterable[Mapping[str, Any]], profile_id: str
) -> dict[str, Any]:
    """Returns checkmate speed/resistance only from complete swapped pairs."""

    groups: dict[tuple[object, ...], list[Mapping[str, Any]]] = {}
    for row in rows:
        opponents = tuple(
            sorted((str(row["white_profile_id"]), str(row["black_profile_id"])))
        )
        if profile_id not in opponents:
            continue
        key = (
            str(row["stage"]),
            opponents,
            int(row["opening_index"]) // 2,
            str(row["opening_case_id"]),
            int(row["seed"]),
        )
        groups.setdefault(key, []).append(row)

    wins: list[int] = []
    losses: list[int] = []
    balanced_games = 0
    for paired in groups.values():
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
        balanced_games += 2
        for row in paired:
            if row["terminal_reason"] != "checkmate":
                continue
            try:
                length = int(row["series_played"])
                decisive_profile_id = row["decisive_profile_id"]
            except (KeyError, IndexError):
                continue
            if decisive_profile_id == profile_id:
                wins.append(length)
            elif decisive_profile_id is not None:
                losses.append(length)
    return {
        "balanced_pair_games": balanced_games,
        "checkmate_wins": len(wins),
        "average_winning_mate_series": (
            sum(wins) / len(wins) if wins else None
        ),
        "checkmate_losses": len(losses),
        "average_losing_resistance_series": (
            sum(losses) / len(losses) if losses else None
        ),
    }


def _fitness(
    rows: Iterable[sqlite3.Row], profiles: Sequence[EngineProfile]
) -> list[dict[str, Any]]:
    materialized = list(rows)
    ranking: list[dict[str, Any]] = []
    for profile in profiles:
        evidence = _pair_evidence(materialized, profile.profile_id)
        efficiency = _mate_efficiency(materialized, profile.profile_id)
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
                "mate_efficiency": efficiency,
            }
        )
    ranking.sort(
        key=lambda item: (
            -item["score_rate"],
            -item["pairs"],
            -item["wins"],
            item["losses"],
            item["technical_failures"],
            (
                item["mate_efficiency"]["average_winning_mate_series"]
                if item["mate_efficiency"]["average_winning_mate_series"]
                is not None
                else float("inf")
            ),
            -(
                item["mate_efficiency"]["average_losing_resistance_series"]
                or 0.0
            ),
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
            population = store.population(
                run_id,
                generation,
                expected_size=config.population_size,
                champion_id=champion.profile_id,
            )
            if not population:
                population = create_population(
                    champion, size=config.population_size, seed=config.seed + generation
                )
                store.save_population(
                    run_id, generation, population, champion.profile_id
                )

            # Cached feature scoring replaces the O(population^2) all-play-all
            # preliminary games. Its proxy is never stored as WDL/fitness and
            # never changes the strict full-game promotion decision below.
            from .fast_training import FastTrainingConfig, run_fast_preselection

            funnel_root = Path(f"{Path(database).expanduser().resolve()}.fast-training")
            funnel_config = FastTrainingConfig(
                position_limit=config.fast_preselection_positions,
                rollout_steps=config.fast_preselection_rollout_steps,
                label_depth_series=2,
                label_branch_cap=max(4, min(8, config.max_series_per_node)),
                label_max_work_positions=max(
                    200_000, config.max_generation_positions
                ),
                finalist_count=min(
                    config.fast_preselection_finalists,
                    max(1, config.population_size - 1),
                ),
                seed=_stable_seed(config.seed, generation, "fast-preselection"),
                smoke=config.fast_preselection_smoke,
            )
            report, resumed_funnel = run_fast_preselection(
                population,
                champion,
                cache_path=funnel_root / f"{run_id}-g{generation}-cache.json",
                report_path=funnel_root / f"{run_id}-g{generation}-report.json",
                config=funnel_config,
                preliminary_games_per_pair=config.preliminary_games_per_pair,
                promotion_games=config.promotion_games,
            )
            performance = report["performance"]
            schedule = report["full_game_schedule"]
            emit(
                f"fast-preselection-g{generation}: "
                f"{performance['candidate_iterations_per_second']:.0f} cached "
                f"profiles/s; avoided {schedule['games_avoided']} scheduled full "
                f"games; {'resumed' if resumed_funnel else 'fresh'} evidence"
            )
            finalist_ids = list(report["finalist_profile_ids"])
            if not finalist_ids:
                last_reason = (
                    "no challenger passed cached tactical preselection; proxy is "
                    "not game-strength evidence"
                )
                store.update_run(
                    run_id,
                    status="complete",
                    generation=generation,
                    champion_id=champion.profile_id,
                    reason=last_reason,
                )
                break
            candidate_id = finalist_ids[0]
            candidate = next(
                profile for profile in population if profile.profile_id == candidate_id
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
