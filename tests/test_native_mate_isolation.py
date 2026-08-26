from __future__ import annotations

from dataclasses import asdict
import hashlib
from pathlib import Path
import random
import subprocess
import sys

import chess
import pytest

import scottish_progressive.evaluation as evaluation
import scottish_progressive.series_mate as series_mate
from scottish_progressive.model import Outcome, ProgressiveState
from scottish_progressive.rules import GenerationStats, generate_series, play_series
from scottish_progressive.series_mate import (
    SeriesMateStatus,
    find_native_series_mate,
)


NATIVE_SOURCE_HASHES = {
    "_native_eval.cpp": "29cde43737338f3ed40aa520a54375637a0b34d3433c774da44de4835f3bfc0a",
    "native_eval.hpp": "85b426f8f868def00dea0d7a7f6f9d048d9836cde1018dc1ae0d61e33c25ac5f",
}
NATIVE_IDENTITY = (
    "515f49aa565b7dd634b2601f6dceb2cd3cf6507a488eb5e0aac72aed1df54259"
)
NATIVE_SURFACE = {
    "SOURCE_IDENTITY",
    "complete_series_candidate",
    "deep_teacher_score_v1",
    "expand_legal_move_variants",
    "fast_evaluate",
    "full_evaluate",
    "generate_complete_series",
    "generate_full_game_batch",
    "generate_full_game_batch_v2",
    "has_legal_move",
    "legal_move_variants",
    "neural_ordering_evaluate",
    "neural_ordering_identity",
    "neural_ordering_parameters",
    "prepare_complete_series",
    "prepare_complete_series_timed",
    "prepare_complete_series_timed_parallel",
    "proof_aware_root_precedes_v1",
    "create_subtree_search",
    "subtree_begin_transaction",
    "subtree_enumerate_root",
    "subtree_external_cache_present",
    "subtree_import_root",
    "subtree_insert_external_cache",
    "subtree_rollback_transaction",
    "subtree_search",
    "subtree_search_root_candidate",
    "subtree_touch_external_cache",
    "teacher_value_features_v3",
    "teacher_value_features_v3_with_receipt",
}
LIVE_S5_HISTORY = (
    ("e2e4",),
    ("f7f6", "e8f7"),
    ("d2d4", "b1c3", "f1d3"),
    ("d7d5", "c8g4", "d5e4", "g4d1"),
)
S9_LEGACY_ORDERING_STATE = ProgressiveState.from_fen(
    "rn1q1bnr/ppp1pkpp/5p2/8/3Pp3/2NB4/PPP2PPP/R1BbK1NR w KQ - 0 7",
    9,
)
S7_STATE = ProgressiveState.from_fen(
    "rnk3nr/pp3ppp/8/8/8/1Pp1P3/P1PP1PPP/R1b1K1NR w K - 0 13",
    7,
)
BLUNDERING_S7 = (
    "e3e4",
    "g1e2",
    "e4e5",
    "e5e6",
    "e6f7",
    "a1c1",
    "f7g8q",
)
S16_STATE = ProgressiveState.from_fen(
    "5Q1Q/8/3k4/8/8/8/4K3/8 b - - 0 57",
    16,
)
BLUNDERING_S16 = (
    "d6c6",
    "c6b5",
    "b5a4",
    "a4a5",
    "a5a4",
    "a4a5",
    "a5a4",
    "a4a5",
    "a5a4",
    "a4a5",
    "a5a4",
    "a4a5",
    "a5a4",
    "a4a5",
    "a5a4",
    "a4a5",
)


def _require_native_mate() -> object:
    native = series_mate._native_mate
    if native is None or not hasattr(native, "find_series_mate"):
        pytest.skip("source-matched isolated native mate extension is unavailable")
    assert native.SOURCE_IDENTITY == series_mate._native_mate_source_identity()
    return native


def _live_s5() -> ProgressiveState:
    state = ProgressiveState.initial()
    for moves in LIVE_S5_HISTORY:
        state = play_series(state, moves).final_state
    assert state.series_number == 5
    return state


def _raw_native_arguments(state: ProgressiveState) -> list[object]:
    board = state.board
    return [
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
        None,
        None,
        None,
    ]


def test_native_sources_identity_and_surface_are_unchanged() -> None:
    package = Path(evaluation.__file__).resolve().parent
    for filename, expected in NATIVE_SOURCE_HASHES.items():
        normalized = (package / filename).read_text(encoding="utf-8").encode()
        assert hashlib.sha256(normalized).hexdigest() == expected

    native = evaluation._native_eval
    assert native is not None
    assert evaluation._native_source_identity() == NATIVE_IDENTITY
    assert native.SOURCE_IDENTITY == NATIVE_IDENTITY
    assert {name for name in dir(native) if not name.startswith("__")} == (
        NATIVE_SURFACE
    )
    assert not hasattr(native, "find_series_mate")


def test_isolated_native_mate_has_its_own_strict_identity_and_surface() -> None:
    native = _require_native_mate()
    assert native is not evaluation._native_eval
    assert native.SOURCE_IDENTITY == series_mate._native_mate_source_identity()
    assert {name for name in dir(native) if not name.startswith("__")} == {
        "SOURCE_IDENTITY",
        "find_series_mate",
    }
    assert series_mate._validated_native_mate_module(native) is native

    class StaleNativeMate:
        SOURCE_IDENTITY = "stale"

    assert series_mate._validated_native_mate_module(StaleNativeMate()) is None


def test_fullgame_import_path_does_not_load_or_use_native_mate() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    code = f"""
import sys
sys.path.insert(0, {str(source_root)!r})
import scottish_progressive.evaluation
import scottish_progressive.fast_training
import scottish_progressive.league
import scottish_progressive.rules
import scottish_progressive.search
assert 'scottish_progressive.series_mate' not in sys.modules
assert 'scottish_progressive._native_mate' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    native_source = (
        Path(series_mate.__file__).resolve().parent / "_native_mate.cpp"
    ).read_text(encoding="utf-8")
    assert "full_game" not in native_source
    assert "native_selfplay" not in native_source


@pytest.mark.parametrize(
    ("name", "state", "expected", "expected_stats"),
    (
        (
            "quiet-prefix-live-s5",
            _live_s5(),
            ("c3d5", "d3e4", "e4h7", "d5f4", "h7g6"),
            (207, 8_471, 48, 315, 1, 311, 5),
        ),
        (
            "promotion-s8",
            play_series(S7_STATE, BLUNDERING_S7).final_state,
            (
                "h8g8",
                "g8e8",
                "b8c6",
                "c6a5",
                "a5c4",
                "c4e3",
                "e8d8",
                "c3d2",
            ),
            (13_260, 401_050, 35_552, 35_535, 1, 13_091, 8),
        ),
        (
            "series-nine-legacy-ordering-boundary",
            S9_LEGACY_ORDERING_STATE,
            (
                "c3d5",
                "c1h6",
                "d3e4",
                "e4f5",
                "h6g7",
                "d5f6",
                "g1f3",
                "g7h8",
                "f3e5",
            ),
            (566, 24_473, 367, 1_263, 1, 891, 9),
        ),
        (
            "nonpromotion-s17",
            play_series(S16_STATE, BLUNDERING_S16).final_state,
            ("h8b2", "f8a3"),
            (2, 71, 4, 20, 1, 46, 2),
        ),
    ),
)
def test_exact_prototype_replay_and_work_receipts(
    name: str,
    state: ProgressiveState,
    expected: tuple[str, ...],
    expected_stats: tuple[int, ...],
) -> None:
    _require_native_mate()
    probes = tuple(
        find_native_series_mate(
            state,
            max_positions=250_000,
            time_limit_seconds=30.0,
        )
        for _ in range(3)
    )

    assert asdict(probes[0]) == asdict(probes[1]) == asdict(probes[2]), name
    probe = probes[0]
    assert probe.status is SeriesMateStatus.FOUND
    assert probe.complete
    assert not probe.exhausted
    assert not probe.cancelled
    assert not probe.work_limit_reached
    assert probe.series is not None
    assert probe.series.moves == expected
    assert probe.series.outcome is Outcome.CHECKMATE
    assert probe.series.ended_by_check
    assert play_series(state, probe.series.moves).outcome is Outcome.CHECKMATE
    assert (
        probe.positions_visited,
        probe.moves_generated,
        probe.transpositions_merged,
        probe.checking_series,
        probe.checkmates,
        probe.peak_frontier,
        probe.max_depth_reached,
    ) == expected_stats


@pytest.mark.parametrize(
    ("fen", "series_number"),
    (
        ("8/8/8/8/8/2k5/8/K7 w - - 0 1", 1),
        ("8/8/8/8/8/2k5/8/K7 w - - 0 1", 3),
        ("8/8/8/8/8/2k5/5N2/K7 w - - 0 1", 3),
        ("8/7b/8/8/8/2k5/8/K7 b - - 0 1", 2),
        ("8/5P2/8/2k5/7B/3K4/8/8 w - - 0 1", 7),
    ),
)
def test_exhaustion_is_an_exact_negative_result(
    fen: str,
    series_number: int,
) -> None:
    _require_native_mate()
    probe = find_native_series_mate(
        ProgressiveState.from_fen(fen, series_number),
        max_positions=None,
        time_limit_seconds=30.0,
    )

    assert probe.status is SeriesMateStatus.EXHAUSTED
    assert probe.complete
    assert probe.exhausted
    assert probe.series is None
    assert probe.positions_visited > 0


def test_work_limit_and_deadline_are_unknown_not_no_mate() -> None:
    _require_native_mate()
    work_limited = find_native_series_mate(
        _live_s5(),
        max_positions=1,
        time_limit_seconds=30.0,
    )
    assert work_limited.status is SeriesMateStatus.WORK_LIMIT
    assert work_limited.work_limit_reached
    assert not work_limited.complete
    assert not work_limited.exhausted
    assert work_limited.series is None
    assert work_limited.positions_visited == 1

    deadline = find_native_series_mate(
        ProgressiveState.from_fen(
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            5,
        ),
        max_positions=None,
        time_limit_seconds=0.0,
    )
    assert deadline.status is SeriesMateStatus.DEADLINE
    assert deadline.cancelled
    assert not deadline.complete
    assert not deadline.exhausted
    assert deadline.series is None
    assert deadline.positions_visited == 0


def test_total_work_limit_counts_positions_and_generated_edges_exactly() -> None:
    _require_native_mate()
    probe = find_native_series_mate(
        _live_s5(),
        max_positions=None,
        max_work=100,
        time_limit_seconds=30.0,
    )

    assert probe.status is SeriesMateStatus.WORK_LIMIT
    assert probe.positions_visited + probe.moves_generated == 100
    assert probe.series is None


def test_realistic_opening_series_two_through_four_are_exact_exhaustions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_native_mate()
    state = ProgressiveState.initial()
    boundaries: list[ProgressiveState] = []
    for series in (
        ("e2e4",),
        ("f7f6", "e8f7"),
        ("d2d4", "b1c3", "f1d3"),
    ):
        state = play_series(state, series).final_state
        boundaries.append(state)

    expected_work = {
        2: (21, 466),
        3: (472, 14_932),
        4: (3_511, 95_121),
    }
    for boundary in boundaries:
        native_probe = find_native_series_mate(
            boundary,
            max_positions=None,
            max_work=3_000_000,
            time_limit_seconds=30.0,
        )
        with monkeypatch.context() as oracle:
            oracle.setattr(evaluation, "_native_eval", None)
            generated = generate_series(
                boundary,
                max_frontier_states=None,
                max_positions=500_000,
            )

        assert not any(
            series.outcome is Outcome.CHECKMATE and series.ended_by_check
            for series in generated
        )
        assert native_probe.status is SeriesMateStatus.EXHAUSTED
        assert native_probe.series is None
        assert (
            native_probe.positions_visited,
            native_probe.moves_generated,
        ) == expected_work[boundary.series_number]


def test_oversized_python_integers_are_explicitly_unsupported() -> None:
    native = _require_native_mate()
    state = _live_s5()
    oversized_cases = (
        (0, 1 << 64),
        (0, -1),
        (11, 1 << 63),
        (11, -(1 << 63) - 1),
        (13, 1 << 64),
        (14, 1 << 64),
        (15, 1 << 64),
    )
    for index, value in oversized_cases:
        arguments = _raw_native_arguments(state)
        arguments[index] = value
        raw = tuple(native.find_series_mate(*arguments))
        assert raw[0] == 4
        assert tuple(raw[3]) == ()

    huge_series = ProgressiveState(chess.Board(), (1 << 63) + 1)
    assert find_native_series_mate(huge_series).status is SeriesMateStatus.UNSUPPORTED
    assert find_native_series_mate(
        state,
        max_positions=1 << 64,
    ).status is SeriesMateStatus.UNSUPPORTED
    assert find_native_series_mate(
        state,
        max_work=1 << 64,
    ).status is SeriesMateStatus.UNSUPPORTED


def test_unavailable_or_identity_mismatched_native_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(series_mate, "_native_mate", None)
    probe = find_native_series_mate(_live_s5())
    assert probe.status is SeriesMateStatus.UNSUPPORTED
    assert not probe.complete
    assert not probe.exhausted
    assert probe.series is None


@pytest.mark.parametrize(
    ("state", "required_root_moves"),
    (
        (
            ProgressiveState.from_fen(
                "k7/8/8/8/8/8/8/4K2R w K - 0 1",
                1,
            ),
            {"e1g1"},
        ),
        (
            ProgressiveState.from_fen(
                "7k/8/8/1Pp2pP1/8/8/8/K7 w - - 0 1",
                1,
                ep_targets=(chess.C6, chess.F6),
            ),
            {"b5c6", "g5f6"},
        ),
    ),
)
def test_castling_and_multi_target_progressive_ep_match_python_oracle(
    monkeypatch: pytest.MonkeyPatch,
    state: ProgressiveState,
    required_root_moves: set[str],
) -> None:
    _require_native_mate()
    native_probe = find_native_series_mate(
        state,
        max_positions=None,
        time_limit_seconds=30.0,
    )
    with monkeypatch.context() as oracle:
        oracle.setattr(evaluation, "_native_eval", None)
        generated = generate_series(
            state,
            max_frontier_states=None,
            max_positions=None,
        )

    root_moves = {series.moves[0] for series in generated}
    oracle_has_mate = any(
        series.outcome is Outcome.CHECKMATE and series.ended_by_check
        for series in generated
    )
    assert required_root_moves <= root_moves
    assert native_probe.complete
    assert (native_probe.series is not None) is oracle_has_mate
    assert native_probe.moves_generated == len(generated)


@pytest.mark.parametrize(
    ("fen", "series_number"),
    (
        ("8/8/8/8/8/2k5/8/K7 w - - 0 1", 3),
        ("8/8/8/8/8/2k5/5N2/K7 w - - 0 1", 3),
        ("7k/8/5K2/8/8/8/8/R7 w - - 0 1", 3),
        ("8/7q/8/8/8/2k5/8/K7 b - - 0 1", 2),
        ("8/7r/8/8/8/2k5/8/K7 b - - 0 1", 2),
        ("8/7b/8/8/8/2k5/8/K7 b - - 0 1", 2),
    ),
)
def test_native_result_matches_curated_small_exhaustive_python_oracle(
    monkeypatch: pytest.MonkeyPatch,
    fen: str,
    series_number: int,
) -> None:
    _require_native_mate()
    state = ProgressiveState.from_fen(fen, series_number)
    native_probe = find_native_series_mate(
        state,
        max_positions=500_000,
        time_limit_seconds=30.0,
    )

    with monkeypatch.context() as oracle:
        oracle.setattr(evaluation, "_native_eval", None)
        stats = GenerationStats()
        generated = generate_series(
            state,
            stats=stats,
            max_frontier_states=None,
            max_positions=500_000,
        )
    oracle_mates = tuple(
        series
        for series in generated
        if series.outcome is Outcome.CHECKMATE and series.ended_by_check
    )

    assert native_probe.complete
    assert (native_probe.series is not None) is bool(oracle_mates)
    assert native_probe.exhausted is (not oracle_mates)
    if native_probe.series is not None:
        assert play_series(state, native_probe.series.moves).outcome is Outcome.CHECKMATE


def test_native_result_matches_64_seeded_small_exhaustive_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_native_mate()
    randomizer = random.Random(68_942_034)
    piece_types = (chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.QUEEN)
    states: list[ProgressiveState] = []
    while len(states) < 64:
        board = chess.Board(None)
        squares = randomizer.sample(range(64), 2 + randomizer.randrange(3))
        board.set_piece_at(squares[0], chess.Piece(chess.KING, chess.WHITE))
        board.set_piece_at(squares[1], chess.Piece(chess.KING, chess.BLACK))
        for square in squares[2:]:
            board.set_piece_at(
                square,
                chess.Piece(
                    randomizer.choice(piece_types),
                    randomizer.choice((chess.WHITE, chess.BLACK)),
                ),
            )
        series_number = randomizer.choice((1, 2, 3))
        board.turn = chess.WHITE if series_number % 2 else chess.BLACK
        if board.is_valid():
            states.append(ProgressiveState(board, series_number))

    for state in states:
        native_probe = find_native_series_mate(
            state,
            max_positions=2_000_000,
            time_limit_seconds=30.0,
        )
        with monkeypatch.context() as oracle:
            oracle.setattr(evaluation, "_native_eval", None)
            generated = generate_series(
                state,
                max_frontier_states=None,
                max_positions=2_000_000,
            )
        oracle_has_mate = any(
            series.outcome is Outcome.CHECKMATE and series.ended_by_check
            for series in generated
        )
        assert native_probe.complete
        assert (native_probe.series is not None) is oracle_has_mate
