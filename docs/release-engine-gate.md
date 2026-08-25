# Release engine benchmark and strength gate

`benchmarks/release_engine_gate.py` measures the real Scottish Progressive
engine. It never substitutes scripted moves, Python fallback, an unbound WASM
file, or a cosmetic strength label.

The gate keeps three claims separate:

1. **Product modes:** real browser WASM must complete requested D5 from the
   initial position and as Black after White `e4`, in both Faster (5 seconds)
   and Strong (30 seconds).
2. **Sub-10 progress:** a quiet, controlled native run needs at least three
   samples on each boundary, median D5 wall time at most 10 seconds, no timeout
   or work-limit stop, and stable chosen-series/PV identity.
3. **Playing strength:** a candidate must pass every published tactical mate
   and an equal-budget, color-swapped fixed-suite match against the certified
   baseline profile without incomplete games or technical failures.

Passing any one of these does not imply either of the others. In particular,
finishing Strong inside 30 seconds is not a sub-10 result, and sub-10 D5 is not
evidence of Stockfish-level strength.

## Build the native engine

From the repository root on Windows:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

The benchmark fails closed if the loaded extension is absent, is Python
fallback, or its compiled source identity differs from the checked-out C++
sources. Every sample records the exact Git commit and dirty state, engine
fingerprint, native source identity, module filename, and module SHA-256. The
extension build recipe requests optimization, but the binary does not attest
its compiler flags, so the report leaves debug-build status unknown instead of
claiming it was independently proven.

## Native D5 measurements

A functional run while other work is using the machine must be labeled as
contended. Its timings are retained but explicitly non-reportable:

```powershell
.\.venv\Scripts\python.exe benchmarks\release_engine_gate.py native `
  --samples 1 `
  --measurement-quality contended-functional-only `
  --output build\release-gate\native-functional.json
```

For real performance evidence, stop other builds, engines, browsers, and heavy
background work, then take at least three fresh-process samples:

```powershell
.\.venv\Scripts\python.exe benchmarks\release_engine_gate.py native `
  --samples 3 `
  --measurement-quality quiet-controlled `
  --output build\release-gate\native-quiet.json

.\.venv\Scripts\python.exe benchmarks\release_engine_gate.py stockfish-progress `
  --suite build\release-gate\native-quiet.json `
  --target-seconds 10 `
  --output build\release-gate\sub10-verdict.json
```

Sampling is paired, interleaved, order-alternated, and performed in a fresh
process. Each raw sample records:

- backend and public mode;
- requested/completed series depth;
- outer wall time and engine-reported elapsed time;
- nodes, deterministic work, generated unique series, and peak frontier states;
- NPS;
- selected complete series, PV, score, classification, proof, and width state;
- timeout/work-limit reason;
- exact source, compiled module, Git, Python, OS, CPU, and thread identity.

The native result does not expose an exact retained-state cardinality, so that
field remains `null`; generated unique series and peak frontier states are
reported under their exact names instead.

## Browser WASM measurements

The browser evidence comes from the existing real Opera Worker/root-session
probe. First serve the checkout and launch Opera with remote debugging enabled.
Then generate the exact four URLs using the paths from the candidate WASM build
receipt:

```powershell
.\.venv\Scripts\python.exe benchmarks\release_engine_gate.py browser-plan `
  --origin http://127.0.0.1:8879 `
  --module-url /build/browser/spc-engine.js `
  --wasm-url /build/browser/spc-root-session.wasm `
  --build-receipt-url /build/browser/build-receipt.json `
  --workers 8 `
  --output build\release-gate\browser-plan.json
```

Run every `capture_command` in the plan during a quiet window. Feed the four
raw CDP receipts to the release decision:

```powershell
.\.venv\Scripts\python.exe benchmarks\release_engine_gate.py browser-release `
  --initial-faster build\release-gate\browser-initial-faster.json `
  --initial-strong build\release-gate\browser-initial-strong.json `
  --black-after-e4-faster build\release-gate\browser-black-after-e4-faster.json `
  --black-after-e4-strong build\release-gate\browser-black-after-e4-strong.json `
  --output build\release-gate\browser-release.json
```

This command intentionally accepts only the capture script's raw
`spc-opera-root-session-cdp-receipt-v1` envelope containing the
`spc-opera-root-d5-benchmark-v1` Worker receipt. It rejects the derived v2
promotion receipt produced by `build_opera_release_receipt.py`; that artifact
has already crossed a different validation boundary and is not raw timing
evidence.

Each receipt is rejected unless Opera reports an ordinary real Worker run and
the fetched bytes match the checked-in certified baseline's WASM, module JS,
kernel, source fingerprint, profile, and certificate. The raw build receipt's
exact source revision is also recorded and must match across all four cases.
The selected series is replayed with the authoritative rules engine.

The release decision fails if any required receipt is missing, duplicated,
uses a different artifact, misses D5, times out, or exceeds its mode's wall
budget. WASM nodes/NPS and retained-state count remain `null` because the
current root-session receipt does not expose them; deterministic native work
and the retained-manifest digest remain available.

## Tactical and strength gates

Run the published tactical anchors against the actual candidate profile:

```powershell
.\.venv\Scripts\python.exe benchmarks\release_engine_gate.py tactical `
  --candidate profiles\candidate.json `
  --output build\release-gate\candidate-tactical.json
```

The published moves validate each fixture only. They are never supplied to
search as the answer; the real engine must independently select a mating
series on all five anchors.

Then run the existing deterministic, color-swapped strength harness:

```powershell
.\.venv\Scripts\python.exe benchmarks\release_engine_gate.py strength `
  --candidate profiles\candidate.json `
  --pairs 10 `
  --seed 20260820 `
  --depth 2 `
  --width 32 `
  --max-search-work 250000 `
  --max-game-work 5000000 `
  --minimum-score-rate 0.5 `
  --output build\release-gate\candidate-strength.json
```

An optional `--candidate-value-model` attaches the validated deep-teacher
overlay to the candidate seat only. Both colors receive the same deterministic
depth, branch, search-work, and game-work budgets. A technical failure or
incomplete game is not scored as a draw or loss; it fails this gate.

The reference profile ID is bound to the checked-in browser root-session
certificate. Both match seats still execute the current candidate search core,
so this is an evaluator/profile regression gate rather than an old-WASM binary
versus candidate-binary match. The JSON says so explicitly.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_release_engine_gate.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_tactical_anchors.py tests\test_strength.py -q
.\.venv\Scripts\python.exe -m py_compile benchmarks\release_engine_gate.py
git diff --check
```

Do not publish timings from a contended functional run. Keep every raw receipt;
the aggregate verdict intentionally embeds the raw samples rather than only a
median.
