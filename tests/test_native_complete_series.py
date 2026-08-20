from __future__ import annotations

from dataclasses import asdict

import chess
import pytest

import scottish_progressive.evaluation as evaluation
import scottish_progressive.rules as rules
from scottish_progressive.model import Outcome, ProgressiveState, SeriesResult
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import (
    GenerationCancelled,
    GenerationStats,
    GenerationWorkLimit,
    NativeFinalSeriesScoreConfig,
    NativeFrontierScoreConfig,
    SeriesLegalityError,
    generate_series,
    play_series,
)
from scottish_progressive.search import MATE_SCORE, SearchLimits, SeriesSearcher


def _require_native_complete_series() -> object:
    native = evaluation._native_eval
    if native is None or not hasattr(native, "generate_complete_series"):
        pytest.skip("source-matched native complete-series kernel is not built")
    return native


def _series_signature(results) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            result.moves,
            result.san,
            result.final_state.pfen,
            result.ended_by_check,
            result.outcome,
            result.unused_moves,
            result.transposition_count,
        )
        for result in results
    )


def _paired_generation(
    monkeypatch: pytest.MonkeyPatch,
    state: ProgressiveState,
    *,
    required_prefix: tuple[str, ...] = (),
    max_frontier_states: int | None = None,
    max_positions: int | None = None,
    structured_score: bool = False,
):
    native = _require_native_complete_series()
    score = (
        NativeFrontierScoreConfig.from_profile(state, baseline_profile())
        if structured_score
        else None
    )
    native_stats = GenerationStats()
    oracle_generator = rules._merged_series_generation

    class BulkWitness:
        calls = 0

        def __getattr__(self, name: str):
            return getattr(native, name)

        def generate_complete_series(self, *args):
            self.calls += 1
            return native.generate_complete_series(*args)

    witness = BulkWitness()
    monkeypatch.setattr(evaluation, "_native_eval", witness)

    def bulk_required(*_args, **_kwargs):
        raise AssertionError("supported generation bypassed the bulk native API")

    monkeypatch.setattr(rules, "_merged_series_generation", bulk_required)
    native_results = generate_series(
        state,
        stats=native_stats,
        required_prefix=required_prefix,
        max_frontier_states=max_frontier_states,
        max_positions=max_positions,
        frontier_score=score,
    )
    assert witness.calls == 1

    monkeypatch.setattr(rules, "_merged_series_generation", oracle_generator)
    monkeypatch.setattr(evaluation, "_native_eval", None)
    oracle_stats = GenerationStats()
    oracle_results = generate_series(
        state,
        stats=oracle_stats,
        required_prefix=required_prefix,
        max_frontier_states=max_frontier_states,
        max_positions=max_positions,
        frontier_score=score,
    )
    monkeypatch.setattr(evaluation, "_native_eval", native)
    return native_results, native_stats, oracle_results, oracle_stats


def _paired_native_final_cap(
    monkeypatch: pytest.MonkeyPatch,
    state: ProgressiveState,
    *,
    final_cap: int,
    ply_from_root: int,
    required_prefix: tuple[str, ...] = (),
    max_frontier_states: int = 32,
):
    native = _require_native_complete_series()
    profile = baseline_profile()
    frontier_score = NativeFrontierScoreConfig.from_profile(state, profile)
    final_score = NativeFinalSeriesScoreConfig.from_profile(
        profile,
        max_returned_series=final_cap,
        ply_from_root=ply_from_root,
        mate_score=MATE_SCORE,
    )
    oracle_generator = rules._merged_series_generation

    class BulkWitness:
        calls = 0

        def __getattr__(self, name: str):
            return getattr(native, name)

        def generate_complete_series(self, *args):
            self.calls += 1
            return native.generate_complete_series(*args)

    witness = BulkWitness()
    monkeypatch.setattr(evaluation, "_native_eval", witness)

    def bulk_required(*_args, **_kwargs):
        raise AssertionError("native final pre-cap bypassed bulk generation")

    monkeypatch.setattr(rules, "_merged_series_generation", bulk_required)
    native_stats = GenerationStats()
    native_results = generate_series(
        state,
        stats=native_stats,
        required_prefix=required_prefix,
        max_frontier_states=max_frontier_states,
        frontier_score=frontier_score,
        native_final_score=final_score,
    )
    assert witness.calls == 1

    monkeypatch.setattr(rules, "_merged_series_generation", oracle_generator)
    monkeypatch.setattr(evaluation, "_native_eval", None)
    oracle_stats = GenerationStats()
    oracle_results = generate_series(
        state,
        stats=oracle_stats,
        required_prefix=required_prefix,
        max_frontier_states=max_frontier_states,
        frontier_score=frontier_score,
        native_final_score=final_score,
    )
    expected = SeriesSearcher(
        SearchLimits(depth_series=1, max_series_per_node=final_cap),
        profile,
    )._ordered(oracle_results, state.board.turn, ply_from_root)
    monkeypatch.setattr(evaluation, "_native_eval", native)

    assert _series_signature(native_results) == _series_signature(expected)
    assert asdict(native_stats) == asdict(oracle_stats)
    return native_results, native_stats, oracle_results


def _direct_bulk_call(
    native: object,
    state: ProgressiveState,
    *,
    max_frontier_states: int | None,
    max_positions: int | None = None,
    required_prefix: tuple[str, ...] = (),
):
    board = state.board
    return native.generate_complete_series(
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
        board.halfmove_clock,
        board.fullmove_number,
        state.series_number,
        state.quiet_series,
        state.ep_targets,
        required_prefix,
        max_frontier_states,
        max_positions,
        None,
        None,
    )


def test_bulk_complete_series_matches_python_rules_and_structured_ordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = {
        "early-check": (
            ProgressiveState.from_fen(
                "4k3/q3R3/8/8/8/8/8/4K3 b - - 0 1",
                2,
            ),
            (),
            4,
        ),
        "progressive-stalemate": (
            ProgressiveState.from_fen(
                "8/8/8/8/8/p7/8/k1K5 b - - 0 1",
                2,
            ),
            (),
            4,
        ),
        "multi-ep": (
            ProgressiveState.from_fen(
                "7k/p1p5/8/1P1P4/8/8/8/K7 b - - 0 1",
                2,
            ),
            (),
            8,
        ),
        "inherited-multi-ep": (
            ProgressiveState.from_fen(
                "7k/8/8/pPpP4/8/8/8/K7 w - - 0 1",
                3,
                ep_targets=(chess.A6, chess.C6),
            ),
            (),
            8,
        ),
        "promotion-reuse": (
            ProgressiveState.from_fen(
                "8/P6k/8/8/8/8/8/7K w - - 0 1",
                3,
            ),
            ("a7a8n",),
            4,
        ),
        "promotion-check": (
            ProgressiveState.from_fen(
                "7k/P7/8/8/8/8/8/7K w - - 0 1",
                3,
            ),
            (),
            4,
        ),
        "castling": (
            ProgressiveState.from_fen(
                "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
                3,
            ),
            ("e1g1",),
            4,
        ),
        "clocked-prefix": (
            ProgressiveState.from_fen(
                "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR "
                "w KQkq - 37 19",
                3,
            ),
            ("h2h3",),
            8,
        ),
        "black-clocks": (
            ProgressiveState.from_fen(
                "7k/8/8/8/8/8/6n1/K7 b - - 7 11",
                2,
                quiet_series=8,
            ),
            (),
            32,
        ),
        "clock-cache-revisit": (
            ProgressiveState.from_fen(
                "7k/8/8/8/8/8/6n1/K7 b - - 7 11",
                4,
            ),
            (),
            4,
        ),
    }

    generated: dict[str, list[SeriesResult]] = {}
    generated_stats: dict[str, GenerationStats] = {}
    for name, (state, prefix, cap) in cases.items():
        native, native_stats, oracle, oracle_stats = _paired_generation(
            monkeypatch,
            state,
            required_prefix=prefix,
            max_frontier_states=cap,
            structured_score=True,
        )
        assert _series_signature(native) == _series_signature(oracle), name
        assert asdict(native_stats) == asdict(oracle_stats), name
        assert native_stats.frontier_score_positions > 0, name
        generated[name] = native
        generated_stats[name] = native_stats

    assert any(
        result.moves == ("a7e7",) and result.ended_by_check
        for result in generated["early-check"]
    )
    assert any(
        result.moves == ("a3a2",) and result.outcome == Outcome.STALEMATE
        for result in generated["progressive-stalemate"]
    )
    assert any(
        result.moves == ("a7a5", "c7c5")
        and result.final_state.ep_targets == (chess.A6, chess.C6)
        and result.transposition_count == 2
        for result in generated["multi-ep"]
    )
    inherited = generated["inherited-multi-ep"]
    assert {"b5a6", "b5c6", "d5c6"} <= {
        result.moves[0] for result in inherited
    }
    assert not any(
        move in {"b5a6", "b5c6", "d5c6"}
        for result in inherited
        for move in result.moves[1:]
    )
    assert all(
        result.moves[0] == "a7a8n" for result in generated["promotion-reuse"]
    )
    assert any(
        result.moves[1].startswith("a8")
        for result in generated["promotion-reuse"]
    )
    assert all(
        any(
            result.moves == (promotion,)
            and result.ended_by_check
            and result.unused_moves == 2
            for result in generated["promotion-check"]
        )
        for promotion in ("a7a8q", "a7a8r")
    )
    assert all(result.moves[0] == "e1g1" for result in generated["castling"])
    assert all(
        result.moves[0] == "h2h3" for result in generated["clocked-prefix"]
    )
    assert len(
        {result.moves[1] for result in generated["clocked-prefix"]}
    ) == 8
    black_clock_result = next(
        result
        for result in generated["black-clocks"]
        if result.moves == ("g2e3", "e3g4")
    )
    assert black_clock_result.final_state.board.halfmove_clock == 9
    assert black_clock_result.final_state.board.fullmove_number == 13
    assert black_clock_result.final_state.quiet_series == 9
    # This S4 frontier revisits board layouts at different move clocks. The
    # Python FEN-keyed scorer treats those as distinct work, so the native
    # cache must carry clocks rather than collapsing them by board identity.
    assert generated_stats["clock-cache-revisit"].frontier_score_positions == 84


def test_bulk_forced_countercheck_and_required_prefix_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    countercheck = ProgressiveState.from_fen(
        "r7/k6R/8/K7/8/8/8/8 b - - 0 1",
        2,
    )
    native, native_stats, oracle, oracle_stats = _paired_generation(
        monkeypatch,
        countercheck,
        max_frontier_states=32,
        structured_score=True,
    )
    assert _series_signature(native) == _series_signature(oracle)
    assert asdict(native_stats) == asdict(oracle_stats)
    assert len(native) == 1
    assert native[0].moves == ("a7b8",)
    assert native[0].san == ("Kb8+",)
    assert native[0].ended_by_check
    assert native[0].unused_moves == 1

    starting = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    native, native_stats, oracle, oracle_stats = _paired_generation(
        monkeypatch,
        starting,
        required_prefix=("h2h3",),
        max_frontier_states=32,
        structured_score=True,
    )
    assert _series_signature(native) == _series_signature(oracle)
    assert asdict(native_stats) == asdict(oracle_stats)
    preserved = next(
        result
        for result in native
        if result.moves == ("h2h3", "a2a3", "b2b3")
    )
    assert preserved.transposition_count == 2
    assert native_stats.required_prefix_moves == 1


def test_native_final_pre_cap_matches_white_and_black_search_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    white = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    black = play_series(ProgressiveState.initial(), ("e2e4",)).final_state

    for state in (white, black):
        selected, stats, oracle = _paired_native_final_cap(
            monkeypatch,
            state,
            final_cap=5,
            ply_from_root=3,
        )
        assert len(selected) == 5
        assert stats.unique_series == len(oracle) > len(selected)


def test_native_final_pre_cap_terminal_ties_and_mate_distance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = ProgressiveState.from_fen(
        "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1",
        2,
    )
    selected, stats, oracle = _paired_native_final_cap(
        monkeypatch,
        checked,
        final_cap=4,
        ply_from_root=7,
        max_frontier_states=4,
    )
    assert len(selected) == stats.unique_series == len(oracle) == 1
    assert selected[0].moves == ()
    assert selected[0].outcome == Outcome.CHECKMATE
    assert not selected[0].ended_by_check

    draw = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K7 b - - 0 1",
        2,
        quiet_series=9,
    )
    selected, stats, oracle = _paired_native_final_cap(
        monkeypatch,
        draw,
        final_cap=3,
        ply_from_root=7,
        max_frontier_states=4,
    )
    assert stats.unique_series == len(oracle) > len(selected)
    assert all(result.outcome == Outcome.TEN_SERIES_DRAW for result in oracle)
    assert [result.machine_notation for result in selected] == sorted(
        result.machine_notation for result in oracle
    )[:3]

    mixed = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
        3,
    )
    ordinary, _stats, _oracle = _paired_native_final_cap(
        monkeypatch,
        mixed,
        final_cap=5,
        ply_from_root=7,
        max_frontier_states=8,
    )
    delayed, _stats, _oracle = _paired_native_final_cap(
        monkeypatch,
        mixed,
        final_cap=5,
        ply_from_root=MATE_SCORE + 1,
        max_frontier_states=8,
    )
    assert all(result.outcome == Outcome.CHECKMATE for result in ordinary)
    assert all(result.outcome is None for result in delayed)


def test_native_final_cap_preserves_prefix_representative_without_pruning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    selected, stats, oracle = _paired_native_final_cap(
        monkeypatch,
        state,
        required_prefix=("h2h3",),
        final_cap=10_000,
        ply_from_root=2,
    )
    assert len(selected) == stats.unique_series == len(oracle)
    representative = next(
        result
        for result in selected
        if result.moves == ("h2h3", "a2a3", "b2b3")
    )
    assert representative.transposition_count == 2


def test_bulk_quiet_draw_and_nonlex_transposition_representative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quiet = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K7 b - - 0 1",
        2,
        quiet_series=9,
    )
    native, native_stats, oracle, oracle_stats = _paired_generation(
        monkeypatch,
        quiet,
        max_frontier_states=4,
        structured_score=True,
    )
    assert _series_signature(native) == _series_signature(oracle)
    assert asdict(native_stats) == asdict(oracle_stats)
    assert native
    assert all(result.outcome == Outcome.TEN_SERIES_DRAW for result in native)
    assert all(result.final_state.quiet_series == 10 for result in native)

    castling = ProgressiveState.from_fen(
        "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
        3,
    )
    native, native_stats, oracle, oracle_stats = _paired_generation(
        monkeypatch,
        castling,
        required_prefix=("e1c1",),
        max_frontier_states=16,
    )
    assert _series_signature(native) == _series_signature(oracle)
    assert asdict(native_stats) == asdict(oracle_stats)
    representative = next(
        result
        for result in native
        if result.moves == ("e1c1", "d1f1", "f1e1")
    )
    assert representative.transposition_count == 3
    assert not any(result.moves == ("e1c1", "d1e1") for result in native)


def test_bulk_work_limit_matches_python_combined_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_native_complete_series()
    state = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    profile = baseline_profile()
    score = NativeFrontierScoreConfig.from_profile(state, profile)
    final_score = NativeFinalSeriesScoreConfig.from_profile(
        profile,
        max_returned_series=2,
        ply_from_root=1,
        mate_score=MATE_SCORE,
    )
    native_stats = GenerationStats()
    oracle_generator = rules._merged_series_generation

    def bulk_required(*_args, **_kwargs):
        raise AssertionError("work-limited request bypassed native bulk generation")

    monkeypatch.setattr(rules, "_merged_series_generation", bulk_required)
    with pytest.raises(GenerationWorkLimit):
        generate_series(
            state,
            stats=native_stats,
            max_frontier_states=8,
            max_positions=40,
            frontier_score=score,
            native_final_score=final_score,
        )

    monkeypatch.setattr(rules, "_merged_series_generation", oracle_generator)
    monkeypatch.setattr(evaluation, "_native_eval", None)
    oracle_stats = GenerationStats()
    with pytest.raises(GenerationWorkLimit):
        generate_series(
            state,
            stats=oracle_stats,
            max_frontier_states=8,
            max_positions=40,
            frontier_score=score,
            native_final_score=final_score,
        )
    assert asdict(native_stats) == asdict(oracle_stats)
    assert native_stats.positions_visited + native_stats.frontier_score_positions == 40


def test_native_u64_overflow_restarts_pristine_python_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_native_complete_series()
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K7 w - - 0 1",
        101,
    )
    status, _message, raw_stats, raw_series = _direct_bulk_call(
        native,
        state,
        max_frontier_states=4,
    )
    assert status == 3
    assert raw_series == ()
    assert raw_stats[0] > 0
    assert raw_stats[8] > 0

    native_stats = GenerationStats()
    native_results = generate_series(
        state,
        stats=native_stats,
        max_frontier_states=4,
    )
    monkeypatch.setattr(evaluation, "_native_eval", None)
    oracle_stats = GenerationStats()
    oracle_results = generate_series(
        state,
        stats=oracle_stats,
        max_frontier_states=4,
    )
    assert _series_signature(native_results) == _series_signature(oracle_results)
    assert asdict(native_stats) == asdict(oracle_stats)
    assert native_stats.raw_series > 2**64


def test_unusual_prefix_strings_keep_exact_python_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_native_complete_series()
    state = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    prefix = ("h2h3\0junk",)
    status, _message, _raw_stats, raw_series = _direct_bulk_call(
        native,
        state,
        required_prefix=prefix,
        max_frontier_states=1,
    )
    assert status == 2
    assert raw_series == ()

    stats = GenerationStats()
    with pytest.raises(SeriesLegalityError):
        generate_series(
            state,
            stats=stats,
            required_prefix=prefix,
            max_frontier_states=1,
        )
    assert stats.required_prefix_moves == 1
    assert stats.positions_visited == 1

    class NeverEqualString(str):
        def __hash__(self) -> int:
            return 0

        def __eq__(self, _other: object) -> bool:
            return False

    class BulkMustNotRun:
        def __getattr__(self, name: str):
            return getattr(native, name)

        def generate_complete_series(self, *_args):
            raise AssertionError("str subclass reached native prefix decoding")

    monkeypatch.setattr(evaluation, "_native_eval", BulkMustNotRun())
    with pytest.raises(SeriesLegalityError):
        generate_series(
            state,
            required_prefix=(NeverEqualString("h2h3"),),
            max_frontier_states=1,
        )


def test_native_series_range_and_optional_module_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_native_complete_series()
    high_native = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
        2_147_483_649,
    )
    native_results, native_stats, oracle_results, oracle_stats = _paired_generation(
        monkeypatch,
        high_native,
        required_prefix=("g6g7",),
        max_frontier_states=1,
    )
    assert _series_signature(native_results) == _series_signature(oracle_results)
    assert asdict(native_stats) == asdict(oracle_stats)
    assert native_results[0].outcome == Outcome.CHECKMATE

    class BulkMustNotRun:
        def __getattr__(self, name: str):
            return getattr(native, name)

        def generate_complete_series(self, *_args):
            raise AssertionError("out-of-range series reached the native kernel")

    monkeypatch.setattr(evaluation, "_native_eval", BulkMustNotRun())
    unbounded = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
        (1 << 63) + 1,
    )
    fallback = generate_series(
        unbounded,
        required_prefix=("g6g7",),
        max_frontier_states=1,
    )
    assert fallback[0].outcome == Outcome.CHECKMATE

    class MissingBulkSymbol:
        def __getattr__(self, name: str):
            if name == "generate_complete_series":
                raise AttributeError(name)
            return getattr(native, name)

    monkeypatch.setattr(evaluation, "_native_eval", MissingBulkSymbol())
    assert generate_series(ProgressiveState.initial())

    class StaleNative(BulkMustNotRun):
        SOURCE_IDENTITY = "stale-native-complete-series"

    assert evaluation._validated_native_module(StaleNative()) is None
    monkeypatch.setattr(evaluation, "_native_eval", StaleNative())
    assert generate_series(ProgressiveState.initial())

    chess960 = chess.Board(chess960=True)
    monkeypatch.setattr(evaluation, "_native_eval", BulkMustNotRun())
    assert generate_series(ProgressiveState(chess960, 1))


def test_unsupported_generation_modes_keep_python_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_native_complete_series()

    class BulkMustNotRun:
        def __getattr__(self, name: str):
            return getattr(native, name)

        def generate_complete_series(self, *_args):
            raise AssertionError("unsupported mode reached native bulk generation")

    monkeypatch.setattr(evaluation, "_native_eval", BulkMustNotRun())
    raw_state = ProgressiveState.initial()
    state = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    profile = baseline_profile()

    raw = generate_series(raw_state, merge_transpositions=False)
    assert raw

    scored = generate_series(
        state,
        max_frontier_states=2,
        frontier_score=lambda _board: 0,
    )
    assert scored

    mismatched_config = NativeFrontierScoreConfig.from_profile(
        ProgressiveState.initial(),
        baseline_profile(),
    )
    assert generate_series(
        state,
        max_frontier_states=2,
        frontier_score=mismatched_config,
    )

    assert generate_series(
        raw_state,
        max_frontier_states=(1 << 64) + 1,
    )

    with pytest.raises(SeriesLegalityError):
        generate_series(raw_state, required_prefix=(123,))  # type: ignore[arg-type]

    s1_frontier = NativeFrontierScoreConfig.from_profile(raw_state, profile)
    s1_final = NativeFinalSeriesScoreConfig.from_profile(
        profile,
        max_returned_series=2,
        ply_from_root=1,
        mate_score=MATE_SCORE,
    )
    s1_results = generate_series(
        raw_state,
        max_frontier_states=2,
        frontier_score=s1_frontier,
        native_final_score=s1_final,
    )
    assert len(s1_results) == 20

    class OverriddenFrontierScore(NativeFrontierScoreConfig):
        def __call__(self, _board: chess.Board) -> int:
            return 0

    overridden = OverriddenFrontierScore(
        state.series_number,
        state.quiet_series,
        profile.weights.material,
        profile.weights.king_space,
        profile.weights.promotion_corridors,
        profile.weights.immediate_vulnerability,
        profile.weights.boundary_check,
    )
    assert generate_series(
        state,
        max_frontier_states=2,
        frontier_score=overridden,
    )

    class DerivedFinalScore(NativeFinalSeriesScoreConfig):
        pass

    derived_final = DerivedFinalScore(
        2,
        1,
        MATE_SCORE,
        profile.weights.material,
        profile.weights.king_space,
        profile.weights.promotion_corridors,
        profile.weights.immediate_vulnerability,
        profile.weights.boundary_check,
    )
    uncapped = generate_series(
        state,
        max_frontier_states=2,
        frontier_score=NativeFrontierScoreConfig.from_profile(state, profile),
        native_final_score=derived_final,
    )
    assert len(uncapped) > derived_final.max_returned_series

    cancelled_stats = GenerationStats()
    with pytest.raises(GenerationCancelled):
        generate_series(
            state,
            stats=cancelled_stats,
            max_frontier_states=2,
            should_stop=lambda: True,
        )
    assert asdict(cancelled_stats) == asdict(GenerationStats())
