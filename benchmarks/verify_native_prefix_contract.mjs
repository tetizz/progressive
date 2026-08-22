import assert from "node:assert/strict";
import createKernelModule from "../build/native-subtree-wasm/spc-start-kernel.js";


const module = await createKernelModule();
assert.equal(typeof module._spc_boundary_prefix_contract_json, "function");
const nativeContract = JSON.parse(module.UTF8ToString(
  module._spc_boundary_prefix_contract_json(),
));
assert.deepEqual(nativeContract, {
  schema: "spc-boundary-prefix-contract-v1",
  abi_version: 1,
  result_schema: "spc-boundary-prefix-v1",
  chess960: false,
  promoted_hex_required_for_product: true,
  hard_limits: {
    maximum_fen_utf8_bytes: 512,
    maximum_series_number: 256,
    maximum_quiet_series: 1_000_000,
    maximum_ep_targets: 8,
    maximum_ep_utf8_bytes: 23,
    maximum_prefix_moves: 256,
    maximum_prefix_utf8_bytes: 1_535,
    maximum_uci_move_bytes: 5,
    maximum_promoted_hex_bytes: 18,
  },
});

function inspect({ fen, series, quiet = 0, ep = "-", promoted = "0", prefix = "" }) {
  const pointers = [fen, ep, promoted, prefix].map((value) => module.stringToNewUTF8(value));
  try {
    return JSON.parse(module.UTF8ToString(module._spc_boundary_prefix_json(
      pointers[0], series, quiet, pointers[1], pointers[2], pointers[3],
    )));
  } finally {
    pointers.forEach((pointer) => module._free(pointer));
  }
}

const start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const accepted = inspect({ fen: start, series: 1 });
assert.equal(accepted.ok, true);
assert.equal(accepted.boundary_state.chess960, false);
assert.equal(accepted.boundary_state.promoted_hex, "0000000000000000");

const promoted = inspect({
  fen: "7k/8/8/8/8/8/Q7/7K w - - 0 1",
  series: 1,
  promoted: "100",
});
assert.equal(promoted.ok, true);
assert.equal(promoted.boundary_state.promoted_hex, "0000000000000100");
assert.equal(promoted.boundary_state.chess960, false);

const oversizedFen = inspect({ fen: "x".repeat(513), series: 1 });
assert.equal(oversizedFen.ok, false);
assert.equal(oversizedFen.error_code, "invalid-boundary");

const oversizedQuiet = inspect({ fen: start, series: 1, quiet: 1_000_001 });
assert.equal(oversizedQuiet.ok, false);
assert.equal(oversizedQuiet.error_code, "invalid-boundary");

const oversizedEp = inspect({
  fen: start,
  series: 1,
  ep: "a3,b3,c3,d3,e3,f3,g3,h3,a6",
});
assert.equal(oversizedEp.ok, false);
assert.equal(oversizedEp.error_code, "invalid-boundary");

const oversizedPrefix = inspect({
  fen: start,
  series: 255,
  prefix: Array(257).fill("e2e4").join("/"),
});
assert.equal(oversizedPrefix.ok, false);
assert.equal(oversizedPrefix.error_code, "invalid-move");

process.stdout.write(`${JSON.stringify({
  schema: "spc-native-prefix-contract-receipt-v1",
  native_contract: nativeContract,
  exact_chess960_identity: true,
  exact_promoted_identity: "0000000000000100",
  fen_limit_rejected: true,
  quiet_limit_rejected: true,
  ep_limit_rejected: true,
  prefix_limit_rejected: true,
})}\n`);
