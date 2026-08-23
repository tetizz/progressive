import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";


const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const api = require(path.join(root, "root-iteration-coordinator.js"));
const MATE = 1_000_000;
const WHITE_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const BLACK_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1";
const IDENTITY = Object.freeze({
  source_fingerprint: "a".repeat(16),
  kernel_sha256: "b".repeat(64),
  module_js_sha256: "c".repeat(64),
  certificate_id: "spc-synthetic-certificate-v1",
  runtime_variant: "single-thread-wasm",
  thread_count: 1,
  engine_version: "spc-synthetic-v1",
  ruleset_version: "progressive-synthetic-v1",
  profile_id: "baseline-100",
});


function request(workerCount, overrides = {}) {
  const series = overrides.series ?? 1;
  const { boundary: _boundary, caps: _caps, ...topLevelOverrides } = overrides;
  const caps = {
    max_work: 2_000,
    initial_work: 0,
    safety_reserve_work: 100,
    search_call_work_credit: 25,
    safety_call_work_credit: 5,
    max_memory_bytes: workerCount * 1_024,
    ...(overrides.caps || {}),
  };
  return {
    schema: api.REQUEST_SCHEMA,
    request_id: overrides.request_id ?? `request-${series}-${workerCount}`,
    iteration_id: overrides.iteration_id ?? `iteration-${series}-${workerCount}`,
    ...IDENTITY,
    boundary: {
      fen: series % 2 === 1 ? WHITE_FEN : BLACK_FEN,
      series,
      quiet_series: 0,
      ep_targets: [],
      promoted_hex: "0",
      chess960: false,
      ...(overrides.boundary || {}),
    },
    required_prefix: [],
    depth: 5,
    width: overrides.width ?? 32,
    mate_score: MATE,
    deadline_monotonic_ms: overrides.deadline_monotonic_ms ?? performance.now() + 5_000,
    worker_count: workerCount,
    initial_full_wave: overrides.initial_full_wave ?? Math.min(workerCount, 4),
    dynamic_work_pool: true,
    call_work_credit_supported: true,
    ...topLevelOverrides,
    caps,
  };
}


function manifest(
  definitions,
  { white = true, width = 32, complete = false, preferredSeries = [] } = {},
) {
  const series = white ? 1 : 2;
  const moves = white ? ["e2e4"] : ["e7e5", "g8f6"];
  const childFen = white ? BLACK_FEN : WHITE_FEN;
  return {
    enumeration_identity: `manifest-${white ? "w" : "b"}-${definitions.map((item) => item.id).join("-")}`,
    root_white_to_move: white,
    requested_width: width,
    retained_count: definitions.length,
    width_complete: complete,
    preferred_series: [...preferredSeries],
    candidates: definitions.map((item, orderIndex) => ({
      candidate_identity: item.id,
      order_index: orderIndex,
      order_key: item.key,
      terminal_score: item.terminalScore ?? null,
      terminal_proof_bounds: item.terminalProof ?? [-1, 1],
      root_series: {
        moves: [...moves],
        machine_notation: moves.join("/"),
        transposition_count: 1,
        child_boundary: {
          fen: childFen,
          board_fen: childFen,
          series: series + 1,
          series_number: series + 1,
          side_to_move: white ? "black" : "white",
          quiet_series: 0,
          quiet_draw_pending: false,
          ep_targets: [],
          progressive_ep: [],
          promoted_hex: "0000000000000000",
          chess960: false,
        },
        outcome: item.terminalScore === undefined ? null : "checkmate",
        ended_by_check: item.terminalScore !== undefined,
      },
    })),
  };
}


function referenceWinner(definitions, white, overrides = new Map()) {
  const candidates = definitions.map((item) => ({
    id: item.id,
    key: item.key,
    score: overrides.has(item.id)
      ? overrides.get(item.id)
      : item.terminalScore ?? item.score,
  }));
  candidates.sort((left, right) => {
    if (left.score !== right.score) {
      return white ? right.score - left.score : left.score - right.score;
    }
    return left.key.localeCompare(right.key);
  });
  return candidates[0];
}


function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}


class SyntheticWorker {
  constructor(id, oracle, options = {}) {
    this.id = id;
    this.oracle = oracle;
    this.call_work_credit_supported = true;
    this.hard_memory_limit_supported = true;
    this.identity = { ...IDENTITY, ...(options.identity || {}) };
    this.memory_limit_bytes = options.memoryLimit ?? 1_024;
    this.native_work_after = options.nativeWorkAfter ?? 0;
    this.nativeWork = this.native_work_after;
    this.delay = options.delay ?? (() => 0);
    this.work = options.work ?? (() => 1);
    this.memory = options.memory ?? (() => 128);
    this.mutate = options.mutate ?? ((_task, reply) => reply);
    this.throwWhen = options.throwWhen ?? (() => false);
    this.honorAbort = options.honorAbort ?? true;
    this.events = options.events ?? [];
    this.cancelCalls = [];
    this.calls = [];
  }

  async search(task, { signal }) {
    this.calls.push(task);
    this.events.push({ event: "worker-dispatch", worker: this.id, task });
    const delay = this.delay(task);
    if (delay > 0) {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, delay);
        if (this.honorAbort) {
          signal.addEventListener("abort", () => {
            clearTimeout(timer);
            reject(new Error("synthetic worker aborted"));
          }, { once: true });
        }
      });
    }
    if (this.throwWhen(task)) throw new Error(`synthetic loss ${this.id}`);
    const score = this.oracle.get(task.candidate_identity);
    if (!Number.isSafeInteger(score)) throw new Error("missing synthetic oracle score");
    const bound = task.purpose === "scout" || task.purpose === "aspiration"
      ? score <= task.alpha ? "upper" : score >= task.beta ? "lower" : "exact"
      : "exact";
    const actualWork = this.work(task);
    const before = this.nativeWork;
    this.nativeWork += actualWork;
    const reply = {
      request_id: task.request_id,
      iteration_id: task.iteration_id,
      source_fingerprint: task.source_fingerprint,
      kernel_sha256: task.kernel_sha256,
      module_js_sha256: task.module_js_sha256,
      certificate_id: task.certificate_id,
      runtime_variant: task.runtime_variant,
      thread_count: task.thread_count,
      engine_version: task.engine_version,
      ruleset_version: task.ruleset_version,
      profile_id: task.profile_id,
      generation: task.generation,
      safety_revision: task.safety_revision,
      incumbent_epoch: task.incumbent_epoch,
      task_id: task.task_id,
      enumeration_identity: task.enumeration_identity,
      candidate_identity: task.candidate_identity,
      purpose: task.purpose,
      tt_persistence: task.tt_persistence,
      child_depth: task.child_depth,
      alpha: task.alpha,
      beta: task.beta,
      status: "complete",
      bound,
      score,
      proof_bounds: [-1, 1],
      child_pv: [`${task.candidate_identity}-pv`],
      work: {
        call_work_credit: task.call_work_credit,
        external_work: task.external_work,
        native_work_before: before,
        native_work_after: this.nativeWork,
        call_native_work: actualWork,
        total_accounted_work: task.external_work + this.nativeWork,
      },
      memory_bytes: this.memory(task),
      memory_peak_bytes: this.memory(task),
    };
    const mutated = this.mutate(task, reply, this.calls.length);
    this.events.push({ event: "worker-complete", worker: this.id, task, reply: mutated });
    return mutated;
  }

  cancel(payload) {
    this.cancelCalls.push(payload);
    this.events.push({ event: "worker-cancel", worker: this.id, payload });
  }
}


function workers(count, definitions, options = {}) {
  const oracle = new Map(definitions.map((item) => [item.id, item.score]));
  const events = options.events ?? [];
  return Array.from({ length: count }, (_, index) => new SyntheticWorker(
    `worker-${index}`,
    oracle,
    { ...options, events, ...(options.perWorker?.[index] || {}) },
  ));
}


function exhaustedSafety(events = [], workUsed = 0) {
  return async (task) => {
    events.push({ event: "safety-probe", task });
    return {
      request_id: task.request_id,
      iteration_id: task.iteration_id,
      source_fingerprint: task.source_fingerprint,
      kernel_sha256: task.kernel_sha256,
      module_js_sha256: task.module_js_sha256,
      certificate_id: task.certificate_id,
      runtime_variant: task.runtime_variant,
      thread_count: task.thread_count,
      engine_version: task.engine_version,
      ruleset_version: task.ruleset_version,
      profile_id: task.profile_id,
      generation: task.generation,
      safety_revision: task.safety_revision,
      candidate_identity: task.candidate_identity,
      status: "exhausted",
      work_used: workUsed,
    };
  };
}


async function expectCode(promiseOrCallback, code) {
  if (typeof promiseOrCallback === "function") {
    assert.throws(promiseOrCallback, (error) => error?.code === code);
    return;
  }
  await assert.rejects(promiseOrCallback, (error) => error?.code === code);
}


async function testStreamingWhiteAndStaleEpoch() {
  const definitions = [
    { id: "a", key: "a2a3", score: 10 },
    { id: "b", key: "b2b3", score: 20 },
    { id: "c", key: "c2c3", score: 5 },
    { id: "d", key: "d2d3", score: 30 },
  ];
  const events = [];
  const pool = workers(2, definitions, {
    events,
    delay: (task) => {
      if (task.candidate_identity === "a") return 3;
      if (task.candidate_identity === "b") return 25;
      if (task.candidate_identity === "c") return 35;
      return 3;
    },
  });
  const result = await api.runRootIteration({
    request: request(2),
    manifest: manifest(definitions),
    workers: pool,
    safetyProbe: exhaustedSafety(events),
  });
  const oracle = referenceWinner(definitions, true);
  assert.equal(result.selected.candidate_identity, oracle.id);
  assert.equal(result.selected.score, oracle.score);
  const dispatchC = events.findIndex((item) => (
    item.event === "worker-dispatch" && item.task.candidate_identity === "c"
  ));
  const completeB = events.findIndex((item) => (
    item.event === "worker-complete" && item.task.candidate_identity === "b"
  ));
  assert(dispatchC >= 0 && dispatchC < completeB, "freed first-wave Worker was not streamed");
  assert(result.tasks.some((item) => (
    item.event === "complete" && item.candidate_identity === "c" && item.stale_epoch
  )));
  const dCalls = pool.flatMap((worker) => worker.calls.map((task) => ({ worker, task })))
    .filter(({ task }) => task.candidate_identity === "d");
  const threat = dCalls.find(({ task }) => task.purpose === "threat-research");
  const certificate = dCalls.find(({ task }) => task.purpose === "selected-certification");
  assert(threat && certificate);
  assert.equal(threat.worker.id, certificate.worker.id);
  assert(result.coverage_complete);
  assert(result.dynamic_work_pool_certified);
  assert.equal(result.root_bounds.length, definitions.length);
  assert.deepEqual(
    result.root_bounds.map((item) => item.candidate_identity),
    [...definitions.map((item) => item.id)].sort(),
  );
  assert(result.root_bounds.every((item) => item.bound !== "unknown"));
  assert.equal(
    result.root_bounds.find((item) => item.candidate_identity === oracle.id)?.bound,
    "exact",
  );
  return result;
}


async function testCertifiedInitialFullWave() {
  const definitions = [
    { id: "a", key: "a", score: 50 },
    { id: "b", key: "b", score: 10 },
    { id: "c", key: "c", score: 20 },
    { id: "d", key: "d", score: 30 },
    { id: "e", key: "e", score: 0 },
    { id: "f", key: "f", score: 1 },
    { id: "g", key: "g", score: 2 },
    { id: "h", key: "h", score: 3 },
    { id: "i", key: "i", score: 60 },
    { id: "j", key: "j", score: 5 },
    { id: "k", key: "k", score: 4 },
    { id: "l", key: "l", score: 3 },
  ];
  const events = [];
  const pool = workers(8, definitions, {
    events,
    delay: (task) => (
      task.purpose === "full" && task.candidate_identity !== "a" ? 45 : 2
    ),
  });
  const result = await api.runRootIteration({
    request: request(8, { initial_full_wave: 4 }),
    manifest: manifest(definitions),
    workers: pool,
    safetyProbe: exhaustedSafety(events),
  });
  assert.equal(result.selected.candidate_identity, "i");
  const searchEvents = events.filter((item) => (
    item.event === "worker-dispatch" || item.event === "worker-complete"
  ));
  const firstCompletion = searchEvents.findIndex((item) => item.event === "worker-complete");
  assert(firstCompletion >= 4);
  assert.deepEqual(
    searchEvents.slice(0, firstCompletion).map((item) => [item.worker, item.task.purpose]),
    [
      ["worker-0", "full"],
      ["worker-1", "full"],
      ["worker-2", "full"],
      ["worker-3", "full"],
    ],
  );
  assert.equal(searchEvents[firstCompletion].task.candidate_identity, "a");
  const nextInitialFullCompletion = searchEvents.findIndex((item, index) => (
    index > firstCompletion
    && item.event === "worker-complete"
    && item.task.purpose === "full"
  ));
  const streamedBeforeBarrier = searchEvents
    .slice(firstCompletion + 1, nextInitialFullCompletion)
    .filter((item) => item.event === "worker-dispatch" && item.task.purpose === "scout");
  assert.deepEqual(
    new Set(streamedBeforeBarrier.map((item) => item.worker)),
    new Set(["worker-0", "worker-4", "worker-5", "worker-6", "worker-7"]),
  );
  assert.equal(
    pool.flatMap((worker) => worker.calls).filter((task) => task.purpose === "full").length,
    4,
  );
}


async function testWhiteCanonicalTies() {
  const definitions = [
    { id: "b", key: "b2b3", score: 10 },
    { id: "a", key: "a2a3", score: 10 },
    { id: "c", key: "c2c3", score: 10 },
  ];
  const pool = workers(1, definitions);
  const result = await api.runRootIteration({
    request: request(1),
    manifest: manifest(definitions),
    workers: pool,
    safetyProbe: exhaustedSafety(),
  });
  assert.equal(result.selected.candidate_identity, "a");
  const purposes = (id) => pool[0].calls
    .filter((task) => task.candidate_identity === id)
    .map((task) => task.purpose);
  assert.deepEqual(purposes("a"), ["scout", "threat-research", "selected-certification"]);
  assert.deepEqual(purposes("c"), ["scout"]);
}


async function testBlackMirror() {
  const definitions = [
    { id: "b", key: "b7b6", score: -10 },
    { id: "a", key: "a7a6", score: -10 },
    { id: "d", key: "d7d6", score: -20 },
  ];
  const pool = workers(1, definitions);
  const result = await api.runRootIteration({
    request: request(1, { series: 2 }),
    manifest: manifest(definitions, { white: false }),
    workers: pool,
    safetyProbe: exhaustedSafety(),
  });
  const oracle = referenceWinner(definitions, false);
  assert.equal(result.mover, "black");
  assert.equal(result.selected.candidate_identity, oracle.id);
  const callsA = pool[0].calls.filter((task) => task.candidate_identity === "a");
  const callsD = pool[0].calls.filter((task) => task.candidate_identity === "d");
  assert.equal(callsA[0].alpha, -11);
  assert.equal(callsA[0].beta, -10);
  assert(callsA.some((task) => task.purpose === "threat-research"));
  assert(callsD.some((task) => task.purpose === "threat-research"));
}


async function testTerminalProductionOrder() {
  for (const white of [true, false]) {
    const score = white ? MATE - 1 : -MATE + 1;
    const definitions = [
      { id: "ordinary", key: "a1a2", score: 0 },
      { id: "mate-first", key: "z-mate", score, terminalScore: score, terminalProof: white ? [1, 1] : [-1, -1] },
      { id: "mate-lex", key: "a-mate", score, terminalScore: score, terminalProof: white ? [1, 1] : [-1, -1] },
    ];
    const pool = workers(1, definitions);
    const result = await api.runRootIteration({
      request: request(1, { series: white ? 1 : 2 }),
      manifest: manifest(definitions, { white }),
      workers: pool,
    });
    assert.equal(result.selected.candidate_identity, "mate-first");
    assert.equal(result.safety_status, "terminal");
    assert.equal(pool[0].calls.length, 0);
  }
}


async function testSafetyRevisionAndBoundInvalidation() {
  const definitions = [
    { id: "a", key: "a2a3", score: 100 },
    { id: "b", key: "b2b3", score: 90 },
  ];
  const pool = workers(1, definitions);
  const seenSafety = [];
  const safetyProbe = async (task) => {
    seenSafety.push({ candidate: task.candidate_identity, revision: task.safety_revision });
    if (task.candidate_identity === "a") {
      return {
        request_id: task.request_id,
        iteration_id: task.iteration_id,
        source_fingerprint: task.source_fingerprint,
        kernel_sha256: task.kernel_sha256,
        module_js_sha256: task.module_js_sha256,
        certificate_id: task.certificate_id,
        runtime_variant: task.runtime_variant,
        thread_count: task.thread_count,
        engine_version: task.engine_version,
        ruleset_version: task.ruleset_version,
        profile_id: task.profile_id,
        generation: task.generation,
        safety_revision: task.safety_revision,
        candidate_identity: task.candidate_identity,
        status: "found",
        work_used: 1,
        override_score: -MATE + 2,
        proof_bounds: [-1, -1],
        reply_mate: "synthetic-reply-mate",
      };
    }
    return {
      request_id: task.request_id,
      iteration_id: task.iteration_id,
      source_fingerprint: task.source_fingerprint,
      kernel_sha256: task.kernel_sha256,
      module_js_sha256: task.module_js_sha256,
      certificate_id: task.certificate_id,
      runtime_variant: task.runtime_variant,
      thread_count: task.thread_count,
      engine_version: task.engine_version,
      ruleset_version: task.ruleset_version,
      profile_id: task.profile_id,
      generation: task.generation,
      safety_revision: task.safety_revision,
      candidate_identity: task.candidate_identity,
      status: "exhausted",
      work_used: 1,
    };
  };
  const result = await api.runRootIteration({
    request: request(1),
    manifest: manifest(definitions),
    workers: pool,
    safetyProbe,
  });
  assert.equal(result.selected.candidate_identity, "b");
  assert.equal(result.safety_revision, 1);
  assert.deepEqual(seenSafety, [
    { candidate: "a", revision: 0 },
    { candidate: "b", revision: 1 },
  ]);
  const bCalls = pool[0].calls.filter((task) => task.candidate_identity === "b");
  assert.deepEqual(bCalls.map((task) => [task.purpose, task.safety_revision]), [
    ["scout", 0],
    ["scout", 1],
    ["threat-research", 1],
    ["selected-certification", 1],
  ]);
}


async function testResponseOrderPermutations() {
  const definitions = [
    { id: "a", key: "a", score: 5 },
    { id: "b", key: "b", score: 20 },
    { id: "c", key: "c", score: 10 },
    { id: "d", key: "d", score: 30 },
    { id: "e", key: "e", score: 0 },
    { id: "f", key: "f", score: 25 },
    { id: "g", key: "g", score: 15 },
    { id: "h", key: "h", score: 40 },
    { id: "i", key: "i", score: 35 },
    { id: "j", key: "j", score: 45 },
    { id: "k", key: "k", score: 42 },
    { id: "l", key: "l", score: 50 },
  ];
  const ids = definitions.map((item) => item.id);
  const permutations = [
    ids,
    [...ids].reverse(),
    ...Array.from({ length: 6 }, (_, offset) => (
      [...ids.slice(offset + 1), ...ids.slice(0, offset + 1)]
    )),
  ];
  const signatures = [];
  for (const order of permutations) {
    const rank = new Map(order.map((id, index) => [id, index]));
    const pool = workers(8, definitions, {
      delay: (task) => rank.has(task.candidate_identity)
        ? 1 + rank.get(task.candidate_identity) % 5
        : 1,
    });
    const result = await api.runRootIteration({
      request: request(8, {
        initial_full_wave: 4,
        iteration_id: `permutation-${order.join("")}`,
      }),
      manifest: manifest(definitions),
      workers: pool,
      safetyProbe: exhaustedSafety(),
    });
    signatures.push([result.selected.candidate_identity, result.selected.score]);
  }
  assert.equal(permutations.length, 8);
  assert(signatures.every(([id, score]) => id === "l" && score === 50));
}


async function testAspirationWideningAndFallback() {
  const definitions = [
    { id: "a", key: "a", score: 3_000 },
    { id: "b", key: "b", score: -3_000 },
    { id: "c", key: "c", score: 100 },
    { id: "d", key: "d", score: 100_000 },
  ];
  const pool = workers(4, definitions);
  const result = await api.runRootIteration({
    request: request(4, {
      aspiration: { center_score: 0, initial_delta: 2_048 },
    }),
    manifest: manifest(definitions, { preferredSeries: ["e2e4"] }),
    workers: pool,
    safetyProbe: exhaustedSafety(),
  });
  assert(pool.every((worker) => worker.calls[0]?.purpose === "aspiration"));
  assert.deepEqual(
    pool[0].calls.slice(0, 2).map((task) => [task.alpha, task.beta]),
    [[-2_048, 2_048], [-4_096, 4_096]],
  );
  assert.deepEqual(
    pool[1].calls.slice(0, 2).map((task) => [task.alpha, task.beta]),
    [[-2_048, 2_048], [-4_096, 4_096]],
  );
  assert.deepEqual(pool[2].calls.map((task) => task.purpose), ["aspiration"]);
  assert.deepEqual(pool[3].calls.map((task) => task.purpose), [
    "aspiration",
    "aspiration",
    "aspiration",
    "aspiration",
    "full",
    "selected-certification",
  ]);
  assert.deepEqual(
    pool[3].calls.slice(0, 4).map((task) => [task.alpha, task.beta]),
    [
      [-2_048, 2_048],
      [-4_096, 4_096],
      [-8_192, 8_192],
      [-16_384, 16_384],
    ],
  );
  assert.deepEqual(result.aspiration, {
    enabled: true,
    center_score: 0,
    initial_delta: 2_048,
    maximum_attempts: 4,
    candidate_count: 4,
    attempts: 9,
    fail_highs: 5,
    fail_lows: 1,
    exact_hits: 3,
    full_window_fallbacks: 1,
  });
  assert(result.tasks.some((task) => (
    task.event === "dispatch"
    && task.purpose === "full"
    && task.aspiration_fallback === true
  )));

  await expectCode(
    () => api.normalizeRequest(request(1, {
      aspiration: { center_score: 0, initial_delta: 2_047 },
    })),
    "root-request-invalid",
  );

  const blackDefinitions = definitions.map((item) => ({
    ...item,
    score: -item.score,
  }));
  const blackPool = workers(4, blackDefinitions);
  const blackResult = await api.runRootIteration({
    request: request(4, {
      series: 2,
      aspiration: { center_score: 0, initial_delta: 2_048 },
    }),
    manifest: manifest(blackDefinitions, {
      white: false,
      preferredSeries: ["e7e5", "g8f6"],
    }),
    workers: blackPool,
    safetyProbe: exhaustedSafety(),
  });
  const blackOracle = referenceWinner(blackDefinitions, false);
  assert.equal(blackResult.mover, "black");
  assert.equal(blackResult.selected.candidate_identity, blackOracle.id);
  assert.equal(blackResult.selected.score, blackOracle.score);
  assert.equal(blackResult.root_scores_complete, true);
  assert.deepEqual(
    blackResult.root_bounds.map((item) => ({
      candidate_identity: item.candidate_identity,
      bound: item.bound,
      score: item.score,
    })),
    result.root_bounds.map((item) => ({
      candidate_identity: item.candidate_identity,
      bound: item.bound,
      score: -item.score,
    })),
  );
  assert.deepEqual(blackResult.aspiration, {
    ...result.aspiration,
    fail_highs: result.aspiration.fail_lows,
    fail_lows: result.aspiration.fail_highs,
  });
  for (const definition of blackDefinitions) {
    const owners = blackPool.filter((worker) => worker.calls.some(
      (task) => task.candidate_identity === definition.id,
    ));
    assert.equal(
      owners.length,
      1,
      `${definition.id} aspiration retries left their owning Worker`,
    );
    assert.equal(
      owners[0].calls.find((task) => task.candidate_identity === definition.id)?.purpose,
      "aspiration",
    );
  }
  assert.deepEqual(
    blackPool[3].calls.map((task) => task.purpose),
    [
      "aspiration",
      "aspiration",
      "aspiration",
      "aspiration",
      "full",
      "selected-certification",
    ],
  );
  assert(blackResult.tasks.some((task) => (
    task.event === "dispatch"
    && task.purpose === "full"
    && task.aspiration_fallback === true
    && task.worker_id === "worker-3"
  )));
}


async function testProtocolFaults() {
  const single = [{ id: "a", key: "a", score: 10 }];
  await expectCode(
    () => api.normalizeManifest(manifest([
      ...single,
      { id: "a", key: "duplicate", score: 9 },
    ]), api.normalizeRequest(request(1))),
    "root-manifest-candidate-invalid",
  );

  const unknownPool = workers(1, single, {
    mutate: (_task, reply) => ({ ...reply, bound: "unknown" }),
  });
  await expectCode(api.runRootIteration({
    request: request(1),
    manifest: manifest(single),
    workers: unknownPool,
    safetyProbe: exhaustedSafety(),
  }), "root-worker-result-unknown");

  const missingPool = workers(1, single, {
    mutate: (_task, reply) => ({ ...reply, candidate_identity: "missing" }),
  });
  await expectCode(api.runRootIteration({
    request: request(1),
    manifest: manifest(single),
    workers: missingPool,
    safetyProbe: exhaustedSafety(),
  }), "root-worker-result-invalid");

  let firstTaskId = null;
  const duplicatePool = workers(1, single, {
    mutate: (task, reply) => {
      if (firstTaskId === null) firstTaskId = task.task_id;
      if (task.purpose === "selected-certification") {
        return { ...reply, task_id: firstTaskId };
      }
      return reply;
    },
  });
  await expectCode(api.runRootIteration({
    request: request(1),
    manifest: manifest(single),
    workers: duplicatePool,
    safetyProbe: exhaustedSafety(),
  }), "root-worker-result-duplicate");

  const two = [
    { id: "a", key: "a", score: 10 },
    { id: "b", key: "b", score: 5 },
  ];
  const malformedBoundPool = workers(1, two, {
    mutate: (task, reply) => task.purpose === "scout"
      ? { ...reply, bound: "lower" }
      : reply,
  });
  await expectCode(api.runRootIteration({
    request: request(1),
    manifest: manifest(two),
    workers: malformedBoundPool,
    safetyProbe: exhaustedSafety(),
  }), "root-worker-bound-invalid");

  const mutateManifest = (mutate) => {
    const value = JSON.parse(JSON.stringify(manifest(single)));
    mutate(value);
    return value;
  };
  for (const invalid of [
    mutateManifest((value) => { delete value.candidates[0].root_series; }),
    mutateManifest((value) => { delete value.candidates[0].root_series.child_boundary.promoted_hex; }),
    mutateManifest((value) => { value.candidates[0].root_series.moves = ["e2e5"]; }),
    mutateManifest((value) => { value.candidates[0].root_series.child_boundary.fen = WHITE_FEN; }),
    mutateManifest((value) => { value.candidates[0].root_series.child_boundary.series_number = 99; }),
    mutateManifest((value) => { delete value.preferred_series; }),
    mutateManifest((value) => { value.retained_count = 2; }),
  ]) {
    await assert.rejects(api.runRootIteration({
      request: request(1),
      manifest: invalid,
      workers: workers(1, single),
      safetyProbe: exhaustedSafety(),
    }), (error) => String(error?.code || "").startsWith("root-manifest"));
  }

  const safetyUnknown = async (task) => ({
    request_id: task.request_id,
    iteration_id: task.iteration_id,
    source_fingerprint: task.source_fingerprint,
    kernel_sha256: task.kernel_sha256,
    module_js_sha256: task.module_js_sha256,
    certificate_id: task.certificate_id,
    runtime_variant: task.runtime_variant,
    thread_count: task.thread_count,
    engine_version: task.engine_version,
    ruleset_version: task.ruleset_version,
    profile_id: task.profile_id,
    generation: task.generation,
    safety_revision: task.safety_revision,
    candidate_identity: task.candidate_identity,
    status: "unknown",
    work_used: 0,
  });
  await expectCode(api.runRootIteration({
    request: request(1),
    manifest: manifest(single),
    workers: workers(1, single),
    safetyProbe: safetyUnknown,
  }), "root-safety-unknown");

  const safetyFound = async (task) => ({
    request_id: task.request_id,
    iteration_id: task.iteration_id,
    source_fingerprint: task.source_fingerprint,
    kernel_sha256: task.kernel_sha256,
    module_js_sha256: task.module_js_sha256,
    certificate_id: task.certificate_id,
    runtime_variant: task.runtime_variant,
    thread_count: task.thread_count,
    engine_version: task.engine_version,
    ruleset_version: task.ruleset_version,
    profile_id: task.profile_id,
    generation: task.generation,
    safety_revision: task.safety_revision,
    candidate_identity: task.candidate_identity,
    status: "found",
    work_used: 0,
    override_score: -MATE + 2,
    proof_bounds: [-1, -1],
    reply_mate: "mate",
  });
  await expectCode(api.runRootIteration({
    request: request(1),
    manifest: manifest(single),
    workers: workers(1, single),
    safetyProbe: safetyFound,
  }), "root-safety-widening-required");
}


async function testCapsCrashAndMemory() {
  await expectCode(
    () => api.normalizeRequest(request(1, { call_work_credit_supported: false })),
    "root-call-work-credit-unsupported",
  );
  const single = [{ id: "a", key: "a", score: 10 }];
  const lost = workers(1, single, { throwWhen: () => true });
  await assert.rejects(api.runRootIteration({
    request: request(1),
    manifest: manifest(single),
    workers: lost,
    safetyProbe: exhaustedSafety(),
  }), (error) => {
    assert.equal(error?.code, "root-worker-lost");
    assert.equal(error.work.committed_work, 25);
    assert(error.work.within_cap);
    return true;
  });

  const terminal = [{
    id: "mate",
    key: "mate",
    score: MATE - 1,
    terminalScore: MATE - 1,
    terminalProof: [1, 1],
  }];
  const capRequest = request(1, {
    caps: {
      max_work: 100,
      initial_work: 100,
      safety_reserve_work: 0,
      search_call_work_credit: 1,
      safety_call_work_credit: 0,
      max_memory_bytes: 1_024,
    },
  });
  const atCap = await api.runRootIteration({
    request: capRequest,
    manifest: manifest(terminal),
    workers: workers(1, terminal),
  });
  assert(atCap.work.exact_at_cap);
  assert(atCap.work.within_cap);

  const exactCapPool = workers(1, single, {
    work: (task) => task.purpose === "selected-certification" ? 1 : 24,
  });
  const exactCap = await api.runRootIteration({
    request: request(1, {
      caps: {
        max_work: 25,
        initial_work: 0,
        safety_reserve_work: 0,
        search_call_work_credit: 25,
        safety_call_work_credit: 0,
        max_memory_bytes: 1_024,
      },
    }),
    manifest: manifest(single),
    workers: exactCapPool,
    safetyProbe: exhaustedSafety(),
  });
  assert(exactCap.work.exact_at_cap);
  assert.equal(exactCap.work.committed_work, 25);

  const overCredit = workers(1, single, {
    work: (task) => task.call_work_credit + 1,
  });
  await assert.rejects(api.runRootIteration({
    request: request(1),
    manifest: manifest(single),
    workers: overCredit,
    safetyProbe: exhaustedSafety(),
  }), (error) => {
    assert.equal(error?.code, "root-worker-result-invalid");
    assert.equal(error.work.committed_work, 25);
    return true;
  });

  const admissionWorkers = workers(2, single, {
    perWorker: [{ memoryLimit: 800 }, { memoryLimit: 800 }],
  });
  await expectCode(api.runRootIteration({
    request: request(2, { caps: { max_memory_bytes: 1_024 } }),
    manifest: manifest(single),
    workers: admissionWorkers,
    safetyProbe: exhaustedSafety(),
  }), "root-memory-cap");

  const badMemory = workers(1, single, {
    memoryLimit: 128,
    memory: () => 129,
  });
  await expectCode(api.runRootIteration({
    request: request(1),
    manifest: manifest(single),
    workers: badMemory,
    safetyProbe: exhaustedSafety(),
  }), "root-memory-receipt-invalid");

  const wrongIdentity = workers(1, single);
  wrongIdentity[0].identity = {
    ...wrongIdentity[0].identity,
    module_js_sha256: "d".repeat(64),
  };
  await expectCode(api.runRootIteration({
    request: request(1),
    manifest: manifest(single),
    workers: wrongIdentity,
    safetyProbe: exhaustedSafety(),
  }), "root-worker-set-invalid");

  const wrongReplyIdentity = workers(1, single, {
    mutate: (_task, reply) => ({ ...reply, certificate_id: "wrong-certificate" }),
  });
  await expectCode(api.runRootIteration({
    request: request(1),
    manifest: manifest(single),
    workers: wrongReplyIdentity,
    safetyProbe: exhaustedSafety(),
  }), "root-worker-result-invalid");
}


async function testCancellationAndDeadline() {
  const single = [{ id: "a", key: "a", score: 10 }];
  const controller = new AbortController();
  const slow = workers(1, single, { delay: () => 60, honorAbort: false });
  const pending = api.runRootIteration({
    request: request(1),
    manifest: manifest(single),
    workers: slow,
    safetyProbe: exhaustedSafety(),
    signal: controller.signal,
  });
  setTimeout(() => controller.abort(), 5);
  await assert.rejects(pending, (error) => {
    assert.equal(error?.code, "root-cancelled");
    assert.equal(error.work.committed_work, 25);
    return true;
  });
  assert.equal(slow[0].cancelCalls.length, 1);
  assert.equal(slow[0].cancelCalls[0].cancelled_generation, 1);
  assert.equal(slow[0].cancelCalls[0].next_generation, 2);
  await sleep(65); // The ignored late result must not revive publication.

  const deadlinePool = workers(1, single, { delay: () => 50, honorAbort: false });
  await expectCode(api.runRootIteration({
    request: request(1, { deadline_monotonic_ms: performance.now() + 5 }),
    manifest: manifest(single),
    workers: deadlinePool,
    safetyProbe: exhaustedSafety(),
  }), "root-deadline");
  assert.equal(deadlinePool[0].cancelCalls.length, 1);

  const receiptPool = workers(1, single);
  const receiptSearchDeadline = performance.now() + 50;
  let safetyStartedBeforeSearchDeadline = false;
  const receiptGrace = await api.runRootIteration({
    request: request(1, { deadline_monotonic_ms: receiptSearchDeadline }),
    manifest: manifest(single),
    workers: receiptPool,
    safetyProbe: async (task) => {
      safetyStartedBeforeSearchDeadline = performance.now() < receiptSearchDeadline;
      await sleep(75);
      return exhaustedSafety()(task);
    },
    receiptDeadlineMs: receiptSearchDeadline + 200,
  });
  assert.equal(receiptGrace.status, "complete");
  assert.equal(safetyStartedBeforeSearchDeadline, true);
  assert(performance.now() >= receiptSearchDeadline);
  assert.equal(receiptPool[0].cancelCalls.length, 0);

  await expectCode(api.runRootIteration({
    request: request(1, { deadline_monotonic_ms: performance.now() + 100 }),
    manifest: manifest(single),
    workers: workers(1, single),
    safetyProbe: exhaustedSafety(),
    receiptDeadlineMs: performance.now() + 50,
  }), "root-receipt-deadline-invalid");

  const neverStarted = workers(1, single);
  await expectCode(api.runRootIteration({
    request: request(1, { deadline_monotonic_ms: performance.now() - 1 }),
    manifest: manifest(single),
    workers: neverStarted,
    safetyProbe: exhaustedSafety(),
  }), "root-deadline");
  assert.equal(neverStarted[0].calls.length, 0);

  const monotonicPool = workers(1, single);
  const monotonic = await api.runRootIteration({
    request: request(1, { deadline_monotonic_ms: 100 }),
    manifest: manifest(single),
    workers: monotonicPool,
    safetyProbe: exhaustedSafety(),
    now: () => 50,
  });
  assert.equal(monotonic.status, "complete");
  assert(monotonicPool[0].calls.every((task) => task.deadline_monotonic_ms === 100));
  assert(monotonicPool[0].calls.every((task) => !("deadline_epoch_ms" in task)));

  const injectedExpired = workers(1, single);
  await expectCode(api.runRootIteration({
    request: request(1, { deadline_monotonic_ms: 100 }),
    manifest: manifest(single),
    workers: injectedExpired,
    safetyProbe: exhaustedSafety(),
    now: () => 101,
  }), "root-deadline");
  assert.equal(injectedExpired[0].calls.length, 0);
}


async function testUnsupportedEnvelope() {
  await expectCode(
    () => api.normalizeRequest(request(1, { required_prefix: ["e2e4"] })),
    "root-prefix-unsupported",
  );
  await expectCode(
    () => api.normalizeRequest(request(1, {
      boundary: { chess960: true },
    })),
    "root-chess960-unsupported",
  );
  await expectCode(
    () => api.normalizeRequest(request(1, { width: 16_385 })),
    "root-request-invalid",
  );
  await expectCode(
    () => api.normalizeRequest(request(1, {
      boundary: { quiet_series: 1_000_001 },
    })),
    "root-boundary-invalid",
  );
  await expectCode(
    () => api.normalizeRequest(request(1, {
      boundary: { ep_targets: ["A1"] },
    })),
    "root-boundary-invalid",
  );
  await expectCode(
    () => api.normalizeRequest(request(1, {
      boundary: { ep_targets: ["a1", "a1"] },
    })),
    "root-boundary-invalid",
  );
  await expectCode(
    () => api.normalizeRequest(request(1, {
      boundary: { fen: `${"x".repeat(505)} w - - 0 1` },
    })),
    "root-boundary-invalid",
  );
  const maximumEnvelope = api.normalizeRequest(request(1, {
    width: 16_384,
    boundary: {
      quiet_series: 1_000_000,
      ep_targets: ["a1", "b2", "c3", "d4", "e5", "f6", "g7", "h8"],
    },
  }));
  assert.equal(maximumEnvelope.width, 16_384);
  assert.equal(maximumEnvelope.boundary.quiet_series, 1_000_000);
}


const streaming = await testStreamingWhiteAndStaleEpoch();
await testCertifiedInitialFullWave();
await testWhiteCanonicalTies();
await testBlackMirror();
await testTerminalProductionOrder();
await testSafetyRevisionAndBoundInvalidation();
await testResponseOrderPermutations();
await testAspirationWideningAndFallback();
await testProtocolFaults();
await testCapsCrashAndMemory();
await testCancellationAndDeadline();
await testUnsupportedEnvelope();

process.stdout.write(`${JSON.stringify({
  schema: "spc-root-iteration-coordinator-verifier-v1",
  scenarios: 17,
  response_order_permutations: 8,
  response_order_worker_count: 8,
  streaming_first_wave: true,
  certified_initial_full_wave_4_of_8: true,
  stale_epoch_revalidated: true,
  white_black_mirrored: true,
  canonical_ties: true,
  terminal_production_order: true,
  safety_revision_bound_invalidation: true,
  exact_aspiration_widening: true,
  aspiration_full_window_fallback: true,
  black_aspiration_mirror: true,
  same_worker_aspiration_retries: true,
  same_worker_threat_research: true,
  selected_owner_certification: true,
  malformed_missing_duplicate_unknown_fail_closed: true,
  root_series_boundary_mutations_fail_closed: true,
  dynamic_credit_required: true,
  lost_worker_full_charge: true,
  exact_at_cap_accepted: true,
  memory_admission_and_receipt_caps: true,
  cancellation_generation_invalidated: true,
  deadline_generation_invalidated: true,
  deadline_receipt_grace_without_extra_dispatch: true,
  monotonic_deadline_contract: true,
  full_artifact_identity_bound: true,
  prefix_hard_limits_mirrored: true,
  prefix_chess960_rejected: true,
  reference_winner: streaming.selected.candidate_identity,
  reference_score: streaming.selected.score,
})}\n`);
