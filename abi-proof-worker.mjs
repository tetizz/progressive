import createKernelModule from "./build/native-subtree-wasm/spc-start-kernel.js";
import createMateModule from "./build/mate-wasm/spc-series-mate.js";

function withStrings(module, values, callback) {
  const pointers = values.map((value) => module.stringToNewUTF8(value));
  try {
    return callback(pointers);
  } finally {
    for (const pointer of pointers) module._free(pointer);
  }
}

const started = performance.now();
const [kernel, mate] = await Promise.all([
  createKernelModule(),
  createMateModule(),
]);
const loaded = performance.now();

const prefix = withStrings(
  kernel,
  [
    "8/8/8/8/1p5p/7b/2PB1KPk/7b w - - 0 1",
    "-",
    "-",
    "g2g4/c2c4/d2f4",
  ],
  (pointers) => JSON.parse(kernel.UTF8ToString(
    kernel._spc_boundary_prefix_json(
      pointers[0],
      3,
      0,
      pointers[1],
      pointers[2],
      pointers[3],
    ),
  )),
);

const mateProof = withStrings(
  mate,
  [
    "rn1q1bnr/ppp1pkpp/5p2/8/3Pp3/2NB4/PPP2PPP/R1BbK1NR w KQ - 0 7",
    "-",
    "-",
  ],
  (pointers) => JSON.parse(mate.UTF8ToString(
    mate._spc_series_mate_search_json(
      pointers[0],
      5,
      pointers[1],
      pointers[2],
      0,
      0,
      0,
    ),
  )),
);
const ended = performance.now();

self.postMessage({
  schema: "spc-opera-abi-smoke-v1",
  load_ms: loaded - started,
  total_ms: ended - started,
  prefix: {
    san: prefix.san,
    completion_reason: prefix.completion_reason,
    outcome: prefix.outcome,
    ep_targets: prefix.next_state?.ep_targets,
  },
  mate: {
    proof_status: mateProof.proof_status,
    complete: mateProof.complete,
    moves: mateProof.moves,
    stats: mateProof.stats,
  },
});
