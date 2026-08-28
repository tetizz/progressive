from __future__ import annotations

import os

import pytest

import scottish_progressive.evaluation as evaluation
import scottish_progressive.search as search_module
from scottish_progressive.model import ProgressiveState
from scottish_progressive.rules import play_series
from scottish_progressive.search import (
    MATE_SCORE,
    UNKNOWN_PROOF_BOUNDS,
    ScoredSeries,
    SearchLimits,
    SeriesSearcher,
    analyze,
)
from scottish_progressive.teacher_value_features import state_from_pfen


GAME1_S3_PFEN = (
    "rnbqkb1r/ppp1pppp/3p1n2/8/8/1P6/P1PPPPPP/RNBQKBNR "
    "w KQkq - 1 3 | series=3 quiet=0 progressive_ep=- "
    "rules=scottish-modern-common-v1 quiet_draw=manual-proof-required"
)
GAME1_FALSE_MATE_ROOT = ("e2e3", "d1f3", "f1b5")
GAME1_BUCE_REFUTATION = ("c7c6", "c8g4", "g4f3", "c6b5")
GAME1_OLD_UNSAFE_ROOT = ("c1b2", "b2f6", "f6g7")
GAME1_SAFE_FALLBACK = ("e2e4", "e4e5", "e5f6")


def _require_source_matched_native() -> None:
    native = evaluation._native_eval
    if native is None or not hasattr(native, "prepare_complete_series"):
        pytest.skip("source-matched native boundary search is not built")
    assert native.SOURCE_IDENTITY == evaluation._native_source_identity()


def _game1_state() -> ProgressiveState:
    state = state_from_pfen(GAME1_S3_PFEN)
    claimed_child = play_series(state, GAME1_FALSE_MATE_ROOT).final_state
    counter = play_series(claimed_child, GAME1_BUCE_REFUTATION)
    assert counter.outcome is None
    return state


def _game1_limits(max_work: int) -> SearchLimits:
    return SearchLimits(
        depth_series=5,
        max_series_per_node=8,
        time_limit_seconds=30.0,
        max_generation_positions=max_work,
        collect_all_root_scores=False,
        native_threads=1,
    )


def test_game1_s3_low_budget_fails_closed_without_false_mate() -> None:
    """The 5M contract keeps a legal move but no stale f3 score or depth."""

    _require_source_matched_native()
    result = analyze(_game1_state(), _game1_limits(5_000_000))

    assert result.completed_depth == 0
    assert not result.timed_out
    assert result.work_limit_reached
    assert result.best_series is not None
    assert result.best_series.moves != GAME1_FALSE_MATE_ROOT
    assert result.score == result.root_evaluation.total
    assert result.proof is None
    assert result.principal_variation == (result.best_series,)
    assert result.alternatives == ()
    assert not result.exact_width
    assert not result.root_scores_complete
    assert result.stats.root_mate_claim_quarantines >= 1
    assert result.stats.root_mate_claim_prior_depth_discards >= 1
    assert result.stats.root_mate_claim_move_only_fallbacks >= 1
    assert result.stats.root_mate_claim_final_discards == 0
    assert result.stats.selected_pv_horizon_unknown >= 1
    assert result.stats.work_positions <= 5_000_000


def test_game1_s3_serious_budget_widens_and_keeps_proven_mates_out() -> None:
    """The 20M gate widens rather than publishing the old unsafe D5 line.

    The internal-boundary ladder proves that the former ``c1b2/Bxf6/Bxg7``
    winner permits a later one-series mate. Exact-adverse alternatives must
    trigger the 832-root widening; if that wider proof work reaches the fixed
    budget, only a reply-mate-exhausted move may survive without its shallow
    score, depth, alternatives, or proof.
    """

    _require_source_matched_native()
    result = analyze(_game1_state(), _game1_limits(20_000_000))

    assert result.completed_depth == 0
    assert not result.timed_out
    assert result.work_limit_reached
    assert result.best_series is not None
    assert result.best_series.moves == GAME1_SAFE_FALLBACK
    assert result.best_series.moves != GAME1_OLD_UNSAFE_ROOT
    assert result.score == result.root_evaluation.total == -46
    assert result.principal_variation == (result.best_series,)
    assert result.alternatives == ()
    assert result.proof is None
    assert not result.exact_width
    assert not result.root_scores_complete
    assert result.stats.root_mate_claim_quarantines >= 3
    assert result.stats.root_mate_claim_prior_depth_discards == 0
    assert result.stats.root_mate_claim_final_discards == 0
    assert result.stats.root_safety_all_mating_widenings >= 1
    assert result.stats.root_safety_widened_candidates > 32
    assert result.stats.root_safety_widened_exact_children >= 1
    assert result.stats.final_fallback_reply_mate_exhausted == 1
    assert result.stats.selected_pv_horizon_unknown == 0
    assert result.stats.root_safety_unknown_interruptions == 0
    assert result.stats.work_positions == 20_000_000


def test_candidate_local_mate_proof_handles_both_signs_and_terminal_root() -> None:
    white = play_series(ProgressiveState.initial(), ("e2e4",))
    black = play_series(white.final_state, ("e7e6", "g8f6"))
    positive = MATE_SCORE - 3
    negative = -MATE_SCORE + 3

    assert SeriesSearcher._selected_root_has_publishable_mate_claim(
        positive,
        white,
        (ScoredSeries(white, positive, (), (1, 1)),),
    )
    assert not SeriesSearcher._selected_root_has_publishable_mate_claim(
        positive,
        white,
        (ScoredSeries(white, positive, (), UNKNOWN_PROOF_BOUNDS),),
    )
    assert SeriesSearcher._selected_root_has_publishable_mate_claim(
        negative,
        black,
        (ScoredSeries(black, negative, (), (-1, -1)),),
    )
    assert not SeriesSearcher._selected_root_has_publishable_mate_claim(
        negative,
        black,
        (ScoredSeries(black, negative, (), UNKNOWN_PROOF_BOUNDS),),
    )

    mate_state = ProgressiveState.from_fen(
        "7k/5K2/6Q1/8/8/8/8/8 w - - 0 1",
        1,
    )
    terminal = play_series(mate_state, ("g6g7",))
    assert terminal.outcome is not None
    assert SeriesSearcher._selected_root_has_publishable_mate_claim(
        MATE_SCORE - 1,
        terminal,
        (ScoredSeries(terminal, MATE_SCORE - 1),),
    )
    assert not SeriesSearcher._selected_root_has_publishable_mate_claim(
        -MATE_SCORE + 1,
        terminal,
        (ScoredSeries(terminal, -MATE_SCORE + 1),),
    )


@pytest.mark.parametrize("mover_white", (True, False))
@pytest.mark.parametrize("claim_would_win", (True, False))
@pytest.mark.parametrize("collect_all", (True, False))
def test_candidate_local_unknown_mate_never_enters_root_ranking(
    monkeypatch: pytest.MonkeyPatch,
    mover_white: bool,
    claim_would_win: bool,
    collect_all: bool,
) -> None:
    if mover_white:
        root = ProgressiveState.initial()
        first = play_series(root, ("e2e4",))
        second = play_series(root, ("d2d4",))
        finite_score = 50
        claim_score = MATE_SCORE - 3 if claim_would_win else -MATE_SCORE + 3
    else:
        root = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
        first = play_series(root, ("e7e6", "g8f6"))
        second = play_series(root, ("d7d6", "c8g4"))
        finite_score = -50
        claim_score = -MATE_SCORE + 3 if claim_would_win else MATE_SCORE - 3

    finite, claim = (first, second) if claim_would_win else (second, first)
    frontier = search_module._GeneratedSeriesList(
        [finite, claim] if claim_would_win else [claim, finite],
        width_complete=True,
    )
    child_scores = {
        finite.final_state.transposition_key: (
            finite_score,
            UNKNOWN_PROOF_BOUNDS,
        ),
        claim.final_state.transposition_key: (
            claim_score,
            UNKNOWN_PROOF_BOUNDS,
        ),
    }
    windows: dict[tuple[int, str, int, int], tuple[int, int]] = {}
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            collect_all_root_scores=collect_all,
        )
    )
    monkeypatch.setattr(
        searcher,
        "_ordered_generated",
        lambda *_args, **_kwargs: frontier,
    )
    monkeypatch.setattr(searcher, "_start_native_subtree", lambda _state: None)
    monkeypatch.setattr(searcher, "_root_pvs_eligible", lambda *_args: False)

    def fake_minimax(
        state: ProgressiveState,
        _depth: int,
        alpha: int,
        beta: int,
        _ply_from_root: int,
    ) -> tuple[int, tuple, tuple[int, int]]:
        windows[state.transposition_key] = (alpha, beta)
        score, bounds = child_scores[state.transposition_key]
        return score, (), bounds

    monkeypatch.setattr(searcher, "_minimax", fake_minimax)
    score, pv, alternatives, proof = searcher._search_root_pass(root, 1, (), {})

    assert score == finite_score
    assert pv == (finite,)
    assert [item.series for item in alternatives] == [finite]
    assert proof is None
    assert claim.machine_notation in searcher._root_mate_claim_quarantines
    assert searcher.stats.root_mate_claim_quarantines == 1
    assert not searcher._root_scores_complete
    assert searcher._tt == {}
    if not claim_would_win and not collect_all:
        assert windows[finite.final_state.transposition_key] == (
            -MATE_SCORE * 2,
            MATE_SCORE * 2,
        )


@pytest.mark.parametrize("mover_white", (True, False))
def test_warm_exact_unknown_mate_entry_cannot_select_or_rewrite_root(
    monkeypatch: pytest.MonkeyPatch,
    mover_white: bool,
) -> None:
    if mover_white:
        root = ProgressiveState.initial()
        claim = play_series(root, ("e2e4",))
        finite = play_series(root, ("d2d4",))
        claim_score, finite_score = MATE_SCORE - 3, 50
    else:
        root = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
        claim = play_series(root, ("e7e6", "g8f6"))
        finite = play_series(root, ("d7d6", "c8g4"))
        claim_score, finite_score = -MATE_SCORE + 3, -50

    frontier = search_module._GeneratedSeriesList(
        [claim, finite],
        width_complete=True,
    )
    searcher = SeriesSearcher(
        SearchLimits(depth_series=2, collect_all_root_scores=True)
    )
    monkeypatch.setattr(
        searcher,
        "_ordered_generated",
        lambda *_args, **_kwargs: frontier,
    )
    monkeypatch.setattr(searcher, "_start_native_subtree", lambda _state: None)
    seeded = {
        searcher._tt_key(claim.final_state): search_module._TTEntry(
            1,
            claim_score,
            search_module.Bound.EXACT,
            (),
            UNKNOWN_PROOF_BOUNDS,
        ),
        searcher._tt_key(finite.final_state): search_module._TTEntry(
            1,
            finite_score,
            search_module.Bound.EXACT,
            (),
            UNKNOWN_PROOF_BOUNDS,
        ),
    }
    searcher._tt.update(seeded)

    score, pv, alternatives, proof = searcher._search_root_pass(root, 2, (), {})

    assert score == finite_score
    assert pv == (finite,)
    assert [item.series for item in alternatives] == [finite]
    assert proof is None
    assert searcher.stats.tt_hits == 2
    assert claim.machine_notation in searcher._root_mate_claim_quarantines
    assert searcher._tt == seeded


@pytest.mark.parametrize(
    ("cause_type", "timed_out", "work_limited"),
    (
        (search_module._WorkLimit, False, True),
        (search_module._Timeout, True, False),
    ),
)
def test_interrupt_after_quarantine_keeps_scored_clean_root_move_only(
    monkeypatch: pytest.MonkeyPatch,
    cause_type: type[Exception],
    timed_out: bool,
    work_limited: bool,
) -> None:
    root = ProgressiveState.initial()
    claim = play_series(root, ("e2e4",))
    clean = play_series(root, ("d2d4",))
    interrupted = play_series(root, ("c2c4",))
    frontier = search_module._GeneratedSeriesList(
        [claim, clean, interrupted],
        width_complete=True,
    )
    searcher = SeriesSearcher(
        SearchLimits(depth_series=1, collect_all_root_scores=False)
    )
    monkeypatch.setattr(
        searcher,
        "_ordered_generated",
        lambda *_args, **_kwargs: frontier,
    )
    monkeypatch.setattr(searcher, "_start_native_subtree", lambda _state: None)
    monkeypatch.setattr(searcher, "_root_pvs_eligible", lambda *_args: False)
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: False,
    )

    def fake_minimax(
        state: ProgressiveState,
        _depth: int,
        _alpha: int,
        _beta: int,
        _ply_from_root: int,
    ) -> tuple[int, tuple, tuple[int, int]]:
        if state.transposition_key == claim.final_state.transposition_key:
            return MATE_SCORE - 3, (), UNKNOWN_PROOF_BOUNDS
        if state.transposition_key == clean.final_state.transposition_key:
            return 50, (), UNKNOWN_PROOF_BOUNDS
        raise cause_type()

    monkeypatch.setattr(searcher, "_minimax", fake_minimax)
    result = searcher.run(root)

    assert result.completed_depth == 0
    assert result.timed_out is timed_out
    assert result.work_limit_reached is work_limited
    assert result.best_series == clean
    assert result.score == result.root_evaluation.total
    assert result.principal_variation == (clean,)
    assert result.alternatives == ()
    assert result.proof is None
    assert claim.machine_notation in searcher._root_mate_claim_quarantines
    assert result.stats.root_mate_claim_move_only_fallbacks == 1


def test_adjudication_after_quarantine_keeps_scored_clean_root_move_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    claim = play_series(root, ("e2e4",))
    clean = play_series(root, ("d2d4",))
    pending = play_series(root, ("c2c4",))
    frontier = search_module._GeneratedSeriesList(
        [claim, clean, pending],
        width_complete=True,
    )
    searcher = SeriesSearcher(
        SearchLimits(depth_series=1, collect_all_root_scores=False)
    )
    monkeypatch.setattr(
        searcher,
        "_ordered_generated",
        lambda *_args, **_kwargs: frontier,
    )
    monkeypatch.setattr(searcher, "_start_native_subtree", lambda _state: None)
    monkeypatch.setattr(searcher, "_root_pvs_eligible", lambda *_args: False)

    def fake_minimax(
        state: ProgressiveState,
        _depth: int,
        _alpha: int,
        _beta: int,
        _ply_from_root: int,
    ) -> tuple[int, tuple, tuple[int, int]]:
        if state.transposition_key == claim.final_state.transposition_key:
            return MATE_SCORE - 3, (), UNKNOWN_PROOF_BOUNDS
        if state.transposition_key == clean.final_state.transposition_key:
            return 50, (), UNKNOWN_PROOF_BOUNDS
        raise search_module._AdjudicationPending

    monkeypatch.setattr(searcher, "_minimax", fake_minimax)
    result = searcher.run(root)

    assert result.completed_depth == 0
    assert result.adjudication_status == "manual-proof-required"
    assert result.best_series == clean
    assert result.score == result.root_evaluation.total
    assert result.principal_variation == (clean,)
    assert result.alternatives == ()
    assert result.proof is None
    assert claim.machine_notation in searcher._root_mate_claim_quarantines


@pytest.mark.parametrize("mover_white", (True, False))
@pytest.mark.parametrize("claim_would_win", (True, False))
def test_widened_root_quarantines_unknown_mate_before_alternatives(
    monkeypatch: pytest.MonkeyPatch,
    mover_white: bool,
    claim_would_win: bool,
) -> None:
    if mover_white:
        root = ProgressiveState.initial()
        claim = play_series(root, ("e2e4",))
        finite = play_series(root, ("d2d4",))
        claim_score = MATE_SCORE - 3 if claim_would_win else -MATE_SCORE + 3
        finite_score = 50
    else:
        root = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
        claim = play_series(root, ("e7e6", "g8f6"))
        finite = play_series(root, ("d7d6", "c8g4"))
        claim_score = -MATE_SCORE + 3 if claim_would_win else MATE_SCORE - 3
        finite_score = -50

    candidates = [finite, claim] if claim_would_win else [claim, finite]
    scores = {
        claim.final_state.transposition_key: claim_score,
        finite.final_state.transposition_key: finite_score,
    }
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_generation_positions=10_000_000,
            collect_all_root_scores=False,
        )
    )
    monkeypatch.setattr(
        searcher,
        "_generate",
        lambda *_args, **_kwargs: (candidates, False),
    )

    def exact_safe(
        self: SeriesSearcher,
        state: ProgressiveState,
    ) -> None:
        self._mark_root_child_exact_exhausted(state.transposition_key)
        return None

    monkeypatch.setattr(
        searcher,
        "_root_child_immediate_mate",
        exact_safe.__get__(searcher, SeriesSearcher),
    )
    monkeypatch.setattr(
        searcher,
        "_minimax",
        lambda state, *_args: (
            scores[state.transposition_key],
            (),
            UNKNOWN_PROOF_BOUNDS,
        ),
    )

    score, pv, alternatives, proof = searcher._root_all_mating_widening(
        root,
        1,
        (),
        (),
    )

    assert score == finite_score
    assert pv == (finite,)
    assert [item.series for item in alternatives] == [finite]
    assert proof is None
    assert claim.machine_notation in searcher._root_mate_claim_quarantines
    assert searcher.stats.root_mate_claim_quarantines == 1


def test_final_boundary_omits_bypassed_nonselected_unknown_mate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    finite = play_series(root, ("e2e4",))
    claim = play_series(root, ("d2d4",))
    finite_scored = ScoredSeries(finite, 50)
    claim_scored = ScoredSeries(
        claim,
        -MATE_SCORE + 3,
        (),
        UNKNOWN_PROOF_BOUNDS,
    )
    searcher = SeriesSearcher(
        SearchLimits(depth_series=1, collect_all_root_scores=True)
    )

    def bypassed_root(
        self: SeriesSearcher,
        _state: ProgressiveState,
        _depth: int,
        _prefix: tuple[str, ...],
    ):
        self._root_scores_complete = True
        return (
            finite_scored.score,
            (finite,),
            (finite_scored, claim_scored),
            "black",
        )

    monkeypatch.setattr(searcher, "_search_root", bypassed_root.__get__(
        searcher,
        SeriesSearcher,
    ))
    result = searcher.run(root)

    assert result.completed_depth == 1
    assert result.best_series == finite
    assert result.score == finite_scored.score
    assert result.alternatives == (finite_scored,)
    assert result.proof is None
    assert not result.root_scores_complete
    assert not result.exact_width
    assert claim.machine_notation in searcher._root_mate_claim_quarantines
    assert result.stats.root_mate_claim_quarantines == 1


@pytest.mark.parametrize(
    ("fen", "series_number", "expected_score"),
    (
        ("7k/6Q1/6K1/8/8/8/8/8 b - - 0 1", 2, MATE_SCORE - 1),
        ("8/8/8/8/8/1k6/1q6/K7 w - - 0 1", 1, -MATE_SCORE + 1),
    ),
)
def test_zero_move_boundary_mates_keep_their_authoritative_score(
    fen: str,
    series_number: int,
    expected_score: int,
) -> None:
    state = ProgressiveState.from_fen(fen, series_number)
    result = analyze(state, SearchLimits(depth_series=1))

    assert result.best_series is not None
    assert result.best_series.moves == ()
    assert result.score == expected_score
    assert result.proof == ("white" if expected_score > 0 else "black")
    assert result.stats.root_mate_claim_quarantines == 0
    assert result.stats.root_mate_claim_final_discards == 0


@pytest.mark.parametrize(
    ("cause_type", "timed_out", "work_limited"),
    (
        (search_module._WorkLimit, False, True),
        (search_module._Timeout, True, False),
    ),
)
def test_interrupted_unproved_mate_candidate_is_reduced_to_d0(
    monkeypatch: pytest.MonkeyPatch,
    cause_type: type[Exception],
    timed_out: bool,
    work_limited: bool,
) -> None:
    """Even a bypassed quarantine cannot publish a stale mate from partials."""

    state = ProgressiveState.initial()
    candidate = play_series(state, ("e2e4",))
    claim = ScoredSeries(candidate, MATE_SCORE - 3)
    searcher = SeriesSearcher(SearchLimits(depth_series=2))

    def interrupted_root(
        _self: SeriesSearcher,
        _state: ProgressiveState,
        depth: int,
        _prefix: tuple[str, ...],
    ):
        if depth == 1:
            return claim.score, (candidate,), (claim,), None
        raise search_module._RootInterrupted(
            (claim,),
            cause_type(),
            candidate,
        )

    monkeypatch.setattr(SeriesSearcher, "_search_root", interrupted_root)
    result = searcher.run(state)

    assert result.completed_depth == 0
    assert result.timed_out is timed_out
    assert result.work_limit_reached is work_limited
    assert result.best_series is None
    assert result.score == result.root_evaluation.total
    assert result.principal_variation == ()
    assert result.alternatives == ()
    assert result.proof is None
    assert result.stats.root_mate_claim_quarantines == 1
    assert result.stats.root_mate_claim_prior_depth_discards == 1
    assert result.stats.root_mate_claim_final_discards == 0


@pytest.mark.parametrize(
    ("cause_type", "timed_out", "work_limited"),
    (
        (search_module._WorkLimit, False, True),
        (search_module._Timeout, True, False),
    ),
)
def test_final_defense_scrubs_bypassed_unproved_mate_after_raw_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    cause_type: type[Exception],
    timed_out: bool,
    work_limited: bool,
) -> None:
    state = ProgressiveState.initial()
    candidate = play_series(state, ("e2e4",))
    claim = ScoredSeries(candidate, MATE_SCORE - 3)
    searcher = SeriesSearcher(SearchLimits(depth_series=2))

    def interrupted_root(
        _self: SeriesSearcher,
        _state: ProgressiveState,
        depth: int,
        _prefix: tuple[str, ...],
    ):
        if depth == 1:
            return claim.score, (candidate,), (claim,), None
        raise cause_type()

    monkeypatch.setattr(SeriesSearcher, "_search_root", interrupted_root)
    result = searcher.run(state)

    assert result.completed_depth == 0
    assert result.timed_out is timed_out
    assert result.work_limit_reached is work_limited
    assert result.best_series == candidate
    assert result.score == result.root_evaluation.total
    assert result.principal_variation == (candidate,)
    assert result.alternatives == ()
    assert result.proof is None
    assert result.stats.root_mate_claim_final_discards == 1


def test_all_quarantined_roots_return_legal_d0_move_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ProgressiveState.initial()
    candidates = (
        play_series(state, ("e2e4",)),
        play_series(state, ("d2d4",)),
    )
    claims = tuple(
        ScoredSeries(candidate, MATE_SCORE - 3) for candidate in candidates
    )
    searcher = SeriesSearcher(SearchLimits(depth_series=1))

    def all_claims_root_pass(
        self: SeriesSearcher,
        root: ProgressiveState,
        _depth: int,
        _prefix: tuple[str, ...],
        _overrides,
        _horizon_overrides=None,
        _horizon_vetoes=frozenset(),
        _frontier=None,
    ):
        for claim in claims:
            if (
                claim.series.machine_notation
                not in self._root_mate_claim_quarantines
            ):
                return claim.score, (claim.series,), (claim,), None
        return self._evaluate(root).total, (), (), None

    monkeypatch.setattr(
        SeriesSearcher,
        "_search_root_pass",
        all_claims_root_pass,
    )
    result = searcher.run(state)

    assert result.completed_depth == 0
    assert not result.timed_out
    assert not result.work_limit_reached
    assert result.best_series == candidates[0]
    assert result.score == result.root_evaluation.total
    assert result.principal_variation == (candidates[0],)
    assert result.alternatives == ()
    assert result.proof is None
    assert result.stats.root_mate_claim_quarantines == 2
    assert result.stats.root_mate_claim_all_quarantined == 1
    assert result.stats.root_mate_claim_move_only_fallbacks == 1


@pytest.mark.parametrize(
    ("cause_type", "timed_out", "work_limited"),
    (
        (search_module._WorkLimit, False, True),
        (search_module._Timeout, True, False),
    ),
)
def test_all_quarantined_retry_preserves_interrupt_reason(
    monkeypatch: pytest.MonkeyPatch,
    cause_type: type[Exception],
    timed_out: bool,
    work_limited: bool,
) -> None:
    state = ProgressiveState.initial()
    candidate = play_series(state, ("e2e4",))
    claim = ScoredSeries(candidate, MATE_SCORE - 3)
    searcher = SeriesSearcher(SearchLimits(depth_series=1))
    calls = 0

    def interrupted_retry(
        _self: SeriesSearcher,
        _root: ProgressiveState,
        _depth: int,
        _prefix: tuple[str, ...],
        _overrides,
        _horizon_overrides=None,
        _horizon_vetoes=frozenset(),
        _frontier=None,
    ):
        nonlocal calls
        calls += 1
        if calls == 1:
            return claim.score, (candidate,), (claim,), None
        raise cause_type()

    monkeypatch.setattr(
        SeriesSearcher,
        "_search_root_pass",
        interrupted_retry,
    )
    result = searcher.run(state)

    assert calls == 2
    assert result.completed_depth == 0
    assert result.timed_out is timed_out
    assert result.work_limit_reached is work_limited
    assert result.best_series == candidate
    assert result.score == result.root_evaluation.total
    assert result.principal_variation == (candidate,)
    assert result.alternatives == ()
    assert result.proof is None
    assert result.stats.root_mate_claim_quarantines == 1
    assert result.stats.root_mate_claim_all_quarantined == 1
    assert result.stats.root_mate_claim_move_only_fallbacks == 1


@pytest.mark.parametrize("root_pending", (False, True))
def test_adjudication_cannot_resurrect_prior_quarantined_root(
    monkeypatch: pytest.MonkeyPatch,
    root_pending: bool,
) -> None:
    state = ProgressiveState.initial()
    prior = play_series(state, ("e2e4",))
    fallback = play_series(state, ("d2d4",))
    searcher = SeriesSearcher(SearchLimits(depth_series=2))

    def pending_root(
        self: SeriesSearcher,
        _state: ProgressiveState,
        depth: int,
        _prefix: tuple[str, ...],
    ):
        if depth == 1:
            return 25, (prior,), (ScoredSeries(prior, 25),), None
        self._root_mate_claim_quarantines.add(prior.machine_notation)
        if root_pending:
            raise search_module._RootAdjudicationPending(fallback)
        raise search_module._AdjudicationPending()

    monkeypatch.setattr(SeriesSearcher, "_search_root", pending_root)
    result = searcher.run(state)

    assert result.completed_depth == 0
    assert result.adjudication_status == "manual-proof-required"
    assert result.best_series == (fallback if root_pending else None)
    assert result.score == result.root_evaluation.total
    assert result.principal_variation == (
        (fallback,) if root_pending else ()
    )
    assert result.alternatives == ()
    assert result.proof is None
    assert result.stats.root_mate_claim_prior_depth_discards == 1
    assert result.stats.root_mate_claim_move_only_fallbacks == int(root_pending)


def test_repeated_native_search_cannot_restore_game1_false_mate() -> None:
    _require_source_matched_native()
    state = _game1_state()
    limits = _game1_limits(5_000_000)
    first = SeriesSearcher(limits).run(state)
    second = SeriesSearcher(limits).run(state)

    for result in (first, second):
        assert result.completed_depth == 0
        assert result.best_series is not None
        assert result.best_series.moves != GAME1_FALSE_MATE_ROOT
        assert result.score == result.root_evaluation.total
        assert result.proof is None
        assert result.alternatives == ()
        assert result.stats.root_mate_claim_quarantines >= 1


@pytest.mark.skipif(
    os.environ.get("SPC_RUN_INITIAL_B3_D5_GATE") != "1",
    reason="set SPC_RUN_INITIAL_B3_D5_GATE=1 for the 120s initial D5 gate",
)
def test_initial_b3_d5_has_no_mate_claim_guard_cost() -> None:
    """Opt-in promoted initial-position strength/performance regression."""

    _require_source_matched_native()
    result = analyze(
        ProgressiveState.initial(),
        SearchLimits(
            depth_series=5,
            max_series_per_node=32,
            time_limit_seconds=120.0,
            max_generation_positions=4_000_000_000,
            collect_all_root_scores=False,
            native_threads=16,
        ),
    )

    assert result.completed_depth == 5
    assert not result.timed_out
    assert not result.work_limit_reached
    assert result.best_series is not None
    assert result.best_series.machine_notation == "b2b3"
    assert result.stats.root_mate_claim_quarantines == 0
