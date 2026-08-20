# Native ordering acceleration

The first native milestone accelerates the exact ordering evaluator used twice
on the serious search path: while bounded complete-series frontiers are ranked
and again when generated series are ordered for minimax. It is a C++20 CPython
extension, not a second rules engine. If compilation or loading is unavailable,
the same public function automatically calls the retained Python oracle.

This boundary was selected from a profile of the actual S1/S3/S4 search. On the
published S4 position, ordering evaluation accounted for about 0.86 seconds of
a 2.11-second profiled run. Moving only the final `sorted()` call would not have
removed that work.

## Correctness boundary

The compiled implementation exactly mirrors the integer terms in
`evaluation.fast_evaluate`:

- material from color-specific bitboards;
- legal king flight squares, including captures and attacked destinations;
- progressive promotion budgets and blocked promotion corridors;
- attacked-material vulnerability;
- boundary check and profile-specific weights;
- Python-compatible floor division and ties-to-even rounding.

The Python complete-series generator remains authoritative for Scottish
multi-en-passant state, check truncation, repeated promotion-piece moves,
castling, progressive stalemate, quiet-series metadata, SAN, transposition path
counts, cancellation, and deterministic work charging. Because native and
fallback ordering scores are identical, none of those rules or counters change.

The differential gate covers deterministic random progressive-reachable
positions, multi-en-passant, promotion, castling, checked boundaries, mutated
weight vectors, the published S4 mating position, and live high-series S22,
S24, and S101 anchors. Full-search tests compare the complete scored ordering
and logical-work counters, not only the selected move.

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

## Packaging and fallback

Setuptools builds `scottish_progressive._native_eval` with `/std:c++20 /O2` on
Windows and `-std=c++20 -O3` on Linux. The extension is optional so an
unsupported source environment still installs a correct, slower engine. Normal
Windows and Linux release wheels should be built on their target platform and
cold-installed before publication.

## Next boundary

Legal move generation, board copies, SAN construction, and transposition
materialization are still Python. A later native complete-series frontier can
reuse this bitboard core, but it must first reproduce every rule result and
every deterministic work/cancellation count. This milestone deliberately does
not claim the previously estimated 3x frontier target or a fully native engine.
