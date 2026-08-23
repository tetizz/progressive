from __future__ import annotations

from dataclasses import replace
import struct

import pytest

from scottish_progressive import evaluation
from scottish_progressive.model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    RULESET_VERSION,
    Outcome,
)
from scottish_progressive.native_corpus import (
    FULL_GAME_V2_RECORD_HEADER_SIZE,
    FULL_GAME_V2_RESPONSE_HEADER_SIZE,
    NativeCorpusConfig,
    NativeCorpusIdentityError,
    NativeCorpusProfile,
    NativeCorpusProtocolError,
    NativeCorpusReplayError,
    NativeCorpusUnavailable,
    NativeFullGameRecord,
    NativePolicyKind,
    NativeProfileSchedule,
    NativeRankPolicy,
    NativeReject,
    NativeTerminal,
    bind_native_profiles,
    decode_full_game_v2_response,
    encode_full_game_v2_request,
    generate_native_full_game_batch,
    replay_native_full_game,
    semantic_config_digest,
    unpack_native_move,
)
from scottish_progressive.profiles import baseline_profile, mutate_profile


RESPONSE_HEADER = struct.Struct("<8sHHHHQQ32sIHHQ")
RESPONSE_RECORD = struct.Struct("<QQBBBBIIIQQQQ")


def _pack_move(uci: str) -> int:
    files = "abcdefgh"
    from_square = files.index(uci[0]) + (int(uci[1]) - 1) * 8
    to_square = files.index(uci[2]) + (int(uci[3]) - 1) * 8
    promotions = {"n": 2, "b": 3, "r": 4, "q": 5}
    promotion = promotions.get(uci[4:], 0)
    return from_square | (to_square << 6) | (promotion << 12)


def _response(
    config: NativeCorpusConfig,
    profiles: tuple[object, ...],
    *,
    first_attempt: int = 0,
    corrupt_pair: bool = False,
) -> bytes:
    digest = semantic_config_digest(config, profiles)
    accepted_moves = (_pack_move("e2e4"),)
    accepted_size = FULL_GAME_V2_RECORD_HEADER_SIZE + 8 + 2
    white, black = (0, 1) if corrupt_pair else (0, 0)
    accepted = RESPONSE_RECORD.pack(
        accepted_size,
        first_attempt,
        0,
        int(NativeTerminal.STALEMATE),
        int(NativeReject.NONE),
        0,
        0,
        white,
        black,
        1,
        1,
        123,
        0,
    ) + struct.pack("<QH", 1, *accepted_moves)
    second_attempt = first_attempt + 1
    second_profile = second_attempt % len(profiles)
    rejected = RESPONSE_RECORD.pack(
        FULL_GAME_V2_RECORD_HEADER_SIZE,
        second_attempt,
        1,
        int(NativeTerminal.NONE),
        int(NativeReject.WORK_LIMIT),
        0,
        0,
        second_profile,
        second_profile,
        0,
        0,
        456,
        0,
    )
    return RESPONSE_HEADER.pack(
        b"SPCFGB02",
        2,
        FULL_GAME_V2_RESPONSE_HEADER_SIZE,
        FULL_GAME_V2_RECORD_HEADER_SIZE,
        0,
        first_attempt,
        2,
        digest,
        len(profiles),
        int(config.policy.kind),
        int(config.schedule),
        0,
    ) + accepted + rejected


@pytest.fixture
def profiles() -> tuple[object, ...]:
    baseline = baseline_profile()
    return baseline, mutate_profile(baseline, seed=17)


def test_semantic_digest_binds_configuration_and_not_attempt_range(
    profiles: tuple[object, ...],
) -> None:
    config = NativeCorpusConfig(seed=91)
    first = encode_full_game_v2_request(
        config, profiles, first_attempt=0, attempt_count=2
    )
    second = encode_full_game_v2_request(
        config, profiles, first_attempt=10_000, attempt_count=7
    )
    assert first[:8] == b"SPCFGR02"
    assert len(first) == 144 + 72 * len(profiles)
    assert first[108:140] == second[108:140] == semantic_config_digest(
        config, profiles
    )
    changed = semantic_config_digest(replace(config, seed=92), profiles)
    assert changed != semantic_config_digest(config, profiles)


def test_profile_binding_is_canonical_and_rejects_duplicates() -> None:
    profile = baseline_profile()
    first = NativeCorpusProfile.from_engine_profile(profile)
    second = NativeCorpusProfile.from_engine_profile(profile)
    assert first == second
    assert first.profile_id == profile.profile_id
    assert len(first.digest) == 32
    with pytest.raises(ValueError, match="must be unique"):
        bind_native_profiles((first, second))
    with pytest.raises(ValueError, match="canonical preimage"):
        replace(first, digest=b"x" * 32)


def test_rank_policy_validation_is_fail_closed() -> None:
    assert NativeRankPolicy.uniform().kind is NativePolicyKind.UNIFORM
    with pytest.raises(ValueError, match="total 10000"):
        NativeRankPolicy(top_weight_basis_points=1)
    with pytest.raises(ValueError, match="tail has weight"):
        NativeCorpusConfig(candidate_count=4)


def test_decoder_binds_every_header_record_and_schedule_field(
    profiles: tuple[object, ...],
) -> None:
    config = NativeCorpusConfig(seed=7)
    payload = _response(config, profiles)
    batch = decode_full_game_v2_response(
        payload,
        config=config,
        profiles=profiles,
        first_attempt=0,
        attempt_count=2,
    )
    assert batch.accepted_count == 1
    assert batch.rejected_count == 1
    assert batch.logical_work == 579
    assert batch.payload_size == len(payload)
    assert batch.records[0].series == (("e2e4",),)
    assert batch.records[1].reject is NativeReject.WORK_LIMIT


def test_decoder_rejects_trailing_bytes_and_wrong_profile_pair(
    profiles: tuple[object, ...],
) -> None:
    config = NativeCorpusConfig()
    kwargs = {
        "config": config,
        "profiles": profiles,
        "first_attempt": 0,
        "attempt_count": 2,
    }
    with pytest.raises(NativeCorpusProtocolError, match="trailing bytes"):
        decode_full_game_v2_response(_response(config, profiles) + b"x", **kwargs)
    with pytest.raises(NativeCorpusProtocolError, match="profile schedule"):
        decode_full_game_v2_response(
            _response(config, profiles, corrupt_pair=True), **kwargs
        )


def test_decoder_rejects_digest_drift(profiles: tuple[object, ...]) -> None:
    config = NativeCorpusConfig()
    payload = bytearray(_response(config, profiles))
    payload[32] ^= 1
    with pytest.raises(NativeCorpusProtocolError, match="semantic digest"):
        decode_full_game_v2_response(
            payload,
            config=config,
            profiles=profiles,
            first_attempt=0,
            attempt_count=2,
        )


def test_native_surface_is_exact_and_never_silently_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    profiles: tuple[object, ...],
) -> None:
    config = NativeCorpusConfig()
    expected_request = encode_full_game_v2_request(
        config, profiles, first_attempt=0, attempt_count=2
    )
    expected_response = _response(config, profiles)

    class FakeNative:
        def generate_full_game_batch_v2(self, request: bytes) -> bytes:
            assert request == expected_request
            return expected_response

    monkeypatch.setattr(evaluation, "_native_eval", FakeNative())
    batch = generate_native_full_game_batch(
        config, profiles, first_attempt=0, attempt_count=2
    )
    assert batch.accepted_count == 1
    monkeypatch.setattr(evaluation, "_native_eval", None)
    with pytest.raises(NativeCorpusUnavailable, match="source-matched"):
        generate_native_full_game_batch(
            config, profiles, first_attempt=0, attempt_count=2
        )


@pytest.mark.parametrize(
    ("field", "forged"),
    (
        ("engine_version", "forged-version"),
        ("engine_source_fingerprint", "0000000000000000"),
        ("ruleset_version", "forged-rules"),
    ),
)
def test_generation_rejects_forged_runtime_provenance_before_native_call(
    monkeypatch: pytest.MonkeyPatch,
    profiles: tuple[object, ...],
    field: str,
    forged: str,
) -> None:
    current = NativeCorpusConfig(
        engine_version=ENGINE_VERSION,
        engine_source_fingerprint=ENGINE_SOURCE_FINGERPRINT,
        ruleset_version=RULESET_VERSION,
    )
    config = replace(current, **{field: forged})

    class MustNotRun:
        def generate_full_game_batch_v2(self, request: bytes) -> bytes:
            raise AssertionError("native generation ran for forged provenance")

    monkeypatch.setattr(evaluation, "_native_eval", MustNotRun())
    with pytest.raises(NativeCorpusIdentityError, match=field):
        generate_native_full_game_batch(
            config, profiles, first_attempt=0, attempt_count=2
        )


def test_replay_validates_a_complete_standard_start_trace() -> None:
    record = NativeFullGameRecord(
        attempt_index=19,
        terminal=NativeTerminal.CHECKMATE_WHITE,
        reject=NativeReject.NONE,
        white_profile_index=0,
        black_profile_index=0,
        logical_work=1,
        path_count_saturations=0,
        series=(
            ("e2e4",),
            ("a7a6", "a6a5"),
            ("d1h5", "f1c4", "h5f7"),
        ),
    )
    replay = replay_native_full_game(record)
    assert replay.outcome is Outcome.CHECKMATE
    assert replay.winner is True
    assert replay.boundary_count == 4
    assert replay.states[-1].series_number == 4
    with pytest.raises(NativeCorpusReplayError, match="terminal disagrees"):
        replay_native_full_game(
            replace(record, terminal=NativeTerminal.CHECKMATE_BLACK)
        )


def test_packed_move_decoder_rejects_noncanonical_values() -> None:
    assert unpack_native_move(_pack_move("a7a8q")) == "a7a8q"
    with pytest.raises(NativeCorpusProtocolError, match="promotion code"):
        unpack_native_move(_pack_move("a7a8") | (1 << 12))
    with pytest.raises(NativeCorpusProtocolError, match="zero-length"):
        unpack_native_move(0)


@pytest.mark.skipif(
    evaluation._native_eval is None,
    reason="source-matched optional native extension is unavailable",
)
def test_real_native_v2_batch_is_deterministic_and_authoritatively_replayable() -> None:
    profile = baseline_profile()
    config = NativeCorpusConfig(
        seed=730_194_821,
        max_attempt_series=24,
        max_frontier_states=8,
        max_positions_per_series=100_000,
        max_positions_per_game=2_000_000,
        candidate_count=4,
        policy=NativeRankPolicy.uniform(),
    )
    request = encode_full_game_v2_request(
        config, (profile,), first_attempt=41, attempt_count=8
    )
    first = evaluation._native_eval.generate_full_game_batch_v2(request)
    second = evaluation._native_eval.generate_full_game_batch_v2(request)
    assert first == second
    batch = decode_full_game_v2_response(
        first,
        config=config,
        profiles=(profile,),
        first_attempt=41,
        attempt_count=8,
    )
    assert len(batch.records) == 8
    left = generate_native_full_game_batch(
        config, (profile,), first_attempt=41, attempt_count=3
    )
    right = generate_native_full_game_batch(
        config, (profile,), first_attempt=44, attempt_count=5
    )
    assert batch.records == left.records + right.records
    for record in batch.records:
        if record.accepted:
            replay_native_full_game(record)
