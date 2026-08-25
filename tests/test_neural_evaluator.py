from dataclasses import replace
import hashlib
import json

import chess
import pytest

from scottish_progressive.evaluation import evaluate
from scottish_progressive.fast_training import CachedFeatures
from scottish_progressive.league import run_rules_tactical_gate
from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT, ProgressiveState
from scottish_progressive.neural_evaluator import (
    CASTLING_OFFSET,
    CHECK_OFFSET,
    FEATURE_COUNT,
    FEATURE_FINGERPRINT,
    NEURAL_INFERENCE_SCOPE,
    MOVER_OFFSET,
    MOVES_REMAINING_OFFSET,
    PROGRESSIVE_EP_OFFSET,
    QUIET_OFFSET,
    SERIES_OFFSET,
    FixedPointNetwork,
    NeuralBlend,
    NeuralSample,
    NeuralTrainerConfig,
    build_neural_dataset_from_weak_corpus,
    build_neural_dataset,
    extract_active_features,
    load_dataset,
    load_network,
    load_optional_blend,
    piece_square_feature,
    promoted_feature,
    sample_from_teacher_result,
    samples_from_weak_corpus,
    save_dataset,
    save_network,
    train_fixed_point_network,
)
from scottish_progressive.profiles import baseline_profile, mutate_profile
from scottish_progressive.search import (
    SearchLimits,
    SearchResult,
    SearchStats,
    analyze,
)
from scottish_progressive.selfplay_training import (
    FULLGAME_CORPUS_METHOD,
    SelfPlayCorpus,
    SelfPlaySample,
)


def _network(*, hidden_size: int = 1, neural_score: int = 0) -> FixedPointNetwork:
    denominator = 256
    return FixedPointNetwork(
        source_fingerprint=ENGINE_SOURCE_FINGERPRINT,
        base_profile_id=baseline_profile().profile_id,
        teacher_fingerprint="teacher-fixture",
        corpus_fingerprint="corpus-fixture",
        trainer_fingerprint="trainer-fixture",
        hidden_size=hidden_size,
        input_weights=(0,) * (FEATURE_COUNT * hidden_size),
        hidden_bias=(0,) * hidden_size,
        output_weights=(0,) * hidden_size,
        output_bias=neural_score * denominator,
        output_denominator=denominator,
        recommended_blend_percent=25,
    )


def _teacher_sample(
    index: int,
    *,
    shared_state: ProgressiveState | None = None,
    include_weak: bool = False,
) -> NeuralSample:
    if shared_state is None:
        square = chess.square(index % 8, 1 + (index // 8) % 5)
        board = chess.Board(None)
        board.set_piece_at(chess.E1, chess.Piece(chess.KING, chess.WHITE))
        board.set_piece_at(chess.E8, chess.Piece(chess.KING, chess.BLACK))
        board.set_piece_at(square, chess.Piece(chess.PAWN, chess.WHITE))
        board.turn = chess.WHITE
        state = ProgressiveState(board, series_number=1)
    else:
        state = shared_state
    return NeuralSample(
        game_key=f"game-{index}",
        position_hash=state.position_hash,
        pfen=state.pfen,
        active_features=extract_active_features(state),
        base_profile_id=baseline_profile().profile_id,
        base_hand_score=evaluate(state).total,
        teacher_score=(index - 15) * 40,
        teacher_proof=None,
        teacher_result_fingerprint=f"teacher-result-{index}",
        teacher_profile_id=baseline_profile().profile_id,
        teacher_completed_depth=4,
        teacher_exact_width=index % 2 == 0,
        weak_wdl_milli=(index * 37) % 1_001 if include_weak else None,
        weak_source_fingerprint="weak-corpus-fixture" if include_weak else None,
    )


def test_feature_schema_covers_progressive_state_not_just_orthodox_fen() -> None:
    initial = set(extract_active_features(ProgressiveState.initial()))
    assert MOVER_OFFSET in initial
    assert SERIES_OFFSET in initial
    assert MOVES_REMAINING_OFFSET + 1 in initial
    assert QUIET_OFFSET in initial
    assert set(range(CASTLING_OFFSET, CASTLING_OFFSET + 4)) <= initial

    ep_state = ProgressiveState.from_fen(
        "7k/8/8/pPpP4/8/8/8/K7 w - - 0 1",
        3,
        quiet_series=7,
        ep_targets=(chess.A6, chess.C6),
    )
    ep_features = set(extract_active_features(ep_state, moves_remaining=2))
    assert SERIES_OFFSET + 2 in ep_features
    assert MOVES_REMAINING_OFFSET + 2 in ep_features
    assert QUIET_OFFSET + 7 in ep_features
    assert PROGRESSIVE_EP_OFFSET + chess.A6 in ep_features
    assert PROGRESSIVE_EP_OFFSET + chess.C6 in ep_features

    board = chess.Board("4k3/8/8/8/8/2Q5/4r3/4K3 w - - 0 1")
    board.promoted = chess.BB_C3
    checked = ProgressiveState(board, series_number=3)
    checked_features = set(extract_active_features(checked))
    assert CHECK_OFFSET in checked_features
    assert piece_square_feature(chess.WHITE, chess.QUEEN, chess.C3) in checked_features
    assert promoted_feature(chess.WHITE, chess.C3) in checked_features
    assert FEATURE_COUNT == 1_014
    assert len(FEATURE_FINGERPRINT) == 64


def test_fixed_point_inference_is_exact_and_artifact_is_tamper_evident(tmp_path) -> None:
    state = ProgressiveState.initial()
    feature = piece_square_feature(chess.WHITE, chess.KING, chess.E1)
    weights = [0] * FEATURE_COUNT
    weights[feature] = 7
    network = FixedPointNetwork(
        source_fingerprint=ENGINE_SOURCE_FINGERPRINT,
        base_profile_id=baseline_profile().profile_id,
        teacher_fingerprint="teacher-fixture",
        corpus_fingerprint="corpus-fixture",
        trainer_fingerprint="trainer-fixture",
        hidden_size=1,
        input_weights=tuple(weights),
        hidden_bias=(3,),
        output_weights=(256,),
        output_bias=-2 * 256,
        output_denominator=256,
        recommended_blend_percent=25,
    )
    assert network.predict(state) == 8

    path = save_network(network, tmp_path / "network.json")
    loaded = load_network(path)
    assert loaded == network
    assert loaded.artifact_id == network.artifact_id
    assert loaded.inference_scope == NEURAL_INFERENCE_SCOPE

    payload = loaded.as_dict()
    payload["output_bias"] += 1
    with pytest.raises(ValueError, match="artifact_id"):
        FixedPointNetwork.from_dict(payload)


def test_missing_or_stale_artifact_explicitly_falls_back_to_hand(tmp_path) -> None:
    profile = baseline_profile()
    overlay, status = load_optional_blend(tmp_path / "missing.json", profile)
    assert overlay is None
    assert status.startswith("hand-evaluator-fallback:")

    stale = replace(_network(), source_fingerprint="stale-source")
    path = save_network(stale, tmp_path / "stale.json")
    overlay, status = load_optional_blend(path, profile)
    assert overlay is None
    assert "source fingerprint is stale" in status


def test_teacher_search_labels_reject_incomplete_or_shallow_results() -> None:
    state = ProgressiveState.initial()
    result = SearchResult(
        score=123,
        best_series=None,
        principal_variation=(),
        alternatives=(),
        requested_depth=4,
        completed_depth=4,
        exact_width=False,
        timed_out=False,
        elapsed_seconds=1.0,
        stats=SearchStats(),
        root_evaluation=evaluate(state),
        proof="white",
        max_series_per_node=32,
        max_generation_positions=1_000_000,
        root_scores_complete=True,
        engine_profile_id=baseline_profile().profile_id,
    )
    sample = sample_from_teacher_result(state, result, game_key="teacher-game")
    assert sample.teacher_score == 123
    assert sample.teacher_proof == "white"
    assert sample.weak_wdl_milli is None
    assert sample.teacher_result_fingerprint.startswith("spc-teacher-result-")

    with pytest.raises(ValueError, match="required deeper depth"):
        sample_from_teacher_result(
            state,
            replace(result, requested_depth=3, completed_depth=3),
            game_key="too-shallow",
        )
    with pytest.raises(ValueError, match="interrupted"):
        sample_from_teacher_result(
            state,
            replace(result, timed_out=True),
            game_key="timed-out",
        )
    with pytest.raises(ValueError, match="root scores"):
        sample_from_teacher_result(
            state,
            replace(result, root_scores_complete=False),
            game_key="best-only",
        )


def test_fullgame_rollout_corpus_stays_an_explicitly_weak_label() -> None:
    state = ProgressiveState.initial()
    replayed = SelfPlaySample(
        position_hash=state.position_hash,
        pfen=state.pfen,
        run_id="fullgame-run",
        game_key="fullgame-1",
        opening_case_id="after-s1-e2e4",
        line_family="fixture-family",
        split_component="fixture-component",
        split="train",
        series_number=state.series_number,
        mover="white",
        profile_id=baseline_profile().profile_id,
        chosen_series="e2e4",
        result="1-0",
        target_white_score=1.0,
        sample_weight=0.5,
        features=CachedFeatures.from_state(state),
    )
    corpus = SelfPlayCorpus(
        seed=1,
        holdout_percent=0,
        database_evidence=({"source": "fixture"},),
        completed_games=1,
        excluded_games=0,
        samples=(replayed,),
        method=FULLGAME_CORPUS_METHOD,
    )

    samples = samples_from_weak_corpus(corpus, base_profile=baseline_profile())
    assert len(samples) == 1
    assert samples[0].teacher_score is None
    assert samples[0].teacher_result_fingerprint is None
    assert samples[0].weak_wdl_milli == 1_000
    assert samples[0].weak_source_fingerprint.startswith("spc-weak-corpus-")


def test_fullgame_neural_bridge_preserves_train_and_never_rejoins_dense_splits(
    tmp_path,
) -> None:
    profile = baseline_profile()

    def state_with_pawn(square: chess.Square) -> ProgressiveState:
        board = chess.Board(None)
        board.set_piece_at(chess.E1, chess.Piece(chess.KING, chess.WHITE))
        board.set_piece_at(chess.E8, chess.Piece(chess.KING, chess.BLACK))
        board.set_piece_at(square, chess.Piece(chess.PAWN, chess.WHITE))
        board.turn = chess.BLACK
        return ProgressiveState(board, series_number=2)

    shared = state_with_pawn(chess.A2)
    replayed: list[SelfPlaySample] = []
    for game_index in range(16):
        split = "train" if game_index < 4 else "holdout"
        unique = state_with_pawn(chess.square(game_index % 8, 2 + game_index // 8))
        for sample_index, state in enumerate((shared, unique)):
            replayed.append(
                SelfPlaySample(
                    position_hash=state.position_hash,
                    pfen=state.pfen,
                    run_id="fullgame-run",
                    game_key=f"fullgame-{game_index}",
                    opening_case_id="fixture",
                    line_family="fixture",
                    split_component=f"source-component-{game_index}",
                    split=split,
                    series_number=state.series_number,
                    mover="black",
                    profile_id=profile.profile_id,
                    chosen_series=f"fixture-{sample_index}",
                    result="1-0" if game_index % 2 else "0-1",
                    target_white_score=1.0 if game_index % 2 else 0.0,
                    sample_weight=0.5,
                    features=CachedFeatures.from_state(state),
                )
            )
    corpus = SelfPlayCorpus(
        seed=1,
        holdout_percent=20,
        database_evidence=({"source": "fixture"},),
        completed_games=16,
        excluded_games=0,
        samples=tuple(replayed),
        method=FULLGAME_CORPUS_METHOD,
    )

    dataset = None
    for seed in range(100):
        candidate = build_neural_dataset_from_weak_corpus(
            corpus,
            base_profile=profile,
            seed=seed,
            validation_percent=10,
            test_percent=10,
            max_positions_per_game=2,
        )
        if all(candidate.split_samples(split) for split in ("train", "validation", "test")):
            dataset = candidate
            break
    assert dataset is not None
    assert {
        sample.game_key for sample in dataset.split_samples("train")
    } <= {f"fullgame-{index}" for index in range(4)}
    for split in ("train", "validation", "test"):
        samples = dataset.split_samples(split)
        other_hashes = {
            sample.position_hash
            for other in ("train", "validation", "test")
            if other != split
            for sample in dataset.split_samples(other)
        }
        assert {sample.position_hash for sample in samples}.isdisjoint(other_hashes)
    by_game: dict[str, list[NeuralSample]] = {}
    for sample in dataset.samples:
        by_game.setdefault(sample.game_key, []).append(sample)
    assert all(len({sample.split for sample in samples}) == 1 for samples in by_game.values())
    assert all(sum(sample.sample_weight_milli for sample in samples) == 1_000 for samples in by_game.values())
    saved = save_dataset(dataset, tmp_path / "dataset.json")
    assert load_dataset(saved) == dataset


def test_dataset_keeps_whole_games_and_transpositions_out_of_other_splits() -> None:
    shared = ProgressiveState.initial()
    samples = [
        _teacher_sample(0, shared_state=shared),
        replace(
            _teacher_sample(1, shared_state=shared),
            game_key="game-1",
        ),
        *(_teacher_sample(index) for index in range(2, 32)),
    ]
    selected = None
    for seed in range(1_000):
        candidate = build_neural_dataset(
            samples,
            base_profile_id=baseline_profile().profile_id,
            seed=seed,
            validation_percent=20,
            test_percent=20,
        )
        if all(candidate.split_samples(split) for split in ("train", "validation", "test")):
            selected = candidate
            break
    assert selected is not None

    shared_rows = [
        sample for sample in selected.samples if sample.position_hash == shared.position_hash
    ]
    assert len({sample.split for sample in shared_rows}) == 1
    for split in ("train", "validation", "test"):
        games = {sample.game_key for sample in selected.split_samples(split)}
        positions = {sample.position_hash for sample in selected.split_samples(split)}
        for other in ("train", "validation", "test"):
            if other == split:
                continue
            assert games.isdisjoint(
                {sample.game_key for sample in selected.split_samples(other)}
            )
            assert positions.isdisjoint(
                {sample.position_hash for sample in selected.split_samples(other)}
            )


def test_trainer_separates_deeper_teacher_and_weak_rollout_objectives() -> None:
    samples = [_teacher_sample(index, include_weak=True) for index in range(32)]
    dataset = build_neural_dataset(
        samples,
        base_profile_id=baseline_profile().profile_id,
        seed=29,
        validation_percent=0,
        test_percent=0,
    )
    config = NeuralTrainerConfig(
        hidden_size=4,
        epochs=3,
        learning_rate_millionths=2_000,
        weak_label_weight_milli=100,
    )
    first, first_report = train_fixed_point_network(dataset, config=config)
    second, second_report = train_fixed_point_network(dataset, config=config)

    assert first == second
    assert first.artifact_id == second.artifact_id
    assert first_report == second_report
    assert first.source_fingerprint == ENGINE_SOURCE_FINGERPRINT
    assert first.feature_fingerprint == FEATURE_FINGERPRINT
    assert first.teacher_fingerprint == dataset.teacher_fingerprint
    assert first.corpus_fingerprint == dataset.corpus_fingerprint
    assert first.trainer_fingerprint == config.trainer_fingerprint
    assert first_report["labels"]["teacher"] == 32
    assert first_report["labels"]["weak_rollout_wdl"] == 32
    assert first_report["labels"]["weak_relative_weight_milli"] == 100
    assert first_report["strength_claim"] is False
    assert first_report["promotion_eligible"] is False
    assert first_report["inference_scope"] == NEURAL_INFERENCE_SCOPE
    assert first_report["partial_frontier_scoring_eligible"] is False


def test_dataset_streaming_identity_matches_v1_and_enforces_artifact_cap(tmp_path) -> None:
    dataset = build_neural_dataset(
        [_teacher_sample(index, include_weak=True) for index in range(8)],
        base_profile_id=baseline_profile().profile_id,
        seed=71,
        validation_percent=0,
        test_percent=0,
    )
    historical = "spc-neural-corpus-" + hashlib.sha256(
        json.dumps(
            dataset.deterministic_payload(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert dataset.corpus_fingerprint == historical

    too_small = tmp_path / "too-small.json"
    with pytest.raises(ValueError, match="artifact cap"):
        save_dataset(dataset, too_small, max_bytes=100)
    assert not too_small.exists()
    assert not list(tmp_path.glob(".too-small.json.*.tmp"))

    saved = save_dataset(dataset, tmp_path / "bounded.json", max_bytes=128 * 1024)
    assert saved.stat().st_size < 128 * 1024
    assert load_dataset(saved) == dataset


def test_trainer_retains_all_teachers_caps_weak_rows_and_seals_test_metrics() -> None:
    teachers = [_teacher_sample(index, include_weak=True) for index in range(6)]
    weak_only = [
        replace(
            _teacher_sample(index + 100, include_weak=True),
            teacher_score=None,
            teacher_proof=None,
            teacher_result_fingerprint=None,
            teacher_profile_id=None,
            teacher_completed_depth=None,
            teacher_exact_width=None,
        )
        for index in range(20)
    ]
    dataset = build_neural_dataset(
        [*teachers, *weak_only],
        base_profile_id=baseline_profile().profile_id,
        seed=91,
        validation_percent=0,
        test_percent=0,
    )
    config = NeuralTrainerConfig(
        hidden_size=2,
        epochs=1,
        max_weak_train_samples=5,
    )
    _network_result, report = train_fixed_point_network(dataset, config=config)
    selection = report["training_selection"]
    assert selection["available_teacher_samples"] == 6
    assert selection["selected_teacher_samples"] == 6
    assert selection["available_weak_only_samples"] == 20
    assert selection["selected_weak_only_samples"] == 5
    assert selection["selected_train_samples"] == 11
    assert report["metrics"]["test"]["sealed"] is True
    assert report["metrics"]["test"]["teacher"]["mean_absolute_error"] is None


def test_neural_overlay_uses_same_depth_three_search_api_and_zero_blend_is_fallback() -> None:
    state = ProgressiveState.initial()
    profile = baseline_profile()
    limits = SearchLimits(
        depth_series=3,
        max_series_per_node=1,
        max_generation_positions=20_000,
        collect_all_root_scores=False,
    )
    baseline = analyze(state, limits, profile)
    overlay = NeuralBlend.for_profile(_network(neural_score=50_000), profile, blend_percent=0)
    neural = analyze(
        state,
        limits,
        profile,
        evaluation_overlay=overlay,
    )

    assert neural.score == baseline.score
    assert (
        neural.best_series.machine_notation if neural.best_series else None
    ) == (baseline.best_series.machine_notation if baseline.best_series else None)
    assert neural.completed_depth == baseline.completed_depth
    assert neural.engine_profile_id == overlay.variant_id
    assert neural.engine_profile_name == overlay.name

    active_overlay = NeuralBlend.for_profile(
        _network(neural_score=50_000),
        profile,
        blend_percent=25,
    )
    active = analyze(
        state,
        limits,
        profile,
        evaluation_overlay=active_overlay,
    )
    assert active.requested_depth == 3
    assert active.completed_depth == 3
    assert active.engine_profile_id == active_overlay.variant_id
    assert active_overlay.score(state, 1_000) == 13_250

    with pytest.raises(ValueError, match="different base profile"):
        analyze(
            state,
            limits,
            profile,
            evaluation_overlay=replace(overlay, base_profile_id="wrong-profile"),
        )


def test_neural_blend_is_profile_bound_serializable_and_runs_tactical_gate() -> None:
    profile = baseline_profile()
    overlay = NeuralBlend.for_profile(
        _network(neural_score=500),
        profile,
        blend_percent=20,
    )
    restored = NeuralBlend.from_dict(overlay.as_dict(), profile=profile)
    assert restored == overlay
    wrong = mutate_profile(profile, seed=912, name="wrong base")
    with pytest.raises(ValueError, match="different profile"):
        NeuralBlend.from_dict(overlay.as_dict(), profile=wrong)

    gate = run_rules_tactical_gate(
        profile,
        search_depth=2,
        max_series_per_node=32,
        evaluation_overlay=overlay,
    )
    assert gate.passed
