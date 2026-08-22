# Browser `/api/prefix` integration plan

Status: implementation contract and smoke verifier are ready in this lab, but
the browser shell has not been edited and this is not a product-safety claim.
Commit `116f1298454cea04d188eeb5815a71ca6b1a43af` is also not a standalone
build: its facade includes `native_subtree.hpp`, while the canonical
`native_subtree.cpp/.hpp` are still pending from the native-core lane.

The inspected browser snapshot currently routes only `/api/analyze` locally;
its Worker accepts only `probe` and `analyze`, its adapter requires only the
search ABI, and all seven `/api/prefix` call sites remain hosted. The PV-frame
call is the one ordinary caller that drops `promoted_hex` and `chess960` instead
of forwarding its cursor boundary. Hosted prefix payloads include engine and
ruleset versions but not the source fingerprint needed for an identity-bound
fallback.

## Route contract

Only exact ordinary Progressive boundaries are eligible for local replay:

- six-field FEN, series, quiet-series, and at most eight Progressive EP targets;
- exact `promoted_hex` provenance (one to sixteen hex digits, canonicalized to
  sixteen lower-case digits);
- explicit `chess960: false`;
- an array of canonical lower-case UCI micro-moves no longer than the current
  series budget; and
- every structural value inside the prefix certificate's limits.

There is no clamping. Missing promotion provenance, Chess960, disagreement
between `ep_targets` and `progressive_ep`, or any over-limit field makes the
request ineligible for local replay. The original request is then sent to the
authoritative endpoint unchanged. The current endpoint also rejects Chess960,
so fallback preserves that error rather than silently treating it as orthodox.

`browser-prefix-contract.js` is a copy-ready pure contract layer. It performs
request normalization, exact response/identity validation, and fallback
routing. It deliberately does not attach `safety_certified` to prefix replay:
legal replay certification and search move-safety certification are separate
claims.

## Browser-shell patch map

All paths below are relative to
`src/scottish_progressive/web/static` in the browser shell.

Expected target files are:

- `scripts/build_browser_wasm_bundle.py` for the independent prefix
  certificate and limit gate;
- `src/scottish_progressive/web/static/wasm-kernel-adapter.js`;
- `src/scottish_progressive/web/static/browser-engine-worker.js`;
- `src/scottish_progressive/web/static/browser-engine-client.js`;
- `src/scottish_progressive/web/static/browser-prefix-contract.js` (copy this
  lab module) and `index.html` for its load order;
- `src/scottish_progressive/web/static/app.js` for routing and PV cursor
  identity propagation;
- `src/scottish_progressive/webapp.py` so hosted prefix replies repeat source,
  engine, and ruleset identity; and
- `tests/test_browser_wasm.py`, `tests/test_webapp.py`, and
  `tests/test_web_assets.py` for the gates below.

### 1. Artifact certificate and bundle builder

Add a prefix capability independently of the search safety certificate. A
variant may be usable for prefix replay even while `analyze` remains hosted.
Bind the capability to the same source, WASM, module-JS, runtime variant, and
support-file hashes as the variant:

```json
{
  "prefix_certificate": {
    "status": "certified",
    "contract_version": 1,
    "certificate_id": "...",
    "source_fingerprint": "...",
    "wasm_sha256": "...",
    "module_js_sha256": "...",
    "runtime_variant": "single",
    "thread_count": 1,
    "evidence": {
      "failures": 0,
      "compiled_prefix_replay": true,
      "multi_ep_san": true,
      "illegal_prefix_fail_closed": true,
      "differential_cases": 1
    },
    "prefix_contract": {
      "schema": "spc-boundary-prefix-contract-v1",
      "result_schema": "spc-boundary-prefix-v1",
      "abi_version": 1,
      "chess960": false,
      "promoted_hex_required_for_product": true,
      "limits": {
        "maximum_fen_utf8_bytes": 512,
        "maximum_series_number": 256,
        "maximum_quiet_series": 1000000,
        "maximum_ep_targets": 8,
        "maximum_ep_utf8_bytes": 23,
        "maximum_prefix_moves": 256,
        "maximum_prefix_utf8_bytes": 1535,
        "maximum_uci_move_bytes": 5,
        "maximum_promoted_hex_bytes": 18
      }
    }
  }
}
```

The example's `differential_cases: 1` is a schema minimum, not an acceptable
release threshold. Release policy must set and enforce the real required count.
The builder must reject missing evidence, a limit above the ABI hard envelope,
or any certificate/artifact identity mismatch. The adapter must compare the
certificate limits with `_spc_boundary_prefix_contract_json()` at load time;
the certificate may be stricter but never broader.

### 2. `wasm-kernel-adapter.js`

Require these additional exports before marking prefix replay available:

```js
module._spc_boundary_prefix_json
module._spc_boundary_prefix_contract_json
```

Parse the native contract JSON once, compare it with the identity-bound prefix
certificate, and expose `identity.prefix_ready` plus
`identity.prefix_contract`. Add `kernel.inspectPrefix(request)` which:

1. validates the request again against `identity.prefix_contract` (the Worker,
   not the caller, owns the effective limits);
2. allocates exact FEN, comma-joined EP targets or `-`, canonical promoted mask,
   and slash-joined prefix;
3. calls `_spc_boundary_prefix_json` synchronously;
4. frees every input allocation in `finally`;
5. parses JSON, requiring `spc-boundary-prefix-v1`, ABI 1, and `ok: true`; and
6. attaches `request_id`, source/WASM/module hashes, prefix certificate ID,
   engine/ruleset versions, runtime variant, and thread count.

An ABI `ok:false`, null pointer, invalid JSON, contract mismatch, or over-limit
request becomes a fallback-required error. Never transform it into a legal
payload. Result validation also requires the full next-boundary schema, the
correct handoff series (or same-series terminal stuck state), canonical unique
EP targets, exact promoted/Chess960 identity, coherent outcome/completion
fields, and final-frame board consistency.

### 3. `browser-engine-worker.js`

Add a `prefix` message next to `probe` and `analyze`:

```js
if (message.type === "prefix") {
  if (!kernelPromise) throw notReadyError();
  const kernel = await kernelPromise;
  const result = await kernel.inspectPrefix(message.payload);
  self.postMessage({ id, ok: true, payload: result });
  return;
}
```

Do not implement a queued cancel message. Emscripten replay is synchronous, so
the existing client-side Worker termination remains the cancellation boundary.

### 4. `browser-engine-client.js`

Copy the normalization and validation logic from `browser-prefix-contract.js`
into the shipped client or load that file before the client. Add:

- `canInspectPrefix(payload)`: true only when the loaded identity has a valid
  prefix certificate, no search is active, and normalization succeeds;
- `inspectPrefix(payload, {signal})`: normalize with a unique request ID, call
  `_call("prefix", ...)`, validate exact boundary/prefix/artifact identity, and
  return the validated payload; and
- capability-aware preflight: `canAnalyze` still requires search certification,
  while prefix replay only requires the independent prefix certificate.

Do not queue prefix work behind an active synchronous search. Return a
fallback-required busy error. `_call` already supplies the needed hard
cancellation: abort removes the pending request, terminates the Worker,
increments its generation, rejects other work, and marks preflight stale.

After Worker termination, prefix or analysis must preflight the newly created
Worker again. Generation checks already suppress replies from the old Worker.

### 5. `app.js`

Extend `requestJson` before the `/api/analyze` branch. Use the contract router,
with an identity-bound hosted callback, rather than a raw `fetch` fallback:

```js
if (path === "/api/prefix" && browserEngineClient && options.body !== undefined) {
  const originalBody = typeof options.body === "string"
    ? JSON.parse(options.body)
    : options.body;
  return BROWSER_PREFIX.routePrefixRequest({
    payload: originalBody,
    signal: options.signal,
    localClient: browserEngineClient,
    remote: {
      identity: currentPrefixAuthority(),
      request: (body, { signal }) => requestRemoteJson("/api/prefix", {
        ...options,
        signal,
        body: JSON.stringify(body),
      }),
    },
  });
}
```

The hosted `/api/prefix` response must include `source_fingerprint`,
`engine_version`, and `ruleset_version`; `currentPrefixAuthority()` comes from
the already checked health/local manifest identity. The router requires the
declared hosted identity to equal the selected local identity and requires the
returned response to repeat that identity. If the server has not supplied an
identity-bound callback, fallback is unavailable. This prevents one request
from silently mixing local legality with a different deployed ruleset.

Never fallback after cancellation: otherwise an aborted stale position can
start a new hosted request. For requests that never select the local router
(for example a browser with no Worker support), the existing server-only path
may continue directly after its normal health identity check.

The current call at the PV-frame replay loop omits identity fields. Add
`progressive_ep`, `promoted_hex`, and `chess960` from `cursor` there. Other
ordinary boundary callers already spread a boundary carrying these values.
The custom setup path intentionally sets `promoted_hex: null`; keep that request
hosted until the UI has an explicit trusted promotion-provenance input. Do not
guess zero for arbitrary FENs.

The existing `prefixAbort` plus `prefixSequence` checks remain the UI stale
result gate. Tree replay calls without a signal are sequential and still receive
exact boundary validation; adding one navigation-scoped AbortController would
make cancellation uniform but is not required for correctness of a completed
response.

## Fallback matrix

| Event | Local result usable | Hosted fallback |
| --- | --- | --- |
| Certified exact replay | Yes | No |
| Local unsupported or over certified limit | No | Yes, original request, same authority |
| Missing `promoted_hex` | No | Yes, original request, same authority |
| `chess960: true` | No | Yes, same authority; surface current 422 |
| Artifact/certificate/boundary mismatch | No | Yes, same authority |
| Native `ok:false`, null pointer, or invalid JSON | No | Yes, same authority |
| Active synchronous local search | No | Yes, same authority |
| Hosted identity absent or different | No | **No** |
| Abort/cancel or superseded UI sequence | No | **No** |
| Error explicitly marked `fallbackRequired: false` | No | **No** |

## Dependency closure

The minimum no-Python WASM source closure is:

- `_native_eval.cpp`
- `native_eval.hpp`
- canonical `native_subtree.cpp`
- canonical `native_subtree.hpp`
- `native_subtree_wasm.cpp`
- `native_subtree_wasm.hpp`

`native_subtree_wasm.cpp` uses `SubtreeState`, `SubtreeSearchConfig`,
`SubtreeSearchSession`, `SubtreeSearchStats`, `SubtreeSearchStatus`, public board
and move types, and the public legality/EP helpers from that core. The canonical
core files do not yet exist in a committed clean checkout. The build script now
fails early if any dependency is absent, and
`benchmarks/check_wasm_dependency_closure.py` distinguishes dirty-worktree
availability from clean-checkout closure.

## Verification commands

These checks do not benchmark engine search:

```powershell
node .\benchmarks\verify_browser_prefix_contract.mjs
python .\benchmarks\check_wasm_dependency_closure.py
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\benchmarks\build_native_subtree_wasm.ps1 `
  -EmPlusPlus .\.emsdk\upstream\emscripten\em++.exe
node .\benchmarks\verify_native_prefix_contract.mjs
python .\benchmarks\verify_native_prefix_wasm.py
```

The dependency check is expected to fail until canonical
`native_subtree.cpp/.hpp` are tracked. A successful dirty-worktree compile does
not close that release gate.

Before shell integration is accepted, add focused tests for:

- builder rejection of absent prefix evidence, artifact hash drift, and any
  certified limit broader than the native hard contract;
- exact promoted-mask and `chess960: false` round-trip, including nonzero
  promoted provenance and multi-EP boundaries;
- full next-state, completion/outcome, and final-frame consistency rejection;
- Worker termination on prefix abort and rejection of old-generation replies;
- no hosted fallback after abort or a non-fallback error;
- unchanged request bytes on fallback and rejection of an absent/mismatched
  hosted source/engine/ruleset identity;
- the PV-frame request carrying EP, promoted, and Chess960 identity; and
- custom FEN with unknown promoted provenance staying on the hosted path.
