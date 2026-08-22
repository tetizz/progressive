import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";


const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = new Map();
for (let index = 2; index < process.argv.length; index += 2) {
  const key = process.argv[index];
  const value = process.argv[index + 1];
  if (!key?.startsWith("--") || value === undefined) {
    throw new Error(`invalid argument near ${String(key)}`);
  }
  args.set(key, value);
}
if (!args.has("--build-receipt") || !args.has("--output")) {
  throw new Error("--build-receipt and --output are required");
}
const require = createRequire(import.meta.url);
const api = require(path.join(root, "browser-prefix-contract.js"));

const startFen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const contract = {
  schema: api.CONTRACT_SCHEMA,
  result_schema: api.RESULT_SCHEMA,
  abi_version: 1,
  chess960: false,
  promoted_hex_required_for_product: true,
  limits: { ...api.HARD_LIMITS },
};
const identity = {
  source_fingerprint: "a".repeat(16),
  wasm_sha256: "b".repeat(64),
  module_js_sha256: "c".repeat(64),
  certificate_id: "prefix-parity-gate-1",
  engine_version: "spc-test-v1",
  ruleset_version: "progressive-test-v1",
  prefix_contract: contract,
};
const payload = {
  fen: startFen,
  series: 1,
  quiet_series: 0,
  ep_targets: [],
  progressive_ep: [],
  promoted_hex: "0",
  chess960: false,
  prefix: [],
};

const request = api.normalizePrefixRequest(payload, "prefix-1", contract);
assert.equal(request.boundary.promoted_hex, "0000000000000000");
assert.equal(request.boundary.chess960, false);
assert.deepEqual(request.boundary.ep_targets, []);

const result = {
  schema: api.RESULT_SCHEMA,
  abi_version: 1,
  ok: true,
  status: "complete",
  request_id: request.request_id,
  source_fingerprint: identity.source_fingerprint,
  wasm_sha256: identity.wasm_sha256,
  module_js_sha256: identity.module_js_sha256,
  certificate_id: identity.certificate_id,
  engine_version: identity.engine_version,
  ruleset_version: identity.ruleset_version,
  boundary_state: {
    fen: startFen,
    board_fen: startFen,
    series: 1,
    series_number: 1,
    side_to_move: "white",
    quiet_series: 0,
    quiet_draw_pending: false,
    ep_targets: [],
    progressive_ep: [],
    promoted_hex: "0x0",
    chess960: false,
  },
  fen: startFen,
  board_fen: startFen,
  prefix: [],
  current_prefix: [],
  san: [],
  frames: [],
  remaining: 1,
  moves_remaining: 1,
  complete: false,
  completion_reason: null,
  check: false,
  ended_by_check: false,
  in_check: false,
  outcome: null,
  unused_moves: 0,
  legal_next: [{ uci: "e2e4", san: "e4" }],
  legal_moves: [{ uci: "e2e4", san: "e4" }],
  next_state: null,
};
assert.equal(api.validatePrefixResult(result, request, identity), result);

const completeRequest = api.normalizePrefixRequest(
  { ...payload, prefix: ["e2e4"] },
  "prefix-complete-1",
  contract,
);
const afterE4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1";
const frameAfterE4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1";
const completeResult = {
  ...result,
  request_id: completeRequest.request_id,
  fen: afterE4,
  board_fen: afterE4,
  prefix: ["e2e4"],
  current_prefix: ["e2e4"],
  san: ["e4"],
  frames: [{ index: 1, uci: "e2e4", san: "e4", board_fen: frameAfterE4 }],
  remaining: 0,
  moves_remaining: 0,
  complete: true,
  completion_reason: "budget",
  legal_next: [],
  legal_moves: [],
  next_state: {
    fen: afterE4,
    board_fen: afterE4,
    series: 2,
    series_number: 2,
    side_to_move: "black",
    quiet_series: 0,
    quiet_draw_pending: false,
    ep_targets: [],
    progressive_ep: [],
    promoted_hex: "0000000000000000",
    chess960: false,
  },
};
assert.equal(
  api.validatePrefixResult(completeResult, completeRequest, identity),
  completeResult,
);

const stalemateFen = "7k/8/8/8/8/6q1/8/7K w - - 0 1";
const stuckRequest = api.normalizePrefixRequest(
  { ...payload, fen: stalemateFen },
  "prefix-stuck-1",
  contract,
);
const stuckBoundary = {
  fen: stalemateFen,
  board_fen: stalemateFen,
  series: 1,
  series_number: 1,
  side_to_move: "white",
  quiet_series: 0,
  quiet_draw_pending: false,
  ep_targets: [],
  progressive_ep: [],
  promoted_hex: "0000000000000000",
  chess960: false,
};
const stuckResult = {
  ...result,
  request_id: stuckRequest.request_id,
  boundary_state: stuckBoundary,
  fen: stalemateFen,
  board_fen: stalemateFen,
  remaining: 1,
  moves_remaining: 1,
  complete: true,
  completion_reason: "stalemate",
  outcome: "stalemate",
  unused_moves: 1,
  legal_next: [],
  legal_moves: [],
  next_state: { ...stuckBoundary },
};
assert.equal(api.validatePrefixResult(stuckResult, stuckRequest, identity), stuckResult);

function expectCode(callback, code) {
  assert.throws(callback, (error) => error?.code === code);
}

expectCode(
  () => api.normalizePrefixRequest({ ...payload, promoted_hex: null }, "missing", contract),
  "browser-prefix-request-unsupported",
);
expectCode(
  () => api.normalizePrefixRequest({ ...payload, chess960: true }, "960", contract),
  "browser-prefix-request-unsupported",
);
expectCode(
  () => api.normalizePrefixRequest({
    ...payload,
    ep_targets: ["a3"],
    progressive_ep: ["b3"],
  }, "ep-drift", contract),
  "browser-prefix-request-unsupported",
);
expectCode(
  () => api.normalizePrefixRequest({ ...payload, series: 257 }, "over-series", contract),
  "browser-prefix-request-unsupported",
);
expectCode(
  () => api.validatePrefixResult(
    { ...result, source_fingerprint: "d".repeat(16) },
    request,
    identity,
  ),
  "browser-prefix-result-invalid",
);
expectCode(
  () => api.validatePrefixResult({
    ...completeResult,
    next_state: { ...completeResult.next_state, series: 1, series_number: 1 },
  }, completeRequest, identity),
  "browser-prefix-result-invalid",
);
expectCode(
  () => api.validatePrefixResult({
    ...completeResult,
    frames: [{ ...completeResult.frames[0], board_fen: startFen }],
  }, completeRequest, identity),
  "browser-prefix-result-invalid",
);
expectCode(
  () => api.validatePrefixResult(
    { ...result, boundary_state: { ...result.boundary_state, chess960: true } },
    request,
    identity,
  ),
  "browser-prefix-result-invalid",
);

let remoteCalls = 0;
const localSuccess = {
  identity,
  canInspectPrefix: () => true,
  inspectPrefix: async () => result,
};
assert.equal(await api.routePrefixRequest({
  payload,
  localClient: localSuccess,
  remote: {
    identity,
    request: async () => { remoteCalls += 1; return { remote: true }; },
  },
}), result);
assert.equal(remoteCalls, 0);

const originalPayload = { ...payload, promoted_hex: null };
const localUnavailable = {
  identity,
  canInspectPrefix: () => false,
  inspectPrefix: async () => { throw new Error("must not run"); },
};
const remoteResult = await api.routePrefixRequest({
  payload: originalPayload,
  localClient: localUnavailable,
  remote: {
    identity,
    request: async (received) => {
      remoteCalls += 1;
      assert.equal(received, originalPayload);
      return { remote: true, ...identity };
    },
  },
});
assert.deepEqual(remoteResult, { remote: true, ...identity });

const localMalformed = {
  identity,
  canInspectPrefix: () => true,
  inspectPrefix: async () => {
    throw new api.PrefixContractError(
      "malformed local result",
      "browser-prefix-result-invalid",
    );
  },
};
assert.deepEqual(await api.routePrefixRequest({
  payload,
  localClient: localMalformed,
  remote: {
    identity,
    request: async (received) => {
      remoteCalls += 1;
      assert.equal(received, payload);
      return { remote: "authoritative", ...identity };
    },
  },
}), { remote: "authoritative", ...identity });

await assert.rejects(api.routePrefixRequest({
  payload,
  localClient: localMalformed,
  remote: {
    identity: { ...identity, ruleset_version: "different-rules" },
    request: async () => { throw new Error("must not call mismatched authority"); },
  },
}), (error) => error?.code === "browser-prefix-authority-mismatch");

const controller = new AbortController();
let cancellationFallback = false;
const localCancelled = {
  identity,
  canInspectPrefix: () => true,
  inspectPrefix: (_body, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new api.PrefixContractError(
      "worker restarted",
      "browser-worker-restarted",
    )), { once: true });
  }),
};
const cancelled = api.routePrefixRequest({
  payload,
  signal: controller.signal,
  localClient: localCancelled,
  remote: {
    identity,
    request: async () => { cancellationFallback = true; return {}; },
  },
});
controller.abort();
await assert.rejects(cancelled, (error) => error?.name === "AbortError");
assert.equal(cancellationFallback, false);

const nonFallback = new api.PrefixContractError(
  "terminal local policy error",
  "browser-prefix-terminal",
  { fallbackRequired: false },
);
await assert.rejects(api.routePrefixRequest({
  payload,
  localClient: {
    identity,
    canInspectPrefix: () => true,
    inspectPrefix: async () => { throw nonFallback; },
  },
  remote: {
    identity,
    request: async () => { throw new Error("must not fall back"); },
  },
}), (error) => error === nonFallback);

const buildReceipt = JSON.parse(await readFile(args.get("--build-receipt"), "utf8"));
const artifact = Object.fromEntries([
  "source_revision",
  "source_fingerprint",
  "kernel_sha256",
  "wasm_sha256",
  "module_js_sha256",
  "artifact_set_sha256",
].map((key) => [key, buildReceipt[key]]));
const receipt = {
  schema: "spc-browser-prefix-contract-receipt-v1",
  status: "passed",
  artifact,
  exact_identity: true,
  promoted_hex: request.boundary.promoted_hex,
  chess960_rejected: true,
  certified_limits_enforced: true,
  full_next_state_enforced: true,
  same_series_terminal_covered: true,
  final_frame_consistency_enforced: true,
  malformed_local_fallback: true,
  original_request_preserved: true,
  remote_authority_bound: true,
  cancellation_fallback_suppressed: true,
  remote_calls: remoteCalls,
};
await mkdir(path.dirname(args.get("--output")), { recursive: true });
await writeFile(args.get("--output"), `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
process.stdout.write(`${JSON.stringify(receipt)}\n`);
