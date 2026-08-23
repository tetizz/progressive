# Deterministic corpus shards

`scottish_progressive.corpus_shards` is the first durable storage boundary for
large self-play corpora. It stores framed binary records, not per-game JSON, and
keeps only compact corpus/shard metadata in canonical JSON.

## Identity and layout

Every store is opened with one immutable `CorpusIdentity` containing the record
schema, engine source fingerprint, generator-configuration SHA-256, ordered
profile IDs, and Scottish ruleset. The native generator's semantic-config
digest is the intended configuration value: it prevents workers with different
seeds, limits, policies, or profile schedules from sharing a store. The
identity's domain-separated SHA-256 is embedded in every shard header. A store
refuses to open under a different identity.

```text
corpus-root/
  manifest.json
  native-generation-contract.json  # native producer semantic preimage
  .corpus.lock
  claims/
    claim-<attempt-start>-<attempt-stop>-<owner-digest>.json
  native-outcomes/
    outcome-<attempt-start>-<attempt-stop>.json  # native producer only
  shards/
    shard-<attempt-start>-<attempt-stop>-<sha-prefix>.spcbin
```

The canonical manifest owns non-overlapping half-open attempt ranges. Each
entry records its opaque owner SHA-256, raw-file SHA-256, byte size, record
count, and an optional producer-receipt SHA-256. Manifest entries are always
sorted by attempt range, so completion order does not change the manifest or
corpus digest. Owner identity is provenance, not content: it is deliberately
excluded from shard bytes and the corpus content root, so safe worker
reassignment cannot change otherwise identical data. A producer-receipt digest
is content and remains inside the root. Native corpus generation uses it to
bind the exact accepted/rejected outcomes to the shard's full attempt range.
This is deterministic content binding, not a signature or external trust
anchor: the store assumes its local writers are trusted. A deployment that
must resist coordinated manifest and sidecar rewriting should pin the expected
corpus root outside the store or add a signing layer.

## Full progressive-state key

`progressive_state_dedup_key()` hashes a canonical binary encoding of:

- every color/piece bitboard;
- side to move, clean castling rights, and Chess960 mode;
- promoted-piece provenance;
- the complete progressive en-passant target set;
- series number and quiet-series count;
- ruleset and quiet-draw policy.

Orthodox halfmove and fullmove display clocks are deliberately excluded because
they do not decide Scottish Progressive legality or the ten-series quiet rule.
Consequently, two states that share an ordinary board FEN but differ in any
progressive rule field do not deduplicate.

## Binary contract

A shard begins with fixed magic/version fields, its attempt start/count, record
count, corpus-identity digest, and reserved zero bytes. Each record is framed as:

```text
uint64 attempt_index
uint32 sequence_index
bytes32 progressive_state_sha256
uint32 payload_size
bytes[payload_size] compact caller-defined payload
```

Records must arrive in strict `(attempt_index, sequence_index)` order and remain
inside the claimed range. The payload schema is named by `CorpusIdentity`; the
storage layer does not serialize or interpret verbose game dictionaries.

## Atomicity and resume

Range claims and manifest updates use canonical temporary files and file
`fsync`, all under a retrying cross-process store lock. Publication uses an
atomic replace plus directory `fsync` on POSIX and write-through `MoveFileExW`
on Windows. A writer patches the final record count, flushes and syncs its
temporary shard, then publishes it under its content-addressed filename. A
producer may use the locked pre-publication hook to durably write side data
after the final shard hash is known. If the hook returns a receipt digest, that
digest is first saved in the active claim and then carried into the manifest
content root.

Shard rename and manifest replacement are two separate filesystem commits; the
module does not call that window transactionally atomic. Recovery makes it
convergent instead:

1. A crash before shard rename leaves only an ignored `.tmp` file. The same
   stable owner can restart the claimed range from its beginning. A native
   outcome receipt written just before that crash must reproduce exactly on
   retry or generation fails closed.
2. A crash after shard rename but before manifest replacement leaves a complete
   orphan. The next opener requires its exact active range/identity claim, then
   verifies binary framing, record count, filename, and SHA-256 before adopting
   it. The claim also preserves any producer-receipt digest, so adoption cannot
   discard outcome provenance. An unclaimed file is rejected rather than
   silently trusted.
3. A crash after manifest replacement but before claim deletion leaves a stale
   exact claim. The next opener removes it after matching range and owner digest.
4. Overlapping claims, overlapping finalized/orphan ranges, missing shards,
   corruption, or identity drift fail closed.

Resume is currently shard-granular, not mid-shard. Keep shard ranges bounded so
replaying one interrupted range is cheap.

## Merge and dedup

`CorpusStore.iter_records()` takes a verified snapshot and yields shards in
attempt-range order and records in attempt/sequence order. With
`deduplicate_states=True`, the earliest deterministic owner of a full state key
is retained. The first implementation keeps seen SHA-256 keys in memory; a
future billion-scale merge should partition those keys externally without
changing the state-key or shard contracts.

```python
identity = CorpusIdentity(
    record_schema="spc-nnue-sample-v1",
    source_fingerprint=source_fingerprint,
    generator_config_sha256=semantic_config_digest.hex(),
    profile_ids=profile_ids,
    ruleset_version=ruleset_version,
)
store = CorpusStore(output_directory, identity)
writer = store.begin_shard(0, 100_000, owner_id="worker-000")
for attempt, sequence, state, compact_payload in records:
    writer.add_state(attempt, sequence, state, compact_payload)
writer.finalize()
```

This layer intentionally does not schedule remote workers, generate games,
define training labels, or garbage-collect abandoned temporary files. Those are
separate producer/orchestration policies built on top of the verified shard
boundary.
