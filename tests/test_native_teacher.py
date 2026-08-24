from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from threading import Barrier

import chess
import pytest

from scripts.build_native_teacher_corpus import _cross_tier_forbidden_state_keys

from scottish_progressive.corpus_samples import NativeBoundarySample
from scottish_progressive.corpus_shards import progressive_state_dedup_key
from scottish_progressive.evaluation import evaluate
from scottish_progressive.model import ProgressiveState
from scottish_progressive.native_corpus import (
    NativeCorpusConfig,
    NativeTerminal,
    bind_native_profiles,
)
from scottish_progressive.native_teacher import (
    NativeTeacherConfig,
    _BucketJob,
    _Candidate,
    _balanced_quotas,
    _complete_result_or_reason,
    _mate_distance,
    _resolve_receipt_cache_contract,
    _validate_replacement_cache_source,
    _run_bucket,
    _run_buckets_parallel,
    _single_candidate_job,
    merge_native_teacher_tiers,
)
from scottish_progressive.profiles import baseline_profile, create_population
from scottish_progressive.rules import play_series
from scottish_progressive.search import (
    MATE_SCORE,
    SearchResult,
    SearchStats,
    ScoredSeries,
)


def _teacher_config() -> NativeTeacherConfig:
    return NativeTeacherConfig(
        target_roots=2,
        train_roots=1,
        minimum_series=1,
        maximum_series=1,
        depth_series=3,
        branch_cap=32,
        max_generation_positions=10_000,
        hard_negative_count=1,
        workers=1,
        expected_train_attempts=1,
        expected_holdout_attempts=1,
    )


def _candidate() -> _Candidate:
    state = ProgressiveState.initial()
    profile = baseline_profile()
    return _Candidate(
        split="train",
        attempt_index=0,
        sequence_index=0,
        state_key_sha256=progressive_state_dedup_key(state).hex(),
        sample=NativeBoundarySample(
            state=state,
            white_profile_index=0,
            black_profile_index=0,
            terminal=NativeTerminal.CHECKMATE_WHITE,
            value_for_side_to_move=1,
        ),
        source_profile_id=profile.profile_id,
        white_profile_id=profile.profile_id,
        black_profile_id=profile.profile_id,
        source_series_remaining=1,
    )


def _result(*, complete: bool) -> SearchResult:
    state = ProgressiveState.initial()
    e4 = play_series(state, ("e2e4",))
    d4 = play_series(state, ("d2d4",))
    alternatives = (
        ScoredSeries(e4, 25, (), (-1, 1)),
        ScoredSeries(d4, 10, (), (-1, 1)),
    )
    return SearchResult(
        score=25,
        best_series=e4,
        principal_variation=(e4,),
        alternatives=alternatives,
        requested_depth=3,
        completed_depth=3 if complete else 2,
        exact_width=False,
        timed_out=False,
        elapsed_seconds=0.01,
        stats=SearchStats(generation_positions=123),
        root_evaluation=evaluate(state, baseline_profile()),
        proof=None,
        max_series_per_node=32,
        work_limit_reached=not complete,
        max_generation_positions=10_000,
        root_scores_complete=complete,
    )


def _job(candidate: _Candidate, receipt_root: str = ".teacher-test-receipts") -> _BucketJob:
    profile = baseline_profile()
    return _BucketJob(
        split="train",
        source_profile_id=profile.profile_id,
        series_number=1,
        quota=1,
        candidates=(candidate,),
        source_config=NativeCorpusConfig(),
        source_profiles=bind_native_profiles((profile,)),
        teacher_profile=profile,
        teacher_config=_teacher_config(),
        receipt_root=receipt_root,
        cache_contract_id="spc-native-teacher-cache-test",
    )


def test_balanced_quotas_cover_profiles_series_and_split_exactly() -> None:
    config = NativeTeacherConfig()
    profiles = tuple(
        profile.profile_id
        for profile in create_population(baseline_profile(), size=4, seed=3101)
    )
    quotas = _balanced_quotas(profiles, config)
    assert sum(value for key, value in quotas.items() if key[0] == "train") == 128
    assert sum(value for key, value in quotas.items() if key[0] == "holdout") == 64
    for profile_id in profiles:
        assert sum(
            value
            for (split, profile, _series), value in quotas.items()
            if split == "train" and profile == profile_id
        ) == 32
        assert sum(
            value
            for (split, profile, _series), value in quotas.items()
            if split == "holdout" and profile == profile_id
        ) == 16
    for profile_id in profiles:
        for series in range(4, 10):
            assert quotas[("train", profile_id, series)] + quotas[
                ("holdout", profile_id, series)
            ] == 8


def test_incomplete_search_is_never_accepted_or_source_recovered(
    monkeypatch, tmp_path
) -> None:
    candidate = _candidate()
    monkeypatch.setattr(
        "scottish_progressive.native_teacher.analyze",
        lambda *_args, **_kwargs: _result(complete=False),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("incomplete labels must not recover/cache a source move")

    monkeypatch.setattr(
        "scottish_progressive.native_teacher._recover_source_series", forbidden
    )
    result = _run_bucket(_job(candidate, str(tmp_path)))
    assert result["accepted"] == []
    assert result["failures"][0]["reason"] == "work-limit"


def test_complete_search_retains_options_features_pv_and_hard_negative(
    monkeypatch, tmp_path
) -> None:
    candidate = _candidate()
    monkeypatch.setattr(
        "scottish_progressive.native_teacher.analyze",
        lambda *_args, **_kwargs: _result(complete=True),
    )
    monkeypatch.setattr(
        "scottish_progressive.native_teacher._recover_source_series",
        lambda *_args, **_kwargs: "d2d4",
    )
    job = _job(candidate, str(tmp_path))
    result = _run_bucket(job)
    assert result["failures"] == []
    label = result["accepted"][0]
    assert label["teacher_best_series"] == "e2e4"
    assert label["teacher_agrees_with_source_play"] is False
    assert label["source_played_regret_points"] == 15
    assert len(label["options"]) == 2
    source = next(item for item in label["options"] if item["is_source_played"])
    assert source["is_hard_negative"] is True
    assert source["principal_variation"][0]["series"] == "d2d4"
    assert source["final_state_key_sha256"]
    assert source["final_features"]["material"] == 0

    monkeypatch.setattr(
        "scottish_progressive.native_teacher.analyze",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("completed root must resume from its atomic receipt")
        ),
    )
    resumed = _run_bucket(job)
    assert resumed["accepted"] == result["accepted"]
    assert resumed["failures"] == []


def test_forbidden_train_option_final_state_uses_next_cached_candidate(
    monkeypatch, tmp_path
) -> None:
    first = _candidate()
    second = replace(first, state_key_sha256="f" * 64, attempt_index=1)
    forbidden_label = {
        "options": [{"final_state_key_sha256": "a" * 64}],
    }
    accepted_label = {
        "options": [{"final_state_key_sha256": "b" * 64}],
    }

    monkeypatch.setattr(
        "scottish_progressive.native_teacher._load_root_receipt",
        lambda _job, candidate: {
            "status": "complete",
            "label": forbidden_label if candidate is first else accepted_label,
        },
    )
    job = replace(
        _job(first, str(tmp_path)),
        candidates=(first, second),
        forbidden_state_keys=frozenset({"a" * 64}),
    )
    result = _run_bucket(job)

    assert result["accepted"] == [accepted_label]
    assert result["failures"] == []
    assert result["excluded"] == [
        {
            "split": "train",
            "source_profile_id": baseline_profile().profile_id,
            "series_number": 1,
            "state_key_sha256": first.state_key_sha256,
            "attempt_index": 0,
            "sequence_index": 0,
            "reason": "forbidden-cross-split-option-final-state",
            "forbidden_state_keys": ["a" * 64],
        }
    ]


def _scheduler_candidate(index: int) -> _Candidate:
    return replace(
        _candidate(),
        state_key_sha256=hashlib.sha256(f"scheduler-{index}".encode()).hexdigest(),
        attempt_index=index,
    )


def _scheduler_result(job: _BucketJob, status: str) -> dict[str, object]:
    candidate = job.candidates[0]
    row = {"state_key_sha256": candidate.state_key_sha256}
    return {
        "split": job.split,
        "source_profile_id": job.source_profile_id,
        "series_number": job.series_number,
        "quota": 1,
        "candidate_count": 1,
        "accepted": [row] if status == "accepted" else [],
        "failures": [row] if status == "failure" else [],
        "excluded": [row] if status == "excluded" else [],
        "elapsed_seconds": 0.0,
    }


def test_parallel_bucket_scheduler_commits_the_exact_ordered_quota_prefix(
    monkeypatch, tmp_path
) -> None:
    candidates = tuple(_scheduler_candidate(index) for index in range(5))
    statuses = {
        candidates[0].state_key_sha256: "failure",
        candidates[1].state_key_sha256: "accepted",
        candidates[2].state_key_sha256: "excluded",
        candidates[3].state_key_sha256: "accepted",
        candidates[4].state_key_sha256: "accepted",
    }
    visited: list[str] = []

    def fake_run(job: _BucketJob) -> dict[str, object]:
        candidate = job.candidates[0]
        visited.append(candidate.state_key_sha256)
        return _scheduler_result(job, statuses[candidate.state_key_sha256])

    monkeypatch.setattr(
        "scottish_progressive.native_teacher._run_bucket", fake_run
    )
    job = replace(
        _job(candidates[0], str(tmp_path)),
        quota=2,
        candidates=candidates,
    )

    result = _run_buckets_parallel(
        (job,), workers=3, executor_factory=ThreadPoolExecutor
    )[0]

    assert [row["state_key_sha256"] for row in result["accepted"]] == [
        candidates[1].state_key_sha256,
        candidates[3].state_key_sha256,
    ]
    assert [row["state_key_sha256"] for row in result["failures"]] == [
        candidates[0].state_key_sha256
    ]
    assert [row["state_key_sha256"] for row in result["excluded"]] == [
        candidates[2].state_key_sha256
    ]
    assert set(visited) == {
        candidate.state_key_sha256 for candidate in candidates[:4]
    }
    assert candidates[4].state_key_sha256 not in visited


def test_parallel_bucket_scheduler_exposes_one_quota_wave_concurrently(
    monkeypatch, tmp_path
) -> None:
    candidates = tuple(_scheduler_candidate(index) for index in range(4))
    rendezvous = Barrier(4, timeout=2.0)

    def fake_run(job: _BucketJob) -> dict[str, object]:
        rendezvous.wait()
        return _scheduler_result(job, "accepted")

    monkeypatch.setattr(
        "scottish_progressive.native_teacher._run_bucket", fake_run
    )
    job = replace(
        _job(candidates[0], str(tmp_path)),
        quota=4,
        candidates=candidates,
    )

    result = _run_buckets_parallel(
        (job,), workers=4, executor_factory=ThreadPoolExecutor
    )[0]

    assert [row["state_key_sha256"] for row in result["accepted"]] == [
        candidate.state_key_sha256 for candidate in candidates
    ]


def test_parallel_bucket_scheduler_rejects_worker_binding_drift(
    monkeypatch, tmp_path
) -> None:
    candidate = _scheduler_candidate(0)

    def fake_run(job: _BucketJob) -> dict[str, object]:
        result = _scheduler_result(job, "accepted")
        result["candidate_count"] = 2
        return result

    monkeypatch.setattr(
        "scottish_progressive.native_teacher._run_bucket", fake_run
    )
    with pytest.raises(ValueError, match="binding drifted"):
        _run_buckets_parallel(
            (_job(candidate, str(tmp_path)),),
            workers=2,
            executor_factory=ThreadPoolExecutor,
        )


def test_single_candidate_scheduler_job_preserves_receipt_identity(tmp_path) -> None:
    candidates = (_scheduler_candidate(0), _scheduler_candidate(1))
    job = replace(
        _job(candidates[0], str(tmp_path)),
        quota=2,
        candidates=candidates,
        forbidden_state_keys=frozenset({"a" * 64}),
    )

    single = _single_candidate_job(job, candidates[1])

    assert single.quota == 1
    assert single.candidates == (candidates[1],)
    assert single.cache_contract_id == job.cache_contract_id
    assert single.receipt_root == job.receipt_root
    assert single.forbidden_state_keys == job.forbidden_state_keys


def test_prior_receipt_cache_contract_is_fail_closed(tmp_path) -> None:
    current = {
        "schema": "cache-v1",
        "source_fingerprint": "b" * 16,
        "native_mate_runtime_identity": "native-mate",
        "teacher_config": {"depth": 2},
    }
    prior_payload = {**current, "source_fingerprint": "a" * 16}
    prior = {
        **prior_payload,
        "cache_contract_id": "spc-native-teacher-cache-"
        + hashlib.sha256(
            json.dumps(
                prior_payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()[:20],
        "receipt_root": str(tmp_path.resolve()),
    }

    payload, cache_id = _resolve_receipt_cache_contract(
        current, prior, tmp_path.resolve()
    )
    assert payload == prior_payload
    assert cache_id == prior["cache_contract_id"]

    drifted = deepcopy(prior)
    drifted["native_mate_runtime_identity"] = "other"
    with pytest.raises(ValueError, match="label contract drifted"):
        _resolve_receipt_cache_contract(current, drifted, tmp_path.resolve())

    with pytest.raises(ValueError, match="cross-source receipt cache"):
        _validate_replacement_cache_source(
            "a" * 16,
            "b" * 16,
            frozenset({"c" * 64}),
            frozenset(),
        )


def test_label_runtime_identities_pin_search_eval_rules_and_native(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "scottish_progressive.native_teacher.evaluation._native_source_identity",
        lambda: "native-eval",
    )
    monkeypatch.setattr(
        "scottish_progressive.native_teacher.evaluation._native_eval",
        type("Native", (), {"SOURCE_IDENTITY": "native-eval"})(),
    )
    monkeypatch.setattr(
        "scottish_progressive.native_teacher._source_file_sha256",
        lambda filename: f"sha256:{filename}",
    )
    from scottish_progressive.native_teacher import _label_runtime_identities

    assert _label_runtime_identities("native-mate") == {
        "evaluation_source_sha256": "sha256:evaluation.py",
        "native_eval_runtime_identity": "native-eval",
        "native_mate_runtime_identity": "native-mate",
        "rules_source_sha256": "sha256:rules.py",
        "search_source_sha256": "sha256:search.py",
    }


def test_cross_tier_artifact_forbids_the_opposite_split_state_universe() -> None:
    payload = {
        "quality": {"status": "complete"},
        "labels": [
            {
                "split": "train",
                "state_key_sha256": "a" * 64,
                "options": [{"final_state_key_sha256": "b" * 64}],
            },
            {
                "split": "holdout",
                "state_key_sha256": "c" * 64,
                "options": [{"final_state_key_sha256": "d" * 64}],
            },
        ],
    }

    forbidden_train, forbidden_holdout = _cross_tier_forbidden_state_keys(payload)

    assert forbidden_train == {"c" * 64, "d" * 64}
    assert forbidden_holdout == {"a" * 64, "b" * 64}


def test_completeness_contract_and_signed_mate_distance() -> None:
    assert _complete_result_or_reason(_result(complete=True)) is None
    assert _complete_result_or_reason(_result(complete=False)) == "work-limit"
    assert _mate_distance(MATE_SCORE - 3) == 3
    assert _mate_distance(-MATE_SCORE + 2) == -2
    assert _mate_distance(250) is None


def _mixed_tier_payload(
    *,
    tag: str,
    selection_mode: str,
    depth: int,
    target_roots: int,
    train_roots: int,
) -> dict[str, object]:
    profile_ids = [f"profile-{index}" for index in range(4)]
    cells = [(profile_id, series) for profile_id in profile_ids for series in range(4, 10)]
    labels: list[dict[str, object]] = []
    quota_rows: list[dict[str, object]] = []
    train_extra_cells = train_roots - len(cells)
    for cell_index, (profile_id, series) in enumerate(cells):
        cell_total = target_roots // len(cells)
        if cell_total == 6:
            split_quotas = {"train": 4, "holdout": 2}
        else:
            train_quota = 2 if cell_index < train_extra_cells else 1
            split_quotas = {"train": train_quota, "holdout": cell_total - train_quota}
        for split, quota in split_quotas.items():
            quota_rows.append(
                {
                    "split": split,
                    "source_profile_id": profile_id,
                    "series_number": series,
                    "quota": quota,
                    "accepted": quota,
                }
            )
            for row_index in range(quota):
                prefix = f"{tag}|{split}|{profile_id}|{series}|{row_index}"
                root_key = hashlib.sha256((prefix + "|root").encode()).hexdigest()
                option_key = hashlib.sha256((prefix + "|option").encode()).hexdigest()
                labels.append(
                    {
                        "split": split,
                        "state_key_sha256": root_key,
                        "source_profile_id": profile_id,
                        "series_number": series,
                        "search": {
                            "requested_depth_series": depth,
                            "completed_depth_series": depth,
                            "root_scores_complete": True,
                            "timed_out": False,
                            "work_limit_reached": False,
                        },
                        "options": [
                            {
                                "proof_bounds": [-1, 1],
                                "proof": None,
                                "signed_mate_distance_series": None,
                                "principal_variation": [],
                                "final_state_key_sha256": option_key,
                                "final_pfen": "synthetic",
                                "final_features": {},
                                "is_hard_negative": False,
                                "hard_negative_reasons": [],
                            }
                        ],
                    }
                )
    quality = {
        "status": "complete",
        "accepted_roots": target_roots,
        "train_roots": train_roots,
        "holdout_roots": target_roots - train_roots,
        "label_search_failures": 3,
        "teacher_source_agreement_rate": 0.5,
    }
    return {
        "schema": "spc-native-deep-teacher-corpus-v1",
        "method": "balanced-native-trajectory-depth3-policy-teacher-v1",
        "engine_version": "test-engine",
        "source_fingerprint": "0123456789abcdef",
        "teacher_profile": baseline_profile().as_dict(),
        "config": {
            "selection_mode": selection_mode,
            "depth_series": depth,
            "branch_cap": 32,
            "target_roots": target_roots,
            "train_roots": train_roots,
            "hard_negative_count": 4,
            "expected_train_attempts": 8_192,
            "expected_holdout_attempts": 4_096,
        },
        "generation": {
            "train_contract_sha256": "train-contract",
            "holdout_contract_sha256": "holdout-contract",
            "train_corpus_sha256": "train-corpus",
            "holdout_corpus_sha256": "holdout-corpus",
            "ordered_profile_ids": profile_ids,
            "profile_schedule": "ordered-pair-round-robin",
            "train_attempts": 8_192,
            "holdout_attempts": 4_096,
        },
        "selection": {"quota_by_cell": quota_rows},
        "labels": labels,
        "quality": quality,
        "failure_diagnostics": [{"reason": "work-limit"}] * 3,
        "contract": {
            "incomplete_labels_cached": False,
            "full_retained_root_scores_required": True,
        },
        "corpus_id": f"corpus-{tag}",
        "runtime": {"elapsed_seconds": 1.0},
    }


def test_mixed_depth_merge_keeps_tier_metrics_separate_and_fails_on_leakage() -> None:
    quiet = _mixed_tier_payload(
        tag="quiet",
        selection_mode="quiet-nonterminal",
        depth=2,
        target_roots=144,
        train_roots=96,
    )
    tactical = _mixed_tier_payload(
        tag="tactical",
        selection_mode="tactical-low-complexity",
        depth=3,
        target_roots=48,
        train_roots=32,
    )
    merged = merge_native_teacher_tiers(quiet, tactical)
    assert merged["schema"] == "spc-deep-teacher-corpus-v1"
    assert merged["quality"]["accepted_roots"] == 192
    assert merged["quality"]["train_roots"] == 128
    assert merged["quality"]["holdout_roots"] == 64
    assert set(merged["quality"]["tier_metrics"]) == {"quiet_d2", "tactical_d3"}
    assert "teacher_source_agreement_rate" not in merged["quality"]
    assert {label["teacher_depth_series"] for label in merged["labels"]} == {2, 3}

    leaking = deepcopy(tactical)
    train_final = next(
        label["options"][0]["final_state_key_sha256"]
        for label in quiet["labels"]
        if label["split"] == "train"
    )
    next(
        label for label in leaking["labels"] if label["split"] == "holdout"
    )["options"][0]["final_state_key_sha256"] = train_final
    with pytest.raises(ValueError, match="leakage audit failed"):
        merge_native_teacher_tiers(quiet, leaking)
