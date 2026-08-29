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
const ORDINARY_CANDIDATE_TASK_KEYS = Object.freeze([
  "alpha", "beta", "call_work_credit", "candidate_identity", "certificate_id",
  "child_depth", "deadline_monotonic_ms", "engine_version", "enumeration_identity",
  "external_work", "generation", "incumbent_epoch", "iteration_id", "kernel_sha256",
  "mate_score", "module_js_sha256", "mover", "native_work_before", "order_index",
  "order_key", "profile_id", "purpose", "request_id", "ruleset_version",
  "runtime_variant", "safety_revision", "schema", "source_fingerprint", "task_id",
  "thread_count", "tt_persistence",
].sort());


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
  constructor(id, oracle, proofOracle, options = {}) {
    this.id = id;
    this.oracle = oracle;
    this.proofOracle = proofOracle;
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
      schema: task.schema === "spc-root-horizon-research-task-v1"
        ? "spc-root-horizon-research-result-v1"
        : "spc-root-candidate-result-v1",
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
      proof_bounds: [...(this.proofOracle.get(task.candidate_identity) ?? [-1, 1])],
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
  const proofOracle = new Map(
    definitions.map((item) => [item.id, item.proof ?? [-1, 1]]),
  );
  const events = options.events ?? [];
  return Array.from({ length: count }, (_, index) => new SyntheticWorker(
    `worker-${index}`,
    oracle,
    proofOracle,
    { ...options, events, ...(options.perWorker?.[index] || {}) },
  ));
}


function safetyReply(task, values) {
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
    ...values,
  };
}


function syntheticBoundary(series) {
  const white = series % 2 === 1;
  const fen = white ? WHITE_FEN : BLACK_FEN;
  return {
    fen,
    board_fen: fen,
    series,
    series_number: series,
    side_to_move: white ? "white" : "black",
    quiet_series: 0,
    quiet_draw_pending: false,
    ep_targets: [],
    progressive_ep: [],
    promoted_hex: "0000000000000000",
    chess960: false,
  };
}


function syntheticSeries(moves, childSeries, {
  outcome = null,
  endedByCheck = false,
} = {}) {
  return {
    moves: [...moves],
    machine_notation: moves.join("/"),
    transposition_count: 1,
    child_boundary: syntheticBoundary(childSeries),
    outcome,
    ended_by_check: endedByCheck,
  };
}


function exhaustedSafety(events = [], workUsed = 0) {
  return async (task) => {
    events.push({ event: "safety-probe", task });
    return safetyReply(task, {
      status: "exhausted",
      work_used: workUsed,
    });
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


async function testProofAwareRootSelection() {
  const policy = "exclude-proven-opponent-wins-unless-forced-v1";
  const white = [
    { id: "white-loss", key: "a2a3", score: 100, proof: [-1, -1] },
    { id: "white-safe", key: "b2b3", score: 10, proof: [-1, 1] },
  ];
  const whitePool = workers(1, white);
  const whiteResult = await api.runRootIteration({
    request: request(1, { iteration_id: "proof-aware-white" }),
    manifest: manifest(white),
    workers: whitePool,
    safetyProbe: exhaustedSafety(),
  });
  assert.equal(whiteResult.selected.candidate_identity, "white-safe");
  assert.equal(whiteResult.proof_selection_policy, policy);
  assert.equal(whiteResult.proof_aware_selection, true);
  assert.equal(whiteResult.proof_policy_filtered, true);
  assert.equal(whiteResult.proven_adverse_candidates, 1);
  assert.deepEqual(
    whitePool[0].calls
      .filter((task) => task.candidate_identity === "white-safe")
      .map((task) => task.purpose),
    ["scout", "threat-research", "selected-certification"],
  );

  const black = [
    { id: "black-loss", key: "a7a6", score: -100, proof: [1, 1] },
    { id: "black-safe", key: "b7b6", score: -10, proof: [-1, 1] },
  ];
  const blackPool = workers(1, black);
  const blackResult = await api.runRootIteration({
    request: request(1, { series: 2, iteration_id: "proof-aware-black" }),
    manifest: manifest(black, { white: false }),
    workers: blackPool,
    safetyProbe: exhaustedSafety(),
  });
  assert.equal(blackResult.selected.candidate_identity, "black-safe");
  assert.equal(blackResult.proof_policy_filtered, true);
  assert.deepEqual(
    blackPool[0].calls
      .filter((task) => task.candidate_identity === "black-safe")
      .map((task) => task.purpose),
    ["scout", "threat-research", "selected-certification"],
  );

  const forcedLoss = [
    { id: "worse", key: "a2a3", score: 90, proof: [-1, -1] },
    { id: "better", key: "b2b3", score: 100, proof: [-1, -1] },
  ];
  const forcedResult = await api.runRootIteration({
    request: request(1, { iteration_id: "proof-aware-forced-loss" }),
    manifest: manifest(forcedLoss),
    workers: workers(1, forcedLoss),
    safetyProbe: exhaustedSafety(),
  });
  assert.equal(forcedResult.selected.candidate_identity, "better");
  assert.equal(forcedResult.proof_policy_filtered, false);
  assert.equal(forcedResult.proven_adverse_candidates, 2);

  const record = (score, proofBounds, orderKey) => ({
    score,
    proofBounds,
    candidate: { order_key: orderKey },
  });
  assert(api.proofAwareRootPrecedes(
    record(10, [-1, 1], "z"),
    record(100, [-1, -1], "a"),
    true,
  ));
  assert(api.proofAwareRootPrecedes(
    record(-10, [-1, 1], "z"),
    record(-100, [1, 1], "a"),
    false,
  ));
  assert(api.proofAwareRootPrecedes(
    record(100, [-1, -1], "b"),
    record(90, [-1, -1], "a"),
    true,
  ));
}


async function testUnprovedMateClaimsAreQuarantinedForBothMovers() {
  const policy = "require-sign-matching-exact-proof-for-nonterminal-mate-band-v1";
  for (const white of [true, false]) {
    const claimScore = white ? MATE - 2 : -MATE + 2;
    const safeScore = white ? 200 : -200;
    const mismatchedProof = white ? [-1, -1] : [1, 1];
    for (const [proofName, proof] of [
      ["unknown", [-1, 1]],
      ["mismatched", mismatchedProof],
    ]) {
      const definitions = [
        { id: "claim", key: "a-claim", score: claimScore, proof },
        { id: "safe", key: "b-safe", score: safeScore },
      ];
      const pool = workers(1, definitions);
      const result = await api.runRootIteration({
        request: request(1, {
          series: white ? 1 : 2,
          iteration_id: `mate-claim-${white ? "white" : "black"}-${proofName}`,
        }),
        manifest: manifest(definitions, { white }),
        workers: pool,
        safetyProbe: exhaustedSafety(),
      });

      assert.equal(result.selected.candidate_identity, "safe");
      assert.equal(result.selected.score, safeScore);
      assert.equal(result.mate_claim_selection_policy, policy);
      assert.equal(result.mate_claim_policy_filtered, true);
      assert.equal(result.root_mate_claim_quarantines, 1);
      assert.equal(result.selection_policy_filtered, false);
      assert.equal(result.pv_horizon_candidate_vetoes, 0);
      assert.equal(result.coverage_scope, "selection-eligible-candidates");
      assert.equal(result.unfiltered_score_winner_selected, false);
      assert.deepEqual(result.mate_claim_quarantine_receipts, [{
        candidate_identity: "claim",
        quarantine_count: 1,
        score: claimScore,
        proof_bounds: proof,
        currently_quarantined: true,
      }]);
      assert.deepEqual(
        result.root_bounds.find((item) => item.candidate_identity === "claim"),
        {
          candidate_identity: "claim",
          bound: "exact",
          score: claimScore,
          proof_bounds: proof,
          selection_eligible: false,
          mate_claim_quarantined: true,
        },
      );
      assert(!pool[0].calls.some((task) => (
        task.candidate_identity === "claim" && task.purpose === "selected-certification"
      )));
    }
  }
}


async function testMateClaimsRequireProofAcrossExactAndRecertificationPaths() {
  for (const white of [true, false]) {
    const claimScore = white ? MATE - 2 : -MATE + 2;
    const matchingProof = white ? [1, 1] : [-1, -1];
    const definitions = [
      { id: "claim", key: "a-claim", score: claimScore, proof: matchingProof },
      { id: "safe", key: "b-safe", score: white ? 100 : -100 },
    ];
    const result = await api.runRootIteration({
      request: request(1, {
        series: white ? 1 : 2,
        iteration_id: `proved-mate-claim-${white ? "white" : "black"}`,
      }),
      manifest: manifest(definitions, { white }),
      workers: workers(1, definitions),
      safetyProbe: exhaustedSafety(),
    });
    assert.equal(result.selected.candidate_identity, "claim");
    assert.deepEqual(result.selected.proof_bounds, matchingProof);
    assert.equal(result.selected.mate_claim_quarantined, false);
    assert.equal(result.mate_claim_policy_filtered, false);
    assert.equal(result.root_mate_claim_quarantines, 0);
  }

  const recertDefinitions = [
    { id: "claim", key: "a-claim", score: MATE - 2, proof: [1, 1] },
    { id: "safe", key: "b-safe", score: 100 },
  ];
  const recertPool = workers(1, recertDefinitions, {
    mutate: (task, reply) => (
      task.candidate_identity === "claim" && task.purpose === "selected-certification"
        ? { ...reply, proof_bounds: [-1, 1] }
        : reply
    ),
  });
  const recertResult = await api.runRootIteration({
    request: request(1, { iteration_id: "mate-claim-owner-recertification" }),
    manifest: manifest(recertDefinitions),
    workers: recertPool,
    safetyProbe: exhaustedSafety(),
  });
  assert.equal(recertResult.selected.candidate_identity, "safe");
  assert.equal(recertResult.root_mate_claim_quarantines, 1);
  assert.deepEqual(
    recertPool[0].calls
      .filter((task) => task.candidate_identity === "claim")
      .map((task) => task.purpose),
    ["full", "selected-certification"],
  );

  const aspirationDefinitions = [
    { id: "claim", key: "a-claim", score: MATE - 10_000 },
    { id: "safe", key: "b-safe", score: MATE - 10_001 },
  ];
  const aspirationPool = workers(1, aspirationDefinitions);
  const aspirationResult = await api.runRootIteration({
    request: request(1, {
      iteration_id: "mate-claim-aspiration-exact",
      aspiration: { center_score: MATE - 10_000, initial_delta: 2_048 },
    }),
    manifest: manifest(aspirationDefinitions, { preferredSeries: ["e2e4"] }),
    workers: aspirationPool,
    safetyProbe: exhaustedSafety(),
  });
  assert.equal(aspirationResult.selected.candidate_identity, "safe");
  assert.equal(aspirationResult.root_mate_claim_quarantines, 1);
  assert.deepEqual(
    aspirationPool[0].calls
      .filter((task) => task.candidate_identity === "claim")
      .map((task) => task.purpose),
    ["aspiration"],
  );
}


async function testAllUnprovedMateClaimsFailClosedDistinctly() {
  const definitions = [
    { id: "unknown", key: "a-unknown", score: MATE - 2 },
    { id: "mismatched", key: "b-mismatched", score: MATE - 3, proof: [-1, -1] },
  ];
  let safetyCalls = 0;
  await assert.rejects(api.runRootIteration({
    request: request(1, { iteration_id: "all-mate-claims-quarantined" }),
    manifest: manifest(definitions),
    workers: workers(1, definitions),
    safetyProbe: async (task) => {
      safetyCalls += 1;
      return exhaustedSafety()(task);
    },
  }), (error) => {
    assert.equal(error?.code, "root-mate-claim-frontier-exhausted");
    assert.deepEqual(error?.details, {
      mate_claim_quarantines: 2,
      quarantined_candidates: ["mismatched", "unknown"],
    });
    assert.equal(error?.work?.committed_work, 2);
    return true;
  });
  assert.equal(safetyCalls, 0);
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
    assert.equal(result.selected.mate_claim_quarantined, false);
    assert.equal(result.mate_claim_policy_filtered, false);
    assert.equal(result.root_mate_claim_quarantines, 0);
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


async function testCheckedPvPolicyVeto() {
  const definitions = [
    { id: "a", key: "a2a3", score: 100 },
    { id: "b", key: "b2b3", score: 90 },
  ];
  const pool = workers(1, definitions);
  const safetyProbe = async (task) => {
    if (task.candidate_identity !== "a") {
      return safetyReply(task, { status: "exhausted", work_used: 1 });
    }
    return safetyReply(task, {
      status: "line-rejected",
      safety_scope: "pv-horizon",
      work_used: 2,
      line_rejection: {
        schema: "spc-pv-horizon-line-rejection-v1",
        reason: "adverse-immediate-series-mate",
        mate_ply: 6,
        horizon_series: "a1a2/a2a3/a3a4/a4a5/a5a6",
      },
      reply_mate: { outcome: "checkmate", moves: ["h8h1"] },
    });
  };
  const result = await api.runRootIteration({
    request: request(1),
    manifest: manifest(definitions),
    workers: pool,
    safetyProbe,
  });
  assert.equal(result.selected.candidate_identity, "b");
  assert.equal(result.selected.score, 90);
  assert.equal(result.selected.safety_override, false);
  assert.equal(
    result.selection_policy,
    "repair-once-then-veto-adverse-selected-pv-boundary-mates-v2",
  );
  assert.equal(result.selection_policy_filtered, true);
  assert.equal(result.pv_horizon_line_rejections, 1);
  assert.equal(result.coverage_scope, "selection-eligible-candidates");
  assert.equal(result.unfiltered_score_winner_selected, false);
  assert.equal(result.safety_revision, 1);
  assert.equal(result.safety_certified, true);
  assert.equal(result.work.safety_committed_work, 3);
  const rejected = result.root_bounds.find((item) => item.candidate_identity === "a");
  assert.equal(rejected.score, 100, "a line veto must not forge the searched score");
  assert.deepEqual(rejected.proof_bounds, [-1, 1]);
  assert.equal(rejected.selection_eligible, false);
  assert.equal(
    result.root_bounds.find((item) => item.candidate_identity === "b")?.selection_eligible,
    true,
  );
  assert.deepEqual(
    result.tasks.filter((item) => item.event === "safety")
      .map((item) => [item.candidate_identity, item.status, item.safety_scope]),
    [
      ["a", "line-rejected", "pv-horizon"],
      ["b", "exhausted", null],
    ],
  );

  const single = [{ id: "a", key: "a2a3", score: 100 }];
  await assert.rejects(api.runRootIteration({
    request: request(1, { iteration_id: "all-policy-rejected" }),
    manifest: manifest(single),
    workers: workers(1, single),
    safetyProbe,
  }), (error) => {
    assert.equal(error?.code, "root-policy-frontier-exhausted");
    assert.equal(error?.work?.safety_committed_work, 2);
    return true;
  });
}


async function testCheckedPvProofRepairsOnlyTheSelectedCandidate() {
  const definitions = [
    { id: "a", key: "a2a3", score: 100 },
    { id: "b", key: "b2b3", score: 90 },
  ];
  const checkedPv = [
    syntheticSeries(["a7a6", "b8c6"], 3),
    syntheticSeries(["g1f3", "b1c3", "f1b5"], 4),
    syntheticSeries(["d7d6", "c8d7", "d8c8", "e8d8"], 5),
    syntheticSeries(["b5c6", "f3e5", "c3d5", "d1h5", "h5f7"], 6, {
      endedByCheck: true,
    }),
  ];
  checkedPv[1].transposition_count = 9;
  const mateReply = syntheticSeries(
    ["d8e7", "c8c1", "c1e1", "e1e2", "e2e1", "e1h1"],
    7,
    { outcome: "checkmate", endedByCheck: true },
  );
  const pool = workers(1, definitions, {
    mutate: (task, reply) => {
      if (task.schema === "spc-root-horizon-research-task-v1") {
        assert.equal(task.candidate_identity, "a");
        assert.equal(task.purpose, "horizon-research");
        assert.equal(task.tt_persistence, "commit");
        assert.equal(task.alpha, -2 * MATE);
        assert.equal(task.beta, 2 * MATE);
        assert.equal(task.horizon_proofs.length, 1);
        assert.equal(task.horizon_proofs[0].rooted_path[0].transposition_count, 7);
        assert.equal(task.horizon_proofs[0].rooted_path[2].transposition_count, 9);
        assert.equal(task.horizon_proofs[0].mate_reply.transposition_count, 1);
        return {
          ...reply,
          schema: "spc-root-horizon-research-result-v1",
          score: 80,
          child_pv: [],
          horizon_proof_set_identity: "spc-horizon-proof-set-v1|synthetic",
          horizon_proofs_validated: 1,
          horizon_proof_hits: 1,
          horizon_proof_hit_mask: 1,
        };
      }
      assert.equal(task.schema, "spc-root-candidate-task-v1");
      assert.deepEqual(Object.keys(task).sort(), ORDINARY_CANDIDATE_TASK_KEYS);
      if (task.candidate_identity === "a") {
        return { ...reply, child_pv: checkedPv };
      }
      return reply;
    },
  });
  let rejectedA = false;
  const retainedManifest = manifest(definitions);
  retainedManifest.candidates[0].root_series.transposition_count = 7;
  const result = await api.runRootIteration({
    request: request(1, { iteration_id: "checked-pv-native-repair" }),
    manifest: retainedManifest,
    workers: pool,
    safetyProbe: async (task) => {
      if (task.candidate_identity !== "a" || rejectedA) {
        return safetyReply(task, { status: "exhausted", work_used: 1 });
      }
      rejectedA = true;
      return safetyReply(task, {
        status: "line-rejected",
        safety_scope: "pv-horizon",
        work_used: 2,
        line_rejection: {
          schema: "spc-pv-horizon-line-rejection-v1",
          reason: "adverse-immediate-series-mate",
          mate_ply: 6,
          horizon_series: checkedPv.at(-1).machine_notation,
        },
        reply_mate: mateReply,
        horizon_proof: {
          schema: "spc-retained-root-horizon-proof-v1",
          rooted_path: [task.candidate.root_series, ...checkedPv],
          mate_reply: mateReply,
        },
      });
    },
  });

  assert.equal(result.selected.candidate_identity, "b");
  assert.equal(result.selected.score, 90);
  assert.equal(result.pv_horizon_line_rejections, 1);
  assert.equal(result.pv_horizon_native_repairs, 1);
  assert.equal(result.pv_horizon_candidate_vetoes, 0);
  assert.equal(result.selection_policy_filtered, false);
  assert.equal(
    result.root_bounds.find((item) => item.candidate_identity === "a")?.score,
    80,
  );
  assert.equal(
    result.root_bounds.find((item) => item.candidate_identity === "a")?.selection_eligible,
    true,
  );
  const horizonCalls = pool[0].calls.filter(
    (task) => task.schema === "spc-root-horizon-research-task-v1",
  );
  assert.equal(horizonCalls.length, 1);
  assert.deepEqual(horizonCalls[0].horizon_proofs[0].rooted_path.slice(1), checkedPv);
  assert.equal(
    pool[0].calls.filter((task) => (
      task.candidate_identity === "b" && task.purpose === "full"
    )).length,
    0,
    "repair must not force a full search of every challenger",
  );
}


async function runRepairedWinnerRecertification(mode) {
  const definitions = [
    { id: "a", key: "a2a3", score: 100 },
    { id: "b", key: "b2b3", score: 90 },
  ];
  const checkedPv = [
    syntheticSeries(["a7a6", "b8c6"], 3),
    syntheticSeries(["g1f3", "b1c3", "f1b5"], 4),
    syntheticSeries(["d7d6", "c8d7", "d8c8", "e8d8"], 5),
    syntheticSeries(["b5c6", "f3e5", "c3d5", "d1h5", "h5f7"], 6, {
      endedByCheck: true,
    }),
  ];
  const mateReply = syntheticSeries(
    ["d8e7", "c8c1", "c1e1", "e1e2", "e2e1", "e1h1"],
    7,
    { outcome: "checkmate", endedByCheck: true },
  );
  let horizonCalls = 0;
  const pool = workers(1, definitions, {
    mutate: (task, reply) => {
      if (task.schema === "spc-root-horizon-research-task-v1") {
        horizonCalls += 1;
        assert.equal(task.candidate_identity, "a");
        assert.equal(task.horizon_proofs.length, 1);
        const completed = {
          ...reply,
          schema: "spc-root-horizon-research-result-v1",
          score: 95,
          child_pv: [],
          horizon_proof_set_identity: "spc-horizon-proof-set-v1|same-owner",
          horizon_proofs_validated: 1,
          horizon_proof_hits: horizonCalls === 1 ? 1 : 0,
          horizon_proof_hit_mask: horizonCalls === 1 ? 1 : 0,
        };
        if (horizonCalls !== 2 || mode === "complete") return completed;
        return {
          ...completed,
          status: mode,
          bound: "unknown",
          score: 0,
          child_pv: [],
          horizon_proof_set_identity: "",
          horizon_proofs_validated: 0,
          horizon_proof_hits: 0,
          horizon_proof_hit_mask: 0,
        };
      }
      if (task.candidate_identity === "a") return { ...reply, child_pv: checkedPv };
      return reply;
    },
  });
  let safetyCalls = 0;
  const result = await api.runRootIteration({
    request: request(1, { iteration_id: "checked-pv-owner-recertification" }),
    manifest: manifest(definitions),
    workers: pool,
    safetyProbe: async (task) => {
      safetyCalls += 1;
      if (safetyCalls > 1) {
        return safetyReply(task, { status: "exhausted", work_used: 1 });
      }
      return safetyReply(task, {
        status: "line-rejected",
        safety_scope: "pv-horizon",
        work_used: 2,
        line_rejection: {
          schema: "spc-pv-horizon-line-rejection-v1",
          reason: "adverse-immediate-series-mate",
          mate_ply: 6,
          horizon_series: checkedPv.at(-1).machine_notation,
        },
        reply_mate: mateReply,
        horizon_proof: {
          schema: "spc-retained-root-horizon-proof-v1",
          rooted_path: [task.candidate.root_series, ...checkedPv],
          mate_reply: mateReply,
        },
      });
    },
  });

  return { checkedPv, horizonCalls, pool, result };
}


async function testRepairedWinnerRecertifiesWithTheSameProofSet() {
  const { checkedPv, horizonCalls, pool, result } = await runRepairedWinnerRecertification(
    "complete",
  );
  assert.equal(result.selected.candidate_identity, "a");
  assert.equal(result.selected.score, 95);
  assert.equal(result.pv_horizon_native_repairs, 1);
  assert.equal(result.pv_horizon_candidate_vetoes, 0);
  assert.equal(horizonCalls, 2, "repair and warm owner certification must both carry proofs");
  const calls = pool[0].calls.filter(
    (task) => task.schema === "spc-root-horizon-research-task-v1",
  );
  assert.deepEqual(calls[1].horizon_proofs, calls[0].horizon_proofs);
  assert.equal(
    pool[0].calls.filter((task) => (
      task.candidate_identity === "a" && task.purpose === "selected-certification"
    )).length,
    1,
    "only the pre-proof owner certification may use ordinary candidate v1",
  );
}


async function testFailedRepairedWinnerRecertificationReclassifiesTheLastRepair() {
  for (const mode of ["work_limit", "unsupported"]) {
    const { horizonCalls, result } = await runRepairedWinnerRecertification(mode);
    assert.equal(result.selected.candidate_identity, "b", mode);
    assert.equal(result.pv_horizon_line_rejections, 1, mode);
    assert.equal(result.pv_horizon_native_repairs, 0, mode);
    assert.equal(result.pv_horizon_candidate_vetoes, 1, mode);
    assert.equal(
      result.pv_horizon_native_repairs + result.pv_horizon_candidate_vetoes,
      result.pv_horizon_line_rejections,
      mode,
    );
    assert.equal(result.selection_policy_filtered, true, mode);
    assert.equal(horizonCalls, 2, mode);
  }
}


async function runSecondCheckedPvProofScenario({ invalidReceipt = null } = {}) {
  const definitions = [
    { id: "a", key: "a2a3", score: 100 },
    { id: "b", key: "b2b3", score: 90 },
  ];
  const firstPv = [
    syntheticSeries(["a7a6", "b8c6"], 3),
    syntheticSeries(["g1f3", "b1c3", "f1b5"], 4),
    syntheticSeries(["d7d6", "c8d7", "d8c8", "e8d8"], 5),
    syntheticSeries(["b5c6", "f3e5", "c3d5", "d1h5", "h5f7"], 6, {
      endedByCheck: true,
    }),
  ];
  const secondPv = [
    syntheticSeries(["a7a5", "b8a6"], 3),
    syntheticSeries(["g1h3", "b1a3", "h1g1"], 4),
    syntheticSeries(["d7d5", "c8f5", "d8d7", "e8d8"], 5),
    syntheticSeries(["h3g5", "a3b5", "g1h1", "d1e1", "e1e7"], 6, {
      endedByCheck: true,
    }),
  ];
  const firstMate = syntheticSeries(
    ["d8e7", "c8c1", "c1e1", "e1e2", "e2e1", "e1h1"],
    7,
    { outcome: "checkmate", endedByCheck: true },
  );
  const secondMate = syntheticSeries(
    ["d8c8", "f5c2", "c2d1", "d1a4", "a4e8", "e8e1"],
    7,
    { outcome: "checkmate", endedByCheck: true },
  );
  const proofSetCalls = [];
  const pool = workers(1, definitions, {
    mutate: (task, reply) => {
      if (task.schema === "spc-root-horizon-research-task-v1") {
        proofSetCalls.push(task.horizon_proofs);
        const proofCount = task.horizon_proofs.length;
        const firstForSet = proofSetCalls.filter((set) => set.length === proofCount).length === 1;
        const invalidFirstReceipt = invalidReceipt !== null && proofCount === 1 && firstForSet;
        return {
          ...reply,
          schema: "spc-root-horizon-research-result-v1",
          score: proofCount === 1 ? 98 : 92,
          child_pv: proofCount === 1 ? secondPv : [],
          horizon_proof_set_identity: `spc-horizon-proof-set-v1|${proofCount}`,
          horizon_proofs_validated: proofCount,
          horizon_proof_hits: invalidFirstReceipt && invalidReceipt === "population"
            ? 2
            : firstForSet ? 1 : 0,
          horizon_proof_hit_mask: invalidFirstReceipt && invalidReceipt === "mask"
            ? 0x1_0000
            : firstForSet ? 2 ** (proofCount - 1) : 0,
        };
      }
      if (task.candidate_identity === "a") return { ...reply, child_pv: firstPv };
      return reply;
    },
  });
  let safetyCall = 0;
  const resultPromise = api.runRootIteration({
    request: request(1, { iteration_id: "checked-pv-second-proof-bit" }),
    manifest: manifest(definitions),
    workers: pool,
    safetyProbe: async (task) => {
      safetyCall += 1;
      if (safetyCall > 2) {
        return safetyReply(task, { status: "exhausted", work_used: 1 });
      }
      const pv = safetyCall === 1 ? firstPv : secondPv;
      const mate = safetyCall === 1 ? firstMate : secondMate;
      assert.deepEqual(task.candidate.child_pv, pv);
      return safetyReply(task, {
        status: "line-rejected",
        safety_scope: "pv-horizon",
        work_used: 2,
        line_rejection: {
          schema: "spc-pv-horizon-line-rejection-v1",
          reason: "adverse-immediate-series-mate",
          mate_ply: 6,
          horizon_series: pv.at(-1).machine_notation,
        },
        reply_mate: mate,
        horizon_proof: {
          schema: "spc-retained-root-horizon-proof-v1",
          rooted_path: [task.candidate.root_series, ...pv],
          mate_reply: mate,
        },
      });
    },
  });

  if (invalidReceipt !== null) {
    await assert.rejects(resultPromise, (error) => {
      assert.equal(error?.code, "root-worker-result-invalid");
      return true;
    });
    return proofSetCalls;
  }
  const result = await resultPromise;
  assert.equal(result.selected.candidate_identity, "b");
  assert.equal(result.selected.score, 90);
  assert.equal(result.pv_horizon_line_rejections, 2);
  assert.equal(result.pv_horizon_native_repairs, 1);
  assert.equal(result.pv_horizon_candidate_vetoes, 1);
  assert.deepEqual(
    proofSetCalls.map((set) => set.length),
    [1, 1],
    "the second distinct checked-PV mate must veto before a second repair dispatch",
  );
  assert.deepEqual(result.same_root_repair_policy, {
    schema: "spc-same-root-horizon-repair-policy-v1",
    maximum_successful_same_root_repairs: 1,
  });
  assert.deepEqual(result.pv_horizon_policy_vetoes, [{
    schema: "spc-pv-horizon-candidate-veto-v1",
    candidate_identity: "a",
    reason: "same-root-repair-limit",
    maximum_successful_same_root_repairs: 1,
    repairs_before_veto: 1,
    retained_proofs_before_veto: 1,
    distinct_proofs_observed: 2,
  }]);
  return proofSetCalls;
}


async function testSecondDistinctCheckedPvProofVetoesAtSameRootRepairLimit() {
  await runSecondCheckedPvProofScenario();
}


async function testImpossibleProofHitPopulationFailsClosed() {
  const calls = await runSecondCheckedPvProofScenario({ invalidReceipt: "population" });
  assert.deepEqual(calls.map((set) => set.length), [1]);
}


async function testDuplicateCheckedPvProofFallsBackToCandidateVeto() {
  const definitions = [
    { id: "a", key: "a2a3", score: 100 },
    { id: "b", key: "b2b3", score: 90 },
  ];
  const checkedPv = [
    syntheticSeries(["a7a6", "b8c6"], 3),
    syntheticSeries(["g1f3", "b1c3", "f1b5"], 4),
    syntheticSeries(["d7d6", "c8d7", "d8c8", "e8d8"], 5),
    syntheticSeries(["b5c6", "f3e5", "c3d5", "d1h5", "h5f7"], 6, {
      endedByCheck: true,
    }),
  ];
  const mateReply = syntheticSeries(
    ["d8e7", "c8c1", "c1e1", "e1e2", "e2e1", "e1h1"],
    7,
    { outcome: "checkmate", endedByCheck: true },
  );
  let horizonCalls = 0;
  const pool = workers(1, definitions, {
    mutate: (task, reply) => {
      if (task.schema === "spc-root-horizon-research-task-v1") {
        horizonCalls += 1;
        return {
          ...reply,
          schema: "spc-root-horizon-research-result-v1",
          score: 100,
          child_pv: checkedPv,
          horizon_proof_set_identity: "spc-horizon-proof-set-v1|duplicate",
          horizon_proofs_validated: 1,
          horizon_proof_hits: horizonCalls === 1 ? 1 : 0,
          horizon_proof_hit_mask: horizonCalls === 1 ? 1 : 0,
        };
      }
      if (task.candidate_identity === "a") return { ...reply, child_pv: checkedPv };
      return reply;
    },
  });
  let aSafetyCalls = 0;
  const result = await api.runRootIteration({
    request: request(1, { iteration_id: "checked-pv-duplicate-proof" }),
    manifest: manifest(definitions),
    workers: pool,
    safetyProbe: async (task) => {
      if (task.candidate_identity !== "a" || aSafetyCalls >= 2) {
        return safetyReply(task, { status: "exhausted", work_used: 1 });
      }
      aSafetyCalls += 1;
      return safetyReply(task, {
        status: "line-rejected",
        safety_scope: "pv-horizon",
        work_used: 2,
        line_rejection: {
          schema: "spc-pv-horizon-line-rejection-v1",
          reason: "adverse-immediate-series-mate",
          mate_ply: 6,
          horizon_series: checkedPv.at(-1).machine_notation,
        },
        reply_mate: mateReply,
        horizon_proof: {
          schema: "spc-retained-root-horizon-proof-v1",
          rooted_path: [task.candidate.root_series, ...checkedPv],
          mate_reply: mateReply,
        },
      });
    },
  });

  assert.equal(result.selected.candidate_identity, "b");
  assert.equal(result.pv_horizon_line_rejections, 2);
  assert.equal(result.pv_horizon_native_repairs, 1);
  assert.equal(result.pv_horizon_candidate_vetoes, 1);
  assert.equal(horizonCalls, 2, "duplicate proof must not dispatch a third native search");
}


async function testSixteenBitProofMaskProtocolLimitRemainsFailClosed() {
  const calls = await runSecondCheckedPvProofScenario({ invalidReceipt: "mask" });
  assert.deepEqual(
    calls.map((set) => set.length),
    [1],
    "a proof hit outside the retained-proof protocol mask must fail closed",
  );
}


async function testHorizonResearchFallbacksAndMalformedReceipt() {
  const definitions = [
    { id: "a", key: "a2a3", score: 100 },
    { id: "b", key: "b2b3", score: 90 },
  ];
  const checkedPv = [
    syntheticSeries(["a7a6", "b8c6"], 3),
    syntheticSeries(["g1f3", "b1c3", "f1b5"], 4),
    syntheticSeries(["d7d6", "c8d7", "d8c8", "e8d8"], 5),
    syntheticSeries(["b5c6", "f3e5", "c3d5", "d1h5", "h5f7"], 6, {
      endedByCheck: true,
    }),
  ];
  const mateReply = syntheticSeries(
    ["d8e7", "c8c1", "c1e1", "e1e2", "e2e1", "e1h1"],
    7,
    { outcome: "checkmate", endedByCheck: true },
  );
  const run = async (mode) => {
    const pool = workers(1, definitions, {
      throwWhen: (task) => (
        mode === "lost" && task.schema === "spc-root-horizon-research-task-v1"
      ),
      mutate: (task, reply) => {
        if (task.schema === "spc-root-horizon-research-task-v1") {
          const common = {
            ...reply,
            schema: "spc-root-horizon-research-result-v1",
            score: 80,
            child_pv: [],
            horizon_proof_set_identity: "spc-horizon-proof-set-v1|fallback",
            horizon_proofs_validated: 1,
            horizon_proof_hits: mode === "zero-hit" ? 0 : 1,
            horizon_proof_hit_mask: mode === "zero-hit" ? 0 : 1,
          };
          if (["work-limit", "unsupported", "deadline"].includes(mode)) {
            return {
              ...common,
              status: mode === "work-limit" ? "work_limit" : mode,
              bound: "unknown",
              score: 0,
              child_pv: [],
              horizon_proof_set_identity: "",
              horizon_proofs_validated: 0,
              horizon_proof_hits: 0,
              horizon_proof_hit_mask: 0,
            };
          }
          if (mode === "malformed") {
            return { ...common, horizon_proofs_validated: 0 };
          }
          if (mode === "stale") {
            return { ...common, safety_revision: task.safety_revision - 1 };
          }
          if (mode === "identity-mismatch") {
            return { ...common, kernel_sha256: "f".repeat(64) };
          }
          if (mode === "malformed-deadline") {
            return { ...common, status: "deadline", bound: "unknown" };
          }
          return common;
        }
        if (task.candidate_identity === "a") return { ...reply, child_pv: checkedPv };
        return reply;
      },
    });
    const resultPromise = api.runRootIteration({
      request: request(1, { iteration_id: `checked-pv-${mode}` }),
      manifest: manifest(definitions),
      workers: pool,
      safetyProbe: async (task) => (
        task.candidate_identity === "a"
          ? safetyReply(task, {
            status: "line-rejected",
            safety_scope: "pv-horizon",
            work_used: 2,
            line_rejection: {
              schema: "spc-pv-horizon-line-rejection-v1",
              reason: "adverse-immediate-series-mate",
              mate_ply: 6,
              horizon_series: checkedPv.at(-1).machine_notation,
            },
            reply_mate: mateReply,
            horizon_proof: {
              schema: "spc-retained-root-horizon-proof-v1",
              rooted_path: [task.candidate.root_series, ...checkedPv],
              mate_reply: mateReply,
            },
          })
          : safetyReply(task, { status: "exhausted", work_used: 1 })
      ),
    });
    return { pool, resultPromise };
  };

  for (const mode of ["zero-hit", "work-limit", "unsupported"]) {
    const { resultPromise } = await run(mode);
    const result = await resultPromise;
    assert.equal(result.selected.candidate_identity, "b");
    assert.equal(result.pv_horizon_native_repairs, 0);
    assert.equal(result.pv_horizon_candidate_vetoes, 1);
    assert.equal(result.selection_policy_filtered, true);
    const rejected = result.root_bounds.find((item) => item.candidate_identity === "a");
    assert.equal(rejected.score, 100, `${mode} must not publish an unproven repair score`);
    assert.equal(rejected.selection_eligible, false);
  }

  for (const mode of ["malformed", "stale", "identity-mismatch", "malformed-deadline"]) {
    const { resultPromise } = await run(mode);
    await assert.rejects(resultPromise, (error) => {
      assert.equal(error?.code, "root-worker-result-invalid", mode);
      return true;
    });
  }
  const { pool: deadlinePool, resultPromise: deadline } = await run("deadline");
  await assert.rejects(deadline, (error) => {
    assert.equal(error?.code, "root-deadline");
    assert.equal(error?.work?.within_cap, true);
    return true;
  });
  assert.equal(deadlinePool[0].cancelCalls.length, 1);
  const { resultPromise: lost } = await run("lost");
  await assert.rejects(lost, (error) => {
    assert.equal(error?.code, "root-worker-lost");
    return true;
  });
}


async function testNoSearchCreditFallsBackWithoutSpendingHeldSafetyWork() {
  const definitions = [
    { id: "a", key: "a2a3", score: 100 },
    { id: "b", key: "b2b3", score: 0, terminalScore: 0, terminalProof: [0, 0] },
  ];
  const checkedPv = [
    syntheticSeries(["a7a6", "b8c6"], 3),
    syntheticSeries(["g1f3", "b1c3", "f1b5"], 4),
    syntheticSeries(["d7d6", "c8d7", "d8c8", "e8d8"], 5),
    syntheticSeries(["b5c6", "f3e5", "c3d5", "d1h5", "h5f7"], 6, {
      endedByCheck: true,
    }),
  ];
  const mateReply = syntheticSeries(
    ["d8e7", "c8c1", "c1e1", "e1e2", "e2e1", "e1h1"],
    7,
    { outcome: "checkmate", endedByCheck: true },
  );
  const retainedManifest = manifest(definitions);
  retainedManifest.candidates[1].root_series.outcome = "stalemate";
  retainedManifest.candidates[1].root_series.ended_by_check = false;
  const pool = workers(1, definitions, {
    mutate: (task, reply) => (
      task.candidate_identity === "a" ? { ...reply, child_pv: checkedPv } : reply
    ),
  });
  const result = await api.runRootIteration({
    request: request(1, {
      iteration_id: "checked-pv-no-research-credit",
      caps: {
        max_work: 7,
        initial_work: 0,
        safety_reserve_work: 5,
        search_call_work_credit: 7,
        safety_call_work_credit: 5,
        max_memory_bytes: 1_024,
      },
    }),
    manifest: retainedManifest,
    workers: pool,
    safetyProbe: async (task) => safetyReply(task, {
      status: "line-rejected",
      safety_scope: "pv-horizon",
      work_used: 2,
      line_rejection: {
        schema: "spc-pv-horizon-line-rejection-v1",
        reason: "adverse-immediate-series-mate",
        mate_ply: 6,
        horizon_series: checkedPv.at(-1).machine_notation,
      },
      reply_mate: mateReply,
      horizon_proof: {
        schema: "spc-retained-root-horizon-proof-v1",
        rooted_path: [task.candidate.root_series, ...checkedPv],
        mate_reply: mateReply,
      },
    }),
  });

  assert.equal(result.selected.candidate_identity, "b");
  assert.equal(result.pv_horizon_native_repairs, 0);
  assert.equal(result.pv_horizon_candidate_vetoes, 1);
  assert.equal(result.work.safety_committed_work, 2);
  assert.equal(result.work.remaining_work, 3);
  assert.equal(
    pool[0].calls.some((task) => task.schema === "spc-root-horizon-research-task-v1"),
    false,
  );
}


async function testBlackCheckedPvRepairReversesTheCorrectionDirection() {
  const definitions = [
    { id: "a", key: "a7a6", score: -100 },
    { id: "b", key: "b7b6", score: -90 },
  ];
  const checkedPv = [
    syntheticSeries(["a2a3", "b1c3", "g1f3"], 4),
    syntheticSeries(["a7a6", "b8c6", "g8f6", "h7h6"], 5),
    syntheticSeries(["d2d4", "c1f4", "d1d2", "e1d1", "d2e3"], 6),
    syntheticSeries(["d7d6", "c8d7", "d8c8", "e8d8", "f6e4", "e4f2"], 7, {
      endedByCheck: true,
    }),
  ];
  const mateReply = syntheticSeries(
    ["d1e1", "f4c7", "c7d8", "d8c8", "c8e8", "e8e7", "e7f7"],
    8,
    { outcome: "checkmate", endedByCheck: true },
  );
  const pool = workers(1, definitions, {
    mutate: (task, reply) => {
      if (task.schema === "spc-root-horizon-research-task-v1") {
        return {
          ...reply,
          schema: "spc-root-horizon-research-result-v1",
          score: -80,
          child_pv: [],
          horizon_proof_set_identity: "spc-horizon-proof-set-v1|black",
          horizon_proofs_validated: 1,
          horizon_proof_hits: 1,
          horizon_proof_hit_mask: 1,
        };
      }
      if (task.candidate_identity === "a") return { ...reply, child_pv: checkedPv };
      return reply;
    },
  });
  let rejected = false;
  const result = await api.runRootIteration({
    request: request(1, { series: 2, iteration_id: "checked-pv-black-repair" }),
    manifest: manifest(definitions, { white: false }),
    workers: pool,
    safetyProbe: async (task) => {
      if (task.candidate_identity !== "a" || rejected) {
        return safetyReply(task, { status: "exhausted", work_used: 1 });
      }
      rejected = true;
      return safetyReply(task, {
        status: "line-rejected",
        safety_scope: "pv-horizon",
        work_used: 2,
        line_rejection: {
          schema: "spc-pv-horizon-line-rejection-v1",
          reason: "adverse-immediate-series-mate",
          mate_ply: 6,
          horizon_series: checkedPv.at(-1).machine_notation,
        },
        reply_mate: mateReply,
        horizon_proof: {
          schema: "spc-retained-root-horizon-proof-v1",
          rooted_path: [task.candidate.root_series, ...checkedPv],
          mate_reply: mateReply,
        },
      });
    },
  });

  assert.equal(result.mover, "black");
  assert.equal(result.selected.candidate_identity, "b");
  assert.equal(result.pv_horizon_native_repairs, 1);
  assert.equal(result.pv_horizon_candidate_vetoes, 0);
  assert.equal(result.root_bounds.find((item) => item.candidate_identity === "a")?.score, -80);
  assert.equal(
    pool[0].calls.filter((task) => (
      task.candidate_identity === "b" && task.purpose === "full"
    )).length,
    0,
  );
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

  const wrongSchemaPool = workers(1, single, {
    mutate: (_task, reply) => ({ ...reply, schema: "spc-root-candidate-result-v0" }),
  });
  await expectCode(api.runRootIteration({
    request: request(1),
    manifest: manifest(single),
    workers: wrongSchemaPool,
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

  const nativeDeadlinePool = workers(1, single, {
    mutate: (_task, reply) => ({
      ...reply,
      status: "deadline",
      bound: "unknown",
      score: 0,
      child_pv: [],
    }),
  });
  await assert.rejects(api.runRootIteration({
    request: request(1),
    manifest: manifest(single),
    workers: nativeDeadlinePool,
    safetyProbe: exhaustedSafety(),
  }), (error) => {
    assert.equal(error?.code, "root-deadline");
    assert.equal(error?.work?.within_cap, true);
    return true;
  });
  assert.equal(nativeDeadlinePool[0].cancelCalls.length, 1);

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


async function testSelectedRootSingleReplyLadderGate() {
  const definitions = [
    { id: "ladder-losing", key: "a2a3", score: 100 },
    { id: "ladder-safe", key: "b2b3", score: 50 },
  ];
  const candidateManifest = manifest(definitions);
  candidateManifest.candidates = candidateManifest.candidates.map((candidate) => ({
    ...candidate,
    root_series: {
      ...candidate.root_series,
      child_boundary: syntheticBoundary(8),
      ended_by_check: true,
    },
  }));
  const makeProof = (child) => ({
    schema: "spc-single-reply-mate-ladder-proof-v1",
    root_child_boundary: child,
    forced_reply_unique_legal_move: true,
    attack: syntheticSeries(["e7e5"], 9, { endedByCheck: true }),
    forced_reply: syntheticSeries(["e2e4"], 10, { endedByCheck: true }),
    mate: syntheticSeries(["d8h4"], 11, {
      outcome: "checkmate",
      endedByCheck: true,
    }),
  });
  const makeReceipt = (task, status, proof, nativeStatus = status) => ({
    schema: "spc-selected-root-single-reply-mate-ladder-receipt-v1",
    status,
    proof_status: status,
    native_status: nativeStatus,
    native_message: `synthetic ${status}`,
    native_stats: status === "unknown" ? null : {
      attack_positions_visited: status === "found" ? 1 : 0,
      attack_moves_generated: 0,
      reply_positions_visited: 0,
      reply_moves_generated: 0,
      mate_positions_visited: 0,
      mate_moves_generated: 0,
      attack_transpositions_merged: 0,
      mate_transpositions_merged: 0,
      checking_series: 0,
      forced_counterchecks: 0,
      mate_probes: 0,
      peak_attack_frontier: 0,
      attack_max_depth_reached: 0,
      mate_max_depth_reached: 0,
      work_used: status === "found" ? 1 : 0,
    },
    cache_hit: false,
    call_work_credit: 1,
    work_used: status === "found" ? 1 : 0,
    source_fingerprint: task.source_fingerprint,
    kernel_sha256: task.kernel_sha256,
    module_js_sha256: task.module_js_sha256,
    certificate_id: task.certificate_id,
    mate_certificate_id: "spc-synthetic-mate-v1",
    prefix_certificate_id: "spc-synthetic-prefix-v1",
    request_id: task.request_id,
    iteration_id: task.iteration_id,
    safety_revision: task.safety_revision,
    candidate_identity: task.candidate_identity,
    root_child_boundary: task.candidate.root_series.child_boundary,
    proof,
  });
  const ladderSafety = async (task) => {
    const found = task.candidate_identity === "ladder-losing";
    const proof = found ? makeProof(task.candidate.root_series.child_boundary) : null;
    const receipt = makeReceipt(task, found ? "found" : "exhausted", proof);
    return safetyReply(task, {
      status: found ? "found" : "exhausted",
      work_used: receipt.work_used,
      safety_scope: "selected-root-single-reply-mate-ladder",
      single_reply_mate_ladder: receipt,
      ...(found ? {
        override_score: -MATE + 4,
        proof_bounds: [-1, -1],
        ladder_proof: proof,
      } : {}),
    });
  };
  const result = await api.runRootIteration({
    request: request(2, { series: 7 }),
    manifest: candidateManifest,
    workers: workers(2, definitions),
    safetyProbe: ladderSafety,
  });
  assert.equal(result.selected.candidate_identity, "ladder-safe");
  assert.equal(result.tasks.filter((event) => (
    event.event === "safety" && event.single_reply_mate_ladder !== null
  )).length, 2);
  assert.equal(result.tasks.find((event) => (
    event.event === "safety" && event.status === "found"
  )).single_reply_mate_ladder.proof.forced_reply.moves.length, 1);
  await expectCode(api.runRootIteration({
    request: request(2, { series: 7 }),
    manifest: candidateManifest,
    workers: workers(2, definitions),
    safetyProbe: async (task) => {
      const proof = makeProof(task.candidate.root_series.child_boundary);
      const receipt = makeReceipt(task, "found", proof);
      return safetyReply(task, {
        status: "found",
        work_used: 0,
        safety_scope: "selected-root-single-reply-mate-ladder",
        single_reply_mate_ladder: receipt,
        override_score: -MATE + 4,
        proof_bounds: [-1, -1],
        ladder_proof: proof,
      });
    },
  }), "root-safety-result-invalid");

  const exactBoundary = (fen, series, promotedHex) => ({
    fen,
    board_fen: fen,
    series,
    series_number: series,
    side_to_move: series % 2 === 1 ? "white" : "black",
    quiet_series: 0,
    quiet_draw_pending: false,
    ep_targets: [],
    progressive_ep: [],
    promoted_hex: promotedHex,
    chess960: false,
  });
  const recordedRootFen =
    "Nnb1kbnr/pppp2pp/4p3/5p2/8/3P4/PPPKPPPP/3R1BNR b k - 1 7";
  const recordedChild = exactBoundary(
    "Nnb1kbnr/pppp2pp/4p3/8/5q2/3P4/PPPKPP2/3R1BN1 w k - 1 13",
    7,
    "0000000020000000",
  );
  const recordedAttack = {
    moves: ["d2c3", "d3d4", "d4d5", "d5e6", "d1d7", "a8c7"],
    machine_notation: "d2c3/d3d4/d4d5/d5e6/d1d7/a8c7",
    transposition_count: 1,
    child_boundary: exactBoundary(
      "1nb1kbnr/ppNR2pp/4P3/8/5q2/2K5/PPP1PP2/5BN1 b k - 0 13",
      8,
      "0000000020000000",
    ),
    outcome: null,
    ended_by_check: true,
  };
  const recordedReply = {
    moves: ["f4c7"],
    machine_notation: "f4c7",
    transposition_count: 1,
    child_boundary: exactBoundary(
      "1nb1kbnr/ppqR2pp/4P3/8/8/2K5/PPP1PP2/5BN1 w k - 0 14",
      9,
      "0004000000000000",
    ),
    outcome: null,
    ended_by_check: true,
  };
  const recordedMate = {
    moves: [
      "c3b3", "a2a4", "c2c4", "c4c5", "c5c6", "e2e4", "d7f7",
      "e6e7", "e7f8q",
    ],
    machine_notation: "c3b3/a2a4/c2c4/c4c5/c5c6/e2e4/d7f7/e6e7/e7f8q",
    transposition_count: 1,
    child_boundary: exactBoundary(
      "1nb1kQnr/ppq2Rpp/2P5/8/P3P3/1K6/1P3P2/5BN1 b k - 0 14",
      10,
      "2004000000000000",
    ),
    outcome: "checkmate",
    ended_by_check: true,
  };
  const recordedProof = {
    schema: "spc-single-reply-mate-ladder-proof-v1",
    root_child_boundary: recordedChild,
    forced_reply_unique_legal_move: true,
    attack: recordedAttack,
    forced_reply: recordedReply,
    mate: recordedMate,
  };
  const recordedDefinitions = [
    { id: "recorded-3dfd-loss", key: "f5f4", score: -100 },
    { id: "recorded-safe-alternative", key: "a7a6", score: -50 },
  ];
  const recordedManifest = manifest(recordedDefinitions, { white: false });
  recordedManifest.candidates[0] = {
    ...recordedManifest.candidates[0],
    order_key: "f5f4/f4f3/f3g2/g2h1q/h1h2/h2f4",
    root_series: {
      moves: ["f5f4", "f4f3", "f3g2", "g2h1q", "h1h2", "h2f4"],
      machine_notation: "f5f4/f4f3/f3g2/g2h1q/h1h2/h2f4",
      transposition_count: 1,
      child_boundary: recordedChild,
      outcome: null,
      ended_by_check: true,
    },
  };
  recordedManifest.candidates[1] = {
    ...recordedManifest.candidates[1],
    order_key: "a7a6/a6a5/a5a4/a4a3/a3b2/b2b1q",
    root_series: syntheticSeries(
      ["a7a6", "a6a5", "a5a4", "a4a3", "a3b2", "b2b1q"],
      7,
    ),
  };
  const recordedSafety = async (task) => {
    if (task.candidate_identity !== "recorded-3dfd-loss") {
      return safetyReply(task, {
        status: "exhausted",
        work_used: 0,
        safety_scope: "selected-root-single-reply-mate-ladder",
        single_reply_mate_ladder: makeReceipt(task, "exhausted", null),
      });
    }
    const nativeStats = {
      attack_positions_visited: 628_052,
      attack_moves_generated: 0,
      reply_positions_visited: 0,
      reply_moves_generated: 0,
      mate_positions_visited: 0,
      mate_moves_generated: 0,
      attack_transpositions_merged: 0,
      mate_transpositions_merged: 0,
      checking_series: 1,
      forced_counterchecks: 1,
      mate_probes: 1,
      peak_attack_frontier: 1,
      attack_max_depth_reached: 7,
      mate_max_depth_reached: 9,
      work_used: 628_052,
    };
    const receipt = {
      ...makeReceipt(task, "found", recordedProof),
      call_work_credit: 1_000_000,
      work_used: 628_052,
      native_stats: nativeStats,
    };
    return safetyReply(task, {
      status: "found",
      work_used: 628_052,
      safety_scope: "selected-root-single-reply-mate-ladder",
      single_reply_mate_ladder: receipt,
      override_score: MATE - 4,
      proof_bounds: [1, 1],
      ladder_proof: recordedProof,
    });
  };
  const recordedResult = await api.runRootIteration({
    request: request(2, {
      series: 6,
      boundary: { fen: recordedRootFen },
      caps: {
        max_work: 2_000_000,
        safety_reserve_work: 1_000_000,
        safety_call_work_credit: 1_000_000,
      },
    }),
    manifest: recordedManifest,
    workers: workers(2, recordedDefinitions),
    safetyProbe: recordedSafety,
  });
  assert.equal(recordedResult.selected.candidate_identity, "recorded-safe-alternative");
  assert.equal(recordedResult.tasks.find((event) => (
    event.event === "safety" && event.status === "found"
  )).single_reply_mate_ladder.work_used, 628_052);

  const unknownDefinitions = [{ id: "ladder-unknown", key: "a2a3", score: 10 }];
  const unknownManifest = manifest(unknownDefinitions);
  unknownManifest.candidates[0] = {
    ...unknownManifest.candidates[0],
    root_series: {
      ...unknownManifest.candidates[0].root_series,
      child_boundary: syntheticBoundary(8),
      ended_by_check: true,
    },
  };
  await expectCode(api.runRootIteration({
    request: request(1, { series: 7 }),
    manifest: unknownManifest,
    workers: workers(1, unknownDefinitions),
    safetyProbe: async (task) => safetyReply(task, {
      status: "unknown",
      work_used: 0,
      safety_scope: "selected-root-single-reply-mate-ladder",
      single_reply_mate_ladder: makeReceipt(task, "unknown", null, "deadline"),
    }),
  }), "root-safety-unknown");
}


const streaming = await testStreamingWhiteAndStaleEpoch();
await testCertifiedInitialFullWave();
await testWhiteCanonicalTies();
await testProofAwareRootSelection();
await testUnprovedMateClaimsAreQuarantinedForBothMovers();
await testMateClaimsRequireProofAcrossExactAndRecertificationPaths();
await testAllUnprovedMateClaimsFailClosedDistinctly();
await testBlackMirror();
await testTerminalProductionOrder();
await testSafetyRevisionAndBoundInvalidation();
await testCheckedPvProofRepairsOnlyTheSelectedCandidate();
await testRepairedWinnerRecertifiesWithTheSameProofSet();
await testFailedRepairedWinnerRecertificationReclassifiesTheLastRepair();
await testSecondDistinctCheckedPvProofVetoesAtSameRootRepairLimit();
await testImpossibleProofHitPopulationFailsClosed();
await testDuplicateCheckedPvProofFallsBackToCandidateVeto();
await testSixteenBitProofMaskProtocolLimitRemainsFailClosed();
await testHorizonResearchFallbacksAndMalformedReceipt();
await testNoSearchCreditFallsBackWithoutSpendingHeldSafetyWork();
await testBlackCheckedPvRepairReversesTheCorrectionDirection();
await testCheckedPvPolicyVeto();
await testResponseOrderPermutations();
await testAspirationWideningAndFallback();
await testProtocolFaults();
await testCapsCrashAndMemory();
await testCancellationAndDeadline();
await testSelectedRootSingleReplyLadderGate();
await testUnsupportedEnvelope();

process.stdout.write(`${JSON.stringify({
  schema: "spc-root-iteration-coordinator-verifier-v1",
  scenarios: 31,
  response_order_permutations: 8,
  response_order_worker_count: 8,
  streaming_first_wave: true,
  certified_initial_full_wave_4_of_8: true,
  stale_epoch_revalidated: true,
  white_black_mirrored: true,
  canonical_ties: true,
  proof_aware_white_black_veto: true,
  proof_aware_forced_loss_fallback: true,
  proof_aware_scout_research: true,
  unproved_mate_claims_quarantined_white_black: true,
  matching_mate_claim_proofs_accepted_white_black: true,
  owner_recertification_mate_claim_guarded: true,
  aspiration_exact_mate_claim_guarded: true,
  terminal_mates_preserved: true,
  all_unproved_mate_claims_fail_closed_distinctly: true,
  terminal_production_order: true,
  safety_revision_bound_invalidation: true,
  checked_pv_native_candidate_repair: true,
  checked_pv_repaired_winner_same_owner_recertified: true,
  checked_pv_failed_warm_recertification_accounting_balanced: true,
  checked_pv_second_distinct_proof_vetoes_after_one_repair: true,
  checked_pv_impossible_hit_population_fail_closed: true,
  checked_pv_duplicate_proof_veto_fallback: true,
  checked_pv_16_bit_protocol_mask_limit_fail_closed: true,
  checked_pv_zero_hit_work_limit_and_unsupported_veto_fallback: true,
  checked_pv_malformed_stale_identity_and_lost_fail_closed: true,
  checked_pv_native_deadline_maps_to_root_deadline: true,
  checked_pv_no_search_credit_veto_fallback: true,
  checked_pv_black_repair_direction: true,
  checked_pv_policy_veto_without_score_forgery: true,
  all_policy_vetoes_fail_closed: true,
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
  native_search_deadline_maps_to_root_deadline: true,
  deadline_receipt_grace_without_extra_dispatch: true,
  monotonic_deadline_contract: true,
  full_artifact_identity_bound: true,
  prefix_hard_limits_mirrored: true,
  prefix_chess960_rejected: true,
  constrained_prefix_never_enters_emergency_publication: true,
  selected_root_single_reply_ladder_veto_reselect_and_unknown_fail_closed: true,
  recorded_3dfd_628052_ladder_vetoes_selected_root: true,
  reference_winner: streaming.selected.candidate_identity,
  reference_score: streaming.selected.score,
})}\n`);
