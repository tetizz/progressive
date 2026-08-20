from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from scottish_progressive.evaluation import evaluate
from scottish_progressive.fast_training import (
    PROXY_DISCLAIMER,
    CachedFeatures,
    FastTrainingConfig,
    TrainingPosition,
    benchmark_profile_scoring,
    build_training_cache,
    default_training_positions,
    load_training_cache,
    rank_profiles,
    run_fast_preselection,
    save_training_cache,
)
from scottish_progressive.model import ProgressiveState
from scottish_progressive.cli import main
from scottish_progressive.league import LeagueConfig, LeagueStore, run_league
from scottish_progressive.profiles import (
    baseline_profile,
    create_population,
    mutate_profile,
)
from scottish_progressive.resources import detect_resource_budget
from scottish_progressive.search import SearchLimits, analyze


@pytest.fixture(scope="module")
def fast_config() -> FastTrainingConfig:
    return FastTrainingConfig(
        position_limit=4,
        rollout_steps=1,
        label_depth_series=2,
        label_branch_cap=4,
        label_max_work_positions=200_000,
        finalist_count=2,
        stage_two_multiplier=2,
        seed=41,
        smoke=True,
    )


@pytest.fixture(scope="module")
def training_cache(fast_config):
    defaults = default_training_positions()
    positions = defaults[:4]
    return build_training_cache(
        baseline_profile(), config=fast_config, positions=positions
    )


@pytest.fixture(scope="module")
def full_training_cache():
    return build_training_cache(
        baseline_profile(),
        config=FastTrainingConfig(),
        positions=default_training_positions(),
    )


def test_cached_seven_term_dot_score_matches_live_evaluation() -> None:
    state = ProgressiveState.initial()
    profile = mutate_profile(baseline_profile(), seed=811)

    cached = CachedFeatures.from_state(state)

    assert cached.score(profile) == evaluate(state, profile).total
    assert cached.reach_complete == evaluate(state, profile).reach_complete
    assert cached.white_king_ring_attack_multiplicity >= 0
    assert cached.black_king_ring_attack_multiplicity >= 0


def test_cache_uses_canonical_trace_split_without_row_leakage(training_cache) -> None:
    split_by_trace: dict[str, set[str]] = {}
    hashes: set[str] = set()
    for position in training_cache.positions:
        split_by_trace.setdefault(position.trace_id, set()).add(position.split)
        assert position.position_hash not in hashes
        hashes.add(position.position_hash)

    assert all(len(splits) == 1 for splits in split_by_trace.values())
    assert {position.split for position in training_cache.positions} == {
        "train",
        "holdout",
    }


def test_opening_history_prefixes_share_one_line_family_and_split(
    full_training_cache,
) -> None:
    roots = {
        position.case_id: position
        for position in full_training_cache.positions
        if position.trace_step == 0
    }
    prefix_pairs = {
        "b1a3": ("after-1-na3", "after-b1a3-a6-b6"),
        "b1c3": ("after-1-nc3", "after-b1c3-a6-b6"),
        "b2b3": ("after-1-b3", "after-b2b3-a6-b6"),
        "c2c4": ("after-1-c4", "after-c2c4-a6-b6"),
        "d2d4": ("after-1-d4", "after-d2d4-a6-b6"),
        "e2e4": ("after-1-e4", "after-e2e4-a6-b6"),
        "g1f3": ("after-1-nf3", "after-g1f3-a6-b6"),
    }
    for first_uci, (ancestor_id, descendant_id) in prefix_pairs.items():
        ancestor = roots[ancestor_id]
        descendant = roots[descendant_id]
        assert ancestor.trace_id == descendant.trace_id == (
            f"opening-suite-v4:first-series:{first_uci}"
        )
        assert ancestor.split == descendant.split

    assert roots["initial"].trace_id == "opening-suite-v4:empty-root-anchor"
    assert roots["published-bishop-pressure"].trace_id == (
        roots["after-1-e4"].trace_id
    )
    assert roots["published-central-pressure"].trace_id == (
        roots["after-1-d4"].trace_id
    )


def test_full_selective_corpus_never_claims_safety_but_can_shortlist_testing(
    full_training_cache,
) -> None:
    performed = [
        position
        for position in full_training_cache.positions
        if position.bounded_opponent_mate_check_performed
    ]
    assert sum(position.trace_step == 0 for position in full_training_cache.positions) == 32
    assert performed
    assert not any(
        position.bounded_opponent_mate_check_complete
        for position in performed
    )

    report = rank_profiles(
        full_training_cache,
        create_population(baseline_profile(), size=10, seed=51),
    )

    assert report["finalist_profile_ids"]
    stage_two = [row for row in report["ranking"] if row["stage_two_evaluated"]]
    assert stage_two
    assert not any(
        row["bounded_opponent_mate_safety_passed"] for row in stage_two
    )
    assert not any(row["tactical_non_regression_passed"] for row in stage_two)
    for profile_id in report["finalist_profile_ids"]:
        finalist = next(
            row for row in report["ranking"] if row["profile_id"] == profile_id
        )
        assert finalist["eligible_for_full_game_testing"] is True
        assert finalist["bounded_opponent_mate_safety_status"] == (
            "unknown-selective"
        )
        assert finalist["tactical_screen_status"] == (
            "provisional-unknown-selective"
        )
        assert finalist["short_rollout_proxy"][
            "opponent_mate_safety_passed"
        ] is False


def test_cache_labels_are_deeper_search_evidence_not_wdl(training_cache) -> None:
    for position in training_cache.positions:
        provenance = position.label_provenance
        assert provenance["kind"] == "deeper-bounded-internal-research"
        assert provenance["completed_depth_series"] == 2
        assert provenance["score_unit"] == "white-centric-heuristic-points"
        assert provenance["is_game_outcome"] is False
        assert provenance["is_wdl"] is False

    contract = training_cache.as_dict()["proxy_contract"]
    assert contract == {
        "is_wdl": False,
        "strength_claim": False,
        "teacher_distillation": True,
        "teacher_move_is_truth": False,
        "external_code_copied": False,
        "notice": PROXY_DISCLAIMER,
    }


def test_cache_round_trip_is_deterministic_and_tamper_evident(
    tmp_path: Path, training_cache
) -> None:
    path = save_training_cache(training_cache, tmp_path / "cache.json")

    loaded = load_training_cache(path)

    assert loaded == training_cache
    assert loaded.cache_id == training_cache.cache_id
    payload = json.loads(path.read_text())
    payload["positions"][0]["label_best_series"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="cache_id"):
        load_training_cache(path)


def test_external_suggestion_is_provenanced_but_internal_research_labels_it() -> None:
    position = TrainingPosition.from_state(
        "prior-engine-suggestion",
        ProgressiveState.initial(),
        suggestion_series="e2e4",
        suggestion_provenance={
            "provider": "Bucephalus",
            "artifact_version": "unspecified",
            "role": "move-suggestion-only",
            "code_imported": False,
        },
    )
    config = FastTrainingConfig(
        position_limit=1,
        rollout_steps=2,
        label_depth_series=1,
        label_branch_cap=4,
        label_max_work_positions=50_000,
        finalist_count=1,
        smoke=True,
    )

    cache = build_training_cache(
        baseline_profile(), config=config, positions=(position,)
    )
    row = cache.positions[0]

    assert row.suggestion_series == "e2e4"
    assert row.suggestion_provenance["code_imported"] is False
    assert isinstance(row.suggestion_agrees_with_label, bool)
    assert row.label_provenance["kind"] == "deeper-bounded-internal-research"
    assert len(cache.positions) == 2
    assert cache.positions[1].suggestion_series is None
    assert cache.positions[1].suggestion_provenance is None
    assert cache.positions[1].suggestion_agrees_with_label is None


def test_unproven_suggestion_without_provenance_is_rejected() -> None:
    with pytest.raises(ValueError, match="explicit provenance"):
        TrainingPosition.from_state(
            "bad-suggestion",
            ProgressiveState.initial(),
            suggestion_series="e2e4",
        )
    with pytest.raises(ValueError, match="code_imported=false"):
        TrainingPosition.from_state(
            "copied-code-suggestion",
            ProgressiveState.initial(),
            suggestion_series="e2e4",
            suggestion_provenance={
                "provider": "untrusted teacher",
                "code_imported": True,
            },
        )


def test_all_corpus_roots_are_cached_before_derived_rollouts() -> None:
    champion = baseline_profile()
    initial = ProgressiveState.initial()
    limits = SearchLimits(
        depth_series=1,
        max_series_per_node=4,
        time_limit_seconds=None,
        max_generation_positions=50_000,
    )
    best = analyze(initial, limits, profile=champion).best_series
    assert best is not None and best.outcome is None
    reached_as_rollout = best.final_state
    positions = (
        TrainingPosition.from_state("trace-a-root", initial),
        TrainingPosition.from_state("trace-b-root", reached_as_rollout),
    )
    config = FastTrainingConfig(
        position_limit=2,
        rollout_steps=2,
        label_depth_series=1,
        label_branch_cap=4,
        label_max_work_positions=50_000,
        finalist_count=1,
        smoke=True,
    )

    cache = build_training_cache(
        champion, config=config, positions=positions
    )

    assert {
        position.case_id
        for position in cache.positions
        if position.trace_step == 0
    } == {"trace-a-root", "trace-b-root"}
    assert len({position.position_hash for position in cache.positions}) == len(
        cache.positions
    )


def test_duplicate_corpus_roots_are_rejected() -> None:
    state = ProgressiveState.initial()
    positions = (
        TrainingPosition.from_state("duplicate-a", state),
        TrainingPosition.from_state("duplicate-b", state),
    )
    config = FastTrainingConfig(
        position_limit=2,
        rollout_steps=1,
        label_depth_series=1,
        label_branch_cap=4,
        label_max_work_positions=50_000,
        finalist_count=1,
        smoke=True,
    )

    with pytest.raises(ValueError, match="duplicate root positions"):
        build_training_cache(
            baseline_profile(), config=config, positions=positions
        )


def test_default_corpus_is_all_v4_boundaries_plus_tactical_anchors() -> None:
    positions = default_training_positions()

    assert len(positions) == 32
    assert sum(item.tactical_anchor for item in positions) == 2
    assert all(
        item.case_id.startswith("fast-tactical-")
        for item in positions
        if item.tactical_anchor
    )
    assert len({item.state().position_hash for item in positions}) == 32


def test_non_smoke_mode_rejects_an_incomplete_explicit_corpus() -> None:
    with pytest.raises(ValueError, match="30 opening boundaries"):
        FastTrainingConfig(position_limit=31)
    config = FastTrainingConfig(position_limit=32)

    with pytest.raises(ValueError, match="corpus is incomplete"):
        build_training_cache(
            baseline_profile(),
            config=config,
            positions=default_training_positions()[:4],
        )
    without_anchors = tuple(
        replace(position, tactical_anchor=False)
        for position in default_training_positions()
    )
    with pytest.raises(ValueError, match="two explicit tactical anchors"):
        build_training_cache(
            baseline_profile(),
            config=config,
            positions=without_anchors,
        )
    with pytest.raises(ValueError, match="30 opening boundaries"):
        LeagueConfig(fast_preselection_positions=31)


def test_ranked_proxy_is_not_wdl_and_full_game_gate_is_unchanged(
    training_cache,
) -> None:
    population = create_population(baseline_profile(), size=10, seed=71)

    report = rank_profiles(training_cache, population)

    assert report["proxy_contract"]["is_wdl"] is False
    assert report["proxy_contract"]["strength_claim"] is False
    assert report["proxy_contract"]["full_game_promotion_required"] is True
    assert report["full_game_schedule"] == {
        "legacy_preliminary_games": 450,
        "fast_funnel_preliminary_games": 0,
        "promotion_games_if_challenger": 20,
        "total_before": 470,
        "total_after": 20,
        "games_avoided": 450,
        "reduction_fraction": 0.957447,
        "promotion_gate_changed": False,
    }
    assert report["finalist_profile_ids"]
    for row in report["ranking"]:
        assert row["position_proxy"]["is_wdl"] is False
        assert row["short_rollout_proxy"]["is_wdl"] is False
        assert row["strength_claim"] is False
    # The report is API-safe JSON: unevaluated profiles use null, never NaN or
    # Infinity, and no short-rollout score is emitted without safety evidence.
    json.dumps(report, allow_nan=False)
    for row in report["ranking"]:
        rollout = row["short_rollout_proxy"]
        if row["bounded_opponent_mate_safety_passed"]:
            assert row["bounded_opponent_mate_safety_status"] == (
                "complete-no-mate"
            )
            assert rollout["safety_check_complete_position_count"] > 0
        if row["profile_id"] in report["finalist_profile_ids"]:
            assert row["eligible_for_full_game_testing"] is True


def test_truncated_branch_cap_is_unknown_and_never_a_false_safety_pass(
    training_cache,
) -> None:
    performed = [
        position
        for position in training_cache.positions
        if position.bounded_opponent_mate_check_performed
    ]
    assert performed
    assert all(
        not position.bounded_opponent_mate_check_complete
        for position in performed
    )

    report = rank_profiles(
        training_cache,
        create_population(baseline_profile(), size=4, seed=81),
    )

    for row in report["ranking"]:
        if not row["stage_two_evaluated"]:
            continue
        assert row["bounded_opponent_mate_safety_status"] in {
            "unknown-selective",
            "proven-unsafe",
        }
        assert row["bounded_opponent_mate_safety_passed"] is False
        assert row["tactical_non_regression_passed"] is False
        rollout = row["short_rollout_proxy"]
        assert rollout["safety_check_complete_position_count"] == 0
        assert rollout["safety_check_incomplete_position_count"] > 0


def test_large_population_only_sends_top_k_through_short_rollout(training_cache) -> None:
    population = create_population(baseline_profile(), size=64, seed=91)

    report = rank_profiles(training_cache, population)

    evaluated = sum(row["stage_two_evaluated"] for row in report["ranking"])
    assert evaluated <= (
        training_cache.config.finalist_count
        * training_cache.config.stage_two_multiplier
        + 1
    )
    assert len(report["finalist_profile_ids"]) <= 2


def test_tactical_anchor_and_bounded_opponent_mate_screen_are_mandatory(
    training_cache,
) -> None:
    tactical_index = next(
        index
        for index, position in enumerate(training_cache.positions)
        if position.tactical_expected_series
    )
    tactical = training_cache.positions[tactical_index]
    bad_tactical = replace(
        tactical,
        tactical_expected_series=("not-a-legal-cached-series",),
    )
    broken = replace(
        training_cache,
        positions=training_cache.positions[:tactical_index]
        + (bad_tactical,)
        + training_cache.positions[tactical_index + 1 :],
    )

    report = rank_profiles(
        broken, create_population(baseline_profile(), size=4, seed=101)
    )

    assert not report["finalist_profile_ids"]
    assert all(
        not row["tactical_non_regression_passed"]
        for row in report["ranking"]
        if row["stage_two_evaluated"]
    )


def test_depth_one_rows_cannot_masquerade_as_safety_checked_rollouts(
    training_cache,
) -> None:
    unchecked = replace(
        training_cache,
        positions=tuple(
            replace(
                position,
                bounded_opponent_mate_check_performed=False,
                bounded_opponent_mate_check_complete=False,
            )
            for position in training_cache.positions
        ),
    )

    report = rank_profiles(
        unchecked, create_population(baseline_profile(), size=4, seed=111)
    )

    assert not report["finalist_profile_ids"]
    json.dumps(report, allow_nan=False)
    for row in report["ranking"]:
        if row["stage_two_evaluated"]:
            assert row["bounded_opponent_mate_safety_passed"] is False
            assert row["bounded_opponent_mate_safety_status"] == (
                "unknown-selective"
            )
            assert row["short_rollout_proxy"] == {
                "mean_discounted_regret": None,
                "safety_checked_position_count": 0,
                "safety_check_performed_position_count": 0,
                "safety_check_complete_position_count": 0,
                "safety_check_incomplete_position_count": 0,
                "unit": "cached-short-rollout-proxy-points",
                "is_wdl": False,
                "opponent_mate_safety_check": "bounded-cached-research",
                "opponent_mate_safety_status": "unknown-selective",
                "opponent_mate_safety_passed": False,
            }


def test_ranking_evidence_id_is_deterministic_despite_timing(training_cache) -> None:
    population = create_population(baseline_profile(), size=10, seed=121)

    first = rank_profiles(training_cache, population)
    second = rank_profiles(training_cache, population)

    assert first["evidence_id"] == second["evidence_id"]
    assert first["ranking"] == second["ranking"]
    assert first["performance"]["elapsed_seconds"] >= 0


def test_preselection_resume_reuses_atomic_cache_and_report(
    tmp_path: Path, fast_config
) -> None:
    champion = baseline_profile()
    population = create_population(champion, size=6, seed=141)
    cache_path = tmp_path / "training-cache.json"
    report_path = tmp_path / "preselection-report.json"

    first, first_resumed = run_fast_preselection(
        population,
        champion,
        cache_path=cache_path,
        report_path=report_path,
        config=fast_config,
    )
    first_cache_mtime = cache_path.stat().st_mtime_ns
    first_report_mtime = report_path.stat().st_mtime_ns
    second, second_resumed = run_fast_preselection(
        population,
        champion,
        cache_path=cache_path,
        report_path=report_path,
        config=fast_config,
    )

    assert first_resumed is False
    assert second_resumed is True
    assert first == second
    assert cache_path.stat().st_mtime_ns == first_cache_mtime
    assert report_path.stat().st_mtime_ns == first_report_mtime

    changed, changed_resumed = run_fast_preselection(
        population,
        champion,
        cache_path=cache_path,
        report_path=report_path,
        config=fast_config,
        promotion_games=22,
    )
    assert changed_resumed is False
    assert changed["evidence_id"] != first["evidence_id"]
    assert changed["ranking_request"]["promotion_games"] == 22


def test_warm_cache_benchmark_reports_candidate_iterations_not_games(
    training_cache,
) -> None:
    population = create_population(baseline_profile(), size=10, seed=161)

    result = benchmark_profile_scoring(
        training_cache, population, repetitions=20
    )

    assert result["candidate_iterations"] == 200
    assert result["candidate_iterations_per_second"] > 0
    assert result["includes_cache_build"] is False
    assert result["is_wdl"] is False
    assert result["strength_claim"] is False
    assert isinstance(result["checksum"], int)


def test_population_save_is_atomic_and_resume_rejects_partial_slots(
    tmp_path: Path,
) -> None:
    champion = baseline_profile()
    population = create_population(champion, size=2, seed=181)
    config = LeagueConfig.smoke(seed=181)
    resources = detect_resource_budget(
        1, memory_per_worker_mb=128, reserve_memory_mb=128
    )
    with LeagueStore(tmp_path / "population.sqlite3") as store:
        run_id = store.create_run(config, resources, champion)
        store.save_population(run_id, 1, population, champion.profile_id)
        assert store.population(
            run_id,
            1,
            expected_size=2,
            champion_id=champion.profile_id,
        ) == population

        store.connection.execute(
            """
            CREATE TRIGGER reject_population_slot_one
            BEFORE INSERT ON run_population
            WHEN NEW.population_slot=1
            BEGIN SELECT RAISE(ABORT, 'fixture interruption'); END
            """
        )
        with pytest.raises(Exception, match="fixture interruption"):
            store.save_population(run_id, 1, tuple(reversed(population)), champion.profile_id)
        store.connection.execute("DROP TRIGGER reject_population_slot_one")
        # The delete and first insert rolled back together.
        assert store.population(
            run_id,
            1,
            expected_size=2,
            champion_id=champion.profile_id,
        ) == population

        store.connection.execute(
            "DELETE FROM run_population WHERE run_id=? AND generation=? AND population_slot=1",
            (run_id, 1),
        )
        store.connection.commit()
        with pytest.raises(ValueError, match="incomplete"):
            store.population(
                run_id,
                1,
                expected_size=2,
                champion_id=champion.profile_id,
            )


def test_league_uses_fast_funnel_instead_of_preliminary_full_games(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_preselection(profiles, champion, **kwargs):
        return (
            {
                "finalist_profile_ids": [],
                "performance": {"candidate_iterations_per_second": 12_345.0},
                "full_game_schedule": {"games_avoided": 2},
            },
            False,
        )

    monkeypatch.setattr(
        "scottish_progressive.fast_training.run_fast_preselection",
        fake_preselection,
    )
    database = tmp_path / "fast-league.sqlite3"

    status = run_league(database, config=LeagueConfig.smoke(seed=191))

    assert status["status"] == "complete"
    assert "cached tactical preselection" in status["decisive_reason"]
    with LeagueStore(database) as store:
        games = store.connection.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    assert games == 0


def test_external_match_has_a_real_cli_entrypoint(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_external_match(profile, spec, **kwargs):
        captured["profile"] = profile
        captured["spec"] = spec
        captured.update(kwargs)
        return {
            "summary": {
                "local_game_wdl": {"wins": 1, "draws": 0, "losses": 1}
            },
            "claim_scope": {"warning": "fixed-suite evidence only"},
        }

    monkeypatch.setattr(
        "scottish_progressive.external_match.run_external_match",
        fake_external_match,
    )

    assert main(
        [
            "external-match",
            "baseline",
            "fixture-bucephalus.exe",
            "--sha256",
            "a" * 64,
            "--pairs",
            "1",
        ]
    ) == 0
    assert captured["profile"].profile_id == baseline_profile().profile_id
    assert captured["spec"].sha256 == "a" * 64
    assert "fixed-suite evidence only" in capsys.readouterr().out


def test_train_fast_cli_defaults_to_full_corpus_and_labels_smoke(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    calls: list[dict[str, object]] = []

    def fake_preselection(profiles, champion, **kwargs):
        calls.append(
            {
                "profiles": profiles,
                "champion": champion,
                **kwargs,
            }
        )
        return (
            {
                "performance": {
                    "candidate_iterations_per_second": 1_500.0,
                },
                "full_game_schedule": {"games_avoided": 450},
                "finalist_profile_ids": [],
                "proxy_contract": {
                    "is_wdl": False,
                    "strength_claim": False,
                },
            },
            False,
        )

    monkeypatch.setattr(
        "scottish_progressive.fast_training.run_fast_preselection",
        fake_preselection,
    )

    assert main(["train-fast", str(tmp_path / "full"), "--json"]) == 0
    full_payload = json.loads(capsys.readouterr().out)
    assert full_payload["mode"] == "full-corpus"
    assert calls[-1]["config"].smoke is False
    assert calls[-1]["config"].position_limit == 32
    assert len(calls[-1]["profiles"]) == 10

    assert main(
        ["train-fast", str(tmp_path / "smoke"), "--smoke", "--json"]
    ) == 0
    smoke_payload = json.loads(capsys.readouterr().out)
    assert smoke_payload["mode"] == "smoke-wiring-only"
    assert calls[-1]["config"].smoke is True
    assert calls[-1]["config"].position_limit == 4

    assert main(["train-fast", str(tmp_path / "wording"), "--smoke"]) == 0
    text_output = capsys.readouterr().out
    assert "Full-game test shortlist:" in text_output
    assert "Eligible finalists:" not in text_output
