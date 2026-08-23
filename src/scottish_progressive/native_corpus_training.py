from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

import chess

from .corpus_samples import (
    NATIVE_BOUNDARY_SAMPLE_SCHEMA,
    NativeBoundarySample,
    decode_native_boundary_sample,
)
from .corpus_pipeline import (
    NativeGenerationContract as PersistedNativeGenerationContract,
    NativeShardOutcomeError,
    read_native_generation_contract,
    read_native_shard_outcome,
)
from .corpus_shards import (
    AttemptRange,
    CorpusRecord,
    CorpusStore,
    progressive_state_dedup_key,
)
from .fast_training import CachedFeatures
from .model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    RULESET_VERSION,
    ProgressiveState,
)
from .native_corpus import (
    NativeCorpusConfig,
    NativeCorpusProfile,
    bind_native_profiles,
    semantic_config_digest,
)
from .profiles import EngineProfile


NATIVE_VALUE_CORPUS_METHOD = "native-shard-aggregated-wdl-v1"
NATIVE_GENERATION_CONTRACT_SCHEMA = "spc-native-training-generation-contract-v1"


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(prefix: str, payload: Mapping[str, Any]) -> str:
    return prefix + hashlib.sha256(_canonical_json(payload)).hexdigest()[:20]


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class NativeCorpusGenerationContract:
    """Exact generator evidence required before train/holdout fitting.

    A corpus manifest binds an opaque semantic-config digest. This contract
    retains the preimage needed to prove that the train and holdout generators
    differ only by seed, including the full ordered native profile records.
    """

    train_config: NativeCorpusConfig
    holdout_config: NativeCorpusConfig
    ordered_profiles: tuple[NativeCorpusProfile, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.train_config, NativeCorpusConfig) or not isinstance(
            self.holdout_config, NativeCorpusConfig
        ):
            raise TypeError("train_config and holdout_config must be NativeCorpusConfig")
        profiles = bind_native_profiles(self.ordered_profiles)
        object.__setattr__(self, "ordered_profiles", profiles)
        if self.train_config.seed == self.holdout_config.seed:
            raise ValueError("train and holdout must use distinct generator seeds")
        if self.train_non_seed_config != self.holdout_non_seed_config:
            raise ValueError(
                "train and holdout generator non-seed settings must be identical"
            )

    @staticmethod
    def _without_seed(config: NativeCorpusConfig) -> dict[str, object]:
        payload = config.as_semantic_dict()
        del payload["seed"]
        return payload

    @property
    def train_non_seed_config(self) -> dict[str, object]:
        return self._without_seed(self.train_config)

    @property
    def holdout_non_seed_config(self) -> dict[str, object]:
        return self._without_seed(self.holdout_config)

    @property
    def train_config_sha256(self) -> str:
        return semantic_config_digest(
            self.train_config, self.ordered_profiles
        ).hex()

    @property
    def holdout_config_sha256(self) -> str:
        return semantic_config_digest(
            self.holdout_config, self.ordered_profiles
        ).hex()

    @property
    def ordered_profile_ids(self) -> tuple[str, ...]:
        return tuple(profile.profile_id for profile in self.ordered_profiles)

    @property
    def shared_non_seed_config_sha256(self) -> str:
        return _sha256(
            {
                "schema": NATIVE_GENERATION_CONTRACT_SCHEMA,
                "shared_non_seed_config": self.train_non_seed_config,
            }
        )

    @property
    def ordered_profiles_sha256(self) -> str:
        return _sha256(
            {
                "schema": NATIVE_GENERATION_CONTRACT_SCHEMA,
                "ordered_profiles": [
                    profile.as_semantic_dict() for profile in self.ordered_profiles
                ],
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": NATIVE_GENERATION_CONTRACT_SCHEMA,
            "train_seed": self.train_config.seed,
            "holdout_seed": self.holdout_config.seed,
            "train_generator_config_sha256": self.train_config_sha256,
            "holdout_generator_config_sha256": self.holdout_config_sha256,
            "shared_non_seed_config_sha256": self.shared_non_seed_config_sha256,
            "shared_non_seed_config": self.train_non_seed_config,
            "ordered_profiles_sha256": self.ordered_profiles_sha256,
            "ordered_profiles": [
                profile.as_semantic_dict() for profile in self.ordered_profiles
            ],
        }


def _target_white_score(sample: NativeBoundarySample) -> float:
    value = sample.value_for_side_to_move
    if value == 0:
        return 0.5
    if sample.state.board.turn == chess.WHITE:
        return (value + 1) / 2
    return (1 - value) / 2


def _profile_for_mover(store: CorpusStore, sample: NativeBoundarySample) -> str:
    profile_index = (
        sample.white_profile_index
        if sample.state.board.turn == chess.WHITE
        else sample.black_profile_index
    )
    try:
        return store.identity.profile_ids[profile_index]
    except IndexError as error:
        raise ValueError(
            f"sample profile index {profile_index} exceeds the corpus identity"
        ) from error


def _decoded_records(
    store: CorpusStore,
) -> Iterable[tuple[CorpusRecord, NativeBoundarySample]]:
    if store.identity.record_schema != NATIVE_BOUNDARY_SAMPLE_SCHEMA:
        raise ValueError(
            f"unsupported corpus record schema {store.identity.record_schema!r}"
        )
    if store.identity.ruleset_version != RULESET_VERSION:
        raise ValueError(
            f"unsupported corpus ruleset {store.identity.ruleset_version!r}"
        )
    for record in store.iter_records():
        sample = decode_native_boundary_sample(record.payload)
        expected = progressive_state_dedup_key(
            sample.state,
            ruleset_version=store.identity.ruleset_version,
        )
        if expected != record.state_key:
            raise ValueError(
                f"sample state key drifted at attempt {record.attempt_index}"
            )
        yield record, sample


def _grouped_games(
    store: CorpusStore,
) -> Iterable[tuple[int, tuple[tuple[CorpusRecord, NativeBoundarySample], ...]]]:
    current_attempt: int | None = None
    current: list[tuple[CorpusRecord, NativeBoundarySample]] = []
    for record, sample in _decoded_records(store):
        if current_attempt is None:
            current_attempt = record.attempt_index
        if record.attempt_index != current_attempt:
            yield current_attempt, _validate_game_group(current_attempt, current)
            current_attempt = record.attempt_index
            current = []
        current.append((record, sample))
    if current_attempt is not None:
        yield current_attempt, _validate_game_group(current_attempt, current)


def _validate_game_group(
    attempt_index: int,
    items: list[tuple[CorpusRecord, NativeBoundarySample]],
) -> tuple[tuple[CorpusRecord, NativeBoundarySample], ...]:
    if not items:
        raise ValueError(f"attempt {attempt_index} has no boundary records")
    sequences = [record.sequence_index for record, _ in items]
    if sequences != list(range(len(items))):
        raise ValueError(f"attempt {attempt_index} boundary sequence is not contiguous")
    terminals = {sample.terminal for _, sample in items}
    profile_pairs = {
        (sample.white_profile_index, sample.black_profile_index)
        for _, sample in items
    }
    if len(terminals) != 1 or len(profile_pairs) != 1:
        raise ValueError(f"attempt {attempt_index} sample labels are inconsistent")
    states = [sample.state for _, sample in items]
    if progressive_state_dedup_key(states[0]) != progressive_state_dedup_key(
        ProgressiveState.initial()
    ):
        raise ValueError(f"attempt {attempt_index} does not start at the initial state")
    for index, (prior, state) in enumerate(zip(states, states[1:], strict=False), 1):
        expected = prior.series_number + 1
        if index == len(states) - 1:
            if state.series_number not in {prior.series_number, expected}:
                raise ValueError(
                    f"attempt {attempt_index} terminal series progression is invalid"
                )
        elif state.series_number != expected:
            raise ValueError(
                f"attempt {attempt_index} boundary series progression is invalid"
            )
    return tuple(items)


def _eligible_state_keys(
    store: CorpusStore,
    *,
    minimum_series: int,
) -> set[bytes]:
    keys: set[bytes] = set()
    for _, game in _grouped_games(store):
        for record, sample in game[:-1]:  # The last boundary is terminal.
            if sample.state.series_number >= minimum_series:
                keys.add(record.state_key)
    return keys


@dataclass(slots=True)
class _Aggregate:
    state_key: bytes
    state: ProgressiveState
    profile_id: str
    target_weighted_sum: float = 0.0
    total_weight: float = 0.0
    occurrences: int = 0


@dataclass(frozen=True, slots=True)
class NativeValueSample:
    state_key_sha256: str
    position_hash: str
    pfen: str
    split: str
    series_number: int
    mover: str
    profile_id: str
    target_white_score: float
    sample_weight: float
    occurrences: int
    features: CachedFeatures

    def as_dict(self) -> dict[str, Any]:
        return {
            "state_key_sha256": self.state_key_sha256,
            "position_hash": self.position_hash,
            "pfen": self.pfen,
            "split": self.split,
            "series_number": self.series_number,
            "mover": self.mover,
            "profile_id": self.profile_id,
            "target_white_score": self.target_white_score,
            "sample_weight": self.sample_weight,
            "occurrences": self.occurrences,
            "features": self.features.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class _AggregatedStore:
    samples: tuple[NativeValueSample, ...]
    completed_games: int
    excluded_attempts: int
    eligible_games: int
    eligible_occurrences: int
    excluded_overlap_occurrences: int
    pre_filter_weight: float
    retained_weight: float


@dataclass(frozen=True, slots=True)
class _StoreManifestSnapshot:
    corpus_sha256: str
    attempt_count: int
    record_count: int
    shard_count: int


def _capture_store_manifest(store: CorpusStore) -> _StoreManifestSnapshot:
    manifest = store.manifest
    totals = manifest["totals"]
    return _StoreManifestSnapshot(
        corpus_sha256=str(manifest["corpus_sha256"]),
        attempt_count=int(totals["attempt_count"]),
        record_count=int(totals["record_count"]),
        shard_count=int(totals["shard_count"]),
    )


def _require_unchanged_manifest(
    store: CorpusStore,
    captured: _StoreManifestSnapshot,
    *,
    split: str,
) -> None:
    if _capture_store_manifest(store) != captured:
        raise ValueError(
            f"{split} corpus manifest changed during native training corpus build"
        )


@dataclass(frozen=True, slots=True)
class _DurableRangeOutcomes:
    attempt_range: AttemptRange
    accepted_games: int
    rejected_attempts: int
    record_count: int


@dataclass(frozen=True, slots=True)
class _DurableStoreOutcomes:
    accepted_games: int
    rejected_attempts: int
    record_count: int
    ranges: tuple[_DurableRangeOutcomes, ...]


def _durable_store_outcomes(
    store: CorpusStore,
    *,
    config: NativeCorpusConfig,
    ordered_profiles: tuple[NativeCorpusProfile, ...],
    captured_manifest: _StoreManifestSnapshot,
) -> _DurableStoreOutcomes:
    try:
        persisted_contract = read_native_generation_contract(store.root)
    except (NativeShardOutcomeError, ValueError) as error:
        raise ValueError(
            f"native training store generation contract is invalid: {error}"
        ) from error
    if not isinstance(persisted_contract, PersistedNativeGenerationContract):
        raise TypeError("persisted native generation contract type drifted")
    if (
        persisted_contract.config != config
        or persisted_contract.ordered_profiles != ordered_profiles
        or persisted_contract.identity != store.identity
    ):
        raise ValueError(
            "native training store generation contract differs from supplied evidence"
        )

    accepted_games = 0
    rejected_attempts = 0
    record_count = 0
    attempt_count = 0
    range_outcomes: list[_DurableRangeOutcomes] = []
    for metadata in store.shards:
        if metadata.producer_receipt_sha256 is None:
            raise ValueError(
                "native training shard has no manifest-bound outcome receipt"
            )
        try:
            receipt = read_native_shard_outcome(
                store.root,
                metadata.attempt_range.start,
                metadata.attempt_range.stop,
            )
            receipt.verify_binding(
                metadata,
                store.identity,
                persisted_contract,
            )
        except (NativeShardOutcomeError, ValueError) as error:
            raise ValueError(
                f"native training shard outcome receipt is invalid: {error}"
            ) from error
        accepted_games += receipt.accepted_games
        rejected_attempts += receipt.rejected_attempts
        record_count += receipt.record_count
        attempt_count += receipt.attempt_count
        range_outcomes.append(
            _DurableRangeOutcomes(
                attempt_range=receipt.attempt_range,
                accepted_games=receipt.accepted_games,
                rejected_attempts=receipt.rejected_attempts,
                record_count=receipt.record_count,
            )
        )

    if (
        attempt_count != captured_manifest.attempt_count
        or record_count != captured_manifest.record_count
        or len(range_outcomes) != captured_manifest.shard_count
        or accepted_games + rejected_attempts != attempt_count
    ):
        raise ValueError(
            "native training outcome receipts do not cover the manifest exactly"
        )
    return _DurableStoreOutcomes(
        accepted_games=accepted_games,
        rejected_attempts=rejected_attempts,
        record_count=record_count,
        ranges=tuple(range_outcomes),
    )


def _aggregate_store(
    store: CorpusStore,
    *,
    split: str,
    minimum_series: int,
    excluded_state_keys: set[bytes],
    durable_outcomes: _DurableStoreOutcomes,
) -> _AggregatedStore:
    if split not in {"train", "holdout"}:
        raise ValueError("split must be train or holdout")
    aggregates: dict[tuple[bytes, str], _Aggregate] = {}
    completed_games = 0
    eligible_games = 0
    eligible_occurrences = 0
    excluded_overlap_occurrences = 0
    pre_filter_weight = 0.0
    retained_weight = 0.0
    accepted_by_range = [0] * len(durable_outcomes.ranges)
    range_index = 0
    previous_attempt: int | None = None
    for attempt_index, game in _grouped_games(store):
        if previous_attempt is not None and attempt_index <= previous_attempt:
            raise ValueError("accepted attempt groups are not distinct and increasing")
        previous_attempt = attempt_index
        while (
            range_index < len(durable_outcomes.ranges)
            and durable_outcomes.ranges[range_index].attempt_range.stop
            <= attempt_index
        ):
            range_index += 1
        if (
            range_index >= len(durable_outcomes.ranges)
            or not durable_outcomes.ranges[range_index].attempt_range.contains(
                attempt_index
            )
        ):
            raise ValueError(
                f"accepted attempt {attempt_index} has no durable shard outcome range"
            )
        accepted_by_range[range_index] += 1
        completed_games += 1
        eligible: list[tuple[CorpusRecord, NativeBoundarySample]] = []
        for record, sample in game[:-1]:  # Search scores terminals separately.
            if sample.state.series_number < minimum_series:
                continue
            eligible_occurrences += 1
            eligible.append((record, sample))
        if not eligible:
            continue
        eligible_games += 1
        pre_filter_weight += 1.0
        game_weight = 1.0 / len(eligible)
        for record, sample in eligible:
            if record.state_key in excluded_state_keys:
                excluded_overlap_occurrences += 1
                continue
            retained_weight += game_weight
            target = _target_white_score(sample)
            profile_id = _profile_for_mover(store, sample)
            aggregate_key = (record.state_key, profile_id)
            aggregate = aggregates.get(aggregate_key)
            if aggregate is None:
                aggregate = _Aggregate(
                    record.state_key,
                    sample.state,
                    profile_id,
                )
                aggregates[aggregate_key] = aggregate
            aggregate.target_weighted_sum += target * game_weight
            aggregate.total_weight += game_weight
            aggregate.occurrences += 1

    for expected, actual in zip(
        durable_outcomes.ranges,
        accepted_by_range,
        strict=True,
    ):
        if actual != expected.accepted_games:
            raise ValueError(
                "grouped accepted attempts do not match durable outcomes for "
                f"range [{expected.attempt_range.start}, "
                f"{expected.attempt_range.stop}): expected "
                f"{expected.accepted_games}, found {actual}"
            )

    samples: list[NativeValueSample] = []
    for (state_key, _), aggregate in sorted(aggregates.items()):
        state = aggregate.state
        state_id = state_key.hex()
        samples.append(
            NativeValueSample(
                state_key_sha256=state_id,
                position_hash=state.position_hash,
                pfen=state.pfen,
                split=split,
                series_number=state.series_number,
                mover="white" if state.board.turn == chess.WHITE else "black",
                profile_id=aggregate.profile_id,
                target_white_score=(
                    aggregate.target_weighted_sum / aggregate.total_weight
                ),
                sample_weight=aggregate.total_weight,
                occurrences=aggregate.occurrences,
                features=CachedFeatures.from_state(state),
            )
        )
    return _AggregatedStore(
        samples=tuple(samples),
        completed_games=completed_games,
        excluded_attempts=durable_outcomes.rejected_attempts,
        eligible_games=eligible_games,
        eligible_occurrences=eligible_occurrences,
        excluded_overlap_occurrences=excluded_overlap_occurrences,
        pre_filter_weight=pre_filter_weight,
        retained_weight=retained_weight,
    )


@dataclass(frozen=True, slots=True)
class NativeShardValueCorpus:
    generation_contract: NativeCorpusGenerationContract
    train_identity_sha256: str
    holdout_identity_sha256: str
    train_corpus_sha256: str
    holdout_corpus_sha256: str
    minimum_series: int
    completed_games: int
    excluded_attempts: int
    exact_overlap_states_removed: int
    exact_overlap_occurrences_removed: int
    holdout_game_weight_coverage: float
    samples: tuple[NativeValueSample, ...]

    def __post_init__(self) -> None:
        if not isinstance(
            self.generation_contract, NativeCorpusGenerationContract
        ):
            raise TypeError(
                "generation_contract must be NativeCorpusGenerationContract"
            )
        if self.minimum_series < 1:
            raise ValueError("minimum_series must be positive")
        if self.completed_games < 1 or not self.samples:
            raise ValueError("native value corpus has no usable samples")
        if not self.train_samples or not self.holdout_samples:
            raise ValueError("native value corpus requires train and holdout samples")
        if not 0.0 < self.holdout_game_weight_coverage <= 1.0:
            raise ValueError("holdout retained game-weight coverage must be positive")

    @property
    def corpus_id(self) -> str:
        return _digest("spc-native-value-corpus-", self.deterministic_payload())

    @property
    def train_samples(self) -> tuple[NativeValueSample, ...]:
        return tuple(sample for sample in self.samples if sample.split == "train")

    @property
    def holdout_samples(self) -> tuple[NativeValueSample, ...]:
        return tuple(sample for sample in self.samples if sample.split == "holdout")

    def deterministic_payload(self) -> dict[str, Any]:
        return {
            "method": NATIVE_VALUE_CORPUS_METHOD,
            "engine_version": ENGINE_VERSION,
            "training_source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
            "generation_contract": self.generation_contract.as_dict(),
            "train_identity_sha256": self.train_identity_sha256,
            "holdout_identity_sha256": self.holdout_identity_sha256,
            "train_corpus_sha256": self.train_corpus_sha256,
            "holdout_corpus_sha256": self.holdout_corpus_sha256,
            "minimum_series": self.minimum_series,
            "completed_games": self.completed_games,
            "excluded_attempts": self.excluded_attempts,
            "exact_overlap_states_removed": self.exact_overlap_states_removed,
            "exact_overlap_occurrences_removed": self.exact_overlap_occurrences_removed,
            "holdout_game_weight_coverage": self.holdout_game_weight_coverage,
            "samples": [sample.as_dict() for sample in self.samples],
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.deterministic_payload(),
            "corpus_id": self.corpus_id,
            "summary": {
                "train_samples": len(self.train_samples),
                "holdout_samples": len(self.holdout_samples),
                "train_weight": sum(
                    sample.sample_weight for sample in self.train_samples
                ),
                "holdout_weight": sum(
                    sample.sample_weight for sample in self.holdout_samples
                ),
                "label_contract": (
                    "per-game-normalized eventual checkmate/draw WDL; duplicate "
                    "states aggregated; terminal boundaries excluded"
                ),
                "leakage_contract": (
                    "every exact full-state key present in train is removed from holdout; "
                    "per-game weights are fixed before removal"
                ),
            },
        }


def build_native_shard_value_corpus(
    train_store: CorpusStore,
    holdout_store: CorpusStore,
    *,
    train_config: NativeCorpusConfig,
    holdout_config: NativeCorpusConfig,
    profiles: Sequence[EngineProfile | NativeCorpusProfile],
    minimum_series: int = 3,
) -> NativeShardValueCorpus:
    """Aggregate two independently generated stores with exact-state isolation."""

    if not isinstance(train_store, CorpusStore) or not isinstance(
        holdout_store, CorpusStore
    ):
        raise TypeError("train_store and holdout_store must be CorpusStore instances")
    if minimum_series < 1:
        raise ValueError("minimum_series must be positive")
    if train_store.root == holdout_store.root:
        raise ValueError("train and holdout must be different corpus roots")
    contract = NativeCorpusGenerationContract(
        train_config=train_config,
        holdout_config=holdout_config,
        ordered_profiles=bind_native_profiles(profiles),
    )
    if train_store.identity.generator_config_sha256 != contract.train_config_sha256:
        raise ValueError(
            "train store generator digest does not match the supplied config/profiles"
        )
    if (
        holdout_store.identity.generator_config_sha256
        != contract.holdout_config_sha256
    ):
        raise ValueError(
            "holdout store generator digest does not match the supplied config/profiles"
        )
    if train_store.identity.profile_ids != contract.ordered_profile_ids:
        raise ValueError(
            "train store ordered profile IDs do not match the supplied profiles"
        )
    if holdout_store.identity.profile_ids != contract.ordered_profile_ids:
        raise ValueError(
            "holdout store ordered profile IDs do not match the supplied profiles"
        )
    if (
        train_store.identity.source_fingerprint
        != contract.train_config.engine_source_fingerprint
        or holdout_store.identity.source_fingerprint
        != contract.holdout_config.engine_source_fingerprint
    ):
        raise ValueError("store source fingerprints do not match generator configs")
    if (
        train_store.identity.ruleset_version != contract.train_config.ruleset_version
        or holdout_store.identity.ruleset_version
        != contract.holdout_config.ruleset_version
    ):
        raise ValueError("store rulesets do not match generator configs")
    if (
        train_store.identity.source_fingerprint
        != holdout_store.identity.source_fingerprint
        or train_store.identity.ruleset_version
        != holdout_store.identity.ruleset_version
        or train_store.identity.profile_ids != holdout_store.identity.profile_ids
        or train_store.identity.record_schema != holdout_store.identity.record_schema
    ):
        raise ValueError("train and holdout engine/profile/rules identities differ")
    train_manifest_snapshot = _capture_store_manifest(train_store)
    holdout_manifest_snapshot = _capture_store_manifest(holdout_store)
    train_outcomes = _durable_store_outcomes(
        train_store,
        config=train_config,
        ordered_profiles=contract.ordered_profiles,
        captured_manifest=train_manifest_snapshot,
    )
    holdout_outcomes = _durable_store_outcomes(
        holdout_store,
        config=holdout_config,
        ordered_profiles=contract.ordered_profiles,
        captured_manifest=holdout_manifest_snapshot,
    )
    train_keys = _eligible_state_keys(
        train_store,
        minimum_series=minimum_series,
    )
    holdout_keys = _eligible_state_keys(
        holdout_store,
        minimum_series=minimum_series,
    )
    overlap = train_keys & holdout_keys
    train = _aggregate_store(
        train_store,
        split="train",
        minimum_series=minimum_series,
        excluded_state_keys=set(),
        durable_outcomes=train_outcomes,
    )
    holdout = _aggregate_store(
        holdout_store,
        split="holdout",
        minimum_series=minimum_series,
        excluded_state_keys=train_keys,
        durable_outcomes=holdout_outcomes,
    )
    if holdout.pre_filter_weight <= 0.0 or holdout.retained_weight <= 0.0:
        raise ValueError("holdout has zero retained game-weight coverage")
    holdout_game_weight_coverage = (
        holdout.retained_weight / holdout.pre_filter_weight
    )
    _require_unchanged_manifest(
        train_store,
        train_manifest_snapshot,
        split="train",
    )
    _require_unchanged_manifest(
        holdout_store,
        holdout_manifest_snapshot,
        split="holdout",
    )
    return NativeShardValueCorpus(
        generation_contract=contract,
        train_identity_sha256=train_store.identity.digest_hex,
        holdout_identity_sha256=holdout_store.identity.digest_hex,
        train_corpus_sha256=train_manifest_snapshot.corpus_sha256,
        holdout_corpus_sha256=holdout_manifest_snapshot.corpus_sha256,
        minimum_series=minimum_series,
        completed_games=train.completed_games + holdout.completed_games,
        excluded_attempts=train.excluded_attempts + holdout.excluded_attempts,
        exact_overlap_states_removed=len(overlap),
        exact_overlap_occurrences_removed=holdout.excluded_overlap_occurrences,
        holdout_game_weight_coverage=holdout_game_weight_coverage,
        samples=train.samples + holdout.samples,
    )
