# Cycle 5 frontier-cache checkpoint

Date: `2026-08-26`

Comparison base: `46d5c0a8c113ae40dbba657f28dc266c44eb4428`

Candidate source fingerprint: `a71198a160691211`

## Accepted changes

- Key complete-series frontier inspection by board geometry rather than the
  halfmove/fullmove clocks. The cached operation uses material, attacks, legal
  checks/mates, captures, and promotions; quiet adjudication continues to use
  the full state and its clocks separately.
- Raise the native weighted complete-series cache from `16,384` to `65,536`,
  matching the desktop browser geometry. Search width remains 32.
- Refresh deterministic work receipts and source-bound opening reports.

## Deterministic D4 evidence

All comparisons used one native thread, no wall deadline, and identical
positions, limits, profile, score, selected series, proof, and full PV.

| Position | Base median | Board-cache median | Base work | Board-cache work | Final 65K work | Combined work change |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Initial | 7.0415 s | 6.8218 s | 2,351,812 | 2,249,372 | 2,229,039 | -5.22% |
| Black after `e4` | 7.9793 s | 7.6406 s | 2,506,980 | 2,354,035 | 2,334,657 | -6.87% |

The median timing comparison isolates the board-only frontier cache across
three alternating fresh-process samples: 3.12% faster from the initial
position and 4.24% faster for Black after `e4`. The 65K result is an additional
deterministic work comparison; it removed all observed D4 series-cache
evictions. Its measured peak-working-set cost was about 15 MB. Raising the
cache again to 131K produced no further work reduction and was rejected.

At the fixed five-second Faster deadline, all six candidate samples still
completed depth 3 and chose the same series as the base. Median charged-work
throughput increased 1.96% from the initial position and 2.74% for Black after
`e4`.

## Rejected experiment

A proof-only whole-series history ordering experiment preserved the exact
result but increased initial-position D4 work by 0.53% and did not improve the
Black-after-`e4` anchor. It was removed rather than merged.

## Correctness and release evidence

- Repository gate: 960 tests passed with four opt-in release gates skipped;
  the separately executed hard S4 D5, S7 parity, early-S4 tactical safety, and
  hard native D5 gates all passed.
- Cold wheel packaging passed after regenerating all three opening-report pairs
  for the candidate fingerprint. Their chess payloads are unchanged; only
  identity and timing metadata differ.
- A clean-source WASM build passed dependency closure, root-session smoke,
  native/WASM root differential, prefix parity, browser prefix contract, and
  mate parity. The S7 staged mate stayed identical while work fell from 48,777
  to 45,694 (6.32%).
- Real Opera GX certification completed W32 D5 with the same selected series,
  score, proof bounds, full PV, and all 20 rival bounds in three schedules:
  warm wave 8 in 33.567 seconds, warm wave 4 in 36.988 seconds, and cold D5 in
  32.947 seconds. The seven-receipt release promotion passed and produced the
  tracked website bundle for fingerprint `a71198a160691211`.

## Limits of this checkpoint

This is a modest exact-cache optimization, not a Stockfish-level result. It
does not demonstrate depth 5 under ten seconds: Faster remains depth 3 at five
seconds in the native anchors. The certified eight-Worker browser path reaches
depth 5 in about 33-37 seconds, which is a real completion improvement but is
still well outside the sub-10 target.
