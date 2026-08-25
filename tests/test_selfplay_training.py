from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from scottish_progressive.fast_training import CachedFeatures
from scottish_progressive.fullgame import FullGameSemanticConfig
from scottish_progressive.fullgame_codec import (
    FullGameRecord,
    RejectReason,
    RejectedAttempt,
    Terminal,
)
from scottish_progressive.cli import main
from scottish_progressive.league import OPENING_SUITE_VERSION, OpeningCase
from scottish_progressive.model import ProgressiveState
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.selfplay_training import (
    FULLGAME_CORPUS_METHOD,
    HUMAN_REFUTATION_BLUNDERS,
    HUMAN_REFUTATION_GATE_ID,
    HUMAN_REFUTATION_TRACE,
    SelfPlayCorpus,
    SelfPlaySample,
    _ReplayedGame,
    _samples_from_replayed_full_games,
    build_fullgame_corpus,
    build_selfplay_corpus,
    build_verified_fullgame_corpus,
    evaluate_human_refutation_gate,
    tune_selfplay_profile,
)


MATE_FEN = "7k/8/5KQ1/8/8/8/8/8 w - - 0 1"
SHORT_FULL_GAME = (
    ("d2d3",),
    ("e7e5", "e8e7"),
    ("d1d2", "d2f4", "f4e5"),
)


def _mate_game(job_key: str, case_id: str, *, series: str = "g6g7") -> dict[str, object]:
    opening = OpeningCase(case_id, MATE_FEN, 1, source="unit fixture")
    state = opening.state()
    result = play_series(state, ("g6g7",))
    return {
        "job_key": job_key,
        "run_id": "run-1",
        "opening_suite_version": "unit-suite-v1",
        "opening_case_id": case_id,
        "opening_json": json.dumps(opening.as_dict(), sort_keys=True),
        "white_profile_id": "white-profile",
        "black_profile_id": "black-profile",
        "result": "1-0",
        "terminal_reason": "checkmate",
        "engine_failure_profile_id": None,
        "error": None,
        "start_pfen": state.pfen,
        "final_pfen": result.final_state.pfen,
        "series_played": 1,
        "trace_json": json.dumps(
            [
                {
                    "series_number": 1,
                    "profile_id": "white-profile",
                    "series": series,
                    "played": True,
                    "outcome": "checkmate",
                }
            ],
            sort_keys=True,
        ),
    }


def _write_database(path: Path, games: list[dict[str, object]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL
            );
            CREATE TABLE games (
                job_key TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                opening_suite_version TEXT NOT NULL,
                opening_case_id TEXT NOT NULL,
                opening_json TEXT NOT NULL,
                white_profile_id TEXT NOT NULL,
                black_profile_id TEXT NOT NULL,
                result TEXT NOT NULL,
                terminal_reason TEXT NOT NULL,
                engine_failure_profile_id TEXT,
                error TEXT,
                start_pfen TEXT NOT NULL,
                final_pfen TEXT NOT NULL,
                series_played INTEGER NOT NULL,
                trace_json TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "insert into runs values (?, ?, ?, ?)",
            ("run-1", "complete", "spc-test", "source-test"),
        )
        columns = tuple(games[0])
        placeholders = ",".join("?" for _ in columns)
        for game in games:
            connection.execute(
                f"insert into games ({','.join(columns)}) values ({placeholders})",
                tuple(game[column] for column in columns),
            )
        connection.commit()
    finally:
        connection.close()


def test_corpus_replays_games_and_joins_transposed_line_families(tmp_path: Path) -> None:
    database = tmp_path / "league.sqlite3"
    _write_database(
        database,
        [
            _mate_game("game-a", "custom-a"),
            _mate_game("game-b", "custom-b"),
        ],
    )

    first = build_selfplay_corpus((database,), seed=19, holdout_percent=50)
    second = build_selfplay_corpus((database,), seed=19, holdout_percent=50)

    assert first.corpus_id == second.corpus_id
    assert first.completed_games == 2
    assert first.excluded_games == 0
    assert len(first.samples) == 2
    assert {sample.position_hash for sample in first.samples} == {
        ProgressiveState.from_fen(MATE_FEN, 1).position_hash
    }
    assert len({sample.split_component for sample in first.samples}) == 1
    assert len({sample.split for sample in first.samples}) == 1
    assert sum(sample.sample_weight for sample in first.samples) == 2.0
    assert all(sample.target_white_score == 1.0 for sample in first.samples)
    evidence = first.database_evidence[0]
    assert len(str(evidence["main_file_sha256"])) == 64
    assert len(str(evidence["logical_content_sha256"])) == 64


def test_corpus_rejects_a_trace_that_does_not_replay(tmp_path: Path) -> None:
    database = tmp_path / "tampered.sqlite3"
    _write_database(database, [_mate_game("game-bad", "custom", series="g6g8")])

    with pytest.raises(ValueError, match="illegal series|outcome diverges"):
        build_selfplay_corpus((database,))


def test_corpus_rejects_opening_case_metadata_tampering(tmp_path: Path) -> None:
    database = tmp_path / "tampered-case.sqlite3"
    game = _mate_game("game-bad-case", "row-case")
    opening = json.loads(str(game["opening_json"]))
    opening["case_id"] = "after-1-e4"
    game["opening_json"] = json.dumps(opening, sort_keys=True)
    _write_database(database, [game])

    with pytest.raises(ValueError, match="case id does not match"):
        build_selfplay_corpus((database,))


def test_corpus_rejects_noncanonical_boundary_for_known_suite(tmp_path: Path) -> None:
    database = tmp_path / "tampered-canonical.sqlite3"
    game = _mate_game("game-bad-boundary", "after-1-e4")
    game["opening_suite_version"] = OPENING_SUITE_VERSION
    _write_database(database, [game])

    with pytest.raises(ValueError, match="canonical opening boundary diverges"):
        build_selfplay_corpus((database,))


def test_corpus_logical_digest_includes_committed_wal_rows(tmp_path: Path) -> None:
    database = tmp_path / "wal-backed.sqlite3"
    first_game = _mate_game("game-wal-a", "wal-a")
    _write_database(database, [first_game])

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("pragma journal_mode=wal").fetchone()[0] == "wal"
        before = build_selfplay_corpus((database,), holdout_percent=0)
        second_game = _mate_game("game-wal-b", "wal-b")
        columns = tuple(second_game)
        placeholders = ",".join("?" for _ in columns)
        connection.execute(
            f"insert into games ({','.join(columns)}) values ({placeholders})",
            tuple(second_game[column] for column in columns),
        )
        connection.commit()

        after = build_selfplay_corpus((database,), holdout_percent=0)
        assert after.completed_games == 2
        assert (
            before.database_evidence[0]["logical_content_sha256"]
            != after.database_evidence[0]["logical_content_sha256"]
        )
        assert (
            before.database_evidence[0]["main_file_sha256"]
            == after.database_evidence[0]["main_file_sha256"]
        )
    finally:
        connection.close()


def _fullgame_fixture(
    attempt_index: int,
    series: tuple[tuple[str, ...], ...],
) -> FullGameRecord:
    return FullGameRecord(
        attempt_index=attempt_index,
        terminal=Terminal.CHECKMATE_WHITE,
        series=series,
        logical_work=100 + attempt_index,
    )


def test_fullgame_corpus_replays_weak_labels_without_universal_root_leakage() -> None:
    config = FullGameSemanticConfig.from_profile(baseline_profile())
    records = (
        _fullgame_fixture(0, HUMAN_REFUTATION_TRACE),
        _fullgame_fixture(1, SHORT_FULL_GAME),
        RejectedAttempt(2, RejectReason.WORK_LIMIT, logical_work=77),
    )

    corpus = None
    for seed in range(100):
        candidate = build_fullgame_corpus(
            records,
            config,
            seed=seed,
            holdout_percent=50,
        )
        if candidate.train_samples and candidate.holdout_samples:
            corpus = candidate
            break
    assert corpus is not None
    assert corpus.method == FULLGAME_CORPUS_METHOD
    assert corpus.completed_games == 2
    assert corpus.excluded_games == 1
    assert len(corpus.samples) == 6
    assert all(sample.series_number >= 2 for sample in corpus.samples)
    assert ProgressiveState.initial().position_hash not in {
        sample.position_hash for sample in corpus.samples
    }
    assert {
        sample.profile_id for sample in corpus.samples
    } == {baseline_profile().profile_id}
    assert {
        sample.position_hash for sample in corpus.train_samples
    }.isdisjoint(
        sample.position_hash for sample in corpus.holdout_samples
    )
    games: dict[str, list[SelfPlaySample]] = {}
    for sample in corpus.samples:
        games.setdefault(sample.game_key, []).append(sample)
    assert len(games) == 2
    for samples in games.values():
        assert len({sample.split for sample in samples}) == 1
        assert sum(sample.sample_weight for sample in samples) == pytest.approx(1.0)
    assert "weak value label" in corpus.as_dict()["summary"]["label_contract"]
    assert "never promotion evidence" in corpus.as_dict()["summary"][
        "label_contract"
    ]


def test_dense_fullgame_transpositions_keep_per_game_splits_disjoint() -> None:
    initial = ProgressiveState.initial()
    shared = play_series(initial, ("e2e4",)).final_state
    first_moves = tuple(
        move.uci()
        for move in initial.board.legal_moves
        if move.uci() != "e2e4"
    )[:12]
    profile_id = baseline_profile().profile_id
    games = tuple(
        _ReplayedGame(
            run_id="dense-fullgame-run",
            game_key=f"dense-game-{index:02d}",
            opening_case_id=f"after-s1-{move}",
            line_family=f"fullgame-after-s1:{index:02d}",
            result="1-0",
            target_white_score=1.0,
            states=(play_series(initial, (move,)).final_state, shared),
            profile_ids=(profile_id, profile_id),
            chosen_series=("a7a6/b7b6", "a7a6/b7b6"),
        )
        for index, move in enumerate(first_moves)
    )

    selected = None
    for seed in range(100):
        first = _samples_from_replayed_full_games(
            games, seed=seed, holdout_percent=50
        )
        if {sample.split for sample in first[0]} == {"train", "holdout"}:
            repeated = _samples_from_replayed_full_games(
                games, seed=seed, holdout_percent=50
            )
            selected = (first, repeated)
            break
    assert selected is not None
    (samples, shadowed, removed), repeated = selected
    assert (samples, shadowed, removed) == repeated
    assert shadowed == 0
    assert removed > 0
    assert len({sample.split_component for sample in samples}) == len(games)
    assert initial.position_hash not in {
        sample.position_hash for sample in samples
    }

    games_by_key: dict[str, list[SelfPlaySample]] = {}
    for sample in samples:
        games_by_key.setdefault(sample.game_key, []).append(sample)
    assert set(games_by_key) == {game.game_key for game in games}
    for game_samples in games_by_key.values():
        assert len({sample.split for sample in game_samples}) == 1
        assert len({sample.split_component for sample in game_samples}) == 1
        assert sum(sample.sample_weight for sample in game_samples) == pytest.approx(1.0)
    train_hashes = {
        sample.position_hash for sample in samples if sample.split == "train"
    }
    holdout_hashes = {
        sample.position_hash for sample in samples if sample.split == "holdout"
    }
    assert train_hashes.isdisjoint(holdout_hashes)
    assert shared.position_hash in train_hashes
    assert shared.position_hash not in holdout_hashes


def test_fullgame_corpus_rejects_duplicate_and_illegal_accepted_traces() -> None:
    config = FullGameSemanticConfig.from_profile(baseline_profile())
    duplicate = _fullgame_fixture(1, HUMAN_REFUTATION_TRACE)
    with pytest.raises(ValueError, match="duplicate accepted trace"):
        build_fullgame_corpus(
            (_fullgame_fixture(0, HUMAN_REFUTATION_TRACE), duplicate),
            config,
        )

    illegal = _fullgame_fixture(
        0,
        (*HUMAN_REFUTATION_TRACE[:-1], (("d1c2", "c2c7"))),
    )
    with pytest.raises(
        ValueError, match="illegal|incomplete|terminal|checkmate|authoritative"
    ):
        build_fullgame_corpus((illegal,), config)

    bad_attribution = FullGameRecord(
        attempt_index=0,
        terminal=Terminal.CHECKMATE_WHITE,
        series=SHORT_FULL_GAME,
        white_profile_index=1,
    )
    with pytest.raises(ValueError, match="invalid profile attribution"):
        build_fullgame_corpus((bad_attribution,), config)


def test_verified_fullgame_adapter_uses_verified_store_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scottish_progressive.fullgame as fullgame

    config = FullGameSemanticConfig.from_profile(baseline_profile())
    manifest = {"semantic_config": config.as_dict()}
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )
    verification = {
        "accepted_unique_games": 1,
        "authoritative_replay": "passed",
        "checkpoint_rejections": 2,
        "logical_work": 999,
        "micro_moves": 6,
        "path_count_saturations": 0,
        "series": 3,
        "simulation_id": config.simulation_id,
        "terminal_counts": {"checkmate_white": 1},
        "trace_deduplication": "passed",
    }
    monkeypatch.setattr(fullgame, "verify_fullgame_run", lambda root: verification)
    monkeypatch.setattr(
        fullgame,
        "iter_fullgame_records",
        lambda root: iter((_fullgame_fixture(0, SHORT_FULL_GAME),)),
    )

    corpus = build_verified_fullgame_corpus(
        tmp_path,
        holdout_percent=0,
        max_games=1,
    )

    assert corpus.completed_games == 1
    assert corpus.excluded_games == 2
    assert corpus.database_evidence[1]["authoritative_replay"] == "passed"
    assert corpus.database_evidence[1]["trace_deduplication"] == "passed"

    verification["accepted_unique_games"] = 2
    prefix = build_verified_fullgame_corpus(
        tmp_path,
        holdout_percent=0,
        max_games=1,
    )
    assert prefix.excluded_games == 0
    assert prefix.database_evidence[1]["consumed_entire_snapshot"] is False
    assert "store-wide evidence only" in prefix.database_evidence[1][
        "checkpoint_rejections_scope"
    ]


def test_human_refutation_gate_requires_complete_search_and_avoids_both_blunders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_complete_analyze(state, limits, profile):
        safe = {
            2: ("e7e6", "e8e7"),
            4: ("b8c6", "c6d4", "g8f6", "d4f3"),
        }[state.series_number]
        selected = play_series(state, safe)
        return SimpleNamespace(
            best_series=selected,
            principal_variation=(selected,),
            score=123,
            requested_depth=limits.depth_series,
            completed_depth=limits.depth_series,
            exact_width=False,
            timed_out=False,
            work_limit_reached=False,
            stats=SimpleNamespace(work_positions=1234, nodes=42),
            elapsed_seconds=0.25,
        )

    monkeypatch.setattr(
        "scottish_progressive.selfplay_training.analyze", fake_complete_analyze
    )
    passed = evaluate_human_refutation_gate(baseline_profile())
    assert passed["gate_id"] == HUMAN_REFUTATION_GATE_ID
    assert passed["passed"] is True
    assert passed["fixture"]["terminal"] == "checkmate-white"
    assert all(anchor["avoided_known_blunder"] for anchor in passed["anchors"])

    def fake_incomplete_analyze(state, limits, profile):
        selected = play_series(
            state, HUMAN_REFUTATION_BLUNDERS[state.series_number]
        )
        return SimpleNamespace(
            best_series=selected,
            principal_variation=(selected,),
            score=-1,
            requested_depth=limits.depth_series,
            completed_depth=1,
            exact_width=False,
            timed_out=True,
            work_limit_reached=False,
            stats=SimpleNamespace(work_positions=500, nodes=12),
            elapsed_seconds=5.0,
        )

    monkeypatch.setattr(
        "scottish_progressive.selfplay_training.analyze", fake_incomplete_analyze
    )
    failed = evaluate_human_refutation_gate(baseline_profile())
    assert failed["passed"] is False
    assert all(not anchor["passed"] for anchor in failed["anchors"])
    assert all(
        not anchor["completed_required_search"] for anchor in failed["anchors"]
    )

    def fake_empty_analyze(state, limits, profile):
        return SimpleNamespace(
            best_series=None,
            principal_variation=(),
            score=0,
            requested_depth=limits.depth_series,
            completed_depth=limits.depth_series,
            exact_width=True,
            timed_out=False,
            work_limit_reached=False,
            stats=SimpleNamespace(work_positions=1, nodes=1),
            elapsed_seconds=0.01,
        )

    monkeypatch.setattr(
        "scottish_progressive.selfplay_training.analyze", fake_empty_analyze
    )
    empty = evaluate_human_refutation_gate(baseline_profile())
    assert empty["passed"] is False
    assert all(not anchor["avoided_known_blunder"] for anchor in empty["anchors"])


def test_train_selfplay_cli_writes_reproducible_candidate_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "league.sqlite3"
    output = tmp_path / "training-output"
    _write_database(database, [_mate_game("game-cli", "custom-cli")])

    assert (
        main(
            [
                "train-selfplay",
                str(output),
                str(database),
                "--holdout-percent",
                "0",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["completed_games"] == 1
    assert payload["excluded_games"] == 0
    assert payload["claim_scope"].startswith("self-play value-fit proxy only")
    assert (output / "selfplay-corpus.json").is_file()
    assert (output / "selfplay-tuning-report.json").is_file()
    candidate = json.loads((output / "candidate-profile.json").read_text())
    assert candidate["profile_id"] == payload["candidate_profile_id"]


def _features(material: int) -> CachedFeatures:
    return CachedFeatures(
        material=material,
        king_space=0,
        series_reach=0,
        promotion_corridors=0,
        immediate_vulnerability=0,
        useful_mobility=0,
        boundary_check=0,
        white_check_distance=None,
        black_check_distance=None,
        reach_complete=True,
        white_king_ring_attack_multiplicity=0,
        black_king_ring_attack_multiplicity=0,
        white_promotable_next_series=0,
        black_promotable_next_series=0,
        white_king_edge_distance=0,
        black_king_edge_distance=0,
    )


def _sample(index: int, material: int, target: float, split: str) -> SelfPlaySample:
    result = "1-0" if target == 1.0 else "0-1" if target == 0.0 else "1/2-1/2"
    return SelfPlaySample(
        position_hash=f"hash-{index}",
        pfen=f"pfen-{index}",
        run_id="run",
        game_key=f"game-{index}",
        opening_case_id=f"case-{index}",
        line_family=f"family-{index}",
        split_component=f"component-{index}",
        split=split,
        series_number=1,
        mover="white",
        profile_id="profile",
        chosen_series="a2a3",
        result=result,
        target_white_score=target,
        sample_weight=1.0,
        features=_features(material),
    )


def test_texel_coordinate_tuner_uses_train_only_and_improves_holdout() -> None:
    samples = (
        _sample(1, 100, 1.0, "train"),
        _sample(2, -100, 0.0, "train"),
        _sample(3, 0, 0.5, "train"),
        _sample(4, 100, 1.0, "holdout"),
        _sample(5, -100, 0.0, "holdout"),
        _sample(6, 0, 0.5, "holdout"),
    )
    corpus = SelfPlayCorpus(
        seed=1,
        holdout_percent=50,
        database_evidence=({"filename": "synthetic"},),
        completed_games=6,
        excluded_games=0,
        samples=samples,
    )

    candidate, report = tune_selfplay_profile(
        corpus,
        baseline_profile(),
        scales=(400,),
        step_schedule=(100, 25),
        regularization=0.0,
    )

    assert candidate.weights.material > 100
    assert candidate.profile_id != baseline_profile().profile_id
    assert report["candidate_train_loss"] < report["baseline_train_loss"]
    assert report["candidate_holdout_loss"] < report["baseline_holdout_loss"]
    assert "fixed-suite match" in candidate.notes
