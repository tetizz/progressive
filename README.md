# Scottish Progressive Chess Research Engine

This repository combines a rules-correct Scottish Progressive Chess engine
with a local analysis board, reproducible series-level search, a CLI, a small
SQLite theory store, and evidence-bound opening reports. The browser board is
designed around progressive series rather than ordinary alternating chess: a
player keeps moving until the series budget is used or check ends it.

## Analysis board

On Windows, double-click `launch-analysis-board.cmd`. It prepares the local
environment when needed, loads the latest gated champion when one exists,
starts a loopback-only server, and opens the board in your browser. The same
launch is available from PowerShell:

```powershell
.\.venv\Scripts\spc web
```

The board supports click-to-move and dragging, legal-target highlighting,
promotion choice, castling, progressive en passant, board flipping, series
undo/reset, arbitrary boundary positions, opening-report browsing, and real
engine analysis. A completed series hands off automatically, so the board is
immediately playable for the next side. Every micro-move appears separately
inside its progressive series. The local analysis tree can branch from any
checked prefix, survives a reload, and replays every saved path through the
server instead of trusting an intermediate client FEN. Named positions can be
saved and loaded on the device; the server rechecks their boundary and full
move prefix before accepting them.

Analysis begins automatically, cancels when the board changes, and deepens one
complete-series ply at a time up to the selected Quick or Strong ceiling. The
latest completed result remains visible while the next pass is running. The
pause control stops analysis without making the board unplayable, and the
principal variation can be stepped one micro-move at a time without altering
the played study.

Search results expose depth, timeout, deterministic work limits, width
selectivity, proof/adjudication status, principal variations, alternatives,
and evaluation components. Quick and Strong presets are real search limits,
not cosmetic labels. Move-quality badges are shown only when comparable engine
evidence is deep and complete enough; otherwise the honest label is `Not
rated`. Scores are explicitly White-centric heuristic points, not pawns or
centipawns.

The normal web service binds only to `127.0.0.1`. Analysis is time-, depth-,
body-, branch-, deterministic-work-, and concurrency-bounded; database writes
are disabled unless a fixed database path is supplied when launching the
server.

## Hosted board

[`render.yaml`](render.yaml) defines the real Python web service rather than a
static imitation of the board. Hosted mode requires an exact HTTPS origin,
disables all SQLite access, reduces request and search ceilings, and permits
only one CPU-heavy analysis at a time. Deploy from the public repository:

[Deploy to Render](https://render.com/deploy?repo=https://github.com/tetizz/progressive)

The free service can sleep between visits and take time to wake. Cloudflare can
provide a custom domain and edge protection in front of this origin. A free
Cloudflare Worker is not used as the chess engine because it cannot provide the
Python runtime and sustained CPU search this project requires; Cloudflare
Containers are a compatible paid alternative.

## Engine training league

Double-click `train-engine.cmd` to start or continue a checkpointed 10-engine
league. With no `--workers` override, training uses the smaller of the detected
logical-CPU limit and an available-memory planning estimate. It never spawns
beyond that recorded worker envelope. CPU affinity is enforced by the operating
system; the RAM figure is a conservative estimate, not a per-process hard
memory limit. On this machine that means it may use all available logical
processors while the league is running.

The ten engines share one rules/search implementation and vary a bounded,
versioned progressive-evaluation genome. Matches use the same deterministic
depth, complete-series branch cap, and per-search work limits for both engines.
The normal league has no series-number or whole-game cutoff: budgets keep
increasing 1, 2, 3, ... until checkmate or a rules-proven draw. Each default
promotion match uses ten unique opening boundaries in color-swapped pairs.
Technical failures are recorded as `*`, excluded from W/D/L and pair fitness,
and never awarded as wins. A challenger becomes the one board champion only after the tactical
gate passes, at least ten unique pairs finish, it wins at least nine pairs,
loses no pair, and its pair score is above 50%. This is deliberately labeled
fixed-suite evidence, not a general-strength confidence claim.

Training data is stored in `data\evolution.sqlite3`; the currently trusted
profile is atomically published to `profiles\champion.json`. Closing the
training window does not erase completed games: run `train-engine.cmd` again
to resume the latest unfinished run. Full design and evidence boundaries are
in [`docs/engine-evolution.md`](docs/engine-evolution.md).

Completed league databases can also be replayed into a leakage-resistant value
corpus and an explicitly unpromoted evaluation candidate:

```powershell
spc train-selfplay build\selfplay-tuning `
  data\evolution-seed-a.sqlite3 data\evolution-seed-b.sqlite3
```

The command rechecks every stored series and terminal PFEN. It excludes manual
or technical results, reports train and holdout loss separately, and never
changes the board champion. Tactical and color-swapped match gates remain
mandatory.

For an independent color-swapped test, generate neutral openings without using
either profile's evaluation:

```powershell
spc strength-match build\selfplay-tuning\candidate-profile.json baseline `
  --pairs 10 --seed 20260822 --seeded-openings 20 --workers 16 `
  --output build\fresh-strength.json
```

The generated suite is replayable from the initial position and
content-addressed inside the report. A good proxy score or a slight match edge
does not update the champion; the tactical and fixed-pair promotion gate still
decides that.

## Rules contract

The implementation follows the Scottish rules documented by Timo Honkela's
World Internet Progressive Chess Championship page:

- White receives one move, Black two, White three, and so on.
- Giving check immediately ends the current series, including an early check.
- A player who starts in check must escape with the first move.
- A player who has no legal move, or runs out of legal moves inside a series,
  is stalemated and the game is drawn.
- En passant is available only on the first move of a series. The vulnerable
  pawn must have made a two-square move in the previous series and must not
  have moved again later in that series.
- Ten quiet series set a proof-required adjudication flag. They are not
  auto-drawn because the historical/common rule exempts an impending mate.
- Ordinary move legality, promotion choices, and castling legality still
  apply after adapting them to series turns.

Rule sources and implementation decisions are recorded in
[`docs/rules.md`](docs/rules.md).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\spc rules
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\spc web
```

## CLI examples

Start the local analysis board without automatically opening a browser:

```powershell
spc web --no-browser --port 8765
```

List every unique legal Black two-move series after `1.e4`:

```powershell
spc series --fen "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1" --series 2
```

Analyze an arbitrary boundary position, where one search ply is one complete
series:

```powershell
spc analyze --fen "<standard FEN>" --series 5 --depth 2
```

Inspect the detected training envelope or start the same league manually:

```powershell
spc league resources
spc league run data\evolution.sqlite3 --continue-latest --champion-output profiles\champion.json
```

Persist the result, including the exact search limits and source fingerprint,
to the versioned SQLite evidence store:

```powershell
spc analyze --fen "<standard FEN>" --series 5 --depth 2 --database data\theory.sqlite3
```

Rank all initial moves by exhaustively considering every unique Black
two-move reply, then save the raw evidence and Markdown report:

```powershell
spc rank-openings --reply-depth 1 --output-dir reports
```

Deepen selected openings and compare named historical reply hypotheses:

```powershell
spc deepen-openings --moves e2e4,d2d4 --reply-depth 2 --max-series 16 --output-dir reports
spc compare-replies --output-dir reports
```

## Current milestone evidence

- [`reports/initial-opening-ranking.md`](reports/initial-opening-ranking.md)
  covers all 20 first moves and every unique Black two-move reply. This is an
  exhaustive two-series horizon, not an objective opening ranking.
- [`reports/selective-opening-deepening.md`](reports/selective-opening-deepening.md)
  adds White's three-move response for `1.e4` and `1.d4`, retaining at most 16
  tactically/diversely ordered partial paths and complete series per searched
  node. Any such pruning is labeled selective.
- [`reports/published-reply-comparison.md`](reports/published-reply-comparison.md)
  tests named historical and engine reply hypotheses with a 64-finalist White
  response screen.
- [`docs/native-acceleration.md`](docs/native-acceleration.md) records the
  optional C++20 evaluation, legal-move, and complete-series kernels, the
  exact Python fallback boundary, differential rule gates, and fresh-process
  speed measurements.
- [`benchmarks/results/native-boundary-search-v0.9.0-cp314-windows-depth2.json`](benchmarks/results/native-boundary-search-v0.9.0-cp314-windows-depth2.json)
  is the v0.9 N2 boundary-search benchmark target. The release evidence below
  uses only the verified speedup ratios; the artifact owns the final raw
  timings and semantic signatures.
- [`benchmarks/results/selfplay-fresh-seeded-100-v0.9.0.json`](benchmarks/results/selfplay-fresh-seeded-100-v0.9.0.json),
  [`benchmarks/results/league-standout-fresh-seeded-100-v0.9.0.json`](benchmarks/results/league-standout-fresh-seeded-100-v0.9.0.json),
  and [`benchmarks/results/league-generation1-standout-fresh-seeded-100-v0.9.0.json`](benchmarks/results/league-generation1-standout-fresh-seeded-100-v0.9.0.json)
  freeze three fresh 100-game v0.9 checks against the baseline.
- [`docs/engine-evolution.md`](docs/engine-evolution.md) records the replayed
  self-play fit, natural-selection runs, independent match results, and why no
  v0.7, v0.8, or v0.9 challenger was promoted.

The shallow run ranks `1.e3` first and `1.e4` second, but that ordering
collapses an important horizon: White has not yet received the three-move
series. In the
selective three-series extension, `1.e4` scores `+530` and `1.d4` `+452` under
the current heuristic. Those are useful hypotheses only; branch pruning and
uncalibrated evaluation weights make neither a proof nor an objective answer.

The v0.8 native milestone generates compatible complete-series frontiers in
C++20 and performs the searcher's exact final top-32 selection before Python
materializes the retained series. It still reports the full raw/unique/path and
pruning counts. On a cold-installed Windows CPython 3.14 wheel, the frozen
depth-2 benchmark measured these median end-to-end search times against the
same native micro-kernels with the bulk generator hidden:

| Position | Micro-native path | v0.8 bulk path | Speedup |
| --- | ---: | ---: | ---: |
| S1 | 0.7127814 s | 0.4193095 s | 1.700x |
| S3 | 6.8801801 s | 0.6795530 s | 10.1246x |
| published S4 mate | 0.2163944 s | 0.0170888 s | 12.663x |
| live S22 adjudication state | 2.3934753 s | 0.1119073 s | 21.388x |

Every complete result, proof field, and `SearchStats` field was identical. A
same-suite 20-record self-play replay fell from 346.5628463 seconds on v0.7 to
20.1899159 seconds with the final cold-installed v0.8 wheel, a 17.165x
throughput gain. All 20 records matched; 19 were valid checkmates and one
remained a manual-adjudication incomplete. That comparison is 16-worker pool
throughput, not the latency of one game.

The final cold-installed v0.8 wheel (source fingerprint `f369b5da69c17c5f`)
then completed 100/100 games conclusively in 75.944 seconds with no technical
failures or incompletes, averaging 1.31676 games/second across the 16-worker
pool. The candidate scored 49 wins, 1 draw, and 50 losses by game; its
color-swapped pairs scored 7 wins, 36 draws, and 7 losses for an exact 0.500
pair score and a descriptive -3 performance difference. This is fixed-suite
evidence, not individual-game latency or a general rating. In that
self-play-fit match, no engine stood out; no profile was promoted, and the
baseline remains champion.

A separate v0.8 natural-selection run
`ce7c3cc7-baa1-4a7c-864d-652e18ba4924` screened populations of 64 over two
generations, then gave each finalist a 50-game color-swapped gate with up to 10
replacement games. The 16-worker pool used depth 2, cap 32, and 250,000 work
positions per search. Generation-one finalist `spc-d14d1dae18b54c23`
completed all 25 pairs at W/D/L `6/18/1` and score `0.600`, but failed the
required-win and zero-loss rules. Generation-two finalist
`spc-c2faf211c4300f12` completed only 22/25 pairs at `3/14/5` and score
`0.455`; it also failed. Across 118 raw games there were no technical failures.
The baseline therefore remained champion.

The promising generation-one runner-up then faced a fresh 100-game holdout.
Its conclusive game W/D/L was `49/3/45`, with three manual-adjudication
incompletes; its conclusive pair W/D/L was `5/41/2`, with two incomplete pairs.
Across 48 conclusive pairs it scored `0.53125`, a descriptive +14, but the two
losses and incomplete pairs still rule out a promotion claim. The holdout
harness cannot change the champion in any case. Its reported 62.558 seconds
and 1.55057 completed games/second measure the 16-worker pool, not one game's
latency. `spc-d14d1dae18b54c23` is a promising runner-up only; the baseline
remains champion.

The v0.9 N2 milestone keeps the v0.8 native complete-series semantics while
holding retained candidates in a native capsule and decoding them lazily for
search. The frozen engine source fingerprint is `806aa0d679f6d1ef`; the
source-matched native identity is
`e7d36c5fc755cca2ae8877f4e73d8f9aa161a405a4c91361a6866d2f6463ca4f`.
The exact-native gate passed 374 tests. With native acceleration forced off,
the same gate passed 302 fallback tests and skipped the 72 native-only checks.
All five published tactical positions remained replay-proven searched mates,
and the promotion-mate regressions also preserved the same early checking
mates at S9 and S10 with two unused moves. These are parity and tactical
correctness results, not a playing-strength claim.

The final five-sample v0.9 wheel benchmark preserves every undeclared search
result and deterministic work field. Rounded end-to-end speedups over the
frozen v0.8 wheel are:

| Position | N2 speedup over v0.8 | Output/proof/work |
| --- | ---: | --- |
| S1 | about 7.9x | identical |
| S3 | about 2.0x | identical |
| published S4 mate | about 1.5x | identical, apart from the pinned sound-proof correction |
| live S22 adjudication state | about 1.3x | identical |

The benchmark deliberately omits the old exact-trajectory pool comparison:
v0.9's new tactical lane can choose a different legal game, so that would no
longer be an apples-to-apples speed measurement. The raw benchmark is
[`native-boundary-search-v0.9.0-cp314-windows-depth2.json`](benchmarks/results/native-boundary-search-v0.9.0-cp314-windows-depth2.json).

Natural-selection run `70e97c0e-596f-4cb3-b4a9-f5024e42ac7d` then completed
two v0.9 generations under the unchanged fixed-suite gate. Generation-one
finalist `spc-5db5dfc40c211826` scored pair W/D/L `6/16/3` and `0.560`;
generation-two finalist `spc-71dde5190fddd22a` scored `4/18/3` and `0.520`.
Both were rejected because they missed the required-win gate and lost three
pairs. The three fresh 100-game reports were also mixed: the self-play-fit
candidate recorded conclusive game W/D/L `50/0/49` plus one incomplete, the
older `spc-d14d1dae18b54c23` recorded `47/2/48` plus three incompletes, and
`spc-5db5dfc40c211826` recorded `47/4/48` plus one incomplete. None of this is
Stockfish-level evidence, and baseline `spc-68942034c41b4cc4` remains the only
champion.

## Evidence language

Static or depth-limited scores are heuristic, not proofs. The CLI reserves
`forced` for checkmates or draws actually established by the searched tree.
Reports include engine version, depth, nodes, transposition counts, elapsed
time, principal variations, search limits, a source fingerprint, and whether
any branch cap or deadline made the result selective. Incomplete timed runs use
separate filenames so they cannot overwrite the last completed evidence.
