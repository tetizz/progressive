# Cycle 14 pawn-sacrifice checked-leaf extension

Date: 2026-08-27

## Claim

This candidate fixes one certified selective-search horizon error. It does not
establish a general strength gain or Stockfish-level play.

At a checked Series-6 boundary from the retained `1.b3` depth-5 line, the
static evaluator scored White `+522`. Black's retained width-32 reply
`f7e7/d8e8/e8h5/e7e8/h5e5/e5a1` evades check and wins the a1 rook, producing
`-2641`. The candidate searches one real reply series at this narrow class of
leaves and returns `-2641` with 2,467 total charged work, an empty exposed PV,
unknown proof bounds, and no leaf transposition-table write.

The trigger is color-symmetric and requires all of the following:

- the exact leaf is in check at Series 6 or later;
- the static check term is what crosses a 500-point apparent advantage;
- the checking side is exactly one pawn behind.

The one-pawn condition binds the heuristic to the certified pawn-sacrifice
motif. Broader experiments were rejected because they added substantial work
without changing match results.

## Deterministic native work

Both opening comparisons used depth 5, retained width 32, one native thread,
and a 100,000,000-work ceiling. Wall time was intentionally not used as the
acceptance metric because workstation load varied materially between runs.

| Root | Baseline work | Candidate work | Delta | Candidate extensions | Move/PV changed |
| --- | ---: | ---: | ---: | ---: | --- |
| Initial position | 14,714,063 | 14,733,882 | +19,819 (+0.135%) | 12 | No |
| After `1.e4` | 9,058,570 | 9,062,460 | +3,890 (+0.043%) | 1 | No |

The existing early-Series-4 depth-4 anchor remained exactly 6,716,498 work
with zero new extensions. The opt-in hard Series-4 depth-5 gate completed under
its ten-million-work ceiling.

## Fixed-suite prescreen

The deterministic 20-pair color-swapped prescreen completed 40/40 games and
20/20 pairs with no technical, integrity, or swap failures. Candidate game WDL
was 20-0-20; paired WDL was 0-20-0; both score rates were 50%; the exact
one-sided sign-test p-value was 1.0. Baseline and candidate each used 2,770,413
work across 49 searches in this sample.

The new predicate did not activate in those games. The result therefore proves
non-regression only; it is not positive strength evidence. Full and compact
machine-readable receipts are stored beside the other benchmark results.

## Validation before source freeze

- Focused rules/search/native gate: 245 passed, 1 optional skip.
- Hard Series-4 depth-5 gate: 1 passed.
- Full suite: 1,096 passed, 4 optional skips, with five expected pre-promotion
  failures: one pre-existing Windows EOL mismatch plus four identity/artifact
  gates that intentionally reject modified native source before a source-pinned
  commit and genuine report/WASM regeneration.

Candidate identities at this checkpoint:

- engine source fingerprint: `f3ce2d85406c32e6`
- native source identity:
  `33438dda660c03fd7716038f839193bacda64794082964aab26b5826213bd770`

The browser bundle was not relabeled. Promotion still requires a source-pinned
commit, fresh opening reports, native identity pins, a rebuilt and certified
WASM bundle, browser parity, and new Opera receipts.
