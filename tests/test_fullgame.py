from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import struct
import time

import pytest

import scottish_progressive.evaluation as evaluation
import scottish_progressive.fullgame as fullgame_module
import scottish_progressive.fullgame_codec as codec
import scottish_progressive.fullgame_identity as fullgame_identity_module
from scottish_progressive.fullgame_identity import (
    FULLGAME_SEMANTIC_FINGERPRINT,
    FULLGAME_SEMANTIC_SOURCE_FILES,
    fullgame_semantic_fingerprint,
)
from scottish_progressive.cli import main
from scottish_progressive.fullgame import (
    DATA_PURPOSE,
    DEFAULT_CANDIDATE_COUNT,
    DEFAULT_FRONTIER_STATES,
    DEFAULT_POSITIONS_PER_GAME,
    DEFAULT_POSITIONS_PER_SERIES,
    PROFILE_SCHEDULE_ORDERED_V2,
    STRENGTH_CLAIM,
    UNIFORM_POLICY_ID,
    FullGameSemanticConfig,
    FullGameStore,
    deliver_committed_fullgame_chunks,
    export_fullgame_jsonl,
    fullgame_status,
    generate_native_batch_v2,
    run_fullgame_generation,
    verify_fullgame_run,
)
from scottish_progressive.fullgame_codec import (
    FullGameRecord,
    NativeV2Profile,
    NATIVE_V2_POLICY_UNIFORM,
    NATIVE_V2_RECORD_PREFIX,
    NATIVE_V2_REQUEST_HEADER,
    NATIVE_V2_RESPONSE_HEADER,
    NATIVE_V2_RESPONSE_MAGIC,
    NATIVE_V2_SCHEDULE_ORDERED_PAIR_ROUND_ROBIN,
    NATIVE_V2_VERSION,
    RejectReason,
    RejectedAttempt,
    Terminal,
    decode_chunk,
    decode_native_batch,
    decode_native_batch_v2,
    decode_record,
    encode_chunk,
    encode_native_v2_request,
    encode_record,
    pack_move,
    replay_record,
    trace_sha256,
    unpack_move,
)
from scottish_progressive.profiles import (
    EngineProfile,
    EvaluationWeights,
    baseline_profile,
    save_profile,
)


def test_fullgame_semantic_fingerprint_ignores_unrelated_product_code(
    tmp_path: Path,
) -> None:
    for name in FULLGAME_SEMANTIC_SOURCE_FILES:
        (tmp_path / name).write_bytes(f"{name}:semantic-v1".encode("ascii"))
    baseline = fullgame_semantic_fingerprint(tmp_path)

    for unrelated in ("tournament.py", "webapp.py", "neural_evaluator.py"):
        path = tmp_path / unrelated
        path.write_bytes(b"unrelated-v1")
        assert fullgame_semantic_fingerprint(tmp_path) == baseline
        path.write_bytes(b"unrelated-v2")
        assert fullgame_semantic_fingerprint(tmp_path) == baseline


def test_fullgame_semantic_fingerprint_fails_closed_on_contract_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in FULLGAME_SEMANTIC_SOURCE_FILES:
        (tmp_path / name).write_bytes(f"{name}:semantic-v1".encode("ascii"))
    baseline = fullgame_semantic_fingerprint(tmp_path)

    for name in FULLGAME_SEMANTIC_SOURCE_FILES:
        path = tmp_path / name
        original = path.read_bytes()
        path.write_bytes(original + b"-drift")
        assert fullgame_semantic_fingerprint(tmp_path) != baseline
        path.write_bytes(original)

    monkeypatch.setattr(
        fullgame_identity_module.chess,
        "__version__",
        "semantic-drift-test",
    )
    assert fullgame_semantic_fingerprint(tmp_path) != baseline

    (tmp_path / FULLGAME_SEMANTIC_SOURCE_FILES[0]).unlink()
    with pytest.raises(FileNotFoundError):
        fullgame_semantic_fingerprint(tmp_path)


def test_fullgame_config_uses_narrow_semantic_fingerprint() -> None:
    config = FullGameSemanticConfig.from_profile(backend_kind="native")

    assert config.source_fingerprint == FULLGAME_SEMANTIC_FINGERPRINT
    assert config.backend_source_identity == evaluation._native_source_identity()
    assert len(bytes.fromhex(config.source_fingerprint)) == 32
    with pytest.raises(ValueError, match="semantic fingerprint is stale"):
        replace(config, source_fingerprint="0" * 64)


WHITE_MATE = (
    ("e2e4",),
    ("g8f6", "f6d5"),
    ("g1h3", "g2g3", "d2d4"),
    ("a7a5", "a5a4", "b7b5", "b8c6"),
    ("a2a3", "d1h5", "h3g5", "g5f7", "f7d6"),
)
BLACK_MATE = (
    ("b2b4",),
    ("g7g6", "f8g7"),
    ("d2d3", "b1d2", "c1a3"),
    ("e7e6", "d8h4", "g7d4", "d4f2"),
)


def _reference_config() -> FullGameSemanticConfig:
    return FullGameSemanticConfig.from_profile(backend_kind="reference")


def _outcome(attempt: int) -> FullGameRecord | RejectedAttempt:
    if attempt == 0:
        return FullGameRecord(attempt, Terminal.CHECKMATE_WHITE, WHITE_MATE, 10)
    if attempt == 1:
        return RejectedAttempt(attempt, RejectReason.WORK_LIMIT, 3)
    if attempt == 2:
        return FullGameRecord(attempt, Terminal.CHECKMATE_WHITE, WHITE_MATE, 12)
    if attempt == 3:
        return FullGameRecord(attempt, Terminal.CHECKMATE_BLACK, BLACK_MATE, 20)
    return RejectedAttempt(attempt, RejectReason.MANUAL_PROOF_REQUIRED, 5)


def _generator(
    _config: FullGameSemanticConfig,
    first_attempt: int,
    attempt_count: int,
) -> tuple[FullGameRecord | RejectedAttempt, ...]:
    return tuple(
        _outcome(attempt)
        for attempt in range(first_attempt, first_attempt + attempt_count)
    )


def _second_profile() -> EngineProfile:
    return EngineProfile(
        name="v2 pool profile",
        weights=EvaluationWeights(material=125, king_space=75),
    )


def _rejected_v2_response(
    request: bytes,
    *,
    tamper_digest: bool = False,
    tamper_pair: bool = False,
    tamper_total_saturations: bool = False,
) -> bytes:
    fields = NATIVE_V2_REQUEST_HEADER.unpack_from(request)
    first_attempt = fields[5]
    attempt_count = fields[6]
    config_digest = bytes(32) if tamper_digest else fields[23]
    profile_count = fields[13]
    policy_kind = fields[14]
    schedule_kind = fields[15]
    records = bytearray()
    total_saturations = 0
    for offset in range(attempt_count):
        attempt = first_attempt + offset
        if schedule_kind == NATIVE_V2_SCHEDULE_ORDERED_PAIR_ROUND_ROBIN:
            white = attempt % profile_count
            black = (white + (attempt // profile_count) % profile_count) % profile_count
        else:
            white = black = attempt % profile_count
        if tamper_pair and offset == 0:
            black = (black + 1) % profile_count
        saturation = offset + 1
        total_saturations = min(codec.MAX_U64, total_saturations + saturation)
        records.extend(
            NATIVE_V2_RECORD_PREFIX.pack(
                NATIVE_V2_RECORD_PREFIX.size,
                attempt,
                1,
                0,
                int(RejectReason.WORK_LIMIT),
                0,
                1,
                white,
                black,
                0,
                0,
                7 + offset,
                saturation,
            )
        )
    if tamper_total_saturations:
        total_saturations += 1
    return NATIVE_V2_RESPONSE_HEADER.pack(
        NATIVE_V2_RESPONSE_MAGIC,
        NATIVE_V2_VERSION,
        NATIVE_V2_RESPONSE_HEADER.size,
        NATIVE_V2_RECORD_PREFIX.size,
        1,
        first_attempt,
        attempt_count,
        config_digest,
        profile_count,
        policy_kind,
        schedule_kind,
        total_saturations,
    ) + bytes(records)


def _run_two_games(
    root: Path,
    *,
    workers: int = 1,
    attempts_per_chunk: int = 2,
) -> dict[str, object]:
    return run_fullgame_generation(
        root,
        _reference_config(),
        target_unique_games=2,
        attempts_per_chunk=attempts_per_chunk,
        backend="reference",
        requested_workers=workers,
        generator=_generator,
    )


def test_known_traces_replay_from_exact_s1_to_checkmate() -> None:
    white = FullGameRecord(0, Terminal.CHECKMATE_WHITE, WHITE_MATE, 101)
    black = FullGameRecord(1, Terminal.CHECKMATE_BLACK, BLACK_MATE, 202)

    white_evidence = replay_record(white)
    black_evidence = replay_record(black)

    assert white_evidence.result == "1-0"
    assert black_evidence.result == "0-1"
    assert white_evidence.series_played == 5
    assert black_evidence.series_played == 4
    assert white_evidence.micro_moves_played == 15
    assert black_evidence.micro_moves_played == 10


def test_record_codec_is_canonical_and_trace_identity_ignores_provenance() -> None:
    record = FullGameRecord(7, Terminal.CHECKMATE_WHITE, WHITE_MATE, 9876)

    assert decode_record(encode_record(record)) == record
    assert trace_sha256(record) == trace_sha256(
        replace(record, attempt_index=8, logical_work=123)
    )
    differently_bounded = FullGameRecord(
        7,
        Terminal.CHECKMATE_WHITE,
        (("e2e4",), ("e7e5", "g1f3")),
    )
    same_flat_moves = FullGameRecord(
        7,
        Terminal.CHECKMATE_WHITE,
        (("e2e4", "e7e5"), ("g1f3",)),
    )
    assert trace_sha256(differently_bounded) != trace_sha256(same_flat_moves)

    encoded = encode_record(record)
    prefix = codec.FINAL_RECORD_PREFIX.size
    overlong = encoded[:prefix] + b"\x85\x00" + encoded[prefix + 1 :]
    with pytest.raises(ValueError, match="minimally encoded"):
        decode_record(overlong)


def test_packed_move_codec_rejects_null_reserved_and_invalid_promotion() -> None:
    assert unpack_move(pack_move("a7a8q")) == "a7a8q"
    with pytest.raises(ValueError, match="null"):
        unpack_move(0)
    with pytest.raises(ValueError, match="reserved"):
        unpack_move(0x8001)
    with pytest.raises(ValueError, match="promotion"):
        unpack_move(1 | (2 << 6) | (1 << 12))


def _native_frame(
    record: FullGameRecord | RejectedAttempt,
) -> bytes:
    if isinstance(record, FullGameRecord):
        flat = tuple(move for series in record.series for move in series)
        cumulative = []
        total = 0
        for series in record.series:
            total += len(series)
            cumulative.append(total)
        body_size = (
            codec.NATIVE_RECORD_PREFIX.size
            + len(cumulative) * 8
            + len(flat) * 2
        )
        body = bytearray(
            codec.NATIVE_RECORD_PREFIX.pack(
                body_size,
                record.attempt_index,
                0,
                int(record.terminal),
                0,
                0,
                len(record.series),
                len(flat),
                record.logical_work,
            )
        )
        body.extend(struct.pack(f"<{len(cumulative)}Q", *cumulative))
        body.extend(struct.pack(f"<{len(flat)}H", *(pack_move(move) for move in flat)))
    else:
        body = bytearray(
            codec.NATIVE_RECORD_PREFIX.pack(
                codec.NATIVE_RECORD_PREFIX.size,
                record.attempt_index,
                1,
                0,
                int(record.reason),
                0,
                0,
                0,
                record.logical_work,
            )
        )
    header = codec.NATIVE_BATCH_HEADER.pack(
        codec.NATIVE_BATCH_MAGIC,
        codec.NATIVE_BATCH_VERSION,
        codec.NATIVE_BATCH_HEADER.size,
        1,
        record.attempt_index,
        1,
    )
    return header + body


def test_native_frame_preserves_boundaries_reject_reason_and_work() -> None:
    accepted = FullGameRecord(9, Terminal.CHECKMATE_WHITE, WHITE_MATE, 12345)
    rejected = RejectedAttempt(10, RejectReason.OVERFLOW, 54321)

    assert decode_native_batch(_native_frame(accepted)).records == (accepted,)
    assert decode_native_batch(_native_frame(rejected)).records == (rejected,)

    corrupt = bytearray(_native_frame(rejected))
    prefix_at = codec.NATIVE_BATCH_HEADER.size
    values = list(codec.NATIVE_RECORD_PREFIX.unpack_from(corrupt, prefix_at))
    values[6] = 1
    values[7] = 1
    values[0] += 10
    corrupt[prefix_at : prefix_at + codec.NATIVE_RECORD_PREFIX.size] = (
        codec.NATIVE_RECORD_PREFIX.pack(*values)
    )
    corrupt.extend(struct.pack("<QH", 1, pack_move("e2e4")))
    with pytest.raises(ValueError, match="rejected native record carries"):
        decode_native_batch(corrupt)
    with pytest.raises(ValueError, match="native rejection"):
        RejectedAttempt(11, RejectReason.CANCELLED, 1)


def test_v2_semantic_defaults_bind_profile_pool_policy_and_schedule() -> None:
    config = FullGameSemanticConfig.from_profiles(
        (baseline_profile(), _second_profile()),
        backend_kind="native",
    )

    assert config.max_frontier_states == DEFAULT_FRONTIER_STATES == 8
    assert config.candidate_count == DEFAULT_CANDIDATE_COUNT == 8
    assert config.max_positions_per_series == DEFAULT_POSITIONS_PER_SERIES == 5_000
    assert config.max_positions_per_game == DEFAULT_POSITIONS_PER_GAME == 5_000_000
    assert config.rank_policy_id == UNIFORM_POLICY_ID
    assert config.profile_schedule_id == PROFILE_SCHEDULE_ORDERED_V2
    assert config.preserve_returned_mate is True
    assert config.data_purpose == DATA_PURPOSE == "exploration-rollout-v1"
    assert config.strength_claim == STRENGTH_CLAIM == "not-champion-play"
    assert [config.profile_pair(attempt) for attempt in range(4)] == [
        (0, 0),
        (1, 1),
        (0, 1),
        (1, 0),
    ]
    assert FullGameSemanticConfig.from_dict(config.as_dict()) == config
    assert len(config.semantic_config_digest) == 32
    assert config.as_dict()["native_semantic_digest"] == (
        config.semantic_config_digest.hex()
    )
    assert len(bytes.fromhex(config.simulation_id.removeprefix("spc-fullgame-"))) == 32
    changed_profile = replace(config.profile_pool[1], material=150)
    changed = replace(config, profile_pool=(config.profile_pool[0], changed_profile))
    assert changed.profile_pool[1].profile_digest != config.profile_pool[1].profile_digest
    assert changed.simulation_id != config.simulation_id
    provenance_only_profile = replace(config.profile_pool[1], series_reach=125)
    provenance_only = replace(
        config,
        profile_pool=(config.profile_pool[0], provenance_only_profile),
    )
    assert provenance_only_profile.profile_digest == config.profile_pool[1].profile_digest
    assert provenance_only.simulation_id != config.simulation_id
    tampered = config.as_dict()
    tampered["profiles"][0]["weights"]["material"] = 150
    with pytest.raises(ValueError, match="profile digest"):
        FullGameSemanticConfig.from_dict(tampered)
    with pytest.raises(ValueError, match="preserve"):
        replace(config, preserve_returned_mate=False)


def test_v2_request_is_canonical_and_checked_decoder_requires_exact_echo() -> None:
    config = FullGameSemanticConfig.from_profiles(
        (baseline_profile(), _second_profile()),
        backend_kind="native",
    )
    profiles = tuple(
        NativeV2Profile(profile.profile_digest_bytes, *profile.native_weights)
        for profile in config.profile_pool
    )
    request = encode_native_v2_request(
        first_attempt=2,
        attempt_count=2,
        seed=config.seed,
        max_attempt_series=config.max_attempt_series,
        max_frontier_states=config.max_frontier_states,
        max_positions_per_series=config.max_positions_per_series,
        max_positions_per_game=config.max_positions_per_game,
        candidate_count=config.candidate_count,
        profiles=profiles,
        policy_kind=config.native_policy_kind,
        schedule_kind=config.native_schedule_kind,
        config_digest=config.semantic_config_digest,
    )
    fields = NATIVE_V2_REQUEST_HEADER.unpack_from(request)
    assert fields[0] == b"SPCFGR02"
    assert fields[4] == len(request)
    assert fields[13] == 2
    assert fields[16] == 1  # preserve-returned-mate flag
    assert fields[17:23] == (0, 0, 0, 0, 0, 0)
    assert fields[23] == config.semantic_config_digest
    assert profiles[0].digest == hashlib.sha256(
        b"SPC-FAST-WEIGHTS-V1\0" + struct.pack("<5q", *profiles[0].weights)
    ).digest()
    canonical_request = bytearray(request)
    canonical_request[24:40] = bytes(16)
    canonical_request[108:140] = bytes(32)
    assert fields[23] == hashlib.sha256(
        b"SPC-FULLGAME-CONFIG-V2\0" + canonical_request
    ).digest()
    with pytest.raises(ValueError, match="preserve"):
        encode_native_v2_request(
            first_attempt=2,
            attempt_count=2,
            seed=config.seed,
            max_attempt_series=config.max_attempt_series,
            max_frontier_states=config.max_frontier_states,
            max_positions_per_series=config.max_positions_per_series,
            max_positions_per_game=config.max_positions_per_game,
            candidate_count=config.candidate_count,
            profiles=profiles,
            policy_kind=config.native_policy_kind,
            schedule_kind=config.native_schedule_kind,
            preserve_returned_mate=False,
        )
    opaque_profile = NativeV2Profile(bytes([7]) * 32, *profiles[0].weights)
    with pytest.raises(ValueError, match="profile digest"):
        encode_native_v2_request(
            first_attempt=2,
            attempt_count=2,
            seed=config.seed,
            max_attempt_series=config.max_attempt_series,
            max_frontier_states=config.max_frontier_states,
            max_positions_per_series=config.max_positions_per_series,
            max_positions_per_game=config.max_positions_per_game,
            candidate_count=config.candidate_count,
            profiles=(opaque_profile,),
            policy_kind=config.native_policy_kind,
            schedule_kind=codec.NATIVE_V2_SCHEDULE_SELF_ROUND_ROBIN,
        )

    decoded = decode_native_batch_v2(
        _rejected_v2_response(request),
        expected_first_attempt=2,
        expected_attempt_count=2,
        expected_config_digest=config.semantic_config_digest,
        expected_profile_count=2,
        expected_policy_kind=NATIVE_V2_POLICY_UNIFORM,
        expected_schedule_kind=NATIVE_V2_SCHEDULE_ORDERED_PAIR_ROUND_ROBIN,
    )
    assert [
        (record.white_profile_index, record.black_profile_index)
        for record in decoded.records
    ] == [(0, 1), (1, 0)]
    assert decoded.total_path_count_saturations == 3

    for payload, message in (
        (_rejected_v2_response(request, tamper_digest=True), "config digest"),
        (_rejected_v2_response(request, tamper_pair=True), "profile pair"),
        (
            _rejected_v2_response(request, tamper_total_saturations=True),
            "saturation total",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            decode_native_batch_v2(
                payload,
                expected_first_attempt=2,
                expected_attempt_count=2,
                expected_config_digest=config.semantic_config_digest,
                expected_profile_count=2,
                expected_policy_kind=NATIVE_V2_POLICY_UNIFORM,
                expected_schedule_kind=NATIVE_V2_SCHEDULE_ORDERED_PAIR_ROUND_ROBIN,
            )

    valid_response = _rejected_v2_response(request)
    for field_index, replacement, message in (
        (5, 3, "range"),
        (6, 1, "range"),
        (8, 3, "profile count"),
        (9, 2, "policy"),
        (10, 1, "schedule"),
    ):
        header = list(NATIVE_V2_RESPONSE_HEADER.unpack_from(valid_response))
        header[field_index] = replacement
        tampered_response = (
            NATIVE_V2_RESPONSE_HEADER.pack(*header)
            + valid_response[NATIVE_V2_RESPONSE_HEADER.size :]
        )
        with pytest.raises(ValueError, match=message):
            decode_native_batch_v2(
                tampered_response,
                expected_first_attempt=2,
                expected_attempt_count=2,
                expected_config_digest=config.semantic_config_digest,
                expected_profile_count=2,
                expected_policy_kind=NATIVE_V2_POLICY_UNIFORM,
                expected_schedule_kind=NATIVE_V2_SCHEDULE_ORDERED_PAIR_ROUND_ROBIN,
            )


def test_checked_v2_wrapper_recomputes_bindings_and_rejects_echo_tamper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = FullGameSemanticConfig.from_profiles(
        (baseline_profile(), _second_profile()),
        backend_kind="native",
    )
    requests: list[bytes] = []

    class Native:
        SOURCE_IDENTITY = config.backend_source_identity

        @staticmethod
        def generate_full_game_batch_v2(request: bytes) -> bytes:
            requests.append(request)
            return _rejected_v2_response(request)

    monkeypatch.setattr(evaluation, "_native_eval", Native())
    records = generate_native_batch_v2(config, 0, 2)
    assert len(records) == 2
    fields = NATIVE_V2_REQUEST_HEADER.unpack_from(requests[0])
    assert fields[23] == config.semantic_config_digest
    assert fields[16] == 1
    first_profile_offset = NATIVE_V2_REQUEST_HEADER.size
    assert requests[0][first_profile_offset : first_profile_offset + 32] == (
        config.profile_pool[0].profile_digest_bytes
    )

    class TamperedNative(Native):
        @staticmethod
        def generate_full_game_batch_v2(request: bytes) -> bytes:
            return _rejected_v2_response(request, tamper_digest=True)

    monkeypatch.setattr(evaluation, "_native_eval", TamperedNative())
    with pytest.raises(ValueError, match="config digest"):
        generate_native_batch_v2(config, 0, 1)


def test_chunk_crc_and_exact_header_are_enforced() -> None:
    record = FullGameRecord(0, Terminal.CHECKMATE_WHITE, WHITE_MATE, 88)
    simulation_id = "spc-fullgame-" + "a" * 64
    payload = encode_chunk(
        (record,), simulation_id=simulation_id, first_attempt=0, attempt_count=1
    )

    assert decode_chunk(payload).records == (record,)
    corrupted = bytearray(payload)
    corrupted[-1] ^= 1
    with pytest.raises(ValueError, match="CRC"):
        decode_chunk(corrupted)


def test_store_counts_only_global_unique_replay_verified_terminal_games(
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    result = _run_two_games(root)

    assert result["accepted_unique_games"] == 2
    assert result["attempts_committed"] == 4
    assert result["duplicate_traces"] == 1
    assert result["native_or_policy_rejects"] == 1
    assert result["logical_work"] == 45
    assert result["rejections_by_reason"] == {
        "duplicate_trace": 1,
        "work_limit": 1,
    }
    verified = verify_fullgame_run(root)
    assert verified["authoritative_replay"] == "passed"
    assert verified["attempts_committed"] == 4

    destination = tmp_path / "games.jsonl"
    exported = export_fullgame_jsonl(root, destination)
    lines = [json.loads(line) for line in destination.read_text().splitlines()]
    assert exported["exported_games"] == 2
    assert [line["result"] for line in lines] == ["1-0", "0-1"]
    assert all(line["label_kind"] == "terminal-WDL" for line in lines)
    assert all(len(line["series"]) >= 4 for line in lines)
    assert [line["logical_work"] for line in lines] == [10, 20]


def test_v2_store_persists_profile_colors_and_saturation_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = FullGameSemanticConfig.from_profiles(
        (baseline_profile(), _second_profile()),
        backend_kind="native",
    )

    class Native:
        SOURCE_IDENTITY = config.backend_source_identity

        @staticmethod
        def generate_full_game_batch_v2(_request: bytes) -> bytes:
            raise AssertionError("custom deterministic generator should be used")

    monkeypatch.setattr(evaluation, "_native_eval", Native())

    def generator(
        semantic: FullGameSemanticConfig,
        first_attempt: int,
        attempt_count: int,
    ) -> tuple[FullGameRecord, ...]:
        records = []
        traces = (WHITE_MATE, BLACK_MATE)
        terminals = (Terminal.CHECKMATE_WHITE, Terminal.CHECKMATE_BLACK)
        for attempt in range(first_attempt, first_attempt + attempt_count):
            white, black = semantic.profile_pair(attempt)
            index = attempt % 2
            records.append(
                FullGameRecord(
                    attempt,
                    terminals[index],
                    traces[index],
                    100 + attempt,
                    white,
                    black,
                    10 + attempt,
                )
            )
        return tuple(records)

    root = tmp_path / "v2-pool-run"
    result = run_fullgame_generation(
        root,
        config,
        target_unique_games=2,
        attempts_per_chunk=2,
        backend="native",
        requested_workers=1,
        generator=generator,
    )
    assert result["profile_count"] == 2
    assert result["path_count_saturations"] == 21
    assert result["data_purpose"] == "exploration-rollout-v1"
    assert verify_fullgame_run(root)["path_count_saturations"] == 21

    destination = tmp_path / "v2-pool.jsonl"
    export_fullgame_jsonl(root, destination)
    lines = [json.loads(line) for line in destination.read_text().splitlines()]
    assert [line["white_profile_id"] for line in lines] == [
        config.profile_pool[0].profile_id,
        config.profile_pool[1].profile_id,
    ]
    assert [line["black_profile_id"] for line in lines] == [
        config.profile_pool[0].profile_id,
        config.profile_pool[1].profile_id,
    ]
    assert [line["path_count_saturations"] for line in lines] == [10, 11]
    assert all(line["data_purpose"] == "exploration-rollout-v1" for line in lines)
    assert all(line["strength_claim"] == "not-champion-play" for line in lines)


def test_illegal_native_terminal_cannot_increment_or_persist_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bad-terminal"
    illegal = FullGameRecord(
        0,
        Terminal.CHECKMATE_WHITE,
        (("e2e4",), ("e7e5", "e7e6")),
        99,
    )
    with FullGameStore(
        root,
        _reference_config(),
        target_unique_games=1,
        attempts_per_chunk=1,
        backend="reference",
    ) as store:
        with pytest.raises(ValueError, match="illegal series"):
            store.commit_outcomes((illegal,))
        assert store.summary()["accepted_unique_games"] == 0
        assert store.next_attempt == 0
    assert not list((root / "chunks").glob("*.spcg"))


def test_optional_chunk_sink_is_out_of_band_resumable_and_content_addressed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sink-run"
    _run_two_games(root)

    class Sink:
        def __init__(self) -> None:
            self.chunks = []

        def store_committed_chunk(self, chunk) -> None:
            assert chunk.path.read_bytes()
            self.chunks.append(chunk)

    sink = Sink()
    first = deliver_committed_fullgame_chunks(root, sink, limit=1)
    assert first["offered_chunks"] == 1
    second = deliver_committed_fullgame_chunks(
        root,
        sink,
        delivered_chunk_ids=first["offered_chunk_ids"],
    )
    assert second["offered_chunks"] == 1
    assert second["skipped_already_delivered"] == 1
    assert len({chunk.chunk_id for chunk in sink.chunks}) == 2
    assert all(chunk.chunk_id.endswith(chunk.sha256) for chunk in sink.chunks)


def test_one_and_many_workers_produce_identical_chunk_bytes(tmp_path: Path) -> None:
    serial = tmp_path / "serial"
    parallel = tmp_path / "parallel"

    _run_two_games(serial, workers=1)

    def out_of_order_generator(
        config: FullGameSemanticConfig,
        first_attempt: int,
        attempt_count: int,
    ) -> tuple[FullGameRecord | RejectedAttempt, ...]:
        if first_attempt == 0:
            time.sleep(0.03)
        return _generator(config, first_attempt, attempt_count)

    parallel_result = run_fullgame_generation(
        parallel,
        _reference_config(),
        target_unique_games=2,
        attempts_per_chunk=2,
        backend="reference",
        requested_workers=4,
        generator=out_of_order_generator,
    )

    serial_chunks = [path.read_bytes() for path in sorted((serial / "chunks").glob("*.spcg"))]
    parallel_chunks = [path.read_bytes() for path in sorted((parallel / "chunks").glob("*.spcg"))]
    assert parallel_chunks == serial_chunks
    assert fullgame_status(parallel)["progress"] == fullgame_status(serial)["progress"]
    assert parallel_result["discarded_or_cancelled_attempts"] > 0


def test_native_one_and_many_workers_persist_identical_bytes(tmp_path: Path) -> None:
    if evaluation._native_eval is None or not hasattr(
        evaluation._native_eval, "generate_full_game_batch"
    ):
        pytest.skip("optional source-matched native full-game kernel is not built")
    config = FullGameSemanticConfig.from_profile(
        seed=123,
        max_frontier_states=8,
        max_positions_per_series=20_000,
        max_positions_per_game=500_000,
        candidate_count=8,
        backend_kind="native",
    )
    serial = tmp_path / "native-serial"
    parallel = tmp_path / "native-parallel"
    for root, workers in ((serial, 1), (parallel, 4)):
        run_fullgame_generation(
            root,
            config,
            target_unique_games=6,
            attempts_per_chunk=3,
            backend="native",
            requested_workers=workers,
        )
        verify_fullgame_run(root)

    serial_chunks = [path.read_bytes() for path in sorted((serial / "chunks").glob("*.spcg"))]
    parallel_chunks = [path.read_bytes() for path in sorted((parallel / "chunks").glob("*.spcg"))]
    assert parallel_chunks == serial_chunks
    assert fullgame_status(parallel)["progress"] == fullgame_status(serial)["progress"]


def test_staged_target_and_changed_chunk_size_match_one_shot_logic(
    tmp_path: Path,
) -> None:
    staged = tmp_path / "staged"
    one_shot = tmp_path / "one-shot"
    config = _reference_config()

    run_fullgame_generation(
        staged,
        config,
        target_unique_games=1,
        attempts_per_chunk=1,
        backend="reference",
        requested_workers=1,
        generator=_generator,
    )
    resumed = run_fullgame_generation(
        staged,
        config,
        target_unique_games=2,
        attempts_per_chunk=3,
        backend="reference",
        requested_workers=3,
        generator=_generator,
    )
    direct = _run_two_games(one_shot, workers=2, attempts_per_chunk=2)

    assert resumed["attempts_committed"] == direct["attempts_committed"] == 4
    assert fullgame_status(staged)["progress"] == fullgame_status(one_shot)["progress"]
    staged_export = tmp_path / "staged.jsonl"
    direct_export = tmp_path / "direct.jsonl"
    export_fullgame_jsonl(staged, staged_export)
    export_fullgame_jsonl(one_shot, direct_export)
    assert staged_export.read_bytes() == direct_export.read_bytes()
    verify_fullgame_run(staged)


def test_worker_failure_leaves_only_the_contiguous_committed_prefix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "interrupted"

    def failing_generator(
        config: FullGameSemanticConfig,
        first_attempt: int,
        attempt_count: int,
    ) -> tuple[FullGameRecord | RejectedAttempt, ...]:
        if first_attempt == 2:
            raise RuntimeError("worker failed")
        return _generator(config, first_attempt, attempt_count)

    with pytest.raises(RuntimeError, match="worker failed"):
        run_fullgame_generation(
            root,
            _reference_config(),
            target_unique_games=2,
            attempts_per_chunk=2,
            backend="reference",
            requested_workers=2,
            generator=failing_generator,
        )
    assert fullgame_status(root)["progress"]["attempts_committed"] == 2

    resumed = _run_two_games(root, workers=2)
    assert resumed["attempts_committed"] == 4
    verify_fullgame_run(root)


def test_prepared_chunk_is_recovered_atomically(tmp_path: Path) -> None:
    root = tmp_path / "prepared"
    _run_two_games(root)
    checkpoint = root / "checkpoint.sqlite3"
    with sqlite3.connect(checkpoint) as connection:
        row = connection.execute(
            "SELECT filename,pending_filename FROM chunks WHERE chunk_index=1"
        ).fetchone()
        assert row is not None
        connection.execute("UPDATE chunks SET state='prepared' WHERE chunk_index=1")
    final_path = root / "chunks" / row[0]
    pending_path = root / "chunks" / row[1]
    final_path.replace(pending_path)

    with FullGameStore(
        root,
        _reference_config(),
        target_unique_games=2,
        attempts_per_chunk=7,
        backend="reference",
    ) as store:
        assert store.summary()["status"] == "complete"
    assert final_path.is_file()
    assert not pending_path.exists()
    verify_fullgame_run(root)


def test_run_directory_allows_only_one_checkpoint_writer(tmp_path: Path) -> None:
    root = tmp_path / "single-writer"
    config = _reference_config()
    with FullGameStore(
        root,
        config,
        target_unique_games=2,
        attempts_per_chunk=2,
        backend="reference",
    ):
        with pytest.raises(ValueError, match="another full-game writer"):
            FullGameStore(
                root,
                config,
                target_unique_games=2,
                attempts_per_chunk=2,
                backend="reference",
            )
    with FullGameStore(
        root,
        config,
        target_unique_games=2,
        attempts_per_chunk=2,
        backend="reference",
    ) as resumed:
        assert resumed.next_attempt == 0


def test_corrupt_committed_chunk_fails_closed_and_releases_windows_handles(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corrupt"
    _run_two_games(root)
    chunk = next((root / "chunks").glob("*.spcg"))
    payload = bytearray(chunk.read_bytes())
    payload[-1] ^= 1
    chunk.write_bytes(payload)

    with pytest.raises(ValueError, match="hash mismatch"):
        FullGameStore(
            root,
            _reference_config(),
            target_unique_games=3,
            attempts_per_chunk=1,
            backend="reference",
        )
    renamed = tmp_path / "corrupt-renamed"
    root.rename(renamed)
    shutil.rmtree(renamed)


def test_startup_rejects_deleted_seen_trace_before_scheduling_and_closes_db(
    tmp_path: Path,
) -> None:
    root = tmp_path / "missing-seen-index"
    config = _reference_config()
    run_fullgame_generation(
        root,
        config,
        target_unique_games=1,
        attempts_per_chunk=2,
        backend="reference",
        requested_workers=1,
        generator=_generator,
    )
    chunks_before = {
        path.name: path.read_bytes() for path in (root / "chunks").glob("*.spcg")
    }
    manifest_before = (root / "manifest.json").read_bytes()
    connection = sqlite3.connect(root / "checkpoint.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM seen_traces").fetchone()[0] == 1
        connection.execute("DELETE FROM seen_traces")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="accepted index diverges"):
        FullGameStore(
            root,
            config,
            target_unique_games=2,
            attempts_per_chunk=7,
            backend="reference",
        )
    assert (root / "manifest.json").read_bytes() == manifest_before
    assert {
        path.name: path.read_bytes() for path in (root / "chunks").glob("*.spcg")
    } == chunks_before
    renamed = tmp_path / "missing-seen-index-renamed"
    root.rename(renamed)


@pytest.mark.parametrize("tamper_kind", ("reason", "balanced_work"))
def test_startup_rejects_rejected_envelope_tamper_before_manifest_rebuild(
    tmp_path: Path,
    tamper_kind: str,
) -> None:
    root = tmp_path / f"rejected-envelope-{tamper_kind}"
    config = _reference_config()
    _run_two_games(root, attempts_per_chunk=4)
    manifest_before = (root / "manifest.json").read_bytes()
    chunks_before = {
        path.name: path.read_bytes() for path in (root / "chunks").glob("*.spcg")
    }

    connection = sqlite3.connect(root / "checkpoint.sqlite3")
    try:
        if tamper_kind == "reason":
            connection.execute(
                "UPDATE rejected_attempts SET reason='overflow' "
                "WHERE attempt_index='1'"
            )
        else:
            connection.execute(
                "UPDATE rejected_attempts SET logical_work=logical_work+1 "
                "WHERE attempt_index='1'"
            )
            connection.execute(
                "UPDATE rejected_attempts SET logical_work=logical_work-1 "
                "WHERE attempt_index='2'"
            )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="disposition"):
        FullGameStore(
            root,
            config,
            target_unique_games=3,
            attempts_per_chunk=7,
            backend="reference",
        )
    assert (root / "manifest.json").read_bytes() == manifest_before
    assert {
        path.name: path.read_bytes() for path in (root / "chunks").glob("*.spcg")
    } == chunks_before

    renamed = tmp_path / f"rejected-envelope-{tamper_kind}-renamed"
    root.rename(renamed)
    shutil.rmtree(renamed)


def test_verify_cross_checks_checkpoint_work_and_attempt_partition(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tampered"
    _run_two_games(root)
    checkpoint = root / "checkpoint.sqlite3"
    with sqlite3.connect(checkpoint) as connection:
        connection.execute(
            "UPDATE chunks SET logical_work=logical_work+1 WHERE chunk_index=0"
        )
    with pytest.raises(ValueError, match="counters do not match replay"):
        verify_fullgame_run(root)


def test_verify_fails_closed_when_semantic_fingerprint_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "semantic-drift"
    _run_two_games(root)

    monkeypatch.setattr(
        fullgame_module,
        "FULLGAME_SEMANTIC_FINGERPRINT",
        "f" * 64,
    )
    with pytest.raises(ValueError, match="semantic fingerprint is stale"):
        verify_fullgame_run(root)


def test_verify_rejects_canonical_backend_relabel(tmp_path: Path) -> None:
    root = tmp_path / "backend-relabel"
    _run_two_games(root)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    manifest["backend"] = "native"
    manifest_path.write_bytes(
        (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
    )

    with pytest.raises(ValueError, match="backend label is inconsistent"):
        verify_fullgame_run(root)


def test_backend_and_source_identity_are_part_of_simulation_identity() -> None:
    native = FullGameSemanticConfig.from_profile(backend_kind="native")
    reference = FullGameSemanticConfig.from_profile(backend_kind="reference")

    assert native.simulation_id != reference.simulation_id
    with pytest.raises(ValueError, match="source identity is stale"):
        replace(reference, backend_source_identity="wrong")


def test_fullgames_run_cli_builds_an_exact_reference_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_run(root, config, **options):
        captured.update(root=root, config=config, options=options)
        return {
            "accepted_unique_games": 2,
            "accepted_unique_games_per_second": 12.5,
            "attempts_committed": 4,
            "attempted_this_call": 4,
            "backend": "reference",
            "committed_attempts_per_second": 25.0,
            "duplicate_traces": 1,
            "native_or_policy_rejects": 1,
            "status": "complete",
        }

    monkeypatch.setattr(fullgame_module, "run_fullgame_generation", fake_run)
    root = tmp_path / "cli-run"
    assert main(
        [
            "fullgames",
            "run",
            str(root),
            "--backend",
            "reference",
            "--target",
            "2",
            "--attempts-per-chunk",
            "3",
            "--workers",
            "2",
            "--json",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    config = captured["config"]
    assert isinstance(config, FullGameSemanticConfig)
    assert config.backend_kind == "reference"
    assert captured["options"]["target_unique_games"] == 2
    assert captured["options"]["attempts_per_chunk"] == 3
    assert captured["options"]["requested_workers"] == 2
    assert payload["accepted_unique_games"] == 2


def test_fullgames_cli_loads_immutable_profile_pool_with_fast_v2_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_path = save_profile(baseline_profile(), tmp_path / "baseline.json")
    second_path = save_profile(_second_profile(), tmp_path / "second.json")
    captured: dict[str, object] = {}

    def fake_run(root, config, **options):
        captured.update(root=root, config=config, options=options)
        return {
            "accepted_unique_games": 1,
            "accepted_unique_games_per_second": 1.0,
            "attempts_committed": 1,
            "attempted_this_call": 1,
            "backend": "native",
            "committed_attempts_per_second": 1.0,
            "duplicate_traces": 0,
            "native_or_policy_rejects": 0,
            "status": "complete",
        }

    monkeypatch.setattr(fullgame_module, "run_fullgame_generation", fake_run)
    assert main(
        [
            "fullgames",
            "run",
            str(tmp_path / "pool-run"),
            "--profile-pool",
            str(first_path),
            str(second_path),
            "--target",
            "1",
            "--json",
        ]
    ) == 0
    capsys.readouterr()
    config = captured["config"]
    assert isinstance(config, FullGameSemanticConfig)
    assert [profile.profile_id for profile in config.profile_pool] == [
        baseline_profile().profile_id,
        _second_profile().profile_id,
    ]
    assert config.max_frontier_states == 8
    assert config.candidate_count == 8
    assert config.max_positions_per_series == 5_000
    assert config.max_positions_per_game == 5_000_000
    assert config.profile_schedule_id == PROFILE_SCHEDULE_ORDERED_V2


def test_fullgames_status_verify_and_export_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "cli-existing"
    _run_two_games(root)

    assert main(["fullgames", "status", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["progress"][
        "accepted_unique_games"
    ] == 2
    assert main(["fullgames", "verify", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["authoritative_replay"] == "passed"
    destination = tmp_path / "cli-export.jsonl"
    assert main(
        ["fullgames", "export", str(root), str(destination), "--limit", "1"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["exported_games"] == 1
    assert len(destination.read_text().splitlines()) == 1
