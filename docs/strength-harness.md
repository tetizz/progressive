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

## Deep-teacher evaluator candidate

The optional deep-teacher path is match-only. To compare the evaluator against
the unchanged deployed baseline while holding every ordinary profile weight
constant, use the baseline profile for both roles and attach the model only to
the candidate:

```powershell
spc strength-match baseline baseline `
  --candidate-value-model build\deep-teacher\candidate-model.json `
  --pairs 10 --seed 20260820 --workers 2 `
  --output reports\deep-teacher-vs-baseline.json
```

The strict loader and each process-pool worker independently verify the model,
corpus, native-source, score-policy, work-policy, and base-profile identities.
The overlay follows the candidate through both color swaps; the reference seat
never receives it. Native evaluator reach and legal-variant work is charged to
the same deterministic search budget, and exhaustion is an incomplete `*`.
Omitting `--candidate-value-model` preserves the original job and report
contract.

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

## Fair Bucephalus rematch

The 100-game protocol uses 50 content-addressed neutral boundaries from Series
3 through Series 6. Each boundary is played twice with the local engine's
orthodox color swapped. Both engines receive the same end-to-end call-wall
ceiling. Each engine plays its deepest completed legal iteration when available;
the local engine may instead return a clearly labeled legal move-only liveness
fallback when its conservative safety search remains incomplete. This is equal
move latency, not equal search time: Bucephalus's clock necessarily includes a
fresh process start and canonical from-start history replay because its 2016
interface has no persistent position command. If that disposable process exits
abnormally after flushing a complete legal iteration, the harness replay-checks
and plays that iteration while recording the exit code and recovery. An exit or
deadline with no complete legal root series remains a technical failure.

The upstream binary buffers stdout and cannot expose completed iterations when
the watchdog terminates it. Build a separately pinned benchmark binary from
upstream commit `0e11fcdc84e65122fd8b91cada71dad6323db417` with only
[`bucephalus-0e11fcdc-stdout-flush.patch`](../benchmarks/patches/bucephalus-0e11fcdc-stdout-flush.patch)
applied. That patch flushes each already-completed PLY line; it does not alter
move generation, search, or evaluation. The checked-in machine-readable build
receipt pins the rebuilt binary, compiler, command, source commit, compatibility
header, and patch. The harness rejects any mismatch.

First calibrate two pairs in a new journal directory. If it completes without
technical failures, start the 50-pair run with the same controls in a different
journal directory. Pair count is part of the frozen protocol, so calibration
and the full run must never share a journal:

```powershell
$env:PYTHONPATH = 'src'
python -B -m benchmarks.bucephalus_fair_rematch `
  .\build\bucephalus-flushed-0e11fcdc\bucephalus-flushed.exe `
  --local-profile baseline `
  --sha256 9d7b0b2c75d9cc01577e116a4afd0f17075339b242ff47e859bcf1adb7f7a7e0 `
  --upstream-commit 0e11fcdc84e65122fd8b91cada71dad6323db417 `
  --external-build-receipt .\benchmarks\protocols\bucephalus-flushed-0e11fcdc-build-receipt.json `
  --pairs 2 --seed 20260827 `
  --depth 8 --branch-cap 32 `
  --max-generation-positions 4000000000 `
  --max-game-work-positions 100000000000 `
  --common-move-seconds 30 `
  --emergency-max-series 18 `
  --workers 1 --memory-per-worker-mb 768 --reserve-memory-mb 1024 `
  --journal-directory .\reports\bucephalus-calibration `
  --output .\reports\bucephalus-calibration.json
```

For the full match, change `--pairs 2` to `--pairs 50` and use new paths such
as `reports\bucephalus-rematch-100` and
`reports\bucephalus-rematch-100.json`. Do not copy calibration game records
into the full journal.

The configured generation and per-game work limits are nonbinding outer safety
reserves; reaching either invalidates that game. The engine's separate 3M
root-child safety sub-budget is part of its playing policy: if it returns a legal
move after that selective proof remains unknown, the move is played and labeled
as an internal selective limit or move-only liveness fallback. A call with no
legal output, crash, protocol/replay failure, illegal series, fixed-array guard,
outer work reserve, wall overrun, or emergency watchdog is recorded as `*`.
Per-game records are written atomically and an exact matching run can continue
with `--resume`; failures are never selectively omitted or rerun. A
local-engine-superiority-over-Bucephalus statement is gated on the
content-addressed equal-wall protocol, all 100 games and all 50 pairs
completing, a positive local paired W/L balance, and a two-sided exact paired
sign-test p-value below 0.05. It also requires the exact approved Bucephalus
binary/build receipt and no benchmark, source, or native-backend identity drift
from game 1 through report finalization. Even a passing result applies only to
the native CPython research engine; it is not direct proof of the browser/WASM
release, Elo calibration, or Stockfish-level/world-best strength.
