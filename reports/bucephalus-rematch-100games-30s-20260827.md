# Bucephalus 100-game equal-wall rematch

Run date: 2026-08-27

This report preserves the first complete, precommitted 100-game run of the
current native Scottish Progressive baseline against the pinned Bucephalus
build. The schedule used 50 content-addressed openings with both colors, one
worker, and the same 30-second end-to-end wall ceiling for each engine call.

## Frozen identities

- Local artifact commit: `00a336ba36b5b3b302357ce4397bf4fe67dd5452`
- Local source commit: `1950ad88bb975c06a0e454cad2220acad9fb43be`
- Local source fingerprint: `dd1621ac34bd4acb`
- Local profile: `spc-68942034c41b4cc4`
- Harness artifact set: `3db3361e58f741d85a577eddf8fc5e8b21d2eed2e5d1ec480277091b7bc99b9b`
- Bucephalus upstream commit: `0e11fcdc84e65122fd8b91cada71dad6323db417`
- Bucephalus executable SHA-256: `9d7b0b2c75d9cc01577e116a4afd0f17075339b242ff47e859bcf1adb7f7a7e0`
- Opening-suite SHA-256: `53fe7d10b5e31d93e0b9b75374832c2e319a691b710c34c4e4a75b5db2cb6ff1`
- Journal protocol SHA-256: `8d9026b2e1df6943b91da4c37e38ee245cede1f5019ab4a656fa812da0bf39ff`
- Report ID: `external-report-e65c19ddd7e440b482ec`
- Full JSON SHA-256: `6aa5f81d521bc60f8bb368179a4b30b89abd79325f54de432e6617dafdbca646`

The tracked worktree was clean at both ends of the run. The identity-drift
check reported no changed fields. All 100 game IDs and all 50 pair IDs are
unique.

## Controls

- Local search: requested depth 8 series, complete-series width 32, up to
  4,000,000,000 work positions per search and 100,000,000,000 per game.
- Bucephalus search: maximum supported PLY 30, returning only the deepest
  fully emitted and replay-validated legal root series.
- Common clock: 30 seconds per engine call, measured from local analyze-call
  entry or external process start through history replay and search.
- Emergency Series 18 is a technical watchdog, not a chess rule.
- No run was resumed and no result was selectively rerun.
- Total wall time: 3,470.943 seconds (about 57 minutes 51 seconds).

The controls are equal-wall, not equal-depth or equal-work. Local depth counts
complete progressive series, while Bucephalus PLY counts individual moves.

## Result

| Measure | Local | Bucephalus | Other |
|---|---:|---:|---:|
| Completed-game wins | 59 | 35 | 0 draws |
| Complete-pair W/D/L | 10 | 1 | 33 pair draws |
| Technical failures | 0 | 6 | all external timeouts |

- Completed games: 94 of 100
- Completed color-swapped pairs: 44 of 50
- Local completed-game score: 62.765957%
- Local complete-pair score: 60.227273%
- Decisive complete pairs: 11
- Exact two-sided paired sign-test p-value: 0.01171875
- Recovered legal flushed Bucephalus iterations after process exit: 5
- Local deadline-completed iteration moves: 24
- Local internal-selective-limit moves: 14
- Local move-only liveness fallbacks: 13

The completed paired sample favors the local engine, and its paired sign test
is below 0.05. The predeclared overall superiority gate nevertheless remains
**false** because it requires all 100 games and all 50 color-swapped pairs to
complete. Six Bucephalus calls reached their wall deadlines without emitting a
legal complete root series; those games remain transparent incompletes and are
not converted into losses or selectively rerun.

## Claim boundary

This is strong independent-engine evidence for the frozen native research
engine on the fixed opening suite. It is not calibrated Elo, SPRT, equal-node,
equal-depth, browser-release equivalence, or proof of Stockfish-level strength.
The public browser artifact shares the frozen source revision, but this match
used the native research runtime and does not itself certify browser-versus-
Bucephalus equivalence.

The full JSON report contains the schedule, controls, exact identities, every
game trace, authoritative replay states, per-call timing/work receipts,
technical failures, pair summaries, and claim-gate result.
