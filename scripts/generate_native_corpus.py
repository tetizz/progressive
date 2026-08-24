from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile

from scottish_progressive.corpus_pipeline import (
    CorpusGenerationPlan,
    NativeGenerationContract,
    generate_corpus,
    verify_native_boundary_corpus,
)
from scottish_progressive.corpus_shards import CorpusStore
from scottish_progressive.native_corpus import (
    NativeCorpusConfig,
    NativeProfileSchedule,
    NativeRankPolicy,
)
from scottish_progressive.profiles import baseline_profile, load_profile


def _atomic_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate resumable source-bound progressive training shards."
    )
    parser.add_argument("root", type=Path)
    parser.add_argument("--attempts", type=int, required=True)
    parser.add_argument("--first-attempt", type=int, default=0)
    parser.add_argument("--shard-size", type=int, default=10_000)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument(
        "--workers", type=int, default=max(1, min(8, os.cpu_count() or 1))
    )
    parser.add_argument("--seed", type=int, default=730_194_821)
    parser.add_argument("--max-attempt-series", type=int, default=64)
    parser.add_argument("--frontier", type=int, default=32)
    parser.add_argument("--candidates", type=int, default=16)
    parser.add_argument("--max-positions-per-series", type=int, default=250_000)
    parser.add_argument("--max-positions-per-game", type=int, default=10_000_000)
    parser.add_argument("--uniform", action="store_true")
    parser.add_argument(
        "--ordered-pairs",
        action="store_true",
        help=(
            "cycle every ordered white/black profile pairing instead of "
            "self-play only; with N profiles each N**2-attempt block is balanced"
        ),
    )
    parser.add_argument(
        "--profile",
        action="append",
        type=Path,
        help=(
            "EngineProfile JSON to schedule; repeat for multiple profiles. "
            "The built-in baseline is used when omitted."
        ),
    )
    parser.add_argument("--skip-payload-verification", action="store_true")
    parser.add_argument("--skip-unique-count", action="store_true")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument(
        "--preregistration",
        type=Path,
        help="Cycle-4 manifest to bind before any trajectory shard is created.",
    )
    parser.add_argument(
        "--protocol-split",
        choices=("train", "sealed_holdout"),
        help="Preregistered trajectory side; required with --preregistration.",
    )
    return parser.parse_args()


def _cycle4_preregistration_before_profile_load(args: argparse.Namespace):
    if (args.preregistration is None) != (args.protocol_split is None):
        raise ValueError(
            "--preregistration and --protocol-split must be supplied together"
        )
    if args.preregistration is None:
        return None
    try:
        from scripts.fit_deep_teacher_value import _load_preregistration
    except ModuleNotFoundError:
        from fit_deep_teacher_value import (  # type: ignore[no-redef]
            _load_preregistration,
        )
    return _load_preregistration(
        args.preregistration, forbid_pair_preparation=True
    )


def _cycle4_generation_start(
    args: argparse.Namespace,
    plan: CorpusGenerationPlan,
    preregistration=None,
) -> tuple[dict[str, object], Path, str, Path, str] | None:
    if preregistration is None:
        preregistration = _cycle4_preregistration_before_profile_load(args)
    if preregistration is None:
        return None
    try:
        from scripts.fit_deep_teacher_value import (
            _atomic_exclusive_json,
            _exclusive_json,
            _expected_generation_contract_sha256,
            _read_json_artifact,
            _require_protocol_registry_isolation,
            _reserve_output_directory,
        )
    except ModuleNotFoundError:
        from fit_deep_teacher_value import (  # type: ignore[no-redef]
            _atomic_exclusive_json,
            _exclusive_json,
            _expected_generation_contract_sha256,
            _read_json_artifact,
            _require_protocol_registry_isolation,
            _reserve_output_directory,
        )
    split = str(args.protocol_split)
    trajectory = preregistration.manifest["trajectory_corpora"]
    expected = trajectory[split]
    shared = trajectory["shared_config"]
    if (
        args.first_attempt != expected["attempt_start"]
        or args.attempts != expected["attempts"]
        or args.first_attempt + args.attempts != expected["attempt_stop"]
        or args.shard_size != shared["shard_size"]
        or args.batch_size != shared["batch_size"]
        or args.workers != shared["workers"]
        or bool(args.skip_payload_verification) is shared["verify_payloads"]
        or bool(args.skip_unique_count) is shared["count_unique_states"]
    ):
        raise ValueError("trajectory CLI settings differ from preregistration")
    contract = NativeGenerationContract.from_plan(plan)
    expected_contract_sha = _expected_generation_contract_sha256(
        preregistration, split=split
    )
    if contract.digest_hex != expected_contract_sha:
        raise ValueError("trajectory semantic contract differs from preregistration")
    root = plan.root.expanduser().resolve()
    if str(args.root) != str(root):
        raise ValueError("protocol trajectory root must be an absolute canonical path")
    if args.receipt is None:
        raise ValueError("protocol trajectory generation requires --receipt")
    receipt_path = args.receipt.expanduser().resolve()
    if str(args.receipt) != str(receipt_path):
        raise ValueError("protocol trajectory receipt must be an absolute canonical path")
    start_path = root.with_name(
        root.name + ".cycle4-preregistration-generation-start.json"
    )
    root_binding_path = root / "cycle4-preregistration-root-binding.json"
    _require_protocol_registry_isolation(
        preregistration,
        {
            "trajectory root": root,
            "completion receipt": receipt_path,
            "generation start": start_path,
            "root binding": root_binding_path,
        },
        label="generate-native-corpus",
    )
    protected_paths = (root, start_path, root_binding_path)
    if (
        any(
            receipt_path == protected
            or receipt_path in protected.parents
            or protected in receipt_path.parents
            for protected in protected_paths
        )
        or receipt_path.is_dir()
    ):
        raise ValueError(
            "protocol trajectory root, start, binding, and receipt paths must be "
            "distinct, non-nested, and file-compatible"
        )
    start = {
        "schema": "spc-cycle4-trajectory-generation-start-v1",
        "preregistration": {
            "schema": preregistration.schema,
            "raw_artifact_sha256": preregistration.sha256,
        },
        "split": split,
        "root": str(root),
        "receipt": str(receipt_path),
        "attempt_start": args.first_attempt,
        "attempt_stop": args.first_attempt + args.attempts,
        "generation_contract_sha256": contract.digest_hex,
        "operational": {
            "shard_size": args.shard_size,
            "batch_size": args.batch_size,
            "workers": args.workers,
            "verify_payloads": not args.skip_payload_verification,
            "count_unique_states": not args.skip_unique_count,
        },
    }
    start_preexisted = start_path.exists()
    if start_preexisted:
        existing, _ = _read_json_artifact(start_path)
        if existing != start:
            raise ValueError(
                "existing trajectory generation-start binding differs; resume denied"
            )
    else:
        # Never retroactively legitimize an existing store or an orphaned
        # in-root binding. The external start is the ownership fence observed
        # by every CorpusStore writer before it may create or mutate the root.
        if root_binding_path.exists():
            raise FileExistsError(
                "trajectory root binding exists without its external generation start"
            )
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise FileExistsError(
                "protocol trajectory root contains unbound preexisting data"
            )
        if receipt_path.exists():
            raise FileExistsError(
                "protocol trajectory receipt exists without a generation-start binding"
            )
        _atomic_exclusive_json(
            start_path,
            start,
            conflict_message="cycle-4 trajectory generation was already started",
        )
    persisted_start, start_raw_sha256 = _read_json_artifact(start_path)
    if persisted_start != start:
        raise ValueError("trajectory generation-start changed after reservation")
    root_binding = {
        "schema": "spc-cycle4-trajectory-root-binding-v1",
        "root": str(root),
        "generation_start": {
            "path": str(start_path),
            "raw_artifact_sha256": start_raw_sha256,
        },
    }
    if root_binding_path.exists():
        existing_root_binding, _ = _read_json_artifact(root_binding_path)
        if existing_root_binding != root_binding:
            raise ValueError("trajectory root binding differs; resume denied")
    else:
        if root.exists():
            if not root.is_dir() or any(root.iterdir()):
                raise FileExistsError(
                    "protocol trajectory root contains unbound preexisting data"
                )
        else:
            _reserve_output_directory(root, "protocol trajectory")
        _atomic_exclusive_json(
            root_binding_path,
            root_binding,
            conflict_message="cycle-4 trajectory root was concurrently bound",
        )
    persisted_root_binding, root_binding_raw_sha256 = _read_json_artifact(
        root_binding_path
    )
    if persisted_root_binding != root_binding:
        raise ValueError("trajectory root binding changed after reservation")
    return (
        start,
        start_path,
        start_raw_sha256,
        root_binding_path,
        root_binding_raw_sha256,
    )


def _completed_cycle4_receipt(
    args: argparse.Namespace,
    plan: CorpusGenerationPlan,
    protocol_start: tuple[dict[str, object], Path, str, Path, str],
) -> dict[str, object] | None:
    if args.receipt is None or not args.receipt.exists():
        return None
    try:
        from scripts.fit_deep_teacher_value import _read_json_artifact
    except ModuleNotFoundError:
        from fit_deep_teacher_value import _read_json_artifact  # type: ignore[no-redef]

    (
        start,
        start_path,
        start_raw_sha256,
        root_binding_path,
        root_binding_raw_sha256,
    ) = protocol_start
    persisted_start, persisted_start_raw = _read_json_artifact(start_path)
    if persisted_start != start or persisted_start_raw != start_raw_sha256:
        raise ValueError("trajectory generation-start changed before resume")
    persisted_root_binding, persisted_root_binding_raw = _read_json_artifact(
        root_binding_path
    )
    if (
        persisted_root_binding
        != {
            "schema": "spc-cycle4-trajectory-root-binding-v1",
            "root": str(plan.root),
            "generation_start": {
                "path": str(start_path),
                "raw_artifact_sha256": start_raw_sha256,
            },
        }
        or persisted_root_binding_raw != root_binding_raw_sha256
    ):
        raise ValueError("trajectory root binding changed before resume")
    receipt, _ = _read_json_artifact(args.receipt)
    store = CorpusStore(
        plan.root,
        plan.identity,
        protocol_root_binding_sha256=plan.protocol_root_binding_sha256,
    )
    snapshot = store.verified_snapshot()
    expected_verification = (
        None
        if args.skip_payload_verification
        else verify_native_boundary_corpus(
            store,
            count_unique_states=not args.skip_unique_count,
            verified_snapshot=snapshot,
        )
    )
    expected_start = {
        "schema": start["schema"],
        "path": str(start_path),
        "raw_artifact_sha256": start_raw_sha256,
        "preregistration_raw_artifact_sha256": start["preregistration"][
            "raw_artifact_sha256"
        ],
        "root_binding_path": str(root_binding_path),
        "root_binding_raw_artifact_sha256": root_binding_raw_sha256,
    }
    generation_contract = receipt.get("generation_contract")
    if (
        receipt.get("format") != "spc-native-corpus-generation-receipt-v1"
        or receipt.get("root") != str(plan.root)
        or receipt.get("planned_attempt_start") != plan.first_attempt
        or receipt.get("planned_attempt_stop")
        != plan.first_attempt + plan.attempt_count
        or receipt.get("planned_attempt_count") != plan.attempt_count
        or receipt.get("shard_size") != plan.shard_size
        or receipt.get("batch_size") != plan.batch_size
        or receipt.get("workers") != plan.workers
        or receipt.get("corpus") != snapshot[0]
        or not isinstance(generation_contract, dict)
        or generation_contract.get("sha256")
        != NativeGenerationContract.from_plan(plan).digest_hex
        or receipt.get("preregistration_generation_start") != expected_start
        or receipt.get("payload_verification") != expected_verification
        or not isinstance(receipt.get("completed_at"), str)
    ):
        raise ValueError("completed protocol trajectory receipt differs on resume")
    if store.verified_snapshot() != snapshot:
        raise ValueError("native corpus changed during completed-receipt validation")
    return receipt


def _run(args: argparse.Namespace) -> None:
    # Fence the entire upstream producer before any caller-controlled profile
    # path is opened. Once holdout preparation starts, only the pair-resume and
    # completed-pair consumer commands may run.
    preregistration = _cycle4_preregistration_before_profile_load(args)
    profiles = (
        tuple(load_profile(path) for path in args.profile)
        if args.profile
        else (baseline_profile(),)
    )
    config = NativeCorpusConfig(
        seed=args.seed,
        max_attempt_series=args.max_attempt_series,
        max_frontier_states=args.frontier,
        max_positions_per_series=args.max_positions_per_series,
        max_positions_per_game=args.max_positions_per_game,
        candidate_count=args.candidates,
        policy=NativeRankPolicy.uniform() if args.uniform else NativeRankPolicy(),
        schedule=(
            NativeProfileSchedule.ORDERED_PAIR_ROUND_ROBIN
            if args.ordered_pairs
            else NativeProfileSchedule.SELF_ROUND_ROBIN
        ),
    )
    plan = CorpusGenerationPlan(
        root=args.root,
        config=config,
        profiles=profiles,
        first_attempt=args.first_attempt,
        attempt_count=args.attempts,
        shard_size=args.shard_size,
        batch_size=args.batch_size,
        workers=args.workers,
    )
    protocol_start = _cycle4_generation_start(args, plan, preregistration)
    if protocol_start is not None:
        plan = replace(
            plan,
            protocol_root_binding_sha256=protocol_start[4],
        )
        completed = _completed_cycle4_receipt(args, plan, protocol_start)
        if completed is not None:
            print(json.dumps(completed, sort_keys=True, indent=2))
            return
    receipt = generate_corpus(plan)
    if not args.skip_payload_verification:
        store = CorpusStore(
            plan.root,
            plan.identity,
            protocol_root_binding_sha256=plan.protocol_root_binding_sha256,
        )
        snapshot = store.verified_snapshot()
        if receipt.get("corpus") != snapshot[0]:
            raise ValueError(
                "generation receipt corpus differs from the payload-verification snapshot"
            )
        receipt["payload_verification"] = verify_native_boundary_corpus(
            store,
            count_unique_states=not args.skip_unique_count,
            verified_snapshot=snapshot,
        )
        if store.verified_snapshot() != snapshot:
            raise ValueError("native corpus changed before receipt publication")
    receipt["completed_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    if protocol_start is not None:
        (
            start,
            start_path,
            start_raw_sha256,
            root_binding_path,
            root_binding_raw_sha256,
        ) = protocol_start
        try:
            from scripts.fit_deep_teacher_value import _read_json_artifact
        except ModuleNotFoundError:
            from fit_deep_teacher_value import (  # type: ignore[no-redef]
                _read_json_artifact,
            )
        persisted_start, persisted_start_raw = _read_json_artifact(start_path)
        if persisted_start != start or persisted_start_raw != start_raw_sha256:
            raise ValueError("trajectory generation-start changed before completion")
        persisted_root_binding, persisted_root_binding_raw = _read_json_artifact(
            root_binding_path
        )
        if (
            persisted_root_binding
            != {
                "schema": "spc-cycle4-trajectory-root-binding-v1",
                "root": str(plan.root),
                "generation_start": {
                    "path": str(start_path),
                    "raw_artifact_sha256": start_raw_sha256,
                },
            }
            or persisted_root_binding_raw != root_binding_raw_sha256
        ):
            raise ValueError("trajectory root binding changed before completion")
        receipt["preregistration_generation_start"] = {
            "schema": start["schema"],
            "path": str(start_path),
            "raw_artifact_sha256": start_raw_sha256,
            "preregistration_raw_artifact_sha256": start["preregistration"][
                "raw_artifact_sha256"
            ],
            "root_binding_path": str(root_binding_path),
            "root_binding_raw_artifact_sha256": root_binding_raw_sha256,
        }
    if args.receipt is not None:
        if protocol_start is None:
            _atomic_receipt(args.receipt.resolve(), receipt)
        else:
            try:
                from scripts.fit_deep_teacher_value import _atomic_exclusive_json
            except ModuleNotFoundError:
                from fit_deep_teacher_value import (  # type: ignore[no-redef]
                    _atomic_exclusive_json,
                )
            _atomic_exclusive_json(
                args.receipt,
                receipt,
                conflict_message="protocol trajectory receipt already exists",
            )
    print(json.dumps(receipt, sort_keys=True, indent=2))


def main() -> None:
    args = parse_args()
    if args.preregistration is None:
        _run(args)
        return
    try:
        from scripts.fit_deep_teacher_value import _protocol_stage_lock
    except ModuleNotFoundError:
        from fit_deep_teacher_value import (  # type: ignore[no-redef]
            _protocol_stage_lock,
        )
    with _protocol_stage_lock("generate-native-corpus", exclusive=False):
        _run(args)


if __name__ == "__main__":
    main()
