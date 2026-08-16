from __future__ import annotations

import pytest

from scottish_progressive.model import Outcome, ProgressiveState
from scottish_progressive.rules import SeriesLegalityError, play_series
from scottish_progressive.rules import generate_series
from scottish_progressive.theory import PUBLISHED_REPLY_CANDIDATES


@pytest.mark.parametrize(
    ("fen", "series_number", "moves"),
    [
        (
            "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3",
            4,
            ("c7c6", "d8b6", "f6e4", "b6f2"),
        ),
        (
            "rn1qkb1r/ppp1pppp/5n2/3P4/8/5N2/PPPP1PPP/RNBbK2R w KQkq - 0 7",
            5,
            ("f3e5", "g2g4", "g4g5", "g5g6", "g6f7"),
        ),
        (
            "bnq1nr2/p1pp1pk1/8/4PP2/1P2P1p1/8/P1P2KP1/BNbBN2r w - - 0 1",
            7,
            ("e1f3", "f3d4", "e5e6", "e6e7", "e7f8r", "f8h8", "d4e6"),
        ),
        (
            "7R/pp3p1p/1p3k2/3P4/1b6/5P2/PPP2P1P/RNK5 b - - 0 1",
            8,
            ("b4d6", "b6b5", "b5b4", "b4b3", "b3a2", "a2b1n", "b1c3", "d6f4"),
        ),
        (
            "rnbqkb1r/ppp1pppp/5n2/3p2B1/3P4/2N2N2/PPP1PPPP/R2QKB1R b KQkq - 4 3",
            4,
            ("f6e4", "d8d6", "d6g3", "g3f2"),
        ),
    ],
)
def test_published_tactical_series_are_legal_mates(
    fen: str, series_number: int, moves: tuple[str, ...]
) -> None:
    result = play_series(ProgressiveState.from_fen(fen, series_number), moves)
    assert result.outcome == Outcome.CHECKMATE
    assert result.ended_by_check
    assert result.used_moves == series_number


def test_scottish_countercheck_refutes_an_italian_mate_construction() -> None:
    black = ProgressiveState.from_fen(
        "8/1p2k1p1/8/8/5Pp1/3Q4/1P2K1P1/q5N1 b - - 1 21", 10
    )
    construction = play_series(
        black,
        (
            "a1g1",
            "g1c1",
            "g4g3",
            "g7g5",
            "g5f4",
            "e7f6",
            "f6g5",
            "g5g4",
            "b7b6",
            "f4f3",
        ),
    )
    assert construction.final_state.series_number == 11
    assert construction.final_state.board.is_check()

    countercheck = play_series(construction.final_state, ("d3f3",))
    assert countercheck.ended_by_check
    assert countercheck.unused_moves == 10
    assert countercheck.final_state.series_number == 12


def test_moves_after_a_check_are_rejected() -> None:
    state = ProgressiveState.from_fen(
        "4k3/q3R3/8/8/8/8/8/4K3 b - - 0 1", 2
    )
    with pytest.raises(SeriesLegalityError, match="check ended the series"):
        play_series(state, ("a7e7", "e7e1"))


def test_every_published_reply_candidate_is_a_complete_legal_series() -> None:
    first_moves = {
        result.moves[0]: result
        for result in generate_series(ProgressiveState.initial())
    }
    for first_uci, candidates in PUBLISHED_REPLY_CANDIDATES.items():
        for _, moves, _ in candidates:
            result = play_series(first_moves[first_uci].final_state, moves)
            assert result.used_moves == 2 or result.ended_by_check
