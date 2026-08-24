from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from scottish_progressive.corpus_pipeline import read_native_generation_contract
from scottish_progressive.corpus_samples import decode_native_boundary_sample
from scottish_progressive.corpus_shards import (
    CorpusStore,
    ShardMetadata,
    progressive_state_dedup_key,
)
from scottish_progressive.fast_training import CachedFeatures
from scottish_progressive.model import ProgressiveState
from scottish_progressive.rules import play_series


SEMANTIC_AUGMENTATION_SCHEMA = "spc-native-teacher-semantic-augmentation-v1"


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_root_states(
    store: CorpusStore,
    required_keys: set[str],
    *,
    verified_shards: Sequence[ShardMetadata],
) -> dict[str, ProgressiveState]:
    found: dict[str, ProgressiveState] = {}
    for record in store.iter_snapshot_records(verified_shards):
        key = record.state_key.hex()
        if key not in required_keys or key in found:
            continue
        sample = decode_native_boundary_sample(record.payload)
        regenerated = progressive_state_dedup_key(
            sample.state,
            ruleset_version=store.identity.ruleset_version,
        ).hex()
        if regenerated != key:
            raise ValueError(f"binary corpus state key drifted: {key}")
        found[key] = sample.state
        if len(found) == len(required_keys):
            break
    missing = sorted(required_keys - set(found))
    if missing:
        raise ValueError(
            f"teacher labels reference {len(missing)} missing binary roots; first={missing[0]}"
        )
    return found


def augment_label_semantics(
    label: Mapping[str, Any],
    root: ProgressiveState,
) -> tuple[dict[str, Any], int]:
    root_key = progressive_state_dedup_key(root).hex()
    if label.get("state_key_sha256") != root_key:
        raise ValueError(f"teacher root key does not match binary state: {root_key}")
    if label.get("pfen") != root.pfen:
        raise ValueError(f"teacher root PFEN does not match binary state: {root_key}")
    if label.get("position_hash") != root.position_hash:
        raise ValueError(f"teacher root position hash drifted: {root_key}")
    if label.get("root_features") != CachedFeatures.from_state(root).as_dict():
        raise ValueError(f"teacher root features drifted: {root_key}")
    options = label.get("options")
    if not isinstance(options, list) or not options:
        raise ValueError(f"teacher root has no options: {root_key}")
    augmented_options: list[dict[str, Any]] = []
    replayed_options = 0
    for option in options:
        if not isinstance(option, Mapping):
            raise ValueError(f"teacher root has a malformed option: {root_key}")
        notation = option.get("series")
        if not isinstance(notation, str) or not notation:
            raise ValueError(f"teacher option has no machine series: {root_key}")
        moves = tuple(item.strip() for item in notation.split("/") if item.strip())
        result = play_series(root, moves)
        if result.machine_notation != notation:
            raise ValueError(f"teacher option notation is not canonical: {root_key}/{notation}")
        final_state = result.final_state
        final_key = progressive_state_dedup_key(final_state).hex()
        if option.get("final_state_key_sha256") != final_key:
            raise ValueError(f"teacher option final key drifted: {root_key}/{notation}")
        if option.get("final_pfen") != final_state.pfen:
            raise ValueError(f"teacher option final PFEN drifted: {root_key}/{notation}")
        if option.get("final_features") != CachedFeatures.from_state(final_state).as_dict():
            raise ValueError(f"teacher option final features drifted: {root_key}/{notation}")
        pv = option.get("principal_variation")
        if not isinstance(pv, list) or not pv:
            raise ValueError(f"teacher option has no replay PV: {root_key}/{notation}")
        first = pv[0]
        if (
            not isinstance(first, Mapping)
            or first.get("series") != notation
            or first.get("final_state_key_sha256") != final_key
        ):
            raise ValueError(f"teacher option PV root step drifted: {root_key}/{notation}")
        augmented_options.append(
            {
                **option,
                "final_promoted_bitboard": int(final_state.board.promoted),
                "final_chess960": bool(final_state.board.chess960),
            }
        )
        replayed_options += 1
    return (
        {
            **label,
            "root_promoted_bitboard": int(root.board.promoted),
            "root_chess960": bool(root.board.chess960),
            "options": augmented_options,
        },
        replayed_options,
    )


def augment_teacher_artifact(
    payload: Mapping[str, Any],
    *,
    train_store: CorpusStore,
    holdout_store: CorpusStore,
    input_sha256: str,
    input_semantic_sha256: str | None = None,
    train_snapshot: tuple[dict[str, Any], tuple[ShardMetadata, ...]] | None = None,
    holdout_snapshot: tuple[dict[str, Any], tuple[ShardMetadata, ...]] | None = None,
    preregistration_augmentation_start: Mapping[str, Any] | None = None,
    preregistration_augmentation_source_binding: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    labels = payload.get("labels")
    generation = payload.get("generation")
    if not isinstance(labels, list) or not isinstance(generation, Mapping):
        raise ValueError("teacher artifact is malformed")
    train_snapshot = train_snapshot or train_store.verified_snapshot()
    holdout_snapshot = holdout_snapshot or holdout_store.verified_snapshot()
    train_manifest, train_shards = train_snapshot
    holdout_manifest, holdout_shards = holdout_snapshot
    if generation.get("train_corpus_sha256") != train_manifest["corpus_sha256"]:
        raise ValueError("teacher train corpus binding drifted")
    if generation.get("holdout_corpus_sha256") != holdout_manifest["corpus_sha256"]:
        raise ValueError("teacher holdout corpus binding drifted")
    required: dict[str, set[str]] = {"train": set(), "holdout": set()}
    for label in labels:
        if not isinstance(label, Mapping) or label.get("split") not in required:
            raise ValueError("teacher label split is malformed")
        required[str(label["split"])].add(str(label["state_key_sha256"]))
    roots = {
        "train": _load_root_states(
            train_store, required["train"], verified_shards=train_shards
        ),
        "holdout": _load_root_states(
            holdout_store, required["holdout"], verified_shards=holdout_shards
        ),
    }
    augmented_labels: list[dict[str, Any]] = []
    option_count = 0
    promoted_roots = 0
    promoted_option_states = 0
    for label in labels:
        split = str(label["split"])
        key = str(label["state_key_sha256"])
        augmented, replayed = augment_label_semantics(label, roots[split][key])
        augmented_labels.append(augmented)
        option_count += replayed
        promoted_roots += int(augmented["root_promoted_bitboard"] != 0)
        promoted_option_states += sum(
            int(option["final_promoted_bitboard"] != 0)
            for option in augmented["options"]
        )
    contract = {
        "schema": SEMANTIC_AUGMENTATION_SCHEMA,
        "input_raw_artifact_sha256": input_sha256,
        "input_semantic_sha256": input_semantic_sha256,
        "train_corpus_sha256": train_manifest["corpus_sha256"],
        "holdout_corpus_sha256": holdout_manifest["corpus_sha256"],
        "labels_replayed": len(augmented_labels),
        "options_replayed": option_count,
        "promoted_roots": promoted_roots,
        "promoted_option_final_states": promoted_option_states,
        "root_fields": ["root_promoted_bitboard", "root_chess960"],
        "option_fields": ["final_promoted_bitboard", "final_chess960"],
        "key_algorithm": "progressive_state_dedup_key-v1-sha256-hex",
        "pfen_is_not_semantically_complete": True,
        "all_root_keys_replayed": True,
        "all_option_keys_replayed": True,
        "all_cached_features_regenerated": True,
    }
    deterministic = {
        key: value
        for key, value in payload.items()
        if key not in {"corpus_id", "runtime"}
    }
    if preregistration_augmentation_start is not None:
        source_provenance = generation.get("preregistration_generation_provenance")
        if not isinstance(source_provenance, Mapping):
            raise ValueError("teacher preregistration provenance is missing")
        augmented_provenance = {
            **dict(source_provenance),
            "teacher_semantic_augmentation_start": dict(
                preregistration_augmentation_start
            ),
        }
        if preregistration_augmentation_source_binding is None:
            raise ValueError("semantic augmentation source binding is missing")
        augmented_provenance["teacher_semantic_augmentation_source_binding"] = dict(
            preregistration_augmentation_source_binding
        )
        deterministic["generation"] = {
            **dict(generation),
            "preregistration_generation_provenance": augmented_provenance,
        }
    deterministic["labels"] = augmented_labels
    deterministic["semantic_state_contract"] = contract
    source_corpus_id = payload.get("corpus_id")
    augmented_id = "spc-native-teacher-semantic-" + hashlib.sha256(
        _canonical_json(deterministic)
    ).hexdigest()[:20]
    result = {
        **deterministic,
        "source_corpus_id": source_corpus_id,
        "corpus_id": augmented_id,
        "runtime": payload.get("runtime"),
    }
    receipt = {
        **contract,
        "source_corpus_id": source_corpus_id,
        "augmented_corpus_id": augmented_id,
    }
    if train_store.verified_snapshot() != train_snapshot:
        raise ValueError("train corpus changed during semantic augmentation")
    if holdout_store.verified_snapshot() != holdout_snapshot:
        raise ValueError("holdout corpus changed during semantic augmentation")
    return result, receipt


def _main_locked() -> None:
    parser = argparse.ArgumentParser(
        description="Add replay-verified semantic state provenance to teacher labels."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("train_root", type=Path)
    parser.add_argument("holdout_root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--preregistration", type=Path, required=True)
    parser.add_argument(
        "--tier",
        choices=("quiet_depth2", "tactical_depth3"),
        required=True,
    )
    args = parser.parse_args()
    try:
        from scripts.fit_deep_teacher_value import (
            _atomic_exclusive_json,
            _expected_generation_contract_sha256,
            _load_preregistration,
            _pretty_json_bytes,
            _read_json_artifact,
            _require_protocol_registry_isolation,
            _teacher_semantic_sha256,
            _validate_raw_teacher_publication,
            _validate_preregistered_teacher_tier_artifact,
        )
    except ModuleNotFoundError:
        from fit_deep_teacher_value import (  # type: ignore[no-redef]
            _atomic_exclusive_json,
            _expected_generation_contract_sha256,
            _load_preregistration,
            _pretty_json_bytes,
            _read_json_artifact,
            _require_protocol_registry_isolation,
            _teacher_semantic_sha256,
            _validate_raw_teacher_publication,
            _validate_preregistered_teacher_tier_artifact,
        )

    source = args.input.expanduser().resolve()
    train_root = args.train_root.expanduser().resolve()
    holdout_root = args.holdout_root.expanduser().resolve()
    output = args.output.expanduser().resolve()
    receipt_path = args.receipt.expanduser().resolve()
    canonical_inputs = {
        "input": (args.input, source),
        "train root": (args.train_root, train_root),
        "holdout root": (args.holdout_root, holdout_root),
        "output": (args.output, output),
        "receipt": (args.receipt, receipt_path),
    }
    if any(str(supplied) != str(canonical) for supplied, canonical in canonical_inputs.values()):
        raise ValueError("semantic augmentation paths must be absolute and canonical")
    start_path = output.with_name(output.name + ".preregistration-start.json")
    source_binding_path = output.with_name(
        output.name + ".preregistration-sources.json"
    )
    protocol_paths = {
        "input": source,
        "train root": train_root,
        "sealed holdout root": holdout_root,
        "output": output,
        "receipt": receipt_path,
        "start": start_path,
        "source binding": source_binding_path,
    }
    path_items = list(protocol_paths.items())
    for index, (name, path) in enumerate(path_items):
        for other_name, other in path_items[index + 1 :]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError(
                    "semantic augmentation paths must be distinct and non-nested: "
                    f"{name}, {other_name}"
                )
    for name, path in (
        ("input", source),
        ("output", output),
        ("receipt", receipt_path),
        ("start", start_path),
        ("source binding", source_binding_path),
    ):
        if path.is_dir():
            raise ValueError(f"semantic augmentation {name} must be a file path")
    preregistration = _load_preregistration(
        args.preregistration, forbid_pair_preparation=True
    )
    _require_protocol_registry_isolation(
        preregistration,
        protocol_paths,
        label="augment-native-teacher-semantics",
    )
    start = {
        "schema": "spc-cycle4-teacher-semantic-augmentation-start-v1",
        "preregistration": {
            "schema": preregistration.schema,
            "raw_artifact_sha256": preregistration.sha256,
        },
        "tier": args.tier,
        "input": str(source),
        "train_root": str(train_root),
        "sealed_holdout_root": str(holdout_root),
        "output": str(output),
        "receipt": str(receipt_path),
        "source_binding": str(source_binding_path),
    }
    start_preexisted = start_path.exists()
    if start_preexisted:
        persisted_start, _ = _read_json_artifact(start_path)
        if persisted_start != start:
            raise ValueError("existing semantic augmentation-start binding differs")
    else:
        if output.exists() or receipt_path.exists() or source_binding_path.exists():
            raise FileExistsError(
                "semantic augmentation has unbound preexisting output artifacts"
            )
        _atomic_exclusive_json(
            start_path,
            start,
            conflict_message="semantic augmentation was already started",
        )
    persisted_start, start_raw_sha256 = _read_json_artifact(start_path)
    if persisted_start != start:
        raise ValueError("semantic augmentation-start changed after publication")
    if start_preexisted and not source_binding_path.exists():
        raise FileExistsError(
            "semantic augmentation stopped after its input may have been opened but "
            "before its byte binding was durable; this start cannot be resumed"
        )
    start_evidence = {
        "schema": start["schema"],
        "tier": args.tier,
        "path": str(start_path),
        "raw_artifact_sha256": start_raw_sha256,
    }
    payload, input_raw_sha256 = _read_json_artifact(source)
    initial_source_lineage = _validate_raw_teacher_publication(
        payload,
        preregistration,
        tier_name=args.tier,
        supplied_path=source,
        supplied_raw_sha256=input_raw_sha256,
    )
    if initial_source_lineage["trajectory_roots"] != {
        "train": str(train_root),
        "sealed_holdout": str(holdout_root),
    }:
        raise ValueError(
            "semantic augmentation trajectory roots differ from the raw teacher start"
        )
    input_semantic_sha256 = _teacher_semantic_sha256(payload)
    stores = {
        "train": CorpusStore.open(train_root),
        "sealed_holdout": CorpusStore.open(holdout_root),
    }
    snapshots = {name: store.verified_snapshot() for name, store in stores.items()}
    generation = payload["generation"]
    provenance = generation["preregistration_generation_provenance"]
    trajectory_starts = provenance["trajectory_generation_starts"]
    trajectory_binding: dict[str, Any] = {}
    for split, root in (("train", train_root), ("sealed_holdout", holdout_root)):
        corpus = snapshots[split][0]
        expected_contract = _expected_generation_contract_sha256(
            preregistration, split=split
        )
        if (
            read_native_generation_contract(root).digest_hex != expected_contract
            or trajectory_starts[split]["corpus"] != corpus
            or generation[
                "train_corpus_sha256" if split == "train" else "holdout_corpus_sha256"
            ]
            != corpus["corpus_sha256"]
        ):
            raise ValueError(f"{split} semantic augmentation corpus binding differs")
        trajectory_binding[split] = {
            "root": str(root),
            "generation_contract_sha256": expected_contract,
            "corpus": dict(corpus),
            "generation_start": dict(trajectory_starts[split]),
        }
    source_binding = {
        "schema": "spc-cycle4-teacher-semantic-augmentation-sources-v1",
        "preregistration": {
            "schema": preregistration.schema,
            "raw_artifact_sha256": preregistration.sha256,
        },
        "tier": args.tier,
        "augmentation_start": start_evidence,
        "input": {
            "path": str(source),
            "corpus_id": payload.get("corpus_id"),
            "semantic_sha256": input_semantic_sha256,
            "raw_artifact_sha256": input_raw_sha256,
        },
        "trajectory_corpora": trajectory_binding,
        "output": str(output),
        "receipt": str(receipt_path),
    }
    if source_binding_path.exists():
        persisted_sources, _ = _read_json_artifact(source_binding_path)
        if persisted_sources != source_binding:
            raise ValueError("existing semantic augmentation source binding differs")
    else:
        if output.exists() or receipt_path.exists():
            raise FileExistsError(
                "semantic augmentation output/receipt exists without source binding"
            )
        _atomic_exclusive_json(
            source_binding_path,
            source_binding,
            conflict_message="semantic augmentation sources were already bound",
        )
    persisted_sources, source_binding_raw_sha256 = _read_json_artifact(
        source_binding_path
    )
    if persisted_sources != source_binding:
        raise ValueError("semantic augmentation source binding changed")
    source_binding_evidence = {
        "schema": source_binding["schema"],
        "tier": args.tier,
        "path": str(source_binding_path),
        "raw_artifact_sha256": source_binding_raw_sha256,
    }
    augmented, receipt = augment_teacher_artifact(
        payload,
        train_store=stores["train"],
        holdout_store=stores["sealed_holdout"],
        input_sha256=input_raw_sha256,
        input_semantic_sha256=input_semantic_sha256,
        train_snapshot=snapshots["train"],
        holdout_snapshot=snapshots["sealed_holdout"],
        preregistration_augmentation_start=start_evidence,
        preregistration_augmentation_source_binding=source_binding_evidence,
    )
    _validate_preregistered_teacher_tier_artifact(
        augmented,
        preregistration,
        tier_name=args.tier,
        require_semantic_augmentation=True,
    )
    current_source, current_source_sha = _read_json_artifact(source)
    if current_source != payload or current_source_sha != input_raw_sha256:
        raise ValueError("semantic augmentation input changed before publication")
    current_source_lineage = _validate_raw_teacher_publication(
        current_source,
        preregistration,
        tier_name=args.tier,
        supplied_path=source,
        supplied_raw_sha256=current_source_sha,
    )
    final_source, final_source_sha = _read_json_artifact(source)
    current_start, current_start_raw = _read_json_artifact(start_path)
    current_sources, current_sources_raw = _read_json_artifact(source_binding_path)
    if (
        current_source_lineage != initial_source_lineage
        or final_source != payload
        or final_source_sha != input_raw_sha256
        or current_start != start
        or current_start_raw != start_raw_sha256
        or current_sources != source_binding
        or current_sources_raw != source_binding_raw_sha256
        or any(
            stores[split].verified_snapshot() != snapshots[split]
            for split in ("train", "sealed_holdout")
        )
    ):
        raise ValueError("semantic augmentation lineage changed before publication")
    output_raw_sha256 = hashlib.sha256(_pretty_json_bytes(augmented)).hexdigest()
    output_semantic_sha256 = _teacher_semantic_sha256(augmented)
    completion = {
        "schema": "spc-cycle4-teacher-semantic-augmentation-receipt-v1",
        "preregistration": start["preregistration"],
        "tier": args.tier,
        "augmentation_start": start_evidence,
        "augmentation_source_binding": source_binding_evidence,
        "input": source_binding["input"],
        "trajectory_corpora": source_binding["trajectory_corpora"],
        "output": {
            "path": str(output),
            "corpus_id": augmented["corpus_id"],
            "semantic_sha256": output_semantic_sha256,
            "raw_artifact_sha256": output_raw_sha256,
        },
        "semantic_replay": receipt,
    }
    if receipt_path.exists() and not output.exists():
        raise FileExistsError(
            "semantic augmentation receipt exists without its output"
        )
    if output.exists():
        existing, existing_raw_sha = _read_json_artifact(output)
        if existing != augmented or existing_raw_sha != output_raw_sha256:
            raise FileExistsError("semantic augmentation output already differs")
    else:
        _atomic_exclusive_json(
            output,
            augmented,
            conflict_message="semantic augmentation output already exists",
        )
    if receipt_path.exists():
        existing_receipt, _ = _read_json_artifact(receipt_path)
        if existing_receipt != completion:
            raise FileExistsError("semantic augmentation receipt already differs")
    else:
        _atomic_exclusive_json(
            receipt_path,
            completion,
            conflict_message="semantic augmentation receipt already exists",
        )
    written, written_raw_sha = _read_json_artifact(output)
    written_receipt, receipt_raw_sha = _read_json_artifact(receipt_path)
    if (
        written != augmented
        or written_raw_sha != output_raw_sha256
        or written_receipt != completion
    ):
        raise ValueError("semantic augmentation publication changed")
    print(
        json.dumps(
            {
                "artifact_path": str(output),
                "receipt_path": str(receipt_path),
                "receipt_raw_artifact_sha256": receipt_raw_sha,
                **completion["output"],
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
    with _protocol_stage_lock(
        "augment-native-teacher-semantics", exclusive=False
    ):
        _main_locked()


if __name__ == "__main__":
    main()
