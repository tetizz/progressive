from __future__ import annotations

from dataclasses import asdict
import random

import chess
import pytest

import scottish_progressive.evaluation as evaluation
from scottish_progressive.model import ProgressiveState
from scottish_progressive.rules import (
    GenerationStats,
    GenerationWorkLimit,
    _native_expanded_move_variants,
    _native_legal_move_variants,
    _python_legal_move_variants,
    generate_series,
)


def _require_native_series() -> None:
    if (
        not evaluation.native_acceleration_available()
        or not hasattr(evaluation._native_eval, "expand_legal_move_variants")
        or not hasattr(evaluation._native_eval, "has_legal_move")
    ):
        pytest.skip("source-matched native series kernel is not built")


def _move_signature(
    variants: list[tuple[chess.Move, int | None]],
) -> list[tuple[str, int | None]]:
    return [(move.uci(), required_ep) for move, required_ep in variants]


def _python_expansion_signature(
    board: chess.Board,
    ep_targets: tuple[int, ...],
) -> list[tuple[object, ...]]:
    signature: list[tuple[object, ...]] = []
    for move, required_ep in _python_legal_move_variants(board, ep_targets):
        child = board.copy(stack=False)
        child.ep_square = required_ep
        san = child.san(move)
        piece = child.piece_at(move.from_square)
        is_pawn_move = piece is not None and piece.piece_type == chess.PAWN
        is_capture = child.is_capture(move)
        child.push(move)
        signature.append(
            (
                move.uci(),
                required_ep,
                child.fen(en_passant="fen"),
                child.promoted,
                san,
                is_pawn_move,
                is_capture,
                child.is_check(),
            )
        )
    return signature


def _native_expansion_signature(
    board: chess.Board,
    ep_targets: tuple[int, ...],
) -> list[tuple[object, ...]]:
    expanded = _native_expanded_move_variants(board, ep_targets)
    assert expanded is not None
    return [
        (
            item.move.uci(),
            item.required_ep,
            item.board.fen(en_passant="fen"),
            item.board.promoted,
            item.san,
            item.is_pawn_move,
            item.is_capture,
            item.delivered_check,
        )
        for item in expanded
    ]


@pytest.mark.parametrize(
    ("fen", "ep_targets"),
    [
        (chess.STARTING_FEN, ()),
        ("4k3/q3R3/8/8/8/8/8/4K3 b - - 0 1", ()),
        ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", ()),
        ("4r1k1/8/8/8/8/8/4R3/4K3 w - - 0 1", ()),
        ("8/P6k/8/8/8/8/8/7K w - - 0 1", ()),
        (
            "7k/8/8/pPpP4/8/8/8/K7 w - - 0 1",
            (chess.A6, chess.C6),
        ),
    ],
)
def test_native_series_kernel_matches_python_rule_edges(
    fen: str,
    ep_targets: tuple[int, ...],
) -> None:
    _require_native_series()
    board = chess.Board(fen)
    native_moves = _native_legal_move_variants(board, ep_targets)
    assert native_moves is not None
    assert _move_signature(native_moves) == _move_signature(
        _python_legal_move_variants(board, ep_targets)
    )
    assert _native_expansion_signature(
        board, ep_targets
    ) == _python_expansion_signature(board, ep_targets)


def test_native_series_kernel_matches_random_orthodox_positions() -> None:
    _require_native_series()
    rng = random.Random(20_260_823)
    checked = 0
    for _ in range(12):
        board = chess.Board()
        for _ in range(40):
            ep_targets = (
                (board.ep_square,) if board.ep_square is not None else ()
            )
            native_moves = _native_legal_move_variants(board, ep_targets)
            assert native_moves is not None
            assert _move_signature(native_moves) == _move_signature(
                _python_legal_move_variants(board, ep_targets)
            )
            assert _native_expansion_signature(
                board, ep_targets
            ) == _python_expansion_signature(board, ep_targets)
            checked += 1
            legal = list(board.legal_moves)
            if not legal:
                break
            board.push(rng.choice(legal))
    assert checked >= 400


def _series_signature(results) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            result.machine_notation,
            result.san,
            result.final_state.pfen,
            result.ended_by_check,
            result.outcome,
            result.unused_moves,
            result.transposition_count,
        )
        for result in results
    )


def test_native_series_generation_preserves_work_budget_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_native_series()
    state = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    native_stats = GenerationStats()
    native = generate_series(
        state,
        stats=native_stats,
        max_frontier_states=8,
        max_positions=300,
        frontier_score=lambda _board: 0,
    )

    monkeypatch.setattr(evaluation, "_native_eval", None)
    python_stats = GenerationStats()
    python = generate_series(
        state,
        stats=python_stats,
        max_frontier_states=8,
        max_positions=300,
        frontier_score=lambda _board: 0,
    )

    assert _series_signature(native) == _series_signature(python)
    assert asdict(native_stats) == asdict(python_stats)


def test_native_series_generation_preserves_exact_work_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_native_series()
    state = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    native_stats = GenerationStats()
    with pytest.raises(GenerationWorkLimit):
        generate_series(
            state,
            stats=native_stats,
            max_frontier_states=8,
            max_positions=40,
            frontier_score=lambda _board: 0,
        )

    monkeypatch.setattr(evaluation, "_native_eval", None)
    python_stats = GenerationStats()
    with pytest.raises(GenerationWorkLimit):
        generate_series(
            state,
            stats=python_stats,
            max_frontier_states=8,
            max_positions=40,
            frontier_score=lambda _board: 0,
        )
    assert asdict(native_stats) == asdict(python_stats)


def test_native_series_generation_keeps_unbounded_series_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_native_series()
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/6N1/K7 w - - 0 1",
        101,
    )
    native_stats = GenerationStats()
    native = generate_series(
        state,
        stats=native_stats,
        max_frontier_states=1,
        max_positions=101,
        frontier_score=None,
    )

    monkeypatch.setattr(evaluation, "_native_eval", None)
    python_stats = GenerationStats()
    python = generate_series(
        state,
        stats=python_stats,
        max_frontier_states=1,
        max_positions=101,
        frontier_score=None,
    )
    assert native
    assert all(result.used_moves == 101 for result in native)
    assert _series_signature(native) == _series_signature(python)
    assert asdict(native_stats) == asdict(python_stats)
