from __future__ import annotations

import argparse
from contextlib import contextmanager
import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import numpy as np

from scottish_progressive.corpus_shards import (
    CorpusIdentity,
    CorpusStore,
    progressive_state_dedup_key,
)
from scottish_progressive.corpus_pipeline import (
    NativeGenerationContract,
    read_native_generation_contract,
)
from scottish_progressive.corpus_samples import NATIVE_BOUNDARY_SAMPLE_SCHEMA
from scottish_progressive import evaluation, series_mate
from scottish_progressive.league import promotion_decision
from scottish_progressive.fast_training import CachedFeatures, FEATURE_NAMES
from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT, ENGINE_VERSION
from scottish_progressive.native_corpus import (
    NativeCorpusConfig,
    NativeProfileSchedule,
    NativeRankPolicy,
    bind_native_profiles,
    semantic_config_digest,
)
from scottish_progressive.native_teacher import semantic_exclusion_sha256
from scottish_progressive.profiles import (
    EngineProfile,
    EvaluationWeights,
    baseline_profile,
    load_profile,
)
from scottish_progressive.rules import play_series
from scottish_progressive.teacher_value_features import (
    TEACHER_VALUE_FEATURE_NAMES,
    TEACHER_VALUE_FEATURE_SCHEMA,
    TeacherValueFeaturesV3,
    state_from_pfen,
)


CORPUS_SCHEMA = "spc-deep-teacher-corpus-v1"
CORPUS_METHOD = "balanced-native-trajectory-mixed-depth-policy-teacher-v1"
MODEL_SCHEMA = "spc-deep-teacher-linear-value-v1"
FIT_RECEIPT_SCHEMA = "spc-deep-teacher-fit-receipt-v1"
HOLDOUT_RECEIPT_SCHEMA = "spc-deep-teacher-holdout-receipt-v1"
SPLIT_ARTIFACT_SCHEMA = "spc-deep-teacher-split-artifact-v1"
SPLIT_INTEGRITY_SCHEMA = "spc-deep-teacher-split-integrity-v1"
HOLDOUT_CLAIM_SCHEMA = "spc-deep-teacher-holdout-claim-v2"
HOLDOUT_CLAIM_BINDING_SCHEMA = "spc-deep-teacher-holdout-claim-binding-v1"
HOLDOUT_PREPARATION_CLAIM_SCHEMA = "spc-deep-teacher-pair-preparation-claim-v2"
HOLDOUT_PREPARATION_SOURCE_SCHEMA = (
    "spc-deep-teacher-pair-preparation-source-binding-v2"
)
PAIR_PUBLICATION_SCHEMA = "spc-deep-teacher-pair-publication-v1"
PAIR_COMPLETION_REGISTRY_SCHEMA = "spc-deep-teacher-pair-completion-registry-v1"

_ACTIVE_SEALED_ALIAS_GUARD: tuple[Path, dict[str, int]] | None = None
PREREGISTRATION_RESERVATION_SCHEMA = "spc-cycle4-preregistration-reservation-v1"
DEVELOPMENT_PROVENANCE_SCHEMA = "spc-consumed-holdout-development-v1"
DEVELOPMENT_SOURCE_METADATA_SCHEMA = "spc-consumed-development-source-metadata-v1"
DEVELOPMENT_IMPORT_SCHEMA = "spc-consumed-development-import-v1"
POST_HOLDOUT_MATCH_SCHEMA = "spc-cycle4-post-holdout-match-contract-v1"
TEACHER_SEMANTIC_HASH_CONTRACT = (
    "canonical-json-sha256-without-runtime-created-or-raw-artifact-hashes-v1"
)
FIXED_POINT_SCALE = 1_000_000_000
MATE_SCORE = 1_000_000
DEFAULT_ADVERSE_PAIR_WEIGHT = 8.0
BASELINE_WEIGHTS = (100,) * len(FEATURE_NAMES)
DEVELOPMENT_PROFILE_WEIGHTS = (238, 188, 203, 223, 28, 164, 294)
NONROUTE_GROUPS = (
    "base7",
    "phase14",
    "cached19",
    "positional38",
    "direct44",
)
FEATURE_GROUPS = {
    "base7": tuple(range(7)),
    "phase14": tuple(range(14)),
    "cached19": tuple(range(19)),
    "positional38": tuple(range(38)),
    "direct44": tuple(range(44)),
    "all47": tuple(range(47)),
}
RIDGES = (0.0003, 0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0, 3.0)
QUARANTINED_HOLDOUT_CORPORA = {
    "c889078ca4e54780123b74c4b747cde2e74c2eb7bb12be61e786e3307aabef7f": (
        "evaluation-contaminated: an exploratory Stockfish correlation inspected "
        "14 holdout labels"
    ),
}
CYCLE4_PREREGISTRATION_SCHEMA = "spc-cycle4-one-shot-protocol-v1"
HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
FROZEN_IMPLEMENTATION_PATHS = (
    "scripts/generate_native_corpus.py",
    "scripts/build_native_teacher_corpus.py",
    "scripts/augment_native_teacher_semantics.py",
    "scripts/merge_native_teacher_tiers.py",
    "scripts/fit_deep_teacher_value.py",
    "src/scottish_progressive/native_teacher.py",
    "src/scottish_progressive/corpus_shards.py",
    "src/scottish_progressive/corpus_pipeline.py",
    "src/scottish_progressive/corpus_samples.py",
    "src/scottish_progressive/native_corpus.py",
    "src/scottish_progressive/native_corpus_training.py",
    "src/scottish_progressive/model.py",
    "src/scottish_progressive/evaluation.py",
    "src/scottish_progressive/rules.py",
    "src/scottish_progressive/series_mate.py",
    "src/scottish_progressive/fast_training.py",
    "src/scottish_progressive/teacher_value_features.py",
    "src/scottish_progressive/strength.py",
    "src/scottish_progressive/league.py",
    "src/scottish_progressive/cli.py",
    "src/scottish_progressive/search.py",
)
OPTIONAL_INTEGRATED_MATCH_IMPLEMENTATION_PATHS = (
    "src/scottish_progressive/deep_teacher_overlay.py",
)
DEPLOYED_GATE_FLOOR_COMMIT = "37937e0e4de98eed1a830fd11890b756a6b62d85"
REQUIRED_ZERO_INTERSECTIONS = [
    "train roots vs holdout roots",
    "train option finals vs holdout option finals",
    "train option finals vs holdout roots",
    "holdout option finals vs train roots",
]
REQUIRED_CANDIDATE_GATES = [
    "have strictly lower normalized regret than baseline and rejected development leader",
    "have strictly higher gap-weighted pairwise accuracy than baseline and rejected development leader",
    "select zero proven-adverse options",
    "not regress white, black, quiet-depth2, or tactical-depth3 regret versus baseline",
]
REQUIRED_ROUTE_ABLATION_GATES = [
    "strictly lower regret than primary_nonroute",
    "strictly improve pairwise accuracy over primary_nonroute",
    "select zero proven-adverse options",
]
REQUIRED_POST_HOLDOUT_GATES = [
    "tactical and mate safety",
    "color symmetry",
    "Python/native/WASM evaluator parity",
    "runtime overhead gate",
    "independent paired match or SPRT against deployed 37937e0 or newer champion",
]
SHARED_TRAJECTORY_FIELDS = {
    "max_attempt_series",
    "max_frontier_states",
    "candidate_count",
    "max_positions_per_series",
    "max_positions_per_game",
    "policy",
    "profile_schedule",
    "shard_size",
    "batch_size",
    "workers",
    "verify_payloads",
    "count_unique_states",
}

POST_HOLDOUT_MATCH_FIXED = {
    "schema": POST_HOLDOUT_MATCH_SCHEMA,
    "candidate_role": "primary_nonroute",
    "reference_role": "base_deployed_champion",
    "candidate_evaluator": {
        "schema": "spc-deep-teacher-match-overlay-v1",
        "loader_argument": "--candidate-value-model",
        "model_role": "primary_nonroute",
        "base_profile_argument": "baseline",
    },
    "opening_suite": {
        "source": "fresh-preregistered-progressive-opening-suite",
        "algorithm": "spc-neutral-seeded-openings-v1",
        "minimum_series": 3,
        "maximum_series": 6,
        "max_frontier_states": 32,
        "color_swapped_pairs": 50,
        "games": 100,
        "reuse_forbidden": True,
    },
    "search": {
        "depth_series": 2,
        "branch_cap": 32,
        "max_positions_per_search": 250_000,
        "max_positions_per_game": 5_000_000,
        "workers": 1,
        "workers_contract": "requested-single-worker-deterministic-execution",
    },
    "completion": {
        "technical_results_allowed": 0,
        "incomplete_results_allowed": 0,
    },
    "acceptance": {
        "decision_rule": "scottish_progressive.league.promotion_decision",
        "minimum_games": 100,
        "required_completed_pairs": 50,
        "minimum_pair_wins": 45,
        "maximum_pair_losses": 0,
        "pair_score_must_be_strictly_above": 0.5,
    },
}

PROMOTION_EVIDENCE_FIXED = {
    "status": "blocked-until-all-verifiers-implemented-and-passed",
    "required_receipt_schemas": [
        "spc-cycle4-tactical-mate-safety-receipt-v1",
        "spc-cycle4-color-symmetry-receipt-v1",
        "spc-cycle4-evaluator-parity-receipt-v1",
        "spc-cycle4-runtime-overhead-receipt-v1",
        "spc-cycle4-fixed-match-receipt-v1",
    ],
}

CYCLE4_TRAJECTORY_ATTEMPTS = {"train": 262_144, "sealed_holdout": 131_072}
CYCLE4_TEACHER_TIERS = {
    "quiet_depth2": {
        "target_roots": 3_072,
        "train_roots": 2_304,
        "holdout_roots": 768,
        "selection_mode": "quiet-nonterminal",
        "tactical_gate": "skipped-for-quiet-tier",
    },
    "tactical_depth3": {
        "target_roots": 1_024,
        "train_roots": 768,
        "holdout_roots": 256,
        "selection_mode": "tactical-low-complexity",
        "tactical_gate": "required",
    },
}
CYCLE3_CONSUMED_DEVELOPMENT_LABELS = 192
CYCLE3_CONSUMED_TRAIN_LABELS = 128
CYCLE3_CONSUMED_HOLDOUT_LABELS = 64
CYCLE3_CONSUMPTION_RESULT_RAW_SHA256 = (
    "d5c0a17b2d5a069a108a5f727aedf82a759eab725d68ee948d9914d8ef5009c7"
)
CYCLE3_CONSUMED_CORPUS_RAW_SHA256 = (
    "56d263edcb4117ba32af334eb2cbf5c2275bdfbc7704f7fe6218037c9a5d8c93"
)


def _match_profile_binding() -> dict[str, str]:
    profile = baseline_profile()
    return {
        "profile_id": profile.profile_id,
        "payload_sha256": hashlib.sha256(
            _canonical_json(profile.as_dict())
        ).hexdigest(),
    }


def _post_holdout_match_contract(seed: int, reference_commit: str) -> dict[str, Any]:
    base_profile = _match_profile_binding()
    return {
        "schema": POST_HOLDOUT_MATCH_FIXED["schema"],
        "candidate_role": POST_HOLDOUT_MATCH_FIXED["candidate_role"],
        "reference_role": POST_HOLDOUT_MATCH_FIXED["reference_role"],
        "candidate_evaluator": dict(
            POST_HOLDOUT_MATCH_FIXED["candidate_evaluator"]
        ),
        "seed": seed,
        "reference_commit": reference_commit,
        "candidate_base_profile": dict(base_profile),
        "reference_profile": dict(base_profile),
        "opening_suite": {
            **POST_HOLDOUT_MATCH_FIXED["opening_suite"],
            "seed": seed,
        },
        "search": dict(POST_HOLDOUT_MATCH_FIXED["search"]),
        "completion": dict(POST_HOLDOUT_MATCH_FIXED["completion"]),
        "acceptance": dict(POST_HOLDOUT_MATCH_FIXED["acceptance"]),
    }


def _seed_burn_rule(seed: int) -> str:
    return (
        f"Opening sealed holdout seed {seed} at any protected boundary permanently "
        "consumes it; every retry, refit, copied receipt, output path, and worktree "
        "is forbidden."
    )
TEACHER_FIELDS = {
    "selection_seed",
    "minimum_series",
    "maximum_series",
    "branch_cap",
    "max_work",
    "hard_negatives",
    "workers",
    "prior_receipt_cache_reuse",
    "tiers",
    "expected_merged_roots",
    "expected_merged_train_roots",
    "expected_merged_holdout_roots",
}
TEACHER_TIER_FIELDS = {
    "target_roots",
    "train_roots",
    "holdout_roots",
    "selection_mode",
    "tactical_gate",
}


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _pretty_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _semantic_teacher_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_teacher_value(item)
            for key, item in value.items()
            if str(key)
            not in {
                "runtime",
                "created_at",
                "started_at",
                "completed_at",
            }
            and not str(key).endswith("_raw_artifact_sha256")
            and str(key) != "raw_artifact_sha256"
        }
    if isinstance(value, list):
        return [_semantic_teacher_value(item) for item in value]
    if isinstance(value, tuple):
        return [_semantic_teacher_value(item) for item in value]
    return value


def _teacher_semantic_sha256(corpus: Mapping[str, Any]) -> str:
    semantic = _semantic_teacher_value(
        {key: value for key, value in corpus.items() if key != "corpus_id"}
    )
    if not isinstance(semantic, Mapping):
        raise TypeError("teacher semantic payload must remain an object")
    return hashlib.sha256(_canonical_json(semantic)).hexdigest()


def _sha256(path: Path) -> str:
    if _ACTIVE_SEALED_ALIAS_GUARD is not None:
        forbidden_path, forbidden_identity = _ACTIVE_SEALED_ALIAS_GUARD
        raw = _read_identity_isolated_bytes(
            path,
            forbidden_path=forbidden_path,
            label=f"hashed artifact {path}",
            forbidden_file_identity=forbidden_identity,
        )
        return hashlib.sha256(raw).hexdigest()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_hashes() -> dict[str, str]:
    paths = {
        Path(__file__).resolve(),
        Path(sys.modules[TeacherValueFeaturesV3.__module__].__file__).resolve(),
        Path(sys.modules[CachedFeatures.__module__].__file__).resolve(),
        Path(sys.modules[progressive_state_dedup_key.__module__].__file__).resolve(),
        Path(sys.modules[play_series.__module__].__file__).resolve(),
        Path(sys.modules[baseline_profile.__module__].__file__).resolve(),
    }
    return {str(path): _sha256(path) for path in sorted(paths)}


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _fsync_directory(path: Path) -> None:
    """Persist directory entries where the platform exposes directory fsync."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reserve_output_directory(path: Path, label: str) -> None:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        _fsync_directory(path.parent.parent)
    try:
        os.mkdir(path)
    except FileExistsError as error:
        raise FileExistsError(f"{label} output directory already exists") from error
    _fsync_directory(path.parent)


def _exclusive_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    conflict_message: str = (
        "this cycle/seed holdout has already been opened and is permanently "
        "consumed; another evaluation is forbidden"
    ),
) -> None:
    """Reserve *path* with O_EXCL and durably write its JSON payload.

    A crash after the exclusive create intentionally leaves a consumed marker.
    That is safer than permitting a second look at a one-shot holdout.
    """

    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        _fsync_directory(path.parent.parent)
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise FileExistsError(conflict_message) from error
    finally:
        if created:
            _fsync_directory(path.parent)


def _atomic_exclusive_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    conflict_message: str,
) -> None:
    """Publish complete JSON with create-if-absent semantics.

    Unlike a one-shot burn marker, a normal completion artifact must never be
    visible partially after a crash. A fully fsynced temporary file is linked
    into its final name atomically and without overwrite.
    """

    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        _fsync_directory(path.parent.parent)
    # Stage beside the final directory, not inside it. Strict protocol output
    # directories reject unexpected entries, and a process death before link
    # must not strand a helper temp that makes an exact retry impossible.
    staging_directory = (
        path.parent.parent if path.parent.parent != path.parent else path.parent
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.parent.name}-{path.name}.",
        suffix=".complete.tmp",
        dir=staging_directory,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as error:
            raise FileExistsError(conflict_message) from error
        _fsync_directory(path.parent)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _read_json_artifact(path: Path) -> tuple[dict[str, Any], str]:
    """Parse and hash one immutable byte snapshot of an artifact."""

    if _ACTIVE_SEALED_ALIAS_GUARD is not None:
        forbidden_path, forbidden_identity = _ACTIVE_SEALED_ALIAS_GUARD
        return _read_identity_isolated_json_artifact(
            path,
            forbidden_path=forbidden_path,
            label=f"JSON artifact {path}",
            forbidden_file_identity=forbidden_identity,
        )
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load JSON artifact {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact root must be an object: {path}")
    return value, hashlib.sha256(raw).hexdigest()


def _read_json_artifact_with_expected_raw(
    path: Path,
    expected_raw_sha256: str,
    *,
    label: str,
) -> tuple[dict[str, Any], str]:
    """Authenticate one byte snapshot before interpreting it as JSON."""

    if _ACTIVE_SEALED_ALIAS_GUARD is not None:
        forbidden_path, forbidden_identity = _ACTIVE_SEALED_ALIAS_GUARD
        raw = _read_identity_isolated_bytes(
            path,
            forbidden_path=forbidden_path,
            label=label,
            forbidden_file_identity=forbidden_identity,
        )
    else:
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise ValueError(f"could not load {label} {path}: {error}") from error
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    if raw_sha256 != expected_raw_sha256:
        raise ValueError(f"{label} raw bytes differ from the central reservation")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not parse authenticated {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"authenticated {label} root must be an object: {path}")
    return value, raw_sha256


def _file_identity(value: os.stat_result) -> dict[str, int]:
    return {"device": int(value.st_dev), "inode": int(value.st_ino)}


def _validated_file_identity(value: object, label: str) -> dict[str, int]:
    identity = _require_exact_fields(
        value, {"device", "inode"}, f"{label} file identity"
    )
    if any(type(identity[name]) is not int or identity[name] < 0 for name in identity):
        raise ValueError(f"{label} file identity is malformed")
    return {name: int(identity[name]) for name in ("device", "inode")}


@contextmanager
def _sealed_alias_guard(
    forbidden_path: Path,
    forbidden_identity: Mapping[str, Any],
) -> Iterator[None]:
    global _ACTIVE_SEALED_ALIAS_GUARD

    previous = _ACTIVE_SEALED_ALIAS_GUARD
    _ACTIVE_SEALED_ALIAS_GUARD = (
        forbidden_path,
        {name: int(forbidden_identity[name]) for name in ("device", "inode")},
    )
    try:
        yield
    finally:
        _ACTIVE_SEALED_ALIAS_GUARD = previous


def _read_identity_isolated_bytes(
    path: Path,
    *,
    forbidden_path: Path,
    label: str,
    expected_file_identity: Mapping[str, Any] | None = None,
    forbidden_file_identity: Mapping[str, Any] | None = None,
) -> bytes:
    """Read one path only after proving it is not the sealed artifact.

    The first same-file check rejects an existing hardlink before open. The
    descriptor check catches a swap between stat and open, and all bytes are
    then hashed and parsed from that one verified descriptor snapshot.
    """

    try:
        path_before = os.stat(path)
        forbidden_before = os.stat(forbidden_path)
    except OSError as error:
        raise ValueError(f"could not stat {label}: {error}") from error
    if os.path.samestat(path_before, forbidden_before):
        raise ValueError(f"{label} aliases the sealed holdout artifact")
    if expected_file_identity is not None and _file_identity(path_before) != dict(
        expected_file_identity
    ):
        raise ValueError(f"{label} identity differs from central completion")
    if forbidden_file_identity is not None and _file_identity(
        forbidden_before
    ) != dict(forbidden_file_identity):
        raise ValueError("sealed holdout identity differs from central completion")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"could not open {label}: {error}") from error
    try:
        descriptor_before = os.fstat(descriptor)
        forbidden_during = os.stat(forbidden_path)
        if os.path.samestat(descriptor_before, forbidden_during):
            raise ValueError(f"{label} aliases the sealed holdout artifact")
        if expected_file_identity is not None and _file_identity(
            descriptor_before
        ) != dict(expected_file_identity):
            raise ValueError(f"{label} identity differs from central completion")
        if forbidden_file_identity is not None:
            if _file_identity(descriptor_before) == dict(forbidden_file_identity):
                raise ValueError(f"{label} aliases the sealed holdout artifact")
            if _file_identity(forbidden_during) != dict(forbidden_file_identity):
                raise ValueError(
                    "sealed holdout identity differs from central completion"
                )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        descriptor_after = os.fstat(descriptor)
    except OSError as error:
        raise ValueError(f"could not read {label}: {error}") from error
    finally:
        os.close(descriptor)
    try:
        path_after = os.stat(path)
        forbidden_after = os.stat(forbidden_path)
    except OSError as error:
        raise ValueError(f"could not restat {label}: {error}") from error
    if (
        not os.path.samestat(path_before, descriptor_before)
        or not os.path.samestat(descriptor_before, descriptor_after)
        or not os.path.samestat(descriptor_after, path_after)
        or os.path.samestat(descriptor_after, forbidden_after)
        or (
            expected_file_identity is not None
            and _file_identity(descriptor_after) != dict(expected_file_identity)
        )
        or (
            forbidden_file_identity is not None
            and _file_identity(forbidden_after) != dict(forbidden_file_identity)
        )
        or descriptor_before.st_size != descriptor_after.st_size
        or descriptor_before.st_mtime_ns != descriptor_after.st_mtime_ns
    ):
        raise ValueError(f"{label} identity changed during its verified read")
    return b"".join(chunks)


def _read_stable_file_snapshot(
    path: Path,
    *,
    label: str,
) -> tuple[bytes, dict[str, int]]:
    """Bind bytes and file identity from one stable descriptor snapshot."""

    try:
        path_before = os.stat(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError as error:
        raise ValueError(f"could not open stable {label}: {error}") from error
    try:
        descriptor_before = os.fstat(descriptor)
        if not os.path.samestat(path_before, descriptor_before):
            raise ValueError(f"{label} changed before stable snapshot open")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        descriptor_after = os.fstat(descriptor)
    except OSError as error:
        raise ValueError(f"could not read stable {label}: {error}") from error
    finally:
        os.close(descriptor)
    try:
        path_after = os.stat(path)
    except OSError as error:
        raise ValueError(f"could not close stable {label}: {error}") from error
    if (
        not os.path.samestat(descriptor_before, descriptor_after)
        or not os.path.samestat(descriptor_after, path_after)
        or descriptor_before.st_size != descriptor_after.st_size
        or descriptor_before.st_mtime_ns != descriptor_after.st_mtime_ns
    ):
        raise ValueError(f"{label} changed during stable snapshot")
    return b"".join(chunks), _file_identity(descriptor_after)


def _read_identity_isolated_json_artifact(
    path: Path,
    *,
    forbidden_path: Path,
    label: str,
    expected_file_identity: Mapping[str, Any] | None = None,
    forbidden_file_identity: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    raw = _read_identity_isolated_bytes(
        path,
        forbidden_path=forbidden_path,
        label=label,
        expected_file_identity=expected_file_identity,
        forbidden_file_identity=forbidden_file_identity,
    )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not parse {label}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} JSON root must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def _read_profile_artifact(path: Path) -> tuple[EngineProfile, str]:
    payload, raw_sha256 = _read_json_artifact(path)
    profile_payload = payload.get("profile", payload)
    if not isinstance(profile_payload, Mapping):
        raise ValueError(f"engine profile envelope is malformed: {path}")
    return EngineProfile.from_dict(profile_payload), raw_sha256


def _reject_quarantined_holdout(corpus: Mapping[str, Any]) -> None:
    generation = corpus.get("generation")
    if not isinstance(generation, Mapping):
        return
    holdout_sha256 = generation.get("holdout_corpus_sha256")
    if not isinstance(holdout_sha256, str):
        return
    reason = QUARANTINED_HOLDOUT_CORPORA.get(holdout_sha256)
    if reason is not None:
        raise ValueError(
            "teacher corpus holdout is permanently quarantined; "
            f"sha256={holdout_sha256}; reason={reason}"
        )


@dataclass(frozen=True, slots=True)
class Preregistration:
    path: Path
    sha256: str
    schema: str
    expected_train_labels: int
    expected_holdout_labels: int
    manifest: Mapping[str, Any]


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _repository_file(relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute():
        raise ValueError(f"preregistration repository path must be relative: {relative}")
    root = _repository_root()
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(
            f"preregistration repository path escapes the checkout: {relative}"
        ) from error
    return resolved


def _git_common_dir() -> Path:
    """Resolve Git's repository-wide state directory across linked worktrees."""

    marker = _repository_root() / ".git"
    if marker.is_dir():
        git_dir = marker.resolve()
    elif marker.is_file():
        try:
            line = marker.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ValueError("could not read the Git worktree marker") from error
        prefix = "gitdir: "
        if not line.startswith(prefix) or not line[len(prefix) :].strip():
            raise ValueError("Git worktree marker is malformed")
        candidate = Path(line[len(prefix) :].strip())
        git_dir = (
            candidate if candidate.is_absolute() else marker.parent / candidate
        ).resolve()
    else:
        raise ValueError("one-shot holdout claims require a Git checkout")
    common_marker = git_dir / "commondir"
    if not common_marker.exists():
        return git_dir
    try:
        common_value = common_marker.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError("could not read the Git common-dir marker") from error
    if not common_value:
        raise ValueError("Git common-dir marker is empty")
    candidate = Path(common_value)
    common_dir = (
        candidate if candidate.is_absolute() else git_dir / candidate
    ).resolve()
    if not common_dir.is_dir():
        raise ValueError("Git common-dir marker does not name a directory")
    return common_dir


def _verify_preregistration_commit_labels(
    base_deployed_commit: str,
    integrated_engine_source_commit: str,
) -> None:
    root = _repository_root()

    def resolved_commit(value: str, label: str) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{value}^{{commit}}"],
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
        )
        resolved = result.stdout.strip()
        if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", resolved) is None:
            raise ValueError(f"{label} commit does not resolve in this repository")
        return resolved

    base = resolved_commit(base_deployed_commit, "base deployed")
    floor = resolved_commit(DEPLOYED_GATE_FLOOR_COMMIT, "deployed gate floor")
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", floor, base],
        cwd=root,
        check=False,
    )
    if ancestry.returncode != 0:
        raise ValueError(
            "base deployed commit predates the minimum independently certified champion"
        )
    integrated = resolved_commit(
        integrated_engine_source_commit, "integrated engine source"
    )
    head = resolved_commit("HEAD", "checkout HEAD")
    if integrated != head:
        raise ValueError("integrated engine source commit must equal checkout HEAD")
    for relative in _frozen_implementation_paths():
        committed = subprocess.run(
            ["git", "cat-file", "-e", f"{integrated}:{relative}"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative],
            cwd=root,
            capture_output=True,
            check=False,
        )
        diff = subprocess.run(
            ["git", "diff", "--quiet", integrated, "--", relative],
            cwd=root,
            check=False,
        )
        if (
            committed.returncode != 0
            or tracked.returncode != 0
            or not _repository_file(relative).is_file()
            or diff.returncode != 0
        ):
            raise ValueError(
                "frozen implementation file is not the exact integrated-commit blob: "
                + relative
            )


def _holdout_claim_path_for_seed(seed: int) -> Path:
    seed = _exact_positive_int(seed, "sealed holdout seed")
    filename = f"spc-one-shot-holdout-seed-{seed}.json"
    return (_git_common_dir() / "spc-one-shot-holdout-claims" / filename).resolve()


def _holdout_claim_path(preregistration: Preregistration) -> Path:
    """Return the cycle/seed claim marker shared by every fit/output path."""

    holdout = preregistration.manifest["trajectory_corpora"]["sealed_holdout"]
    return _holdout_claim_path_for_seed(holdout.get("seed"))


def _cycle_preregistration_reservation_path() -> Path:
    return (
        _git_common_dir()
        / "spc-one-shot-holdout-claims"
        / "spc-cycle4-preregistration.json"
    ).resolve()


def _seed_preregistration_reservation_path(seed: int) -> Path:
    return (
        _git_common_dir()
        / "spc-one-shot-holdout-claims"
        / f"spc-one-shot-holdout-seed-{seed}-preregistration.json"
    ).resolve()


def _preregistration_reservation_payload(
    preregistration: Preregistration,
) -> dict[str, Any]:
    manifest = preregistration.manifest
    trajectory = manifest["trajectory_corpora"]
    holdout_seed = _exact_positive_int(
        trajectory["sealed_holdout"].get("seed"), "sealed holdout seed"
    )
    return {
        "schema": PREREGISTRATION_RESERVATION_SCHEMA,
        "cycle_schema": preregistration.schema,
        "manifest_path": str(preregistration.path),
        "manifest_raw_artifact_sha256": preregistration.sha256,
        "train_seed": trajectory["train"]["seed"],
        "holdout_seed": holdout_seed,
        "teacher_selection_seed": manifest["teacher"]["selection_seed"],
        "post_holdout_match_seed": manifest["post_holdout_match"]["seed"],
    }


def _reserve_preregistration(preregistration: Preregistration) -> tuple[Path, Path]:
    payload = _preregistration_reservation_payload(preregistration)
    cycle_path = _cycle_preregistration_reservation_path()
    seed_path = _seed_preregistration_reservation_path(int(payload["holdout_seed"]))
    for path, label in (
        (cycle_path, "cycle-4 preregistration"),
        (seed_path, "holdout-seed preregistration"),
    ):
        if path.exists():
            existing, _ = _read_json_artifact(path)
            if existing != payload:
                raise FileExistsError(
                    f"{label} is already reserved to a different manifest"
                )
        else:
            _atomic_exclusive_json(
                path,
                payload,
                conflict_message=f"{label} was concurrently reserved",
            )
    return cycle_path, seed_path


def _validate_preregistration_reservation(
    preregistration: Preregistration,
) -> None:
    expected = _preregistration_reservation_payload(preregistration)
    paths = (
        _cycle_preregistration_reservation_path(),
        _seed_preregistration_reservation_path(int(expected["holdout_seed"])),
    )
    for path in paths:
        reservation, _ = _read_json_artifact(path)
        if reservation != expected:
            raise ValueError(
                "preregistration reservation differs from the supplied manifest"
            )


def _holdout_preparation_claim_path_for_seed(seed: int) -> Path:
    seed = _exact_positive_int(seed, "sealed holdout seed")
    filename = f"spc-one-shot-holdout-seed-{seed}-pair-preparation.json"
    return (_git_common_dir() / "spc-one-shot-holdout-claims" / filename).resolve()


def _holdout_preparation_claim_path(preregistration: Preregistration) -> Path:
    """Return the repository-wide, one-shot artifact-preparation marker."""

    holdout = preregistration.manifest["trajectory_corpora"]["sealed_holdout"]
    return _holdout_preparation_claim_path_for_seed(holdout.get("seed"))


def _holdout_preparation_source_path_for_seed(seed: int) -> Path:
    seed = _exact_positive_int(seed, "sealed holdout seed")
    filename = f"spc-one-shot-holdout-seed-{seed}-pair-source.json"
    return (_git_common_dir() / "spc-one-shot-holdout-claims" / filename).resolve()


def _holdout_preparation_source_path(preregistration: Preregistration) -> Path:
    holdout = preregistration.manifest["trajectory_corpora"]["sealed_holdout"]
    return _holdout_preparation_source_path_for_seed(holdout.get("seed"))


def _pair_completion_registry_path_from_claim(claim_path: Path) -> Path:
    return claim_path.with_name(f"{claim_path.stem}-completion.json").resolve()


def _pair_completion_registry_path_for_seed(seed: int) -> Path:
    filename = f"spc-one-shot-holdout-seed-{seed}-pair-preparation-completion.json"
    return (_git_common_dir() / "spc-one-shot-holdout-claims" / filename).resolve()


def _pair_completion_registry_path(preregistration: Preregistration) -> Path:
    return _pair_completion_registry_path_from_claim(
        _holdout_preparation_claim_path(preregistration)
    )


def _protocol_stage_lock_path() -> Path:
    return (
        _git_common_dir()
        / "spc-one-shot-holdout-claims"
        / "spc-cycle4-upstream-stage.lock"
    ).resolve()


@contextmanager
def _protocol_stage_lock(operation: str, *, exclusive: bool):
    """Hold the repository-wide producer/pair exclusion for one whole command.

    The lock is an OS advisory byte lock, not a mkdir lease, so process death
    releases it automatically. Every cooperating upstream writer and the pair
    publisher takes this same lock exclusively; producers hold a shared lock so
    independent train/holdout work can still run in parallel. This closes the
    check/read race between observing no terminal marker and later opening an
    input artifact.
    """

    path = _protocol_stage_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    locked = False
    windows_lock: tuple[Any, Any] | None = None
    try:
        if os.fstat(descriptor).st_size < 1:
            os.ftruncate(descriptor, 1)
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import ctypes
                import msvcrt
                from ctypes import wintypes

                class Overlapped(ctypes.Structure):
                    _fields_ = (
                        ("Internal", ctypes.c_void_p),
                        ("InternalHigh", ctypes.c_void_p),
                        ("Offset", wintypes.DWORD),
                        ("OffsetHigh", wintypes.DWORD),
                        ("hEvent", wintypes.HANDLE),
                    )

                overlapped = Overlapped()
                handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
                lock_file_ex = ctypes.windll.kernel32.LockFileEx
                lock_file_ex.argtypes = (
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(Overlapped),
                )
                lock_file_ex.restype = wintypes.BOOL
                lock_flags = 0x00000001 | (0x00000002 if exclusive else 0)
                if not lock_file_ex(
                    handle,
                    lock_flags,
                    0,
                    1,
                    0,
                    ctypes.byref(overlapped),
                ):
                    raise ctypes.WinError()
                windows_lock = (handle, overlapped)
            else:
                import fcntl

                mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            raise RuntimeError(
                f"cycle-4 protocol stage is active; {operation} cannot run concurrently"
            ) from error
        yield path
    finally:
        if locked:
            if os.name == "nt":
                import ctypes
                import msvcrt
                from ctypes import wintypes

                if windows_lock is None:
                    raise RuntimeError("Windows protocol stage lock state is missing")
                handle, overlapped = windows_lock
                unlock_file_ex = ctypes.windll.kernel32.UnlockFileEx
                unlock_file_ex.argtypes = (
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(type(overlapped)),
                )
                unlock_file_ex.restype = wintypes.BOOL
                if not unlock_file_ex(
                    handle, 0, 1, 0, ctypes.byref(overlapped)
                ):
                    raise ctypes.WinError()
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _terminal_pair_state_paths_for_seed(seed: int) -> tuple[Path, ...]:
    seed = _exact_positive_int(seed, "sealed holdout seed")
    return (
        _holdout_preparation_claim_path_for_seed(seed),
        _holdout_preparation_source_path_for_seed(seed),
        _pair_completion_registry_path_for_seed(seed),
        _holdout_claim_path_for_seed(seed),
    )


def _reject_preregistration_after_pair_start(supplied_holdout_seed: int) -> None:
    """Fence manifest rebuilds before any caller-controlled artifact is opened."""

    seeds = {_exact_positive_int(supplied_holdout_seed, "holdout seed")}
    cycle_reservation_path = _cycle_preregistration_reservation_path()
    if cycle_reservation_path.exists():
        reservation, _ = _read_json_artifact(cycle_reservation_path)
        if reservation.get("schema") != PREREGISTRATION_RESERVATION_SCHEMA:
            raise ValueError("cycle preregistration reservation is malformed")
        seeds.add(
            _exact_positive_int(
                reservation.get("holdout_seed"), "reserved holdout seed"
            )
        )
    if any(
        path.exists()
        for seed in seeds
        for path in _terminal_pair_state_paths_for_seed(seed)
    ):
        raise FileExistsError(
            "cycle-4 holdout preparation has started; preregistration is permanently "
            "closed before any development/profile/runtime input is opened"
        )


def _protocol_registry_paths(
    preregistration: Preregistration,
) -> dict[str, Path]:
    holdout_seed = _exact_positive_int(
        preregistration.manifest["trajectory_corpora"]["sealed_holdout"].get(
            "seed"
        ),
        "sealed holdout seed",
    )
    return {
        "preregistration manifest": preregistration.path,
        "holdout claim": _holdout_claim_path(preregistration),
        "preparation claim": _holdout_preparation_claim_path(preregistration),
        "preparation source binding": _holdout_preparation_source_path(
            preregistration
        ),
        "pair completion registry": _pair_completion_registry_path(
            preregistration
        ),
        "upstream stage lock": _protocol_stage_lock_path(),
        "cycle reservation": _cycle_preregistration_reservation_path(),
        "seed reservation": _seed_preregistration_reservation_path(holdout_seed),
    }


def _require_protocol_registry_isolation(
    preregistration: Preregistration,
    data_paths: Mapping[str, Path],
    *,
    label: str,
) -> None:
    """Keep every producer/consumer path outside the shared claim namespace."""

    for name, path in data_paths.items():
        _require_lexical_absolute_input(path, f"{label} {name}")
    for data_name, data_path in data_paths.items():
        for registry_name, registry_path in _protocol_registry_paths(
            preregistration
        ).items():
            if (
                data_path == registry_path
                or data_path in registry_path.parents
                or registry_path in data_path.parents
            ):
                raise ValueError(
                    f"{label} {data_name} overlaps protocol registry path "
                    f"{registry_name}"
                )


def _claim_holdout_preparation(
    preregistration: Preregistration,
    *,
    requested_source: Path,
    requested_train_source: Path | None = None,
    requested_output: Path,
    operation: str,
    allow_identical_resume: bool = False,
) -> Path:
    claim_path = _holdout_preparation_claim_path(preregistration)
    holdout_seed = preregistration.manifest["trajectory_corpora"][
        "sealed_holdout"
    ]["seed"]
    claim = {
            "schema": HOLDOUT_PREPARATION_CLAIM_SCHEMA,
            "operation": operation,
            "preregistration_schema": preregistration.schema,
            "preregistration_sha256": preregistration.sha256,
            "holdout_seed": holdout_seed,
            "requested_train_source": (
                None if requested_train_source is None else str(requested_train_source)
            ),
            "requested_sealed_holdout_source": str(requested_source),
            "requested_output": str(requested_output),
            "burn_rule": _seed_burn_rule(int(holdout_seed)),
    }
    if claim_path.exists():
        if not allow_identical_resume:
            raise FileExistsError(
                "this cycle/seed holdout has already been opened for artifact preparation; "
                "retrying from any output path or worktree is forbidden"
            )
        existing, _ = _read_json_artifact(claim_path)
        if existing != claim:
            raise FileExistsError(
                "this cycle/seed holdout preparation belongs to a different request; "
                "retrying from any output path or worktree is forbidden"
            )
    else:
        try:
            _atomic_exclusive_json(
                claim_path,
                claim,
                conflict_message=(
                    "this cycle/seed holdout has already been opened for artifact "
                    "preparation; retrying from any output path or worktree is forbidden"
                ),
            )
        except FileExistsError:
            if not allow_identical_resume:
                raise
            existing, _ = _read_json_artifact(claim_path)
            if existing != claim:
                raise FileExistsError(
                    "this cycle/seed holdout preparation belongs to a different request; "
                    "retrying from any output path or worktree is forbidden"
                )
    return claim_path


def _bind_holdout_preparation_source(
    preregistration: Preregistration,
    *,
    preparation_claim_path: Path,
    operation: str,
    sealed_source_path: Path,
    sealed_source_raw_sha256: str,
    train_source_path: Path | None = None,
    train_source_raw_sha256: str | None = None,
    requested_output: Path,
    allow_identical_resume: bool = False,
) -> Path:
    """Durably bind the claimed path to the one byte snapshot opened afterward."""

    if HEX_SHA256.fullmatch(sealed_source_raw_sha256) is None:
        raise ValueError("sealed preparation source raw SHA-256 is malformed")
    if (train_source_path is None) != (train_source_raw_sha256 is None):
        raise ValueError("train preparation source path and raw SHA must be paired")
    if (
        train_source_raw_sha256 is not None
        and HEX_SHA256.fullmatch(train_source_raw_sha256) is None
    ):
        raise ValueError("train preparation source raw SHA-256 is malformed")
    claim, claim_raw_sha256 = _read_json_artifact(preparation_claim_path)
    if (
        claim.get("schema") != HOLDOUT_PREPARATION_CLAIM_SCHEMA
        or claim.get("operation") != operation
        or claim.get("preregistration_schema") != preregistration.schema
        or claim.get("preregistration_sha256") != preregistration.sha256
        or claim.get("requested_train_source")
        != (None if train_source_path is None else str(train_source_path))
        or claim.get("requested_sealed_holdout_source") != str(sealed_source_path)
        or claim.get("requested_output") != str(requested_output)
    ):
        raise ValueError("holdout preparation claim differs before source binding")
    binding_path = _holdout_preparation_source_path(preregistration)
    binding = {
            "schema": HOLDOUT_PREPARATION_SOURCE_SCHEMA,
            "operation": operation,
            "preregistration_schema": preregistration.schema,
            "preregistration_sha256": preregistration.sha256,
            "holdout_seed": preregistration.manifest["trajectory_corpora"][
                "sealed_holdout"
            ]["seed"],
            "preparation_claim": {
                "path": str(preparation_claim_path),
                "raw_artifact_sha256": claim_raw_sha256,
            },
            "train_source": (
                None
                if train_source_path is None
                else {
                    "path": str(train_source_path),
                    "raw_artifact_sha256": train_source_raw_sha256,
                }
            ),
            "sealed_holdout_source": {
                "path": str(sealed_source_path),
                "raw_artifact_sha256": sealed_source_raw_sha256,
            },
            "requested_output": str(requested_output),
            "burn_rule": _seed_burn_rule(
                int(
                    preregistration.manifest["trajectory_corpora"][
                        "sealed_holdout"
                    ]["seed"]
                )
            ),
    }
    if binding_path.exists():
        if not allow_identical_resume:
            raise FileExistsError(
                "this cycle/seed holdout already has an immutable preparation-source "
                "binding; rebinding different bytes is forbidden"
            )
        existing, _ = _read_json_artifact(binding_path)
        if existing != binding:
            raise FileExistsError(
                "this cycle/seed holdout preparation-source binding differs; "
                "rebinding different bytes is forbidden"
            )
    else:
        try:
            _atomic_exclusive_json(
                binding_path,
                binding,
                conflict_message=(
                    "this cycle/seed holdout already has an immutable preparation-source "
                    "binding; rebinding different bytes is forbidden"
                ),
            )
        except FileExistsError:
            if not allow_identical_resume:
                raise
            existing, _ = _read_json_artifact(binding_path)
            if existing != binding:
                raise FileExistsError(
                    "this cycle/seed holdout preparation-source binding differs; "
                    "rebinding different bytes is forbidden"
                )
    return binding_path


def _protocol_contract_hashes() -> dict[str, str]:
    feature_contract = {
        "feature_schema": TEACHER_VALUE_FEATURE_SCHEMA,
        "feature_names": list(TEACHER_VALUE_FEATURE_NAMES),
        "feature_groups": {
            name: list(indices) for name, indices in sorted(FEATURE_GROUPS.items())
        },
        "base_feature_names": list(FEATURE_NAMES),
        "expensive_two_move_route_indices": [44, 45, 46],
    }
    selection_contract = {
        "nonroute_groups": list(NONROUTE_GROUPS),
        "ridges": list(RIDGES),
        "default_adverse_pair_weight": DEFAULT_ADVERSE_PAIR_WEIGHT,
        "objective": [
            "chosen_avoidable_proven_adverse",
            "normalized_regret",
            "negative_gap_weighted_pairwise_accuracy",
            "negative_agreement",
        ],
        "cross_validation": "five-fold-semantic-component-disjoint",
    }
    holdout_gate_ast = ast.dump(
        ast.parse(inspect.getsource(_holdout_gate)),
        annotate_fields=True,
        include_attributes=False,
    ).encode("utf-8")
    promotion_ast = ast.dump(
        ast.parse(inspect.getsource(promotion_decision)),
        annotate_fields=True,
        include_attributes=False,
    ).encode("utf-8")
    return {
        "feature_contract_sha256": hashlib.sha256(
            _canonical_json(feature_contract)
        ).hexdigest(),
        "selection_contract_sha256": hashlib.sha256(
            _canonical_json(selection_contract)
        ).hexdigest(),
        "holdout_gate_ast_sha256": hashlib.sha256(holdout_gate_ast).hexdigest(),
        "post_holdout_match_verifier_ast_sha256": hashlib.sha256(
            promotion_ast
        ).hexdigest(),
    }


def _current_frozen_implementation() -> dict[str, str]:
    frozen_paths = _frozen_implementation_paths()
    return {
        **{
            relative: _sha256(_repository_file(relative))
            for relative in frozen_paths
        },
        **_protocol_contract_hashes(),
    }


def _frozen_implementation_paths() -> list[str]:
    """Bind every tracked package source plus the protocol entrypoints/build files."""

    root = _repository_root()
    result = subprocess.run(
        ["git", "ls-files", "--", "src/scottish_progressive"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError("could not enumerate tracked engine implementation files")
    package_sources = {
        line.strip().replace("\\", "/")
        for line in result.stdout.splitlines()
        if Path(line.strip()).suffix.lower() in {".py", ".cpp", ".hpp", ".h", ".c"}
    }
    paths = {
        *FROZEN_IMPLEMENTATION_PATHS,
        *package_sources,
        "pyproject.toml",
        "setup.py",
    }
    paths.update(
        relative
        for relative in OPTIONAL_INTEGRATED_MATCH_IMPLEMENTATION_PATHS
        if _repository_file(relative).is_file()
    )
    return sorted(paths)


def _validate_preregistered_runtime(runtime: object) -> None:
    runtime = _require_exact_fields(
        runtime,
        {
            "platform",
            "python",
            "python_abi",
            "compiler",
            "numpy",
            "numeric_runtime_sha256",
            "native_eval_binary_sha256",
            "native_mate_binary_sha256",
        },
        "runtime contract",
    )
    actual = _runtime_contract()
    drifted = [name for name, value in actual.items() if runtime.get(name) != value]
    if drifted:
        raise ValueError(
            "preregistration runtime drifted exactly: " + ", ".join(drifted)
        )


def _exact_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"preregistration {label} must be a positive integer")
    return value


def _validate_tactical_gate_evidence(
    quality: Mapping[str, Any],
    preregistered_mode: str,
    label: str,
) -> None:
    gate = quality.get("tactical_gate")
    if preregistered_mode == "skipped-for-quiet-tier":
        if gate != {"passed": None, "checks": [], "skipped": True}:
            raise ValueError(f"{label} tactical gate skip evidence drifted")
        return
    if preregistered_mode != "required" or not isinstance(gate, Mapping):
        raise ValueError(f"{label} tactical gate evidence is malformed")
    checks = gate.get("checks")
    if (
        gate.get("passed") is not True
        or not isinstance(checks, list)
        or not checks
        or any(
            not isinstance(check, Mapping) or check.get("passed") is not True
            for check in checks
        )
        or quality.get("tactical_failures") != []
    ):
        raise ValueError(f"{label} required tactical gate did not pass exactly")


def _require_exact_fields(
    value: object,
    expected: set[str],
    label: str,
    *,
    optional: set[str] | None = None,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"preregistration {label} is malformed")
    optional = optional or set()
    supplied = set(value)
    if not expected.issubset(supplied) or not supplied.issubset(expected | optional):
        raise ValueError(
            f"preregistration {label} fields differ; "
            f"missing={sorted(expected - supplied)}, "
            f"unexpected={sorted(supplied - expected - optional)}"
        )
    return value


def _validate_declared_artifact_source(value: object, label: str) -> None:
    if value is None:
        return
    source = _require_exact_fields(
        value,
        {
            "path",
            "corpus_id",
            "semantic_sha256",
            "raw_artifact_sha256",
            "label_count",
            "semantic_exclusion_sha256",
            "consumption_evidence",
        },
        f"{label} artifact source",
    )
    if (
        not isinstance(source["path"], str)
        or not Path(source["path"]).is_absolute()
        or os.path.normpath(source["path"]) != source["path"]
        or str(Path(source["path"]).expanduser().resolve()) != source["path"]
    ):
        raise ValueError(f"preregistration {label} artifact source path is malformed")
    if not isinstance(source["corpus_id"], str) or not source["corpus_id"]:
        raise ValueError(f"preregistration {label} artifact source ID is malformed")
    for name in (
        "semantic_sha256",
        "raw_artifact_sha256",
        "semantic_exclusion_sha256",
    ):
        digest = source[name]
        if not isinstance(digest, str) or HEX_SHA256.fullmatch(digest) is None:
            raise ValueError(
                f"preregistration {label} artifact source {name} is malformed"
            )
    _exact_positive_int(source["label_count"], f"{label} artifact source label count")
    evidence = _require_exact_fields(
        source["consumption_evidence"],
        {"path", "schema", "raw_artifact_sha256"},
        f"{label} artifact source consumption evidence",
    )
    if (
        not isinstance(evidence["path"], str)
        or not Path(evidence["path"]).is_absolute()
        or os.path.normpath(evidence["path"]) != evidence["path"]
        or str(Path(evidence["path"]).expanduser().resolve()) != evidence["path"]
        or evidence["schema"] != "spc-cycle3-one-shot-result-v1"
        or not isinstance(evidence["raw_artifact_sha256"], str)
        or HEX_SHA256.fullmatch(evidence["raw_artifact_sha256"]) is None
    ):
        raise ValueError(
            f"preregistration {label} artifact consumption evidence is malformed"
        )


def _preregistration_from_manifest(
    path: Path,
    manifest: Mapping[str, Any],
    manifest_raw_sha: str,
) -> Preregistration:
    manifest = _require_exact_fields(
        manifest,
        {
            "schema",
            "status",
            "purpose",
            "source",
            "runtime",
            "profiles",
            "trajectory_corpora",
            "teacher",
            "integrity",
            "one_shot_gates",
            "post_holdout_match",
            "promotion_evidence",
            "preflight",
            "frozen_implementation",
        },
        "manifest",
    )
    schema = str(manifest.get("schema", ""))
    if schema != CYCLE4_PREREGISTRATION_SCHEMA:
        raise ValueError(f"unsupported preregistration schema: {schema!r}")
    if manifest.get("status") != "pre-registered-before-generation":
        raise ValueError("preregistration was not frozen before generation")
    if not isinstance(manifest.get("purpose"), str) or not manifest["purpose"].strip():
        raise ValueError("preregistration purpose is missing")

    source = _require_exact_fields(
        manifest.get("source"),
        {
            "base_deployed_commit",
            "integrated_engine_source_commit",
            "engine_version",
            "engine_source_fingerprint",
            "native_eval_source_identity_sha256",
            "native_mate_source_identity_sha256",
            "commit_reference_role",
        },
        "source contract",
    )
    if source.get("commit_reference_role") != (
        "operator provenance labels; production preregister verifies local Git "
        "resolution while executable source is bound by fingerprints, native "
        "identities, and frozen implementation hashes"
    ):
        raise ValueError("preregistration source commit-reference role differs")
    expected_source = {
        "engine_version": ENGINE_VERSION,
        "engine_source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "native_eval_source_identity_sha256": evaluation._native_source_identity(),
        "native_mate_source_identity_sha256": series_mate._native_mate_source_identity(),
    }
    for name, actual in expected_source.items():
        if not isinstance(actual, str) or source.get(name) != actual:
            raise ValueError(
                f"preregistration source {name} does not match the checkout"
            )
    for name in ("base_deployed_commit", "integrated_engine_source_commit"):
        value = source.get(name)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError(f"preregistration source {name} is malformed")

    trajectories = _require_exact_fields(
        manifest.get("trajectory_corpora"),
        {"train", "sealed_holdout", "shared_config"},
        "trajectory contract",
    )
    train_trajectory = _require_exact_fields(
        trajectories.get("train"),
        {"seed", "attempts", "attempt_start", "attempt_stop"},
        "train trajectory",
        optional={"artifact_source"},
    )
    holdout_trajectory = _require_exact_fields(
        trajectories.get("sealed_holdout"),
        {
            "seed",
            "attempts",
            "attempt_start",
            "attempt_stop",
            "development_exclusion_sha256",
            "one_shot",
        },
        "sealed holdout trajectory",
        optional={"artifact_source"},
    )
    shared_trajectory = _require_exact_fields(
        trajectories.get("shared_config"),
        SHARED_TRAJECTORY_FIELDS,
        "shared trajectory config",
    )
    train_seed = _exact_positive_int(train_trajectory.get("seed"), "train seed")
    holdout_seed = _exact_positive_int(
        holdout_trajectory.get("seed"), "sealed holdout seed"
    )
    train_attempts = _exact_positive_int(
        train_trajectory.get("attempts"), "train attempts"
    )
    holdout_attempts = _exact_positive_int(
        holdout_trajectory.get("attempts"), "sealed holdout attempts"
    )
    for label, trajectory, attempts in (
        ("train", train_trajectory, train_attempts),
        ("sealed holdout", holdout_trajectory, holdout_attempts),
    ):
        if (
            trajectory.get("attempt_start") != 0
            or trajectory.get("attempt_stop") != attempts
        ):
            raise ValueError(f"preregistration {label} attempt window differs")
    if train_seed == holdout_seed:
        raise ValueError("preregistration train and holdout seeds must differ")
    if holdout_trajectory.get("one_shot") is not True:
        raise ValueError("preregistration sealed holdout must be one-shot")
    _validate_declared_artifact_source(
        train_trajectory.get("artifact_source"), "train"
    )
    expected_exclusion_sha = (
        str(train_trajectory["artifact_source"]["semantic_exclusion_sha256"])
        if isinstance(train_trajectory.get("artifact_source"), Mapping)
        else semantic_exclusion_sha256(())
    )
    if holdout_trajectory.get("development_exclusion_sha256") != expected_exclusion_sha:
        raise ValueError("preregistration development exclusion commitment differs")
    if holdout_trajectory.get("artifact_source") is not None:
        raise ValueError(
            "preregistration sealed holdout cannot predeclare an existing artifact source"
        )
    for name in (
        "max_attempt_series",
        "max_frontier_states",
        "candidate_count",
        "max_positions_per_series",
        "max_positions_per_game",
        "shard_size",
        "batch_size",
        "workers",
    ):
        _exact_positive_int(shared_trajectory.get(name), f"shared {name}")
    if (
        shared_trajectory.get("verify_payloads") is not True
        or shared_trajectory.get("count_unique_states") is not True
    ):
        raise ValueError("preregistration trajectory verification policy differs")
    if shared_trajectory.get("policy") != "uniform":
        raise ValueError("preregistration trajectory policy differs")
    if shared_trajectory.get("profile_schedule") != "ordered-pair-round-robin":
        raise ValueError("preregistration trajectory profile schedule differs")
    if shared_trajectory["candidate_count"] > shared_trajectory["max_frontier_states"]:
        raise ValueError("preregistration candidate count exceeds frontier capacity")

    teacher = _require_exact_fields(
        manifest.get("teacher"), TEACHER_FIELDS, "teacher contract"
    )
    for name in (
        "selection_seed",
        "minimum_series",
        "maximum_series",
        "branch_cap",
        "max_work",
        "hard_negatives",
        "workers",
    ):
        _exact_positive_int(teacher.get(name), f"teacher {name}")
    if teacher["minimum_series"] > teacher["maximum_series"]:
        raise ValueError("preregistration teacher series bounds are reversed")
    if teacher.get("prior_receipt_cache_reuse") is not False:
        raise ValueError("preregistration teacher cache-reuse flag is malformed")
    tiers = _require_exact_fields(
        teacher.get("tiers"), {"quiet_depth2", "tactical_depth3"}, "teacher tiers"
    )
    expected_tier_modes = {
        "quiet_depth2": ("quiet-nonterminal", "skipped-for-quiet-tier"),
        "tactical_depth3": ("tactical-low-complexity", "required"),
    }
    for tier_name, (selection_mode, tactical_gate) in expected_tier_modes.items():
        tier = _require_exact_fields(
            tiers.get(tier_name), TEACHER_TIER_FIELDS, f"teacher tier {tier_name}"
        )
        target = _exact_positive_int(
            tier.get("target_roots"), f"teacher tier {tier_name} target roots"
        )
        train = _exact_positive_int(
            tier.get("train_roots"), f"teacher tier {tier_name} train roots"
        )
        holdout = _exact_positive_int(
            tier.get("holdout_roots"), f"teacher tier {tier_name} holdout roots"
        )
        if train + holdout != target:
            raise ValueError(f"preregistration teacher tier {tier_name} counts differ")
        if (
            tier.get("selection_mode") != selection_mode
            or tier.get("tactical_gate") != tactical_gate
        ):
            raise ValueError(f"preregistration teacher tier {tier_name} mode differs")
    expected_total = _exact_positive_int(
        teacher.get("expected_merged_roots"), "merged teacher roots"
    )
    expected_train = _exact_positive_int(
        teacher.get("expected_merged_train_roots"), "train teacher roots"
    )
    expected_holdout = _exact_positive_int(
        teacher.get("expected_merged_holdout_roots"), "holdout teacher roots"
    )
    if expected_train + expected_holdout != expected_total:
        raise ValueError("preregistration teacher split counts do not sum")
    if expected_total != sum(
        int(tiers[name]["target_roots"]) for name in expected_tier_modes
    ) or expected_train != sum(
        int(tiers[name]["train_roots"]) for name in expected_tier_modes
    ) or expected_holdout != sum(
        int(tiers[name]["holdout_roots"]) for name in expected_tier_modes
    ):
        raise ValueError("preregistration merged teacher counts differ from tiers")

    integrity = _require_exact_fields(
        manifest.get("integrity"),
        {
            "holdout_output_must_not_be_manually_inspected_before_gate",
            "holdout_informed_filtering_forbidden",
            "required_zero_intersections",
            "seed_burn_rule",
            "teacher_semantic_hash_contract",
            "semantic_key",
        },
        "holdout integrity contract",
    )
    if any(
        integrity.get(name) is not True
        for name in (
            "holdout_output_must_not_be_manually_inspected_before_gate",
            "holdout_informed_filtering_forbidden",
        )
    ):
        raise ValueError("preregistration holdout integrity contract differs")
    intersections = integrity.get("required_zero_intersections")
    if intersections != REQUIRED_ZERO_INTERSECTIONS:
        raise ValueError("preregistration cross-split leakage gates differ")
    if integrity.get("seed_burn_rule") != _seed_burn_rule(holdout_seed):
        raise ValueError("preregistration seed-burn rule differs")
    if integrity.get("semantic_key") != "progressive_state_dedup_key":
        raise ValueError("preregistration semantic-key contract differs")
    if (
        integrity.get("teacher_semantic_hash_contract")
        != TEACHER_SEMANTIC_HASH_CONTRACT
    ):
        raise ValueError("preregistration teacher semantic-hash contract differs")

    gates = _require_exact_fields(
        manifest.get("one_shot_gates"),
        {
            "candidate_roles",
            "each_candidate_must",
            "route_ablation_must",
            "post_holdout_required_before_promotion",
        },
        "one-shot gates",
    )
    roles = gates.get("candidate_roles")
    if roles != ["primary_nonroute", "distilled_seven_weight"]:
        raise ValueError("preregistration candidate roles differ")
    if gates.get("each_candidate_must") != REQUIRED_CANDIDATE_GATES:
        raise ValueError("preregistration candidate holdout gates differ")
    if gates.get("route_ablation_must") != REQUIRED_ROUTE_ABLATION_GATES:
        raise ValueError("preregistration route-ablation holdout gates differ")
    if gates.get("post_holdout_required_before_promotion") != REQUIRED_POST_HOLDOUT_GATES:
        raise ValueError("preregistration post-holdout gates differ")

    match = manifest.get("post_holdout_match")
    if not isinstance(match, Mapping):
        raise ValueError("preregistration post-holdout match contract is missing")
    match_seed = _exact_positive_int(match.get("seed"), "post-holdout match seed")
    if len({train_seed, holdout_seed, int(teacher["selection_seed"]), match_seed}) != 4:
        raise ValueError("preregistration train, holdout, teacher, and match seeds must differ")
    expected_match = _post_holdout_match_contract(
        match_seed, str(source["base_deployed_commit"])
    )
    if dict(match) != expected_match:
        raise ValueError("preregistration post-holdout match contract differs")
    if manifest.get("promotion_evidence") != PROMOTION_EVIDENCE_FIXED:
        raise ValueError("preregistration promotion evidence contract differs")

    preflight = _require_exact_fields(
        manifest.get("preflight"),
        {"holdout_consumed", "generation_started"},
        "preflight",
    )
    if any(
        preflight.get(name) is not False
        for name in ("holdout_consumed", "generation_started")
    ):
        raise ValueError("preregistration preflight is not an unused pre-generation state")

    frozen = manifest.get("frozen_implementation")
    expected_frozen = _current_frozen_implementation()
    if not isinstance(frozen, Mapping) or set(frozen) != set(expected_frozen):
        raise ValueError("preregistration frozen implementation fields differ")
    drifted = [
        name for name, actual in expected_frozen.items() if frozen.get(name) != actual
    ]
    if drifted:
        raise ValueError(
            "preregistration frozen implementation drifted: " + ", ".join(drifted)
        )

    profiles = _require_exact_fields(
        manifest.get("profiles"),
        {"ordered_source_schedule", "rejected_development_leader"},
        "profile bindings",
    )
    schedule = profiles.get("ordered_source_schedule")
    leader = profiles.get("rejected_development_leader")
    if not isinstance(schedule, list) or not schedule or not isinstance(leader, Mapping):
        raise ValueError("preregistration profile bindings are incomplete")
    if len(schedule) != 4:
        raise ValueError("preregistration source schedule must contain four profiles")
    for label, entry in [
        *( (f"source profile {index}", item) for index, item in enumerate(schedule) ),
        ("rejected development leader", leader),
    ]:
        entry = _require_exact_fields(
            entry, {"path", "profile_id", "sha256"}, label
        )
        relative = entry.get("path")
        expected_sha = entry.get("sha256")
        expected_id = entry.get("profile_id")
        if (
            not isinstance(relative, str)
            or not isinstance(expected_sha, str)
            or HEX_SHA256.fullmatch(expected_sha) is None
            or not isinstance(expected_id, str)
        ):
            raise ValueError(f"preregistration {label} binding is malformed")
        profile_path = _repository_file(relative)
        profile, profile_raw_sha = _read_profile_artifact(profile_path)
        if profile_raw_sha != expected_sha:
            raise ValueError(f"preregistration {label} file hash drifted")
        if profile.profile_id != expected_id:
            raise ValueError(f"preregistration {label} identity drifted")

    if "runtime" not in manifest:
        raise ValueError("preregistration runtime contract is missing")
    _validate_preregistered_runtime(manifest["runtime"])
    declared_train_source = train_trajectory.get("artifact_source")
    effective_train_labels = (
        int(declared_train_source["label_count"])
        if isinstance(declared_train_source, Mapping)
        else expected_train
    )
    return Preregistration(
        path=path,
        sha256=manifest_raw_sha,
        schema=schema,
        expected_train_labels=effective_train_labels,
        expected_holdout_labels=expected_holdout,
        manifest=manifest,
    )


def _load_preregistration(
    path: Path,
    *,
    require_reservation: bool = True,
    require_pair_completion: bool = False,
    forbid_pair_preparation: bool = False,
) -> Preregistration:
    if require_pair_completion and forbid_pair_preparation:
        raise ValueError(
            "preregistration cannot require completed pairing and forbid pairing"
        )
    if require_reservation:
        # The caller spelling is untrusted and could name (or hardlink to) the
        # sealed holdout. Bootstrap from the fixed Git-common reservation and
        # reject a different spelling before touching the supplied path.
        _require_lexical_absolute_input(path, "preregistration manifest")
        cycle_reservation_path = _cycle_preregistration_reservation_path()
        reservation, _ = _read_json_artifact(cycle_reservation_path)
        reservation = _require_exact_fields(
            reservation,
            {
                "schema",
                "cycle_schema",
                "manifest_path",
                "manifest_raw_artifact_sha256",
                "train_seed",
                "holdout_seed",
                "teacher_selection_seed",
                "post_holdout_match_seed",
            },
            "cycle preregistration reservation",
        )
        reserved_path = reservation.get("manifest_path")
        reserved_raw_sha = reservation.get("manifest_raw_artifact_sha256")
        if (
            reservation.get("schema") != PREREGISTRATION_RESERVATION_SCHEMA
            or reservation.get("cycle_schema")
            != "spc-cycle4-one-shot-protocol-v1"
            or not isinstance(reserved_path, str)
            or not Path(reserved_path).is_absolute()
            or os.path.normpath(reserved_path) != reserved_path
            or not isinstance(reserved_raw_sha, str)
            or HEX_SHA256.fullmatch(reserved_raw_sha) is None
        ):
            raise ValueError("cycle preregistration reservation is malformed")
        if str(path) != reserved_path:
            raise ValueError(
                "supplied preregistration path differs from the central reservation"
            )
        completion_path = _pair_completion_registry_path_for_seed(
            reserved_holdout_seed := _exact_positive_int(
                reservation.get("holdout_seed"), "reserved holdout seed"
            )
        )
        if forbid_pair_preparation:
            if any(
                terminal_path.exists()
                for terminal_path in _terminal_pair_state_paths_for_seed(
                    reserved_holdout_seed
                )
            ):
                raise FileExistsError(
                    "cycle-4 holdout preparation has started; upstream producer and "
                    "development-import stages are permanently closed"
                )
        if require_pair_completion and not completion_path.exists():
            raise FileNotFoundError(
                "central pair completion is missing; resume pair-artifacts before "
                "fit or holdout evaluation"
            )
        guard: tuple[Path, dict[str, int]] | None = None
        if completion_path.exists():
            completion, _ = _read_json_artifact(completion_path)
            if (
                completion.get("schema") != PAIR_COMPLETION_REGISTRY_SCHEMA
                or completion.get("preregistration")
                != {
                    "schema": reservation["cycle_schema"],
                    "sha256": reserved_raw_sha,
                }
            ):
                raise ValueError(
                    "central pair completion differs from preregistration reservation"
                )
            sealed = _require_exact_fields(
                completion.get("sealed_holdout"),
                {"path", "raw_artifact_sha256", "file_identity"},
                "bootstrap sealed holdout binding",
            )
            if not isinstance(sealed["path"], str) or not Path(
                sealed["path"]
            ).is_absolute():
                raise ValueError("bootstrap sealed holdout path is malformed")
            guard = (
                Path(sealed["path"]),
                _validated_file_identity(
                    sealed["file_identity"], "bootstrap sealed holdout"
                ),
            )
        if guard is None:
            manifest, manifest_raw_sha = _read_json_artifact_with_expected_raw(
                path,
                reserved_raw_sha,
                label="preregistration manifest",
            )
        else:
            with _sealed_alias_guard(*guard):
                manifest, manifest_raw_sha = _read_json_artifact_with_expected_raw(
                    path,
                    reserved_raw_sha,
                    label="preregistration manifest",
                )
    else:
        path = path.expanduser().resolve()
        manifest, manifest_raw_sha = _read_json_artifact(path)
    if require_reservation and guard is not None:
        with _sealed_alias_guard(*guard):
            preregistration = _preregistration_from_manifest(
                path, manifest, manifest_raw_sha
            )
    else:
        preregistration = _preregistration_from_manifest(
            path, manifest, manifest_raw_sha
        )
    if require_reservation:
        if reservation != _preregistration_reservation_payload(preregistration):
            raise ValueError(
                "central cycle reservation differs from the authenticated manifest"
            )
        _validate_preregistration_reservation(preregistration)
    return preregistration


def _preregistered_source_profiles(
    preregistration: Preregistration,
) -> tuple[EngineProfile, ...]:
    entries = preregistration.manifest["profiles"]["ordered_source_schedule"]
    profiles: list[EngineProfile] = []
    for entry in entries:
        profile, raw_sha = _read_profile_artifact(
            _repository_file(str(entry["path"]))
        )
        if raw_sha != entry["sha256"] or profile.profile_id != entry["profile_id"]:
            raise ValueError("preregistered source profile changed after validation")
        profiles.append(profile)
    return tuple(profiles)


def _expected_generation_contract_sha256(
    preregistration: Preregistration,
    *,
    split: str,
) -> str:
    manifest = preregistration.manifest
    trajectory = manifest["trajectory_corpora"]
    shared = trajectory["shared_config"]
    split_config = trajectory[split]
    if shared.get("policy") != "uniform":
        raise ValueError("preregistration trajectory policy is unsupported")
    if shared.get("profile_schedule") != "ordered-pair-round-robin":
        raise ValueError("preregistration trajectory profile schedule is unsupported")
    config = NativeCorpusConfig(
        seed=int(split_config["seed"]),
        max_attempt_series=int(shared["max_attempt_series"]),
        max_frontier_states=int(shared["max_frontier_states"]),
        max_positions_per_series=int(shared["max_positions_per_series"]),
        max_positions_per_game=int(shared["max_positions_per_game"]),
        candidate_count=int(shared["candidate_count"]),
        policy=NativeRankPolicy.uniform(),
        schedule=NativeProfileSchedule.ORDERED_PAIR_ROUND_ROBIN,
    )
    native_profiles = bind_native_profiles(
        _preregistered_source_profiles(preregistration)
    )
    identity = CorpusIdentity(
        record_schema=NATIVE_BOUNDARY_SAMPLE_SCHEMA,
        source_fingerprint=config.engine_source_fingerprint,
        generator_config_sha256=semantic_config_digest(
            config, native_profiles
        ).hex(),
        profile_ids=tuple(profile.profile_id for profile in native_profiles),
        ruleset_version=config.ruleset_version,
    )
    return NativeGenerationContract(config, native_profiles, identity).digest_hex


def _validate_preregistered_identity(
    provenance: Mapping[str, Any], preregistration: Preregistration
) -> None:
    if provenance.get("schema") != "spc-cycle4-preregistered-generation-provenance-v1" or provenance.get(
        "preregistration"
    ) != {
        "schema": preregistration.schema,
        "raw_artifact_sha256": preregistration.sha256,
    }:
        raise ValueError("generation preregistration identity differs")


def _validate_trajectory_generation_starts(
    value: object, preregistration: Preregistration
) -> None:
    trajectory_starts = _require_exact_fields(
        value,
        {"train", "sealed_holdout"},
        "trajectory generation starts",
    )
    for split in ("train", "sealed_holdout"):
        start = _require_exact_fields(
            trajectory_starts[split],
            {
                "schema",
                "generation_contract_sha256",
                "corpus",
                "raw_artifact_sha256",
                "root_binding_path",
                "root_binding_raw_artifact_sha256",
                "completion_receipt_raw_artifact_sha256",
            },
            f"{split} trajectory generation start",
        )
        if (
            start["schema"] != "spc-cycle4-trajectory-generation-start-v1"
            or start["generation_contract_sha256"]
            != _expected_generation_contract_sha256(preregistration, split=split)
            or not isinstance(start["corpus"], Mapping)
            or set(start["corpus"])
            != {"corpus_sha256", "attempt_count", "record_count", "shard_count"}
            or any(type(start["corpus"][name]) is not int for name in (
                "attempt_count", "record_count", "shard_count"
            ))
            or start["corpus"]["attempt_count"]
            != preregistration.manifest["trajectory_corpora"][split]["attempts"]
            or not isinstance(start["corpus"]["corpus_sha256"], str)
            or HEX_SHA256.fullmatch(start["corpus"]["corpus_sha256"]) is None
            or not isinstance(start["raw_artifact_sha256"], str)
            or HEX_SHA256.fullmatch(start["raw_artifact_sha256"]) is None
            or not isinstance(start["root_binding_path"], str)
            or not Path(start["root_binding_path"]).is_absolute()
            or os.path.normpath(start["root_binding_path"])
            != start["root_binding_path"]
            or not isinstance(start["root_binding_raw_artifact_sha256"], str)
            or HEX_SHA256.fullmatch(
                start["root_binding_raw_artifact_sha256"]
            )
            is None
            or not isinstance(
                start["completion_receipt_raw_artifact_sha256"], str
            )
            or HEX_SHA256.fullmatch(
                start["completion_receipt_raw_artifact_sha256"]
            )
            is None
        ):
            raise ValueError(f"{split} trajectory generation-start evidence differs")


def _validate_teacher_generation_start(value: object, tier_name: str) -> None:
    start = _require_exact_fields(
        value,
        {"schema", "tier", "path", "raw_artifact_sha256"},
        f"{tier_name} teacher generation start",
    )
    if (
        start["schema"] != "spc-cycle4-teacher-generation-start-v1"
        or start["tier"] != tier_name
        or not isinstance(start["path"], str)
        or not Path(start["path"]).is_absolute()
        or os.path.normpath(start["path"]) != start["path"]
        or not isinstance(start["raw_artifact_sha256"], str)
        or HEX_SHA256.fullmatch(start["raw_artifact_sha256"]) is None
    ):
        raise ValueError(f"{tier_name} teacher generation-start evidence differs")


def _validate_teacher_generation_source_binding(
    value: object, tier_name: str
) -> None:
    binding = _require_exact_fields(
        value,
        {"schema", "tier", "path", "raw_artifact_sha256"},
        f"{tier_name} teacher source binding",
    )
    if (
        binding["schema"] != "spc-cycle4-teacher-generation-sources-v1"
        or binding["tier"] != tier_name
        or not isinstance(binding["path"], str)
        or not Path(binding["path"]).is_absolute()
        or os.path.normpath(binding["path"]) != binding["path"]
        or not isinstance(binding["raw_artifact_sha256"], str)
        or HEX_SHA256.fullmatch(binding["raw_artifact_sha256"]) is None
    ):
        raise ValueError(f"{tier_name} teacher source-binding evidence differs")


def _validate_semantic_augmentation_start(value: object, tier_name: str) -> None:
    start = _require_exact_fields(
        value,
        {"schema", "tier", "path", "raw_artifact_sha256"},
        f"{tier_name} semantic augmentation start",
    )
    if (
        start["schema"]
        != "spc-cycle4-teacher-semantic-augmentation-start-v1"
        or start["tier"] != tier_name
        or not isinstance(start["path"], str)
        or not Path(start["path"]).is_absolute()
        or os.path.normpath(start["path"]) != start["path"]
        or not isinstance(start["raw_artifact_sha256"], str)
        or HEX_SHA256.fullmatch(start["raw_artifact_sha256"]) is None
    ):
        raise ValueError(f"{tier_name} semantic augmentation-start evidence differs")


def _validate_semantic_augmentation_source_binding(
    value: object, tier_name: str
) -> None:
    binding = _require_exact_fields(
        value,
        {"schema", "tier", "path", "raw_artifact_sha256"},
        f"{tier_name} semantic augmentation source binding",
    )
    if (
        binding["schema"]
        != "spc-cycle4-teacher-semantic-augmentation-sources-v1"
        or binding["tier"] != tier_name
        or not isinstance(binding["path"], str)
        or not Path(binding["path"]).is_absolute()
        or os.path.normpath(binding["path"]) != binding["path"]
        or not isinstance(binding["raw_artifact_sha256"], str)
        or HEX_SHA256.fullmatch(binding["raw_artifact_sha256"]) is None
    ):
        raise ValueError(
            f"{tier_name} semantic augmentation source-binding evidence differs"
        )


def _validate_preregistered_generation_provenance(
    generation: Mapping[str, Any],
    preregistration: Preregistration,
) -> dict[str, Any]:
    provenance = _require_exact_fields(
        generation.get("preregistration_generation_provenance"),
        {
            "schema",
            "preregistration",
            "trajectory_generation_starts",
            "teacher_generation_starts",
            "teacher_generation_source_bindings",
            "semantic_augmentation_starts",
            "semantic_augmentation_source_bindings",
            "merge_generation_start",
            "merge_generation_source_binding",
        },
        "generation preregistration provenance",
    )
    _validate_preregistered_identity(provenance, preregistration)
    _validate_trajectory_generation_starts(
        provenance["trajectory_generation_starts"], preregistration
    )
    teacher_starts = _require_exact_fields(
        provenance["teacher_generation_starts"],
        {"quiet_depth2", "tactical_depth3"},
        "teacher generation starts",
    )
    for tier_name in ("quiet_depth2", "tactical_depth3"):
        _validate_teacher_generation_start(teacher_starts[tier_name], tier_name)
    teacher_source_bindings = _require_exact_fields(
        provenance["teacher_generation_source_bindings"],
        {"quiet_depth2", "tactical_depth3"},
        "teacher generation source bindings",
    )
    for tier_name in ("quiet_depth2", "tactical_depth3"):
        _validate_teacher_generation_source_binding(
            teacher_source_bindings[tier_name], tier_name
        )
    augmentation_starts = _require_exact_fields(
        provenance["semantic_augmentation_starts"],
        {"quiet_depth2", "tactical_depth3"},
        "teacher semantic augmentation starts",
    )
    for tier_name in ("quiet_depth2", "tactical_depth3"):
        _validate_semantic_augmentation_start(
            augmentation_starts[tier_name], tier_name
        )
    augmentation_source_bindings = _require_exact_fields(
        provenance["semantic_augmentation_source_bindings"],
        {"quiet_depth2", "tactical_depth3"},
        "teacher semantic augmentation source bindings",
    )
    for tier_name in ("quiet_depth2", "tactical_depth3"):
        _validate_semantic_augmentation_source_binding(
            augmentation_source_bindings[tier_name], tier_name
        )
    merge_start_evidence = _require_exact_fields(
        provenance["merge_generation_start"],
        {"schema", "path", "raw_artifact_sha256"},
        "teacher merge generation start",
    )
    if (
        merge_start_evidence["schema"] != "spc-cycle4-teacher-merge-start-v1"
        or not isinstance(merge_start_evidence["path"], str)
        or not Path(merge_start_evidence["path"]).is_absolute()
        or os.path.normpath(merge_start_evidence["path"])
        != merge_start_evidence["path"]
        or str(Path(merge_start_evidence["path"]).expanduser().resolve())
        != merge_start_evidence["path"]
        or not isinstance(merge_start_evidence["raw_artifact_sha256"], str)
        or HEX_SHA256.fullmatch(merge_start_evidence["raw_artifact_sha256"])
        is None
    ):
        raise ValueError("teacher merge generation-start evidence differs")
    merge_start, merge_start_raw = _read_json_artifact(
        Path(merge_start_evidence["path"])
    )
    merge_start = _require_exact_fields(
        merge_start,
        {
            "schema",
            "preregistration",
            "quiet_depth2",
            "tactical_depth3",
            "output",
            "source_binding",
        },
        "teacher merge generation-start artifact",
    )
    if (
        merge_start_raw != merge_start_evidence["raw_artifact_sha256"]
        or
        merge_start["schema"] != "spc-cycle4-teacher-merge-start-v1"
        or merge_start["preregistration"] != provenance["preregistration"]
        or any(
            not isinstance(merge_start[name], str)
            or not Path(merge_start[name]).is_absolute()
            or os.path.normpath(merge_start[name]) != merge_start[name]
            or str(Path(merge_start[name]).expanduser().resolve())
            != merge_start[name]
            for name in ("quiet_depth2", "tactical_depth3", "output")
        )
        or not isinstance(merge_start.get("source_binding"), str)
        or not Path(merge_start["source_binding"]).is_absolute()
        or os.path.normpath(merge_start["source_binding"])
        != merge_start["source_binding"]
        or str(Path(merge_start["source_binding"]).expanduser().resolve())
        != merge_start["source_binding"]
    ):
        raise ValueError("teacher merge generation-start evidence differs")
    merge_source_evidence = _require_exact_fields(
        provenance["merge_generation_source_binding"],
        {"schema", "path", "raw_artifact_sha256"},
        "teacher merge source binding",
    )
    if (
        merge_source_evidence["schema"] != "spc-cycle4-teacher-merge-sources-v1"
        or merge_source_evidence["path"] != merge_start["source_binding"]
        or str(Path(merge_source_evidence["path"]).expanduser().resolve())
        != merge_source_evidence["path"]
        or not isinstance(merge_source_evidence["raw_artifact_sha256"], str)
        or HEX_SHA256.fullmatch(merge_source_evidence["raw_artifact_sha256"])
        is None
    ):
        raise ValueError("teacher merge source-binding evidence differs")
    merge_sources, merge_sources_raw = _read_json_artifact(
        Path(merge_source_evidence["path"])
    )
    merge_sources = _require_exact_fields(
        merge_sources,
        {"schema", "preregistration", "merge_start", "tier_inputs"},
        "teacher merge source-binding artifact",
    )
    if (
        merge_sources_raw != merge_source_evidence["raw_artifact_sha256"]
        or merge_sources.get("schema") != "spc-cycle4-teacher-merge-sources-v1"
        or merge_sources.get("preregistration") != provenance["preregistration"]
        or merge_sources.get("merge_start") != merge_start_evidence
    ):
        raise ValueError("teacher merge source-binding artifact differs")
    tier_inputs = _require_exact_fields(
        merge_sources.get("tier_inputs"),
        {"quiet_depth2", "tactical_depth3"},
        "teacher merge tier inputs",
    )
    expected_input_fields = {
        "path",
        "corpus_id",
        "semantic_sha256",
        "raw_artifact_sha256",
        "augmentation_start_path",
        "augmentation_start_raw_artifact_sha256",
        "augmentation_source_binding_path",
        "augmentation_source_binding_raw_artifact_sha256",
        "augmentation_receipt_path",
        "augmentation_receipt_raw_artifact_sha256",
    }
    for tier_name in ("quiet_depth2", "tactical_depth3"):
        binding = _require_exact_fields(
            tier_inputs[tier_name], expected_input_fields, f"{tier_name} merge input"
        )
        if (
            binding["path"] != merge_start[tier_name]
            or not isinstance(binding["corpus_id"], str)
            or not binding["corpus_id"].startswith("spc-native-teacher-")
            or any(
                not isinstance(binding[name], str)
                or HEX_SHA256.fullmatch(binding[name]) is None
                for name in (
                    "semantic_sha256",
                    "raw_artifact_sha256",
                    "augmentation_start_raw_artifact_sha256",
                    "augmentation_source_binding_raw_artifact_sha256",
                    "augmentation_receipt_raw_artifact_sha256",
                )
            )
            or any(
                not isinstance(binding[name], str)
                or not Path(binding[name]).is_absolute()
                or os.path.normpath(binding[name]) != binding[name]
                for name in (
                    "path",
                    "augmentation_start_path",
                    "augmentation_source_binding_path",
                    "augmentation_receipt_path",
                )
            )
            or binding["augmentation_start_path"]
            != augmentation_starts[tier_name]["path"]
            or binding["augmentation_start_raw_artifact_sha256"]
            != augmentation_starts[tier_name]["raw_artifact_sha256"]
            or binding["augmentation_source_binding_path"]
            != augmentation_source_bindings[tier_name]["path"]
            or binding["augmentation_source_binding_raw_artifact_sha256"]
            != augmentation_source_bindings[tier_name]["raw_artifact_sha256"]
        ):
            raise ValueError(f"{tier_name} merge input binding differs")
    return {
        "merge_start": merge_start,
        "merge_start_evidence": merge_start_evidence,
        "merge_source_evidence": merge_source_evidence,
        "merge_sources": merge_sources,
        "merge_sources_raw_artifact_sha256": merge_sources_raw,
        "tier_inputs": tier_inputs,
        "trajectory_generation_starts": provenance["trajectory_generation_starts"],
        "semantic_augmentation_starts": augmentation_starts,
        "semantic_augmentation_source_bindings": augmentation_source_bindings,
    }


def _validate_preregistered_teacher_tier_artifact(
    corpus: Mapping[str, Any],
    preregistration: Preregistration,
    *,
    tier_name: str,
    require_semantic_augmentation: bool,
) -> None:
    """Validate an individual tier before augmentation or cross-tier use."""

    tier_specs = {
        "quiet_depth2": ("quiet-nonterminal", 2),
        "tactical_depth3": ("tactical-low-complexity", 3),
    }
    if tier_name not in tier_specs:
        raise ValueError("teacher tier name is unsupported")
    selection_mode, depth = tier_specs[tier_name]
    if (
        corpus.get("schema") != "spc-native-deep-teacher-corpus-v1"
        or corpus.get("method")
        != "balanced-native-trajectory-depth3-policy-teacher-v1"
        or corpus.get("engine_version") != ENGINE_VERSION
        or corpus.get("source_fingerprint") != ENGINE_SOURCE_FINGERPRINT
    ):
        raise ValueError(f"{tier_name} teacher source identity differs")
    manifest = preregistration.manifest
    teacher = manifest["teacher"]
    trajectory = manifest["trajectory_corpora"]
    tier = teacher["tiers"][tier_name]
    expected_config = {
        "target_roots": tier["target_roots"],
        "train_roots": tier["train_roots"],
        "minimum_series": teacher["minimum_series"],
        "maximum_series": teacher["maximum_series"],
        "depth_series": depth,
        "branch_cap": teacher["branch_cap"],
        "max_generation_positions": teacher["max_work"],
        "hard_negative_count": teacher["hard_negatives"],
        "seed": teacher["selection_seed"],
        "workers": teacher["workers"],
        "expected_train_attempts": trajectory["train"]["attempts"],
        "expected_holdout_attempts": trajectory["sealed_holdout"]["attempts"],
        "selection_mode": selection_mode,
    }
    config = corpus.get("config")
    if not isinstance(config, Mapping) or dict(config) != expected_config:
        raise ValueError(f"{tier_name} teacher config differs from preregistration")
    expected_profile = _preregistered_source_profiles(preregistration)[0].as_dict()
    if corpus.get("teacher_profile") != expected_profile:
        raise ValueError(f"{tier_name} teacher profile differs from preregistration")
    generation = corpus.get("generation")
    if not isinstance(generation, Mapping):
        raise ValueError(f"{tier_name} teacher generation provenance is missing")
    provenance_fields = {
        "schema",
        "preregistration",
        "trajectory_generation_starts",
        "teacher_generation_start",
        "teacher_generation_source_binding",
    }
    if require_semantic_augmentation:
        provenance_fields.add("teacher_semantic_augmentation_start")
        provenance_fields.add("teacher_semantic_augmentation_source_binding")
    provenance = _require_exact_fields(
        generation.get("preregistration_generation_provenance"),
        provenance_fields,
        f"{tier_name} teacher preregistration provenance",
    )
    _validate_preregistered_identity(provenance, preregistration)
    _validate_trajectory_generation_starts(
        provenance["trajectory_generation_starts"], preregistration
    )
    _validate_teacher_generation_start(
        provenance["teacher_generation_start"], tier_name
    )
    _validate_teacher_generation_source_binding(
        provenance["teacher_generation_source_binding"], tier_name
    )
    if require_semantic_augmentation:
        _validate_semantic_augmentation_start(
            provenance["teacher_semantic_augmentation_start"], tier_name
        )
        _validate_semantic_augmentation_source_binding(
            provenance["teacher_semantic_augmentation_source_binding"], tier_name
        )
    generation_expected = {
        "train_contract_sha256": _expected_generation_contract_sha256(
            preregistration, split="train"
        ),
        "holdout_contract_sha256": _expected_generation_contract_sha256(
            preregistration, split="sealed_holdout"
        ),
        "ordered_profile_ids": [
            entry["profile_id"]
            for entry in manifest["profiles"]["ordered_source_schedule"]
        ],
        "profile_schedule": trajectory["shared_config"]["profile_schedule"],
        "train_attempts": trajectory["train"]["attempts"],
        "holdout_attempts": trajectory["sealed_holdout"]["attempts"],
        "train_attempt_start": trajectory["train"]["attempt_start"],
        "train_attempt_stop": trajectory["train"]["attempt_stop"],
        "holdout_attempt_start": trajectory["sealed_holdout"]["attempt_start"],
        "holdout_attempt_stop": trajectory["sealed_holdout"]["attempt_stop"],
        "development_holdout_exclusion_sha256": trajectory["sealed_holdout"][
            "development_exclusion_sha256"
        ],
        "prior_receipt_cache_reuse": teacher["prior_receipt_cache_reuse"],
    }
    for name, expected in generation_expected.items():
        if generation.get(name) != expected:
            raise ValueError(f"{tier_name} teacher generation {name} drifted")
    for split, name in (
        ("train", "train_corpus_sha256"),
        ("sealed_holdout", "holdout_corpus_sha256"),
    ):
        if generation.get(name) != provenance["trajectory_generation_starts"][split][
            "corpus"
        ]["corpus_sha256"]:
            raise ValueError(f"{tier_name} teacher generation {name} differs")
    quality = corpus.get("quality")
    if (
        not isinstance(quality, Mapping)
        or quality.get("status") != "complete"
        or quality.get("accepted_roots") != tier["target_roots"]
        or quality.get("train_roots") != tier["train_roots"]
        or quality.get("holdout_roots") != tier["holdout_roots"]
    ):
        raise ValueError(f"{tier_name} teacher quality differs")
    _validate_tactical_gate_evidence(
        quality, str(tier["tactical_gate"]), f"{tier_name} teacher"
    )
    selection = corpus.get("selection")
    if not isinstance(selection, Mapping) or any(
        selection.get(name) != 0
        for name in (
            "selected_root_exact_overlap_states",
            "cross_split_option_final_exact_overlap_states",
            "train_option_final_to_holdout_root_overlap_states",
            "holdout_option_final_to_train_root_overlap_states",
        )
    ):
        raise ValueError(f"{tier_name} teacher leakage evidence differs")
    labels = corpus.get("labels")
    if not isinstance(labels, list) or len(labels) != tier["target_roots"]:
        raise ValueError(f"{tier_name} teacher label count differs")
    semantic_contract: Mapping[str, Any] | None = None
    if require_semantic_augmentation:
        semantic_contract = _require_exact_fields(
            corpus.get("semantic_state_contract"),
            {
                "schema",
                "input_raw_artifact_sha256",
                "input_semantic_sha256",
                "train_corpus_sha256",
                "holdout_corpus_sha256",
                "labels_replayed",
                "options_replayed",
                "promoted_roots",
                "promoted_option_final_states",
                "root_fields",
                "option_fields",
                "key_algorithm",
                "pfen_is_not_semantically_complete",
                "all_root_keys_replayed",
                "all_option_keys_replayed",
                "all_cached_features_regenerated",
            },
            f"{tier_name} semantic state contract",
        )
        if any(
            not isinstance(semantic_contract[name], str)
            or HEX_SHA256.fullmatch(semantic_contract[name]) is None
            for name in ("input_raw_artifact_sha256", "input_semantic_sha256")
        ):
            raise ValueError(f"{tier_name} semantic input identity differs")
    split_counts = {"train": 0, "holdout": 0}
    profile_ids = set(generation_expected["ordered_profile_ids"])
    for label in labels:
        if not isinstance(label, Mapping) or label.get("split") not in split_counts:
            raise ValueError(f"{tier_name} teacher label is malformed")
        split = str(label["split"])
        split_counts[split] += 1
        attempt = label.get("attempt_index")
        trajectory_split = "train" if split == "train" else "sealed_holdout"
        if (
            type(attempt) is not int
            or not trajectory[trajectory_split]["attempt_start"]
            <= attempt
            < trajectory[trajectory_split]["attempt_stop"]
            or any(
                label.get(name) not in profile_ids
                for name in (
                    "source_profile_id",
                    "white_profile_id",
                    "black_profile_id",
                )
            )
        ):
            raise ValueError(f"{tier_name} teacher label provenance differs")
        search = label.get("search")
        options = label.get("options")
        if (
            not isinstance(search, Mapping)
            or search.get("requested_depth_series") != depth
            or search.get("completed_depth_series") != depth
            or search.get("root_scores_complete") is not True
            or search.get("timed_out") is not False
            or search.get("work_limit_reached") is not False
            or not isinstance(options, list)
            or not options
        ):
            raise ValueError(f"{tier_name} teacher label search is incomplete")
        if require_semantic_augmentation and (
            type(label.get("root_promoted_bitboard")) is not int
            or type(label.get("root_chess960")) is not bool
            or any(
                not isinstance(option, Mapping)
                or type(option.get("final_promoted_bitboard")) is not int
                or type(option.get("final_chess960")) is not bool
                for option in options
            )
        ):
            raise ValueError(f"{tier_name} semantic augmentation is incomplete")
    if semantic_contract is not None:
        expected_semantic_contract = {
            "schema": "spc-native-teacher-semantic-augmentation-v1",
            "train_corpus_sha256": generation["train_corpus_sha256"],
            "holdout_corpus_sha256": generation["holdout_corpus_sha256"],
            "labels_replayed": len(labels),
            "options_replayed": sum(len(label["options"]) for label in labels),
            "promoted_roots": sum(
                int(label["root_promoted_bitboard"] != 0) for label in labels
            ),
            "promoted_option_final_states": sum(
                int(option["final_promoted_bitboard"] != 0)
                for label in labels
                for option in label["options"]
            ),
            "root_fields": ["root_promoted_bitboard", "root_chess960"],
            "option_fields": ["final_promoted_bitboard", "final_chess960"],
            "key_algorithm": "progressive_state_dedup_key-v1-sha256-hex",
            "pfen_is_not_semantically_complete": True,
            "all_root_keys_replayed": True,
            "all_option_keys_replayed": True,
            "all_cached_features_regenerated": True,
        }
        if any(
            semantic_contract.get(name) != expected
            for name, expected in expected_semantic_contract.items()
        ):
            raise ValueError(f"{tier_name} semantic state contract differs")
    if split_counts != {
        "train": tier["train_roots"],
        "holdout": tier["holdout_roots"],
    }:
        raise ValueError(f"{tier_name} teacher split counts differ")


def _validate_merge_tier_publications(
    binding: Mapping[str, Any], preregistration: Preregistration
) -> None:
    preregistration_identity = {
        "schema": preregistration.schema,
        "raw_artifact_sha256": preregistration.sha256,
    }
    tier_inputs = binding["tier_inputs"]
    augmentation_starts = binding["semantic_augmentation_starts"]
    augmentation_sources = binding["semantic_augmentation_source_bindings"]
    trajectory_starts = binding["trajectory_generation_starts"]
    raw_teacher_inputs: dict[str, Mapping[str, Any]] = {}
    for tier_name in ("quiet_depth2", "tactical_depth3"):
        tier_input = tier_inputs[tier_name]
        tier_path = Path(tier_input["path"])
        tier_payload, tier_raw_sha = _read_json_artifact(tier_path)
        if (
            tier_raw_sha != tier_input["raw_artifact_sha256"]
            or tier_payload.get("corpus_id") != tier_input["corpus_id"]
            or _teacher_semantic_sha256(tier_payload)
            != tier_input["semantic_sha256"]
        ):
            raise ValueError(f"{tier_name} augmented merge input differs")
        _validate_preregistered_teacher_tier_artifact(
            tier_payload,
            preregistration,
            tier_name=tier_name,
            require_semantic_augmentation=True,
        )

        start_evidence = augmentation_starts[tier_name]
        start_payload, start_raw_sha = _read_json_artifact(
            Path(start_evidence["path"])
        )
        if (
            start_raw_sha != start_evidence["raw_artifact_sha256"]
            or start_payload.get("schema")
            != "spc-cycle4-teacher-semantic-augmentation-start-v1"
            or start_payload.get("preregistration") != preregistration_identity
            or start_payload.get("tier") != tier_name
            or start_payload.get("output") != str(tier_path)
            or start_payload.get("source_binding")
            != augmentation_sources[tier_name]["path"]
            or start_payload.get("receipt")
            != tier_input["augmentation_receipt_path"]
        ):
            raise ValueError(f"{tier_name} augmentation-start artifact differs")

        source_evidence = augmentation_sources[tier_name]
        source_payload, source_raw_sha = _read_json_artifact(
            Path(source_evidence["path"])
        )
        source_payload = _require_exact_fields(
            source_payload,
            {
                "schema",
                "preregistration",
                "tier",
                "augmentation_start",
                "input",
                "trajectory_corpora",
                "output",
                "receipt",
            },
            f"{tier_name} augmentation source artifact",
        )
        if (
            source_raw_sha != source_evidence["raw_artifact_sha256"]
            or source_payload["schema"]
            != "spc-cycle4-teacher-semantic-augmentation-sources-v1"
            or source_payload["preregistration"] != preregistration_identity
            or source_payload["tier"] != tier_name
            or source_payload["augmentation_start"] != start_evidence
            or source_payload["output"] != str(tier_path)
            or source_payload["receipt"]
            != tier_input["augmentation_receipt_path"]
        ):
            raise ValueError(f"{tier_name} augmentation source artifact differs")

        source_input = _require_exact_fields(
            source_payload["input"],
            {"path", "corpus_id", "semantic_sha256", "raw_artifact_sha256"},
            f"{tier_name} raw augmentation input",
        )
        raw_input_path = Path(source_input["path"])
        if (
            not raw_input_path.is_absolute()
            or os.path.normpath(str(raw_input_path)) != str(raw_input_path)
            or start_payload.get("input") != str(raw_input_path)
        ):
            raise ValueError(f"{tier_name} raw augmentation input path differs")
        raw_input, raw_input_sha = _read_json_artifact(raw_input_path)
        if (
            raw_input_sha != source_input["raw_artifact_sha256"]
            or raw_input.get("corpus_id") != source_input["corpus_id"]
            or _teacher_semantic_sha256(raw_input)
            != source_input["semantic_sha256"]
        ):
            raise ValueError(f"{tier_name} raw augmentation input differs")
        _validate_preregistered_teacher_tier_artifact(
            raw_input,
            preregistration,
            tier_name=tier_name,
            require_semantic_augmentation=False,
        )
        raw_publication = _validate_raw_teacher_publication(
            raw_input,
            preregistration,
            tier_name=tier_name,
            supplied_path=raw_input_path,
            supplied_raw_sha256=raw_input_sha,
        )
        raw_teacher_inputs[tier_name] = raw_publication["input_artifacts"]
        if tier_name == "tactical_depth3":
            cross_binding = raw_publication["input_artifacts"][
                "cross_tier_artifact"
            ]
            quiet_binding = tier_inputs["quiet_depth2"]
            expected_cross_binding = {
                name: quiet_binding[name]
                for name in (
                    "path",
                    "corpus_id",
                    "semantic_sha256",
                    "raw_artifact_sha256",
                )
            }
            if cross_binding != expected_cross_binding:
                raise ValueError(
                    "tactical_depth3 cross-tier input differs from merged quiet tier"
                )

        trajectory = _require_exact_fields(
            source_payload["trajectory_corpora"],
            {"train", "sealed_holdout"},
            f"{tier_name} augmentation trajectory sources",
        )
        raw_generation = raw_input["generation"]
        for split, corpus_field, root_field in (
            ("train", "train_corpus_sha256", "train_root"),
            ("sealed_holdout", "holdout_corpus_sha256", "sealed_holdout_root"),
        ):
            trajectory_source = _require_exact_fields(
                trajectory[split],
                {
                    "root",
                    "generation_contract_sha256",
                    "corpus",
                    "generation_start",
                },
                f"{tier_name} {split} augmentation trajectory source",
            )
            if (
                trajectory_source["root"] != start_payload[root_field]
                or trajectory_source["generation_contract_sha256"]
                != _expected_generation_contract_sha256(
                    preregistration, split=split
                )
                or trajectory_source["generation_start"] != trajectory_starts[split]
                or trajectory_source["corpus"] != trajectory_starts[split]["corpus"]
                or raw_generation[corpus_field]
                != trajectory_source["corpus"]["corpus_sha256"]
            ):
                raise ValueError(
                    f"{tier_name} {split} augmentation trajectory source differs"
                )

        receipt_payload, receipt_raw_sha = _read_json_artifact(
            Path(tier_input["augmentation_receipt_path"])
        )
        expected_output = {
            "path": str(tier_path),
            "corpus_id": tier_payload["corpus_id"],
            "semantic_sha256": tier_input["semantic_sha256"],
            "raw_artifact_sha256": tier_input["raw_artifact_sha256"],
        }
        expected_semantic_replay = {
            **dict(tier_payload["semantic_state_contract"]),
            "source_corpus_id": tier_payload.get("source_corpus_id"),
            "augmented_corpus_id": tier_payload["corpus_id"],
        }
        semantic_contract = tier_payload["semantic_state_contract"]
        if (
            receipt_raw_sha
            != tier_input["augmentation_receipt_raw_artifact_sha256"]
            or receipt_payload.get("schema")
            != "spc-cycle4-teacher-semantic-augmentation-receipt-v1"
            or receipt_payload.get("preregistration") != preregistration_identity
            or receipt_payload.get("tier") != tier_name
            or receipt_payload.get("augmentation_start") != start_evidence
            or receipt_payload.get("augmentation_source_binding") != source_evidence
            or receipt_payload.get("input") != source_payload["input"]
            or receipt_payload.get("trajectory_corpora")
            != source_payload["trajectory_corpora"]
            or receipt_payload.get("output") != expected_output
            or receipt_payload.get("semantic_replay") != expected_semantic_replay
            or semantic_contract["input_raw_artifact_sha256"]
            != source_input["raw_artifact_sha256"]
            or semantic_contract["input_semantic_sha256"]
            != source_input["semantic_sha256"]
            or tier_payload.get("source_corpus_id") != source_input["corpus_id"]
        ):
            raise ValueError(f"{tier_name} augmentation receipt differs")
    for tier_name in ("quiet_depth2", "tactical_depth3"):
        tier_input = tier_inputs[tier_name]
        tier_path = Path(tier_input["path"])
        final_payload, final_raw_sha = _read_json_artifact(tier_path)
        if (
            final_raw_sha != tier_input["raw_artifact_sha256"]
            or final_payload.get("corpus_id") != tier_input["corpus_id"]
            or _teacher_semantic_sha256(final_payload)
            != tier_input["semantic_sha256"]
        ):
            raise ValueError(f"{tier_name} augmented merge input changed")
        final_lineage = _validate_augmented_teacher_publication(
            final_payload,
            preregistration,
            tier_name=tier_name,
            supplied_path=tier_path,
            supplied_raw_sha256=final_raw_sha,
        )
        if (
            final_lineage["raw_teacher_input_artifacts"]
            != raw_teacher_inputs[tier_name]
            or final_lineage["augmentation_receipt_path"]
            != tier_input["augmentation_receipt_path"]
            or final_lineage["augmentation_receipt_raw_artifact_sha256"]
            != tier_input["augmentation_receipt_raw_artifact_sha256"]
        ):
            raise ValueError(f"{tier_name} augmented merge lineage changed")
        closed_payload, closed_raw_sha = _read_json_artifact(tier_path)
        if closed_payload != final_payload or closed_raw_sha != final_raw_sha:
            raise ValueError(f"{tier_name} augmented merge input changed")
    if (
        raw_teacher_inputs["quiet_depth2"]["development_exclusion_artifact"]
        != raw_teacher_inputs["tactical_depth3"][
            "development_exclusion_artifact"
        ]
    ):
        raise ValueError("teacher tiers use different development exclusions")


def _validate_trajectory_publication_chain(
    root: Path,
    summary: Mapping[str, Any],
    preregistration: Preregistration,
    *,
    split: str,
) -> None:
    """Reopen and prove one completed trajectory publication end to end."""

    canonical_root = _canonical_cli_input(root, f"{split} trajectory root")
    trajectory = preregistration.manifest["trajectory_corpora"]
    split_contract = trajectory[split]
    shared = trajectory["shared_config"]
    expected_contract_sha = _expected_generation_contract_sha256(
        preregistration, split=split
    )
    start_path = canonical_root.with_name(
        canonical_root.name + ".cycle4-preregistration-generation-start.json"
    )
    root_binding_path = canonical_root / "cycle4-preregistration-root-binding.json"
    if summary.get("root_binding_path") != str(root_binding_path):
        raise ValueError(f"{split} trajectory root-binding path differs")

    root_binding, root_binding_raw = _read_json_artifact(root_binding_path)
    root_binding = _require_exact_fields(
        root_binding,
        {"schema", "root", "generation_start"},
        f"{split} trajectory root binding",
    )
    start_reference = _require_exact_fields(
        root_binding["generation_start"],
        {"path", "raw_artifact_sha256"},
        f"{split} trajectory start reference",
    )
    if (
        root_binding_raw != summary.get("root_binding_raw_artifact_sha256")
        or root_binding["schema"] != "spc-cycle4-trajectory-root-binding-v1"
        or root_binding["root"] != str(canonical_root)
        or start_reference["path"] != str(start_path)
        or start_reference["raw_artifact_sha256"]
        != summary.get("raw_artifact_sha256")
    ):
        raise ValueError(f"{split} trajectory root binding differs")

    start, start_raw = _read_json_artifact(start_path)
    start = _require_exact_fields(
        start,
        {
            "schema",
            "preregistration",
            "split",
            "root",
            "receipt",
            "attempt_start",
            "attempt_stop",
            "generation_contract_sha256",
            "operational",
        },
        f"{split} trajectory generation start",
    )
    expected_operational = {
        "shard_size": shared["shard_size"],
        "batch_size": shared["batch_size"],
        "workers": shared["workers"],
        "verify_payloads": shared["verify_payloads"],
        "count_unique_states": shared["count_unique_states"],
    }
    preregistration_identity = {
        "schema": preregistration.schema,
        "raw_artifact_sha256": preregistration.sha256,
    }
    if (
        start_raw != summary.get("raw_artifact_sha256")
        or start["schema"] != "spc-cycle4-trajectory-generation-start-v1"
        or start["preregistration"] != preregistration_identity
        or start["split"] != split
        or start["root"] != str(canonical_root)
        or start["attempt_start"] != split_contract["attempt_start"]
        or start["attempt_stop"] != split_contract["attempt_stop"]
        or start["generation_contract_sha256"] != expected_contract_sha
        or start["operational"] != expected_operational
    ):
        raise ValueError(f"{split} trajectory generation start differs")

    receipt_path = Path(start["receipt"])
    if (
        not receipt_path.is_absolute()
        or os.path.normpath(str(receipt_path)) != str(receipt_path)
        or str(receipt_path.expanduser().resolve()) != str(receipt_path)
        or canonical_root == receipt_path
        or canonical_root in receipt_path.parents
    ):
        raise ValueError(f"{split} trajectory receipt path is not canonical")
    if read_native_generation_contract(canonical_root).digest_hex != expected_contract_sha:
        raise ValueError(f"{split} actual generation contract differs")
    store = CorpusStore.open(canonical_root)
    verified_store = store.verify()
    if verified_store != summary.get("corpus"):
        raise ValueError(f"{split} current trajectory corpus differs")

    receipt, receipt_raw = _read_json_artifact(receipt_path)
    payload_verification = receipt.get("payload_verification")
    if (
        not isinstance(payload_verification, Mapping)
        or set(payload_verification)
        != {
            "records",
            "wins",
            "losses",
            "draws",
            "unique_states",
            "duplicate_states",
        }
        or any(type(payload_verification[name]) is not int for name in payload_verification)
        or any(value < 0 for value in payload_verification.values())
        or payload_verification["wins"]
        + payload_verification["losses"]
        + payload_verification["draws"]
        != payload_verification["records"]
        or payload_verification["unique_states"]
        + payload_verification["duplicate_states"]
        != payload_verification["records"]
        or payload_verification["records"] != verified_store["record_count"]
    ):
        raise ValueError(f"{split} trajectory payload verification differs")
    receipt_start = receipt.get("preregistration_generation_start")
    generation_contract = receipt.get("generation_contract")
    if (
        receipt_raw != summary.get("completion_receipt_raw_artifact_sha256")
        or receipt.get("format") != "spc-native-corpus-generation-receipt-v1"
        or receipt.get("root") != str(canonical_root)
        or receipt.get("planned_attempt_start") != split_contract["attempt_start"]
        or receipt.get("planned_attempt_stop") != split_contract["attempt_stop"]
        or receipt.get("planned_attempt_count") != split_contract["attempts"]
        or receipt.get("shard_size") != shared["shard_size"]
        or receipt.get("batch_size") != shared["batch_size"]
        or receipt.get("workers") != shared["workers"]
        or receipt.get("corpus") != verified_store
        or not isinstance(generation_contract, Mapping)
        or generation_contract.get("sha256") != expected_contract_sha
        or receipt_start
        != {
            "schema": start["schema"],
            "path": str(start_path),
            "raw_artifact_sha256": start_raw,
            "preregistration_raw_artifact_sha256": preregistration.sha256,
            "root_binding_path": str(root_binding_path),
            "root_binding_raw_artifact_sha256": root_binding_raw,
        }
    ):
        raise ValueError(f"{split} completed trajectory receipt differs")

    final_start, final_start_raw = _read_json_artifact(start_path)
    final_binding, final_binding_raw = _read_json_artifact(root_binding_path)
    final_receipt, final_receipt_raw = _read_json_artifact(receipt_path)
    if (
        final_start != start
        or final_start_raw != start_raw
        or final_binding != root_binding
        or final_binding_raw != root_binding_raw
        or final_receipt != receipt
        or final_receipt_raw != receipt_raw
        or store.verify() != verified_store
    ):
        raise ValueError(f"{split} trajectory publication changed during validation")


def _validate_raw_teacher_publication(
    corpus: Mapping[str, Any],
    preregistration: Preregistration,
    *,
    tier_name: str,
    supplied_path: Path,
    supplied_raw_sha256: str,
) -> dict[str, Any]:
    canonical_path = _canonical_cli_input(
        supplied_path, f"{tier_name} raw teacher publication"
    )
    if HEX_SHA256.fullmatch(supplied_raw_sha256) is None:
        raise ValueError(f"{tier_name} raw teacher SHA-256 is malformed")
    _validate_preregistered_teacher_tier_artifact(
        corpus,
        preregistration,
        tier_name=tier_name,
        require_semantic_augmentation=False,
    )
    generation = corpus["generation"]
    provenance = generation["preregistration_generation_provenance"]
    start_evidence = provenance["teacher_generation_start"]
    source_evidence = provenance["teacher_generation_source_binding"]
    start_path = Path(start_evidence["path"])
    expected_start_path = canonical_path.with_name(
        canonical_path.name + ".preregistration-start.json"
    )
    expected_source_path = canonical_path.with_name(
        canonical_path.name + ".preregistration-sources.json"
    )
    if start_path != expected_start_path or Path(source_evidence["path"]) != expected_source_path:
        raise ValueError(f"{tier_name} teacher publication sidecar path differs")
    start, start_raw_sha = _read_json_artifact(start_path)
    teacher = preregistration.manifest["teacher"]
    trajectory = preregistration.manifest["trajectory_corpora"]
    tier = teacher["tiers"][tier_name]
    expected_inputs = {
        "receipt_root": start.get("inputs", {}).get("receipt_root")
        if isinstance(start.get("inputs"), Mapping)
        else None,
        "teacher_profile": "preregistered-first-source-profile",
        "manual_forbidden_train_option_final_keys": [],
        "cross_tier_artifact": None,
        "development_exclusion_artifact": None,
    }
    expected_config = {
        "target_roots": tier["target_roots"],
        "train_roots": tier["train_roots"],
        "minimum_series": teacher["minimum_series"],
        "maximum_series": teacher["maximum_series"],
        "branch_cap": teacher["branch_cap"],
        "max_work": teacher["max_work"],
        "hard_negatives": teacher["hard_negatives"],
        "seed": teacher["selection_seed"],
        "workers": teacher["workers"],
        "train_attempts": trajectory["train"]["attempts"],
        "holdout_attempts": trajectory["sealed_holdout"]["attempts"],
    }
    start = _require_exact_fields(
        start,
        {
            "schema",
            "preregistration",
            "tier",
            "train_root",
            "sealed_holdout_root",
            "output",
            "config",
            "selection_mode",
            "tactical_gate",
            "inputs",
        },
        f"{tier_name} teacher generation-start artifact",
    )
    start_inputs = _require_exact_fields(
        start["inputs"],
        set(expected_inputs),
        f"{tier_name} teacher generation-start inputs",
    )
    receipt_root = Path(start_inputs["receipt_root"])
    if (
        not receipt_root.is_absolute()
        or os.path.normpath(str(receipt_root)) != str(receipt_root)
        or str(receipt_root.expanduser().resolve()) != str(receipt_root)
    ):
        raise ValueError(f"{tier_name} teacher receipt root is not canonical")
    expected_inputs["receipt_root"] = str(receipt_root)
    if (
        start_raw_sha != start_evidence["raw_artifact_sha256"]
        or start.get("schema") != "spc-cycle4-teacher-generation-start-v1"
        or start.get("preregistration")
        != {
            "schema": preregistration.schema,
            "raw_artifact_sha256": preregistration.sha256,
        }
        or start.get("tier") != tier_name
        or start.get("output") != str(canonical_path)
        or start["config"] != expected_config
        or start["selection_mode"] != tier["selection_mode"]
        or start["tactical_gate"] != tier["tactical_gate"]
    ):
        raise ValueError(f"{tier_name} teacher generation-start artifact differs")
    source_path = Path(source_evidence["path"])
    source, source_raw_sha = _read_json_artifact(source_path)
    source = _require_exact_fields(
        source,
        {
            "schema",
            "preregistration",
            "tier",
            "teacher_generation_start",
            "trajectory_generation_starts",
            "input_artifacts",
        },
        f"{tier_name} teacher generation source artifact",
    )
    if (
        source_raw_sha != source_evidence["raw_artifact_sha256"]
        or source["schema"] != "spc-cycle4-teacher-generation-sources-v1"
        or source["preregistration"] != start["preregistration"]
        or source["tier"] != tier_name
        or source["teacher_generation_start"] != start_evidence
        or source["trajectory_generation_starts"]
        != provenance["trajectory_generation_starts"]
    ):
        raise ValueError(f"{tier_name} teacher generation source artifact differs")
    input_artifacts = _require_exact_fields(
        source["input_artifacts"],
        {"cross_tier_artifact", "development_exclusion_artifact"},
        f"{tier_name} teacher input artifacts",
    )
    for name in ("cross_tier_artifact", "development_exclusion_artifact"):
        bound = input_artifacts[name]
        if bound is None:
            expected_inputs[name] = None
        else:
            binding = _require_exact_fields(
                bound,
                {"path", "corpus_id", "semantic_sha256", "raw_artifact_sha256"},
                f"{tier_name} teacher {name}",
            )
            expected_inputs[name] = binding["path"]
    if start_inputs != expected_inputs:
        raise ValueError(f"{tier_name} teacher generation-start inputs differ")
    train_root = Path(start["train_root"])
    holdout_root = Path(start["sealed_holdout_root"])
    if (
        train_root == holdout_root
        or train_root in holdout_root.parents
        or holdout_root in train_root.parents
    ):
        raise ValueError("teacher trajectory roots must be distinct and non-nested")
    completion_path = canonical_path.with_name(
        canonical_path.name + ".preregistration-completion.json"
    )
    artifact_paths = {
        canonical_path,
        start_path,
        source_path,
        completion_path,
        receipt_root,
        *(
            Path(value["path"])
            for value in input_artifacts.values()
            if isinstance(value, Mapping)
        ),
    }
    expected_artifact_count = 5 + sum(
        isinstance(value, Mapping) for value in input_artifacts.values()
    )
    if len(artifact_paths) != expected_artifact_count:
        raise ValueError("teacher protocol artifact paths must be distinct")
    if any(
        root == path or root in path.parents
        for root in (train_root, holdout_root)
        for path in artifact_paths
    ):
        raise ValueError(
            "teacher output, receipts, and inputs must be outside trajectory roots"
        )
    trajectory_starts = provenance["trajectory_generation_starts"]
    _validate_trajectory_generation_starts(trajectory_starts, preregistration)
    for split, root in (
        ("train", train_root),
        ("sealed_holdout", holdout_root),
    ):
        _validate_trajectory_publication_chain(
            root,
            trajectory_starts[split],
            preregistration,
            split=split,
        )
    cross_tier = input_artifacts["cross_tier_artifact"]
    if tier_name == "quiet_depth2":
        if cross_tier is not None:
            raise ValueError("quiet_depth2 teacher cross-tier input must be absent")
    else:
        cross_binding = _require_exact_fields(
            cross_tier,
            {"path", "corpus_id", "semantic_sha256", "raw_artifact_sha256"},
            "tactical_depth3 teacher cross-tier input",
        )
        cross_path = Path(cross_binding["path"])
        cross_payload, cross_raw_sha = _read_json_artifact(cross_path)
        if (
            cross_raw_sha != cross_binding["raw_artifact_sha256"]
            or cross_payload.get("corpus_id") != cross_binding["corpus_id"]
            or _teacher_semantic_sha256(cross_payload)
            != cross_binding["semantic_sha256"]
        ):
            raise ValueError("tactical_depth3 teacher cross-tier input differs")
        _validate_augmented_teacher_publication(
            cross_payload,
            preregistration,
            tier_name="quiet_depth2",
            supplied_path=cross_path,
            supplied_raw_sha256=cross_raw_sha,
        )
    development_source = preregistration.manifest["trajectory_corpora"]["train"].get(
        "artifact_source"
    )
    development = input_artifacts["development_exclusion_artifact"]
    if development_source is None:
        if development is not None:
            raise ValueError("teacher development exclusion must be absent")
    else:
        development_binding = _require_exact_fields(
            development,
            {"path", "corpus_id", "semantic_sha256", "raw_artifact_sha256"},
            f"{tier_name} teacher development exclusion input",
        )
        development_path = Path(development_binding["path"])
        development_payload, development_raw_sha = _read_json_artifact(
            development_path
        )
        if (
            development_raw_sha != development_binding["raw_artifact_sha256"]
            or development_payload.get("corpus_id")
            != development_binding["corpus_id"]
            or _teacher_semantic_sha256(development_payload)
            != development_binding["semantic_sha256"]
        ):
            raise ValueError(f"{tier_name} teacher development exclusion differs")
        _validate_development_import(
            development_payload,
            preregistration,
            supplied_path=development_path,
        )
    completion, completion_raw_sha = _read_json_artifact(completion_path)
    completion = _require_exact_fields(
        completion,
        {
            "schema",
            "preregistration",
            "tier",
            "teacher_generation_start",
            "teacher_generation_source_binding",
            "output",
        },
        f"{tier_name} teacher completion receipt",
    )
    expected_output = {
        "path": str(canonical_path),
        "corpus_id": corpus.get("corpus_id"),
        "semantic_sha256": _teacher_semantic_sha256(corpus),
        "raw_artifact_sha256": supplied_raw_sha256,
    }
    if (
        completion["schema"] != "spc-cycle4-teacher-generation-completion-v1"
        or completion["preregistration"] != start["preregistration"]
        or completion["tier"] != tier_name
        or completion["teacher_generation_start"] != start_evidence
        or completion["teacher_generation_source_binding"] != source_evidence
        or completion["output"] != expected_output
    ):
        raise ValueError(f"{tier_name} teacher completion binding differs")
    final_start, final_start_raw = _read_json_artifact(start_path)
    final_source, final_source_raw = _read_json_artifact(source_path)
    final_completion, final_completion_raw = _read_json_artifact(completion_path)
    if (
        final_start != start
        or final_start_raw != start_raw_sha
        or final_source != source
        or final_source_raw != source_raw_sha
        or final_completion != completion
        or final_completion_raw != completion_raw_sha
    ):
        raise ValueError(f"{tier_name} teacher publication changed during validation")
    return {
        "input_artifacts": input_artifacts,
        "trajectory_roots": {
            "train": str(train_root),
            "sealed_holdout": str(holdout_root),
        },
    }


def _validate_augmented_teacher_publication(
    corpus: Mapping[str, Any],
    preregistration: Preregistration,
    *,
    tier_name: str,
    supplied_path: Path,
    supplied_raw_sha256: str,
) -> dict[str, Any]:
    canonical_path = _canonical_cli_input(
        supplied_path, f"{tier_name} augmented teacher publication"
    )
    if HEX_SHA256.fullmatch(supplied_raw_sha256) is None:
        raise ValueError(f"{tier_name} augmented teacher SHA-256 is malformed")
    _validate_preregistered_teacher_tier_artifact(
        corpus,
        preregistration,
        tier_name=tier_name,
        require_semantic_augmentation=True,
    )
    provenance = corpus["generation"]["preregistration_generation_provenance"]
    start_evidence = provenance["teacher_semantic_augmentation_start"]
    source_evidence = provenance["teacher_semantic_augmentation_source_binding"]
    start_path = canonical_path.with_name(
        canonical_path.name + ".preregistration-start.json"
    )
    source_path = canonical_path.with_name(
        canonical_path.name + ".preregistration-sources.json"
    )
    if (
        Path(start_evidence["path"]) != start_path
        or Path(source_evidence["path"]) != source_path
    ):
        raise ValueError(f"{tier_name} semantic augmentation sidecar path differs")
    start, start_raw_sha = _read_json_artifact(start_path)
    start = _require_exact_fields(
        start,
        {
            "schema",
            "preregistration",
            "tier",
            "input",
            "train_root",
            "sealed_holdout_root",
            "output",
            "receipt",
            "source_binding",
        },
        f"{tier_name} semantic augmentation-start artifact",
    )
    if (
        start_raw_sha != start_evidence["raw_artifact_sha256"]
        or start.get("schema")
        != "spc-cycle4-teacher-semantic-augmentation-start-v1"
        or start.get("preregistration")
        != {
            "schema": preregistration.schema,
            "raw_artifact_sha256": preregistration.sha256,
        }
        or start.get("tier") != tier_name
        or start.get("output") != str(canonical_path)
        or start.get("source_binding") != source_evidence["path"]
    ):
        raise ValueError(f"{tier_name} semantic augmentation-start artifact differs")
    source, source_raw_sha = _read_json_artifact(source_path)
    source = _require_exact_fields(
        source,
        {
            "schema",
            "preregistration",
            "tier",
            "augmentation_start",
            "input",
            "trajectory_corpora",
            "output",
            "receipt",
        },
        f"{tier_name} semantic augmentation source artifact",
    )
    if (
        source_raw_sha != source_evidence["raw_artifact_sha256"]
        or source["schema"]
        != "spc-cycle4-teacher-semantic-augmentation-sources-v1"
        or source["preregistration"] != start["preregistration"]
        or source["tier"] != tier_name
        or source["augmentation_start"] != start_evidence
        or source["output"] != str(canonical_path)
        or source["receipt"] != start.get("receipt")
    ):
        raise ValueError(f"{tier_name} semantic augmentation source artifact differs")
    source_input = _require_exact_fields(
        source["input"],
        {"path", "corpus_id", "semantic_sha256", "raw_artifact_sha256"},
        f"{tier_name} semantic augmentation raw input",
    )
    raw_path = _canonical_cli_input(
        Path(source_input["path"]), f"{tier_name} semantic augmentation raw input"
    )
    train_root = _canonical_cli_input(
        Path(start["train_root"]), f"{tier_name} semantic augmentation train root"
    )
    holdout_root = _canonical_cli_input(
        Path(start["sealed_holdout_root"]),
        f"{tier_name} semantic augmentation holdout root",
    )
    receipt_path = _canonical_cli_input(
        Path(source["receipt"]), f"{tier_name} semantic augmentation receipt"
    )
    if (
        train_root == holdout_root
        or train_root in holdout_root.parents
        or holdout_root in train_root.parents
    ):
        raise ValueError("semantic augmentation trajectory roots must be distinct")
    protocol_paths = {
        canonical_path,
        start_path,
        source_path,
        raw_path,
        receipt_path,
    }
    if len(protocol_paths) != 5 or any(
        root == path or root in path.parents
        for root in (train_root, holdout_root)
        for path in protocol_paths
    ):
        raise ValueError("semantic augmentation artifact paths overlap")
    raw_payload, raw_sha = _read_json_artifact(raw_path)
    if (
        start.get("input") != str(raw_path)
        or raw_sha != source_input["raw_artifact_sha256"]
        or raw_payload.get("corpus_id") != source_input["corpus_id"]
        or _teacher_semantic_sha256(raw_payload)
        != source_input["semantic_sha256"]
    ):
        raise ValueError(f"{tier_name} semantic augmentation raw input differs")
    raw_publication = _validate_raw_teacher_publication(
        raw_payload,
        preregistration,
        tier_name=tier_name,
        supplied_path=raw_path,
        supplied_raw_sha256=raw_sha,
    )
    raw_provenance = raw_payload["generation"][
        "preregistration_generation_provenance"
    ]
    trajectory_sources = _require_exact_fields(
        source["trajectory_corpora"],
        {"train", "sealed_holdout"},
        f"{tier_name} semantic augmentation trajectory sources",
    )
    for split in ("train", "sealed_holdout"):
        trajectory_source = _require_exact_fields(
            trajectory_sources[split],
            {
                "root",
                "generation_contract_sha256",
                "corpus",
                "generation_start",
            },
            f"{tier_name} {split} semantic augmentation trajectory source",
        )
        if (
            trajectory_source["root"]
            != str(train_root if split == "train" else holdout_root)
            or trajectory_source["root"]
            != raw_publication["trajectory_roots"][split]
            or
            trajectory_source["generation_contract_sha256"]
            != _expected_generation_contract_sha256(
                preregistration, split=split
            )
            or trajectory_source["generation_start"]
            != raw_provenance["trajectory_generation_starts"][split]
            or trajectory_source["corpus"]
            != raw_provenance["trajectory_generation_starts"][split]["corpus"]
        ):
            raise ValueError(
                f"{tier_name} {split} semantic trajectory binding differs"
            )
    semantic_contract = corpus["semantic_state_contract"]
    if (
        semantic_contract["input_raw_artifact_sha256"]
        != source_input["raw_artifact_sha256"]
        or semantic_contract["input_semantic_sha256"]
        != source_input["semantic_sha256"]
        or corpus.get("source_corpus_id") != source_input["corpus_id"]
    ):
        raise ValueError(f"{tier_name} semantic augmentation lineage differs")
    receipt, receipt_raw_sha = _read_json_artifact(receipt_path)
    expected_output = {
        "path": str(canonical_path),
        "corpus_id": corpus.get("corpus_id"),
        "semantic_sha256": _teacher_semantic_sha256(corpus),
        "raw_artifact_sha256": supplied_raw_sha256,
    }
    expected_replay = {
        **dict(semantic_contract),
        "source_corpus_id": corpus.get("source_corpus_id"),
        "augmented_corpus_id": corpus.get("corpus_id"),
    }
    if (
        receipt.get("schema")
        != "spc-cycle4-teacher-semantic-augmentation-receipt-v1"
        or receipt.get("preregistration") != start["preregistration"]
        or receipt.get("tier") != tier_name
        or receipt.get("augmentation_start") != start_evidence
        or receipt.get("augmentation_source_binding") != source_evidence
        or receipt.get("input") != source["input"]
        or receipt.get("trajectory_corpora") != source["trajectory_corpora"]
        or receipt.get("output") != expected_output
        or receipt.get("semantic_replay") != expected_replay
    ):
        raise ValueError(f"{tier_name} semantic augmentation receipt differs")
    final_start, final_start_raw = _read_json_artifact(start_path)
    final_source, final_source_raw = _read_json_artifact(source_path)
    final_raw_payload, final_raw_sha = _read_json_artifact(raw_path)
    final_receipt, final_receipt_raw = _read_json_artifact(receipt_path)
    if (
        final_start != start
        or final_start_raw != start_raw_sha
        or final_source != source
        or final_source_raw != source_raw_sha
        or final_raw_payload != raw_payload
        or final_raw_sha != raw_sha
        or final_receipt != receipt
        or final_receipt_raw != receipt_raw_sha
    ):
        raise ValueError(f"{tier_name} semantic augmentation changed during validation")
    return {
        "raw_teacher_input_artifacts": raw_publication["input_artifacts"],
        "raw_teacher_path": str(raw_path),
        "raw_teacher_sha256": raw_sha,
        "augmentation_receipt_path": str(receipt_path),
        "augmentation_receipt_raw_artifact_sha256": receipt_raw_sha,
    }


def _validate_combined_corpus_preregistration(
    corpus: Mapping[str, Any],
    preregistration: Preregistration,
    *,
    supplied_path: Path | None = None,
    supplied_raw_sha256: str | None = None,
) -> None:
    manifest = preregistration.manifest
    if corpus.get("schema") != CORPUS_SCHEMA or corpus.get("method") != CORPUS_METHOD:
        raise ValueError("combined teacher schema or method differs")
    if corpus.get("engine_version") != ENGINE_VERSION:
        raise ValueError("combined teacher engine version differs from preregistration")
    if corpus.get("source_fingerprint") != ENGINE_SOURCE_FINGERPRINT:
        raise ValueError("combined teacher source fingerprint differs from preregistration")

    schedule = manifest["profiles"]["ordered_source_schedule"]
    ordered_profile_ids = [str(entry["profile_id"]) for entry in schedule]
    generation = corpus.get("generation")
    if not isinstance(generation, Mapping):
        raise ValueError("combined teacher generation binding is missing")
    publication_binding = _validate_preregistered_generation_provenance(
        generation, preregistration
    )
    if (supplied_path is None) != (supplied_raw_sha256 is None):
        raise ValueError("combined teacher path and raw SHA must be supplied together")
    if supplied_path is not None and supplied_raw_sha256 is not None:
        canonical_path = _canonical_cli_input(
            supplied_path, "combined teacher publication"
        )
        merge_start = publication_binding["merge_start"]
        merge_start_path = canonical_path.with_name(
            canonical_path.name + ".preregistration-start.json"
        )
        merge_source_path = canonical_path.with_name(
            canonical_path.name + ".preregistration-sources.json"
        )
        if (
            str(canonical_path) != merge_start["output"]
            or publication_binding["merge_start_evidence"]["path"]
            != str(merge_start_path)
            or merge_start["source_binding"] != str(merge_source_path)
            or publication_binding["merge_source_evidence"]["path"]
            != str(merge_source_path)
        ):
            raise ValueError("combined teacher merge sidecar paths differ")
        if HEX_SHA256.fullmatch(supplied_raw_sha256) is None:
            raise ValueError("combined teacher raw SHA-256 is malformed")
        completion_path = canonical_path.with_name(
            canonical_path.name + ".preregistration-completion.json"
        )
        completion, completion_raw_sha = _read_json_artifact(completion_path)
        completion = _require_exact_fields(
            completion,
            {
                "schema",
                "preregistration",
                "merge_start",
                "merge_source_binding",
                "output",
            },
            "teacher merge completion",
        )
        expected_output = {
            "path": str(canonical_path),
            "corpus_id": corpus.get("corpus_id"),
            "semantic_sha256": _teacher_semantic_sha256(corpus),
            "raw_artifact_sha256": supplied_raw_sha256,
        }
        if (
            completion["schema"] != "spc-cycle4-teacher-merge-completion-v1"
            or completion["preregistration"]
            != {
                "schema": preregistration.schema,
                "raw_artifact_sha256": preregistration.sha256,
            }
            or completion["merge_start"]
            != publication_binding["merge_start_evidence"]
            or completion["merge_source_binding"]
            != publication_binding["merge_source_evidence"]
            or completion["output"] != expected_output
        ):
            raise ValueError("teacher merge completion binding differs")
        _validate_merge_tier_publications(publication_binding, preregistration)
        final_start, final_start_raw = _read_json_artifact(merge_start_path)
        final_sources, final_sources_raw = _read_json_artifact(merge_source_path)
        final_completion, final_completion_raw = _read_json_artifact(completion_path)
        final_corpus, final_corpus_raw = _read_json_artifact(canonical_path)
        if (
            final_start != merge_start
            or final_start_raw
            != publication_binding["merge_start_evidence"]["raw_artifact_sha256"]
            or final_sources != publication_binding["merge_sources"]
            or final_sources_raw
            != publication_binding["merge_sources_raw_artifact_sha256"]
            or final_completion != completion
            or final_completion_raw != completion_raw_sha
            or final_corpus != corpus
            or final_corpus_raw != supplied_raw_sha256
        ):
            raise ValueError("teacher merge publication changed during validation")
    trajectory = manifest["trajectory_corpora"]
    generation_expected = {
        "train_contract_sha256": _expected_generation_contract_sha256(
            preregistration, split="train"
        ),
        "holdout_contract_sha256": _expected_generation_contract_sha256(
            preregistration, split="sealed_holdout"
        ),
        "ordered_profile_ids": ordered_profile_ids,
        "profile_schedule": trajectory["shared_config"]["profile_schedule"],
        "train_attempts": trajectory["train"]["attempts"],
        "holdout_attempts": trajectory["sealed_holdout"]["attempts"],
        "train_attempt_start": trajectory["train"]["attempt_start"],
        "train_attempt_stop": trajectory["train"]["attempt_stop"],
        "holdout_attempt_start": trajectory["sealed_holdout"]["attempt_start"],
        "holdout_attempt_stop": trajectory["sealed_holdout"]["attempt_stop"],
        "development_holdout_exclusion_sha256": trajectory[
            "sealed_holdout"
        ]["development_exclusion_sha256"],
    }
    for name, expected in generation_expected.items():
        if generation.get(name) != expected:
            raise ValueError(f"combined teacher generation {name} drifted")
    for name in ("train_corpus_sha256", "holdout_corpus_sha256"):
        digest = generation.get(name)
        if not isinstance(digest, str) or HEX_SHA256.fullmatch(digest) is None:
            raise ValueError(f"combined teacher generation {name} is malformed")

    teacher = manifest["teacher"]
    prior_reuse = teacher.get("prior_receipt_cache_reuse")
    if generation.get("prior_receipt_cache_reuse") is not prior_reuse:
        raise ValueError("combined teacher prior receipt-cache reuse drifted")
    teacher_profile = corpus.get("teacher_profile")
    expected_teacher_profile = _preregistered_source_profiles(preregistration)[
        0
    ].as_dict()
    if not isinstance(teacher_profile, Mapping) or dict(teacher_profile) != expected_teacher_profile:
        raise ValueError("combined teacher profile differs from preregistration")

    tiers = corpus.get("tiers")
    if not isinstance(tiers, Mapping):
        raise ValueError("combined teacher tier bindings are missing")
    tier_contracts = {
        "quiet_d2": teacher["tiers"]["quiet_depth2"],
        "tactical_d3": teacher["tiers"]["tactical_depth3"],
    }
    for tier_name, preregistered in tier_contracts.items():
        tier = tiers.get(tier_name)
        config = tier.get("config") if isinstance(tier, Mapping) else None
        if not isinstance(config, Mapping) or not isinstance(preregistered, Mapping):
            raise ValueError(f"combined teacher {tier_name} config is missing")
        expected_config = {
            "target_roots": preregistered["target_roots"],
            "train_roots": preregistered["train_roots"],
            "selection_mode": preregistered["selection_mode"],
            "depth_series": 2 if tier_name == "quiet_d2" else 3,
            "minimum_series": teacher["minimum_series"],
            "maximum_series": teacher["maximum_series"],
            "branch_cap": teacher["branch_cap"],
            "max_generation_positions": teacher["max_work"],
            "hard_negative_count": teacher["hard_negatives"],
            "seed": teacher["selection_seed"],
            "workers": teacher["workers"],
            "expected_train_attempts": trajectory["train"]["attempts"],
            "expected_holdout_attempts": trajectory["sealed_holdout"]["attempts"],
        }
        for name, expected in expected_config.items():
            if config.get(name) != expected:
                raise ValueError(f"combined teacher {tier_name} config {name} drifted")
        if set(config) != set(expected_config):
            raise ValueError(f"combined teacher {tier_name} config fields drifted")
        tier_quality = tier.get("quality")
        expected_tier_quality = {
            "accepted_roots": preregistered["target_roots"],
            "train_roots": preregistered["train_roots"],
            "holdout_roots": preregistered["holdout_roots"],
        }
        if (
            not isinstance(tier_quality, Mapping)
            or tier_quality.get("status") != "complete"
            or any(
                tier_quality.get(name) != expected
                for name, expected in expected_tier_quality.items()
            )
        ):
            raise ValueError(f"combined teacher {tier_name} quality drifted")
        _validate_tactical_gate_evidence(
            tier_quality,
            str(preregistered["tactical_gate"]),
            f"combined teacher {tier_name}",
        )
        tier_safety = tier.get("contract")
        if not isinstance(tier_safety, Mapping) or any(
            tier_safety.get(name) is not expected
            for name, expected in {
                "incomplete_labels_cached": False,
                "full_retained_root_scores_required": True,
            }.items()
        ):
            raise ValueError(f"combined teacher {tier_name} contract drifted")

    quality = corpus.get("quality")
    expected_quality = {
        "accepted_roots": teacher["expected_merged_roots"],
        "train_roots": teacher["expected_merged_train_roots"],
        "holdout_roots": teacher["expected_merged_holdout_roots"],
    }
    if not isinstance(quality, Mapping) or any(
        quality.get(name) != expected for name, expected in expected_quality.items()
    ):
        raise ValueError("combined teacher merged quality counts drifted")
    if quality.get("status") != "complete":
        raise ValueError("combined teacher merged quality is incomplete")

    selection = corpus.get("selection")
    if not isinstance(selection, Mapping) or any(
        selection.get(name) != 0
        for name in (
            "selected_root_exact_overlap_states",
            "cross_split_option_final_exact_overlap_states",
            "train_option_final_to_holdout_root_overlap_states",
            "holdout_option_final_to_train_root_overlap_states",
        )
    ):
        raise ValueError("combined teacher leakage contract drifted")
    safety = corpus.get("contract")
    if not isinstance(safety, Mapping) or any(
        safety.get(name) is not expected
        for name, expected in {
            "incomplete_labels_cached": False,
            "full_retained_root_scores_required": True,
            "depth_is_per_label_provenance": True,
            "cross_depth_quality_metrics_blended": False,
            "train_holdout_exact_leakage_allowed": False,
            "strength_claim": False,
        }.items()
    ):
        raise ValueError("combined teacher safety contract drifted")

    raw_labels = corpus.get("labels")
    if not isinstance(raw_labels, list):
        raise ValueError("combined teacher labels are malformed")
    label_profile_ids = {
        raw.get("source_profile_id")
        for raw in raw_labels
        if isinstance(raw, Mapping)
    }
    if label_profile_ids != set(ordered_profile_ids):
        raise ValueError("combined teacher label profile IDs drifted")
    attempt_ranges = {
        "train": (
            trajectory["train"]["attempt_start"],
            trajectory["train"]["attempt_stop"],
        ),
        "holdout": (
            trajectory["sealed_holdout"]["attempt_start"],
            trajectory["sealed_holdout"]["attempt_stop"],
        ),
    }
    for raw in raw_labels:
        if not isinstance(raw, Mapping):
            raise ValueError("combined teacher label is malformed")
        split = raw.get("split")
        attempt = raw.get("attempt_index")
        if split not in attempt_ranges or type(attempt) is not int:
            raise ValueError("combined teacher label attempt provenance is malformed")
        start, stop = attempt_ranges[str(split)]
        if not start <= attempt < stop:
            raise ValueError("combined teacher label attempt range drifted")
        if (
            raw.get("white_profile_id") not in ordered_profile_ids
            or raw.get("black_profile_id") not in ordered_profile_ids
        ):
            raise ValueError("combined teacher label game-profile IDs drifted")
    tier_counts = {
        tier_name: sum(
            1
            for raw in raw_labels
            if isinstance(raw, Mapping) and raw.get("teacher_tier") == tier_name
        )
        for tier_name in tier_contracts
    }
    if any(
        tier_counts[tier_name] != contract["target_roots"]
        for tier_name, contract in tier_contracts.items()
    ):
        raise ValueError("combined teacher label tier counts drifted")
    expected_depths = {"quiet_d2": 2, "tactical_d3": 3}
    if any(
        raw.get("teacher_depth_series") != expected_depths.get(
            str(raw.get("teacher_tier"))
        )
        for raw in raw_labels
        if isinstance(raw, Mapping)
    ):
        raise ValueError("combined teacher label tier/depth provenance drifted")
    if supplied_path is not None and supplied_raw_sha256 is not None:
        # Close the mutation window after every recursive/config/label check,
        # not merely after the earlier publication-lineage validation.
        final_start, final_start_raw = _read_json_artifact(merge_start_path)
        final_sources, final_sources_raw = _read_json_artifact(merge_source_path)
        final_completion, final_completion_raw = _read_json_artifact(completion_path)
        final_corpus, final_corpus_raw = _read_json_artifact(canonical_path)
        if (
            final_start != merge_start
            or final_start_raw
            != publication_binding["merge_start_evidence"]["raw_artifact_sha256"]
            or final_sources != publication_binding["merge_sources"]
            or final_sources_raw
            != publication_binding["merge_sources_raw_artifact_sha256"]
            or final_completion != completion
            or final_completion_raw != completion_raw_sha
            or final_corpus != corpus
            or final_corpus_raw != supplied_raw_sha256
        ):
            raise ValueError("teacher merge publication changed during validation")


def _raw_semantic_commitment(raw_labels: Sequence[object]) -> tuple[str, set[str], set[str]]:
    roots: set[str] = set()
    finals: set[str] = set()
    for raw in raw_labels:
        if not isinstance(raw, Mapping):
            raise ValueError("teacher artifact labels must be objects")
        root = raw.get("state_key_sha256")
        if not isinstance(root, str) or HEX_SHA256.fullmatch(root) is None:
            raise ValueError("teacher artifact root key is malformed")
        if root in roots:
            raise ValueError(f"duplicate teacher artifact root key: {root}")
        roots.add(root)
        options = raw.get("options")
        if not isinstance(options, list) or not options:
            raise ValueError(f"teacher artifact label has no options: {root}")
        for option in options:
            if not isinstance(option, Mapping):
                raise ValueError(f"teacher artifact option is malformed: {root}")
            final = option.get("final_state_key_sha256")
            if not isinstance(final, str) or HEX_SHA256.fullmatch(final) is None:
                raise ValueError(f"teacher artifact final key is malformed: {root}")
            finals.add(final)
    commitment = hashlib.sha256(
        _canonical_json(
            {
                "root_state_keys": sorted(roots),
                "option_final_state_keys": sorted(finals),
            }
        )
    ).hexdigest()
    return commitment, roots, finals


def _raw_label_payload_commitment(raw_labels: Sequence[object]) -> str:
    """Bind the complete semantic teacher labels, not only their state keys."""

    normalized: list[Any] = []
    for raw in raw_labels:
        if not isinstance(raw, Mapping):
            raise ValueError("teacher artifact labels must be objects")
        root = raw.get("state_key_sha256")
        if not isinstance(root, str) or HEX_SHA256.fullmatch(root) is None:
            raise ValueError("teacher artifact root key is malformed")
        normalized.append(_semantic_teacher_value(raw))
    normalized.sort(key=lambda item: str(item["state_key_sha256"]))
    return hashlib.sha256(
        _canonical_json(
            {
                "schema": "spc-deep-teacher-label-payload-commitment-v1",
                "labels": normalized,
            }
        )
    ).hexdigest()


SOURCE_BINDING_FIELDS = {
    "corpus_id",
    "semantic_sha256",
    "raw_artifact_sha256",
}


def _validated_source_binding(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != SOURCE_BINDING_FIELDS:
        raise ValueError(f"{label} source binding fields differ")
    corpus_id = value.get("corpus_id")
    semantic_sha = value.get("semantic_sha256")
    raw_sha = value.get("raw_artifact_sha256")
    if (
        not isinstance(corpus_id, str)
        or not corpus_id.startswith("spc-native-mixed-teacher-")
        or not isinstance(semantic_sha, str)
        or HEX_SHA256.fullmatch(semantic_sha) is None
        or not isinstance(raw_sha, str)
        or HEX_SHA256.fullmatch(raw_sha) is None
    ):
        raise ValueError(f"{label} source binding is malformed")
    return {
        "corpus_id": corpus_id,
        "semantic_sha256": semantic_sha,
        "raw_artifact_sha256": raw_sha,
    }


def _dataset_pairing_sha256(
    *,
    preregistration_sha256: str,
    train_semantic_keys_sha256: str,
    holdout_semantic_keys_sha256: str,
    train_label_payload_sha256: str,
    holdout_label_payload_sha256: str,
    cross_split_audit_sha256: str,
    train_source: Mapping[str, str],
    holdout_source: Mapping[str, str],
    source_pairing_mode: str,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "schema": "spc-deep-teacher-dataset-pair-v3",
                "preregistration_sha256": preregistration_sha256,
                "source_pairing_mode": source_pairing_mode,
                "train_source_semantic": {
                    name: train_source[name]
                    for name in ("corpus_id", "semantic_sha256")
                },
                "sealed_holdout_source_semantic": {
                    name: holdout_source[name]
                    for name in ("corpus_id", "semantic_sha256")
                },
                "train_semantic_keys_sha256": train_semantic_keys_sha256,
                "sealed_holdout_semantic_keys_sha256": (
                    holdout_semantic_keys_sha256
                ),
                "train_label_payload_sha256": train_label_payload_sha256,
                "sealed_holdout_label_payload_sha256": (
                    holdout_label_payload_sha256
                ),
                "cross_split_audit_sha256": cross_split_audit_sha256,
            }
        )
    ).hexdigest()


def _require_clean_cross_artifact_split(
    *,
    train_roots: set[str],
    train_finals: set[str],
    holdout_roots: set[str],
    holdout_finals: set[str],
) -> dict[str, list[str]]:
    intersections = {
        "train_root_to_holdout_root": sorted(train_roots & holdout_roots),
        "train_final_to_holdout_final": sorted(train_finals & holdout_finals),
        "train_final_to_holdout_root": sorted(train_finals & holdout_roots),
        "holdout_final_to_train_root": sorted(holdout_finals & train_roots),
    }
    if any(intersections.values()):
        raise ValueError("train/sealed-holdout semantic contamination detected")
    return intersections


def _read_declared_artifact_source(
    preregistration: Preregistration,
    declared_source: Mapping[str, Any],
) -> Mapping[str, Any]:
    _validate_consumption_evidence(declared_source)
    source_path = Path(str(declared_source["path"]))
    source, raw_sha = _read_json_artifact(source_path)
    if raw_sha != declared_source["raw_artifact_sha256"]:
        raise ValueError("preregistered artifact source raw bytes changed")
    if _teacher_semantic_sha256(source) != declared_source["semantic_sha256"]:
        raise ValueError("preregistered artifact source semantic payload changed")
    if source.get("corpus_id") != declared_source["corpus_id"]:
        raise ValueError("preregistered artifact source corpus ID changed")
    labels = source.get("labels")
    if not isinstance(labels, list) or len(labels) != declared_source["label_count"]:
        raise ValueError("preregistered artifact source label count changed")
    return source


def _validate_consumption_evidence(
    declared_source: Mapping[str, Any],
    *,
    evidence_snapshot: tuple[Mapping[str, Any], str] | None = None,
) -> None:
    evidence = declared_source["consumption_evidence"]
    if not isinstance(evidence, Mapping):
        raise ValueError("development consumption evidence is malformed")
    path = Path(str(evidence["path"]))
    receipt, raw_sha = (
        evidence_snapshot
        if evidence_snapshot is not None
        else _read_json_artifact(path)
    )
    if raw_sha != evidence["raw_artifact_sha256"]:
        raise ValueError("development consumption evidence raw bytes changed")
    if raw_sha != CYCLE3_CONSUMPTION_RESULT_RAW_SHA256:
        raise ValueError(
            "development consumption evidence is not the exact committed cycle-3 result"
        )
    if receipt.get("schema") != evidence["schema"]:
        raise ValueError("development consumption evidence schema changed")
    corpora = receipt.get("corpora")
    if not isinstance(corpora, Mapping):
        raise ValueError("development consumption evidence corpus binding is missing")
    if (
        declared_source["raw_artifact_sha256"]
        != CYCLE3_CONSUMED_CORPUS_RAW_SHA256
        or corpora.get("mixed_teacher_sha256")
        != CYCLE3_CONSUMED_CORPUS_RAW_SHA256
    ):
        raise ValueError("development source is not the consumed corpus in its evidence")
    train_labels = corpora.get("train_labels")
    holdout_labels = corpora.get("holdout_labels")
    holdout_metrics = receipt.get("one_shot_holdout_metrics")
    receipt_hashes = receipt.get("receipts")
    verdict = receipt.get("verdict")
    if (
        type(train_labels) is not int
        or type(holdout_labels) is not int
        or train_labels != CYCLE3_CONSUMED_TRAIN_LABELS
        or holdout_labels != CYCLE3_CONSUMED_HOLDOUT_LABELS
        or train_labels + holdout_labels != declared_source["label_count"]
        or declared_source["label_count"] != CYCLE3_CONSUMED_DEVELOPMENT_LABELS
        or not isinstance(holdout_metrics, Mapping)
        or set(holdout_metrics)
        != {
            "baseline",
            "rejected_development_leader",
            "primary_nonroute",
            "route_ablation",
            "distilled_seven_weight",
        }
        or not isinstance(receipt_hashes, Mapping)
        or set(receipt_hashes)
        != {"fit_sha256", "holdout_claim_sha256", "holdout_evaluation_sha256"}
        or any(
            not isinstance(value, str) or HEX_SHA256.fullmatch(value) is None
            for value in receipt_hashes.values()
        )
        or not isinstance(verdict, Mapping)
        or verdict.get("deployed") is not False
        or verdict.get("promotion_recommended") is not False
    ):
        raise ValueError("development consumption evidence does not prove a completed holdout")


def _require_exact_canonical_input(path: Path, expected: str, label: str) -> Path:
    supplied = str(path)
    resolved = str(path.expanduser().resolve())
    if not path.is_absolute() or supplied != resolved or supplied != expected:
        raise ValueError(f"{label} path must exactly match its canonical preregistration path")
    return path


def _require_labels_derived_from_declared_source(
    raw_labels: Sequence[object],
    source: Mapping[str, Any],
    declared_source: Mapping[str, Any],
    *,
    development_relabel: bool,
    artifact_split: str,
) -> None:
    source_labels = source.get("labels")
    if not isinstance(source_labels, list) or not source_labels:
        raise ValueError("preregistered artifact source labels are missing")
    if not development_relabel:
        expected_split = "train" if artifact_split == "train" else "holdout"
        source_labels = [
            raw
            for raw in source_labels
            if isinstance(raw, Mapping) and raw.get("split") == expected_split
        ]
    source_by_root = {
        str(raw.get("state_key_sha256")): raw
        for raw in source_labels
        if isinstance(raw, Mapping)
    }
    if len(source_by_root) != len(source_labels) or len(raw_labels) != len(source_labels):
        raise ValueError("teacher split does not exactly cover its declared source labels")
    seen: set[str] = set()
    for raw in raw_labels:
        if not isinstance(raw, Mapping):
            raise ValueError("teacher split label provenance is malformed")
        root = str(raw.get("state_key_sha256"))
        original = source_by_root.get(root)
        if original is None or root in seen:
            raise ValueError("teacher split label is absent from its declared source")
        seen.add(root)
        candidate = dict(raw)
        if development_relabel:
            provenance = candidate.pop("development_provenance", None)
            if not isinstance(provenance, Mapping) or set(provenance) != {
                "schema",
                "original_split",
                "original_artifact_semantic_sha256",
                "original_artifact_raw_sha256",
                "consumption_evidence_raw_sha256",
            }:
                raise ValueError("declared development train provenance is missing")
            if (
                provenance.get("schema") != DEVELOPMENT_PROVENANCE_SCHEMA
                or provenance.get("original_split") not in {"train", "holdout"}
                or provenance.get("original_artifact_semantic_sha256")
                != declared_source["semantic_sha256"]
                or provenance.get("original_artifact_raw_sha256")
                != declared_source["raw_artifact_sha256"]
                or provenance.get("consumption_evidence_raw_sha256")
                != declared_source["consumption_evidence"]["raw_artifact_sha256"]
            ):
                raise ValueError("declared development train provenance differs")
            candidate["split"] = provenance["original_split"]
        if candidate != dict(original):
            raise ValueError("teacher split label payload differs from declared source")


def _validate_split_preregistration_provenance(
    corpus: Mapping[str, Any],
    preregistration: Preregistration,
    *,
    artifact_split: str,
    raw_labels: Sequence[object],
    declared_source_snapshot: Mapping[str, Any] | None = None,
    reopen_external_lineage: bool = True,
) -> None:
    """Reject split JSON that bypassed the preregistered generation boundary."""

    if corpus.get("schema") != CORPUS_SCHEMA or corpus.get("method") != CORPUS_METHOD:
        raise ValueError("teacher split schema or method differs from preregistration")
    if (
        corpus.get("engine_version") != ENGINE_VERSION
        or corpus.get("source_fingerprint") != ENGINE_SOURCE_FINGERPRINT
    ):
        raise ValueError("teacher split source identity differs from preregistration")

    manifest = preregistration.manifest
    trajectory_name = "train" if artifact_split == "train" else "sealed_holdout"
    trajectory = manifest["trajectory_corpora"]
    declared_source = trajectory[trajectory_name].get("artifact_source")
    if declared_source is not None and reopen_external_lineage:
        source = (
            declared_source_snapshot
            if declared_source_snapshot is not None
            else _read_declared_artifact_source(preregistration, declared_source)
        )
        _require_labels_derived_from_declared_source(
            raw_labels,
            source,
            declared_source,
            development_relabel=artifact_split == "train",
            artifact_split=artifact_split,
        )
    if artifact_split == "train" and declared_source is not None:
        return

    schedule = manifest["profiles"]["ordered_source_schedule"]
    ordered_profile_ids = [str(entry["profile_id"]) for entry in schedule]
    generation = corpus.get("generation")
    if not isinstance(generation, Mapping):
        raise ValueError("teacher split generation provenance is missing")
    if reopen_external_lineage:
        _validate_preregistered_generation_provenance(generation, preregistration)
    else:
        embedded_provenance = _require_exact_fields(
            generation.get("preregistration_generation_provenance"),
            {
                "schema",
                "preregistration",
                "trajectory_generation_starts",
                "teacher_generation_starts",
                "teacher_generation_source_bindings",
                "semantic_augmentation_starts",
                "semantic_augmentation_source_bindings",
                "merge_generation_start",
                "merge_generation_source_binding",
            },
            "embedded generation preregistration provenance",
        )
        _validate_preregistered_identity(embedded_provenance, preregistration)
    generation_expected = {
        "ordered_profile_ids": ordered_profile_ids,
        "profile_schedule": trajectory["shared_config"]["profile_schedule"],
        "train_attempts": trajectory["train"]["attempts"],
        "holdout_attempts": trajectory["sealed_holdout"]["attempts"],
        "train_attempt_start": trajectory["train"]["attempt_start"],
        "train_attempt_stop": trajectory["train"]["attempt_stop"],
        "holdout_attempt_start": trajectory["sealed_holdout"]["attempt_start"],
        "holdout_attempt_stop": trajectory["sealed_holdout"]["attempt_stop"],
        "development_holdout_exclusion_sha256": trajectory[
            "sealed_holdout"
        ]["development_exclusion_sha256"],
        "prior_receipt_cache_reuse": manifest["teacher"][
            "prior_receipt_cache_reuse"
        ],
    }
    if reopen_external_lineage:
        generation_expected.update(
            {
                "train_contract_sha256": _expected_generation_contract_sha256(
                    preregistration, split="train"
                ),
                "holdout_contract_sha256": _expected_generation_contract_sha256(
                    preregistration, split="sealed_holdout"
                ),
            }
        )
    else:
        for name in ("train_contract_sha256", "holdout_contract_sha256"):
            digest = generation.get(name)
            if not isinstance(digest, str) or HEX_SHA256.fullmatch(digest) is None:
                raise ValueError(f"teacher split generation {name} is malformed")
    for name, expected in generation_expected.items():
        if generation.get(name) != expected:
            raise ValueError(f"teacher split generation {name} drifted")
    for name in ("train_corpus_sha256", "holdout_corpus_sha256"):
        digest = generation.get(name)
        if not isinstance(digest, str) or HEX_SHA256.fullmatch(digest) is None:
            raise ValueError(f"teacher split generation {name} is malformed")

    teacher_profile = corpus.get("teacher_profile")
    if not isinstance(teacher_profile, Mapping):
        raise ValueError("teacher split profile differs from preregistration")
    if reopen_external_lineage:
        expected_teacher_profile = _preregistered_source_profiles(preregistration)[
            0
        ].as_dict()
        if dict(teacher_profile) != expected_teacher_profile:
            raise ValueError("teacher split profile differs from preregistration")
    elif EngineProfile.from_dict(teacher_profile).profile_id != ordered_profile_ids[0]:
        raise ValueError("teacher split profile identity differs from preregistration")

    teacher = manifest["teacher"]
    tiers = corpus.get("tiers")
    if not isinstance(tiers, Mapping) or set(tiers) != {"quiet_d2", "tactical_d3"}:
        raise ValueError("teacher split tier provenance is missing")
    tier_contracts = {
        "quiet_d2": teacher["tiers"]["quiet_depth2"],
        "tactical_d3": teacher["tiers"]["tactical_depth3"],
    }
    for tier_name, preregistered in tier_contracts.items():
        tier = tiers.get(tier_name)
        config = tier.get("config") if isinstance(tier, Mapping) else None
        expected_config = {
            "target_roots": preregistered["target_roots"],
            "train_roots": preregistered["train_roots"],
            "selection_mode": preregistered["selection_mode"],
            "depth_series": 2 if tier_name == "quiet_d2" else 3,
            "minimum_series": teacher["minimum_series"],
            "maximum_series": teacher["maximum_series"],
            "branch_cap": teacher["branch_cap"],
            "max_generation_positions": teacher["max_work"],
            "hard_negative_count": teacher["hard_negatives"],
            "seed": teacher["selection_seed"],
            "workers": teacher["workers"],
            "expected_train_attempts": trajectory["train"]["attempts"],
            "expected_holdout_attempts": trajectory["sealed_holdout"]["attempts"],
        }
        if (
            not isinstance(config, Mapping)
            or set(config) != set(expected_config)
            or any(config.get(name) != value for name, value in expected_config.items())
        ):
            raise ValueError(f"teacher split {tier_name} config drifted")
        quality = tier.get("quality") if isinstance(tier, Mapping) else None
        if (
            not isinstance(quality, Mapping)
            or quality.get("status") != "complete"
            or quality.get("accepted_roots") != preregistered["target_roots"]
            or quality.get("train_roots") != preregistered["train_roots"]
            or quality.get("holdout_roots") != preregistered["holdout_roots"]
        ):
            raise ValueError(f"teacher split {tier_name} quality drifted")
        _validate_tactical_gate_evidence(
            quality,
            str(preregistered["tactical_gate"]),
            f"teacher split {tier_name}",
        )
        safety = tier.get("contract") if isinstance(tier, Mapping) else None
        if not isinstance(safety, Mapping) or any(
            safety.get(name) is not expected
            for name, expected in {
                "incomplete_labels_cached": False,
                "full_retained_root_scores_required": True,
            }.items()
        ):
            raise ValueError(f"teacher split {tier_name} contract drifted")

    safety = corpus.get("contract")
    if not isinstance(safety, Mapping) or any(
        safety.get(name) is not expected
        for name, expected in {
            "incomplete_labels_cached": False,
            "full_retained_root_scores_required": True,
            "depth_is_per_label_provenance": True,
            "cross_depth_quality_metrics_blended": False,
            "train_holdout_exact_leakage_allowed": False,
            "strength_claim": False,
        }.items()
    ):
        raise ValueError("teacher split safety contract drifted")

    label_split = "train" if artifact_split == "train" else "holdout"
    start = trajectory[trajectory_name]["attempt_start"]
    stop = trajectory[trajectory_name]["attempt_stop"]
    tier_counts = {"quiet_d2": 0, "tactical_d3": 0}
    for raw in raw_labels:
        if not isinstance(raw, Mapping):
            raise ValueError("teacher split label provenance is malformed")
        attempt = raw.get("attempt_index")
        if type(attempt) is not int or not start <= attempt < stop:
            raise ValueError("teacher split label attempt provenance drifted")
        if any(
            raw.get(name) not in ordered_profile_ids
            for name in ("source_profile_id", "white_profile_id", "black_profile_id")
        ):
            raise ValueError("teacher split label profile provenance drifted")
        tier_name = str(raw.get("teacher_tier"))
        expected_depth = {"quiet_d2": 2, "tactical_d3": 3}.get(tier_name)
        if expected_depth is None or raw.get("teacher_depth_series") != expected_depth:
            raise ValueError("teacher split label tier/depth provenance drifted")
        tier_counts[tier_name] += 1
    count_field = "train_roots" if label_split == "train" else "holdout_roots"
    if any(
        tier_counts[name] != contract[count_field]
        for name, contract in tier_contracts.items()
    ):
        raise ValueError("teacher split label tier counts drifted")


def _validate_split_artifact(
    corpus: Mapping[str, Any],
    preregistration: Preregistration,
    *,
    expected_artifact_split: str,
    declared_source_snapshot: Mapping[str, Any] | None = None,
    reopen_external_lineage: bool = True,
) -> dict[str, Any]:
    if expected_artifact_split not in {"train", "sealed_holdout"}:
        raise ValueError(f"unknown requested artifact split: {expected_artifact_split}")
    artifact = corpus.get("artifact")
    if not isinstance(artifact, Mapping) or artifact.get("schema") != SPLIT_ARTIFACT_SCHEMA:
        raise ValueError("teacher input is not an isolated split artifact")
    if artifact.get("split") != expected_artifact_split:
        raise ValueError(
            f"expected {expected_artifact_split} artifact; got {artifact.get('split')!r}"
        )
    preregistration_binding = artifact.get("preregistration")
    if not isinstance(preregistration_binding, Mapping) or any(
        preregistration_binding.get(name) != value
        for name, value in {
            "schema": preregistration.schema,
            "sha256": preregistration.sha256,
        }.items()
    ):
        raise ValueError("teacher artifact preregistration binding differs")
    source = _validated_source_binding(
        artifact.get("source_combined_corpus"), "teacher artifact own"
    )
    counterpart_source = _validated_source_binding(
        artifact.get("counterpart_source_combined_corpus"),
        "teacher artifact counterpart",
    )
    source_pairing_mode = artifact.get("source_pairing_mode")
    if source_pairing_mode == "same-source-split":
        if source != counterpart_source:
            raise ValueError("same-source split has different source commitments")
    elif source_pairing_mode == "distinct-source-pair":
        _require_distinct_source_bindings(source, counterpart_source)
    else:
        raise ValueError("teacher artifact source-pairing mode is unsupported")
    source_id = source["corpus_id"]
    source_semantic_sha = source["semantic_sha256"]
    source_raw_sha = source["raw_artifact_sha256"]
    trajectory_name = (
        "train" if expected_artifact_split == "train" else "sealed_holdout"
    )
    declared_source = preregistration.manifest["trajectory_corpora"][
        trajectory_name
    ].get("artifact_source")
    if declared_source is not None:
        declared_binding = {
            name: declared_source[name]
            for name in ("corpus_id", "semantic_sha256", "raw_artifact_sha256")
        }
        if dict(source) != declared_binding:
            raise ValueError(
                f"teacher artifact {trajectory_name} source differs from preregistration"
            )
    for name in (
        "dataset_pairing_sha256",
        "semantic_keys_sha256",
        "counterpart_semantic_keys_sha256",
        "label_payload_sha256",
        "counterpart_label_payload_sha256",
        "source_cross_split_audit_sha256",
    ):
        value = artifact.get(name)
        if not isinstance(value, str) or HEX_SHA256.fullmatch(value) is None:
            raise ValueError(f"teacher artifact {name} is malformed")

    raw_labels = corpus.get("labels")
    if not isinstance(raw_labels, list) or not raw_labels:
        raise ValueError("teacher artifact labels must be a nonempty list")
    label_split = "train" if expected_artifact_split == "train" else "holdout"
    if any(
        not isinstance(raw, Mapping) or raw.get("split") != label_split
        for raw in raw_labels
    ):
        raise ValueError(
            f"{expected_artifact_split} artifact contains cross-split labels"
        )
    expected_labels = (
        preregistration.expected_train_labels
        if expected_artifact_split == "train"
        else preregistration.expected_holdout_labels
    )
    if len(raw_labels) != expected_labels:
        raise ValueError(
            f"{expected_artifact_split} artifact contains {len(raw_labels)} labels; "
            f"preregistration requires {expected_labels}"
        )
    _validate_split_preregistration_provenance(
        corpus,
        preregistration,
        artifact_split=expected_artifact_split,
        raw_labels=raw_labels,
        declared_source_snapshot=declared_source_snapshot,
        reopen_external_lineage=reopen_external_lineage,
    )
    quality = corpus.get("quality")
    expected_counts = (
        (expected_labels, expected_labels, 0)
        if expected_artifact_split == "train"
        else (expected_labels, 0, expected_labels)
    )
    if not isinstance(quality, Mapping) or tuple(
        quality.get(name)
        for name in ("accepted_roots", "train_roots", "holdout_roots")
    ) != expected_counts:
        raise ValueError("teacher artifact quality split counts differ")
    contract = corpus.get("contract")
    if (
        not isinstance(contract, Mapping)
        or contract.get("split_artifact_isolated") is not True
        or "development_import_unpaired" in contract
        or contract.get("distinct_source_pair_complete")
        is not (source_pairing_mode == "distinct-source-pair")
    ):
        raise ValueError("teacher artifact isolation contract is missing")
    selection = corpus.get("selection")
    if not isinstance(selection, Mapping) or "audit_state_keys" in selection:
        raise ValueError("teacher artifact exposes unsplit cross-split audit keys")
    semantic_sha, roots, finals = _raw_semantic_commitment(raw_labels)
    if artifact.get("semantic_keys_sha256") != semantic_sha:
        raise ValueError("teacher artifact semantic-key commitment differs")
    label_payload_sha = _raw_label_payload_commitment(raw_labels)
    if artifact.get("label_payload_sha256") != label_payload_sha:
        raise ValueError("teacher artifact label-payload commitment differs")
    train_semantic_sha = (
        semantic_sha
        if expected_artifact_split == "train"
        else str(artifact["counterpart_semantic_keys_sha256"])
    )
    holdout_semantic_sha = (
        semantic_sha
        if expected_artifact_split == "sealed_holdout"
        else str(artifact["counterpart_semantic_keys_sha256"])
    )
    train_label_payload_sha = (
        label_payload_sha
        if expected_artifact_split == "train"
        else str(artifact["counterpart_label_payload_sha256"])
    )
    holdout_label_payload_sha = (
        label_payload_sha
        if expected_artifact_split == "sealed_holdout"
        else str(artifact["counterpart_label_payload_sha256"])
    )
    expected_pairing_sha = _dataset_pairing_sha256(
        preregistration_sha256=preregistration.sha256,
        train_semantic_keys_sha256=train_semantic_sha,
        holdout_semantic_keys_sha256=holdout_semantic_sha,
        train_label_payload_sha256=train_label_payload_sha,
        holdout_label_payload_sha256=holdout_label_payload_sha,
        cross_split_audit_sha256=str(artifact["source_cross_split_audit_sha256"]),
        train_source=(source if expected_artifact_split == "train" else counterpart_source),
        holdout_source=(
            source if expected_artifact_split == "sealed_holdout" else counterpart_source
        ),
        source_pairing_mode=str(source_pairing_mode),
    )
    if artifact.get("dataset_pairing_sha256") != expected_pairing_sha:
        raise ValueError("teacher artifact dataset-pairing commitment differs")
    return {
        "artifact_split": expected_artifact_split,
        "source_combined_corpus_id": source_id,
        "source_combined_corpus_semantic_sha256": source_semantic_sha,
        "source_combined_corpus_raw_artifact_sha256": source_raw_sha,
        "source_combined_corpus": source,
        "counterpart_source_combined_corpus": counterpart_source,
        "source_pairing_mode": source_pairing_mode,
        "artifact_semantic_sha256": _teacher_semantic_sha256(corpus),
        "dataset_pairing_sha256": artifact["dataset_pairing_sha256"],
        "semantic_keys_sha256": semantic_sha,
        "counterpart_semantic_keys_sha256": artifact[
            "counterpart_semantic_keys_sha256"
        ],
        "label_payload_sha256": label_payload_sha,
        "counterpart_label_payload_sha256": artifact[
            "counterpart_label_payload_sha256"
        ],
        "source_cross_split_audit_sha256": artifact[
            "source_cross_split_audit_sha256"
        ],
        "root_state_keys": sorted(roots),
        "option_final_state_keys": sorted(finals),
    }


@dataclass(frozen=True, slots=True)
class TeacherOption:
    series: str
    score_white: int
    proof: str | None
    proof_bounds: tuple[int, int]
    signed_mate_distance: int | None
    final_state_key: str
    final_pfen: str
    outcome: str | None
    ended_by_check: bool
    is_teacher_best: bool
    is_hard_negative: bool
    features: tuple[int, ...]
    base_features: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TeacherLabel:
    split: str
    state_key: str
    position_hash: str
    pfen: str
    series_number: int
    mover_sign: int
    source_profile_id: str
    teacher_tier: str
    teacher_depth_series: int
    teacher_best_series: str
    teacher_score_white: int
    teacher_proof: str | None
    teacher_signed_mate_distance: int | None
    options: tuple[TeacherOption, ...]

    @property
    def tactical(self) -> bool:
        return bool(
            self.teacher_proof is not None
            or self.teacher_signed_mate_distance is not None
            or any(
                option.is_hard_negative
                or option.proof is not None
                or option.outcome is not None
                for option in self.options
            )
        )


def _option_is_mover_adverse(
    label: TeacherLabel, option: TeacherOption
) -> bool:
    opponent = "black" if label.mover_sign == 1 else "white"
    return option.proof == opponent


def _validate_adverse_pair_weight(value: float) -> float:
    weight = float(value)
    if not math.isfinite(weight) or not 1.0 <= weight <= 1_000.0:
        raise ValueError("adverse_pair_weight must be finite and between 1 and 1000")
    return weight


def _cached_payload_matches(
    supplied: Mapping[str, Any],
    regenerated: CachedFeatures,
    *,
    label: str,
) -> None:
    expected = regenerated.as_dict()
    missing = [name for name in expected if name not in supplied]
    if missing:
        raise ValueError(f"{label} cached features miss {missing}")
    mismatches = {
        name: (supplied[name], expected[name])
        for name in expected
        if supplied[name] != expected[name]
    }
    if mismatches:
        raise ValueError(f"{label} cached features differ from regenerated values: {mismatches}")


def _materialize_labels(
    corpus: Mapping[str, Any],
    *,
    selected_split: str | None,
) -> tuple[tuple[TeacherLabel, ...], dict[str, Any]]:
    if corpus.get("schema") != CORPUS_SCHEMA:
        raise ValueError(
            f"unsupported teacher corpus schema: {corpus.get('schema')!r}"
        )
    if corpus.get("method") != CORPUS_METHOD:
        raise ValueError(
            f"unsupported teacher corpus method: {corpus.get('method')!r}"
        )
    declared_contract = corpus.get("contract")
    corpus_id_prefix = (
        "spc-native-mixed-teacher-exploratory-"
        if isinstance(declared_contract, Mapping)
        and declared_contract.get("exploratory_only") is True
        else "spc-native-mixed-teacher-"
    )
    deterministic = {
        key: value
        for key, value in corpus.items()
        if key not in {"corpus_id", "runtime"}
    }
    if (
        isinstance(corpus.get("artifact"), Mapping)
        and corpus["artifact"].get("schema") == SPLIT_ARTIFACT_SCHEMA
    ):
        identity_sha256 = _teacher_semantic_sha256(corpus)
    else:
        identity_sha256 = hashlib.sha256(_canonical_json(deterministic)).hexdigest()
    expected_corpus_id = corpus_id_prefix + identity_sha256[:20]
    if corpus.get("corpus_id") != expected_corpus_id:
        raise ValueError("teacher corpus_id does not match its deterministic payload")
    raw_labels = corpus.get("labels")
    if not isinstance(raw_labels, list) or not raw_labels:
        raise ValueError("teacher corpus labels must be a nonempty list")
    quality = corpus.get("quality")
    if (
        not isinstance(quality, Mapping)
        or quality.get("status") != "complete"
        or int(quality.get("accepted_roots", -1)) != len(raw_labels)
    ):
        raise ValueError("teacher corpus quality contract is incomplete")
    tiers = corpus.get("tiers")
    if not isinstance(tiers, Mapping) or set(tiers) != {
        "quiet_d2",
        "tactical_d3",
    }:
        raise ValueError("teacher corpus must contain both fixed teacher tiers")
    selection = corpus.get("selection")
    required_zero_audits = (
        "selected_root_exact_overlap_states",
        "cross_split_option_final_exact_overlap_states",
        "train_option_final_to_holdout_root_overlap_states",
        "holdout_option_final_to_train_root_overlap_states",
    )
    if not isinstance(selection, Mapping) or any(
        selection.get(name) != 0 for name in required_zero_audits
    ):
        raise ValueError("teacher corpus declares a nonzero leakage audit")
    contract = declared_contract
    if not isinstance(contract, Mapping) or any(
        contract.get(name) is not expected
        for name, expected in {
            "incomplete_labels_cached": False,
            "full_retained_root_scores_required": True,
            "depth_is_per_label_provenance": True,
            "cross_depth_quality_metrics_blended": False,
            "train_holdout_exact_leakage_allowed": False,
            "strength_claim": False,
        }.items()
    ):
        raise ValueError("teacher corpus safety contract differs")
    labels: list[TeacherLabel] = []
    root_keys: dict[str, set[str]] = {"train": set(), "holdout": set()}
    final_keys: dict[str, set[str]] = {"train": set(), "holdout": set()}
    all_counts = {"train": 0, "holdout": 0}
    for raw in raw_labels:
        split = str(raw["split"])
        if split not in root_keys:
            raise ValueError(f"unknown teacher split: {split}")
        all_counts[split] += 1
        root_key = str(raw["state_key_sha256"])
        if root_key in root_keys[split]:
            raise ValueError(f"duplicate teacher root key in {split}: {root_key}")
        root_promoted = raw["root_promoted_bitboard"]
        root_chess960 = raw["root_chess960"]
        if type(root_promoted) is not int or type(root_chess960) is not bool:
            raise ValueError(f"root semantic metadata has the wrong type: {root_key}")
        root_state_identity = state_from_pfen(
            str(raw["pfen"]),
            promoted_bitboard=root_promoted,
            chess960=root_chess960,
        )
        regenerated_root_key = progressive_state_dedup_key(
            root_state_identity
        ).hex()
        if regenerated_root_key != root_key:
            raise ValueError(f"root semantic state key mismatch: {root_key}")
        if root_state_identity.position_hash != str(raw["position_hash"]):
            raise ValueError(f"root PFEN/hash mismatch: {root_key}")
        if root_state_identity.series_number != int(raw["series_number"]):
            raise ValueError(f"root series mismatch: {root_key}")
        expected_mover = "white" if root_state_identity.board.turn else "black"
        if expected_mover != str(raw["mover"]):
            raise ValueError(f"root mover mismatch: {root_key}")
        root_keys[split].add(root_key)
        raw_options = raw.get("options")
        if not isinstance(raw_options, list) or not raw_options:
            raise ValueError(f"teacher label has no options: {root_key}")
        identity_final_states = []
        for option in raw_options:
            final_key = str(option["final_state_key_sha256"])
            final_promoted = option["final_promoted_bitboard"]
            final_chess960 = option["final_chess960"]
            if type(final_promoted) is not int or type(final_chess960) is not bool:
                raise ValueError(
                    f"option semantic metadata has the wrong type: "
                    f"{root_key}/{option['series']}"
                )
            final_state_from_pfen = state_from_pfen(
                str(option["final_pfen"]),
                promoted_bitboard=final_promoted,
                chess960=final_chess960,
            )
            series = str(option["series"])
            moves = () if not series else tuple(series.split("/"))
            replayed = play_series(root_state_identity, moves)
            final_state_identity = replayed.final_state
            if replayed.machine_notation != series:
                raise ValueError(
                    f"option replay notation mismatch: {root_key}/{series}"
                )
            if final_state_identity.pfen != str(option["final_pfen"]):
                raise ValueError(f"option replay PFEN mismatch: {root_key}/{series}")
            if (
                final_state_identity.board.promoted != final_promoted
                or final_state_identity.board.chess960 != final_chess960
            ):
                raise ValueError(
                    f"option replay semantic metadata mismatch: {root_key}/{series}"
                )
            replayed_outcome = (
                None if replayed.outcome is None else replayed.outcome.value
            )
            if (
                replayed_outcome != option.get("outcome")
                or replayed.ended_by_check != bool(option["ended_by_check"])
            ):
                raise ValueError(f"option replay outcome mismatch: {root_key}/{series}")
            regenerated_final_key = progressive_state_dedup_key(
                final_state_identity
            ).hex()
            if regenerated_final_key != final_key:
                raise ValueError(
                    f"option semantic state key mismatch: "
                    f"{root_key}/{option['series']}"
                )
            if progressive_state_dedup_key(final_state_from_pfen).hex() != final_key:
                raise ValueError(
                    f"option PFEN semantic state key mismatch: {root_key}/{series}"
                )
            final_keys[split].add(final_key)
            identity_final_states.append(final_state_identity)
        if selected_split is not None and split != selected_split:
            continue
        search = raw["search"]
        if (
            int(search["completed_depth_series"])
            != int(search["requested_depth_series"])
            or bool(search["timed_out"])
            or bool(search["work_limit_reached"])
            or not bool(search["root_scores_complete"])
        ):
            raise ValueError(f"accepted teacher label is incomplete: {root_key}")
        teacher_tier = str(raw["teacher_tier"])
        teacher_depth_series = int(raw["teacher_depth_series"])
        expected_tier_depth = {
            "quiet_d2": 2,
            "tactical_d3": 3,
        }.get(teacher_tier)
        if (
            expected_tier_depth is None
            or teacher_depth_series != expected_tier_depth
            or teacher_depth_series != int(search["requested_depth_series"])
        ):
            raise ValueError(f"teacher tier/depth mismatch: {root_key}")
        root_state = root_state_identity
        _cached_payload_matches(
            raw["root_features"],
            CachedFeatures.from_state(root_state),
            label=f"root {root_key}",
        )
        options: list[TeacherOption] = []
        best_count = 0
        for raw_option, final_state in zip(
            raw_options, identity_final_states, strict=True
        ):
            final_key = str(raw_option["final_state_key_sha256"])
            raw_proof_bounds = raw_option.get("proof_bounds")
            if (
                not isinstance(raw_proof_bounds, list)
                or len(raw_proof_bounds) != 2
                or any(type(value) is not int for value in raw_proof_bounds)
            ):
                raise ValueError(
                    f"option proof bounds are not an integer pair: "
                    f"{root_key}/{raw_option['series']}"
                )
            supplied_features = raw_option["final_features"]
            _cached_payload_matches(
                supplied_features,
                CachedFeatures.from_state(final_state),
                label=f"option {root_key}/{raw_option['series']}",
            )
            raw_pv = raw_option.get("principal_variation")
            if not isinstance(raw_pv, list) or not raw_pv:
                raise ValueError(
                    f"option has no replayable principal variation: "
                    f"{root_key}/{raw_option['series']}"
                )
            pv_state = root_state
            for expected_ply, raw_pv_row in enumerate(raw_pv, 1):
                if int(raw_pv_row["series_ply"]) != expected_ply:
                    raise ValueError(
                        f"teacher PV ply mismatch: {root_key}/{raw_option['series']}"
                    )
                pv_series = str(raw_pv_row["series"])
                pv_moves = () if not pv_series else tuple(pv_series.split("/"))
                pv_replayed = play_series(pv_state, pv_moves)
                pv_key = progressive_state_dedup_key(
                    pv_replayed.final_state
                ).hex()
                pv_outcome = (
                    None
                    if pv_replayed.outcome is None
                    else pv_replayed.outcome.value
                )
                if (
                    pv_replayed.machine_notation != pv_series
                    or pv_key != str(raw_pv_row["final_state_key_sha256"])
                    or pv_outcome != raw_pv_row.get("outcome")
                    or pv_replayed.ended_by_check
                    != bool(raw_pv_row["ended_by_check"])
                ):
                    raise ValueError(
                        f"teacher PV replay mismatch: "
                        f"{root_key}/{raw_option['series']}/{expected_ply}"
                    )
                if expected_ply == 1 and (
                    pv_series != str(raw_option["series"])
                    or pv_key != final_key
                ):
                    raise ValueError(
                        f"teacher PV root option mismatch: "
                        f"{root_key}/{raw_option['series']}"
                    )
                pv_state = pv_replayed.final_state
            features = TeacherValueFeaturesV3.from_state_and_cached(
                final_state,
                supplied_features,
            ).values
            base_features = tuple(int(supplied_features[name]) for name in FEATURE_NAMES)
            is_best = bool(raw_option["is_teacher_best"])
            best_count += int(is_best)
            options.append(
                TeacherOption(
                    series=str(raw_option["series"]),
                    score_white=int(raw_option["score_white_heuristic_points"]),
                    proof=(None if raw_option.get("proof") is None else str(raw_option["proof"])),
                    proof_bounds=(raw_proof_bounds[0], raw_proof_bounds[1]),
                    signed_mate_distance=(
                        None
                        if raw_option.get("signed_mate_distance_series") is None
                        else int(raw_option["signed_mate_distance_series"])
                    ),
                    final_state_key=final_key,
                    final_pfen=str(raw_option["final_pfen"]),
                    outcome=(None if raw_option.get("outcome") is None else str(raw_option["outcome"])),
                    ended_by_check=bool(raw_option["ended_by_check"]),
                    is_teacher_best=is_best,
                    is_hard_negative=bool(raw_option["is_hard_negative"]),
                    features=features,
                    base_features=base_features,
                )
            )
        if best_count != 1:
            raise ValueError(
                f"teacher label must flag exactly one best option: {root_key}"
            )
        best_series = str(raw["teacher_best_series"])
        if not any(option.series == best_series and option.is_teacher_best for option in options):
            raise ValueError(f"teacher best is not a flagged retained option: {root_key}")
        selected_best = next(
            option for option in options if option.series == best_series
        )
        teacher_score = int(raw["teacher_score_white_heuristic_points"])
        if selected_best.score_white != teacher_score:
            raise ValueError(f"teacher best score differs from retained option: {root_key}")
        teacher_best_proof = (
            None
            if raw.get("teacher_best_proof") is None
            else str(raw["teacher_best_proof"])
        )
        if selected_best.proof != teacher_best_proof:
            raise ValueError(f"teacher best proof differs from retained option: {root_key}")
        teacher_best_bounds = raw.get("teacher_best_proof_bounds")
        if (
            not isinstance(teacher_best_bounds, list)
            or len(teacher_best_bounds) != 2
            or any(type(value) is not int for value in teacher_best_bounds)
            or selected_best.proof_bounds
            != (teacher_best_bounds[0], teacher_best_bounds[1])
        ):
            raise ValueError(
                f"teacher best proof bounds differ from retained option: {root_key}"
            )
        teacher_mate_distance = (
            None
            if raw.get("teacher_signed_mate_distance_series") is None
            else int(raw["teacher_signed_mate_distance_series"])
        )
        if selected_best.signed_mate_distance != teacher_mate_distance:
            raise ValueError(
                f"teacher best mate distance differs from retained option: {root_key}"
            )
        mover_sign = 1 if expected_mover == "white" else -1
        if mover_sign * selected_best.score_white != max(
            mover_sign * option.score_white for option in options
        ):
            raise ValueError(f"teacher best is not mover-optimal by exact score: {root_key}")
        labels.append(
            TeacherLabel(
                split=split,
                state_key=root_key,
                position_hash=str(raw["position_hash"]),
                pfen=str(raw["pfen"]),
                series_number=int(raw["series_number"]),
                mover_sign=mover_sign,
                source_profile_id=str(raw["source_profile_id"]),
                teacher_tier=teacher_tier,
                teacher_depth_series=teacher_depth_series,
                teacher_best_series=best_series,
                teacher_score_white=teacher_score,
                teacher_proof=(None if raw.get("teacher_proof") is None else str(raw["teacher_proof"])),
                teacher_signed_mate_distance=teacher_mate_distance,
                options=tuple(options),
            )
        )
    root_overlap = root_keys["train"] & root_keys["holdout"]
    final_overlap = final_keys["train"] & final_keys["holdout"]
    train_final_to_holdout_root = final_keys["train"] & root_keys["holdout"]
    holdout_final_to_train_root = final_keys["holdout"] & root_keys["train"]
    if root_overlap:
        raise ValueError(f"train/holdout root-key leakage: {len(root_overlap)}")
    if final_overlap:
        raise ValueError(f"train/holdout option-final leakage: {len(final_overlap)}")
    if train_final_to_holdout_root:
        raise ValueError(
            "train option-final/holdout root leakage: "
            f"{len(train_final_to_holdout_root)}"
        )
    if holdout_final_to_train_root:
        raise ValueError(
            "holdout option-final/train root leakage: "
            f"{len(holdout_final_to_train_root)}"
        )
    if (
        int(quality.get("train_roots", -1)) != all_counts["train"]
        or int(quality.get("holdout_roots", -1)) != all_counts["holdout"]
    ):
        raise ValueError("teacher corpus split counts differ from quality metadata")
    return tuple(labels), {
        "all_label_counts": all_counts,
        "train_root_keys": len(root_keys["train"]),
        "holdout_root_keys": len(root_keys["holdout"]),
        "root_key_overlap": 0,
        "train_option_final_keys": len(final_keys["train"]),
        "holdout_option_final_keys": len(final_keys["holdout"]),
        "option_final_key_overlap": 0,
        "train_option_final_to_holdout_root_overlap": 0,
        "holdout_option_final_to_train_root_overlap": 0,
        "materialized_split": selected_split,
        "materialized_labels": len(labels),
        "materialized_options": sum(len(label.options) for label in labels),
    }


def _runtime_contract() -> dict[str, str]:
    native_paths: dict[str, Path] = {}
    for name, module in (
        ("native_eval_binary_sha256", evaluation._native_eval),
        ("native_mate_binary_sha256", series_mate._native_mate),
    ):
        module_path = getattr(module, "__file__", None)
        if not isinstance(module_path, str):
            raise ValueError(f"cannot preregister without a loaded {name} runtime")
        native_paths[name] = Path(module_path).resolve()
    compiler = platform.python_compiler()
    compiler_match = re.search(r"MSC v\.(\d{2})(\d{2})", compiler)
    if compiler_match is None:
        raise ValueError(f"cycle-4 preregistration requires MSVC; got {compiler!r}")
    numeric_runtime = {
        "numpy": np.__version__,
        "numpy_build": getattr(np.__config__, "CONFIG", {}),
        "thread_environment": {
            name: os.environ.get(name)
            for name in (
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            )
        },
    }
    return {
        "platform": f"{platform.system()} {platform.machine().lower()}",
        "python": platform.python_version(),
        "python_abi": str(sys.implementation.cache_tag),
        "compiler": (
            f"MSVC {compiler_match.group(1)}.{compiler_match.group(2)} ({compiler})"
        ),
        "numpy": np.__version__,
        "numeric_runtime_sha256": hashlib.sha256(
            _canonical_json(numeric_runtime)
        ).hexdigest(),
        **{name: _sha256(path) for name, path in native_paths.items()},
    }


def _profile_bindings() -> dict[str, Any]:
    root = _repository_root()
    schedule_paths = sorted(
        (root / "profiles" / "training" / "teacher-source-schedule").glob(
            "*.json"
        )
    )
    if len(schedule_paths) != 4:
        raise ValueError("cycle-4 preregistration requires exactly four source profiles")

    def binding(path: Path) -> dict[str, str]:
        profile, raw_sha = _read_profile_artifact(path)
        return {
            "path": path.relative_to(root).as_posix(),
            "profile_id": profile.profile_id,
            "sha256": raw_sha,
        }

    leader_path = root / "profiles" / "training" / "native-corpus-development-leader.json"
    return {
        "ordered_source_schedule": [binding(path) for path in schedule_paths],
        "rejected_development_leader": binding(leader_path),
    }


def _canonical_cli_input(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    resolved = path.expanduser().resolve()
    if str(path) != str(resolved):
        raise ValueError(f"{label} path must be canonical; aliases are forbidden")
    return path


def _require_lexical_absolute_input(path: Path, label: str) -> None:
    supplied = str(path)
    if not path.is_absolute() or os.path.normpath(supplied) != supplied:
        raise ValueError(f"{label} path must be absolute and lexically normalized")


def _require_pairwise_nonoverlapping_paths(
    paths: Mapping[str, Path],
    *,
    label: str,
) -> None:
    """Reject lexical file/directory collisions without touching their targets."""

    entries = list(paths.items())
    for name, path in entries:
        _require_lexical_absolute_input(path, f"{label} {name}")
    for index, (name, path) in enumerate(entries):
        for other_name, other in entries[index + 1 :]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError(
                    f"{label} paths must be distinct and non-nested: "
                    f"{name}, {other_name}"
                )


def _development_source_binding(
    source_path: Path,
    evidence_path: Path,
) -> dict[str, Any]:
    source_path = _canonical_cli_input(source_path, "development source")
    evidence_path = _canonical_cli_input(
        evidence_path, "development consumption evidence"
    )
    source, source_raw_sha = _read_json_artifact(source_path)
    evidence, evidence_raw_sha = _read_json_artifact(evidence_path)
    if evidence.get("schema") != "spc-cycle3-one-shot-result-v1":
        raise ValueError("development consumption evidence schema is unsupported")
    raw_labels = source.get("labels")
    if (
        not isinstance(raw_labels, list)
        or len(raw_labels) != CYCLE3_CONSUMED_DEVELOPMENT_LABELS
    ):
        raise ValueError("development source is not the exact 192-label consumed corpus")
    _, source_roots, source_finals = _raw_semantic_commitment(raw_labels)
    binding = {
        "path": str(source_path),
        "corpus_id": source.get("corpus_id"),
        "semantic_sha256": _teacher_semantic_sha256(source),
        "raw_artifact_sha256": source_raw_sha,
        "label_count": len(raw_labels),
        "semantic_exclusion_sha256": semantic_exclusion_sha256(
            source_roots | source_finals
        ),
        "consumption_evidence": {
            "path": str(evidence_path),
            "schema": evidence["schema"],
            "raw_artifact_sha256": evidence_raw_sha,
        },
    }
    _validate_declared_artifact_source(binding, "train")
    _validate_consumption_evidence(
        binding,
        evidence_snapshot=(evidence, evidence_raw_sha),
    )
    _materialize_labels(source, selected_split=None)
    return binding


def _development_binding_from_metadata(
    path: Path,
) -> tuple[dict[str, Any], str]:
    path = _canonical_cli_input(path, "development source metadata")
    metadata, raw_sha256 = _read_json_artifact(path)
    metadata = _require_exact_fields(
        metadata,
        {"schema", "artifact_source"},
        "development source metadata",
    )
    if metadata["schema"] != DEVELOPMENT_SOURCE_METADATA_SCHEMA:
        raise ValueError("development source metadata schema is unsupported")
    source = metadata["artifact_source"]
    _validate_declared_artifact_source(source, "train")
    if source["label_count"] != CYCLE3_CONSUMED_DEVELOPMENT_LABELS:
        raise ValueError("development metadata does not bind the exact 192-label corpus")
    return dict(source), raw_sha256


def _build_cycle4_preregistration_manifest(
    *,
    base_deployed_commit: str,
    integrated_engine_source_commit: str,
    train_seed: int,
    holdout_seed: int,
    selection_seed: int,
    match_seed: int,
    development_source: Mapping[str, Any] | None,
) -> dict[str, Any]:
    for label, commit in (
        ("base deployed", base_deployed_commit),
        ("integrated engine source", integrated_engine_source_commit),
    ):
        if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise ValueError(f"{label} commit must be a full lowercase Git SHA-1")
    seeds = {
        "train": _exact_positive_int(train_seed, "train seed"),
        "holdout": _exact_positive_int(holdout_seed, "holdout seed"),
        "teacher": _exact_positive_int(selection_seed, "teacher selection seed"),
        "match": _exact_positive_int(match_seed, "post-holdout match seed"),
    }
    if len(set(seeds.values())) != len(seeds):
        raise ValueError("cycle-4 train, holdout, teacher, and match seeds must be unique")
    train_trajectory: dict[str, Any] = {
        "seed": train_seed,
        "attempts": CYCLE4_TRAJECTORY_ATTEMPTS["train"],
        "attempt_start": 0,
        "attempt_stop": CYCLE4_TRAJECTORY_ATTEMPTS["train"],
    }
    if development_source is not None:
        train_trajectory["artifact_source"] = dict(development_source)
    development_exclusion_sha = (
        str(development_source["semantic_exclusion_sha256"])
        if development_source is not None
        else semantic_exclusion_sha256(())
    )
    expected_train = sum(
        int(tier["train_roots"]) for tier in CYCLE4_TEACHER_TIERS.values()
    )
    expected_holdout = sum(
        int(tier["holdout_roots"]) for tier in CYCLE4_TEACHER_TIERS.values()
    )
    return {
        "schema": "spc-cycle4-one-shot-protocol-v1",
        "status": "pre-registered-before-generation",
        "purpose": (
            "Leakage-safe larger cycle-4 teacher ranking; promotion remains blocked "
            "until every separately verified post-holdout receipt passes."
        ),
        "source": {
            "base_deployed_commit": base_deployed_commit,
            "integrated_engine_source_commit": integrated_engine_source_commit,
            "engine_version": ENGINE_VERSION,
            "engine_source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
            "native_eval_source_identity_sha256": evaluation._native_source_identity(),
            "native_mate_source_identity_sha256": series_mate._native_mate_source_identity(),
            "commit_reference_role": (
                "operator provenance labels; production preregister verifies local Git "
                "resolution while executable source is bound by fingerprints, native "
                "identities, and frozen implementation hashes"
            ),
        },
        "runtime": _runtime_contract(),
        "profiles": _profile_bindings(),
        "trajectory_corpora": {
            "train": train_trajectory,
            "sealed_holdout": {
                "seed": holdout_seed,
                "attempts": CYCLE4_TRAJECTORY_ATTEMPTS["sealed_holdout"],
                "attempt_start": 0,
                "attempt_stop": CYCLE4_TRAJECTORY_ATTEMPTS["sealed_holdout"],
                "development_exclusion_sha256": development_exclusion_sha,
                "one_shot": True,
            },
            "shared_config": {
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
            },
        },
        "teacher": {
            "selection_seed": selection_seed,
            "minimum_series": 4,
            "maximum_series": 9,
            "branch_cap": 32,
            "max_work": 10_000_000,
            "hard_negatives": 4,
            "workers": 8,
            "prior_receipt_cache_reuse": False,
            "tiers": {
                name: dict(value) for name, value in CYCLE4_TEACHER_TIERS.items()
            },
            "expected_merged_roots": expected_train + expected_holdout,
            "expected_merged_train_roots": expected_train,
            "expected_merged_holdout_roots": expected_holdout,
        },
        "integrity": {
            "holdout_output_must_not_be_manually_inspected_before_gate": True,
            "holdout_informed_filtering_forbidden": True,
            "semantic_key": "progressive_state_dedup_key",
            "required_zero_intersections": list(REQUIRED_ZERO_INTERSECTIONS),
            "seed_burn_rule": _seed_burn_rule(holdout_seed),
            "teacher_semantic_hash_contract": TEACHER_SEMANTIC_HASH_CONTRACT,
        },
        "one_shot_gates": {
            "candidate_roles": ["primary_nonroute", "distilled_seven_weight"],
            "each_candidate_must": list(REQUIRED_CANDIDATE_GATES),
            "route_ablation_must": list(REQUIRED_ROUTE_ABLATION_GATES),
            "post_holdout_required_before_promotion": list(REQUIRED_POST_HOLDOUT_GATES),
        },
        "post_holdout_match": _post_holdout_match_contract(
            match_seed, base_deployed_commit
        ),
        "promotion_evidence": {
            "status": PROMOTION_EVIDENCE_FIXED["status"],
            "required_receipt_schemas": list(
                PROMOTION_EVIDENCE_FIXED["required_receipt_schemas"]
            ),
        },
        "preflight": {"holdout_consumed": False, "generation_started": False},
        "frozen_implementation": _current_frozen_implementation(),
    }


def _preregister_command(args: argparse.Namespace) -> None:
    with _protocol_stage_lock("preregister", exclusive=False):
        _preregister_command_locked(args)


def _preregister_command_locked(args: argparse.Namespace) -> None:
    # A repository-wide preparation/evaluation marker is terminal for manifest
    # construction. Check the fixed registry before resolving or opening any
    # caller-selected development, profile, runtime, or output path.
    _reject_preregistration_after_pair_start(args.holdout_seed)
    output = args.output.expanduser().resolve()
    if str(args.output) != str(output):
        raise ValueError("preregistration output must be an absolute canonical path")
    if args.dry_run and output.exists():
        raise FileExistsError("preregistration output already exists")
    supplied_source = args.development_source is not None
    supplied_evidence = args.development_consumption_evidence is not None
    supplied_metadata = args.development_source_metadata is not None
    if supplied_metadata and (supplied_source or supplied_evidence):
        raise ValueError(
            "development metadata cannot be combined with source/evidence inputs"
        )
    if supplied_source != supplied_evidence:
        raise ValueError("development source and consumption evidence are an exact pair")
    _verify_preregistration_commit_labels(
        args.base_deployed_commit,
        args.integrated_engine_source_commit,
    )
    if supplied_metadata:
        development_source, development_metadata_raw_sha256 = (
            _development_binding_from_metadata(args.development_source_metadata)
        )
        metadata_only = True
    elif supplied_source:
        development_source = _development_source_binding(
            args.development_source,
            args.development_consumption_evidence,
        )
        development_metadata_raw_sha256 = None
        metadata_only = False
    else:
        development_source = None
        development_metadata_raw_sha256 = None
        metadata_only = True
    manifest = _build_cycle4_preregistration_manifest(
        base_deployed_commit=args.base_deployed_commit,
        integrated_engine_source_commit=args.integrated_engine_source_commit,
        train_seed=args.train_seed,
        holdout_seed=args.holdout_seed,
        selection_seed=args.selection_seed,
        match_seed=args.match_seed,
        development_source=development_source,
    )
    _verify_preregistration_commit_labels(
        args.base_deployed_commit,
        args.integrated_engine_source_commit,
    )
    if manifest["frozen_implementation"] != _current_frozen_implementation():
        raise ValueError("frozen implementation changed during preregistration")
    raw_sha = hashlib.sha256(_pretty_json_bytes(manifest)).hexdigest()
    preregistration = _preregistration_from_manifest(output, manifest, raw_sha)
    claim_path = _holdout_claim_path(preregistration)
    preparation_claim_path = _holdout_preparation_claim_path(preregistration)
    preparation_source_path = _holdout_preparation_source_path(preregistration)
    pair_completion_path = _pair_completion_registry_path(preregistration)
    cycle_reservation_path = _cycle_preregistration_reservation_path()
    seed_reservation_path = _seed_preregistration_reservation_path(
        int(manifest["trajectory_corpora"]["sealed_holdout"]["seed"])
    )
    protocol_paths = {
        "manifest output": output,
        "holdout claim": claim_path,
        "preparation claim": preparation_claim_path,
        "preparation source binding": preparation_source_path,
        "pair completion registry": pair_completion_path,
        "upstream stage lock": _protocol_stage_lock_path(),
        "cycle reservation": cycle_reservation_path,
        "seed reservation": seed_reservation_path,
    }
    for name, supplied in (
        ("development source", args.development_source),
        ("development evidence", args.development_consumption_evidence),
        ("development metadata", args.development_source_metadata),
    ):
        if supplied is not None:
            protocol_paths[name] = supplied
    _require_pairwise_nonoverlapping_paths(
        protocol_paths, label="preregistration"
    )
    if output.exists():
        existing_output, existing_output_sha = _read_json_artifact(output)
        if existing_output != manifest or existing_output_sha != raw_sha:
            raise FileExistsError("preregistration output already differs")
    if args.dry_run:
        expected_reservation = _preregistration_reservation_payload(preregistration)
        for reservation_path in (cycle_reservation_path, seed_reservation_path):
            if reservation_path.exists():
                existing_reservation, _ = _read_json_artifact(reservation_path)
                if existing_reservation != expected_reservation:
                    raise FileExistsError(
                        "cycle-4 preregistration is already reserved differently"
                    )
    if (
        claim_path.exists()
        or preparation_claim_path.exists()
        or preparation_source_path.exists()
        or pair_completion_path.exists()
    ):
        raise FileExistsError(
            "cycle-4 holdout seed already has a repository-wide consumption claim"
        )
    summary = {
        "dry_run": bool(args.dry_run),
        "metadata_only": metadata_only,
        "output": str(output),
        "schema": preregistration.schema,
        "sha256": raw_sha,
        "effective_train_labels": preregistration.expected_train_labels,
        "fresh_teacher_train_labels": manifest["teacher"][
            "expected_merged_train_roots"
        ],
        "sealed_holdout_labels": preregistration.expected_holdout_labels,
        "train_attempts": CYCLE4_TRAJECTORY_ATTEMPTS["train"],
        "holdout_attempts": CYCLE4_TRAJECTORY_ATTEMPTS["sealed_holdout"],
        "holdout_claim_path": str(claim_path),
        "holdout_preparation_claim_path": str(preparation_claim_path),
        "holdout_preparation_source_path": str(preparation_source_path),
        "pair_completion_registry_path": str(pair_completion_path),
        "cycle_preregistration_reservation_path": str(cycle_reservation_path),
        "seed_preregistration_reservation_path": str(seed_reservation_path),
        "promotion_status": PROMOTION_EVIDENCE_FIXED["status"],
    }
    if not args.dry_run:
        # Rebuild the complete candidate immediately before the irreversible
        # cycle/seed reservation. This second pass reopens the direct
        # development source/evidence (or only the metadata carrier in
        # metadata-only mode), every frozen profile/runtime binary, and every
        # frozen implementation file. A reservation must never attest a mix of
        # snapshots gathered on opposite sides of a concurrent rewrite.
        _verify_preregistration_commit_labels(
            args.base_deployed_commit,
            args.integrated_engine_source_commit,
        )
        if supplied_metadata:
            final_development_source, final_metadata_raw_sha256 = (
                _development_binding_from_metadata(
                    args.development_source_metadata
                )
            )
            if final_metadata_raw_sha256 != development_metadata_raw_sha256:
                raise ValueError(
                    "development source metadata changed during preregistration"
                )
        elif supplied_source:
            final_development_source = _development_source_binding(
                args.development_source,
                args.development_consumption_evidence,
            )
        else:
            final_development_source = None
        final_manifest = _build_cycle4_preregistration_manifest(
            base_deployed_commit=args.base_deployed_commit,
            integrated_engine_source_commit=args.integrated_engine_source_commit,
            train_seed=args.train_seed,
            holdout_seed=args.holdout_seed,
            selection_seed=args.selection_seed,
            match_seed=args.match_seed,
            development_source=final_development_source,
        )
        if (
            final_development_source != development_source
            or _pretty_json_bytes(final_manifest) != _pretty_json_bytes(manifest)
        ):
            raise ValueError(
                "preregistration inputs or executable contract changed before reservation"
            )
        reserved_cycle, reserved_seed = _reserve_preregistration(preregistration)
        if (reserved_cycle, reserved_seed) != (
            cycle_reservation_path,
            seed_reservation_path,
        ):
            raise RuntimeError("preregistration reservation paths changed")
        if output.exists():
            existing, existing_sha = _read_json_artifact(output)
            if existing != manifest or existing_sha != raw_sha:
                raise FileExistsError("preregistration output already differs")
        else:
            _atomic_exclusive_json(
                output,
                manifest,
                conflict_message="preregistration output already exists",
            )
        written, written_sha = _read_json_artifact(output)
        if written != manifest or written_sha != raw_sha:
            raise RuntimeError("written preregistration bytes differ from validation")
        _validate_preregistration_reservation(preregistration)
    print(json.dumps(summary, indent=2, sort_keys=True))


def _development_import_payload(
    source: Mapping[str, Any],
    *,
    declared_source: Mapping[str, Any],
    preregistration: Preregistration,
    output_path: Path,
) -> dict[str, Any]:
    relabeled: list[dict[str, Any]] = []
    for raw in source["labels"]:
        if not isinstance(raw, Mapping) or raw.get("split") not in {"train", "holdout"}:
            raise ValueError("development source contains a malformed split label")
        original_split = str(raw["split"])
        relabeled.append(
            {
                **dict(raw),
                "split": "train",
                "development_provenance": {
                    "schema": DEVELOPMENT_PROVENANCE_SCHEMA,
                    "original_split": original_split,
                    "original_artifact_semantic_sha256": declared_source[
                        "semantic_sha256"
                    ],
                    "original_artifact_raw_sha256": declared_source[
                        "raw_artifact_sha256"
                    ],
                    "consumption_evidence_raw_sha256": declared_source[
                        "consumption_evidence"
                    ]["raw_artifact_sha256"],
                },
            }
        )
    semantic_sha, _, _ = _raw_semantic_commitment(relabeled)
    label_payload_sha = _raw_label_payload_commitment(relabeled)
    deterministic = {
        key: value
        for key, value in source.items()
        if key
        not in {
            "artifact",
            "corpus_id",
            "labels",
            "quality",
            "runtime",
            "selection",
        }
    }
    deterministic.update(
        {
            "artifact": {
                "schema": DEVELOPMENT_IMPORT_SCHEMA,
                "role": "train-development",
                "canonical_output_path": str(output_path),
                "preregistration": {
                    "schema": preregistration.schema,
                    "sha256": preregistration.sha256,
                },
                "source_combined_corpus": {
                    name: declared_source[name]
                    for name in (
                        "corpus_id",
                        "semantic_sha256",
                        "raw_artifact_sha256",
                    )
                },
                "consumption_evidence": dict(
                    declared_source["consumption_evidence"]
                ),
                "semantic_keys_sha256": semantic_sha,
                "label_payload_sha256": label_payload_sha,
                "semantic_exclusion_sha256": declared_source[
                    "semantic_exclusion_sha256"
                ],
            },
            "labels": relabeled,
            "selection": {
                "selected_root_exact_overlap_states": 0,
                "cross_split_option_final_exact_overlap_states": 0,
                "train_option_final_to_holdout_root_overlap_states": 0,
                "holdout_option_final_to_train_root_overlap_states": 0,
            },
            "quality": {
                "status": "complete",
                "accepted_roots": len(relabeled),
                "train_roots": len(relabeled),
                "holdout_roots": 0,
            },
            "contract": {
                **dict(source["contract"]),
                "development_import_unpaired": True,
                "split_artifact_isolated": True,
            },
        }
    )
    payload = {
        **deterministic,
        "runtime": {
            "importer_script_sha256": _sha256(Path(__file__).resolve()),
            "python": sys.version,
        },
    }
    payload["corpus_id"] = "spc-native-mixed-teacher-" + hashlib.sha256(
        _canonical_json(deterministic)
    ).hexdigest()[:20]
    return payload


def _validate_development_import(
    payload: Mapping[str, Any],
    preregistration: Preregistration,
    *,
    supplied_path: Path,
    declared_source_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    declared_source = preregistration.manifest["trajectory_corpora"]["train"].get(
        "artifact_source"
    )
    if not isinstance(declared_source, Mapping):
        raise ValueError("preregistration does not declare a consumed development source")
    artifact = payload.get("artifact")
    if not isinstance(artifact, Mapping) or artifact.get("schema") != DEVELOPMENT_IMPORT_SCHEMA:
        raise ValueError("train input is not a development import artifact")
    canonical_path = artifact.get("canonical_output_path")
    if not isinstance(canonical_path, str):
        raise ValueError("development import canonical path binding is missing")
    _require_exact_canonical_input(supplied_path, canonical_path, "development import")
    if artifact.get("role") != "train-development" or artifact.get(
        "preregistration"
    ) != {"schema": preregistration.schema, "sha256": preregistration.sha256}:
        raise ValueError("development import preregistration binding differs")
    expected_source_binding = {
        name: declared_source[name]
        for name in ("corpus_id", "semantic_sha256", "raw_artifact_sha256")
    }
    if artifact.get("source_combined_corpus") != expected_source_binding:
        raise ValueError("development import source binding differs")
    if artifact.get("consumption_evidence") != declared_source["consumption_evidence"]:
        raise ValueError("development import consumption evidence differs")
    labels = payload.get("labels")
    if (
        not isinstance(labels, list)
        or len(labels) != preregistration.expected_train_labels
        or any(not isinstance(raw, Mapping) or raw.get("split") != "train" for raw in labels)
    ):
        raise ValueError("development import label coverage differs")
    source = (
        declared_source_snapshot
        if declared_source_snapshot is not None
        else _read_declared_artifact_source(preregistration, declared_source)
    )
    _require_labels_derived_from_declared_source(
        labels,
        source,
        declared_source,
        development_relabel=True,
        artifact_split="train",
    )
    semantic_sha, roots, finals = _raw_semantic_commitment(labels)
    payload_sha = _raw_label_payload_commitment(labels)
    if (
        artifact.get("semantic_keys_sha256") != semantic_sha
        or artifact.get("label_payload_sha256") != payload_sha
        or artifact.get("semantic_exclusion_sha256")
        != semantic_exclusion_sha256(roots | finals)
        or artifact.get("semantic_exclusion_sha256")
        != declared_source["semantic_exclusion_sha256"]
    ):
        raise ValueError("development import label commitments differ")
    _materialize_labels(payload, selected_split="train")
    return {
        "semantic_keys_sha256": semantic_sha,
        "label_payload_sha256": payload_sha,
        "root_state_keys": roots,
        "option_final_state_keys": finals,
        "source_binding": expected_source_binding,
        "declared_source_snapshot": source,
    }


def _import_development_command(args: argparse.Namespace) -> None:
    with _protocol_stage_lock("import-development", exclusive=False):
        _import_development_command_locked(args)


def _import_development_command_locked(args: argparse.Namespace) -> None:
    preregistration = _load_preregistration(
        args.preregistration, forbid_pair_preparation=True
    )
    declared_source = preregistration.manifest["trajectory_corpora"]["train"].get(
        "artifact_source"
    )
    if not isinstance(declared_source, Mapping):
        raise ValueError("preregistration has no frozen consumed-development source")
    source_path = _require_exact_canonical_input(
        args.consumed_source,
        str(declared_source["path"]),
        "consumed development source",
    )
    output = args.output.expanduser().resolve()
    if str(args.output) != str(output):
        raise ValueError(
            "development import output must be absolute and canonical; aliases are forbidden"
        )
    _require_protocol_registry_isolation(
        preregistration,
        {
            "consumed source": source_path,
            "output": output,
            "consumption evidence": Path(
                str(declared_source["consumption_evidence"]["path"])
            ),
        },
        label="import-development",
    )
    if args.dry_run and output.exists():
        raise FileExistsError("development import output already exists")
    source = _read_declared_artifact_source(preregistration, declared_source)
    payload = _development_import_payload(
        source,
        declared_source=declared_source,
        preregistration=preregistration,
        output_path=output,
    )
    integrity = _validate_development_import(
        payload,
        preregistration,
        supplied_path=output,
        declared_source_snapshot=source,
    )
    output_raw_sha: str | None = None
    if not args.dry_run:
        final_source = _read_declared_artifact_source(
            preregistration, declared_source
        )
        final_payload = _development_import_payload(
            final_source,
            declared_source=declared_source,
            preregistration=preregistration,
            output_path=output,
        )
        final_integrity = _validate_development_import(
            final_payload,
            preregistration,
            supplied_path=output,
            declared_source_snapshot=final_source,
        )
        if (
            final_source != source
            or final_payload != payload
            or final_integrity != integrity
        ):
            raise ValueError(
                "development source or consumption evidence changed before import publication"
            )
        expected_raw_sha = hashlib.sha256(_pretty_json_bytes(payload)).hexdigest()
        if output.exists():
            existing, existing_raw_sha = _read_json_artifact(output)
            if existing != payload or existing_raw_sha != expected_raw_sha:
                raise FileExistsError("development import output already differs")
        else:
            _atomic_exclusive_json(
                output,
                payload,
                conflict_message="development import output already exists",
            )
        written, written_raw_sha = _read_json_artifact(output)
        closing_source = _read_declared_artifact_source(
            preregistration, declared_source
        )
        closing_integrity = _validate_development_import(
            written,
            preregistration,
            supplied_path=output,
            declared_source_snapshot=closing_source,
        )
        if (
            written != payload
            or written_raw_sha != expected_raw_sha
            or closing_source != source
            or closing_integrity != integrity
        ):
            raise ValueError(
                "development import or its source lineage changed during publication"
            )
        output_raw_sha = written_raw_sha
    print(
        json.dumps(
            {
                "dry_run": bool(args.dry_run),
                "output": str(output),
                "labels": len(payload["labels"]),
                "semantic_keys_sha256": integrity["semantic_keys_sha256"],
                "label_payload_sha256": integrity["label_payload_sha256"],
                "semantic_sha256": _teacher_semantic_sha256(payload),
                "raw_artifact_sha256": output_raw_sha,
            },
            indent=2,
            sort_keys=True,
        )
    )


def _source_binding_from_artifact(
    payload: Mapping[str, Any],
    *,
    raw_sha256: str,
) -> dict[str, str]:
    artifact = payload.get("artifact")
    if isinstance(artifact, Mapping):
        source = artifact.get("source_combined_corpus")
        if artifact.get("schema") in {
            SPLIT_ARTIFACT_SCHEMA,
            DEVELOPMENT_IMPORT_SCHEMA,
        }:
            return _validated_source_binding(source, "teacher input")
    corpus_id = payload.get("corpus_id")
    if not isinstance(corpus_id, str):
        raise ValueError("teacher source corpus ID is missing")
    return {
        "corpus_id": corpus_id,
        "semantic_sha256": _teacher_semantic_sha256(payload),
        "raw_artifact_sha256": raw_sha256,
    }


def _require_distinct_source_bindings(
    train_source: Mapping[str, str],
    holdout_source: Mapping[str, str],
) -> None:
    if any(
        train_source.get(name) == holdout_source.get(name)
        for name in ("corpus_id", "semantic_sha256", "raw_artifact_sha256")
    ):
        raise ValueError("distinct-source pairing received the same source corpus")


def _pair_train_input(
    payload: Mapping[str, Any],
    raw_sha256: str,
    path: Path,
    preregistration: Preregistration,
) -> tuple[
    list[Mapping[str, Any]],
    dict[str, str],
    Mapping[str, Any] | None,
]:
    artifact = payload.get("artifact")
    if isinstance(artifact, Mapping) and artifact.get("schema") == DEVELOPMENT_IMPORT_SCHEMA:
        integrity = _validate_development_import(
            payload, preregistration, supplied_path=path
        )
        return (
            list(payload["labels"]),
            dict(integrity["source_binding"]),
            integrity["declared_source_snapshot"],
        )
    _canonical_cli_input(path, "pair train artifact")
    _validate_split_artifact(
        payload, preregistration, expected_artifact_split="train"
    )
    _materialize_labels(payload, selected_split="train")
    return (
        list(payload["labels"]),
        _source_binding_from_artifact(payload, raw_sha256=raw_sha256),
        None,
    )


def _pair_holdout_input(
    payload: Mapping[str, Any],
    raw_sha256: str,
    path: Path,
    preregistration: Preregistration,
) -> tuple[list[Mapping[str, Any]], dict[str, str]]:
    _canonical_cli_input(path, "pair sealed-holdout source")
    artifact = payload.get("artifact")
    if isinstance(artifact, Mapping) and artifact.get("schema") == SPLIT_ARTIFACT_SCHEMA:
        raise ValueError(
            "pair-artifacts requires the original fresh combined holdout source; "
            "an already split holdout cannot be rebound"
        )
    _reject_quarantined_holdout(payload)
    _validate_combined_corpus_preregistration(
        payload,
        preregistration,
        supplied_path=path,
        supplied_raw_sha256=raw_sha256,
    )
    _materialize_labels(payload, selected_split=None)
    labels = [
        raw
        for raw in payload["labels"]
        if isinstance(raw, Mapping) and raw.get("split") == "holdout"
    ]
    if len(labels) != preregistration.expected_holdout_labels:
        raise ValueError("fresh combined source holdout count differs")
    return labels, _source_binding_from_artifact(payload, raw_sha256=raw_sha256)


def _publish_pair_directory(
    output: Path,
    train: Mapping[str, Any],
    holdout: Mapping[str, Any],
    preparation_claim_path: Path,
    preparation_source_path: Path,
) -> tuple[Path, Path]:
    if not output.is_dir():
        raise FileExistsError("paired-artifact output reservation is missing")
    train_path = output / "train-teacher-artifact.json"
    holdout_path = output / "sealed-holdout-teacher-artifact.json"
    publication_path = output / "pair-publication-receipt.json"
    allowed_paths = {train_path, holdout_path, publication_path}
    unexpected = sorted(path.name for path in output.iterdir() if path not in allowed_paths)
    if unexpected:
        raise FileExistsError(
            "paired-artifact output contains unexpected files: " + ", ".join(unexpected)
        )
    train_raw_sha = hashlib.sha256(_pretty_json_bytes(train)).hexdigest()
    holdout_raw_sha = hashlib.sha256(_pretty_json_bytes(holdout)).hexdigest()
    train_artifact = train["artifact"]
    holdout_artifact = holdout["artifact"]
    preparation_claim, preparation_claim_raw_sha = _read_json_artifact(
        preparation_claim_path
    )
    if preparation_claim.get("schema") != HOLDOUT_PREPARATION_CLAIM_SCHEMA:
        raise ValueError("pair preparation claim schema differs before publication")
    preparation_source, preparation_source_raw_sha = _read_json_artifact(
        preparation_source_path
    )
    if preparation_source.get("schema") != HOLDOUT_PREPARATION_SOURCE_SCHEMA:
        raise ValueError("pair preparation source binding differs before publication")
    sealed_source = _require_exact_fields(
        preparation_source.get("sealed_holdout_source"),
        {"path", "raw_artifact_sha256"},
        "pair preparation sealed source",
    )
    sealed_source_path = Path(sealed_source["path"])
    _, sealed_source_raw_sha = _read_json_artifact(sealed_source_path)
    if sealed_source_raw_sha != sealed_source["raw_artifact_sha256"]:
        raise ValueError("sealed preparation source changed before publication")
    train_source = preparation_source.get("train_source")
    train_source_path: Path | None = None
    train_source_raw_sha: str | None = None
    if train_source is not None:
        train_source = _require_exact_fields(
            train_source,
            {"path", "raw_artifact_sha256"},
            "pair preparation train source",
        )
        train_source_path = Path(str(train_source["path"]))
        _, train_source_raw_sha = _read_json_artifact(train_source_path)
        if train_source_raw_sha != train_source["raw_artifact_sha256"]:
            raise ValueError("train preparation source changed before publication")
    publication = {
        "schema": PAIR_PUBLICATION_SCHEMA,
        "output_directory": str(output),
        "preregistration": dict(train_artifact["preregistration"]),
        "dataset_pairing_sha256": train_artifact["dataset_pairing_sha256"],
        "source_pairing_mode": train_artifact["source_pairing_mode"],
        "preparation_claim": {
            "path": str(preparation_claim_path),
            "raw_artifact_sha256": preparation_claim_raw_sha,
        },
        "preparation_source_binding": {
            "path": str(preparation_source_path),
            "raw_artifact_sha256": preparation_source_raw_sha,
        },
        "train": {
            "path": str(train_path),
            "raw_artifact_sha256": train_raw_sha,
            "semantic_sha256": _teacher_semantic_sha256(train),
            "source_combined_corpus": dict(
                train_artifact["source_combined_corpus"]
            ),
        },
        "sealed_holdout": {
            "path": str(holdout_path),
            "raw_artifact_sha256": holdout_raw_sha,
            "semantic_sha256": _teacher_semantic_sha256(holdout),
            "source_combined_corpus": dict(
                holdout_artifact["source_combined_corpus"]
            ),
        },
    }
    train_exists = train_path.exists()
    holdout_exists = holdout_path.exists()
    publication_exists = publication_path.exists()
    if holdout_exists and not train_exists:
        raise FileExistsError("sealed holdout artifact exists without train artifact")
    if publication_exists and not holdout_exists:
        raise FileExistsError("pair publication receipt exists without both artifacts")
    for path, payload, expected_raw, label in (
        (train_path, train, train_raw_sha, "train artifact"),
        (holdout_path, holdout, holdout_raw_sha, "sealed holdout artifact"),
    ):
        if path.exists():
            existing, existing_raw = _read_json_artifact(path)
            if existing != payload or existing_raw != expected_raw:
                raise FileExistsError(f"{label} already differs")
        else:
            _atomic_exclusive_json(
                path, payload, conflict_message=f"{label} already exists"
            )
    if publication_path.exists():
        existing_publication, _ = _read_json_artifact(publication_path)
        if existing_publication != publication:
            raise FileExistsError("pair publication receipt already differs")
    else:
        _atomic_exclusive_json(
            publication_path,
            publication,
            conflict_message="pair publication receipt already exists",
        )
    final_claim, final_claim_raw = _read_json_artifact(preparation_claim_path)
    final_source, final_source_raw = _read_json_artifact(preparation_source_path)
    _, final_sealed_source_raw = _read_json_artifact(sealed_source_path)
    final_train_source_raw = (
        None
        if train_source_path is None
        else _read_json_artifact(train_source_path)[1]
    )
    final_train_bytes, train_file_identity = _read_stable_file_snapshot(
        train_path, label="paired train artifact"
    )
    final_holdout_bytes, sealed_file_identity = _read_stable_file_snapshot(
        holdout_path, label="paired sealed-holdout artifact"
    )
    try:
        final_train = json.loads(final_train_bytes.decode("utf-8"))
        final_holdout = json.loads(final_holdout_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("paired artifact stable snapshot is malformed") from error
    final_train_raw = hashlib.sha256(final_train_bytes).hexdigest()
    final_holdout_raw = hashlib.sha256(final_holdout_bytes).hexdigest()
    final_publication, final_publication_raw = _read_json_artifact(publication_path)
    if (
        final_claim != preparation_claim
        or final_claim_raw != preparation_claim_raw_sha
        or final_source != preparation_source
        or final_source_raw != preparation_source_raw_sha
        or final_sealed_source_raw != sealed_source_raw_sha
        or final_train_source_raw != train_source_raw_sha
        or final_train != train
        or final_train_raw != train_raw_sha
        or final_holdout != holdout
        or final_holdout_raw != holdout_raw_sha
        or final_publication != publication
    ):
        raise ValueError("pair publication changed during atomic completion")
    if train_file_identity == sealed_file_identity:
        raise ValueError("paired train and sealed holdout share one file identity")
    completion_path = _pair_completion_registry_path_from_claim(
        preparation_claim_path
    )
    completion = {
        "schema": PAIR_COMPLETION_REGISTRY_SCHEMA,
        "preregistration": dict(train_artifact["preregistration"]),
        "output_directory": str(output),
        "dataset_pairing_sha256": train_artifact["dataset_pairing_sha256"],
        "source_pairing_mode": train_artifact["source_pairing_mode"],
        "preparation_claim": {
            "path": str(preparation_claim_path),
            "raw_artifact_sha256": preparation_claim_raw_sha,
        },
        "preparation_source_binding": {
            "path": str(preparation_source_path),
            "raw_artifact_sha256": preparation_source_raw_sha,
        },
        "local_publication": {
            "path": str(publication_path),
            "raw_artifact_sha256": final_publication_raw,
        },
        "train": {
            "path": str(train_path),
            "raw_artifact_sha256": train_raw_sha,
            "file_identity": train_file_identity,
        },
        "sealed_holdout": {
            "path": str(holdout_path),
            "raw_artifact_sha256": holdout_raw_sha,
            "file_identity": sealed_file_identity,
        },
        "lineage_validation": {
            "mode": "full-recursive-before-central-completion",
            "train_raw_artifact_sha256": train_raw_sha,
            "sealed_holdout_raw_artifact_sha256": holdout_raw_sha,
        },
    }
    if completion_path.exists():
        existing_completion, existing_completion_raw = _read_json_artifact(
            completion_path
        )
        expected_completion_raw = hashlib.sha256(
            _pretty_json_bytes(completion)
        ).hexdigest()
        if (
            existing_completion != completion
            or existing_completion_raw != expected_completion_raw
        ):
            raise FileExistsError("central pair completion binding already differs")
    else:
        _atomic_exclusive_json(
            completion_path,
            completion,
            conflict_message="central pair completion binding already exists",
        )
    final_completion, final_completion_raw = _read_json_artifact(completion_path)
    if (
        final_completion != completion
        or final_completion_raw
        != hashlib.sha256(_pretty_json_bytes(completion)).hexdigest()
    ):
        raise ValueError("central pair completion binding changed during publication")
    tail_train_bytes, tail_train_identity = _read_stable_file_snapshot(
        train_path, label="tail paired train artifact"
    )
    tail_holdout_bytes, tail_holdout_identity = _read_stable_file_snapshot(
        holdout_path, label="tail paired sealed-holdout artifact"
    )
    if (
        hashlib.sha256(tail_train_bytes).hexdigest() != train_raw_sha
        or hashlib.sha256(tail_holdout_bytes).hexdigest() != holdout_raw_sha
        or tail_train_identity != train_file_identity
        or tail_holdout_identity != sealed_file_identity
        or tail_train_identity == tail_holdout_identity
    ):
        raise ValueError("paired artifacts changed around central completion")
    _fsync_directory(output)
    return train_path, holdout_path


def _validate_pair_publication(
    artifact_path: Path,
    artifact_raw_sha256: str,
    split_integrity: Mapping[str, Any],
    preregistration: Preregistration,
    *,
    forbidden_path: Path | None = None,
    forbidden_file_identity: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    publication_path = artifact_path.parent / "pair-publication-receipt.json"
    publication, publication_raw_sha = (
        _read_identity_isolated_json_artifact(
            publication_path,
            forbidden_path=forbidden_path,
            label="pair publication receipt",
            forbidden_file_identity=forbidden_file_identity,
        )
        if forbidden_path is not None
        else _read_json_artifact(publication_path)
    )
    publication = _require_exact_fields(
        publication,
        {
            "schema",
            "output_directory",
            "preregistration",
            "dataset_pairing_sha256",
            "source_pairing_mode",
            "preparation_claim",
            "preparation_source_binding",
            "train",
            "sealed_holdout",
        },
        "pair publication receipt",
    )
    if (
        publication["schema"] != PAIR_PUBLICATION_SCHEMA
        or publication["output_directory"] != str(artifact_path.parent)
        or publication["preregistration"]
        != {"schema": preregistration.schema, "sha256": preregistration.sha256}
        or publication["dataset_pairing_sha256"]
        != split_integrity["dataset_pairing_sha256"]
        or publication["source_pairing_mode"]
        != split_integrity["source_pairing_mode"]
    ):
        raise ValueError("pair publication receipt binding differs")
    preparation = _require_exact_fields(
        publication["preparation_claim"],
        {"path", "raw_artifact_sha256"},
        "pair publication preparation claim",
    )
    expected_claim_path = _holdout_preparation_claim_path(preregistration)
    if (
        preparation.get("path") != str(expected_claim_path)
        or not isinstance(preparation.get("raw_artifact_sha256"), str)
        or HEX_SHA256.fullmatch(str(preparation["raw_artifact_sha256"])) is None
    ):
        raise ValueError("pair publication preparation-claim binding differs")
    claim, claim_raw_sha = _read_json_artifact(expected_claim_path)
    claim = _require_exact_fields(
        claim,
        {
            "schema",
            "operation",
            "preregistration_schema",
            "preregistration_sha256",
            "holdout_seed",
            "requested_train_source",
            "requested_sealed_holdout_source",
            "requested_output",
            "burn_rule",
        },
        "pair preparation claim",
    )
    if (
        claim_raw_sha != preparation["raw_artifact_sha256"]
        or claim["schema"] != HOLDOUT_PREPARATION_CLAIM_SCHEMA
        or claim["operation"]
        not in {"pair-distinct-source-artifacts", "split-fresh-combined-artifacts"}
        or claim["preregistration_schema"] != preregistration.schema
        or claim["preregistration_sha256"] != preregistration.sha256
        or claim["holdout_seed"]
        != preregistration.manifest["trajectory_corpora"]["sealed_holdout"]["seed"]
        or claim["requested_output"] != str(artifact_path.parent)
        or claim["burn_rule"]
        != _seed_burn_rule(int(claim["holdout_seed"]))
    ):
        raise ValueError("pair preparation claim evidence differs")
    expected_operation = {
        "distinct-source-pair": "pair-distinct-source-artifacts",
        "same-source-split": "split-fresh-combined-artifacts",
    }.get(str(split_integrity["source_pairing_mode"]))
    if expected_operation is None or claim["operation"] != expected_operation:
        raise ValueError("pair preparation operation differs from pairing mode")
    if (
        expected_operation == "pair-distinct-source-artifacts"
        and not isinstance(claim["requested_train_source"], str)
    ) or (
        expected_operation == "split-fresh-combined-artifacts"
        and claim["requested_train_source"] is not None
    ):
        raise ValueError("pair preparation train request differs from pairing mode")
    source_reference = _require_exact_fields(
        publication["preparation_source_binding"],
        {"path", "raw_artifact_sha256"},
        "pair publication preparation source binding",
    )
    expected_source_path = _holdout_preparation_source_path(preregistration)
    if (
        source_reference["path"] != str(expected_source_path)
        or not isinstance(source_reference["raw_artifact_sha256"], str)
        or HEX_SHA256.fullmatch(source_reference["raw_artifact_sha256"]) is None
    ):
        raise ValueError("pair publication preparation-source reference differs")
    source_binding, source_binding_raw_sha = _read_json_artifact(
        expected_source_path
    )
    source_binding = _require_exact_fields(
        source_binding,
        {
            "schema",
            "operation",
            "preregistration_schema",
            "preregistration_sha256",
            "holdout_seed",
            "preparation_claim",
            "train_source",
            "sealed_holdout_source",
            "requested_output",
            "burn_rule",
        },
        "pair preparation source binding",
    )
    source_claim = _require_exact_fields(
        source_binding["preparation_claim"],
        {"path", "raw_artifact_sha256"},
        "pair preparation source claim",
    )
    protected_source = _require_exact_fields(
        source_binding["sealed_holdout_source"],
        {"path", "raw_artifact_sha256"},
        "pair preparation sealed source",
    )
    protected_train: Mapping[str, Any] | None
    if expected_operation == "pair-distinct-source-artifacts":
        protected_train = _require_exact_fields(
            source_binding["train_source"],
            {"path", "raw_artifact_sha256"},
            "pair preparation train source",
        )
    else:
        if source_binding["train_source"] is not None:
            raise ValueError("same-source split unexpectedly binds a train source")
        protected_train = None
    if (
        source_binding_raw_sha != source_reference["raw_artifact_sha256"]
        or source_binding["schema"] != HOLDOUT_PREPARATION_SOURCE_SCHEMA
        or source_binding["operation"] != expected_operation
        or source_binding["preregistration_schema"] != preregistration.schema
        or source_binding["preregistration_sha256"] != preregistration.sha256
        or source_binding["holdout_seed"]
        != preregistration.manifest["trajectory_corpora"]["sealed_holdout"][
            "seed"
        ]
        or source_claim
        != {
            "path": str(expected_claim_path),
            "raw_artifact_sha256": claim_raw_sha,
        }
        or protected_source["path"]
        != claim["requested_sealed_holdout_source"]
        or not isinstance(protected_source["raw_artifact_sha256"], str)
        or HEX_SHA256.fullmatch(protected_source["raw_artifact_sha256"]) is None
        or source_binding["requested_output"] != claim["requested_output"]
        or source_binding["burn_rule"] != claim["burn_rule"]
        or (
            protected_train is not None
            and (
                protected_train["path"] != claim["requested_train_source"]
                or not isinstance(
                    protected_train["raw_artifact_sha256"], str
                )
                or HEX_SHA256.fullmatch(
                    protected_train["raw_artifact_sha256"]
                )
                is None
            )
        )
    ):
        raise ValueError("pair preparation source evidence differs")
    if protected_train is not None:
        train_source_path = Path(str(protected_train["path"]))
        _, current_train_source_raw = (
            _read_identity_isolated_json_artifact(
                train_source_path,
                forbidden_path=forbidden_path,
                label="paired train source",
                forbidden_file_identity=forbidden_file_identity,
            )
            if forbidden_path is not None
            else _read_json_artifact(train_source_path)
        )
        if current_train_source_raw != protected_train["raw_artifact_sha256"]:
            raise ValueError("paired train source changed after preparation binding")
    entries: dict[str, Mapping[str, Any]] = {}
    expected_paths = {
        "train": artifact_path.parent / "train-teacher-artifact.json",
        "sealed_holdout": (
            artifact_path.parent / "sealed-holdout-teacher-artifact.json"
        ),
    }
    for split in ("train", "sealed_holdout"):
        entry = _require_exact_fields(
            publication[split],
            {
                "path",
                "raw_artifact_sha256",
                "semantic_sha256",
                "source_combined_corpus",
            },
            f"pair publication {split}",
        )
        _validated_source_binding(
            entry["source_combined_corpus"], f"pair publication {split}"
        )
        if any(
            not isinstance(entry[name], str)
            or HEX_SHA256.fullmatch(entry[name]) is None
            for name in ("raw_artifact_sha256", "semantic_sha256")
        ):
            raise ValueError(f"pair publication {split} hashes are malformed")
        if entry["path"] != str(expected_paths[split]):
            raise ValueError(f"pair publication {split} path differs")
        entries[split] = entry
    protected_raw_sha = protected_source["raw_artifact_sha256"]
    if expected_operation == "split-fresh-combined-artifacts":
        if any(
            entry["source_combined_corpus"]["raw_artifact_sha256"]
            != protected_raw_sha
            for entry in entries.values()
        ):
            raise ValueError("same-source publication is not bound to claimed bytes")
    elif (
        entries["sealed_holdout"]["source_combined_corpus"][
            "raw_artifact_sha256"
        ]
        != protected_raw_sha
    ):
        raise ValueError("distinct-source publication is not bound to claimed bytes")
    artifact_split = str(split_integrity["artifact_split"])
    current = entries[artifact_split]
    counterpart_split = (
        "sealed_holdout" if artifact_split == "train" else "train"
    )
    counterpart = entries[counterpart_split]
    if (
        current["path"] != str(artifact_path)
        or current["raw_artifact_sha256"] != artifact_raw_sha256
        or current["semantic_sha256"]
        != split_integrity["artifact_semantic_sha256"]
        or current["source_combined_corpus"]
        != split_integrity["source_combined_corpus"]
        or counterpart["source_combined_corpus"]
        != split_integrity["counterpart_source_combined_corpus"]
    ):
        raise ValueError("pair publication artifact evidence differs")
    return {
        "path": str(publication_path),
        "raw_artifact_sha256": publication_raw_sha,
        "train_path": str(expected_paths["train"]),
        "train_raw_artifact_sha256": str(entries["train"]["raw_artifact_sha256"]),
        "sealed_holdout_path": str(expected_paths["sealed_holdout"]),
        "sealed_holdout_raw_artifact_sha256": str(
            entries["sealed_holdout"]["raw_artifact_sha256"]
        ),
    }


def _trusted_pair_completion(
    preregistration: Preregistration,
) -> tuple[dict[str, Any], str]:
    """Load the central completion binding without touching pair artifacts."""

    completion_path = _pair_completion_registry_path(preregistration)
    completion, completion_raw_sha = _read_json_artifact(completion_path)
    completion = dict(
        _require_exact_fields(
            completion,
            {
                "schema",
                "preregistration",
                "output_directory",
                "dataset_pairing_sha256",
                "source_pairing_mode",
                "preparation_claim",
                "preparation_source_binding",
                "local_publication",
                "train",
                "sealed_holdout",
                "lineage_validation",
            },
            "central pair completion binding",
        )
    )
    expected_claim_path = _holdout_preparation_claim_path(preregistration)
    expected_source_path = _holdout_preparation_source_path(preregistration)
    claim, claim_raw_sha = _read_json_artifact(expected_claim_path)
    source, source_raw_sha = _read_json_artifact(expected_source_path)
    claim_reference = _require_exact_fields(
        completion["preparation_claim"],
        {"path", "raw_artifact_sha256"},
        "central pair completion claim",
    )
    source_reference = _require_exact_fields(
        completion["preparation_source_binding"],
        {"path", "raw_artifact_sha256"},
        "central pair completion source binding",
    )
    output_directory = Path(str(completion["output_directory"]))
    train = _require_exact_fields(
        completion["train"],
        {"path", "raw_artifact_sha256", "file_identity"},
        "central pair completion train",
    )
    sealed = _require_exact_fields(
        completion["sealed_holdout"],
        {"path", "raw_artifact_sha256", "file_identity"},
        "central pair completion sealed holdout",
    )
    local = _require_exact_fields(
        completion["local_publication"],
        {"path", "raw_artifact_sha256"},
        "central pair completion local publication",
    )
    train_identity = _validated_file_identity(train["file_identity"], "central train")
    sealed_identity = _validated_file_identity(
        sealed["file_identity"], "central sealed holdout"
    )
    lineage = _require_exact_fields(
        completion["lineage_validation"],
        {
            "mode",
            "train_raw_artifact_sha256",
            "sealed_holdout_raw_artifact_sha256",
        },
        "central pair completion lineage validation",
    )
    for label, binding in (
        ("train", train),
        ("sealed holdout", sealed),
        ("local publication", local),
    ):
        if (
            not isinstance(binding["path"], str)
            or not Path(str(binding["path"])).is_absolute()
            or not isinstance(binding["raw_artifact_sha256"], str)
            or HEX_SHA256.fullmatch(str(binding["raw_artifact_sha256"])) is None
        ):
            raise ValueError(f"central pair completion {label} binding is malformed")
    expected_train_path = output_directory / "train-teacher-artifact.json"
    expected_sealed_path = output_directory / "sealed-holdout-teacher-artifact.json"
    expected_local_path = output_directory / "pair-publication-receipt.json"
    expected_preregistration = {
        "schema": preregistration.schema,
        "sha256": preregistration.sha256,
    }
    if (
        completion["schema"] != PAIR_COMPLETION_REGISTRY_SCHEMA
        or completion["preregistration"] != expected_preregistration
        or claim_reference
        != {"path": str(expected_claim_path), "raw_artifact_sha256": claim_raw_sha}
        or source_reference
        != {"path": str(expected_source_path), "raw_artifact_sha256": source_raw_sha}
        or train["path"] != str(expected_train_path)
        or sealed["path"] != str(expected_sealed_path)
        or local["path"] != str(expected_local_path)
        or claim.get("requested_output") != str(output_directory)
        or source.get("requested_output") != str(output_directory)
        or claim.get("preregistration_sha256") != preregistration.sha256
        or source.get("preregistration_sha256") != preregistration.sha256
        or source.get("preparation_claim") != claim_reference
        or train_identity == sealed_identity
        or lineage
        != {
            "mode": "full-recursive-before-central-completion",
            "train_raw_artifact_sha256": train["raw_artifact_sha256"],
            "sealed_holdout_raw_artifact_sha256": sealed[
                "raw_artifact_sha256"
            ],
        }
    ):
        raise ValueError("central pair completion binding differs")
    return completion, completion_raw_sha


def _pair_artifacts_command(args: argparse.Namespace) -> None:
    with _protocol_stage_lock("pair-artifacts", exclusive=True):
        _pair_artifacts_command_locked(args)


def _pair_artifacts_command_locked(args: argparse.Namespace) -> None:
    preregistration = _load_preregistration(args.preregistration)
    if _pair_completion_registry_path(preregistration).exists():
        raise FileExistsError(
            "cycle-4 pair publication is already complete; upstream artifact "
            "preparation is permanently closed"
        )
    if args.dry_run and not args.metadata_only:
        raise ValueError(
            "pair-artifacts --dry-run is forbidden because opening the sealed "
            "holdout consumes the seed; use --metadata-only for a non-consuming preview"
        )
    train_path = args.train_artifact
    holdout_path = args.sealed_holdout_source
    if str(train_path) == str(holdout_path):
        raise ValueError("train and sealed-holdout inputs must be distinct files")
    requested_output = args.output
    _require_lexical_absolute_input(train_path, "pair train artifact")
    _require_lexical_absolute_input(
        holdout_path, "pair sealed-holdout source"
    )
    _require_lexical_absolute_input(requested_output, "pair output")
    preparation_claim_expected = _holdout_preparation_claim_path(preregistration)
    preparation_source_expected = _holdout_preparation_source_path(preregistration)
    _require_pairwise_nonoverlapping_paths(
        {
            "train artifact": train_path,
            "sealed holdout source": holdout_path,
            "output": requested_output,
            "holdout claim": _holdout_claim_path(preregistration),
            "preparation claim": preparation_claim_expected,
            "preparation source binding": preparation_source_expected,
            "preregistration": preregistration.path,
        },
        label="pair-artifacts",
    )
    _require_protocol_registry_isolation(
        preregistration,
        {
            "train artifact": train_path,
            "sealed holdout source": holdout_path,
            "output": requested_output,
        },
        label="pair-artifacts",
    )
    if args.metadata_only:
        print(
            json.dumps(
                {
                    "metadata_only": True,
                    "dry_run": True,
                    "preregistration_sha256": preregistration.sha256,
                    "train_path": str(train_path),
                    "train_source": None,
                    "requested_sealed_holdout_source": str(holdout_path),
                    "output": str(requested_output),
                    "artifacts_opened": False,
                    "holdout_opened": False,
                    "preparation_claim_created": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    resuming = preparation_claim_expected.exists()
    if resuming and not preparation_source_expected.exists():
        raise FileExistsError(
            "paired-artifact preparation stopped after the sealed source may have "
            "been opened but before its byte binding was durable; this seed is burned"
        )
    if not resuming and preparation_source_expected.exists():
        raise FileExistsError(
            "paired-artifact source binding exists without its preparation claim"
        )
    preparation_claim_path = _claim_holdout_preparation(
        preregistration,
        requested_source=holdout_path,
        requested_train_source=train_path,
        requested_output=requested_output,
        operation="pair-distinct-source-artifacts",
        allow_identical_resume=resuming,
    )
    output = requested_output.expanduser().resolve()
    if str(requested_output) != str(output):
        raise ValueError("pair output path must be canonical; aliases are forbidden")
    if not resuming and output.exists():
        raise FileExistsError("paired-artifact output directory already exists")
    if resuming and not output.exists():
        raise FileExistsError(
            "paired-artifact source binding exists without its output reservation"
        )
    if output.exists():
        if not output.is_dir():
            raise FileExistsError("paired-artifact output reservation is not a directory")
    else:
        _reserve_output_directory(output, "paired-artifact")
    canonical_holdout_path = _canonical_cli_input(
        holdout_path, "pair sealed-holdout source"
    )
    holdout_payload, holdout_raw_sha = _read_json_artifact(canonical_holdout_path)
    canonical_train_path = _canonical_cli_input(train_path, "pair train artifact")
    train_payload, train_raw_sha = _read_json_artifact(canonical_train_path)
    preparation_source_path = _bind_holdout_preparation_source(
        preregistration,
        preparation_claim_path=preparation_claim_path,
        operation="pair-distinct-source-artifacts",
        sealed_source_path=canonical_holdout_path,
        sealed_source_raw_sha256=holdout_raw_sha,
        train_source_path=canonical_train_path,
        train_source_raw_sha256=train_raw_sha,
        requested_output=requested_output,
        allow_identical_resume=True,
    )
    # No validation occurs until one atomic source binding freezes both caller-
    # controlled byte snapshots. Exact resume can therefore never substitute a
    # different train corpus while repeatedly reopening the sealed source.
    train_labels, train_source, train_source_snapshot = _pair_train_input(
        train_payload,
        train_raw_sha,
        canonical_train_path,
        preregistration,
    )
    holdout_labels, holdout_source = _pair_holdout_input(
        holdout_payload,
        holdout_raw_sha,
        canonical_holdout_path,
        preregistration,
    )
    _require_distinct_source_bindings(train_source, holdout_source)
    train_semantic, train_roots, train_finals = _raw_semantic_commitment(train_labels)
    holdout_semantic, holdout_roots, holdout_finals = _raw_semantic_commitment(
        holdout_labels
    )
    train_payload_sha = _raw_label_payload_commitment(train_labels)
    holdout_payload_sha = _raw_label_payload_commitment(holdout_labels)
    intersections = _require_clean_cross_artifact_split(
        train_roots=train_roots,
        train_finals=train_finals,
        holdout_roots=holdout_roots,
        holdout_finals=holdout_finals,
    )
    cross_audit_sha = hashlib.sha256(_canonical_json(intersections)).hexdigest()
    pairing_sha = _dataset_pairing_sha256(
        preregistration_sha256=preregistration.sha256,
        train_semantic_keys_sha256=train_semantic,
        holdout_semantic_keys_sha256=holdout_semantic,
        train_label_payload_sha256=train_payload_sha,
        holdout_label_payload_sha256=holdout_payload_sha,
        cross_split_audit_sha256=cross_audit_sha,
        train_source=train_source,
        holdout_source=holdout_source,
        source_pairing_mode="distinct-source-pair",
    )
    train_source_payload = {**train_payload, "corpus_id": train_source["corpus_id"]}
    holdout_source_payload = {
        **holdout_payload,
        "corpus_id": holdout_source["corpus_id"],
    }
    train = _split_artifact_payload(
        train_source_payload,
        source_raw_sha256=train_source["raw_artifact_sha256"],
        source_semantic_sha256=train_source["semantic_sha256"],
        preregistration=preregistration,
        artifact_split="train",
        own_labels=train_labels,
        own_semantic_sha256=train_semantic,
        counterpart_semantic_sha256=holdout_semantic,
        own_label_payload_sha256=train_payload_sha,
        counterpart_label_payload_sha256=holdout_payload_sha,
        cross_split_audit_sha256=cross_audit_sha,
        dataset_pairing_sha256=pairing_sha,
        counterpart_source=holdout_source,
        source_pairing_mode="distinct-source-pair",
    )
    holdout = _split_artifact_payload(
        holdout_source_payload,
        source_raw_sha256=holdout_source["raw_artifact_sha256"],
        source_semantic_sha256=holdout_source["semantic_sha256"],
        preregistration=preregistration,
        artifact_split="sealed_holdout",
        own_labels=holdout_labels,
        own_semantic_sha256=holdout_semantic,
        counterpart_semantic_sha256=train_semantic,
        own_label_payload_sha256=holdout_payload_sha,
        counterpart_label_payload_sha256=train_payload_sha,
        cross_split_audit_sha256=cross_audit_sha,
        dataset_pairing_sha256=pairing_sha,
        counterpart_source=train_source,
        source_pairing_mode="distinct-source-pair",
    )
    _validate_split_artifact(
        train,
        preregistration,
        expected_artifact_split="train",
        declared_source_snapshot=train_source_snapshot,
    )
    _validate_split_artifact(
        holdout,
        preregistration,
        expected_artifact_split="sealed_holdout",
    )
    final_train_path, final_holdout_path = _publish_pair_directory(
        output,
        train,
        holdout,
        preparation_claim_path,
        preparation_source_path,
    )
    print(
        json.dumps(
            {
                "dry_run": False,
                "preregistration_sha256": preregistration.sha256,
                "dataset_pairing_sha256": pairing_sha,
                "preparation_claim": {
                    "path": str(preparation_claim_path),
                    "raw_artifact_sha256": _sha256(preparation_claim_path),
                },
                "preparation_source_binding": {
                    "path": str(preparation_source_path),
                    "raw_artifact_sha256": _sha256(preparation_source_path),
                },
                "train": {
                    "path": str(final_train_path),
                    "source": train_source,
                    "labels": len(train_labels),
                    "semantic_sha256": _teacher_semantic_sha256(train),
                    "raw_artifact_sha256": _sha256(final_train_path),
                },
                "sealed_holdout": {
                    "path": str(final_holdout_path),
                    "source": holdout_source,
                    "labels": len(holdout_labels),
                    "semantic_sha256": _teacher_semantic_sha256(holdout),
                    "raw_artifact_sha256": _sha256(final_holdout_path),
                },
                "cross_split_intersection_counts": {
                    name: len(values) for name, values in intersections.items()
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


def _split_artifact_payload(
    source: Mapping[str, Any],
    *,
    source_raw_sha256: str,
    source_semantic_sha256: str,
    preregistration: Preregistration,
    artifact_split: str,
    own_labels: Sequence[Mapping[str, Any]],
    own_semantic_sha256: str,
    counterpart_semantic_sha256: str,
    own_label_payload_sha256: str,
    counterpart_label_payload_sha256: str,
    cross_split_audit_sha256: str,
    dataset_pairing_sha256: str,
    counterpart_source: Mapping[str, str],
    source_pairing_mode: str,
) -> dict[str, Any]:
    label_split = "train" if artifact_split == "train" else "holdout"
    deterministic = {
        key: value
        for key, value in source.items()
        if key
        not in {
            "artifact",
            "corpus_id",
            "labels",
            "quality",
            "runtime",
            "selection",
        }
    }
    deterministic.update(
        {
            "artifact": {
                "schema": SPLIT_ARTIFACT_SCHEMA,
                "split": artifact_split,
                "preregistration": {
                    "schema": preregistration.schema,
                    "sha256": preregistration.sha256,
                },
                "source_combined_corpus": {
                    "corpus_id": source["corpus_id"],
                    "semantic_sha256": source_semantic_sha256,
                    "raw_artifact_sha256": source_raw_sha256,
                },
                "counterpart_source_combined_corpus": dict(counterpart_source),
                "source_pairing_mode": source_pairing_mode,
                "dataset_pairing_sha256": dataset_pairing_sha256,
                "semantic_keys_sha256": own_semantic_sha256,
                "counterpart_semantic_keys_sha256": counterpart_semantic_sha256,
                "label_payload_sha256": own_label_payload_sha256,
                "counterpart_label_payload_sha256": (
                    counterpart_label_payload_sha256
                ),
                "source_cross_split_audit_sha256": cross_split_audit_sha256,
            },
            "labels": list(own_labels),
            "selection": {
                "selected_root_exact_overlap_states": 0,
                "cross_split_option_final_exact_overlap_states": 0,
                "train_option_final_to_holdout_root_overlap_states": 0,
                "holdout_option_final_to_train_root_overlap_states": 0,
                "source_cross_split_audit_sha256": cross_split_audit_sha256,
            },
            "quality": {
                "status": "complete",
                "accepted_roots": len(own_labels),
                "train_roots": len(own_labels) if label_split == "train" else 0,
                "holdout_roots": len(own_labels) if label_split == "holdout" else 0,
            },
            "contract": {
                **{
                    name: value
                    for name, value in dict(source["contract"]).items()
                    if name != "development_import_unpaired"
                },
                "split_artifact_isolated": True,
                "distinct_source_pair_complete": (
                    source_pairing_mode == "distinct-source-pair"
                ),
            },
        }
    )
    payload = {
        **deterministic,
        "runtime": {
            "splitter_script_sha256": _sha256(Path(__file__).resolve()),
            "python": sys.version,
        },
    }
    prefix = (
        "spc-native-mixed-teacher-exploratory-"
        if deterministic["contract"].get("exploratory_only") is True
        else "spc-native-mixed-teacher-"
    )
    payload["corpus_id"] = prefix + _teacher_semantic_sha256(payload)[:20]
    return payload


def _split_artifacts_command(args: argparse.Namespace) -> None:
    with _protocol_stage_lock("split-artifacts", exclusive=True):
        _split_artifacts_command_locked(args)


def _split_artifacts_command_locked(args: argparse.Namespace) -> None:
    preregistration = _load_preregistration(args.preregistration)
    if _pair_completion_registry_path(preregistration).exists():
        raise FileExistsError(
            "cycle-4 pair publication is already complete; upstream artifact "
            "preparation is permanently closed"
        )
    if preregistration.manifest["trajectory_corpora"]["train"].get(
        "artifact_source"
    ) is not None:
        raise ValueError(
            "split-artifacts is forbidden for a consumed-development preregistration; "
            "use import-development and pair-artifacts"
        )
    requested_source = args.teacher_corpus
    requested_output = args.output
    _require_lexical_absolute_input(requested_source, "combined teacher corpus")
    _require_lexical_absolute_input(requested_output, "split-artifact output")
    preparation_claim_expected = _holdout_preparation_claim_path(preregistration)
    preparation_source_expected = _holdout_preparation_source_path(preregistration)
    _require_pairwise_nonoverlapping_paths(
        {
            "combined teacher corpus": requested_source,
            "output": requested_output,
            "holdout claim": _holdout_claim_path(preregistration),
            "preparation claim": preparation_claim_expected,
            "preparation source binding": preparation_source_expected,
            "preregistration": preregistration.path,
        },
        label="split-artifacts",
    )
    _require_protocol_registry_isolation(
        preregistration,
        {
            "combined teacher corpus": requested_source,
            "output": requested_output,
        },
        label="split-artifacts",
    )
    resuming = preparation_claim_expected.exists()
    if resuming and not preparation_source_expected.exists():
        raise FileExistsError(
            "split-artifact preparation stopped after the sealed source may have "
            "been opened but before its byte binding was durable; this seed is burned"
        )
    if not resuming and preparation_source_expected.exists():
        raise FileExistsError(
            "split-artifact source binding exists without its preparation claim"
        )
    preparation_claim_path = _claim_holdout_preparation(
        preregistration,
        requested_source=requested_source,
        requested_output=requested_output,
        operation="split-fresh-combined-artifacts",
        allow_identical_resume=resuming,
    )
    output = requested_output.expanduser().resolve()
    if str(requested_output) != str(output):
        raise ValueError(
            "split-artifact output path must be canonical; aliases are forbidden"
        )
    if not resuming and output.exists():
        raise FileExistsError("split-artifact output already exists")
    if resuming and not output.exists():
        raise FileExistsError(
            "split-artifact source binding exists without its output reservation"
        )
    if output.exists():
        if not output.is_dir():
            raise FileExistsError("split-artifact output reservation is not a directory")
    else:
        _reserve_output_directory(output, "split-artifact")
    source_path = _canonical_cli_input(
        requested_source, "combined teacher corpus"
    )
    source, source_raw_sha = _read_json_artifact(source_path)
    preparation_source_path = _bind_holdout_preparation_source(
        preregistration,
        preparation_claim_path=preparation_claim_path,
        operation="split-fresh-combined-artifacts",
        sealed_source_path=source_path,
        sealed_source_raw_sha256=source_raw_sha,
        requested_output=requested_output,
        allow_identical_resume=True,
    )
    _reject_quarantined_holdout(source)
    _validate_combined_corpus_preregistration(
        source,
        preregistration,
        supplied_path=source_path,
        supplied_raw_sha256=source_raw_sha,
    )
    labels, _ = _materialize_labels(source, selected_split=None)
    train_labels = [
        raw for raw in source["labels"] if raw.get("split") == "train"
    ]
    holdout_labels = [
        raw for raw in source["labels"] if raw.get("split") == "holdout"
    ]
    if (
        len(train_labels) != preregistration.expected_train_labels
        or len(holdout_labels) != preregistration.expected_holdout_labels
        or len(labels) != len(train_labels) + len(holdout_labels)
    ):
        raise ValueError("combined teacher corpus counts differ from preregistration")
    train_semantic, train_roots, train_finals = _raw_semantic_commitment(train_labels)
    holdout_semantic, holdout_roots, holdout_finals = _raw_semantic_commitment(
        holdout_labels
    )
    train_label_payload = _raw_label_payload_commitment(train_labels)
    holdout_label_payload = _raw_label_payload_commitment(holdout_labels)
    cross_contamination = {
        "root_state_keys": sorted(train_roots & holdout_roots),
        "option_final_state_keys": sorted(train_finals & holdout_finals),
        "train_final_to_holdout_root": sorted(train_finals & holdout_roots),
        "holdout_final_to_train_root": sorted(holdout_finals & train_roots),
    }
    if any(cross_contamination.values()):
        raise ValueError("combined teacher corpus has cross-split semantic contamination")
    cross_audit_sha = hashlib.sha256(
        _canonical_json(cross_contamination)
    ).hexdigest()
    source_semantic_sha = _teacher_semantic_sha256(source)
    dataset_pairing_sha = _dataset_pairing_sha256(
        preregistration_sha256=preregistration.sha256,
        train_semantic_keys_sha256=train_semantic,
        holdout_semantic_keys_sha256=holdout_semantic,
        train_label_payload_sha256=train_label_payload,
        holdout_label_payload_sha256=holdout_label_payload,
        cross_split_audit_sha256=cross_audit_sha,
        train_source={
            "corpus_id": str(source["corpus_id"]),
            "semantic_sha256": source_semantic_sha,
            "raw_artifact_sha256": source_raw_sha,
        },
        holdout_source={
            "corpus_id": str(source["corpus_id"]),
            "semantic_sha256": source_semantic_sha,
            "raw_artifact_sha256": source_raw_sha,
        },
        source_pairing_mode="same-source-split",
    )
    train = _split_artifact_payload(
        source,
        source_raw_sha256=source_raw_sha,
        source_semantic_sha256=source_semantic_sha,
        preregistration=preregistration,
        artifact_split="train",
        own_labels=train_labels,
        own_semantic_sha256=train_semantic,
        counterpart_semantic_sha256=holdout_semantic,
        own_label_payload_sha256=train_label_payload,
        counterpart_label_payload_sha256=holdout_label_payload,
        cross_split_audit_sha256=cross_audit_sha,
        dataset_pairing_sha256=dataset_pairing_sha,
        counterpart_source={
            "corpus_id": str(source["corpus_id"]),
            "semantic_sha256": source_semantic_sha,
            "raw_artifact_sha256": source_raw_sha,
        },
        source_pairing_mode="same-source-split",
    )
    holdout = _split_artifact_payload(
        source,
        source_raw_sha256=source_raw_sha,
        source_semantic_sha256=source_semantic_sha,
        preregistration=preregistration,
        artifact_split="sealed_holdout",
        own_labels=holdout_labels,
        own_semantic_sha256=holdout_semantic,
        counterpart_semantic_sha256=train_semantic,
        own_label_payload_sha256=holdout_label_payload,
        counterpart_label_payload_sha256=train_label_payload,
        cross_split_audit_sha256=cross_audit_sha,
        dataset_pairing_sha256=dataset_pairing_sha,
        counterpart_source={
            "corpus_id": str(source["corpus_id"]),
            "semantic_sha256": source_semantic_sha,
            "raw_artifact_sha256": source_raw_sha,
        },
        source_pairing_mode="same-source-split",
    )
    _validate_split_artifact(train, preregistration, expected_artifact_split="train")
    _validate_split_artifact(
        holdout,
        preregistration,
        expected_artifact_split="sealed_holdout",
    )
    train_path, holdout_path = _publish_pair_directory(
        output,
        train,
        holdout,
        preparation_claim_path,
        preparation_source_path,
    )
    print(
        json.dumps(
            {
                "preregistration_sha256": preregistration.sha256,
                "preparation_claim": {
                    "path": str(preparation_claim_path),
                    "raw_artifact_sha256": _sha256(preparation_claim_path),
                },
                "preparation_source_binding": {
                    "path": str(preparation_source_path),
                    "raw_artifact_sha256": _sha256(preparation_source_path),
                },
                "source_raw_artifact_sha256": source_raw_sha,
                "source_semantic_sha256": source_semantic_sha,
                "train": {
                    "path": str(train_path),
                    "raw_artifact_sha256": _sha256(train_path),
                    "semantic_sha256": _teacher_semantic_sha256(train),
                },
                "sealed_holdout": {
                    "path": str(holdout_path),
                    "raw_artifact_sha256": _sha256(holdout_path),
                    "semantic_sha256": _teacher_semantic_sha256(holdout),
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


def _indices(group: str) -> tuple[int, ...]:
    try:
        return FEATURE_GROUPS[group]
    except KeyError as error:
        raise ValueError(f"unknown feature group: {group}") from error


def _selected_features(option: TeacherOption, group: str) -> tuple[int, ...]:
    return tuple(option.features[index] for index in _indices(group))


def _pairwise_rows(
    labels: Sequence[TeacherLabel],
    group: str,
    adverse_pair_weight: float = DEFAULT_ADVERSE_PAIR_WEIGHT,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    adverse_pair_weight = _validate_adverse_pair_weight(adverse_pair_weight)
    rows: list[tuple[int, ...]] = []
    outcomes: list[float] = []
    weights: list[float] = []
    for label in labels:
        rankable = tuple(
            option for option in label.options if option.outcome is None
        )
        comparisons = tuple(
            (rankable[left], rankable[right])
            for left in range(len(rankable))
            for right in range(left + 1, len(rankable))
            if rankable[left].score_white != rankable[right].score_white
        )
        if not comparisons:
            continue
        for left, right in comparisons:
            left_features = _selected_features(left, group)
            right_features = _selected_features(right, group)
            delta = left.score_white - right.score_white
            rows.append(
                tuple(
                    left_value - right_value
                    for left_value, right_value in zip(
                        left_features, right_features, strict=True
                    )
                )
            )
            outcomes.append(1.0 if delta > 0 else -1.0)
            pair_weight = min(1.0, abs(delta) / 1000.0) / len(comparisons)
            if _option_is_mover_adverse(label, left) != _option_is_mover_adverse(
                label, right
            ):
                pair_weight *= adverse_pair_weight
            weights.append(pair_weight)
    if not rows:
        raise ValueError("teacher train split has no nonterminal ranking pairs")
    return (
        np.asarray(rows, dtype=np.float64),
        np.asarray(outcomes, dtype=np.float64),
        np.asarray(weights, dtype=np.float64),
    )


def _fit_pairwise(
    labels: Sequence[TeacherLabel],
    group: str,
    ridge: float,
    adverse_pair_weight: float = DEFAULT_ADVERSE_PAIR_WEIGHT,
) -> tuple[np.ndarray, dict[str, Any]]:
    adverse_pair_weight = _validate_adverse_pair_weight(adverse_pair_weight)
    rows, outcomes, sample_weights = _pairwise_rows(
        labels, group, adverse_pair_weight
    )
    deviations = np.sqrt(np.average(rows * rows, axis=0, weights=sample_weights))
    deviations[deviations < 1e-9] = 1.0
    matrix = rows / deviations
    parameters = np.zeros(matrix.shape[1], dtype=np.float64)
    total_weight = float(sample_weights.sum())
    iterations = 0
    for iteration in range(1, 81):
        margins = np.clip(outcomes * (matrix @ parameters), -30.0, 30.0)
        error_probability = 1.0 / (1.0 + np.exp(margins))
        gradient = -(
            matrix.T @ (sample_weights * outcomes * error_probability)
        ) / total_weight
        gradient += ridge * parameters
        curvature = (
            sample_weights
            * error_probability
            * (1.0 - error_probability)
            / total_weight
        )
        hessian = matrix.T @ (matrix * curvature[:, None])
        hessian += np.eye(matrix.shape[1]) * ridge
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(hessian, gradient, rcond=None)[0]
        before = float(
            np.average(np.logaddexp(0.0, -margins), weights=sample_weights)
            + ridge * np.dot(parameters, parameters) / 2.0
        )
        rate = 1.0
        accepted = None
        while rate >= 1e-6:
            proposal = parameters - rate * step
            proposal_margins = np.clip(
                outcomes * (matrix @ proposal), -30.0, 30.0
            )
            after = float(
                np.average(
                    np.logaddexp(0.0, -proposal_margins),
                    weights=sample_weights,
                )
                + ridge * np.dot(proposal, proposal) / 2.0
            )
            if after <= before + 1e-12:
                accepted = proposal
                break
            rate /= 2.0
        if accepted is None:
            break
        parameters = accepted
        iterations = iteration
        if float(np.max(np.abs(rate * step))) < 1e-8:
            break
    raw = parameters / deviations
    return raw, {
        "group": group,
        "ridge": ridge,
        "pairs": len(outcomes),
        "iterations": iterations,
        "adverse_pair_weight": adverse_pair_weight,
    }


def _terminal_score(label: TeacherLabel, option: TeacherOption) -> int | None:
    if option.outcome == "checkmate":
        winner_sign = label.mover_sign if option.ended_by_check else -label.mover_sign
        distance = abs(option.signed_mate_distance or 1)
        return winner_sign * (MATE_SCORE - min(999, distance))
    if option.outcome in {"stalemate", "ten-series-draw"}:
        return 0
    return None


def _linear_scorer(
    coefficients: Sequence[int | float],
    group: str,
) -> Callable[[TeacherLabel, TeacherOption], float]:
    def score(label: TeacherLabel, option: TeacherOption) -> float:
        terminal = _terminal_score(label, option)
        if terminal is not None:
            return float(terminal * FIXED_POINT_SCALE)
        return float(
            sum(
                coefficient * value
                for coefficient, value in zip(
                    coefficients,
                    _selected_features(option, group),
                    strict=True,
                )
            )
        )

    return score


def _profile_scorer(
    weights: Sequence[int],
) -> Callable[[TeacherLabel, TeacherOption], float]:
    def score(label: TeacherLabel, option: TeacherOption) -> float:
        terminal = _terminal_score(label, option)
        if terminal is not None:
            return float(terminal)
        return float(
            sum(
                round(value * weight / 100)
                for value, weight in zip(
                    option.base_features, weights, strict=True
                )
            )
        )

    return score


def _metric_rows(
    labels: Sequence[TeacherLabel],
    scorer: Callable[[TeacherLabel, TeacherOption], float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label in labels:
        predictions = [scorer(label, option) for option in label.options]
        predicted_utilities = [label.mover_sign * value for value in predictions]
        chosen_index = max(
            range(len(predicted_utilities)),
            key=lambda index: (predicted_utilities[index], -index),
        )
        teacher_utilities = [
            label.mover_sign * option.score_white for option in label.options
        ]
        teacher_best = max(teacher_utilities)
        best_indices = {
            index
            for index, value in enumerate(teacher_utilities)
            if value == teacher_best
        }
        chosen = label.options[chosen_index]
        regret = min(
            5000.0,
            max(0.0, teacher_best - teacher_utilities[chosen_index]),
        )
        pair_correct = 0.0
        pair_weight = 0.0
        for left in range(len(label.options)):
            for right in range(left + 1, len(label.options)):
                teacher_delta = (
                    label.options[left].score_white
                    - label.options[right].score_white
                )
                if teacher_delta == 0:
                    continue
                predicted_delta = predictions[left] - predictions[right]
                gap_weight = min(1.0, abs(teacher_delta) / 1000.0)
                pair_weight += gap_weight
                if teacher_delta * predicted_delta > 0:
                    pair_correct += gap_weight
                elif predicted_delta == 0:
                    pair_correct += gap_weight / 2.0
        chosen_proven_adverse = _option_is_mover_adverse(label, chosen)
        chosen_avoidable_proven_adverse = bool(
            chosen_proven_adverse
            and any(
                not _option_is_mover_adverse(label, option)
                for option in label.options
            )
        )
        rows.append(
            {
                "state_key": label.state_key,
                "mover": "white" if label.mover_sign == 1 else "black",
                "series_number": label.series_number,
                "source_profile_id": label.source_profile_id,
                "teacher_tier": label.teacher_tier,
                "teacher_depth_series": label.teacher_depth_series,
                "tactical": label.tactical,
                "agreement": chosen_index in best_indices,
                "normalized_regret": regret / 5000.0,
                "pair_correct": pair_correct,
                "pair_weight": pair_weight,
                "chosen_series": chosen.series,
                "teacher_best_series": label.teacher_best_series,
                "chosen_proven_adverse": chosen_proven_adverse,
                "chosen_avoidable_proven_adverse": (
                    chosen_avoidable_proven_adverse
                ),
            }
        )
    return rows


def _summarize_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "labels": 0,
            "normalized_regret": None,
            "agreement": None,
            "gap_weighted_pairwise_accuracy": None,
            "chosen_proven_adverse": 0,
            "chosen_avoidable_proven_adverse": 0,
        }
    pair_weight = sum(float(row["pair_weight"]) for row in rows)
    return {
        "labels": len(rows),
        "normalized_regret": sum(float(row["normalized_regret"]) for row in rows) / len(rows),
        "agreement": sum(bool(row["agreement"]) for row in rows) / len(rows),
        "gap_weighted_pairwise_accuracy": (
            sum(float(row["pair_correct"]) for row in rows) / pair_weight
            if pair_weight
            else 1.0
        ),
        "chosen_proven_adverse": sum(bool(row["chosen_proven_adverse"]) for row in rows),
        "chosen_avoidable_proven_adverse": sum(
            bool(row["chosen_avoidable_proven_adverse"]) for row in rows
        ),
    }


def _metrics(
    labels: Sequence[TeacherLabel],
    scorer: Callable[[TeacherLabel, TeacherOption], float],
    *,
    include_rows: bool = False,
) -> dict[str, Any]:
    rows = _metric_rows(labels, scorer)
    strata: dict[str, dict[str, Any]] = {}
    partitions: dict[str, Callable[[Mapping[str, Any]], str]] = {
        "mover": lambda row: str(row["mover"]),
        "series": lambda row: str(row["series_number"]),
        "class": lambda row: "tactical" if row["tactical"] else "quiet",
        "teacher_tier": lambda row: str(row["teacher_tier"]),
        "teacher_depth": lambda row: str(row["teacher_depth_series"]),
        "source_profile": lambda row: str(row["source_profile_id"]),
    }
    for partition, classifier in partitions.items():
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            buckets.setdefault(classifier(row), []).append(row)
        strata[partition] = {
            name: _summarize_rows(bucket)
            for name, bucket in sorted(buckets.items())
        }
    result = {
        "overall": _summarize_rows(rows),
        "strata": strata,
    }
    if include_rows:
        result["rows"] = rows
    return result


def _label_semantic_keys(label: TeacherLabel) -> frozenset[str]:
    return frozenset(
        (label.state_key, *(option.final_state_key for option in label.options))
    )


def _folds(
    labels: Sequence[TeacherLabel], count: int = 5
) -> tuple[tuple[TeacherLabel, ...], ...]:
    # Root-disjoint folds are insufficient when two teacher roots transpose to
    # the same retained option state.  Union every label connected by either a
    # root or option-final semantic key, then keep the entire component in one
    # fold.
    parent = list(range(len(labels)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    owner_by_key: dict[str, int] = {}
    for index, label in enumerate(labels):
        for key in _label_semantic_keys(label):
            owner = owner_by_key.setdefault(key, index)
            union(index, owner)
    components: dict[int, list[TeacherLabel]] = {}
    for index, label in enumerate(labels):
        components.setdefault(find(index), []).append(label)
    grouped = list(components.values())
    if len(grouped) < 2:
        raise ValueError(
            "teacher train split has fewer than two semantic-state components"
        )

    def component_key(component: Sequence[TeacherLabel]) -> tuple[Any, ...]:
        keys = sorted(
            key for label in component for key in _label_semantic_keys(label)
        )
        digest = hashlib.sha256(
            ("deep-teacher-cv-component-v1|" + "|".join(keys)).encode()
        ).digest()
        return (-len(component), digest, tuple(label.state_key for label in component))

    buckets: list[list[TeacherLabel]] = [
        [] for _ in range(min(count, len(grouped)))
    ]
    for component in sorted(grouped, key=component_key):
        bucket_index = min(
            range(len(buckets)), key=lambda index: (len(buckets[index]), index)
        )
        buckets[bucket_index].extend(component)
    return tuple(
        tuple(sorted(bucket, key=lambda label: label.state_key))
        for bucket in buckets
    )


def _cross_validate(
    labels: Sequence[TeacherLabel],
    group: str,
    ridge: float,
    adverse_pair_weight: float = DEFAULT_ADVERSE_PAIR_WEIGHT,
) -> dict[str, Any]:
    adverse_pair_weight = _validate_adverse_pair_weight(adverse_pair_weight)
    folds = _folds(labels)
    all_rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    for index, validation in enumerate(folds):
        validation_keys = {label.state_key for label in validation}
        training = tuple(
            label for label in labels if label.state_key not in validation_keys
        )
        validation_semantic_keys = {
            key for label in validation for key in _label_semantic_keys(label)
        }
        training_semantic_keys = {
            key for label in training for key in _label_semantic_keys(label)
        }
        semantic_overlap = validation_semantic_keys & training_semantic_keys
        if semantic_overlap:
            raise ValueError(
                f"cross-validation semantic-state leakage: {len(semantic_overlap)}"
            )
        coefficients, fit = _fit_pairwise(
            training, group, ridge, adverse_pair_weight
        )
        rows = _metric_rows(validation, _linear_scorer(coefficients, group))
        all_rows.extend(rows)
        fold_rows.append(
            {
                "fold": index,
                "train_labels": len(training),
                "validation_labels": len(validation),
                "train_semantic_keys": len(training_semantic_keys),
                "validation_semantic_keys": len(validation_semantic_keys),
                "semantic_key_overlap": 0,
                "fit": fit,
                "metrics": _summarize_rows(rows),
            }
        )
    return {
        "group": group,
        "ridge": ridge,
        "adverse_pair_weight": adverse_pair_weight,
        "out_of_fold": _summarize_rows(all_rows),
        "folds": fold_rows,
    }


def _metric_objective(metrics: Mapping[str, Any]) -> tuple[int, float, float, float]:
    return (
        int(metrics["chosen_avoidable_proven_adverse"]),
        float(metrics["normalized_regret"]),
        -float(metrics["gap_weighted_pairwise_accuracy"]),
        -float(metrics["agreement"]),
    )


def _selection_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        *_metric_objective(row["out_of_fold"]),
        len(_indices(str(row["group"]))),
        row["ridge"],
    )


def _quantize(coefficients: np.ndarray) -> tuple[int, ...]:
    maximum = float(np.max(np.abs(coefficients)))
    if maximum <= 0.0 or not math.isfinite(maximum):
        raise ValueError("fitted coefficient vector is empty or nonfinite")
    normalized = coefficients / maximum
    return tuple(int(math.copysign(math.floor(abs(value) * FIXED_POINT_SCALE + 0.5), value)) for value in normalized)


def _model_payload(
    *,
    group: str,
    ridge: float,
    coefficients: Sequence[int],
    adverse_pair_weight: float,
    corpus_id: str,
    corpus_semantic_sha256: str,
    corpus_raw_artifact_sha256: str,
) -> dict[str, Any]:
    feature_names = [
        TEACHER_VALUE_FEATURE_NAMES[index] for index in _indices(group)
    ]
    core = {
        "schema": MODEL_SCHEMA,
        "feature_schema": TEACHER_VALUE_FEATURE_SCHEMA,
        "feature_group": group,
        "feature_names": feature_names,
        "fixed_point_scale": FIXED_POINT_SCALE,
        "coefficients": list(coefficients),
        "ridge": ridge,
        "adverse_pair_weight": _validate_adverse_pair_weight(
            adverse_pair_weight
        ),
        "terminal_override": "replayed terminal checkmate and draw outcomes are authoritative",
        "teacher_corpus_id": corpus_id,
        "teacher_corpus_sha256": corpus_semantic_sha256,
        "teacher_corpus_semantic_sha256": corpus_semantic_sha256,
    }
    return {
        **core,
        "teacher_corpus_raw_artifact_sha256": corpus_raw_artifact_sha256,
        "model_id": "spc-dtv-" + hashlib.sha256(_canonical_json(core)).hexdigest()[:20],
    }


def _validate_model_payload(model: dict[str, Any], path: Path) -> dict[str, Any]:
    supplied = str(model.get("model_id", ""))
    core = {
        key: value
        for key, value in model.items()
        if key not in {"model_id", "teacher_corpus_raw_artifact_sha256"}
    }
    expected = "spc-dtv-" + hashlib.sha256(_canonical_json(core)).hexdigest()[:20]
    if supplied != expected:
        raise ValueError(f"model_id mismatch: {path}")
    if model.get("schema") != MODEL_SCHEMA:
        raise ValueError(f"unsupported model schema: {path}")
    try:
        _validate_adverse_pair_weight(float(model["adverse_pair_weight"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"model adverse pair weight is invalid: {path}") from error
    group = str(model["feature_group"])
    expected_names = [
        TEACHER_VALUE_FEATURE_NAMES[index] for index in _indices(group)
    ]
    if model["feature_names"] != expected_names:
        raise ValueError(f"model feature order mismatch: {path}")
    return model


def _read_model_artifact(path: Path) -> tuple[dict[str, Any], str]:
    model, raw_sha256 = _read_json_artifact(path)
    return _validate_model_payload(model, path), raw_sha256


def _load_model(path: Path) -> dict[str, Any]:
    return _read_model_artifact(path)[0]


def _coordinate_profile(
    labels: Sequence[TeacherLabel],
    starts: Sequence[Sequence[int]],
) -> tuple[int, ...]:
    def objective(weights: Sequence[int]) -> tuple[int, float, float, float]:
        metrics = _metrics(labels, _profile_scorer(weights))["overall"]
        return _metric_objective(metrics)

    best: tuple[int, ...] | None = None
    best_value: tuple[int, float, float, float] | None = None
    for requested in starts:
        current = tuple(max(25, min(300, int(value))) for value in requested)
        current_value = objective(current)
        for step in (50, 25, 12, 6, 3, 1):
            changed = True
            while changed:
                changed = False
                for index in range(len(current)):
                    proposals: list[
                        tuple[tuple[int, float, float, float], tuple[int, ...]]
                    ] = []
                    for direction in (-1, 1):
                        proposal = list(current)
                        proposal[index] = max(
                            25,
                            min(300, proposal[index] + direction * step),
                        )
                        candidate = tuple(proposal)
                        proposals.append((objective(candidate), candidate))
                    proposal_value, proposal = min(proposals)
                    if proposal_value < current_value:
                        current_value, current = proposal_value, proposal
                        changed = True
        if best_value is None or (current_value, current) < (best_value, best or current):
            best_value, best = current_value, current
    assert best is not None
    return best


def _profile_payload(
    weights: Sequence[int],
    leader: EngineProfile,
) -> dict[str, Any]:
    profile = EngineProfile(
        name="one-shot deep-teacher distilled profile",
        weights=EvaluationWeights(
            **dict(zip(FEATURE_NAMES, (int(value) for value in weights), strict=True))
        ),
        recommended_depth=2,
        recommended_branch_cap=32,
        generation=max(3, leader.generation + 1),
        parent_profile_ids=(leader.profile_id,),
        notes=(
            "Train-only deep-teacher ranking distillation; independent holdout "
            "and paired match gates are mandatory before promotion."
        ),
    )
    return profile.as_dict()


def _fit_command(args: argparse.Namespace) -> None:
    preregistration = _load_preregistration(
        args.preregistration, require_pair_completion=True
    )
    pair_completion, pair_completion_raw_sha = _trusted_pair_completion(
        preregistration
    )
    sealed = _require_exact_fields(
        pair_completion["sealed_holdout"],
        {"path", "raw_artifact_sha256", "file_identity"},
        "fit central sealed holdout",
    )
    sealed_path = Path(str(sealed["path"]))
    sealed_identity = _validated_file_identity(
        sealed["file_identity"], "fit central sealed holdout"
    )
    with _sealed_alias_guard(sealed_path, sealed_identity):
        _fit_command_with_bound_pair(
            args,
            preregistration,
            pair_completion,
            pair_completion_raw_sha,
        )


def _fit_command_with_bound_pair(
    args: argparse.Namespace,
    preregistration: Preregistration,
    pair_completion: Mapping[str, Any],
    pair_completion_raw_sha: str,
) -> None:
    _require_lexical_absolute_input(args.teacher_corpus, "fit train artifact")
    _require_lexical_absolute_input(
        args.leader_profile, "fit rejected leader profile"
    )
    leader_path = args.leader_profile
    leader_binding = _require_exact_fields(
        preregistration.manifest["profiles"]["rejected_development_leader"],
        {"path", "profile_id", "sha256"},
        "rejected development leader",
    )
    expected_leader_path = _repository_file(str(leader_binding["path"]))
    if leader_path != expected_leader_path:
        raise ValueError(
            "fit leader path differs from the preregistered rejected development leader"
        )
    adverse_pair_weight = _validate_adverse_pair_weight(
        getattr(args, "adverse_pair_weight", DEFAULT_ADVERSE_PAIR_WEIGHT)
    )
    if adverse_pair_weight != DEFAULT_ADVERSE_PAIR_WEIGHT:
        raise ValueError("fit adverse-pair weight differs from preregistration")
    expected_train_path = Path(str(pair_completion["train"]["path"]))
    sealed_holdout_path = Path(str(pair_completion["sealed_holdout"]["path"]))
    train_file_identity = _validated_file_identity(
        pair_completion["train"]["file_identity"], "fit central train"
    )
    sealed_file_identity = _validated_file_identity(
        pair_completion["sealed_holdout"]["file_identity"],
        "fit central sealed holdout",
    )
    if args.teacher_corpus != expected_train_path:
        raise ValueError(
            "fit train path is not the centrally completed pair train member"
    )
    corpus_path = args.teacher_corpus
    output = _canonical_cli_input(args.output, "fit output")
    _require_protocol_registry_isolation(
        preregistration,
        {
            "train artifact": corpus_path,
            "leader profile": leader_path,
            "output": output,
        },
        label="fit",
    )
    receipt_path = output / "deep-teacher-fit-receipt.json"
    holdout_claim_path = _holdout_claim_path(preregistration)
    if output.exists() and not output.is_dir():
        raise FileExistsError("fit output exists and is not a directory")
    fit_script_path = Path(__file__).resolve()
    feature_module_path = Path(
        sys.modules[TeacherValueFeaturesV3.__module__].__file__
    ).resolve()
    initial_script_sha256 = _sha256(fit_script_path)
    initial_implementation_sha256 = _implementation_hashes()
    initial_feature_module_sha256 = _sha256(feature_module_path)
    corpus, corpus_raw_sha = _read_identity_isolated_json_artifact(
        corpus_path,
        forbidden_path=sealed_holdout_path,
        label="fit train artifact",
        expected_file_identity=train_file_identity,
        forbidden_file_identity=sealed_file_identity,
    )
    if corpus_raw_sha != pair_completion["train"]["raw_artifact_sha256"]:
        raise ValueError("fit train bytes differ from central pair completion")
    _reject_quarantined_holdout(corpus)
    split_integrity = _validate_split_artifact(
        corpus,
        preregistration,
        expected_artifact_split="train",
        reopen_external_lineage=False,
    )
    pair_publication = _validate_pair_publication(
        corpus_path,
        corpus_raw_sha,
        split_integrity,
        preregistration,
        forbidden_path=sealed_holdout_path,
        forbidden_file_identity=sealed_file_identity,
    )
    if (
        pair_publication["raw_artifact_sha256"]
        != pair_completion["local_publication"]["raw_artifact_sha256"]
        or pair_publication["train_raw_artifact_sha256"]
        != pair_completion["train"]["raw_artifact_sha256"]
        or pair_publication["sealed_holdout_raw_artifact_sha256"]
        != pair_completion["sealed_holdout"]["raw_artifact_sha256"]
    ):
        raise ValueError("fit pair publication differs from central completion")
    corpus_semantic_sha = _teacher_semantic_sha256(corpus)
    if split_integrity["artifact_semantic_sha256"] != corpus_semantic_sha:
        raise AssertionError("validated train semantic digest changed")
    corpus_id = str(corpus["corpus_id"])
    train, leakage = _materialize_labels(corpus, selected_split="train")
    if not train:
        raise ValueError("teacher corpus has no train labels")
    leader_payload, leader_raw_sha = _read_identity_isolated_json_artifact(
        leader_path,
        forbidden_path=sealed_holdout_path,
        label="fit rejected leader profile",
        forbidden_file_identity=sealed_file_identity,
    )
    raw_leader_profile = leader_payload.get("profile", leader_payload)
    if not isinstance(raw_leader_profile, Mapping):
        raise ValueError("fit rejected leader profile envelope is malformed")
    leader = EngineProfile.from_dict(raw_leader_profile)
    if (
        leader.profile_id != leader_binding["profile_id"]
        or leader_raw_sha != leader_binding["sha256"]
    ):
        raise ValueError("fit leader identity differs from preregistration")

    cv_rows = [
        _cross_validate(train, group, ridge, adverse_pair_weight)
        for group in (*NONROUTE_GROUPS, "all47")
        for ridge in RIDGES
    ]
    nonroute_selection = min(
        (row for row in cv_rows if row["group"] in NONROUTE_GROUPS),
        key=_selection_key,
    )
    route_selection = min(
        (row for row in cv_rows if row["group"] == "all47"),
        key=_selection_key,
    )
    models: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for role, selection in (
        ("primary_nonroute", nonroute_selection),
        ("route_ablation", route_selection),
    ):
        raw, fit = _fit_pairwise(
            train,
            str(selection["group"]),
            float(selection["ridge"]),
            adverse_pair_weight,
        )
        quantized = _quantize(raw)
        model = _model_payload(
            group=str(selection["group"]),
            ridge=float(selection["ridge"]),
            coefficients=quantized,
            adverse_pair_weight=adverse_pair_weight,
            corpus_id=corpus_id,
            corpus_semantic_sha256=corpus_semantic_sha,
            corpus_raw_artifact_sha256=corpus_raw_sha,
        )
        model_path = output / f"{role}-{model['model_id']}.json"
        model_raw_sha256 = hashlib.sha256(_pretty_json_bytes(model)).hexdigest()
        metrics = _metrics(
            train,
            _linear_scorer(quantized, str(selection["group"])),
        )
        models.append(
            (
                role,
                model,
                {
                    "path": str(model_path),
                    "sha256": model_raw_sha256,
                    "fit": fit,
                    "cross_validation": selection,
                    "train_metrics": metrics,
                },
            )
        )

    # The matchable seven-weight surface is distilled separately.  The richer
    # model never silently changes the native/browser evaluator.
    projected_raw, _ = _fit_pairwise(
        train, "base7", 0.01, adverse_pair_weight
    )
    median = float(np.median(np.abs(projected_raw)))
    projected = tuple(
        max(25, min(300, round(abs(value) * 100.0 / max(1e-9, median))))
        for value in projected_raw
    )
    profile_weights = _coordinate_profile(
        train,
        (
            BASELINE_WEIGHTS,
            tuple(int(getattr(leader.weights, name)) for name in FEATURE_NAMES),
            DEVELOPMENT_PROFILE_WEIGHTS,
            projected,
        ),
    )
    profile = _profile_payload(profile_weights, leader)
    profile_path = output / f"teacher-distilled-{profile['profile_id']}.json"
    profile_raw_sha256 = hashlib.sha256(_pretty_json_bytes(profile)).hexdigest()

    receipt = {
        "schema": FIT_RECEIPT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "claim_scope": (
            "train-only isolated evaluation experiment; holdout labels are not "
            "scored or used for selection; no live evaluator or release changed"
        ),
        "one_shot_holdout": {
            "schema": HOLDOUT_CLAIM_BINDING_SCHEMA,
            "claim_path": str(holdout_claim_path),
        },
        "inputs": {
            "preregistration": str(preregistration.path),
            "preregistration_schema": preregistration.schema,
            "preregistration_sha256": preregistration.sha256,
            "artifact_split": "train",
            "train_artifact": str(corpus_path),
            "train_artifact_raw_sha256": corpus_raw_sha,
            "train_artifact_semantic_sha256": corpus_semantic_sha,
            "teacher_corpus": str(corpus_path),
            "teacher_corpus_id": corpus_id,
            "teacher_corpus_sha256": corpus_semantic_sha,
            "teacher_corpus_raw_artifact_sha256": corpus_raw_sha,
            "leader_profile": str(leader_path),
            "leader_profile_id": leader.profile_id,
            "leader_profile_sha256": leader_raw_sha,
        },
        "split_integrity": {
            "schema": SPLIT_INTEGRITY_SCHEMA,
            **split_integrity,
        },
        "pair_publication": pair_publication,
        "pair_completion": {
            "path": str(_pair_completion_registry_path(preregistration)),
            "raw_artifact_sha256": pair_completion_raw_sha,
        },
        "leakage_audit": leakage,
        "feature_contract": {
            "schema": TEACHER_VALUE_FEATURE_SCHEMA,
            "feature_names": list(TEACHER_VALUE_FEATURE_NAMES),
            "feature_module": str(feature_module_path),
            "feature_module_sha256": initial_feature_module_sha256,
            "expensive_two_move_route_indices": [44, 45, 46],
            "primary_candidate_excludes_expensive_routes": True,
        },
        "selection": {
            "method": (
                "five-fold semantic-component-disjoint all-nonterminal-pairs "
                "proof-contrast-weighted ridge ranking"
            ),
            "ridges": list(RIDGES),
            "adverse_pair_weight": adverse_pair_weight,
            "adverse_pair_rule": (
                "multiply a pair's deterministic gap weight when exactly one "
                "option has a proof for the mover's opponent"
            ),
            "primary_objective": (
                "raw avoidable proven-adverse selections, normalized regret, "
                "pairwise accuracy, agreement"
            ),
            "raw_option_argmax_metrics": True,
            "rows": cv_rows,
        },
        "models": {
            role: {
                "model_id": model["model_id"],
                **evidence,
            }
            for role, model, evidence in models
        },
        "profile": {
            "profile_id": profile["profile_id"],
            "weights": list(profile_weights),
            "path": str(profile_path),
            "sha256": profile_raw_sha256,
            "train_metrics": _metrics(train, _profile_scorer(profile_weights)),
        },
        "references_train": {
            "baseline": _metrics(train, _profile_scorer(BASELINE_WEIGHTS)),
            "rejected_leader": _metrics(
                train,
                _profile_scorer(
                    tuple(int(getattr(leader.weights, name)) for name in FEATURE_NAMES)
                ),
            ),
        },
        "runtime": {
            "python": sys.version,
            "numpy": np.__version__,
            "argv": list(sys.argv),
            "script_sha256": initial_script_sha256,
            "implementation_sha256": initial_implementation_sha256,
        },
    }

    # The fit may run for a long time. Reopen every input and every executable
    # source commitment after all selection/model work, before publishing even
    # the first candidate artifact. This prevents a model built from one byte
    # snapshot from being attested with a later train/pair/leader/runtime state.
    final_pair_publication = _validate_pair_publication(
        corpus_path,
        corpus_raw_sha,
        split_integrity,
        preregistration,
        forbidden_path=sealed_holdout_path,
        forbidden_file_identity=sealed_file_identity,
    )
    final_corpus, final_corpus_raw_sha = _read_identity_isolated_json_artifact(
        corpus_path,
        forbidden_path=sealed_holdout_path,
        label="fit train artifact",
        expected_file_identity=train_file_identity,
        forbidden_file_identity=sealed_file_identity,
    )
    final_leader_payload, final_leader_raw_sha = (
        _read_identity_isolated_json_artifact(
            leader_path,
            forbidden_path=sealed_holdout_path,
            label="fit rejected leader profile",
            forbidden_file_identity=sealed_file_identity,
        )
    )
    final_raw_leader = final_leader_payload.get("profile", final_leader_payload)
    if not isinstance(final_raw_leader, Mapping):
        raise ValueError("fit rejected leader profile envelope changed")
    final_leader = EngineProfile.from_dict(final_raw_leader)
    final_pair_completion, final_pair_completion_raw_sha = (
        _trusted_pair_completion(preregistration)
    )
    final_implementation_sha256 = _implementation_hashes()
    final_script_sha256 = _sha256(fit_script_path)
    final_feature_module_sha256 = _sha256(feature_module_path)
    if (
        final_pair_publication != pair_publication
        or final_pair_completion != pair_completion
        or final_pair_completion_raw_sha != pair_completion_raw_sha
        or final_corpus != corpus
        or final_corpus_raw_sha != corpus_raw_sha
        or final_leader != leader
        or final_leader_raw_sha != leader_raw_sha
        or final_implementation_sha256 != initial_implementation_sha256
        or final_script_sha256 != initial_script_sha256
        or final_feature_module_sha256 != initial_feature_module_sha256
    ):
        raise ValueError(
            "fit inputs, pair publication, or executable sources changed during fitting"
        )

    artifact_plan: list[tuple[Path, Mapping[str, Any], str]] = [
        (
            output / f"{role}-{model['model_id']}.json",
            model,
            f"frozen model already exists: {role}",
        )
        for role, model, _evidence in models
    ]
    artifact_plan.append(
        (profile_path, profile, "frozen distilled profile already exists")
    )
    ordered_paths = [path for path, _payload, _message in artifact_plan]
    ordered_paths.append(receipt_path)
    if output.exists():
        unexpected = sorted(
            str(entry)
            for entry in output.iterdir()
            if entry not in set(ordered_paths)
        )
        if unexpected:
            raise FileExistsError(
                "fit output contains unexpected artifacts: " + ", ".join(unexpected)
            )
    present = [path.exists() for path in ordered_paths]
    first_missing = next(
        (index for index, exists in enumerate(present) if not exists),
        len(present),
    )
    if any(present[first_missing + 1 :]):
        raise FileExistsError(
            "fit output is not a contiguous atomic publication prefix"
        )
    for index, (path, expected, _message) in enumerate(artifact_plan):
        if not present[index]:
            break
        existing, existing_raw_sha = _read_identity_isolated_json_artifact(
            path,
            forbidden_path=sealed_holdout_path,
            label=f"existing fit artifact {path.name}",
            forbidden_file_identity=sealed_file_identity,
        )
        expected_raw_sha = hashlib.sha256(_pretty_json_bytes(expected)).hexdigest()
        if existing != expected or existing_raw_sha != expected_raw_sha:
            raise FileExistsError(f"existing fit artifact differs: {path.name}")
    if present[-1]:
        existing_receipt, _ = _read_identity_isolated_json_artifact(
            receipt_path,
            forbidden_path=sealed_holdout_path,
            label="existing fit receipt",
            forbidden_file_identity=sealed_file_identity,
        )
        existing_created_at = existing_receipt.get("created_at")
        try:
            parsed_created_at = datetime.fromisoformat(str(existing_created_at))
        except (TypeError, ValueError) as error:
            raise FileExistsError("existing fit receipt timestamp is malformed") from error
        if parsed_created_at.tzinfo is None:
            raise FileExistsError("existing fit receipt timestamp is malformed")
        receipt["created_at"] = existing_created_at
        if existing_receipt != receipt:
            raise FileExistsError("existing fit receipt differs")

    for index, (path, payload, conflict_message) in enumerate(artifact_plan):
        if not present[index]:
            _atomic_exclusive_json(
                path,
                payload,
                conflict_message=conflict_message,
            )

    closing_preregistration = _load_preregistration(
        args.preregistration, require_pair_completion=True
    )
    closing_pair_completion, closing_pair_completion_raw = (
        _trusted_pair_completion(closing_preregistration)
    )
    closing_pair_publication = _validate_pair_publication(
        corpus_path,
        corpus_raw_sha,
        split_integrity,
        closing_preregistration,
        forbidden_path=sealed_holdout_path,
        forbidden_file_identity=sealed_file_identity,
    )
    closing_corpus, closing_corpus_raw = _read_identity_isolated_json_artifact(
        corpus_path,
        forbidden_path=sealed_holdout_path,
        label="fit train artifact",
        expected_file_identity=train_file_identity,
        forbidden_file_identity=sealed_file_identity,
    )
    closing_leader_payload, closing_leader_raw = (
        _read_identity_isolated_json_artifact(
            leader_path,
            forbidden_path=sealed_holdout_path,
            label="fit rejected leader profile",
            forbidden_file_identity=sealed_file_identity,
        )
    )
    closing_raw_leader = closing_leader_payload.get(
        "profile", closing_leader_payload
    )
    if not isinstance(closing_raw_leader, Mapping):
        raise ValueError("fit rejected leader profile envelope changed")
    closing_leader = EngineProfile.from_dict(closing_raw_leader)
    if (
        closing_preregistration != preregistration
        or closing_pair_completion != pair_completion
        or closing_pair_completion_raw != pair_completion_raw_sha
        or closing_pair_publication != pair_publication
        or closing_corpus != corpus
        or closing_corpus_raw != corpus_raw_sha
        or closing_leader != leader
        or closing_leader_raw != leader_raw_sha
        or _implementation_hashes() != initial_implementation_sha256
        or _sha256(fit_script_path) != initial_script_sha256
        or _sha256(feature_module_path) != initial_feature_module_sha256
    ):
        raise ValueError(
            "fit lineage or executable sources changed before receipt publication"
        )
    for role, model, evidence in models:
        final_model, final_model_raw_sha = _read_identity_isolated_json_artifact(
            output / f"{role}-{model['model_id']}.json",
            forbidden_path=sealed_holdout_path,
            label=f"published frozen model {role}",
            forbidden_file_identity=sealed_file_identity,
        )
        if final_model != model or final_model_raw_sha != evidence["sha256"]:
            raise ValueError(f"published frozen model differs: {role}")
    final_profile, final_profile_raw_sha = _read_identity_isolated_json_artifact(
        profile_path,
        forbidden_path=sealed_holdout_path,
        label="published distilled profile",
        forbidden_file_identity=sealed_file_identity,
    )
    if final_profile != profile or final_profile_raw_sha != profile_raw_sha256:
        raise ValueError("published distilled profile differs")
    if not present[-1]:
        _atomic_exclusive_json(
            receipt_path,
            receipt,
            conflict_message="fit receipt already exists",
        )
    final_receipt, _ = _read_identity_isolated_json_artifact(
        receipt_path,
        forbidden_path=sealed_holdout_path,
        label="published fit receipt",
        forbidden_file_identity=sealed_file_identity,
    )
    if final_receipt != receipt:
        raise ValueError("published fit receipt differs")
    for role, model, evidence in models:
        tail_model, tail_model_raw_sha = _read_identity_isolated_json_artifact(
            output / f"{role}-{model['model_id']}.json",
            forbidden_path=sealed_holdout_path,
            label=f"tail frozen model {role}",
            forbidden_file_identity=sealed_file_identity,
        )
        if tail_model != model or tail_model_raw_sha != evidence["sha256"]:
            raise ValueError(f"published frozen model changed after receipt: {role}")
    tail_profile, tail_profile_raw_sha = _read_identity_isolated_json_artifact(
        profile_path,
        forbidden_path=sealed_holdout_path,
        label="tail distilled profile",
        forbidden_file_identity=sealed_file_identity,
    )
    if tail_profile != profile or tail_profile_raw_sha != profile_raw_sha256:
        raise ValueError("published distilled profile changed after receipt")
    print(
        json.dumps(
            {
                "receipt": str(receipt_path),
                "primary_model": receipt["models"]["primary_nonroute"],
                "route_ablation": receipt["models"]["route_ablation"],
                "profile": receipt["profile"],
            },
            indent=2,
            sort_keys=True,
        )
    )


def _holdout_gate(
    candidate: Mapping[str, Any],
    route: Mapping[str, Any],
    profile: Mapping[str, Any],
    baseline: Mapping[str, Any],
    leader: Mapping[str, Any],
) -> dict[str, Any]:
    def stratum_regret_no_worse(
        metrics: Mapping[str, Any], partition: str, stratum: str
    ) -> bool:
        candidate_row = metrics["strata"][partition].get(stratum)
        baseline_row = baseline["strata"][partition].get(stratum)
        return bool(
            candidate_row is not None
            and baseline_row is not None
            and candidate_row["normalized_regret"]
            <= baseline_row["normalized_regret"]
        )

    candidate_overall = candidate["overall"]
    route_overall = route["overall"]
    reference_rows = [baseline["overall"], leader["overall"]]
    best_regret = min(row["normalized_regret"] for row in reference_rows)
    best_pairwise = max(
        row["gap_weighted_pairwise_accuracy"] for row in reference_rows
    )
    candidate_gate = {
        "lower_regret_than_both_references": candidate_overall["normalized_regret"] < best_regret,
        "higher_pairwise_accuracy_than_both_references": candidate_overall["gap_weighted_pairwise_accuracy"] > best_pairwise,
        "no_proven_adverse_selection": candidate_overall["chosen_proven_adverse"] == 0,
        "no_avoidable_proven_adverse_selection": candidate_overall[
            "chosen_avoidable_proven_adverse"
        ]
        == 0,
        "white_regret_no_worse_than_baseline": stratum_regret_no_worse(
            candidate, "mover", "white"
        ),
        "black_regret_no_worse_than_baseline": stratum_regret_no_worse(
            candidate, "mover", "black"
        ),
        "quiet_regret_no_worse_than_baseline": stratum_regret_no_worse(
            candidate, "teacher_tier", "quiet_d2"
        ),
        "tactical_regret_no_worse_than_baseline": stratum_regret_no_worse(
            candidate, "teacher_tier", "tactical_d3"
        ),
    }
    candidate_gate["passed"] = all(candidate_gate.values())
    route_gate = {
        "lower_regret_than_nonroute": route_overall["normalized_regret"] < candidate_overall["normalized_regret"],
        "higher_pairwise_accuracy_than_nonroute": route_overall["gap_weighted_pairwise_accuracy"] > candidate_overall["gap_weighted_pairwise_accuracy"],
        "no_proven_adverse_selection": route_overall["chosen_proven_adverse"] == 0,
        "no_avoidable_proven_adverse_selection": route_overall[
            "chosen_avoidable_proven_adverse"
        ]
        == 0,
    }
    route_gate["passed"] = all(route_gate.values())
    profile_overall = profile["overall"]
    profile_gate = {
        "lower_regret_than_both_references": profile_overall["normalized_regret"] < best_regret,
        "higher_pairwise_accuracy_than_both_references": profile_overall["gap_weighted_pairwise_accuracy"] > best_pairwise,
        "no_proven_adverse_selection": profile_overall["chosen_proven_adverse"] == 0,
        "no_avoidable_proven_adverse_selection": profile_overall[
            "chosen_avoidable_proven_adverse"
        ]
        == 0,
        "white_regret_no_worse_than_baseline": stratum_regret_no_worse(
            profile, "mover", "white"
        ),
        "black_regret_no_worse_than_baseline": stratum_regret_no_worse(
            profile, "mover", "black"
        ),
        "quiet_regret_no_worse_than_baseline": stratum_regret_no_worse(
            profile, "teacher_tier", "quiet_d2"
        ),
        "tactical_regret_no_worse_than_baseline": stratum_regret_no_worse(
            profile, "teacher_tier", "tactical_d3"
        ),
    }
    profile_gate["passed"] = all(profile_gate.values())
    return {
        "primary_nonroute": candidate_gate,
        "route_ablation": route_gate,
        "distilled_profile": profile_gate,
    }


def _evaluate_holdout_command(args: argparse.Namespace) -> None:
    preregistration = _load_preregistration(
        args.preregistration, require_pair_completion=True
    )
    pair_completion, pair_completion_raw_sha = _trusted_pair_completion(
        preregistration
    )
    pair_completion_reference = {
        "path": str(_pair_completion_registry_path(preregistration)),
        "raw_artifact_sha256": pair_completion_raw_sha,
    }
    sealed_holdout_argument = args.teacher_corpus
    _require_lexical_absolute_input(
        sealed_holdout_argument, "sealed holdout artifact"
    )
    sealed_holdout_display = os.fspath(sealed_holdout_argument)
    _require_lexical_absolute_input(args.leader_profile, "rejected leader profile")
    _require_lexical_absolute_input(args.fit_receipt, "fit receipt")
    requested_output = args.output
    _require_lexical_absolute_input(requested_output, "holdout output")
    _require_pairwise_nonoverlapping_paths(
        {
            "sealed holdout artifact": sealed_holdout_argument,
            "leader profile": args.leader_profile,
            "fit receipt": args.fit_receipt,
            "output": requested_output,
        },
        label="evaluate-holdout",
    )
    _require_protocol_registry_isolation(
        preregistration,
        {
            "sealed holdout artifact": sealed_holdout_argument,
            "leader profile": args.leader_profile,
            "fit receipt": args.fit_receipt,
            "output": requested_output,
        },
        label="evaluate-holdout",
    )
    requested_receipt = requested_output / "deep-teacher-holdout-receipt.json"
    holdout_claim_path = _holdout_claim_path(preregistration)

    # A caller-controlled "unsealed" input can be a hardlink to the sealed
    # artifact. Burn the globally shared cycle/seed marker before resolving,
    # stating, opening, hashing, or parsing any such path. The immutable fit
    # receipt later proves the exact model/input bytes used by this attempt.
    frozen_fitter_sha256 = preregistration.manifest["frozen_implementation"].get(
        "scripts/fit_deep_teacher_value.py"
    )
    if (
        not isinstance(frozen_fitter_sha256, str)
        or HEX_SHA256.fullmatch(frozen_fitter_sha256) is None
    ):
        raise ValueError("preregistration frozen fitter hash is malformed")
    holdout_claim = {
        "schema": HOLDOUT_CLAIM_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "preregistration_sha256": preregistration.sha256,
        "preregistration_schema": preregistration.schema,
        "sealed_holdout_seed": preregistration.manifest["trajectory_corpora"][
            "sealed_holdout"
        ]["seed"],
        # The preregistration loader already authenticated this hash while its
        # sealed-file identity guard was active. Do not reopen executable bytes
        # until the irreversible holdout claim has been published.
        "script_sha256": frozen_fitter_sha256,
        "requested_fit_receipt": os.fspath(args.fit_receipt),
        "requested_leader_profile": os.fspath(args.leader_profile),
        "requested_sealed_holdout_artifact": sealed_holdout_display,
        "requested_receipt": str(requested_receipt),
        "pair_completion": pair_completion_reference,
    }
    _exclusive_json(holdout_claim_path, holdout_claim)
    persisted_holdout_claim, holdout_claim_raw_sha = _read_json_artifact(
        holdout_claim_path
    )
    if persisted_holdout_claim != holdout_claim:
        raise ValueError("holdout claim changed immediately after publication")

    leader_path = _canonical_cli_input(
        args.leader_profile, "rejected leader profile"
    )
    fit_receipt_path = _canonical_cli_input(args.fit_receipt, "fit receipt")
    output = requested_output.expanduser().resolve()
    if str(requested_output) != str(output):
        raise ValueError("holdout output path must be canonical; aliases are forbidden")
    _reserve_output_directory(output, "holdout")
    receipt_path = output / "deep-teacher-holdout-receipt.json"

    # Validate the exact unsealed inputs after the irreversible claim.
    fit_receipt, fit_receipt_raw_sha = _read_json_artifact(fit_receipt_path)
    if fit_receipt.get("schema") != FIT_RECEIPT_SCHEMA:
        raise ValueError("fit receipt schema mismatch")
    if fit_receipt.get("pair_completion") != pair_completion_reference:
        raise ValueError("fit receipt central pair-completion binding differs")
    fit_inputs = fit_receipt.get("inputs")
    if not isinstance(fit_inputs, Mapping) or any(
        fit_inputs.get(name) != value
        for name, value in {
            "preregistration_schema": preregistration.schema,
            "preregistration_sha256": preregistration.sha256,
            "artifact_split": "train",
        }.items()
    ):
        raise ValueError("fit receipt preregistration or train-split binding differs")
    initial_script_sha256 = _sha256(Path(__file__).resolve())
    if (
        fit_receipt["runtime"]["script_sha256"] != initial_script_sha256
        or holdout_claim["script_sha256"] != initial_script_sha256
    ):
        raise ValueError("trainer/evaluator script changed after fitting")
    initial_implementation_sha256 = _implementation_hashes()
    if (
        fit_receipt["runtime"].get("implementation_sha256")
        != initial_implementation_sha256
    ):
        raise ValueError("teacher evaluator implementation changed after fitting")
    feature_module = Path(
        sys.modules[TeacherValueFeaturesV3.__module__].__file__
    ).resolve()
    initial_feature_module_sha256 = _sha256(feature_module)
    if (
        fit_receipt["feature_contract"]["feature_module_sha256"]
        != initial_feature_module_sha256
    ):
        raise ValueError("teacher-value feature implementation changed after fitting")
    train_path_value = fit_inputs.get("train_artifact")
    if not isinstance(train_path_value, str):
        raise ValueError("fit receipt frozen train artifact path is missing")
    train_artifact_path = Path(train_path_value)
    if not train_artifact_path.is_absolute():
        raise ValueError("fit receipt frozen train artifact path is not absolute")
    train_corpus, train_raw_sha = _read_json_artifact(train_artifact_path)
    if train_raw_sha != fit_inputs.get("train_artifact_raw_sha256"):
        raise ValueError("frozen train artifact raw bytes changed after fitting")
    _reject_quarantined_holdout(train_corpus)
    recomputed_train_integrity = _validate_split_artifact(
        train_corpus,
        preregistration,
        expected_artifact_split="train",
    )
    train_semantic_sha = _teacher_semantic_sha256(train_corpus)
    if train_semantic_sha != fit_inputs.get("train_artifact_semantic_sha256"):
        raise ValueError("frozen train artifact semantic payload changed after fitting")
    train_labels, _ = _materialize_labels(train_corpus, selected_split="train")
    train_integrity = {
        "schema": SPLIT_INTEGRITY_SCHEMA,
        **recomputed_train_integrity,
    }
    train_publication = _validate_pair_publication(
        train_artifact_path,
        train_raw_sha,
        recomputed_train_integrity,
        preregistration,
    )
    if fit_receipt.get("split_integrity") != train_integrity:
        raise ValueError("fit receipt train split-integrity evidence differs")
    if fit_receipt.get("pair_publication") != train_publication:
        raise ValueError("fit receipt pair-publication evidence differs")
    if (
        train_publication["raw_artifact_sha256"]
        != pair_completion["local_publication"]["raw_artifact_sha256"]
        or train_publication["train_raw_artifact_sha256"]
        != pair_completion["train"]["raw_artifact_sha256"]
        or train_publication["sealed_holdout_raw_artifact_sha256"]
        != pair_completion["sealed_holdout"]["raw_artifact_sha256"]
    ):
        raise ValueError("train publication differs from central pair completion")
    if sealed_holdout_display != train_publication["sealed_holdout_path"]:
        raise ValueError(
            "sealed holdout argument differs from the frozen pair publication path"
        )
    if sealed_holdout_display != pair_completion["sealed_holdout"]["path"]:
        raise ValueError("sealed holdout differs from central pair completion")
    train_roots = {label.state_key for label in train_labels}
    train_finals = {
        option.final_state_key
        for label in train_labels
        for option in label.options
    }

    leader, leader_raw_sha = _read_profile_artifact(leader_path)
    if fit_inputs["leader_profile_sha256"] != leader_raw_sha:
        raise ValueError("rejected leader profile changed after fitting")
    models: dict[str, dict[str, Any]] = {}
    train_semantic_sha = str(fit_inputs["train_artifact_semantic_sha256"])
    for role in ("primary_nonroute", "route_ablation"):
        model_path = Path(fit_receipt["models"][role]["path"])
        model, model_raw_sha = _read_model_artifact(model_path)
        if model_raw_sha != fit_receipt["models"][role]["sha256"]:
            raise ValueError(f"frozen model changed: {role}")
        if (
            model.get("teacher_corpus_semantic_sha256") != train_semantic_sha
            or model.get("teacher_corpus_raw_artifact_sha256") != train_raw_sha
        ):
            raise ValueError(f"model train-artifact binding differs: {role}")
        models[role] = model
    profile_path = Path(fit_receipt["profile"]["path"])
    profile, profile_raw_sha = _read_profile_artifact(profile_path)
    if profile_raw_sha != fit_receipt["profile"]["sha256"]:
        raise ValueError("frozen distilled profile changed")

    claim_binding = fit_receipt.get("one_shot_holdout")
    if (
        not isinstance(claim_binding, Mapping)
        or claim_binding.get("schema") != HOLDOUT_CLAIM_BINDING_SCHEMA
        or not isinstance(claim_binding.get("claim_path"), str)
    ):
        raise ValueError("fit receipt one-shot claim binding is missing")
    recorded_claim_path = Path(str(claim_binding["claim_path"]))
    if not recorded_claim_path.is_absolute():
        raise ValueError("fit receipt one-shot claim path is not absolute")
    recorded_claim_path_resolved = recorded_claim_path.resolve()
    expected_claim_path = _holdout_claim_path(preregistration)
    if (
        str(recorded_claim_path_resolved) != str(recorded_claim_path)
        or recorded_claim_path_resolved != expected_claim_path
        or holdout_claim_path != expected_claim_path
    ):
        raise ValueError("fit receipt one-shot claim path is not canonical")

    corpus_path = _canonical_cli_input(
        Path(sealed_holdout_argument), "sealed holdout artifact"
    )
    corpus, corpus_raw_sha = _read_json_artifact(corpus_path)
    _reject_quarantined_holdout(corpus)
    holdout_integrity = _validate_split_artifact(
        corpus,
        preregistration,
        expected_artifact_split="sealed_holdout",
    )
    holdout_publication = _validate_pair_publication(
        corpus_path,
        corpus_raw_sha,
        holdout_integrity,
        preregistration,
    )
    corpus_semantic_sha = _teacher_semantic_sha256(corpus)
    if holdout_integrity["artifact_semantic_sha256"] != corpus_semantic_sha:
        raise AssertionError("validated holdout semantic digest changed")
    pairing_checks = {
        "dataset_pairing_identity_matches": (
            holdout_integrity["dataset_pairing_sha256"]
            == train_integrity["dataset_pairing_sha256"]
        ),
        "source_cross_split_audit_matches": (
            holdout_integrity["source_cross_split_audit_sha256"]
            == train_integrity["source_cross_split_audit_sha256"]
        ),
        "train_commitment_matches_holdout_counterpart": (
            train_integrity["semantic_keys_sha256"]
            == holdout_integrity["counterpart_semantic_keys_sha256"]
        ),
        "holdout_commitment_matches_train_counterpart": (
            holdout_integrity["semantic_keys_sha256"]
            == train_integrity["counterpart_semantic_keys_sha256"]
        ),
        "train_label_payload_matches_holdout_counterpart": (
            train_integrity["label_payload_sha256"]
            == holdout_integrity["counterpart_label_payload_sha256"]
        ),
        "holdout_label_payload_matches_train_counterpart": (
            holdout_integrity["label_payload_sha256"]
            == train_integrity["counterpart_label_payload_sha256"]
        ),
        "train_source_matches_holdout_counterpart": (
            train_integrity["source_combined_corpus"]
            == holdout_integrity["counterpart_source_combined_corpus"]
        ),
        "holdout_source_matches_train_counterpart": (
            holdout_integrity["source_combined_corpus"]
            == train_integrity["counterpart_source_combined_corpus"]
        ),
        "source_pairing_mode_matches": (
            holdout_integrity["source_pairing_mode"]
            == train_integrity["source_pairing_mode"]
        ),
        "pair_publication_receipt_matches": (
            holdout_publication == train_publication
        ),
    }
    if not all(pairing_checks.values()):
        raise ValueError("train and sealed-holdout artifacts are not a bound pair")
    holdout, leakage = _materialize_labels(corpus, selected_split="holdout")
    if not holdout:
        raise ValueError("teacher corpus has no holdout labels")
    holdout_roots = {label.state_key for label in holdout}
    holdout_finals = {
        option.final_state_key for label in holdout for option in label.options
    }
    cross_artifact_leakage = _require_clean_cross_artifact_split(
        train_roots=train_roots,
        train_finals=train_finals,
        holdout_roots=holdout_roots,
        holdout_finals=holdout_finals,
    )

    model_metrics: dict[str, dict[str, Any]] = {}
    for role, model in models.items():
        model_metrics[role] = _metrics(
            holdout,
            _linear_scorer(
                tuple(int(value) for value in model["coefficients"]),
                str(model["feature_group"]),
            ),
            include_rows=True,
        )

    profile_weights = tuple(
        int(getattr(profile.weights, name)) for name in FEATURE_NAMES
    )
    baseline = baseline_profile()
    baseline_weights = tuple(
        int(getattr(baseline.weights, name)) for name in FEATURE_NAMES
    )
    leader_weights = tuple(
        int(getattr(leader.weights, name)) for name in FEATURE_NAMES
    )
    references = {
        "baseline": _metrics(holdout, _profile_scorer(baseline_weights)),
        "rejected_leader": _metrics(holdout, _profile_scorer(leader_weights)),
    }
    profile_metrics = _metrics(
        holdout,
        _profile_scorer(profile_weights),
        include_rows=True,
    )
    gates = _holdout_gate(
        model_metrics["primary_nonroute"],
        model_metrics["route_ablation"],
        profile_metrics,
        references["baseline"],
        references["rejected_leader"],
    )
    corpus_contract = corpus["contract"]
    corpus_promotion_eligible = bool(
        corpus_contract.get("promotion_eligible", True)
        and not corpus_contract.get("exploratory_only", False)
    )
    receipt = {
        "schema": HOLDOUT_RECEIPT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "claim_scope": (
            "one-shot state-disjoint teacher holdout ranking evidence; no game "
            "strength, Elo, live evaluator, or release claim"
        ),
        "inputs": {
            "preregistration": str(preregistration.path),
            "preregistration_schema": preregistration.schema,
            "preregistration_sha256": preregistration.sha256,
            "artifact_split": "sealed_holdout",
            "sealed_holdout_artifact": str(corpus_path),
            "sealed_holdout_artifact_raw_sha256": corpus_raw_sha,
            "sealed_holdout_artifact_semantic_sha256": corpus_semantic_sha,
            "teacher_corpus": str(corpus_path),
            "teacher_corpus_id": corpus["corpus_id"],
            "teacher_corpus_sha256": corpus_semantic_sha,
            "teacher_corpus_raw_artifact_sha256": corpus_raw_sha,
            "fit_receipt": str(fit_receipt_path),
            "fit_receipt_sha256": fit_receipt_raw_sha,
            "holdout_claim": str(holdout_claim_path),
            "holdout_claim_sha256": holdout_claim_raw_sha,
            "leader_profile": str(leader_path),
            "leader_profile_id": leader.profile_id,
            "leader_profile_sha256": leader_raw_sha,
        },
        "split_integrity": {
            "schema": SPLIT_INTEGRITY_SCHEMA,
            **holdout_integrity,
            "pairing_checks": pairing_checks,
        },
        "pair_publication": holdout_publication,
        "pair_completion": pair_completion_reference,
        "leakage_audit": {
            "within_holdout_artifact": leakage,
            "cross_artifact": {
                name: len(values)
                for name, values in cross_artifact_leakage.items()
            },
        },
        "models": {
            role: {
                "model_id": models[role]["model_id"],
                "feature_group": models[role]["feature_group"],
                "metrics": model_metrics[role],
            }
            for role in models
        },
        "profile": {
            "profile_id": profile.profile_id,
            "weights": list(profile_weights),
            "metrics": profile_metrics,
        },
        "references": references,
        "gates": gates,
        "corpus_promotion_contract": {
            "exploratory_only": bool(
                corpus_contract.get("exploratory_only", False)
            ),
            "promotion_eligible": corpus_promotion_eligible,
            "promotion_ineligible_reasons": list(
                corpus_contract.get("promotion_ineligible_reasons", ())
            ),
            "missing_positional_series": list(
                corpus_contract.get("missing_positional_series", ())
            ),
        },
        "promotion_recommendation": bool(
            corpus_promotion_eligible
            and gates["primary_nonroute"]["passed"]
            and gates["distilled_profile"]["passed"]
        ),
        "route_features_deserve_live_consideration": bool(
            gates["route_ablation"]["passed"]
        ),
        "runtime": {
            "python": sys.version,
            "numpy": np.__version__,
            "argv": list(sys.argv),
            "script_sha256": initial_script_sha256,
            "implementation_sha256": initial_implementation_sha256,
        },
    }
    final_pair_completion, final_pair_completion_raw = _trusted_pair_completion(
        preregistration
    )
    if (
        final_pair_completion != pair_completion
        or final_pair_completion_raw != pair_completion_raw_sha
        or _validate_pair_publication(
            train_artifact_path,
            train_raw_sha,
            recomputed_train_integrity,
            preregistration,
        )
        != train_publication
        or _validate_pair_publication(
            corpus_path,
            corpus_raw_sha,
            holdout_integrity,
            preregistration,
        )
        != holdout_publication
        or holdout_publication["raw_artifact_sha256"]
        != pair_completion["local_publication"]["raw_artifact_sha256"]
        or holdout_publication["train_raw_artifact_sha256"]
        != pair_completion["train"]["raw_artifact_sha256"]
        or holdout_publication["sealed_holdout_raw_artifact_sha256"]
        != pair_completion["sealed_holdout"]["raw_artifact_sha256"]
    ):
        raise ValueError("pair publication changed before holdout receipt")
    final_fit_receipt, final_fit_receipt_raw = _read_json_artifact(fit_receipt_path)
    final_train, final_train_raw = _read_json_artifact(train_artifact_path)
    final_leader, final_leader_raw = _read_profile_artifact(leader_path)
    final_models = {
        role: _read_model_artifact(Path(fit_receipt["models"][role]["path"]))
        for role in models
    }
    final_profile, final_profile_raw = _read_profile_artifact(profile_path)
    final_holdout, final_holdout_raw = _read_json_artifact(corpus_path)
    final_pair_receipt, final_pair_receipt_raw = _read_json_artifact(
        Path(train_publication["path"])
    )
    final_claim, final_claim_raw = _read_json_artifact(holdout_claim_path)
    tail_pair_completion, tail_pair_completion_raw = _trusted_pair_completion(
        preregistration
    )
    if (
        final_fit_receipt != fit_receipt
        or final_fit_receipt_raw != fit_receipt_raw_sha
        or final_train != train_corpus
        or final_train_raw != train_raw_sha
        or final_leader != leader
        or final_leader_raw != leader_raw_sha
        or any(
            final_models[role][0] != models[role]
            or final_models[role][1] != fit_receipt["models"][role]["sha256"]
            for role in models
        )
        or final_profile != profile
        or final_profile_raw != profile_raw_sha
        or final_holdout != corpus
        or final_holdout_raw != corpus_raw_sha
        or final_pair_receipt_raw != train_publication["raw_artifact_sha256"]
        or final_claim != holdout_claim
        or final_claim_raw != holdout_claim_raw_sha
        or tail_pair_completion != pair_completion
        or tail_pair_completion_raw != pair_completion_raw_sha
        or _sha256(Path(__file__).resolve()) != initial_script_sha256
        or _implementation_hashes() != initial_implementation_sha256
        or _sha256(feature_module) != initial_feature_module_sha256
    ):
        raise ValueError("holdout evaluation inputs changed before receipt publication")
    _atomic_exclusive_json(
        receipt_path,
        receipt,
        conflict_message=(
            "holdout receipt already exists; one-shot evidence is never overwritten"
        ),
    )
    print(
        json.dumps(
            {
                "receipt": str(receipt_path),
                "gates": gates,
                "promotion_recommendation": receipt["promotion_recommendation"],
                "corpus_promotion_contract": receipt[
                    "corpus_promotion_contract"
                ],
                "route_features_deserve_live_consideration": receipt[
                    "route_features_deserve_live_consideration"
                ],
                "primary_holdout": model_metrics["primary_nonroute"]["overall"],
                "profile_holdout": profile_metrics["overall"],
                "references": {
                    name: metrics["overall"]
                    for name, metrics in references.items()
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fit a train-only deep-teacher value candidate, then evaluate its "
            "frozen artifacts with a separate one-shot holdout command."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    preregister = commands.add_parser(
        "preregister",
        help="create the exclusive larger cycle-4 protocol manifest",
    )
    preregister.add_argument("--output", type=Path, required=True)
    preregister.add_argument("--base-deployed-commit", required=True)
    preregister.add_argument("--integrated-engine-source-commit", required=True)
    preregister.add_argument("--train-seed", type=int, required=True)
    preregister.add_argument("--holdout-seed", type=int, required=True)
    preregister.add_argument("--selection-seed", type=int, required=True)
    preregister.add_argument("--match-seed", type=int, required=True)
    preregister.add_argument("--development-source", type=Path)
    preregister.add_argument("--development-consumption-evidence", type=Path)
    preregister.add_argument("--development-source-metadata", type=Path)
    preregister.add_argument("--dry-run", action="store_true")
    preregister.set_defaults(handler=_preregister_command)
    development = commands.add_parser(
        "import-development",
        help="exactly relabel a frozen consumed corpus as train-only development",
    )
    development.add_argument("--preregistration", type=Path, required=True)
    development.add_argument("consumed_source", type=Path)
    development.add_argument("output", type=Path)
    development.add_argument("--dry-run", action="store_true")
    development.set_defaults(handler=_import_development_command)
    pair = commands.add_parser(
        "pair-artifacts",
        help="bind distinct train and fresh cycle-4 holdout sources",
    )
    pair.add_argument("--preregistration", type=Path, required=True)
    pair.add_argument("train_artifact", type=Path)
    pair.add_argument("sealed_holdout_source", type=Path)
    pair.add_argument("output", type=Path)
    pair_mode = pair.add_mutually_exclusive_group()
    pair_mode.add_argument("--dry-run", action="store_true")
    pair_mode.add_argument("--metadata-only", action="store_true")
    pair.set_defaults(handler=_pair_artifacts_command)
    split = commands.add_parser("split-artifacts")
    split.add_argument("--preregistration", type=Path, required=True)
    split.add_argument("teacher_corpus", type=Path, metavar="combined_teacher_corpus")
    split.add_argument("output", type=Path)
    split.set_defaults(handler=_split_artifacts_command)
    fit = commands.add_parser("fit")
    fit.add_argument("--preregistration", type=Path, required=True)
    fit.add_argument("teacher_corpus", type=Path, metavar="train_artifact")
    fit.add_argument("leader_profile", type=Path)
    fit.add_argument("output", type=Path)
    fit.add_argument(
        "--adverse-pair-weight",
        type=float,
        default=DEFAULT_ADVERSE_PAIR_WEIGHT,
        help=(
            "Pairwise weight multiplier when exactly one option is proven "
            "adverse to the mover (default: %(default)s)."
        ),
    )
    fit.set_defaults(handler=_fit_command)
    holdout = commands.add_parser("evaluate-holdout")
    holdout.add_argument("--preregistration", type=Path, required=True)
    holdout.add_argument(
        "teacher_corpus", type=Path, metavar="sealed_holdout_artifact"
    )
    holdout.add_argument("leader_profile", type=Path)
    holdout.add_argument("fit_receipt", type=Path)
    holdout.add_argument("output", type=Path)
    holdout.set_defaults(handler=_evaluate_holdout_command)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
