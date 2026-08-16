# Scottish Progressive Chess Research Engine

This repository is the rules-and-search first milestone for a Scottish
Progressive Chess theory project. It deliberately does not contain a web UI
yet. The current target is a correct series generator, reproducible
series-level search, a CLI, a small SQLite theory store, and an evidence-bound
ranking of the 20 initial moves.

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
```

## CLI examples

List every unique legal Black two-move series after `1.e4`:

```powershell
spc series --fen "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1" --series 2
```

Analyze an arbitrary boundary position, where one search ply is one complete
series:

```powershell
spc analyze --fen "<standard FEN>" --series 5 --depth 2
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
  adds White's three-move response for `1.e4` and `1.d4`, retaining 16 ordered
  series per searched node after full generation and transposition merging.
- [`reports/published-reply-comparison.md`](reports/published-reply-comparison.md)
  tests named historical and engine reply hypotheses with a 64-finalist White
  response screen.

The shallow run ranks `1.e3` and `1.Nf3` first, but that ordering collapses an
important horizon: White has not yet received the three-move series. In the
selective three-series extension, `1.e4` scores `+1137` and `1.d4` `+902` under
the current heuristic. Those are useful hypotheses only; branch pruning and
uncalibrated evaluation weights make neither a proof nor an objective answer.

## Evidence language

Static or depth-limited scores are heuristic, not proofs. The CLI reserves
`forced` for checkmates or draws actually established by the searched tree.
Reports include engine version, depth, nodes, transposition counts, elapsed
time, principal variations, search limits, a source fingerprint, and whether
any branch cap or deadline made the result selective. Incomplete timed runs use
separate filenames so they cannot overwrite the last completed evidence.
