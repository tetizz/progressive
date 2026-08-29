from __future__ import annotations

import chess
import pytest

import scottish_progressive.rules as rules
import scottish_progressive.series_mate as series_mate
from scottish_progressive.model import Outcome, ProgressiveState
from scottish_progressive.rules import generate_series, play_series
from scottish_progressive.single_reply_mate_ladder import (
    SingleReplyMateLadderStatus,
    find_native_single_reply_mate_ladder,
)
from scottish_progressive.teacher_value_features import state_from_pfen


BUCEPHALUS_3DFD_S7_PFEN = (
    "Nnb1kbnr/pppp2pp/4p3/8/5q2/3P4/PPPKPP2/3R1BN1 w k - 1 13 "
    "| series=7 quiet=0 progressive_ep=- "
    "rules=scottish-modern-common-v1 quiet_draw=manual-proof-required"
)
BUCEPHALUS_ATTACK = (
    "d2c3",
    "d3d4",
    "d4d5",
    "d5e6",
    "d1d7",
    "a8c7",
)
BUCEPHALUS_FORCED_REPLY = ("f4c7",)
BUCEPHALUS_MATE = (
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

TINY_FOUND_FEN = "6Q1/6R1/4Q3/1q6/8/5K2/8/4r2k w - - 0 1"
TINY_ESCAPE_FEN = "5K2/7k/8/8/3Q4/8/1b2b2r/8 w - - 0 1"


def _require_native_ladder() -> object:
    native = series_mate._native_mate  # noqa: SLF001
    if native is None or not hasattr(native, "find_single_reply_mate_ladder"):
        pytest.skip("source-matched native ladder extension is unavailable")
    assert native.SOURCE_IDENTITY == series_mate._native_mate_source_identity()  # noqa: SLF001
    return native


def _python_brute_force_ladders(
    root: ProgressiveState,
) -> tuple[tuple[object, object, object], ...]:
    """Small-position oracle with no merging or native ladder assumptions."""

    found: list[tuple[object, object, object]] = []
    for attack in generate_series(root, merge_transpositions=False):
        if attack.outcome is not None or not attack.ended_by_check:
            continue
        first_variants = rules._legal_move_variants(  # noqa: SLF001
            attack.final_state.board,
            attack.final_state.ep_targets,
        )
        if len(first_variants) != 1:
            continue
        forced_reply = play_series(
            attack.final_state,
            (first_variants[0][0].uci(),),
        )
        if forced_reply.outcome is not None or not forced_reply.ended_by_check:
            continue
        for mate in generate_series(
            forced_reply.final_state,
            merge_transpositions=False,
        ):
            if mate.outcome is Outcome.CHECKMATE and mate.ended_by_check:
                found.append((attack, forced_reply, mate))
    return tuple(found)


def test_recorded_bucephalus_loss_is_found_and_replayed_exactly() -> None:
    _require_native_ladder()
    root = state_from_pfen(BUCEPHALUS_3DFD_S7_PFEN)

    probe = find_native_single_reply_mate_ladder(
        root,
        max_work=1_000_000,
        time_limit_seconds=30,
    )

    assert probe.status is SingleReplyMateLadderStatus.FOUND
    assert probe.proven_losing
    assert probe.proof is not None
    assert probe.proof.attack.moves == BUCEPHALUS_ATTACK
    assert probe.proof.forced_reply.moves == BUCEPHALUS_FORCED_REPLY
    assert probe.proof.mate.moves == BUCEPHALUS_MATE
    assert probe.proof.mate.outcome is Outcome.CHECKMATE
    assert probe.proof.identity_sha256 == probe.proof.recomputed_identity_sha256(root)
    assert probe.work_used == 628_052
    assert probe.stats.forced_counterchecks == 1
    assert probe.stats.mate_probes == 1


@pytest.mark.parametrize(
    ("fen", "expect_found", "expected_path"),
    (
        (
            TINY_FOUND_FEN,
            True,
            (("e6h6",), ("b5h5",), ("h6h5",)),
        ),
        (TINY_ESCAPE_FEN, False, None),
    ),
)
def test_tiny_native_result_matches_unmerged_python_brute_force(
    fen: str,
    expect_found: bool,
    expected_path: tuple[tuple[str, ...], ...] | None,
) -> None:
    _require_native_ladder()
    root = ProgressiveState.from_fen(fen, 1)
    oracle = _python_brute_force_ladders(root)

    probe = find_native_single_reply_mate_ladder(
        root,
        max_work=100_000,
        time_limit_seconds=5,
    )

    assert bool(oracle) is expect_found
    assert probe.proven_losing is expect_found
    assert probe.status is (
        SingleReplyMateLadderStatus.FOUND
        if expect_found
        else SingleReplyMateLadderStatus.EXHAUSTED
    )
    if expected_path is not None:
        assert probe.proof is not None
        assert (
            probe.proof.attack.moves,
            probe.proof.forced_reply.moves,
            probe.proof.mate.moves,
        ) == expected_path
    else:
        assert probe.proof is None
        # This fixture enters the forced-countercheck lane, but its exact
        # immediate mate probe exhausts, demonstrating a genuine escape.
        assert probe.stats.forced_counterchecks == 1
        assert probe.stats.mate_probes == 1


def test_ladder_proof_is_color_symmetric() -> None:
    _require_native_ladder()
    mirrored = chess.Board(TINY_FOUND_FEN).mirror()
    root = ProgressiveState(mirrored, series_number=2)

    probe = find_native_single_reply_mate_ladder(
        root,
        max_work=100_000,
        time_limit_seconds=5,
    )

    assert probe.proven_losing
    assert probe.proof is not None
    assert probe.proof.attack.moves == ("e3h3",)
    assert probe.proof.forced_reply.moves == ("b4h4",)
    assert probe.proof.mate.moves == ("h3h4",)
    assert probe.proof.mate.final_state.board.turn == chess.WHITE


def test_work_limit_and_deadline_are_unknown_and_cannot_veto() -> None:
    _require_native_ladder()
    root = state_from_pfen(BUCEPHALUS_3DFD_S7_PFEN)

    work_limited = find_native_single_reply_mate_ladder(
        root,
        max_work=1,
        time_limit_seconds=30,
    )
    deadline = find_native_single_reply_mate_ladder(
        root,
        max_work=1_000_000,
        time_limit_seconds=0,
    )

    assert work_limited.status is SingleReplyMateLadderStatus.UNKNOWN
    assert work_limited.native_status is series_mate.SeriesMateStatus.WORK_LIMIT
    assert work_limited.work_used == 1
    assert not work_limited.proven_losing
    assert work_limited.proof is None
    assert deadline.status is SingleReplyMateLadderStatus.UNKNOWN
    assert deadline.native_status is series_mate.SeriesMateStatus.DEADLINE
    assert deadline.work_used == 0
    assert not deadline.proven_losing
    assert deadline.proof is None


def test_nested_mate_work_limit_is_combined_and_cannot_veto() -> None:
    _require_native_ladder()
    root = ProgressiveState.from_fen(TINY_FOUND_FEN, 1)

    probe = find_native_single_reply_mate_ladder(
        root,
        max_work=36,
        time_limit_seconds=5,
    )

    assert probe.status is SingleReplyMateLadderStatus.UNKNOWN
    assert probe.native_status is series_mate.SeriesMateStatus.WORK_LIMIT
    assert probe.stats.mate_probes == 1
    assert probe.stats.mate_positions_visited == 1
    assert probe.stats.mate_moves_generated == 4
    assert probe.work_used == 36
    assert not probe.proven_losing
    assert probe.proof is None


def test_proof_identity_binds_quiet_clock_and_promoted_full_state() -> None:
    _require_native_ladder()
    ordinary = ProgressiveState.from_fen(TINY_FOUND_FEN, 1)
    quiet = ProgressiveState(chess.Board(TINY_FOUND_FEN), 1, quiet_series=4)
    clock_board = chess.Board(TINY_FOUND_FEN)
    clock_board.halfmove_clock = 17
    clock_board.fullmove_number = 23
    clocked = ProgressiveState(clock_board, 1)
    promoted_board = chess.Board(TINY_FOUND_FEN)
    promoted_board.promoted |= chess.BB_G8
    promoted = ProgressiveState(promoted_board, 1)

    probes = tuple(
        find_native_single_reply_mate_ladder(
            state,
            max_work=100_000,
            time_limit_seconds=5,
        )
        for state in (ordinary, quiet, clocked, promoted)
    )

    assert all(probe.proven_losing and probe.proof is not None for probe in probes)
    root_identities = {probe.proof.root_identity_sha256 for probe in probes if probe.proof}
    proof_identities = {probe.proof.identity_sha256 for probe in probes if probe.proof}
    assert len(root_identities) == 4
    assert len(proof_identities) == 4


def test_unavailable_native_is_unknown_not_a_negative_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(series_mate, "_native_mate", None)
    probe = find_native_single_reply_mate_ladder(
        ProgressiveState.from_fen(TINY_FOUND_FEN, 1)
    )

    assert probe.status is SingleReplyMateLadderStatus.UNKNOWN
    assert probe.native_status is series_mate.SeriesMateStatus.UNSUPPORTED
    assert not probe.proven_losing
    assert probe.proof is None


def test_native_found_must_survive_authoritative_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForgedNative:
        SOURCE_IDENTITY = series_mate._native_mate_source_identity()  # noqa: SLF001

        @staticmethod
        def find_single_reply_mate_ladder(*_arguments: object) -> tuple[object, ...]:
            return (
                0,
                "forged",
                (0,) * 14,
                ("e6h6",),
                ("b5h5",),
                ("h6h4",),
            )

    monkeypatch.setattr(series_mate, "_native_mate", ForgedNative())

    with pytest.raises(RuntimeError, match="failed authoritative replay"):
        find_native_single_reply_mate_ladder(
            ProgressiveState.from_fen(TINY_FOUND_FEN, 1)
        )


@pytest.mark.parametrize("value", (0, -1, True, 1.5, 1 << 64))
def test_invalid_or_oversized_work_is_rejected_or_unknown(value: object) -> None:
    root = ProgressiveState.from_fen(TINY_FOUND_FEN, 1)
    if value == 1 << 64:
        probe = find_native_single_reply_mate_ladder(root, max_work=value)
        assert probe.status is SingleReplyMateLadderStatus.UNKNOWN
        assert probe.native_status is series_mate.SeriesMateStatus.UNSUPPORTED
    else:
        with pytest.raises(ValueError, match="max_work"):
            find_native_single_reply_mate_ladder(root, max_work=value)  # type: ignore[arg-type]
