# Reproducible strength matches

`spc strength-match` compares one candidate `EngineProfile` against one
reference without opening or changing the evolution league database. Either
profile argument can be an EngineProfile JSON file, a champion envelope, or the
literal name `baseline`.

```powershell
spc strength-match profiles\candidate.json baseline `
  --pairs 10 `
  --seed 20260820 `
  --depth 2 `
  --branch-cap 32 `
  --max-work-positions-per-search 250000 `
  --max-game-work-positions 5000000 `
  --output reports\candidate-vs-baseline.json
```

The harness shuffles the versioned opening suite deterministically, selects
without replacement, and plays each selected boundary twice with the profiles'
orthodox colors swapped. Every search receives the same depth, complete-series
candidate branch cap, and logical-work limit. The whole-game budget counts
complete-series expansion, distinct frontier-score evaluations, uncached
static evaluations, evaluation-reach nodes, and quiet-adjudication work over
all series. It never creates a draw or win: exhaustion is an incomplete `*`.
There is no wall-clock limit. Series budgets continue 1, 2, 3, ... normally;
the default `--emergency-max-series` is null/unbounded. Node counts are recorded
in each trace but are not themselves capped.

By default, games run in parallel using the detected CPU and estimated-memory
worker envelope. `--workers N` can request fewer workers; a request above the
safe detected cap is clamped. The JSON report records both the detected cap and
the actual worker count. The RAM calculation is a planning estimate, not an
operating-system memory limit.

The report contains:

- candidate game W/D/L and color-swapped pair W/D/L;
- incomplete games and pairs rather than silently adjudicating them;
- profile-attributed technical failures and unattributed worker failures;
- every selected boundary, final PFEN, and per-series engine trace;
- engine source/runtime provenance and the exact deterministic limits;
- a descriptive fixed-suite Elo-like transform when the score is finite.

That last number is not a calibrated Elo rating or confidence bound. It applies
only to the exact suite and limits in the report and cannot be compared with
orthodox Stockfish Elo. A convincing result is evidence for choosing between
these two Scottish Progressive profiles, not proof of general or Stockfish-level
strength.

## Fast wiring check

This two-game command selects the published fourth-series mating boundary. It
is useful for verifying color swapping, parallel result ordering, trace capture,
and report serialization; it is far too small to measure strength.

```powershell
spc strength-match build\strength-smoke\candidate.json baseline `
  --pairs 1 --seed 7 --depth 1 --branch-cap 64 `
  --max-work-positions-per-search 250000 `
  --max-game-work-positions 250000 `
  --workers 2 --output build\strength-smoke\report.json
```
