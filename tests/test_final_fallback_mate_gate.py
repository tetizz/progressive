from __future__ import annotations

import time

import chess
import pytest

import scottish_progressive.search as search_module
import scottish_progressive.series_mate as series_mate
from scottish_progressive.mate_proof_cache import MateProofCache
from scottish_progressive.model import Outcome, ProgressiveState, SeriesResult
from scottish_progressive.rules import play_series
from scottish_progressive.search import (
    ScoredSeries,
    SearchLimits,
    SeriesSearcher,
    analyze,
)
from scottish_progressive.series_mate import SeriesMateProbe, SeriesMateStatus
from scottish_progressive.teacher_value_features import state_from_pfen


BUCEPHALUS_GAME_3AAEF_S5_PFEN = (
    "rnbqkb1r/pppp2pp/4p3/5p2/8/5P1N/PPPP1KPP/RNBn1B1R "
    "w kq - 0 7 | series=5 quiet=0 progressive_ep=- "
    "rules=scottish-modern-common-v1 quiet_draw=manual-proof-required"
)
BUCEPHALUS_GAME_3AAEF_FALLBACK = (
    "f2e1",
    "h3f4",
    "f4e6",
    "f1d3",
    "e6d8",
)
BUCEPHALUS_GAME_3AAEF_SAFE_RESELECTION = (
    "f2e2",
    "d2d4",
    "c1g5",
    "g5d8",
    "d8e7",
)
BUCEPHALUS_GAME_4044_S7_PFEN = (
    "rn1k1bnr/4ppp1/7p/8/8/5PP1/2PP3P/qNBbK1NR "
    "w K - 0 13 | series=7 quiet=0 progressive_ep=- "
    "rules=scottish-modern-common-v1 quiet_draw=manual-proof-required"
)
BUCEPHALUS_GAME_4044_FALLBACK = (
    "g3g4",
    "g4g5",
    "g5h6",
    "h6g7",
    "g7h8q",
    "h8f6",
    "c2c4",
)
BUCEPHALUS_GAME_3DFD_S8_PFEN = (
    "1nb1kbnr/ppNR2pp/4P3/8/5q2/2K5/PPP1PP2/5BN1 "
    "b k - 0 13 | series=8 quiet=0 progressive_ep=- "
    "rules=scottish-modern-common-v1 quiet_draw=manual-proof-required"
)
BUCEPHALUS_GAME_3DFD_LOSING_SERIES = ("f4c7",)
BUCEPHALUS_GAME_3DFD_REPLY_MATE = (
    "c3b3",
    "a2a4",
    "c2c4",
    "c4c5",
    "c5c6",
    "e2e4",
    "d7f7",
    "e6e7",
    "e7f8q",
)


def _require_native_mate() -> None:
    native = series_mate._native_mate  # noqa: SLF001
    if native is None or not hasattr(native, "find_series_mate"):
        pytest.skip("source-matched isolated native mate search is unavailable")
    assert native.SOURCE_IDENTITY == series_mate._native_mate_source_identity()  # noqa: SLF001


def _force_move_only_fallback(
    monkeypatch: pytest.MonkeyPatch,
    fallback: SeriesResult,
) -> None:
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_current_series_mate",
        lambda *_args, **_kwargs: None,
    )

    def pending(
        _self: SeriesSearcher,
        _state: ProgressiveState,
        _depth: int,
        _required_prefix: tuple[str, ...],
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[ScoredSeries, ...], None]:
        raise search_module._RootAdjudicationPending(fallback)  # noqa: SLF001

    monkeypatch.setattr(SeriesSearcher, "_search_root", pending)


def _force_completed_root(
    monkeypatch: pytest.MonkeyPatch,
    completed: SeriesResult,
) -> None:
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_current_series_mate",
        lambda *_args, **_kwargs: None,
    )

    def complete(
        _self: SeriesSearcher,
        _state: ProgressiveState,
        _depth: int,
        _required_prefix: tuple[str, ...],
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[ScoredSeries, ...], None]:
        scored = ScoredSeries(completed, 25)
        return 25, (completed,), (scored,), None

    monkeypatch.setattr(SeriesSearcher, "_search_root", complete)


def test_recorded_safe_child_requires_more_than_width_64_but_is_rank_62_at_512(
) -> None:
    state = state_from_pfen(BUCEPHALUS_GAME_3AAEF_S5_PFEN)
    notation = "/".join(BUCEPHALUS_GAME_3AAEF_SAFE_RESELECTION)

    def retained_notations(width: int) -> tuple[SeriesSearcher, list[object]]:
        searcher = SeriesSearcher(
            SearchLimits(
                depth_series=1,
                max_series_per_node=32,
                max_generation_positions=4_000_000_000,
                collect_all_root_scores=False,
            )
        )
        generated, complete = searcher._generate(  # noqa: SLF001
            state,
            ply_from_root=1,
            tactical_protection=True,
            max_frontier_states=width,
        )
        assert not complete
        candidates = (
            generated.references()
            if hasattr(generated, "references")
            else generated
        )
        return searcher, list(candidates)

    _narrow_searcher, narrow = retained_notations(64)
    assert notation not in [item.machine_notation for item in narrow]

    wide_searcher, wide = retained_notations(512)
    assert wide[61].machine_notation == notation
    witness = wide_searcher._materialize_series(wide[61])  # noqa: SLF001
    assert witness.final_state.position_hash == "c3504ae0c86022bb9c79b0ed8a89361c"
    assert witness.final_state.pfen == (
        "rnb1kb1r/ppppB1pp/4p3/5p2/3P4/5P1N/PPP1K1PP/RN1n1B1R "
        "b kq - 1 7 | series=6 quiet=0 progressive_ep=- "
        "rules=scottish-modern-common-v1 quiet_draw=manual-proof-required"
    )


@pytest.mark.parametrize(
    ("pfen", "moves"),
    (
        (
            BUCEPHALUS_GAME_3AAEF_S5_PFEN,
            BUCEPHALUS_GAME_3AAEF_FALLBACK,
        ),
        (
            BUCEPHALUS_GAME_4044_S7_PFEN,
            BUCEPHALUS_GAME_4044_FALLBACK,
        ),
    ),
)
def test_exact_bucephalus_mating_fallbacks_never_publish(
    monkeypatch: pytest.MonkeyPatch,
    pfen: str,
    moves: tuple[str, ...],
) -> None:
    """The two recorded D0 fallbacks both permit a replay-valid reply mate."""

    _require_native_mate()
    state = state_from_pfen(pfen)
    fallback = play_series(state, moves)
    _force_move_only_fallback(monkeypatch, fallback)

    result = analyze(
        state,
        SearchLimits(
            depth_series=8,
            max_series_per_node=32,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
        ),
    )

    assert result.best_series is None
    assert result.principal_variation == ()
    assert result.alternatives == ()
    assert result.completed_depth == 0
    assert result.proof is None
    assert not result.root_scores_complete
    assert result.stats.final_fallback_reply_mate_probes >= 1
    assert result.stats.final_fallback_reply_mate_found >= 1
    assert result.stats.final_fallback_reply_mate_rejections == 1
    assert result.stats.final_fallback_safe_reselection_attempts == 1
    assert result.stats.final_fallback_safe_reselection_candidates > 0
    assert result.stats.final_fallback_safe_reselection_exhausted == 0
    assert result.stats.final_fallback_safe_reselection_rescues == 0
    assert result.stats.final_fallback_safe_reselection_budget_interruptions == 1
    assert result.work_limit_reached
    assert 0 < result.stats.final_fallback_reply_mate_work <= 2_000_000
    assert 0 < result.stats.final_fallback_safe_reselection_work <= 2_000_000
    assert result.stats.generation_positions <= 2_000_000
    assert result.stats.root_safety_screen_positions == 0


def test_recorded_3aaef_loss_reselects_the_rank_62_exact_safe_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_native_mate()
    state = state_from_pfen(BUCEPHALUS_GAME_3AAEF_S5_PFEN)
    fallback = play_series(state, BUCEPHALUS_GAME_3AAEF_FALLBACK)
    expected = play_series(state, BUCEPHALUS_GAME_3AAEF_SAFE_RESELECTION)
    _force_move_only_fallback(monkeypatch, fallback)
    native_probe = series_mate.find_native_series_mate
    proof_caps: list[tuple[str, int | None]] = []

    def record_proof_cap(
        child: ProgressiveState,
        **kwargs: object,
    ) -> SeriesMateProbe:
        cap = kwargs.get("max_work")
        proof_caps.append(
            (child.position_hash, cap if type(cap) is int else None)
        )
        return native_probe(child, **kwargs)

    monkeypatch.setattr(series_mate, "find_native_series_mate", record_proof_cap)

    result = analyze(
        state,
        SearchLimits(
            depth_series=8,
            max_series_per_node=32,
            max_generation_positions=4_000_000_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
    )

    assert result.best_series == expected
    assert result.principal_variation == (expected,)
    assert result.score == -478
    assert result.completed_depth == 0
    assert result.proof is None
    assert result.alternatives == ()
    assert not result.root_scores_complete
    assert not result.exact_width
    assert not result.timed_out
    assert not result.work_limit_reached
    assert result.stats.final_fallback_reply_mate_rejections == 1
    assert result.stats.final_fallback_safe_reselection_attempts == 1
    assert result.stats.final_fallback_safe_reselection_exhausted == 1
    assert result.stats.final_fallback_safe_reselection_rescues == 1
    assert result.stats.final_fallback_safe_reselection_candidates == 61
    assert result.stats.final_fallback_safe_reselection_found == 58
    assert result.stats.final_fallback_safe_reselection_unknown == 2
    assert result.stats.final_fallback_safe_reselection_work == 39_737_928
    assert ("8e102ef8f6bc120eb34c52fa8b893dcf", 3_000_000) in proof_caps
    assert ("4a8fe538cf5805da8023981e12ac0a3d", 3_000_000) in proof_caps
    safe_cap = next(
        cap
        for position_hash, cap in proof_caps
        if position_hash == expected.final_state.position_hash
    )
    assert safe_cap is not None
    assert 7_276_223 <= safe_cap <= 10_000_000
    assert all(cap is None or cap <= 10_000_000 for _hash, cap in proof_caps)


def test_recorded_4044_frontier_still_fails_closed_without_an_exact_safe_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_native_mate()
    state = state_from_pfen(BUCEPHALUS_GAME_4044_S7_PFEN)
    fallback = play_series(state, BUCEPHALUS_GAME_4044_FALLBACK)
    _force_move_only_fallback(monkeypatch, fallback)

    result = analyze(
        state,
        SearchLimits(
            depth_series=8,
            max_series_per_node=32,
            max_generation_positions=4_000_000_000,
            collect_all_root_scores=False,
            native_threads=1,
        ),
    )

    assert result.best_series is None
    assert result.principal_variation == ()
    assert result.alternatives == ()
    assert result.completed_depth == 0
    assert result.proof is None
    assert not result.root_scores_complete
    assert result.stats.final_fallback_reply_mate_rejections == 1
    assert result.stats.final_fallback_safe_reselection_attempts == 1
    assert result.stats.final_fallback_safe_reselection_candidates > 0
    assert result.stats.final_fallback_safe_reselection_exhausted == 0
    assert result.stats.final_fallback_safe_reselection_rescues == 0
    assert 0 < result.stats.final_fallback_safe_reselection_work <= 40_000_000


def test_completed_d8_bucephalus_reply_mate_never_publishes() -> None:
    """A completed iteration cannot bypass immediate reply-mate certification."""

    _require_native_mate()
    state = state_from_pfen(BUCEPHALUS_GAME_3DFD_S8_PFEN)
    losing = play_series(state, BUCEPHALUS_GAME_3DFD_LOSING_SERIES)
    reply = play_series(losing.final_state, BUCEPHALUS_GAME_3DFD_REPLY_MATE)
    assert reply.outcome is Outcome.CHECKMATE

    result = analyze(
        state,
        SearchLimits(
            depth_series=8,
            max_series_per_node=32,
            max_generation_positions=4_000_000_000,
            time_limit_seconds=30.0,
            collect_all_root_scores=False,
            native_threads=1,
        ),
    )

    assert result.best_series is None
    assert result.principal_variation == ()
    assert result.alternatives == ()
    assert result.proof is None
    assert not result.root_scores_complete
    assert not result.work_limit_reached
    assert result.stats.generation_work_limit_hits == 0
    assert result.stats.selected_pv_horizon_repair_interruptions == 0
    assert result.stats.final_fallback_reply_mate_found == 1
    assert result.stats.final_fallback_reply_mate_rejections == 1


def _color_fixture(color: chess.Color) -> tuple[ProgressiveState, SeriesResult]:
    if color == chess.WHITE:
        root = ProgressiveState.initial()
        return root, play_series(root, ("e2e4",))
    root = play_series(ProgressiveState.initial(), ("e2e4",)).final_state
    return root, play_series(root, ("a7a6", "b7b6"))


@pytest.mark.parametrize("color", (chess.WHITE, chess.BLACK))
@pytest.mark.parametrize(
    ("status", "should_publish"),
    (
        (SeriesMateStatus.FOUND, False),
        (SeriesMateStatus.EXHAUSTED, True),
        (SeriesMateStatus.WORK_LIMIT, False),
        (SeriesMateStatus.DEADLINE, False),
        (SeriesMateStatus.UNSUPPORTED, False),
    ),
)
def test_only_exact_exhaustion_can_publish_a_d0_fallback(
    monkeypatch: pytest.MonkeyPatch,
    color: chess.Color,
    status: SeriesMateStatus,
    should_publish: bool,
) -> None:
    root, fallback = _color_fixture(color)
    _force_move_only_fallback(monkeypatch, fallback)
    fake_mate = SeriesResult(
        ("a1a1",),
        ("#",),
        fallback.final_state,
        ended_by_check=True,
        outcome=Outcome.CHECKMATE,
    )

    def injected_probe(*_args: object, **_kwargs: object) -> SeriesMateProbe:
        return SeriesMateProbe(
            status,
            "injected final-fallback result",
            series=fake_mate if status is SeriesMateStatus.FOUND else None,
            positions_visited=7,
            moves_generated=11,
        )

    monkeypatch.setattr(series_mate, "find_native_series_mate", injected_probe)
    result = analyze(
        root,
        SearchLimits(
            depth_series=2,
            # Isolate the selected-child publication theorem. The cap-32
            # emergency sibling policy has dedicated tests below.
            max_series_per_node=512,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
        ),
    )

    assert (result.best_series == fallback) is should_publish
    assert result.completed_depth == 0
    assert result.proof is None
    assert result.stats.final_fallback_reply_mate_probes == 1
    assert result.stats.final_fallback_reply_mate_work == 18
    assert result.stats.root_safety_screen_positions == 0
    if status is SeriesMateStatus.FOUND:
        assert result.stats.final_fallback_reply_mate_found == 1
        assert result.stats.final_fallback_reply_mate_rejections == 1
    elif status is SeriesMateStatus.EXHAUSTED:
        assert result.stats.final_fallback_reply_mate_exhausted == 1
        assert result.stats.final_fallback_reply_mate_rejections == 0
    else:
        assert result.stats.final_fallback_reply_mate_unknown == 1
        assert result.stats.final_fallback_reply_mate_rejections == 0


def test_final_fallback_lane_is_globally_bounded_and_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, fallback = _color_fixture(chess.WHITE)
    _force_move_only_fallback(monkeypatch, fallback)
    observed: dict[str, object] = {}

    def bounded_probe(*_args: object, **kwargs: object) -> SeriesMateProbe:
        observed.update(kwargs)
        return SeriesMateProbe(
            SeriesMateStatus.WORK_LIMIT,
            "injected work stop",
            positions_visited=13,
            moves_generated=17,
        )

    monkeypatch.setattr(series_mate, "find_native_series_mate", bounded_probe)
    result = analyze(
        root,
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=1_250_000,
            collect_all_root_scores=False,
        ),
    )

    assert 0 < int(observed["max_work"]) <= 1_000_000
    assert observed["max_positions"] is None
    assert result.best_series is None
    assert result.stats.final_fallback_reply_mate_work == 30
    assert result.stats.root_safety_screen_positions == 0
    assert result.stats.generation_positions >= 30
    assert result.stats.generation_positions <= 1_250_000


def test_final_fallback_deadline_is_unknown_and_does_not_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, fallback = _color_fixture(chess.WHITE)
    _force_move_only_fallback(monkeypatch, fallback)
    observed: dict[str, object] = {}

    def deadline_probe(*_args: object, **kwargs: object) -> SeriesMateProbe:
        observed.update(kwargs)
        return SeriesMateProbe(SeriesMateStatus.DEADLINE, "injected deadline")

    monkeypatch.setattr(series_mate, "find_native_series_mate", deadline_probe)
    result = analyze(
        root,
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            time_limit_seconds=0.25,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
        ),
    )

    assert 0 < float(observed["time_limit_seconds"]) <= 0.25
    assert result.best_series is None
    assert result.timed_out
    assert result.stats.final_fallback_reply_mate_unknown == 1


def test_expired_deadline_never_starts_the_final_fallback_solver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, fallback = _color_fixture(chess.WHITE)
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_current_series_mate",
        lambda *_args, **_kwargs: None,
    )

    def expired(
        _self: SeriesSearcher,
        _state: ProgressiveState,
        _depth: int,
        _required_prefix: tuple[str, ...],
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[ScoredSeries, ...], None]:
        time.sleep(0.02)
        raise search_module._RootInterrupted(  # noqa: SLF001
            (),
            search_module._Timeout(),  # noqa: SLF001
            fallback,
        )

    def unexpected_probe(*_args: object, **_kwargs: object) -> SeriesMateProbe:
        raise AssertionError("expired final deadline must not start native work")

    monkeypatch.setattr(SeriesSearcher, "_search_root", expired)
    monkeypatch.setattr(series_mate, "find_native_series_mate", unexpected_probe)
    result = analyze(
        root,
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            time_limit_seconds=0.01,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
        ),
    )

    assert result.best_series is None
    assert result.timed_out
    assert result.stats.final_fallback_reply_mate_probes == 0
    assert result.stats.final_fallback_reply_mate_unknown == 1


@pytest.mark.parametrize("color", (chess.WHITE, chess.BLACK))
@pytest.mark.parametrize(
    ("status", "should_publish"),
    (
        (SeriesMateStatus.FOUND, False),
        (SeriesMateStatus.EXHAUSTED, True),
        (SeriesMateStatus.WORK_LIMIT, False),
        (SeriesMateStatus.DEADLINE, False),
        (SeriesMateStatus.UNSUPPORTED, False),
    ),
)
def test_completed_depth_requires_exact_reply_mate_certification(
    monkeypatch: pytest.MonkeyPatch,
    color: chess.Color,
    status: SeriesMateStatus,
    should_publish: bool,
) -> None:
    root, completed = _color_fixture(color)
    _force_completed_root(monkeypatch, completed)
    fake_mate = SeriesResult(
        ("a1a1",),
        ("#",),
        completed.final_state,
        ended_by_check=True,
        outcome=Outcome.CHECKMATE,
    )
    probed: list[tuple[int, str, int, int]] = []

    def injected_probe(
        state: ProgressiveState,
        **_kwargs: object,
    ) -> SeriesMateProbe:
        probed.append(state.transposition_key)
        return SeriesMateProbe(
            status,
            "injected completed-depth result",
            series=fake_mate if status is SeriesMateStatus.FOUND else None,
            positions_visited=7,
            moves_generated=11,
        )

    monkeypatch.setattr(series_mate, "find_native_series_mate", injected_probe)
    result = analyze(
        root,
        SearchLimits(
            depth_series=1,
            # Keep this matrix about selected-child certification only.
            max_series_per_node=512,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
        ),
    )

    assert probed == [completed.final_state.transposition_key]
    assert (result.best_series == completed) is should_publish
    assert result.completed_depth == (1 if should_publish else 0)
    assert result.stats.final_fallback_reply_mate_probes == 1
    assert result.stats.final_fallback_reply_mate_work == 18
    if status is SeriesMateStatus.FOUND:
        assert result.stats.final_fallback_reply_mate_found == 1
        assert result.stats.final_fallback_reply_mate_rejections == 1
    elif status is SeriesMateStatus.EXHAUSTED:
        assert result.stats.final_fallback_reply_mate_exhausted == 1
        assert result.stats.final_fallback_reply_mate_rejections == 0
    else:
        assert result.stats.final_fallback_reply_mate_unknown == 1
        assert result.stats.final_fallback_reply_mate_rejections == 0


def test_found_partial_d0_candidate_never_promotes_unchecked_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    first = play_series(root, ("e2e4",))
    unchecked = play_series(root, ("d2d4",))
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_current_series_mate",
        lambda *_args, **_kwargs: None,
    )

    def partial(
        _self: SeriesSearcher,
        _state: ProgressiveState,
        _depth: int,
        _required_prefix: tuple[str, ...],
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[ScoredSeries, ...], None]:
        raise search_module._RootInterrupted(  # noqa: SLF001
            (ScoredSeries(first, 50), ScoredSeries(unchecked, 25)),
            search_module._WorkLimit(),  # noqa: SLF001
            unchecked,
        )

    probed: list[tuple[tuple[object, ...], bool]] = []

    def selected_found_siblings_unknown(
        _self: SeriesSearcher,
        state: ProgressiveState,
        **kwargs: object,
    ) -> SeriesMateStatus:
        key = SeriesSearcher._tt_key(state)
        probed.append((key, bool(kwargs.get("full_state_only", False))))
        if key == SeriesSearcher._tt_key(first.final_state):
            return SeriesMateStatus.FOUND
        return SeriesMateStatus.WORK_LIMIT

    monkeypatch.setattr(SeriesSearcher, "_search_root", partial)
    monkeypatch.setattr(
        SeriesSearcher,
        "_certify_final_fallback_reply_mate",
        selected_found_siblings_unknown,
    )
    result = analyze(
        root,
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
        ),
    )

    assert probed[0] == (SeriesSearcher._tt_key(first.final_state), False)
    assert (
        SeriesSearcher._tt_key(unchecked.final_state),
        True,
    ) in probed[1:]
    assert all(full_state_only for _key, full_state_only in probed[1:])
    assert result.best_series is None
    assert result.principal_variation == ()
    assert result.alternatives == ()
    assert result.stats.final_fallback_reply_mate_rejections == 1
    assert result.stats.final_fallback_safe_reselection_exhausted == 0
    assert result.stats.final_fallback_safe_reselection_unknown > 0
    assert result.stats.final_fallback_safe_reselection_rescues == 0


def test_found_candidate_reselects_only_an_exact_safe_widened_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNKNOWN and FOUND siblings are skipped; only EXHAUSTED may publish."""

    root = ProgressiveState.initial()
    selected = play_series(root, ("e2e4",))
    unknown = play_series(root, ("a2a3",))
    losing = play_series(root, ("a2a4",))
    second_losing = play_series(root, ("b1a3",))
    safe = play_series(root, ("b1c3",))
    _force_completed_root(monkeypatch, selected)

    status_by_key = {
        SeriesSearcher._tt_key(selected.final_state): SeriesMateStatus.FOUND,
        SeriesSearcher._tt_key(unknown.final_state): SeriesMateStatus.WORK_LIMIT,
        SeriesSearcher._tt_key(losing.final_state): SeriesMateStatus.FOUND,
        SeriesSearcher._tt_key(second_losing.final_state): SeriesMateStatus.FOUND,
        SeriesSearcher._tt_key(safe.final_state): SeriesMateStatus.EXHAUSTED,
    }
    observed: list[
        tuple[tuple[object, ...], int | None, bool]
    ] = []

    def injected_certification(
        _self: SeriesSearcher,
        state: ProgressiveState,
        **kwargs: object,
    ) -> SeriesMateStatus:
        key = SeriesSearcher._tt_key(state)
        observed.append(
            (
                key,
                kwargs.get("max_work"),
                bool(kwargs.get("full_state_only", False)),
            )
        )
        status = status_by_key.get(key)
        if status is None:
            raise AssertionError("safe child should stop the ordered rescue scan")
        return status

    monkeypatch.setattr(
        SeriesSearcher,
        "_certify_final_fallback_reply_mate",
        injected_certification,
    )
    result = analyze(
        root,
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=100_000_000,
            collect_all_root_scores=False,
        ),
    )

    assert [item[0] for item in observed] == [
        SeriesSearcher._tt_key(selected.final_state),
        SeriesSearcher._tt_key(unknown.final_state),
        SeriesSearcher._tt_key(losing.final_state),
        SeriesSearcher._tt_key(second_losing.final_state),
        SeriesSearcher._tt_key(safe.final_state),
    ]
    assert observed[0][1:] == (None, False)
    assert [item[1] for item in observed[1:]] == [3_000_000] * 4
    assert [item[2] for item in observed[1:]] == [True] * 4
    assert result.best_series == safe
    assert result.principal_variation == (safe,)
    assert result.root_evaluation.total == 0
    assert result.score == 4
    assert result.completed_depth == 0
    assert result.proof is None
    assert result.alternatives == ()
    assert not result.root_scores_complete
    assert not result.exact_width
    assert not result.work_limit_reached
    assert result.stats.final_fallback_reply_mate_rejections == 1
    assert result.stats.final_fallback_safe_reselection_attempts == 1
    assert result.stats.final_fallback_safe_reselection_candidates == 4
    assert result.stats.final_fallback_safe_reselection_found == 2
    assert result.stats.final_fallback_safe_reselection_exhausted == 1
    assert result.stats.final_fallback_safe_reselection_unknown == 1
    assert result.stats.final_fallback_safe_reselection_rescues == 1
    assert 0 < result.stats.final_fallback_safe_reselection_work <= 40_000_000


def test_safe_reselection_generation_and_proofs_share_one_40m_hard_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    selected = play_series(root, ("e2e4",))
    _force_completed_root(monkeypatch, selected)
    rescue_caps: list[int] = []

    def consume_exact_credit(
        self: SeriesSearcher,
        state: ProgressiveState,
        **kwargs: object,
    ) -> SeriesMateStatus:
        if SeriesSearcher._tt_key(state) == SeriesSearcher._tt_key(
            selected.final_state
        ):
            return SeriesMateStatus.FOUND
        assert kwargs.get("full_state_only") is True
        cap = kwargs.get("max_work")
        assert type(cap) is int and cap > 0
        rescue_caps.append(cap)
        self.stats.generation_positions += cap
        return SeriesMateStatus.WORK_LIMIT

    monkeypatch.setattr(
        SeriesSearcher,
        "_certify_final_fallback_reply_mate",
        consume_exact_credit,
    )
    result = analyze(
        root,
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=100_000_000,
            collect_all_root_scores=False,
        ),
    )

    assert result.best_series is None
    assert result.principal_variation == ()
    assert result.completed_depth == 0
    assert result.proof is None
    assert result.work_limit_reached
    assert rescue_caps
    assert all(0 < cap <= 3_000_000 for cap in rescue_caps)
    assert rescue_caps[-1] < 3_000_000
    assert result.stats.final_fallback_safe_reselection_work == 40_000_000
    assert result.stats.final_fallback_safe_reselection_budget_interruptions == 1
    assert result.stats.final_fallback_safe_reselection_rescues == 0


def test_exact_safe_proof_at_40m_edge_still_publishes_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    selected = play_series(root, ("e2e4",))
    safe = play_series(root, ("d2d4",))
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=100_000_000,
            collect_all_root_scores=False,
        )
    )

    def generation_uses_37m(
        _state: ProgressiveState,
        **_kwargs: object,
    ) -> tuple[list[SeriesResult], bool]:
        searcher.stats.generation_positions += 37_000_000
        return [safe], False

    def exact_on_last_credit(
        _state: ProgressiveState,
        **kwargs: object,
    ) -> SeriesMateStatus:
        assert kwargs.get("full_state_only") is True
        assert kwargs.get("max_work") == 3_000_000
        searcher.stats.generation_positions += 3_000_000
        return SeriesMateStatus.EXHAUSTED

    monkeypatch.setattr(searcher, "_generate", generation_uses_37m)
    monkeypatch.setattr(
        searcher,
        "_certify_final_fallback_reply_mate",
        exact_on_last_credit,
    )
    rescue = searcher._final_safe_reselection(  # noqa: SLF001
        root,
        selected,
        (),
        allow_widening=True,
    )

    assert rescue.series == safe
    assert rescue.score is None
    assert not rescue.work_limited
    assert not rescue.timed_out
    assert searcher.stats.final_fallback_safe_reselection_work == 40_000_000
    assert searcher.stats.final_fallback_safe_reselection_exhausted == 1
    assert searcher.stats.final_fallback_safe_reselection_rescues == 1


def test_cached_safe_child_after_40m_generation_still_publishes_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    selected = play_series(root, ("e2e4",))
    safe = play_series(root, ("d2d4",))
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=100_000_000,
            collect_all_root_scores=False,
        )
    )
    safe_key = searcher._tt_key(safe.final_state)  # noqa: SLF001
    searcher._root_child_mate_screen_cache[safe_key] = None  # noqa: SLF001
    searcher._root_child_native_mate_cache_keys.add(safe_key)  # noqa: SLF001

    def generation_uses_every_position(
        _state: ProgressiveState,
        **_kwargs: object,
    ) -> tuple[list[SeriesResult], bool]:
        searcher.stats.generation_positions += 40_000_000
        return [safe], False

    def no_fresh_probe(*_args: object, **_kwargs: object) -> SeriesMateStatus:
        raise AssertionError("cached exact safety must not require fresh work")

    monkeypatch.setattr(searcher, "_generate", generation_uses_every_position)
    monkeypatch.setattr(
        searcher,
        "_certify_final_fallback_reply_mate",
        no_fresh_probe,
    )
    rescue = searcher._final_safe_reselection(  # noqa: SLF001
        root,
        selected,
        (),
        allow_widening=True,
    )

    assert rescue.series == safe
    assert rescue.score is None
    assert not rescue.work_limited
    assert not rescue.timed_out
    assert searcher.stats.final_fallback_safe_reselection_work == 40_000_000
    assert searcher.stats.final_fallback_safe_reselection_exhausted == 1
    assert searcher.stats.final_fallback_safe_reselection_rescues == 1


def test_zero_credit_skips_uncached_child_then_publishes_cached_safe_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    selected = play_series(root, ("e2e4",))
    unknown = play_series(root, ("c2c4",))
    safe = play_series(root, ("d2d4",))
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=100_000_000,
            collect_all_root_scores=False,
        )
    )
    safe_key = searcher._tt_key(safe.final_state)  # noqa: SLF001
    searcher._root_child_mate_screen_cache[safe_key] = None  # noqa: SLF001
    searcher._root_child_native_mate_cache_keys.add(safe_key)  # noqa: SLF001

    def generation_uses_every_position(
        _state: ProgressiveState,
        **_kwargs: object,
    ) -> tuple[list[SeriesResult], bool]:
        searcher.stats.generation_positions += 40_000_000
        return [unknown, safe], False

    def no_fresh_probe(*_args: object, **_kwargs: object) -> SeriesMateStatus:
        raise AssertionError("zero-credit scan must not start fresh proof work")

    monkeypatch.setattr(searcher, "_generate", generation_uses_every_position)
    monkeypatch.setattr(
        searcher,
        "_certify_final_fallback_reply_mate",
        no_fresh_probe,
    )
    rescue = searcher._final_safe_reselection(  # noqa: SLF001
        root,
        selected,
        (),
        allow_widening=True,
    )

    assert rescue.series == safe
    assert rescue.score is None
    assert not rescue.work_limited
    assert not rescue.timed_out
    assert searcher.stats.final_fallback_safe_reselection_candidates == 2
    assert searcher.stats.final_fallback_safe_reselection_unknown == 1
    assert searcher.stats.final_fallback_safe_reselection_budget_interruptions == 1
    assert searcher.stats.final_fallback_safe_reselection_work == 40_000_000


@pytest.mark.parametrize("root_cap", (512, None))
def test_found_candidate_reuses_full_state_safe_retained_sibling_without_widening(
    monkeypatch: pytest.MonkeyPatch,
    root_cap: int | None,
) -> None:
    root = ProgressiveState.initial()
    selected = play_series(root, ("e2e4",))
    safe = play_series(root, ("d2d4",))
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_current_series_mate",
        lambda *_args, **_kwargs: None,
    )

    def completed_with_safe_sibling(
        _self: SeriesSearcher,
        _state: ProgressiveState,
        _depth: int,
        _required_prefix: tuple[str, ...],
    ) -> tuple[int, tuple[SeriesResult, ...], tuple[ScoredSeries, ...], None]:
        return (
            25,
            (selected,),
            (ScoredSeries(selected, 25), ScoredSeries(safe, 10)),
            None,
        )

    monkeypatch.setattr(SeriesSearcher, "_search_root", completed_with_safe_sibling)
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=root_cap,
            max_generation_positions=100_000_000,
            collect_all_root_scores=False,
        )
    )
    safe_key = searcher._tt_key(safe.final_state)  # noqa: SLF001
    searcher._root_child_mate_screen_cache[safe_key] = None  # noqa: SLF001
    searcher._root_child_native_mate_cache_keys.add(safe_key)  # noqa: SLF001

    def selected_is_losing(
        _self: SeriesSearcher,
        state: ProgressiveState,
        **_kwargs: object,
    ) -> SeriesMateStatus:
        assert SeriesSearcher._tt_key(state) == SeriesSearcher._tt_key(
            selected.final_state
        )
        return SeriesMateStatus.FOUND

    monkeypatch.setattr(
        SeriesSearcher,
        "_certify_final_fallback_reply_mate",
        selected_is_losing,
    )
    result = searcher.run(root)

    assert result.best_series == safe
    assert result.principal_variation == (safe,)
    assert result.score == 44
    assert result.completed_depth == 0
    assert result.proof is None
    assert result.alternatives == ()
    assert not result.root_scores_complete
    assert result.stats.final_fallback_safe_reselection_attempts == 0
    assert result.stats.final_fallback_safe_reselection_candidates == 1
    assert result.stats.final_fallback_safe_reselection_exhausted == 1
    assert result.stats.final_fallback_safe_reselection_rescues == 1


def test_retained_safe_choice_keeps_canonical_order_ahead_of_later_draw() -> None:
    root = ProgressiveState.initial()
    selected = play_series(root, ("e2e4",))
    safe = play_series(root, ("d2d4",))
    draw_base = play_series(root, ("g1f3",))
    draw = SeriesResult(
        draw_base.moves,
        draw_base.san,
        draw_base.final_state,
        outcome=Outcome.STALEMATE,
    )
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=100_000_000,
            collect_all_root_scores=False,
        )
    )
    safe_key = searcher._tt_key(safe.final_state)  # noqa: SLF001
    searcher._root_child_mate_screen_cache[safe_key] = None  # noqa: SLF001
    searcher._root_child_native_mate_cache_keys.add(safe_key)  # noqa: SLF001

    rescue = searcher._final_safe_reselection(  # noqa: SLF001
        root,
        selected,
        (ScoredSeries(safe, 10), ScoredSeries(draw, 0)),
        allow_widening=False,
    )

    assert rescue.series == safe
    assert rescue.score != 0
    assert searcher.stats.final_fallback_safe_reselection_terminal == 0


def test_safe_reselection_never_resurrects_selected_pv_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    selected = play_series(root, ("e2e4",))
    vetoed = play_series(root, ("d2d4",))
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=100_000_000,
            collect_all_root_scores=False,
        )
    )
    searcher._selected_pv_root_vetoes.add(vetoed.machine_notation)  # noqa: SLF001
    vetoed_key = searcher._tt_key(vetoed.final_state)  # noqa: SLF001
    searcher._root_child_mate_screen_cache[vetoed_key] = None  # noqa: SLF001
    searcher._root_child_native_mate_cache_keys.add(vetoed_key)  # noqa: SLF001
    generated_calls: list[bool] = []

    def only_vetoed_candidate(
        _state: ProgressiveState,
        **kwargs: object,
    ) -> tuple[list[SeriesResult], bool]:
        generated_calls.append(bool(kwargs.get("tactical_protection")))
        return [vetoed], False

    monkeypatch.setattr(searcher, "_generate", only_vetoed_candidate)
    rescue = searcher._final_safe_reselection(  # noqa: SLF001
        root,
        selected,
        (ScoredSeries(vetoed, 10),),
        allow_widening=True,
    )

    assert rescue.series is None
    assert generated_calls == [True]
    assert searcher.stats.final_fallback_safe_reselection_candidates == 0
    assert searcher.stats.final_fallback_safe_reselection_rescues == 0


def test_terminal_safe_reselection_uses_the_authoritative_root_terminal_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
        1,
    )
    selected = play_series(root, ("g6b1",))
    _force_completed_root(monkeypatch, selected)

    def every_nonterminal_is_losing(
        _self: SeriesSearcher,
        _state: ProgressiveState,
        **_kwargs: object,
    ) -> SeriesMateStatus:
        return SeriesMateStatus.FOUND

    monkeypatch.setattr(
        SeriesSearcher,
        "_certify_final_fallback_reply_mate",
        every_nonterminal_is_losing,
    )
    result = analyze(
        root,
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=100_000_000,
            collect_all_root_scores=False,
        ),
    )

    assert result.best_series is not None
    assert result.best_series.moves == ("g6g7",)
    assert result.best_series.outcome is Outcome.CHECKMATE
    assert result.score == search_module.MATE_SCORE - 1
    assert result.completed_depth == 0
    assert result.proof is None
    assert result.alternatives == ()
    assert not result.root_scores_complete
    assert result.stats.final_fallback_safe_reselection_terminal == 1
    assert result.stats.final_fallback_safe_reselection_rescues == 1


def test_full_state_reselection_proofs_neither_read_nor_write_clock_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    child = play_series(root, ("e2e4",)).final_state
    alias_board = child.board.copy(stack=False)
    alias_board.halfmove_clock += 7
    alias_board.fullmove_number += 3
    alias = ProgressiveState(
        alias_board,
        child.series_number,
        child.quiet_series,
        child.ep_targets,
    )
    assert child.transposition_key == alias.transposition_key
    assert SeriesSearcher._tt_key(child) != SeriesSearcher._tt_key(alias)

    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=100_000_000,
            collect_all_root_scores=False,
        )
    )
    searcher._root_child_native_mate_exhausted_keys.add(  # noqa: SLF001
        child.transposition_key
    )
    calls: list[tuple[object, ...]] = []

    def work_limited(
        state: ProgressiveState,
        **_kwargs: object,
    ) -> SeriesMateProbe:
        calls.append(SeriesSearcher._tt_key(state))
        return SeriesMateProbe(
            SeriesMateStatus.WORK_LIMIT,
            "injected full-state read check",
        )

    monkeypatch.setattr(series_mate, "find_native_series_mate", work_limited)
    assert (
        searcher._certify_final_fallback_reply_mate(  # noqa: SLF001
            alias,
            max_work=10_000_000,
            full_state_only=True,
        )
        is SeriesMateStatus.WORK_LIMIT
    )
    assert calls == [SeriesSearcher._tt_key(alias)]

    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=100_000_000,
            collect_all_root_scores=False,
        )
    )
    calls.clear()
    statuses = iter((SeriesMateStatus.EXHAUSTED, SeriesMateStatus.WORK_LIMIT))

    def exact_then_unknown(
        state: ProgressiveState,
        **_kwargs: object,
    ) -> SeriesMateProbe:
        calls.append(SeriesSearcher._tt_key(state))
        return SeriesMateProbe(
            next(statuses),
            "injected full-state write check",
        )

    monkeypatch.setattr(
        series_mate,
        "find_native_series_mate",
        exact_then_unknown,
    )
    assert (
        searcher._certify_final_fallback_reply_mate(  # noqa: SLF001
            child,
            max_work=10_000_000,
            full_state_only=True,
        )
        is SeriesMateStatus.EXHAUSTED
    )
    assert (
        child.transposition_key
        not in searcher._root_child_native_mate_exhausted_keys  # noqa: SLF001
    )
    assert (
        searcher._certify_final_fallback_reply_mate(  # noqa: SLF001
            alias,
            max_work=10_000_000,
        )
        is SeriesMateStatus.WORK_LIMIT
    )
    assert calls == [
        SeriesSearcher._tt_key(child),
        SeriesSearcher._tt_key(alias),
    ]


@pytest.mark.parametrize(
    ("cached_status", "should_publish"),
    (("found", False), ("exhausted", True)),
)
@pytest.mark.parametrize("color", (chess.WHITE, chess.BLACK))
def test_completed_depth_final_gate_reuses_exact_in_process_cache_entries(
    monkeypatch: pytest.MonkeyPatch,
    cached_status: str,
    should_publish: bool,
    color: chess.Color,
) -> None:
    root, completed = _color_fixture(color)
    _force_completed_root(monkeypatch, completed)
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
        )
    )
    child_key = completed.final_state.transposition_key
    if cached_status == "found":
        searcher._root_child_proven_mate_keys.add(child_key)  # noqa: SLF001
    else:
        searcher._root_child_native_mate_exhausted_keys.add(child_key)  # noqa: SLF001

    probed: list[tuple[object, ...]] = []

    def sibling_unknown(
        state: ProgressiveState,
        **_kwargs: object,
    ) -> SeriesMateProbe:
        probed.append(SeriesSearcher._tt_key(state))
        return SeriesMateProbe(
            SeriesMateStatus.WORK_LIMIT,
            "injected unproved sibling",
        )

    monkeypatch.setattr(series_mate, "find_native_series_mate", sibling_unknown)
    result = searcher.run(root)

    assert (result.best_series == completed) is should_publish
    assert result.completed_depth == (1 if should_publish else 0)
    assert result.stats.final_fallback_reply_mate_cache_hits == 1
    assert SeriesSearcher._tt_key(completed.final_state) not in probed
    if cached_status == "found":
        assert probed
        assert result.stats.final_fallback_reply_mate_probes == len(probed)
        assert result.stats.final_fallback_safe_reselection_attempts == 1
        assert result.stats.final_fallback_safe_reselection_unknown == len(probed)
        assert result.stats.final_fallback_safe_reselection_exhausted == 0
        assert result.stats.final_fallback_safe_reselection_rescues == 0
    else:
        assert probed == []
        assert result.stats.final_fallback_reply_mate_probes == 0
        assert result.stats.final_fallback_safe_reselection_attempts == 0


@pytest.mark.parametrize("cached_status", ("found", "exhausted"))
def test_completed_depth_final_gate_reuses_persistent_proof_ledger(
    monkeypatch: pytest.MonkeyPatch,
    cached_status: str,
) -> None:
    _require_native_mate()
    cache = MateProofCache()
    if cached_status == "found":
        root = state_from_pfen(BUCEPHALUS_GAME_3DFD_S8_PFEN)
        completed = play_series(root, BUCEPHALUS_GAME_3DFD_LOSING_SERIES)
        reply = play_series(completed.final_state, BUCEPHALUS_GAME_3DFD_REPLY_MATE)
        cache.store_found(completed.final_state, reply, proof_work=15_541)
        should_publish = False
    else:
        root, completed = _color_fixture(chess.WHITE)
        cache.store_exhausted(completed.final_state, proof_work=18)
        should_publish = True
    _force_completed_root(monkeypatch, completed)

    def unexpected_probe(*_args: object, **_kwargs: object) -> SeriesMateProbe:
        raise AssertionError("persistent exact proof should bypass native work")

    monkeypatch.setattr(series_mate, "find_native_series_mate", unexpected_probe)
    result = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
        ),
        mate_proof_cache=cache,
    ).run(root)

    assert (result.best_series == completed) is should_publish
    assert result.completed_depth == (1 if should_publish else 0)
    assert result.stats.final_fallback_reply_mate_probes == 0
    assert result.stats.final_fallback_reply_mate_cache_hits == 1
    assert result.stats.mate_proof_cache_hits == 1
    if cached_status == "found":
        assert result.stats.mate_proof_cache_found_hits == 1
        assert result.stats.mate_proof_cache_work_saved == 15_541
        assert result.stats.final_fallback_reply_mate_rejections == 1
    else:
        assert result.stats.mate_proof_cache_exhausted_hits == 1
        assert result.stats.mate_proof_cache_work_saved == 18
        assert result.stats.final_fallback_reply_mate_rejections == 0
