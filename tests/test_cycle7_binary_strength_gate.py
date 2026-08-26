from __future__ import annotations

from fractions import Fraction
import hashlib
import math
from pathlib import Path
from typing import Any

from benchmarks import cycle7_binary_strength_gate as gate


gate._activate_referee_source()


def _game(
    pair: int,
    swap: int,
    *,
    result: str,
    completion: str = "complete",
    category: str | None = None,
    reason: str = "checkmate",
    failing_runtime: str | None = None,
) -> dict[str, Any]:
    candidate_color = "white" if swap == 0 else "black"
    white_runtime = "candidate" if candidate_color == "white" else "baseline"
    black_runtime = "baseline" if candidate_color == "white" else "candidate"
    return {
        "game_index": pair * 2 + swap,
        "pair_index": pair,
        "swap_index": swap,
        "opening_case_id": f"case-{pair}",
        "opening_position_hash": f"hash-{pair}",
        "opening_state": {"unit": pair},
        "candidate_color": candidate_color,
        "white_runtime": white_runtime,
        "black_runtime": black_runtime,
        "result": result,
        "completion": completion,
        "incomplete_category": category,
        "terminal_reason": reason,
        "failing_runtime": failing_runtime,
    }


def test_loads_and_authoritatively_replays_the_full_neutral_suite() -> None:
    report = (
        Path(__file__).resolve().parents[1]
        / "benchmarks"
        / "results"
        / "selfplay-fresh-seeded-100-v0.9.0.json"
    )
    metadata, cases = gate._load_verified_openings(report)

    assert metadata["count"] == 100
    assert metadata["authoritative_history_replay"] is True
    assert metadata["unique_case_ids"] == 100
    assert metadata["unique_position_hashes"] == 100
    assert len(cases) == 100


def test_exact_one_sided_sign_test_uses_only_decisive_pairs() -> None:
    result = gate.exact_one_sided_sign_test(15, 5)

    expected = Fraction(sum(math.comb(20, k) for k in range(15, 21)), 2**20)
    assert result["decisive_pairs"] == 20
    assert result["exact_fraction"] == f"{expected.numerator}/{expected.denominator}"
    assert result["p_value"] == float(expected)
    assert gate.exact_one_sided_sign_test(0, 0)["p_value"] == 1.0


def test_summary_applies_the_declared_stronger_claim_thresholds() -> None:
    games = [
        _game(
            pair,
            swap,
            result="1-0" if swap == 0 else "0-1",
        )
        for pair in range(20)
        for swap in range(2)
    ]

    summary = gate.summarize_games(games)

    assert summary["completed_games"] == 40
    assert summary["candidate_game_score_rate"] == 1.0
    assert summary["candidate_pair_wdl"] == {"wins": 20, "draws": 0, "losses": 0}
    assert summary["sign_test"]["decisive_pairs"] == 20
    assert summary["sign_test"]["p_value"] == 1 / 2**20
    assert summary["pair_integrity_failures"] == 0
    assert summary["acceptance"]["all_pairs_are_exact_color_swaps"] is True
    assert summary["acceptance"]["non_regression_passed"] is True
    assert summary["acceptance"]["stronger_claim_passed"] is True


def test_summary_keeps_technical_and_integrity_incompletes_distinct() -> None:
    games = [
        _game(0, 0, result="1-0"),
        _game(
            0,
            1,
            result="*",
            completion="incomplete",
            category="integrity",
            reason="integrity-authoritative-replay-failed",
            failing_runtime="candidate",
        ),
        _game(
            1,
            0,
            result="*",
            completion="incomplete",
            category="technical",
            reason="technical-worker-error",
            failing_runtime="baseline",
        ),
        _game(1, 1, result="0-1"),
    ]

    summary = gate.summarize_games(games)
    accounting = summary["technical_incomplete_accounting"]

    assert summary["incomplete_games"] == 2
    assert summary["incomplete_pairs"] == 2
    assert accounting["technical"] == 1
    assert accounting["integrity"] == 1
    assert accounting["by_runtime"] == {
        "candidate": 1,
        "baseline": 1,
        "unattributed": 0,
    }
    assert summary["acceptance"]["non_regression_passed"] is False


def test_compact_report_binds_omitted_game_traces() -> None:
    payload = {
        "format": gate.REPORT_FORMAT,
        "summary": {"completed_games": 2},
        "games": [_game(0, 0, result="1-0"), _game(0, 1, result="0-1")],
    }

    compact = gate.compact_report(payload)

    assert "games" not in compact
    assert compact["game_receipt"] == {
        "count": 2,
        "canonical_sha256": hashlib.sha256(
            gate._canonical_json(payload["games"]).encode("utf-8")
        ).hexdigest(),
        "full_traces_committed": False,
    }


class _ReplayPool:
    def __init__(self, moves: tuple[str, ...], *, tamper: bool = False) -> None:
        self.moves = moves
        self.tamper = tamper

    def analyze(self, state: Any, **_: int) -> dict[str, Any]:
        from scottish_progressive.rules import play_series

        replayed = play_series(state, self.moves)
        final_state = gate._state_payload(replayed.final_state)
        if self.tamper:
            final_state = {**final_state, "quiet_series": final_state["quiet_series"] + 1}
        return {
            "native_source_identity": "unit-native-identity",
            "score": -999_999,
            "requested_depth": 1,
            "completed_depth": 1,
            "exact_width": True,
            "root_scores_complete": True,
            "work_limit_reached": False,
            "timed_out": False,
            "proof": "black",
            "adjudication_status": None,
            "selected": {
                "moves": list(self.moves),
                "machine_notation": "/".join(self.moves),
                "notation": replayed.notation,
                "outcome": replayed.outcome.value if replayed.outcome else None,
                "ended_by_check": replayed.ended_by_check,
                "unused_moves": replayed.unused_moves,
                "final_state": final_state,
            },
            "stats": {"work_positions": 10},
        }


def _published_mate_opening() -> Any:
    from scottish_progressive.league import OpeningCase

    return OpeningCase(
        "unit-published-mate",
        "rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3",
        4,
    )


def _published_mate_job(opening: Any) -> dict[str, Any]:
    return {
        "game_index": 0,
        "pair_index": 0,
        "swap_index": 0,
        "opening_case_id": opening.case_id,
        "opening_position_hash": opening.state().position_hash,
        "opening_state": gate._state_payload(opening.state()),
        "candidate_color": "white",
        "white_runtime": "candidate",
        "black_runtime": "baseline",
    }


def test_parent_referee_accepts_a_matching_authoritative_mate_replay() -> None:
    opening = _published_mate_opening()
    moves = ("c7c6", "d8b6", "f6e4", "b6f2")
    game = gate._play_game(
        _published_mate_job(opening),
        opening,
        {"baseline": _ReplayPool(moves), "candidate": _ReplayPool(moves)},
        depth=1,
        branch_cap=32,
        max_search_work=1_000,
        max_game_work=10_000,
        emergency_max_series=18,
    )

    assert game["completion"] == "complete"
    assert game["result"] == "0-1"
    assert game["terminal_reason"] == "checkmate"
    assert game["series_played"] == 1


def test_parent_referee_rejects_child_final_state_drift() -> None:
    opening = _published_mate_opening()
    moves = ("c7c6", "d8b6", "f6e4", "b6f2")
    game = gate._play_game(
        _published_mate_job(opening),
        opening,
        {"baseline": _ReplayPool(moves, tamper=True), "candidate": _ReplayPool(moves)},
        depth=1,
        branch_cap=32,
        max_search_work=1_000,
        max_game_work=10_000,
        emergency_max_series=18,
    )

    assert game["completion"] == "incomplete"
    assert game["incomplete_category"] == "integrity"
    assert game["terminal_reason"] == "integrity-child-replay-mismatch"
    assert game["failing_runtime"] == "baseline"
