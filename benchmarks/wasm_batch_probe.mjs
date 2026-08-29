import process from "node:process";
import { pathToFileURL } from "node:url";


const [mode, modulePath] = process.argv.slice(2);
if (!new Set(["prefix", "mate", "ladder"]).has(mode) || !modulePath) {
  throw new Error(
    "usage: node wasm_batch_probe.mjs <prefix|mate|ladder> <combined-module.mjs>",
  );
}

let input = "";
for await (const chunk of process.stdin) input += chunk;
const cases = JSON.parse(input);
if (!Array.isArray(cases)) throw new Error("stdin must be a JSON case array");

const createModule = (await import(pathToFileURL(modulePath).href)).default;
const module = await createModule();
const results = [];

for (const item of cases) {
  if (mode === "prefix") {
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
        item.quiet_series ?? 0,
        pointers[1],
        pointers[2],
        pointers[3],
      );
      results.push(JSON.parse(module.UTF8ToString(resultPointer)));
    } finally {
      for (const pointer of pointers) module._free(pointer);
    }
    continue;
  }

  const strings = [
    item.fen,
    item.progressive_ep || "-",
    item.promoted_hex || "-",
  ];
  const pointers = strings.map((value) => module.stringToNewUTF8(value));
  try {
    const resultPointer = mode === "ladder"
      ? module._spc_single_reply_mate_ladder_search_json(
        pointers[0],
        item.series,
        pointers[1],
        pointers[2],
        item.max_work ?? 0,
        item.time_limit_ms ?? 0,
      )
      : module._spc_series_mate_search_json(
        pointers[0],
        item.series,
        pointers[1],
        pointers[2],
        item.max_positions ?? 0,
        item.max_work ?? 0,
        item.time_limit_ms ?? 0,
      );
    results.push(JSON.parse(module.UTF8ToString(resultPointer)));
  } finally {
    for (const pointer of pointers) module._free(pointer);
  }
}

process.stdout.write(`${JSON.stringify(results)}\n`);
