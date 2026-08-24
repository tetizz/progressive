import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const modulePath = process.argv[2];
if (!modulePath) {
  throw new Error("usage: node verify_native_root_session_wasm.mjs <module.js> [native-source-identity] [profile-id]");
}
const modelNativeSourceIdentity = process.argv[3] ?? "d".repeat(64);
const modelProfileId = process.argv[4] ?? "spc-test-baseline";
const factory = (await import(pathToFileURL(modulePath).href)).default;
const wasm = await factory();

assert.equal(wasm._spc_root_session_abi_version(), 2);

function callBytes(fn, prefix, bytes) {
  const pointer = wasm._malloc(bytes.byteLength);
  assert.notEqual(pointer, 0);
  try {
    wasm.HEAPU8.set(bytes, pointer);
    const resultPointer = fn(...prefix, pointer, bytes.byteLength);
    assert.notEqual(resultPointer, 0);
    return JSON.parse(wasm.UTF8ToString(resultPointer));
  } finally {
    wasm._free(pointer);
  }
}

function call(fn, prefix, request) {
  return callBytes(fn, prefix, new TextEncoder().encode(JSON.stringify(request)));
}

const identity = Object.freeze({
  source_fingerprint: "0123456789abcdef",
  kernel_sha256: "a".repeat(64),
  module_js_sha256: "b".repeat(64),
  certificate_id: "root-session-test-certificate",
  runtime_variant: "single",
  thread_count: 1,
  engine_version: "test-engine-v1",
  ruleset_version: "progressive-v1",
  profile_id: modelProfileId,
});
const boundary = Object.freeze({
  fen: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  series: 1,
  quiet_series: 0,
  ep_targets: [],
  promoted_hex: "0000000000000000",
  chess960: false,
});
const neuralS2Boundary = Object.freeze({
  fen: "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
  series: 2,
  quiet_series: 0,
  ep_targets: [],
  promoted_hex: "0000000000000000",
  chess960: false,
});
const NEURAL_S2_ORDER_POLICY = "root-order-s3-neural-model1-blend75";
const config = Object.freeze({
  max_depth: 2,
  width: 4,
  max_work: 2_000_000,
  mate_score: 1_000_000,
  series_cache_capacity: 16_384,
  external_cache_weight: 0,
  worker_threads: 1,
  root_tactical_protection: false,
  root_contract_tt_capacity: 16_384,
  root_contract_eval_capacity: 16_384,
  weights: {
    material: 100,
    king_space: 100,
    series_reach: 100,
    promotion_corridors: 100,
    immediate_vulnerability: 100,
    useful_mobility: 100,
    boundary_check: 100,
  },
});
const neuralS2Config = Object.freeze({ ...config, width: 32 });
const deepTeacherValueModel = Object.freeze({
  schema: "spc-deep-teacher-match-overlay-v1",
  base_profile_id: identity.profile_id,
  variant_id: "spc-dtv-variant-root-wasm-test",
  model_id: "spc-dtv-root-wasm-test",
  model_sha256: "c".repeat(64),
  native_source_identity: modelNativeSourceIdentity,
  feature_count: 7,
  fixed_point_scale: 1_000_000_000,
  coefficients: [1_000_000_000, 0, 0, 0, 0, 0, 0],
  score_policy: "symmetric-half-away-from-zero-divide-by-1000000000-then-clamp-below-mate-v1",
  work_policy: "charge-reach-plus-direct-and-two-move-legal-variants-v1",
});
const deepTeacherConfig = Object.freeze({
  ...config,
  deep_teacher_value_model: deepTeacherValueModel,
});
const materialBoundary = Object.freeze({
  fen: "rnb1kbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  series: 1,
  quiet_series: 0,
  ep_targets: [],
  promoted_hex: "0000000000000000",
  chess960: false,
});

let createSequence = 0;
function createRequest(overrides = {}) {
  createSequence += 1;
  return {
    schema: "spc-root-session-create-v1",
    request_id: `request-${createSequence}`,
    iteration_id: `create-${createSequence}`,
    generation: 0,
    ...identity,
    boundary,
    config,
    ...overrides,
  };
}

function create(overrides = {}) {
  const reply = call(
    wasm._spc_root_session_create_json,
    [],
    createRequest(overrides),
  );
  assert.equal(reply.status, "ready", JSON.stringify(reply));
  assert.equal(reply.product_publishable, false);
  assert.equal(reply.safety_certified, false);
  assert.equal(reply.capabilities.reply_mate_safety, false);
  assert.equal(reply.capabilities.aspiration_windows, true);
  assert.equal(reply.capabilities.selected_owner_certification, true);
  assert.deepEqual(reply.config, overrides.config ?? config);
  return reply;
}

function operationBase({
  schema,
  iteration,
  generation,
  externalWork,
  nativeWorkBefore,
  credit,
  deadline,
}) {
  return {
    schema,
    request_id: "persistent-request",
    iteration_id: iteration,
    generation,
    ...identity,
    external_work: externalWork,
    native_work_before: nativeWorkBefore,
    call_work_credit: credit,
    deadline_monotonic_ms: deadline,
    remaining_time_ms: Math.max(1, Math.floor(deadline - performance.now())),
  };
}

function enumerate(
  sessionId,
  nativeBefore,
  deadline,
  preferred = [],
  credit = 500_000,
  iteration = "depth-1-enumerate",
) {
  return call(wasm._spc_root_session_enumerate_json, [sessionId], {
    ...operationBase({
      schema: "spc-root-session-enumerate-v1",
      iteration,
      generation: 1,
      externalWork: 0,
      nativeWorkBefore: nativeBefore,
      credit,
      deadline,
    }),
    preferred_series: preferred,
  });
}

function manifestOf(reply) {
  return {
    enumeration_identity: reply.enumeration_identity,
    root_white_to_move: reply.root_white_to_move,
    requested_width: reply.requested_width,
    retained_count: reply.retained_count,
    width_complete: reply.width_complete,
    preferred_series: reply.preferred_series,
    candidates: reply.candidates,
  };
}

function searchTask({
  candidate,
  manifest,
  nativeBefore,
  externalWork = 0,
  childDepth,
  deadline,
  credit = 500_000,
  purpose = "full",
  alpha = -2 * config.mate_score,
  beta = 2 * config.mate_score,
  ttPersistence = purpose === "scout" ? "rollback" : "commit",
  iteration = `depth-${childDepth + 1}`,
  task = `task-${childDepth + 1}`,
}) {
  return {
    ...operationBase({
      schema: "spc-root-candidate-task-v1",
      iteration,
      generation: childDepth + 1,
      externalWork,
      nativeWorkBefore: nativeBefore,
      credit,
      deadline,
    }),
    safety_revision: 0,
    incumbent_epoch: 0,
    task_id: task,
    enumeration_identity: manifest.enumeration_identity,
    candidate_identity: candidate.candidate_identity,
    order_index: candidate.order_index,
    order_key: candidate.order_key,
    purpose,
    mate_score: config.mate_score,
    child_depth: childDepth,
    alpha,
    beta,
    tt_persistence: ttPersistence,
    mover: manifest.root_white_to_move ? "white" : "black",
  };
}

const contract = JSON.parse(wasm.UTF8ToString(wasm._spc_root_session_contract_json()));
assert.equal(contract.abi_version, 2);
assert.equal(contract.product_publishable, false);
assert.equal(contract.reply_mate_safety, false);
assert.equal(contract.response_lifetime, "until-next-root-session-abi-call-on-this-worker");
assert.equal(contract.capabilities.aspiration_windows, true);
assert.equal(contract.capabilities.deep_teacher_value_model, true);
assert.equal(contract.hard_limits.deep_teacher_value_model.optional, true);
assert.deepEqual(
  contract.hard_limits.deep_teacher_value_model.feature_counts,
  [7, 14, 19, 38, 44, 47],
);
assert.equal(contract.hard_limits.minimum_aspiration_initial_delta, 2_048);
assert.equal(contract.hard_limits.maximum_aspiration_attempts, 4);
const aspirationInitialDelta = contract.hard_limits.minimum_aspiration_initial_delta;

// One session retains caches and work counters across D1 then D2 candidate calls.
const coordinator = create();
const commonDeadline = Math.floor(performance.now() + 60_000);
const enumeration = enumerate(coordinator.session_id, 0, commonDeadline);
assert.equal(enumeration.status, "complete", JSON.stringify(enumeration));
assert.equal(enumeration.candidates.length, 4);
assert.equal(enumeration.configured_max_depth, 2);
for (const [index, candidate] of enumeration.candidates.entries()) {
  assert.equal(candidate.order_index, index);
  assert.equal(candidate.order_key, candidate.root_series.machine_notation);
  assert.equal(candidate.root_series.child_boundary.series, 2);
  assert.equal(candidate.root_series.child_boundary.promoted_hex.length, 16);
}
const manifest = manifestOf(enumeration);
const candidate = manifest.candidates[0];
const depthOne = call(
  wasm._spc_root_session_search_json,
  [coordinator.session_id],
  searchTask({
    candidate,
    manifest,
    nativeBefore: enumeration.work.native_work_after,
    childDepth: 0,
    deadline: commonDeadline,
  }),
);
assert.equal(depthOne.status, "complete", JSON.stringify(depthOne));
assert.equal(depthOne.bound, "exact");
const depthTwo = call(
  wasm._spc_root_session_search_json,
  [coordinator.session_id],
  searchTask({
    candidate,
    manifest,
    nativeBefore: depthOne.work.native_work_after,
    childDepth: 1,
    deadline: commonDeadline,
    iteration: "depth-2",
    task: "task-depth-2",
  }),
);
assert.equal(depthTwo.status, "complete", JSON.stringify(depthTwo));
assert.equal(depthTwo.bound, "exact");
assert.ok(depthTwo.work.native_work_after >= depthOne.work.native_work_after);
assert.ok(depthTwo.work.call_native_work <= depthTwo.work.call_work_credit);
assert.equal(
  depthTwo.work.total_accounted_work,
  depthTwo.work.external_work + depthTwo.work.native_work_after,
);
const aspiration = call(
  wasm._spc_root_session_search_json,
  [coordinator.session_id],
  searchTask({
    candidate,
    manifest,
    nativeBefore: depthTwo.work.native_work_after,
    childDepth: 1,
    deadline: commonDeadline,
    purpose: "aspiration",
    alpha: depthTwo.score - aspirationInitialDelta,
    beta: depthTwo.score + aspirationInitialDelta,
    iteration: "depth-2-aspiration",
    task: "task-depth-2-aspiration",
  }),
);
assert.equal(aspiration.status, "complete", JSON.stringify(aspiration));
assert.equal(aspiration.purpose, "aspiration");
assert.equal(aspiration.tt_persistence, "commit");
assert.equal(aspiration.bound, "exact");

const scout = call(
  wasm._spc_root_session_search_json,
  [coordinator.session_id],
  searchTask({
    candidate,
    manifest,
    nativeBefore: aspiration.work.native_work_after,
    childDepth: 1,
    deadline: commonDeadline,
    purpose: "scout",
    alpha: depthTwo.score - 1,
    beta: depthTwo.score,
    iteration: "depth-2-scout",
    task: "task-depth-2-scout",
  }),
);
assert.equal(scout.status, "complete", JSON.stringify(scout));
assert.equal(scout.purpose, "scout");
assert.equal(scout.tt_persistence, "rollback");
assert.equal(scout.beta, scout.alpha + 1);

const aspirationFullWindowRejected = call(
  wasm._spc_root_session_search_json,
  [coordinator.session_id],
  searchTask({
    candidate,
    manifest,
    nativeBefore: scout.work.native_work_after,
    childDepth: 1,
    deadline: commonDeadline,
    purpose: "aspiration",
    iteration: "depth-2-aspiration-full-rejected",
    task: "task-depth-2-aspiration-full-rejected",
  }),
);
assert.equal(aspirationFullWindowRejected.status, "unsupported");
assert.equal(aspirationFullWindowRejected.error_code, "candidate-task-invalid");

const narrowFullRejected = call(
  wasm._spc_root_session_search_json,
  [coordinator.session_id],
  searchTask({
    candidate,
    manifest,
    nativeBefore: scout.work.native_work_after,
    childDepth: 1,
    deadline: commonDeadline,
    alpha: depthTwo.score - aspirationInitialDelta,
    beta: depthTwo.score + aspirationInitialDelta,
    iteration: "depth-2-narrow-full-rejected",
    task: "task-depth-2-narrow-full-rejected",
  }),
);
assert.equal(narrowFullRejected.status, "unsupported");
assert.equal(narrowFullRejected.error_code, "candidate-task-invalid");

const aspirationRollbackRejected = call(
  wasm._spc_root_session_search_json,
  [coordinator.session_id],
  searchTask({
    candidate,
    manifest,
    nativeBefore: scout.work.native_work_after,
    childDepth: 1,
    deadline: commonDeadline,
    purpose: "aspiration",
    alpha: depthTwo.score - aspirationInitialDelta,
    beta: depthTwo.score + aspirationInitialDelta,
    ttPersistence: "rollback",
    iteration: "depth-2-aspiration-rollback-rejected",
    task: "task-depth-2-aspiration-rollback-rejected",
  }),
);
assert.equal(aspirationRollbackRejected.status, "unsupported");
assert.equal(aspirationRollbackRejected.error_code, "candidate-task-invalid");
assert.equal(wasm._spc_root_session_destroy(coordinator.session_id), 1);
assert.equal(wasm._spc_root_session_destroy(coordinator.session_id), 0);

// The value model is an optional config extension. A plain create remains
// byte-for-byte shape compatible, while a supplied model is profile-bound,
// echoed exactly, included in enumeration identity, and charged at each leaf.
const wrongProfileModel = {
  ...deepTeacherValueModel,
  base_profile_id: "spc-wrong-profile",
};
const wrongProfile = call(
  wasm._spc_root_session_create_json,
  [],
  createRequest({
    boundary: materialBoundary,
    config: { ...config, deep_teacher_value_model: wrongProfileModel },
  }),
);
assert.equal(wrongProfile.status, "unsupported");
assert.equal(wrongProfile.error_code, "value-model-invalid");

const materialBaselineSession = create({ boundary: materialBoundary });
const materialDeadline = Math.floor(performance.now() + 60_000);
const materialBaselineEnumeration = enumerate(
  materialBaselineSession.session_id,
  0,
  materialDeadline,
);
assert.equal(materialBaselineEnumeration.status, "complete");
const materialBaselineManifest = manifestOf(materialBaselineEnumeration);
const materialBaseline = call(
  wasm._spc_root_session_search_json,
  [materialBaselineSession.session_id],
  searchTask({
    candidate: materialBaselineManifest.candidates[0],
    manifest: materialBaselineManifest,
    nativeBefore: materialBaselineEnumeration.work.native_work_after,
    childDepth: 0,
    deadline: materialDeadline,
    iteration: "material-baseline",
    task: "material-baseline",
  }),
);
assert.equal(materialBaseline.status, "complete", JSON.stringify(materialBaseline));
assert.equal(materialBaseline.work.call_stats.overlay_evaluations, 0);
assert.equal(wasm._spc_root_session_destroy(materialBaselineSession.session_id), 1);

const materialModelSession = create({
  boundary: materialBoundary,
  config: deepTeacherConfig,
});
assert.deepEqual(materialModelSession.config, deepTeacherConfig);
const materialModelEnumeration = enumerate(
  materialModelSession.session_id,
  0,
  materialDeadline,
);
assert.equal(materialModelEnumeration.status, "complete");
assert.notEqual(
  materialModelEnumeration.enumeration_identity,
  materialBaselineEnumeration.enumeration_identity,
);
const materialModelManifest = manifestOf(materialModelEnumeration);
assert.deepEqual(
  materialModelManifest.candidates.map((item) => item.candidate_identity),
  materialBaselineManifest.candidates.map((item) => item.candidate_identity),
);
const materialModeled = call(
  wasm._spc_root_session_search_json,
  [materialModelSession.session_id],
  searchTask({
    candidate: materialModelManifest.candidates[0],
    manifest: materialModelManifest,
    nativeBefore: materialModelEnumeration.work.native_work_after,
    childDepth: 0,
    deadline: materialDeadline,
    iteration: "material-modeled",
    task: "material-modeled",
  }),
);
assert.equal(materialModeled.status, "complete", JSON.stringify(materialModeled));
assert.equal(materialModeled.score, 975);
assert.equal(materialModeled.work.call_stats.overlay_evaluations, 1);
assert.ok(materialModeled.work.call_stats.overlay_reach_positions > 0);
assert.equal(materialModeled.work.call_stats.overlay_direct_move_variants, 0);
assert.equal(materialModeled.work.call_stats.overlay_two_move_variants, 0);
assert.equal(
  materialModeled.work.call_native_work,
  materialBaseline.work.call_native_work
    + materialModeled.work.call_stats.overlay_reach_positions,
);
assert.equal(wasm._spc_root_session_destroy(materialModelSession.session_id), 1);

// Production neural ordering is scoped to Black's Series 2 after a real
// completed White opening series. Freeze its full root manifest and one
// persistent D1/D2 lane for the native/Python differential gate.
const neuralS2Coordinator = create({
  boundary: neuralS2Boundary,
  config: neuralS2Config,
});
const neuralS2Deadline = Math.floor(performance.now() + 60_000);
const neuralS2Enumeration = enumerate(
  neuralS2Coordinator.session_id,
  0,
  neuralS2Deadline,
  [],
  500_000,
  "neural-s2-enumerate",
);
assert.equal(
  neuralS2Enumeration.status,
  "complete",
  JSON.stringify(neuralS2Enumeration),
);
assert.equal(neuralS2Enumeration.requested_width, 32);
assert.equal(neuralS2Enumeration.candidates.length, 32);
assert.ok(
  neuralS2Enumeration.enumeration_identity.includes(NEURAL_S2_ORDER_POLICY),
  "Series-2 enumeration did not identify the promoted neural ordering policy",
);
const neuralS2Manifest = manifestOf(neuralS2Enumeration);
for (const [index, retained] of neuralS2Manifest.candidates.entries()) {
  assert.equal(retained.order_index, index);
  assert.equal(retained.order_key, retained.root_series.machine_notation);
  assert.equal(retained.root_series.child_boundary.series, 3);
}
const neuralS2Candidate = neuralS2Manifest.candidates[0];
const neuralS2DepthOne = call(
  wasm._spc_root_session_search_json,
  [neuralS2Coordinator.session_id],
  searchTask({
    candidate: neuralS2Candidate,
    manifest: neuralS2Manifest,
    nativeBefore: neuralS2Enumeration.work.native_work_after,
    childDepth: 0,
    deadline: neuralS2Deadline,
    iteration: "neural-s2-depth-1",
    task: "neural-s2-depth-1",
  }),
);
assert.equal(neuralS2DepthOne.status, "complete", JSON.stringify(neuralS2DepthOne));
assert.equal(neuralS2DepthOne.bound, "exact");
const neuralS2DepthTwo = call(
  wasm._spc_root_session_search_json,
  [neuralS2Coordinator.session_id],
  searchTask({
    candidate: neuralS2Candidate,
    manifest: neuralS2Manifest,
    nativeBefore: neuralS2DepthOne.work.native_work_after,
    childDepth: 1,
    deadline: neuralS2Deadline,
    iteration: "neural-s2-depth-2",
    task: "neural-s2-depth-2",
  }),
);
assert.equal(neuralS2DepthTwo.status, "complete", JSON.stringify(neuralS2DepthTwo));
assert.equal(neuralS2DepthTwo.bound, "exact");
assert.equal(
  neuralS2DepthTwo.work.native_work_before,
  neuralS2DepthOne.work.native_work_after,
);
assert.ok(
  neuralS2DepthTwo.work.call_native_work <= neuralS2DepthTwo.work.call_work_credit,
);
assert.equal(wasm._spc_root_session_destroy(neuralS2Coordinator.session_id), 1);

function aspirationSemantic(result) {
  return {
    score: result.score,
    proof_bounds: result.proof_bounds,
    root_series: result.root_series,
    child_pv: result.child_pv,
  };
}

function assertAspirationWorkReceipt(result, expectedBefore) {
  const work = result.work;
  assert.equal(work.native_work_before, expectedBefore);
  assert.equal(work.call_work_credit, 500_000);
  assert.equal(work.call_native_work, work.native_work_after - work.native_work_before);
  assert.ok(work.call_native_work >= 0 && work.call_native_work <= 500_000);
  assert.equal(work.total_accounted_work, work.external_work + work.native_work_after);
  for (const key of [
    "tt_entries", "tt_entries_peak", "tt_capacity",
    "eval_entries", "eval_entries_peak", "eval_capacity",
  ]) {
    assert.ok(Number.isSafeInteger(work[key]) && work[key] >= 0, `invalid ${key}`);
  }
}

function assertAspirationCumulative(previous, current) {
  assert.ok(current.native_work_after >= previous.native_work_after);
  assert.ok(current.tt_entries_peak >= previous.tt_entries_peak);
  assert.ok(current.eval_entries_peak >= previous.eval_entries_peak);
  assert.ok(current.series_cache_weight_peak >= previous.series_cache_weight_peak);
  assert.ok(current.series_cache_entries_peak >= previous.series_cache_entries_peak);
}

function prepareAspirationSession(label, laneBoundary) {
  const created = create({ boundary: laneBoundary });
  const deadline = Math.floor(performance.now() + 60_000);
  const root = enumerate(
    created.session_id,
    0,
    deadline,
    [],
    500_000,
    `${label}-enumerate`,
  );
  assert.equal(root.status, "complete", JSON.stringify(root));
  assert.ok(root.candidates.length > 0, `${label} retained no root candidate`);
  const laneManifest = manifestOf(root);
  const laneCandidate = laneManifest.candidates[0];
  assert.equal(laneCandidate.terminal_score, null, `${label} candidate is terminal`);
  return {
    created,
    deadline,
    root,
    manifest: laneManifest,
    candidate: laneCandidate,
  };
}

function runAspirationSearch(prepared, {
  label,
  purpose,
  alpha,
  beta,
  nativeBefore,
}) {
  return call(
    wasm._spc_root_session_search_json,
    [prepared.created.session_id],
    searchTask({
      candidate: prepared.candidate,
      manifest: prepared.manifest,
      nativeBefore,
      childDepth: 1,
      deadline: prepared.deadline,
      purpose,
      alpha,
      beta,
      iteration: label,
      task: `${label}-task`,
    }),
  );
}

function certifyFailSoftAspiration(label, laneBoundary) {
  const initialDelta = aspirationInitialDelta;
  const oraclePrepared = prepareAspirationSession(`${label}-oracle`, laneBoundary);
  const oracle = runAspirationSearch(oraclePrepared, {
    label: `${label}-oracle-full`,
    purpose: "full",
    alpha: -2 * config.mate_score,
    beta: 2 * config.mate_score,
    nativeBefore: oraclePrepared.root.work.native_work_after,
  });
  assert.equal(oracle.status, "complete", JSON.stringify(oracle));
  assert.equal(oracle.bound, "exact");
  assert.equal(oracle.tt_persistence, "commit");
  assert.equal(oracle.mover, laneBoundary.series % 2 === 1 ? "white" : "black");
  assertAspirationWorkReceipt(
    oracle,
    oraclePrepared.root.work.native_work_after,
  );
  assertAspirationCumulative(oraclePrepared.root.work, oracle.work);
  assert.ok(oracle.work.call_native_work > 0, `${label} oracle did no native work`);
  assert.equal(wasm._spc_root_session_destroy(oraclePrepared.created.session_id), 1);

  const directions = {};
  for (const spec of [
    { name: "fail-high", bound: "lower", center: oracle.score - (3 * initialDelta) / 2 },
    { name: "fail-low", bound: "upper", center: oracle.score + (3 * initialDelta) / 2 },
  ]) {
    assert.ok(Number.isSafeInteger(spec.center));
    const prepared = prepareAspirationSession(`${label}-${spec.name}`, laneBoundary);
    const initialAlpha = spec.center - initialDelta;
    const initialBeta = spec.center + initialDelta;
    const failure = runAspirationSearch(prepared, {
      label: `${label}-${spec.name}-initial`,
      purpose: "aspiration",
      alpha: initialAlpha,
      beta: initialBeta,
      nativeBefore: prepared.root.work.native_work_after,
    });
    assert.equal(failure.status, "complete", JSON.stringify(failure));
    assert.equal(failure.bound, spec.bound);
    assert.equal(failure.tt_persistence, "commit");
    assert.equal(failure.tt_writes_rolled_back, 0);
    assertAspirationWorkReceipt(failure, prepared.root.work.native_work_after);
    assertAspirationCumulative(prepared.root.work, failure.work);
    assert.ok(failure.work.call_native_work > 0, `${label} ${spec.name} did no work`);
    assert.ok(
      failure.work.tt_entries > prepared.root.work.tt_entries,
      `${label} ${spec.name} did not commit a TT bound`,
    );

    const widened = runAspirationSearch(prepared, {
      label: `${label}-${spec.name}-widened`,
      purpose: "aspiration",
      alpha: spec.center - 2 * initialDelta,
      beta: spec.center + 2 * initialDelta,
      nativeBefore: failure.work.native_work_after,
    });
    assert.equal(widened.status, "complete", JSON.stringify(widened));
    assert.equal(widened.bound, "exact");
    assert.equal(widened.tt_persistence, "commit");
    assert.equal(widened.tt_writes_rolled_back, 0);
    assert.deepEqual(aspirationSemantic(widened), aspirationSemantic(oracle));
    assertAspirationWorkReceipt(widened, failure.work.native_work_after);
    assertAspirationCumulative(failure.work, widened.work);
    assert.ok(widened.work.tt_entries >= failure.work.tt_entries);
    assert.ok(widened.work.tt_entries_peak >= failure.work.tt_entries_peak);
    assert.ok(widened.work.call_stats.tt_hits > 0, `${label} widened search missed TT`);
    assert.equal(wasm._spc_root_session_destroy(prepared.created.session_id), 1);

    directions[spec.name.replace("-", "_")] = {
      initial_bound: failure.bound,
      initial_score: failure.score,
      initial_work: failure.work.call_native_work,
      initial_tt_entries: failure.work.tt_entries,
      widened_bound: widened.bound,
      widened_score: widened.score,
      widened_work: widened.work.call_native_work,
      widened_tt_hits: widened.work.call_stats.tt_hits,
      widened_tt_entries: widened.work.tt_entries,
    };
  }
  return {
    mover: oracle.mover,
    oracle: aspirationSemantic(oracle),
    ...directions,
  };
}

const aspirationFailSoft = {
  white: certifyFailSoftAspiration("white", boundary),
  black: certifyFailSoftAspiration("black", {
    ...boundary,
    fen: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1",
    series: 2,
  }),
};

const rootEnumerationWork = enumeration.work.call_native_work;
assert.ok(rootEnumerationWork > 0);
const exactEnumerationSession = create();
const exactEnumerationDeadline = Math.floor(performance.now() + 60_000);
const exactEnumeration = enumerate(
  exactEnumerationSession.session_id,
  0,
  exactEnumerationDeadline,
  [],
  rootEnumerationWork,
  "exact-enumeration-credit",
);
assert.equal(exactEnumeration.status, "complete", JSON.stringify(exactEnumeration));
assert.equal(exactEnumeration.work.call_native_work, rootEnumerationWork);
assert.equal(exactEnumeration.work.call_work_credit, rootEnumerationWork);
assert.equal(wasm._spc_root_session_destroy(exactEnumerationSession.session_id), 1);

const shortEnumerationSession = create();
const shortEnumerationDeadline = Math.floor(performance.now() + 60_000);
const shortEnumeration = enumerate(
  shortEnumerationSession.session_id,
  0,
  shortEnumerationDeadline,
  [],
  rootEnumerationWork - 1,
  "short-enumeration-credit",
);
assert.equal(shortEnumeration.status, "work_limit", JSON.stringify(shortEnumeration));
assert.ok(
  shortEnumeration.work.call_native_work <= shortEnumeration.work.call_work_credit,
);
const retriedEnumeration = enumerate(
  shortEnumerationSession.session_id,
  shortEnumeration.work.native_work_after,
  shortEnumerationDeadline,
  [],
  rootEnumerationWork,
  "retry-enumeration-credit",
);
assert.equal(retriedEnumeration.status, "complete", JSON.stringify(retriedEnumeration));
assert.equal(wasm._spc_root_session_destroy(shortEnumerationSession.session_id), 1);

// Peer import carries the complete manifest, replays it, and then searches it.
const worker = create();
const importRequest = {
  ...operationBase({
    schema: "spc-root-session-import-v1",
    iteration: "depth-1-import",
    generation: 1,
    externalWork: enumeration.work.native_work_after,
    nativeWorkBefore: 0,
    credit: 500_000,
    deadline: Math.floor(performance.now() + 60_000),
  }),
  manifest,
};
const imported = call(
  wasm._spc_root_session_import_json,
  [worker.session_id],
  importRequest,
);
assert.equal(imported.status, "complete", JSON.stringify(imported));
assert.equal(imported.enumeration_identity, manifest.enumeration_identity);
assert.deepEqual(
  imported.candidates.map((item) => item.candidate_identity),
  manifest.candidates.map((item) => item.candidate_identity),
);
const measuredImportWork = imported.work.call_native_work;
assert.ok(measuredImportWork > 0);

// A native over-credit reimport reaches the core, fails, and leaves the prior
// verified manifest searchable (not merely the parser-level rejection below).
const interruptedReimport = call(
  wasm._spc_root_session_import_json,
  [worker.session_id],
  {
    ...importRequest,
    iteration_id: "interrupted-reimport",
    native_work_before: imported.work.native_work_after,
    call_work_credit: 0,
  },
);
assert.equal(interruptedReimport.status, "work_limit", JSON.stringify(interruptedReimport));
assert.equal(interruptedReimport.work.call_native_work, 0);

// Parser-level manifest tampering is rejected without changing work or manifest.
const tampered = structuredClone(manifest);
tampered.candidates[0].order_key = "a1a1";
const tamperReply = call(wasm._spc_root_session_import_json, [worker.session_id], {
  ...importRequest,
  native_work_before: interruptedReimport.work.native_work_after,
  manifest: tampered,
});
assert.equal(tamperReply.status, "unsupported");
assert.equal(tamperReply.error_code, "manifest-invalid");
const afterTamper = call(
  wasm._spc_root_session_search_json,
  [worker.session_id],
  searchTask({
    candidate: imported.candidates[0],
    manifest: imported,
    nativeBefore: interruptedReimport.work.native_work_after,
    externalWork: enumeration.work.native_work_after,
    childDepth: 0,
    deadline: importRequest.deadline_monotonic_ms,
    iteration: "after-tamper",
    task: "after-tamper",
  }),
);
assert.equal(afterTamper.status, "complete", JSON.stringify(afterTamper));
assert.equal(wasm._spc_root_session_destroy(worker.session_id), 1);

const exactImportSession = create();
const exactImportDeadline = Math.floor(performance.now() + 60_000);
const exactImport = call(
  wasm._spc_root_session_import_json,
  [exactImportSession.session_id],
  {
    ...importRequest,
    iteration_id: "exact-import-credit",
    native_work_before: 0,
    call_work_credit: measuredImportWork,
    deadline_monotonic_ms: exactImportDeadline,
    remaining_time_ms: Math.floor(exactImportDeadline - performance.now()),
  },
);
assert.equal(exactImport.status, "complete", JSON.stringify(exactImport));
assert.equal(exactImport.work.call_native_work, measuredImportWork);
assert.equal(wasm._spc_root_session_destroy(exactImportSession.session_id), 1);

const shortImportSession = create();
const shortImportDeadline = Math.floor(performance.now() + 60_000);
const shortImport = call(
  wasm._spc_root_session_import_json,
  [shortImportSession.session_id],
  {
    ...importRequest,
    iteration_id: "short-import-credit",
    native_work_before: 0,
    call_work_credit: measuredImportWork - 1,
    deadline_monotonic_ms: shortImportDeadline,
    remaining_time_ms: Math.floor(shortImportDeadline - performance.now()),
  },
);
assert.equal(shortImport.status, "work_limit", JSON.stringify(shortImport));
assert.ok(shortImport.work.call_native_work <= shortImport.work.call_work_credit);
const retryImport = call(
  wasm._spc_root_session_import_json,
  [shortImportSession.session_id],
  {
    ...importRequest,
    iteration_id: "retry-import-credit",
    native_work_before: shortImport.work.native_work_after,
    call_work_credit: 500_000,
    deadline_monotonic_ms: shortImportDeadline,
    remaining_time_ms: Math.floor(shortImportDeadline - performance.now()),
  },
);
assert.equal(retryImport.status, "complete", JSON.stringify(retryImport));
assert.equal(wasm._spc_root_session_destroy(shortImportSession.session_id), 1);

// Candidate evaluation completes exactly at its measured credit. One fewer
// unit fails closed, matching the canonical Python contract gate.
async function preparedSession() {
  const created = create();
  const deadline = Math.floor(performance.now() + 60_000);
  const root = enumerate(created.session_id, 0, deadline);
  assert.equal(root.status, "complete");
  return { created, deadline, root, manifest: manifestOf(root) };
}
const measuredPrepared = await preparedSession();
const measured = call(
  wasm._spc_root_session_search_json,
  [measuredPrepared.created.session_id],
  searchTask({
    candidate: measuredPrepared.manifest.candidates[0],
    manifest: measuredPrepared.manifest,
    nativeBefore: measuredPrepared.root.work.native_work_after,
    childDepth: 1,
    deadline: measuredPrepared.deadline,
    credit: 500_000,
    iteration: "measure-credit",
    task: "measure-credit",
  }),
);
assert.equal(measured.status, "complete", JSON.stringify(measured));
const measuredDepthTwoWork = measured.work.call_native_work;
assert.equal(wasm._spc_root_session_destroy(measuredPrepared.created.session_id), 1);

const exactPrepared = await preparedSession();
const exactCredit = call(
  wasm._spc_root_session_search_json,
  [exactPrepared.created.session_id],
  searchTask({
    candidate: exactPrepared.manifest.candidates[0],
    manifest: exactPrepared.manifest,
    nativeBefore: exactPrepared.root.work.native_work_after,
    childDepth: 1,
    deadline: exactPrepared.deadline,
    credit: measuredDepthTwoWork,
    iteration: "exact-credit",
    task: "exact-credit",
  }),
);
assert.equal(exactCredit.status, "complete", JSON.stringify(exactCredit));
assert.equal(exactCredit.work.call_native_work, measuredDepthTwoWork);
assert.equal(wasm._spc_root_session_destroy(exactPrepared.created.session_id), 1);

const shortPrepared = await preparedSession();
const oneShort = call(
  wasm._spc_root_session_search_json,
  [shortPrepared.created.session_id],
  searchTask({
    candidate: shortPrepared.manifest.candidates[0],
    manifest: shortPrepared.manifest,
    nativeBefore: shortPrepared.root.work.native_work_after,
    childDepth: 1,
    deadline: shortPrepared.deadline,
    credit: measuredDepthTwoWork - 1,
    iteration: "one-short",
    task: "one-short",
  }),
);
assert.equal(oneShort.status, "work_limit", JSON.stringify(oneShort));
assert.equal(oneShort.bound, "unknown");
assert.ok(oneShort.work.call_native_work <= oneShort.work.call_work_credit);
const retry = call(
  wasm._spc_root_session_search_json,
  [shortPrepared.created.session_id],
  searchTask({
    candidate: shortPrepared.manifest.candidates[0],
    manifest: shortPrepared.manifest,
    nativeBefore: oneShort.work.native_work_after,
    childDepth: 1,
    deadline: shortPrepared.deadline,
    credit: 500_000,
    iteration: "after-work-limit",
    task: "after-work-limit",
  }),
);
assert.equal(retry.status, "complete", JSON.stringify(retry));
assert.equal(wasm._spc_root_session_destroy(shortPrepared.created.session_id), 1);

// Terminal root scores are exact and mover-aware for both colors; they consume
// zero descendant work and may complete with a zero call credit.
for (const terminalCase of [
  {
    fen: "7k/5Q2/6K1/8/8/8/8/8 w - - 0 1",
    series: 1,
    mover: "white",
    score: config.mate_score - 1,
    proof: [1, 1],
  },
  {
    fen: "8/8/8/8/8/6k1/5q2/7K b - - 0 1",
    series: 2,
    mover: "black",
    score: -config.mate_score + 1,
    proof: [-1, -1],
  },
]) {
  const terminalBoundary = {
    ...boundary,
    fen: terminalCase.fen,
    series: terminalCase.series,
  };
  const terminalConfig = { ...config, width: 64 };
  const terminalSession = create({
    boundary: terminalBoundary,
    config: terminalConfig,
  });
  const terminalDeadline = Math.floor(performance.now() + 60_000);
  const terminalManifest = enumerate(
    terminalSession.session_id,
    0,
    terminalDeadline,
  );
  assert.equal(terminalManifest.status, "complete", JSON.stringify(terminalManifest));
  const terminalCandidate = terminalManifest.candidates.find(
    (item) => item.terminal_score === terminalCase.score,
  );
  assert.ok(terminalCandidate, "terminal candidate was not retained");
  const terminalSearch = call(
    wasm._spc_root_session_search_json,
    [terminalSession.session_id],
    {
      ...searchTask({
        candidate: terminalCandidate,
        manifest: terminalManifest,
        nativeBefore: terminalManifest.work.native_work_after,
        childDepth: 1,
        deadline: terminalDeadline,
        credit: 0,
        iteration: `terminal-${terminalCase.mover}`,
        task: `terminal-${terminalCase.mover}`,
      }),
      mover: terminalCase.mover,
    },
  );
  assert.equal(terminalSearch.status, "complete", JSON.stringify(terminalSearch));
  assert.equal(terminalSearch.bound, "exact");
  assert.equal(terminalSearch.score, terminalCase.score);
  assert.deepEqual(terminalSearch.proof_bounds, terminalCase.proof);
  assert.equal(terminalSearch.work.call_native_work, 0);
  assert.equal(wasm._spc_root_session_destroy(terminalSession.session_id), 1);
}

// Exercise CompactMovePath's heap fallback in the optimized WASM build. A
// Series 9 root must retain a complete nine-move candidate, so this crosses
// the eight-move inline boundary under Emscripten -O3/-flto rather than only
// in the native MSVC regression suite.
const longPathBoundary = {
  ...boundary,
  fen: "7k/8/8/8/8/8/8/K7 w - - 0 1",
  series: 9,
};
const longPathSession = create({
  boundary: longPathBoundary,
  config: { ...config, width: 1 },
});
const longPathDeadline = Math.floor(performance.now() + 60_000);
const longPathManifest = enumerate(
  longPathSession.session_id,
  0,
  longPathDeadline,
);
assert.equal(longPathManifest.status, "complete", JSON.stringify(longPathManifest));
assert.equal(longPathManifest.candidates.length, 1);
assert.equal(longPathManifest.candidates[0].root_series.moves.length, 9);
assert.equal(wasm._spc_root_session_destroy(longPathSession.session_id), 1);

// Remaining time is a same-runtime transport. Zero fails before native work;
// a later absolute deadline cannot extend the session's first pinned deadline.
const deadlineSession = create();
const pinnedDeadline = Math.floor(performance.now() + 60_000);
const deadlineManifest = enumerate(deadlineSession.session_id, 0, pinnedDeadline);
assert.equal(deadlineManifest.status, "complete");
const timedOut = call(
  wasm._spc_root_session_search_json,
  [deadlineSession.session_id],
  {
    ...searchTask({
      candidate: deadlineManifest.candidates[0],
      manifest: deadlineManifest,
      nativeBefore: deadlineManifest.work.native_work_after,
      childDepth: 1,
      deadline: pinnedDeadline,
      iteration: "zero-remaining",
      task: "zero-remaining",
    }),
    remaining_time_ms: 0,
  },
);
assert.equal(timedOut.status, "deadline", JSON.stringify(timedOut));
assert.equal(timedOut.bound, "unknown");
assert.equal(timedOut.work.call_native_work, 0);
assert.equal(wasm._spc_root_session_destroy(deadlineSession.session_id), 1);

const extensionSession = create();
const originalDeadline = Math.floor(performance.now() + 60_000);
const extensionManifest = enumerate(
  extensionSession.session_id,
  0,
  originalDeadline,
);
const extensionRejected = call(
  wasm._spc_root_session_enumerate_json,
  [extensionSession.session_id],
  {
    ...operationBase({
      schema: "spc-root-session-enumerate-v1",
      iteration: "deadline-extension",
      generation: 2,
      externalWork: 0,
      nativeWorkBefore: extensionManifest.work.native_work_after,
      credit: 500_000,
      deadline: originalDeadline + 1,
    }),
    preferred_series: [],
  },
);
assert.equal(extensionRejected.status, "unsupported");
assert.equal(extensionRejected.error_code, "deadline-extension-rejected");
const afterExtension = call(
  wasm._spc_root_session_search_json,
  [extensionSession.session_id],
  searchTask({
    candidate: extensionManifest.candidates[0],
    manifest: extensionManifest,
    nativeBefore: extensionManifest.work.native_work_after,
    childDepth: 0,
    deadline: originalDeadline,
    iteration: "after-deadline-extension",
    task: "after-deadline-extension",
  }),
);
assert.equal(afterExtension.status, "complete", JSON.stringify(afterExtension));
assert.equal(wasm._spc_root_session_destroy(extensionSession.session_id), 1);

// Duplicate known keys, surrogate attacks, invalid raw UTF-8, and unknown keys
// are all rejected before a session is created or mutated.
const rawCreate = JSON.stringify(createRequest());
for (const attacked of [
  rawCreate.replace(
    '"source_fingerprint":"0123456789abcdef"',
    '"source_fingerprint":"0123456789abcdef","source_fingerprint":"fedcba9876543210"',
  ),
  rawCreate.replace(
    '"max_depth":2',
    '"max_depth":2,"max_depth":3',
  ),
  rawCreate.replace('"request_id":', '"unknown_field":1,"request_id":'),
  rawCreate.replace('"request-', '"request_id":"\\uD800","discard":"request-'),
]) {
  const reply = callBytes(
    wasm._spc_root_session_create_json,
    [],
    new TextEncoder().encode(attacked),
  );
  assert.equal(reply.status, "unsupported");
}
const invalidUtf8 = new TextEncoder().encode(rawCreate);
invalidUtf8[rawCreate.indexOf("request-")] = 0xff;
const invalidUtf8Reply = callBytes(
  wasm._spc_root_session_create_json,
  [],
  invalidUtf8,
);
assert.equal(invalidUtf8Reply.status, "unsupported");
assert.equal(invalidUtf8Reply.error_code, "invalid-utf8");

// Existing compiled prefix ABI remains linked and authoritative in the same artifact.
const allocated = [boundary.fen, "-", boundary.promoted_hex, ""].map(
  (value) => wasm.stringToNewUTF8(value),
);
try {
  const pointer = wasm._spc_boundary_prefix_json(
    allocated[0],
    boundary.series,
    boundary.quiet_series,
    allocated[1],
    allocated[2],
    allocated[3],
  );
  const prefix = JSON.parse(wasm.UTF8ToString(pointer));
  assert.equal(prefix.ok, true);
  assert.equal(prefix.status, "complete");
  assert.equal(prefix.boundary_state.promoted_hex, boundary.promoted_hex);
} finally {
  allocated.forEach((pointer) => wasm._free(pointer));
}

process.stdout.write(`${JSON.stringify({
  status: "passed",
  enumeration_identity: enumeration.enumeration_identity,
  candidate_identity: candidate.candidate_identity,
  candidate_order_key: candidate.order_key,
  long_path_moves: longPathManifest.candidates[0].root_series.moves,
  neural_s2: {
    ordering_policy: NEURAL_S2_ORDER_POLICY,
    boundary: neuralS2Boundary,
    config: neuralS2Config,
    enumeration_identity: neuralS2Enumeration.enumeration_identity,
    candidate_identity: neuralS2Candidate.candidate_identity,
    candidate_order_key: neuralS2Candidate.order_key,
    manifest: neuralS2Manifest,
    depth_one: {
      score: neuralS2DepthOne.score,
      proof_bounds: neuralS2DepthOne.proof_bounds,
      root_series: neuralS2DepthOne.root_series,
      child_pv: neuralS2DepthOne.child_pv,
      work: neuralS2DepthOne.work,
    },
    depth_two: {
      score: neuralS2DepthTwo.score,
      proof_bounds: neuralS2DepthTwo.proof_bounds,
      root_series: neuralS2DepthTwo.root_series,
      child_pv: neuralS2DepthTwo.child_pv,
      work: neuralS2DepthTwo.work,
    },
  },
  depth_one: {
    score: depthOne.score,
    proof_bounds: depthOne.proof_bounds,
    child_pv: depthOne.child_pv,
    work: depthOne.work,
  },
  depth_two: {
    score: depthTwo.score,
    proof_bounds: depthTwo.proof_bounds,
    child_pv: depthTwo.child_pv,
    work: depthTwo.work,
  },
  aspiration_window: {
    accepted: true,
    bound: aspiration.bound,
    score: aspiration.score,
    alpha: aspiration.alpha,
    beta: aspiration.beta,
    narrow_full_rejected: narrowFullRejected.error_code === "candidate-task-invalid",
    full_window_rejected: aspirationFullWindowRejected.error_code === "candidate-task-invalid",
    rollback_rejected: aspirationRollbackRejected.error_code === "candidate-task-invalid",
    scout_rollback_preserved: scout.status === "complete"
      && scout.tt_persistence === "rollback",
  },
  aspiration_fail_soft: aspirationFailSoft,
  deep_teacher: {
    boundary: materialBoundary,
    config: deepTeacherConfig,
    baseline_enumeration_identity: materialBaselineEnumeration.enumeration_identity,
    enumeration_identity: materialModelEnumeration.enumeration_identity,
    manifest: materialModelManifest,
    baseline: {
      score: materialBaseline.score,
      proof_bounds: materialBaseline.proof_bounds,
      child_pv: materialBaseline.child_pv,
      work: materialBaseline.work,
    },
    modeled: {
      score: materialModeled.score,
      proof_bounds: materialModeled.proof_bounds,
      child_pv: materialModeled.child_pv,
      work: materialModeled.work,
    },
  },
  manifest,
})}\n`);
