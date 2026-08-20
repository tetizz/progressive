from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

import pytest

from scottish_progressive.external import (
    BUCEPHALUS_ADAPTER_VERSION,
    BUCEPHALUS_OPENING_HISTORIES_V1,
    BucephalusSpec,
    ExternalEngineConfigurationError,
    ExternalEngineHashMismatch,
    ExternalEngineProtocolError,
    ExternalEngineTimeout,
    analyze_bucephalus,
    replay_series_history,
)
from scottish_progressive.league import OPENING_SUITE
from scottish_progressive.model import Outcome, ProgressiveState


PUBLISHED_HISTORY = (
    ("e2e4",),
    ("d7d5", "g8f6"),
    ("e4d5", "g1f3", "f1b5"),
)


def _dummy_spec(tmp_path: Path) -> BucephalusSpec:
    executable = tmp_path / "user-supplied-bucephalus.exe"
    executable.write_bytes(b"test executable fingerprint only")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    return BucephalusSpec(executable, digest, upstream_commit="test-commit")


def _published_stdout(line: str) -> str:
    return (
        "Bucephalus v1.0.0\n"
        "Side to move: B  Length of Series: 4  Count in Series: 1\n"
        "[PLY  1][SCORE  0.00][TIME 0 m 0.00 s][LINE: c7-c6 ]\n"
        f"[PLY  4][SCORE *MATE*][TIME 0 m 0.06 s][LINE: {line} ]\n"
    )


def test_all_league_openings_have_exact_canonical_replay_histories() -> None:
    assert set(BUCEPHALUS_OPENING_HISTORIES_V1) == {
        case.case_id for case in OPENING_SUITE
    }
    for case in OPENING_SUITE:
        replayed = replay_series_history(
            BUCEPHALUS_OPENING_HISTORIES_V1[case.case_id]
        )
        assert replayed.position_hash == case.state().position_hash


def test_one_shot_replay_parses_and_validates_full_mating_series(
    tmp_path: Path, monkeypatch
) -> None:
    state = replay_series_history(PUBLISHED_HISTORY)
    spec = _dummy_spec(tmp_path)
    observed: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=_published_stdout("c7-c6 d8-b6 f6-e4 b6xf2"),
            stderr="",
        )

    monkeypatch.setattr("scottish_progressive.external.subprocess.run", fake_run)
    analysis = analyze_bucephalus(
        state,
        PUBLISHED_HISTORY,
        spec,
        search_ply=4,
        wall_timeout_seconds=2.0,
    )

    assert analysis.best_series.machine_notation == "c7c6/d8b6/f6e4/b6f2"
    assert analysis.best_series.outcome == Outcome.CHECKMATE
    assert analysis.score_text == "*MATE*"
    assert analysis.adapter_version == BUCEPHALUS_ADAPTER_VERSION
    assert analysis.executable_sha256 == spec.sha256
    assert analysis.upstream_commit == "test-commit"
    assert analysis.request_script.endswith("p\ne\n4\nt\nq\n")
    assert analysis.request_script.splitlines().count("m") == 6
    assert "shell" not in observed["kwargs"]


def test_hash_mismatch_stops_before_process_launch(tmp_path: Path, monkeypatch) -> None:
    state = replay_series_history(PUBLISHED_HISTORY)
    executable = tmp_path / "bucephalus.exe"
    executable.write_bytes(b"wrong")
    spec = BucephalusSpec(executable, "0" * 64)
    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not execute a hash-mismatched binary")

    monkeypatch.setattr("scottish_progressive.external.subprocess.run", fake_run)
    with pytest.raises(ExternalEngineHashMismatch):
        analyze_bucephalus(
            state,
            PUBLISHED_HISTORY,
            spec,
            search_ply=4,
            wall_timeout_seconds=2.0,
        )
    assert not called


def test_timeout_is_a_technical_failure_not_a_partial_move(
    tmp_path: Path, monkeypatch
) -> None:
    state = replay_series_history(PUBLISHED_HISTORY)
    spec = _dummy_spec(tmp_path)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0], kwargs["timeout"], output=_published_stdout("c7-c6")
        )

    monkeypatch.setattr("scottish_progressive.external.subprocess.run", fake_run)
    with pytest.raises(ExternalEngineTimeout):
        analyze_bucephalus(
            state,
            PUBLISHED_HISTORY,
            spec,
            search_ply=4,
            wall_timeout_seconds=0.25,
        )


def test_incomplete_or_illegal_pv_is_rejected(tmp_path: Path, monkeypatch) -> None:
    state = replay_series_history(PUBLISHED_HISTORY)
    spec = _dummy_spec(tmp_path)

    monkeypatch.setattr(
        "scottish_progressive.external.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout=_published_stdout("c7-c6 d8-b6"),
            stderr="",
        ),
    )
    with pytest.raises(
        ExternalEngineProtocolError,
        match="did not contain a complete root series",
    ):
        analyze_bucephalus(
            state,
            PUBLISHED_HISTORY,
            spec,
            search_ply=4,
            wall_timeout_seconds=2.0,
        )


def test_history_must_reach_requested_state_before_launch(
    tmp_path: Path, monkeypatch
) -> None:
    spec = _dummy_spec(tmp_path)
    monkeypatch.setattr(
        "scottish_progressive.external.subprocess.run",
        lambda *args, **kwargs: pytest.fail("mismatched state must not launch"),
    )
    with pytest.raises(
        ExternalEngineConfigurationError,
        match="does not reach",
    ):
        analyze_bucephalus(
            ProgressiveState.initial(),
            PUBLISHED_HISTORY,
            spec,
            search_ply=4,
            wall_timeout_seconds=2.0,
        )
