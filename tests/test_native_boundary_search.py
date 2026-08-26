from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import gc
import importlib.util
import os
from pathlib import Path
import sys
import time

import chess
import pytest

import scottish_progressive.evaluation as evaluation
import scottish_progressive.rules as rules_module
import scottish_progressive.search as search_module
from scottish_progressive.model import Outcome, ProgressiveState
from scottish_progressive.move_quality import AnalysisEvidence
from scottish_progressive.profiles import EvaluationWeights, baseline_profile
from scottish_progressive.rules import (
    NativeFinalSeriesScoreConfig,
    NativeFrontierScoreConfig,
    _NativeSeriesBatch,
    play_series,
)
from scottish_progressive.search import (
    MATE_SCORE,
    ScoredSeries,
    SearchLimits,
    SeriesSearcher,
    analyze,
)


def _require_n2_native() -> object:
    override = os.environ.get("SPC_N2_NATIVE_PATH")
    if override:
        path = Path(override).resolve()
        spec = importlib.util.spec_from_file_location(
            "scottish_progressive._native_eval",
            path,
        )
        if spec is None or spec.loader is None:
            raise AssertionError(f"cannot load native test module {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        native = module
    else:
        native = evaluation._native_eval
    if (
        native is None
        or not hasattr(native, "full_evaluate")
        or not hasattr(native, "prepare_complete_series")
        or not hasattr(native, "complete_series_candidate")
    ):
        pytest.skip("source-matched N2 native boundary APIs are not built")
    assert native.SOURCE_IDENTITY == evaluation._native_source_identity()
    return native


def _native_full(
    native: object,
    state: ProgressiveState,
    weights: EvaluationWeights,
    max_reach_positions: int,
) -> tuple[object, ...]:
    board = state.board
    return tuple(
        native.full_evaluate(
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
            max_reach_positions,
            weights.material,
            weights.king_space,
            weights.series_reach,
            weights.promotion_corridors,
            weights.immediate_vulnerability,
            weights.useful_mobility,
            weights.boundary_check,
        )
    )


def _evaluation_tuple(result: object) -> tuple[object, ...]:
    values = asdict(result)
    return (
        values["total"],
        values["material"],
        values["king_space"],
        values["series_reach"],
        values["promotion_corridors"],
        values["immediate_vulnerability"],
        values["useful_mobility"],
        values["boundary_check"],
        values["white_check_distance"],
        values["black_check_distance"],
        values["reach_complete"],
        values["white_reach_nodes"],
        values["black_reach_nodes"],
    )


@pytest.mark.parametrize(
    "state",
    [
        ProgressiveState.initial(),
        ProgressiveState.from_fen(chess.STARTING_FEN, 3),
        ProgressiveState.from_fen(
            "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R "
            "b KQkq - 1 3",
            4,
        ),
        ProgressiveState.from_fen(
            "7k/8/8/pPpP4/8/8/8/K7 w - - 0 1",
            3,
            ep_targets=(chess.A6, chess.C6),
        ),
    ],
)
@pytest.mark.parametrize("max_reach_positions", [0, 1, 17, 127, 128, 129, 256])
def test_direct_native_full_evaluation_matches_every_python_field(
    state: ProgressiveState,
    max_reach_positions: int,
) -> None:
    native = _require_n2_native()
    weights = EvaluationWeights(
        material=73,
        king_space=149,
        series_reach=211,
        promotion_corridors=97,
        immediate_vulnerability=131,
        useful_mobility=181,
        boundary_check=53,
    )
    expected = evaluation._python_evaluate(
        state,
        weights,
        max_reach_positions=max_reach_positions,
    )
    expected_tuple = _evaluation_tuple(expected)
    assert _native_full(
        native,
        state,
        weights,
        max_reach_positions,
    ) == expected_tuple
    prior = evaluation._native_eval
    evaluation._native_eval = native
    try:
        assert _evaluation_tuple(
            evaluation.evaluate(
                state,
                weights,
                max_reach_positions=max_reach_positions,
            )
        ) == expected_tuple
    finally:
        evaluation._native_eval = prior


def _batch_arguments(state: ProgressiveState) -> tuple[object, ...]:
    profile = baseline_profile()
    frontier = NativeFrontierScoreConfig.from_profile(state, profile)
    final = NativeFinalSeriesScoreConfig.from_profile(
        profile,
        max_returned_series=8,
        ply_from_root=2,
        mate_score=MATE_SCORE,
    )
    board = state.board
    return (
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
        (),
        8,
        250_000,
        (
            frontier.material,
            frontier.king_space,
            frontier.promotion_corridors,
            frontier.immediate_vulnerability,
            frontier.boundary_check,
        ),
        (
            final.max_returned_series,
            final.ply_from_root,
            final.mate_score,
            final.material,
            final.king_space,
            final.promotion_corridors,
            final.immediate_vulnerability,
            final.boundary_check,
        ),
    )


def _user_refutation_states() -> tuple[ProgressiveState, ProgressiveState]:
    state = play_series(ProgressiveState.initial(), ("g1f3",)).final_state
    s2 = state
    state = play_series(state, ("e7e6", "d8f6")).final_state
    s4 = play_series(
        state,
        ("d2d4", "c1g5", "g5f6"),
    ).final_state
    return s2, s4


class _TimedNativeWitness:
    def __init__(self, native: object) -> None:
        self.native = native
        self.statuses: list[int] = []
        self.calls = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self.native, name)

    def prepare_complete_series_timed(self, *args: object) -> object:
        self.calls += 1
        result = self.native.prepare_complete_series_timed(*args)
        self.statuses.append(int(result[0]))
        return result


class _ParallelNativeWitness(_TimedNativeWitness):
    def __init__(self, native: object) -> None:
        super().__init__(native)
        self.parallel_calls = 0

    def prepare_complete_series_timed_parallel(
        self,
        *args: object,
    ) -> object:
        self.parallel_calls += 1
        result = self.native.prepare_complete_series_timed_parallel(*args)
        self.statuses.append(int(result[0]))
        return result


def _prepared_batch_signature(
    native: object,
    prepared: object,
) -> tuple[object, ...]:
    status, message, raw_stats, raw_series, capsule = prepared
    canonical_series = tuple(raw_series)
    candidates = tuple(
        tuple(native.complete_series_candidate(capsule, index))
        for index in range(len(canonical_series))
    )
    return (
        int(status),
        str(message),
        tuple(raw_stats),
        canonical_series,
        candidates,
    )


def _heavy_s7_state() -> ProgressiveState:
    return ProgressiveState.from_fen(chess.STARTING_FEN, 7)


def _wide_batch_arguments(state: ProgressiveState) -> tuple[object, ...]:
    arguments = list(_batch_arguments(state))
    arguments[17] = None
    arguments[18] = 5_000_000
    final_score = list(arguments[20])
    final_score[0] = 32
    arguments[20] = tuple(final_score)
    return tuple(arguments)


def test_direct_native_batch_keeps_paths_stats_and_final_states_exact() -> None:
    native = _require_n2_native()
    state = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    arguments = _batch_arguments(state)
    ordinary = native.generate_complete_series(*arguments)
    prepared = native.prepare_complete_series(*arguments)
    assert tuple(prepared[:4]) == tuple(ordinary)
    assert int(prepared[0]) == 0

    capsule = prepared[4]
    outcome_codes = {
        None: 0,
        Outcome.CHECKMATE: 1,
        Outcome.STALEMATE: 2,
        Outcome.TEN_SERIES_DRAW: 3,
    }
    for index, (moves, count) in enumerate(prepared[3]):
        replayed = play_series(state, tuple(moves)).with_transposition_count(count)
        record = tuple(native.complete_series_candidate(capsule, index))
        board = replayed.final_state.board
        assert record == (
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
            replayed.final_state.series_number,
            replayed.final_state.quiet_series,
            replayed.final_state.ep_targets,
            outcome_codes[replayed.outcome],
            replayed.ended_by_check,
        )

    with pytest.raises(IndexError):
        native.complete_series_candidate(capsule, len(prepared[3]))


def test_direct_timed_native_batch_has_explicit_immediate_deadline_status() -> None:
    native = _require_n2_native()
    if not hasattr(native, "prepare_complete_series_timed"):
        pytest.skip("source-matched timed native boundary API is not built")
    state = _user_refutation_states()[1]
    status, message, raw_stats, raw_series, _capsule = (
        native.prepare_complete_series_timed(*_batch_arguments(state), 0)
    )
    assert int(status) == 4
    assert message == "native complete-series deadline reached"
    assert tuple(raw_series) == ()
    assert sum(int(value) for value in raw_stats) == 0


def test_direct_parallel_batch_matches_serial_payload_stats_and_states() -> None:
    native = _require_n2_native()
    if not hasattr(native, "prepare_complete_series_timed_parallel"):
        pytest.skip("source-matched parallel native boundary API is not built")
    state = _user_refutation_states()[1]
    arguments = _batch_arguments(state)
    serial = native.prepare_complete_series_timed(
        *arguments,
        10_000_000_000,
    )
    parallel = native.prepare_complete_series_timed_parallel(
        *arguments,
        10_000_000_000,
        16,
    )
    assert _prepared_batch_signature(
        native,
        parallel,
    ) == _prepared_batch_signature(native, serial)


@pytest.mark.parametrize(
    "remaining_nanoseconds",
    [1_000_000, 10_000_000, 100_000_000],
)
def test_parallel_native_deadline_is_explicit_and_responsive(
    remaining_nanoseconds: int,
) -> None:
    native = _require_n2_native()
    if not hasattr(native, "prepare_complete_series_timed_parallel"):
        pytest.skip("source-matched parallel native boundary API is not built")
    started = time.perf_counter()
    status, message, _stats, raw_series, _capsule = (
        native.prepare_complete_series_timed_parallel(
            *_wide_batch_arguments(_heavy_s7_state()),
            remaining_nanoseconds,
            16,
        )
    )
    wall = time.perf_counter() - started
    assert int(status) == 4
    assert message == "native complete-series deadline reached"
    assert tuple(raw_series) == ()
    assert wall < remaining_nanoseconds / 1_000_000_000 + 0.5


@pytest.mark.parametrize("native_threads", [True, 0, 65, 1.5])
def test_search_limits_reject_invalid_native_thread_counts(
    native_threads: object,
) -> None:
    with pytest.raises(ValueError, match="native_threads"):
        SearchLimits(native_threads=native_threads)  # type: ignore[arg-type]


def test_parallel_native_threads_require_a_search_deadline() -> None:
    with pytest.raises(ValueError, match="require time_limit_seconds"):
        SearchLimits(native_threads=2)
    assert SearchLimits(
        native_threads=2,
        time_limit_seconds=1.0,
    ).native_threads == 2


def test_batch_capsule_releases_by_refcount_with_cyclic_gc_disabled() -> None:
    released: list[str] = []

    class DummyCapsule:
        def __del__(self) -> None:
            released.append("released")

    class DummyNative:
        pass

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        capsule = DummyCapsule()
        batch = _NativeSeriesBatch(
            DummyNative(),
            capsule,
            ProgressiveState.initial(),
            ((('e2e4',), 1),),
        )
        reference = batch.references()[0]
        del capsule
        del batch
        assert released == []  # The active reference owns the capsule.
        del reference
        assert released == ["released"]  # No cyclic-GC pass was required.
    finally:
        if was_enabled:
            gc.enable()


def test_active_reference_decodes_after_generation_cache_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_n2_native()
    monkeypatch.setattr(evaluation, "_native_eval", native)
    monkeypatch.setattr(search_module, "SERIES_GENERATION_CACHE_CAPACITY", 1)
    searcher = SeriesSearcher(
        SearchLimits(depth_series=1, max_series_per_node=1),
        baseline_profile(),
    )
    first = searcher._ordered_generated(
        ProgressiveState.from_fen(chess.STARTING_FEN, 3),
        ply_from_root=1,
    )
    reference = first[0]
    assert getattr(reference, "_decoded") is None
    searcher._ordered_generated(
        ProgressiveState.from_fen(chess.STARTING_FEN, 5),
        ply_from_root=1,
    )
    assert searcher.stats.series_generation_cache_evictions == 1
    assert reference.final_state.series_number == 4
    assert reference.materialize().machine_notation == reference.machine_notation


def _search_signature(result: object) -> tuple[object, ...]:
    return (
        result.score,
        result.best_series.machine_notation if result.best_series else None,
        tuple(item.machine_notation for item in result.principal_variation),
        tuple(
            (
                item.series.machine_notation,
                item.score,
                tuple(
                    series.machine_notation
                    for series in item.principal_variation
                ),
                item.proof_bounds,
            )
            for item in result.alternatives
        ),
        result.completed_depth,
        result.exact_width,
        result.timed_out,
        result.work_limit_reached,
        result.root_scores_complete,
        result.proof,
        result.adjudication_status,
        asdict(result.stats),
    )


class _V08NativeSurface:
    """Same compiled kernel with the N2 APIs deliberately hidden."""

    def __init__(self, native: object) -> None:
        self._native = native

    def __getattr__(self, name: str) -> object:
        if name in {
            "full_evaluate",
            "prepare_complete_series",
            "complete_series_candidate",
        }:
            raise AttributeError(name)
        return getattr(self._native, name)


def _wide_reply_screen_signature(
    monkeypatch: pytest.MonkeyPatch,
    surface: object,
    state: ProgressiveState,
) -> tuple[bool, str | None, dict[str, object]]:
    monkeypatch.setattr(evaluation, "_native_eval", surface)
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        baseline_profile(),
    )
    mate, completed = searcher._root_child_mate_screen_stage(
        state,
        frontier=4_096,
        tactical_protection=False,
    )
    return (
        completed,
        mate.machine_notation if mate is not None else None,
        asdict(searcher.stats),
    )


def test_v08_wide_s5_screen_matches_n2_result_and_all_work_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_n2_native()
    state = ProgressiveState.initial()
    for moves in (
        ("e2e4",),
        ("f7f6", "e8f7"),
        ("d2d4", "b1c3", "f1d3"),
        ("d7d5", "c8g4", "d5e4", "g4d1"),
    ):
        state = play_series(state, moves).final_state

    legacy = _wide_reply_screen_signature(
        monkeypatch,
        _V08NativeSurface(native),
        state,
    )
    accelerated = _wide_reply_screen_signature(monkeypatch, native, state)

    assert accelerated == legacy
    assert legacy[0]
    assert legacy[1] == "c3d5/d3e4/d5f4/e4h7/h7g6"
    assert legacy[2]["root_safety_screen_positions"] == 71_587


def test_v08_wide_s6_screen_matches_n2_result_and_all_work_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_n2_native()
    state = ProgressiveState.from_fen(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1",
        6,
    )

    legacy = _wide_reply_screen_signature(
        monkeypatch,
        _V08NativeSurface(native),
        state,
    )
    accelerated = _wide_reply_screen_signature(monkeypatch, native, state)

    assert accelerated == legacy
    assert legacy[0]
    assert legacy[1] == "a7a5/a5a4/e7e5/d8h4/f8c5/c5f2"
    assert legacy[2]["root_safety_screen_positions"] == 103_661


def test_v08_wide_screen_with_deadline_does_not_run_untimed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_n2_native()
    state = ProgressiveState.from_fen(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1",
        6,
    )

    def unexpected_untimed_generation(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("timed screen entered the untimed legacy ABI")

    monkeypatch.setattr(evaluation, "_native_eval", _V08NativeSurface(native))
    monkeypatch.setattr(
        search_module,
        "_native_complete_series_generation",
        unexpected_untimed_generation,
    )
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            time_limit_seconds=30.0,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        baseline_profile(),
    )
    searcher._deadline = time.perf_counter() + 30.0

    mate, completed = searcher._root_child_mate_screen_stage(
        state,
        frontier=4_096,
        tactical_protection=False,
    )

    assert mate is None
    assert not completed
    assert searcher.stats.root_safety_screen_positions == 0


def test_integrated_n2_search_matches_v08_output_and_all_work_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_n2_native()
    state = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    limits = SearchLimits(
        depth_series=2,
        max_series_per_node=16,
        max_generation_positions=250_000,
        collect_all_root_scores=False,
    )
    monkeypatch.setattr(evaluation, "_native_eval", _V08NativeSurface(native))
    oracle = analyze(state, limits, baseline_profile())
    monkeypatch.setattr(evaluation, "_native_eval", native)
    accelerated = analyze(state, limits, baseline_profile())
    assert _search_signature(accelerated) == _search_signature(oracle)


def test_timed_native_search_matches_untimed_result_and_all_work_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_n2_native()
    if not hasattr(native, "prepare_complete_series_timed"):
        pytest.skip("source-matched timed native boundary API is not built")
    state = _user_refutation_states()[1]
    base = dict(
        depth_series=3,
        max_series_per_node=32,
        max_generation_positions=500_000,
        collect_all_root_scores=True,
    )
    monkeypatch.setattr(evaluation, "_native_eval", native)
    untimed = analyze(state, SearchLimits(**base), baseline_profile())
    witness = _TimedNativeWitness(native)
    monkeypatch.setattr(evaluation, "_native_eval", witness)
    timed = analyze(
        state,
        SearchLimits(**base, time_limit_seconds=5.0),
        baseline_profile(),
    )
    assert witness.calls > 0
    assert set(witness.statuses) <= {0, 1}
    assert _search_signature(timed) == _search_signature(untimed)
    assert timed.completed_depth >= 2
    assert timed.best_series is not None
    assert timed.best_series.machine_notation != "c7c5/c5d4/d4d3/d3c2"
    assert timed.elapsed_seconds < 5.0


def test_default_search_uses_serial_native_surface_without_pool_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_n2_native()
    if not hasattr(native, "prepare_complete_series_timed_parallel"):
        pytest.skip("source-matched parallel native boundary API is not built")
    witness = _ParallelNativeWitness(native)
    monkeypatch.setattr(evaluation, "_native_eval", witness)
    result = analyze(
        _user_refutation_states()[1],
        SearchLimits(
            depth_series=1,
            max_series_per_node=16,
            time_limit_seconds=10.0,
            max_generation_positions=1_000_000,
        ),
        baseline_profile(),
    )
    assert result.completed_depth == 1
    assert SearchLimits().native_threads == 1
    assert witness.calls > 0
    assert witness.parallel_calls == 0


def test_parallel_search_matches_serial_and_shares_one_contention_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_n2_native()
    if not hasattr(native, "prepare_complete_series_timed_parallel"):
        pytest.skip("source-matched parallel native boundary API is not built")
    monkeypatch.setattr(evaluation, "_native_eval", native)
    state = _user_refutation_states()[1]
    common = dict(
        depth_series=2,
        max_series_per_node=16,
        time_limit_seconds=10.0,
        max_generation_positions=1_000_000,
    )
    serial = analyze(
        state,
        SearchLimits(**common, native_threads=1),
        baseline_profile(),
    )

    def parallel_search() -> object:
        return analyze(
            state,
            SearchLimits(**common, native_threads=16),
            baseline_profile(),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(parallel_search) for _ in range(2)]
        parallel = [future.result() for future in futures]
    assert serial.completed_depth == 2
    assert all(
        _search_signature(result) == _search_signature(serial)
        for result in parallel
    )


def test_user_refutation_boundaries_complete_d2_with_timed_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_n2_native()
    if not hasattr(native, "prepare_complete_series_timed"):
        pytest.skip("source-matched timed native boundary API is not built")
    s2, s4 = _user_refutation_states()
    repeated_losing_series = {
        2: "e7e6/d8f6",
        4: "c7c5/c5d4/d4d3/d3c2",
    }
    for state in (s2, s4):
        witness = _TimedNativeWitness(native)
        monkeypatch.setattr(evaluation, "_native_eval", witness)
        result = analyze(
            state,
            SearchLimits(
                depth_series=3,
                max_series_per_node=32,
                time_limit_seconds=5.0,
                max_generation_positions=(
                    250_000 if state.series_number == 2 else 500_000
                ),
                collect_all_root_scores=True,
            ),
            baseline_profile(),
        )
        assert witness.calls > 0
        assert result.completed_depth >= 2
        assert result.best_series is not None
        assert (
            result.best_series.machine_notation
            != repeated_losing_series[state.series_number]
        )
        assert result.elapsed_seconds < 5.0


def test_collect_all_s4_at_250k_fails_closed_without_partial_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_n2_native()
    monkeypatch.setattr(evaluation, "_native_eval", native)

    result = analyze(
        _user_refutation_states()[1],
        SearchLimits(
            depth_series=3,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=True,
        ),
        baseline_profile(),
    )

    assert result.completed_depth == 0
    assert result.work_limit_reached
    assert result.alternatives == ()
    assert result.proof is None
    assert not result.root_scores_complete
    assert result.stats.root_safety_proven_mate_children > 0
    assert result.stats.native_series_mate_work_limit_hits == 1


def test_timed_native_search_keeps_last_completed_iteration_on_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_n2_native()
    if not hasattr(native, "prepare_complete_series_timed"):
        pytest.skip("source-matched timed native boundary API is not built")
    state = _user_refutation_states()[1]
    witness = _TimedNativeWitness(native)
    monkeypatch.setattr(evaluation, "_native_eval", witness)
    started = time.perf_counter()
    result = analyze(
        state,
        SearchLimits(
            depth_series=3,
            max_series_per_node=32,
            time_limit_seconds=0.5,
            max_generation_positions=5_000_000,
        ),
        baseline_profile(),
    )
    wall = time.perf_counter() - started
    assert result.timed_out
    assert result.completed_depth >= 1

    # Faster native kernels may finish another complete iteration inside the
    # same deadline. Compare the published result to an untimed oracle at the
    # depth actually completed instead of hard-coding the old depth-one timing.
    monkeypatch.setattr(evaluation, "_native_eval", native)
    completed = analyze(
        state,
        SearchLimits(
            depth_series=result.completed_depth,
            max_series_per_node=32,
            max_generation_positions=5_000_000,
        ),
        baseline_profile(),
    )
    assert result.score == completed.score
    assert result.best_series == completed.best_series
    assert result.principal_variation == completed.principal_variation
    assert result.alternatives == completed.alternatives
    assert result.proof == completed.proof
    assert result.forced == completed.forced
    assert result.exact_width == completed.exact_width
    assert result.root_scores_complete == completed.root_scores_complete
    # The timed search also accounts for work in the interrupted next
    # iteration, so it must cover (not equal) the completed-depth oracle work.
    assert (
        result.stats.generation_positions
        >= completed.stats.generation_positions
    )
    assert witness.calls >= 1
    # The public deadline may be observed between native batches after the last
    # batch returned Complete. The direct immediate-deadline test above pins the
    # native status itself; this test pins fail-closed top-level publication.
    assert set(witness.statuses) <= {0, 4}
    assert wall < 1.0


def test_integrated_batch_replays_only_returned_search_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _require_n2_native()
    monkeypatch.setattr(evaluation, "_native_eval", native)
    replay_calls = 0
    original = rules_module.play_series

    def counted(*args: object, **kwargs: object):
        nonlocal replay_calls
        replay_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(rules_module, "play_series", counted)
    interrupted = analyze(
        ProgressiveState.from_fen(chess.STARTING_FEN, 3),
        SearchLimits(
            depth_series=2,
            max_series_per_node=16,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
        ),
        baseline_profile(),
    )
    assert interrupted.completed_depth == 1
    assert interrupted.work_limit_reached
    assert interrupted.stats.native_series_mate_exhausted == 1
    assert interrupted.stats.native_series_mate_work_limit_hits == 1
    assert interrupted.stats.root_safety_unknown_interruptions == 1

    # A 500k configured work contract gives the shared exact-safety lane enough
    # room to settle both Series-4 reply children. The replay assertion remains
    # about returned evidence, while the smaller run above proves fail-closed
    # last-completed-depth behavior instead of silently accepting a capped miss.
    replay_calls = 0
    result = analyze(
        ProgressiveState.from_fen(chess.STARTING_FEN, 3),
        SearchLimits(
            depth_series=2,
            max_series_per_node=16,
            max_generation_positions=500_000,
            collect_all_root_scores=False,
        ),
        baseline_profile(),
    )
    assert result.completed_depth == 2
    assert not result.work_limit_reached
    assert result.stats.native_series_mate_calls == 2
    assert result.stats.native_series_mate_exhausted == 2
    assert replay_calls > 0
    assert replay_calls < result.stats.generated_unique_series


def test_selective_cap_cannot_publish_false_draw_proof_to_consumers() -> None:
    state = ProgressiveState.from_fen(
        "k7/8/8/4b3/8/3q4/1K6/8 w - - 0 1",
        3,
    )
    limits = SearchLimits(
        depth_series=1,
        max_series_per_node=1,
        max_generation_positions=250_000,
        collect_all_root_scores=True,
    )
    result = analyze(state, limits, baseline_profile())
    assert result.best_series is not None
    assert result.best_series.outcome == Outcome.STALEMATE
    assert not result.exact_width
    assert result.root_scores_complete  # Complete within the retained frontier.
    assert result.proof is None
    assert result.forced is None
    evidence = AnalysisEvidence.from_search_result(result)
    assert evidence.forced_outcome is None

    searcher = SeriesSearcher(limits, baseline_profile())
    score, pv, proof_bounds = searcher._minimax(
        state,
        1,
        -2 * MATE_SCORE,
        2 * MATE_SCORE,
        0,
    )
    assert pv
    # One retained draw proves White cannot be forced to lose, while omitted
    # siblings leave a possible White win: the sound interval is draw-or-win.
    assert proof_bounds == (0, 1)
    assert ScoredSeries(pv[0], score, (), proof_bounds).proof is None


def test_selective_cap_preserves_sound_existential_mate_proof() -> None:
    state = ProgressiveState.from_fen(
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R "
        "b KQkq - 1 3",
        4,
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=1,
            max_series_per_node=64,
            max_generation_positions=250_000,
            collect_all_root_scores=True,
        ),
        baseline_profile(),
    )
    assert result.best_series is not None
    assert result.best_series.outcome == Outcome.CHECKMATE
    assert not result.exact_width
    assert result.proof == "black"
    assert result.alternatives[0].proof == "black"
