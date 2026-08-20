from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

import scottish_progressive.league as league_module
import scottish_progressive.resources as resource_module
from scottish_progressive.cli import main
from scottish_progressive.league import (
    OPENING_SUITE,
    OPENING_SUITE_VERSION,
    PROMOTION_METHOD,
    GameRecord,
    LeagueConfig,
    LeagueStore,
    _fitness,
    _next_population,
    _pair_evidence,
    _paired_jobs,
    league_status,
    promotion_decision,
    run_league,
    runtime_provenance,
)
from scottish_progressive.profiles import (
    baseline_profile,
    create_population,
    load_profile,
    mutate_profile,
    save_profile,
)
from scottish_progressive.model import ProgressiveState
from scottish_progressive.rules import play_series


def _profiles():
    population = create_population(size=2, seed=404)
    return population[0], population[1]


def _pair_rows(
    first_id: str,
    second_id: str,
    *,
    case_id: str,
    pair_index: int,
    results: tuple[str, str],
    reasons: tuple[str, str] = ("checkmate", "checkmate"),
    failure_ids: tuple[str | None, str | None] = (None, None),
) -> list[dict[str, object]]:
    seed = 1000 + pair_index
    return [
        {
            "stage": "promotion-g1",
            "opening_index": pair_index * 2,
            "opening_case_id": case_id,
            "seed": seed,
            "white_profile_id": first_id,
            "black_profile_id": second_id,
            "result": results[0],
            "terminal_reason": reasons[0],
            "engine_failure_profile_id": failure_ids[0],
        },
        {
            "stage": "promotion-g1",
            "opening_index": pair_index * 2 + 1,
            "opening_case_id": case_id,
            "seed": seed,
            "white_profile_id": second_id,
            "black_profile_id": first_id,
            "result": results[1],
            "terminal_reason": reasons[1],
            "engine_failure_profile_id": failure_ids[1],
        },
    ]


def test_opening_suite_has_thirty_unique_legal_recorded_boundaries() -> None:
    assert OPENING_SUITE_VERSION == "spc-league-boundaries-v4"
    assert len(OPENING_SUITE) == 30
    assert len({case.case_id for case in OPENING_SUITE}) == 30
    assert len({case.state().position_hash for case in OPENING_SUITE}) == 30
    assert {case.case_id for case in OPENING_SUITE} >= {
        "initial",
        "after-1-e4",
        "after-1-d4",
        "published-bishop-pressure",
    }
    for case in OPENING_SUITE:
        payload = case.as_dict()
        assert payload["pfen"] == case.state().pfen
        assert payload["position_hash"] == case.state().position_hash


def test_published_central_pressure_metadata_matches_canonical_replay() -> None:
    state = ProgressiveState.initial()
    state = play_series(state, ("d2d4",)).final_state
    state = play_series(state, ("d7d5", "g8f6")).final_state
    state = play_series(state, ("b1c3", "g1f3", "c1g5")).final_state
    case = next(
        item for item in OPENING_SUITE if item.case_id == "published-central-pressure"
    )
    assert state.pfen == case.state().pfen
    assert state.quiet_series == case.quiet_series == 1


def test_seeded_pair_order_is_reproducible_diverse_and_never_replaced() -> None:
    first, second = _profiles()

    def jobs(config: LeagueConfig, count: int, offset: int = 0):
        return _paired_jobs(
            run_id="fixture",
            generation=1,
            stage="promotion-g1",
            first=first,
            second=second,
            game_count=count,
            pair_offset=offset,
            config=config,
        )

    config = LeagueConfig(seed=11)
    base = jobs(config, 20)
    replacements = jobs(config, 40, 10)
    all_pairs = base[::2] + replacements[::2]
    case_ids = [job.opening.case_id for job in all_pairs]
    hashes = [job.opening.state().position_hash for job in all_pairs]
    assert len(case_ids) == len(set(case_ids)) == 30
    assert len(hashes) == len(set(hashes)) == 30
    assert case_ids == [
        job.opening.case_id
        for job in (jobs(config, 20)[::2] + jobs(config, 40, 10)[::2])
    ]

    different = jobs(LeagueConfig(seed=12), 20)[::2]
    assert [job.opening.case_id for job in base[::2]] != [
        job.opening.case_id for job in different
    ]
    for paired in (base, replacements):
        for index in range(0, len(paired), 2):
            white_first, black_first = paired[index : index + 2]
            assert white_first.opening.as_dict() == black_first.opening.as_dict()
            assert white_first.seed == black_first.seed
            assert white_first.white_profile.profile_id == black_first.black_profile.profile_id
            assert white_first.black_profile.profile_id == black_first.white_profile.profile_id

    with pytest.raises(ValueError, match="suite exhausted"):
        jobs(config, 42, 10)


def test_serious_defaults_share_fixed_limits_and_run_two_generations() -> None:
    config = LeagueConfig()
    payload = config.as_dict()
    assert config.population_size == 10
    assert config.generations == 2
    assert config.promotion_games == 20
    assert config.max_replacement_games == 40
    assert config.max_game_work_positions is None
    assert config.emergency_max_series is None
    assert payload["deterministic_match_limits"] == {
        "depth_series": 2,
        "branch_cap_complete_series_per_node": 32,
        "max_work_positions_per_search": 250000,
        "max_game_work_positions": None,
        "game_work_definition": (
            "deterministic logical positions across complete-series generation, "
            "evaluation reach, and quiet adjudication over the whole game"
        ),
        "emergency_max_series": None,
        "series_number_limit": "unbounded",
        "time_limit_seconds": None,
        "fresh_searcher_each_series": True,
        "collect_all_root_scores": False,
        "root_score_mode": "best-only-play-optimized",
        "same_for_every_profile": True,
    }
    assert LeagueConfig.smoke().generations == 1


def test_fixed_suite_promotion_requires_nine_pair_wins_and_no_pair_loss() -> None:
    convincing = promotion_decision(
        wins=9,
        draws=1,
        losses=0,
        technical_failures=0,
        gate_passed=True,
    )
    assert convincing.promoted
    assert convincing.games == 20
    assert convincing.pairs == 10
    assert convincing.lower_confidence_bound is None
    assert PROMOTION_METHOD in convincing.reason
    assert "fixed-suite evidence only" in convincing.reason

    for decision in (
        promotion_decision(
            wins=8, draws=2, losses=0, technical_failures=0, gate_passed=True
        ),
        promotion_decision(
            wins=9, draws=0, losses=1, technical_failures=0, gate_passed=True
        ),
        promotion_decision(
            wins=10, draws=0, losses=0, technical_failures=1, gate_passed=True
        ),
        promotion_decision(
            wins=8, draws=0, losses=0, technical_failures=0, gate_passed=True
        ),
    ):
        assert not decision.promoted
        assert "95%" not in decision.reason
        assert "Wilson" not in decision.reason


def test_pair_evidence_groups_swapped_games_and_tracks_candidate_only() -> None:
    candidate, champion = _profiles()
    rows: list[dict[str, object]] = []
    for index in range(9):
        rows += _pair_rows(
            candidate.profile_id,
            champion.profile_id,
            case_id=OPENING_SUITE[index].case_id,
            pair_index=index,
            results=("1-0", "0-1"),
        )
    rows += _pair_rows(
        candidate.profile_id,
        champion.profile_id,
        case_id=OPENING_SUITE[9].case_id,
        pair_index=9,
        results=("1/2-1/2", "1/2-1/2"),
    )
    evidence = _pair_evidence(rows, candidate.profile_id, maximum_pairs=10)
    assert (evidence.wins, evidence.draws, evidence.losses) == (9, 1, 0)
    assert evidence.completed_pairs == 10
    assert evidence.candidate_failures == 0

    failure = _pair_rows(
        candidate.profile_id,
        champion.profile_id,
        case_id=OPENING_SUITE[10].case_id,
        pair_index=10,
        results=("1-0", "*"),
        reasons=("checkmate", "engine-work-limit"),
        failure_ids=(None, champion.profile_id),
    )
    candidate_evidence = _pair_evidence(failure, candidate.profile_id)
    assert candidate_evidence.completed_pairs == 0
    assert candidate_evidence.wins == 0
    assert candidate_evidence.candidate_failures == 0


def test_pair_evidence_rejects_case_reuse_within_one_promotion_match() -> None:
    candidate, champion = _profiles()
    case_id = OPENING_SUITE[0].case_id
    rows = _pair_rows(
        candidate.profile_id,
        champion.profile_id,
        case_id=case_id,
        pair_index=0,
        results=("1-0", "0-1"),
    )
    rows += _pair_rows(
        candidate.profile_id,
        champion.profile_id,
        case_id=case_id,
        pair_index=1,
        results=("1-0", "0-1"),
    )

    with pytest.raises(ValueError, match="duplicate opening case"):
        _pair_evidence(rows, candidate.profile_id)


def test_profile_engine_failure_is_incomplete_not_a_win(monkeypatch) -> None:
    first, second = _profiles()
    job = _paired_jobs(
        run_id="fixture",
        generation=1,
        stage="promotion-g1",
        first=first,
        second=second,
        game_count=2,
        config=LeagueConfig(),
    )[0]

    def explode(*args, **kwargs):
        raise RuntimeError("fixture")

    monkeypatch.setattr(league_module, "analyze", explode)
    record = league_module._play_game(job)
    failing = first if job.opening.state().board.turn else second
    assert record.result == "*"
    assert record.engine_failure_profile_id == failing.profile_id
    assert record.decisive_profile_id is None
    assert record.terminal_reason == "engine-exception"


@pytest.mark.parametrize(
    ("timed_out", "work_limited", "reason"),
    (
        (True, False, "engine-timeout"),
        (False, True, "engine-work-limit"),
        (False, False, "engine-no-move"),
    ),
)
def test_timeout_work_limit_and_no_move_without_a_legal_best_are_incomplete(
    monkeypatch, timed_out: bool, work_limited: bool, reason: str
) -> None:
    first, second = _profiles()
    job = _paired_jobs(
        run_id="fixture",
        generation=1,
        stage="promotion-g1",
        first=first,
        second=second,
        game_count=2,
        config=LeagueConfig(),
    )[0]
    incomplete = SimpleNamespace(
        timed_out=timed_out,
        work_limit_reached=work_limited,
        best_series=None,
        proof=None,
        adjudication_status=None,
    )
    monkeypatch.setattr(league_module, "analyze", lambda *args, **kwargs: incomplete)
    record = league_module._play_game(job)
    assert record.result == "*"
    assert record.decisive_profile_id is None
    assert record.terminal_reason == reason
    assert record.engine_failure_profile_id in {
        first.profile_id,
        second.profile_id,
    }


def test_root_manual_adjudication_is_inconclusive_not_engine_failure() -> None:
    first, second = _profiles()
    opening = league_module.OpeningCase(
        case_id="manual-adjudication-root",
        fen="7k/8/8/8/8/8/6R1/K7 w - - 0 1",
        series_number=101,
        quiet_series=10,
        source="root quiet-draw proof fixture",
    )
    job = league_module.GameJob(
        job_key="manual-adjudication-root",
        run_id="fixture",
        generation=1,
        stage="promotion-g1",
        opening_index=0,
        opening=opening,
        seed=1,
        white_profile=first,
        black_profile=second,
        search_depth=1,
        max_series_per_node=8,
        max_generation_positions=10_000,
        max_game_work_positions=None,
        emergency_max_series=None,
    )

    record = league_module._play_game(job)

    assert record.result == "*"
    assert record.terminal_reason == "manual-adjudication-pending"
    assert record.decisive_profile_id is None
    assert record.engine_failure_profile_id is None
    assert record.error is None
    assert record.series_played == 0
    assert len(record.trace) == 1
    assert record.trace[0]["played"] is False
    assert record.trace[0]["promotion_mate_positions"] == 0
    assert record.trace[0]["promotion_mate_setup_states"] == 0
    assert record.trace[0]["promotion_mate_candidates"] == 0
    assert record.trace[0]["promotion_mate_completion_probes"] == 0
    assert record.trace[0]["promotion_mate_mates"] == 0
    assert record.trace[0]["promotion_mate_limit_hits"] == 0
    assert record.trace[0]["promotion_mate_replay_rejects"] == 0

    rows = _pair_rows(
        first.profile_id,
        second.profile_id,
        case_id="manual-adjudication-root",
        pair_index=0,
        results=("*", "1-0"),
        reasons=("manual-adjudication-pending", "checkmate"),
    )
    evidence = _pair_evidence(rows, first.profile_id)
    assert evidence.completed_pairs == 0
    assert evidence.candidate_failures == 0
    assert evidence.worker_failures == 0


def test_league_trace_preserves_nonzero_promotion_mate_evidence(monkeypatch) -> None:
    first, second = _profiles()
    opening = league_module.OpeningCase(
        case_id="promotion-mate-trace",
        fen=(
            "bnq1nr2/p1pp1pk1/8/4PP2/1P2P1p1/8/"
            "P1P2KP1/BNbBN2r w - - 0 1"
        ),
        series_number=7,
        source="trace serialization fixture",
    )
    job = league_module.GameJob(
        job_key="promotion-mate-trace",
        run_id="fixture",
        generation=1,
        stage="promotion-g1",
        opening_index=0,
        opening=opening,
        seed=1,
        white_profile=first,
        black_profile=second,
        search_depth=2,
        max_series_per_node=32,
        max_generation_positions=250_000,
        max_game_work_positions=None,
        emergency_max_series=None,
    )
    selected = play_series(
        opening.state(),
        ("e1f3", "f3d4", "e5e6", "e6e7", "e7f8r", "f8h8", "d4e6"),
    )
    stats = SimpleNamespace(
        work_positions=31,
        nodes=2,
        root_bound_candidates=0,
        promotion_mate_positions=23,
        promotion_mate_setup_states=17,
        promotion_mate_candidates=11,
        promotion_mate_completion_probes=7,
        promotion_mate_mates=1,
        promotion_mate_limit_hits=2,
        promotion_mate_replay_rejects=3,
    )
    result = SimpleNamespace(
        best_series=selected,
        score=999_999,
        completed_depth=2,
        exact_width=False,
        root_scores_complete=True,
        work_limit_reached=False,
        timed_out=False,
        proof="white",
        adjudication_status=None,
        stats=stats,
    )
    monkeypatch.setattr(league_module, "analyze", lambda *args, **kwargs: result)

    record = league_module._play_game(job)
    trace = record.trace[0]
    assert trace["promotion_mate_positions"] == 23
    assert trace["promotion_mate_setup_states"] == 17
    assert trace["promotion_mate_candidates"] == 11
    assert trace["promotion_mate_completion_probes"] == 7
    assert trace["promotion_mate_mates"] == 1
    assert trace["promotion_mate_limit_hits"] == 2
    assert trace["promotion_mate_replay_rejects"] == 3


def test_child_manual_fallback_is_inconclusive_before_move_is_played() -> None:
    first, second = _profiles()
    opening = league_module.OpeningCase(
        case_id="manual-adjudication-child",
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        series_number=1,
        quiet_series=9,
        source="child quiet-draw proof fixture",
    )
    job = league_module.GameJob(
        job_key="manual-adjudication-child",
        run_id="fixture",
        generation=1,
        stage="promotion-g1",
        opening_index=0,
        opening=opening,
        seed=1,
        white_profile=first,
        black_profile=second,
        search_depth=1,
        max_series_per_node=32,
        max_generation_positions=10_000,
        max_game_work_positions=None,
        emergency_max_series=None,
    )

    record = league_module._play_game(job)

    assert record.result == "*"
    assert record.terminal_reason == "manual-adjudication-pending"
    assert record.decisive_profile_id is None
    assert record.engine_failure_profile_id is None
    assert record.error is None
    assert record.series_played == 0
    assert record.final_pfen == opening.state().pfen
    assert len(record.trace) == 1
    assert record.trace[0]["series"] is not None
    assert record.trace[0]["played"] is False
    assert record.trace[0]["completed_depth"] == 0
    assert record.trace[0]["root_scores_complete"] is False


def test_worker_exception_is_retriable_and_upserted(tmp_path) -> None:
    database = tmp_path / "retry.sqlite3"
    first, second = _profiles()
    config = LeagueConfig.smoke()
    budget = resource_module.detect_resource_budget(
        1, memory_per_worker_mb=128, reserve_memory_mb=128
    )
    opening = OPENING_SUITE[0]
    with LeagueStore(database) as store:
        store.set_active_champion(first)
        run_id = store.create_run(config, budget, first)
        store.save_profile(second)
        worker = GameRecord(
            "job", run_id, 1, "preliminary-g1", 0, opening.case_id,
            OPENING_SUITE_VERSION, 1, first.profile_id, second.profile_id,
            "*", "worker-exception", None, None, opening.state().pfen,
            opening.state().pfen, 0, (), "fixture",
        )
        store.save_game(worker, opening)
        assert "job" not in store.completed_job_keys(run_id, 1, "preliminary-g1")
        completed = GameRecord(
            "job", run_id, 1, "preliminary-g1", 0, opening.case_id,
            OPENING_SUITE_VERSION, 1, first.profile_id, second.profile_id,
            "1-0", "checkmate", first.profile_id, None, opening.state().pfen,
            opening.state().pfen, 1, (), None,
        )
        store.save_game(completed, opening)
        assert "job" in store.completed_job_keys(run_id, 1, "preliminary-g1")
        row = store.stage_rows(run_id, 1, "preliminary-g1")[0]
        assert row["result"] == "1-0"
        assert row["error"] is None


def test_fitness_ranks_valid_pair_score_not_inconclusive_flag() -> None:
    first, second = _profiles()
    rows = _pair_rows(
        first.profile_id,
        second.profile_id,
        case_id=OPENING_SUITE[0].case_id,
        pair_index=0,
        results=("1-0", "0-1"),
    )
    rows += _pair_rows(
        first.profile_id,
        second.profile_id,
        case_id=OPENING_SUITE[1].case_id,
        pair_index=1,
        results=("*", "*"),
        reasons=(
            "technical-game-work-budget-exhausted",
            "technical-game-work-budget-exhausted",
        ),
    )
    ranking = _fitness(rows, (first, second))
    assert ranking[0]["profile_id"] == first.profile_id
    assert ranking[0]["score_rate"] == 1.0
    assert ranking[1]["score_rate"] == 0.0


def test_fitness_allows_case_reuse_across_different_opponents() -> None:
    first, second, third = create_population(size=3, seed=505)
    case_id = OPENING_SUITE[0].case_id
    rows = _pair_rows(
        first.profile_id,
        second.profile_id,
        case_id=case_id,
        pair_index=0,
        results=("1-0", "0-1"),
    )
    rows += _pair_rows(
        first.profile_id,
        third.profile_id,
        case_id=case_id,
        pair_index=0,
        results=("1-0", "0-1"),
    )
    for row in rows:
        row["stage"] = "preliminary-g1"

    ranking = _fitness(rows, (first, second, third))
    by_id = {item["profile_id"]: item for item in ranking}

    assert by_id[first.profile_id]["pairs"] == 2
    assert by_id[first.profile_id]["wins"] == 2
    assert by_id[first.profile_id]["games"] == 4
    assert by_id[first.profile_id]["mate_efficiency"]["balanced_pair_games"] == 4
    assert by_id[second.profile_id]["losses"] == 1
    assert by_id[second.profile_id]["mate_efficiency"]["balanced_pair_games"] == 2
    assert by_id[third.profile_id]["losses"] == 1
    assert by_id[third.profile_id]["mate_efficiency"]["balanced_pair_games"] == 2


def test_equal_pair_score_uses_balanced_checkmate_efficiency_only_as_tiebreak() -> None:
    first, second = _profiles()
    rows = _pair_rows(
        first.profile_id,
        second.profile_id,
        case_id=OPENING_SUITE[0].case_id,
        pair_index=0,
        results=("1-0", "1-0"),
    )
    rows[0]["decisive_profile_id"] = first.profile_id
    rows[0]["series_played"] = 2
    rows[1]["decisive_profile_id"] = second.profile_id
    rows[1]["series_played"] = 5
    ranking = _fitness(rows, (first, second))
    assert ranking[0]["profile_id"] == first.profile_id
    assert ranking[0]["score_rate"] == ranking[1]["score_rate"] == 0.5
    assert ranking[0]["mate_efficiency"] == {
        "balanced_pair_games": 2,
        "checkmate_wins": 1,
        "average_winning_mate_series": 2.0,
        "checkmate_losses": 1,
        "average_losing_resistance_series": 5.0,
    }


def test_next_population_contains_champion_partner_crossover() -> None:
    champion, partner = _profiles()
    population = _next_population(
        champion, partner, (), size=4, seed=9, generation=2
    )
    assert population[0].profile_id == champion.profile_id
    assert population[1].parent_profile_ids == (
        champion.profile_id,
        partner.profile_id,
    )


def test_resource_budget_is_explicitly_an_estimate(monkeypatch) -> None:
    monkeypatch.setattr(resource_module, "detected_logical_cpus", lambda: 16)
    monkeypatch.setattr(
        resource_module, "detected_available_memory", lambda: 10 * 1024**3
    )
    budget = resource_module.detect_resource_budget(
        999, memory_per_worker_mb=1024, reserve_memory_mb=2048
    )
    payload = budget.as_dict()
    assert budget.workers == 8
    assert payload["envelope_kind"] == "detected-estimated"
    assert payload["cpu_worker_limit_enforced"] is True
    assert payload["ram_limit_enforced"] is False
    assert "estimate" in payload["ram_note"]


def test_cli_threads_budget_defaults_two_generations_and_progress(
    tmp_path, monkeypatch, capsys
) -> None:
    captured: dict[str, object] = {}

    def fake_run_league(database, **kwargs):
        captured["database"] = database
        captured.update(kwargs)
        kwargs["progress"]("fixture stage: finished 1/1 scheduled games")
        return {"status": "complete", "run_id": "fixture"}

    monkeypatch.setattr("scottish_progressive.league.run_league", fake_run_league)
    assert main(
        [
            "league", "run", str(tmp_path / "league.sqlite3"),
            "--max-generation-positions", "4321",
            "--champion-output", str(tmp_path / "champion.json"),
        ]
    ) == 0
    config = captured["config"]
    assert config.max_generation_positions == 4321
    assert config.generations == 2
    assert "finished 1/1" in capsys.readouterr().out


def test_cli_continue_latest_resumes_needs_resume_run(tmp_path, monkeypatch) -> None:
    database = tmp_path / "needs-resume.sqlite3"
    champion = baseline_profile()
    config = LeagueConfig.smoke()
    budget = resource_module.detect_resource_budget(
        1, memory_per_worker_mb=128, reserve_memory_mb=128
    )
    with LeagueStore(database) as store:
        store.set_active_champion(champion)
        run_id = store.create_run(config, budget, champion)
        store.update_run(
            run_id,
            status="needs-resume",
            generation=1,
            champion_id=champion.profile_id,
            reason="worker fixture",
        )

    captured: dict[str, object] = {}

    def fake_run_league(database_path, **kwargs):
        captured.update(kwargs)
        return {"status": "complete", "run_id": run_id}

    monkeypatch.setattr("scottish_progressive.league.run_league", fake_run_league)
    assert main(
        [
            "league", "run", str(database), "--continue-latest",
            "--champion-output", str(tmp_path / "champion.json"),
        ]
    ) == 0
    assert captured["resume_run_id"] == run_id
    assert captured["config"] is None


def test_champion_output_refreshes_stale_file_and_completed_resume(tmp_path) -> None:
    database = tmp_path / "smoke.sqlite3"
    output = tmp_path / "champion.json"
    champion = mutate_profile(baseline_profile(), seed=808)
    save_profile(baseline_profile(), output)
    progress: list[str] = []
    status = run_league(
        database,
        config=LeagueConfig.smoke(seed=101),
        initial_champion=champion,
        champion_output=output,
        progress=progress.append,
    )
    assert status["status"] == "complete"
    assert load_profile(output).profile_id == champion.profile_id
    envelope = json.loads(output.read_text())
    assert envelope["format"] == "spc-champion-envelope-v1"
    provenance = envelope["provenance"]
    assert provenance["source_fingerprint"]
    assert provenance["runtime"] == runtime_provenance()
    assert provenance["publishing_run_id"] == status["run_id"]
    assert provenance["publishing_run_last_match"]["gate"]
    assert progress and any("finished" in message for message in progress)

    save_profile(baseline_profile(), output)
    with sqlite3.connect(database) as connection:
        resources = json.loads(
            connection.execute(
                "SELECT resource_json FROM runs WHERE run_id=?", (status["run_id"],)
            ).fetchone()[0]
        )
        resources["workers"] = 999
        connection.execute(
            "UPDATE runs SET resource_json=? WHERE run_id=?",
            (json.dumps(resources), status["run_id"]),
        )
        connection.commit()
    resumed = run_league(
        database,
        resume_run_id=status["run_id"],
        champion_output=output,
    )
    assert resumed["status"] == "complete"
    assert load_profile(output).profile_id == champion.profile_id
    assert json.loads(output.read_text())["provenance"]["publishing_run_id"] == status["run_id"]
    assert resumed["resources"]["workers"] <= resumed["resources"]["worker_cap"]
    assert resumed["resources"]["envelope_kind"] == "detected-estimated"


def test_resume_checks_source_and_runtime(tmp_path) -> None:
    database = tmp_path / "resume.sqlite3"
    status = run_league(database, config=LeagueConfig.smoke(seed=202))
    run_id = status["run_id"]
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET source_fingerprint='changed' WHERE run_id=?", (run_id,)
        )
        connection.commit()
    with pytest.raises(ValueError, match="source fingerprint changed"):
        run_league(database, resume_run_id=run_id)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET source_fingerprint=?,runtime_json='{}' WHERE run_id=?",
            (league_module.ENGINE_SOURCE_FINGERPRINT, run_id),
        )
        connection.commit()
    with pytest.raises(ValueError, match="runtime changed"):
        run_league(database, resume_run_id=run_id)


def test_status_exposes_runtime_resources_and_deterministic_method(tmp_path) -> None:
    database = tmp_path / "status.sqlite3"
    status = run_league(database, config=LeagueConfig.smoke(seed=303))
    loaded = league_status(database, status["run_id"])
    assert loaded["runtime"] == runtime_provenance()
    assert loaded["promotion_method"] == PROMOTION_METHOD
    assert loaded["resources"]["envelope_kind"] == "detected-estimated"
