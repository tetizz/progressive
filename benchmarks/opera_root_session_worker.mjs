const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const LIVE_S5 = "rn1q1bnr/ppp1pkpp/5p2/8/3Pp3/2NB4/PPP2PPP/R1BbK1NR w KQ - 0 7";
const ZERO_PROMOTED = "0000000000000000";
const MATE_SCORE = 1_000_000;


function invariant(value, message) {
  if (!value) {
    throw new Error(message);
  }
}


async function sha256(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}


function bridge(Module) {
  const encoder = new TextEncoder();

  function withCString(value, callback) {
    const bytes = encoder.encode(value);
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

  function rootJson(name, sessionId, request) {
    const bytes = encoder.encode(JSON.stringify(request));
    const pointer = Module._malloc(bytes.length);
    invariant(pointer !== 0, "WASM request allocation failed");
    Module.HEAPU8.set(bytes, pointer);
    try {
      const resultPointer = sessionId === null
        ? Module[name](pointer, bytes.length)
        : Module[name](sessionId, pointer, bytes.length);
      return JSON.parse(Module.UTF8ToString(resultPointer));
    } finally {
      Module._free(pointer);
    }
  }

  function prefixJson() {
    return withCString(START_FEN, (fen) => withCString("-", (ep) => (
      withCString(ZERO_PROMOTED, (promoted) => withCString("", (prefix) => {
        const pointer = Module._spc_boundary_prefix_json(
          fen,
          1,
          0,
          ep,
          promoted,
          prefix,
        );
        return JSON.parse(Module.UTF8ToString(pointer));
      }))
    )));
  }

  function mateJson() {
    return withCString(LIVE_S5, (fen) => withCString("-", (ep) => (
      withCString(ZERO_PROMOTED, (promoted) => {
        const pointer = Module._spc_series_mate_search_json(
          fen,
          5,
          ep,
          promoted,
          0,
          1_000_000,
          60_000,
        );
        return JSON.parse(Module.UTF8ToString(pointer));
      })
    )));
  }
  return { rootJson, prefixJson, mateJson };
}


function candidateSignature(value) {
  return {
    status: value.status,
    bound: value.bound,
    score: value.score,
    terminal: value.terminal,
    proof_bounds: value.proof_bounds,
    root_series: value.root_series,
    child_pv: value.child_pv,
    selective: value.selective,
    evaluation_work_limit_reached: value.evaluation_work_limit_reached,
  };
}


self.onmessage = async (event) => {
  try {
    const { moduleUrl, wasmUrl, buildReceiptUrl } = event.data ?? {};
    invariant(
      [moduleUrl, wasmUrl, buildReceiptUrl].every(
        (value) => typeof value === "string" && value.length > 0,
      ),
      "Opera probe URLs are missing",
    );
    const [moduleResponse, wasmResponse, receiptResponse] = await Promise.all([
      fetch(moduleUrl, { cache: "no-store" }),
      fetch(wasmUrl, { cache: "no-store" }),
      fetch(buildReceiptUrl, { cache: "no-store" }),
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
    invariant(receipt.status === "built-not-certified", "build receipt status changed");
    invariant(receipt.product_publishable === false, "lab receipt claims publishability");
    invariant(receipt.runtime_variant === "single", "artifact is not single-Worker");
    invariant(receipt.thread_count === 1 && receipt.pthreads === false, "artifact is threaded");

    const moduleBlob = URL.createObjectURL(
      new Blob([moduleBytes], { type: "text/javascript" }),
    );
    let Module;
    const instantiateStart = performance.now();
    try {
      const factory = (await import(moduleBlob)).default;
      invariant(typeof factory === "function", "module factory is missing");
      Module = await factory({
        wasmBinary: new Uint8Array(wasmBytes),
        locateFile: (path) => path.endsWith(".wasm") ? wasmUrl : path,
      });
    } finally {
      URL.revokeObjectURL(moduleBlob);
    }
    const instantiateMs = performance.now() - instantiateStart;
    invariant(Module._spc_root_session_abi_version() === 2, "root ABI mismatch");
    invariant(Module._spc_start_kernel_abi_version() === 1, "prefix ABI mismatch");
    invariant(Module._spc_series_mate_abi_version() === 1, "mate ABI mismatch");
    const contract = JSON.parse(
      Module.UTF8ToString(Module._spc_root_session_contract_json()),
    );
    invariant(contract.reply_mate_safety === false, "root ABI claims mate safety");
    invariant(contract.capabilities.selected_owner_certification === true, "selected-owner capability missing");

    const api = bridge(Module);
    const identity = {
      source_fingerprint: receipt.source_fingerprint,
      kernel_sha256: receipt.kernel_sha256,
      module_js_sha256: receipt.module_js_sha256,
      certificate_id: `opera-lab-${receipt.artifact_set_sha256.slice(0, 16)}`,
      runtime_variant: "single",
      thread_count: 1,
      engine_version: receipt.engine_version,
      ruleset_version: receipt.ruleset_version,
      profile_id: receipt.profile_id,
    };
    const config = {
      max_depth: 2,
      width: 4,
      max_work: 20_000_000,
      mate_score: MATE_SCORE,
      series_cache_capacity: receipt.session_geometry.desktop_series_cache_capacity,
      external_cache_weight: 0,
      worker_threads: 1,
      root_tactical_protection: true,
      root_contract_tt_capacity: receipt.session_geometry.root_contract_tt_capacity,
      root_contract_eval_capacity: receipt.session_geometry.root_contract_eval_capacity,
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
    const create = api.rootJson("_spc_root_session_create_json", null, {
      schema: "spc-root-session-create-v1",
      request_id: "opera-create",
      iteration_id: "opera-d1-d2",
      generation: 1,
      ...identity,
      boundary: {
        fen: START_FEN,
        series: 1,
        quiet_series: 0,
        ep_targets: [],
        promoted_hex: ZERO_PROMOTED,
        chess960: false,
      },
      config,
    });
    invariant(create.status === "ready", `create failed: ${JSON.stringify(create)}`);
    const sessionId = create.session_id;
    const deadline = Math.ceil(performance.now() + 60_000);
    let nativeWork = 0;
    const route = (schema, requestId, generation) => ({
      schema,
      request_id: requestId,
      iteration_id: "opera-d1-d2",
      generation,
      ...identity,
      external_work: 0,
      native_work_before: nativeWork,
      call_work_credit: 20_000_000,
      deadline_monotonic_ms: deadline,
      remaining_time_ms: Math.max(0, Math.ceil(deadline - performance.now())),
    });
    const enumerateStart = performance.now();
    const enumeration = api.rootJson("_spc_root_session_enumerate_json", sessionId, {
      ...route("spc-root-session-enumerate-v1", "opera-enumerate", 1),
      preferred_series: [],
    });
    const enumerateMs = performance.now() - enumerateStart;
    invariant(enumeration.status === "complete", `enumerate failed: ${JSON.stringify(enumeration)}`);
    nativeWork = enumeration.work.native_work_after;
    const candidate = enumeration.candidates[0];
    const search = (childDepth, purpose, requestId) => api.rootJson(
      "_spc_root_session_search_json",
      sessionId,
      {
        ...route("spc-root-candidate-task-v1", requestId, childDepth + 1),
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
      },
    );
    const d1Start = performance.now();
    const d1 = search(0, "full", "opera-d1");
    const d1Ms = performance.now() - d1Start;
    invariant(d1.status === "complete" && d1.bound === "exact", "D1 was not exact");
    nativeWork = d1.work.native_work_after;
    const d2Start = performance.now();
    const d2 = search(1, "full", "opera-d2");
    const d2Ms = performance.now() - d2Start;
    invariant(d2.status === "complete" && d2.bound === "exact", "D2 was not exact");
    nativeWork = d2.work.native_work_after;
    const selectedStart = performance.now();
    const selected = search(1, "selected-certification", "opera-selected");
    const selectedMs = performance.now() - selectedStart;
    invariant(selected.status === "complete" && selected.bound === "exact", "selected certification failed");
    invariant(
      JSON.stringify(candidateSignature(selected)) === JSON.stringify(candidateSignature(d2)),
      "warm selected certification changed the exact result",
    );
    invariant(Module._spc_root_session_destroy(sessionId) === 1, "session destroy failed");

    const prefix = api.prefixJson();
    invariant(prefix.ok === true && prefix.schema === "spc-boundary-prefix-v1", "prefix smoke failed");
    const mateStart = performance.now();
    const mate = api.mateJson();
    const mateMs = performance.now() - mateStart;
    invariant(mate.proof_status === "found" && mate.complete === true, "mate smoke failed");
    invariant(
      JSON.stringify(mate.moves) === JSON.stringify(["c3d5", "d3e4", "e4h7", "d5f4", "h7g6"]),
      "mate line changed",
    );

    self.postMessage({
      status: "passed-not-certified",
      product_publishable: false,
      safety_certified: false,
      artifact: {
        source_fingerprint: receipt.source_fingerprint,
        kernel_sha256: receipt.kernel_sha256,
        wasm_sha256: wasmHash,
        module_js_sha256: moduleHash,
        artifact_set_sha256: receipt.artifact_set_sha256,
        exception_strategy: receipt.optimization.exception_strategy,
        wasm_simd: receipt.optimization.wasm_simd,
        allocator: receipt.optimization.allocator,
      },
      environment: {
        ordinary_module_worker: true,
        worker_global_scope: self.constructor?.name ?? "unknown",
        cross_origin_isolated: self.crossOriginIsolated === true,
        hardware_concurrency: self.navigator?.hardwareConcurrency ?? null,
        heap_bytes: Module.HEAPU8.buffer.byteLength,
      },
      timings_ms: {
        instantiate: instantiateMs,
        enumerate: enumerateMs,
        d1: d1Ms,
        d2: d2Ms,
        selected_certification: selectedMs,
        mate_found: mateMs,
      },
      exact_signature: {
        d1: candidateSignature(d1),
        d2: candidateSignature(d2),
        selected: candidateSignature(selected),
        mate_moves: mate.moves,
        mate_stats: mate.stats,
        prefix_legal_count: prefix.legal_next.length,
      },
      root_session_contract: contract,
      gates: {
        exact_bytes_hashed_before_import: true,
        ordinary_module_worker: true,
        combined_prefix_root_mate_abi: true,
        persistent_d1_d2_session: true,
        selected_owner_warm_exact_certification: true,
        browser_worker_smoke: true,
        opera_worker_smoke: true,
        mate_python_parity: false,
        w32_d5_under_60_seconds: false,
      },
    });
  } catch (error) {
    self.postMessage({
      status: "failed",
      product_publishable: false,
      message: error instanceof Error ? error.stack ?? error.message : String(error),
    });
  }
};
