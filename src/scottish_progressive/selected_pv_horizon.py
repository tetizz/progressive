from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import json
from typing import Callable

import chess

from .model import Outcome, ProgressiveState, SeriesResult
from .rules import play_series
from .series_mate import SeriesMateProbe, SeriesMateStatus


BROWSER_CHECKED_PV_SELECTION_POLICY = (
    "repair-once-then-veto-adverse-checked-pv-mates-v1"
)
SELECTED_PV_SELECTION_POLICY = (
    "repair-once-then-veto-adverse-selected-pv-mates-v1"
)
RETAINED_ROOT_HORIZON_PROOF_SCHEMA = "spc-retained-root-horizon-proof-v1"
SAME_ROOT_HORIZON_REPAIR_POLICY_SCHEMA = (
    "spc-same-root-horizon-repair-policy-v1"
)
MAX_SAME_ROOT_HORIZON_REPAIRS = 1
MAX_RETAINED_ROOT_HORIZON_PROOFS = 16
MAX_RETAINED_ROOT_HORIZON_PROOF_PATH = 8


class SelectedPvHorizonStatus(StrEnum):
    NOT_APPLICABLE = "not-applicable"
    FOUND = "found"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"


class HorizonPolicyAction(StrEnum):
    REPAIR = "repair"
    VETO = "veto"
    UNKNOWN = "unknown"


def _state_payload(state: ProgressiveState) -> dict[str, object]:
    board = state.board
    return {
        "fen": board.fen(en_passant="fen"),
        "series_number": state.series_number,
        "quiet_series": state.quiet_series,
        "ep_targets": [chess.square_name(square) for square in state.ep_targets],
        "promoted_hex": f"{board.promoted:016x}",
        "chess960": board.chess960,
    }


def _series_payload(series: SeriesResult) -> dict[str, object]:
    return {
        "moves": list(series.moves),
        "san": list(series.san),
        "final_state": _state_payload(series.final_state),
        "ended_by_check": series.ended_by_check,
        "outcome": None if series.outcome is None else series.outcome.value,
        "unused_moves": series.unused_moves,
        "transposition_count": series.transposition_count,
    }


def _same_series(left: SeriesResult, right: SeriesResult) -> bool:
    return _series_payload(left) == _series_payload(right)


def _proof_identity(
    rooted_path: tuple[SeriesResult, ...],
    mate_reply: SeriesResult,
) -> str:
    payload = {
        "schema": RETAINED_ROOT_HORIZON_PROOF_SCHEMA,
        "rooted_path": [_series_payload(series) for series in rooted_path],
        "mate_reply": _series_payload(mate_reply),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class SelectedPvHorizonProof:
    rooted_path: tuple[SeriesResult, ...]
    mate_reply: SeriesResult
    proof_bounds: tuple[int, int]
    identity_sha256: str
    schema: str = RETAINED_ROOT_HORIZON_PROOF_SCHEMA

    @classmethod
    def create(
        cls,
        rooted_path: tuple[SeriesResult, ...],
        mate_reply: SeriesResult,
    ) -> SelectedPvHorizonProof:
        # A completed series flips the board turn. Therefore the mating side is
        # the opposite of the final state's side to move.
        mating_side = not mate_reply.final_state.board.turn
        proof_value = 1 if mating_side == chess.WHITE else -1
        return cls(
            rooted_path,
            mate_reply,
            (proof_value, proof_value),
            _proof_identity(rooted_path, mate_reply),
        )

    def recomputed_identity_sha256(self) -> str:
        return _proof_identity(self.rooted_path, self.mate_reply)


@dataclass(frozen=True, slots=True)
class SelectedPvHorizonCertification:
    status: SelectedPvHorizonStatus
    replay_verified: bool
    probe_status: SeriesMateStatus | None
    proof: SelectedPvHorizonProof | None = None
    message: str = ""
    work_used: int = 0

    @property
    def safe(self) -> bool:
        return self.status in {
            SelectedPvHorizonStatus.NOT_APPLICABLE,
            SelectedPvHorizonStatus.EXHAUSTED,
        }


@dataclass(frozen=True, slots=True)
class CandidateHorizonState:
    candidate_series: str
    retained_proofs: tuple[SelectedPvHorizonProof, ...] = ()
    successful_repairs: int = 0

    def __post_init__(self) -> None:
        if not self.candidate_series:
            raise ValueError("candidate series must be nonempty")
        if not 0 <= self.successful_repairs <= MAX_SAME_ROOT_HORIZON_REPAIRS:
            raise ValueError("successful horizon repair count is invalid")
        identities = tuple(proof.identity_sha256 for proof in self.retained_proofs)
        if len(identities) != len(set(identities)):
            raise ValueError("retained horizon proofs must be distinct")

    def record_successful_repair(self) -> CandidateHorizonState:
        if not self.retained_proofs:
            raise ValueError("a successful repair requires a retained proof")
        if self.successful_repairs >= MAX_SAME_ROOT_HORIZON_REPAIRS:
            raise ValueError("same-root horizon repair limit reached")
        return replace(self, successful_repairs=self.successful_repairs + 1)


@dataclass(frozen=True, slots=True)
class HorizonPolicyDecision:
    action: HorizonPolicyAction
    reason: str
    next_state: CandidateHorizonState
    distinct_proofs_observed: int
    retained_proofs_before_veto: int


def _replay_selected_path(
    root: ProgressiveState,
    selected_pv: tuple[SeriesResult, ...],
) -> tuple[SeriesResult, ...] | None:
    if (
        type(selected_pv) is not tuple
        or not selected_pv
        or len(selected_pv) > MAX_RETAINED_ROOT_HORIZON_PROOF_PATH
        or any(type(series) is not SeriesResult for series in selected_pv)
    ):
        return None
    cursor = root
    replayed_path: list[SeriesResult] = []
    for supplied in selected_pv:
        try:
            replayed = play_series(cursor, supplied.moves).with_transposition_count(
                supplied.transposition_count
            )
        except Exception:
            return None
        if not _same_series(replayed, supplied):
            return None
        replayed_path.append(replayed)
        cursor = replayed.final_state
    return tuple(replayed_path)


def certify_selected_pv_horizon(
    root: ProgressiveState,
    selected_pv: tuple[SeriesResult, ...],
    probe: Callable[[ProgressiveState], SeriesMateProbe],
) -> SelectedPvHorizonCertification:
    """Certifies the adverse one-series horizon after one selected canonical PV.

    Every supplied series is replayed through the rules oracle. A terminal leaf
    or a leaf where the root mover acts next needs no adverse-mate probe. For an
    opponent-to-move leaf, only exact native ``EXHAUSTED`` is safe. ``FOUND``
    carries a second replayed mate proof into same-root repair; every resource
    or compatibility stop remains UNKNOWN and cannot certify the selection.
    """

    replayed_path = _replay_selected_path(root, selected_pv)
    if replayed_path is None:
        return SelectedPvHorizonCertification(
            SelectedPvHorizonStatus.UNKNOWN,
            False,
            None,
            message="selected PV failed authoritative replay",
        )
    leaf_series = replayed_path[-1]
    if leaf_series.outcome is not None:
        return SelectedPvHorizonCertification(
            SelectedPvHorizonStatus.NOT_APPLICABLE,
            True,
            None,
            message="selected PV is already terminal",
        )
    leaf = leaf_series.final_state
    if leaf.board.turn == root.board.turn:
        return SelectedPvHorizonCertification(
            SelectedPvHorizonStatus.NOT_APPLICABLE,
            True,
            None,
            message="next-series mate would favor the root mover",
        )

    result = probe(leaf)
    if type(result) is not SeriesMateProbe:
        return SelectedPvHorizonCertification(
            SelectedPvHorizonStatus.UNKNOWN,
            True,
            None,
            message="mate probe returned an invalid result",
        )
    work_used = result.positions_visited + result.moves_generated
    if result.status is SeriesMateStatus.EXHAUSTED and result.series is None:
        return SelectedPvHorizonCertification(
            SelectedPvHorizonStatus.EXHAUSTED,
            True,
            result.status,
            message=result.message,
            work_used=work_used,
        )
    if result.status is SeriesMateStatus.FOUND and result.series is not None:
        try:
            replayed_mate = play_series(
                leaf,
                result.series.moves,
            ).with_transposition_count(result.series.transposition_count)
        except Exception:
            replayed_mate = None
        if (
            replayed_mate is not None
            and _same_series(replayed_mate, result.series)
            and replayed_mate.outcome is Outcome.CHECKMATE
            and replayed_mate.ended_by_check
        ):
            proof = SelectedPvHorizonProof.create(replayed_path, replayed_mate)
            return SelectedPvHorizonCertification(
                SelectedPvHorizonStatus.FOUND,
                True,
                result.status,
                proof=proof,
                message=result.message,
                work_used=work_used,
            )
    return SelectedPvHorizonCertification(
        SelectedPvHorizonStatus.UNKNOWN,
        True,
        result.status,
        message=result.message,
        work_used=work_used,
    )


def observe_horizon_proof(
    state: CandidateHorizonState,
    proof: SelectedPvHorizonProof,
) -> HorizonPolicyDecision:
    """Applies the browser-compatible repair-once/then-veto policy."""

    if (
        not proof.rooted_path
        or proof.rooted_path[0].machine_notation != state.candidate_series
        or proof.identity_sha256 != proof.recomputed_identity_sha256()
    ):
        return HorizonPolicyDecision(
            HorizonPolicyAction.UNKNOWN,
            "invalid-horizon-proof",
            state,
            len(state.retained_proofs),
            len(state.retained_proofs),
        )
    if any(
        retained.identity_sha256 == proof.identity_sha256
        for retained in state.retained_proofs
    ):
        return HorizonPolicyDecision(
            HorizonPolicyAction.VETO,
            "duplicate-horizon-proof",
            state,
            len(state.retained_proofs),
            len(state.retained_proofs),
        )
    distinct = len(state.retained_proofs) + 1
    if len(state.retained_proofs) >= MAX_RETAINED_ROOT_HORIZON_PROOFS:
        return HorizonPolicyDecision(
            HorizonPolicyAction.VETO,
            "retained-proof-capacity",
            state,
            distinct,
            len(state.retained_proofs),
        )
    if state.successful_repairs >= MAX_SAME_ROOT_HORIZON_REPAIRS:
        return HorizonPolicyDecision(
            HorizonPolicyAction.VETO,
            "same-root-repair-limit",
            state,
            distinct,
            len(state.retained_proofs),
        )
    retained = replace(
        state,
        retained_proofs=state.retained_proofs + (proof,),
    )
    return HorizonPolicyDecision(
        HorizonPolicyAction.REPAIR,
        "adverse-immediate-series-mate",
        retained,
        distinct,
        len(state.retained_proofs),
    )
