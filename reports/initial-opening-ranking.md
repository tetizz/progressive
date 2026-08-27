# Initial-move ranking — Scottish Progressive Chess

Generated: `2026-08-27T09:27:08+00:00`<br>
Engine: `spc-0.9.0`<br>
Source fingerprint: `dd1621ac34bd4acb`<br>
Rules profile: `scottish-modern-common-v1`<br>
Search: `exhaustive`, 1 Black/continuation series ply after the fixed White move<br>
Total series horizon: `2`<br>
Nodes: `5362`<br>
Summed analysis time: `0.673s`

> This is a depth-limited engine ranking, not a claim that the top move is objectively best. Every leaf that is not a proven terminal uses the current progressive-specific heuristic.

| Rank | White move | Score | Classification | Best Black series | Unique / raw Black series | Confidence |
|---:|:---|---:|:---|:---|---:|:---|
| 1 | `e3` (`e2e3`) | -72 | Unclear | e5 / Ke7 | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 2 | `e4` (`e2e4`) | -106 | Slight Disadvantage | d5 / dxe4 | 269 / 446 | exhaustive at stated series depth; heuristic leaf evaluation |
| 3 | `Nf3` (`g1f3`) | -112 | Slight Disadvantage | d5 / Kd7 | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 4 | `Nc3` (`b1c3`) | -116 | Slight Disadvantage | f5 / Kf7 | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 5 | `b3` (`b2b3`) | -118 | Slight Disadvantage | d5 / Kd7 | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 6 | `c3` (`c2c3`) | -118 | Slight Disadvantage | e5 / Ke7 | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 7 | `g3` (`g2g3`) | -118 | Slight Disadvantage | e5 / Ke7 | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 8 | `h4` (`h2h4`) | -118 | Slight Disadvantage | d5 / Kd7 | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 9 | `a4` (`a2a4`) | -120 | Slight Disadvantage | e5 / Qh4 | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 10 | `Nh3` (`g1h3`) | -120 | Slight Disadvantage | d5 / Kd7 | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 11 | `h3` (`h2h3`) | -122 | Slight Disadvantage | d5 / Kd7 | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 12 | `a3` (`a2a3`) | -124 | Slight Disadvantage | e5 / Qh4 | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 13 | `c4` (`c2c4`) | -150 | Slight Disadvantage | d5 / dxc4 | 269 / 446 | exhaustive at stated series depth; heuristic leaf evaluation |
| 14 | `g4` (`g2g4`) | -156 | Slight Disadvantage | d5 / Bxg4 | 267 / 444 | exhaustive at stated series depth; heuristic leaf evaluation |
| 15 | `b4` (`b2b4`) | -192 | Slight Disadvantage | e5 / Bxb4 | 267 / 444 | exhaustive at stated series depth; heuristic leaf evaluation |
| 16 | `Na3` (`b1a3`) | -202 | Slight Disadvantage | e5 / Qh4 | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 17 | `d3` (`d2d3`) | -406 | Disadvantage | e5 / Bb4+ | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 18 | `d4` (`d2d4`) | -412 | Disadvantage | e5 / Bb4+ | 269 / 446 | exhaustive at stated series depth; heuristic leaf evaluation |
| 19 | `f3` (`f2f3`) | -438 | Disadvantage | e5 / Qh4+ | 268 / 445 | exhaustive at stated series depth; heuristic leaf evaluation |
| 20 | `f4` (`f2f4`) | -438 | Disadvantage | e5 / Qh4+ | 269 / 446 | exhaustive at stated series depth; heuristic leaf evaluation |

## Principal variations

1. **e3** — S1 White[1]: e3 | S2 Black[2]: e5 / Ke7
2. **e4** — S1 White[1]: e4 | S2 Black[2]: d5 / dxe4
3. **Nf3** — S1 White[1]: Nf3 | S2 Black[2]: d5 / Kd7
4. **Nc3** — S1 White[1]: Nc3 | S2 Black[2]: f5 / Kf7
5. **b3** — S1 White[1]: b3 | S2 Black[2]: d5 / Kd7
6. **c3** — S1 White[1]: c3 | S2 Black[2]: e5 / Ke7
7. **g3** — S1 White[1]: g3 | S2 Black[2]: e5 / Ke7
8. **h4** — S1 White[1]: h4 | S2 Black[2]: d5 / Kd7
9. **a4** — S1 White[1]: a4 | S2 Black[2]: e5 / Qh4
10. **Nh3** — S1 White[1]: Nh3 | S2 Black[2]: d5 / Kd7
11. **h3** — S1 White[1]: h3 | S2 Black[2]: d5 / Kd7
12. **a3** — S1 White[1]: a3 | S2 Black[2]: e5 / Qh4
13. **c4** — S1 White[1]: c4 | S2 Black[2]: d5 / dxc4
14. **g4** — S1 White[1]: g4 | S2 Black[2]: d5 / Bxg4
15. **b4** — S1 White[1]: b4 | S2 Black[2]: e5 / Bxb4
16. **Na3** — S1 White[1]: Na3 | S2 Black[2]: e5 / Qh4
17. **d3** — S1 White[1]: d3 | S2 Black[2]: e5 / Bb4+
18. **d4** — S1 White[1]: d4 | S2 Black[2]: e5 / Bb4+
19. **f3** — S1 White[1]: f3 | S2 Black[2]: e5 / Qh4+
20. **f4** — S1 White[1]: f4 | S2 Black[2]: e5 / Qh4+

## What this run establishes

- All 20 orthodox-legal first moves were considered independently.
- Every requested Black reply search completed to the stated series depth.
- Every complete Black two-move series (including legal early checks) was generated at this exact-width horizon.
- Different intra-series move orders with identical full progressive state were merged and counted.
- Scores, nodes, limits, source/rules versions, and exact series PVs are retained for reproduction.

## What it does not establish

- A heuristic score after Black's reply is not a forced-win/loss proof.
- The ranking can change when White's three-move responses and later series are searched.
- The current reach probe is bounded and evaluation weights have not yet been calibrated against a large expert game set.
