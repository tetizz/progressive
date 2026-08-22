# Progressive browser-WASM lab receipt

This is an isolated lab receipt, not a release claim. No release repository was
edited, committed, pushed, or deployed from this lane.

## Source and toolchain

- Detached source base: `5b926d689ae82cd5f0bc998d8ba78d7b2edb0755`
- Emscripten: 6.0.8 (`aeb67926e7de656da38bc807d83050af93578758`)
- Binaryen `wasm-opt`: 132
- Opera GX proof binary: 134.0.5954.67
- Browser kernel weights: all five fast and all seven full-evaluation weights
  are 100.
- Search receipts below are a direct single-depth C++ subtree kernel. They do
  not include Python iterative deepening, root widening, or reply-mate safety.

## Build and verification commands

```powershell
& .\benchmarks\build_native_mate_wasm.ps1 `
  -EmPlusPlus '.\.emsdk\upstream\emscripten\em++.exe'
python .\benchmarks\verify_native_mate_wasm.py

& .\benchmarks\build_native_subtree_wasm.ps1 `
  -EmPlusPlus '.\.emsdk\upstream\emscripten\em++.exe'
$env:PYTHONPATH = (Resolve-Path 'src').Path
python .\benchmarks\verify_native_prefix_wasm.py
```

Receipts:

```json
{"cases":5,"exhausted":1,"found":2,"live_s5_stats":{"checking_series":579,"checkmates":1,"max_depth_reached":5,"moves_generated":24006,"peak_frontier":421,"positions_visited":600,"transpositions_merged":472},"schema":"spc-mate-wasm-receipt-v1","unknown":2}
{"cases":14,"exact_python_parity":9,"fail_closed_errors":3,"mate_replay":"checkmate","multi_ep":"covered","progressive_san_corrections":2,"schema":"spc-prefix-parity-receipt-v1"}
```

## One-series mate proof ABI

Export:

```c
const char* spc_series_mate_search_json(
  const char* fen,
  int32_t series_number,
  const char* progressive_ep,
  const char* promoted_hex,
  uint32_t max_positions,
  uint32_t max_work,
  uint32_t time_limit_ms
);
```

`proof_status` is exactly one of `found`, `exhausted`, or `unknown`.
Work/deadline/unsupported requests fail closed as `unknown` and
`complete:false`.

- S5 live boundary: found
  `c3d5/d3e4/e4h7/d5f4/h7g6`, with the exact stats in the receipt.
- S17 boundary: found `h8b2/f8a3`.
- Bare-kings S3 boundary: exhausted.
- S5 with `max_positions=1`: unknown/work-limit.
- S5 with `max_work=100`: unknown/work-limit.

The independent prefix/rules ABI replayed the S5 result as
`Nd5 / Bxe4 / Bxh7 / Nf4 / Bg6#`, `ended_by_check:true`, and
`outcome:"checkmate"`.

Opera GX then loaded both ABI modules in a real module Worker. The smoke
receipt was `proof_status:"found"` with the same S5 line/stats, plus the SAN
edge replay `g4 / c4 / Bf4+`, `outcome:null`, and retained EP target `g3`.
Module load was 13.7 ms and the whole smoke was 35.5 ms; these timings are only
load/smoke evidence, not a search-performance benchmark. The browser reported
Opera 134, 16 logical CPUs, and `crossOriginIsolated:false`; these two ABIs are
single-threaded and do not require SharedArrayBuffer.

## Prefix and legal-continuation ABI

Export:

```c
const char* spc_boundary_prefix_json(
  const char* fen,
  int32_t series_number,
  int32_t quiet_series,
  const char* progressive_ep,
  const char* promoted_hex,
  const char* prefix_uci
);
```

The slash-separated prefix is replayed from the trusted boundary. The response
contains FEN/frames, UCI and SAN, legal-next payloads, clocks, Progressive EP,
completion reason, terminal outcome, and the exact next boundary. Invalid
boundary, illegal move, and overlong prefix each fail closed with `ok:false`.

Exact Python parity passed for starting legal moves, `e2e4` handoff, early
countercheck, two retained Progressive EP targets, first-move multi-EP replies,
castling, promotion check, and the native mate replay.

The differential suite also confirms a pre-existing Python SAN defect and the
WASM correction in both legal-next and completed-prefix payloads:

```text
boundary: 8/8/8/8/1p5p/7b/2PB1KPk/7b w - - 0 1
series:   3
prefix:   g2g4/c2c4
move:     d2f4
Python:   Bf4#
WASM:     Bf4+
```

After `g2g4/c2c4/d2f4`, the next boundary retains `g3`; Black's only legal
reply is `h4g3`. Therefore `+` is correct and `#` is not. The WASM suffix uses
the fully updated pending Progressive EP set, not only the orthodox EP square
created by the final micro-move.

## Artifact manifest

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `build/mate-wasm/spc-series-mate.js` | 20,383 | `CE389D8B96D410D6C629D821AD5C36DDCA766F707CF611497144405AD5E6836B` |
| `build/mate-wasm/spc-series-mate.wasm` | 235,532 | `5F903E568AEB5EFBBBB267FC835D51699017506E91725843F93D4723D222A6BF` |
| `build/native-subtree-wasm/spc-start-kernel.js` | 23,383 | `F2C2EDC16AFE71F3557D2C4B345669C27792265FC5D98E0A8B5FB32A13C20FC5` |
| `build/native-subtree-wasm/spc-start-kernel.wasm` | 468,331 | `CAC1647E554939ACAE7393B52C56C4CE61420D72EC318DA150AACE8279B13A7B` |

## D4 compile-factor evidence (preserved; not rerun)

All variants used W32, cache capacity 65,536, one worker, and produced the same
completed D4 score, PV, and work receipt: score -1308; 2,319,913 generation
positions; PV
`e2e3 / f7f6 e8f7 / f1b5 b5d7 d7c8 / f6f5 f5f4 f4e3 e3f2`.

| Factor | Cold | Warm | WASM SHA-256 |
|---|---:|---:|---|
| O3 baseline | 14.1426 s | 12.3716 s | `37F05CB791B9CDD84B78D679093FCBBEE6B4BC9D0FCDB2BDBB79387AED05565B` |
| O3 + LTO | 10.1671 s | 10.5663 s | `3AB5A8DA7B43682F668469CD2642F4F382A6E07BBFFE8C72AF14BB8FD8807F4E` |
| O3 + SIMD | 12.1037 s | 12.1086 s | `E69C7F7680D787470D102E61B2995BCA66C925B5D1C0CB0B01125D923576AAB3` |
| O3 + LTO + SIMD | 12.1804 s | 12.7861 s | `8545A6C5C586A74ACCB6B92F0864B4747523460090713F95153C9ECD1491F066` |
| O3 + LTO then wasm-opt -O4 | 11.1218 s | 10.5978 s | `3B1ACE025EA916765FCF78F561EB28A8399CA8CE1C0BEC3420292AEB62000D5D` |

Only LTO won. SIMD, LTO+SIMD, and post-link `wasm-opt -O4` were rejected.
These are machine timings, not product-parity claims.

The factor recipe was the common Emscripten command
`_native_eval.cpp native_subtree.cpp wasm_probe.cpp -std=c++20 -O3
-fexceptions -DSPC_NATIVE_CORE_ONLY=1 -sALLOW_MEMORY_GROWTH=1
-sENVIRONMENT=node`, with one factor added at a time: `-flto`, `-msimd128`,
both, or post-link `wasm-opt -O4`. `wasm_probe.cpp` was a scratch timing harness
and is deliberately excluded from the integration commit. The receipt
artifacts predate the prefix ABI's three public legality wrappers, so rebuilding
from the current lab source is expected to change their byte hashes even with
identical flags.

## Existing browser proof (preserved; not rerun)

Opera GX loaded the real worker module and completed D5/W32/100M in
195,860.5 ms with the exact deterministic kernel result: score +951, work
36,956,874, and PV
`b2b3 / f7f5 e8f7 / c1b2 e2e3 f1c4 / e7e6 f5f4 f4e3 e3f2 / e1f2 d1g4 f2e2 g1h3 g4g7`.
The response correctly remained `safety_certified:false` and
`safety_status:"not_screened"`. This is proof that the real kernel runs in
Opera, not evidence of full-product safety or an under-60-second Strong mode.

## Browser shell integration status (2026-08-22)

The browser shell now has an independent, fail-closed prefix capability path.
Its bundle builder accepts a prefix-only certificate or a separate search
certificate plus prefix certificate. Both certificate types are artifact-bound;
when they coexist they must agree on artifact, runtime, thread count, engine,
ruleset, support files, and the same capped memory envelope. A prefix
certificate does not make search selectable.

At runtime the adapter verifies the manifest, wrapper bytes, WASM bytes, native
prefix ABI contract, current/initial heap, and certificate identity before it
reports `prefix_ready:true`. The Worker and client route exact supported
`/api/prefix` requests locally, terminate synchronous WASM on cancellation,
reprobe after a crash, and only use the hosted endpoint when the unchanged
request and exact engine/rules/source authority can be preserved. Hosted prefix
responses now echo that authority. Progressive EP, promoted provenance, and
`chess960:false` travel through principal-variation replay.

Runtime checks prove the instantiated initial/current heap size. WebAssembly's
declared maximum is not introspectable through this wrapper; it remains
certificate-bound build evidence under the hard JavaScript cap, not a claimed
runtime-proven maximum.

This is source integration, not a release certificate. There is no browser
engine manifest or certified WASM bundle in the static tree yet. The Pages
workflow deliberately fails if those assets or hashes are absent or drifted,
and the Render deployment-identity gate remains in place. Pthreads remain
unselectable until their wrapper/bootstrap bytes can be bound equivalently.
Search certification remains completely separate; no under-60-second Strong
certificate is asserted here.

## Integration files and remaining gates

Integrate these source files and builders:

- `src/scottish_progressive/_native_mate.cpp`
- `src/scottish_progressive/native_eval.hpp`
- `src/scottish_progressive/_native_eval.cpp`
- `src/scottish_progressive/native_subtree_wasm.hpp`
- `src/scottish_progressive/native_subtree_wasm.cpp`
- `benchmarks/build_native_mate_wasm.ps1`
- `benchmarks/build_native_subtree_wasm.ps1`
- `benchmarks/verify_native_mate_wasm.py`
- `benchmarks/verify_native_prefix_wasm.py`
- `mate_batch_probe.mjs`
- `prefix_batch_probe.mjs`
- `abi-proof-worker.mjs`
- `abi-proof.html`

Remaining fail-closed gates:

1. A `found` mate must be replayed through the independent prefix ABI before
   it can certify a selected root; limit/deadline results remain unknown.
2. The public search facade remains `safety_certified:false` until the root
   reply-mate screen/retry state machine calls this proof ABI for every selected
   root.
3. A release build must compile the current native facade and canonical native
   subtree into a single-lane WASM artifact, then issue an artifact-bound prefix
   certificate from the required parity/evidence run. The canonical
   `native_subtree.cpp`/`.hpp` sources are not present in this checkout, so no
   bundle was fabricated.
4. The Python SAN suffix bug above should be fixed or explicitly normalized so
   server fallback and WASM do not display conflicting notation.
5. Engine/ruleset/source identity is supplied and checked by the production
   adapter and hosted prefix route. PFEN and position hash remain absent from
   the lab C ABI and would need an explicit contract before any UI depends on
   them.
6. Standard KQkq castling is supported; Chess960 is deliberately rejected.
7. No release artifact should be called Strong until the exact W32 public
   safety path, not just this kernel, passes its release gate.
