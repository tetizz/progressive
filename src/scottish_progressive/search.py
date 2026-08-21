from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from enum import StrEnum
import time
from typing import Protocol

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
from .promotion_mate import (
    MAX_PROMOTION_MATE_POSITIONS,
    PromotionMateProbe,
    find_promotion_series_mate,
    promotion_mate_eligible,
)
from .rules import (
    GenerationCancelled,
    GenerationStats,
    GenerationWorkLimit,
    NativeFinalSeriesScoreConfig,
    NativeFrontierScoreConfig,
    _NativeSeriesBatch,
    _NativeSeriesReference,
    _native_complete_series_batch,
    _series_tactical_provenance,
    generate_series,
    play_series,
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
# Best-only play may otherwise accept a root series whose opponent reply mate
# was discarded by the ordinary width-32 child beam. This second, tactical
# beam is deliberately much wider, globally bounded, and late-series or
# promotion-gated so ordinary opening positions pay only the cheap scan.
ROOT_CHILD_MATE_SCREEN_FRONTIER = 832
ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT = 1_600_000
ROOT_CHILD_MATE_SCREEN_MIN_SERIES = 7
# A low-risk root may still expose a concrete promotion tactic in the
# opponent's immediately following series.  Protect that one-series safety
# horizon, but do not let broad structural promotion eligibility at distant
# Series-7 descendants inflate an otherwise ordinary depth-five search.
TACTICAL_DESCENDANT_PROMOTION_MAX_PLY = 2


def _tactical_frontier_protection_eligible(
    state: ProgressiveState,
    *,
    required_prefix: tuple[str, ...] = (),
) -> bool:
    """Activates the wider tactical reserve only at deterministic risk nodes."""

    return (
        state.series_number >= ROOT_CHILD_MATE_SCREEN_MIN_SERIES
        or promotion_mate_eligible(
            state,
            required_prefix=required_prefix,
        )
    )


class EvaluationOverlay(Protocol):
    """Optional leaf-score layer sharing the ordinary rules/search core."""

    base_profile_id: str
    variant_id: str
    name: str

    def score(self, state: ProgressiveState, hand_score: int) -> int: ...


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
    native_threads: int = 1

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
        if type(self.native_threads) is not int or not 1 <= self.native_threads <= 64:
            raise ValueError("native_threads must be an integer from 1 through 64")
        if self.native_threads > 1 and self.time_limit_seconds is None:
            raise ValueError("parallel native_threads require time_limit_seconds")


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
    tactical_frontier_states_retained: int = 0
    tactical_frontier_reserve_drops: int = 0
    tactical_final_series_retained: int = 0
    tactical_final_reserve_drops: int = 0
    peak_frontier_states: int = 0
    generation_work_limit_hits: int = 0
    quiet_adjudication_positions: int = 0
    quiet_adjudication_cache_hits: int = 0
    quiet_adjudication_limit_hits: int = 0
    series_generation_cache_hits: int = 0
    series_generation_cache_evictions: int = 0
    series_generation_cache_peak: int = 0
    promotion_mate_positions: int = 0
    promotion_mate_setup_states: int = 0
    promotion_mate_candidates: int = 0
    promotion_mate_completion_probes: int = 0
    promotion_mate_limit_hits: int = 0
    promotion_mate_replay_rejects: int = 0
    promotion_mate_mates: int = 0

    @property
    def work_positions(self) -> int:
        """Unified deterministic work counter.

        ``generation_positions`` remains the stored compatibility name used
        by existing API/database consumers. New code should prefer this alias;
        both include series generation, frontier scoring, promotion-mate
        probing, static evaluation, evaluation reach, and quiet-proof work.
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
        if self.adjudication_status == "manual-proof-required":
            return None
        # Proof intervals remain sound under selective width: an existential
        # terminal win for the mover cannot be invalidated by omitted siblings.
        # Keep the heuristic mate-score fallback below exact-width gated.
        if self.proof in {"white", "black", "draw"}:
            return self.proof
        if (
            not self.exact_width
            or self.timed_out
            or self.completed_depth != self.requested_depth
        ):
            return None
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
        if self.proof in {"white", "black", "draw"}:
            return "forced/proven by sound search proof bounds"
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


@dataclass(frozen=True, slots=True)
class _SeriesCacheEntry:
    collection: tuple[SeriesResult, ...] | _NativeSeriesBatch
    width_complete: bool


class _GeneratedSeriesList(list[SeriesResult | _NativeSeriesReference]):
    """List-compatible frontier carrying whether every legal branch exists."""

    __slots__ = ("width_complete",)

    def __init__(
        self,
        values: list[SeriesResult | _NativeSeriesReference],
        *,
        width_complete: bool,
    ) -> None:
        super().__init__(values)
        self.width_complete = width_complete


class _Timeout(Exception):
    pass


class _WorkLimit(Exception):
    pass


class _RootInterrupted(Exception):
    def __init__(
        self,
        scored: tuple[ScoredSeries, ...],
        cause: _Timeout | _WorkLimit,
        fallback: SeriesResult,
    ) -> None:
        super().__init__(type(cause).__name__)
        self.scored = scored
        self.cause = cause
        self.fallback = fallback


class _AdjudicationPending(Exception):
    pass


class _RootAdjudicationPending(_AdjudicationPending):
    def __init__(self, fallback: SeriesResult) -> None:
        super().__init__()
        self.fallback = fallback


class SeriesSearcher:
    """Deterministic alpha-beta search where one ply is one complete series."""

    def __init__(
        self,
        limits: SearchLimits,
        profile: EngineProfile | None = None,
        evaluation_overlay: EvaluationOverlay | None = None,
    ) -> None:
        self.limits = limits
        self.profile = profile or baseline_profile()
        self.evaluation_overlay = evaluation_overlay
        if (
            evaluation_overlay is not None
            and evaluation_overlay.base_profile_id != self.profile.profile_id
        ):
            raise ValueError("evaluation overlay is bound to a different base profile")
        self.engine_profile_id = (
            self.profile.profile_id
            if evaluation_overlay is None
            else evaluation_overlay.variant_id
        )
        self.engine_profile_name = (
            self.profile.name if evaluation_overlay is None else evaluation_overlay.name
        )
        self.stats = SearchStats()
        # Keep the deepest result per boundary. Iterative deepening can then
        # use a shallower principal variation as a hash-series ordering hint,
        # while score/bound reuse remains gated by ``entry.depth >= depth``.
        # This is the progressive-series equivalent of Stockfish's hash-move
        # ordering: it changes visit order, never the generated legal set.
        self._tt: dict[tuple[int, str, int, int], _TTEntry] = {}
        self._eval_cache: dict[tuple[int, str, int, int], EvaluationBreakdown] = {}
        self._quiet_adjudication_cache: dict[
            tuple[int, str, int, int], str | None
        ] = {}
        self._series_generation_cache: OrderedDict[
            tuple[tuple[int, str, int, int], tuple[str, ...], int, bool],
            _SeriesCacheEntry,
        ] = OrderedDict()
        self._series_generation_cache_weight = 0
        self._deadline: float | None = None
        self._selective = False
        self._quiet_work_limit_reached = False
        self._evaluation_work_limit_reached = False
        self._root_scores_complete = True
        self._preferred_root_series: str | None = None
        self._promotion_mate_checked = False
        self._promotion_mate_series: SeriesResult | None = None
        # Tactical beam protection is selected from the search root, not from
        # the deepest descendant reached by iterative minimax.  Otherwise an
        # ordinary Series-4 root silently switches to the much wider reserve
        # after descending to Series 7 and can lose an entire completed depth
        # to beam work.  ``None`` keeps direct private-generation probes useful:
        # their first state is treated as their root.
        self._root_tactical_frontier_protection: bool | None = None
        self._root_child_mate_screen_cache: dict[
            tuple[int, str, int, int], SeriesResult | None
        ] = {}
        configured_work = self.limits.max_generation_positions
        self._root_child_mate_screen_budget = (
            ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT
            if configured_work is None
            else min(
                ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT,
                configured_work // 5,
            )
        )
        self._root_child_mate_screen_work = 0

    def _tactical_frontier_protection_enabled(
        self,
        state: ProgressiveState,
        *,
        ply_from_root: int = 1,
        required_prefix: tuple[str, ...] = (),
    ) -> bool:
        """Returns the root-stable tactical policy for one generated node.

        A late or promotion-risk root protects every descendant in the search.
        An earlier ordinary root remains on the fixed-width fast path even if
        minimax eventually reaches Series 7; only a concrete promotion risk in
        the immediate opponent-series safety horizon may opt its node in.
        """

        if self._root_tactical_frontier_protection is None:
            self._root_tactical_frontier_protection = (
                _tactical_frontier_protection_eligible(
                    state,
                    required_prefix=required_prefix,
                )
            )
        if self._root_tactical_frontier_protection:
            return True
        return (
            ply_from_root <= TACTICAL_DESCENDANT_PROMOTION_MAX_PLY
            and promotion_mate_eligible(
                state,
                required_prefix=required_prefix,
            )
        )

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
            if self.evaluation_overlay is not None:
                blended_total = self.evaluation_overlay.score(state, cached.total)
                if type(blended_total) is not int:
                    raise TypeError("evaluation overlay score must be an exact integer")
                cached = replace(cached, total=blended_total)
            self._eval_cache[key] = cached
            self.stats.leaf_evaluations += 1
        return cached

    def _record_generation_stats(self, generation: GenerationStats) -> None:
        self.stats.generated_raw_series += generation.raw_series
        self.stats.generated_unique_series += generation.unique_series
        self.stats.intra_series_transpositions += generation.transpositions_merged
        self.stats.series_generation_positions += generation.positions_visited
        self.stats.frontier_score_positions += generation.frontier_score_positions
        self.stats.generation_positions += (
            generation.positions_visited + generation.frontier_score_positions
        )
        self.stats.frontier_prunes += generation.frontier_prunes
        self.stats.frontier_states_pruned += generation.frontier_states_pruned
        self.stats.frontier_paths_pruned += generation.frontier_paths_pruned
        self.stats.tactical_frontier_states_retained += (
            generation.tactical_frontier_states_retained
        )
        self.stats.tactical_frontier_reserve_drops += (
            generation.tactical_frontier_reserve_drops
        )
        self.stats.tactical_final_series_retained += (
            generation.tactical_final_series_retained
        )
        self.stats.tactical_final_reserve_drops += (
            generation.tactical_final_reserve_drops
        )
        self.stats.peak_frontier_states = max(
            self.stats.peak_frontier_states,
            generation.peak_frontier_states,
        )
        if generation.frontier_prunes:
            self._selective = True
        if generation.work_limit_reached:
            self.stats.generation_work_limit_hits += 1

    def _record_promotion_mate_probe(self, probe: PromotionMateProbe) -> None:
        self.stats.promotion_mate_positions += probe.positions_visited
        self.stats.promotion_mate_setup_states += probe.setup_states
        self.stats.promotion_mate_candidates += probe.promotion_candidates
        self.stats.promotion_mate_completion_probes += probe.completion_probes
        self.stats.promotion_mate_limit_hits += int(probe.work_limit_reached)
        self.stats.promotion_mate_replay_rejects += probe.replay_rejects
        self.stats.promotion_mate_mates += int(probe.series is not None)
        self.stats.generation_positions += probe.positions_visited

    def _root_promotion_mate(
        self,
        state: ProgressiveState,
        *,
        required_prefix: tuple[str, ...],
        reserve_positions: int,
    ) -> SeriesResult | None:
        """Runs the separately bounded current-series mate lane at most once."""

        if self._promotion_mate_checked:
            return self._promotion_mate_series
        self._promotion_mate_checked = True
        if (
            self.limits.max_series_per_node is None
            or not promotion_mate_eligible(
                state,
                required_prefix=required_prefix,
            )
        ):
            return None

        lane_limit = MAX_PROMOTION_MATE_POSITIONS
        if self.limits.max_generation_positions is not None:
            available = max(
                0,
                self.limits.max_generation_positions
                - self.stats.generation_positions
                - reserve_positions,
            )
            # Preserve four fifths of every finite budget for the ordinary
            # search. At the production 250k gate this gives the lane its
            # independently validated at-most-50k envelope.
            lane_limit = min(lane_limit, available // 5)
        if lane_limit < 1:
            return None

        probe = find_promotion_series_mate(
            state,
            required_prefix=required_prefix,
            max_positions=lane_limit,
            should_stop=(
                (lambda: time.perf_counter() >= self._deadline)
                if self._deadline is not None
                else None
            ),
            promotion_score=NativeFrontierScoreConfig.from_profile(
                state,
                self.profile,
            ),
        )
        self._record_promotion_mate_probe(probe)
        if probe.cancelled:
            return None
        if probe.series is None:
            return None
        self._promotion_mate_series = probe.series
        self._selective = True
        return probe.series

    def _generate(
        self,
        state: ProgressiveState,
        *,
        ply_from_root: int,
        required_prefix: tuple[str, ...] = (),
        reserve_positions: int = 0,
        tactical_protection: bool | None = None,
    ) -> tuple[list[SeriesResult] | _NativeSeriesBatch, bool]:
        generation = GenerationStats()
        remaining_positions: int | None = None
        if self.limits.max_generation_positions is not None:
            remaining_positions = (
                self.limits.max_generation_positions
                - self.stats.generation_positions
                - reserve_positions
            )
            if remaining_positions <= 0:
                if not (
                    self._quiet_work_limit_reached
                    or self._evaluation_work_limit_reached
                ):
                    self.stats.generation_work_limit_hits += 1
                raise _WorkLimit

        frontier_score = NativeFrontierScoreConfig.from_profile(
            state,
            self.profile,
            tactical_protection=(
                self._tactical_frontier_protection_enabled(
                    state,
                    ply_from_root=ply_from_root,
                    required_prefix=required_prefix,
                )
                if tactical_protection is None
                else tactical_protection
            ),
        )
        native_final_score = (
            NativeFinalSeriesScoreConfig.from_profile(
                self.profile,
                max_returned_series=self.limits.max_series_per_node,
                ply_from_root=ply_from_root,
                mate_score=MATE_SCORE,
            )
            if self.limits.max_series_per_node is not None
            else None
        )

        should_stop = (
            (lambda: time.perf_counter() >= self._deadline)
            if self._deadline is not None
            else None
        )
        native_time_budget_ns = (
            max(0, int((self._deadline - time.perf_counter()) * 1_000_000_000))
            if self._deadline is not None
            else None
        )
        try:
            series = (
                _native_complete_series_batch(
                    state,
                    generation,
                    required_prefix=required_prefix,
                    max_frontier_states=self.limits.max_series_per_node,
                    max_positions=remaining_positions,
                    frontier_score=frontier_score,
                    native_final_score=native_final_score,
                    should_stop=should_stop,
                    native_time_budget_ns=native_time_budget_ns,
                    native_threads=self.limits.native_threads,
                )
                if native_final_score is not None
                else None
            )
            if series is None:
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
                    native_final_score=native_final_score,
                    should_stop=should_stop,
                )
        except GenerationWorkLimit as error:
            raise _WorkLimit from error
        except GenerationCancelled as error:
            raise _Timeout from error
        finally:
            self._record_generation_stats(generation)
        if generation.unique_series > len(series):
            self.stats.branch_caps += 1
            self._selective = True
        width_complete = (
            generation.frontier_prunes == 0
            and generation.unique_series <= len(series)
        )
        return series, width_complete

    def _generate_root_seed(
        self,
        state: ProgressiveState,
        *,
        required_prefix: tuple[str, ...],
    ) -> SeriesResult:
        """Generates one deterministic legal root series from reserved work.

        A width-one lexicographic beam expands at most one partial state per
        micro-move and does no frontier evaluation, so a non-terminal series
        costs at most ``state.moves_available`` logical positions. Callers
        reserve that amount before attempting the ordinary root frontier.
        If the entire configured budget is smaller, this helper still obeys
        it and can legitimately fail instead of doing unmetered work.
        """

        generation = GenerationStats()
        remaining_positions: int | None = None
        if self.limits.max_generation_positions is not None:
            remaining_positions = (
                self.limits.max_generation_positions
                - self.stats.generation_positions
            )
            if remaining_positions <= 0:
                if not self.stats.generation_work_limit_hits:
                    self.stats.generation_work_limit_hits += 1
                raise _WorkLimit
        try:
            series = generate_series(
                state,
                stats=generation,
                required_prefix=required_prefix,
                max_frontier_states=1,
                max_positions=remaining_positions,
                frontier_score=None,
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
            self._record_generation_stats(generation)
        if not series:
            raise _WorkLimit
        return series[0]

    def _root_child_immediate_mate(
        self,
        state: ProgressiveState,
    ) -> SeriesResult | None:
        """Returns one replay-proven reply mate from a wide native screen.

        This is an existential tactical check, never a no-mate proof. A
        missing native kernel, a selective miss, or exhaustion therefore
        returns ``None`` and ordinary minimax remains authoritative. Work is
        charged to the search's normal deterministic counter and the screen
        has one search-wide budget shared by every root child and iteration.
        """

        if (
            self.limits.collect_all_root_scores
            or self.limits.max_series_per_node is None
            or not _tactical_frontier_protection_eligible(state)
        ):
            return None

        key = state.transposition_key
        if key in self._root_child_mate_screen_cache:
            return self._root_child_mate_screen_cache[key]

        remaining = (
            self._root_child_mate_screen_budget
            - self._root_child_mate_screen_work
        )
        if self.limits.max_generation_positions is not None:
            remaining = min(
                remaining,
                self.limits.max_generation_positions
                - self.stats.generation_positions,
            )
        if remaining <= 0:
            return None

        generation = GenerationStats()
        frontier_score = NativeFrontierScoreConfig.from_profile(
            state,
            self.profile,
        )
        final_score = NativeFinalSeriesScoreConfig.from_profile(
            self.profile,
            max_returned_series=ROOT_CHILD_MATE_SCREEN_FRONTIER,
            ply_from_root=2,
            mate_score=MATE_SCORE,
        )
        should_stop = (
            (lambda: time.perf_counter() >= self._deadline)
            if self._deadline is not None
            else None
        )
        native_time_budget_ns = (
            max(0, int((self._deadline - time.perf_counter()) * 1_000_000_000))
            if self._deadline is not None
            else None
        )
        exhausted = False
        cancelled = False
        batch: _NativeSeriesBatch | None = None
        try:
            batch = _native_complete_series_batch(
                state,
                generation,
                required_prefix=(),
                max_frontier_states=ROOT_CHILD_MATE_SCREEN_FRONTIER,
                max_positions=remaining,
                frontier_score=frontier_score,
                native_final_score=final_score,
                should_stop=should_stop,
                native_time_budget_ns=native_time_budget_ns,
                native_threads=self.limits.native_threads,
            )
        except GenerationWorkLimit:
            # This screen's ceiling is not the whole search ceiling. An
            # incomplete screen is unknown, so continue with minimax.
            exhausted = True
        except GenerationCancelled:
            cancelled = True
        finally:
            work = (
                generation.positions_visited
                + generation.frontier_score_positions
            )
            self._root_child_mate_screen_work += work
            self._record_generation_stats(generation)

        if cancelled:
            self._check_deadline()
            raise _Timeout
        if exhausted or batch is None:
            self._root_child_mate_screen_cache[key] = None
            return None

        mate: SeriesResult | None = None
        for candidate in batch.references():
            self._check_deadline()
            if (
                candidate.outcome != Outcome.CHECKMATE
                or not candidate.ended_by_check
            ):
                continue
            try:
                replayed = play_series(state, candidate.moves)
            except ValueError:
                continue
            if replayed.outcome == Outcome.CHECKMATE and replayed.ended_by_check:
                mate = replayed
                break
        self._root_child_mate_screen_cache[key] = mate
        return mate

    @staticmethod
    def _terminal_score(
        result: SeriesResult | _NativeSeriesReference,
        mover: chess.Color,
        ply_from_root: int,
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
        result: SeriesResult | _NativeSeriesReference,
        mover: chess.Color,
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
        all_branches_visited: bool,
    ) -> tuple[int, int]:
        """Combines proof intervals without treating heuristic zero as draw.

        ``(-1, 1)`` means Black win through White win are all still possible.
        One unknown sentinel covers every absent branch, whether it was
        skipped by alpha-beta or omitted by selective frontier/final capping.
        Max/min interval combination is idempotent, and an existential mate
        for the mover remains provable in the presence of that sentinel.
        """

        if not bounds:
            return UNKNOWN_PROOF_BOUNDS
        candidates = list(bounds)
        if not all_branches_visited:
            candidates.append(UNKNOWN_PROOF_BOUNDS)
        if mover == chess.WHITE:
            return max(item[0] for item in candidates), max(
                item[1] for item in candidates
            )
        return min(item[0] for item in candidates), min(
            item[1] for item in candidates
        )

    @staticmethod
    def _materialize_series(
        result: SeriesResult | _NativeSeriesReference,
    ) -> SeriesResult:
        return (
            result.materialize()
            if isinstance(result, _NativeSeriesReference)
            else result
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
        state: ProgressiveState,
        series: list[SeriesResult],
        mover: chess.Color,
        ply_from_root: int,
        *,
        tactical_protection: bool = True,
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
            if not tactical_protection:
                return ordered[:cap]
            representatives: dict[
                tuple[int, str],
                tuple[int, SeriesResult],
            ] = {}
            aggregated_provenance = getattr(
                series,
                "tactical_provenance_by_notation",
                {},
            )
            for rank, item in enumerate(ordered):
                self._check_deadline()
                provenance = aggregated_provenance.get(item.machine_notation)
                if provenance is None:
                    provenance = _series_tactical_provenance(state, item)
                for opportunity in provenance:
                    representatives.setdefault(opportunity, (rank, item))
            tactical_candidates = {
                item.machine_notation
                for _rank, item in representatives.values()
            }
            selected_notation: set[str] = set()
            for _opportunity, (_rank, item) in sorted(
                representatives.items(),
                key=lambda entry: (
                    entry[0][0],
                    entry[1][0],
                    entry[0][1],
                    entry[1][1].machine_notation,
                ),
            ):
                selected_notation.add(item.machine_notation)
                if len(selected_notation) == cap:
                    break
            tactically_selected = set(selected_notation)
            for item in ordered:
                if len(selected_notation) == cap:
                    break
                selected_notation.add(item.machine_notation)
            ordinary_top = {
                item.machine_notation
                for item in ordered[:cap]
            }
            self.stats.tactical_final_series_retained += len(
                tactically_selected - ordinary_top
            )
            self.stats.tactical_final_reserve_drops += len(
                tactical_candidates - selected_notation
            )
            return [
                item
                for item in ordered
                if item.machine_notation in selected_notation
            ]
        return ordered

    def _apply_root_promotion_mate_lane(
        self,
        state: ProgressiveState,
        series: _GeneratedSeriesList,
        *,
        ply_from_root: int,
        required_prefix: tuple[str, ...],
        reserve_positions: int,
    ) -> _GeneratedSeriesList:
        """Runs the costly lane only when the retained root has no legal mate."""

        if ply_from_root != 1:
            return series
        if self._promotion_mate_checked:
            if self._promotion_mate_series is None:
                return series
            return _GeneratedSeriesList(
                [self._promotion_mate_series],
                width_complete=False,
            )
        if (
            self.limits.max_series_per_node is None
            or not promotion_mate_eligible(
                state,
                required_prefix=required_prefix,
            )
        ):
            # Preserve native series references as genuinely lazy on the
            # overwhelming ordinary-position path. Metadata scanning below
            # materializes a reference, so do it only when the specialized
            # lane could actually run.
            self._promotion_mate_checked = True
            return series

        for candidate in series:
            if (
                candidate.outcome != Outcome.CHECKMATE
                or not candidate.ended_by_check
            ):
                continue
            try:
                replayed = play_series(state, candidate.moves)
            except ValueError:
                continue
            if replayed.outcome == Outcome.CHECKMATE and replayed.ended_by_check:
                # Ordinary bounded generation already retained an authoritative
                # current-series mate. The specialized lane cannot improve it.
                self._promotion_mate_checked = True
                return series

        promotion_mate = self._root_promotion_mate(
            state,
            required_prefix=required_prefix,
            reserve_positions=reserve_positions,
        )
        if promotion_mate is None:
            return series
        return _GeneratedSeriesList(
            [promotion_mate],
            width_complete=False,
        )

    def _ordered_generated(
        self,
        state: ProgressiveState,
        *,
        ply_from_root: int,
        required_prefix: tuple[str, ...] = (),
        reserve_positions: int = 0,
        preferred_series: str | None = None,
    ) -> _GeneratedSeriesList:
        """Returns one deterministic capped frontier with bounded reuse.

        The ply is part of the key because terminal mate-distance ordering is
        expressed relative to the root. The state itself fixes the mover and
        every other generation input; a root prefix is included verbatim so
        fixed-prefix analysis cannot alias an unconstrained frontier.
        """

        self._check_deadline()
        tactical_protection = self._tactical_frontier_protection_enabled(
            state,
            ply_from_root=ply_from_root,
            required_prefix=required_prefix,
        )
        key = (
            state.transposition_key,
            required_prefix,
            ply_from_root,
            tactical_protection,
        )
        cached = self._series_generation_cache.get(key)
        if cached is not None:
            self._series_generation_cache.move_to_end(key)
            self.stats.series_generation_cache_hits += 1
            ordered: list[SeriesResult | _NativeSeriesReference] = (
                cached.collection.references()
                if isinstance(cached.collection, _NativeSeriesBatch)
                else list(cached.collection)
            )
            self._prefer_series(ordered, preferred_series)
            return self._apply_root_promotion_mate_lane(
                state,
                _GeneratedSeriesList(
                    ordered,
                    width_complete=cached.width_complete,
                ),
                ply_from_root=ply_from_root,
                required_prefix=required_prefix,
                reserve_positions=reserve_positions,
            )

        generated, width_complete = self._generate(
            state,
            ply_from_root=ply_from_root,
            required_prefix=required_prefix,
            reserve_positions=reserve_positions,
            tactical_protection=tactical_protection,
        )
        if isinstance(generated, _NativeSeriesBatch):
            ordered = generated.references()
            collection: tuple[SeriesResult, ...] | _NativeSeriesBatch = generated
        else:
            generated_count = len(generated)
            ordered = self._ordered(
                state,
                generated,
                state.board.turn,
                ply_from_root,
                tactical_protection=tactical_protection,
            )
            width_complete = width_complete and len(ordered) == generated_count
            collection = tuple(ordered)
        weight = max(1, len(ordered))
        if weight > SERIES_GENERATION_CACHE_CAPACITY:
            self._prefer_series(ordered, preferred_series)
            return self._apply_root_promotion_mate_lane(
                state,
                _GeneratedSeriesList(
                    ordered,
                    width_complete=width_complete,
                ),
                ply_from_root=ply_from_root,
                required_prefix=required_prefix,
                reserve_positions=reserve_positions,
            )
        while (
            self._series_generation_cache
            and self._series_generation_cache_weight + weight
            > SERIES_GENERATION_CACHE_CAPACITY
        ):
            _, evicted = self._series_generation_cache.popitem(last=False)
            self._series_generation_cache_weight -= max(
                1,
                len(evicted.collection),
            )
            self.stats.series_generation_cache_evictions += 1
        self._series_generation_cache[key] = _SeriesCacheEntry(
            collection,
            width_complete,
        )
        self._series_generation_cache_weight += weight
        self.stats.series_generation_cache_peak = max(
            self.stats.series_generation_cache_peak,
            self._series_generation_cache_weight,
        )
        self._prefer_series(ordered, preferred_series)
        return self._apply_root_promotion_mate_lane(
            state,
            _GeneratedSeriesList(
                ordered,
                width_complete=width_complete,
            ),
            ply_from_root=ply_from_root,
            required_prefix=required_prefix,
            reserve_positions=reserve_positions,
        )

    @staticmethod
    def _prefer_series(
        series: list[SeriesResult | _NativeSeriesReference],
        preferred_series: str | None,
    ) -> None:
        """Moves an already-legal PV/hash series to the front in-place.

        The cached frontier retains its deterministic static ordering. A
        preference is applied only to the per-call list, so iterative depths
        and transpositions cannot poison the canonical generation cache.
        """

        if preferred_series is None:
            return
        for index, result in enumerate(series):
            if result.machine_notation == preferred_series:
                if index:
                    series.insert(0, series.pop(index))
                return

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

        key = state.transposition_key
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
        preferred_candidate = (
            entry.pv[0]
            if (
                self.limits.depth_series >= 4
                and entry is not None
                and entry.depth < depth
                and entry.pv
            )
            else None
        )
        preferred_series = (
            entry.pv[0].machine_notation
            if entry is not None and entry.pv
            else None
        )
        search_alpha, search_beta = alpha, beta
        best_score = -MATE_SCORE * 2 if mover == chess.WHITE else MATE_SCORE * 2
        best_candidate: SeriesResult | _NativeSeriesReference | None = None
        best_child_pv: tuple[SeriesResult, ...] = ()
        child_bounds: list[tuple[int, int]] = []
        cutoff_before_generation = False
        previsited_series: str | None = None

        if preferred_candidate is not None:
            self._check_deadline()
            terminal = self._terminal_score(
                preferred_candidate,
                mover,
                ply_from_root + 1,
            )
            if terminal is None:
                score, child_pv, proof_bounds = self._minimax(
                    preferred_candidate.final_state,
                    depth - 1,
                    alpha,
                    beta,
                    ply_from_root + 1,
                )
            else:
                score, child_pv = terminal, ()
                proof_bounds = self._terminal_proof_bounds(
                    preferred_candidate,
                    mover,
                )
            child_bounds.append(proof_bounds)
            best_score = score
            best_candidate = preferred_candidate
            best_child_pv = child_pv
            previsited_series = preferred_series

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
                cutoff_before_generation = True
            else:
                if mover == chess.WHITE:
                    alpha = max(alpha, best_score)
                else:
                    beta = min(beta, best_score)
                if alpha >= beta:
                    self.stats.alpha_beta_cutoffs += 1
                    cutoff_before_generation = True

        width_complete = False
        series_count = 0
        if not cutoff_before_generation:
            series = self._ordered_generated(
                state,
                ply_from_root=ply_from_root + 1,
                preferred_series=preferred_series,
            )
            width_complete = series.width_complete
            series_count = len(series)
            if not series:
                return 0, (), UNKNOWN_PROOF_BOUNDS
            if previsited_series is not None and not any(
                result.machine_notation == previsited_series for result in series
            ):
                # A TT path created by this searcher should remain in the same
                # deterministic retained frontier. If that invariant ever
                # changes, discard the probe instead of changing cap semantics.
                previsited_series = None
                alpha, beta = search_alpha, search_beta
                best_score = (
                    -MATE_SCORE * 2 if mover == chess.WHITE else MATE_SCORE * 2
                )
                best_candidate = None
                best_child_pv = ()
                child_bounds = []

            for result in series:
                if (
                    previsited_series is not None
                    and result.machine_notation == previsited_series
                ):
                    continue
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
                    best_candidate = result
                    best_child_pv = child_pv

                immediate_mate_score = MATE_SCORE - (ply_from_root + 1)
                if (
                    mover == chess.WHITE
                    and best_score == immediate_mate_score
                    or mover == chess.BLACK
                    and best_score == -immediate_mate_score
                ):
                    break

                if mover == chess.WHITE:
                    alpha = max(alpha, best_score)
                else:
                    beta = min(beta, best_score)
                if alpha >= beta:
                    self.stats.alpha_beta_cutoffs += 1
                    break

        best_pv = (
            (self._materialize_series(best_candidate),) + best_child_pv
            if best_candidate is not None
            else ()
        )

        if best_score <= original_alpha:
            bound = Bound.UPPER
        elif best_score >= original_beta:
            bound = Bound.LOWER
        else:
            bound = Bound.EXACT
        proof_bounds = self._combine_proof_bounds(
            mover,
            child_bounds,
            all_branches_visited=(
                not cutoff_before_generation
                and width_complete
                and len(child_bounds) == series_count
            ),
        )
        replacement = _TTEntry(depth, best_score, bound, best_pv, proof_bounds)
        if (
            entry is None
            or depth > entry.depth
            or (
                depth == entry.depth
                and replacement.bound == Bound.EXACT
                and entry.bound != Bound.EXACT
            )
        ):
            self._tt[key] = replacement
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
        reserve_positions = (
            state.moves_available
            if self.limits.max_generation_positions is not None
            else 0
        )
        try:
            series = self._ordered_generated(
                state,
                ply_from_root=1,
                required_prefix=required_prefix,
                reserve_positions=reserve_positions,
                preferred_series=self._preferred_root_series,
            )
            width_complete = series.width_complete
        except _WorkLimit as error:
            # The ordinary frontier exhausted only the non-reserved part of
            # the deterministic budget. Spend the remaining at-most-one-
            # position-per-micro-move allowance on a legal width-one seed so
            # engine play does not fail merely because no scored root existed.
            fallback = self._generate_root_seed(
                state,
                required_prefix=required_prefix,
            )
            raise _RootInterrupted((), error, fallback) from error
        scored: list[ScoredSeries] = []
        root_alpha = -MATE_SCORE * 2
        root_beta = MATE_SCORE * 2
        for result in series:
            try:
                self._check_deadline()
                terminal = self._terminal_score(result, mover, 1)
                if terminal is None:
                    child_state = result.final_state
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
                        child_state,
                        depth - 1,
                        child_alpha,
                        child_beta,
                        1,
                    )
                    score_is_exact = (
                        self.limits.collect_all_root_scores
                        or child_alpha < score < child_beta
                    )
                    if not score_is_exact and scored:
                        exact_best = min(
                            scored,
                            key=lambda item: (
                                -item.score
                                if mover == chess.WHITE
                                else item.score,
                                item.series.machine_notation,
                            ),
                        )
                        if (
                            score == exact_best.score
                            and result.machine_notation
                            < exact_best.series.machine_notation
                        ):
                            # A root bound equal to the current best can hide an
                            # equal, lexicographically earlier canonical move.
                            # Re-search only that candidate at a full window;
                            # ordinary later ties and clear non-improvements
                            # retain the fast best-only path.
                            score, child_pv, proof_bounds = self._minimax(
                                child_state,
                                depth - 1,
                                -MATE_SCORE * 2,
                                MATE_SCORE * 2,
                                1,
                            )
                            score_is_exact = True

                    # Rejected candidates pay no wide-frontier cost. Every
                    # candidate that could become the root choice is screened
                    # before it is allowed to tighten the root window.
                    contender = (
                        not self.limits.collect_all_root_scores
                        and score_is_exact
                        and (
                            not scored
                            or (
                                -score if mover == chess.WHITE else score,
                                result.machine_notation,
                            )
                            < min(
                                (
                                    -item.score
                                    if mover == chess.WHITE
                                    else item.score,
                                    item.series.machine_notation,
                                )
                                for item in scored
                            )
                        )
                    )
                    if contender:
                        reply_mate = self._root_child_immediate_mate(child_state)
                        if reply_mate is not None:
                            child_mover = child_state.board.turn
                            screened_score = self._terminal_score(
                                reply_mate,
                                child_mover,
                                2,
                            )
                            if screened_score is None:  # pragma: no cover
                                raise RuntimeError(
                                    "replay-proven mate has no terminal score"
                                )
                            score = screened_score
                            child_pv = (reply_mate,)
                            proof_bounds = self._terminal_proof_bounds(
                                reply_mate,
                                child_mover,
                            )
                            score_is_exact = True
                else:
                    score, child_pv = terminal, ()
                    proof_bounds = self._terminal_proof_bounds(result, mover)
                    score_is_exact = True
            except (_Timeout, _WorkLimit) as error:
                # Root generation already produced a deterministic ordered
                # frontier. Preserve its first fully legal series even when
                # no child received a complete score before cancellation.
                # The caller marks it as an unscored emergency fallback and
                # never turns it into proof or an evaluated alternative.
                raise _RootInterrupted(
                    tuple(scored),
                    error,
                    self._materialize_series(series[0]),
                ) from error
            except _AdjudicationPending as error:
                # A child reaching the quiet-series threshold can make the
                # whole minimax iteration unknown. The ordered root frontier
                # is nevertheless complete and legal, so carry one series to
                # the engine-play liveness path without assigning the child a
                # draw or heuristic minimax score.
                self._root_scores_complete = False
                raise _RootAdjudicationPending(
                    self._materialize_series(series[0])
                ) from error
            if score_is_exact:
                scored.append(
                    ScoredSeries(
                        self._materialize_series(result),
                        score,
                        child_pv,
                        proof_bounds,
                    )
                )
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
        # This flag intentionally means every retained root candidate has an
        # exact score. ``exact_width`` separately reports whether the retained
        # frontier contains every legal branch.
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
            all_branches_visited=(
                width_complete and len(scored) == len(series)
            ),
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
        self._preferred_root_series = None
        self._root_tactical_frontier_protection = (
            _tactical_frontier_protection_eligible(
                state,
                required_prefix=required_prefix,
            )
        )
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
                engine_profile_id=self.engine_profile_id,
                engine_profile_name=self.engine_profile_name,
                required_prefix=required_prefix,
                work_limit_reached=(
                    self._quiet_work_limit_reached
                    or self._evaluation_work_limit_reached
                ),
                max_generation_positions=self.limits.max_generation_positions,
                root_scores_complete=False,
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
                engine_profile_id=self.engine_profile_id,
                engine_profile_name=self.engine_profile_name,
                required_prefix=required_prefix,
                work_limit_reached=(
                    self._quiet_work_limit_reached
                    or self._evaluation_work_limit_reached
                ),
                max_generation_positions=self.limits.max_generation_positions,
                root_scores_complete=(
                    adjudication == "proven-draw-no-mating-material"
                ),
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
                    if interrupted.scored:
                        mover = state.board.turn
                        partial = tuple(
                            sorted(
                                interrupted.scored,
                                key=lambda item: (
                                    -item.score
                                    if mover == chess.WHITE
                                    else item.score,
                                    item.series.machine_notation,
                                ),
                            )
                        )
                        best = partial[0]
                        best_score = best.score
                        best_pv = (best.series,) + best.principal_variation
                        alternatives = partial
                    else:
                        # This is a move-only liveness fallback. Keep the
                        # explicitly labeled root evaluation rather than
                        # pretending the chosen series completed a search.
                        best_score = root_evaluation.total
                        best_pv = (interrupted.fallback,)
                        alternatives = ()
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
            except _AdjudicationPending as pending:
                if completed_depth > 0:
                    # The deeper iteration is unknown, but the last fully
                    # completed iteration remains a legal search result. Keep
                    # its score, PV, alternatives, and proof while making the
                    # incomplete requested horizon explicit. This is not a
                    # quiet-draw adjudication and must never manufacture one.
                    self._selective = True
                    adjudication = "manual-proof-required"
                    break
                if isinstance(pending, _RootAdjudicationPending):
                    # No iteration completed, but root generation did. Return
                    # its first deterministic legal series solely so engine
                    # play remains live. Score/proof/alternatives deliberately
                    # stay at the root/no-proof values because the child was
                    # not adjudicated or evaluated.
                    self._selective = True
                    self._root_scores_complete = False
                    adjudication = "manual-proof-required"
                    best_score = root_evaluation.total
                    best_pv = (pending.fallback,)
                    alternatives = ()
                    best_proof = None
                    break
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
                    engine_profile_id=self.engine_profile_id,
                    engine_profile_name=self.engine_profile_name,
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
            self._preferred_root_series = (
                best_pv[0].machine_notation if best_pv else None
            )

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
            engine_profile_id=self.engine_profile_id,
            engine_profile_name=self.engine_profile_name,
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
    evaluation_overlay: EvaluationOverlay | None = None,
) -> SearchResult:
    return SeriesSearcher(
        limits or SearchLimits(),
        profile,
        evaluation_overlay,
    ).run(
        state,
        required_prefix=required_prefix,
    )
