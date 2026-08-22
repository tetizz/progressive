# Opera persistent-root D5 regression receipt

The first exact combined-artifact D5 run was stopped by its semantic anchor.
It is regression evidence, not a performance or release certificate.

## Reproduction

- Browser: Opera GX `OPR/134.0.0.0`, headless Chromium `150.0.7871.187`
- Isolation: dedicated profile and CDP port `9241`
- Runtime: eight ordinary `DedicatedWorkerGlobalScope` ES-module Workers,
  pthreads disabled, `crossOriginIsolated:false`
- Search: starting position, iterative D1 through D5, width 32, initial full
  wave 4, aggregate work cap 100,000,000, reply-mate reserve 1,000,000
- Per-Worker caches: 65,536 series weight and 262,144 TT/eval entries
- Expected fresh-D5 anchor: `b2b3`, score `+951`
- Observed persistent result: `g1h3`, score `+1613`

The owning Worker completed a full-window `g1h3` threat re-search at `+1613`.
Its mandatory warm selected-owner certification then returned the same score
as a zero-work TT hit. At that point its exact accounted work was 61,932,654
(49,499,901 external plus 12,432,753 native). The harness rejected the move
before emitting a passing benchmark result.

## Bound artifact

- Source revision: `37bbee4f50370bd9ba6d2b9e4be97750a7e876fb`
- Source fingerprint: `b2d501e736f13415`
- Kernel SHA-256: `2498117b25555b96712af35ac579ce095f2014cb3713ce3b11d7ef240f05c577`
- WASM SHA-256: `3907cdcf5a5e27c09fba094fde5d2a8e2a8d184be39c071d616a8e07c66ae53b`
- Module SHA-256: `9887ea4032f353e88a6a225bb25ff698525a920b0ad72bade93db7211ed40a52`
- Artifact-set SHA-256: `3cf0399c25b413e2c4cd61f9a3589ef6700370a4faa5b9e220a19eb9b24b879a`

The ignored raw CDP receipt remains at
`build/root-session-wasm-matrix/baseline/opera-d5-wave4-run1-safety1m.json`.
It is 287,949 bytes with SHA-256
`08eca1507c691a4b7b9de2663c57d02409469c4315ac9a7fee3d23b5c52f35f2`.

## Diagnosis and gate

The first diagnosis found a real transposition-table iterator lifetime bug: a
map iterator could survive recursive inserts that rehashed the table, then be
dereferenced for replacement or preferred-PV data. An ownership-safe build
removed that undefined behavior but still reproduced `g1h3/+1613`, so this
receipt must not attribute the semantic drift to that bug alone.

The remaining failure is currently isolated to persistent distributed
coordinator/PVS execution; a small cold-versus-warm native corpus, rolled-back
scout-to-full searches, committed WorkLimit retry, stale-manifest replacement,
and synthetic eight-Worker response-order permutations all pass. Those are
necessary regression gates, not evidence that the width-32 D5 failure is fixed.

No further compiler or seed-wave timing may use this artifact. The combined
WASM must be rebuilt from a validated semantic fix, reproduce `b2b3/+951`, pass
selected-owner exact certification and compiled mate/prefix safety, and only
then restart the quiet Opera matrix. `product_publishable` remains false.
