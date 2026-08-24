from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

import scottish_progressive.evaluation as evaluation
from scottish_progressive.cli import build_parser
from scottish_progressive.deep_teacher_overlay import (
    DEEP_TEACHER_FIXED_POINT_SCALE,
    DEEP_TEACHER_MODEL_SCHEMA,
    DEEP_TEACHER_TERMINAL_POLICY,
    DeepTeacherOverlayPayload,
    _rounded_fixed_point,
    build_deep_teacher_overlay,
    load_deep_teacher_overlay_payload,
    reconstruct_deep_teacher_variant_id,
)
from scottish_progressive.league import GameRecord, _play_game
from scottish_progressive.model import ProgressiveState
from scottish_progressive.profiles import baseline_profile, mutate_profile
from scottish_progressive.strength import (
    StrengthMatchConfig,
    _build_jobs,
    _game_payload,
    _summarize,
)
from scottish_progressive.teacher_value_features import (
    TEACHER_VALUE_FEATURE_NAMES,
    TEACHER_VALUE_FEATURE_SCHEMA,
)


def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _model(group: str = "base7") -> dict[str, object]:
    feature_count = {
        "base7": 7,
        "phase14": 14,
        "cached19": 19,
        "positional38": 38,
        "direct44": 44,
        "all47": 47,
    }[group]
    coefficients = [0] * feature_count
    coefficients[0] = DEEP_TEACHER_FIXED_POINT_SCALE
    core: dict[str, object] = {
        "schema": DEEP_TEACHER_MODEL_SCHEMA,
        "feature_schema": TEACHER_VALUE_FEATURE_SCHEMA,
        "feature_group": group,
        "feature_names": list(TEACHER_VALUE_FEATURE_NAMES[:feature_count]),
        "fixed_point_scale": DEEP_TEACHER_FIXED_POINT_SCALE,
        "coefficients": coefficients,
        "ridge": 0.01,
        "adverse_pair_weight": 8.0,
        "terminal_override": DEEP_TEACHER_TERMINAL_POLICY,
        "teacher_corpus_id": "spc-native-mixed-teacher-fixture",
        "teacher_corpus_sha256": "a" * 64,
        "teacher_corpus_semantic_sha256": "a" * 64,
        "teacher_corpus_raw_artifact_sha256": "b" * 64,
    }
    model_identity = {
        key: value
        for key, value in core.items()
        if key != "teacher_corpus_raw_artifact_sha256"
    }
    return {
        **core,
        "model_id": (
            "spc-dtv-"
            + hashlib.sha256(_canonical_json(model_identity)).hexdigest()[:20]
        ),
    }


def _write_model(path: Path, model: dict[str, object]) -> Path:
    path.write_text(
        json.dumps(model, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return path


def _payload(
    tmp_path: Path,
    *,
    group: str = "base7",
) -> tuple[DeepTeacherOverlayPayload, object, object]:
    reference = baseline_profile()
    candidate = mutate_profile(reference, seed=404, name="candidate")
    path = _write_model(tmp_path / f"{group}.json", _model(group))
    return load_deep_teacher_overlay_payload(path, candidate), candidate, reference


@pytest.mark.parametrize(
    ("raw_score", "expected"),
    (
        (499_999_999, 0),
        (500_000_000, 1),
        (-499_999_999, 0),
        (-500_000_000, -1),
        (1_500_000_000, 2),
        (-1_500_000_000, -2),
    ),
)
def test_fixed_point_score_conversion_is_symmetric_half_away_from_zero(
    raw_score: int,
    expected: int,
) -> None:
    assert _rounded_fixed_point(raw_score, DEEP_TEACHER_FIXED_POINT_SCALE) == expected


def test_strict_model_loader_and_native_overlay_receipt(tmp_path: Path) -> None:
    payload, candidate, _reference = _payload(tmp_path, group="all47")
    overlay = build_deep_teacher_overlay(payload, candidate)
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/4Q3/7K w - - 0 1",
        3,
    )

    scored = overlay.score_with_work(state, 123_456, None)

    assert scored.score == 975
    assert scored.reach_positions >= 0
    assert scored.direct_move_variants > 0
    assert scored.two_move_variants > 0
    assert scored.work_positions == (
        scored.reach_positions
        + scored.direct_move_variants
        + scored.two_move_variants
    )
    assert scored.complete is True
    assert overlay.variant_id == payload.variant_id
    assert payload.model_sha256 == hashlib.sha256(
        (tmp_path / "all47.json").read_bytes()
    ).hexdigest()


@pytest.mark.parametrize(
    "group",
    ("base7", "phase14", "cached19", "positional38", "direct44", "all47"),
)
def test_every_frozen_feature_prefix_scores_through_native_overlay(
    tmp_path: Path,
    group: str,
) -> None:
    payload, candidate, _reference = _payload(tmp_path, group=group)
    overlay = build_deep_teacher_overlay(payload, candidate)

    result = overlay.score_with_work(
        ProgressiveState.from_fen(
            "7k/8/8/8/8/8/4Q3/7K w - - 0 1",
            3,
        ),
        0,
        None,
    )

    assert type(result.score) is int
    assert result.complete is True
    if payload.feature_count < 44:
        assert result.direct_move_variants == 0
        assert result.two_move_variants == 0
    elif payload.feature_count == 44:
        assert result.direct_move_variants > 0
        assert result.two_move_variants == 0
    else:
        assert result.direct_move_variants > 0
        assert result.two_move_variants > 0


def test_leaf_hot_path_uses_worker_validated_in_memory_native_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload, candidate, _reference = _payload(tmp_path)
    overlay = build_deep_teacher_overlay(payload, candidate)

    def unexpected_rehash() -> None:
        raise AssertionError("unexpected source rehash")

    monkeypatch.setattr(
        evaluation,
        "_native_source_identity",
        unexpected_rehash,
    )

    result = overlay.score_with_work(ProgressiveState.initial(), 0, None)

    assert type(result.score) is int


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda model: model.update(fixed_point_scale=100), "scale"),
        (lambda model: model.update(adverse_pair_weight=0.5), "adverse_pair_weight"),
        (lambda model: model.update(coefficients=[True] * 7), "integer"),
        (lambda model: model.update(feature_names=["wrong"] * 7), "order"),
        (lambda model: model.update(extra="field"), "keys differ"),
    ],
)
def test_model_loader_rejects_malformed_contract(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    candidate = mutate_profile(baseline_profile(), seed=99, name="candidate")
    model = _model()
    mutation(model)
    path = _write_model(tmp_path / "bad.json", model)

    with pytest.raises((TypeError, ValueError), match=message):
        load_deep_teacher_overlay_payload(path, candidate)


def test_model_loader_rejects_validly_rehashed_bad_adverse_weight(
    tmp_path: Path,
) -> None:
    candidate = mutate_profile(baseline_profile(), seed=100, name="candidate")
    model = _model()
    model["adverse_pair_weight"] = 0.5
    core = {
        key: value
        for key, value in model.items()
        if key not in {"model_id", "teacher_corpus_raw_artifact_sha256"}
    }
    model["model_id"] = (
        "spc-dtv-" + hashlib.sha256(_canonical_json(core)).hexdigest()[:20]
    )

    with pytest.raises(ValueError, match="adverse_pair_weight"):
        load_deep_teacher_overlay_payload(
            _write_model(tmp_path / "bad-weight.json", model),
            candidate,
        )


def test_raw_corpus_provenance_is_retained_but_excluded_from_model_id(
    tmp_path: Path,
) -> None:
    candidate = mutate_profile(baseline_profile(), seed=101, name="candidate")
    original = _model()
    changed = {**original, "teacher_corpus_raw_artifact_sha256": "c" * 64}

    first = load_deep_teacher_overlay_payload(
        _write_model(tmp_path / "first.json", original),
        candidate,
    )
    second = load_deep_teacher_overlay_payload(
        _write_model(tmp_path / "second.json", changed),
        candidate,
    )

    assert first.model_id == second.model_id
    assert first.teacher_corpus_raw_artifact_sha256 == "b" * 64
    assert second.teacher_corpus_raw_artifact_sha256 == "c" * 64
    assert first.variant_id != second.variant_id


def test_legacy_and_semantic_corpus_identities_must_match(tmp_path: Path) -> None:
    candidate = mutate_profile(baseline_profile(), seed=102, name="candidate")
    model = _model()
    model["teacher_corpus_semantic_sha256"] = "d" * 64

    with pytest.raises(ValueError, match="legacy and semantic"):
        load_deep_teacher_overlay_payload(
            _write_model(tmp_path / "corpus-mismatch.json", model),
            candidate,
        )


def test_candidate_overlay_is_color_swapped_and_plain_jobs_are_unchanged(
    tmp_path: Path,
) -> None:
    payload, candidate, reference = _payload(tmp_path)
    config = StrengthMatchConfig.smoke()
    plain = _build_jobs(candidate, reference, config)
    repeated = _build_jobs(candidate, reference, config)
    modeled = _build_jobs(
        candidate,
        reference,
        config,
        candidate_value_model=payload,
    )

    assert [job.job_key for job in plain] == [job.job_key for job in repeated]
    assert all(
        job.white_evaluation_overlay is None
        and job.black_evaluation_overlay is None
        for job in plain
    )
    assert modeled[0].white_profile.profile_id == candidate.profile_id
    assert modeled[0].white_evaluation_overlay == payload
    assert modeled[0].black_evaluation_overlay is None
    assert modeled[1].black_profile.profile_id == candidate.profile_id
    assert modeled[1].black_evaluation_overlay == payload
    assert modeled[1].white_evaluation_overlay is None
    assert modeled[0].run_id == modeled[1].run_id
    assert modeled[0].run_id != plain[0].run_id
    assert {job.job_key for job in modeled}.isdisjoint(
        job.job_key for job in plain
    )

    baseline = baseline_profile()
    baseline_payload = load_deep_teacher_overlay_payload(
        _write_model(tmp_path / "baseline-model.json", _model()),
        baseline,
    )
    with pytest.raises(ValueError, match="different engine profiles"):
        _build_jobs(baseline, baseline, config)
    evaluator_only = _build_jobs(
        baseline,
        baseline,
        config,
        candidate_value_model=baseline_payload,
    )
    assert evaluator_only[0].white_evaluation_overlay == baseline_payload
    assert evaluator_only[0].black_evaluation_overlay is None
    assert evaluator_only[1].white_evaluation_overlay is None
    assert evaluator_only[1].black_evaluation_overlay == baseline_payload
    assert all(
        job.white_profile.as_dict() == baseline.as_dict()
        and job.black_profile.as_dict() == baseline.as_dict()
        for job in evaluator_only
    )


def _finished_record(job, result: str) -> GameRecord:
    state = job.opening.state()
    return GameRecord(
        job.job_key,
        job.run_id,
        job.generation,
        job.stage,
        job.opening_index,
        job.opening.case_id,
        job.opening_suite_version,
        job.seed,
        job.white_profile.profile_id,
        job.black_profile.profile_id,
        result,
        "checkmate",
        (
            job.white_profile.profile_id
            if result == "1-0"
            else job.black_profile.profile_id
        ),
        None,
        state.pfen,
        state.pfen,
        1,
        (),
    )


def test_same_profile_match_scores_the_explicit_candidate_seat(
    tmp_path: Path,
) -> None:
    baseline = baseline_profile()
    payload = load_deep_teacher_overlay_payload(
        _write_model(tmp_path / "same-profile.json", _model()),
        baseline,
    )
    jobs = _build_jobs(
        baseline,
        baseline,
        StrengthMatchConfig.smoke(),
        candidate_value_model=payload,
    )
    # Candidate is White in game zero and Black in game one. Both colors win,
    # so role-aware accounting must award two candidate wins even though both
    # sides carry the exact same base profile identity.
    records = (
        _finished_record(jobs[0], "1-0"),
        _finished_record(jobs[1], "0-1"),
    )
    candidate_colors = {
        jobs[0].job_key: True,
        jobs[1].job_key: False,
    }

    summary, pairs = _summarize(
        records,
        baseline,
        baseline,
        payload,
        candidate_colors,
    )

    assert summary["candidate_game_wdl"] == {
        "wins": 2,
        "draws": 0,
        "losses": 0,
    }
    assert pairs[0]["candidate_points"] == 2.0
    with pytest.raises(ValueError, match="candidate-color identity map"):
        _summarize(records, baseline, baseline, payload, {})


def test_plain_game_payload_keeps_the_pre_overlay_report_shape(
    tmp_path: Path,
) -> None:
    payload, candidate, reference = _payload(tmp_path)
    plain_job = _build_jobs(
        candidate,
        reference,
        StrengthMatchConfig.smoke(),
    )[0]
    modeled_job = _build_jobs(
        candidate,
        reference,
        StrengthMatchConfig.smoke(),
        candidate_value_model=payload,
    )[0]
    record = _finished_record(plain_job, "1-0")

    plain = _game_payload(record, plain_job.opening, plain_job)
    modeled = _game_payload(record, modeled_job.opening, modeled_job)

    assert "engine_failure_engine_id" not in plain
    assert "white_engine_id" not in plain
    assert "black_engine_id" not in plain
    assert modeled["white_engine_id"] == payload.variant_id
    assert modeled["black_engine_id"] == reference.profile_id


def test_payload_reconstructs_inside_process_pool(tmp_path: Path) -> None:
    payload, candidate, _reference = _payload(tmp_path)

    with ProcessPoolExecutor(max_workers=1) as executor:
        result = executor.submit(
            reconstruct_deep_teacher_variant_id,
            payload,
            candidate,
        ).result(timeout=30)

    assert result == payload.variant_id


def test_modeled_game_job_reconstructs_and_fails_closed_in_process_pool(
    tmp_path: Path,
) -> None:
    payload, candidate, reference = _payload(tmp_path, group="all47")
    job = _build_jobs(
        candidate,
        reference,
        StrengthMatchConfig(
            pairs=1,
            search_depth=1,
            max_series_per_node=1,
            max_generation_positions=1_000,
            max_game_work_positions=1_000,
            opening_case_ids=("initial",),
        ),
        candidate_value_model=payload,
    )[0]

    with ProcessPoolExecutor(max_workers=1) as executor:
        result = executor.submit(_play_game, job).result(timeout=30)

    assert result.result == "*"
    assert result.terminal_reason == "engine-work-limit"
    assert result.engine_failure_engine_id == payload.variant_id
    assert result.trace[0]["engine_variant_id"] == payload.variant_id


def test_profile_or_variant_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    payload, candidate, reference = _payload(tmp_path)
    with pytest.raises(ValueError, match="different profile"):
        _build_jobs(
            candidate,
            reference,
            StrengthMatchConfig.smoke(),
            candidate_value_model=replace(
                payload,
                base_profile_id=reference.profile_id,
            ),
        )

    job = _build_jobs(
        candidate,
        reference,
        StrengthMatchConfig.smoke(),
        candidate_value_model=payload,
    )[0]
    corrupted = replace(
        job,
        white_evaluation_overlay=replace(payload, variant_id="spc-dtv-variant-bad"),
    )
    result = _play_game(corrupted)
    assert result.result == "*"
    assert result.terminal_reason == "engine-overlay-invalid"
    assert result.engine_failure_profile_id == candidate.profile_id


def test_overlay_work_limit_is_incomplete_not_a_played_result(tmp_path: Path) -> None:
    payload, candidate, reference = _payload(tmp_path, group="all47")
    config = StrengthMatchConfig(
        pairs=1,
        search_depth=1,
        max_series_per_node=1,
        max_generation_positions=1_000,
        max_game_work_positions=1_000,
        emergency_max_series=None,
        opening_case_ids=("initial",),
    )
    job = _build_jobs(
        candidate,
        reference,
        config,
        candidate_value_model=payload,
    )[0]

    result = _play_game(job)

    assert result.result == "*"
    assert result.terminal_reason == "engine-work-limit"
    assert result.engine_failure_profile_id == candidate.profile_id
    assert result.trace
    assert result.trace[0]["played"] is False
    assert result.trace[0]["engine_variant_id"] == payload.variant_id


def test_strength_parser_exposes_candidate_only_model_option() -> None:
    args = build_parser().parse_args(
        [
            "strength-match",
            "candidate.json",
            "baseline",
            "--candidate-value-model",
            "model.json",
        ]
    )

    assert args.candidate_value_model == "model.json"
