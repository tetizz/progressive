from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
import heapq
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping

from scottish_progressive import evaluation as evaluation_module
from scottish_progressive.fullgame import (
    FullGameSemanticConfig,
    iter_fullgame_records,
    verify_fullgame_run,
)
from scottish_progressive.fullgame_codec import FullGameRecord
from scottish_progressive.fullgame_codec import (
    NATIVE_V2_SCHEDULE_ORDERED_PAIR_ROUND_ROBIN,
    NATIVE_V2_SCHEDULE_SELF_ROUND_ROBIN,
    chunk_sha256,
    decode_chunk,
    expected_v2_profile_pair,
    replay_record,
)
from scottish_progressive.fast_training import CachedFeatures
from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT, ProgressiveState
from scottish_progressive.neural_evaluator import (
    NeuralBlend,
    NeuralDataset,
    NeuralSample,
    NeuralTrainerConfig,
    _state_from_pfen,
    attach_teacher_result,
    build_neural_dataset,
    build_neural_dataset_from_weak_corpus,
    extract_active_features,
    load_dataset,
    load_network,
    sample_from_teacher_result,
    save_dataset,
    save_network,
    train_fixed_point_network,
)
from scottish_progressive.profiles import EngineProfile, baseline_profile, load_profile
from scottish_progressive.rules import play_series
from scottish_progressive.search import SearchLimits, analyze
from scottish_progressive.selfplay_training import (
    SelfPlayCorpus,
    build_fullgame_corpus,
    evaluate_human_refutation_gate,
    write_selfplay_artifact,
)
from scottish_progressive.league import (
    HUMAN_FIRST_GAME_CONTENDER_HYPOTHESES,
    HUMAN_FIRST_GAME_REFUTATION,
    run_rules_tactical_gate,
)
from scottish_progressive.strength import (
    StrengthMatchConfig,
    StrengthParticipant,
    build_seeded_opening_suite,
    run_strength_match,
    write_strength_report,
)


TEACHER_FORMAT = "spc-neural-teacher-run-v2"
TEACHER_BATCH_SIZE = 16
DEFAULT_REQUIRED_STORE_GAMES = 1_000_000
DEFAULT_DATASET_GAMES = 100_000
DEFAULT_DATASET_SAMPLES = 200_000
DEFAULT_DATASET_ARTIFACT_MB = 512
NEURAL_GATE_FORMAT = "spc-neural-gates-v2"
FROZEN_RECEIPT_FORMAT = "spc-frozen-fullgame-verification-receipt-v1"
_STORE_STREAM_DOMAIN = b"SPC-FROZEN-FULLGAME-STORE-V1\0"
MANDATORY_TEACHER_GROUP = "human-first-game-root-and-contenders-v1"


def _profile(reference: str) -> EngineProfile:
    return baseline_profile() if reference.lower() == "baseline" else load_profile(reference)


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _store_catalog(root: Path) -> dict[str, Any]:
    manifest = root / "manifest.json"
    checkpoint = root / "checkpoint.sqlite3"
    chunks = root / "chunks"
    if not manifest.is_file() or not checkpoint.is_file() or not chunks.is_dir():
        raise ValueError("full-game store is missing manifest, checkpoint, or chunks")
    forbidden = tuple(chunks.glob("*.pending"))
    if forbidden:
        raise ValueError("full-game store has pending chunk files")
    wal = root / "checkpoint.sqlite3-wal"
    paths = (
        manifest,
        checkpoint,
        *((wal,) if wal.is_file() else ()),
        *sorted(chunks.glob("*.spcg")),
    )
    entries: list[dict[str, Any]] = []
    stream = hashlib.sha256(_STORE_STREAM_DOMAIN)
    for path in paths:
        relative = path.relative_to(root).as_posix()
        encoded = relative.encode("utf-8")
        stream.update(len(encoded).to_bytes(8, "big"))
        stream.update(encoded)
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                size += len(block)
                digest.update(block)
                stream.update(block)
        entries.append({"path": relative, "sha256": digest.hexdigest(), "size": size})
    return {
        "files": entries,
        "catalog_sha256": hashlib.sha256(
            _canonical({"files": entries}).encode("ascii")
        ).hexdigest(),
        "stream_sha256": stream.hexdigest(),
        "manifest_sha256": entries[0]["sha256"],
        "checkpoint_sha256": entries[1]["sha256"],
        "checkpoint_wal_sha256": next(
            (
                item["sha256"]
                for item in entries
                if item["path"] == "checkpoint.sqlite3-wal"
            ),
            None,
        ),
        "chunk_count": sum(item["path"].startswith("chunks/") for item in entries),
    }


def _load_frozen_receipt(
    root: str | Path,
    receipt_source: str | Path,
    *,
    expected_snapshot_manifest_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    base = Path(root).expanduser().resolve()
    receipt_path = Path(receipt_source).expanduser().resolve()
    try:
        raw = receipt_path.read_bytes()
        receipt = json.loads(raw.decode("ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load frozen verification receipt: {error}") from error
    if not isinstance(receipt, dict) or receipt.get("format") != FROZEN_RECEIPT_FORMAT:
        raise ValueError("unsupported frozen verification receipt")
    if raw != _canonical(receipt).encode("ascii") + b"\n":
        raise ValueError("frozen verification receipt is not canonical")
    if Path(str(receipt.get("store_root", ""))).resolve() != base:
        raise ValueError("frozen verification receipt belongs to another store")
    snapshot_root = Path(str(receipt.get("snapshot_root", ""))).resolve()
    if receipt_path == base or base in receipt_path.parents:
        raise ValueError("frozen verification receipt must be outside the full-game store")
    if receipt_path == snapshot_root or snapshot_root in receipt_path.parents:
        raise ValueError("frozen verification receipt must be outside the immutable snapshot")
    if (
        len(expected_snapshot_manifest_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_snapshot_manifest_sha256)
        or receipt.get("snapshot_manifest_sha256") != expected_snapshot_manifest_sha256
    ):
        raise ValueError("frozen snapshot manifest SHA-256 is not the pinned value")
    generator = Path(__file__).resolve().with_name("frozen_store_receipt.py")
    generator_sha256 = _file_sha256(generator)
    if receipt.get("receipt_generator_sha256") != generator_sha256:
        raise ValueError("frozen verification receipt generator identity differs")
    with tempfile.TemporaryDirectory(prefix="spc-frozen-receipt-recheck-") as temporary:
        regenerated = Path(temporary) / "receipt.json"
        completed = subprocess.run(
            (
                sys.executable,
                str(generator),
                str(base),
                str(snapshot_root),
                str(regenerated),
            ),
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(
                "frozen official verification recheck failed: "
                + completed.stderr.strip()
            )
        if regenerated.read_bytes() != raw:
            raise ValueError("detached frozen receipt differs from a fresh official recheck")
    observed_catalog = _store_catalog(base)
    if receipt.get("store") != observed_catalog:
        raise ValueError("full-game store bytes differ from the frozen verification receipt")

    manifest_raw = (base / "manifest.json").read_bytes()
    try:
        manifest = json.loads(manifest_raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"full-game manifest is invalid: {error}") from error
    if not isinstance(manifest, dict):
        raise ValueError("full-game manifest must be an object")
    semantic = manifest.get("semantic_config")
    execution = manifest.get("execution")
    verification = receipt.get("verification_result")
    if not isinstance(semantic, dict) or not isinstance(execution, dict):
        raise ValueError("full-game manifest has no semantic/execution contract")
    if not isinstance(verification, dict):
        raise ValueError("frozen verification receipt has no official result")
    if hashlib.sha256(_canonical(verification).encode("ascii")).hexdigest() != receipt.get(
        "verification_result_sha256"
    ):
        raise ValueError("frozen verification result hash is invalid")
    if hashlib.sha256(_canonical(semantic).encode("ascii")).hexdigest() != receipt.get(
        "semantic_config_sha256"
    ):
        raise ValueError("frozen semantic config hash is invalid")
    accepted = int(receipt.get("accepted_unique_games", -1))
    target = int(receipt.get("target_unique_games", -1))
    if (
        accepted < 1
        or accepted != target
        or accepted != int(verification.get("accepted_unique_games", -1))
        or target != int(execution.get("target_unique_games", -1))
    ):
        raise ValueError("frozen verification receipt does not prove a complete store")
    simulation_id = str(manifest.get("simulation_id", ""))
    if simulation_id != receipt.get("simulation_id") or simulation_id != verification.get(
        "simulation_id"
    ):
        raise ValueError("frozen receipt simulation identity differs from the store")
    if semantic.get("backend_kind") != "native":
        raise ValueError("frozen neural ingest requires a native full-game store")
    if semantic.get("data_purpose") != "exploration-rollout-v1" or semantic.get(
        "strength_claim"
    ) != "not-champion-play":
        raise ValueError("frozen store is not labeled as exploration-only data")
    policy = semantic.get("policy")
    if not isinstance(policy, dict) or policy.get("policy_id") != "spc-uniform-top-k-v1" or policy.get(
        "preserve_returned_mate"
    ) is not True:
        raise ValueError("frozen store policy contract is unsupported")
    profiles = semantic.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("frozen store semantic config has no profiles")
    if simulation_id != "spc-fullgame-" + hashlib.sha256(
        _canonical(semantic).encode("ascii")
    ).hexdigest():
        raise ValueError("frozen store simulation id does not match its semantic config")
    evidence = {
        "source_kind": "frozen-verifier-receipt-current-rules-replay-v1",
        "receipt": str(receipt_path),
        "receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "snapshot_manifest_sha256": receipt.get("snapshot_manifest_sha256"),
        "snapshot_semantic_fingerprint": receipt.get("snapshot_semantic_fingerprint"),
        "snapshot_native_source_identity": receipt.get("snapshot_native_source_identity"),
        "current_engine_source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "current_native_source_identity": evaluation_module._native_source_identity(),
        "store_catalog_sha256": observed_catalog["catalog_sha256"],
        "store_stream_sha256": observed_catalog["stream_sha256"],
        "official_verification_result_sha256": receipt["verification_result_sha256"],
        "simulation_id": simulation_id,
        "accepted_unique_games": accepted,
        "target_unique_games": target,
        "authoritative_replay": verification.get("authoritative_replay"),
        "trace_deduplication": verification.get("trace_deduplication"),
    }
    return manifest, receipt, evidence


def _receipt_profile_pair(semantic: Mapping[str, Any], attempt: int) -> tuple[int, int]:
    profiles = semantic["profiles"]
    schedule = semantic.get("profile_schedule_id")
    if schedule == "self-round-robin-v2":
        kind = NATIVE_V2_SCHEDULE_SELF_ROUND_ROBIN
    elif schedule == "ordered-pair-round-robin-v2":
        kind = NATIVE_V2_SCHEDULE_ORDERED_PAIR_ROUND_ROBIN
    else:
        raise ValueError("frozen store profile schedule is unsupported")
    return expected_v2_profile_pair(attempt, len(profiles), kind)


def _iter_receipt_bound_records(
    root: str | Path,
    manifest: Mapping[str, Any],
) -> Iterable[FullGameRecord]:
    base = Path(root).expanduser().resolve()
    checkpoint = base / "checkpoint.sqlite3"
    connection = sqlite3.connect(f"file:{checkpoint.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM chunks ORDER BY chunk_index").fetchall()
    except sqlite3.Error as error:
        raise ValueError(f"could not read receipt-bound checkpoint: {error}") from error
    finally:
        connection.close()
    catalog = manifest.get("chunk_catalog")
    if not isinstance(catalog, dict) or int(catalog.get("committed_chunks", -1)) != len(rows):
        raise ValueError("receipt-bound manifest/checkpoint chunk counts differ")
    semantic = manifest["semantic_config"]
    simulation_id = str(manifest["simulation_id"])
    for index, row in enumerate(rows):
        if int(row["chunk_index"]) != index or row["state"] != "committed":
            raise ValueError("receipt-bound checkpoint has an uncommitted chunk")
        filename = str(row["filename"])
        path = (base / "chunks" / filename).resolve()
        if path.parent != (base / "chunks").resolve():
            raise ValueError("receipt-bound chunk path escapes its directory")
        payload = path.read_bytes()
        if chunk_sha256(payload) != str(row["sha256"]):
            raise ValueError("receipt-bound chunk hash differs from its checkpoint")
        decoded = decode_chunk(payload)
        if decoded.header["simulation_id"] != simulation_id:
            raise ValueError("receipt-bound chunk simulation identity differs")
        if (
            int(decoded.header["first_attempt"]) != int(row["attempt_start"])
            or int(decoded.header["attempt_count"]) != int(row["attempt_count"])
            or len(decoded.records) != int(row["accepted_count"])
        ):
            raise ValueError("receipt-bound chunk range/count differs from its checkpoint")
        for record in decoded.records:
            if (
                record.white_profile_index,
                record.black_profile_index,
            ) != _receipt_profile_pair(semantic, record.attempt_index):
                raise ValueError("receipt-bound record profile attribution is invalid")
            yield record


def _receipt_game_key(simulation_id: str, attempt_index: int) -> str:
    return hashlib.sha256(
        f"spc-fullgame-id-v1|{simulation_id}|{attempt_index}".encode("ascii")
    ).hexdigest()


def _build_receipt_bound_dataset(
    records: Iterable[FullGameRecord],
    *,
    simulation_id: str,
    receipt_sha256: str,
    base_profile: EngineProfile,
    seed: int,
    validation_percent: int,
    test_percent: int,
    max_positions_per_game: int,
) -> NeuralDataset:
    if max_positions_per_game < 1:
        raise ValueError("positions_per_game must be positive")
    selected = tuple(records)
    if not selected:
        raise ValueError("receipt-bound selection contains no games")
    weak_source = "spc-weak-frozen-receipt-" + hashlib.sha256(
        (
            receipt_sha256
            + "|"
            + ",".join(
                str(record.attempt_index)
                for record in sorted(selected, key=lambda item: item.attempt_index)
            )
        ).encode("ascii")
    ).hexdigest()
    samples: list[NeuralSample] = []
    # Convert one replay at a time. At production scale this retains only the
    # compact selected records plus final NeuralSample rows; live Board objects
    # from 100k games never accumulate in a second prepared structure.
    for record in selected:
        replay_record(record)
        state = ProgressiveState.initial()
        boundaries: list[ProgressiveState] = []
        for series_index, moves in enumerate(record.series):
            if series_index:
                boundaries.append(state)
            result = play_series(state, moves)
            if result.machine_notation != "/".join(moves):
                raise ValueError("receipt-bound trace is not canonical under current rules")
            state = result.final_state
        if not boundaries:
            raise ValueError("receipt-bound game has no post-S1 training boundary")
        if len(boundaries) > max_positions_per_game:
            count = len(boundaries)
            boundaries = [
                boundaries[((2 * index + 1) * count) // (2 * max_positions_per_game)]
                for index in range(max_positions_per_game)
            ]
        count = len(boundaries)
        base_weight, remainder = divmod(1_000, count)
        target = 1_000 if record.result == "1-0" else 0 if record.result == "0-1" else 500
        game_key = _receipt_game_key(simulation_id, record.attempt_index)
        for index, state in enumerate(boundaries):
            samples.append(
                NeuralSample(
                    game_key=game_key,
                    position_hash=state.position_hash,
                    pfen=state.pfen,
                    active_features=extract_active_features(state),
                    base_profile_id=base_profile.profile_id,
                    base_hand_score=CachedFeatures.from_state(state).score(base_profile),
                    weak_wdl_milli=target,
                    weak_source_fingerprint=weak_source,
                    sample_weight_milli=base_weight + (1 if index < remainder else 0),
                )
            )
    return build_neural_dataset(
        samples,
        base_profile_id=base_profile.profile_id,
        seed=seed,
        validation_percent=validation_percent,
        test_percent=test_percent,
    )


def _script_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _select_bounded_records(
    records: Iterable[FullGameRecord],
    *,
    simulation_id: str,
    seed: int,
    max_games: int,
) -> tuple[tuple[FullGameRecord, ...], int]:
    """Selects the lowest deterministic hashes with O(max_games) memory."""

    if type(seed) is not int:
        raise ValueError("dataset seed must be an exact integer")
    if type(max_games) is not int or max_games < 1:
        raise ValueError("max_games must be a positive integer")
    heap: list[tuple[int, int, FullGameRecord]] = []
    scanned = 0
    for record in records:
        if type(record) is not FullGameRecord:
            raise ValueError("verified store yielded a non-full-game record")
        priority = int.from_bytes(
            hashlib.sha256(
                f"{seed}|neural-game-sample|{simulation_id}|{record.attempt_index}".encode(
                    "utf-8"
                )
            ).digest(),
            "big",
        )
        # Negated keys turn heapq into a bounded max-heap. attempt_index is a
        # deterministic tie-break even under a contrived digest collision.
        entry = (-priority, -record.attempt_index, record)
        if len(heap) < max_games:
            heapq.heappush(heap, entry)
        elif entry > heap[0]:
            heapq.heapreplace(heap, entry)
        scanned += 1
    selected = tuple(sorted((entry[2] for entry in heap), key=lambda row: row.attempt_index))
    return selected, scanned


def _selected_attempts_sha256(records: Iterable[FullGameRecord]) -> str:
    digest = hashlib.sha256(b"spc-neural-selected-attempts-v1\0")
    for record in records:
        digest.update(record.attempt_index.to_bytes(8, "big"))
    return digest.hexdigest()


def _build_bounded_verified_corpus(
    root: str | Path,
    *,
    seed: int,
    holdout_percent: int,
    max_games: int,
    required_store_games: int,
) -> tuple[SelfPlayCorpus, dict[str, Any]]:
    """Verifies all store bytes, then materializes only a stable hash sample."""

    if type(required_store_games) is not int or required_store_games < 0:
        raise ValueError("required_store_games must be a nonnegative integer")
    base = Path(root).expanduser().resolve()
    manifest_path = base / "manifest.json"
    try:
        raw_before = manifest_path.read_bytes()
    except OSError as error:
        raise ValueError(f"could not read full-game manifest: {error}") from error
    verification = verify_fullgame_run(base)
    try:
        raw_verified = manifest_path.read_bytes()
        manifest = json.loads(raw_verified.decode("ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read full-game manifest: {error}") from error
    if raw_before != raw_verified:
        raise ValueError("full-game store changed while it was being verified")

    execution = manifest.get("execution")
    if not isinstance(execution, Mapping):
        raise ValueError("verified full-game manifest has no execution contract")
    accepted = int(verification["accepted_unique_games"])
    target = int(execution.get("target_unique_games", 0))
    if accepted != target:
        raise ValueError(
            f"full-game store is incomplete: {accepted}/{target} accepted unique games"
        )
    if required_store_games and (
        accepted != required_store_games or target != required_store_games
    ):
        raise ValueError(
            "full-game store does not satisfy the required exact game count: "
            f"accepted={accepted}, target={target}, required={required_store_games}"
        )
    if max_games > accepted:
        raise ValueError(
            f"max_games={max_games} exceeds the completed verified store ({accepted})"
        )
    config = FullGameSemanticConfig.from_dict(manifest["semantic_config"])
    if config.simulation_id != verification["simulation_id"]:
        raise ValueError("verified full-game simulation identity changed")

    selected, scanned = _select_bounded_records(
        iter_fullgame_records(base),
        simulation_id=config.simulation_id,
        seed=seed,
        max_games=max_games,
    )
    try:
        raw_after = manifest_path.read_bytes()
    except OSError as error:
        raise ValueError(f"could not reread full-game manifest: {error}") from error
    if raw_after != raw_verified:
        raise ValueError("full-game store changed while the neural sample was selected")
    if scanned != accepted:
        raise ValueError(
            f"verified store scan produced {scanned} records, expected {accepted}"
        )
    selection_sha256 = _selected_attempts_sha256(selected)
    evidence = {
        "source_kind": "verified-fullgame-store-hash-sample",
        "manifest_sha256": hashlib.sha256(raw_verified).hexdigest(),
        "simulation_id": config.simulation_id,
        "authoritative_replay": verification["authoritative_replay"],
        "trace_deduplication": verification["trace_deduplication"],
        "store_accepted_unique_games": accepted,
        "store_target_unique_games": target,
        "scanned_entire_snapshot": True,
        "selection_method": "lowest-sha256(seed,simulation_id,attempt_index)-v1",
        "selected_games": len(selected),
        "selected_attempts_sha256": selection_sha256,
        "checkpoint_rejections": verification["checkpoint_rejections"],
        "checkpoint_rejections_scope": "store-wide evidence; not attributed to sample",
    }
    corpus = build_fullgame_corpus(
        selected,
        config,
        seed=seed,
        holdout_percent=holdout_percent,
        excluded_attempts=0,
        evidence=(evidence,),
    )
    return corpus, evidence


def _teacher_runtime_identity() -> dict[str, Any]:
    expected_native = evaluation_module._native_source_identity()
    native_module = evaluation_module._native_eval
    native_path_value = getattr(native_module, "__file__", None)
    native_path = (
        None
        if native_path_value is None
        else Path(str(native_path_value)).expanduser().resolve()
    )
    native_sha256 = None
    if native_path is not None:
        try:
            native_sha256 = hashlib.sha256(native_path.read_bytes()).hexdigest()
        except OSError as error:
            raise ValueError(f"could not hash loaded native teacher binary: {error}") from error
    return {
        "engine_source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": platform.python_version(),
        "native_available": native_module is not None,
        "native_expected_source_identity": expected_native,
        "native_loaded_source_identity": getattr(native_module, "SOURCE_IDENTITY", None),
        "native_binary": None if native_path is None else str(native_path),
        "native_binary_sha256": native_sha256,
    }


def _require_teacher_runtime(expected: Mapping[str, Any]) -> dict[str, Any]:
    actual = _teacher_runtime_identity()
    if not actual["native_available"]:
        raise ValueError("depth-4 teacher labeling requires the validated native engine")
    if not actual["native_expected_source_identity"]:
        raise ValueError("depth-4 teacher labeling cannot identify its packaged native sources")
    if actual["native_loaded_source_identity"] != actual["native_expected_source_identity"]:
        raise ValueError("loaded native teacher binary does not match its packaged sources")
    if dict(expected) != actual:
        raise ValueError("teacher worker runtime/source/native identity differs from the manifest")
    return actual


def _teacher_plan(dataset: NeuralDataset, split: str, seed: int) -> list[NeuralSample]:
    return sorted(
        dataset.split_samples(split),
        key=lambda sample: (
            hashlib.sha256(
                f"{seed}|teacher|{split}|{sample.sample_id}".encode("utf-8")
            ).digest(),
            sample.sample_id,
        ),
    )


def _mandatory_teacher_anchor_plan() -> tuple[dict[str, Any], ...]:
    state = ProgressiveState.initial()
    for moves in HUMAN_FIRST_GAME_REFUTATION.history[:-1]:
        state = play_series(state, moves).final_state
    if state.series_number != 4:
        raise AssertionError("mandatory first-game teacher root is not Series 4")
    group_key = "spc-mandatory-teacher-" + HUMAN_FIRST_GAME_REFUTATION.anchor_id
    anchors: list[tuple[str, ProgressiveState]] = [("root", state)]
    for hypothesis in HUMAN_FIRST_GAME_CONTENDER_HYPOTHESES:
        anchors.append(
            (
                f"contender-{hypothesis.hypothesis_id}",
                play_series(state, hypothesis.series).final_state,
            )
        )
    return tuple(
        {
            "anchor_id": f"{MANDATORY_TEACHER_GROUP}:{label}",
            "group_id": MANDATORY_TEACHER_GROUP,
            "game_key": group_key,
            "split_component": "spc-neural-mandatory-group-" + hashlib.sha256(
                MANDATORY_TEACHER_GROUP.encode("ascii")
            ).hexdigest()[:20],
            "split": "train",
            "position_hash": anchor_state.position_hash,
            "pfen": anchor_state.pfen,
        }
        for label, anchor_state in anchors
    )


def _mandatory_teacher_worker(
    anchor: Mapping[str, Any],
    profile: EngineProfile,
    limits: SearchLimits,
    expected_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    actual_runtime = _require_teacher_runtime(expected_runtime)
    runtime_fingerprint = hashlib.sha256(
        _canonical(actual_runtime).encode("utf-8")
    ).hexdigest()
    state = _state_from_pfen(str(anchor["pfen"]))
    if state.position_hash != anchor["position_hash"]:
        raise ValueError("mandatory teacher anchor PFEN identity differs")
    result = analyze(state, limits, profile)
    diagnostics = {
        "requested_depth": result.requested_depth,
        "completed_depth": result.completed_depth,
        "exact_width": result.exact_width,
        "root_scores_complete": result.root_scores_complete,
        "timed_out": result.timed_out,
        "work_limit_reached": result.work_limit_reached,
        "work_positions": result.stats.work_positions,
    }
    try:
        sample = sample_from_teacher_result(
            state,
            result,
            game_key=str(anchor["game_key"]),
            minimum_completed_depth=limits.depth_series,
        )
        sample = replace(
            sample,
            split_component=str(anchor["split_component"]),
            split=str(anchor["split"]),
        )
    except ValueError as error:
        return {
            "accepted": False,
            "anchor_id": anchor["anchor_id"],
            "reason": str(error),
            "runtime_fingerprint": runtime_fingerprint,
            "diagnostics": diagnostics,
        }
    return {
        "accepted": True,
        "anchor_id": anchor["anchor_id"],
        "sample": sample.as_dict(),
        "runtime_fingerprint": runtime_fingerprint,
        "diagnostics": diagnostics,
    }


def _validate_mandatory_anchor_row(
    anchor: Mapping[str, Any],
    row: Mapping[str, Any],
    runtime_fingerprint: str,
) -> NeuralSample:
    if row.get("accepted") is not True:
        raise ValueError(
            f"mandatory teacher anchor {anchor['anchor_id']} was not accepted: "
            f"{row.get('reason', 'unknown reason')}"
        )
    if row.get("anchor_id") != anchor["anchor_id"] or row.get(
        "runtime_fingerprint"
    ) != runtime_fingerprint:
        raise ValueError("mandatory teacher anchor result identity differs")
    sample_payload = row.get("sample")
    if not isinstance(sample_payload, Mapping):
        raise ValueError("mandatory teacher anchor result has no sample")
    sample = NeuralSample.from_dict(sample_payload)
    if (
        sample.position_hash != anchor["position_hash"]
        or sample.pfen != anchor["pfen"]
        or sample.game_key != anchor["game_key"]
        or sample.split_component != anchor["split_component"]
        or sample.split != "train"
        or sample.teacher_score is None
        or sample.weak_wdl_milli is not None
    ):
        raise ValueError("mandatory teacher anchor sample contract differs")
    return sample


def _validate_attached_teacher_sample(
    source: NeuralSample,
    labeled: NeuralSample,
) -> None:
    restored = replace(
        labeled,
        teacher_score=source.teacher_score,
        teacher_proof=source.teacher_proof,
        teacher_result_fingerprint=source.teacher_result_fingerprint,
        teacher_profile_id=source.teacher_profile_id,
        teacher_completed_depth=source.teacher_completed_depth,
        teacher_exact_width=source.teacher_exact_width,
    )
    if restored != source:
        raise ValueError("teacher result changed fields outside the teacher label")


def _teacher_worker(
    payload: tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ]
) -> dict[str, Any]:
    sample_payload, profile_payload, limits_payload, expected_runtime = payload
    actual_runtime = _require_teacher_runtime(expected_runtime)
    runtime_fingerprint = hashlib.sha256(_canonical(actual_runtime).encode("utf-8")).hexdigest()
    sample = NeuralSample.from_dict(sample_payload)
    profile = EngineProfile.from_dict(profile_payload)
    limits = SearchLimits(**limits_payload)
    result = analyze(_state_from_pfen(sample.pfen), limits, profile)
    diagnostics = {
        "requested_depth": result.requested_depth,
        "completed_depth": result.completed_depth,
        "exact_width": result.exact_width,
        "root_scores_complete": result.root_scores_complete,
        "timed_out": result.timed_out,
        "work_limit_reached": result.work_limit_reached,
        "work_positions": result.stats.work_positions,
    }
    try:
        labeled = attach_teacher_result(
            sample,
            result,
            minimum_completed_depth=limits.depth_series,
        )
    except ValueError as error:
        return {
            "sample_id": sample.sample_id,
            "split": sample.split,
            "accepted": False,
            "reason": str(error),
            "runtime_fingerprint": runtime_fingerprint,
            "diagnostics": diagnostics,
        }
    return {
        "sample_id": sample.sample_id,
        "split": sample.split,
        "accepted": True,
        "sample": labeled.as_dict(),
        "runtime_fingerprint": runtime_fingerprint,
        "diagnostics": diagnostics,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.endswith("\n"):
                raise ValueError(f"teacher result line {line_number} is not committed")
            row = json.loads(line)
            if not isinstance(row, Mapping):
                raise ValueError(f"teacher result line {line_number} is not an object")
            if line != _canonical(row) + "\n":
                raise ValueError(f"teacher result line {line_number} is not canonical")
            rows.append(row)
    return rows


def _append_rows(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(_canonical(row) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _dataset_command(args: argparse.Namespace) -> int:
    if any(
        type(value) is not int or value < 1
        for value in (args.max_games, args.max_samples, args.max_artifact_mb)
    ):
        raise ValueError("dataset game, sample, and artifact caps must be positive")
    if args.max_games * args.positions_per_game > args.max_samples:
        raise ValueError(
            "max_games * positions_per_game exceeds max_samples; lower the ingest cap"
        )
    profile = _profile(args.base_profile)
    if args.verification_receipt:
        manifest, receipt, ingest = _load_frozen_receipt(
            args.fullgame_store,
            args.verification_receipt,
            expected_snapshot_manifest_sha256=(
                args.snapshot_manifest_sha256 or ""
            ),
        )
        accepted = int(receipt["accepted_unique_games"])
        if args.required_store_games and accepted != args.required_store_games:
            raise ValueError(
                "frozen receipt does not satisfy the required exact game count: "
                f"accepted={accepted}, required={args.required_store_games}"
            )
        if args.max_games > accepted:
            raise ValueError(
                f"max_games={args.max_games} exceeds the frozen store ({accepted})"
            )
        selected, scanned = _select_bounded_records(
            _iter_receipt_bound_records(args.fullgame_store, manifest),
            simulation_id=str(manifest["simulation_id"]),
            seed=args.seed,
            max_games=args.max_games,
        )
        if scanned != accepted:
            raise ValueError(
                f"receipt-bound store scan produced {scanned} records, expected {accepted}"
            )
        selected_sha256 = _selected_attempts_sha256(selected)
        ingest = {
            **ingest,
            "scanned_entire_snapshot": True,
            "selected_games": len(selected),
            "selected_attempts_sha256": selected_sha256,
            "selection_method": "lowest-sha256(seed,simulation_id,attempt_index)-v1",
            "current_rules_replayed_selected_games": len(selected),
        }
        dataset = _build_receipt_bound_dataset(
            selected,
            simulation_id=str(manifest["simulation_id"]),
            receipt_sha256=str(ingest["receipt_sha256"]),
            base_profile=profile,
            seed=args.seed,
            validation_percent=args.validation_percent,
            test_percent=args.test_percent,
            max_positions_per_game=args.positions_per_game,
        )
    else:
        corpus, ingest = _build_bounded_verified_corpus(
            args.fullgame_store,
            seed=args.seed,
            holdout_percent=args.validation_percent + args.test_percent,
            max_games=args.max_games,
            required_store_games=args.required_store_games,
        )
        dataset = build_neural_dataset_from_weak_corpus(
            corpus,
            base_profile=profile,
            seed=args.seed,
            validation_percent=args.validation_percent,
            test_percent=args.test_percent,
            max_positions_per_game=args.positions_per_game,
        )
    if len(dataset.samples) > args.max_samples:
        raise ValueError(
            f"neural dataset has {len(dataset.samples)} samples, cap is {args.max_samples}"
        )
    max_bytes = args.max_artifact_mb * 1024 * 1024
    destination = save_dataset(dataset, args.output, max_bytes=max_bytes)
    print(
        _canonical(
            {
                "dataset": str(destination),
                "dataset_id": dataset.corpus_fingerprint,
                "samples": len(dataset.samples),
                "artifact_bytes": destination.stat().st_size,
                "artifact_cap_bytes": max_bytes,
                "fullgame_ingest": ingest,
                "splits": {
                    split: len(dataset.split_samples(split))
                    for split in ("train", "validation", "test")
                },
            }
        )
    )
    return 0


def _teacher_command(args: argparse.Namespace) -> int:
    if args.depth < 4:
        raise ValueError("teacher depth must be at least 4")
    if args.max_labeled_artifact_mb < 1:
        raise ValueError("max_labeled_artifact_mb must be positive")
    dataset = load_dataset(args.dataset)
    profile = _profile(args.base_profile)
    if dataset.base_profile_id != profile.profile_id:
        raise ValueError("teacher profile differs from the dataset base profile")
    run = Path(args.output_dir).expanduser().resolve()
    run.mkdir(parents=True, exist_ok=True)
    manifest_path = run / "manifest.json"
    rows_path = run / "results.jsonl"
    anchor_rows_path = run / "mandatory-anchors.jsonl"
    labeled_path = run / "dataset-labeled.json"
    targets = {
        "train": args.train_target,
        "validation": args.validation_target,
        "test": args.test_target,
    }
    if any(type(value) is not int or value < 0 for value in targets.values()):
        raise ValueError("teacher targets must be nonnegative integers")
    available = {
        split: len(dataset.split_samples(split))
        for split in ("train", "validation", "test")
    }
    for split, target in targets.items():
        if target > available[split]:
            raise ValueError(
                f"teacher target {target} exceeds the {split} split ({available[split]})"
            )
    runtime = _teacher_runtime_identity()
    _require_teacher_runtime(runtime)
    runtime_fingerprint = hashlib.sha256(_canonical(runtime).encode("utf-8")).hexdigest()
    limits_payload = {
        "depth_series": args.depth,
        "max_series_per_node": args.branch_cap,
        "max_generation_positions": args.max_work,
        "time_limit_seconds": None,
        "collect_all_root_scores": True,
    }
    mandatory_anchors = _mandatory_teacher_anchor_plan()
    manifest = {
        "format": TEACHER_FORMAT,
        "script_sha256": _script_sha256(),
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "dataset_id": dataset.corpus_fingerprint,
        "base_profile_id": profile.profile_id,
        "seed": args.seed,
        "targets": targets,
        "limits": limits_payload,
        "batch_size": TEACHER_BATCH_SIZE,
        "teacher_runtime": runtime,
        "teacher_runtime_fingerprint": runtime_fingerprint,
        "mandatory_teacher_group": {
            "group_id": MANDATORY_TEACHER_GROUP,
            "split": "train",
            "label_kind": "completed-deeper-search-teacher-only",
            "anchors": list(mandatory_anchors),
        },
        "labeled_artifact_cap_bytes": args.max_labeled_artifact_mb * 1024 * 1024,
    }
    if manifest_path.exists():
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
        if stored != manifest:
            raise ValueError("teacher run manifest differs; use a new output directory")
    else:
        write_selfplay_artifact(manifest, manifest_path)

    anchor_rows = _read_jsonl(anchor_rows_path)
    anchors_by_id = {str(item["anchor_id"]): item for item in mandatory_anchors}
    if any(str(row.get("anchor_id", "")) not in anchors_by_id for row in anchor_rows):
        raise ValueError("mandatory teacher result names an unknown anchor")
    if len({str(row["anchor_id"]) for row in anchor_rows}) != len(anchor_rows):
        raise ValueError("mandatory teacher result log repeats an anchor")
    missing_anchors = [
        anchor
        for anchor in mandatory_anchors
        if anchor["anchor_id"] not in {row["anchor_id"] for row in anchor_rows}
    ]
    if missing_anchors:
        limits = SearchLimits(**limits_payload)
        completed_anchors = [
            _mandatory_teacher_worker(anchor, profile, limits, runtime)
            for anchor in missing_anchors
        ]
        _append_rows(anchor_rows_path, completed_anchors)
        anchor_rows.extend(completed_anchors)
    anchor_rows_by_id = {str(row["anchor_id"]): row for row in anchor_rows}
    mandatory_samples = tuple(
        _validate_mandatory_anchor_row(
            anchor,
            anchor_rows_by_id[str(anchor["anchor_id"])],
            runtime_fingerprint,
        )
        for anchor in mandatory_anchors
    )

    rows = _read_jsonl(rows_path)
    dataset_by_id = {sample.sample_id: sample for sample in dataset.samples}
    for row in rows:
        accepted_value = row.get("accepted")
        if type(accepted_value) is not bool or not isinstance(
            row.get("diagnostics"), Mapping
        ):
            raise ValueError("teacher result row has an invalid completion envelope")
        if accepted_value:
            expected_keys = {
                "accepted",
                "diagnostics",
                "runtime_fingerprint",
                "sample",
                "sample_id",
                "split",
            }
            if set(row) != expected_keys or not isinstance(row.get("sample"), Mapping):
                raise ValueError("accepted teacher result row has an invalid shape")
        else:
            expected_keys = {
                "accepted",
                "diagnostics",
                "reason",
                "runtime_fingerprint",
                "sample_id",
                "split",
            }
            if set(row) != expected_keys or type(row.get("reason")) is not str:
                raise ValueError("rejected teacher result row has an invalid shape")
        sample_id = str(row.get("sample_id", ""))
        if sample_id not in dataset_by_id:
            raise ValueError("teacher result names a sample outside the frozen dataset")
        if str(row.get("split", "")) != dataset_by_id[sample_id].split:
            raise ValueError("teacher result split differs from the frozen dataset")
        if row.get("runtime_fingerprint") != runtime_fingerprint:
            raise ValueError("teacher result runtime identity differs from the manifest")
        if row.get("accepted"):
            _validate_attached_teacher_sample(
                dataset_by_id[sample_id],
                NeuralSample.from_dict(row["sample"]),
            )
    by_sample = {str(row["sample_id"]): row for row in rows}
    if len(by_sample) != len(rows):
        raise ValueError("teacher result log repeats a sample")
    labeled: dict[str, NeuralSample] = {
        sample_id: NeuralSample.from_dict(row["sample"])
        for sample_id, row in by_sample.items()
        if row["accepted"]
    }
    if not 1 <= args.workers <= 64:
        raise ValueError("teacher workers must be from 1 through 64")
    executor = (
        None
        if args.workers == 1
        else ProcessPoolExecutor(
            max_workers=args.workers,
            initializer=_require_teacher_runtime,
            initargs=(runtime,),
        )
    )
    try:
        for split in ("train", "validation", "test"):
            target = targets[split]
            if target == 0:
                continue
            accepted = sum(sample.split == split for sample in labeled.values())
            pending = [
                sample
                for sample in _teacher_plan(dataset, split, args.seed)
                if sample.sample_id not in by_sample
            ]
            cursor = 0
            while accepted < target:
                if cursor >= len(pending):
                    raise RuntimeError(f"teacher candidates exhausted in {split}")
                requested = min(TEACHER_BATCH_SIZE, target - accepted)
                batch = pending[cursor : cursor + requested]
                cursor += len(batch)
                work = [
                    (sample.as_dict(), profile.as_dict(), limits_payload, runtime)
                    for sample in batch
                ]
                completed = (
                    [_teacher_worker(item) for item in work]
                    if executor is None
                    else list(executor.map(_teacher_worker, work))
                )
                _append_rows(rows_path, completed)
                for row in completed:
                    sample_id = str(row["sample_id"])
                    by_sample[sample_id] = row
                    if row["accepted"]:
                        labeled[sample_id] = NeuralSample.from_dict(row["sample"])
                        accepted += 1
                print(f"teacher {split}: {accepted}/{target} accepted", flush=True)
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    mandatory_hashes = {sample.position_hash for sample in mandatory_samples}
    merged = tuple(
        labeled.get(sample.sample_id, sample)
        for sample in dataset.samples
        if sample.position_hash not in mandatory_hashes
    ) + mandatory_samples
    labeled_dataset = replace(dataset, samples=merged)
    max_labeled_bytes = args.max_labeled_artifact_mb * 1024 * 1024
    save_dataset(labeled_dataset, labeled_path, max_bytes=max_labeled_bytes)
    print(
        _canonical(
            {
                "dataset": str(labeled_path),
                "dataset_id": labeled_dataset.corpus_fingerprint,
                "artifact_bytes": labeled_path.stat().st_size,
                "artifact_cap_bytes": max_labeled_bytes,
                "teacher_runtime_fingerprint": runtime_fingerprint,
                "mandatory_teacher_samples": len(mandatory_samples),
                "mandatory_teacher_group": MANDATORY_TEACHER_GROUP,
            }
        )
    )
    return 0


def _train_command(args: argparse.Namespace) -> int:
    dataset = load_dataset(args.dataset)
    profile = _profile(args.base_profile)
    if dataset.base_profile_id != profile.profile_id:
        raise ValueError("training profile differs from the dataset base profile")
    output = Path(args.output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    blends = tuple(int(value) for value in args.blends.split(","))
    if not blends or any(not 0 <= value <= 100 for value in blends):
        raise ValueError("training blends must be comma-separated percentages")
    for seed in args.seeds:
        config = NeuralTrainerConfig(
            hidden_size=args.hidden_size,
            epochs=args.epochs,
            seed=seed,
            learning_rate_millionths=args.learning_rate,
            weak_label_weight_milli=args.weak_weight,
            max_weak_train_samples=args.max_weak_train_samples,
            recommended_blend_percent=blends[0],
        )
        network, report = train_fixed_point_network(dataset, config=config)
        prefix = output / f"seed-{seed}"
        save_network(network, prefix.with_suffix(".network.json"))
        write_selfplay_artifact(report, prefix.with_suffix(".report.json"))
        for blend in blends:
            participant = StrengthParticipant(
                profile,
                NeuralBlend.for_profile(network, profile, blend_percent=blend),
            )
            write_selfplay_artifact(
                participant.as_dict(),
                output / f"seed-{seed}.blend-{blend}.variant.json",
            )
    return 0


def _load_gate_approval(
    source: str | Path,
    candidate: StrengthParticipant,
) -> dict[str, Any]:
    path = Path(source).expanduser().resolve()
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load neural gate report: {error}") from error
    if not isinstance(payload, Mapping) or payload.get("format") != NEURAL_GATE_FORMAT:
        raise ValueError("unsupported neural gate report")
    variants = payload.get("variants")
    if not isinstance(variants, list):
        raise ValueError("neural gate report has no variants")
    expected = _canonical(candidate.as_dict())
    matched = [
        item
        for item in variants
        if isinstance(item, Mapping) and _canonical(item.get("variant", {})) == expected
    ]
    if len(matched) != 1:
        raise ValueError("neural gate report does not uniquely approve this exact variant")
    approval = matched[0]
    rules = approval.get("rules_tactical")
    human = approval.get("human_refutation")
    if (
        approval.get("passed") is not True
        or not isinstance(rules, Mapping)
        or rules.get("passed") is not True
        or not isinstance(human, Mapping)
        or human.get("passed") is not True
    ):
        raise ValueError("exact neural variant did not pass both prerequisite gates")
    return {
        "report": str(path),
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "variant_id": candidate.participant_id,
        "passed": True,
    }


def _one_sided_pair_p_value(wins: int, losses: int) -> float:
    decisive = wins + losses
    if decisive == 0 or wins <= losses:
        return 1.0
    numerator = sum(math.comb(decisive, count) for count in range(wins, decisive + 1))
    return numerator / (1 << decisive)


def _strength_decision(
    report: Mapping[str, Any],
    *,
    minimum_pairs: int,
) -> dict[str, Any]:
    summary = report.get("summary")
    if not isinstance(summary, Mapping):
        raise ValueError("strength report has no summary")
    pair_wdl = summary.get("candidate_pair_wdl")
    technical = summary.get("technical_failures")
    if not isinstance(pair_wdl, Mapping) or not isinstance(technical, Mapping):
        raise ValueError("strength report summary is incomplete")
    wins = int(pair_wdl.get("wins", 0))
    draws = int(pair_wdl.get("draws", 0))
    losses = int(pair_wdl.get("losses", 0))
    p_value = _one_sided_pair_p_value(wins, losses)
    checks = {
        "minimum_pairs": int(summary.get("scheduled_pairs", 0)) >= minimum_pairs,
        "all_pairs_complete": int(summary.get("incomplete_pairs", 0)) == 0,
        "all_games_complete": int(summary.get("incomplete_games", 0)) == 0,
        "zero_technical_failures": int(technical.get("total_profile_failures", 0)) == 0
        and int(technical.get("unattributed_worker_failures", 0)) == 0
        and int(technical.get("unattributed_match_limit_failures", 0)) == 0,
        "candidate_score_above_half": float(
            summary.get("candidate_game_score_rate", 0.0) or 0.0
        )
        > 0.5,
        "candidate_pair_wins_exceed_losses": wins > losses,
        "one_sided_pair_sign_test_p_at_most_0_05": p_value <= 0.05,
    }
    return {
        "format": "spc-neural-strength-decision-v1",
        "minimum_pairs": minimum_pairs,
        "pair_wdl": {"wins": wins, "draws": draws, "losses": losses},
        "decisive_pairs": wins + losses,
        "one_sided_pair_sign_test_p_value": p_value,
        "checks": checks,
        "passed": all(checks.values()),
        "effect": "qualification evidence only; never changes the champion",
    }


def _gate_command(args: argparse.Namespace) -> int:
    profile = _profile(args.base_profile)
    network = load_network(args.network)
    output: list[dict[str, Any]] = []
    blends = tuple(int(value) for value in args.blends.split(","))
    if not blends or any(not 0 <= value <= 100 for value in blends):
        raise ValueError("gate blends must be comma-separated percentages")
    for blend in blends:
        overlay = NeuralBlend.for_profile(network, profile, blend_percent=blend)
        rules = run_rules_tactical_gate(
            profile,
            search_depth=args.depth,
            max_series_per_node=args.branch_cap,
            max_generation_positions=args.max_work,
            evaluation_overlay=overlay,
        )
        human = evaluate_human_refutation_gate(profile, evaluation_overlay=overlay)
        output.append(
            {
                "variant": StrengthParticipant(profile, overlay).as_dict(),
                "rules_tactical": rules.as_dict(),
                "human_refutation": human,
                "passed": rules.passed and bool(human["passed"]),
            }
        )
    write_selfplay_artifact(
        {
            "format": NEURAL_GATE_FORMAT,
            "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
            "network_artifact_id": network.artifact_id,
            "base_profile_id": profile.profile_id,
            "variants": output,
        },
        args.output,
    )
    return 0 if all(item["passed"] for item in output) else 2


def _match_command(args: argparse.Namespace) -> int:
    variant_payload = json.loads(Path(args.variant).read_text(encoding="utf-8"))
    candidate = StrengthParticipant.from_dict(variant_payload)
    if candidate.evaluation_overlay is None:
        raise ValueError("neural match candidate has no evaluation overlay")
    gate_approval = _load_gate_approval(args.gate_report, candidate)
    reference = StrengthParticipant(_profile(args.reference))
    if args.minimum_pairs < 50:
        raise ValueError("neural qualification requires at least 50 color-swapped pairs")
    if args.pairs < args.minimum_pairs:
        raise ValueError(
            f"match pairs ({args.pairs}) are below the qualification floor ({args.minimum_pairs})"
        )
    suite = build_seeded_opening_suite(
        seed=args.opening_seed,
        count=args.pairs,
        min_series=args.min_series,
        max_series=args.max_series,
        max_frontier_states=args.opening_frontier,
    )
    holdout_dataset = load_dataset(args.holdout_dataset)
    dataset_hashes = {sample.position_hash for sample in holdout_dataset.samples}
    collisions = [
        case.case_id for case in suite.cases if case.state().position_hash in dataset_hashes
    ]
    if collisions:
        raise ValueError(
            "match opening suite overlaps the neural dataset: " + ", ".join(collisions)
        )
    config = StrengthMatchConfig(
        pairs=args.pairs,
        seed=args.match_seed,
        search_depth=args.depth,
        max_series_per_node=args.branch_cap,
        max_generation_positions=args.max_work,
        max_game_work_positions=args.max_game_work,
        emergency_max_series=args.emergency_max_series,
        opening_suite_version=suite.version,
        opening_case_ids=tuple(case.case_id for case in suite.cases),
    )
    report = run_strength_match(
        candidate,
        reference,
        config=config,
        opening_cases=suite,
        requested_workers=args.workers,
        memory_per_worker_mb=args.memory_per_worker_mb,
        reserve_memory_mb=args.reserve_memory_mb,
        progress=lambda message: print(message, flush=True),
    )
    decision = _strength_decision(report, minimum_pairs=args.minimum_pairs)
    report["neural_qualification"] = {
        "prerequisite_gate": gate_approval,
        "holdout_dataset_id": holdout_dataset.corpus_fingerprint,
        "opening_collision_count": 0,
        "decision": decision,
    }
    write_strength_report(report, args.output)
    return 0 if decision["passed"] else 2


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Deterministic Scottish neural experiment lane")
    commands = root.add_subparsers(dest="command", required=True)

    dataset = commands.add_parser("dataset")
    dataset.add_argument("fullgame_store")
    dataset.add_argument("output")
    dataset.add_argument("--base-profile", default="baseline")
    dataset.add_argument("--seed", type=int, default=20260842)
    dataset.add_argument("--validation-percent", type=int, default=10)
    dataset.add_argument("--test-percent", type=int, default=10)
    dataset.add_argument("--positions-per-game", type=int, default=2)
    dataset.add_argument(
        "--required-store-games", type=int, default=DEFAULT_REQUIRED_STORE_GAMES
    )
    dataset.add_argument("--max-games", type=int, default=DEFAULT_DATASET_GAMES)
    dataset.add_argument("--max-samples", type=int, default=DEFAULT_DATASET_SAMPLES)
    dataset.add_argument(
        "--max-artifact-mb", type=int, default=DEFAULT_DATASET_ARTIFACT_MB
    )
    dataset.add_argument(
        "--verification-receipt",
        help=(
            "frozen-runtime verification receipt for an immutable historical store; "
            "without this, current source/native identity is required"
        ),
    )
    dataset.add_argument(
        "--snapshot-manifest-sha256",
        help=(
            "pinned immutable snapshot manifest SHA-256; required with "
            "--verification-receipt"
        ),
    )
    dataset.set_defaults(handler=_dataset_command)

    teacher = commands.add_parser("teacher")
    teacher.add_argument("dataset")
    teacher.add_argument("output_dir")
    teacher.add_argument("--base-profile", default="baseline")
    teacher.add_argument("--seed", type=int, default=20260842)
    teacher.add_argument("--train-target", type=int, default=4096)
    teacher.add_argument("--validation-target", type=int, default=512)
    teacher.add_argument("--test-target", type=int, default=512)
    teacher.add_argument("--depth", type=int, default=4)
    teacher.add_argument("--branch-cap", type=int, default=64)
    teacher.add_argument("--max-work", type=int, default=5_000_000)
    teacher.add_argument("--workers", type=int, default=12)
    teacher.add_argument("--max-labeled-artifact-mb", type=int, default=512)
    teacher.set_defaults(handler=_teacher_command)

    train = commands.add_parser("train")
    train.add_argument("dataset")
    train.add_argument("output_dir")
    train.add_argument("--base-profile", default="baseline")
    train.add_argument("--seeds", type=int, nargs="+", default=(20260842, 20260843))
    train.add_argument("--hidden-size", type=int, default=16)
    train.add_argument("--epochs", type=int, default=16)
    train.add_argument("--learning-rate", type=int, default=5000)
    train.add_argument("--weak-weight", type=int, default=100)
    train.add_argument("--max-weak-train-samples", type=int, default=32_768)
    train.add_argument("--blends", default="10,20,30,40,50")
    train.set_defaults(handler=_train_command)

    gate = commands.add_parser("gate")
    gate.add_argument("network")
    gate.add_argument("output")
    gate.add_argument("--base-profile", default="baseline")
    gate.add_argument("--blends", default="10,20,30,40,50")
    gate.add_argument("--depth", type=int, default=3)
    gate.add_argument("--branch-cap", type=int, default=32)
    gate.add_argument("--max-work", type=int, default=1_600_000)
    gate.set_defaults(handler=_gate_command)

    match = commands.add_parser("match")
    match.add_argument("variant")
    match.add_argument("reference")
    match.add_argument("holdout_dataset")
    match.add_argument("output")
    match.add_argument("--gate-report", required=True)
    match.add_argument("--pairs", type=int, default=50)
    match.add_argument("--minimum-pairs", type=int, default=50)
    match.add_argument("--opening-seed", type=int, default=20260844)
    match.add_argument("--match-seed", type=int, default=20260845)
    match.add_argument("--min-series", type=int, default=4)
    match.add_argument("--max-series", type=int, default=7)
    match.add_argument("--opening-frontier", type=int, default=32)
    match.add_argument("--depth", type=int, default=3)
    match.add_argument("--branch-cap", type=int, default=32)
    match.add_argument("--max-work", type=int, default=1_600_000)
    match.add_argument("--max-game-work", type=int, default=32_000_000)
    match.add_argument("--emergency-max-series", type=int)
    match.add_argument("--workers", type=int, default=12)
    match.add_argument("--memory-per-worker-mb", type=int, default=768)
    match.add_argument("--reserve-memory-mb", type=int, default=1024)
    match.set_defaults(handler=_match_command)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
