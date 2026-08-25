from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import json
import sqlite3

import pytest

from scottish_progressive.cli import main
from scottish_progressive.league import (
    OPENING_SUITE,
    OPENING_SUITE_VERSION,
    GameRecord,
    GameJob,
    LeagueConfig,
    LeagueStore,
    OpeningCase,
    _preliminary_jobs,
    _play_game,
)
from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT, ProgressiveState
from scottish_progressive.neural_evaluator import (
    FEATURE_COUNT,
    FixedPointNetwork,
    NeuralBlend,
)
from scottish_progressive.profiles import (
    baseline_profile,
    create_population,
    mutate_profile,
    save_profile,
)
from scottish_progressive.resources import detect_resource_budget
from scottish_progressive.rules import play_series
from scottish_progressive.strength import (
    SEEDED_OPENING_SUITE_FORMAT,
    STRENGTH_REPORT_FORMAT,
    StrengthMatchConfig,
    StrengthParticipant,
    _build_jobs,
    _worker_failure,
    build_seeded_opening_suite,
    resolve_match_profile,
    run_strength_match,
    verify_seeded_opening_suite,
    write_strength_report,
)


def _profiles():
    reference = baseline_profile()
    candidate = mutate_profile(reference, seed=404, name="candidate")
    return candidate, reference


def _neural_participant() -> StrengthParticipant:
    profile = baseline_profile()
    network = FixedPointNetwork(
        source_fingerprint=ENGINE_SOURCE_FINGERPRINT,
        base_profile_id=profile.profile_id,
        teacher_fingerprint="teacher-fixture",
        corpus_fingerprint="corpus-fixture",
        trainer_fingerprint="trainer-fixture",
        hidden_size=1,
        input_weights=(0,) * FEATURE_COUNT,
        hidden_bias=(0,),
        output_weights=(0,),
        output_bias=0,
        output_denominator=256,
        recommended_blend_percent=25,
    )
    return StrengthParticipant(
        profile,
        NeuralBlend.for_profile(network, profile, blend_percent=25),
    )


def test_strength_jobs_are_deterministic_unique_and_color_swapped() -> None:
    candidate, reference = _profiles()
    config = StrengthMatchConfig(
        pairs=5,
        seed=91,
        search_depth=3,
        max_series_per_node=17,
        max_generation_positions=12_345,
        max_game_work_positions=123_456,
        emergency_max_series=23,
    )
    first = _build_jobs(candidate, reference, config)
    second = _build_jobs(candidate, reference, config)
    assert [job.job_key for job in first] == [job.job_key for job in second]
    case_ids = [job.opening.case_id for job in first[::2]]
    assert len(case_ids) == len(set(case_ids)) == 5
    for pair_index in range(0, len(first), 2):
        game_a, game_b = first[pair_index : pair_index + 2]
        assert game_a.opening.as_dict() == game_b.opening.as_dict()
        assert game_a.seed == game_b.seed
        assert game_a.white_profile.profile_id == game_b.black_profile.profile_id
        assert game_a.black_profile.profile_id == game_b.white_profile.profile_id
        for game in (game_a, game_b):
            assert game.search_depth == 3
            assert game.max_series_per_node == 17
            assert game.max_generation_positions == 12_345
            assert game.max_game_work_positions == 123_456
            assert game.emergency_max_series == 23


def test_neural_participant_round_trips_and_plays_as_effective_match_identity() -> None:
    candidate = _neural_participant()
    reference = StrengthParticipant(baseline_profile())
    restored = StrengthParticipant.from_dict(candidate.as_dict())
    assert restored == candidate
    assert restored.participant_id != reference.participant_id

    jobs = _build_jobs(candidate, reference, StrengthMatchConfig.smoke(seed=71))
    assert jobs[0].white_evaluation_overlay == candidate.evaluation_overlay
    assert jobs[1].black_evaluation_overlay == candidate.evaluation_overlay

    mate = OpeningCase(
        case_id="neural-immediate-mate",
        fen="7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
        series_number=1,
        source="neural match identity regression",
    )
    record = _play_game(
        GameJob(
            job_key="neural-game",
            run_id="neural-run",
            generation=0,
            stage="neural-screen",
            opening_index=0,
            opening=mate,
            seed=71,
            white_profile=candidate.profile,
            black_profile=reference.profile,
            search_depth=1,
            max_series_per_node=32,
            max_generation_positions=250_000,
            max_game_work_positions=1_000_000,
            emergency_max_series=18,
            white_evaluation_overlay=candidate.evaluation_overlay,
        )
    )
    assert record.result == "1-0"
    assert record.white_profile_id == candidate.participant_id
    assert record.black_profile_id == reference.participant_id
    assert record.decisive_profile_id == candidate.participant_id
    assert record.trace[0]["profile_id"] == candidate.participant_id
    assert record.trace[0]["evaluation_overlay_id"] == candidate.participant_id


def test_strength_config_rejects_duplicate_suite_cases_and_identical_profiles() -> None:
    duplicate = (OPENING_SUITE[0].case_id, OPENING_SUITE[0].case_id)
    with pytest.raises(ValueError, match="duplicates"):
        StrengthMatchConfig(pairs=1, opening_case_ids=duplicate)
    with pytest.raises(ValueError, match="different engine profiles"):
        _build_jobs(baseline_profile(), baseline_profile(), StrengthMatchConfig.smoke())


def test_seeded_opening_suite_is_deterministic_unique_and_replay_verified() -> None:
    first = build_seeded_opening_suite(
        seed=818,
        count=8,
        min_series=3,
        max_series=5,
        max_frontier_states=12,
    )
    repeated = build_seeded_opening_suite(
        seed=818,
        count=8,
        min_series=3,
        max_series=5,
        max_frontier_states=12,
    )
    changed = build_seeded_opening_suite(
        seed=819,
        count=8,
        min_series=3,
        max_series=5,
        max_frontier_states=12,
    )

    assert first.as_dict() == repeated.as_dict()
    assert first.version.startswith(f"{SEEDED_OPENING_SUITE_FORMAT}-")
    assert changed.version != first.version
    assert [case.state().position_hash for case in changed.cases] != [
        case.state().position_hash for case in first.cases
    ]
    assert len({case.case_id for case in first.cases}) == 8
    assert len({case.state().position_hash for case in first.cases}) == 8
    assert {case.series_number for case in first.cases} == {3, 4, 5}
    assert all("history=" in case.source for case in first.cases)
    verify_seeded_opening_suite(first)

    corrupted = replace(first, version=first.version + "-tampered")
    with pytest.raises(ValueError, match="version does not match"):
        verify_seeded_opening_suite(corrupted)


def test_custom_seeded_suite_builds_fixed_color_swapped_jobs() -> None:
    candidate, reference = _profiles()
    suite = build_seeded_opening_suite(seed=33, count=3, max_frontier_states=8)
    config = StrengthMatchConfig(
        pairs=3,
        seed=44,
        search_depth=1,
        max_series_per_node=2,
        max_generation_positions=5_000,
        max_game_work_positions=10_000,
        opening_suite_version=suite.version,
        opening_case_ids=tuple(case.case_id for case in suite.cases),
    )
    with pytest.raises(ValueError, match="verified SeededOpeningSuite"):
        _build_jobs(candidate, reference, config)
    with pytest.raises(ValueError, match="verified SeededOpeningSuite"):
        _build_jobs(candidate, reference, config, suite.cases)

    jobs = _build_jobs(candidate, reference, config, suite)
    assert len(jobs) == 6
    assert {job.opening_suite_version for job in jobs} == {suite.version}
    assert len({job.opening.case_id for job in jobs[::2]}) == 3
    for first, second in zip(jobs[::2], jobs[1::2], strict=True):
        assert first.opening.as_dict() == second.opening.as_dict()
        assert first.seed == second.seed
        assert first.white_profile.profile_id == second.black_profile.profile_id
        assert first.black_profile.profile_id == second.white_profile.profile_id


def test_custom_suite_version_survives_match_records_and_worker_failure(
    monkeypatch,
) -> None:
    candidate, reference = _profiles()
    suite = build_seeded_opening_suite(seed=73, count=1, max_frontier_states=8)
    config = StrengthMatchConfig(
        pairs=1,
        seed=74,
        search_depth=1,
        max_series_per_node=2,
        max_generation_positions=5_000,
        max_game_work_positions=10_000,
        opening_suite_version=suite.version,
        opening_case_ids=(suite.cases[0].case_id,),
    )

    def fake_play(job):
        state = job.opening.state()
        return GameRecord(
            job.job_key,
            job.run_id,
            job.generation,
            job.stage,
            job.opening_index,
            job.opening.case_id,
            job.opening_suite_version,
            job.seed,
            job.white_profile.profile_id,
            job.black_profile.profile_id,
            "1/2-1/2",
            "stalemate",
            None,
            None,
            state.pfen,
            state.pfen,
            0,
            (),
        )

    monkeypatch.setattr("scottish_progressive.strength._play_game", fake_play)
    report = run_strength_match(
        candidate,
        reference,
        config=config,
        opening_cases=suite,
        requested_workers=1,
        memory_per_worker_mb=128,
        reserve_memory_mb=128,
    )
    assert report["config"]["opening_suite_version"] == suite.version
    assert report["opening_suite"] == suite.as_dict()
    assert report["summary"]["completed_pairs"] == 1
    assert {game["opening_suite_version"] for game in report["games"]} == {
        suite.version
    }

    jobs = _build_jobs(candidate, reference, config, suite)
    failure = _worker_failure(jobs[0], RuntimeError("fixture"))
    assert failure.opening_suite_version == suite.version


def test_custom_suite_rejects_reused_version_after_opening_tamper() -> None:
    candidate, reference = _profiles()
    suite = build_seeded_opening_suite(seed=991, count=2, max_frontier_states=8)
    config = StrengthMatchConfig(
        pairs=2,
        opening_suite_version=suite.version,
        opening_case_ids=tuple(case.case_id for case in suite.cases),
    )

    altered_case = replace(suite.cases[0], source=suite.cases[0].source + "; altered")
    tampered = replace(suite, cases=(altered_case, *suite.cases[1:]))
    with pytest.raises(ValueError, match="version does not match its content"):
        _build_jobs(candidate, reference, config, tampered)

    replacement = build_seeded_opening_suite(
        seed=992,
        count=1,
        min_series=suite.cases[0].series_number,
        max_series=suite.cases[0].series_number,
        max_frontier_states=8,
    ).cases[0]
    altered_pfen = replace(
        suite.cases[0],
        fen=replacement.fen,
        quiet_series=replacement.quiet_series,
        ep_targets=replacement.ep_targets,
    )
    pfen_tampered = replace(suite, cases=(altered_pfen, *suite.cases[1:]))
    with pytest.raises(ValueError, match="does not replay to its boundary"):
        _build_jobs(candidate, reference, config, pfen_tampered)


def test_series_twelve_advances_to_thirteen_with_thirteen_moves_available() -> None:
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K5n1 b - - 0 1", 12
    )
    moves = ("g1e2", "e2g3", "g3e2", "e2g1") * 3
    result = play_series(state, moves)
    assert len(result.moves) == 12
    assert result.outcome is None
    assert result.final_state.series_number == 13
    assert result.final_state.moves_available == 13


def test_whole_game_work_cutoff_is_unattributed_incomplete_with_attempt_trace(
    monkeypatch,
) -> None:
    candidate, reference = _profiles()
    opening = OPENING_SUITE[0]
    state = opening.state()
    selected = play_series(state, ("e2e4",))
    job = GameJob(
        "work-cutoff",
        "fixture",
        0,
        "strength-fixed-suite",
        0,
        opening,
        1,
        candidate,
        reference,
        1,
        4,
        5_000,
        1_000,
        None,
        "unit-work-suite-v1",
    )
    partial = SimpleNamespace(
        timed_out=False,
        work_limit_reached=True,
        best_series=selected,
        proof=None,
        adjudication_status=None,
        score=12,
        completed_depth=0,
        exact_width=False,
        stats=SimpleNamespace(
            nodes=2,
            generation_positions=1_000,
            series_generation_positions=600,
            evaluation_reach_positions=300,
            quiet_adjudication_positions=100,
        ),
    )
    monkeypatch.setattr("scottish_progressive.league.analyze", lambda *a, **k: partial)
    record = _play_game(job)
    assert record.result == "*"
    assert record.terminal_reason == "technical-game-work-budget-exhausted"
    assert record.decisive_profile_id is None
    assert record.engine_failure_profile_id is None
    assert record.series_played == 0
    assert record.final_pfen == state.pfen
    assert record.trace[-1]["played"] is False
    assert record.trace[-1]["search_work_limit"] == 1_000
    assert record.trace[-1]["game_work_positions"] == 1_000
    assert record.trace[-1]["reduced_for_game_budget"] is True
    assert record.opening_suite_version == "unit-work-suite-v1"


def test_per_search_work_limit_plays_legal_best_so_far_mate(monkeypatch) -> None:
    candidate, reference = _profiles()
    opening = next(
        case for case in OPENING_SUITE if case.case_id == "published-bishop-pressure"
    )
    selected = play_series(
        opening.state(), ("c7c6", "d8b6", "f6e4", "b6f2")
    )
    job = GameJob(
        "best-so-far",
        "fixture",
        0,
        "strength-fixed-suite",
        0,
        opening,
        1,
        candidate,
        reference,
        1,
        64,
        1_000,
        None,
        None,
        "unit-mate-suite-v1",
    )
    partial = SimpleNamespace(
        timed_out=False,
        work_limit_reached=True,
        best_series=selected,
        proof="black",
        adjudication_status=None,
        score=-999_999,
        completed_depth=0,
        exact_width=False,
        stats=SimpleNamespace(
            nodes=0,
            generation_positions=1_000,
            series_generation_positions=700,
            evaluation_reach_positions=300,
            quiet_adjudication_positions=0,
        ),
    )
    monkeypatch.setattr("scottish_progressive.league.analyze", lambda *a, **k: partial)
    record = _play_game(job)
    assert record.result == "0-1"
    assert record.terminal_reason == "checkmate"
    assert record.engine_failure_profile_id is None
    assert record.trace[-1]["played"] is True
    assert record.trace[-1]["work_limit_reached"] is True
    assert record.opening_suite_version == "unit-mate-suite-v1"


def test_report_counts_game_pair_failures_and_preserves_traces(monkeypatch) -> None:
    candidate, reference = _profiles()
    config = StrengthMatchConfig(
        pairs=3,
        seed=55,
        search_depth=1,
        max_series_per_node=2,
        max_generation_positions=5_000,
        max_game_work_positions=5_000,
        emergency_max_series=18,
    )

    def fake_play(job):
        pair_index = job.opening_index // 2
        candidate_white = job.white_profile.profile_id == candidate.profile_id
        failure_id = None
        terminal_reason = "checkmate"
        if pair_index == 0:
            result = "1-0" if candidate_white else "0-1"
            decisive_id = candidate.profile_id
        elif pair_index == 1:
            result = "1/2-1/2"
            terminal_reason = "stalemate"
            decisive_id = None
        elif candidate_white:
            result = "*"
            terminal_reason = "engine-work-limit"
            failure_id = reference.profile_id
            decisive_id = None
        else:
            result = "*"
            terminal_reason = "engine-no-move"
            failure_id = candidate.profile_id
            decisive_id = None
        state = job.opening.state()
        return GameRecord(
            job.job_key,
            job.run_id,
            job.generation,
            job.stage,
            job.opening_index,
            job.opening.case_id,
            OPENING_SUITE_VERSION,
            job.seed,
            job.white_profile.profile_id,
            job.black_profile.profile_id,
            result,
            terminal_reason,
            decisive_id,
            failure_id,
            state.pfen,
            state.pfen,
            1,
            ({"series_number": state.series_number, "series": "fixture"},),
        )

    monkeypatch.setattr("scottish_progressive.strength._play_game", fake_play)
    progress: list[str] = []
    report = run_strength_match(
        candidate,
        reference,
        config=config,
        requested_workers=1,
        memory_per_worker_mb=128,
        reserve_memory_mb=128,
        progress=progress.append,
    )
    summary = report["summary"]
    assert report["format"] == STRENGTH_REPORT_FORMAT
    assert summary["candidate_game_wdl"] == {"wins": 2, "draws": 2, "losses": 0}
    assert summary["candidate_pair_wdl"] == {"wins": 1, "draws": 1, "losses": 0}
    assert summary["incomplete_games"] == 2
    assert summary["incomplete_pairs"] == 1
    assert summary["candidate_game_score_rate"] == pytest.approx(3 / 4)
    assert summary["candidate_pair_score_rate"] == pytest.approx(3 / 4)
    assert summary["technical_failures"] == {
        "total_profile_failures": 2,
        "candidate": 1,
        "reference": 1,
        "unattributed_worker_failures": 0,
        "unattributed_match_limit_failures": 0,
        "by_reason": {"engine-no-move": 1, "engine-work-limit": 1},
    }
    estimate = summary["fixed_suite_performance_difference"]
    assert estimate["value"] == 191
    assert "not a calibrated Elo" in estimate["warning"]
    assert "not comparable to orthodox Stockfish Elo" in estimate["warning"]
    assert "does not establish Stockfish-level strength" in report["claim_scope"][
        "stockfish_comparison"
    ]
    assert len({item["case_id"] for item in report["selected_openings"]}) == 3
    assert report["games"][0]["trace"][0]["series"] == "fixture"
    assert report["resources"]["workers"] == 1
    assert report["resources"]["cpu_worker_limit_enforced"] is True
    assert report["resources"]["ram_limit_enforced"] is False
    assert progress[-1] == "strength match: finished 6/6 games"


def test_strength_report_writes_json_and_resolves_baseline_or_envelope(tmp_path) -> None:
    candidate, reference = _profiles()
    profile_path = tmp_path / "candidate.json"
    save_profile(candidate, profile_path, provenance={"fixture": True})
    assert resolve_match_profile("baseline").profile_id == reference.profile_id
    assert resolve_match_profile(profile_path).profile_id == candidate.profile_id

    report = {
        "format": STRENGTH_REPORT_FORMAT,
        "summary": {"candidate_game_wdl": {"wins": 1, "draws": 0, "losses": 0}},
    }
    output = write_strength_report(report, tmp_path / "nested" / "report.json")
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_cli_strength_match_threads_limits_and_writes_report(
    tmp_path, monkeypatch, capsys
) -> None:
    candidate, _ = _profiles()
    candidate_path = tmp_path / "candidate.json"
    output = tmp_path / "strength.json"
    save_profile(candidate, candidate_path)
    captured = {}

    def fake_run(
        candidate_profile,
        reference_profile,
        *,
        config,
        opening_cases=None,
        requested_workers,
        memory_per_worker_mb,
        reserve_memory_mb,
        progress,
    ):
        captured["candidate"] = candidate_profile
        captured["reference"] = reference_profile
        captured["config"] = config
        captured["opening_cases"] = opening_cases
        captured["requested_workers"] = requested_workers
        progress("strength match: finished 2/2 games")
        return {
            "format": STRENGTH_REPORT_FORMAT,
            "summary": {
                "candidate_game_wdl": {"wins": 1, "draws": 1, "losses": 0},
                "candidate_pair_wdl": {"wins": 1, "draws": 0, "losses": 0},
                "incomplete_games": 0,
                "incomplete_pairs": 0,
                "technical_failures": {
                    "candidate": 0,
                    "reference": 0,
                "unattributed_worker_failures": 0,
                },
                "fixed_suite_performance_difference": {
                    "value": 191,
                    "status": "finite",
                },
            },
            "claim_scope": {"stockfish_comparison": "No Stockfish claim."},
        }

    monkeypatch.setattr(
        "scottish_progressive.strength.run_strength_match", fake_run
    )
    assert main(
        [
            "strength-match",
            str(candidate_path),
            "baseline",
            "--pairs",
            "1",
            "--depth",
            "3",
            "--branch-cap",
            "19",
            "--max-generation-positions",
            "6789",
            "--max-game-work-positions",
            "45678",
            "--emergency-max-series",
            "21",
            "--workers",
            "99",
            "--output",
            str(output),
        ]
    ) == 0
    config = captured["config"]
    assert config.pairs == 1
    assert config.search_depth == 3
    assert config.max_series_per_node == 19
    assert config.max_generation_positions == 6789
    assert config.max_game_work_positions == 45678
    assert config.emergency_max_series == 21
    assert captured["requested_workers"] == 99
    assert json.loads(output.read_text())["format"] == STRENGTH_REPORT_FORMAT
    printed = capsys.readouterr().out
    assert "Games W/D/L: 1/1/0" in printed
    assert "No Stockfish claim." in printed

    seeded_output = tmp_path / "seeded-report.json"
    assert main(
        [
            "strength-match",
            str(candidate_path),
            "baseline",
            "--pairs",
            "1",
            "--seed",
            "20260822",
            "--seeded-openings",
            "2",
            "--seeded-min-series",
            "3",
            "--seeded-max-series",
            "4",
            "--seeded-frontier-cap",
            "8",
            "--output",
            str(seeded_output),
        ]
    ) == 0
    seeded_suite = captured["opening_cases"]
    assert seeded_suite is not None
    assert len(seeded_suite.cases) == 2
    assert captured["config"].opening_suite_version == seeded_suite.version


def test_population_order_and_preliminary_job_keys_survive_reopen(tmp_path) -> None:
    database = tmp_path / "order.sqlite3"
    champion = baseline_profile()
    config = LeagueConfig(population_size=10, generations=1, seed=818)
    resources = detect_resource_budget(
        1, memory_per_worker_mb=128, reserve_memory_mb=128
    )
    original = create_population(champion, size=10, seed=config.seed + 1)
    with LeagueStore(database) as store:
        run_id = store.create_run(config, resources, champion)
        store.save_population(run_id, 1, original, champion.profile_id)
        before = _preliminary_jobs(run_id, 1, original, config)

    # Simulate a v2 database: slots were absent, but insertion rowid survived.
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE run_population SET population_slot=NULL")
        connection.commit()

    with LeagueStore(database) as reopened:
        restored = reopened.population(run_id, 1)
        after = _preliminary_jobs(run_id, 1, restored, config)
        slots = reopened.connection.execute(
            """
            SELECT population_slot FROM run_population
            WHERE run_id=? AND generation=? ORDER BY population_slot
            """,
            (run_id, 1),
        ).fetchall()
    assert [profile.profile_id for profile in restored] == [
        profile.profile_id for profile in original
    ]
    assert [job.job_key for job in after] == [job.job_key for job in before]
    assert [row[0] for row in slots] == list(range(10))


def test_continue_latest_skips_incompatible_run_but_explicit_resume_still_passes_id(
    tmp_path, monkeypatch, capsys
) -> None:
    database = tmp_path / "incompatible.sqlite3"
    champion = baseline_profile()
    config = LeagueConfig.smoke(seed=606)
    resources = detect_resource_budget(
        1, memory_per_worker_mb=128, reserve_memory_mb=128
    )
    with LeagueStore(database) as store:
        stale_run_id = store.create_run(config, resources, champion)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET source_fingerprint='old-source' WHERE run_id=?",
            (stale_run_id,),
        )
        connection.commit()

    calls = []

    def fake_run(database_path, **kwargs):
        calls.append(kwargs)
        return {"status": "complete", "run_id": "new-run"}

    monkeypatch.setattr("scottish_progressive.league.run_league", fake_run)
    assert main(
        [
            "league",
            "run",
            str(database),
            "--continue-latest",
            "--smoke",
            "--champion-output",
            str(tmp_path / "new.json"),
        ]
    ) == 0
    assert calls[-1]["resume_run_id"] is None
    assert calls[-1]["config"] is not None
    assert "starting a new run" in capsys.readouterr().out

    assert main(
        [
            "league",
            "run",
            str(database),
            "--resume",
            stale_run_id,
            "--champion-output",
            str(tmp_path / "explicit.json"),
        ]
    ) == 0
    assert calls[-1]["resume_run_id"] == stale_run_id
    assert calls[-1]["config"] is None
