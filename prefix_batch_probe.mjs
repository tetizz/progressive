import process from "node:process";
import createKernelModule from "./build/native-subtree-wasm/spc-start-kernel.js";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const cases = JSON.parse(input);
if (!Array.isArray(cases)) throw new Error("stdin must be a JSON case array");

const module = await createKernelModule();
const results = [];
for (const item of cases) {
  const strings = [
    item.fen,
    item.progressive_ep || "-",
    item.promoted_hex || "-",
    (item.prefix || []).join("/"),
  ];
  const pointers = strings.map((value) => module.stringToNewUTF8(value));
  try {
    const resultPointer = module._spc_boundary_prefix_json(
      pointers[0],
      item.series,
      item.quiet_series || 0,
      pointers[1],
      pointers[2],
      pointers[3],
    );
    results.push(JSON.parse(module.UTF8ToString(resultPointer)));
  } finally {
    for (const pointer of pointers) module._free(pointer);
  }
}
process.stdout.write(`${JSON.stringify(results)}\n`);
