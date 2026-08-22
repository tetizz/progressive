# Published and engine reply comparison

Generated: `2026-08-22T21:02:04+00:00`<br>
Engine: `spc-0.9.0`<br>
Source fingerprint: `218625767141aef7`<br>
Rules: `scottish-modern-common-v1`
Horizon: White 1 move, fixed Black candidate 2-move series, then a screened White 3-move response.

> Only named historical/engine candidate Black replies are compared. This is not exhaustive over all Black series and is not a proof.

## 1.e4

Lower scores are better for Black. A deterministic tactical/diversity frontier retains at most 64 White candidates per layer/node; this response search is selective when pruning occurs.

| Black series | Source | Score after White's best response | Best White series | Unique / raw White series |
|:---|:---|---:|:---|---:|
| e6 / Ke7 | selective engine candidate | +1137 | Qg4 / Qxg7 / Qxf8+ | 2510 / 4964 |
| e5 / Qh4 | two-series engine baseline | +1560 | Qg4 / Qxh4 / Qe7+ | 2056 / 5120 |
| e5 / f6 | community drawing hypothesis | +1602 | Qf3 / Qxf6 / Qxd8+ | 2239 / 4366 |
| e5 / Qe7 | Italian opening-book prior | +1646 | Qh5 / Qxe5 / Qxe7+ | 2402 / 5162 |
| e5 / Nh6 | Italian opening-book prior | +1733 | Qg4 / Qxg7 / Qxf8+ | 2393 / 4983 |
| d5 / dxe4 | Italian opening-book prior | +1777 | Qg4 / Qxc8 / Qxd8+ | 2495 / 5020 |
| e6 / Nf6 | Italian opening-book prior | +1863 | Qf3 / Qxf6 / Qxd8+ | 2326 / 4383 |
| d5 / e5 | Italian opening-book prior | +1869 | Qg4 / Qxc8 / Qxd8+ | 2474 / 4976 |
| d5 / d4 | Italian opening-book prior | +1899 | Qg4 / Qxc8 / Qxd8+ | 2413 / 4959 |
| d5 / Nc6 | Italian opening-book prior | +1923 | Qg4 / Qxc8 / Qxd8+ | 2611 / 5130 |

## 1.d4

Lower scores are better for Black. A deterministic tactical/diversity frontier retains at most 64 White candidates per layer/node; this response search is selective when pruning occurs.

| Black series | Source | Score after White's best response | Best White series | Unique / raw White series |
|:---|:---|---:|:---|---:|
| e5 / Bb4+ | two-series engine baseline | +908 | Qd2 / Qxb4 / Qf8+ | 1275 / 1742 |
| e6 / Bb4+ | selective engine candidate | +947 | c3 / Bg5 / Bxd8 | 1216 / 1682 |
| d5 / h5 | Italian opening-book prior | +1003 | Bf4 / Bxc7 / Bxd8 | 2396 / 4141 |
| d5 / Nc6 | Italian opening-book prior | +1021 | e4 / Bb5 / Bxc6+ | 2145 / 3715 |
| e5 / exd4 | Italian opening-book prior | +1247 | Qxd4 / Qf6 / Qxd8+ | 2972 / 5909 |
| d6 / Nf6 | Italian opening-book prior | +1338 | Bh6 / Bxg7 / Bxh8 | 2393 / 4116 |
| f5 / Nf6 | Italian opening-book prior | +1350 | Bh6 / Bxg7 / Bxh8 | 2508 / 4247 |
| e5 / e4 | community refutation candidate | +1382 | Qd2 / Qg5 / Qxd8+ | 2049 / 3528 |
| c5 / cxd4 | Italian opening-book prior | +1391 | Qxd4 / Qb6 / Qxd8+ | 2961 / 5471 |
| d5 / c6 | community drawing hypothesis | +1534 | Qd2 / Qa5 / Qxd8+ | 2150 / 3643 |
| c5 / d5 | Italian opening-book prior | +1724 | dxc5 / Qxd5 / Qxd8+ | 2604 / 4687 |

## Interpretation limit

The comparison extends the two-series baseline with a searched White tactical series for each named reply. It still cannot establish the best Black reply: unlisted Black series remain possible, the White frontier is selective, and leaf values are heuristic.
