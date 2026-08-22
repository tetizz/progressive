# Browser WebAssembly integration handoff — 2026-08-22

## Current outcome

This branch contains a fail-closed browser shell for a certified Progressive
root-search bundle. It does **not** contain a release WASM bundle or certificate,
and it makes no under-60-second depth-5 claim. The static `engine/` directory is
absent, so the Pages bundle-validation gate correctly remains closed.

The implemented local lane uses persistent ordinary module Workers, not
pthreads. A desktop certificate binds eight single-thread Workers with a
four-Worker initial full-window wave. Certified lower geometries may be selected
when they fit reported hardware and memory; unknown device memory never admits
the desktop geometry.

## Trust and capability gates

The builder and runtime require three separately truthful capabilities in one
byte-identical artifact before local root search is selectable:

- compiled prefix replay, including exact Progressive EP and promoted provenance;
- root-session ABI v2 for enumerate, exact manifest import, and candidate search;
- reply-mate ABI v1 with Python-parity evidence.

Root-session certification alone explicitly has `reply_mate_safety:false` and
`product_publishable:false`. Publication requires the separate mate and prefix
certificates, identical artifact/kernel/runtime/engine/memory identity, the exact
combined export list, and a full runtime contract match. Pthreads stay disabled.

The certificate also binds the exact session config, Worker geometry, memory
envelope, and `geometry.play_limits`:

```text
maximum_seconds
default_seconds
default_generation_positions
safety_reserve_positions
```

The live Strong button takes its depth-5 time/work defaults from those promoted
fields. It no longer inherits legacy Render limits or a hardcoded 10M work cap.
No defaults are promoted in this branch because there is no real certificate.

## D1 through D5 root execution

Each play request creates a fresh native session in every retained Worker, then
keeps those sessions alive throughout iterative D1→D5. Worker modules remain
loaded across game turns, while the old native sessions are destroyed and new
sessions bind the next exact boundary and deadline. This preserves module/code
warmth without leaking request boundary, generation, or work state.

At every depth the primary session authoritatively enumerates the exact root
manifest once. Peers import that manifest byte-structurally, including
`preferred_series` and each candidate's exact child boundary. The coordinator
runs the certified initial full wave, streams scouts after the first exact
result, retains worker affinity for re-search, and performs the final full-window
certification on the selected candidate's warm owning Worker.

Scout-pruned alternatives may remain alpha-beta bounds. A result can publish
when root bound coverage is complete even when every alternative does not have
an exact score. The UI says “Best move exact; alternatives certified by
alpha-beta bounds” in that case. Missing bound coverage fails closed. An
immediate checkmate can publish with other retained candidates without claiming
that all root scores are exact.

Only the last fully completed depth is exposed. Every selected series is replayed
through the compiled prefix ABI from the original boundary, and the exact final
state must match the enumerated child boundary before the result reaches the app.

## Reply-mate safety cache

Complete `found` and `exhausted` reply-mate proofs are cached by the exact
authoritative child boundary plus source, artifact, kernel, module, certificate,
engine, ruleset, profile, runtime, thread, ABI, and mate-score identity. `unknown`
is never cached. A cache hit charges zero mate-search work and rebinds the current
task envelope. A cached `found` line is still freshly replayed through the
compiled prefix ABI with the current request ID before it can affect the root.

The cache is bounded to 256 entries and is cleared when the browser engine client
closes. It may survive compatible game turns because every lookup is exact-keyed;
an incompatible Worker probe or identity change fails closed.

## Work, deadline, cancellation, and crash contracts

Native root work and separately consumed mate-safety work share one cumulative
global ledger. Enumerate and import calls include prior safety work in their
`external_work` snapshots, preventing D2+ external-work regression. The
certificate-bound safety reserve reaches the coordinator directly, bounded only
by the work remaining while retaining at least one search credit.

The Window transports an absolute epoch deadline derived from its monotonic time
origin. Each Worker clamps caller-reported remaining time against its own
`performance.timeOrigin + performance.now()` and strips the transport-only epoch
before calling native code. Raw `performance.now()` values are never compared
across Window and Worker contexts.

User cancellation terminates synchronous Workers and never starts hosted
fallback. Deadline expiry shares the same absolute budget and cannot start a
second full search. A crash discards the whole pool; the last already certified
depth may be returned, and the next request must create and probe a replacement
pool. An idle retained root Worker handles local human prefix replay, avoiding an
uncertified JavaScript legality path and avoiding an extra N+1 WASM heap.

Hosted analysis and prefix remain optional fallbacks. Their responses must echo
the already loaded source/engine/rules/profile authority; stale Render identity
cannot contaminate a local result. Pages deployment no longer waits for Render.

## Memory, wrapper bytes, CSP, and Pages paths

Every capability has the same page-aligned memory envelope under 128 MiB initial,
192 MiB estimated-peak, and 256 MiB maximum hard caps. The adapter validates the
instantiated initial/current Emscripten heap,
and the root runner admits only a certificate-bound aggregate Worker geometry.
WebAssembly maximum memory is certificate-bound build evidence plus a hard
JavaScript cap; it is not described as runtime-introspected proof.

The module wrapper and WASM bytes are fetched and SHA-256 checked before use. The
verified single-lane wrapper bytes execute from a temporary Blob URL. CSP permits
that exact path with `script-src 'self' blob: 'wasm-unsafe-eval'`, while
`worker-src 'self'` keeps ordinary Worker creation same-origin. All script and
artifact URLs remain relative for GitHub Pages project paths, and the Pages build
versions the coordinator and root-client assets with the deployment commit.

## Verification state

Focused checks cover strict coordinator/root-series mutation rejection, the
builder/runtime root-prefix-mate manifest boundary, an eight-Worker mock pool,
4-of-8 initial dispatch, streamed scouts, fresh sessions across two turns,
pooled prefix replay, cumulative D1→D2 safety work, D1→D5 complete-proof cache,
UNKNOWN non-caching, White/Black mate mapping, immediate mate publication,
incomplete bound rejection, mismatched time origins, crash last-safe/reprobe,
Pages load order/CSP/versioning, and independent Pages deployment.

These are contract and mock-Worker tests, not a D5 performance receipt. Real
Opera timing and product promotion remain blocked on corrected native semantics,
the complete combined artifact, exact differential/parity evidence, memory
evidence, and all certificate gates.
