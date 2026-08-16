# Selective opening deepening

Generated: `2026-08-16T21:45:41+00:00`<br>
Engine: `spc-0.2.0-m1`<br>
Source fingerprint: `9d2e0e58eeff4cae`<br>
Rules: `scottish-modern-common-v1`<br>
Total series horizon: `3`<br>
Maximum retained series per node: `16`
Search completion: `complete`

> Selective results are hypotheses, not proof and not a complete ranking. A pruned reply may change the result.

The cap is applied only after every legal series at that node has been generated and full-state transpositions have been merged.

| Move | Score | Classification | Best tested Black series | PV | Depth | Status | Generated unique / raw | Time |
|:---|---:|:---|:---|:---|---:|:---|---:|---:|
| `e4` | +1137 | Likely Win | e6 / Ke7 | S1 White[1]: e4 \| S2 Black[2]: e6 / Ke7 \| S3 White[3]: Qg4 / Qxg7 / Qxf8+ | 2/2 | complete | 97971 / 417566 | 43.36s |
| `d4` | +902 | Likely Win | e6 / Bb4+ | S1 White[1]: d4 \| S2 Black[2]: e6 / Bb4+ \| S3 White[3]: Qd2 / Qxb4 / Qe7+ | 2/2 | complete | 78552 / 308973 | 30.85s |

## Interpretation

These runs include a searched White three-move response, so they repair the most obvious horizon weakness in the two-series baseline. They remain selective: neither a positive score nor a negative score is a forced result.
