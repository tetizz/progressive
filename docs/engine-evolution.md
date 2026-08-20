# Progressive engine and evolution contract

## What the engine is

The engine searches Scottish Progressive Chess directly. One search ply is a
complete legal series, and a fixed-prefix search keeps every already-played
micro-move before expanding the remaining moves in that series. It does not
feed the position to orthodox Stockfish or alternate sides after every move.

Series generation is incremental and bounded before every full series has been
materialized. A deterministic frontier keeps tactically urgent and diverse
partial paths, including checks, promotions, captures, and mating
constructions. Any frontier pruning is reported as selective. A wall-clock or
generation-work interruption keeps the best fully scored root candidate found
so far and never relabels partial work as an exact result.

This is a serious progressive-specific engine foundation, not a claim of
Stockfish-level playing strength. Strength has to be earned by repeatable
tactical gates, controlled games, deeper search, and later calibration against
other progressive engines.

## Profiles

Every engine profile contains seven bounded percentage weights:

- material
- king space
- series checking reach
- promotion corridors
- immediate vulnerability
- useful mobility
- boundary check pressure

The shared rules and search code never change between opponents. Recommended
profile limits are metadata only during a league: both sides receive the exact
same match limits, so a challenger cannot win by buying a larger tree. Profile
IDs are hashes of the behavior-affecting parameter vector.

## Ten-engine league

The default population is the current champion plus nine deterministic bounded
mutations. Later generations retain the champion, can include prior champions,
cross the champion with the strongest challenger, and fill the remaining slots
with new mutations. Engines never vote on moves or splice principal variations;
only evaluation parameters cross over.

The preliminary stage is a color-balanced round robin. The promotion stage is
a separate match against the champion. Its first 20 games are ten unique,
versioned boundaries with the profile colors swapped once at each boundary.
Replacement pairs are added when a game is inconclusive, up to the configured
cap. Seeds, boundaries, profile IDs, limits, traces, outcomes, and failures are
stored in SQLite.

Promotion requires all of the following:

1. The rules/tactical gate passes, including published long-series legality
   anchors and a searched series-four mate construction.
2. There are no timeout, work-limit, missing-move, or worker failures in the
   evidence.
3. At least 20 controlled games finish as ten unique color-swapped pairs.
4. The candidate wins at least nine of those pairs and loses no pair.
5. The candidate's normalized pair score is above 50%.

This gate is deterministic fixed-suite evidence. It is intentionally not
described as a 95% confidence interval or proof of general superiority; that
would require independently sampled openings from a declared population and a
separate external-engine test set.

Reaching the configured maximum series is stored as `*` inconclusive. It is
excluded from wins, draws, losses, and the confidence denominator. A technical
failure invalidates promotion evidence; it is never converted into a draw.

## Resource boundary

On Windows, the launcher detects the process CPU-affinity limit and currently
available physical memory. The worker count is capped by both. Supplying a
larger `--workers` value is clamped rather than honored beyond the detected
envelope. CPU affinity is an enforced ceiling; the RAM calculation is a worker
planning estimate rather than an operating-system RSS limit. The detected CPU
count, available memory, per-worker estimate, reserve, and final worker count
are persisted with every run and refreshed on resume.

League fairness uses deterministic depth, branch, and generation-position
budgets. Wall time is not used to give one profile more search than another.
Each game uses fresh search state so process order and warm caches do not become
fitness.

## Move quality

`Best`, `Excellent`, `Good`, `Inaccuracy`, `Mistake`, and `Blunder` use a
versioned provisional policy measured in this engine's White-centric heuristic
points. These values are not centipawns. A micro-move is compared against the
best continuation before that exact move, while retaining every earlier move
in the same series.

A badge becomes `Not rated` when evidence is shallow, timed out, work-limited,
selective under the strict policy, adjudication-pending, profile-mismatched, or
otherwise incomparable. Raw loss and game outcomes can later calibrate new
threshold versions without rewriting historical evidence.

## Commands

```powershell
spc league resources
spc league run data\evolution.sqlite3 --continue-latest --champion-output profiles\champion.json
spc league status data\evolution.sqlite3
spc web --engine-profile profiles\champion.json
```
