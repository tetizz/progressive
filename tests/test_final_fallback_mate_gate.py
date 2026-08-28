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


@pytest.mark.parametrize(
    ("pfen", "moves", "maximum_expected_work"),
    (
        (
            BUCEPHALUS_GAME_3AAEF_S5_PFEN,
            BUCEPHALUS_GAME_3AAEF_FALLBACK,
            1_000_000,
        ),
        (
            BUCEPHALUS_GAME_4044_S7_PFEN,
            BUCEPHALUS_GAME_4044_FALLBACK,
            1_000_000,
        ),
    ),
)
def test_exact_bucephalus_mating_fallbacks_never_publish(
    monkeypatch: pytest.MonkeyPatch,
    pfen: str,
    moves: tuple[str, ...],
    maximum_expected_work: int,
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
    assert result.stats.final_fallback_reply_mate_probes == 1
    assert result.stats.final_fallback_reply_mate_found == 1
    assert result.stats.final_fallback_reply_mate_rejections == 1
    assert 0 < result.stats.final_fallback_reply_mate_work <= maximum_expected_work
    assert result.stats.root_safety_screen_positions == 0


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
            max_series_per_node=32,
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
            max_series_per_node=32,
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

    probed: list[tuple[int, str, int, int]] = []
    fake_mate = SeriesResult(
        ("a1a1",),
        ("#",),
        first.final_state,
        ended_by_check=True,
        outcome=Outcome.CHECKMATE,
    )

    def found_probe(state: ProgressiveState, **_kwargs: object) -> SeriesMateProbe:
        probed.append(state.transposition_key)
        return SeriesMateProbe(
            SeriesMateStatus.FOUND,
            "injected mate",
            series=fake_mate,
        )

    monkeypatch.setattr(SeriesSearcher, "_search_root", partial)
    monkeypatch.setattr(series_mate, "find_native_series_mate", found_probe)
    result = analyze(
        root,
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            max_generation_positions=2_000_000,
            collect_all_root_scores=False,
        ),
    )

    assert probed == [first.final_state.transposition_key]
    assert result.best_series is None
    assert result.principal_variation == ()
    assert result.alternatives == ()
    assert result.stats.final_fallback_reply_mate_rejections == 1


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

    def unexpected_probe(*_args: object, **_kwargs: object) -> SeriesMateProbe:
        raise AssertionError("exact in-process proof should bypass native work")

    monkeypatch.setattr(series_mate, "find_native_series_mate", unexpected_probe)
    result = searcher.run(root)

    assert (result.best_series == completed) is should_publish
    assert result.completed_depth == (1 if should_publish else 0)
    assert result.stats.final_fallback_reply_mate_probes == 0
    assert result.stats.final_fallback_reply_mate_cache_hits == 1


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
