"""Bounded Stockfish-as-a-prior experiment for Scottish Progressive Chess.

This module intentionally does not plug into the normal search, league, CLI, or
web application.  Stockfish 18 searches orthodox alternating chess, so its
output is treated only as an untrusted micro-move preference.  The project's
Scottish move generator validates every choice, and :func:`play_series`
replays the complete result before it is returned.

Run the opt-in position benchmark with::

    python -m experiments.stockfish.stockfish_policy \
        --stockfish path/to/stockfish --output benchmark.json
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import subprocess
import threading
import time
from typing import Protocol, Sequence

import chess

from scottish_progressive.model import (
    ENGINE_SOURCE_FINGERPRINT,
    Outcome,
    ProgressiveState,
    SeriesResult,
)
from scottish_progressive.profiles import load_profile
from scottish_progressive.rules import _legal_move_variants, play_series
from scottish_progressive.search import SearchLimits, analyze


@dataclass(frozen=True, slots=True)
class OrthodoxCandidate:
    """One root move reported by an orthodox UCI search."""

    move: str
    rank: int = 1
    score_cp: int | None = None
    mate: int | None = None
    depth: int | None = None
    nodes: int | None = None


class OrthodoxAnalyzer(Protocol):
    """Small injectable boundary used by the fake-protocol unit tests."""

    @property
    def engine_id(self) -> str: ...

    def candidates(
        self,
        fen: str,
        *,
        nodes: int,
        multipv: int,
    ) -> tuple[OrthodoxCandidate, ...]: ...


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    move_index: int
    fen_queries: tuple[str, ...]
    legal_moves: tuple[str, ...]
    reported_moves: tuple[str, ...]
    selected_move: str
    used_fallback: bool


@dataclass(frozen=True, slots=True)
class PolicyResult:
    series: SeriesResult
    decisions: tuple[PolicyDecision, ...]
    engine_calls: int
    elapsed_seconds: float


def _parse_info_candidate(line: str) -> OrthodoxCandidate | None:
    """Parses the subset of Stockfish ``info`` needed by this experiment."""

    fields = line.split()
    if not fields or fields[0] != "info" or "pv" not in fields:
        return None
    pv_index = fields.index("pv")
    if pv_index + 1 >= len(fields):
        return None

    def integer_after(name: str) -> int | None:
        try:
            index = fields.index(name)
            return int(fields[index + 1])
        except (ValueError, IndexError):
            return None

    score_cp: int | None = None
    mate: int | None = None
    try:
        score_index = fields.index("score")
        score_kind = fields[score_index + 1]
        score_value = int(fields[score_index + 2])
        if score_kind == "cp":
            score_cp = score_value
        elif score_kind == "mate":
            mate = score_value
    except (ValueError, IndexError):
        pass

    return OrthodoxCandidate(
        move=fields[pv_index + 1],
        rank=integer_after("multipv") or 1,
        score_cp=score_cp,
        mate=mate,
        depth=integer_after("depth"),
        nodes=integer_after("nodes"),
    )


class UciStockfish:
    """Persistent, timeout-bounded UCI client configured deterministically."""

    def __init__(
        self,
        executable: str | os.PathLike[str],
        *,
        timeout_seconds: float = 10.0,
        hash_mb: int = 16,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if hash_mb < 1:
            raise ValueError("hash_mb must be positive")
        self.executable = Path(executable).expanduser().resolve()
        if not self.executable.is_file():
            raise FileNotFoundError(self.executable)
        self.timeout_seconds = timeout_seconds
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._process = subprocess.Popen(
            [str(self.executable)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()
        self._engine_id = "unknown UCI engine"

        self._send("uci")
        handshake = self._read_until(lambda line: line == "uciok")
        for line in handshake:
            if line.startswith("id name "):
                self._engine_id = line.removeprefix("id name ").strip()
        self._send("setoption name Threads value 1")
        self._send(f"setoption name Hash value {hash_mb}")
        self._synchronize()

    @property
    def engine_id(self) -> str:
        return self._engine_id

    def _read_output(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                self._lines.put(line.rstrip("\r\n"))
        finally:
            self._lines.put(None)

    def _send(self, command: str) -> None:
        if self._process.poll() is not None:
            raise RuntimeError(f"UCI engine exited with code {self._process.returncode}")
        assert self._process.stdin is not None
        self._process.stdin.write(command + "\n")
        self._process.stdin.flush()

    def _read_until(self, predicate) -> list[str]:
        deadline = time.monotonic() + self.timeout_seconds
        lines: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("UCI engine response timed out")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty as error:
                raise TimeoutError("UCI engine response timed out") from error
            if line is None:
                raise RuntimeError(
                    f"UCI engine exited with code {self._process.poll()}"
                )
            lines.append(line)
            if predicate(line):
                return lines

    def _synchronize(self) -> None:
        self._send("isready")
        self._read_until(lambda line: line == "readyok")

    def candidates(
        self,
        fen: str,
        *,
        nodes: int,
        multipv: int,
    ) -> tuple[OrthodoxCandidate, ...]:
        if nodes < 1:
            raise ValueError("nodes must be positive")
        if not 1 <= multipv <= 256:
            raise ValueError("multipv must be between 1 and 256")

        # Each query is independent. Clearing the TT prevents fixture order
        # from changing a fixed-node result.
        self._send(f"setoption name MultiPV value {multipv}")
        self._send("setoption name Clear Hash")
        self._synchronize()
        self._send(f"position fen {fen}")
        self._send(f"go nodes {nodes}")
        try:
            lines = self._read_until(lambda line: line.startswith("bestmove "))
        except TimeoutError:
            # Bring the protocol back to an idle boundary before surfacing the
            # failure. A caller may catch the error and safely issue a new query.
            self._send("stop")
            try:
                self._read_until(lambda line: line.startswith("bestmove "))
            except (TimeoutError, RuntimeError):
                self.close()
            raise

        latest: dict[int, OrthodoxCandidate] = {}
        bestmove: str | None = None
        for line in lines:
            parsed = _parse_info_candidate(line)
            if parsed is not None:
                latest[parsed.rank] = parsed
            elif line.startswith("bestmove "):
                fields = line.split()
                if len(fields) >= 2 and fields[1] != "(none)":
                    bestmove = fields[1]
        if bestmove is not None and not any(
            candidate.move == bestmove for candidate in latest.values()
        ):
            latest[1] = OrthodoxCandidate(move=bestmove)
        return tuple(latest[rank] for rank in sorted(latest))

    def close(self) -> None:
        if self._process.poll() is not None:
            return
        try:
            self._send("quit")
            self._process.wait(timeout=min(2.0, self.timeout_seconds))
        except (RuntimeError, subprocess.TimeoutExpired):
            self._process.terminate()
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2.0)

    def __enter__(self) -> UciStockfish:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _candidate_value(candidate: OrthodoxCandidate) -> int:
    """Comparable root score from the root side-to-move perspective."""

    if candidate.mate is not None:
        if candidate.mate > 0:
            return 10_000_000 - candidate.mate
        return -10_000_000 - candidate.mate
    return candidate.score_cp if candidate.score_cp is not None else -20_000_000


def _query_fens(
    board: chess.Board,
    first_move_ep_targets: Sequence[int],
) -> tuple[tuple[str, int | None], ...]:
    targets: tuple[int | None, ...] = (
        tuple(sorted(set(first_move_ep_targets))) if first_move_ep_targets else (None,)
    )
    queries: list[tuple[str, int | None]] = []
    for target in targets:
        query = board.copy(stack=False)
        query.ep_square = target
        queries.append((query.fen(en_passant="fen"), target))
    return tuple(queries)


def select_stockfish_series(
    state: ProgressiveState,
    analyzer: OrthodoxAnalyzer,
    *,
    nodes_per_move: int = 4_096,
    multipv: int = 8,
) -> PolicyResult:
    """Builds one legal Scottish series from orthodox Stockfish preferences.

    This is deliberately a policy probe, not a Scottish evaluation. Stockfish
    is queried with the same mover restored after each non-checking move. Its
    recommendation is accepted only when the project's legal-move primitive
    contains the exact UCI move (including the required progressive e.p.
    target). The completed sequence is then replayed through ``play_series``.
    """

    if nodes_per_move < 1:
        raise ValueError("nodes_per_move must be positive")
    if multipv < 1:
        raise ValueError("multipv must be positive")

    started = time.perf_counter()
    board = state.board.copy(stack=False)
    mover = board.turn
    selected_moves: list[str] = []
    decisions: list[PolicyDecision] = []
    engine_calls = 0

    for move_index in range(state.moves_available):
        ep_targets = state.ep_targets if move_index == 0 else ()
        legal_variants = _legal_move_variants(board, ep_targets)
        if not legal_variants:
            break
        legal = {move.uci(): (move, required_ep) for move, required_ep in legal_variants}

        reported: list[tuple[OrthodoxCandidate, int | None]] = []
        query_fens = _query_fens(board, ep_targets)
        for fen, query_ep in query_fens:
            engine_calls += 1
            for candidate in analyzer.candidates(
                fen,
                nodes=nodes_per_move,
                multipv=min(multipv, len(legal)),
            ):
                selected_variant = legal.get(candidate.move)
                if selected_variant is None:
                    continue
                required_ep = selected_variant[1]
                if required_ep is not None and required_ep != query_ep:
                    continue
                reported.append((candidate, query_ep))

        if reported:
            candidate, _ = max(
                reported,
                key=lambda item: (
                    _candidate_value(item[0]),
                    -item[0].rank,
                    item[0].move,
                ),
            )
            selected_uci = candidate.move
            used_fallback = False
        else:
            # Failing closed to a deterministic project-legal move keeps the
            # probe runnable while making the loss of Stockfish signal visible.
            selected_uci = min(legal)
            used_fallback = True

        move, required_ep = legal[selected_uci]
        decisions.append(
            PolicyDecision(
                move_index=move_index + 1,
                fen_queries=tuple(fen for fen, _ in query_fens),
                legal_moves=tuple(sorted(legal)),
                reported_moves=tuple(item[0].move for item in reported),
                selected_move=selected_uci,
                used_fallback=used_fallback,
            )
        )
        board.ep_square = required_ep
        board.push(move)
        selected_moves.append(selected_uci)
        if board.is_check() or len(selected_moves) == state.moves_available:
            break
        board.turn = mover
        board.ep_square = None

    # This final replay is the authority for early-check truncation, progressive
    # stalemate, promotion reuse, multi-e.p. semantics, and next-boundary state.
    series = play_series(state, selected_moves)
    return PolicyResult(
        series=series,
        decisions=tuple(decisions),
        engine_calls=engine_calls,
        elapsed_seconds=time.perf_counter() - started,
    )


@dataclass(frozen=True, slots=True)
class TacticalFixture:
    name: str
    fen: str
    series_number: int
    expected_moves: tuple[str, ...]


TACTICAL_FIXTURES = (
    TacticalFixture(
        "checked-start-mate",
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3",
        4,
        ("c7c6", "d8b6", "f6e4", "b6f2"),
    ),
    TacticalFixture(
        "wrong-defense-punishment",
        "rn1qkb1r/ppp1pppp/5n2/3P4/8/5N2/PPPP1PPP/RNBbK2R w KQkq - 0 7",
        5,
        ("f3e5", "g2g4", "g4g5", "g5g6", "g6f7"),
    ),
    TacticalFixture(
        "underpromotion-avoids-early-check",
        "bnq1nr2/p1pp1pk1/8/4PP2/1P2P1p1/8/P1P2KP1/BNbBN2r w - - 0 1",
        7,
        ("e1f3", "f3d4", "e5e6", "e6e7", "e7f8r", "f8h8", "d4e6"),
    ),
    TacticalFixture(
        "capture-promotion-then-reuse",
        "7R/pp3p1p/1p3k2/3P4/1b6/5P2/PPP2P1P/RNK5 b - - 0 1",
        8,
        ("b4d6", "b6b5", "b5b4", "b4b3", "b3a2", "a2b1n", "b1c3", "d6f4"),
    ),
    TacticalFixture(
        "uk-scottish-tournament-mate",
        "rnbqkb1r/ppp1pppp/5n2/3p2B1/3P4/2N2N2/PPP1PPPP/R2QKB1R b KQkq - 4 3",
        4,
        ("f6e4", "d8d6", "d6g3", "g3f2"),
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_position_benchmark(
    analyzer: OrthodoxAnalyzer,
    *,
    nodes_per_move: int,
    multipv: int,
    champion_path: Path | None = None,
    champion_work_limit: int = 50_000,
) -> dict[str, object]:
    """Compares the policy and current champion on fixed tactical positions."""

    champion = load_profile(champion_path) if champion_path is not None else None
    rows: list[dict[str, object]] = []
    for fixture in TACTICAL_FIXTURES:
        state = ProgressiveState.from_fen(fixture.fen, fixture.series_number)
        policy = select_stockfish_series(
            state,
            analyzer,
            nodes_per_move=nodes_per_move,
            multipv=multipv,
        )
        row: dict[str, object] = {
            "name": fixture.name,
            "series_number": fixture.series_number,
            "expected": "/".join(fixture.expected_moves),
            "stockfish_policy": policy.series.machine_notation,
            "stockfish_policy_legal": True,
            "stockfish_policy_mate": policy.series.outcome == Outcome.CHECKMATE,
            "stockfish_policy_exact_anchor": policy.series.moves == fixture.expected_moves,
            "stockfish_engine_calls": policy.engine_calls,
            "stockfish_fallbacks": sum(item.used_fallback for item in policy.decisions),
            "stockfish_elapsed_seconds": round(policy.elapsed_seconds, 6),
        }
        if champion is not None:
            champion_result = analyze(
                state,
                SearchLimits(
                    depth_series=1,
                    max_series_per_node=champion.recommended_branch_cap,
                    max_generation_positions=champion_work_limit,
                    collect_all_root_scores=False,
                ),
                profile=champion,
            )
            chosen = champion_result.best_series
            row.update(
                {
                    "champion_series": chosen.machine_notation if chosen else None,
                    "champion_mate": bool(chosen and chosen.outcome == Outcome.CHECKMATE),
                    "champion_exact_anchor": bool(
                        chosen and chosen.moves == fixture.expected_moves
                    ),
                    "champion_elapsed_seconds": round(
                        champion_result.elapsed_seconds, 6
                    ),
                    "champion_work_positions": champion_result.stats.work_positions,
                    "champion_work_limit_reached": champion_result.work_limit_reached,
                    "champion_exact_width": champion_result.exact_width,
                }
            )
        rows.append(row)

    return {
        "format": "spc-stockfish-policy-spike-v1",
        "engine_id": analyzer.engine_id,
        "engine_source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "nodes_per_micro_move": nodes_per_move,
        "multipv": multipv,
        "champion_profile_id": champion.profile_id if champion is not None else None,
        "champion_work_limit": champion_work_limit if champion is not None else None,
        "positions": rows,
        "summary": {
            "positions": len(rows),
            "stockfish_policy_legal": sum(
                bool(row["stockfish_policy_legal"]) for row in rows
            ),
            "stockfish_policy_mates": sum(
                bool(row["stockfish_policy_mate"]) for row in rows
            ),
            "stockfish_policy_exact_anchors": sum(
                bool(row["stockfish_policy_exact_anchor"]) for row in rows
            ),
            "champion_mates": (
                sum(bool(row.get("champion_mate")) for row in rows)
                if champion is not None
                else None
            ),
            "champion_exact_anchors": (
                sum(bool(row.get("champion_exact_anchor")) for row in rows)
                if champion is not None
                else None
            ),
        },
    }


def find_stockfish() -> Path | None:
    for name in (
        "stockfish",
        "stockfish.exe",
        "stockfish-windows-x86-64-avx2.exe",
        "stockfish-windows-x86-64-sse41-popcnt.exe",
    ):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stockfish", type=Path)
    parser.add_argument("--nodes", type=int, default=4_096)
    parser.add_argument("--multipv", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--champion", type=Path)
    parser.add_argument("--champion-work", type=int, default=50_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    executable = args.stockfish or find_stockfish()
    if executable is None:
        parser.error("Stockfish was not found; pass --stockfish")
    with UciStockfish(executable, timeout_seconds=args.timeout) as engine:
        report = run_position_benchmark(
            engine,
            nodes_per_move=args.nodes,
            multipv=args.multipv,
            champion_path=args.champion,
            champion_work_limit=args.champion_work,
        )
    # The content hash is the durable provenance. Do not publish a user's
    # absolute home-directory path in a shareable benchmark artifact.
    report["stockfish_executable_name"] = executable.name
    report["stockfish_sha256"] = _sha256(executable.resolve())
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8", newline="\n")
        print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
