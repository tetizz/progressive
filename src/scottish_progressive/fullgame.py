from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import tempfile
import time
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Mapping, Protocol, Sequence

import chess

from .evaluation import _native_source_identity, fast_evaluate
from .fullgame_codec import (
    DecodedChunk,
    MAX_U32,
    MAX_U64,
    FullGameRecord,
    NativeV2Profile,
    RejectReason,
    RejectedAttempt,
    Terminal,
    chunk_sha256,
    decode_chunk,
    decode_native_batch,
    decode_native_batch_v2,
    encode_native_v2_request,
    encode_chunk,
    expected_v2_profile_pair,
    native_v2_profile_digest,
    native_v2_semantic_digest,
    NATIVE_V2_POLICY_UNIFORM,
    NATIVE_V2_CONFIG_DIGEST_END,
    NATIVE_V2_CONFIG_DIGEST_OFFSET,
    NATIVE_V2_SCHEDULE_ORDERED_PAIR_ROUND_ROBIN,
    NATIVE_V2_SCHEDULE_SELF_ROUND_ROBIN,
    replay_record,
    trace_sha256,
)
from .fullgame_identity import (
    FULLGAME_SEMANTIC_FINGERPRINT,
    FULLGAME_TERMINAL_SCORE,
)
from .model import (
    ENGINE_VERSION,
    RULESET_VERSION,
    Outcome,
    ProgressiveState,
    SeriesResult,
)
from .profiles import EngineProfile, EvaluationWeights, baseline_profile
from .rules import (
    GenerationStats,
    GenerationWorkLimit,
    NativeFrontierScoreConfig,
    generate_series,
)


FULLGAME_RUN_FORMAT = "spc-fullgame-run-v2"
FULLGAME_CHECKPOINT_SCHEMA = 3
FULLGAME_GENERATOR_CONTRACT_V1 = "spc-native-fullgame-v1"
FULLGAME_GENERATOR_CONTRACT_V2 = "spc-native-fullgame-v2"
FULLGAME_GENERATOR_CONTRACT = FULLGAME_GENERATOR_CONTRACT_V2
RANK_POLICY_ID = "spc-rank-mixture-80-15-5-v1"
UNIFORM_POLICY_ID = "spc-uniform-top-k-v1"
LABEL_KIND = "terminal-WDL"
SELFPLAY_SCOPE_V1 = "single-profile-both-colors-v1"
SELFPLAY_SCOPE_SINGLE_V2 = "profile-pool-self-round-robin-v2"
SELFPLAY_SCOPE_POOL_V2 = "profile-pool-ordered-pairs-v2"
SELFPLAY_SCOPE = SELFPLAY_SCOPE_SINGLE_V2
PROFILE_SCHEDULE_V1 = "implicit-single-profile-v1"
PROFILE_SCHEDULE_SELF_V2 = "self-round-robin-v2"
PROFILE_SCHEDULE_ORDERED_V2 = "ordered-pair-round-robin-v2"
DATA_PURPOSE = "exploration-rollout-v1"
STRENGTH_CLAIM = "not-champion-play"
DEFAULT_TARGET_UNIQUE_GAMES = 10_000
DEFAULT_ATTEMPTS_PER_CHUNK = 64
DEFAULT_FRONTIER_STATES = 8
DEFAULT_CANDIDATE_COUNT = 8
DEFAULT_POSITIONS_PER_SERIES = 5_000
DEFAULT_POSITIONS_PER_GAME = 5_000_000
DEFAULT_MEMORY_PER_WORKER_MB = 512
DEFAULT_RESERVE_MEMORY_MB = 1024
PROFILE_ID_PATTERN = re.compile(r"spc-[0-9a-f]{16}\Z")
CHUNK_FILENAME_PATTERN = re.compile(r"[0-9]{20}-[0-9]{20}\.spcg\Z")
PENDING_FILENAME_PATTERN = re.compile(
    r"\.[0-9]{20}-[0-9]{20}\.spcg\.pending\Z"
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
DISPOSITION_DIGEST_DOMAIN = b"SPC-FULLGAME-DISPOSITION-V1\0"
DISPOSITION_DIGEST_PREFIX = struct.Struct("<QBBQIIQ")
EXPECTED_NATIVE_SOURCE_IDENTITY = _native_source_identity()
REFERENCE_BACKEND_SOURCE_IDENTITY = f"python-{FULLGAME_SEMANTIC_FINGERPRINT}"

MASK_64 = (1 << 64) - 1
ATTEMPT_DOMAIN = 0x415454454D505456
SERIES_DOMAIN = 0x5345524945535631
LANE_DOMAIN = 0x4C414E4553504331


def _expected_backend_source_identity(backend_kind: str) -> str:
    if backend_kind == "native":
        if EXPECTED_NATIVE_SOURCE_IDENTITY is None:
            raise ValueError("native full-game source identity is unavailable")
        return EXPECTED_NATIVE_SOURCE_IDENTITY
    if backend_kind == "reference":
        return REFERENCE_BACKEND_SOURCE_IDENTITY
    raise ValueError("backend_kind must be native or reference")


def _validate_backend_binding(
    config: "FullGameSemanticConfig",
    backend_kind: str,
) -> None:
    if config.backend_kind != backend_kind:
        raise ValueError("full-game backend does not match its semantic simulation")
    if backend_kind == "native":
        from . import evaluation

        native = evaluation._native_eval
        if native is None or not hasattr(native, "generate_full_game_batch_v2"):
            raise ValueError(
                "source-matched native full-game v2 generation is unavailable"
            )
        if getattr(native, "SOURCE_IDENTITY", None) != config.backend_source_identity:
            raise ValueError("loaded native full-game source identity has drifted")


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(_canonical_json(payload))
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _acquire_writer_lock(path: Path) -> BinaryIO:
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - exercised by Linux CI
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        raise ValueError("another full-game writer already owns this run") from error
    return handle


def _release_writer_lock(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised by Linux CI
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & MASK_64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK_64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK_64
    return (value ^ (value >> 31)) & MASK_64


def decision_random(
    seed: int,
    attempt_index: int,
    series_number: int,
    lane: int,
) -> int:
    """Counter RNG shared byte-for-byte with the native full-game contract."""

    values = (seed, attempt_index, series_number, lane)
    if any(type(value) is not int for value in values) or any(
        not 0 <= value <= MAX_U64 for value in values
    ):
        raise ValueError("full-game RNG inputs must fit uint64")
    return _splitmix64(
        seed
        ^ _splitmix64(attempt_index ^ ATTEMPT_DOMAIN)
        ^ _splitmix64(series_number ^ SERIES_DOMAIN)
        ^ _splitmix64(lane ^ LANE_DOMAIN)
    )


@dataclass(frozen=True, slots=True)
class FullGameProfileConfig:
    profile_id: str
    material: int
    king_space: int
    series_reach: int
    promotion_corridors: int
    immediate_vulnerability: int
    useful_mobility: int
    boundary_check: int

    def __post_init__(self) -> None:
        if type(self.profile_id) is not str or not PROFILE_ID_PATTERN.fullmatch(
            self.profile_id
        ):
            raise ValueError("full-game profile_id is invalid")
        if any(
            type(value) is not int
            for value in (
                self.material,
                self.king_space,
                self.series_reach,
                self.promotion_corridors,
                self.immediate_vulnerability,
                self.useful_mobility,
                self.boundary_check,
            )
        ):
            raise ValueError("full-game profile weights must be exact integers")
        EvaluationWeights(**asdict(self.weights))

    @classmethod
    def from_engine_profile(cls, profile: EngineProfile) -> "FullGameProfileConfig":
        if type(profile) is not EngineProfile:
            raise ValueError("profile pool entries must be exact EngineProfile objects")
        return cls(profile_id=profile.profile_id, **asdict(profile.weights))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FullGameProfileConfig":
        if type(payload) is not dict or set(payload) != {
            "profile_digest",
            "profile_id",
            "weights",
        }:
            raise ValueError("full-game profile config keys are invalid")
        weights = payload["weights"]
        expected_weights = {
            "material",
            "king_space",
            "series_reach",
            "promotion_corridors",
            "immediate_vulnerability",
            "useful_mobility",
            "boundary_check",
        }
        if type(weights) is not dict or set(weights) != expected_weights:
            raise ValueError("full-game profile config weights are invalid")
        profile = cls(profile_id=payload["profile_id"], **weights)
        if payload["profile_digest"] != profile.profile_digest:
            raise ValueError("full-game profile digest does not match its weights")
        if profile.as_dict() != payload:
            raise ValueError("full-game profile config is not canonical")
        return profile

    @property
    def weights(self) -> EvaluationWeights:
        return EvaluationWeights(
            material=self.material,
            king_space=self.king_space,
            series_reach=self.series_reach,
            promotion_corridors=self.promotion_corridors,
            immediate_vulnerability=self.immediate_vulnerability,
            useful_mobility=self.useful_mobility,
            boundary_check=self.boundary_check,
        )

    @property
    def native_weights(self) -> tuple[int, int, int, int, int]:
        return (
            self.material,
            self.king_space,
            self.promotion_corridors,
            self.immediate_vulnerability,
            self.boundary_check,
        )

    @property
    def profile_digest_bytes(self) -> bytes:
        return native_v2_profile_digest(self.native_weights)

    @property
    def profile_digest(self) -> str:
        return self.profile_digest_bytes.hex()

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_digest": self.profile_digest,
            "profile_id": self.profile_id,
            "weights": asdict(self.weights),
        }


@dataclass(frozen=True, slots=True)
class FullGameSemanticConfig:
    seed: int
    profile_id: str
    material: int
    king_space: int
    series_reach: int
    promotion_corridors: int
    immediate_vulnerability: int
    useful_mobility: int
    boundary_check: int
    profile_pool: tuple[FullGameProfileConfig, ...] = ()
    max_attempt_series: int = 0
    max_frontier_states: int = DEFAULT_FRONTIER_STATES
    max_positions_per_series: int = DEFAULT_POSITIONS_PER_SERIES
    max_positions_per_game: int = DEFAULT_POSITIONS_PER_GAME
    candidate_count: int = DEFAULT_CANDIDATE_COUNT
    preserve_returned_mate: bool = True
    backend_kind: str = "native"
    backend_source_identity: str = EXPECTED_NATIVE_SOURCE_IDENTITY or "unavailable"
    engine_version: str = ENGINE_VERSION
    source_fingerprint: str = FULLGAME_SEMANTIC_FINGERPRINT
    ruleset_version: str = RULESET_VERSION
    generator_contract: str = FULLGAME_GENERATOR_CONTRACT_V2
    rank_policy_id: str = UNIFORM_POLICY_ID
    profile_schedule_id: str = PROFILE_SCHEDULE_SELF_V2
    selfplay_scope: str = SELFPLAY_SCOPE_SINGLE_V2
    data_purpose: str = DATA_PURPOSE
    strength_claim: str = STRENGTH_CLAIM

    def __post_init__(self) -> None:
        integer_fields = (
            "seed",
            "material",
            "king_space",
            "series_reach",
            "promotion_corridors",
            "immediate_vulnerability",
            "useful_mobility",
            "boundary_check",
            "max_attempt_series",
            "max_frontier_states",
            "max_positions_per_series",
            "max_positions_per_game",
            "candidate_count",
        )
        if any(type(getattr(self, field)) is not int for field in integer_fields):
            raise ValueError("full-game numeric config fields must be exact integers")
        string_fields = (
            "profile_id",
            "engine_version",
            "source_fingerprint",
            "ruleset_version",
            "generator_contract",
            "rank_policy_id",
            "profile_schedule_id",
            "selfplay_scope",
            "data_purpose",
            "strength_claim",
            "backend_kind",
            "backend_source_identity",
        )
        if any(type(getattr(self, field)) is not str for field in string_fields):
            raise ValueError("full-game identity config fields must be exact strings")
        if type(self.preserve_returned_mate) is not bool or not self.preserve_returned_mate:
            raise ValueError("full-game rollout must preserve every returned mate")
        if not 0 <= self.seed <= MAX_U64:
            raise ValueError("seed must fit uint64")
        primary = FullGameProfileConfig(
            profile_id=self.profile_id,
            material=self.material,
            king_space=self.king_space,
            series_reach=self.series_reach,
            promotion_corridors=self.promotion_corridors,
            immediate_vulnerability=self.immediate_vulnerability,
            useful_mobility=self.useful_mobility,
            boundary_check=self.boundary_check,
        )
        if type(self.profile_pool) is not tuple:
            raise ValueError("full-game profile pool must be an exact tuple")
        if not self.profile_pool:
            object.__setattr__(self, "profile_pool", (primary,))
        elif any(type(profile) is not FullGameProfileConfig for profile in self.profile_pool):
            raise ValueError("full-game profile pool entries are invalid")
        if self.profile_pool[0] != primary:
            raise ValueError("primary profile fields do not match profile_pool[0]")
        if not 1 <= len(self.profile_pool) <= 4096:
            raise ValueError("full-game profile pool size is invalid")
        if len({profile.profile_id for profile in self.profile_pool}) != len(
            self.profile_pool
        ) or len({profile.profile_digest for profile in self.profile_pool}) != len(
            self.profile_pool
        ):
            raise ValueError("full-game profile pool entries must be unique")
        if self.backend_source_identity != _expected_backend_source_identity(
            self.backend_kind
        ):
            raise ValueError("full-game backend source identity is stale")
        if not 0 <= self.max_attempt_series <= MAX_U64:
            raise ValueError("max_attempt_series must fit uint64")
        if not 1 <= self.max_frontier_states <= MAX_U64:
            raise ValueError("max_frontier_states must be positive")
        if not 1 <= self.max_positions_per_series <= MAX_U64:
            raise ValueError("max_positions_per_series must be positive")
        if not 1 <= self.max_positions_per_game <= MAX_U64:
            raise ValueError("max_positions_per_game must be positive")
        if not 1 <= self.candidate_count <= MAX_U32:
            raise ValueError("candidate_count must fit positive uint32")
        if self.candidate_count > self.max_frontier_states:
            raise ValueError("candidate_count cannot exceed max_frontier_states")
        if self.engine_version != ENGINE_VERSION:
            raise ValueError("full-game config engine version is stale")
        if self.source_fingerprint != FULLGAME_SEMANTIC_FINGERPRINT:
            raise ValueError("full-game semantic fingerprint is stale")
        if self.ruleset_version != RULESET_VERSION:
            raise ValueError("full-game config ruleset version is stale")
        if self.data_purpose != DATA_PURPOSE or self.strength_claim != STRENGTH_CLAIM:
            raise ValueError("full-game data-purpose labels are unsupported")

        if self.backend_kind == "native":
            expected_schedule = (
                PROFILE_SCHEDULE_SELF_V2
                if len(self.profile_pool) == 1
                else PROFILE_SCHEDULE_ORDERED_V2
            )
            expected_scope = (
                SELFPLAY_SCOPE_SINGLE_V2
                if len(self.profile_pool) == 1
                else SELFPLAY_SCOPE_POOL_V2
            )
            if self.generator_contract != FULLGAME_GENERATOR_CONTRACT_V2:
                raise ValueError("native full-game generator contract must be v2")
            if self.rank_policy_id != UNIFORM_POLICY_ID:
                raise ValueError("native v2 production policy must be uniform")
            if self.profile_schedule_id != expected_schedule:
                raise ValueError("native v2 profile schedule does not match its pool")
            if self.selfplay_scope != expected_scope:
                raise ValueError("native v2 self-play scope does not match its pool")
        elif self.backend_kind == "reference":
            if len(self.profile_pool) != 1:
                raise ValueError("reference backend supports one profile only")
            if self.generator_contract != FULLGAME_GENERATOR_CONTRACT_V1:
                raise ValueError("reference full-game contract must be v1")
            if self.rank_policy_id != RANK_POLICY_ID:
                raise ValueError("reference rank policy is unsupported")
            if self.profile_schedule_id != PROFILE_SCHEDULE_V1:
                raise ValueError("reference profile schedule is unsupported")
            if self.selfplay_scope != SELFPLAY_SCOPE_V1:
                raise ValueError("reference self-play scope is unsupported")
        else:
            raise ValueError("backend_kind must be native or reference")

    @classmethod
    def from_profile(
        cls,
        profile: EngineProfile | None = None,
        *,
        seed: int = 20260820,
        max_attempt_series: int = 0,
        max_frontier_states: int = DEFAULT_FRONTIER_STATES,
        max_positions_per_series: int = DEFAULT_POSITIONS_PER_SERIES,
        max_positions_per_game: int = DEFAULT_POSITIONS_PER_GAME,
        candidate_count: int = DEFAULT_CANDIDATE_COUNT,
        backend_kind: str = "native",
    ) -> "FullGameSemanticConfig":
        return cls.from_profiles(
            (profile or baseline_profile(),),
            seed=seed,
            max_attempt_series=max_attempt_series,
            max_frontier_states=max_frontier_states,
            max_positions_per_series=max_positions_per_series,
            max_positions_per_game=max_positions_per_game,
            candidate_count=candidate_count,
            backend_kind=backend_kind,
        )

    @classmethod
    def from_profiles(
        cls,
        profiles: Sequence[EngineProfile],
        *,
        seed: int = 20260820,
        max_attempt_series: int = 0,
        max_frontier_states: int = DEFAULT_FRONTIER_STATES,
        max_positions_per_series: int = DEFAULT_POSITIONS_PER_SERIES,
        max_positions_per_game: int = DEFAULT_POSITIONS_PER_GAME,
        candidate_count: int = DEFAULT_CANDIDATE_COUNT,
        backend_kind: str = "native",
    ) -> "FullGameSemanticConfig":
        if type(profiles) not in {tuple, list} or not profiles:
            raise ValueError("full-game profile pool must be a nonempty sequence")
        pool = tuple(FullGameProfileConfig.from_engine_profile(profile) for profile in profiles)
        if backend_kind == "native":
            generator_contract = FULLGAME_GENERATOR_CONTRACT_V2
            rank_policy_id = UNIFORM_POLICY_ID
            schedule = (
                PROFILE_SCHEDULE_SELF_V2
                if len(pool) == 1
                else PROFILE_SCHEDULE_ORDERED_V2
            )
            scope = (
                SELFPLAY_SCOPE_SINGLE_V2
                if len(pool) == 1
                else SELFPLAY_SCOPE_POOL_V2
            )
        elif backend_kind == "reference":
            generator_contract = FULLGAME_GENERATOR_CONTRACT_V1
            rank_policy_id = RANK_POLICY_ID
            schedule = PROFILE_SCHEDULE_V1
            scope = SELFPLAY_SCOPE_V1
        else:
            raise ValueError("backend_kind must be native or reference")
        first = pool[0]
        return cls(
            seed=seed,
            profile_id=first.profile_id,
            material=first.material,
            king_space=first.king_space,
            series_reach=first.series_reach,
            promotion_corridors=first.promotion_corridors,
            immediate_vulnerability=first.immediate_vulnerability,
            useful_mobility=first.useful_mobility,
            boundary_check=first.boundary_check,
            profile_pool=pool,
            max_attempt_series=max_attempt_series,
            max_frontier_states=max_frontier_states,
            max_positions_per_series=max_positions_per_series,
            max_positions_per_game=max_positions_per_game,
            candidate_count=candidate_count,
            backend_kind=backend_kind,
            backend_source_identity=_expected_backend_source_identity(backend_kind),
            generator_contract=generator_contract,
            rank_policy_id=rank_policy_id,
            profile_schedule_id=schedule,
            selfplay_scope=scope,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FullGameSemanticConfig":
        expected_keys = {
            "backend_kind",
            "backend_source_identity",
            "candidate_count",
            "data_purpose",
            "engine_version",
            "generator_contract",
            "max_attempt_series",
            "max_frontier_states",
            "max_positions_per_game",
            "max_positions_per_series",
            "native_semantic_digest",
            "policy",
            "profile_schedule_id",
            "profiles",
            "ruleset_version",
            "seed",
            "selfplay_scope",
            "source_fingerprint",
            "strength_claim",
        }
        if type(payload) is not dict or set(payload) != expected_keys:
            raise ValueError("full-game semantic config keys are invalid")
        profiles_payload = payload["profiles"]
        if type(profiles_payload) is not list or not profiles_payload:
            raise ValueError("full-game semantic profile pool is invalid")
        pool = tuple(FullGameProfileConfig.from_dict(item) for item in profiles_payload)
        policy = payload["policy"]
        if type(policy) is not dict or set(policy) != {
            "policy_id",
            "preserve_returned_mate",
        }:
            raise ValueError("full-game semantic policy is invalid")
        first = pool[0]
        config = cls(
            seed=payload["seed"],
            profile_id=first.profile_id,
            material=first.material,
            king_space=first.king_space,
            series_reach=first.series_reach,
            promotion_corridors=first.promotion_corridors,
            immediate_vulnerability=first.immediate_vulnerability,
            useful_mobility=first.useful_mobility,
            boundary_check=first.boundary_check,
            profile_pool=pool,
            max_attempt_series=payload["max_attempt_series"],
            max_frontier_states=payload["max_frontier_states"],
            max_positions_per_series=payload["max_positions_per_series"],
            max_positions_per_game=payload["max_positions_per_game"],
            candidate_count=payload["candidate_count"],
            preserve_returned_mate=policy["preserve_returned_mate"],
            backend_kind=payload["backend_kind"],
            backend_source_identity=payload["backend_source_identity"],
            engine_version=payload["engine_version"],
            source_fingerprint=payload["source_fingerprint"],
            ruleset_version=payload["ruleset_version"],
            generator_contract=payload["generator_contract"],
            rank_policy_id=policy["policy_id"],
            profile_schedule_id=payload["profile_schedule_id"],
            selfplay_scope=payload["selfplay_scope"],
            data_purpose=payload["data_purpose"],
            strength_claim=payload["strength_claim"],
        )
        expected_native_digest = (
            config.semantic_config_digest.hex()
            if config.backend_kind == "native"
            else None
        )
        if payload["native_semantic_digest"] != expected_native_digest:
            raise ValueError("native semantic digest does not match its config")
        if config.as_dict() != payload:
            raise ValueError("full-game semantic config is not canonical")
        return config

    @property
    def weights(self) -> EvaluationWeights:
        return self.profile_pool[0].weights

    @property
    def profile(self) -> EngineProfile:
        return EngineProfile(name="full-game rollout profile", weights=self.weights)

    @property
    def native_policy_kind(self) -> int:
        if self.rank_policy_id == UNIFORM_POLICY_ID:
            return NATIVE_V2_POLICY_UNIFORM
        raise ValueError("semantic policy has no native v2 production mapping")

    @property
    def native_schedule_kind(self) -> int:
        if self.profile_schedule_id == PROFILE_SCHEDULE_SELF_V2:
            return NATIVE_V2_SCHEDULE_SELF_ROUND_ROBIN
        if self.profile_schedule_id == PROFILE_SCHEDULE_ORDERED_V2:
            return NATIVE_V2_SCHEDULE_ORDERED_PAIR_ROUND_ROBIN
        raise ValueError("semantic schedule has no native v2 production mapping")

    def profile_pair(self, attempt_index: int) -> tuple[int, int]:
        return expected_v2_profile_pair(
            attempt_index, len(self.profile_pool), self.native_schedule_kind
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend_kind": self.backend_kind,
            "backend_source_identity": self.backend_source_identity,
            "candidate_count": self.candidate_count,
            "data_purpose": self.data_purpose,
            "engine_version": self.engine_version,
            "generator_contract": self.generator_contract,
            "max_attempt_series": self.max_attempt_series,
            "max_frontier_states": self.max_frontier_states,
            "max_positions_per_game": self.max_positions_per_game,
            "max_positions_per_series": self.max_positions_per_series,
            "native_semantic_digest": (
                self.semantic_config_digest.hex()
                if self.backend_kind == "native"
                else None
            ),
            "policy": {
                "policy_id": self.rank_policy_id,
                "preserve_returned_mate": self.preserve_returned_mate,
            },
            "profile_schedule_id": self.profile_schedule_id,
            "profiles": [profile.as_dict() for profile in self.profile_pool],
            "ruleset_version": self.ruleset_version,
            "seed": self.seed,
            "selfplay_scope": self.selfplay_scope,
            "source_fingerprint": self.source_fingerprint,
            "strength_claim": self.strength_claim,
        }

    @property
    def semantic_config_digest(self) -> bytes:
        if self.backend_kind != "native":
            raise ValueError("native semantic digest is unavailable for reference runs")
        request = encode_native_v2_request(
            first_attempt=0,
            attempt_count=1,
            seed=self.seed,
            max_attempt_series=self.max_attempt_series,
            max_frontier_states=self.max_frontier_states,
            max_positions_per_series=self.max_positions_per_series,
            max_positions_per_game=self.max_positions_per_game,
            candidate_count=self.candidate_count,
            profiles=tuple(
                NativeV2Profile(
                    profile.profile_digest_bytes,
                    *profile.native_weights,
                )
                for profile in self.profile_pool
            ),
            policy_kind=self.native_policy_kind,
            schedule_kind=self.native_schedule_kind,
            preserve_returned_mate=True,
        )
        digest = request[
            NATIVE_V2_CONFIG_DIGEST_OFFSET:NATIVE_V2_CONFIG_DIGEST_END
        ]
        if digest != native_v2_semantic_digest(request):
            raise ValueError("native v2 semantic digest derivation failed")
        return digest

    @property
    def simulation_id(self) -> str:
        digest = hashlib.sha256(_canonical_json(self.as_dict())).hexdigest()
        return f"spc-fullgame-{digest}"


def game_id(config: FullGameSemanticConfig, attempt_index: int) -> str:
    if type(attempt_index) is not int or not 0 <= attempt_index <= MAX_U64:
        raise ValueError("attempt_index must fit uint64")
    payload = (
        f"spc-fullgame-id-v1|{config.simulation_id}|{attempt_index}"
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _disposition_sha256(
    accepted: Iterable[tuple[FullGameRecord, str, str]],
    rejected: Iterable[tuple[int, str, str, int, int, int, int]],
) -> str:
    """Bind every persisted attempt envelope to one canonical chunk digest."""

    entries: list[tuple[int, int, int, int, int, int, int, str, str]] = []
    for record, trace_digest, item_game_id in accepted:
        if type(record) is not FullGameRecord:
            raise ValueError("accepted disposition record is invalid")
        if (
            type(trace_digest) is not str
            or not SHA256_PATTERN.fullmatch(trace_digest)
            or trace_digest != trace_sha256(record)
            or type(item_game_id) is not str
            or not SHA256_PATTERN.fullmatch(item_game_id)
        ):
            raise ValueError("accepted disposition identity is invalid")
        entries.append(
            (
                record.attempt_index,
                0,
                int(record.terminal),
                record.logical_work,
                record.white_profile_index,
                record.black_profile_index,
                record.path_count_saturations,
                item_game_id,
                trace_digest,
            )
        )

    for (
        attempt,
        item_game_id,
        reason,
        logical_work,
        white_profile_index,
        black_profile_index,
        path_count_saturations,
    ) in rejected:
        if (
            type(attempt) is not int
            or not 0 <= attempt <= MAX_U64
            or type(item_game_id) is not str
            or not SHA256_PATTERN.fullmatch(item_game_id)
            or type(reason) is not str
        ):
            raise ValueError("rejected disposition identity is invalid")
        try:
            reason_code = RejectReason[reason.upper()]
        except KeyError as error:
            raise ValueError("rejected disposition reason is invalid") from error
        if (
            reason != reason_code.name.lower()
            or reason_code in {RejectReason.NONE, RejectReason.CANCELLED}
            or type(logical_work) is not int
            or not 0 <= logical_work <= MAX_U64
            or type(white_profile_index) is not int
            or not 0 <= white_profile_index <= MAX_U32
            or type(black_profile_index) is not int
            or not 0 <= black_profile_index <= MAX_U32
            or type(path_count_saturations) is not int
            or not 0 <= path_count_saturations <= MAX_U64
        ):
            raise ValueError("rejected disposition envelope is invalid")
        entries.append(
            (
                attempt,
                1,
                int(reason_code),
                logical_work,
                white_profile_index,
                black_profile_index,
                path_count_saturations,
                item_game_id,
                "0" * 64,
            )
        )

    entries.sort(key=lambda item: item[0])
    if any(
        entry[0] == entries[index - 1][0]
        for index, entry in enumerate(entries)
        if index
    ):
        raise ValueError("disposition attempts must be unique")
    digest = hashlib.sha256()
    digest.update(DISPOSITION_DIGEST_DOMAIN)
    digest.update(struct.pack("<Q", len(entries)))
    for (
        attempt,
        kind,
        result_code,
        logical_work,
        white_profile_index,
        black_profile_index,
        path_count_saturations,
        item_game_id,
        trace_digest,
    ) in entries:
        digest.update(
            DISPOSITION_DIGEST_PREFIX.pack(
                attempt,
                kind,
                result_code,
                logical_work,
                white_profile_index,
                black_profile_index,
                path_count_saturations,
            )
        )
        digest.update(bytes.fromhex(item_game_id))
        digest.update(bytes.fromhex(trace_digest))
    return digest.hexdigest()


def _terminal_score(
    candidate: SeriesResult,
    mover: chess.Color,
) -> int | None:
    if candidate.outcome == Outcome.CHECKMATE:
        winner = mover if candidate.ended_by_check else not mover
        return (
            FULLGAME_TERMINAL_SCORE - 1
            if winner == chess.WHITE
            else -FULLGAME_TERMINAL_SCORE + 1
        )
    if candidate.outcome in {Outcome.STALEMATE, Outcome.TEN_SERIES_DRAW}:
        return 0
    return None


def _rank_candidates(
    state: ProgressiveState,
    candidates: Sequence[SeriesResult],
    config: FullGameSemanticConfig,
) -> tuple[SeriesResult, ...]:
    mover = state.board.turn
    scored = []
    for candidate in candidates:
        terminal = _terminal_score(candidate, mover)
        score = (
            terminal
            if terminal is not None
            else fast_evaluate(candidate.final_state, config.weights)
        )
        scored.append((candidate, score))
    scored.sort(
        key=lambda item: (
            -item[1] if mover == chess.WHITE else item[1],
            item[0].moves,
        )
    )
    return tuple(item[0] for item in scored[: config.candidate_count])


def choose_ranked_candidate(
    candidates: Sequence[SeriesResult],
    *,
    mover: chess.Color,
    seed: int,
    attempt_index: int,
    series_number: int,
) -> SeriesResult:
    if not candidates:
        raise ValueError("cannot choose from an empty candidate list")
    for candidate in candidates:
        if candidate.outcome != Outcome.CHECKMATE:
            continue
        winner = mover if candidate.ended_by_check else not mover
        if winner == mover:
            return candidate

    count = len(candidates)
    bucket = decision_random(seed, attempt_index, series_number, 0) % 100
    if bucket < 80 or count == 1:
        index = 0
    elif bucket < 95:
        width = min(3, count - 1)
        index = 1 + decision_random(
            seed, attempt_index, series_number, 1
        ) % width
    elif count >= 5:
        index = 4 + decision_random(
            seed, attempt_index, series_number, 2
        ) % (count - 4)
    else:
        index = count - 1
    return candidates[index]


def _terminal_for_series(
    result: SeriesResult,
    mover: chess.Color,
) -> Terminal | None:
    if result.outcome == Outcome.CHECKMATE:
        winner = mover if result.ended_by_check else not mover
        return (
            Terminal.CHECKMATE_WHITE
            if winner == chess.WHITE
            else Terminal.CHECKMATE_BLACK
        )
    if result.outcome == Outcome.STALEMATE:
        return Terminal.STALEMATE
    if result.outcome == Outcome.TEN_SERIES_DRAW:
        return Terminal.TEN_SERIES_DRAW
    return None


def generate_reference_attempt(
    config: FullGameSemanticConfig,
    attempt_index: int,
) -> FullGameRecord | RejectedAttempt:
    """Slow oracle for the frozen native fast-rollout policy.

    This always starts from the exact initial S1 state. A deterministic work
    ceiling can exclude an attempt, but it can never manufacture a result.
    """

    if type(attempt_index) is not int or not 0 <= attempt_index <= MAX_U64:
        raise ValueError("attempt_index must fit uint64")
    state = ProgressiveState.initial()
    played: list[tuple[str, ...]] = []
    game_work = 0
    profile = config.profile

    def reject(reason: RejectReason) -> RejectedAttempt:
        return RejectedAttempt(attempt_index, reason, game_work)

    while True:
        if (
            config.max_attempt_series
            and state.series_number > config.max_attempt_series
        ):
            return reject(RejectReason.TECHNICAL_SERIES_WATCHDOG)
        if state.quiet_draw_pending:
            if state.board.is_insufficient_material():
                # A preceding authoritative series should already have carried
                # TEN_SERIES_DRAW. Refuse to invent a zero-series terminal if
                # malformed state ever reaches this branch.
                return reject(RejectReason.INTERNAL_ERROR)
            # Conservative by contract: this also excludes positions where an
            # immediate mating-series exception may exist. It is never a draw.
            return reject(RejectReason.MANUAL_PROOF_REQUIRED)

        remaining_game = config.max_positions_per_game - game_work
        if remaining_game <= 0:
            return reject(RejectReason.WORK_LIMIT)
        search_work = min(config.max_positions_per_series, remaining_game)
        stats = GenerationStats()
        try:
            generated = generate_series(
                state,
                stats=stats,
                max_frontier_states=config.max_frontier_states,
                max_positions=search_work,
                frontier_score=NativeFrontierScoreConfig.from_profile(
                    state,
                    profile,
                ),
            )
        except GenerationWorkLimit:
            game_work += stats.positions_visited + stats.frontier_score_positions
            return reject(RejectReason.WORK_LIMIT)
        game_work += stats.positions_visited + stats.frontier_score_positions
        if game_work > config.max_positions_per_game:
            return reject(RejectReason.WORK_LIMIT)
        if not generated:
            return reject(RejectReason.INTERNAL_ERROR)

        ranked = _rank_candidates(state, generated, config)
        if not ranked:
            return reject(RejectReason.INTERNAL_ERROR)
        mover = state.board.turn
        selected = choose_ranked_candidate(
            ranked,
            mover=mover,
            seed=config.seed,
            attempt_index=attempt_index,
            series_number=state.series_number,
        )
        played.append(selected.moves)
        terminal = _terminal_for_series(selected, mover)
        state = selected.final_state
        if terminal is not None:
            return FullGameRecord(attempt_index, terminal, tuple(played), game_work)


def generate_reference_batch(
    config: FullGameSemanticConfig,
    first_attempt: int,
    attempt_count: int,
) -> tuple[FullGameRecord | RejectedAttempt, ...]:
    if type(first_attempt) is not int or not 0 <= first_attempt <= MAX_U64:
        raise ValueError("first_attempt must fit uint64")
    if type(attempt_count) is not int or not 1 <= attempt_count <= MAX_U32:
        raise ValueError("attempt_count must fit positive uint32")
    if first_attempt + attempt_count > MAX_U64 + 1:
        raise ValueError("attempt range overflows uint64")
    return tuple(
        generate_reference_attempt(config, attempt)
        for attempt in range(first_attempt, first_attempt + attempt_count)
    )


def generate_native_batch(
    config: FullGameSemanticConfig,
    first_attempt: int,
    attempt_count: int,
) -> tuple[FullGameRecord | RejectedAttempt, ...]:
    from . import evaluation

    if type(first_attempt) is not int or not 0 <= first_attempt <= MAX_U64:
        raise ValueError("first_attempt must fit uint64")
    if type(attempt_count) is not int or not 1 <= attempt_count <= MAX_U32:
        raise ValueError("attempt_count must fit positive uint32")
    if first_attempt + attempt_count > MAX_U64 + 1:
        raise ValueError("attempt range overflows uint64")
    if config.backend_kind != "native":
        raise ValueError("native generation requires a native semantic config")
    native = evaluation._native_eval
    if native is None or not hasattr(native, "generate_full_game_batch"):
        raise ValueError(
            "source-matched native full-game generation is unavailable; "
            "use --backend reference only for bounded correctness work"
        )
    if getattr(native, "SOURCE_IDENTITY", None) != config.backend_source_identity:
        raise ValueError("loaded native full-game source identity has drifted")
    payload = native.generate_full_game_batch(
        first_attempt,
        attempt_count,
        config.seed,
        config.max_attempt_series,
        config.max_frontier_states,
        config.max_positions_per_series,
        config.max_positions_per_game,
        config.candidate_count,
        config.material,
        config.king_space,
        config.promotion_corridors,
        config.immediate_vulnerability,
        config.boundary_check,
    )
    if not isinstance(payload, bytes):
        raise ValueError("native full-game generator returned a non-bytes payload")
    decoded = decode_native_batch(payload)
    if (
        decoded.first_attempt != first_attempt
        or decoded.attempt_count != attempt_count
    ):
        raise ValueError("native full-game batch range does not match its request")
    return decoded.records


def generate_native_batch_v2(
    config: FullGameSemanticConfig,
    first_attempt: int,
    attempt_count: int,
) -> tuple[FullGameRecord | RejectedAttempt, ...]:
    """Checked production wrapper around the opaque native v2 byte boundary."""

    from . import evaluation

    if config.backend_kind != "native":
        raise ValueError("native v2 generation requires a native semantic config")
    _validate_backend_binding(config, "native")
    # Reconstructing from canonical data catches any future config class drift
    # before its digest or profile bindings cross the native boundary.
    canonical_config = FullGameSemanticConfig.from_dict(config.as_dict())
    if canonical_config != config:
        raise ValueError("native v2 semantic config is not canonical")
    native_profiles = tuple(
        NativeV2Profile(
            profile.profile_digest_bytes,
            *profile.native_weights,
        )
        for profile in config.profile_pool
    )
    # Raw native digest bytes are opaque tags. Bind every one here to the exact
    # five-weight vector the v2 kernel actually consumes.
    for profile, native_profile in zip(config.profile_pool, native_profiles):
        if native_profile.digest != native_v2_profile_digest(
            profile.native_weights
        ):
            raise ValueError("native v2 profile digest recomputation failed")

    config_digest = config.semantic_config_digest
    request = encode_native_v2_request(
        first_attempt=first_attempt,
        attempt_count=attempt_count,
        seed=config.seed,
        max_attempt_series=config.max_attempt_series,
        max_frontier_states=config.max_frontier_states,
        max_positions_per_series=config.max_positions_per_series,
        max_positions_per_game=config.max_positions_per_game,
        candidate_count=config.candidate_count,
        profiles=native_profiles,
        policy_kind=config.native_policy_kind,
        schedule_kind=config.native_schedule_kind,
        config_digest=config_digest,
        preserve_returned_mate=True,
    )
    if native_v2_semantic_digest(request) != config_digest or request[
        NATIVE_V2_CONFIG_DIGEST_OFFSET:NATIVE_V2_CONFIG_DIGEST_END
    ] != config_digest:
        raise ValueError("native v2 semantic config digest recomputation failed")
    native = evaluation._native_eval
    assert native is not None
    payload = native.generate_full_game_batch_v2(request)
    if type(payload) is not bytes:
        raise ValueError("native v2 full-game generator returned a non-bytes payload")
    decoded = decode_native_batch_v2(
        payload,
        expected_first_attempt=first_attempt,
        expected_attempt_count=attempt_count,
        expected_config_digest=config_digest,
        expected_profile_count=len(native_profiles),
        expected_policy_kind=config.native_policy_kind,
        expected_schedule_kind=config.native_schedule_kind,
    )
    for record in decoded.records:
        if (
            record.white_profile_index,
            record.black_profile_index,
        ) != config.profile_pair(record.attempt_index):
            raise ValueError("native v2 record profile attribution is invalid")
    return decoded.records


class FullGameStore:
    """Single-writer checkpoint and compact accepted-trace store.

    SQLite commits semantic attempt/dedup state. A prepared row bridges the
    separate filesystem rename; startup deterministically finishes or rolls
    back that one bounded partial state before any new work is scheduled.
    """

    def __init__(
        self,
        root: str | Path,
        config: FullGameSemanticConfig,
        *,
        target_unique_games: int,
        attempts_per_chunk: int,
        backend: str,
    ) -> None:
        if type(target_unique_games) is not int or target_unique_games < 1:
            raise ValueError("target_unique_games must be positive")
        if type(attempts_per_chunk) is not int or not 1 <= attempts_per_chunk <= MAX_U32:
            raise ValueError("attempts_per_chunk must fit positive uint32")
        if backend not in {"native", "reference"}:
            raise ValueError("backend must be native or reference")
        _validate_backend_binding(config, backend)
        self.root = Path(root).expanduser().resolve()
        self.chunks_dir = self.root / "chunks"
        self.root.mkdir(parents=True, exist_ok=True)
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self.checkpoint_path = self.root / "checkpoint.sqlite3"
        self.writer_lock_path = self.root / ".writer.lock"
        self.config = config
        self.target_unique_games = target_unique_games
        self.attempts_per_chunk = attempts_per_chunk
        self.backend = backend
        self._writer_lock = _acquire_writer_lock(self.writer_lock_path)
        try:
            self.connection = sqlite3.connect(self.checkpoint_path, timeout=30)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self._create_schema()
            self._initialize_or_validate()
            self._reconcile_prepared()
            self._write_manifest()
        except BaseException:
            connection = getattr(self, "connection", None)
            if connection is not None:
                connection.close()
            _release_writer_lock(self._writer_lock)
            raise

    def __enter__(self) -> "FullGameStore":
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self.connection.close()
        finally:
            _release_writer_lock(self._writer_lock)

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_index INTEGER PRIMARY KEY,
                attempt_start TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('prepared','committed')),
                filename TEXT NOT NULL UNIQUE,
                pending_filename TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                disposition_sha256 TEXT NOT NULL,
                accepted_count INTEGER NOT NULL,
                duplicate_count INTEGER NOT NULL,
                rejected_count INTEGER NOT NULL,
                terminal_json TEXT NOT NULL,
                series_count INTEGER NOT NULL,
                move_count INTEGER NOT NULL,
                logical_work INTEGER NOT NULL,
                path_saturations TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seen_traces (
                trace_sha256 TEXT PRIMARY KEY,
                attempt_index TEXT NOT NULL UNIQUE,
                game_id TEXT NOT NULL UNIQUE,
                white_profile_index INTEGER NOT NULL,
                black_profile_index INTEGER NOT NULL,
                path_saturations TEXT NOT NULL,
                chunk_index INTEGER NOT NULL REFERENCES chunks(chunk_index)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS rejected_attempts (
                attempt_index TEXT PRIMARY KEY,
                game_id TEXT NOT NULL UNIQUE,
                reason TEXT NOT NULL,
                logical_work INTEGER NOT NULL,
                white_profile_index INTEGER NOT NULL,
                black_profile_index INTEGER NOT NULL,
                path_saturations TEXT NOT NULL,
                chunk_index INTEGER NOT NULL REFERENCES chunks(chunk_index)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS run_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                next_attempt TEXT NOT NULL,
                accepted_count INTEGER NOT NULL,
                duplicate_count INTEGER NOT NULL,
                rejected_count INTEGER NOT NULL,
                series_count INTEGER NOT NULL,
                move_count INTEGER NOT NULL,
                logical_work INTEGER NOT NULL,
                path_saturations TEXT NOT NULL,
                terminal_json TEXT NOT NULL,
                rejection_json TEXT NOT NULL,
                chunks_committed INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO run_state(
                singleton,next_attempt,accepted_count,duplicate_count,
                rejected_count,series_count,move_count,logical_work,
                path_saturations,terminal_json,rejection_json,chunks_committed
            ) VALUES(1,'0',0,0,0,0,0,0,'0','{}','{}',0);
            """
        )
        self.connection.commit()

    def _metadata(self) -> dict[str, str]:
        return {
            str(row["key"]): str(row["value"])
            for row in self.connection.execute("SELECT key,value FROM metadata")
        }

    def _initialize_or_validate(self) -> None:
        expected = {
            "checkpoint_schema": str(FULLGAME_CHECKPOINT_SCHEMA),
            "semantic_config": _canonical_json(self.config.as_dict()).decode("ascii"),
            "simulation_id": self.config.simulation_id,
        }
        existing = self._metadata()
        if not existing:
            with self.connection:
                self.connection.executemany(
                    "INSERT INTO metadata(key,value) VALUES(?,?)",
                    sorted(expected.items()),
                )
            return
        if existing != expected:
            mismatched = sorted(
                key
                for key in set(existing) | set(expected)
                if existing.get(key) != expected.get(key)
            )
            raise ValueError(
                "full-game checkpoint configuration mismatch: "
                + ", ".join(mismatched)
            )

    def _safe_chunk_path(self, filename: str) -> Path:
        if (
            type(filename) is not str
            or Path(filename).name != filename
            or not CHUNK_FILENAME_PATTERN.fullmatch(filename)
        ):
            raise ValueError("checkpoint chunk filename is invalid")
        path = (self.chunks_dir / filename).resolve()
        if path.parent != self.chunks_dir.resolve():
            raise ValueError("checkpoint chunk path escapes its directory")
        return path

    def _safe_pending_path(self, filename: str) -> Path:
        if (
            type(filename) is not str
            or Path(filename).name != filename
            or not PENDING_FILENAME_PATTERN.fullmatch(filename)
        ):
            raise ValueError("checkpoint pending filename is invalid")
        path = (self.chunks_dir / filename).resolve()
        if path.parent != self.chunks_dir.resolve():
            raise ValueError("checkpoint pending path escapes its directory")
        return path

    def _validate_chunk_file(
        self,
        row: Mapping[str, Any],
        path: Path,
    ) -> DecodedChunk:
        if not path.is_file():
            raise ValueError(f"checkpoint full-game chunk is missing: {path.name}")
        payload = path.read_bytes()
        if chunk_sha256(payload) != str(row["sha256"]):
            raise ValueError(f"checkpoint full-game chunk hash mismatch: {path.name}")
        decoded = decode_chunk(payload)
        if decoded.header["simulation_id"] != self.config.simulation_id:
            raise ValueError("checkpoint full-game chunk belongs to another simulation")
        if int(decoded.header["first_attempt"]) != int(row["attempt_start"]):
            raise ValueError("prepared full-game chunk start does not match checkpoint")
        if int(decoded.header["attempt_count"]) != int(row["attempt_count"]):
            raise ValueError("prepared full-game chunk count does not match checkpoint")
        if len(decoded.records) != int(row["accepted_count"]):
            raise ValueError("prepared full-game accepted count does not match checkpoint")
        return decoded

    def _validate_checkpoint_indexes(
        self,
        row: Mapping[str, Any],
        decoded: DecodedChunk,
    ) -> None:
        chunk_index = int(row["chunk_index"])
        seen_rows = self.connection.execute(
            "SELECT trace_sha256,attempt_index,game_id,white_profile_index,"
            "black_profile_index,path_saturations FROM seen_traces "
            "WHERE chunk_index=?",
            (chunk_index,),
        ).fetchall()
        seen_by_attempt = {int(item["attempt_index"]): item for item in seen_rows}
        if len(seen_by_attempt) != len(decoded.records):
            raise ValueError("checkpoint accepted index diverges from committed chunk")

        accepted_attempts: list[int] = []
        accepted_envelopes: list[tuple[FullGameRecord, str, str]] = []
        terminal_counts: dict[str, int] = {}
        series_count = move_count = logical_work = path_saturations = 0
        for record in decoded.records:
            seen = seen_by_attempt.get(record.attempt_index)
            expected_pair = (
                self.config.profile_pair(record.attempt_index)
                if self.config.backend_kind == "native"
                else (0, 0)
            )
            if (
                seen is None
                or str(seen["trace_sha256"]) != trace_sha256(record)
                or str(seen["game_id"])
                != game_id(self.config, record.attempt_index)
                or int(seen["white_profile_index"])
                != record.white_profile_index
                or int(seen["black_profile_index"])
                != record.black_profile_index
                or int(seen["path_saturations"])
                != record.path_count_saturations
                or (
                    record.white_profile_index,
                    record.black_profile_index,
                )
                != expected_pair
            ):
                raise ValueError(
                    "checkpoint accepted identity diverges from committed chunk"
                )
            accepted_attempts.append(record.attempt_index)
            accepted_envelopes.append(
                (record, str(seen["trace_sha256"]), str(seen["game_id"]))
            )
            terminal = record.terminal.name.lower()
            terminal_counts[terminal] = terminal_counts.get(terminal, 0) + 1
            series_count += len(record.series)
            move_count += record.move_count
            logical_work += record.logical_work
            path_saturations = min(
                MAX_U64,
                path_saturations + record.path_count_saturations,
            )

        rejected_rows = self.connection.execute(
            "SELECT attempt_index,game_id,reason,logical_work,"
            "white_profile_index,black_profile_index,path_saturations "
            "FROM rejected_attempts WHERE chunk_index=?",
            (chunk_index,),
        ).fetchall()
        rejected_attempts: list[int] = []
        rejected_envelopes: list[tuple[int, str, str, int, int, int, int]] = []
        duplicates = native_rejects = 0
        valid_reasons = {
            reason.name.lower()
            for reason in RejectReason
            if reason not in {RejectReason.NONE, RejectReason.CANCELLED}
        }
        for rejected in rejected_rows:
            attempt = int(rejected["attempt_index"])
            reason = str(rejected["reason"])
            work = int(rejected["logical_work"])
            white_profile = int(rejected["white_profile_index"])
            black_profile = int(rejected["black_profile_index"])
            record_saturations = int(rejected["path_saturations"])
            expected_pair = (
                self.config.profile_pair(attempt)
                if self.config.backend_kind == "native"
                else (0, 0)
            )
            if (
                reason not in valid_reasons
                or not 0 <= work <= MAX_U64
                or not 0 <= record_saturations <= MAX_U64
                or (white_profile, black_profile) != expected_pair
                or str(rejected["game_id"]) != game_id(self.config, attempt)
            ):
                raise ValueError("checkpoint rejected disposition is invalid")
            rejected_attempts.append(attempt)
            rejected_envelopes.append(
                (
                    attempt,
                    str(rejected["game_id"]),
                    reason,
                    work,
                    white_profile,
                    black_profile,
                    record_saturations,
                )
            )
            logical_work += work
            path_saturations = min(
                MAX_U64, path_saturations + record_saturations
            )
            if reason == RejectReason.DUPLICATE_TRACE.name.lower():
                duplicates += 1
            else:
                native_rejects += 1

        attempt_start = int(row["attempt_start"])
        attempt_count = int(row["attempt_count"])
        dispositions = sorted((*accepted_attempts, *rejected_attempts))
        if len(dispositions) != attempt_count or any(
            attempt != attempt_start + offset
            for offset, attempt in enumerate(dispositions)
        ):
            raise ValueError("checkpoint attempt disposition index is incomplete")
        try:
            stored_terminals = json.loads(str(row["terminal_json"]))
        except json.JSONDecodeError as error:
            raise ValueError("checkpoint terminal counters are invalid") from error
        if (
            len(decoded.records) != int(row["accepted_count"])
            or duplicates != int(row["duplicate_count"])
            or native_rejects != int(row["rejected_count"])
            or terminal_counts != stored_terminals
            or series_count != int(row["series_count"])
            or move_count != int(row["move_count"])
            or logical_work != int(row["logical_work"])
            or path_saturations != int(row["path_saturations"])
            or str(row["disposition_sha256"])
            != _disposition_sha256(accepted_envelopes, rejected_envelopes)
        ):
            raise ValueError("checkpoint disposition counters diverge from chunk")

    def _finalize_prepared_row(self, row: Mapping[str, Any]) -> None:
        final_path = self._safe_chunk_path(str(row["filename"]))
        pending_path = self._safe_pending_path(str(row["pending_filename"]))
        if final_path.is_file():
            decoded = self._validate_chunk_file(row, final_path)
            self._validate_checkpoint_indexes(row, decoded)
            if pending_path.is_file():
                pending_path.unlink()
        elif pending_path.is_file():
            decoded = self._validate_chunk_file(row, pending_path)
            self._validate_checkpoint_indexes(row, decoded)
            os.replace(pending_path, final_path)
            self._validate_chunk_file(row, final_path)
        else:
            with self.connection:
                self.connection.execute(
                    "DELETE FROM chunks WHERE chunk_index=?",
                    (int(row["chunk_index"]),),
                )
            return
        with self.connection:
            self.connection.execute(
                "UPDATE chunks SET state='committed' WHERE chunk_index=?",
                (int(row["chunk_index"]),),
            )

    def _rebuild_run_state(self) -> None:
        aggregates = self.connection.execute(
            """
            SELECT COUNT(*) AS chunks,
                   COALESCE(SUM(accepted_count),0) AS accepted,
                   COALESCE(SUM(duplicate_count),0) AS duplicates,
                   COALESCE(SUM(rejected_count),0) AS rejected,
                   COALESCE(SUM(series_count),0) AS series,
                   COALESCE(SUM(move_count),0) AS moves,
                   COALESCE(SUM(logical_work),0) AS logical_work
            FROM chunks WHERE state='committed'
            """
        ).fetchone()
        terminal_counts: dict[str, int] = {}
        for row in self.connection.execute(
            "SELECT terminal_json FROM chunks WHERE state='committed'"
        ):
            for terminal, count in json.loads(str(row["terminal_json"])).items():
                terminal_counts[terminal] = terminal_counts.get(terminal, 0) + int(count)
        rejection_counts = {
            str(row["reason"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT rejected_attempts.reason,COUNT(*) AS count "
                "FROM rejected_attempts JOIN chunks USING(chunk_index) "
                "WHERE chunks.state='committed' "
                "GROUP BY rejected_attempts.reason ORDER BY rejected_attempts.reason"
            )
        }
        path_saturations = 0
        for row in self.connection.execute(
            "SELECT path_saturations FROM chunks WHERE state='committed'"
        ):
            path_saturations = min(
                MAX_U64, path_saturations + int(row["path_saturations"])
            )
        with self.connection:
            self.connection.execute(
                """
                UPDATE run_state SET
                    next_attempt=?,accepted_count=?,duplicate_count=?,
                    rejected_count=?,series_count=?,move_count=?,logical_work=?,
                    path_saturations=?,terminal_json=?,rejection_json=?,
                    chunks_committed=?
                WHERE singleton=1
                """,
                (
                    str(self._contiguous_attempt_end()),
                    int(aggregates["accepted"]),
                    int(aggregates["duplicates"]),
                    int(aggregates["rejected"]),
                    int(aggregates["series"]),
                    int(aggregates["moves"]),
                    int(aggregates["logical_work"]),
                    str(path_saturations),
                    json.dumps(
                        terminal_counts, sort_keys=True, separators=(",", ":")
                    ),
                    json.dumps(
                        rejection_counts, sort_keys=True, separators=(",", ":")
                    ),
                    int(aggregates["chunks"]),
                ),
            )

    def _contiguous_attempt_end(self) -> int:
        row = self.connection.execute(
            "SELECT attempt_start,attempt_count FROM chunks "
            "WHERE state='committed' ORDER BY chunk_index DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0
        return int(row["attempt_start"]) + int(row["attempt_count"])

    def _apply_committed_chunk_to_state(self, row: Mapping[str, Any]) -> None:
        state = self.connection.execute(
            "SELECT * FROM run_state WHERE singleton=1"
        ).fetchone()
        assert state is not None
        attempt_start = int(row["attempt_start"])
        if int(state["next_attempt"]) != attempt_start:
            raise ValueError("run-state watermark does not match committed chunk")
        terminals = json.loads(str(state["terminal_json"]))
        for terminal, count in json.loads(str(row["terminal_json"])).items():
            terminals[terminal] = terminals.get(terminal, 0) + int(count)
        rejections = json.loads(str(state["rejection_json"]))
        for reason_row in self.connection.execute(
            "SELECT reason,COUNT(*) AS count FROM rejected_attempts "
            "WHERE chunk_index=? GROUP BY reason",
            (int(row["chunk_index"]),),
        ):
            reason = str(reason_row["reason"])
            rejections[reason] = rejections.get(reason, 0) + int(reason_row["count"])
        path_saturations = min(
            MAX_U64,
            int(state["path_saturations"]) + int(row["path_saturations"]),
        )
        self.connection.execute(
            """
            UPDATE run_state SET
                next_attempt=?,
                accepted_count=accepted_count+?,
                duplicate_count=duplicate_count+?,
                rejected_count=rejected_count+?,
                series_count=series_count+?,
                move_count=move_count+?,
                logical_work=logical_work+?,
                path_saturations=?,
                terminal_json=?,rejection_json=?,
                chunks_committed=chunks_committed+1
            WHERE singleton=1
            """,
            (
                str(attempt_start + int(row["attempt_count"])),
                int(row["accepted_count"]),
                int(row["duplicate_count"]),
                int(row["rejected_count"]),
                int(row["series_count"]),
                int(row["move_count"]),
                int(row["logical_work"]),
                str(path_saturations),
                json.dumps(terminals, sort_keys=True, separators=(",", ":")),
                json.dumps(rejections, sort_keys=True, separators=(",", ":")),
            ),
        )

    def _reconcile_prepared(self) -> None:
        prepared = self.connection.execute(
            "SELECT * FROM chunks WHERE state='prepared' ORDER BY chunk_index"
        ).fetchall()
        if len(prepared) > 1:
            raise ValueError("checkpoint contains multiple prepared chunks")
        for row in prepared:
            self._finalize_prepared_row(row)
        self._assert_contiguous()
        for row in self.connection.execute(
            "SELECT * FROM chunks WHERE state='committed' ORDER BY chunk_index"
        ):
            decoded = self._validate_chunk_file(
                row,
                self._safe_chunk_path(str(row["filename"])),
            )
            self._validate_checkpoint_indexes(row, decoded)
            pending_path = self._safe_pending_path(str(row["pending_filename"]))
            if pending_path.is_file():
                pending_path.unlink()
        referenced = {
            str(row["pending_filename"])
            for row in self.connection.execute("SELECT pending_filename FROM chunks")
        }
        for path in self.chunks_dir.iterdir():
            if path.is_file() and PENDING_FILENAME_PATTERN.fullmatch(path.name):
                if path.name not in referenced:
                    # A crash before the SQLite prepare transaction leaves only
                    # this bounded temp file. It was never committed and is safe
                    # to discard; accepted data exists only after the DB row.
                    path.unlink()
            elif path.is_file() and path.name.endswith(".spcg"):
                row = self.connection.execute(
                    "SELECT 1 FROM chunks WHERE filename=?", (path.name,)
                ).fetchone()
                if row is None:
                    raise ValueError(
                        f"unregistered full-game chunk requires inspection: {path.name}"
                    )
        totals = self.connection.execute(
            "SELECT COALESCE(SUM(accepted_count),0) AS accepted, "
            "COALESCE(SUM(duplicate_count+rejected_count),0) AS rejected "
            "FROM chunks WHERE state='committed'"
        ).fetchone()
        assert totals is not None
        seen_total = int(
            self.connection.execute("SELECT COUNT(*) FROM seen_traces").fetchone()[0]
        )
        rejected_total = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM rejected_attempts"
            ).fetchone()[0]
        )
        if (
            seen_total != int(totals["accepted"])
            or rejected_total != int(totals["rejected"])
        ):
            raise ValueError("checkpoint contains unowned attempt index rows")
        self._rebuild_run_state()

    def _assert_contiguous(self) -> None:
        expected_start = 0
        expected_index = 0
        for row in self.connection.execute(
            "SELECT * FROM chunks WHERE state='committed' ORDER BY chunk_index"
        ):
            if int(row["chunk_index"]) != expected_index:
                raise ValueError("committed chunk indexes are not contiguous")
            if int(row["attempt_start"]) != expected_start:
                raise ValueError("committed attempt ranges are not contiguous")
            expected_start += int(row["attempt_count"])
            expected_index += 1

    @property
    def next_attempt(self) -> int:
        row = self.connection.execute(
            "SELECT next_attempt FROM run_state WHERE singleton=1"
        ).fetchone()
        assert row is not None
        return int(row["next_attempt"])

    @property
    def accepted_count(self) -> int:
        row = self.connection.execute(
            "SELECT accepted_count FROM run_state WHERE singleton=1"
        ).fetchone()
        assert row is not None
        return int(row["accepted_count"])

    def summary(self) -> dict[str, Any]:
        state = self.connection.execute(
            "SELECT * FROM run_state WHERE singleton=1"
        ).fetchone()
        assert state is not None
        reasons = json.loads(str(state["rejection_json"]))
        terminal_counts = json.loads(str(state["terminal_json"]))
        accepted = int(state["accepted_count"])
        attempts = int(state["next_attempt"])
        return {
            "accepted_unique_games": accepted,
            "attempts_committed": attempts,
            "chunks_committed": int(state["chunks_committed"]),
            "duplicate_traces": int(state["duplicate_count"]),
            "data_purpose": self.config.data_purpose,
            "label_kind": LABEL_KIND,
            "logical_work": int(state["logical_work"]),
            "micro_moves": int(state["move_count"]),
            "native_or_policy_rejects": int(state["rejected_count"]),
            "next_attempt": attempts,
            "path_count_saturations": int(state["path_saturations"]),
            "policy_id": self.config.rank_policy_id,
            "profile_count": len(self.config.profile_pool),
            "profile_schedule_id": self.config.profile_schedule_id,
            "rejections_by_reason": reasons,
            "series": int(state["series_count"]),
            "simulation_id": self.config.simulation_id,
            "status": (
                "complete" if accepted >= self.target_unique_games else "running"
            ),
            "strength_claim": self.config.strength_claim,
            "target_unique_games": self.target_unique_games,
            "terminal_counts": dict(sorted(terminal_counts.items())),
        }

    def _manifest_payload(self) -> dict[str, Any]:
        progress = self.summary()
        return {
            "backend": self.backend,
            "checkpoint_schema": FULLGAME_CHECKPOINT_SCHEMA,
            "chunk_catalog": {
                "checkpoint": self.checkpoint_path.name,
                "committed_chunks": progress["chunks_committed"],
            },
            "execution": {
                "attempts_per_chunk": self.attempts_per_chunk,
                "target_unique_games": self.target_unique_games,
            },
            "format": FULLGAME_RUN_FORMAT,
            "progress": progress,
            "semantic_config": self.config.as_dict(),
            "simulation_id": self.config.simulation_id,
        }

    def _write_manifest(self) -> None:
        _atomic_json(self.manifest_path, self._manifest_payload())

    def commit_outcomes(
        self,
        outcomes: Sequence[FullGameRecord | RejectedAttempt],
    ) -> dict[str, Any]:
        if not outcomes:
            raise ValueError("cannot commit an empty attempt batch")
        expected_start = self.next_attempt
        ordered = tuple(outcomes)
        for offset, item in enumerate(ordered):
            if type(item) not in {FullGameRecord, RejectedAttempt}:
                raise ValueError("attempt outcome type is invalid")
            if item.attempt_index != expected_start + offset:
                raise ValueError("attempt batch must be one contiguous ordered range")
            expected_pair = (
                self.config.profile_pair(item.attempt_index)
                if self.config.backend_kind == "native"
                else (0, 0)
            )
            if (
                item.white_profile_index,
                item.black_profile_index,
            ) != expected_pair:
                raise ValueError("attempt profile attribution violates its schedule")

        remaining = self.target_unique_games - self.accepted_count
        if remaining <= 0:
            return self.summary()
        local_hashes: set[str] = set()
        accepted: list[tuple[FullGameRecord, str, str]] = []
        rejected: list[tuple[int, str, str, int, int, int, int]] = []
        duplicates = 0
        committed_attempts = 0
        terminal_counts: dict[str, int] = {}
        series_count = move_count = 0
        logical_work = path_saturations = 0

        for outcome in ordered:
            committed_attempts += 1
            logical_work += outcome.logical_work
            path_saturations = min(
                MAX_U64,
                path_saturations + outcome.path_count_saturations,
            )
            item_game_id = game_id(self.config, outcome.attempt_index)
            if isinstance(outcome, RejectedAttempt):
                rejected.append(
                    (
                        outcome.attempt_index,
                        item_game_id,
                        outcome.reason.name.lower(),
                        outcome.logical_work,
                        outcome.white_profile_index,
                        outcome.black_profile_index,
                        outcome.path_count_saturations,
                    )
                )
            else:
                replay_record(outcome)
                digest = trace_sha256(outcome)
                already_seen = self.connection.execute(
                    "SELECT 1 FROM seen_traces WHERE trace_sha256=?",
                    (digest,),
                ).fetchone()
                if already_seen is not None or digest in local_hashes:
                    duplicates += 1
                    rejected.append(
                        (
                            outcome.attempt_index,
                            item_game_id,
                            RejectReason.DUPLICATE_TRACE.name.lower(),
                            outcome.logical_work,
                            outcome.white_profile_index,
                            outcome.black_profile_index,
                            outcome.path_count_saturations,
                        )
                    )
                else:
                    local_hashes.add(digest)
                    accepted.append((outcome, digest, item_game_id))
                    terminal_counts[outcome.terminal.name.lower()] = (
                        terminal_counts.get(outcome.terminal.name.lower(), 0) + 1
                    )
                    series_count += len(outcome.series)
                    move_count += outcome.move_count
                    if len(accepted) >= remaining:
                        break

        committed = ordered[:committed_attempts]
        attempt_start = committed[0].attempt_index
        attempt_count = len(committed)
        chunk_index_row = self.connection.execute(
            "SELECT COALESCE(MAX(chunk_index),-1)+1 AS next FROM chunks"
        ).fetchone()
        chunk_index = int(chunk_index_row["next"])
        attempt_end = attempt_start + attempt_count - 1
        filename = f"{attempt_start:020d}-{attempt_end:020d}.spcg"
        pending_filename = f".{filename}.pending"
        final_path = self._safe_chunk_path(filename)
        pending_path = self._safe_pending_path(pending_filename)
        payload = encode_chunk(
            (item[0] for item in accepted),
            simulation_id=self.config.simulation_id,
            first_attempt=attempt_start,
            attempt_count=attempt_count,
        )
        digest = chunk_sha256(payload)
        disposition_digest = _disposition_sha256(accepted, rejected)
        with pending_path.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if _sha256_file(pending_path) != digest:
            raise ValueError("pending full-game chunk failed its write hash")

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO chunks(
                    chunk_index,attempt_start,attempt_count,state,filename,
                    pending_filename,sha256,disposition_sha256,accepted_count,
                    duplicate_count,rejected_count,terminal_json,series_count,
                    move_count,logical_work,path_saturations
                ) VALUES(?,?,?,'prepared',?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    chunk_index,
                    str(attempt_start),
                    attempt_count,
                    filename,
                    pending_filename,
                    digest,
                    disposition_digest,
                    len(accepted),
                    duplicates,
                    len(rejected) - duplicates,
                    json.dumps(terminal_counts, sort_keys=True, separators=(",", ":")),
                    series_count,
                    move_count,
                    logical_work,
                    str(path_saturations),
                ),
            )
            self.connection.executemany(
                """
                INSERT INTO seen_traces(
                    trace_sha256,attempt_index,game_id,white_profile_index,
                    black_profile_index,path_saturations,chunk_index
                ) VALUES(?,?,?,?,?,?,?)
                """,
                [
                    (
                        trace_digest,
                        str(record.attempt_index),
                        item_game_id,
                        record.white_profile_index,
                        record.black_profile_index,
                        str(record.path_count_saturations),
                        chunk_index,
                    )
                    for record, trace_digest, item_game_id in accepted
                ],
            )
            self.connection.executemany(
                """
                INSERT INTO rejected_attempts(
                    attempt_index,game_id,reason,logical_work,
                    white_profile_index,black_profile_index,path_saturations,
                    chunk_index
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        str(attempt),
                        item_game_id,
                        reason,
                        work,
                        white_profile,
                        black_profile,
                        str(record_saturations),
                        chunk_index,
                    )
                    for (
                        attempt,
                        item_game_id,
                        reason,
                        work,
                        white_profile,
                        black_profile,
                        record_saturations,
                    ) in rejected
                ],
            )

        os.replace(pending_path, final_path)
        row = self.connection.execute(
            "SELECT * FROM chunks WHERE chunk_index=?", (chunk_index,)
        ).fetchone()
        assert row is not None
        decoded = self._validate_chunk_file(row, final_path)
        self._validate_checkpoint_indexes(row, decoded)
        with self.connection:
            self.connection.execute(
                "UPDATE chunks SET state='committed' WHERE chunk_index=?",
                (chunk_index,),
            )
            self._apply_committed_chunk_to_state(row)
        self._assert_contiguous()
        self._write_manifest()
        return self.summary()


def _manifest(root: str | Path) -> tuple[Path, dict[str, Any]]:
    base = Path(root).expanduser().resolve()
    manifest_path = base / "manifest.json"
    try:
        raw = manifest_path.read_bytes()
        payload = json.loads(raw.decode("ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read full-game manifest: {error}") from error
    if not isinstance(payload, dict) or payload.get("format") != FULLGAME_RUN_FORMAT:
        raise ValueError("full-game manifest format is unsupported")
    if raw != _canonical_json(payload) + b"\n":
        raise ValueError("full-game manifest is not canonical")
    return base, payload


def fullgame_status(root: str | Path) -> dict[str, Any]:
    _, manifest = _manifest(root)
    return {
        "backend": manifest["backend"],
        "execution": manifest["execution"],
        "format": manifest["format"],
        "progress": manifest["progress"],
        "simulation_id": manifest["simulation_id"],
    }


def _checkpoint_chunks(base: Path) -> tuple[dict[str, Any], ...]:
    checkpoint = base / "checkpoint.sqlite3"
    try:
        connection = sqlite3.connect(
            f"file:{checkpoint.as_posix()}?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT * FROM chunks ORDER BY chunk_index"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise ValueError(f"could not read full-game checkpoint: {error}") from error
    if any(row["state"] != "committed" for row in rows):
        raise ValueError("full-game checkpoint contains an uncommitted chunk")
    return tuple(dict(row) for row in rows)


def iter_fullgame_records(root: str | Path) -> Iterator[FullGameRecord]:
    base, manifest = _manifest(root)
    config = FullGameSemanticConfig.from_dict(manifest["semantic_config"])
    simulation_id = str(manifest["simulation_id"])
    rows = _checkpoint_chunks(base)
    catalog = manifest.get("chunk_catalog")
    if type(catalog) is not dict or catalog != {
        "checkpoint": "checkpoint.sqlite3",
        "committed_chunks": len(rows),
    }:
        raise ValueError("manifest chunk catalog does not match checkpoint")
    for row in rows:
        filename = str(row["filename"])
        if Path(filename).name != filename or not filename.endswith(".spcg"):
            raise ValueError("manifest chunk filename is invalid")
        path = (base / "chunks" / filename).resolve()
        if path.parent != (base / "chunks").resolve():
            raise ValueError("manifest chunk path escapes its directory")
        payload = path.read_bytes()
        if chunk_sha256(payload) != str(row["sha256"]):
            raise ValueError(f"full-game chunk SHA-256 mismatch: {filename}")
        decoded = decode_chunk(payload)
        if decoded.header["simulation_id"] != simulation_id:
            raise ValueError("full-game chunk simulation identity mismatch")
        if (
            int(decoded.header["first_attempt"]) != int(row["attempt_start"])
            or int(decoded.header["attempt_count"]) != int(row["attempt_count"])
            or len(decoded.records) != int(row["accepted_count"])
        ):
            raise ValueError("full-game chunk range/count does not match manifest")
        for record in decoded.records:
            expected_pair = (
                config.profile_pair(record.attempt_index)
                if config.backend_kind == "native"
                else (0, 0)
            )
            if (
                record.white_profile_index,
                record.black_profile_index,
            ) != expected_pair:
                raise ValueError("full-game chunk profile attribution is invalid")
            yield record


def verify_fullgame_run(root: str | Path) -> dict[str, Any]:
    base, manifest = _manifest(root)
    if set(manifest) != {
        "backend",
        "checkpoint_schema",
        "chunk_catalog",
        "execution",
        "format",
        "progress",
        "semantic_config",
        "simulation_id",
    }:
        raise ValueError("full-game manifest keys are invalid")
    if manifest["backend"] not in {"native", "reference"}:
        raise ValueError("full-game manifest backend is invalid")
    if manifest["checkpoint_schema"] != FULLGAME_CHECKPOINT_SCHEMA:
        raise ValueError("full-game checkpoint schema is unsupported")
    chunk_catalog = manifest["chunk_catalog"]
    if type(chunk_catalog) is not dict or set(chunk_catalog) != {
        "checkpoint",
        "committed_chunks",
    }:
        raise ValueError("full-game manifest chunk catalog is invalid")
    if chunk_catalog["checkpoint"] != "checkpoint.sqlite3" or type(
        chunk_catalog["committed_chunks"]
    ) is not int:
        raise ValueError("full-game manifest chunk catalog fields are invalid")
    execution = manifest["execution"]
    if type(execution) is not dict or set(execution) != {
        "attempts_per_chunk",
        "target_unique_games",
    }:
        raise ValueError("full-game execution metadata is invalid")
    if any(
        type(execution[key]) is not int or execution[key] < 1
        for key in ("attempts_per_chunk", "target_unique_games")
    ):
        raise ValueError("full-game execution counters are invalid")

    semantic = manifest.get("semantic_config")
    if type(semantic) is not dict:
        raise ValueError("full-game manifest semantic config is invalid")
    config = FullGameSemanticConfig.from_dict(semantic)
    if manifest["backend"] != config.backend_kind:
        raise ValueError("full-game manifest backend label is inconsistent")
    expected_simulation = config.simulation_id
    if manifest.get("simulation_id") != expected_simulation:
        raise ValueError("full-game manifest simulation identity is invalid")

    checkpoint = base / "checkpoint.sqlite3"
    connection = sqlite3.connect(f"file:{checkpoint.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    accepted_count = duplicate_count = native_reject_count = 0
    series_count = move_count = logical_work = path_saturations = 0
    expected_attempt_start = 0
    terminal_counts: dict[str, int] = {}
    rejection_counts: dict[str, int] = {}
    previous_accepted_attempt = -1
    try:
        metadata = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key,value FROM metadata")
        }
        expected_metadata = {
            "checkpoint_schema": str(FULLGAME_CHECKPOINT_SCHEMA),
            "semantic_config": _canonical_json(config.as_dict()).decode("ascii"),
            "simulation_id": expected_simulation,
        }
        if metadata != expected_metadata:
            raise ValueError("checkpoint metadata does not match the manifest")

        chunk_rows = connection.execute(
            "SELECT * FROM chunks ORDER BY chunk_index"
        ).fetchall()
        if len(chunk_rows) != int(chunk_catalog["committed_chunks"]):
            raise ValueError("manifest and checkpoint chunk counts differ")
        for chunk_index, row in enumerate(chunk_rows):
            if int(row["chunk_index"]) != chunk_index or row["state"] != "committed":
                raise ValueError("checkpoint chunk sequence is not fully committed")
            attempt_start = int(row["attempt_start"])
            attempt_count = int(row["attempt_count"])
            if attempt_start != expected_attempt_start or attempt_count < 1:
                raise ValueError("checkpoint attempt ranges are not contiguous")
            attempt_limit = attempt_start + attempt_count
            if attempt_limit > MAX_U64 + 1:
                raise ValueError("checkpoint attempt range overflows uint64")
            expected_attempt_start = attempt_limit

            filename = str(row["filename"])
            expected_filename = (
                f"{attempt_start:020d}-{attempt_limit - 1:020d}.spcg"
            )
            if filename != expected_filename or Path(filename).name != filename:
                raise ValueError("checkpoint chunk filename is not canonical")
            path = (base / "chunks" / filename).resolve()
            if path.parent != (base / "chunks").resolve() or not path.is_file():
                raise ValueError(f"checkpoint full-game chunk is missing: {filename}")
            payload = path.read_bytes()
            sha256 = chunk_sha256(payload)
            if sha256 != str(row["sha256"]):
                raise ValueError(f"checkpoint full-game chunk SHA-256 mismatch: {filename}")
            decoded = decode_chunk(payload)
            expected_header = {
                "accepted_records": int(row["accepted_count"]),
                "attempt_count": attempt_count,
                "first_attempt": attempt_start,
                "schema": "spc-fullgame-chunk-v2",
                "simulation_id": expected_simulation,
            }
            if dict(decoded.header) != expected_header:
                raise ValueError("checkpoint chunk header does not match its row")

            seen_rows = connection.execute(
                "SELECT trace_sha256,attempt_index,game_id,white_profile_index,"
                "black_profile_index,path_saturations FROM seen_traces "
                "WHERE chunk_index=?",
                (chunk_index,),
            ).fetchall()
            seen_by_attempt = {int(item["attempt_index"]): item for item in seen_rows}
            if len(seen_by_attempt) != len(decoded.records):
                raise ValueError("checkpoint accepted index does not match its chunk")
            accepted_attempts: list[int] = []
            accepted_envelopes: list[tuple[FullGameRecord, str, str]] = []
            chunk_terminals: dict[str, int] = {}
            chunk_series = chunk_moves = chunk_work = chunk_saturations = 0
            for record in decoded.records:
                if record.attempt_index <= previous_accepted_attempt:
                    raise ValueError("accepted full-game attempts are not ordered")
                previous_accepted_attempt = record.attempt_index
                evidence = replay_record(record)
                digest = trace_sha256(record)
                seen = seen_by_attempt.get(record.attempt_index)
                if seen is None or str(seen["trace_sha256"]) != digest:
                    raise ValueError("accepted trace does not match checkpoint dedup index")
                if str(seen["game_id"]) != game_id(config, record.attempt_index):
                    raise ValueError("accepted trace game identity is invalid")
                expected_pair = (
                    config.profile_pair(record.attempt_index)
                    if config.backend_kind == "native"
                    else (0, 0)
                )
                if (
                    int(seen["white_profile_index"]),
                    int(seen["black_profile_index"]),
                ) != expected_pair or (
                    record.white_profile_index,
                    record.black_profile_index,
                ) != expected_pair:
                    raise ValueError("accepted trace profile attribution is invalid")
                if int(seen["path_saturations"]) != record.path_count_saturations:
                    raise ValueError("accepted trace saturation evidence is invalid")
                accepted_attempts.append(record.attempt_index)
                accepted_envelopes.append(
                    (record, str(seen["trace_sha256"]), str(seen["game_id"]))
                )
                terminal = record.terminal.name.lower()
                chunk_terminals[terminal] = chunk_terminals.get(terminal, 0) + 1
                terminal_counts[terminal] = terminal_counts.get(terminal, 0) + 1
                chunk_series += evidence.series_played
                chunk_moves += evidence.micro_moves_played
                chunk_work += record.logical_work
                chunk_saturations = min(
                    MAX_U64,
                    chunk_saturations + record.path_count_saturations,
                )

            rejected_rows = connection.execute(
                "SELECT attempt_index,game_id,reason,logical_work,"
                "white_profile_index,black_profile_index,path_saturations "
                "FROM rejected_attempts WHERE chunk_index=?",
                (chunk_index,),
            ).fetchall()
            rejected_attempts: list[int] = []
            rejected_envelopes: list[tuple[int, str, str, int, int, int, int]] = []
            chunk_duplicates = chunk_native_rejects = 0
            valid_reasons = {
                reason.name.lower()
                for reason in RejectReason
                if reason not in {RejectReason.NONE, RejectReason.CANCELLED}
            }
            for rejected in rejected_rows:
                attempt = int(rejected["attempt_index"])
                reason = str(rejected["reason"])
                work = int(rejected["logical_work"])
                record_saturations = int(rejected["path_saturations"])
                expected_pair = (
                    config.profile_pair(attempt)
                    if config.backend_kind == "native"
                    else (0, 0)
                )
                if (
                    reason not in valid_reasons
                    or not 0 <= work <= MAX_U64
                    or not 0 <= record_saturations <= MAX_U64
                    or (
                        int(rejected["white_profile_index"]),
                        int(rejected["black_profile_index"]),
                    )
                    != expected_pair
                ):
                    raise ValueError("checkpoint rejected attempt is invalid")
                if str(rejected["game_id"]) != game_id(config, attempt):
                    raise ValueError("rejected attempt game identity is invalid")
                rejected_attempts.append(attempt)
                rejected_envelopes.append(
                    (
                        attempt,
                        str(rejected["game_id"]),
                        reason,
                        work,
                        int(rejected["white_profile_index"]),
                        int(rejected["black_profile_index"]),
                        record_saturations,
                    )
                )
                chunk_work += work
                chunk_saturations = min(
                    MAX_U64, chunk_saturations + record_saturations
                )
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                if reason == RejectReason.DUPLICATE_TRACE.name.lower():
                    chunk_duplicates += 1
                else:
                    chunk_native_rejects += 1

            dispositions = sorted((*accepted_attempts, *rejected_attempts))
            if len(dispositions) != attempt_count or any(
                attempt != attempt_start + offset
                for offset, attempt in enumerate(dispositions)
            ):
                raise ValueError("checkpoint does not disposition every attempted index")
            try:
                stored_terminals = json.loads(str(row["terminal_json"]))
            except json.JSONDecodeError as error:
                raise ValueError("checkpoint terminal counters are invalid") from error
            if _canonical_json(stored_terminals).decode("ascii") != str(
                row["terminal_json"]
            ):
                raise ValueError("checkpoint terminal counters are not canonical")
            if (
                len(decoded.records) != int(row["accepted_count"])
                or chunk_duplicates != int(row["duplicate_count"])
                or chunk_native_rejects != int(row["rejected_count"])
                or chunk_terminals != stored_terminals
                or chunk_series != int(row["series_count"])
                or chunk_moves != int(row["move_count"])
                or chunk_work != int(row["logical_work"])
                or chunk_saturations != int(row["path_saturations"])
                or str(row["disposition_sha256"])
                != _disposition_sha256(accepted_envelopes, rejected_envelopes)
            ):
                raise ValueError("checkpoint chunk counters do not match replay")

            accepted_count += len(decoded.records)
            duplicate_count += chunk_duplicates
            native_reject_count += chunk_native_rejects
            series_count += chunk_series
            move_count += chunk_moves
            logical_work += chunk_work
            path_saturations = min(
                MAX_U64, path_saturations + chunk_saturations
            )

        seen_total = int(connection.execute("SELECT COUNT(*) FROM seen_traces").fetchone()[0])
        rejected_total = int(
            connection.execute("SELECT COUNT(*) FROM rejected_attempts").fetchone()[0]
        )
        if seen_total != accepted_count or rejected_total != duplicate_count + native_reject_count:
            raise ValueError("checkpoint contains unowned attempt identities")
        state = connection.execute(
            "SELECT * FROM run_state WHERE singleton=1"
        ).fetchone()
        if state is None or (
            int(state["next_attempt"]) != expected_attempt_start
            or int(state["accepted_count"]) != accepted_count
            or int(state["duplicate_count"]) != duplicate_count
            or int(state["rejected_count"]) != native_reject_count
            or int(state["series_count"]) != series_count
            or int(state["move_count"]) != move_count
            or int(state["logical_work"]) != logical_work
            or int(state["path_saturations"]) != path_saturations
            or int(state["chunks_committed"]) != len(chunk_rows)
            or json.loads(str(state["terminal_json"]))
            != dict(sorted(terminal_counts.items()))
            or json.loads(str(state["rejection_json"]))
            != dict(sorted(rejection_counts.items()))
        ):
            raise ValueError("checkpoint run-state aggregate does not match replay")
    except sqlite3.Error as error:
        raise ValueError(f"could not read full-game checkpoint: {error}") from error
    finally:
        connection.close()

    target = int(execution["target_unique_games"])
    expected_progress = {
        "accepted_unique_games": accepted_count,
        "attempts_committed": expected_attempt_start,
        "chunks_committed": len(chunk_rows),
        "duplicate_traces": duplicate_count,
        "data_purpose": config.data_purpose,
        "label_kind": LABEL_KIND,
        "logical_work": logical_work,
        "micro_moves": move_count,
        "native_or_policy_rejects": native_reject_count,
        "next_attempt": expected_attempt_start,
        "path_count_saturations": path_saturations,
        "policy_id": config.rank_policy_id,
        "profile_count": len(config.profile_pool),
        "profile_schedule_id": config.profile_schedule_id,
        "rejections_by_reason": dict(sorted(rejection_counts.items())),
        "series": series_count,
        "simulation_id": expected_simulation,
        "status": "complete" if accepted_count >= target else "running",
        "strength_claim": config.strength_claim,
        "target_unique_games": target,
        "terminal_counts": dict(sorted(terminal_counts.items())),
    }
    if _canonical_json(manifest.get("progress")) != _canonical_json(expected_progress):
        raise ValueError("manifest progress does not match checkpoint replay")
    return {
        "accepted_unique_games": accepted_count,
        "authoritative_replay": "passed",
        "attempts_committed": expected_attempt_start,
        "checkpoint_rejections": duplicate_count + native_reject_count,
        "chunks": len(chunk_rows),
        "logical_work": logical_work,
        "micro_moves": move_count,
        "path_count_saturations": path_saturations,
        "series": series_count,
        "simulation_id": expected_simulation,
        "terminal_counts": dict(sorted(terminal_counts.items())),
        "trace_deduplication": "passed",
    }


def export_fullgame_jsonl(
    root: str | Path,
    destination: str | Path,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    if limit is not None and limit < 1:
        raise ValueError("export limit must be positive")
    base, manifest = _manifest(root)
    config = FullGameSemanticConfig.from_dict(manifest["semantic_config"])
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    exported = 0
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            for record in iter_fullgame_records(base):
                evidence = replay_record(record)
                payload = {
                    "black_profile_id": config.profile_pool[
                        record.black_profile_index
                    ].profile_id,
                    "data_purpose": config.data_purpose,
                    "game_id": hashlib.sha256(
                        (
                            f"spc-fullgame-id-v1|{manifest['simulation_id']}|"
                            f"{record.attempt_index}"
                        ).encode("ascii")
                    ).hexdigest(),
                    "label_kind": LABEL_KIND,
                    "logical_work": record.logical_work,
                    "path_count_saturations": record.path_count_saturations,
                    "policy_id": config.rank_policy_id,
                    "profile_schedule_id": config.profile_schedule_id,
                    "result": record.result,
                    "ruleset_version": manifest["semantic_config"]["ruleset_version"],
                    "series": [list(moves) for moves in record.series],
                    "simulation_id": manifest["simulation_id"],
                    "start_pfen": ProgressiveState.initial().pfen,
                    "strength_claim": config.strength_claim,
                    "terminal": record.terminal.name.lower(),
                    "trace_sha256": trace_sha256(record),
                    "white_profile_id": config.profile_pool[
                        record.white_profile_index
                    ].profile_id,
                    **evidence.as_dict(),
                }
                stream.write(
                    json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                )
                stream.write("\n")
                exported += 1
                if limit is not None and exported >= limit:
                    break
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return {
        "destination": str(target),
        "exported_games": exported,
        "format": "spc-fullgame-jsonl-v2",
        "simulation_id": manifest["simulation_id"],
    }


BatchGenerator = Callable[
    [FullGameSemanticConfig, int, int],
    Sequence[FullGameRecord | RejectedAttempt],
]


@dataclass(frozen=True, slots=True)
class CommittedFullGameChunk:
    chunk_id: str
    path: Path
    sha256: str
    simulation_id: str
    attempt_start: int
    attempt_count: int
    accepted_records: int


class FullGameChunkSink(Protocol):
    """Optional out-of-band sink; generation never depends on this interface."""

    def store_committed_chunk(self, chunk: CommittedFullGameChunk) -> None: ...


def deliver_committed_fullgame_chunks(
    root: str | Path,
    sink: FullGameChunkSink,
    *,
    delivered_chunk_ids: Iterable[str] = (),
    limit: int | None = None,
) -> dict[str, Any]:
    """Offer verified local chunks to an optional sink after local commit.

    This deliberately has no R2/network implementation and is never called by
    the generator. A caller can resume an external copy by supplying the stable
    chunk IDs its sink has already made durable.
    """

    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("sink delivery limit must be a positive integer")
    base, manifest = _manifest(root)
    delivered = set(delivered_chunk_ids)
    if any(type(chunk_id) is not str for chunk_id in delivered):
        raise ValueError("delivered chunk IDs must be strings")
    offered: list[str] = []
    skipped = 0
    rows = _checkpoint_chunks(base)
    if manifest.get("chunk_catalog") != {
        "checkpoint": "checkpoint.sqlite3",
        "committed_chunks": len(rows),
    }:
        raise ValueError("sink manifest chunk catalog does not match checkpoint")
    for row in rows:
        sha256 = str(row["sha256"])
        chunk_id = f"spc-fullgame-chunk-{sha256}"
        if chunk_id in delivered:
            skipped += 1
            continue
        filename = str(row["filename"])
        if not CHUNK_FILENAME_PATTERN.fullmatch(filename):
            raise ValueError("sink chunk filename is invalid")
        path = (base / "chunks" / filename).resolve()
        if path.parent != (base / "chunks").resolve() or not path.is_file():
            raise ValueError(f"sink chunk is missing: {filename}")
        payload = path.read_bytes()
        if chunk_sha256(payload) != sha256:
            raise ValueError(f"sink chunk SHA-256 mismatch: {filename}")
        decoded = decode_chunk(payload)
        if (
            decoded.header["simulation_id"] != manifest["simulation_id"]
            or int(decoded.header["first_attempt"]) != int(row["attempt_start"])
            or int(decoded.header["attempt_count"]) != int(row["attempt_count"])
            or len(decoded.records) != int(row["accepted_count"])
        ):
            raise ValueError("sink chunk metadata does not match its manifest")
        sink.store_committed_chunk(
            CommittedFullGameChunk(
                chunk_id=chunk_id,
                path=path,
                sha256=sha256,
                simulation_id=str(manifest["simulation_id"]),
                attempt_start=int(row["attempt_start"]),
                attempt_count=int(row["attempt_count"]),
                accepted_records=int(row["accepted_count"]),
            )
        )
        offered.append(chunk_id)
        if limit is not None and len(offered) >= limit:
            break
    return {
        "offered_chunk_ids": offered,
        "offered_chunks": len(offered),
        "simulation_id": manifest["simulation_id"],
        "skipped_already_delivered": skipped,
    }


def run_fullgame_generation(
    root: str | Path,
    config: FullGameSemanticConfig,
    *,
    target_unique_games: int = DEFAULT_TARGET_UNIQUE_GAMES,
    attempts_per_chunk: int = DEFAULT_ATTEMPTS_PER_CHUNK,
    backend: str = "native",
    max_attempts: int | None = None,
    requested_workers: int | None = None,
    memory_per_worker_mb: int = DEFAULT_MEMORY_PER_WORKER_MB,
    reserve_memory_mb: int = DEFAULT_RESERVE_MEMORY_MB,
    progress: Callable[[Mapping[str, Any]], None] | None = None,
    generator: BatchGenerator | None = None,
) -> dict[str, Any]:
    if type(target_unique_games) is not int or target_unique_games < 1:
        raise ValueError("target_unique_games must be a positive integer")
    if type(attempts_per_chunk) is not int or not 1 <= attempts_per_chunk <= MAX_U32:
        raise ValueError("attempts_per_chunk must fit positive uint32")
    if max_attempts is not None and (
        type(max_attempts) is not int or max_attempts < 1
    ):
        raise ValueError("max_attempts must be positive")
    if requested_workers is not None and type(requested_workers) is not int:
        raise ValueError("requested_workers must be an exact integer")
    if backend not in {"native", "reference"}:
        raise ValueError("backend must be native or reference")
    from .resources import detect_resource_budget

    effective_request = (
        1 if backend == "reference" and requested_workers is None
        else requested_workers
    )
    resource_budget = detect_resource_budget(
        effective_request,
        memory_per_worker_mb=memory_per_worker_mb,
        reserve_memory_mb=reserve_memory_mb,
    )
    selected_generator = generator
    if selected_generator is None:
        selected_generator = (
            generate_native_batch_v2
            if backend == "native"
            else generate_reference_batch
        )
    started = time.perf_counter()
    committed_this_call = 0
    retrieved_this_call = 0
    discarded_prefetch_ranges = 0
    with FullGameStore(
        root,
        config,
        target_unique_games=target_unique_games,
        attempts_per_chunk=attempts_per_chunk,
        backend=backend,
    ) as store:
        summary = store.summary()
        initial_accepted = int(summary["accepted_unique_games"])
        initial_work = int(summary["logical_work"])
        initial_attempts = int(summary["attempts_committed"])
        futures: dict[
            int,
            tuple[
                int,
                Future[Sequence[FullGameRecord | RejectedAttempt]],
            ],
        ] = {}
        executor = ThreadPoolExecutor(
            max_workers=resource_budget.workers,
            thread_name_prefix="spc-fullgame",
        )
        next_submit = store.next_attempt
        submitted_this_call = 0
        try:
            while summary["status"] != "complete":
                while len(futures) < resource_budget.workers:
                    if (
                        max_attempts is not None
                        and submitted_this_call >= max_attempts
                    ):
                        break
                    count = attempts_per_chunk
                    if max_attempts is not None:
                        count = min(count, max_attempts - submitted_this_call)
                    if next_submit + count > MAX_U64 + 1:
                        raise ValueError("full-game attempt space is exhausted")
                    futures[next_submit] = (
                        count,
                        executor.submit(
                            selected_generator,
                            config,
                            next_submit,
                            count,
                        ),
                    )
                    next_submit += count
                    submitted_this_call += count
                if not futures:
                    break

                expected_start = store.next_attempt
                scheduled = futures.pop(expected_start, None)
                if scheduled is None:
                    raise ValueError("full-game scheduler lost its contiguous watermark")
                count, future = scheduled
                outcomes = future.result()
                retrieved_this_call += count
                if len(outcomes) != count:
                    raise ValueError(
                        "batch generator did not return every requested attempt"
                    )
                before = store.next_attempt
                summary = store.commit_outcomes(outcomes)
                committed_this_call += store.next_attempt - before
                if progress is not None:
                    elapsed = max(time.perf_counter() - started, 1e-9)
                    progress(
                        {
                            **summary,
                            "accepted_unique_games_per_second": (
                                int(summary["accepted_unique_games"])
                                - initial_accepted
                            ) / elapsed,
                            "committed_attempts_per_second": (
                                int(summary["attempts_committed"])
                                - initial_attempts
                            ) / elapsed,
                            "elapsed_seconds": elapsed,
                        }
                    )
            discarded_prefetch_ranges = len(futures)
        finally:
            for _, future in futures.values():
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)

        elapsed = max(time.perf_counter() - started, 1e-9)
        accepted_added = int(summary["accepted_unique_games"]) - initial_accepted
        work_added = int(summary["logical_work"]) - initial_work
        return {
            **summary,
            "accepted_unique_games_added": accepted_added,
            "accepted_unique_games_per_second": accepted_added / elapsed,
            "attempted_this_call": committed_this_call,
            "backend": backend,
            "committed_attempts_per_second": committed_this_call / elapsed,
            "discarded_or_cancelled_attempts": (
                submitted_this_call - committed_this_call
            ),
            "discarded_prefetch_ranges": discarded_prefetch_ranges,
            "elapsed_seconds": elapsed,
            "logical_work_added": work_added,
            "logical_work_per_second": work_added / elapsed,
            "resource_budget": resource_budget.as_dict(),
            "retrieved_attempts_this_call": retrieved_this_call,
            "root": str(store.root),
            "submitted_attempts_this_call": submitted_this_call,
        }
