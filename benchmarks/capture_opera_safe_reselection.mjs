import { writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";


function argumentsOf(argv) {
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    if (!argv[index]?.startsWith("--") || argv[index + 1] === undefined) {
      throw new Error(`invalid argument near ${String(argv[index])}`);
    }
    values.set(argv[index], argv[index + 1]);
  }
  for (const required of [
    "--endpoint", "--url", "--candidate-receipt-url", "--output",
  ]) {
    if (!values.has(required)) throw new Error(`missing ${required}`);
  }
  return {
    endpoint: values.get("--endpoint").replace(/\/$/, ""),
    url: values.get("--url"),
    candidateReceiptUrl: values.get("--candidate-receipt-url"),
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


export function validateAuthoritativeAssetBindings(
  authority,
  observed,
  expectedSourceRevision,
) {
  const SHA256 = /^[0-9a-f]{64}$/;
  const fail = (message) => {
    throw new Error(`authoritative browser-runtime asset binding failed: ${message}`);
  };
  if (
    !authority
    || typeof authority !== "object"
    || Array.isArray(authority)
    || authority.schema !== "spc-browser-runtime-asset-set-v1"
    || authority.source_revision !== expectedSourceRevision
    || !SHA256.test(String(authority.artifact_set_sha256 || ""))
    || !Array.isArray(authority.files)
    || !Array.isArray(observed)
  ) fail("authority envelope is invalid");
  const normalize = (record, label) => {
    if (
      !record
      || typeof record !== "object"
      || Array.isArray(record)
      || typeof record.label !== "string"
      || !record.label
      || typeof record.path !== "string"
      || !/^[A-Za-z0-9._/-]+$/.test(record.path)
      || record.path.startsWith("/")
      || record.path.includes("..")
      || !SHA256.test(String(record.sha256 || ""))
      || !Number.isSafeInteger(record.bytes)
      || record.bytes < 1
    ) fail(`${label} contains an invalid record`);
    return {
      label: record.label,
      path: record.path,
      sha256: record.sha256,
      bytes: record.bytes,
    };
  };
  const expected = authority.files.map((record) => normalize(record, "authority"));
  const actual = observed.map((record) => normalize(record, "observation"));
  const expectedLabels = new Set(expected.map((record) => record.label));
  const actualLabels = new Set(actual.map((record) => record.label));
  if (
    expectedLabels.size !== expected.length
    || actualLabels.size !== actual.length
    || expectedLabels.size !== actualLabels.size
    || [...expectedLabels].some((label) => !actualLabels.has(label))
  ) fail("authority and observation labels differ");
  const actualByLabel = new Map(actual.map((record) => [record.label, record]));
  for (const expectedRecord of expected) {
    const actualRecord = actualByLabel.get(expectedRecord.label);
    if (actualRecord.path !== expectedRecord.path) {
      fail(`${expectedRecord.label} path mismatch`);
    }
    if (actualRecord.sha256 !== expectedRecord.sha256) {
      fail(`${expectedRecord.label} SHA-256 mismatch`);
    }
    if (actualRecord.bytes !== expectedRecord.bytes) {
      fail(`${expectedRecord.label} byte-length mismatch`);
    }
  }
  return true;
}


async function browserProbe(candidateReceiptHref, validateAssetBindings) {
  const SHA256 = /^[0-9a-f]{64}$/;
  const GIT_REVISION = /^[0-9a-f]{40,64}$/;
  const SOURCE_FINGERPRINT = /^[0-9a-f]{16}$/;
  const ROOT_CERTIFICATE_ID = /^spc-root-session-[0-9a-f]{16}$/;
  const MATE_CERTIFICATE_ID = /^spc-mate-[0-9a-f]{16}$/;
  const PREFIX_CERTIFICATE_ID = /^spc-prefix-[0-9a-f]{16}$/;
  const CANDIDATE_ID = /^spc-browser-wasm-candidate-[0-9a-f]{16}$/;
  const UCI_MOVE = /^[a-h][1-8][a-h][1-8][qrbn]?$/;
  const ARTIFACT_IDENTITY_KEYS = Object.freeze([
    "artifact_set_sha256", "kernel_sha256", "module_js_sha256",
    "source_fingerprint", "source_revision", "wasm_sha256",
  ]);
  const EXPECTED_MOVES = Object.freeze([
    "f2e2", "d2d4", "c1g5", "g5d8", "d8e7",
  ]);
  const EXPECTED_MACHINE_NOTATION = EXPECTED_MOVES.join("/");
  const EXPECTED_CHILD = Object.freeze({
    fen: "rnb1kb1r/ppppB1pp/4p3/5p2/3P4/5P1N/PPP1K1PP/RN1n1B1R b kq - 1 7",
    board_fen: "rnb1kb1r/ppppB1pp/4p3/5p2/3P4/5P1N/PPP1K1PP/RN1n1B1R b kq - 1 7",
    series: 6,
    series_number: 6,
    side_to_move: "black",
    quiet_series: 0,
    quiet_draw_pending: false,
    ep_targets: Object.freeze([]),
    progressive_ep: Object.freeze([]),
    promoted_hex: "0000000000000000",
    chess960: false,
  });
  const EXPECTED_CHILD_PFEN =
    EXPECTED_CHILD.fen
    + " | series=6 quiet=0 progressive_ep=- rules=scottish-modern-common-v1"
    + " quiet_draw=manual-proof-required";
  const EXPECTED_POSITION_HASH = "c3504ae0c86022bb9c79b0ed8a89361c";
  const EXPECTED_CHILD_BOUNDARY_SHA256 =
    "e3c72990d4e0613e1b7b3d91fe213c2cf823cc452f7f601c4cd4aaf9d552b0f6";
  const EXPECTED_GENERATION_WORK = 9_213;
  const EXPECTED_SELECTED_WORK = 7_276_223;
  const EXPECTED_LANE_WORK = 39_737_928;
  const SAFE_RESELECT_WIDTH = 512;
  const SAFE_RESELECT_TOTAL_WORK = 40_000_000;
  const SAFE_RESELECT_EARLY_COUNT = 32;
  const SAFE_RESELECT_EARLY_CHILD_WORK = 3_000_000;
  const SAFE_RESELECT_WIDENED_CHILD_WORK = 10_000_000;
  const EXPECTED_RUNTIME_ASSET_PATHS = Object.freeze({
    page_document: "index.html",
    page_styles: "styles.css",
    study_safety: "study-safety.js",
    evaluation_format: "evaluation-format.js",
    play_handoff: "play-handoff.js",
    play_timeline: "play-timeline.js",
    browser_engine_worker: "browser-engine-worker.js",
    browser_engine_client: "browser-engine-client.js",
    browser_prefix_contract: "browser-prefix-contract.js",
    browser_root_iteration_client: "browser-root-iteration-client.js",
    root_iteration_coordinator: "root-iteration-coordinator.js",
    wasm_kernel_adapter: "wasm-kernel-adapter.js",
    board_renderer: "board-renderer.js",
    page_application: "app.js",
  });
  const sameStrings = (left, right) => Array.isArray(left)
    && Array.isArray(right)
    && left.length === right.length
    && left.every((value, index) => value === right[index]);
  const plainObject = (value) => Boolean(
    value && typeof value === "object" && !Array.isArray(value)
  );
  const exactInteger = (
    value,
    minimum = 0,
    maximum = Number.MAX_SAFE_INTEGER,
  ) => Number.isSafeInteger(value) && value >= minimum && value <= maximum;
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const canonicalJson = (value) => {
    if (value === null || typeof value === "string" || typeof value === "boolean") {
      return JSON.stringify(value);
    }
    if (typeof value === "number" && Number.isFinite(value)) {
      return JSON.stringify(value);
    }
    if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
    if (plainObject(value)) {
      return "{" + Object.keys(value).sort().map((key) => (
        JSON.stringify(key) + ":" + canonicalJson(value[key])
      )).join(",") + "}";
    }
    throw new Error("receipt identity contains a non-canonical JSON value");
  };
  const sameJson = (left, right) => canonicalJson(left) === canonicalJson(right);
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
  const strictJson = (asset, label) => {
    try {
      return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(asset.bytes));
    } catch {
      throw new Error(label + " is not strict UTF-8 JSON");
    }
  };
  const publicAsset = (asset) => Object.freeze({
    url: asset.url,
    byte_length: asset.byte_length,
    sha256: asset.sha256,
  });

  if (location.protocol !== "http:" || location.hostname !== "127.0.0.1") {
    throw new Error("the safe-reselection receipt is restricted to loopback bytes");
  }
  const candidateReceiptUrl = new URL(candidateReceiptHref, location.href);
  if (
    candidateReceiptUrl.origin !== location.origin
    || candidateReceiptUrl.protocol !== "http:"
    || candidateReceiptUrl.hostname !== "127.0.0.1"
  ) {
    throw new Error("the candidate receipt must be served from the page loopback origin");
  }
  const pageEnvironment = Object.freeze({
    location: location.href,
    user_agent: String(navigator.userAgent || ""),
    hardware_concurrency: navigator.hardwareConcurrency,
    cross_origin_isolated: globalThis.crossOriginIsolated === true,
  });
  if (
    !pageEnvironment.user_agent.includes(" OPR/")
    || !exactInteger(pageEnvironment.hardware_concurrency, 1, 1_024)
    || typeof globalThis.crossOriginIsolated !== "boolean"
  ) {
    throw new Error("the safe-reselection receipt requires an identified Opera page realm");
  }

  const candidateAsset = await fetchAsset(candidateReceiptUrl, "candidate receipt");
  const candidate = strictJson(candidateAsset, "candidate receipt");
  const artifact = candidate?.artifact;
  const candidateFiles = candidate?.browser_bundle?.files;
  if (
    candidate?.schema !== "spc-browser-wasm-release-candidate-v1"
    || candidate.status !== "staged-for-local-opera-attestation"
    || candidate.product_publishable !== false
    || candidate.safety_certified !== false
    || !CANDIDATE_ID.test(String(candidate.candidate_id || ""))
    || !GIT_REVISION.test(String(candidate.source_revision || ""))
    || !plainObject(artifact)
    || !sameStrings(Object.keys(artifact).sort(), ARTIFACT_IDENTITY_KEYS)
    || artifact.source_revision !== candidate.source_revision
    || !SOURCE_FINGERPRINT.test(String(artifact.source_fingerprint || ""))
    || !SHA256.test(String(artifact.kernel_sha256 || ""))
    || !SHA256.test(String(artifact.wasm_sha256 || ""))
    || !SHA256.test(String(artifact.module_js_sha256 || ""))
    || !SHA256.test(String(artifact.artifact_set_sha256 || ""))
    || candidate.next_required_gate !== "spc-opera-checked-pv-horizon-receipt-v6"
    || candidate?.browser_bundle?.path !== "browser-engine"
    || !Array.isArray(candidateFiles)
    || candidateFiles.length < 3
    || !SHA256.test(String(candidate.browser_bundle.artifact_set_sha256 || ""))
  ) {
    throw new Error("candidate receipt does not describe staged core-seven bytes");
  }
  const releaseTag = new URL(location.href).searchParams.get("release") || "";
  if (
    releaseTag.length < 7
    || !/^[0-9a-f]+$/.test(releaseTag)
    || !candidate.source_revision.startsWith(releaseTag)
  ) {
    throw new Error("page release query is not a prefix of the candidate source revision");
  }
  const normalizedCandidateFiles = candidateFiles.map((record) => {
    if (
      !plainObject(record)
      || typeof record.path !== "string"
      || !/^[A-Za-z0-9._/-]+$/.test(record.path)
      || record.path.startsWith("/")
      || record.path.includes("..")
      || !SHA256.test(String(record.sha256 || ""))
      || !exactInteger(record.bytes, 1)
    ) throw new Error("candidate bundle contains an invalid file record");
    return { path: record.path, sha256: record.sha256, bytes: record.bytes };
  });
  const sortedCandidateFiles = [...normalizedCandidateFiles].sort(
    (left, right) => left.path.localeCompare(right.path),
  );
  if (
    !sameJson(normalizedCandidateFiles, sortedCandidateFiles)
    || await canonicalSha256(normalizedCandidateFiles)
      !== candidate.browser_bundle.artifact_set_sha256
  ) throw new Error("candidate browser bundle file-set identity is invalid");

  const manifestUrl = new URL("./engine/browser-engine-manifest.json", location.href);
  const manifestAsset = await fetchAsset(manifestUrl, "browser engine manifest");
  const manifest = strictJson(manifestAsset, "browser engine manifest");
  const variant = manifest?.variants?.single;
  const rootCertificate = variant?.root_session_certificate;
  const mateCertificate = variant?.mate_certificate;
  const prefixCertificate = variant?.prefix_certificate;
  if (
    manifest?.schema !== "spc-browser-wasm-manifest-v1"
    || manifest.contract_version !== 1
    || manifest.abi_version !== 1
    || manifest.source_fingerprint !== artifact.source_fingerprint
    || !plainObject(manifest.variants)
    || !sameStrings(Object.keys(manifest.variants).sort(), ["single"])
    || !plainObject(variant)
    || variant.thread_count !== 1
    || !Array.isArray(variant.support_files)
    || variant.support_files.length !== 0
    || !/^[A-Za-z0-9._-]+$/.test(String(variant.module_js || ""))
    || !/^[A-Za-z0-9._-]+$/.test(String(variant.wasm || ""))
    || variant.module_js_sha256 !== artifact.module_js_sha256
    || variant.wasm_sha256 !== artifact.wasm_sha256
    || variant.kernel_sha256 !== artifact.kernel_sha256
    || rootCertificate?.schema !== "spc-root-session-certificate-v1"
    || rootCertificate.status !== "certified"
    || rootCertificate.root_session_certified !== true
    || rootCertificate.product_publishable !== false
    || !ROOT_CERTIFICATE_ID.test(String(rootCertificate.certificate_id || ""))
    || mateCertificate?.schema !== "spc-series-mate-certificate-v1"
    || mateCertificate.status !== "certified"
    || mateCertificate.mate_capability_certified !== true
    || mateCertificate.reply_mate_safety !== true
    || mateCertificate.product_publishable !== false
    || !MATE_CERTIFICATE_ID.test(String(mateCertificate.certificate_id || ""))
    || prefixCertificate?.status !== "certified"
    || !PREFIX_CERTIFICATE_ID.test(String(prefixCertificate.certificate_id || ""))
    || rootCertificate.source_fingerprint !== manifest.source_fingerprint
    || mateCertificate.source_fingerprint !== manifest.source_fingerprint
    || prefixCertificate.source_fingerprint !== manifest.source_fingerprint
    || rootCertificate.wasm_sha256 !== variant.wasm_sha256
    || mateCertificate.wasm_sha256 !== variant.wasm_sha256
    || prefixCertificate.wasm_sha256 !== variant.wasm_sha256
    || rootCertificate.module_js_sha256 !== variant.module_js_sha256
    || mateCertificate.module_js_sha256 !== variant.module_js_sha256
    || prefixCertificate.module_js_sha256 !== variant.module_js_sha256
    || rootCertificate.kernel_sha256 !== variant.kernel_sha256
    || mateCertificate.kernel_sha256 !== variant.kernel_sha256
    || rootCertificate.runtime_variant !== "single"
    || mateCertificate.runtime_variant !== "single"
    || prefixCertificate.runtime_variant !== "single"
    || rootCertificate.thread_count !== 1
    || mateCertificate.thread_count !== 1
    || prefixCertificate.thread_count !== 1
    || rootCertificate.root_session_contract?.hard_limits?.maximum_width
      !== SAFE_RESELECT_WIDTH
  ) throw new Error("manifest has no artifact-bound W512 root/mate/prefix identity");

  const engineRootUrl = new URL("./single/", manifestUrl);
  const compiledModuleUrl = new URL(variant.module_js, engineRootUrl);
  const compiledWasmUrl = new URL(variant.wasm, engineRootUrl);
  compiledModuleUrl.searchParams.set("sha256", variant.module_js_sha256);
  compiledWasmUrl.searchParams.set("sha256", variant.wasm_sha256);
  const runtimeAuthority = candidate.browser_runtime;
  if (
    !plainObject(runtimeAuthority)
    || runtimeAuthority.schema !== "spc-browser-runtime-asset-set-v1"
    || runtimeAuthority.source_revision !== candidate.source_revision
    || !Array.isArray(runtimeAuthority.files)
    || !SHA256.test(String(runtimeAuthority.artifact_set_sha256 || ""))
    || await canonicalSha256(runtimeAuthority.files)
      !== runtimeAuthority.artifact_set_sha256
  ) throw new Error("candidate has no authoritative browser-runtime asset set");
  const seedReceiptRecords = candidate.evidence_receipts?.map((record) => ({
    label: record?.label,
    sha256: record?.sha256,
  }));
  if (
    !Array.isArray(seedReceiptRecords)
    || seedReceiptRecords.length !== 7
    || new Set(seedReceiptRecords.map((record) => record.label)).size !== 7
    || seedReceiptRecords.some((record) => (
      typeof record.label !== "string"
      || !record.label
      || !SHA256.test(String(record.sha256 || ""))
    ))
    || !plainObject(candidate.policy)
    || !Number.isFinite(candidate.policy.maximum_seconds)
    || !Number.isFinite(candidate.policy.default_seconds)
  ) throw new Error("candidate cannot reproduce its cryptographic seed");
  const candidateSeed = {
    artifact,
    bundle_set_sha256: candidate.browser_bundle.artifact_set_sha256,
    certificate_set_sha256: candidate.certificate_set_sha256,
    browser_runtime_set_sha256: runtimeAuthority.artifact_set_sha256,
    receipts: seedReceiptRecords,
    policy: candidate.policy,
  };
  const derivedCandidateId =
    `spc-browser-wasm-candidate-${(await canonicalSha256(candidateSeed)).slice(0, 16)}`;
  if (candidate.candidate_id !== derivedCandidateId) {
    throw new Error("candidate ID does not commit to its runtime asset set");
  }
  validateAssetBindings(
    runtimeAuthority,
    runtimeAuthority.files,
    candidate.source_revision,
  );
  const runtimePathByLabel = Object.fromEntries(runtimeAuthority.files.map((record) => (
    [record?.label, record?.path]
  )));
  if (!sameJson(runtimePathByLabel, EXPECTED_RUNTIME_ASSET_PATHS)) {
    throw new Error("candidate browser-runtime authority omits an executed page asset");
  }
  const runtimeRecordByLabel = new Map(runtimeAuthority.files.map((record) => (
    [record.label, record]
  )));
  const workerAuthority = runtimeRecordByLabel.get("browser_engine_worker");
  const adapterAuthority = runtimeRecordByLabel.get("wasm_kernel_adapter");
  const workerUrl = new URL("./browser-engine-worker.js", location.href);
  workerUrl.searchParams.set("worker_sha256", workerAuthority.sha256);
  workerUrl.searchParams.set("adapter_sha256", adapterAuthority.sha256);
  const adapterUrl = new URL("./wasm-kernel-adapter.js", location.href);
  adapterUrl.search = workerUrl.search;
  const runtimeAssetEntries = await Promise.all(runtimeAuthority.files.map(
    async (record) => {
      const url = record.label === "page_document"
        ? new URL(location.href)
        : record.label === "browser_engine_worker"
          ? workerUrl
          : record.label === "wasm_kernel_adapter"
            ? adapterUrl
            : new URL(`./${record.path}`, location.href);
      return [record.label, await fetchAsset(url, record.label)];
    },
  ));
  const observedRuntimeRecords = runtimeAssetEntries.map(([label, assetValue]) => ({
    label,
    path: runtimeRecordByLabel.get(label).path,
    sha256: assetValue.sha256,
    bytes: assetValue.byte_length,
  }));
  validateAssetBindings(
    runtimeAuthority,
    observedRuntimeRecords,
    candidate.source_revision,
  );
  const runtimeAssets = Object.fromEntries(runtimeAssetEntries);
  const pageSource = new TextDecoder("utf-8", { fatal: true }).decode(
    runtimeAssets.page_document.bytes,
  );
  const parsedPage = new DOMParser().parseFromString(pageSource, "text/html");
  if (parsedPage.querySelector("parsererror")) {
    throw new Error("the bound page document could not be parsed");
  }
  const pageRoot = new URL("./", location.href);
  const relativePagePath = (value) => {
    const resolved = new URL(value, location.href);
    if (
      resolved.origin !== location.origin
      || !resolved.pathname.startsWith(pageRoot.pathname)
    ) throw new Error("the page executes an asset outside its bound directory");
    return resolved.pathname.slice(pageRoot.pathname.length);
  };
  const scriptElements = [...parsedPage.querySelectorAll("script")];
  if (scriptElements.some((element) => (
    !element.getAttribute("src") || element.textContent.trim() !== ""
  ))) throw new Error("the bound page contains an unrecorded inline script");
  const observedScriptPaths = scriptElements.map((element) => (
    relativePagePath(element.getAttribute("src"))
  )).sort();
  const expectedScriptPaths = Object.entries(EXPECTED_RUNTIME_ASSET_PATHS)
    .filter(([label, path]) => (
      label !== "page_document"
      && label !== "page_styles"
      && label !== "browser_engine_worker"
      && label !== "wasm_kernel_adapter"
      && path.endsWith(".js")
    ))
    .map(([, path]) => path)
    .sort();
  if (!sameStrings(observedScriptPaths, expectedScriptPaths)) {
    throw new Error("the page script execution surface differs from its candidate set");
  }
  const stylesheetPaths = [...parsedPage.querySelectorAll('link[rel="stylesheet"]')]
    .map((element) => relativePagePath(element.getAttribute("href")))
    .sort();
  if (!sameStrings(stylesheetPaths, [EXPECTED_RUNTIME_ASSET_PATHS.page_styles])) {
    throw new Error("the page stylesheet surface differs from its candidate set");
  }
  const compiledAssetEntries = await Promise.all([
    ["compiled_module", compiledModuleUrl],
    ["compiled_wasm", compiledWasmUrl],
  ].map(async ([label, url]) => [label, await fetchAsset(url, label)]));
  const fetchedPageAssets = Object.fromEntries([
    ...runtimeAssetEntries,
    ...compiledAssetEntries,
  ]);
  const pageAssets = Object.fromEntries([
    ...runtimeAssetEntries,
    ...compiledAssetEntries,
  ].map(([label, assetValue]) => (
    [label, publicAsset(assetValue)]
  )));
  const bundleFileByPath = new Map(normalizedCandidateFiles.map((record) => (
    [record.path, record]
  )));
  const expectedBundlePaths = [
    "browser-engine-manifest.json",
    `single/${variant.module_js}`,
    `single/${variant.wasm}`,
  ].sort();
  if (!sameStrings([...bundleFileByPath.keys()].sort(), expectedBundlePaths)) {
    throw new Error("candidate browser bundle has an unexpected file surface");
  }
  for (const [path, assetValue] of [
    ["browser-engine-manifest.json", manifestAsset],
    [`single/${variant.module_js}`, fetchedPageAssets.compiled_module],
    [`single/${variant.wasm}`, fetchedPageAssets.compiled_wasm],
  ]) {
    const record = bundleFileByPath.get(path);
    if (
      record?.sha256 !== assetValue.sha256
      || record?.bytes !== assetValue.byte_length
    ) throw new Error("page engine byte differs from candidate bundle: " + path);
  }
  if (
    fetchedPageAssets.compiled_module.sha256 !== variant.module_js_sha256
    || fetchedPageAssets.compiled_wasm.sha256 !== variant.wasm_sha256
  ) throw new Error("compiled module or WASM differs from the manifest");

  const certificateManifestValues = Object.freeze({
    root_session: rootCertificate,
    mate: mateCertificate,
    prefix: prefixCertificate,
  });
  if (
    !plainObject(candidate.certificates)
    || !sameStrings(Object.keys(candidate.certificates).sort(), [
      "mate", "prefix", "root_session",
    ])
  ) throw new Error("candidate receipt has an unexpected certificate set");
  const certificateAssets = {};
  const certificateDirectoryRecords = [];
  for (const label of ["mate", "prefix", "root_session"]) {
    const record = candidate.certificates[label];
    if (
      !plainObject(record)
      || typeof record.path !== "string"
      || !record.path.startsWith("certificates/")
      || record.path.includes("..")
      || !SHA256.test(String(record.sha256 || ""))
      || record.certificate_id !== certificateManifestValues[label].certificate_id
    ) throw new Error("candidate has an invalid " + label + " certificate record");
    const certificateUrl = new URL(record.path, candidateReceiptUrl);
    if (certificateUrl.origin !== location.origin) {
      throw new Error("certificate escaped the loopback candidate origin");
    }
    const certificateAsset = await fetchAsset(certificateUrl, label + " certificate");
    const certificatePayload = strictJson(certificateAsset, label + " certificate");
    if (
      certificateAsset.sha256 !== record.sha256
      || !sameJson(certificatePayload, certificateManifestValues[label])
    ) throw new Error(label + " certificate does not match candidate and manifest");
    certificateAssets[label] = publicAsset(certificateAsset);
    certificateDirectoryRecords.push({
      path: record.path.slice("certificates/".length),
      sha256: certificateAsset.sha256,
      bytes: certificateAsset.byte_length,
    });
  }
  certificateDirectoryRecords.sort((left, right) => left.path.localeCompare(right.path));
  if (
    await canonicalSha256(certificateDirectoryRecords)
      !== candidate.certificate_set_sha256
  ) throw new Error("standalone certificate file-set identity is invalid");

  const buildRecord = candidate.evidence_receipts?.find((entry) => entry?.label === "build");
  if (
    !plainObject(buildRecord)
    || typeof buildRecord.path !== "string"
    || !buildRecord.path.startsWith("evidence/")
    || buildRecord.path.includes("..")
    || !SHA256.test(String(buildRecord.sha256 || ""))
    || !exactInteger(buildRecord.bytes, 1)
  ) throw new Error("candidate has no bound build receipt");
  const buildReceiptUrl = new URL(buildRecord.path, candidateReceiptUrl);
  if (buildReceiptUrl.origin !== location.origin) {
    throw new Error("build receipt escaped the loopback candidate origin");
  }
  const buildAsset = await fetchAsset(buildReceiptUrl, "root-session build receipt");
  const buildReceipt = strictJson(buildAsset, "root-session build receipt");
  const buildIdentity = Object.fromEntries(ARTIFACT_IDENTITY_KEYS.map((key) => (
    [key, buildReceipt[key]]
  )));
  if (
    buildAsset.sha256 !== buildRecord.sha256
    || buildAsset.byte_length !== buildRecord.bytes
    || buildReceipt.schema !== "spc-root-session-build-receipt-v1"
    || buildReceipt.status !== "built-not-certified"
    || buildReceipt.product_publishable !== false
    || !sameJson(buildIdentity, artifact)
  ) throw new Error("candidate build receipt does not bind its six-field artifact subject");

  const localAssetSetSha256 = await canonicalSha256([
    ["candidate_receipt", publicAsset(candidateAsset)],
    ["browser_engine_manifest", publicAsset(manifestAsset)],
    ["root_session_build_receipt", publicAsset(buildAsset)],
    ...Object.entries(certificateAssets).map(([label, assetValue]) => (
      [`${label}_certificate`, assetValue]
    )),
    ...Object.entries(pageAssets),
  ].sort(([left], [right]) => left.localeCompare(right)));

  const apiDeadline = performance.now() + 10_000;
  while (
    !globalThis.ScottishProgressiveBrowserEngine?.createClient
    && performance.now() < apiDeadline
  ) await new Promise((resolve) => setTimeout(resolve, 25));
  const api = globalThis.ScottishProgressiveBrowserEngine;
  if (
    !api?.createClient
    || typeof api.BrowserEngineClient !== "function"
    || Object.isFrozen(api) !== true
  ) throw new Error("the frozen production browser-engine API did not load");
  const NativeWorker = globalThis.Worker;
  const nativePostMessage = NativeWorker?.prototype?.postMessage;
  const nativeAddEventListener = EventTarget.prototype.addEventListener;
  if (
    typeof NativeWorker !== "function"
    || typeof nativePostMessage !== "function"
    || typeof nativeAddEventListener !== "function"
    || !/\[native code\]/.test(Function.prototype.toString.call(NativeWorker))
    || !/\[native code\]/.test(Function.prototype.toString.call(nativePostMessage))
    || !/\[native code\]/.test(Function.prototype.toString.call(nativeAddEventListener))
  ) throw new Error("the page Worker constructor and messaging surface are not native");
  const workerConstructorTrace = [];
  const tracedWorkerFactory = (url, options) => {
    const resolvedUrl = new URL(String(url), location.href);
    if (
      resolvedUrl.href !== workerUrl.href
      || !plainObject(options)
      || !sameStrings(Object.keys(options).sort(), ["name", "type"])
      || options.type !== "module"
      || typeof options.name !== "string"
      || !/^scottish-progressive-(engine|root-(root-[0-7]|safe-reselector))$/
        .test(options.name)
    ) throw new Error("production client requested an unbound Worker constructor call");
    const worker = new NativeWorker(resolvedUrl.href, options);
    if (!(worker instanceof NativeWorker)) {
      throw new Error("Worker trace did not create a native Worker instance");
    }
    workerConstructorTrace.push(Object.freeze({
      sequence: workerConstructorTrace.length + 1,
      url: resolvedUrl.href,
      type: options.type,
      name: options.name,
      native_instance: true,
    }));
    return worker;
  };

  const client = api.createClient({
    workerUrl: workerUrl.href,
    workerFactory: tracedWorkerFactory,
  });
  let preflight;
  let result;
  let searchElapsedSeconds;
  try {
    const preflightStarted = performance.now();
    preflight = await client.preflight({
      sourceFingerprint: manifest.source_fingerprint,
      deadlineMs: preflightStarted + 20_000,
    });
    const searchStarted = performance.now();
    result = await client.analyzeRoot({
      fen: "rnbqkb1r/pppp2pp/4p3/5p2/8/5P1N/PPPP1KPP/RNBn1B1R w kq - 0 7",
      series: 5,
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
    }, {
      deadlineMs: searchStarted + 60_000,
      receiptDeadlineMs: searchStarted + 65_000,
    });
    searchElapsedSeconds = (performance.now() - searchStarted) / 1_000;
  } finally {
    client.close("3aaef safe-reselection supplemental capture complete");
  }

  const rootContractSha256 = await canonicalSha256(
    rootCertificate.root_session_contract,
  );
  const rootGeometrySha256 = await canonicalSha256(rootCertificate.geometry);
  const prefixContractSha256 = await canonicalSha256(
    prefixCertificate.prefix_contract,
  );
  const preflightIdentity = Object.freeze({
    ready: preflight?.ready,
    analysis_ready: preflight?.analysis_ready,
    root_iteration_ready: preflight?.root_iteration_ready,
    root_session_ready: preflight?.root_session_ready,
    mate_ready: preflight?.mate_ready,
    prefix_ready: preflight?.prefix_ready,
    source_fingerprint: preflight?.source_fingerprint,
    runtime_variant: preflight?.runtime_variant,
    thread_count: preflight?.thread_count,
    module_js_sha256: preflight?.module_js_sha256,
    wasm_sha256: preflight?.wasm_sha256,
    kernel_sha256: preflight?.kernel_sha256,
    certificate_id: preflight?.certificate_id,
    root_session_certificate_id: preflight?.root_session_certificate_id,
    mate_certificate_id: preflight?.mate_certificate_id,
    prefix_certificate_id: preflight?.prefix_certificate_id,
    engine_profile_id: preflight?.engine_profile_id,
    engine_version: preflight?.engine_version,
    ruleset_version: preflight?.ruleset_version,
    root_contract_sha256: await canonicalSha256(preflight?.root_session_contract),
    root_geometry_sha256: await canonicalSha256(preflight?.root_geometry),
    prefix_contract_sha256: await canonicalSha256(preflight?.prefix_contract),
  });
  const safe = result?.runtime_receipt?.safe_root_reselector;
  const selected = safe?.selected;
  const scans = safe?.scans;
  const scanWork = Array.isArray(scans)
    ? scans.reduce((sum, scan) => sum + Number(scan?.work_used || 0), 0)
    : -1;
  const earlyScanWork = Array.isArray(scans)
    ? scans.reduce((sum, scan) => sum + (
      scan?.order_index < SAFE_RESELECT_EARLY_COUNT
        ? Number(scan?.work_used || 0)
        : 0
    ), 0)
    : -1;
  const widenedScanWork = Array.isArray(scans)
    ? scans.reduce((sum, scan) => sum + (
      scan?.order_index >= SAFE_RESELECT_EARLY_COUNT
        ? Number(scan?.work_used || 0)
        : 0
    ), 0)
    : -1;
  const observedChildBoundarySha256 = await canonicalSha256(
    result?.checked_prefix?.next_state,
  );
  const assertedChildPositionHash = observedChildBoundarySha256
    === EXPECTED_CHILD_BOUNDARY_SHA256
    ? EXPECTED_POSITION_HASH
    : null;
  const workerNames = new Set(workerConstructorTrace.map((entry) => entry.name));
  const expectedOrdinaryRootWorkers = Array.from(
    { length: 8 },
    (_, index) => `scottish-progressive-root-root-${index}`,
  );
  const checks = Object.freeze({
    candidate_receipt_raw_sha256_bound: SHA256.test(candidateAsset.sha256),
    candidate_source_revision_bound:
      candidate.source_revision === artifact.source_revision,
    candidate_id_commits_runtime_assets:
      candidate.candidate_id === derivedCandidateId,
    page_release_query_bound: candidate.source_revision.startsWith(releaseTag),
    candidate_bundle_file_set_bound:
      await canonicalSha256(normalizedCandidateFiles)
        === candidate.browser_bundle.artifact_set_sha256,
    standalone_certificates_bound:
      await canonicalSha256(certificateDirectoryRecords)
        === candidate.certificate_set_sha256,
    build_receipt_six_field_subject_bound: sameJson(buildIdentity, artifact),
    authoritative_browser_runtime_asset_set_bound:
      validateAssetBindings(
        runtimeAuthority,
        observedRuntimeRecords,
        candidate.source_revision,
      ) === true
      && await canonicalSha256(runtimeAuthority.files)
        === runtimeAuthority.artifact_set_sha256,
    page_execution_surface_bound:
      sameStrings(observedScriptPaths, expectedScriptPaths)
      && sameStrings(stylesheetPaths, [EXPECTED_RUNTIME_ASSET_PATHS.page_styles]),
    local_page_asset_set_hash_bound: SHA256.test(localAssetSetSha256),
    native_worker_constructor_trace_bound:
      workerConstructorTrace.length >= 10
      && workerConstructorTrace.every((entry) => (
        entry.native_instance === true
        && entry.url === workerUrl.href
        && entry.type === "module"
      ))
      && workerNames.has("scottish-progressive-engine")
      && workerNames.has("scottish-progressive-root-safe-reselector")
      && expectedOrdinaryRootWorkers.every((name) => workerNames.has(name))
      && pageAssets.browser_engine_worker.url === workerUrl.href
      && pageAssets.browser_engine_worker.sha256 === workerAuthority.sha256
      && pageAssets.wasm_kernel_adapter.url === adapterUrl.href
      && pageAssets.wasm_kernel_adapter.sha256 === adapterAuthority.sha256,
    manifest_preflight_identity_bound: Boolean(
      preflight?.ready === true
      && preflight.root_iteration_ready === true
      && preflight.root_session_ready === true
      && preflight.mate_ready === true
      && preflight.prefix_ready === true
      && preflight.source_fingerprint === manifest.source_fingerprint
      && preflight.runtime_variant === "single"
      && preflight.thread_count === 1
      && preflight.module_js_sha256 === variant.module_js_sha256
      && preflight.wasm_sha256 === variant.wasm_sha256
      && preflight.kernel_sha256 === variant.kernel_sha256
      && preflight.root_session_certificate_id === rootCertificate.certificate_id
      && preflight.mate_certificate_id === mateCertificate.certificate_id
      && preflight.prefix_certificate_id === prefixCertificate.certificate_id
      && preflight.engine_profile_id === rootCertificate.engine?.profile_id
      && preflight.engine_version === rootCertificate.engine?.engine_version
      && preflight.ruleset_version === rootCertificate.engine?.ruleset_version
      && preflightIdentity.root_contract_sha256 === rootContractSha256
      && preflightIdentity.root_geometry_sha256 === rootGeometrySha256
      && preflightIdentity.prefix_contract_sha256 === prefixContractSha256
    ),
    public_analyze_root_completed: result?.ok === true && result.status === "complete",
    safe_reselector_publication: result?.root_search_mode === "safe-root-reselector"
      && result.runtime_receipt?.search_mode === "safe-root-reselector"
      && safe?.schema === "spc-root-safe-reselector-receipt-v1"
      && safe.trigger === "all-retained-children-proven-mating"
      && safe.status === "selected",
    d5_requested_d0_fallback: result?.requested_depth === 5
      && result.completed_depth === 0,
    exact_rank_62_move: sameStrings(result?.best_full_series, EXPECTED_MOVES)
      && selected?.order_index === 61
      && selected.order_key === EXPECTED_MACHINE_NOTATION
      && selected.root_series?.machine_notation === EXPECTED_MACHINE_NOTATION
      && sameStrings(selected.root_series?.moves, EXPECTED_MOVES),
    exact_rank_62_child_pfen: sameJson(
      result?.checked_prefix?.next_state,
      EXPECTED_CHILD,
    ) && sameJson(selected?.authoritative_child_boundary, EXPECTED_CHILD)
      && sameJson(selected?.root_series?.child_boundary, EXPECTED_CHILD),
    exact_rank_62_child_position_hash:
      observedChildBoundarySha256 === EXPECTED_CHILD_BOUNDARY_SHA256
      && assertedChildPositionHash === EXPECTED_POSITION_HASH,
    authoritative_root_replay_bound:
      result?.checked_prefix?.complete === true
      && result.checked_prefix.outcome === null
      && result.checked_prefix.ended_by_check === false
      && sameStrings(result.checked_prefix.prefix, EXPECTED_MOVES)
      && sameJson(selected?.authoritative_root_replay, result.checked_prefix)
      && Array.isArray(scans)
      && scans.every((scan) => (
        scan?.authoritative_root_replay?.complete === true
        && sameStrings(
          scan.authoritative_root_replay.prefix,
          scan.root_series?.moves,
        )
        && sameJson(
          scan.authoritative_root_replay.next_state,
          scan.authoritative_child_boundary,
        )
      )),
    widened_w512_frontier: safe?.requested_width === SAFE_RESELECT_WIDTH
      && safe.retained_count === SAFE_RESELECT_WIDTH
      && safe.width_complete === false
      && selected?.frontier_stage === "widened-w512"
      && selected.per_child_max_work === SAFE_RESELECT_WIDENED_CHILD_WORK,
    exact_safe_exhaustion: selected?.status === "exhausted"
      && selected.cache_hit === false
      && selected.call_work_credit === SAFE_RESELECT_WIDENED_CHILD_WORK
      && safe.selected_safety_basis === "exact-immediate-reply-mate-exhaustion"
      && safe.safety_certification_scope
        === "selected-child-immediate-reply-mate-only",
    exact_generation_work: safe?.generation_work === EXPECTED_GENERATION_WORK,
    exact_selected_proof_work: selected?.work_used === EXPECTED_SELECTED_WORK,
    exact_safe_lane_work: safe?.lane_work_used === EXPECTED_LANE_WORK,
    work_conserved: Array.isArray(scans)
      && scans.length === 62
      && safe.early_frontier_child_max_work === SAFE_RESELECT_EARLY_CHILD_WORK
      && safe.widened_frontier_child_max_work
        === SAFE_RESELECT_WIDENED_CHILD_WORK
      && safe.lane_max_work === SAFE_RESELECT_TOTAL_WORK
      && safe.early_frontier_work_used === earlyScanWork
      && safe.widened_frontier_work_used === widenedScanWork
      && scanWork === earlyScanWork + widenedScanWork
      && safe.lane_work_used === safe.generation_work + scanWork
      && safe.total_committed_work
        === safe.ordinary_committed_work + safe.lane_work_used
      && result.work === safe.total_committed_work
      && result.work <= 100_000_000,
    isolated_cleanup_complete: safe?.isolated_session_destroyed === true
      && safe.isolated_destroy_error_code === null
      && safe.isolated_worker_terminated === true
      && safe.isolated_worker_termination_error_code === null
      && safe.isolated_cleanup_status
        === "session-destroyed-and-worker-terminated"
      && safe.ordinary_pool_recreated === false
      && safe.ordinary_pool_restore_policy === "lazy-next-request"
      && safe.restore_error_code === null,
    no_score_or_proof_claim: !own(result, "score")
      && !own(result, "proof")
      && !own(result, "proof_bounds")
      && !own(result, "alternatives")
      && Array.isArray(result?.principal_variation)
      && result.principal_variation.length === 0
      && result.root_scores_complete === false
      && result.root_bound_coverage_complete === false,
    runtime_identity_bound: result?.source_fingerprint === manifest.source_fingerprint
      && result.wasm_sha256 === variant.wasm_sha256
      && result.module_js_sha256 === variant.module_js_sha256
      && result.kernel_sha256 === variant.kernel_sha256
      && result.certificate_id === rootCertificate.certificate_id
      && result.mate_certificate_id === mateCertificate.certificate_id
      && result.prefix_certificate_id === prefixCertificate.certificate_id,
    compiled_safety_publication: result?.publishable === true
      && result.safety_certified === true
      && result.legal_series_certified === true
      && result.authoritative_replay_certified === true
      && result.legal_validation_runtime === "compiled-wasm",
    deadline_respected: result?.timed_out === false
      && result.work_limit_reached === false
      && Number.isFinite(searchElapsedSeconds)
      && searchElapsedSeconds <= 65,
  });
  const failedChecks = Object.entries(checks)
    .filter(([, passed]) => passed !== true)
    .map(([name]) => name);
  if (failedChecks.length > 0) {
    throw new Error("3aaef safe-reselection checks failed: " + failedChecks.join(", "));
  }

  return {
    schema: "spc-opera-safe-reselection-receipt-v1",
    status: "passed-not-certified",
    product_publishable: false,
    safety_certified: false,
    certificate_id: null,
    authenticity: {
      scope: "local-checkout-hash-bound-unsigned-v1",
      limitation:
        "This supplemental receipt observes one loopback-served candidate and does not replace the signed release authority.",
      candidate_receipt: publicAsset(candidateAsset),
      candidate_id: candidate.candidate_id,
      source_revision: candidate.source_revision,
      artifact_subject: artifact,
      build_receipt: publicAsset(buildAsset),
      browser_bundle_artifact_set_sha256:
        candidate.browser_bundle.artifact_set_sha256,
      browser_runtime_artifact_set_sha256:
        runtimeAuthority.artifact_set_sha256,
      certificate_set_sha256: candidate.certificate_set_sha256,
      standalone_certificates: certificateAssets,
      local_page_asset_set_sha256: localAssetSetSha256,
      manifest: publicAsset(manifestAsset),
      page_assets: pageAssets,
    },
    manifest_binding: {
      source_fingerprint: manifest.source_fingerprint,
      runtime_variant: "single",
      thread_count: 1,
      module_js: variant.module_js,
      wasm: variant.wasm,
      module_js_sha256: variant.module_js_sha256,
      wasm_sha256: variant.wasm_sha256,
      kernel_sha256: variant.kernel_sha256,
      root_session_certificate_id: rootCertificate.certificate_id,
      mate_certificate_id: mateCertificate.certificate_id,
      prefix_certificate_id: prefixCertificate.certificate_id,
      root_contract_sha256: rootContractSha256,
      root_geometry_sha256: rootGeometrySha256,
      prefix_contract_sha256: prefixContractSha256,
    },
    page_environment: pageEnvironment,
    preflight_identity: preflightIdentity,
    request: {
      fixture_id: "bucephalus-3aaef-series5-rank62-v1",
      fen: "rnbqkb1r/pppp2pp/4p3/5p2/8/5P1N/PPPP1KPP/RNBn1B1R w kq - 0 7",
      series: 5,
      requested_depth: 5,
      retained_width: 32,
      widened_width: SAFE_RESELECT_WIDTH,
      time_limit_seconds: 60,
      maximum_work: 100_000_000,
    },
    expected_witness: {
      one_based_rank: 62,
      machine_notation: EXPECTED_MACHINE_NOTATION,
      child_pfen: EXPECTED_CHILD_PFEN,
      child_boundary_sha256: observedChildBoundarySha256,
      asserted_child_position_hash: assertedChildPositionHash,
      generation_work: EXPECTED_GENERATION_WORK,
      selected_probe_work: EXPECTED_SELECTED_WORK,
      safe_lane_work: EXPECTED_LANE_WORK,
    },
    search_elapsed_seconds: searchElapsedSeconds,
    worker_constructor_trace: workerConstructorTrace,
    worker_constructor_trace_sha256: await canonicalSha256(workerConstructorTrace),
    checks,
    result,
  };
}


async function main() {
  const args = argumentsOf(process.argv.slice(2));
  if (!Number.isFinite(args.timeoutMs) || args.timeoutMs < 65_000) {
    throw new Error("--timeout-ms must be at least 65000");
  }
  const version = await fetch(`${args.endpoint}/json/version`, {
    cache: "no-store",
  }).then((response) => {
    if (!response.ok) throw new Error(`Opera CDP version failed: ${response.status}`);
    return response.json();
  });
  const targetResponse = await fetch(
    `${args.endpoint}/json/new?${encodeURIComponent("about:blank")}`,
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
    await call("Network.enable");
    await call("Network.setCacheDisabled", { cacheDisabled: true });
    const cdpVersion = await call("Browser.getVersion");
    const navigation = await call("Page.navigate", { url: args.url });
    if (navigation.errorText) {
      throw new Error(`Opera page navigation failed: ${navigation.errorText}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    const expression = `(${browserProbe.toString()})(${JSON.stringify(
      args.candidateReceiptUrl,
    )}, ${validateAuthoritativeAssetBindings.toString()})`;
    const evaluated = await call("Runtime.evaluate", {
      expression,
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
        "Opera safe-reselection probe did not return a passing receipt: "
          + JSON.stringify(payload),
      );
    }
    if (
      payload.page_environment?.user_agent !== cdpVersion.userAgent
      || cdpVersion.userAgent !== version["User-Agent"]
      || !String(cdpVersion.userAgent || "").includes(" OPR/")
    ) throw new Error("Opera CDP and page realm identities differ");
    const receipt = {
      ...payload,
      captured_at: new Date().toISOString(),
      cdp: {
        browser: version.Browser,
        product: cdpVersion.product,
        revision: cdpVersion.revision,
        protocol_version: cdpVersion.protocolVersion,
        js_version: cdpVersion.jsVersion,
        user_agent: cdpVersion.userAgent,
        web_socket_debugger_url_recorded: true,
        cache_disabled_before_navigation: true,
      },
      page_url: args.url,
      candidate_receipt_url: args.candidateReceiptUrl,
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


if (
  process.argv[1]
  && import.meta.url === pathToFileURL(process.argv[1]).href
) await main();
