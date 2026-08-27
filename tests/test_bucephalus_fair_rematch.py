from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import chess
import pytest

from benchmarks.bucephalus_timed_adapter import (
    BUCEPHALUS_ADAPTER_VERSION,
    BUCEPHALUS_MAX_PLY,
    BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
    BucephalusSpec,
    ExternalAnalysis,
    ExternalEngineTimeout,
    replay_series_history,
)
from benchmarks.bucephalus_fair_rematch import (
    APPROVED_BUCEPHALUS_BUILD_RECEIPT_SHA256,
    BUCEPHALUS_FAIR_OPENING_HISTORIES,
    BUCEPHALUS_FAIR_OPENING_SUITE,
    BUCEPHALUS_FAIR_OPENING_SUITE_CANONICAL_SHA256,
    BUCEPHALUS_FAIR_OPENING_SUITE_VERSION,
    COMMON_WALL_OVERRUN_GRACE_SECONDS,
    DEFAULT_EXTERNAL_MATCH_MEMORY_PER_WORKER_MB,
    TIMED_ITERATIVE_PLY_POLICY,
    ExternalGameJob,
    ExternalMatchConfig,
    _build_jobs,
    _git_source_provenance,
    _journal_protocol,
    _load_external_build_receipt,
    _play_external_game,
    _prepare_journal,
    _summarize,
    _superiority_gate,
    build_parser,
    write_external_match_report,
)
from scottish_progressive.league import OpeningCase
from scottish_progressive.model import ProgressiveState, SeriesResult
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.resources import ResourceBudget
from scottish_progressive.rules import play_series


PINNED_HASH = "1" * 64
PUBLISHED_MATE = ("c7c6", "d8b6", "f6e4", "b6f2")


def _identity_snapshot() -> dict[str, object]:
    return {
        "engine_version": "test-engine",
        "source_fingerprint": "test-source",
        "git": {"head_commit": "test-head"},
        "runtime": {"python_version": "test-python"},
        "backend": {"release_native_runtime": "test-native"},
        "benchmark_harness": {
            "schema": "test-harness",
            "files": [],
            "artifact_set_sha256": "2" * 64,
        },
    }


def _spec(tmp_path: Path) -> BucephalusSpec:
    return BucephalusSpec(
        tmp_path / "not-launched-in-unit-tests.exe",
        PINNED_HASH,
        upstream_commit="test-upstream",
    )


def _config(
    opening: str = "published-bishop-pressure",
    *,
    emergency_max_series: int = 4,
) -> ExternalMatchConfig:
    return ExternalMatchConfig(
        pairs=1,
        opening_case_ids=(opening,),
        local_depth_series=1,
        local_max_series_per_node=4,
        local_max_generation_positions=100,
        local_max_game_work_positions=1_000,
        external_wall_timeout_seconds=1.0,
        emergency_max_series=emergency_max_series,
    )


def _resources(*, workers: int = 1) -> ResourceBudget:
    return ResourceBudget(
        detected_logical_cpus=16,
        available_memory_bytes=16 * 1024**3,
        memory_per_worker_bytes=768 * 1024**2,
        reserved_memory_bytes=512 * 1024**2,
        cpu_worker_cap=16,
        memory_worker_cap=20,
        worker_cap=16,
        requested_workers=workers,
        workers=workers,
    )


def _timed_config(
    opening: str = "published-bishop-pressure",
    *,
    emergency_max_series: int = 4,
) -> ExternalMatchConfig:
    return ExternalMatchConfig(
        pairs=1,
        opening_case_ids=(opening,),
        local_depth_series=8,
        local_max_series_per_node=32,
        local_max_generation_positions=100_000_000,
        local_max_game_work_positions=4_000_000_000,
        external_ply_policy=TIMED_ITERATIVE_PLY_POLICY,
        external_wall_timeout_seconds=1.0,
        common_wall_timeout_seconds=1.0,
        emergency_max_series=emergency_max_series,
    )


def _local_result(state: ProgressiveState, moves: tuple[str, ...]):
    selected = play_series(state, moves)
    stats = SimpleNamespace(
        work_positions=7,
        generation_positions=7,
        nodes=3,
        promotion_mate_positions=5,
        promotion_mate_setup_states=6,
        promotion_mate_candidates=4,
        promotion_mate_completion_probes=3,
        promotion_mate_mates=1,
        promotion_mate_limit_hits=0,
        promotion_mate_replay_rejects=2,
    )
    return SimpleNamespace(
        best_series=selected,
        score=-1_000_000 if state.board.turn == chess.BLACK else 1_000_000,
        completed_depth=1,
        exact_width=False,
        root_scores_complete=False,
        work_limit_reached=False,
        timed_out=False,
        proof="black" if state.board.turn == chess.BLACK else "white",
        adjudication_status=None,
        stats=stats,
    )


def _external_result(
    state: ProgressiveState,
    moves: tuple[str, ...],
    *,
    requested_ply: int,
    completed_ply: int | None = None,
    adapter_version: str = BUCEPHALUS_ADAPTER_VERSION,
    deadline_reached: bool = False,
    process_exit_code: int | None = None,
    process_exit_recovered: bool = False,
) -> ExternalAnalysis:
    selected = play_series(state, moves)
    return ExternalAnalysis(
        best_series=selected,
        requested_ply=requested_ply,
        completed_ply=(
            requested_ply if completed_ply is None else completed_ply
        ),
        score_text="*MATE*" if selected.outcome else "0.00",
        elapsed_seconds=0.01,
        executable_sha256=PINNED_HASH,
        upstream_commit="test-upstream",
        adapter_version=adapter_version,
        request_script="p\ne\n4\nt\nq\n",
        stdout="Bucephalus v1.0.0\n[PLY 4]...\n",
        stderr="",
        deadline_reached=deadline_reached,
        process_exit_code=process_exit_code,
        process_exit_recovered=process_exit_recovered,
    )


def test_fair_suite_has_50_unique_authoritatively_replayable_boundaries() -> None:
    assert len(BUCEPHALUS_FAIR_OPENING_SUITE) == 50
    assert len(BUCEPHALUS_FAIR_OPENING_HISTORIES) == 50
    hashes: set[int] = set()
    for opening in BUCEPHALUS_FAIR_OPENING_SUITE:
        replayed = replay_series_history(
            BUCEPHALUS_FAIR_OPENING_HISTORIES[opening.case_id]
        )
        assert replayed.position_hash == opening.state().position_hash
        hashes.add(replayed.position_hash)
    assert len(hashes) == 50
    assert BUCEPHALUS_FAIR_OPENING_SUITE_VERSION == (
        "spc-neutral-seeded-openings-v1-a292fa4db4e8b7d98248"
    )
    assert BUCEPHALUS_FAIR_OPENING_SUITE_CANONICAL_SHA256 == (
        "53fe7d10b5e31d93e0b9b75374832c2e319a691b710c34c4e4a75b5db2cb6ff1"
    )
    distribution = {
        series: sum(
            case.series_number == series
            for case in BUCEPHALUS_FAIR_OPENING_SUITE
        )
        for series in range(3, 7)
    }
    assert distribution == {3: 12, 4: 12, 5: 13, 6: 13}


def test_fair_suite_schedules_100_games_as_50_color_swapped_pairs(
    tmp_path: Path,
) -> None:
    config = ExternalMatchConfig(
        pairs=50,
        opening_suite_version=BUCEPHALUS_FAIR_OPENING_SUITE_VERSION,
        opening_case_ids=tuple(
            case.case_id for case in BUCEPHALUS_FAIR_OPENING_SUITE
        ),
        local_depth_series=8,
        local_max_series_per_node=32,
        local_max_generation_positions=100_000_000,
        local_max_game_work_positions=4_000_000_000,
        external_ply_policy=TIMED_ITERATIVE_PLY_POLICY,
        external_wall_timeout_seconds=30.0,
        common_wall_timeout_seconds=30.0,
    )

    jobs = _build_jobs(baseline_profile(), _spec(tmp_path), config)

    assert len(jobs) == 100
    assert len({job.pair_id for job in jobs}) == 50
    for offset in range(0, len(jobs), 2):
        first, second = jobs[offset : offset + 2]
        assert first.opening.case_id == second.opening.case_id
        assert first.history == second.history
        assert (first.local_color, second.local_color) == (
            chess.WHITE,
            chess.BLACK,
        )


def test_color_swapped_pair_groups_real_legal_results(tmp_path: Path) -> None:
    profile = baseline_profile()
    jobs = _build_jobs(profile, _spec(tmp_path), _config())

    def fake_external(state, history, spec, *, search_ply, wall_timeout_seconds):
        assert history[-1] == ("e4d5", "g1f3", "f1b5")
        return _external_result(state, PUBLISHED_MATE, requested_ply=search_ply)

    def fake_local(state, limits, profile):
        assert limits.collect_all_root_scores is False
        assert limits.time_limit_seconds is None
        return _local_result(state, PUBLISHED_MATE)

    records = tuple(
        _play_external_game(
            job,
            external_adapter=fake_external,
            local_analyzer=fake_local,
        )
        for job in jobs
    )
    summary, pairs = _summarize(records)

    assert [record.local_color for record in records] == ["white", "black"]
    assert [record.winner for record in records] == ["bucephalus", "local"]
    assert all(record.result == "0-1" for record in records)
    assert summary["local_game_wdl"] == {"wins": 1, "draws": 0, "losses": 1}
    assert summary["local_pair_wdl"] == {"wins": 0, "draws": 1, "losses": 0}
    assert pairs[0]["result"] == "draw"
    assert records[0].trace[0]["request_script"].endswith("q\n")
    assert records[0].trace[0]["stdout"].startswith("Bucephalus v1.0.0")
    local_trace = records[1].trace[0]
    assert local_trace["promotion_mate_positions"] == 5
    assert local_trace["promotion_mate_setup_states"] == 6
    assert local_trace["promotion_mate_candidates"] == 4
    assert local_trace["promotion_mate_completion_probes"] == 3
    assert local_trace["promotion_mate_mates"] == 1
    assert local_trace["promotion_mate_limit_hits"] == 0
    assert local_trace["promotion_mate_replay_rejects"] == 2


def test_equal_wall_local_plays_deepest_completed_iteration_after_deadline(
    tmp_path: Path,
) -> None:
    job = _build_jobs(
        baseline_profile(), _spec(tmp_path), _timed_config()
    )[1]

    def fake_local(state, limits, profile):
        assert limits.time_limit_seconds == 1.0
        completed = _local_result(state, PUBLISHED_MATE)
        return SimpleNamespace(
            **{
                **vars(completed),
                "completed_depth": 1,
                "timed_out": True,
                "elapsed_seconds": 1.0,
            }
        )

    record = _play_external_game(job, local_analyzer=fake_local)

    assert record.result == "0-1"
    assert record.winner == "local"
    assert record.trace[0]["deadline_completed_iteration_used"] is True


def test_equal_wall_local_internal_selective_limit_plays_legal_move(
    tmp_path: Path,
) -> None:
    job = _build_jobs(
        baseline_profile(), _spec(tmp_path), _timed_config()
    )[1]

    def work_capped(state, limits, profile):
        completed = _local_result(state, PUBLISHED_MATE)
        return SimpleNamespace(
            **{
                **vars(completed),
                "work_limit_reached": True,
                "elapsed_seconds": 0.5,
            }
        )

    record = _play_external_game(job, local_analyzer=work_capped)

    assert record.result == "0-1"
    assert record.winner == "local"
    assert record.technical_failure_owner is None
    assert record.trace[0]["completed_depth_series"] == 1
    assert record.trace[0]["internal_selective_limit_reached"] is True
    assert record.trace[0]["hard_work_reserve_reached"] is False


def test_equal_wall_local_hard_work_reserve_invalidates_game(
    tmp_path: Path,
) -> None:
    job = _build_jobs(
        baseline_profile(), _spec(tmp_path), _timed_config()
    )[1]

    def hard_work_capped(state, limits, profile):
        completed = _local_result(state, PUBLISHED_MATE)
        return SimpleNamespace(
            **{
                **vars(completed),
                "stats": SimpleNamespace(
                    **vars(completed.stats),
                    generation_work_limit_hits=1,
                ),
                "work_limit_reached": True,
                "elapsed_seconds": 0.5,
            }
        )

    record = _play_external_game(job, local_analyzer=hard_work_capped)

    assert record.result == "*"
    assert record.technical_failure_owner == "local"
    assert record.terminal_reason == "technical-local-hard-work-reserve-reached"
    assert record.trace[0]["hard_work_reserve_reached"] is True


def test_equal_wall_local_deadline_without_completed_iteration_is_incomplete(
    tmp_path: Path,
) -> None:
    job = _build_jobs(
        baseline_profile(), _spec(tmp_path), _timed_config()
    )[1]

    def no_completed_iteration(state, limits, profile):
        partial = _local_result(state, PUBLISHED_MATE)
        return SimpleNamespace(
            **{
                **vars(partial),
                "best_series": None,
                "completed_depth": 0,
                "timed_out": True,
                "elapsed_seconds": 1.0,
            }
        )

    record = _play_external_game(job, local_analyzer=no_completed_iteration)

    assert record.result == "*"
    assert record.technical_failure_owner == "local"
    assert record.terminal_reason == (
        "technical-local-deadline-no-complete-iteration"
    )


def test_equal_wall_rejects_local_call_that_overruns_wrapper(
    tmp_path: Path, monkeypatch
) -> None:
    job = _build_jobs(
        baseline_profile(), _spec(tmp_path), _timed_config()
    )[1]
    ticks = iter((10.0, 11.0 + COMMON_WALL_OVERRUN_GRACE_SECONDS + 0.01))
    monkeypatch.setattr(
        "benchmarks.bucephalus_fair_rematch.time.perf_counter",
        lambda: next(ticks),
    )

    record = _play_external_game(
        job,
        local_analyzer=lambda state, limits, profile: _local_result(
            state, PUBLISHED_MATE
        ),
    )

    assert record.result == "*"
    assert record.terminal_reason == "technical-local-common-wall-overrun"


def test_equal_wall_plays_and_labels_selected_depth_zero_fallback(
    tmp_path: Path,
) -> None:
    job = _build_jobs(
        baseline_profile(), _spec(tmp_path), _timed_config()
    )[1]

    def depth_zero(state, limits, profile):
        completed = _local_result(state, PUBLISHED_MATE)
        return SimpleNamespace(
            **{
                **vars(completed),
                "completed_depth": 0,
                "timed_out": False,
            }
        )

    record = _play_external_game(job, local_analyzer=depth_zero)

    assert record.result == "0-1"
    assert record.winner == "local"
    assert record.trace[0]["move_only_liveness_fallback"] is True


def test_operator_interrupt_is_not_recorded_as_an_engine_failure(
    tmp_path: Path,
) -> None:
    job = _build_jobs(
        baseline_profile(), _spec(tmp_path), _timed_config()
    )[1]

    def interrupted(state, limits, profile):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _play_external_game(job, local_analyzer=interrupted)


def test_equal_wall_bucephalus_plays_deepest_completed_iteration_after_deadline(
    tmp_path: Path,
) -> None:
    job = _build_jobs(
        baseline_profile(), _spec(tmp_path), _timed_config()
    )[0]

    def fake_external(state, history, spec, *, wall_timeout_seconds):
        assert wall_timeout_seconds == 1.0
        return _external_result(
            state,
            PUBLISHED_MATE,
            requested_ply=BUCEPHALUS_MAX_PLY,
            completed_ply=state.moves_available,
            adapter_version=BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
            deadline_reached=True,
        )

    record = _play_external_game(job, external_adapter=fake_external)

    assert record.result == "0-1"
    assert record.winner == "bucephalus"
    assert record.trace[0]["requested_micro_ply"] == BUCEPHALUS_MAX_PLY
    assert record.trace[0]["completed_micro_ply"] == 4
    assert record.trace[0]["deadline_completed_iteration_used"] is True


def test_equal_wall_bucephalus_plays_flushed_iteration_after_process_exit(
    tmp_path: Path,
) -> None:
    job = _build_jobs(
        baseline_profile(), _spec(tmp_path), _timed_config()
    )[0]

    def fake_external(state, history, spec, *, wall_timeout_seconds):
        return _external_result(
            state,
            PUBLISHED_MATE,
            requested_ply=BUCEPHALUS_MAX_PLY,
            completed_ply=state.moves_available,
            adapter_version=BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
            process_exit_code=0xC0000005,
            process_exit_recovered=True,
        )

    record = _play_external_game(job, external_adapter=fake_external)

    assert record.result == "0-1"
    assert record.winner == "bucephalus"
    assert record.trace[0]["process_exit_code"] == 0xC0000005
    assert record.trace[0]["process_exit_recovered"] is True


def test_equal_wall_accepts_early_checking_external_iteration(
    tmp_path: Path,
) -> None:
    history = (
        ("e2e4",),
        ("g8f6", "f6g4"),
        ("f1a6", "a6b5", "b2b3"),
        ("g4e5", "e5c6", "a7a5", "d7d5"),
    )
    state = replay_series_history(history)
    opening = OpeningCase(
        case_id="series-five-early-check",
        fen=state.board.fen(en_passant="fen"),
        series_number=state.series_number,
        quiet_series=state.quiet_series,
        ep_targets=tuple(chess.square_name(square) for square in state.ep_targets),
        source="regression",
    )
    config = _timed_config(emergency_max_series=5)
    job = ExternalGameJob(
        game_id="early-check-game",
        pair_id="early-check-pair",
        pair_index=0,
        swap_index=0,
        opening=opening,
        history=history,
        local_color=chess.BLACK,
        local_profile=baseline_profile(),
        external_spec=_spec(tmp_path),
        config=config,
    )

    def early_check(state, history, spec, *, wall_timeout_seconds):
        return _external_result(
            state,
            ("b5c6",),
            requested_ply=BUCEPHALUS_MAX_PLY,
            completed_ply=1,
            adapter_version=BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
            deadline_reached=True,
        )

    record = _play_external_game(job, external_adapter=early_check)

    assert record.terminal_reason != "technical-external-provenance-mismatch"
    assert record.trace[0]["played"] is True
    assert record.trace[0]["selected_series"] == "b5c6"


def test_equal_wall_rejects_external_call_that_overruns_wrapper(
    tmp_path: Path, monkeypatch
) -> None:
    job = _build_jobs(
        baseline_profile(), _spec(tmp_path), _timed_config()
    )[0]
    ticks = iter((20.0, 21.0 + COMMON_WALL_OVERRUN_GRACE_SECONDS + 0.01))
    monkeypatch.setattr(
        "benchmarks.bucephalus_fair_rematch.time.perf_counter",
        lambda: next(ticks),
    )

    def fake_external(state, history, spec, *, wall_timeout_seconds):
        return _external_result(
            state,
            PUBLISHED_MATE,
            requested_ply=BUCEPHALUS_MAX_PLY,
            completed_ply=state.moves_available,
            adapter_version=BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
            deadline_reached=True,
        )

    record = _play_external_game(job, external_adapter=fake_external)

    assert record.result == "*"
    assert record.terminal_reason == "technical-external-common-wall-overrun"


def test_equal_wall_rejects_partial_bucephalus_depth_without_deadline(
    tmp_path: Path,
) -> None:
    job = _build_jobs(
        baseline_profile(), _spec(tmp_path), _timed_config()
    )[0]

    def inconsistent_external(state, history, spec, *, wall_timeout_seconds):
        return _external_result(
            state,
            PUBLISHED_MATE,
            requested_ply=BUCEPHALUS_MAX_PLY,
            completed_ply=state.moves_available,
            adapter_version=BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
            deadline_reached=False,
        )

    record = _play_external_game(job, external_adapter=inconsistent_external)

    assert record.result == "*"
    assert record.terminal_reason == "technical-external-provenance-mismatch"


@pytest.mark.parametrize(
    ("process_exit_code", "deadline_reached"),
    ((None, False), (0, False), (0xC0000005, True)),
)
def test_equal_wall_rejects_inconsistent_process_exit_recovery_provenance(
    tmp_path: Path,
    process_exit_code: int | None,
    deadline_reached: bool,
) -> None:
    job = _build_jobs(
        baseline_profile(), _spec(tmp_path), _timed_config()
    )[0]

    def inconsistent_external(state, history, spec, *, wall_timeout_seconds):
        return _external_result(
            state,
            PUBLISHED_MATE,
            requested_ply=BUCEPHALUS_MAX_PLY,
            completed_ply=state.moves_available,
            adapter_version=BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
            deadline_reached=deadline_reached,
            process_exit_code=process_exit_code,
            process_exit_recovered=True,
        )

    record = _play_external_game(job, external_adapter=inconsistent_external)

    assert record.result == "*"
    assert record.terminal_reason == "technical-external-provenance-mismatch"


def test_equal_wall_rejects_unrecovered_nonzero_process_exit(
    tmp_path: Path,
) -> None:
    job = _build_jobs(
        baseline_profile(), _spec(tmp_path), _timed_config()
    )[0]

    def inconsistent_external(state, history, spec, *, wall_timeout_seconds):
        return _external_result(
            state,
            PUBLISHED_MATE,
            requested_ply=BUCEPHALUS_MAX_PLY,
            completed_ply=state.moves_available,
            adapter_version=BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
            process_exit_code=0xC0000005,
            process_exit_recovered=False,
        )

    record = _play_external_game(job, external_adapter=inconsistent_external)

    assert record.result == "*"
    assert record.terminal_reason == "technical-external-provenance-mismatch"


def test_external_timeout_is_incomplete_not_a_loss(tmp_path: Path) -> None:
    job = _build_jobs(baseline_profile(), _spec(tmp_path), _config())[0]

    def timeout(*args, **kwargs):
        raise ExternalEngineTimeout("deadline")

    record = _play_external_game(job, external_adapter=timeout)
    summary, pairs = _summarize((record,))

    assert record.result == "*"
    assert record.winner is None
    assert record.technical_failure_owner == "bucephalus"
    assert record.terminal_reason == "technical-external-timeout"
    assert summary["completed_games"] == 0
    assert summary["incomplete_games"] == 1
    assert pairs[0]["result"] == "incomplete"


def test_fake_adapter_cannot_bypass_authoritative_series_replay(
    tmp_path: Path,
) -> None:
    job = _build_jobs(baseline_profile(), _spec(tmp_path), _config())[0]

    def illegal(state, history, spec, *, search_ply, wall_timeout_seconds):
        fabricated = SeriesResult(
            moves=("a7a6",),
            san=("a6",),
            final_state=state,
        )
        return ExternalAnalysis(
            fabricated,
            search_ply,
            search_ply,
            "0.00",
            0.01,
            PINNED_HASH,
            "test-upstream",
            BUCEPHALUS_ADAPTER_VERSION,
            "request",
            "stdout",
            "",
        )

    record = _play_external_game(job, external_adapter=illegal)

    assert record.result == "*"
    assert record.terminal_reason == (
        "technical-bucephalus-illegal-or-inconsistent-series"
    )
    assert record.series_played == 0


def test_every_played_series_is_appended_to_external_replay_history(
    tmp_path: Path,
) -> None:
    config = _config("initial", emergency_max_series=2)
    job = _build_jobs(baseline_profile(), _spec(tmp_path), config)[0]

    def fake_local(state, limits, profile):
        return _local_result(state, ("e2e4",))

    def fake_external(state, history, spec, *, search_ply, wall_timeout_seconds):
        assert history == (("e2e4",),)
        return _external_result(
            state,
            ("a7a6", "b7b6"),
            requested_ply=search_ply,
        )

    record = _play_external_game(
        job,
        external_adapter=fake_external,
        local_analyzer=fake_local,
    )

    assert record.result == "*"
    assert record.terminal_reason == "technical-emergency-series-watchdog-exhausted"
    assert [item["authoritative_series"] for item in record.trace] == [
        "e2e4",
        "a7a6/b7b6",
    ]
    assert record.trace[-1]["canonical_history_after"] == [
        ["e2e4"],
        ["a7a6", "b7b6"],
    ]
    assert record.series_played == 2


def test_default_worker_memory_includes_external_process_and_local_search() -> None:
    assert DEFAULT_EXTERNAL_MATCH_MEMORY_PER_WORKER_MB == 768


def test_git_provenance_resolves_the_actual_repository() -> None:
    repository = Path(__file__).resolve().parents[1]
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    provenance = _git_source_provenance()

    assert provenance["repository_detected"] is True
    assert provenance["head_commit"] == expected


def test_only_fair_equal_wall_100_game_protocol_can_support_superiority() -> None:
    complete_winning_summary = {
        "scheduled_games": 100,
        "completed_games": 100,
        "scheduled_pairs": 50,
        "completed_pairs": 50,
        "local_pair_wdl": {"wins": 20, "draws": 29, "losses": 1},
        "paired_sign_test": {"two_sided_exact_binomial_p": 0.0001},
    }
    fair = ExternalMatchConfig(
        pairs=50,
        seed=20260827,
        opening_suite_version=BUCEPHALUS_FAIR_OPENING_SUITE_VERSION,
        opening_case_ids=tuple(
            case.case_id for case in BUCEPHALUS_FAIR_OPENING_SUITE
        ),
        local_depth_series=5,
        local_max_series_per_node=32,
        local_max_generation_positions=4_000_000_000,
        local_max_game_work_positions=100_000_000_000,
        external_ply_policy=TIMED_ITERATIVE_PLY_POLICY,
        external_wall_timeout_seconds=30.0,
        common_wall_timeout_seconds=30.0,
    )
    asymmetric = ExternalMatchConfig(
        pairs=50,
        seed=20260827,
        opening_suite_version=BUCEPHALUS_FAIR_OPENING_SUITE_VERSION,
        opening_case_ids=fair.opening_case_ids,
        local_depth_series=5,
        local_max_series_per_node=32,
        local_max_generation_positions=4_000_000_000,
        local_max_game_work_positions=100_000_000_000,
        external_wall_timeout_seconds=30.0,
    )

    assert _superiority_gate(
        fair,
        complete_winning_summary,
        approved_bucephalus_identity=True,
        identity_stable=True,
    ) == (True, True)
    assert _superiority_gate(
        fair,
        complete_winning_summary,
        approved_bucephalus_identity=False,
        identity_stable=True,
    ) == (True, False)
    assert _superiority_gate(
        fair,
        complete_winning_summary,
        approved_bucephalus_identity=True,
        identity_stable=False,
    ) == (True, False)
    assert _superiority_gate(
        asymmetric,
        complete_winning_summary,
        approved_bucephalus_identity=True,
        identity_stable=True,
    ) == (False, False)


def test_benchmark_cli_freezes_the_fair_timed_defaults() -> None:
    args = build_parser().parse_args(
        [
            "bucephalus-flushed.exe",
            "--sha256",
            PINNED_HASH,
            "--upstream-commit",
            "0e11fcdc",
            "--external-build-receipt",
            "build-receipt.json",
            "--journal-directory",
            "journal",
        ]
    )

    assert args.pairs == 50
    assert args.depth == 8
    assert args.common_move_seconds == 30.0
    assert args.workers == 1


def test_build_receipt_binds_executable_upstream_and_repository_patch(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "test-bucephalus.exe"
    executable.write_bytes(b"unit-test-executable")
    executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    repository = Path(__file__).resolve().parents[1]
    payload = json.loads(
        (
            repository
            / "benchmarks"
            / "protocols"
            / "bucephalus-flushed-0e11fcdc-build-receipt.json"
        ).read_text(encoding="utf-8")
    )
    payload["opponent"]["upstream_commit"] = "test-upstream"
    payload["output"]["bytes"] = executable.stat().st_size
    payload["output"]["sha256"] = executable_sha256
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    spec = BucephalusSpec(
        executable,
        executable_sha256,
        upstream_commit="test-upstream",
    )

    receipt = _load_external_build_receipt(
        receipt_path,
        external_spec=spec,
        executable=executable,
        executable_hash=executable_sha256,
    )

    assert receipt["canonical_sha256"] != APPROVED_BUCEPHALUS_BUILD_RECEIPT_SHA256
    assert receipt["approved_for_named_bucephalus_claim"] is False
    payload["output"]["sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="output SHA-256"):
        _load_external_build_receipt(
            receipt_path,
            external_spec=spec,
            executable=executable,
            executable_hash=executable_sha256,
        )


def test_atomic_game_journal_resumes_only_the_exact_frozen_protocol(
    tmp_path: Path,
) -> None:
    profile = baseline_profile()
    spec = _spec(tmp_path)
    config = _config()
    jobs = _build_jobs(profile, spec, config)
    protocol = _journal_protocol(
        profile,
        spec,
        config,
        jobs,
        executable=spec.executable,
        executable_hash=spec.sha256,
        resources=_resources(),
        identity_snapshot=_identity_snapshot(),
    )
    assert protocol["resource_execution_controls"]["workers"] == 1
    assert "backend" in protocol["local_engine"]
    assert len(protocol["benchmark_harness"]["artifact_set_sha256"]) == 64
    journal = tmp_path / "journal"

    root, protocol_sha256, existing = _prepare_journal(
        journal, protocol, jobs, resume=False
    )
    assert root == journal.resolve()
    assert existing == {}

    def timeout(*args, **kwargs):
        raise ExternalEngineTimeout("deadline")

    record = _play_external_game(jobs[0], external_adapter=timeout)
    write_external_match_report(
        {
            "format": "spc-bucephalus-match-journal-v1",
            "protocol_sha256": protocol_sha256,
            "record": record.as_dict(),
        },
        root / "games" / f"{record.game_id}.json",
    )

    _, resumed_sha256, resumed = _prepare_journal(
        journal, protocol, jobs, resume=True
    )
    assert resumed_sha256 == protocol_sha256
    assert resumed[record.game_id].as_dict() == record.as_dict()
    with pytest.raises(ValueError, match="pass resume=True"):
        _prepare_journal(journal, protocol, jobs, resume=False)

    changed_protocol = {**protocol, "match_id": "different"}
    with pytest.raises(ValueError, match="does not match"):
        _prepare_journal(journal, changed_protocol, jobs, resume=True)

    changed_resources = _journal_protocol(
        profile,
        spec,
        config,
        jobs,
        executable=spec.executable,
        executable_hash=spec.sha256,
        resources=_resources(workers=2),
        identity_snapshot=_identity_snapshot(),
    )
    with pytest.raises(ValueError, match="does not match"):
        _prepare_journal(journal, changed_resources, jobs, resume=True)
