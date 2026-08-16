from __future__ import annotations

from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT
from scottish_progressive.theory import (
    deepen_initial_moves,
    rank_initial_moves,
    ranking_markdown,
    write_deepening,
    write_ranking,
)


def test_opening_report_marks_timed_out_run_as_incomplete() -> None:
    ranking = rank_initial_moves(reply_depth=1, time_limit_per_move=0.000001)
    report = ranking_markdown(ranking)

    assert len(ranking.results) == 20
    assert not ranking.all_searches_completed
    assert not ranking.all_reply_searches_exact
    assert ranking.source_fingerprint == ENGINE_SOURCE_FINGERPRINT
    assert "Search: `incomplete/time-limited`" in report
    assert "not valid comparative ranking evidence" in report
    assert "Every requested Black reply search completed" not in report


def test_incomplete_reports_do_not_overwrite_completed_artifact_names(tmp_path) -> None:
    ranking = rank_initial_moves(reply_depth=1, time_limit_per_move=0.000001)
    ranking_json, ranking_markdown_path = write_ranking(ranking, tmp_path)
    assert ranking_json.name == "initial-opening-ranking-incomplete.json"
    assert ranking_markdown_path.name == "initial-opening-ranking-incomplete.md"

    payload = deepen_initial_moves(
        ("e2e4",),
        reply_depth=2,
        max_series_per_node=1,
        time_limit_per_move=0.000001,
    )
    json_path, markdown_path = write_deepening(payload, tmp_path)
    report = markdown_path.read_text(encoding="utf-8")
    assert not payload["all_searches_completed"]
    assert json_path.name == "selective-opening-deepening-incomplete.json"
    assert "incomplete diagnostic" in report
    assert "These runs include a searched White" not in report
