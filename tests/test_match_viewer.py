from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "scottish_progressive" / "web" / "static"
RECEIPT = ROOT / "reports" / "bucephalus-rematch-100games-30s-20260827.json"
EXPECTED_RECEIPT_SHA256 = (
    "6aa5f81d521bc60f8bb368179a4b30b89abd79325f54de432e6617dafdbca646"
)
NODE = shutil.which("node")


def _run_generator(
    output: Path,
    *,
    receipt: Path = RECEIPT,
) -> tuple[dict[str, object], bytes, dict[str, object]]:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_match_viewer_data.py"),
            "--receipt",
            str(receipt),
            "--output",
            str(output),
            "--expected-receipt-sha256",
            EXPECTED_RECEIPT_SHA256,
        ],
        cwd=ROOT,
        check=True,
    )
    manifest = json.loads(
        (output / "match-viewer-manifest.json").read_text(encoding="utf-8")
    )
    data_path = output / str(manifest["data_file"])
    data_bytes = data_path.read_bytes()
    bundle = json.loads(data_bytes)
    return manifest, data_bytes, bundle


def test_match_viewer_provenance_and_bundle_are_line_ending_portable(
    tmp_path: Path,
) -> None:
    source = RECEIPT.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    lf_receipt = tmp_path / "lf-source" / RECEIPT.name
    crlf_receipt = tmp_path / "crlf-source" / RECEIPT.name
    lf_receipt.parent.mkdir()
    crlf_receipt.parent.mkdir()
    lf_receipt.write_bytes(source)
    crlf_receipt.write_bytes(source.replace(b"\n", b"\r\n"))

    lf_manifest, lf_bytes, lf_bundle = _run_generator(
        tmp_path / "lf",
        receipt=lf_receipt,
    )
    crlf_manifest, crlf_bytes, crlf_bundle = _run_generator(
        tmp_path / "crlf",
        receipt=crlf_receipt,
    )

    assert hashlib.sha256(source).hexdigest() == EXPECTED_RECEIPT_SHA256
    assert lf_manifest == crlf_manifest
    assert lf_bytes == crlf_bytes
    assert lf_bundle == crlf_bundle
    assert b"\r" not in lf_bytes


def test_match_viewer_bundle_is_deterministic_content_addressed_and_complete(
    tmp_path: Path,
) -> None:
    first_manifest, first_bytes, first = _run_generator(tmp_path / "first")
    second_manifest, second_bytes, second = _run_generator(tmp_path / "second")

    assert first_manifest == second_manifest
    assert first_bytes == second_bytes
    assert first == second
    assert first_manifest["schema"] == "spc-match-viewer-manifest-v1"
    assert first_manifest["receipt_sha256"] == EXPECTED_RECEIPT_SHA256
    assert first_manifest["data_sha256"] == hashlib.sha256(first_bytes).hexdigest()
    assert first_manifest["data_file"] == (
        f"match.{first_manifest['data_sha256']}.json"
    )

    assert first["schema"] == "spc-bucephalus-match-viewer-v1"
    assert first["source"]["report_id"] == "external-report-e65c19ddd7e440b482ec"
    assert first["source"]["receipt_sha256"] == EXPECTED_RECEIPT_SHA256
    assert first["summary"]["scheduled_games"] == 100
    assert first["summary"]["completed_games"] == 94
    assert first["summary"]["incomplete_games"] == 6
    assert first["summary"]["status_counts"] == {
        "completed": 94,
        "integrity": 0,
        "technical": 0,
        "timeout": 6,
    }
    assert len(first["games"]) == 100
    assert len(first["pairs"]) == 50
    assert sum(len(game["frames"]) for game in first["games"]) == 1_852
    assert sum(len(game["frames"]) - 1 for game in first["games"]) == 1_752
    assert sum(game["benchmark_start_frame"] for game in first["games"]) == 864
    assert sum(
        len(game["frames"]) - 1 - game["benchmark_start_frame"]
        for game in first["games"]
    ) == 888

    timeout = first["games"][0]
    assert timeout["game_number"] == 1
    assert timeout["pair_number"] == 1
    assert timeout["status"] == "timeout"
    assert timeout["completed"] is False
    assert timeout["terminal_reason"] == "technical-external-timeout"
    assert timeout["technical_failure_owner"] == "bucephalus"
    assert timeout["white"]["key"] == "local"
    assert timeout["black"]["key"] == "bucephalus"
    assert timeout["frames"][timeout["benchmark_start_frame"]]["fen"] == (
        "r1bq1bnr/pppppk1p/5p2/6p1/2P5/P4P2/1PnPP1PP/"
        "RNBQKBNR w KQ - 3 7"
    )
    assert timeout["frames"][timeout["benchmark_start_frame"] + 1]["uci"] == "d1c2"
    assert timeout["frames"][-1]["fen"] == timeout["final_pfen"].split(" | ", 1)[0]
    assert [frame["frame"] for frame in timeout["frames"]] == list(
        range(len(timeout["frames"]))
    )

    completed = first["games"][1]
    assert completed["status"] == "completed"
    assert completed["completed"] is True
    assert completed["result"] == "0-1"
    assert completed["winner"] == "local"
    assert completed["winner_color"] == "black"
    assert completed["frames"][-1]["san"] == "gxf1=Q#"
    assert completed["frames"][-1]["outcome"] == "checkmate"

    for game in first["games"]:
        assert game["replay_verified"] is True
        assert game["frames"][0]["phase"] == "opening"
        assert sum(frame["is_benchmark_start"] for frame in game["frames"]) == 1
        assert game["frames"][game["benchmark_start_frame"]][
            "is_benchmark_start"
        ] is True
        assert sum(frame["uci"] is not None for frame in game["frames"]) == (
            len(game["frames"]) - 1
        )
        assert game["frames"][-1]["fen"] == game["final_pfen"].split(" | ", 1)[0]


def test_match_viewer_static_surface_reuses_the_live_board_and_is_read_only() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    viewer = (STATIC / "matches.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    viewer_js = (STATIC / "match-viewer.js").read_text(encoding="utf-8")
    board = (STATIC / "board-renderer.js").read_text(encoding="utf-8")
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert 'href="./matches.html"' in index
    assert index.index('src="./board-renderer.js"') < index.index('src="./app.js"')
    assert viewer.index('src="./board-renderer.js"') < viewer.index(
        'src="./match-viewer.js"'
    )
    assert 'href="./styles.css"' in viewer
    assert 'class="board-shell" id="board-shell"' in viewer
    assert 'class="board" id="board"' in viewer
    assert 'class="play-player-strip' in viewer
    assert 'id="match-game"' in viewer
    assert 'id="replay-previous"' in viewer
    assert 'id="replay-play"' in viewer
    assert 'id="replay-next"' in viewer
    assert 'id="replay-flip"' in viewer
    assert 'id="match-provenance"' in viewer
    assert 'id="match-status"' in viewer
    assert 'aria-live="polite"' in viewer

    assert "globalThis.ScottishProgressiveBoard" in board
    assert "BOARD_RENDERER.render" in app
    assert "BOARD_RENDERER.render" in viewer_js
    assert 'new URLSearchParams(window.location.search).get("workspace")' in app
    assert 'await switchWorkspaceMode("analyze")' in app
    assert 'fetch("./matches/match-viewer-manifest.json"' in viewer_js
    assert r"/^match\.[0-9a-f]{64}\.json$/" in viewer_js
    assert "crypto.subtle.digest" in viewer_js
    assert 'event.key === "ArrowLeft"' in viewer_js
    assert 'event.key === "ArrowRight"' in viewer_js
    assert 'event.key === " "' in viewer_js
    assert "status-timeout" in styles
    assert "status-technical" in styles
    assert "status-integrity" in styles
    assert "status-completed" in styles
    assert ".board-layout.match-board-layout {" in styles
    assert "grid-template-columns: minmax(0, 1fr);" in styles
    assert (
        ".match-replay-toolbar .tool-button span:first-child { display: inline; }"
        in styles
    )
    assert "!game.completed && frame.frame === game.frames.length - 1" in viewer_js

    forbidden = (
        "/api/analyze",
        "/api/prefix",
        "browser-engine-client",
        "WebAssembly",
        'method: "POST"',
    )
    assert all(token not in viewer_js for token in forbidden)
    assert "benchmark execution" not in viewer_js.lower()


@pytest.mark.skipif(NODE is None, reason="Node.js is required for board tests")
def test_shared_board_renderer_has_exact_orientation_pieces_and_highlights() -> None:
    script = r"""
require(process.argv[1]);
const board = globalThis.ScottishProgressiveBoard;
const fen = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1";
const white = board.model({
  fen,
  flipped: false,
  lastMove: "a1a8",
  focusSquare: "e1",
  interactive: false,
});
const black = board.model({ fen, flipped: true });
process.stdout.write(JSON.stringify({
  whiteFirst: white[0],
  whiteLast: white.at(-1),
  blackFirst: black[0],
  blackLast: black.at(-1),
  count: white.length,
  lastSquares: white.filter((square) => square.last).map((square) => square.name),
  king: white.find((square) => square.name === "e1"),
  asset: board.pieceAsset({ color: "black", type: "q" }),
}));
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "board-renderer.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["count"] == 64
    assert payload["whiteFirst"]["name"] == "a8"
    assert payload["whiteLast"]["name"] == "h1"
    assert payload["blackFirst"]["name"] == "h1"
    assert payload["blackLast"]["name"] == "a8"
    assert payload["lastSquares"] == ["a8", "a1"]
    assert payload["king"]["piece"] == {"color": "white", "type": "k"}
    assert payload["king"]["tabIndex"] == -1
    assert payload["king"]["ariaLabel"] == "e1, white king"
    assert payload["asset"] == "./pieces/cburnett/bQ.svg"


def test_pages_build_versions_both_live_and_match_viewer_assets(tmp_path: Path) -> None:
    output = tmp_path / "_site"
    version = "viewer-test-94cd26d"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_pages_site.py"),
            "--source",
            str(STATIC),
            "--output",
            str(output),
            "--version",
            version,
        ],
        check=True,
    )

    deployed_index = (output / "index.html").read_text(encoding="utf-8")
    deployed_viewer = (output / "matches.html").read_text(encoding="utf-8")
    assert f'src="./board-renderer.js?v={version}"' in deployed_index
    assert f'href="./matches.html?v={version}"' in deployed_index
    assert f'href="./styles.css?v={version}"' in deployed_viewer
    assert f'src="./board-renderer.js?v={version}"' in deployed_viewer
    assert f'src="./match-viewer.js?v={version}"' in deployed_viewer
    manifest = json.loads(
        (output / "matches" / "match-viewer-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert (output / "matches" / manifest["data_file"]).is_file()
