# Published and engine reply comparison

Generated: `2026-08-28T01:46:39+00:00`<br>
Engine: `spc-0.9.0`<br>
Source fingerprint: `47defd096b2ded42`<br>
Rules: `scottish-modern-common-v1`
Horizon: White 1 move, fixed Black candidate 2-move series, then a screened White 3-move response.

> Only named historical/engine candidate Black replies are compared. This is not exhaustive over all Black series and is not a proof.

## 1.e4

Lower scores are better for Black. A deterministic tactical/diversity frontier retains at most 64 White candidates per layer/node; this response search is selective when pruning occurs.

| Black series | Source | Score after White's best response | Best White series | Unique / raw White series |
|:---|:---|---:|:---|---:|
| e6 / Ke7 | selective engine candidate | +661 | Qg4 / Qxg7 / Qxh8 | 5225 / 10397 |
| e5 / Qe7 | Italian opening-book prior | +687 | d3 / Bg5 / Bxe7 | 4849 / 10517 |
| d5 / dxe4 | Italian opening-book prior | +708 | Qg4 / Qxc8 / Bb5+ | 3570 / 7127 |
| e5 / f6 | community drawing hypothesis | +763 | Bc4 / Bxg8 / Qh5+ | 3330 / 6327 |
| d5 / e5 | Italian opening-book prior | +784 | Qg4 / Qxc8 / Bb5+ | 4148 / 7686 |
| d5 / d4 | Italian opening-book prior | +808 | Qg4 / Qxc8 / Bb5+ | 3438 / 6643 |
| e5 / Qh4 | two-series engine baseline | +1225 | Qg4 / Qxh4 / Ke2 | 4803 / 10834 |
| e6 / Nf6 | Italian opening-book prior | +1235 | Qg4 / Qxg7 / Qxh8 | 8372 / 23770 |
| e5 / Nh6 | Italian opening-book prior | +1339 | Qg4 / Qxg7 / Qxh8 | 4933 / 13655 |
| d5 / Nc6 | Italian opening-book prior | +1497 | Ba6 / Bxb7 / Bxc6+ | 4144 / 7193 |

## 1.d4

Lower scores are better for Black. A deterministic tactical/diversity frontier retains at most 64 White candidates per layer/node; this response search is selective when pruning occurs.

| Black series | Source | Score after White's best response | Best White series | Unique / raw White series |
|:---|:---|---:|:---|---:|
| e5 / Bb4+ | two-series engine baseline | +412 | Qd2 / Qxb4 / Kd2 | 15625 / 50430 |
| e6 / Bb4+ | selective engine candidate | +512 | Qd2 / Qxb4 / Kd2 | 3919 / 8903 |
| c5 / cxd4 | Italian opening-book prior | +555 | Bd2 / Ba5 / Bxd8 | 7802 / 15867 |
| d5 / Nc6 | Italian opening-book prior | +597 | Bf4 / Bxc7 / Bxd8 | 5054 / 11151 |
| c5 / d5 | Italian opening-book prior | +611 | Bg5 / Bxe7 / Bxd8 | 5370 / 10497 |
| d5 / c6 | community drawing hypothesis | +703 | Bg5 / Bxe7 / Bxd8 | 4958 / 9272 |
| d5 / h5 | Italian opening-book prior | +713 | e3 / Qxh5 / Qxh8 | 5536 / 11661 |
| e5 / e4 | community refutation candidate | +717 | Bg5 / Bxd8 / Kd2 | 14648 / 31684 |
| d6 / Nf6 | Italian opening-book prior | +749 | Bh6 / Bxg7 / Bxf6 | 8718 / 18780 |
| f5 / Nf6 | Italian opening-book prior | +759 | Bh6 / Bxg7 / Bxf6 | 7973 / 15784 |
| e5 / exd4 | Italian opening-book prior | +841 | Bg5 / Bxd8 / Bg5 | 8121 / 17085 |

## Interpretation limit

The comparison extends the two-series baseline with a searched White tactical series for each named reply. It still cannot establish the best Black reply: unlisted Black series remain possible, the White frontier is selective, and leaf values are heuristic.
