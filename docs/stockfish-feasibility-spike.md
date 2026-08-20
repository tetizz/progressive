# Stockfish 18 feasibility spike

Status: external policy **rejected**; native Scottish fork **feasible but not yet
strength-proven**. This experiment is isolated from the trusted search, league,
CLI, and web paths.

## Decision

An unmodified Stockfish cannot be trained into a Scottish Progressive engine by
feeding it ordinary FENs or Scottish games. Its position model has one orthodox
en-passant square, its move transition always hands the turn to the opponent,
and its negamax recursion negates the score after every micro-move. Scottish
play can keep the same mover for many moves, can carry several en-passant
targets, and ends a series immediately on check. Those are search semantics,
not evaluation weights.

The cheap integration was tested anyway: Stockfish 18 ranked each micro-move,
while the project's rule implementation validated the move and replayed the
complete series. It was fast and produced legal output, but missed all five
known Scottish mating positions. Do not add this policy to the product or use
its moves as Scottish ground truth.

The route worth a separately gated experiment is a GPL Stockfish 18 source
fork at `tetizz/progressive`: keep its bitboards, incremental make/unmake,
transposition table, move ordering, NNUE inference machinery, and low-level CPU
optimizations, while replacing its turn transition, search recursion, terminal
rules, state hash, protocol, and training records with Scottish-aware versions.
Until that fork clears the gates below, the current Python champion remains the
only trusted engine and normal engine evolution should continue.

## Reproducible evidence

The inspected upstream source was the official `sf_18` tag at commit
`cb3d4ee9b47d0c5aae855b12379378ea1439675c`. The local Winget executable
identified itself over UCI as `Stockfish 18`; its SHA-256 was
`c86215fa1977d53b82ed854540a4c7b025be4cd042276c85ba3de53fb9118911`. The
binary was executed in place and was not copied or redistributed.

Benchmark command:

```powershell
$sf = (Get-Command stockfish-windows-x86-64-avx2.exe).Source
.\.venv\Scripts\python -m experiments.stockfish.stockfish_policy `
  --stockfish $sf --nodes 2048 --multipv 8 --timeout 15 `
  --champion profiles\champion.json --champion-work 50000 `
  --output experiments\stockfish\benchmark-sf18-2048.json
```

Both sides were deterministic and single-threaded. Stockfish received 2,048
nodes per micro-move and a cleared 16 MiB hash before every query. The champion
used depth one complete series, its configured branch cap of 32, and a 50,000
work-position ceiling. The two work units are not equivalent, so this is a
position/tactical feasibility check, not an Elo match.

| Fixture | Stockfish policy | Current champion |
|:--|:--|:--|
| checked-start mate | legal, no mate, 0.012 s | mate, 0.459 s |
| wrong-defense punishment | legal, no mate, 0.013 s | alternate mate, 0.753 s |
| underpromotion avoids early check | legal early check, no mate, 0.003 s | no mate, 1.118 s |
| capture-promotion then reuse | legal, no mate, 0.022 s | no mate, 0.762 s |
| U.K. Scottish tournament mate | legal, no mate, 0.012 s | alternate mate, 0.542 s |

Summary: Stockfish policy 5/5 legal, **0/5 mates**; current champion **3/5
mates**. Timings are one local Windows 11 run on 20 August 2026 and are not a
general performance claim. Raw moves, work counts, exact-anchor flags, engine
identity, and timings are in
[`experiments/stockfish/benchmark-sf18-2048.json`](../experiments/stockfish/benchmark-sf18-2048.json).

## Why the micro-policy failed

After a non-checking Scottish move the adapter restores the same side to move,
then asks Stockfish another orthodox question. Each answer still assumes that
the opponent replies immediately after that one move. It therefore values such
moves as `f5-f6+` in the underpromotion fixture, even though that check throws
away six remaining moves and misses the Scottish mate. More nodes or a larger
MultiPV does not repair the objective; it only searches the wrong game more
accurately.

Stockfish output can still be an optional opening/move-order prior in a future
fork, but it must never supply legality, terminal results, training labels, or
promotion evidence.

## Minimum native fork

### 1. Exact position and undo state

Add this rule state to `Position`/`StateInfo` and to make/unmake snapshots:

```cpp
struct ProgressiveInfo {
    std::uint64_t seriesNumber;       // 1, 2, 3, ...; never silently capped
    std::uint64_t movesRemaining;     // before the next micro-move
    std::uint8_t quietSeries;         // saturates at 10 (proof-required)
    Bitboard boundaryEpTargets;       // all first-move e.p. targets
    Bitboard pendingEpTargets;        // double steps made in this series
    bool seriesProgressMade;          // pawn move or capture already occurred
};
```

`StateInfo` also needs `bool handedOff` so `undo_move()` knows whether the last
micro-move changed sides. `unusedMoves` and `endedByCheck` describe a transition
or returned series; they are not position identity.

Replace the single `epSquare` assumption in FEN parsing, legal en-passant move
generation, make/unmake, and hashing. On the first micro-move, generate the
union of legal captures for every bit in `boundaryEpTargets`. Clear those
rights after that move. Track every same-series double step in
`pendingEpTargets`; clear its target if that pawn moves again. At handoff,
publish the surviving pending targets as the next boundary targets.

The transposition key must include full `seriesNumber`, `movesRemaining`,
`quietSeries`, both e.p. bitboards, and `seriesProgressMade`. Orthodox
repetition and 50-move adjudication are disabled. The Scottish ten-quiet-series
condition remains `manual-proof-required` unless a draw or impending mate is
actually proved.

### 2. Make/unmake and terminals

Generalize the Stockfish move transition instead of toggling `sideToMove`
unconditionally:

1. Apply orthodox piece movement, capture, castling, promotion, king-safety,
   dirty-piece, and NNUE accumulator updates.
2. If the move gives check, end the series immediately, record all unused
   moves, hand off, and initialize the opponent's full next series.
3. Otherwise, if `movesRemaining == 1`, hand off normally.
4. Otherwise, decrement `movesRemaining`, retain the mover, clear boundary
   e.p. rights, recompute check/pin metadata for that mover, and continue.
5. No legal move while checked is mate. No legal move while not checked,
   including halfway through a series, is progressive stalemate and a draw.

The promoted piece remains on the board and can move again during the same
series. A promotion that gives check truncates immediately. These behaviors
must be rules tests before search tuning begins.

### 3. Search changes

Stockfish 18's main search makes one move and calls `-search(...)`; that sign
flip encodes alternating turns. The first correct fork should use a simple
series-aware alpha-beta core:

```cpp
transition = do_progressive_move(move);
if (transition.handedOff)
    value = -search(child, -beta, -alpha, depthSeries - 1);
else
    value =  search(child,  alpha,  beta, depthSeries);
```

Use a new stack frame for every micro-move so undo/history data remain sound,
but decrement search depth only at a complete-series handoff. Count mate
distance primarily in series and secondarily in micro-moves. Root output and
PV storage must group micro-moves into complete series.

Initially retain only correctness-safe infrastructure: legal move generation,
incremental make/unmake, alpha-beta, exact progressive TT keys, deterministic
move ordering, time/node stops, and NNUE evaluation. Disable null-move pruning,
ordinary quiescence, Syzygy, repetition logic, futility/razoring, LMR, singular
extensions, correction histories, and orthodox draw scaling. Re-enable each
heuristic only after a paired A/B test and tactical/rules gate; most encode
assumptions about alternating plies.

Add an explicit protocol mode such as `setoption name UCI_Variant value
ScottishProgressive`, plus commands that accept `seriesNumber`,
`movesRemaining`, quiet count, and comma-separated e.p. targets. Do not overload
ordinary FEN and silently lose rule state. Emit `bestseries` (or a documented
UCI `info string` series followed by the first `bestmove`) so callers cannot
mistake one micro-move for a complete turn.

### 4. NNUE features

The existing Stockfish board feature transformer and quantized CPU inference
are reusable. The current orthodox net is not a Scottish evaluator. Extend the
sparse input with:

- capped plus logarithmic buckets for `seriesNumber` and `movesRemaining`;
- a first-micro-move flag and `seriesProgressMade`;
- `quietSeries` buckets 0 through 10;
- one spatial feature per active boundary e.p. target and pending target;
- boundary-in-check and side perspective (the latter already exists in
  Stockfish's transformer ordering).

Exact uncapped values remain in the search state and TT even though evaluation
uses buckets. Early-check truncation is enforced by search and learned through
result/value labels; it must not be approximated by the network.

Test two initializations under the same data and match budget: scratch, and a
warm start that copies only compatible orthodox board-transformer weights and
randomly initializes progressive features/output layers. Do not call the warm
start “trained on Stockfish”; it transfers weights, while Scottish self-play
supplies the actual targets.

## Lossless training record

The official NNUE trainer's inspected 32-byte binpack entry contains an
orthodox position, one move, score, ply, result, and rule-50 counter. It has no
place for progressive state and therefore must not be used unchanged.

Start with canonical `spc-nnue-sample-v1` JSONL for auditing, then convert to a
versioned binary shard for throughput. Every pre-move sample contains:

```json
{
  "schema": "spc-nnue-sample-v1",
  "game_id": "stable-content-id",
  "board": "piece-placement-only FEN",
  "side_to_move": "w",
  "castling": "KQkq",
  "series_number": 7,
  "moves_remaining": 3,
  "quiet_series": 0,
  "boundary_ep_targets": ["d6", "f6"],
  "pending_ep_targets": ["e3"],
  "series_progress_made": true,
  "best_micro_move": "e7f8r",
  "best_series": ["e1f3", "f3d4", "e5e6", "e6e7", "e7f8r", "f8h8", "d4e6"],
  "transition": {
    "handed_off": false,
    "ended_by_check": false,
    "unused_moves": 0
  },
  "label": {
    "score_white": 1234,
    "result_white": 1,
    "proof": null
  },
  "provenance": {
    "rules": "scottish-modern-common-v1",
    "generator": "fork-commit-or-python-fingerprint",
    "search_limits": "structured-object",
    "seed": 1
  }
}
```

`boundary_ep_targets` is non-empty only before the first micro-move.
`pending_ep_targets` records double steps that can become next-series rights.
For a checking transition, set `ended_by_check=true`, `handed_off=true`, and
`unused_moves=moves_remaining-1`; this makes Scottish truncation auditable.
Use signed White-centric labels independent of side to move. Preserve unknown
results/proofs explicitly rather than converting incomplete searches to draws.

Split train/validation/test by whole game and opening-family hash before
emitting positions; never split adjacent micro-moves across sets. Deduplicate
by the complete progressive state above, not orthodox FEN. Oversample scarce
but rule-critical strata: checked starts, early checks with unused moves,
multi-e.p., underpromotions, promotion reuse, mid-series stalemate, and quiet
proof cases.

Training targets should come from Scottish self-play and deeper Scottish
search, mixed with terminal W/D/L. Orthodox Stockfish scores may be stored as
an auxiliary diagnostic but must have zero authority over Scottish legality or
terminal labels.

## GPU path on this machine

Read-only hardware detection found an NVIDIA GeForce RTX 5090 with 32,607 MiB
reported VRAM and driver 610.74. PyTorch is not installed in this project's
virtual environment; no training was attempted in this spike.

1. Fork the official `nnue-pytorch` trainer at inspected commit
   `9f72946529c4187d3679014036cd22c3be419716` alongside the engine fork.
2. Add the versioned progressive record reader and sparse features above to its
   C++ data loader and Python model. Reject any shard whose schema/rules hash
   differs.
3. Use the official Docker path with NVIDIA support (the upstream image
   includes a CUDA 12.x stack), mounting read-only raw shards and a separate
   checkpoint directory. Verify RTX 5090 support with a one-batch forward,
   backward, checkpoint, reload, and quantized-export smoke before a long run.
4. Train deterministic seeds with game-level held-out validation. Record data
   hashes, source commits, CUDA/PyTorch versions, batch size, loss mix, and
   checkpoint hash.
5. Serialize with a fork-specific NNUE architecture hash; the engine must
   reject an orthodox or wrong-schema net. Benchmark scalar and SIMD inference
   parity before matches.
6. Use GPU only for training. Engine matches run the shipped quantized net on
   pinned single CPU threads so GPU availability cannot alter playing strength.

The official trainer supports CUDA/ROCm Docker setup, validation datasets,
checkpoint resume, quantization, and automated net matches. Its stock game
runner and orthodox binpack format still need Scottish replacements.

## Promotion and kill gates

No “Stockfish-like” or strength claim is allowed before all gates pass.

1. **Rules differential:** zero mismatches across at least 100,000 seeded
   reachable micro-states against the Python rules oracle, including resulting
   board, handoff, unused moves, terminal, quiet count, castling, promotion, and
   every e.p. target. All existing rules tests pass unchanged.
2. **Tactical gate:** the fork finds a legal mate on all five published anchors
   at one declared deterministic limit. Constrained-route tests must preserve
   the underpromotion and promotion-reuse concepts; alternate valid mates are
   acceptable in unconstrained search.
3. **Determinism/safety:** identical single-thread runs give identical series
   and hashes; malformed progressive protocol state is rejected; ASan/UBSan
   and make/unmake round trips are clean.
4. **Search baseline:** the Scottish fork without a trained net must at least
   equal the current champion's tactical count (currently 3/5 in this spike)
   before spending a large GPU budget.
5. **Held-out match:** use at least 100 unique opening boundaries, color-swapped
   for 200 games, one pinned CPU thread each, equal wall-clock per series and
   equal hash. Games continue to mate/proven draw; emergency cutoffs are
   technical `*`, never draws or wins. Require score above 55%, a 95% Wilson
   lower bound above 50%, no opening-pair loss imbalance, and zero technical
   failures against the active champion. Repeat against the strongest retained
   non-champion if it differs.
6. **Net promotion:** two fixed training seeds must each beat the untrained fork
   and one must clear the champion match gate. Publish only the exact net/source
   pair that passed.

If the fork fails rules or tactical gates, stop immediately. If two bounded
training attempts fail the held-out match gate, archive the experiment and keep
improving the current engine's series generator, ordering, TT, selective
search, and progressive evaluation. A faster C++ engine that searches the
wrong game does not count as an improvement.

## Primary sources inspected

- [Official Stockfish 18 source tag](https://github.com/official-stockfish/Stockfish/tree/cb3d4ee9b47d0c5aae855b12379378ea1439675c)
- [Stockfish 18 position state and make/unmake API](https://github.com/official-stockfish/Stockfish/blob/cb3d4ee9b47d0c5aae855b12379378ea1439675c/src/position.h)
- [Stockfish 18 move transition implementation](https://github.com/official-stockfish/Stockfish/blob/cb3d4ee9b47d0c5aae855b12379378ea1439675c/src/position.cpp)
- [Stockfish 18 negamax search](https://github.com/official-stockfish/Stockfish/blob/cb3d4ee9b47d0c5aae855b12379378ea1439675c/src/search.cpp)
- [Stockfish 18 NNUE architecture](https://github.com/official-stockfish/Stockfish/tree/cb3d4ee9b47d0c5aae855b12379378ea1439675c/src/nnue)
- [Official Stockfish developer/testing guidance](https://official-stockfish.github.io/docs/stockfish-wiki/Developers.html)
- [Official NNUE PyTorch trainer](https://github.com/official-stockfish/nnue-pytorch/tree/9f72946529c4187d3679014036cd22c3be419716)
- [Inspected orthodox binpack entry](https://github.com/official-stockfish/nnue-pytorch/blob/9f72946529c4187d3679014036cd22c3be419716/data_loader/cpp/lib/training_data_entry.h)
- [Official detailed NNUE design/training document](https://github.com/official-stockfish/nnue-pytorch/blob/9f72946529c4187d3679014036cd22c3be419716/docs/nnue.md)

Stockfish is GPL v3. This repository is GPL-3.0-or-later, so a source fork is
license-compatible, but any distributed modified binary must ship the
corresponding modified source/license as required by Stockfish's terms.
