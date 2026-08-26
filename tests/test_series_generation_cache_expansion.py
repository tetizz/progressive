from __future__ import annotations

import chess
import pytest

import scottish_progressive.search as search_module
from scottish_progressive.model import ProgressiveState
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.search import SearchLimits, SeriesSearcher


def _searcher(*, width: int = 8) -> SeriesSearcher:
    return SeriesSearcher(
        SearchLimits(
            depth_series=4,
            max_series_per_node=width,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        baseline_profile(),
    )


def _materialized_by_notation(items: object) -> dict[str, object]:
    return {
        item.machine_notation: (
            item.materialize() if hasattr(item, "materialize") else item
        )
        for item in items
    }


def test_expanded_cache_keeps_clock_promoted_and_chess960_keys_exact() -> None:
    searcher = _searcher()

    clock_a = ProgressiveState.from_fen(chess.STARTING_FEN, 1)
    clock_b = ProgressiveState.from_fen(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 9 7",
        1,
    )
    assert clock_a.transposition_key == clock_b.transposition_key
    clock_a_items = _materialized_by_notation(
        searcher._ordered_generated(clock_a, ply_from_root=1)
    )
    clock_b_items = _materialized_by_notation(
        searcher._ordered_generated(clock_b, ply_from_root=1)
    )
    clock_notation = sorted(clock_a_items.keys() & clock_b_items.keys())[0]
    assert (
        clock_a_items[clock_notation].final_state.board.fullmove_number
        != clock_b_items[clock_notation].final_state.board.fullmove_number
    )
    assert searcher.stats.series_generation_cache_hits == 0
    assert len(searcher._series_generation_cache) == 2

    searcher = _searcher()
    ordinary_board = chess.Board("7k/8/8/8/8/8/Q7/7K w - - 0 1")
    promoted_board = ordinary_board.copy(stack=False)
    promoted_board.promoted |= chess.BB_A2
    ordinary = ProgressiveState(ordinary_board, series_number=5)
    promoted = ProgressiveState(promoted_board, series_number=5)
    assert ordinary.transposition_key == promoted.transposition_key
    ordinary_items = _materialized_by_notation(
        searcher._ordered_generated(ordinary, ply_from_root=1)
    )
    promoted_items = _materialized_by_notation(
        searcher._ordered_generated(promoted, ply_from_root=1)
    )
    promoted_notation = sorted(ordinary_items.keys() & promoted_items.keys())[0]
    assert (
        ordinary_items[promoted_notation].final_state.board.promoted
        != promoted_items[promoted_notation].final_state.board.promoted
    )
    assert searcher.stats.series_generation_cache_hits == 0
    assert len(searcher._series_generation_cache) == 2

    searcher = _searcher()
    orthodox_board = chess.Board()
    chess960_board = orthodox_board.copy(stack=False)
    chess960_board.chess960 = True
    orthodox = ProgressiveState(orthodox_board, series_number=1)
    chess960 = ProgressiveState(chess960_board, series_number=1)
    assert orthodox.transposition_key == chess960.transposition_key
    orthodox_items = _materialized_by_notation(
        searcher._ordered_generated(orthodox, ply_from_root=1)
    )
    chess960_items = _materialized_by_notation(
        searcher._ordered_generated(chess960, ply_from_root=1)
    )
    chess960_notation = sorted(orthodox_items.keys() & chess960_items.keys())[0]
    assert not orthodox_items[chess960_notation].final_state.board.chess960
    assert chess960_items[chess960_notation].final_state.board.chess960

    # None of the byte-distinct concrete-state pairs may hit an earlier entry.
    assert searcher.stats.series_generation_cache_hits == 0
    assert len(searcher._series_generation_cache) == 2


def test_interrupted_generation_does_not_publish_a_partial_cache_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProgressiveState.initial()
    searcher = _searcher(width=4)
    original_generate = searcher._generate

    def interrupt_after_generation(*args: object, **kwargs: object) -> object:
        original_generate(*args, **kwargs)
        raise search_module._WorkLimit

    monkeypatch.setattr(searcher, "_generate", interrupt_after_generation)
    with pytest.raises(search_module._WorkLimit):
        searcher._ordered_generated(state, ply_from_root=1)

    assert searcher._series_generation_cache == {}
    assert searcher._series_generation_cache_weight == 0

    monkeypatch.setattr(searcher, "_generate", original_generate)
    completed = searcher._ordered_generated(state, ply_from_root=1)
    assert completed
    assert len(searcher._series_generation_cache) == 1
    assert searcher._series_generation_cache_weight == len(completed)


def test_interrupted_pvs_rolls_back_tt_but_keeps_only_complete_exact_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProgressiveState.initial()
    searcher = _searcher(width=4)
    key = searcher._tt_key(state)
    original = search_module._TTEntry(
        depth=1,
        score=7,
        bound=search_module.Bound.EXACT,
        pv=(),
        proof_bounds=search_module.UNKNOWN_PROOF_BOUNDS,
    )
    speculative = search_module._TTEntry(
        depth=2,
        score=8,
        bound=search_module.Bound.LOWER,
        pv=(),
        proof_bounds=search_module.UNKNOWN_PROOF_BOUNDS,
    )
    searcher._tt[key] = original

    def interrupt(*_args: object) -> object:
        completed = searcher._ordered_generated(state, ply_from_root=2)
        assert completed
        searcher._write_tt(key, speculative)
        raise search_module._Timeout

    monkeypatch.setattr(searcher, "_minimax", interrupt)
    with pytest.raises(search_module._Timeout):
        searcher._search_root_child_with_pvs(
            state,
            2,
            -100,
            100,
            1,
            parent_mover=chess.WHITE,
            has_prior_child=True,
        )

    assert searcher._tt[key] is original
    assert searcher._tt_transaction_stack == []
    assert len(searcher._series_generation_cache) == 1

    cached = searcher._ordered_generated(state, ply_from_root=2)
    assert searcher.stats.series_generation_cache_hits == 1
    concrete = (
        cached[0].materialize()
        if hasattr(cached[0], "materialize")
        else cached[0]
    )
    replayed = play_series(state, concrete.moves)
    assert replayed.final_state.pfen == concrete.final_state.pfen


def test_production_capacity_matches_desktop_browser_geometry() -> None:
    assert search_module.SERIES_GENERATION_CACHE_CAPACITY == 65_536
