from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct
import threading

import pytest

import scottish_progressive.evaluation as evaluation
from scottish_progressive.fullgame_codec import (
    RejectReason,
    FullGameRecord,
    Terminal,
    decode_native_batch,
    replay_record,
    trace_sha256,
    unpack_move,
)


BASE_ARGS = (
    123,  # seed
    0,  # no artificial series watchdog
    8,  # complete-series frontier states
    20_000,  # work per series
    500_000,  # work per full game
    8,  # ranked candidates
    100,
    100,
    100,
    100,
    100,
)

V2_REQUEST_HEADER = struct.Struct("<8sHHI8QIIHHI6H32sI")
V2_PROFILE = struct.Struct("<32s5q")
V2_RESPONSE_HEADER = struct.Struct("<8sHHHHQQ32sIHHQ")
V2_RECORD_PREFIX = struct.Struct("<QQBBBBIIIQQQQ")
MAX_U64 = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class _V2Record:
    attempt_index: int
    terminal: int
    reject: int
    flags: int
    white_profile_index: int
    black_profile_index: int
    series: tuple[tuple[str, ...], ...]
    logical_work: int
    path_count_saturations: int


@dataclass(frozen=True, slots=True)
class _V2Batch:
    first_attempt: int
    attempt_count: int
    config_digest: bytes
    profile_count: int
    policy_kind: int
    schedule_kind: int
    total_path_count_saturations: int
    records: tuple[_V2Record, ...]


def _v2_request(
    first_attempt: int,
    attempt_count: int,
    *,
    profiles: tuple[tuple[int, int, int, int, int], ...] = (
        (100, 100, 100, 100, 100),
    ),
    policy_kind: int = 1,
    schedule_kind: int = 2,
    preserve_mate: bool = True,
    rank_mixture: tuple[int, int, int, int, int] = (0, 0, 0, 0, 0),
    seed: int = 20260820,
    max_positions_per_game: int = 5_000_000,
    config_digest: bytes | None = None,
) -> bytes:
    assert V2_REQUEST_HEADER.size == 144
    assert V2_PROFILE.size == 72
    top_weight, near_weight, tail_weight, top_count, near_count = rank_mixture
    digest = config_digest or hashlib.sha256(b"native-v2-test-config").digest()
    profile_payloads = []
    for index, weights in enumerate(profiles):
        profile_digest = hashlib.sha256(
            b"native-v2-test-profile" + bytes((index,)) + struct.pack("<5q", *weights)
        ).digest()
        profile_payloads.append(V2_PROFILE.pack(profile_digest, *weights))
    request_size = V2_REQUEST_HEADER.size + len(profile_payloads) * V2_PROFILE.size
    header = V2_REQUEST_HEADER.pack(
        b"SPCFGR02",
        2,
        V2_REQUEST_HEADER.size,
        0,
        request_size,
        first_attempt,
        attempt_count,
        seed,
        0,
        8,
        5_000,
        max_positions_per_game,
        8,
        len(profiles),
        policy_kind,
        schedule_kind,
        int(preserve_mate),
        top_weight,
        near_weight,
        tail_weight,
        top_count,
        near_count,
        0,
        digest,
        0,
    )
    return header + b"".join(profile_payloads)


def _decode_v2(payload: bytes) -> _V2Batch:
    assert V2_RESPONSE_HEADER.size == 80
    assert V2_RECORD_PREFIX.size == 64
    (
        magic,
        version,
        header_size,
        record_header_size,
        header_flags,
        first_attempt,
        attempt_count,
        config_digest,
        profile_count,
        policy_kind,
        schedule_kind,
        total_saturations,
    ) = V2_RESPONSE_HEADER.unpack_from(payload)
    assert magic == b"SPCFGB02"
    assert version == 2
    assert header_size == V2_RESPONSE_HEADER.size
    assert record_header_size == V2_RECORD_PREFIX.size
    assert header_flags == int(total_saturations > 0)

    records = []
    offset = header_size
    for expected_attempt in range(first_attempt, first_attempt + attempt_count):
        start = offset
        (
            record_size,
            attempt_index,
            status,
            terminal,
            reject,
            reserved,
            flags,
            white_profile,
            black_profile,
            series_count,
            move_count,
            logical_work,
            saturations,
        ) = V2_RECORD_PREFIX.unpack_from(payload, offset)
        assert attempt_index == expected_attempt
        assert reserved == 0
        assert status == int(reject != 0)
        assert flags == int(saturations > 0)
        offset += V2_RECORD_PREFIX.size
        ends = struct.unpack_from(f"<{series_count}Q", payload, offset)
        offset += series_count * 8
        words = struct.unpack_from(f"<{move_count}H", payload, offset)
        offset += move_count * 2
        assert offset - start == record_size
        series = []
        prior = 0
        for end in ends:
            series.append(tuple(unpack_move(word) for word in words[prior:end]))
            prior = end
        assert prior == len(words) if ends else not words
        if reject:
            assert terminal == 0
            assert not series
            assert not words
        records.append(
            _V2Record(
                attempt_index,
                terminal,
                reject,
                flags,
                white_profile,
                black_profile,
                tuple(series),
                logical_work,
                saturations,
            )
        )
    assert offset == len(payload)
    assert total_saturations == min(
        MAX_U64,
        sum(record.path_count_saturations for record in records),
    )
    return _V2Batch(
        first_attempt,
        attempt_count,
        config_digest,
        profile_count,
        policy_kind,
        schedule_kind,
        total_saturations,
        tuple(records),
    )


def _generate_v2(*args, **kwargs) -> _V2Batch:
    native = _native()
    if not hasattr(native, "generate_full_game_batch_v2"):
        pytest.skip("optional C++20 full-game v2 kernel is not built")
    return _decode_v2(native.generate_full_game_batch_v2(_v2_request(*args, **kwargs)))


def _native():
    native = evaluation._native_eval
    if native is None or not hasattr(native, "generate_full_game_batch"):
        pytest.skip("optional C++20 full-game kernel is not built")
    return native


def _generate(first_attempt: int, attempt_count: int, *args: int):
    return decode_native_batch(
        _native().generate_full_game_batch(
            first_attempt,
            attempt_count,
            *(args or BASE_ARGS),
        )
    )


def test_native_full_games_replay_from_s1_to_authoritative_terminal() -> None:
    batch = _generate(0, 10)

    assert not batch.rejected
    assert len(batch.accepted) == 10
    assert len({trace_sha256(record) for record in batch.accepted}) == 10
    assert any(
        len(move) == 5
        for record in batch.accepted
        for series in record.series
        for move in series
    )
    assert any(
        len(series) < series_number
        for record in batch.accepted
        for series_number, series in enumerate(record.series, start=1)
    )

    for record in batch.accepted:
        assert record.logical_work > 0
        assert record.series[0]
        assert len(record.series[0]) == 1
        assert all(
            1 <= len(series) <= series_number
            for series_number, series in enumerate(record.series, start=1)
        )
        evidence = replay_record(record)
        assert evidence.series_played == len(record.series)
        assert evidence.micro_moves_played == record.move_count
        assert evidence.terminal_reason == "checkmate"


def test_native_attempts_are_invariant_across_batch_partitions() -> None:
    whole = _generate(0, 6)
    partitioned = (
        *_generate(0, 2).records,
        *_generate(2, 4).records,
    )

    assert whole.records == partitioned


def test_native_rank_mixture_has_stable_top_mid_and_tail_lanes() -> None:
    batch = _generate(0, 13)
    records = {record.attempt_index: record for record in batch.accepted}

    # For seed 123 at series one, the frozen counter RNG puts attempt 0 in
    # the 80% best lane, attempt 12 in ranks 1-3, and attempt 2 in rank 4+.
    assert records[0].series[0] == ("d2d3",)
    assert records[12].series[0] == ("d2d4",)
    assert records[2].series[0] == ("a2a4",)


@pytest.mark.parametrize(
    ("first_attempt", "args", "reason"),
    [
        (
            7,
            (123, 3, 8, 20_000, 100_000, 8, 100, 100, 100, 100, 100),
            RejectReason.TECHNICAL_SERIES_WATCHDOG,
        ),
        (
            0,
            (123, 0, 8, 20_000, 1, 8, 100, 100, 100, 100, 100),
            RejectReason.WORK_LIMIT,
        ),
        (
            9,
            (999, 0, 8, 20_000, 500_000, 8, 300, 25, 25, 25, 25),
            RejectReason.MANUAL_PROOF_REQUIRED,
        ),
    ],
)
def test_native_incomplete_attempts_are_reason_only_without_wdl(
    first_attempt: int,
    args: tuple[int, ...],
    reason: RejectReason,
) -> None:
    batch = _generate(first_attempt, 1, *args)

    assert not batch.accepted
    assert batch.rejected[0].attempt_index == first_attempt
    assert batch.rejected[0].reason == reason
    assert batch.rejected[0].logical_work > 0


def test_native_direct_call_requires_a_finite_game_safety_mechanism() -> None:
    with pytest.raises(ValueError, match="full-game batch configuration"):
        _native().generate_full_game_batch(
            0,
            1,
            123,
            0,  # no series watchdog
            8,
            20_000,
            0,  # no whole-game work ceiling
            8,
            100,
            100,
            100,
            100,
            100,
        )


def test_native_batch_releases_the_gil_during_cpp_generation() -> None:
    ready = threading.Event()
    stop = threading.Event()
    progress = [0]

    def run_python() -> None:
        ready.set()
        while not stop.is_set():
            progress[0] += 1

    thread = threading.Thread(target=run_python)
    thread.start()
    assert ready.wait(timeout=1)
    before = progress[0]
    try:
        _native().generate_full_game_batch(0, 2, *BASE_ARGS)
    finally:
        after = progress[0]
        stop.set()
        thread.join(timeout=1)

    assert not thread.is_alive()
    assert after - before > 1_000


def test_native_games_and_logical_work_match_the_python_reference() -> None:
    from scottish_progressive.fullgame import (
        FullGameSemanticConfig,
        generate_native_batch,
        generate_reference_batch,
    )

    config = FullGameSemanticConfig.from_profile(
        seed=123,
        max_frontier_states=8,
        max_positions_per_series=20_000,
        max_positions_per_game=500_000,
        candidate_count=8,
    )

    assert generate_native_batch(config, 0, 3) == generate_reference_batch(
        config, 0, 3
    )


def test_native_manual_reject_and_work_match_the_python_reference() -> None:
    from scottish_progressive.fullgame import (
        FullGameSemanticConfig,
        generate_native_batch,
        generate_reference_batch,
    )
    from scottish_progressive.profiles import EngineProfile, EvaluationWeights

    profile = EngineProfile(
        name="manual-proof parity",
        weights=EvaluationWeights(
            material=300,
            king_space=25,
            series_reach=25,
            promotion_corridors=25,
            immediate_vulnerability=25,
            useful_mobility=25,
            boundary_check=25,
        ),
    )
    config = FullGameSemanticConfig.from_profile(
        profile,
        seed=999,
        max_frontier_states=8,
        max_positions_per_series=20_000,
        max_positions_per_game=500_000,
        candidate_count=8,
    )

    assert generate_native_batch(config, 9, 1) == generate_reference_batch(
        config, 9, 1
    )


def test_native_v1_packed_bytes_remain_golden() -> None:
    payload = _native().generate_full_game_batch(0, 2, *BASE_ARGS)

    assert len(payload) == 530
    assert hashlib.sha256(payload).hexdigest() == (
        "60b941ac5fd31f93655bdb391e35517f4ebb04f14f44bc4484f99ccfa60bb33b"
    )


def test_native_v2_uniform_profile_pairs_partition_and_replay() -> None:
    profiles = (
        (100, 100, 100, 100, 100),
        (300, 25, 25, 25, 25),
        (25, 300, 300, 300, 300),
    )
    whole = _generate_v2(0, 9, profiles=profiles)
    partitioned = (
        *_generate_v2(0, 4, profiles=profiles).records,
        *_generate_v2(4, 5, profiles=profiles).records,
    )

    assert whole.records == partitioned
    assert whole.policy_kind == 1
    assert whole.schedule_kind == 2
    assert whole.profile_count == 3
    assert [
        (record.white_profile_index, record.black_profile_index)
        for record in whole.records
    ] == [
        (0, 0),
        (1, 1),
        (2, 2),
        (0, 1),
        (1, 2),
        (2, 0),
        (0, 2),
        (1, 0),
        (2, 1),
    ]
    accepted = [record for record in whole.records if record.reject == 0]
    assert accepted
    for record in accepted:
        replay = replay_record(
            FullGameRecord(
                record.attempt_index,
                Terminal(record.terminal),
                record.series,
                record.logical_work,
            )
        )
        assert replay.terminal_reason in {"checkmate", "stalemate", "ten-series-draw"}


def test_native_v2_self_schedule_and_u64_attempt_are_global() -> None:
    profiles = (
        (100, 100, 100, 100, 100),
        (125, 100, 100, 100, 100),
        (150, 100, 100, 100, 100),
    )
    batch = _generate_v2(
        MAX_U64,
        1,
        profiles=profiles,
        schedule_kind=1,
    )

    expected = MAX_U64 % len(profiles)
    assert batch.records[0].white_profile_index == expected
    assert batch.records[0].black_profile_index == expected


def test_native_v2_saturates_unused_path_multiplicity_without_overflow_reject() -> None:
    batch = _generate_v2(
        1_496,
        1,
        policy_kind=2,
        rank_mixture=(5_000, 3_000, 2_000, 1, 3),
    )
    record = batch.records[0]

    assert record.reject != int(RejectReason.OVERFLOW)
    assert record.path_count_saturations > 0
    assert record.flags == 1
    assert batch.total_path_count_saturations == record.path_count_saturations
    if record.reject == 0:
        replay_record(
            FullGameRecord(
                record.attempt_index,
                Terminal(record.terminal),
                record.series,
                record.logical_work,
            )
        )


def test_native_v2_rejects_noncanonical_or_tampered_requests() -> None:
    native = _native()
    if not hasattr(native, "generate_full_game_batch_v2"):
        pytest.skip("optional C++20 full-game v2 kernel is not built")
    valid = _v2_request(0, 1)
    cases = []

    bad_magic = bytearray(valid)
    bad_magic[0] ^= 1
    cases.append(bytes(bad_magic))
    cases.extend((valid[:-1], valid + b"\0"))

    zero_config_digest = bytearray(valid)
    zero_config_digest[108:140] = bytes(32)
    cases.append(bytes(zero_config_digest))

    bad_reserved = bytearray(valid)
    struct.pack_into("<I", bad_reserved, 140, 1)
    cases.append(bytes(bad_reserved))

    bad_uniform_fields = bytearray(valid)
    struct.pack_into("<H", bad_uniform_fields, 96, 1)
    cases.append(bytes(bad_uniform_fields))

    bad_profile_weight = bytearray(valid)
    struct.pack_into("<q", bad_profile_weight, 176, 24)
    cases.append(bytes(bad_profile_weight))

    duplicate_profiles = bytearray(
        _v2_request(
            0,
            1,
            profiles=((100, 100, 100, 100, 100), (125, 100, 100, 100, 100)),
        )
    )
    duplicate_profiles[216:248] = duplicate_profiles[144:176]
    cases.append(bytes(duplicate_profiles))

    for request in cases:
        with pytest.raises(ValueError):
            native.generate_full_game_batch_v2(request)


def test_native_v2_batch_releases_the_gil() -> None:
    native = _native()
    if not hasattr(native, "generate_full_game_batch_v2"):
        pytest.skip("optional C++20 full-game v2 kernel is not built")
    ready = threading.Event()
    stop = threading.Event()
    progress = [0]

    def run_python() -> None:
        ready.set()
        while not stop.is_set():
            progress[0] += 1

    thread = threading.Thread(target=run_python)
    thread.start()
    assert ready.wait(timeout=1)
    before = progress[0]
    try:
        native.generate_full_game_batch_v2(_v2_request(0, 2))
    finally:
        after = progress[0]
        stop.set()
        thread.join(timeout=1)

    assert not thread.is_alive()
    assert after - before > 1_000
