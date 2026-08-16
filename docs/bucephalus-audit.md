# Bucephalus audit

Audited upstream snapshot:
[`0e11fcdc84e65122fd8b91cada71dad6323db417`](https://github.com/hyattpd/Bucephalus/tree/0e11fcdc84e65122fd8b91cada71dad6323db417)
(16 February 2016).

## Decision

Build a new engine. Bucephalus remains a useful historical prototype and a
source of differential fixtures, but its core is not a safe base for an
opening-theory solver.

## Useful ideas retained as independent contracts

- Represent all en-passant targets created during a series, not the one square
  available in orthodox FEN.
- Treat a same-side sequence as a first-class search concept.
- Preserve the terms *seriesmate* and *ghost* for future tactical solvers.

No Bucephalus source code is copied. Both projects are GPL-3.0-or-later, but an
independent implementation also avoids inheriting the defects below.

## Confirmed limitations and defects

- Search depth decreases per individual move, so a horizon can stop in the
  middle of a series instead of on a legal high-level boundary.
- Board transpositions omit series length and moves remaining, conflating
  different progressive states. The hash is not Zobrist and TT entries have no
  exact/lower/upper bound type.
- Progressive stalemate is detected but evaluated heuristically rather than as
  a draw.
- Strict score comparisons can turn an all-forced-mate node into a zero score.
- An alpha-beta cutoff links a principal-variation node and then frees it,
  creating use-after-free/double-free risk.
- The legal-move array has a fixed 100-entry capacity without a bounds check.
- Move ordering reads an uninitialized score slot and desynchronizes parallel
  move/score arrays.
- The position hash contains shifts that are undefined on Windows LLP64.
- The fixed 200-entry game record has no overflow guard.
- The evaluation is mostly orthodox material, individual-move mobility, and a
  small king-field term. It has no promotion-corridor or explicit series-reach
  feature.
- There are no tests, CI, FEN CLI, machine-readable output, database, time
  control, cancellation, or multithreading.
- The committed binary is Linux ELF and the POSIX Makefile is not directly
  usable on this Windows environment.

The upstream repository is GPL-3.0-or-later:
<https://github.com/hyattpd/Bucephalus/blob/0e11fcdc84e65122fd8b91cada71dad6323db417/LICENSE>.
