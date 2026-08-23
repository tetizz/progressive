from __future__ import annotations

import argparse
import json
from pathlib import Path

from scottish_progressive.corpus_pipeline import read_native_generation_contract
from scottish_progressive.corpus_shards import CorpusStore
from scottish_progressive.native_corpus_training import (
    build_native_shard_value_corpus,
)
from scottish_progressive.profiles import baseline_profile, load_profile, save_profile
from scottish_progressive.selfplay_training import (
    tune_selfplay_profile,
    write_selfplay_artifact,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fit an unpromoted positional-weight candidate from native shards."
    )
    parser.add_argument("train_root", type=Path)
    parser.add_argument("holdout_root", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--minimum-series", type=int, default=3)
    parser.add_argument("--parent-profile", type=Path)
    parser.add_argument(
        "--candidate-name", default="native shard WDL positional candidate"
    )
    parser.add_argument("--regularization", type=float, default=0.02)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    train_store = CorpusStore.open(args.train_root)
    holdout_store = CorpusStore.open(args.holdout_root)
    train_contract = read_native_generation_contract(train_store.root)
    holdout_contract = read_native_generation_contract(holdout_store.root)
    corpus = build_native_shard_value_corpus(
        train_store,
        holdout_store,
        train_config=train_contract.config,
        holdout_config=holdout_contract.config,
        profiles=train_contract.ordered_profiles,
        minimum_series=args.minimum_series,
    )
    parent = (
        baseline_profile()
        if args.parent_profile is None
        else load_profile(args.parent_profile)
    )
    candidate, tuning = tune_selfplay_profile(
        corpus,  # Compatible train/holdout weighted sample surface.
        parent,
        name=args.candidate_name,
        regularization=args.regularization,
    )
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    corpus_payload = corpus.as_dict()
    summary = {
        key: value for key, value in corpus_payload.items() if key != "samples"
    }
    summary_path = write_selfplay_artifact(
        summary,
        output / "native-value-corpus-summary.json",
    )
    tuning_path = write_selfplay_artifact(
        {
            **tuning,
            "candidate": candidate.as_dict(),
            "native_corpus_summary": corpus_payload["summary"],
            "evidence_scope": (
                "seven-weight WDL value fit only; the candidate remains unpromoted "
                "until tactical and independently seeded paired-match gates pass"
            ),
        },
        output / "native-value-tuning-report.json",
    )
    candidate_path = save_profile(candidate, output / "candidate-profile.json")
    receipt = {
        "format": "spc-native-value-training-receipt-v1",
        "corpus_id": corpus.corpus_id,
        "completed_games": corpus.completed_games,
        "excluded_attempts": corpus.excluded_attempts,
        "train_samples": len(corpus.train_samples),
        "holdout_samples": len(corpus.holdout_samples),
        "train_feature_buckets": tuning["train_feature_buckets"],
        "holdout_feature_buckets": tuning["holdout_feature_buckets"],
        "exact_overlap_states_removed": corpus.exact_overlap_states_removed,
        "exact_overlap_occurrences_removed": corpus.exact_overlap_occurrences_removed,
        "holdout_game_weight_coverage": corpus.holdout_game_weight_coverage,
        "train_generation_contract_sha256": train_contract.digest_hex,
        "holdout_generation_contract_sha256": holdout_contract.digest_hex,
        "train_corpus_sha256": corpus.train_corpus_sha256,
        "holdout_corpus_sha256": corpus.holdout_corpus_sha256,
        "parent_profile_id": parent.profile_id,
        "candidate_profile_id": candidate.profile_id,
        "candidate_weights": candidate.as_dict()["weights"],
        "baseline_train_loss": tuning["baseline_train_loss"],
        "candidate_train_loss": tuning["candidate_train_loss"],
        "baseline_holdout_loss": tuning["baseline_holdout_loss"],
        "candidate_holdout_loss": tuning["candidate_holdout_loss"],
        "summary_path": str(summary_path),
        "tuning_path": str(tuning_path),
        "candidate_path": str(candidate_path),
        "claim_scope": tuning["claim_scope"],
    }
    receipt_path = write_selfplay_artifact(
        receipt, output / "native-value-training-receipt.json"
    )
    receipt["receipt_path"] = str(receipt_path)
    print(json.dumps(receipt, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
