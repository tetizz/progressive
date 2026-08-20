from __future__ import annotations

from dataclasses import asdict
import hashlib
from pathlib import Path
import random

import chess
import pytest

import scottish_progressive.evaluation as evaluation
import scottish_progressive.model as model
import scottish_progressive.search as search_module
from scottish_progressive.model import Outcome, ProgressiveState
from scottish_progressive.profiles import EvaluationWeights, baseline_profile
from scottish_progressive.rules import generate_series
from scottish_progressive.search import SearchLimits, analyze


def _require_native() -> None:
    if not evaluation.native_acceleration_available():
        pytest.skip("optional C++20 ordering evaluator is not built")


def _assert_native_matches_python(
    state: ProgressiveState,
    weights: EvaluationWeights | None = None,
) -> None:
    _require_native()
    assert evaluation.fast_evaluate(state, weights) == evaluation._python_fast_evaluate(
        state, weights
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
            "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
            3,
        ),
        ProgressiveState.from_fen(
            "7k/8/8/pPpP4/8/8/8/K7 w - - 0 1",
            3,
            ep_targets=(chess.A6, chess.C6),
        ),
        ProgressiveState.from_fen(
            "8/P6k/8/8/8/8/8/7K w - - 0 1",
            3,
        ),
        ProgressiveState.from_fen(
            "4k3/q3R3/8/8/8/8/8/4K3 b - - 0 1",
            2,
        ),
    ],
)
def test_native_fast_evaluation_matches_python_on_rule_edges(
    state: ProgressiveState,
) -> None:
    for value in (25, 99, 100, 101, 175, 300):
        _assert_native_matches_python(
            state,
            EvaluationWeights(
                material=value,
                king_space=325 - value,
                series_reach=100,
                promotion_corridors=value,
                immediate_vulnerability=325 - value,
                useful_mobility=100,
                boundary_check=value,
            ),
        )


def test_native_fast_evaluation_matches_random_progressive_reachable_states() -> None:
    _require_native()
    rng = random.Random(20_260_820)
    sampled: list[ProgressiveState] = []
    for _ in range(5):
        state = ProgressiveState.initial()
        for _ in range(5):
            candidates = [
                result
                for result in generate_series(
                    state,
                    max_frontier_states=8,
                    frontier_score=lambda _board: 0,
                )
                if result.outcome is None
            ]
            if not candidates:
                break
            state = rng.choice(candidates).final_state
            sampled.append(state)

    assert len(sampled) >= 20
    for state in sampled:
        weights = EvaluationWeights(
            **{
                name: rng.randint(25, 300)
                for name in EvaluationWeights.__dataclass_fields__
            }
        )
        _assert_native_matches_python(state, weights)


@pytest.mark.parametrize(
    ("fen", "series_number", "quiet_series"),
    [
        ("8/8/8/8/6Q1/2K5/6k1/8 b - - 144 109", 22, 8),
        ("8/3K4/8/4Q3/8/4k3/8/8 b - - 156 119", 24, 8),
        ("7k/8/8/8/8/8/6R1/K7 w - - 0 1", 101, 10),
    ],
)
def test_native_fast_evaluation_preserves_high_series_anchors(
    fen: str,
    series_number: int,
    quiet_series: int,
) -> None:
    _assert_native_matches_python(
        ProgressiveState.from_fen(
            fen,
            series_number,
            quiet_series=quiet_series,
        )
    )


def _search_signature(result) -> tuple[object, ...]:
    return (
        result.score,
        result.best_series.machine_notation if result.best_series else None,
        tuple(item.machine_notation for item in result.principal_variation),
        tuple(
            (
                item.score,
                item.series.machine_notation,
                tuple(series.machine_notation for series in item.principal_variation),
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


def _analyze_with_ordering(
    monkeypatch: pytest.MonkeyPatch,
    state: ProgressiveState,
    ordering,
    *,
    branch_cap: int = 32,
    max_work: int = 250_000,
):
    monkeypatch.setattr(search_module, "fast_evaluate", ordering)
    return analyze(
        state,
        SearchLimits(
            depth_series=1,
            max_series_per_node=branch_cap,
            max_generation_positions=max_work,
            collect_all_root_scores=False,
        ),
        baseline_profile(),
    )


def test_published_s4_mate_keeps_identical_native_output_and_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_native()
    state = ProgressiveState.from_fen(
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R "
        "b KQkq - 1 3",
        4,
    )
    python = _analyze_with_ordering(
        monkeypatch,
        state,
        evaluation._python_fast_evaluate,
    )
    native = _analyze_with_ordering(
        monkeypatch,
        state,
        evaluation.fast_evaluate,
    )

    assert _search_signature(native) == _search_signature(python)
    assert native.best_series is not None
    assert native.best_series.machine_notation == "c7c6/d8b6/f6e4/b6f2"
    assert native.best_series.outcome == Outcome.CHECKMATE


@pytest.mark.parametrize(
    ("fen", "series_number", "quiet_series"),
    [
        ("8/8/8/8/6Q1/2K5/6k1/8 b - - 144 109", 22, 8),
        ("8/3K4/8/4Q3/8/4k3/8/8 b - - 156 119", 24, 8),
    ],
)
def test_high_series_search_keeps_identical_native_output_and_work(
    monkeypatch: pytest.MonkeyPatch,
    fen: str,
    series_number: int,
    quiet_series: int,
) -> None:
    _require_native()
    state = ProgressiveState.from_fen(
        fen,
        series_number,
        quiet_series=quiet_series,
    )
    python = _analyze_with_ordering(
        monkeypatch,
        state,
        evaluation._python_fast_evaluate,
    )
    native = _analyze_with_ordering(
        monkeypatch,
        state,
        evaluation.fast_evaluate,
    )
    assert _search_signature(native) == _search_signature(python)
    assert native.best_series is not None
    assert native.best_series.used_moves == series_number


def test_native_ordering_preserves_deterministic_work_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_native()
    state = ProgressiveState.from_fen(chess.STARTING_FEN, 3)
    python = _analyze_with_ordering(
        monkeypatch,
        state,
        evaluation._python_fast_evaluate,
        max_work=300,
    )
    native = _analyze_with_ordering(
        monkeypatch,
        state,
        evaluation.fast_evaluate,
        max_work=300,
    )
    assert _search_signature(native) == _search_signature(python)
    assert native.work_limit_reached
    assert native.stats.generation_positions == 300
    assert native.stats.series_generation_positions == 23
    assert native.stats.frontier_score_positions == 20


def test_fast_evaluate_falls_back_to_python_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProgressiveState.initial()
    expected = evaluation._python_fast_evaluate(state)
    monkeypatch.setattr(evaluation, "_native_eval", None)
    assert not evaluation.native_acceleration_available()
    assert evaluation.fast_evaluate(state) == expected


def test_native_source_identity_guard_rejects_stale_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProgressiveState.initial()
    expected = evaluation._python_fast_evaluate(state)

    class StaleNative:
        SOURCE_IDENTITY = "not-the-packaged-source-digest"

        @staticmethod
        def fast_evaluate(*_args):
            raise AssertionError("stale native extension was executed")

    monkeypatch.setattr(
        evaluation,
        "_native_eval",
        evaluation._validated_native_module(StaleNative()),
    )
    assert not evaluation.native_acceleration_available()
    assert evaluation.fast_evaluate(state) == expected


def test_loaded_native_source_identity_matches_packaged_sources() -> None:
    _require_native()
    assert evaluation._native_eval.SOURCE_IDENTITY == evaluation._native_source_identity()


def test_engine_fingerprint_includes_native_cpp_and_headers() -> None:
    package = Path(model.__file__).resolve().parent

    def fingerprint(patterns: tuple[str, ...]) -> str:
        digest = hashlib.sha256()
        paths = (
            path
            for pattern in patterns
            for path in package.rglob(pattern)
        )
        for path in sorted(
            paths,
            key=lambda item: item.relative_to(package).as_posix(),
        ):
            digest.update(path.relative_to(package).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()[:16]

    assert list(package.glob("*.cpp"))
    assert list(package.glob("*.hpp"))
    assert model.ENGINE_SOURCE_FINGERPRINT == fingerprint(
        ("*.py", "*.cpp", "*.hpp", "*.h")
    )
    assert model.ENGINE_SOURCE_FINGERPRINT != fingerprint(("*.py",))


def _promotion_state(series_number: int) -> ProgressiveState:
    return ProgressiveState.from_fen(
        "8/P6k/8/8/8/8/8/7K w - - 0 1",
        series_number,
    )


@pytest.mark.parametrize("series_number", [39_050_001, 2_147_483_649])
def test_native_accepts_series_above_signed_int32_without_wrapping(
    series_number: int,
) -> None:
    _require_native()
    state = _promotion_state(series_number)
    expected = evaluation._python_fast_evaluate(state)
    assert evaluation._native_fast_evaluation_is_safe(
        state,
        EvaluationWeights(),
    )
    assert evaluation.fast_evaluate(state) == expected
    if series_number == 39_050_001:
        assert expected == 2_147_750_710


@pytest.mark.parametrize(
    "series_number",
    [
        (1 << 63) - 1,
        (1 << 63) + 1,
        ((1 << 63) - 1) // 55 + 2,
        432_870_825_515_397,
    ],
)
def test_unbounded_series_uses_python_before_native_int64_overflow(
    monkeypatch: pytest.MonkeyPatch,
    series_number: int,
) -> None:
    if series_number % 2 == 0:
        series_number += 1
    state = _promotion_state(series_number)
    expected = evaluation._python_fast_evaluate(state)

    class NativeMustNotRun:
        @staticmethod
        def fast_evaluate(*_args):
            raise AssertionError("unsafe series reached native arithmetic")

    monkeypatch.setattr(evaluation, "_native_eval", NativeMustNotRun())
    assert not evaluation._native_fast_evaluation_is_safe(
        state,
        EvaluationWeights(),
    )
    assert evaluation.fast_evaluate(state) == expected


def test_adversarial_weight_uses_unbounded_python_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _promotion_state(3)
    weights = EvaluationWeights()
    object.__setattr__(weights, "promotion_corridors", (1 << 63) - 1)
    expected = evaluation._python_fast_evaluate(state, weights)

    class NativeMustNotRun:
        @staticmethod
        def fast_evaluate(*_args):
            raise AssertionError("unsafe profile reached native arithmetic")

    monkeypatch.setattr(evaluation, "_native_eval", NativeMustNotRun())
    assert not evaluation._native_fast_evaluation_is_safe(state, weights)
    assert evaluation.fast_evaluate(state, weights) == expected


def test_direct_native_api_reports_int64_overflow_instead_of_wrapping() -> None:
    _require_native()
    state = _promotion_state((1 << 63) - 1)
    board = state.board
    with pytest.raises(OverflowError, match="signed 64-bit"):
        evaluation._native_eval.fast_evaluate(
            board.pawns,
            board.knights,
            board.bishops,
            board.rooks,
            board.queens,
            board.kings,
            board.occupied_co[chess.WHITE],
            board.occupied_co[chess.BLACK],
            board.turn,
            state.series_number,
            100,
            100,
            100,
            100,
            100,
        )
