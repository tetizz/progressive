# Published and engine reply comparison

Generated: `2026-08-16T21:45:25+00:00`<br>
Engine: `spc-0.2.0-m1`<br>
Source fingerprint: `9d2e0e58eeff4cae`<br>
Rules: `scottish-modern-common-v1`
Horizon: White 1 move, fixed Black candidate 2-move series, then a screened White 3-move response.

> Only named historical/engine candidate Black replies are compared. This is not exhaustive over all Black series and is not a proof.

## 1.e4

Lower scores are better for Black. Every legal White series is generated; the top 64 deterministic screening finalists receive the full evaluation.

| Black series | Source | Score after White's best response | Best White series | Unique / raw White series |
|:---|:---|---:|:---|---:|
| e6 / Ke7 | selective engine candidate | +1137 | Qg4 / Qxg7 / Qxf8+ | 6631 / 29627 |
| e5 / Qh4 | two-series engine baseline | +1560 | Qg4 / Qxh4 / Qe7+ | 4931 / 20978 |
| e5 / f6 | community drawing hypothesis | +1602 | Qf3 / Qxf6 / Qxd8+ | 5968 / 24961 |
| e5 / Qe7 | Italian opening-book prior | +1646 | Qh5 / Qxe5 / Qxe7+ | 6206 / 27338 |
| e5 / Nh6 | Italian opening-book prior | +1733 | Qg4 / Qxg7 / Qxf8+ | 6182 / 27174 |
| d5 / dxe4 | Italian opening-book prior | +1777 | Qg4 / Qxc8 / Qxd8+ | 6168 / 26169 |
| e6 / Nf6 | Italian opening-book prior | +1863 | Qf3 / Qxf6 / Qxd8+ | 6700 / 30042 |
| d5 / e5 | Italian opening-book prior | +1869 | Qg4 / Qxc8 / Qxd8+ | 6461 / 27847 |
| d5 / d4 | Italian opening-book prior | +1899 | Qg4 / Qxc8 / Qxd8+ | 5747 / 25027 |
| d5 / Nc6 | Italian opening-book prior | +1923 | Qg4 / Qxc8 / Qxd8+ | 7220 / 32612 |

## 1.d4

Lower scores are better for Black. Every legal White series is generated; the top 64 deterministic screening finalists receive the full evaluation.

| Black series | Source | Score after White's best response | Best White series | Unique / raw White series |
|:---|:---|---:|:---|---:|
| e6 / Bb4+ | selective engine candidate | +902 | Qd2 / Qxb4 / Qe7+ | 1480 / 2725 |
| e5 / Bb4+ | two-series engine baseline | +908 | Qd2 / Qxb4 / Qf8+ | 1585 / 2940 |
| d5 / Nc6 | Italian opening-book prior | +1021 | e4 / Bb5 / Bxc6+ | 5524 / 23106 |
| d5 / h5 | Italian opening-book prior | +1036 | e4 / Qxh5 / Qxh8 | 5516 / 22935 |
| e5 / exd4 | Italian opening-book prior | +1247 | Qxd4 / Qf6 / Qxd8+ | 6596 / 26806 |
| d6 / Nf6 | Italian opening-book prior | +1338 | Bh6 / Bxg7 / Bxh8 | 5871 / 25005 |
| f5 / Nf6 | Italian opening-book prior | +1350 | Bh6 / Bxg7 / Bxh8 | 5917 / 25175 |
| e5 / e4 | community refutation candidate | +1382 | Qd2 / Qg5 / Qxd8+ | 5120 / 22048 |
| c5 / cxd4 | Italian opening-book prior | +1391 | Qxd4 / Qb6 / Qxd8+ | 6599 / 27045 |
| d5 / c6 | community drawing hypothesis | +1534 | Qd2 / Qa5 / Qxd8+ | 5550 / 23186 |
| c5 / d5 | Italian opening-book prior | +1724 | dxc5 / Qxd5 / Qxd8+ | 6076 / 25570 |

## Interpretation limit

The comparison is stronger than the two-series baseline because it generates White's entire immediate tactical series set for each named reply. It still cannot establish the best Black reply: unlisted Black series remain possible, the White finalist screen is selective, and leaf values are heuristic.
