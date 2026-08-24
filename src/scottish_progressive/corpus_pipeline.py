from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from .corpus_samples import (
    NATIVE_BOUNDARY_SAMPLE_SCHEMA,
    decode_native_boundary_sample,
    encode_native_boundary_sample,
    sample_from_native_game,
)
from .corpus_shards import (
    AttemptRange,
    AttemptRangeConflict,
    CorpusIdentity,
    CorpusStore,
    ShardMetadata,
    _atomic_write_json,
    _exclusive_store_lock,
    _fsync_directory,
    _read_canonical_json,
    progressive_state_dedup_key,
)
from .native_corpus import (
    FULL_GAME_V2_VERSION,
    NativeCorpusConfig,
    NativeCorpusProfile,
    NativeProfileSchedule,
    NativeRankPolicy,
    NativeReject,
    NativeTerminal,
    bind_native_profiles,
    generate_native_full_game_batch,
    replay_native_batch,
    semantic_config_digest,
    validate_current_native_generation_config,
)
from .profiles import EngineProfile


NATIVE_GENERATION_CONTRACT_FILE = "native-generation-contract.json"
NATIVE_GENERATION_CONTRACT_FORMAT = "spc-native-generation-contract-v1"
NATIVE_SEMANTIC_CONFIG_SCHEMA = "spc-native-full-game-v2-semantic-config-v1"
NATIVE_OUTCOMES_DIRECTORY = "native-outcomes"
NATIVE_SHARD_OUTCOME_FORMAT = "spc-native-shard-outcome-v1"
_NATIVE_PROFILE_WEIGHT_NAMES = (
    "material",
    "king_space",
    "promotion_corridors",
    "immediate_vulnerability",
    "boundary_check",
)
_NATIVE_TERMINAL_NAMES = frozenset(
    terminal.name.lower()
    for terminal in NativeTerminal
    if terminal is not NativeTerminal.NONE
)
_NATIVE_REJECTION_NAMES = frozenset(
    rejection.name.lower()
    for rejection in NativeReject
    if rejection is not NativeReject.NONE
)


class NativeGenerationContractError(ValueError):
    """A native corpus generation sidecar is missing or inconsistent."""


class NativeShardOutcomeError(ValueError):
    """A native per-shard attempt-outcome receipt is missing or inconsistent."""


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _contract_digest(payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(b"spc-native-generation-contract-v1\0")
    digest.update(_canonical_json(payload))
    return digest.hexdigest()


def _outcome_digest(payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(b"spc-native-shard-outcome-v1\0")
    digest.update(_canonical_json(payload))
    return digest.hexdigest()


def _exact_object(
    name: str,
    value: object,
    required: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        raise NativeGenerationContractError(f"{name} has an invalid schema")
    return value


def _strict_int(name: str, value: object) -> int:
    if type(value) is not int:
        raise NativeGenerationContractError(f"{name} must be an integer")
    return value


def _strict_text(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise NativeGenerationContractError(f"{name} must be a string")
    return value


def _strict_sha256(name: str, value: object) -> str:
    text = _strict_text(name, value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise NativeGenerationContractError(f"{name} must be a lowercase SHA-256")
    return text


def _config_from_semantic_dict(payload: object) -> NativeCorpusConfig:
    config = _exact_object(
        "config",
        payload,
        {
            "seed",
            "max_attempt_series",
            "max_frontier_states",
            "max_positions_per_series",
            "max_positions_per_game",
            "candidate_count",
            "policy",
            "schedule",
            "engine_version",
            "engine_source_fingerprint",
            "ruleset_version",
        },
    )
    policy_payload = _exact_object(
        "config.policy",
        config["policy"],
        {
            "kind",
            "preserve_returned_mate",
            "top_weight_basis_points",
            "near_weight_basis_points",
            "tail_weight_basis_points",
            "top_rank_count",
            "near_rank_count",
        },
    )
    if policy_payload["preserve_returned_mate"] is not True:
        raise NativeGenerationContractError(
            "config.policy.preserve_returned_mate must be true"
        )
    try:
        policy = NativeRankPolicy(
            kind=_strict_int("config.policy.kind", policy_payload["kind"]),
            top_weight_basis_points=_strict_int(
                "config.policy.top_weight_basis_points",
                policy_payload["top_weight_basis_points"],
            ),
            near_weight_basis_points=_strict_int(
                "config.policy.near_weight_basis_points",
                policy_payload["near_weight_basis_points"],
            ),
            tail_weight_basis_points=_strict_int(
                "config.policy.tail_weight_basis_points",
                policy_payload["tail_weight_basis_points"],
            ),
            top_rank_count=_strict_int(
                "config.policy.top_rank_count", policy_payload["top_rank_count"]
            ),
            near_rank_count=_strict_int(
                "config.policy.near_rank_count", policy_payload["near_rank_count"]
            ),
        )
        return NativeCorpusConfig(
            seed=_strict_int("config.seed", config["seed"]),
            max_attempt_series=_strict_int(
                "config.max_attempt_series", config["max_attempt_series"]
            ),
            max_frontier_states=_strict_int(
                "config.max_frontier_states", config["max_frontier_states"]
            ),
            max_positions_per_series=_strict_int(
                "config.max_positions_per_series",
                config["max_positions_per_series"],
            ),
            max_positions_per_game=_strict_int(
                "config.max_positions_per_game", config["max_positions_per_game"]
            ),
            candidate_count=_strict_int(
                "config.candidate_count", config["candidate_count"]
            ),
            policy=policy,
            schedule=NativeProfileSchedule(
                _strict_int("config.schedule", config["schedule"])
            ),
            engine_version=_strict_text(
                "config.engine_version", config["engine_version"]
            ),
            engine_source_fingerprint=_strict_text(
                "config.engine_source_fingerprint",
                config["engine_source_fingerprint"],
            ),
            ruleset_version=_strict_text(
                "config.ruleset_version", config["ruleset_version"]
            ),
        )
    except (TypeError, ValueError) as error:
        if isinstance(error, NativeGenerationContractError):
            raise
        raise NativeGenerationContractError(
            f"config semantic preimage is invalid: {error}"
        ) from error


def _profile_from_semantic_dict(payload: object) -> NativeCorpusProfile:
    profile = _exact_object(
        "ordered profile",
        payload,
        {"profile_id", "digest_sha256", "native_weights"},
    )
    weights = _exact_object(
        "ordered profile native_weights",
        profile["native_weights"],
        set(_NATIVE_PROFILE_WEIGHT_NAMES),
    )
    digest_hex = _strict_sha256("ordered profile digest_sha256", profile["digest_sha256"])
    try:
        return NativeCorpusProfile(
            profile_id=_strict_text("ordered profile profile_id", profile["profile_id"]),
            digest=bytes.fromhex(digest_hex),
            **{
                name: _strict_int(f"ordered profile {name}", weights[name])
                for name in _NATIVE_PROFILE_WEIGHT_NAMES
            },
        )
    except (TypeError, ValueError) as error:
        if isinstance(error, NativeGenerationContractError):
            raise
        raise NativeGenerationContractError(f"ordered profile is invalid: {error}") from error


def _identity_from_dict(payload: object) -> CorpusIdentity:
    identity = _exact_object(
        "identity",
        payload,
        {
            "generator_config_sha256",
            "profile_ids",
            "record_schema",
            "ruleset_version",
            "source_fingerprint",
        },
    )
    profile_ids = identity["profile_ids"]
    if not isinstance(profile_ids, list) or not all(
        isinstance(profile_id, str) for profile_id in profile_ids
    ):
        raise NativeGenerationContractError("identity.profile_ids must be strings")
    try:
        return CorpusIdentity(
            generator_config_sha256=_strict_text(
                "identity.generator_config_sha256",
                identity["generator_config_sha256"],
            ),
            profile_ids=tuple(profile_ids),
            record_schema=_strict_text(
                "identity.record_schema", identity["record_schema"]
            ),
            ruleset_version=_strict_text(
                "identity.ruleset_version", identity["ruleset_version"]
            ),
            source_fingerprint=_strict_text(
                "identity.source_fingerprint", identity["source_fingerprint"]
            ),
        )
    except ValueError as error:
        raise NativeGenerationContractError(f"identity is invalid: {error}") from error


@dataclass(frozen=True, slots=True)
class NativeGenerationContract:
    config: NativeCorpusConfig
    ordered_profiles: tuple[NativeCorpusProfile, ...]
    identity: CorpusIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.config, NativeCorpusConfig):
            raise TypeError("config must be a NativeCorpusConfig")
        profiles = bind_native_profiles(self.ordered_profiles)
        object.__setattr__(self, "ordered_profiles", profiles)
        if not isinstance(self.identity, CorpusIdentity):
            raise TypeError("identity must be a CorpusIdentity")
        expected_digest = semantic_config_digest(self.config, profiles).hex()
        expected_profile_ids = tuple(profile.profile_id for profile in profiles)
        if self.identity.generator_config_sha256 != expected_digest:
            raise NativeGenerationContractError(
                "identity does not bind the config/profile semantic preimage"
            )
        if self.identity.profile_ids != expected_profile_ids:
            raise NativeGenerationContractError(
                "identity does not bind the ordered native profiles"
            )
        if self.identity.record_schema != NATIVE_BOUNDARY_SAMPLE_SCHEMA:
            raise NativeGenerationContractError("identity record schema is unsupported")
        if self.identity.source_fingerprint != self.config.engine_source_fingerprint:
            raise NativeGenerationContractError(
                "identity source fingerprint does not match the config"
            )
        if self.identity.ruleset_version != self.config.ruleset_version:
            raise NativeGenerationContractError(
                "identity ruleset does not match the config"
            )

    @classmethod
    def from_plan(cls, plan: CorpusGenerationPlan) -> NativeGenerationContract:
        if not isinstance(plan, CorpusGenerationPlan):
            raise TypeError("plan must be a CorpusGenerationPlan")
        return cls(plan.config, bind_native_profiles(plan.profiles), plan.identity)

    @classmethod
    def from_dict(cls, payload: object) -> NativeGenerationContract:
        sidecar = _exact_object(
            "native generation contract",
            payload,
            {
                "abi_version",
                "config",
                "contract_sha256",
                "format",
                "generator_config_sha256",
                "identity",
                "identity_sha256",
                "ordered_profiles",
                "semantic_config_schema",
            },
        )
        if sidecar["format"] != NATIVE_GENERATION_CONTRACT_FORMAT:
            raise NativeGenerationContractError("native generation contract format is invalid")
        if _strict_int("abi_version", sidecar["abi_version"]) != FULL_GAME_V2_VERSION:
            raise NativeGenerationContractError("native generation ABI version is unsupported")
        if sidecar["semantic_config_schema"] != NATIVE_SEMANTIC_CONFIG_SCHEMA:
            raise NativeGenerationContractError("semantic config schema is unsupported")
        ordered_payload = sidecar["ordered_profiles"]
        if not isinstance(ordered_payload, list) or not ordered_payload:
            raise NativeGenerationContractError("ordered_profiles must be a nonempty list")
        contract = cls(
            config=_config_from_semantic_dict(sidecar["config"]),
            ordered_profiles=tuple(
                _profile_from_semantic_dict(profile) for profile in ordered_payload
            ),
            identity=_identity_from_dict(sidecar["identity"]),
        )
        _strict_sha256("contract_sha256", sidecar["contract_sha256"])
        _strict_sha256("generator_config_sha256", sidecar["generator_config_sha256"])
        _strict_sha256("identity_sha256", sidecar["identity_sha256"])
        if sidecar != contract.as_dict():
            raise NativeGenerationContractError(
                "native generation contract digest or redundant identity fields differ"
            )
        return contract

    def _content(self) -> dict[str, Any]:
        return {
            "abi_version": FULL_GAME_V2_VERSION,
            "config": self.config.as_semantic_dict(),
            "format": NATIVE_GENERATION_CONTRACT_FORMAT,
            "generator_config_sha256": self.identity.generator_config_sha256,
            "identity": self.identity.as_dict(),
            "identity_sha256": self.identity.digest_hex,
            "ordered_profiles": [
                profile.as_semantic_dict() for profile in self.ordered_profiles
            ],
            "semantic_config_schema": NATIVE_SEMANTIC_CONFIG_SCHEMA,
        }

    @property
    def digest_hex(self) -> str:
        return _contract_digest(self._content())

    def as_dict(self) -> dict[str, Any]:
        return {**self._content(), "contract_sha256": self.digest_hex}


def _outcome_exact_object(
    name: str,
    value: object,
    required: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        raise NativeShardOutcomeError(f"{name} has an invalid schema")
    return value


def _outcome_nonnegative_int(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise NativeShardOutcomeError(f"{name} must be a nonnegative integer")
    return value


def _outcome_sha256(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        or value == "0" * 64
    ):
        raise NativeShardOutcomeError(f"{name} must be a nonzero lowercase SHA-256")
    return value


def _outcome_counter(
    name: str,
    value: object,
    allowed: frozenset[str],
) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, dict):
        raise NativeShardOutcomeError(f"{name} must be an object")
    pairs: list[tuple[str, int]] = []
    for key, count in value.items():
        if not isinstance(key, str) or key not in allowed:
            raise NativeShardOutcomeError(f"{name} contains an unsupported outcome")
        if type(count) is not int or count <= 0:
            raise NativeShardOutcomeError(f"{name} counts must be positive integers")
        pairs.append((key, count))
    return tuple(sorted(pairs))


@dataclass(frozen=True, slots=True)
class NativeShardOutcomeReceipt:
    """Durable native generation outcomes bound to one finalized binary shard."""

    attempt_range: AttemptRange
    identity_sha256: str
    generation_contract_sha256: str
    shard_file: str
    shard_sha256: str
    shard_size_bytes: int
    record_count: int
    accepted_games: int
    rejected_attempts: int
    logical_work: int
    path_count_saturations: int
    terminal_counts: tuple[tuple[str, int], ...]
    rejection_counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_range, AttemptRange):
            raise TypeError("attempt_range must be an AttemptRange")
        _outcome_sha256("identity_sha256", self.identity_sha256)
        _outcome_sha256(
            "generation_contract_sha256", self.generation_contract_sha256
        )
        shard_sha256 = _outcome_sha256("shard.sha256", self.shard_sha256)
        expected_file = (
            f"shards/shard-{self.attempt_range.start:020d}-"
            f"{self.attempt_range.stop:020d}-{shard_sha256[:16]}.spcbin"
        )
        if self.shard_file != expected_file:
            raise NativeShardOutcomeError(
                "shard.file does not match its attempt range and SHA-256"
            )
        for name in (
            "shard_size_bytes",
            "record_count",
            "accepted_games",
            "rejected_attempts",
            "logical_work",
            "path_count_saturations",
        ):
            _outcome_nonnegative_int(name, getattr(self, name))
        terminal_counts = _outcome_counter(
            "terminal_counts", dict(self.terminal_counts), _NATIVE_TERMINAL_NAMES
        )
        rejection_counts = _outcome_counter(
            "rejection_counts", dict(self.rejection_counts), _NATIVE_REJECTION_NAMES
        )
        if terminal_counts != tuple(self.terminal_counts):
            raise NativeShardOutcomeError("terminal_counts are not canonical")
        if rejection_counts != tuple(self.rejection_counts):
            raise NativeShardOutcomeError("rejection_counts are not canonical")
        if self.accepted_games + self.rejected_attempts != self.attempt_count:
            raise NativeShardOutcomeError(
                "accepted and rejected outcomes do not cover the full attempt range"
            )
        if sum(count for _, count in terminal_counts) != self.accepted_games:
            raise NativeShardOutcomeError(
                "terminal counts do not equal accepted game count"
            )
        if sum(count for _, count in rejection_counts) != self.rejected_attempts:
            raise NativeShardOutcomeError(
                "rejection counts do not equal rejected attempt count"
            )

    @property
    def attempt_count(self) -> int:
        return self.attempt_range.stop - self.attempt_range.start

    @property
    def file_name(self) -> str:
        return (
            f"outcome-{self.attempt_range.start:020d}-"
            f"{self.attempt_range.stop:020d}.json"
        )

    def _content(self) -> dict[str, Any]:
        return {
            "attempt_count": self.attempt_count,
            "attempt_start": self.attempt_range.start,
            "attempt_stop": self.attempt_range.stop,
            "format": NATIVE_SHARD_OUTCOME_FORMAT,
            "generation_contract_sha256": self.generation_contract_sha256,
            "identity_sha256": self.identity_sha256,
            "outcomes": {
                "accepted_games": self.accepted_games,
                "logical_work": self.logical_work,
                "path_count_saturations": self.path_count_saturations,
                "rejected_attempts": self.rejected_attempts,
                "rejection_counts": dict(self.rejection_counts),
                "terminal_counts": dict(self.terminal_counts),
            },
            "shard": {
                "file": self.shard_file,
                "record_count": self.record_count,
                "sha256": self.shard_sha256,
                "size_bytes": self.shard_size_bytes,
            },
        }

    @property
    def digest_hex(self) -> str:
        return _outcome_digest(self._content())

    def as_dict(self) -> dict[str, Any]:
        return {**self._content(), "receipt_sha256": self.digest_hex}

    @classmethod
    def from_dict(cls, payload: object) -> NativeShardOutcomeReceipt:
        sidecar = _outcome_exact_object(
            "native shard outcome receipt",
            payload,
            {
                "attempt_count",
                "attempt_start",
                "attempt_stop",
                "format",
                "generation_contract_sha256",
                "identity_sha256",
                "outcomes",
                "receipt_sha256",
                "shard",
            },
        )
        if sidecar["format"] != NATIVE_SHARD_OUTCOME_FORMAT:
            raise NativeShardOutcomeError("native shard outcome format is invalid")
        attempt_start = _outcome_nonnegative_int(
            "attempt_start", sidecar["attempt_start"]
        )
        attempt_stop = _outcome_nonnegative_int(
            "attempt_stop", sidecar["attempt_stop"]
        )
        try:
            attempt_range = AttemptRange(attempt_start, attempt_stop)
        except ValueError as error:
            raise NativeShardOutcomeError("attempt range is invalid") from error
        if (
            _outcome_nonnegative_int("attempt_count", sidecar["attempt_count"])
            != attempt_stop - attempt_start
        ):
            raise NativeShardOutcomeError("attempt_count does not match the range")
        shard = _outcome_exact_object(
            "shard",
            sidecar["shard"],
            {"file", "record_count", "sha256", "size_bytes"},
        )
        outcomes = _outcome_exact_object(
            "outcomes",
            sidecar["outcomes"],
            {
                "accepted_games",
                "logical_work",
                "path_count_saturations",
                "rejected_attempts",
                "rejection_counts",
                "terminal_counts",
            },
        )
        if not isinstance(shard["file"], str):
            raise NativeShardOutcomeError("shard.file must be a string")
        receipt = cls(
            attempt_range=attempt_range,
            identity_sha256=_outcome_sha256(
                "identity_sha256", sidecar["identity_sha256"]
            ),
            generation_contract_sha256=_outcome_sha256(
                "generation_contract_sha256",
                sidecar["generation_contract_sha256"],
            ),
            shard_file=shard["file"],
            shard_sha256=_outcome_sha256("shard.sha256", shard["sha256"]),
            shard_size_bytes=_outcome_nonnegative_int(
                "shard.size_bytes", shard["size_bytes"]
            ),
            record_count=_outcome_nonnegative_int(
                "shard.record_count", shard["record_count"]
            ),
            accepted_games=_outcome_nonnegative_int(
                "outcomes.accepted_games", outcomes["accepted_games"]
            ),
            rejected_attempts=_outcome_nonnegative_int(
                "outcomes.rejected_attempts", outcomes["rejected_attempts"]
            ),
            logical_work=_outcome_nonnegative_int(
                "outcomes.logical_work", outcomes["logical_work"]
            ),
            path_count_saturations=_outcome_nonnegative_int(
                "outcomes.path_count_saturations",
                outcomes["path_count_saturations"],
            ),
            terminal_counts=_outcome_counter(
                "outcomes.terminal_counts",
                outcomes["terminal_counts"],
                _NATIVE_TERMINAL_NAMES,
            ),
            rejection_counts=_outcome_counter(
                "outcomes.rejection_counts",
                outcomes["rejection_counts"],
                _NATIVE_REJECTION_NAMES,
            ),
        )
        _outcome_sha256("receipt_sha256", sidecar["receipt_sha256"])
        if sidecar != receipt.as_dict():
            raise NativeShardOutcomeError(
                "native shard outcome receipt digest or redundant fields differ"
            )
        return receipt

    def verify_binding(
        self,
        metadata: ShardMetadata,
        identity: CorpusIdentity,
        generation_contract: NativeGenerationContract,
    ) -> None:
        if not isinstance(metadata, ShardMetadata):
            raise TypeError("metadata must be ShardMetadata")
        if not isinstance(identity, CorpusIdentity):
            raise TypeError("identity must be CorpusIdentity")
        if not isinstance(generation_contract, NativeGenerationContract):
            raise TypeError("generation_contract must be NativeGenerationContract")
        if generation_contract.identity != identity:
            raise NativeShardOutcomeError(
                "generation contract and corpus identity differ"
            )
        if self.identity_sha256 != identity.digest_hex:
            raise NativeShardOutcomeError("outcome receipt has a different identity")
        if self.generation_contract_sha256 != generation_contract.digest_hex:
            raise NativeShardOutcomeError(
                "outcome receipt has a different generation contract"
            )
        if (
            self.attempt_range != metadata.attempt_range
            or self.shard_file != metadata.file
            or self.shard_sha256 != metadata.sha256
            or self.shard_size_bytes != metadata.size_bytes
            or self.record_count != metadata.record_count
        ):
            raise NativeShardOutcomeError(
                "outcome receipt does not bind the finalized shard metadata"
            )
        if (
            metadata.producer_receipt_sha256 is not None
            and metadata.producer_receipt_sha256 != self.digest_hex
        ):
            raise NativeShardOutcomeError(
                "manifest producer receipt digest does not match the outcome receipt"
            )


def read_native_generation_contract(root: str | Path) -> NativeGenerationContract:
    path = Path(root).expanduser().resolve() / NATIVE_GENERATION_CONTRACT_FILE
    try:
        payload = _read_canonical_json(path)
    except (OSError, ValueError) as error:
        raise NativeGenerationContractError(
            f"could not read native generation contract: {error}"
        ) from error
    return NativeGenerationContract.from_dict(payload)


def _native_outcomes_directory(root: str | Path) -> Path:
    return Path(root).expanduser().resolve() / NATIVE_OUTCOMES_DIRECTORY


def _outcome_path(root: str | Path, attempt_range: AttemptRange) -> Path:
    return _native_outcomes_directory(root) / (
        f"outcome-{attempt_range.start:020d}-{attempt_range.stop:020d}.json"
    )


def _ensure_native_outcomes_directory(store: CorpusStore) -> Path:
    directory = _native_outcomes_directory(store.root)
    with _exclusive_store_lock(store.root):
        if directory.exists() and not directory.is_dir():
            raise NativeShardOutcomeError(
                f"{NATIVE_OUTCOMES_DIRECTORY} is not a directory"
            )
        if not directory.exists():
            directory.mkdir()
            _fsync_directory(store.root)
    return directory


def read_native_shard_outcome(
    root: str | Path,
    attempt_start: int,
    attempt_stop: int,
) -> NativeShardOutcomeReceipt:
    """Read and strictly validate one canonical per-range outcome receipt."""

    try:
        attempt_range = AttemptRange(attempt_start, attempt_stop)
        payload = _read_canonical_json(_outcome_path(root, attempt_range))
        receipt = NativeShardOutcomeReceipt.from_dict(payload)
    except (OSError, ValueError) as error:
        if isinstance(error, NativeShardOutcomeError):
            raise
        raise NativeShardOutcomeError(
            f"could not read native shard outcome receipt: {error}"
        ) from error
    if receipt.attempt_range != attempt_range or receipt.file_name != _outcome_path(
        root, attempt_range
    ).name:
        raise NativeShardOutcomeError(
            "native shard outcome filename and attempt range differ"
        )
    return receipt


def _read_bound_native_shard_outcome(
    store: CorpusStore,
    generation_contract: NativeGenerationContract,
    metadata: ShardMetadata,
) -> NativeShardOutcomeReceipt:
    if metadata.producer_receipt_sha256 is None:
        raise NativeShardOutcomeError(
            "finalized native shard has no manifest-bound outcome receipt"
        )
    receipt = read_native_shard_outcome(
        store.root,
        metadata.attempt_range.start,
        metadata.attempt_range.stop,
    )
    receipt.verify_binding(metadata, store.identity, generation_contract)
    return receipt


def _persist_native_shard_outcome(
    store: CorpusStore,
    generation_contract: NativeGenerationContract,
    receipt: NativeShardOutcomeReceipt,
    metadata: ShardMetadata,
) -> NativeShardOutcomeReceipt:
    """Persist a receipt from the writer's locked pre-publish callback.

    The caller holds the corpus store lock.  Writing the range-keyed receipt
    before the shard rename makes crash recovery safe: an adoptable orphan
    shard can never predate its durable outcome provenance.
    """

    receipt.verify_binding(metadata, store.identity, generation_contract)
    path = _outcome_path(store.root, receipt.attempt_range)
    if path.exists():
        existing = read_native_shard_outcome(
            store.root,
            receipt.attempt_range.start,
            receipt.attempt_range.stop,
        )
        if existing.as_dict() != receipt.as_dict():
            raise NativeShardOutcomeError(
                "existing native shard outcome conflicts with regenerated outcomes"
            )
    else:
        _atomic_write_json(path, receipt.as_dict())
    persisted = read_native_shard_outcome(
        store.root,
        receipt.attempt_range.start,
        receipt.attempt_range.stop,
    )
    if persisted.as_dict() != receipt.as_dict():
        raise NativeShardOutcomeError(
            "persisted native shard outcome differs from generated outcomes"
        )
    persisted.verify_binding(metadata, store.identity, generation_contract)
    return persisted


@dataclass(frozen=True, slots=True)
class CorpusGenerationPlan:
    root: Path
    config: NativeCorpusConfig
    profiles: tuple[EngineProfile | NativeCorpusProfile, ...]
    first_attempt: int
    attempt_count: int
    shard_size: int = 10_000
    batch_size: int = 256
    workers: int = 1
    protocol_root_binding_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())
        object.__setattr__(self, "profiles", tuple(self.profiles))
        if not isinstance(self.config, NativeCorpusConfig):
            raise TypeError("config must be a NativeCorpusConfig")
        bind_native_profiles(self.profiles)
        for name in ("first_attempt", "attempt_count", "shard_size", "batch_size", "workers"):
            value = getattr(self, name)
            minimum = 0 if name == "first_attempt" else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer of at least {minimum}")
        if self.first_attempt >= 1 << 64:
            raise ValueError("first_attempt must fit unsigned 64 bits")
        if self.attempt_count > (1 << 64) - self.first_attempt:
            raise ValueError("corpus attempt range overflows unsigned 64 bits")
        if self.batch_size > (1 << 32) - 1:
            raise ValueError("batch_size exceeds the native v2 request limit")
        if self.protocol_root_binding_sha256 is not None and (
            not isinstance(self.protocol_root_binding_sha256, str)
            or len(self.protocol_root_binding_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.protocol_root_binding_sha256
            )
        ):
            raise ValueError("protocol_root_binding_sha256 must be a lowercase SHA-256")

    @property
    def attempt_stop(self) -> int:
        return self.first_attempt + self.attempt_count

    @property
    def identity(self) -> CorpusIdentity:
        profiles = bind_native_profiles(self.profiles)
        return CorpusIdentity(
            record_schema=NATIVE_BOUNDARY_SAMPLE_SCHEMA,
            source_fingerprint=self.config.engine_source_fingerprint,
            generator_config_sha256=semantic_config_digest(
                self.config, profiles
            ).hex(),
            profile_ids=tuple(profile.profile_id for profile in profiles),
            ruleset_version=self.config.ruleset_version,
        )

    @property
    def shard_ranges(self) -> tuple[AttemptRange, ...]:
        ranges: list[AttemptRange] = []
        start = self.first_attempt
        while start < self.attempt_stop:
            stop = min(start + self.shard_size, self.attempt_stop)
            ranges.append(AttemptRange(start, stop))
            start = stop
        return tuple(ranges)


def _materialize_native_generation_contract(
    plan: CorpusGenerationPlan,
    store: CorpusStore,
) -> NativeGenerationContract:
    if store.identity != plan.identity or store.root != plan.root:
        raise NativeGenerationContractError(
            "generation plan does not match the target corpus store"
        )
    expected = NativeGenerationContract.from_plan(plan)
    path = store.root / NATIVE_GENERATION_CONTRACT_FILE
    with _exclusive_store_lock(store.root):
        if path.exists():
            try:
                existing = NativeGenerationContract.from_dict(
                    _read_canonical_json(path)
                )
            except (OSError, ValueError) as error:
                raise NativeGenerationContractError(
                    f"existing native generation contract is invalid: {error}"
                ) from error
            if existing.as_dict() != expected.as_dict():
                raise NativeGenerationContractError(
                    "existing native generation contract conflicts with the supplied plan"
                )
        else:
            _atomic_write_json(path, expected.as_dict())
        try:
            persisted = NativeGenerationContract.from_dict(
                _read_canonical_json(path)
            )
        except (OSError, ValueError) as error:
            raise NativeGenerationContractError(
                f"persisted native generation contract is invalid: {error}"
            ) from error
        if persisted.as_dict() != expected.as_dict():
            raise NativeGenerationContractError(
                "persisted native generation contract differs from the supplied plan"
            )
    return persisted


def materialize_native_generation_contract(
    plan: CorpusGenerationPlan,
) -> NativeGenerationContract:
    """Create or verify the immutable contract for generation or safe backfill."""

    if not isinstance(plan, CorpusGenerationPlan):
        raise TypeError("plan must be a CorpusGenerationPlan")
    store = CorpusStore(
        plan.root,
        plan.identity,
        protocol_root_binding_sha256=plan.protocol_root_binding_sha256,
    )
    return _materialize_native_generation_contract(plan, store)


@dataclass(frozen=True, slots=True)
class GeneratedShard:
    outcome: NativeShardOutcomeReceipt
    elapsed_seconds: float

    @property
    def attempt_range(self) -> AttemptRange:
        return self.outcome.attempt_range


def _owner_id(identity: CorpusIdentity, attempt_range: AttemptRange) -> str:
    return (
        f"spc-native-corpus-producer-v1:{identity.digest_hex}:"
        f"{attempt_range.start}:{attempt_range.stop}"
    )


def _generate_shard(
    store: CorpusStore,
    plan: CorpusGenerationPlan,
    generation_contract: NativeGenerationContract,
    attempt_range: AttemptRange,
) -> GeneratedShard:
    started = time.perf_counter()
    writer = store.begin_shard(
        attempt_range.start,
        attempt_range.stop,
        owner_id=_owner_id(store.identity, attempt_range),
    )
    accepted_games = 0
    rejected_attempts = 0
    logical_work = 0
    path_count_saturations = 0
    terminal_counts: Counter[str] = Counter()
    rejection_counts: Counter[str] = Counter()
    try:
        batch_start = attempt_range.start
        while batch_start < attempt_range.stop:
            batch_count = min(plan.batch_size, attempt_range.stop - batch_start)
            batch = generate_native_full_game_batch(
                plan.config,
                plan.profiles,
                first_attempt=batch_start,
                attempt_count=batch_count,
            )
            if (
                batch.first_attempt != batch_start
                or batch.attempt_count != batch_count
                or len(batch.records) != batch_count
            ):
                raise NativeShardOutcomeError(
                    "native batch does not cover its complete requested attempt range"
                )
            expected_attempt = batch_start
            batch_saturations = 0
            for record in batch.records:
                if record.attempt_index != expected_attempt:
                    raise NativeShardOutcomeError(
                        "native batch attempts are missing, duplicated, or out of order"
                    )
                expected_attempt += 1
                batch_saturations += record.path_count_saturations
            if batch_saturations != batch.total_saturations:
                raise NativeShardOutcomeError(
                    "native batch saturation total differs from its records"
                )
            logical_work += batch.logical_work
            path_count_saturations += batch.total_saturations
            games_by_attempt = {
                game.record.attempt_index: game for game in replay_native_batch(batch)
            }
            for record in batch.records:
                if not record.accepted:
                    rejected_attempts += 1
                    rejection_counts[record.reject.name.lower()] += 1
                    continue
                terminal_counts[record.terminal.name.lower()] += 1
                accepted_games += 1
                game = games_by_attempt[record.attempt_index]
                for sequence_index, state in enumerate(game.states):
                    sample = sample_from_native_game(state, record)
                    writer.add_state(
                        record.attempt_index,
                        sequence_index,
                        state,
                        encode_native_boundary_sample(sample),
                    )
            batch_start += batch_count
        if accepted_games + rejected_attempts != (
            attempt_range.stop - attempt_range.start
        ):
            raise NativeShardOutcomeError(
                "native outcomes do not cover the complete shard attempt range"
            )
        persisted_outcome: NativeShardOutcomeReceipt | None = None

        def publish_outcome(metadata: ShardMetadata) -> str:
            nonlocal persisted_outcome
            receipt = NativeShardOutcomeReceipt(
                attempt_range=attempt_range,
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
                terminal_counts=tuple(sorted(terminal_counts.items())),
                rejection_counts=tuple(sorted(rejection_counts.items())),
            )
            persisted_outcome = _persist_native_shard_outcome(
                store,
                generation_contract,
                receipt,
                metadata,
            )
            return persisted_outcome.digest_hex

        metadata = writer.finalize(before_publish=publish_outcome)
        if persisted_outcome is None:
            raise NativeShardOutcomeError(
                "shard finalized without publishing its native outcome receipt"
            )
        persisted_outcome.verify_binding(
            metadata, store.identity, generation_contract
        )
    except BaseException:
        writer.abort()
        raise
    return GeneratedShard(
        outcome=persisted_outcome,
        elapsed_seconds=time.perf_counter() - started,
    )


def _pending_ranges(
    store: CorpusStore,
    planned: Sequence[AttemptRange],
) -> tuple[tuple[ShardMetadata, ...], tuple[AttemptRange, ...]]:
    existing = store.shards
    completed: list[ShardMetadata] = []
    pending: list[AttemptRange] = []
    for attempt_range in planned:
        exact = tuple(
            shard for shard in existing if shard.attempt_range == attempt_range
        )
        overlaps = tuple(
            shard.attempt_range
            for shard in existing
            if shard.attempt_range.overlaps(attempt_range)
        )
        if len(exact) == 1:
            completed.append(exact[0])
        elif overlaps:
            raise AttemptRangeConflict(
                f"planned range {attempt_range} partially overlaps finalized ranges {overlaps}"
            )
        else:
            pending.append(attempt_range)
    return tuple(completed), tuple(pending)


def _aggregate_native_outcomes(
    receipts: Sequence[NativeShardOutcomeReceipt],
) -> dict[str, object]:
    terminal_counts: Counter[str] = Counter()
    rejection_counts: Counter[str] = Counter()
    for receipt in receipts:
        terminal_counts.update(dict(receipt.terminal_counts))
        rejection_counts.update(dict(receipt.rejection_counts))
    return {
        "attempt_count": sum(receipt.attempt_count for receipt in receipts),
        "accepted_games": sum(receipt.accepted_games for receipt in receipts),
        "rejected_attempts": sum(receipt.rejected_attempts for receipt in receipts),
        "record_count": sum(receipt.record_count for receipt in receipts),
        "logical_work": sum(receipt.logical_work for receipt in receipts),
        "path_count_saturations": sum(
            receipt.path_count_saturations for receipt in receipts
        ),
        "terminal_counts": dict(sorted(terminal_counts.items())),
        "rejection_counts": dict(sorted(rejection_counts.items())),
    }


def generate_corpus(plan: CorpusGenerationPlan) -> dict[str, object]:
    """Generate all missing shards and return a verified resumable receipt."""

    if not isinstance(plan, CorpusGenerationPlan):
        raise TypeError("plan must be a CorpusGenerationPlan")
    validate_current_native_generation_config(plan.config)
    store = CorpusStore(
        plan.root,
        plan.identity,
        protocol_root_binding_sha256=plan.protocol_root_binding_sha256,
    )
    generation_contract = _materialize_native_generation_contract(plan, store)
    _ensure_native_outcomes_directory(store)
    completed, pending = _pending_ranges(store, plan.shard_ranges)
    for metadata in completed:
        _read_bound_native_shard_outcome(store, generation_contract, metadata)
    generated: list[GeneratedShard] = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=plan.workers) as executor:
        futures = {
            executor.submit(
                _generate_shard,
                store,
                plan,
                generation_contract,
                attempt_range,
            ): attempt_range
            for attempt_range in pending
        }
        for future in as_completed(futures):
            generated.append(future.result())
    elapsed = time.perf_counter() - started
    verification = store.verify()
    finalized, still_pending = _pending_ranges(store, plan.shard_ranges)
    if still_pending or len(finalized) != len(plan.shard_ranges):
        raise NativeShardOutcomeError(
            "generation returned without finalizing every planned attempt range"
        )
    all_outcomes = tuple(
        _read_bound_native_shard_outcome(store, generation_contract, metadata)
        for metadata in finalized
    )
    outcome_totals = _aggregate_native_outcomes(all_outcomes)
    if outcome_totals["attempt_count"] != plan.attempt_count:
        raise NativeShardOutcomeError(
            "durable outcome receipts do not cover every planned attempt"
        )
    generated_outcomes = tuple(item.outcome for item in generated)
    generated_totals = _aggregate_native_outcomes(generated_outcomes)
    generated_attempts = int(generated_totals["attempt_count"])
    return {
        "format": "spc-native-corpus-generation-receipt-v1",
        "root": str(store.root),
        "identity_sha256": store.identity.digest_hex,
        "generator_config_sha256": store.identity.generator_config_sha256,
        "generation_contract": {
            "file": NATIVE_GENERATION_CONTRACT_FILE,
            "format": NATIVE_GENERATION_CONTRACT_FORMAT,
            "sha256": generation_contract.digest_hex,
        },
        "planned_attempt_start": plan.first_attempt,
        "planned_attempt_stop": plan.attempt_stop,
        "planned_attempt_count": plan.attempt_count,
        "shard_size": plan.shard_size,
        "batch_size": plan.batch_size,
        "workers": plan.workers,
        "already_complete_shards": len(completed),
        "generated_shards": len(generated),
        "generated_attempts": generated_attempts,
        "accepted_games": generated_totals["accepted_games"],
        "rejected_attempts": generated_totals["rejected_attempts"],
        "generated_records": generated_totals["record_count"],
        "logical_work": generated_totals["logical_work"],
        "path_count_saturations": generated_totals["path_count_saturations"],
        "terminal_counts": generated_totals["terminal_counts"],
        "rejection_counts": generated_totals["rejection_counts"],
        "outcome_totals": outcome_totals,
        "elapsed_seconds": elapsed,
        "attempts_per_second": (
            0.0 if elapsed == 0 else generated_attempts / elapsed
        ),
        "corpus": verification,
        "outcome_receipts": [
            {
                "attempt_start": receipt.attempt_range.start,
                "attempt_stop": receipt.attempt_range.stop,
                "file": f"{NATIVE_OUTCOMES_DIRECTORY}/{receipt.file_name}",
                "sha256": receipt.digest_hex,
                "shard_sha256": receipt.shard_sha256,
            }
            for receipt in all_outcomes
        ],
        "shards": [
            {
                "attempt_start": item.attempt_range.start,
                "attempt_stop": item.attempt_range.stop,
                "file": item.outcome.shard_file,
                "sha256": item.outcome.shard_sha256,
                "size_bytes": item.outcome.shard_size_bytes,
                "record_count": item.outcome.record_count,
                "outcome_receipt": {
                    "file": (
                        f"{NATIVE_OUTCOMES_DIRECTORY}/{item.outcome.file_name}"
                    ),
                    "sha256": item.outcome.digest_hex,
                },
                "elapsed_seconds": item.elapsed_seconds,
            }
            for item in sorted(generated, key=lambda value: value.attempt_range.start)
        ],
    }


def verify_native_boundary_corpus(
    store: CorpusStore,
    *,
    count_unique_states: bool = True,
    verified_snapshot: tuple[dict[str, Any], tuple[ShardMetadata, ...]] | None = None,
) -> dict[str, int | None]:
    """Decode every payload from one exact, unchanged corpus snapshot."""

    if not isinstance(store, CorpusStore):
        raise TypeError("store must be a CorpusStore")
    snapshot = store.verified_snapshot() if verified_snapshot is None else verified_snapshot
    manifest, shards = snapshot
    if (
        not isinstance(manifest, dict)
        or not isinstance(shards, tuple)
        or manifest.get("record_count")
        != sum(shard.record_count for shard in shards)
    ):
        raise ValueError("verified corpus snapshot is malformed")
    records = 0
    wins = 0
    losses = 0
    draws = 0
    seen: set[bytes] | None = set() if count_unique_states else None
    duplicate_states = 0
    for record in store.iter_snapshot_records(shards):
        sample = decode_native_boundary_sample(record.payload)
        expected_key = progressive_state_dedup_key(
            sample.state,
            ruleset_version=store.identity.ruleset_version,
        )
        if record.state_key != expected_key:
            raise ValueError(
                f"sample state does not match stored key at attempt {record.attempt_index}"
            )
        records += 1
        wins += sample.value_for_side_to_move == 1
        losses += sample.value_for_side_to_move == -1
        draws += sample.value_for_side_to_move == 0
        if seen is not None:
            if record.state_key in seen:
                duplicate_states += 1
            else:
                seen.add(record.state_key)
    result = {
        "records": records,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "unique_states": None if seen is None else len(seen),
        "duplicate_states": None if seen is None else duplicate_states,
    }
    if records != manifest["record_count"]:
        raise ValueError("verified corpus snapshot record count drifted")
    if store.verified_snapshot() != snapshot:
        raise ValueError("native corpus changed while payloads were verified")
    return result
