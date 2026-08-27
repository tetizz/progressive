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
  const MATE_SCORE = 1_000_000;
  const AUTHENTICITY_SCOPE = "local-checkout-hash-bound-unsigned-v1";
  const CHECKED_PV_SELECTION_POLICY =
    "repair-once-then-veto-adverse-checked-pv-mates-v1";
  const SAME_ROOT_REPAIR_POLICY_SCHEMA = "spc-same-root-horizon-repair-policy-v1";
  const POLICY_VETO_SCHEMA = "spc-pv-horizon-candidate-veto-v1";
  const THRESHOLD_VETO_WITNESS_SCHEMA =
    "spc-opera-same-root-repair-limit-witness-v1";
  const MAXIMUM_SUCCESSFUL_SAME_ROOT_REPAIRS = 1;
  const EXPECTED_WITNESSES = Object.freeze([
    Object.freeze({
      label: "f3",
      root_series: "f2f3",
      unsafe_horizon: "d1e2/e2c4/c4c7/f1c4/c7c8",
      unsafe_child_fen: "rnQ1k1nr/1p1p1ppp/8/p3p3/1PB1P3/5P2/1PPP3P/RNB1K1Nq b Qkq - 0 7",
    }),
    Object.freeze({
      label: "b3",
      root_series: "b2b3",
      unsafe_horizon: "e1f2/d1g4/f2e3/g1h3/g4h5",
      unsafe_child_fen: "rnbq1bnr/pppp1kpp/4p3/7Q/2B5/1P2K2N/PBPP2PP/RN5R b - - 4 7",
    }),
  ]);
  const SHA256 = /^[0-9a-f]{64}$/;
  const SOURCE_FINGERPRINT = /^[0-9a-f]{16}$/;
  const UCI_MOVE = /^[a-h][1-8][a-h][1-8][qrbn]?$/;
  const SQUARE = /^[a-h][1-8]$/;
  const ROOT_CERTIFICATE_ID = /^spc-root-session-[0-9a-f]{16}$/;
  const MATE_CERTIFICATE_ID = /^spc-mate-[0-9a-f]{16}$/;
  const PREFIX_CERTIFICATE_ID = /^spc-prefix-[0-9a-f]{16}$/;
  const EXACT_BOUNDARY_KEYS = Object.freeze([
    "board_fen", "chess960", "ep_targets", "fen", "progressive_ep",
    "promoted_hex", "quiet_draw_pending", "quiet_series", "series",
    "series_number", "side_to_move",
  ]);
  const CANONICAL_SERIES_KEYS = Object.freeze([
    "child_boundary", "ended_by_check", "machine_notation", "moves",
    "outcome", "transposition_count",
  ]);
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const sameJson = (left, right) => JSON.stringify(left) === JSON.stringify(right);
  const exactInteger = (
    value,
    minimum = 0,
    maximum = Number.MAX_SAFE_INTEGER,
  ) => Number.isSafeInteger(value) && value >= minimum && value <= maximum;
  const plainObject = (value) => Boolean(
    value && typeof value === "object" && !Array.isArray(value)
  );
  const sameStrings = (left, right) => Array.isArray(left)
    && Array.isArray(right)
    && left.length === right.length
    && left.every((value, index) => value === right[index]);
  const exactBoundary = (value) => {
    if (
      !plainObject(value)
      || !sameStrings(Object.keys(value).sort(), EXACT_BOUNDARY_KEYS)
      || typeof value.fen !== "string"
      || value.fen !== value.fen.trim()
      || value.fen.split(" ").length !== 6
      || value.board_fen !== value.fen
      || !exactInteger(value.series, 1, 256)
      || value.series_number !== value.series
      || !exactInteger(value.quiet_series, 0, 1_000_000)
      || value.quiet_draw_pending !== (value.quiet_series >= 10)
      || !Array.isArray(value.ep_targets)
      || value.ep_targets.length > 8
      || value.ep_targets.some((square) => !SQUARE.test(String(square)))
      || new Set(value.ep_targets).size !== value.ep_targets.length
      || !sameStrings(value.progressive_ep, value.ep_targets)
      || !/^[0-9a-f]{16}$/.test(String(value.promoted_hex || ""))
      || value.chess960 !== false
    ) return false;
    const fenMover = value.fen.split(" ")[1];
    return ["w", "b"].includes(fenMover)
      && value.side_to_move === (fenMover === "w" ? "white" : "black")
      && ((value.series % 2 === 1) === (fenMover === "w"));
  };
  const canonicalSeries = (value) => plainObject(value)
    && sameStrings(Object.keys(value).sort(), CANONICAL_SERIES_KEYS)
    && Array.isArray(value.moves)
    && value.moves.length > 0
    && value.moves.every((move) => typeof move === "string" && UCI_MOVE.test(move))
    && value.machine_notation === value.moves.join("/")
    && exactInteger(value.transposition_count, 1)
    && exactBoundary(value.child_boundary)
    && [null, "checkmate", "stalemate", "ten_series_draw"].includes(value.outcome)
    && typeof value.ended_by_check === "boolean"
    && (value.outcome !== "checkmate" || value.ended_by_check === true)
    && (!["stalemate", "ten_series_draw"].includes(value.outcome)
      || value.ended_by_check === false);
  const sameBoundary = (left, right) => exactBoundary(left)
    && exactBoundary(right)
    && left.fen === right.fen
    && left.board_fen === right.board_fen
    && left.series === right.series
    && left.series_number === right.series_number
    && left.side_to_move === right.side_to_move
    && left.quiet_series === right.quiet_series
    && left.quiet_draw_pending === right.quiet_draw_pending
    && sameStrings(left.ep_targets, right.ep_targets)
    && sameStrings(left.progressive_ep, right.progressive_ep)
    && left.promoted_hex === right.promoted_hex
    && left.chess960 === right.chess960;
  const sameSeries = (left, right) => canonicalSeries(left)
    && canonicalSeries(right)
    && sameStrings(left.moves, right.moves)
    && left.machine_notation === right.machine_notation
    && left.transposition_count === right.transposition_count
    && sameBoundary(left.child_boundary, right.child_boundary)
    && left.outcome === right.outcome
    && left.ended_by_check === right.ended_by_check;
  const contiguousRootedPath = (path) => Array.isArray(path)
    && path.length > 0
    && path.every((series, index) => {
      const expectedSeries = index + 1;
      return canonicalSeries(series)
        && series.moves.length <= expectedSeries
        && (series.moves.length === expectedSeries || series.ended_by_check === true)
        && series.child_boundary.series === expectedSeries + 1
        && (index === path.length - 1 || series.outcome === null);
    });
  const pathSignature = (path) => Array.isArray(path)
    && path.every(canonicalSeries)
    ? path.map((series) => series.machine_notation).join("|")
    : null;
  const sameFinalBoard = (frameFen, boardFen) => {
    const frame = typeof frameFen === "string" ? frameFen.split(" ") : [];
    const board = typeof boardFen === "string" ? boardFen.split(" ") : [];
    return frame.length === 6
      && board.length === 6
      && [0, 1, 2, 4, 5].every((index) => frame[index] === board[index]);
  };
  const bitCount16 = (value) => {
    let remaining = value;
    let count = 0;
    while (remaining !== 0) {
      count += remaining & 1;
      remaining >>>= 1;
    }
    return count;
  };
  const nonnegativeIntegerObject = (value) => plainObject(value)
    && Object.keys(value).length > 0
    && Object.values(value).every((item) => exactInteger(item, 0));
  const canonicalJson = (value) => {
    if (value === null || typeof value === "string" || typeof value === "boolean") {
      return JSON.stringify(value);
    }
    if (typeof value === "number" && Number.isFinite(value)) return JSON.stringify(value);
    if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
    if (plainObject(value)) {
      return "{" + Object.keys(value).sort().map((key) => (
        JSON.stringify(key) + ":" + canonicalJson(value[key])
      )).join(",") + "}";
    }
    throw new Error("asset identity contains a non-canonical JSON value");
  };
  const sha256Hex = async (bytes) => {
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), (value) => (
      value.toString(16).padStart(2, "0")
    )).join("");
  };
  const canonicalSha256 = async (value) => sha256Hex(
    new TextEncoder().encode(canonicalJson(value)),
  );
  const fetchAsset = async (url, label) => {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(label + " fetch failed: " + response.status);
    const bytes = await response.arrayBuffer();
    if (bytes.byteLength < 1) throw new Error(label + " is empty");
    return Object.freeze({
      url: url.href,
      byte_length: bytes.byteLength,
      sha256: await sha256Hex(bytes),
      bytes,
    });
  };
  if (location.protocol !== "http:" || location.hostname !== "127.0.0.1") {
    throw new Error("checked-PV certification is restricted to a local checkout");
  }
  const pageEnvironment = Object.freeze({
    location: location.href,
    userAgent: String(navigator.userAgent || ""),
    hardwareConcurrency: navigator.hardwareConcurrency,
    crossOriginIsolated: globalThis.crossOriginIsolated === true,
  });
  if (
    !pageEnvironment.location
    || !pageEnvironment.userAgent.includes(" OPR/")
    || !exactInteger(pageEnvironment.hardwareConcurrency, 1, 1_024)
    || typeof globalThis.crossOriginIsolated !== "boolean"
  ) throw new Error("checked-PV certification requires an identified Opera page realm");
  const manifestUrl = new URL(
    "./engine/browser-engine-manifest.json",
    location.href,
  );
  const expectedWorkerUrl = new URL(
    "./browser-engine-worker.js?checked-pv-horizon",
    location.href,
  );
  const manifestAsset = await fetchAsset(manifestUrl, "browser engine manifest");
  let manifest;
  try {
    manifest = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(
      manifestAsset.bytes,
    ));
  } catch {
    throw new Error("browser engine manifest is not strict UTF-8 JSON");
  }
  const variant = manifest?.variants?.single;
  const safetyCertificate = variant?.safety_certificate ?? null;
  const rootCertificate = variant?.root_session_certificate;
  const mateCertificate = variant?.mate_certificate;
  const prefixCertificate = variant?.prefix_certificate;
  const horizonContract = rootCertificate?.root_session_contract?.horizon_research;
  if (
    manifest?.schema !== "spc-browser-wasm-manifest-v1"
    || manifest.contract_version !== 1
    || manifest.abi_version !== 1
    || !SOURCE_FINGERPRINT.test(String(manifest.source_fingerprint || ""))
    || !plainObject(manifest.variants)
    || !sameStrings(Object.keys(manifest.variants).sort(), ["single"])
    || !plainObject(variant)
    || variant.thread_count !== 1
    || !Array.isArray(variant.support_files)
    || variant.support_files.length !== 0
    || typeof variant.module_js !== "string"
    || !/^[A-Za-z0-9._-]+$/.test(variant.module_js)
    || typeof variant.wasm !== "string"
    || !/^[A-Za-z0-9._-]+$/.test(variant.wasm)
    || !SHA256.test(String(variant.module_js_sha256 || ""))
    || !SHA256.test(String(variant.wasm_sha256 || ""))
    || !SHA256.test(String(variant.kernel_sha256 || ""))
    || (safetyCertificate !== null && (
      safetyCertificate.status !== "certified"
      || typeof safetyCertificate.certificate_id !== "string"
      || !safetyCertificate.certificate_id
      || safetyCertificate.source_fingerprint !== manifest.source_fingerprint
      || safetyCertificate.wasm_sha256 !== variant.wasm_sha256
      || safetyCertificate.module_js_sha256 !== variant.module_js_sha256
    ))
    || rootCertificate?.status !== "certified"
    || rootCertificate.schema !== "spc-root-session-certificate-v1"
    || rootCertificate.contract_version !== 1
    || rootCertificate.abi_version !== 2
    || rootCertificate.root_session_certified !== true
    || rootCertificate.reply_mate_safety !== false
    || rootCertificate.product_publishable !== false
    || !ROOT_CERTIFICATE_ID.test(String(rootCertificate.certificate_id || ""))
    || !["engine_version", "ruleset_version", "profile_id"].every((key) => (
      typeof rootCertificate.engine?.[key] === "string"
      && Boolean(rootCertificate.engine[key])
    ))
    || mateCertificate?.status !== "certified"
    || mateCertificate.schema !== "spc-series-mate-certificate-v1"
    || mateCertificate.contract_version !== 1
    || mateCertificate.abi_version !== 1
    || mateCertificate.mate_capability_certified !== true
    || mateCertificate.reply_mate_safety !== true
    || mateCertificate.product_publishable !== false
    || !MATE_CERTIFICATE_ID.test(String(mateCertificate.certificate_id || ""))
    || prefixCertificate?.status !== "certified"
    || prefixCertificate.contract_version !== 1
    || !PREFIX_CERTIFICATE_ID.test(String(prefixCertificate.certificate_id || ""))
    || rootCertificate.runtime_variant !== "single"
    || rootCertificate.thread_count !== 1
    || mateCertificate.runtime_variant !== "single"
    || mateCertificate.thread_count !== 1
    || prefixCertificate.runtime_variant !== "single"
    || prefixCertificate.thread_count !== 1
    || rootCertificate.root_session_contract?.schema !== "spc-root-session-contract-v1"
    || rootCertificate.root_session_contract?.abi_version !== 2
    || rootCertificate.root_session_contract?.request_schemas?.horizon_research
      !== "spc-root-horizon-research-task-v1"
    || rootCertificate.root_session_contract?.result_schemas?.horizon_research
      !== "spc-root-horizon-research-result-v1"
    || rootCertificate.root_session_contract?.hard_limits?.maximum_horizon_proofs !== 16
    || rootCertificate.root_session_contract?.hard_limits?.maximum_horizon_proof_path !== 8
    || horizonContract?.task_schema !== "spc-root-horizon-research-task-v1"
    || horizonContract?.result_schema !== "spc-root-horizon-research-result-v1"
    || horizonContract?.proof_schema !== "spc-retained-root-horizon-proof-v1"
    || horizonContract?.hit_mask_order !== "request-order"
    || rootCertificate.evidence?.failures !== 0
    || rootCertificate.evidence?.checked_horizon_proof_research !== true
    || rootCertificate.evidence?.checked_horizon_newest_proof_hit !== true
    || rootCertificate.evidence?.selected_owner_warm_exact_certification !== true
  ) {
    throw new Error("browser engine manifest has no certified single-WASM identity");
  }
  const manifestBinding = Object.freeze({
    source_fingerprint: manifest.source_fingerprint,
    runtime_variant: "single",
    thread_count: variant.thread_count,
    module_js: variant.module_js,
    wasm: variant.wasm,
    module_js_sha256: variant.module_js_sha256,
    wasm_sha256: variant.wasm_sha256,
    kernel_sha256: variant.kernel_sha256,
    analysis_certificate_id: safetyCertificate?.certificate_id ?? null,
    root_session_certificate_id: rootCertificate.certificate_id,
    mate_certificate_id: mateCertificate.certificate_id,
    prefix_certificate_id: prefixCertificate.certificate_id,
    root_contract_sha256: await canonicalSha256(rootCertificate.root_session_contract),
    root_geometry_sha256: await canonicalSha256(rootCertificate.geometry),
    root_evidence_sha256: await canonicalSha256(rootCertificate.evidence),
    prefix_contract_sha256: await canonicalSha256(prefixCertificate.prefix_contract),
  });
  const engineRootUrl = new URL("./single/", manifestUrl);
  const compiledModuleUrl = new URL(variant.module_js, engineRootUrl);
  const compiledWasmUrl = new URL(variant.wasm, engineRootUrl);
  compiledModuleUrl.searchParams.set("sha256", variant.module_js_sha256);
  compiledWasmUrl.searchParams.set("sha256", variant.wasm_sha256);
  if (
    engineRootUrl.origin !== location.origin
    || compiledModuleUrl.origin !== location.origin
    || compiledWasmUrl.origin !== location.origin
  ) throw new Error("compiled browser assets escaped the local origin");
  const localAssetEntries = await Promise.all([
    ["browser_engine_worker", expectedWorkerUrl],
    ["browser_engine_client", new URL("./browser-engine-client.js", location.href)],
    ["browser_prefix_contract", new URL(
      "./browser-prefix-contract.js",
      location.href,
    )],
    ["browser_root_iteration_client", new URL(
      "./browser-root-iteration-client.js",
      location.href,
    )],
    ["root_iteration_coordinator", new URL(
      "./root-iteration-coordinator.js",
      location.href,
    )],
    ["wasm_kernel_adapter", new URL(
      "./wasm-kernel-adapter.js?checked-pv-horizon",
      location.href,
    )],
    ["compiled_module", compiledModuleUrl],
    ["compiled_wasm", compiledWasmUrl],
  ].map(async ([label, url]) => [label, await fetchAsset(url, label)]));
  const localAssets = Object.fromEntries(localAssetEntries.map(([label, asset]) => [
    label,
    Object.freeze({
      url: asset.url,
      byte_length: asset.byte_length,
      sha256: asset.sha256,
    }),
  ]));
  const localCheckoutAssetSetSha256 = await canonicalSha256([
    ["browser_engine_manifest", {
      url: manifestAsset.url,
      byte_length: manifestAsset.byte_length,
      sha256: manifestAsset.sha256,
    }],
    ...Object.entries(localAssets),
  ].sort(([left], [right]) => left.localeCompare(right)));
  if (
    localAssets.compiled_module.sha256 !== variant.module_js_sha256
    || localAssets.compiled_wasm.sha256 !== variant.wasm_sha256
  ) {
    throw new Error("local compiled browser assets differ from their manifest hashes");
  }
  const api = globalThis.ScottishProgressiveBrowserEngine;
  if (
    !api?.createClient
    || typeof api.BrowserEngineClient !== "function"
    || Object.isFrozen(api) !== true
  ) throw new Error("browser engine client did not load as its frozen local API");
  const NativeWorker = globalThis.Worker;
  if (
    typeof NativeWorker !== "function"
    || !/\[native code\]/.test(Function.prototype.toString.call(NativeWorker))
  ) throw new Error("the browser Worker constructor is not native");
  const nativePostMessage = NativeWorker.prototype.postMessage;
  const nativeAddEventListener = EventTarget.prototype.addEventListener;
  if (
    typeof nativePostMessage !== "function"
    || typeof nativeAddEventListener !== "function"
    || !/\[native code\]/.test(Function.prototype.toString.call(nativePostMessage))
    || !/\[native code\]/.test(Function.prototype.toString.call(nativeAddEventListener))
  ) throw new Error("the browser Worker messaging surface is unavailable");
  const safetyTrace = [];
  const horizonResearchTrace = [];
  const workerFactoryCalls = [];
  const workerNames = new Set();
  let capturedRequestSequence = 0;
  let syntheticWorkerEvents = 0;
  const workerFactory = (url, options) => {
    const resolvedUrl = new URL(String(url), location.href);
    const workerName = String(options?.name || "");
    const rootName = /^scottish-progressive-root-(root-[0-7])$/.exec(workerName);
    if (
      resolvedUrl.href !== expectedWorkerUrl.href
      || !plainObject(options)
      || !sameStrings(Object.keys(options).sort(), ["name", "type"])
      || options?.type !== "module"
      || (workerName !== "scottish-progressive-engine" && rootName === null)
      || workerNames.has(workerName)
    ) {
      throw new Error("browser engine requested an unbound Worker instance");
    }
    workerNames.add(workerName);
    const workerIdentity = Object.freeze({
      factory_sequence: workerFactoryCalls.length + 1,
      name: workerName,
      channel_id: rootName === null ? null : rootName[1],
      url: resolvedUrl.href,
      type: options.type,
    });
    workerFactoryCalls.push(workerIdentity);
    const worker = new NativeWorker(resolvedUrl.href, options);
    if (!(worker instanceof NativeWorker)) {
      throw new Error("browser engine Worker construction was intercepted");
    }
    const requests = new Map();
    const tracedPostMessage = (message, transfer) => {
      const horizonResearch = message?.type === "root-search"
        && message?.payload?.schema === "spc-root-horizon-research-task-v1";
      if (
        Number.isInteger(message?.id)
        && (message.type === "root-safety" || horizonResearch)
      ) {
        if (requests.has(message.id)) {
          throw new Error("duplicate in-flight Worker request id");
        }
        capturedRequestSequence += 1;
        requests.set(message.id, {
          type: message.type,
          payload: structuredClone(message.payload),
          request_sequence: capturedRequestSequence,
          posted_monotonic_ms: performance.now(),
        });
      }
      if (transfer === undefined) nativePostMessage.call(worker, message);
      else nativePostMessage.call(worker, message, transfer);
    };
    Object.defineProperty(worker, "postMessage", {
      value: tracedPostMessage,
      writable: false,
      configurable: false,
    });
    nativeAddEventListener.call(worker, "message", (event) => {
      if (event.isTrusted !== true) {
        syntheticWorkerEvents += 1;
        return;
      }
      const pending = requests.get(event.data?.id);
      if (!pending) return;
      requests.delete(event.data.id);
      const entry = {
        worker: workerIdentity,
        request_sequence: pending.request_sequence,
        posted_monotonic_ms: pending.posted_monotonic_ms,
        received_monotonic_ms: performance.now(),
        request: pending.payload,
        ok: event.data.ok === true,
        response: structuredClone(
          event.data.ok === true ? event.data.payload : event.data.error,
        ),
      };
      if (pending.type === "root-safety") safetyTrace.push(entry);
      else horizonResearchTrace.push(entry);
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
  if (!(client instanceof api.BrowserEngineClient)) {
    throw new Error("local browser API returned an uncertified client implementation");
  }
  try {
    const preflightStarted = performance.now();
    const preflight = await client.preflight({
      sourceFingerprint: manifest.source_fingerprint,
      deadlineMs: preflightStarted + 20_000,
    });
    if (preflight.ready !== true) {
      throw new Error("browser preflight failed: " + (preflight.reason || "unknown"));
    }
    const preflightIdentity = Object.freeze({
      ready: preflight.ready,
      analysis_ready: preflight.analysis_ready,
      root_iteration_ready: preflight.root_iteration_ready,
      root_session_ready: preflight.root_session_ready,
      mate_ready: preflight.mate_ready,
      prefix_ready: preflight.prefix_ready,
      safety_certified: preflight.safety_certified,
      source_fingerprint: preflight.source_fingerprint,
      runtime_variant: preflight.runtime_variant,
      thread_count: preflight.thread_count,
      module_js_sha256: preflight.module_js_sha256,
      wasm_sha256: preflight.wasm_sha256,
      kernel_sha256: preflight.kernel_sha256,
      certificate_id: preflight.certificate_id,
      root_session_certificate_id: preflight.root_session_certificate_id,
      mate_certificate_id: preflight.mate_certificate_id,
      prefix_certificate_id: preflight.prefix_certificate_id,
      engine_profile_id: preflight.engine_profile_id,
      engine_version: preflight.engine_version,
      ruleset_version: preflight.ruleset_version,
      root_contract_sha256: await canonicalSha256(preflight.root_session_contract),
      root_geometry_sha256: await canonicalSha256(preflight.root_geometry),
      prefix_contract_sha256: await canonicalSha256(preflight.prefix_contract),
    });
    const searchStarted = performance.now();
    const searchDeadline = searchStarted + 60_000;
    const searchReceiptDeadline = searchStarted + 65_000;
    const searchDeadlineEpoch = performance.timeOrigin + searchDeadline;
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
      deadlineMs: searchDeadline,
      receiptDeadlineMs: searchReceiptDeadline,
    });
    const elapsedSeconds = (performance.now() - searchStarted) / 1_000;

    const rootIdentity = Object.freeze({
      source_fingerprint: manifest.source_fingerprint,
      kernel_sha256: variant.kernel_sha256,
      module_js_sha256: variant.module_js_sha256,
      certificate_id: rootCertificate.certificate_id,
      runtime_variant: "single",
      thread_count: 1,
      engine_version: rootCertificate.engine?.engine_version,
      ruleset_version: rootCertificate.engine?.ruleset_version,
      profile_id: rootCertificate.engine?.profile_id,
    });
    const sharedRootIdentityMatches = (value) => plainObject(value)
      && Object.entries(rootIdentity).every(([key, expected]) => (
        key === "certificate_id" || value[key] === expected
      ));
    const rootIdentityMatches = (value) => sharedRootIdentityMatches(value)
      && value.certificate_id === rootIdentity.certificate_id;
    const prefixIdentityMatches = (value) => plainObject(value)
      && value.source_fingerprint === manifest.source_fingerprint
      && value.wasm_sha256 === variant.wasm_sha256
      && value.module_js_sha256 === variant.module_js_sha256
      && value.certificate_id === prefixCertificate.certificate_id
      && value.engine_version === rootIdentity.engine_version
      && value.ruleset_version === rootIdentity.ruleset_version;
    const manifestPreflightBound = Boolean(
      sharedRootIdentityMatches(preflight)
      && preflight.ready === true
      && preflight.wasm_sha256 === variant.wasm_sha256
      && preflight.certificate_id === (safetyCertificate?.certificate_id ?? null)
      && preflight.analysis_ready === (safetyCertificate !== null)
      && preflight.root_session_certificate_id === rootCertificate.certificate_id
      && preflight.mate_certificate_id === mateCertificate.certificate_id
      && preflight.prefix_certificate_id === prefixCertificate.certificate_id
      && preflight.runtime_variant === "single"
      && preflight.thread_count === 1
      && preflight.engine_profile_id === rootIdentity.profile_id
      && preflight.root_iteration_ready === true
      && preflight.root_session_ready === true
      && preflight.mate_ready === true
      && preflight.prefix_ready === true
      && sameJson(preflight.root_session_contract, rootCertificate.root_session_contract)
      && sameJson(preflight.root_geometry, rootCertificate.geometry)
      // The compiled facade and manifest may serialize the same contract with
      // a different property order; compare their canonical value instead.
      && canonicalJson(preflight.prefix_contract)
        === canonicalJson(prefixCertificate.prefix_contract)
      && preflightIdentity.root_contract_sha256 === manifestBinding.root_contract_sha256
      && preflightIdentity.root_geometry_sha256 === manifestBinding.root_geometry_sha256
      && preflightIdentity.prefix_contract_sha256 === manifestBinding.prefix_contract_sha256
      && rootCertificate.source_fingerprint === manifest.source_fingerprint
      && rootCertificate.wasm_sha256 === variant.wasm_sha256
      && rootCertificate.kernel_sha256 === variant.kernel_sha256
      && rootCertificate.module_js_sha256 === variant.module_js_sha256
      && mateCertificate.source_fingerprint === manifest.source_fingerprint
      && mateCertificate.wasm_sha256 === variant.wasm_sha256
      && mateCertificate.kernel_sha256 === variant.kernel_sha256
      && mateCertificate.module_js_sha256 === variant.module_js_sha256
      && prefixCertificate.source_fingerprint === manifest.source_fingerprint
      && prefixCertificate.wasm_sha256 === variant.wasm_sha256
      && prefixCertificate.module_js_sha256 === variant.module_js_sha256
    );
    const resultIdentityBound = Boolean(
      result.source_fingerprint === manifest.source_fingerprint
      && result.wasm_sha256 === variant.wasm_sha256
      && result.kernel_sha256 === variant.kernel_sha256
      && result.module_js_sha256 === variant.module_js_sha256
      && result.certificate_id === rootCertificate.certificate_id
      && result.mate_certificate_id === mateCertificate.certificate_id
      && result.prefix_certificate_id === prefixCertificate.certificate_id
      && result.runtime_variant === "single"
      && result.thread_count === 1
      && result.engine_profile_id === rootIdentity.profile_id
      && result.engine_version === rootIdentity.engine_version
      && result.ruleset_version === rootIdentity.ruleset_version
      && result.runtime_receipt?.source_fingerprint === manifest.source_fingerprint
      && result.runtime_receipt?.artifact_fingerprint === variant.wasm_sha256
      && result.runtime_receipt?.kernel_fingerprint === variant.kernel_sha256
      && result.runtime_receipt?.module_fingerprint === variant.module_js_sha256
      && result.runtime_receipt?.certificate_id === rootCertificate.certificate_id
      && result.runtime_receipt?.mate_certificate_id === mateCertificate.certificate_id
      && result.runtime_receipt?.runtime_variant === "single"
      && result.runtime_receipt?.thread_count === 1
    );
    const resultSummary = Object.freeze({
      ok: result.ok,
      status: result.status,
      requested_depth: result.requested_depth,
      completed_depth: result.completed_depth,
      publishable: result.publishable,
      safety_certified: result.safety_certified,
      coverage_complete: result.root_bound_coverage_complete,
      coverage_scope: result.root_bound_coverage_scope,
      root_scores_complete: result.root_scores_complete,
      width_complete: result.exact_width,
      legal_series_certified: result.legal_series_certified,
      authoritative_replay_certified: result.authoritative_replay_certified,
      legal_validation_runtime: result.legal_validation_runtime,
      root_search_mode: result.root_search_mode,
      selection_policy: result.selection_policy,
      selection_policy_filtered: result.selection_policy_filtered,
      unfiltered_score_winner_selected: result.unfiltered_score_winner_selected,
      pv_horizon_line_rejections: result.pv_horizon_line_rejections,
      pv_horizon_native_repairs: result.pv_horizon_native_repairs,
      pv_horizon_candidate_vetoes: result.pv_horizon_candidate_vetoes,
      same_root_repair_policy: result.same_root_repair_policy,
      pv_horizon_policy_vetoes: result.pv_horizon_policy_vetoes,
      timed_out: result.timed_out,
      work_limit_reached: result.work_limit_reached,
      work: result.work,
      source_fingerprint: result.source_fingerprint,
      wasm_sha256: result.wasm_sha256,
      kernel_sha256: result.kernel_sha256,
      module_js_sha256: result.module_js_sha256,
      certificate_id: result.certificate_id,
      mate_certificate_id: result.mate_certificate_id,
      prefix_certificate_id: result.prefix_certificate_id,
      runtime_variant: result.runtime_variant,
      thread_count: result.thread_count,
      engine_profile_id: result.engine_profile_id,
      engine_version: result.engine_version,
      ruleset_version: result.ruleset_version,
      best_full_series: result.best_full_series,
      score: result.score,
      proof_bounds: result.proof_bounds,
    });
    const exactDeadline = (entry) => plainObject(entry)
      && Number.isFinite(entry.request?.deadline_monotonic_ms)
      && entry.request.deadline_monotonic_ms === searchDeadline
      && Number.isFinite(entry.request?.deadline_epoch_ms)
      && entry.request.deadline_epoch_ms === searchDeadlineEpoch
      && exactInteger(entry.request?.remaining_time_ms, 1, 60_000)
      && entry.posted_monotonic_ms < searchDeadline
      && entry.received_monotonic_ms < searchDeadline
      && entry.request.remaining_time_ms <= Math.ceil(
        searchDeadline - entry.posted_monotonic_ms,
      ) + 5;
    const fullWorkAccounting = (entry, { positive }) => {
      const request = entry?.request;
      const work = entry?.response?.work;
      if (
        !plainObject(request)
        || !plainObject(work)
        || !exactInteger(request.external_work, 0, payload.max_generation_positions)
        || !exactInteger(request.native_work_before, 0, payload.max_generation_positions)
        || !exactInteger(request.call_work_credit, 1, 0xffffffff)
        || work.external_work !== request.external_work
        || work.native_work_before !== request.native_work_before
        || work.call_work_credit !== request.call_work_credit
        || !exactInteger(work.native_work_after, work.native_work_before)
        || !exactInteger(work.call_native_work, positive ? 1 : 0, request.call_work_credit)
        || work.call_native_work !== work.native_work_after - work.native_work_before
        || work.total_accounted_work !== work.external_work + work.native_work_after
        || !exactInteger(work.total_accounted_work, 0, payload.max_generation_positions)
        || !nonnegativeIntegerObject(work.call_stats)
        || !nonnegativeIntegerObject(work.cumulative_stats)
        || !sameStrings(
          Object.keys(work.call_stats).sort(),
          Object.keys(work.cumulative_stats).sort(),
        )
        || Object.keys(work.call_stats).some(
          (key) => work.cumulative_stats[key] < work.call_stats[key],
        )
      ) return false;
      for (const [entries, peak, capacity] of [
        ["tt_entries", "tt_entries_peak", "tt_capacity"],
        ["eval_entries", "eval_entries_peak", "eval_capacity"],
      ]) {
        if (
          !exactInteger(work[capacity], 1)
          || !exactInteger(work[entries], 0, work[capacity])
          || !exactInteger(work[peak], work[entries], work[capacity])
        ) return false;
      }
      return exactInteger(work.series_cache_capacity, 1)
        && exactInteger(
          work.series_cache_weight_peak,
          0,
          work.series_cache_capacity,
        )
        && exactInteger(
          work.series_cache_entries_peak,
          0,
          work.series_cache_weight_peak,
        );
    };
    const exactPrefixMateReplay = (entry) => {
      const request = entry?.request;
      const response = entry?.response;
      const child = request?.authoritative_child_boundary;
      const horizonSeries = request?.candidate?.root_series;
      const rootReplay = request?.authoritative_root_replay;
      const mate = response?.reply_mate;
      const checked = mate?.checked_prefix;
      const expectedScore = child?.side_to_move === "white"
        ? MATE_SCORE - 2
        : -MATE_SCORE + 2;
      const expectedBounds = child?.side_to_move === "white" ? [1, 1] : [-1, -1];
      const remaining = Number(child?.series) - Number(mate?.moves?.length);
      return entry?.ok === true
        && entry.worker?.channel_id === request?.candidate?.owner_worker_id
        && request?.schema === "spc-root-safety-task-v1"
        && rootIdentityMatches(request)
        && rootIdentityMatches(response)
        && exactDeadline(entry)
        && exactInteger(request.call_work_credit, 1, 0xffffffff)
        && Object.keys(request).every((key) => (
          own(response, key) && sameJson(response[key], request[key])
        ))
        && canonicalSeries(horizonSeries)
        && horizonSeries.ended_by_check === true
        && horizonSeries.outcome === null
        && sameBoundary(horizonSeries.child_boundary, child)
        && rootReplay?.schema === "spc-boundary-prefix-v1"
        && prefixIdentityMatches(rootReplay)
        && rootReplay.request_id === request.iteration_id + ":"
          + request.safety_revision + ":pv-horizon-replay-4"
        && sameStrings(rootReplay.prefix, horizonSeries.moves)
        && sameStrings(rootReplay.current_prefix, horizonSeries.moves)
        && rootReplay.complete === true
        && rootReplay.outcome === null
        && rootReplay.completion_reason === "check"
        && rootReplay.ended_by_check === true
        && rootReplay.check === true
        && rootReplay.in_check === true
        && sameBoundary(rootReplay.next_state, child)
        && response.status === "found"
        && exactInteger(response.work_used, 1, request.call_work_credit)
        && exactInteger(
          response.memory_bytes,
          1,
          preflight.memory_limits?.maximum_bytes,
        )
        && exactInteger(
          response.memory_peak_bytes,
          response.memory_bytes,
          preflight.memory_limits?.maximum_bytes,
        )
        && response.override_score === expectedScore
        && sameJson(response.proof_bounds, expectedBounds)
        && plainObject(mate)
        && sameStrings(
          Object.keys(mate).sort(),
          ["checked_prefix", "ended_by_check", "machine_notation", "moves", "outcome"],
        )
        && Array.isArray(mate?.moves)
        && mate.moves.length > 0
        && mate.moves.length <= child.series
        && mate.moves.every((move) => typeof move === "string" && UCI_MOVE.test(move))
        && mate.machine_notation === mate.moves.join("/")
        && mate.outcome === "checkmate"
        && mate.ended_by_check === true
        && checked?.schema === "spc-boundary-prefix-v1"
        && checked.abi_version === 1
        && checked.ok === true
        && checked.status === "complete"
        && prefixIdentityMatches(checked)
        && checked.request_id === request.iteration_id + ":"
          + request.safety_revision + ":mate-replay"
        && sameBoundary(checked.boundary_state, child)
        && sameStrings(checked.prefix, mate.moves)
        && sameStrings(checked.current_prefix, mate.moves)
        && Array.isArray(checked.san)
        && checked.san.length === mate.moves.length
        && Array.isArray(checked.frames)
        && checked.frames.length === mate.moves.length
        && checked.frames.every((frame, index) => (
          frame?.index === index + 1
          && frame.uci === mate.moves[index]
          && typeof frame.san === "string"
          && frame.san === checked.san[index]
          && typeof frame.board_fen === "string"
          && frame.board_fen.split(" ").length === 6
        ))
        && checked.fen === checked.board_fen
        && sameFinalBoard(checked.frames.at(-1)?.board_fen, checked.board_fen)
        && checked.complete === true
        && checked.outcome === "checkmate"
        && checked.completion_reason === "checkmate"
        && checked.ended_by_check === true
        && checked.check === true
        && checked.in_check === true
        && checked.remaining === remaining
        && checked.moves_remaining === remaining
        && checked.unused_moves === remaining
        && Array.isArray(checked.legal_next)
        && checked.legal_next.length === 0
        && Array.isArray(checked.legal_moves)
        && checked.legal_moves.length === 0
        && exactBoundary(checked.next_state)
        && checked.next_state.fen === checked.board_fen
        && checked.next_state.series === child.series + 1
        && checked.next_state.series_number === child.series + 1;
    };
    const retainedProof = (proof, rootSeries, childDepth) => plainObject(proof)
      && sameStrings(Object.keys(proof).sort(), ["mate_reply", "rooted_path", "schema"])
      && proof.schema === "spc-retained-root-horizon-proof-v1"
      && Array.isArray(proof.rooted_path)
      && proof.rooted_path.length === childDepth + 1
      && proof.rooted_path.length <= 8
      && proof.rooted_path.every(canonicalSeries)
      && contiguousRootedPath(proof.rooted_path)
      && proof.rooted_path[0].machine_notation === rootSeries
      && canonicalSeries(proof.mate_reply)
      && proof.mate_reply.transposition_count === 1
      && proof.mate_reply.outcome === "checkmate"
      && proof.mate_reply.ended_by_check === true;
    const SEARCH_ECHO_KEYS = Object.freeze([
      "session_id", "request_id", "iteration_id", "generation",
      "deadline_monotonic_ms", "remaining_time_ms", "source_fingerprint",
      "kernel_sha256", "module_js_sha256", "certificate_id", "runtime_variant",
      "thread_count", "engine_version", "ruleset_version", "profile_id",
      "safety_revision", "incumbent_epoch", "task_id", "enumeration_identity",
      "candidate_identity", "order_index", "order_key", "purpose", "mate_score",
      "child_depth", "alpha", "beta", "tt_persistence", "mover",
    ]);
    const proofSetByRequest = new Map();
    const requestByProofSet = new Map();
    const exactHorizonResearch = (entry, expected, { requireNewestHit, positiveWork }) => {
      const request = entry?.request;
      const response = entry?.response;
      const proofs = request?.horizon_proofs;
      if (
        entry?.ok !== true
        || entry.worker?.channel_id === null
        || request?.schema !== "spc-root-horizon-research-task-v1"
        || !rootIdentityMatches(request)
        || response?.schema !== "spc-root-horizon-research-result-v1"
        || response.abi_version !== 2
        || !rootIdentityMatches(response)
        || response.product_publishable !== false
        || response.safety_certified !== false
        || response.status !== "complete"
        || response.bound !== "exact"
        || !exactInteger(
          response.memory_bytes,
          1,
          preflight.memory_limits?.maximum_bytes,
        )
        || !exactInteger(
          response.memory_peak_bytes,
          response.memory_bytes,
          preflight.memory_limits?.maximum_bytes,
        )
        || response.session_id !== request.session_id
        || SEARCH_ECHO_KEYS.some((key) => response[key] !== request[key])
        || request.iteration_id !== request.request_id + ":d5"
        || !exactInteger(request.generation, 1)
        || typeof request.task_id !== "string"
        || !request.task_id
        || typeof request.enumeration_identity !== "string"
        || !request.enumeration_identity
        || typeof request.candidate_identity !== "string"
        || !request.candidate_identity
        || !exactInteger(request.order_index, 0, 31)
        || typeof request.order_key !== "string"
        || !request.order_key
        || request.purpose !== "horizon-research"
        || request.mate_score !== MATE_SCORE
        || request.child_depth !== 4
        || request.alpha !== -2 * request.mate_score
        || request.beta !== 2 * request.mate_score
        || request.tt_persistence !== "commit"
        || request.mover !== "white"
        || !exactDeadline(entry)
        || !fullWorkAccounting(entry, { positive: positiveWork })
        || !Array.isArray(proofs)
        || proofs.length < 1
        || proofs.length > 16
        || proofs.some((proof) => !retainedProof(proof, expected.root_series, 4))
        || new Set(proofs.map((proof) => JSON.stringify(proof))).size !== proofs.length
        || !canonicalSeries(response.root_series)
        || response.root_series.machine_notation !== expected.root_series
        || !sameSeries(response.root_series, proofs.at(-1).rooted_path[0])
        || !Array.isArray(response.child_pv)
        || response.child_pv.length > request.child_depth
        || response.child_pv.some((series) => !canonicalSeries(series))
        || pathSignature([response.root_series, ...response.child_pv])
          === pathSignature(proofs.at(-1).rooted_path)
        || !Number.isSafeInteger(response.score)
        || Math.abs(response.score) >= 2 * request.mate_score
        || !Array.isArray(response.proof_bounds)
        || response.proof_bounds.length !== 2
        || response.proof_bounds.some((bound) => ![-1, 0, 1].includes(bound))
        || response.configured_max_depth !== 5
        || response.horizon_proofs_validated !== proofs.length
        || !exactInteger(response.horizon_proof_hits, 0, proofs.length)
        || !exactInteger(response.horizon_proof_hit_mask, 0, 0xffff)
        || response.horizon_proof_hit_mask >= 2 ** proofs.length
        || bitCount16(response.horizon_proof_hit_mask) !== response.horizon_proof_hits
        || (response.horizon_proof_hits === 0)
          !== (response.horizon_proof_hit_mask === 0)
        || (
          requireNewestHit
          && (response.horizon_proof_hit_mask & (2 ** (proofs.length - 1))) === 0
        )
      ) return false;
      const setPrefix = "spc-horizon-proof-set-v1|candidate"
        + request.candidate_identity.length + ":" + request.candidate_identity
        + "|proofs" + proofs.length + ":";
      if (
        typeof response.horizon_proof_set_identity !== "string"
        || !response.horizon_proof_set_identity.startsWith(setPrefix)
        || response.horizon_proof_set_identity.length <= setPrefix.length
      ) return false;
      const proofRequestKey = request.candidate_identity + "\u0000" + JSON.stringify(proofs);
      const priorSet = proofSetByRequest.get(proofRequestKey);
      const priorRequest = requestByProofSet.get(response.horizon_proof_set_identity);
      if (
        (priorSet !== undefined && priorSet !== response.horizon_proof_set_identity)
        || (priorRequest !== undefined && priorRequest !== proofRequestKey)
      ) return false;
      proofSetByRequest.set(proofRequestKey, response.horizon_proof_set_identity);
      requestByProofSet.set(response.horizon_proof_set_identity, proofRequestKey);
      return true;
    };
    const matchesExpectedSafety = (entry, expected) => entry?.request
      ?.authoritative_child_boundary?.series === 6
      && entry.request.candidate?.root_series?.machine_notation
        === expected.unsafe_horizon
      && entry.request.authoritative_child_boundary.fen === expected.unsafe_child_fen
      && exactPrefixMateReplay(entry);
    const exactRepairPair = (safety, repair, expected) => {
      const newestProof = repair?.request?.horizon_proofs?.at(-1);
      const rawMate = safety?.response?.reply_mate;
      const checkedMate = rawMate?.checked_prefix;
      const proofMate = newestProof?.mate_reply;
      const rootedPath = newestProof?.rooted_path;
      const rawCandidate = safety?.request?.candidate;
      const rawChildPv = rawCandidate?.child_pv;
      return exactHorizonResearch(
        repair,
        expected,
        { requireNewestHit: true, positiveWork: true },
      )
        && repair.request_sequence > safety.request_sequence
        && repair.posted_monotonic_ms >= safety.received_monotonic_ms
        && repair.worker === safety.worker
        && repair.worker.channel_id === safety.worker.channel_id
        && repair.worker.channel_id === safety.request.candidate.owner_worker_id
        && repair.request.session_id === safety.request.session_id
        && repair.request.request_id === safety.request.request_id
        && repair.request.iteration_id === safety.request.iteration_id
        && repair.request.generation === safety.request.generation
        && repair.request.safety_revision === safety.request.safety_revision + 1
        && repair.request.incumbent_epoch === safety.request.incumbent_epoch
        && repair.request.deadline_monotonic_ms === safety.request.deadline_monotonic_ms
        && repair.request.deadline_epoch_ms === safety.request.deadline_epoch_ms
        && repair.request.remaining_time_ms <= safety.request.remaining_time_ms
        && repair.request.candidate_identity === safety.request.candidate_identity
        && rawCandidate.candidate_identity === safety.request.candidate_identity
        && repair.request.order_index === rawCandidate.order_index
        && repair.request.order_key === rawCandidate.order_key
        && repair.request.candidate_identity === rawCandidate.candidate_identity
        && rawCandidate.owner_worker_id === safety.worker.channel_id
        && rawCandidate.terminal === false
        && Number.isSafeInteger(rawCandidate.score)
        && Array.isArray(rawCandidate.proof_bounds)
        && rawCandidate.proof_bounds.length === 2
        && rawCandidate.proof_bounds.every((bound) => [-1, 0, 1].includes(bound))
        && Array.isArray(rawChildPv)
        && rawChildPv.length === repair.request.child_depth
        && rawChildPv.every(canonicalSeries)
        && Array.isArray(rootedPath)
        && rootedPath.length === rawChildPv.length + 1
        && rootedPath[0].machine_notation === expected.root_series
        && rootedPath.slice(1).every((series, index) => (
          sameSeries(series, rawChildPv[index])
        ))
        && sameSeries(rawCandidate.root_series, rawChildPv.at(-1))
        && sameSeries(rootedPath.at(-1), safety.request.candidate.root_series)
        && sameBoundary(
          safety.request.authoritative_root_replay.boundary_state,
          rootedPath.at(-2).child_boundary,
        )
        && sameBoundary(
          rootedPath.at(-1).child_boundary,
          safety.request.authoritative_child_boundary,
        )
        && canonicalSeries(proofMate)
        && sameStrings(proofMate.moves, rawMate.moves)
        && proofMate.machine_notation === rawMate.machine_notation
        && proofMate.transposition_count === 1
        && proofMate.outcome === rawMate.outcome
        && proofMate.ended_by_check === rawMate.ended_by_check
        && sameBoundary(proofMate.child_boundary, checkedMate.next_state);
    };
    const repairWitnesses = EXPECTED_WITNESSES.map((expected) => {
      for (const safety of safetyTrace) {
        if (!matchesExpectedSafety(safety, expected)) continue;
        for (const repair of horizonResearchTrace) {
          if (exactRepairPair(safety, repair, expected)) {
            return Object.freeze({ expected, safety, repair });
          }
        }
      }
      return null;
    });
    const exactWarmRecertification = (entry, witness) => {
      const repair = witness.repair;
      return entry.request_sequence > repair.request_sequence
        && entry.posted_monotonic_ms >= repair.received_monotonic_ms
        && entry.worker === repair.worker
        && entry.worker.channel_id === repair.worker.channel_id
        && entry.request?.session_id === repair.request.session_id
        && entry.request?.request_id === repair.request.request_id
        && entry.request?.iteration_id === repair.request.iteration_id
        && entry.request?.generation === repair.request.generation
        && entry.request?.deadline_monotonic_ms === repair.request.deadline_monotonic_ms
        && entry.request?.deadline_epoch_ms === repair.request.deadline_epoch_ms
        && entry.request?.remaining_time_ms <= repair.request.remaining_time_ms
        && entry.request?.enumeration_identity === repair.request.enumeration_identity
        && entry.request?.candidate_identity === repair.request.candidate_identity
        && entry.request?.order_index === repair.request.order_index
        && entry.request?.order_key === repair.request.order_key
        && entry.request?.task_id !== repair.request.task_id
        && entry.request?.safety_revision === repair.request.safety_revision
        && exactInteger(
          entry.request?.incumbent_epoch,
          repair.request.incumbent_epoch,
          repair.request.incumbent_epoch + 1,
        )
        && sameJson(entry.request?.horizon_proofs, repair.request.horizon_proofs)
        && exactHorizonResearch(
          entry,
          witness.expected,
          { requireNewestHit: false, positiveWork: false },
        )
        && entry.response.horizon_proof_set_identity
          === repair.response.horizon_proof_set_identity
        && entry.response.horizon_proof_hits === 0
        && entry.response.horizon_proof_hit_mask === 0
        && exactInteger(entry.response.work.call_stats?.tt_hits, 1)
        && entry.response.score === repair.response.score
        && sameSeries(entry.response.root_series, repair.response.root_series)
        && sameJson(entry.response.child_pv, repair.response.child_pv);
    };
    const finalRootSeries = Array.isArray(result.best_full_series)
      ? result.best_full_series.join("/")
      : null;
    const f3Witness = repairWitnesses[0];
    const b3Witness = repairWitnesses[1];
    const warmRecertifications = repairWitnesses.map((witness) => {
      if (witness === null || witness.expected.root_series !== finalRootSeries) return null;
      return horizonResearchTrace.find((entry) => exactWarmRecertification(entry, witness))
        || null;
    });
    const exactRepairPolicy = (value) => plainObject(value)
      && sameStrings(
        Object.keys(value).sort(),
        ["maximum_successful_same_root_repairs", "schema"],
      )
      && value.schema === SAME_ROOT_REPAIR_POLICY_SCHEMA
      && value.maximum_successful_same_root_repairs
        === MAXIMUM_SUCCESSFUL_SAME_ROOT_REPAIRS;
    const exactPolicyVeto = (value, candidateIdentity) => plainObject(value)
      && sameStrings(
        Object.keys(value).sort(),
        [
          "candidate_identity", "distinct_proofs_observed",
          "maximum_successful_same_root_repairs", "reason",
          "repairs_before_veto", "retained_proofs_before_veto", "schema",
        ],
      )
      && value.schema === POLICY_VETO_SCHEMA
      && value.candidate_identity === candidateIdentity
      && value.reason === "same-root-repair-limit"
      && value.maximum_successful_same_root_repairs
        === MAXIMUM_SUCCESSFUL_SAME_ROOT_REPAIRS
      && value.repairs_before_veto === MAXIMUM_SUCCESSFUL_SAME_ROOT_REPAIRS
      && value.retained_proofs_before_veto === MAXIMUM_SUCCESSFUL_SAME_ROOT_REPAIRS
      && value.distinct_proofs_observed === 2;
    const retainedProofFromSafety = (entry, rootSeries) => {
      const candidate = entry?.request?.candidate;
      const mate = entry?.response?.reply_mate;
      const checked = mate?.checked_prefix;
      if (
        !canonicalSeries(rootSeries)
        || !Array.isArray(candidate?.child_pv)
        || candidate.child_pv.length !== 4
        || !candidate.child_pv.every(canonicalSeries)
        || !sameSeries(candidate.root_series, candidate.child_pv.at(-1))
        || !plainObject(mate)
        || !exactBoundary(checked?.next_state)
      ) return null;
      const proof = {
        schema: "spc-retained-root-horizon-proof-v1",
        rooted_path: [rootSeries, ...candidate.child_pv],
        mate_reply: {
          child_boundary: checked.next_state,
          ended_by_check: mate.ended_by_check,
          machine_notation: mate.machine_notation,
          moves: [...mate.moves],
          outcome: mate.outcome,
          transposition_count: 1,
        },
      };
      return retainedProof(proof, rootSeries.machine_notation, 4) ? proof : null;
    };
    const sameRootRepairPolicy = result.same_root_repair_policy;
    const policyVetoes = result.pv_horizon_policy_vetoes;
    const f3FirstProof = f3Witness?.repair?.request?.horizon_proofs?.at(-1) || null;
    const f3RootSeries = f3FirstProof?.rooted_path?.[0] || null;
    const f3FirstProofSha256 = f3FirstProof === null
      ? null
      : await canonicalSha256(f3FirstProof);
    let secondF3Safety = null;
    let secondF3ProofSha256 = null;
    if (f3Witness !== null && f3FirstProof !== null && canonicalSeries(f3RootSeries)) {
      for (const entry of safetyTrace) {
        if (
          entry.request_sequence <= f3Witness.repair.request_sequence
          || entry.posted_monotonic_ms < f3Witness.repair.received_monotonic_ms
          || entry.worker !== f3Witness.repair.worker
          || entry.request?.session_id !== f3Witness.repair.request.session_id
          || entry.request?.request_id !== f3Witness.repair.request.request_id
          || entry.request?.iteration_id !== f3Witness.repair.request.iteration_id
          || entry.request?.generation !== f3Witness.repair.request.generation
          || entry.request?.deadline_monotonic_ms
            !== f3Witness.repair.request.deadline_monotonic_ms
          || entry.request?.deadline_epoch_ms !== f3Witness.repair.request.deadline_epoch_ms
          || entry.request?.candidate_identity
            !== f3Witness.repair.request.candidate_identity
          || entry.request?.candidate?.candidate_identity
            !== f3Witness.repair.request.candidate_identity
          || entry.request?.candidate?.order_index !== f3Witness.repair.request.order_index
          || entry.request?.candidate?.order_key !== f3Witness.expected.root_series
          || entry.request?.safety_revision !== f3Witness.repair.request.safety_revision
          || !exactInteger(
            entry.request?.incumbent_epoch,
            f3Witness.repair.request.incumbent_epoch,
          )
          || entry.request?.remaining_time_ms > f3Witness.repair.request.remaining_time_ms
          || !exactPrefixMateReplay(entry)
        ) continue;
        const derivedProof = retainedProofFromSafety(entry, f3RootSeries);
        if (derivedProof === null) continue;
        const derivedSha256 = await canonicalSha256(derivedProof);
        if (derivedSha256 === f3FirstProofSha256) continue;
        secondF3Safety = entry;
        secondF3ProofSha256 = derivedSha256;
        break;
      }
    }
    const f3CandidateIdentity = f3Witness?.repair?.request?.candidate_identity || null;
    const f3PolicyVeto = Array.isArray(policyVetoes)
      ? policyVetoes.find((value) => exactPolicyVeto(value, f3CandidateIdentity)) || null
      : null;
    const f3ProofCount2ResearchDispatched = f3CandidateIdentity !== null
      && horizonResearchTrace.some((entry) => (
        entry.request?.candidate_identity === f3CandidateIdentity
        && Array.isArray(entry.request?.horizon_proofs)
        && entry.request.horizon_proofs.length >= 2
      ));
    const thresholdVetoWitness = secondF3Safety === null || f3PolicyVeto === null
      ? null
      : Object.freeze({
        schema: THRESHOLD_VETO_WITNESS_SCHEMA,
        root_series: "f2f3",
        candidate_identity: f3CandidateIdentity,
        first_repair_request_sequence: f3Witness.repair.request_sequence,
        second_safety_request_sequence: secondF3Safety.request_sequence,
        first_proof_sha256: f3FirstProofSha256,
        second_proof_sha256: secondF3ProofSha256,
        proof_count_2_research_dispatched: f3ProofCount2ResearchDispatched,
        policy_veto: f3PolicyVeto,
        second_safety_trace: secondF3Safety,
      });
    const workerCount = result.runtime_receipt?.worker_count;
    const mainWorkerCalls = workerFactoryCalls.filter(
      (worker) => worker.name === "scottish-progressive-engine",
    );
    const rootWorkerCalls = workerFactoryCalls.filter(
      (worker) => worker.channel_id !== null,
    );
    const expectedRootWorkerNames = exactInteger(workerCount, 1, 8)
      ? Array.from(
        { length: workerCount },
        (_, index) => "scottish-progressive-root-root-" + index,
      )
      : [];
    const factoryUseBound = mainWorkerCalls.length === 1
      && workerFactoryCalls[0]?.name === "scottish-progressive-engine"
      && rootWorkerCalls.length === workerCount
      && sameStrings(
        rootWorkerCalls.map((worker) => worker.name).sort(),
        expectedRootWorkerNames.sort(),
      )
      && workerFactoryCalls.length === workerCount + 1
      && workerFactoryCalls.every((worker) => (
        worker.url === expectedWorkerUrl.href && worker.type === "module"
      ));
    const lineRejections = result.pv_horizon_line_rejections;
    const nativeRepairs = result.pv_horizon_native_repairs;
    const candidateVetoes = result.pv_horizon_candidate_vetoes;
    const checks = {
      authenticity_scope_is_local_checkout: AUTHENTICITY_SCOPE
        === "local-checkout-hash-bound-unsigned-v1",
      standalone_signature_not_claimed: manifest.signature === undefined,
      local_origin_is_loopback: location.protocol === "http:"
        && location.hostname === "127.0.0.1",
      evaluated_page_is_opera: pageEnvironment.userAgent.includes(" OPR/"),
      local_assets_are_sha256_bound: Object.values(localAssets).every((asset) => (
        SHA256.test(asset.sha256) && asset.byte_length > 0
      ))
        && SHA256.test(manifestAsset.sha256)
        && SHA256.test(localCheckoutAssetSetSha256),
      native_worker_factory_is_bound: factoryUseBound,
      no_synthetic_worker_events: syntheticWorkerEvents === 0,
      manifest_preflight_identity_bound: manifestPreflightBound,
      result_identity_bound: resultIdentityBound,
      local_wasm_preflight: preflight.ready === true,
      completed_depth_5: result.requested_depth === 5
        && result.completed_depth === 5,
      publishable: result.publishable === true,
      selected_safety_certified: result.safety_certified === true,
      compiled_replay_is_authoritative: result.legal_series_certified === true
        && result.authoritative_replay_certified === true
        && result.legal_validation_runtime === "compiled-wasm",
      policy_is_explicit: result.selection_policy
        === CHECKED_PV_SELECTION_POLICY,
      repair_once_then_veto_policy_bound: lineRejections === 3
        && nativeRepairs === 2
        && candidateVetoes === 1
        && nativeRepairs + candidateVetoes === lineRejections
        && result.selection_policy_filtered === true
        && result.root_bound_coverage_scope === "selection-eligible-candidates"
        && exactRepairPolicy(sameRootRepairPolicy)
        && Array.isArray(policyVetoes)
        && policyVetoes.length === 1
        && f3PolicyVeto === policyVetoes[0]
        && result.runtime_receipt?.pv_horizon_line_rejections === lineRejections
        && result.runtime_receipt?.pv_horizon_native_repairs === nativeRepairs
        && result.runtime_receipt?.pv_horizon_candidate_vetoes === candidateVetoes
        && result.runtime_receipt?.selection_policy === result.selection_policy
        && result.runtime_receipt?.selection_policy_filtered
          === result.selection_policy_filtered
        && canonicalJson(result.runtime_receipt?.same_root_repair_policy)
          === canonicalJson(sameRootRepairPolicy)
        && canonicalJson(result.runtime_receipt?.pv_horizon_policy_vetoes)
          === canonicalJson(policyVetoes)
        && result.stats?.pv_horizon_line_rejections === lineRejections
        && result.stats?.pv_horizon_native_repairs === nativeRepairs
        && result.stats?.pv_horizon_candidate_vetoes === candidateVetoes,
      f3_exact_raw_mate_and_same_root_repair: f3Witness !== null,
      f3_second_distinct_proof_vetoed_without_research: thresholdVetoWitness !== null
        && thresholdVetoWitness.first_proof_sha256
          !== thresholdVetoWitness.second_proof_sha256
        && thresholdVetoWitness.proof_count_2_research_dispatched === false,
      b3_exact_raw_mate_and_same_root_repair: b3Witness !== null,
      repaired_candidates_are_distinct: f3Witness !== null
        && b3Witness !== null
        && f3Witness.repair.request.candidate_identity
          !== b3Witness.repair.request.candidate_identity
        && f3Witness.repair.response.root_series.machine_notation === "f2f3"
        && b3Witness.repair.response.root_series.machine_notation === "b2b3",
      newest_request_order_proofs_hit: f3Witness !== null
        && b3Witness !== null
        && preflight.root_session_contract?.horizon_research?.hit_mask_order
          === "request-order",
      selected_b3_after_f3_policy_veto: finalRootSeries === "b2b3"
        && f3PolicyVeto !== null,
      final_repaired_winner_warm_recertified: repairWitnesses.every(
        (witness, index) => witness === null
          || witness.expected.root_series !== finalRootSeries
          || warmRecertifications[index] !== null,
      ),
      global_work_respected: exactInteger(
        result.work,
        1,
        payload.max_generation_positions,
      )
        && result.runtime_receipt?.work === result.work
        && result.stats?.generation_positions === result.work,
      no_interruption: result.timed_out === false
        && result.work_limit_reached === false,
      deadline_respected: elapsedSeconds < 60
        && repairWitnesses.every((witness) => witness === null
          || witness.repair.received_monotonic_ms < searchDeadline),
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
          pv_horizon_native_repairs: result.pv_horizon_native_repairs,
          pv_horizon_candidate_vetoes: result.pv_horizon_candidate_vetoes,
          work: result.work,
          timed_out: result.timed_out,
          work_limit_reached: result.work_limit_reached,
        },
        workerFactoryCalls,
        safetyTrace,
        horizonResearchTrace,
      }));
    }
    return {
      schema: "spc-opera-checked-pv-horizon-receipt-v4",
      status: "passed-not-certified",
      product_publishable: false,
      safety_certified: false,
      certificate_id: null,
      authenticity: {
        scope: AUTHENTICITY_SCOPE,
        standalone_signature_verified: false,
        limitation: "This receipt authenticates one loopback-served local checkout by observed asset hashes; it is not portable proof without a signed asset manifest.",
        local_origin: location.origin,
        local_checkout_asset_set_sha256: localCheckoutAssetSetSha256,
        manifest: {
          url: manifestAsset.url,
          byte_length: manifestAsset.byte_length,
          sha256: manifestAsset.sha256,
        },
        assets: localAssets,
        worker_factory_calls: workerFactoryCalls,
        trusted_worker_events_only: syntheticWorkerEvents === 0,
      },
      page_environment: pageEnvironment,
      manifest_binding: manifestBinding,
      preflight_identity: preflightIdentity,
      result_summary: resultSummary,
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
      pv_horizon_native_repairs: result.pv_horizon_native_repairs,
      pv_horizon_candidate_vetoes: result.pv_horizon_candidate_vetoes,
      same_root_repair_policy: sameRootRepairPolicy,
      pv_horizon_policy_vetoes: policyVetoes,
      threshold_veto_witness: thresholdVetoWitness,
      horizon_safety_traces: {
        f3: f3Witness?.safety,
        b3: b3Witness?.safety,
      },
      horizon_research_traces: horizonResearchTrace,
      certified_repair_traces: {
        f3: f3Witness?.repair,
        b3: b3Witness?.repair,
      },
      final_winner_warm_recertification: {
        f3: warmRecertifications[0],
        b3: warmRecertifications[1],
      },
      runtime_receipt: result.runtime_receipt,
      stats: result.stats,
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
    if (!payload || payload.status !== "passed-not-certified") {
      throw new Error(
        "Opera checked-PV probe did not return a passing receipt: "
          + JSON.stringify(payload),
      );
    }
    const normalizedRequestedUrl = new URL(args.url).href;
    if (
      payload.page_environment?.location !== normalizedRequestedUrl
      || payload.page_environment?.userAgent !== version["User-Agent"]
      || !String(version["User-Agent"] || "").includes(" OPR/")
      || !String(version.Browser || "").startsWith("Chrome/")
    ) {
      throw new Error("Opera CDP and evaluated page identities do not match");
    }
    const receipt = {
      ...payload,
      cdp: {
        browser: version.Browser,
        protocol_version: version["Protocol-Version"],
        user_agent: version["User-Agent"],
      },
      page_url: normalizedRequestedUrl,
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
