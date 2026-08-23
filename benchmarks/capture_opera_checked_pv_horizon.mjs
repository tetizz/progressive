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
    timeoutMs: Number(values.get("--timeout-ms") ?? 120_000),
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
  const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
  const UNSAFE_HORIZON = "d1e2/e2c4/c4c7/f1c4/c7c8";
  const UNSAFE_CHILD_FEN = "rnQ1k1nr/1p1p1ppp/8/p3p3/1PB1P3/5P2/1PPP3P/RNB1K1Nq b Qkq - 0 7";
  const manifest = await fetch(
    new URL("./engine/browser-engine-manifest.json?checked-pv-horizon", location.href),
    { cache: "no-store" },
  ).then((response) => {
    if (!response.ok) throw new Error("browser engine manifest fetch failed");
    return response.json();
  });
  const api = globalThis.ScottishProgressiveBrowserEngine;
  if (!api?.createClient) throw new Error("browser engine client did not load");
  const safetyTrace = [];
  const workerFactory = (url, options) => {
    const worker = new Worker(url, options);
    const requests = new Map();
    const postMessage = worker.postMessage.bind(worker);
    worker.postMessage = (message, transfer) => {
      if (message?.type === "root-safety" && Number.isInteger(message.id)) {
        requests.set(message.id, structuredClone(message.payload));
      }
      if (transfer === undefined) postMessage(message);
      else postMessage(message, transfer);
    };
    worker.addEventListener("message", (event) => {
      const request = requests.get(event.data?.id);
      if (!request) return;
      requests.delete(event.data.id);
      safetyTrace.push({
        request,
        ok: event.data.ok === true,
        response: structuredClone(event.data.payload || event.data.error || null),
      });
    });
    return worker;
  };
  const client = api.createClient({
    workerUrl: new URL(
      "./browser-engine-worker.js?checked-pv-horizon",
      location.href,
    ).href,
    workerFactory,
  });
  try {
    const preflightStarted = performance.now();
    const preflight = await client.preflight({
      sourceFingerprint: manifest.source_fingerprint,
      deadlineMs: preflightStarted + 20_000,
    });
    if (preflight.ready !== true) {
      throw new Error("browser preflight failed: " + (preflight.reason || "unknown"));
    }
    const searchStarted = performance.now();
    const payload = {
      fen: START_FEN,
      series: 1,
      quiet_series: 0,
      ep_targets: [],
      progressive_ep: [],
      promoted_hex: "0000000000000000",
      chess960: false,
      prefix: [],
      depth: 5,
      max_series: 32,
      time_limit: 60,
      max_generation_positions: 100_000_000,
      alternatives: 0,
      best_move_only: true,
      rate_move: false,
      save: false,
    };
    const result = await client.analyzeRoot(payload, {
      deadlineMs: searchStarted + 60_000,
      receiptDeadlineMs: searchStarted + 65_000,
    });
    const elapsedSeconds = (performance.now() - searchStarted) / 1_000;
    const horizon = safetyTrace.find((entry) => (
      entry.ok
      && entry.request?.authoritative_child_boundary?.series === 6
      && entry.request?.candidate?.root_series?.machine_notation === UNSAFE_HORIZON
      && entry.request?.authoritative_child_boundary?.fen === UNSAFE_CHILD_FEN
    ));
    const checks = {
      local_wasm_preflight: preflight.ready === true,
      completed_depth_5: result.completed_depth === 5,
      publishable: result.publishable === true,
      selected_safety_certified: result.safety_certified === true,
      policy_is_explicit: result.selection_policy
        === "reject-adverse-checked-pv-mates-v1",
      policy_filtered: result.selection_policy_filtered === true,
      one_or_more_line_vetoes: result.pv_horizon_line_rejections >= 1
        && result.runtime_receipt?.pv_horizon_line_rejections >= 1,
      unsafe_root_not_published: result.best_full_series?.join("/") !== "f2f3",
      exact_horizon_request: horizon?.request?.call_work_credit === 16_384,
      exact_horizon_found: horizon?.response?.status === "found",
      exact_horizon_work: horizon?.response?.work_used === 1_267,
      exact_mate_replayed: horizon?.response?.reply_mate?.checked_prefix?.outcome
        === "checkmate",
      global_work_respected: result.work <= payload.max_generation_positions,
      no_interruption: result.timed_out === false
        && result.work_limit_reached === false,
      deadline_respected: elapsedSeconds < 60,
    };
    if (Object.values(checks).some((value) => value !== true)) {
      throw new Error("checked-PV horizon checks failed: " + JSON.stringify({
        checks,
        result: {
          completed_depth: result.completed_depth,
          best_full_series: result.best_full_series,
          selection_policy: result.selection_policy,
          selection_policy_filtered: result.selection_policy_filtered,
          pv_horizon_line_rejections: result.pv_horizon_line_rejections,
          work: result.work,
          timed_out: result.timed_out,
          work_limit_reached: result.work_limit_reached,
        },
        safetyTrace,
      }));
    }
    return {
      schema: "spc-opera-checked-pv-horizon-receipt-v1",
      status: "passed",
      checks,
      elapsed_seconds: elapsedSeconds,
      best_full_series: result.best_full_series,
      principal_variation: result.principal_variation,
      score: result.score,
      work: result.work,
      source_fingerprint: result.source_fingerprint,
      wasm_sha256: result.wasm_sha256,
      kernel_sha256: result.kernel_sha256,
      module_js_sha256: result.module_js_sha256,
      selection_policy: result.selection_policy,
      pv_horizon_line_rejections: result.pv_horizon_line_rejections,
      horizon_trace: horizon,
      runtime_receipt: result.runtime_receipt,
    };
  } finally {
    client.close("checked-PV horizon Opera probe complete");
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
        "Opera checked-PV probe did not return a passing receipt: "
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
