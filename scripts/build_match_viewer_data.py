from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from scottish_progressive.external import replay_series_history
from scottish_progressive.model import ProgressiveState
from scottish_progressive.rules import play_series
from scottish_progressive.webapp import inspect_prefix


DEFAULT_RECEIPT = ROOT / "reports" / "bucephalus-rematch-100games-30s-20260827.json"
DEFAULT_OUTPUT = (
    ROOT / "src" / "scottish_progressive" / "web" / "static" / "matches"
)
EXPECTED_RECEIPT_SHA256 = (
    "6aa5f81d521bc60f8bb368179a4b30b89abd79325f54de432e6617dafdbca646"
)
DATA_STEM = "match"


class ViewerDataError(RuntimeError):
    """The immutable benchmark receipt cannot produce an authoritative replay."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalized_lf_bytes(data: bytes) -> bytes:
    """Return portable source bytes without changing any JSON content."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ViewerDataError(f"{label} must be an object")
    return value


def _require_sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ViewerDataError(f"{label} must be an array")
    return value


def _orthodox_fen(pfen: str) -> str:
    fen, separator, _ = str(pfen).partition(" | ")
    if not separator or len(fen.split()) != 6:
        raise ViewerDataError(f"invalid progressive FEN: {pfen!r}")
    return fen


def _split_series(value: object, label: str) -> tuple[str, ...]:
    moves = tuple(token for token in str(value or "").split("/") if token)
    if not moves:
        raise ViewerDataError(f"{label} did not contain a played series")
    return moves


def _status(game: Mapping[str, Any]) -> str:
    if game.get("result") in {"1-0", "0-1", "1/2-1/2"}:
        if game.get("error") is not None or game.get("technical_failure_owner") is not None:
            return "integrity"
        return "completed"
    reason = str(game.get("terminal_reason") or "").lower()
    error = str(game.get("error") or "").lower()
    combined = f"{reason} {error}"
    if "timeout" in combined or "deadline" in combined:
        return "timeout"
    if any(
        token in combined
        for token in (
            "identity",
            "illegal",
            "integrity",
            "mismatch",
            "replay",
        )
    ):
        return "integrity"
    return "technical"


def _engine_player(
    key: str,
    local_player: Mapping[str, object],
    external_player: Mapping[str, object],
) -> Mapping[str, object]:
    return local_player if key == "local" else external_player


def _initial_frame(state: ProgressiveState) -> dict[str, object]:
    return {
        "engine": "opening-suite",
        "engine_name": "Neutral seeded opening",
        "fen": state.board.fen(en_passant="fen"),
        "frame": 0,
        "is_benchmark_start": False,
        "is_series_end": False,
        "last_move": None,
        "notation": "Initial position",
        "outcome": None,
        "phase": "opening",
        "pfen": state.pfen,
        "san": None,
        "series_length": state.series_number,
        "series_move": 0,
        "series_number": state.series_number,
        "side": "white",
        "uci": None,
    }


def _append_series_frames(
    frames: list[dict[str, object]],
    state: ProgressiveState,
    moves: tuple[str, ...],
    *,
    phase: str,
    engine: str,
    engine_name: str,
    expected_notation: object | None = None,
    trace: Mapping[str, Any] | None = None,
) -> ProgressiveState:
    prefix = inspect_prefix(state, moves)
    authoritative = play_series(state, moves)
    if prefix.get("complete") is not True:
        raise ViewerDataError(
            f"series {state.series_number} was not complete after authoritative replay"
        )
    if tuple(prefix.get("prefix") or ()) != authoritative.moves:
        raise ViewerDataError(
            f"series {state.series_number} prefix disagreed with authoritative replay"
        )
    if tuple(prefix.get("san") or ()) != authoritative.san:
        raise ViewerDataError(
            f"series {state.series_number} notation disagreed with authoritative replay"
        )
    if expected_notation is not None and str(expected_notation) != authoritative.notation:
        raise ViewerDataError(
            f"series {state.series_number} receipt notation disagreed with replay"
        )

    raw_frames = _require_sequence(prefix.get("frames"), "prefix frames")
    if len(raw_frames) != len(moves):
        raise ViewerDataError(
            f"series {state.series_number} frame count disagreed with played moves"
        )
    for index, (uci, raw_frame) in enumerate(zip(moves, raw_frames, strict=True), 1):
        item = _require_mapping(raw_frame, "prefix frame")
        if item.get("uci") != uci:
            raise ViewerDataError(
                f"series {state.series_number} frame {index} changed move identity"
            )
        final_micro_move = index == len(moves)
        fen = str(item.get("board_fen"))
        if final_micro_move:
            fen = authoritative.final_state.board.fen(en_passant="fen")
        frame: dict[str, object] = {
            "engine": engine,
            "engine_name": engine_name,
            "fen": fen,
            "frame": len(frames),
            "is_benchmark_start": False,
            "is_series_end": final_micro_move,
            "last_move": uci,
            "notation": authoritative.notation,
            "outcome": (
                authoritative.outcome.value
                if final_micro_move and authoritative.outcome is not None
                else None
            ),
            "phase": phase,
            "pfen": authoritative.final_state.pfen if final_micro_move else None,
            "san": authoritative.san[index - 1],
            "series_length": len(moves),
            "series_move": index,
            "series_number": state.series_number,
            "side": "white" if state.board.turn else "black",
            "uci": uci,
        }
        if trace is not None and final_micro_move:
            frame["deadline_reached"] = bool(trace.get("deadline_reached"))
            frame["elapsed_seconds"] = trace.get(
                "local_wall_elapsed_seconds",
                trace.get("external_call_wall_elapsed_seconds"),
            )
            frame["search_depth"] = trace.get(
                "completed_depth_series",
                trace.get("completed_micro_ply"),
            )
        frames.append(frame)
    return authoritative.final_state


def _benchmark_start_frame(
    frames: list[dict[str, object]],
    state: ProgressiveState,
) -> int:
    if not frames:
        raise ViewerDataError("benchmark start requires a replayed position")
    index = len(frames) - 1
    frame = frames[index]
    if frame.get("fen") != state.board.fen(en_passant="fen"):
        raise ViewerDataError("benchmark start board drifted from opening replay")
    if frame.get("pfen") != state.pfen:
        raise ViewerDataError("benchmark start PFEN drifted from opening replay")
    frame["is_benchmark_start"] = True
    return index


def _build_game(
    game: Mapping[str, Any],
    *,
    game_number: int,
    opening: Mapping[str, Any],
    local_player: Mapping[str, object],
    external_player: Mapping[str, object],
) -> dict[str, object]:
    opening_history = tuple(
        tuple(str(move) for move in _require_sequence(series, "opening series"))
        for series in _require_sequence(
            opening.get("canonical_series_history"),
            "opening canonical history",
        )
    )
    state = ProgressiveState.initial()
    frames = [_initial_frame(state)]
    for moves in opening_history:
        state = _append_series_frames(
            frames,
            state,
            moves,
            phase="opening",
            engine="opening-suite",
            engine_name="Neutral seeded opening",
        )

    replayed_opening = replay_series_history(opening_history)
    if replayed_opening.position_hash != state.position_hash:
        raise ViewerDataError("opening replay paths disagreed")
    expected_start = str(game.get("start_pfen"))
    if state.pfen != expected_start or str(opening.get("pfen")) != expected_start:
        raise ViewerDataError(
            f"game {game.get('game_id')} start position did not match its opening"
        )
    benchmark_start_frame = _benchmark_start_frame(frames, state)

    canonical_history = [list(moves) for moves in opening_history]
    played_series = 0
    for trace_index, raw_trace in enumerate(
        _require_sequence(game.get("trace"), "game trace"),
        1,
    ):
        trace = _require_mapping(raw_trace, f"trace {trace_index}")
        before_pfen = str(trace.get("before_pfen"))
        if before_pfen != state.pfen:
            raise ViewerDataError(
                f"game {game.get('game_id')} trace {trace_index} before PFEN drifted"
            )
        if trace.get("played") is not True:
            if trace.get("selected_series") not in {None, ""}:
                raise ViewerDataError(
                    f"game {game.get('game_id')} unplayed trace had a selected series"
                )
            continue

        moves = _split_series(
            trace.get("authoritative_series"),
            f"game {game.get('game_id')} trace {trace_index}",
        )
        selected = trace.get("selected_series")
        if selected not in {None, trace.get("authoritative_series")}:
            raise ViewerDataError(
                f"game {game.get('game_id')} trace {trace_index} selected series drifted"
            )
        engine = str(trace.get("engine"))
        if engine not in {"local", "bucephalus"}:
            raise ViewerDataError(
                f"game {game.get('game_id')} trace {trace_index} has unknown engine"
            )
        player = _engine_player(engine, local_player, external_player)
        state = _append_series_frames(
            frames,
            state,
            moves,
            phase="benchmark",
            engine=engine,
            engine_name=str(player["name"]),
            expected_notation=trace.get("authoritative_notation"),
            trace=trace,
        )
        played_series += 1
        canonical_history.append(list(moves))
        if str(trace.get("after_pfen")) != state.pfen:
            raise ViewerDataError(
                f"game {game.get('game_id')} trace {trace_index} after PFEN drifted"
            )
        if trace.get("canonical_history_after") != canonical_history:
            raise ViewerDataError(
                f"game {game.get('game_id')} trace {trace_index} history drifted"
            )

    final_pfen = str(game.get("final_pfen"))
    if state.pfen != final_pfen:
        raise ViewerDataError(f"game {game.get('game_id')} final PFEN drifted")
    if played_series != int(game.get("series_played", -1)):
        raise ViewerDataError(f"game {game.get('game_id')} series count drifted")
    if _orthodox_fen(final_pfen) != str(frames[-1]["fen"]):
        raise ViewerDataError(f"game {game.get('game_id')} final frame drifted")

    local_color = str(game.get("local_color"))
    external_color = str(game.get("external_color"))
    if {local_color, external_color} != {"white", "black"}:
        raise ViewerDataError(f"game {game.get('game_id')} color assignment is invalid")
    white = local_player if local_color == "white" else external_player
    black = local_player if local_color == "black" else external_player
    result = str(game.get("result"))
    expected_winner_color = "white" if result == "1-0" else "black" if result == "0-1" else None
    if game.get("winner_color") != expected_winner_color:
        raise ViewerDataError(f"game {game.get('game_id')} winner color drifted")
    expected_winner = (
        str(white["key"])
        if expected_winner_color == "white"
        else str(black["key"])
        if expected_winner_color == "black"
        else None
    )
    if game.get("winner") != expected_winner:
        raise ViewerDataError(f"game {game.get('game_id')} winner identity drifted")

    status = _status(game)
    return {
        "benchmark_start_frame": benchmark_start_frame,
        "black": dict(black),
        "completed": status == "completed",
        "error": game.get("error"),
        "final_pfen": final_pfen,
        "frames": frames,
        "game_id": str(game.get("game_id")),
        "game_number": game_number,
        "local_work_positions": game.get("local_work_positions"),
        "opening_case_id": str(game.get("opening_case_id")),
        "pair_id": str(game.get("pair_id")),
        "pair_number": int(game.get("pair_index", -1)) + 1,
        "replay_verified": True,
        "result": result,
        "series_played": played_series,
        "start_pfen": expected_start,
        "status": status,
        "swap_number": int(game.get("swap_index", -1)) + 1,
        "technical_failure_owner": game.get("technical_failure_owner"),
        "terminal_reason": str(game.get("terminal_reason")),
        "white": dict(white),
        "winner": expected_winner,
        "winner_color": expected_winner_color,
    }


def _player_payload(receipt: Mapping[str, Any]) -> tuple[dict[str, object], dict[str, object]]:
    local = _require_mapping(receipt.get("local_engine"), "local engine")
    profile = _require_mapping(local.get("profile"), "local profile")
    backend = _require_mapping(local.get("backend"), "local backend")
    native = _require_mapping(
        backend.get("evaluation_native_module"),
        "local native module",
    )
    git = _require_mapping(local.get("git"), "local git identity")
    external = _require_mapping(receipt.get("external_engine"), "external engine")
    return (
        {
            "engine_version": str(local.get("engine_version")),
            "key": "local",
            "name": "Scottish Progressive",
            "native_module_sha256": str(native.get("sha256")),
            "native_source_identity": str(native.get("source_identity")),
            "profile_id": str(profile.get("profile_id")),
            "profile_name": str(profile.get("name")),
            "source_commit": str(git.get("head_commit")),
            "source_fingerprint": str(local.get("source_fingerprint")),
        },
        {
            "adapter_version": str(external.get("adapter_version")),
            "build_provenance": str(external.get("build_provenance")),
            "executable_sha256": str(external.get("executable_sha256")),
            "key": "bucephalus",
            "name": str(external.get("name")),
            "upstream_commit": str(external.get("upstream_commit")),
        },
    )


def build_bundle(receipt_path: Path, receipt_sha256: str) -> dict[str, object]:
    receipt = _require_mapping(
        json.loads(receipt_path.read_text(encoding="utf-8")),
        "benchmark receipt",
    )
    if receipt.get("format") != "spc-bucephalus-fixed-suite-v1":
        raise ViewerDataError("unsupported Bucephalus receipt format")
    config = _require_mapping(receipt.get("config"), "benchmark config")
    summary = _require_mapping(receipt.get("summary"), "benchmark summary")
    games_raw = _require_sequence(receipt.get("games"), "games")
    pairs_raw = _require_sequence(receipt.get("pairs"), "pairs")
    openings_raw = _require_sequence(receipt.get("selected_openings"), "selected openings")
    scheduled_games = int(summary.get("scheduled_games", -1))
    scheduled_pairs = int(config.get("pairs", -1))
    if len(games_raw) != scheduled_games or len(pairs_raw) != scheduled_pairs:
        raise ViewerDataError("scheduled game or pair count disagreed with receipt arrays")

    local_player, external_player = _player_payload(receipt)
    openings = {
        str(_require_mapping(item, "selected opening").get("case_id")): _require_mapping(
            item,
            "selected opening",
        )
        for item in openings_raw
    }
    games = []
    for index, raw_game in enumerate(games_raw, 1):
        game = _require_mapping(raw_game, f"game {index}")
        opening_id = str(game.get("opening_case_id"))
        if opening_id not in openings:
            raise ViewerDataError(f"game {index} references an unknown opening")
        games.append(
            _build_game(
                game,
                game_number=index,
                opening=openings[opening_id],
                local_player=local_player,
                external_player=external_player,
            )
        )

    game_numbers = {str(game["game_id"]): int(game["game_number"]) for game in games}
    pairs = []
    for raw_pair in pairs_raw:
        pair = _require_mapping(raw_pair, "pair")
        ids = [str(value) for value in _require_sequence(pair.get("game_ids"), "pair game ids")]
        if len(ids) != 2 or any(game_id not in game_numbers for game_id in ids):
            raise ViewerDataError(f"pair {pair.get('pair_id')} does not bind two games")
        pair_status = "completed" if pair.get("result") != "incomplete" else "technical"
        failure_reasons = " ".join(
            str(_require_mapping(item, "pair technical failure").get("reason") or "")
            for item in _require_sequence(
                pair.get("technical_failures"),
                "pair technical failures",
            )
        ).lower()
        if pair_status != "completed" and (
            "timeout" in failure_reasons or "deadline" in failure_reasons
        ):
            pair_status = "timeout"
        elif pair_status != "completed" and any(
            token in failure_reasons
            for token in ("identity", "illegal", "integrity", "mismatch", "replay")
        ):
            pair_status = "integrity"
        pairs.append(
            {
                "game_numbers": [game_numbers[game_id] for game_id in ids],
                "local_points": pair.get("local_points"),
                "opening_case_id": str(pair.get("opening_case_id")),
                "pair_id": str(pair.get("pair_id")),
                "pair_number": int(pair.get("pair_index", -1)) + 1,
                "result": str(pair.get("result")),
                "status": pair_status,
                "technical_failures": pair.get("technical_failures"),
            }
        )

    status_counts = Counter(str(game["status"]) for game in games)
    for status in ("completed", "integrity", "technical", "timeout"):
        status_counts.setdefault(status, 0)
    common_control = _require_mapping(config.get("common_control"), "common control")
    opening_suite = _require_mapping(receipt.get("opening_suite"), "opening suite")
    execution = _require_mapping(receipt.get("execution"), "execution")
    benchmark_harness = _require_mapping(
        receipt.get("benchmark_harness"),
        "benchmark harness",
    )
    return {
        "controls": {
            "policy": common_control.get("policy"),
            "wall_overrun_grace_seconds": common_control.get(
                "wall_overrun_grace_seconds"
            ),
            "wall_seconds_per_move": common_control.get("wall_seconds_per_move"),
        },
        "games": games,
        "pairs": pairs,
        "players": {
            "bucephalus": external_player,
            "local": local_player,
        },
        "provenance": {
            "benchmark_harness_artifact_set_sha256": benchmark_harness.get(
                "artifact_set_sha256"
            ),
            "opening_suite_sha256": opening_suite.get("canonical_sha256"),
            "opening_suite_version": opening_suite.get("version"),
            "receipt_sha256": receipt_sha256,
            "result_order": execution.get("result_order"),
            "ruleset_version": "scottish-modern-common-v1",
        },
        "schema": "spc-bucephalus-match-viewer-v1",
        "source": {
            "created_at": receipt.get("created_at"),
            "format": receipt.get("format"),
            "receipt_file": receipt_path.name,
            "receipt_sha256": receipt_sha256,
            "report_id": receipt.get("report_id"),
        },
        "summary": {
            "completed_games": int(summary.get("completed_games", -1)),
            "completed_pairs": int(summary.get("completed_pairs", -1)),
            "incomplete_games": int(summary.get("incomplete_games", -1)),
            "incomplete_pairs": int(summary.get("incomplete_pairs", -1)),
            "local_game_score_rate": summary.get("local_game_score_rate"),
            "local_game_wdl": summary.get("local_game_wdl"),
            "local_pair_score_rate": summary.get("local_pair_score_rate"),
            "local_pair_wdl": summary.get("local_pair_wdl"),
            "scheduled_games": scheduled_games,
            "scheduled_pairs": scheduled_pairs,
            "status_counts": dict(sorted(status_counts.items())),
            "strict_protocol_complete": bool(
                summary.get("strict_100_game_protocol_complete")
            ),
        },
        "title": "Scottish Progressive vs Bucephalus",
    }


def write_bundle(
    receipt_path: Path,
    output: Path,
    *,
    expected_receipt_sha256: str,
) -> tuple[Path, Path]:
    receipt_bytes = receipt_path.read_bytes()
    receipt_sha256 = _sha256(_normalized_lf_bytes(receipt_bytes))
    if receipt_sha256 != expected_receipt_sha256:
        raise ViewerDataError(
            "immutable receipt hash mismatch: "
            f"expected {expected_receipt_sha256}, got {receipt_sha256}"
        )
    bundle = build_bundle(receipt_path, receipt_sha256)
    data = _canonical_json(bundle)
    data_sha256 = _sha256(data)
    data_file = f"{DATA_STEM}.{data_sha256}.json"
    manifest = {
        "data_file": data_file,
        "data_sha256": data_sha256,
        "receipt_sha256": receipt_sha256,
        "schema": "spc-match-viewer-manifest-v1",
    }
    output.mkdir(parents=True, exist_ok=True)
    data_path = output / data_file
    manifest_path = output / "match-viewer-manifest.json"
    data_path.write_bytes(data)
    manifest_path.write_bytes(_canonical_json(manifest))
    return manifest_path, data_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic, content-addressed read-only match viewer "
            "bundle from the immutable Bucephalus receipt."
        )
    )
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--expected-receipt-sha256",
        default=EXPECTED_RECEIPT_SHA256,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the checked-in bundle without changing it",
    )
    arguments = parser.parse_args()
    receipt = arguments.receipt.resolve(strict=True)
    output = arguments.output.resolve()
    if arguments.check:
        with tempfile.TemporaryDirectory(prefix="spc-match-viewer-") as temporary:
            expected_manifest, expected_data = write_bundle(
                receipt,
                Path(temporary),
                expected_receipt_sha256=arguments.expected_receipt_sha256,
            )
            checked_manifest = output / expected_manifest.name
            checked_data = output / expected_data.name
            if not checked_manifest.is_file() or not checked_data.is_file():
                raise ViewerDataError("checked-in match viewer bundle is missing")
            if checked_manifest.read_bytes() != expected_manifest.read_bytes():
                raise ViewerDataError("checked-in match viewer manifest is stale")
            if checked_data.read_bytes() != expected_data.read_bytes():
                raise ViewerDataError("checked-in match viewer data is stale")
            actual_json = {path.name for path in output.glob("*.json")}
            expected_json = {expected_manifest.name, expected_data.name}
            if actual_json != expected_json:
                raise ViewerDataError(
                    f"unexpected match viewer data files: {sorted(actual_json - expected_json)}"
                )
        print(f"verified {checked_manifest}")
        print(f"verified {checked_data}")
    else:
        manifest, data = write_bundle(
            receipt,
            output,
            expected_receipt_sha256=arguments.expected_receipt_sha256,
        )
        print(f"wrote {manifest}")
        print(f"wrote {data}")


if __name__ == "__main__":
    main()
