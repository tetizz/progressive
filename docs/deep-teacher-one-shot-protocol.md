# Deep-teacher one-shot artifact boundary

The deep-teacher fitter accepts an isolated train artifact only. It has no
sealed-holdout argument and rejects a combined corpus or any label whose
`split` is not `train`. The evaluator accepts a separate
`sealed_holdout` artifact and reserves its cycle/seed claim file with an
exclusive create before it resolves, stats, opens, reads, hashes, or parses that
artifact. The marker lives in Git's shared common-directory claim registry and
is derived from the preregistered cycle schema and holdout seed. It therefore
survives copied fit receipts, refits, fresh output directories, and linked Git
worktrees.
A parse, binding, leakage, or scoring failure after that point consumes the
cycle/seed holdout and must not be retried.

Every command requires a frozen `spc-cycle<N>-one-shot-protocol-v1` manifest.
The manifest must retain the pre-generation status and unused preflight flags,
distinct train and one-shot holdout seeds, exact trajectory and teacher-tier
settings, source/runtime identities, ordered profile paths/IDs/hashes, every
frozen generator/merger/augmenter/fitter hash and evaluator contract hash,
exact split counts, and the complete candidate/ablation/post-holdout gates.
It also requires:

```json
{
  "integrity": {
    "teacher_semantic_hash_contract": "canonical-json-sha256-without-runtime-created-or-raw-artifact-hashes-v1"
  }
}
```

Create isolated artifacts from a leakage-audited combined corpus, then fit and
open the holdout once:

```powershell
python scripts/fit_deep_teacher_value.py split-artifacts `
  --preregistration benchmarks/protocols/cycle4-one-shot.json `
  mixed-teacher.json isolated

python scripts/fit_deep_teacher_value.py fit `
  --preregistration benchmarks/protocols/cycle4-one-shot.json `
  isolated/train-teacher-artifact.json leader.json fit-output

python scripts/fit_deep_teacher_value.py evaluate-holdout `
  --preregistration benchmarks/protocols/cycle4-one-shot.json `
  isolated/sealed-holdout-teacher-artifact.json leader.json `
  fit-output/deep-teacher-fit-receipt.json holdout-output
```

## Split artifact contract

Both files retain the teacher corpus schema and have an `artifact` object with
schema `spc-deep-teacher-split-artifact-v1`. Required fields are:

- `split`: `train` or `sealed_holdout`;
- exact preregistration schema and SHA-256 binding;
- source corpus ID plus separate semantic and raw artifact SHA-256 values;
- the artifact's root/option semantic-key commitment;
- the counterpart semantic-key commitment;
- the artifact's canonical full label-payload commitment (including scores,
  proofs, PVs, features, and options) and its counterpart commitment;
- a shared exact cross-split audit digest; and
- a shared dataset-pairing digest over the preregistration, both semantic-key
  commitments, both full label-payload commitments, and the cross-split audit.

The artifact contains labels from only its declared split. Its quality counts
name only that split, `split_artifact_isolated` is true, and raw cross-split key
lists are absent. Runtime and creation timing are excluded from semantic
teacher identity. Raw bytes are still hashed and recorded as provenance, but
they do not change model identity. Every JSON corpus is read once: parsing and
the raw SHA-256 use the same byte snapshot. Before the claim is created, the
evaluator reopens the unsealed train artifact, recomputes its semantic and raw
commitments and split evidence, and derives leakage keys from those labels
instead of trusting mutable receipt lists.

Consumed cycle-3 labels may be reused only as cycle-4 development data, never
as a cycle-4 holdout. A bounded relabeling tool must create a new train artifact
with every label explicitly marked `train`, preserve each label's original
split in separate development provenance, and give the result a new semantic
identity. The completely fresh holdout generator must exclude every
development root and option-final semantic key. Once generated, the two files
must receive matching counterpart commitments, cross-split audit digest, and
dataset-pairing digest under the new preregistration. The evaluator does not
require both files to originate from the same combined corpus; it requires the
explicit pair binding and recomputes all four exact intersections.

The exact relabeled development record is an ordinary isolated train artifact
plus this per-label object, which is covered by the full payload commitment:

```json
{
  "split": "train",
  "development_provenance": {
    "schema": "spc-consumed-holdout-development-v1",
    "original_split": "holdout",
    "original_artifact_semantic_sha256": "<64 lowercase hex>",
    "original_artifact_raw_sha256": "<64 lowercase hex>"
  }
}
```

When the development artifact already exists before preregistration, the train
artifact's optional `trajectory_corpora.train.artifact_source` entry freezes its
path, corpus ID, and semantic and raw SHA-256. The fitter reopens that unsealed
source once, hashes and parses the same bytes, and requires every relabeled
development payload to match its original label exactly apart from `split` and
the explicit provenance object. A source created only after
preregistration is instead bound by the preregistered generation contract and
recorded as independent artifact provenance. Train and holdout source identities
are intentionally allowed to differ; the label-payload commitments, counterpart
commitments, pair digest, and exact four-way key audit are the cross-source
integrity boundary.
