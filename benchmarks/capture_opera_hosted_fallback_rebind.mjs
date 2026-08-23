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
    timeoutMs: Number(values.get("--timeout-ms") ?? 60_000),
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


async function evaluate(call, expression) {
  const response = await call("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (response.exceptionDetails) {
    throw new Error(JSON.stringify(response.exceptionDetails));
  }
  return response.result?.value;
}


async function waitFor(call, expression, timeoutMs) {
  const deadline = performance.now() + timeoutMs;
  let last = null;
  while (performance.now() < deadline) {
    last = await evaluate(call, expression);
    if (last?.ready === true) return last;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`Opera fallback probe timed out: ${JSON.stringify(last)}`);
}


const ISOLATE_GAME_AND_BREAK_ROOT_WORKERS = String.raw`(() => {
  const harnessKey = "spc-hosted-fallback-harness-initialized";
  if (sessionStorage.getItem(harnessKey) !== "1") {
    localStorage.removeItem("scottish-progressive-play-session-v2");
    localStorage.removeItem("scottish-progressive-play-session-v1");
    sessionStorage.setItem(harnessKey, "1");
  }
  const NativeWorker = globalThis.Worker;
  globalThis.Worker = class RootFailureWorker extends NativeWorker {
    constructor(url, options = {}) {
      if (String(options?.name || "").startsWith("scottish-progressive-root-")) {
        throw new Error("simulated mobile root Worker admission failure");
      }
      super(url, options);
    }
  };
})()`;


const READY_LOCAL_ENGINE = String.raw`(() => ({
  ...(() => {
    let saved = null;
    try {
      saved = JSON.parse(localStorage.getItem("scottish-progressive-play-session-v2"));
    } catch {}
    const title = document.querySelector("#play-status-title")?.textContent || null;
    const series = document.querySelector("#play-series-title")?.textContent || null;
    const history = [...document.querySelectorAll(".play-history-row")];
    const boardReady = Boolean(
      document.querySelector('[data-square="g2"]')
      && document.querySelector('[data-square="g4"]'),
    );
    return {
      ready: document.querySelector("#engine-status-text")?.textContent
        === "Engine on this device"
        && title === "Your series"
        && series === "Series 1"
        && history.length === 0
        && boardReady
        && typeof saved?.sessionId === "string"
        && saved?.completedSeries?.length === 0
        && saved?.currentPrefix?.length === 0,
      title,
      series,
      boardReady,
      saved,
    };
  })(),
  status: document.querySelector("#engine-status-text")?.textContent || null,
  identity: document.querySelector("#play-engine-version")?.textContent || null,
}))()`;


const PLAY_G4 = String.raw`(() => {
  const faster = document.querySelector("#play-strength-faster");
  if (!faster) return { played: false };
  faster.click();
  const from = document.querySelector('[data-square="g2"]');
  if (!from) return { played: false };
  from.click();
  const to = document.querySelector('[data-square="g4"]');
  if (!to) return { played: false };
  to.click();
  return {
    played: true,
    selected: from.classList.contains("is-selected"),
  };
})()`;


const FALLBACK_FINISHED = String.raw`(() => {
  const title = document.querySelector("#play-status-title")?.textContent || "";
  const detail = document.querySelector("#play-status-detail")?.textContent || "";
  const series = document.querySelector("#play-series-title")?.textContent || "";
  const engine = document.querySelector("#play-engine-version")?.textContent || "";
  const engineStatus = document.querySelector("#engine-status-text")?.textContent || "";
  const engineTitle = document.querySelector("#engine-status")?.title || "";
  const runtime = document.querySelector("#play-runtime-status")?.textContent || "";
  const history = [...document.querySelectorAll(".play-history-row")]
    .map((row) => row.textContent?.replace(/\s+/g, " ").trim() || "");
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem("scottish-progressive-play-session-v2"));
  } catch {}
  const stopped = title.startsWith("Search stopped");
  return {
    ready: series === "Series 3"
      && history.some((row) => row.startsWith("S1Youg4"))
      && history.some((row) => row.startsWith("S2Champion"))
      && saved?.completedSeries?.length === 2,
    stopped,
    title,
    detail,
    series,
    engine,
    engineStatus,
    engineTitle,
    runtime,
    history,
    saved,
  };
})()`;


const RESTORED_GAME = String.raw`(() => {
  const series = document.querySelector("#play-series-title")?.textContent || "";
  const title = document.querySelector("#play-status-title")?.textContent || "";
  const history = [...document.querySelectorAll(".play-history-row")]
    .map((row) => row.textContent?.replace(/\s+/g, " ").trim() || "");
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem("scottish-progressive-play-session-v2"));
  } catch {}
  return {
    ready: series === "Series 3"
      && title === "Your series"
      && history.some((row) => row.startsWith("S1Youg4"))
      && history.some((row) => row.startsWith("S2Champion"))
      && saved?.completedSeries?.length === 2,
    series,
    title,
    history,
    saved,
  };
})()`;


async function main() {
  const args = argumentsOf(process.argv.slice(2));
  if (!Number.isFinite(args.timeoutMs) || args.timeoutMs < 1) {
    throw new Error("--timeout-ms must be positive");
  }
  const version = await fetch(`${args.endpoint}/json/version`, {
    cache: "no-store",
  }).then((response) => response.json());
  const browser = await connect(version.webSocketDebuggerUrl);
  const { browserContextId } = await browser.call("Target.createBrowserContext");
  const { targetId } = await browser.call("Target.createTarget", {
    url: "about:blank",
    browserContextId,
  });
  const targets = await fetch(`${args.endpoint}/json/list`, {
    cache: "no-store",
  }).then((response) => response.json());
  const target = targets.find((entry) => entry.id === targetId);
  if (!target?.webSocketDebuggerUrl) {
    await browser.call("Target.disposeBrowserContext", { browserContextId });
    browser.socket.close();
    throw new Error("Opera isolated CDP target was not discoverable");
  }
  const { socket, call } = await connect(target.webSocketDebuggerUrl);
  try {
    await call("Runtime.enable");
    await call("Page.enable");
    await call("Page.addScriptToEvaluateOnNewDocument", {
      source: ISOLATE_GAME_AND_BREAK_ROOT_WORKERS,
    });
    await call("Page.navigate", { url: args.url });
    const local = await waitFor(call, READY_LOCAL_ENGINE, args.timeoutMs);
    const hostedHealth = await evaluate(call, String.raw`(() => {
      const configuredOrigin = document
        .querySelector('meta[name="spc-api-origin"]')
        ?.getAttribute("content")
        ?.trim()
        ?.replace(/\/$/, "");
      const healthUrl = new URL(
        "/api/health",
        configuredOrigin || location.origin,
      );
      return fetch(healthUrl, { cache: "no-store" })
        .then((response) => response.json());
    })()`);
    const played = await evaluate(call, PLAY_G4);
    if (played?.played !== true) throw new Error("Opera fallback probe could not play g2g4");
    const result = await waitFor(call, FALLBACK_FINISHED, args.timeoutMs);
    await call("Page.reload", { ignoreCache: true });
    const restored = await waitFor(call, RESTORED_GAME, args.timeoutMs);
    const exactHostedIdentity = [
      hostedHealth?.engine_version,
      hostedHealth?.source_fingerprint,
    ].filter(Boolean).join(" · ");
    const checks = {
      local_engine_loaded_first: local.ready === true,
      same_game_advanced: result.series === "Series 3",
      same_session_id: result.saved?.sessionId === local.saved?.sessionId,
      ledger_extended_exactly: result.saved?.completedSeries?.length === 2
        && result.saved.completedSeries[0]?.join("/") === "g2g4"
        && result.saved.completedSeries[1]?.length === 2
        && result.saved.completedSeries[1].every((move) => (
          /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(move)
        ))
        && result.saved?.currentPrefix?.length === 0,
      hosted_engine_rebound: result.engineStatus === "Engine online",
      fallback_reason_recorded: result.engineTitle.includes("browser-root-worker-unavailable"),
      fallback_visible_on_mobile: result.runtime.includes("Continued safely on the hosted engine"),
      engine_identity_changed_truthfully: result.engine === exactHostedIdentity
        && result.engine !== local.identity,
      no_search_stopped_banner: result.stopped === false,
      reload_restored_same_game: restored.ready === true
        && restored.saved?.sessionId === local.saved?.sessionId,
    };
    const receipt = {
      schema: "spc-opera-hosted-fallback-rebind-v1",
      status: Object.values(checks).every(Boolean) ? "passed" : "failed",
      checks,
      before: local,
      after: result,
      restored,
      hosted_health_identity: {
        source_fingerprint: hostedHealth?.source_fingerprint || null,
        engine_profile_id: hostedHealth?.engine_profile_id || null,
        engine_version: hostedHealth?.engine_version || null,
        ruleset_version: hostedHealth?.ruleset_version || null,
      },
      cdp: {
        browser: version.Browser,
        protocol_version: version["Protocol-Version"],
        user_agent: version["User-Agent"],
      },
      page_url: args.url,
    };
    if (receipt.status !== "passed") {
      throw new Error(`Opera hosted fallback rebind failed: ${JSON.stringify(receipt)}`);
    }
    await writeFile(args.output, `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
    process.stdout.write(`${JSON.stringify(receipt)}\n`);
  } finally {
    socket.close();
    await browser.call("Target.disposeBrowserContext", { browserContextId })
      .catch(() => undefined);
    browser.socket.close();
  }
}


await main();
