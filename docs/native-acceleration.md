# Native acceleration

The first native milestone accelerates the exact ordering evaluator used twice
on the serious search path: while bounded complete-series frontiers are ranked
and again when generated series are ordered for minimax. It is a C++20 CPython
extension, not a second rules engine. If compilation or loading is unavailable,
the same public function automatically calls the retained Python oracle.

This boundary was selected from a profile of the actual S1/S3/S4 search. On the
published S4 position, ordering evaluation accounted for about 0.86 seconds of
a 2.11-second profiled run. Moving only the final `sorted()` call would not have
removed that work.

The v0.7 milestone extends the same source-matched C++20 module with exact legal
micro-move generation, post-move state transitions, check/capture/pawn flags,
ordinary castling, promotion, and Scottish multi-target en passant. Python still
owns complete-series orchestration, SAN, proof-aware search, training, and the
web API. Unsupported Chess960 boards and any stale or missing native module use
the retained Python oracle.

The v0.8 milestone adds bulk complete-series generation for compatible search
requests. The native kernel replays a required prefix, expands and merges the
same-side frontier, applies Scottish check and stalemate truncation, enforces
the deterministic work budget, and preserves full raw, unique, transposition,
frontier-pruning, and work counts. For the normal branch cap it also reproduces
the searcher's exact terminal/static ordering and returns only the top 32
series. Python then materializes those retained UCI paths and continues to own
minimax, proof propagation, quiet-series adjudication, and the public result.

The v0.9 N2 milestone adds the exact full evaluation boundary and changes the
bulk handoff from eagerly materialized retained series to an opaque native
capsule plus lightweight UCI references. Search decodes a candidate's final
boundary only when it needs that candidate, and an active reference keeps its
capsule alive even after the generation cache evicts the batch. Published
result evidence is still replayed through `play_series`; Python continues to
own minimax, proof intervals, adjudication, cancellation policy, and the public
objects. A separately metered promotion-mate lane covers the verified
check-avoiding underpromotion/reuse construction without turning failure to
find a mate into a no-mate proof.

## Correctness boundary

The compiled implementation exactly mirrors the integer terms in
`evaluation.fast_evaluate`:

- material from color-specific bitboards;
- legal king flight squares, including captures and attacked destinations;
- progressive promotion budgets and blocked promotion corridors;
- attacked-material vulnerability;
- boundary check and profile-specific weights;
- Python-compatible floor division and ties-to-even rounding.

Through v0.7, Python complete-series orchestration remained authoritative for
check truncation, repeated promoted-piece moves, progressive stalemate,
quiet-series metadata, SAN, transposition path counts, cancellation, and
deterministic work charging. The v0.8 bulk path now mirrors that generation and
charging in native code when its exact contract applies. It retains every
full-generation count even when native final scoring pre-caps the returned
series to 32. In v0.9, native records may supply lazy search boundaries, but
Python replays the series that become returned search evidence through
`play_series`, which remains authoritative for `SeriesResult`, PFEN, SAN, and
published outcome materialization. Python still owns minimax, proof, and
adjudication.

The one-move S1 root deliberately bypasses the bulk kernel; the cheap direct
Python path handles that series, while its S2 descendants use native bulk
generation. Searches carrying a deadline/cancellation callback also use the
Python oracle so cancellation remains live. Missing or stale extensions,
Chess960, arbitrary frontier callbacks, unsupported numeric ranges, and native
counter overflow likewise restart on the pristine oracle. Native and fallback
paths must produce identical selected series, scores, PVs, proof flags, and
every generation/search work counter.

The differential gate covers deterministic random progressive-reachable
positions, multi-en-passant, promotion, castling, checked boundaries, mutated
weight vectors, the published S4 mating position, and live high-series S22,
S24, and S101 anchors. Full-search tests compare the complete scored ordering
and logical-work counters, not only the selected move.

For the frozen v0.9 source, the exact-native gate passed 374 tests. The forced
Python-fallback run passed 302 tests and skipped 72 native-only checks. All five
published tactical anchors were found as replay-proven mates at the production
depth/cap/work limits. The promotion-mate gate also reproduced the same early
checking mates at S9 and S10, correctly ending each series with two moves
unused. A standard-checkmate prefilter is never sufficient by itself: the
authoritative replay rejects a nominal mate when Scottish multi-target en
passant supplies a reply.

The compiled API uses checked signed 64-bit arithmetic throughout. The Python
dispatcher keeps the public unbounded-series contract: it uses the oracle when
a series/profile could exceed that native range or the exact-integer envelope
of the oracle's floating-point percentage scaling. This is an acceleration
guard, not a game move limit. Regression anchors include series 39,050,001,
series 2,147,483,649, near-`int64` inputs, and deliberately adversarial weights.

The source fingerprint hashes `.py`, `.cpp`, `.hpp`, and `.h` files. The wheel
ships the native source beside the compiled module so runtime and build-time
fingerprints stay reproducible. The build also embeds a SHA-256 identity of the
native `.cpp` and `.hpp` sources in the extension. At import time Python hashes
the packaged copies and enables the extension only on an exact match, so a
locked or stale in-place binary falls back instead of silently running old
code. `SPC_DISABLE_NATIVE=1` forces the oracle path
for differential testing.

The frozen v0.9 engine fingerprint is `806aa0d679f6d1ef`. Its embedded native
source identity is
`e7d36c5fc755cca2ae8877f4e73d8f9aa161a405a4c91361a6866d2f6463ca4f`.

## Reproducible benchmark

Run from a build where the extension is available:

```powershell
python benchmarks\native_ordering_benchmark.py --samples 3 --depth 2
```

Each measurement starts in a fresh Python process. The benchmark refuses to
report a speedup if the selected series, PV, scored alternatives, proof flags,
or logical-work counters differ.

Measured on Windows with CPython 3.14.4, depth 2, branch cap 32, and a 250,000
logical-position budget (median of three fresh processes):

| Position | Python oracle | C++20 | End-to-end speedup | Output/work |
| --- | ---: | ---: | ---: | --- |
| S1 initial | 1.3838 s | 1.0597 s | 1.31x | identical |
| S3 initial board | 13.2208 s | 8.2807 s | 1.60x | identical |
| published S4 mate | 0.4584 s | 0.2738 s | 1.67x | identical |

The exact raw run is artifact `spc-native-ordering-0e631b2fe6d86b02` in
[the checked benchmark JSON](../benchmarks/results/native-ordering-v0.6.0-cp314-windows-depth2.json).
It records all 18 fresh-process timings in paired, interleaved, alternating
order, plus engine/source identity, the embedded native source digest,
compiled-module basename and SHA-256, host/runtime details, timestamp, and
configuration. The parity signature includes every search-stat counter and
every alternative's proof bounds. No user-specific filesystem path is
serialized.

### v0.7 legal-series kernel

The series benchmark isolates legal generation with a constant frontier score,
so it measures the new rules kernel rather than re-crediting the ordering
evaluator. Seven fresh-process, paired, interleaved samples on the same Windows
host produced:

| Position | Python | Native | Speedup | Series/work |
| --- | ---: | ---: | ---: | --- |
| S1 | 1.249 ms | 0.767 ms | 1.63x | identical |
| S3 | 55.881 ms | 35.649 ms | 1.57x | identical |
| published S4 mate | 93.213 ms | 60.403 ms | 1.54x | identical |
| live S22 adjudication state | 6.456 ms | 4.482 ms | 1.44x | identical |
| S101 width-one stress case | 23.291 ms | 20.495 ms | 1.14x | identical |

Artifact `spc-native-series-8da9cb0440334621` is checked in as
[`native-series-v0.7.0-cp314-windows.json`](../benchmarks/results/native-series-v0.7.0-cp314-windows.json).
It records engine fingerprint `1b7cfe378fa646c6`, native source identity,
compiled-module SHA-256, host/runtime data, every raw timing, full series digest,
and every `GenerationStats` field. This is a generation benchmark, not a claim
that complete games became 1.6 times faster.

### v0.8 complete-series search path

The v0.8 benchmark compares one cold-installed wheel in two modes: the
micro-native baseline keeps the existing evaluation and legal-move kernels but
hides `generate_complete_series`; the bulk-native mode exposes the new complete
series and exact top-32 path. Each measurement runs in a fresh process, paired
and interleaved in alternating order:

```powershell
python benchmarks\native_complete_search_benchmark.py `
  --samples 3 --depth 2 --branch-cap 32 --max-work 250000 `
  --output benchmarks\results\native-complete-search-v0.8.0-cp314-windows-depth2.json
```

Measured on Windows with CPython 3.14.4, depth 2, branch cap 32, and a 250,000
logical-position budget (median of three fresh processes):

| Position | Micro-native | Bulk native | End-to-end speedup | Output/proof/work |
| --- | ---: | ---: | ---: | --- |
| S1 | 0.7127814 s | 0.4193095 s | 1.700x | identical |
| S3 | 6.8801801 s | 0.6795530 s | 10.1246x | identical |
| published S4 mate | 0.2163944 s | 0.0170888 s | 12.663x | identical |
| live S22 adjudication state | 2.3934753 s | 0.1119073 s | 21.388x | identical |

Artifact `spc-native-complete-search-6d8b8617c9206cf2` is checked in as
[`native-complete-search-v0.8.0-cp314-windows-depth2.json`](../benchmarks/results/native-complete-search-v0.8.0-cp314-windows-depth2.json).
It records the installed module and source identities, all 24 timings, and the
complete semantic signature for each position. Parity includes the score, best
series, PV, alternatives digest, completed/exact/timed/work flags, proof,
adjudication, and every `SearchStats` field. S1's root itself takes the direct
Python bypass; its 20 native calls are the S2 child searches.

As a separate throughput check, the exact same 20 scheduled fixed-suite records
took 346.5628463 seconds under v0.7 and 20.1899159 seconds with the final
cold-installed v0.8 wheel. The 17.165x improvement reproduced
all 20 game records exactly: 19 valid checkmates plus one unchanged
`manual-adjudication-pending` incomplete. These wall times measure a 16-worker
pool, not the latency of an individual game and not a strength improvement.

The final cold-installed v0.8 wheel, source fingerprint
`f369b5da69c17c5f`, also ran a larger independent gate:
[`selfplay-fresh-seeded-100-v0.8.0.json`](../benchmarks/results/selfplay-fresh-seeded-100-v0.8.0.json).
All 100 scheduled games were conclusive, with zero failures or incompletes, in
75.944 seconds (1.31676 games/second across the 16-worker pool). From the
candidate's perspective the game W/D/L was 49/1/50 and the color-swapped pair
W/D/L was 7/36/7, an exact 0.500 pair score and a descriptive -3 performance
difference. Those results establish neither a speed-related strength gain nor
a standout variant: no promotion occurred and the baseline remains champion.
The rate is aggregate pool throughput, not single-game latency.

### v0.9 N2 lazy boundary search

The final five-sample benchmark compares the frozen source-matched v0.9 wheel
against the frozen v0.8 wheel. It rejects any undeclared difference in score,
best series, PV, alternatives, completion/width/timeout/work flags, proof,
adjudication, or deterministic work categories. The checked benchmark is
[`native-boundary-search-v0.9.0-cp314-windows-depth2.json`](../benchmarks/results/native-boundary-search-v0.9.0-cp314-windows-depth2.json).
Rounded release comparisons are:

| Position | N2 speedup over v0.8 | Output/proof/work |
| --- | ---: | --- |
| S1 | about 7.9x | identical |
| S3 | about 2.0x | identical |
| published S4 mate | about 1.5x | identical, apart from the pinned sound-proof correction |
| live S22 adjudication state | about 1.3x | identical |

The final artifact does not claim a pool speedup. The new tactical lane can
legitimately change a full-game trajectory, so v0.8 and v0.9 pool games are not
accepted as an apples-to-apples timing pair. None of these timings is a playing
strength gain or evidence of Stockfish-level play.

## Packaging and fallback

Setuptools builds `scottish_progressive._native_eval` with `/std:c++20 /O2` on
Windows and `-std=c++20 -O3` on Linux. The extension is optional so an
unsupported source environment still installs a correct, slower engine. Normal
Windows and Linux release wheels should be built on their target platform and
cold-installed before publication.

## Next boundary

Compatible complete-series generation, exact final pre-capping, full
evaluation, and lazy retained-candidate decoding are now native. Python still
performs series-boundary alpha-beta, proof propagation, adjudication,
transposition policy, and authoritative replay of published evidence. A future
boundary can move more of that search loop native only if it preserves Scottish
truncation, liveness fallbacks, proof intervals, capsule ownership, and every
deterministic work/cancellation count. N2 is a verified acceleration boundary,
not a fully native or Stockfish-strength engine.
