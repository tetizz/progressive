# Published and engine reply comparison

Generated: `2026-08-20T05:53:41+00:00`<br>
Engine: `spc-0.4.0`<br>
Source fingerprint: `7b92ea2a6678d6be`<br>
Rules: `scottish-modern-common-v1`
Horizon: White 1 move, fixed Black candidate 2-move series, then a screened White 3-move response.

> Only named historical/engine candidate Black replies are compared. This is not exhaustive over all Black series and is not a proof.

## 1.e4

Lower scores are better for Black. A deterministic tactical/diversity frontier retains at most 64 White candidates per layer/node; this response search is selective when pruning occurs.

| Black series | Source | Score after White's best response | Best White series | Unique / raw White series |
|:---|:---|---:|:---|---:|
| e6 / Ke7 | selective engine candidate | +1137 | Qg4 / Qxg7 / Qxf8+ | 1964 / 3640 |
| e5 / Qe7 | Italian opening-book prior | +1560 | Qf3 / Qf6 / Qxe7+ | 1836 / 3827 |
| e5 / Qh4 | two-series engine baseline | +1560 | Qg4 / Qxh4 / Qe7+ | 1560 / 3980 |
| e5 / f6 | community drawing hypothesis | +1602 | Qf3 / Qxf6 / Qxd8+ | 1853 / 3500 |
| e5 / Nh6 | Italian opening-book prior | +1733 | Qg4 / Qxg7 / Qxf8+ | 1836 / 3723 |
| d5 / dxe4 | Italian opening-book prior | +1777 | Qg4 / Qxc8 / Qxd8+ | 1960 / 3634 |
| e6 / Nf6 | Italian opening-book prior | +1863 | Qf3 / Qxf6 / Qxd8+ | 1914 / 3262 |
| d5 / e5 | Italian opening-book prior | +1869 | Qg4 / Qxc8 / Qxd8+ | 1944 / 3673 |
| d5 / d4 | Italian opening-book prior | +1899 | Qg4 / Qxc8 / Qxd8+ | 1897 / 3492 |
| d5 / Nc6 | Italian opening-book prior | +1923 | Qg4 / Qxc8 / Qxd8+ | 2002 / 3487 |

## 1.d4

Lower scores are better for Black. A deterministic tactical/diversity frontier retains at most 64 White candidates per layer/node; this response search is selective when pruning occurs.

| Black series | Source | Score after White's best response | Best White series | Unique / raw White series |
|:---|:---|---:|:---|---:|
| f5 / Nf6 | Italian opening-book prior | +799 | Qd2 / Qb4 / Qxe7+ | 1948 / 3341 |
| d5 / h5 | Italian opening-book prior | +864 | e4 / Qxh5 / Qxf7+ | 1889 / 3299 |
| c5 / cxd4 | Italian opening-book prior | +867 | Bd2 / Ba5 / Bxd8 | 2036 / 3587 |
| d6 / Nf6 | Italian opening-book prior | +906 | Nc3 / Nd5 / Nxf6+ | 1972 / 3320 |
| e5 / Bb4+ | two-series engine baseline | +908 | Qd2 / Qxb4 / Qf8+ | 1275 / 1742 |
| e6 / Bb4+ | selective engine candidate | +947 | c3 / Bg5 / Bxd8 | 1216 / 1682 |
| d5 / Nc6 | Italian opening-book prior | +1021 | e4 / Bb5 / Bxc6+ | 1847 / 3148 |
| e5 / exd4 | Italian opening-book prior | +1247 | Qxd4 / Qf6 / Qxd8+ | 2081 / 4047 |
| e5 / e4 | community refutation candidate | +1382 | Qd2 / Qg5 / Qxd8+ | 1790 / 2953 |
| d5 / c6 | community drawing hypothesis | +1534 | Qd2 / Qa5 / Qxd8+ | 1837 / 2967 |
| c5 / d5 | Italian opening-book prior | +1724 | dxc5 / Qxd5 / Qxd8+ | 1992 / 3464 |

## Interpretation limit

The comparison extends the two-series baseline with a searched White tactical series for each named reply. It still cannot establish the best Black reply: unlisted Black series remain possible, the White frontier is selective, and leaf values are heuristic.
