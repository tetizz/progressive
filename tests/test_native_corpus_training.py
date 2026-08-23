from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

import scottish_progressive.corpus_pipeline as corpus_pipeline
import scottish_progressive.native_corpus_training as native_corpus_training
from scottish_progressive.corpus_pipeline import (
    CorpusGenerationPlan,
    NATIVE_OUTCOMES_DIRECTORY,
    NativeShardOutcomeReceipt,
    materialize_native_generation_contract,
    read_native_generation_contract,
)
from scottish_progressive.corpus_samples import (
    NATIVE_BOUNDARY_SAMPLE_SCHEMA,
    encode_native_boundary_sample,
    sample_from_native_game,
)
from scottish_progressive.corpus_shards import (
    CorpusIdentity,
    CorpusShardWriter,
    CorpusStore,
    ShardMetadata,
)
from scottish_progressive.native_corpus import (
    NativeCorpusConfig,
    NativeFullGameRecord,
    NativeReject,
    NativeTerminal,
    replay_native_full_game,
    semantic_config_digest,
)
from scottish_progressive.native_corpus_training import (
    build_native_shard_value_corpus,
)
from scottish_progressive.profiles import (
    EngineProfile,
    EvaluationWeights,
    baseline_profile,
)


SOURCE_FINGERPRINT = "0123456789abcdef"


def _config(seed: int, *, frontier: int = 96) -> NativeCorpusConfig:
    return NativeCorpusConfig(
        seed=seed,
        max_frontier_states=frontier,
        engine_source_fingerprint=SOURCE_FINGERPRINT,
    )


def _record(
    attempt: int,
    black_series: tuple[str, str],
    *,
    profile_index: int = 0,
) -> NativeFullGameRecord:
    return NativeFullGameRecord(
        attempt_index=attempt,
        terminal=NativeTerminal.CHECKMATE_WHITE,
        reject=NativeReject.NONE,
        white_profile_index=profile_index,
        black_profile_index=profile_index,
        logical_work=1,
        path_count_saturations=0,
        series=(
            ("e2e4",),
            black_series,
            ("d1h5", "f1c4", "h5f7"),
        ),
    )


def _store(
    root: Path,
    *,
    config: NativeCorpusConfig,
    record: NativeFullGameRecord,
    profiles: tuple[EngineProfile, ...] | None = None,
    sequences: tuple[int, ...] = (0, 1, 2, 3),
) -> CorpusStore:
    ordered_profiles = profiles or (baseline_profile(),)
    identity = CorpusIdentity(
        record_schema=NATIVE_BOUNDARY_SAMPLE_SCHEMA,
        source_fingerprint=config.engine_source_fingerprint,
        generator_config_sha256=semantic_config_digest(
            config, ordered_profiles
        ).hex(),
        profile_ids=tuple(profile.profile_id for profile in ordered_profiles),
        ruleset_version=config.ruleset_version,
    )
    plan = CorpusGenerationPlan(
        root=root,
        config=config,
        profiles=ordered_profiles,
        first_attempt=record.attempt_index,
        attempt_count=1,
    )
    assert plan.identity == identity
    materialize_native_generation_contract(plan)
    store = CorpusStore(root, identity)
    replay = replay_native_full_game(record)
    writer = store.begin_shard(
        record.attempt_index,
        record.attempt_index + 1,
        owner_id=f"fixture-{root.name}",
    )
    for sequence, state in zip(sequences, replay.states, strict=True):
        writer.add_state(
            record.attempt_index,
            sequence,
            state,
            encode_native_boundary_sample(sample_from_native_game(state, record)),
        )
    _finalize_accepted_writer(store, writer, record)
    return store


def _finalize_accepted_writer(
    store: CorpusStore,
    writer: CorpusShardWriter,
    record: NativeFullGameRecord,
) -> None:
    _finalize_with_outcomes(
        store,
        writer,
        accepted_games=1,
        rejected_attempts=0,
        logical_work=record.logical_work,
        path_count_saturations=record.path_count_saturations,
        terminal_counts=((record.terminal.name.lower(), 1),),
        rejection_counts=(),
    )


def _finalize_with_outcomes(
    store: CorpusStore,
    writer: CorpusShardWriter,
    *,
    accepted_games: int,
    rejected_attempts: int,
    logical_work: int,
    path_count_saturations: int,
    terminal_counts: tuple[tuple[str, int], ...],
    rejection_counts: tuple[tuple[str, int], ...],
) -> None:
    generation_contract = read_native_generation_contract(store.root)
    corpus_pipeline._ensure_native_outcomes_directory(store)

    def publish(metadata: ShardMetadata) -> str:
        receipt = NativeShardOutcomeReceipt(
            attempt_range=metadata.attempt_range,
            identity_sha256=store.identity.digest_hex,
            generation_contract_sha256=generation_contract.digest_hex,
            shard_file=metadata.file,
            shard_sha256=metadata.sha256,
            shard_size_bytes=metadata.size_bytes,
            record_count=metadata.record_count,
            accepted_games=accepted_games,
            rejected_attempts=rejected_attempts,
            logical_work=logical_work,
            path_count_saturations=path_count_saturations,
            terminal_counts=terminal_counts,
            rejection_counts=rejection_counts,
        )
        persisted = corpus_pipeline._persist_native_shard_outcome(
            store,
            generation_contract,
            receipt,
            metadata,
        )
        return persisted.digest_hex

    writer.finalize(before_publish=publish)


def _append_record(store: CorpusStore, record: NativeFullGameRecord) -> None:
    replay = replay_native_full_game(record)
    writer = store.begin_shard(
        record.attempt_index,
        record.attempt_index + 1,
        owner_id=f"fixture-append-{record.attempt_index}",
    )
    for sequence, state in enumerate(replay.states):
        writer.add_state(
            record.attempt_index,
            sequence,
            state,
            encode_native_boundary_sample(sample_from_native_game(state, record)),
        )
    _finalize_accepted_writer(store, writer, record)


def _append_rejected_range(
    store: CorpusStore,
    attempt_start: int,
    attempt_stop: int,
) -> None:
    writer = store.begin_shard(
        attempt_start,
        attempt_stop,
        owner_id=f"fixture-rejected-{attempt_start}-{attempt_stop}",
    )
    rejected = attempt_stop - attempt_start
    _finalize_with_outcomes(
        store,
        writer,
        accepted_games=0,
        rejected_attempts=rejected,
        logical_work=7 * rejected,
        path_count_saturations=rejected,
        terminal_counts=(),
        rejection_counts=((NativeReject.WORK_LIMIT.name.lower(), rejected),),
    )


def test_native_value_corpus_aggregates_games_and_removes_exact_holdout_leakage(
    tmp_path: Path,
) -> None:
    profiles = (baseline_profile(),)
    train_config = _config(101)
    holdout_config = _config(202)
    train = _store(
        tmp_path / "train",
        config=train_config,
        record=_record(0, ("a7a6", "a6a5")),
        profiles=profiles,
    )
    holdout = _store(
        tmp_path / "holdout",
        config=holdout_config,
        record=_record(10, ("b7b6", "b6b5")),
        profiles=profiles,
    )
    captured_train_root = str(train.manifest["corpus_sha256"])
    captured_holdout_root = str(holdout.manifest["corpus_sha256"])
    corpus = build_native_shard_value_corpus(
        train,
        holdout,
        train_config=train_config,
        holdout_config=holdout_config,
        profiles=profiles,
        minimum_series=2,
    )
    assert corpus.completed_games == 2
    assert corpus.train_corpus_sha256 == captured_train_root
    assert corpus.holdout_corpus_sha256 == captured_holdout_root
    assert corpus.excluded_attempts == 0
    assert corpus.exact_overlap_states_removed == 1  # Shared post-e4 boundary.
    assert corpus.exact_overlap_occurrences_removed == 1
    assert len(corpus.train_samples) == 2
    assert len(corpus.holdout_samples) == 1
    assert {sample.series_number for sample in corpus.samples} <= {2, 3}
    assert all(sample.target_white_score == 1.0 for sample in corpus.samples)
    assert sum(sample.sample_weight for sample in corpus.train_samples) == 1.0
    assert sum(sample.sample_weight for sample in corpus.holdout_samples) == 0.5
    assert corpus.holdout_game_weight_coverage == 0.5
    train_hashes = {sample.state_key_sha256 for sample in corpus.train_samples}
    holdout_hashes = {sample.state_key_sha256 for sample in corpus.holdout_samples}
    assert train_hashes.isdisjoint(holdout_hashes)
    assert corpus.as_dict()["summary"]["leakage_contract"].startswith(
        "every exact full-state"
    )
    contract = corpus.as_dict()["generation_contract"]
    assert contract["train_seed"] == 101
    assert contract["holdout_seed"] == 202
    assert contract["train_generator_config_sha256"] == (
        train.identity.generator_config_sha256
    )
    assert contract["holdout_generator_config_sha256"] == (
        holdout.identity.generator_config_sha256
    )
    assert contract["shared_non_seed_config"]["max_frontier_states"] == 96
    assert [item["profile_id"] for item in contract["ordered_profiles"]] == [
        profiles[0].profile_id
    ]
    assert len(contract["shared_non_seed_config_sha256"]) == 64
    assert len(contract["ordered_profiles_sha256"]) == 64


def test_train_and_holdout_must_use_distinct_generator_seeds(tmp_path: Path) -> None:
    profiles = (baseline_profile(),)
    config = _config(303)
    train = _store(
        tmp_path / "train",
        config=config,
        record=_record(0, ("a7a6", "a6a5")),
        profiles=profiles,
    )
    holdout = _store(
        tmp_path / "holdout",
        config=config,
        record=_record(10, ("b7b6", "b6b5")),
        profiles=profiles,
    )
    with pytest.raises(ValueError, match="distinct generator seeds"):
        build_native_shard_value_corpus(
            train,
            holdout,
            train_config=config,
            holdout_config=config,
            profiles=profiles,
            minimum_series=2,
        )


def test_train_and_holdout_non_seed_generator_settings_must_match(
    tmp_path: Path,
) -> None:
    profiles = (baseline_profile(),)
    train_config = _config(404, frontier=96)
    holdout_config = _config(405, frontier=97)
    train = _store(
        tmp_path / "train",
        config=train_config,
        record=_record(0, ("a7a6", "a6a5")),
        profiles=profiles,
    )
    holdout = _store(
        tmp_path / "holdout",
        config=holdout_config,
        record=_record(10, ("b7b6", "b6b5")),
        profiles=profiles,
    )
    with pytest.raises(ValueError, match="non-seed settings"):
        build_native_shard_value_corpus(
            train,
            holdout,
            train_config=train_config,
            holdout_config=holdout_config,
            profiles=profiles,
            minimum_series=2,
        )


def test_training_contract_binds_full_ordered_profiles(tmp_path: Path) -> None:
    baseline = baseline_profile()
    alternate = EngineProfile(
        name="alternate",
        weights=EvaluationWeights(material=101),
    )
    supplied_profiles = (baseline, alternate)
    reversed_profiles = tuple(reversed(supplied_profiles))
    train_config = _config(500)
    holdout_config = _config(501)
    train = _store(
        tmp_path / "train",
        config=train_config,
        record=_record(0, ("a7a6", "a6a5")),
        profiles=supplied_profiles,
    )
    holdout = _store(
        tmp_path / "holdout",
        config=holdout_config,
        record=_record(10, ("b7b6", "b6b5")),
        profiles=reversed_profiles,
    )
    with pytest.raises(ValueError, match="holdout store generator digest"):
        build_native_shard_value_corpus(
            train,
            holdout,
            train_config=train_config,
            holdout_config=holdout_config,
            profiles=supplied_profiles,
            minimum_series=2,
        )


def test_multi_profile_duplicate_states_retain_profile_provenance(
    tmp_path: Path,
) -> None:
    baseline = baseline_profile()
    alternate = EngineProfile(
        name="alternate",
        weights=EvaluationWeights(material=101),
    )
    profiles = (baseline, alternate)
    train_config = _config(520)
    holdout_config = _config(521)
    shared_line = ("a7a6", "a6a5")
    train = _store(
        tmp_path / "train",
        config=train_config,
        record=_record(0, shared_line, profile_index=0),
        profiles=profiles,
    )
    _append_record(train, _record(1, shared_line, profile_index=1))
    holdout = _store(
        tmp_path / "holdout",
        config=holdout_config,
        record=_record(10, ("b7b6", "b6b5"), profile_index=0),
        profiles=profiles,
    )

    corpus = build_native_shard_value_corpus(
        train,
        holdout,
        train_config=train_config,
        holdout_config=holdout_config,
        profiles=profiles,
        minimum_series=3,
    )

    series_three = [
        sample for sample in corpus.train_samples if sample.series_number == 3
    ]
    assert len(series_three) == 2
    assert len({sample.state_key_sha256 for sample in series_three}) == 1
    assert {sample.profile_id for sample in series_three} == {
        baseline.profile_id,
        alternate.profile_id,
    }


def test_training_reader_rejects_noncontiguous_boundary_sequence(tmp_path: Path) -> None:
    profiles = (baseline_profile(),)
    train_config = _config(606)
    holdout_config = _config(607)
    train = _store(
        tmp_path / "train",
        config=train_config,
        record=_record(0, ("a7a6", "a6a5")),
        profiles=profiles,
        sequences=(0, 1, 2, 4),
    )
    holdout = _store(
        tmp_path / "holdout",
        config=holdout_config,
        record=_record(10, ("b7b6", "b6b5")),
        profiles=profiles,
    )
    with pytest.raises(ValueError, match="not contiguous"):
        build_native_shard_value_corpus(
            train,
            holdout,
            train_config=train_config,
            holdout_config=holdout_config,
            profiles=profiles,
            minimum_series=2,
        )


def test_training_accepts_empty_rejection_shard_and_uses_durable_reject_total(
    tmp_path: Path,
) -> None:
    profiles = (baseline_profile(),)
    train_config = _config(700)
    holdout_config = _config(701)
    train = _store(
        tmp_path / "train",
        config=train_config,
        record=_record(0, ("a7a6", "a6a5")),
        profiles=profiles,
    )
    _append_rejected_range(train, 1, 3)
    holdout = _store(
        tmp_path / "holdout",
        config=holdout_config,
        record=_record(10, ("b7b6", "b6b5")),
        profiles=profiles,
    )

    corpus = build_native_shard_value_corpus(
        train,
        holdout,
        train_config=train_config,
        holdout_config=holdout_config,
        profiles=profiles,
        minimum_series=2,
    )
    assert corpus.completed_games == 2
    assert corpus.excluded_attempts == 2
    empty = next(shard for shard in train.shards if shard.record_count == 0)
    assert empty.producer_receipt_sha256 is not None


def test_training_rejects_missing_or_recomputed_tampered_outcome_receipt(
    tmp_path: Path,
) -> None:
    profiles = (baseline_profile(),)
    train_config = _config(710)
    holdout_config = _config(711)
    train = _store(
        tmp_path / "train",
        config=train_config,
        record=_record(0, ("a7a6", "a6a5")),
        profiles=profiles,
    )
    holdout = _store(
        tmp_path / "holdout",
        config=holdout_config,
        record=_record(10, ("b7b6", "b6b5")),
        profiles=profiles,
    )
    receipt = corpus_pipeline.read_native_shard_outcome(train.root, 0, 1)
    path = train.root / NATIVE_OUTCOMES_DIRECTORY / receipt.file_name
    original = path.read_bytes()
    path.unlink()
    with pytest.raises(ValueError, match="outcome receipt is invalid"):
        build_native_shard_value_corpus(
            train,
            holdout,
            train_config=train_config,
            holdout_config=holdout_config,
            profiles=profiles,
            minimum_series=2,
        )

    path.write_bytes(original)
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
    with pytest.raises(ValueError, match="outcome receipt is invalid"):
        build_native_shard_value_corpus(
            train,
            holdout,
            train_config=train_config,
            holdout_config=holdout_config,
            profiles=profiles,
            minimum_series=2,
        )


def test_training_rejects_receipt_that_claims_an_omitted_accepted_game(
    tmp_path: Path,
) -> None:
    profiles = (baseline_profile(),)
    train_config = _config(720)
    holdout_config = _config(721)
    record = _record(0, ("a7a6", "a6a5"))
    plan = CorpusGenerationPlan(
        root=tmp_path / "train",
        config=train_config,
        profiles=profiles,
        first_attempt=0,
        attempt_count=2,
    )
    materialize_native_generation_contract(plan)
    train = CorpusStore(plan.root, plan.identity)
    writer = train.begin_shard(0, 2, owner_id="fixture-omitted-accepted")
    replay = replay_native_full_game(record)
    for sequence, state in enumerate(replay.states):
        writer.add_state(
            0,
            sequence,
            state,
            encode_native_boundary_sample(sample_from_native_game(state, record)),
        )
    _finalize_with_outcomes(
        train,
        writer,
        accepted_games=2,
        rejected_attempts=0,
        logical_work=2,
        path_count_saturations=0,
        terminal_counts=((NativeTerminal.CHECKMATE_WHITE.name.lower(), 2),),
        rejection_counts=(),
    )
    holdout = _store(
        tmp_path / "holdout",
        config=holdout_config,
        record=_record(10, ("b7b6", "b6b5")),
        profiles=profiles,
    )

    with pytest.raises(ValueError, match="grouped accepted attempts"):
        build_native_shard_value_corpus(
            train,
            holdout,
            train_config=train_config,
            holdout_config=holdout_config,
            profiles=profiles,
            minimum_series=2,
        )


def test_training_fails_if_manifest_changes_after_all_sample_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = (baseline_profile(),)
    train_config = _config(730)
    holdout_config = _config(731)
    train = _store(
        tmp_path / "train",
        config=train_config,
        record=_record(0, ("a7a6", "a6a5")),
        profiles=profiles,
    )
    holdout = _store(
        tmp_path / "holdout",
        config=holdout_config,
        record=_record(10, ("b7b6", "b6b5")),
        profiles=profiles,
    )
    original_aggregate = native_corpus_training._aggregate_store
    mutated = False

    def aggregate_then_finalize_concurrent_shard(
        store: CorpusStore,
        **kwargs: object,
    ) -> object:
        nonlocal mutated
        result = original_aggregate(store, **kwargs)
        if store.root == holdout.root and not mutated:
            mutated = True
            _append_record(train, _record(1, ("g7g6", "g6g5")))
        return result

    monkeypatch.setattr(
        native_corpus_training,
        "_aggregate_store",
        aggregate_then_finalize_concurrent_shard,
    )
    with pytest.raises(ValueError, match="train corpus manifest changed"):
        build_native_shard_value_corpus(
            train,
            holdout,
            train_config=train_config,
            holdout_config=holdout_config,
            profiles=profiles,
            minimum_series=2,
        )
    assert mutated


def test_per_range_accepted_mismatches_cannot_cancel_across_shards(
    tmp_path: Path,
) -> None:
    profiles = (baseline_profile(),)
    train_config = _config(740)
    holdout_config = _config(741)
    plan = CorpusGenerationPlan(
        root=tmp_path / "train",
        config=train_config,
        profiles=profiles,
        first_attempt=0,
        attempt_count=4,
    )
    materialize_native_generation_contract(plan)
    train = CorpusStore(plan.root, plan.identity)

    first = train.begin_shard(0, 2, owner_id="fixture-cancelling-first")
    for record in (
        _record(0, ("a7a6", "a6a5")),
        _record(1, ("g7g6", "g6g5")),
    ):
        replay = replay_native_full_game(record)
        for sequence, state in enumerate(replay.states):
            first.add_state(
                record.attempt_index,
                sequence,
                state,
                encode_native_boundary_sample(
                    sample_from_native_game(state, record)
                ),
            )
    _finalize_with_outcomes(
        train,
        first,
        accepted_games=1,
        rejected_attempts=1,
        logical_work=2,
        path_count_saturations=0,
        terminal_counts=((NativeTerminal.CHECKMATE_WHITE.name.lower(), 1),),
        rejection_counts=((NativeReject.WORK_LIMIT.name.lower(), 1),),
    )

    second = train.begin_shard(2, 4, owner_id="fixture-cancelling-second")
    _finalize_with_outcomes(
        train,
        second,
        accepted_games=1,
        rejected_attempts=1,
        logical_work=2,
        path_count_saturations=0,
        terminal_counts=((NativeTerminal.CHECKMATE_WHITE.name.lower(), 1),),
        rejection_counts=((NativeReject.WORK_LIMIT.name.lower(), 1),),
    )
    holdout = _store(
        tmp_path / "holdout",
        config=holdout_config,
        record=_record(10, ("b7b6", "b6b5")),
        profiles=profiles,
    )

    with pytest.raises(ValueError, match=r"range \[0, 2\): expected 1, found 2"):
        build_native_shard_value_corpus(
            train,
            holdout,
            train_config=train_config,
            holdout_config=holdout_config,
            profiles=profiles,
            minimum_series=2,
        )
