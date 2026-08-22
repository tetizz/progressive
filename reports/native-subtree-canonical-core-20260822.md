# Canonical native subtree core audit (2026-08-22)

## Decision

This change is a parity-closed native descendant kernel and a non-publishable
root-coordinator contract. The Python product remains authoritative for
iterative root ordering, exact Progressive adjudication, the one-series reply
mate screen, unsafe-root retry/widening, canonical replay, alternatives, and
the final publish decision. No asynchronous arrival-order selector is wired
into Python release code.

The C++ root surface intentionally has no `publishable` or
`safety_certified` result. Quiet-draw adjudication returns
`AdjudicationPending`; promotion-mate-eligible roots and malformed boundaries
return `Unsupported`; deadline or work exhaustion returns no usable bound.

## Boundary and accounting

- `enumerate_retained_root` returns the deterministic retained order, stable
  candidate/order identities, exact complete root results, mover-aware terminal
  scores, requested/retained width, and width completeness.
- `import_retained_root` avoids broad regeneration. It validates the full
  boundary/config identity and authoritatively replays each supplied series
  through a width-one required-prefix call. Import replay work is charged.
- `search_retained_root_candidate` accepts an explicit alpha/beta window and
  returns only `Exact`, `Upper`, or `Lower` on completion. All interrupted or
  unsupported calls return `Unknown`. Transactional scouts roll back TT writes.
- Every root call reports cumulative and delta `SearchStats`, external work,
  native work before/after/delta, total accounted work, and its optional
  `call_work_credit`. Native delta never exceeds the credit; zero is valid and
  exact completion at the credit is allowed.
- External work and deadlines are monotonic per persistent session. A browser
  coordinator can therefore count root generation once, import its exact
  manifest into workers, and distribute a single global budget as call credits.
- Generation cache storage remains weighted/LRU bounded. Root-contract TT and
  evaluation maps fail closed before their configured ceilings and report
  current, peak, and configured capacities. Both ceilings are bound into the
  enumeration identity, so workers with different memory envelopes cannot
  import the manifest.

The full identity preserves piece/color bitboards, promoted provenance,
castling rights, side to move, halfmove/fullmove clocks, Progressive series and
quiet counters, all canonical Progressive en-passant targets, result outcome,
check termination, path multiplicity, weights, width, depth, work/cache limits,
thread shape, and preferred ordering. Native validation rejects occupancy,
king, pawn, promoted, orthodox-castling, turn/parity, clock, en-passant, and
just-moved-in-check drift.

## Evidence

- MSVC CPython extension clean compile: passed; native source identity
  `f6ea7c7f849787fcb7e019c7dafcdbad6d5cd27ca78307b7be5d9b1f991a4066`.
- Emscripten 6.0.8 independent object builds for `_native_eval.cpp` and
  `native_subtree.cpp` with `-DSPC_WASM_CORE=1`: passed without pthreads.
- Root contract: 14 passed, including S1/S4/S7 full product parity, imported
  manifest parity/tampering, White/Black terminal scoring, full boundary state,
  hard TT/eval ceilings, deadline/work/adjudication, bounds/rollback, and
  per-call credit exact-cap/one-less/retry behavior.
- Native boundary search: 53 passed.
- Broader native evaluation/generation/root-mate regressions: 133 passed and one
  explicit opt-in early-S4 strength gate skipped.
- Differential corpus: 512/512 exact result/PV/alternatives/proof/work and
  `SearchStats` signatures matched across Series 1-8 at depth 4, width 4.
  Python took 17.4628 s; native took 14.0732 s (1.2409x).
- Anchors at depth 4, width 4 matched exactly: S1 `d2d4`, score -1145,
  work 17,834; S4 `c7c6/d8b6/f6e4/b6f2`, mate score -999999, work 634;
  S7 `a1c1/c1d1/d2c3/g1f3/f3g5/g5e6/d1d8`, mate score +999999,
  work 86,194.
- The isolated wheel path with `SPC_OMIT_STALE_OPENING_REPORTS=1` builds and
  imports the source-matched native subtree from outside the repository. A
  normal wheel remains correctly blocked by pre-existing stale opening reports
  (`db6f5c99234685b5`); this change does not falsify or regenerate that evidence.

## Deliberate gaps

This is not the prefix/mate WASM facade and does not replace its certified ABI.
It does not implement browser Worker scheduling, async reduction, root
reply-mate screening, authoritative prefix publication, or a public move
result. Any `Unknown`, crash, identity mismatch, memory ceiling, missing
candidate, deadline, or global-credit exhaustion must terminate reduction and
fall back to the existing Python/server product path.
