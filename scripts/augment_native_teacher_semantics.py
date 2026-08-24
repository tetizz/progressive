from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Mapping

from scottish_progressive.corpus_samples import decode_native_boundary_sample
from scottish_progressive.corpus_shards import (
    CorpusStore,
    progressive_state_dedup_key,
)
from scottish_progressive.fast_training import CachedFeatures
from scottish_progressive.model import ProgressiveState
from scottish_progressive.rules import play_series
from scottish_progressive.selfplay_training import write_selfplay_artifact


SEMANTIC_AUGMENTATION_SCHEMA = "spc-native-teacher-semantic-augmentation-v1"


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_root_states(
    store: CorpusStore,
    required_keys: set[str],
) -> dict[str, ProgressiveState]:
    found: dict[str, ProgressiveState] = {}
    for record in store.iter_records():
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
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    labels = payload.get("labels")
    generation = payload.get("generation")
    if not isinstance(labels, list) or not isinstance(generation, Mapping):
        raise ValueError("teacher artifact is malformed")
    train_manifest = train_store.verify()
    holdout_manifest = holdout_store.verify()
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
        "train": _load_root_states(train_store, required["train"]),
        "holdout": _load_root_states(holdout_store, required["holdout"]),
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
        "input_artifact_sha256": input_sha256,
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
        "elapsed_seconds": time.perf_counter() - started,
    }
    return result, receipt


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add replay-verified semantic state provenance to teacher labels."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("train_root", type=Path)
    parser.add_argument("holdout_root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    source = args.input.expanduser().resolve()
    output = args.output.expanduser().resolve()
    receipt_path = args.receipt.expanduser().resolve()
    if source == output:
        raise ValueError("semantic augmentation must not overwrite its source artifact")
    if output.exists() or receipt_path.exists():
        raise FileExistsError("semantic augmentation output or receipt already exists")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("teacher artifact must be a JSON object")
    augmented, receipt = augment_teacher_artifact(
        payload,
        train_store=CorpusStore.open(args.train_root),
        holdout_store=CorpusStore.open(args.holdout_root),
        input_sha256=_file_sha256(source),
    )
    written = write_selfplay_artifact(augmented, output)
    receipt = {**receipt, "output_artifact_sha256": _file_sha256(written)}
    write_selfplay_artifact(receipt, receipt_path)
    print(
        json.dumps(
            {
                "artifact_path": str(written),
                "receipt_path": str(receipt_path),
                **receipt,
            },
            sort_keys=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
