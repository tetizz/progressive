import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const prefixApi = require(path.join(
  root,
  "src/scottish_progressive/web/static/browser-prefix-contract.js",
));
globalThis.ScottishProgressiveBrowserPrefix = prefixApi;
const coordinator = require(path.join(root, "root-iteration-coordinator.js"));
globalThis.ScottishProgressiveRootCoordinator = coordinator;
const rootClientApi = require(path.join(
  root,
  "src/scottish_progressive/web/static/browser-root-iteration-client.js",
));
globalThis.ScottishProgressiveBrowserRootIteration = rootClientApi;
const browserClientApi = require(path.join(
  root,
  "src/scottish_progressive/web/static/browser-engine-client.js",
));

const WHITE_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const ALT_WHITE_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1";
const BLACK_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1";
const PROMOTION_FEN = "7k/4P3/8/8/8/8/8/K7 w - - 0 1";
const SOURCE = "a".repeat(16);
const WASM = "b".repeat(64);
const MODULE = "c".repeat(64);
const KERNEL = "d".repeat(64);
const MEMORY = Object.freeze({
  initial_bytes: 16 * 1024 * 1024,
  maximum_bytes: 32 * 1024 * 1024,
  estimated_peak_bytes: 24 * 1024 * 1024,
  growth_enabled: true,
});
const CONFIG = Object.freeze({
  max_depth: 5,
  width: 32,
  max_work: 10_000_000,
  mate_score: 1_000_000,
  series_cache_capacity: 1_024,
  external_cache_weight: 128,
  worker_threads: 1,
  root_tactical_protection: false,
  root_contract_tt_capacity: 4_096,
  root_contract_eval_capacity: 4_096,
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
const PLAY_LIMITS = Object.freeze({
  maximum_seconds: 60,
  default_seconds: 60,
  default_generation_positions: CONFIG.max_work,
  safety_reserve_positions: 1_000_000,
});
const PREFIX_CONTRACT = Object.freeze({
  schema: prefixApi.CONTRACT_SCHEMA,
  result_schema: prefixApi.RESULT_SCHEMA,
  abi_version: 1,
  chess960: false,
  promoted_hex_required_for_product: true,
  limits: prefixApi.HARD_LIMITS,
});
const GEOMETRY = Object.freeze({
  desktop_workers: 8,
  desktop_initial_full_wave: 4,
  aggregate_maximum_bytes: 8 * MEMORY.maximum_bytes,
  supported_lower_geometries: Object.freeze([
    Object.freeze({ workers: 4, initial_full_wave: 2, aggregate_maximum_bytes: 4 * MEMORY.maximum_bytes }),
    Object.freeze({ workers: 2, initial_full_wave: 1, aggregate_maximum_bytes: 2 * MEMORY.maximum_bytes }),
    Object.freeze({ workers: 1, initial_full_wave: 1, aggregate_maximum_bytes: MEMORY.maximum_bytes }),
  ]),
  play_limits: PLAY_LIMITS,
  session_config: CONFIG,
});
const ROOT_IDENTITY = Object.freeze({
  source_fingerprint: SOURCE,
  kernel_sha256: KERNEL,
  module_js_sha256: MODULE,
  certificate_id: "root-cert-v1",
  runtime_variant: "single",
  thread_count: 1,
  engine_version: "spc-mock-v1",
  ruleset_version: "progressive-v1",
  profile_id: "progressive-baseline",
});
const IDENTITY = Object.freeze({
  ready: true,
  certificate_schema: null,
  certificate_status: null,
  contract_version: 1,
  abi_version: 1,
  source_fingerprint: SOURCE,
  wasm_sha256: WASM,
  module_js_sha256: MODULE,
  analysis_ready: false,
  prefix_ready: true,
  root_session_ready: true,
  mate_ready: true,
  root_iteration_ready: true,
  safety_certified: false,
  certificate_id: null,
  prefix_certificate_id: "prefix-cert-v1",
  root_session_certificate_id: ROOT_IDENTITY.certificate_id,
  mate_certificate_id: "mate-cert-v1",
  kernel_sha256: KERNEL,
  runtime_variant: "single",
  thread_count: 1,
  engine_profile_id: null,
  engine_profile_name: null,
  profile_id: ROOT_IDENTITY.profile_id,
  engine_version: ROOT_IDENTITY.engine_version,
  ruleset_version: ROOT_IDENTITY.ruleset_version,
  analysis_limits: null,
  prefix_contract: PREFIX_CONTRACT,
  root_session_contract: Object.freeze({ schema: "spc-root-session-contract-v1", abi_version: 2 }),
  root_geometry: GEOMETRY,
  memory_limits: MEMORY,
});

const CREATE_KEYS = [
  "boundary", "certificate_id", "config", "engine_version", "generation",
  "iteration_id", "kernel_sha256", "module_js_sha256", "profile_id", "request_id",
  "ruleset_version", "runtime_variant", "schema", "source_fingerprint", "thread_count",
];
const ROUTING_KEYS = [
  "call_work_credit", "deadline_epoch_ms", "deadline_monotonic_ms", "external_work",
  "generation", "iteration_id", "native_work_before", "remaining_time_ms", "request_id",
  "session_id", ...Object.keys(ROOT_IDENTITY),
];
const ENUMERATE_KEYS = [...ROUTING_KEYS, "preferred_series", "schema"];
const IMPORT_KEYS = [...ROUTING_KEYS, "manifest", "schema"];
const SEARCH_KEYS = [
  ...ROUTING_KEYS, "alpha", "beta", "candidate_identity", "child_depth",
  "enumeration_identity", "incumbent_epoch", "mate_score", "mover", "order_index",
  "order_key", "purpose", "safety_revision", "schema", "task_id", "tt_persistence",
];
const NO_REPLY = Symbol("no-reply");
const DESKTOP_NAVIGATOR = Object.freeze({ hardwareConcurrency: 8, deviceMemory: 8 });

function sameJson(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function strictKeys(value, expected) {
  assert.deepEqual(Object.keys(value).sort(), [...new Set(expected)].sort());
}

function exactState(boundary) {
  const fields = String(boundary.fen).split(" ");
  return {
    fen: boundary.fen,
    board_fen: boundary.fen,
    series: boundary.series,
    series_number: boundary.series,
    side_to_move: fields[1] === "w" ? "white" : "black",
    quiet_series: boundary.quiet_series,
    quiet_draw_pending: boundary.quiet_series >= 10,
    ep_targets: [...boundary.ep_targets],
    progressive_ep: [...boundary.ep_targets],
    promoted_hex: boundary.promoted_hex,
    chess960: false,
  };
}

function flipFen(fen) {
  const fields = fen.split(" ");
  fields[1] = fields[1] === "w" ? "b" : "w";
  return fields.join(" ");
}

function boundaryPayload(fen, series, quietSeries = 0) {
  return {
    fen,
    series,
    quiet_series: quietSeries,
    ep_targets: [],
    progressive_ep: [],
    promoted_hex: "0000000000000000",
    chess960: false,
  };
}

function canonicalTacticalProtection(boundary) {
  if (boundary.series >= 5) return true;
  const white = boundary.series % 2 === 1;
  const pawn = white ? "P" : "p";
  const ranks = boundary.fen.split(" ")[0].split("/");
  for (let row = 0; row < ranks.length; row += 1) {
    const expanded = ranks[row].replace(/[1-8]/g, (digit) => " ".repeat(Number(digit)));
    const distance = white ? row : 7 - row;
    if (distance > 0 && boundary.series - distance >= 2 && expanded.includes(pawn)) return true;
  }
  return false;
}

function candidateMoves(series, index) {
  if (series === 1) {
    return [["e2e4"], ["d2d4"], ["g1f3"], ["c2c4"], ["b1c3"], ["a2a3"], ["h2h3"], ["g2g3"]][index];
  }
  const twoMoveSeries = [
    ["a7a6", "h7h6"], ["b7b6", "g7g6"], ["c7c6", "f7f6"], ["d7d6", "e7e6"],
    ["g8f6", "b8c6"], ["a7a5", "h7h5"], ["b7b5", "g7g5"], ["c7c5", "f7f5"],
  ][index];
  if (series === 2) return twoMoveSeries;
  const longerSeries = [
    ["a2a3", "a3a4", "b2b3", "b3b4", "c2c3"],
    ["b2b3", "b3b4", "c2c3", "c3c4", "d2d3"],
    ["c2c3", "c3c4", "d2d3", "d3d4", "e2e3"],
    ["d2d3", "d3d4", "e2e3", "e3e4", "f2f3"],
    ["e2e3", "e3e4", "f2f3", "f3f4", "g2g3"],
    ["f2f3", "f3f4", "g2g3", "g3g4", "h2h3"],
    ["g2g3", "g3g4", "h2h3", "h3h4", "a2a3"],
    ["h2h3", "h3h4", "a2a3", "a3a4", "b2b3"],
  ];
  return longerSeries[index].slice(0, series);
}

function manifestFor(boundary, generation, preferredSeries, { terminalFirst = false } = {}) {
  const white = boundary.series % 2 === 1;
  const childFen = white ? BLACK_FEN : WHITE_FEN;
  const candidates = Array.from({ length: 8 }, (_, index) => {
    const moves = candidateMoves(boundary.series, index);
    const childFields = childFen.split(" ");
    childFields[4] = String(index);
    childFields[5] = String(index + 1);
    const candidateChildFen = childFields.join(" ");
    const terminal = terminalFirst && index === 0;
    return {
      candidate_identity: `c${index}`,
      order_index: index,
      order_key: moves.join("/"),
      terminal_score: terminal
        ? white ? CONFIG.mate_score - 1 : -CONFIG.mate_score + 1
        : null,
      terminal_proof_bounds: terminal
        ? white ? [1, 1] : [-1, -1]
        : [-1, 1],
      root_series: {
        moves,
        machine_notation: moves.join("/"),
        transposition_count: 1,
        child_boundary: exactState(boundaryPayload(candidateChildFen, boundary.series + 1)),
        outcome: terminal ? "checkmate" : null,
        ended_by_check: terminal,
      },
    };
  });
  return {
    enumeration_identity: `manifest-${boundary.series}-${generation}-${preferredSeries.join("-") || "none"}`,
    root_white_to_move: white,
    requested_width: 32,
    retained_count: candidates.length,
    width_complete: true,
    preferred_series: [...preferredSeries],
    candidates,
  };
}

function prefixResult(request, identity, { child = null, outcome = null } = {}) {
  const complete = child !== null;
  const finalFen = complete ? child.fen : request.boundary.fen;
  const remaining = request.boundary.series - request.prefix.length;
  const san = request.prefix.map((move) => `san-${move}`);
  return {
    schema: prefixApi.RESULT_SCHEMA,
    abi_version: 1,
    ok: true,
    status: "complete",
    request_id: request.request_id,
    source_fingerprint: identity.source_fingerprint,
    wasm_sha256: identity.wasm_sha256,
    module_js_sha256: identity.module_js_sha256,
    certificate_id: identity.prefix_certificate_id,
    engine_version: identity.engine_version,
    ruleset_version: identity.ruleset_version,
    boundary_state: exactState(request.boundary),
    fen: finalFen,
    board_fen: finalFen,
    prefix: [...request.prefix],
    current_prefix: [...request.prefix],
    san,
    frames: request.prefix.map((move, index) => ({
      index: index + 1,
      uci: move,
      san: san[index],
      board_fen: finalFen,
    })),
    remaining,
    moves_remaining: remaining,
    complete,
    completion_reason: complete ? outcome === "checkmate" ? "checkmate" : "budget" : null,
    check: outcome === "checkmate",
    ended_by_check: outcome === "checkmate",
    in_check: outcome === "checkmate",
    outcome,
    unused_moves: complete ? remaining : 0,
    legal_next: complete ? [] : [{ uci: "e2e4", san: "e4" }],
    legal_moves: complete ? [] : [{ uci: "e2e4", san: "e4" }],
    next_state: child,
    memory_bytes: MEMORY.initial_bytes,
  };
}

function setupWork(worker, payload, used) {
  const before = worker.nativeWork;
  const accountedBefore = payload.external_work + before;
  assert(
    accountedBefore >= worker.lastAccountedWork,
    `external work regressed from ${worker.lastAccountedWork} to ${accountedBefore}`,
  );
  worker.nativeWork += used;
  worker.lastAccountedWork = payload.external_work + worker.nativeWork;
  return {
    call_work_credit: payload.call_work_credit,
    external_work: payload.external_work,
    native_work_before: before,
    native_work_after: worker.nativeWork,
    call_native_work: used,
    total_accounted_work: payload.external_work + worker.nativeWork,
  };
}

class MockWorld {
  constructor({
    foundFirst = false,
    terminalFirst = false,
    safetyUnknown = false,
    crashGeneration = null,
    searchDelayMs = 2,
    policyDrift = null,
  } = {}) {
    this.foundFirst = foundFirst;
    this.terminalFirst = terminalFirst;
    this.safetyUnknown = safetyUnknown;
    this.crashGeneration = crashGeneration;
    this.searchDelayMs = searchDelayMs;
    this.policyDrift = policyDrift;
    this.workers = [];
    this.live = 0;
    this.peakLive = 0;
    this.probes = 0;
    this.searchDispatches = [];
    this.prefixWorkerNames = [];
    this.safetyReceipts = [];
    this.crashed = false;
    this.createBoundaries = [];
    this.canonicalProtections = [];
    this.deadlineEpochs = [];
  }

  factory = (_url, options) => new MockWorker(this, options);

  async handle(worker, type, payload) {
    if (type === "probe") {
      this.probes += 1;
      return { ...IDENTITY };
    }
    if (type === "root-session-create") {
      strictKeys(payload, CREATE_KEYS);
      assert.equal(payload.schema, "spc-root-session-create-v1");
      assert.deepEqual(Object.fromEntries(Object.keys(ROOT_IDENTITY).map(
        (key) => [key, payload[key]],
      )), ROOT_IDENTITY);
      assert.deepEqual(payload.config, CONFIG);
      worker.sessionCounter += 1;
      worker.sessionId = worker.sessionCounter;
      worker.nativeWork = 0;
      worker.lastAccountedWork = 0;
      worker.boundary = { ...payload.boundary, ep_targets: [...payload.boundary.ep_targets] };
      const canonicalProtection = canonicalTacticalProtection(worker.boundary);
      worker.manifest = null;
      worker.createCount += 1;
      this.createBoundaries.push({ worker: worker.name, boundary: worker.boundary });
      this.canonicalProtections.push(canonicalProtection);
      return {
        schema: "spc-root-session-create-result-v1",
        abi_version: 2,
        status: "ready",
        session_id: worker.sessionId,
        request_id: payload.request_id,
        iteration_id: payload.iteration_id,
        generation: payload.generation,
        ...ROOT_IDENTITY,
        boundary: exactState(payload.boundary),
        config: CONFIG,
        configured_max_depth: CONFIG.max_depth,
        native_work_after: 0,
        canonical_root_tactical_policy: "canonical-boundary-policy-v1",
        canonical_root_tactical_protection: this.policyDrift === "create"
          && worker.name.endsWith("1")
          ? !canonicalProtection
          : canonicalProtection,
        capabilities: {
          selected_owner_certification: true,
          canonical_root_tactical_policy: true,
          reply_mate_safety: false,
        },
        product_publishable: false,
        safety_certified: false,
        memory_bytes: MEMORY.initial_bytes,
        memory_peak_bytes: MEMORY.initial_bytes,
      };
    }
    if (type === "root-session-destroy") {
      assert.equal(payload.session_id, worker.sessionId);
      const sessionId = worker.sessionId;
      worker.sessionId = null;
      worker.destroyCount += 1;
      return {
        status: "destroyed",
        session_id: sessionId,
        memory_bytes: MEMORY.initial_bytes,
        memory_peak_bytes: MEMORY.initial_bytes,
      };
    }
    if (type !== "prefix") assert.equal(payload.session_id, worker.sessionId);
    if (type === "root-enumerate") {
      strictKeys(payload, ENUMERATE_KEYS);
      assert.equal(payload.schema, "spc-root-session-enumerate-v1");
      this.deadlineEpochs.push(payload.deadline_epoch_ms);
      const manifest = manifestFor(
        worker.boundary,
        payload.generation,
        payload.preferred_series,
        { terminalFirst: this.terminalFirst },
      );
      worker.manifest = manifest;
      const work = setupWork(worker, payload, 2);
      return {
        schema: "spc-root-session-enumeration-result-v1",
        abi_version: 2,
        session_id: worker.sessionId,
        status: "complete",
        imported: false,
        request_id: payload.request_id,
        iteration_id: payload.iteration_id,
        generation: payload.generation,
        deadline_monotonic_ms: payload.deadline_monotonic_ms,
        remaining_time_ms: payload.remaining_time_ms,
        ...ROOT_IDENTITY,
        ...manifest,
        canonical_root_tactical_policy: "canonical-boundary-policy-v1",
        canonical_root_tactical_protection: this.policyDrift === "enumerate"
          ? !canonicalTacticalProtection(worker.boundary)
          : canonicalTacticalProtection(worker.boundary),
        work,
        product_publishable: false,
        safety_certified: false,
        memory_bytes: MEMORY.initial_bytes,
        memory_peak_bytes: MEMORY.initial_bytes,
      };
    }
    if (type === "root-import") {
      strictKeys(payload, IMPORT_KEYS);
      assert.equal(payload.schema, "spc-root-session-import-v1");
      this.deadlineEpochs.push(payload.deadline_epoch_ms);
      worker.manifest = JSON.parse(JSON.stringify(payload.manifest));
      const work = setupWork(worker, payload, 1);
      return {
        schema: "spc-root-session-import-result-v1",
        abi_version: 2,
        session_id: worker.sessionId,
        status: "complete",
        imported: true,
        request_id: payload.request_id,
        iteration_id: payload.iteration_id,
        generation: payload.generation,
        deadline_monotonic_ms: payload.deadline_monotonic_ms,
        remaining_time_ms: payload.remaining_time_ms,
        ...ROOT_IDENTITY,
        ...worker.manifest,
        canonical_root_tactical_policy: "canonical-boundary-policy-v1",
        canonical_root_tactical_protection: this.policyDrift === "import"
          ? !canonicalTacticalProtection(worker.boundary)
          : canonicalTacticalProtection(worker.boundary),
        work,
        product_publishable: false,
        safety_certified: false,
        memory_bytes: MEMORY.initial_bytes,
        memory_peak_bytes: MEMORY.initial_bytes,
      };
    }
    if (type === "root-search") {
      strictKeys(payload, SEARCH_KEYS);
      assert.equal(payload.schema, "spc-root-candidate-task-v1");
      this.deadlineEpochs.push(payload.deadline_epoch_ms);
      this.searchDispatches.push({ worker: worker.name, task: { ...payload } });
      if (
        this.crashGeneration === payload.child_depth + 1
        && !this.crashed
      ) {
        this.crashed = true;
        queueMicrotask(() => worker.emit("error", { error: new Error("synthetic crash") }));
        return NO_REPLY;
      }
      const delay = payload.purpose === "full" && payload.candidate_identity !== "c0"
        ? this.searchDelayMs * 4
        : this.searchDelayMs;
      if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
      const index = Number(payload.candidate_identity.slice(1));
      const white = worker.boundary.series % 2 === 1;
      const score = white ? 100 - index * 10 : -100 + index * 10;
      const bound = payload.purpose === "scout"
        ? score <= payload.alpha ? "upper" : score >= payload.beta ? "lower" : "exact"
        : "exact";
      const work = setupWork(worker, payload, 1);
      return {
        schema: "spc-root-candidate-result-v1",
        abi_version: 2,
        request_id: payload.request_id,
        iteration_id: payload.iteration_id,
        ...ROOT_IDENTITY,
        generation: payload.generation,
        safety_revision: payload.safety_revision,
        incumbent_epoch: payload.incumbent_epoch,
        task_id: payload.task_id,
        enumeration_identity: payload.enumeration_identity,
        candidate_identity: payload.candidate_identity,
        purpose: payload.purpose,
        tt_persistence: payload.tt_persistence,
        child_depth: payload.child_depth,
        alpha: payload.alpha,
        beta: payload.beta,
        status: "complete",
        bound,
        score,
        proof_bounds: [-1, 1],
        child_pv: [],
        work,
        product_publishable: false,
        safety_certified: false,
        memory_bytes: MEMORY.initial_bytes,
        memory_peak_bytes: MEMORY.initial_bytes,
      };
    }
    if (type === "prefix") {
      this.prefixWorkerNames.push(worker.name);
      const candidate = worker.manifest?.candidates.find((item) => (
        sameJson(item.root_series.moves, payload.prefix)
      ));
      return prefixResult(payload, IDENTITY, {
        child: candidate?.root_series.child_boundary ?? null,
        outcome: candidate?.root_series.outcome ?? null,
      });
    }
    if (type === "root-safety") {
      this.deadlineEpochs.push(payload.deadline_epoch_ms);
      if (this.safetyUnknown) {
        const result = {
          ...payload,
          status: "unknown",
          work_used: 1,
          memory_bytes: MEMORY.initial_bytes,
          memory_peak_bytes: MEMORY.initial_bytes,
        };
        this.safetyReceipts.push(result);
        return result;
      }
      const found = this.foundFirst && payload.candidate_identity === "c0";
      if (!found) {
        const result = {
          ...payload,
          status: "exhausted",
          work_used: 1,
          memory_bytes: MEMORY.initial_bytes,
          memory_peak_bytes: MEMORY.initial_bytes,
        };
        this.safetyReceipts.push(result);
        return result;
      }
      const child = payload.authoritative_child_boundary;
      const mateMoves = child.series % 2 === 0
        ? ["a7a6", "h7h6"]
        : ["a2a3", "h2h3", "g2g3"];
      const mateRequest = prefixApi.normalizePrefixRequest({
        ...child,
        prefix: mateMoves,
      }, `${payload.iteration_id}:${payload.safety_revision}:mate-replay`, PREFIX_CONTRACT);
      const mateChild = exactState(boundaryPayload(
        flipFen(child.fen),
        child.series + 1,
      ));
      const checked = prefixResult(mateRequest, IDENTITY, {
        child: mateChild,
        outcome: "checkmate",
      });
      const childIsWhite = child.side_to_move === "white";
      const result = {
        ...payload,
        status: "found",
        work_used: 1,
        override_score: childIsWhite ? CONFIG.mate_score - 2 : -CONFIG.mate_score + 2,
        proof_bounds: childIsWhite ? [1, 1] : [-1, -1],
        memory_bytes: MEMORY.initial_bytes,
        memory_peak_bytes: MEMORY.initial_bytes,
        reply_mate: {
          moves: mateMoves,
          machine_notation: mateMoves.join("/"),
          outcome: "checkmate",
          ended_by_check: true,
          checked_prefix: checked,
        },
      };
      this.safetyReceipts.push(result);
      return result;
    }
    throw new Error(`unexpected mock Worker request ${type}`);
  }
}

class MockWorker {
  constructor(world, options) {
    this.world = world;
    this.name = String(options?.name || "unnamed");
    this.listeners = new Map();
    this.terminated = false;
    this.sessionCounter = 0;
    this.sessionId = null;
    this.nativeWork = 0;
    this.lastAccountedWork = 0;
    this.createCount = 0;
    this.destroyCount = 0;
    this.manifest = null;
    world.workers.push(this);
    world.live += 1;
    world.peakLive = Math.max(world.peakLive, world.live);
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  emit(type, event) {
    for (const listener of this.listeners.get(type) || []) listener(event);
  }

  postMessage(message) {
    queueMicrotask(async () => {
      if (this.terminated) return;
      try {
        const payload = await this.world.handle(this, message.type, message.payload);
        if (payload === NO_REPLY || this.terminated) return;
        this.emit("message", { data: { id: message.id, ok: true, payload } });
      } catch (error) {
        if (this.terminated) return;
        this.emit("message", {
          data: {
            id: message.id,
            ok: false,
            error: { message: error.message, code: error.code || "mock-worker-error" },
          },
        });
      }
    });
  }

  terminate() {
    if (this.terminated) return;
    this.terminated = true;
    this.world.live -= 1;
  }
}

function payload(boundary, depth = 2) {
  return {
    ...boundary,
    prefix: [],
    depth,
    max_series: 32,
    time_limit: 20,
    max_generation_positions: 10_000_000,
    alternatives: 0,
    best_move_only: true,
    rate_move: false,
    save: false,
  };
}

async function preflight(client) {
  const result = await client.preflight({ sourceFingerprint: SOURCE });
  assert.equal(result.ready, true);
  return result;
}

async function testPersistentPoolTwoTurns() {
  const world = new MockWorld();
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  assert.equal(world.workers.length, 1);
  const firstDeadline = performance.now() + 20_000;
  const firstPayload = payload(boundaryPayload(WHITE_FEN, 1));
  const first = await client.analyzeRoot(firstPayload, { deadlineMs: firstDeadline });
  assert.equal(first.completed_depth, 2);
  assert.equal(first.root_scores_complete, false);
  assert.equal(first.root_bound_coverage_complete, true);
  assert.equal(first.runtime_receipt.safety_reserve_positions, 1_000_000);
  assert.equal(first.stats.safety_reserve_positions, 1_000_000);
  assert.equal(first.runtime_receipt.worker_count, 8);
  assert.equal(world.peakLive, 8, "preflight heap must be dropped before admitting 8 roots");
  assert.equal(client.rootRunner.pool.length, 8);
  const rootWorkers = [...client.rootRunner.pool].map((channel) => channel.worker);
  assert(rootWorkers.every((worker) => worker.createCount === 1 && worker.destroyCount === 0));
  const firstWave = world.searchDispatches.filter((entry) => entry.task.generation === 1).slice(0, 4);
  assert.equal(firstWave.length, 4);
  assert(firstWave.every((entry) => entry.task.purpose === "full"));
  assert.equal(new Set(firstWave.map((entry) => entry.worker)).size, 4);
  assert(world.searchDispatches.some((entry) => entry.task.purpose === "scout"));
  assert.equal(world.safetyReceipts.length, 1, "D1 and D2 must share one complete mate proof");
  assert.equal(world.safetyReceipts[0].call_work_credit, 1_000_000);
  assert.deepEqual(first.runtime_receipt.mate_cache, {
    schema: "spc-root-mate-proof-cache-summary-v1",
    hits: 1,
    misses: 1,
    entries: 1,
    complete_proofs_only: true,
  });

  const workerCountBeforePrefix = world.workers.length;
  const inspected = await client.inspectPrefix({
    ...boundaryPayload(BLACK_FEN, 2),
    prefix: [],
  });
  assert.equal(inspected.complete, false);
  assert.equal(world.workers.length, workerCountBeforePrefix);
  assert.equal(world.prefixWorkerNames.at(-1), "scottish-progressive-root-root-0");
  assert.equal(client.rootRunner.pool.length, 8);

  const secondDeadline = performance.now() + 30_000;
  const secondPayload = payload(boundaryPayload(ALT_WHITE_FEN, 1));
  const second = await client.analyzeRoot(
    secondPayload,
    { deadlineMs: secondDeadline },
  );
  assert.equal(second.completed_depth, 2);
  assert.deepEqual(
    client.rootRunner.pool.map((channel) => channel.worker),
    rootWorkers,
    "ordinary Worker modules must survive between game turns",
  );
  assert(rootWorkers.every((worker) => worker.createCount === 2 && worker.destroyCount === 1));
  assert(world.createBoundaries.some((entry) => entry.boundary.fen === WHITE_FEN));
  assert(world.createBoundaries.some((entry) => entry.boundary.fen === ALT_WHITE_FEN));
  assert(world.deadlineEpochs.some((value) => value >= performance.timeOrigin + firstDeadline));
  assert(world.deadlineEpochs.some((value) => value >= performance.timeOrigin + secondDeadline));
  assert.equal(world.peakLive, 8);
  assert.throws(
    () => browserClientApi.validatePublishedRootAnalysis({
      ...second,
      root_bound_coverage_complete: false,
    }, secondPayload, client.identity),
    (error) => error?.code === "browser-root-result-invalid",
    "incomplete root-bound coverage must never publish",
  );
  client.close();
}

async function testWhiteAndBlackMateMapping() {
  for (const [boundary, expectedOverride] of [
    [boundaryPayload(WHITE_FEN, 1), -CONFIG.mate_score + 2],
    [boundaryPayload(BLACK_FEN, 2), CONFIG.mate_score - 2],
  ]) {
    const world = new MockWorld({ foundFirst: true });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    const result = await client.analyzeRoot(payload(boundary, 1), {
      deadlineMs: performance.now() + 20_000,
    });
    assert.equal(result.completed_depth, 1);
    const found = world.safetyReceipts.find((receipt) => receipt.status === "found");
    assert(found);
    assert.equal(found.override_score, expectedOverride);
    assert.deepEqual(found.proof_bounds, expectedOverride < 0 ? [-1, -1] : [1, 1]);
    assert.notEqual(result.best_full_series.join("/"), candidateMoves(boundary.series, 0).join("/"));
    client.close();
  }
}

async function testCrashReturnsLastSafeAndReprobes() {
  const world = new MockWorld({ crashGeneration: 3 });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot(payload(boundaryPayload(WHITE_FEN, 1), 3), {
    deadlineMs: performance.now() + 20_000,
  });
  assert.equal(result.completed_depth, 2);
  assert.equal(result.requested_depth, 3);
  assert.equal(client.rootRunner.pool.length, 0, "a crashed pool must be discarded");
  const probesBefore = world.probes;
  world.crashGeneration = null;
  const recovered = await client.analyzeRoot(payload(boundaryPayload(WHITE_FEN, 1), 1), {
    deadlineMs: performance.now() + 20_000,
  });
  assert.equal(recovered.completed_depth, 1);
  assert.equal(world.probes, probesBefore + 8, "recovery must reprobe every replacement Worker");
  client.close();
}

async function testMateProofCacheAcrossFiveDepthsAndBoundaries() {
  const world = new MockWorld();
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot(payload(boundaryPayload(WHITE_FEN, 1), 5), {
    deadlineMs: performance.now() + 20_000,
  });
  assert.equal(result.completed_depth, 5);
  assert.equal(world.safetyReceipts.length, 1);
  assert.equal(result.runtime_receipt.mate_cache.hits, 4);
  assert.equal(result.runtime_receipt.mate_cache.misses, 1);
  const differentChild = await client.analyzeRoot(payload(boundaryPayload(BLACK_FEN, 2), 1), {
    deadlineMs: performance.now() + 20_000,
  });
  assert.equal(differentChild.completed_depth, 1);
  assert.equal(world.safetyReceipts.length, 2, "a different exact child boundary must miss");
  assert.equal(differentChild.runtime_receipt.mate_cache.misses, 1);
  client.close();
}

async function testUnknownMateProofNeverCaches() {
  const world = new MockWorld({ safetyUnknown: true });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  for (let attempt = 0; attempt < 2; attempt += 1) {
    await assert.rejects(
      client.analyzeRoot(payload(boundaryPayload(WHITE_FEN, 1), 1), {
        deadlineMs: performance.now() + 20_000,
      }),
      (error) => error?.code === "root-safety-unknown",
    );
  }
  assert.equal(world.safetyReceipts.length, 2);
  assert.equal(client.rootRunner.mateProofCache.size, 0);
  client.close();
}

async function testImmediateMatePublishesWithBoundCoverage() {
  const world = new MockWorld({ terminalFirst: true });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot(payload(boundaryPayload(WHITE_FEN, 1), 1), {
    deadlineMs: performance.now() + 20_000,
  });
  assert.equal(result.publishable, true);
  assert.equal(result.root_scores_complete, false);
  assert.equal(result.root_bound_coverage_complete, true);
  assert.equal(result.checked_prefix.outcome, "checkmate");
  assert.equal(world.safetyReceipts.length, 0);
  client.close();
}

async function testMateCacheIdentityAndBoundaryBinding() {
  const child = manifestFor(boundaryPayload(WHITE_FEN, 1), 1, []).candidates[0]
    .root_series.child_boundary;
  const key = rootClientApi.mateProofCacheKey(IDENTITY, child);
  const changedIdentityKey = rootClientApi.mateProofCacheKey({
    ...IDENTITY,
    mate_certificate_id: "different-mate-certificate",
  }, child);
  assert.notEqual(changedIdentityKey, key);
  assert.throws(
    () => rootClientApi.mateProofCacheKey(IDENTITY, {
      ...child,
      progressive_ep: ["e3"],
    }),
    (error) => error?.code === "browser-root-mate-cache-key-invalid",
  );

  const world = new MockWorld();
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  client.identity = Object.freeze({
    ...client.identity,
    mate_certificate_id: "different-mate-certificate",
  });
  await assert.rejects(
    client.analyzeRoot(payload(boundaryPayload(WHITE_FEN, 1), 1), {
      deadlineMs: performance.now() + 20_000,
    }),
    (error) => error?.code === "browser-root-worker-identity-mismatch",
  );
  client.close();
}

async function testMismatchedWorkerTimeOriginClampsDeadline() {
  const adapterSource = await readFile(path.join(
    root,
    "src/scottish_progressive/web/static/wasm-kernel-adapter.js",
  ), "utf8");
  const adapter = await import(`data:text/javascript;base64,${Buffer.from(adapterSource).toString("base64")}`);
  const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "performance");
  try {
    Object.defineProperty(globalThis, "performance", {
      value: { timeOrigin: 5_000, now: () => 25 },
      configurable: true,
    });
    const clamped = adapter.clampRootRemainingTime({
      remaining_time_ms: 1_000,
      deadline_monotonic_ms: 999_999_999,
      deadline_epoch_ms: 5_075,
    });
    assert.equal(clamped.remaining_time_ms, 50);
  } finally {
    if (originalDescriptor) Object.defineProperty(globalThis, "performance", originalDescriptor);
    else delete globalThis.performance;
  }
}

async function testCanonicalRootPolicyDriftFailsClosed() {
  for (const [policyDrift, expectedCode] of [
    ["create", "browser-root-session-create-invalid"],
    ["enumerate", "browser-root-enumeration-invalid"],
    ["import", "browser-root-import-mismatch"],
  ]) {
    const world = new MockWorld({ policyDrift });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    await assert.rejects(
      client.analyzeRoot(payload(boundaryPayload(WHITE_FEN, 1), 1), {
        deadlineMs: performance.now() + 20_000,
      }),
      (error) => error?.code === expectedCode,
      `${policyDrift} tactical-policy drift must fail closed`,
    );
    client.close();
  }
}

async function testCanonicalRootPolicySelection() {
  for (const boundary of [
    boundaryPayload(WHITE_FEN, 5),
    boundaryPayload(PROMOTION_FEN, 3),
  ]) {
    const world = new MockWorld();
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    const result = await client.analyzeRoot(payload(boundary, 1), {
      deadlineMs: performance.now() + 20_000,
    });
    assert.equal(result.completed_depth, 1);
    assert.equal(world.canonicalProtections.length, 8);
    assert(world.canonicalProtections.every(Boolean));
    client.close();
  }
}

function testGeometry() {
  assert.equal(rootClientApi.selectCertifiedGeometry(IDENTITY, {
    hardwareConcurrency: 8,
    deviceMemory: 8,
  }).workers, 8);
  assert.equal(rootClientApi.selectCertifiedGeometry(IDENTITY, {
    hardwareConcurrency: 8,
  }).workers, 1, "unknown mobile memory must choose the safest certified lower pool");
  assert.equal(rootClientApi.selectCertifiedGeometry(IDENTITY, {
    hardwareConcurrency: 4,
    deviceMemory: 8,
  }).workers, 4, "the largest fitting certified lower pool must win");
}

await testPersistentPoolTwoTurns();
await testWhiteAndBlackMateMapping();
await testCrashReturnsLastSafeAndReprobes();
await testMateProofCacheAcrossFiveDepthsAndBoundaries();
await testUnknownMateProofNeverCaches();
await testImmediateMatePublishesWithBoundCoverage();
await testMateCacheIdentityAndBoundaryBinding();
await testCanonicalRootPolicySelection();
await testCanonicalRootPolicyDriftFailsClosed();
await testMismatchedWorkerTimeOriginClampsDeadline();
testGeometry();

process.stdout.write(JSON.stringify({
  schema: "spc-browser-root-iteration-mock-receipt-v1",
  desktop_initial_full_wave: "4-of-8",
  persistent_worker_pool: true,
  fresh_sessions_per_turn: true,
  pooled_native_prefix: true,
  preflight_heap_released: true,
  white_black_mate_mapping: true,
  pruned_bounds_publish: true,
  immediate_mate_with_alternatives: true,
  incomplete_bound_coverage_fails_closed: true,
  complete_mate_proof_cache: true,
  unknown_mate_proof_not_cached: true,
  mate_cache_identity_boundary_bound: true,
  crash_last_safe_and_reprobe: true,
  absolute_deadline_epoch_transport: true,
  canonical_root_policy_drift_fails_closed: true,
  canonical_root_policy_selects_late_and_promotion_boundaries: true,
  mismatched_worker_time_origin_clamped: true,
  unknown_memory_uses_lower_geometry: true,
}));
