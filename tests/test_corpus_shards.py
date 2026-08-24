from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import chess
import pytest

import scottish_progressive.corpus_shards as corpus_shards
from scottish_progressive.corpus_shards import (
    AttemptRangeConflict,
    CorpusIdentity,
    CorpusIdentityError,
    CorpusRecord,
    CorpusStore,
    SHARD_MAGIC,
    ShardCorruptionError,
    ShardMetadata,
    progressive_state_dedup_key,
)
from scottish_progressive.model import ProgressiveState


def _identity(*, profile_ids: tuple[str, ...] = ("profile-b", "profile-a")) -> CorpusIdentity:
    return CorpusIdentity(
        record_schema="spc-nnue-sample-v1",
        source_fingerprint="source-test-01234567",
        generator_config_sha256="ab" * 32,
        profile_ids=profile_ids,
        ruleset_version="scottish-modern-common-v1",
    )


def _state(*, quiet_series: int = 0, promoted: bool = False) -> ProgressiveState:
    board = chess.Board("4k3/8/8/8/8/8/8/3QK3 w - - 0 1")
    if promoted:
        board.promoted = chess.BB_D1
    return ProgressiveState(board, series_number=1, quiet_series=quiet_series)


def _write_one(
    store: CorpusStore,
    start: int,
    stop: int,
    records: list[CorpusRecord],
    *,
    owner: str,
):
    writer = store.begin_shard(start, stop, owner_id=owner)
    for record in records:
        writer.add(record)
    return writer.finalize()


def test_progressive_state_dedup_key_covers_full_progressive_identity() -> None:
    ordinary = _state()
    promoted = _state(promoted=True)
    quiet = _state(quiet_series=1)

    assert ordinary.board.board_fen(promoted=False) == promoted.board.board_fen(
        promoted=False
    )
    assert ordinary.pfen == promoted.pfen
    assert progressive_state_dedup_key(ordinary) != progressive_state_dedup_key(
        promoted
    )
    assert progressive_state_dedup_key(ordinary) != progressive_state_dedup_key(quiet)

    castling_board = chess.Board()
    no_castling_board = castling_board.copy(stack=False)
    no_castling_board.castling_rights = 0
    with_castling = ProgressiveState(castling_board, 1)
    without_castling = ProgressiveState(no_castling_board, 1)
    assert with_castling.board.board_fen() == without_castling.board.board_fen()
    assert progressive_state_dedup_key(with_castling) != progressive_state_dedup_key(
        without_castling
    )

    ep_board = chess.Board("4k3/8/8/3pP3/8/8/8/4K3 w - - 0 1")
    no_ep = ProgressiveState(ep_board, 1)
    with_ep = ProgressiveState(ep_board, 1, ep_targets=(chess.D6,))
    assert no_ep.board.board_fen() == with_ep.board.board_fen()
    assert progressive_state_dedup_key(no_ep) != progressive_state_dedup_key(with_ep)
    assert progressive_state_dedup_key(no_ep) != progressive_state_dedup_key(
        no_ep, ruleset_version="future-rules-v2"
    )


def test_manifest_binds_schema_source_profiles_and_rules(tmp_path: Path) -> None:
    identity = _identity()
    store = CorpusStore(tmp_path / "corpus", identity)

    manifest = store.manifest
    assert manifest["format"] == "spc-sharded-corpus-manifest-v1"
    assert manifest["schema_version"] == 1
    assert manifest["identity"] == {
        "generator_config_sha256": "ab" * 32,
        "profile_ids": ["profile-b", "profile-a"],
        "record_schema": "spc-nnue-sample-v1",
        "ruleset_version": "scottish-modern-common-v1",
        "source_fingerprint": "source-test-01234567",
    }
    assert manifest["identity_sha256"] == identity.digest_hex
    assert manifest["totals"] == {
        "attempt_count": 0,
        "record_count": 0,
        "shard_count": 0,
    }
    raw = store.manifest_path.read_bytes()
    assert raw.count(b"\n") == 1
    assert raw == json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii") + b"\n"

    with pytest.raises(CorpusIdentityError, match="identity"):
        CorpusStore(tmp_path / "corpus", _identity(profile_ids=("different",)))
    with pytest.raises(CorpusIdentityError, match="identity"):
        CorpusStore(
            tmp_path / "corpus",
            _identity(profile_ids=("profile-a", "profile-b")),
        )
    reopened = CorpusStore.open(tmp_path / "corpus")
    assert reopened.identity == identity
    assert reopened.verify() == store.verify()


def test_verified_snapshot_reads_exact_shards_and_detects_later_append(
    tmp_path: Path,
) -> None:
    store = CorpusStore(tmp_path / "corpus", _identity())
    first = CorpusRecord.from_state(0, 0, _state(), b"first")
    _write_one(store, 0, 1, [first], owner="worker-0")

    manifest, shards = store.verified_snapshot()
    assert manifest["attempt_count"] == 1
    assert [record.payload for record in store.iter_snapshot_records(shards)] == [
        b"first"
    ]
    second = CorpusRecord.from_state(1, 0, _state(quiet_series=1), b"second")
    _write_one(store, 1, 2, [second], owner="worker-1")
    assert store.verified_snapshot() != (manifest, shards)
    assert [record.payload for record in store.iter_snapshot_records(shards)] == [
        b"first"
    ]


def test_snapshot_record_reader_rejects_mutated_bytes_before_yield(
    tmp_path: Path,
) -> None:
    store = CorpusStore(tmp_path / "corpus", _identity())
    _write_one(
        store,
        0,
        1,
        [CorpusRecord.from_state(0, 0, _state(), b"immutable")],
        owner="worker-0",
    )
    _, shards = store.verified_snapshot()
    shard_path = store.root / shards[0].file
    raw = bytearray(shard_path.read_bytes())
    raw[-1] ^= 1
    shard_path.write_bytes(raw)

    with pytest.raises(ShardCorruptionError, match="bytes changed"):
        list(store.iter_snapshot_records(shards))

def test_binary_shard_finalization_is_atomic_and_content_addressed(
    tmp_path: Path,
) -> None:
    store = CorpusStore(tmp_path / "corpus", _identity())
    first = CorpusRecord.from_state(0, 0, _state(), b"\x01win")
    second = CorpusRecord.from_state(2, 0, _state(quiet_series=1), b"\x00draw")
    metadata = _write_one(store, 0, 10, [first, second], owner="worker-0")

    path = store.root / metadata.file
    assert path.read_bytes().startswith(SHARD_MAGIC)
    assert not path.read_bytes().startswith(b"{")
    assert metadata.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert metadata.record_count == 2
    assert metadata.attempt_range.start == 0
    assert metadata.attempt_range.stop == 10
    assert not list(store.shards_directory.glob("*.tmp"))
    assert not list(store.claims_directory.glob("claim-*.json"))
    assert store.verify() == {
        "attempt_count": 10,
        "corpus_sha256": store.manifest["corpus_sha256"],
        "record_count": 2,
        "shard_count": 1,
    }
    assert list(store.iter_records()) == [first, second]


def test_attempt_claims_and_finalized_ranges_reject_duplicates_and_overlaps(
    tmp_path: Path,
) -> None:
    store = CorpusStore(tmp_path / "corpus", _identity())
    writer = store.begin_shard(0, 10, owner_id="worker-a")
    with pytest.raises(AttemptRangeConflict, match="overlaps active"):
        store.begin_shard(5, 15, owner_id="worker-b")

    record = CorpusRecord.from_state(0, 0, _state(), b"record")
    writer.add(record)
    with pytest.raises(ValueError, match="strict attempt/sequence order"):
        writer.add(record)
    writer.finalize()

    with pytest.raises(AttemptRangeConflict, match="overlaps finalized"):
        store.begin_shard(0, 10, owner_id="worker-a")
    with pytest.raises(AttemptRangeConflict, match="overlaps finalized"):
        store.begin_shard(9, 20, owner_id="worker-c")

    adjacent = store.begin_shard(10, 20, owner_id="worker-c")
    adjacent.abort(release_claim=True)


def test_attempt_range_supports_the_last_unsigned_64_bit_attempt(tmp_path: Path) -> None:
    store = CorpusStore(tmp_path / "corpus", _identity())
    last = (1 << 64) - 1
    record = CorpusRecord.from_state(last, 0, _state(), b"last-attempt")
    _write_one(store, last, 1 << 64, [record], owner="edge-worker")
    assert list(store.iter_records()) == [record]
    with pytest.raises(ValueError, match="attempt range"):
        store.begin_shard(False, 1, owner_id="invalid")


def test_merge_order_and_state_dedup_are_deterministic(tmp_path: Path) -> None:
    store = CorpusStore(tmp_path / "corpus", _identity())
    shared_key = progressive_state_dedup_key(_state())
    later = CorpusRecord(10, 0, shared_key, b"later-duplicate")
    tail = CorpusRecord.from_state(11, 0, _state(quiet_series=2), b"tail")
    _write_one(store, 10, 20, [later, tail], owner="worker-1")

    first = CorpusRecord(1, 0, shared_key, b"first-owner")
    middle = CorpusRecord.from_state(3, 0, _state(quiet_series=1), b"middle")
    _write_one(store, 0, 10, [first, middle], owner="worker-0")

    assert [shard.attempt_range.start for shard in store.shards] == [0, 10]
    assert [record.attempt_index for record in store.iter_records()] == [1, 3, 10, 11]
    unique = list(store.iter_records(deduplicate_states=True))
    assert [record.attempt_index for record in unique] == [1, 3, 11]
    assert unique[0].payload == b"first-owner"


def test_manifest_bytes_do_not_depend_on_shard_completion_order(tmp_path: Path) -> None:
    identity = _identity()

    def populate(root: Path, order: tuple[int, int]) -> bytes:
        store = CorpusStore(root, identity)
        records = {
            0: CorpusRecord.from_state(1, 0, _state(), b"first"),
            10: CorpusRecord.from_state(11, 0, _state(quiet_series=1), b"second"),
        }
        for start in order:
            _write_one(
                store,
                start,
                start + 10,
                [records[start]],
                owner=f"worker-{start}",
            )
        return store.manifest_path.read_bytes()

    assert populate(tmp_path / "one", (0, 10)) == populate(tmp_path / "two", (10, 0))


def test_owner_provenance_does_not_change_shard_or_corpus_content_address(
    tmp_path: Path,
) -> None:
    identity = _identity()
    record = CorpusRecord.from_state(0, 0, _state(), b"same-content")
    first = CorpusStore(tmp_path / "first", identity)
    second = CorpusStore(tmp_path / "second", identity)
    first_metadata = _write_one(first, 0, 10, [record], owner="worker-a")
    second_metadata = _write_one(second, 0, 10, [record], owner="worker-b")

    assert first_metadata.sha256 == second_metadata.sha256
    assert first.manifest["corpus_sha256"] == second.manifest["corpus_sha256"]
    assert first_metadata.owner_sha256 != second_metadata.owner_sha256


def test_unclaimed_orphan_is_rejected_instead_of_silently_adopted(
    tmp_path: Path,
) -> None:
    identity = _identity()
    source = CorpusStore(tmp_path / "source", identity)
    metadata = _write_one(
        source,
        0,
        10,
        [CorpusRecord.from_state(0, 0, _state(), b"unclaimed")],
        owner="source-worker",
    )
    target = CorpusStore(tmp_path / "target", identity)
    shutil.copyfile(source.root / metadata.file, target.root / metadata.file)

    with pytest.raises(corpus_shards.CorpusStoreError, match="no exact active"):
        CorpusStore(target.root, identity)


def test_crash_after_shard_rename_is_recovered_on_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    identity = _identity()
    store = CorpusStore(root, identity)
    writer = store.begin_shard(0, 10, owner_id="stable-worker")
    record = CorpusRecord.from_state(0, 0, _state(), b"recover-me")
    writer.add(record)

    original = corpus_shards._atomic_write_json
    failed = False

    def fail_manifest_once(path: Path, payload: dict[str, object]) -> None:
        nonlocal failed
        totals = payload.get("totals")
        if (
            not failed
            and path.name == "manifest.json"
            and isinstance(totals, dict)
            and totals.get("shard_count") == 1
        ):
            failed = True
            raise OSError("simulated crash before manifest publication")
        original(path, payload)

    monkeypatch.setattr(corpus_shards, "_atomic_write_json", fail_manifest_once)
    with pytest.raises(OSError, match="simulated crash"):
        writer.finalize()
    assert len(list(store.shards_directory.glob("*.spcbin"))) == 1
    stale_manifest = json.loads(store.manifest_path.read_text(encoding="ascii"))
    assert stale_manifest["totals"]["shard_count"] == 0

    monkeypatch.setattr(corpus_shards, "_atomic_write_json", original)
    resumed = CorpusStore(root, identity)
    assert list(resumed.iter_records()) == [record]
    assert resumed.manifest["totals"] == {
        "attempt_count": 10,
        "record_count": 1,
        "shard_count": 1,
    }
    assert not list(resumed.claims_directory.glob("claim-*.json"))


def test_producer_receipt_is_bound_before_shard_publication(tmp_path: Path) -> None:
    store = CorpusStore(tmp_path / "corpus", _identity())
    writer = store.begin_shard(0, 1, owner_id="receipt-worker")
    writer.add(CorpusRecord.from_state(0, 0, _state(), b"bound"))
    receipt_sha256 = "a" * 64
    observed: list[ShardMetadata] = []

    def bind_receipt(metadata: ShardMetadata) -> str:
        observed.append(metadata)
        assert not (store.root / metadata.file).exists()
        return receipt_sha256

    metadata = writer.finalize(before_publish=bind_receipt)
    assert len(observed) == 1
    assert observed[0].producer_receipt_sha256 is None
    assert metadata.producer_receipt_sha256 == receipt_sha256
    manifest_shard = store.manifest["shards"][0]
    assert manifest_shard["producer_receipt_sha256"] == receipt_sha256
    assert CorpusStore(store.root, store.identity).shards == (metadata,)

    other = CorpusStore(tmp_path / "other", store.identity)
    other_writer = other.begin_shard(0, 1, owner_id="other-receipt-worker")
    other_writer.add(CorpusRecord.from_state(0, 0, _state(), b"bound"))
    other_writer.finalize(before_publish=lambda _: "b" * 64)
    assert metadata.sha256 == other.shards[0].sha256
    assert store.verify()["corpus_sha256"] != other.verify()["corpus_sha256"]


def test_failed_receipt_publication_leaves_no_adoptable_shard(tmp_path: Path) -> None:
    store = CorpusStore(tmp_path / "corpus", _identity())
    writer = store.begin_shard(0, 1, owner_id="receipt-worker")
    writer.add(CorpusRecord.from_state(0, 0, _state(), b"not-published"))

    def fail_receipt(_: ShardMetadata) -> str:
        raise OSError("receipt durability failed")

    with pytest.raises(OSError, match="receipt durability failed"):
        writer.finalize(before_publish=fail_receipt)
    writer.abort()
    assert not list(store.shards_directory.glob("*.spcbin"))
    assert store.manifest["totals"]["shard_count"] == 0


def test_partial_temp_is_invisible_and_same_owner_can_restart(tmp_path: Path) -> None:
    store = CorpusStore(tmp_path / "corpus", _identity())
    first = store.begin_shard(20, 30, owner_id="stable-worker")
    first.add(CorpusRecord.from_state(20, 0, _state(), b"partial"))
    first.abort()
    (store.shards_directory / ".simulated-crash.tmp").write_bytes(b"partial")

    resumed = CorpusStore(store.root, store.identity)
    assert resumed.manifest["totals"]["shard_count"] == 0
    with pytest.raises(AttemptRangeConflict, match="overlaps active"):
        resumed.begin_shard(20, 30, owner_id="different-worker")
    retry = resumed.begin_shard(20, 30, owner_id="stable-worker")
    retry.add(CorpusRecord.from_state(20, 0, _state(), b"complete"))
    retry.finalize()
    assert [record.payload for record in resumed.iter_records()] == [b"complete"]


def test_claim_parser_rejects_coerced_numbers(tmp_path: Path) -> None:
    store = CorpusStore(tmp_path / "corpus", _identity())
    owner = corpus_shards._owner_digest("worker").hex()
    claim_path = store.claims_directory / f"claim-{0:020d}-{10:020d}-{owner[:16]}.json"
    payload = {
        "attempt_start": 0.0,
        "attempt_stop": 10.0,
        "format": "spc-corpus-attempt-claim-v1",
        "identity_sha256": store.identity.digest_hex,
        "owner_sha256": owner,
    }
    claim_path.write_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
        + b"\n"
    )
    with pytest.raises(corpus_shards.CorpusStoreError, match="claim .* is invalid"):
        CorpusStore(store.root, store.identity)


def test_corruption_and_manifest_duplicate_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    identity = _identity()
    store = CorpusStore(root, identity)
    metadata = _write_one(
        store,
        0,
        10,
        [CorpusRecord.from_state(0, 0, _state(), b"intact")],
        owner="worker-0",
    )
    path = store.root / metadata.file
    content = bytearray(path.read_bytes())
    content[-1] ^= 1
    path.write_bytes(content)
    with pytest.raises(ShardCorruptionError):
        store.verify()

    path.write_bytes(bytes(content[:-1]) + bytes([content[-1] ^ 1]))
    manifest = store.manifest
    manifest["shards"].append(dict(manifest["shards"][0]))
    store.manifest_path.write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("ascii")
        + b"\n"
    )
    with pytest.raises(AttemptRangeConflict, match="duplicate shard"):
        CorpusStore(root, identity)
