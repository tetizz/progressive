from __future__ import annotations

from dataclasses import asdict
import gc
import importlib.util
import os
from pathlib import Path
import sys

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
    result = analyze(
        ProgressiveState.from_fen(chess.STARTING_FEN, 3),
        SearchLimits(
            depth_series=2,
            max_series_per_node=16,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
        ),
        baseline_profile(),
    )
    assert result.completed_depth == 2
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
