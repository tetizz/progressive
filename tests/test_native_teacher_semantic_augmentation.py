from __future__ import annotations

from copy import deepcopy

import chess
import pytest

from scripts.augment_native_teacher_semantics import augment_label_semantics
from scottish_progressive.corpus_shards import progressive_state_dedup_key
from scottish_progressive.fast_training import CachedFeatures
from scottish_progressive.model import ProgressiveState
from scottish_progressive.rules import play_series


def _promotion_label() -> tuple[dict[str, object], ProgressiveState]:
    root = ProgressiveState.from_fen(
        "7k/P7/8/8/8/8/8/K7 w - - 0 1",
        1,
    )
    result = play_series(root, ("a7a8q",))
    final = result.final_state
    final_key = progressive_state_dedup_key(final).hex()
    return (
        {
            "state_key_sha256": progressive_state_dedup_key(root).hex(),
            "position_hash": root.position_hash,
            "pfen": root.pfen,
            "root_features": CachedFeatures.from_state(root).as_dict(),
            "options": [
                {
                    "series": result.machine_notation,
                    "final_state_key_sha256": final_key,
                    "final_pfen": final.pfen,
                    "final_features": CachedFeatures.from_state(final).as_dict(),
                    "principal_variation": [
                        {
                            "series": result.machine_notation,
                            "final_state_key_sha256": final_key,
                        }
                    ],
                }
            ],
        },
        root,
    )


def test_semantic_augmentation_preserves_promoted_provenance() -> None:
    label, root = _promotion_label()
    augmented, replayed = augment_label_semantics(label, root)

    assert replayed == 1
    assert augmented["root_promoted_bitboard"] == 0
    assert augmented["root_chess960"] is False
    option = augmented["options"][0]
    assert option["final_promoted_bitboard"] == chess.BB_A8
    assert option["final_chess960"] is False


def test_semantic_augmentation_rejects_option_key_drift() -> None:
    label, root = _promotion_label()
    corrupted = deepcopy(label)
    corrupted["options"][0]["final_state_key_sha256"] = "00" * 32

    with pytest.raises(ValueError, match="final key drifted"):
        augment_label_semantics(corrupted, root)
