from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import time
from types import MappingProxyType
from typing import Mapping, Sequence

import chess

from scottish_progressive.model import ProgressiveState, SeriesResult
from scottish_progressive.rules import SeriesLegalityError, play_series


BUCEPHALUS_ADAPTER_VERSION = "bucephalus-terminal-v1"
BUCEPHALUS_TIMED_ITERATIVE_ADAPTER_VERSION = "bucephalus-timed-iterative-v1"
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


def _bucephalus_move(uci: str) -> str:
    if re.fullmatch(r"[a-h][1-8][a-h][1-8][nbrq]?", uci) is None:
        raise ExternalEngineConfigurationError(
            f"history contains unsupported UCI move {uci!r}"
        )
    return uci[:4] + uci[4:].upper()


def _request_script(
    history: Sequence[Sequence[str]], search_ply: int
) -> str:
    commands: list[str] = []
    for series in history:
        for uci in series:
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


def _validate_output_identity_and_boundary(
    stdout: str, state: ProgressiveState
) -> None:
    if "Bucephalus v" not in stdout:
        raise ExternalEngineProtocolError(
            "external process did not identify itself as Bucephalus"
        )
    side = "W" if state.board.turn == chess.WHITE else "B"
    status = re.compile(
        rf"Side to move:\s*{side}\s+Length of Series:\s*"
        rf"{state.series_number}\s+Count in Series:\s*1"
    )
    if status.search(stdout) is None:
        raise ExternalEngineProtocolError(
            "Bucephalus replay did not report the requested series boundary"
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
