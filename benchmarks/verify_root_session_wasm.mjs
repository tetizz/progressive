import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { performance } from "node:perf_hooks";
import { pathToFileURL } from "node:url";


const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const START_BLACK_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1";
const LIVE_S5 = "rn1q1bnr/ppp1pkpp/5p2/8/3Pp3/2NB4/PPP2PPP/R1BbK1NR w KQ - 0 7";
const BARE_KINGS = "8/8/8/8/8/2k5/8/K7 w - - 0 1";
const ZERO_PROMOTED = "0000000000000000";
const MATE_SCORE = 1_000_000;
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
  assert.equal(contract.capabilities.selected_owner_certification, true);
  assert.equal(contract.capabilities.canonical_root_tactical_policy, true);
  assert.equal(contract.hard_limits.root_tactical_policy, "canonical-boundary-policy-v1");
  assert.deepEqual(contract.hard_limits.root_tactical_protection_values, [false]);
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
    alpha: -2 * MATE_SCORE,
    beta: 2 * MATE_SCORE,
    tt_persistence: "commit",
    mover: enumeration.root_white_to_move ? "white" : "black",
  });

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
  assert.equal(created.value.capabilities.canonical_root_tactical_policy, true);
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
  assertCumulative(depthTwo.value.work, selectedCertification.value.work);

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
        importedDepthTwo.value.memory_peak_bytes,
        timedOut.memory_peak_bytes,
      ),
    },
    config,
    timings_ms: timings,
    gates: {
      combined_exports: true,
      root_contract_reply_mate_safety_false: true,
      persistent_d1_d2_session: true,
      selected_owner_warm_exact_certification: true,
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
      mate_python_parity: false,
      browser_worker_smoke: false,
      opera_worker_smoke: false,
      w32_d5_under_60_seconds: false,
    },
    persistent_results: {
      d1: exactCandidateResult(depthOne.value),
      d2: exactCandidateResult(depthTwo.value),
      selected_certification: exactCandidateResult(selectedCertification.value),
      imported_d2: exactCandidateResult(importedDepthTwo.value),
      native_work_after: nativeWork,
      imported_native_work_after: importedWork,
    },
    mate_receipts: {
      found: mateFound.value,
      exhausted: mateExhausted,
      work_limit: mateWorkLimit,
      deadline: mateDeadline,
    },
    root_session_contract: contract,
    prefix_contract: prefixContract,
  };
  await writeFile(args.output, `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify(receipt)}\n`);
}


await main();
