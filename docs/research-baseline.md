# Published theory baseline

Research snapshot: 16 August 2026.

## What is and is not established

No source located gives a solved or academically established ranking of all 20
first moves under Scottish rules. The responsible baseline is narrower:

- `1.e4` and `1.d4` dominate historical serious play.
- A tentative 2016 Bucephalus-assisted community analysis proposed `1.d4` as
  possibly strongest, left `1.e4` unresolved, and claimed that the other 18
  moves lose. Its author also stressed that one new series could overturn a
  variation. This project records that as `COMMUNITY_CLAIM_2016`, never as
  ground truth.
- The 2016 peer-reviewed engine and most large historical data use **Italian**
  checking rules. Their lines are candidate data that must be revalidated
  under Scottish early-check semantics.

Sources:

- Vito Janko and Matej Guid, [A Program for Progressive Chess](https://doi.org/10.1016/j.tcs.2016.06.028)
- [Open author PDF](https://chesslife.io/matej/doc/A_Program_for_Progressive_Chess.pdf)
- [Doug Hyatt's 2016 Progressive Chess theory discussion](https://www.chess.com/forum/view/chess960-chess-variants/progressive-chess2)
- [Timo Honkela's WIPCC games and strategic notes](https://users.ics.aalto.fi/tho/chess.html)
- Malcolm Horne, [first U.K. postal tournament report, Variant Chess 1](http://www.mayhematics.com/v/vol1/vc01.pdf)

## Historical hypotheses to challenge

The following are test hypotheses, not evaluation labels:

1. Only `1.d4` and `1.e4` survive best defense.
2. After `1.d4`, the series `d5 / c6` may be Black's most resilient reply.
3. After `1.e4`, `e5 / f6` may hold, while the overall result remains unclear.
4. After most other first moves, `e5 / e4` is a candidate refutation.

Useful seed variations from the same community analysis:

- `1.d4 2.c5 / cxd4 3.a4 / e4 / e5`
- `1.d4 2.d5 / Nc6 3.Bf4 / Bxc7 / Bxd8`
- `1.d4 2.d5 / Nf6 3.e4 / e5 / Bb5+`
- `1.d4 2.e5 / exd4 3.Bg5 / Bxd8 / f4`
- `1.e4 2.d5 / dxe4 3.d3 / dxe4 / Qxd8+`
- `1.e4 2.d5 / e5 3.Qg4 / Qxc8 / Qxd8+`
- `1.e4 2.e5 / Nh6 3.d4 / Bg5 / Bxd8`
- `1.e4 2.d5 / d4 3.Qg4 / Qxc8 / Qxd8+`

The current engine must be allowed to disagree with every one of them.

## Strategy features supported by prior work

- Search for seriesmate before material gain, then test the opponent's entire
  next series for mating resources.
- Open squares around the king often matter more than orthodox pawn shelter.
- Promotion and underpromotion are opening/middlegame concerns. An
  underpromotion can avoid a premature checking move that would truncate the
  series.
- Bishops' long range is often valuable early; knights can improve in sparse
  endings.
- Queens are powerful but unusually exposed to multi-capture raids.
- A check on the last scheduled move can be efficient, but an early Scottish
  check may be stronger when its forced first reply outweighs forfeited moves.
- Plain Monte Carlo is a poor default because volatile positions often have
  only one viable series.

These informed the initial evaluation terms, but their numeric weights remain
uncalibrated.

## External data candidates

- PRBASE is a historical Italian database reported at either 654 or 2,970
  games depending on release. It must be normalized, deduplicated, and
  Scottish-revalidated before training or evaluation claims.
- [VitoJanko/ProgressiveChess](https://github.com/VitoJanko/ProgressiveChess)
  contains an Italian-rule Android engine and opening asset. The repository
  had no license in the reviewed snapshot, so its code/data is not copied.
- The asset gives useful human/engine priors after `1.e4` and `1.d4`, but its
  weights are selection probabilities, not objective scores.

## Tactical regression queue

The exact FEN must always be paired with series number, because FEN alone is
not a complete progressive state.

| Purpose | FEN | Series | Expected series |
|:---|:---|---:|:---|
| Checked-start mate | `rnbqkb1r/ppp1pppp/5n2/1B1P4/8/5N2/PPPP1PPP/RNBQK2R b KQkq - 1 3` | 4 | `c6 / Qb6 / Ne4 / Qxf2#` |
| Wrong-defense punishment | `rn1qkb1r/ppp1pppp/5n2/3P4/8/5N2/PPPP1PPP/RNBbK2R w KQkq - 0 7` | 5 | `Ne5 / g4 / g5 / g6 / gxf7#` |
| Underpromotion avoids early check | `bnq1nr2/p1pp1pk1/8/4PP2/1P2P1p1/8/P1P2KP1/BNbBN2r w - - 0 1` | 7 | `Nf3 / Nd4 / e6 / e7 / exf8=R / Rxh8 / Ne6#` |
| Capture-promotion then reuse | `7R/pp3p1p/1p3k2/3P4/1b6/5P2/PPP2P1P/RNK5 b - - 0 1` | 8 | `Bd6 / b5 / b4 / b3 / bxa2 / axb1=N / Nc3 / Bf4#` |
| U.K. Scottish tournament mate | `rnbqkb1r/ppp1pppp/5n2/3p2B1/3P4/2N2N2/PPP1PPPP/R2QKB1R b KQkq - 4 3` | 4 | `Ne4 / Qd6 / Qg3 / Qxf2#` |

The first implementation milestone uses smaller unit fixtures for fast rules
coverage. These longer anchors are the next mate-solver and evaluation-tuning
suite.
