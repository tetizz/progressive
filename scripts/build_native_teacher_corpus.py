from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scottish_progressive.corpus_pipeline import read_native_generation_contract
from scottish_progressive.corpus_shards import CorpusStore
from scottish_progressive.native_teacher import (
    NativeTeacherConfig,
    build_native_teacher_corpus,
    semantic_exclusion_sha256,
    write_native_teacher_artifact,
)
from scottish_progressive.profiles import baseline_profile, load_profile


def _cross_tier_forbidden_state_keys(
    payload: dict[str, object],
) -> tuple[set[str], set[str]]:
    quality = payload.get("quality")
    labels = payload.get("labels")
    if not isinstance(quality, dict) or quality.get("status") != "complete":
        raise ValueError("cross-tier teacher artifact is not complete")
    if not isinstance(labels, list):
        raise ValueError("cross-tier teacher labels are malformed")
    states_by_split: dict[str, set[str]] = {"train": set(), "holdout": set()}
    for label in labels:
        if not isinstance(label, dict) or label.get("split") not in states_by_split:
            raise ValueError("cross-tier teacher label split is malformed")
        split = str(label["split"])
        root_key = label.get("state_key_sha256")
        options = label.get("options")
        if not isinstance(root_key, str) or not isinstance(options, list):
            raise ValueError("cross-tier teacher state provenance is malformed")
        states_by_split[split].add(root_key)
        for option in options:
            if not isinstance(option, dict) or not isinstance(
                option.get("final_state_key_sha256"), str
            ):
                raise ValueError("cross-tier option state provenance is malformed")
            states_by_split[split].add(str(option["final_state_key_sha256"]))
    overlap = states_by_split["train"] & states_by_split["holdout"]
    if overlap:
        raise ValueError(
            f"cross-tier teacher artifact already leaks {len(overlap)} exact states"
        )
    return states_by_split["holdout"], states_by_split["train"]


def _development_exclusion_state_keys(
    payload: dict[str, object],
    *,
    expected_sha256: str,
    preregistration_sha256: str,
) -> set[str]:
    artifact = payload.get("artifact")
    labels = payload.get("labels")
    artifact_preregistration = (
        artifact.get("preregistration") if isinstance(artifact, dict) else None
    )
    if (
        not isinstance(artifact, dict)
        or artifact.get("schema") != "spc-consumed-development-import-v1"
        or artifact.get("role") != "train-development"
        or artifact.get("semantic_exclusion_sha256") != expected_sha256
        or not isinstance(artifact_preregistration, dict)
        or artifact_preregistration.get("sha256") != preregistration_sha256
        or not isinstance(labels, list)
        or not labels
    ):
        raise ValueError("development exclusion artifact is malformed")
    keys: set[str] = set()
    for label in labels:
        if not isinstance(label, dict) or label.get("split") != "train":
            raise ValueError("development exclusion label is malformed")
        root = label.get("state_key_sha256")
        options = label.get("options")
        if not isinstance(root, str) or not isinstance(options, list):
            raise ValueError("development exclusion state provenance is malformed")
        keys.add(root)
        for option in options:
            if not isinstance(option, dict) or not isinstance(
                option.get("final_state_key_sha256"), str
            ):
                raise ValueError("development exclusion option is malformed")
            keys.add(str(option["final_state_key_sha256"]))
    if semantic_exclusion_sha256(keys) != expected_sha256:
        raise ValueError("development exclusion commitment differs from preregistration")
    return keys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build exact, balanced depth-3 policy labels from independently "
            "generated native train/holdout trajectories."
        )
    )
    parser.add_argument("train_root", type=Path)
    parser.add_argument("holdout_root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--teacher-profile", type=Path)
    parser.add_argument(
        "--preregistration",
        type=Path,
        required=True,
        help="Cycle-4 manifest bound before any trajectory or tier input is read.",
    )
    parser.add_argument("--target-roots", type=int, default=192)
    parser.add_argument("--train-roots", type=int, default=128)
    parser.add_argument("--minimum-series", type=int, default=4)
    parser.add_argument("--maximum-series", type=int, default=9)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--branch-cap", type=int, default=32)
    parser.add_argument("--max-work", type=int, default=10_000_000)
    parser.add_argument("--hard-negatives", type=int, default=4)
    parser.add_argument("--seed", type=int, default=2_026_082_303)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--train-attempts", type=int, default=8_192)
    parser.add_argument("--holdout-attempts", type=int, default=4_096)
    parser.add_argument(
        "--selection-mode",
        choices=("all", "tactical-low-complexity", "quiet-nonterminal"),
        default="all",
    )
    parser.add_argument("--receipt-root", type=Path)
    parser.add_argument(
        "--forbidden-train-option-final-key",
        action="append",
        default=[],
        help=(
            "Exclude a training root if any retained option reaches this exact "
            "state key. May be repeated for cross-tier leakage prevention."
        ),
    )
    parser.add_argument(
        "--prior-receipt-cache-contract-artifact",
        type=Path,
        help=(
            "Reuse receipts only after fail-closed validation against the exact "
            "cache contract embedded in this prior teacher artifact."
        ),
    )
    parser.add_argument(
        "--cross-tier-artifact",
        type=Path,
        help=(
            "Exclude roots/options that would create exact train/holdout leakage "
            "against this already-complete teacher tier."
        ),
    )
    parser.add_argument(
        "--development-exclusion-artifact",
        type=Path,
        help=(
            "Exclude every root and option-final semantic key in an exact "
            "consumed-development import from holdout selection."
        ),
    )
    parser.add_argument("--skip-tactical-gate", action="store_true")
    return parser.parse_args()


def _protocol_helpers():
    try:
        from scripts.fit_deep_teacher_value import (
            _atomic_exclusive_json,
            _expected_generation_contract_sha256,
            _exclusive_json,
            _load_preregistration,
            _read_json_artifact,
        )
    except ModuleNotFoundError:
        from fit_deep_teacher_value import (  # type: ignore[no-redef]
            _atomic_exclusive_json,
            _expected_generation_contract_sha256,
            _exclusive_json,
            _load_preregistration,
            _read_json_artifact,
        )
    return (
        _atomic_exclusive_json,
        _exclusive_json,
        _expected_generation_contract_sha256,
        _load_preregistration,
        _read_json_artifact,
    )


def _protocol_artifact_helpers():
    try:
        from scripts.fit_deep_teacher_value import (
            _teacher_semantic_sha256,
            _validate_augmented_teacher_publication,
            _validate_development_import,
            _validate_preregistered_teacher_tier_artifact,
        )
    except ModuleNotFoundError:
        from fit_deep_teacher_value import (  # type: ignore[no-redef]
            _teacher_semantic_sha256,
            _validate_augmented_teacher_publication,
            _validate_development_import,
            _validate_preregistered_teacher_tier_artifact,
        )
    return (
        _teacher_semantic_sha256,
        _validate_preregistered_teacher_tier_artifact,
        _validate_augmented_teacher_publication,
        _validate_development_import,
    )


def _teacher_completion_payload(
    output: Path,
    payload: dict[str, object],
    raw_artifact_sha256: str,
    preregistration,
    protocol_binding: dict[str, object],
) -> dict[str, object]:
    _teacher_semantic_sha256, _, _, _ = _protocol_artifact_helpers()
    return {
        "schema": "spc-cycle4-teacher-generation-completion-v1",
        "preregistration": {
            "schema": preregistration.schema,
            "raw_artifact_sha256": preregistration.sha256,
        },
        "tier": protocol_binding["teacher_generation_start"]["tier"],
        "teacher_generation_start": protocol_binding["teacher_generation_start"],
        "teacher_generation_source_binding": protocol_binding[
            "teacher_generation_source_binding"
        ],
        "output": {
            "path": str(output),
            "corpus_id": payload["corpus_id"],
            "semantic_sha256": _teacher_semantic_sha256(payload),
            "raw_artifact_sha256": raw_artifact_sha256,
        },
    }


def _publish_teacher_completion(
    completion_path: Path, completion: dict[str, object]
) -> None:
    _atomic_exclusive_json, _, _, _, _read_json_artifact = _protocol_helpers()
    if completion_path.exists():
        existing, _ = _read_json_artifact(completion_path)
        if existing != completion:
            raise FileExistsError("teacher completion receipt already differs")
    else:
        _atomic_exclusive_json(
            completion_path,
            completion,
            conflict_message="teacher completion receipt already exists",
        )


def _cycle4_teacher_start(args: argparse.Namespace):
    (
        _atomic_exclusive_json,
        _exclusive_json,
        _expected_generation_contract_sha256,
        _load_preregistration,
        _read_json_artifact,
    ) = _protocol_helpers()
    preregistration = _load_preregistration(
        args.preregistration, forbid_pair_preparation=True
    )
    try:
        from scripts.fit_deep_teacher_value import (
            _require_protocol_registry_isolation,
        )
    except ModuleNotFoundError:
        from fit_deep_teacher_value import (  # type: ignore[no-redef]
            _require_protocol_registry_isolation,
        )
    output = args.output.expanduser().resolve()
    if str(args.output) != str(output):
        raise ValueError("teacher output must be an absolute canonical path")
    train_root = args.train_root.expanduser().resolve()
    holdout_root = args.holdout_root.expanduser().resolve()
    if str(args.train_root) != str(train_root) or str(args.holdout_root) != str(
        holdout_root
    ):
        raise ValueError("teacher trajectory roots must be absolute canonical paths")
    manifest = preregistration.manifest
    teacher = manifest["teacher"]
    trajectory = manifest["trajectory_corpora"]
    tier_matches = [
        (name, tier)
        for name, tier in teacher["tiers"].items()
        if tier["selection_mode"] == args.selection_mode
        and (2 if name == "quiet_depth2" else 3) == args.depth
    ]
    if len(tier_matches) != 1:
        raise ValueError("teacher tier mode/depth is not preregistered")
    tier_name, tier = tier_matches[0]
    expected = {
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
    if any(getattr(args, name) != value for name, value in expected.items()):
        raise ValueError("teacher CLI settings differ from preregistration")
    expected_skip = tier["tactical_gate"] == "skipped-for-quiet-tier"
    if bool(args.skip_tactical_gate) is not expected_skip:
        raise ValueError("teacher tactical-gate mode differs from preregistration")
    if teacher["prior_receipt_cache_reuse"] is False and (
        args.prior_receipt_cache_contract_artifact is not None
    ):
        raise ValueError("teacher receipt-cache reuse is forbidden by preregistration")
    if args.forbidden_train_option_final_key:
        raise ValueError(
            "manual teacher exclusion keys are forbidden in the preregistered lane"
        )
    if args.teacher_profile is not None:
        raise ValueError(
            "preregistered teacher profile is fixed; --teacher-profile is forbidden"
        )
    has_development = trajectory["train"].get("artifact_source") is not None
    if (args.development_exclusion_artifact is not None) is not has_development:
        raise ValueError("teacher development exclusion input differs from preregistration")
    if tier_name == "quiet_depth2":
        if args.cross_tier_artifact is not None:
            raise ValueError("quiet tier cannot consume a cross-tier artifact")
    elif args.cross_tier_artifact is None:
        raise ValueError("tactical tier requires the completed quiet-tier artifact")
    receipt_root = (
        train_root.parent / "deep-teacher-root-receipts"
        if args.receipt_root is None
        else args.receipt_root.expanduser().resolve()
    )
    if args.receipt_root is not None and str(args.receipt_root) != str(receipt_root):
        raise ValueError("teacher receipt root must be absolute and canonical")
    bound_inputs: dict[str, object] = {
        "receipt_root": str(receipt_root),
        "teacher_profile": "preregistered-first-source-profile",
        "manual_forbidden_train_option_final_keys": [],
    }
    for name, supplied in (
        ("cross_tier_artifact", args.cross_tier_artifact),
        ("development_exclusion_artifact", args.development_exclusion_artifact),
    ):
        if supplied is None:
            bound_inputs[name] = None
            continue
        canonical = supplied.expanduser().resolve()
        if str(supplied) != str(canonical):
            raise ValueError(f"teacher {name} path must be absolute and canonical")
        bound_inputs[name] = str(canonical)
    start_path = output.with_name(output.name + ".preregistration-start.json")
    source_binding_path = output.with_name(
        output.name + ".preregistration-sources.json"
    )
    completion_path = output.with_name(
        output.name + ".preregistration-completion.json"
    )
    if (
        train_root == holdout_root
        or train_root in holdout_root.parents
        or holdout_root in train_root.parents
    ):
        raise ValueError("teacher trajectory roots must be distinct and non-nested")
    protocol_paths = {
        "train root": train_root,
        "sealed holdout root": holdout_root,
        "output": output,
        "start": start_path,
        "source binding": source_binding_path,
        "completion": completion_path,
        "receipt root": receipt_root,
    }
    protocol_paths.update(
        {
            name: Path(str(bound_inputs[name]))
            for name in ("cross_tier_artifact", "development_exclusion_artifact")
            if bound_inputs[name] is not None
        }
    )
    _require_protocol_registry_isolation(
        preregistration,
        protocol_paths,
        label="build-native-teacher-corpus",
    )
    path_items = list(protocol_paths.items())
    for index, (name, path) in enumerate(path_items):
        for other_name, other in path_items[index + 1 :]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError(
                    "teacher protocol paths must be distinct and non-nested: "
                    f"{name}, {other_name}"
                )
    for name, path in (
        ("output", output),
        ("start", start_path),
        ("source binding", source_binding_path),
        ("completion", completion_path),
    ):
        if path.is_dir():
            raise ValueError(f"teacher {name} must be a file path")
    if receipt_root.exists() and not receipt_root.is_dir():
        raise ValueError("teacher receipt root must be a directory path")
    start = {
        "schema": "spc-cycle4-teacher-generation-start-v1",
        "preregistration": {
            "schema": preregistration.schema,
            "raw_artifact_sha256": preregistration.sha256,
        },
        "tier": tier_name,
        "train_root": str(train_root),
        "sealed_holdout_root": str(holdout_root),
        "output": str(output),
        "config": expected,
        "selection_mode": args.selection_mode,
        "tactical_gate": tier["tactical_gate"],
        "inputs": bound_inputs,
    }
    start_preexisted = start_path.exists()
    if start_preexisted:
        existing, _ = _read_json_artifact(start_path)
        if existing != start:
            raise ValueError(
                "existing teacher generation-start binding differs; resume denied"
            )
    else:
        if output.exists() or source_binding_path.exists() or completion_path.exists():
            raise FileExistsError(
                "teacher output/source exists without a generation-start binding"
            )
        _atomic_exclusive_json(
            start_path,
            start,
            conflict_message="cycle-4 teacher generation was already started",
        )
    persisted_start, start_raw_sha = _read_json_artifact(start_path)
    if persisted_start != start:
        raise ValueError("teacher generation-start changed after reservation")
    if start_preexisted and not source_binding_path.exists():
        raise FileExistsError(
            "teacher generation stopped after a trajectory or input may have been "
            "opened but before its byte binding was durable; this start cannot be resumed"
        )
    teacher_start_evidence = {
        "schema": start["schema"],
        "tier": tier_name,
        "path": str(start_path),
        "raw_artifact_sha256": start_raw_sha,
    }
    trajectory_starts: dict[str, dict[str, object]] = {}
    shared = trajectory["shared_config"]
    for split, root in (("train", train_root), ("sealed_holdout", holdout_root)):
        path = root.with_name(
            root.name + ".cycle4-preregistration-generation-start.json"
        )
        payload, raw_sha = _read_json_artifact(path)
        root_binding_path = root / "cycle4-preregistration-root-binding.json"
        root_binding, root_binding_raw_sha = _read_json_artifact(
            root_binding_path
        )
        expected_split = trajectory[split]
        expected_contract_sha = _expected_generation_contract_sha256(
            preregistration, split=split
        )
        if read_native_generation_contract(root).digest_hex != expected_contract_sha:
            raise ValueError(f"{split} actual generation contract differs")
        verified_store = CorpusStore.open(root).verify()
        expected_operational = {
            "shard_size": shared["shard_size"],
            "batch_size": shared["batch_size"],
            "workers": shared["workers"],
            "verify_payloads": shared["verify_payloads"],
            "count_unique_states": shared["count_unique_states"],
        }
        if (
            set(payload)
            != {
                "schema",
                "preregistration",
                "split",
                "root",
                "receipt",
                "attempt_start",
                "attempt_stop",
                "generation_contract_sha256",
                "operational",
            }
            or
            payload.get("schema") != "spc-cycle4-trajectory-generation-start-v1"
            or payload.get("preregistration")
            != {
                "schema": preregistration.schema,
                "raw_artifact_sha256": preregistration.sha256,
            }
            or payload.get("split") != split
            or payload.get("root") != str(root)
            or payload.get("attempt_start") != expected_split["attempt_start"]
            or payload.get("attempt_stop") != expected_split["attempt_stop"]
            or payload.get("generation_contract_sha256") != expected_contract_sha
            or payload.get("operational") != expected_operational
            or root_binding
            != {
                "schema": "spc-cycle4-trajectory-root-binding-v1",
                "root": str(root),
                "generation_start": {
                    "path": str(path),
                    "raw_artifact_sha256": raw_sha,
                },
            }
        ):
            raise ValueError(f"{split} trajectory generation-start binding differs")
        receipt_path = Path(str(payload.get("receipt")))
        if (
            not receipt_path.is_absolute()
            or str(receipt_path.expanduser().resolve()) != str(receipt_path)
        ):
            raise ValueError(f"{split} trajectory receipt path is not canonical")
        receipt, receipt_raw_sha = _read_json_artifact(receipt_path)
        receipt_start = receipt.get("preregistration_generation_start")
        payload_verification = receipt.get("payload_verification")
        if (
            not isinstance(payload_verification, dict)
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
            raise ValueError(f"{split} payload verification receipt differs")
        if (
            receipt.get("format") != "spc-native-corpus-generation-receipt-v1"
            or receipt.get("root") != str(root)
            or receipt.get("planned_attempt_start") != expected_split["attempt_start"]
            or receipt.get("planned_attempt_stop") != expected_split["attempt_stop"]
            or receipt.get("planned_attempt_count") != expected_split["attempts"]
            or receipt.get("shard_size") != shared["shard_size"]
            or receipt.get("batch_size") != shared["batch_size"]
            or receipt.get("workers") != shared["workers"]
            or receipt.get("corpus") != verified_store
            or not isinstance(receipt_start, dict)
            or receipt_start.get("path") != str(path)
            or receipt_start.get("raw_artifact_sha256") != raw_sha
            or receipt_start.get("preregistration_raw_artifact_sha256")
            != preregistration.sha256
            or receipt_start.get("root_binding_path") != str(root_binding_path)
            or receipt_start.get("root_binding_raw_artifact_sha256")
            != root_binding_raw_sha
            or not isinstance(receipt.get("generation_contract"), dict)
            or receipt["generation_contract"].get("sha256") != expected_contract_sha
        ):
            raise ValueError(f"{split} completed trajectory receipt differs")
        trajectory_starts[split] = {
            "schema": payload["schema"],
            "generation_contract_sha256": expected_contract_sha,
            "corpus": dict(verified_store),
            "raw_artifact_sha256": raw_sha,
            "root_binding_path": str(root_binding_path),
            "root_binding_raw_artifact_sha256": root_binding_raw_sha,
            "completion_receipt_raw_artifact_sha256": receipt_raw_sha,
        }
    artifact_snapshots: dict[str, tuple[dict[str, object], str]] = {}
    artifact_bindings: dict[str, object] = {}
    (
        _teacher_semantic_sha256,
        _validate_preregistered_teacher_tier_artifact,
        _validate_augmented_teacher_publication,
        _validate_development_import,
    ) = _protocol_artifact_helpers()
    for name, supplied in (
        ("cross_tier_artifact", args.cross_tier_artifact),
        ("development_exclusion_artifact", args.development_exclusion_artifact),
    ):
        if supplied is None:
            artifact_bindings[name] = None
            continue
        path = Path(str(bound_inputs[name]))
        artifact, artifact_raw = _read_json_artifact(path)
        if name == "cross_tier_artifact":
            _validate_augmented_teacher_publication(
                artifact,
                preregistration,
                tier_name="quiet_depth2",
                supplied_path=path,
                supplied_raw_sha256=artifact_raw,
            )
        else:
            _validate_development_import(
                artifact,
                preregistration,
                supplied_path=path,
            )
        artifact_snapshots[name] = (artifact, artifact_raw)
        artifact_bindings[name] = {
            "path": str(path),
            "corpus_id": artifact.get("corpus_id"),
            "semantic_sha256": _teacher_semantic_sha256(artifact),
            "raw_artifact_sha256": artifact_raw,
        }
    source_binding = {
        "schema": "spc-cycle4-teacher-generation-sources-v1",
        "preregistration": start["preregistration"],
        "tier": tier_name,
        "teacher_generation_start": teacher_start_evidence,
        "trajectory_generation_starts": trajectory_starts,
        "input_artifacts": artifact_bindings,
    }
    if source_binding_path.exists():
        existing_binding, _ = _read_json_artifact(source_binding_path)
        if existing_binding != source_binding:
            raise ValueError(
                "existing teacher source binding differs; resume denied"
            )
    else:
        if output.exists() or completion_path.exists():
            raise FileExistsError(
                "teacher output exists without a generation-source binding"
            )
        _atomic_exclusive_json(
            source_binding_path,
            source_binding,
            conflict_message="cycle-4 teacher sources were already bound",
        )
    persisted_source_binding, source_binding_raw_sha = _read_json_artifact(
        source_binding_path
    )
    if persisted_source_binding != source_binding:
        raise ValueError("teacher source binding changed after publication")
    binding = {
        "schema": "spc-cycle4-preregistered-generation-provenance-v1",
        "preregistration": {
            "schema": preregistration.schema,
            "raw_artifact_sha256": preregistration.sha256,
        },
        "trajectory_generation_starts": trajectory_starts,
        "teacher_generation_start": teacher_start_evidence,
        "teacher_generation_source_binding": {
            "schema": source_binding["schema"],
            "tier": tier_name,
            "path": str(source_binding_path),
            "raw_artifact_sha256": source_binding_raw_sha,
        },
    }
    completed_payload = None
    completed_receipt = None
    if output.exists():
        completed_payload, completed_raw_sha = _read_json_artifact(output)
        _validate_preregistered_teacher_tier_artifact(
            completed_payload,
            preregistration,
            tier_name=tier_name,
            require_semantic_augmentation=False,
        )
        completed_generation = completed_payload.get("generation")
        if (
            not isinstance(completed_generation, dict)
            or completed_generation.get("preregistration_generation_provenance")
            != binding
        ):
            raise ValueError("completed teacher output provenance differs on resume")
        completed_receipt = _teacher_completion_payload(
            output,
            completed_payload,
            completed_raw_sha,
            preregistration,
            binding,
        )
        if completion_path.exists():
            persisted_completion, _ = _read_json_artifact(completion_path)
            if persisted_completion != completed_receipt:
                raise ValueError("completed teacher receipt differs on resume")
    elif completion_path.exists():
        raise FileExistsError("teacher completion exists without its output")
    return (
        preregistration,
        binding,
        output,
        artifact_snapshots,
        completed_payload,
        completion_path,
        completed_receipt,
    )


def _run(args: argparse.Namespace) -> None:
    (
        preregistration,
        protocol_binding,
        protocol_output,
        protocol_artifact_snapshots,
        completed_payload,
        completion_path,
        completed_receipt,
    ) = _cycle4_teacher_start(args)
    forbidden_train_state_keys = set(args.forbidden_train_option_final_key)
    forbidden_holdout_state_keys: set[str] = set()
    development_forbidden_holdout_state_keys: set[str] = set()
    if args.development_exclusion_artifact is not None:
        preregistration_sha = preregistration.sha256
        trajectory = preregistration.manifest.get("trajectory_corpora")
        train_contract = trajectory.get("train") if isinstance(trajectory, dict) else None
        holdout_contract = (
            trajectory.get("sealed_holdout") if isinstance(trajectory, dict) else None
        )
        source = (
            train_contract.get("artifact_source")
            if isinstance(train_contract, dict)
            else None
        )
        expected_exclusion_sha = (
            source.get("semantic_exclusion_sha256")
            if isinstance(source, dict)
            else None
        )
        if (
            not isinstance(holdout_contract, dict)
            or holdout_contract.get("development_exclusion_sha256")
            != expected_exclusion_sha
            or not isinstance(expected_exclusion_sha, str)
        ):
            raise ValueError(
                "preregistration does not freeze this development exclusion"
            )
        development_payload = protocol_artifact_snapshots[
            "development_exclusion_artifact"
        ][0]
        development_forbidden_holdout_state_keys = (
            _development_exclusion_state_keys(
                development_payload,
                expected_sha256=expected_exclusion_sha,
                preregistration_sha256=preregistration_sha,
            )
        )
    if args.cross_tier_artifact is not None:
        cross_tier_payload = protocol_artifact_snapshots["cross_tier_artifact"][0]
        cross_train, cross_holdout = _cross_tier_forbidden_state_keys(
            cross_tier_payload
        )
        forbidden_train_state_keys.update(cross_train)
        forbidden_holdout_state_keys.update(cross_holdout)
    if completed_payload is not None:
        if completed_receipt is None:
            raise RuntimeError("completed teacher output has no receipt payload")
        _publish_teacher_completion(completion_path, completed_receipt)
        quality = completed_payload["quality"]
        summary = {
            "artifact_path": str(protocol_output),
            "corpus_id": completed_payload["corpus_id"],
            "status": quality["status"],
            "accepted_roots": quality["accepted_roots"],
            "train_roots": quality["train_roots"],
            "holdout_roots": quality["holdout_roots"],
            "teacher_source_agreement_rate": quality[
                "teacher_source_agreement_rate"
            ],
            "tactical_gate_passed": quality["tactical_gate"]["passed"],
            "label_search_failures": quality["label_search_failures"],
            "cross_tier_forbidden_train_states": len(forbidden_train_state_keys),
            "cross_tier_forbidden_holdout_states": len(forbidden_holdout_state_keys),
            "development_forbidden_holdout_states": len(
                development_forbidden_holdout_state_keys
            ),
            "runtime": completed_payload["runtime"],
        }
        print(json.dumps(summary, sort_keys=True, indent=2))
        return
    prior_receipt_cache_contract = None
    if args.prior_receipt_cache_contract_artifact is not None:
        prior_payload = json.loads(
            args.prior_receipt_cache_contract_artifact.read_text(encoding="utf-8")
        )
        prior_receipt_cache_contract = prior_payload.get("generation", {}).get(
            "root_receipt_cache_contract"
        )
        if not isinstance(prior_receipt_cache_contract, dict):
            raise ValueError("prior teacher artifact has no receipt cache contract")
    config = NativeTeacherConfig(
        target_roots=args.target_roots,
        train_roots=args.train_roots,
        minimum_series=args.minimum_series,
        maximum_series=args.maximum_series,
        depth_series=args.depth,
        branch_cap=args.branch_cap,
        max_generation_positions=args.max_work,
        hard_negative_count=args.hard_negatives,
        seed=args.seed,
        workers=args.workers,
        expected_train_attempts=args.train_attempts,
        expected_holdout_attempts=args.holdout_attempts,
        selection_mode=args.selection_mode,
    )
    teacher = (
        baseline_profile()
        if args.teacher_profile is None
        else load_profile(args.teacher_profile)
    )
    payload = build_native_teacher_corpus(
        CorpusStore.open(args.train_root),
        CorpusStore.open(args.holdout_root),
        teacher,
        config=config,
        run_tactical_gate=not args.skip_tactical_gate,
        receipt_root=args.receipt_root,
        forbidden_train_state_keys=forbidden_train_state_keys,
        forbidden_holdout_state_keys=forbidden_holdout_state_keys,
        development_forbidden_holdout_state_keys=(
            development_forbidden_holdout_state_keys
        ),
        prior_receipt_cache_contract=prior_receipt_cache_contract,
        preregistration_generation_provenance=protocol_binding,
    )
    _atomic_exclusive_json, _, _, _, _read_json_artifact = _protocol_helpers()
    teacher_start = protocol_binding["teacher_generation_start"]
    persisted_start, persisted_start_raw = _read_json_artifact(
        Path(str(teacher_start["path"]))
    )
    if (
        persisted_start.get("schema") != teacher_start["schema"]
        or persisted_start.get("tier") != teacher_start["tier"]
        or persisted_start_raw != teacher_start["raw_artifact_sha256"]
    ):
        raise ValueError("teacher generation-start changed before publication")
    source_binding = protocol_binding["teacher_generation_source_binding"]
    persisted_sources, persisted_sources_raw = _read_json_artifact(
        Path(str(source_binding["path"]))
    )
    if (
        persisted_sources.get("schema") != source_binding["schema"]
        or persisted_sources.get("tier") != source_binding["tier"]
        or persisted_sources_raw != source_binding["raw_artifact_sha256"]
    ):
        raise ValueError("teacher source binding changed before publication")
    for name, snapshot in protocol_artifact_snapshots.items():
        supplied = getattr(args, name)
        current = _read_json_artifact(supplied)
        if current != snapshot:
            raise ValueError(f"teacher {name} changed before publication")
    revalidated = _cycle4_teacher_start(args)
    if (
        revalidated[0] != preregistration
        or revalidated[1] != protocol_binding
        or revalidated[2] != protocol_output
        or revalidated[3] != protocol_artifact_snapshots
        or revalidated[4] is not None
    ):
        raise ValueError("teacher external lineage changed before publication")
    _atomic_exclusive_json(
        protocol_output,
        payload,
        conflict_message="teacher output already exists",
    )
    written_payload, written_raw_sha = _read_json_artifact(protocol_output)
    if written_payload != payload:
        raise ValueError("teacher output changed after publication")
    completion = _teacher_completion_payload(
        protocol_output,
        payload,
        written_raw_sha,
        preregistration,
        protocol_binding,
    )
    _publish_teacher_completion(completion_path, completion)
    path = protocol_output
    quality = payload["quality"]
    summary = {
        "artifact_path": str(path),
        "corpus_id": payload["corpus_id"],
        "status": quality["status"],
        "accepted_roots": quality["accepted_roots"],
        "train_roots": quality["train_roots"],
        "holdout_roots": quality["holdout_roots"],
        "teacher_source_agreement_rate": quality[
            "teacher_source_agreement_rate"
        ],
        "tactical_gate_passed": quality["tactical_gate"]["passed"],
        "label_search_failures": quality["label_search_failures"],
        "cross_tier_forbidden_train_states": len(forbidden_train_state_keys),
        "cross_tier_forbidden_holdout_states": len(forbidden_holdout_state_keys),
        "development_forbidden_holdout_states": len(
            development_forbidden_holdout_state_keys
        ),
        "runtime": payload["runtime"],
    }
    print(json.dumps(summary, sort_keys=True, indent=2))
    if quality["status"] != "complete":
        raise SystemExit(2)


def main() -> None:
    args = parse_args()
    try:
        from scripts.fit_deep_teacher_value import _protocol_stage_lock
    except ModuleNotFoundError:
        from fit_deep_teacher_value import (  # type: ignore[no-redef]
            _protocol_stage_lock,
        )
    with _protocol_stage_lock("build-native-teacher-corpus", exclusive=False):
        _run(args)


if __name__ == "__main__":
    main()
