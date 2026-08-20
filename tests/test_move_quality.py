from __future__ import annotations

from dataclasses import replace
import json

import chess
import pytest

from scottish_progressive.evaluation import EvaluationBreakdown
from scottish_progressive.model import ProgressiveState, SeriesResult
from scottish_progressive.move_quality import (
    MoveQuality,
    QualityPolicy,
    QualitySubject,
    grade_move_quality,
)
from scottish_progressive.search import MATE_SCORE, SearchResult, SearchStats


def _breakdown(score: int = 0) -> EvaluationBreakdown:
    return EvaluationBreakdown(
        total=score,
        material=0,
        king_space=0,
        series_reach=0,
        promotion_corridors=0,
        immediate_vulnerability=0,
        useful_mobility=0,
        boundary_check=0,
        white_check_distance=None,
        black_check_distance=None,
        reach_complete=True,
    )


def _series(moves: tuple[str, ...]) -> SeriesResult:
    return SeriesResult(
        moves=moves,
        san=moves,
        final_state=ProgressiveState.initial(),
    )


def _result(
    score: int,
    *,
    moves: tuple[str, ...] | None = ("e2e4",),
    required_prefix: tuple[str, ...] = (),
    requested_depth: int = 2,
    completed_depth: int | None = None,
    exact_width: bool = True,
    timed_out: bool = False,
    work_limit_reached: bool = False,
    proof: str | None = None,
    adjudication_status: str | None = None,
    profile_id: str = "spc-test-profile",
    source_fingerprint: str = "0123456789abcdef",
    engine_version: str = "spc-test-v1",
    branch_cap: int | None = 64,
    time_limit_seconds: float | None = 30.0,
    generation_limit: int | None = 50_000,
) -> SearchResult:
    candidate = None if moves is None else _series(moves)
    return SearchResult(
        score=score,
        best_series=candidate,
        principal_variation=(() if candidate is None else (candidate,)),
        alternatives=(),
        requested_depth=requested_depth,
        completed_depth=(
            requested_depth if completed_depth is None else completed_depth
        ),
        exact_width=exact_width,
        timed_out=timed_out,
        elapsed_seconds=0.1,
        stats=SearchStats(),
        root_evaluation=_breakdown(score),
        proof=proof,
        adjudication_status=adjudication_status,
        max_series_per_node=branch_cap,
        time_limit_seconds=time_limit_seconds,
        engine_version=engine_version,
        source_fingerprint=source_fingerprint,
        engine_profile_id=profile_id,
        engine_profile_name="Test profile",
        required_prefix=required_prefix,
        work_limit_reached=work_limit_reached,
        max_generation_positions=generation_limit,
    )


def _grade(
    parent: SearchResult,
    candidate: SearchResult,
    *,
    mover: chess.Color = chess.WHITE,
    played_prefix: tuple[str, ...] = ("e2e4",),
    subject: QualitySubject = QualitySubject.MICRO_MOVE,
    policy: QualityPolicy | None = None,
):
    return grade_move_quality(
        parent,
        candidate,
        mover=mover,
        played_prefix=played_prefix,
        subject=subject,
        policy=policy,
    )


@pytest.mark.parametrize(
    ("candidate_score", "expected"),
    [
        (1_000, MoveQuality.BEST),
        (980, MoveQuality.EXCELLENT),
        (930, MoveQuality.GOOD),
        (850, MoveQuality.INACCURACY),
        (600, MoveQuality.MISTAKE),
        (300, MoveQuality.BLUNDER),
    ],
)
def test_white_quality_uses_provisional_heuristic_loss_bands(
    candidate_score: int, expected: MoveQuality
) -> None:
    verdict = _grade(
        _result(1_000, moves=("d2d4",)),
        _result(
            candidate_score,
            moves=("e2e4",),
            required_prefix=("e2e4",),
        ),
    )

    assert verdict.label == expected
    assert verdict.loss_heuristic_points == 1_000 - candidate_score


def test_black_loss_direction_is_inverted_for_white_centric_scores() -> None:
    verdict = _grade(
        _result(-1_000, moves=("e7e5",)),
        _result(-850, moves=("c7c5",), required_prefix=("c7c5",)),
        mover=chess.BLACK,
        played_prefix=("c7c5",),
    )

    assert verdict.label == MoveQuality.INACCURACY
    assert verdict.loss_heuristic_points == 150
    assert verdict.mover == "black"


def test_second_micro_move_is_graded_against_prior_prefix_best() -> None:
    played = ("e7e5", "g8f6")
    verdict = _grade(
        _result(
            -400,
            moves=("e7e5", "d7d5"),
            required_prefix=("e7e5",),
        ),
        _result(-300, moves=played, required_prefix=played),
        mover=chess.BLACK,
        played_prefix=played,
    )

    assert verdict.label == MoveQuality.GOOD
    assert verdict.loss_heuristic_points == 100
    assert verdict.parent.required_prefix == ("e7e5",)
    assert verdict.fixed_prefix.required_prefix == played


def test_third_micro_move_is_graded_against_first_two_moves() -> None:
    prior = ("e2e4", "g1f3")
    played = prior + ("f1c4",)
    verdict = _grade(
        _result(600, moves=prior + ("d2d4",), required_prefix=prior),
        _result(350, moves=played, required_prefix=played),
        played_prefix=played,
    )

    assert verdict.label == MoveQuality.INACCURACY
    assert verdict.loss_heuristic_points == 250
    assert verdict.parent.required_prefix == prior


def test_later_micro_candidate_must_include_the_newly_played_move() -> None:
    prior = ("e7e5",)
    played = prior + ("g8f6",)
    verdict = _grade(
        _result(-400, moves=prior + ("d7d5",), required_prefix=prior),
        _result(-300, moves=played, required_prefix=prior),
        mover=chess.BLACK,
        played_prefix=played,
    )

    assert verdict.label == MoveQuality.NOT_RATED
    assert "fixed-prefix-mismatch" in verdict.reasons


def test_candidate_that_scores_better_than_parent_best_is_clamped_to_best() -> None:
    verdict = _grade(
        _result(100, moves=("d2d4",)),
        _result(120, moves=("e2e4",), required_prefix=("e2e4",)),
    )

    assert verdict.label == MoveQuality.BEST
    assert verdict.loss_heuristic_points == -20
    assert verdict.effective_loss_heuristic_points == 0


@pytest.mark.parametrize(
    ("parent_proof", "candidate_proof", "mover", "expected_swing"),
    [
        ("white", "draw", chess.WHITE, "win-to-draw"),
        ("white", "black", chess.WHITE, "win-to-loss"),
        ("draw", "black", chess.WHITE, "draw-to-loss"),
        ("black", "draw", chess.BLACK, "win-to-draw"),
        ("draw", "white", chess.BLACK, "draw-to-loss"),
    ],
)
def test_decisive_proof_downgrades_are_blunders(
    parent_proof: str,
    candidate_proof: str,
    mover: chess.Color,
    expected_swing: str,
) -> None:
    parent_score = {
        "white": MATE_SCORE - 3,
        "black": -MATE_SCORE + 3,
        "draw": 0,
    }[parent_proof]
    candidate_score = {
        "white": MATE_SCORE - 4,
        "black": -MATE_SCORE + 4,
        "draw": 0,
    }[candidate_proof]
    prefix = ("e2e4",) if mover == chess.WHITE else ("e7e5",)
    verdict = _grade(
        _result(parent_score, moves=(("d2d4",) if mover else ("d7d5",)), proof=parent_proof),
        _result(
            candidate_score,
            moves=prefix,
            required_prefix=prefix,
            proof=candidate_proof,
        ),
        mover=mover,
        played_prefix=prefix,
    )

    assert verdict.label == MoveQuality.BLUNDER
    assert verdict.outcome_swing == expected_swing


def test_mate_score_without_proof_is_recorded_as_decisive_evidence() -> None:
    verdict = _grade(
        _result(MATE_SCORE - 2, moves=("d2d4",)),
        _result(0, moves=("e2e4",), required_prefix=("e2e4",), proof="draw"),
    )

    assert verdict.label == MoveQuality.BLUNDER
    assert verdict.outcome_swing == "win-to-draw"
    assert verdict.parent.forced_outcome_source == "mate-score"


def test_same_forced_result_still_uses_mate_distance_loss() -> None:
    verdict = _grade(
        _result(MATE_SCORE - 2, moves=("d2d4",), proof="white"),
        _result(
            MATE_SCORE - 22,
            moves=("e2e4",),
            required_prefix=("e2e4",),
            proof="white",
        ),
    )

    assert verdict.label == MoveQuality.EXCELLENT
    assert verdict.loss_heuristic_points == 20
    assert verdict.outcome_swing == "win-to-win"


@pytest.mark.parametrize(
    ("mutate_candidate", "reason"),
    [
        (lambda item: replace(item, timed_out=True), "timed-out"),
        (
            lambda item: replace(item, completed_depth=1),
            "incomplete-requested-depth",
        ),
        (
            lambda item: replace(item, requested_depth=1, completed_depth=1),
            "shallow-evidence",
        ),
        (
            lambda item: replace(item, engine_profile_id="different-profile"),
            "engine-profile-mismatch",
        ),
        (
            lambda item: replace(item, source_fingerprint="fedcba9876543210"),
            "source-fingerprint-mismatch",
        ),
        (
            lambda item: replace(item, max_series_per_node=32),
            "search-limits-mismatch",
        ),
        (
            lambda item: replace(item, time_limit_seconds=60.0),
            "search-limits-mismatch",
        ),
        (
            lambda item: replace(item, max_generation_positions=25_000),
            "search-limits-mismatch",
        ),
        (lambda item: replace(item, best_series=None), "missing-candidate"),
        (lambda item: replace(item, exact_width=False), "selective-evidence"),
        (
            lambda item: replace(item, work_limit_reached=True),
            "work-limit-reached",
        ),
        (
            lambda item: replace(
                item, adjudication_status="manual-proof-required"
            ),
            "adjudication-pending",
        ),
    ],
)
def test_insufficient_or_unmatched_evidence_is_not_rated(
    mutate_candidate, reason: str
) -> None:
    parent = _result(100, moves=("d2d4",))
    candidate = _result(
        90, moves=("e2e4",), required_prefix=("e2e4",)
    )

    verdict = _grade(parent, mutate_candidate(candidate))

    assert verdict.label == MoveQuality.NOT_RATED
    assert verdict.rated is False
    assert reason in verdict.reasons
    assert verdict.loss_heuristic_points is None


def test_selective_evidence_requires_explicit_policy_permission() -> None:
    parent = _result(100, moves=("d2d4",), exact_width=False)
    candidate = _result(
        90,
        moves=("e2e4",),
        required_prefix=("e2e4",),
        exact_width=False,
    )
    policy = QualityPolicy(allow_selective_evidence=True)

    verdict = _grade(parent, candidate, policy=policy)

    assert verdict.label == MoveQuality.EXCELLENT
    assert verdict.rated is True
    assert verdict.parent.exact_width is False


@pytest.mark.parametrize(
    ("played_prefix", "candidate_prefix", "candidate_moves", "reason"),
    [
        ((), (), ("e2e4",), "missing-played-prefix"),
        (("e2e4",), ("d2d4",), ("d2d4",), "fixed-prefix-mismatch"),
        (("e2e4",), ("e2e4",), ("d2d4",), "missing-candidate"),
    ],
)
def test_candidate_must_match_the_played_fixed_prefix(
    played_prefix: tuple[str, ...],
    candidate_prefix: tuple[str, ...],
    candidate_moves: tuple[str, ...],
    reason: str,
) -> None:
    verdict = _grade(
        _result(100, moves=("d2d4",)),
        _result(90, moves=candidate_moves, required_prefix=candidate_prefix),
        played_prefix=played_prefix,
    )

    assert verdict.label == MoveQuality.NOT_RATED
    assert reason in verdict.reasons


@pytest.mark.parametrize(
    ("subject", "played", "parent_prefix", "parent_moves", "reason"),
    [
        (
            QualitySubject.MICRO_MOVE,
            ("e7e5", "g8f6"),
            (),
            ("e7e5", "d7d5"),
            "parent-prefix-mismatch",
        ),
        (
            QualitySubject.MICRO_MOVE,
            ("e2e4", "g1f3", "f1c4"),
            ("e2e4", "b1c3"),
            ("e2e4", "b1c3", "d2d4"),
            "parent-prefix-mismatch",
        ),
        (
            QualitySubject.MICRO_MOVE,
            ("e2e4", "g1f3", "f1c4"),
            ("e2e4", "g1f3"),
            ("d2d4", "g1f3", "c1f4"),
            "missing-parent-best",
        ),
        (
            QualitySubject.SERIES,
            ("e7e5", "g8f6"),
            ("e7e5",),
            ("e7e5", "d7d5"),
            "parent-prefix-mismatch",
        ),
    ],
)
def test_parent_constraint_must_match_subject_and_prior_micro_prefix(
    subject: QualitySubject,
    played: tuple[str, ...],
    parent_prefix: tuple[str, ...],
    parent_moves: tuple[str, ...],
    reason: str,
) -> None:
    verdict = _grade(
        _result(100, moves=parent_moves, required_prefix=parent_prefix),
        _result(90, moves=played, required_prefix=played),
        mover=chess.BLACK if played[0][1] == "7" else chess.WHITE,
        played_prefix=played,
        subject=subject,
    )

    assert verdict.label == MoveQuality.NOT_RATED
    assert reason in verdict.reasons


def test_full_series_subject_preserves_every_played_micro_move() -> None:
    moves = ("e7e5", "g8f6")
    verdict = _grade(
        _result(-200, moves=("d7d5", "c8f5")),
        _result(-150, moves=moves, required_prefix=moves),
        mover=chess.BLACK,
        played_prefix=moves,
        subject=QualitySubject.SERIES,
    )

    assert verdict.label == MoveQuality.GOOD
    assert verdict.subject == QualitySubject.SERIES
    assert verdict.played_prefix == moves


def test_policy_is_versioned_provisional_custom_and_round_trips() -> None:
    policy = QualityPolicy(
        version=2,
        name="Self-play experiment policy 2",
        minimum_depth_series=3,
        good_max_loss=110,
    )
    payload = policy.as_dict()

    restored = QualityPolicy.from_dict(payload)

    assert restored == policy
    assert payload["version"] == 2
    assert payload["score_unit"] == "heuristic-points"
    assert payload["score_is_centipawns"] is False
    assert payload["calibration_status"] == "provisional-custom-uncalibrated"
    assert payload["policy_id"] == restored.policy_id


def test_tampered_serialized_policy_id_is_rejected() -> None:
    payload = QualityPolicy().as_dict()
    payload["policy_id"] = "spc-quality-tampered"

    with pytest.raises(ValueError, match="policy_id"):
        QualityPolicy.from_dict(payload)


def test_verdict_serialization_is_json_safe_and_calibration_ready() -> None:
    verdict = _grade(
        _result(500, moves=("d2d4",)),
        _result(430, moves=("e2e4",), required_prefix=("e2e4",)),
    )

    payload = verdict.as_dict()
    sample = verdict.as_calibration_sample()

    json.dumps(payload)
    json.dumps(sample)
    assert payload["label"] == "Good"
    assert payload["score"]["unit"] == "heuristic-points"
    assert payload["score"]["is_centipawns"] is False
    assert payload["evidence"]["comparable"] is True
    assert sample["provisional_custom"] is True
    assert sample["loss"] == 70
    assert sample["policy_id"] == QualityPolicy().policy_id
