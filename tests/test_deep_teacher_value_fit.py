from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.fit_deep_teacher_value as fitter

from scripts.fit_deep_teacher_value import (
    DEFAULT_ADVERSE_PAIR_WEIGHT,
    MATE_SCORE,
    QUARANTINED_HOLDOUT_CORPORA,
    SPLIT_ARTIFACT_SCHEMA,
    SPLIT_INTEGRITY_SCHEMA,
    TEACHER_SEMANTIC_HASH_CONTRACT,
    Preregistration,
    TeacherLabel,
    TeacherOption,
    _evaluate_holdout_command,
    _folds,
    _exclusive_json,
    _label_semantic_keys,
    _linear_scorer,
    _metric_objective,
    _metrics,
    _load_json,
    _load_preregistration,
    _model_payload,
    _pairwise_rows,
    _raw_label_payload_commitment,
    _raw_semantic_commitment,
    _read_json_artifact,
    _require_clean_cross_artifact_split,
    _reject_quarantined_holdout,
    _teacher_semantic_sha256,
    _terminal_score,
    _validate_adverse_pair_weight,
    _validate_split_artifact,
)


@pytest.fixture(autouse=True)
def _source_backed_native_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fitter.evaluation,
        "_native_eval",
        SimpleNamespace(__file__=fitter.evaluation.__file__),
    )
    monkeypatch.setattr(
        fitter.series_mate,
        "_native_mate",
        SimpleNamespace(__file__=fitter.series_mate.__file__),
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
        corpus_semantic_sha256="a" * 64,
        corpus_raw_artifact_sha256="b" * 64,
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

    with pytest.raises(FileExistsError, match="already been opened"):
        _exclusive_json(claim, {"schema": "second-claim"})


def test_exclusive_claim_persists_new_registry_and_claim_directory_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "new-claim-registry"
    claim = registry / "claim.json"
    synced: list[Path] = []
    monkeypatch.setattr(fitter, "_fsync_directory", synced.append)

    _exclusive_json(claim, {"schema": "claim"})

    assert synced == [tmp_path, registry]


def test_holdout_claim_registry_is_cycle_seed_anchored_across_refits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common_dir = tmp_path / "repository.git"
    common_dir.mkdir()
    first_root = tmp_path / "first-worktree"
    second_root = tmp_path / "second-worktree"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / ".git").write_text(
        f"gitdir: {common_dir}\n", encoding="utf-8"
    )
    second_git_dir = common_dir / "worktrees" / "second"
    second_git_dir.mkdir(parents=True)
    (second_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (second_root / ".git").write_text(
        f"gitdir: {second_git_dir}\n", encoding="utf-8"
    )
    first = Preregistration(
        path=tmp_path / "first" / "protocol.json",
        sha256="1" * 64,
        schema="spc-cycle4-one-shot-protocol-v1",
        expected_train_labels=1,
        expected_holdout_labels=1,
        manifest={"trajectory_corpora": {"sealed_holdout": {"seed": 202}}},
    )
    copied_or_refit = Preregistration(
        path=tmp_path / "copied" / "protocol.json",
        sha256="2" * 64,
        schema=first.schema,
        expected_train_labels=1,
        expected_holdout_labels=1,
        manifest=first.manifest,
    )

    monkeypatch.setattr(fitter, "_repository_root", lambda: first_root)
    first_claim = fitter._holdout_claim_path(first)
    first_preparation_claim = fitter._holdout_preparation_claim_path(first)
    monkeypatch.setattr(fitter, "_repository_root", lambda: second_root)
    second_claim = fitter._holdout_claim_path(copied_or_refit)
    second_preparation_claim = fitter._holdout_preparation_claim_path(
        copied_or_refit
    )

    assert first_claim == second_claim
    assert first_preparation_claim == second_preparation_claim
    assert common_dir in first_claim.parents
    assert common_dir in first_preparation_claim.parents


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


def test_quarantine_metadata_remains_fail_closed(tmp_path: Path) -> None:
    corpus = _quarantined_corpus(tmp_path)

    with pytest.raises(
        ValueError,
        match="permanently quarantined.*evaluation-contaminated",
    ):
        _reject_quarantined_holdout(_load_json(corpus))


def _write_preregistration(
    path: Path,
    *,
    train_labels: int = 2,
    holdout_labels: int = 2,
    artifact_source: dict[str, object] | None = None,
) -> Preregistration:
    if train_labels < 2 or holdout_labels < 2:
        raise ValueError("test preregistration needs both fixed tiers")
    root = fitter._repository_root()
    schedule_paths = sorted(
        (root / "profiles" / "training" / "teacher-source-schedule").glob("*.json")
    )
    schedule = [
        {
            "path": profile_path.relative_to(root).as_posix(),
            "profile_id": fitter.load_profile(profile_path).profile_id,
            "sha256": fitter._sha256(profile_path),
        }
        for profile_path in schedule_paths
    ]
    leader_path = root / "profiles" / "training" / "native-corpus-development-leader.json"
    quiet_train = train_labels - 1
    quiet_holdout = holdout_labels - 1
    compiler_match = fitter.re.search(
        r"MSC v\.(\d{2})(\d{2})", fitter.platform.python_compiler()
    )
    assert compiler_match is not None
    manifest = {
        "schema": "spc-cycle4-one-shot-protocol-v1",
        "status": "pre-registered-before-generation",
        "purpose": "test-only frozen one-shot protocol",
        "source": {
            "base_deployed_commit": "1" * 40,
            "integrated_engine_source_commit": "2" * 40,
            "engine_version": fitter.ENGINE_VERSION,
            "engine_source_fingerprint": fitter.ENGINE_SOURCE_FINGERPRINT,
            "native_eval_source_identity_sha256": (
                fitter.evaluation._native_source_identity()
            ),
            "native_mate_source_identity_sha256": (
                fitter.series_mate._native_mate_source_identity()
            ),
            "commit_reference_role": (
                "operator provenance labels; production preregister verifies local Git "
                "resolution while executable source is bound by fingerprints, native "
                "identities, and frozen implementation hashes"
            ),
        },
        "runtime": fitter._runtime_contract(),
        "profiles": {
            "ordered_source_schedule": schedule,
            "rejected_development_leader": {
                "path": leader_path.relative_to(root).as_posix(),
                "profile_id": fitter.load_profile(leader_path).profile_id,
                "sha256": fitter._sha256(leader_path),
            },
        },
        "trajectory_corpora": {
            "train": {
                "seed": 101,
                "attempts": 2,
                "attempt_start": 0,
                "attempt_stop": 2,
                **(
                    {"artifact_source": artifact_source}
                    if artifact_source is not None
                    else {}
                ),
            },
            "sealed_holdout": {
                "seed": 202,
                "attempts": 2,
                "attempt_start": 0,
                "attempt_stop": 2,
                "development_exclusion_sha256": (
                    artifact_source["semantic_exclusion_sha256"]
                    if artifact_source is not None
                    else fitter.semantic_exclusion_sha256(())
                ),
                "one_shot": True,
            },
            "shared_config": {
                "max_attempt_series": 64,
                "max_frontier_states": 32,
                "candidate_count": 16,
                "max_positions_per_series": 250_000,
                "max_positions_per_game": 10_000_000,
                "policy": "uniform",
                "profile_schedule": "ordered-pair-round-robin",
                "shard_size": 10_000,
                "batch_size": 256,
                "workers": 8,
                "verify_payloads": True,
                "count_unique_states": True,
            },
        },
        "teacher": {
            "selection_seed": 303,
            "minimum_series": 4,
            "maximum_series": 9,
            "branch_cap": 32,
            "max_work": 10_000_000,
            "hard_negatives": 4,
            "workers": 1,
            "prior_receipt_cache_reuse": False,
            "tiers": {
                "quiet_depth2": {
                    "target_roots": quiet_train + quiet_holdout,
                    "train_roots": quiet_train,
                    "holdout_roots": quiet_holdout,
                    "selection_mode": "quiet-nonterminal",
                    "tactical_gate": "skipped-for-quiet-tier",
                },
                "tactical_depth3": {
                    "target_roots": 2,
                    "train_roots": 1,
                    "holdout_roots": 1,
                    "selection_mode": "tactical-low-complexity",
                    "tactical_gate": "required",
                },
            },
            "expected_merged_roots": train_labels + holdout_labels,
            "expected_merged_train_roots": train_labels,
            "expected_merged_holdout_roots": holdout_labels,
        },
        "integrity": {
            "holdout_output_must_not_be_manually_inspected_before_gate": True,
            "holdout_informed_filtering_forbidden": True,
            "required_zero_intersections": fitter.REQUIRED_ZERO_INTERSECTIONS,
            "seed_burn_rule": fitter._seed_burn_rule(202),
            "semantic_key": "progressive_state_dedup_key",
            "teacher_semantic_hash_contract": TEACHER_SEMANTIC_HASH_CONTRACT,
        },
        "one_shot_gates": {
            "candidate_roles": ["primary_nonroute", "distilled_seven_weight"],
            "each_candidate_must": fitter.REQUIRED_CANDIDATE_GATES,
            "route_ablation_must": fitter.REQUIRED_ROUTE_ABLATION_GATES,
            "post_holdout_required_before_promotion": (
                fitter.REQUIRED_POST_HOLDOUT_GATES
            ),
        },
        "post_holdout_match": fitter._post_holdout_match_contract(404, "1" * 40),
        "promotion_evidence": {
            "status": fitter.PROMOTION_EVIDENCE_FIXED["status"],
            "required_receipt_schemas": list(
                fitter.PROMOTION_EVIDENCE_FIXED["required_receipt_schemas"]
            ),
        },
        "preflight": {"holdout_consumed": False, "generation_started": False},
        "frozen_implementation": fitter._current_frozen_implementation(),
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return _load_preregistration(path, require_reservation=False)


def _split_artifact(
    preregistration: Preregistration,
    *,
    artifact_split: str,
    label_split: str,
) -> dict[str, object]:
    count = (
        preregistration.expected_train_labels
        if artifact_split == "train"
        else preregistration.expected_holdout_labels
    )
    manifest = preregistration.manifest
    teacher = manifest["teacher"]
    trajectory = manifest["trajectory_corpora"]
    profile_ids = [
        entry["profile_id"]
        for entry in manifest["profiles"]["ordered_source_schedule"]
    ]
    tier_names = [
        *(
            ["quiet_d2"]
            * teacher["tiers"]["quiet_depth2"][
                "train_roots" if artifact_split == "train" else "holdout_roots"
            ]
        ),
        *(
            ["tactical_d3"]
            * teacher["tiers"]["tactical_depth3"][
                "train_roots" if artifact_split == "train" else "holdout_roots"
            ]
        ),
    ]
    labels = []
    for index, tier_name in enumerate(tier_names):
        profile_id = profile_ids[index % len(profile_ids)]
        labels.append(
            {
                "split": label_split,
                "state_key_sha256": f"{index + 1:064x}",
                "attempt_index": index % trajectory[
                    "train" if artifact_split == "train" else "sealed_holdout"
                ]["attempts"],
                "source_profile_id": profile_id,
                "white_profile_id": profile_id,
                "black_profile_id": profile_ids[(index + 1) % len(profile_ids)],
                "teacher_tier": tier_name,
                "teacher_depth_series": 2 if tier_name == "quiet_d2" else 3,
                "teacher_score_white": index,
                "options": [
                    {
                        "final_state_key_sha256": f"{index + 100:064x}",
                        "score_white_heuristic_points": index * 10,
                        "proof": None,
                    }
                ],
            }
        )
    assert len(labels) == count
    semantic_sha, _, _ = _raw_semantic_commitment(labels)
    label_payload_sha = _raw_label_payload_commitment(labels)
    train_semantic_sha = semantic_sha if artifact_split == "train" else "5" * 64
    holdout_semantic_sha = (
        semantic_sha if artifact_split == "sealed_holdout" else "5" * 64
    )
    train_label_payload_sha = (
        label_payload_sha if artifact_split == "train" else "7" * 64
    )
    holdout_label_payload_sha = (
        label_payload_sha if artifact_split == "sealed_holdout" else "7" * 64
    )
    source_binding = {
        "corpus_id": "spc-native-mixed-teacher-source",
        "semantic_sha256": "3" * 64,
        "raw_artifact_sha256": "4" * 64,
    }
    preregistration_identity = {
        "schema": preregistration.schema,
        "raw_artifact_sha256": preregistration.sha256,
    }
    tier_inputs = {
        tier: {
            "path": str(
                (
                    preregistration.path.parent
                    / ("quiet.json" if tier == "quiet_depth2" else "tactical.json")
                ).resolve()
            ),
            "corpus_id": "spc-native-teacher-test",
            "semantic_sha256": "3" * 64,
            "raw_artifact_sha256": "4" * 64,
            "augmentation_start_path": str(
                (
                    preregistration.path.parent
                    / f"{tier}.semantic.preregistration-start.json"
                ).resolve()
            ),
            "augmentation_start_raw_artifact_sha256": (
                "1" if tier == "quiet_depth2" else "2"
            )
            * 64,
            "augmentation_source_binding_path": str(
                (
                    preregistration.path.parent
                    / f"{tier}.semantic.preregistration-sources.json"
                ).resolve()
            ),
            "augmentation_source_binding_raw_artifact_sha256": "6" * 64,
            "augmentation_receipt_path": str(
                (
                    preregistration.path.parent
                    / f"{tier}.semantic.receipt.json"
                ).resolve()
            ),
            "augmentation_receipt_raw_artifact_sha256": "5" * 64,
        }
        for tier in ("quiet_depth2", "tactical_depth3")
    }
    merge_start_path = (preregistration.path.parent / "merge-start.json").resolve()
    merge_sources_path = (
        preregistration.path.parent / "merge-sources.json"
    ).resolve()
    merge_start_payload = {
        "schema": "spc-cycle4-teacher-merge-start-v1",
        "preregistration": preregistration_identity,
        "quiet_depth2": str((preregistration.path.parent / "quiet.json").resolve()),
        "tactical_depth3": str(
            (preregistration.path.parent / "tactical.json").resolve()
        ),
        "output": str((preregistration.path.parent / "merged.json").resolve()),
        "source_binding": str(merge_sources_path),
    }
    merge_start_path.write_text(json.dumps(merge_start_payload), encoding="utf-8")
    merge_start_evidence = {
        "schema": merge_start_payload["schema"],
        "path": str(merge_start_path),
        "raw_artifact_sha256": hashlib.sha256(
            merge_start_path.read_bytes()
        ).hexdigest(),
    }
    merge_sources_payload = {
        "schema": "spc-cycle4-teacher-merge-sources-v1",
        "preregistration": preregistration_identity,
        "merge_start": merge_start_evidence,
        "tier_inputs": tier_inputs,
    }
    merge_sources_path.write_text(
        json.dumps(merge_sources_payload), encoding="utf-8"
    )
    merge_sources_evidence = {
        "schema": merge_sources_payload["schema"],
        "path": str(merge_sources_path),
        "raw_artifact_sha256": hashlib.sha256(
            merge_sources_path.read_bytes()
        ).hexdigest(),
    }
    generation_provenance = {
        "schema": "spc-cycle4-preregistered-generation-provenance-v1",
        "preregistration": preregistration_identity,
        "trajectory_generation_starts": {
            split: {
                "schema": "spc-cycle4-trajectory-generation-start-v1",
                "generation_contract_sha256": (
                    fitter._expected_generation_contract_sha256(
                        preregistration, split=split
                    )
                ),
                    "corpus": {
                    "corpus_sha256": ("8" if split == "train" else "9") * 64,
                    "attempt_count": preregistration.manifest[
                        "trajectory_corpora"
                    ][split]["attempts"],
                    "record_count": 1,
                    "shard_count": 1,
                    },
                    "raw_artifact_sha256": ("a" if split == "train" else "b") * 64,
                    "root_binding_path": str(
                        (
                            preregistration.path.parent
                            / f"{split}.trajectory-root-binding.json"
                        ).resolve()
                    ),
                    "root_binding_raw_artifact_sha256": (
                        ("e" if split == "train" else "f") * 64
                    ),
                    "completion_receipt_raw_artifact_sha256": (
                    ("c" if split == "train" else "d") * 64
                ),
            }
            for split in ("train", "sealed_holdout")
        },
        "teacher_generation_starts": {
            tier: {
                "schema": "spc-cycle4-teacher-generation-start-v1",
                "tier": tier,
                "path": str(
                    (
                        preregistration.path.parent
                        / f"{tier}.preregistration-start.json"
                    ).resolve()
                ),
                "raw_artifact_sha256": ("e" if tier == "quiet_depth2" else "f")
                * 64,
            }
            for tier in ("quiet_depth2", "tactical_depth3")
        },
        "teacher_generation_source_bindings": {
            tier: {
                "schema": "spc-cycle4-teacher-generation-sources-v1",
                "tier": tier,
                "path": str(
                    (
                        preregistration.path.parent
                        / f"{tier}.preregistration-sources.json"
                    ).resolve()
                ),
                "raw_artifact_sha256": "0" * 64,
            }
            for tier in ("quiet_depth2", "tactical_depth3")
        },
        "semantic_augmentation_starts": {
            tier: {
                "schema": "spc-cycle4-teacher-semantic-augmentation-start-v1",
                "tier": tier,
                "path": str(
                    (
                        preregistration.path.parent
                        / f"{tier}.semantic.preregistration-start.json"
                    ).resolve()
                ),
                "raw_artifact_sha256": ("1" if tier == "quiet_depth2" else "2")
                * 64,
            }
            for tier in ("quiet_depth2", "tactical_depth3")
        },
        "semantic_augmentation_source_bindings": {
            tier: {
                "schema": "spc-cycle4-teacher-semantic-augmentation-sources-v1",
                "tier": tier,
                "path": str(
                    (
                        preregistration.path.parent
                        / f"{tier}.semantic.preregistration-sources.json"
                    ).resolve()
                ),
                "raw_artifact_sha256": "6" * 64,
            }
            for tier in ("quiet_depth2", "tactical_depth3")
        },
        "merge_generation_start": merge_start_evidence,
        "merge_generation_source_binding": merge_sources_evidence,
    }
    dataset_pairing_sha = fitter._dataset_pairing_sha256(
        preregistration_sha256=preregistration.sha256,
        train_semantic_keys_sha256=train_semantic_sha,
        holdout_semantic_keys_sha256=holdout_semantic_sha,
        train_label_payload_sha256=train_label_payload_sha,
        holdout_label_payload_sha256=holdout_label_payload_sha,
        cross_split_audit_sha256="6" * 64,
        train_source=source_binding,
        holdout_source=source_binding,
        source_pairing_mode="same-source-split",
    )
    tier_payloads = {}
    for tier_name, manifest_name, depth in (
        ("quiet_d2", "quiet_depth2", 2),
        ("tactical_d3", "tactical_depth3", 3),
    ):
        tier = teacher["tiers"][manifest_name]
        tier_payloads[tier_name] = {
            "config": {
                "target_roots": tier["target_roots"],
                "train_roots": tier["train_roots"],
                "selection_mode": tier["selection_mode"],
                "depth_series": depth,
                "minimum_series": teacher["minimum_series"],
                "maximum_series": teacher["maximum_series"],
                "branch_cap": teacher["branch_cap"],
                "max_generation_positions": teacher["max_work"],
                "hard_negative_count": teacher["hard_negatives"],
                "seed": teacher["selection_seed"],
                "workers": teacher["workers"],
                "expected_train_attempts": trajectory["train"]["attempts"],
                "expected_holdout_attempts": trajectory["sealed_holdout"][
                    "attempts"
                ],
            },
            "quality": {
                "status": "complete",
                "accepted_roots": tier["target_roots"],
                "train_roots": tier["train_roots"],
                "holdout_roots": tier["holdout_roots"],
                "tactical_gate": (
                    {"passed": None, "checks": [], "skipped": True}
                    if manifest_name == "quiet_depth2"
                    else {"passed": True, "checks": [{"passed": True}]}
                ),
                "tactical_failures": [],
            },
            "contract": {
                "incomplete_labels_cached": False,
                "full_retained_root_scores_required": True,
            },
        }
    return {
        "schema": fitter.CORPUS_SCHEMA,
        "method": fitter.CORPUS_METHOD,
        "engine_version": fitter.ENGINE_VERSION,
        "source_fingerprint": fitter.ENGINE_SOURCE_FINGERPRINT,
        "teacher_profile": fitter._preregistered_source_profiles(
            preregistration
        )[0].as_dict(),
        "generation": {
            "preregistration_generation_provenance": generation_provenance,
            "train_contract_sha256": fitter._expected_generation_contract_sha256(
                preregistration, split="train"
            ),
            "holdout_contract_sha256": fitter._expected_generation_contract_sha256(
                preregistration, split="sealed_holdout"
            ),
            "train_corpus_sha256": "8" * 64,
            "holdout_corpus_sha256": "9" * 64,
            "ordered_profile_ids": profile_ids,
            "profile_schedule": trajectory["shared_config"]["profile_schedule"],
            "train_attempts": trajectory["train"]["attempts"],
            "holdout_attempts": trajectory["sealed_holdout"]["attempts"],
            "train_attempt_start": trajectory["train"]["attempt_start"],
            "train_attempt_stop": trajectory["train"]["attempt_stop"],
            "holdout_attempt_start": trajectory["sealed_holdout"]["attempt_start"],
            "holdout_attempt_stop": trajectory["sealed_holdout"]["attempt_stop"],
            "development_holdout_exclusion_sha256": trajectory[
                "sealed_holdout"
            ]["development_exclusion_sha256"],
            "prior_receipt_cache_reuse": teacher["prior_receipt_cache_reuse"],
        },
        "tiers": tier_payloads,
        "artifact": {
            "schema": SPLIT_ARTIFACT_SCHEMA,
            "split": artifact_split,
            "preregistration": {
                "schema": preregistration.schema,
                "sha256": preregistration.sha256,
            },
            "source_combined_corpus": dict(source_binding),
            "counterpart_source_combined_corpus": dict(source_binding),
            "source_pairing_mode": "same-source-split",
            "dataset_pairing_sha256": dataset_pairing_sha,
            "semantic_keys_sha256": semantic_sha,
            "counterpart_semantic_keys_sha256": "5" * 64,
            "label_payload_sha256": label_payload_sha,
            "counterpart_label_payload_sha256": "7" * 64,
            "source_cross_split_audit_sha256": "6" * 64,
        },
        "labels": labels,
        "quality": {
            "accepted_roots": count,
            "train_roots": count if artifact_split == "train" else 0,
            "holdout_roots": count if artifact_split == "sealed_holdout" else 0,
        },
        "contract": {
            "incomplete_labels_cached": False,
            "full_retained_root_scores_required": True,
            "depth_is_per_label_provenance": True,
            "cross_depth_quality_metrics_blended": False,
            "train_holdout_exact_leakage_allowed": False,
            "strength_claim": False,
            "split_artifact_isolated": True,
            "distinct_source_pair_complete": False,
        },
        "selection": {},
    }


def test_preregistration_and_artifact_binding_reject_manifest_mismatch(
    tmp_path: Path,
) -> None:
    first = _write_preregistration(tmp_path / "first.json")
    artifact = _split_artifact(first, artifact_split="train", label_split="train")
    _validate_split_artifact(artifact, first, expected_artifact_split="train")

    second = _write_preregistration(
        tmp_path / "second.json", train_labels=3, holdout_labels=2
    )
    assert second.sha256 != first.sha256
    with pytest.raises(ValueError, match="preregistration binding differs"):
        _validate_split_artifact(artifact, second, expected_artifact_split="train")


def test_preregistration_rejects_frozen_implementation_and_gate_drift(
    tmp_path: Path,
) -> None:
    path = tmp_path / "protocol.json"
    _write_preregistration(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["frozen_implementation"]["scripts/generate_native_corpus.py"] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="frozen implementation drifted"):
        _load_preregistration(path, require_reservation=False)

    _write_preregistration(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["one_shot_gates"]["each_candidate_must"][-1] = "weaker gate"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="candidate holdout gates differ"):
        _load_preregistration(path, require_reservation=False)

    _write_preregistration(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["runtime"] = None
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="runtime contract is malformed"):
        _load_preregistration(path, require_reservation=False)

    _write_preregistration(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["schema"] = "spc-cycle5-one-shot-protocol-v1"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported preregistration schema"):
        _load_preregistration(path, require_reservation=False)


def test_cycle4_preregister_builder_freezes_larger_contract_and_unique_seeds(
    tmp_path: Path,
) -> None:
    manifest = fitter._build_cycle4_preregistration_manifest(
        base_deployed_commit="1" * 40,
        integrated_engine_source_commit="2" * 40,
        train_seed=101,
        holdout_seed=202,
        selection_seed=303,
        match_seed=404,
        development_source=None,
    )
    assert manifest["trajectory_corpora"]["train"]["attempts"] == 262_144
    assert manifest["trajectory_corpora"]["sealed_holdout"]["attempts"] == 131_072
    assert {
        "shard_size": 10_000,
        "batch_size": 256,
        "workers": 8,
        "verify_payloads": True,
        "count_unique_states": True,
    }.items() <= manifest["trajectory_corpora"]["shared_config"].items()
    assert manifest["teacher"]["tiers"]["quiet_depth2"] == {
        "target_roots": 3_072,
        "train_roots": 2_304,
        "holdout_roots": 768,
        "selection_mode": "quiet-nonterminal",
        "tactical_gate": "skipped-for-quiet-tier",
    }
    assert manifest["teacher"]["tiers"]["tactical_depth3"]["target_roots"] == 1_024
    assert manifest["teacher"]["expected_merged_roots"] == 4_096
    assert manifest["teacher"]["expected_merged_train_roots"] == 3_072
    assert manifest["teacher"]["expected_merged_holdout_roots"] == 1_024
    assert manifest["teacher"]["workers"] == 8
    assert manifest["post_holdout_match"]["acceptance"] == {
        "decision_rule": "scottish_progressive.league.promotion_decision",
        "minimum_games": 100,
        "required_completed_pairs": 50,
        "minimum_pair_wins": 45,
        "maximum_pair_losses": 0,
        "pair_score_must_be_strictly_above": 0.5,
    }
    raw_sha = fitter.hashlib.sha256(fitter._pretty_json_bytes(manifest)).hexdigest()
    validated = fitter._preregistration_from_manifest(
        (tmp_path / "cycle4.json").resolve(), manifest, raw_sha
    )
    assert validated.expected_train_labels == 3_072
    assert validated.expected_holdout_labels == 1_024

    with pytest.raises(ValueError, match="seeds must be unique"):
        fitter._build_cycle4_preregistration_manifest(
            base_deployed_commit="1" * 40,
            integrated_engine_source_commit="2" * 40,
            train_seed=101,
            holdout_seed=101,
            selection_seed=303,
            match_seed=404,
            development_source=None,
        )


def test_preregister_command_exclusively_writes_valid_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = (tmp_path / "cycle4.json").resolve()
    monkeypatch.setattr(
        fitter,
        "_holdout_claim_path",
        lambda _: tmp_path / "evaluation-claim.json",
    )
    monkeypatch.setattr(
        fitter,
        "_holdout_preparation_claim_path",
        lambda _: tmp_path / "preparation-claim.json",
    )
    monkeypatch.setattr(
        fitter,
        "_holdout_preparation_source_path",
        lambda _: tmp_path / "preparation-source.json",
    )
    monkeypatch.setattr(
        fitter,
        "_cycle_preregistration_reservation_path",
        lambda: (tmp_path / "cycle-reservation.json").resolve(),
    )
    monkeypatch.setattr(
        fitter,
        "_seed_preregistration_reservation_path",
        lambda _seed: (tmp_path / "seed-reservation.json").resolve(),
    )
    monkeypatch.setattr(
        fitter,
        "_verify_preregistration_commit_labels",
        lambda *_args: None,
    )
    arguments = SimpleNamespace(
        output=output,
        base_deployed_commit="1" * 40,
        integrated_engine_source_commit="2" * 40,
        train_seed=101,
        holdout_seed=202,
        selection_seed=303,
        match_seed=404,
        development_source=None,
        development_consumption_evidence=None,
        development_source_metadata=None,
        dry_run=False,
    )
    fitter._preregister_command(arguments)
    summary = json.loads(capsys.readouterr().out)
    assert summary["effective_train_labels"] == 3_072
    assert summary["sealed_holdout_labels"] == 1_024
    assert output.exists()
    _load_preregistration(output)
    output.unlink()
    fitter._preregister_command(arguments)
    capsys.readouterr()
    assert output.exists()
    _load_preregistration(output)
    fitter._preregister_command(arguments)
    capsys.readouterr()

    cycle_reservation = (tmp_path / "cycle-reservation.json").resolve()
    seed_reservation = (tmp_path / "seed-reservation.json").resolve()
    cycle_reservation.unlink()
    seed_reservation.unlink()
    changed = SimpleNamespace(**{**vars(arguments), "match_seed": 405})
    with pytest.raises(FileExistsError, match="output already differs"):
        fitter._preregister_command(changed)
    assert not cycle_reservation.exists()
    assert not seed_reservation.exists()

    output.unlink()
    original_builder = fitter._build_cycle4_preregistration_manifest
    build_calls = 0

    def drifting_builder(**kwargs: object) -> dict[str, object]:
        nonlocal build_calls
        build_calls += 1
        candidate = original_builder(**kwargs)
        if build_calls == 2:
            candidate["purpose"] = f"{candidate['purpose']} changed"
        return candidate

    monkeypatch.setattr(
        fitter,
        "_build_cycle4_preregistration_manifest",
        drifting_builder,
    )
    with pytest.raises(ValueError, match="changed before reservation"):
        fitter._preregister_command(arguments)
    assert build_calls == 2
    assert not output.exists()
    assert not cycle_reservation.exists()
    assert not seed_reservation.exists()


def test_protocol_registry_isolation_rejects_equal_parent_and_child_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preregistration = Preregistration(
        path=(tmp_path / "protocol.json").resolve(),
        sha256="1" * 64,
        schema="spc-cycle4-one-shot-protocol-v1",
        expected_train_labels=1,
        expected_holdout_labels=1,
        manifest={"trajectory_corpora": {"sealed_holdout": {"seed": 202}}},
    )
    marker = (tmp_path / "registry" / "claim.json").resolve()
    monkeypatch.setattr(
        fitter,
        "_protocol_registry_paths",
        lambda _: {"claim": marker},
    )
    for candidate in (marker, marker.parent, marker / "child"):
        with pytest.raises(ValueError, match="overlaps protocol registry"):
            fitter._require_protocol_registry_isolation(
                preregistration,
                {"candidate": candidate},
                label="test",
            )


def test_fit_preflight_binds_preregistered_leader_and_proof_weight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_leader = (tmp_path / "expected-leader.json").resolve()
    preregistration = Preregistration(
        path=(tmp_path / "protocol.json").resolve(),
        sha256="1" * 64,
        schema="spc-cycle4-one-shot-protocol-v1",
        expected_train_labels=1,
        expected_holdout_labels=1,
        manifest={
            "trajectory_corpora": {"sealed_holdout": {"seed": 202}},
            "profiles": {
                "rejected_development_leader": {
                    "path": "profiles/leader.json",
                    "profile_id": "spc-frozen-leader",
                    "sha256": "2" * 64,
                }
            },
        },
    )
    def load_completed_pair_preregistration(
        _path: Path, **kwargs: object
    ) -> Preregistration:
        assert kwargs == {"require_pair_completion": True}
        return preregistration

    monkeypatch.setattr(
        fitter, "_load_preregistration", load_completed_pair_preregistration
    )
    monkeypatch.setattr(fitter, "_repository_file", lambda _: expected_leader)
    monkeypatch.setattr(
        fitter, "_require_protocol_registry_isolation", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        fitter, "_holdout_claim_path", lambda _: (tmp_path / "claim.json").resolve()
    )
    sealed_path = (tmp_path / "sealed.json").resolve()
    sealed_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        fitter,
        "_trusted_pair_completion",
        lambda _: (
            {
                "sealed_holdout": {
                    "path": str(sealed_path),
                    "raw_artifact_sha256": fitter._sha256(sealed_path),
                    "file_identity": fitter._file_identity(os.stat(sealed_path)),
                }
            },
            "3" * 64,
        ),
    )
    arguments = SimpleNamespace(
        preregistration=preregistration.path,
        teacher_corpus=(tmp_path / "train.json").resolve(),
        leader_profile=(tmp_path / "wrong-leader.json").resolve(),
        output=(tmp_path / "fit").resolve(),
        adverse_pair_weight=fitter.DEFAULT_ADVERSE_PAIR_WEIGHT,
    )
    with pytest.raises(ValueError, match="leader path differs"):
        fitter._fit_command(arguments)

    arguments.leader_profile = expected_leader
    arguments.adverse_pair_weight = fitter.DEFAULT_ADVERSE_PAIR_WEIGHT + 1.0
    with pytest.raises(ValueError, match="weight differs"):
        fitter._fit_command(arguments)


def test_preregistration_bootstrap_rejects_unreserved_path_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reserved_manifest = (tmp_path / "protocol.json").resolve()
    supplied_sealed = (tmp_path / "sealed-holdout.json").resolve()
    cycle_reservation = (tmp_path / "cycle-reservation.json").resolve()
    cycle_reservation.write_text(
        json.dumps(
            {
                "schema": fitter.PREREGISTRATION_RESERVATION_SCHEMA,
                "cycle_schema": "spc-cycle4-one-shot-protocol-v1",
                "manifest_path": str(reserved_manifest),
                "manifest_raw_artifact_sha256": "1" * 64,
                "train_seed": 101,
                "holdout_seed": 202,
                "teacher_selection_seed": 303,
                "post_holdout_match_seed": 404,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        fitter, "_cycle_preregistration_reservation_path", lambda: cycle_reservation
    )
    opened: list[Path] = []
    original_read = fitter._read_json_artifact

    def guarded_read(path: Path) -> tuple[dict[str, object], str]:
        opened.append(path)
        assert path != supplied_sealed, "unreserved caller path was opened"
        return original_read(path)

    monkeypatch.setattr(fitter, "_read_json_artifact", guarded_read)
    with pytest.raises(ValueError, match="differs from the central reservation"):
        fitter._load_preregistration(supplied_sealed)
    assert opened == [cycle_reservation]


def test_completed_pair_bootstrap_fails_before_manifest_when_marker_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reserved_manifest = (tmp_path / "protocol.json").resolve()
    cycle_reservation = (tmp_path / "cycle-reservation.json").resolve()
    missing_completion = (tmp_path / "missing-pair-completion.json").resolve()
    cycle_reservation.write_text(
        json.dumps(
            {
                "schema": fitter.PREREGISTRATION_RESERVATION_SCHEMA,
                "cycle_schema": "spc-cycle4-one-shot-protocol-v1",
                "manifest_path": str(reserved_manifest),
                "manifest_raw_artifact_sha256": "1" * 64,
                "train_seed": 101,
                "holdout_seed": 202,
                "teacher_selection_seed": 303,
                "post_holdout_match_seed": 404,
            }
        ),
        encoding="utf-8",
    )
    reserved_manifest.write_bytes(b'{"must_not_be_opened":true}')
    monkeypatch.setattr(
        fitter, "_cycle_preregistration_reservation_path", lambda: cycle_reservation
    )
    monkeypatch.setattr(
        fitter,
        "_pair_completion_registry_path_for_seed",
        lambda _seed: missing_completion,
    )
    opened: list[Path] = []
    original_read = fitter._read_json_artifact

    def guarded_read(path: Path) -> tuple[dict[str, object], str]:
        opened.append(path)
        assert path != reserved_manifest, "manifest opened before pair completion"
        return original_read(path)

    monkeypatch.setattr(fitter, "_read_json_artifact", guarded_read)
    with pytest.raises(FileNotFoundError, match="central pair completion is missing"):
        fitter._load_preregistration(
            reserved_manifest, require_pair_completion=True
        )
    assert opened == [cycle_reservation]


def test_upstream_fence_rejects_pair_prefix_before_manifest_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reserved_manifest = (tmp_path / "protocol.json").resolve()
    cycle_reservation = (tmp_path / "cycle-reservation.json").resolve()
    preparation_claim = (tmp_path / "pair-preparation.json").resolve()
    missing_completion = (tmp_path / "missing-pair-completion.json").resolve()
    cycle_reservation.write_text(
        json.dumps(
            {
                "schema": fitter.PREREGISTRATION_RESERVATION_SCHEMA,
                "cycle_schema": "spc-cycle4-one-shot-protocol-v1",
                "manifest_path": str(reserved_manifest),
                "manifest_raw_artifact_sha256": "1" * 64,
                "train_seed": 101,
                "holdout_seed": 202,
                "teacher_selection_seed": 303,
                "post_holdout_match_seed": 404,
            }
        ),
        encoding="utf-8",
    )
    preparation_claim.write_text("{}", encoding="utf-8")
    reserved_manifest.write_bytes(b'{"must_not_be_opened":true}')
    monkeypatch.setattr(
        fitter, "_cycle_preregistration_reservation_path", lambda: cycle_reservation
    )
    monkeypatch.setattr(
        fitter,
        "_pair_completion_registry_path_for_seed",
        lambda _seed: missing_completion,
    )
    monkeypatch.setattr(
        fitter,
        "_terminal_pair_state_paths_for_seed",
        lambda _seed: (preparation_claim, missing_completion),
    )
    opened: list[Path] = []
    original_read = fitter._read_json_artifact

    def guarded_read(path: Path) -> tuple[dict[str, object], str]:
        opened.append(path)
        assert path != reserved_manifest, "upstream fence opened the manifest"
        return original_read(path)

    monkeypatch.setattr(fitter, "_read_json_artifact", guarded_read)
    with pytest.raises(FileExistsError, match="upstream producer"):
        fitter._load_preregistration(
            reserved_manifest, forbid_pair_preparation=True
        )
    assert opened == [cycle_reservation]


def test_protocol_stage_lock_allows_producers_and_excludes_pairing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = (tmp_path / "cycle4-stage.lock").resolve()
    monkeypatch.setattr(fitter, "_protocol_stage_lock_path", lambda: lock_path)

    with fitter._protocol_stage_lock("producer one", exclusive=False):
        with fitter._protocol_stage_lock("producer two", exclusive=False):
            with pytest.raises(RuntimeError, match="cannot run concurrently"):
                with fitter._protocol_stage_lock("pair", exclusive=True):
                    raise AssertionError("exclusive lock unexpectedly acquired")

    with fitter._protocol_stage_lock("pair", exclusive=True):
        with pytest.raises(RuntimeError, match="cannot run concurrently"):
            with fitter._protocol_stage_lock("producer", exclusive=False):
                raise AssertionError("shared lock unexpectedly acquired")


def test_fit_rejects_direct_sealed_path_and_train_hardlink_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    train_path = (tmp_path / "paired" / "train-teacher-artifact.json").resolve()
    sealed_path = (
        tmp_path / "paired" / "sealed-holdout-teacher-artifact.json"
    ).resolve()
    train_path.parent.mkdir()
    sealed_path.write_bytes(b'{"sealed":"secret"}')
    train_path.write_bytes(b'{"train":"safe"}')
    original_train_identity = fitter._file_identity(os.stat(train_path))
    sealed_identity = fitter._file_identity(os.stat(sealed_path))
    leader_path = (tmp_path / "leader.json").resolve()
    preregistration = Preregistration(
        path=(tmp_path / "protocol.json").resolve(),
        sha256="1" * 64,
        schema="spc-cycle4-one-shot-protocol-v1",
        expected_train_labels=1,
        expected_holdout_labels=1,
        manifest={
            "trajectory_corpora": {"sealed_holdout": {"seed": 202}},
            "profiles": {
                "rejected_development_leader": {
                    "path": "profiles/leader.json",
                    "profile_id": "spc-frozen-leader",
                    "sha256": "2" * 64,
                }
            },
        },
    )
    completion = {
        "train": {
            "path": str(train_path),
            "raw_artifact_sha256": "3" * 64,
            "file_identity": original_train_identity,
        },
        "sealed_holdout": {
            "path": str(sealed_path),
            "raw_artifact_sha256": "4" * 64,
            "file_identity": sealed_identity,
        },
        "local_publication": {"raw_artifact_sha256": "5" * 64},
    }
    def load_completed_pair_preregistration(
        _path: Path, **kwargs: object
    ) -> Preregistration:
        assert kwargs == {"require_pair_completion": True}
        return preregistration

    monkeypatch.setattr(
        fitter, "_load_preregistration", load_completed_pair_preregistration
    )
    monkeypatch.setattr(
        fitter, "_trusted_pair_completion", lambda _: (completion, "6" * 64)
    )
    monkeypatch.setattr(fitter, "_repository_file", lambda _: leader_path)
    monkeypatch.setattr(
        fitter, "_require_protocol_registry_isolation", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        fitter, "_holdout_claim_path", lambda _: (tmp_path / "claim.json").resolve()
    )
    base_arguments = {
        "preregistration": preregistration.path,
        "leader_profile": leader_path,
        "output": (tmp_path / "fit").resolve(),
        "adverse_pair_weight": fitter.DEFAULT_ADVERSE_PAIR_WEIGHT,
    }
    with pytest.raises(ValueError, match="not the centrally completed pair"):
        fitter._fit_command(
            SimpleNamespace(**{**base_arguments, "teacher_corpus": sealed_path})
        )

    train_path.unlink()
    os.link(sealed_path, train_path)
    sealed_reads = 0
    original_os_read = fitter.os.read

    def guarded_os_read(descriptor: int, size: int) -> bytes:
        nonlocal sealed_reads
        if fitter._file_identity(os.fstat(descriptor)) == sealed_identity:
            sealed_reads += 1
        return original_os_read(descriptor, size)

    monkeypatch.setattr(fitter.os, "read", guarded_os_read)
    with pytest.raises(ValueError, match="aliases the sealed holdout"):
        fitter._fit_command(
            SimpleNamespace(**{**base_arguments, "teacher_corpus": train_path})
        )
    assert sealed_reads == 0


def _development_import_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Preregistration, Path, dict[str, object]]:
    labels = [
        {
            "split": split,
            "state_key_sha256": f"{index + 1:064x}",
            "teacher_score_white": index * 100,
            "options": [
                {
                    "final_state_key_sha256": f"{index + 100:064x}",
                    "score_white_heuristic_points": index * 10,
                    "proof": None,
                }
            ],
        }
        for index, split in enumerate(("train", "holdout"))
    ]
    source = {
        "schema": fitter.CORPUS_SCHEMA,
        "method": fitter.CORPUS_METHOD,
        "corpus_id": "spc-native-mixed-teacher-consumed-source",
        "labels": labels,
        "tiers": {"quiet_d2": {}, "tactical_d3": {}},
        "quality": {
            "status": "complete",
            "accepted_roots": 2,
            "train_roots": 1,
            "holdout_roots": 1,
        },
        "selection": {
            "selected_root_exact_overlap_states": 0,
            "cross_split_option_final_exact_overlap_states": 0,
            "train_option_final_to_holdout_root_overlap_states": 0,
            "holdout_option_final_to_train_root_overlap_states": 0,
        },
        "contract": {
            "incomplete_labels_cached": False,
            "full_retained_root_scores_required": True,
            "depth_is_per_label_provenance": True,
            "cross_depth_quality_metrics_blended": False,
            "train_holdout_exact_leakage_allowed": False,
            "strength_claim": False,
        },
    }
    source_path = (tmp_path / "consumed-source.json").resolve()
    source_path.write_text(json.dumps(source), encoding="utf-8")
    source_raw_sha = fitter._sha256(source_path)
    _, source_roots, source_finals = fitter._raw_semantic_commitment(labels)
    evidence = {
        "schema": "spc-cycle3-one-shot-result-v1",
        "corpora": {
            "mixed_teacher_sha256": source_raw_sha,
            "train_labels": 1,
            "holdout_labels": 1,
        },
        "one_shot_holdout_metrics": {"baseline": {}},
    }
    evidence_path = (tmp_path / "consumption-evidence.json").resolve()
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    binding: dict[str, object] = {
        "path": str(source_path),
        "corpus_id": source["corpus_id"],
        "semantic_sha256": fitter._teacher_semantic_sha256(source),
        "raw_artifact_sha256": source_raw_sha,
        "label_count": 2,
        "semantic_exclusion_sha256": fitter.semantic_exclusion_sha256(
            source_roots | source_finals
        ),
        "consumption_evidence": {
            "path": str(evidence_path),
            "schema": evidence["schema"],
            "raw_artifact_sha256": fitter._sha256(evidence_path),
        },
    }
    monkeypatch.setattr(
        fitter, "_validate_consumption_evidence", lambda _source: None
    )
    preregistration = _write_preregistration(
        tmp_path / "protocol.json",
        train_labels=2,
        holdout_labels=2,
        artifact_source=binding,
    )
    output = (tmp_path / "development-import.json").resolve()
    monkeypatch.setattr(
        fitter,
        "_materialize_labels",
        lambda *_args, **_kwargs: ((), {}),
    )
    payload = fitter._development_import_payload(
        source,
        declared_source=binding,
        preregistration=preregistration,
        output_path=output,
    )
    def load_upstream_preregistration(
        _path: Path, **kwargs: object
    ) -> Preregistration:
        assert kwargs == {"forbid_pair_preparation": True}
        return preregistration

    monkeypatch.setattr(
        fitter, "_load_preregistration", load_upstream_preregistration
    )
    return preregistration, output, payload


def test_development_import_rejects_forged_relabels_and_path_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preregistration, output, payload = _development_import_fixture(
        tmp_path, monkeypatch
    )
    fitter._validate_development_import(
        payload, preregistration, supplied_path=output
    )

    forged = json.loads(json.dumps(payload))
    forged["labels"][0]["teacher_score_white"] += 1
    forged["artifact"]["label_payload_sha256"] = fitter._raw_label_payload_commitment(
        forged["labels"]
    )
    with pytest.raises(ValueError, match="payload differs from declared source"):
        fitter._validate_development_import(
            forged, preregistration, supplied_path=output
        )

    alias = output.parent / "alias" / ".." / output.name
    with pytest.raises(ValueError, match="canonical preregistration path"):
        fitter._validate_development_import(
            payload, preregistration, supplied_path=alias
        )


def test_consumption_evidence_requires_exact_cycle3_128_64_source(
    tmp_path: Path,
) -> None:
    evidence_path = (
        fitter._repository_root()
        / "benchmarks/results/cycle3-one-shot-teacher-ranking-2026-08-23.json"
    ).resolve()
    binding = {
        "raw_artifact_sha256": fitter.CYCLE3_CONSUMED_CORPUS_RAW_SHA256,
        "label_count": 192,
        "consumption_evidence": {
            "path": str(evidence_path),
            "schema": "spc-cycle3-one-shot-result-v1",
            "raw_artifact_sha256": fitter._sha256(evidence_path),
        },
    }
    fitter._validate_consumption_evidence(binding)

    forged_path = (tmp_path / "forged-cycle3-result.json").resolve()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["corpora"]["train_labels"] = 127
    forged_path.write_text(json.dumps(evidence), encoding="utf-8")
    forged_binding = json.loads(json.dumps(binding))
    forged_binding["consumption_evidence"]["path"] = str(forged_path)
    forged_binding["consumption_evidence"]["raw_artifact_sha256"] = (
        fitter._sha256(forged_path)
    )
    with pytest.raises(ValueError, match="exact committed cycle-3 result"):
        fitter._validate_consumption_evidence(forged_binding)


def test_declared_development_metadata_rejects_symlink_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (tmp_path / "source.json").resolve()
    evidence = (tmp_path / "evidence.json").resolve()
    source.write_text("{}", encoding="utf-8")
    evidence.write_text("{}", encoding="utf-8")
    source_alias = tmp_path / "source-alias.json"
    evidence_alias = tmp_path / "evidence-alias.json"
    original_resolve = Path.resolve

    def simulated_alias_resolve(
        path: Path, *args: object, **kwargs: object
    ) -> Path:
        if path == source_alias:
            return source
        if path == evidence_alias:
            return evidence
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", simulated_alias_resolve)
    binding = {
        "path": str(source_alias),
        "corpus_id": "consumed-development",
        "semantic_sha256": "1" * 64,
        "raw_artifact_sha256": "2" * 64,
        "label_count": 192,
        "semantic_exclusion_sha256": "3" * 64,
        "consumption_evidence": {
            "path": str(evidence),
            "schema": "spc-cycle3-one-shot-result-v1",
            "raw_artifact_sha256": "4" * 64,
        },
    }
    with pytest.raises(ValueError, match="artifact source path is malformed"):
        fitter._validate_declared_artifact_source(binding, "train")

    binding["path"] = str(source)
    binding["consumption_evidence"]["path"] = str(evidence_alias)
    with pytest.raises(ValueError, match="consumption evidence is malformed"):
        fitter._validate_declared_artifact_source(binding, "train")


def test_import_development_dry_run_executes_complete_validation_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    preregistration, output, _ = _development_import_fixture(
        tmp_path, monkeypatch
    )
    source_path = Path(
        preregistration.manifest["trajectory_corpora"]["train"][
            "artifact_source"
        ]["path"]
    )
    fitter._import_development_command(
        SimpleNamespace(
            preregistration=preregistration.path,
            consumed_source=source_path,
            output=output,
            dry_run=True,
        )
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["dry_run"] is True
    assert summary["labels"] == 2
    assert not output.exists()

    alias = source_path.parent / "alias" / ".." / source_path.name
    with pytest.raises(ValueError, match="canonical preregistration path"):
        fitter._import_development_command(
            SimpleNamespace(
                preregistration=preregistration.path,
                consumed_source=alias,
                output=output,
                dry_run=True,
            )
        )


def test_import_development_output_is_canonical_and_exactly_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    preregistration, output, expected = _development_import_fixture(
        tmp_path, monkeypatch
    )
    source_path = Path(
        preregistration.manifest["trajectory_corpora"]["train"][
            "artifact_source"
        ]["path"]
    )
    arguments = SimpleNamespace(
        preregistration=preregistration.path,
        consumed_source=source_path,
        output=output,
        dry_run=False,
    )
    fitter._import_development_command(arguments)
    capsys.readouterr()
    fitter._import_development_command(arguments)
    capsys.readouterr()
    assert json.loads(output.read_text(encoding="utf-8")) == expected

    alias = output.parent / "alias" / ".." / "second-development.json"
    with pytest.raises(ValueError, match="absolute and canonical"):
        fitter._import_development_command(
            SimpleNamespace(**{**vars(arguments), "output": alias})
        )


def test_import_development_reopens_source_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preregistration, output, _ = _development_import_fixture(
        tmp_path, monkeypatch
    )
    source_path = Path(
        preregistration.manifest["trajectory_corpora"]["train"][
            "artifact_source"
        ]["path"]
    )
    original_read = fitter._read_declared_artifact_source
    calls = 0

    def drifting_read(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        snapshot = dict(original_read(*args, **kwargs))
        if calls >= 2:
            snapshot["created_at"] = "changed-during-import"
        return snapshot

    monkeypatch.setattr(fitter, "_read_declared_artifact_source", drifting_read)
    with pytest.raises(ValueError, match="changed before import publication"):
        fitter._import_development_command(
            SimpleNamespace(
                preregistration=preregistration.path,
                consumed_source=source_path,
                output=output,
                dry_run=False,
            )
        )
    assert calls >= 2
    assert not output.exists()


def test_distinct_source_pairing_rejects_semantic_or_raw_aliases() -> None:
    train = {
        "corpus_id": "train",
        "semantic_sha256": "1" * 64,
        "raw_artifact_sha256": "2" * 64,
    }
    fitter._require_distinct_source_bindings(
        train,
        {
            "corpus_id": "holdout",
            "semantic_sha256": "3" * 64,
            "raw_artifact_sha256": "4" * 64,
        },
    )
    with pytest.raises(ValueError, match="same source corpus"):
        fitter._require_distinct_source_bindings(
            train,
            {
                "corpus_id": "forged-new-id",
                "semantic_sha256": "1" * 64,
                "raw_artifact_sha256": "9" * 64,
            },
        )
    with pytest.raises(ValueError, match="same source corpus"):
        fitter._require_distinct_source_bindings(
            train,
            {
                "corpus_id": "forged-new-id",
                "semantic_sha256": "8" * 64,
                "raw_artifact_sha256": "2" * 64,
            },
        )


def test_pair_claim_precedes_holdout_read_and_unbound_crash_burns_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preregistration = Preregistration(
        path=tmp_path / "protocol.json",
        sha256="1" * 64,
        schema="spc-cycle4-one-shot-protocol-v1",
        expected_train_labels=1,
        expected_holdout_labels=1,
        manifest={
            "trajectory_corpora": {
                "sealed_holdout": {"seed": 202},
            }
        },
    )
    claim = (tmp_path / "pair-preparation-claim.json").resolve()
    train_path = (tmp_path / "train.json").resolve()
    holdout_path = (tmp_path / "sealed-holdout.json").resolve()
    holdout_reads = 0
    original_read = fitter._read_json_artifact
    monkeypatch.setattr(fitter, "_load_preregistration", lambda _: preregistration)
    monkeypatch.setattr(
        fitter, "_holdout_preparation_claim_path", lambda _: claim
    )
    source_binding_path = (tmp_path / "pair-source-binding.json").resolve()
    monkeypatch.setattr(
        fitter, "_holdout_preparation_source_path", lambda _: source_binding_path
    )

    def read(path: Path) -> tuple[dict[str, object], str]:
        nonlocal holdout_reads
        if path in {train_path, holdout_path}:
            assert claim.exists(), "caller-controlled artifact read before claim"
        if path == holdout_path:
            holdout_reads += 1
        if path == claim:
            return original_read(path)
        return {}, "2" * 64

    monkeypatch.setattr(fitter, "_read_json_artifact", read)
    monkeypatch.setattr(
        fitter,
        "_bind_holdout_preparation_source",
        lambda *_args, **_kwargs: source_binding_path,
    )
    monkeypatch.setattr(
        fitter,
        "_pair_train_input",
        lambda *_args, **_kwargs: (
            [
                {
                    "state_key_sha256": "3" * 64,
                    "options": [{"final_state_key_sha256": "4" * 64}],
                }
            ],
            {
                "corpus_id": "train",
                "semantic_sha256": "5" * 64,
                "raw_artifact_sha256": "6" * 64,
            },
            None,
        ),
    )
    original_resolve = Path.resolve

    def guarded_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path in {train_path, holdout_path}:
            assert claim.exists(), "caller-controlled artifact resolve before claim"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)

    def stop_after_claim(*_args: object, **_kwargs: object) -> object:
        assert claim.exists(), "sealed holdout validation before preparation claim"
        holdout_path.resolve()
        raise ValueError("stop-after-protected-holdout-touch")

    monkeypatch.setattr(fitter, "_pair_holdout_input", stop_after_claim)
    arguments = SimpleNamespace(
        preregistration=preregistration.path,
        train_artifact=train_path,
        sealed_holdout_source=holdout_path,
        output=(tmp_path / "paired").resolve(),
        dry_run=False,
        metadata_only=False,
    )
    with pytest.raises(ValueError, match="stop-after-protected"):
        fitter._pair_artifacts_command(arguments)
    assert claim.exists()
    assert holdout_reads == 1

    with pytest.raises(FileExistsError, match="seed is burned"):
        fitter._pair_artifacts_command(arguments)
    assert holdout_reads == 1

    retry = SimpleNamespace(
        **{**vars(arguments), "output": (tmp_path / "paired-retry").resolve()}
    )
    with pytest.raises(FileExistsError, match="seed is burned"):
        fitter._pair_artifacts_command(retry)
    assert holdout_reads == 1


@pytest.mark.parametrize(
    "operation",
    ("pair-distinct-source-artifacts", "split-fresh-combined-artifacts"),
)
def test_preparation_claim_and_source_prefixes_resume_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    preregistration = Preregistration(
        path=(tmp_path / "protocol.json").resolve(),
        sha256="1" * 64,
        schema="spc-cycle4-one-shot-protocol-v1",
        expected_train_labels=1,
        expected_holdout_labels=1,
        manifest={"trajectory_corpora": {"sealed_holdout": {"seed": 202}}},
    )
    claim_path = (tmp_path / "preparation-claim.json").resolve()
    source_binding_path = (tmp_path / "preparation-source.json").resolve()
    sealed_source = (tmp_path / "sealed-source.json").resolve()
    train_source = (
        (tmp_path / "train-source.json").resolve()
        if operation == "pair-distinct-source-artifacts"
        else None
    )
    output = (tmp_path / "paired").resolve()
    monkeypatch.setattr(
        fitter, "_holdout_preparation_claim_path", lambda _: claim_path
    )
    monkeypatch.setattr(
        fitter, "_holdout_preparation_source_path", lambda _: source_binding_path
    )

    first_claim = fitter._claim_holdout_preparation(
        preregistration,
        requested_source=sealed_source,
        requested_train_source=train_source,
        requested_output=output,
        operation=operation,
        allow_identical_resume=True,
    )
    assert first_claim == claim_path
    assert (
        fitter._claim_holdout_preparation(
            preregistration,
            requested_source=sealed_source,
            requested_train_source=train_source,
            requested_output=output,
            operation=operation,
            allow_identical_resume=True,
        )
        == claim_path
    )
    if train_source is not None:
        with pytest.raises(FileExistsError, match="different request"):
            fitter._claim_holdout_preparation(
                preregistration,
                requested_source=sealed_source,
                requested_train_source=(tmp_path / "alternate-train.json").resolve(),
                requested_output=output,
                operation=operation,
                allow_identical_resume=True,
            )
    first_source = fitter._bind_holdout_preparation_source(
        preregistration,
        preparation_claim_path=claim_path,
        operation=operation,
        sealed_source_path=sealed_source,
        sealed_source_raw_sha256="2" * 64,
        train_source_path=train_source,
        train_source_raw_sha256=(
            "4" * 64 if train_source is not None else None
        ),
        requested_output=output,
        allow_identical_resume=True,
    )
    assert first_source == source_binding_path
    assert (
        fitter._bind_holdout_preparation_source(
            preregistration,
            preparation_claim_path=claim_path,
            operation=operation,
            sealed_source_path=sealed_source,
            sealed_source_raw_sha256="2" * 64,
            train_source_path=train_source,
            train_source_raw_sha256=(
                "4" * 64 if train_source is not None else None
            ),
            requested_output=output,
            allow_identical_resume=True,
        )
        == source_binding_path
    )
    with pytest.raises(FileExistsError, match="source binding differs"):
        fitter._bind_holdout_preparation_source(
            preregistration,
            preparation_claim_path=claim_path,
            operation=operation,
            sealed_source_path=sealed_source,
            sealed_source_raw_sha256="3" * 64,
            train_source_path=train_source,
            train_source_raw_sha256=(
                "4" * 64 if train_source is not None else None
            ),
            requested_output=output,
            allow_identical_resume=True,
        )
    if train_source is not None:
        with pytest.raises(FileExistsError, match="source binding differs"):
            fitter._bind_holdout_preparation_source(
                preregistration,
                preparation_claim_path=claim_path,
                operation=operation,
                sealed_source_path=sealed_source,
                sealed_source_raw_sha256="2" * 64,
                train_source_path=train_source,
                train_source_raw_sha256="5" * 64,
                requested_output=output,
                allow_identical_resume=True,
            )


def test_atomic_resumable_publication_never_exposes_partial_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = (tmp_path / "resumable.json").resolve()

    def fail_link(_source: Path, _destination: Path) -> None:
        raise OSError("simulated power loss before final link")

    monkeypatch.setattr(fitter.os, "link", fail_link)
    with pytest.raises(OSError, match="simulated power loss"):
        fitter._atomic_exclusive_json(
            final,
            {"schema": "resumable"},
            conflict_message="already exists",
        )
    assert not final.exists()


def test_pair_metadata_only_never_claims_or_reads_holdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    preregistration = Preregistration(
        path=tmp_path / "protocol.json",
        sha256="1" * 64,
        schema="spc-cycle4-one-shot-protocol-v1",
        expected_train_labels=1,
        expected_holdout_labels=1,
        manifest={"trajectory_corpora": {"sealed_holdout": {"seed": 202}}},
    )
    claim = (tmp_path / "pair-preparation-claim.json").resolve()
    train_path = (tmp_path / "train.json").resolve()
    holdout_path = (tmp_path / "sealed-holdout.json").resolve()
    monkeypatch.setattr(fitter, "_load_preregistration", lambda _: preregistration)
    monkeypatch.setattr(
        fitter, "_holdout_preparation_claim_path", lambda _: claim
    )

    def read(path: Path) -> tuple[dict[str, object], str]:
        raise AssertionError(f"metadata-only mode opened an artifact: {path}")

    monkeypatch.setattr(fitter, "_read_json_artifact", read)
    fitter._pair_artifacts_command(
        SimpleNamespace(
            preregistration=preregistration.path,
            train_artifact=train_path,
            sealed_holdout_source=holdout_path,
            output=(tmp_path / "paired").resolve(),
            dry_run=False,
            metadata_only=True,
        )
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["artifacts_opened"] is False
    assert summary["holdout_opened"] is False
    assert summary["train_source"] is None
    assert not claim.exists()


def test_pair_dry_run_is_rejected_before_claim_or_holdout_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preregistration = Preregistration(
        path=tmp_path / "protocol.json",
        sha256="1" * 64,
        schema="spc-cycle4-one-shot-protocol-v1",
        expected_train_labels=1,
        expected_holdout_labels=1,
        manifest={"trajectory_corpora": {"sealed_holdout": {"seed": 202}}},
    )
    claim = (tmp_path / "claim.json").resolve()
    monkeypatch.setattr(fitter, "_load_preregistration", lambda _: preregistration)
    monkeypatch.setattr(fitter, "_holdout_preparation_claim_path", lambda _: claim)
    monkeypatch.setattr(
        fitter,
        "_read_json_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run must not read any artifact")
        ),
    )
    with pytest.raises(ValueError, match="--dry-run is forbidden"):
        fitter._pair_artifacts_command(
            SimpleNamespace(
                preregistration=preregistration.path,
                train_artifact=(tmp_path / "train.json").resolve(),
                sealed_holdout_source=(tmp_path / "holdout.json").resolve(),
                output=(tmp_path / "paired").resolve(),
                dry_run=True,
                metadata_only=False,
            )
        )
    assert not claim.exists()


def test_pair_publication_is_exclusive_and_non_overwriting(tmp_path: Path) -> None:
    output = (tmp_path / "paired").resolve()
    fitter._reserve_output_directory(output, "test pair")
    claim = (tmp_path / "claim.json").resolve()
    source = (tmp_path / "source-binding.json").resolve()
    sealed_source = (tmp_path / "sealed-source.json").resolve()
    sealed_source.write_text("{}", encoding="utf-8")
    claim.write_text(
        json.dumps({"schema": fitter.HOLDOUT_PREPARATION_CLAIM_SCHEMA}),
        encoding="utf-8",
    )
    source.write_text(
        json.dumps(
            {
                "schema": fitter.HOLDOUT_PREPARATION_SOURCE_SCHEMA,
                "train_source": None,
                "sealed_holdout_source": {
                    "path": str(sealed_source),
                    "raw_artifact_sha256": fitter._sha256(sealed_source),
                },
            }
        ),
        encoding="utf-8",
    )
    artifact = {
        "preregistration": {
            "schema": "spc-cycle4-one-shot-protocol-v1",
            "sha256": "1" * 64,
        },
        "dataset_pairing_sha256": "2" * 64,
        "source_pairing_mode": "same-source-split",
        "source_combined_corpus": {
            "corpus_id": "source",
            "semantic_sha256": "3" * 64,
            "raw_artifact_sha256": "4" * 64,
        },
    }
    train_payload = {"schema": "train", "artifact": artifact}
    holdout_payload = {"schema": "holdout", "artifact": artifact}
    partial_train_path = output / "train-teacher-artifact.json"
    fitter._atomic_exclusive_json(
        partial_train_path,
        train_payload,
        conflict_message="train artifact already exists",
    )
    train_path, holdout_path = fitter._publish_pair_directory(
        output,
        train_payload,
        holdout_payload,
        claim,
        source,
    )
    assert json.loads(train_path.read_text(encoding="utf-8"))["schema"] == "train"
    assert json.loads(holdout_path.read_text(encoding="utf-8"))["schema"] == "holdout"
    (output / "pair-publication-receipt.json").unlink()
    resumed_train, resumed_holdout = fitter._publish_pair_directory(
        output,
        train_payload,
        holdout_payload,
        claim,
        source,
    )
    assert resumed_train == train_path and resumed_holdout == holdout_path
    with pytest.raises(FileExistsError, match="train artifact already differs"):
        fitter._publish_pair_directory(
            output,
            {"schema": "replacement-train", "artifact": artifact},
            {"schema": "replacement-holdout", "artifact": artifact},
            claim,
            source,
        )
    assert json.loads(train_path.read_text(encoding="utf-8"))["schema"] == "train"


def test_preregistration_rejects_runtime_and_match_drift(tmp_path: Path) -> None:
    path = tmp_path / "protocol.json"
    _write_preregistration(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["runtime"]["compiler"] = "MSVC 00.00 forged"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="runtime drifted exactly: compiler"):
        _load_preregistration(path, require_reservation=False)

    _write_preregistration(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["post_holdout_match"]["opening_suite"]["maximum_series"] = 7
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="match contract differs"):
        _load_preregistration(path, require_reservation=False)


def test_count_matching_combined_corpus_rejects_generation_contract_drift(
    tmp_path: Path,
) -> None:
    preregistration = _write_preregistration(tmp_path / "protocol.json")
    corpus = _split_artifact(
        preregistration, artifact_split="train", label_split="train"
    )
    corpus["generation"]["train_contract_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="generation train_contract_sha256 drifted"):
        fitter._validate_combined_corpus_preregistration(corpus, preregistration)


def test_train_artifact_rejects_holdout_label_before_materialization(
    tmp_path: Path,
) -> None:
    preregistration = _write_preregistration(tmp_path / "protocol.json")
    contaminated = _split_artifact(
        preregistration,
        artifact_split="train",
        label_split="holdout",
    )

    with pytest.raises(ValueError, match="contains cross-split labels"):
        _validate_split_artifact(
            contaminated,
            preregistration,
            expected_artifact_split="train",
        )


def test_paired_split_artifact_rejects_missing_or_wrong_generation_provenance(
    tmp_path: Path,
) -> None:
    preregistration = _write_preregistration(tmp_path / "protocol.json")
    artifact = _split_artifact(
        preregistration, artifact_split="train", label_split="train"
    )
    missing = dict(artifact)
    missing.pop("generation")
    with pytest.raises(ValueError, match="generation provenance is missing"):
        _validate_split_artifact(
            missing, preregistration, expected_artifact_split="train"
        )

    artifact["generation"]["train_contract_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="generation train_contract_sha256 drifted"):
        _validate_split_artifact(
            artifact, preregistration, expected_artifact_split="train"
        )


@pytest.mark.parametrize(
    ("train_roots", "train_finals", "holdout_roots", "holdout_finals"),
    (
        ({"x"}, set(), {"x"}, set()),
        (set(), {"x"}, set(), {"x"}),
        (set(), {"x"}, {"x"}, set()),
        ({"x"}, set(), set(), {"x"}),
    ),
)
def test_cross_artifact_semantic_contamination_fails_closed(
    train_roots: set[str],
    train_finals: set[str],
    holdout_roots: set[str],
    holdout_finals: set[str],
) -> None:
    with pytest.raises(ValueError, match="semantic contamination"):
        _require_clean_cross_artifact_split(
            train_roots=train_roots,
            train_finals=train_finals,
            holdout_roots=holdout_roots,
            holdout_finals=holdout_finals,
        )


def test_semantic_teacher_hash_ignores_runtime_and_created_timestamps() -> None:
    first = {
        "schema": "teacher",
        "labels": [{"value": 7}],
        "created_at": "first",
        "runtime": {"elapsed_seconds": 1.5},
        "source_raw_artifact_sha256": "1" * 64,
        "artifact": {
            "source_combined_corpus": {"raw_artifact_sha256": "3" * 64}
        },
    }
    replay = {
        **first,
        "created_at": "second",
        "runtime": {"elapsed_seconds": 99.0},
        "source_raw_artifact_sha256": "2" * 64,
        "artifact": {
            "source_combined_corpus": {"raw_artifact_sha256": "4" * 64}
        },
    }

    assert _teacher_semantic_sha256(first) == _teacher_semantic_sha256(replay)
    replay["labels"] = [{"value": 8}]
    assert _teacher_semantic_sha256(first) != _teacher_semantic_sha256(replay)


def test_model_identity_uses_semantic_hash_but_records_raw_artifact_hash() -> None:
    first = _model_payload(
        group="base7",
        ridge=0.01,
        coefficients=(1,) * 7,
        adverse_pair_weight=DEFAULT_ADVERSE_PAIR_WEIGHT,
        corpus_id="spc-native-mixed-teacher-example",
        corpus_semantic_sha256="1" * 64,
        corpus_raw_artifact_sha256="2" * 64,
    )
    replay = _model_payload(
        group="base7",
        ridge=0.01,
        coefficients=(1,) * 7,
        adverse_pair_weight=DEFAULT_ADVERSE_PAIR_WEIGHT,
        corpus_id="spc-native-mixed-teacher-example",
        corpus_semantic_sha256="1" * 64,
        corpus_raw_artifact_sha256="3" * 64,
    )

    assert first["model_id"] == replay["model_id"]
    assert (
        first["teacher_corpus_raw_artifact_sha256"]
        != replay["teacher_corpus_raw_artifact_sha256"]
    )


def test_label_payload_commitment_rejects_changed_score_with_same_keys(
    tmp_path: Path,
) -> None:
    preregistration = _write_preregistration(tmp_path / "protocol.json")
    artifact = _split_artifact(
        preregistration, artifact_split="train", label_split="train"
    )
    artifact["labels"][0]["options"][0]["score_white_heuristic_points"] = 999

    with pytest.raises(ValueError, match="label-payload commitment differs"):
        _validate_split_artifact(
            artifact, preregistration, expected_artifact_split="train"
        )


def test_json_artifact_parse_and_hash_share_one_byte_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "artifact.json"
    first = b'{"value":1}\n'
    replacement = b'{"value":2}\n'
    reads = 0

    def fake_read_bytes(self: Path) -> bytes:
        nonlocal reads
        assert self == path
        reads += 1
        return first if reads == 1 else replacement

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    payload, raw_sha = _read_json_artifact(path)

    assert reads == 1
    assert payload == {"value": 1}
    assert raw_sha == fitter.hashlib.sha256(first).hexdigest()


def test_stable_file_snapshot_rejects_path_swap_before_identity_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = (tmp_path / "artifact.json").resolve()
    replacement = (tmp_path / "replacement.json").resolve()
    artifact.write_bytes(b'{"artifact":"first"}\n')
    replacement.write_bytes(b'{"artifact":"replacement"}\n')
    original_stat = fitter.os.stat
    calls = 0

    def swapped_stat(path: object, *args: object, **kwargs: object) -> object:
        nonlocal calls
        if Path(path) == artifact:
            calls += 1
            if calls > 1:
                return original_stat(replacement, *args, **kwargs)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(fitter.os, "stat", swapped_stat)
    with pytest.raises(ValueError, match="changed during stable snapshot"):
        fitter._read_stable_file_snapshot(
            artifact, label="test central pair member"
        )
    assert calls == 2


def test_holdout_claim_precedes_every_sealed_touch_and_clones_share_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preregistration = Preregistration(
        path=tmp_path / "protocol.json",
        sha256="a" * 64,
        schema="spc-cycle4-one-shot-protocol-v1",
        expected_train_labels=1,
        expected_holdout_labels=1,
        manifest={
            "trajectory_corpora": {"sealed_holdout": {"seed": 202}},
            "frozen_implementation": {
                "scripts/fit_deep_teacher_value.py": "b" * 64
            },
        },
    )
    fit_receipt_path = tmp_path / "deep-teacher-fit-receipt.json"
    cloned_fit_receipt_path = tmp_path / "clone" / "deep-teacher-fit-receipt.json"
    train_path = tmp_path / "train.json"
    sealed_path = tmp_path / "sealed.json"
    sealed_path.write_bytes(b'{"sealed":true}\n')
    output = tmp_path / "holdout-output"
    clone_output = tmp_path / "clone-holdout-output"
    claim = tmp_path / "cycle4-seed-202-claim.json"
    model_path = tmp_path / "model.json"
    profile_path = tmp_path / "profile.json"
    leader_path = tmp_path / "leader.json"
    script_sha = "b" * 64
    generic_sha = "c" * 64
    pair_completion_path = (tmp_path / "pair-completion.json").resolve()
    train_semantic_sha = "d" * 64
    train_raw_sha = "e" * 64
    train_integrity = {
        "artifact_split": "train",
        "source_combined_corpus_id": "spc-native-mixed-teacher-source",
        "source_combined_corpus_semantic_sha256": "f" * 64,
        "source_combined_corpus_raw_artifact_sha256": "0" * 64,
        "artifact_semantic_sha256": train_semantic_sha,
        "dataset_pairing_sha256": "1" * 64,
        "semantic_keys_sha256": "2" * 64,
        "counterpart_semantic_keys_sha256": "3" * 64,
        "label_payload_sha256": "4" * 64,
        "counterpart_label_payload_sha256": "5" * 64,
        "source_cross_split_audit_sha256": "6" * 64,
        "root_state_keys": [],
        "option_final_state_keys": [],
    }
    pair_publication = {
        "path": str((tmp_path / "pair-publication-receipt.json").resolve()),
        "raw_artifact_sha256": "7" * 64,
        "train_path": str(train_path.resolve()),
        "train_raw_artifact_sha256": train_raw_sha,
        "sealed_holdout_path": str(sealed_path.resolve()),
        "sealed_holdout_raw_artifact_sha256": "8" * 64,
    }
    pair_completion = {
        "local_publication": {
            "path": pair_publication["path"],
            "raw_artifact_sha256": pair_publication["raw_artifact_sha256"],
        },
        "train": {
            "path": pair_publication["train_path"],
            "raw_artifact_sha256": pair_publication[
                "train_raw_artifact_sha256"
            ],
        },
        "sealed_holdout": {
            "path": pair_publication["sealed_holdout_path"],
            "raw_artifact_sha256": pair_publication[
                "sealed_holdout_raw_artifact_sha256"
            ],
        },
    }
    fit_receipt = {
        "schema": fitter.FIT_RECEIPT_SCHEMA,
        "one_shot_holdout": {
            "schema": fitter.HOLDOUT_CLAIM_BINDING_SCHEMA,
            "claim_path": str(claim.resolve()),
        },
        "inputs": {
            "preregistration_schema": preregistration.schema,
            "preregistration_sha256": preregistration.sha256,
            "artifact_split": "train",
            "train_artifact": str(train_path),
            "train_artifact_semantic_sha256": train_semantic_sha,
            "train_artifact_raw_sha256": train_raw_sha,
            "leader_profile_sha256": generic_sha,
        },
        "runtime": {
            "script_sha256": script_sha,
            "implementation_sha256": {"implementation": generic_sha},
        },
        "feature_contract": {"feature_module_sha256": generic_sha},
        "split_integrity": {
            "schema": SPLIT_INTEGRITY_SCHEMA,
            **train_integrity,
        },
        "models": {
            role: {"path": str(model_path), "sha256": generic_sha}
            for role in ("primary_nonroute", "route_ablation")
        },
        "profile": {"path": str(profile_path), "sha256": generic_sha},
        "pair_publication": pair_publication,
        "pair_completion": {
            "path": str(pair_completion_path),
            "raw_artifact_sha256": generic_sha,
        },
    }
    sealed_reads = 0
    sealed_hashes = 0
    supplied_train_raw_sha = train_raw_sha

    original_resolve = Path.resolve
    original_stat = Path.stat
    original_open = Path.open
    original_read_bytes = Path.read_bytes
    original_artifact_read = fitter._read_json_artifact

    protected_inputs = {
        fit_receipt_path,
        cloned_fit_receipt_path,
        train_path,
        sealed_path,
        model_path,
        profile_path,
        leader_path,
    }

    def assert_claim_for_caller_input(path: Path) -> None:
        if path in protected_inputs:
            assert claim.exists(), "caller-controlled input was touched before claim"

    def guarded_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        assert_claim_for_caller_input(self)
        return original_resolve(self, *args, **kwargs)

    def guarded_stat(self: Path, *args: object, **kwargs: object) -> object:
        assert_claim_for_caller_input(self)
        return original_stat(self, *args, **kwargs)

    def guarded_open(self: Path, *args: object, **kwargs: object) -> object:
        assert_claim_for_caller_input(self)
        return original_open(self, *args, **kwargs)

    def guarded_read_bytes(self: Path) -> bytes:
        assert_claim_for_caller_input(self)
        return original_read_bytes(self)

    def fake_load_json(path: Path) -> dict[str, object]:
        if path in {fit_receipt_path, cloned_fit_receipt_path}:
            return fit_receipt
        raise AssertionError(f"unexpected JSON read: {path}")

    def fake_read_json_artifact(path: Path) -> tuple[dict[str, object], str]:
        nonlocal sealed_reads, sealed_hashes
        assert_claim_for_caller_input(path)
        if path in {fit_receipt_path, cloned_fit_receipt_path}:
            return fit_receipt, generic_sha
        if path == claim:
            return original_artifact_read(path)
        if path == train_path:
            return {"kind": "train"}, supplied_train_raw_sha
        if path == sealed_path:
            sealed_reads += 1
            path.stat()
            with path.open("rb") as stream:
                assert stream.read(1) == b"{"
            raw = path.read_bytes()
            fitter.hashlib.sha256(raw).hexdigest()
            sealed_hashes += 1
            raise ValueError("synthetic sealed parse failure")
        raise AssertionError(f"unexpected artifact read: {path}")

    script_hashes = 0

    def fake_sha256(path: Path) -> str:
        nonlocal script_hashes
        if path == Path(fitter.__file__).resolve():
            assert claim.exists(), "evaluator script opened before holdout claim"
            script_hashes += 1
            return script_sha
        return generic_sha

    def load_completed_pair_preregistration(
        _path: Path, **kwargs: object
    ) -> Preregistration:
        assert kwargs == {"require_pair_completion": True}
        return preregistration

    monkeypatch.setattr(
        fitter, "_load_preregistration", load_completed_pair_preregistration
    )
    monkeypatch.setattr(fitter, "_holdout_claim_path", lambda _: claim.resolve())
    monkeypatch.setattr(
        fitter, "_pair_completion_registry_path", lambda _: pair_completion_path
    )
    monkeypatch.setattr(
        fitter,
        "_trusted_pair_completion",
        lambda _: (pair_completion, generic_sha),
    )
    monkeypatch.setattr(fitter, "_load_json", fake_load_json)
    monkeypatch.setattr(fitter, "_read_json_artifact", fake_read_json_artifact)
    monkeypatch.setattr(fitter, "_sha256", fake_sha256)
    monkeypatch.setattr(fitter, "_reject_quarantined_holdout", lambda _: None)
    monkeypatch.setattr(
        fitter,
        "_validate_pair_publication",
        lambda *_args, **_kwargs: pair_publication,
    )
    monkeypatch.setattr(
        fitter,
        "_validate_split_artifact",
        lambda _corpus, _preregistration, *, expected_artifact_split: (
            train_integrity
            if expected_artifact_split == "train"
            else (_ for _ in ()).throw(AssertionError("sealed validation reached"))
        ),
    )
    monkeypatch.setattr(
        fitter,
        "_teacher_semantic_sha256",
        lambda corpus: train_semantic_sha if corpus.get("kind") == "train" else "9" * 64,
    )
    monkeypatch.setattr(fitter, "_materialize_labels", lambda *_args, **_kwargs: ((), {}))
    monkeypatch.setattr(
        fitter,
        "_implementation_hashes",
        lambda: {"implementation": generic_sha},
    )
    def read_profile(path: Path) -> tuple[SimpleNamespace, str]:
        assert_claim_for_caller_input(path)
        return SimpleNamespace(), generic_sha

    def read_model(path: Path) -> tuple[dict[str, str], str]:
        assert_claim_for_caller_input(path)
        return (
            {
                "teacher_corpus_semantic_sha256": train_semantic_sha,
                "teacher_corpus_raw_artifact_sha256": train_raw_sha,
            },
            generic_sha,
        )

    monkeypatch.setattr(fitter, "_read_profile_artifact", read_profile)
    monkeypatch.setattr(fitter, "_read_model_artifact", read_model)
    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    with pytest.raises(ValueError, match="synthetic sealed parse failure"):
        _evaluate_holdout_command(
            SimpleNamespace(
                preregistration=preregistration.path,
                teacher_corpus=sealed_path,
                leader_profile=leader_path,
                fit_receipt=fit_receipt_path,
                output=output,
            )
        )
    assert claim.exists()
    assert sealed_reads == 1
    assert sealed_hashes == 1
    assert script_hashes >= 1

    with pytest.raises(FileExistsError, match="already been opened"):
        _evaluate_holdout_command(
            SimpleNamespace(
                preregistration=preregistration.path,
                teacher_corpus=sealed_path,
                leader_profile=leader_path,
                fit_receipt=cloned_fit_receipt_path,
                output=clone_output,
            )
        )
    assert sealed_reads == 1
