from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable

import chess
import pytest

import scottish_progressive.search as search_module
import scottish_progressive.series_mate as series_mate_module
from scottish_progressive.mate_proof_cache import (
    MateProofCache,
    MateProofIdentity,
    MateProofStatus,
)
from scottish_progressive.model import Outcome, ProgressiveState
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.search import SearchLimits, SeriesSearcher
from scottish_progressive.series_mate import SeriesMateProbe, SeriesMateStatus
from scottish_progressive.webapp import PUBLIC_ANALYSIS_LIMITS, analyze_payload


IDENTITY = MateProofIdentity(
    engine_source="engine-source-a",
    ruleset="rules-a",
    quiet_draw_policy="quiet-a",
    native_mate="native-a",
)
LIVE_S4_HISTORY = (
    ("e2e4",),
    ("f7f6", "e8f7"),
    ("d2d4", "b1c3", "f1d3"),
)
LIVE_S4_BLUNDER = ("d7d5", "c8g4", "d5e4", "g4d1")


def _mate_state() -> ProgressiveState:
    return ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1",
        1,
    )


def _exhausted_state(file_index: int = 0) -> ProgressiveState:
    first_rank = ("K7", "1K6", "2K5")[file_index]
    return ProgressiveState.from_fen(
        f"7k/8/8/8/8/8/8/{first_rank} w - - 0 1",
        5,
    )


def _live_s5() -> ProgressiveState:
    state = ProgressiveState.initial()
    for series in LIVE_S4_HISTORY:
        state = play_series(state, series).final_state
    return play_series(state, LIVE_S4_BLUNDER).final_state


def _rewrite_document(
    path: Path,
    mutator: Callable[[dict[str, Any]], None],
) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    payload = {key: value for key, value in document.items() if key != "checksum"}
    mutator(payload)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    payload["checksum"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def test_found_is_replayed_and_invalid_witness_never_enters_cache() -> None:
    state = _mate_state()
    mate = play_series(state, ("g6g7",))
    nonmate = play_series(state, ("g6h6",))
    cache = MateProofCache(capacity=4, identity=IDENTITY)

    assert cache.store_found(state, nonmate, proof_work=9) == 0
    assert cache.snapshot().entries == 0
    assert cache.snapshot().replay_rejects == 1

    assert cache.store_found(state, mate, proof_work=123) == 0
    hit = cache.lookup(state)

    assert hit is not None
    assert hit.status is MateProofStatus.FOUND
    assert hit.series is not None
    assert hit.series.outcome is Outcome.CHECKMATE
    assert hit.series.ended_by_check
    assert hit.proof_work == 123
    assert cache.snapshot().work_saved == 123


def test_restart_reuses_found_and_authoritative_exhausted(tmp_path: Path) -> None:
    path = tmp_path / "mate-proofs.json"
    mate_state = _mate_state()
    exhausted_state = _exhausted_state()
    first = MateProofCache(path, capacity=4, identity=IDENTITY)
    first.store_found(
        mate_state,
        play_series(mate_state, ("g6g7",)),
        proof_work=41,
    )
    first.store_exhausted(exhausted_state, proof_work=73)

    restarted = MateProofCache(path, capacity=4, identity=IDENTITY)
    found = restarted.lookup(mate_state)
    exhausted = restarted.lookup(exhausted_state)

    assert found is not None and found.status is MateProofStatus.FOUND
    assert exhausted is not None and exhausted.status is MateProofStatus.EXHAUSTED
    assert exhausted.series is None
    assert restarted.snapshot().entries == 2
    assert restarted.snapshot().hits == 2
    assert restarted.snapshot().work_saved == 114
    assert not tuple(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("engine_source", "engine-source-b"),
        ("ruleset", "rules-b"),
        ("quiet_draw_policy", "quiet-b"),
        ("native_mate", "native-b"),
    ),
)
def test_identity_drift_rejects_the_entire_persisted_namespace(
    tmp_path: Path,
    field: str,
    changed: str,
) -> None:
    path = tmp_path / "mate-proofs.json"
    state = _exhausted_state()
    MateProofCache(path, identity=IDENTITY).store_exhausted(state, proof_work=5)
    values = IDENTITY.as_dict()
    values[field] = changed

    drifted = MateProofCache(path, identity=MateProofIdentity(**values))

    assert drifted.snapshot().entries == 0
    assert drifted.snapshot().identity_rejects == 1
    assert drifted.lookup(state) is None


@pytest.mark.parametrize(
    "status",
    ("unknown", "work-limit", "deadline", "unsupported"),
)
def test_resource_and_compatibility_statuses_cannot_be_loaded(
    tmp_path: Path,
    status: str,
) -> None:
    path = tmp_path / "mate-proofs.json"
    state = _exhausted_state()
    MateProofCache(path, identity=IDENTITY).store_exhausted(state, proof_work=5)
    _rewrite_document(
        path,
        lambda payload: payload["entries"][0].__setitem__("status", status),
    )

    restarted = MateProofCache(path, identity=IDENTITY)

    assert restarted.snapshot().entries == 0
    assert restarted.snapshot().load_failures == 1
    assert restarted.lookup(state) is None


def test_corrupted_checksum_and_replay_mismatched_found_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mate-proofs.json"
    state = _mate_state()
    cache = MateProofCache(path, identity=IDENTITY)
    cache.store_found(
        state,
        play_series(state, ("g6g7",)),
        proof_work=17,
    )
    corrupted = json.loads(path.read_text(encoding="utf-8"))
    corrupted["checksum"] = "0" * 64
    path.write_text(json.dumps(corrupted), encoding="utf-8")
    broken = MateProofCache(path, identity=IDENTITY)
    assert broken.snapshot().entries == 0
    assert broken.snapshot().load_failures == 1

    persistent = MateProofCache(tmp_path / "valid.json", identity=IDENTITY)
    persistent.store_found(
        state,
        play_series(state, ("g6g7",)),
        proof_work=17,
    )
    valid_path = tmp_path / "valid.json"
    _rewrite_document(
        valid_path,
        lambda payload: payload["entries"][0].__setitem__("moves", ["g6h6"]),
    )
    replay_mismatch = MateProofCache(valid_path, identity=IDENTITY)

    assert replay_mismatch.snapshot().entries == 1
    assert replay_mismatch.lookup(state) is None
    assert replay_mismatch.snapshot().entries == 0
    assert replay_mismatch.snapshot().replay_rejects == 1


def test_fifo_eviction_is_bounded_deterministic_and_hits_do_not_reorder() -> None:
    first, second, third = (_exhausted_state(index) for index in range(3))
    cache = MateProofCache(capacity=2, identity=IDENTITY)
    cache.store_exhausted(first, proof_work=1)
    cache.store_exhausted(second, proof_work=2)
    assert cache.lookup(first) is not None

    assert cache.store_exhausted(third, proof_work=3) == 1

    assert cache.lookup(first) is None
    assert cache.lookup(second) is not None
    assert cache.lookup(third) is not None
    assert cache.snapshot().entries == 2
    assert cache.snapshot().evictions == 1


def test_exact_pfen_disambiguates_adversarial_digest_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _exhausted_state(0)
    second = _exhausted_state(1)
    cache = MateProofCache(capacity=2, identity=IDENTITY)
    monkeypatch.setattr(cache, "_digest", lambda _pfen: "0" * 64)

    cache.store_exhausted(first, proof_work=1)
    cache.store_exhausted(second, proof_work=2)

    assert cache.snapshot().entries == 2
    assert cache.lookup(first) is not None
    assert cache.lookup(second) is not None


def test_promoted_piece_provenance_is_part_of_the_exact_child_identity() -> None:
    ordinary_board = chess.Board("7k/8/8/8/8/8/Q7/7K w - - 0 1")
    promoted_board = ordinary_board.copy(stack=False)
    promoted_board.promoted |= chess.BB_A2
    ordinary = ProgressiveState(ordinary_board, series_number=5)
    promoted = ProgressiveState(promoted_board, series_number=5)
    assert ordinary.pfen == promoted.pfen
    cache = MateProofCache(identity=IDENTITY)

    cache.store_exhausted(ordinary, proof_work=3)

    assert cache.lookup(ordinary) is not None
    assert cache.lookup(promoted) is None


def test_replayed_found_dominates_conflicting_negative_and_saved_work_is_conservative() -> None:
    state = _mate_state()
    mate = play_series(state, ("g6g7",))
    cache = MateProofCache(identity=IDENTITY)
    cache.store_exhausted(state, proof_work=100)
    cache.store_found(state, mate, proof_work=20)
    cache.store_exhausted(state, proof_work=200)
    cache.store_found(state, mate, proof_work=5)

    hit = cache.lookup(state)

    assert hit is not None and hit.status is MateProofStatus.FOUND
    assert hit.proof_work == 5

    negative_state = _exhausted_state()
    cache.store_exhausted(negative_state, proof_work=100)
    cache.store_exhausted(negative_state, proof_work=7)
    negative = cache.lookup(negative_state)
    assert negative is not None and negative.status is MateProofStatus.EXHAUSTED
    assert negative.proof_work == 7


@pytest.mark.parametrize(
    "status",
    (
        SeriesMateStatus.WORK_LIMIT,
        SeriesMateStatus.DEADLINE,
        SeriesMateStatus.UNSUPPORTED,
    ),
)
def test_unknown_native_outcomes_never_store_or_settle(
    monkeypatch: pytest.MonkeyPatch,
    status: SeriesMateStatus,
) -> None:
    state = _exhausted_state()
    cache = MateProofCache(identity=IDENTITY)
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_mate_screen_stage",
        lambda *_args, **_kwargs: (None, True),
    )
    monkeypatch.setattr(
        series_mate_module,
        "find_native_series_mate",
        lambda *_args, **_kwargs: SeriesMateProbe(status, "injected unknown"),
    )
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=1,
            max_series_per_node=32,
            collect_all_root_scores=False,
        ),
        baseline_profile(),
        mate_proof_cache=cache,
    )

    with pytest.raises((search_module._WorkLimit, search_module._Timeout)):
        searcher._root_child_immediate_mate(state)

    assert cache.snapshot().entries == 0
    assert cache.snapshot().stores == 0
    assert searcher.stats.mate_proof_cache_store_attempts == 0


def test_live_s5_found_restarts_into_zero_solver_work(tmp_path: Path) -> None:
    state = _live_s5()
    path = tmp_path / "live-s5-proofs.json"
    first_cache = MateProofCache(path)
    limits = SearchLimits(
        depth_series=2,
        max_series_per_node=32,
        max_generation_positions=10_000_000,
        time_limit_seconds=30.0,
        collect_all_root_scores=False,
        native_threads=1,
    )
    first = SeriesSearcher(
        limits,
        baseline_profile(),
        mate_proof_cache=first_cache,
    )
    first._deadline = time.perf_counter() + 30.0

    mate = first._root_child_immediate_mate(state)

    assert mate is not None and mate.outcome is Outcome.CHECKMATE
    assert first.stats.native_series_mate_calls == 1
    assert first.stats.native_series_mate_found == 1
    first_work = first.stats.work_positions
    assert first_work == 30_837

    restarted_cache = MateProofCache(path)
    second = SeriesSearcher(
        limits,
        baseline_profile(),
        mate_proof_cache=restarted_cache,
    )
    second._deadline = time.perf_counter() + 30.0
    cached_mate = second._root_child_immediate_mate(state)

    assert cached_mate is not None and cached_mate.moves == mate.moves
    assert second.stats.native_series_mate_calls == 0
    assert second.stats.root_safety_screen_stages == 0
    assert second.stats.work_positions == 0
    assert second.stats.mate_proof_cache_found_hits == 1
    assert second.stats.mate_proof_cache_work_saved == first_work


def test_exact_exhausted_restarts_without_selective_or_native_solver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _exhausted_state()
    path = tmp_path / "exhausted-proofs.json"
    cache = MateProofCache(path)
    monkeypatch.setattr(
        SeriesSearcher,
        "_root_child_mate_screen_stage",
        lambda *_args, **_kwargs: (None, True),
    )
    limits = SearchLimits(
        depth_series=1,
        max_series_per_node=32,
        collect_all_root_scores=False,
    )
    first = SeriesSearcher(limits, baseline_profile(), mate_proof_cache=cache)
    assert first._root_child_immediate_mate(state) is None
    assert first.stats.native_series_mate_calls == 1
    assert first.stats.native_series_mate_exhausted == 1

    restarted = MateProofCache(path)
    second = SeriesSearcher(limits, baseline_profile(), mate_proof_cache=restarted)
    assert second._root_child_immediate_mate(state) is None
    assert second.stats.native_series_mate_calls == 0
    assert second.stats.root_safety_screen_stages == 0
    assert second.stats.mate_proof_cache_exhausted_hits == 1
    assert state.transposition_key in second._root_child_native_mate_exhausted_keys


def test_independent_web_requests_reuse_opening_exhausted_proofs() -> None:
    cache = MateProofCache()
    payload = {
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "series": 1,
        "prefix": [],
        "depth": 2,
        "max_series": 32,
        "time_limit": 5,
        "max_generation_positions": 500_000,
        "alternatives": 0,
        "best_move_only": True,
    }

    first = analyze_payload(
        payload,
        request_limits=PUBLIC_ANALYSIS_LIMITS,
        mate_proof_cache=cache,
    )
    second = analyze_payload(
        payload,
        request_limits=PUBLIC_ANALYSIS_LIMITS,
        mate_proof_cache=cache,
    )

    assert first["best_series_uci"] == second["best_series_uci"] == "e2e3"
    assert first["completed_depth"] == second["completed_depth"] == 2
    assert first["stats"]["mate_proof_cache_hits"] == 0
    assert first["stats"]["mate_proof_cache_store_attempts"] == 1
    assert second["stats"]["mate_proof_cache_exhausted_hits"] == 1
    assert second["stats"]["mate_proof_cache_misses"] == 0
    assert second["stats"]["mate_proof_cache_work_saved"] == 527
    assert (
        first["stats"]["generation_positions"]
        - second["stats"]["generation_positions"]
        == second["stats"]["mate_proof_cache_work_saved"]
    )
