from __future__ import annotations

from dataclasses import dataclass
import struct

import chess

from .model import ProgressiveState
from .native_corpus import NativeFullGameRecord, NativeTerminal


NATIVE_BOUNDARY_SAMPLE_SCHEMA = "spc-native-boundary-outcome-v1"
NATIVE_BOUNDARY_SAMPLE_MAGIC = b"SPCNBO01"
NATIVE_BOUNDARY_SAMPLE_VERSION = 1
_FLAG_WHITE_TO_MOVE = 1
_FLAG_CHESS960 = 2
_KNOWN_FLAGS = _FLAG_WHITE_TO_MOVE | _FLAG_CHESS960
_SAMPLE = struct.Struct("<8sHHI12Q5QHHBbH")
NATIVE_BOUNDARY_SAMPLE_SIZE = _SAMPLE.size

assert NATIVE_BOUNDARY_SAMPLE_SIZE == 160


class NativeBoundarySampleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NativeBoundarySample:
    state: ProgressiveState
    white_profile_index: int
    black_profile_index: int
    terminal: NativeTerminal
    value_for_side_to_move: int

    def __post_init__(self) -> None:
        if not isinstance(self.state, ProgressiveState):
            raise TypeError("state must be a ProgressiveState")
        for name in ("white_profile_index", "black_profile_index"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 4095:
                raise ValueError(f"{name} must be between 0 and 4095")
        try:
            terminal = NativeTerminal(self.terminal)
        except (TypeError, ValueError) as error:
            raise ValueError("sample terminal is invalid") from error
        if terminal is NativeTerminal.NONE:
            raise ValueError("training sample terminal cannot be NONE")
        object.__setattr__(self, "terminal", terminal)
        if self.value_for_side_to_move not in (-1, 0, 1):
            raise ValueError("sample value must be -1, 0, or 1")
        if terminal in (NativeTerminal.STALEMATE, NativeTerminal.TEN_SERIES_DRAW):
            if self.value_for_side_to_move != 0:
                raise ValueError("drawn samples must have value 0")
        else:
            winner = _terminal_winner(terminal)
            expected = 1 if self.state.board.turn == winner else -1
            if self.value_for_side_to_move != expected:
                raise ValueError(
                    "checkmate sample value disagrees with winner and side to move"
                )


def _terminal_winner(terminal: NativeTerminal) -> chess.Color | None:
    if terminal is NativeTerminal.CHECKMATE_WHITE:
        return chess.WHITE
    if terminal is NativeTerminal.CHECKMATE_BLACK:
        return chess.BLACK
    return None


def sample_from_native_game(
    state: ProgressiveState,
    record: NativeFullGameRecord,
) -> NativeBoundarySample:
    if not isinstance(record, NativeFullGameRecord) or not record.accepted:
        raise NativeBoundarySampleError("sample source must be an accepted native game")
    winner = _terminal_winner(record.terminal)
    value = 0 if winner is None else (1 if state.board.turn == winner else -1)
    return NativeBoundarySample(
        state=state,
        white_profile_index=record.white_profile_index,
        black_profile_index=record.black_profile_index,
        terminal=record.terminal,
        value_for_side_to_move=value,
    )


def encode_native_boundary_sample(sample: NativeBoundarySample) -> bytes:
    if not isinstance(sample, NativeBoundarySample):
        raise TypeError("sample must be a NativeBoundarySample")
    state = sample.state
    board = state.board
    flags = (
        (_FLAG_WHITE_TO_MOVE if board.turn == chess.WHITE else 0)
        | (_FLAG_CHESS960 if board.chess960 else 0)
    )
    piece_masks = tuple(
        board.pieces_mask(piece_type, color)
        for color in (chess.WHITE, chess.BLACK)
        for piece_type in range(chess.PAWN, chess.KING + 1)
    )
    ep_mask = sum(chess.BB_SQUARES[square] for square in state.ep_targets)
    return _SAMPLE.pack(
        NATIVE_BOUNDARY_SAMPLE_MAGIC,
        NATIVE_BOUNDARY_SAMPLE_VERSION,
        NATIVE_BOUNDARY_SAMPLE_SIZE,
        flags,
        *piece_masks,
        board.clean_castling_rights(),
        board.promoted,
        state.series_number,
        state.quiet_series,
        ep_mask,
        sample.white_profile_index,
        sample.black_profile_index,
        int(sample.terminal),
        sample.value_for_side_to_move,
        0,
    )


def decode_native_boundary_sample(payload: bytes | bytearray | memoryview) -> NativeBoundarySample:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("sample payload must be bytes-like")
    data = bytes(payload)
    if len(data) != NATIVE_BOUNDARY_SAMPLE_SIZE:
        raise NativeBoundarySampleError("native boundary sample size is invalid")
    values = _SAMPLE.unpack(data)
    magic, version, header_size, flags = values[:4]
    if (
        magic != NATIVE_BOUNDARY_SAMPLE_MAGIC
        or version != NATIVE_BOUNDARY_SAMPLE_VERSION
        or header_size != NATIVE_BOUNDARY_SAMPLE_SIZE
        or flags & ~_KNOWN_FLAGS
    ):
        raise NativeBoundarySampleError("native boundary sample header is invalid")
    piece_masks = values[4:16]
    (
        castling_rights,
        promoted,
        series_number,
        quiet_series,
        ep_mask,
        white_profile_index,
        black_profile_index,
        terminal_value,
        value_for_side_to_move,
        reserved,
    ) = values[16:]
    if reserved != 0:
        raise NativeBoundarySampleError("native boundary sample reserved field is nonzero")
    board = chess.Board(None, chess960=bool(flags & _FLAG_CHESS960))
    for mask, (color, piece_type) in zip(
        piece_masks,
        (
            (color, piece_type)
            for color in (chess.WHITE, chess.BLACK)
            for piece_type in range(chess.PAWN, chess.KING + 1)
        ),
        strict=True,
    ):
        for square in chess.scan_forward(mask):
            if board.piece_at(square) is not None:
                raise NativeBoundarySampleError("native boundary sample pieces overlap")
            board.set_piece_at(square, chess.Piece(piece_type, color))
    board.turn = bool(flags & _FLAG_WHITE_TO_MOVE)
    board.castling_rights = castling_rights
    board.promoted = promoted
    board.halfmove_clock = 0
    board.fullmove_number = 1
    if promoted & ~board.occupied or promoted & (board.pawns | board.kings):
        raise NativeBoundarySampleError(
            "native boundary sample promoted provenance is invalid"
        )
    if board.clean_castling_rights() != castling_rights:
        raise NativeBoundarySampleError(
            "native boundary sample castling rights are invalid"
        )
    ep_targets = tuple(chess.scan_forward(ep_mask))
    try:
        state = ProgressiveState(
            board,
            series_number=series_number,
            quiet_series=quiet_series,
            ep_targets=ep_targets,
        )
        terminal = NativeTerminal(terminal_value)
        return NativeBoundarySample(
            state=state,
            white_profile_index=white_profile_index,
            black_profile_index=black_profile_index,
            terminal=terminal,
            value_for_side_to_move=value_for_side_to_move,
        )
    except (TypeError, ValueError) as error:
        raise NativeBoundarySampleError(f"native boundary sample state is invalid: {error}") from error
