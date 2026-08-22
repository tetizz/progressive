from __future__ import annotations

from dataclasses import asdict
import statistics
import time

import chess
import pytest

from scottish_progressive.model import Outcome, ProgressiveState
from scottish_progressive.promotion_mate import (
    COMPLETION_CANDIDATE_BATCH,
    PromotionMateProbe,
    find_promotion_series_mate,
    promotion_mate_eligible,
)
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import NativeFrontierScoreConfig, play_series
from scottish_progressive.search import SearchLimits, SeriesSearcher, analyze


HARD_PROMOTION_MATES = (
    (
        "bnq1nr2/p1pp1pk1/8/4PP2/1P2P1p1/8/P1P2KP1/BNbBN2r w - - 0 1",
        7,
        ("e1f3", "f3d4"),
        ("e1f3", "f3d4", "e5e6", "e6e7", "e7f8r", "f8h8", "d4e6"),
    ),
    (
        "7R/pp3p1p/1p3k2/3P4/1b6/5P2/PPP2P1P/RNK5 b - - 0 1",
        8,
        ("b4d6", "b6b5"),
        (
            "b4d6",
            "b6b5",
            "b5b4",
            "b4b3",
            "b3a2",
            "a2b1n",
            "b1c3",
            "d6f4",
        ),
    ),
)


PROMOTION_MATE_RECALL_CASES = tuple(
    (
        fen,
        series_number + extra_moves,
        expected,
        extra_moves,
    )
    for fen, series_number, _required_prefix, expected in HARD_PROMOTION_MATES
    for extra_moves in (0, 2)
)


@pytest.mark.parametrize(
    ("fen", "series_number", "required_prefix", "expected"),
    HARD_PROMOTION_MATES,
)
def test_promotion_mate_lane_is_deterministic_and_replay_proven(
    fen: str,
    series_number: int,
    required_prefix: tuple[str, ...],
    expected: tuple[str, ...],
) -> None:
    state = ProgressiveState.from_fen(fen, series_number)
    probes = tuple(
        find_promotion_series_mate(
            state,
            required_prefix=required_prefix,
            max_positions=50_000,
        )
        for _ in range(3)
    )

    signatures = []
    for probe in probes:
        assert probe.eligible
        assert not probe.work_limit_reached
        assert not probe.cancelled
        assert probe.series is not None
        assert probe.series.moves == expected
        assert 0 < probe.positions_visited <= 50_000
        assert probe.promotion_candidates > 0
        assert probe.completion_probes > 0
        replayed = play_series(state, probe.series.moves)
        assert replayed.outcome == Outcome.CHECKMATE
        assert replayed.ended_by_check
        signatures.append(asdict(probe))
    assert signatures[0] == signatures[1] == signatures[2]

    searches = tuple(
        analyze(
            state,
            SearchLimits(
                depth_series=2,
                max_series_per_node=32,
                max_generation_positions=250_000,
                collect_all_root_scores=False,
            ),
            required_prefix=required_prefix,
        )
        for _ in range(3)
    )
    search_signatures = []
    for result in searches:
        assert result.best_series is not None
        assert result.best_series.moves == expected
        assert result.completed_depth == 2
        assert not result.exact_width
        assert not result.timed_out
        assert not result.work_limit_reached
        assert result.proof == ("white" if state.board.turn else "black")
        assert result.forced == result.proof
        assert result.stats.work_positions == (
            result.stats.series_generation_positions
            + result.stats.frontier_score_positions
            + result.stats.promotion_mate_positions
            + result.stats.static_evaluation_positions
            + result.stats.evaluation_reach_positions
            + result.stats.quiet_adjudication_positions
        )
        search_signatures.append(
            (
                result.score,
                result.best_series.machine_notation,
                tuple(item.machine_notation for item in result.principal_variation),
                tuple(
                    (
                        item.series.machine_notation,
                        item.score,
                        item.proof_bounds,
                    )
                    for item in result.alternatives
                ),
                result.completed_depth,
                result.exact_width,
                result.timed_out,
                result.work_limit_reached,
                result.root_scores_complete,
                result.proof,
                asdict(result.stats),
            )
        )
    assert search_signatures[0] == search_signatures[1] == search_signatures[2]


def test_root_promotion_mate_memo_separates_clocks_and_prefixes() -> None:
    fen, series_number, required_prefix, expected = HARD_PROMOTION_MATES[0]
    first = ProgressiveState.from_fen(fen, series_number)
    second = ProgressiveState.from_fen(
        fen.rsplit(" ", 2)[0] + " 9 7",
        series_number,
    )
    assert first.transposition_key == second.transposition_key

    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
        ),
        baseline_profile(),
    )
    first_mate = searcher._root_promotion_mate(
        first,
        required_prefix=required_prefix,
        reserve_positions=0,
    )
    second_mate = searcher._root_promotion_mate(
        second,
        required_prefix=required_prefix,
        reserve_positions=0,
    )

    assert first_mate is not None
    assert second_mate is not None
    assert first_mate.moves == second_mate.moves == expected
    assert first_mate.final_state.pfen != second_mate.final_state.pfen
    assert second_mate.final_state.pfen == play_series(
        second,
        second_mate.moves,
    ).final_state.pfen
    assert len(searcher._root_promotion_mate_cache) == 2

    different_prefix = required_prefix[:1]
    searcher._root_promotion_mate(
        first,
        required_prefix=different_prefix,
        reserve_positions=0,
    )
    assert len(searcher._root_promotion_mate_cache) == 3


@pytest.mark.parametrize(
    ("fen", "series_number", "expected", "expected_unused"),
    PROMOTION_MATE_RECALL_CASES,
)
def test_unconstrained_promotion_mate_recall_is_deterministic_with_or_without_scorer(
    fen: str,
    series_number: int,
    expected: tuple[str, ...],
    expected_unused: int,
) -> None:
    state = ProgressiveState.from_fen(fen, series_number)
    scorer = NativeFrontierScoreConfig.from_profile(state, baseline_profile())

    for promotion_score in (None, scorer):
        probes = tuple(
            find_promotion_series_mate(
                state,
                max_positions=50_000,
                promotion_score=promotion_score,
            )
            for _ in range(3)
        )
        assert asdict(probes[0]) == asdict(probes[1]) == asdict(probes[2])
        for probe in probes:
            assert probe.eligible
            assert not probe.cancelled
            assert not probe.work_limit_reached
            assert probe.series is not None
            assert probe.series.moves == expected
            assert probe.series.unused_moves == expected_unused
            assert play_series(state, probe.series.moves).outcome == Outcome.CHECKMATE

    if expected[0] == "e1f3":
        # The published route is deliberately beyond the first fixed-size
        # batch in the intrinsic ordering. This guards fair continuation.
        assert probes[0].promotion_candidates > COMPLETION_CANDIDATE_BATCH * 2


@pytest.mark.parametrize(
    ("fen", "series_number"),
    tuple(
        (case[0], case[1] + 2)
        for case in HARD_PROMOTION_MATES
    ),
)
def test_production_search_preserves_same_series_promotion_mate(
    fen: str,
    series_number: int,
) -> None:
    state = ProgressiveState.from_fen(fen, series_number)
    result = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
        ),
    )

    assert result.best_series is not None
    assert result.best_series.outcome == Outcome.CHECKMATE
    assert result.best_series.ended_by_check
    assert play_series(state, result.best_series.moves).outcome == Outcome.CHECKMATE
    assert result.proof == ("white" if state.board.turn else "black")
    assert result.forced == result.proof
    assert not result.work_limit_reached


@pytest.mark.parametrize(
    ("fen", "series_number"),
    [(case[0], case[1]) for case in HARD_PROMOTION_MATES],
)
def test_promotion_mate_lane_exhaustion_proves_nothing(
    fen: str,
    series_number: int,
) -> None:
    probe = find_promotion_series_mate(
        ProgressiveState.from_fen(fen, series_number),
        max_positions=1,
    )
    assert probe.eligible
    assert probe.series is None
    assert probe.positions_visited == 1
    assert probe.work_limit_reached
    assert not probe.cancelled


def test_promotion_mate_lane_cancellation_proves_nothing() -> None:
    fen, series_number, _, _ = HARD_PROMOTION_MATES[0]
    probe = find_promotion_series_mate(
        ProgressiveState.from_fen(fen, series_number),
        should_stop=lambda: True,
    )
    assert probe.eligible
    assert probe.series is None
    assert probe.positions_visited == 0
    assert probe.cancelled
    assert not probe.work_limit_reached


@pytest.mark.parametrize(
    "fen",
    (
        "8/5P2/8/2k5/7B/3K4/8/8 w - - 0 1",
        "8/1K1q4/1n1k4/P2n4/8/4P3/3p1Q1N/2b3n1 w - - 0 1",
    ),
)
def test_eligible_negative_is_bounded_and_deterministic(fen: str) -> None:
    state = ProgressiveState.from_fen(fen, 7)
    scorer = NativeFrontierScoreConfig.from_profile(state, baseline_profile())
    probes: list[PromotionMateProbe] = []
    elapsed: list[float] = []
    for _ in range(3):
        started = time.perf_counter()
        probes.append(
            find_promotion_series_mate(
                state,
                promotion_score=scorer,
            )
        )
        elapsed.append(time.perf_counter() - started)

    assert promotion_mate_eligible(state)
    assert asdict(probes[0]) == asdict(probes[1]) == asdict(probes[2])
    assert all(probe.series is None for probe in probes)
    assert all(probe.eligible for probe in probes)
    assert all(not probe.work_limit_reached for probe in probes)
    assert all(not probe.cancelled for probe in probes)
    assert all(probe.positions_visited <= 2_000 for probe in probes)
    assert max(elapsed) < 1.0


def test_promotion_mate_deadline_is_checked_inside_completion_work() -> None:
    state = ProgressiveState.from_fen(
        HARD_PROMOTION_MATES[0][0],
        7,
    )
    deadline = time.perf_counter() + 0.05
    started = time.perf_counter()
    probe = find_promotion_series_mate(
        state,
        should_stop=lambda: time.perf_counter() >= deadline,
    )
    elapsed = time.perf_counter() - started

    assert probe.eligible
    assert probe.series is None
    assert probe.cancelled
    assert not probe.work_limit_reached
    assert elapsed < 0.5


def test_one_second_deadline_is_checked_inside_scoring_work() -> None:
    state = ProgressiveState.from_fen(HARD_PROMOTION_MATES[0][0], 9)
    deadline = time.perf_counter() + 1.0

    def slow_score(_board: object) -> int:
        time.sleep(0.002)
        return 0

    started = time.perf_counter()
    probe = find_promotion_series_mate(
        state,
        promotion_score=slow_score,
        should_stop=lambda: time.perf_counter() >= deadline,
    )
    elapsed = time.perf_counter() - started

    assert probe.eligible
    assert probe.series is None
    assert probe.cancelled
    assert not probe.work_limit_reached
    assert 0.9 <= elapsed <= 1.1


def test_standard_mate_prefilter_still_requires_progressive_ep_replay() -> None:
    state = ProgressiveState.from_fen(
        "8/4K3/5P2/2N1k3/1p6/8/2P5/2BR1R2 w - - 0 1",
        7,
    )
    moves = ("c2c4", "c1b2")

    standard = state.board.copy(stack=False)
    standard.push_uci(moves[0])
    standard.turn = state.board.turn
    standard.ep_square = None
    standard.push_uci(moves[1])
    assert standard.is_checkmate()

    progressive = play_series(state, moves)
    assert progressive.ended_by_check
    assert progressive.outcome is None
    assert progressive.final_state.ep_targets == (chess.C3,)

    probe = find_promotion_series_mate(state, required_prefix=moves)
    assert probe.eligible
    assert probe.series is None
    assert probe.replay_rejects == 1


@pytest.mark.parametrize(
    ("fen", "series_number", "bad_prefix"),
    (
        (
            HARD_PROMOTION_MATES[0][0],
            7,
            ("e1f3", "f3d4", "e5e6", "e6e7", "e7f8q"),
        ),
        (
            HARD_PROMOTION_MATES[0][0],
            7,
            ("e1f3", "f3d4", "e5e6", "e6e7", "e7f8b"),
        ),
        (
            HARD_PROMOTION_MATES[1][0],
            8,
            ("b4d6", "b6b5", "b5b4", "b4b3", "b3a2", "a2b1q"),
        ),
        (
            HARD_PROMOTION_MATES[1][0],
            8,
            ("b4d6", "b6b5", "b5b4", "b4b3", "b3a2", "a2b1r"),
        ),
    ),
)
def test_nonmating_promotion_check_with_proven_reply_mate_is_rejected(
    fen: str,
    series_number: int,
    bad_prefix: tuple[str, ...],
) -> None:
    state = ProgressiveState.from_fen(fen, series_number)
    probe = find_promotion_series_mate(
        state,
        required_prefix=bad_prefix,
        max_positions=len(bad_prefix),
    )
    terminal = play_series(state, bad_prefix)

    assert probe.eligible
    assert probe.series is None
    assert not probe.work_limit_reached
    assert probe.completion_probes == 0
    assert terminal.ended_by_check
    assert terminal.outcome is None
    assert terminal.unused_moves == 2

    searched = analyze(
        state,
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
        ),
        required_prefix=bad_prefix,
    )
    assert searched.best_series is None
    assert searched.proof is None
    assert searched.forced is None
    assert searched.stats.root_safety_proven_mate_children >= 1
    assert searched.stats.promotion_mate_mates == 0


def test_ordinary_opening_positions_pay_no_lane_work() -> None:
    states = (
        ProgressiveState.initial(),
        ProgressiveState.from_fen(
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            3,
        ),
    )
    for state in states:
        assert not promotion_mate_eligible(state)
        assert find_promotion_series_mate(state) == PromotionMateProbe()

    result = analyze(
        states[1],
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
        ),
    )
    assert result.stats.promotion_mate_positions == 0
    assert result.stats.promotion_mate_candidates == 0
    assert result.stats.promotion_mate_mates == 0
    assert result.stats.promotion_mate_limit_hits == 0


def test_retained_ordinary_mate_skips_the_lane_entirely() -> None:
    state = ProgressiveState.from_fen(
        "rn1qkb1r/ppp1pppp/5n2/3P4/8/5N2/PPPP1PPP/RNBbK2R w KQkq - 0 7",
        5,
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=2,
            max_series_per_node=32,
            max_generation_positions=250_000,
            collect_all_root_scores=False,
        ),
    )

    assert result.best_series is not None
    assert result.best_series.outcome == Outcome.CHECKMATE
    assert result.stats.promotion_mate_positions == 0
    assert result.stats.promotion_mate_setup_states == 0
    assert result.stats.promotion_mate_candidates == 0
    assert result.stats.promotion_mate_completion_probes == 0
    assert result.stats.promotion_mate_mates == 0
    assert result.stats.promotion_mate_limit_hits == 0
    assert result.stats.promotion_mate_replay_rejects == 0


def test_retained_ordinary_mate_lane_overhead_is_below_ten_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProgressiveState.from_fen(
        "rn1qkb1r/ppp1pppp/5n2/3P4/8/5N2/PPPP1PPP/RNBbK2R w KQkq - 0 7",
        5,
    )
    limits = SearchLimits(
        depth_series=2,
        max_series_per_node=32,
        max_generation_positions=250_000,
        collect_all_root_scores=False,
    )
    enabled_lane = SeriesSearcher._apply_root_promotion_mate_lane

    def disabled_lane(
        _searcher: SeriesSearcher,
        _state: ProgressiveState,
        series: object,
        **_kwargs: object,
    ) -> object:
        return series

    def run(enabled: bool) -> tuple[float, tuple[object, ...]]:
        monkeypatch.setattr(
            SeriesSearcher,
            "_apply_root_promotion_mate_lane",
            enabled_lane if enabled else disabled_lane,
        )
        started = time.perf_counter()
        result = analyze(state, limits)
        elapsed = time.perf_counter() - started
        signature = (
            result.score,
            result.best_series.machine_notation if result.best_series else None,
            result.completed_depth,
            result.exact_width,
            result.proof,
            result.forced,
            asdict(result.stats),
        )
        return elapsed, signature

    # Warm both paths, then alternate first-run order to reduce temporal bias.
    run(False)
    run(True)
    baseline_times: list[float] = []
    candidate_times: list[float] = []
    signatures: list[tuple[object, ...]] = []
    for sample in range(7):
        for enabled in ((False, True) if sample % 2 == 0 else (True, False)):
            elapsed, signature = run(enabled)
            (candidate_times if enabled else baseline_times).append(elapsed)
            signatures.append(signature)

    assert all(signature == signatures[0] for signature in signatures)
    assert statistics.median(candidate_times) <= (
        statistics.median(baseline_times) * 1.10
    )
