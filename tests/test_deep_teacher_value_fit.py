from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.fit_deep_teacher_value import (
    DEFAULT_ADVERSE_PAIR_WEIGHT,
    MATE_SCORE,
    QUARANTINED_HOLDOUT_CORPORA,
    TeacherLabel,
    TeacherOption,
    _evaluate_holdout_command,
    _fit_command,
    _folds,
    _exclusive_json,
    _label_semantic_keys,
    _linear_scorer,
    _metric_objective,
    _metrics,
    _model_payload,
    _pairwise_rows,
    _terminal_score,
    _validate_adverse_pair_weight,
)


def _option(
    *,
    outcome: str | None,
    ended_by_check: bool,
    signed_mate_distance: int | None,
    feature: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        outcome=outcome,
        ended_by_check=ended_by_check,
        signed_mate_distance=signed_mate_distance,
        features=(feature,) * 47,
    )


def test_terminal_override_is_color_correct_and_dominates_linear_features() -> None:
    white = SimpleNamespace(mover_sign=1)
    black = SimpleNamespace(mover_sign=-1)
    white_mate = _option(
        outcome="checkmate",
        ended_by_check=True,
        signed_mate_distance=2,
        feature=-(10**12),
    )
    black_mate = _option(
        outcome="checkmate",
        ended_by_check=True,
        signed_mate_distance=-2,
        feature=10**12,
    )
    quiet = _option(
        outcome=None,
        ended_by_check=False,
        signed_mate_distance=None,
        feature=10**12,
    )
    draw = _option(
        outcome="stalemate",
        ended_by_check=False,
        signed_mate_distance=None,
        feature=10**12,
    )
    scorer = _linear_scorer((1,) * 7, "base7")

    assert _terminal_score(white, white_mate) == MATE_SCORE - 2
    assert _terminal_score(black, black_mate) == -(MATE_SCORE - 2)
    assert _terminal_score(white, draw) == 0
    assert white.mover_sign * scorer(white, white_mate) > white.mover_sign * scorer(
        white, quiet
    )
    assert black.mover_sign * scorer(black, black_mate) > black.mover_sign * scorer(
        black, quiet
    )


def test_terminal_best_still_teaches_ranking_among_nonterminal_alternatives() -> None:
    def teacher_option(
        series: str, score: int, feature: int, *, outcome: str | None = None
    ) -> TeacherOption:
        return TeacherOption(
            series=series,
            score_white=score,
            proof=None,
            proof_bounds=(-1, 1),
            signed_mate_distance=None,
            final_state_key=series,
            final_pfen=series,
            outcome=outcome,
            ended_by_check=outcome == "checkmate",
            is_teacher_best=outcome == "checkmate",
            is_hard_negative=False,
            features=(feature,) * 47,
            base_features=(feature,) * 7,
        )

    best = teacher_option("mate", MATE_SCORE, 99, outcome="checkmate")
    first = teacher_option("first", 30, 3)
    second = teacher_option("second", 10, 1)
    third = teacher_option("third", -20, -2)
    label = TeacherLabel(
        split="train",
        state_key="root",
        position_hash="position",
        pfen="pfen",
        series_number=5,
        mover_sign=1,
        source_profile_id="source",
        teacher_tier="tactical_d3",
        teacher_depth_series=3,
        teacher_best_series="mate",
        teacher_score_white=MATE_SCORE,
        teacher_proof="white",
        teacher_signed_mate_distance=1,
        options=(best, first, second, third),
    )

    rows, outcomes, weights = _pairwise_rows((label,), "base7")

    assert rows.shape == (3, 7)
    assert tuple(outcomes) == (1.0, 1.0, 1.0)
    assert weights.sum() > 0


def _proof_option(
    series: str,
    *,
    score: int,
    proof: str | None,
    feature: int,
    is_best: bool = False,
) -> TeacherOption:
    return TeacherOption(
        series=series,
        score_white=score,
        proof=proof,
        proof_bounds=(-1, 1),
        signed_mate_distance=None,
        final_state_key=f"final-{series}",
        final_pfen=f"pfen-{series}",
        outcome=None,
        ended_by_check=False,
        is_teacher_best=is_best,
        is_hard_negative=False,
        features=(feature,) * 47,
        base_features=(feature,) * 7,
    )


def _proof_label(
    *options: TeacherOption,
    mover_sign: int = 1,
) -> TeacherLabel:
    best = next((option for option in options if option.is_teacher_best), options[0])
    return TeacherLabel(
        split="train",
        state_key="proof-root",
        position_hash="proof-position",
        pfen="proof-pfen",
        series_number=5,
        mover_sign=mover_sign,
        source_profile_id="source",
        teacher_tier="tactical_d3",
        teacher_depth_series=3,
        teacher_best_series=best.series,
        teacher_score_white=best.score_white,
        teacher_proof=best.proof,
        teacher_signed_mate_distance=None,
        options=tuple(options),
    )


def test_mover_adverse_proof_contrast_gets_configured_pair_weight() -> None:
    safe = _proof_option("safe", score=20, proof=None, feature=2, is_best=True)
    adverse = _proof_option("adverse", score=-20, proof="black", feature=-2)
    ordinary = _proof_option("ordinary", score=-20, proof=None, feature=-2)

    _rows, _outcomes, weighted = _pairwise_rows(
        (_proof_label(safe, adverse),), "base7", adverse_pair_weight=7.0
    )
    _rows, _outcomes, unweighted = _pairwise_rows(
        (_proof_label(safe, ordinary),), "base7", adverse_pair_weight=7.0
    )

    assert weighted.shape == unweighted.shape == (1,)
    assert weighted[0] == pytest.approx(unweighted[0] * 7.0)


def test_raw_metrics_count_avoidable_and_unavoidable_adverse_choices() -> None:
    safe = _proof_option("safe", score=20, proof=None, feature=0, is_best=True)
    adverse = _proof_option("adverse", score=-20, proof="black", feature=1)
    avoidable = _metrics(
        (_proof_label(safe, adverse),),
        lambda _label, option: float(option.features[0]),
        include_rows=True,
    )

    assert avoidable["rows"][0]["chosen_series"] == "adverse"
    assert avoidable["overall"]["chosen_proven_adverse"] == 1
    assert avoidable["overall"]["chosen_avoidable_proven_adverse"] == 1

    other_adverse = _proof_option(
        "other-adverse", score=-30, proof="black", feature=0
    )
    unavoidable = _metrics(
        (_proof_label(adverse, other_adverse),),
        lambda _label, option: float(option.features[0]),
    )

    assert unavoidable["overall"]["chosen_proven_adverse"] == 1
    assert unavoidable["overall"]["chosen_avoidable_proven_adverse"] == 0


def test_avoidable_adverse_count_is_primary_model_and_profile_objective() -> None:
    lower_regret_but_adverse = {
        "chosen_avoidable_proven_adverse": 1,
        "normalized_regret": 0.0,
        "gap_weighted_pairwise_accuracy": 1.0,
        "agreement": 1.0,
    }
    safe_but_worse_aggregate = {
        "chosen_avoidable_proven_adverse": 0,
        "normalized_regret": 1.0,
        "gap_weighted_pairwise_accuracy": 0.0,
        "agreement": 0.0,
    }

    assert _metric_objective(safe_but_worse_aggregate) < _metric_objective(
        lower_regret_but_adverse
    )


def test_adverse_pair_weight_is_bounded_and_hashed_into_model_metadata() -> None:
    assert _validate_adverse_pair_weight(DEFAULT_ADVERSE_PAIR_WEIGHT) == 8.0
    for invalid in (0.5, float("nan"), float("inf"), 1_001.0):
        with pytest.raises(ValueError, match="adverse_pair_weight"):
            _validate_adverse_pair_weight(invalid)

    model = _model_payload(
        group="base7",
        ridge=0.01,
        coefficients=(1,) * 7,
        adverse_pair_weight=11.0,
        corpus_id="corpus",
        corpus_sha256="a" * 64,
    )

    assert model["adverse_pair_weight"] == 11.0
    assert model["model_id"].startswith("spc-dtv-")


def test_cross_validation_keeps_transposed_option_states_in_one_fold() -> None:
    def label(root: str, final: str) -> TeacherLabel:
        option = TeacherOption(
            series=f"move-{root}",
            score_white=10,
            proof=None,
            proof_bounds=(-1, 1),
            signed_mate_distance=None,
            final_state_key=final,
            final_pfen=final,
            outcome=None,
            ended_by_check=False,
            is_teacher_best=True,
            is_hard_negative=False,
            features=(1,) * 47,
            base_features=(1,) * 7,
        )
        return TeacherLabel(
            split="train",
            state_key=root,
            position_hash=root,
            pfen=root,
            series_number=5,
            mover_sign=1,
            source_profile_id="source",
            teacher_tier="quiet_d2",
            teacher_depth_series=2,
            teacher_best_series=option.series,
            teacher_score_white=10,
            teacher_proof=None,
            teacher_signed_mate_distance=None,
            options=(option,),
        )

    labels = (
        label("root-a", "shared-final"),
        label("root-b", "shared-final"),
        label("root-c", "final-c"),
        label("root-d", "final-d"),
    )
    folds = _folds(labels, count=3)
    fold_by_root = {
        item.state_key: fold_index
        for fold_index, fold in enumerate(folds)
        for item in fold
    }

    assert fold_by_root["root-a"] == fold_by_root["root-b"]
    for left in range(len(folds)):
        left_keys = {
            key for item in folds[left] for key in _label_semantic_keys(item)
        }
        for right in range(left + 1, len(folds)):
            right_keys = {
                key
                for item in folds[right]
                for key in _label_semantic_keys(item)
            }
            assert left_keys.isdisjoint(right_keys)


def test_holdout_claim_is_exclusive_across_output_paths(tmp_path: Path) -> None:
    claim = tmp_path / "holdout-evaluation-claim.json"

    _exclusive_json(claim, {"schema": "claim"})

    with pytest.raises(FileExistsError, match="already opened"):
        _exclusive_json(claim, {"schema": "second-claim"})


def _quarantined_corpus(tmp_path: Path) -> Path:
    holdout_sha256 = next(iter(QUARANTINED_HOLDOUT_CORPORA))
    path = tmp_path / "quarantined-corpus.json"
    path.write_text(
        '{"generation":{"holdout_corpus_sha256":"'
        + holdout_sha256
        + '"}}\n',
        encoding="utf-8",
    )
    return path


def test_fit_refuses_evaluation_contaminated_holdout(tmp_path: Path) -> None:
    corpus = _quarantined_corpus(tmp_path)

    with pytest.raises(
        ValueError,
        match="permanently quarantined.*evaluation-contaminated",
    ):
        _fit_command(
            SimpleNamespace(
                teacher_corpus=corpus,
                leader_profile=tmp_path / "unused-leader.json",
                output=tmp_path / "fit",
            )
        )


def test_holdout_evaluation_refuses_contaminated_corpus_before_claim(
    tmp_path: Path,
) -> None:
    corpus = _quarantined_corpus(tmp_path)
    output = tmp_path / "holdout-output"

    with pytest.raises(
        ValueError,
        match="permanently quarantined.*evaluation-contaminated",
    ):
        _evaluate_holdout_command(
            SimpleNamespace(
                teacher_corpus=corpus,
                leader_profile=tmp_path / "unused-leader.json",
                fit_receipt=tmp_path / "unused-fit-receipt.json",
                output=output,
            )
        )

    assert not (output / "deep-teacher-holdout-receipt.json").exists()
    assert not (tmp_path / "holdout-evaluation-claim.json").exists()
