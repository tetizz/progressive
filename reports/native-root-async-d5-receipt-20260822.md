# Native root async D5 feasibility receipt

This isolated, start-position-only prototype completed the exact depth-5,
width-32 kernel result in **35.426074 seconds cold** on an AMD Ryzen 7 9800X3D.
It is a strong scheduling result, not a safe/product result.

## Result

- Best series: `b2b3`
- Score: `+951`
- PV: `b2b3 / f7f5-e8f7 / c1b2-e2e3-f1c4 /
  e7e6-f5f4-f4e3-e3f2 / e1f2-d1g4-f2e2-g1h3-g4g7`
- Selected-child proof bounds: `[-1, +1]`
- Root proof: unknown
- Anchor signature: exact match

The first four production-ordered candidates (`e3`, `d3`, `d4`, `e4`) ran
full-window concurrently in four persistent spawned workers. Their exact
scores reduced to `d4/+634`. Remaining candidates were dispatched one at a
time in production order with explicit incumbent epochs and `EXACT`, `UPPER`,
or `LOWER` results. The exact improvements arrived as `Na3/+836`, `f3/+847`,
and finally stale-epoch `b3/+951`. There were no lower, unknown, missing,
duplicate, identity, or epoch-invalid results.

## Timing and resource receipt

- Worker startup: `0.128608 s`
- Exact first wave: `12.225498 s`
- Dynamic phase: `23.021492 s`
- Search: `35.247065 s`
- Cold total: `35.426074 s`
- Aggregate work: `39,778,301 / 100,000,000`
- Per-worker cumulative work: `9,699,033`, `10,513,249`, `10,359,609`,
  `9,206,409`; every worker stayed below its fixed `20,000,000` allocation.
- Peak sampled aggregate RSS: `309,100,544` bytes (`294.78125 MiB`)
- Work-limit/deadline hits: zero

The run used four processes with four native threads each, a 65,536-entry
generation cache per worker, and one common 300-second absolute deadline.
Python was 3.14.4 on Windows 11 build 26200. The exact command and all summary
counters, dispatches, completion order, hashes, and worker assignments are in
[`native_root_async_d5_4x4_20260822.json`](../benchmarks/results/native_root_async_d5_4x4_20260822.json).

## What this proves

The no-serial-seed scheduling shape clears the 60-second kernel target with
24.574 seconds of observed headroom. It is 44.66% faster than the four-root
staged static-RR prototype and 47.13% faster than the one-root staged static-RR
prototype. It preserves the exact winning move, score, PV, full-window first
wave, mover-aware/canonical reduction, aggregate work cap, and fail-closed
response validation.

## Why it remains nonpublishable

`publishable`, `safety_certified`, `legal_series_certified`, and
`authoritative_replay_certified` are all false. This run omitted iterative
depths 1-4, Python's root reply-mate screen and retry/widening state machine,
and authoritative prefix replay. It injected `e2e3` as the known depth-4
preferred root.

The completion order is intentionally recorded as nondeterministic. Arrival
order changes which incumbent epoch later work sees, so redundant research,
cache state, work, and SearchStats are not deterministic even though the exact
winner is. That accounting conflict must be resolved in the production
ordinary-Worker design; this unsafe multiprocessing harness must not be folded
into release code.
