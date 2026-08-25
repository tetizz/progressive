from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import struct
from typing import Any, Callable, Mapping, Sequence

from .fast_training import FEATURE_NAMES
from .fullgame import verify_fullgame_run
from .league import run_rules_tactical_gate
from .model import ENGINE_SOURCE_FINGERPRINT, ENGINE_VERSION
from .profiles import EngineProfile, load_profile
from .search import SearchLimits
from .selfplay_training import (
    SelfPlayCorpus,
    SelfPlaySample,
    build_verified_fullgame_corpus,
    evaluate_human_refutation_gate,
)
from .tournament import (
    FunnelCheckpoint,
    PopulationCollisionLedger,
    PopulationMember,
    PopulationStream,
    RankedCandidate,
    TournamentFunnelConfig,
    behavioral_signature,
    build_funnel_manifest,
    build_tournament_plan,
    canonical_digest,
    canonical_json_bytes,
    collapse_behavioral_phenotypes,
    component_split,
    effective_profile_id,
    finalize_survivors,
    rank_candidate_stage,
    scan_population_stage_a,
    stamp_tactical_bundle,
    validate_tactical_bundle,
    write_json_atomic,
    TRUSTED_PROMOTION_REGISTRY_PATH,
)
from .tournament_runner import (
    TournamentEnvironment,
    TournamentRunner,
    VerifiedFullGameCorpusSpec,
    PromotionBatchAbandonmentReason,
    abandon_promotion_batch,
    build_corpus_exclusion_artifact,
    freeze_opening_suites_before_batch,
    reserve_promotion_batch,
    validate_opening_suite_preparation,
)


CHALLENGER_DRIVER_FORMAT = "spc-challenger-driver-v2"
CHALLENGER_CACHE_FORMAT = "spc-challenger-proxy-cache-v1"
CHALLENGER_CATALOG_FORMAT = "spc-challenger-profile-catalog-v1"
CACHED_SCORER_ID = "quadratic-fixed-point-fullgame-value-proxy-v1"
TARGET_EVALUATION_POINTS = 1_000
SAMPLE_WEIGHT_SCALE = 1_000_000
STAGE_B_REGULARIZATION = 100_000
DEFAULT_STAGE_A_CHECKPOINT_EVERY = 65_536
REQUIRED_CHALLENGER_FULLGAME_UNIQUE_GAMES = 1_000_000


Progress = Callable[[str], None]


def _quiet(_: str) -> None:
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as error:
        raise ValueError(f"could not read input artifact {path}: {error}") from error
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read challenger artifact {path}: {error}") from error
    if type(payload) is not dict:
        raise ValueError(f"challenger artifact must be a JSON object: {path}")
    return payload


def _artifact_digest(
    payload: Mapping[str, Any], *, domain: str, digest_field: str
) -> str:
    deterministic = {
        key: value for key, value in payload.items() if key != digest_field
    }
    expected = canonical_digest(domain, deterministic)
    if payload.get(digest_field) != expected:
        raise ValueError(f"challenger artifact {digest_field} mismatch")
    return expected


def _resolved_inputs(
    fullgame_store: str | Path,
    profile_path: str | Path,
    batch_registry: str | Path,
) -> tuple[Path, Path, Path]:
    store = Path(fullgame_store).expanduser().resolve()
    profile = Path(profile_path).expanduser().resolve()
    registry = Path(batch_registry).expanduser().resolve()
    if not store.is_dir():
        raise ValueError(f"verified full-game store does not exist: {store}")
    if not profile.is_file():
        raise ValueError(f"engine profile does not exist: {profile}")
    trusted = TRUSTED_PROMOTION_REGISTRY_PATH.expanduser().resolve()
    if registry != trusted:
        raise ValueError(
            "production challengers require the trusted project batch registry: "
            f"{trusted}"
        )
    return store, profile, registry


@dataclass(frozen=True, slots=True)
class CachedProxyRow:
    position_hash: str
    split_component: str
    chosen_series: str
    mover: str
    target_twice_minus_one: int
    sample_weight_units: int
    features: tuple[int, ...]

    @classmethod
    def from_sample(cls, sample: SelfPlaySample) -> CachedProxyRow:
        target = round(float(sample.target_white_score) * 2) - 1
        if target not in {-1, 0, 1}:
            raise ValueError("full-game sample target is not an exact WDL value")
        weight = max(1, round(float(sample.sample_weight) * SAMPLE_WEIGHT_SCALE))
        row = cls(
            position_hash=str(sample.position_hash),
            split_component=str(sample.split_component),
            chosen_series=str(sample.chosen_series),
            mover=str(sample.mover),
            target_twice_minus_one=target,
            sample_weight_units=weight,
            features=tuple(int(getattr(sample.features, name)) for name in FEATURE_NAMES),
        )
        row.validate()
        return row

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CachedProxyRow:
        if set(payload) != {
            "position_hash",
            "split_component",
            "chosen_series",
            "mover",
            "target_twice_minus_one",
            "sample_weight_units",
            "features",
        }:
            raise ValueError("cached proxy row fields are not canonical")
        row = cls(
            position_hash=str(payload["position_hash"]),
            split_component=str(payload["split_component"]),
            chosen_series=str(payload["chosen_series"]),
            mover=str(payload["mover"]),
            target_twice_minus_one=int(payload["target_twice_minus_one"]),
            sample_weight_units=int(payload["sample_weight_units"]),
            features=tuple(int(value) for value in payload["features"]),
        )
        row.validate()
        return row

    def validate(self) -> None:
        try:
            decoded = bytes.fromhex(self.position_hash)
        except ValueError as error:
            raise ValueError("cached proxy position hash is not hex") from error
        if len(decoded) != 16:
            raise ValueError("cached proxy position hash is not 128-bit")
        if (
            not self.split_component.startswith("spc-split-component-")
            or not self.chosen_series
            or self.mover not in {"white", "black"}
            or self.target_twice_minus_one not in {-1, 0, 1}
            or self.sample_weight_units < 1
            or len(self.features) != len(FEATURE_NAMES)
        ):
            raise ValueError("cached proxy row is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "features": list(self.features),
        }


@dataclass(frozen=True, slots=True)
class ChallengerProxyCache:
    corpus_id: str
    corpus_manifest_sha256: str
    corpus_snapshot_digest: str
    component_split_digest: str
    train_rows: tuple[CachedProxyRow, ...]
    validation_rows: tuple[CachedProxyRow, ...]
    audit_position_count: int
    artifact_digest: str

    @property
    def stage_a_rows(self) -> tuple[CachedProxyRow, ...]:
        return self.train_rows[:64]

    @property
    def stage_b_rows(self) -> tuple[CachedProxyRow, ...]:
        return self.train_rows[:1_024]

    @property
    def stage_c_rows(self) -> tuple[CachedProxyRow, ...]:
        return self.validation_rows[:4_096]

    def as_dict(self) -> dict[str, Any]:
        deterministic = {
            "format": CHALLENGER_CACHE_FORMAT,
            "engine_version": ENGINE_VERSION,
            "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
            "corpus_id": self.corpus_id,
            "corpus_manifest_sha256": self.corpus_manifest_sha256,
            "corpus_snapshot_digest": self.corpus_snapshot_digest,
            "component_split_digest": self.component_split_digest,
            "scorer": {
                "id": CACHED_SCORER_ID,
                "feature_names": list(FEATURE_NAMES),
                "sample_weight_scale": SAMPLE_WEIGHT_SCALE,
                "target_evaluation_points": TARGET_EVALUATION_POINTS,
                "stage_b_regularization": STAGE_B_REGULARIZATION,
                "claim": "cached weak-value proxy; not WDL, Elo, or promotion evidence",
            },
            "stage_counts": {
                "stage_a_train": len(self.stage_a_rows),
                "stage_b_train": len(self.stage_b_rows),
                "stage_c_validation": len(self.stage_c_rows),
                "audit_positions_sealed": self.audit_position_count,
            },
            "train_rows": [row.as_dict() for row in self.train_rows],
            "validation_rows": [row.as_dict() for row in self.validation_rows],
        }
        return {**deterministic, "artifact_digest": self.artifact_digest}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ChallengerProxyCache:
        required = {
            "format",
            "engine_version",
            "source_fingerprint",
            "corpus_id",
            "corpus_manifest_sha256",
            "corpus_snapshot_digest",
            "component_split_digest",
            "scorer",
            "stage_counts",
            "train_rows",
            "validation_rows",
            "artifact_digest",
        }
        if set(payload) != required or (
            payload.get("format") != CHALLENGER_CACHE_FORMAT
            or payload.get("engine_version") != ENGINE_VERSION
            or payload.get("source_fingerprint") != ENGINE_SOURCE_FINGERPRINT
        ):
            raise ValueError("challenger proxy cache source/schema identity is stale")
        digest = _artifact_digest(
            payload,
            domain="spc-challenger-proxy-cache-v1\0",
            digest_field="artifact_digest",
        )
        expected_scorer = {
            "id": CACHED_SCORER_ID,
            "feature_names": list(FEATURE_NAMES),
            "sample_weight_scale": SAMPLE_WEIGHT_SCALE,
            "target_evaluation_points": TARGET_EVALUATION_POINTS,
            "stage_b_regularization": STAGE_B_REGULARIZATION,
            "claim": "cached weak-value proxy; not WDL, Elo, or promotion evidence",
        }
        if payload.get("scorer") != expected_scorer:
            raise ValueError("challenger cached scorer contract changed")
        cache = cls(
            corpus_id=str(payload["corpus_id"]),
            corpus_manifest_sha256=str(payload["corpus_manifest_sha256"]),
            corpus_snapshot_digest=str(payload["corpus_snapshot_digest"]),
            component_split_digest=str(payload["component_split_digest"]),
            train_rows=tuple(
                CachedProxyRow.from_dict(item) for item in payload["train_rows"]
            ),
            validation_rows=tuple(
                CachedProxyRow.from_dict(item)
                for item in payload["validation_rows"]
            ),
            audit_position_count=int(payload["stage_counts"]["audit_positions_sealed"]),
            artifact_digest=digest,
        )
        if (
            len(cache.train_rows) != 1_024
            or len(cache.validation_rows) != 4_096
            or cache.audit_position_count < 1
            or payload["stage_counts"]
            != {
                "stage_a_train": 64,
                "stage_b_train": 1_024,
                "stage_c_validation": 4_096,
                "audit_positions_sealed": cache.audit_position_count,
            }
            or len({row.position_hash for row in cache.train_rows})
            != len(cache.train_rows)
            or len({row.position_hash for row in cache.validation_rows})
            != len(cache.validation_rows)
            or {row.position_hash for row in cache.train_rows}
            & {row.position_hash for row in cache.validation_rows}
            or {row.split_component for row in cache.train_rows}
            & {row.split_component for row in cache.validation_rows}
        ):
            raise ValueError("challenger proxy cache split/count contract is invalid")
        return cache


def _sample_digest(corpus: SelfPlayCorpus) -> str:
    digest = hashlib.sha256(b"spc-challenger-fullgame-samples-v1\0")
    for sample in corpus.samples:
        digest.update(canonical_json_bytes(sample.as_dict()))
    return digest.hexdigest()


def _manifest_snapshot_evidence(corpus: SelfPlayCorpus) -> Mapping[str, Any]:
    verified = [
        item
        for item in corpus.database_evidence
        if item.get("source_kind") == "verified-fullgame-store-snapshot"
    ]
    if len(verified) != 1:
        raise ValueError("full-game corpus lacks one verified store snapshot")
    return verified[0]


def _deduplicate_proxy_samples_by_split(
    samples: Sequence[SelfPlaySample],
    component_map: Mapping[str, str],
    *,
    master_seed: int,
) -> tuple[
    dict[str, dict[str, tuple[str, SelfPlaySample]]],
    str,
]:
    """Keeps duplicate positions only in the highest-priority funnel split."""

    split_priority = {"train": 0, "validation": 1, "audit": 2}
    sample_components = {sample.split_component for sample in samples}
    if set(component_map) != sample_components or any(
        split not in split_priority for split in component_map.values()
    ):
        raise ValueError("challenger component split map is incomplete")

    components_by_game: dict[tuple[str, str], set[str]] = {}
    owner_by_position: dict[str, str] = {}
    for sample in samples:
        identity = (sample.run_id, sample.game_key)
        components_by_game.setdefault(identity, set()).add(sample.split_component)
        split = component_map[sample.split_component]
        current = owner_by_position.get(sample.position_hash)
        if current is None or split_priority[split] < split_priority[current]:
            owner_by_position[sample.position_hash] = split
    if any(len(components) != 1 for components in components_by_game.values()):
        raise ValueError("challenger corpus does not keep whole-game components")

    chosen_by_split: dict[str, dict[str, tuple[str, SelfPlaySample]]] = {
        "train": {},
        "validation": {},
        "audit": {},
    }
    for sample in samples:
        split = component_map[sample.split_component]
        if owner_by_position[sample.position_hash] != split:
            continue
        selection_key = hashlib.sha256(
            (
                f"spc-challenger-cache-row-v1|{master_seed}|"
                f"{sample.position_hash}|{sample.game_key}|{sample.chosen_series}"
            ).encode("ascii")
        ).hexdigest()
        current = chosen_by_split[split].get(sample.position_hash)
        if current is None or selection_key < current[0]:
            chosen_by_split[split][sample.position_hash] = (selection_key, sample)

    position_sets = {
        split: set(chosen_by_split[split]) for split in split_priority
    }
    if any(
        position_sets[left] & position_sets[right]
        for left, right in (
            ("train", "validation"),
            ("train", "audit"),
            ("validation", "audit"),
        )
    ):
        raise AssertionError("challenger proxy split position leakage")
    owner_digest = hashlib.sha256(b"spc-challenger-position-owner-v1\0")
    for position_hash, split in sorted(owner_by_position.items()):
        owner_digest.update(canonical_json_bytes([position_hash, split]))
    return chosen_by_split, owner_digest.hexdigest()


def _build_proxy_cache(
    corpus: SelfPlayCorpus,
    *,
    config: TournamentFunnelConfig,
) -> ChallengerProxyCache:
    evidence = _manifest_snapshot_evidence(corpus)
    manifest_sha256 = str(evidence.get("manifest_sha256", ""))
    if len(manifest_sha256) != 64:
        raise ValueError("verified full-game manifest digest is invalid")

    component_map = {
        sample.split_component: component_split(
            sample.split_component,
            config=config,
            source_fingerprint=ENGINE_SOURCE_FINGERPRINT,
        )
        for sample in corpus.samples
    }
    chosen_by_split, position_owner_digest = _deduplicate_proxy_samples_by_split(
        corpus.samples,
        component_map,
        master_seed=config.master_seed,
    )
    split_digest = canonical_digest(
        "spc-challenger-component-split-v1\0",
        {
            "components": dict(sorted(component_map.items())),
            "collision_priority": ["train", "validation", "audit"],
            "position_owner_digest": position_owner_digest,
        },
    )

    def select(split: str, count: int) -> tuple[CachedProxyRow, ...]:
        ordered = sorted(chosen_by_split[split].values(), key=lambda item: item[0])
        if len(ordered) < count:
            raise ValueError(
                f"verified full-game store has only {len(ordered)} unique {split} "
                f"positions; the frozen funnel requires {count}"
            )
        return tuple(CachedProxyRow.from_sample(sample) for _, sample in ordered[:count])

    train = select("train", 1_024)
    validation = select("validation", 4_096)
    audit_count = len(chosen_by_split["audit"])
    if audit_count < 1:
        raise ValueError("verified full-game store has no sealed audit component")
    sample_digest = _sample_digest(corpus)
    corpus_snapshot_digest = canonical_digest(
        "spc-challenger-corpus-snapshot-v1\0",
        {
            "corpus_id": corpus.corpus_id,
            "manifest_sha256": manifest_sha256,
            "completed_games": corpus.completed_games,
            "excluded_games": corpus.excluded_games,
            "sample_count": len(corpus.samples),
            "sample_digest": sample_digest,
        },
    )
    deterministic = {
        "format": CHALLENGER_CACHE_FORMAT,
        "engine_version": ENGINE_VERSION,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "corpus_id": corpus.corpus_id,
        "corpus_manifest_sha256": manifest_sha256,
        "corpus_snapshot_digest": corpus_snapshot_digest,
        "component_split_digest": split_digest,
        "scorer": {
            "id": CACHED_SCORER_ID,
            "feature_names": list(FEATURE_NAMES),
            "sample_weight_scale": SAMPLE_WEIGHT_SCALE,
            "target_evaluation_points": TARGET_EVALUATION_POINTS,
            "stage_b_regularization": STAGE_B_REGULARIZATION,
            "claim": "cached weak-value proxy; not WDL, Elo, or promotion evidence",
        },
        "stage_counts": {
            "stage_a_train": 64,
            "stage_b_train": 1_024,
            "stage_c_validation": 4_096,
            "audit_positions_sealed": audit_count,
        },
        "train_rows": [row.as_dict() for row in train],
        "validation_rows": [row.as_dict() for row in validation],
    }
    return ChallengerProxyCache(
        corpus_id=corpus.corpus_id,
        corpus_manifest_sha256=manifest_sha256,
        corpus_snapshot_digest=corpus_snapshot_digest,
        component_split_digest=split_digest,
        train_rows=train,
        validation_rows=validation,
        audit_position_count=audit_count,
        artifact_digest=canonical_digest(
            "spc-challenger-proxy-cache-v1\0", deterministic
        ),
    )


class QuadraticCachedScorer:
    """Constant-work exact-integer summary of cached weak terminal labels."""

    def __init__(
        self,
        rows: Sequence[CachedProxyRow],
        *,
        regularization: int = 0,
    ) -> None:
        if not rows:
            raise ValueError("cached scorer requires at least one position")
        if regularization < 0:
            raise ValueError("cached scorer regularization cannot be negative")
        size = len(FEATURE_NAMES)
        linear = [0] * size
        quadratic = [[0] * size for _ in range(size)]
        constant = 0
        total_weight = 0
        for row in rows:
            weight = row.sample_weight_units
            target = (
                row.target_twice_minus_one * TARGET_EVALUATION_POINTS * 100
            )
            total_weight += weight
            constant += weight * target * target
            for left, feature in enumerate(row.features):
                linear[left] -= 2 * weight * target * feature
                for right in range(left, size):
                    coefficient = weight * feature * row.features[right]
                    quadratic[left][right] += (
                        coefficient if left == right else 2 * coefficient
                    )
        self._linear = tuple(linear)
        self._quadratic = tuple(
            tuple(quadratic[left][right] for right in range(left, size))
            for left in range(size)
        )
        self._constant = constant
        self._total_weight = total_weight
        self._regularization = regularization
        self.rows = tuple(rows)
        self.scorer_digest = canonical_digest(
            "spc-challenger-cached-scorer-v1\0",
            {
                "id": CACHED_SCORER_ID,
                "rows": [row.as_dict() for row in rows],
                "regularization": regularization,
                "target_evaluation_points": TARGET_EVALUATION_POINTS,
                "sample_weight_scale": SAMPLE_WEIGHT_SCALE,
            },
        )

    def __call__(self, member: PopulationMember) -> int:
        weights = tuple(
            int(getattr(member.profile.weights, name)) for name in FEATURE_NAMES
        )
        numerator = self._constant
        numerator += sum(
            coefficient * weight
            for coefficient, weight in zip(self._linear, weights, strict=True)
        )
        for left, coefficients in enumerate(self._quadratic):
            left_weight = weights[left]
            numerator += sum(
                coefficient * left_weight * weights[right]
                for coefficient, right in zip(
                    coefficients, range(left, len(weights)), strict=True
                )
            )
        loss = numerator // self._total_weight
        if loss < 0:
            raise AssertionError("quadratic cached loss cannot be negative")
        return loss + self._regularization * sum(
            (weight - 100) ** 2 for weight in weights
        )


def _cache_evidence_digests(cache: ChallengerProxyCache) -> dict[str, str]:
    return {
        "corpus": cache.corpus_snapshot_digest,
        "component_split": cache.component_split_digest,
        "stage_a_cache": canonical_digest(
            "spc-challenger-stage-a-cache-v1\0",
            [row.as_dict() for row in cache.stage_a_rows],
        ),
        "stage_b_cache": canonical_digest(
            "spc-challenger-stage-b-cache-v1\0",
            [row.as_dict() for row in cache.stage_b_rows],
        ),
        "validation_cache": canonical_digest(
            "spc-challenger-stage-c-cache-v1\0",
            [row.as_dict() for row in cache.stage_c_rows],
        ),
        "rules_suite": canonical_digest(
            "spc-challenger-tactical-contract-v1\0",
            {
                "rules_gate": "run_rules_tactical_gate",
                "human_gate": "human-refutation-v1",
                "depth_series": 2,
                "branch_cap": 32,
                "max_generation_positions": 250_000,
                "time_limit_seconds": None,
                "collect_all_root_scores": False,
            },
        ),
    }


def _require_cache_input_identity(
    cache: ChallengerProxyCache, *, corpus_id: object, manifest_sha256: object
) -> None:
    if (
        cache.corpus_id != corpus_id
        or cache.corpus_manifest_sha256 != manifest_sha256
    ):
        raise ValueError("challenger proxy cache corpus identity changed")


def _read_complete_store_manifest(store: Path) -> tuple[bytes, dict[str, Any]]:
    path = store / "manifest.json"
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read full-game manifest: {error}") from error
    progress = manifest.get("progress") if type(manifest) is dict else None
    execution = manifest.get("execution") if type(manifest) is dict else None
    if (
        type(manifest) is not dict
        or not isinstance(progress, Mapping)
        or not isinstance(execution, Mapping)
        or progress.get("status") != "complete"
    ):
        raise ValueError("challenger funnel requires a completed full-game store snapshot")
    target = REQUIRED_CHALLENGER_FULLGAME_UNIQUE_GAMES
    if (
        execution.get("target_unique_games") != target
        or progress.get("target_unique_games") != target
        or progress.get("accepted_unique_games") != target
    ):
        raise ValueError(
            "challenger funnel requires exactly 1,000,000 verified unique "
            "full games"
        )
    return raw, manifest


def preflight_challengers(
    run_root: str | Path,
    *,
    fullgame_store: str | Path,
    profile_path: str | Path,
    batch_registry: str | Path,
    progress: Progress = _quiet,
) -> dict[str, Any]:
    root = Path(run_root).expanduser().resolve()
    store, profile_file, registry = _resolved_inputs(
        fullgame_store, profile_path, batch_registry
    )
    profile = load_profile(profile_file)
    raw_manifest, store_manifest = _read_complete_store_manifest(store)
    environment = TournamentEnvironment.current(require_native=True)
    if environment.source_fingerprint != ENGINE_SOURCE_FINGERPRINT:
        raise ValueError("loaded package source fingerprint changed during preflight")
    config = TournamentFunnelConfig()

    sealed_path = root / "preflight.json"
    if sealed_path.exists():
        sealed, _cache, _manifest, _profile = _load_preflight(
            root,
            fullgame_store=store,
            profile_path=profile_file,
            batch_registry=registry,
            verify_store=True,
        )
        return {**sealed, "resumed": True}

    root.mkdir(parents=True, exist_ok=True)
    allowed_unsealed = {"proxy-cache.json", "funnel-manifest.json"}
    unexpected = [path.name for path in root.iterdir() if path.name not in allowed_unsealed]
    if unexpected:
        raise ValueError(
            "challenger run directory contains unsealed artifacts: "
            + ", ".join(sorted(unexpected))
        )

    progress("replaying and verifying the completed full-game store")
    corpus = build_verified_fullgame_corpus(
        store,
        seed=config.master_seed,
        holdout_percent=20,
        max_games=None,
    )
    evidence = _manifest_snapshot_evidence(corpus)
    current_manifest_sha256 = hashlib.sha256(raw_manifest).hexdigest()
    if evidence.get("manifest_sha256") != current_manifest_sha256:
        raise ValueError("full-game manifest changed across verified corpus construction")
    progress("compiling the fixed train/validation proxy cache")
    cache = _build_proxy_cache(corpus, config=config)
    write_json_atomic(cache.as_dict(), root / "proxy-cache.json")
    roundtrip_cache = ChallengerProxyCache.from_dict(
        _load_json(root / "proxy-cache.json")
    )
    if roundtrip_cache != cache:
        raise ValueError("published challenger cache did not round-trip")

    manifest = build_funnel_manifest(
        profile,
        evidence_digests=_cache_evidence_digests(cache),
        native_source_identity=environment.native_source_identity,
        runtime_identity=environment.runtime,
        config=config,
        source_fingerprint=environment.source_fingerprint,
    )
    write_json_atomic(manifest, root / "funnel-manifest.json")

    deterministic = {
        "format": CHALLENGER_DRIVER_FORMAT,
        "state": "preflight-ready",
        "engine_version": environment.engine_version,
        "source_fingerprint": environment.source_fingerprint,
        "environment": environment.as_dict(),
        "inputs": {
            "fullgame_store": str(store),
            "fullgame_manifest_sha256": current_manifest_sha256,
            "fullgame_simulation_id": str(store_manifest["simulation_id"]),
            "fullgame_target_unique_games": (
                REQUIRED_CHALLENGER_FULLGAME_UNIQUE_GAMES
            ),
            "fullgame_accepted_unique_games": int(
                store_manifest["progress"]["accepted_unique_games"]
            ),
            "corpus_id": corpus.corpus_id,
            "profile_path": str(profile_file),
            "profile_file_sha256": _sha256_file(profile_file),
            "profile_id": profile.profile_id,
            "baseline_effective_id": effective_profile_id(
                profile.weights,
                source_fingerprint=environment.source_fingerprint,
            ),
            "batch_registry": str(registry),
        },
        "proxy_cache": {
            "path": "proxy-cache.json",
            "artifact_digest": cache.artifact_digest,
        },
        "funnel_manifest": {
            "path": "funnel-manifest.json",
            "protocol_digest": manifest["protocol_digest"],
        },
        "outputs": {
            "champion_write": False,
            "promotion_decision": False,
            "strength_claim": False,
        },
    }
    sealed = {
        **deterministic,
        "preflight_digest": canonical_digest(
            "spc-challenger-driver-preflight-v2\0", deterministic
        ),
    }
    write_json_atomic(sealed, sealed_path)
    return {**sealed, "resumed": False}


def _load_preflight(
    run_root: Path,
    *,
    fullgame_store: str | Path,
    profile_path: str | Path,
    batch_registry: str | Path,
    verify_store: bool,
) -> tuple[
    dict[str, Any], ChallengerProxyCache, dict[str, Any], EngineProfile
]:
    root = run_root.expanduser().resolve()
    store, profile_file, registry = _resolved_inputs(
        fullgame_store, profile_path, batch_registry
    )
    sealed = _load_json(root / "preflight.json")
    if set(sealed) != {
        "format",
        "state",
        "engine_version",
        "source_fingerprint",
        "environment",
        "inputs",
        "proxy_cache",
        "funnel_manifest",
        "outputs",
        "preflight_digest",
    } or sealed.get("format") != CHALLENGER_DRIVER_FORMAT:
        raise ValueError("challenger preflight schema is invalid")
    _artifact_digest(
        sealed,
        domain="spc-challenger-driver-preflight-v2\0",
        digest_field="preflight_digest",
    )
    environment = TournamentEnvironment.current(require_native=True)
    if (
        sealed.get("state") != "preflight-ready"
        or sealed.get("engine_version") != environment.engine_version
        or sealed.get("source_fingerprint") != environment.source_fingerprint
        or sealed.get("environment") != environment.as_dict()
    ):
        raise ValueError("challenger preflight source/runtime identity is stale")
    inputs = sealed.get("inputs")
    if not isinstance(inputs, Mapping) or (
        inputs.get("fullgame_store") != str(store)
        or inputs.get("profile_path") != str(profile_file)
        or inputs.get("batch_registry") != str(registry)
        or inputs.get("profile_file_sha256") != _sha256_file(profile_file)
        or inputs.get("fullgame_target_unique_games")
        != REQUIRED_CHALLENGER_FULLGAME_UNIQUE_GAMES
        or inputs.get("fullgame_accepted_unique_games")
        != REQUIRED_CHALLENGER_FULLGAME_UNIQUE_GAMES
    ):
        raise ValueError("challenger preflight input identity changed")
    profile = load_profile(profile_file)
    if profile.profile_id != inputs.get("profile_id"):
        raise ValueError("challenger baseline profile identity changed")

    raw_manifest, manifest_payload = _read_complete_store_manifest(store)
    if (
        hashlib.sha256(raw_manifest).hexdigest()
        != inputs.get("fullgame_manifest_sha256")
        or manifest_payload.get("simulation_id")
        != inputs.get("fullgame_simulation_id")
        or manifest_payload.get("execution", {}).get("target_unique_games")
        != inputs.get("fullgame_target_unique_games")
        or manifest_payload.get("progress", {}).get("accepted_unique_games")
        != inputs.get("fullgame_accepted_unique_games")
    ):
        raise ValueError("challenger full-game corpus snapshot identity changed")
    if verify_store:
        verification = verify_fullgame_run(store)
        if (
            verification.get("simulation_id")
            != inputs.get("fullgame_simulation_id")
            or verification.get("accepted_unique_games")
            != REQUIRED_CHALLENGER_FULLGAME_UNIQUE_GAMES
        ):
            raise ValueError("verified full-game corpus simulation changed")

    cache_record = sealed.get("proxy_cache")
    manifest_record = sealed.get("funnel_manifest")
    if not isinstance(cache_record, Mapping) or not isinstance(
        manifest_record, Mapping
    ):
        raise ValueError("challenger preflight artifact table is invalid")
    cache = ChallengerProxyCache.from_dict(
        _load_json(root / str(cache_record.get("path", "")))
    )
    if cache.artifact_digest != cache_record.get("artifact_digest"):
        raise ValueError("challenger proxy cache identity changed")
    _require_cache_input_identity(
        cache,
        corpus_id=inputs.get("corpus_id"),
        manifest_sha256=inputs.get("fullgame_manifest_sha256"),
    )
    manifest = _load_json(root / str(manifest_record.get("path", "")))
    if (
        manifest.get("protocol_digest") != manifest_record.get("protocol_digest")
        or manifest.get("source_fingerprint") != environment.source_fingerprint
        or manifest.get("baseline", {}).get("profile_id") != profile.profile_id
        or manifest.get("evidence_digests") != _cache_evidence_digests(cache)
    ):
        raise ValueError("challenger funnel manifest identity changed")
    rebuilt = build_funnel_manifest(
        profile,
        evidence_digests=_cache_evidence_digests(cache),
        native_source_identity=environment.native_source_identity,
        runtime_identity=environment.runtime,
        config=TournamentFunnelConfig(),
        source_fingerprint=environment.source_fingerprint,
    )
    if manifest != rebuilt:
        raise ValueError("challenger funnel manifest cannot be regenerated")
    return sealed, cache, manifest, profile


def _load_checkpoint(path: Path) -> FunnelCheckpoint | None:
    return FunnelCheckpoint.from_dict(_load_json(path)) if path.exists() else None


def _validate_complete_checkpoint(
    checkpoint: FunnelCheckpoint,
    *,
    protocol_digest: str,
    stage: str,
    scorer_digest: str,
    input_size: int,
    keep_count: int,
) -> None:
    if (
        checkpoint.protocol_digest != protocol_digest
        or checkpoint.stage != stage
        or checkpoint.scorer_digest != scorer_digest
        or checkpoint.input_size != input_size
        or checkpoint.keep_count != keep_count
        or not checkpoint.complete
        or checkpoint.next_input_offset != input_size
        or len(checkpoint.ranked_candidates) != keep_count
    ):
        raise ValueError(f"persisted {stage} checkpoint identity/shape changed")


def _validate_checkpoint_candidates(
    checkpoint: FunnelCheckpoint,
    *,
    stream: PopulationStream,
    scorer: QuadraticCachedScorer,
    allowed_indices: set[int] | None = None,
) -> None:
    for candidate in checkpoint.ranked_candidates:
        if allowed_indices is not None and candidate.candidate_index not in allowed_indices:
            raise ValueError(
                f"persisted {checkpoint.stage} candidate is outside its sealed input"
            )
        member = stream.member(candidate.candidate_index)
        if (
            member.effective_id != candidate.effective_id
            or member.profile.profile_id != candidate.profile_id
            or scorer(member) != candidate.rank_units
        ):
            raise ValueError(
                f"persisted {checkpoint.stage} candidate cannot be regenerated"
            )


def _reconcile_stage_a_ledger(
    ledger: PopulationCollisionLedger,
    *,
    stream: PopulationStream,
    checkpoint: FunnelCheckpoint | None,
) -> None:
    """Roll back only a committed Stage-A batch lacking its atomic JSON seal."""

    expected = 0 if checkpoint is None else checkpoint.next_input_offset
    actual = ledger.count()
    if actual < expected:
        raise ValueError(
            f"collision ledger/checkpoint mismatch: {actual} < {expected}"
        )
    if actual == expected:
        return
    if actual > len(stream):
        raise ValueError("collision ledger extends beyond the population")
    rows = ledger.connection.execute(
        """
        select candidate_index, weight_key, effective_id, profile_id
        from population_identity
        where candidate_index >= ?
        order by candidate_index
        """,
        (expected,),
    ).fetchall()
    if len(rows) != actual - expected:
        raise ValueError("collision ledger unsealed suffix is not contiguous")
    for offset, row in enumerate(rows, expected):
        member = stream.member(offset)
        if (
            int(row[0]) != offset
            or bytes(row[1]) != struct.pack("<7H", *member.weight_tuple)
            or str(row[2]) != member.effective_id
            or str(row[3]) != member.profile.profile_id
        ):
            raise ValueError("collision ledger unsealed suffix cannot be regenerated")
    ledger.connection.execute(
        "delete from population_identity where candidate_index >= ?", (expected,)
    )
    ledger.commit()
    ledger.require_count(expected)


def _run_cached_funnel(
    root: Path,
    *,
    baseline: EngineProfile,
    cache: ChallengerProxyCache,
    manifest: Mapping[str, Any],
    checkpoint_every: int,
    progress: Progress,
) -> tuple[PopulationStream, FunnelCheckpoint, FunnelCheckpoint]:
    if checkpoint_every < 1:
        raise ValueError("stage-A checkpoint interval must be positive")
    config = TournamentFunnelConfig()
    protocol_digest = str(manifest["protocol_digest"])
    stream = PopulationStream(
        baseline,
        config,
        source_fingerprint=str(manifest["source_fingerprint"]),
    )
    stage_a_scorer = QuadraticCachedScorer(cache.stage_a_rows)
    stage_b_scorer = QuadraticCachedScorer(
        cache.stage_b_rows, regularization=STAGE_B_REGULARIZATION
    )
    stage_c_scorer = QuadraticCachedScorer(cache.stage_c_rows)

    stage_a_path = root / "stage-a-checkpoint.json"
    stage_a = _load_checkpoint(stage_a_path)
    with PopulationCollisionLedger(
        root / "population-collisions.sqlite3",
        protocol_digest=protocol_digest,
    ) as ledger:
        _reconcile_stage_a_ledger(ledger, stream=stream, checkpoint=stage_a)
        while stage_a is None or not stage_a.complete:
            start = 0 if stage_a is None else stage_a.next_input_offset
            stop = min(len(stream), start + checkpoint_every)
            progress(f"Stage A: scoring candidates {start:,}..{stop - 1:,}")
            stage_a = scan_population_stage_a(
                stream,
                stage_a_scorer,
                protocol_digest=protocol_digest,
                scorer_digest=stage_a_scorer.scorer_digest,
                collision_ledger=ledger,
                checkpoint=stage_a,
                stop_index=stop,
            )
            write_json_atomic(stage_a.as_dict(), stage_a_path)
    assert stage_a is not None
    _validate_complete_checkpoint(
        stage_a,
        protocol_digest=protocol_digest,
        stage="stage-a",
        scorer_digest=stage_a_scorer.scorer_digest,
        input_size=len(stream),
        keep_count=config.stage_a_keep,
    )
    _validate_checkpoint_candidates(
        stage_a,
        stream=stream,
        scorer=stage_a_scorer,
    )

    stage_b_path = root / "stage-b-checkpoint.json"
    stage_b = _load_checkpoint(stage_b_path)
    if stage_b is None:
        progress("Stage B: rescoring the exact 65,536-candidate cut")
        stage_b = rank_candidate_stage(
            stream,
            stage_a.ranked_candidates,
            stage_b_scorer,
            protocol_digest=protocol_digest,
            scorer_digest=stage_b_scorer.scorer_digest,
            stage="stage-b",
            keep_count=config.stage_b_keep,
            initial_disposition=stage_a.disposition,
        )
        write_json_atomic(stage_b.as_dict(), stage_b_path)
    _validate_complete_checkpoint(
        stage_b,
        protocol_digest=protocol_digest,
        stage="stage-b",
        scorer_digest=stage_b_scorer.scorer_digest,
        input_size=config.stage_a_keep,
        keep_count=config.stage_b_keep,
    )
    _validate_checkpoint_candidates(
        stage_b,
        stream=stream,
        scorer=stage_b_scorer,
        allowed_indices={item.candidate_index for item in stage_a.ranked_candidates},
    )

    progress("Behavioral collapse: hashing cached validation behavior")
    signature_rows = cache.stage_c_rows[:64]
    signatures: dict[str, str] = {}
    for candidate in stage_b.ranked_candidates:
        member = stream.member(candidate.candidate_index)
        weights = member.profile.weights
        rows = []
        for row in signature_rows:
            score = sum(
                round(feature * int(getattr(weights, name)) / 100)
                for name, feature in zip(FEATURE_NAMES, row.features, strict=True)
            )
            rows.append(
                {
                    "case_id": row.position_hash,
                    "selected_series": row.chosen_series,
                    "clipped_score": max(-5_000, min(5_000, score)),
                }
            )
        signatures[candidate.effective_id] = behavioral_signature(rows)
    phenotypes = collapse_behavioral_phenotypes(
        stage_b.ranked_candidates, signatures
    )
    if len(phenotypes) < config.stage_c_keep:
        raise ValueError(
            "behavioral collapse left fewer than 512 independently ranked profiles"
        )

    stage_c_path = root / "stage-c-checkpoint.json"
    stage_c = _load_checkpoint(stage_c_path)
    if stage_c is None:
        progress(
            f"Stage C: validation rescoring {len(phenotypes):,} phenotypes"
        )
        stage_c = rank_candidate_stage(
            stream,
            phenotypes,
            stage_c_scorer,
            protocol_digest=protocol_digest,
            scorer_digest=stage_c_scorer.scorer_digest,
            stage="stage-c",
            keep_count=config.stage_c_keep,
            initial_disposition=stage_b.disposition,
        )
        write_json_atomic(stage_c.as_dict(), stage_c_path)
    _validate_complete_checkpoint(
        stage_c,
        protocol_digest=protocol_digest,
        stage="stage-c",
        scorer_digest=stage_c_scorer.scorer_digest,
        input_size=len(phenotypes),
        keep_count=config.stage_c_keep,
    )
    _validate_checkpoint_candidates(
        stage_c,
        stream=stream,
        scorer=stage_c_scorer,
        allowed_indices={item.candidate_index for item in phenotypes},
    )
    return stream, stage_b, stage_c


def _validate_bundle_envelope(
    candidate: RankedCandidate,
    bundle: Mapping[str, Any],
    *,
    protocol_digest: str,
    environment: TournamentEnvironment,
) -> None:
    if (
        bundle.get("format") != "spc-tactical-bundle-v1"
        or bundle.get("effective_id") != candidate.effective_id
        or bundle.get("profile_id") != candidate.profile_id
        or bundle.get("source_fingerprint") != environment.source_fingerprint
        or bundle.get("native_source_identity")
        != environment.native_source_identity
        or bundle.get("runtime_identity_digest")
        != environment.runtime_identity_digest
        or bundle.get("protocol_digest") != protocol_digest
    ):
        raise ValueError("persisted tactical bundle identity changed")
    _artifact_digest(
        bundle,
        domain="spc-tactical-bundle-v1\0",
        digest_field="artifact_digest",
    )


def _run_tactical_gates(
    root: Path,
    *,
    stream: PopulationStream,
    stage_b: FunnelCheckpoint,
    stage_c: FunnelCheckpoint,
    manifest: Mapping[str, Any],
    progress: Progress,
) -> dict[str, Any]:
    survivor_path = root / "survivors.json"
    if survivor_path.exists():
        result = _load_json(survivor_path)
        if (
            result.get("protocol_digest") != manifest.get("protocol_digest")
            or result.get("source_fingerprint") != ENGINE_SOURCE_FINGERPRINT
            or result.get("status") != "ready"
            or len(result.get("survivors", ())) != 64
        ):
            raise ValueError("persisted survivor set identity changed")
        return result

    environment = TournamentEnvironment.current(require_native=True)
    protocol_digest = str(manifest["protocol_digest"])
    stage_b_by_id = {
        candidate.effective_id: candidate for candidate in stage_b.ranked_candidates
    }
    ordered = list(stage_c.ranked_candidates)
    ordered.extend(
        candidate
        for candidate in stage_b.ranked_candidates
        if candidate.effective_id
        not in {item.effective_id for item in stage_c.ranked_candidates}
    )
    tactical_dir = root / "tactical-bundles"
    tactical_dir.mkdir(parents=True, exist_ok=True)
    bundles: dict[str, Mapping[str, Any]] = {}
    passed = 0
    limits = SearchLimits(
        depth_series=2,
        max_series_per_node=32,
        max_generation_positions=250_000,
        time_limit_seconds=None,
        collect_all_root_scores=False,
    )
    for ordinal, candidate in enumerate(ordered):
        canonical = stage_b_by_id.get(candidate.effective_id)
        if canonical is None or canonical.profile_id != candidate.profile_id:
            raise ValueError("tactical candidate is outside the frozen Stage-B cut")
        path = tactical_dir / f"{ordinal:05d}-{candidate.candidate_index:07d}.json"
        if path.exists():
            bundle = _load_json(path)
            _validate_bundle_envelope(
                candidate,
                bundle,
                protocol_digest=protocol_digest,
                environment=environment,
            )
        else:
            member = stream.member(candidate.candidate_index)
            progress(
                f"Tactical gate {ordinal + 1}: {candidate.effective_id}"
            )
            rules_gate = run_rules_tactical_gate(
                member.profile,
                search_depth=2,
                max_series_per_node=32,
                max_generation_positions=250_000,
            ).as_dict()
            human_gate = evaluate_human_refutation_gate(
                member.profile, limits=limits
            )
            bundle = stamp_tactical_bundle(
                candidate,
                protocol_digest=protocol_digest,
                native_source_identity=environment.native_source_identity,
                runtime_identity_digest=environment.runtime_identity_digest,
                rules_tactical_gate=rules_gate,
                human_refutation_gate=human_gate,
            )
            write_json_atomic(bundle, path)
        bundles[candidate.effective_id] = bundle
        try:
            validate_tactical_bundle(
                candidate,
                bundle,
                protocol_digest=protocol_digest,
                native_source_identity=environment.native_source_identity,
                runtime_identity_digest=environment.runtime_identity_digest,
            )
        except ValueError:
            continue
        passed += 1
        if passed == 64:
            break
    result = finalize_survivors(
        stage_c.ranked_candidates,
        stage_b.ranked_candidates,
        bundles,
        protocol_digest=protocol_digest,
        native_source_identity=environment.native_source_identity,
        runtime_identity_digest=environment.runtime_identity_digest,
    )
    if result.get("status") != "ready" or len(result.get("survivors", ())) != 64:
        raise ValueError("frozen tactical gates did not produce exactly 64 survivors")
    write_json_atomic(result, survivor_path)
    return result


def _materialize_catalog_and_plan(
    root: Path,
    *,
    stream: PopulationStream,
    baseline: EngineProfile,
    survivors: Mapping[str, Any],
    manifest: Mapping[str, Any],
    registry: Path,
    corpus_exclusion_artifact: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, EngineProfile]]:
    rows = survivors.get("survivors")
    if not isinstance(rows, list) or len(rows) != 64:
        raise ValueError("profile catalog requires exactly 64 survivors")
    ranked = tuple(RankedCandidate.from_dict(item) for item in rows)
    profiles: dict[str, EngineProfile] = {}
    challenger_records = []
    for candidate in ranked:
        member = stream.member(candidate.candidate_index)
        if (
            member.effective_id != candidate.effective_id
            or member.profile.profile_id != candidate.profile_id
        ):
            raise ValueError("survivor cannot be regenerated into its profile")
        profiles[candidate.effective_id] = member.profile
        challenger_records.append(
            {
                "candidate": candidate.as_dict(),
                "profile": member.profile.as_dict(),
            }
        )
    baseline_effective_id = effective_profile_id(
        baseline.weights, source_fingerprint=str(manifest["source_fingerprint"])
    )
    if baseline_effective_id in profiles:
        raise ValueError("challenger catalog includes the baseline control")
    profiles[baseline_effective_id] = baseline
    deterministic_catalog = {
        "format": CHALLENGER_CATALOG_FORMAT,
        "protocol_digest": manifest["protocol_digest"],
        "survivor_set_digest": survivors["survivor_set_digest"],
        "challenger_count": 64,
        "baseline": {
            "effective_id": baseline_effective_id,
            "profile": baseline.as_dict(),
        },
        "challengers": challenger_records,
        "claim": "proxy-ranked and tactically gated; no match-strength claim",
    }
    catalog = {
        **deterministic_catalog,
        "artifact_digest": canonical_digest(
            "spc-challenger-profile-catalog-v1\0", deterministic_catalog
        ),
    }
    catalog_path = root / "profile-catalog.json"
    if catalog_path.exists() and _load_json(catalog_path) != catalog:
        raise ValueError("persisted exact-64 profile catalog changed")
    write_json_atomic(catalog, catalog_path)

    plan_path = root / "tournament-plan.json"
    if plan_path.exists():
        plan = _load_json(plan_path)
        deterministic_plan = {
            key: value
            for key, value in plan.items()
            if key != "tournament_plan_digest"
        }
        if (
            plan.get("tournament_plan_digest")
            != canonical_digest("spc-tournament-plan-v1\0", deterministic_plan)
            or plan.get("protocol_digest") != manifest["protocol_digest"]
            or plan.get("source_fingerprint") != manifest["source_fingerprint"]
            or plan.get("survivors_in_validation_rank_order")
            != [candidate.as_dict() for candidate in ranked]
            or plan.get("baseline")
            != {
                "profile_id": baseline.profile_id,
                "effective_id": baseline_effective_id,
            }
        ):
            raise ValueError("persisted tournament plan identity changed")
        return plan, profiles

    reservation_key = (
        "challengers-"
        + str(manifest["protocol_digest"])[:16]
        + "-"
        + str(survivors["survivor_set_digest"])[:16]
    )
    preparation_path = root / "opening-preparation.json"
    if preparation_path.exists():
        opening_preparation = _load_json(preparation_path)
    else:
        opening_preparation = freeze_opening_suites_before_batch(
            registry,
            reservation_key=reservation_key,
            protocol_digest=str(manifest["protocol_digest"]),
            corpus_exclusion_artifact=corpus_exclusion_artifact,
            master_seed=TournamentFunnelConfig().master_seed,
        )
        write_json_atomic(opening_preparation, preparation_path)
    frozen_opening_suites = validate_opening_suite_preparation(
        opening_preparation,
        protocol_digest=str(manifest["protocol_digest"]),
        reservation_key=reservation_key,
        corpus_exclusion_digest=str(corpus_exclusion_artifact["artifact_digest"]),
    )
    batch = reserve_promotion_batch(
        registry,
        reservation_key=reservation_key,
        protocol_digest=str(manifest["protocol_digest"]),
        baseline_effective_id=baseline_effective_id,
        expected_registry_snapshot_digest=str(
            opening_preparation["registry_snapshot_digest"]
        ),
    )
    plan = build_tournament_plan(
        ranked,
        baseline,
        protocol_digest=str(manifest["protocol_digest"]),
        promotion_batch=batch,
        config=TournamentFunnelConfig(),
        frozen_opening_suites=frozen_opening_suites,
    )
    if plan_path.exists() and _load_json(plan_path) != plan:
        raise ValueError("persisted tournament plan changed")
    write_json_atomic(plan, plan_path)
    return plan, profiles


def run_challengers(
    run_root: str | Path,
    *,
    fullgame_store: str | Path,
    profile_path: str | Path,
    batch_registry: str | Path,
    checkpoint_every: int = DEFAULT_STAGE_A_CHECKPOINT_EVERY,
    progress: Progress = _quiet,
) -> dict[str, Any]:
    root = Path(run_root).expanduser().resolve()
    store, _profile_file, registry = _resolved_inputs(
        fullgame_store, profile_path, batch_registry
    )
    _sealed, cache, manifest, baseline = _load_preflight(
        root,
        fullgame_store=store,
        profile_path=profile_path,
        batch_registry=registry,
        verify_store=True,
    )
    stream, stage_b, stage_c = _run_cached_funnel(
        root,
        baseline=baseline,
        cache=cache,
        manifest=manifest,
        checkpoint_every=checkpoint_every,
        progress=progress,
    )
    survivors = _run_tactical_gates(
        root,
        stream=stream,
        stage_b=stage_b,
        stage_c=stage_c,
        manifest=manifest,
        progress=progress,
    )
    corpus_spec = VerifiedFullGameCorpusSpec(
        store_root=store,
        expected_corpus_id=cache.corpus_id,
        seed=TournamentFunnelConfig().master_seed,
        holdout_percent=20,
        max_games=None,
    )
    progress(
        "Tournament: re-verifying corpus exclusions and freezing all 13 "
        "opening suites before alpha-batch reservation"
    )
    corpus_exclusion_artifact = build_corpus_exclusion_artifact((corpus_spec,))
    plan, profiles = _materialize_catalog_and_plan(
        root,
        stream=stream,
        baseline=baseline,
        survivors=survivors,
        manifest=manifest,
        registry=registry,
        corpus_exclusion_artifact=corpus_exclusion_artifact,
    )
    progress(
        "Tournament: all primary and replacement opening lanes are "
        "collision-free and plan-frozen"
    )
    progress(
        "Tournament: resuming the first 10 calibration matchups with "
        "pre-seal whole-pair replacements"
    )
    with TournamentRunner(
        root / "tournament.sqlite3",
        tournament_plan=plan,
        profiles_by_effective_id=profiles,
        schedule="pending",
        verified_corpus_exclusion_artifact=corpus_exclusion_artifact,
        promotion_registry_path=registry,
        require_native=True,
    ) as runner:
        if runner.schedule == "pending":
            runner.run_matchups(plan["matchups"]["group"][:10])
            decision = runner.freeze_result_blind_expansion()
            progress(
                "Tournament: result-blind schedule frozen as "
                + str(decision["schedule"])
            )
        progress("Tournament: resuming all frozen group and knockout matches")
        result = runner.run_all()
        state_digest = runner.state_digest()
    completion = {
        **result,
        "driver_format": CHALLENGER_DRIVER_FORMAT,
        "corpus_id": cache.corpus_id,
        "profile_catalog_artifact_digest": _load_json(
            root / "profile-catalog.json"
        )["artifact_digest"],
        "runner_state_digest": state_digest,
        "champion_write": False,
        "promotion_decision": False,
    }
    write_json_atomic(completion, root / "tournament-complete.json")
    return completion


def challenger_status(
    run_root: str | Path,
    *,
    fullgame_store: str | Path,
    profile_path: str | Path,
    batch_registry: str | Path,
) -> dict[str, Any]:
    root = Path(run_root).expanduser().resolve()
    sealed, _cache, manifest, _profile = _load_preflight(
        root,
        fullgame_store=fullgame_store,
        profile_path=profile_path,
        batch_registry=batch_registry,
        verify_store=False,
    )
    checkpoints: dict[str, Any] = {}
    for stage in ("stage-a", "stage-b", "stage-c"):
        path = root / f"{stage}-checkpoint.json"
        checkpoint = _load_checkpoint(path)
        checkpoints[stage] = (
            {"present": False}
            if checkpoint is None
            else {
                "present": True,
                "complete": checkpoint.complete,
                "processed": checkpoint.next_input_offset,
                "input_size": checkpoint.input_size,
                "retained": len(checkpoint.ranked_candidates),
                "checkpoint_digest": checkpoint.as_dict()["checkpoint_digest"],
            }
        )
    survivor_path = root / "survivors.json"
    catalog_path = root / "profile-catalog.json"
    plan_path = root / "tournament-plan.json"
    complete_path = root / "tournament-complete.json"
    tournament_path = root / "tournament.sqlite3"
    persisted_matchups = 0
    schedule = "not-started"
    if tournament_path.exists():
        try:
            connection = sqlite3.connect(
                f"file:{tournament_path.as_posix()}?mode=ro", uri=True
            )
            try:
                row = connection.execute(
                    "select schedule from run_identity where singleton=1"
                ).fetchone()
                if row is not None:
                    schedule = str(row[0])
                persisted_matchups = int(
                    connection.execute("select count(*) from match_reports").fetchone()[0]
                )
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise ValueError(f"could not read tournament status: {error}") from error
    survivors = _load_json(survivor_path) if survivor_path.exists() else None
    catalog = _load_json(catalog_path) if catalog_path.exists() else None
    plan = _load_json(plan_path) if plan_path.exists() else None
    completion = _load_json(complete_path) if complete_path.exists() else None
    return {
        "format": "spc-challenger-driver-status-v1",
        "run_root": str(root),
        "identity_status": "current",
        "preflight_digest": sealed["preflight_digest"],
        "protocol_digest": manifest["protocol_digest"],
        "checkpoints": checkpoints,
        "tactical_survivors": (
            0 if survivors is None else len(survivors.get("survivors", ()))
        ),
        "exact_64_catalog": bool(
            catalog is not None
            and catalog.get("challenger_count") == 64
            and len(catalog.get("challengers", ())) == 64
        ),
        "plan_digest": (
            None if plan is None else plan.get("tournament_plan_digest")
        ),
        "tournament": {
            "schedule": schedule,
            "persisted_matchups": persisted_matchups,
            "complete": completion is not None,
        },
        "champion_write": False,
        "promotion_decision": False,
    }


def abandon_challenger_batch(
    run_root: str | Path,
    *,
    batch_registry: str | Path,
    reason: PromotionBatchAbandonmentReason,
) -> dict[str, Any]:
    """Seals one invalid challenger plan as consumed with no promotion."""

    root = Path(run_root).expanduser().resolve()
    registry = Path(batch_registry).expanduser().resolve()
    if registry != TRUSTED_PROMOTION_REGISTRY_PATH.resolve():
        raise ValueError(
            "production challengers require the trusted project batch registry: "
            f"{TRUSTED_PROMOTION_REGISTRY_PATH.resolve()}"
        )
    plan = _load_json(root / "tournament-plan.json")
    plan_digest = str(plan.get("tournament_plan_digest", ""))
    deterministic = {
        key: value for key, value in plan.items() if key != "tournament_plan_digest"
    }
    if plan_digest != canonical_digest("spc-tournament-plan-v1\0", deterministic):
        raise ValueError("challenger tournament plan digest mismatch")
    batch = plan.get("promotion_batch")
    if not isinstance(batch, Mapping):
        raise ValueError("challenger tournament plan lacks its promotion batch")
    decision = abandon_promotion_batch(
        registry,
        batch_index=int(batch["batch_index"]),
        expected_plan_digest=plan_digest,
        reason=reason,
    )
    return {
        "format": "spc-challenger-administrative-abandonment-v1",
        "run_root": str(root),
        "batch_index": int(batch["batch_index"]),
        "plan_digest": plan_digest,
        "reason": reason,
        "decision_digest": decision["decision_digest"],
        "alpha_batch_consumed": True,
        "promoted": False,
        "promotion_effect": "none",
    }
