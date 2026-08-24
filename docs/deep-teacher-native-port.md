# Deep teacher native/WASM port gate

This lane ports the frozen `spc-teacher-value-features-v3` contract into the
shared C++20 core. It does not inspect a holdout corpus and does not activate a
new live evaluator.

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

## Promotion sequence

1. Freeze the train-selected model JSON and verify its schema, feature names,
   coefficient count, fixed-point scale, model ID, and SHA-256 before compiling
   it or loading it into a search request.
2. Run exact Python/native feature and fixed-point score parity on development
   positions, promoted provenance, multiple Progressive en-passant targets,
   direct mates, pins, and color-swapped states.
3. Add the model identity and coefficients to root-session enumeration and
   search identities. A model mismatch must invalidate imported manifests and
   transposition/evaluation caches.
4. Charge reach probes, direct variants, and (for all47 only) second-move
   variants against deterministic search work receipts. The shared extractor
   already stops at the selected prefix; the search receipt must consume the
   counts it returns.
5. Keep terminal checkmate/draw adjudication authoritative. To preserve the
   frozen model's exact ordering in alpha-beta, use fixed-point leaf scores and
   a correspondingly scaled mate domain; do not divide by the model scale.
6. Integrate the separately reviewed Python root-selector change with this
   native/browser comparator. Training metrics, Python search, C++, and WASM
   must all be present in the candidate integration commit before activation.
   Raw unfiltered adverse choices remain diagnostic evidence and must not be
   mistaken for production selector behavior.
7. Rebuild the WASM artifact from the same sources and prove CPython/WASM
   parity before any browser manifest points at it.
8. Only then run the unopened one-shot holdout gate and independent match gate.
   A failure leaves the deployed seven-term evaluator unchanged.

## Opt-in strength-match seam

The candidate must not be added to `baseline_profile()` or silently embedded in
ordinary `SearchLimits`. The existing safe seam is `EvaluationOverlay`, passed
explicitly to `analyze()` and identity-bound to one ordinary `EngineProfile`.
The bounded match-only integration should be:

1. Add a strict loader for one `spc-deep-teacher-linear-value-v1` JSON path. It
   must recompute `model_id`, require the exact feature schema/order/group and
   scale, require exact signed integers, and retain the model file SHA-256.
2. Materialize an immutable overlay with `base_profile_id` equal to the chosen
   candidate profile and `variant_id` derived from the base profile ID, model
   ID, model SHA-256, evaluator source identity, score-domain policy, and work
   policy. The reference profile receives no overlay.
3. Add an explicit strength command option such as
   `--candidate-value-model <frozen-model.json>`. Serialize the validated model
   payload and identities onto each `GameJob`; reconstruct the immutable
   overlay inside each process-pool worker and pass it as
   `evaluation_overlay=` to `analyze()`. This is necessary because the current
   strength harness transports only `EngineProfile` objects.
4. Keep the ordinary deterministic `SearchLimits` identical for both players,
   but charge the candidate extractor's reach/direct/two-move receipt against
   its existing logical-work budget. A limit interruption remains incomplete,
   never a result.
5. Convert the exact fixed-point dot product to the activated search score
   domain once, with a declared deterministic rounding rule, then clamp quiet
   leaves below the proof/mate band. Replayed terminal outcomes remain
   authoritative.
6. Record the variant/model/hash/work/score-domain identities in every game and
   in the match report. Reject resume or aggregation when any identity differs.

That CLI/GameJob worker transport is intentionally not part of this core port.
Until it is implemented and tested, the compiled evaluator is parity-capable
but cannot alter a strength-match player or the deployed baseline.

## Deliberate non-activation

The train-selected model and its sealed evaluation result do not exist in this
branch. Wiring placeholder coefficients into live search would make evaluator
identity ambiguous and could break mate-score dominance. This port therefore
supplies the exact shared implementation, proof-aware native/browser selector,
and parity surface. Activation remains gated on the reviewed Python selector,
a frozen model artifact, opt-in match transport, complete work/cache/score
accounting, rebuilt WASM artifacts, holdout evidence, and match evidence.
