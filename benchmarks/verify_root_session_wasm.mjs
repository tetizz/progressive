import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { performance } from "node:perf_hooks";
import { pathToFileURL } from "node:url";


const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const START_BLACK_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1";
const LIVE_S5 = "rn1q1bnr/ppp1pkpp/5p2/8/3Pp3/2NB4/PPP2PPP/R1BbK1NR w KQ - 0 7";
const BARE_KINGS = "8/8/8/8/8/2k5/8/K7 w - - 0 1";
const CHECKED_HORIZON_FEN = "6k1/8/8/q7/8/8/5PPP/1R4K1 w - - 0 1";
const DEEP_HORIZON_FEN = "2k5/4pr2/8/8/3K3N/7R/2qP2b1/1B6 w - - 0 1";
const BLACK_HORIZON_FEN = "1r4k1/5ppp/8/8/Q7/8/8/6K1 b - - 0 1";
const HIGH_SERIES_FEN = "8/8/8/8/1K6/8/1k6/8 b - - 100 110";
const HIGH_SERIES_ROOT = [
  "b2a1", "a1a2", "a2a1", "a1a2", "a2a1", "a1b2",
  "b2c2", "c2b2", "b2c2", "c2b2", "b2c2", "c2b2",
  "b2c2", "c2b2", "b2c2", "c2b2", "b2c2", "c2b2",
  "b2c2", "c2b2", "b2c2", "c2b2", "b2c2", "c2b2",
].join("/");
const HIGH_SERIES_CHILD = [
  "b4a4", "a4a5", "a5a4", "a4a5", "a5b4", "b4b5",
  "b5b4", "b4b5", "b5b4", "b4b5", "b5b4", "b4b5",
  "b5b4", "b4b5", "b5b4", "b4b5", "b5b4", "b4b5",
  "b5b4", "b4b5", "b5b4", "b4b5", "b5b4", "b4b5", "b5b4",
].join("/");
const ZERO_PROMOTED = "0000000000000000";
const MATE_SCORE = 1_000_000;
const HIGH_SERIES_MAX_WORK = 250_000;
const REQUIRED_EXPORTS = [
  "_spc_start_kernel_search_json",
  "_spc_boundary_kernel_search_json",
  "_spc_boundary_prefix_json",
  "_spc_boundary_prefix_contract_json",
  "_spc_start_kernel_abi_version",
  "_spc_root_session_contract_json",
  "_spc_root_session_create_json",
  "_spc_root_session_enumerate_json",
  "_spc_root_session_import_json",
  "_spc_root_session_search_json",
  "_spc_root_session_destroy",
  "_spc_root_session_abi_version",
  "_spc_series_mate_search_json",
  "_spc_series_mate_abi_version",
  "_malloc",
  "_free",
];


function parseArguments(argv) {
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined) {
      throw new Error(`invalid argument near ${String(key)}`);
    }
    values.set(key, value);
  }
  for (const required of ["--module", "--wasm", "--build-receipt", "--output"]) {
    if (!values.has(required)) {
      throw new Error(`missing required ${required}`);
    }
  }
  const positiveInteger = (name, fallback) => {
    const raw = values.get(name) ?? String(fallback);
    const parsed = Number(raw);
    if (!Number.isSafeInteger(parsed) || parsed < 1) {
      throw new Error(`${name} must be a positive safe integer`);
    }
    return parsed;
  };
  return {
    module: values.get("--module"),
    wasm: values.get("--wasm"),
    buildReceipt: values.get("--build-receipt"),
    output: values.get("--output"),
    timeoutMs: positiveInteger("--timeout-ms", 120_000),
    seriesCacheCapacity: positiveInteger("--series-cache-capacity", 65_536),
    ttCapacity: positiveInteger("--tt-capacity", 262_144),
    evalCapacity: positiveInteger("--eval-capacity", 262_144),
    maxWork: positiveInteger("--max-work", 20_000_000),
  };
}


function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}


function assertSafeJsonIntegers(value, path = "$") {
  if (typeof value === "number") {
    assert.ok(Number.isSafeInteger(value), `unsafe JSON number at ${path}: ${value}`);
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertSafeJsonIntegers(item, `${path}[${index}]`));
    return;
  }
  if (value !== null && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      assertSafeJsonIntegers(item, `${path}.${key}`);
    }
  }
}


function elapsedCall(call) {
  const started = performance.now();
  const value = call();
  return { value, elapsedMs: performance.now() - started };
}


function createBridge(Module) {
  const encoder = new TextEncoder();

  function withCString(value, callback) {
    const bytes = encoder.encode(value);
    const pointer = Module._malloc(bytes.length + 1);
    assert.notEqual(pointer, 0, "WASM malloc failed");
    Module.HEAPU8.set(bytes, pointer);
    Module.HEAPU8[pointer + bytes.length] = 0;
    try {
      return callback(pointer);
    } finally {
      Module._free(pointer);
    }
  }

  function rootJson(exportName, sessionId, request) {
    const bytes = encoder.encode(JSON.stringify(request));
    const pointer = Module._malloc(bytes.length);
    assert.notEqual(pointer, 0, "WASM request allocation failed");
    Module.HEAPU8.set(bytes, pointer);
    try {
      const resultPointer = sessionId === null
        ? Module[exportName](pointer, bytes.length)
        : Module[exportName](sessionId, pointer, bytes.length);
      return JSON.parse(Module.UTF8ToString(resultPointer));
    } finally {
      Module._free(pointer);
    }
  }

  function prefixJson(fen, series, quiet, ep, promoted, prefix) {
    return withCString(fen, (fenPointer) => withCString(ep, (epPointer) => (
      withCString(promoted, (promotedPointer) => withCString(prefix, (prefixPointer) => {
        const pointer = Module._spc_boundary_prefix_json(
          fenPointer,
          series,
          quiet,
          epPointer,
          promotedPointer,
          prefixPointer,
        );
        return JSON.parse(Module.UTF8ToString(pointer));
      }))
    )));
  }

  function mateJson(fen, series, ep, promoted, maxPositions, maxWork, timeMs) {
    return withCString(fen, (fenPointer) => withCString(ep, (epPointer) => (
      withCString(promoted, (promotedPointer) => {
        const pointer = Module._spc_series_mate_search_json(
          fenPointer,
          series,
          epPointer,
          promotedPointer,
          maxPositions,
          maxWork,
          timeMs,
        );
        return JSON.parse(Module.UTF8ToString(pointer));
      })
    )));
  }

  return { rootJson, prefixJson, mateJson };
}


function assertWorkReceipt(result, expectedBefore, expectedCredit) {
  const work = result.work;
  assert.equal(typeof work, "object", "root reply has no work receipt");
  assert.equal(work.native_work_before, expectedBefore);
  assert.equal(work.call_work_credit, expectedCredit);
  assert.equal(work.call_native_work, work.native_work_after - work.native_work_before);
  assert.ok(work.call_native_work >= 0);
  assert.ok(work.call_native_work <= expectedCredit);
  assert.equal(work.total_accounted_work, work.external_work + work.native_work_after);
  for (const key of [
    "tt_entries",
    "tt_entries_peak",
    "tt_capacity",
    "eval_entries",
    "eval_entries_peak",
    "eval_capacity",
    "series_cache_capacity",
    "series_cache_weight_peak",
    "series_cache_entries_peak",
  ]) {
    assert.ok(Number.isSafeInteger(work[key]) && work[key] >= 0, `invalid ${key}`);
  }
  return work.native_work_after;
}


function assertCumulative(previous, current) {
  assert.ok(current.native_work_after >= previous.native_work_after);
  assert.ok(current.tt_entries_peak >= previous.tt_entries_peak);
  assert.ok(current.eval_entries_peak >= previous.eval_entries_peak);
  assert.ok(current.series_cache_weight_peak >= previous.series_cache_weight_peak);
  assert.ok(current.series_cache_entries_peak >= previous.series_cache_entries_peak);
  for (const [key, value] of Object.entries(previous.cumulative_stats)) {
    assert.ok(current.cumulative_stats[key] >= value, `cumulative stat regressed: ${key}`);
  }
}


function manifestFrom(result) {
  return {
    enumeration_identity: result.enumeration_identity,
    root_white_to_move: result.root_white_to_move,
    requested_width: result.requested_width,
    retained_count: result.retained_count,
    width_complete: result.width_complete,
    preferred_series: result.preferred_series,
    candidates: result.candidates,
  };
}


function exactCandidateResult(result) {
  return {
    status: result.status,
    bound: result.bound,
    score: result.score,
    terminal: result.terminal,
    proof_bounds: result.proof_bounds,
    root_series: result.root_series,
    child_pv: result.child_pv,
    selective: result.selective,
    evaluation_work_limit_reached: result.evaluation_work_limit_reached,
  };
}


function proofBoundary(fen, series, quietSeries) {
  return {
    fen,
    series,
    quiet_series: quietSeries,
    ep_targets: [],
    promoted_hex: ZERO_PROMOTED,
    chess960: false,
  };
}


function proofSeries(
  moves,
  fen,
  series,
  quietSeries,
  { transpositionCount = 1, outcome = null, endedByCheck = false } = {},
) {
  return {
    moves,
    machine_notation: moves.join("/"),
    transposition_count: transpositionCount,
    child_boundary: proofBoundary(fen, series, quietSeries),
    outcome,
    ended_by_check: endedByCheck,
  };
}


function deriveHorizonDisposition(result, priorSameRoot, requestProofCount) {
  const newestProofBit = 2 ** (requestProofCount - 1);
  if (
    result.schema !== "spc-root-horizon-research-result-v1"
    || result.status !== "complete"
    || result.bound !== "exact"
    || result.horizon_proofs_validated !== requestProofCount
  ) {
    return "invalid-horizon-result";
  }
  if ((result.horizon_proof_hit_mask & newestProofBit) === 0) {
    return "newest-proof-not-hit";
  }
  if (
    priorSameRoot.schema !== "spc-root-candidate-result-v1"
    || priorSameRoot.status !== "complete"
    || priorSameRoot.bound !== "exact"
    || priorSameRoot.candidate_identity !== result.candidate_identity
    || priorSameRoot.order_key !== result.order_key
    || priorSameRoot.score === result.score
  ) {
    return "newest-proof-hit-without-same-root-repair";
  }
  return "same-root-repaired";
}


function horizonProofAnchor(proof) {
  const mate = proof?.mate_reply?.machine_notation;
  if (mate === "c7d7/d7e6/e6d6/d6d4") {
    return "deep";
  }
  if (mate === "c8b8/e7e5/f7b7/a1d4") {
    return "alternate";
  }
  if (mate === "g1f2/a4e8") {
    return "black-mate";
  }
  throw new Error(`unknown checked-horizon proof anchor: ${String(mate)}`);
}


function horizonCaseEvidence({
  result,
  priorSameRoot,
  proofs,
}) {
  const proofOrder = proofs.map(horizonProofAnchor);
  const proofPathLengths = proofs.map((proof) => proof.rooted_path.length);
  assert.equal(result.child_depth, proofPathLengths[0] - 1);
  assert.equal(proofOrder.length, proofPathLengths.length);
  assert.equal(priorSameRoot.candidate_identity, result.candidate_identity);
  assert.equal(priorSameRoot.order_key, result.order_key);
  const disposition = deriveHorizonDisposition(
    result,
    priorSameRoot,
    proofOrder.length,
  );
  return {
    root_side: result.mover,
    root_order_key: result.order_key,
    request_proof_count: proofOrder.length,
    request_proof_order: proofOrder,
    request_proof_path_lengths: proofPathLengths,
    newest_proof_anchor: proofOrder.at(-1),
    child_depth: result.child_depth,
    schema: result.schema,
    status: result.status,
    bound: result.bound,
    score: result.score,
    horizon_proofs_validated: result.horizon_proofs_validated,
    horizon_proof_hits: result.horizon_proof_hits,
    horizon_proof_hit_mask: result.horizon_proof_hit_mask,
    horizon_proof_set_identity_sha256: sha256(result.horizon_proof_set_identity),
    candidate_identity_sha256: sha256(result.candidate_identity),
    exact_tt_hits: result.work.call_stats.tt_hits,
    prior_same_root_schema: priorSameRoot.schema,
    prior_same_root_status: priorSameRoot.status,
    prior_same_root_bound: priorSameRoot.bound,
    prior_same_root_score: priorSameRoot.score,
    prior_same_root_candidate_identity_sha256: sha256(
      priorSameRoot.candidate_identity,
    ),
    disposition,
  };
}


function warmHorizonCaseEvidence({ result, repaired, proofs }) {
  assert.equal(result.schema, "spc-root-horizon-research-result-v1");
  assert.equal(result.status, "complete");
  assert.equal(result.bound, "exact");
  assert.equal(result.score, repaired.score);
  assert.deepEqual(result.root_series, repaired.root_series);
  assert.deepEqual(result.child_pv, repaired.child_pv);
  assert.equal(result.horizon_proof_set_identity, repaired.horizon_proof_set_identity);
  assert.equal(result.candidate_identity, repaired.candidate_identity);
  assert.equal(result.horizon_proofs_validated, proofs.length);
  assert.equal(result.horizon_proof_hits, 0);
  assert.equal(result.horizon_proof_hit_mask, 0);
  assert.ok(Number.isSafeInteger(result.work.call_stats.tt_hits));
  assert.ok(result.work.call_stats.tt_hits > 0);
  const proofOrder = proofs.map(horizonProofAnchor);
  const proofPathLengths = proofs.map((proof) => proof.rooted_path.length);
  const rootPvSha256 = sha256(JSON.stringify({
    root_series: result.root_series,
    child_pv: result.child_pv,
  }));
  const priorRootPvSha256 = sha256(JSON.stringify({
    root_series: repaired.root_series,
    child_pv: repaired.child_pv,
  }));
  assert.equal(rootPvSha256, priorRootPvSha256);
  return {
    root_side: result.mover,
    root_order_key: result.order_key,
    request_proof_count: proofs.length,
    request_proof_order: proofOrder,
    request_proof_path_lengths: proofPathLengths,
    newest_proof_anchor: proofOrder.at(-1),
    child_depth: result.child_depth,
    schema: result.schema,
    status: result.status,
    bound: result.bound,
    score: result.score,
    horizon_proofs_validated: result.horizon_proofs_validated,
    horizon_proof_hits: result.horizon_proof_hits,
    horizon_proof_hit_mask: result.horizon_proof_hit_mask,
    horizon_proof_set_identity_sha256: sha256(result.horizon_proof_set_identity),
    candidate_identity_sha256: sha256(result.candidate_identity),
    exact_tt_hits: result.work.call_stats.tt_hits,
    prior_same_root_schema: repaired.schema,
    prior_same_root_status: repaired.status,
    prior_same_root_bound: repaired.bound,
    prior_same_root_score: repaired.score,
    prior_same_root_candidate_identity_sha256: sha256(
      repaired.candidate_identity,
    ),
    root_pv_sha256: rootPvSha256,
    prior_same_root_root_pv_sha256: priorRootPvSha256,
    disposition: "warm-exact-recertified",
  };
}


async function main() {
  const args = parseArguments(process.argv.slice(2));
  const [moduleBytes, wasmBytes, buildReceiptText] = await Promise.all([
    readFile(args.module),
    readFile(args.wasm),
    readFile(args.buildReceipt, "utf8"),
  ]);
  const buildReceipt = JSON.parse(buildReceiptText);
  assert.equal(buildReceipt.status, "built-not-certified");
  assert.equal(buildReceipt.product_publishable, false);
  assert.equal(buildReceipt.module_js_sha256, sha256(moduleBytes));
  assert.equal(buildReceipt.wasm_sha256, sha256(wasmBytes));
  assert.equal(buildReceipt.runtime_variant, "single");
  assert.equal(buildReceipt.thread_count, 1);
  assert.equal(buildReceipt.pthreads, false);

  const factory = (await import(pathToFileURL(args.module).href)).default;
  assert.equal(typeof factory, "function", "module default export is not a factory");
  const Module = await factory({ wasmBinary: wasmBytes });
  for (const name of REQUIRED_EXPORTS) {
    assert.equal(typeof Module[name], "function", `combined export is missing: ${name}`);
  }
  assert.equal(Module._spc_start_kernel_abi_version(), 1);
  assert.equal(Module._spc_root_session_abi_version(), 2);
  assert.equal(Module._spc_series_mate_abi_version(), 1);
  const contract = JSON.parse(
    Module.UTF8ToString(Module._spc_root_session_contract_json()),
  );
  assert.equal(contract.schema, "spc-root-session-contract-v1");
  assert.equal(contract.abi_version, 2);
  assert.equal(contract.worker_threads, 1);
  assert.equal(contract.pthreads_required, false);
  assert.equal(contract.product_publishable, false);
  assert.equal(contract.reply_mate_safety, false);
  assert.equal(contract.capabilities.call_work_credit, true);
  assert.equal(contract.capabilities.persistent_depth_reuse, true);
  assert.equal(contract.capabilities.aspiration_windows, true);
  assert.equal(contract.capabilities.selected_owner_certification, true);
  assert.equal(contract.capabilities.canonical_root_tactical_policy, true);
  assert.equal(contract.capabilities.checked_horizon_proof_research, true);
  assert.equal(contract.request_schemas.search, "spc-root-candidate-task-v1");
  assert.equal(
    contract.request_schemas.horizon_research,
    "spc-root-horizon-research-task-v1",
  );
  assert.equal(contract.result_schemas.search, "spc-root-candidate-result-v1");
  assert.equal(
    contract.result_schemas.horizon_research,
    "spc-root-horizon-research-result-v1",
  );
  assert.equal(contract.hard_limits.maximum_horizon_proofs, 16);
  assert.equal(contract.hard_limits.maximum_horizon_proof_path, 8);
  assert.deepEqual(contract.horizon_research, {
    task_schema: "spc-root-horizon-research-task-v1",
    result_schema: "spc-root-horizon-research-result-v1",
    proof_schema: "spc-retained-root-horizon-proof-v1",
    purpose: "horizon-research",
    full_window: true,
    tt_persistence: "commit",
    hit_mask_order: "request-order",
    warm_exact_zero_hit_allowed: true,
  });
  assert.equal(contract.hard_limits.root_tactical_policy, "canonical-boundary-policy-v1");
  assert.deepEqual(contract.hard_limits.root_tactical_protection_values, [false]);
  assert.equal(contract.hard_limits.minimum_aspiration_initial_delta, 2_048);
  assert.equal(contract.hard_limits.maximum_aspiration_attempts, 4);
  const aspirationInitialDelta = contract.hard_limits.minimum_aspiration_initial_delta;
  const prefixContract = JSON.parse(
    Module.UTF8ToString(Module._spc_boundary_prefix_contract_json()),
  );
  assert.equal(prefixContract.schema, "spc-boundary-prefix-contract-v1");
  assert.equal(prefixContract.abi_version, 1);

  const bridge = createBridge(Module);
  const identity = {
    source_fingerprint: buildReceipt.source_fingerprint,
    kernel_sha256: buildReceipt.kernel_sha256,
    module_js_sha256: buildReceipt.module_js_sha256,
    certificate_id: `lab-not-certified-${buildReceipt.artifact_set_sha256.slice(0, 16)}`,
    runtime_variant: "single",
    thread_count: 1,
    engine_version: buildReceipt.engine_version,
    ruleset_version: buildReceipt.ruleset_version,
    profile_id: buildReceipt.profile_id,
  };
  const boundary = {
    fen: START_FEN,
    series: 1,
    quiet_series: 0,
    ep_targets: [],
    promoted_hex: ZERO_PROMOTED,
    chess960: false,
  };
  const config = {
    max_depth: 2,
    width: 4,
    max_work: args.maxWork,
    mate_score: MATE_SCORE,
    series_cache_capacity: args.seriesCacheCapacity,
    external_cache_weight: 0,
    worker_threads: 1,
    root_tactical_protection: false,
    root_contract_tt_capacity: args.ttCapacity,
    root_contract_eval_capacity: args.evalCapacity,
    weights: {
      material: 100,
      king_space: 100,
      series_reach: 100,
      promotion_corridors: 100,
      immediate_vulnerability: 100,
      useful_mobility: 100,
      boundary_check: 100,
    },
  };
  const createRequest = (requestId, iterationId, generation) => ({
    schema: "spc-root-session-create-v1",
    request_id: requestId,
    iteration_id: iterationId,
    generation,
    ...identity,
    boundary,
    config,
  });
  const route = (schema, requestId, iterationId, generation, nativeWork, credit, deadline) => ({
    schema,
    request_id: requestId,
    iteration_id: iterationId,
    generation,
    ...identity,
    external_work: 0,
    native_work_before: nativeWork,
    call_work_credit: credit,
    deadline_monotonic_ms: deadline,
    remaining_time_ms: Math.max(0, Math.ceil(deadline - performance.now())),
  });
  const searchRequest = (
    requestId,
    iterationId,
    generation,
    nativeWork,
    credit,
    deadline,
    enumeration,
    candidate,
    childDepth,
    purpose = "full",
    {
      alpha = -2 * MATE_SCORE,
      beta = 2 * MATE_SCORE,
      ttPersistence = purpose === "scout" ? "rollback" : "commit",
    } = {},
  ) => ({
    ...route(
      "spc-root-candidate-task-v1",
      requestId,
      iterationId,
      generation,
      nativeWork,
      credit,
      deadline,
    ),
    safety_revision: 0,
    incumbent_epoch: 0,
    task_id: `${requestId}-task`,
    enumeration_identity: enumeration.enumeration_identity,
    candidate_identity: candidate.candidate_identity,
    order_index: candidate.order_index,
    order_key: candidate.order_key,
    purpose,
    mate_score: MATE_SCORE,
    child_depth: childDepth,
    alpha,
    beta,
    tt_persistence: ttPersistence,
    mover: enumeration.root_white_to_move ? "white" : "black",
  });

  const aspirationSemantic = (result) => ({
    score: result.score,
    proof_bounds: result.proof_bounds,
    root_series: result.root_series,
    child_pv: result.child_pv,
  });
  const aspirationWorkReceipt = (result) => ({
    native_work_before: result.work.native_work_before,
    native_work_after: result.work.native_work_after,
    call_native_work: result.work.call_native_work,
    tt_entries: result.work.tt_entries,
    tt_entries_peak: result.work.tt_entries_peak,
    tt_hits: result.work.call_stats.tt_hits,
  });

  const prepareAspirationSession = (label, laneBoundary) => {
    const laneCreated = bridge.rootJson(
      "_spc_root_session_create_json",
      null,
      {
        ...createRequest(`${label}-create`, label, 1),
        boundary: laneBoundary,
      },
    );
    assert.equal(laneCreated.status, "ready", JSON.stringify(laneCreated));
    const laneDeadline = Math.ceil(performance.now() + args.timeoutMs);
    const laneEnumeration = bridge.rootJson(
      "_spc_root_session_enumerate_json",
      laneCreated.session_id,
      {
        ...route(
          "spc-root-session-enumerate-v1",
          `${label}-enumerate`,
          label,
          1,
          0,
          args.maxWork,
          laneDeadline,
        ),
        preferred_series: [],
      },
    );
    assert.equal(laneEnumeration.status, "complete", JSON.stringify(laneEnumeration));
    assertWorkReceipt(laneEnumeration, 0, args.maxWork);
    assert.ok(laneEnumeration.candidates.length > 0, `${label} retained no candidate`);
    const laneCandidate = laneEnumeration.candidates[0];
    assert.equal(laneCandidate.terminal_score, null, `${label} candidate is terminal`);
    return {
      label,
      sessionId: laneCreated.session_id,
      deadline: laneDeadline,
      enumeration: laneEnumeration,
      candidate: laneCandidate,
    };
  };

  const runAspirationCandidate = (
    prepared,
    { requestId, purpose, alpha, beta, nativeWork },
  ) => bridge.rootJson(
    "_spc_root_session_search_json",
    prepared.sessionId,
    searchRequest(
      requestId,
      prepared.label,
      2,
      nativeWork,
      args.maxWork,
      prepared.deadline,
      prepared.enumeration,
      prepared.candidate,
      1,
      purpose,
      { alpha, beta },
    ),
  );

  const certifyFailSoftAspiration = (label, laneBoundary) => {
    const oraclePrepared = prepareAspirationSession(`${label}-oracle`, laneBoundary);
    const oracle = runAspirationCandidate(oraclePrepared, {
      requestId: `${label}-oracle-full`,
      purpose: "full",
      alpha: -2 * MATE_SCORE,
      beta: 2 * MATE_SCORE,
      nativeWork: oraclePrepared.enumeration.work.native_work_after,
    });
    assert.equal(oracle.status, "complete", JSON.stringify(oracle));
    assert.equal(oracle.bound, "exact");
    assert.equal(oracle.tt_persistence, "commit");
    assert.equal(oracle.mover, laneBoundary.series % 2 === 1 ? "white" : "black");
    assertWorkReceipt(
      oracle,
      oraclePrepared.enumeration.work.native_work_after,
      args.maxWork,
    );
    assertCumulative(oraclePrepared.enumeration.work, oracle.work);
    assert.ok(oracle.work.call_native_work > 0, `${label} oracle did no native work`);
    assert.equal(Module._spc_root_session_destroy(oraclePrepared.sessionId), 1);

    let peakMemory = oracle.memory_peak_bytes;
    const directions = {};
    const initialDelta = aspirationInitialDelta;
    for (const spec of [
      {
        name: "fail_high",
        bound: "lower",
        center: oracle.score - (3 * initialDelta) / 2,
      },
      {
        name: "fail_low",
        bound: "upper",
        center: oracle.score + (3 * initialDelta) / 2,
      },
    ]) {
      assert.ok(Number.isSafeInteger(spec.center));
      const prepared = prepareAspirationSession(`${label}-${spec.name}`, laneBoundary);
      const failure = runAspirationCandidate(prepared, {
        requestId: `${label}-${spec.name}-initial`,
        purpose: "aspiration",
        alpha: spec.center - initialDelta,
        beta: spec.center + initialDelta,
        nativeWork: prepared.enumeration.work.native_work_after,
      });
      assert.equal(failure.status, "complete", JSON.stringify(failure));
      assert.equal(failure.bound, spec.bound);
      assert.equal(failure.tt_persistence, "commit");
      assert.equal(failure.tt_writes_rolled_back, 0);
      assertWorkReceipt(
        failure,
        prepared.enumeration.work.native_work_after,
        args.maxWork,
      );
      assertCumulative(prepared.enumeration.work, failure.work);
      assert.ok(failure.work.call_native_work > 0, `${label} ${spec.name} did no work`);
      assert.ok(
        failure.work.tt_entries > prepared.enumeration.work.tt_entries,
        `${label} ${spec.name} did not commit its TT bound`,
      );

      const widened = runAspirationCandidate(prepared, {
        requestId: `${label}-${spec.name}-widened`,
        purpose: "aspiration",
        alpha: spec.center - 2 * initialDelta,
        beta: spec.center + 2 * initialDelta,
        nativeWork: failure.work.native_work_after,
      });
      assert.equal(widened.status, "complete", JSON.stringify(widened));
      assert.equal(widened.bound, "exact");
      assert.equal(widened.tt_persistence, "commit");
      assert.equal(widened.tt_writes_rolled_back, 0);
      assert.deepEqual(aspirationSemantic(widened), aspirationSemantic(oracle));
      assertWorkReceipt(widened, failure.work.native_work_after, args.maxWork);
      assertCumulative(failure.work, widened.work);
      assert.ok(widened.work.tt_entries >= failure.work.tt_entries);
      assert.ok(widened.work.call_stats.tt_hits > 0, `${label} widened search missed TT`);
      assert.equal(Module._spc_root_session_destroy(prepared.sessionId), 1);
      peakMemory = Math.max(
        peakMemory,
        failure.memory_peak_bytes,
        widened.memory_peak_bytes,
      );

      directions[spec.name] = {
        initial: {
          bound: failure.bound,
          score: failure.score,
          tt_persistence: failure.tt_persistence,
          tt_writes_rolled_back: failure.tt_writes_rolled_back,
          work: aspirationWorkReceipt(failure),
        },
        widened: {
          ...aspirationSemantic(widened),
          bound: widened.bound,
          tt_persistence: widened.tt_persistence,
          tt_writes_rolled_back: widened.tt_writes_rolled_back,
          work: aspirationWorkReceipt(widened),
        },
      };
    }
    return {
      mover: oracle.mover,
      oracle: {
        ...aspirationSemantic(oracle),
        work: aspirationWorkReceipt(oracle),
      },
      ...directions,
      memory_peak_bytes: peakMemory,
    };
  };

  const aspirationFailSoft = {
    white: certifyFailSoftAspiration("aspiration-white", boundary),
    black: certifyFailSoftAspiration("aspiration-black", {
      ...boundary,
      fen: START_BLACK_FEN,
      series: 2,
    }),
  };

  const timings = {};
  const created = elapsedCall(() => bridge.rootJson(
    "_spc_root_session_create_json",
    null,
    createRequest("create-primary", "persistent-d1-d2", 1),
  ));
  timings.createPrimaryMs = created.elapsedMs;
  assert.equal(created.value.status, "ready");
  assert.equal(created.value.configured_max_depth, 2);
  assert.deepEqual(created.value.config, config);
  assert.equal(created.value.canonical_root_tactical_policy, "canonical-boundary-policy-v1");
  assert.equal(created.value.canonical_root_tactical_protection, false);
  assert.equal(created.value.capabilities.aspiration_windows, true);
  assert.equal(created.value.capabilities.canonical_root_tactical_policy, true);
  assert.equal(created.value.capabilities.checked_horizon_proof_research, true);
  const primarySession = created.value.session_id;
  const deadline = Math.ceil(performance.now() + args.timeoutMs);
  const credit = args.maxWork;
  const enumerated = elapsedCall(() => bridge.rootJson(
    "_spc_root_session_enumerate_json",
    primarySession,
    {
      ...route(
        "spc-root-session-enumerate-v1",
        "enumerate-primary",
        "persistent-d1-d2",
        1,
        0,
        credit,
        deadline,
      ),
      preferred_series: [],
    },
  ));
  timings.enumeratePrimaryMs = enumerated.elapsedMs;
  assert.equal(enumerated.value.status, "complete");
  assert.equal(enumerated.value.canonical_root_tactical_policy, "canonical-boundary-policy-v1");
  assert.equal(enumerated.value.canonical_root_tactical_protection, false);
  assert.ok(enumerated.value.retained_count > 0);
  assert.equal(enumerated.value.retained_count, enumerated.value.candidates.length);
  let nativeWork = assertWorkReceipt(enumerated.value, 0, credit);
  const candidate = enumerated.value.candidates[0];
  assert.equal(candidate.terminal_score, null, "start candidate unexpectedly terminal");

  const depthOne = elapsedCall(() => bridge.rootJson(
    "_spc_root_session_search_json",
    primarySession,
    searchRequest(
      "search-d1",
      "persistent-d1-d2",
      1,
      nativeWork,
      credit,
      deadline,
      enumerated.value,
      candidate,
      0,
    ),
  ));
  timings.searchD1Ms = depthOne.elapsedMs;
  assert.equal(depthOne.value.status, "complete");
  assert.notEqual(depthOne.value.bound, "unknown");
  for (const key of [
    "horizon_proof_set_identity",
    "horizon_proofs_validated",
    "horizon_proof_hits",
    "horizon_proof_hit_mask",
  ]) {
    assert.equal(key in depthOne.value, false, `ordinary candidate v1 leaked ${key}`);
  }
  nativeWork = assertWorkReceipt(depthOne.value, nativeWork, credit);
  assertCumulative(enumerated.value.work, depthOne.value.work);

  const depthTwo = elapsedCall(() => bridge.rootJson(
    "_spc_root_session_search_json",
    primarySession,
    searchRequest(
      "search-d2",
      "persistent-d1-d2",
      2,
      nativeWork,
      credit,
      deadline,
      enumerated.value,
      candidate,
      1,
    ),
  ));
  timings.searchD2Ms = depthTwo.elapsedMs;
  assert.equal(depthTwo.value.status, "complete");
  assert.notEqual(depthTwo.value.bound, "unknown");
  nativeWork = assertWorkReceipt(depthTwo.value, nativeWork, credit);
  assertCumulative(depthOne.value.work, depthTwo.value.work);

  const aspiration = elapsedCall(() => bridge.rootJson(
    "_spc_root_session_search_json",
    primarySession,
    searchRequest(
      "search-d2-aspiration",
      "persistent-d1-d2",
      2,
      nativeWork,
      credit,
      deadline,
      enumerated.value,
      candidate,
      1,
      "aspiration",
      {
        alpha: depthTwo.value.score - aspirationInitialDelta,
        beta: depthTwo.value.score + aspirationInitialDelta,
      },
    ),
  ));
  timings.aspirationMs = aspiration.elapsedMs;
  assert.equal(aspiration.value.status, "complete");
  assert.equal(aspiration.value.bound, "exact");
  assert.equal(aspiration.value.tt_persistence, "commit");
  nativeWork = assertWorkReceipt(aspiration.value, nativeWork, credit);
  assertCumulative(depthTwo.value.work, aspiration.value.work);

  const aspirationFullWindow = bridge.rootJson(
    "_spc_root_session_search_json",
    primarySession,
    searchRequest(
      "search-d2-aspiration-full-window",
      "persistent-d1-d2",
      2,
      nativeWork,
      credit,
      deadline,
      enumerated.value,
      candidate,
      1,
      "aspiration",
    ),
  );
  assert.equal(aspirationFullWindow.status, "unsupported");
  assert.equal(aspirationFullWindow.error_code, "candidate-task-invalid");

  const aspirationRollback = bridge.rootJson(
    "_spc_root_session_search_json",
    primarySession,
    searchRequest(
      "search-d2-aspiration-rollback",
      "persistent-d1-d2",
      2,
      nativeWork,
      credit,
      deadline,
      enumerated.value,
      candidate,
      1,
      "aspiration",
      {
        alpha: depthTwo.value.score - aspirationInitialDelta,
        beta: depthTwo.value.score + aspirationInitialDelta,
        ttPersistence: "rollback",
      },
    ),
  );
  assert.equal(aspirationRollback.status, "unsupported");
  assert.equal(aspirationRollback.error_code, "candidate-task-invalid");

  const selectedCertification = elapsedCall(() => bridge.rootJson(
    "_spc_root_session_search_json",
    primarySession,
    searchRequest(
      "search-selected-certification",
      "persistent-d1-d2",
      2,
      nativeWork,
      credit,
      deadline,
      enumerated.value,
      candidate,
      1,
      "selected-certification",
    ),
  ));
  timings.selectedCertificationMs = selectedCertification.elapsedMs;
  assert.equal(selectedCertification.value.status, "complete");
  assert.equal(selectedCertification.value.bound, "exact");
  assert.deepEqual(
    exactCandidateResult(selectedCertification.value),
    exactCandidateResult(depthTwo.value),
  );
  nativeWork = assertWorkReceipt(selectedCertification.value, nativeWork, credit);
  assertCumulative(aspiration.value.work, selectedCertification.value.work);

  const overDepth = bridge.rootJson(
    "_spc_root_session_search_json",
    primarySession,
    searchRequest(
      "search-over-depth",
      "persistent-d1-d2",
      3,
      nativeWork,
      credit,
      deadline,
      enumerated.value,
      candidate,
      2,
    ),
  );
  assert.equal(overDepth.status, "unsupported");
  assert.equal(overDepth.error_code, "candidate-task-invalid");
  assert.equal(Module._spc_root_session_destroy(primarySession), 1);
  assert.equal(Module._spc_root_session_destroy(primarySession), 0);

  const horizonCreated = bridge.rootJson(
    "_spc_root_session_create_json",
    null,
    {
      ...createRequest("create-horizon", "checked-horizon-research", 1),
      boundary: {
        ...boundary,
        fen: CHECKED_HORIZON_FEN,
      },
    },
  );
  assert.equal(horizonCreated.status, "ready", JSON.stringify(horizonCreated));
  const horizonSession = horizonCreated.session_id;
  const horizonDeadline = Math.ceil(performance.now() + args.timeoutMs);
  const horizonEnumerated = bridge.rootJson(
    "_spc_root_session_enumerate_json",
    horizonSession,
    {
      ...route(
        "spc-root-session-enumerate-v1",
        "enumerate-horizon",
        "checked-horizon-research",
        1,
        0,
        credit,
        horizonDeadline,
      ),
      preferred_series: ["b1b8"],
    },
  );
  assert.equal(horizonEnumerated.status, "complete", JSON.stringify(horizonEnumerated));
  const horizonCandidate = horizonEnumerated.candidates.find(
    (item) => item.order_key === "b1b8",
  );
  assert(horizonCandidate, "preferred checked root candidate was not retained");
  assert.equal(horizonCandidate.root_series.ended_by_check, true);
  const horizonProof = {
    schema: "spc-retained-root-horizon-proof-v1",
    rooted_path: [horizonCandidate.root_series],
    mate_reply: {
      moves: ["g8f7", "a5e1"],
      machine_notation: "g8f7/a5e1",
      transposition_count: 1,
      child_boundary: {
        fen: "1R6/5k2/8/8/8/8/5PPP/4q1K1 w - - 3 3",
        board_fen: "1R6/5k2/8/8/8/8/5PPP/4q1K1 w - - 3 3",
        series: 3,
        series_number: 3,
        side_to_move: "white",
        quiet_series: 2,
        quiet_draw_pending: false,
        ep_targets: [],
        progressive_ep: [],
        promoted_hex: ZERO_PROMOTED,
        chess960: false,
      },
      outcome: "checkmate",
      ended_by_check: true,
    },
  };
  const horizonSearchRequest = (requestId, nativeWork) => ({
    ...searchRequest(
      requestId,
      "checked-horizon-research",
      1,
      nativeWork,
      credit,
      horizonDeadline,
      horizonEnumerated,
      horizonCandidate,
      0,
      "horizon-research",
    ),
    schema: "spc-root-horizon-research-task-v1",
    horizon_proofs: [horizonProof],
  });
  const horizonRepair = bridge.rootJson(
    "_spc_root_session_search_json",
    horizonSession,
    horizonSearchRequest(
      "search-horizon-repair",
      horizonEnumerated.work.native_work_after,
    ),
  );
  assert.equal(horizonRepair.schema, "spc-root-horizon-research-result-v1");
  assert.equal(horizonRepair.status, "complete", JSON.stringify(horizonRepair));
  assert.equal(horizonRepair.bound, "exact");
  assert.equal(horizonRepair.score, -MATE_SCORE + 2);
  assert.equal(horizonRepair.horizon_proofs_validated, 1);
  assert.equal(horizonRepair.horizon_proof_hits, 1);
  assert.equal(horizonRepair.horizon_proof_hit_mask, 1);
  assert.match(horizonRepair.horizon_proof_set_identity, /^spc-horizon-proof-set-v1\|/);
  assert.equal(Module._spc_root_session_destroy(horizonSession), 1);

  const deepProofFor = (rootSeries) => ({
    schema: "spc-retained-root-horizon-proof-v1",
    rooted_path: [
      rootSeries,
      proofSeries(
        ["c2c7", "e7e5"],
        "2k5/2q2r2/8/4p3/3K4/7R/3P2N1/1B6 w - - 0 3",
        3,
        0,
        { endedByCheck: true },
      ),
      proofSeries(
        ["d4e4", "b1a2", "a2e6"],
        "2k5/2q2r2/4B3/4p3/4K3/7R/3P2N1/8 b - - 3 3",
        4,
        1,
        { endedByCheck: true },
      ),
    ],
    mate_reply: proofSeries(
      ["c7d7", "d7e6", "e6d6", "d6d4"],
      "2k5/5r2/8/4p3/3qK3/7R/3P2N1/8 w - - 2 7",
      5,
      0,
      { outcome: "checkmate", endedByCheck: true },
    ),
  });
  const alternateDeepProofFor = (rootSeries) => ({
    schema: "spc-retained-root-horizon-proof-v1",
    rooted_path: [
      rootSeries,
      proofSeries(
        ["c2a2", "a2a1"],
        "2k5/4pr2/8/8/3K4/7R/3P2N1/qB6 w - - 2 3",
        3,
        1,
        { endedByCheck: true },
      ),
      proofSeries(
        ["d4c4", "b1c2", "c2f5"],
        "2k5/4pr2/8/5B2/2K5/7R/3P2N1/q7 b - - 5 3",
        4,
        2,
        { transpositionCount: 9, endedByCheck: true },
      ),
    ],
    mate_reply: proofSeries(
      ["c8b8", "e7e5", "f7b7", "a1d4"],
      "1k6/1r6/8/4pB2/2Kq4/7R/3P2N1/8 w - - 2 7",
      5,
      0,
      { outcome: "checkmate", endedByCheck: true },
    ),
  });
  const prepareDeepHorizonSession = (label) => {
    const iterationId = `checked-horizon-${label}`;
    const createdDeep = bridge.rootJson(
      "_spc_root_session_create_json",
      null,
      {
        ...createRequest(`create-${label}`, iterationId, 1),
        boundary: {
          ...boundary,
          fen: DEEP_HORIZON_FEN,
        },
        config: {
          ...config,
          max_depth: 3,
        },
      },
    );
    assert.equal(createdDeep.status, "ready", JSON.stringify(createdDeep));
    const deadlineDeep = Math.ceil(performance.now() + args.timeoutMs);
    const enumerationDeep = bridge.rootJson(
      "_spc_root_session_enumerate_json",
      createdDeep.session_id,
      {
        ...route(
          "spc-root-session-enumerate-v1",
          `enumerate-${label}`,
          iterationId,
          1,
          0,
          credit,
          deadlineDeep,
        ),
        preferred_series: ["h4g2"],
      },
    );
    assert.equal(enumerationDeep.status, "complete", JSON.stringify(enumerationDeep));
    const candidateDeep = enumerationDeep.candidates.find(
      (item) => item.order_key === "h4g2",
    );
    assert(candidateDeep, "preferred deep checked-horizon candidate was not retained");
    const baselineDeep = bridge.rootJson(
      "_spc_root_session_search_json",
      createdDeep.session_id,
      searchRequest(
        `baseline-${label}`,
        iterationId,
        1,
        enumerationDeep.work.native_work_after,
        credit,
        deadlineDeep,
        enumerationDeep,
        candidateDeep,
        2,
      ),
    );
    assert.equal(baselineDeep.schema, "spc-root-candidate-result-v1");
    assert.equal(baselineDeep.status, "complete", JSON.stringify(baselineDeep));
    assert.equal(baselineDeep.bound, "exact");
    assert.equal(baselineDeep.score, 336);
    assert.deepEqual(
      [baselineDeep.root_series, ...baselineDeep.child_pv].map(
        (item) => item.machine_notation,
      ),
      ["h4g2", "c2c7/e7e5", "d4e4/b1a2/a2e6"],
    );
    return {
      sessionId: createdDeep.session_id,
      iterationId,
      deadline: deadlineDeep,
      enumeration: enumerationDeep,
      candidate: candidateDeep,
      baseline: baselineDeep,
      deepProof: deepProofFor(candidateDeep.root_series),
      alternateProof: alternateDeepProofFor(candidateDeep.root_series),
    };
  };
  const searchDeepHorizon = (
    prepared,
    requestId,
    proofs,
    nativeWork = prepared.baseline.work.native_work_after,
  ) => bridge.rootJson(
    "_spc_root_session_search_json",
    prepared.sessionId,
    {
      ...searchRequest(
        requestId,
        prepared.iterationId,
        1,
        nativeWork,
        credit,
        prepared.deadline,
        prepared.enumeration,
        prepared.candidate,
        2,
        "horizon-research",
      ),
      schema: "spc-root-horizon-research-task-v1",
      horizon_proofs: proofs,
    },
  );

  const deepNewest = prepareDeepHorizonSession("deep-newest");
  const deepNewestProofs = [deepNewest.alternateProof, deepNewest.deepProof];
  const deepNewestRepair = searchDeepHorizon(
    deepNewest,
    "search-deep-newest",
    deepNewestProofs,
  );
  assert.equal(deepNewestRepair.status, "complete", JSON.stringify(deepNewestRepair));
  assert.equal(deepNewestRepair.bound, "exact");
  assert.equal(deepNewestRepair.score, 179);
  assert.equal(deepNewestRepair.horizon_proofs_validated, 2);
  assert.equal(deepNewestRepair.horizon_proof_hits, 1);
  assert.equal(deepNewestRepair.horizon_proof_hit_mask, 0b10);
  assert.deepEqual(
    [deepNewestRepair.root_series, ...deepNewestRepair.child_pv].map(
      (item) => item.machine_notation,
    ),
    ["h4g2", "c2c7/e7e5", "d4d3/d3e2/h3h8"],
  );
  const whiteDeepTwoProof = horizonCaseEvidence({
    result: deepNewestRepair,
    priorSameRoot: deepNewest.baseline,
    proofs: deepNewestProofs,
  });
  assert.equal(whiteDeepTwoProof.disposition, "same-root-repaired");
  const deepWarmExactResult = searchDeepHorizon(
    deepNewest,
    "search-deep-warm-exact",
    deepNewestProofs,
    deepNewestRepair.work.native_work_after,
  );
  const whiteDeepWarmExact = warmHorizonCaseEvidence({
    result: deepWarmExactResult,
    repaired: deepNewestRepair,
    proofs: deepNewestProofs,
  });
  assert.equal(whiteDeepWarmExact.disposition, "warm-exact-recertified");
  assert.equal(Module._spc_root_session_destroy(deepNewest.sessionId), 1);

  const deepReversed = prepareDeepHorizonSession("deep-reversed");
  const deepReversedProofs = [deepReversed.deepProof, deepReversed.alternateProof];
  const deepReversedResult = searchDeepHorizon(
    deepReversed,
    "search-deep-reversed",
    deepReversedProofs,
  );
  assert.equal(deepReversedResult.status, "complete", JSON.stringify(deepReversedResult));
  assert.equal(deepReversedResult.bound, "exact");
  assert.equal(deepReversedResult.score, 179);
  assert.equal(deepReversedResult.horizon_proofs_validated, 2);
  assert.equal(deepReversedResult.horizon_proof_hits, 1);
  assert.equal(deepReversedResult.horizon_proof_hit_mask, 0b01);
  assert.equal(
    deepReversedResult.horizon_proof_set_identity,
    deepNewestRepair.horizon_proof_set_identity,
  );
  const whiteDeepReversedOrder = horizonCaseEvidence({
    result: deepReversedResult,
    priorSameRoot: deepReversed.baseline,
    proofs: deepReversedProofs,
  });
  assert.equal(whiteDeepReversedOrder.disposition, "newest-proof-not-hit");
  assert.equal(Module._spc_root_session_destroy(deepReversed.sessionId), 1);

  const blackIterationId = "checked-horizon-black-parity";
  const blackCreated = bridge.rootJson(
    "_spc_root_session_create_json",
    null,
    {
      ...createRequest("create-horizon-black", blackIterationId, 1),
      boundary: {
        ...boundary,
        fen: BLACK_HORIZON_FEN,
        series: 2,
      },
      config: {
        ...config,
        max_depth: 1,
      },
    },
  );
  assert.equal(blackCreated.status, "ready", JSON.stringify(blackCreated));
  const blackDeadline = Math.ceil(performance.now() + args.timeoutMs);
  const blackEnumerated = bridge.rootJson(
    "_spc_root_session_enumerate_json",
    blackCreated.session_id,
    {
      ...route(
        "spc-root-session-enumerate-v1",
        "enumerate-horizon-black",
        blackIterationId,
        1,
        0,
        credit,
        blackDeadline,
      ),
      preferred_series: ["f7f5", "b8b1"],
    },
  );
  assert.equal(blackEnumerated.status, "complete", JSON.stringify(blackEnumerated));
  const blackCandidate = blackEnumerated.candidates.find(
    (item) => item.order_key === "f7f5/b8b1",
  );
  assert(blackCandidate, "preferred Black checked-horizon candidate was not retained");
  const blackBaseline = bridge.rootJson(
    "_spc_root_session_search_json",
    blackCreated.session_id,
    searchRequest(
      "baseline-horizon-black",
      blackIterationId,
      1,
      blackEnumerated.work.native_work_after,
      credit,
      blackDeadline,
      blackEnumerated,
      blackCandidate,
      0,
    ),
  );
  assert.equal(blackBaseline.schema, "spc-root-candidate-result-v1");
  assert.equal(blackBaseline.status, "complete", JSON.stringify(blackBaseline));
  assert.equal(blackBaseline.bound, "exact");
  assert.equal(blackBaseline.score, -235);
  const blackProof = {
    schema: "spc-retained-root-horizon-proof-v1",
    rooted_path: [blackCandidate.root_series],
    mate_reply: proofSeries(
      ["g1f2", "a4e8"],
      "4Q1k1/6pp/8/5p2/8/8/5K2/1r6 b - - 3 3",
      4,
      1,
      { outcome: "checkmate", endedByCheck: true },
    ),
  };
  const blackRepair = bridge.rootJson(
    "_spc_root_session_search_json",
    blackCreated.session_id,
    {
      ...searchRequest(
        "search-horizon-black",
        blackIterationId,
        1,
        blackBaseline.work.native_work_after,
        credit,
        blackDeadline,
        blackEnumerated,
        blackCandidate,
        0,
        "horizon-research",
      ),
      schema: "spc-root-horizon-research-task-v1",
      horizon_proofs: [blackProof],
    },
  );
  assert.equal(blackRepair.status, "complete", JSON.stringify(blackRepair));
  assert.equal(blackRepair.bound, "exact");
  assert.equal(blackRepair.score, MATE_SCORE - 2);
  assert.deepEqual(blackRepair.proof_bounds, [1, 1]);
  assert.equal(blackRepair.horizon_proofs_validated, 1);
  assert.equal(blackRepair.horizon_proof_hits, 1);
  assert.equal(blackRepair.horizon_proof_hit_mask, 0b1);
  const blackParity = horizonCaseEvidence({
    result: blackRepair,
    priorSameRoot: blackBaseline,
    proofs: [blackProof],
  });
  assert.equal(blackParity.disposition, "same-root-repaired");
  assert.equal(Module._spc_root_session_destroy(blackCreated.session_id), 1);

  const checkedHorizonProofResearch = {
    schema: "spc-checked-horizon-wasm-evidence-v1",
    white_deep_two_proof: whiteDeepTwoProof,
    white_deep_warm_exact: whiteDeepWarmExact,
    white_deep_reversed_order: whiteDeepReversedOrder,
    black_parity: blackParity,
  };
  const checkedHorizonProofResearchGate =
    whiteDeepTwoProof.disposition === "same-root-repaired"
    && whiteDeepTwoProof.horizon_proof_hit_mask === 0b10
    && whiteDeepReversedOrder.disposition === "newest-proof-not-hit"
    && whiteDeepReversedOrder.horizon_proof_hit_mask === 0b01
    && whiteDeepWarmExact.disposition === "warm-exact-recertified"
    && whiteDeepWarmExact.horizon_proof_hits === 0
    && whiteDeepWarmExact.horizon_proof_hit_mask === 0
    && whiteDeepWarmExact.exact_tt_hits > 0
    && whiteDeepWarmExact.horizon_proof_set_identity_sha256
      === whiteDeepTwoProof.horizon_proof_set_identity_sha256
    && whiteDeepReversedOrder.horizon_proof_set_identity_sha256
      === whiteDeepTwoProof.horizon_proof_set_identity_sha256
    && blackParity.disposition === "same-root-repaired"
    && blackParity.root_side === "black"
    && blackParity.horizon_proof_hit_mask === 0b1;
  assert.equal(checkedHorizonProofResearchGate, true);

  const legacyPolicy = bridge.rootJson(
    "_spc_root_session_create_json",
    null,
    {
      ...createRequest("create-legacy-policy", "legacy-policy", 3),
      config: { ...config, root_tactical_protection: true },
    },
  );
  assert.equal(legacyPolicy.status, "unsupported");
  assert.equal(legacyPolicy.error_code, "legacy-root-tactical-policy-unsupported");

  for (const [name, tacticalBoundary] of [
    ["late", { ...boundary, series: 5 }],
    ["promotion", {
      ...boundary,
      fen: "7k/4P3/8/8/8/8/8/K7 w - - 0 1",
      series: 3,
    }],
  ]) {
    const policyCreated = bridge.rootJson(
      "_spc_root_session_create_json",
      null,
      {
        ...createRequest(`create-${name}-policy`, `${name}-policy`, 3),
        boundary: tacticalBoundary,
      },
    );
    assert.equal(policyCreated.status, "ready");
    assert.equal(policyCreated.canonical_root_tactical_protection, true);
    assert.equal(Module._spc_root_session_destroy(policyCreated.session_id), 1);
  }

  const importCreated = bridge.rootJson(
    "_spc_root_session_create_json",
    null,
    createRequest("create-import", "exact-import", 4),
  );
  assert.equal(importCreated.status, "ready");
  const importSession = importCreated.session_id;
  const importDeadline = Math.ceil(performance.now() + args.timeoutMs);
  const imported = elapsedCall(() => bridge.rootJson(
    "_spc_root_session_import_json",
    importSession,
    {
      ...route(
        "spc-root-session-import-v1",
        "import-exact",
        "exact-import",
        4,
        0,
        credit,
        importDeadline,
      ),
      manifest: manifestFrom(enumerated.value),
      external_work: enumerated.value.work.native_work_after,
    },
  ));
  timings.importExactMs = imported.elapsedMs;
  assert.equal(imported.value.status, "complete");
  assert.equal(imported.value.imported, true);
  assert.equal(imported.value.enumeration_identity, enumerated.value.enumeration_identity);
  assert.deepEqual(imported.value.candidates, enumerated.value.candidates);
  let importedWork = assertWorkReceipt(imported.value, 0, credit);
  const importedDepthTwo = elapsedCall(() => bridge.rootJson(
    "_spc_root_session_search_json",
    importSession,
    {
      ...searchRequest(
        "search-imported-d2",
        "exact-import",
        4,
        importedWork,
        credit,
        importDeadline,
        imported.value,
        imported.value.candidates[0],
        1,
      ),
      external_work: enumerated.value.work.native_work_after,
    },
  ));
  timings.searchImportedD2Ms = importedDepthTwo.elapsedMs;
  assert.equal(importedDepthTwo.value.status, "complete");
  importedWork = assertWorkReceipt(importedDepthTwo.value, importedWork, credit);
  assert.deepEqual(
    exactCandidateResult(importedDepthTwo.value),
    exactCandidateResult(depthTwo.value),
  );
  assert.equal(Module._spc_root_session_destroy(importSession), 1);

  const limitedCreated = bridge.rootJson(
    "_spc_root_session_create_json",
    null,
    createRequest("create-work-limit", "work-limit", 5),
  );
  const limitedDeadline = Math.ceil(performance.now() + args.timeoutMs);
  const limited = bridge.rootJson(
    "_spc_root_session_enumerate_json",
    limitedCreated.session_id,
    {
      ...route(
        "spc-root-session-enumerate-v1",
        "enumerate-work-limit",
        "work-limit",
        5,
        0,
        0,
        limitedDeadline,
      ),
      preferred_series: [],
    },
  );
  assert.equal(limited.status, "work_limit");
  assertWorkReceipt(limited, 0, 0);
  assert.equal(Module._spc_root_session_destroy(limitedCreated.session_id), 1);

  const deadlineCreated = bridge.rootJson(
    "_spc_root_session_create_json",
    null,
    createRequest("create-deadline", "deadline", 6),
  );
  const deadlineNow = performance.now();
  const timedOut = bridge.rootJson(
    "_spc_root_session_enumerate_json",
    deadlineCreated.session_id,
    {
      ...route(
        "spc-root-session-enumerate-v1",
        "enumerate-deadline",
        "deadline",
        6,
        0,
        credit,
        deadlineNow,
      ),
      remaining_time_ms: 0,
      preferred_series: [],
    },
  );
  assert.equal(timedOut.status, "deadline");
  assertWorkReceipt(timedOut, 0, credit);
  assert.equal(Module._spc_root_session_destroy(deadlineCreated.session_id), 1);

  const highSeriesBoundary = {
    fen: HIGH_SERIES_FEN,
    series: 24,
    quiet_series: 4,
    ep_targets: [],
    promoted_hex: ZERO_PROMOTED,
    chess960: false,
  };
  const highSeriesConfig = {
    ...config,
    width: 32,
    max_work: HIGH_SERIES_MAX_WORK,
  };
  const highSeriesCreated = elapsedCall(() => bridge.rootJson(
    "_spc_root_session_create_json",
    null,
    {
      ...createRequest("create-high-series", "high-series-json-safety", 7),
      boundary: highSeriesBoundary,
      config: highSeriesConfig,
    },
  ));
  timings.createHighSeriesMs = highSeriesCreated.elapsedMs;
  assert.equal(highSeriesCreated.value.status, "ready", JSON.stringify(highSeriesCreated.value));
  assertSafeJsonIntegers(highSeriesCreated.value, "$.high_series_create");
  const highSeriesSession = highSeriesCreated.value.session_id;
  const highSeriesDeadline = Math.ceil(performance.now() + args.timeoutMs);
  const highSeriesEnumerated = elapsedCall(() => bridge.rootJson(
    "_spc_root_session_enumerate_json",
    highSeriesSession,
    {
      ...route(
        "spc-root-session-enumerate-v1",
        "enumerate-high-series",
        "high-series-json-safety",
        7,
        0,
        HIGH_SERIES_MAX_WORK,
        highSeriesDeadline,
      ),
      preferred_series: [],
    },
  ));
  timings.enumerateHighSeriesMs = highSeriesEnumerated.elapsedMs;
  assert.equal(
    highSeriesEnumerated.value.status,
    "complete",
    JSON.stringify(highSeriesEnumerated.value),
  );
  assert.equal(highSeriesEnumerated.value.retained_count, 32);
  assertSafeJsonIntegers(highSeriesEnumerated.value, "$.high_series_enumerate");
  const highSeriesCounts = highSeriesEnumerated.value.candidates.map(
    (item) => item.root_series.transposition_count,
  );
  assert.ok(highSeriesCounts.every((count) => count >= 1));
  assert.equal(
    Math.max(...highSeriesCounts),
    Number.MAX_SAFE_INTEGER,
    "Series-24 fixture did not exercise the JavaScript-safe count ceiling",
  );
  const highSeriesCandidate = highSeriesEnumerated.value.candidates.find(
    (item) => item.root_series.machine_notation === HIGH_SERIES_ROOT,
  );
  assert.ok(highSeriesCandidate, "frozen Series-24 oracle root was not retained");
  let highSeriesNativeWork = assertWorkReceipt(
    highSeriesEnumerated.value,
    0,
    HIGH_SERIES_MAX_WORK,
  );
  const highSeriesSearched = elapsedCall(() => bridge.rootJson(
    "_spc_root_session_search_json",
    highSeriesSession,
    searchRequest(
      "search-high-series-d2",
      "high-series-json-safety",
      7,
      highSeriesNativeWork,
      HIGH_SERIES_MAX_WORK,
      highSeriesDeadline,
      highSeriesEnumerated.value,
      highSeriesCandidate,
      1,
    ),
  ));
  timings.searchHighSeriesD2Ms = highSeriesSearched.elapsedMs;
  assert.equal(
    highSeriesSearched.value.status,
    "complete",
    JSON.stringify(highSeriesSearched.value),
  );
  assert.equal(highSeriesSearched.value.bound, "exact");
  assert.equal(highSeriesSearched.value.score, 0);
  assert.equal(highSeriesSearched.value.root_series.machine_notation, HIGH_SERIES_ROOT);
  assert.equal(highSeriesSearched.value.child_pv[0]?.machine_notation, HIGH_SERIES_CHILD);
  assertSafeJsonIntegers(highSeriesSearched.value, "$.high_series_search");
  highSeriesNativeWork = assertWorkReceipt(
    highSeriesSearched.value,
    highSeriesNativeWork,
    HIGH_SERIES_MAX_WORK,
  );
  assertCumulative(highSeriesEnumerated.value.work, highSeriesSearched.value.work);
  const highSeriesManifest = manifestFrom(highSeriesEnumerated.value);
  assert.equal(Module._spc_root_session_destroy(highSeriesSession), 1);

  const highSeriesImportCreated = bridge.rootJson(
    "_spc_root_session_create_json",
    null,
    {
      ...createRequest("create-high-series-import", "high-series-import-safety", 8),
      boundary: highSeriesBoundary,
      config: highSeriesConfig,
    },
  );
  assert.equal(highSeriesImportCreated.status, "ready", JSON.stringify(highSeriesImportCreated));
  assertSafeJsonIntegers(highSeriesImportCreated, "$.high_series_import_create");
  const highSeriesImportSession = highSeriesImportCreated.session_id;
  const highSeriesImportDeadline = Math.ceil(performance.now() + args.timeoutMs);
  const highSeriesImported = elapsedCall(() => bridge.rootJson(
    "_spc_root_session_import_json",
    highSeriesImportSession,
    {
      ...route(
        "spc-root-session-import-v1",
        "import-high-series",
        "high-series-import-safety",
        8,
        0,
        HIGH_SERIES_MAX_WORK,
        highSeriesImportDeadline,
      ),
      external_work: highSeriesEnumerated.value.work.native_work_after,
      manifest: highSeriesManifest,
    },
  ));
  timings.importHighSeriesMs = highSeriesImported.elapsedMs;
  assert.equal(
    highSeriesImported.value.status,
    "complete",
    JSON.stringify(highSeriesImported.value),
  );
  assert.deepEqual(highSeriesImported.value.candidates, highSeriesEnumerated.value.candidates);
  assertSafeJsonIntegers(highSeriesImported.value, "$.high_series_import");
  const highSeriesImportNativeWork = assertWorkReceipt(
    highSeriesImported.value,
    0,
    HIGH_SERIES_MAX_WORK,
  );

  const unsafeHighSeriesManifest = structuredClone(highSeriesManifest);
  unsafeHighSeriesManifest.candidates[0].root_series.transposition_count =
    Number.MAX_SAFE_INTEGER + 1;
  const unsafeHighSeriesImport = bridge.rootJson(
    "_spc_root_session_import_json",
    highSeriesImportSession,
    {
      ...route(
        "spc-root-session-import-v1",
        "import-high-series-unsafe-count",
        "high-series-import-safety",
        8,
        highSeriesImportNativeWork,
        HIGH_SERIES_MAX_WORK,
        highSeriesImportDeadline,
      ),
      external_work: highSeriesEnumerated.value.work.native_work_after,
      manifest: unsafeHighSeriesManifest,
    },
  );
  assert.equal(unsafeHighSeriesImport.status, "unsupported");
  assert.equal(unsafeHighSeriesImport.error_code, "request-field-invalid");
  assert.match(
    unsafeHighSeriesImport.message,
    /transposition_count is outside its exact integer envelope/,
  );
  assertSafeJsonIntegers(unsafeHighSeriesImport, "$.high_series_unsafe_import_reply");
  assert.equal(Module._spc_root_session_destroy(highSeriesImportSession), 1);

  const prefix = bridge.prefixJson(START_FEN, 1, 0, "-", ZERO_PROMOTED, "");
  assert.equal(prefix.schema, "spc-boundary-prefix-v1");
  assert.equal(prefix.ok, true);
  assert.equal(prefix.status, "complete");
  assert.equal(prefix.boundary_state.promoted_hex, ZERO_PROMOTED);

  const mateFound = elapsedCall(() => bridge.mateJson(
    LIVE_S5,
    5,
    "-",
    ZERO_PROMOTED,
    0,
    1_000_000,
    args.timeoutMs,
  ));
  timings.mateFoundMs = mateFound.elapsedMs;
  assert.equal(mateFound.value.proof_status, "found");
  assert.equal(mateFound.value.complete, true);
  assert.deepEqual(
    mateFound.value.moves,
    ["c3d5", "d3e4", "e4h7", "d5f4", "h7g6"],
  );
  const mateExhausted = bridge.mateJson(
    BARE_KINGS,
    3,
    "-",
    ZERO_PROMOTED,
    0,
    1_000_000,
    args.timeoutMs,
  );
  assert.equal(mateExhausted.proof_status, "exhausted");
  assert.equal(mateExhausted.complete, true);
  const mateWorkLimit = bridge.mateJson(
    LIVE_S5,
    5,
    "-",
    ZERO_PROMOTED,
    0,
    100,
    args.timeoutMs,
  );
  assert.equal(mateWorkLimit.kernel_status, "work_limit");
  assert.equal(mateWorkLimit.proof_status, "unknown");
  assert.equal(mateWorkLimit.complete, false);
  assert.ok(
    mateWorkLimit.stats.positions_visited + mateWorkLimit.stats.moves_generated <= 100,
  );
  const mateDeadline = bridge.mateJson(
    START_BLACK_FEN,
    8,
    "-",
    ZERO_PROMOTED,
    0,
    10_000_000,
    1,
  );
  assert.equal(mateDeadline.kernel_status, "deadline");
  assert.equal(mateDeadline.proof_status, "unknown");
  assert.equal(mateDeadline.complete, false);

  const receipt = {
    schema: "spc-root-session-wasm-smoke-v1",
    status: "passed-not-certified",
    product_publishable: false,
    safety_certified: false,
    certificate_id: null,
    source_revision: buildReceipt.source_revision,
    source_fingerprint: buildReceipt.source_fingerprint,
    kernel_sha256: buildReceipt.kernel_sha256,
    wasm_sha256: buildReceipt.wasm_sha256,
    module_js_sha256: buildReceipt.module_js_sha256,
    artifact_set_sha256: buildReceipt.artifact_set_sha256,
    exception_strategy: buildReceipt.optimization.exception_strategy,
    wasm_simd: buildReceipt.optimization.wasm_simd,
    allocator: buildReceipt.optimization.allocator,
    runtime_requirements: buildReceipt.runtime_requirements,
    runtime_variant: "single",
    thread_count: 1,
    memory: {
      configured: buildReceipt.memory_envelope,
      observed_bytes: Module.HEAPU8.buffer.byteLength,
      native_peak_bytes: Math.max(
        depthTwo.value.memory_peak_bytes,
        aspiration.value.memory_peak_bytes,
        aspirationFailSoft.white.memory_peak_bytes,
        aspirationFailSoft.black.memory_peak_bytes,
        importedDepthTwo.value.memory_peak_bytes,
        horizonRepair.memory_peak_bytes,
        deepNewestRepair.memory_peak_bytes,
        deepWarmExactResult.memory_peak_bytes,
        deepReversedResult.memory_peak_bytes,
        blackRepair.memory_peak_bytes,
        timedOut.memory_peak_bytes,
        highSeriesSearched.value.memory_peak_bytes,
        highSeriesImported.value.memory_peak_bytes,
        unsafeHighSeriesImport.memory_peak_bytes,
      ),
    },
    config,
    timings_ms: timings,
    gates: {
      combined_exports: true,
      root_contract_reply_mate_safety_false: true,
      persistent_d1_d2_session: true,
      aspiration_fail_soft_window: true,
      aspiration_fail_high_low_white_black: true,
      selected_owner_warm_exact_certification: true,
      checked_horizon_proof_research: checkedHorizonProofResearchGate,
      checked_horizon_newest_proof_hit: checkedHorizonProofResearchGate,
      cumulative_work_and_cache_receipts: true,
      exact_manifest_import: true,
      configured_max_depth_rejected: true,
      work_limit_fail_closed: true,
      deadline_fail_closed: true,
      prefix_smoke: true,
      mate_found_exhausted_unknown: true,
      canonical_root_tactical_policy: true,
      legacy_root_tactical_policy_rejected: true,
      canonical_root_tactical_boundary_echoes: true,
      high_series_json_number_safety: true,
      high_series_safe_manifest_import: true,
      unsafe_transposition_count_rejected: true,
      mate_python_parity: false,
      browser_worker_smoke: false,
      opera_worker_smoke: false,
      w32_d5_under_60_seconds: false,
    },
    persistent_results: {
      d1: exactCandidateResult(depthOne.value),
      d2: exactCandidateResult(depthTwo.value),
      aspiration: exactCandidateResult(aspiration.value),
      aspiration_fail_soft: aspirationFailSoft,
      selected_certification: exactCandidateResult(selectedCertification.value),
      horizon_repair: exactCandidateResult(horizonRepair),
      horizon_warm_exact: exactCandidateResult(deepWarmExactResult),
      horizon_proof_set_identity_sha256:
        whiteDeepWarmExact.horizon_proof_set_identity_sha256,
      imported_d2: exactCandidateResult(importedDepthTwo.value),
      native_work_after: nativeWork,
      imported_native_work_after: importedWork,
      high_series_d2: exactCandidateResult(highSeriesSearched.value),
      high_series_native_work_after: highSeriesNativeWork,
      high_series_max_transposition_count: Math.max(...highSeriesCounts),
      high_series_import_native_work_after: highSeriesImportNativeWork,
      high_series_unsafe_import_error: unsafeHighSeriesImport.error_code,
    },
    checked_horizon_proof_research: checkedHorizonProofResearch,
    mate_receipts: {
      found: mateFound.value,
      exhausted: mateExhausted,
      work_limit: mateWorkLimit,
      deadline: mateDeadline,
    },
    root_session_contract: contract,
    prefix_contract: prefixContract,
  };
  await mkdir(dirname(args.output), { recursive: true });
  await writeFile(args.output, `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify(receipt)}\n`);
}


await main();
