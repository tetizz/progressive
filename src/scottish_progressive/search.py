from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from enum import StrEnum
import time
from typing import TYPE_CHECKING, Mapping, Protocol

import chess

from .evaluation import EvaluationBreakdown, classify_score, evaluate, fast_evaluate
from .model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    Outcome,
    ProgressiveState,
    SeriesResult,
)
from .native_subtree import (
    SUBTREE_STAT_FIELDS,
    NativeSubtreeSession,
    native_subtree_eligible,
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
    _native_complete_series_generation,
    _series_tactical_provenance,
    generate_series,
    play_series,
    quiet_adjudication_status,
)

if TYPE_CHECKING:
    from .mate_proof_cache import MateProofCache
    from .series_mate import SeriesMateProbe


MATE_SCORE = 1_000_000
UNKNOWN_PROOF_BOUNDS = (-1, 1)
# Leaf evaluators must stay outside the reserved mate/proof band used by
# ``SearchResult.forced`` and the public analysis notation. An experimental
# overlay is allowed to reorder ordinary positions, but never to numerically
# outrank a sound terminal proof or manufacture a mate-looking heuristic.
MAX_EVALUATION_OVERLAY_SCORE = MATE_SCORE - 10_000 - 1
# The ten-quiet-series mate exception is a proof search inside the ordinary
# series search.  It must have a search-wide ceiling of its own: otherwise a
# wide node can start one 100k-node probe per child without any of that work
# appearing in ``max_generation_positions``.  Exhaustion is conservative --
# it yields manual-proof-required, never a fabricated draw.
QUIET_ADJUDICATION_POSITION_LIMIT = 4_096
# Complete progressive series are far costlier to generate than orthodox
# single moves. Iterative deepening revisits the same boundary frontiers, but
# retaining every frontier would multiply memory across league workers. Bound
# reuse by the number of retained SeriesResult objects, not by node count. The
# 65K ceiling matches the certified desktop browser geometry and removes D4
# LRU churn without increasing the retained search width.
SERIES_GENERATION_CACHE_CAPACITY = 65_536
# Best-only play may otherwise accept a root series whose opponent reply mate
# was discarded by the ordinary width-32 child beam. This second screen is
# deliberately much wider and globally bounded. It is invoked only after a
# fully scored root candidate can become the new choice, so early positions pay
# for successive contenders rather than a blanket wide/tactical search.
ROOT_CHILD_MATE_SCREEN_FRONTIER = 832
ROOT_CHILD_EARLY_MATE_SCREEN_FRONTIER = 4_096
ROOT_CHILD_MATE_SCREEN_CHEAP_FRONTIER = 32
ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT = 3_000_000
# Preserve one fifth of the shared safety allowance for the established
# staged current-series screens whenever the exact native solver is unknown.
# At the hosted 10M search cap the exact lane still receives 1.28M work,
# enough for the accepted S8 mate receipt while leaving a conservative
# fallback instead of turning WorkLimit or Unsupported into a no-mate claim.
ROOT_CHILD_NATIVE_MATE_FALLBACK_DENOMINATOR = 5
# Exact reply safety is not a late-game optimization. A capped legacy miss is
# selective at every series number, including the opening, so every valid
# reply child must reach the authoritative native Found/Exhausted lane before
# it can be settled or cached as safe.
ROOT_CHILD_NATIVE_MATE_MIN_SERIES = 1
# A single exact late-series negative can consume nearly the entire shared
# root-safety allowance before the established tactical screen finds a mate in
# a few thousand positions. Bound that speculative lane per child so one early
# contender cannot starve the final winner. Series 5-6 remain uncapped here:
# their exhaustive negatives are affordable and are required by the live D2
# safe-reply gate.
ROOT_CHILD_NATIVE_MATE_LATE_SERIES_WORK_LIMIT = 250_000
ROOT_CHILD_MATE_SCREEN_MIN_SERIES = 7
# A cap-32 root whose every retained child has a replay-proven reply mate is a
# selector failure signal, not a proof that every legal root series loses.
# Regenerate one bounded tactical frontier before accepting that result. This
# is deliberately root-only; widening every descendant would erase the hosted
# depth contract.
ROOT_ALL_MATING_WIDEN_FRONTIER = 832
ROOT_ALL_MATING_WIDEN_MAX_SERIES = 8
# Five- and six-move reply series can hide mating routes behind several quiet
# prefixes. Keep this range local to successive root-contender safety
# screens: lowering the general tactical-frontier threshold would widen every
# descendant and erase a completed search depth in early play.
ROOT_CHILD_ADAPTIVE_MATE_SCREEN_MIN_SERIES = 5
ROOT_CHILD_ADAPTIVE_MATE_SCREEN_MAX_SERIES = 6
# A low-risk root may still expose a concrete promotion tactic in the
# opponent's immediately following series.  Protect that one-series safety
# horizon, but do not let broad structural promotion eligibility at distant
# Series-7 descendants inflate an otherwise ordinary depth-five search.
TACTICAL_DESCENDANT_PROMOTION_MAX_PLY = 2
# Tactical final capping must not replace every static-evaluation leader. Keep
# at least half of each capped beam for the ordinary order; terminal mates are
# seeded first and may consume more when they alone fill the cap. Keep this
# ratio synchronized with FINAL_ORDINARY_QUOTA_DENOMINATOR in _native_eval.cpp.
TACTICAL_FINAL_ORDINARY_QUOTA_DENOMINATOR = 2
ROOT_PVS_ENABLED = True
NATIVE_SUBTREE_ENABLED = True
# Root scouts are transactional and concrete PV caches include the exact FEN
# clocks, promoted provenance, and Chess960 mode.  That makes the same
# fail-soft one-point probe safe at later Progressive roots as well as the
# single-move opening root; failed probes cannot leak bound PV ordering into
# the canonical full-window result.


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
    requires_exact_work_receipt: bool

    def score(self, state: ProgressiveState, hand_score: int) -> int: ...

    def score_with_work(
        self,
        state: ProgressiveState,
        hand_score: int,
        max_work_positions: int | None,
    ) -> EvaluationOverlayScore: ...


@dataclass(frozen=True, slots=True)
class EvaluationOverlayScore:
    """One overlay leaf value and every additional logical position it used."""

    score: int
    reach_positions: int = 0
    direct_move_variants: int = 0
    two_move_variants: int = 0
    complete: bool = True

    def __post_init__(self) -> None:
        if type(self.score) is not int:
            raise TypeError("evaluation overlay score must be an exact integer")
        for name in (
            "reach_positions",
            "direct_move_variants",
            "two_move_variants",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise TypeError(
                    f"evaluation overlay {name} must be a nonnegative integer"
                )
        if type(self.complete) is not bool:
            raise TypeError("evaluation overlay complete must be an exact bool")

    @property
    def work_positions(self) -> int:
        return (
            self.reach_positions
            + self.direct_move_variants
            + self.two_move_variants
        )


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
    continue_after_root_mate: bool = False

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
    pvs_zero_window_searches: int = 0
    pvs_researches: int = 0
    pvs_tt_writes_rolled_back: int = 0
    root_pvs_zero_window_searches: int = 0
    root_pvs_researches: int = 0
    root_pvs_tt_writes_rolled_back: int = 0
    root_bound_candidates: int = 0
    root_safety_passes: int = 0
    root_safety_retries: int = 0
    root_safety_screen_calls: int = 0
    root_safety_screen_cache_hits: int = 0
    root_safety_screen_stages: int = 0
    root_safety_screen_positions: int = 0
    root_safety_promotion_cache_hits: int = 0
    root_safety_budget_interruptions: int = 0
    root_safety_unknown_interruptions: int = 0
    root_safety_proven_mate_children: int = 0
    root_safety_exact_exhausted_children: int = 0
    root_safety_exhausted_fallbacks: int = 0
    root_safety_terminal_fallbacks: int = 0
    root_safety_unknown_fallbacks: int = 0
    root_safety_all_mating_widenings: int = 0
    root_safety_widened_candidates: int = 0
    root_safety_widening_positions: int = 0
    root_safety_widened_terminal_mates: int = 0
    root_safety_widened_exact_children: int = 0
    native_series_mate_calls: int = 0
    native_series_mate_positions: int = 0
    native_series_mate_edges: int = 0
    native_series_mate_cache_hits: int = 0
    native_series_mate_found: int = 0
    native_series_mate_exhausted: int = 0
    native_series_mate_work_limit_hits: int = 0
    native_series_mate_deadline_hits: int = 0
    native_series_mate_unsupported: int = 0
    mate_proof_cache_hits: int = 0
    mate_proof_cache_found_hits: int = 0
    mate_proof_cache_exhausted_hits: int = 0
    mate_proof_cache_misses: int = 0
    mate_proof_cache_store_attempts: int = 0
    mate_proof_cache_evictions: int = 0
    mate_proof_cache_work_saved: int = 0
    mate_proof_cache_errors: int = 0
    branch_caps: int = 0
    series_generation_positions: int = 0
    frontier_score_positions: int = 0
    static_evaluation_positions: int = 0
    evaluation_reach_positions: int = 0
    evaluation_capture_positions: int = 0
    incomplete_reach_evaluations: int = 0
    tactical_leaf_extensions: int = 0
    overlay_evaluations: int = 0
    overlay_reach_positions: int = 0
    overlay_direct_move_variants: int = 0
    overlay_two_move_variants: int = 0
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
    series_generation_cache_entries_peak: int = 0
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
        probing, exact native mate positions and generated edges, static
        evaluation, evaluation reach, and quiet-proof work.
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


def _root_candidate_is_proven_adverse(
    mover: chess.Color,
    candidate: ScoredSeries,
) -> bool:
    """Returns whether sound exact bounds prove the mover loses this line."""

    adverse_value = -1 if mover == chess.WHITE else 1
    return candidate.proof_bounds == (adverse_value, adverse_value)


def _proof_safe_root_order(
    mover: chess.Color,
    scored: list[ScoredSeries] | tuple[ScoredSeries, ...],
) -> tuple[ScoredSeries, ...]:
    """Ranks exact root scores without choosing a proven loss over uncertainty.

    Proof bounds outrank heuristic scores only for the single sound fact they
    establish here: an exact opponent win. Unknown/partial intervals remain
    eligible. If every scored line is proven adverse, preserve the historical
    score and notation order so the engine still chooses its best resistance.
    """

    canonical = tuple(
        sorted(
            scored,
            key=lambda item: (
                -item.score if mover == chess.WHITE else item.score,
                item.series.machine_notation,
            ),
        )
    )
    if not canonical or all(
        _root_candidate_is_proven_adverse(mover, item) for item in canonical
    ):
        return canonical
    return tuple(
        item
        for adverse in (False, True)
        for item in canonical
        if _root_candidate_is_proven_adverse(mover, item) is adverse
    )


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


_SearchKey = tuple[object, ...]
_TTKey = tuple[_SearchKey, int, int, int, bool]


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
        *,
        mate_proof_cache: MateProofCache | None = None,
    ) -> None:
        self.limits = limits
        self.profile = profile or baseline_profile()
        self.evaluation_overlay = evaluation_overlay
        self.mate_proof_cache = mate_proof_cache
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
        self._tt: dict[_TTKey, _TTEntry] = {}
        # A PVS null-window probe is deliberately speculative.  Every TT write
        # made below that probe is journaled, including repeated writes to one
        # key.  Nested probes own nested journals and always restore their
        # caller's view before returning or propagating an interruption.
        self._tt_transaction_stack: list[
            list[
                tuple[
                    _TTKey,
                    _TTEntry | None,
                ]
            ]
        ] = []
        self._eval_cache: dict[_SearchKey, EvaluationBreakdown] = {}
        self._quiet_adjudication_cache: dict[_SearchKey, str | None] = {}
        self._series_generation_cache: OrderedDict[
            tuple[
                _SearchKey,
                int,
                int,
                int,
                bool,
                tuple[str, ...],
                int,
                bool,
            ],
            _SeriesCacheEntry,
        ] = OrderedDict()
        self._series_generation_cache_weight = 0
        self._deadline: float | None = None
        self._selective = False
        self._quiet_work_limit_reached = False
        self._evaluation_work_limit_reached = False
        self._root_scores_complete = True
        self._preferred_root_series: str | None = None
        # Root promotion results are concrete replay objects. Memoize by the
        # exact boundary and prefix rather than with a searcher-wide singleton,
        # so a reused searcher cannot return clocks or moves from another root.
        self._root_promotion_mate_cache: dict[
            tuple[_TTKey, tuple[str, ...]], SeriesResult | None
        ] = {}
        # Tactical beam protection is selected from the search root, not from
        # the deepest descendant reached by iterative minimax.  Otherwise an
        # ordinary Series-4 root silently switches to the much wider reserve
        # after descending to Series 7 and can lose an entire completed depth
        # to beam work.  ``None`` keeps direct private-generation probes useful:
        # their first state is treated as their root.
        self._root_tactical_frontier_protection: bool | None = None
        # These caches retain concrete SeriesResult objects, so their keys must
        # preserve every state field that affects replayed PFEN output. The
        # position-only transposition key deliberately omits FEN clocks and
        # would let a mate found at one clock pair leak stale final-state clocks
        # into an otherwise identical boundary reached later in the same run.
        self._root_child_mate_screen_cache: dict[
            _TTKey, SeriesResult | None
        ] = {}
        self._root_child_native_mate_cache_keys: set[_TTKey] = set()
        self._root_child_promotion_mate_cache: dict[
            _TTKey, SeriesResult | None
        ] = {}
        self._root_child_proven_mate_keys: set[
            tuple[int, str, int, int]
        ] = set()
        self._root_child_native_mate_exhausted_keys: set[
            tuple[int, str, int, int]
        ] = set()
        self._root_widened_terminal_series: SeriesResult | None = None
        configured_work = self.limits.max_generation_positions
        if configured_work is None:
            self._root_child_mate_screen_budget = (
                ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT
            )
        elif self.limits.collect_all_root_scores:
            # Full-window root scoring already obeys the caller's shared work
            # ceiling. Dividing that same small envelope again can make exact
            # reply safety fail before a low-budget label search scores depth
            # one. Retain the independent 3M safety guard for larger callers.
            self._root_child_mate_screen_budget = min(
                ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT,
                configured_work,
            )
        else:
            self._root_child_mate_screen_budget = min(
                ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT,
                configured_work // 3,
            )
        self._root_child_mate_screen_work = 0
        self._native_subtree_session: NativeSubtreeSession | None = None
        self._native_subtree_stats_applied = (0,) * len(SUBTREE_STAT_FIELDS)

    def _tactical_frontier_protection_enabled(
        self,
        state: ProgressiveState,
        *,
        ply_from_root: int = 1,
        required_prefix: tuple[str, ...] = (),
    ) -> bool:
        """Returns the root-stable tactical policy for one generated node.

        Every root protects distinct tactical candidates before the first
        irreversible beam cut. A late or promotion-risk root also protects
        every descendant in the search. Descendants of an earlier ordinary
        root remain on the fixed-width fast path unless a concrete promotion
        risk appears in the immediate opponent-series safety horizon.
        """

        if self._root_tactical_frontier_protection is None:
            self._root_tactical_frontier_protection = (
                _tactical_frontier_protection_eligible(
                    state,
                    required_prefix=required_prefix,
                )
            )
        if ply_from_root == 1:
            return True
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

    def _start_native_subtree(self, state: ProgressiveState) -> None:
        """Starts the descendant-only core after Python root policy is settled."""

        if (
            not NATIVE_SUBTREE_ENABLED
            or not native_subtree_eligible(
                state,
                requested_depth=self.limits.depth_series,
                max_series_per_node=self.limits.max_series_per_node,
                max_work=self.limits.max_generation_positions,
                profile=self.profile,
                has_overlay=self.evaluation_overlay is not None,
            )
        ):
            return
        cap = self.limits.max_series_per_node
        if cap is None:  # narrowed by native_subtree_eligible
            return
        self._native_subtree_session = NativeSubtreeSession(
            max_series_per_node=cap,
            max_work=self.limits.max_generation_positions,
            requested_depth=self.limits.depth_series,
            mate_score=MATE_SCORE,
            cache_capacity=SERIES_GENERATION_CACHE_CAPACITY,
            external_cache_weight=self._series_generation_cache_weight,
            native_threads=self.limits.native_threads,
            root_tactical_protection=bool(
                self._root_tactical_frontier_protection
            ),
            profile=self.profile,
        )

    def _sync_native_subtree_stats(self, raw: tuple[int, ...]) -> None:
        if len(raw) != len(SUBTREE_STAT_FIELDS):
            raise RuntimeError("native subtree stats shape mismatch")
        previous = self._native_subtree_stats_applied
        peak_fields = {
            "peak_frontier_states",
            "series_generation_cache_peak",
            "series_generation_cache_entries_peak",
        }
        for index, field_name in enumerate(SUBTREE_STAT_FIELDS):
            value = raw[index]
            if value < previous[index]:
                raise RuntimeError("native subtree stats regressed")
            if field_name in peak_fields:
                continue
            setattr(
                self.stats,
                field_name,
                getattr(self.stats, field_name) + value - previous[index],
            )
        peak_frontier_index = SUBTREE_STAT_FIELDS.index("peak_frontier_states")
        self.stats.peak_frontier_states = max(
            self.stats.peak_frontier_states,
            raw[peak_frontier_index],
        )
        cache_peak_index = SUBTREE_STAT_FIELDS.index(
            "series_generation_cache_peak"
        )
        cache_entries_index = SUBTREE_STAT_FIELDS.index(
            "series_generation_cache_entries_peak"
        )
        # Root generation remains intentionally Python-owned. Its small cached
        # frontier is the only storage outside the native descendant LRU, so
        # include that live weight in the public unified peak counters.
        self.stats.series_generation_cache_peak = max(
            self.stats.series_generation_cache_peak,
            raw[cache_peak_index],
        )
        self.stats.series_generation_cache_entries_peak = max(
            self.stats.series_generation_cache_entries_peak,
            raw[cache_entries_index],
        )
        self._native_subtree_stats_applied = raw

    def _native_minimax(
        self,
        state: ProgressiveState,
        depth: int,
        alpha: int,
        beta: int,
        ply_from_root: int,
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[int, int]]:
        session = self._native_subtree_session
        if session is None:  # pragma: no cover - caller invariant
            raise RuntimeError("native subtree session is unavailable")
        work_index = SUBTREE_STAT_FIELDS.index("generation_positions")
        native_work = self._native_subtree_stats_applied[work_index]
        external_work = self.stats.generation_positions - native_work
        if external_work < 0:
            raise RuntimeError("native subtree external work accounting regressed")
        remaining_nanoseconds = (
            None
            if self._deadline is None
            else max(
                0,
                int((self._deadline - time.perf_counter()) * 1_000_000_000),
            )
        )
        result = session.search(
            state,
            depth=depth,
            alpha=alpha,
            beta=beta,
            ply_from_root=ply_from_root,
            external_work=external_work,
            remaining_nanoseconds=remaining_nanoseconds,
        )
        self._sync_native_subtree_stats(result.stats)
        self._selective = self._selective or result.selective
        self._evaluation_work_limit_reached = (
            self._evaluation_work_limit_reached
            or result.evaluation_work_limit_reached
        )
        if result.status == 1:
            raise _WorkLimit
        if result.status == 2:
            raise _Timeout
        if result.status == 3:
            raise _AdjudicationPending
        if result.status != 0:
            raise RuntimeError(
                result.message or "native subtree search is unsupported"
            )
        return result.score, result.principal_variation, result.proof_bounds

    def _quiet_adjudication(self, state: ProgressiveState) -> str | None:
        if not state.quiet_draw_pending:
            return None
        self._check_deadline()
        key = state.search_key
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
        key = state.search_key
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
            tactical_positions = cached.capture_reach_positions
            probe_positions = reach_positions + tactical_positions
            self.stats.evaluation_reach_positions += reach_positions
            self.stats.evaluation_capture_positions += tactical_positions
            self.stats.generation_positions += probe_positions
            if not cached.reach_complete:
                self.stats.incomplete_reach_evaluations += 1
                if (
                    remaining is not None
                    and probe_positions >= remaining
                ):
                    if not self._evaluation_work_limit_reached:
                        self.stats.generation_work_limit_hits += 1
                    self._evaluation_work_limit_reached = True
                    self._selective = True
            if self.evaluation_overlay is not None:
                exact_work_method = getattr(
                    self.evaluation_overlay,
                    "score_with_work",
                    None,
                )
                if callable(exact_work_method):
                    overlay_remaining = (
                        None
                        if self.limits.max_generation_positions is None
                        else max(
                            0,
                            self.limits.max_generation_positions
                            - self.stats.generation_positions,
                        )
                    )
                    overlay_score = exact_work_method(
                        state,
                        cached.total,
                        overlay_remaining,
                    )
                    if type(overlay_score) is not EvaluationOverlayScore:
                        raise TypeError(
                            "evaluation overlay exact-work result has the wrong type"
                        )
                    self.stats.overlay_evaluations += 1
                    self.stats.overlay_reach_positions += (
                        overlay_score.reach_positions
                    )
                    self.stats.overlay_direct_move_variants += (
                        overlay_score.direct_move_variants
                    )
                    self.stats.overlay_two_move_variants += (
                        overlay_score.two_move_variants
                    )
                    self.stats.evaluation_reach_positions += (
                        overlay_score.reach_positions
                    )
                    self.stats.generation_positions += overlay_score.work_positions
                    if (
                        not overlay_score.complete
                        or overlay_remaining is not None
                        and overlay_score.work_positions > overlay_remaining
                    ):
                        if not self._evaluation_work_limit_reached:
                            self.stats.generation_work_limit_hits += 1
                        self._evaluation_work_limit_reached = True
                        self._selective = True
                        raise _WorkLimit
                    blended_total = overlay_score.score
                else:
                    if getattr(
                        self.evaluation_overlay,
                        "requires_exact_work_receipt",
                        False,
                    ):
                        raise RuntimeError(
                            "evaluation overlay requires an exact work receipt"
                        )
                    blended_total = self.evaluation_overlay.score(
                        state,
                        cached.total,
                    )
                    if type(blended_total) is not int:
                        raise TypeError(
                            "evaluation overlay score must be an exact integer"
                        )
                blended_total = max(
                    -MAX_EVALUATION_OVERLAY_SCORE,
                    min(MAX_EVALUATION_OVERLAY_SCORE, blended_total),
                )
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
        """Runs the bounded current-series mate lane once per exact root/prefix."""

        cache_key = (self._tt_key(state), required_prefix)
        if cache_key in self._root_promotion_mate_cache:
            return self._root_promotion_mate_cache[cache_key]
        self._root_promotion_mate_cache[cache_key] = None
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
        self._root_promotion_mate_cache[cache_key] = probe.series
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
        max_frontier_states: int | None = None,
    ) -> tuple[list[SeriesResult] | _NativeSeriesBatch, bool]:
        frontier_limit = (
            self.limits.max_series_per_node
            if max_frontier_states is None
            else max_frontier_states
        )
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
                max_returned_series=frontier_limit,
                ply_from_root=ply_from_root,
                mate_score=MATE_SCORE,
            )
            if frontier_limit is not None
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
                    max_frontier_states=frontier_limit,
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
                    max_frontier_states=frontier_limit,
                    max_positions=remaining_positions,
                    frontier_score=(
                        frontier_score
                        if frontier_limit is not None
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

    def _root_child_mate_screen_remaining(self) -> int:
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
        return max(0, remaining)

    def _record_native_series_mate_probe(
        self,
        probe: SeriesMateProbe,
    ) -> None:
        work = probe.positions_visited + probe.moves_generated
        self.stats.native_series_mate_positions += probe.positions_visited
        self.stats.native_series_mate_edges += probe.moves_generated
        self._root_child_mate_screen_work += work
        self.stats.root_safety_screen_positions += work
        self.stats.generation_positions += work

    def _mark_root_child_proven_mate(
        self,
        key: tuple[int, str, int, int],
    ) -> None:
        if key not in self._root_child_proven_mate_keys:
            self._root_child_proven_mate_keys.add(key)
            self.stats.root_safety_proven_mate_children += 1

    def _mark_root_child_exact_exhausted(
        self,
        key: tuple[int, str, int, int],
    ) -> None:
        if key not in self._root_child_native_mate_exhausted_keys:
            self._root_child_native_mate_exhausted_keys.add(key)
            self.stats.root_safety_exact_exhausted_children += 1

    def _persistent_mate_proof(
        self,
        state: ProgressiveState,
    ) -> tuple[str, SeriesResult | None] | None:
        """Consult an injected cross-search cache before any proof solver."""

        cache = self.mate_proof_cache
        if cache is None:
            return None
        try:
            hit = cache.lookup(state)
        except Exception:
            self.stats.mate_proof_cache_errors += 1
            self.stats.mate_proof_cache_misses += 1
            return None
        if hit is None:
            self.stats.mate_proof_cache_misses += 1
            return None
        status = str(hit.status)
        proof_work = hit.proof_work
        if type(proof_work) is not int or proof_work < 0:
            self.stats.mate_proof_cache_errors += 1
            self.stats.mate_proof_cache_misses += 1
            return None
        series = hit.series
        if status == "found":
            if series is None:
                self.stats.mate_proof_cache_errors += 1
                self.stats.mate_proof_cache_misses += 1
                return None
            try:
                replayed = play_series(state, series.moves)
            except Exception:
                replayed = None
            if (
                replayed is None
                or replayed.outcome is not Outcome.CHECKMATE
                or not replayed.ended_by_check
            ):
                self.stats.mate_proof_cache_errors += 1
                self.stats.mate_proof_cache_misses += 1
                return None
            series = replayed
            self.stats.mate_proof_cache_found_hits += 1
        elif status == "exhausted":
            if series is not None:
                self.stats.mate_proof_cache_errors += 1
                self.stats.mate_proof_cache_misses += 1
                return None
            self.stats.mate_proof_cache_exhausted_hits += 1
        else:
            self.stats.mate_proof_cache_errors += 1
            self.stats.mate_proof_cache_misses += 1
            return None
        self.stats.mate_proof_cache_hits += 1
        self.stats.mate_proof_cache_work_saved += proof_work
        return status, series

    def _store_persistent_mate_proof(
        self,
        state: ProgressiveState,
        mate: SeriesResult | None,
        *,
        proof_work: int,
        exhausted: bool = False,
    ) -> None:
        cache = self.mate_proof_cache
        if cache is None:
            return
        try:
            if mate is not None:
                evictions = cache.store_found(
                    state,
                    mate,
                    proof_work=proof_work,
                )
            elif exhausted:
                evictions = cache.store_exhausted(
                    state,
                    proof_work=proof_work,
                )
            else:
                return
        except Exception:
            self.stats.mate_proof_cache_errors += 1
            return
        if type(evictions) is not int or evictions < 0:
            self.stats.mate_proof_cache_errors += 1
            return
        self.stats.mate_proof_cache_store_attempts += 1
        self.stats.mate_proof_cache_evictions += evictions

    def _root_child_promotion_mate(
        self,
        state: ProgressiveState,
    ) -> SeriesResult | None:
        """Runs the established promotion proof before broader reply search."""

        key = self._tt_key(state)
        if key in self._root_child_promotion_mate_cache:
            self.stats.root_safety_promotion_cache_hits += 1
            return self._root_child_promotion_mate_cache[key]
        if not promotion_mate_eligible(state):
            self._root_child_promotion_mate_cache[key] = None
            return None

        remaining = self._root_child_mate_screen_remaining()
        if remaining <= 0:
            self.stats.root_safety_budget_interruptions += 1
            raise _WorkLimit
        lane_limit = min(
            MAX_PROMOTION_MATE_POSITIONS,
            max(1, remaining // 5),
        )
        probe = find_promotion_series_mate(
            state,
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
        self._root_child_mate_screen_work += probe.positions_visited
        self.stats.root_safety_screen_positions += probe.positions_visited
        if probe.cancelled:
            raise _Timeout
        # A selective miss is not a no-mate proof, but it is deterministic and
        # need not be repeated for the same cached child boundary.
        self._root_child_promotion_mate_cache[key] = probe.series
        return probe.series

    def _root_child_exact_native_mate(
        self,
        state: ProgressiveState,
    ) -> tuple[bool, SeriesResult | None, bool]:
        """Returns ``(complete, mate, unknown)`` from the exact native lane.

        Import stays local so evaluation, training, league, and full-game
        consumers do not load the separate extension. Only replay-validated
        Found and exhaustive negative results settle this safety question;
        every resource or compatibility status remains unknown.
        """

        remaining = self._root_child_mate_screen_remaining()
        if remaining <= 1:
            self.stats.root_safety_budget_interruptions += 1
            raise _WorkLimit
        fallback_reserve = max(
            1,
            remaining // ROOT_CHILD_NATIVE_MATE_FALLBACK_DENOMINATOR,
        )
        native_work_limit = remaining - fallback_reserve
        if state.series_number < ROOT_CHILD_NATIVE_MATE_MIN_SERIES:
            return False, None, True
        if state.series_number >= ROOT_CHILD_MATE_SCREEN_MIN_SERIES:
            native_work_limit = min(
                native_work_limit,
                ROOT_CHILD_NATIVE_MATE_LATE_SERIES_WORK_LIMIT,
            )
        if native_work_limit < 1:
            self.stats.root_safety_budget_interruptions += 1
            raise _WorkLimit

        self._check_deadline()
        remaining_seconds = (
            max(0.0, self._deadline - time.perf_counter())
            if self._deadline is not None
            else None
        )
        # Deliberately lazy: see the non-import contract in the docstring.
        from .series_mate import SeriesMateStatus, find_native_series_mate

        self.stats.native_series_mate_calls += 1
        probe = find_native_series_mate(
            state,
            max_positions=None,
            max_work=native_work_limit,
            time_limit_seconds=remaining_seconds,
        )
        self._record_native_series_mate_probe(probe)
        if probe.status is SeriesMateStatus.FOUND:
            if probe.series is None:  # pragma: no cover - adapter invariant
                raise RuntimeError("native mate status carried no replayed line")
            self.stats.native_series_mate_found += 1
            return True, probe.series, False
        if probe.status is SeriesMateStatus.EXHAUSTED:
            self.stats.native_series_mate_exhausted += 1
            return True, None, False
        if probe.status is SeriesMateStatus.DEADLINE:
            self.stats.native_series_mate_deadline_hits += 1
            raise _Timeout
        if probe.status is SeriesMateStatus.WORK_LIMIT:
            self.stats.native_series_mate_work_limit_hits += 1
        else:
            self.stats.native_series_mate_unsupported += 1
        return False, None, True

    def _root_child_safety_screen_required(self) -> bool:
        """Whether a capped root needs a separate one-series reply proof."""

        cap = self.limits.max_series_per_node
        return cap is not None and cap < ROOT_CHILD_MATE_SCREEN_FRONTIER

    def _root_child_immediate_mate(
        self,
        state: ProgressiveState,
    ) -> SeriesResult | None:
        """Returns a replay-proven reply mate or certifies an exact miss.

        An injected identity-bound proof cache is consulted before any solver.
        On a miss, the specialized promotion lane still runs before every
        broader proof search. Its replayed hit is authoritative, but its
        selective miss is not a no-mate proof. A cheap width-32 screen follows,
        then the complete native one-series solver. Native ``EXHAUSTED`` is the
        only exact no-mate result and is cached by child transposition key.
        ``WORK_LIMIT``, ``DEADLINE``, ``UNSUPPORTED``, a missing kernel, or an
        incomplete selective screen remain unknown and fail closed instead of
        certifying the current root depth. The adaptive legacy screen may still
        recover a concrete replayed mate after an unknown native result, but
        never turns a miss into an exact proof.

        Every expanded position and generated edge is charged to the search's
        deterministic shared safety budget. Series 5-6 retain width 4096 for
        the legacy recovery screen; later tactical replies retain width 832.
        """

        if not self._root_child_safety_screen_required():
            # A root already running the established one-series verifier width
            # is itself the safety screen. Recursively screening the selected
            # reply asks a different two-series question and can consume the
            # verifier's fixed evidence budget before depth one completes.
            return None

        cache_key = self._tt_key(state)
        position_key = state.transposition_key
        if cache_key in self._root_child_mate_screen_cache:
            self.stats.root_safety_screen_cache_hits += 1
            if cache_key in self._root_child_native_mate_cache_keys:
                self.stats.native_series_mate_cache_hits += 1
            return self._root_child_mate_screen_cache[cache_key]
        self.stats.root_safety_screen_calls += 1

        persistent = self._persistent_mate_proof(state)
        if persistent is not None:
            status, mate = persistent
            self._root_child_mate_screen_cache[cache_key] = mate
            if status == "found":
                assert mate is not None
                self._mark_root_child_proven_mate(position_key)
            else:
                self._mark_root_child_exact_exhausted(position_key)
            return mate

        proof_work_start = self.stats.work_positions

        promotion_mate = self._root_child_promotion_mate(state)
        if promotion_mate is not None:
            self._root_child_mate_screen_cache[cache_key] = promotion_mate
            self._mark_root_child_proven_mate(position_key)
            self._store_persistent_mate_proof(
                state,
                promotion_mate,
                proof_work=self.stats.work_positions - proof_work_start,
            )
            return promotion_mate

        mate, completed = self._root_child_mate_screen_stage(
            state,
            frontier=ROOT_CHILD_MATE_SCREEN_CHEAP_FRONTIER,
            tactical_protection=True,
        )
        if mate is not None:
            self._root_child_mate_screen_cache[cache_key] = mate
            self._mark_root_child_proven_mate(position_key)
            self._store_persistent_mate_proof(
                state,
                mate,
                proof_work=self.stats.work_positions - proof_work_start,
            )
            return mate
        if not completed and self._root_child_mate_screen_remaining() <= 0:
            self.stats.root_safety_budget_interruptions += 1
            raise _WorkLimit

        native_complete, mate, native_unknown = (
            self._root_child_exact_native_mate(state)
        )
        if not native_complete and not native_unknown:
            raise RuntimeError(
                "incomplete exact mate lane must be classified as unknown"
            )
        if native_complete:
            self._root_child_mate_screen_cache[cache_key] = mate
            self._root_child_native_mate_cache_keys.add(cache_key)
            if mate is None:
                self._mark_root_child_exact_exhausted(position_key)
            else:
                self._mark_root_child_proven_mate(position_key)
            self._store_persistent_mate_proof(
                state,
                mate,
                proof_work=self.stats.work_positions - proof_work_start,
                exhausted=mate is None,
            )
            return mate
        if (
            ROOT_CHILD_ADAPTIVE_MATE_SCREEN_MIN_SERIES
            <= state.series_number
            <= ROOT_CHILD_ADAPTIVE_MATE_SCREEN_MAX_SERIES
        ):
            frontier = ROOT_CHILD_EARLY_MATE_SCREEN_FRONTIER
        elif _tactical_frontier_protection_eligible(state):
            frontier = ROOT_CHILD_MATE_SCREEN_FRONTIER
        else:
            if native_unknown or not completed:
                self.stats.root_safety_unknown_interruptions += 1
                raise _WorkLimit
            self._root_child_mate_screen_cache[cache_key] = None
            return None
        mate, wide_completed = self._root_child_mate_screen_stage(
            state,
            frontier=frontier,
            tactical_protection=False,
        )
        if mate is None and (native_unknown or not wide_completed):
            if self._root_child_mate_screen_remaining() <= 0:
                self.stats.root_safety_budget_interruptions += 1
            else:
                self.stats.root_safety_unknown_interruptions += 1
            raise _WorkLimit
        self._root_child_mate_screen_cache[cache_key] = mate
        if mate is not None:
            self._mark_root_child_proven_mate(position_key)
            self._store_persistent_mate_proof(
                state,
                mate,
                proof_work=self.stats.work_positions - proof_work_start,
            )
        return mate

    def _root_child_mate_screen_stage(
        self,
        state: ProgressiveState,
        *,
        frontier: int,
        tactical_protection: bool,
    ) -> tuple[SeriesResult | None, bool]:
        """Runs one native screen stage; the boolean means the batch completed."""

        self.stats.root_safety_screen_stages += 1

        remaining = self._root_child_mate_screen_remaining()
        if remaining <= 0:
            return None, False

        generation = GenerationStats()
        frontier_score = NativeFrontierScoreConfig.from_profile(
            state,
            self.profile,
            tactical_protection=tactical_protection,
        )
        final_score = NativeFinalSeriesScoreConfig.from_profile(
            self.profile,
            max_returned_series=frontier,
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
        candidates: list[SeriesResult | _NativeSeriesReference] = []
        try:
            batch = _native_complete_series_batch(
                state,
                generation,
                required_prefix=(),
                max_frontier_states=frontier,
                max_positions=remaining,
                frontier_score=frontier_score,
                native_final_score=final_score,
                should_stop=should_stop,
                native_time_budget_ns=native_time_budget_ns,
                native_threads=self.limits.native_threads,
            )
            if batch is not None:
                candidates = batch.references()
            else:
                from . import evaluation as evaluation_module

                legacy_native = evaluation_module._native_eval
                if (
                    legacy_native is None
                    or hasattr(legacy_native, "complete_series_candidate")
                    or not hasattr(legacy_native, "generate_complete_series")
                    or should_stop is not None
                ):
                    return None, False
                # The safety contract cannot disappear merely because the
                # lazy N2 batch ABI is unavailable in an untimed analysis.
                # Invoke only the older source-matched native surface here;
                # never let this 4096-wide screen spill into Python or ignore
                # a real deadline that the older ABI cannot enforce.
                legacy_results = _native_complete_series_generation(
                    state,
                    generation,
                    required_prefix=(),
                    max_frontier_states=frontier,
                    max_positions=remaining,
                    frontier_score=frontier_score,
                    native_final_score=final_score,
                    should_stop=None,
                )
                if legacy_results is None:
                    return None, False
                candidates = legacy_results
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
            self.stats.root_safety_screen_positions += work
            self._record_generation_stats(generation)

        if cancelled:
            self._check_deadline()
            raise _Timeout
        if exhausted:
            return None, False

        mate: SeriesResult | None = None
        for candidate in candidates:
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
        return mate, True

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
            tactically_selected: set[str] = set()
            # A delivered terminal mate is never traded away for a quota. In
            # ordinary positions, reserve half the cap for the static leaders
            # before tactical representatives compete for the other half.
            for item in ordered:
                if (
                    item.outcome == Outcome.CHECKMATE
                    and item.ended_by_check
                ):
                    selected_notation.add(item.machine_notation)
                    if item.machine_notation in tactical_candidates:
                        tactically_selected.add(item.machine_notation)
                    if len(selected_notation) == cap:
                        break
            ordinary_quota = min(
                cap,
                max(
                    1,
                    (
                        cap
                        + TACTICAL_FINAL_ORDINARY_QUOTA_DENOMINATOR
                        - 1
                    )
                    // TACTICAL_FINAL_ORDINARY_QUOTA_DENOMINATOR,
                ),
            )
            if len(selected_notation) < cap:
                for item in ordered[:ordinary_quota]:
                    selected_notation.add(item.machine_notation)
                    if len(selected_notation) == cap:
                        break
            for _opportunity, (_rank, item) in sorted(
                representatives.items(),
                key=lambda entry: (
                    entry[0][0],
                    entry[1][0],
                    entry[0][1],
                    entry[1][1].machine_notation,
                ),
            ):
                if len(selected_notation) == cap:
                    break
                selected_notation.add(item.machine_notation)
                tactically_selected.add(item.machine_notation)
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
        cache_key = (self._tt_key(state), required_prefix)
        if cache_key in self._root_promotion_mate_cache:
            promotion_mate = self._root_promotion_mate_cache[cache_key]
            if promotion_mate is None:
                return series
            return _GeneratedSeriesList(
                [promotion_mate],
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
            self._root_promotion_mate_cache[cache_key] = None
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
                self._root_promotion_mate_cache[cache_key] = None
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
            state.search_key,
            state.board.halfmove_clock,
            state.board.fullmove_number,
            state.board.promoted,
            state.board.chess960,
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
        self.stats.series_generation_cache_entries_peak = max(
            self.stats.series_generation_cache_entries_peak,
            len(self._series_generation_cache),
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

    @staticmethod
    def _tt_key(state: ProgressiveState) -> _TTKey:
        return (
            state.search_key,
            state.board.halfmove_clock,
            state.board.fullmove_number,
            state.board.promoted,
            state.board.chess960,
        )

    def _begin_tt_transaction(
        self,
    ) -> list[tuple[_TTKey, _TTEntry | None]]:
        journal: list[tuple[_TTKey, _TTEntry | None]] = []
        self._tt_transaction_stack.append(journal)
        if self._native_subtree_session is not None:
            self._native_subtree_session.begin_transaction()
        return journal

    def _write_tt(
        self,
        key: _TTKey,
        entry: _TTEntry,
    ) -> None:
        if self._tt_transaction_stack:
            # None is an unambiguous missing sentinel: TT values are always
            # concrete frozen _TTEntry instances.
            self._tt_transaction_stack[-1].append((key, self._tt.get(key)))
        self._tt[key] = entry

    def _rollback_tt_transaction(
        self,
        journal: list[tuple[_TTKey, _TTEntry | None]],
    ) -> int:
        if (
            not self._tt_transaction_stack
            or self._tt_transaction_stack[-1] is not journal
        ):
            raise RuntimeError("TT transactions must roll back in nested LIFO order")
        self._tt_transaction_stack.pop()
        native_writes = (
            self._native_subtree_session.rollback_transaction()
            if self._native_subtree_session is not None
            else 0
        )
        for key, previous in reversed(journal):
            if previous is None:
                self._tt.pop(key, None)
            else:
                self._tt[key] = previous
        return len(journal) + native_writes

    def _search_child_with_pvs(
        self,
        state: ProgressiveState,
        depth: int,
        alpha: int,
        beta: int,
        ply_from_root: int,
        *,
        parent_mover: chess.Color,
        has_prior_child: bool,
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[int, int]]:
        """Search a later child with a transactional one-point PV window.

        Scores are integral, so a one-point window cannot contain an unknown
        exact score.  A result inside the caller's full window is re-searched
        exactly.  The speculative probe never leaves TT state behind: that is
        required for canonical PV, alternative, and proof parity with ordinary
        alpha-beta, including when the probe is interrupted.
        """

        if (
            not has_prior_child
            # A depth-one child has already paid for its complete-series
            # frontier and searches only static leaves. Probing it twice adds
            # work on sparse positions without reducing generation.
            or depth < 2
            or beta - alpha <= 1
        ):
            return self._minimax(
                state,
                depth,
                alpha,
                beta,
                ply_from_root,
            )

        self.stats.pvs_zero_window_searches += 1
        journal = self._begin_tt_transaction()
        try:
            if parent_mover == chess.WHITE:
                score, pv, proof_bounds = self._minimax(
                    state,
                    depth,
                    alpha,
                    alpha + 1,
                    ply_from_root,
                )
            else:
                score, pv, proof_bounds = self._minimax(
                    state,
                    depth,
                    beta - 1,
                    beta,
                    ply_from_root,
                )
            needs_research = alpha < score < beta
        finally:
            self.stats.pvs_tt_writes_rolled_back += self._rollback_tt_transaction(
                journal
            )

        if needs_research:
            self.stats.pvs_researches += 1
            return self._minimax(
                state,
                depth,
                alpha,
                beta,
                ply_from_root,
            )
        return score, pv, proof_bounds

    def _search_root_child_with_pvs(
        self,
        state: ProgressiveState,
        depth: int,
        alpha: int,
        beta: int,
        ply_from_root: int,
        *,
        parent_mover: chess.Color,
        has_prior_child: bool,
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[int, int]]:
        """Rejects a later root candidate with a transactional one-point probe."""

        if (
            not ROOT_PVS_ENABLED
            or not has_prior_child
            or depth < 2
            or beta - alpha <= 1
        ):
            return self._minimax(
                state,
                depth,
                alpha,
                beta,
                ply_from_root,
            )

        self.stats.root_pvs_zero_window_searches += 1
        journal = self._begin_tt_transaction()
        try:
            if parent_mover == chess.WHITE:
                score, pv, proof_bounds = self._minimax(
                    state,
                    depth,
                    alpha,
                    alpha + 1,
                    ply_from_root,
                )
            else:
                score, pv, proof_bounds = self._minimax(
                    state,
                    depth,
                    beta - 1,
                    beta,
                    ply_from_root,
                )
            needs_research = alpha < score < beta
        finally:
            self.stats.root_pvs_tt_writes_rolled_back += (
                self._rollback_tt_transaction(journal)
            )

        if needs_research:
            self.stats.root_pvs_researches += 1
            return self._minimax(
                state,
                depth,
                alpha,
                beta,
                ply_from_root,
            )
        return score, pv, proof_bounds

    def _root_pvs_eligible(
        self,
        state: ProgressiveState,
        depth: int,
    ) -> bool:
        """Limits root scouts to the final best-move-only iteration."""

        return (
            ROOT_PVS_ENABLED
            and not self.limits.collect_all_root_scores
            and depth == self.limits.depth_series
        )

    def _tactical_leaf_extension(
        self,
        state: ProgressiveState,
        alpha: int,
        beta: int,
        ply_from_root: int,
        static_score: int,
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[int, int]]:
        """Searches one complete series at an unstable nominal leaf.

        The extension deliberately has no stand-pat and does not write a TT
        entry or claim proof. Its children are evaluated statically, which is
        the one-token bound that prevents recursive quiescence explosions.
        """

        self.stats.tactical_leaf_extensions += 1
        mover = state.board.turn
        series = self._ordered_generated(
            state,
            ply_from_root=ply_from_root + 1,
        )
        if not series:
            return static_score, (), UNKNOWN_PROOF_BOUNDS

        best_score = -MATE_SCORE * 2 if mover == chess.WHITE else MATE_SCORE * 2
        for result in series:
            self._check_deadline()
            self.stats.nodes += 1
            terminal = self._terminal_score(result, mover, ply_from_root + 1)
            if terminal is not None:
                score = terminal
            else:
                adjudication = self._quiet_adjudication(result.final_state)
                if adjudication == "proven-draw-no-mating-material":
                    score = 0
                elif adjudication == "manual-proof-required":
                    raise _AdjudicationPending
                else:
                    score = self._evaluate(result.final_state).total
            if (
                mover == chess.WHITE
                and score > best_score
                or mover == chess.BLACK
                and score < best_score
            ):
                best_score = score

            if mover == chess.WHITE:
                alpha = max(alpha, best_score)
            else:
                beta = min(beta, best_score)
            if alpha >= beta:
                self.stats.alpha_beta_cutoffs += 1
                break

        return best_score, (), UNKNOWN_PROOF_BOUNDS

    def _minimax(
        self,
        state: ProgressiveState,
        depth: int,
        alpha: int,
        beta: int,
        ply_from_root: int,
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[int, int]]:
        if self._native_subtree_session is not None:
            return self._native_minimax(
                state,
                depth,
                alpha,
                beta,
                ply_from_root,
            )
        self._check_deadline()
        self.stats.nodes += 1
        adjudication = self._quiet_adjudication(state)
        if adjudication == "proven-draw-no-mating-material":
            return 0, (), (0, 0)
        if adjudication == "manual-proof-required":
            raise _AdjudicationPending
        if depth == 0:
            leaf = self._evaluate(state)
            if leaf.tactical_unstable:
                return self._tactical_leaf_extension(
                    state,
                    alpha,
                    beta,
                    ply_from_root,
                    leaf.total,
                )
            return leaf.total, (), UNKNOWN_PROOF_BOUNDS

        key = self._tt_key(state)
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
                    score, child_pv, proof_bounds = self._search_child_with_pvs(
                        result.final_state,
                        depth - 1,
                        alpha,
                        beta,
                        ply_from_root + 1,
                        parent_mover=mover,
                        has_prior_child=best_candidate is not None,
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
            self._write_tt(key, replacement)
        return best_score, best_pv, proof_bounds

    def _search_root_pass(
        self,
        state: ProgressiveState,
        depth: int,
        required_prefix: tuple[str, ...],
        root_mate_overrides: Mapping[_TTKey, SeriesResult],
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
        external_cache_was_present: bool | None = None
        if self._native_subtree_session is not None:
            external_cache_was_present = (
                self._native_subtree_session.external_cache_present()
            )
            if not external_cache_was_present:
                # The unified LRU evicted the mirrored root entry while native
                # descendants were searched. Drop only the Python materialized
                # copy without double-counting that already-recorded eviction.
                self._series_generation_cache.clear()
                self._series_generation_cache_weight = 0
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
        # Root policy and its cached frontier stay in Python. Start the native
        # descendant session only after that frontier exists, reserving its
        # exact cache weight from the shared public 16k-series envelope.
        if self._native_subtree_session is None:
            self._start_native_subtree(state)
        elif external_cache_was_present:
            self._native_subtree_session.touch_external_cache()
        else:
            self._native_subtree_session.insert_external_cache(
                self._series_generation_cache_weight
            )
        scored: list[ScoredSeries] = []
        root_alpha = -MATE_SCORE * 2
        root_beta = MATE_SCORE * 2
        has_non_adverse_exact = False
        for result in series:
            try:
                self._check_deadline()
                terminal = self._terminal_score(result, mover, 1)
                if terminal is None:
                    child_state = result.final_state
                    reply_override = root_mate_overrides.get(
                        self._tt_key(child_state)
                    )
                    if reply_override is not None:
                        score = self._terminal_score(
                            reply_override,
                            child_state.board.turn,
                            2,
                        )
                        if score is None:  # pragma: no cover
                            raise RuntimeError(
                                "replay-proven mate override has no terminal score"
                            )
                        scored_candidate = ScoredSeries(
                            self._materialize_series(result),
                            score,
                            (reply_override,),
                            self._terminal_proof_bounds(
                                reply_override,
                                child_state.board.turn,
                            ),
                        )
                        scored.append(scored_candidate)
                        if not _root_candidate_is_proven_adverse(
                            mover, scored_candidate
                        ):
                            has_non_adverse_exact = True
                            if mover == chess.WHITE:
                                root_alpha = max(root_alpha, score)
                            else:
                                root_beta = min(root_beta, score)
                        continue
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
                    if not self._root_pvs_eligible(state, depth):
                        score, child_pv, proof_bounds = self._minimax(
                            child_state,
                            depth - 1,
                            child_alpha,
                            child_beta,
                            1,
                        )
                    else:
                        score, child_pv, proof_bounds = (
                            self._search_root_child_with_pvs(
                                child_state,
                                depth - 1,
                                child_alpha,
                                child_beta,
                                1,
                                parent_mover=mover,
                                has_prior_child=has_non_adverse_exact,
                            )
                        )
                    score_is_exact = (
                        self.limits.collect_all_root_scores
                        or child_alpha < score < child_beta
                    )
                    if not score_is_exact and scored:
                        exact_best = _proof_safe_root_order(mover, scored)[0]
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
                scored_candidate = ScoredSeries(
                    self._materialize_series(result),
                    score,
                    child_pv,
                    proof_bounds,
                )
                scored.append(scored_candidate)
                if not _root_candidate_is_proven_adverse(mover, scored_candidate):
                    has_non_adverse_exact = True
                    if mover == chess.WHITE:
                        root_alpha = max(root_alpha, score)
                    else:
                        root_beta = min(root_beta, score)
            else:
                self.stats.root_bound_candidates += 1
            if not self.limits.continue_after_root_mate and (
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
        scored = list(_proof_safe_root_order(mover, scored))
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

    def _root_safety_fallback(
        self,
        *candidates: SeriesResult | None,
    ) -> SeriesResult | None:
        """Prefers exact-safe children and never returns a proven mate child."""

        unique: list[SeriesResult] = []
        seen: set[tuple[int, str, int, int]] = set()
        for candidate in candidates:
            if candidate is None or candidate.outcome is not None:
                continue
            key = candidate.final_state.transposition_key
            if key in seen or key in self._root_child_proven_mate_keys:
                continue
            seen.add(key)
            unique.append(candidate)
        for candidate in unique:
            if (
                candidate.final_state.transposition_key
                in self._root_child_native_mate_exhausted_keys
            ):
                self.stats.root_safety_exhausted_fallbacks += 1
                return candidate
        if unique:
            self.stats.root_safety_unknown_fallbacks += 1
            return unique[0]
        return None

    def _root_all_mating_widening(
        self,
        state: ProgressiveState,
        depth: int,
        required_prefix: tuple[str, ...],
        retained: tuple[ScoredSeries, ...],
    ) -> tuple[
        int,
        tuple[SeriesResult, ...],
        tuple[ScoredSeries, ...],
        str | None,
    ]:
        """Widens only a root frontier whose every retained child is mating.

        A capped selector cannot turn ``FOUND`` for every retained child into a
        game-theoretic loss. Generate one larger root-only frontier, with the
        same explicit native frontier and final-return cap, and first look for
        a decisive current-series terminal. If none exists, screen previously
        unseen children. An exact-EXHAUSTED child may replace the known losses;
        an unresolved child aborts the depth as UNKNOWN. Descendants retain the
        configured ordinary width.
        """

        self.stats.root_safety_all_mating_widenings += 1
        self._selective = True
        work_before = self.stats.generation_positions
        try:
            generated, width_complete = self._generate(
                state,
                ply_from_root=1,
                required_prefix=required_prefix,
                tactical_protection=True,
                max_frontier_states=ROOT_ALL_MATING_WIDEN_FRONTIER,
            )
        finally:
            self.stats.root_safety_widening_positions += (
                self.stats.generation_positions - work_before
            )
        candidates = (
            generated.references()
            if isinstance(generated, _NativeSeriesBatch)
            else generated
        )
        self.stats.root_safety_widened_candidates += len(candidates)

        mover = state.board.turn
        for candidate in candidates:
            terminal = self._terminal_score(candidate, mover, 1)
            if terminal not in (MATE_SCORE - 1, -MATE_SCORE + 1):
                continue
            if (
                mover == chess.WHITE
                and terminal != MATE_SCORE - 1
                or mover == chess.BLACK
                and terminal != -MATE_SCORE + 1
            ):
                continue
            materialized = self._materialize_series(candidate)
            proof_bounds = self._terminal_proof_bounds(materialized, mover)
            scored = ScoredSeries(materialized, terminal, (), proof_bounds)
            self.stats.root_safety_widened_terminal_mates += 1
            self._root_widened_terminal_series = materialized
            self._root_scores_complete = False
            return (
                terminal,
                (materialized,),
                (scored,),
                _proof_from_bounds(proof_bounds),
            )

        retained_keys = {
            item.series.final_state.transposition_key for item in retained
        }
        unknown_fallback: SeriesResult | None = None
        exact_unscored_fallback: SeriesResult | None = None
        pending_interruption: _Timeout | _WorkLimit | None = None
        safe_scored: list[ScoredSeries] = []
        adverse_scored_by_child = {
            item.series.final_state.transposition_key: item
            for item in retained
            if _root_candidate_is_proven_adverse(mover, item)
        }
        for candidate in candidates:
            try:
                self._check_deadline()
            except _Timeout as error:
                pending_interruption = error
                break
            if candidate.outcome is not None:
                # A terminal draw is authoritative safety, but it must still
                # compete with every later fully screened widened child. A
                # stronger win may appear after it in deterministic order.
                if candidate.outcome in {Outcome.STALEMATE, Outcome.TEN_SERIES_DRAW}:
                    materialized = self._materialize_series(candidate)
                    safe_scored.append(ScoredSeries(materialized, 0))
                    self.stats.root_safety_widened_exact_children += 1
                continue
            child_key = candidate.final_state.transposition_key
            if child_key in retained_keys:
                continue
            materialized = self._materialize_series(candidate)
            try:
                reply_mate = self._root_child_immediate_mate(
                    materialized.final_state
                )
            except _Timeout as error:
                pending_interruption = error
                if unknown_fallback is None:
                    unknown_fallback = materialized
                break
            except _WorkLimit as error:
                pending_interruption = pending_interruption or error
                if unknown_fallback is None:
                    unknown_fallback = materialized
                if self._root_child_mate_screen_remaining() <= 0:
                    break
                continue
            if reply_mate is not None:
                score = self._terminal_score(
                    reply_mate,
                    materialized.final_state.board.turn,
                    2,
                )
                if score is None:  # pragma: no cover - replay invariant
                    raise RuntimeError(
                        "replay-proven root reply mate has no terminal score"
                    )
                adverse_scored_by_child[child_key] = ScoredSeries(
                    materialized,
                    score,
                    (reply_mate,),
                    self._terminal_proof_bounds(
                        reply_mate,
                        materialized.final_state.board.turn,
                    ),
                )
                continue
            if child_key not in self._root_child_native_mate_exhausted_keys:
                if unknown_fallback is None:
                    unknown_fallback = materialized
                continue

            try:
                score, child_pv, proof_bounds = self._minimax(
                    materialized.final_state,
                    depth - 1,
                    -MATE_SCORE * 2,
                    MATE_SCORE * 2,
                    1,
                )
            except (_Timeout, _WorkLimit) as error:
                pending_interruption = pending_interruption or error
                if exact_unscored_fallback is None:
                    exact_unscored_fallback = materialized
                break
            safe_scored.append(
                ScoredSeries(
                    materialized,
                    score,
                    child_pv,
                    proof_bounds,
                )
            )
            self.stats.root_safety_widened_exact_children += 1

        safe_scored.sort(
            key=lambda item: (
                -item.score if mover == chess.WHITE else item.score,
                item.series.machine_notation,
            )
        )
        if safe_scored:
            best = safe_scored[0]
            self._root_scores_complete = False
            # An unknown sibling can still be stronger. Preserve the best
            # fully scored exact-safe move as the fallback choice, but abort
            # this depth so its score/proof/alternatives cannot be certified.
            if pending_interruption is not None or unknown_fallback is not None:
                error = pending_interruption or _WorkLimit()
                best_key = best.series.final_state.transposition_key
                if best_key in self._root_child_native_mate_exhausted_keys:
                    self.stats.root_safety_exhausted_fallbacks += 1
                elif best.series.outcome is not None:
                    self.stats.root_safety_terminal_fallbacks += 1
                raise _RootInterrupted((), error, best.series) from error
            return (
                best.score,
                (best.series,) + best.principal_variation,
                tuple(safe_scored),
                None,
            )

        # Every widened child is replay-proven to allow an immediate mate.
        # There is no safe move to preserve, so refusing to return a series
        # would turn a forced chess loss into a technical engine failure. Keep
        # the least-bad exact line. A complete widened frontier may expose its
        # proof; a capped frontier remains explicitly selective with UNKNOWN
        # bounds, but still keeps engine play live.
        candidate_child_keys = {
            candidate.final_state.transposition_key
            for candidate in candidates
            if candidate.outcome is None
        }
        if (
            pending_interruption is None
            and exact_unscored_fallback is None
            and unknown_fallback is None
            and candidate_child_keys
            and candidate_child_keys.issubset(adverse_scored_by_child)
        ):
            adverse_scored = list(
                _proof_safe_root_order(
                    mover,
                    tuple(adverse_scored_by_child.values()),
                )
            )
            best = adverse_scored[0]
            if required_prefix:
                # A constrained analysis describes whether the already-chosen
                # prefix remains acceptable; it is not an engine-play
                # liveness request. Preserve the certified safety contract by
                # rejecting a prefix whose every legal completion has a
                # replay-proven mate reply, but do not mislabel that semantic
                # rejection as work exhaustion.
                self._root_scores_complete = False
                return self._evaluate(state).total, (), (), None
            self._root_scores_complete = True
            proof_bounds = self._combine_proof_bounds(
                mover,
                [item.proof_bounds for item in adverse_scored],
                all_branches_visited=width_complete,
            )
            return (
                best.score,
                (best.series,) + best.principal_variation,
                tuple(adverse_scored),
                _proof_from_bounds(proof_bounds),
            )

        error = pending_interruption or _WorkLimit()
        if exact_unscored_fallback is not None:
            self.stats.root_safety_exhausted_fallbacks += 1
            raise _RootInterrupted((), error, exact_unscored_fallback) from error
        if unknown_fallback is not None:
            self.stats.root_safety_unknown_fallbacks += 1
            raise _RootInterrupted((), error, unknown_fallback) from error
        raise error

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
        """Screens only provisional root winners, then deterministically retries.

        A wide immediate-reply probe is safety verification, not move ordering.
        Running it on every successive alpha contender made hosted depth two
        spend most of its budget on moves that could never become the final
        choice.  Each pass now completes ordinary root selection first.  An
        unsafe provisional winner is assigned its authoritative replay-mate
        override by child position, and the cached root/minimax pass is
        repeated with a reset root window until a screened winner survives.
        """

        if not self._root_child_safety_screen_required():
            return self._search_root_pass(
                state,
                depth,
                required_prefix,
                {},
            )

        widened_terminal = self._root_widened_terminal_series
        if widened_terminal is not None:
            mover = state.board.turn
            score = self._terminal_score(widened_terminal, mover, 1)
            if score is None:  # pragma: no cover - cache invariant
                raise RuntimeError("cached widened root terminal is nonterminal")
            proof_bounds = self._terminal_proof_bounds(widened_terminal, mover)
            scored = ScoredSeries(
                widened_terminal,
                score,
                (),
                proof_bounds,
            )
            self._root_scores_complete = False
            return (
                score,
                (widened_terminal,),
                (scored,),
                _proof_from_bounds(proof_bounds),
            )

        overrides: dict[_TTKey, SeriesResult] = {}
        last_exact_exhausted: SeriesResult | None = None
        last_unknown: SeriesResult | None = None

        while True:
            try:
                self.stats.root_safety_passes += 1
                score, pv, alternatives, proof = self._search_root_pass(
                    state,
                    depth,
                    required_prefix,
                    overrides,
                )
            except _RootInterrupted as interrupted:
                # A retry has reset alpha/beta around new authoritative evidence.
                # Partial scores from any pass cannot certify the iteration, so
                # expose only an exact-EXHAUSTED or explicitly UNKNOWN child as
                # a move fallback. A replay-proven mate child is never eligible.
                fallback = self._root_safety_fallback(
                    last_exact_exhausted,
                    last_unknown,
                    interrupted.fallback,
                )
                if fallback is None:
                    raise interrupted.cause from interrupted
                raise _RootInterrupted((), interrupted.cause, fallback) from interrupted
            except (_Timeout, _WorkLimit) as error:
                # Root generation can be interrupted before _search_root_pass has
                # materialized a frontier and wrapped the cancellation. A retry
                # may still have a previously screened exact-EXHAUSTED or UNKNOWN
                # child; keep only that move, never its now-discarded score,
                # alternatives, or proof. A first-pass raw cancellation with no
                # eligible child retains the historical no-move result in run().
                fallback = self._root_safety_fallback(
                    last_exact_exhausted,
                    last_unknown,
                )
                if fallback is None:
                    raise
                raise _RootInterrupted((), error, fallback) from error
            if not pv:
                return score, pv, alternatives, proof

            provisional = pv[0]
            if provisional.outcome is not None:
                return score, pv, alternatives, proof
            child_state = provisional.final_state
            child_key = child_state.transposition_key
            override_key = self._tt_key(child_state)
            if child_key in self._root_child_native_mate_exhausted_keys:
                last_exact_exhausted = provisional
            elif child_key not in self._root_child_proven_mate_keys:
                last_unknown = provisional
            if override_key in overrides:
                # The best root choice still loses after every root bound was
                # repaired around its authoritative reply mate. If every
                # retained choice has the same replay-proven defect, widen the
                # selector before describing any one retained line as a loss.
                if alternatives and all(
                    item.series.outcome is None
                    and item.series.final_state.transposition_key
                    in self._root_child_proven_mate_keys
                    for item in alternatives
                ):
                    if state.series_number <= ROOT_ALL_MATING_WIDEN_MAX_SERIES:
                        return self._root_all_mating_widening(
                            state,
                            depth,
                            required_prefix,
                            alternatives,
                        )
                    # At later series, an 832-wide root itself can exceed the
                    # hosted 10M ceiling. Preserve the replay-truth for the
                    # selected retained line, but never promote the capped set
                    # to a game-wide forced-loss proof.
                    self._selective = True
                    return score, pv, alternatives, None
                return score, pv, alternatives, proof
            reply_mate = (
                pv[1]
                if (
                    len(pv) > 1
                    and pv[1].outcome == Outcome.CHECKMATE
                    and pv[1].ended_by_check
                )
                else None
            )
            try:
                if reply_mate is None:
                    reply_mate = self._root_child_immediate_mate(child_state)
            except (_Timeout, _WorkLimit) as error:
                # No incomplete safety probe may certify an unscreened score.
                # The iterative-deepening caller will retain its last complete
                # depth; at depth zero this is explicitly a move-only fallback.
                fallback = self._root_safety_fallback(
                    last_exact_exhausted,
                    provisional,
                    last_unknown,
                )
                if fallback is None:
                    raise
                raise _RootInterrupted((), error, fallback) from error
            if reply_mate is None:
                if child_key in self._root_child_native_mate_exhausted_keys:
                    last_exact_exhausted = provisional
                return score, pv, alternatives, proof
            self._mark_root_child_proven_mate(child_key)
            if (
                last_unknown is not None
                and last_unknown.final_state.transposition_key == child_key
            ):
                last_unknown = None
            overrides[override_key] = reply_mate
            self.stats.root_safety_retries += 1

    def run(
        self,
        state: ProgressiveState,
        *,
        required_prefix: tuple[str, ...] = (),
    ) -> SearchResult:
        required_prefix = tuple(required_prefix)
        started = time.perf_counter()
        self._preferred_root_series = None
        self._root_widened_terminal_series = None
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
        completed_root_scores_complete = False
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
                        partial = _proof_safe_root_order(
                            mover,
                            interrupted.scored,
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
                else:
                    self._root_scores_complete = completed_root_scores_complete
                break
            except _Timeout:
                timed_out = True
                self._selective = True
                if completed_depth == 0:
                    self._root_scores_complete = False
                else:
                    self._root_scores_complete = completed_root_scores_complete
                break
            except _WorkLimit:
                work_limit_reached = True
                self._selective = True
                if completed_depth == 0:
                    self._root_scores_complete = False
                else:
                    self._root_scores_complete = completed_root_scores_complete
                break
            except _AdjudicationPending as pending:
                if completed_depth > 0:
                    # The deeper iteration is unknown, but the last fully
                    # completed iteration remains a legal search result. Keep
                    # its score, PV, alternatives, and proof while making the
                    # incomplete requested horizon explicit. This is not a
                    # quiet-draw adjudication and must never manufacture one.
                    self._selective = True
                    self._root_scores_complete = completed_root_scores_complete
                    adjudication = "manual-proof-required"
                    if (
                        not self.limits.collect_all_root_scores
                        and best_pv
                        and best_pv[0].outcome is None
                    ):
                        # Match the public shape that the retained depth would
                        # have had as a completed best-move-only result. Unlike
                        # a deadline/work interruption, quiet adjudication is a
                        # semantic stop rather than a diagnostic partial pass.
                        alternatives = ()
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
            completed_root_scores_complete = self._root_scores_complete
            self._preferred_root_series = (
                best_pv[0].machine_notation if best_pv else None
            )

        elapsed = time.perf_counter() - started
        work_limit_reached = (
            work_limit_reached
            or self._quiet_work_limit_reached
            or self._evaluation_work_limit_reached
        )
        if (
            not self.limits.collect_all_root_scores
            and completed_depth == self.limits.depth_series
            and best_pv
            and best_pv[0].outcome is None
        ):
            # A fully completed best-move-only pass may retain a few exact root
            # candidates for internal PVS and safety bookkeeping, but it has
            # not scored the whole root. Hide that partial subset. Interrupted
            # deeper iterations must still preserve the last completed depth's
            # receipt, and an authoritative terminal result remains publishable.
            alternatives = ()
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
    mate_proof_cache: MateProofCache | None = None,
) -> SearchResult:
    return SeriesSearcher(
        limits or SearchLimits(),
        profile,
        evaluation_overlay,
        mate_proof_cache=mate_proof_cache,
    ).run(
        state,
        required_prefix=required_prefix,
    )
