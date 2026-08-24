from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.fit_deep_teacher_value import (
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
    _pairwise_rows,
    _terminal_score,
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
