from __future__ import annotations

from dataclasses import replace
import struct

import chess
import pytest

from scottish_progressive.corpus_samples import (
    NATIVE_BOUNDARY_SAMPLE_SIZE,
    NativeBoundarySample,
    NativeBoundarySampleError,
    decode_native_boundary_sample,
    encode_native_boundary_sample,
    sample_from_native_game,
)
from scottish_progressive.corpus_shards import progressive_state_dedup_key
from scottish_progressive.model import ProgressiveState
from scottish_progressive.native_corpus import (
    NativeFullGameRecord,
    NativeReject,
    NativeTerminal,
)


def _record(terminal: NativeTerminal = NativeTerminal.CHECKMATE_WHITE) -> NativeFullGameRecord:
    return NativeFullGameRecord(
        attempt_index=5,
        terminal=terminal,
        reject=NativeReject.NONE,
        white_profile_index=2,
        black_profile_index=3,
        logical_work=12,
        path_count_saturations=0,
        series=(("e2e4",),),
    )


def test_fixed_sample_round_trips_the_full_progressive_state() -> None:
    board = chess.Board("4k3/8/8/3pP3/8/8/8/3QK3 w - - 0 1")
    board.promoted = chess.BB_D1
    state = ProgressiveState(
        board,
        series_number=3,
        quiet_series=2,
        ep_targets=(chess.D6,),
    )
    sample = sample_from_native_game(state, _record())
    payload = encode_native_boundary_sample(sample)
    decoded = decode_native_boundary_sample(payload)
    assert len(payload) == NATIVE_BOUNDARY_SAMPLE_SIZE == 160
    assert progressive_state_dedup_key(decoded.state) == progressive_state_dedup_key(
        state
    )
    assert decoded.state.board.promoted == chess.BB_D1
    assert decoded.state.ep_targets == (chess.D6,)
    assert decoded.value_for_side_to_move == 1
    assert decoded.white_profile_index == 2
    assert decoded.black_profile_index == 3


def test_sample_value_is_from_the_boundary_side_to_move() -> None:
    white = ProgressiveState.initial()
    black_board = chess.Board()
    black_board.turn = chess.BLACK
    black = ProgressiveState(black_board, series_number=2)
    record = _record(NativeTerminal.CHECKMATE_WHITE)
    assert sample_from_native_game(white, record).value_for_side_to_move == 1
    assert sample_from_native_game(black, record).value_for_side_to_move == -1
    draw = _record(NativeTerminal.TEN_SERIES_DRAW)
    assert sample_from_native_game(white, draw).value_for_side_to_move == 0


def test_decisive_sample_rejects_a_reversed_side_to_move_label() -> None:
    with pytest.raises(ValueError, match="disagrees with winner"):
        NativeBoundarySample(
            state=ProgressiveState.initial(),
            white_profile_index=0,
            black_profile_index=0,
            terminal=NativeTerminal.CHECKMATE_WHITE,
            value_for_side_to_move=-1,
        )


def test_sample_decoder_rejects_corruption_and_noncanonical_records() -> None:
    payload = bytearray(
        encode_native_boundary_sample(
            sample_from_native_game(ProgressiveState.initial(), _record())
        )
    )
    payload[-1] = 1
    with pytest.raises(NativeBoundarySampleError, match="reserved"):
        decode_native_boundary_sample(payload)
    with pytest.raises(NativeBoundarySampleError, match="size"):
        decode_native_boundary_sample(payload[:-1])
    ordinary = bytearray(
        encode_native_boundary_sample(
            sample_from_native_game(ProgressiveState.initial(), _record())
        )
    )
    struct.pack_into("<Q", ordinary, 120, chess.BB_A3)
    with pytest.raises(NativeBoundarySampleError, match="promoted provenance"):
        decode_native_boundary_sample(ordinary)
    ordinary = bytearray(
        encode_native_boundary_sample(
            sample_from_native_game(ProgressiveState.initial(), _record())
        )
    )
    struct.pack_into("<Q", ordinary, 112, chess.BB_A3)
    with pytest.raises(NativeBoundarySampleError, match="castling rights"):
        decode_native_boundary_sample(ordinary)
    with pytest.raises(NativeBoundarySampleError, match="accepted"):
        sample_from_native_game(
            ProgressiveState.initial(),
            replace(
                _record(),
                terminal=NativeTerminal.NONE,
                reject=NativeReject.WORK_LIMIT,
                series=(),
            ),
        )
