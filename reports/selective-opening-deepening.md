# Selective opening deepening

Generated: `2026-08-20T05:53:33+00:00`<br>
Engine: `spc-0.4.0`<br>
Source fingerprint: `7b92ea2a6678d6be`<br>
Rules: `scottish-modern-common-v1`<br>
Total series horizon: `3`<br>
Maximum retained series per node: `16`
Search completion: `complete`

> Selective results are hypotheses, not proof and not a complete ranking. A pruned reply may change the result.

The cap bounds intermediate series frontiers as well as complete candidates. This prevents high-budget series from materializing an unbounded tree, and it makes the result explicitly selective whenever pruning occurs.

| Move | Score | Classification | Best tested Black series | PV | Depth | Status | Generated unique / raw | Time |
|:---|---:|:---|:---|:---|---:|:---|---:|---:|
| `e4` | +848 | Likely Win | f5 / Kf7 | S1 White[1]: e4 \| S2 Black[2]: f5 / Kf7 \| S3 White[3]: Nc3 / Qg4 / Qxf5+ | 2/2 | complete | 9489 / 15920 | 5.41s |
| `d4` | +752 | Likely Win | d6 / Kd7 | S1 White[1]: d4 \| S2 Black[2]: d6 / Kd7 \| S3 White[3]: e4 / Qh5 / Qf5+ | 2/2 | complete | 9100 / 13758 | 5.82s |

## Interpretation

These runs include a searched White three-move response, so they repair the most obvious horizon weakness in the two-series baseline. They remain selective: neither a positive score nor a negative score is a forced result.
