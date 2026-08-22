import process from "node:process";
import createMateModule from "./build/mate-wasm/spc-series-mate.js";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const cases = JSON.parse(input);
if (!Array.isArray(cases)) throw new Error("stdin must be a JSON case array");

const module = await createMateModule();
const results = [];
for (const item of cases) {
  const strings = [
    item.fen,
    item.progressive_ep || "-",
    item.promoted_hex || "-",
  ];
  const pointers = strings.map((value) => module.stringToNewUTF8(value));
  try {
    const resultPointer = module._spc_series_mate_search_json(
      pointers[0],
      item.series,
      pointers[1],
      pointers[2],
      item.max_positions || 0,
      item.max_work || 0,
      item.time_limit_ms || 0,
    );
    results.push(JSON.parse(module.UTF8ToString(resultPointer)));
  } finally {
    for (const pointer of pointers) module._free(pointer);
  }
}
process.stdout.write(`${JSON.stringify(results)}\n`);
