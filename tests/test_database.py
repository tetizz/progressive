from __future__ import annotations

import json
import sqlite3

from scottish_progressive.database import TheoryDatabase
from scottish_progressive.model import ProgressiveState
from scottish_progressive.search import SearchLimits, analyze


def test_analysis_round_trip_to_theory_database(tmp_path) -> None:
    path = tmp_path / "theory.sqlite3"
    state = ProgressiveState.initial()
    result = analyze(state, SearchLimits(depth_series=1, max_series_per_node=4))
    with TheoryDatabase(path) as database:
        analysis_id = database.save_analysis(state, result)
    assert analysis_id == 1

    connection = sqlite3.connect(path)
    position = connection.execute(
        "SELECT * FROM positions WHERE position_hash=?", (state.position_hash,)
    ).fetchone()
    analysis_count = connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
    analysis_cursor = connection.execute("SELECT * FROM analyses WHERE id=1")
    analysis = analysis_cursor.fetchone()
    analysis_columns = [item[0] for item in analysis_cursor.description]
    edge_count = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    analysis_edge_count = connection.execute(
        "SELECT COUNT(*) FROM analysis_edges"
    ).fetchone()[0]
    connection.close()

    assert position is not None
    assert analysis_count == 1
    assert analysis is not None
    saved = dict(zip(analysis_columns, analysis, strict=True))
    limits = json.loads(saved["search_limits_json"])
    assert limits["depth_series"] == 1
    assert limits["max_series_per_node"] == 4
    assert saved["ruleset_version"] == "scottish-modern-common-v1"
    assert saved["source_fingerprint"] == result.source_fingerprint
    assert edge_count == 4
    assert analysis_edge_count == 4


def test_weaker_analysis_does_not_replace_best_snapshot_or_leave_stale_edges(
    tmp_path,
) -> None:
    path = tmp_path / "theory.sqlite3"
    state = ProgressiveState.initial()
    exhaustive = analyze(state, SearchLimits(depth_series=1))
    selective = analyze(
        state, SearchLimits(depth_series=1, max_series_per_node=1)
    )
    with TheoryDatabase(path) as database:
        first_id = database.save_analysis(state, exhaustive)
        second_id = database.save_analysis(state, selective)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    position = connection.execute(
        "SELECT * FROM positions WHERE position_hash=?", (state.position_hash,)
    ).fetchone()
    current_edges = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    historical_edges = connection.execute(
        "SELECT COUNT(*) FROM analysis_edges"
    ).fetchone()[0]
    connection.close()

    assert first_id == 1 and second_id == 2
    assert position["best_analysis_id"] == first_id
    assert position["exact_width"] == 1
    assert current_edges == 20
    assert historical_edges == 21


def test_proven_quiet_draw_is_persisted_as_terminal_adjudication(tmp_path) -> None:
    path = tmp_path / "theory.sqlite3"
    state = ProgressiveState.from_fen(
        "7k/8/8/8/8/8/8/K7 w - - 0 1", 1, quiet_series=10
    )
    result = analyze(state, SearchLimits(depth_series=1))
    with TheoryDatabase(path) as database:
        database.save_analysis(state, result)

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT terminal_outcome, adjudication_status, proof_kind FROM positions "
        "WHERE position_hash=?",
        (state.position_hash,),
    ).fetchone()
    analysis_status = connection.execute(
        "SELECT adjudication_status, proof_kind FROM analyses WHERE id=1"
    ).fetchone()
    connection.close()

    assert row == (
        "ten-series-draw",
        "proven-draw-no-mating-material",
        "draw",
    )
    assert analysis_status == ("proven-draw-no-mating-material", "draw")
