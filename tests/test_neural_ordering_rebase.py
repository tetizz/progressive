from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random

import chess
import pytest

import scottish_progressive.evaluation as evaluation
import scottish_progressive.rules as rules
import scottish_progressive.search as search_module
from scottish_progressive.evaluation import fast_evaluate
from scottish_progressive.model import Outcome, ProgressiveState
from scottish_progressive.native_subtree import (
    SUBTREE_STAT_FIELDS,
    NativeSubtreeSession,
)
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import (
    GenerationStats,
    NativeFinalSeriesScoreConfig,
    generate_series,
    play_series,
)
from scottish_progressive.search import (
    MATE_SCORE,
    SearchLimits,
    SeriesSearcher,
    analyze,
)


ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_PATH = ROOT / "experiments" / "neural-ordering-s3-provenance.json"
EXPECTED_ARTIFACT_ID = "spc-nnue-955ab36e1657870a31ee1130"
EXPECTED_ARTIFACT_SHA256 = (
    "4bbab0180470439a17441882b0e5a24870a4a004b4b0fded71646dd305bbcab8"
)
EXPECTED_SCOPE = "complete-boundaries-entering-series-3-only-v1"
MODEL_ID = 1
BLEND_PERCENT = 75

PIECE_SQUARE_OFFSET = 0
PROMOTED_OFFSET = 2 * 6 * 64
MOVER_OFFSET = PROMOTED_OFFSET + 2 * 64
SERIES_OFFSET = MOVER_OFFSET + 2
MOVES_REMAINING_OFFSET = SERIES_OFFSET + 17
QUIET_OFFSET = MOVES_REMAINING_OFFSET + 18
CHECK_OFFSET = QUIET_OFFSET + 12
CASTLING_OFFSET = CHECK_OFFSET + 1
PROGRESSIVE_EP_OFFSET = CASTLING_OFFSET + 4
FEATURE_COUNT = PROGRESSIVE_EP_OFFSET + 64


def _native() -> object:
    native = evaluation._native_eval
    if native is None or not all(
        hasattr(native, name)
        for name in (
            "generate_complete_series",
            "neural_ordering_evaluate",
            "neural_ordering_identity",
            "neural_ordering_parameters",
        )
    ):
        pytest.skip("source-matched neural-ordering native runtime is not built")
    return native


def _network() -> tuple[dict[str, object], bytes]:
    provenance = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
    raw = (PROVENANCE_PATH.parent / str(provenance["frozen_network"])).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_ARTIFACT_SHA256
    return json.loads(raw), raw


def _color_index(color: chess.Color) -> int:
    return 0 if color == chess.WHITE else 1


def _active_features(
    board: chess.Board,
    series_number: int,
    quiet_series: int,
    moves_remaining: int,
    ep_targets: tuple[int, ...],
    known_in_check: bool,
) -> tuple[int, ...]:
    active: list[int] = []
    for square, piece in sorted(board.piece_map().items()):
        active.append(
            PIECE_SQUARE_OFFSET
            + ((_color_index(piece.color) * 6 + piece.piece_type - 1) * 64)
            + square
        )
        if board.promoted & chess.BB_SQUARES[square]:
            active.append(PROMOTED_OFFSET + _color_index(piece.color) * 64 + square)
    active.append(MOVER_OFFSET + _color_index(board.turn))
    active.append(SERIES_OFFSET + min(series_number, 17) - 1)
    active.append(MOVES_REMAINING_OFFSET + min(moves_remaining, 17))
    active.append(QUIET_OFFSET + min(quiet_series, 11))
    if known_in_check:
        active.append(CHECK_OFFSET)
    castling = (
        board.has_kingside_castling_rights(chess.WHITE),
        board.has_queenside_castling_rights(chess.WHITE),
        board.has_kingside_castling_rights(chess.BLACK),
        board.has_queenside_castling_rights(chess.BLACK),
    )
    active.extend(
        CASTLING_OFFSET + index
        for index, enabled in enumerate(castling)
        if enabled
    )
    active.extend(PROGRESSIVE_EP_OFFSET + square for square in ep_targets)
    ordered = tuple(sorted(set(active)))
    assert len(ordered) == len(active)
    assert all(0 <= feature < FEATURE_COUNT for feature in ordered)
    return ordered


def _divide_nearest(numerator: int, denominator: int) -> int:
    sign = -1 if numerator < 0 else 1
    return sign * ((abs(numerator) + denominator // 2) // denominator)


def _predict(network: dict[str, object], features: tuple[int, ...]) -> int:
    assert int(network["hidden_size"]) == 1
    weights = tuple(int(value) for value in network["input_weights"])
    hidden = int(network["hidden_bias"][0]) + sum(weights[index] for index in features)
    activated = max(0, min(hidden, int(network["activation_clip"])))
    accumulator = int(network["output_bias"]) + (
        activated * int(network["output_weights"][0])
    )
    score = _divide_nearest(accumulator, int(network["output_denominator"]))
    clip = int(network["score_clip"])
    return max(-clip, min(score, clip))


def _blend(hand_score: int, neural_score: int, blend_percent: int) -> int:
    return _divide_nearest(
        hand_score * (100 - blend_percent) + neural_score * blend_percent,
        100,
    )


def _native_feature_score(
    native: object,
    board: chess.Board,
    series_number: int,
    quiet_series: int,
    moves_remaining: int,
    ep_targets: tuple[int, ...],
    known_in_check: bool,
) -> tuple[tuple[int, ...], int]:
    features, score = native.neural_ordering_evaluate(
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
        series_number,
        quiet_series,
        moves_remaining,
        known_in_check,
        ep_targets,
    )
    return tuple(int(value) for value in features), int(score)


def _final_score_tuple(
    cap: int,
    *,
    ply_from_root: int = 0,
    neural: tuple[int, int] | None = None,
) -> tuple[int, ...]:
    weights = baseline_profile().weights
    values = (
        cap,
        ply_from_root,
        MATE_SCORE,
        weights.material,
        weights.king_space,
        weights.promotion_corridors,
        weights.immediate_vulnerability,
        weights.boundary_check,
    )
    return values if neural is None else values + neural


def _direct_generation(
    native: object,
    state: ProgressiveState,
    *,
    cap: int,
    neural: tuple[int, int] | None,
    tactical: bool = False,
    frontier_cap: int | None = None,
    ply_from_root: int = 0,
) -> object:
    board = state.board
    weights = baseline_profile().weights
    frontier = None
    if tactical:
        frontier = (
            weights.material,
            weights.king_space,
            weights.promotion_corridors,
            weights.immediate_vulnerability,
            weights.boundary_check,
            1,
        )
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
        (),
        frontier_cap,
        None,
        frontier,
        _final_score_tuple(
            cap,
            ply_from_root=ply_from_root,
            neural=neural,
        ),
    )


def test_frozen_s3_model_identity_and_every_parameter_match_accepted_artifact() -> None:
    native = _native()
    network, _raw = _network()
    assert native.neural_ordering_identity() == (
        EXPECTED_ARTIFACT_ID,
        EXPECTED_ARTIFACT_SHA256,
        FEATURE_COUNT,
        MODEL_ID,
        BLEND_PERCENT,
        EXPECTED_SCOPE,
    )
    compiled = native.neural_ordering_parameters()
    assert set(compiled) == {
        "feature_count",
        "hidden_size",
        "input_weights",
        "hidden_bias",
        "output_weights",
        "output_bias",
        "output_denominator",
        "activation_clip",
        "score_clip",
    }
    for field in (
        "feature_count",
        "hidden_size",
        "output_bias",
        "output_denominator",
        "activation_clip",
        "score_clip",
    ):
        assert int(compiled[field]) == int(network[field])
    for field in ("input_weights", "hidden_bias", "output_weights"):
        assert tuple(int(value) for value in compiled[field]) == tuple(
            int(value) for value in network[field]
        )


def test_neural_ordering_public_api_rejects_inconsistent_bitboards() -> None:
    native = _native()
    a1 = chess.BB_A1
    b1 = chess.BB_B1

    def evaluate(**overrides: int) -> object:
        values = {
            "pawns": a1,
            "knights": 0,
            "bishops": 0,
            "rooks": 0,
            "queens": 0,
            "kings": 0,
            "white_occupied": a1,
            "black_occupied": 0,
            "promoted": 0,
        }
        values.update(overrides)
        return native.neural_ordering_evaluate(
            values["pawns"],
            values["knights"],
            values["bishops"],
            values["rooks"],
            values["queens"],
            values["kings"],
            values["white_occupied"],
            values["black_occupied"],
            values["promoted"],
            0,
            chess.WHITE,
            3,
            0,
            3,
            False,
            (),
        )

    malformed = (
        {"pawns": 0},
        {"knights": a1},
        {"black_occupied": a1},
        {"white_occupied": 0},
        {"promoted": b1},
    )
    for bitboards in malformed:
        with pytest.raises(ValueError, match="invalid neural board bitboards"):
            evaluate(**bitboards)


def test_python_cpp_fixed_point_feature_and_score_parity_1024_states() -> None:
    native = _native()
    network, _raw = _network()
    rng = random.Random(0x5C073)
    board = chess.Board()
    promoted = chess.Board("4k3/8/8/8/8/2Q5/4r3/4K3 w - - 0 1")
    promoted.promoted = chess.BB_C3
    explicit_check = chess.Board("7k/6Q1/6K1/8/8/8/8/8 b - - 0 1")
    special = (promoted, explicit_check)

    for index in range(1_024):
        if index % 29 == 0:
            board = special[(index // 29) % len(special)].copy(stack=False)
        else:
            if board.is_game_over() or index % 47 == 0:
                board = chess.Board()
            legal = sorted(board.legal_moves, key=lambda move: move.uci())
            if legal:
                board.push(legal[rng.randrange(len(legal))])
        sample = board.copy(stack=False)
        parity = 1 if sample.turn == chess.WHITE else 0
        series_choices = [value for value in range(1, 19) if value % 2 == parity]
        series_number = series_choices[rng.randrange(len(series_choices))]
        quiet_series = rng.randrange(15)
        moves_remaining = rng.randrange(series_number + 1)
        ep_targets = tuple(sorted({rng.randrange(64), rng.randrange(64)}))
        known_in_check = sample.is_check()
        expected_features = _active_features(
            sample,
            series_number,
            quiet_series,
            moves_remaining,
            ep_targets,
            known_in_check,
        )
        actual_features, actual_score = _native_feature_score(
            native,
            sample,
            series_number,
            quiet_series,
            moves_remaining,
            ep_targets,
            known_in_check,
        )
        assert actual_features == expected_features
        assert actual_score == _predict(network, expected_features)


def test_hook_disabled_and_blend_zero_are_exact_and_scope_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _native()
    state = ProgressiveState.from_fen("7k/8/8/8/8/8/8/K7 b - - 0 1", 2)
    baseline = _direct_generation(native, state, cap=4, neural=None)
    blend_zero = _direct_generation(native, state, cap=4, neural=(MODEL_ID, 0))
    assert blend_zero == baseline

    outside = ProgressiveState.from_fen("7k/8/8/8/8/8/8/K7 w - - 0 1", 3)
    status, message, _stats, _series = _direct_generation(
        native,
        outside,
        cap=4,
        neural=(MODEL_ID, BLEND_PERCENT),
    )
    assert status == 3
    assert "out of range" in message

    config = NativeFinalSeriesScoreConfig.from_profile(
        baseline_profile(),
        max_returned_series=4,
        ply_from_root=0,
        mate_score=MATE_SCORE,
    )
    monkeypatch.delenv("SPC_NATIVE_NEURAL_S3", raising=False)
    call = rules._native_complete_series_call(
        state,
        GenerationStats(),
        required_prefix=(),
        max_frontier_states=None,
        max_positions=None,
        frontier_score=None,
        native_final_score=config,
        should_stop=None,
        symbols=("generate_complete_series",),
    )
    assert call is not None and len(call[1][-1]) == 8
    call = rules._native_complete_series_call(
        state,
        GenerationStats(),
        required_prefix=(),
        max_frontier_states=None,
        max_positions=None,
        frontier_score=None,
        native_final_score=config,
        should_stop=None,
        symbols=("generate_complete_series",),
        root_contract_s3_neural_ordering=True,
    )
    assert call is not None and call[1][-1][-2:] == (MODEL_ID, BLEND_PERCENT)
    monkeypatch.setenv("SPC_NATIVE_NEURAL_S3", "1")
    call = rules._native_complete_series_call(
        state,
        GenerationStats(),
        required_prefix=(),
        max_frontier_states=None,
        max_positions=None,
        frontier_score=None,
        native_final_score=config,
        should_stop=None,
        symbols=("generate_complete_series",),
    )
    assert call is not None and call[1][-1][-2:] == (MODEL_ID, BLEND_PERCENT)


def test_current_s2_openings_match_python_neural_top_k_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _native()
    network, _raw = _network()
    monkeypatch.delenv("SPC_NATIVE_NEURAL_S3", raising=False)
    roots = {
        result.moves[0]: result.final_state
        for result in generate_series(ProgressiveState.initial())
    }
    profile = baseline_profile()
    for root_id in ("f2f3", "g2g4"):
        root = roots[root_id]
        candidates = generate_series(root)

        def score(result: object) -> int:
            if result.outcome == Outcome.CHECKMATE:
                winner = root.board.turn if result.ended_by_check else not root.board.turn
                return MATE_SCORE if winner == chess.WHITE else -MATE_SCORE
            if result.outcome in {Outcome.STALEMATE, Outcome.TEN_SERIES_DRAW}:
                return 0
            state = result.final_state
            features = _active_features(
                state.board,
                state.series_number,
                state.quiet_series,
                state.series_number,
                state.ep_targets,
                result.ended_by_check,
            )
            return _blend(
                fast_evaluate(state, profile),
                _predict(network, features),
                BLEND_PERCENT,
            )

        expected = sorted(
            candidates,
            key=lambda result: (score(result), result.machine_notation),
        )
        for width in (1, 4, 32):
            status, message, _stats, raw = _direct_generation(
                native,
                root,
                cap=width,
                neural=(MODEL_ID, BLEND_PERCENT),
            )
            assert status == 0, message
            assert ["/".join(item[0]) for item in raw] == [
                result.machine_notation for result in expected[:width]
            ]


def test_s3_neural_ordering_preserves_terminal_mate_and_sound_black_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _native()
    state = ProgressiveState.from_fen(
        "8/8/8/8/8/5kq1/8/7K b - - 0 1",
        2,
    )
    status, message, _stats, raw = _direct_generation(
        native,
        state,
        cap=3,
        neural=(MODEL_ID, BLEND_PERCENT),
        tactical=True,
        frontier_cap=8,
        ply_from_root=MATE_SCORE + 1,
    )
    assert status == 0, message
    replayed = [play_series(state, tuple(item[0])) for item in raw]
    assert replayed
    assert all(
        result.outcome == Outcome.CHECKMATE and result.ended_by_check
        for result in replayed
    )

    monkeypatch.setenv("SPC_NATIVE_NEURAL_S3", "1")
    result = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        baseline_profile(),
    )
    assert result.best_series is not None
    assert result.best_series.outcome == Outcome.CHECKMATE
    assert result.best_series.ended_by_check
    assert result.proof == "black"
    assert result.forced == "black"


def test_root_contract_s2_uses_the_accepted_neural_ordering() -> None:
    native = _native()
    roots = {
        result.moves[0]: result.final_state
        for result in generate_series(ProgressiveState.initial())
    }
    assert set(roots) == {
        move.uci() for move in chess.Board().legal_moves
    }

    def root_contract_order(state: ProgressiveState) -> list[str]:
        session = NativeSubtreeSession(
            max_series_per_node=32,
            max_work=250_000,
            requested_depth=2,
            mate_score=MATE_SCORE,
            cache_capacity=16_384,
            external_cache_weight=0,
            native_threads=1,
            root_tactical_protection=False,
            profile=baseline_profile(),
        )
        manifest = session.enumerate_root(
            state,
            preferred_series=None,
            external_work=0,
            remaining_nanoseconds=None,
        )
        assert manifest.status == 0, manifest.message
        return [
            candidate.series.machine_notation
            for candidate in manifest.candidates
        ]

    def direct_order(
        state: ProgressiveState,
        neural: tuple[int, int] | None,
    ) -> list[str]:
        status, message, _stats, raw = _direct_generation(
            native,
            state,
            cap=32,
            neural=neural,
            tactical=True,
            frontier_cap=32,
            ply_from_root=1,
        )
        assert status == 0, message
        return ["/".join(item[0]) for item in raw]

    changed_roots = 0
    for root in roots.values():
        actual = root_contract_order(root)
        assert actual == direct_order(root, (MODEL_ID, BLEND_PERCENT))
        changed_roots += actual != direct_order(root, None)
    assert changed_roots > 0

    # The production policy is root-only and fails closed outside Black S2.
    initial = ProgressiveState.initial()
    assert root_contract_order(initial) == direct_order(initial, None)
    series_three = generate_series(roots["e2e4"])[0].final_state
    assert series_three.series_number == 3
    assert root_contract_order(series_three) == direct_order(series_three, None)


def test_safe_reselector_s2_w512_matches_root_contract_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SPC_NATIVE_NEURAL_S3", raising=False)
    root = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
    session = NativeSubtreeSession(
        max_series_per_node=512,
        max_work=10_000_000,
        requested_depth=1,
        mate_score=MATE_SCORE,
        cache_capacity=16_384,
        external_cache_weight=0,
        native_threads=1,
        root_tactical_protection=False,
        profile=baseline_profile(),
    )
    manifest = session.enumerate_root(
        root,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert manifest.status == 0, manifest.message
    expected = [
        candidate.series.machine_notation
        for candidate in manifest.candidates
    ]

    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            collect_all_root_scores=False,
            native_threads=1,
        )
    )
    generated, _complete = searcher._generate(  # noqa: SLF001
        root,
        ply_from_root=1,
        required_prefix=(),
        tactical_protection=True,
        max_frontier_states=512,
        max_additional_positions=10_000_000,
        root_contract_s3_neural_ordering=True,
    )
    actual = [
        candidate.machine_notation
        for candidate in (
            generated.references()
            if hasattr(generated, "references")
            else generated
        )
    ]

    assert actual == expected


def test_safe_reselector_s2_ordering_fails_closed_without_native_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=10_000_000,
            collect_all_root_scores=False,
        )
    )
    monkeypatch.setattr(search_module, "_native_complete_series_batch", lambda *_a, **_k: None)

    with pytest.raises(search_module._WorkLimit):  # noqa: SLF001
        searcher._generate(  # noqa: SLF001
            root,
            ply_from_root=1,
            tactical_protection=True,
            max_frontier_states=512,
            max_additional_positions=10_000_000,
            root_contract_s3_neural_ordering=True,
        )


def test_native_subtree_high_series_path_counts_preserve_frozen_python_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equivalent move-order counts must never abort a valid finite search."""

    _native()
    monkeypatch.setenv("SPC_NATIVE_NEURAL_S3", "1")
    state = ProgressiveState.from_fen(
        "8/8/8/8/1K6/8/1k6/8 b - - 100 110",
        24,
        quiet_series=4,
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
        baseline_profile(),
    )

    assert result.best_series is not None
    assert result.best_series.machine_notation == (
        "b2a1/a1a2/a2a1/a1a2/a2a1/a1b2/b2c2/c2b2/b2c2/c2b2/b2c2/"
        "c2b2/b2c2/c2b2/b2c2/c2b2/b2c2/c2b2/b2c2/c2b2/b2c2/c2b2/"
        "b2c2/c2b2"
    )
    assert result.score == 0
    assert result.completed_depth == 2
    assert result.proof is None
    assert result.forced is None
    # Frozen pristine-Python receipt (SPC_DISABLE_NATIVE=1), deterministic 3/3:
    # work=75_658. The optimized native frontier has its own deterministic
    # work receipt; its chess result above is the Python oracle signature. The
    # selected-root ladder is soundly inapplicable because the mover owns only
    # a king, so this pre-existing receipt remains unchanged.
    assert result.stats.generation_positions == 40_647
    assert result.stats.selected_root_ladder_probe_calls == 0
    assert tuple(item.machine_notation for item in result.principal_variation) == (
        result.best_series.machine_notation,
        (
            "b4a4/a4a5/a5a4/a4a5/a5b4/b4b5/b5b4/b4b5/b5b4/b4b5/b5b4/"
            "b4b5/b5b4/b4b5/b5b4/b4b5/b5b4/b4b5/b5b4/b4b5/b5b4/b4b5/"
            "b5b4/b4b5/b5b4"
        ),
    )


def test_native_root_contract_preserves_u64_counts_and_imports_exactly() -> None:
    state = ProgressiveState.from_fen(
        "8/8/8/8/1K6/8/1k6/8 b - - 100 110",
        24,
        quiet_series=4,
    )

    def session() -> NativeSubtreeSession:
        return NativeSubtreeSession(
            max_series_per_node=32,
            max_work=250_000,
            requested_depth=2,
            mate_score=MATE_SCORE,
            cache_capacity=16_384,
            external_cache_weight=0,
            native_threads=1,
            root_tactical_protection=True,
            profile=baseline_profile(),
        )

    source = session()
    legacy = source.search(
        state,
        depth=1,
        alpha=-2 * MATE_SCORE,
        beta=2 * MATE_SCORE,
        ply_from_root=1,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert legacy.status == 0, legacy.message
    manifest = source.enumerate_root(
        state,
        preferred_series=None,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert manifest.status == 0, manifest.message

    javascript_safe_limit = (1 << 53) - 1
    native_limit = (1 << 64) - 1
    assert manifest.candidates
    assert all(
        candidate.series.transposition_count <= native_limit
        for candidate in manifest.candidates
    )
    observed_path_counts = [
        candidate.series.transposition_count
        for candidate in manifest.candidates
    ]
    for field in (
        "generated_raw_series",
        "intra_series_transpositions",
        "frontier_paths_pruned",
    ):
        index = SUBTREE_STAT_FIELDS.index(field)
        observed_path_counts.extend(
            (
                manifest.work.cumulative_stats[index],
                manifest.work.call_stats[index],
            )
        )
    assert all(value <= native_limit for value in observed_path_counts)
    assert any(value > javascript_safe_limit for value in observed_path_counts)

    imported = session().import_root(
        state,
        manifest,
        external_work=0,
        remaining_nanoseconds=None,
    )
    assert imported.status == 0, imported.message
    assert imported.enumeration_identity == manifest.enumeration_identity
    assert tuple(candidate.transport for candidate in imported.candidates) == tuple(
        candidate.transport for candidate in manifest.candidates
    )
