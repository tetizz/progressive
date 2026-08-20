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
    assert limits["max_generation_positions"] is None
    assert limits["required_prefix"] == []
    assert limits["engine_profile_id"] == result.engine_profile_id
    assert saved["ruleset_version"] == "scottish-modern-common-v1"
    assert saved["source_fingerprint"] == result.source_fingerprint
    assert saved["engine_profile_id"] == result.engine_profile_id
    assert saved["required_prefix_json"] == "[]"
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


def test_deeper_one_branch_search_does_not_replace_shallower_exact_snapshot(
    tmp_path,
) -> None:
    path = tmp_path / "theory.sqlite3"
    state = ProgressiveState.initial()
    exact = analyze(state, SearchLimits(depth_series=1))
    deep_one_branch = analyze(
        state, SearchLimits(depth_series=2, max_series_per_node=1)
    )
    assert exact.exact_width is True
    assert deep_one_branch.exact_width is False

    with TheoryDatabase(path) as database:
        exact_id = database.save_analysis(state, exact)
        database.save_analysis(state, deep_one_branch)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    position = connection.execute(
        "SELECT best_analysis_id,search_depth,exact_width,best_series "
        "FROM positions WHERE position_hash=?",
        (state.position_hash,),
    ).fetchone()
    connection.close()

    assert position["best_analysis_id"] == exact_id
    assert position["search_depth"] == 1
    assert position["exact_width"] == 1
    assert position["best_series"] == exact.best_series.machine_notation


def test_fixed_prefix_analysis_is_historical_and_never_replaces_global_snapshot(
    tmp_path,
) -> None:
    path = tmp_path / "theory.sqlite3"
    state = ProgressiveState.initial()
    global_result = analyze(
        state, SearchLimits(depth_series=1, max_series_per_node=4)
    )
    fixed_result = analyze(
        state,
        SearchLimits(depth_series=1, max_series_per_node=64),
        required_prefix=("e2e4",),
    )
    with TheoryDatabase(path) as database:
        global_id = database.save_analysis(state, global_result)
        fixed_id = database.save_analysis(state, fixed_result)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    position = connection.execute(
        "SELECT best_analysis_id,best_series FROM positions WHERE position_hash=?",
        (state.position_hash,),
    ).fetchone()
    current_edges = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    fixed = connection.execute(
        "SELECT required_prefix_json,engine_profile_id FROM analyses WHERE id=?",
        (fixed_id,),
    ).fetchone()
    connection.close()

    assert position["best_analysis_id"] == global_id
    assert position["best_series"] == global_result.best_series.machine_notation
    assert current_edges == 4
    assert json.loads(fixed["required_prefix_json"]) == ["e2e4"]
    assert fixed["engine_profile_id"] == fixed_result.engine_profile_id


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
