from __future__ import annotations

from pathlib import Path

import pytest

from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT, ProgressiveState
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.selfplay_training import (
    HUMAN_REFUTATION_GATE_ID,
    HUMAN_REFUTATION_TRACE,
)
from scottish_progressive.strength import (
    STRENGTH_REPORT_FORMAT,
    build_seeded_opening_suite,
    seeded_opening_suite_from_dict,
)
from scottish_progressive.tournament import (
    POPULATION_MASK,
    POPULATION_MULTIPLIER,
    POPULATION_SIZE,
    FunnelCheckpoint,
    PopulationCollisionLedger,
    PopulationStream,
    RankedCandidate,
    attach_replay_verified_full_traces,
    baseline_promotion_decision,
    behavioral_signature,
    bind_frozen_match_report,
    build_tournament_run_checkpoint,
    build_tournament_plan,
    canonical_digest,
    choose_result_blind_expansion,
    collapse_behavioral_phenotypes,
    effective_profile_id,
    exact_sign_flip_p_value,
    finalize_survivors,
    match_pair_seed,
    make_promotion_batch_artifact,
    rank_group,
    scan_population_stage_a,
    seed64,
    select_knockout_winner,
    stamp_human_gate_artifact,
    stamp_tactical_bundle,
    summarize_match_report,
    strength_report_limits,
    tournament_limits_for_stage,
    tournament_job_key,
)


PROTOCOL_DIGEST = "cd" * 32
NATIVE_IDENTITY = "ef" * 32
RUNTIME_IDENTITY_DIGEST = "01" * 32


def _promotion_batch(protocol_digest: str) -> dict:
    return make_promotion_batch_artifact(
        registry_id="73" * 32,
        reservation_key=f"tournament-test-{protocol_digest[:8]}",
        batch_index=1,
        protocol_digest=protocol_digest,
        baseline_effective_id=effective_profile_id(baseline_profile().weights),
        predecessor_chain_digest="00" * 32,
    )


def _human_gate(
    profile_id: str, *, depth: int = 2, max_work: int = 250_000
) -> dict:
    return {
        "gate_id": HUMAN_REFUTATION_GATE_ID,
        "profile_id": profile_id,
        "passed": True,
        "limits": {
            "depth_series": depth,
            "max_series_per_node": 32,
            "max_generation_positions": max_work,
            "time_limit_seconds": None,
            "collect_all_root_scores": False,
        },
        "anchors": [
            {
                "series_number": series,
                "selected_series": "legal/series",
                "requested_depth": depth,
                "completed_depth": depth,
                "timed_out": False,
                "work_limit_reached": False,
                "completed_required_search": True,
                "avoided_known_blunder": True,
                "passed": True,
            }
            for series in (2, 4)
        ],
    }


def _bundle(candidate: RankedCandidate) -> dict:
    return stamp_tactical_bundle(
        candidate,
        protocol_digest=PROTOCOL_DIGEST,
        native_source_identity=NATIVE_IDENTITY,
        runtime_identity_digest=RUNTIME_IDENTITY_DIGEST,
        rules_tactical_gate={
            "passed": True,
            "checks": [{"passed": True}],
        },
        human_refutation_gate=_human_gate(candidate.profile_id),
    )


def _report(
    candidate_id: str, reference_id: str, pair_results: list[str]
) -> dict:
    games = []
    pairs = []
    for index, result in enumerate(pair_results):
        if result == "win":
            game_results, candidate_points = ("1-0", "0-1"), 2.0
        elif result == "three-quarter":
            game_results, candidate_points = ("1-0", "1/2-1/2"), 1.5
        elif result == "draw":
            game_results, candidate_points = (
                "1/2-1/2",
                "1/2-1/2",
            ), 1.0
        elif result == "loss":
            game_results, candidate_points = ("0-1", "1-0"), 0.0
        else:
            game_results, candidate_points = ("*", "*"), None
        games.extend(
            (
                {
                    "white_profile_id": candidate_id,
                    "black_profile_id": reference_id,
                    "result": game_results[0],
                    "engine_failure_profile_id": None,
                },
                {
                    "white_profile_id": reference_id,
                    "black_profile_id": candidate_id,
                    "result": game_results[1],
                    "engine_failure_profile_id": None,
                },
            )
        )
        coarse = (
            "incomplete"
            if candidate_points is None
            else "win"
            if candidate_points > 1
            else "draw"
            if candidate_points == 1
            else "loss"
        )
        pairs.append(
            {
                "pair_index": index,
                "opening_case_id": f"case-{index}",
                "candidate_points": candidate_points,
                "result": coarse,
            }
        )
    count = len(pair_results)
    return {
        "format": STRENGTH_REPORT_FORMAT,
        "engine": {"source_fingerprint": ENGINE_SOURCE_FINGERPRINT},
        "candidate": {"profile_id": candidate_id},
        "reference": {"profile_id": reference_id},
        "config": {
            "pairs": count,
            "games": count * 2,
            "deterministic_limits": strength_report_limits(
                tournament_limits_for_stage("group")
            ),
        },
        "games": games,
        "pairs": pairs,
    }


def _bound_incomplete_report(
    candidate_id: str,
    reference_id: str,
    *,
    match_spec: dict,
    suite,
    protocol_digest: str,
    tournament_plan_digest: str,
    effective_by_profile_id: dict[str, str],
) -> dict:
    games = []
    pairs = []
    for pair_index, case in enumerate(suite.cases):
        state = case.state()
        for white, black in (
            (candidate_id, reference_id),
            (reference_id, candidate_id),
        ):
            games.append(
                {
                    "opening_case_id": case.case_id,
                    "white_profile_id": white,
                    "black_profile_id": black,
                    "result": "*",
                    "engine_failure_profile_id": None,
                    "start_pfen": state.pfen,
                    "final_pfen": state.pfen,
                    "terminal_reason": "honest-incomplete-test-fixture",
                    "trace": [],
                }
            )
        pairs.append(
            {
                "pair_index": pair_index,
                "opening_case_id": case.case_id,
                "candidate_points": None,
                "result": "incomplete",
            }
        )
    raw = {
        "format": STRENGTH_REPORT_FORMAT,
        "engine": {"source_fingerprint": ENGINE_SOURCE_FINGERPRINT},
        "candidate": {"profile_id": candidate_id},
        "reference": {"profile_id": reference_id},
        "config": {
            "pairs": 50,
            "games": 100,
            "seed": match_spec["match_seed"],
            "opening_suite_version": suite.version,
            "deterministic_limits": strength_report_limits(match_spec["limits"]),
        },
        "opening_suite": suite.as_dict(),
        "games": games,
        "pairs": pairs,
    }
    return bind_frozen_match_report(
        raw,
        match_spec=match_spec,
        protocol_digest=protocol_digest,
        tournament_plan_digest=tournament_plan_digest,
        effective_by_profile_id=effective_by_profile_id,
    )


def test_population_mapping_is_bijective_and_baseline_is_replaced() -> None:
    stream = PopulationStream(baseline_profile())
    assert len(stream) == POPULATION_SIZE
    first = [stream.member(index) for index in range(10_000)]
    assert len({member.code for member in first}) == 10_000
    assert len({member.weight_tuple for member in first}) == 10_000
    repeated = PopulationStream(baseline_profile())
    assert [member.effective_id for member in first] == [
        repeated.member(index).effective_id for index in range(10_000)
    ]
    baseline_code = sum(
        4 << shift for shift in (0, 3, 6, 9, 12, 15)
    ) + (8 << 18)
    inverse = pow(POPULATION_MULTIPLIER, -1, POPULATION_SIZE)
    baseline_index = (
        (baseline_code - stream.order_offset) * inverse
    ) & POPULATION_MASK
    replaced = stream.member(baseline_index)
    assert replaced.profile.weights.material == 101
    assert replaced.weight_tuple[1:] == (100, 100, 100, 100, 100, 100)
    assert replaced.effective_id != effective_profile_id(
        baseline_profile().weights
    )
    diversity = stream.diversity_manifest()
    assert diversity["unique_weight_vectors_by_bijection"] == POPULATION_SIZE
    assert diversity["l1_distance_from_baseline"]["minimum"] == 1


def test_stream_checkpoint_resume_is_partition_invariant(
    tmp_path: Path,
) -> None:
    stream = PopulationStream(baseline_profile())
    protocol = "ab" * 32

    def scorer(member):
        return sum(member.weight_tuple)

    with PopulationCollisionLedger(
        tmp_path / "split.sqlite", protocol_digest=protocol
    ) as ledger:
        first = scan_population_stage_a(
            stream,
            scorer,
            protocol_digest=protocol,
            scorer_digest="01" * 32,
            collision_ledger=ledger,
            stop_index=500,
        )
        with pytest.raises(ValueError, match="identity mismatch"):
            scan_population_stage_a(
                stream,
                scorer,
                protocol_digest=protocol,
                scorer_digest="02" * 32,
                collision_ledger=ledger,
                checkpoint=first,
                stop_index=1_000,
            )
        resumed = scan_population_stage_a(
            stream,
            scorer,
            protocol_digest=protocol,
            scorer_digest="01" * 32,
            collision_ledger=ledger,
            checkpoint=first,
            stop_index=1_000,
        )
    with PopulationCollisionLedger(
        tmp_path / "one.sqlite", protocol_digest=protocol
    ) as ledger:
        one = scan_population_stage_a(
            stream,
            scorer,
            protocol_digest=protocol,
            scorer_digest="01" * 32,
            collision_ledger=ledger,
            stop_index=1_000,
        )
    assert resumed.disposition == one.disposition
    assert resumed.ranked_candidates == one.ranked_candidates
    assert resumed.next_input_offset == 1_000
    assert resumed.complete is False
    encoded = resumed.as_dict()
    assert FunnelCheckpoint.from_dict(encoded) == resumed
    encoded["next_input_offset"] = 999
    with pytest.raises(ValueError, match="digest mismatch"):
        FunnelCheckpoint.from_dict(encoded)


def test_behavioral_collapse_keeps_best_ranked_representative() -> None:
    rows = [
        {
            "case_id": "b",
            "selected_series": "b2b3",
            "clipped_score": 7,
        },
        {
            "case_id": "a",
            "selected_series": "a2a3",
            "clipped_score": -2,
        },
    ]
    assert behavioral_signature(rows) == behavioral_signature(
        tuple(reversed(rows))
    )
    first = RankedCandidate(1, "e1", "p1", 5)
    second = RankedCandidate(2, "e2", "p2", 4)
    third = RankedCandidate(3, "e3", "p3", 6)
    signature = behavioral_signature(rows)
    collapsed = collapse_behavioral_phenotypes(
        (first, second, third),
        {"e1": signature, "e2": signature, "e3": "f" * 64},
    )
    assert collapsed == (second, third)


def test_survivors_use_stage_c_then_stage_b_and_never_relax() -> None:
    stage_c = tuple(
        RankedCandidate(index, f"e{index}", f"p{index}", index)
        for index in range(512)
    )
    stage_b = tuple(
        RankedCandidate(index, f"e{index}", f"p{index}", index)
        for index in range(8_192)
    )
    candidates = (*stage_c, *stage_b[512:])
    bundles = {
        candidate.effective_id: _bundle(candidate)
        for candidate in candidates[:64]
    }
    result = finalize_survivors(
        stage_c,
        stage_b,
        bundles,
        protocol_digest=PROTOCOL_DIGEST,
        native_source_identity=NATIVE_IDENTITY,
        runtime_identity_digest=RUNTIME_IDENTITY_DIGEST,
    )
    assert result["status"] == "ready"
    assert [item["effective_id"] for item in result["survivors"]] == [
        candidate.effective_id for candidate in candidates[:64]
    ]
    insufficient = finalize_survivors(
        stage_c,
        stage_b,
        dict(list(bundles.items())[:63]),
        protocol_digest=PROTOCOL_DIGEST,
        native_source_identity=NATIVE_IDENTITY,
        runtime_identity_digest=RUNTIME_IDENTITY_DIGEST,
    )
    assert insufficient["status"] == "insufficient-tactical-survivors"
    stale = {key: dict(value) for key, value in bundles.items()}
    stale[candidates[0].effective_id]["source_fingerprint"] = "stale"
    stale_result = finalize_survivors(
        stage_c,
        stage_b,
        stale,
        protocol_digest=PROTOCOL_DIGEST,
        native_source_identity=NATIVE_IDENTITY,
        runtime_identity_digest=RUNTIME_IDENTITY_DIGEST,
    )
    assert stale_result["status"] == "insufficient-tactical-survivors"
    assert stale_result["tactical_dispositions"][0]["passed"] is False


def test_plan_has_pots_fresh_suites_and_exact_game_totals() -> None:
    stream = PopulationStream(baseline_profile())
    survivors = tuple(
        RankedCandidate(
            member.candidate_index,
            member.effective_id,
            member.profile.profile_id,
            index,
        )
        for index, member in enumerate(stream.iter_range(0, 64))
    )
    plan = build_tournament_plan(
        survivors,
        baseline_profile(),
        protocol_digest="12" * 32,
        promotion_batch=_promotion_batch("12" * 32),
    )
    assert plan["scheduled"]["base_total_games"] == 24_300
    assert plan["scheduled"]["expanded_total_games"] == 25_000
    assert len(plan["matchups"]["group"]) == 224
    assert len(plan["matchups"]["round_of_16"]) == 8
    assert len(plan["opening_suites"]) == 13
    assert all(len(reserve["attempt_lanes"]) == 3 for reserve in plan["opening_suites"])
    assert len(
        {
            lane["seed"]
            for reserve in plan["opening_suites"]
            for lane in reserve["attempt_lanes"]
        }
    ) == 39
    assert plan["scheduled"]["replacement_opening_attempts"] == 2
    assert plan["scheduled"]["nominal_selected_games"] == {
        "base": 24_300,
        "expanded": 25_000,
    }
    assert plan["scheduled"]["worst_case_executed_games"] == {
        "base": 72_900,
        "expanded": 75_000,
    }
    assert all(len(group) == 8 for group in plan["groups"].values())
    for pot in range(8):
        pot_ids = {
            item.effective_id
            for item in survivors[pot * 8 : pot * 8 + 8]
        }
        assert all(
            len(
                pot_ids
                & {item["effective_id"] for item in group}
            )
            == 1
            for group in plan["groups"].values()
        )
    assert plan["advancement"]["group_stage_can_promote"] is False
    assert plan["advancement"]["group_advancers"] == 16
    group_limits = tournament_limits_for_stage("group")
    decisive_limits = tournament_limits_for_stage("round-of-16")
    assert all(
        spec["limits"] == group_limits
        for spec in plan["matchups"]["group"]
    )
    decisive_specs = [
        spec
        for key in (
            "round_of_16",
            "quarterfinal",
            "semifinal",
            "challenger_final",
            "baseline_final",
        )
        for spec in plan["matchups"][key]
    ]
    assert all(spec["limits"] == decisive_limits for spec in decisive_specs)
    assert all(spec["base_games"] >= 100 for spec in decisive_specs)
    assert plan["matchups"]["baseline_final"][0]["base_games"] == 400
    assert plan["strength_contract"]["group_screen"]["can_promote"] is False
    assert plan["strength_contract"]["decisive_matches"]["base_games"] == 1_900
    assert plan["strength_contract"]["decisive_matches"]["expanded_games"] == 2_600
    assert plan["strength_contract"]["variant_search_advantage"] is False


def test_expansion_decision_is_result_blind_and_content_addressed() -> None:
    slow_timing = [
        {
            "stage": "group",
            "matchup_id": f"group-calibration-{index:02d}",
            "ordinal": index,
            "pair_records": 50,
            "selected_game_records": 100,
            "executed_game_records": 100,
            "execution_wall_seconds": 80.0,
        }
        for index in range(10)
    ]
    conservative = choose_result_blind_expansion(
        protocol_digest="34" * 32,
        calibration_timing_evidence=slow_timing,
        fixed_overhead_reserve_seconds=1_800,
    )
    repeated = choose_result_blind_expansion(
        protocol_digest="34" * 32,
        calibration_timing_evidence=slow_timing,
        fixed_overhead_reserve_seconds=1_800,
    )
    assert conservative == repeated
    assert conservative["result_fields_consumed"] == []
    assert conservative["schedule"] == "base"
    assert conservative["selected_group_game_records"] == 1_000
    assert conservative["executed_group_game_records"] == 1_000
    assert conservative["calibration_pair_records"] == 500
    assert conservative["remaining_depth2_screening_game_records"] == 21_400
    assert conservative["expanded_depth3_decisive_games"] == 2_600
    assert conservative["decisive_depth3_timing_multiplier"] == 20
    fast = choose_result_blind_expansion(
        protocol_digest="34" * 32,
        calibration_timing_evidence=[
            {**row, "execution_wall_seconds": 20.0} for row in slow_timing
        ],
        fixed_overhead_reserve_seconds=1_800,
    )
    assert fast["schedule"] == "expanded"


def test_summary_excludes_incompletes_and_keeps_quarter_scores() -> None:
    report = _report(
        "spc-a",
        "spc-b",
        ["win"] * 20
        + ["three-quarter"] * 10
        + ["draw"] * 10
        + ["loss"] * 9
        + ["incomplete"],
    )
    summary = summarize_match_report(report, expected_pairs=50)
    first = summary["profiles"]["spc-a"]
    assert first["pair_wdl"] == {
        "wins": 30,
        "draws": 10,
        "losses": 9,
    }
    assert first["completed_pairs"] == 49
    assert first["incomplete_pairs"] == 1
    assert first["pair_score_quarter_units"].count(3) == 10
    assert first["completed_games"] == 98
    assert first["incomplete_games"] == 2
    assert first["color_wdl"]["white"]["wins"] == 30
    assert first["color_wdl"]["black"]["wins"] == 20


def test_full_trace_envelope_replays_prefix_through_true_terminal() -> None:
    prefix = HUMAN_REFUTATION_TRACE[:2]
    continuation = HUMAN_REFUTATION_TRACE[2:]
    state = ProgressiveState.initial()
    for moves in prefix:
        state = play_series(state, moves).final_state
    start_pfen = state.pfen
    trace = []
    final_result = None
    for moves in continuation:
        final_result = play_series(state, moves)
        trace.append({"played": True, "series": "/".join(moves)})
        state = final_result.final_state
    assert final_result is not None and final_result.is_terminal
    report = {
        "opening_suite": {
            "histories": [
                {
                    "case_id": "human-refutation-s3",
                    "target_series": 3,
                    "series": [list(moves) for moves in prefix],
                }
            ]
        },
        "games": [
            {
                "opening_case_id": "human-refutation-s3",
                "start_pfen": start_pfen,
                "final_pfen": state.pfen,
                "result": "1-0",
                "terminal_reason": "checkmate",
                "trace": trace,
            }
        ],
    }
    wrapped = attach_replay_verified_full_traces(report)
    assert wrapped["full_trace_evidence"]["completed_games"] == 1
    assert len(wrapped["games"][0]["full_trace"]["all_series"]) == 5
    assert wrapped["games"][0]["full_trace"]["terminal_status"] == "true-terminal"


def test_full_trace_rejects_fabricated_no_material_draw() -> None:
    prefix = HUMAN_REFUTATION_TRACE[:2]
    state = ProgressiveState.initial()
    for moves in prefix:
        state = play_series(state, moves).final_state
    assert state.board.is_insufficient_material() is False
    report = {
        "opening_suite": {
            "histories": [
                {
                    "case_id": "fake-no-material-s3",
                    "target_series": 3,
                    "series": [list(moves) for moves in prefix],
                }
            ]
        },
        "games": [
            {
                "opening_case_id": "fake-no-material-s3",
                "start_pfen": state.pfen,
                "final_pfen": state.pfen,
                "result": "1/2-1/2",
                "terminal_reason": "proven-draw-no-mating-material",
                "trace": [],
            }
        ],
    }
    with pytest.raises(ValueError, match="no mating material"):
        attach_replay_verified_full_traces(report)


def test_seeded_suite_rejects_relabelled_seed_and_content() -> None:
    suite = build_seeded_opening_suite(
        seed=1,
        count=2,
        min_series=3,
        max_series=3,
        max_frontier_states=8,
    )
    relabelled = suite.as_dict()
    relabelled["seed"] = 2
    with pytest.raises(ValueError, match="version does not match"):
        seeded_opening_suite_from_dict(relabelled)


def test_knockout_tie_is_administrative_not_match_win() -> None:
    report = _report("spc-a", "spc-b", ["draw"] * 50)
    result = select_knockout_winner(
        report,
        preregistered_seed_order=("spc-b", "spc-a"),
    )
    assert result["winner_profile_id"] == "spc-b"
    assert result["status"] == "administrative-tie-advance"
    assert result["promotion_effect"] == "none"


def test_group_rejects_raw_reports_and_incompletes_award_no_points() -> None:
    stream = PopulationStream(baseline_profile())
    survivors = tuple(
        RankedCandidate(
            item.candidate_index,
            item.effective_id,
            item.profile.profile_id,
            index,
        )
        for index, item in enumerate(stream.iter_range(0, 64))
    )
    plan = build_tournament_plan(
        survivors,
        baseline_profile(),
        protocol_digest=PROTOCOL_DIGEST,
        promotion_batch=_promotion_batch(PROTOCOL_DIGEST),
    )
    members = plan["groups"]["group-01"]
    profile_ids = tuple(member["profile_id"] for member in members)
    effective_by_profile_id = {
        member["profile_id"]: member["effective_id"] for member in members
    }
    profile_by_effective_id = {
        effective_id: profile_id
        for profile_id, effective_id in effective_by_profile_id.items()
    }
    match_specs = tuple(
        spec
        for spec in plan["matchups"]["group"]
        if spec["opening_domain"] == "group-01-openings"
    )
    raw_reports = [
        _report(first, second, ["incomplete"] * 50)
        for index, first in enumerate(profile_ids)
        for second in profile_ids[index + 1 :]
    ]
    with pytest.raises(ValueError, match="digest mismatch"):
        rank_group(
            "group-01",
            profile_ids,
            raw_reports,
            protocol_digest=PROTOCOL_DIGEST,
            tournament_plan_digest=plan["tournament_plan_digest"],
            match_specs=match_specs,
            effective_by_profile_id=effective_by_profile_id,
        )
    suite = build_seeded_opening_suite(
        seed=match_specs[0]["opening_seed"],
        count=50,
        min_series=3,
        max_series=6,
        max_frontier_states=32,
    )
    suite_digest = canonical_digest(
        "spc-tournament-opening-suite-v1\0", suite.as_dict()
    )
    bound_match_specs = tuple(
        {**dict(spec), "opening_suite_digest": suite_digest}
        for spec in match_specs
    )
    with pytest.raises(ValueError, match="opening attempt manifest"):
        _bound_incomplete_report(
            profile_by_effective_id[bound_match_specs[0]["first_slot"]],
            profile_by_effective_id[bound_match_specs[0]["second_slot"]],
            match_spec=bound_match_specs[0],
            suite=suite,
            protocol_digest=PROTOCOL_DIGEST,
            tournament_plan_digest=plan["tournament_plan_digest"],
            effective_by_profile_id=effective_by_profile_id,
        )


def test_exact_sign_flip_and_synthetic_promotion_report_is_rejected() -> None:
    stream = PopulationStream(baseline_profile())
    member = stream.member(0)
    candidate = RankedCandidate(
        member.candidate_index,
        member.effective_id,
        member.profile.profile_id,
        0,
    )
    report = _report(
        candidate.profile_id,
        baseline_profile().profile_id,
        ["win"] * 200,
    )
    survivors = tuple(
        RankedCandidate(
            item.candidate_index,
            item.effective_id,
            item.profile.profile_id,
            index,
        )
        for index, item in enumerate(stream.iter_range(0, 64))
    )
    plan = build_tournament_plan(
        survivors,
        baseline_profile(),
        protocol_digest=PROTOCOL_DIGEST,
        promotion_batch=_promotion_batch(PROTOCOL_DIGEST),
    )
    base_spec = plan["matchups"]["baseline_final"][0]
    match_spec = {
        **base_spec,
        "resolved_first_effective_id": candidate.effective_id,
        "resolved_second_effective_id": effective_profile_id(
            baseline_profile().weights
        ),
    }
    challenger_spec = {
        **plan["matchups"]["challenger_final"][0],
        "resolved_first_effective_id": candidate.effective_id,
        "resolved_second_effective_id": survivors[1].effective_id,
    }
    challenger_report = _report(
        candidate.profile_id, survivors[1].profile_id, ["win"] * 50
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        baseline_promotion_decision(
            report,
            _bundle(candidate),
            stamp_human_gate_artifact(
                candidate,
                protocol_digest=PROTOCOL_DIGEST,
                native_source_identity=NATIVE_IDENTITY,
                runtime_identity_digest=RUNTIME_IDENTITY_DIGEST,
                human_refutation_gate=_human_gate(
                    candidate.profile_id,
                    depth=3,
                    max_work=5_000_000,
                ),
                depth=3,
                max_work=5_000_000,
            ),
            candidate=candidate,
            baseline_profile_id=baseline_profile().profile_id,
            baseline_effective_id=effective_profile_id(
                baseline_profile().weights
            ),
            protocol_digest=PROTOCOL_DIGEST,
            tournament_plan_digest=plan["tournament_plan_digest"],
            match_spec=match_spec,
            challenger_final_report=challenger_report,
            challenger_final_match_spec=challenger_spec,
            challenger_final_effective_by_profile_id={
                candidate.profile_id: candidate.effective_id,
                survivors[1].profile_id: survivors[1].effective_id,
            },
            challenger_final_seed_order=(
                candidate.profile_id,
                survivors[1].profile_id,
            ),
            native_source_identity=NATIVE_IDENTITY,
            runtime_identity_digest=RUNTIME_IDENTITY_DIGEST,
        )
    exact = exact_sign_flip_p_value([4] * 200)
    assert exact["numerator"] == "1"
    assert exact["denominator"] == str(1 << 200)


def test_stale_report_and_incomplete_points_are_rejected() -> None:
    report = _report("spc-a", "spc-b", ["draw"] * 50)
    report["engine"] = {"source_fingerprint": "stale"}
    with pytest.raises(ValueError, match="stale"):
        summarize_match_report(report)
    report = _report(
        "spc-a",
        "spc-b",
        ["draw"] * 49 + ["incomplete"],
    )
    report["pairs"][-1]["candidate_points"] = 0.0
    with pytest.raises(ValueError, match="cannot carry points"):
        summarize_match_report(report)


def test_seed64_is_stable_and_domain_separated() -> None:
    assert seed64("population-order") == seed64("population-order")
    assert seed64("population-order") != seed64("group-assignment")


def test_run_checkpoint_accepts_only_complete_color_swapped_pairs() -> None:
    protocol = "56" * 32
    stream = PopulationStream(baseline_profile())
    survivors = tuple(
        RankedCandidate(
            item.candidate_index,
            item.effective_id,
            item.profile.profile_id,
            index,
        )
        for index, item in enumerate(stream.iter_range(0, 64))
    )
    plan = build_tournament_plan(
        survivors,
        baseline_profile(),
        protocol_digest=protocol,
        promotion_batch=_promotion_batch(protocol),
    )
    match_spec = plan["matchups"]["group"][0]
    first = match_spec["first_slot"]
    second = match_spec["second_slot"]
    case_id = "case-3"
    common = {
        "protocol_digest": protocol,
        "stage": match_spec["stage"],
        "matchup_id": match_spec["matchup_id"],
        "opening_boundary_digest": "78" * 32,
        "pair_index": 3,
        "pair_seed": match_pair_seed(
            match_spec["stage"], match_spec["matchup_id"], 3, case_id
        ),
    }
    job_keys = [
        tournament_job_key(
            **common,
            white_effective_id=white,
            black_effective_id=black,
        )
        for white, black in ((first, second), (second, first))
    ]
    pair = {
        "stage": common["stage"],
        "matchup_id": common["matchup_id"],
        "pair_index": common["pair_index"],
        "opening_case_id": case_id,
        "opening_boundary_digest": common["opening_boundary_digest"],
        "pair_seed": common["pair_seed"],
        "first_effective_id": first,
        "second_effective_id": second,
        "game_job_keys": job_keys,
        "game_record_digests": ["ab" * 32, "bc" * 32],
        "bound_report_digest": "cd" * 32,
        "resolved_match_spec": {
            **match_spec,
            "resolved_first_effective_id": first,
            "resolved_second_effective_id": second,
        },
    }
    checkpoint = build_tournament_run_checkpoint(
        tournament_plan=plan,
        completed_pairs=(pair,),
        expansion_decision_digest=None,
    )
    assert len(checkpoint["completed_game_job_keys"]) == 2
    assert checkpoint["audit_unsealed"] is False
    assert checkpoint["winner_ids"] == {}
    with pytest.raises(TypeError, match="winner_ids"):
        build_tournament_run_checkpoint(
            tournament_plan=plan,
            completed_pairs=(),
            winner_ids={"challenger-final-winner": first},
            expansion_decision_digest=None,
        )
    pair["game_job_keys"] = job_keys[:1]
    with pytest.raises(ValueError, match="both exact color-swap"):
        build_tournament_run_checkpoint(
            tournament_plan=plan,
            completed_pairs=(pair,),
            expansion_decision_digest=None,
        )
