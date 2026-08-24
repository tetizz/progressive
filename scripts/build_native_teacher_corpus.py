from __future__ import annotations

import argparse
import json
from pathlib import Path

from scottish_progressive.corpus_shards import CorpusStore
from scottish_progressive.native_teacher import (
    NativeTeacherConfig,
    build_native_teacher_corpus,
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
    parser.add_argument("--skip-tactical-gate", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    forbidden_train_state_keys = set(args.forbidden_train_option_final_key)
    forbidden_holdout_state_keys: set[str] = set()
    if args.cross_tier_artifact is not None:
        cross_tier_payload = json.loads(
            args.cross_tier_artifact.read_text(encoding="utf-8")
        )
        cross_train, cross_holdout = _cross_tier_forbidden_state_keys(
            cross_tier_payload
        )
        forbidden_train_state_keys.update(cross_train)
        forbidden_holdout_state_keys.update(cross_holdout)
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
        prior_receipt_cache_contract=prior_receipt_cache_contract,
    )
    path = write_native_teacher_artifact(payload, args.output)
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
        "runtime": payload["runtime"],
    }
    print(json.dumps(summary, sort_keys=True, indent=2))
    if quality["status"] != "complete":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
