from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from scottish_progressive import search as search_module
from scottish_progressive import series_mate as series_mate_module
from scottish_progressive.mate_proof_cache import MateProofCache
from scottish_progressive.model import ProgressiveState
from scottish_progressive.rules import play_series
from scottish_progressive.search import (
    ScoredSeries,
    SearchLimits,
    SeriesSearcher,
)
from scottish_progressive.selected_pv_horizon import (
    BROWSER_CHECKED_PV_SELECTION_POLICY,
    MAX_SAME_ROOT_HORIZON_REPAIRS,
    SAME_ROOT_HORIZON_REPAIR_POLICY_SCHEMA,
    CandidateHorizonState,
    HorizonPolicyAction,
    SELECTED_PV_SELECTION_POLICY,
    SelectedPvHorizonStatus,
    certify_selected_pv_horizon,
    observe_horizon_proof,
)
from scottish_progressive.series_mate import (
    SeriesMateProbe,
    SeriesMateStatus,
    find_native_series_mate,
)


ACTUAL_SERIES_FIVE_ROOT = ProgressiveState.from_fen(
    "rnbqkbnr/1pp1pppp/8/P7/8/5P2/P2PPKPP/RNBq1BNR w kq - 0 7",
    5,
)
ACTUAL_SERIES_FIVE = play_series(
    ACTUAL_SERIES_FIVE_ROOT,
    ("a5a6", "a6b7", "b7c8q", "c8b8", "b8a7"),
)
ACTUAL_BLACK_MATE = play_series(
    ACTUAL_SERIES_FIVE.final_state,
    ("d8d3", "c7c5", "c5c4", "c4c3", "c3d2", "d1e1"),
)


def _probe(status: SeriesMateStatus, series=None) -> SeriesMateProbe:
    return SeriesMateProbe(status, status.value, series)


def test_internal_boundary_ladder_uses_a_distinct_v2_policy_identity() -> None:
    assert SELECTED_PV_SELECTION_POLICY == (
        "repair-once-then-veto-adverse-selected-pv-boundary-mates-v2"
    )
    assert SELECTED_PV_SELECTION_POLICY != BROWSER_CHECKED_PV_SELECTION_POLICY


def _browser_f3_proof_paths():
    paths = []
    for bishop_move in ("f1c4", "f1b5"):
        state = ProgressiveState.initial()
        rooted = []
        for moves in (
            ("f2f3",),
            ("e7e5", "f8b4"),
            ("a2a3", "a3b4", "e2e4"),
            ("a7a5", "d8g5", "g5g2", "g2h1"),
            ("d1e2", "e2c4", "c4c7", bishop_move, "c7c8"),
        ):
            result = play_series(state, moves)
            rooted.append(result)
            state = result.final_state
        mate = play_series(
            state,
            ("e8e7", "h1f3", "a5a4", "a4a3", "a3b2", "b2c1q"),
        )
        paths.append((tuple(rooted), mate))
    return tuple(paths)


def _browser_b3_selected_path():
    state = ProgressiveState.initial()
    rooted = []
    for moves, count in (
        (("b2b3",), 1),
        (("f7f5", "e8f7"), 1),
        (("c1b2", "e2e3", "f1c4"), 2),
        (("e7e6", "f5f4", "f4e3", "e3f2"), 1),
        (("e1e2", "e2f2", "d1g4", "f2e2", "g4h5"), 9),
    ):
        result = play_series(state, moves).with_transposition_count(count)
        rooted.append(result)
        state = result.final_state
    return tuple(rooted)


def _mined_internal_boundary_path():
    root = ProgressiveState.from_fen(
        "rnbqkbnr/1pp1pppp/8/p2p4/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 3",
        3,
    )
    state = root
    rooted = []
    for moves in (
        ("b2b4", "b4a5", "e1f2"),
        ("d5d4", "d4d3", "d3c2", "c2d1q"),
        ("a5a6", "a6b7", "b7c8q", "c8b8", "b8a7"),
        ("a8b8", "b8a8", "a8b8", "b8a8", "a8b8", "b8a8"),
        ("b1c3", "c3b1", "b1c3", "c3b1", "b1c3", "c3b1", "a2a3"),
    ):
        result = play_series(state, moves)
        rooted.append(result)
        state = result.final_state
    adverse_mate = play_series(
        rooted[2].final_state,
        ("a8a7", "d1c1", "c1b1", "b1a1", "d8d2", "a1e1"),
    )
    return root, tuple(rooted), adverse_mate


def _black_internal_boundary_path():
    root = ProgressiveState.from_fen(
        "1r4k1/5ppp/8/8/Q7/8/8/6K1 b - - 0 1",
        2,
    )
    state = root
    rooted = []
    for moves in (
        ("f7f5", "b8b1"),
        ("a4d1", "d1b1", "b1a1"),
        ("f5f4", "f4f3", "f3f2"),
        ("g1f1", "a1a3", "a3a1", "a1a3", "a3a1"),
        ("g7g5", "g5g4", "g4g3", "g8f7", "f7e6", "e6d5"),
    ):
        result = play_series(state, moves)
        rooted.append(result)
        state = result.final_state
    white_mate = play_series(
        rooted[0].final_state,
        ("g1f2", "a4e8"),
    )
    return root, tuple(rooted), white_mate


def test_synthetic_internal_opponent_boundary_ladder_stops_at_first_found() -> None:
    root, selected_pv, adverse_mate = _mined_internal_boundary_path()
    probed_series_numbers = []

    def probe(state):
        probed_series_numbers.append(state.series_number)
        if state.transposition_key == selected_pv[2].final_state.transposition_key:
            return _probe(SeriesMateStatus.FOUND, adverse_mate)
        return _probe(SeriesMateStatus.EXHAUSTED)

    certification = certify_selected_pv_horizon(root, selected_pv, probe)

    assert certification.status is SelectedPvHorizonStatus.FOUND
    assert certification.proof is not None
    assert certification.proof.rooted_path == selected_pv[:3]
    assert certification.proof.mate_reply == adverse_mate
    assert probed_series_numbers == [8, 6]


@pytest.mark.parametrize("depth", (4, 5))
def test_white_root_internal_mate_proof_has_black_score_sign_at_d4_d5(
    depth: int,
) -> None:
    root, selected_pv, adverse_mate = _mined_internal_boundary_path()
    internal_key = selected_pv[2].final_state.transposition_key

    certification = certify_selected_pv_horizon(
        root,
        selected_pv[:depth],
        lambda state: (
            _probe(SeriesMateStatus.FOUND, adverse_mate)
            if state.transposition_key == internal_key
            else _probe(SeriesMateStatus.EXHAUSTED)
        ),
    )

    assert certification.status is SelectedPvHorizonStatus.FOUND
    assert certification.proof is not None
    assert certification.proof.rooted_path == selected_pv[:3]
    assert certification.proof.proof_bounds == (-1, -1)


@pytest.mark.parametrize("depth", (4, 5))
def test_black_root_internal_mate_proof_has_white_score_sign_at_d4_d5(
    depth: int,
) -> None:
    root, selected_pv, white_mate = _black_internal_boundary_path()
    first_adverse_key = selected_pv[0].final_state.transposition_key

    certification = certify_selected_pv_horizon(
        root,
        selected_pv[:depth],
        lambda state: (
            _probe(SeriesMateStatus.FOUND, white_mate)
            if state.transposition_key == first_adverse_key
            else _probe(SeriesMateStatus.EXHAUSTED)
        ),
    )

    assert certification.status is SelectedPvHorizonStatus.FOUND
    assert certification.proof is not None
    assert certification.proof.rooted_path == selected_pv[:1]
    assert certification.proof.proof_bounds == (1, 1)


def test_terminal_final_pv_still_checks_an_earlier_adverse_boundary() -> None:
    root, selected_pv, adverse_mate = _mined_internal_boundary_path()
    terminal_final = play_series(
        selected_pv[3].final_state,
        ("a7c7", "a2a4", "a4a5", "a1a4", "a4b4", "b4b8", "c7c6"),
    )
    terminal_path = selected_pv[:4] + (terminal_final,)
    internal_key = selected_pv[2].final_state.transposition_key
    probed_series_numbers = []

    def probe(state):
        probed_series_numbers.append(state.series_number)
        if state.transposition_key == internal_key:
            return _probe(SeriesMateStatus.FOUND, adverse_mate)
        return _probe(SeriesMateStatus.EXHAUSTED)

    certification = certify_selected_pv_horizon(root, terminal_path, probe)

    assert terminal_final.outcome is not None
    assert terminal_final.ended_by_check is True
    assert certification.status is SelectedPvHorizonStatus.FOUND
    assert certification.proof is not None
    assert certification.proof.rooted_path == selected_pv[:3]
    assert probed_series_numbers == [6]


def test_internal_boundary_certification_reports_cumulative_probe_work() -> None:
    root, selected_pv, adverse_mate = _mined_internal_boundary_path()
    probes = iter(
        (
            SeriesMateProbe(
                SeriesMateStatus.EXHAUSTED,
                "safe final leaf",
                positions_visited=5,
                moves_generated=7,
            ),
            SeriesMateProbe(
                SeriesMateStatus.FOUND,
                "internal mate",
                adverse_mate,
                positions_visited=11,
                moves_generated=13,
            ),
        )
    )

    certification = certify_selected_pv_horizon(
        root,
        selected_pv,
        lambda _state: next(probes),
    )

    assert certification.status is SelectedPvHorizonStatus.FOUND
    assert certification.work_used == 36


def test_analyze_reuses_authoritative_exact_internal_boundary_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, selected_pv, _adverse_mate = _mined_internal_boundary_path()
    cache = MateProofCache(capacity=8)
    for index, proof_work in ((0, 10), (2, 20), (4, 30)):
        cache.store_exhausted(
            selected_pv[index].final_state,
            proof_work=proof_work,
        )
    searcher = SeriesSearcher(
        SearchLimits(depth_series=5, max_series_per_node=32),
        mate_proof_cache=cache,
    )
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: False,
    )

    def root_pass(
        _root,
        depth,
        _prefix,
        _mate_overrides,
        _horizon_overrides,
        _horizon_vetoes,
        _root_frontier_override,
    ):
        pv = selected_pv[:depth]
        candidate = ScoredSeries(pv[0], 100 + depth, pv[1:])
        return candidate.score, pv, (candidate,), None

    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("exact cached boundary must not rerun the mate solver")

    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(
        series_mate_module,
        "find_native_series_mate",
        unexpected_probe,
    )

    result = searcher.run(root)

    assert result.completed_depth == 5
    assert result.best_series == selected_pv[0]
    assert result.work_limit_reached is False
    assert result.timed_out is False
    assert result.stats.selected_pv_horizon_probe_calls == 0
    assert result.stats.mate_proof_cache_hits > 0


def test_internal_found_proof_is_reused_from_the_authoritative_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, selected_pv, adverse_mate = _mined_internal_boundary_path()
    cache = MateProofCache(capacity=8)
    cache.store_exhausted(selected_pv[4].final_state, proof_work=30)
    cache.store_found(
        selected_pv[2].final_state,
        adverse_mate,
        proof_work=20,
    )
    searcher = SeriesSearcher(
        SearchLimits(depth_series=5, max_series_per_node=32),
        mate_proof_cache=cache,
    )

    monkeypatch.setattr(
        series_mate_module,
        "find_native_series_mate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("persistent FOUND proof must not rerun the mate solver")
        ),
    )

    certification = searcher._certify_selected_pv_horizon(root, selected_pv)

    assert certification.status is SelectedPvHorizonStatus.FOUND
    assert certification.proof is not None
    assert certification.proof.rooted_path == selected_pv[:3]
    assert certification.proof.mate_reply == adverse_mate
    assert searcher.stats.selected_pv_horizon_probe_calls == 0
    assert searcher.stats.mate_proof_cache_hits == 2
    assert searcher.stats.mate_proof_cache_found_hits == 1
    assert searcher.stats.mate_proof_cache_exhausted_hits == 1
    assert searcher.stats.mate_proof_cache_work_saved == 50


def test_unknown_internal_probe_is_never_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, selected_pv, _adverse_mate = _mined_internal_boundary_path()
    boundary = selected_pv[2].final_state
    cache = MateProofCache(capacity=8)
    searcher = SeriesSearcher(
        SearchLimits(depth_series=5, max_series_per_node=32),
        mate_proof_cache=cache,
    )
    calls = 0

    def unknown_probe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SeriesMateProbe(
            SeriesMateStatus.WORK_LIMIT,
            "synthetic incomplete proof",
            positions_visited=7,
            moves_generated=11,
        )

    monkeypatch.setattr(
        series_mate_module,
        "find_native_series_mate",
        unknown_probe,
    )

    first = searcher._selected_pv_horizon_probe(boundary)
    second = searcher._selected_pv_horizon_probe(boundary)

    assert first.status is SeriesMateStatus.WORK_LIMIT
    assert second.status is SeriesMateStatus.WORK_LIMIT
    assert calls == 2
    assert searcher.stats.selected_pv_horizon_probe_calls == 2
    assert searcher.stats.mate_proof_cache_hits == 0
    assert cache.lookup(boundary) is None


def test_actual_quiet_series_five_leaf_is_replay_proven_adverse_mate() -> None:
    certification = certify_selected_pv_horizon(
        ACTUAL_SERIES_FIVE_ROOT,
        (ACTUAL_SERIES_FIVE,),
        lambda state: find_native_series_mate(
            state,
            max_positions=None,
            max_work=10_000,
        ),
    )

    assert ACTUAL_SERIES_FIVE.ended_by_check is False
    assert certification.status is SelectedPvHorizonStatus.FOUND
    assert certification.safe is False
    assert certification.proof is not None
    assert certification.proof.proof_bounds == (-1, -1)
    assert certification.proof.mate_reply == ACTUAL_BLACK_MATE
    assert certification.proof.mate_reply.machine_notation == (
        "d8d3/c7c5/c5c4/c4c3/c3d2/d1e1"
    )
    assert certification.proof.identity_sha256 == (
        certification.proof.recomputed_identity_sha256()
    )


@pytest.mark.parametrize(
    "root,path",
    [
        (
            ProgressiveState.initial(),
            lambda root: (play_series(root, ("e2e4",)),),
        ),
        (
            ProgressiveState.from_fen(
                "6k1/8/8/8/8/8/5PPP/1R4K1 w - - 0 1",
                1,
            ),
            lambda root: (play_series(root, ("b1b8",)),),
        ),
    ],
    ids=("quiet-leaf", "checked-leaf"),
)
def test_exact_exhaustion_certifies_benign_adverse_leaf(root, path) -> None:
    selected = path(root)
    certification = certify_selected_pv_horizon(
        root,
        selected,
        lambda _state: _probe(SeriesMateStatus.EXHAUSTED),
    )

    assert certification.status is SelectedPvHorizonStatus.EXHAUSTED
    assert certification.safe is True
    assert certification.proof is None


@pytest.mark.parametrize(
    "status",
    (
        SeriesMateStatus.WORK_LIMIT,
        SeriesMateStatus.DEADLINE,
        SeriesMateStatus.UNSUPPORTED,
    ),
)
def test_incomplete_native_status_is_unknown_and_never_safe(status) -> None:
    root = ProgressiveState.initial()
    selected = (play_series(root, ("e2e4",)),)

    certification = certify_selected_pv_horizon(
        root,
        selected,
        lambda _state: _probe(status),
    )

    assert certification.status is SelectedPvHorizonStatus.UNKNOWN
    assert certification.safe is False
    assert certification.probe_status is status


def test_noncanonical_selected_pv_fails_closed_before_probe() -> None:
    root = ProgressiveState.initial()
    selected = play_series(root, ("e2e4",))
    stale = replace(selected, ended_by_check=True)
    called = False

    def probe(_state):
        nonlocal called
        called = True
        return _probe(SeriesMateStatus.EXHAUSTED)

    certification = certify_selected_pv_horizon(root, (stale,), probe)

    assert certification.status is SelectedPvHorizonStatus.UNKNOWN
    assert certification.safe is False
    assert certification.replay_verified is False
    assert called is False


def test_first_proof_repairs_same_root_and_second_distinct_proof_vetoes() -> None:
    first_path, second_path = _browser_f3_proof_paths()
    first = certify_selected_pv_horizon(
        ProgressiveState.initial(),
        first_path[0],
        lambda _state: _probe(SeriesMateStatus.FOUND, first_path[1]),
    ).proof
    second = certify_selected_pv_horizon(
        ProgressiveState.initial(),
        second_path[0],
        lambda _state: _probe(SeriesMateStatus.FOUND, second_path[1]),
    ).proof
    assert first is not None and second is not None
    assert first.rooted_path[0].machine_notation == "f2f3"
    assert second.rooted_path[0].machine_notation == "f2f3"
    assert first.identity_sha256 != second.identity_sha256

    initial = CandidateHorizonState(candidate_series="f2f3")
    repair = observe_horizon_proof(initial, first)
    repaired = repair.next_state.record_successful_repair()
    veto = observe_horizon_proof(repaired, second)

    assert repair.action is HorizonPolicyAction.REPAIR
    assert repair.distinct_proofs_observed == 1
    assert repaired.successful_repairs == 1
    assert veto.action is HorizonPolicyAction.VETO
    assert veto.reason == "same-root-repair-limit"
    assert veto.distinct_proofs_observed == 2
    assert veto.retained_proofs_before_veto == 1


def test_shared_policy_constants_match_the_promoted_browser_receipt() -> None:
    repository = Path(__file__).parents[1]
    release_path = repository / "release" / "browser-wasm"
    receipt_path = (
        release_path
        / "evidence"
        / "opera-checked-pv-horizon-receipt.json"
    )
    # Keep this policy test bound to whichever release the promotion gate
    # actually signed instead of making every safe artifact refresh edit source
    # code solely to replace a literal receipt digest.
    release = json.loads((release_path / "release-receipt.json").read_bytes())
    checked_record = next(
        record
        for record in release["evidence_receipts"]
        if record["path"] == "evidence/opera-checked-pv-horizon-receipt.json"
    )
    raw = receipt_path.read_bytes()
    receipt = json.loads(raw)

    assert hashlib.sha256(raw).hexdigest() == checked_record["sha256"]
    assert receipt["schema"] == "spc-opera-checked-pv-horizon-receipt-v5"
    assert receipt["source_fingerprint"] == release["artifact"]["source_fingerprint"]
    assert receipt["wasm_sha256"] == release["artifact"]["wasm_sha256"]
    assert receipt["module_js_sha256"] == release["artifact"]["module_js_sha256"]
    assert receipt["best_full_series"] == ["b2b3"]
    assert receipt["selection_policy"] == BROWSER_CHECKED_PV_SELECTION_POLICY
    assert receipt["same_root_repair_policy"] == {
        "schema": SAME_ROOT_HORIZON_REPAIR_POLICY_SCHEMA,
        "maximum_successful_same_root_repairs": MAX_SAME_ROOT_HORIZON_REPAIRS,
    }
    assert (
        receipt["pv_horizon_line_rejections"],
        receipt["pv_horizon_native_repairs"],
        receipt["pv_horizon_candidate_vetoes"],
    ) == (2, 1, 1)
    assert receipt["pv_horizon_policy_vetoes"][0]["distinct_proofs_observed"] == 2


def test_root_selection_repairs_then_vetoes_initial_f3_and_publishes_b3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    first_path, second_path = _browser_f3_proof_paths()
    b3_path = _browser_b3_selected_path()
    first_candidate = ScoredSeries(first_path[0][0], 617, first_path[0][1:])
    repaired_candidate = ScoredSeries(second_path[0][0], 500, second_path[0][1:])
    safe_candidate = ScoredSeries(b3_path[0], 482, b3_path[1:])
    searcher = SeriesSearcher(
        SearchLimits(
            depth_series=5,
            max_series_per_node=32,
            max_generation_positions=100_000_000,
        )
    )
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: False,
    )

    def root_pass(
        _root,
        _depth,
        _prefix,
        _mate_overrides,
        horizon_overrides,
        horizon_vetoes,
        _root_frontier_override,
    ):
        if "f2f3" in horizon_vetoes:
            return (
                safe_candidate.score,
                (safe_candidate.series,) + safe_candidate.principal_variation,
                (safe_candidate,),
                None,
            )
        selected = horizon_overrides.get("f2f3", first_candidate)
        return (
            selected.score,
            (selected.series,) + selected.principal_variation,
            (selected, safe_candidate),
            None,
        )

    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(
        searcher,
        "_repair_selected_root",
        lambda _root, _candidate, _depth, _state: repaired_candidate,
    )
    def state_key(state):
        return (
            state.board.fen(en_passant="fen"),
            state.series_number,
            state.quiet_series,
            state.ep_targets,
        )

    replies = {
        state_key(first_path[0][-1].final_state): first_path[1],
        state_key(second_path[0][-1].final_state): second_path[1],
    }

    def probe(state):
        reply = replies.get(state_key(state))
        if reply is None:
            return _probe(SeriesMateStatus.EXHAUSTED)
        return _probe(SeriesMateStatus.FOUND, reply)

    monkeypatch.setattr(searcher, "_selected_pv_horizon_probe", probe)

    score, pv, _alternatives, _proof = searcher._search_root(root, 5, ())

    assert score == 482
    assert pv == b3_path
    assert pv[0].machine_notation == "b2b3"
    assert searcher.stats.selected_pv_horizon_line_rejections == 2
    assert searcher.stats.selected_pv_horizon_native_repairs == 1
    assert searcher.stats.selected_pv_horizon_candidate_vetoes == 1


def test_deadline_unknown_discards_current_depth_and_keeps_certified_prior_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    selected_path = _browser_f3_proof_paths()[0][0]
    searcher = SeriesSearcher(
        SearchLimits(depth_series=3, max_series_per_node=32)
    )
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: False,
    )

    def root_pass(
        _root,
        depth,
        _prefix,
        _mate_overrides,
        _horizon_overrides,
        _horizon_vetoes,
        _root_frontier_override,
    ):
        pv = selected_path[:depth]
        candidate = ScoredSeries(pv[0], 100 + depth, pv[1:])
        return candidate.score, pv, (candidate,), None

    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(
        searcher,
        "_selected_pv_horizon_probe",
        lambda state: (
            _probe(SeriesMateStatus.EXHAUSTED)
            if state.series_number == 2
            else _probe(SeriesMateStatus.DEADLINE)
        ),
    )

    result = searcher.run(root)

    assert result.completed_depth == 2
    assert result.requested_depth == 3
    assert result.timed_out is True
    assert result.best_series == selected_path[0]
    assert result.principal_variation == selected_path[:2]
    assert result.score == 102
    assert result.stats.selected_pv_horizon_exhausted == 2
    assert result.stats.selected_pv_horizon_unknown == 1


def test_all_horizon_vetoed_frontier_never_returns_the_vetoed_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    first_path, second_path = _browser_f3_proof_paths()
    first_candidate = ScoredSeries(first_path[0][0], 617, first_path[0][1:])
    repaired_candidate = ScoredSeries(second_path[0][0], 500, second_path[0][1:])
    searcher = SeriesSearcher(
        SearchLimits(depth_series=5, max_series_per_node=1)
    )
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: False,
    )

    def root_pass(
        _root,
        _depth,
        _prefix,
        _mate_overrides,
        horizon_overrides,
        horizon_vetoes,
        _root_frontier_override,
    ):
        if horizon_vetoes:
            return 0, (), (), None
        selected = horizon_overrides.get("f2f3", first_candidate)
        return (
            selected.score,
            (selected.series,) + selected.principal_variation,
            (selected,),
            None,
        )

    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(
        searcher,
        "_repair_selected_root",
        lambda _root, _candidate, _depth, _state: repaired_candidate,
    )
    replies = iter((first_path[1], second_path[1]))
    monkeypatch.setattr(
        searcher,
        "_selected_pv_horizon_probe",
        lambda _state: _probe(SeriesMateStatus.FOUND, next(replies)),
    )
    monkeypatch.setattr(
        searcher,
        "_selected_pv_horizon_widened_frontier",
        lambda *_args: search_module._GeneratedSeriesList(
            [],
            width_complete=False,
        ),
    )

    with pytest.raises(search_module._HorizonPolicyExhausted):
        searcher._search_root(root, 5, ())

    assert first_candidate.series.machine_notation in searcher._selected_pv_root_vetoes
    assert searcher.stats.selected_pv_horizon_candidate_vetoes == 1
    assert searcher.stats.selected_pv_horizon_all_vetoed_frontiers == 1
    assert searcher._root_scores_complete is False


def test_all_vetoed_retained_frontier_widens_and_certifies_b3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    first_path, second_path = _browser_f3_proof_paths()
    b3_path = _browser_b3_selected_path()
    first_candidate = ScoredSeries(first_path[0][0], 617, first_path[0][1:])
    repaired_candidate = ScoredSeries(second_path[0][0], 500, second_path[0][1:])
    b3_candidate = ScoredSeries(b3_path[0], 482, b3_path[1:])
    searcher = SeriesSearcher(
        SearchLimits(depth_series=5, max_series_per_node=1)
    )
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: False,
    )

    def root_pass(
        _root,
        _depth,
        _prefix,
        _mate_overrides,
        horizon_overrides,
        horizon_vetoes,
        root_frontier_override,
    ):
        if horizon_vetoes and root_frontier_override is None:
            return 0, (), (), None
        if root_frontier_override is not None:
            return (
                b3_candidate.score,
                (b3_candidate.series,) + b3_candidate.principal_variation,
                (b3_candidate,),
                None,
            )
        selected = horizon_overrides.get("f2f3", first_candidate)
        return (
            selected.score,
            (selected.series,) + selected.principal_variation,
            (selected,),
            None,
        )

    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(
        searcher,
        "_repair_selected_root",
        lambda _root, _candidate, _depth, _state: repaired_candidate,
    )
    monkeypatch.setattr(
        searcher,
        "_selected_pv_horizon_widened_frontier",
        lambda *_args: search_module._GeneratedSeriesList(
            [b3_candidate.series],
            width_complete=False,
        ),
    )
    mate_replies = {
        first_path[0][-1].final_state.transposition_key: first_path[1],
        second_path[0][-1].final_state.transposition_key: second_path[1],
    }

    def probe(state):
        reply = mate_replies.get(state.transposition_key)
        if reply is not None:
            return _probe(SeriesMateStatus.FOUND, reply)
        return _probe(SeriesMateStatus.EXHAUSTED)

    monkeypatch.setattr(searcher, "_selected_pv_horizon_probe", probe)

    score, pv, _alternatives, _proof = searcher._search_root(root, 5, ())

    assert score == 482
    assert pv == b3_path
    assert pv[0].machine_notation == "b2b3"
    assert "f2f3" in searcher._selected_pv_root_vetoes
    assert searcher.stats.selected_pv_horizon_candidate_vetoes == 1


def test_vetoed_root_seed_cannot_escape_through_interruption_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    first_path, second_path = _browser_f3_proof_paths()
    first_candidate = ScoredSeries(first_path[0][0], 617, first_path[0][1:])
    repaired_candidate = ScoredSeries(second_path[0][0], 500, second_path[0][1:])
    searcher = SeriesSearcher(
        SearchLimits(depth_series=5, max_series_per_node=1)
    )
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: False,
    )

    def root_pass(
        _root,
        _depth,
        _prefix,
        _mate_overrides,
        horizon_overrides,
        horizon_vetoes,
        _root_frontier_override,
    ):
        if horizon_vetoes:
            cause = search_module._WorkLimit()
            raise search_module._RootInterrupted(
                (),
                cause,
                first_candidate.series,
            ) from cause
        selected = horizon_overrides.get("f2f3", first_candidate)
        return (
            selected.score,
            (selected.series,) + selected.principal_variation,
            (selected,),
            None,
        )

    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(
        searcher,
        "_repair_selected_root",
        lambda _root, _candidate, _depth, _state: repaired_candidate,
    )
    replies = iter((first_path[1], second_path[1]))
    monkeypatch.setattr(
        searcher,
        "_selected_pv_horizon_probe",
        lambda _state: _probe(SeriesMateStatus.FOUND, next(replies)),
    )

    with pytest.raises(search_module._WorkLimit):
        searcher._search_root(root, 5, ())

    assert first_candidate.series.machine_notation in searcher._selected_pv_root_vetoes


def test_deeper_veto_discards_same_root_from_completed_prior_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    prior = _browser_f3_proof_paths()[0][0][0]
    searcher = SeriesSearcher(
        SearchLimits(depth_series=3, max_series_per_node=32)
    )

    def search_root(_root, depth, _prefix):
        if depth < 3:
            candidate = ScoredSeries(prior, 100 + depth)
            return candidate.score, (prior,), (candidate,), None
        searcher._selected_pv_root_vetoes.add(prior.machine_notation)
        searcher.stats.selected_pv_horizon_candidate_vetoes += 1
        raise search_module._Timeout

    monkeypatch.setattr(searcher, "_search_root", search_root)

    result = searcher.run(root)

    assert result.timed_out is True
    assert result.best_series is None
    assert result.principal_variation == ()
    assert result.completed_depth == 0
    assert result.proof is None
    assert result.stats.selected_pv_horizon_prior_depth_discards == 1


def test_fresh_depth_reconsiders_a_root_vetoed_only_at_an_earlier_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    selected_path = _browser_f3_proof_paths()[0][0]
    candidate = ScoredSeries(selected_path[0], 617, selected_path[1:])
    searcher = SeriesSearcher(
        SearchLimits(depth_series=5, max_series_per_node=32)
    )
    searcher._selected_pv_root_vetoes.add(candidate.series.machine_notation)
    received_veto_sets: list[frozenset[str]] = []
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: False,
    )

    def root_pass(
        _root,
        _depth,
        _prefix,
        _mate_overrides,
        _horizon_overrides,
        horizon_vetoes,
        _root_frontier_override,
    ):
        received_veto_sets.append(horizon_vetoes)
        return (
            candidate.score,
            (candidate.series,) + candidate.principal_variation,
            (candidate,),
            None,
        )

    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(
        searcher,
        "_selected_pv_horizon_probe",
        lambda _state: _probe(SeriesMateStatus.EXHAUSTED),
    )

    _score, pv, _alternatives, _proof = searcher._search_root(root, 5, ())

    assert received_veto_sets == [frozenset()]
    assert pv[0].machine_notation == "f2f3"
    assert searcher._selected_pv_root_vetoes == set()


def test_veto_a_then_deadline_on_b_returns_only_b_as_move_only_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    first_path, second_path = _browser_f3_proof_paths()
    b3_path = _browser_b3_selected_path()
    first_candidate = ScoredSeries(first_path[0][0], 617, first_path[0][1:])
    repaired_candidate = ScoredSeries(second_path[0][0], 500, second_path[0][1:])
    b3_candidate = ScoredSeries(b3_path[0], 482, b3_path[1:])
    searcher = SeriesSearcher(
        SearchLimits(depth_series=5, max_series_per_node=32)
    )
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: True,
    )

    def root_pass(
        _root,
        _depth,
        _prefix,
        _mate_overrides,
        horizon_overrides,
        horizon_vetoes,
        _root_frontier_override,
    ):
        if "f2f3" in horizon_vetoes:
            return (
                b3_candidate.score,
                (b3_candidate.series,) + b3_candidate.principal_variation,
                (b3_candidate,),
                None,
            )
        selected = horizon_overrides.get("f2f3", first_candidate)
        return (
            selected.score,
            (selected.series,) + selected.principal_variation,
            (selected, b3_candidate),
            None,
        )

    def exact_child_screen(state):
        searcher._mark_root_child_exact_exhausted(state.transposition_key)
        return None

    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(searcher, "_root_child_immediate_mate", exact_child_screen)
    monkeypatch.setattr(
        searcher,
        "_repair_selected_root",
        lambda _root, _candidate, _depth, _state: repaired_candidate,
    )
    replies = iter(
        (
            _probe(SeriesMateStatus.FOUND, first_path[1]),
            _probe(SeriesMateStatus.FOUND, second_path[1]),
            _probe(SeriesMateStatus.DEADLINE),
        )
    )
    monkeypatch.setattr(
        searcher,
        "_selected_pv_horizon_probe",
        lambda _state: next(replies),
    )

    result = searcher.run(root)

    assert searcher._selected_pv_root_vetoes == {"f2f3"}
    assert searcher.stats.selected_pv_horizon_candidate_vetoes == 1
    assert searcher.stats.selected_pv_horizon_unknown == 1
    assert result.timed_out is True
    assert result.completed_depth == 0
    assert result.best_series == b3_candidate.series
    assert result.best_series != first_candidate.series
    assert result.principal_variation == (b3_candidate.series,)
    assert result.score == result.root_evaluation.total
    assert result.alternatives == ()
    assert result.proof is None
    assert result.stats.selected_pv_horizon_move_only_fallbacks == 1


def test_deeper_veto_prefers_unvetoed_current_frontier_move_only_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    prior = _browser_f3_proof_paths()[0][0][0]
    fallback = _browser_b3_selected_path()[0]
    searcher = SeriesSearcher(
        SearchLimits(depth_series=2, max_series_per_node=32)
    )

    def search_root(_root, depth, _prefix):
        if depth == 1:
            candidate = ScoredSeries(prior, 101)
            return candidate.score, (prior,), (candidate,), None
        searcher._selected_pv_root_vetoes.clear()
        searcher._selected_pv_root_vetoes.add(prior.machine_notation)
        cause = search_module._Timeout()
        raise search_module._RootInterrupted((), cause, fallback) from cause

    monkeypatch.setattr(searcher, "_search_root", search_root)

    result = searcher.run(root)

    assert result.timed_out is True
    assert result.completed_depth == 0
    assert result.best_series == fallback
    assert result.principal_variation == (fallback,)
    assert result.score == result.root_evaluation.total
    assert result.alternatives == ()
    assert result.proof is None
    assert result.root_scores_complete is False
    assert result.stats.selected_pv_horizon_prior_depth_discards == 1
    assert result.stats.selected_pv_horizon_move_only_fallbacks == 1


def test_quiet_nonterminal_selected_leaf_uses_production_repair_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ACTUAL_SERIES_FIVE_ROOT
    raw = ScoredSeries(ACTUAL_SERIES_FIVE, 500)
    repaired = ScoredSeries(ACTUAL_SERIES_FIVE, -500)
    searcher = SeriesSearcher(
        SearchLimits(depth_series=1, max_series_per_node=32)
    )
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: False,
    )

    def root_pass(
        _root,
        _depth,
        _prefix,
        _mate_overrides,
        horizon_overrides,
        _horizon_vetoes,
        _root_frontier_override,
    ):
        selected = horizon_overrides.get(ACTUAL_SERIES_FIVE.machine_notation, raw)
        return selected.score, (selected.series,), (selected,), None

    seen_leaves = []
    probes = iter(
        (
            _probe(SeriesMateStatus.FOUND, ACTUAL_BLACK_MATE),
            _probe(SeriesMateStatus.EXHAUSTED),
        )
    )

    def probe(state):
        seen_leaves.append(state)
        return next(probes)

    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(searcher, "_selected_pv_horizon_probe", probe)
    monkeypatch.setattr(
        searcher,
        "_repair_selected_root",
        lambda _root, _candidate, _depth, _state: repaired,
    )

    score, pv, _alternatives, _proof = searcher._search_root(root, 1, ())

    assert ACTUAL_SERIES_FIVE.ended_by_check is False
    assert score == repaired.score
    assert pv == (ACTUAL_SERIES_FIVE,)
    assert seen_leaves == [
        ACTUAL_SERIES_FIVE.final_state,
        ACTUAL_SERIES_FIVE.final_state,
    ]
    assert searcher.stats.selected_pv_horizon_line_rejections == 1
    assert searcher.stats.selected_pv_horizon_native_repairs == 1


def test_nonterminal_all_mating_widening_rejoins_horizon_certification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    f3_path = _browser_f3_proof_paths()[0][0]
    b3_path = _browser_b3_selected_path()
    adverse = ScoredSeries(f3_path[0], -999, f3_path[1:])
    widened = ScoredSeries(b3_path[0], 482, b3_path[1:])
    searcher = SeriesSearcher(
        SearchLimits(depth_series=5, max_series_per_node=1)
    )
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: True,
    )

    def root_pass(
        _root,
        _depth,
        _prefix,
        mate_overrides,
        _horizon_overrides,
        _horizon_vetoes,
        _root_frontier_override,
    ):
        pv = (adverse.series,) + (
            (ACTUAL_BLACK_MATE,) if mate_overrides else adverse.principal_variation
        )
        selected = ScoredSeries(adverse.series, adverse.score, pv[1:])
        return selected.score, pv, (selected,), None

    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(
        searcher,
        "_root_child_immediate_mate",
        lambda _state: ACTUAL_BLACK_MATE,
    )
    monkeypatch.setattr(
        searcher,
        "_root_all_mating_widening",
        lambda *_args: (
            widened.score,
            (widened.series,) + widened.principal_variation,
            (widened,),
            None,
        ),
    )
    certified_leaves = []

    def certify_probe(state):
        certified_leaves.append(state)
        return _probe(SeriesMateStatus.EXHAUSTED)

    monkeypatch.setattr(searcher, "_selected_pv_horizon_probe", certify_probe)

    score, pv, _alternatives, _proof = searcher._search_root(root, 5, ())

    assert score == widened.score
    assert pv == b3_path
    assert certified_leaves == [
        b3_path[4].final_state,
        b3_path[2].final_state,
        b3_path[0].final_state,
    ]


@pytest.mark.parametrize(
    ("cause_type", "timed_out", "work_limit_reached"),
    (
        (search_module._Timeout, True, False),
        (search_module._WorkLimit, False, True),
    ),
)
def test_prior_a_cannot_survive_first_proof_repair_interruption(
    monkeypatch: pytest.MonkeyPatch,
    cause_type,
    timed_out: bool,
    work_limit_reached: bool,
) -> None:
    root = ProgressiveState.initial()
    f3_path, f3_mate = _browser_f3_proof_paths()[0]
    b3_path = _browser_b3_selected_path()
    prior_a = ScoredSeries(f3_path[0], 101)
    unsafe_a = ScoredSeries(f3_path[0], 617, f3_path[1:])
    fallback_b = ScoredSeries(b3_path[0], 482, b3_path[1:])
    searcher = SeriesSearcher(
        SearchLimits(depth_series=2, max_series_per_node=32)
    )
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: False,
    )

    def root_pass(
        _root,
        depth,
        _prefix,
        _mate_overrides,
        _horizon_overrides,
        _horizon_vetoes,
        _root_frontier_override,
    ):
        selected = prior_a if depth == 1 else unsafe_a
        alternatives = (selected,) if depth == 1 else (selected, fallback_b)
        return (
            selected.score,
            (selected.series,) + selected.principal_variation,
            alternatives,
            None,
        )

    probes = iter(
        (
            _probe(SeriesMateStatus.EXHAUSTED),
            _probe(SeriesMateStatus.FOUND, f3_mate),
        )
    )
    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(
        searcher,
        "_selected_pv_horizon_probe",
        lambda _state: next(probes),
    )
    def interrupt_repair(*_args):
        raise cause_type

    monkeypatch.setattr(searcher, "_repair_selected_root", interrupt_repair)

    result = searcher.run(root)

    assert result.timed_out is timed_out
    assert result.work_limit_reached is work_limit_reached
    assert result.completed_depth == 0
    assert result.best_series == fallback_b.series
    assert result.best_series != prior_a.series
    assert result.principal_variation == (fallback_b.series,)
    assert result.score == result.root_evaluation.total
    assert result.alternatives == ()
    assert result.proof is None
    assert searcher._selected_pv_root_vetoes == {"f2f3"}
    assert result.stats.selected_pv_horizon_line_rejections == 1
    assert result.stats.selected_pv_horizon_native_repairs == 0
    assert result.stats.selected_pv_horizon_repair_interruptions == 1
    assert result.stats.selected_pv_horizon_prior_depth_discards == 1
    assert result.stats.selected_pv_horizon_move_only_fallbacks == 1


def test_prior_a_cannot_survive_first_proof_repair_adjudication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ProgressiveState.initial()
    f3_path, f3_mate = _browser_f3_proof_paths()[0]
    b3_path = _browser_b3_selected_path()
    prior_a = ScoredSeries(f3_path[0], 101)
    unsafe_a = ScoredSeries(f3_path[0], 617, f3_path[1:])
    fallback_b = ScoredSeries(b3_path[0], 482, b3_path[1:])
    searcher = SeriesSearcher(
        SearchLimits(depth_series=2, max_series_per_node=32)
    )
    monkeypatch.setattr(
        searcher,
        "_root_child_safety_screen_required",
        lambda: False,
    )

    def root_pass(
        _root,
        depth,
        _prefix,
        _mate_overrides,
        _horizon_overrides,
        _horizon_vetoes,
        _root_frontier_override,
    ):
        selected = prior_a if depth == 1 else unsafe_a
        alternatives = (selected,) if depth == 1 else (selected, fallback_b)
        return (
            selected.score,
            (selected.series,) + selected.principal_variation,
            alternatives,
            None,
        )

    probes = iter(
        (
            _probe(SeriesMateStatus.EXHAUSTED),
            _probe(SeriesMateStatus.FOUND, f3_mate),
        )
    )
    monkeypatch.setattr(searcher, "_search_root_pass", root_pass)
    monkeypatch.setattr(
        searcher,
        "_selected_pv_horizon_probe",
        lambda _state: next(probes),
    )

    def pending_repair(*_args):
        raise search_module._AdjudicationPending

    monkeypatch.setattr(searcher, "_repair_selected_root", pending_repair)

    result = searcher.run(root)

    assert result.adjudication_status == "manual-proof-required"
    assert result.timed_out is False
    assert result.work_limit_reached is False
    assert result.completed_depth == 0
    assert result.best_series == fallback_b.series
    assert result.best_series != prior_a.series
    assert result.principal_variation == (fallback_b.series,)
    assert result.score == result.root_evaluation.total
    assert result.alternatives == ()
    assert result.proof is None
    assert searcher._selected_pv_root_vetoes == {"f2f3"}
    assert result.stats.selected_pv_horizon_line_rejections == 1
    assert result.stats.selected_pv_horizon_native_repairs == 0
    assert result.stats.selected_pv_horizon_repair_interruptions == 1
    assert result.stats.selected_pv_horizon_prior_depth_discards == 1
    assert result.stats.selected_pv_horizon_move_only_fallbacks == 1
