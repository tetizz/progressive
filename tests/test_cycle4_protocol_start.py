from __future__ import annotations

from argparse import Namespace
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_native_teacher_corpus as teacher_builder
from scripts import fit_deep_teacher_value as fitter
from scripts import generate_native_corpus as generator
from scottish_progressive.corpus_pipeline import (
    CorpusGenerationPlan,
    NativeGenerationContract,
)
from scottish_progressive.native_corpus import (
    NativeCorpusConfig,
    NativeProfileSchedule,
    NativeRankPolicy,
)
from scottish_progressive.profiles import baseline_profile


def _preregistration(tmp_path: Path) -> fitter.Preregistration:
    shared = {
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
    }
    manifest = {
        "trajectory_corpora": {
            "train": {"seed": 101, "attempts": 2, "attempt_start": 0, "attempt_stop": 2},
            "sealed_holdout": {
                "seed": 202,
                "attempts": 2,
                "attempt_start": 0,
                "attempt_stop": 2,
                "one_shot": True,
                "development_exclusion_sha256": fitter.semantic_exclusion_sha256(()),
            },
            "shared_config": shared,
        },
        "teacher": {
            "selection_seed": 303,
            "minimum_series": 4,
            "maximum_series": 9,
            "branch_cap": 32,
            "max_work": 10_000_000,
            "hard_negatives": 4,
            "workers": 8,
            "prior_receipt_cache_reuse": False,
            "tiers": {
                "quiet_depth2": {
                    "target_roots": 4,
                    "train_roots": 3,
                    "holdout_roots": 1,
                    "selection_mode": "quiet-nonterminal",
                    "tactical_gate": "skipped-for-quiet-tier",
                },
                "tactical_depth3": {
                    "target_roots": 4,
                    "train_roots": 3,
                    "holdout_roots": 1,
                    "selection_mode": "tactical-low-complexity",
                    "tactical_gate": "required",
                },
            },
        },
    }
    return fitter.Preregistration(
        path=(tmp_path / "protocol.json").resolve(),
        sha256="a" * 64,
        schema="spc-cycle4-one-shot-protocol-v1",
        expected_train_labels=6,
        expected_holdout_labels=2,
        manifest=manifest,
    )


def _generation_args(root: Path, receipt: Path, preregistration: fitter.Preregistration) -> Namespace:
    return Namespace(
        preregistration=preregistration.path,
        protocol_split="train",
        root=root,
        receipt=receipt,
        first_attempt=0,
        attempts=2,
        shard_size=10_000,
        batch_size=256,
        workers=8,
        skip_payload_verification=False,
        skip_unique_count=False,
    )


def test_atomic_completion_ignores_stranded_temp_and_never_overwrites_partial_final(
    tmp_path: Path,
) -> None:
    output = tmp_path / "complete.json"
    (tmp_path / ".complete.json.crash.complete.tmp").write_bytes(b"partial-temp")
    fitter._atomic_exclusive_json(
        output, {"status": "complete"}, conflict_message="already published"
    )
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "complete"}

    partial = tmp_path / "partial-final.json"
    partial.write_bytes(b'{"status":')
    with pytest.raises(FileExistsError, match="already published"):
        fitter._atomic_exclusive_json(
            partial, {"status": "complete"}, conflict_message="already published"
        )
    assert partial.read_bytes() == b'{"status":'


def test_trajectory_generation_start_is_preexisting_data_safe_and_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preregistration = _preregistration(tmp_path)
    config = NativeCorpusConfig(
        seed=101,
        max_attempt_series=64,
        max_frontier_states=32,
        max_positions_per_series=250_000,
        max_positions_per_game=10_000_000,
        candidate_count=16,
        policy=NativeRankPolicy.uniform(),
        schedule=NativeProfileSchedule.ORDERED_PAIR_ROUND_ROBIN,
    )
    root = (tmp_path / "train-root").resolve()
    receipt = (tmp_path / "train-receipt.json").resolve()
    plan = CorpusGenerationPlan(
        root=root,
        config=config,
        profiles=(baseline_profile(),),
        first_attempt=0,
        attempt_count=2,
        shard_size=10_000,
        batch_size=256,
        workers=8,
    )
    expected_digest = NativeGenerationContract.from_plan(plan).digest_hex
    def load_upstream_preregistration(
        _path: Path, **kwargs: object
    ) -> fitter.Preregistration:
        assert kwargs == {"forbid_pair_preparation": True}
        return preregistration

    monkeypatch.setattr(
        fitter, "_load_preregistration", load_upstream_preregistration
    )
    monkeypatch.setattr(
        fitter,
        "_expected_generation_contract_sha256",
        lambda *_args, **_kwargs: expected_digest,
    )
    args = _generation_args(root, receipt, preregistration)

    start_path = root.with_name(
        root.name + ".cycle4-preregistration-generation-start.json"
    )
    for incompatible_receipt in (root, root / "receipt.json", root.parent):
        with pytest.raises(ValueError, match="distinct, non-nested|overlaps protocol"):
            generator._cycle4_generation_start(
                _generation_args(root, incompatible_receipt, preregistration),
                plan,
            )
        assert not start_path.exists()

    first = generator._cycle4_generation_start(args, plan)
    assert first is not None and first[1].exists()
    assert generator._cycle4_generation_start(args, plan) == first
    bound_plan = replace(plan, protocol_root_binding_sha256=first[4])

    corpus = {
        "corpus_sha256": "7" * 64,
        "attempt_count": 2,
        "record_count": 2,
        "shard_count": 1,
    }
    verification = {
        "records": 2,
        "wins": 1,
        "losses": 1,
        "draws": 0,
        "unique_states": 2,
        "duplicate_states": 0,
    }
    fake_store = SimpleNamespace(
        verified_snapshot=lambda: (corpus, ("frozen-shard",))
    )
    monkeypatch.setattr(
        generator, "CorpusStore", lambda *_args, **_kwargs: fake_store
    )
    monkeypatch.setattr(
        generator,
        "verify_native_boundary_corpus",
        lambda *_args, **_kwargs: verification,
    )
    completed_receipt = {
        "format": "spc-native-corpus-generation-receipt-v1",
        "root": str(plan.root),
        "planned_attempt_start": 0,
        "planned_attempt_stop": 2,
        "planned_attempt_count": 2,
        "shard_size": 10_000,
        "batch_size": 256,
        "workers": 8,
        "corpus": corpus,
        "generation_contract": {"sha256": expected_digest},
        "payload_verification": verification,
        "completed_at": "2026-08-24T00:00:00+00:00",
        "preregistration_generation_start": {
            "schema": first[0]["schema"],
            "path": str(first[1]),
            "raw_artifact_sha256": first[2],
            "preregistration_raw_artifact_sha256": preregistration.sha256,
            "root_binding_path": str(first[3]),
            "root_binding_raw_artifact_sha256": first[4],
        },
    }
    receipt.write_text(json.dumps(completed_receipt), encoding="utf-8")
    assert (
        generator._completed_cycle4_receipt(args, bound_plan, first)
        == completed_receipt
    )

    changed = json.loads(first[1].read_text(encoding="utf-8"))
    changed["operational"]["workers"] = 7
    first[1].write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="resume denied"):
        generator._cycle4_generation_start(args, plan)

    rogue_root = (tmp_path / "rogue-root").resolve()
    rogue_root.mkdir()
    (rogue_root / "unbound-shard").write_bytes(b"generated-before-preregistration")
    rogue_plan = CorpusGenerationPlan(
        root=rogue_root,
        config=config,
        profiles=(baseline_profile(),),
        first_attempt=0,
        attempt_count=2,
        shard_size=10_000,
        batch_size=256,
        workers=8,
    )
    with pytest.raises(FileExistsError, match="unbound preexisting data"):
        generator._cycle4_generation_start(
            _generation_args(rogue_root, (tmp_path / "rogue.json").resolve(), preregistration),
            rogue_plan,
        )

    orphan_binding_root = (tmp_path / "orphan-binding-root").resolve()
    orphan_binding_root.mkdir()
    (orphan_binding_root / "cycle4-preregistration-root-binding.json").write_text(
        json.dumps({"forged": True}), encoding="utf-8"
    )
    orphan_binding_plan = CorpusGenerationPlan(
        root=orphan_binding_root,
        config=config,
        profiles=(baseline_profile(),),
        first_attempt=0,
        attempt_count=2,
        shard_size=10_000,
        batch_size=256,
        workers=8,
    )
    with pytest.raises(FileExistsError, match="without its external generation start"):
        generator._cycle4_generation_start(
            _generation_args(
                orphan_binding_root,
                (tmp_path / "orphan-binding.json").resolve(),
                preregistration,
            ),
            orphan_binding_plan,
        )


def _write_trajectory_evidence(
    root: Path,
    receipt_path: Path,
    preregistration: fitter.Preregistration,
    split: str,
    contract_sha: str,
) -> None:
    root.mkdir(parents=True)
    trajectory = preregistration.manifest["trajectory_corpora"][split]
    shared = preregistration.manifest["trajectory_corpora"]["shared_config"]
    start_path = root.with_name(
        root.name + ".cycle4-preregistration-generation-start.json"
    )
    start = {
        "schema": "spc-cycle4-trajectory-generation-start-v1",
        "preregistration": {
            "schema": preregistration.schema,
            "raw_artifact_sha256": preregistration.sha256,
        },
        "split": split,
        "root": str(root),
        "receipt": str(receipt_path),
        "attempt_start": trajectory["attempt_start"],
        "attempt_stop": trajectory["attempt_stop"],
        "generation_contract_sha256": contract_sha,
        "operational": {
            name: shared[name]
            for name in (
                "shard_size",
                "batch_size",
                "workers",
                "verify_payloads",
                "count_unique_states",
            )
        },
    }
    start_path.write_text(json.dumps(start), encoding="utf-8")
    start_raw = hashlib.sha256(start_path.read_bytes()).hexdigest()
    root_binding_path = root / "cycle4-preregistration-root-binding.json"
    root_binding = {
        "schema": "spc-cycle4-trajectory-root-binding-v1",
        "root": str(root),
        "generation_start": {
            "path": str(start_path),
            "raw_artifact_sha256": start_raw,
        },
    }
    root_binding_path.write_text(json.dumps(root_binding), encoding="utf-8")
    root_binding_raw = hashlib.sha256(root_binding_path.read_bytes()).hexdigest()
    receipt = {
        "format": "spc-native-corpus-generation-receipt-v1",
        "root": str(root),
        "planned_attempt_start": trajectory["attempt_start"],
        "planned_attempt_stop": trajectory["attempt_stop"],
        "planned_attempt_count": trajectory["attempts"],
        "shard_size": shared["shard_size"],
        "batch_size": shared["batch_size"],
        "workers": shared["workers"],
        "payload_verification": {
            "records": 2,
            "wins": 1,
            "losses": 1,
            "draws": 0,
            "unique_states": 2,
            "duplicate_states": 0,
        },
        "corpus": {
            "corpus_sha256": ("3" if split == "train" else "4") * 64,
            "attempt_count": 2,
            "record_count": 2,
            "shard_count": 1,
        },
        "generation_contract": {"sha256": contract_sha},
        "preregistration_generation_start": {
            "path": str(start_path),
            "raw_artifact_sha256": start_raw,
            "preregistration_raw_artifact_sha256": preregistration.sha256,
            "root_binding_path": str(root_binding_path),
            "root_binding_raw_artifact_sha256": root_binding_raw,
        },
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


def test_teacher_start_requires_exact_trajectory_receipts_and_resumes_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preregistration = _preregistration(tmp_path)
    train_root = (tmp_path / "train").resolve()
    holdout_root = (tmp_path / "holdout").resolve()
    train_receipt = (tmp_path / "train-complete.json").resolve()
    holdout_receipt = (tmp_path / "holdout-complete.json").resolve()
    digests = {"train": "1" * 64, "sealed_holdout": "2" * 64}
    _write_trajectory_evidence(
        train_root, train_receipt, preregistration, "train", digests["train"]
    )
    _write_trajectory_evidence(
        holdout_root,
        holdout_receipt,
        preregistration,
        "sealed_holdout",
        digests["sealed_holdout"],
    )
    monkeypatch.setattr(
        teacher_builder,
        "_protocol_helpers",
        lambda: (
            fitter._atomic_exclusive_json,
            fitter._exclusive_json,
            lambda _prereg, *, split: digests[split],
                lambda _path, **kwargs: (
                    preregistration
                    if kwargs == {"forbid_pair_preparation": True}
                    else (_ for _ in ()).throw(
                        AssertionError("teacher did not request upstream fence")
                    )
                ),
            fitter._read_json_artifact,
        ),
    )
    monkeypatch.setattr(
        teacher_builder,
        "read_native_generation_contract",
        lambda root: SimpleNamespace(
            digest_hex=(digests["train"] if Path(root) == train_root else digests["sealed_holdout"])
        ),
    )
    monkeypatch.setattr(
        teacher_builder.CorpusStore,
        "open",
        lambda root: SimpleNamespace(
            verify=lambda: {
                "corpus_sha256": (
                    "3" if Path(root) == train_root else "4"
                )
                * 64,
                "attempt_count": 2,
                "record_count": 2,
                "shard_count": 1,
            }
        ),
    )
    args = Namespace(
        preregistration=preregistration.path,
        output=(tmp_path / "quiet.json").resolve(),
        train_root=train_root,
        holdout_root=holdout_root,
        target_roots=4,
        train_roots=3,
        minimum_series=4,
        maximum_series=9,
        depth=2,
        branch_cap=32,
        max_work=10_000_000,
        hard_negatives=4,
        seed=303,
        workers=8,
        train_attempts=2,
        holdout_attempts=2,
        selection_mode="quiet-nonterminal",
        skip_tactical_gate=True,
        prior_receipt_cache_contract_artifact=None,
        development_exclusion_artifact=None,
        cross_tier_artifact=None,
        forbidden_train_option_final_key=[],
        teacher_profile=None,
        receipt_root=(tmp_path / "root-receipts").resolve(),
    )
    nested_output = (tmp_path / "nested-teacher.json").resolve()
    nested_args = Namespace(
        **{
            **vars(args),
            "output": nested_output,
            "receipt_root": nested_output / "receipts",
        }
    )
    with pytest.raises(ValueError, match="distinct and non-nested"):
        teacher_builder._cycle4_teacher_start(nested_args)
    assert not nested_output.with_name(
        nested_output.name + ".preregistration-start.json"
    ).exists()

    first = teacher_builder._cycle4_teacher_start(args)
    second = teacher_builder._cycle4_teacher_start(args)
    assert first[1] == second[1]
    source_binding_path = args.output.with_name(
        args.output.name + ".preregistration-sources.json"
    )
    source_binding_bytes = source_binding_path.read_bytes()
    source_binding_path.unlink()
    with pytest.raises(FileExistsError, match="cannot be resumed"):
        teacher_builder._cycle4_teacher_start(args)
    source_binding_path.write_bytes(source_binding_bytes)

    completed_payload = {
        "corpus_id": "spc-native-teacher-complete",
        "generation": {"preregistration_generation_provenance": first[1]},
    }
    args.output.write_text(json.dumps(completed_payload), encoding="utf-8")
    monkeypatch.setattr(
        teacher_builder,
        "_protocol_artifact_helpers",
        lambda: (
            lambda _payload: "8" * 64,
            lambda *_args, **_kwargs: None,
            lambda *_args, **_kwargs: None,
            lambda *_args, **_kwargs: None,
        ),
    )
    completed = teacher_builder._cycle4_teacher_start(args)
    assert completed[4] == completed_payload
    assert completed[6] is not None
    teacher_builder._publish_teacher_completion(completed[5], completed[6])
    resumed = teacher_builder._cycle4_teacher_start(args)
    assert resumed[4] == completed_payload
    args.output.unlink()
    completed[5].unlink()

    start_path = args.output.with_name(args.output.name + ".preregistration-start.json")
    changed = json.loads(start_path.read_text(encoding="utf-8"))
    changed["config"]["workers"] = 7
    start_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="resume denied"):
        teacher_builder._cycle4_teacher_start(args)

    trajectory_start = train_root.with_name(
        train_root.name + ".cycle4-preregistration-generation-start.json"
    )
    tampered = json.loads(trajectory_start.read_text(encoding="utf-8"))
    tampered["generation_contract_sha256"] = "9" * 64
    trajectory_start.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="generation-start binding differs"):
        teacher_builder._cycle4_teacher_start(args)
