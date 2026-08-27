from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

import chess

from scottish_progressive.model import Outcome, ProgressiveState, SeriesResult
from scottish_progressive.rules import SeriesLegalityError, play_series


BUCEPHALUS_ADAPTER_VERSION = "bucephalus-terminal-v1"
BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION = "bucephalus-timed-live-checkpoint-v3"
BUCEPHALUS_MAX_PLY = 30
BUCEPHALUS_MAX_GAME_RECORD = 200

SeriesHistory = tuple[tuple[str, ...], ...]


class ExternalEngineError(RuntimeError):
    """Base class for failures attributable to an external engine run."""


class ExternalEngineConfigurationError(ExternalEngineError):
    """The executable or requested analysis scope cannot be driven safely."""


class ExternalEngineHashMismatch(ExternalEngineConfigurationError):
    """The configured executable does not match its required fingerprint."""


class ExternalEngineTimeout(ExternalEngineError):
    """The external process exceeded its wall-clock watchdog."""


class ExternalEngineProtocolError(ExternalEngineError):
    """The process exited, but did not return a legal complete root series."""


@dataclass(frozen=True, slots=True)
class BucephalusSpec:
    """A user-supplied Bucephalus executable pinned by SHA-256.

    The project deliberately does not download, copy, or package the GPL
    executable. Callers must provide an explicit path and the fingerprint they
    intend to test.
    """

    executable: Path
    sha256: str
    upstream_commit: str | None = None
    build_provenance: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "executable", Path(self.executable))
        normalized = self.sha256.strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
            raise ValueError("sha256 must be exactly 64 hexadecimal characters")
        object.__setattr__(self, "sha256", normalized)
        if self.build_provenance is not None:
            provenance = self.build_provenance.strip()
            if not provenance:
                raise ValueError("build_provenance cannot be blank")
            object.__setattr__(self, "build_provenance", provenance)

    def verify(self) -> tuple[Path, str]:
        try:
            executable = self.executable.resolve(strict=True)
        except OSError as error:
            raise ExternalEngineConfigurationError(
                f"external executable is unavailable: {self.executable}"
            ) from error
        if not executable.is_file():
            raise ExternalEngineConfigurationError(
                f"external executable is not a file: {executable}"
            )

        digest = hashlib.sha256()
        try:
            with executable.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            raise ExternalEngineConfigurationError(
                f"cannot fingerprint external executable: {executable}"
            ) from error
        actual = digest.hexdigest()
        if actual != self.sha256:
            raise ExternalEngineHashMismatch(
                f"external executable SHA-256 mismatch: expected {self.sha256}, "
                f"got {actual}"
            )
        return executable, actual


@dataclass(frozen=True, slots=True)
class ExternalAnalysisStage:
    stage_index: int
    purpose: str
    starting_prefix: tuple[str, ...]
    emitted_prefix: tuple[str, ...]
    requested_ply: int
    completed_ply: int
    wall_timeout_seconds: float
    elapsed_seconds: float
    request_script: str
    stdout: str
    stderr: str
    deadline_reached: bool
    process_exit_code: int | None
    process_exit_recovered: bool
    usable: bool = True
    error: str | None = None
    stop_reason: str = "process-exit"
    process_id: int | None = None
    same_process_continued: bool = False
    soft_checkpoint_seconds: float | None = None
    hard_deadline_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ExternalAnalysis:
    best_series: SeriesResult
    requested_ply: int
    completed_ply: int
    score_text: str
    elapsed_seconds: float
    executable_sha256: str
    upstream_commit: str | None
    adapter_version: str
    request_script: str
    stdout: str
    stderr: str
    deadline_reached: bool = False
    process_exit_code: int | None = None
    process_exit_recovered: bool = False
    continuation_stages: tuple[ExternalAnalysisStage, ...] = ()
    selection_mode: str = "single-stage"
    terminal_stage_score: str | None = None
    global_deadline_seconds: float | None = None
    global_deadline_reached: bool = False
    process_count: int = 1
    selection_root_prefix_ply: int | None = None
    terminal_stage_ply: int | None = None


_PLY_LINE = re.compile(
    r"^\[PLY\s+(?P<ply>\d+)\]"
    r"\[SCORE\s+(?P<score>.*?)\]"
    r"\[TIME\s+.*?\]"
    r"\[LINE:\s*(?P<line>.*?)\s*\]\s*$",
    re.MULTILINE,
)
_MOVE_TOKEN = re.compile(
    r"(?P<from>[a-h][1-8])[-x](?P<to>[a-h][1-8])(?:=(?P<promotion>[NBRQ]))?"
)


def replay_series_history(history: Sequence[Sequence[str]]) -> ProgressiveState:
    """Replays complete from-start series with the authoritative rules API."""

    state = ProgressiveState.initial()
    for series_number, moves in enumerate(history, 1):
        if series_number != state.series_number:
            raise ExternalEngineConfigurationError(
                "series history is not aligned with the progressive turn number"
            )
        try:
            result = play_series(state, tuple(moves))
        except SeriesLegalityError as error:
            raise ExternalEngineConfigurationError(
                f"invalid replay history at series {series_number}: {error}"
            ) from error
        if result.outcome is not None:
            raise ExternalEngineConfigurationError(
                f"replay history continues from terminal {result.outcome.value} "
                f"at series {series_number}"
            )
        state = result.final_state
    return state


def _canonical_opening_histories() -> Mapping[str, SeriesHistory]:
    initial = ProgressiveState.initial()
    histories: dict[str, SeriesHistory] = {"initial": ()}
    first_moves: dict[str, str] = {}
    for uci in sorted(move.uci() for move in initial.board.legal_moves):
        result = play_series(initial, (uci,))
        san = result.san[0].replace("+", "").replace("#", "").lower()
        case_id = f"after-1-{san}"
        histories[case_id] = ((uci,),)
        first_moves[uci] = case_id

    for uci in ("b1a3", "b1c3", "b2b3", "c2c4", "d2d4", "e2e4", "g1f3"):
        if uci not in first_moves:
            raise RuntimeError(f"missing canonical first move {uci}")
        histories[f"after-{uci}-a6-b6"] = ((uci,), ("a7a6", "b7b6"))

    histories["published-bishop-pressure"] = (
        ("e2e4",),
        ("d7d5", "g8f6"),
        ("e4d5", "g1f3", "f1b5"),
    )
    histories["published-central-pressure"] = (
        ("d2d4",),
        ("d7d5", "g8f6"),
        ("b1c3", "g1f3", "c1g5"),
    )
    if len(histories) != 30:
        raise RuntimeError(
            f"canonical external opening histories must contain 30 cases, got "
            f"{len(histories)}"
        )
    return MappingProxyType(histories)


BUCEPHALUS_OPENING_HISTORIES_V1 = _canonical_opening_histories()


def _validate_history_capacity(history: Sequence[Sequence[str]]) -> None:
    for series_number, moves in enumerate(history, 1):
        if not moves:
            continue
        final_record_index = (
            series_number * (series_number - 1) // 2 + len(moves) - 1
        )
        if final_record_index >= BUCEPHALUS_MAX_GAME_RECORD:
            raise ExternalEngineConfigurationError(
                "replay exceeds Bucephalus's 200-move game-record array at "
                f"series {series_number}"
            )


def _validate_partial_series_capacity(
    history: Sequence[Sequence[str]], prefix: Sequence[str]
) -> None:
    if not prefix:
        return
    series_number = len(history) + 1
    final_record_index = (
        series_number * (series_number - 1) // 2 + len(prefix) - 1
    )
    if final_record_index >= BUCEPHALUS_MAX_GAME_RECORD:
        raise ExternalEngineConfigurationError(
            "partial-series replay exceeds Bucephalus's 200-move game-record "
            f"array at series {series_number}"
        )


def _bucephalus_move(uci: str) -> str:
    if re.fullmatch(r"[a-h][1-8][a-h][1-8][nbrq]?", uci) is None:
        raise ExternalEngineConfigurationError(
            f"history contains unsupported UCI move {uci!r}"
        )
    return uci[:4] + uci[4:].upper()


def _request_script(
    history: Sequence[Sequence[str]], search_ply: int, *, prefix: Sequence[str] = ()
) -> str:
    _validate_partial_series_capacity(history, prefix)
    commands: list[str] = []
    for series in history:
        for uci in series:
            commands.extend(("m", _bucephalus_move(uci)))
    for uci in prefix:
        commands.extend(("m", _bucephalus_move(uci)))
    # `p` gives a machine-checkable boundary marker. `e`, depth, `t` is
    # Bucephalus's terminal evaluator; `q` flushes output through an ordinary
    # pipe once the requested iterative search finishes.
    commands.extend(("p", "e", str(search_ply), "t", "q"))
    return "\n".join(commands) + "\n"


def _parse_requested_ply(stdout: str, requested_ply: int) -> tuple[str, tuple[str, ...]]:
    matches = [
        match for match in _PLY_LINE.finditer(stdout)
        if int(match.group("ply")) == requested_ply
    ]
    if len(matches) != 1:
        raise ExternalEngineProtocolError(
            f"expected exactly one completed PLY {requested_ply} line, got "
            f"{len(matches)}"
        )
    match = matches[0]
    move_text = match.group("line").strip()
    if not move_text:
        raise ExternalEngineProtocolError(
            f"completed PLY {requested_ply} contained no principal variation"
        )
    moves: list[str] = []
    for token in move_text.split():
        parsed = _MOVE_TOKEN.fullmatch(token)
        if parsed is None:
            raise ExternalEngineProtocolError(
                f"unrecognized Bucephalus principal-variation token {token!r}"
            )
        promotion = (parsed.group("promotion") or "").lower()
        moves.append(parsed.group("from") + parsed.group("to") + promotion)
    return match.group("score").strip(), tuple(moves)


def _validated_root_series(
    state: ProgressiveState, principal_variation: Sequence[str]
) -> SeriesResult:
    for length in range(1, len(principal_variation) + 1):
        prefix = tuple(principal_variation[:length])
        try:
            return play_series(state, prefix)
        except SeriesLegalityError as error:
            if str(error).startswith("series is incomplete:"):
                continue
            raise ExternalEngineProtocolError(
                f"Bucephalus principal variation is illegal at root: {error}"
            ) from error
    raise ExternalEngineProtocolError(
        "Bucephalus principal variation did not contain a complete root series"
    )


def _decode_process_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def _run_bucephalus_live_checkpoint(
    executable: Path,
    script: str,
    *,
    cwd: Path,
    creationflags: int,
    soft_timeout_seconds: float,
    hard_timeout_seconds: float,
    snapshot_has_complete_root: Callable[[str], bool],
) -> tuple[str, str, bool, int | None, float, str, int, bool]:
    """Drains one live process and conditionally carries it past a soft gate."""

    started = time.perf_counter()
    soft_deadline = started + soft_timeout_seconds
    hard_deadline = started + hard_timeout_seconds
    try:
        process = subprocess.Popen(
            [str(executable)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            creationflags=creationflags,
        )
    except OSError as error:
        raise ExternalEngineConfigurationError(
            f"cannot start external executable: {executable}"
        ) from error
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise ExternalEngineConfigurationError(
            "Bucephalus checkpoint process did not expose all standard pipes"
        )
    lock = threading.Lock()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def drain(stream, chunks: list[bytes]) -> None:
        try:
            while True:
                reader = getattr(stream, "read1", stream.read)
                chunk = reader(8192)
                if not chunk:
                    break
                with lock:
                    chunks.append(chunk)
        except OSError:
            return

    stdout_thread = threading.Thread(
        target=drain, args=(process.stdout, stdout_chunks), daemon=True
    )
    stderr_thread = threading.Thread(
        target=drain, args=(process.stderr, stderr_chunks), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    stop_reason = "process-exit"
    same_process_continued = False
    deadline_reached = False
    try:
        process.stdin.write(script.encode("utf-8"))
        process.stdin.flush()
        process.stdin.close()
        try:
            process.wait(timeout=max(0.0, soft_deadline - time.perf_counter()))
        except subprocess.TimeoutExpired:
            with lock:
                snapshot = b"".join(stdout_chunks).decode(
                    "utf-8", errors="replace"
                )
            if snapshot_has_complete_root(snapshot):
                same_process_continued = True
                remaining = max(0.0, hard_deadline - time.perf_counter())
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    stop_reason = "hard-deadline"
                    deadline_reached = True
                    process.kill()
            else:
                stop_reason = "soft-checkpoint-incomplete"
                deadline_reached = True
                process.kill()
        if process.poll() is None:
            process.kill()
        process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        stdout_thread.join(timeout=2.0)
        stderr_thread.join(timeout=2.0)
        process.stdout.close()
        process.stderr.close()
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        raise ExternalEngineProtocolError(
            "Bucephalus checkpoint drain thread did not terminate"
        )
    with lock:
        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return (
        stdout,
        stderr,
        deadline_reached,
        process.returncode,
        time.perf_counter() - started,
        stop_reason,
        process.pid,
        same_process_continued,
    )


def _validate_output_identity_and_boundary(
    stdout: str, state: ProgressiveState, *, count_in_series: int = 1
) -> None:
    if "Bucephalus v" not in stdout:
        raise ExternalEngineProtocolError(
            "external process did not identify itself as Bucephalus"
        )
    side = "W" if state.board.turn == chess.WHITE else "B"
    status = re.compile(
        rf"Side to move:\s*{side}\s+Length of Series:\s*"
        rf"{state.series_number}\s+Count in Series:\s*{count_in_series}"
    )
    if status.search(stdout) is None:
        raise ExternalEngineProtocolError(
            "Bucephalus replay did not report the requested series boundary"
        )


def _parse_deepest_legal_incomplete_prefix(
    stdout: str, *, requested_ply: int, state: ProgressiveState
) -> tuple[int, str, tuple[str, ...]]:
    matches = list(_PLY_LINE.finditer(stdout))
    if not matches:
        raise ExternalEngineProtocolError(
            "timed Bucephalus output contained no completed PLY line"
        )
    observed = {int(match.group("ply")) for match in matches}
    if max(observed) > requested_ply:
        raise ExternalEngineProtocolError(
            f"Bucephalus reported PLY {max(observed)} beyond requested PLY "
            f"{requested_ply}"
        )
    rejected: list[str] = []
    for completed_ply in sorted(observed, reverse=True):
        try:
            score, pv = _parse_requested_ply(stdout, completed_ply)
            prefix = tuple(pv[: state.moves_available - 1])
            if not prefix:
                raise ExternalEngineProtocolError("no root-series prefix was emitted")
            try:
                play_series(state, prefix)
            except SeriesLegalityError as error:
                if str(error).startswith("series is incomplete:"):
                    return completed_ply, score, prefix
                raise ExternalEngineProtocolError(
                    f"Bucephalus principal variation is illegal at root: {error}"
                ) from error
            raise ExternalEngineProtocolError(
                "principal variation already completed the root series"
            )
        except ExternalEngineProtocolError as error:
            rejected.append(f"PLY {completed_ply}: {error}")
    raise ExternalEngineProtocolError(
        "timed Bucephalus output contained no replay-valid legal incomplete "
        f"root prefix ({'; '.join(rejected[:3])})"
    )


def _parse_deepest_continuation_progress(
    stdout: str,
    *,
    requested_ply: int,
    state: ProgressiveState,
    prefix: tuple[str, ...],
) -> tuple[int, str, tuple[str, ...], SeriesResult | None]:
    matches = list(_PLY_LINE.finditer(stdout))
    if not matches:
        raise ExternalEngineProtocolError("continuation emitted no completed PLY line")
    observed = {int(match.group("ply")) for match in matches}
    if max(observed) > requested_ply:
        raise ExternalEngineProtocolError(
            f"Bucephalus reported PLY {max(observed)} beyond requested PLY {requested_ply}"
        )
    rejected: list[str] = []
    for completed_ply in sorted(observed, reverse=True):
        try:
            score, pv = _parse_requested_ply(stdout, completed_ply)
        except ExternalEngineProtocolError as error:
            rejected.append(f"PLY {completed_ply}: {error}")
            continue
        accepted: tuple[str, ...] = ()
        for length in range(1, min(len(pv), state.moves_available - len(prefix)) + 1):
            extension = tuple(pv[:length])
            try:
                result = play_series(state, prefix + extension)
            except SeriesLegalityError as error:
                if str(error).startswith("series is incomplete:"):
                    accepted = extension
                    continue
                rejected.append(f"PLY {completed_ply}: {error}")
                accepted = ()
                break
            return completed_ply, score, extension, result
        if accepted:
            return completed_ply, score, accepted, None
    raise ExternalEngineProtocolError(
        "continuation emitted no authoritative legal progress "
        f"({'; '.join(rejected[:3])})"
    )


def _parse_deepest_completed_ply(
    stdout: str,
    *,
    requested_ply: int,
    state: ProgressiveState,
) -> tuple[int, str, SeriesResult]:
    matches = list(_PLY_LINE.finditer(stdout))
    if not matches:
        raise ExternalEngineProtocolError(
            "timed Bucephalus output contained no completed PLY line"
        )
    observed_plies = {int(match.group("ply")) for match in matches}
    if max(observed_plies) > requested_ply:
        raise ExternalEngineProtocolError(
            f"Bucephalus reported PLY {max(observed_plies)} beyond requested PLY "
            f"{requested_ply}"
        )
    rejected: list[str] = []
    for completed_ply in sorted(observed_plies, reverse=True):
        try:
            score_text, principal_variation = _parse_requested_ply(
                stdout, completed_ply
            )
            best_series = _validated_root_series(state, principal_variation)
        except ExternalEngineProtocolError as error:
            rejected.append(f"PLY {completed_ply}: {error}")
            continue
        return completed_ply, score_text, best_series
    details = "; ".join(rejected[:3])
    raise ExternalEngineProtocolError(
        "timed Bucephalus output contained no unambiguous completed PLY "
        f"with a legal complete root series ({details})"
    )


def analyze_bucephalus(
    state: ProgressiveState,
    history: Sequence[Sequence[str]],
    spec: BucephalusSpec,
    *,
    search_ply: int,
    wall_timeout_seconds: float,
) -> ExternalAnalysis:
    """Requests and validates one full root series from Bucephalus.

    Bucephalus has no UCI/XBoard or FEN command, so every call is a fresh,
    one-shot process fed a complete canonical history from the initial board.
    A timeout, nonzero exit, missing final iterative line, or illegal series is
    a technical external-engine failure; no partial result is promoted.
    """

    if isinstance(search_ply, bool) or not isinstance(search_ply, int):
        raise ValueError("search_ply must be an integer")
    if search_ply < state.moves_available:
        raise ExternalEngineConfigurationError(
            f"search_ply {search_ply} cannot complete series "
            f"{state.moves_available}"
        )
    if search_ply > BUCEPHALUS_MAX_PLY:
        raise ExternalEngineConfigurationError(
            f"Bucephalus supports at most {BUCEPHALUS_MAX_PLY} micro-plies"
        )
    if not math.isfinite(wall_timeout_seconds) or wall_timeout_seconds <= 0:
        raise ValueError("wall_timeout_seconds must be finite and positive")

    normalized_history = tuple(tuple(series) for series in history)
    _validate_history_capacity(normalized_history)
    replayed = replay_series_history(normalized_history)
    if replayed.position_hash != state.position_hash:
        raise ExternalEngineConfigurationError(
            "canonical replay history does not reach the requested progressive state"
        )
    if len(normalized_history) + 1 != state.series_number:
        raise ExternalEngineConfigurationError(
            "canonical replay history has the wrong number of completed series"
        )

    try:
        terminal = play_series(state, ())
    except SeriesLegalityError:
        terminal = None
    if terminal is not None:
        raise ExternalEngineConfigurationError(
            f"requested state is already terminal ({terminal.outcome.value})"
        )

    executable, actual_hash = spec.verify()
    script = _request_script(normalized_history, search_ply)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [str(executable)],
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            timeout=wall_timeout_seconds,
            check=False,
            cwd=str(executable.parent),
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as error:
        elapsed = time.perf_counter() - started
        raise ExternalEngineTimeout(
            f"Bucephalus exceeded the {wall_timeout_seconds:.3f}s wall watchdog "
            f"after {elapsed:.3f}s"
        ) from error
    except OSError as error:
        raise ExternalEngineConfigurationError(
            f"cannot start external executable: {executable}"
        ) from error
    elapsed = time.perf_counter() - started

    if completed.returncode != 0:
        raise ExternalEngineProtocolError(
            f"Bucephalus exited with status {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    if "Bucephalus v" not in completed.stdout:
        raise ExternalEngineProtocolError(
            "external process did not identify itself as Bucephalus"
        )
    side = "W" if state.board.turn == chess.WHITE else "B"
    status = re.compile(
        rf"Side to move:\s*{side}\s+Length of Series:\s*"
        rf"{state.series_number}\s+Count in Series:\s*1"
    )
    if status.search(completed.stdout) is None:
        raise ExternalEngineProtocolError(
            "Bucephalus replay did not report the requested series boundary"
        )

    score_text, principal_variation = _parse_requested_ply(
        completed.stdout, search_ply
    )
    best_series = _validated_root_series(state, principal_variation)
    return ExternalAnalysis(
        best_series=best_series,
        requested_ply=search_ply,
        completed_ply=search_ply,
        score_text=score_text,
        elapsed_seconds=elapsed,
        executable_sha256=actual_hash,
        upstream_commit=spec.upstream_commit,
        adapter_version=BUCEPHALUS_ADAPTER_VERSION,
        request_script=script,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def analyze_bucephalus_timed_iterative(
    state: ProgressiveState,
    history: Sequence[Sequence[str]],
    spec: BucephalusSpec,
    *,
    wall_timeout_seconds: float,
) -> ExternalAnalysis:
    """Runs Bucephalus to its maximum ply under a wall-clock budget.

    Unlike :func:`analyze_bucephalus`, this explicit timed adapter can recover
    after the watchdog expires or the per-call process exits abnormally.
    Recovery is limited to the deepest fully emitted PLY record, and only after
    the process identity, replay boundary, move syntax, and complete root series
    all validate. The exact-depth adapter intentionally retains its original
    fail-on-timeout/fail-on-exit behavior.
    """

    adapter_entry_started = time.perf_counter()
    if not math.isfinite(wall_timeout_seconds) or wall_timeout_seconds <= 0:
        raise ValueError("wall_timeout_seconds must be finite and positive")
    if BUCEPHALUS_MAX_PLY < state.moves_available:
        raise ExternalEngineConfigurationError(
            f"Bucephalus's maximum PLY {BUCEPHALUS_MAX_PLY} cannot complete "
            f"series {state.moves_available}"
        )

    normalized_history = tuple(tuple(series) for series in history)
    _validate_history_capacity(normalized_history)
    replayed = replay_series_history(normalized_history)
    if replayed.position_hash != state.position_hash:
        raise ExternalEngineConfigurationError(
            "canonical replay history does not reach the requested progressive state"
        )
    if len(normalized_history) + 1 != state.series_number:
        raise ExternalEngineConfigurationError(
            "canonical replay history has the wrong number of completed series"
        )

    try:
        terminal = play_series(state, ())
    except SeriesLegalityError:
        terminal = None
    if terminal is not None:
        raise ExternalEngineConfigurationError(
            f"requested state is already terminal ({terminal.outcome.value})"
        )

    executable, actual_hash = spec.verify()
    if wall_timeout_seconds >= 12.0:
        return _analyze_bucephalus_with_continuation(
            state,
            normalized_history,
            spec,
            executable=executable,
            actual_hash=actual_hash,
            wall_timeout_seconds=wall_timeout_seconds,
            overall_started=adapter_entry_started,
        )
    requested_ply = BUCEPHALUS_MAX_PLY
    script = _request_script(normalized_history, requested_ply)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [str(executable)],
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            timeout=wall_timeout_seconds,
            check=False,
            cwd=str(executable.parent),
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as error:
        elapsed = time.perf_counter() - started
        stdout = _decode_process_output(error.stdout)
        stderr = _decode_process_output(error.stderr)
        try:
            _validate_output_identity_and_boundary(stdout, state)
            completed_ply, score_text, best_series = (
                _parse_deepest_completed_ply(
                    stdout,
                    requested_ply=requested_ply,
                    state=state,
                )
            )
        except ExternalEngineProtocolError as protocol_error:
            raise ExternalEngineTimeout(
                "Bucephalus reached the wall deadline without a usable "
                f"completed iteration: {protocol_error}"
            ) from error
        return ExternalAnalysis(
            best_series=best_series,
            requested_ply=requested_ply,
            completed_ply=completed_ply,
            score_text=score_text,
            elapsed_seconds=elapsed,
            executable_sha256=actual_hash,
            upstream_commit=spec.upstream_commit,
            adapter_version=BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
            request_script=script,
            stdout=stdout,
            stderr=stderr,
            deadline_reached=True,
        )
    except OSError as error:
        raise ExternalEngineConfigurationError(
            f"cannot start external executable: {executable}"
        ) from error
    elapsed = time.perf_counter() - started

    stdout = _decode_process_output(completed.stdout)
    stderr = _decode_process_output(completed.stderr)
    if completed.returncode != 0:
        try:
            _validate_output_identity_and_boundary(stdout, state)
            completed_ply, score_text, best_series = (
                _parse_deepest_completed_ply(
                    stdout,
                    requested_ply=requested_ply,
                    state=state,
                )
            )
        except ExternalEngineProtocolError as protocol_error:
            raise ExternalEngineProtocolError(
                f"Bucephalus exited with status {completed.returncode} without "
                f"a usable completed iteration: {protocol_error}; "
                f"stderr={stderr.strip()!r}"
            ) from protocol_error
        return ExternalAnalysis(
            best_series=best_series,
            requested_ply=requested_ply,
            completed_ply=completed_ply,
            score_text=score_text,
            elapsed_seconds=elapsed,
            executable_sha256=actual_hash,
            upstream_commit=spec.upstream_commit,
            adapter_version=BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
            request_script=script,
            stdout=stdout,
            stderr=stderr,
            deadline_reached=False,
            process_exit_code=completed.returncode,
            process_exit_recovered=True,
        )
    _validate_output_identity_and_boundary(stdout, state)
    score_text, principal_variation = _parse_requested_ply(
        stdout, requested_ply
    )
    best_series = _validated_root_series(state, principal_variation)
    return ExternalAnalysis(
        best_series=best_series,
        requested_ply=requested_ply,
        completed_ply=requested_ply,
        score_text=score_text,
        elapsed_seconds=elapsed,
        executable_sha256=actual_hash,
        upstream_commit=spec.upstream_commit,
        adapter_version=BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
        request_script=script,
        stdout=stdout,
        stderr=stderr,
        deadline_reached=False,
    )


def _analyze_bucephalus_with_continuation(
    state: ProgressiveState,
    history: SeriesHistory,
    spec: BucephalusSpec,
    *,
    executable: Path,
    actual_hash: str,
    wall_timeout_seconds: float,
    overall_started: float,
) -> ExternalAnalysis:
    """Builds a Bucephalus-only anchor, then spends the wall on deep search."""

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    cleanup_margin = min(1.0, wall_timeout_seconds * 0.01)
    searchable_deadline = wall_timeout_seconds - cleanup_margin
    anchor_deadline = min(12.0, wall_timeout_seconds * 0.10)
    stages: list[ExternalAnalysisStage] = []

    def elapsed_total() -> float:
        return time.perf_counter() - overall_started

    def run_stage(
        *, purpose: str, prefix: tuple[str, ...], requested_ply: int, timeout: float
    ) -> tuple[str, str, bool, int | None, float, str]:
        if timeout <= 0:
            raise ExternalEngineTimeout(
                f"Bucephalus {purpose} has no time left inside the common wall"
            )
        script = _request_script(history, requested_ply, prefix=prefix)
        before = elapsed_total()
        try:
            completed = subprocess.run(
                [str(executable)], input=script, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                timeout=timeout, check=False, cwd=str(executable.parent),
                creationflags=creationflags,
            )
            return (
                _decode_process_output(completed.stdout),
                _decode_process_output(completed.stderr),
                False, completed.returncode, elapsed_total() - before, script,
            )
        except subprocess.TimeoutExpired as error:
            return (
                _decode_process_output(error.stdout),
                _decode_process_output(error.stderr),
                True, None, elapsed_total() - before, script,
            )
        except OSError as error:
            raise ExternalEngineConfigurationError(
                f"cannot start external executable: {executable}"
            ) from error

    def append_stage(
        purpose: str,
        prefix: tuple[str, ...],
        emitted: tuple[str, ...],
        requested: int,
        completed: int,
        timeout: float,
        process: tuple[str, str, bool, int | None, float, str],
        *,
        usable: bool = True,
        error: str | None = None,
        stop_reason: str | None = None,
        process_id: int | None = None,
        same_process_continued: bool = False,
        soft_checkpoint_seconds: float | None = None,
        hard_deadline_seconds: float | None = None,
    ) -> None:
        stdout, stderr, timed, exit_code, elapsed, script = process
        stages.append(
            ExternalAnalysisStage(
                len(stages) + 1, purpose, prefix, emitted, requested, completed,
                timeout, elapsed, script, stdout, stderr, timed, exit_code,
                bool(exit_code not in (None, 0) and not timed), usable, error,
                stop_reason or ("stage-deadline" if timed else "process-exit"),
                process_id,
                same_process_continued,
                soft_checkpoint_seconds,
                hard_deadline_seconds,
            )
        )

    def exact_one_move(
        prefix: tuple[str, ...], *, purpose: str, timeout: float
    ) -> tuple[tuple[str, ...], SeriesResult | None, str]:
        process = run_stage(
            purpose=purpose, prefix=prefix, requested_ply=1, timeout=timeout
        )
        stdout = process[0]
        try:
            _validate_output_identity_and_boundary(
                stdout, state, count_in_series=len(prefix) + 1
            )
            score, pv = _parse_requested_ply(stdout, 1)
            if len(pv) != 1:
                raise ExternalEngineProtocolError(
                    "exact PLY1 stage did not emit exactly one move"
                )
            candidate = prefix + pv
            try:
                result = play_series(state, candidate)
            except SeriesLegalityError as replay_error:
                if not str(replay_error).startswith("series is incomplete:"):
                    raise ExternalEngineProtocolError(
                        f"exact PLY1 move failed authoritative replay: {replay_error}"
                    ) from replay_error
                result = None
        except ExternalEngineProtocolError as error:
            append_stage(
                purpose, prefix, (), 1, 0, timeout, process,
                usable=False, error=str(error),
            )
            raise
        append_stage(purpose, prefix, pv, 1, 1, timeout, process)
        return candidate, result, score

    # A complete fallback exists before expensive search starts. It is made
    # exclusively from exact PLY1 choices emitted by the pinned executable.
    anchor_prefix: tuple[str, ...] = ()
    anchor_result: SeriesResult | None = None
    anchor_score = ""
    while anchor_result is None:
        anchor_remaining = anchor_deadline - elapsed_total()
        if anchor_remaining <= 0:
            raise ExternalEngineTimeout(
                "Bucephalus could not build its engine-native anchor inside "
                "the reserved common-wall budget"
            )
        remaining_root_moves = state.moves_available - len(anchor_prefix)
        per_call_timeout = min(
            1.0,
            anchor_remaining / (remaining_root_moves + 1),
        )
        try:
            anchor_prefix, anchor_result, anchor_score = exact_one_move(
                anchor_prefix, purpose="anchor-ply1", timeout=per_call_timeout
            )
        except ExternalEngineProtocolError as error:
            raise ExternalEngineTimeout(
                f"Bucephalus engine-native anchor failed: {error}"
            ) from error
    assert anchor_result is not None
    if anchor_result.outcome == Outcome.CHECKMATE:
        total_elapsed = elapsed_total()
        recovered_exit = next(
            (stage.process_exit_code for stage in stages if stage.process_exit_recovered),
            None,
        )
        return ExternalAnalysis(
            anchor_result, BUCEPHALUS_MAX_PLY, 1, anchor_score, total_elapsed,
            actual_hash, spec.upstream_commit,
            BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
            "multi-stage; see continuation_stages",
            "multi-stage stdout retained per continuation stage",
            "multi-stage stderr retained per continuation stage",
            deadline_reached=any(stage.deadline_reached for stage in stages),
            process_exit_code=recovered_exit,
            process_exit_recovered=recovered_exit is not None,
            continuation_stages=tuple(stages),
            selection_mode="anchor-terminal",
            terminal_stage_score=anchor_score,
            global_deadline_seconds=wall_timeout_seconds,
            global_deadline_reached=False,
            process_count=len(stages),
            selection_root_prefix_ply=1,
            terminal_stage_ply=1,
        )

    remaining_searchable = searchable_deadline - elapsed_total()
    deep_timeout = remaining_searchable * 0.75
    if deep_timeout <= 0:
        raise ExternalEngineTimeout(
            "Bucephalus anchor left no deep-search budget inside the common wall"
        )
    deep_script = _request_script(history, BUCEPHALUS_MAX_PLY)

    def snapshot_has_complete_root(snapshot: str) -> bool:
        try:
            _validate_output_identity_and_boundary(snapshot, state)
            _parse_deepest_completed_ply(
                snapshot, requested_ply=BUCEPHALUS_MAX_PLY, state=state
            )
        except ExternalEngineProtocolError:
            return False
        return True

    deep_hard_timeout = searchable_deadline - elapsed_total()
    live = _run_bucephalus_live_checkpoint(
        executable,
        deep_script,
        cwd=executable.parent,
        creationflags=creationflags,
        soft_timeout_seconds=deep_timeout,
        hard_timeout_seconds=deep_hard_timeout,
        snapshot_has_complete_root=snapshot_has_complete_root,
    )
    deep_process = (live[0], live[1], live[2], live[3], live[4], deep_script)
    deep_stop_reason, deep_process_id, deep_continued = live[5], live[6], live[7]
    deep_stdout = deep_process[0]
    selected = anchor_result
    selected_score = anchor_score
    selected_completed_ply = 1
    selection_mode = "anchor-fallback"
    try:
        _validate_output_identity_and_boundary(deep_stdout, state)
        completed_ply, score, complete = _parse_deepest_completed_ply(
            deep_stdout, requested_ply=BUCEPHALUS_MAX_PLY, state=state
        )
    except ExternalEngineProtocolError:
        try:
            prefix_ply, score, deep_prefix = _parse_deepest_legal_incomplete_prefix(
                deep_stdout, requested_ply=BUCEPHALUS_MAX_PLY, state=state
            )
        except ExternalEngineProtocolError as error:
            append_stage(
                "deep-max-ply", (), (), BUCEPHALUS_MAX_PLY, 0,
                deep_hard_timeout, deep_process, usable=False, error=str(error),
                stop_reason=deep_stop_reason, process_id=deep_process_id,
                same_process_continued=deep_continued,
                soft_checkpoint_seconds=deep_timeout,
                hard_deadline_seconds=deep_hard_timeout,
            )
            deep_prefix = ()
        else:
            append_stage(
                "deep-max-ply", (), deep_prefix, BUCEPHALUS_MAX_PLY,
                prefix_ply, deep_hard_timeout, deep_process,
                stop_reason=deep_stop_reason, process_id=deep_process_id,
                same_process_continued=deep_continued,
                soft_checkpoint_seconds=deep_timeout,
                hard_deadline_seconds=deep_hard_timeout,
            )
            stitched = deep_prefix
            stitched_result: SeriesResult | None = None
            stitched_score = score
            while stitched_result is None:
                remaining = searchable_deadline - elapsed_total()
                remaining_moves = state.moves_available - len(stitched)
                if remaining <= 0 or remaining_moves <= 0:
                    break
                stage_timeout = remaining / remaining_moves
                process = run_stage(
                    purpose="suffix-max-ply",
                    prefix=stitched,
                    requested_ply=BUCEPHALUS_MAX_PLY,
                    timeout=stage_timeout,
                )
                try:
                    _validate_output_identity_and_boundary(
                        process[0], state, count_in_series=len(stitched) + 1
                    )
                    suffix_ply, stitched_score, extension, stitched_result = (
                        _parse_deepest_continuation_progress(
                            process[0],
                            requested_ply=BUCEPHALUS_MAX_PLY,
                            state=state,
                            prefix=stitched,
                        )
                    )
                except ExternalEngineProtocolError as error:
                    append_stage(
                        "suffix-max-ply", stitched, (), BUCEPHALUS_MAX_PLY,
                        0, stage_timeout, process, usable=False,
                        error=str(error),
                    )
                    while stitched_result is None:
                        remaining = searchable_deadline - elapsed_total()
                        remaining_moves = state.moves_available - len(stitched)
                        if remaining <= 0 or remaining_moves <= 0:
                            break
                        try:
                            stitched, stitched_result, stitched_score = exact_one_move(
                                stitched,
                                purpose="suffix-ply1",
                                timeout=remaining / (remaining_moves + 1),
                            )
                        except ExternalEngineProtocolError:
                            break
                    break
                append_stage(
                    "suffix-max-ply", stitched, extension,
                    BUCEPHALUS_MAX_PLY, suffix_ply, stage_timeout, process,
                )
                stitched += extension
            if stitched_result is not None:
                selected = stitched_result
                selected_score = stitched_score
                selected_completed_ply = prefix_ply
                selection_mode = "deep-prefix-continuation"
    else:
        append_stage(
            "deep-max-ply", (), tuple(complete.moves), BUCEPHALUS_MAX_PLY,
            completed_ply, deep_hard_timeout, deep_process,
            stop_reason=deep_stop_reason, process_id=deep_process_id,
            same_process_continued=deep_continued,
            soft_checkpoint_seconds=deep_timeout,
            hard_deadline_seconds=deep_hard_timeout,
        )
        selected = complete
        selected_score = score
        selected_completed_ply = completed_ply
        selection_mode = "deep-complete-live"

    total_elapsed = elapsed_total()
    if total_elapsed > wall_timeout_seconds:
        raise ExternalEngineTimeout(
            "Bucephalus continuation exceeded the single common-wall deadline"
        )
    recovered_exit = next(
        (
            stage.process_exit_code
            for stage in stages
            if stage.process_exit_recovered
        ),
        None,
    )
    return ExternalAnalysis(
        selected, BUCEPHALUS_MAX_PLY, selected_completed_ply, selected_score,
        total_elapsed, actual_hash, spec.upstream_commit,
        BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION,
        "multi-stage; see continuation_stages",
        "multi-stage stdout retained per continuation stage",
        "multi-stage stderr retained per continuation stage",
        deadline_reached=any(stage.deadline_reached for stage in stages),
        process_exit_code=recovered_exit,
        process_exit_recovered=recovered_exit is not None,
        continuation_stages=tuple(stages),
        selection_mode=selection_mode,
        terminal_stage_score=selected_score,
        global_deadline_seconds=wall_timeout_seconds,
        global_deadline_reached=total_elapsed >= wall_timeout_seconds,
        process_count=len(stages),
        selection_root_prefix_ply=(
            selected_completed_ply
            if selection_mode == "deep-prefix-continuation"
            else None
        ),
        terminal_stage_ply=(
            stages[-1].completed_ply if stages and stages[-1].usable else None
        ),
    )
