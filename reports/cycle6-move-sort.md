# Cycle 6 compact move-sort checkpoint

Date: `2026-08-26`

Baseline source fingerprint: `a71198a160691211`

Candidate source fingerprint: `102906f5c8651305`

Primary evidence: `build/cycle6/paired-sort-d4-7.json`

## Accepted change

The native legal-expansion path now sorts the compact five-byte pseudo `Move`
entries by UCI key before applying them. It filters illegal moves and skips
duplicate legal keys while constructing the result, so it no longer sorts and
deduplicates the completed `ExpandedMove` vector afterward. In the measured
Emscripten layout, an `ExpandedMove` is 104 bytes and contains an 88-byte child
board. Moving the sort ahead of child construction reduces comparison and
memory-movement cost without changing legal-move order or returned children.

## Paired native D4 evidence

The benchmark used seven fresh-process samples per runtime and scenario. Each
repetition alternated baseline/candidate execution order. Both cases used the
baseline engine profile, depth 4, retained width 32, a 10,000,000-position work
ceiling, one native thread, and no wall deadline.

| Position | Baseline median | Candidate median | Median speed change | Speedup |
| --- | ---: | ---: | ---: | ---: |
| Initial | 6.5127 s | 6.4502 s | **+0.9596%** | 1.00969x |
| Black after `e4` | 7.3851 s | 7.3538 s | **+0.4238%** | 1.00426x |

The benchmark rejects a candidate unless every sample within a runtime is
deterministic and the candidate exactly matches the baseline semantic and work
signature. Both cases passed. The compared signature includes score, selected
series, complete PV, alternatives and their bounds, completed depth, proof and
forced status, width/exhaustion flags, timeout/work-limit flags, and every
search-statistics field. This therefore measures a real implementation speed
change with identical selected chess and identical charged work.

The evidence artifact was produced immediately before the benchmark's
descriptive rename, so its top-level schema retains the temporary
`spc-cycle6-frontier-reuse-benchmark-v1` label. The renamed benchmark now emits
`spc-cycle6-native-d4-benchmark-v1`; its sampling and equality gates are
unchanged.

## Isolated Emscripten evidence

An optimized Emscripten 6.0.8 expansion microbenchmark used 4,096
deterministically generated boards, three warm-up passes, and 64 measured
passes: 262,144 calls producing 2,941,952 legal expansions. Baseline and
candidate returned the same generated count, checksum, and exact board/move
parity hash. The compact pre-sort candidate was **19.192% faster** in this
isolated kernel measurement.

This larger microbenchmark result explains the direction of the whole-search
gain; it is not a prediction that a full browser search becomes 19% faster.
Legal expansion is only one part of complete progressive-series search.

## Certified Opera GX release evidence

The promoted single-thread WASM bundle was also exercised end to end in Opera
GX 134 (Chromium 150) from a real browser worker. All three D5 runs completed
with the same result: `f2f3`, score `+617`, 20 retained root bounds, a complete
PV, and the reply-mate safety gate passing.

| Browser run | Previous bundle | Candidate bundle | Speed change |
| --- | ---: | ---: | ---: |
| Warm, wave 8 | 33.567 s | 33.143 s | **+1.26%** |
| Warm, wave 4 | 36.988 s | 36.088 s | **+2.43%** |
| Cold, wave 8 | 32.947 s | 33.262 s | **-0.96%** |

The candidate passed the D1-D5 root-session oracle, the 14-case native/WASM
prefix differential, browser prefix-contract checks, and mate parity. It was
promoted as release `spc-browser-wasm-release-2f3281196de38ef9`, backed by root
session certificate `spc-root-session-5f603e5f10ce6b81`, prefix certificate
`spc-prefix-52ec6301a141b4b5`, and mate certificate
`spc-mate-b335d6effde48a90`.

The warm measurements moved in the expected direction, but the cold result did
not. Browser scheduling noise is large enough that this checkpoint does not
claim a consistent end-to-end D5 speedup.

## Rejected experiment

A legality-mask/frontier-reuse candidate preserved the exact semantic and work
signature but regressed the paired native D4 medians:

| Position | Baseline median | Mask-cache median | Median speed change |
| --- | ---: | ---: | ---: |
| Initial | 6.5503 s | 6.5938 s | **-0.664%** |
| Black after `e4` | 7.4101 s | 7.4391 s | **-0.391%** |

That candidate was rejected rather than combined with the accepted move-sort
change. Avoiding repeated legality work did not repay its added cache/mask
bookkeeping on these representative searches.

## Reproduction

From the repository root, with baseline and candidate packages already built:

```powershell
python benchmarks\cycle6_native_d4_benchmark.py `
  --baseline-package build\cycle6\baseline `
  --candidate-package build\cycle6\candidate-sort `
  --samples 7 `
  --output build\cycle6\paired-sort-d4-7.json
```

The benchmark exits with an error before reporting speed if output, proof, or
charged work differs.

## Limits of this checkpoint

- The full-search comparison covers native CPython, one thread, D4/width 32,
  and two opening boundaries. The separate Opera D5 measurements are mixed and
  do not establish a statistically stable timed-browser gain.
- The Emscripten microbenchmark isolates legal expansion under Node. The Opera
  receipts include the browser worker, but three runs are not enough to separate
  this small search-path gain reliably from scheduling and thermal noise.
- The accepted change preserves the search tree, evaluation, pruning, proof,
  and work counters. It is a modest speed optimization, not demonstrated
  playing-strength improvement.
- Nothing here establishes sub-10-second D5 performance or Stockfish-level
  playing strength.
