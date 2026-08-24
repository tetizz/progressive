from __future__ import annotations

import chess
import pytest

import scottish_progressive.evaluation as evaluation
from scottish_progressive.fast_training import CachedFeatures
from scottish_progressive.model import ProgressiveState
from scottish_progressive.teacher_value_features import TeacherValueFeaturesV3


def _require_native() -> object:
    native = evaluation._native_eval
    if (
        native is None
        or not hasattr(native, "teacher_value_features_v3")
        or not hasattr(native, "teacher_value_features_v3_with_receipt")
        or not hasattr(native, "deep_teacher_score_v1")
        or not hasattr(native, "proof_aware_root_precedes_v1")
    ):
        pytest.skip("source-matched native teacher-value evaluator is not built")
    assert native.SOURCE_IDENTITY == evaluation._native_source_identity()
    return native


def _python_features(state: ProgressiveState) -> tuple[int, ...]:
    # Keep the parity oracle independent of the extension under test.  This
    # forces both the full evaluator and Progressive legal variants through
    # their Python implementations while materializing the frozen contract.
    prior = evaluation._native_eval
    evaluation._native_eval = None
    try:
        return TeacherValueFeaturesV3.from_state_and_cached(
            state,
            CachedFeatures.from_state(state).as_dict(),
        ).values
    finally:
        evaluation._native_eval = prior


def _native_features(
    native: object,
    state: ProgressiveState,
    feature_count: int | None = None,
) -> tuple[int, ...]:
    board = state.board
    arguments = (
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
        board.occupied_co[chess.WHITE],
        board.occupied_co[chess.BLACK],
        board.promoted,
        board.clean_castling_rights(),
        board.turn,
        state.series_number,
        state.ep_targets,
        256,
    )
    if feature_count is not None:
        arguments += (feature_count,)
    return tuple(native.teacher_value_features_v3(*arguments))


@pytest.mark.parametrize(
    "state",
    [
        ProgressiveState.initial(),
        ProgressiveState.from_fen(chess.STARTING_FEN, 3),
        ProgressiveState.from_fen(
            "7k/8/8/pPpP4/8/8/8/K7 w - - 0 1",
            3,
            ep_targets=(chess.A6, chess.C6),
        ),
        ProgressiveState.from_fen(
            "7k/8/8/8/8/8/1r6/1Q5K w - - 0 1",
            3,
        ),
        ProgressiveState.from_fen(
            "1q5k/1R6/8/8/8/8/8/7K b - - 0 1",
            2,
        ),
        ProgressiveState.from_fen(
            "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
            1,
        ),
        ProgressiveState.from_fen(
            "4r2k/8/8/8/8/8/4R3/4K3 w - - 0 1",
            1,
        ),
        ProgressiveState.from_fen(
            "r3k2r/pp1n1ppp/2p1b3/3pP3/3P4/2N1B3/PP3PPP/R3K2R w KQkq - 0 1",
            11,
        ),
        ProgressiveState(
            chess.Board(
                "r3k2r/pp1n1ppp/2p1b3/3pP3/3P4/2N1B3/PP3PPP/R3K2R w KQkq - 0 1"
            ).mirror(),
            series_number=12,
        ),
    ],
)
def test_native_teacher_value_features_match_python_exactly(
    state: ProgressiveState,
) -> None:
    native = _require_native()

    expected = _python_features(state)
    actual = _native_features(native, state)

    assert len(actual) == len(expected) == 47
    assert actual == expected


def test_native_teacher_value_features_preserve_promoted_provenance() -> None:
    native = _require_native()
    board = chess.Board("7k/4Q3/8/8/8/8/8/7K b - - 0 1")
    board.promoted = chess.BB_E7
    state = ProgressiveState(board, series_number=2)

    assert _native_features(native, state) == _python_features(state)


@pytest.mark.parametrize("feature_count", [7, 14, 19, 38, 44, 47])
def test_native_teacher_value_prefix_stops_at_frozen_model_group(
    feature_count: int,
) -> None:
    native = _require_native()
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/1r6/1Q5K w - - 0 1",
        3,
    )

    assert _native_features(native, state, feature_count) == _python_features(
        state
    )[:feature_count]


def test_native_teacher_value_prefix_rejects_unknown_group() -> None:
    native = _require_native()

    with pytest.raises(ValueError, match="frozen prefix group"):
        _native_features(native, ProgressiveState.initial(), 8)


def test_native_teacher_value_work_receipt_matches_feature_call() -> None:
    native = _require_native()
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/4Q3/7K w - - 0 1",
        3,
    )
    board = state.board

    features, receipt = native.teacher_value_features_v3_with_receipt(
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
        board.occupied_co[chess.WHITE],
        board.occupied_co[chess.BLACK],
        board.promoted,
        board.clean_castling_rights(),
        board.turn,
        state.series_number,
        state.ep_targets,
        256,
        47,
    )

    assert tuple(features) == _native_features(native, state)
    assert set(receipt) == {
        "white_reach_positions",
        "black_reach_positions",
        "direct_move_variants",
        "two_move_variants",
        "white_reach_complete",
        "black_reach_complete",
    }
    assert all(
        type(receipt[name]) is int and receipt[name] >= 0
        for name in (
            "white_reach_positions",
            "black_reach_positions",
            "direct_move_variants",
            "two_move_variants",
        )
    )
    assert receipt["direct_move_variants"] > 0
    assert receipt["two_move_variants"] > 0
    assert type(receipt["white_reach_complete"]) is bool
    assert type(receipt["black_reach_complete"]) is bool


@pytest.mark.parametrize("feature_count", [7, 14, 19, 38, 44, 47])
def test_native_deep_teacher_score_matches_frozen_integer_dot_product(
    feature_count: int,
) -> None:
    native = _require_native()
    features = tuple(range(-23, 24))
    coefficients = tuple(
        ((index % 7) - 3) * 91_000_003 for index in range(feature_count)
    )

    expected = sum(
        value * coefficient
        for value, coefficient in zip(features, coefficients, strict=False)
    )

    assert (
        native.deep_teacher_score_v1(features, coefficients, 1_000_000_000)
        == expected
    )


def test_native_deep_teacher_score_fails_closed_on_contract_drift() -> None:
    native = _require_native()
    features = (0,) * 47

    with pytest.raises(ValueError, match="frozen prefix group"):
        native.deep_teacher_score_v1(features, (1,) * 8, 1_000_000_000)
    with pytest.raises(ValueError, match="scale 1000000000"):
        native.deep_teacher_score_v1(features, (1,) * 7, 100)


def test_native_deep_teacher_score_fails_closed_on_int64_overflow() -> None:
    native = _require_native()
    features = (2,) * 47
    coefficients = ((1 << 63) - 1,) + (0,) * 6

    with pytest.raises(OverflowError, match="signed 64-bit"):
        native.deep_teacher_score_v1(features, coefficients, 1_000_000_000)


@pytest.mark.parametrize(
    (
        "mover_white",
        "left_score",
        "left_proof",
        "left_notation",
        "right_score",
        "right_proof",
        "right_notation",
        "expected",
    ),
    [
        (True, 10, (-1, 1), "b2b3", 100, (-1, -1), "a2a3", True),
        (False, -10, (-1, 1), "b7b6", -100, (1, 1), "a7a6", True),
        (True, 100, (-1, -1), "b2b3", 90, (-1, -1), "a2a3", True),
        (False, -100, (1, 1), "b7b6", -90, (1, 1), "a7a6", True),
        (True, 10, (0, 1), "b2b3", 9, (-1, 1), "a2a3", True),
        (False, -10, (-1, 0), "b7b6", -9, (-1, 1), "a7a6", True),
        (True, 10, (-1, 1), "a2a3", 10, (-1, 1), "b2b3", True),
        (True, 10, (-1, 1), "b2b3", 10, (-1, 1), "a2a3", False),
    ],
)
def test_native_proof_aware_root_comparator_matches_frozen_policy(
    mover_white: bool,
    left_score: int,
    left_proof: tuple[int, int],
    left_notation: str,
    right_score: int,
    right_proof: tuple[int, int],
    right_notation: str,
    expected: bool,
) -> None:
    native = _require_native()

    assert native.proof_aware_root_precedes_v1(
        mover_white,
        left_score,
        left_proof,
        left_notation,
        right_score,
        right_proof,
        right_notation,
    ) is expected


def test_native_proof_aware_root_comparator_fails_closed_on_bad_contract() -> None:
    native = _require_native()

    with pytest.raises(TypeError, match="exact bool"):
        native.proof_aware_root_precedes_v1(
            1, 0, (-1, 1), "a", 0, (-1, 1), "b"
        )
    with pytest.raises(ValueError, match=r"\[-1, 1\]"):
        native.proof_aware_root_precedes_v1(
            True, 0, (-2, 1), "a", 0, (-1, 1), "b"
        )
