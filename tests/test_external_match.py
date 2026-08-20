from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import chess

from scottish_progressive.external import (
    BUCEPHALUS_ADAPTER_VERSION,
    BucephalusSpec,
    ExternalAnalysis,
    ExternalEngineTimeout,
)
from scottish_progressive.external_match import (
    DEFAULT_EXTERNAL_MATCH_MEMORY_PER_WORKER_MB,
    ExternalMatchConfig,
    _build_jobs,
    _play_external_game,
    _summarize,
)
from scottish_progressive.model import ProgressiveState, SeriesResult
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series


PINNED_HASH = "1" * 64
PUBLISHED_MATE = ("c7c6", "d8b6", "f6e4", "b6f2")


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
) -> ExternalAnalysis:
    selected = play_series(state, moves)
    return ExternalAnalysis(
        best_series=selected,
        requested_ply=requested_ply,
        completed_ply=requested_ply,
        score_text="*MATE*" if selected.outcome else "0.00",
        elapsed_seconds=0.01,
        executable_sha256=PINNED_HASH,
        upstream_commit="test-upstream",
        adapter_version=BUCEPHALUS_ADAPTER_VERSION,
        request_script="p\ne\n4\nt\nq\n",
        stdout="Bucephalus v1.0.0\n[PLY 4]...\n",
        stderr="",
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
