from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
import time

import chess

from .evaluation import EvaluationBreakdown, classify_score, evaluate, fast_evaluate
from .model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    Outcome,
    ProgressiveState,
    SeriesResult,
)
from .profiles import EngineProfile, baseline_profile
from .rules import (
    GenerationCancelled,
    GenerationStats,
    GenerationWorkLimit,
    _legal_move_variants,
    generate_series,
    quiet_adjudication_status,
)


MATE_SCORE = 1_000_000
UNKNOWN_PROOF_BOUNDS = (-1, 1)
# The ten-quiet-series mate exception is a proof search inside the ordinary
# series search.  It must have a search-wide ceiling of its own: otherwise a
# wide node can start one 100k-node probe per child without any of that work
# appearing in ``max_generation_positions``.  Exhaustion is conservative --
# it yields manual-proof-required, never a fabricated draw.
QUIET_ADJUDICATION_POSITION_LIMIT = 4_096
# Complete progressive series are far costlier to generate than orthodox
# single moves. Iterative deepening revisits the same boundary frontiers, but
# retaining every frontier would multiply memory across league workers. Bound
# reuse by the number of retained SeriesResult objects, not by node count.
SERIES_GENERATION_CACHE_CAPACITY = 4_096


def _proof_from_bounds(bounds: tuple[int, int]) -> str | None:
    if bounds[0] != bounds[1]:
        return None
    return {-1: "black", 0: "draw", 1: "white"}[bounds[0]]


class Bound(StrEnum):
    EXACT = "exact"
    LOWER = "lower"
    UPPER = "upper"


@dataclass(frozen=True, slots=True)
class SearchLimits:
    depth_series: int = 1
    max_series_per_node: int | None = None
    time_limit_seconds: float | None = None
    max_generation_positions: int | None = None
    collect_all_root_scores: bool = True

    def __post_init__(self) -> None:
        if self.depth_series < 1:
            raise ValueError("depth_series must be at least 1")
        if self.max_series_per_node is not None and self.max_series_per_node < 1:
            raise ValueError("max_series_per_node must be positive")
        if self.time_limit_seconds is not None and self.time_limit_seconds <= 0:
            raise ValueError("time_limit_seconds must be positive")
        if (
            self.max_generation_positions is not None
            and self.max_generation_positions < 1
        ):
            raise ValueError("max_generation_positions must be positive")


@dataclass(slots=True)
class SearchStats:
    nodes: int = 0
    leaf_evaluations: int = 0
    generated_raw_series: int = 0
    generated_unique_series: int = 0
    intra_series_transpositions: int = 0
    tt_hits: int = 0
    alpha_beta_cutoffs: int = 0
    root_bound_candidates: int = 0
    branch_caps: int = 0
    series_generation_positions: int = 0
    frontier_score_positions: int = 0
    static_evaluation_positions: int = 0
    evaluation_reach_positions: int = 0
    incomplete_reach_evaluations: int = 0
    generation_positions: int = 0
    frontier_prunes: int = 0
    frontier_states_pruned: int = 0
    frontier_paths_pruned: int = 0
    peak_frontier_states: int = 0
    generation_work_limit_hits: int = 0
    quiet_adjudication_positions: int = 0
    quiet_adjudication_cache_hits: int = 0
    quiet_adjudication_limit_hits: int = 0
    series_generation_cache_hits: int = 0
    series_generation_cache_evictions: int = 0
    series_generation_cache_peak: int = 0

    @property
    def work_positions(self) -> int:
        """Unified deterministic work counter.

        ``generation_positions`` remains the stored compatibility name used
        by existing API/database consumers. New code should prefer this alias;
        both include series generation, frontier scoring, static evaluation,
        evaluation reach, and quiet-proof work.
        """

        return self.generation_positions


@dataclass(frozen=True, slots=True)
class ScoredSeries:
    series: SeriesResult
    score: int
    principal_variation: tuple[SeriesResult, ...] = ()
    proof_bounds: tuple[int, int] = UNKNOWN_PROOF_BOUNDS

    @property
    def proof(self) -> str | None:
        return _proof_from_bounds(self.proof_bounds)


@dataclass(frozen=True, slots=True)
class SearchResult:
    score: int
    best_series: SeriesResult | None
    principal_variation: tuple[SeriesResult, ...]
    alternatives: tuple[ScoredSeries, ...]
    requested_depth: int
    completed_depth: int
    exact_width: bool
    timed_out: bool
    elapsed_seconds: float
    stats: SearchStats
    root_evaluation: EvaluationBreakdown
    proof: str | None = None
    adjudication_status: str | None = None
    max_series_per_node: int | None = None
    time_limit_seconds: float | None = None
    engine_version: str = ENGINE_VERSION
    source_fingerprint: str = ENGINE_SOURCE_FINGERPRINT
    engine_profile_id: str = ""
    engine_profile_name: str = ""
    required_prefix: tuple[str, ...] = ()
    work_limit_reached: bool = False
    max_generation_positions: int | None = None
    root_scores_complete: bool = True

    @property
    def forced(self) -> str | None:
        if self.adjudication_status == "proven-draw-no-mating-material":
            return "draw"
        if (
            not self.exact_width
            or self.timed_out
            or self.completed_depth != self.requested_depth
        ):
            return None
        if self.proof in {"white", "black", "draw"}:
            return self.proof
        if abs(self.score) >= MATE_SCORE - 10_000:
            return "white" if self.score > 0 else "black"
        return None

    @property
    def classification(self) -> str:
        if self.adjudication_status == "manual-proof-required":
            return "Adjudication Pending"
        return classify_score(self.score, forced=self.forced)

    @property
    def confidence(self) -> str:
        if self.adjudication_status == "manual-proof-required":
            return "quiet-draw proof required; no theory score issued"
        if self.adjudication_status == "proven-draw-no-mating-material":
            return "forced/proven draw by no mating material"
        if self.work_limit_reached:
            return "deterministic work limit reached; incomplete/selective"
        if self.forced and self.exact_width and not self.timed_out:
            return "forced/proven within searched horizon"
        if self.exact_width and self.completed_depth == self.requested_depth:
            return "exhaustive at stated series depth; heuristic leaf evaluation"
        if self.completed_depth:
            return "selective depth-limited heuristic"
        if self.best_series is not None:
            return "partial root candidates only; incomplete/selective"
        return "static heuristic only"


@dataclass(frozen=True, slots=True)
class _TTEntry:
    depth: int
    score: int
    bound: Bound
    pv: tuple[SeriesResult, ...]
    proof_bounds: tuple[int, int]


class _Timeout(Exception):
    pass


class _WorkLimit(Exception):
    pass


class _RootInterrupted(Exception):
    def __init__(
        self,
        scored: tuple[ScoredSeries, ...],
        cause: _Timeout | _WorkLimit,
    ) -> None:
        super().__init__(type(cause).__name__)
        self.scored = scored
        self.cause = cause


class _AdjudicationPending(Exception):
    pass


class SeriesSearcher:
    """Deterministic alpha-beta search where one ply is one complete series."""

    def __init__(
        self, limits: SearchLimits, profile: EngineProfile | None = None
    ) -> None:
        self.limits = limits
        self.profile = profile or baseline_profile()
        self.stats = SearchStats()
        self._tt: dict[tuple[tuple[int, str, int, int], int], _TTEntry] = {}
        self._eval_cache: dict[tuple[int, str, int, int], EvaluationBreakdown] = {}
        self._quiet_adjudication_cache: dict[
            tuple[int, str, int, int], str | None
        ] = {}
        self._series_generation_cache: OrderedDict[
            tuple[tuple[int, str, int, int], tuple[str, ...], int],
            tuple[SeriesResult, ...],
        ] = OrderedDict()
        self._series_generation_cache_weight = 0
        self._deadline: float | None = None
        self._selective = False
        self._quiet_work_limit_reached = False
        self._evaluation_work_limit_reached = False
        self._root_scores_complete = True

    def _check_deadline(self) -> None:
        if self._deadline is not None and time.perf_counter() >= self._deadline:
            raise _Timeout

    def _quiet_adjudication(self, state: ProgressiveState) -> str | None:
        if not state.quiet_draw_pending:
            return None
        self._check_deadline()
        key = state.transposition_key
        if key in self._quiet_adjudication_cache:
            self.stats.quiet_adjudication_cache_hits += 1
            return self._quiet_adjudication_cache[key]

        cancellation: str | None = None

        def should_stop() -> bool:
            nonlocal cancellation
            if self._deadline is not None and time.perf_counter() >= self._deadline:
                cancellation = "time"
                return True
            if (
                self.limits.max_generation_positions is not None
                and self.stats.generation_positions
                >= self.limits.max_generation_positions
            ):
                cancellation = "work"
                return True
            if (
                self.stats.quiet_adjudication_positions
                >= QUIET_ADJUDICATION_POSITION_LIMIT
            ):
                cancellation = "quiet-proof"
                return True
            # has_mating_series invokes this immediately before visiting one
            # candidate. Charge that candidate to the same deterministic work
            # counter used by complete-series generation.
            self.stats.quiet_adjudication_positions += 1
            self.stats.generation_positions += 1
            return False

        try:
            status = quiet_adjudication_status(state, should_stop=should_stop)
        except GenerationCancelled as error:
            if cancellation == "time":
                raise _Timeout from error
            self.stats.quiet_adjudication_limit_hits += 1
            if cancellation == "work":
                self.stats.generation_work_limit_hits += 1
                self._quiet_work_limit_reached = True
            # An incomplete mating-series probe is unknown. The rules require
            # human/proof adjudication here, so abort ordinary minimax without
            # assigning a heuristic draw score.
            status = "manual-proof-required"
        self._quiet_adjudication_cache[key] = status
        return status

    def _evaluate(self, state: ProgressiveState) -> EvaluationBreakdown:
        key = state.transposition_key
        cached = self._eval_cache.get(key)
        if cached is None:
            if (
                self.limits.max_generation_positions is not None
                and self.stats.generation_positions
                >= self.limits.max_generation_positions
            ):
                if not self._evaluation_work_limit_reached:
                    self.stats.generation_work_limit_hits += 1
                self._evaluation_work_limit_reached = True
                self._selective = True
                raise _WorkLimit
            self.stats.static_evaluation_positions += 1
            self.stats.generation_positions += 1
            remaining: int | None = None
            if self.limits.max_generation_positions is not None:
                remaining = max(
                    0,
                    self.limits.max_generation_positions
                    - self.stats.generation_positions,
                )
            cached = evaluate(
                state,
                self.profile,
                max_reach_positions=remaining,
            )
            reach_positions = (
                cached.white_reach_nodes + cached.black_reach_nodes
            )
            self.stats.evaluation_reach_positions += reach_positions
            self.stats.generation_positions += reach_positions
            if not cached.reach_complete:
                self.stats.incomplete_reach_evaluations += 1
                if (
                    remaining is not None
                    and remaining < 256
                    and reach_positions >= remaining
                ):
                    if not self._evaluation_work_limit_reached:
                        self.stats.generation_work_limit_hits += 1
                    self._evaluation_work_limit_reached = True
                    self._selective = True
            self._eval_cache[key] = cached
            self.stats.leaf_evaluations += 1
        return cached

    def _generate(
        self,
        state: ProgressiveState,
        *,
        required_prefix: tuple[str, ...] = (),
    ) -> list[SeriesResult]:
        generation = GenerationStats()
        remaining_positions: int | None = None
        if self.limits.max_generation_positions is not None:
            remaining_positions = (
                self.limits.max_generation_positions
                - self.stats.generation_positions
            )
            if remaining_positions <= 0:
                if not (
                    self._quiet_work_limit_reached
                    or self._evaluation_work_limit_reached
                ):
                    self.stats.generation_work_limit_hits += 1
                raise _WorkLimit

        def frontier_score(board: chess.Board) -> int:
            partial = ProgressiveState(
                board,
                state.series_number,
                quiet_series=state.quiet_series,
            )
            score = fast_evaluate(partial, self.profile)
            checks = 0
            immediate_mates = 0
            captures = 0
            promotions = 0
            for move, required_ep in _legal_move_variants(board):
                board.ep_square = required_ep
                gives_check = board.gives_check(move)
                checks += int(gives_check)
                captures += int(board.is_capture(move))
                promotions += int(move.promotion is not None)
                if gives_check:
                    child = board.copy(stack=False)
                    child.push(move)
                    immediate_mates += int(child.is_checkmate())
            board.ep_square = None
            tactical = (
                immediate_mates * 5_000_000
                + checks * 50_000
                + promotions * 2_000
                + captures * 100
            )
            score += tactical if board.turn == chess.WHITE else -tactical
            return score

        try:
            series = generate_series(
                state,
                stats=generation,
                required_prefix=required_prefix,
                max_frontier_states=self.limits.max_series_per_node,
                max_positions=remaining_positions,
                frontier_score=(
                    frontier_score
                    if self.limits.max_series_per_node is not None
                    else None
                ),
                should_stop=(
                    (lambda: time.perf_counter() >= self._deadline)
                    if self._deadline is not None
                    else None
                ),
            )
        except GenerationWorkLimit as error:
            raise _WorkLimit from error
        except GenerationCancelled as error:
            raise _Timeout from error
        finally:
            self.stats.generated_raw_series += generation.raw_series
            self.stats.generated_unique_series += generation.unique_series
            self.stats.intra_series_transpositions += generation.transpositions_merged
            self.stats.series_generation_positions += generation.positions_visited
            self.stats.frontier_score_positions += (
                generation.frontier_score_positions
            )
            self.stats.generation_positions += (
                generation.positions_visited
                + generation.frontier_score_positions
            )
            self.stats.frontier_prunes += generation.frontier_prunes
            self.stats.frontier_states_pruned += generation.frontier_states_pruned
            self.stats.frontier_paths_pruned += generation.frontier_paths_pruned
            self.stats.peak_frontier_states = max(
                self.stats.peak_frontier_states,
                generation.peak_frontier_states,
            )
            if generation.frontier_prunes:
                self._selective = True
            if generation.work_limit_reached:
                self.stats.generation_work_limit_hits += 1
        return series

    @staticmethod
    def _terminal_score(
        result: SeriesResult, mover: chess.Color, ply_from_root: int
    ) -> int | None:
        if result.outcome == Outcome.CHECKMATE:
            winner = mover if result.ended_by_check else not mover
            return (
                MATE_SCORE - ply_from_root
                if winner == chess.WHITE
                else -MATE_SCORE + ply_from_root
            )
        if result.outcome in {Outcome.STALEMATE, Outcome.TEN_SERIES_DRAW}:
            return 0
        return None

    @staticmethod
    def _terminal_proof_bounds(
        result: SeriesResult, mover: chess.Color
    ) -> tuple[int, int]:
        if result.outcome == Outcome.CHECKMATE:
            winner = mover if result.ended_by_check else not mover
            value = 1 if winner == chess.WHITE else -1
            return value, value
        if result.outcome in {Outcome.STALEMATE, Outcome.TEN_SERIES_DRAW}:
            return 0, 0
        return UNKNOWN_PROOF_BOUNDS

    @staticmethod
    def _combine_proof_bounds(
        mover: chess.Color,
        bounds: list[tuple[int, int]],
        *,
        total_series: int,
    ) -> tuple[int, int]:
        """Combines proof intervals without treating heuristic zero as draw.

        ``(-1, 1)`` means Black win through White win are all still possible.
        Unexamined alpha-beta siblings retain that interval. This lets a
        parent prove, for example, that one exact draw plus only draw-or-loss
        alternatives is game-theoretically drawn.
        """

        if not bounds:
            return UNKNOWN_PROOF_BOUNDS
        candidates = bounds + [UNKNOWN_PROOF_BOUNDS] * (total_series - len(bounds))
        if mover == chess.WHITE:
            return max(item[0] for item in candidates), max(
                item[1] for item in candidates
            )
        return min(item[0] for item in candidates), min(
            item[1] for item in candidates
        )

    def _static_series_score(
        self, result: SeriesResult, mover: chess.Color, ply_from_root: int
    ) -> int:
        terminal = self._terminal_score(result, mover, ply_from_root)
        return (
            terminal
            if terminal is not None
            else fast_evaluate(result.final_state, self.profile)
        )

    def _ordered(
        self,
        series: list[SeriesResult],
        mover: chess.Color,
        ply_from_root: int,
    ) -> list[SeriesResult]:
        def order_key(item: SeriesResult) -> tuple[int, str]:
            self._check_deadline()
            return (
                (
                    -self._static_series_score(item, mover, ply_from_root)
                    if mover == chess.WHITE
                    else self._static_series_score(item, mover, ply_from_root)
                ),
                item.machine_notation,
            )

        ordered = sorted(
            series,
            key=order_key,
        )
        cap = self.limits.max_series_per_node
        if cap is not None and len(ordered) > cap:
            self.stats.branch_caps += 1
            self._selective = True
            return ordered[:cap]
        return ordered

    def _ordered_generated(
        self,
        state: ProgressiveState,
        *,
        ply_from_root: int,
        required_prefix: tuple[str, ...] = (),
    ) -> list[SeriesResult]:
        """Returns one deterministic capped frontier with bounded reuse.

        The ply is part of the key because terminal mate-distance ordering is
        expressed relative to the root. The state itself fixes the mover and
        every other generation input; a root prefix is included verbatim so
        fixed-prefix analysis cannot alias an unconstrained frontier.
        """

        self._check_deadline()
        key = (state.transposition_key, required_prefix, ply_from_root)
        cached = self._series_generation_cache.get(key)
        if cached is not None:
            self._series_generation_cache.move_to_end(key)
            self.stats.series_generation_cache_hits += 1
            return list(cached)

        ordered = self._ordered(
            self._generate(state, required_prefix=required_prefix),
            state.board.turn,
            ply_from_root,
        )
        weight = max(1, len(ordered))
        if weight > SERIES_GENERATION_CACHE_CAPACITY:
            return ordered
        while (
            self._series_generation_cache
            and self._series_generation_cache_weight + weight
            > SERIES_GENERATION_CACHE_CAPACITY
        ):
            _, evicted = self._series_generation_cache.popitem(last=False)
            self._series_generation_cache_weight -= max(1, len(evicted))
            self.stats.series_generation_cache_evictions += 1
        self._series_generation_cache[key] = tuple(ordered)
        self._series_generation_cache_weight += weight
        self.stats.series_generation_cache_peak = max(
            self.stats.series_generation_cache_peak,
            self._series_generation_cache_weight,
        )
        return ordered

    def _minimax(
        self,
        state: ProgressiveState,
        depth: int,
        alpha: int,
        beta: int,
        ply_from_root: int,
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[int, int]]:
        self._check_deadline()
        self.stats.nodes += 1
        adjudication = self._quiet_adjudication(state)
        if adjudication == "proven-draw-no-mating-material":
            return 0, (), (0, 0)
        if adjudication == "manual-proof-required":
            raise _AdjudicationPending
        if depth == 0:
            return self._evaluate(state).total, (), UNKNOWN_PROOF_BOUNDS

        key = (state.transposition_key, depth)
        entry = self._tt.get(key)
        original_alpha, original_beta = alpha, beta
        if entry is not None and entry.depth >= depth:
            self.stats.tt_hits += 1
            if entry.bound == Bound.EXACT:
                return entry.score, entry.pv, entry.proof_bounds
            if entry.bound == Bound.LOWER:
                alpha = max(alpha, entry.score)
            else:
                beta = min(beta, entry.score)
            if alpha >= beta:
                return entry.score, entry.pv, entry.proof_bounds

        mover = state.board.turn
        series = self._ordered_generated(
            state,
            ply_from_root=ply_from_root + 1,
        )
        if not series:
            return 0, (), UNKNOWN_PROOF_BOUNDS

        best_score = -MATE_SCORE * 2 if mover == chess.WHITE else MATE_SCORE * 2
        best_pv: tuple[SeriesResult, ...] = ()
        child_bounds: list[tuple[int, int]] = []
        for result in series:
            self._check_deadline()
            terminal = self._terminal_score(result, mover, ply_from_root + 1)
            if terminal is None:
                score, child_pv, proof_bounds = self._minimax(
                    result.final_state,
                    depth - 1,
                    alpha,
                    beta,
                    ply_from_root + 1,
                )
            else:
                score, child_pv = terminal, ()
                proof_bounds = self._terminal_proof_bounds(result, mover)
            child_bounds.append(proof_bounds)

            if (
                mover == chess.WHITE
                and score > best_score
                or mover == chess.BLACK
                and score < best_score
            ):
                best_score = score
                best_pv = (result,) + child_pv

            immediate_mate_score = MATE_SCORE - (ply_from_root + 1)
            if (
                mover == chess.WHITE
                and best_score == immediate_mate_score
                or mover == chess.BLACK
                and best_score == -immediate_mate_score
            ):
                # Nothing can improve on checkmate in the current series.
                # This also prevents an irrelevant unresolved quiet-draw claim
                # in a losing sibling from masking the proven mate.
                break

            if mover == chess.WHITE:
                alpha = max(alpha, best_score)
            else:
                beta = min(beta, best_score)
            if alpha >= beta:
                self.stats.alpha_beta_cutoffs += 1
                break

        if best_score <= original_alpha:
            bound = Bound.UPPER
        elif best_score >= original_beta:
            bound = Bound.LOWER
        else:
            bound = Bound.EXACT
        proof_bounds = self._combine_proof_bounds(
            mover,
            child_bounds,
            total_series=len(series),
        )
        self._tt[key] = _TTEntry(depth, best_score, bound, best_pv, proof_bounds)
        return best_score, best_pv, proof_bounds

    def _search_root(
        self,
        state: ProgressiveState,
        depth: int,
        required_prefix: tuple[str, ...],
    ) -> tuple[
        int,
        tuple[SeriesResult, ...],
        tuple[ScoredSeries, ...],
        str | None,
    ]:
        mover = state.board.turn
        series = self._ordered_generated(
            state,
            ply_from_root=1,
            required_prefix=required_prefix,
        )
        scored: list[ScoredSeries] = []
        root_alpha = -MATE_SCORE * 2
        root_beta = MATE_SCORE * 2
        for result in series:
            try:
                self._check_deadline()
                terminal = self._terminal_score(result, mover, 1)
                if terminal is None:
                    child_alpha = (
                        -MATE_SCORE * 2
                        if self.limits.collect_all_root_scores
                        else root_alpha
                    )
                    child_beta = (
                        MATE_SCORE * 2
                        if self.limits.collect_all_root_scores
                        else root_beta
                    )
                    score, child_pv, proof_bounds = self._minimax(
                        result.final_state,
                        depth - 1,
                        child_alpha,
                        child_beta,
                        1,
                    )
                    score_is_exact = (
                        self.limits.collect_all_root_scores
                        or child_alpha < score < child_beta
                    )
                else:
                    score, child_pv = terminal, ()
                    proof_bounds = self._terminal_proof_bounds(result, mover)
                    score_is_exact = True
            except (_Timeout, _WorkLimit) as error:
                if scored:
                    raise _RootInterrupted(tuple(scored), error) from error
                raise
            if score_is_exact:
                scored.append(ScoredSeries(result, score, child_pv, proof_bounds))
            else:
                self.stats.root_bound_candidates += 1

            if not self.limits.collect_all_root_scores:
                if mover == chess.WHITE:
                    root_alpha = max(root_alpha, score)
                else:
                    root_beta = min(root_beta, score)
            if (
                mover == chess.WHITE
                and score == MATE_SCORE - 1
                or mover == chess.BLACK
                and score == -MATE_SCORE + 1
            ):
                break
        self._root_scores_complete = len(scored) == len(series)
        scored.sort(
            key=lambda item: (
                -item.score if mover == chess.WHITE else item.score,
                item.series.machine_notation,
            )
        )
        if not scored:
            static = self._evaluate(state).total
            return static, (), (), None
        best = scored[0]
        proof_bounds = self._combine_proof_bounds(
            mover,
            [item.proof_bounds for item in scored],
            total_series=len(series),
        )
        return (
            best.score,
            (best.series,) + best.principal_variation,
            tuple(scored),
            _proof_from_bounds(proof_bounds),
        )

    def run(
        self,
        state: ProgressiveState,
        *,
        required_prefix: tuple[str, ...] = (),
    ) -> SearchResult:
        required_prefix = tuple(required_prefix)
        started = time.perf_counter()
        if self.limits.time_limit_seconds is not None:
            self._deadline = started + self.limits.time_limit_seconds
        root_evaluation = self._evaluate(state)
        try:
            adjudication = self._quiet_adjudication(state)
        except _Timeout:
            return SearchResult(
                score=root_evaluation.total,
                best_series=None,
                principal_variation=(),
                alternatives=(),
                requested_depth=self.limits.depth_series,
                completed_depth=0,
                exact_width=False,
                timed_out=True,
                elapsed_seconds=time.perf_counter() - started,
                stats=self.stats,
                root_evaluation=root_evaluation,
                proof=None,
                max_series_per_node=self.limits.max_series_per_node,
                time_limit_seconds=self.limits.time_limit_seconds,
                engine_profile_id=self.profile.profile_id,
                engine_profile_name=self.profile.name,
                required_prefix=required_prefix,
                work_limit_reached=(
                    self._quiet_work_limit_reached
                    or self._evaluation_work_limit_reached
                ),
                max_generation_positions=self.limits.max_generation_positions,
            )
        if adjudication in {
            "manual-proof-required",
            "proven-draw-no-mating-material",
        }:
            return SearchResult(
                score=0,
                best_series=None,
                principal_variation=(),
                alternatives=(),
                requested_depth=self.limits.depth_series,
                completed_depth=0,
                exact_width=adjudication == "proven-draw-no-mating-material",
                timed_out=False,
                elapsed_seconds=time.perf_counter() - started,
                stats=self.stats,
                root_evaluation=root_evaluation,
                proof=(
                    "draw"
                    if adjudication == "proven-draw-no-mating-material"
                    else None
                ),
                adjudication_status=adjudication,
                max_series_per_node=self.limits.max_series_per_node,
                time_limit_seconds=self.limits.time_limit_seconds,
                engine_profile_id=self.profile.profile_id,
                engine_profile_name=self.profile.name,
                required_prefix=required_prefix,
                work_limit_reached=(
                    self._quiet_work_limit_reached
                    or self._evaluation_work_limit_reached
                ),
                max_generation_positions=self.limits.max_generation_positions,
            )

        completed_depth = 0
        timed_out = False
        work_limit_reached = False
        best_score = root_evaluation.total
        best_pv: tuple[SeriesResult, ...] = ()
        alternatives: tuple[ScoredSeries, ...] = ()
        best_proof: str | None = None
        for depth in range(1, self.limits.depth_series + 1):
            try:
                score, pv, root_alternatives, proof = self._search_root(
                    state,
                    depth,
                    required_prefix,
                )
            except _RootInterrupted as interrupted:
                timed_out = isinstance(interrupted.cause, _Timeout)
                work_limit_reached = isinstance(interrupted.cause, _WorkLimit)
                self._selective = True
                if completed_depth == 0:
                    self._root_scores_complete = False
                    mover = state.board.turn
                    partial = tuple(
                        sorted(
                            interrupted.scored,
                            key=lambda item: (
                                -item.score if mover == chess.WHITE else item.score,
                                item.series.machine_notation,
                            ),
                        )
                    )
                    best = partial[0]
                    best_score = best.score
                    best_pv = (best.series,) + best.principal_variation
                    alternatives = partial
                    best_proof = None
                break
            except _Timeout:
                timed_out = True
                self._selective = True
                if completed_depth == 0:
                    self._root_scores_complete = False
                break
            except _WorkLimit:
                work_limit_reached = True
                self._selective = True
                if completed_depth == 0:
                    self._root_scores_complete = False
                break
            except _AdjudicationPending:
                return SearchResult(
                    score=0,
                    best_series=None,
                    principal_variation=(),
                    alternatives=(),
                    requested_depth=self.limits.depth_series,
                    completed_depth=completed_depth,
                    exact_width=False,
                    timed_out=False,
                    elapsed_seconds=time.perf_counter() - started,
                    stats=self.stats,
                    root_evaluation=root_evaluation,
                    proof=None,
                    adjudication_status="manual-proof-required",
                    max_series_per_node=self.limits.max_series_per_node,
                    time_limit_seconds=self.limits.time_limit_seconds,
                    engine_profile_id=self.profile.profile_id,
                    engine_profile_name=self.profile.name,
                    required_prefix=required_prefix,
                    work_limit_reached=(
                        self._quiet_work_limit_reached
                        or self._evaluation_work_limit_reached
                    ),
                    max_generation_positions=self.limits.max_generation_positions,
                )
            best_score = score
            best_pv = pv
            alternatives = root_alternatives
            best_proof = proof
            completed_depth = depth

        elapsed = time.perf_counter() - started
        work_limit_reached = (
            work_limit_reached
            or self._quiet_work_limit_reached
            or self._evaluation_work_limit_reached
        )
        return SearchResult(
            score=best_score,
            best_series=best_pv[0] if best_pv else None,
            principal_variation=best_pv,
            alternatives=alternatives,
            requested_depth=self.limits.depth_series,
            completed_depth=completed_depth,
            exact_width=not self._selective,
            timed_out=timed_out,
            elapsed_seconds=elapsed,
            stats=self.stats,
            root_evaluation=root_evaluation,
            proof=best_proof,
            adjudication_status=adjudication,
            max_series_per_node=self.limits.max_series_per_node,
            time_limit_seconds=self.limits.time_limit_seconds,
            engine_profile_id=self.profile.profile_id,
            engine_profile_name=self.profile.name,
            required_prefix=required_prefix,
            work_limit_reached=work_limit_reached,
            max_generation_positions=self.limits.max_generation_positions,
            root_scores_complete=self._root_scores_complete,
        )


def analyze(
    state: ProgressiveState,
    limits: SearchLimits | None = None,
    profile: EngineProfile | None = None,
    *,
    required_prefix: tuple[str, ...] = (),
) -> SearchResult:
    return SeriesSearcher(limits or SearchLimits(), profile).run(
        state,
        required_prefix=required_prefix,
    )
