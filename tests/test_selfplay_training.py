from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from scottish_progressive.fast_training import CachedFeatures
from scottish_progressive.cli import main
from scottish_progressive.league import OPENING_SUITE_VERSION, OpeningCase
from scottish_progressive.model import ProgressiveState
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.selfplay_training import (
    SelfPlayCorpus,
    SelfPlaySample,
    build_selfplay_corpus,
    tune_selfplay_profile,
)


MATE_FEN = "7k/8/5KQ1/8/8/8/8/8 w - - 0 1"


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
    assert report["train_feature_buckets"] == 3
    assert report["loss_surface_collapsed_exactly"] is True
    assert "fixed-suite match" in candidate.notes


def test_texel_tuner_collapses_equal_feature_vectors_with_exact_weighted_targets() -> None:
    first = replace(_sample(20, 75, 1.0, "train"), sample_weight=0.25)
    second = replace(_sample(21, 75, 0.0, "train"), sample_weight=0.75)
    holdout = _sample(22, 75, 0.25, "holdout")
    corpus = SelfPlayCorpus(
        seed=2,
        holdout_percent=50,
        database_evidence=({"filename": "collapse-fixture"},),
        completed_games=3,
        excluded_games=0,
        samples=(first, second, holdout),
    )

    _, report = tune_selfplay_profile(
        corpus,
        baseline_profile(),
        scales=(400,),
        step_schedule=(25,),
        regularization=0.0,
    )

    assert report["train_samples"] == 2
    assert report["train_feature_buckets"] == 1
    assert report["holdout_feature_buckets"] == 1
