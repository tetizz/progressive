from __future__ import annotations

import chess
import pytest

from scottish_progressive.model import Outcome, ProgressiveState, boundary_fen
from scottish_progressive.rules import (
    GenerationStats,
    SeriesLegalityError,
    _FrontierState,
    _board_position_key,
    _bound_frontier,
    _merge_frontier_states,
    _merged_series_generation,
    generate_series,
    play_series,
)


def find_series(results, *moves: str):
    wanted = tuple(moves)
    return next(result for result in results if result.moves == wanted)


def test_initial_position_has_all_20_first_moves() -> None:
    results = generate_series(ProgressiveState.initial())
    assert len(results) == 20
    assert {result.moves[0] for result in results} == {
        move.uci() for move in chess.Board().legal_moves
    }


def test_repeated_movement_of_same_piece_is_legal() -> None:
    e4 = find_series(generate_series(ProgressiveState.initial()), "e2e4")
    results = generate_series(e4.final_state, merge_transpositions=False)
    repeated_pawn = find_series(results, "a7a5", "a5a4")
    assert repeated_pawn.notation == "a5 / a4"


def test_early_check_ends_series_and_forfeits_unused_move() -> None:
    state = ProgressiveState.from_fen(
        "4k3/q3R3/8/8/8/8/8/4K3 b - - 0 1", 2
    )
    result = find_series(generate_series(state), "a7e7")
    assert result.ended_by_check
    assert result.used_moves == 1
    assert result.unused_moves == 1
    assert result.final_state.series_number == 3
    assert result.final_state.board.is_check()


def test_first_move_countercheck_is_legal_scottish_evasion() -> None:
    state = ProgressiveState.from_fen(
        "4k3/q3R3/8/8/8/8/8/4K3 b - - 0 1", 2
    )
    assert state.board.is_check()
    result = find_series(generate_series(state), "a7e7")
    assert result.san == ("Qxe7+",)
    assert result.ended_by_check


def test_forced_discovered_countercheck_ends_checked_players_series() -> None:
    state = ProgressiveState.from_fen(
        "r7/k6R/8/K7/8/8/8/8 b - - 0 1", 2
    )
    results = generate_series(state)
    assert len(results) == 1
    assert results[0].moves == ("a7b8",)
    assert results[0].san == ("Kb8+",)
    assert results[0].unused_moves == 1
    assert results[0].ended_by_check


def test_starting_in_check_all_generated_first_moves_escape() -> None:
    state = ProgressiveState.from_fen(
        "4k3/q3R3/8/8/8/8/8/4K3 b - - 0 1", 2
    )
    for result in generate_series(state, merge_transpositions=False):
        if not result.moves:
            continue
        board = state.board.copy(stack=False)
        board.push_uci(result.moves[0])
        board.turn = chess.BLACK
        assert not board.is_check()


def test_en_passant_survives_intervening_move_and_only_reply_move_one() -> None:
    black = ProgressiveState.from_fen(
        "7k/p7/8/1P6/8/8/8/K7 b - - 0 1", 2
    )
    black_series = find_series(generate_series(black), "a7a5", "h8h7")
    assert black_series.final_state.ep_targets == (chess.A6,)

    white_results = generate_series(
        black_series.final_state, merge_transpositions=False
    )
    assert any(result.moves[0] == "b5a6" for result in white_results)
    assert not any("b5a6" in result.moves[1:] for result in white_results)


def test_multiple_progressive_en_passant_targets_are_preserved() -> None:
    black = ProgressiveState.from_fen(
        "7k/p1p5/8/1P1P4/8/8/8/K7 b - - 0 1", 2
    )
    result = find_series(generate_series(black), "a7a5", "c7c5")
    assert result.final_state.ep_targets == (chess.A6, chess.C6)
    assert result.transposition_count == 2

    white_results = generate_series(result.final_state, merge_transpositions=False)
    first_moves = {series.moves[0] for series in white_results}
    assert {"b5a6", "b5c6", "d5c6"} <= first_moves


def test_progressive_en_passant_reply_downgrades_orthodox_mate_san() -> None:
    state = ProgressiveState.from_fen(
        "8/8/8/8/1p5p/7b/2PB1KPk/7b w - - 0 1",
        3,
    )

    result = play_series(state, ("g2g4", "c2c4", "d2f4"))
    generated = find_series(
        generate_series(state, merge_transpositions=False),
        "g2g4",
        "c2c4",
        "d2f4",
    )

    assert result.ended_by_check
    assert result.outcome is None
    assert result.san == ("g4", "c4", "Bf4+")
    assert generated.san == result.san
    assert generated.outcome is None
    assert result.final_state.ep_targets == (chess.G3,)
    replies = generate_series(result.final_state, merge_transpositions=False)
    assert any(reply.moves[0] == "h4g3" for reply in replies)


def test_moving_double_stepped_pawn_again_removes_ep_eligibility() -> None:
    black = ProgressiveState.from_fen(
        "7k/p7/8/1P6/8/8/8/K7 b - - 0 1", 2
    )
    result = find_series(generate_series(black), "a7a5", "a5a4")
    assert result.final_state.ep_targets == ()


def test_promotion_piece_may_move_again_when_promotion_does_not_check() -> None:
    state = ProgressiveState.from_fen(
        "8/P6k/8/8/8/8/8/7K w - - 0 1", 3
    )
    results = generate_series(state, merge_transpositions=False)
    assert any(
        result.moves[0] == "a7a8q"
        and len(result.moves) == 3
        and any(move.startswith("a8") for move in result.moves[1:])
        for result in results
    )


def test_promotion_with_check_truncates_series() -> None:
    state = ProgressiveState.from_fen(
        "7k/P7/8/8/8/8/8/7K w - - 0 1", 3
    )
    raw = generate_series(state, merge_transpositions=False)
    queen = find_series(raw, "a7a8q")
    rook = find_series(raw, "a7a8r")
    assert queen.ended_by_check and queen.unused_moves == 2
    assert rook.ended_by_check and rook.unused_moves == 2


def test_castling_is_one_move_and_play_can_continue() -> None:
    state = ProgressiveState.from_fen(
        "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", 3
    )
    results = generate_series(state, merge_transpositions=False)
    assert any(result.moves[0] == "e1g1" and len(result.moves) == 3 for result in results)


def test_castling_through_attacked_square_is_rejected() -> None:
    state = ProgressiveState.from_fen(
        "k4r2/8/8/8/8/8/8/4K2R w K - 0 1", 1
    )
    first_moves = {result.moves[0] for result in generate_series(state)}
    assert "e1g1" not in first_moves


def test_illegal_king_exposure_is_never_generated() -> None:
    state = ProgressiveState.from_fen(
        "4r1k1/8/8/8/8/8/4R3/4K3 w - - 0 1", 1
    )
    first_moves = {result.moves[0] for result in generate_series(state)}
    assert "e2a2" not in first_moves
    assert "e2e8" in first_moves


def test_checkmate_ends_game_without_capturing_king() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1", 1
    )
    mate = find_series(generate_series(state), "g6g7")
    assert mate.outcome == Outcome.CHECKMATE
    assert mate.ended_by_check
    assert mate.final_state.board.king(chess.BLACK) == chess.H8


def test_stalemate_at_series_start_is_draw() -> None:
    state = ProgressiveState.from_fen(
        "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1", 2
    )
    results = generate_series(state)
    assert len(results) == 1
    assert results[0].moves == ()
    assert results[0].outcome == Outcome.STALEMATE


def test_progressive_stalemate_can_happen_mid_series() -> None:
    state = ProgressiveState.from_fen(
        "8/8/8/8/8/p7/8/k1K5 b - - 0 1", 2
    )
    result = find_series(generate_series(state), "a3a2")
    assert result.outcome == Outcome.STALEMATE
    assert result.used_moves == 1
    assert result.unused_moves == 1
    assert not result.ended_by_check


def test_ten_quiet_series_flags_proof_required_adjudication_and_pawn_reset() -> None:
    quiet = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/6R1/K7 w - - 0 1", 1, quiet_series=9
    )
    rook_move = generate_series(quiet)[0]
    assert rook_move.outcome is None
    assert rook_move.final_state.quiet_draw_pending

    pawn = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/P7/K7 w - - 0 1", 1, quiet_series=9
    )
    pawn_result = find_series(generate_series(pawn), "a2a3")
    assert pawn_result.final_state.quiet_series == 0
    assert pawn_result.outcome is None


def test_reconstructed_ten_quiet_series_state_remains_searchable_for_mate_exception() -> None:
    pending = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/6R1/K7 w - - 0 1", 1, quiet_series=10
    )
    results = generate_series(pending)
    assert pending.quiet_draw_pending
    assert any(result.moves for result in results)


def test_checkmate_takes_precedence_over_quiet_draw_boundary() -> None:
    mate = ProgressiveState.from_fen(
        "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1", 2, quiet_series=10
    )
    assert generate_series(mate)[0].outcome == Outcome.CHECKMATE


def test_quiet_draw_exception_does_not_erase_immediate_mate() -> None:
    white = ProgressiveState.from_fen(
        "8/8/8/8/8/8/8/K1kq4 w - - 0 1", 1, quiet_series=9
    )
    after_king = play_series(white, ("a1a2",))
    assert after_king.final_state.quiet_draw_pending
    mate = play_series(after_king.final_state, ("d1a4",))
    assert mate.outcome == Outcome.CHECKMATE


def test_transposition_merge_counts_different_move_orders() -> None:
    e4 = find_series(generate_series(ProgressiveState.initial()), "e2e4")
    stats = GenerationStats()
    results = generate_series(e4.final_state, stats=stats)
    target = next(
        result
        for result in results
        if result.final_state.board.piece_at(chess.A6) == chess.Piece(chess.PAWN, chess.BLACK)
        and result.final_state.board.piece_at(chess.B6) == chess.Piece(chess.PAWN, chess.BLACK)
    )
    assert target.transposition_count == 2
    assert stats.transpositions_merged > 0


def test_dynamic_transposition_generation_matches_raw_enumeration() -> None:
    e4 = find_series(generate_series(ProgressiveState.initial()), "e2e4")
    raw = generate_series(e4.final_state, merge_transpositions=False)
    stats = GenerationStats()
    merged = generate_series(e4.final_state, stats=stats)

    def key(result):
        return (
            result.final_state.transposition_key,
            result.outcome,
            result.ended_by_check,
        )

    raw_counts = {}
    for result in raw:
        raw_counts[key(result)] = raw_counts.get(key(result), 0) + 1
    assert {key(result) for result in merged} == set(raw_counts)
    assert {
        key(result): result.transposition_count for result in merged
    } == raw_counts
    assert stats.raw_series == len(raw) == 446
    assert stats.unique_series == len(merged)


def test_in_memory_position_key_matches_boundary_fen_identity() -> None:
    clock_only = chess.Board()
    clock_only.halfmove_clock = 37
    clock_only.fullmove_number = 19
    black_to_move = chess.Board()
    black_to_move.turn = chess.BLACK
    no_castling = chess.Board()
    no_castling.castling_rights = chess.BB_EMPTY
    after_e4 = chess.Board()
    after_e4.push_uci("e2e4")
    progressive_ep = chess.Board("7k/8/8/pP6/8/8/8/K7 w - - 0 1")
    samples = [
        (chess.Board(), ()),
        (clock_only, ()),
        (black_to_move, ()),
        (no_castling, ()),
        (after_e4, ()),
        (progressive_ep, (chess.A6,)),
        (progressive_ep, ()),
    ]

    for left_board, left_ep in samples:
        for right_board, right_ep in samples:
            assert (
                _board_position_key(left_board, left_ep)
                == _board_position_key(right_board, right_ep)
            ) == (
                boundary_fen(left_board, left_ep)
                == boundary_fen(right_board, right_ep)
            )


def test_frontier_scorer_charges_each_distinct_partial_state_once() -> None:
    state = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    stats = GenerationStats()
    scored_keys: list[str] = []

    def score(board: chess.Board) -> int:
        scored_keys.append(board.fen(en_passant="fen"))
        return 0

    results = generate_series(
        state,
        stats=stats,
        max_frontier_states=8,
        frontier_score=score,
    )

    assert results
    assert stats.frontier_score_positions == len(scored_keys)
    assert stats.frontier_score_positions > 0
    assert len(scored_keys) == len(set(scored_keys))


def test_tactical_frontier_reserve_is_bounded_and_mate_check_first() -> None:
    moves = (
        "a2a3",
        "b2b3",
        "c2c3",
        "d2d3",
        "e2e3",
        "f2f3",
        "g2g3",
        "h2h3",
    )
    frontier: dict[tuple[int], _FrontierState] = {}
    for index, move in enumerate(moves):
        board = chess.Board()
        board.fullmove_number = index + 1
        frontier[(index,)] = _FrontierState(
            board,
            (move,),
            (move,),
            {},
            False,
        )

    # The first four are the static/quiet leaders.  The last four expose one
    # distinct tactical class each but are intentionally scored last.
    opportunities = {
        5: ((0, "a1a8"),),  # mate
        6: ((1, "b1b8"),),  # non-mating early check
        7: ((2, "c7c8q"),),  # promotion
        8: ((3, "d4e5"),),  # capture
    }

    def score(board: chess.Board) -> int:
        return 100 - board.fullmove_number

    def tactical(board: chess.Board) -> tuple[tuple[int, str], ...]:
        return opportunities.get(board.fullmove_number, ())

    narrow_stats = GenerationStats()
    narrow = _bound_frontier(
        frontier,
        mover=chess.WHITE,
        prefix_length=0,
        max_frontier_states=1,
        frontier_score=score,
        tactical_opportunities=tactical,
        counters=narrow_stats,
    )
    narrow_moves = {item.moves[0] for item in narrow}

    assert len(narrow) == 3  # cap 1 plus at most 2 tactical extras
    assert "a2a3" in narrow_moves
    assert {"e2e3", "f2f3"} <= narrow_moves  # mate and check win reserve ties
    assert "b2b3" not in narrow_moves
    assert "g2g3" not in narrow_moves
    assert "h2h3" not in narrow_moves
    assert narrow_stats.frontier_prunes == 1
    assert narrow_stats.frontier_states_pruned == 5
    assert narrow_stats.tactical_frontier_states_retained == 2
    assert narrow_stats.tactical_frontier_reserve_drops == 2

    full_stats = GenerationStats()
    full = _bound_frontier(
        frontier,
        mover=chess.WHITE,
        prefix_length=0,
        max_frontier_states=4,
        frontier_score=score,
        tactical_opportunities=tactical,
        counters=full_stats,
    )
    assert len(full) == 8  # cap 4 plus all 4 tactical representatives
    assert full_stats.frontier_prunes == 1
    assert full_stats.frontier_states_pruned == 0
    assert full_stats.tactical_frontier_states_retained == 4
    assert full_stats.tactical_frontier_reserve_drops == 0


def test_tactical_frontier_key_is_immediate_move_uci_not_path_identity() -> None:
    frontier: dict[tuple[int], _FrontierState] = {}
    for index, move in enumerate(("a2a3", "b2b3", "c2c3")):
        board = chess.Board()
        board.fullmove_number = index + 1
        frontier[(index,)] = _FrontierState(
            board,
            (move,),
            (move,),
            {},
            False,
        )

    def tactical(board: chess.Board) -> tuple[tuple[int, str], ...]:
        # Two different paths expose the same immediately legal capture.  The
        # reserve keeps only the better-scored representative for that UCI at
        # this layer; it does not treat whole path notation as a distinct key.
        return ((3, "d4e5"),) if board.fullmove_number >= 2 else ()

    stats = GenerationStats()
    selected = _bound_frontier(
        frontier,
        mover=chess.WHITE,
        prefix_length=0,
        max_frontier_states=1,
        frontier_score=lambda board: 10 - board.fullmove_number,
        tactical_opportunities=tactical,
        counters=stats,
    )

    assert [item.moves[0] for item in selected] == ["a2a3", "b2b3"]
    assert stats.tactical_frontier_states_retained == 1
    assert stats.tactical_frontier_reserve_drops == 0


def test_played_capture_provenance_survives_a_later_quiet_prune() -> None:
    quiet_board = chess.Board()
    quiet_board.fullmove_number = 1
    capture_then_quiet_board = chess.Board()
    capture_then_quiet_board.fullmove_number = 20
    frontier = {
        (0,): _FrontierState(
            quiet_board,
            ("a2a3", "a3a4"),
            ("a3", "a4"),
            {},
            False,
        ),
        (1,): _FrontierState(
            capture_then_quiet_board,
            ("a4b5", "b5b6"),
            ("axb5", "b6"),
            {},
            True,
            1,
            ((3, "1:a4b5"),),
        ),
    }
    stats = GenerationStats()

    selected = _bound_frontier(
        frontier,
        mover=chess.WHITE,
        prefix_length=0,
        max_frontier_states=1,
        frontier_score=lambda board: 100 - board.fullmove_number,
        tactical_opportunities=lambda _board: (),
        counters=stats,
    )

    assert [item.moves for item in selected] == [
        ("a2a3", "a3a4"),
        ("a4b5", "b5b6"),
    ]
    assert stats.tactical_frontier_states_retained == 1


def test_partial_transposition_unions_tactical_provenance() -> None:
    board = chess.Board()
    canonical = _FrontierState(
        board,
        ("a2a3", "b2b3"),
        ("a3", "b3"),
        {},
        True,
        2,
        ((3, "1:a2b3"),),
    )
    alternate = _FrontierState(
        board.copy(stack=False),
        ("b2b3", "a2a3"),
        ("b3", "a3"),
        {},
        True,
        3,
        ((2, "1:b7b8q"),),
    )

    merged = _merge_frontier_states(canonical, alternate)

    assert merged.moves == canonical.moves
    assert merged.path_count == 5
    assert merged.tactical_provenance == (
        (2, "1:b7b8q"),
        (3, "1:a2b3"),
    )


def test_completed_transposition_unions_noncanonical_capture_orders() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/1n4n1/8/1R1K2R1 w - - 0 1",
        3,
    )
    stats = GenerationStats()
    results = _merged_series_generation(
        state,
        stats,
        required_prefix=(),
        max_frontier_states=None,
        max_positions=None,
        frontier_score=None,
        # A non-None tactical inspector opts this Python oracle generation
        # into provenance tracking. Immediate opportunities are irrelevant to
        # this uncapped fixture; the played captures are recorded directly.
        tactical_opportunities=lambda _board: (),
        should_stop=None,
    )
    representative = find_series(
        results,
        "b1b3",
        "d1e1",
        "g1g3",
    )
    provenance = results.tactical_provenance_by_notation[
        representative.machine_notation
    ]

    assert representative.transposition_count == 6
    assert (3, "1:g1g3") in provenance
    assert (3, "2:b1b3") in provenance
    assert (3, "3:b1b3") in provenance
    assert (3, "1:b1b3") in provenance
    assert (3, "2:g1g3") in provenance
    assert (3, "3:g1g3") in provenance


@pytest.mark.parametrize("merge_transpositions", [True, False])
def test_required_prefix_is_replayed_before_move_order_merging(
    merge_transpositions: bool,
) -> None:
    state = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    stats = GenerationStats()
    results = generate_series(
        state,
        merge_transpositions=merge_transpositions,
        required_prefix=("h2h3",),
        stats=stats,
    )

    assert results
    assert all(result.moves[0] == "h2h3" for result in results)
    assert any(
        result.moves == ("h2h3", "a2a3", "b2b3") for result in results
    )
    assert stats.required_prefix_moves == 1


def test_required_prefix_cannot_continue_after_check() -> None:
    state = ProgressiveState.from_fen(
        "4k3/q3R3/8/8/8/8/8/4K3 b - - 0 1", 2
    )
    with pytest.raises(SeriesLegalityError, match="continues after check"):
        generate_series(state, required_prefix=("a7e7", "e7e1"))


def test_full_progressive_hash_distinguishes_series_budget() -> None:
    first = ProgressiveState.from_fen(chess.STARTING_FEN, 1)
    third = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    assert first.boundary_key == third.boundary_key
    assert first.transposition_key != third.transposition_key


def test_no_artificial_maximum_series_length() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/6N1/K7 w - - 0 1", 101
    )
    assert state.moves_available == 101


def test_side_and_series_parity_must_match() -> None:
    with pytest.raises(ValueError, match="series 2 belongs to Black"):
        ProgressiveState.from_fen(chess.STARTING_FEN, 2)


def test_invalid_progressive_en_passant_target_is_rejected() -> None:
    with pytest.raises(ValueError, match="no matching double-stepped pawn"):
        ProgressiveState.from_fen(
            "7k/8/8/8/8/8/8/K7 w - - 0 1",
            3,
            ep_targets=(chess.A6,),
        )


def test_irrelevant_or_blocked_ep_right_does_not_split_canonical_state() -> None:
    state = ProgressiveState.from_fen(
        "7k/p7/7r/1P6/8/8/8/K7 b - - 0 1", 4
    )
    first = play_series(state, ("a7a5", "h6g6", "g6h6", "h6a6"))
    second = play_series(state, ("a7a6", "a6a5", "h6g6", "g6a6"))
    assert first.final_state.ep_targets == second.final_state.ep_targets == ()
    assert first.final_state.transposition_key == second.final_state.transposition_key
