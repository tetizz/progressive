from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

import scottish_progressive.corpus_pipeline as corpus_pipeline
import scottish_progressive.corpus_shards as corpus_shards
from scottish_progressive.corpus_pipeline import (
    CorpusGenerationPlan,
    NATIVE_GENERATION_CONTRACT_FILE,
    NATIVE_OUTCOMES_DIRECTORY,
    NativeGenerationContract,
    NativeGenerationContractError,
    NativeShardOutcomeError,
    generate_corpus,
    materialize_native_generation_contract,
    read_native_generation_contract,
    read_native_shard_outcome,
    verify_native_boundary_corpus,
)
from scottish_progressive.corpus_shards import CorpusStore
from scottish_progressive.native_corpus import (
    NativeCorpusConfig,
    NativeCorpusIdentityError,
    NativeFullGameBatch,
    NativeFullGameRecord,
    NativePolicyKind,
    NativeProfileSchedule,
    NativeRankPolicy,
    NativeReject,
    NativeTerminal,
    semantic_config_digest,
)
from scottish_progressive.profiles import (
    EngineProfile,
    EvaluationWeights,
    baseline_profile,
)


SCHOLARS_PROGRESSIVE_MATE = (
    ("e2e4",),
    ("a7a6", "a6a5"),
    ("d1h5", "f1c4", "h5f7"),
)


def _fake_batch(
    config: NativeCorpusConfig,
    profiles: tuple[object, ...],
    *,
    first_attempt: int,
    attempt_count: int,
) -> NativeFullGameBatch:
    records = tuple(
        NativeFullGameRecord(
            attempt_index=attempt,
            terminal=NativeTerminal.CHECKMATE_WHITE,
            reject=NativeReject.NONE,
            white_profile_index=0,
            black_profile_index=0,
            logical_work=100,
            path_count_saturations=0,
            series=SCHOLARS_PROGRESSIVE_MATE,
        )
        for attempt in range(first_attempt, first_attempt + attempt_count)
    )
    return NativeFullGameBatch(
        first_attempt=first_attempt,
        attempt_count=attempt_count,
        semantic_config_digest=semantic_config_digest(config, profiles),
        profile_count=1,
        policy_kind=NativePolicyKind.UNIFORM,
        schedule=NativeProfileSchedule.SELF_ROUND_ROBIN,
        total_saturations=0,
        records=records,
        payload_size=80,
    )


def _mixed_batch(
    config: NativeCorpusConfig,
    profiles: tuple[object, ...],
    *,
    first_attempt: int,
    attempt_count: int,
    reject_all: bool = False,
) -> NativeFullGameBatch:
    records: list[NativeFullGameRecord] = []
    for attempt in range(first_attempt, first_attempt + attempt_count):
        rejected = reject_all or attempt % 2 == 1
        records.append(
            NativeFullGameRecord(
                attempt_index=attempt,
                terminal=(
                    NativeTerminal.NONE
                    if rejected
                    else NativeTerminal.CHECKMATE_WHITE
                ),
                reject=(
                    NativeReject.WORK_LIMIT if rejected else NativeReject.NONE
                ),
                white_profile_index=0,
                black_profile_index=0,
                logical_work=25 if rejected else 100,
                path_count_saturations=1 if rejected else 2,
                series=() if rejected else SCHOLARS_PROGRESSIVE_MATE,
            )
        )
    return NativeFullGameBatch(
        first_attempt=first_attempt,
        attempt_count=attempt_count,
        semantic_config_digest=semantic_config_digest(config, profiles),
        profile_count=1,
        policy_kind=NativePolicyKind.UNIFORM,
        schedule=NativeProfileSchedule.SELF_ROUND_ROBIN,
        total_saturations=sum(record.path_count_saturations for record in records),
        records=tuple(records),
        payload_size=80,
    )


def test_pipeline_generates_resumes_and_verifies_fixed_binary_samples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    def generate(
        config: NativeCorpusConfig,
        profiles: tuple[object, ...],
        *,
        first_attempt: int,
        attempt_count: int,
    ) -> NativeFullGameBatch:
        calls.append((first_attempt, attempt_count))
        return _fake_batch(
            config,
            profiles,
            first_attempt=first_attempt,
            attempt_count=attempt_count,
        )

    monkeypatch.setattr(corpus_pipeline, "generate_native_full_game_batch", generate)
    profile = baseline_profile()
    config = NativeCorpusConfig(
        max_frontier_states=8,
        candidate_count=4,
        policy=NativeRankPolicy.uniform(),
    )
    plan = CorpusGenerationPlan(
        root=tmp_path / "corpus",
        config=config,
        profiles=(profile,),
        first_attempt=10,
        attempt_count=4,
        shard_size=2,
        batch_size=1,
        workers=2,
    )
    first = generate_corpus(plan)
    assert sorted(calls) == [(10, 1), (11, 1), (12, 1), (13, 1)]
    assert first["generated_attempts"] == 4
    assert first["accepted_games"] == 4
    assert first["generated_records"] == 16
    assert first["generated_shards"] == 2
    assert first["outcome_totals"] == {
        "attempt_count": 4,
        "accepted_games": 4,
        "rejected_attempts": 0,
        "record_count": 16,
        "logical_work": 400,
        "path_count_saturations": 0,
        "terminal_counts": {"checkmate_white": 4},
        "rejection_counts": {},
    }
    assert len(first["outcome_receipts"]) == 2
    contract_path = plan.root / NATIVE_GENERATION_CONTRACT_FILE
    original_contract_bytes = contract_path.read_bytes()
    contract = read_native_generation_contract(plan.root)
    assert contract.config == config
    assert contract.identity == plan.identity
    assert contract.ordered_profiles[0].profile_id == profile.profile_id
    assert first["generation_contract"] == {
        "file": NATIVE_GENERATION_CONTRACT_FILE,
        "format": "spc-native-generation-contract-v1",
        "sha256": contract.digest_hex,
    }
    store = CorpusStore(plan.root, plan.identity)
    assert verify_native_boundary_corpus(store) == {
        "records": 16,
        "wins": 8,
        "losses": 8,
        "draws": 0,
        "unique_states": 4,
        "duplicate_states": 12,
    }

    calls.clear()
    resumed = generate_corpus(plan)
    assert calls == []
    assert resumed["already_complete_shards"] == 2
    assert resumed["generated_shards"] == 0
    assert resumed["generated_attempts"] == 0
    assert resumed["accepted_games"] == 0
    assert resumed["outcome_totals"] == first["outcome_totals"]
    assert resumed["outcome_receipts"] == first["outcome_receipts"]
    assert resumed["corpus"]["attempt_count"] == 4
    assert resumed["corpus"]["record_count"] == 16
    assert contract_path.read_bytes() == original_contract_bytes
    assert all(
        shard.producer_receipt_sha256 is not None for shard in store.shards
    )


def test_all_rejected_empty_shard_keeps_exact_durable_outcomes_on_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def generate(
        config: NativeCorpusConfig,
        profiles: tuple[object, ...],
        *,
        first_attempt: int,
        attempt_count: int,
    ) -> NativeFullGameBatch:
        nonlocal calls
        calls += 1
        return _mixed_batch(
            config,
            profiles,
            first_attempt=first_attempt,
            attempt_count=attempt_count,
            reject_all=True,
        )

    monkeypatch.setattr(corpus_pipeline, "generate_native_full_game_batch", generate)
    plan = CorpusGenerationPlan(
        root=tmp_path / "empty",
        config=NativeCorpusConfig(
            max_frontier_states=8,
            candidate_count=4,
            policy=NativeRankPolicy.uniform(),
        ),
        profiles=(baseline_profile(),),
        first_attempt=50,
        attempt_count=2,
        shard_size=2,
        batch_size=2,
    )
    first = generate_corpus(plan)
    expected = {
        "attempt_count": 2,
        "accepted_games": 0,
        "rejected_attempts": 2,
        "record_count": 0,
        "logical_work": 50,
        "path_count_saturations": 2,
        "terminal_counts": {},
        "rejection_counts": {"work_limit": 2},
    }
    assert first["outcome_totals"] == expected
    store = CorpusStore(plan.root, plan.identity)
    assert store.shards[0].record_count == 0
    receipt = read_native_shard_outcome(plan.root, 50, 52)
    assert receipt.digest_hex == store.shards[0].producer_receipt_sha256

    calls = 0
    resumed = generate_corpus(plan)
    assert calls == 0
    assert resumed["generated_attempts"] == 0
    assert resumed["outcome_totals"] == expected


def test_recomputed_tampered_outcome_is_rejected_by_manifest_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        corpus_pipeline,
        "generate_native_full_game_batch",
        _fake_batch,
    )
    plan = CorpusGenerationPlan(
        root=tmp_path / "tamper",
        config=NativeCorpusConfig(),
        profiles=(baseline_profile(),),
        first_attempt=0,
        attempt_count=1,
    )
    generate_corpus(plan)
    receipt = read_native_shard_outcome(plan.root, 0, 1)
    path = plan.root / NATIVE_OUTCOMES_DIRECTORY / receipt.file_name
    wrong_schema = {**receipt.as_dict(), "unexpected": 1}
    path.write_bytes(
        json.dumps(
            wrong_schema,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        + b"\n"
    )
    with pytest.raises(NativeShardOutcomeError, match="invalid schema"):
        read_native_shard_outcome(plan.root, 0, 1)

    tampered = replace(receipt, logical_work=receipt.logical_work + 1)
    path.write_bytes(
        json.dumps(
            tampered.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        + b"\n"
    )

    with pytest.raises(
        NativeShardOutcomeError,
        match="manifest producer receipt digest",
    ):
        generate_corpus(plan)


def test_missing_outcome_receipt_fails_closed_before_resume_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def generate(
        config: NativeCorpusConfig,
        profiles: tuple[object, ...],
        *,
        first_attempt: int,
        attempt_count: int,
    ) -> NativeFullGameBatch:
        nonlocal calls
        calls += 1
        return _fake_batch(
            config,
            profiles,
            first_attempt=first_attempt,
            attempt_count=attempt_count,
        )

    monkeypatch.setattr(corpus_pipeline, "generate_native_full_game_batch", generate)
    plan = CorpusGenerationPlan(
        root=tmp_path / "missing",
        config=NativeCorpusConfig(),
        profiles=(baseline_profile(),),
        first_attempt=0,
        attempt_count=1,
    )
    generate_corpus(plan)
    receipt = read_native_shard_outcome(plan.root, 0, 1)
    (plan.root / NATIVE_OUTCOMES_DIRECTORY / receipt.file_name).unlink()
    calls = 0

    with pytest.raises(NativeShardOutcomeError, match="could not read"):
        generate_corpus(plan)
    assert calls == 0


def test_incomplete_native_attempt_batch_never_finalizes_a_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def incomplete(
        config: NativeCorpusConfig,
        profiles: tuple[object, ...],
        *,
        first_attempt: int,
        attempt_count: int,
    ) -> NativeFullGameBatch:
        complete = _fake_batch(
            config,
            profiles,
            first_attempt=first_attempt,
            attempt_count=attempt_count,
        )
        return replace(complete, records=complete.records[:-1])

    monkeypatch.setattr(
        corpus_pipeline,
        "generate_native_full_game_batch",
        incomplete,
    )
    plan = CorpusGenerationPlan(
        root=tmp_path / "incomplete",
        config=NativeCorpusConfig(),
        profiles=(baseline_profile(),),
        first_attempt=0,
        attempt_count=2,
        batch_size=2,
    )
    with pytest.raises(NativeShardOutcomeError, match="complete requested"):
        generate_corpus(plan)
    assert CorpusStore(plan.root, plan.identity).shards == ()
    assert not list(
        (plan.root / NATIVE_OUTCOMES_DIRECTORY).glob("outcome-*.json")
    )


def test_manifest_crash_recovers_only_after_receipt_and_claim_are_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def generate(
        config: NativeCorpusConfig,
        profiles: tuple[object, ...],
        *,
        first_attempt: int,
        attempt_count: int,
    ) -> NativeFullGameBatch:
        nonlocal calls
        calls += 1
        return _fake_batch(
            config,
            profiles,
            first_attempt=first_attempt,
            attempt_count=attempt_count,
        )

    monkeypatch.setattr(corpus_pipeline, "generate_native_full_game_batch", generate)
    original_write = corpus_shards._atomic_write_json
    failed = False

    def fail_manifest_once(path: Path, payload: dict[str, object]) -> None:
        nonlocal failed
        totals = payload.get("totals")
        if (
            not failed
            and path.name == "manifest.json"
            and isinstance(totals, dict)
            and totals.get("shard_count") == 1
        ):
            failed = True
            raise OSError("simulated crash after shard rename")
        original_write(path, payload)

    monkeypatch.setattr(corpus_shards, "_atomic_write_json", fail_manifest_once)
    plan = CorpusGenerationPlan(
        root=tmp_path / "crash",
        config=NativeCorpusConfig(),
        profiles=(baseline_profile(),),
        first_attempt=5,
        attempt_count=1,
    )
    with pytest.raises(OSError, match="simulated crash"):
        generate_corpus(plan)
    receipt = read_native_shard_outcome(plan.root, 5, 6)
    claim_payload = json.loads(
        next((plan.root / "claims").glob("claim-*.json")).read_text(
            encoding="ascii"
        )
    )
    assert claim_payload["producer_receipt_sha256"] == receipt.digest_hex
    assert len(list((plan.root / "shards").glob("*.spcbin"))) == 1

    monkeypatch.setattr(corpus_shards, "_atomic_write_json", original_write)
    calls = 0
    resumed = generate_corpus(plan)
    assert calls == 0
    assert resumed["generated_attempts"] == 0
    assert resumed["outcome_totals"]["accepted_games"] == 1
    metadata = CorpusStore(plan.root, plan.identity).shards[0]
    assert metadata.producer_receipt_sha256 == receipt.digest_hex


def test_pipeline_rejects_forged_runtime_provenance_before_creating_store(
    tmp_path: Path,
) -> None:
    root = tmp_path / "must-not-exist"
    plan = CorpusGenerationPlan(
        root=root,
        config=NativeCorpusConfig(
            engine_source_fingerprint="0000000000000000"
        ),
        profiles=(baseline_profile(),),
        first_attempt=0,
        attempt_count=1,
    )

    with pytest.raises(NativeCorpusIdentityError, match="engine_source_fingerprint"):
        generate_corpus(plan)

    assert not root.exists()


def test_plan_identity_binds_generator_semantics_and_profile_order(
    tmp_path: Path,
) -> None:
    first = baseline_profile()
    config = NativeCorpusConfig(
        max_frontier_states=8,
        candidate_count=4,
        policy=NativeRankPolicy.uniform(),
    )
    plan = CorpusGenerationPlan(
        root=tmp_path,
        config=config,
        profiles=(first,),
        first_attempt=0,
        attempt_count=1,
    )
    assert plan.identity.generator_config_sha256 == semantic_config_digest(
        config, (first,)
    ).hex()
    assert plan.identity.profile_ids == (first.profile_id,)


def test_contract_strictly_reconstructs_config_and_ordered_profiles(
    tmp_path: Path,
) -> None:
    baseline = baseline_profile()
    alternate = EngineProfile(
        name="alternate",
        weights=EvaluationWeights(material=101),
    )
    config = NativeCorpusConfig(
        seed=987_654_321,
        max_attempt_series=37,
        max_frontier_states=12,
        max_positions_per_series=123_456,
        max_positions_per_game=7_654_321,
        candidate_count=7,
        policy=NativeRankPolicy(
            top_weight_basis_points=7_500,
            near_weight_basis_points=2_000,
            tail_weight_basis_points=500,
            top_rank_count=2,
            near_rank_count=3,
        ),
        schedule=NativeProfileSchedule.ORDERED_PAIR_ROUND_ROBIN,
    )
    plan = CorpusGenerationPlan(
        root=tmp_path / "ordered",
        config=config,
        profiles=(alternate, baseline),
        first_attempt=20,
        attempt_count=4,
    )
    generated = materialize_native_generation_contract(plan)
    loaded = read_native_generation_contract(plan.root)
    assert loaded == generated == NativeGenerationContract.from_plan(plan)
    assert loaded.config.as_semantic_dict() == config.as_semantic_dict()
    assert [profile.as_semantic_dict() for profile in loaded.ordered_profiles] == [
        profile.as_semantic_dict()
        for profile in corpus_pipeline.bind_native_profiles(plan.profiles)
    ]
    assert loaded.identity.generator_config_sha256 == semantic_config_digest(
        config, plan.profiles
    ).hex()
    assert loaded.as_dict()["contract_sha256"] == loaded.digest_hex


def test_contract_helper_safely_backfills_existing_matching_store(
    tmp_path: Path,
) -> None:
    config = NativeCorpusConfig(
        seed=123,
        max_frontier_states=8,
        candidate_count=4,
        policy=NativeRankPolicy.uniform(),
    )
    plan = CorpusGenerationPlan(
        root=tmp_path / "legacy",
        config=config,
        profiles=(baseline_profile(),),
        first_attempt=0,
        attempt_count=1,
    )
    CorpusStore(plan.root, plan.identity)
    path = plan.root / NATIVE_GENERATION_CONTRACT_FILE
    assert not path.exists()
    backfilled = materialize_native_generation_contract(plan)
    assert path.is_file()
    assert read_native_generation_contract(plan.root) == backfilled


def test_contract_helper_rejects_conflicting_existing_content_without_overwrite(
    tmp_path: Path,
) -> None:
    profile = baseline_profile()
    expected_plan = CorpusGenerationPlan(
        root=tmp_path / "corpus",
        config=NativeCorpusConfig(seed=1),
        profiles=(profile,),
        first_attempt=0,
        attempt_count=1,
    )
    materialize_native_generation_contract(expected_plan)
    conflicting_plan = CorpusGenerationPlan(
        root=expected_plan.root,
        config=NativeCorpusConfig(seed=2),
        profiles=(profile,),
        first_attempt=0,
        attempt_count=1,
    )
    path = expected_plan.root / NATIVE_GENERATION_CONTRACT_FILE
    conflicting_payload = NativeGenerationContract.from_plan(
        conflicting_plan
    ).as_dict()
    conflicting_bytes = (
        json.dumps(
            conflicting_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        + b"\n"
    )
    path.write_bytes(conflicting_bytes)
    with pytest.raises(NativeGenerationContractError, match="conflicts"):
        materialize_native_generation_contract(expected_plan)
    assert path.read_bytes() == conflicting_bytes


def test_contract_reader_rejects_noncanonical_or_mutated_semantic_preimage(
    tmp_path: Path,
) -> None:
    plan = CorpusGenerationPlan(
        root=tmp_path / "corpus",
        config=NativeCorpusConfig(seed=99),
        profiles=(baseline_profile(),),
        first_attempt=0,
        attempt_count=1,
    )
    contract = materialize_native_generation_contract(plan)
    path = plan.root / NATIVE_GENERATION_CONTRACT_FILE
    payload = contract.as_dict()
    payload["config"]["policy"]["preserve_returned_mate"] = False
    path.write_bytes(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        + b"\n"
    )
    with pytest.raises(NativeGenerationContractError):
        read_native_generation_contract(plan.root)


def test_contract_reader_rejects_tampered_profile_digest(tmp_path: Path) -> None:
    plan = CorpusGenerationPlan(
        root=tmp_path / "corpus",
        config=NativeCorpusConfig(seed=100),
        profiles=(baseline_profile(),),
        first_attempt=0,
        attempt_count=1,
    )
    contract = materialize_native_generation_contract(plan)
    path = plan.root / NATIVE_GENERATION_CONTRACT_FILE
    payload = contract.as_dict()
    payload["ordered_profiles"][0]["digest_sha256"] = "1" * 64
    path.write_bytes(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        + b"\n"
    )

    with pytest.raises(NativeGenerationContractError, match="canonical preimage"):
        read_native_generation_contract(plan.root)
