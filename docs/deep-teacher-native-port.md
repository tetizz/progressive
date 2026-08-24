# Deep teacher native/WASM port gate

This lane ports the frozen `spc-teacher-value-features-v3` contract into the
shared C++20 core and exposes it through an explicit strength-match-only
candidate path. It does not inspect a holdout corpus or change the deployed
baseline.

## Implemented contract

- All 47 White-centric integer features use the exact frozen order from
  `teacher_value_features.py`.
- The supported model groups are the frozen prefix sizes 7, 14, 19, 38, 44,
  and 47. Prefix extraction stops immediately at the selected group, so the
  primary non-route model never pays for unused direct or two-move routes.
- `deep_teacher_score_v1()` returns the exact signed 64-bit fixed-point dot
  product at scale 1,000,000,000. It never divides or converts to floating
  point, so it cannot create ranking ties absent from the Python scorer.
- Invalid group sizes, scale drift, and signed 64-bit overflow fail closed.
- The source lives in `_native_eval.cpp`/`native_eval.hpp`, which are compiled
  into both the CPython extension and the WebAssembly engine core.
- The extraction receipt exposes direct and two-move variant counts internally
  so an activated search can charge all added evaluator work.
- The shared C++ root comparator and the browser/WASM root coordinator now use
  the same proof-aware policy: an exact opponent-win proof loses to every
  unknown, partial, draw, or mover-win interval. If every option is an exact
  opponent win, ordinary mover score and canonical notation order decide.
- Browser null-window coverage is proof-aware too. A non-adverse scout cannot
  be dismissed against a proven-loss incumbent; it receives the exact
  threat-research pass needed to become selectable.
- The CPython extraction surface returns an exact receipt for reach positions,
  direct legal variants, and two-move legal variants. Search charges every
  receipt count to the existing deterministic work limit.
- A strict model loader accepts only the frozen final JSON schema. It verifies
  the model ID, feature prefix and order, exact integer coefficients, scale,
  adverse-pair weight, semantic corpus identity, raw artifact provenance, and
  the source-matched native evaluator before a worker can search.

## Promotion sequence

1. Freeze the train-selected model JSON and verify its schema, feature names,
   coefficient count, fixed-point scale, model ID, and SHA-256 before compiling
   it or loading it into a search request.
2. Run exact Python/native feature and fixed-point score parity on development
   positions, promoted provenance, multiple Progressive en-passant targets,
   direct mates, pins, and color-swapped states.
3. Keep the separately reviewed Python root-selector change integrated with this
   native/browser comparator. Training metrics, Python search, C++, and WASM
   must all be present in the candidate integration commit before activation.
   Raw unfiltered adverse choices remain diagnostic evidence and must not be
   mistaken for production selector behavior.
4. Rebuild the WASM artifact from the same sources and prove CPython/WASM
   parity before any browser manifest points at it.
5. Run a paired evaluator-only strength match through the opt-in transport
   below. A work interruption is incomplete and never counts as a played
   result.
6. Only then run the unopened one-shot holdout gate and independent match gate.
   A failure leaves the deployed seven-term evaluator unchanged.

## Opt-in strength-match transport

The candidate is not added to `baseline_profile()` or silently embedded in
ordinary `SearchLimits`. Supply one frozen model only to the candidate role:

```powershell
spc strength-match baseline baseline `
  --candidate-value-model build\deep-teacher\candidate-model.json `
  --pairs 10 --workers 2 `
  --output build\deep-teacher\candidate-vs-baseline.json
```

Using `baseline baseline` is deliberate: both seats retain the exact deployed
base profile, and only the candidate seat receives the evaluator overlay. The
candidate role is tracked explicitly through both color swaps rather than
inferred from the shared profile ID. An identical-profile match without the
model option remains invalid.

The validated immutable payload is serialized on `GameJob`, reconstructed in
each process-pool worker, and checked again against the seat's base profile,
model hash, semantic and raw corpus provenance, native source identity, score
policy, and work policy. Those identities are included in run/job IDs, modeled
game records, traces, and the report. A mismatch fails closed as a technical
incomplete.

The native fixed-point dot product is converted once with symmetric
half-away-from-zero division by 1,000,000,000, then the existing search clamp
keeps quiet values below the mate/proof band. Terminal checkmate and draw replay
remain authoritative. Every native reach/direct/two-move receipt is charged;
an incomplete or over-budget receipt raises the ordinary search work limit and
cannot become match evidence.

Without `--candidate-value-model`, job identities and the strength report keep
their pre-overlay shape and behavior.

## Deliberate non-activation

No placeholder coefficient set is embedded and no browser or production
manifest selects this evaluator. Activation remains gated on a reviewed frozen
model, rebuilt WASM parity artifacts, unopened holdout evidence, and the paired
strength-match result. A failed gate leaves the deployed evaluator unchanged.
