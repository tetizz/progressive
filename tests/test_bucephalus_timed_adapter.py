from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from benchmarks.bucephalus_timed_adapter import (
    BUCEPHALUS_ADAPTER_VERSION,
    BUCEPHALUS_MAX_PLY,
    BUCEPHALUS_OPENING_HISTORIES_V1,
    BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
    BucephalusSpec,
    ExternalEngineConfigurationError,
    ExternalEngineHashMismatch,
    ExternalEngineProtocolError,
    ExternalEngineTimeout,
    analyze_bucephalus,
    analyze_bucephalus_timed_iterative,
    replay_series_history,
    _run_bucephalus_live_checkpoint,
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


SERIES_NINE_HISTORY = (
    ("c2c4",),
    ("d7d6", "c8h3"),
    ("e2e3", "g2h3", "a2a4"),
    ("c7c5", "d8a5", "a5a4", "a4d1"),
    ("e1d1", "a1a7", "a7a8", "d2d3", "a8b8"),
    ("e8d7", "d7c7", "g8f6", "f6e4", "c7b8", "e4f2"),
    ("d1e1", "b2b4", "b4c5", "c5d6", "d6e7", "e1f2", "e7f8q"),
    ("h8f8", "b7b5", "b5c4", "c4d3", "d3d2", "d2c1q", "b8b7", "f8e8"),
)


def _timed_stdout(series: int, count: int, records: tuple[tuple[int, str], ...]) -> str:
    lines = [
        "Bucephalus v1.0.0",
        f"Side to move: W  Length of Series: {series}  Count in Series: {count}",
    ]
    lines.extend(
        f"[PLY {ply:2d}][SCORE 0.00][TIME 0 m 1.00 s][LINE: {line} ]"
        for ply, line in records
    )
    return "\n".join(lines) + "\n"


def test_series_nine_stitches_replay_validated_ply7_then_continuation(
    tmp_path: Path, monkeypatch
) -> None:
    state = replay_series_history(SERIES_NINE_HISTORY)
    spec = _dummy_spec(tmp_path)
    prefix = "b1-a3 a3-b1 b1-a3 a3-b1 b1-a3 a3-b1 b1-a3"
    anchor = ("b1a3", "a3b1", "b1a3", "a3b1", "b1a3", "a3b1", "b1a3", "a3b1", "b1a3")
    calls: list[dict[str, object]] = []
    clock = {"now": 0.0}

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        if kwargs["input"].endswith("p\ne\n1\nt\nq\n"):
            index = len([call for call in calls if call["input"].endswith("p\ne\n1\nt\nq\n")]) - 1
            clock["now"] += 0.05
            return subprocess.CompletedProcess(
                args[0], 0,
                stdout=_timed_stdout(9, index + 1, ((1, anchor[index][:2] + "-" + anchor[index][2:]),)),
                stderr="anchor",
            )
        if kwargs["input"].endswith("p\ne\n29\nt\nq\n"):
            clock["now"] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(
                args[0], kwargs["timeout"],
                output=_timed_stdout(9, 1, ((7, prefix),)), stderr="stage one",
            )
        assert kwargs["input"].count("\nm\n") >= 7
        clock["now"] += 1.0
        return subprocess.CompletedProcess(
            args[0], 0,
            stdout=_timed_stdout(9, 8, ((2, "a3-b1 b1-a3"),)),
            stderr="stage two",
        )

    monkeypatch.setattr(
        "benchmarks.bucephalus_timed_adapter.time.perf_counter",
        lambda: clock["now"],
    )
    monkeypatch.setattr("benchmarks.bucephalus_timed_adapter.subprocess.run", fake_run)

    def fake_live(*args, **kwargs):
        clock["now"] += kwargs["soft_timeout_seconds"]
        return (
            _timed_stdout(9, 1, ((7, prefix),)), "", True, -9,
            kwargs["soft_timeout_seconds"], "soft-checkpoint-incomplete", 4242, False,
        )

    monkeypatch.setattr(
        "benchmarks.bucephalus_timed_adapter._run_bucephalus_live_checkpoint",
        fake_live,
    )

    analysis = analyze_bucephalus_timed_iterative(
        state, SERIES_NINE_HISTORY, spec, wall_timeout_seconds=120.0
    )

    assert analysis.best_series.machine_notation == (
        "b1a3/a3b1/b1a3/a3b1/b1a3/a3b1/b1a3/a3b1/b1a3"
    )
    assert len(calls) == 10
    assert calls[9]["timeout"] > 10.0
    assert analysis.elapsed_seconds < 120.0
    assert len(analysis.continuation_stages) == 11
    assert analysis.continuation_stages[9].emitted_prefix == tuple(
        move.replace("-", "") for move in prefix.split()
    )
    assert analysis.continuation_stages[10].starting_prefix == (
        "b1a3", "a3b1", "b1a3", "a3b1", "b1a3", "a3b1", "b1a3"
    )
    assert analysis.continuation_stages[10].completed_ply == 2
    assert analysis.selection_mode == "deep-prefix-continuation"
    assert analysis.deadline_reached is True
    assert analysis.global_deadline_reached is False
    assert analysis.continuation_stages[9].stop_reason == (
        "soft-checkpoint-incomplete"
    )

    from benchmarks.bucephalus_fair_rematch import (
        _continuation_chain_selects_analysis,
    )

    assert _continuation_chain_selects_analysis(
        state, SERIES_NINE_HISTORY, analysis, 120.0
    )
    altered = list(analysis.continuation_stages)
    altered[-1] = replace(altered[-1], emitted_prefix=("a3b1",))
    assert not _continuation_chain_selects_analysis(
        state,
        SERIES_NINE_HISTORY,
        replace(analysis, continuation_stages=tuple(altered)),
        120.0,
    )
    altered = list(analysis.continuation_stages)
    altered[9] = replace(altered[9], process_id=-1)
    assert not _continuation_chain_selects_analysis(
        state,
        SERIES_NINE_HISTORY,
        replace(analysis, continuation_stages=tuple(altered)),
        120.0,
    )
    altered = list(analysis.continuation_stages)
    altered[-1] = replace(altered[-1], stdout="Bucephalus v1.0.0\n")
    assert not _continuation_chain_selects_analysis(
        state,
        SERIES_NINE_HISTORY,
        replace(analysis, continuation_stages=tuple(altered)),
        120.0,
    )


def test_series_nine_continuation_falls_back_to_bucephalus_only_anchor(
    tmp_path: Path, monkeypatch
) -> None:
    state = replay_series_history(SERIES_NINE_HISTORY)
    spec = _dummy_spec(tmp_path)
    prefix = "b1-a3 a3-b1 b1-a3 a3-b1 b1-a3 a3-b1 b1-a3"
    anchor = ("b1a3", "a3b1", "b1a3", "a3b1", "b1a3", "a3b1", "b1a3", "a3b1", "b1a3")
    clock = {"now": 0.0, "anchor": 0, "deep": 0}

    def fake_run(*args, **kwargs):
        if kwargs["input"].endswith("p\ne\n1\nt\nq\n") and clock["anchor"] < 9:
            index = clock["anchor"]
            clock["anchor"] += 1
            clock["now"] += 0.05
            return subprocess.CompletedProcess(
                args[0], 0,
                stdout=_timed_stdout(9, index + 1, ((1, anchor[index][:2] + "-" + anchor[index][2:]),)),
                stderr="",
            )
        if kwargs["input"].endswith("p\ne\n1\nt\nq\n"):
            clock["now"] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(
                args[0], kwargs["timeout"], output="Bucephalus v1.0.0\n", stderr=""
            )
        clock["deep"] += 1
        clock["now"] += kwargs["timeout"]
        output = _timed_stdout(9, 1, ((7, prefix),)) if clock["deep"] == 1 else "Bucephalus v1.0.0\n"
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=output, stderr="")

    monkeypatch.setattr(
        "benchmarks.bucephalus_timed_adapter.time.perf_counter",
        lambda: clock["now"],
    )
    monkeypatch.setattr("benchmarks.bucephalus_timed_adapter.subprocess.run", fake_run)
    monkeypatch.setattr(
        "benchmarks.bucephalus_timed_adapter._run_bucephalus_live_checkpoint",
        lambda *args, **kwargs: (
            _timed_stdout(9, 1, ((7, prefix),)), "", True, -9,
            kwargs["soft_timeout_seconds"], "soft-checkpoint-incomplete", 5252, False,
        ),
    )
    analysis = analyze_bucephalus_timed_iterative(
        state, SERIES_NINE_HISTORY, spec, wall_timeout_seconds=120.0
    )
    assert analysis.best_series.machine_notation == "/".join(anchor)
    assert analysis.selection_mode == "anchor-fallback"
    assert analysis.continuation_stages[-1].usable is False


def test_series_nine_fails_when_bucephalus_cannot_emit_a_legal_anchor_move(
    tmp_path: Path, monkeypatch
) -> None:
    state = replay_series_history(SERIES_NINE_HISTORY)
    spec = _dummy_spec(tmp_path)
    launched = 0

    def fake_run(*args, **kwargs):
        nonlocal launched
        launched += 1
        return subprocess.CompletedProcess(
            args[0], 0,
            stdout=_timed_stdout(9, 1, ((1, "a2-a3"),)),
            stderr="",
        )

    monkeypatch.setattr("benchmarks.bucephalus_timed_adapter.subprocess.run", fake_run)
    with pytest.raises(ExternalEngineTimeout, match="engine-native anchor failed"):
        analyze_bucephalus_timed_iterative(
            state, SERIES_NINE_HISTORY, spec, wall_timeout_seconds=120.0
        )
    assert launched == 1


def test_timed_turn_uses_terminal_ply1_anchor_before_deep_search(
    tmp_path: Path, monkeypatch
) -> None:
    state = replay_series_history(PUBLISHED_HISTORY)
    spec = _dummy_spec(tmp_path)
    moves = ("c7c6", "d8b6", "f6e4", "b6f2")
    launched = 0

    def fake_run(*args, **kwargs):
        nonlocal launched
        move = moves[launched]
        launched += 1
        return subprocess.CompletedProcess(
            args[0], 0,
            stdout=(
                "Bucephalus v1.0.0\n"
                f"Side to move: B  Length of Series: 4  Count in Series: {launched}\n"
                f"[PLY  1][SCORE *MATE*][TIME 0 m 0.01 s][LINE: {move[:2]}x{move[2:]} ]\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("benchmarks.bucephalus_timed_adapter.subprocess.run", fake_run)
    analysis = analyze_bucephalus_timed_iterative(
        state, PUBLISHED_HISTORY, spec, wall_timeout_seconds=120.0
    )
    assert analysis.best_series.outcome == Outcome.CHECKMATE
    assert analysis.selection_mode == "anchor-terminal"
    assert analysis.process_count == 4
    assert launched == 4


def test_complete_soft_checkpoint_keeps_same_process_to_hard_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    state = replay_series_history(SERIES_NINE_HISTORY)
    spec = _dummy_spec(tmp_path)
    line = ("b1a3", "a3b1", "b1a3", "a3b1", "b1a3", "a3b1", "b1a3", "a3b1", "b1a3")
    clock = {"now": 0.0, "anchor": 0}

    def fake_run(*args, **kwargs):
        if kwargs["input"].endswith("p\ne\n1\nt\nq\n"):
            index = clock["anchor"]
            clock["anchor"] += 1
            clock["now"] += 0.02
            move = line[index]
            return subprocess.CompletedProcess(
                args[0], 0,
                stdout=_timed_stdout(9, index + 1, ((1, move[:2] + "-" + move[2:]),)),
                stderr="",
            )
        raise AssertionError("deep search must use the live checkpoint helper")

    monkeypatch.setattr("benchmarks.bucephalus_timed_adapter.time.perf_counter", lambda: clock["now"])
    monkeypatch.setattr("benchmarks.bucephalus_timed_adapter.subprocess.run", fake_run)

    def fake_live(*args, **kwargs):
        clock["now"] += kwargs["hard_timeout_seconds"]
        stdout = _timed_stdout(
            9, 1, ((10, " ".join(move[:2] + "-" + move[2:] for move in line)),)
        )
        return (
            stdout, "drained stderr", True, -9,
            kwargs["hard_timeout_seconds"], "hard-deadline", 6262, True,
        )

    monkeypatch.setattr(
        "benchmarks.bucephalus_timed_adapter._run_bucephalus_live_checkpoint",
        fake_live,
    )
    analysis = analyze_bucephalus_timed_iterative(
        state, SERIES_NINE_HISTORY, spec, wall_timeout_seconds=120.0
    )
    assert analysis.selection_mode == "deep-complete-live"
    assert analysis.continuation_stages[-1].purpose == "deep-max-ply"
    assert analysis.continuation_stages[-1].usable is True
    assert analysis.continuation_stages[-1].same_process_continued is True
    assert analysis.continuation_stages[-1].process_id == 6262
    assert analysis.completed_ply == 10
    assert analysis.process_count == 10


def test_live_checkpoint_drains_small_partial_transcript_then_kills_for_suffix(
    tmp_path: Path,
) -> None:
    before = {thread.ident for thread in threading.enumerate()}
    result = _run_bucephalus_live_checkpoint(
        Path(sys.executable),
        "import sys,time\nprint('Bucephalus v test PLY-partial', flush=True)\n"
        "print('stderr-partial', file=sys.stderr, flush=True)\ntime.sleep(5)\n",
        cwd=tmp_path,
        creationflags=0,
        soft_timeout_seconds=0.15,
        hard_timeout_seconds=0.6,
        snapshot_has_complete_root=lambda stdout: "complete-root" in stdout,
    )
    assert "PLY-partial" in result[0]
    assert "stderr-partial" in result[1]
    assert result[2] is True
    assert result[5] == "soft-checkpoint-incomplete"
    assert result[7] is False
    assert not {
        thread.ident for thread in threading.enumerate()
    }.difference(before)


def test_live_checkpoint_keeps_same_pid_and_drains_until_hard_deadline(
    tmp_path: Path,
) -> None:
    result = _run_bucephalus_live_checkpoint(
        Path(sys.executable),
        "import sys,time\nprint('complete-root', flush=True)\n"
        "time.sleep(.25)\nprint('deeper-complete-root', flush=True)\n"
        "print('deep-stderr', file=sys.stderr, flush=True)\ntime.sleep(5)\n",
        cwd=tmp_path,
        creationflags=0,
        soft_timeout_seconds=0.15,
        hard_timeout_seconds=0.65,
        snapshot_has_complete_root=lambda stdout: "complete-root" in stdout,
    )
    assert "deeper-complete-root" in result[0]
    assert "deep-stderr" in result[1]
    assert result[2] is True
    assert result[5] == "hard-deadline"
    assert isinstance(result[6], int)
    assert result[7] is True


def test_live_checkpoint_records_clean_early_process_exit(tmp_path: Path) -> None:
    result = _run_bucephalus_live_checkpoint(
        Path(sys.executable),
        "print('early-exit', flush=True)\n",
        cwd=tmp_path,
        creationflags=0,
        soft_timeout_seconds=0.5,
        hard_timeout_seconds=1.0,
        snapshot_has_complete_root=lambda stdout: False,
    )
    assert "early-exit" in result[0]
    assert result[2] is False
    assert result[3] == 0
    assert result[5] == "process-exit"
    assert result[7] is False


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
    assert analysis.deadline_reached is False
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
            args[0],
            kwargs["timeout"],
            output=_published_stdout("c7-c6 d8-b6 f6-e4 b6xf2"),
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


def test_timed_iterative_recovers_deepest_complete_legal_line_on_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    state = replay_series_history(PUBLISHED_HISTORY)
    spec = _dummy_spec(tmp_path)
    observed: dict[str, object] = {}
    partial_stdout = (
        _published_stdout("c7-c6 d8-b6 f6-e4 b6xf2")
        + "[PLY  5][SCORE  *MATE*][TIME 0 m 0.07 s][LINE: c7-c6"
    )

    def fake_run(*args, **kwargs):
        observed["kwargs"] = kwargs
        raise subprocess.TimeoutExpired(
            args[0],
            kwargs["timeout"],
            output=partial_stdout.encode("utf-8"),
            stderr="watchdog stderr",
        )

    monkeypatch.setattr("scottish_progressive.external.subprocess.run", fake_run)
    analysis = analyze_bucephalus_timed_iterative(
        state,
        PUBLISHED_HISTORY,
        spec,
        wall_timeout_seconds=0.25,
    )

    assert analysis.best_series.machine_notation == "c7c6/d8b6/f6e4/b6f2"
    assert analysis.best_series.outcome == Outcome.CHECKMATE
    assert analysis.requested_ply == BUCEPHALUS_MAX_PLY
    assert analysis.completed_ply == 4
    assert analysis.deadline_reached is True
    assert analysis.elapsed_seconds >= 0
    assert analysis.adapter_version == BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION
    assert analysis.request_script.endswith(
        f"p\ne\n{BUCEPHALUS_MAX_PLY}\nt\nq\n"
    )
    assert analysis.stdout == partial_stdout
    assert analysis.stderr == "watchdog stderr"
    assert observed["kwargs"]["timeout"] == 0.25


def test_timed_iterative_recovers_flushed_legal_line_after_process_exit(
    tmp_path: Path, monkeypatch
) -> None:
    state = replay_series_history(PUBLISHED_HISTORY)
    spec = _dummy_spec(tmp_path)
    stdout = _published_stdout("c7-c6 d8-b6 f6-e4 b6xf2")

    monkeypatch.setattr(
        "scottish_progressive.external.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0xC0000005,
            stdout=stdout,
            stderr="",
        ),
    )
    analysis = analyze_bucephalus_timed_iterative(
        state,
        PUBLISHED_HISTORY,
        spec,
        wall_timeout_seconds=0.25,
    )

    assert analysis.completed_ply == 4
    assert analysis.best_series.machine_notation == "c7c6/d8b6/f6e4/b6f2"
    assert analysis.deadline_reached is False
    assert analysis.process_exit_code == 0xC0000005
    assert analysis.process_exit_recovered is True


def test_timed_iterative_rejects_process_exit_without_legal_root_series(
    tmp_path: Path, monkeypatch
) -> None:
    state = replay_series_history(PUBLISHED_HISTORY)
    spec = _dummy_spec(tmp_path)
    stdout = (
        "Bucephalus v1.0.0\n"
        "Side to move: B  Length of Series: 4  Count in Series: 1\n"
        "[PLY  1][SCORE 0.00][TIME 0 m 0.00 s][LINE: c7-c6 ]\n"
    )

    monkeypatch.setattr(
        "scottish_progressive.external.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0xC0000005,
            stdout=stdout,
            stderr="",
        ),
    )
    with pytest.raises(ExternalEngineProtocolError, match="without a usable"):
        analyze_bucephalus_timed_iterative(
            state,
            PUBLISHED_HISTORY,
            spec,
            wall_timeout_seconds=0.25,
        )


def test_timed_iterative_skips_invalid_deeper_record_for_last_legal_iteration(
    tmp_path: Path, monkeypatch
) -> None:
    state = replay_series_history(PUBLISHED_HISTORY)
    spec = _dummy_spec(tmp_path)
    stdout = (
        _published_stdout("c7-c6 d8-b6 f6-e4 b6xf2")
        + "[PLY  5][SCORE  0.00][TIME 0 m 0.07 s]"
        "[LINE: c7-c6 not-a-move ]\n"
    )

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0], kwargs["timeout"], output=stdout, stderr=""
        )

    monkeypatch.setattr("scottish_progressive.external.subprocess.run", fake_run)
    analysis = analyze_bucephalus_timed_iterative(
        state,
        PUBLISHED_HISTORY,
        spec,
        wall_timeout_seconds=0.25,
    )

    assert analysis.completed_ply == 4
    assert analysis.best_series.machine_notation == "c7c6/d8b6/f6e4/b6f2"


def test_timed_iterative_rejects_timeout_without_a_complete_ply_line(
    tmp_path: Path, monkeypatch
) -> None:
    state = replay_series_history(PUBLISHED_HISTORY)
    spec = _dummy_spec(tmp_path)

    def fake_run(*args, **kwargs):
        partial_stdout = (
            "Bucephalus v1.0.0\n"
            "Side to move: B  Length of Series: 4  Count in Series: 1\n"
            "[PLY  4][SCORE *MATE*][TIME 0 m 0.06 s][LINE: c7-c6"
        )
        raise subprocess.TimeoutExpired(
            args[0],
            kwargs["timeout"],
            output=partial_stdout,
            stderr=b"partial stderr",
        )

    monkeypatch.setattr("scottish_progressive.external.subprocess.run", fake_run)
    with pytest.raises(
        ExternalEngineTimeout,
        match="no completed PLY line",
    ):
        analyze_bucephalus_timed_iterative(
            state,
            PUBLISHED_HISTORY,
            spec,
            wall_timeout_seconds=0.25,
        )


@pytest.mark.parametrize(
    ("line", "message"),
    (
        ("c7-c6 not-a-move", "unrecognized"),
        ("a2-a3 a7-a6 b7-b6 c7-c6", "illegal at root"),
    ),
)
def test_timed_iterative_rejects_malformed_or_illegal_deepest_partial_line(
    tmp_path: Path,
    monkeypatch,
    line: str,
    message: str,
) -> None:
    state = replay_series_history(PUBLISHED_HISTORY)
    spec = _dummy_spec(tmp_path)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0],
            kwargs["timeout"],
            output=_published_stdout(line),
            stderr="",
        )

    monkeypatch.setattr("scottish_progressive.external.subprocess.run", fake_run)
    with pytest.raises(ExternalEngineTimeout, match=message):
        analyze_bucephalus_timed_iterative(
            state,
            PUBLISHED_HISTORY,
            spec,
            wall_timeout_seconds=0.25,
        )


def test_timed_iterative_accepts_check_that_completes_series_before_budget(
    tmp_path: Path, monkeypatch
) -> None:
    history = (
        ("e2e4",),
        ("g8f6", "f6g4"),
        ("f1a6", "a6b5", "b2b3"),
        ("g4e5", "e5c6", "a7a5", "d7d5"),
    )
    state = replay_series_history(history)
    assert state.moves_available == 5
    spec = _dummy_spec(tmp_path)
    stdout = (
        "Bucephalus v1.0.0\n"
        "Side to move: W  Length of Series: 5  Count in Series: 1\n"
        "[PLY  1][SCORE *MATE*][TIME 0 m 0.01 s][LINE: b5xc6 ]\n"
    )

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0], kwargs["timeout"], output=stdout, stderr=""
        )

    monkeypatch.setattr("scottish_progressive.external.subprocess.run", fake_run)
    analysis = analyze_bucephalus_timed_iterative(
        state,
        history,
        spec,
        wall_timeout_seconds=0.25,
    )

    assert analysis.completed_ply == 1
    assert analysis.best_series.machine_notation == "b5c6"
    assert analysis.best_series.ended_by_check is True
    assert analysis.deadline_reached is True


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
