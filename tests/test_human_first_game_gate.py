from __future__ import annotations

from types import SimpleNamespace

from scottish_progressive.league import (
    HUMAN_FIRST_GAME_CONTENDER_HYPOTHESES,
    HUMAN_FIRST_GAME_REFUTATION,
    HUMAN_FIRST_GAME_REPLY_VERIFIER_WIDTH,
    HUMAN_FIRST_GAME_ROOT_WIDTH,
    _evaluate_human_first_game_refutation,
    _replay_tactical_refutation_anchor,
)
from scottish_progressive.model import Outcome
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import generate_series, play_series
from scottish_progressive.search import ScoredSeries


def _fake_search_result(
    *,
    best_series,
    alternatives=(),
    score: int = 0,
    depth: int = 1,
    root_scores_complete: bool = True,
    proof: str | None = None,
):
    return SimpleNamespace(
        best_series=best_series,
        alternatives=tuple(alternatives),
        score=score,
        requested_depth=depth,
        completed_depth=depth,
        timed_out=False,
        work_limit_reached=False,
        root_scores_complete=root_scores_complete,
        stats=SimpleNamespace(work_positions=1234),
        proof=proof,
        exact_width=False,
    )


def test_first_game_anchor_replays_exact_s4_blunder_and_s5_mate() -> None:
    root, blunder, mate, history = _replay_tactical_refutation_anchor()

    assert history == tuple(
        "/".join(series) for series in HUMAN_FIRST_GAME_REFUTATION.history
    )
    assert blunder.moves == HUMAN_FIRST_GAME_REFUTATION.blundering_series
    assert blunder.final_state.series_number == 5
    assert mate.moves == HUMAN_FIRST_GAME_REFUTATION.immediate_reply_mate
    assert mate.outcome == Outcome.CHECKMATE
    assert mate.ended_by_check
    assert root.series_number == 4


def test_first_game_gate_selects_best_retained_safe_candidate_without_win_claim() -> None:
    gate = _evaluate_human_first_game_refutation(baseline_profile())
    evidence = gate["evidence"]

    assert gate["passed"] is True
    assert evidence["blunder_reply_verifier"]["mate_found"] is True
    assert evidence["blunder_reply_verifier"]["verifier_width"] == 832
    assert evidence["selected_series"] != evidence["blundering_series"]
    assert evidence["selected_is_best_retained_by_score"] is True
    assert evidence["selected_is_best_retained_safe"] is True
    assert evidence["reply_safety_passed"] is True
    assert evidence["retained_move_quality_passed"] is True
    hypotheses = evidence["contender_hypotheses"]
    assert [item["hypothesis_id"] for item in hypotheses] == ["A", "B", "E"]
    assert [item["series"] for item in hypotheses] == [
        "/".join(item.series) for item in HUMAN_FIRST_GAME_CONTENDER_HYPOTHESES
    ]
    assert all(item["pass_required"] is False for item in hypotheses)
    assert all("not a win proof" in item["evidence_label"] for item in hypotheses)
    assert evidence["retained_candidates"] == HUMAN_FIRST_GAME_ROOT_WIDTH
    assert evidence["selector_ordinary_static_quota_slots"] == 16
    assert evidence["selector_tactical_reserve_slots"] == 16
    assert (
        evidence["retained_tactical_provenance_candidates"]
        + evidence["retained_non_tactical_provenance_candidates"]
        == evidence["retained_candidates"]
    )
    selected = evidence["screened_selected_and_strictly_better"][-1]
    assert selected["is_selected"] is True
    assert selected["reply_verifier"]["completed"] is True
    assert selected["reply_verifier"]["mate_found"] is False
    assert selected["reply_verifier"]["verifier_width"] == (
        HUMAN_FIRST_GAME_REPLY_VERIFIER_WIDTH
    )
    assert evidence["proof"] is None
    assert "not a forced-win proof" in evidence["quality_scope"]


def test_first_game_gate_rejects_a_safe_choice_when_a_better_retained_one_exists(
    monkeypatch,
) -> None:
    root, blunder, canonical_mate, _history = _replay_tactical_refutation_anchor()
    better = play_series(root, ("g8h6", "h6f5", "f5h4", "h4f3"))
    inferior = play_series(root, ("e7e5", "f6f5", "f5e4", "f8b4"))
    root_result = _fake_search_result(
        best_series=inferior,
        alternatives=(
            ScoredSeries(better, 1_043),
            ScoredSeries(inferior, 1_812),
        ),
        score=1_812,
        depth=2,
    )

    def fake_analyze(state, limits, profile, evaluation_overlay):
        del profile, evaluation_overlay
        if state.series_number == 4:
            return root_result
        if state.transposition_key == blunder.final_state.transposition_key:
            return _fake_search_result(
                best_series=canonical_mate,
                score=999_999,
                depth=limits.depth_series,
                proof="white",
            )
        reply = generate_series(state, max_frontier_states=1)[0]
        assert reply.outcome != Outcome.CHECKMATE
        return _fake_search_result(
            best_series=reply,
            depth=limits.depth_series,
        )

    monkeypatch.setattr(
        "scottish_progressive.league._analyze_gate_position",
        fake_analyze,
    )
    gate = _evaluate_human_first_game_refutation(baseline_profile())
    evidence = gate["evidence"]

    assert gate["passed"] is False
    assert evidence["reply_safety_passed"] is True
    assert evidence["selected_is_best_retained_by_score"] is False
    assert evidence["selected_is_best_retained_safe"] is False
    assert evidence["retained_move_quality_passed"] is False
    assert len(evidence["screened_selected_and_strictly_better"]) == 2
    assert all(
        not item["reply_verifier"]["mate_found"]
        for item in evidence["screened_selected_and_strictly_better"]
    )


def test_first_game_gate_rejects_selected_line_with_replay_proven_reply_mate(
    monkeypatch,
) -> None:
    root, blunder, canonical_mate, _history = _replay_tactical_refutation_anchor()
    root_result = _fake_search_result(
        best_series=blunder,
        alternatives=(ScoredSeries(blunder, -2_000),),
        score=-2_000,
        depth=2,
    )

    def fake_analyze(state, limits, profile, evaluation_overlay):
        del profile, evaluation_overlay
        if state.series_number == 4:
            return root_result
        assert state.transposition_key == blunder.final_state.transposition_key
        return _fake_search_result(
            best_series=canonical_mate,
            score=999_999,
            depth=limits.depth_series,
            proof="white",
        )

    monkeypatch.setattr(
        "scottish_progressive.league._analyze_gate_position",
        fake_analyze,
    )
    gate = _evaluate_human_first_game_refutation(baseline_profile())
    evidence = gate["evidence"]

    assert gate["passed"] is False
    assert evidence["avoided_blunder"] is False
    assert evidence["reply_safety_passed"] is False
    selected = evidence["screened_selected_and_strictly_better"][-1]
    assert selected["reply_verifier"]["mate_found"] is True
    assert selected["reply_verifier"]["replayed_outcome"] == "checkmate"
