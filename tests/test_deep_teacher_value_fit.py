from __future__ import annotations

import json
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
    monkeypatch.setattr(fitter, "_repository_root", lambda: second_root)
    second_claim = fitter._holdout_claim_path(copied_or_refit)

    assert first_claim == second_claim
    assert common_dir in first_claim.parents


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
        },
        "runtime": {
            "platform": f"{fitter.platform.system()} x86-64",
            "python": f"{fitter.sys.version_info.major}.{fitter.sys.version_info.minor}",
            "compiler": (
                f"MSVC {compiler_match.group(1)}.{compiler_match.group(2)} test"
            ),
            "native_eval_binary_sha256": fitter._sha256(
                Path(fitter.evaluation.__file__).resolve()
            ),
            "native_mate_binary_sha256": fitter._sha256(
                Path(fitter.series_mate.__file__).resolve()
            ),
        },
        "profiles": {
            "ordered_source_schedule": schedule,
            "rejected_development_leader": {
                "path": leader_path.relative_to(root).as_posix(),
                "profile_id": fitter.load_profile(leader_path).profile_id,
                "sha256": fitter._sha256(leader_path),
            },
        },
        "trajectory_corpora": {
            "train": {"seed": 101, "attempts": 2},
            "sealed_holdout": {"seed": 202, "attempts": 2, "one_shot": True},
            "shared_config": {
                "first_attempt": 0,
                "shard_size": 1,
                "batch_size": 1,
                "workers": 1,
                "max_attempt_series": 64,
                "max_frontier_states": 32,
                "candidate_count": 16,
                "max_positions_per_series": 250_000,
                "max_positions_per_game": 10_000_000,
                "policy": "uniform",
                "profile_schedule": "ordered-pair-round-robin",
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
            "seed_burn_rule": "any opened holdout is consumed",
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
        "preflight": {"holdout_consumed": False, "generation_started": False},
        "frozen_implementation": fitter._current_frozen_implementation(),
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return _load_preregistration(path)


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
    dataset_pairing_sha = fitter._dataset_pairing_sha256(
        preregistration_sha256=preregistration.sha256,
        train_semantic_keys_sha256=train_semantic_sha,
        holdout_semantic_keys_sha256=holdout_semantic_sha,
        train_label_payload_sha256=train_label_payload_sha,
        holdout_label_payload_sha256=holdout_label_payload_sha,
        cross_split_audit_sha256="6" * 64,
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
            "source_combined_corpus": {
                "corpus_id": "spc-native-mixed-teacher-source",
                "semantic_sha256": "3" * 64,
                "raw_artifact_sha256": "4" * 64,
            },
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
        _load_preregistration(path)

    _write_preregistration(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["one_shot_gates"]["each_candidate_must"][-1] = "weaker gate"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="candidate holdout gates differ"):
        _load_preregistration(path)

    _write_preregistration(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["runtime"] = None
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="runtime contract is malformed"):
        _load_preregistration(path)


def test_count_matching_combined_corpus_rejects_generation_contract_drift(
    tmp_path: Path,
) -> None:
    preregistration = _write_preregistration(tmp_path / "protocol.json")
    corpus = {
        "schema": fitter.CORPUS_SCHEMA,
        "method": fitter.CORPUS_METHOD,
        "engine_version": fitter.ENGINE_VERSION,
        "source_fingerprint": fitter.ENGINE_SOURCE_FINGERPRINT,
        "generation": {"train_contract_sha256": "0" * 64},
    }

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
            "trajectory_corpora": {"sealed_holdout": {"seed": 202}}
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
    }
    sealed_reads = 0
    sealed_hashes = 0
    supplied_train_raw_sha = "0" * 64

    original_resolve = Path.resolve
    original_stat = Path.stat
    original_open = Path.open
    original_read_bytes = Path.read_bytes

    def assert_claim_for_sealed(path: Path) -> None:
        if path == sealed_path:
            assert claim.exists(), "sealed artifact was touched before claim creation"

    def guarded_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        assert_claim_for_sealed(self)
        return original_resolve(self, *args, **kwargs)

    def guarded_stat(self: Path, *args: object, **kwargs: object) -> object:
        assert_claim_for_sealed(self)
        return original_stat(self, *args, **kwargs)

    def guarded_open(self: Path, *args: object, **kwargs: object) -> object:
        assert_claim_for_sealed(self)
        return original_open(self, *args, **kwargs)

    def guarded_read_bytes(self: Path) -> bytes:
        assert_claim_for_sealed(self)
        return original_read_bytes(self)

    def fake_load_json(path: Path) -> dict[str, object]:
        if path in {fit_receipt_path, cloned_fit_receipt_path}:
            return fit_receipt
        raise AssertionError(f"unexpected JSON read: {path}")

    def fake_read_json_artifact(path: Path) -> tuple[dict[str, object], str]:
        nonlocal sealed_reads, sealed_hashes
        if path in {fit_receipt_path, cloned_fit_receipt_path}:
            return fit_receipt, generic_sha
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

    def fake_sha256(path: Path) -> str:
        if path == Path(fitter.__file__).resolve():
            return script_sha
        return generic_sha

    monkeypatch.setattr(fitter, "_load_preregistration", lambda _: preregistration)
    monkeypatch.setattr(fitter, "_holdout_claim_path", lambda _: claim.resolve())
    monkeypatch.setattr(fitter, "_load_json", fake_load_json)
    monkeypatch.setattr(fitter, "_read_json_artifact", fake_read_json_artifact)
    monkeypatch.setattr(fitter, "_sha256", fake_sha256)
    monkeypatch.setattr(fitter, "_reject_quarantined_holdout", lambda _: None)
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
    monkeypatch.setattr(
        fitter,
        "_read_profile_artifact",
        lambda _: (SimpleNamespace(), generic_sha),
    )
    monkeypatch.setattr(
        fitter,
        "_read_model_artifact",
        lambda _: (
            {
                "teacher_corpus_semantic_sha256": train_semantic_sha,
                "teacher_corpus_raw_artifact_sha256": train_raw_sha,
            },
            generic_sha,
        ),
    )
    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    with pytest.raises(ValueError, match="raw bytes changed"):
        _evaluate_holdout_command(
            SimpleNamespace(
                preregistration=preregistration.path,
                teacher_corpus=sealed_path,
                leader_profile=leader_path,
                fit_receipt=fit_receipt_path,
                output=output,
            )
        )
    assert not claim.exists()
    assert sealed_reads == 0

    supplied_train_raw_sha = train_raw_sha

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
