from __future__ import annotations

import random

import chess

from scottish_progressive.model import ProgressiveState


def _assert_same_equivalence_class(states: list[ProgressiveState]) -> None:
    old_to_new: dict[tuple[int, str, int, int], tuple[object, ...]] = {}
    new_to_old: dict[tuple[object, ...], tuple[int, str, int, int]] = {}
    for state in states:
        old = state.transposition_key
        new = state.search_key
        assert old_to_new.setdefault(old, new) == new
        assert new_to_old.setdefault(new, old) == old


def test_search_key_preserves_clock_promoted_and_chess960_equivalence() -> None:
    clock_a = ProgressiveState.from_fen(chess.STARTING_FEN, 1)
    clock_b = ProgressiveState.from_fen(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 19 23",
        1,
    )
    assert clock_a.transposition_key == clock_b.transposition_key
    assert clock_a.search_key == clock_b.search_key

    ordinary_board = chess.Board("4k3/8/8/8/8/8/1Q6/4K3 w - - 0 1")
    promoted_board = ordinary_board.copy(stack=False)
    promoted_board.promoted = chess.BB_B2
    ordinary = ProgressiveState(ordinary_board, series_number=1)
    promoted = ProgressiveState(promoted_board, series_number=1)
    assert ordinary.transposition_key == promoted.transposition_key
    assert ordinary.search_key == promoted.search_key

    orthodox_board = chess.Board()
    chess960_board = orthodox_board.copy(stack=False)
    chess960_board.chess960 = True
    orthodox = ProgressiveState(orthodox_board, series_number=1)
    chess960 = ProgressiveState(chess960_board, series_number=1)
    assert orthodox.transposition_key == chess960.transposition_key
    assert orthodox.search_key == chess960.search_key


def test_search_key_changes_for_every_clockless_progressive_identity_field() -> None:
    baseline = ProgressiveState.initial()
    variants = [
        ProgressiveState.from_fen(
            "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            2,
        ),
        ProgressiveState(chess.Board(), series_number=1, quiet_series=1),
        ProgressiveState.from_fen(
            "4k3/8/8/8/3pP3/8/8/4K3 b - - 0 1",
            2,
            ep_targets=(chess.E3,),
        ),
    ]
    for variant in variants:
        assert variant.transposition_key != baseline.transposition_key
        assert variant.search_key != baseline.search_key


def test_search_key_matches_verified_transposition_equivalence_randomly() -> None:
    rng = random.Random(20260822)
    states: list[ProgressiveState] = []
    seen: set[str] = set()
    while len(states) < 128:
        series_number = rng.randrange(1, 9)
        board = chess.Board(None)
        board.turn = chess.WHITE if series_number % 2 else chess.BLACK
        squares = rng.sample(list(chess.SQUARES), 6)
        board.set_piece_at(squares[0], chess.Piece(chess.KING, chess.WHITE))
        board.set_piece_at(squares[1], chess.Piece(chess.KING, chess.BLACK))
        board.set_piece_at(
            squares[2],
            chess.Piece(rng.choice((chess.QUEEN, chess.ROOK)), chess.WHITE),
        )
        board.set_piece_at(
            squares[3],
            chess.Piece(rng.choice((chess.QUEEN, chess.ROOK)), chess.BLACK),
        )
        board.set_piece_at(
            squares[4],
            chess.Piece(rng.choice((chess.BISHOP, chess.KNIGHT)), chess.WHITE),
        )
        board.set_piece_at(
            squares[5],
            chess.Piece(rng.choice((chess.BISHOP, chess.KNIGHT)), chess.BLACK),
        )
        board.halfmove_clock = rng.randrange(20)
        board.fullmove_number = rng.randrange(1, 30)
        if not board.is_valid() or board.is_game_over(claim_draw=False):
            continue
        state = ProgressiveState(board, series_number=series_number)
        if state.pfen in seen:
            continue
        seen.add(state.pfen)
        states.append(state)
        clock_variant = state.copy()
        clock_variant.board.halfmove_clock += 31
        clock_variant.board.fullmove_number += 17
        states.append(clock_variant)

    _assert_same_equivalence_class(states)


def test_search_key_reflects_public_board_mutation_without_retained_state() -> None:
    state = ProgressiveState.initial()
    before = state.search_key

    state.board.push(chess.Move.from_uci("e2e4"))

    assert state.search_key != before


def test_search_key_ignores_inert_orthodox_ep_mutation_like_public_key() -> None:
    state = ProgressiveState.from_fen(
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        2,
    )
    mutated = state.copy()
    mutated.board.ep_square = chess.E3

    assert mutated.board.is_valid()
    assert mutated.transposition_key == state.transposition_key
    assert mutated.search_key == state.search_key


def test_search_key_canonicalizes_progressive_ep_target_order() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/pPpP4/8/8/8/K7 w - - 0 1",
        3,
        ep_targets=(chess.A6, chess.C6),
    )
    reordered = state.copy()
    reordered.ep_targets = (chess.C6, chess.A6)

    assert reordered.transposition_key == state.transposition_key
    assert reordered.search_key == state.search_key
