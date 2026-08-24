from __future__ import annotations

import chess

from scottish_progressive.corpus_shards import progressive_state_dedup_key
from scottish_progressive.fast_training import CachedFeatures
from scottish_progressive.model import ProgressiveState
from scottish_progressive.teacher_value_features import (
    TEACHER_VALUE_FEATURE_NAMES,
    TeacherValueFeaturesV3,
    state_from_pfen,
)


def _features(state: ProgressiveState) -> TeacherValueFeaturesV3:
    return TeacherValueFeaturesV3.from_state_and_cached(
        state,
        CachedFeatures.from_state(state).as_dict(),
    )


def test_teacher_value_features_are_exact_deterministic_and_named() -> None:
    state = ProgressiveState.initial()

    first = _features(state)
    second = _features(state)

    assert first == second
    assert len(first.values) == len(TEACHER_VALUE_FEATURE_NAMES) == 47
    assert tuple(first.as_dict()) == TEACHER_VALUE_FEATURE_NAMES
    assert all(type(value) is int for value in first.values)


def test_teacher_value_pfen_parser_preserves_progressive_identity() -> None:
    state = ProgressiveState.from_fen(
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        2,
        quiet_series=1,
    )

    restored = state_from_pfen(state.pfen)

    assert restored.pfen == state.pfen
    assert restored.position_hash == state.position_hash
    assert _features(restored) == _features(state)


def test_teacher_value_pfen_parser_restores_promoted_provenance() -> None:
    board = chess.Board("7k/4Q3/8/8/8/8/8/7K b - - 0 1")
    board.promoted = chess.BB_E7
    state = ProgressiveState(board, series_number=2)

    lossy = state_from_pfen(state.pfen)
    restored = state_from_pfen(
        state.pfen,
        promoted_bitboard=state.board.promoted,
        chess960=state.board.chess960,
    )

    assert lossy.board.promoted == 0
    assert progressive_state_dedup_key(lossy) != progressive_state_dedup_key(state)
    assert restored.board.promoted == state.board.promoted
    assert progressive_state_dedup_key(restored) == progressive_state_dedup_key(
        state
    )


def test_teacher_value_threat_features_keep_white_centric_sign() -> None:
    white = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/1r6/1Q5K w - - 0 1",
        1,
    )
    black = ProgressiveState.from_fen(
        "1q5k/1R6/8/8/8/8/8/7K b - - 0 1",
        2,
    )

    white_values = _features(white).as_dict()
    black_values = _features(black).as_dict()

    assert white_values["direct_capture_count_for_mover"] > 0
    assert white_values["direct_max_capture_value_for_mover"] == 525
    assert black_values["direct_capture_count_for_mover"] < 0
    assert black_values["direct_max_capture_value_for_mover"] == -525


def test_teacher_value_terminal_mate_threat_is_visible() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
        1,
    )

    values = _features(state).as_dict()

    assert values["direct_check_count_for_mover"] > 0
    assert values["direct_mate_count_for_mover"] > 0


def test_teacher_value_counts_every_isolated_doubled_pawn() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/P7/P7/7K w - - 0 1",
        1,
    )

    values = _features(state).as_dict()

    assert values["isolated_pawn_liability_balance"] == -2
    assert values["doubled_pawn_liability_balance"] == -1


def test_teacher_value_features_are_color_swap_symmetric() -> None:
    white = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/1r6/1Q5K w - - 0 1",
        11,
    )
    # Series 11/12 are a legal color-swapped pair.  Both are capped to the
    # same phase bucket, so every white-centric feature must change sign.
    black = ProgressiveState(white.board.mirror(), series_number=12)

    white_features = TeacherValueFeaturesV3.from_state_and_cached(
        white, CachedFeatures.from_state(white).as_dict()
    ).as_dict()
    black_features = TeacherValueFeaturesV3.from_state_and_cached(
        black, CachedFeatures.from_state(black).as_dict()
    ).as_dict()

    for name in TEACHER_VALUE_FEATURE_NAMES:
        if name == "reach_complete":
            assert white_features[name] == black_features[name] == 1
        else:
            assert white_features[name] == -black_features[name], name
