# Deep-teacher one-shot artifact boundary

The deep-teacher fitter accepts an isolated train artifact only. It has no
sealed-holdout argument and rejects a combined corpus or any label whose
`split` is not `train`. The evaluator accepts a separate
`sealed_holdout` artifact and reserves its cycle/seed claim file with an
exclusive create before it resolves, stats, opens, reads, hashes, or parses that
artifact. The marker lives in Git's shared common-directory claim registry and
is derived from the holdout seed in a schema-independent namespace. It therefore
survives copied fit receipts, refits, fresh output directories, and linked Git
worktrees.
A parse, binding, leakage, or scoring failure after that point consumes the
cycle/seed holdout and must not be retried.

Artifact preparation has its own repository-wide cycle/seed claim. Both
`split-artifacts` and full `pair-artifacts` validation durably create it before
resolving, stating, opening, reading, hashing, or parsing the sealed source. A
post-claim crash or validation failure permanently binds the operation, source
path, and output path across linked worktrees. An exact retry may resume only
after the source byte binding was atomically published. A claim-only crash is
conservatively burned because the source may already have been opened; a
different operation, path, or source byte snapshot is always forbidden.
For distinct-source pairing, the claim also freezes the lexical train path and
the single atomic source binding freezes both the train and sealed byte
snapshots before any validation. A retry cannot substitute a different train
artifact to probe the same sealed holdout.
`pair-artifacts --metadata-only` is a manifest-and-lexical-path preview. It
opens neither the train path nor the sealed source, because either spelling
could alias the sealed bytes; it also creates no claim. A full
`pair-artifacts --dry-run` is rejected: a command that
opens the sealed source is never described as non-consuming.

The Git-common registry also owns one process-lifetime protocol stage lock.
Preregistration, development import, trajectory generation, teacher building,
semantic augmentation, and tier merge hold it in shared mode for their entire
command and final closure; independent producers may therefore run in parallel.
Pairing and splitting take it exclusively before rechecking state and publishing
the terminal preparation claim. Once preparation has begun, every upstream
producer/import command fails reservation-first, before it opens manifest,
profile, runtime, root, or artifact content. The OS releases the lock on process
death, so it is not a stale filesystem lease.

Every cycle-4 data-consuming command requires the exact frozen
`spc-cycle4-one-shot-protocol-v1` manifest. Future cycle schemas require new
validated contracts; renaming this manifest to another cycle is rejected.
`preregister` exclusively creates
the cycle-4 manifest before generation. It fixes 262,144 train attempts,
131,072 holdout attempts, 3,072 quiet-D2 roots (2,304/768), and 1,024
tactical-D3 roots (768/256). The trajectory and teacher CLIs still expose their
ordinary knobs, but protocol mode compares every semantic and operational value
to the manifest before creating a durable start receipt or consuming data. A
cycle-3-sized fallback therefore fails before expensive work.
The manifest must retain the pre-generation status and unused preflight flags,
distinct train, one-shot holdout, teacher-selection, and match seeds, exact
semantic trajectory and teacher-tier
settings, source/runtime identities, ordered profile paths/IDs/hashes, every
frozen generator/merger/augmenter/fitter hash and evaluator contract hash,
exact split counts, and the complete candidate/ablation/post-holdout gates.
Trajectory `first_attempt`, shard size 10,000, batch size 256, eight workers,
full payload verification, and unique-state counting are frozen and bound into
start and completion receipts. Teacher workers are also fixed at eight. Exact
trajectory-generation starts and completed receipts permit safe crash-resume.
Teacher, semantic-augmentation, and merge starts require their exact durable
source binding before retry; a start-only retry is burned because an input may
already have been consumed. An unbound preexisting shard or a changed start,
contract, operation, output, or receipt fails closed.
Completion JSON is published atomically and without overwrite.
Fit publishes its two models, distilled profile, and receipt in that fixed
order. An exact same-path retry may resume only a byte-identical contiguous
prefix of those artifacts; a gap, extra file, or changed payload fails closed.

Before the manifest is published, `preregister` durably and exclusively binds
both the cycle and holdout seed to the exact canonical manifest path and raw
SHA-256 in Git's shared common directory. An identical retry may finish a
reservation-without-manifest crash; a different path, manifest, or seed loses
the reservation. Every later protocol command verifies both reservation
records before accepting the manifest.

The manifest also requires:

```json
{
  "integrity": {
    "teacher_semantic_hash_contract": "canonical-json-sha256-without-runtime-created-or-raw-artifact-hashes-v1"
  }
}
```

Create the larger fresh manifest first. Paths that name data artifacts must be
absolute and canonical; aliases are rejected:

```powershell
python scripts/fit_deep_teacher_value.py preregister `
  --output C:\spc-c4\cycle4-one-shot.json `
  --base-deployed-commit <40-lowercase-hex> `
  --integrated-engine-source-commit <40-lowercase-hex> `
  --train-seed <fresh-positive-int> `
  --holdout-seed <fresh-positive-int> `
  --selection-seed <fresh-positive-int> `
  --match-seed <fresh-positive-int>
```

The current cycle-4 lane is fully fresh. The exact consumed cycle-3 artifact is
not available, so do not use `import-development`. Generate both trajectory
stores with the same ordered four-profile schedule frozen by the manifest:

```powershell
$manifest = 'C:\spc-c4\cycle4-one-shot.json'
$profiles = @(
  'C:\path\to\repo\profiles\training\teacher-source-schedule\0-spc-68942034c41b4cc4.json',
  'C:\path\to\repo\profiles\training\teacher-source-schedule\1-spc-49aa573617f48331.json',
  'C:\path\to\repo\profiles\training\teacher-source-schedule\2-spc-b9e49ec3fadb9b00.json',
  'C:\path\to\repo\profiles\training\teacher-source-schedule\3-spc-71ba893f3efe214e.json'
)
$profileArgs = $profiles | ForEach-Object { @('--profile', $_) }

python scripts/generate_native_corpus.py C:\spc-c4\trajectory-train `
  --preregistration $manifest --protocol-split train `
  --attempts 262144 --first-attempt 0 --shard-size 10000 --batch-size 256 `
  --workers 8 --seed <train-seed> --max-attempt-series 64 --frontier 32 `
  --candidates 16 --max-positions-per-series 250000 `
  --max-positions-per-game 10000000 --uniform --ordered-pairs `
  @profileArgs --receipt C:\spc-c4\trajectory-train-receipt.json

python scripts/generate_native_corpus.py C:\spc-c4\trajectory-holdout `
  --preregistration $manifest --protocol-split sealed_holdout `
  --attempts 131072 --first-attempt 0 --shard-size 10000 --batch-size 256 `
  --workers 8 --seed <holdout-seed> --max-attempt-series 64 --frontier 32 `
  --candidates 16 --max-positions-per-series 250000 `
  --max-positions-per-game 10000000 --uniform --ordered-pairs `
  @profileArgs --receipt C:\spc-c4\trajectory-holdout-receipt.json
```

Then build and replay-augment each preregistered tier. The augmentation command
publishes its path/config start before it opens the tier or trajectory stores,
then binds the exact single-read input and store snapshots. The tactical tier
consumes the augmented quiet tier only to enforce the cross-tier semantic
exclusion:

```powershell
python scripts/build_native_teacher_corpus.py `
  C:\spc-c4\trajectory-train C:\spc-c4\trajectory-holdout `
  C:\spc-c4\quiet-d2.json --preregistration $manifest `
  --target-roots 3072 --train-roots 2304 --minimum-series 4 `
  --maximum-series 9 --depth 2 --branch-cap 32 --max-work 10000000 `
  --hard-negatives 4 --seed <selection-seed> --workers 8 `
  --train-attempts 262144 --holdout-attempts 131072 `
  --selection-mode quiet-nonterminal --skip-tactical-gate `
  --receipt-root C:\spc-c4\quiet-root-receipts

python scripts/augment_native_teacher_semantics.py `
  C:\spc-c4\quiet-d2.json `
  C:\spc-c4\trajectory-train C:\spc-c4\trajectory-holdout `
  C:\spc-c4\quiet-d2-augmented.json `
  C:\spc-c4\quiet-d2-augmentation-receipt.json `
  --preregistration $manifest --tier quiet_depth2

python scripts/build_native_teacher_corpus.py `
  C:\spc-c4\trajectory-train C:\spc-c4\trajectory-holdout `
  C:\spc-c4\tactical-d3.json --preregistration $manifest `
  --target-roots 1024 --train-roots 768 --minimum-series 4 `
  --maximum-series 9 --depth 3 --branch-cap 32 --max-work 10000000 `
  --hard-negatives 4 --seed <selection-seed> --workers 8 `
  --train-attempts 262144 --holdout-attempts 131072 `
  --selection-mode tactical-low-complexity `
  --cross-tier-artifact C:\spc-c4\quiet-d2-augmented.json `
  --receipt-root C:\spc-c4\tactical-root-receipts

python scripts/augment_native_teacher_semantics.py `
  C:\spc-c4\tactical-d3.json `
  C:\spc-c4\trajectory-train C:\spc-c4\trajectory-holdout `
  C:\spc-c4\tactical-d3-augmented.json `
  C:\spc-c4\tactical-d3-augmentation-receipt.json `
  --preregistration $manifest --tier tactical_depth3

python scripts/merge_native_teacher_tiers.py `
  C:\spc-c4\quiet-d2-augmented.json `
  C:\spc-c4\tactical-d3-augmented.json `
  C:\spc-c4\mixed-teacher.json --preregistration $manifest
```

The merge publishes a sibling completion receipt that binds the canonical
merged path, semantic identity, and exact raw SHA-256 to both replay receipts.
`split-artifacts` and `pair-artifacts` require that receipt and reject a copied
or rebound combined artifact.

Finally isolate the leakage-audited combined corpus, fit, and open the holdout
once:

```powershell
python scripts/fit_deep_teacher_value.py split-artifacts `
  --preregistration C:\spc-c4\cycle4-one-shot.json `
  C:\spc-c4\mixed-teacher.json C:\spc-c4\isolated

python scripts/fit_deep_teacher_value.py fit `
  --preregistration C:\spc-c4\cycle4-one-shot.json `
  C:\spc-c4\isolated\train-teacher-artifact.json `
  C:\path\to\repo\profiles\training\native-corpus-development-leader.json `
  C:\spc-c4\fit-output

python scripts/fit_deep_teacher_value.py evaluate-holdout `
  --preregistration C:\spc-c4\cycle4-one-shot.json `
  C:\spc-c4\isolated\sealed-holdout-teacher-artifact.json `
  C:\path\to\repo\profiles\training\native-corpus-development-leader.json `
  C:\spc-c4\fit-output\deep-teacher-fit-receipt.json `
  C:\spc-c4\holdout-output
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
the raw SHA-256 use the same byte snapshot. After the irreversible claim is
created, the evaluator reopens the unsealed train artifact, recomputes its
semantic and raw
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
    "original_artifact_raw_sha256": "<64 lowercase hex>",
    "consumption_evidence_raw_sha256": "<64 lowercase hex>"
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

The optional import lane is fail-closed and is not usable for the present run:
the exact 192-label raw artifact named by the committed cycle-3 result must
exist and match its 128/64 consumption evidence. An exploratory or partial
artifact, including an 89-label file, is rejected. If that exact evidence is
available in a future cycle, preregister it with
`--development-source <canonical-absolute-path>` and
`--development-consumption-evidence <canonical-absolute-path>`, run
`import-development`, then run `pair-artifacts` against a fresh combined C4
source. The pairer rejects equal corpus IDs, semantic identities, or raw hashes
and exclusively publishes mutually bound train/holdout files. A crash may leave
a partial directory as evidence. The exact same request may finish it only when
the source byte binding is already durable; a claim-only crash burns the seed.
Changing the operation, paths, or source bytes is forbidden for that seed.

The post-holdout match contract freezes a fresh deterministic suite seed, suite
algorithm, series range 3–6, frontier cap 32, 50 color-swapped pairs, 100 games,
D2/cap32 search, and one requested worker. It reuses the exact
`league.promotion_decision` rule: at least 45 pair wins, zero pair losses, pair
score strictly above 0.5, and zero technical/incomplete results. Promotion is
explicitly blocked until dedicated tactical/mate, symmetry,
Python/native/WASM parity, runtime-overhead, and fixed-match receipt verifiers
all exist and pass.
