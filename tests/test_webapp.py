from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
from typing import Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import chess
import pytest

from scottish_progressive.cli import main
from scottish_progressive.model import ProgressiveState
from scottish_progressive.webapp import (
    APIError,
    MAX_ANALYSIS_SECONDS,
    analyze_payload,
    create_server,
    inspect_prefix,
    load_openings,
)


def test_prefix_exposes_only_legal_next_micro_moves() -> None:
    payload = inspect_prefix(ProgressiveState.initial(), ())

    assert payload["complete"] is False
    assert payload["moves_remaining"] == 1
    assert len(payload["legal_moves"]) == 20
    e4 = next(move for move in payload["legal_moves"] if move["uci"] == "e2e4")
    assert e4 == {
        "uci": "e2e4",
        "san": "e4",
        "from": "e2",
        "to": "e4",
        "promotion": None,
        "capture": False,
        "gives_check": False,
    }


def test_prefix_replays_from_boundary_and_completes_without_full_generation() -> None:
    payload = inspect_prefix(ProgressiveState.initial(), ("e2e4",))

    assert payload["complete"] is True
    assert payload["completion_reason"] == "budget"
    assert payload["prefix"] == ["e2e4"]
    assert payload["san"] == ["e4"]
    assert payload["legal_next"] == []
    assert payload["next_state"]["series"] == 2
    assert payload["side_to_move"] == "black"
    assert payload["active_series_side"] == "white"


def test_prefix_countercheck_ends_series_early() -> None:
    state = ProgressiveState.from_fen(
        "r7/k6R/8/K7/8/8/8/8 b - - 0 1",
        2,
    )
    payload = inspect_prefix(state, ("a7b8",))

    assert payload["complete"] is True
    assert payload["ended_by_check"] is True
    assert payload["completion_reason"] == "check"
    assert payload["moves_remaining"] == 1
    assert payload["unused_moves"] == 1
    assert payload["san"] == ["Kb8+"]


def test_prefix_reconstructs_multiple_progressive_ep_targets() -> None:
    state = ProgressiveState.from_fen(
        "7k/3p1p2/8/4P1P1/8/8/8/K7 b - - 0 1",
        2,
    )
    payload = inspect_prefix(state, ("d7d5", "f7f5"))

    assert payload["complete"] is True
    assert payload["next_state"]["ep_targets"] == ["d6", "f6"]

    reply = ProgressiveState.from_fen(
        payload["next_state"]["fen"],
        3,
        ep_targets=(chess.D6, chess.F6),
    )
    legal = {move["uci"] for move in inspect_prefix(reply, ())["legal_next"]}
    assert {"e5d6", "e5f6", "g5f6"} <= legal


def test_prefix_rejects_illegal_and_overlong_client_paths() -> None:
    with pytest.raises(APIError, match="illegal move") as illegal:
        inspect_prefix(ProgressiveState.initial(), ("e2e5",))
    assert illegal.value.status == 422
    assert illegal.value.code == "illegal-move"

    with pytest.raises(APIError, match="series budget") as overflow:
        inspect_prefix(ProgressiveState.initial(), ("e2e4", "e4e5"))
    assert overflow.value.code == "series-overflow"


def test_analysis_is_real_bounded_search_and_labels_score_unit() -> None:
    result = analyze_payload(
        {
            "fen": "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
            "series": 1,
            "prefix": [],
            "depth": 1,
            "max_series": 32,
            "time_limit": 2,
            "alternatives": 3,
        }
    )

    assert result["classification"] == "Forced Win"
    assert result["proven_result"] == "white"
    assert result["proof"] == "white"
    assert result["score_unit"] == "heuristic-points"
    assert result["score_is_centipawns"] is False
    assert result["score"] == result["score_heuristic_points"]
    assert isinstance(result["reach_complete"], bool)
    assert result["requested_depth"] == 1
    assert result["completed_depth"] == 1
    assert result["exact_width"] is True
    assert result["timed_out"] is False
    assert result["principal_variation"]
    assert result["stats"]["generated_unique_series"] >= 1


def test_analysis_accepts_a_trusted_incomplete_prefix_and_enforces_time_ceiling() -> None:
    fixed = analyze_payload(
        {
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "series": 2,
            "prefix": ["e7e5"],
            "depth": 1,
            "max_series": 16,
            "time_limit": 2,
            "max_generation_positions": 50_000,
        }
    )
    assert fixed["analysis_scope"] == "series-prefix"
    assert fixed["fixed_prefix"] == ["e7e5"]
    assert fixed["prefix_complete"] is False
    assert fixed["required_prefix"] == ["e7e5"]
    assert fixed["analysis_searches"] == 1
    assert fixed["best_full_series"][:1] == ["e7e5"]
    assert fixed["best_completion"] == fixed["best_full_series"][1:]
    assert all(
        alternative["full_series"][:1] == ["e7e5"]
        for alternative in fixed["alternatives"]
    )

    with pytest.raises(APIError, match="time_limit") as too_long:
        analyze_payload(
            {
                "fen": chess.STARTING_FEN,
                "series": 1,
                "time_limit": MAX_ANALYSIS_SECONDS + 1,
            }
        )
    assert too_long.value.status == 422


def test_analysis_can_grade_the_latest_micro_move_against_its_parent_prefix() -> None:
    result = analyze_payload(
        {
            "fen": "7k/8/8/8/8/8/8/K7 w - - 0 1",
            "series": 3,
            "prefix": ["a1a2", "a2a3"],
            "depth": 1,
            "max_series": 512,
            "time_limit": 2,
            "max_generation_positions": 100_000,
            "rate_move": True,
        }
    )

    quality = result["move_quality"]
    assert quality["subject"] == "micro-move"
    assert quality["played_prefix"] == ["a1a2", "a2a3"]
    assert quality["evidence"]["parent"]["required_prefix"] == ["a1a2"]
    assert quality["evidence"]["fixed_prefix"]["required_prefix"] == [
        "a1a2",
        "a2a3",
    ]
    assert quality["label"] == "Not rated"
    assert quality["reasons"] == ["shallow-evidence"]
    assert result["analysis_searches"] == 2
    assert result["request_time_limit_seconds"] == 2.0
    assert result["time_limit_seconds"] == 1.0
    assert result["request_max_generation_positions"] == 100_000
    assert result["max_generation_positions"] == 50_000


def test_completed_series_analysis_grades_final_micro_and_searches_next_boundary() -> None:
    result = analyze_payload(
        {
            "fen": chess.STARTING_FEN,
            "series": 1,
            "prefix": ["e2e4"],
            "depth": 1,
            "max_series": 32,
            "time_limit": 2,
            "max_generation_positions": 100_000,
            "rate_move": True,
        }
    )

    assert result["analysis_scope"] == "next-boundary"
    assert result["state"]["series"] == 2
    assert result["fixed_prefix"] == ["e2e4"]
    assert result["required_prefix"] == []
    assert result["analysis_searches"] == 3
    assert result["request_time_limit_seconds"] == 2.0
    assert result["request_max_generation_positions"] == 100_000
    quality = result["move_quality"]
    assert quality["played_prefix"] == ["e2e4"]
    assert quality["evidence"]["parent"]["required_prefix"] == []
    assert quality["evidence"]["fixed_prefix"]["required_prefix"] == ["e2e4"]


def test_analysis_rejects_a_prefix_that_already_ended_the_game() -> None:
    with pytest.raises(APIError, match="already ended") as terminal:
        analyze_payload(
            {
                "fen": "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
                "series": 1,
                "prefix": ["g6g7"],
                "depth": 1,
            }
        )
    assert terminal.value.status == 409
    assert terminal.value.code == "game-over"
    assert terminal.value.details == {
        "outcome": "checkmate",
        "notation": "Qg7#",
        "unused_moves": 0,
    }

    with pytest.raises(APIError, match="already ended") as stalemate:
        analyze_payload(
            {
                "fen": "8/8/8/8/8/p7/8/k1K5 b - - 0 1",
                "series": 2,
                "prefix": ["a3a2"],
                "depth": 1,
            }
        )
    assert stalemate.value.status == 409
    assert stalemate.value.code == "game-over"
    assert stalemate.value.details == {
        "outcome": "stalemate",
        "notation": "a2",
        "unused_moves": 1,
    }


def test_already_terminal_boundary_has_no_phantom_series() -> None:
    result = analyze_payload(
        {
            "fen": "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1",
            "series": 2,
            "depth": 1,
        }
    )
    assert result["terminal"] is True
    assert result["terminal_outcome"] == "checkmate"
    assert result["best_series"] is None
    assert result["principal_variation"] == []
    assert result["principal_variation_text"] is None
    assert result["alternatives"] == []


def test_analysis_can_save_only_to_configured_database(tmp_path: Path) -> None:
    database = tmp_path / "theory.sqlite3"
    result = analyze_payload(
        {
            "fen": "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
            "series": 1,
            "depth": 1,
            "max_series": 32,
            "time_limit": 2,
            "save": True,
        },
        database_path=database,
    )

    assert result["saved"] is True
    assert isinstance(result["analysis_id"], int)
    assert database.exists()

    with pytest.raises(APIError, match="cannot override") as denied:
        analyze_payload(
            {
                "fen": "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
                "series": 1,
                "depth": 1,
                "time_limit": 1,
                "database": str(tmp_path / "other.sqlite3"),
            },
            database_path=database,
        )
    assert denied.value.status == 403


def test_openings_loads_known_reports_and_marks_fingerprint(tmp_path: Path) -> None:
    report = {
        "source_fingerprint": "fixture",
        "results": [{"move_uci": "e2e4", "move_san": "e4"}],
    }
    (tmp_path / "initial-opening-ranking.json").write_text(
        json.dumps(report), encoding="utf-8"
    )

    payload = load_openings(tmp_path)

    assert payload["available"] is True
    loaded = payload["reports"]["initial_ranking"]
    assert loaded["current"] is False
    assert loaded["data"] == report


@contextmanager
def running_server(tmp_path: Path, *, request_limit: int = 256 * 1024) -> Iterator[str]:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>Board</title>", encoding="utf-8")
    server = create_server(
        "127.0.0.1",
        0,
        static_root=static,
        reports_dir=tmp_path,
        request_limit=request_limit,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def get_json(url: str) -> tuple[int, dict[str, object]]:
    with urlopen(url, timeout=3) as response:
        return response.status, json.loads(response.read())


def post_json(url: str, payload: object) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def test_http_health_prefix_static_and_traversal(tmp_path: Path) -> None:
    with running_server(tmp_path) as base:
        status, health = get_json(f"{base}/api/health")
        assert status == 200
        assert health["ok"] is True
        assert health["analysis_limits"]["maximum_seconds"] == MAX_ANALYSIS_SECONDS
        assert len(health["ui_source_fingerprint"]) == 16

        status, prefix = post_json(
            f"{base}/api/prefix",
            {
                "fen": chess.STARTING_FEN,
                "series": 1,
                "prefix": ["e2e4"],
                # An alleged intermediate board is ignored; the prefix is
                # always replayed from the supplied boundary FEN.
                "current_fen": "8/8/8/8/8/8/8/8 w - - 0 1",
            },
        )
        assert status == 200
        assert prefix["complete"] is True
        assert "4P3" in prefix["board_fen"]

        with urlopen(f"{base}/", timeout=3) as response:
            assert response.status == 200
            assert b"Board" in response.read()

        with pytest.raises(HTTPError) as traversal:
            urlopen(f"{base}/%2e%2e/secret", timeout=3)
        assert traversal.value.code == 404


def test_http_rejects_oversized_json_before_parsing(tmp_path: Path) -> None:
    with running_server(tmp_path, request_limit=1024) as base:
        status, payload = post_json(
            f"{base}/api/prefix",
            {"fen": chess.STARTING_FEN, "series": 1, "padding": "x" * 2000},
        )
    assert status == 413
    assert payload["error"]["code"] == "request-too-large"


def test_http_default_board_analysis_needs_no_database(tmp_path: Path) -> None:
    with running_server(tmp_path) as base:
        status, result = post_json(
            f"{base}/api/analyze",
            {
                "fen": chess.STARTING_FEN,
                "series": 1,
                "quiet_series": 0,
                "ep_targets": [],
                "prefix": [],
                "depth": 1,
                "max_series": 24,
                "time_limit": 2,
                "alternatives": 4,
                "save": False,
            },
        )

    assert status == 200
    assert result["saved"] is False
    assert result["best_series_uci"]


def test_web_cli_defaults_to_loopback_and_can_disable_browser(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_serve(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("scottish_progressive.webapp.serve", fake_serve)
    assert main(["web", "--no-browser", "--port", "9012"]) == 0
    assert captured == {
        "host": "127.0.0.1",
        "port": 9012,
        "open_browser": False,
        "database": None,
    }


def test_server_rejects_non_loopback_binding(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("board", encoding="utf-8")
    with pytest.raises(ValueError, match="local-only"):
        create_server("0.0.0.0", 0, static_root=static)


def test_http_rejects_dns_rebinding_host_and_cross_origin_post(tmp_path: Path) -> None:
    with running_server(tmp_path) as base:
        hostile_host = Request(
            f"{base}/api/health",
            headers={"Host": "attacker.example"},
        )
        with pytest.raises(HTTPError) as host_error:
            urlopen(hostile_host, timeout=3)
        assert host_error.value.code == 403
        assert json.loads(host_error.value.read())["error"]["code"] == "invalid-host"

        body = json.dumps(
            {"fen": chess.STARTING_FEN, "series": 1, "prefix": []}
        ).encode("utf-8")
        hostile_origin = Request(
            f"{base}/api/prefix",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://attacker.example",
            },
        )
        with pytest.raises(HTTPError) as origin_error:
            urlopen(hostile_origin, timeout=3)
        assert origin_error.value.code == 403
        assert json.loads(origin_error.value.read())["error"]["code"] == "invalid-origin"
