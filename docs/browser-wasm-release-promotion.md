# Browser WASM release promotion

`scripts/promote_browser_wasm_release.py` is the only promotion step for the
combined root-session, prefix, and mate WebAssembly artifact. It copies the
already verified bytes by digest. It does not rebuild them and it does not turn
legacy lab output into release evidence.

No current receipt is implicitly trusted. Promotion stops unless all seven
receipts identify the same source revision, source fingerprint, kernel source
set, module, WASM file, and two-file artifact set. The helper also hashes the
current checkout, tracked dependency closure, compiler, artifacts, input
receipts, generated certificates, and final browser bundle.
The compiler executable, version, digest, and exact canonical builder command
are also bound; extra or conflicting compile/link flags fail promotion.

## Required evidence

The input receipt schemas are:

- `spc-root-session-build-receipt-v1`
- `spc-root-session-wasm-smoke-v1`
- `spc-root-d5-oracle-v1`
- `spc-prefix-parity-receipt-v2`
- `spc-browser-prefix-contract-receipt-v1`
- `spc-mate-wasm-receipt-v2`
- `spc-opera-root-session-cdp-receipt-v1`, containing an
  `spc-opera-root-d5-benchmark-v2` Worker receipt

The old root differential, prefix v1, mate v1, and Opera benchmark v1 formats
are deliberately non-promotable. They either lack exact artifact identity or
do not prove the final semantic and scheduling gates.

Every parity receipt carries the six-field artifact subject:
`source_revision`, `source_fingerprint`, `kernel_sha256`, `wasm_sha256`,
`module_js_sha256`, and `artifact_set_sha256`. Root and Opera evidence also
bind `exception_strategy`, `wasm_simd`, and `allocator`.

The root D5 oracle binds the exact initial Progressive position, W32 D5 session
configuration, canonical boundary tactical policy, memory envelope, retained
manifest digest, selected candidate identity/score/full PV/proof bounds, and
all 20 rival bounds. Its `oracle_signature_sha256` is recomputed over canonical
semantic JSON. The deadline limit is part of that signature, while elapsed and
remaining time, work scheduling, and arrival order are excluded. The contract must expose
`canonical_root_tactical_policy: true`,
`root_tactical_policy: canonical-boundary-policy-v1`, and only the legacy wire
value `root_tactical_protection: false`.

Prefix and mate v2 receipts retain each case's input, WASM output, and oracle
output SHA-256. Mate coverage must include White and Black `found`,
`exhausted`, work-limit `unknown`, and deadline `unknown`, with signed mate
distance, proof-bound, work, replay, and deadline parity.

The Opera receipt must prove all of the following from Opera's CDP identity and
eight ordinary `DedicatedWorkerGlobalScope` module Workers:

- exact artifact identity in every Worker;
- the hard per-Worker and aggregate memory envelope;
- cold D5 and warm persistent D1 through D5 both equal the signed root oracle;
- at least two distinct real seed-wave/order shapes reach that same result;
- exact selected-owner certification, complete rival-bound coverage, compiled
  prefix replay, compiled reply-mate safety, and zero Unknown/limit results;
- exact W32 D1 through D5 completion in less than 60 seconds, including pool
  startup.

Gate booleans alone are insufficient. The helper recomputes digests, counts,
membership, timings, work ledgers, memory totals, semantic signatures, and
cross-receipt equality.

## Receipt case records

Prefix v2 `cases` entries contain `name`, `input_sha256`,
`wasm_output_sha256`, `oracle_output_sha256`, and `exact_match`. The top-level
`case_set_sha256` is the canonical JSON SHA-256 of the full ordered array. Case
names and input hashes must both be unique, so duplicated fixtures cannot meet
the differential-case minimum.

Mate v2 uses the same fields plus `side_to_move` (`white` or `black`) and
`proof_status` (`found`, `exhausted`, or `unknown`). Its summary `gates` must
explicitly pass Python parity, authoritative replay, both mover colors,
exhaustion, both fail-closed Unknown paths, signed mate distance, proof bounds,
work receipts, deadline receipts, compiled prefix replay, and case input/output
hash accounting. Prefix v2 similarly requires explicit exact-Python, compiled
replay, multi-EP SAN, illegal-prefix fail-closed, and case-hash gates.

The Opera Worker receipt binds the root oracle under `oracle` and records real
schedule repetitions under `schedule_trials`. Every trial contains eight
Workers, the initial full wave, an order-shape digest, the oracle result
signature, completion status, zero Unknown/limit results, one selected-owner
certification, and elapsed milliseconds. At least two distinct order digests
are required and the signed primary trial must use wave 8.

## Release commands

Run from a clean checkout at the exact revision named by the build receipt.
The artifact paths are read from that receipt; they are not supplied again.

```powershell
$evidence = 'C:\path\to\final-evidence'
$common = @(
  '--build-receipt', "$evidence\root-session-build-receipt.json",
  '--root-smoke-receipt', "$evidence\root-session-smoke-receipt.json",
  '--root-parity-receipt', "$evidence\root-d5-oracle.json",
  '--prefix-parity-receipt', "$evidence\prefix-parity-v2.json",
  '--browser-prefix-receipt', "$evidence\browser-prefix-receipt.json",
  '--mate-parity-receipt', "$evidence\mate-parity-v2.json",
  '--opera-receipt', "$evidence\opera-d1-d5-receipt.json",
  '--maximum-seconds', '60',
  '--default-seconds', '60'
)

python .\scripts\promote_browser_wasm_release.py @common --check-only

python .\scripts\promote_browser_wasm_release.py @common `
  --authorized-by tetizz `
  --output .\build\browser-wasm-release
```

The second command creates an immutable directory containing:

- `browser-engine/`, the existing-builder-validated bundle;
- `certificates/`, the prefix, root-session, and mate certificates;
- `evidence/`, byte-for-byte copies of the seven receipts;
- `release-receipt.json`, the promotion authorization, all evidence hashes,
  bundle digest, measured Opera result, memory proof, and release gates.

The output path must not already exist. A failed gate leaves no promoted output.
The Pages build must consume `browser-engine/` from this directory directly;
recompiling the kernel after promotion invalidates the release.

## Optional deep-teacher browser activation

The combined ordinary-Worker artifact can carry a deep-teacher value model, but
the browser never treats the model as an ordinary mutable option. A modeled
bundle must contain all three of these inputs in addition to the ordinary
baseline root certificate:

- the exact frozen `spc-deep-teacher-linear-value-v1` JSON asset;
- a second root-session certificate whose `geometry.session_config` contains
  the matching `deep_teacher_value_model` wire config; and
- that modeled certificate's `value_model_asset` descriptor, binding the asset
  SHA-256, model/variant IDs, base profile, and native source identity.

The modeled certificate must explicitly pass asset binding, Python/native/WASM
parity, real browser-Worker smoke, and the independent playing-strength gate.
It must otherwise be identical to the baseline certificate: same source,
kernel, wrapper, ABI contract, memory envelope, engine identity, worker
geometry, time/work policy, and seven base weights. The bundle builder enforces
those invariants and copies the model by exact bytes:

```powershell
python .\scripts\build_browser_wasm_bundle.py `
  --single-wasm .\build\verified\spc-root-session.wasm `
  --single-module-js .\build\verified\spc-engine.js `
  --single-prefix-certificate .\build\certificates\prefix.json `
  --single-root-session-certificate .\build\certificates\root-baseline.json `
  --single-value-model-root-session-certificate .\build\certificates\root-modeled.json `
  --single-value-model .\build\models\promoted-value-model.json `
  --single-mate-certificate .\build\certificates\mate.json `
  --output .\build\browser-engine-modeled
```

At runtime, every Worker verifies the model bytes before selecting the modeled
root certificate. A missing, malformed, cross-origin, or digest-mismatched
model selects the separately certified baseline root configuration and exposes
the fallback reason. It never reports the model ID or variant as active. A
bundle without `value_model_activation` keeps the previous baseline request and
identity shape.

The current release-promotion receipt schema remains baseline-only. It must not
be widened until a real frozen model has the required parity, browser-Worker,
and independent strength evidence; builder fixtures are not promotion evidence.
