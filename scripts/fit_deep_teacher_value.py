from __future__ import annotations

import argparse
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
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

from scottish_progressive.corpus_shards import progressive_state_dedup_key
from scottish_progressive.corpus_shards import CorpusIdentity
from scottish_progressive.corpus_pipeline import NativeGenerationContract
from scottish_progressive.corpus_samples import NATIVE_BOUNDARY_SAMPLE_SCHEMA
from scottish_progressive import evaluation, series_mate
from scottish_progressive.fast_training import CachedFeatures, FEATURE_NAMES
from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT, ENGINE_VERSION
from scottish_progressive.native_corpus import (
    NativeCorpusConfig,
    NativeProfileSchedule,
    NativeRankPolicy,
    bind_native_profiles,
    semantic_config_digest,
)
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
DEVELOPMENT_PROVENANCE_SCHEMA = "spc-consumed-holdout-development-v1"
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
PREREGISTRATION_SCHEMA = re.compile(r"spc-cycle[1-9][0-9]*-one-shot-protocol-v1")
HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
FROZEN_IMPLEMENTATION_PATHS = (
    "scripts/generate_native_corpus.py",
    "scripts/build_native_teacher_corpus.py",
    "scripts/augment_native_teacher_semantics.py",
    "scripts/merge_native_teacher_tiers.py",
    "scripts/fit_deep_teacher_value.py",
    "src/scottish_progressive/native_teacher.py",
    "src/scottish_progressive/teacher_value_features.py",
)
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
    "first_attempt",
    "shard_size",
    "batch_size",
    "workers",
    "max_attempt_series",
    "max_frontier_states",
    "candidate_count",
    "max_positions_per_series",
    "max_positions_per_game",
    "policy",
    "profile_schedule",
}
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

    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load JSON artifact {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact root must be an object: {path}")
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


def _holdout_claim_path(preregistration: Preregistration) -> Path:
    """Return the cycle/seed claim marker shared by every fit/output path."""

    holdout = preregistration.manifest["trajectory_corpora"]["sealed_holdout"]
    seed = _exact_positive_int(holdout.get("seed"), "sealed holdout seed")
    filename = f"{preregistration.schema}-seed-{seed}.json"
    return (_git_common_dir() / "spc-one-shot-holdout-claims" / filename).resolve()


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
    return {
        "feature_contract_sha256": hashlib.sha256(
            _canonical_json(feature_contract)
        ).hexdigest(),
        "selection_contract_sha256": hashlib.sha256(
            _canonical_json(selection_contract)
        ).hexdigest(),
        "holdout_gate_ast_sha256": hashlib.sha256(holdout_gate_ast).hexdigest(),
    }


def _current_frozen_implementation() -> dict[str, str]:
    return {
        **{
            relative: _sha256(_repository_file(relative))
            for relative in FROZEN_IMPLEMENTATION_PATHS
        },
        **_protocol_contract_hashes(),
    }


def _validate_preregistered_runtime(runtime: object) -> None:
    runtime = _require_exact_fields(
        runtime,
        {
            "platform",
            "python",
            "compiler",
            "native_eval_binary_sha256",
            "native_mate_binary_sha256",
        },
        "runtime contract",
    )
    expected_python = runtime.get("python")
    actual_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if expected_python != actual_python:
        raise ValueError(
            f"preregistration Python runtime drifted: {expected_python!r} != {actual_python!r}"
        )
    expected_platform = runtime.get("platform")
    actual_platform = f"{platform.system()} x86-64"
    if expected_platform != actual_platform:
        raise ValueError(
            f"preregistration platform drifted: {expected_platform!r} != {actual_platform!r}"
        )
    expected_compiler = runtime.get("compiler")
    actual_compiler = platform.python_compiler()
    compiler_match = re.search(r"MSC v\.(\d{2})(\d{2})", actual_compiler)
    if compiler_match is None:
        raise ValueError(
            f"preregistration requires an MSVC runtime; got {actual_compiler!r}"
        )
    compiler_family = f"MSVC {compiler_match.group(1)}.{compiler_match.group(2)}"
    if not isinstance(expected_compiler, str) or not expected_compiler.startswith(
        compiler_family
    ):
        raise ValueError(
            "preregistration compiler drifted: "
            f"{expected_compiler!r} is not {compiler_family!r}"
        )
    for name, module in (
        ("native_eval_binary_sha256", evaluation._native_eval),
        ("native_mate_binary_sha256", series_mate._native_mate),
    ):
        expected = runtime.get(name)
        module_path = getattr(module, "__file__", None)
        if (
            not isinstance(expected, str)
            or HEX_SHA256.fullmatch(expected) is None
            or not isinstance(module_path, str)
            or _sha256(Path(module_path).resolve()) != expected
        ):
            raise ValueError(f"preregistration {name} does not match the loaded runtime")


def _exact_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"preregistration {label} must be a positive integer")
    return value


def _exact_nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"preregistration {label} must be a nonnegative integer")
    return value


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
        {"path", "corpus_id", "semantic_sha256", "raw_artifact_sha256"},
        f"{label} artifact source",
    )
    if not isinstance(source["path"], str) or not source["path"].strip():
        raise ValueError(f"preregistration {label} artifact source path is malformed")
    if not isinstance(source["corpus_id"], str) or not source["corpus_id"]:
        raise ValueError(f"preregistration {label} artifact source ID is malformed")
    for name in ("semantic_sha256", "raw_artifact_sha256"):
        digest = source[name]
        if not isinstance(digest, str) or HEX_SHA256.fullmatch(digest) is None:
            raise ValueError(
                f"preregistration {label} artifact source {name} is malformed"
            )


def _load_preregistration(path: Path) -> Preregistration:
    path = path.expanduser().resolve()
    manifest, manifest_raw_sha = _read_json_artifact(path)
    schema = str(manifest.get("schema", ""))
    if PREREGISTRATION_SCHEMA.fullmatch(schema) is None:
        raise ValueError(f"unsupported preregistration schema: {schema!r}")
    if manifest.get("status") != "pre-registered-before-generation":
        raise ValueError("preregistration was not frozen before generation")

    source = _require_exact_fields(
        manifest.get("source"),
        {
            "base_deployed_commit",
            "integrated_engine_source_commit",
            "engine_version",
            "engine_source_fingerprint",
            "native_eval_source_identity_sha256",
            "native_mate_source_identity_sha256",
        },
        "source contract",
        optional={"carrier_commit_note"},
    )
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
        {"seed", "attempts"},
        "train trajectory",
        optional={"artifact_source"},
    )
    holdout_trajectory = _require_exact_fields(
        trajectories.get("sealed_holdout"),
        {"seed", "attempts", "one_shot"},
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
    _exact_positive_int(train_trajectory.get("attempts"), "train attempts")
    _exact_positive_int(
        holdout_trajectory.get("attempts"), "sealed holdout attempts"
    )
    if train_seed == holdout_seed:
        raise ValueError("preregistration train and holdout seeds must differ")
    if holdout_trajectory.get("one_shot") is not True:
        raise ValueError("preregistration sealed holdout must be one-shot")
    _validate_declared_artifact_source(
        train_trajectory.get("artifact_source"), "train"
    )
    _validate_declared_artifact_source(
        holdout_trajectory.get("artifact_source"), "sealed holdout"
    )
    _exact_nonnegative_int(
        shared_trajectory.get("first_attempt"), "shared first attempt"
    )
    for name in (
        "shard_size",
        "batch_size",
        "workers",
        "max_attempt_series",
        "max_frontier_states",
        "candidate_count",
        "max_positions_per_series",
        "max_positions_per_game",
    ):
        _exact_positive_int(shared_trajectory.get(name), f"shared {name}")
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
    if type(teacher.get("prior_receipt_cache_reuse")) is not bool:
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
        },
        "holdout integrity contract",
        optional={
            "quarantined_holdout_corpus_sha256",
            "quarantine_reason",
            "semantic_key",
        },
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
    if not str(integrity.get("seed_burn_rule", "")).strip():
        raise ValueError("preregistration seed-burn rule is missing")
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

    preflight = _require_exact_fields(
        manifest.get("preflight"),
        {"holdout_consumed", "generation_started"},
        "preflight",
        optional={"integrated_targeted_tests"},
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
    return Preregistration(
        path=path,
        sha256=manifest_raw_sha,
        schema=schema,
        expected_train_labels=expected_train,
        expected_holdout_labels=expected_holdout,
        manifest=manifest,
    )


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


def _validate_combined_corpus_preregistration(
    corpus: Mapping[str, Any],
    preregistration: Preregistration,
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
            trajectory["shared_config"]["first_attempt"],
            trajectory["shared_config"]["first_attempt"]
            + trajectory["train"]["attempts"],
        ),
        "holdout": (
            trajectory["shared_config"]["first_attempt"],
            trajectory["shared_config"]["first_attempt"]
            + trajectory["sealed_holdout"]["attempts"],
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


def _dataset_pairing_sha256(
    *,
    preregistration_sha256: str,
    train_semantic_keys_sha256: str,
    holdout_semantic_keys_sha256: str,
    train_label_payload_sha256: str,
    holdout_label_payload_sha256: str,
    cross_split_audit_sha256: str,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "schema": "spc-deep-teacher-dataset-pair-v2",
                "preregistration_sha256": preregistration_sha256,
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
    supplied_path = Path(str(declared_source["path"]))
    source_path = (
        supplied_path
        if supplied_path.is_absolute()
        else preregistration.path.parent / supplied_path
    ).expanduser().resolve()
    source, raw_sha = _read_json_artifact(source_path)
    if raw_sha != declared_source["raw_artifact_sha256"]:
        raise ValueError("preregistered artifact source raw bytes changed")
    if _teacher_semantic_sha256(source) != declared_source["semantic_sha256"]:
        raise ValueError("preregistered artifact source semantic payload changed")
    if source.get("corpus_id") != declared_source["corpus_id"]:
        raise ValueError("preregistered artifact source corpus ID changed")
    return source


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
            }:
                raise ValueError("declared development train provenance is missing")
            if (
                provenance.get("schema") != DEVELOPMENT_PROVENANCE_SCHEMA
                or provenance.get("original_split") not in {"train", "holdout"}
                or provenance.get("original_artifact_semantic_sha256")
                != declared_source["semantic_sha256"]
                or provenance.get("original_artifact_raw_sha256")
                != declared_source["raw_artifact_sha256"]
            ):
                raise ValueError("declared development train provenance differs")
            candidate["split"] = provenance["original_split"]
        if _semantic_teacher_value(candidate) != _semantic_teacher_value(original):
            raise ValueError("teacher split label payload differs from declared source")


def _validate_split_preregistration_provenance(
    corpus: Mapping[str, Any],
    preregistration: Preregistration,
    *,
    artifact_split: str,
    raw_labels: Sequence[object],
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
    if declared_source is not None:
        source = _read_declared_artifact_source(preregistration, declared_source)
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
        "prior_receipt_cache_reuse": manifest["teacher"][
            "prior_receipt_cache_reuse"
        ],
    }
    for name, expected in generation_expected.items():
        if generation.get(name) != expected:
            raise ValueError(f"teacher split generation {name} drifted")
    for name in ("train_corpus_sha256", "holdout_corpus_sha256"):
        digest = generation.get(name)
        if not isinstance(digest, str) or HEX_SHA256.fullmatch(digest) is None:
            raise ValueError(f"teacher split generation {name} is malformed")

    teacher_profile = corpus.get("teacher_profile")
    expected_teacher_profile = _preregistered_source_profiles(preregistration)[
        0
    ].as_dict()
    if not isinstance(teacher_profile, Mapping) or dict(teacher_profile) != expected_teacher_profile:
        raise ValueError("teacher split profile differs from preregistration")

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
        if not isinstance(config, Mapping) or dict(config) != expected_config:
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
    start = trajectory["shared_config"]["first_attempt"]
    stop = start + trajectory[trajectory_name]["attempts"]
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
    source = artifact.get("source_combined_corpus")
    if not isinstance(source, Mapping):
        raise ValueError("teacher artifact source binding is missing")
    source_id = source.get("corpus_id")
    source_semantic_sha = source.get("semantic_sha256")
    source_raw_sha = source.get("raw_artifact_sha256")
    if (
        not isinstance(source_id, str)
        or not source_id.startswith("spc-native-mixed-teacher-")
        or not isinstance(source_semantic_sha, str)
        or HEX_SHA256.fullmatch(source_semantic_sha) is None
        or not isinstance(source_raw_sha, str)
        or HEX_SHA256.fullmatch(source_raw_sha) is None
    ):
        raise ValueError("teacher artifact source binding is malformed")
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
    if not isinstance(contract, Mapping) or contract.get("split_artifact_isolated") is not True:
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
    )
    if artifact.get("dataset_pairing_sha256") != expected_pairing_sha:
        raise ValueError("teacher artifact dataset-pairing commitment differs")
    return {
        "artifact_split": expected_artifact_split,
        "source_combined_corpus_id": source_id,
        "source_combined_corpus_semantic_sha256": source_semantic_sha,
        "source_combined_corpus_raw_artifact_sha256": source_raw_sha,
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
                **dict(source["contract"]),
                "split_artifact_isolated": True,
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
    preregistration = _load_preregistration(args.preregistration)
    source_path = args.teacher_corpus.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FileExistsError(
            "split-artifact output is not empty; use a fresh directory"
        )
    source, source_raw_sha = _read_json_artifact(source_path)
    _reject_quarantined_holdout(source)
    _validate_combined_corpus_preregistration(source, preregistration)
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
    dataset_pairing_sha = _dataset_pairing_sha256(
        preregistration_sha256=preregistration.sha256,
        train_semantic_keys_sha256=train_semantic,
        holdout_semantic_keys_sha256=holdout_semantic,
        train_label_payload_sha256=train_label_payload,
        holdout_label_payload_sha256=holdout_label_payload,
        cross_split_audit_sha256=cross_audit_sha,
    )
    source_semantic_sha = _teacher_semantic_sha256(source)
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
    )
    _validate_split_artifact(train, preregistration, expected_artifact_split="train")
    _validate_split_artifact(
        holdout,
        preregistration,
        expected_artifact_split="sealed_holdout",
    )
    train_path = output / "train-teacher-artifact.json"
    holdout_path = output / "sealed-holdout-teacher-artifact.json"
    _exclusive_json(
        train_path,
        train,
        conflict_message="train teacher artifact already exists",
    )
    _exclusive_json(
        holdout_path,
        holdout,
        conflict_message="sealed holdout teacher artifact already exists",
    )
    print(
        json.dumps(
            {
                "preregistration_sha256": preregistration.sha256,
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
            ("cycle3-cv-component|" + "|".join(keys)).encode()
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
        name="cycle 3 deep-teacher distilled profile",
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
    preregistration = _load_preregistration(args.preregistration)
    corpus_path = args.teacher_corpus.expanduser().resolve()
    leader_path = args.leader_profile.expanduser().resolve()
    output = args.output.expanduser().resolve()
    receipt_path = output / "deep-teacher-fit-receipt.json"
    holdout_claim_path = _holdout_claim_path(preregistration)
    if output.exists() and (
        not output.is_dir() or any(output.iterdir())
    ):
        raise FileExistsError(
            "fit output is not empty; use a fresh directory so frozen model "
            "evidence cannot be overwritten"
        )
    corpus, corpus_raw_sha = _read_json_artifact(corpus_path)
    _reject_quarantined_holdout(corpus)
    split_integrity = _validate_split_artifact(
        corpus,
        preregistration,
        expected_artifact_split="train",
    )
    corpus_semantic_sha = _teacher_semantic_sha256(corpus)
    if split_integrity["artifact_semantic_sha256"] != corpus_semantic_sha:
        raise AssertionError("validated train semantic digest changed")
    corpus_id = str(corpus["corpus_id"])
    adverse_pair_weight = _validate_adverse_pair_weight(
        getattr(args, "adverse_pair_weight", DEFAULT_ADVERSE_PAIR_WEIGHT)
    )
    train, leakage = _materialize_labels(corpus, selected_split="train")
    if not train:
        raise ValueError("teacher corpus has no train labels")
    leader, leader_raw_sha = _read_profile_artifact(leader_path)

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
        _atomic_json(model_path, model)
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
                    "sha256": _sha256(model_path),
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
    _atomic_json(profile_path, profile)

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
        "leakage_audit": leakage,
        "feature_contract": {
            "schema": TEACHER_VALUE_FEATURE_SCHEMA,
            "feature_names": list(TEACHER_VALUE_FEATURE_NAMES),
            "feature_module": str(Path(sys.modules[TeacherValueFeaturesV3.__module__].__file__).resolve()),
            "feature_module_sha256": _sha256(Path(sys.modules[TeacherValueFeaturesV3.__module__].__file__).resolve()),
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
            "sha256": _sha256(profile_path),
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
            "script_sha256": _sha256(Path(__file__).resolve()),
            "implementation_sha256": _implementation_hashes(),
        },
    }
    _atomic_json(receipt_path, receipt)
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
    preregistration = _load_preregistration(args.preregistration)
    sealed_holdout_argument = args.teacher_corpus
    sealed_holdout_display = os.fspath(sealed_holdout_argument)
    leader_path = args.leader_profile.expanduser().resolve()
    fit_receipt_path = args.fit_receipt.expanduser().resolve()
    output = args.output.expanduser().resolve()
    receipt_path = output / "deep-teacher-holdout-receipt.json"
    if receipt_path.exists():
        raise FileExistsError(
            "holdout receipt already exists; the one-shot holdout command refuses "
            "to overwrite or rerun selection evidence"
        )

    # Validate every unsealed input first.  Nothing above or below this block
    # opens, hashes, or parses the sealed holdout artifact.
    fit_receipt, fit_receipt_raw_sha = _read_json_artifact(fit_receipt_path)
    if fit_receipt.get("schema") != FIT_RECEIPT_SCHEMA:
        raise ValueError("fit receipt schema mismatch")
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
    if fit_receipt["runtime"]["script_sha256"] != _sha256(Path(__file__).resolve()):
        raise ValueError("trainer/evaluator script changed after fitting")
    if fit_receipt["runtime"].get("implementation_sha256") != _implementation_hashes():
        raise ValueError("teacher evaluator implementation changed after fitting")
    feature_module = Path(
        sys.modules[TeacherValueFeaturesV3.__module__].__file__
    ).resolve()
    if fit_receipt["feature_contract"]["feature_module_sha256"] != _sha256(
        feature_module
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
    if fit_receipt.get("split_integrity") != train_integrity:
        raise ValueError("fit receipt train split-integrity evidence differs")
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
    holdout_claim_path = recorded_claim_path.resolve()
    expected_claim_path = _holdout_claim_path(preregistration)
    if (
        str(holdout_claim_path) != str(recorded_claim_path)
        or holdout_claim_path != expected_claim_path
    ):
        raise ValueError("fit receipt one-shot claim path is not canonical")

    # O_EXCL reserves and durably publishes the consumed marker before the
    # sealed artifact is read even once.  Any later failure burns this fit.
    holdout_claim = {
        "schema": HOLDOUT_CLAIM_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "preregistration_sha256": preregistration.sha256,
        "preregistration_schema": preregistration.schema,
        "sealed_holdout_seed": preregistration.manifest["trajectory_corpora"][
            "sealed_holdout"
        ]["seed"],
        "fit_receipt_sha256": fit_receipt_raw_sha,
        "script_sha256": _sha256(Path(__file__).resolve()),
        "requested_sealed_holdout_artifact": sealed_holdout_display,
        "requested_receipt": str(receipt_path),
    }
    _exclusive_json(holdout_claim_path, holdout_claim)

    corpus_path = Path(sealed_holdout_argument).expanduser().resolve()
    corpus, corpus_raw_sha = _read_json_artifact(corpus_path)
    _reject_quarantined_holdout(corpus)
    holdout_integrity = _validate_split_artifact(
        corpus,
        preregistration,
        expected_artifact_split="sealed_holdout",
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
            "holdout_claim_sha256": _sha256(holdout_claim_path),
            "leader_profile": str(leader_path),
            "leader_profile_id": leader.profile_id,
            "leader_profile_sha256": leader_raw_sha,
        },
        "split_integrity": {
            "schema": SPLIT_INTEGRITY_SCHEMA,
            **holdout_integrity,
            "pairing_checks": pairing_checks,
        },
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
            "script_sha256": _sha256(Path(__file__).resolve()),
            "implementation_sha256": _implementation_hashes(),
        },
    }
    _atomic_json(receipt_path, receipt)
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
