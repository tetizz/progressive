# Exact bitboard search identity

## Decision

GO after independent audit and an EP-normalization re-audit. The final audit
found zero P0, P1, or P2 issues. No commit, push, or deployment had been made
when this report was frozen.

The stable public/database identity remains the verified Progressive Zobrist
plus boundary FEN. Internal search tables now use a collision-free tuple of
the canonical piece/color bitboards, mover, cleaned castling rights, orthodox
and Progressive en-passant state, series number, and quiet-series count. This
removes repeated FEN serialization and Polyglot hashing without changing the
rules, generated legal set, work accounting, evaluation, ordering, or pruning.

Orthodox en-passant is normalized to the exact file semantics used by Polyglot
and Progressive targets are sorted exactly like the public key. This preserves
the verified public equivalence classes even after valid direct board mutation.

The TT and generation cache continue to append exact halfmove/fullmove clocks,
promoted provenance, and Chess960 mode. Only the clockless evaluation, quiet
adjudication, TT, and complete-series generation cache identities changed.

## Exactness evidence

- Random differential: 512 deterministic Series 1-8 roots at D4/width 4,
  zero mismatches. The comparison includes the complete result signature,
  concrete replay PFENs, PVs, alternatives, proofs, classifications, and every
  deterministic `SearchStats` field.
- Independent adversarial equivalence matrix: 47,834 states and 15,543 public
  and private key classes, with zero wrong-reuse collisions and zero
  over-partition mismatches. Coverage included all 960 starts with all 16
  castling-right subsets, capturable/inert/pinned en-passant, reordered and
  duplicate Progressive targets, clocks, promoted provenance, Chess960, and
  direct board mutation.
- Full suite: 611 passed, exactly six inherited failures, four explicit hard
  skips, 139.64 seconds. The failures are the established five promotion debts
  plus the intentionally stale normal-wheel report gate.
- Production opt-ins: 4 passed in 56.53 seconds (hard S4 D5, S7 semantic/work
  parity, early-S4 cap-832 mate safety, and root-only tactical reserve).
- Focused exact-key and generation-cache tests: 10 passed. They verify identical
  clock/promoted/Chess960 equivalence classes, every Progressive overlay,
  randomized bidirectional equivalence to the verified public key, inert
  orthodox and reordered Progressive en-passant mutation, public board
  mutation, and exact cache isolation.
- Fresh omit-stale CP314 wheel installed from an unrelated directory and
  returned initial D2 `g2g3`, score -150, 30,610 work, no timeout/work limit.
  Legacy native identity is unchanged at
  `33c8235e2287f2ea0bf87c60e69996ce376b2f2fd96ea65f41aa0d478aaa74e1`.

## Performance evidence

Every paired run alternated order and returned the same move, score, depth, and
work.

- Initial D3, nine samples per side: median 1.0249064s public text key versus
  0.9301486s exact bitboard key, 10.19% faster; 244,372 work on both.
- Initial D4, two samples per side: mean 10.7582872s versus 10.2738522s,
  4.72% faster; 2,502,640 work on both.
- Final repaired 512-position Series 1-8 differential: aggregate 16.5301355s
  versus 15.0467915s, 9.86% faster.

There is no additional slot or retained global cache. A D3 tracemalloc probe
showed peak Python memory slightly lower (4,998,621 to 4,945,568 bytes), while
post-search retained tracing was 152,136 bytes higher. A separate synthetic
5,000-key retained-size sample measured about 2.16 MB for bitboard tuples versus
1.11 MB for public text keys, so this should be treated as a speed-for-live-key
memory tradeoff rather than a memory reduction.

## Frozen artifacts

- `src/scottish_progressive/model.py`:
  `7ad93bd211d1a1097c6908224f1655d0cfc4402b3a7725d48766356eecbb2f5a`
- `src/scottish_progressive/search.py`:
  `3ece380e6ec0337a484ccc2e9ad92bbc896a60282e828b22a09061c7a359b872`
- `tests/test_search_exact_key.py`:
  `a68299a1b3875a8872d4517f0ee441b9497435acf5237864fe95f6c55fb97a77`
- `benchmarks/search_exact_key_parity.py`:
  `5f24c4847c265758747f3efa4d7c2e94048e47e689c8ff1482860976e9dd62a6`
- Omit-stale CP314 wheel, 462,931 bytes:
  `7946e1e567494bb0f3374bb6f5e36546daf61f419677e28e3e13fa67e22eb3d7`
- Installed engine fingerprint: `b8ff25b7f9a17dcf`.

`lab-artifacts-final` and `lab-artifacts-ep-normalized` are audit evidence only
and are not intended for the source commit.
