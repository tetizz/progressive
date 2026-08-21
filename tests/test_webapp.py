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
    LOCAL_ANALYSIS_LIMITS,
    MAX_ANALYSIS_SECONDS,
    PUBLIC_ANALYSIS_LIMITS,
    PUBLIC_MAX_ANALYSIS_DEPTH,
    PUBLIC_MAX_ANALYSIS_SECONDS,
    PUBLIC_MAX_GENERATION_POSITIONS,
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
    assert payload["frames"] == [
        {
            "index": 1,
            "uci": "e2e4",
            "san": "e4",
            "board_fen": payload["frames"][0]["board_fen"],
            "gives_check": False,
        }
    ]
    frame = chess.Board(payload["frames"][0]["board_fen"])
    assert frame.piece_at(chess.E4) == chess.Piece(chess.PAWN, chess.WHITE)


def test_screenshot_line_opens_playable_s7_after_champion_s6() -> None:
    state = ProgressiveState.initial()
    series = (
        ("e2e4",),
        ("f7f5", "e8f7"),
        ("d2d4", "g1f3", "e1d2"),
        ("e7e5", "d8h4", "f5e4", "h4f2"),
        ("d1e2", "e2f2", "f2e3", "e3e4", "e4e5"),
        ("d7d6", "d6e5", "e5e4", "f8d6", "b8c6", "e4e3"),
    )
    for expected_series, moves in enumerate(series, 1):
        assert state.series_number == expected_series
        completed = inspect_prefix(state, moves)
        assert completed["complete"] is True
        assert completed["next_state"] is not None
        next_state = completed["next_state"]
        state = ProgressiveState.from_fen(
            next_state["fen"],
            next_state["series"],
            quiet_series=next_state["quiet_series"],
            ep_targets=tuple(
                chess.parse_square(square) for square in next_state["ep_targets"]
            ),
        )

    s7 = inspect_prefix(state, ())
    assert s7["complete"] is False
    assert s7["moves_remaining"] == 7
    assert s7["in_check"] is True
    assert {move["san"] for move in s7["legal_moves"]} == {
        "Kc3",
        "Kd1",
        "Kd3",
        "Ke1",
        "Ke2",
        "Kxe3",
    }


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
    assert result["mate_score"] == 1_000_000
    assert isinstance(result["reach_complete"], bool)
    assert result["requested_depth"] == 1
    assert result["completed_depth"] == 1
    assert result["exact_width"] is True
    assert result["timed_out"] is False
    assert result["principal_variation"]
    assert result["stats"]["generated_unique_series"] >= 1
    assert {
        "promotion_mate_positions",
        "promotion_mate_setup_states",
        "promotion_mate_candidates",
        "promotion_mate_completion_probes",
        "promotion_mate_limit_hits",
        "promotion_mate_replay_rejects",
        "promotion_mate_mates",
    } <= result["stats"].keys()


def test_deeper_play_request_reports_the_depth_it_actually_completed() -> None:
    result = analyze_payload(
        {
            "fen": chess.STARTING_FEN,
            "series": 1,
            "prefix": [],
            "depth": 3,
            "max_series": 32,
            "time_limit": 0.1,
            "max_generation_positions": 5_000_000,
            "alternatives": 0,
            "best_move_only": True,
        }
    )

    assert result["requested_depth"] == 3
    assert 1 <= result["completed_depth"] < result["requested_depth"]
    assert result["completed_depth"] < result["requested_depth"]
    assert result["timed_out"] is True
    assert result["exact_width"] is False
    assert result["best_full_series"]
    assert result["root_search_mode"] == "best-move"
    assert result["root_scores_complete"] is False


@pytest.mark.parametrize(
    "payload_update",
    (
        {"best_move_only": "yes", "alternatives": 0},
        {"best_move_only": True, "alternatives": 1},
        {"best_move_only": True, "alternatives": 0, "rate_move": True},
    ),
)
def test_best_move_mode_rejects_ambiguous_analysis_contracts(
    payload_update: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "fen": chess.STARTING_FEN,
        "series": 1,
        "depth": 1,
        "time_limit": 0.1,
        "max_generation_positions": 10_000,
    }
    payload.update(payload_update)

    with pytest.raises(APIError) as rejected:
        analyze_payload(payload)

    assert rejected.value.status == 422
    assert rejected.value.code == "invalid-field"


def test_loaded_profile_returns_server_replayable_complete_series_for_play() -> None:
    white = analyze_payload(
        {
            "fen": chess.STARTING_FEN,
            "series": 1,
            "prefix": [],
            "depth": 1,
            "max_series": 24,
            "time_limit": 2,
            "alternatives": 0,
        }
    )
    white_moves = tuple(white["best_full_series"])
    checked_white = inspect_prefix(ProgressiveState.initial(), white_moves)

    assert white["engine_profile_id"].startswith("spc-")
    assert white["source_fingerprint"]
    assert len(white_moves) == 1
    assert checked_white["complete"] is True
    assert checked_white["prefix"] == list(white_moves)
    assert len(checked_white["frames"]) == len(white_moves)

    black_state = ProgressiveState.from_fen(
        checked_white["next_state"]["fen"],
        checked_white["next_state"]["series"],
        quiet_series=checked_white["next_state"]["quiet_series"],
        ep_targets=tuple(
            chess.parse_square(square)
            for square in checked_white["next_state"]["ep_targets"]
        ),
    )
    black = analyze_payload(
        {
            **checked_white["next_state"],
            "prefix": [],
            "depth": 1,
            "max_series": 24,
            "time_limit": 2,
            "alternatives": 0,
        }
    )
    black_moves = tuple(black["best_full_series"])
    checked_black = inspect_prefix(black_state, black_moves)

    assert black["engine_profile_id"] == white["engine_profile_id"]
    assert black["source_fingerprint"] == white["source_fingerprint"]
    assert len(black_moves) == 2
    assert checked_black["complete"] is True
    assert checked_black["prefix"] == list(black_moves)
    assert len(checked_black["frames"]) == len(black_moves)


def test_analysis_exposes_nonzero_promotion_mate_evidence() -> None:
    result = analyze_payload(
        {
            "fen": (
                "7R/pp3p1p/1p3k2/3P4/1b6/5P2/"
                "PPP2P1P/RNK5 b - - 0 1"
            ),
            "series": 8,
            "prefix": [],
            "depth": 2,
            "max_series": 32,
            "time_limit": 5,
            "max_generation_positions": 250_000,
            "alternatives": 1,
        }
    )

    stats = result["stats"]
    assert result["proven_result"] == "black"
    assert stats["promotion_mate_positions"] > 0
    assert stats["promotion_mate_setup_states"] > 0
    assert stats["promotion_mate_candidates"] > 0
    assert stats["promotion_mate_completion_probes"] > 0
    assert stats["promotion_mate_mates"] == 1
    assert stats["promotion_mate_limit_hits"] == 0
    assert stats["promotion_mate_replay_rejects"] == 0


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
        assert (
            health["analysis_limits"]["native_threads"]
            == LOCAL_ANALYSIS_LIMITS.native_threads
        )
        assert health["engine_profile_recommended_depth"] == 2
        assert health["engine_profile_recommended_branch_cap"] == 32
        assert len(health["ui_source_fingerprint"]) == 16
        assert health["mate_proof_cache"]["capacity"] == 4096
        assert health["mate_proof_cache"]["entries"] == 0
        assert health["mate_proof_cache"]["persistent"] is False
        assert health["mate_proof_cache"]["identity"]["engine_source"] == (
            health["source_fingerprint"]
        )

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
        "public_origin": None,
        "mate_proof_cache_path": None,
        "mate_proof_cache_capacity": 4096,
    }


def test_server_rejects_non_loopback_binding(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("board", encoding="utf-8")
    with pytest.raises(ValueError, match="local-only"):
        create_server("0.0.0.0", 0, static_root=static)


def test_public_server_requires_explicit_origin_and_enforces_bounded_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("board", encoding="utf-8")
    with pytest.raises(ValueError, match="https origin"):
        create_server(
            "127.0.0.1",
            0,
            static_root=static,
            public_origin="http://progressive.example",
        )
    with pytest.raises(ValueError, match="cannot expose a SQLite"):
        create_server(
            "127.0.0.1",
            0,
            static_root=static,
            public_origin="https://progressive.example",
            database=tmp_path / "theory.sqlite3",
        )

    server = create_server(
        "127.0.0.1",
        0,
        static_root=static,
        reports_dir=tmp_path,
        public_origin="https://progressive.example",
    )
    assert server.config.public_origin == "https://progressive.example"
    assert server.config.allowed_authority == "progressive.example"
    assert server.config.database_path is None
    assert server.config.analysis_concurrency == 1
    assert server.config.request_limit == 64 * 1024
    assert server.config.analysis_limits.maximum_seconds == PUBLIC_MAX_ANALYSIS_SECONDS
    assert server.config.analysis_limits.maximum_depth == PUBLIC_MAX_ANALYSIS_DEPTH
    assert server.config.analysis_limits.native_threads == 1
    assert server.config.runtime_cpu_count is None
    assert server.config.runtime_cpu_count_source == "conservative-fallback"
    assert server.config.native_threads_policy == "single-thread-pool-avoidance"
    assert (
        server.config.analysis_limits.maximum_generation_positions
        == PUBLIC_MAX_GENERATION_POSITIONS
    )
    server.server_close()


@pytest.mark.parametrize(
    "reported_cpu_count",
    ["0.1", "0.5", "1", "2", "16", "64"],
)
def test_public_server_reports_render_cpu_but_avoids_the_parallel_native_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported_cpu_count: str,
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("board", encoding="utf-8")
    monkeypatch.delenv("RENDER_CPU_COUNT", raising=False)
    monkeypatch.setenv("RENDER_CPU_COUNT", reported_cpu_count)

    server = create_server(
        "127.0.0.1",
        0,
        static_root=static,
        reports_dir=tmp_path,
        public_origin="https://progressive.example",
    )
    try:
        assert server.config.runtime_cpu_count == reported_cpu_count
        assert server.config.runtime_cpu_count_source == "RENDER_CPU_COUNT"
        assert server.config.analysis_limits.native_threads == 1
        assert server.config.native_threads_policy == "single-thread-pool-avoidance"
    finally:
        server.server_close()


@pytest.mark.parametrize("reported_cpu_count", ["", "0", "-1", "nan", "invalid"])
def test_public_server_falls_back_safely_for_invalid_runtime_cpu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported_cpu_count: str,
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("board", encoding="utf-8")
    monkeypatch.setenv("RENDER_CPU_COUNT", reported_cpu_count)

    server = create_server(
        "127.0.0.1",
        0,
        static_root=static,
        reports_dir=tmp_path,
        public_origin="https://progressive.example",
    )
    try:
        assert server.config.runtime_cpu_count is None
        assert server.config.runtime_cpu_count_source == "conservative-fallback"
        assert server.config.analysis_limits.native_threads == 1
        assert server.config.native_threads_policy == "single-thread-pool-avoidance"
    finally:
        server.server_close()


def test_cors_origin_requires_public_mode_and_exact_https_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("board", encoding="utf-8")

    with pytest.raises(ValueError, match="only for a public"):
        create_server(
            "127.0.0.1",
            0,
            static_root=static,
            cors_origin="https://tetizz.github.io",
        )
    with pytest.raises(ValueError, match="https origin"):
        create_server(
            "127.0.0.1",
            0,
            static_root=static,
            public_origin="https://progressive.example",
            cors_origin="http://tetizz.github.io",
        )

    monkeypatch.setenv("SPC_ALLOWED_CORS_ORIGIN", "https://tetizz.github.io")
    server = create_server(
        "127.0.0.1",
        0,
        static_root=static,
        reports_dir=tmp_path,
        public_origin="https://progressive.example",
    )
    assert server.config.allowed_cors_origin == "https://tetizz.github.io"
    server.server_close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("depth", PUBLIC_MAX_ANALYSIS_DEPTH + 1),
        ("time_limit", PUBLIC_MAX_ANALYSIS_SECONDS + 0.01),
        ("max_generation_positions", PUBLIC_MAX_GENERATION_POSITIONS + 1),
    ],
)
def test_public_analysis_rejects_limits_above_the_hosted_envelope(
    field: str, value: int | float
) -> None:
    payload: dict[str, object] = {
        "fen": chess.STARTING_FEN,
        "series": 1,
        "depth": 1,
        "time_limit": 0.1,
        "max_generation_positions": 1_000,
    }
    payload[field] = value

    with pytest.raises(APIError) as denied:
        analyze_payload(payload, request_limits=PUBLIC_ANALYSIS_LIMITS)

    assert denied.value.status == 422


def test_public_server_validates_host_origin_and_reports_public_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("board", encoding="utf-8")
    monkeypatch.setenv("RENDER_CPU_COUNT", "0.5")
    server = create_server(
        "127.0.0.1",
        0,
        static_root=static,
        reports_dir=tmp_path,
        public_origin="https://progressive.example",
        cors_origin="https://tetizz.github.io",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        health_request = Request(
            f"{base}/api/health",
            headers={
                "Host": "progressive.example",
                "Origin": "https://tetizz.github.io",
            },
        )
        with urlopen(health_request, timeout=3) as response:
            health = json.loads(response.read())
            assert (
                response.headers["Access-Control-Allow-Origin"]
                == "https://tetizz.github.io"
            )
        assert health["deployment_mode"] == "public-bounded"
        assert health["database_configured"] is False
        assert health["analysis_limits"]["maximum_seconds"] == PUBLIC_MAX_ANALYSIS_SECONDS
        assert health["analysis_limits"]["maximum_depth"] == PUBLIC_MAX_ANALYSIS_DEPTH
        assert health["analysis_limits"]["maximum_generation_positions"] == (
            PUBLIC_MAX_GENERATION_POSITIONS
        )
        assert health["analysis_limits"]["native_threads"] == 1
        assert health["runtime"] == {
            "cpu_count": "0.5",
            "cpu_count_source": "RENDER_CPU_COUNT",
            "native_threads": 1,
            "native_threads_policy": "single-thread-pool-avoidance",
        }

        body = json.dumps(
            {"fen": chess.STARTING_FEN, "series": 1, "prefix": []}
        ).encode("utf-8")
        accepted = Request(
            f"{base}/api/prefix",
            data=body,
            method="POST",
            headers={
                "Host": "progressive.example",
                "Origin": "https://tetizz.github.io",
                "Content-Type": "application/json",
            },
        )
        with urlopen(accepted, timeout=3) as response:
            assert response.status == 200
            assert (
                response.headers["Access-Control-Allow-Origin"]
                == "https://tetizz.github.io"
            )

        preflight = Request(
            f"{base}/api/prefix",
            method="OPTIONS",
            headers={
                "Host": "progressive.example",
                "Origin": "https://tetizz.github.io",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        with urlopen(preflight, timeout=3) as response:
            assert response.status == 204
            assert response.headers["Access-Control-Allow-Origin"] == (
                "https://tetizz.github.io"
            )
            assert response.headers["Access-Control-Allow-Methods"] == "POST"
            assert response.headers["Access-Control-Allow-Headers"] == "Content-Type"
            assert "Origin" in response.headers["Vary"]

        hostile_preflight = Request(
            f"{base}/api/prefix",
            method="OPTIONS",
            headers={
                "Host": "progressive.example",
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        with pytest.raises(HTTPError) as preflight_denied:
            urlopen(hostile_preflight, timeout=3)
        assert preflight_denied.value.code == 403
        assert preflight_denied.value.headers.get("Access-Control-Allow-Origin") is None

        hostile = Request(
            f"{base}/api/prefix",
            data=body,
            method="POST",
            headers={
                "Host": "progressive.example",
                "Origin": "https://attacker.example",
                "Content-Type": "application/json",
            },
        )
        with pytest.raises(HTTPError) as denied:
            urlopen(hostile, timeout=3)
        assert denied.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
