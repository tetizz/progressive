from __future__ import annotations

import chess

from experiments.stockfish.stockfish_policy import (
    OrthodoxCandidate,
    _parse_info_candidate,
    select_stockfish_series,
)
from scottish_progressive.model import Outcome, ProgressiveState


class ScriptedAnalyzer:
    def __init__(self, moves: tuple[str, ...]) -> None:
        self.moves = iter(moves)
        self.calls: list[str] = []

    @property
    def engine_id(self) -> str:
        return "fake-stockfish"

    def candidates(
        self, fen: str, *, nodes: int, multipv: int
    ) -> tuple[OrthodoxCandidate, ...]:
        self.calls.append(fen)
        return (OrthodoxCandidate(next(self.moves), score_cp=20),)


def test_info_parser_keeps_root_move_score_and_work() -> None:
    parsed = _parse_info_candidate(
        "info depth 13 seldepth 16 multipv 2 score cp -37 nodes 4096 pv e2e4 e7e5"
    )
    assert parsed == OrthodoxCandidate(
        "e2e4", rank=2, score_cp=-37, depth=13, nodes=4096
    )


def test_policy_replays_complete_series_through_scottish_rules() -> None:
    after_e4 = ProgressiveState.from_fen(
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        2,
    )
    analyzer = ScriptedAnalyzer(("e7e5", "g8f6"))
    result = select_stockfish_series(after_e4, analyzer, nodes_per_move=8)
    assert result.series.moves == ("e7e5", "g8f6")
    assert result.series.final_state.series_number == 3
    assert result.engine_calls == 2
    assert not any(decision.used_fallback for decision in result.decisions)


def test_policy_stops_on_scottish_early_check() -> None:
    state = ProgressiveState.from_fen(
        "4k3/8/8/8/8/8/R7/4K3 w - - 0 1",
        3,
    )
    analyzer = ScriptedAnalyzer(("a2e2",))
    result = select_stockfish_series(state, analyzer, nodes_per_move=8)
    assert result.series.moves == ("a2e2",)
    assert result.series.ended_by_check
    assert result.series.unused_moves == 2
    assert len(analyzer.calls) == 1


def test_untrusted_illegal_output_fails_closed_to_project_legal_move() -> None:
    analyzer = ScriptedAnalyzer(("a1a8",))
    result = select_stockfish_series(
        ProgressiveState.initial(), analyzer, nodes_per_move=8
    )
    assert result.series.moves == ("a2a3",)
    assert result.decisions[0].used_fallback
    assert result.series.moves[0] in result.decisions[0].legal_moves


def test_policy_queries_every_progressive_en_passant_target() -> None:
    state = ProgressiveState.from_fen(
        "4k3/8/8/3pPpP1/8/8/8/4K3 w - - 0 1",
        3,
        ep_targets=(chess.D6, chess.F6),
    )
    # Both queries recommend the same ordinary checking move. The purpose is
    # to prove that neither progressive e.p. target is silently dropped.
    analyzer = ScriptedAnalyzer(("e1e2", "e1e2", "e2e3", "e3e4"))
    result = select_stockfish_series(state, analyzer, nodes_per_move=8)
    assert len(result.decisions[0].fen_queries) == 2
    first_query_eps = {
        chess.Board(fen).ep_square for fen in result.decisions[0].fen_queries
    }
    assert first_query_eps == {chess.D6, chess.F6}


def test_scripted_tactical_anchor_is_validated_as_mate() -> None:
    state = ProgressiveState.from_fen(
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3",
        4,
    )
    analyzer = ScriptedAnalyzer(("c7c6", "d8b6", "f6e4", "b6f2"))
    result = select_stockfish_series(state, analyzer, nodes_per_move=8)
    assert result.series.outcome == Outcome.CHECKMATE
    assert result.series.moves == ("c7c6", "d8b6", "f6e4", "b6f2")
