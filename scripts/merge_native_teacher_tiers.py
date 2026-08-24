from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scottish_progressive.native_teacher import (
    NativeTeacherConfig,
    merge_native_teacher_tiers,
)


def _main_locked() -> None:
    parser = argparse.ArgumentParser(
        description="Merge the fixed quiet-D2 and tactical-D3 teacher tiers."
    )
    parser.add_argument("quiet_depth2", type=Path)
    parser.add_argument("tactical_depth3", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--preregistration", type=Path, required=True)
    args = parser.parse_args()

    try:
        from scripts.fit_deep_teacher_value import (
            _atomic_exclusive_json,
            _load_preregistration,
            _pretty_json_bytes,
            _read_json_artifact,
            _require_protocol_registry_isolation,
            _teacher_semantic_sha256,
            _validate_augmented_teacher_publication,
            _validate_combined_corpus_preregistration,
        )
    except ModuleNotFoundError:
        from fit_deep_teacher_value import (  # type: ignore[no-redef]
            _atomic_exclusive_json,
            _load_preregistration,
            _pretty_json_bytes,
            _read_json_artifact,
            _require_protocol_registry_isolation,
            _teacher_semantic_sha256,
            _validate_augmented_teacher_publication,
            _validate_combined_corpus_preregistration,
        )

    preregistration = _load_preregistration(
        args.preregistration, forbid_pair_preparation=True
    )
    paths = {
        "quiet_depth2": args.quiet_depth2.expanduser().resolve(),
        "tactical_depth3": args.tactical_depth3.expanduser().resolve(),
    }
    if any(str(getattr(args, name)) != str(path) for name, path in paths.items()):
        raise ValueError("teacher tier paths must be absolute and canonical")
    output = args.output.expanduser().resolve()
    if str(args.output) != str(output):
        raise ValueError("merged teacher output must be absolute and canonical")
    start_path = output.with_name(output.name + ".preregistration-start.json")
    source_binding_path = output.with_name(
        output.name + ".preregistration-sources.json"
    )
    completion_path = output.with_name(
        output.name + ".preregistration-completion.json"
    )
    protocol_paths = {
        **paths,
        "output": output,
        "start": start_path,
        "source binding": source_binding_path,
        "completion": completion_path,
    }
    path_items = list(protocol_paths.items())
    for index, (name, path) in enumerate(path_items):
        for other_name, other in path_items[index + 1 :]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError(
                    "teacher merge paths must be distinct and non-nested: "
                    f"{name}, {other_name}"
                )
    for name, path in protocol_paths.items():
        if path.is_dir():
            raise ValueError(f"teacher merge {name} must be a file path")
    _require_protocol_registry_isolation(
        preregistration,
        protocol_paths,
        label="merge-native-teacher-tiers",
    )
    start = {
        "schema": "spc-cycle4-teacher-merge-start-v1",
        "preregistration": {
            "schema": preregistration.schema,
            "raw_artifact_sha256": preregistration.sha256,
        },
        "quiet_depth2": str(paths["quiet_depth2"]),
        "tactical_depth3": str(paths["tactical_depth3"]),
        "output": str(output),
        "source_binding": str(source_binding_path),
    }
    start_preexisted = start_path.exists()
    if start_preexisted:
        existing, _ = _read_json_artifact(start_path)
        if existing != start:
            raise ValueError("existing teacher merge-start binding differs")
    else:
        if output.exists() or source_binding_path.exists() or completion_path.exists():
            raise FileExistsError("teacher merge has unbound preexisting artifacts")
        _atomic_exclusive_json(
            start_path,
            start,
            conflict_message="cycle-4 teacher merge was already started",
        )
    persisted_start, start_raw_sha256 = _read_json_artifact(start_path)
    if persisted_start != start:
        raise ValueError("teacher merge-start changed after publication")
    if start_preexisted and not source_binding_path.exists():
        raise FileExistsError(
            "teacher merge stopped after a tier may have been opened but before its "
            "byte binding was durable; this start cannot be resumed"
        )
    start_evidence = {
        "schema": start["schema"],
        "path": str(start_path),
        "raw_artifact_sha256": start_raw_sha256,
    }
    tier_snapshots: dict[str, tuple[dict[str, object], str]] = {}
    tier_inputs: dict[str, dict[str, object]] = {}
    tier_lineage: dict[str, dict[str, object]] = {}
    for tier_name, path in paths.items():
        payload, raw_sha256 = _read_json_artifact(path)
        tier_lineage[tier_name] = _validate_augmented_teacher_publication(
            payload,
            preregistration,
            tier_name=tier_name,
            supplied_path=path,
            supplied_raw_sha256=raw_sha256,
        )
        provenance = payload["generation"][
            "preregistration_generation_provenance"
        ]
        augmentation_start_evidence = provenance[
            "teacher_semantic_augmentation_start"
        ]
        augmentation_source_evidence = provenance[
            "teacher_semantic_augmentation_source_binding"
        ]
        augmentation_start_path = Path(augmentation_start_evidence["path"])
        augmentation_start, augmentation_start_raw = _read_json_artifact(
            augmentation_start_path
        )
        if (
            augmentation_start_raw
            != augmentation_start_evidence["raw_artifact_sha256"]
            or augmentation_start.get("tier") != tier_name
            or augmentation_start.get("output") != str(path)
            or augmentation_start.get("preregistration")
            != {
                "schema": preregistration.schema,
                "raw_artifact_sha256": preregistration.sha256,
            }
        ):
            raise ValueError(f"{tier_name} augmentation-start binding differs")
        augmentation_source_path = Path(augmentation_source_evidence["path"])
        augmentation_sources, augmentation_sources_raw = _read_json_artifact(
            augmentation_source_path
        )
        if (
            augmentation_sources_raw
            != augmentation_source_evidence["raw_artifact_sha256"]
            or augmentation_sources.get("schema")
            != "spc-cycle4-teacher-semantic-augmentation-sources-v1"
            or augmentation_sources.get("tier") != tier_name
            or augmentation_sources.get("augmentation_start")
            != augmentation_start_evidence
            or augmentation_sources.get("output") != str(path)
            or augmentation_start.get("source_binding")
            != str(augmentation_source_path)
        ):
            raise ValueError(f"{tier_name} augmentation source binding differs")
        receipt_path = Path(str(augmentation_start.get("receipt")))
        if (
            not receipt_path.is_absolute()
            or str(receipt_path.expanduser().resolve()) != str(receipt_path)
        ):
            raise ValueError(f"{tier_name} augmentation receipt path is not canonical")
        augmentation_receipt, augmentation_receipt_raw = _read_json_artifact(
            receipt_path
        )
        if (
            tier_lineage[tier_name]["augmentation_receipt_path"]
            != str(receipt_path)
            or tier_lineage[tier_name][
                "augmentation_receipt_raw_artifact_sha256"
            ]
            != augmentation_receipt_raw
        ):
            raise ValueError(f"{tier_name} augmentation receipt snapshot differs")
        semantic_sha256 = _teacher_semantic_sha256(payload)
        semantic_contract = payload.get("semantic_state_contract")
        source_input = augmentation_sources.get("input")
        expected_semantic_replay = (
            {
                **dict(semantic_contract),
                "source_corpus_id": payload.get("source_corpus_id"),
                "augmented_corpus_id": payload.get("corpus_id"),
            }
            if isinstance(semantic_contract, dict)
            else None
        )
        expected_output = {
            "path": str(path),
            "corpus_id": payload.get("corpus_id"),
            "semantic_sha256": semantic_sha256,
            "raw_artifact_sha256": raw_sha256,
        }
        if (
            augmentation_receipt.get("schema")
            != "spc-cycle4-teacher-semantic-augmentation-receipt-v1"
            or augmentation_receipt.get("preregistration")
            != augmentation_start["preregistration"]
            or augmentation_receipt.get("tier") != tier_name
            or augmentation_receipt.get("augmentation_start")
            != augmentation_start_evidence
            or augmentation_receipt.get("augmentation_source_binding")
            != augmentation_source_evidence
            or augmentation_receipt.get("input") != augmentation_sources.get("input")
            or augmentation_receipt.get("trajectory_corpora")
            != augmentation_sources.get("trajectory_corpora")
            or augmentation_receipt.get("output") != expected_output
            or augmentation_receipt.get("semantic_replay")
            != expected_semantic_replay
            or not isinstance(semantic_contract, dict)
            or not isinstance(source_input, dict)
            or semantic_contract.get("input_raw_artifact_sha256")
            != source_input.get("raw_artifact_sha256")
            or semantic_contract.get("input_semantic_sha256")
            != source_input.get("semantic_sha256")
            or payload.get("source_corpus_id") != source_input.get("corpus_id")
        ):
            raise ValueError(f"{tier_name} augmentation receipt differs")
        tier_snapshots[tier_name] = (payload, raw_sha256)
        tier_inputs[tier_name] = {
            **expected_output,
            "augmentation_start_path": str(augmentation_start_path),
            "augmentation_start_raw_artifact_sha256": augmentation_start_raw,
            "augmentation_source_binding_path": str(augmentation_source_path),
            "augmentation_source_binding_raw_artifact_sha256": (
                augmentation_sources_raw
            ),
            "augmentation_receipt_path": str(receipt_path),
            "augmentation_receipt_raw_artifact_sha256": augmentation_receipt_raw,
        }
    quiet_input = tier_inputs["quiet_depth2"]
    expected_cross_tier = {
        name: quiet_input[name]
        for name in (
            "path",
            "corpus_id",
            "semantic_sha256",
            "raw_artifact_sha256",
        )
    }
    if (
        tier_lineage["tactical_depth3"]["raw_teacher_input_artifacts"][
            "cross_tier_artifact"
        ]
        != expected_cross_tier
    ):
        raise ValueError(
            "tactical_depth3 cross-tier input differs from merged quiet tier"
        )
    if (
        tier_lineage["quiet_depth2"]["raw_teacher_input_artifacts"][
            "development_exclusion_artifact"
        ]
        != tier_lineage["tactical_depth3"]["raw_teacher_input_artifacts"][
            "development_exclusion_artifact"
        ]
    ):
        raise ValueError("teacher tiers use different development exclusions")
    source_binding = {
        "schema": "spc-cycle4-teacher-merge-sources-v1",
        "preregistration": start["preregistration"],
        "merge_start": start_evidence,
        "tier_inputs": tier_inputs,
    }
    if source_binding_path.exists():
        existing_sources, _ = _read_json_artifact(source_binding_path)
        if existing_sources != source_binding:
            raise ValueError("existing teacher merge source binding differs")
    else:
        if output.exists() or completion_path.exists():
            raise FileExistsError(
                "teacher merge output/completion exists without source binding"
            )
        _atomic_exclusive_json(
            source_binding_path,
            source_binding,
            conflict_message="cycle-4 teacher merge sources were already bound",
        )
    persisted_sources, source_binding_raw_sha256 = _read_json_artifact(
        source_binding_path
    )
    if persisted_sources != source_binding:
        raise ValueError("teacher merge source binding changed after publication")
    source_binding_evidence = {
        "schema": source_binding["schema"],
        "path": str(source_binding_path),
        "raw_artifact_sha256": source_binding_raw_sha256,
    }

    quiet = tier_snapshots["quiet_depth2"][0]
    tactical = tier_snapshots["tactical_depth3"][0]
    teacher = preregistration.manifest["teacher"]
    trajectory = preregistration.manifest["trajectory_corpora"]
    common = {
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
    quiet_tier = teacher["tiers"]["quiet_depth2"]
    tactical_tier = teacher["tiers"]["tactical_depth3"]

    payload = merge_native_teacher_tiers(
        quiet,
        tactical,
        quiet_config=NativeTeacherConfig(
            **common,
            target_roots=quiet_tier["target_roots"],
            train_roots=quiet_tier["train_roots"],
            depth_series=2,
            selection_mode=quiet_tier["selection_mode"],
        ),
        tactical_config=NativeTeacherConfig(
            **common,
            target_roots=tactical_tier["target_roots"],
            train_roots=tactical_tier["train_roots"],
            depth_series=3,
            selection_mode=tactical_tier["selection_mode"],
        ),
        merge_generation_start=start_evidence,
        merge_generation_source_binding=source_binding_evidence,
    )
    _validate_combined_corpus_preregistration(payload, preregistration)
    current_tier_snapshots: dict[str, tuple[dict[str, object], str]] = {}
    for tier_name, path in paths.items():
        current, current_raw = _read_json_artifact(path)
        if (current, current_raw) != tier_snapshots[tier_name]:
            raise ValueError(f"{tier_name} changed during teacher merge")
        current_tier_snapshots[tier_name] = (current, current_raw)
    current_start, current_start_raw = _read_json_artifact(start_path)
    current_sources, current_sources_raw = _read_json_artifact(source_binding_path)
    if (
        current_start != start
        or current_start_raw != start_raw_sha256
        or current_sources != source_binding
        or current_sources_raw != source_binding_raw_sha256
    ):
        raise ValueError("teacher merge bindings changed before publication")
    for tier_name, path in paths.items():
        current_payload, current_raw_sha = current_tier_snapshots[tier_name]
        current_lineage = _validate_augmented_teacher_publication(
            current_payload,
            preregistration,
            tier_name=tier_name,
            supplied_path=path,
            supplied_raw_sha256=current_raw_sha,
        )
        if current_lineage != tier_lineage[tier_name]:
            raise ValueError(f"{tier_name} lineage changed before teacher merge")
        if (
            current_lineage["augmentation_receipt_path"]
            != tier_inputs[tier_name]["augmentation_receipt_path"]
            or current_lineage["augmentation_receipt_raw_artifact_sha256"]
            != tier_inputs[tier_name][
                "augmentation_receipt_raw_artifact_sha256"
            ]
        ):
            raise ValueError(f"{tier_name} augmentation receipt bytes changed")
    for tier_name, path in paths.items():
        final_tier, final_tier_raw = _read_json_artifact(path)
        if (final_tier, final_tier_raw) != tier_snapshots[tier_name]:
            raise ValueError(f"{tier_name} changed before teacher merge publication")
    final_start, final_start_raw = _read_json_artifact(start_path)
    final_sources, final_sources_raw = _read_json_artifact(source_binding_path)
    if (
        final_start != start
        or final_start_raw != start_raw_sha256
        or final_sources != source_binding
        or final_sources_raw != source_binding_raw_sha256
    ):
        raise ValueError("teacher merge bindings changed before publication")
    expected_output_raw = hashlib.sha256(_pretty_json_bytes(payload)).hexdigest()
    completion = {
        "schema": "spc-cycle4-teacher-merge-completion-v1",
        "preregistration": start["preregistration"],
        "merge_start": start_evidence,
        "merge_source_binding": source_binding_evidence,
        "output": {
            "path": str(output),
            "corpus_id": payload["corpus_id"],
            "semantic_sha256": _teacher_semantic_sha256(payload),
            "raw_artifact_sha256": expected_output_raw,
        },
    }
    if completion_path.exists() and not output.exists():
        raise FileExistsError("teacher merge completion exists without its output")
    if output.exists():
        existing, existing_raw = _read_json_artifact(output)
        if existing != payload or existing_raw != expected_output_raw:
            raise FileExistsError("merged teacher output already differs")
    else:
        _atomic_exclusive_json(
            output,
            payload,
            conflict_message="merged teacher output already exists",
        )
    if completion_path.exists():
        existing_completion, _ = _read_json_artifact(completion_path)
        if existing_completion != completion:
            raise FileExistsError("teacher merge completion already differs")
    else:
        _atomic_exclusive_json(
            completion_path,
            completion,
            conflict_message="teacher merge completion already exists",
        )
    path = output
    print(
        json.dumps(
            {
                "artifact_path": str(path),
                "completion_receipt_path": str(completion_path),
                "corpus_id": payload["corpus_id"],
                "accepted_roots": payload["quality"]["accepted_roots"],
                "train_roots": payload["quality"]["train_roots"],
                "holdout_roots": payload["quality"]["holdout_roots"],
                "tier_metrics": payload["quality"]["tier_metrics"],
                "selection": payload["selection"],
            },
            sort_keys=True,
            indent=2,
        )
    )


def main() -> None:
    try:
        from scripts.fit_deep_teacher_value import _protocol_stage_lock
    except ModuleNotFoundError:
        from fit_deep_teacher_value import (  # type: ignore[no-redef]
            _protocol_stage_lock,
        )
    with _protocol_stage_lock("merge-native-teacher-tiers", exclusive=False):
        _main_locked()


if __name__ == "__main__":
    main()
