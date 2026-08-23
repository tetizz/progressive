# Published and engine reply comparison

Generated: `2026-08-23T15:33:50+00:00`<br>
Engine: `spc-0.9.0`<br>
Source fingerprint: `e41dc0e25186d438`<br>
Rules: `scottish-modern-common-v1`
Horizon: White 1 move, fixed Black candidate 2-move series, then a screened White 3-move response.

> Only named historical/engine candidate Black replies are compared. This is not exhaustive over all Black series and is not a proof.

## 1.e4

Lower scores are better for Black. A deterministic tactical/diversity frontier retains at most 64 White candidates per layer/node; this response search is selective when pruning occurs.

| Black series | Source | Score after White's best response | Best White series | Unique / raw White series |
|:---|:---|---:|:---|---:|
| e6 / Ke7 | selective engine candidate | +661 | Qg4 / Qxg7 / Qxh8 | 2510 / 4964 |
| e5 / Qe7 | Italian opening-book prior | +687 | d3 / Bg5 / Bxe7 | 2402 / 5162 |
| d5 / dxe4 | Italian opening-book prior | +708 | Qg4 / Qxc8 / Bb5+ | 2495 / 5020 |
| e5 / f6 | community drawing hypothesis | +763 | Bc4 / Bxg8 / Qh5+ | 2239 / 4366 |
| d5 / e5 | Italian opening-book prior | +784 | Qg4 / Qxc8 / Bb5+ | 2474 / 4976 |
| d5 / d4 | Italian opening-book prior | +808 | Qg4 / Qxc8 / Bb5+ | 2413 / 4959 |
| e5 / Qh4 | two-series engine baseline | +1225 | Qg4 / Qxh4 / Ke2 | 2056 / 5120 |
| e6 / Nf6 | Italian opening-book prior | +1267 | e5 / exf6 / fxg7 | 2326 / 4383 |
| e5 / Nh6 | Italian opening-book prior | +1339 | Qg4 / Qxg7 / Qxh8 | 2393 / 4983 |
| d5 / Nc6 | Italian opening-book prior | +1497 | Ba6 / Bxb7 / Bxc6+ | 2611 / 5130 |

## 1.d4

Lower scores are better for Black. A deterministic tactical/diversity frontier retains at most 64 White candidates per layer/node; this response search is selective when pruning occurs.

| Black series | Source | Score after White's best response | Best White series | Unique / raw White series |
|:---|:---|---:|:---|---:|
| e5 / Bb4+ | two-series engine baseline | +496 | Bd2 / Bxb4 / dxe5 | 1275 / 1742 |
| e6 / Bb4+ | selective engine candidate | +512 | Qd2 / Qxb4 / Kd2 | 1216 / 1682 |
| c5 / cxd4 | Italian opening-book prior | +565 | Bg5 / Bxe7 / Bxd8 | 2961 / 5471 |
| d5 / Nc6 | Italian opening-book prior | +597 | Bf4 / Bxc7 / Bxd8 | 2145 / 3715 |
| c5 / d5 | Italian opening-book prior | +611 | Bg5 / Bxe7 / Bxd8 | 2604 / 4687 |
| d5 / c6 | community drawing hypothesis | +703 | Bg5 / Bxe7 / Bxd8 | 2150 / 3643 |
| d5 / h5 | Italian opening-book prior | +713 | e3 / Qxh5 / Qxh8 | 2396 / 4141 |
| e5 / exd4 | Italian opening-book prior | +915 | Bg5 / Bxd8 / Bxc7 | 2972 / 5909 |
| e5 / e4 | community refutation candidate | +1089 | Bg5 / Bxd8 / Bxc7 | 2049 / 3528 |
| d6 / Nf6 | Italian opening-book prior | +1273 | Bh6 / Bxg7 / Bxh8 | 2393 / 4116 |
| f5 / Nf6 | Italian opening-book prior | +1285 | Bh6 / Bxg7 / Bxh8 | 2508 / 4247 |

## Interpretation limit

The comparison extends the two-series baseline with a searched White tactical series for each named reply. It still cannot establish the best Black reply: unlisted Black series remain possible, the White frontier is selective, and leaf values are heuristic.
