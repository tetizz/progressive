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
    if (!values.has(required)) {
      throw new Error(`missing ${required}`);
    }
  }
  return {
    endpoint: values.get("--endpoint").replace(/\/$/, ""),
    url: values.get("--url"),
    output: values.get("--output"),
    timeoutMs: Number(values.get("--timeout-ms") ?? 120_000),
  };
}


function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
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
    if (!message.id || !pending.has(message.id)) {
      return;
    }
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) {
      reject(new Error(JSON.stringify(message.error)));
    } else {
      resolve(message.result);
    }
  });
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
  return { socket, call };
}


async function main() {
  const args = argumentsOf(process.argv.slice(2));
  if (!Number.isFinite(args.timeoutMs) || args.timeoutMs < 1) {
    throw new Error("--timeout-ms must be positive");
  }
  const versionResponse = await fetch(`${args.endpoint}/json/version`, {
    cache: "no-store",
  });
  if (!versionResponse.ok) {
    throw new Error(`Opera CDP version endpoint failed: ${versionResponse.status}`);
  }
  const version = await versionResponse.json();
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
    const deadline = performance.now() + args.timeoutMs;
    let status = "";
    while (performance.now() < deadline) {
      const evaluation = await call("Runtime.evaluate", {
        expression: "document.body?.dataset?.probeStatus || ''",
        returnByValue: true,
      });
      status = evaluation.result?.value ?? "";
      if (status === "passed" || status === "failed") {
        break;
      }
      await delay(50);
    }
    if (status !== "passed" && status !== "failed") {
      throw new Error("Opera root-session page did not finish before the deadline");
    }
    const payloadEvaluation = await call("Runtime.evaluate", {
      expression: "document.querySelector('#result')?.textContent || ''",
      returnByValue: true,
    });
    const payload = JSON.parse(payloadEvaluation.result?.value ?? "{}");
    const environmentEvaluation = await call("Runtime.evaluate", {
      expression: `JSON.stringify({
        title: document.title,
        userAgent: navigator.userAgent,
        hardwareConcurrency: navigator.hardwareConcurrency,
        crossOriginIsolated: self.crossOriginIsolated === true,
        location: location.href
      })`,
      returnByValue: true,
    });
    const pageEnvironment = JSON.parse(environmentEvaluation.result?.value ?? "{}");
    const receipt = {
      schema: "spc-opera-root-session-cdp-receipt-v1",
      status: payload.status,
      product_publishable: false,
      safety_certified: false,
      cdp: {
        browser: version.Browser,
        protocol_version: version["Protocol-Version"],
        user_agent: version["User-Agent"],
        web_socket_debugger_url_recorded: true,
      },
      page_environment: pageEnvironment,
      worker_receipt: payload,
    };
    await writeFile(args.output, `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
    process.stdout.write(`${JSON.stringify(receipt)}\n`);
    if (status !== "passed" || payload.status !== "passed-not-certified") {
      throw new Error(payload.message ?? "Opera Worker probe failed");
    }
  } finally {
    socket.close();
    await fetch(`${args.endpoint}/json/close/${target.id}`, {
      cache: "no-store",
    }).catch(() => undefined);
  }
}


await main();
