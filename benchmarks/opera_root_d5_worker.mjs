const MATE_SCORE = 1_000_000;


function invariant(value, message) {
  if (!value) throw new Error(message);
}


async function sha256(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}


function canonicalBoundary(value) {
  return {
    fen: value?.fen,
    board_fen: value?.board_fen ?? value?.fen,
    series: Number(value?.series ?? value?.series_number),
    series_number: Number(value?.series_number ?? value?.series),
    side_to_move: value?.side_to_move,
    quiet_series: Number(value?.quiet_series),
    quiet_draw_pending: value?.quiet_draw_pending === true,
    ep_targets: Array.isArray(value?.ep_targets) ? [...value.ep_targets] : null,
    progressive_ep: Array.isArray(value?.progressive_ep)
      ? [...value.progressive_ep]
      : Array.isArray(value?.ep_targets) ? [...value.ep_targets] : null,
    promoted_hex: String(value?.promoted_hex || "").toLowerCase().replace(/^0x/, "")
      .padStart(16, "0"),
    chess960: value?.chess960,
  };
}


function sameBoundary(left, right) {
  return JSON.stringify(canonicalBoundary(left)) === JSON.stringify(canonicalBoundary(right));
}


let Module = null;
let sessionId = null;
let identity = null;
let boundary = null;
let buildReceipt = null;
let rootContract = null;
let prefixContract = null;
let memoryPeakBytes = 0;
const encoder = new TextEncoder();


function memoryReceipt() {
  const memoryBytes = Module?.HEAPU8?.buffer?.byteLength ?? 0;
  memoryPeakBytes = Math.max(memoryPeakBytes, memoryBytes);
  return {
    memory_bytes: memoryBytes,
    memory_peak_bytes: memoryPeakBytes,
  };
}


function withCString(value, callback) {
  const bytes = encoder.encode(String(value));
  const pointer = Module._malloc(bytes.length + 1);
  invariant(pointer !== 0, "WASM string allocation failed");
  Module.HEAPU8.set(bytes, pointer);
  Module.HEAPU8[pointer + bytes.length] = 0;
  try {
    return callback(pointer);
  } finally {
    Module._free(pointer);
  }
}


function rootJson(exportName, activeSession, request) {
  const bytes = encoder.encode(JSON.stringify(request));
  const pointer = Module._malloc(bytes.length);
  invariant(pointer !== 0, "WASM root request allocation failed");
  Module.HEAPU8.set(bytes, pointer);
  try {
    const resultPointer = activeSession === null
      ? Module[exportName](pointer, bytes.length)
      : Module[exportName](activeSession, pointer, bytes.length);
    invariant(resultPointer !== 0, `${exportName} returned null`);
    const result = JSON.parse(Module.UTF8ToString(resultPointer));
    return { ...result, ...memoryReceipt() };
  } finally {
    Module._free(pointer);
  }
}


function prefixFacade(request) {
  const state = request.boundary;
  const ep = state.ep_targets.join(",") || "-";
  const prefix = request.prefix.join("/");
  const raw = withCString(state.fen, (fenPointer) => withCString(ep, (epPointer) => (
    withCString(state.promoted_hex, (promotedPointer) => withCString(prefix, (prefixPointer) => {
      const pointer = Module._spc_boundary_prefix_json(
        fenPointer,
        state.series,
        state.quiet_series,
        epPointer,
        promotedPointer,
        prefixPointer,
      );
      invariant(pointer !== 0, "prefix ABI returned null");
      return JSON.parse(Module.UTF8ToString(pointer));
    }))
  )));
  return {
    ...raw,
    request_id: request.request_id,
    source_fingerprint: identity.source_fingerprint,
    wasm_sha256: buildReceipt.wasm_sha256,
    module_js_sha256: identity.module_js_sha256,
    certificate_id: `${identity.certificate_id}:prefix`,
    engine_version: identity.engine_version,
    ruleset_version: identity.ruleset_version,
    runtime_variant: identity.runtime_variant,
    thread_count: identity.thread_count,
    ...memoryReceipt(),
  };
}


function mateFacade(child, maxWork, timeMs) {
  const ep = child.ep_targets.join(",") || "-";
  return withCString(child.fen, (fenPointer) => withCString(ep, (epPointer) => (
    withCString(child.promoted_hex, (promotedPointer) => {
      const pointer = Module._spc_series_mate_search_json(
        fenPointer,
        child.series,
        epPointer,
        promotedPointer,
        0,
        maxWork,
        timeMs,
      );
      invariant(pointer !== 0, "mate ABI returned null");
      return JSON.parse(Module.UTF8ToString(pointer));
    })
  )));
}


async function initialize(payload) {
  invariant(Module === null && sessionId === null, "Worker initialized twice");
  const [moduleResponse, wasmResponse, receiptResponse] = await Promise.all([
    fetch(payload.moduleUrl, { cache: "no-store" }),
    fetch(payload.wasmUrl, { cache: "no-store" }),
    fetch(payload.buildReceiptUrl, { cache: "no-store" }),
  ]);
  invariant(moduleResponse.ok && wasmResponse.ok && receiptResponse.ok, "artifact fetch failed");
  const [moduleBytes, wasmBytes, receipt] = await Promise.all([
    moduleResponse.arrayBuffer(),
    wasmResponse.arrayBuffer(),
    receiptResponse.json(),
  ]);
  const [moduleHash, wasmHash] = await Promise.all([
    sha256(moduleBytes),
    sha256(wasmBytes),
  ]);
  invariant(moduleHash === receipt.module_js_sha256, "module JS hash mismatch");
  invariant(wasmHash === receipt.wasm_sha256, "WASM hash mismatch");
  invariant(receipt.status === "built-not-certified", "artifact receipt is not lab-only");
  invariant(receipt.product_publishable === false, "artifact receipt claims publishability");
  invariant(receipt.runtime_variant === "single", "artifact is not an ordinary Worker build");
  invariant(receipt.thread_count === 1 && receipt.pthreads === false, "artifact uses pthreads");
  invariant(
    Object.entries(payload.identity).every(([key, value]) => (
      key === "certificate_id" || receipt[key] === undefined || receipt[key] === value
    )),
    "requested artifact identity does not match the build receipt",
  );

  const moduleBlob = URL.createObjectURL(new Blob([moduleBytes], { type: "text/javascript" }));
  const instantiateStarted = performance.now();
  try {
    const factory = (await import(moduleBlob)).default;
    invariant(typeof factory === "function", "ES-module factory is missing");
    Module = await factory({
      wasmBinary: new Uint8Array(wasmBytes),
      locateFile: (path) => path.endsWith(".wasm") ? payload.wasmUrl : path,
    });
  } finally {
    URL.revokeObjectURL(moduleBlob);
  }
  invariant(Module._spc_root_session_abi_version() === 2, "root-session ABI mismatch");
  invariant(Module._spc_start_kernel_abi_version() === 1, "prefix ABI mismatch");
  invariant(Module._spc_series_mate_abi_version() === 1, "mate ABI mismatch");
  rootContract = JSON.parse(Module.UTF8ToString(Module._spc_root_session_contract_json()));
  prefixContract = JSON.parse(Module.UTF8ToString(Module._spc_boundary_prefix_contract_json()));
  invariant(rootContract.worker_threads === 1, "root contract is not single-threaded");
  invariant(rootContract.pthreads_required === false, "root contract requires pthreads");
  invariant(rootContract.reply_mate_safety === false, "root contract overclaims mate safety");
  invariant(
    rootContract.capabilities?.selected_owner_certification === true,
    "selected-owner certification capability is missing",
  );
  invariant(
    rootContract.capabilities?.canonical_root_tactical_policy === true,
    "canonical root tactical policy capability is missing",
  );
  invariant(
    rootContract.hard_limits?.root_tactical_policy === "canonical-boundary-policy-v1",
    "canonical root tactical policy contract drifted",
  );
  invariant(
    JSON.stringify(rootContract.hard_limits?.root_tactical_protection_values) === "[false]",
    "legacy root tactical configuration is still accepted",
  );
  buildReceipt = receipt;
  identity = Object.freeze({ ...payload.identity });
  boundary = Object.freeze({ ...payload.boundary });
  const created = rootJson("_spc_root_session_create_json", null, {
    schema: "spc-root-session-create-v1",
    request_id: `${payload.workerId}:create`,
    iteration_id: payload.runId,
    generation: 0,
    ...identity,
    boundary,
    config: payload.config,
  });
  invariant(created.status === "ready", `root session create failed: ${JSON.stringify(created)}`);
  invariant(created.native_work_after === 0, "new root session has nonzero work");
  invariant(JSON.stringify(created.config) === JSON.stringify(payload.config), "root config echo drifted");
  invariant(
    created.canonical_root_tactical_policy === "canonical-boundary-policy-v1",
    "create response canonical root policy drifted",
  );
  invariant(
    created.canonical_root_tactical_protection === false,
    "starting boundary did not derive canonical root protection=false",
  );
  sessionId = created.session_id;
  return {
    status: "ready",
    worker_id: payload.workerId,
    session_id: sessionId,
    identity,
    instantiate_ms: performance.now() - instantiateStarted,
    native_work_after: 0,
    build: {
      wasm_sha256: wasmHash,
      module_js_sha256: moduleHash,
      artifact_set_sha256: receipt.artifact_set_sha256,
      exception_strategy: receipt.optimization.exception_strategy,
      wasm_simd: receipt.optimization.wasm_simd,
      allocator: receipt.optimization.allocator,
    },
    root_contract: rootContract,
    prefix_contract: prefixContract,
    canonical_root_tactical_policy: created.canonical_root_tactical_policy,
    canonical_root_tactical_protection: created.canonical_root_tactical_protection,
    environment: {
      ordinary_module_worker: true,
      worker_global_scope: self.constructor?.name ?? "unknown",
      cross_origin_isolated: self.crossOriginIsolated === true,
      hardware_concurrency: self.navigator?.hardwareConcurrency ?? null,
    },
    ...memoryReceipt(),
  };
}


function runSafety(payload) {
  invariant(sessionId !== null, "safety requested without a root session");
  const { task, childBoundary } = payload;
  invariant(task?.schema === "spc-root-safety-task-v1", "invalid safety task schema");
  invariant(Number.isSafeInteger(task.call_work_credit), "invalid mate work credit");
  invariant(task.call_work_credit >= 1 && task.call_work_credit <= 0xffffffff, "mate work credit is out of ABI range");
  invariant(Number.isInteger(payload.remainingTimeMs) && payload.remainingTimeMs >= 0, "invalid mate deadline");
  const mate = mateFacade(childBoundary, task.call_work_credit, payload.remainingTimeMs);
  const workUsed = Number(mate?.stats?.positions_visited) + Number(mate?.stats?.moves_generated);
  invariant(Number.isSafeInteger(workUsed) && workUsed >= 0, "mate work receipt is invalid");
  invariant(workUsed <= task.call_work_credit, "mate solver exceeded its reserved work");
  const common = {
    ...task,
    work_used: workUsed,
    mate_receipt: mate,
    ...memoryReceipt(),
  };
  if (mate.kernel_status === "exhausted" && mate.complete === true) {
    invariant(mate.moves.length === 0, "exhausted mate receipt carried a line");
    return { ...common, status: "exhausted" };
  }
  if (mate.kernel_status !== "found" || mate.complete !== true) {
    return { ...common, status: "unknown" };
  }
  invariant(Array.isArray(mate.moves) && mate.moves.length > 0, "found mate has no line");
  const replayRequest = {
    contract_version: 1,
    request_id: `${task.iteration_id}:mate:${task.safety_revision}`,
    operation: "prefix-replay",
    boundary: childBoundary,
    prefix: mate.moves,
  };
  const checkedPrefix = prefixFacade(replayRequest);
  invariant(checkedPrefix.complete === true, "mate replay did not complete");
  invariant(checkedPrefix.outcome === "checkmate", "mate replay was not checkmate");
  invariant(checkedPrefix.ended_by_check === true, "mate replay did not end by check");
  invariant(
    JSON.stringify(checkedPrefix.prefix) === JSON.stringify(mate.moves),
    "mate replay line drifted",
  );
  const childWhite = childBoundary.side_to_move === "white";
  return {
    ...common,
    status: "found",
    override_score: childWhite ? MATE_SCORE - 2 : -MATE_SCORE + 2,
    proof_bounds: childWhite ? [1, 1] : [-1, -1],
    reply_mate: {
      moves: [...mate.moves],
      machine_notation: mate.moves.join("/"),
      outcome: "checkmate",
      ended_by_check: true,
      checked_prefix: checkedPrefix,
    },
  };
}


async function dispatch(type, payload) {
  if (type === "initialize") return initialize(payload);
  invariant(Module !== null && sessionId !== null, "Worker has no live root session");
  if (type === "enumerate") {
    return rootJson("_spc_root_session_enumerate_json", sessionId, payload);
  }
  if (type === "import") {
    return rootJson("_spc_root_session_import_json", sessionId, payload);
  }
  if (type === "search") {
    return rootJson("_spc_root_session_search_json", sessionId, payload);
  }
  if (type === "prefix") return prefixFacade(payload);
  if (type === "safety") return runSafety(payload);
  if (type === "destroy") {
    const destroyed = Module._spc_root_session_destroy(sessionId);
    const previous = sessionId;
    sessionId = null;
    invariant(destroyed === 1, "root session destroy failed");
    return { status: "destroyed", session_id: previous, ...memoryReceipt() };
  }
  throw new Error(`unknown root benchmark operation: ${String(type)}`);
}


self.onmessage = async (event) => {
  const { id, type, payload } = event.data ?? {};
  if (!Number.isInteger(id)) return;
  try {
    self.postMessage({ id, ok: true, payload: await dispatch(type, payload) });
  } catch (error) {
    self.postMessage({
      id,
      ok: false,
      error: {
        message: error instanceof Error ? error.stack ?? error.message : String(error),
      },
    });
  }
};
