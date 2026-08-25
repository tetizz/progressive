from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

import chess

from scottish_progressive.league import GameRecord, OpeningCase, runtime_provenance
from scottish_progressive.fast_training import CachedFeatures
from scottish_progressive.model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    Outcome,
    ProgressiveState,
)
from scottish_progressive.profiles import EngineProfile, baseline_profile
from scottish_progressive.selfplay_training import (
    FULLGAME_CORPUS_METHOD,
    SelfPlayCorpus,
    SelfPlaySample,
)
from scottish_progressive.strength import (
    SeededOpeningHistory,
    SeededOpeningSuite,
    STRENGTH_REPORT_FORMAT,
    StrengthMatchConfig,
    _build_jobs,
    _game_payload,
    _seeded_suite_version,
    _summarize,
    build_seeded_opening_suite,
    compose_seeded_opening_suite,
    subset_seeded_opening_suite,
)
from scottish_progressive.rules import SeriesLegalityError, play_series
from scottish_progressive.tournament import (
    FROZEN_EXPANSION_OVERHEAD_RESERVE_SECONDS,
    PopulationStream,
    RankedCandidate,
    bind_frozen_match_report,
    build_tournament_plan,
    canonical_digest,
    canonical_json_bytes,
    choose_result_blind_expansion,
    effective_profile_id,
    make_promotion_batch_artifact,
    make_tournament_authority_artifact,
    opening_reserve_digest,
    tournament_opening_retry_policy,
    tournament_limits_for_stage,
    _validate_trusted_tournament_database,
)
from scottish_progressive.tournament_runner import (
    OpeningReplacementExhaustedError,
    TournamentOpeningReserve,
    TournamentRunner,
    abandon_promotion_batch,
    build_corpus_exclusion_artifact,
    freeze_opening_suites_before_batch,
    reserve_promotion_batch,
    tournament_schedule_summary,
    validate_corpus_exclusion_artifact,
)
import scottish_progressive.tournament_runner as runner_module


PROTOCOL_DIGEST = "71" * 32
_FAST_MATE_SERIES = ("a2a3", "d1h5", "h3g5", "g5f7", "f7d6")


def _calibration_timing(seconds_per_matchup: float = 10.0) -> list[dict[str, Any]]:
    return [
        {
            "stage": "group",
            "matchup_id": f"group-calibration-{index:02d}",
            "ordinal": index,
            "pair_records": 50,
            "selected_game_records": 100,
            "executed_game_records": 100,
            "execution_wall_seconds": seconds_per_matchup,
        }
        for index in range(10)
    ]


def _corpus_snapshot() -> SelfPlayCorpus:
    state = ProgressiveState.initial()
    sample = SelfPlaySample(
        position_hash=state.position_hash,
        pfen=state.pfen,
        run_id="runner-test",
        game_key="runner-test-game",
        opening_case_id="runner-test-opening",
        line_family="runner-test-family",
        split_component="runner-test-component",
        split="train",
        series_number=state.series_number,
        mover="white",
        profile_id=baseline_profile().profile_id,
        chosen_series="e2e4",
        result="1/2-1/2",
        target_white_score=0.5,
        sample_weight=1.0,
        features=CachedFeatures.from_state(state),
    )
    return SelfPlayCorpus(
        seed=1,
        holdout_percent=0,
        database_evidence=(
            {
                "source_kind": "verified-fullgame-store-snapshot",
                "manifest_sha256": "ab" * 32,
            },
        ),
        completed_games=1,
        excluded_games=0,
        samples=(sample,),
        method=FULLGAME_CORPUS_METHOD,
    )


def _plan_and_profiles(
    directory: Path | None = None,
) -> tuple[dict[str, Any], dict[str, EngineProfile]]:
    baseline = baseline_profile()
    stream = PopulationStream(baseline)
    members = tuple(stream.iter_range(0, 64))
    survivors = tuple(
        RankedCandidate(
            member.candidate_index,
            member.effective_id,
            member.profile.profile_id,
            index,
        )
        for index, member in enumerate(members)
    )
    baseline_effective_id = effective_profile_id(baseline.weights)
    if directory is None:
        promotion_batch = make_promotion_batch_artifact(
            registry_id="72" * 32,
            reservation_key="runner-plan-only",
            batch_index=1,
            protocol_digest=PROTOCOL_DIGEST,
            baseline_effective_id=baseline_effective_id,
            predecessor_chain_digest="00" * 32,
        )
    else:
        promotion_batch = reserve_promotion_batch(
            directory / "promotion-registry.sqlite3",
            reservation_key="runner-batch-1",
            protocol_digest=PROTOCOL_DIGEST,
            baseline_effective_id=baseline_effective_id,
        )
    plan = build_tournament_plan(
        survivors,
        baseline,
        protocol_digest=PROTOCOL_DIGEST,
        promotion_batch=promotion_batch,
    )
    profiles = {member.effective_id: member.profile for member in members}
    profiles[effective_profile_id(baseline.weights)] = baseline
    return plan, profiles


def _stub_report(
    candidate: EngineProfile,
    reference: EngineProfile,
    *,
    config,
    opening_cases,
    requested_workers: int,
    incomplete_pairs: set[int] | None = None,
    incomplete_terminal_reason: str | None = None,
    attribute_incomplete_to_candidate: bool = False,
    incomplete_error: str | None = None,
    attribute_incomplete_as_decisive: bool = False,
    incomplete_series_played: int | None = None,
    marker: str = "first",
) -> dict[str, Any]:
    assert requested_workers == 16
    incomplete = incomplete_pairs or set()
    jobs = _build_jobs(candidate, reference, config, opening_cases)
    records: list[GameRecord] = []
    for job in jobs:
        state = job.opening.state()
        pair_index = job.opening_index // 2
        is_incomplete = pair_index in incomplete
        replayed_terminal = None
        if not is_incomplete:
            try:
                candidate_terminal = play_series(state, _FAST_MATE_SERIES)
            except SeriesLegalityError:
                pass
            else:
                if candidate_terminal.outcome == Outcome.CHECKMATE:
                    replayed_terminal = candidate_terminal
        records.append(
            GameRecord(
                job_key=job.job_key,
                run_id=job.run_id,
                generation=job.generation,
                stage=job.stage,
                opening_index=job.opening_index,
                opening_case_id=job.opening.case_id,
                opening_suite_version=job.opening_suite_version,
                seed=job.seed,
                white_profile_id=job.white_profile.profile_id,
                black_profile_id=job.black_profile.profile_id,
                result=(
                    "*"
                    if is_incomplete
                    else "1-0"
                    if replayed_terminal is not None
                    else "1/2-1/2"
                ),
                terminal_reason=(
                    incomplete_terminal_reason
                    or "manual-adjudication-pending"
                    if is_incomplete
                    else "checkmate"
                    if replayed_terminal is not None
                    else f"proven-draw-no-mating-material"
                ),
                decisive_profile_id=(
                    candidate.profile_id
                    if is_incomplete and attribute_incomplete_as_decisive
                    else job.white_profile.profile_id
                    if replayed_terminal is not None
                    else None
                ),
                engine_failure_profile_id=(
                    candidate.profile_id
                    if is_incomplete and attribute_incomplete_to_candidate
                    else None
                ),
                start_pfen=state.pfen,
                final_pfen=(
                    replayed_terminal.final_state.pfen
                    if replayed_terminal is not None
                    else state.pfen
                ),
                series_played=(
                    incomplete_series_played
                    if is_incomplete and incomplete_series_played is not None
                    else 1
                    if replayed_terminal is not None
                    else 0
                ),
                trace=(
                    (
                        {
                            "series_number": state.series_number,
                            "series": "/".join(_FAST_MATE_SERIES),
                            "played": True,
                        },
                    )
                    if replayed_terminal is not None
                    else ()
                ),
                error=incomplete_error if is_incomplete else None,
            )
        )
    summary, pairs = _summarize(records, candidate, reference)
    opening_by_id = {job.opening.case_id: job.opening for job in jobs}
    return {
        "format": STRENGTH_REPORT_FORMAT,
        "report_id": jobs[0].run_id,
        "created_at": "ignored-operational-time",
        "engine": {
            "version": ENGINE_VERSION,
            "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
            "runtime": runtime_provenance(),
        },
        "candidate": candidate.as_dict(),
        "reference": reference.as_dict(),
        "config": config.as_dict(),
        "opening_suite": opening_cases.as_dict(),
        "resources": {
            "workers": min(requested_workers, len(jobs)),
            "logical_cpus": 999,
            "available_memory_mb": 999_999,
        },
        "execution": {
            "wall_elapsed_seconds": 123.456,
            "completed_games_per_second": 9.87,
            "result_order": "opening-pair-then-color-swap",
        },
        "selected_openings": [
            opening_by_id[job.opening.case_id].as_dict() for job in jobs[::2]
        ],
        "summary": summary,
        "pairs": list(pairs),
        "games": [
            _game_payload(record, opening_by_id[record.opening_case_id])
            for record in records
        ],
        "claim_scope": {
            "fixed_suite_only": True,
            "statement": (
                "Results apply only to these versioned Scottish Progressive "
                "boundaries and exact deterministic search limits."
            ),
            "promotion_effect": "none; this harness never changes the champion",
            "stockfish_comparison": (
                "This report does not establish Stockfish-level strength. Orthodox "
                "Stockfish is not a Scottish Progressive rules engine and was not "
                "a participant."
            ),
        },
    }


class StubRunner:
    def __init__(
        self,
        *,
        fail_on_call: int | None = None,
        incomplete_pairs: set[int] | None = None,
        incomplete_terminal_reason: str | None = None,
        attribute_incomplete_to_candidate: bool = False,
        incomplete_error: str | None = None,
        attribute_incomplete_as_decisive: bool = False,
        incomplete_series_played: int | None = None,
        incomplete_pairs_by_call: list[set[int]] | None = None,
        marker: str = "first",
    ) -> None:
        self.fail_on_call = fail_on_call
        self.incomplete_pairs = incomplete_pairs
        self.incomplete_terminal_reason = incomplete_terminal_reason
        self.attribute_incomplete_to_candidate = (
            attribute_incomplete_to_candidate
        )
        self.incomplete_error = incomplete_error
        self.attribute_incomplete_as_decisive = (
            attribute_incomplete_as_decisive
        )
        self.incomplete_series_played = incomplete_series_played
        self.incomplete_pairs_by_call = incomplete_pairs_by_call
        self.marker = marker
        self.calls: list[tuple[str, str]] = []

    def __call__(self, candidate, reference, **kwargs):
        self.calls.append((candidate.profile_id, reference.profile_id))
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise RuntimeError("simulated runner crash")
        return _stub_report(
            candidate,
            reference,
            incomplete_pairs=(
                self.incomplete_pairs_by_call[len(self.calls) - 1]
                if self.incomplete_pairs_by_call is not None
                and len(self.calls) <= len(self.incomplete_pairs_by_call)
                else self.incomplete_pairs
            ),
            incomplete_terminal_reason=self.incomplete_terminal_reason,
            attribute_incomplete_to_candidate=(
                self.attribute_incomplete_to_candidate
            ),
            incomplete_error=self.incomplete_error,
            attribute_incomplete_as_decisive=(
                self.attribute_incomplete_as_decisive
            ),
            incomplete_series_played=self.incomplete_series_played,
            marker=self.marker,
            **kwargs,
        )


def _open(
    path: Path,
    plan: dict[str, Any],
    profiles: dict[str, EngineProfile],
    *,
    schedule: str = "base",
) -> TournamentRunner:
    return TournamentRunner(
        path,
        tournament_plan=plan,
        profiles_by_effective_id=profiles,
        schedule=schedule,
        _test_corpus_snapshots=(_corpus_snapshot(),),
        _allow_test_corpus=True,
        promotion_registry_path=path.parent / "promotion-registry.sqlite3",
        _allow_test_registry=True,
        require_native=False,
    )


_FAST_ATTEMPT_LANES = None


def _fast_attempt_lanes():
    global _FAST_ATTEMPT_LANES
    if _FAST_ATTEMPT_LANES is None:
        # Build 150 disjoint, replayable Series-5 boundaries that all retain a
        # known one-series checkmate.  The production sampler intentionally
        # performs more expensive neutral frontier sampling; this focused
        # fixture keeps the real 50-pair/100-game report and full-trace shape
        # while avoiding minutes of unrelated materialization work.
        boundaries: list[tuple[ProgressiveState, tuple[tuple[str, ...], ...]]] = []
        seen_hashes: set[str] = set()
        initial = ProgressiveState.initial()
        fixed_prefix = (
            ("e2e4",),
            ("g8f6", "f6d5"),
            ("g1h3", "g2g3", "d2d4"),
        )
        boundary_root = initial
        for moves in fixed_prefix:
            boundary_root = play_series(boundary_root, moves).final_state
        mover = boundary_root.board.turn

        def collect_black_series(
            board: chess.Board, moves: tuple[str, ...]
        ) -> None:
            if len(boundaries) == 150:
                return
            if len(moves) == boundary_root.series_number:
                try:
                    result = play_series(boundary_root, moves)
                except SeriesLegalityError:
                    return
                state = result.final_state
                if result.is_terminal or state.position_hash in seen_hashes:
                    return
                try:
                    mate = play_series(state, _FAST_MATE_SERIES)
                except SeriesLegalityError:
                    return
                if mate.outcome != Outcome.CHECKMATE:
                    return
                seen_hashes.add(state.position_hash)
                boundaries.append((state, (*fixed_prefix, moves)))
                return
            for move in sorted(board.legal_moves, key=lambda item: item.uci()):
                child = board.copy(stack=False)
                child.push(move)
                if child.is_check():
                    continue
                child.turn = mover
                child.ep_square = None
                collect_black_series(child, (*moves, move.uci()))
                if len(boundaries) == 150:
                    return

        collect_black_series(boundary_root.board.copy(stack=False), ())
        assert len(boundaries) == 150

        lanes = []
        for attempt_index in range(3):
            seed = 90_000 + attempt_index
            cases = []
            histories = []
            for logical_index, (state, series) in enumerate(
                boundaries[attempt_index * 50 : (attempt_index + 1) * 50]
            ):
                case_id = (
                    f"fast-lane-{attempt_index}-pair-{logical_index:02d}-"
                    f"{state.position_hash[:12]}"
                )
                cases.append(
                    OpeningCase(
                        case_id=case_id,
                        fen=state.board.fen(en_passant="fen"),
                        series_number=state.series_number,
                        quiet_series=state.quiet_series,
                        ep_targets=tuple(
                            chess.square_name(square) for square in state.ep_targets
                        ),
                        source="fast deterministic retry integration fixture",
                    )
                )
                histories.append(
                    SeededOpeningHistory(
                        case_id=case_id,
                        target_series=5,
                        attempt=logical_index,
                        series=series,
                    )
                )
            version = _seeded_suite_version(
                seed=seed,
                min_series=5,
                max_series=5,
                max_frontier_states=32,
                cases=cases,
                histories=histories,
            )
            lanes.append(
                SeededOpeningSuite(
                    version=version,
                    seed=seed,
                    min_series=5,
                    max_series=5,
                    max_frontier_states=32,
                    cases=tuple(cases),
                    histories=tuple(histories),
                )
            )
        _FAST_ATTEMPT_LANES = tuple(lanes)
    return _FAST_ATTEMPT_LANES


def _open_fast(
    path: Path,
    plan: dict[str, Any],
    profiles: dict[str, EngineProfile],
    *,
    schedule: str = "base",
) -> TournamentRunner:
    lanes = _fast_attempt_lanes()
    test_runner = _open(path, plan, profiles, schedule=schedule)
    test_runner._opening_suites_ready = {
        str(item["domain"]): TournamentOpeningReserve(
            domain=str(item["domain"]),
            count=50,
            lanes=lanes,
            reserve_digest=canonical_digest(
                "spc-fast-test-opening-reserve-v1\0",
                {"domain": item["domain"]},
            ),
            position_hash_digest=canonical_digest(
                "spc-fast-test-opening-positions-v1\0",
                [
                    case.state().position_hash
                    for lane in lanes
                    for case in lane.cases
                ],
            ),
        )
        for item in test_runner.plan["opening_suites"]
    }
    return test_runner


def test_schedule_totals_are_exact_and_never_result_dependent() -> None:
    plan, _ = _plan_and_profiles()
    assert tournament_schedule_summary(plan, schedule="base") == {
        "schedule": "base",
        "matchups": 240,
        "pairs": 12_150,
        "games": 24_300,
    }
    assert tournament_schedule_summary(plan, schedule="expanded") == {
        "schedule": "expanded",
        "matchups": 240,
        "pairs": 12_500,
        "games": 25_000,
    }


def test_frozen_plan_binds_every_match_to_its_three_lane_reserve() -> None:
    legacy, _profiles = _plan_and_profiles()
    survivors = tuple(
        RankedCandidate.from_dict(item)
        for item in legacy["survivors_in_validation_rank_order"]
    )
    frozen = []
    for index, item in enumerate(legacy["opening_suites"], 1):
        lanes = []
        for attempt_index, raw_lane in enumerate(item["attempt_lanes"]):
            seed = int(raw_lane["seed"])
            lanes.append(
                {
                    **raw_lane,
                    "selection_domain": (
                        f"{item['domain']}|attempt|{attempt_index}"
                    ),
                    "selection_master_seed": 20_260_840,
                    "base_seed": seed,
                    "selection_nonce": 0,
                    "suite": {
                        "fixture": item["domain"],
                        "attempt_index": attempt_index,
                        "seed": seed,
                    },
                    "suite_digest": f"{index * 10 + attempt_index:064x}",
                    "position_hash_digest": (
                        f"{index * 10 + attempt_index + 100:064x}"
                    ),
                }
            )
        reserve = {
            **item,
            "format": "spc-tournament-opening-reserve-v1",
            "attempt_lanes": lanes,
            "total_case_count": int(item["count"]) * 3,
            "position_hash_digest": f"{index + 500:064x}",
        }
        reserve["reserve_digest"] = opening_reserve_digest(reserve)
        frozen.append(reserve)
    plan = build_tournament_plan(
        survivors,
        baseline_profile(),
        protocol_digest=PROTOCOL_DIGEST,
        promotion_batch=legacy["promotion_batch"],
        frozen_opening_suites=frozen,
    )
    digest_by_domain = {
        item["domain"]: item["reserve_digest"] for item in frozen
    }
    for specs in plan["matchups"].values():
        for spec in specs:
            assert spec["opening_reserve_digest"] == digest_by_domain[
                spec["opening_domain"]
            ]
            assert spec["opening_retry_policy"] == tournament_opening_retry_policy()
    runner_module._validate_plan(plan)
    invalid = copy.deepcopy(frozen)
    invalid[0]["attempt_lanes"][0]["selection_nonce"] = 65_536
    invalid[0]["reserve_digest"] = opening_reserve_digest(invalid[0])
    with pytest.raises(ValueError, match="selection is incomplete"):
        build_tournament_plan(
            survivors,
            baseline_profile(),
            protocol_digest=PROTOCOL_DIGEST,
            promotion_batch=legacy["promotion_batch"],
            frozen_opening_suites=invalid,
        )


def test_corpus_exclusions_are_derived_from_verified_snapshot_objects() -> None:
    with pytest.raises(TypeError, match="verified full-game store specs"):
        build_corpus_exclusion_artifact((_corpus_snapshot(),))  # type: ignore[arg-type]
    artifact = runner_module._build_corpus_exclusion_artifact_from_corpora(
        (_corpus_snapshot(),), authority="test-only-synthetic-fixture"
    )
    hashes = validate_corpus_exclusion_artifact(artifact)
    assert hashes == (ProgressiveState.initial().position_hash,)
    forged = copy.deepcopy(artifact)
    forged["corpora"][0]["method"] = "invented-corpus"
    deterministic = {
        key: value for key, value in forged.items() if key != "artifact_digest"
    }
    forged["artifact_digest"] = canonical_digest(
        "spc-tournament-corpus-exclusion-v1\0", deterministic
    )
    with pytest.raises(ValueError, match="snapshot identity"):
        validate_corpus_exclusion_artifact(forged)


def test_production_runner_rejects_synthetic_corpus_injection(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    with pytest.raises(TypeError, match="verified full-game store specs"):
        TournamentRunner(
            tmp_path / "synthetic-corpus.sqlite3",
            tournament_plan=plan,
            profiles_by_effective_id=profiles,
            schedule="base",
            corpus_snapshots=(_corpus_snapshot(),),  # type: ignore[arg-type]
            promotion_registry_path=tmp_path / "promotion-registry.sqlite3",
            _allow_test_registry=True,
            require_native=False,
        )


def test_production_runner_rejects_fresh_alternate_promotion_registry(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    with pytest.raises(ValueError, match="trusted project registry"):
        TournamentRunner(
            tmp_path / "reset-alpha.sqlite3",
            tournament_plan=plan,
            profiles_by_effective_id=profiles,
            schedule="base",
            _test_corpus_snapshots=(_corpus_snapshot(),),
            _allow_test_corpus=True,
            promotion_registry_path=tmp_path / "promotion-registry.sqlite3",
            require_native=False,
        )


def test_report_normalization_derives_summary_and_rejects_forged_metadata(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    spec = plan["matchups"]["group"][0]
    with _open(tmp_path / "canonical-report.sqlite3", plan, profiles) as runner:
        job = runner.build_match_job(spec, ordinal=0)
        assert job.config.search_depth == 2
        assert job.config.max_generation_positions == 250_000
        assert job.config.max_game_work_positions == 5_000_000
        raw = _stub_report(
            job.first_profile,
            job.second_profile,
            config=job.config,
            opening_cases=job.opening_suite,
            requested_workers=16,
            incomplete_pairs=set(range(50)),
        )
        expected_summary = copy.deepcopy(raw["summary"])
        stale = copy.deepcopy(raw)
        stale["summary"] = {"completed_pairs": 50, "incomplete_pairs": 0}
        assert runner._normalize_raw_report(stale, job)["summary"] == expected_summary

        for label, mutate in (
            ("report id", lambda payload: payload.__setitem__("report_id", "forged")),
            (
                "game stage",
                lambda payload: payload["games"][0].__setitem__("stage", "forged"),
            ),
            (
                "game generation",
                lambda payload: payload["games"][0].__setitem__("generation", 99),
            ),
            (
                "game opening",
                lambda payload: payload["games"][0].__setitem__("opening", {}),
            ),
            (
                "pair payload",
                lambda payload: payload["pairs"][0].__setitem__(
                    "game_job_keys", ["forged-a", "forged-b"]
                ),
            ),
            (
                "pair payload",
                lambda payload: payload["pairs"][0].__setitem__(
                    "technical_failures",
                    [{"profile_id": None, "reason": "forged"}],
                ),
            ),
            (
                "claim scope",
                lambda payload: payload.__setitem__(
                    "claim_scope", {"promotion_effect": "forged"}
                ),
            ),
        ):
            forged = copy.deepcopy(raw)
            mutate(forged)
            with pytest.raises(ValueError, match=label):
                runner._normalize_raw_report(forged, job)


def test_decisive_jobs_use_identical_depth_three_limits_from_the_plan(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    spec = plan["matchups"]["round_of_16"][0]
    first = plan["groups"]["group-01"][0]["effective_id"]
    second = plan["groups"]["group-08"][1]["effective_id"]
    with _open(tmp_path / "depth-three.sqlite3", plan, profiles) as runner:
        runner._put_artifact(
            kind="test-slot-fixture",
            key="depth-three-slots",
            payload={"promotion_effect": "none"},
            slot_updates={
                spec["first_slot"]: first,
                spec["second_slot"]: second,
            },
        )
        job = runner.build_match_job(spec, ordinal=224)
    assert job.config.search_depth == 3
    assert job.config.max_series_per_node == 32
    assert job.config.max_generation_positions == 5_000_000
    assert job.config.max_game_work_positions == 100_000_000
    assert job.config.emergency_max_series is None
    assert spec["limits"]["identical_limits_for_both_colors"] is True


def test_one_vs_many_stub_jobs_and_results_use_plan_order(tmp_path: Path) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    specs = plan["matchups"]["group"][:3]
    reversed_specs = list(reversed(specs))
    stub = StubRunner(incomplete_pairs=set(range(50)))
    with _open(tmp_path / "order.sqlite3", plan, profiles) as runner:
        reports = runner.run_matchups(reversed_specs, match_runner=stub)
        assert runner.persisted_match_count() == 3
    assert [
        report["tournament_binding"]["matchup_id"] for report in reports
    ] == [spec["matchup_id"] for spec in specs]
    expected_calls = [
        (
            profiles[spec["first_slot"]].profile_id,
            profiles[spec["second_slot"]].profile_id,
        )
        for spec in specs
    ]
    assert stub.calls == expected_calls
    assert all(len(report["games"]) == 100 for report in reports)
    assert all(len(report["pairs"]) == 50 for report in reports)
    assert all(
        report["resources"] == {"workers": 16}
        and report["execution"]
        == {"result_order": "opening-pair-then-color-swap"}
        for report in reports
    )


def test_crash_resume_is_idempotent_and_matches_uninterrupted_state(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    specs = plan["matchups"]["group"][:3]
    database = tmp_path / "resume.sqlite3"
    crashing = StubRunner(
        fail_on_call=3, incomplete_pairs=set(range(50))
    )
    with _open(database, plan, profiles) as runner:
        with pytest.raises(RuntimeError, match="simulated runner crash"):
            runner.run_matchups(specs, match_runner=crashing)
        assert runner.persisted_match_count() == 2

    resumed_stub = StubRunner(incomplete_pairs=set(range(50)))
    with _open(database, plan, profiles) as resumed:
        resumed_reports = resumed.run_matchups(specs, match_runner=resumed_stub)
        resumed_digest = resumed.state_digest()
        assert resumed.persisted_match_count() == 3
    assert len(resumed_stub.calls) == 1
    assert [
        report["tournament_binding"]["matchup_id"]
        for report in resumed_reports
    ] == [spec["matchup_id"] for spec in specs]

    uninterrupted_stub = StubRunner(incomplete_pairs=set(range(50)))
    with _open(tmp_path / "one-shot.sqlite3", plan, profiles) as one_shot:
        one_shot.run_matchups(specs, match_runner=uninterrupted_stub)
        uninterrupted_digest = one_shot.state_digest()
    assert resumed_digest == uninterrupted_digest


def test_resume_refuses_source_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    database = tmp_path / "source.sqlite3"
    with _open(database, plan, profiles):
        pass
    monkeypatch.setattr(
        runner_module.model, "_source_fingerprint", lambda: "stale-source"
    )
    with pytest.raises(ValueError, match="source fingerprint is stale"):
        _open(database, plan, profiles)


def test_schedule_is_frozen_before_results_and_cannot_be_optionally_extended(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    database = tmp_path / "schedule.sqlite3"
    spec = plan["matchups"]["group"][0]
    with _open(database, plan, profiles) as runner:
        runner.run_matchups(
            (spec,),
            match_runner=StubRunner(incomplete_pairs=set(range(50))),
        )
        assert runner.schedule == "base"
    with pytest.raises(
        ValueError, match="frozen identity changed: schedule"
    ) as live_error:
        expansion = choose_result_blind_expansion(
            protocol_digest=PROTOCOL_DIGEST,
            calibration_timing_evidence=_calibration_timing(),
            fixed_overhead_reserve_seconds=0.0,
        )
        TournamentRunner(
            database,
            tournament_plan=plan,
            profiles_by_effective_id=profiles,
            schedule="expanded",
            _test_corpus_snapshots=(_corpus_snapshot(),),
            _allow_test_corpus=True,
            promotion_registry_path=tmp_path / "promotion-registry.sqlite3",
            expansion_decision=expansion,
            _allow_test_registry=True,
            require_native=False,
        )
    assert live_error.value is not None
    moved = database.with_name("schedule-moved.sqlite3")
    database.rename(moved)
    moved.unlink()


def test_expansion_requires_same_database_pending_calibration(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    expansion = choose_result_blind_expansion(
        protocol_digest=PROTOCOL_DIGEST,
        calibration_timing_evidence=_calibration_timing(),
        fixed_overhead_reserve_seconds=0.0,
    )
    with pytest.raises(ValueError, match="sealed from a pending run"):
        TournamentRunner(
            tmp_path / "fresh-expanded.sqlite3",
            tournament_plan=plan,
            profiles_by_effective_id=profiles,
            schedule="expanded",
            _test_corpus_snapshots=(_corpus_snapshot(),),
            _allow_test_corpus=True,
            promotion_registry_path=tmp_path / "promotion-registry.sqlite3",
            expansion_decision=expansion,
            _allow_test_registry=True,
            require_native=False,
        )
    with TournamentRunner(
        tmp_path / "pending.sqlite3",
        tournament_plan=plan,
        profiles_by_effective_id=profiles,
        schedule="pending",
        _test_corpus_snapshots=(_corpus_snapshot(),),
        _allow_test_corpus=True,
        promotion_registry_path=tmp_path / "promotion-registry.sqlite3",
        _allow_test_registry=True,
        require_native=False,
    ) as pending:
        with pytest.raises(ValueError, match="first 1000 group games"):
            pending.run_matchups(
                (plan["matchups"]["group"][10],),
                match_runner=StubRunner(incomplete_pairs=set(range(50))),
            )
        with pytest.raises(ValueError, match="first 1000 canonical group games"):
            pending.freeze_result_blind_expansion()
        assert pending.persisted_match_count() == 0


def test_calibration_counts_manual_records_and_is_result_blind_and_time_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    ticks = iter(range(0, 1_000, 10))
    monkeypatch.setattr(runner_module.time, "perf_counter", lambda: next(ticks))

    class CalibrationRetryStub:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, candidate, reference, **kwargs):
            attempt_index = self.calls % 2
            self.calls += 1
            return _stub_report(
                candidate,
                reference,
                incomplete_pairs=(
                    set(range(40, 50)) if attempt_index == 0 else set()
                ),
                incomplete_terminal_reason="manual-adjudication-pending",
                **kwargs,
            )

    stub = CalibrationRetryStub()
    with _open_fast(
        tmp_path / "replacement-calibration.sqlite3",
        plan,
        profiles,
        schedule="pending",
    ) as runner:
        reports = runner.run_matchups(
            tuple(plan["matchups"]["group"][:10]),
            match_runner=stub,
        )
        assert stub.calls == 20
        assert all(report["summary"]["incomplete_games"] == 0 for report in reports)
        assert all(len(report["games"]) == 100 for report in reports)
        assert all(
            [
                attempt["executed_game_records"]
                for attempt in report["opening_attempts"]["attempts"]
            ]
            == [100, 20]
            for report in reports
        )
        decision = runner.freeze_result_blind_expansion()
        assert decision["selected_group_game_records"] == 1_000
        assert decision["executed_group_game_records"] == 1_200
        assert decision["observed_execution_wall_seconds"] == 200.0
        assert decision["observed_selected_game_records_per_second"] == 5.0
        assert decision["remaining_depth2_screening_game_records"] == 21_400
        assert decision["result_fields_consumed"] == []
        assert decision["fixed_overhead_reserve_seconds"] == (
            FROZEN_EXPANSION_OVERHEAD_RESERVE_SECONDS
        )
        assert all(
            row["selected_game_records"] == 100
            and row["executed_game_records"] == 120
            and row["execution_wall_seconds"] == 20.0
            for row in decision["calibration_timing_evidence"]
        )

    assert all(
        set(row)
        == {
            "stage",
            "matchup_id",
            "ordinal",
            "pair_records",
            "selected_game_records",
            "executed_game_records",
            "execution_wall_seconds",
        }
        for row in decision["calibration_timing_evidence"]
    )
    assert choose_result_blind_expansion(
        protocol_digest=PROTOCOL_DIGEST,
        calibration_timing_evidence=decision["calibration_timing_evidence"],
        fixed_overhead_reserve_seconds=(
            FROZEN_EXPANSION_OVERHEAD_RESERVE_SECONDS
        ),
    ) == decision


def test_calibration_rejects_nonmanual_and_technical_incompletes(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    for label, stub, message in (
        (
            "arbitrary",
            StubRunner(incomplete_pairs=set(range(50))),
            "manual-adjudication-pending",
        ),
        (
            "worker",
            StubRunner(
                incomplete_pairs=set(range(50)),
                incomplete_terminal_reason="worker-exception",
            ),
            "technical or worker failures",
        ),
        (
            "shared-limit",
            StubRunner(
                incomplete_pairs=set(range(50)),
                incomplete_terminal_reason=(
                    "technical-game-work-budget-exhausted"
                ),
            ),
            "technical or worker failures",
        ),
        (
            "engine",
            StubRunner(
                incomplete_pairs=set(range(50)),
                incomplete_terminal_reason="manual-adjudication-pending",
                attribute_incomplete_to_candidate=True,
            ),
            "technical or worker failures",
        ),
        (
            "error-field",
            StubRunner(
                incomplete_pairs=set(range(50)),
                incomplete_terminal_reason="manual-adjudication-pending",
                incomplete_error="forged-error",
            ),
            "technical or worker failures",
        ),
        (
            "decisive-field",
            StubRunner(
                incomplete_pairs=set(range(50)),
                incomplete_terminal_reason="manual-adjudication-pending",
                attribute_incomplete_as_decisive=True,
            ),
            "decisive profile",
        ),
        (
            "series-count",
            StubRunner(
                incomplete_pairs=set(range(50)),
                incomplete_terminal_reason="manual-adjudication-pending",
                incomplete_series_played=999,
            ),
            "series count",
        ),
    ):
        database = tmp_path / f"{label}-calibration.sqlite3"
        with TournamentRunner(
            database,
            tournament_plan=plan,
            profiles_by_effective_id=profiles,
            schedule="pending",
            _test_corpus_snapshots=(_corpus_snapshot(),),
            _allow_test_corpus=True,
            promotion_registry_path=tmp_path / "promotion-registry.sqlite3",
            _allow_test_registry=True,
            require_native=False,
        ) as runner:
            runner.run_matchups(
                tuple(plan["matchups"]["group"][:10]),
                match_runner=stub,
            )
            with pytest.raises(ValueError, match=message):
                runner.freeze_result_blind_expansion()
            assert runner.schedule == "pending"
            assert runner.expansion_decision is None


def test_calibration_rejects_extra_wrong_count_and_noncanonical_report(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    with TournamentRunner(
        tmp_path / "calibration-integrity.sqlite3",
        tournament_plan=plan,
        profiles_by_effective_id=profiles,
        schedule="pending",
        _test_corpus_snapshots=(_corpus_snapshot(),),
        _allow_test_corpus=True,
        promotion_registry_path=tmp_path / "promotion-registry.sqlite3",
        _allow_test_registry=True,
        require_native=False,
    ) as runner:
        runner.run_matchups(
            tuple(plan["matchups"]["group"][:10]),
            match_runner=StubRunner(
                incomplete_pairs=set(range(50)),
                incomplete_terminal_reason="manual-adjudication-pending",
            ),
        )
        first = runner.connection.execute(
            "select * from match_reports where ordinal=0"
        ).fetchone()
        assert first is not None
        with runner.connection:
            runner.connection.execute(
                "insert into match_reports values(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "group",
                    "extra-calibration-matchup",
                    10,
                    first["resolved_spec_json"],
                    first["resolved_spec_digest"],
                    first["suite_digest"],
                    first["report_json"],
                    "fe" * 32,
                    50,
                    first["execution_elapsed_seconds"],
                    0,
                ),
            )
        with pytest.raises(ValueError, match="non-calibration matchup"):
            runner.freeze_result_blind_expansion()
        with runner.connection:
            runner.connection.execute(
                "delete from match_reports where matchup_id=?",
                ("extra-calibration-matchup",),
            )
            runner.connection.execute(
                "update match_reports set pair_count=49 where ordinal=0"
            )
        with pytest.raises(ValueError, match="persisted match job identity"):
            runner.freeze_result_blind_expansion()
        original_json = str(first["report_json"])
        malformed = json.loads(original_json)
        malformed["games"].pop()
        with runner.connection:
            runner.connection.execute(
                "update match_reports set pair_count=50,report_json=? "
                "where ordinal=0",
                (json.dumps(malformed),),
            )
        with pytest.raises(ValueError, match="frozen game job"):
            runner.freeze_result_blind_expansion()
        assert runner.connection.execute(
            "select count(*) from artifacts where kind='expansion-decision'"
        ).fetchone()[0] == 0


def test_registry_prevents_cross_batch_survivor_and_opening_reuse(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "promotion-registry.sqlite3"
    first_plan, first_profiles = _plan_and_profiles(tmp_path)
    first_domain = "group-01-openings"
    with _open_fast(
        tmp_path / "first-batch.sqlite3", first_plan, first_profiles
    ) as first_runner:
        first_suite = first_runner.prepare_opening_suite(first_domain)
        first_suite_digest = canonical_digest(
            "spc-tournament-opening-suite-v1\0", first_suite.as_dict()
        )
        first_runner._reserve_global_opening_positions(
            first_domain, first_suite, first_suite_digest
        )
    with sqlite3.connect(registry) as connection:
        connection.execute(
            "update promotion_batches set status='complete' where batch_index=1"
        )
        connection.execute(
            "insert into promotion_decisions values(1,?,?,0)",
            ("{}", "f1" * 32),
        )
    baseline = baseline_profile()
    second_batch = reserve_promotion_batch(
        registry,
        reservation_key="runner-batch-2",
        protocol_digest=PROTOCOL_DIGEST,
        baseline_effective_id=effective_profile_id(baseline.weights),
    )
    first_survivors = tuple(
        RankedCandidate(
            item["candidate_index"],
            item["effective_id"],
            item["profile_id"],
            index,
        )
        for index, item in enumerate(
            first_plan["survivors_in_validation_rank_order"]
        )
    )
    reused_plan = build_tournament_plan(
        first_survivors,
        baseline,
        protocol_digest=PROTOCOL_DIGEST,
        promotion_batch=second_batch,
    )
    with pytest.raises(ValueError, match="already consumed"):
        TournamentRunner(
            tmp_path / "reused-survivors.sqlite3",
            tournament_plan=reused_plan,
            profiles_by_effective_id=first_profiles,
            schedule="base",
            _test_corpus_snapshots=(_corpus_snapshot(),),
            _allow_test_corpus=True,
            promotion_registry_path=registry,
            _allow_test_registry=True,
            require_native=False,
        )

    stream = PopulationStream(baseline)
    second_members = tuple(stream.iter_range(64, 128))
    second_survivors = tuple(
        RankedCandidate(
            member.candidate_index,
            member.effective_id,
            member.profile.profile_id,
            index,
        )
        for index, member in enumerate(second_members)
    )
    second_plan = build_tournament_plan(
        second_survivors,
        baseline,
        protocol_digest=PROTOCOL_DIGEST,
        promotion_batch=second_batch,
    )
    assert (
        second_plan["opening_suites"][0]["attempt_lanes"][0]["seed"]
        != first_plan["opening_suites"][0]["attempt_lanes"][0]["seed"]
    )
    second_profiles = {
        member.effective_id: member.profile for member in second_members
    }
    second_profiles[effective_profile_id(baseline.weights)] = baseline
    with TournamentRunner(
        tmp_path / "second-batch.sqlite3",
        tournament_plan=second_plan,
        profiles_by_effective_id=second_profiles,
        schedule="base",
        _test_corpus_snapshots=(_corpus_snapshot(),),
        _allow_test_corpus=True,
        promotion_registry_path=registry,
        _allow_test_registry=True,
        require_native=False,
    ) as second_runner:
        with pytest.raises(ValueError, match="consumed by another batch"):
            second_runner._reserve_global_opening_positions(
                first_domain, first_suite, first_suite_digest
            )


def test_all_opening_suites_resample_corpus_cross_suite_and_prior_batch_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "promotion-registry.sqlite3"
    baseline = baseline_profile()
    prior = reserve_promotion_batch(
        registry,
        reservation_key="prior-batch",
        protocol_digest=PROTOCOL_DIGEST,
        baseline_effective_id=effective_profile_id(baseline.weights),
    )
    prior_hash = "99" * 16
    with sqlite3.connect(registry) as connection:
        connection.execute(
            "update promotion_batches set plan_digest=?,status='complete' "
            "where batch_index=1",
            ("aa" * 32,),
        )
        connection.execute(
            "insert into opening_positions values(?,?,?,?,?)",
            (prior_hash, 1, "group-01-openings", "bb" * 32, "aa" * 32),
        )
        connection.execute(
            "insert into promotion_decisions values(1,?,?,0)",
            ("{}", "cc" * 32),
        )

    corpus_hash = "88" * 16
    exclusion = runner_module._build_corpus_exclusion_artifact_from_corpora(
        (_corpus_snapshot(),), authority="test-only-synthetic-fixture"
    )
    exclusion["position_hashes"] = [corpus_hash]
    exclusion["unique_position_count"] = 1
    exclusion["position_hash_digest"] = canonical_digest(
        "spc-tournament-corpus-position-hashes-v1\0", [corpus_hash]
    )
    exclusion["artifact_digest"] = canonical_digest(
        "spc-tournament-corpus-exclusion-v1\0",
        {key: value for key, value in exclusion.items() if key != "artifact_digest"},
    )

    reservation_key = "fresh-collision-test"
    master_seed = 20260820

    def derived(domain: str, nonce: int) -> int:
        return runner_module._opening_selection_seed(
            protocol_digest=PROTOCOL_DIGEST,
            reservation_key=reservation_key,
            domain=domain,
            master_seed=master_seed,
            nonce=nonce,
        )

    def normal_hash(seed: int, index: int) -> str:
        return hashlib.sha256(f"{seed}|{index}".encode()).hexdigest()[:32]

    first_lane = "group-01-openings|attempt|0"
    second_lane = "group-02-openings|attempt|0"
    collision_by_seed = {
        derived(first_lane, 0): corpus_hash,
        derived(second_lane, 0): normal_hash(derived(first_lane, 1), 0),
        derived(second_lane, 1): prior_hash,
    }

    class FakeCase:
        def __init__(self, position_hash: str) -> None:
            self._position_hash = position_hash

        def state(self):
            return type("State", (), {"position_hash": self._position_hash})()

    class FakeSuite:
        def __init__(self, seed: int, count: int) -> None:
            self.seed = seed
            self.min_series = 3
            self.max_series = 6
            self.max_frontier_states = 32
            hashes = [normal_hash(seed, index) for index in range(count)]
            if seed in collision_by_seed:
                hashes[0] = collision_by_seed[seed]
            self.cases = tuple(FakeCase(value) for value in hashes)

        def as_dict(self) -> dict[str, Any]:
            return {
                "fake_seed": self.seed,
                "hashes": [case.state().position_hash for case in self.cases],
            }

    monkeypatch.setattr(
        runner_module,
        "build_seeded_opening_suite",
        lambda *, seed, count, **_kwargs: FakeSuite(seed, count),
    )
    monkeypatch.setattr(runner_module, "verify_seeded_opening_suite", lambda _suite: None)
    preparation = freeze_opening_suites_before_batch(
        registry,
        reservation_key=reservation_key,
        protocol_digest=PROTOCOL_DIGEST,
        corpus_exclusion_artifact=exclusion,
        master_seed=master_seed,
        _allow_test_registry=True,
    )
    frozen = preparation["opening_suites"]
    assert len(frozen) == 13
    assert frozen[0]["attempt_lanes"][0]["selection_nonce"] == 1
    assert frozen[1]["attempt_lanes"][0]["selection_nonce"] == 2
    assert all(len(item["attempt_lanes"]) == 3 for item in frozen)
    hashes = [
        value
        for item in frozen
        for lane in item["attempt_lanes"]
        for value in lane["suite"]["hashes"]
    ]
    assert len(hashes) == len(set(hashes)) == 2_850
    assert not ({corpus_hash, prior_hash} & set(hashes))
    assert preparation["materialization_work"] == {
        "logical_reserve_count": 13,
        "attempt_lane_count": 39,
        "selected_boundary_count": 2_850,
        "candidate_suite_build_count": 42,
        "candidate_boundary_count": 3_000,
        "nonce_replay_bound_per_lane": 65_535,
        "wall_clock_estimate": "operational-only-not-authoritative",
    }
    assert preparation["result_inputs"] == "none"
    with sqlite3.connect(registry) as connection:
        assert connection.execute(
            "select count(*) from promotion_batches"
        ).fetchone()[0] == 1
    competitor = reserve_promotion_batch(
        registry,
        reservation_key="competing-preparation",
        protocol_digest=PROTOCOL_DIGEST,
        baseline_effective_id=effective_profile_id(baseline.weights),
    )
    assert competitor["batch_index"] == 2
    with pytest.raises(ValueError, match="changed after opening-suite preparation"):
        reserve_promotion_batch(
            registry,
            reservation_key=reservation_key,
            protocol_digest=PROTOCOL_DIGEST,
            baseline_effective_id=effective_profile_id(baseline.weights),
            expected_registry_snapshot_digest=preparation[
                "registry_snapshot_digest"
            ],
        )
    with sqlite3.connect(registry) as connection:
        assert connection.execute(
            "select count(*) from promotion_batches"
        ).fetchone()[0] == 2


def test_resampled_opening_seed_binds_a_real_frozen_match_report() -> None:
    suite = build_seeded_opening_suite(
        seed=987_654_321,
        count=50,
        min_series=3,
        max_series=3,
        max_frontier_states=2,
    )
    stream = PopulationStream(baseline_profile())
    first, second = tuple(stream.iter_range(0, 2))
    limits = tournament_limits_for_stage("group")
    config = StrengthMatchConfig(
        pairs=50,
        seed=123_456,
        search_depth=int(limits["depth_series"]),
        max_series_per_node=int(limits["branch_cap_complete_series_per_node"]),
        max_generation_positions=int(limits["max_work_positions_per_search"]),
        max_game_work_positions=int(limits["max_game_work_positions"]),
        emergency_max_series=limits["emergency_max_series"],
        opening_suite_version=suite.version,
        opening_case_ids=tuple(case.case_id for case in suite.cases),
    )
    report = _stub_report(
        first.profile,
        second.profile,
        config=config,
        opening_cases=suite,
        requested_workers=16,
        incomplete_pairs=set(range(50)),
        incomplete_terminal_reason="manual-adjudication-pending",
    )
    match_spec = {
        "stage": "group",
        "matchup_id": "resampled-suite-bound-report",
        "first_slot": first.effective_id,
        "second_slot": second.effective_id,
        "resolved_first_effective_id": first.effective_id,
        "resolved_second_effective_id": second.effective_id,
        "opening_domain": "group-01-openings",
        "opening_seed": suite.seed,
        "opening_suite_digest": canonical_digest(
            "spc-tournament-opening-suite-v1\0", suite.as_dict()
        ),
        "match_seed": config.seed,
        "base_pairs": 50,
        "maximum_pairs": 50,
        "base_games": 100,
        "maximum_games": 100,
        "limits": limits,
        "job_identity_parent": PROTOCOL_DIGEST,
        "promotion_effect": "none",
    }
    bound = bind_frozen_match_report(
        report,
        match_spec=match_spec,
        protocol_digest=PROTOCOL_DIGEST,
        tournament_plan_digest="dd" * 32,
        effective_by_profile_id={
            first.profile.profile_id: first.effective_id,
            second.profile.profile_id: second.effective_id,
        },
    )
    assert bound["tournament_binding"]["match_spec_digest"] == canonical_digest(
        "spc-resolved-match-spec-v1\0", match_spec
    )
    stale_spec = {**match_spec, "opening_seed": suite.seed + 1}
    with pytest.raises(ValueError, match="opening suite identity"):
        bind_frozen_match_report(
            report,
            match_spec=stale_spec,
            protocol_digest=PROTOCOL_DIGEST,
            tournament_plan_digest="dd" * 32,
            effective_by_profile_id={
                first.profile.profile_id: first.effective_id,
                second.profile.profile_id: second.effective_id,
            },
        )


def test_administrative_abandonment_consumes_alpha_without_promotion(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "promotion-registry.sqlite3"
    plan, profiles = _plan_and_profiles(tmp_path)
    runner = _open(tmp_path / "invalid-plan.sqlite3", plan, profiles)
    consumed_suite = runner.prepare_opening_suite("group-01-openings")
    consumed_hash = consumed_suite.cases[0].state().position_hash
    decision = abandon_promotion_batch(
        registry,
        batch_index=1,
        expected_plan_digest=plan["tournament_plan_digest"],
        reason="invalid-opening-plan",
        _allow_test_registry=True,
    )
    assert decision["alpha_batch_consumed"] is True
    assert decision["promoted"] is False
    assert decision["promotion_effect"] == "none"
    assert abandon_promotion_batch(
        registry,
        batch_index=1,
        expected_plan_digest=plan["tournament_plan_digest"],
        reason="invalid-opening-plan",
        _allow_test_registry=True,
    ) == decision
    blocked_runner = StubRunner()
    with pytest.raises(ValueError, match="administratively closed"):
        runner.run_matchups(
            (plan["matchups"]["group"][0],), match_runner=blocked_runner
        )
    assert blocked_runner.calls == []
    assert runner.persisted_match_count() == 0
    runner.close()
    with sqlite3.connect(registry) as connection:
        assert connection.execute(
            "select status from promotion_batches where batch_index=1"
        ).fetchone()[0] == "abandoned"
        assert connection.execute(
            "select promoted from promotion_decisions where batch_index=1"
        ).fetchone()[0] == 0
        assert connection.execute(
            "select count(*) from challenger_survivors where batch_index=1"
        ).fetchone()[0] == 64
        assert connection.execute(
            "select count(*) from opening_positions where batch_index=1"
        ).fetchone()[0] == 50
        assert connection.execute(
            "select batch_index from opening_positions where position_hash=?",
            (consumed_hash,),
        ).fetchone()[0] == 1
    next_batch = reserve_promotion_batch(
        registry,
        reservation_key="after-abandonment",
        protocol_digest=PROTOCOL_DIGEST,
        baseline_effective_id=effective_profile_id(baseline_profile().weights),
    )
    assert next_batch["batch_index"] == 2


def test_inflight_match_is_discarded_when_batch_is_abandoned_during_compute(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "promotion-registry.sqlite3"
    plan, profiles = _plan_and_profiles(tmp_path)
    spec = plan["matchups"]["group"][0]
    runner = _open_fast(
        tmp_path / "inflight-abandonment.sqlite3", plan, profiles
    )
    calls = 0

    def abandon_during_compute(candidate, reference, **kwargs):
        nonlocal calls
        calls += 1
        abandon_promotion_batch(
            registry,
            batch_index=1,
            expected_plan_digest=plan["tournament_plan_digest"],
            reason="invalid-opening-plan",
            _allow_test_registry=True,
        )
        return _stub_report(
            candidate,
            reference,
            **kwargs,
        )

    with pytest.raises(ValueError, match="administratively closed"):
        runner.run_matchups((spec,), match_runner=abandon_during_compute)
    assert calls == 1
    assert runner.persisted_match_count() == 0
    assert runner.connection.execute(
        "select count(*) from match_attempts"
    ).fetchone()[0] == 0
    runner.close()
    with sqlite3.connect(registry) as connection:
        assert connection.execute(
            "select status from promotion_batches where batch_index=1"
        ).fetchone()[0] == "abandoned"
        assert connection.execute(
            "select count(*) from challenger_survivors where batch_index=1"
        ).fetchone()[0] == 64


def test_incomplete_pair_retry_keeps_pair_atomic_and_summary_canonical(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    spec = plan["matchups"]["group"][0]
    database = tmp_path / "replacement.sqlite3"
    initial = StubRunner(incomplete_pairs=set(range(50)), marker="initial")
    with _open(database, plan, profiles) as runner:
        original = runner.run_matchups((spec,), match_runner=initial)[0]
        replacement = runner.retry_incomplete_pairs(
            "group",
            spec["matchup_id"],
            match_runner=StubRunner(
                incomplete_pairs=set(range(50)), marker="retry"
            ),
        )
        rebound = replacement["report"]
        assert replacement["eligible_incomplete_pairs"] == list(range(50))
        assert replacement["complete_color_swapped_pairs_replaced"] == 0
        assert replacement["completed_pairs_replaced"] == 0
        assert rebound["summary"]["completed_pairs"] == 0
        assert rebound["summary"]["incomplete_pairs"] == 50
        assert canonical_json_bytes(rebound["games"]) == canonical_json_bytes(
            original["games"]
        )
        runner._put_artifact(
            kind="group-standing",
            key="group-01",
            payload={"format": "sealed-test-standing", "promotion_effect": "none"},
        )
        frozen_state = runner.state_digest()
        with pytest.raises(ValueError, match="dependent advancement was sealed"):
            runner.retry_incomplete_pairs(
                "group",
                spec["matchup_id"],
                match_runner=StubRunner(incomplete_pairs=set(range(50))),
            )
        assert runner.state_digest() == frozen_state


def test_knockout_incomplete_report_cannot_advance_or_promote(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    # Resolve the two symbolic R16 slots through frozen, non-promoting artifacts.
    spec = plan["matchups"]["round_of_16"][0]
    first = plan["groups"]["group-01"][0]["effective_id"]
    second = plan["groups"]["group-08"][1]["effective_id"]
    with _open(tmp_path / "incomplete.sqlite3", plan, profiles) as runner:
        runner._put_artifact(
            kind="test-slot-fixture",
            key="r16-slots",
            payload={"promotion_effect": "none"},
            slot_updates={
                spec["first_slot"]: first,
                spec["second_slot"]: second,
            },
        )
        runner.run_matchups(
            (spec,),
            match_runner=StubRunner(incomplete_pairs=set(range(50))),
        )
        with pytest.raises(ValueError, match="every frozen pair to complete"):
            runner.derive_knockout_winners("round-of-16")
        assert runner.connection.execute(
            "select count(*) from slot_resolutions where slot='r16-01-winner'"
        ).fetchone()[0] == 0


def test_three_lane_first_complete_selection_replays_exact_final_openings(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    lanes = _fast_attempt_lanes()
    incomplete_ids_by_call = (
        {case.case_id for case in lanes[0].cases[20:50]},
        {case.case_id for case in lanes[1].cases[40:50]},
        set(),
    )

    class LogicalWaveStub:
        def __init__(self) -> None:
            self.calls = []

        def __call__(self, candidate, reference, **kwargs):
            call_index = len(self.calls)
            self.calls.append((candidate.profile_id, reference.profile_id))
            jobs = _build_jobs(
                candidate,
                reference,
                kwargs["config"],
                kwargs["opening_cases"],
            )
            incomplete_local_indexes = {
                pair_index
                for pair_index, job in enumerate(jobs[::2])
                if job.opening.case_id in incomplete_ids_by_call[call_index]
            }
            return _stub_report(
                candidate,
                reference,
                incomplete_pairs=incomplete_local_indexes,
                **kwargs,
            )

    stub = LogicalWaveStub()
    with _open_fast(tmp_path / "three-lane.sqlite3", plan, profiles) as runner:
        spec = runner._stage_specs("group")[0]
        report = runner.run_matchups((spec,), match_runner=stub)[0]
        assert len(stub.calls) == 3
        assert len(report["games"]) == 100
        assert len(report["selected_openings"]) == 50
        manifest = report["opening_attempts"]
        assert [
            item["executed_game_records"] for item in manifest["attempts"]
        ] == [100, 60, 20]
        assert {
            item["attempt_index"] for item in manifest["selected_pairs"]
        } == {0, 1, 2}
        reserve = runner.prepare_opening_reserve("group-01-openings")
        expected_case_ids = set()
        expected_pfen_by_case = {}
        for item in manifest["selected_pairs"]:
            logical_index = int(item["logical_pair_index"])
            attempt_index = int(item["attempt_index"])
            case = reserve.lanes[attempt_index].cases[logical_index]
            assert item["opening_case_id"] == case.case_id
            expected_case_ids.add(case.case_id)
            expected_pfen_by_case[case.case_id] = case.state().pfen
        assert {item["case_id"] for item in report["selected_openings"]} == (
            expected_case_ids
        )
        for pair_index, pair in enumerate(report["pairs"]):
            games = report["games"][pair_index * 2 : pair_index * 2 + 2]
            assert pair["pair_index"] == pair_index
            assert len({game["opening_case_id"] for game in games}) == 1
            assert {
                game["white_profile_id"] for game in games
            } == {
                report["candidate"]["profile_id"],
                report["reference"]["profile_id"],
            }
            for game in games:
                assert game["start_pfen"] == expected_pfen_by_case[
                    game["opening_case_id"]
                ]
        row = runner.connection.execute(
            "select replacement_attempts,attempt_manifest_digest from "
            "match_reports where ordinal=0"
        ).fetchone()
        assert tuple(row) == (2, manifest["attempt_manifest_digest"])
        sealed_digest = report["bound_report_digest"]

    resumed_stub = StubRunner()
    with _open_fast(tmp_path / "three-lane.sqlite3", plan, profiles) as resumed:
        report = resumed.run_matchups(
            (resumed._stage_specs("group")[0],),
            match_runner=resumed_stub,
        )[0]
        assert resumed_stub.calls == []
        assert report["bound_report_digest"] == sealed_digest


def test_replacement_exhaustion_fails_closed_without_sealed_report(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    stub = StubRunner(incomplete_pairs=set(range(50)))
    with _open_fast(tmp_path / "exhausted.sqlite3", plan, profiles) as runner:
        spec = runner._stage_specs("group")[0]
        with pytest.raises(
            OpeningReplacementExhaustedError,
            match="opening replacement cap exhausted",
        ):
            runner.run_matchups((spec,), match_runner=stub)
        assert len(stub.calls) == 3
        assert runner.connection.execute(
            "select count(*) from match_attempts"
        ).fetchone()[0] == 3
        assert runner.persisted_match_count() == 0
        no_rerun = StubRunner()
        with pytest.raises(OpeningReplacementExhaustedError):
            runner.run_matchups((spec,), match_runner=no_rerun)
        assert no_rerun.calls == []


def test_attempt_commit_survives_registry_commit_crash_without_duplicate_wave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    stub = StubRunner()
    with _open_fast(tmp_path / "commit-crash.sqlite3", plan, profiles) as runner:
        spec = runner._stage_specs("group")[0]
        real_connect = runner_module._connect_promotion_registry
        armed = {"value": True}

        class CommitCrashConnection:
            def __init__(self, connection) -> None:
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def commit(self):
                if armed["value"]:
                    armed["value"] = False
                    raise RuntimeError("simulated registry commit crash")
                return self.connection.commit()

        monkeypatch.setattr(
            runner_module,
            "_connect_promotion_registry",
            lambda path: CommitCrashConnection(real_connect(path)),
        )
        with pytest.raises(RuntimeError, match="registry commit crash"):
            runner.run_matchups((spec,), match_runner=stub)
        assert len(stub.calls) == 1
        first_digest = runner.connection.execute(
            "select report_digest from match_attempts where attempt_index=0"
        ).fetchone()[0]
        assert runner.persisted_match_count() == 0

        monkeypatch.setattr(
            runner_module, "_connect_promotion_registry", real_connect
        )
        no_duplicate = StubRunner()
        report = runner.run_matchups((spec,), match_runner=no_duplicate)[0]
        assert no_duplicate.calls == []
        assert runner.connection.execute(
            "select count(*) from match_attempts"
        ).fetchone()[0] == 1
        assert runner.connection.execute(
            "select report_digest from match_attempts where attempt_index=0"
        ).fetchone()[0] == first_digest
        assert report["opening_attempts"]["attempts"][0][
            "attempt_report_digest"
        ] == first_digest


def test_engine_or_worker_failure_is_not_retried_or_laundered(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    stub = StubRunner(
        incomplete_pairs=set(range(50)),
        incomplete_terminal_reason="worker-exception",
    )
    with _open_fast(tmp_path / "technical.sqlite3", plan, profiles) as runner:
        spec = runner._stage_specs("group")[0]
        with pytest.raises(ValueError, match="fail closed on engine, worker"):
            runner.run_matchups((spec,), match_runner=stub)
        assert len(stub.calls) == 1
        assert runner.connection.execute(
            "select count(*) from match_attempts"
        ).fetchone()[0] == 1
        assert runner.persisted_match_count() == 0
        no_rerun = StubRunner()
        with pytest.raises(ValueError, match="fail closed on engine, worker"):
            runner.run_matchups((spec,), match_runner=no_rerun)
        assert no_rerun.calls == []


def test_malformed_manual_incomplete_trace_cannot_trigger_replacement(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)

    def malformed_incomplete(candidate, reference, **kwargs):
        report = _stub_report(
            candidate,
            reference,
            incomplete_pairs={17},
            incomplete_terminal_reason="manual-adjudication-pending",
            **kwargs,
        )
        for game in report["games"][34:36]:
            game["trace"] = [
                {
                    "series_number": 999,
                    "series": "a2a5",
                    "played": True,
                }
            ]
            game["series_played"] = 1
        return report

    with _open_fast(tmp_path / "malformed-attempt.sqlite3", plan, profiles) as runner:
        spec = runner._stage_specs("group")[0]
        with pytest.raises(SeriesLegalityError, match="illegal move"):
            runner.run_matchups((spec,), match_runner=malformed_incomplete)
        assert runner.connection.execute(
            "select count(*) from match_attempts"
        ).fetchone()[0] == 0
        assert runner.persisted_match_count() == 0


def test_trusted_database_rejects_self_described_retry_history(
    tmp_path: Path,
) -> None:
    plan, profiles = _plan_and_profiles(tmp_path)
    with _open_fast(tmp_path / "authenticated-final.sqlite3", plan, profiles) as runner:
        domain = "group-01-openings"
        fast = runner.prepare_opening_reserve(domain)
        hashes = sorted(
            case.state().position_hash for lane in fast.lanes for case in lane.cases
        )
        lane_payloads = []
        for attempt_index, lane in enumerate(fast.lanes):
            lane_hashes = sorted(case.state().position_hash for case in lane.cases)
            lane_payloads.append(
                {
                    "attempt_index": attempt_index,
                    "count": 50,
                    "suite": lane.as_dict(),
                    "suite_digest": canonical_digest(
                        "spc-tournament-opening-suite-v1\0", lane.as_dict()
                    ),
                    "position_hash_digest": canonical_digest(
                        "spc-tournament-opening-position-hashes-v1\0", lane_hashes
                    ),
                }
            )
        reserve_payload = {
            "format": "spc-tournament-opening-reserve-v1",
            "domain": domain,
            "count": 50,
            "retry_policy": tournament_opening_retry_policy(),
            "attempt_lanes": lane_payloads,
            "total_case_count": 150,
            "position_hash_digest": canonical_digest(
                "spc-tournament-opening-position-hashes-v2\0", hashes
            ),
        }
        reserve_payload["reserve_digest"] = opening_reserve_digest(reserve_payload)
        runner._opening_suites_ready[domain] = TournamentOpeningReserve(
            domain=domain,
            count=50,
            lanes=fast.lanes,
            reserve_digest=reserve_payload["reserve_digest"],
            position_hash_digest=reserve_payload["position_hash_digest"],
        )
        with runner.connection:
            runner.connection.execute(
                "insert or replace into opening_suites values(?,?,?,?,?)",
                (
                    domain,
                    canonical_json_bytes(reserve_payload).decode("ascii"),
                    reserve_payload["reserve_digest"],
                    reserve_payload["position_hash_digest"],
                    150,
                ),
            )
        spec = runner._stage_specs("group")[0]
        report = runner.run_matchups((spec,), match_runner=StubRunner())[0]
        row = runner.connection.execute(
            "select * from match_reports where stage=? and matchup_id=?",
            ("group", spec["matchup_id"]),
        ).fetchone()
        resolved_spec = json.loads(row["resolved_spec_json"])
        authority = make_tournament_authority_artifact(
            runner.connection,
            database_path=runner.path,
            batch_index=1,
            final_rows={"group": row},
        )
        _validate_trusted_tournament_database(
            authority,
            tournament_plan_digest=plan["tournament_plan_digest"],
            evidence=((report, resolved_spec),),
        )

        forged = copy.deepcopy(report)
        raw = copy.deepcopy(report)
        raw.pop("bound_report_digest")
        raw.pop("tournament_binding")
        raw.pop("full_trace_evidence")
        for game in raw["games"]:
            game.pop("full_trace", None)
            game.pop("tournament_identity", None)
        for pair in raw["pairs"]:
            pair.pop("tournament_identity", None)
        indexes = list(range(50))
        zero_failures = {
            "candidate_attributed_failures": 0,
            "reference_attributed_failures": 0,
            "unattributed_worker_failures": 0,
            "unattributed_match_limit_failures": 0,
            "error_records": 0,
        }
        forged_attempts = []
        for attempt_index in range(3):
            completed = indexes if attempt_index == 2 else []
            remaining = [] if attempt_index == 2 else indexes
            forged_attempts.append(
                {
                    "attempt_index": attempt_index,
                    "unresolved_pair_indexes_in": indexes,
                    "completed_pair_indexes": completed,
                    "unresolved_pair_indexes_out": remaining,
                    "lane_suite_digest": f"lane-{attempt_index}",
                    "subset_suite_digest": f"subset-{attempt_index}",
                    "config_digest": f"config-{attempt_index}",
                    "attempt_report_digest": f"report-{attempt_index}",
                    "executed_game_records": 100,
                    "incomplete_terminal_evidence": {
                        "terminal_reason_counts": (
                            {}
                            if attempt_index == 2
                            else {"manual-adjudication-pending": 100}
                        ),
                        **zero_failures,
                    },
                    "execution_elapsed_seconds": float(attempt_index + 1),
                }
            )
        deterministic_manifest = {
            **{
                key: value
                for key, value in raw["opening_attempts"].items()
                if key != "attempt_manifest_digest"
            },
            "attempts": forged_attempts,
            "selected_pairs": [
                {
                    "logical_pair_index": index,
                    "attempt_index": 2,
                    "opening_case_id": report["pairs"][index]["opening_case_id"],
                }
                for index in indexes
            ],
        }
        manifest_digest = canonical_digest(
            "spc-tournament-opening-attempt-manifest-v1\0",
            deterministic_manifest,
        )
        raw["opening_attempts"] = {
            **deterministic_manifest,
            "attempt_manifest_digest": manifest_digest,
        }
        forged_spec = {
            **resolved_spec,
            "opening_attempt_manifest_digest": manifest_digest,
        }
        forged = bind_frozen_match_report(
            raw,
            match_spec=forged_spec,
            protocol_digest=runner.protocol_digest,
            tournament_plan_digest=runner.plan_digest,
            effective_by_profile_id={
                runner.profiles[resolved_spec["resolved_first_effective_id"]].profile_id:
                    resolved_spec["resolved_first_effective_id"],
                runner.profiles[resolved_spec["resolved_second_effective_id"]].profile_id:
                    resolved_spec["resolved_second_effective_id"],
            },
        )
        assert len(forged["opening_attempts"]["attempts"]) == 3
        with pytest.raises(
            ValueError, match="promotion evidence differs from trusted database"
        ):
            _validate_trusted_tournament_database(
                authority,
                tournament_plan_digest=plan["tournament_plan_digest"],
                evidence=((forged, forged_spec),),
            )
