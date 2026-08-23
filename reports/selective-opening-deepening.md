# Selective opening deepening

Generated: `2026-08-23T15:27:40+00:00`<br>
Engine: `spc-0.9.0`<br>
Source fingerprint: `5c6ef2f9a9f41877`<br>
Rules: `scottish-modern-common-v1`<br>
Total series horizon: `3`<br>
Maximum retained series per node: `16`
Search completion: `complete`

> Selective results are hypotheses, not proof and not a complete ranking. A pruned reply may change the result.

The cap bounds intermediate series frontiers as well as complete candidates. This prevents high-budget series from materializing an unbounded tree, and it makes the result explicitly selective whenever pruning occurs.

| Move | Score | Classification | Best tested Black series | PV | Depth | Status | Generated unique / raw | Time |
|:---|---:|:---|:---|:---|---:|:---|---:|---:|
| `e4` | +530 | Advantage | d5 / dxe4 | S1 White[1]: e4 \| S2 Black[2]: d5 / dxe4 \| S3 White[3]: Qe2 / Qxe4 / Bb5+ | 2/2 | complete | 9236 / 15555 | 0.04s |
| `d4` | +452 | Advantage | e6 / Ke7 | S1 White[1]: d4 \| S2 Black[2]: e6 / Ke7 \| S3 White[3]: e4 / Qh5 / Qc5+ | 2/2 | complete | 8845 / 13393 | 0.03s |

## Interpretation

These runs include a searched White three-move response, so they repair the most obvious horizon weakness in the two-series baseline. They remain selective: neither a positive score nor a negative score is a forced result.
