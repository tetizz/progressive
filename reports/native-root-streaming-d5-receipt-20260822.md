# Streaming native root D5 feasibility receipt

Reusing each finished first-wave worker immediately completed the exact start
position width-32 depth-5 kernel in **32.971224 seconds**. The result remained
`b2b3`, score `+951`, with the same complete principal variation and all 20
retained candidates accounted for.

This is 6.93% faster than the prior 35.426074-second barrier scheduler. It also
used slightly less aggregate work: 39,628,874 instead of 39,778,301 positions.
The gain comes from eliminating idle time while the slower exact seeds are
still running, not from relaxing depth, width, work accounting, or the anchor.

## Receipt

- Search: `32.787781 s`
- Fresh-worker total: `32.971224 s`
- Last exact first-wave result: `11.768449 s`
- Aggregate work: `39,628,874 / 100,000,000`
- Per-worker work: `10,901,601`, `9,188,110`, `9,373,295`, `10,165,867`
- Peak sampled aggregate RSS: `308,146,176` bytes (`293.871094 MiB`)
- Deadline/work-limit hits: zero
- Environment: AMD Ryzen 7 9800X3D, Windows 11 build 26200, Python 3.14.4

The four initial exact searches were `e3`, `d3`, `d4`, and `e4`. `e4/+412`
finished first, so that worker immediately started `f3` instead of waiting.
`d4/+634` then raised the incumbent, followed by `f3/+847` and finally
`b3/+951`. Explicit stale-epoch bounds remained valid, and every improving
scout was exact before it could replace the incumbent.

## Boundary of the claim

This remains a native scheduling proof, not a browser or product certificate.
The worker sessions and transposition tables started empty, but the operating
system file cache was not flushed. The run still omits iterative depths 1-4,
the authoritative reply-mate safety/retry loop, final compiled replay, and real
Opera WebAssembly execution. Consequently `publishable`, `safety_certified`,
`legal_series_certified`, and `authoritative_replay_certified` are all false.

The production coordinator must additionally enforce a shared absolute
deadline, native per-call work credits under one reservation ledger, exact
White/Black bound mirrors, terminal roots, safety revisions distinct from
monotonic incumbent epochs, and fail-closed coverage before publishing a move.

The machine-readable summary is
[`native_root_streaming_d5_4x4_20260822.json`](../benchmarks/results/native_root_streaming_d5_4x4_20260822.json).
