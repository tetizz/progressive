const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const ZERO_PROMOTED = "0000000000000000";
const MATE_SCORE = 1_000_000;
const ROOT_API = globalThis.ScottishProgressiveRootCoordinator;
const PREFIX_API = globalThis.ScottishProgressiveBrowserPrefix;
const diagnostics = [];
let activeDepth = 0;


function invariant(value, message) {
  if (!value) throw new Error(message);
}


function exactInteger(value, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}


function sameJson(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}


function canonicalJsonValue(value) {
  if (Array.isArray(value)) return value.map(canonicalJsonValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonicalJsonValue(value[key])]),
    );
  }
  return value;
}


async function canonicalSha256(value) {
  const encoded = new TextEncoder().encode(JSON.stringify(canonicalJsonValue(value)));
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", encoded));
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
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
  return sameJson(canonicalBoundary(left), canonicalBoundary(right));
}


class RootWorkerChannel {
  constructor(id) {
    this.id = id;
    this.worker = new Worker(
      new URL("./opera_root_d5_worker.mjs", import.meta.url),
      { type: "module", name: `spc-opera-root-${id}` },
    );
    this.nextId = 1;
    this.pending = new Map();
    this.nativeWorkAfter = 0;
    this.memoryBytes = 0;
    this.memoryPeakBytes = 0;
    this.closed = false;
    this.worker.onmessage = (event) => {
      const message = event.data;
      const pending = this.pending.get(message?.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.ok) pending.resolve(message.payload);
      else pending.reject(new Error(message.error?.message || `Worker ${id} failed`));
    };
    this.worker.onerror = (event) => this.close(new Error(event.message || `Worker ${id} crashed`));
    this.worker.onmessageerror = () => this.close(new Error(`Worker ${id} message decode failed`));
  }

  call(type, payload) {
    if (this.closed) return Promise.reject(new Error(`Worker ${this.id} is closed`));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.worker.postMessage({ id, type, payload });
    });
  }

  record(reply) {
    if (exactInteger(reply?.work?.native_work_after)) {
      this.nativeWorkAfter = reply.work.native_work_after;
    } else if (exactInteger(reply?.native_work_after)) {
      this.nativeWorkAfter = reply.native_work_after;
    }
    if (exactInteger(reply?.memory_bytes, 1)) this.memoryBytes = reply.memory_bytes;
    if (exactInteger(reply?.memory_peak_bytes, this.memoryBytes)) {
      this.memoryPeakBytes = Math.max(this.memoryPeakBytes, reply.memory_peak_bytes);
    }
    return reply;
  }

  close(error = new Error(`Worker ${this.id} closed`)) {
    if (this.closed) return;
    this.closed = true;
    this.worker.terminate();
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
  }
}


function parseParameters() {
  const values = new URLSearchParams(location.search);
  const required = ["module", "wasm", "receipt"];
  for (const name of required) invariant(values.get(name), `missing ${name} parameter`);
  const integer = (name, fallback, minimum = 1) => {
    const value = Number(values.get(name) ?? fallback);
    invariant(exactInteger(value, minimum), `${name} is invalid`);
    return value;
  };
  const fen = values.get("fen") || START_FEN;
  const fields = fen.split(" ");
  invariant(fields.length === 6, "fen is invalid");
  const series = integer("series", 1);
  invariant(
    (series % 2 === 1) === (fields[1] === "w"),
    "series and FEN side to move disagree",
  );
  const epTargets = (values.get("ep_targets") || "")
    .split(",")
    .filter(Boolean);
  invariant(
    epTargets.length <= 8
      && epTargets.every((square) => /^[a-h][1-8]$/.test(square))
      && new Set(epTargets).size === epTargets.length,
    "ep_targets is invalid",
  );
  const promotedHex = (values.get("promoted_hex") || ZERO_PROMOTED)
    .toLowerCase()
    .replace(/^0x/, "")
    .padStart(16, "0");
  invariant(/^[0-9a-f]{16}$/.test(promotedHex), "promoted_hex is invalid");
  return {
    moduleUrl: new URL(values.get("module"), location.href).href,
    wasmUrl: new URL(values.get("wasm"), location.href).href,
    receiptUrl: new URL(values.get("receipt"), location.href).href,
    depth: integer("depth", 5),
    width: integer("width", 32),
    workers: integer("workers", 8),
    initialFullWave: integer("wave", 4),
    mode: values.get("mode") === "cold" ? "cold" : "warm",
    maxWork: integer("max_work", 100_000_000),
    safetyReserveWork: integer("safety_work", 1_000_000),
    timeoutMs: integer("timeout_ms", 300_000),
    boundary: Object.freeze({
      fen,
      series,
      quiet_series: integer("quiet_series", 0, 0),
      ep_targets: Object.freeze(epTargets),
      promoted_hex: promotedHex,
      chess960: false,
    }),
  };
}


function rootSeries(value) {
  const moves = Array.isArray(value?.moves) ? value.moves.map(String) : [];
  invariant(moves.length > 0, "root series has no moves");
  invariant(value.machine_notation === moves.join("/"), "root series notation drifted");
  invariant(value.outcome === null, "start-position root series is terminal");
  invariant(value.child_boundary && typeof value.child_boundary === "object", "root child missing");
  return { ...value, moves, child_boundary: canonicalBoundary(value.child_boundary) };
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


function route(schema, requestBase, channel, credit) {
  return {
    schema,
    request_id: requestBase.request_id,
    iteration_id: requestBase.iteration_id,
    generation: requestBase.depth,
    source_fingerprint: requestBase.source_fingerprint,
    kernel_sha256: requestBase.kernel_sha256,
    module_js_sha256: requestBase.module_js_sha256,
    certificate_id: requestBase.certificate_id,
    runtime_variant: requestBase.runtime_variant,
    thread_count: requestBase.thread_count,
    engine_version: requestBase.engine_version,
    ruleset_version: requestBase.ruleset_version,
    profile_id: requestBase.profile_id,
    external_work: 0,
    native_work_before: channel.nativeWorkAfter,
    call_work_credit: credit,
    deadline_monotonic_ms: requestBase.deadline_monotonic_ms,
    remaining_time_ms: Math.max(0, Math.floor(
      requestBase.deadline_monotonic_ms - performance.now(),
    )),
  };
}


function validateSetupReceipt(reply, channel, credit, schema, status) {
  invariant(reply?.schema === schema, `unexpected setup schema: ${reply?.schema}`);
  invariant(reply.status === status, `setup call failed: ${JSON.stringify(reply)}`);
  invariant(reply.work?.native_work_before === channel.nativeWorkAfter, "setup work regressed");
  invariant(reply.work.call_work_credit === credit, "setup credit echo drifted");
  invariant(
    reply.work.call_native_work === reply.work.native_work_after - reply.work.native_work_before,
    "setup work delta drifted",
  );
  invariant(reply.work.call_native_work <= credit, "setup call exceeded credit");
  return channel.record(reply);
}


async function main() {
  invariant(ROOT_API?.runRootIteration, "root coordinator did not load");
  invariant(PREFIX_API?.validatePrefixResult, "prefix contract did not load");
  const args = parseParameters();
  invariant(args.initialFullWave <= args.workers, "initial wave exceeds Worker count");
  const receiptResponse = await fetch(args.receiptUrl, { cache: "no-store" });
  invariant(receiptResponse.ok, "build receipt fetch failed");
  const receipt = await receiptResponse.json();
  invariant(receipt.status === "built-not-certified", "build receipt is not lab-only");
  invariant(receipt.product_publishable === false, "build receipt claims publishability");
  const runId = `opera-${args.mode}-d${args.depth}-w${args.width}-wave${args.initialFullWave}-${Date.now()}`;
  const identity = Object.freeze({
    source_fingerprint: receipt.source_fingerprint,
    kernel_sha256: receipt.kernel_sha256,
    module_js_sha256: receipt.module_js_sha256,
    certificate_id: `lab-not-certified-${receipt.artifact_set_sha256.slice(0, 16)}`,
    runtime_variant: "single",
    thread_count: 1,
    engine_version: receipt.engine_version,
    ruleset_version: receipt.ruleset_version,
    profile_id: receipt.profile_id,
  });
  const boundary = args.boundary;
  const config = Object.freeze({
    max_depth: args.depth,
    width: args.width,
    max_work: args.maxWork,
    mate_score: MATE_SCORE,
    series_cache_capacity: receipt.session_geometry.desktop_series_cache_capacity,
    external_cache_weight: 0,
    worker_threads: 1,
    root_tactical_protection: false,
    root_contract_tt_capacity: receipt.session_geometry.root_contract_tt_capacity,
    root_contract_eval_capacity: receipt.session_geometry.root_contract_eval_capacity,
    weights: Object.freeze({
      material: 100,
      king_space: 100,
      series_reach: 100,
      promotion_corridors: 100,
      immediate_vulnerability: 100,
      useful_mobility: 100,
      boundary_check: 100,
    }),
  });
  const channels = Array.from({ length: args.workers }, (_, index) => (
    new RootWorkerChannel(`root-${index}`)
  ));
  const totalStarted = performance.now();
  const absoluteDeadline = totalStarted + args.timeoutMs;
  let safetyWork = 0;
  let preferredSeries = [];
  const iterations = [];
  let prefixIdentity = null;
  try {
    const ready = await Promise.all(channels.map((channel) => channel.call("initialize", {
      workerId: channel.id,
      runId,
      moduleUrl: args.moduleUrl,
      wasmUrl: args.wasmUrl,
      buildReceiptUrl: args.receiptUrl,
      identity,
      boundary,
      config,
    }).then((reply) => channel.record(reply))));
    const poolReadyMs = performance.now() - totalStarted;
    invariant(ready.every((reply) => sameJson(reply.identity, identity)), "Worker identity mismatch");
    invariant(
      ready.every((reply) => sameJson(reply.build, ready[0].build)),
      "Worker artifact-byte identity mismatch",
    );
    invariant(
      ready.every((reply) => sameJson(reply.root_contract, ready[0].root_contract)),
      "Worker root contract mismatch",
    );
    invariant(
      ready.every((reply) => sameJson(reply.prefix_contract, ready[0].prefix_contract)),
      "Worker prefix contract mismatch",
    );
    invariant(
      ready.every((reply) => (
        reply.canonical_root_tactical_policy === "canonical-boundary-policy-v1"
        && reply.canonical_root_tactical_protection === false
      )),
      "Worker canonical root policy echo mismatch",
    );
    const certifiedPrefixContract = Object.freeze({
      ...ready[0].prefix_contract,
      limits: Object.freeze({ ...ready[0].prefix_contract.hard_limits }),
    });
    prefixIdentity = Object.freeze({
      source_fingerprint: identity.source_fingerprint,
      wasm_sha256: receipt.wasm_sha256,
      module_js_sha256: identity.module_js_sha256,
      prefix_certificate_id: `${identity.certificate_id}:prefix`,
      engine_version: identity.engine_version,
      ruleset_version: identity.ruleset_version,
      prefix_contract: certifiedPrefixContract,
    });
    PREFIX_API.validateCertifiedPrefixContract(prefixIdentity.prefix_contract);
    const iterativeStarted = performance.now();
    const depths = args.mode === "cold"
      ? [args.depth]
      : Array.from({ length: args.depth }, (_, index) => index + 1);
    for (const depth of depths) {
      activeDepth = depth;
      invariant(performance.now() < absoluteDeadline, `deadline before D${depth}`);
      const iterationStarted = performance.now();
      const requestBase = {
        schema: ROOT_API.REQUEST_SCHEMA,
        request_id: runId,
        iteration_id: `${runId}:d${depth}`,
        ...identity,
        boundary,
        required_prefix: [],
        depth,
        mate_score: MATE_SCORE,
        deadline_monotonic_ms: absoluteDeadline,
      };
      const nativeBeforeSetup = channels.reduce((sum, channel) => sum + channel.nativeWorkAfter, 0);
      let remaining = args.maxWork - nativeBeforeSetup - safetyWork;
      invariant(remaining > 0, `global work cap exhausted before D${depth}`);
      const setupCredit = Math.max(1, Math.floor(remaining / (channels.length + 1)));
      const primary = channels[0];
      const enumerateRequest = {
        ...route("spc-root-session-enumerate-v1", requestBase, primary, setupCredit),
        preferred_series: preferredSeries,
      };
      enumerateRequest.external_work = channels.reduce(
        (sum, channel) => sum + channel.nativeWorkAfter,
        safetyWork,
      ) - primary.nativeWorkAfter;
      const enumeration = validateSetupReceipt(
        await primary.call("enumerate", enumerateRequest),
        primary,
        setupCredit,
        "spc-root-session-enumeration-result-v1",
        "complete",
      );
      invariant(enumeration.imported === false, "primary enumeration claims import");
      invariant(
        enumeration.canonical_root_tactical_policy === "canonical-boundary-policy-v1"
          && enumeration.canonical_root_tactical_protection === false,
        `D${depth} enumeration canonical root policy drifted`,
      );
      invariant(
        enumeration.enumeration_identity.includes("|root-policycanonical-boundary-v1|root-tactical0"),
        `D${depth} enumeration identity lacks canonical root policy=false`,
      );
      const manifest = manifestOf(enumeration);
      invariant(manifest.requested_width === args.width, "enumerated width drifted");
      invariant(manifest.candidates.length > 0, "root manifest is empty");
      const setupTotal = channels.reduce(
        (sum, channel) => sum + channel.nativeWorkAfter,
        safetyWork,
      );
      await Promise.all(channels.slice(1).map(async (channel) => {
        const request = {
          ...route("spc-root-session-import-v1", requestBase, channel, setupCredit),
          manifest,
          external_work: setupTotal - channel.nativeWorkAfter,
        };
        const imported = validateSetupReceipt(
          await channel.call("import", request),
          channel,
          setupCredit,
          "spc-root-session-import-result-v1",
          "complete",
        );
        invariant(imported.imported === true, `Worker ${channel.id} did not import`);
        invariant(
          imported.canonical_root_tactical_policy === "canonical-boundary-policy-v1"
            && imported.canonical_root_tactical_protection === false,
          `Worker ${channel.id} imported a different canonical root policy`,
        );
        invariant(sameJson(manifestOf(imported), manifest), `Worker ${channel.id} manifest drifted`);
      }));
      const initialWork = channels.reduce(
        (sum, channel) => sum + channel.nativeWorkAfter,
        safetyWork,
      );
      remaining = args.maxWork - initialWork;
      invariant(remaining > 0, `enumeration consumed D${depth} work envelope`);
      const safetyReserve = Math.min(remaining, args.safetyReserveWork);
      const searchCredit = Math.max(
        1,
        Math.floor(Math.max(1, remaining - safetyReserve) / channels.length),
      );
      const coordinatorRequest = {
        ...requestBase,
        width: args.width,
        worker_count: channels.length,
        initial_full_wave: args.initialFullWave,
        dynamic_work_pool: true,
        call_work_credit_supported: true,
        caps: {
          max_work: args.maxWork,
          initial_work: initialWork,
          safety_reserve_work: safetyReserve,
          search_call_work_credit: searchCredit,
          safety_call_work_credit: safetyReserve,
          max_memory_bytes: channels.length * receipt.memory_envelope.maximum_bytes,
        },
      };
      const candidateById = new Map(manifest.candidates.map((candidate) => [
        candidate.candidate_identity,
        candidate,
      ]));
      const adapters = channels.map((channel) => ({
        id: channel.id,
        call_work_credit_supported: true,
        hard_memory_limit_supported: true,
        identity,
        memory_limit_bytes: receipt.memory_envelope.maximum_bytes,
        native_work_after: channel.nativeWorkAfter,
        search: async (task) => {
          const reply = await channel.call("search", {
            ...task,
            remaining_time_ms: Math.max(0, Math.floor(
              task.deadline_monotonic_ms - performance.now(),
            )),
          });
          channel.record(reply);
          diagnostics.push({
            depth,
            worker_id: channel.id,
            task: {
              task_id: task.task_id,
              candidate_identity: task.candidate_identity,
              order_key: task.order_key,
              purpose: task.purpose,
              incumbent_epoch: task.incumbent_epoch,
              safety_revision: task.safety_revision,
              alpha: task.alpha,
              beta: task.beta,
              external_work: task.external_work,
              native_work_before: task.native_work_before,
              call_work_credit: task.call_work_credit,
            },
            reply: {
              status: reply.status,
              error_code: reply.error_code ?? null,
              bound: reply.bound ?? null,
              score: reply.score ?? null,
              purpose: reply.purpose ?? null,
              task_id: reply.task_id ?? null,
              candidate_identity: reply.candidate_identity ?? null,
              work: reply.work ?? null,
              memory_bytes: reply.memory_bytes ?? null,
              memory_peak_bytes: reply.memory_peak_bytes ?? null,
            },
          });
          if (diagnostics.length > 12) diagnostics.shift();
          return reply;
        },
        cancel: () => {},
      }));
      const safetyProbe = async (task) => {
        const candidate = candidateById.get(task.candidate_identity);
        invariant(candidate, "safety candidate is absent from the manifest");
        const series = rootSeries(candidate.root_series);
        const owner = channels.find((channel) => channel.id === task.candidate?.owner_worker_id)
          ?? channels[0];
        const rootReplayRequest = PREFIX_API.normalizePrefixRequest({
          ...boundary,
          prefix: series.moves,
        }, `${task.iteration_id}:root-replay:${task.safety_revision}`, prefixIdentity.prefix_contract);
        const rootReplay = owner.record(await owner.call("prefix", rootReplayRequest));
        PREFIX_API.validatePrefixResult(rootReplay, rootReplayRequest, prefixIdentity);
        invariant(rootReplay.complete === true, "selected root series did not complete");
        invariant(rootReplay.outcome === null, "selected root series is terminal");
        invariant(sameBoundary(rootReplay.next_state, series.child_boundary), "root child replay drifted");
        const safety = owner.record(await owner.call("safety", {
          task,
          childBoundary: series.child_boundary,
          remainingTimeMs: Math.max(0, Math.floor(
            task.deadline_monotonic_ms - performance.now(),
          )),
        }));
        if (safety.status === "found") {
          const moves = safety.reply_mate?.moves;
          const mateReplayRequest = PREFIX_API.normalizePrefixRequest({
            ...series.child_boundary,
            prefix: moves,
          }, `${task.iteration_id}:mate:${task.safety_revision}`, prefixIdentity.prefix_contract);
          PREFIX_API.validatePrefixResult(
            safety.reply_mate.checked_prefix,
            mateReplayRequest,
            prefixIdentity,
          );
          invariant(safety.reply_mate.checked_prefix.outcome === "checkmate", "mate replay drifted");
        }
        return safety;
      };
      const result = await ROOT_API.runRootIteration({
        request: coordinatorRequest,
        manifest,
        workers: adapters,
        safetyProbe,
      });
      invariant(result.status === "complete", `D${depth} coordinator incomplete`);
      invariant(result.coverage_complete === true, `D${depth} bound coverage incomplete`);
      invariant(result.safety_certified === true, `D${depth} mate safety incomplete`);
      invariant(["exhausted", "terminal"].includes(result.safety_status), `D${depth} safety unknown`);
      const selectedManifest = candidateById.get(result.selected.candidate_identity);
      invariant(selectedManifest, `D${depth} selected candidate is not retained`);
      const selectedSeries = rootSeries(selectedManifest.root_series);
      const finalReplayRequest = PREFIX_API.normalizePrefixRequest({
        ...boundary,
        prefix: selectedSeries.moves,
      }, `${requestBase.iteration_id}:final-replay`, prefixIdentity.prefix_contract);
      const finalReplay = channels[0].record(await channels[0].call("prefix", finalReplayRequest));
      PREFIX_API.validatePrefixResult(finalReplay, finalReplayRequest, prefixIdentity);
      invariant(sameBoundary(finalReplay.next_state, selectedSeries.child_boundary), "final replay drifted");
      safetyWork += result.work.safety_committed_work;
      preferredSeries = [...selectedSeries.moves];
      const retainedManifestSha256 = await canonicalSha256(manifest);
      const orderShapeSha256 = await canonicalSha256({
        initial_full_wave: args.initialFullWave,
        tasks: result.tasks.map((task) => ({
          event: task.event,
          worker_id: task.worker_id ?? null,
          candidate_identity: task.candidate_identity ?? null,
          purpose: task.purpose ?? null,
          bound: task.bound ?? null,
          score: task.score ?? null,
        })),
      });
      iterations.push({
        depth,
        elapsed_ms: performance.now() - iterationStarted,
        candidate_identity: result.selected.candidate_identity,
        move: selectedSeries.machine_notation,
        score: result.selected.score,
        proof_bounds: result.selected.proof_bounds,
        principal_variation: [selectedSeries, ...result.selected.child_pv],
        work: result.work,
        memory: result.memory,
        task_count: result.tasks.length,
        safety_status: result.safety_status,
        safety_revision: result.safety_revision,
        owner_worker_id: result.selected.owner_worker_id,
        owner_certification_count: result.tasks.filter(
          (task) => task.event === "complete" && task.purpose === "selected-certification",
        ).length,
        root_bounds: result.root_bounds,
        retained_manifest_sha256: retainedManifestSha256,
        order_shape_sha256: orderShapeSha256,
        coverage_complete: result.coverage_complete,
        root_scores_complete: result.root_scores_complete,
        width_complete: result.width_complete,
        canonical_root_tactical_policy: enumeration.canonical_root_tactical_policy,
        canonical_root_tactical_protection: enumeration.canonical_root_tactical_protection,
        final_replay: {
          complete: finalReplay.complete,
          outcome: finalReplay.outcome,
          prefix: finalReplay.prefix,
          next_state: finalReplay.next_state,
        },
      });
    }
    const iterativeMs = performance.now() - iterativeStarted;
    const totalMs = performance.now() - totalStarted;
    const final = iterations.at(-1);
    invariant(final.depth === args.depth, "requested depth did not complete");
    if (
      args.depth === 5
      && args.width === 32
      && boundary.fen === START_FEN
      && boundary.series === 1
      && boundary.quiet_series === 0
      && boundary.ep_targets.length === 0
      && boundary.promoted_hex === ZERO_PROMOTED
    ) {
      invariant(final.move === "b2b3", `D5 move anchor drifted: ${final.move}`);
      invariant(final.score === 951, `D5 score anchor drifted: ${final.score}`);
    }
    const aggregatePeak = channels.reduce((sum, channel) => sum + channel.memoryPeakBytes, 0);
    return {
      schema: "spc-opera-root-d5-benchmark-v1",
      status: "passed-not-certified",
      product_publishable: false,
      safety_certified: true,
      artifact: {
        source_revision: receipt.source_revision,
        source_fingerprint: receipt.source_fingerprint,
        kernel_sha256: receipt.kernel_sha256,
        wasm_sha256: receipt.wasm_sha256,
        module_js_sha256: receipt.module_js_sha256,
        artifact_set_sha256: receipt.artifact_set_sha256,
        exception_strategy: receipt.optimization.exception_strategy,
        wasm_simd: receipt.optimization.wasm_simd,
        allocator: receipt.optimization.allocator,
      },
      geometry: {
        workers: args.workers,
        initial_full_wave: args.initialFullWave,
        depth: args.depth,
        width: args.width,
        max_work: args.maxWork,
        safety_reserve_work: args.safetyReserveWork,
        config,
        mode: args.mode,
      },
      timings_ms: {
        pool_ready: poolReadyMs,
        iterative_d1_through_d5: iterativeMs,
        total_to_completed_depth: totalMs,
        completed_depth_iteration: final.elapsed_ms,
      },
      result: {
        completed_depth: final.depth,
        candidate_identity: final.candidate_identity,
        move: final.move,
        score: final.score,
        proof_bounds: final.proof_bounds,
        principal_variation: final.principal_variation,
        work: final.work,
        safety_status: final.safety_status,
        safety_revision: final.safety_revision,
        owner_worker_id: final.owner_worker_id,
        root_bounds: final.root_bounds,
        retained_manifest_sha256: final.retained_manifest_sha256,
        order_shape_sha256: final.order_shape_sha256,
        coverage_complete: final.coverage_complete,
        root_scores_complete: final.root_scores_complete,
        width_complete: final.width_complete,
      },
      iterations,
      memory: {
        per_worker_hard_maximum_bytes: receipt.memory_envelope.maximum_bytes,
        aggregate_hard_maximum_bytes: args.workers * receipt.memory_envelope.maximum_bytes,
        aggregate_observed_peak_bytes: aggregatePeak,
        workers: channels.map((channel) => ({
          id: channel.id,
          peak_bytes: channel.memoryPeakBytes,
          native_work_after: channel.nativeWorkAfter,
        })),
      },
      environment: {
        ordinary_module_workers: true,
        worker_count: channels.length,
        worker_global_scope: ready[0].environment.worker_global_scope,
        hardware_concurrency: navigator.hardwareConcurrency,
        cross_origin_isolated: self.crossOriginIsolated === true,
        workers: ready.map((reply) => ({
          worker_id: reply.worker_id,
          identity: reply.identity,
          artifact: reply.build,
          ordinary_module_worker: reply.environment.ordinary_module_worker,
          worker_global_scope: reply.environment.worker_global_scope,
        })),
      },
      gates: {
        exact_artifact_identity_all_workers: true,
        ordinary_module_workers: true,
        pthreads_disabled: true,
        combined_prefix_root_mate_abi: true,
        persistent_d1_through_d5_sessions: args.mode === "warm",
        exact_manifest_import_all_workers: true,
        canonical_root_tactical_policy: config.root_tactical_protection === false,
        canonical_root_tactical_boundary_echoes: iterations.every((iteration) => (
          iteration.canonical_root_tactical_policy === "canonical-boundary-policy-v1"
          && iteration.canonical_root_tactical_protection === false
        )),
        global_work_cap_enforced: final.work.within_cap === true,
        common_monotonic_deadline: true,
        dynamic_work_pool_certified: true,
        final_bound_coverage: final.coverage_complete,
        selected_owner_warm_exact_certification: final.owner_certification_count === 1,
        compiled_root_prefix_replay: final.final_replay.complete === true,
        compiled_reply_mate_safety: final.safety_status === "exhausted",
        memory_envelope_observed: aggregatePeak
          <= args.workers * receipt.memory_envelope.maximum_bytes,
        d5_w32_anchor: args.depth !== 5 || args.width !== 32
          || (final.move === "b2b3" && final.score === 951),
        under_60_seconds_total: totalMs < 60_000,
        mate_python_parity: false,
        release_certificate_present: false,
      },
    };
  } finally {
    await Promise.all(channels.map(async (channel) => {
      if (!channel.closed) await channel.call("destroy", {}).catch(() => undefined);
      channel.close();
    }));
  }
}


const output = document.querySelector("#result");
try {
  const result = await main();
  document.body.dataset.probeStatus = "passed";
  output.textContent = JSON.stringify(result);
} catch (error) {
  document.body.dataset.probeStatus = "failed";
  output.textContent = JSON.stringify({
    schema: "spc-opera-root-d5-benchmark-v1",
    status: "failed",
    product_publishable: false,
    safety_certified: false,
    message: error instanceof Error ? error.stack ?? error.message : String(error),
    active_depth: activeDepth,
    diagnostics,
  });
}
