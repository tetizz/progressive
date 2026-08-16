# Scottish Progressive Chess rules and decisions

## Sources

1. Timo Honkela, *Progressive Chess*, updated 7 January 1996 to conform to
   Scottish rules: <https://users.ics.aalto.fi/tho/chess.html>
2. Primary 1996 WIPCC games, including en passant, castling, counterchecks,
   and promotion reuse: <https://users.ics.aalto.fi/tho/wipcc96final.html>
3. Vito Janko and Matej Guid, *A Program for Progressive Chess*, Theoretical
   Computer Science 644 (2016), 76-91:
   <https://doi.org/10.1016/j.tcs.2016.06.028>
4. Hans Bodlaender, *Progressive Chess*, The Chess Variant Pages:
   <https://www.chessvariants.org/multimove.dir/progressive.html>
5. D. B. Pritchard, *The Encyclopedia of Chess Variants* (book; the historical
   rules are also excerpted in Scottish Correspondence Chess Association
   magazine 50): <https://www.scottishcca.co.uk/members/mag50.pdf>
6. FIDE Rules Commission, *FIDE Laws of Chess*, used only for the underlying
   orthodox piece-movement, check, promotion, and castling rules:
   <https://rcc.fide.com/fide-laws-of-chess_fulltexthtml/>

The sources agree on the defining Scottish rule: check may be given on any
move and immediately ends the series. This differs from Italian Progressive
Chess, where check is permitted only on the final scheduled move.

## Engine interpretation

- A state stored in the high-level tree is a **series boundary**. Its series
  number is also its move budget. Odd series belong to White; even series to
  Black.
- Every intra-series position must be legal for the moving king. The opponent
  does not make replies between moves.
- A checking move is a complete legal series even if it leaves moves unused.
  The opponent receives the next normal series number; unused moves never
  transfer.
- If the checked player has no legal first move, the checking move is mate.
- If a player has no legal move without being checked, including after having
  started but before finishing a series, the game is a draw by progressive
  stalemate.
- Castling is one intra-series move and is legal only under the orthodox
  king-path and castling-right rules.
- All four promotion choices are generated. A promoted piece may move again
  later in the same series unless its promotion gave check and ended it.
- En-passant eligibility is carried separately through the previous series.
  It is offered only for the first move of the next series.
- The modern/common ten-series no-progress clock is stored after ten complete
  series with neither a pawn move nor a capture. Sources add an unbounded
  "unless impending mate can be shown" exception, so the engine marks this as
  `manual-proof-required` instead of silently declaring a draw. The engine
  proves dead-material cases, searches for mate anywhere in the currently
  allotted series, and otherwise leaves the claim unresolved. A future
  dedicated proof adjudicator can handle longer "impending mate" claims.
- Orthodox repetition is not added to this rules profile. An exact progressive
  state includes the globally increasing series number and move budget, so the
  same board at a later series does not have the same legal continuations.
  Historical NOST rules likewise explicitly disallow repetition draws. Search
  transpositions are still merged when full progressive state is identical.

The quiet-series point is isolated as adjudication policy so a federation- or
tournament-specific proof convention can be substituted without changing move
legality.
