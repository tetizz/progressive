# Native self-play corpus pipeline

The native corpus path turns deterministic C++ full-game attempts into compact,
replay-verified positional training data. It is a data-generation and evaluation
facility; generating more positions does not by itself make the playing engine
stronger.

## Trust boundary

`scottish_progressive.native_corpus` is the production wrapper for the binary
`SPCFGR02`/`SPCFGB02` ABI. It:

- derives ordered profile and semantic-configuration SHA-256 bindings;
- rejects production generation when the requested engine version, 16-hex
  engine fingerprint, or ruleset differs from the running source;
- pins the returned-mate preservation policy;
- refuses to run without a native extension whose full C++ source identity
  matches the packaged native sources;
- validates every echoed field, record size, attempt index, profile schedule,
  terminal/reject combination, saturation receipt, and packed move;
- replays every accepted trace from the standard start through `play_series`;
- requires the replayed terminal and checkmate winner to match the native record.

There is no silent Python fallback and no acceptance of a partially valid batch.

## Training record

Each replayed boundary is encoded as one fixed 160-byte
`spc-native-boundary-outcome-v1` sample. It contains all twelve piece bitboards,
side to move, castling rights, promoted-piece provenance, Chess960 mode, the full
progressive en-passant set, series and quiet-series counters, both profile
indices, terminal type, and eventual WDL value from the boundary side's
perspective. The outer shard record adds the attempt/sequence owner and the
full-state SHA-256.

The fixed representation removes repeated field names and verbose FEN/game
dictionaries while remaining independently decodable and verifiable. Actual
space savings depend on game length and must be measured from the shard receipt.

## Generate and resume

Build a fresh source-matched corpus package, then run:

```powershell
$nativeLib = 'build\native-corpus-current'
$nativeTemp = 'build\native-corpus-current-temp'
$env:SPC_OMIT_STALE_OPENING_REPORTS = '1'
python setup.py build_py --build-lib $nativeLib --force
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python setup.py build_ext --build-lib $nativeLib --build-temp $nativeTemp --force
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$env:PYTHONPATH = (Resolve-Path $nativeLib).Path
python scripts\generate_native_corpus.py build\corpus-train `
  --attempts 1000000 `
  --shard-size 10000 `
  --batch-size 256 `
  --workers 8 `
  --receipt build\corpus-train-receipt.json
```

The two forced build stages matter: `build_py` replaces any stale copied Python
package and `build_ext` recompiles the source-identity-bound native module. The
opening-report omission is appropriate for this corpus-only package; it does
not weaken corpus validation or the engine kernel.

The attempt domain is counter-based. Adjacent shards produce the same records as
one unsplit range, and rerunning the same command skips finalized shards. A
changed seed, policy, limit, profile order, ruleset, or source fingerprint has a
different corpus identity and cannot be mixed into the same root.

Every root also contains an immutable `native-generation-contract.json`. The
manifest binds the configuration digest; this sidecar retains and verifies its
full semantic preimage: ABI, seed, limits, policy, schedule, engine/rules
identity, and ordered native profile records. Generation rejects a malformed or
conflicting sidecar before doing shard work. The explicit backfill helper only
creates a missing sidecar when a supplied plan reproduces the existing store
identity exactly.

Each finalized range also has a canonical `native-outcomes/outcome-*.json`
receipt. It binds the complete attempt range, generation contract, finalized
shard hash and metadata, accepted games, native rejections and reasons,
terminal counts, logical work, and saturation counts. Its digest is persisted
in the active claim before shard rename and in the manifest content root during
publication. Resume therefore reports zero newly generated attempts while
reconstructing exact durable totals, including ranges where every attempt was
rejected and the binary shard has no records. Missing, altered, recomputed, or
shard-mismatched receipts fail before more generation or training begins.
These checks detect independent corruption and inconsistent producer output;
they do not replace an external signature against a writer that can rewrite
both a receipt and its manifest root.

The persisted engine fingerprint is the repository's 16-hex source fingerprint.
The native extension's full C++ source identity is checked at runtime and in
fresh-build tests; it is not represented as that shorter sidecar field.

Payload verification is enabled by default. For very large stores,
`--skip-unique-count` retains full payload/state-key verification without holding
every seen SHA-256 in memory. `--skip-payload-verification` is available only for
an explicitly deferred verification pass.

Use `--profile path\to\profile.json` to generate from a non-baseline profile;
the option may be repeated for a profile schedule. Profile order is part of the
immutable identity.

## Fit and gate candidates

Training requires two roots generated with different seeds and otherwise
identical generation contracts:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
python scripts\tune_native_corpus.py build\corpus-train `
  build\corpus-holdout build\candidate-fit --minimum-series 3
```

The adapter validates generation contracts, manifest-bound outcome
receipts, and manifests; checks grouped accepted attempts against each shard
range independently while retaining its durable rejected total; decodes and
validates records in complete accepted-attempt groups; drops the terminal
boundary; normalizes each game before filtering; aggregates repeated labels;
and removes every exact training state from holdout without redistributing its
weight. Train and holdout manifest roots/totals are captured before these
passes, rechecked afterward, and the captured roots are emitted, so a shard
finalized concurrently cannot appear in evidence without appearing in the
samples. This is exact
full-state leakage protection; it does not claim to remove related trajectories
or equal feature vectors across splits. It reports retained holdout game-weight
coverage and refuses a fully overlapping holdout. Equal seven-value feature
vectors are collapsed exactly within each split for fitting; this changes
neither the weighted target nor log loss.

`scripts/screen_native_candidates.py` runs the rules/tactical gate and a short
color-swapped development screen. Its reused fixed suite is only a shortlist,
and the script never promotes a profile. A separate promotion authority must
enforce a differently seeded final match with no technical failures.

## Checked capacity result

The compact tracked evidence, including raw-artifact hashes and runtime details,
is in
[`native-corpus-milestone-2026-08-23.json`](../benchmarks/results/native-corpus-milestone-2026-08-23.json).
Those historical corpus roots were captured before per-shard outcome digests
became part of the manifest content root. Their measured records, timings, and
match results remain evidence for that run, but a current rebuild intentionally
produces a different corpus root.

The 2026-08-23 local source-matched run used the default
higher-ranked-move-biased mixture, 32 retained frontier states, 16 returned
candidates, eight worker threads, eight 512-attempt shards, and 4,096 attempts.
Its receipt measured pending-shard generation, authoritative replay, and atomic
publication at 30.835 seconds; final store verification and the CLI
payload-verification pass were outside that timer:

- 132.84 attempts/second;
- 4,000 accepted games and 96 proof-required rejections;
- 38,397 replayed boundary occurrences;
- 5,289 unique full progressive states;
- 7,987,408 bytes of binary shard data;
- deterministic corpus root
  `952500bd974978be16805e7fc32e06bbf66f517719fb012433df1579eb291f52`.

An immediate restart recognized and reused all eight finalized shards, generated
zero new attempts, and reproduced the same corpus root and unique-state count.

At this measured single-machine rate, a purely linear four-billion-attempt run
would take about 348.5 days. That is not four billion unique positions: the
checked run retained only 5,289 unique states from 38,397 occurrences, and the
unique yield falls as one fixed policy/seed distribution saturates. Billion-scale
unique data cannot be inferred from the attempt counter. A practical
billion-scale plan would likely use distributed attempt ranges, several
independently bound exploration distributions, and an external-memory dedup
merge—not merely raise one local counter.

A later uniform-policy capacity run was faster and more diverse under a
different sampling distribution. The 16,384-attempt bootstrap train store ran
at 299.88 attempts/second, accepted 16,375 games, and retained 40,300 unique
states from 108,488 boundaries. Its independently seeded 8,192-attempt holdout
ran at 312.07 attempts/second and retained 22,013 unique states. A linear
four-billion-attempt extrapolation at 299.88 attempts/second is about 154.4
days, still not four billion unique positions and not directly comparable to
the top-heavy policy above.

## Checked strength outcome

These are local milestone results recorded in the tracked benchmark evidence;
they are not a release-strength claim. The first top-heavy value candidate
passed every tactical check and scored
13/1/6 over 20 fixed-suite games, but a separate 50-game seeded suite was
effectively even: 25/0/24 among completed games, 2/21/1 by pairs, with one
incomplete pair. A larger uniform bootstrap then produced a development-screen
leader at 7/0/3, but the fresh 50-game suite rejected it at 21/1/28 and 1/19/5
by pairs, with no technical failures. The baseline therefore remains champion.
These results show that lower held-out WDL log loss is not sufficient evidence
of stronger move selection; no trained profile from this milestone is deployed.
