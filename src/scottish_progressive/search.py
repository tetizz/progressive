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
    NativeHorizonProof,
    NativeSubtreeBound,
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
    from .selected_pv_horizon import (
        CandidateHorizonState,
        SelectedPvHorizonCertification,
        SelectedPvHorizonProof,
    )
    from .series_mate import SeriesMateProbe, SeriesMateStatus
    from .single_reply_mate_ladder import SingleReplyMateLadderProbe


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
# Match the native retained-proof namespace bound. Long-lived searchers can
# reuse successful repairs without retaining an unbounded number of TT maps.
SELECTED_PV_PROOF_TT_NAMESPACE_CAPACITY = 256
# Best-only play may otherwise accept a root series whose opponent reply mate
# was discarded by the ordinary width-32 child beam. This second screen is
# deliberately much wider and globally bounded. It is invoked only after a
# fully scored root candidate can become the new choice, so early positions pay
# for successive contenders rather than a blanket wide/tactical search.
ROOT_CHILD_MATE_SCREEN_FRONTIER = 832
ROOT_CHILD_EARLY_MATE_SCREEN_FRONTIER = 4_096
ROOT_CHILD_MATE_SCREEN_CHEAP_FRONTIER = 32
ROOT_CHILD_MATE_SCREEN_POSITION_LIMIT = 3_000_000
ROOT_MATE_CLAIM_SAFETY_RESERVE_LIMIT = 20_000_000
# Starting at Series 5, a width-capped best-move search can hide even a cheap
# mate in the mover's current combinatorial series.  Probe that selective lane
# before ordinary search.  The isolated native solver is exact on FOUND and
# replay-validates its line; a bounded miss remains UNKNOWN and simply falls
# through.  Earlier, tractable series and sub-500k analysis jobs retain their
# established search path and exact work receipts.
ROOT_CURRENT_SERIES_MATE_WORK_LIMIT = 250_000
ROOT_CURRENT_SERIES_MATE_WORK_DENOMINATOR = 64
ROOT_CURRENT_SERIES_MATE_MIN_WORK = 1_000
ROOT_CURRENT_SERIES_MATE_MIN_TOTAL_WORK = 500_000
ROOT_CURRENT_SERIES_MATE_MIN_SERIES = 5
ROOT_CURRENT_SERIES_MATE_TIME_LIMIT_SECONDS = 1.0
ROOT_CURRENT_SERIES_MATE_TIME_DENOMINATOR = 10
# A depth-zero liveness fallback has not completed even one minimax iteration.
# Before that last-resort move crosses the public boundary, give its opponent a
# separate exact one-series mate query.  This lane is intentionally lazy and is
# charged to the global work ledger, but not to the ordinary shared 3M root
# screen: the ordinary screen may be the very work limit that produced the D0
# fallback.  Only FOUND and EXHAUSTED settle the question.
FINAL_FALLBACK_REPLY_MATE_WORK_LIMIT = 1_000_000
# Python-only research lane. The browser/WASM controller does not implement
# this contract yet, so no release may advertise it until parity is proved.
# Width 64 does not retain the recorded 3aaef safe witness. Width 512 retains
# that exact full-state child at rank 62 and matches the browser kernel's
# existing maximum retained-root width, minimizing the later parity change.
FINAL_FALLBACK_SAFE_RESELECTION_FRONTIER = 512
FINAL_FALLBACK_SAFE_RESELECTION_CHILD_WORK_LIMIT = 10_000_000
# Re-proving the ordinary W32 first at 10M each starves every newly widened
# escape. A 3M first-stage miss remains UNKNOWN and unpublishable; candidates
# beyond that old beam receive the full 10M exact-proof allowance. This staged
# allocation keeps the recorded rank-62 witness inside the 40M lane ceiling.
FINAL_FALLBACK_SAFE_RESELECTION_EARLY_FRONTIER = 32
FINAL_FALLBACK_SAFE_RESELECTION_EARLY_CHILD_WORK_LIMIT = 3_000_000
FINAL_FALLBACK_SAFE_RESELECTION_TOTAL_WORK_LIMIT = 40_000_000
# A selected root that survives the exact immediate-reply mate gate can still
# permit the narrow, fully forced A/check, B/only-countercheck, C/mate ladder.
# This is a proof lane, not another search width: each selected candidate gets
# at most one million combined native positions-plus-edges, charged to the
# caller's existing work ceiling and governed by the same absolute deadline.
SELECTED_ROOT_SINGLE_REPLY_LADDER_WORK_LIMIT = 1_000_000
# The observed A/B/C failure starts at the Series-7 child boundary. Earlier
# roots already cover three complete series cheaply enough through ordinary
# minimax, while dispatching an extra exact native theorem there changes the
# public opening work contract for no demonstrated gain.
SELECTED_ROOT_SINGLE_REPLY_LADDER_MIN_CHILD_SERIES = 7
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
    root_mate_claim_quarantines: int = 0
    root_mate_claim_all_quarantined: int = 0
    root_mate_claim_move_only_fallbacks: int = 0
    root_mate_claim_final_discards: int = 0
    root_mate_claim_prior_depth_discards: int = 0
    root_current_series_mate_probes: int = 0
    root_current_series_mate_found: int = 0
    root_current_series_mate_exhausted: int = 0
    root_current_series_mate_unknown: int = 0
    root_current_series_mate_work: int = 0
    final_fallback_reply_mate_probes: int = 0
    final_fallback_reply_mate_cache_hits: int = 0
    final_fallback_reply_mate_found: int = 0
    final_fallback_reply_mate_exhausted: int = 0
    final_fallback_reply_mate_unknown: int = 0
    final_fallback_reply_mate_work: int = 0
    final_fallback_reply_mate_rejections: int = 0
    final_fallback_safe_reselection_attempts: int = 0
    final_fallback_safe_reselection_candidates: int = 0
    final_fallback_safe_reselection_found: int = 0
    final_fallback_safe_reselection_exhausted: int = 0
    final_fallback_safe_reselection_unknown: int = 0
    final_fallback_safe_reselection_terminal: int = 0
    final_fallback_safe_reselection_rescues: int = 0
    final_fallback_safe_reselection_work: int = 0
    final_fallback_safe_reselection_budget_interruptions: int = 0
    selected_pv_horizon_probe_calls: int = 0
    selected_pv_horizon_found: int = 0
    selected_pv_horizon_exhausted: int = 0
    selected_pv_horizon_unknown: int = 0
    selected_pv_horizon_line_rejections: int = 0
    selected_pv_horizon_native_repairs: int = 0
    selected_pv_horizon_repair_interruptions: int = 0
    selected_pv_horizon_candidate_vetoes: int = 0
    selected_pv_horizon_all_vetoed_frontiers: int = 0
    selected_pv_horizon_widenings: int = 0
    selected_pv_horizon_widened_candidates: int = 0
    selected_pv_horizon_prior_depth_discards: int = 0
    selected_pv_horizon_move_only_fallbacks: int = 0
    selected_root_ladder_probe_calls: int = 0
    selected_root_ladder_cache_hits: int = 0
    selected_root_ladder_found: int = 0
    selected_root_ladder_exhausted: int = 0
    selected_root_ladder_unknown: int = 0
    selected_root_ladder_work: int = 0
    selected_root_ladder_candidate_vetoes: int = 0
    selected_root_ladder_final_rejections: int = 0
    selected_root_ladder_all_vetoed_fallbacks: int = 0
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
_SelectedPvProofSetIdentity = tuple[object, ...]


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


class _HorizonPolicyExhausted(Exception):
    pass


class _RootInterrupted(Exception):
    def __init__(
        self,
        scored: tuple[ScoredSeries, ...],
        cause: _Timeout | _WorkLimit | _HorizonPolicyExhausted,
        fallback: SeriesResult,
    ) -> None:
        super().__init__(type(cause).__name__)
        self.scored = scored
        self.cause = cause
        self.fallback = fallback


class _AdjudicationPending(Exception):
    pass


class _RootAdjudicationPending(_AdjudicationPending):
    def __init__(self, fallback: SeriesResult | None = None) -> None:
        super().__init__()
        self.fallback = fallback


class _RootMateClaimPending(Exception):
    """All retained roots made unproved mate claims at this depth."""

    def __init__(
        self,
        fallback: SeriesResult,
        cause: _Timeout | _WorkLimit | None = None,
    ) -> None:
        super().__init__(type(cause).__name__ if cause is not None else "")
        self.fallback = fallback
        self.cause = cause


@dataclass(frozen=True, slots=True)
class _FinalSafeReselection:
    """One provisional D0 rescue and any lane-wide hard stop."""

    series: SeriesResult | None = None
    score: int | None = None
    timed_out: bool = False
    work_limited: bool = False
    ladder_gate_applied: bool = False


class _RootLadderAllVetoed(Exception):
    """Every legal root in an exact frontier has the same proven ladder."""

    def __init__(self, fallback: SeriesResult) -> None:
        super().__init__(fallback.machine_notation)
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
        # The pure-Python fallback uses the same replayed leaf theorem as the
        # native retained-root namespace. Exact keys prevent a proof from
        # leaking across clock or promoted-piece provenance.
        self._selected_pv_leaf_mate_overrides: dict[
            _TTKey, SeriesResult
        ] = {}
        self._selected_pv_leaf_override_hits: set[_TTKey] = set()
        # Repaired scores are valid only under the exact retained proof set.
        # Keep those TT entries in their own deterministic namespaces, mirroring
        # the native retained-root contract without destroying the ordinary TT.
        self._selected_pv_proof_tt_namespaces: OrderedDict[
            _SelectedPvProofSetIdentity, dict[_TTKey, _TTEntry]
        ] = OrderedDict()
        # Native proof repair must bind the selected Python root to a retained
        # candidate manifest. Keep an equivalent isolated manifest cache on the
        # Python fallback so both engines report the same real generation work
        # and cache footprint without disturbing the ordinary root cache.
        self._selected_pv_python_root_manifests: OrderedDict[
            _TTKey, _SeriesCacheEntry
        ] = OrderedDict()
        self._selected_pv_python_root_manifest_weight = 0
        self._selected_pv_root_vetoes: set[str] = set()
        # The ladder question is deliberately independent from the immediate
        # one-series mate question. Exact full-state keys prevent clocks,
        # promoted provenance, Chess960 mode, or Progressive EP rights from
        # sharing a proof, and UNKNOWN is never retained here.
        self._selected_root_ladder_cache: dict[
            _TTKey, "SingleReplyMateLadderProbe"
        ] = {}
        self._selected_root_ladder_emergency_fallback: str | None = None
        self._root_mate_claim_quarantines: set[str] = set()
        self._root_mate_claim_emergency_fallback: SeriesResult | None = None

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
        external_work, remaining_nanoseconds = self._native_work_context()
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

    def _native_work_context(self) -> tuple[int, int | None]:
        """Returns the shared work/deadline view for one native session call."""

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
        return external_work, remaining_nanoseconds

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

    def _evaluate(
        self,
        state: ProgressiveState,
        *,
        max_additional_positions: int | None = None,
    ) -> EvaluationBreakdown:
        if (
            max_additional_positions is not None
            and (
                type(max_additional_positions) is not int
                or max_additional_positions < 0
            )
        ):
            raise ValueError(
                "additional evaluation position limit must be nonnegative"
            )
        evaluation_started_work = self.stats.work_positions
        key = state.search_key
        cached = self._eval_cache.get(key)
        if cached is None:
            if (
                (
                    max_additional_positions is not None
                    and max_additional_positions < 1
                )
                or (
                    self.limits.max_generation_positions is not None
                    and self.stats.generation_positions
                    >= self.limits.max_generation_positions
                )
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
            if max_additional_positions is not None:
                local_remaining = max(
                    0,
                    max_additional_positions
                    - (
                        self.stats.work_positions
                        - evaluation_started_work
                    ),
                )
                remaining = (
                    local_remaining
                    if remaining is None
                    else min(remaining, local_remaining)
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
                    if max_additional_positions is not None:
                        local_overlay_remaining = max(
                            0,
                            max_additional_positions
                            - (
                                self.stats.work_positions
                                - evaluation_started_work
                            ),
                        )
                        overlay_remaining = (
                            local_overlay_remaining
                            if overlay_remaining is None
                            else min(
                                overlay_remaining,
                                local_overlay_remaining,
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
        max_additional_positions: int | None = None,
        root_contract_s3_neural_ordering: bool = False,
    ) -> tuple[list[SeriesResult] | _NativeSeriesBatch, bool]:
        frontier_limit = (
            self.limits.max_series_per_node
            if max_frontier_states is None
            else max_frontier_states
        )
        generation = GenerationStats()
        if (
            max_additional_positions is not None
            and (
                type(max_additional_positions) is not int
                or max_additional_positions < 0
            )
        ):
            raise ValueError(
                "additional generation position limit must be nonnegative"
            )
        remaining_positions = max_additional_positions
        if self.limits.max_generation_positions is not None:
            configured_remaining = (
                self.limits.max_generation_positions
                - self.stats.generation_positions
                - reserve_positions
            )
            remaining_positions = (
                configured_remaining
                if remaining_positions is None
                else min(remaining_positions, configured_remaining)
            )
        if remaining_positions is not None:
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
                    root_contract_s3_neural_ordering=(
                        root_contract_s3_neural_ordering
                    ),
                )
                if native_final_score is not None
                else None
            )
            if series is None:
                if (
                    root_contract_s3_neural_ordering
                    and state.series_number == 2
                    and state.board.turn == chess.BLACK
                ):
                    # The browser root contract uses the frozen S3 student in
                    # this exact scope. A pure-Python fallback would silently
                    # change W512 order, so the safety lane must fail closed.
                    raise _WorkLimit
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
        *,
        charge_root_safety: bool = True,
    ) -> None:
        work = probe.positions_visited + probe.moves_generated
        self.stats.native_series_mate_positions += probe.positions_visited
        self.stats.native_series_mate_edges += probe.moves_generated
        if charge_root_safety:
            self._root_child_mate_screen_work += work
            self.stats.root_safety_screen_positions += work
        self.stats.generation_positions += work

    def _root_current_series_mate(
        self,
        state: ProgressiveState,
        *,
        required_prefix: tuple[str, ...],
    ) -> SeriesResult | None:
        """Returns a replay-proven mate-now on selective Series-5+ roots.

        This is a positive-proof lane only.  ``EXHAUSTED`` is useful
        telemetry, while every resource or compatibility stop stays UNKNOWN;
        neither outcome certifies a negative, and ordinary search continues
        with the remaining time and work.  Fixed intra-series prefixes and
        collect-all label searches retain their existing complete-root
        contracts.
        """

        if (
            self.limits.collect_all_root_scores
            or required_prefix
            or state.series_number < ROOT_CURRENT_SERIES_MATE_MIN_SERIES
            or (
                self.limits.max_generation_positions is not None
                and self.limits.max_generation_positions
                < ROOT_CURRENT_SERIES_MATE_MIN_TOTAL_WORK
            )
        ):
            return None

        available = self.limits.max_generation_positions
        if available is None:
            work_limit = ROOT_CURRENT_SERIES_MATE_WORK_LIMIT
        else:
            available -= self.stats.generation_positions
            if available < ROOT_CURRENT_SERIES_MATE_MIN_WORK:
                return None
            work_limit = min(
                ROOT_CURRENT_SERIES_MATE_WORK_LIMIT,
                available // ROOT_CURRENT_SERIES_MATE_WORK_DENOMINATOR,
            )
            if work_limit < ROOT_CURRENT_SERIES_MATE_MIN_WORK:
                return None

        remaining_seconds = (
            None
            if self._deadline is None
            else max(0.0, self._deadline - time.perf_counter())
        )
        if remaining_seconds == 0:
            return None
        probe_seconds = (
            ROOT_CURRENT_SERIES_MATE_TIME_LIMIT_SECONDS
            if remaining_seconds is None
            else min(
                ROOT_CURRENT_SERIES_MATE_TIME_LIMIT_SECONDS,
                remaining_seconds / ROOT_CURRENT_SERIES_MATE_TIME_DENOMINATOR,
            )
        )
        if probe_seconds <= 0:
            return None

        from .series_mate import SeriesMateStatus, find_native_series_mate

        self.stats.root_current_series_mate_probes += 1
        self.stats.native_series_mate_calls += 1
        probe = find_native_series_mate(
            state,
            max_positions=None,
            max_work=work_limit,
            time_limit_seconds=probe_seconds,
        )
        self._record_native_series_mate_probe(
            probe,
            charge_root_safety=False,
        )
        self.stats.root_current_series_mate_work += (
            probe.positions_visited + probe.moves_generated
        )
        if probe.status is SeriesMateStatus.FOUND:
            if probe.series is None:  # pragma: no cover - adapter invariant
                raise RuntimeError("native root mate status carried no line")
            self.stats.native_series_mate_found += 1
            self.stats.root_current_series_mate_found += 1
            self._selective = True
            return probe.series
        if probe.status is SeriesMateStatus.EXHAUSTED:
            self.stats.native_series_mate_exhausted += 1
            self.stats.root_current_series_mate_exhausted += 1
            return None
        if probe.status is SeriesMateStatus.WORK_LIMIT:
            self.stats.native_series_mate_work_limit_hits += 1
        elif probe.status is SeriesMateStatus.DEADLINE:
            self.stats.native_series_mate_deadline_hits += 1
        else:
            self.stats.native_series_mate_unsupported += 1
        self.stats.root_current_series_mate_unknown += 1
        return None

    def _certify_final_fallback_reply_mate(
        self,
        state: ProgressiveState,
        *,
        max_work: int | None = None,
        full_state_only: bool = False,
    ) -> "SeriesMateStatus":
        """Settles whether one selected root permits an immediate reply mate.

        This is deliberately not a selector.  FOUND rejects exactly the
        candidate supplied by the caller; it does not advance to another
        unchecked root.  EXHAUSTED is the sole exact-safe result.  Every
        resource or compatibility stop remains UNKNOWN to the caller.
        """

        from .series_mate import SeriesMateStatus, find_native_series_mate

        work_ceiling = (
            FINAL_FALLBACK_REPLY_MATE_WORK_LIMIT
            if max_work is None
            else max_work
        )
        if type(work_ceiling) is not int or work_ceiling < 1:
            raise ValueError("final fallback mate work must be a positive integer")

        cache_key = self._tt_key(state)
        position_key = state.transposition_key
        if cache_key in self._root_child_mate_screen_cache:
            cached_mate = self._root_child_mate_screen_cache[cache_key]
            if cached_mate is not None:
                self.stats.final_fallback_reply_mate_cache_hits += 1
                self.stats.final_fallback_reply_mate_found += 1
                return SeriesMateStatus.FOUND
            if cache_key in self._root_child_native_mate_cache_keys:
                self.stats.final_fallback_reply_mate_cache_hits += 1
                self.stats.final_fallback_reply_mate_exhausted += 1
                return SeriesMateStatus.EXHAUSTED
        if not full_state_only:
            if position_key in self._root_child_proven_mate_keys:
                self.stats.final_fallback_reply_mate_cache_hits += 1
                self.stats.final_fallback_reply_mate_found += 1
                return SeriesMateStatus.FOUND
            if position_key in self._root_child_native_mate_exhausted_keys:
                self.stats.final_fallback_reply_mate_cache_hits += 1
                self.stats.final_fallback_reply_mate_exhausted += 1
                return SeriesMateStatus.EXHAUSTED

        persistent = self._persistent_mate_proof(state)
        if persistent is not None:
            status, mate = persistent
            self.stats.final_fallback_reply_mate_cache_hits += 1
            self._root_child_mate_screen_cache[cache_key] = mate
            self._root_child_native_mate_cache_keys.add(cache_key)
            if status == "found":
                assert mate is not None
                if not full_state_only:
                    self._mark_root_child_proven_mate(position_key)
                self.stats.final_fallback_reply_mate_found += 1
                return SeriesMateStatus.FOUND
            if not full_state_only:
                self._mark_root_child_exact_exhausted(position_key)
            self.stats.final_fallback_reply_mate_exhausted += 1
            return SeriesMateStatus.EXHAUSTED

        configured_work = self.limits.max_generation_positions
        remaining_work = (
            work_ceiling
            if configured_work is None
            else max(
                0,
                min(
                    work_ceiling,
                    configured_work - self.stats.generation_positions,
                ),
            )
        )
        if remaining_work < 1:
            self.stats.final_fallback_reply_mate_unknown += 1
            return SeriesMateStatus.WORK_LIMIT

        remaining_seconds = (
            None
            if self._deadline is None
            else max(0.0, self._deadline - time.perf_counter())
        )
        if remaining_seconds == 0:
            self.stats.final_fallback_reply_mate_unknown += 1
            return SeriesMateStatus.DEADLINE

        self.stats.final_fallback_reply_mate_probes += 1
        self.stats.native_series_mate_calls += 1
        probe = find_native_series_mate(
            state,
            max_positions=None,
            max_work=remaining_work,
            time_limit_seconds=remaining_seconds,
        )
        work = probe.positions_visited + probe.moves_generated
        self._record_native_series_mate_probe(
            probe,
            charge_root_safety=False,
        )
        self.stats.final_fallback_reply_mate_work += work
        if probe.status is SeriesMateStatus.FOUND:
            if probe.series is None:  # pragma: no cover - adapter invariant
                raise RuntimeError("native final fallback mate status carried no line")
            self.stats.native_series_mate_found += 1
            self.stats.final_fallback_reply_mate_found += 1
            self._root_child_mate_screen_cache[cache_key] = probe.series
            self._root_child_native_mate_cache_keys.add(cache_key)
            if not full_state_only:
                self._mark_root_child_proven_mate(position_key)
            self._store_persistent_mate_proof(
                state,
                probe.series,
                proof_work=work,
            )
            return probe.status
        if probe.status is SeriesMateStatus.EXHAUSTED:
            self.stats.native_series_mate_exhausted += 1
            self.stats.final_fallback_reply_mate_exhausted += 1
            self._root_child_mate_screen_cache[cache_key] = None
            self._root_child_native_mate_cache_keys.add(cache_key)
            if not full_state_only:
                self._mark_root_child_exact_exhausted(position_key)
            self._store_persistent_mate_proof(
                state,
                None,
                proof_work=work,
                exhausted=True,
            )
            return probe.status
        if probe.status is SeriesMateStatus.WORK_LIMIT:
            self.stats.native_series_mate_work_limit_hits += 1
        elif probe.status is SeriesMateStatus.DEADLINE:
            self.stats.native_series_mate_deadline_hits += 1
        else:
            self.stats.native_series_mate_unsupported += 1
        self.stats.final_fallback_reply_mate_unknown += 1
        return probe.status

    def _cached_full_state_reply_mate_status(
        self,
        state: ProgressiveState,
    ) -> "SeriesMateStatus | None":
        """Returns exact evidence only when it binds the complete child state."""

        from .series_mate import SeriesMateStatus

        cache_key = self._tt_key(state)
        if cache_key in self._root_child_mate_screen_cache:
            cached_mate = self._root_child_mate_screen_cache[cache_key]
            if cached_mate is not None:
                return SeriesMateStatus.FOUND
            if cache_key in self._root_child_native_mate_cache_keys:
                return SeriesMateStatus.EXHAUSTED

        persistent = self._persistent_mate_proof(state)
        if persistent is None:
            return None
        status, mate = persistent
        self._root_child_mate_screen_cache[cache_key] = mate
        self._root_child_native_mate_cache_keys.add(cache_key)
        if status == "found":
            assert mate is not None
            return SeriesMateStatus.FOUND
        return SeriesMateStatus.EXHAUSTED

    def _final_safe_reselection(
        self,
        root: ProgressiveState,
        selected: SeriesResult,
        retained: tuple[ScoredSeries, ...],
        *,
        allow_widening: bool,
    ) -> _FinalSafeReselection:
        """Research-only D0 rescue after an exact selected-child mate.

        Already-retained siblings may be reused only with exact full-state
        EXHAUSTED evidence (or an authoritative terminal outcome). Otherwise
        one root-only width-512 frontier is generated in native engine order.
        FOUND and every UNKNOWN status are skipped; only EXHAUSTED or a
        terminal non-loss can cross the boundary. No deeper score, PV, proof,
        or alternative survives the rejected selection.
        """

        from .series_mate import SeriesMateStatus

        lane_started_work = self.stats.work_positions
        seen: set[_TTKey] = {self._tt_key(selected.final_state)}
        exclusions = self._root_policy_exclusions()
        retained_safe: list[tuple[SeriesResult, bool]] = []
        retained_evidence: dict[_TTKey, SeriesMateStatus] = {}

        def remaining_lane_work() -> int:
            return max(
                0,
                FINAL_FALLBACK_SAFE_RESELECTION_TOTAL_WORK_LIMIT
                - (self.stats.work_positions - lane_started_work),
            )

        def publish(
            candidate: SeriesResult,
            *,
            terminal: bool,
        ) -> _FinalSafeReselection | None:
            self._check_deadline()
            timed_out = False
            work_limited = False
            ladder_gate_applied = False
            if terminal:
                score = self._terminal_score(candidate, root.board.turn, 1)
                if score is None:  # pragma: no cover - caller invariant
                    raise RuntimeError(
                        "terminal safe reselection carried no terminal score"
                    )
                self.stats.final_fallback_safe_reselection_terminal += 1
            else:
                # The full-state immediate-mate miss authorizes one distinct
                # A/check, B/only-countercheck, C/mate proof. Keep this inside
                # the same 40M rescue lane: FOUND rejects this sibling and the
                # selector keeps walking, while UNKNOWN remains eligible and
                # is not retried outside the lane at final publication.
                if self._selected_root_single_reply_ladder_required(
                    candidate.final_state
                ):
                    ladder_gate_applied = True
                    ladder_probe = self._selected_root_single_reply_ladder_probe(
                        candidate.final_state,
                        max_work=remaining_lane_work(),
                    )
                    if (
                        ladder_probe is not None
                        and ladder_probe.proven_losing
                    ):
                        self._selected_pv_root_vetoes.add(
                            candidate.machine_notation
                        )
                        self.stats.selected_root_ladder_candidate_vetoes += 1
                        self.stats.selected_root_ladder_final_rejections += 1
                        self._selective = True
                        return None
                score = None
                remaining = remaining_lane_work()
                if remaining > 0:
                    try:
                        self._check_deadline()
                        score = self._evaluate(
                            candidate.final_state,
                            max_additional_positions=remaining,
                        ).total
                    except _Timeout:
                        timed_out = True
                    except _WorkLimit:
                        work_limited = True
                self.stats.final_fallback_safe_reselection_exhausted += 1
            self.stats.final_fallback_safe_reselection_rescues += 1
            return _FinalSafeReselection(
                series=candidate,
                score=score,
                timed_out=timed_out,
                work_limited=work_limited,
                ladder_gate_applied=ladder_gate_applied,
            )

        try:
            retained_seen = set(seen)
            for item in retained:
                candidate = item.series
                if candidate.machine_notation in exclusions:
                    continue
                key = self._tt_key(candidate.final_state)
                if key in retained_seen:
                    continue
                retained_seen.add(key)
                if candidate.outcome is not None:
                    if candidate.outcome is Outcome.CHECKMATE:
                        self.stats.final_fallback_safe_reselection_candidates += 1
                        return publish(candidate, terminal=True)
                    if not allow_widening:
                        self.stats.final_fallback_safe_reselection_candidates += 1
                        retained_safe.append((candidate, True))
                    continue
                status = self._cached_full_state_reply_mate_status(
                    candidate.final_state
                )
                if status is None:
                    # Retained does not mean certified. Leave an unknown child
                    # eligible for the bounded full-state probe when it
                    # reappears in the authoritative widened ordering.
                    continue
                retained_evidence[key] = status
                if not allow_widening:
                    self.stats.final_fallback_safe_reselection_candidates += 1
                    if status is SeriesMateStatus.EXHAUSTED:
                        retained_safe.append((candidate, False))
                    elif status is SeriesMateStatus.FOUND:
                        self.stats.final_fallback_safe_reselection_found += 1

            for candidate, terminal in retained_safe:
                rescued = publish(candidate, terminal=terminal)
                if rescued is not None:
                    return rescued
            if not allow_widening:
                return _FinalSafeReselection()
            self.stats.final_fallback_safe_reselection_attempts += 1
            try:
                generated, _width_complete = self._generate(
                    root,
                    ply_from_root=1,
                    required_prefix=(),
                    # The safety lane deliberately protects tactical roots at
                    # the cutoff: the recorded Bucephalus escape is omitted by
                    # an unprotected W512 frontier but retained by production's
                    # normal root-generation policy.
                    tactical_protection=True,
                    max_frontier_states=(
                        FINAL_FALLBACK_SAFE_RESELECTION_FRONTIER
                    ),
                    max_additional_positions=remaining_lane_work(),
                    root_contract_s3_neural_ordering=True,
                )
            except _Timeout:
                return _FinalSafeReselection(timed_out=True)
            except _WorkLimit:
                self.stats.final_fallback_safe_reselection_budget_interruptions += (
                    1
                )
                return _FinalSafeReselection(work_limited=True)

            candidates = (
                generated.references()
                if isinstance(generated, _NativeSeriesBatch)
                else generated
            )
            # A current-series mate is authoritative and cheap once the root
            # frontier exists, so it gets one global prepass. Every other
            # terminal or exact-safe child stays in canonical production order;
            # a late draw must not preempt an earlier nonterminal safe choice.
            for raw_candidate in candidates:
                self._check_deadline()
                if raw_candidate.machine_notation in exclusions:
                    continue
                if raw_candidate.outcome is not Outcome.CHECKMATE:
                    continue
                candidate = self._materialize_series(raw_candidate)
                key = self._tt_key(candidate.final_state)
                if key in seen:
                    continue
                seen.add(key)
                self.stats.final_fallback_safe_reselection_candidates += 1
                return publish(candidate, terminal=True)

            lane_work_limited = False
            for candidate_index, raw_candidate in enumerate(candidates, start=1):
                try:
                    self._check_deadline()
                except _Timeout:
                    return _FinalSafeReselection(timed_out=True)
                if raw_candidate.machine_notation in exclusions:
                    continue
                candidate = self._materialize_series(raw_candidate)
                key = self._tt_key(candidate.final_state)
                if key in seen:
                    continue
                seen.add(key)
                self.stats.final_fallback_safe_reselection_candidates += 1
                if candidate.outcome is not None:
                    return publish(candidate, terminal=True)

                status = retained_evidence.get(key)
                if status is None:
                    status = self._cached_full_state_reply_mate_status(
                        candidate.final_state
                    )
                if status is None:
                    remaining = remaining_lane_work()
                    if remaining < 1:
                        if not lane_work_limited:
                            self.stats.final_fallback_safe_reselection_budget_interruptions += (
                                1
                            )
                        lane_work_limited = True
                        self.stats.final_fallback_safe_reselection_unknown += 1
                        continue
                    status = self._certify_final_fallback_reply_mate(
                        candidate.final_state,
                        max_work=min(
                            (
                                FINAL_FALLBACK_SAFE_RESELECTION_EARLY_CHILD_WORK_LIMIT
                                if candidate_index
                                <= FINAL_FALLBACK_SAFE_RESELECTION_EARLY_FRONTIER
                                else FINAL_FALLBACK_SAFE_RESELECTION_CHILD_WORK_LIMIT
                            ),
                            remaining,
                        ),
                        full_state_only=True,
                    )
                if status is SeriesMateStatus.EXHAUSTED:
                    rescued = publish(candidate, terminal=False)
                    if rescued is not None:
                        return rescued
                    continue
                if status is SeriesMateStatus.FOUND:
                    self.stats.final_fallback_safe_reselection_found += 1
                    continue
                self.stats.final_fallback_safe_reselection_unknown += 1
                if status is SeriesMateStatus.DEADLINE:
                    return _FinalSafeReselection(timed_out=True)
                configured_work = self.limits.max_generation_positions
                if (
                    remaining_lane_work() < 1
                    or (
                        configured_work is not None
                        and self.stats.work_positions >= configured_work
                    )
                ):
                    if not lane_work_limited:
                        self.stats.final_fallback_safe_reselection_budget_interruptions += (
                            1
                        )
                    lane_work_limited = True
            return _FinalSafeReselection(work_limited=lane_work_limited)
        except _Timeout:
            return _FinalSafeReselection(timed_out=True)
        except _WorkLimit:
            self.stats.final_fallback_safe_reselection_budget_interruptions += 1
            return _FinalSafeReselection(work_limited=True)
        finally:
            self.stats.final_fallback_safe_reselection_work += (
                self.stats.work_positions - lane_started_work
            )

    def _selected_pv_horizon_cached_probe(
        self,
        state: ProgressiveState,
    ) -> "SeriesMateProbe | None":
        """Peeks at replay-valid exact in-process boundary evidence only."""

        from .series_mate import SeriesMateProbe, SeriesMateStatus

        cache_key = self._tt_key(state)
        if cache_key not in self._root_child_mate_screen_cache:
            return None
        cached_mate = self._root_child_mate_screen_cache[cache_key]
        position_key = state.transposition_key
        if (
            cached_mate is not None
            and position_key in self._root_child_proven_mate_keys
        ):
            return SeriesMateProbe(
                SeriesMateStatus.FOUND,
                "reused exact in-process mate proof",
                cached_mate,
            )
        if (
            cached_mate is None
            and cache_key in self._root_child_native_mate_cache_keys
            and position_key in self._root_child_native_mate_exhausted_keys
        ):
            return SeriesMateProbe(
                SeriesMateStatus.EXHAUSTED,
                "reused exact in-process mate exhaustion",
            )
        return None

    def _selected_pv_horizon_probe(
        self,
        state: ProgressiveState,
    ) -> SeriesMateProbe:
        """Runs or reuses one exact boundary probe under the shared safety budget."""

        from .series_mate import (
            SeriesMateProbe,
            SeriesMateStatus,
            find_native_series_mate,
        )

        cached = self._selected_pv_horizon_cached_probe(state)
        if cached is not None:
            return cached

        cache_key = self._tt_key(state)
        position_key = state.transposition_key
        persistent = self._persistent_mate_proof(state)
        if persistent is not None:
            status, cached_mate = persistent
            self._root_child_mate_screen_cache[cache_key] = cached_mate
            self._root_child_native_mate_cache_keys.add(cache_key)
            if status == "found":
                assert cached_mate is not None
                self._mark_root_child_proven_mate(position_key)
                return SeriesMateProbe(
                    SeriesMateStatus.FOUND,
                    "reused persistent mate proof",
                    cached_mate,
                )
            self._mark_root_child_exact_exhausted(position_key)
            return SeriesMateProbe(
                SeriesMateStatus.EXHAUSTED,
                "reused persistent mate exhaustion",
            )

        self.stats.selected_pv_horizon_probe_calls += 1
        self.stats.native_series_mate_calls += 1
        remaining = self._root_child_mate_screen_remaining()
        if remaining < 1:
            self.stats.native_series_mate_work_limit_hits += 1
            return SeriesMateProbe(
                SeriesMateStatus.WORK_LIMIT,
                "selected-PV horizon safety budget is exhausted",
            )
        remaining_seconds = (
            None
            if self._deadline is None
            else max(0.0, self._deadline - time.perf_counter())
        )
        probe = find_native_series_mate(
            state,
            max_positions=None,
            max_work=remaining,
            time_limit_seconds=remaining_seconds,
        )
        work = probe.positions_visited + probe.moves_generated
        self._record_native_series_mate_probe(probe)
        if probe.status is SeriesMateStatus.FOUND:
            if probe.series is None:  # pragma: no cover - adapter invariant
                raise RuntimeError("native selected-PV mate status carried no line")
            self.stats.native_series_mate_found += 1
            self._root_child_mate_screen_cache[cache_key] = probe.series
            self._root_child_native_mate_cache_keys.add(cache_key)
            self._mark_root_child_proven_mate(position_key)
            self._store_persistent_mate_proof(
                state,
                probe.series,
                proof_work=work,
            )
        elif probe.status is SeriesMateStatus.EXHAUSTED:
            self.stats.native_series_mate_exhausted += 1
            self._root_child_mate_screen_cache[cache_key] = None
            self._root_child_native_mate_cache_keys.add(cache_key)
            self._mark_root_child_exact_exhausted(position_key)
            self._store_persistent_mate_proof(
                state,
                None,
                proof_work=work,
                exhausted=True,
            )
        elif probe.status is SeriesMateStatus.WORK_LIMIT:
            self.stats.native_series_mate_work_limit_hits += 1
        elif probe.status is SeriesMateStatus.DEADLINE:
            self.stats.native_series_mate_deadline_hits += 1
        else:
            self.stats.native_series_mate_unsupported += 1
        return probe

    def _certify_selected_pv_horizon(
        self,
        root: ProgressiveState,
        pv: tuple[SeriesResult, ...],
    ) -> SelectedPvHorizonCertification:
        from .selected_pv_horizon import (
            SelectedPvHorizonStatus,
            certify_selected_pv_horizon,
        )

        certification = certify_selected_pv_horizon(
            root,
            pv,
            self._selected_pv_horizon_probe,
            cached_probe=self._selected_pv_horizon_cached_probe,
        )
        if certification.status is SelectedPvHorizonStatus.FOUND:
            self.stats.selected_pv_horizon_found += 1
        elif certification.status is SelectedPvHorizonStatus.EXHAUSTED:
            self.stats.selected_pv_horizon_exhausted += 1
        elif certification.status is SelectedPvHorizonStatus.UNKNOWN:
            self.stats.selected_pv_horizon_unknown += 1
        return certification

    @staticmethod
    def _raise_selected_pv_horizon_unknown(
        certification: SelectedPvHorizonCertification,
    ) -> None:
        from .series_mate import SeriesMateStatus

        if certification.probe_status is SeriesMateStatus.DEADLINE:
            raise _Timeout
        # Work-limit, unsupported, invalid replay, and malformed native output
        # are all UNKNOWN. Abort this depth without certifying the candidate.
        raise _WorkLimit

    def _repair_selected_root_python(
        self,
        root: ProgressiveState,
        candidate: ScoredSeries,
        depth: int,
        state: CandidateHorizonState,
    ) -> ScoredSeries:
        """Pure-Python exact-leaf overlay used when no native subtree exists."""

        manifest_key = self._tt_key(root)
        manifest = self._selected_pv_python_root_manifests.get(manifest_key)
        if manifest is None:
            generated, width_complete = self._generate(
                root,
                ply_from_root=1,
                tactical_protection=bool(
                    self._root_tactical_frontier_protection
                ),
            )
            collection: tuple[SeriesResult, ...] | _NativeSeriesBatch
            if isinstance(generated, _NativeSeriesBatch):
                collection = generated
                retained_count = len(generated)
            else:
                ordered = self._ordered(
                    root,
                    generated,
                    root.board.turn,
                    1,
                    tactical_protection=bool(
                        self._root_tactical_frontier_protection
                    ),
                )
                collection = tuple(ordered)
                retained_count = len(ordered)
            manifest = _SeriesCacheEntry(collection, width_complete)
            self._selected_pv_python_root_manifests[manifest_key] = manifest
            manifest_weight = max(1, retained_count)
            self._selected_pv_python_root_manifest_weight += manifest_weight
            self.stats.series_generation_cache_peak = max(
                self.stats.series_generation_cache_peak,
                self._series_generation_cache_weight
                + self._selected_pv_python_root_manifest_weight,
            )
            self.stats.series_generation_cache_entries_peak = max(
                self.stats.series_generation_cache_entries_peak,
                len(self._series_generation_cache)
                + len(self._selected_pv_python_root_manifests),
            )
        else:
            self._selected_pv_python_root_manifests.move_to_end(manifest_key)
            self.stats.series_generation_cache_hits += 1

        proof_keys: list[_TTKey] = []
        proof_overrides: dict[_TTKey, SeriesResult] = {}
        for proof in state.retained_proofs:
            if not proof.rooted_path or proof.rooted_path[0] != candidate.series:
                raise _WorkLimit
            cursor = root
            for supplied in (*proof.rooted_path, proof.mate_reply):
                replay_work = len(supplied.moves)
                if (
                    self.limits.max_generation_positions is not None
                    and self.stats.generation_positions + replay_work
                    > self.limits.max_generation_positions
                ):
                    self.stats.generation_work_limit_hits += 1
                    raise _WorkLimit
                self._check_deadline()
                try:
                    replayed = play_series(cursor, supplied.moves).with_transposition_count(
                        supplied.transposition_count
                    )
                except ValueError as error:
                    raise _WorkLimit from error
                self.stats.generated_raw_series += 1
                self.stats.generated_unique_series += 1
                self.stats.series_generation_positions += replay_work
                self.stats.generation_positions += replay_work
                if replayed != supplied:
                    raise _WorkLimit
                cursor = replayed.final_state
            leaf = proof.rooted_path[-1].final_state
            key = self._tt_key(leaf)
            proof_overrides[key] = proof.mate_reply
            proof_keys.append(key)
        if not proof_keys:  # pragma: no cover - policy invariant
            raise RuntimeError("selected-PV repair has no retained proof")
        if self._tt_transaction_stack:  # pragma: no cover - root invariant
            raise RuntimeError("selected-PV repair entered during a TT transaction")
        proof_set_identity: _SelectedPvProofSetIdentity = (
            candidate.series.moves,
            candidate.series.san,
            self._tt_key(candidate.series.final_state),
            candidate.series.ended_by_check,
            candidate.series.outcome,
            candidate.series.unused_moves,
            candidate.series.transposition_count,
            tuple(
                sorted(proof.identity_sha256 for proof in state.retained_proofs)
            ),
        )
        proof_tt = self._selected_pv_proof_tt_namespaces.get(proof_set_identity)
        new_namespace = proof_tt is None
        if proof_tt is None:
            if (
                len(self._selected_pv_proof_tt_namespaces)
                >= SELECTED_PV_PROOF_TT_NAMESPACE_CAPACITY
            ):
                self._selected_pv_proof_tt_namespaces.popitem(last=False)
            proof_tt = {}
            self._selected_pv_proof_tt_namespaces[proof_set_identity] = proof_tt

        ordinary_tt = self._tt
        ordinary_overrides = self._selected_pv_leaf_mate_overrides
        ordinary_override_hits = self._selected_pv_leaf_override_hits
        proof_override_hits: set[_TTKey] = set()
        self._tt = proof_tt
        self._selected_pv_leaf_mate_overrides = proof_overrides
        self._selected_pv_leaf_override_hits = proof_override_hits
        completed = False
        try:
            score, child_pv, proof_bounds = self._minimax(
                candidate.series.final_state,
                depth - 1,
                -MATE_SCORE * 2,
                MATE_SCORE * 2,
                1,
            )
            if proof_keys[-1] not in proof_override_hits:
                raise _WorkLimit
            completed = True
            return ScoredSeries(
                candidate.series,
                score,
                child_pv,
                proof_bounds,
            )
        finally:
            self._tt = ordinary_tt
            self._selected_pv_leaf_mate_overrides = ordinary_overrides
            self._selected_pv_leaf_override_hits = ordinary_override_hits
            if not completed:
                proof_tt.clear()
                if new_namespace:
                    self._selected_pv_proof_tt_namespaces.pop(
                        proof_set_identity,
                        None,
                    )

    def _repair_selected_root_native(
        self,
        root: ProgressiveState,
        candidate: ScoredSeries,
        depth: int,
        state: CandidateHorizonState,
    ) -> ScoredSeries:
        """Warm full-window native re-search under replayed leaf proofs."""

        session = self._native_subtree_session
        if session is None:  # pragma: no cover - caller invariant
            raise RuntimeError("native selected-PV repair has no session")
        def enumerate_retained(forced: SeriesResult | None):
            external_work, remaining_nanoseconds = self._native_work_context()
            current_manifest = session.enumerate_root(
                root,
                preferred_series=candidate.series.machine_notation,
                external_work=external_work,
                remaining_nanoseconds=remaining_nanoseconds,
                forced_preferred=forced,
            )
            self._sync_native_subtree_stats(
                current_manifest.work.cumulative_stats
            )
            self._selective = self._selective or current_manifest.selective
            self._evaluation_work_limit_reached = (
                self._evaluation_work_limit_reached
                or current_manifest.evaluation_work_limit_reached
            )
            if current_manifest.status == 1:
                raise _WorkLimit
            if current_manifest.status == 2:
                raise _Timeout
            if current_manifest.status == 3:
                raise _AdjudicationPending
            if current_manifest.status != 0:
                raise _WorkLimit
            current_retained = next(
                (
                    item
                    for item in current_manifest.candidates
                    if item.order_key == candidate.series.machine_notation
                    and item.series == candidate.series
                ),
                None,
            )
            return current_manifest, current_retained

        # Most Python-owned roots already exist in the native retained width.
        # Reuse that canonical manifest without paying for an unnecessary
        # authoritative replay. Only force-retain the selected legal root when
        # the independent native ordering really omitted it.
        manifest, retained = enumerate_retained(None)
        if retained is None:
            manifest, retained = enumerate_retained(candidate.series)
        if retained is None:
            raise _WorkLimit
        native_proofs = tuple(
            NativeHorizonProof(proof.rooted_path, proof.mate_reply)
            for proof in state.retained_proofs
        )
        if not native_proofs:  # pragma: no cover - policy invariant
            raise RuntimeError("selected-PV repair has no retained proof")
        external_work, remaining_nanoseconds = self._native_work_context()
        result = session.search_root_candidate(
            enumeration_identity=manifest.enumeration_identity,
            candidate_identity=retained.candidate_identity,
            child_depth=depth - 1,
            alpha=-MATE_SCORE * 2,
            beta=MATE_SCORE * 2,
            external_work=external_work,
            remaining_nanoseconds=remaining_nanoseconds,
            rollback_tt=False,
            horizon_proofs=native_proofs,
        )
        self._sync_native_subtree_stats(result.work.cumulative_stats)
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
            raise _WorkLimit
        newest_mask = 1 << (len(native_proofs) - 1)
        if (
            result.bound is not NativeSubtreeBound.EXACT
            or result.root_series != candidate.series
            or result.horizon_proofs_validated != len(native_proofs)
            or not result.horizon_proof_set_identity
            or result.horizon_proof_hit_mask & newest_mask == 0
        ):
            raise _WorkLimit
        return ScoredSeries(
            candidate.series,
            result.score,
            result.child_principal_variation,
            result.proof_bounds,
        )

    def _repair_selected_root(
        self,
        root: ProgressiveState,
        candidate: ScoredSeries,
        depth: int,
        state: CandidateHorizonState,
    ) -> ScoredSeries:
        if self._native_subtree_session is None:
            return self._repair_selected_root_python(root, candidate, depth, state)
        return self._repair_selected_root_native(root, candidate, depth, state)

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
            self._root_child_native_mate_cache_keys.add(cache_key)
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

    def _selected_root_single_reply_ladder_probe(
        self,
        state: ProgressiveState,
        *,
        max_work: int = SELECTED_ROOT_SINGLE_REPLY_LADDER_WORK_LIMIT,
    ) -> "SingleReplyMateLadderProbe | None":
        """Proves one narrow three-series loss after immediate safety passes.

        ``FOUND`` and ``EXHAUSTED`` are exact only for the A/check,
        B/only-countercheck, C/mate pattern and are cached by the complete
        Progressive state. Resource stops, unsupported kernels, and replay
        failures remain UNKNOWN: they neither veto the selected root nor enter
        the cache. Every native position and edge is charged to the ordinary
        global work ledger, with a one-million-work per-candidate ceiling.
        """

        from .single_reply_mate_ladder import (
            SingleReplyMateLadderStatus,
            find_native_single_reply_mate_ladder,
        )

        cache_key = self._tt_key(state)
        cached = self._selected_root_ladder_cache.get(cache_key)
        if cached is not None:
            self.stats.selected_root_ladder_cache_hits += 1
            if cached.status is SingleReplyMateLadderStatus.FOUND:
                self.stats.selected_root_ladder_found += 1
            else:
                self.stats.selected_root_ladder_exhausted += 1
            return cached

        configured_work = self.limits.max_generation_positions
        remaining_work = min(
            SELECTED_ROOT_SINGLE_REPLY_LADDER_WORK_LIMIT,
            max(0, max_work),
        )
        if configured_work is not None:
            remaining_work = min(
                remaining_work,
                max(0, configured_work - self.stats.generation_positions),
            )
        remaining_seconds = (
            None
            if self._deadline is None
            else max(0.0, self._deadline - time.perf_counter())
        )
        if remaining_work < 1 or remaining_seconds == 0:
            self.stats.selected_root_ladder_unknown += 1
            return None

        self.stats.selected_root_ladder_probe_calls += 1
        try:
            probe = find_native_single_reply_mate_ladder(
                state,
                max_work=remaining_work,
                time_limit_seconds=remaining_seconds,
            )
        except Exception:
            # A malformed native result or failed authoritative replay is not
            # evidence about the chess position. Keep the candidate eligible,
            # but conservatively charge the whole dispatched allowance because
            # a failed adapter cannot provide a trustworthy native receipt.
            self.stats.generation_positions += remaining_work
            self.stats.selected_root_ladder_work += remaining_work
            self.stats.selected_root_ladder_unknown += 1
            return None

        self.stats.generation_positions += probe.work_used
        self.stats.selected_root_ladder_work += probe.work_used
        if (
            probe.status is SingleReplyMateLadderStatus.FOUND
            and probe.proven_losing
        ):
            self._selected_root_ladder_cache[cache_key] = probe
            self.stats.selected_root_ladder_found += 1
            return probe
        if probe.status is SingleReplyMateLadderStatus.EXHAUSTED:
            self._selected_root_ladder_cache[cache_key] = probe
            self.stats.selected_root_ladder_exhausted += 1
            return probe
        self.stats.selected_root_ladder_unknown += 1
        return None

    def _selected_root_single_reply_ladder_required(
        self,
        state: ProgressiveState,
    ) -> bool:
        """Returns whether the exact immediate gate authorizes this proof lane."""

        cache_key = self._tt_key(state)
        # A king cannot deliver a legal check by itself. With no non-king piece
        # the side to move therefore cannot supply the theorem's checking A,
        # so dispatching the native three-series prover would only perturb an
        # otherwise frozen exact work receipt.
        attacker_has_checking_material = bool(
            state.board.occupied_co[state.board.turn] & ~state.board.kings
        )
        return (
            state.series_number
            >= SELECTED_ROOT_SINGLE_REPLY_LADDER_MIN_CHILD_SERIES
            and attacker_has_checking_material
            and cache_key in self._root_child_mate_screen_cache
            and self._root_child_mate_screen_cache[cache_key] is None
            and cache_key in self._root_child_native_mate_cache_keys
        )

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
        key = self._tt_key(state)
        mate_override = self._selected_pv_leaf_mate_overrides.get(key)
        if mate_override is not None:
            score = self._terminal_score(
                mate_override,
                state.board.turn,
                ply_from_root + 1,
            )
            if score is None:  # pragma: no cover - replay invariant
                raise RuntimeError("selected-PV mate override is nonterminal")
            self._selected_pv_leaf_override_hits.add(key)
            return (
                score,
                (mate_override,),
                self._terminal_proof_bounds(mate_override, state.board.turn),
            )
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

    @staticmethod
    def _mate_score_has_matching_proof(
        score: int,
        proof_bounds: tuple[int, int],
    ) -> bool:
        if score >= MATE_SCORE - 10_000:
            return proof_bounds == (1, 1)
        if score <= -MATE_SCORE + 10_000:
            return proof_bounds == (-1, -1)
        return False

    @classmethod
    def _root_candidate_has_publishable_mate_claim(
        cls,
        score: int,
        series: SeriesResult,
        proof_bounds: tuple[int, int],
    ) -> bool:
        """Accept one mate-band root score only with its own exact proof."""

        if abs(score) < MATE_SCORE - 10_000:
            return True
        if series.outcome is not None:
            if series.outcome != Outcome.CHECKMATE:
                return False
            if not series.moves:
                # A boundary checkmate has no mover to credit. The side still
                # on move is the checkmated side, so its opponent is the
                # authoritative winner. Non-empty series have already handed
                # the turn across and retain the ordinary ended-by-check rule.
                winner = not series.final_state.board.turn
            else:
                root_mover = not series.final_state.board.turn
                winner = (
                    root_mover if series.ended_by_check else not root_mover
                )
            expected = (
                MATE_SCORE - 1 if winner == chess.WHITE else -MATE_SCORE + 1
            )
            return score == expected
        return cls._mate_score_has_matching_proof(
            score,
            proof_bounds,
        )

    @classmethod
    def _selected_root_has_publishable_mate_claim(
        cls,
        score: int,
        series: SeriesResult,
        alternatives: tuple[ScoredSeries, ...],
    ) -> bool:
        """Accept mate-band root scores only with candidate-local exact proof."""

        if abs(score) < MATE_SCORE - 10_000:
            return True
        selected = next(
            (
                item
                for item in alternatives
                if item.series == series and item.score == score
            ),
            None,
        )
        proof_bounds = (
            selected.proof_bounds
            if selected is not None
            else UNKNOWN_PROOF_BOUNDS
        )
        return cls._root_candidate_has_publishable_mate_claim(
            score,
            series,
            proof_bounds,
        )

    def _quarantine_root_mate_claim(self, series: SeriesResult) -> None:
        """Remove an unproved mate claim before it can affect root ranking."""

        notation = series.machine_notation
        if self._root_mate_claim_emergency_fallback is None:
            self._root_mate_claim_emergency_fallback = series
        if notation in self._root_mate_claim_quarantines:
            return
        self._root_mate_claim_quarantines.add(notation)
        self.stats.root_mate_claim_quarantines += 1
        self._selective = True
        self._root_scores_complete = False
        # A quarantined false winner can expose a different root whose
        # established immediate-series safety proof needs more than the
        # default 3M reserve. It may use the caller's remaining documented
        # work allowance, but never exceed that public contract or 20M.
        configured_work = self.limits.max_generation_positions
        if configured_work is not None:
            self._root_child_mate_screen_budget = max(
                self._root_child_mate_screen_budget,
                min(
                    ROOT_MATE_CLAIM_SAFETY_RESERVE_LIMIT,
                    configured_work,
                ),
            )

    def _root_policy_exclusions(
        self,
        horizon_vetoes: set[str] | frozenset[str] = frozenset(),
    ) -> frozenset[str]:
        return frozenset(
            horizon_vetoes
            | self._selected_pv_root_vetoes
            | self._root_mate_claim_quarantines
        )

    def _root_policy_rejection_flags(self, notation: str) -> tuple[bool, bool]:
        return (
            notation in self._selected_pv_root_vetoes,
            notation in self._root_mate_claim_quarantines,
        )

    def _record_root_policy_prior_depth_discard(
        self,
        horizon_vetoed: bool,
        mate_quarantined: bool,
    ) -> None:
        if horizon_vetoed:
            self.stats.selected_pv_horizon_prior_depth_discards += 1
        if mate_quarantined:
            self.stats.root_mate_claim_prior_depth_discards += 1

    def _record_root_policy_move_only_fallback(self) -> None:
        if self._selected_pv_root_vetoes:
            self.stats.selected_pv_horizon_move_only_fallbacks += 1
        if self._root_mate_claim_quarantines:
            self.stats.root_mate_claim_move_only_fallbacks += 1

    def _search_root_pass(
        self,
        state: ProgressiveState,
        depth: int,
        required_prefix: tuple[str, ...],
        root_mate_overrides: Mapping[_TTKey, SeriesResult],
        root_horizon_overrides: Mapping[str, ScoredSeries] | None = None,
        root_horizon_vetoes: frozenset[str] = frozenset(),
        root_frontier_override: _GeneratedSeriesList | None = None,
    ) -> tuple[
        int,
        tuple[SeriesResult, ...],
        tuple[ScoredSeries, ...],
        str | None,
    ]:
        if root_horizon_overrides is None:
            root_horizon_overrides = {}
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
        if root_frontier_override is None:
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
                # The ordinary frontier exhausted only the non-reserved part
                # of the deterministic budget. Spend the remaining at-most-
                # one-position-per-micro-move allowance on a legal width-one
                # seed. The outer policy still excludes any horizon veto.
                fallback = self._generate_root_seed(
                    state,
                    required_prefix=required_prefix,
                )
                raise _RootInterrupted((), error, fallback) from error
        else:
            series = root_frontier_override
            width_complete = series.width_complete
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
        root_exclusions = self._root_policy_exclusions(root_horizon_vetoes)
        if root_exclusions:
            self._selective = True
            series = tuple(
                result
                for result in series
                if result.machine_notation not in root_exclusions
            )
            if not series:
                self._root_scores_complete = False
                return self._evaluate(state).total, (), (), None
        scored: list[ScoredSeries] = []
        root_alpha = -MATE_SCORE * 2
        root_beta = MATE_SCORE * 2
        has_non_adverse_exact = False

        def move_only_fallback() -> SeriesResult:
            exclusions = self._root_policy_exclusions(root_horizon_vetoes)
            scored_fallback = next(
                (
                    item.series
                    for item in _proof_safe_root_order(mover, scored)
                    if item.series.machine_notation not in exclusions
                ),
                None,
            )
            if scored_fallback is not None:
                return scored_fallback
            frontier_fallback = next(
                (
                    candidate
                    for candidate in series
                    if candidate.machine_notation not in exclusions
                ),
                series[0],
            )
            return self._materialize_series(frontier_fallback)

        for result in series:
            try:
                self._check_deadline()
                horizon_override = root_horizon_overrides.get(
                    result.machine_notation
                )
                if horizon_override is not None:
                    materialized = self._materialize_series(result)
                    if horizon_override.series != materialized:
                        raise RuntimeError(
                            "selected-PV root repair drifted from its candidate"
                        )
                    if not self._root_candidate_has_publishable_mate_claim(
                        horizon_override.score,
                        materialized,
                        horizon_override.proof_bounds,
                    ):
                        self._quarantine_root_mate_claim(materialized)
                        continue
                    scored.append(horizon_override)
                    if not _root_candidate_is_proven_adverse(
                        mover, horizon_override
                    ):
                        has_non_adverse_exact = True
                        if mover == chess.WHITE:
                            root_alpha = max(root_alpha, horizon_override.score)
                        else:
                            root_beta = min(root_beta, horizon_override.score)
                    continue
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
                        if not self._root_candidate_has_publishable_mate_claim(
                            scored_candidate.score,
                            scored_candidate.series,
                            scored_candidate.proof_bounds,
                        ):
                            self._quarantine_root_mate_claim(
                                scored_candidate.series
                            )
                            continue
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
                    move_only_fallback(),
                ) from error
            except _AdjudicationPending as error:
                # A child reaching the quiet-series threshold can make the
                # whole minimax iteration unknown. The ordered root frontier
                # is nevertheless complete and legal, so carry one series to
                # the engine-play liveness path without assigning the child a
                # draw or heuristic minimax score.
                self._root_scores_complete = False
                raise _RootAdjudicationPending(
                    move_only_fallback()
                ) from error
            if score_is_exact:
                materialized = self._materialize_series(result)
                scored_candidate = ScoredSeries(
                    materialized,
                    score,
                    child_pv,
                    proof_bounds,
                )
                if not self._root_candidate_has_publishable_mate_claim(
                    scored_candidate.score,
                    scored_candidate.series,
                    scored_candidate.proof_bounds,
                ):
                    self._quarantine_root_mate_claim(materialized)
                    continue
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
                (
                    mover == chess.WHITE
                    and score == MATE_SCORE - 1
                    or mover == chess.BLACK
                    and score == -MATE_SCORE + 1
                )
                and self._mate_score_has_matching_proof(score, proof_bounds)
            ):
                break
        # This flag intentionally means every retained root candidate has an
        # exact score. ``exact_width`` separately reports whether the retained
        # frontier contains every legal branch.
        self._root_scores_complete = (
            not root_horizon_vetoes
            and not self._root_mate_claim_quarantines
            and len(scored) == len(series)
        )
        scored = list(_proof_safe_root_order(mover, scored))
        if not scored:
            static = self._evaluate(state).total
            return static, (), (), None
        best = scored[0]
        proof_bounds = self._combine_proof_bounds(
            mover,
            [item.proof_bounds for item in scored],
            all_branches_visited=(
                width_complete
                and not root_horizon_vetoes
                and not self._root_mate_claim_quarantines
                and len(scored) == len(series)
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
        excluded_series: frozenset[str] = frozenset(),
    ) -> SeriesResult | None:
        """Prefers exact-safe children and never returns a proven mate child."""

        unique: list[SeriesResult] = []
        seen: set[tuple[int, str, int, int]] = set()
        for candidate in candidates:
            if (
                candidate is None
                or candidate.outcome is not None
                or candidate.machine_notation in excluded_series
            ):
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

    def _selected_pv_horizon_widened_frontier(
        self,
        state: ProgressiveState,
        required_prefix: tuple[str, ...],
        vetoes: frozenset[str],
    ) -> _GeneratedSeriesList:
        """Regenerates one bounded wider root after the retained set is vetoed."""

        self.stats.selected_pv_horizon_widenings += 1
        generated, width_complete = self._generate(
            state,
            ply_from_root=1,
            required_prefix=required_prefix,
            tactical_protection=True,
            max_frontier_states=ROOT_ALL_MATING_WIDEN_FRONTIER,
        )
        if isinstance(generated, _NativeSeriesBatch):
            ordered: list[SeriesResult | _NativeSeriesReference] = (
                generated.references()
            )
        else:
            mover = state.board.turn
            ordered = sorted(
                generated,
                key=lambda item: (
                    (
                        -self._static_series_score(item, mover, 1)
                        if mover == chess.WHITE
                        else self._static_series_score(item, mover, 1)
                    ),
                    item.machine_notation,
                ),
            )
        widened = self._apply_root_promotion_mate_lane(
            state,
            _GeneratedSeriesList(ordered, width_complete=width_complete),
            ply_from_root=1,
            required_prefix=required_prefix,
            reserve_positions=0,
        )
        candidates = [
            candidate
            for candidate in widened
            if candidate.machine_notation not in vetoes
        ]
        self.stats.selected_pv_horizon_widened_candidates += len(candidates)
        return _GeneratedSeriesList(
            candidates,
            width_complete=widened.width_complete and not vetoes,
        )

    def _root_all_mating_widening(
        self,
        state: ProgressiveState,
        depth: int,
        required_prefix: tuple[str, ...],
        retained: tuple[ScoredSeries, ...],
        excluded_series: frozenset[str] = frozenset(),
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
        if excluded_series:
            candidates = tuple(
                candidate
                for candidate in candidates
                if candidate.machine_notation not in excluded_series
            )
            retained = tuple(
                item
                for item in retained
                if item.series.machine_notation not in excluded_series
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
            scored_candidate = ScoredSeries(
                materialized,
                score,
                child_pv,
                proof_bounds,
            )
            self.stats.root_safety_widened_exact_children += 1
            if not self._root_candidate_has_publishable_mate_claim(
                scored_candidate.score,
                scored_candidate.series,
                scored_candidate.proof_bounds,
            ):
                self._quarantine_root_mate_claim(materialized)
                continue
            safe_scored.append(scored_candidate)

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
        """Certifies provisional root winners, then deterministically retries.

        A wide immediate-reply probe is safety verification, not move ordering.
        Running it on every successive alpha contender made hosted depth two
        spend most of its budget on moves that could never become the final
        choice.  Each pass now completes ordinary root selection first.  An
        unsafe provisional winner is assigned its authoritative replay-mate
        override by child position, and the cached root/minimax pass is
        repeated with a reset root window until a screened winner survives.

        The selected canonical PV then receives the same exact one-series leaf
        certification used by the browser policy. A first replayed proof
        re-searches that same root under a proof namespace; a second distinct
        proof vetoes only that root series. Any incomplete proof or repair is
        UNKNOWN and aborts this iterative depth before it can be published.
        Between the immediate reply gate and that PV certification, a separate
        exact prover may veto the selected root only when it replays the narrow
        A/check, B/only-countercheck, C/mate ladder. An incomplete ladder probe
        leaves the root eligible.
        """

        from .selected_pv_horizon import (
            CandidateHorizonState,
            HorizonPolicyAction,
            SelectedPvHorizonStatus,
            observe_horizon_proof,
        )

        # The browser creates a fresh coordinator for every iterative depth.
        # Keep the run-visible set scoped to this depth as well: it exists only
        # to stop an interrupted depth from resurrecting one of its own vetoes.
        # A later, freshly started depth is allowed to reconsider that root.
        self._selected_pv_root_vetoes.clear()
        self._root_mate_claim_quarantines.clear()
        self._root_mate_claim_emergency_fallback = None

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
        horizon_overrides: dict[str, ScoredSeries] = {}
        # Veto eligibility is scoped to this selected depth, matching the
        # browser policy. The searcher-wide set below is only a final-return
        # block until a later depth positively certifies that root again.
        horizon_vetoes: set[str] = set()
        ladder_vetoes: set[str] = set()
        ladder_vetoed_candidates: dict[str, ScoredSeries] = {}
        mate_claim_quarantines = self._root_mate_claim_quarantines
        horizon_states: dict[str, CandidateHorizonState] = {}
        widened_frontier: _GeneratedSeriesList | None = None
        last_exact_exhausted: SeriesResult | None = None
        last_unknown: SeriesResult | None = None
        mate_claim_unquarantined_fallback: SeriesResult | None = None

        def quarantine_unproved_mate_claim(
            score: int,
            pv: tuple[SeriesResult, ...],
            alternatives: tuple[ScoredSeries, ...],
        ) -> bool:
            nonlocal mate_claim_unquarantined_fallback
            if not pv or self._selected_root_has_publishable_mate_claim(
                score,
                pv[0],
                alternatives,
            ):
                return False

            provisional = pv[0]
            self._quarantine_root_mate_claim(provisional)
            mate_claim_unquarantined_fallback = next(
                (
                    item.series
                    for item in alternatives
                    if item.series.machine_notation not in mate_claim_quarantines
                ),
                mate_claim_unquarantined_fallback,
            )
            return True

        while True:
            try:
                self.stats.root_safety_passes += 1
                score, pv, alternatives, proof = self._search_root_pass(
                    state,
                    depth,
                    required_prefix,
                    overrides,
                    horizon_overrides,
                    frozenset(horizon_vetoes),
                    widened_frontier,
                )
            except _RootInterrupted as interrupted:
                interrupted_scored_fallback = next(
                    (
                        item.series
                        for item in _proof_safe_root_order(
                            state.board.turn,
                            interrupted.scored,
                        )
                        if item.series.machine_notation
                        not in self._root_policy_exclusions(horizon_vetoes)
                    ),
                    None,
                )
                if horizon_vetoes or mate_claim_quarantines:
                    # Keep only an explicitly unexcluded move-only fallback from
                    # the current frontier. Its score/proof never survives this
                    # interrupted depth, and a rejected seed can never escape.
                    fallback = self._root_safety_fallback(
                        interrupted_scored_fallback,
                        interrupted.fallback,
                        mate_claim_unquarantined_fallback,
                        last_exact_exhausted,
                        last_unknown,
                        excluded_series=self._root_policy_exclusions(
                            horizon_vetoes
                        ),
                    )
                    if fallback is None:
                        if (
                            not horizon_vetoes
                            and self._root_mate_claim_emergency_fallback
                        ):
                            self.stats.root_mate_claim_all_quarantined += 1
                            raise _RootMateClaimPending(
                                self._root_mate_claim_emergency_fallback,
                                interrupted.cause,
                            ) from interrupted
                        raise interrupted.cause from interrupted
                    raise _RootInterrupted(
                        (), interrupted.cause, fallback
                    ) from interrupted
                if (
                    not self._root_child_safety_screen_required()
                    and not horizon_states
                    and not horizon_vetoes
                    and not mate_claim_quarantines
                ):
                    raise
                # A retry has reset alpha/beta around new authoritative evidence.
                # Partial scores from any pass cannot certify the iteration, so
                # expose only an exact-EXHAUSTED or explicitly UNKNOWN child as
                # a move fallback. A replay-proven mate child is never eligible.
                fallback = self._root_safety_fallback(
                    interrupted_scored_fallback,
                    last_exact_exhausted,
                    last_unknown,
                    mate_claim_unquarantined_fallback,
                    interrupted.fallback,
                    excluded_series=self._root_policy_exclusions(
                        horizon_vetoes
                    ),
                )
                if fallback is None:
                    raise interrupted.cause from interrupted
                raise _RootInterrupted((), interrupted.cause, fallback) from interrupted
            except (_Timeout, _WorkLimit) as error:
                if horizon_vetoes and not mate_claim_quarantines:
                    raise
                if (
                    not self._root_child_safety_screen_required()
                    and not horizon_states
                    and not horizon_vetoes
                    and not mate_claim_quarantines
                ):
                    raise
                # Root generation can be interrupted before _search_root_pass has
                # materialized a frontier and wrapped the cancellation. A retry
                # may still have a previously screened exact-EXHAUSTED or UNKNOWN
                # child; keep only that move, never its now-discarded score,
                # alternatives, or proof. A first-pass raw cancellation with no
                # eligible child retains the historical no-move result in run().
                fallback = self._root_safety_fallback(
                    last_exact_exhausted,
                    last_unknown,
                    mate_claim_unquarantined_fallback,
                    excluded_series=self._root_policy_exclusions(
                        horizon_vetoes
                    ),
                )
                if fallback is None:
                    if (
                        not horizon_vetoes
                        and self._root_mate_claim_emergency_fallback
                    ):
                        self.stats.root_mate_claim_all_quarantined += 1
                        raise _RootMateClaimPending(
                            self._root_mate_claim_emergency_fallback,
                            error,
                        ) from error
                    raise
                raise _RootInterrupted((), error, fallback) from error
            if not pv:
                if horizon_vetoes and widened_frontier is None:
                    self.stats.selected_pv_horizon_all_vetoed_frontiers += 1
                    # For a ladder-only rejection set, retain the complete
                    # widened manifest and let _search_root_pass apply the
                    # known vetoes. Filtering here would correctly make the
                    # returned list incomplete, but would also erase the only
                    # evidence that the underlying legal frontier itself was
                    # exhaustive. Mixed horizon/mate-claim exclusions keep the
                    # established filtered, selective behavior.
                    widening_exclusions = self._root_policy_exclusions(
                        horizon_vetoes
                    )
                    if (
                        horizon_vetoes == ladder_vetoes
                        and not mate_claim_quarantines
                        and not required_prefix
                    ):
                        widening_exclusions = frozenset()
                    widened_frontier = (
                        self._selected_pv_horizon_widened_frontier(
                            state,
                            required_prefix,
                            widening_exclusions,
                        )
                    )
                    if widened_frontier:
                        continue
                if horizon_vetoes:
                    if (
                        horizon_vetoes == ladder_vetoes
                        and ladder_vetoed_candidates
                        and not mate_claim_quarantines
                        and not required_prefix
                        and widened_frontier is not None
                        and widened_frontier.width_complete
                    ):
                        # Only a genuinely exhaustive widened frontier can say
                        # every legal root has this replay-proven narrow loss.
                        # Preserve best resistance as an explicit D0 move-only
                        # exception; no score, PV continuation, alternatives,
                        # or proof from these vetoed candidates may publish.
                        adverse = _proof_safe_root_order(
                            state.board.turn,
                            tuple(ladder_vetoed_candidates.values()),
                        )
                        best = adverse[0]
                        self.stats.selected_root_ladder_all_vetoed_fallbacks += 1
                        self._root_scores_complete = False
                        self._selective = True
                        raise _RootLadderAllVetoed(best.series)
                    # The bounded wider selector found no unvetoed root. No
                    # known-vetoed series may cross SearchResult.best_series.
                    self._root_scores_complete = False
                    raise _HorizonPolicyExhausted
                if mate_claim_quarantines:
                    if (
                        self._root_mate_claim_emergency_fallback is None
                    ):  # pragma: no cover
                        raise RuntimeError(
                            "mate-claim quarantine lost its legal fallback"
                        )
                    self.stats.root_mate_claim_all_quarantined += 1
                    raise _RootMateClaimPending(
                        self._root_mate_claim_emergency_fallback
                    )
                return score, pv, alternatives, proof

            provisional = pv[0]
            if quarantine_unproved_mate_claim(score, pv, alternatives):
                continue
            if provisional.outcome is not None:
                self._selected_pv_root_vetoes.discard(
                    provisional.machine_notation
                )
                return score, pv, alternatives, proof
            child_state = provisional.final_state
            child_key = child_state.transposition_key
            override_key = self._tt_key(child_state)
            widened_nonterminal = False
            if self._root_child_safety_screen_required():
                if child_key in self._root_child_native_mate_exhausted_keys:
                    last_exact_exhausted = provisional
                elif child_key not in self._root_child_proven_mate_keys:
                    last_unknown = provisional
                if override_key in overrides:
                    # The best root choice still loses after every root bound
                    # was repaired around its authoritative reply mate. If
                    # every retained choice has the same replay-proven defect,
                    # widen the selector before describing the capped set as a
                    # loss.
                    if alternatives and all(
                        item.series.outcome is None
                        and (
                            item.series.final_state.transposition_key
                            in self._root_child_proven_mate_keys
                            or _root_candidate_is_proven_adverse(
                                state.board.turn, item
                            )
                        )
                        for item in alternatives
                    ):
                        if state.series_number <= ROOT_ALL_MATING_WIDEN_MAX_SERIES:
                            widened = self._root_all_mating_widening(
                                state,
                                depth,
                                required_prefix,
                                alternatives,
                                self._root_policy_exclusions(horizon_vetoes),
                            )
                            score, pv, alternatives, proof = widened
                            if not pv:
                                return widened
                            provisional = pv[0]
                            if provisional.outcome is not None:
                                return widened
                            if quarantine_unproved_mate_claim(
                                score,
                                pv,
                                alternatives,
                            ):
                                continue
                            child_state = provisional.final_state
                            child_key = child_state.transposition_key
                            override_key = self._tt_key(child_state)
                            widened_nonterminal = True
                        else:
                            self._selective = True
                            return score, pv, alternatives, None
                    else:
                        return score, pv, alternatives, proof
                if not widened_nonterminal:
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
                        fallback = self._root_safety_fallback(
                            last_exact_exhausted,
                            provisional,
                            last_unknown,
                            excluded_series=self._root_policy_exclusions(
                                horizon_vetoes
                            ),
                        )
                        if fallback is None:
                            raise
                        raise _RootInterrupted((), error, fallback) from error
                    if reply_mate is not None:
                        self._mark_root_child_proven_mate(child_key)
                        if (
                            last_unknown is not None
                            and last_unknown.final_state.transposition_key == child_key
                        ):
                            last_unknown = None
                        overrides[override_key] = reply_mate
                        self.stats.root_safety_retries += 1
                        continue
                    if child_key in self._root_child_native_mate_exhausted_keys:
                        last_exact_exhausted = provisional

                if (
                    widened_nonterminal
                    and child_key in self._root_child_proven_mate_keys
                ):
                    # The widened selector proved that this root permits an
                    # immediate reply mate. That mate ends the game, so no
                    # later PV boundary is reachable or needs a second repair.
                    # Preserve the forced-loss line only until the final exact
                    # publication gate rejects it from user-facing output.
                    return score, pv, alternatives, proof

            # The ladder theorem begins only after the existing exact
            # immediate-mate gate has exhausted on this same full child. A
            # selective/UNKNOWN immediate result cannot be upgraded by asking
            # a different, narrower three-series question.
            ladder_probe = (
                self._selected_root_single_reply_ladder_probe(child_state)
                if self._selected_root_single_reply_ladder_required(child_state)
                else None
            )
            if ladder_probe is not None and ladder_probe.proven_losing:
                notation = provisional.machine_notation
                candidate = next(
                    (item for item in alternatives if item.series == provisional),
                    ScoredSeries(provisional, score, pv[1:]),
                )
                ladder_vetoes.add(notation)
                horizon_vetoes.add(notation)
                ladder_vetoed_candidates[notation] = candidate
                self._selected_pv_root_vetoes.add(notation)
                if (
                    last_exact_exhausted is not None
                    and last_exact_exhausted.machine_notation == notation
                ):
                    last_exact_exhausted = None
                if (
                    last_unknown is not None
                    and last_unknown.machine_notation == notation
                ):
                    last_unknown = None
                self.stats.selected_root_ladder_candidate_vetoes += 1
                self._selective = True
                continue

            # At depth one the exact immediate child probe is the selected-PV
            # leaf probe. The distinct ladder question above must still run,
            # but the ordinary horizon certifier would only repeat that same
            # one-series immediate-mate question.
            if self._root_child_safety_screen_required() and len(pv) == 1:
                self._selected_pv_root_vetoes.discard(
                    provisional.machine_notation
                )
                return score, pv, alternatives, proof

            certification = self._certify_selected_pv_horizon(state, pv)
            if certification.status in {
                SelectedPvHorizonStatus.NOT_APPLICABLE,
                SelectedPvHorizonStatus.EXHAUSTED,
            }:
                self._selected_pv_root_vetoes.discard(
                    provisional.machine_notation
                )
                return score, pv, alternatives, proof
            if certification.status is SelectedPvHorizonStatus.UNKNOWN:
                try:
                    self._raise_selected_pv_horizon_unknown(certification)
                except (_Timeout, _WorkLimit) as error:
                    if horizon_vetoes:
                        # A known-vetoed earlier root invalidates any retained
                        # depth that selected it. Preserve this current,
                        # explicitly unvetoed root only as a move-only fallback;
                        # its incomplete score/PV/proof remain unpublished.
                        raise _RootInterrupted((), error, provisional) from error
                    raise
            horizon_proof = certification.proof
            if horizon_proof is None:  # pragma: no cover - status invariant
                raise RuntimeError("found selected-PV horizon has no proof")

            candidate = next(
                (item for item in alternatives if item.series == provisional),
                ScoredSeries(provisional, score, pv[1:]),
            )
            notation = provisional.machine_notation
            candidate_state = horizon_states.get(
                notation,
                CandidateHorizonState(candidate_series=notation),
            )
            decision = observe_horizon_proof(candidate_state, horizon_proof)
            self.stats.selected_pv_horizon_line_rejections += 1
            if decision.action is HorizonPolicyAction.UNKNOWN:
                raise _WorkLimit
            if decision.action is HorizonPolicyAction.VETO:
                horizon_overrides.pop(notation, None)
                horizon_vetoes.add(notation)
                self._selected_pv_root_vetoes.add(notation)
                if (
                    last_exact_exhausted is not None
                    and last_exact_exhausted.machine_notation == notation
                ):
                    last_exact_exhausted = None
                if (
                    last_unknown is not None
                    and last_unknown.machine_notation == notation
                ):
                    last_unknown = None
                self.stats.selected_pv_horizon_candidate_vetoes += 1
                self._selective = True
                continue

            # A replay-proven adverse leaf makes the current un-repaired A
            # unsafe to resurrect from an older completed depth. Keep it in
            # the run-visible exclusion set until the same-root repair really
            # completes. A fresh iterative depth clears this depth-local set.
            self._selected_pv_root_vetoes.add(notation)
            try:
                repaired = self._repair_selected_root(
                    state,
                    candidate,
                    depth,
                    decision.next_state,
                )
            except (_Timeout, _WorkLimit) as error:
                self.stats.selected_pv_horizon_repair_interruptions += 1
                fallback = self._root_safety_fallback(
                    *(
                        item.series
                        for item in alternatives
                        if item.series.machine_notation != notation
                    ),
                    last_exact_exhausted,
                    last_unknown,
                    excluded_series=self._root_policy_exclusions(
                        horizon_vetoes | {notation}
                    ),
                )
                if fallback is None:
                    raise
                raise _RootInterrupted((), error, fallback) from error
            except _AdjudicationPending as error:
                self.stats.selected_pv_horizon_repair_interruptions += 1
                fallback = self._root_safety_fallback(
                    *(
                        item.series
                        for item in alternatives
                        if item.series.machine_notation != notation
                    ),
                    last_exact_exhausted,
                    last_unknown,
                    excluded_series=self._root_policy_exclusions(
                        horizon_vetoes | {notation}
                    ),
                )
                raise _RootAdjudicationPending(fallback) from error
            self._selected_pv_root_vetoes.discard(notation)
            horizon_states[notation] = (
                decision.next_state.record_successful_repair()
            )
            horizon_overrides[notation] = repaired
            self.stats.selected_pv_horizon_native_repairs += 1
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
        self._selected_pv_root_vetoes.clear()
        self._selected_root_ladder_emergency_fallback = None
        self._root_mate_claim_quarantines.clear()
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

        # A positive exact proof that the mover can finish the game in this
        # series outranks every heuristic, fallback, and interrupted deeper
        # search.  A bounded miss remains UNKNOWN and ordinary iterative search
        # continues with the remaining budget.
        root_current_series_mate = self._root_current_series_mate(
            state,
            required_prefix=required_prefix,
        )
        if root_current_series_mate is not None:
            self._root_widened_terminal_series = root_current_series_mate

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
            except _RootLadderAllVetoed as all_vetoed:
                # This is chess-loss liveness, not a completed search depth.
                # The exact frontier proved every legal root enters the same
                # narrow forced-mate class, so keep only its least-bad legal
                # move and label every other public field provisional.
                notation = all_vetoed.fallback.machine_notation
                self._selected_pv_root_vetoes.discard(notation)
                self._selected_root_ladder_emergency_fallback = notation
                self._selective = True
                self._root_scores_complete = False
                best_score = root_evaluation.total
                best_pv = (all_vetoed.fallback,)
                alternatives = ()
                best_proof = None
                completed_depth = 0
                completed_root_scores_complete = False
                break
            except _RootMateClaimPending as pending:
                timed_out = isinstance(pending.cause, _Timeout)
                work_limit_reached = isinstance(pending.cause, _WorkLimit)
                prior_horizon_vetoed, prior_mate_quarantined = (
                    self._root_policy_rejection_flags(
                        best_pv[0].machine_notation
                    )
                    if completed_depth > 0 and best_pv
                    else (False, False)
                )
                self._record_root_policy_prior_depth_discard(
                    prior_horizon_vetoed,
                    prior_mate_quarantined,
                )
                self._record_root_policy_move_only_fallback()
                # Every score/PV claim from this depth is discarded. Retain the
                # deterministic legal series only as a provisional D0/root-
                # evaluation candidate; the final exact reply-mate gate still
                # decides whether any move may cross the public boundary.
                self._root_mate_claim_quarantines.discard(
                    pending.fallback.machine_notation
                )
                self._selective = True
                self._root_scores_complete = False
                best_score = root_evaluation.total
                best_pv = (pending.fallback,)
                alternatives = ()
                best_proof = None
                completed_depth = 0
                completed_root_scores_complete = False
                break
            except _RootInterrupted as interrupted:
                timed_out = isinstance(interrupted.cause, _Timeout)
                work_limit_reached = isinstance(interrupted.cause, _WorkLimit)
                self._selective = True
                # The pass may stop after scoring a mate-looking root but before
                # ordinary selected-root quarantine runs. Treat each such
                # partial as UNKNOWN now; it cannot rank, become the fallback,
                # or resurrect the same root from a completed shallow depth.
                for item in interrupted.scored:
                    if self._selected_root_has_publishable_mate_claim(
                        item.score,
                        item.series,
                        (item,),
                    ):
                        continue
                    notation = item.series.machine_notation
                    if notation not in self._root_mate_claim_quarantines:
                        self._root_mate_claim_quarantines.add(notation)
                        self.stats.root_mate_claim_quarantines += 1
                prior_horizon_vetoed, prior_mate_quarantined = (
                    self._root_policy_rejection_flags(
                        best_pv[0].machine_notation
                    )
                    if completed_depth > 0 and best_pv
                    else (False, False)
                )
                prior_root_rejected = (
                    prior_horizon_vetoed or prior_mate_quarantined
                )
                if completed_depth == 0 or prior_root_rejected:
                    self._root_scores_complete = False
                    if interrupted.scored and not prior_root_rejected:
                        mover = state.board.turn
                        partial = _proof_safe_root_order(
                            mover,
                            tuple(
                                item
                                for item in interrupted.scored
                                if not any(
                                    self._root_policy_rejection_flags(
                                        item.series.machine_notation
                                    )
                                )
                            ),
                        )
                        if partial:
                            best = partial[0]
                            best_score = best.score
                            best_pv = (best.series,) + best.principal_variation
                            alternatives = partial
                        else:
                            best_score = root_evaluation.total
                            best_pv = ()
                            alternatives = ()
                    else:
                        # This is only a provisional move candidate. Keep the
                        # explicitly labeled root evaluation rather than
                        # pretending the chosen series completed a search; the
                        # final exact gate may still withhold it.
                        best_score = root_evaluation.total
                        fallback_rejected = any(
                            self._root_policy_rejection_flags(
                                interrupted.fallback.machine_notation
                            )
                        )
                        if (
                            not fallback_rejected
                        ):
                            best_pv = (interrupted.fallback,)
                            self._record_root_policy_move_only_fallback()
                        else:
                            best_pv = ()
                        alternatives = ()
                    best_proof = None
                    self._record_root_policy_prior_depth_discard(
                        prior_horizon_vetoed,
                        prior_mate_quarantined,
                    )
                    if prior_root_rejected:
                        completed_depth = 0
                        completed_root_scores_complete = False
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
            except _HorizonPolicyExhausted:
                self._selective = True
                if completed_depth == 0:
                    self._root_scores_complete = False
                else:
                    self._root_scores_complete = completed_root_scores_complete
                break
            except _AdjudicationPending as pending:
                prior_horizon_vetoed, prior_mate_quarantined = (
                    self._root_policy_rejection_flags(
                        best_pv[0].machine_notation
                    )
                    if completed_depth > 0 and best_pv
                    else (False, False)
                )
                prior_root_rejected = (
                    prior_horizon_vetoed or prior_mate_quarantined
                )
                if isinstance(pending, _RootAdjudicationPending) and (
                    completed_depth == 0 or prior_root_rejected
                ):
                    # The current depth learned that the retained A is unsafe,
                    # then repair reached a position requiring manual proof.
                    # Preserve that honest adjudication state, but never revive
                    # A: an explicitly unvetoed current-frontier B may remain
                    # only as an unevaluated provisional fallback pending the
                    # final exact reply-mate gate.
                    self._selective = True
                    self._root_scores_complete = False
                    adjudication = "manual-proof-required"
                    best_score = root_evaluation.total
                    fallback_rejected = bool(
                        pending.fallback is not None
                        and any(
                            self._root_policy_rejection_flags(
                                pending.fallback.machine_notation
                            )
                        )
                    )
                    if (
                        pending.fallback is not None
                        and not fallback_rejected
                    ):
                        best_pv = (pending.fallback,)
                        self._record_root_policy_move_only_fallback()
                    else:
                        best_pv = ()
                    alternatives = ()
                    best_proof = None
                    self._record_root_policy_prior_depth_discard(
                        prior_horizon_vetoed,
                        prior_mate_quarantined,
                    )
                    completed_depth = 0
                    completed_root_scores_complete = False
                    break
                if prior_root_rejected:
                    # A semantic stop is no more entitled than a deadline to
                    # resurrect a root rejected by the current deeper depth.
                    self._selective = True
                    self._root_scores_complete = False
                    adjudication = "manual-proof-required"
                    best_score = root_evaluation.total
                    best_pv = ()
                    alternatives = ()
                    best_proof = None
                    self._record_root_policy_prior_depth_discard(
                        prior_horizon_vetoed,
                        prior_mate_quarantined,
                    )
                    completed_depth = 0
                    completed_root_scores_complete = False
                    break
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
                    # No iteration completed, but root generation did. Stage
                    # its first deterministic legal series as a provisional
                    # fallback. Score/proof/alternatives deliberately stay at
                    # the root/no-proof values because the child was not
                    # adjudicated or evaluated; the final exact gate may still
                    # withhold the move.
                    self._selective = True
                    self._root_scores_complete = False
                    adjudication = "manual-proof-required"
                    best_score = root_evaluation.total
                    best_pv = (
                        (pending.fallback,) if pending.fallback is not None else ()
                    )
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

        publishable_alternatives: list[ScoredSeries] = []
        for item in alternatives:
            if self._root_candidate_has_publishable_mate_claim(
                item.score,
                item.series,
                item.proof_bounds,
            ):
                publishable_alternatives.append(item)
                continue
            selected_claim = bool(
                best_pv
                and item.series == best_pv[0]
                and item.score == best_score
            )
            if not selected_claim:
                self._quarantine_root_mate_claim(item.series)
            completed_root_scores_complete = False
        if len(publishable_alternatives) != len(alternatives):
            alternatives = tuple(publishable_alternatives)
            best_proof = None

        final_horizon_vetoed, final_mate_quarantined = (
            self._root_policy_rejection_flags(best_pv[0].machine_notation)
            if best_pv
            else (False, False)
        )
        if final_horizon_vetoed or final_mate_quarantined:
            # A deeper iteration can discover that an earlier completed
            # depth's root is unsafe or made an unproved mate claim before that
            # deeper iteration stops. Never resurrect it through fallback.
            self._record_root_policy_prior_depth_discard(
                final_horizon_vetoed,
                final_mate_quarantined,
            )
            best_score = root_evaluation.total
            best_pv = ()
            alternatives = ()
            best_proof = None
            completed_depth = 0
            completed_root_scores_complete = False
            self._root_scores_complete = False

        selected_mate_claim_publishable = bool(
            best_pv
            and self._selected_root_has_publishable_mate_claim(
                best_score,
                best_pv[0],
                alternatives,
            )
        )
        if (
            abs(best_score) >= MATE_SCORE - 10_000
            and not selected_mate_claim_publishable
        ):
            # Defense in depth for interrupted/repaired paths that do not pass
            # through the ordinary candidate-local quarantine. UNKNOWN may
            # stage a legal root-only candidate for the final exact gate, but it
            # may never retain a mate-looking score, a completed depth,
            # alternatives, or a continuation.
            self.stats.root_mate_claim_final_discards += 1
            self._selective = True
            self._root_scores_complete = False
            completed_root_scores_complete = False
            best_score = root_evaluation.total
            best_pv = best_pv[:1]
            alternatives = ()
            best_proof = None
            completed_depth = 0

        final_ladder_gate_applied = False
        if best_pv and best_pv[0].outcome is None:
            # Every nonterminal move crosses one final exact publication gate,
            # including a winner from a fully completed iterative depth.  The
            # earlier root selector normally supplies a cached proof, making
            # this defense-in-depth check free.  If that proof is adverse or
            # unresolved, publish no move.  In particular, never substitute an
            # unchecked sibling and never call one proven-mating child evidence
            # that every legal root loses.
            from .series_mate import SeriesMateStatus

            fallback_status = self._certify_final_fallback_reply_mate(
                best_pv[0].final_state
            )
            if fallback_status is not SeriesMateStatus.EXHAUSTED:
                rescued = _FinalSafeReselection()
                if fallback_status is SeriesMateStatus.FOUND:
                    self.stats.final_fallback_reply_mate_rejections += 1
                    cap = self.limits.max_series_per_node
                    if (
                        not self.limits.collect_all_root_scores
                        and not required_prefix
                    ):
                        rescued = self._final_safe_reselection(
                            state,
                            best_pv[0],
                            alternatives,
                            allow_widening=(
                                cap is not None
                                and cap
                                < FINAL_FALLBACK_SAFE_RESELECTION_FRONTIER
                            ),
                        )
                elif fallback_status is SeriesMateStatus.DEADLINE:
                    timed_out = True
                elif fallback_status is SeriesMateStatus.WORK_LIMIT:
                    work_limit_reached = True
                self._selective = True
                self._root_scores_complete = False
                completed_root_scores_complete = False
                timed_out = timed_out or rescued.timed_out
                work_limit_reached = (
                    work_limit_reached or rescued.work_limited
                )
                best_score = (
                    rescued.score
                    if rescued.score is not None
                    else root_evaluation.total
                )
                best_pv = (
                    (rescued.series,)
                    if rescued.series is not None
                    else ()
                )
                final_ladder_gate_applied = rescued.ladder_gate_applied
                alternatives = ()
                best_proof = None
                completed_depth = 0

        if (
            best_pv
            and best_pv[0].outcome is None
            and not final_ladder_gate_applied
            and self._selected_root_single_reply_ladder_required(
                best_pv[0].final_state
            )
        ):
            # Interrupted D0 candidates and older completed-depth candidates
            # do not necessarily pass through the ordinary selected-root loop.
            # Apply the same exact theorem at the last publication boundary.
            # The only known-losing move allowed through is the explicitly
            # labeled least-resistance exception produced after an exhaustive
            # all-legal frontier proved that every legal root has this loss.
            ladder_probe = self._selected_root_single_reply_ladder_probe(
                best_pv[0].final_state
            )
            if (
                ladder_probe is not None
                and ladder_probe.proven_losing
                and (
                    required_prefix
                    or best_pv[0].machine_notation
                    != self._selected_root_ladder_emergency_fallback
                )
            ):
                notation = best_pv[0].machine_notation
                self._selected_pv_root_vetoes.add(notation)
                self.stats.selected_root_ladder_candidate_vetoes += 1
                self.stats.selected_root_ladder_final_rejections += 1
                self._selective = True
                self._root_scores_complete = False
                completed_root_scores_complete = False
                best_score = root_evaluation.total
                best_pv = ()
                alternatives = ()
                best_proof = None
                completed_depth = 0

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
