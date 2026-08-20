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

## Fast training funnel and ten-engine league

The default population is the current champion plus nine deterministic bounded
mutations. Later generations retain the champion, can include prior champions,
cross the champion with the strongest challenger, and fill the remaining slots
with new mutations. Engines never vote on moves or splice principal variations;
only evaluation parameters cross over.

The old all-play-all preliminary stage has been replaced by a cached
preselection funnel. Normal runs require all 30 unique boundaries in opening
suite v4 plus separate tactical positions; a smaller corpus is accepted only
when explicitly marked as a smoke/wiring run. Exact Scottish boundary
positions are searched once with the champion under versioned, deterministic
shallow/wide limits. The cache
stores the seven unscaled evaluation terms, bounded opponent-mate evidence,
canonical position hashes, trace-level train/holdout assignments, and optional
prior-engine suggestion provenance. Thousands of mutations can then be
dot-scored without rerunning legality or search. Only top-K profiles receive
the cached short-trace and tactical proxy screen, and only the best challenger
marked `eligible_for_full_game_testing` continues to full games. That field is
permission to test, not a safety pass or promotion.

Every funnel result says that it is a cached position/short-rollout proxy, not
WDL, Elo, or independent strength evidence. A prior engine's suggested move is
stored separately from the deeper internal re-search label, along with explicit
agreement or disagreement; teacher moves are never treated as truth and no
external engine code is copied. Opening boundaries are grouped by deterministic
line family before splitting: an ancestor and descendant with the same first
complete series always stay together, while the empty root and arbitrary
anchors have explicit families. Derived rows inherit that family, so a line
cannot leak across train and holdout. Cache and report IDs cover the
deterministic evidence, and atomic JSON writes let an interrupted generation
resume without changing its ranking.

Opponent-reply safety has three explicit states: `proven-unsafe`,
`complete-no-mate`, and `unknown-selective`. Only `complete-no-mate` sets the
safety-pass or tactical-non-regression booleans. A selectively searched row can
still be shortlisted for controlled full-game testing when it found no known
mate and passed the tactical proxy, but it remains `unknown-selective` and
makes no safety claim. Later, combinatorially larger depth-one rows remain
position proxies rather than opponent-reply evidence. The actual top challenger
must still pass the existing searched tactical gate and the full-game promotion
match.

Run the standalone, resumable screen with:

```powershell
spc train-fast .\fast-training-evidence
```

That command uses the full 32-position root corpus by default and writes an
atomic cache and report. `--smoke` is an explicitly labeled four-position
wiring preset; it is not a smaller strength test. The normal `spc league run`
path calls the same funnel before its unchanged full-game promotion stage.

## Replayed self-play value fitting

Completed league games can be converted into a separate value-training corpus:

```powershell
spc train-selfplay build\selfplay-tuning `
  data\evolution-seed-a.sqlite3 data\evolution-seed-b.sqlite3
```

This path does not trust stored scores or intermediate FENs. It reconstructs
each persisted opening boundary, replays every complete series through the
public Scottish rules API, and requires the reconstructed final PFEN and
terminal outcome to match. Only real checkmates and rules-proven ten-series
draws become value labels. Manual adjudications, work limits, engine failures,
and worker failures are excluded rather than assigned an outcome.

Each completed game contributes total weight one regardless of its length.
Related opening boundaries are grouped by their first complete series, and a
connected-component pass joins otherwise separate groups if their games
transpose to an identical progressive state. The entire component goes to
train or holdout, preventing adjacent states or transpositions from leaking
across the split.

The v1 fitter performs deterministic Texel-style coordinate descent over the
seven explainable evaluation scales. It chooses parameters using train data
only and reports holdout log loss separately. Its output is deliberately named
`candidate-profile.json`, never a champion envelope. Lower train or holdout
loss is a value-fit proxy, not match strength: the candidate must still pass
the searched tactical gate and an isolated color-swapped strength match before
the league may consider promotion.

### v0.7 outcome

Two completed v0.6 leagues supplied 88 conclusive games (852 replay-verified
boundary samples); eight manual-adjudication games were excluded. The fitted
candidate improved weighted value loss from `0.886026` to `0.732856` on train
and from `0.870439` to `0.670045` on the untouched line-family holdout. The
parameter trace is checked in as
[`selfplay-tuning-v0.7.0.json`](../benchmarks/results/selfplay-tuning-v0.7.0.json).

That proxy improvement did not promote the engine. On six held-out opening
families the candidate scored pair W/D/L `3/1/2`. On a separately generated,
content-addressed 20-opening suite it scored `1/8/0` across nine complete pairs,
with one pair incomplete because one side reached manual quiet adjudication.
The strict gate requires at least nine pair wins and no loss, so baseline
`spc-68942034c41b4cc4` remains champion. Exact reports are
[`selfplay-family-heldout-v0.7.0.json`](../benchmarks/results/selfplay-family-heldout-v0.7.0.json)
and [`selfplay-fresh-seeded-v0.7.0.json`](../benchmarks/results/selfplay-fresh-seeded-v0.7.0.json).
This is fixed-suite evidence only, not calibrated Elo or a Stockfish comparison.

Future neutral suites are generated through the public rules API, carry exact
from-start replay histories, and derive their version from their content. The
match harness rejects a reused suite ID with changed metadata or PFEN, and each
game record retains the verified suite version.

This shallow/wide design is informed by Janko and Guid's 2016 Progressive Chess
experiments: they tested search depths through self-play, reported their
depth-one configuration outperforming deeper/narrower configurations under the
tested time limits, tested heuristic ablations, and assembled a 900-position
mate corpus. See [A program for Progressive chess](https://chesslife.io/matej/doc/A_Program_for_Progressive_Chess.pdf),
sections 7.1, 7.2, 8.4, and 8.5. Their program used Italian rules and different
depth terminology, so those results motivate our funnel but do not validate
our Scottish engine or count as strength evidence.

The promotion stage remains a separate match against the champion. Its first 20 games are ten unique,
versioned boundaries with the profile colors swapped once at each boundary.
Replacement pairs are added when a game is inconclusive, up to the configured
cap. Seeds, boundaries, profile IDs, limits, traces, outcomes, and failures are
stored in SQLite.

Promotion requires all of the following:

1. The rules/tactical gate passes, including published long-series legality
   anchors and a searched series-four mate construction.
2. There are no exception, missing-move, or worker failures in the evidence.
3. At least 20 controlled games finish as ten unique color-swapped pairs.
4. The candidate wins at least nine of those pairs and loses no pair.
5. The candidate's normalized pair score is above 50%.

This gate is deterministic fixed-suite evidence. It is intentionally not
described as a 95% confidence interval or proof of general superiority; that
would require independently sampled openings from a declared population and a
separate external-engine test set.

There is no normal maximum series: the progressive move allowance continues
1, 2, 3, ... until checkmate or a rules-proven draw. An optional whole-game
logical-work budget or emergency series watchdog is operational only. If used
and exhausted, it is stored as `*` incomplete and excluded from wins, draws,
losses, and pair fitness. Exceptions and searches that produce no legal best
series are also `*`, never opponent wins. A per-search work limit may still
play its best fully legal root series found so far.

## Resource boundary

On Windows, the launcher detects the process CPU-affinity limit and currently
available physical memory. The worker count is capped by both. Supplying a
larger `--workers` value is clamped rather than honored beyond the detected
envelope. CPU affinity is an enforced ceiling; the RAM calculation is a worker
planning estimate rather than an operating-system RSS limit. The detected CPU
count, available memory, per-worker estimate, reserve, and final worker count
are persisted with every run and refreshed on resume.

League fairness uses deterministic depth, complete-series candidates per node,
and logical-work budgets covering series expansion, distinct frontier scoring,
uncached static evaluations, evaluation reach, and quiet adjudication. Wall
time is not used to give one profile more search than another. Each game uses
fresh search state so process order and warm caches do not become fitness.

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
