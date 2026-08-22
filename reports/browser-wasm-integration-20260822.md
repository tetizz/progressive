# Browser WebAssembly integration handoff — 2026-08-22

## Outcome

This lab contains a fail-closed browser WebAssembly shell for the Progressive
engine. It does not contain a publishable engine bundle. In particular, no
under-60-second width-32 depth-5 certificate has been created or inferred from
an incomplete search. Render remains the production fallback and its existing
deployment gate remains in the Pages workflow.

The browser lane can become selectable only after the checked-out engine source,
single-thread WASM binary, exact Emscripten wrapper bytes, manifest, certificate,
analysis envelope, and memory envelope all validate. The current experimental
kernel is not copied into the static site because it is uncertified.

The separate compiled prefix-ABI workstream owns local human micro-move routing.
This shell accepts an engine move only when the search result includes a complete
compiled authoritative replay. It does not weaken `/api/prefix` for boundaries
that are not covered by that separately integrated ABI.

## Files and trust root

- `browser-engine-client.js` validates worker identity, certified request limits,
  replay completeness, result identity, memory receipts, deadlines, and crashes.
- `browser-engine-worker.js` owns one persistent certified module instance and
  the probe/analyze message boundary.
- `wasm-kernel-adapter.js` validates the manifest and certificate, hashes the
  fetched binary and wrapper, imports the verified wrapper bytes, instantiates
  the verified WASM bytes, and calls ABI version 1.
- `build_browser_wasm_bundle.py` builds and revalidates a source-bound bundle.
- `app.js` shares one absolute play-search deadline across local execution,
  permitted fallback, Render fetch/body parsing, and retry waits.
- `pages.yml` requires the browser assets and validates the complete engine bundle
  before upload while retaining the matching-Render deployment gate.

The deployment commit and same-origin Pages artifact are the trust root. Artifact
hashes prevent the executed bytes from drifting from the reviewed manifest; they
are not a signature against an attacker who can replace the entire deployment.

## Certificate and request envelope

The certificate must use `spc-browser-wasm-certificate-v1`, status `certified`,
contract version 1, ABI version 1, the exact checked-out source fingerprint,
and exact wrapper/WASM hashes. It must also bind the engine profile, ruleset,
single-thread runtime, support-file list, analysis limits, memory limits, positive
differential case count, safety gates, and a real completed width-32 depth-5
receipt below 60 seconds.

The builder and runtime reject out-of-range depth, width, duration, and work caps.
The client independently refuses requests outside the certified analysis envelope.
Pthread publication and selection are disabled until its wrapper and bootstrap
support code can be executed from equivalently verified bytes.

The current worker request is:

```text
{
  contract_version,
  request_id,
  boundary: {
    fen, series, quiet_series, ep_targets,
    promoted_hex, chess960: false, prefix: []
  },
  limits: {
    depth, max_series, time_limit_seconds,
    max_generation_positions, best_move_only: true
  }
}
```

## Publication and replay

A local result is publishable only when its source, WASM hash, wrapper hash,
certificate ID, runtime, and thread count remain pinned and all of these are true:

```text
ok
&& publishable
&& safety_certified
&& legal_series_certified
&& authoritative_replay_certified
&& legal_validation_runtime == "compiled-wasm"
&& 1 <= completed_depth <= requested_depth
```

The client also requires the returned requested depth to equal the requested
depth. It does not relabel an interrupted depth-5 search as a depth-5 completion.

`checked_prefix` must reproduce the exact original boundary, including progressive
en-passant targets, promoted-piece provenance, quiet clock, and orthodox mode. Its
UCI, SAN, and frame arrays must align one-for-one. The final frame must match the
reported final board position, apart from the explicit progressive en-passant
representation. A non-terminal replay must provide a complete next state with the
exact final FEN, next series, quiet clock, en-passant targets, promoted bitboard,
side-to-move transition, and `chess960: false`. Missing or inconsistent state is
rejected before the app can play it.

## Wrapper execution and CSP

The adapter downloads both the WASM binary and single-lane module wrapper as
bytes, hashes both with SHA-256, and imports the verified wrapper from a temporary
Blob URL. It never hashes one wrapper and then imports a second URL by name. The
single lane may not declare external support scripts.

The static CSP permits this exact wrapper execution path while keeping worker
creation same-origin:

```text
script-src 'self' blob: 'wasm-unsafe-eval'; worker-src 'self'
```

All browser URLs are relative to the deployed scripts, so GitHub Pages project
subpaths are preserved. The Pages artifact builder versions document scripts
without converting them to root-relative paths.

## Deadline, cancellation, and crash behavior

Play creates one absolute monotonic analysis deadline. Local probe/search time,
any allowed Render fallback, response-body parsing, service-wake retries, and
queue retries all consume that same budget. Expiry or user cancellation terminates
the synchronous WASM worker and does not start another full search. A local
certification or availability failure may fall back only with time still remaining.

Unexpected `error` or `messageerror` events immediately set `ready = false`,
terminate the worker, and reject its pending calls. The pinned identity is retained
only to require an identical fresh probe before another local analysis. Transient
worker crashes, post failures, and probe timeouts do not make a stale worker ready.

## Memory contract

Every certificate declares page-aligned `initial_bytes`, `maximum_bytes`,
`estimated_peak_bytes`, and `growth_enabled`. Current hard JavaScript/builder caps
are 128 MiB initial, 192 MiB estimated peak, and 256 MiB maximum. The builder
rejects inconsistent or excessive declarations.

After module initialization, the adapter compares `HEAPU8.buffer.byteLength`
exactly with the certified initial size. It checks the current heap again after
every search against the certified peak and maximum, and the client validates the
reported current heap before accepting a result. WebAssembly's declared maximum
is not reliably introspectable from an instantiated Emscripten module, so the
maximum remains certificate-bound build evidence plus a hard JavaScript cap; it
is not described as runtime-proven.

## Deployment state

Pages now requires `engine/browser-engine-manifest.json` and runs
`build_browser_wasm_bundle.py --validate-existing` before upload. Validation
recomputes the checked-out source fingerprint and artifact hashes, requires exactly
the single certified lane, rejects extra files, and revalidates the certificate.

No such certified bundle is present in this lab. Therefore the workflow is wired
to stop rather than silently deploy an uncertified or incomplete browser engine.
The Render fingerprint/limits readiness gate remains in place until a real bundle
and the separate prefix integration are released together.

## Verification

The focused browser shell tests cover bundle validation and drift, certificate
and memory caps, pthread rejection, request limits, full replay state, verified
wrapper/CSP wiring, hard cancellation, absolute deadline termination, worker crash
reprobe, promoted provenance, Pages paths, and fail-closed UI wiring. JavaScript
syntax, Python compilation, workflow embedded-Python syntax, patch whitespace, and
staged secret checks are release checks for this commit.

```text
25 passed in 0.90s
```

No performance benchmark was run for this hardening change, and no product speed
or strength claim is made from the shell alone.
