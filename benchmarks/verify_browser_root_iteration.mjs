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
const adapterSource = await readFile(path.join(
  root,
  "src/scottish_progressive/web/static/wasm-kernel-adapter.js",
), "utf8");
const adapterApi = await import(
  `data:text/javascript;base64,${Buffer.from(adapterSource).toString("base64")}`
);
const appSource = await readFile(path.join(
  root,
  "src/scottish_progressive/web/static/app.js",
), "utf8");

const WHITE_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const ALT_WHITE_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1";
const BLACK_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1";
const PROMOTION_FEN = "7k/4P3/8/8/8/8/8/K7 w - - 0 1";
const PROMOTION_MATE_FEN = "bnq1nr2/p1pp1pk1/8/4PP2/1P2P1p1/8/P1P2KP1/BNbBN2r w - - 0 1";
const PROMOTION_MATE_MOVES = Object.freeze([
  "e1f3", "f3d4", "e5e6", "e6e7", "e7f8r", "f8h8", "d4e6",
]);
const PROMOTION_MATE_CHILD = Object.freeze({
  ...boundaryPayload(
    "bnq1n2R/p1pp1pk1/4N3/5P2/1P2P1p1/8/P1P2KP1/BNbB3r b - - 2 1",
    8,
  ),
  promoted_hex: "8000000000000000",
});
const MISSED_S5_MATE_FEN =
  "rn1q1bnr/2pppk1p/b7/pp3pB1/3P4/8/PPPNPPPP/R1Q1KBNR w KQ - 1 7";
const MISSED_S5_MATE_MOVES = Object.freeze([
  "d4d5", "d2e4", "g5f6", "c1g5", "g5h5",
]);
const MISSED_S5_MATE_CHILD = Object.freeze(boundaryPayload(
  "rn1q1bnr/2pppk1p/b4B2/pp1P1p1Q/4N3/8/PPP1PPPP/R3KBNR b KQ - 4 7",
  6,
));
const SOURCE = "a".repeat(16);
const WASM = "b".repeat(64);
const MODULE = "c".repeat(64);
const KERNEL = "d".repeat(64);
const CHECKED_PV_SELECTION_POLICY =
  "repair-once-then-veto-adverse-selected-pv-boundary-mates-v2";
const SAFE_RESELECT_WIDTH = 512;
const SAFE_RESELECT_TOTAL_WORK = 40_000_000;
const SAFE_RESELECT_EARLY_FRONTIER_COUNT = 32;
const SAFE_RESELECT_EARLY_CHILD_WORK = 3_000_000;
const SAFE_RESELECT_WIDENED_CHILD_WORK = 10_000_000;
const SAFE_RESELECT_REPLY_MATE_SCOPE =
  "selected-child-immediate-reply-mate-only";
const SAME_ROOT_REPAIR_POLICY = Object.freeze({
  schema: "spc-same-root-horizon-repair-policy-v1",
  maximum_successful_same_root_repairs: 1,
});
const MEMORY = Object.freeze({
  initial_bytes: 16 * 1024 * 1024,
  maximum_bytes: 32 * 1024 * 1024,
  estimated_peak_bytes: 24 * 1024 * 1024,
  growth_enabled: true,
});
const CONFIG = Object.freeze({
  max_depth: 5,
  width: 32,
  max_work: 100_000_000,
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
  default_generation_positions: 10_000_000,
  safety_reserve_positions: 4_000_000,
});
const ROOT_CURRENT_SERIES_MATE_CREDIT = Math.min(
  PLAY_LIMITS.safety_reserve_positions,
  250_000,
  Math.floor(PLAY_LIMITS.default_generation_positions / 64),
);
const WIDE_CONFIG = Object.freeze({ ...CONFIG, width: SAFE_RESELECT_WIDTH });
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
  desktop_initial_full_wave: 8,
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
  root_session_contract: Object.freeze({
    schema: "spc-root-session-contract-v1",
    abi_version: 2,
    capabilities: Object.freeze({
      aspiration_windows: true,
      checked_horizon_proof_research: true,
    }),
    request_schemas: Object.freeze({
      search: "spc-root-candidate-task-v1",
      horizon_research: "spc-root-horizon-research-task-v1",
    }),
    result_schemas: Object.freeze({
      search: "spc-root-candidate-result-v1",
      horizon_research: "spc-root-horizon-research-result-v1",
    }),
    hard_limits: Object.freeze({
      maximum_width: SAFE_RESELECT_WIDTH,
      minimum_aspiration_initial_delta: 2_048,
      maximum_aspiration_attempts: 4,
      maximum_horizon_proofs: 16,
      maximum_horizon_proof_path: 8,
    }),
    horizon_research: Object.freeze({
      task_schema: "spc-root-horizon-research-task-v1",
      result_schema: "spc-root-horizon-research-result-v1",
      proof_schema: "spc-retained-root-horizon-proof-v1",
      purpose: "horizon-research",
      full_window: true,
      tt_persistence: "commit",
      hit_mask_order: "request-order",
      warm_exact_zero_hit_allowed: true,
    }),
  }),
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

function assertPolicyReceiptSurfaces(result, expectedVetoes) {
  assert.equal(result.selection_policy, CHECKED_PV_SELECTION_POLICY);
  for (const surface of [result, result.stats, result.runtime_receipt]) {
    assert.deepEqual(surface.same_root_repair_policy, SAME_ROOT_REPAIR_POLICY);
    assert.deepEqual(surface.pv_horizon_policy_vetoes, expectedVetoes);
  }
  assert.equal(result.pv_horizon_candidate_vetoes, expectedVetoes.length);
  assert.equal(result.stats.pv_horizon_candidate_vetoes, expectedVetoes.length);
  assert.equal(result.runtime_receipt.pv_horizon_candidate_vetoes, expectedVetoes.length);
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
    return [
      ["e2e4"], ["d2d4"], ["g1f3"], ["c2c4"], ["b1c3"], ["a2a3"],
      ["h2h3"], ["g2g3"], ["f2f4"], ["b2b3"], ["b2b4"], ["h2h4"],
    ][index];
  }
  const twoMoveSeries = [
    ["a7a6", "h7h6"], ["b7b6", "g7g6"], ["c7c6", "f7f6"], ["d7d6", "e7e6"],
    ["g8f6", "b8c6"], ["a7a5", "h7h5"], ["b7b5", "g7g5"], ["c7c5", "f7f5"],
    ["e7e6", "d7d5"], ["f7f6", "e7e5"], ["g7g6", "f8g7"], ["b8c6", "g8f6"],
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
    ["a2a4", "b2b4", "c2c4", "d2d4", "e2e4"],
    ["b2b4", "c2c4", "d2d4", "e2e4", "f2f4"],
    ["c2c4", "d2d4", "e2e4", "f2f4", "g2g4"],
    ["d2d4", "e2e4", "f2f4", "g2g4", "h2h4"],
  ];
  return longerSeries[index].slice(0, series);
}

function omittedTerminalMateMoves(series) {
  if (series === 1) return ["b2b4"];
  if (series === 2) return ["e7e5", "d7d5"];
  throw new Error(`no omitted terminal-mate fixture for Series ${series}`);
}

function manifestFor(boundary, generation, preferredSeries, { terminalFirst = false } = {}) {
  const white = boundary.series % 2 === 1;
  const childFen = white ? BLACK_FEN : WHITE_FEN;
  const candidates = Array.from({ length: 12 }, (_, index) => {
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
  if (preferredSeries.length > 0) {
    const preferredIndex = candidates.findIndex((candidate) => (
      sameJson(candidate.root_series.moves, preferredSeries)
    ));
    if (preferredIndex >= 0) {
      candidates.unshift(candidates.splice(preferredIndex, 1)[0]);
      candidates.forEach((candidate, index) => { candidate.order_index = index; });
    }
  }
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

function safeReselectManifestFor(boundary, generation) {
  const ordinary = manifestFor(boundary, generation, []);
  const usedMoves = new Set(ordinary.candidates.map((candidate) => candidate.order_key));
  const squares = Array.from("abcdefgh").flatMap((file) => (
    Array.from({ length: 8 }, (_, offset) => `${file}${offset + 1}`)
  ));
  const extraMoves = [];
  for (const from of squares) {
    for (const to of squares) {
      const move = `${from}${to}`;
      if (
        from !== to
        && !usedMoves.has(move)
        && extraMoves.length < 64 - ordinary.candidates.length
      ) {
        usedMoves.add(move);
        extraMoves.push(move);
      }
    }
    if (extraMoves.length === 64 - ordinary.candidates.length) break;
  }
  assert.equal(extraMoves.length, 64 - ordinary.candidates.length);
  const candidates = [
    ...ordinary.candidates.map((candidate, index) => ({
      ...structuredClone(candidate),
      candidate_identity: candidate.candidate_identity,
    })),
    ...extraMoves.map((move, offset) => {
      const index = ordinary.candidates.length + offset;
      const childFields = (boundary.series % 2 === 1 ? BLACK_FEN : WHITE_FEN).split(" ");
      childFields[4] = String(100 + index);
      childFields[5] = String(100 + index);
      return {
        candidate_identity: `w${index}`,
        order_index: index,
        order_key: move,
        terminal_score: null,
        terminal_proof_bounds: [-1, 1],
        root_series: {
          moves: [move],
          machine_notation: move,
          transposition_count: 1,
          child_boundary: exactState(boundaryPayload(
            childFields.join(" "),
            boundary.series + 1,
          )),
          outcome: null,
          ended_by_check: false,
        },
      };
    }),
  ];
  return {
    enumeration_identity: `safe-reselect-${boundary.series}-${generation}`,
    root_white_to_move: boundary.series % 2 === 1,
    requested_width: SAFE_RESELECT_WIDTH,
    retained_count: candidates.length,
    width_complete: true,
    preferred_series: [],
    candidates,
  };
}

function prefixResult(
  request,
  identity,
  { child = null, outcome = null, endedByCheck = outcome === "checkmate" } = {},
) {
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
    completion_reason: complete
      ? endedByCheck ? outcome === "checkmate" ? "checkmate" : "check" : "budget"
      : null,
    check: endedByCheck,
    ended_by_check: endedByCheck,
    in_check: endedByCheck,
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
    foundAll = false,
    foundAllChildDepth = null,
    promotionMateDeferral = false,
    terminalMateStatus = "found",
    terminalMateWork = 11,
    terminalFirst = false,
    safetyUnknown = false,
    crashGeneration = null,
    searchDelayMs = 2,
    policyDrift = null,
    horizonMateFirst = false,
    horizonMateTwice = false,
    horizonMateChildDepth = 4,
    internalBoundaryMateFirst = false,
    internalBoundaryMateSeries = null,
    internalBoundarySafetyUnknown = false,
    terminalFinalPv = false,
    horizonSafetyUnknown = false,
    zeroNativeWork = false,
    rootSafetyWork = 1,
    singleCandidate = false,
    favorableHorizonFirst = false,
    unprovedMateFirst = false,
    unprovedMateCandidateIndex = 0,
    unprovedMateChildDepth = null,
    preferredWinnerIndex = null,
    proactiveTerminalMateStatus = "exhausted",
    proactiveTerminalMateWork = 0,
    wideSafetyStatuses = null,
    wideGenerationWork = 2,
    wideIdentityTamper = false,
    wideSafetyDelayMs = 0,
    wideSafetyClockAdvance = null,
    wideTerminalMateIndex = null,
    wideDestroyFails = false,
    wideTerminateFails = false,
    ordinaryTerminateFails = false,
    wideCrashAfterSafety = false,
    wideDestroyDelayMs = 0,
    wideDestroyStarted = null,
  } = {}) {
    this.foundFirst = foundFirst;
    this.foundAll = foundAll;
    this.foundAllChildDepth = Number.isSafeInteger(foundAllChildDepth)
      ? foundAllChildDepth
      : null;
    this.promotionMateDeferral = promotionMateDeferral;
    this.terminalMateStatus = terminalMateStatus;
    this.terminalMateWork = terminalMateWork;
    this.terminalFirst = terminalFirst;
    this.safetyUnknown = safetyUnknown;
    this.crashGeneration = crashGeneration;
    this.searchDelayMs = searchDelayMs;
    this.policyDrift = policyDrift;
    this.horizonMateFirst = horizonMateFirst;
    this.horizonMateTwice = horizonMateTwice;
    this.horizonMateChildDepth = horizonMateChildDepth;
    this.internalBoundaryMateSeries = Number.isSafeInteger(internalBoundaryMateSeries)
      ? internalBoundaryMateSeries
      : internalBoundaryMateFirst ? 4 : null;
    this.internalBoundarySafetyUnknown = internalBoundarySafetyUnknown;
    this.terminalFinalPv = terminalFinalPv;
    this.horizonSafetyUnknown = horizonSafetyUnknown;
    this.zeroNativeWork = zeroNativeWork;
    this.rootSafetyWork = rootSafetyWork;
    this.singleCandidate = singleCandidate;
    this.favorableHorizonFirst = favorableHorizonFirst;
    this.unprovedMateFirst = unprovedMateFirst;
    this.unprovedMateCandidateIndex = Number.isSafeInteger(unprovedMateCandidateIndex)
      ? unprovedMateCandidateIndex
      : 0;
    this.unprovedMateChildDepth = Number.isSafeInteger(unprovedMateChildDepth)
      ? unprovedMateChildDepth
      : null;
    this.preferredWinnerIndex = Number.isSafeInteger(preferredWinnerIndex)
      ? preferredWinnerIndex
      : null;
    this.proactiveTerminalMateStatus = proactiveTerminalMateStatus;
    this.proactiveTerminalMateWork = proactiveTerminalMateWork;
    this.wideSafetyStatuses = Array.isArray(wideSafetyStatuses)
      ? [...wideSafetyStatuses]
      : null;
    this.wideGenerationWork = wideGenerationWork;
    this.wideIdentityTamper = wideIdentityTamper;
    this.wideSafetyDelayMs = wideSafetyDelayMs;
    this.wideSafetyClockAdvance = typeof wideSafetyClockAdvance === "function"
      ? wideSafetyClockAdvance
      : null;
    this.wideTerminalMateIndex = Number.isSafeInteger(wideTerminalMateIndex)
      ? wideTerminalMateIndex
      : null;
    this.wideDestroyFails = wideDestroyFails;
    this.wideTerminateFails = wideTerminateFails;
    this.ordinaryTerminateFails = ordinaryTerminateFails;
    this.wideCrashAfterSafety = wideCrashAfterSafety;
    this.wideDestroyDelayMs = wideDestroyDelayMs;
    this.wideDestroyStarted = typeof wideDestroyStarted === "function"
      ? wideDestroyStarted
      : null;
    this.wideCrashScheduled = false;
    this.workers = [];
    this.live = 0;
    this.peakLive = 0;
    this.probes = 0;
    this.searchDispatches = [];
    this.enumerationRequests = [];
    this.prefixWorkerNames = [];
    this.safetyReceipts = [];
    this.terminalMateReceipts = [];
    this.proactiveTerminalMateReceipts = [];
    this.proactiveTerminalMateRequests = [];
    this.crashed = false;
    this.createBoundaries = [];
    this.canonicalProtections = [];
    this.deadlineEpochs = [];
    this.pvTransitions = new Map();
    this.deepestBoundaryQuietSeries = 0;
    this.safeReselectCreates = [];
    this.safeReselectSafetyReceipts = [];
    this.safeReselectSafetyCalls = 0;
    this.latestSearchChildDepth = null;
  }

  factory = (_url, options) => new MockWorker(this, options);

  async handle(worker, type, payload) {
    if (type === "probe") {
      this.probes += 1;
      return { ...IDENTITY };
    }
    if (type === "root-session-create" || type === "root-safe-reselector-session-create") {
      const safeReselect = type === "root-safe-reselector-session-create";
      const createPayload = safeReselect ? payload.request : payload;
      if (safeReselect) {
        strictKeys(payload, ["purpose", "request", "schema"]);
        assert.equal(payload.schema, "spc-root-safe-reselector-session-create-v1");
        assert.equal(payload.purpose, "safe-root-reselector");
        assert.equal(createPayload.config, undefined);
      }
      const effectivePayload = safeReselect
        ? { ...createPayload, schema: "spc-root-session-create-v1", config: WIDE_CONFIG }
        : createPayload;
      strictKeys(effectivePayload, CREATE_KEYS);
      assert.equal(effectivePayload.schema, "spc-root-session-create-v1");
      assert.deepEqual(Object.fromEntries(Object.keys(ROOT_IDENTITY).map(
        (key) => [key, effectivePayload[key]],
      )), ROOT_IDENTITY);
      assert.deepEqual(effectivePayload.config, safeReselect ? WIDE_CONFIG : CONFIG);
      worker.sessionCounter += 1;
      worker.sessionId = worker.sessionCounter;
      worker.safeReselect = safeReselect;
      worker.nativeWork = 0;
      worker.lastAccountedWork = 0;
      worker.boundary = {
        ...effectivePayload.boundary,
        ep_targets: [...effectivePayload.boundary.ep_targets],
      };
      const canonicalProtection = canonicalTacticalProtection(worker.boundary);
      worker.manifest = null;
      worker.createCount += 1;
      this.createBoundaries.push({ worker: worker.name, boundary: worker.boundary });
      this.canonicalProtections.push(canonicalProtection);
      if (safeReselect) this.safeReselectCreates.push({ worker: worker.name, payload });
      return {
        schema: "spc-root-session-create-result-v1",
        abi_version: 2,
        status: "ready",
        session_id: worker.sessionId,
        request_id: effectivePayload.request_id,
        iteration_id: effectivePayload.iteration_id,
        generation: effectivePayload.generation,
        ...ROOT_IDENTITY,
        boundary: exactState(effectivePayload.boundary),
        config: safeReselect ? WIDE_CONFIG : CONFIG,
        configured_max_depth: effectivePayload.config.max_depth,
        native_work_after: 0,
        canonical_root_tactical_policy: "canonical-boundary-policy-v1",
        canonical_root_tactical_protection: this.policyDrift === "create"
          && worker.name.endsWith("1")
          ? !canonicalProtection
          : canonicalProtection,
        capabilities: {
          aspiration_windows: true,
          selected_owner_certification: true,
          canonical_root_tactical_policy: true,
          checked_horizon_proof_research: true,
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
      if (worker.safeReselect) this.wideDestroyStarted?.();
      if (worker.safeReselect && this.wideDestroyDelayMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, this.wideDestroyDelayMs));
      }
      if (worker.safeReselect && this.wideDestroyFails) {
        const error = new Error("synthetic isolated destroy miss");
        error.code = "synthetic-isolated-destroy-miss";
        throw error;
      }
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
      this.enumerationRequests.push({ ...payload });
      if (this.promotionMateDeferral) {
        assert.deepEqual(
          worker.boundary,
          rootClientApi.canonicalBoundary(boundaryPayload(PROMOTION_MATE_FEN, 7)),
          "the native unsupported lane must stay bound to the exact promotion-mate root",
        );
        const work = setupWork(worker, payload, 0);
        return {
          schema: "spc-root-session-enumeration-result-v1",
          abi_version: 2,
          session_id: worker.sessionId,
          status: "unsupported",
          status_code: 4,
          message: "native root promotion-mate lane is not implemented",
          request_id: payload.request_id,
          iteration_id: payload.iteration_id,
          generation: payload.generation,
          deadline_monotonic_ms: payload.deadline_monotonic_ms,
          remaining_time_ms: payload.remaining_time_ms,
          ...ROOT_IDENTITY,
          configured_max_depth: CONFIG.max_depth,
          imported: false,
          enumeration_identity: "",
          root_white_to_move: true,
          requested_width: CONFIG.width,
          retained_count: 0,
          width_complete: false,
          preferred_series: [...payload.preferred_series],
          candidates: [],
          canonical_root_tactical_policy: "canonical-boundary-policy-v1",
          canonical_root_tactical_protection: true,
          selective: false,
          evaluation_work_limit_reached: false,
          work,
          product_publishable: false,
          safety_certified: false,
          memory_bytes: MEMORY.initial_bytes,
          memory_peak_bytes: MEMORY.initial_bytes,
        };
      }
      const manifest = worker.safeReselect
        ? safeReselectManifestFor(worker.boundary, payload.generation)
        : manifestFor(
          worker.boundary,
          payload.generation,
          payload.preferred_series,
          { terminalFirst: this.terminalFirst },
        );
      if (worker.safeReselect && this.wideTerminalMateIndex !== null) {
        const terminal = manifest.candidates[this.wideTerminalMateIndex];
        assert(terminal, "the synthetic widened terminal mate must be retained");
        const rootWhite = worker.boundary.series % 2 === 1;
        terminal.terminal_score = rootWhite
          ? CONFIG.mate_score - 1
          : -CONFIG.mate_score + 1;
        terminal.terminal_proof_bounds = rootWhite ? [1, 1] : [-1, -1];
        terminal.root_series.outcome = "checkmate";
        terminal.root_series.ended_by_check = true;
      }
      if (this.singleCandidate) {
        manifest.candidates = manifest.candidates.slice(0, 1);
        manifest.retained_count = 1;
      }
      worker.manifest = manifest;
      const work = setupWork(
        worker,
        payload,
        worker.safeReselect
          ? Math.min(this.wideGenerationWork, payload.call_work_credit)
          : this.zeroNativeWork ? 0 : 2,
      );
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
      const work = setupWork(worker, payload, this.zeroNativeWork ? 0 : 1);
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
      const horizonResearch = payload.schema === "spc-root-horizon-research-task-v1";
      strictKeys(payload, horizonResearch ? [...SEARCH_KEYS, "horizon_proofs"] : SEARCH_KEYS);
      assert.equal(
        payload.schema,
        horizonResearch
          ? "spc-root-horizon-research-task-v1"
          : "spc-root-candidate-task-v1",
      );
      this.deadlineEpochs.push(payload.deadline_epoch_ms);
      this.searchDispatches.push({ worker: worker.name, task: { ...payload } });
      if (!horizonResearch) this.latestSearchChildDepth = payload.child_depth;
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
      const horizonScore = this.horizonMateTwice ? 100 : 80;
      let score = horizonResearch
        ? white ? horizonScore : -horizonScore
        : white ? 100 - index * 10 : -100 + index * 10;
      if (
        !horizonResearch
        && this.preferredWinnerIndex !== null
        && index === this.preferredWinnerIndex
      ) {
        score = white ? 1_000 : -1_000;
      }
      if (
        this.unprovedMateFirst
        && payload.candidate_identity === `c${this.unprovedMateCandidateIndex}`
        && !horizonResearch
        && (
          this.unprovedMateChildDepth === null
          || payload.child_depth === this.unprovedMateChildDepth
        )
      ) {
        score = white ? CONFIG.mate_score - 3 : -CONFIG.mate_score + 3;
      }
      const bound = payload.purpose === "scout" || payload.purpose === "aspiration"
        ? score <= payload.alpha ? "upper" : score >= payload.beta ? "lower" : "exact"
        : "exact";
      const work = setupWork(worker, payload, this.zeroNativeWork ? 0 : 1);
      let childPv = [];
      const pvLength = payload.candidate_identity === "c0"
        ? (this.horizonMateFirst || this.internalBoundaryMateSeries !== null)
          && payload.child_depth === this.horizonMateChildDepth
          && (!horizonResearch || this.horizonMateTwice)
          ? 4
          : this.favorableHorizonFirst && payload.child_depth === 3 ? 3 : 0
        : 0;
      if (pvLength > 0) {
        let start = worker.manifest.candidates[0].root_series.child_boundary;
        childPv = Array.from({ length: pvLength }, (_, pvIndex) => {
          const series = start.series;
          const moves = candidateMoves(series, horizonResearch ? 1 : 0);
          const quietSeries = pvIndex === pvLength - 1
            ? horizonResearch && this.horizonMateTwice
              ? 1
              : this.deepestBoundaryQuietSeries
            : 0;
          const child = exactState(boundaryPayload(
            flipFen(start.fen),
            series + 1,
            quietSeries,
          ));
          const endedByCheck = pvIndex === pvLength - 1;
          const outcome = this.terminalFinalPv && endedByCheck ? "checkmate" : null;
          this.pvTransitions.set(
            JSON.stringify([start.fen, start.series, moves]),
            { child, endedByCheck, outcome },
          );
          start = child;
          return {
            moves,
            machine_notation: moves.join("/"),
            transposition_count: 1,
            child_boundary: child,
            outcome,
            ended_by_check: endedByCheck,
          };
        });
      }
      return {
        schema: horizonResearch
          ? "spc-root-horizon-research-result-v1"
          : "spc-root-candidate-result-v1",
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
        child_pv: childPv,
        work,
        product_publishable: false,
        safety_certified: false,
        memory_bytes: MEMORY.initial_bytes,
        memory_peak_bytes: MEMORY.initial_bytes,
        ...(horizonResearch ? {
          horizon_proof_set_identity: "spc-horizon-proof-set-v1|mock",
          horizon_proofs_validated: payload.horizon_proofs.length,
          horizon_proof_hits: 1,
          horizon_proof_hit_mask: 2 ** (payload.horizon_proofs.length - 1),
        } : {}),
      };
    }
    if (type === "prefix") {
      this.prefixWorkerNames.push(worker.name);
      const transition = this.pvTransitions.get(JSON.stringify([
        payload.boundary.fen,
        payload.boundary.series,
        payload.prefix,
      ]));
      if (transition) {
        return prefixResult(payload, IDENTITY, {
          child: transition.child,
          endedByCheck: transition.endedByCheck,
          outcome: transition.outcome,
        });
      }
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
      if (worker.safeReselect) {
        if (this.wideSafetyDelayMs > 0) {
          await new Promise((resolve) => setTimeout(resolve, this.wideSafetyDelayMs));
        }
        const status = this.wideSafetyStatuses?.[this.safeReselectSafetyCalls] ?? "found";
        this.safeReselectSafetyCalls += 1;
        const workUsed = Math.min(this.rootSafetyWork, payload.call_work_credit);
        const common = {
          ...payload,
          ...(this.wideIdentityTamper ? { source_fingerprint: "e".repeat(16) } : {}),
          status,
          work_used: workUsed,
          memory_bytes: MEMORY.initial_bytes,
          memory_peak_bytes: MEMORY.initial_bytes,
        };
        this.wideSafetyClockAdvance?.();
        if (status !== "found") {
          if (this.wideCrashAfterSafety && !this.wideCrashScheduled) {
            this.wideCrashScheduled = true;
            setTimeout(() => worker.emit("error", {
              error: new Error("synthetic isolated post-evidence crash"),
            }), 0);
          }
          this.safeReselectSafetyReceipts.push(common);
          return common;
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
        this.pvTransitions.set(
          JSON.stringify([child.fen, child.series, mateMoves]),
          { child: mateChild, endedByCheck: true, outcome: "checkmate" },
        );
        const checked = prefixResult(mateRequest, IDENTITY, {
          child: mateChild,
          outcome: "checkmate",
        });
        const childIsWhite = child.side_to_move === "white";
        const result = {
          ...common,
          override_score: childIsWhite ? CONFIG.mate_score - 2 : -CONFIG.mate_score + 2,
          proof_bounds: childIsWhite ? [1, 1] : [-1, -1],
          reply_mate: {
            moves: mateMoves,
            machine_notation: mateMoves.join("/"),
            outcome: "checkmate",
            ended_by_check: true,
            checked_prefix: checked,
          },
        };
        this.safeReselectSafetyReceipts.push(result);
        return result;
      }
      const safetyWorkUsed = Math.min(this.rootSafetyWork, payload.call_work_credit);
      if (
        this.safetyUnknown
        || payload.call_work_credit < this.rootSafetyWork
        || (
          this.horizonSafetyUnknown
          && payload.authoritative_child_boundary?.series === 6
        ) || (
          this.internalBoundarySafetyUnknown
          && payload.authoritative_child_boundary?.series === 4
        )
      ) {
        const result = {
          ...payload,
          status: "unknown",
          work_used: safetyWorkUsed,
          memory_bytes: MEMORY.initial_bytes,
          memory_peak_bytes: MEMORY.initial_bytes,
        };
        this.safetyReceipts.push(result);
        return result;
      }
      const found = (
        this.foundAll
        && (
          this.foundAllChildDepth === null
          || this.latestSearchChildDepth === this.foundAllChildDepth
        )
      )
        || (this.foundFirst && payload.candidate_identity === "c0")
        || (
          this.horizonMateFirst
          && payload.candidate_identity === "c0"
          && payload.authoritative_child_boundary?.series === 6
        ) || (
          this.internalBoundaryMateSeries !== null
          && payload.candidate_identity === "c0"
          && payload.authoritative_child_boundary?.series === this.internalBoundaryMateSeries
        ) || (
          this.favorableHorizonFirst
          && payload.candidate_identity === "c0"
          && payload.authoritative_child_boundary?.series === 5
        );
      if (!found) {
        const result = {
          ...payload,
          status: "exhausted",
          work_used: safetyWorkUsed,
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
      this.pvTransitions.set(
        JSON.stringify([child.fen, child.series, mateMoves]),
        { child: mateChild, endedByCheck: true, outcome: "checkmate" },
      );
      const checked = prefixResult(mateRequest, IDENTITY, {
        child: mateChild,
        outcome: "checkmate",
      });
      const childIsWhite = child.side_to_move === "white";
      const result = {
        ...payload,
        status: "found",
        work_used: safetyWorkUsed,
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
    if (type === "root-terminal-mate") {
      this.deadlineEpochs.push(payload.deadline_epoch_ms);
      assert.equal(payload.schema, "spc-root-terminal-mate-task-v1");
      assert.equal(payload.mate_certificate_id, IDENTITY.mate_certificate_id);
      assert.deepEqual(payload.boundary, worker.boundary);
      const proactive = payload.iteration_id.endsWith(":root-terminal-mate-probe");
      if (proactive) this.proactiveTerminalMateRequests.push({ ...payload });
      if (proactive && this.proactiveTerminalMateStatus === "no-reply") {
        return NO_REPLY;
      }
      if (proactive && this.proactiveTerminalMateStatus !== "found") {
        assert(["exhausted", "unknown"].includes(this.proactiveTerminalMateStatus));
        const result = {
          ...payload,
          status: this.proactiveTerminalMateStatus,
          work_used: Math.min(this.proactiveTerminalMateWork, payload.call_work_credit),
          memory_bytes: MEMORY.initial_bytes,
          memory_peak_bytes: MEMORY.initial_bytes,
        };
        this.proactiveTerminalMateReceipts.push(result);
        return result;
      }
      if (proactive) {
        assert.deepEqual(
          worker.boundary,
          rootClientApi.canonicalBoundary(boundaryPayload(MISSED_S5_MATE_FEN, 5)),
        );
      }
      if (this.terminalMateStatus !== "found") {
        const result = {
          ...payload,
          status: this.terminalMateStatus,
          work_used: Math.min(this.terminalMateWork, payload.call_work_credit),
          memory_bytes: MEMORY.initial_bytes,
          memory_peak_bytes: MEMORY.initial_bytes,
        };
        this.terminalMateReceipts.push(result);
        return result;
      }
      const moves = proactive
        ? [...MISSED_S5_MATE_MOVES]
        : this.promotionMateDeferral
        ? [...PROMOTION_MATE_MOVES]
        : omittedTerminalMateMoves(worker.boundary.series);
      const child = proactive
        ? exactState(MISSED_S5_MATE_CHILD)
        : this.promotionMateDeferral
        ? exactState(PROMOTION_MATE_CHILD)
        : exactState(boundaryPayload(
          flipFen(worker.boundary.fen),
          worker.boundary.series + 1,
        ));
      const replayRequest = prefixApi.normalizePrefixRequest({
        ...worker.boundary,
        prefix: moves,
      }, `${payload.iteration_id}:terminal-mate-replay`, PREFIX_CONTRACT);
      const checked = prefixResult(replayRequest, IDENTITY, {
        child,
        outcome: "checkmate",
      });
      const rootWhite = worker.boundary.series % 2 === 1;
      const result = {
        ...payload,
        status: "found",
        work_used: Math.min(
          proactive ? this.proactiveTerminalMateWork : 11,
          payload.call_work_credit,
        ),
        score: rootWhite ? CONFIG.mate_score - 1 : -CONFIG.mate_score + 1,
        proof_bounds: rootWhite ? [1, 1] : [-1, -1],
        memory_bytes: MEMORY.initial_bytes,
        memory_peak_bytes: MEMORY.initial_bytes,
        root_series: {
          moves,
          machine_notation: moves.join("/"),
          transposition_count: 1,
          child_boundary: child,
          outcome: "checkmate",
          ended_by_check: true,
        },
        checked_prefix: checked,
      };
      if (proactive) this.proactiveTerminalMateReceipts.push(result);
      else this.terminalMateReceipts.push(result);
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
    this.safeReselect = false;
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
    if (
      !this.safeReselect
      && this.world.ordinaryTerminateFails
      && this.name.startsWith("scottish-progressive-root-root-")
    ) {
      const error = new Error("synthetic ordinary Worker termination failure");
      error.code = "synthetic-ordinary-termination-failure";
      throw error;
    }
    if (this.safeReselect && this.world.wideTerminateFails) {
      const error = new Error("synthetic isolated Worker termination failure");
      error.code = "synthetic-isolated-termination-failure";
      throw error;
    }
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

function rootIterationApiWithSafetyMutation(mutateSafety) {
  const modulePath = require.resolve(path.join(
    root,
    "src/scottish_progressive/web/static/browser-root-iteration-client.js",
  ));
  const previousCoordinator = globalThis.ScottishProgressiveRootCoordinator;
  const previousRootApi = globalThis.ScottishProgressiveBrowserRootIteration;
  const coordinatorWithMutatedSafety = Object.freeze({
    ...coordinator,
    runRootIteration: (options) => coordinator.runRootIteration({
      ...options,
      safetyProbe: async (...args) => mutateSafety(
        await options.safetyProbe(...args),
        ...args,
      ),
    }),
  });
  try {
    globalThis.ScottishProgressiveRootCoordinator = coordinatorWithMutatedSafety;
    delete require.cache[modulePath];
    return require(modulePath);
  } finally {
    delete require.cache[modulePath];
    globalThis.ScottishProgressiveRootCoordinator = previousCoordinator;
    globalThis.ScottishProgressiveBrowserRootIteration = previousRootApi;
  }
}

function testAspirationAggregateAndAffinityContract() {
  const receipt = {
    enabled: true,
    center_score: 50,
    initial_delta: 2_048,
    maximum_attempts: 4,
    candidate_count: 3,
    attempts: 7,
    fail_highs: 2,
    fail_lows: 2,
    exact_hits: 3,
    full_window_fallbacks: 0,
  };
  assert(rootClientApi.normalizeAspirationReceipt(
    receipt,
    { center_score: 50, initial_delta: 2_048 },
    3,
  ));
  assert.equal(rootClientApi.normalizeAspirationReceipt(
    { ...receipt, attempts: 13, fail_highs: 6 },
    { center_score: 50, initial_delta: 2_048 },
    3,
  ), null, "aggregate attempts above four per candidate must fail closed");
  assert.equal(rootClientApi.normalizeAspirationReceipt(
    { ...receipt, attempts: 6, exact_hits: 2 },
    { center_score: 50, initial_delta: 2_048 },
    3,
  ), null, "a completed aggregate must resolve every aspiration candidate");

  const taskLog = [
    {
      event: "dispatch", task_id: "t0", candidate_identity: "c0",
      worker_id: "root-2", purpose: "full",
    },
    {
      event: "complete", task_id: "t0", candidate_identity: "c0",
      worker_id: "root-2", purpose: "full", bound: "exact",
    },
    {
      event: "dispatch", task_id: "t0-cert", candidate_identity: "c0",
      worker_id: "root-1", purpose: "selected-certification",
    },
    {
      event: "complete", task_id: "t0-cert", candidate_identity: "c0",
      worker_id: "root-1", purpose: "selected-certification", bound: "exact",
    },
    {
      event: "dispatch", task_id: "t1", candidate_identity: "c1",
      worker_id: "root-0", purpose: "threat-research",
    },
    {
      event: "complete", task_id: "t1", candidate_identity: "c1",
      worker_id: "root-0", purpose: "threat-research", bound: "exact",
    },
    {
      event: "dispatch", task_id: "t1-cert", candidate_identity: "c1",
      worker_id: "root-2", purpose: "selected-certification",
    },
    {
      event: "complete", task_id: "t1-cert", candidate_identity: "c1",
      worker_id: "root-2", purpose: "selected-certification", bound: "exact",
    },
    {
      event: "dispatch", task_id: "t2", candidate_identity: "c2",
      worker_id: "root-1", purpose: "scout",
    },
    {
      event: "complete", task_id: "t2", candidate_identity: "c2",
      worker_id: "root-1", purpose: "scout", bound: "exact",
    },
  ];
  const owners = rootClientApi.exactCandidateOwnerMap(taskLog);
  assert.deepEqual([...owners], [["c0", "root-2"], ["c1", "root-0"]]);
  const adapters = ["root-0", "root-1", "root-2"].map((id) => ({ id }));
  const affinityManifest = {
    candidates: ["c0", "c1", "c2"].map((candidate_identity) => ({
      candidate_identity,
      terminal_score: null,
    })),
  };
  const complete = rootClientApi.scheduleAspirationAffinity({
    adapters,
    manifest: affinityManifest,
    initialFullWave: 2,
    aspiration: { center_score: 50, initial_delta: 2_048 },
    previousOwners: owners,
  });
  assert.deepEqual(complete.adapters.map((adapter) => adapter.id), [
    "root-2", "root-0", "root-1",
  ]);
  assert.deepEqual(complete.candidateIds, ["c0", "c1"]);
  assert.deepEqual(complete.ownerIds, ["root-2", "root-0"]);
  assert.equal(complete.warmOwnerReused, true);

  const incomplete = rootClientApi.scheduleAspirationAffinity({
    adapters,
    manifest: affinityManifest,
    initialFullWave: 3,
    aspiration: { center_score: 50, initial_delta: 2_048 },
    previousOwners: owners,
  });
  assert.deepEqual(incomplete.adapters, adapters);
  assert.deepEqual(incomplete.ownerIds, []);
  assert.equal(incomplete.warmOwnerReused, false);

  const duplicateOwners = new Map([
    ["c0", "root-0"], ["c1", "root-0"],
  ]);
  const nonUnique = rootClientApi.scheduleAspirationAffinity({
    adapters,
    manifest: affinityManifest,
    initialFullWave: 2,
    aspiration: { center_score: 50, initial_delta: 2_048 },
    previousOwners: duplicateOwners,
  });
  assert.deepEqual(nonUnique.ownerIds, []);
  assert.equal(nonUnique.warmOwnerReused, false);

  assert.throws(
    () => rootClientApi.scheduleAspirationAffinity({
      adapters,
      manifest: affinityManifest,
      initialFullWave: 2,
      aspiration: { center_score: 50, initial_delta: 2_048 },
      previousOwners: new Map([["c0", "root-2"], ["c1", "root-missing"]]),
    }),
    (error) => error?.code === "browser-root-aspiration-owner-unavailable",
  );
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
  assert.equal(first.runtime_receipt.safety_reserve_positions, 4_000_000);
  assert.equal(first.stats.safety_reserve_positions, 4_000_000);
  assertPolicyReceiptSurfaces(first, []);
  assert.equal(first.runtime_receipt.worker_count, 8);
  assert.equal(world.peakLive, 8, "preflight heap must be dropped before admitting 8 roots");
  assert.equal(client.rootRunner.pool.length, 8);
  const rootWorkers = [...client.rootRunner.pool].map((channel) => channel.worker);
  assert(rootWorkers.every((worker) => worker.createCount === 1 && worker.destroyCount === 0));
  const firstWave = world.searchDispatches.filter((entry) => entry.task.generation === 1).slice(0, 8);
  assert.equal(firstWave.length, 8);
  assert(firstWave.every((entry) => entry.task.purpose === "full"));
  assert.equal(new Set(firstWave.map((entry) => entry.worker)).size, 8);
  const d1OwnerByCandidate = new Map(firstWave.map((entry) => (
    [entry.task.candidate_identity, entry.worker]
  )));
  const d2FirstAspirationByCandidate = new Map();
  for (const entry of world.searchDispatches.filter((item) => (
    item.task.child_depth === 1 && item.task.purpose === "aspiration"
  ))) {
    if (!d2FirstAspirationByCandidate.has(entry.task.candidate_identity)) {
      d2FirstAspirationByCandidate.set(entry.task.candidate_identity, entry);
    }
  }
  assert.equal(d2FirstAspirationByCandidate.size, 8);
  for (const [candidateId, priorOwner] of d1OwnerByCandidate) {
    assert.equal(
      d2FirstAspirationByCandidate.get(candidateId)?.worker,
      priorOwner,
      `${candidateId} must reuse its exact D1 owner for D2 aspiration`,
    );
  }
  assert.deepEqual(first.runtime_receipt.aspiration, {
    enabled: true,
    center_score: 100,
    initial_delta: 2_048,
    maximum_attempts: 4,
    candidate_count: 8,
    attempts: 8,
    fail_highs: 0,
    fail_lows: 0,
    exact_hits: 8,
    full_window_fallbacks: 0,
    owner_worker_id: "root-0",
    owner_worker_ids: [
      "root-0", "root-1", "root-2", "root-3",
      "root-4", "root-5", "root-6", "root-7",
    ],
    owner_worker_count: 8,
    warm_owner_reused: true,
    warm_owner_reused_count: 8,
  });
  assert.equal(first.stats.aspiration_candidate_count, 8);
  assert.deepEqual(
    first.stats.aspiration_owner_worker_ids,
    [
      "root-0", "root-1", "root-2", "root-3",
      "root-4", "root-5", "root-6", "root-7",
    ],
  );
  assert.equal(first.stats.aspiration_owner_worker_count, 8);
  assert.equal(first.stats.aspiration_warm_owner_reused_count, 8);
  assert(world.searchDispatches.some((entry) => entry.task.purpose === "scout"));
  assert.equal(world.safetyReceipts.length, 1, "D1 and D2 must share one complete mate proof");
  assert.equal(world.safetyReceipts[0].call_work_credit, 4_000_000);
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
  for (const [label, mutation] of [
    ["unknown selection policy", {
      selection_policy: "untrusted-policy-v0",
    }],
    ["selection filter/count disagreement", {
      selection_policy_filtered: true,
    }],
    ["non-integer rejection count", {
      pv_horizon_line_rejections: -0.5,
      stats: {
        ...second.stats,
        pv_horizon_line_rejections: -0.5,
      },
      runtime_receipt: {
        ...second.runtime_receipt,
        pv_horizon_line_rejections: -0.5,
      },
    }],
    ["result coverage-scope disagreement", {
      root_bound_coverage_scope: "selection-eligible-candidates",
    }],
    ["statistics rejection-count disagreement", {
      stats: {
        ...second.stats,
        pv_horizon_line_rejections: second.pv_horizon_line_rejections + 1,
      },
    }],
    ["receipt selection-policy disagreement", {
      runtime_receipt: {
        ...second.runtime_receipt,
        selection_policy: "untrusted-policy-v0",
      },
    }],
    ["receipt rejection-count disagreement", {
      runtime_receipt: {
        ...second.runtime_receipt,
        pv_horizon_line_rejections: second.pv_horizon_line_rejections + 1,
      },
    }],
    ["receipt selection-filter disagreement", {
      runtime_receipt: {
        ...second.runtime_receipt,
        selection_policy_filtered: true,
      },
    }],
    ["receipt coverage-scope disagreement", {
      runtime_receipt: {
        ...second.runtime_receipt,
        root_bound_coverage_scope: "selection-eligible-candidates",
      },
    }],
    ["receipt unfiltered-winner disagreement", {
      runtime_receipt: {
        ...second.runtime_receipt,
        unfiltered_score_winner_selected: false,
      },
    }],
    ["unfiltered-winner disagreement", {
      unfiltered_score_winner_selected: false,
    }],
    ["unproved mate-band score", {
      score: CONFIG.mate_score - 3,
      proof_bounds: [-1, 1],
      proof: null,
    }],
  ]) {
    assert.throws(
      () => browserClientApi.validatePublishedRootAnalysis({
        ...second,
        ...mutation,
      }, secondPayload, client.identity),
      (error) => error?.code === "browser-root-result-invalid",
      `${label} must never publish`,
    );
  }
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

async function testUnprovedMateClaimQuarantinePublishesSafeRoot() {
  for (const boundary of [
    boundaryPayload(WHITE_FEN, 1),
    boundaryPayload(BLACK_FEN, 2),
  ]) {
    const world = new MockWorld({ unprovedMateFirst: true });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    const result = await client.analyzeRoot(payload(boundary, 1), {
      deadlineMs: performance.now() + 20_000,
    });

    assert.deepEqual(result.best_full_series, candidateMoves(boundary.series, 1));
    assert(Math.abs(result.score) < CONFIG.mate_score - 10_000);
    assert.deepEqual(result.proof_bounds, [-1, 1]);
    assert.equal(result.mate_claim_policy_filtered, true);
    assert.equal(result.root_mate_claim_quarantines, 1);
    assert.equal(result.selection_policy_filtered, false);
    assert.equal(result.root_bound_coverage_scope, "selection-eligible-candidates");
    assert.equal(result.unfiltered_score_winner_selected, false);
    assert.equal(result.stats.root_mate_claim_quarantines, 1);
    assert.equal(result.runtime_receipt.root_mate_claim_quarantines, 1);
    assert.equal(result.mate_claim_quarantine_receipts.length, 1);
    assert.equal(
      result.mate_claim_quarantine_receipts[0].candidate_identity,
      "c0",
    );
    browserClientApi.validatePublishedRootAnalysis(
      result,
      payload(boundary, 1),
      client.identity,
    );
    for (const [label, mutation] of [
      ["mate policy drift", {
        mate_claim_selection_policy: "untrusted-mate-policy-v0",
      }],
      ["mate quarantine count drift", {
        root_mate_claim_quarantines: 2,
      }],
      ["mate quarantine receipt drift", {
        mate_claim_quarantine_receipts: [{
          ...result.mate_claim_quarantine_receipts[0],
          currently_quarantined: false,
        }],
      }],
    ]) {
      assert.throws(
        () => browserClientApi.validatePublishedRootAnalysis(
          { ...result, ...mutation },
          payload(boundary, 1),
          client.identity,
        ),
        (error) => error?.code === "browser-root-result-invalid",
        `${label} must fail closed`,
      );
    }
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

async function interruptedAfterLastSafe(errorCode) {
  const world = new MockWorld();
  const base = world.handle.bind(world);
  let failed = false;
  world.handle = async (worker, type, request) => {
    if (
      !failed
      && type === "root-search"
      && request.child_depth + 1 === 3
    ) {
      failed = true;
      const error = new Error(`synthetic ${errorCode}`);
      error.code = errorCode;
      throw error;
    }
    return base(worker, type, request);
  };
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const requestPayload = payload(boundaryPayload(WHITE_FEN, 1), 3);
  const result = await client.analyzeRoot(requestPayload, {
    deadlineMs: performance.now() + 20_000,
  });
  const identity = client.identity;
  client.close();
  return { identity, requestPayload, result };
}

async function testNestedDeadlineAndExactWorkLimitClassification() {
  const deadlineRun = await interruptedAfterLastSafe("root-deadline");
  const deadline = deadlineRun.result;
  assert.equal(deadline.completed_depth, 2);
  assert.equal(deadline.timed_out, true);
  assert.equal(deadline.work_limit_reached, false);
  assert.equal(deadline.runtime_receipt.timed_out, true);
  assert.equal(deadline.runtime_receipt.work_limit_reached, false);
  assert.equal(deadline.runtime_receipt.interruption_code, "root-worker-lost");
  assert.equal(deadline.attempted_work, deadline.runtime_receipt.attempted_work);
  assert(deadline.attempted_work > deadline.work);
  assert.equal(
    deadline.attempted_wall_time_seconds,
    deadline.runtime_receipt.attempted_wall_time_seconds,
  );
  assert(deadline.attempted_wall_time_seconds >= deadline.runtime_receipt.wall_time_seconds);

  const workLimitRun = await interruptedAfterLastSafe("root-work-limit");
  const workLimit = workLimitRun.result;
  assert.equal(workLimit.completed_depth, 2);
  assert.equal(workLimit.timed_out, false);
  assert.equal(workLimit.work_limit_reached, true);
  assert.equal(workLimit.runtime_receipt.timed_out, false);
  assert.equal(workLimit.runtime_receipt.work_limit_reached, true);
  assert.equal(workLimit.runtime_receipt.interruption_code, "root-worker-lost");
  assert.equal(workLimit.attempted_work, workLimit.runtime_receipt.attempted_work);
  assert(workLimit.attempted_work > workLimit.work);
  assert.equal(
    workLimit.attempted_wall_time_seconds,
    workLimit.runtime_receipt.attempted_wall_time_seconds,
  );
  assert(workLimit.attempted_wall_time_seconds >= workLimit.runtime_receipt.wall_time_seconds);

  const assertInterruptedMutationRejected = (run, label, mutate) => {
    const candidate = structuredClone(run.result);
    mutate(candidate);
    assert.throws(
      () => browserClientApi.validatePublishedRootAnalysis(
        candidate,
        run.requestPayload,
        run.identity,
      ),
      (error) => error?.code === "browser-root-result-invalid",
      label,
    );
  };
  for (const [label, mutate] of [
    ["attempted work must be present top-level", (candidate) => {
      delete candidate.attempted_work;
    }],
    ["attempted work must be an exact integer", (candidate) => {
      candidate.attempted_work += 0.5;
      candidate.runtime_receipt.attempted_work = candidate.attempted_work;
    }],
    ["attempted work cannot precede last-safe work", (candidate) => {
      candidate.attempted_work = candidate.work - 1;
      candidate.runtime_receipt.attempted_work = candidate.work - 1;
    }],
    ["attempted work surfaces cannot drift", (candidate) => {
      candidate.runtime_receipt.attempted_work += 1;
    }],
    ["attempted work cannot exceed the safe-integer envelope", (candidate) => {
      candidate.attempted_work = Number.MAX_SAFE_INTEGER + 1;
      candidate.runtime_receipt.attempted_work = candidate.attempted_work;
    }],
    ["interrupted accounting requires its interruption code", (candidate) => {
      delete candidate.runtime_receipt.interruption_code;
    }],
    ["interruption flags cannot drift across surfaces", (candidate) => {
      candidate.runtime_receipt.timed_out = !candidate.timed_out;
    }],
  ]) assertInterruptedMutationRejected(deadlineRun, label, mutate);
  for (const [label, mutate] of [
    ["attempted wall time must be present top-level", (candidate) => {
      delete candidate.attempted_wall_time_seconds;
    }],
    ["attempted wall time must be finite", (candidate) => {
      candidate.attempted_wall_time_seconds = Number.POSITIVE_INFINITY;
      candidate.runtime_receipt.attempted_wall_time_seconds =
        candidate.attempted_wall_time_seconds;
    }],
    ["attempted wall time cannot precede last-safe wall time", (candidate) => {
      candidate.attempted_wall_time_seconds = -1;
      candidate.runtime_receipt.attempted_wall_time_seconds = -1;
    }],
    ["attempted wall-time surfaces cannot drift", (candidate) => {
      candidate.runtime_receipt.attempted_wall_time_seconds += 0.001;
    }],
  ]) assertInterruptedMutationRejected(workLimitRun, label, mutate);
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

async function testUnknownCheckedPvHorizonCannotFallThroughToRootChild() {
  const world = new MockWorld({
    horizonMateFirst: true,
    horizonSafetyUnknown: true,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot(
    payload(boundaryPayload(WHITE_FEN, 1), 5),
    { deadlineMs: performance.now() + 20_000 },
  );
  assert.equal(result.completed_depth, 4, "UNKNOWN D5 horizon must not publish");
  assert.equal(result.runtime_receipt.interruption_code, "root-safety-unknown");
  assert.equal(
    world.safetyReceipts.filter((receipt) => (
      receipt.authoritative_child_boundary?.series === 6
    )).length,
    1,
  );
  assert.equal(
    world.safetyReceipts.filter((receipt) => (
      receipt.authoritative_child_boundary?.series === 2
      && receipt.iteration_id.endsWith(":d5")
    )).length,
    0,
    "root-child exhaustion must not replace an UNKNOWN checked horizon",
  );
  client.close();
}

async function testCheckedPvHorizonWithoutProbeCreditFailsClosed() {
  const world = new MockWorld({
    horizonMateFirst: true,
    zeroNativeWork: true,
    rootSafetyWork: 998,
    singleCandidate: true,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const request = payload(boundaryPayload(WHITE_FEN, 1), 5);
  request.max_generation_positions = 1_000;
  const result = await client.analyzeRoot(request, {
    deadlineMs: performance.now() + 20_000,
  });
  assert.equal(result.completed_depth, 4, "unprobed D5 horizon must not publish");
  assert.equal(result.runtime_receipt.interruption_code, "root-safety-unknown");
  assert.equal(
    world.safetyReceipts.some((receipt) => (
      receipt.iteration_id.endsWith(":d5")
    )),
    false,
    "a one-credit safety reservation cannot skip into root-child certification",
  );
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
  assertPolicyReceiptSurfaces(result, []);
  client.close();
}

async function testCheckedPvHorizonMateRejectsTheProvisionalWinner() {
  const world = new MockWorld({ horizonMateFirst: true });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const requestPayload = payload(boundaryPayload(WHITE_FEN, 1), 5);
  const result = await client.analyzeRoot(
    requestPayload,
    { deadlineMs: performance.now() + 20_000 },
  );
  assert.equal(
    result.completed_depth,
    5,
    String(result.runtime_receipt?.interruption_code || "D5 did not complete"),
  );
  assert.notDeepEqual(result.best_full_series, candidateMoves(1, 0));
  const horizonProof = world.safetyReceipts.find((receipt) => (
    receipt.candidate_identity === "c0"
    && receipt.authoritative_child_boundary?.series === 6
    && receipt.status === "found"
  ));
  assert(horizonProof, "the selected checked D5 horizon must receive an exact S6 mate probe");
  assert.equal(horizonProof.call_work_credit, 3_500_000);
  assert.equal(horizonProof.override_score, -CONFIG.mate_score + 2);
  assert.equal(result.selection_policy_filtered, false);
  assert.equal(result.pv_horizon_line_rejections, 1);
  assert.equal(result.pv_horizon_native_repairs, 1);
  assert.equal(result.pv_horizon_candidate_vetoes, 0);
  assertPolicyReceiptSurfaces(result, []);
  const tooManySameRootRepairs = structuredClone(result);
  const invalidRepairCount = requestPayload.max_series + 1;
  tooManySameRootRepairs.pv_horizon_line_rejections = invalidRepairCount;
  tooManySameRootRepairs.pv_horizon_native_repairs = invalidRepairCount;
  tooManySameRootRepairs.stats.pv_horizon_line_rejections = invalidRepairCount;
  tooManySameRootRepairs.stats.pv_horizon_native_repairs = invalidRepairCount;
  tooManySameRootRepairs.runtime_receipt.pv_horizon_line_rejections = invalidRepairCount;
  tooManySameRootRepairs.runtime_receipt.pv_horizon_native_repairs = invalidRepairCount;
  assert.throws(
    () => browserClientApi.validatePublishedRootAnalysis(
      tooManySameRootRepairs,
      requestPayload,
      client.identity,
    ),
    (error) => error?.code === "browser-root-result-invalid",
    "one successful same-root repair per retained candidate is the publication cap",
  );
  const horizonResearch = world.searchDispatches.find(({ task }) => (
    task.schema === "spc-root-horizon-research-task-v1"
  ));
  assert(horizonResearch, "the exact checked-horizon witness must return to its warm owner");
  assert.equal(horizonResearch.task.horizon_proofs.length, 1);
  assert.equal(horizonResearch.task.horizon_proofs[0].schema,
    "spc-retained-root-horizon-proof-v1");
  const assertFilteredMutationRejected = (label, mutate) => {
    const candidate = structuredClone(result);
    mutate(candidate);
    assert.throws(
      () => browserClientApi.validatePublishedRootAnalysis(
        candidate,
        requestPayload,
        client.identity,
      ),
      (error) => error?.code === "browser-root-result-invalid",
      `${label} must never publish`,
    );
  };
  for (const [label, mutate] of [
    ["aligned unknown selection policy", (candidate) => {
      candidate.selection_policy = "untrusted-policy-v0";
      candidate.runtime_receipt.selection_policy = "untrusted-policy-v0";
    }],
    ["aligned fractional rejection count", (candidate) => {
      candidate.pv_horizon_line_rejections = 1.5;
      candidate.stats.pv_horizon_line_rejections = 1.5;
      candidate.runtime_receipt.pv_horizon_line_rejections = 1.5;
    }],
    ["aligned excessive rejection count", (candidate) => {
      const maximumRepairs = requestPayload.max_series;
      const maximumVetoes = requestPayload.max_series;
      const excessive = maximumRepairs + maximumVetoes + 1;
      candidate.pv_horizon_line_rejections = excessive;
      candidate.pv_horizon_native_repairs = maximumRepairs;
      candidate.pv_horizon_candidate_vetoes = maximumVetoes + 1;
      candidate.stats.pv_horizon_line_rejections = excessive;
      candidate.stats.pv_horizon_native_repairs = maximumRepairs;
      candidate.stats.pv_horizon_candidate_vetoes = maximumVetoes + 1;
      candidate.runtime_receipt.pv_horizon_line_rejections = excessive;
      candidate.runtime_receipt.pv_horizon_native_repairs = maximumRepairs;
      candidate.runtime_receipt.pv_horizon_candidate_vetoes = maximumVetoes + 1;
      candidate.selection_policy_filtered = true;
      candidate.runtime_receipt.selection_policy_filtered = true;
      candidate.root_bound_coverage_scope = "selection-eligible-candidates";
      candidate.runtime_receipt.root_bound_coverage_scope = "selection-eligible-candidates";
    }],
    ["aligned forged candidate veto", (candidate) => {
      candidate.selection_policy_filtered = true;
      candidate.runtime_receipt.selection_policy_filtered = true;
    }],
    ["aligned filtered coverage scope", (candidate) => {
      candidate.root_bound_coverage_scope = "selection-eligible-candidates";
      candidate.runtime_receipt.root_bound_coverage_scope = "selection-eligible-candidates";
    }],
    ["aligned unfiltered winner", (candidate) => {
      candidate.unfiltered_score_winner_selected = true;
      candidate.runtime_receipt.unfiltered_score_winner_selected = true;
    }],
    ["statistics rejection-count drift", (candidate) => {
      candidate.stats.pv_horizon_line_rejections = 0;
    }],
    ["statistics repair-count drift", (candidate) => {
      candidate.stats.pv_horizon_native_repairs = 0;
    }],
    ["aligned unresolved line rejection", (candidate) => {
      candidate.pv_horizon_native_repairs = 0;
      candidate.stats.pv_horizon_native_repairs = 0;
      candidate.runtime_receipt.pv_horizon_native_repairs = 0;
    }],
    ["aligned forged veto count", (candidate) => {
      candidate.pv_horizon_candidate_vetoes = 1;
      candidate.stats.pv_horizon_candidate_vetoes = 1;
      candidate.runtime_receipt.pv_horizon_candidate_vetoes = 1;
    }],
    ["receipt selection-policy drift", (candidate) => {
      candidate.runtime_receipt.selection_policy = "untrusted-policy-v0";
    }],
    ["receipt selection-filter drift", (candidate) => {
      candidate.runtime_receipt.selection_policy_filtered = true;
    }],
    ["receipt rejection-count drift", (candidate) => {
      candidate.runtime_receipt.pv_horizon_line_rejections = 0;
    }],
    ["receipt coverage-scope drift", (candidate) => {
      candidate.runtime_receipt.root_bound_coverage_scope = "selection-eligible-candidates";
    }],
    ["receipt unfiltered-winner drift", (candidate) => {
      candidate.runtime_receipt.unfiltered_score_winner_selected = true;
    }],
  ]) {
    assertFilteredMutationRejected(label, mutate);
  }
  client.close();
}

async function testInternalOpponentBoundaryMateIsProbedLeafFirst() {
  const world = new MockWorld({
    internalBoundaryMateFirst: true,
    zeroNativeWork: true,
    rootSafetyWork: 7,
    singleCandidate: true,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot(
    payload(boundaryPayload(WHITE_FEN, 1), 5),
    { deadlineMs: performance.now() + 20_000 },
  );
  const d5Safety = world.safetyReceipts.filter((receipt) => (
    receipt.iteration_id.endsWith(":d5")
    && receipt.candidate_identity === "c0"
  ));
  assert.deepEqual(
    d5Safety.slice(0, 2).map((receipt) => receipt.authoritative_child_boundary?.series),
    [6, 4],
    "the exact selected-PV mate ladder must probe opponent boundaries leaf-first",
  );
  assert.equal(d5Safety[1]?.status, "found");
  assert.equal(result.pv_horizon_line_rejections, 1);
  assert.equal(result.pv_horizon_native_repairs, 1);
  const repair = world.searchDispatches.find(({ task }) => (
    task.iteration_id.endsWith(":d5")
    && task.schema === "spc-root-horizon-research-task-v1"
  ));
  assert(repair, "the internal-boundary proof must return to the warm owner");
  const rootedProof = repair.task.horizon_proofs[0].rooted_path;
  assert.equal(rootedProof.length, 3);
  assert.deepEqual(rootedProof[0].moves, candidateMoves(1, 0));
  assert.deepEqual(
    rootedProof.slice(1),
    d5Safety[1].candidate.child_pv.slice(0, 2),
  );
  assert.equal(
    result.work,
    world.safetyReceipts.reduce((sum, receipt) => sum + receipt.work_used, 0),
    "the published work total must include every exact ladder probe",
  );
  client.close();
}

async function testBlackRootInternalOpponentBoundaryMateUsesExactParity() {
  const world = new MockWorld({
    internalBoundaryMateSeries: 5,
    zeroNativeWork: true,
    rootSafetyWork: 7,
    singleCandidate: true,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot(
    payload(boundaryPayload(BLACK_FEN, 2), 5),
    { deadlineMs: performance.now() + 20_000 },
  );
  const d5Safety = world.safetyReceipts.filter((receipt) => (
    receipt.iteration_id.endsWith(":d5")
    && receipt.candidate_identity === "c0"
  ));
  assert.deepEqual(
    d5Safety.slice(0, 2).map((receipt) => receipt.authoritative_child_boundary?.series),
    [7, 5],
    "a Black root must probe White-to-move boundaries leaf-first",
  );
  assert.equal(d5Safety[1]?.status, "found");
  assert.equal(d5Safety[1]?.override_score, CONFIG.mate_score - 2);
  assert.deepEqual(d5Safety[1]?.proof_bounds, [1, 1]);
  assert.equal(result.pv_horizon_line_rejections, 1);
  assert.equal(result.pv_horizon_native_repairs, 1);
  const repair = world.searchDispatches.find(({ task }) => (
    task.iteration_id.endsWith(":d5")
    && task.schema === "spc-root-horizon-research-task-v1"
  ));
  assert(repair, "the Black-root internal proof must return to its warm owner");
  assert.equal(repair.task.horizon_proofs[0].rooted_path.length, 3);
  assert.deepEqual(
    repair.task.horizon_proofs[0].rooted_path[0].moves,
    candidateMoves(2, 0),
  );
  client.close();
}

async function testCachedInternalMateShortCircuitsUncachedDeeperBoundary() {
  for (const [boundary, internalSeries, deeperSeries, expectedMateMoves] of [
    [boundaryPayload(WHITE_FEN, 1), 4, 6, ["a7a6", "h7h6"]],
    [boundaryPayload(BLACK_FEN, 2), 5, 7, ["a2a3", "h2h3", "g2g3"]],
  ]) {
    const world = new MockWorld({
      internalBoundaryMateSeries: internalSeries,
      zeroNativeWork: true,
      rootSafetyWork: 7,
      singleCandidate: true,
    });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    const request = payload(boundary, 5);
    const first = await client.analyzeRoot(request, {
      deadlineMs: performance.now() + 20_000,
    });
    assert.equal(first.pv_horizon_line_rejections, 1);
    assert.deepEqual(
      world.safetyReceipts.filter((receipt) => (
        receipt.iteration_id.endsWith(":d5")
        && receipt.candidate_identity === "c0"
      )).slice(0, 2).map((receipt) => receipt.authoritative_child_boundary?.series),
      [deeperSeries, internalSeries],
      "the cold run must establish an exact cached internal mate after the deeper miss",
    );

    const safetyStart = world.safetyReceipts.length;
    const searchStart = world.searchDispatches.length;
    const rootChildReceipt = world.safetyReceipts.find((receipt) => (
      receipt.authoritative_child_boundary?.series === boundary.series + 1
    ));
    assert(rootChildReceipt, "the warmup must populate the exact root-child exhaustion");
    client.rootRunner.mateProofCache.delete(rootClientApi.mateProofCacheKey(
      IDENTITY,
      rootChildReceipt.authoritative_child_boundary,
    ));
    world.rootSafetyWork = 998;
    world.deepestBoundaryQuietSeries = 1;
    const second = await client.analyzeRoot({
      ...request,
      max_generation_positions: 1_000,
    }, {
      deadlineMs: performance.now() + 20_000,
    });
    const secondD5Safety = world.safetyReceipts.slice(safetyStart).filter((receipt) => (
      receipt.iteration_id.endsWith(":d5")
      && receipt.candidate_identity === "c0"
    ));
    assert.deepEqual(
      secondD5Safety,
      [],
      "a replayed cached internal mate must reject before dispatching the changed deeper boundary",
    );
    assert.equal(second.pv_horizon_line_rejections, 1);
    assert.equal(second.pv_horizon_native_repairs, 1);
    assert(second.runtime_receipt.mate_cache.hits > 0);
    const repair = world.searchDispatches.slice(searchStart).find(({ task }) => (
      task.iteration_id.endsWith(":d5")
      && task.schema === "spc-root-horizon-research-task-v1"
    ));
    assert(repair, "the cached internal proof must remain authoritative for warm-owner repair");
    const proof = repair.task.horizon_proofs[0];
    assert.equal(proof.rooted_path.length, 3);
    assert.equal(proof.rooted_path.at(-1).child_boundary.series, internalSeries);
    assert.deepEqual(proof.mate_reply.moves, expectedMateMoves);
    assert.equal(proof.mate_reply.child_boundary.series, internalSeries + 1);
    client.close();
  }
}

async function testTerminalPvLeafStillChecksEarlierOpponentBoundary() {
  const world = new MockWorld({
    internalBoundaryMateFirst: true,
    terminalFinalPv: true,
    zeroNativeWork: true,
    singleCandidate: true,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot(
    payload(boundaryPayload(WHITE_FEN, 1), 5),
    { deadlineMs: performance.now() + 20_000 },
  );
  const d5Safety = world.safetyReceipts.filter((receipt) => (
    receipt.iteration_id.endsWith(":d5")
    && receipt.candidate_identity === "c0"
  ));
  assert.deepEqual(
    d5Safety.map((receipt) => receipt.authoritative_child_boundary?.series),
    [4],
    "a terminal final PV series must be skipped without hiding the earlier S4 boundary",
  );
  assert.equal(d5Safety[0]?.candidate.child_pv.at(-1)?.outcome, "checkmate");
  assert.equal(d5Safety[0]?.status, "found");
  assert.equal(result.pv_horizon_line_rejections, 1);
  assert.equal(result.pv_horizon_native_repairs, 1);
  const repair = world.searchDispatches.find(({ task }) => (
    task.iteration_id.endsWith(":d5")
    && task.schema === "spc-root-horizon-research-task-v1"
  ));
  assert(repair, "the earlier adverse boundary must still force a warm-owner repair");
  assert.equal(repair.task.horizon_proofs[0].rooted_path.length, 3);
  client.close();
}

async function testMalformedInternalRootedProofsFailClosed() {
  for (const [label, pathIndex, replacementMoves] of [
    ["wrong retained root", 0, candidateMoves(1, 1)],
    ["non-prefix internal series", 1, candidateMoves(2, 1)],
  ]) {
    let mutationCount = 0;
    const isolatedRootApi = rootIterationApiWithSafetyMutation((safety) => {
      if (safety?.status !== "line-rejected") return safety;
      mutationCount += 1;
      const mutated = structuredClone(safety);
      mutated.horizon_proof.rooted_path[pathIndex].moves = [...replacementMoves];
      mutated.horizon_proof.rooted_path[pathIndex].machine_notation = replacementMoves.join("/");
      return mutated;
    });
    const world = new MockWorld({ internalBoundaryMateFirst: true });
    const runner = new isolatedRootApi.RootIterationRunner({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    const result = await runner.analyze(
      payload(boundaryPayload(WHITE_FEN, 1), 5),
      IDENTITY,
      { deadlineMs: performance.now() + 20_000 },
    );
    assert.equal(mutationCount, 1, `${label} fixture must mutate one internal proof`);
    assert.equal(result.completed_depth, 4, `${label} must not publish D5`);
    assert.equal(
      result.runtime_receipt.interruption_code,
      "root-safety-result-invalid",
      `${label} must be rejected by the retained-root proof contract`,
    );
    assert.equal(
      world.searchDispatches.some(({ task }) => (
        task.iteration_id.endsWith(":d5")
        && task.schema === "spc-root-horizon-research-task-v1"
      )),
      false,
      `${label} must fail before any warm-owner repair`,
    );
    runner.close();
  }
}

async function testUnknownInternalOpponentBoundaryFailsClosed() {
  const world = new MockWorld({
    internalBoundaryMateFirst: true,
    internalBoundarySafetyUnknown: true,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot(
    payload(boundaryPayload(WHITE_FEN, 1), 5),
    { deadlineMs: performance.now() + 20_000 },
  );
  assert.equal(result.completed_depth, 4);
  assert.equal(result.runtime_receipt.interruption_code, "root-safety-unknown");
  const d5Safety = world.safetyReceipts.filter((receipt) => (
    receipt.iteration_id.endsWith(":d5")
    && receipt.candidate_identity === "c0"
  ));
  assert.deepEqual(
    d5Safety.map((receipt) => [
      receipt.authoritative_child_boundary?.series,
      receipt.status,
    ]),
    [[6, "exhausted"], [4, "unknown"]],
  );
  assert.equal(
    d5Safety.some((receipt) => receipt.authoritative_child_boundary?.series === 2),
    false,
    "UNKNOWN at an internal boundary must not fall through to a shallower probe",
  );
  const unknownBoundary = d5Safety.find((receipt) => receipt.status === "unknown")
    ?.authoritative_child_boundary;
  assert(unknownBoundary, "the internal UNKNOWN receipt must retain its exact boundary");
  const unknownCacheKey = rootClientApi.mateProofCacheKey(client.identity, unknownBoundary);
  assert.equal(
    client.rootRunner.mateProofCache.has(unknownCacheKey),
    false,
    "UNKNOWN internal proofs must never enter the exact mate cache",
  );
  const repeated = await client.analyzeRoot(
    payload(boundaryPayload(WHITE_FEN, 1), 5),
    { deadlineMs: performance.now() + 20_000 },
  );
  assert.equal(repeated.completed_depth, 4);
  const repeatedUnknowns = world.safetyReceipts.filter((receipt) => (
    receipt.candidate_identity === "c0"
    && receipt.authoritative_child_boundary?.series === 4
    && receipt.status === "unknown"
  ));
  assert.equal(
    repeatedUnknowns.length,
    2,
    "a second request must re-probe rather than reuse an UNKNOWN internal boundary",
  );
  client.close();
}

async function testSecondDistinctCheckedPvMatePublishesExactPolicyVeto() {
  const world = new MockWorld({ horizonMateFirst: true, horizonMateTwice: true });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const requestPayload = payload(boundaryPayload(WHITE_FEN, 1), 5);
  const result = await client.analyzeRoot(requestPayload, {
    deadlineMs: performance.now() + 20_000,
  });
  const expectedVetoes = [{
    schema: "spc-pv-horizon-candidate-veto-v1",
    candidate_identity: "c0",
    reason: "same-root-repair-limit",
    maximum_successful_same_root_repairs: 1,
    repairs_before_veto: 1,
    retained_proofs_before_veto: 1,
    distinct_proofs_observed: 2,
  }];
  assert.equal(
    result.completed_depth,
    5,
    String(result.runtime_receipt?.interruption_code || "D5 did not complete"),
  );
  assert.notDeepEqual(result.best_full_series, candidateMoves(1, 0));
  assert.equal(result.pv_horizon_line_rejections, 2);
  assert.equal(result.pv_horizon_native_repairs, 1);
  assert.equal(result.pv_horizon_candidate_vetoes, 1);
  assert.equal(result.selection_policy_filtered, true);
  assertPolicyReceiptSurfaces(result, expectedVetoes);

  const assertMutationRejected = (label, mutate) => {
    const candidate = structuredClone(result);
    mutate(candidate);
    assert.throws(
      () => browserClientApi.validatePublishedRootAnalysis(
        candidate,
        requestPayload,
        client.identity,
      ),
      (error) => error?.code === "browser-root-result-invalid",
      label,
    );
  };
  for (const [label, mutate] of [
    ["aligned repair-policy drift must fail closed", (candidate) => {
      for (const surface of [candidate, candidate.stats, candidate.runtime_receipt]) {
        surface.same_root_repair_policy.maximum_successful_same_root_repairs = 2;
      }
    }],
    ["policy-veto surface mismatch must fail closed", (candidate) => {
      candidate.stats.pv_horizon_policy_vetoes = [];
    }],
    ["aligned policy-veto extension must fail exact-key validation", (candidate) => {
      for (const surface of [candidate, candidate.stats, candidate.runtime_receipt]) {
        surface.pv_horizon_policy_vetoes[0].unexpected = true;
      }
    }],
    ["aligned threshold-accounting drift must fail closed", (candidate) => {
      for (const surface of [candidate, candidate.stats, candidate.runtime_receipt]) {
        surface.pv_horizon_policy_vetoes[0].distinct_proofs_observed = 3;
      }
    }],
    ["candidate-veto count must equal the policy-veto array length", (candidate) => {
      candidate.pv_horizon_line_rejections = 3;
      candidate.pv_horizon_candidate_vetoes = 2;
      candidate.stats.pv_horizon_line_rejections = 3;
      candidate.stats.pv_horizon_candidate_vetoes = 2;
      candidate.runtime_receipt.pv_horizon_line_rejections = 3;
      candidate.runtime_receipt.pv_horizon_candidate_vetoes = 2;
    }],
  ]) assertMutationRejected(label, mutate);
  client.close();
}


async function testCheckedPvHorizonIsRootedAndFailClosed() {
  for (const mutate of [
    async (base, worker, type, request) => {
      const result = await base(worker, type, request);
      if (type !== "prefix" || request.boundary.series !== 3) return result;
      return prefixResult(request, IDENTITY, {
        child: exactState(boundaryPayload(flipFen(ALT_WHITE_FEN), 4)),
        endedByCheck: false,
      });
    },
    async (base, worker, type, request) => {
      const result = await base(worker, type, request);
      if (type !== "prefix" || request.boundary.series !== 5) return result;
      return {
        ...result,
        completion_reason: "budget",
        check: false,
        ended_by_check: false,
        in_check: false,
      };
    },
    async (base, worker, type, request) => {
      const result = await base(worker, type, request);
      if (
        type !== "root-safety"
        || request.authoritative_child_boundary?.series !== 6
      ) return result;
      return { ...result, safety_revision: result.safety_revision + 1 };
    },
  ]) {
    const world = new MockWorld({ horizonMateFirst: true });
    const base = world.handle.bind(world);
    world.handle = (worker, type, request) => mutate(base, worker, type, request);
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    const result = await client.analyzeRoot(
      payload(boundaryPayload(WHITE_FEN, 1), 5),
      { deadlineMs: performance.now() + 20_000 },
    );
    assert.equal(result.completed_depth, 4, "an unrooted D5 veto must not publish");
    assert.equal(result.runtime_receipt.interruption_code, "root-safety-unknown");
    assert.equal(result.selection_policy_filtered, false);
    client.close();
  }
}


async function testFavorableCheckedHorizonIsNotVetoed() {
  const world = new MockWorld({ favorableHorizonFirst: true });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot(
    payload(boundaryPayload(WHITE_FEN, 1), 4),
    { deadlineMs: performance.now() + 20_000 },
  );
  assert.equal(result.completed_depth, 4);
  assert.deepEqual(result.best_full_series, candidateMoves(1, 0));
  assert.equal(result.selection_policy_filtered, false);
  assert.equal(result.pv_horizon_line_rejections, 0);
  assert.equal(
    world.safetyReceipts.some((receipt) => (
      receipt.authoritative_child_boundary?.series === 5
    )),
    false,
    "a root-mover mate at an odd horizon ply must not be treated as adverse",
  );
  client.close();
}

async function testUnknownBeforeD1PublishesNoMoveForEitherColor() {
  for (const boundary of [
    boundaryPayload(WHITE_FEN, 1),
    boundaryPayload(BLACK_FEN, 2),
  ]) {
    const world = new MockWorld({ safetyUnknown: true });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    let caught = null;
    try {
      await client.analyzeRoot(payload(boundary, 1), {
        deadlineMs: performance.now() + 20_000,
      });
    } catch (error) {
      caught = error;
    }
    assert(caught, "UNKNOWN safety before D1 must reject instead of returning a move");
    assert.equal(caught.code, "root-safety-unknown");
    assert.equal(caught.best_full_series, undefined);
    assert.equal(caught.publishable, undefined);
    assert.equal(client.rootRunner.lastSafe, null);
    assert.equal(world.terminalMateReceipts.length, 0);
    assert.equal(world.proactiveTerminalMateReceipts.length, 0);
    client.close();
  }
}

async function testUnknownAfterD1ReturnsOnlyCertifiedLastSafe() {
  const world = new MockWorld({
    horizonMateFirst: true,
    horizonSafetyUnknown: true,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot(
    payload(boundaryPayload(WHITE_FEN, 1), 5),
    { deadlineMs: performance.now() + 20_000 },
  );
  const lastSafe = client.rootRunner.lastSafe;
  assert(lastSafe, "D4 must remain the last fully certified publication");
  assert.equal(result.completed_depth, 4);
  assert.equal(lastSafe.completed_depth, 4);
  assert.equal(result.safety_certified, true);
  assert.equal(result.authoritative_replay_certified, true);
  assert.equal(result.runtime_receipt.interruption_code, "root-safety-unknown");
  assert.deepEqual(result.best_full_series, lastSafe.best_full_series);
  assert.deepEqual(result.principal_variation, lastSafe.principal_variation);
  assert.deepEqual(result.checked_prefix, lastSafe.checked_prefix);
  assert.deepEqual(result.proof_bounds, lastSafe.proof_bounds);
  assert.equal(result.score, lastSafe.score);
  assert.equal(result.work, lastSafe.work);
  assert.equal(result.runtime_receipt.terminal_mate_rescue, undefined);
  assert.equal(world.terminalMateReceipts.length, 0);
  client.close();
}

async function testProactiveS5TerminalMateWinsBeforeOrdinarySearch() {
  const boundary = boundaryPayload(MISSED_S5_MATE_FEN, 5);
  const world = new MockWorld({
    proactiveTerminalMateStatus: "found",
    proactiveTerminalMateWork: 505,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot(payload(boundary, 5), {
    deadlineMs: performance.now() + 20_000,
  });
  assert.equal(result.status, "complete");
  assert.equal(result.publishable, true);
  assert.equal(result.safety_certified, true);
  assert.equal(result.authoritative_replay_certified, true);
  assert.equal(result.completed_depth, 1);
  assert.deepEqual(result.best_full_series, MISSED_S5_MATE_MOVES);
  assert.deepEqual(result.principal_variation[0].moves, MISSED_S5_MATE_MOVES);
  assert.equal(result.score, CONFIG.mate_score - 1);
  assert.equal(result.proof, "white");
  assert.deepEqual(result.proof_bounds, [1, 1]);
  assert.equal(result.checked_prefix.outcome, "checkmate");
  assert.equal(result.checked_prefix.ended_by_check, true);
  assert.deepEqual(result.checked_prefix.next_state, exactState(MISSED_S5_MATE_CHILD));
  assert.equal(result.stats.root_tasks, 0);
  assert.equal(result.stats.safety_status, "terminal-mate-rescue");
  assert.equal(result.stats.terminal_mate_rescues, 1);
  assert.equal(result.stats.safety_reserve_positions, ROOT_CURRENT_SERIES_MATE_CREDIT);
  assert.equal(
    result.runtime_receipt.terminal_mate_rescue.trigger,
    "proactive-current-series-terminal-mate",
  );
  assert.equal(result.runtime_receipt.terminal_mate_rescue.status, "found");
  assert.equal(
    result.runtime_receipt.terminal_mate_rescue.work_used,
    world.proactiveTerminalMateWork,
  );
  assertPolicyReceiptSurfaces(result, []);
  assert.equal(result.work, world.proactiveTerminalMateWork);
  assert.equal(result.stats.generation_positions, world.proactiveTerminalMateWork);
  assert.equal(world.proactiveTerminalMateReceipts.length, 1);
  assert.equal(
    world.proactiveTerminalMateReceipts[0].call_work_credit,
    ROOT_CURRENT_SERIES_MATE_CREDIT,
  );
  assert.equal(world.searchDispatches.length, 0);
  assert.equal(world.safetyReceipts.length, 0);
  assert.equal(world.terminalMateReceipts.length, 0);
  assert(world.workers.every((worker) => worker.manifest === null));
  client.close();
}

async function testUnprovenProactiveTerminalMateContinuesOrdinarySearch() {
  for (const proactiveTerminalMateStatus of ["exhausted", "unknown"]) {
    const world = new MockWorld({
      proactiveTerminalMateStatus,
      proactiveTerminalMateWork: 13,
    });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    const result = await client.analyzeRoot(
      payload(boundaryPayload(MISSED_S5_MATE_FEN, 5), 1),
      { deadlineMs: performance.now() + 20_000 },
    );
    assert.equal(result.completed_depth, 1);
    assert.equal(world.proactiveTerminalMateReceipts.length, 1);
    assert.equal(
      world.proactiveTerminalMateReceipts[0].status,
      proactiveTerminalMateStatus,
    );
    assert.equal(world.proactiveTerminalMateReceipts[0].work_used, 13);
    assert(world.enumerationRequests.length >= 1);
    assert.equal(world.enumerationRequests[0].external_work, 13);
    assert(
      world.enumerationRequests[0].deadline_monotonic_ms
        > world.proactiveTerminalMateReceipts[0].deadline_monotonic_ms,
    );
    assert(
      world.proactiveTerminalMateReceipts[0].remaining_time_ms <= 1_000,
    );
    assert(
      world.enumerationRequests[0].remaining_time_ms
        > world.proactiveTerminalMateReceipts[0].remaining_time_ms,
    );
    assert(world.searchDispatches.length > 0);
    assert(result.work >= 13);
    client.close();
  }
}

async function testProactiveProbeTimeoutRecoversOrdinarySearch() {
  const world = new MockWorld({
    proactiveTerminalMateStatus: "no-reply",
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const probesBeforeAnalyze = world.probes;
  const result = await client.analyzeRoot(
    payload(boundaryPayload(MISSED_S5_MATE_FEN, 5), 1),
    { deadlineMs: performance.now() + 1_000 },
  );
  assert.equal(result.completed_depth, 1);
  assert.equal(world.proactiveTerminalMateRequests.length, 1);
  assert.equal(world.proactiveTerminalMateReceipts.length, 0);
  assert.equal(
    world.probes - probesBeforeAnalyze,
    GEOMETRY.desktop_workers * 2,
  );
  assert(world.enumerationRequests.length >= 1);
  assert.equal(
    world.enumerationRequests[0].external_work,
    ROOT_CURRENT_SERIES_MATE_CREDIT,
  );
  assert(world.searchDispatches.length > 0);
  assert.equal(world.live, GEOMETRY.desktop_workers);
  client.close();
}

async function testAllMatingFrontierRescuesTerminalRootMate() {
  for (const [boundary, expectedScore, expectedProof] of [
    [boundaryPayload(WHITE_FEN, 1), CONFIG.mate_score - 1, "white"],
    [boundaryPayload(BLACK_FEN, 2), -CONFIG.mate_score + 1, "black"],
  ]) {
    const world = new MockWorld({ foundAll: true });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    const request = payload(boundary, 1);
    request.max_generation_positions = 60_000_000;
    request.time_limit = 60;
    const result = await client.analyzeRoot(request, {
      deadlineMs: performance.now() + 20_000,
    });
    assert.equal(result.publishable, true);
    assert.equal(result.completed_depth, 1);
    assert.equal(result.score, expectedScore);
    assert.equal(result.proof, expectedProof);
    assert.deepEqual(result.proof_bounds, expectedProof === "white" ? [1, 1] : [-1, -1]);
    assert.equal(result.checked_prefix.outcome, "checkmate");
    assert.equal(result.stats.terminal_mate_rescues, 1);
    assert.equal(result.stats.safety_status, "terminal-mate-rescue");
    assert.equal(result.runtime_receipt.terminal_mate_rescue.status, "found");
    assertPolicyReceiptSurfaces(result, []);
    assert.equal(world.terminalMateReceipts.length, 1);
    assert(world.safetyReceipts.length >= 8);
    client.close();
  }
}

async function testUnprovenTerminalMateRescueFailsClosed() {
  for (const terminalMateStatus of ["unknown", "exhausted"]) {
    const world = new MockWorld({ foundAll: true, terminalMateStatus });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    const request = payload(boundaryPayload(WHITE_FEN, 1), 1);
    request.max_generation_positions = 60_000_000;
    request.time_limit = 60;
    await assert.rejects(
      client.analyzeRoot(request, {
        deadlineMs: performance.now() + 20_000,
      }),
      (error) => error?.code === "root-safety-widening-required",
    );
    assert.equal(world.terminalMateReceipts.length, 1);
    assert.equal(client.rootRunner.lastSafe, null);
    client.close();
  }
}

async function testSafeRootReselectSkipsFoundAndUnknownThenPublishesExhausted() {
  const world = new MockWorld({
    foundAll: true,
    terminalMateStatus: "exhausted",
    wideSafetyStatuses: ["unknown", "exhausted"],
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const request = {
    ...payload(boundaryPayload(WHITE_FEN, 1), 1),
    time_limit: 60,
    max_generation_positions: 60_000_000,
  };
  const result = await client.analyzeRoot(request, {
    deadlineMs: performance.now() + 20_000,
  });
  const receipt = result.runtime_receipt.safe_root_reselector;
  assert.equal(result.publishable, true);
  assert.equal(result.completed_depth, 0);
  assert.equal(result.safety_certification_scope, SAFE_RESELECT_REPLY_MATE_SCOPE);
  assert.equal(result.root_scores_complete, false);
  assert.equal(result.root_bound_coverage_complete, false);
  assert.equal("score" in result, false);
  assert.equal("proof" in result, false);
  assert.equal("proof_bounds" in result, false);
  assert.deepEqual(result.principal_variation, []);
  assert.equal(receipt.schema, "spc-root-safe-reselector-receipt-v1");
  assert.equal(receipt.requested_width, SAFE_RESELECT_WIDTH);
  assert.equal(
    receipt.order_policy,
    "native-tactical-protected-root-production-order-empty-preference-v1",
  );
  assert.equal(receipt.root_order_tactical_protection, true);
  assert.deepEqual(receipt.preferred_series, []);
  assert.equal(receipt.safety_certification_scope, SAFE_RESELECT_REPLY_MATE_SCOPE);
  assert.equal(receipt.selected_safety_basis, "exact-immediate-reply-mate-exhaustion");
  assert.equal(receipt.immediate_reply_mate_horizon_series, 1);
  assert.equal(receipt.early_frontier_count, 32);
  assert.equal(
    receipt.early_frontier_child_max_work,
    SAFE_RESELECT_EARLY_CHILD_WORK,
  );
  assert.equal(
    receipt.widened_frontier_child_max_work,
    SAFE_RESELECT_WIDENED_CHILD_WORK,
  );
  assert.equal(receipt.lane_max_work, SAFE_RESELECT_TOTAL_WORK);
  assert.deepEqual(receipt.scans.slice(-2).map((scan) => scan.status), [
    "unknown", "exhausted",
  ]);
  assert.equal(receipt.selected.status, "exhausted");
  assert.equal(receipt.selected.order_index, 13);
  assert.equal(receipt.selected.frontier_stage, "retained-w32");
  assert.equal(receipt.selected.per_child_max_work, SAFE_RESELECT_EARLY_CHILD_WORK);
  assert.deepEqual(
    receipt.selected.authoritative_child_boundary,
    result.checked_prefix.next_state,
  );
  assert(Object.isFrozen(receipt));
  assert(Object.isFrozen(receipt.scans));
  assert(Object.isFrozen(receipt.selected));
  assert.equal(world.safeReselectCreates.length, 1);
  assert.equal(world.safeReselectCreates[0].payload.request.config, undefined);
  assert.equal(receipt.ordinary_pool_recreated, false);
  assert.equal(receipt.ordinary_pool_restore_policy, "lazy-next-request");
  assert.equal(receipt.restore_error_code, null);
  assert.equal(world.live, 0, "the isolated lane must release every Worker before publication");
  world.foundAll = false;
  world.pvTransitions.clear();
  client.rootRunner.mateProofCache.clear();
  const next = await client.analyzeRoot(payload(boundaryPayload(WHITE_FEN, 1), 1), {
    deadlineMs: performance.now() + 20_000,
  });
  assert.equal(next.completed_depth, 1);
  assert.equal(world.live, GEOMETRY.desktop_workers, "the next request must rebuild lazily");
  client.close();
}

async function testSafeRootReselectReachesRank62WithStagedCredits() {
  const wideStatuses = [
    ...Array.from({ length: 49 }, () => "found"),
    "exhausted",
  ];
  const world = new MockWorld({
    foundAll: true,
    terminalMateStatus: "exhausted",
    rootSafetyWork: 250_000,
    wideSafetyStatuses: wideStatuses,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot({
    ...payload(boundaryPayload(WHITE_FEN, 1), 1),
    time_limit: 60,
    max_generation_positions: 60_000_000,
  }, { deadlineMs: performance.now() + 20_000 });
  const receipt = result.runtime_receipt.safe_root_reselector;
  assert.equal(receipt.selected.order_index, 61, "the 1-based rank-62 witness must be reached");
  assert.equal(receipt.selected.candidate_identity, "w61");
  assert.equal(receipt.selected.order_key, "a1g3");
  assert.equal(receipt.selected.root_series.machine_notation, "a1g3");
  assert.equal(receipt.root_order_tactical_protection, true);
  assert.equal(WIDE_CONFIG.root_tactical_protection, false);
  assert.equal(receipt.selected.status, "exhausted");
  assert.equal(receipt.selected.frontier_stage, "widened-w512");
  assert.equal(receipt.scans.length, 62);
  assert.equal(world.safeReselectSafetyReceipts.length, 50);
  assert(world.safeReselectSafetyReceipts.every((reply) => (
    reply.call_work_credit === (
      reply.candidate.order_index < SAFE_RESELECT_EARLY_FRONTIER_COUNT
        ? SAFE_RESELECT_EARLY_CHILD_WORK
        : SAFE_RESELECT_WIDENED_CHILD_WORK
    )
  )));
  assert.equal(receipt.early_frontier_work_used, 20 * 250_000);
  assert.equal(receipt.widened_frontier_work_used, 30 * 250_000);
  assert.equal(
    receipt.lane_work_used,
    receipt.generation_work
      + receipt.early_frontier_work_used
      + receipt.widened_frontier_work_used,
  );
  assert(receipt.lane_work_used < SAFE_RESELECT_TOTAL_WORK);
  assert(receipt.total_committed_work <= 60_000_000);
  client.close();
}

async function testSafeRootReselectNeverResurrectsRejectedRoots() {
  for (const scenario of [
    {
      label: "checked-PV policy veto",
        world: {
          foundAll: true,
          horizonMateFirst: true,
          horizonMateTwice: true,
          horizonMateChildDepth: 0,
        },
        depth: 1,
      expectedPolicyRejected: true,
      expectedQuarantines: 0,
    },
    {
      label: "mate-claim quarantine",
      world: {
        foundAll: true,
        unprovedMateFirst: true,
      },
      depth: 1,
      expectedPolicyRejected: false,
      expectedQuarantines: 1,
    },
  ]) {
    const world = new MockWorld({
      ...scenario.world,
      terminalMateStatus: "exhausted",
      wideSafetyStatuses: ["exhausted"],
    });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    const result = await client.analyzeRoot({
      ...payload(boundaryPayload(WHITE_FEN, 1), scenario.depth),
      time_limit: 60,
      max_generation_positions: 60_000_000,
    }, { deadlineMs: performance.now() + 20_000 });
    const receipt = result.runtime_receipt.safe_root_reselector;
    assert(receipt, `${scenario.label}: ${JSON.stringify({
      completed_depth: result.completed_depth,
      search_mode: result.runtime_receipt.search_mode,
      interruption_code: result.runtime_receipt.interruption_code,
      policy_vetoes: result.pv_horizon_candidate_vetoes,
      mate_quarantines: result.root_mate_claim_quarantines,
    })}`);
    assert.equal(receipt.excluded_root_count, 1, scenario.label);
    assert.equal(receipt.exclusion_binding_policy,
      "unique-exact-authoritative-child-boundary-v1", scenario.label);
    assert.equal(receipt.exclusions_bound, true, scenario.label);
    const [exclusion] = receipt.excluded_roots;
    assert.equal(exclusion.source_candidate_identity, "c0", scenario.label);
    assert.equal(exclusion.source_order_index, 0, scenario.label);
    assert.equal(exclusion.source_order_key, "e2e4", scenario.label);
    assert.equal(exclusion.source_root_machine_notation, "e2e4", scenario.label);
    assert.equal(exclusion.widened_candidate_identity, "c0", scenario.label);
    assert.equal(exclusion.widened_order_index, 0, scenario.label);
    assert.equal(exclusion.widened_order_key, "e2e4", scenario.label);
    assert.equal(exclusion.widened_root_machine_notation, "e2e4", scenario.label);
    assert.deepEqual(
      exclusion.source_child_boundary,
      exclusion.widened_child_boundary,
      scenario.label,
    );
    assert.equal(
      exclusion.checked_pv_policy_rejected,
      scenario.expectedPolicyRejected,
      scenario.label,
    );
    assert.equal(
      exclusion.mate_claim_quarantine_count,
      scenario.expectedQuarantines,
      scenario.label,
    );
    assert.equal(receipt.scans[0].status, "policy-excluded", scenario.label);
    assert.deepEqual(receipt.scans[0].exclusion, receipt.excluded_roots[0]);
    assert.equal(receipt.scans[0].call_work_credit, 0);
    assert.equal(receipt.scans[0].work_used, 0);
    assert.equal(receipt.selected.order_index, 12, scenario.label);
    assert.notDeepEqual(result.best_full_series, ["e2e4"], scenario.label);
    assert.equal(world.safeReselectSafetyReceipts[0].candidate.order_index, 12);
    assert.equal(world.safeReselectSafetyReceipts.some(
      (reply) => reply.candidate.root_series.machine_notation === "e2e4",
    ), false, scenario.label);
    client.close();
  }
}

async function testSafeRootReselectRemapsPreferredOrderExclusionByExactChild() {
  const world = new MockWorld({
    foundAll: true,
    foundAllChildDepth: 1,
    preferredWinnerIndex: 5,
    unprovedMateFirst: true,
    unprovedMateCandidateIndex: 5,
    unprovedMateChildDepth: 1,
    terminalMateStatus: "exhausted",
    wideSafetyStatuses: [
      "found", "found", "found", "found", "found", "exhausted",
    ],
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot({
    ...payload(boundaryPayload(WHITE_FEN, 1), 2),
    time_limit: 60,
    max_generation_positions: 60_000_000,
  }, { deadlineMs: performance.now() + 20_000 });
  const receipt = result.runtime_receipt.safe_root_reselector;
  assert(receipt, JSON.stringify({
    completed_depth: result.completed_depth,
    best_full_series: result.best_full_series,
    search_mode: result.runtime_receipt.search_mode,
    safety_status: result.stats.safety_status,
    quarantines: result.root_mate_claim_quarantines,
    c5_dispatches: world.searchDispatches.filter(
      (entry) => entry.task.candidate_identity === "c5",
    ).map((entry) => ({
      generation: entry.task.generation,
      child_depth: entry.task.child_depth,
      purpose: entry.task.purpose,
    })),
  }));
  assert.equal(receipt.excluded_root_count, 1);
  assert.equal(receipt.excluded_roots[0].source_candidate_identity, "c5");
  assert.equal(receipt.excluded_roots[0].source_order_index, 0);
  assert.equal(receipt.excluded_roots[0].widened_candidate_identity, "c5");
  assert.equal(receipt.excluded_roots[0].widened_order_index, 5);
  assert.deepEqual(
    receipt.excluded_roots[0].source_child_boundary,
    receipt.scans[5].authoritative_child_boundary,
  );
  assert.equal(receipt.scans[5].status, "policy-excluded");
  assert.equal(receipt.scans[5].call_work_credit, 0);
  assert.equal(receipt.scans[5].work_used, 0);
  assert.equal(receipt.selected.order_index, 17);
  assert.equal(receipt.selected.status, "exhausted");
  assert.equal(world.safeReselectSafetyReceipts.some(
    (reply) => reply.candidate.candidate_identity === "c5",
  ), false);
  client.close();
}

async function testTerminalProbeCannotStarveSafeRootReselectLane() {
  const world = new MockWorld({
    foundAll: true,
    terminalMateStatus: "unknown",
    terminalMateWork: Number.MAX_SAFE_INTEGER,
    wideSafetyStatuses: ["exhausted"],
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot({
    ...payload(boundaryPayload(WHITE_FEN, 1), 1),
    time_limit: 60,
    max_generation_positions: 60_000_000,
  }, { deadlineMs: performance.now() + 20_000 });
  const receipt = result.runtime_receipt.safe_root_reselector;
  assert.equal(world.terminalMateReceipts.length, 1);
  assert.equal(
    world.terminalMateReceipts[0].work_used,
    world.terminalMateReceipts[0].call_work_credit,
  );
  assert.equal(receipt.lane_max_work, SAFE_RESELECT_TOTAL_WORK);
  assert.equal(receipt.selected.status, "exhausted");
  assert.equal(world.safeReselectCreates.length, 1);
  assert(receipt.total_committed_work <= 60_000_000);
  client.close();
}

async function testSafeRootReselectAllUnsafeOrUnknownRetainsOriginalFailure() {
  const world = new MockWorld({
    foundAll: true,
    terminalMateStatus: "exhausted",
    wideSafetyStatuses: ["found", "unknown", "work_limit", "unsupported"],
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  let caught = null;
  try {
    await client.analyzeRoot({
      ...payload(boundaryPayload(WHITE_FEN, 1), 1),
      time_limit: 60,
      max_generation_positions: 60_000_000,
    }, { deadlineMs: performance.now() + 20_000 });
  } catch (error) {
    caught = error;
  }
  assert(caught);
  assert.equal(caught.code, "root-safety-widening-required");
  assert.equal(caught.safe_root_reselector_receipt.status, "no-safe-candidate");
  assert(caught.safe_root_reselector_receipt.scans.some((scan) => scan.status === "unknown"));
  assert(caught.safe_root_reselector_receipt.scans.some(
    (scan) => scan.status === "work_limit",
  ));
  assert(caught.safe_root_reselector_receipt.scans.some(
    (scan) => scan.status === "unsupported",
  ));
  assert.equal(caught.safe_root_reselector_receipt.ordinary_pool_recreated, false);
  assert.equal(
    caught.safe_root_reselector_receipt.ordinary_pool_restore_policy,
    "lazy-next-request",
  );
  assert.equal(world.live, 0);
  client.close();
}

async function testSafeRootReselectHonorsWorkAndDeadlineCaps() {
  {
    const world = new MockWorld({
      foundAll: true,
      terminalMateStatus: "exhausted",
      wideGenerationWork: SAFE_RESELECT_TOTAL_WORK,
    });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    let caught = null;
    try {
      await client.analyzeRoot({
        ...payload(boundaryPayload(WHITE_FEN, 1), 1),
        time_limit: 60,
        max_generation_positions: 60_000_000,
      }, { deadlineMs: performance.now() + 20_000 });
    } catch (error) {
      caught = error;
    }
    assert.equal(caught?.code, "root-safety-widening-required");
    assert.equal(caught.safe_root_reselector_receipt.status, "lane-work-limit");
    assert.equal(caught.safe_root_reselector_receipt.lane_work_used, SAFE_RESELECT_TOTAL_WORK);
    assert(caught.safe_root_reselector_receipt.total_committed_work <= 60_000_000);
    assert.equal(world.safeReselectSafetyReceipts.length, 0);
    client.close();
  }
  {
    const world = new MockWorld({
      foundAll: true,
      terminalMateStatus: "exhausted",
      wideSafetyStatuses: ["exhausted"],
      wideSafetyDelayMs: 500,
      searchDelayMs: 0,
    });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    let caught = null;
    try {
      await client.analyzeRoot({
        ...payload(boundaryPayload(WHITE_FEN, 1), 1),
        max_generation_positions: 60_000_000,
      }, { deadlineMs: performance.now() + 250 });
    } catch (error) {
      caught = error;
    }
    assert.equal(caught?.code, "root-safety-widening-required");
    assert.equal(caught.safe_root_reselector_receipt.status, "deadline");
    client.close();
  }
}

async function testSafeRootReselectRechecksDeadlineBeforePublication() {
  const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "performance");
  let fakeNow = 0;
  Object.defineProperty(globalThis, "performance", {
    configurable: true,
    value: { timeOrigin: 10_000, now: () => fakeNow },
  });
  const world = new MockWorld({
    foundAll: true,
    terminalMateStatus: "exhausted",
    wideSafetyStatuses: ["exhausted"],
    wideSafetyClockAdvance: () => { fakeNow = 101; },
    searchDelayMs: 0,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  try {
    await preflight(client);
    let caught = null;
    try {
      await client.analyzeRoot({
        ...payload(boundaryPayload(WHITE_FEN, 1), 1),
        time_limit: 60,
        max_generation_positions: 60_000_000,
      }, { deadlineMs: 100 });
    } catch (error) {
      caught = error;
    }
    assert.equal(caught?.code, "root-safety-widening-required");
    assert.equal(caught.safe_root_reselector_receipt.status, "deadline");
    assert.equal(caught.safe_root_reselector_receipt.selected, null);
    assert.equal(world.safeReselectSafetyReceipts.length, 1);
  } finally {
    client.close();
    if (originalDescriptor) Object.defineProperty(globalThis, "performance", originalDescriptor);
    else delete globalThis.performance;
  }
}

async function testSafeRootReselectAcceptsZeroWorkWitnessAtLaneCap() {
  {
    const world = new MockWorld({
      foundAll: true,
      terminalMateStatus: "exhausted",
      wideGenerationWork: SAFE_RESELECT_TOTAL_WORK,
      wideTerminalMateIndex: 0,
    });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    const result = await client.analyzeRoot({
      ...payload(boundaryPayload(WHITE_FEN, 1), 1),
      time_limit: 60,
      max_generation_positions: 60_000_000,
    }, { deadlineMs: performance.now() + 20_000 });
    const receipt = result.runtime_receipt.safe_root_reselector;
    assert.equal(receipt.generation_work, SAFE_RESELECT_TOTAL_WORK);
    assert.equal(receipt.lane_work_used, SAFE_RESELECT_TOTAL_WORK);
    assert.equal(receipt.selected.status, "terminal");
    assert.equal(receipt.selected.order_index, 0);
    assert.equal(receipt.selected.call_work_credit, 0);
    assert.equal(receipt.selected.work_used, 0);
    assert.equal(world.safeReselectSafetyReceipts.length, 0);
    client.close();
  }
  {
    const world = new MockWorld({
      foundAll: true,
      terminalMateStatus: "exhausted",
      wideSafetyStatuses: ["found", "exhausted"],
    });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    const request = {
      ...payload(boundaryPayload(WHITE_FEN, 1), 1),
      time_limit: 60,
      max_generation_positions: 60_000_000,
    };
    const warmed = await client.analyzeRoot(request, {
      deadlineMs: performance.now() + 20_000,
    });
    assert.equal(warmed.runtime_receipt.safe_root_reselector.selected.order_index, 13);
    const safetyCallsBefore = world.safeReselectSafetyCalls;
    world.wideGenerationWork = SAFE_RESELECT_TOTAL_WORK;
    const result = await client.analyzeRoot(request, {
      deadlineMs: performance.now() + 20_000,
    });
    const receipt = result.runtime_receipt.safe_root_reselector;
    assert.equal(receipt.generation_work, SAFE_RESELECT_TOTAL_WORK);
    assert.equal(receipt.lane_work_used, SAFE_RESELECT_TOTAL_WORK);
    assert.equal(receipt.selected.status, "exhausted");
    assert.equal(receipt.selected.order_index, 13);
    assert.equal(receipt.selected.cache_hit, true);
    assert.equal(receipt.selected.call_work_credit, 0);
    assert.equal(receipt.selected.work_used, 0);
    assert.equal(world.safeReselectSafetyCalls, safetyCallsBefore);
    client.close();
  }
}

async function testSafeRootReselectWorkerTerminationCompletesCleanupAfterDestroyMiss() {
  const world = new MockWorld({
    foundAll: true,
    terminalMateStatus: "exhausted",
    wideSafetyStatuses: ["exhausted"],
    wideDestroyFails: true,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot({
    ...payload(boundaryPayload(WHITE_FEN, 1), 1),
    time_limit: 60,
    max_generation_positions: 60_000_000,
  }, { deadlineMs: performance.now() + 20_000 });
  const receipt = result.runtime_receipt.safe_root_reselector;
  assert.equal(result.completed_depth, 0);
  assert.equal(receipt.isolated_session_destroyed, false);
  assert.equal(receipt.isolated_worker_terminated, true);
  assert.equal(
    receipt.isolated_cleanup_status,
    "worker-terminated-after-session-destroy-miss",
  );
  assert.equal(receipt.isolated_destroy_error_code, "synthetic-isolated-destroy-miss");
  assert.equal(world.live, 0);
  client.close();
}

async function testSafeRootReselectTerminationFailureFailsClosedWithReceipt() {
  const world = new MockWorld({
    foundAll: true,
    terminalMateStatus: "exhausted",
    wideSafetyStatuses: ["exhausted"],
    wideDestroyFails: true,
    wideTerminateFails: true,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  let caught = null;
  try {
    await client.analyzeRoot({
      ...payload(boundaryPayload(WHITE_FEN, 1), 1),
      time_limit: 60,
      max_generation_positions: 60_000_000,
    }, { deadlineMs: performance.now() + 20_000 });
  } catch (error) {
    caught = error;
  }
  assert.equal(caught?.code, "browser-root-safe-reselector-termination-failed");
  const receipt = caught.safe_root_reselector_receipt;
  assert.equal(receipt.isolated_session_destroyed, false);
  assert.equal(receipt.isolated_worker_terminated, false);
  assert.equal(receipt.isolated_cleanup_status, "worker-termination-failed");
  assert.equal(
    receipt.isolated_worker_termination_error_code,
    "synthetic-isolated-termination-failure",
  );
  assert.equal(receipt.ordinary_pool_recreated, false);
  client.close();
}

async function testSafeRootReselectOrdinaryTerminationFailurePreventsIsolation() {
  const world = new MockWorld({
    foundAll: true,
    terminalMateStatus: "exhausted",
    wideSafetyStatuses: ["exhausted"],
    ordinaryTerminateFails: true,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  let caught = null;
  try {
    await client.analyzeRoot({
      ...payload(boundaryPayload(WHITE_FEN, 1), 1),
      time_limit: 60,
      max_generation_positions: 60_000_000,
    }, { deadlineMs: performance.now() + 20_000 });
  } catch (error) {
    caught = error;
  }
  assert.equal(caught?.code, "browser-root-safe-reselector-isolation-failed");
  assert.equal(caught.safe_root_reselector_receipt, undefined);
  assert.equal(caught.ordinary_pool_teardown_receipt.schema,
    "spc-root-pool-teardown-receipt-v1");
  assert.equal(caught.ordinary_pool_teardown_receipt.attempted_workers,
    GEOMETRY.desktop_workers);
  assert.equal(caught.ordinary_pool_teardown_receipt.terminated_workers, 0);
  assert.equal(caught.ordinary_pool_teardown_receipt.failures.length,
    GEOMETRY.desktop_workers);
  assert(caught.ordinary_pool_teardown_receipt.failures.every(
    (failure) => failure.error_code === "synthetic-ordinary-termination-failure",
  ));
  assert.equal(world.safeReselectCreates.length, 0);
  assert.equal(world.workers.some(
    (worker) => worker.name === "scottish-progressive-root-safe-reselector",
  ), false);
  assert.equal(world.live, GEOMETRY.desktop_workers);
  world.ordinaryTerminateFails = false;
  client.close();
  assert.equal(world.live, 0);
}

async function testSafeRootReselectPostEvidenceCrashFailsClosedWithReceipt() {
  const world = new MockWorld({
    foundAll: true,
    terminalMateStatus: "exhausted",
    wideSafetyStatuses: ["exhausted"],
    wideCrashAfterSafety: true,
    wideDestroyDelayMs: 50,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  let caught = null;
  try {
    await client.analyzeRoot({
      ...payload(boundaryPayload(WHITE_FEN, 1), 1),
      time_limit: 60,
      max_generation_positions: 60_000_000,
    }, { deadlineMs: performance.now() + 20_000 });
  } catch (error) {
    caught = error;
  }
  assert.equal(caught?.code, "browser-root-safe-reselector-worker-crashed");
  const receipt = caught.safe_root_reselector_receipt;
  assert.equal(receipt.selected.status, "exhausted");
  assert.equal(receipt.isolated_session_destroyed, false);
  assert.equal(receipt.isolated_worker_terminated, true);
  assert.equal(receipt.isolated_destroy_error_code, "browser-root-worker-crashed");
  assert.equal(receipt.ordinary_pool_recreated, false);
  client.close();
}

async function testSafeRootReselectAbortDuringCleanupNeverPublishes() {
  const controller = new AbortController();
  const world = new MockWorld({
    foundAll: true,
    terminalMateStatus: "exhausted",
    wideSafetyStatuses: ["exhausted"],
    wideDestroyDelayMs: 25,
    wideDestroyStarted: () => controller.abort(),
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  let caught = null;
  try {
    await client.analyzeRoot({
      ...payload(boundaryPayload(WHITE_FEN, 1), 1),
      time_limit: 60,
      max_generation_positions: 60_000_000,
    }, {
      signal: controller.signal,
      deadlineMs: performance.now() + 20_000,
    });
  } catch (error) {
    caught = error;
  }
  assert.equal(caught?.name, "AbortError");
  assert.equal(caught.safe_root_reselector_receipt.status, "selected");
  assert.equal(caught.safe_root_reselector_receipt.selected.status, "exhausted");
  assert.equal(caught.safe_root_reselector_receipt.isolated_worker_terminated, true);
  assert.equal(world.live, 0);
  client.close();
}

async function testSafeRootReselectRejectsTerminalMateBehindNonterminalCandidate() {
  const world = new MockWorld({
    foundAll: true,
    terminalMateStatus: "exhausted",
    wideSafetyStatuses: ["exhausted"],
    wideTerminalMateIndex: 1,
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  let caught = null;
  try {
    await client.analyzeRoot({
      ...payload(boundaryPayload(WHITE_FEN, 1), 1),
      time_limit: 60,
      max_generation_positions: 60_000_000,
    }, { deadlineMs: performance.now() + 20_000 });
  } catch (error) {
    caught = error;
  }
  assert.equal(caught?.code, "browser-root-safe-reselector-order-invalid");
  assert.equal(caught.safe_root_reselector_receipt.scans.length, 0);
  assert.equal(caught.safe_root_reselector_receipt.selected, null);
  assert.equal(world.safeReselectSafetyReceipts.length, 0);
  client.close();
}

async function testSafeRootReselectIdentityTamperFailsClosedAndOrdinarySuccessDoesNotActivate() {
  {
    const world = new MockWorld({
      foundAll: true,
      terminalMateStatus: "exhausted",
      wideSafetyStatuses: ["exhausted"],
      wideIdentityTamper: true,
    });
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    let caught = null;
    await assert.rejects(
      client.analyzeRoot({
        ...payload(boundaryPayload(WHITE_FEN, 1), 1),
        time_limit: 60,
        max_generation_positions: 60_000_000,
      }, { deadlineMs: performance.now() + 20_000 }),
      (error) => {
        caught = error;
        return error?.code === "browser-root-safe-reselector-result-invalid";
      },
    );
    assert.equal(caught.safe_root_reselector_receipt.schema,
      "spc-root-safe-reselector-receipt-v1");
    assert.equal(caught.safe_root_reselector_receipt.isolated_worker_terminated, true);
    assert.equal(caught.safe_root_reselector_receipt.ordinary_pool_recreated, false);
    assert.equal(
      caught.safe_root_reselector_receipt.ordinary_pool_restore_policy,
      "lazy-next-request",
    );
    client.close();
  }
  {
    const world = new MockWorld();
    const client = browserClientApi.createClient({
      workerUrl: "mock-worker.js",
      workerFactory: world.factory,
      navigatorValue: DESKTOP_NAVIGATOR,
    });
    await preflight(client);
    const result = await client.analyzeRoot(payload(boundaryPayload(WHITE_FEN, 1), 1), {
      deadlineMs: performance.now() + 20_000,
    });
    assert.equal(result.completed_depth, 1);
    assert.equal(world.safeReselectCreates.length, 0);
    assert.equal(result.runtime_receipt.safe_root_reselector, undefined);
    client.close();
  }
}

async function testNativePromotionMateDeferralRescuesExactRootMate() {
  const boundary = boundaryPayload(PROMOTION_MATE_FEN, 7);
  const world = new MockWorld({ promotionMateDeferral: true });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  const result = await client.analyzeRoot(payload(boundary, 1), {
    deadlineMs: performance.now() + 20_000,
  });
  assert.equal(result.publishable, true);
  assert.equal(result.completed_depth, 1);
  assert.equal(result.score, CONFIG.mate_score - 1);
  assert.equal(result.proof, "white");
  assert.deepEqual(result.proof_bounds, [1, 1]);
  assert.deepEqual(result.best_full_series, PROMOTION_MATE_MOVES);
  assert.deepEqual(result.principal_variation[0].moves, PROMOTION_MATE_MOVES);
  assert.equal(result.checked_prefix.outcome, "checkmate");
  assert.equal(result.checked_prefix.ended_by_check, true);
  assert.deepEqual(result.checked_prefix.next_state, exactState(PROMOTION_MATE_CHILD));
  assert.equal(result.stats.root_tasks, 0);
  assert.equal(result.stats.terminal_mate_rescues, 1);
  assert.equal(
    result.runtime_receipt.terminal_mate_rescue.trigger,
    "native-promotion-frontier-deferred",
  );
  assert.equal(result.runtime_receipt.terminal_mate_rescue.status, "found");
  assertPolicyReceiptSurfaces(result, []);
  assert.equal(world.searchDispatches.length, 0);
  assert.equal(world.safetyReceipts.length, 0);
  assert.equal(world.terminalMateReceipts.length, 1);
  assert.equal(
    world.terminalMateReceipts[0].call_work_credit,
    PLAY_LIMITS.default_generation_positions,
  );
  assert.equal(result.work, world.terminalMateReceipts[0].work_used);
  client.close();
}

async function testUnprovenPromotionMateDeferralFailsClosed() {
  const world = new MockWorld({
    promotionMateDeferral: true,
    terminalMateStatus: "unknown",
  });
  const client = browserClientApi.createClient({
    workerUrl: "mock-worker.js",
    workerFactory: world.factory,
    navigatorValue: DESKTOP_NAVIGATOR,
  });
  await preflight(client);
  await assert.rejects(
    client.analyzeRoot(payload(boundaryPayload(PROMOTION_MATE_FEN, 7), 1), {
      deadlineMs: performance.now() + 20_000,
    }),
    (error) => error?.code === "browser-root-promotion-mate-deferred",
  );
  assert.equal(client.rootRunner.lastSafe, null);
  assert.equal(world.searchDispatches.length, 0);
  assert.equal(world.safetyReceipts.length, 0);
  assert.equal(world.terminalMateReceipts.length, 1);
  assert.equal(
    world.terminalMateReceipts[0].call_work_credit,
    PLAY_LIMITS.default_generation_positions,
  );
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
  const clockFields = child.fen.split(" ");
  clockFields[4] = String(Number(clockFields[4]) + 1);
  clockFields[5] = String(Number(clockFields[5]) + 1);
  const changedClockFen = clockFields.join(" ");
  assert.notEqual(rootClientApi.mateProofCacheKey(IDENTITY, {
    ...child,
    fen: changedClockFen,
    board_fen: changedClockFen,
  }), key, "EXHAUSTED evidence must stay bound to both FEN clocks");
  assert.notEqual(rootClientApi.mateProofCacheKey(IDENTITY, {
    ...child,
    promoted_hex: "0000000000000001",
  }), key, "EXHAUSTED evidence must stay bound to exact promotion provenance");
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

function testSafeRootReselectAdapterConfigDerivation() {
  const identity = {
    root_geometry: { session_config: CONFIG },
    root_session_contract: {
      hard_limits: { minimum_width: 1, maximum_width: SAFE_RESELECT_WIDTH },
    },
  };
  const derived = adapterApi.deriveSafeRootReselectSessionConfig(identity);
  assert.equal(derived.width, SAFE_RESELECT_WIDTH);
  assert.equal(CONFIG.width, 32, "the certified public configuration must remain unchanged");
  assert.notEqual(derived, CONFIG);
  assert.notEqual(derived.weights, CONFIG.weights);
  assert(Object.isFrozen(derived));
  assert(Object.isFrozen(derived.weights));
  assert.throws(
    () => adapterApi.deriveSafeRootReselectSessionConfig({
      ...identity,
      root_session_contract: {
        hard_limits: { minimum_width: 1, maximum_width: 511 },
      },
    }),
    (error) => error?.code === "browser-root-safe-reselector-config-invalid",
  );
  assert.throws(
    () => adapterApi.deriveSafeRootReselectSessionConfig({
      ...identity,
      root_geometry: { session_config: { ...CONFIG, width: 64 } },
    }),
    (error) => error?.code === "browser-root-safe-reselector-config-invalid",
  );
}

async function testAppRouteFailsClosedAfterSafeRootReselectError() {
  const safeError = new Error("synthetic safe-root reselector failure");
  safeError.code = "browser-root-safe-reselector-result-invalid";
  safeError.fallbackRequired = true;
  safeError.safe_root_reselector_receipt = Object.freeze({ schema: "synthetic" });
  assert.equal(browserClientApi.isSafeRootReselectFailure(safeError), true);
  assert.equal(browserClientApi.isSafeRootReselectFailure({
    code: "ordinary-local-analysis-failed",
    fallbackRequired: true,
  }), false);
  let monolithicCalls = 0;
  let remoteCalls = 0;
  const route = async () => {
    try {
      throw safeError;
    } catch (error) {
      if (browserClientApi.isSafeRootReselectFailure(error)) throw error;
      if (error?.fallbackRequired !== true) throw error;
    }
    monolithicCalls += 1;
    remoteCalls += 1;
  };
  await assert.rejects(route(), (error) => error === safeError);
  assert.equal(monolithicCalls, 0);
  assert.equal(remoteCalls, 0);
  const rootRouteStart = appSource.indexOf(
    "return await browserEngineClient.analyzeRoot(analysisBody",
  );
  const safetyGuard = appSource.indexOf(
    "if (BROWSER_ENGINE_API.isSafeRootReselectFailure(error)) throw error;",
    rootRouteStart,
  );
  const ordinaryFallbackGate = appSource.indexOf(
    "if (error?.fallbackRequired !== true) throw error;",
    rootRouteStart,
  );
  const monolithicRoute = appSource.indexOf(
    "return await browserEngineClient.analyze(analysisBody",
    rootRouteStart,
  );
  assert(rootRouteStart >= 0);
  assert(safetyGuard > rootRouteStart);
  assert(ordinaryFallbackGate > safetyGuard);
  assert(monolithicRoute > ordinaryFallbackGate);
}

testSafeRootReselectAdapterConfigDerivation();
await testAppRouteFailsClosedAfterSafeRootReselectError();
testAspirationAggregateAndAffinityContract();
await testPersistentPoolTwoTurns();
await testWhiteAndBlackMateMapping();
await testUnprovedMateClaimQuarantinePublishesSafeRoot();
await testCheckedPvHorizonMateRejectsTheProvisionalWinner();
await testInternalOpponentBoundaryMateIsProbedLeafFirst();
await testBlackRootInternalOpponentBoundaryMateUsesExactParity();
await testCachedInternalMateShortCircuitsUncachedDeeperBoundary();
await testTerminalPvLeafStillChecksEarlierOpponentBoundary();
await testMalformedInternalRootedProofsFailClosed();
await testUnknownInternalOpponentBoundaryFailsClosed();
await testSecondDistinctCheckedPvMatePublishesExactPolicyVeto();
await testCheckedPvHorizonIsRootedAndFailClosed();
await testFavorableCheckedHorizonIsNotVetoed();
await testProactiveS5TerminalMateWinsBeforeOrdinarySearch();
await testUnprovenProactiveTerminalMateContinuesOrdinarySearch();
await testProactiveProbeTimeoutRecoversOrdinarySearch();
await testCrashReturnsLastSafeAndReprobes();
await testNestedDeadlineAndExactWorkLimitClassification();
await testMateProofCacheAcrossFiveDepthsAndBoundaries();
await testUnknownMateProofNeverCaches();
await testUnknownBeforeD1PublishesNoMoveForEitherColor();
await testUnknownAfterD1ReturnsOnlyCertifiedLastSafe();
await testUnknownCheckedPvHorizonCannotFallThroughToRootChild();
await testCheckedPvHorizonWithoutProbeCreditFailsClosed();
await testImmediateMatePublishesWithBoundCoverage();
await testAllMatingFrontierRescuesTerminalRootMate();
await testUnprovenTerminalMateRescueFailsClosed();
await testSafeRootReselectSkipsFoundAndUnknownThenPublishesExhausted();
await testSafeRootReselectReachesRank62WithStagedCredits();
await testSafeRootReselectNeverResurrectsRejectedRoots();
await testSafeRootReselectRemapsPreferredOrderExclusionByExactChild();
await testTerminalProbeCannotStarveSafeRootReselectLane();
await testSafeRootReselectAllUnsafeOrUnknownRetainsOriginalFailure();
await testSafeRootReselectHonorsWorkAndDeadlineCaps();
await testSafeRootReselectRechecksDeadlineBeforePublication();
await testSafeRootReselectAcceptsZeroWorkWitnessAtLaneCap();
await testSafeRootReselectWorkerTerminationCompletesCleanupAfterDestroyMiss();
await testSafeRootReselectTerminationFailureFailsClosedWithReceipt();
await testSafeRootReselectOrdinaryTerminationFailurePreventsIsolation();
await testSafeRootReselectPostEvidenceCrashFailsClosedWithReceipt();
await testSafeRootReselectAbortDuringCleanupNeverPublishes();
await testSafeRootReselectRejectsTerminalMateBehindNonterminalCandidate();
await testSafeRootReselectIdentityTamperFailsClosedAndOrdinarySuccessDoesNotActivate();
await testNativePromotionMateDeferralRescuesExactRootMate();
await testUnprovenPromotionMateDeferralFailsClosed();
await testMateCacheIdentityAndBoundaryBinding();
await testCanonicalRootPolicySelection();
await testCanonicalRootPolicyDriftFailsClosed();
await testMismatchedWorkerTimeOriginClampsDeadline();
testGeometry();

process.stdout.write(JSON.stringify({
  schema: "spc-browser-root-iteration-mock-receipt-v1",
  desktop_initial_full_wave: "8-of-8",
  all_initial_wave_aspiration: true,
  aggregate_aspiration_accounting: true,
  exact_owner_affinity: true,
  exact_owner_priority_stable: true,
  unavailable_claimed_owner_fails_closed: true,
  persistent_worker_pool: true,
  fresh_sessions_per_turn: true,
  pooled_native_prefix: true,
  preflight_heap_released: true,
  white_black_mate_mapping: true,
  unproved_mate_claim_quarantine_white_black: true,
  checked_pv_horizon_mate_rejected: true,
  internal_opponent_boundary_mate_leaf_first: true,
  black_root_internal_boundary_mate_parity: true,
  cached_internal_found_short_circuits_uncached_deeper_boundary_white_black: true,
  terminal_pv_leaf_preserves_earlier_boundary: true,
  malformed_internal_rooted_proofs_fail_closed: true,
  unknown_internal_opponent_boundary_fails_closed: true,
  checked_pv_horizon_native_repaired: true,
  checked_pv_second_distinct_mate_policy_veto: true,
  checked_pv_horizon_dedicated_schema: true,
  checked_pv_horizon_root_chain_fail_closed: true,
  stale_horizon_safety_reply_fail_closed: true,
  favorable_checked_horizon_not_vetoed: true,
  proactive_s5_terminal_mate_before_ordinary_search: true,
  unproven_proactive_terminal_mate_continues_ordinary_search: true,
  proactive_probe_timeout_recovers_ordinary_search: true,
  pruned_bounds_publish: true,
  immediate_mate_with_alternatives: true,
  all_mating_frontier_terminal_mate_rescue: true,
  unproven_terminal_mate_rescue_fails_closed: true,
  native_promotion_mate_deferral_terminal_mate_rescue: true,
  unproven_promotion_mate_deferral_fails_closed: true,
  incomplete_bound_coverage_fails_closed: true,
  complete_mate_proof_cache: true,
  unknown_mate_proof_not_cached: true,
  unknown_before_d1_has_no_move_white_black: true,
  unknown_after_d1_preserves_only_certified_last_safe: true,
  unknown_checked_pv_horizon_fails_closed: true,
  unprobed_checked_pv_horizon_fails_closed: true,
  mate_cache_identity_boundary_bound: true,
  crash_last_safe_and_reprobe: true,
  absolute_deadline_epoch_transport: true,
  canonical_root_policy_drift_fails_closed: true,
  canonical_root_policy_selects_late_and_promotion_boundaries: true,
  mismatched_worker_time_origin_clamped: true,
  unknown_memory_uses_lower_geometry: true,
}));
