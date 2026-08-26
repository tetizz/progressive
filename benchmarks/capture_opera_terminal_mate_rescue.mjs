import { writeFile } from "node:fs/promises";


function argumentsOf(argv) {
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    if (!argv[index]?.startsWith("--") || argv[index + 1] === undefined) {
      throw new Error(`invalid argument near ${String(argv[index])}`);
    }
    values.set(argv[index], argv[index + 1]);
  }
  for (const required of ["--endpoint", "--url", "--output"]) {
    if (!values.has(required)) throw new Error(`missing ${required}`);
  }
  return {
    endpoint: values.get("--endpoint").replace(/\/$/, ""),
    url: values.get("--url"),
    output: values.get("--output"),
    timeoutMs: Number(values.get("--timeout-ms") ?? 90_000),
  };
}


async function connect(url) {
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  let nextId = 1;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(String(event.data));
    if (!message.id || !pending.has(message.id)) return;
    const entry = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) entry.reject(new Error(JSON.stringify(message.error)));
    else entry.resolve(message.result);
  });
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
  return { socket, call };
}


const probeExpression = String.raw`(async () => {
  const manifest = await fetch(
    new URL("./engine/browser-engine-manifest.json?terminal-mate-rescue", location.href),
    { cache: "no-store" },
  ).then((response) => {
    if (!response.ok) throw new Error("browser engine manifest fetch failed");
    return response.json();
  });
  const api = globalThis.ScottishProgressiveBrowserEngine;
  if (!api?.createClient) throw new Error("browser engine client did not load");
  const client = api.createClient({
    workerUrl: new URL(
      "./browser-engine-worker.js?terminal-mate-rescue",
      location.href,
    ).href,
  });
  const started = performance.now();
  try {
    const preflight = await client.preflight({
      sourceFingerprint: manifest.source_fingerprint,
      deadlineMs: started + 20_000,
    });
    if (preflight.ready !== true) {
      throw new Error("browser preflight failed: " + (preflight.reason || "unknown"));
    }
    const payload = {
      fen: "rnk3nr/pp3ppp/8/8/8/1Pp1P3/P1PP1PPP/R1b1K1NR w K - 0 13",
      series: 7,
      quiet_series: 0,
      ep_targets: [],
      progressive_ep: [],
      promoted_hex: "0000000000000000",
      chess960: false,
      prefix: [],
      depth: 5,
      max_series: 32,
      time_limit: 60,
      max_generation_positions: 4_000_000_000,
      alternatives: 0,
      best_move_only: true,
      rate_move: false,
      save: false,
    };
    let result;
    try {
      result = await client.analyzeRoot(payload, {
        deadlineMs: started + 60_000,
        receiptDeadlineMs: started + 65_000,
      });
    } catch (error) {
      return {
        status: "failed",
        error: {
          name: String(error?.name || "Error"),
          code: String(error?.code || ""),
          message: String(error?.message || error),
        },
      };
    }
    const elapsedSeconds = (performance.now() - started) / 1_000;
    const rescue = result.runtime_receipt?.terminal_mate_rescue;
    const checks = {
      publishable: result.publishable === true,
      compiled_replay: result.checked_prefix?.outcome === "checkmate"
        && result.checked_prefix?.ended_by_check === true,
      terminal_score: result.score === 999_999,
      white_proof: result.proof === "white"
        && JSON.stringify(result.proof_bounds) === "[1,1]",
      rescue_triggered: result.stats?.terminal_mate_rescues === 1
        && rescue?.trigger === "native-promotion-frontier-deferred"
        && rescue?.status === "found",
      staged_root_line: Array.isArray(result.best_full_series)
        && result.best_full_series.join("/")
          === "d2c3/e1e2/g1f3/f3g5/h1d1/g5e6/d1d8",
      exact_accelerated_work: Number(rescue?.work_used) === 45_694,
      total_work_matches_rescue: Number(result.work) === Number(rescue?.work_used),
      sub_ten_seconds: elapsedSeconds < 10,
      within_global_work: Number(result.work) <= payload.max_generation_positions,
      no_interruption: result.timed_out === false
        && result.work_limit_reached === false,
    };
    if (Object.values(checks).some((value) => value !== true)) {
      throw new Error("terminal mate rescue checks failed: " + JSON.stringify(checks));
    }
    return {
      schema: "spc-opera-terminal-mate-rescue-receipt-v2",
      status: "passed",
      checks,
      elapsed_seconds: elapsedSeconds,
      best_full_series: result.best_full_series,
      requested_depth: result.requested_depth,
      completed_depth: result.completed_depth,
      score: result.score,
      proof: result.proof,
      proof_bounds: result.proof_bounds,
      work: result.work,
      rescue,
      source_fingerprint: result.source_fingerprint,
      wasm_sha256: result.wasm_sha256,
      kernel_sha256: result.kernel_sha256,
      module_js_sha256: result.module_js_sha256,
      certificate_id: result.certificate_id,
      mate_certificate_id: result.mate_certificate_id,
      prefix_certificate_id: result.prefix_certificate_id,
      runtime_receipt: result.runtime_receipt,
    };
  } finally {
    client.close("terminal mate rescue probe complete");
  }
})()`;


async function main() {
  const args = argumentsOf(process.argv.slice(2));
  if (!Number.isFinite(args.timeoutMs) || args.timeoutMs < 1) {
    throw new Error("--timeout-ms must be positive");
  }
  const version = await fetch(`${args.endpoint}/json/version`, {
    cache: "no-store",
  }).then((response) => response.json());
  const targetResponse = await fetch(
    `${args.endpoint}/json/new?${encodeURIComponent(args.url)}`,
    { method: "PUT", cache: "no-store" },
  );
  if (!targetResponse.ok) {
    throw new Error(`Opera CDP target creation failed: ${targetResponse.status}`);
  }
  const target = await targetResponse.json();
  const { socket, call } = await connect(target.webSocketDebuggerUrl);
  try {
    await call("Runtime.enable");
    await call("Page.enable");
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    const evaluated = await call("Runtime.evaluate", {
      expression: probeExpression,
      awaitPromise: true,
      returnByValue: true,
      timeout: args.timeoutMs,
    });
    if (evaluated.exceptionDetails) {
      throw new Error(JSON.stringify(evaluated.exceptionDetails));
    }
    const payload = evaluated.result?.value;
    if (!payload || payload.status !== "passed") {
      throw new Error(
        "Opera terminal-mate probe did not return a passing receipt: "
          + JSON.stringify(payload),
      );
    }
    const receipt = {
      ...payload,
      cdp: {
        browser: version.Browser,
        protocol_version: version["Protocol-Version"],
        user_agent: version["User-Agent"],
      },
      page_url: args.url,
    };
    await writeFile(args.output, `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
    process.stdout.write(`${JSON.stringify(receipt)}\n`);
  } finally {
    socket.close();
    await fetch(`${args.endpoint}/json/close/${target.id}`, {
      cache: "no-store",
    }).catch(() => undefined);
  }
}


await main();
