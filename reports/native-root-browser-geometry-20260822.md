# Native proxy for ordinary browser Workers

Eight persistent single-thread processes are the closest native proxy for the
ordinary Web Workers available in Opera without `SharedArrayBuffer`. At exact
start-position width 32, depth 5, the cache size changed both the speed result
and the release decision materially.

| Cache per worker | Exact total | Work | Peak aggregate RSS | Under 60s |
| ---: | ---: | ---: | ---: | :---: |
| 65,536 entries | **44.813923s** | 52,616,728 | 516.81 MiB | yes |
| 16,384 entries | 64.110696s | 65,984,603 | 365.27 MiB | no |

Both runs returned the exact `b2b3`, `+951` anchor and completed all 20
retained candidates with no deadline or work-limit failure. The 65K cache was
30.10% faster and used 20.26% less search work, at a cost of about 151.54 MiB
more peak aggregate resident memory.

This rejects the current 16K-per-worker browser configuration for a cold
under-60 D5 gate. A desktop certificate should test at least the 65K lane and
must bind the real per-Worker WebAssembly heap plus a global admission cap.
Mobile must use a separately honest worker/cache/depth envelope rather than
silently allocating the desktop pool.

These are native proxy runs, not Opera results. They do not include iterative
depths, exact root mate-safety retries, compiled replay, Worker/module startup,
or JavaScript coordination. The real browser certificate remains closed until
those layers complete under one deadline/work/memory ledger.

Machine-readable evidence is in
[`native_root_browser_geometry_8x1_20260822.json`](../benchmarks/results/native_root_browser_geometry_8x1_20260822.json).
