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

The shallow run ranks `1.g3` and `1.c3` first, but that ordering collapses an
important horizon: White has not yet received the three-move series. In the
selective three-series extension, `1.e4` scores `+848` and `1.d4` `+752` under
the current heuristic. Those are useful hypotheses only; branch pruning and
uncalibrated evaluation weights make neither a proof nor an objective answer.

## Evidence language

Static or depth-limited scores are heuristic, not proofs. The CLI reserves
`forced` for checkmates or draws actually established by the searched tree.
Reports include engine version, depth, nodes, transposition counts, elapsed
time, principal variations, search limits, a source fingerprint, and whether
any branch cap or deadline made the result selective. Incomplete timed runs use
separate filenames so they cannot overwrite the last completed evidence.
