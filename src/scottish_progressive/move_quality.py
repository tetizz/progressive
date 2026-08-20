from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Any, Mapping

import chess

from .search import MATE_SCORE, SearchResult


QUALITY_SCHEMA_VERSION = 1
DEFAULT_QUALITY_POLICY_VERSION = 1


class MoveQuality(StrEnum):
    BEST = "Best"
    EXCELLENT = "Excellent"
    GOOD = "Good"
    INACCURACY = "Inaccuracy"
    MISTAKE = "Mistake"
    BLUNDER = "Blunder"
    NOT_RATED = "Not rated"


class QualitySubject(StrEnum):
    MICRO_MOVE = "micro-move"
    SERIES = "series"


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    """Versioned custom bands for Scottish Progressive heuristic points.

    These values are deliberately not presented as centipawns.  They are an
    initial, provisional policy that can be replaced after self-play data has
    been calibrated against game outcomes.
    """

    version: int = DEFAULT_QUALITY_POLICY_VERSION
    name: str = "Scottish Progressive provisional custom quality v1"
    minimum_depth_series: int = 2
    allow_selective_evidence: bool = False
    best_max_loss: int = 0
    excellent_max_loss: int = 35
    good_max_loss: int = 100
    inaccuracy_max_loss: int = 250
    mistake_max_loss: int = 600

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("quality policy version must be positive")
        if not self.name.strip():
            raise ValueError("quality policy name cannot be empty")
        if self.minimum_depth_series < 1:
            raise ValueError("minimum_depth_series must be positive")
        thresholds = self.thresholds
        if thresholds[0] < 0 or tuple(sorted(thresholds)) != thresholds:
            raise ValueError(
                "quality loss thresholds must be non-negative and ordered"
            )

    @property
    def thresholds(self) -> tuple[int, int, int, int, int]:
        return (
            self.best_max_loss,
            self.excellent_max_loss,
            self.good_max_loss,
            self.inaccuracy_max_loss,
            self.mistake_max_loss,
        )

    @property
    def policy_id(self) -> str:
        encoded = json.dumps(
            self._identity_dict(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return "spc-quality-" + hashlib.sha256(encoded).hexdigest()[:16]

    def _identity_dict(self) -> dict[str, Any]:
        return {
            "schema_version": QUALITY_SCHEMA_VERSION,
            "version": self.version,
            "name": self.name,
            "minimum_depth_series": self.minimum_depth_series,
            "allow_selective_evidence": self.allow_selective_evidence,
            "thresholds_heuristic_points": {
                "best_max_loss": self.best_max_loss,
                "excellent_max_loss": self.excellent_max_loss,
                "good_max_loss": self.good_max_loss,
                "inaccuracy_max_loss": self.inaccuracy_max_loss,
                "mistake_max_loss": self.mistake_max_loss,
            },
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._identity_dict(),
            "policy_id": self.policy_id,
            "score_unit": "heuristic-points",
            "score_is_centipawns": False,
            "calibration_status": "provisional-custom-uncalibrated",
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> QualityPolicy:
        try:
            thresholds = payload.get("thresholds_heuristic_points", {})
            if not isinstance(thresholds, Mapping):
                raise ValueError("thresholds_heuristic_points must be an object")
            policy = cls(
                version=int(payload.get("version", DEFAULT_QUALITY_POLICY_VERSION)),
                name=str(
                    payload.get(
                        "name",
                        "Scottish Progressive provisional custom quality v1",
                    )
                ),
                minimum_depth_series=int(
                    payload.get("minimum_depth_series", 2)
                ),
                allow_selective_evidence=bool(
                    payload.get("allow_selective_evidence", False)
                ),
                best_max_loss=int(thresholds.get("best_max_loss", 0)),
                excellent_max_loss=int(
                    thresholds.get("excellent_max_loss", 35)
                ),
                good_max_loss=int(thresholds.get("good_max_loss", 100)),
                inaccuracy_max_loss=int(
                    thresholds.get("inaccuracy_max_loss", 250)
                ),
                mistake_max_loss=int(
                    thresholds.get("mistake_max_loss", 600)
                ),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid quality policy: {error}") from error

        supplied_schema = int(payload.get("schema_version", QUALITY_SCHEMA_VERSION))
        if supplied_schema != QUALITY_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported quality schema {supplied_schema}; "
                f"expected {QUALITY_SCHEMA_VERSION}"
            )
        supplied_id = payload.get("policy_id")
        if supplied_id is not None and str(supplied_id) != policy.policy_id:
            raise ValueError("quality policy_id does not match its parameters")
        return policy

    def classify_loss(self, loss: int) -> MoveQuality:
        effective_loss = max(0, loss)
        if effective_loss <= self.best_max_loss:
            return MoveQuality.BEST
        if effective_loss <= self.excellent_max_loss:
            return MoveQuality.EXCELLENT
        if effective_loss <= self.good_max_loss:
            return MoveQuality.GOOD
        if effective_loss <= self.inaccuracy_max_loss:
            return MoveQuality.INACCURACY
        if effective_loss <= self.mistake_max_loss:
            return MoveQuality.MISTAKE
        return MoveQuality.BLUNDER


@dataclass(frozen=True, slots=True)
class AnalysisEvidence:
    score_heuristic_points: int
    forced_outcome: str | None
    forced_outcome_source: str | None
    requested_depth: int
    completed_depth: int
    exact_width: bool
    timed_out: bool
    work_limit_reached: bool
    engine_version: str
    source_fingerprint: str
    engine_profile_id: str
    max_series_per_node: int | None
    time_limit_seconds: float | None
    max_generation_positions: int | None
    required_prefix: tuple[str, ...]
    candidate_moves: tuple[str, ...] | None
    adjudication_status: str | None

    @classmethod
    def from_search_result(cls, result: SearchResult) -> AnalysisEvidence:
        forced_outcome: str | None = None
        forced_source: str | None = None
        if result.proof in {"white", "black", "draw"}:
            forced_outcome = result.proof
            forced_source = "proof"
        elif result.adjudication_status == "proven-draw-no-mating-material":
            forced_outcome = "draw"
            forced_source = "adjudication"
        elif (
            result.exact_width
            and not result.timed_out
            and result.completed_depth == result.requested_depth
            and abs(result.score) >= MATE_SCORE - 10_000
        ):
            forced_outcome = "white" if result.score > 0 else "black"
            forced_source = "mate-score"

        return cls(
            score_heuristic_points=result.score,
            forced_outcome=forced_outcome,
            forced_outcome_source=forced_source,
            requested_depth=result.requested_depth,
            completed_depth=result.completed_depth,
            exact_width=result.exact_width,
            timed_out=result.timed_out,
            work_limit_reached=result.work_limit_reached,
            engine_version=result.engine_version,
            source_fingerprint=result.source_fingerprint,
            engine_profile_id=result.engine_profile_id,
            max_series_per_node=result.max_series_per_node,
            time_limit_seconds=result.time_limit_seconds,
            max_generation_positions=result.max_generation_positions,
            required_prefix=result.required_prefix,
            candidate_moves=(
                None if result.best_series is None else result.best_series.moves
            ),
            adjudication_status=result.adjudication_status,
        )

    @property
    def search_limits(self) -> tuple[int, int | None, float | None, int | None]:
        return (
            self.requested_depth,
            self.max_series_per_node,
            self.time_limit_seconds,
            self.max_generation_positions,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "score_heuristic_points": self.score_heuristic_points,
            "forced_outcome": self.forced_outcome,
            "forced_outcome_source": self.forced_outcome_source,
            "requested_depth": self.requested_depth,
            "completed_depth": self.completed_depth,
            "exact_width": self.exact_width,
            "timed_out": self.timed_out,
            "work_limit_reached": self.work_limit_reached,
            "engine_version": self.engine_version,
            "source_fingerprint": self.source_fingerprint,
            "engine_profile_id": self.engine_profile_id,
            "search_limits": {
                "max_series_per_node": self.max_series_per_node,
                "time_limit_seconds": self.time_limit_seconds,
                "max_generation_positions": self.max_generation_positions,
            },
            "required_prefix": list(self.required_prefix),
            "candidate_moves": (
                None if self.candidate_moves is None else list(self.candidate_moves)
            ),
            "adjudication_status": self.adjudication_status,
        }


@dataclass(frozen=True, slots=True)
class MoveQualityVerdict:
    label: MoveQuality
    subject: QualitySubject
    mover: str
    played_prefix: tuple[str, ...]
    policy: QualityPolicy
    reasons: tuple[str, ...]
    parent: AnalysisEvidence
    fixed_prefix: AnalysisEvidence
    loss_heuristic_points: int | None = None
    effective_loss_heuristic_points: int | None = None
    outcome_swing: str | None = None

    @property
    def rated(self) -> bool:
        return self.label != MoveQuality.NOT_RATED

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": QUALITY_SCHEMA_VERSION,
            "label": self.label.value,
            "rated": self.rated,
            "subject": self.subject.value,
            "mover": self.mover,
            "played_prefix": list(self.played_prefix),
            "score": {
                "unit": "heuristic-points",
                "is_centipawns": False,
                "parent_best": self.parent.score_heuristic_points,
                "fixed_prefix": self.fixed_prefix.score_heuristic_points,
                "loss": self.loss_heuristic_points,
                "effective_loss": self.effective_loss_heuristic_points,
            },
            "outcome": {
                "parent": self.parent.forced_outcome,
                "fixed_prefix": self.fixed_prefix.forced_outcome,
                "swing": self.outcome_swing,
            },
            "reasons": list(self.reasons),
            "policy": self.policy.as_dict(),
            "evidence": {
                "comparable": self.rated,
                "parent": self.parent.as_dict(),
                "fixed_prefix": self.fixed_prefix.as_dict(),
            },
        }

    def as_calibration_sample(self) -> dict[str, Any]:
        """Returns stable raw fields for later self-play outcome calibration."""

        return {
            "schema_version": QUALITY_SCHEMA_VERSION,
            "policy_id": self.policy.policy_id,
            "policy_version": self.policy.version,
            "provisional_custom": True,
            "label": self.label.value,
            "rated": self.rated,
            "subject": self.subject.value,
            "mover": self.mover,
            "played_prefix": list(self.played_prefix),
            "score_unit": "heuristic-points",
            "score_is_centipawns": False,
            "parent_best_score": self.parent.score_heuristic_points,
            "fixed_prefix_score": self.fixed_prefix.score_heuristic_points,
            "loss": self.loss_heuristic_points,
            "outcome_swing": self.outcome_swing,
            "parent_forced_outcome": self.parent.forced_outcome,
            "fixed_prefix_forced_outcome": self.fixed_prefix.forced_outcome,
            "engine_profile_id": self.parent.engine_profile_id,
            "source_fingerprint": self.parent.source_fingerprint,
            "requested_depth": self.parent.requested_depth,
            "exact_width": self.parent.exact_width,
            "reasons": list(self.reasons),
        }


def _evidence_reasons(
    parent: AnalysisEvidence,
    candidate: AnalysisEvidence,
    played_prefix: tuple[str, ...],
    subject: QualitySubject,
    policy: QualityPolicy,
) -> tuple[str, ...]:
    reasons: list[str] = []

    expected_parent_prefix = (
        played_prefix[:-1] if subject == QualitySubject.MICRO_MOVE else ()
    )

    if not played_prefix:
        reasons.append("missing-played-prefix")
    if parent.candidate_moves is None:
        reasons.append("missing-parent-best")
    if candidate.candidate_moves is None:
        reasons.append("missing-candidate")
    if parent.required_prefix != expected_parent_prefix:
        reasons.append("parent-prefix-mismatch")
    if (
        parent.candidate_moves is not None
        and parent.candidate_moves[: len(expected_parent_prefix)]
        != expected_parent_prefix
    ):
        reasons.append("missing-parent-best")
    if candidate.required_prefix != played_prefix:
        reasons.append("fixed-prefix-mismatch")
    if (
        candidate.candidate_moves is not None
        and candidate.candidate_moves[: len(played_prefix)] != played_prefix
    ):
        reasons.append("missing-candidate")

    for evidence in (parent, candidate):
        if evidence.timed_out and "timed-out" not in reasons:
            reasons.append("timed-out")
        if evidence.completed_depth != evidence.requested_depth:
            if "incomplete-requested-depth" not in reasons:
                reasons.append("incomplete-requested-depth")
        if evidence.completed_depth < policy.minimum_depth_series:
            if "shallow-evidence" not in reasons:
                reasons.append("shallow-evidence")
        if evidence.work_limit_reached and "work-limit-reached" not in reasons:
            reasons.append("work-limit-reached")
        if evidence.adjudication_status == "manual-proof-required":
            if "adjudication-pending" not in reasons:
                reasons.append("adjudication-pending")
        if not evidence.engine_profile_id and "missing-engine-profile" not in reasons:
            reasons.append("missing-engine-profile")
        if not evidence.source_fingerprint and "missing-source-fingerprint" not in reasons:
            reasons.append("missing-source-fingerprint")
        if not evidence.exact_width and not policy.allow_selective_evidence:
            if "selective-evidence" not in reasons:
                reasons.append("selective-evidence")

    if parent.engine_profile_id != candidate.engine_profile_id:
        reasons.append("engine-profile-mismatch")
    if parent.source_fingerprint != candidate.source_fingerprint:
        reasons.append("source-fingerprint-mismatch")
    if parent.engine_version != candidate.engine_version:
        reasons.append("engine-version-mismatch")
    if parent.search_limits != candidate.search_limits:
        reasons.append("search-limits-mismatch")

    return tuple(reasons)


def _perspective_outcome(outcome: str | None, mover: chess.Color) -> str:
    if outcome is None:
        return "unresolved"
    if outcome == "draw":
        return "draw"
    mover_won = outcome == ("white" if mover == chess.WHITE else "black")
    return "win" if mover_won else "loss"


def _outcome_swing(
    parent: AnalysisEvidence,
    candidate: AnalysisEvidence,
    mover: chess.Color,
) -> str | None:
    before = _perspective_outcome(parent.forced_outcome, mover)
    after = _perspective_outcome(candidate.forced_outcome, mover)
    if before == after == "unresolved":
        return None
    if before == after:
        return f"{before}-to-{after}"
    return f"{before}-to-{after}"


def _decisive_downgrade(swing: str | None) -> bool:
    return swing in {
        "win-to-draw",
        "win-to-loss",
        "draw-to-loss",
        "win-to-unresolved",
        "unresolved-to-loss",
    }


def grade_move_quality(
    parent_best: SearchResult,
    fixed_prefix: SearchResult,
    *,
    mover: chess.Color,
    played_prefix: tuple[str, ...],
    subject: QualitySubject,
    policy: QualityPolicy | None = None,
) -> MoveQualityVerdict:
    """Grades a played prefix using comparable White-centric search scores.

    ``fixed_prefix`` must be a second search from the same series-boundary
    position and with the same limits, constrained by ``played_prefix``.  For
    a micro-move, ``parent_best`` is constrained by every previously played
    micro-move (``played_prefix[:-1]``), so the score loss is genuinely
    marginal to the final move.  For a completed series, ``parent_best`` must
    be unconstrained and the entire played series is the candidate prefix.
    """

    if mover not in (chess.WHITE, chess.BLACK):
        raise ValueError("mover must be chess.WHITE or chess.BLACK")
    if not isinstance(subject, QualitySubject):
        subject = QualitySubject(subject)
    policy = policy or QualityPolicy()
    played_prefix = tuple(played_prefix)
    parent = AnalysisEvidence.from_search_result(parent_best)
    candidate = AnalysisEvidence.from_search_result(fixed_prefix)
    reasons = _evidence_reasons(
        parent, candidate, played_prefix, subject, policy
    )
    mover_name = "white" if mover == chess.WHITE else "black"
    swing = _outcome_swing(parent, candidate, mover)

    if reasons:
        return MoveQualityVerdict(
            label=MoveQuality.NOT_RATED,
            subject=subject,
            mover=mover_name,
            played_prefix=played_prefix,
            policy=policy,
            reasons=reasons,
            parent=parent,
            fixed_prefix=candidate,
            outcome_swing=swing,
        )

    raw_loss = (
        parent.score_heuristic_points - candidate.score_heuristic_points
        if mover == chess.WHITE
        else candidate.score_heuristic_points - parent.score_heuristic_points
    )
    effective_loss = max(0, raw_loss)
    label = (
        MoveQuality.BLUNDER
        if _decisive_downgrade(swing)
        else policy.classify_loss(effective_loss)
    )
    return MoveQualityVerdict(
        label=label,
        subject=subject,
        mover=mover_name,
        played_prefix=played_prefix,
        policy=policy,
        reasons=(),
        parent=parent,
        fixed_prefix=candidate,
        loss_heuristic_points=raw_loss,
        effective_loss_heuristic_points=effective_loss,
        outcome_swing=swing,
    )
