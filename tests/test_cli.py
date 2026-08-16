from __future__ import annotations

import json

import chess

from scottish_progressive.cli import main


def test_rules_command_prints_named_profile(capsys) -> None:
    assert main(["rules"]) == 0
    output = capsys.readouterr().out
    assert "scottish-modern-common-v1" in output
    assert "proof-required" in output


def test_series_command_returns_machine_readable_series(capsys) -> None:
    assert (
        main(
            [
                "series",
                "--fen",
                chess.STARTING_FEN,
                "--series",
                "1",
                "--limit",
                "1",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["raw_series"] == 20
    assert payload["unique_series"] == 20
    assert len(payload["series"]) == 1


def test_init_database_command_creates_schema(tmp_path, capsys) -> None:
    path = tmp_path / "cli.sqlite3"
    assert main(["init-db", str(path)]) == 0
    assert path.exists()
    assert str(path.resolve()) in capsys.readouterr().out


def test_analyze_json_exposes_proven_result(capsys) -> None:
    assert (
        main(
            [
                "analyze",
                "--fen",
                "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
                "--series",
                "1",
                "--depth",
                "1",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["classification"] == "Forced Win"
    assert payload["proven_result"] == "white"
