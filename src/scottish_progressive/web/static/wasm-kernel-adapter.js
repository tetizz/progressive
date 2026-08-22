const MANIFEST_SCHEMA = "spc-browser-wasm-manifest-v1";
const CERTIFICATE_SCHEMA = "spc-browser-wasm-certificate-v1";
const PREFIX_CONTRACT_SCHEMA = "spc-boundary-prefix-contract-v1";
const PREFIX_RESULT_SCHEMA = "spc-boundary-prefix-v1";
const MIN_PREFIX_DIFFERENTIAL_CASES = 14;
const PREFIX_HARD_LIMITS = Object.freeze({
  maximum_fen_utf8_bytes: 512,
  maximum_series_number: 256,
  maximum_quiet_series: 1_000_000,
  maximum_ep_targets: 8,
  maximum_ep_utf8_bytes: 23,
  maximum_prefix_moves: 256,
  maximum_prefix_utf8_bytes: 1_535,
  maximum_uci_move_bytes: 5,
  maximum_promoted_hex_bytes: 18,
});
const SHA256 = /^[0-9a-f]{64}$/;
const SOURCE_FINGERPRINT = /^[0-9a-f]{16}$/;
const PROMOTED_HEX = /^[0-9a-f]{16}$/;
const MAX_INITIAL_MEMORY_BYTES = 128 * 1024 * 1024;
const MAXIMUM_MEMORY_BYTES = 256 * 1024 * 1024;
const MAX_ESTIMATED_PEAK_MEMORY_BYTES = 192 * 1024 * 1024;

class KernelAdapterError extends Error {
  constructor(message, code) {
    super(message);
    this.name = "KernelAdapterError";
    this.code = code;
  }
}

function safeAssetName(value, extension) {
  const name = String(value || "");
  if (
    !name.endsWith(extension)
    || !/^[A-Za-z0-9._-]+$/.test(name)
    || name.includes("..")
  ) {
    throw new KernelAdapterError(
      `The browser engine manifest contains an unsafe ${extension} asset name.`,
      "browser-manifest-invalid",
    );
  }
  return name;
}

async function fetchRequired(url, kind) {
  let response;
  try {
    response = await fetch(url, { cache: "no-store", credentials: "same-origin" });
  } catch (cause) {
    throw new KernelAdapterError(
      `The browser engine ${kind} could not be downloaded: ${cause?.message || cause}`,
      "browser-artifact-unavailable",
    );
  }
  if (!response.ok) {
    throw new KernelAdapterError(
      `The browser engine ${kind} returned HTTP ${response.status}.`,
      "browser-artifact-unavailable",
    );
  }
  return response;
}

async function sha256Hex(bytes) {
  if (!globalThis.crypto?.subtle) {
    throw new KernelAdapterError(
      "This browser cannot verify the WebAssembly artifact hash.",
      "browser-crypto-unavailable",
    );
  }
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

function validateMemoryLimits(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const expectedKeys = [
    "estimated_peak_bytes",
    "growth_enabled",
    "initial_bytes",
    "maximum_bytes",
  ];
  if (!sameJson(Object.keys(value).sort(), expectedKeys)) return null;
  const memory = {
    initial_bytes: value.initial_bytes,
    maximum_bytes: value.maximum_bytes,
    estimated_peak_bytes: value.estimated_peak_bytes,
    growth_enabled: value.growth_enabled,
  };
  const pageAligned = (number) => Number.isInteger(number)
    && number > 0
    && number % 65_536 === 0;
  if (
    !pageAligned(memory.initial_bytes)
    || !pageAligned(memory.maximum_bytes)
    || !pageAligned(memory.estimated_peak_bytes)
    || memory.initial_bytes > MAX_INITIAL_MEMORY_BYTES
    || memory.maximum_bytes > MAXIMUM_MEMORY_BYTES
    || memory.estimated_peak_bytes > MAX_ESTIMATED_PEAK_MEMORY_BYTES
    || memory.initial_bytes > memory.estimated_peak_bytes
    || memory.estimated_peak_bytes > memory.maximum_bytes
    || typeof memory.growth_enabled !== "boolean"
    || (!memory.growth_enabled && memory.initial_bytes !== memory.maximum_bytes)
  ) return null;
  return memory;
}

function validateAnalysisLimits(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const limits = {
    maximum_depth: value.maximum_depth,
    maximum_max_series: value.maximum_max_series,
    maximum_seconds: Number(value.maximum_seconds),
    maximum_generation_positions: value.maximum_generation_positions,
    default_depth: value.default_depth,
    default_max_series: value.default_max_series,
    default_seconds: Number(value.default_seconds),
    default_generation_positions: value.default_generation_positions,
  };
  if (
    !Number.isInteger(limits.maximum_depth)
    || limits.maximum_depth < 1
    || limits.maximum_depth > 64
    || !Number.isInteger(limits.maximum_max_series)
    || limits.maximum_max_series < 1
    || limits.maximum_max_series > 16_384
    || !Number.isInteger(limits.maximum_generation_positions)
    || limits.maximum_generation_positions < 1_000
    || limits.maximum_generation_positions > 0xffffffff
    || !Number.isFinite(limits.maximum_seconds)
    || limits.maximum_seconds <= 0
    || limits.maximum_seconds > 0xffffffff / 1000
    || !Number.isInteger(limits.default_depth)
    || limits.default_depth < 1
    || limits.default_depth > limits.maximum_depth
    || !Number.isInteger(limits.default_max_series)
    || limits.default_max_series < 1
    || limits.default_max_series > limits.maximum_max_series
    || !Number.isInteger(limits.default_generation_positions)
    || limits.default_generation_positions < 1_000
    || limits.default_generation_positions > limits.maximum_generation_positions
    || !Number.isFinite(limits.default_seconds)
    || limits.default_seconds <= 0
    || limits.default_seconds > limits.maximum_seconds
  ) return null;
  return limits;
}

function runtimeMemoryBytes(module) {
  const bytes = module?.HEAPU8?.buffer?.byteLength;
  return Number.isInteger(bytes) && bytes > 0 ? bytes : null;
}

function validateRuntimeMemory(module, memory, { initial = false } = {}) {
  const bytes = runtimeMemoryBytes(module);
  if (
    bytes === null
    || (initial && bytes !== memory.initial_bytes)
    || bytes > memory.estimated_peak_bytes
    || bytes > memory.maximum_bytes
  ) {
    throw new KernelAdapterError(
      "The WebAssembly heap does not match its certified memory envelope.",
      "browser-memory-envelope-exceeded",
    );
  }
  return bytes;
}

function sameJson(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function validatePrefixContract(value, { native = false } = {}) {
  const rawLimits = value?.[native ? "hard_limits" : "limits"];
  if (
    !value
    || typeof value !== "object"
    || Array.isArray(value)
    || value.schema !== PREFIX_CONTRACT_SCHEMA
    || value.result_schema !== PREFIX_RESULT_SCHEMA
    || value.abi_version !== 1
    || value.chess960 !== false
    || value.promoted_hex_required_for_product !== true
    || !rawLimits
    || typeof rawLimits !== "object"
    || Array.isArray(rawLimits)
    || !sameJson(Object.keys(rawLimits).sort(), Object.keys(PREFIX_HARD_LIMITS).sort())
  ) return null;
  const limits = {};
  for (const [name, hardMaximum] of Object.entries(PREFIX_HARD_LIMITS)) {
    const candidate = rawLimits[name];
    if (
      !Number.isInteger(candidate)
      || candidate < 1
      || candidate > hardMaximum
      || (native && candidate !== hardMaximum)
    ) return null;
    limits[name] = candidate;
  }
  if (limits.maximum_prefix_moves > limits.maximum_series_number) return null;
  return Object.freeze({
    schema: PREFIX_CONTRACT_SCHEMA,
    result_schema: PREFIX_RESULT_SCHEMA,
    abi_version: 1,
    chess960: false,
    promoted_hex_required_for_product: true,
    limits: Object.freeze(limits),
  });
}

function certificateMatchesArtifact(certificate, value, {
  name,
  sourceFingerprint,
  threadCount,
  supportFiles,
}) {
  return Boolean(
    certificate
    && typeof certificate === "object"
    && !Array.isArray(certificate)
    && certificate.status === "certified"
    && certificate.contract_version === 1
    && certificate.source_fingerprint === sourceFingerprint
    && certificate.runtime_variant === name
    && certificate.thread_count === threadCount
    && certificate.wasm_sha256 === value.wasm_sha256
    && certificate.module_js_sha256 === value.module_js_sha256
    && typeof certificate.certificate_id === "string"
    && certificate.certificate_id
    && sameJson(certificate.support_files, supportFiles)
  );
}

function validateSafetyCertificate(certificate, value, context) {
  if (certificate === undefined || certificate === null) return null;
  const evidence = certificate?.evidence;
  const engine = certificate?.engine;
  const limits = validateAnalysisLimits(engine?.analysis_limits);
  const memory = validateMemoryLimits(certificate?.memory);
  if (
    !certificateMatchesArtifact(certificate, value, context)
    || certificate.schema !== CERTIFICATE_SCHEMA
    || certificate.safety_certified !== true
    || certificate.abi_version !== 1
    || !evidence
    || evidence.failures !== 0
    || !Number.isInteger(evidence.differential_cases)
    || evidence.differential_cases < 1
    || evidence.start_position_parity !== true
    || evidence.s4_mate_safety !== true
    || evidence.interrupted_depth_publication !== true
    || evidence.compiled_legal_series_validation !== true
    || evidence.compiled_authoritative_replay !== true
    || evidence.start_w32_d5_completed_depth !== 5
    || evidence.start_w32_d5_width !== 32
    || !Number.isFinite(Number(evidence.start_w32_d5_elapsed_seconds))
    || Number(evidence.start_w32_d5_elapsed_seconds) >= 60
    || Number(evidence.start_w32_d5_elapsed_seconds) < 0
    || !engine
    || !["engine_profile_id", "engine_profile_name", "engine_version", "ruleset_version"]
      .every((key) => typeof engine[key] === "string" && engine[key])
    || !limits
    || !memory
  ) {
    throw new KernelAdapterError(
      `The ${context.name} WebAssembly artifact has no matching safety certificate.`,
      "browser-kernel-not-certified",
    );
  }
  return { certificate, engine, limits, memory };
}

function validatePrefixCertificate(certificate, value, context) {
  if (certificate === undefined || certificate === null) return null;
  const evidence = certificate?.evidence;
  const engine = certificate?.engine;
  const contract = validatePrefixContract(certificate?.prefix_contract);
  const memory = validateMemoryLimits(certificate?.memory);
  if (
    !certificateMatchesArtifact(certificate, value, context)
    || !evidence
    || evidence.failures !== 0
    || evidence.compiled_prefix_replay !== true
    || evidence.multi_ep_san !== true
    || evidence.illegal_prefix_fail_closed !== true
    || !Number.isInteger(evidence.differential_cases)
    || evidence.differential_cases < MIN_PREFIX_DIFFERENTIAL_CASES
    || !engine
    || !["engine_version", "ruleset_version"]
      .every((key) => typeof engine[key] === "string" && engine[key])
    || !contract
    || !memory
  ) {
    throw new KernelAdapterError(
      `The ${context.name} WebAssembly artifact has no matching prefix certificate.`,
      "browser-prefix-contract-uncertified",
    );
  }
  return { certificate, contract, engine, memory };
}

function validateVariant(name, value, sourceFingerprint) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new KernelAdapterError(
      `The browser engine ${name} variant is missing.`,
      "browser-manifest-invalid",
    );
  }
  const threadCount = Number(value.thread_count);
  if (
    !SHA256.test(String(value.wasm_sha256 || ""))
    || !SHA256.test(String(value.module_js_sha256 || ""))
    || !Number.isInteger(threadCount)
    || threadCount < 1
    || (name === "single" && threadCount !== 1)
    || (name === "pthread" && threadCount < 2)
  ) {
    throw new KernelAdapterError(
      `The browser engine ${name} variant identity is invalid.`,
      "browser-manifest-invalid",
    );
  }
  const supportFiles = Array.isArray(value.support_files)
    ? value.support_files.map((item) => {
      if (
        !item
        || typeof item !== "object"
        || !SHA256.test(String(item.sha256 || ""))
      ) {
        throw new KernelAdapterError(
          `The ${name} WebAssembly support-file identity is invalid.`,
          "browser-manifest-invalid",
        );
      }
      return { name: safeAssetName(item.name, ".js"), sha256: item.sha256 };
    })
    : [];
  if (name === "single" && supportFiles.length !== 0) {
    throw new KernelAdapterError(
      "The single-thread WebAssembly lane may not load external support code.",
      "browser-support-file-uncertified",
    );
  }
  const context = { name, sourceFingerprint, threadCount, supportFiles };
  const analysis = validateSafetyCertificate(value.safety_certificate, value, context);
  const prefix = validatePrefixCertificate(value.prefix_certificate, value, context);
  if (!analysis && !prefix) {
    throw new KernelAdapterError(
      `The ${name} WebAssembly artifact has no certified capability.`,
      "browser-kernel-not-certified",
    );
  }
  if (analysis && prefix) {
    if (!sameJson(analysis.memory, prefix.memory)) {
      throw new KernelAdapterError(
        "Search and prefix certificates have different memory envelopes.",
        "browser-memory-envelope-mismatch",
      );
    }
    if (
      analysis.engine.engine_version !== prefix.engine.engine_version
      || analysis.engine.ruleset_version !== prefix.engine.ruleset_version
    ) {
      throw new KernelAdapterError(
        "Search and prefix certificates have different engine identities.",
        "browser-manifest-identity-mismatch",
      );
    }
  }
  return {
    ...value,
    thread_count: threadCount,
    wasm: safeAssetName(value.wasm, ".wasm"),
    module_js: safeAssetName(value.module_js, ".js"),
    support_files: supportFiles,
    analysis_ready: analysis !== null,
    prefix_ready: prefix !== null,
    analysis_certificate: analysis,
    prefix_capability: prefix,
    analysis_limits: analysis?.limits ?? null,
    prefix_contract: prefix?.contract ?? null,
    memory_limits: analysis?.memory ?? prefix?.memory ?? null,
    engine_identity: analysis?.engine ?? prefix?.engine ?? null,
  };
}

function validateManifest(manifest, expectedSourceFingerprint) {
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
    throw new KernelAdapterError(
      "The browser engine manifest is not a JSON object.",
      "browser-manifest-invalid",
    );
  }
  if (
    manifest.schema !== MANIFEST_SCHEMA
    || manifest.contract_version !== 1
    || manifest.abi_version !== 1
    || !SOURCE_FINGERPRINT.test(String(manifest.source_fingerprint || ""))
    || (expectedSourceFingerprint
      && manifest.source_fingerprint !== expectedSourceFingerprint)
    || !manifest.variants
    || typeof manifest.variants !== "object"
    || Array.isArray(manifest.variants)
  ) {
    throw new KernelAdapterError(
      "The browser engine manifest does not match this deployed engine.",
      "browser-manifest-identity-mismatch",
    );
  }
  return {
    ...manifest,
    variants: {
      single: validateVariant(
        "single",
        manifest.variants.single,
        manifest.source_fingerprint,
      ),
      ...(manifest.variants.pthread ? {
        pthread: validateVariant(
          "pthread",
          manifest.variants.pthread,
          manifest.source_fingerprint,
        ),
      } : {}),
    },
  };
}

function validateKernelRequest(request, identity) {
  if (
    !request
    || request.contract_version !== 1
    || typeof request.request_id !== "string"
    || !request.request_id
    || !request.boundary
    || !request.limits
  ) {
    throw new KernelAdapterError(
      "The browser engine request does not match contract version 1.",
      "browser-request-invalid",
    );
  }
  const { boundary, limits } = request;
  if (
    typeof boundary.fen !== "string"
    || !boundary.fen
    || boundary.fen.length > 512
    || /[\0\r\n]/.test(boundary.fen)
    || !Number.isInteger(boundary.series)
    || boundary.series < 1
    || boundary.series > 256
    || !Number.isInteger(boundary.quiet_series)
    || boundary.quiet_series < 0
    || boundary.quiet_series > 0x7fffffff
    || !Array.isArray(boundary.ep_targets)
    || boundary.ep_targets.length > 8
    || boundary.ep_targets.some((square) => !/^[a-h][1-8]$/.test(String(square)))
    || !PROMOTED_HEX.test(String(boundary.promoted_hex || ""))
    || boundary.chess960 !== false
    || !Array.isArray(boundary.prefix)
    || boundary.prefix.length !== 0
  ) {
    throw new KernelAdapterError(
      "The supplied position is outside the compiled boundary contract.",
      "browser-boundary-unsupported",
    );
  }
  if (
    !Number.isInteger(limits.depth)
    || limits.depth < 1
    || limits.depth > 64
    || !Number.isInteger(limits.max_series)
    || limits.max_series < 1
    || limits.max_series > 16_384
    || !Number.isInteger(limits.max_generation_positions)
    || limits.max_generation_positions < 1_000
    || limits.max_generation_positions > 0xffffffff
    || !Number.isFinite(limits.time_limit_seconds)
    || limits.time_limit_seconds <= 0
    || limits.time_limit_seconds > 0xffffffff / 1000
    || limits.best_move_only !== true
  ) {
    throw new KernelAdapterError(
      "The browser engine limits are outside the kernel envelope.",
      "browser-limits-unsupported",
    );
  }
  const certified = identity?.analysis_limits;
  if (
    !certified
    || limits.depth > certified.maximum_depth
    || limits.max_series > certified.maximum_max_series
    || limits.time_limit_seconds > certified.maximum_seconds
    || limits.max_generation_positions > certified.maximum_generation_positions
  ) {
    throw new KernelAdapterError(
      "The supplied search exceeds the artifact's certified analysis envelope.",
      "browser-certified-limits-exceeded",
    );
  }
}

function utf8Length(value) {
  if (typeof TextEncoder !== "function") {
    throw new KernelAdapterError(
      "This browser cannot measure the certified prefix request envelope.",
      "browser-prefix-text-encoder-unavailable",
    );
  }
  return new TextEncoder().encode(value).byteLength;
}

function validatePrefixKernelRequest(request, identity) {
  const contract = identity?.prefix_contract;
  const limits = contract?.limits;
  const boundary = request?.boundary;
  const prefix = request?.prefix;
  const epTargets = boundary?.ep_targets;
  if (
    identity?.prefix_ready !== true
    || !contract
    || request?.contract_version !== 1
    || request?.operation !== "prefix-replay"
    || typeof request?.request_id !== "string"
    || !request.request_id
    || !boundary
    || typeof boundary.fen !== "string"
    || !boundary.fen
    || boundary.fen !== boundary.fen.trim()
    || /[\0\r\n]/.test(boundary.fen)
    || utf8Length(boundary.fen) > limits.maximum_fen_utf8_bytes
    || !Number.isInteger(boundary.series)
    || boundary.series < 1
    || boundary.series > limits.maximum_series_number
    || !Number.isInteger(boundary.quiet_series)
    || boundary.quiet_series < 0
    || boundary.quiet_series > limits.maximum_quiet_series
    || !Array.isArray(epTargets)
    || epTargets.length > limits.maximum_ep_targets
    || epTargets.some((square) => (
      typeof square !== "string" || !/^[a-h][1-8]$/.test(square)
    ))
    || new Set(epTargets).size !== epTargets.length
    || epTargets.some((square, index) => index > 0 && epTargets[index - 1] >= square)
    || utf8Length(epTargets.join(",") || "-") > limits.maximum_ep_utf8_bytes
    || !PROMOTED_HEX.test(String(boundary.promoted_hex || ""))
    || utf8Length(boundary.promoted_hex) > limits.maximum_promoted_hex_bytes
    || boundary.chess960 !== false
    || !Array.isArray(prefix)
    || prefix.length > boundary.series
    || prefix.length > limits.maximum_prefix_moves
    || prefix.some((move) => (
      typeof move !== "string"
      || !/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(move)
      || utf8Length(move) > limits.maximum_uci_move_bytes
    ))
    || utf8Length(prefix.join("/")) > limits.maximum_prefix_utf8_bytes
  ) {
    throw new KernelAdapterError(
      "The prefix request exceeds the certified compiled envelope.",
      "browser-prefix-request-unsupported",
    );
  }
}

function validateNativePrefixContract(module, certifiedContract) {
  let pointer;
  try {
    pointer = module._spc_boundary_prefix_contract_json();
  } catch (cause) {
    throw new KernelAdapterError(
      `The compiled prefix contract could not be read: ${cause?.message || cause}`,
      "browser-prefix-abi-mismatch",
    );
  }
  if (!pointer) {
    throw new KernelAdapterError(
      "The compiled prefix contract returned a null pointer.",
      "browser-prefix-abi-mismatch",
    );
  }
  let nativeContract;
  try {
    nativeContract = validatePrefixContract(
      JSON.parse(module.UTF8ToString(pointer)),
      { native: true },
    );
  } catch {
    nativeContract = null;
  }
  if (!nativeContract) {
    throw new KernelAdapterError(
      "The compiled prefix ABI does not match its hard contract.",
      "browser-prefix-abi-mismatch",
    );
  }
  for (const [name, certifiedMaximum] of Object.entries(certifiedContract.limits)) {
    if (certifiedMaximum > nativeContract.limits[name]) {
      throw new KernelAdapterError(
        `The certified prefix limit ${name} exceeds the compiled ABI.`,
        "browser-prefix-abi-mismatch",
      );
    }
  }
  return nativeContract;
}

function normalizeKernelResult(raw, request, identity) {
  const stats = raw?.stats && typeof raw.stats === "object" ? raw.stats : {};
  const requestedDepth = Number(raw?.requested_depth);
  const completedDepth = Number(raw?.completed_depth);
  const best = Array.isArray(raw?.best_full_series)
    ? raw.best_full_series
    : Array.isArray(raw?.pv?.[0]) ? raw.pv[0] : [];
  const safetyCertified = raw?.safety_certified === true
    && raw?.safety_status === "certified";
  const legalSeriesCertified = raw?.legal_series_certified === true;
  const authoritativeReplayCertified = raw?.authoritative_replay_certified === true;
  const legalValidationRuntime = raw?.legal_validation_runtime === "compiled-wasm"
    ? "compiled-wasm"
    : null;
  // Publication is deliberately explicit. Completing minimax is insufficient:
  // the root reply-mate screen and authoritative replay must also certify it.
  const publishable = raw?.publishable === true
    && safetyCertified
    && legalSeriesCertified
    && authoritativeReplayCertified
    && legalValidationRuntime === "compiled-wasm"
    && raw?.checked_prefix
    && typeof raw.checked_prefix === "object"
    && !Array.isArray(raw.checked_prefix);
  return {
    ...raw,
    ok: raw?.status === "complete" || publishable,
    publishable,
    safety_certified: safetyCertified,
    legal_series_certified: legalSeriesCertified,
    authoritative_replay_certified: authoritativeReplayCertified,
    legal_validation_runtime: legalValidationRuntime,
    checked_prefix: raw?.checked_prefix ?? null,
    source_fingerprint: identity.source_fingerprint,
    wasm_sha256: identity.wasm_sha256,
    module_js_sha256: identity.module_js_sha256,
    certificate_id: identity.certificate_id,
    runtime_variant: identity.runtime_variant,
    thread_count: identity.thread_count,
    requested_depth: requestedDepth,
    completed_depth: completedDepth,
    elapsed_seconds: Number(raw?.elapsed_seconds ?? 0),
    timed_out: raw?.status === "deadline" || raw?.timed_out === true,
    work_limit_reached: raw?.status === "work_limit"
      || raw?.work_limit_reached === true,
    best_full_series: best.map(String),
    principal_variation: Array.isArray(raw?.principal_variation)
      ? raw.principal_variation
      : Array.isArray(raw?.pv) ? raw.pv : [],
    score: Number(raw?.score ?? 0),
    stats,
    work: Number(raw?.work ?? stats.generation_positions ?? 0),
    request_id: request.request_id,
  };
}

async function importVerifiedModuleBytes(moduleBytes) {
  if (
    typeof globalThis.Blob !== "function"
    || typeof globalThis.URL?.createObjectURL !== "function"
    || typeof globalThis.URL?.revokeObjectURL !== "function"
  ) {
    throw new KernelAdapterError(
      "This browser cannot execute the verified WebAssembly module wrapper.",
      "browser-verified-module-unavailable",
    );
  }
  const objectUrl = globalThis.URL.createObjectURL(new Blob(
    [moduleBytes],
    { type: "text/javascript" },
  ));
  try {
    return await import(objectUrl);
  } catch (cause) {
    throw new KernelAdapterError(
      `The verified WebAssembly module wrapper could not load: ${cause?.message || cause}`,
      "browser-module-invalid",
    );
  } finally {
    globalThis.URL.revokeObjectURL(objectUrl);
  }
}

export async function loadCertifiedBrowserKernel({
  expectedSourceFingerprint,
  manifestUrl = new URL("./engine/browser-engine-manifest.json", import.meta.url),
  moduleImporter = importVerifiedModuleBytes,
} = {}) {
  if (
    expectedSourceFingerprint !== undefined
    && expectedSourceFingerprint !== null
    && !SOURCE_FINGERPRINT.test(String(expectedSourceFingerprint || ""))
  ) {
    throw new KernelAdapterError(
      "The server did not provide a valid source identity for the browser engine.",
      "browser-source-fingerprint-invalid",
    );
  }
  const manifestResponse = await fetchRequired(manifestUrl, "manifest");
  let rawManifest;
  try {
    rawManifest = await manifestResponse.json();
  } catch {
    throw new KernelAdapterError(
      "The browser engine manifest is not valid JSON.",
      "browser-manifest-invalid",
    );
  }
  const manifest = validateManifest(rawManifest, expectedSourceFingerprint);
  // Pthread wrappers and bootstrap workers require their own exact-byte
  // execution boundary. Keep them unselectable until that boundary exists.
  const runtimeVariant = "single";
  const variant = manifest.variants[runtimeVariant];
  const engineRoot = new URL(`./${runtimeVariant}/`, manifestUrl);
  const wasmUrl = new URL(variant.wasm, engineRoot);
  const moduleUrl = new URL(variant.module_js, engineRoot);
  wasmUrl.searchParams.set("sha256", variant.wasm_sha256);
  moduleUrl.searchParams.set("sha256", variant.module_js_sha256);
  if (wasmUrl.origin !== manifestUrl.origin || moduleUrl.origin !== manifestUrl.origin) {
    throw new KernelAdapterError(
      "Browser engine artifacts must be same-origin.",
      "browser-artifact-origin-denied",
    );
  }
  const supportUrls = variant.support_files.map((item) => ({
    ...item,
    url: new URL(item.name, engineRoot),
  })).map((item) => {
    item.url.searchParams.set("sha256", item.sha256);
    return item;
  });
  const [wasmResponse, moduleResponse, ...supportResponses] = await Promise.all([
    fetchRequired(wasmUrl, "WebAssembly binary"),
    fetchRequired(moduleUrl, "module wrapper"),
    ...supportUrls.map((item) => fetchRequired(item.url, "thread support module")),
  ]);
  const [wasmBytes, moduleBytes, ...supportBytes] = await Promise.all([
    wasmResponse.arrayBuffer(),
    moduleResponse.arrayBuffer(),
    ...supportResponses.map((response) => response.arrayBuffer()),
  ]);
  const [actualWasmHash, actualModuleHash, ...actualSupportHashes] = await Promise.all([
    sha256Hex(wasmBytes),
    sha256Hex(moduleBytes),
    ...supportBytes.map((bytes) => sha256Hex(bytes)),
  ]);
  if (
    actualWasmHash !== variant.wasm_sha256
    || actualModuleHash !== variant.module_js_sha256
    || actualSupportHashes.some(
      (hash, index) => hash !== supportUrls[index].sha256,
    )
  ) {
    throw new KernelAdapterError(
      "A browser engine artifact failed its SHA-256 identity check.",
      "browser-artifact-hash-mismatch",
    );
  }

  if (typeof moduleImporter !== "function") {
    throw new KernelAdapterError(
      "The verified WebAssembly module importer is unavailable.",
      "browser-verified-module-unavailable",
    );
  }
  const imported = await moduleImporter(moduleBytes, moduleUrl);
  if (typeof imported.default !== "function") {
    throw new KernelAdapterError(
      "The browser engine module has no Emscripten factory.",
      "browser-module-invalid",
    );
  }
  const module = await imported.default({
    wasmBinary: wasmBytes,
    locateFile: (path) => {
      if (path === variant.wasm) return wasmUrl.href;
      const support = supportUrls.find((item) => item.name === path);
      if (support) return support.url.href;
      throw new KernelAdapterError(
        `The browser module requested an uncertified support file: ${path}`,
        "browser-support-file-uncertified",
      );
    },
  });
  const initialMemoryBytes = validateRuntimeMemory(module, variant.memory_limits, {
    initial: true,
  });
  if (
    typeof module?._spc_start_kernel_abi_version !== "function"
    || module._spc_start_kernel_abi_version() !== manifest.abi_version
    || typeof module?.stringToNewUTF8 !== "function"
    || typeof module?._free !== "function"
    || typeof module?.UTF8ToString !== "function"
    || (variant.analysis_ready
      && typeof module?._spc_boundary_kernel_search_json !== "function")
    || (variant.prefix_ready && (
      typeof module?._spc_boundary_prefix_json !== "function"
      || typeof module?._spc_boundary_prefix_contract_json !== "function"
    ))
  ) {
    throw new KernelAdapterError(
      "The browser engine module does not implement ABI version 1.",
      "browser-abi-mismatch",
    );
  }
  if (variant.prefix_ready) {
    validateNativePrefixContract(module, variant.prefix_contract);
  }

  const safetyCertificate = variant.analysis_certificate?.certificate ?? null;
  const prefixCertificate = variant.prefix_capability?.certificate ?? null;
  const engineIdentity = variant.engine_identity;
  const identity = Object.freeze({
    certificate_schema: safetyCertificate?.schema ?? null,
    certificate_status: safetyCertificate?.status ?? null,
    contract_version: manifest.contract_version,
    abi_version: manifest.abi_version,
    source_fingerprint: manifest.source_fingerprint,
    wasm_sha256: variant.wasm_sha256,
    module_js_sha256: variant.module_js_sha256,
    runtime_variant: runtimeVariant,
    thread_count: variant.thread_count,
    analysis_ready: variant.analysis_ready,
    prefix_ready: variant.prefix_ready,
    safety_certified: variant.analysis_ready,
    certificate_id: safetyCertificate?.certificate_id ?? null,
    prefix_certificate_id: prefixCertificate?.certificate_id ?? null,
    engine_profile_id: safetyCertificate?.engine?.engine_profile_id ?? null,
    engine_profile_name: safetyCertificate?.engine?.engine_profile_name ?? null,
    engine_version: engineIdentity.engine_version,
    ruleset_version: engineIdentity.ruleset_version,
    analysis_limits: variant.analysis_limits
      ? Object.freeze({ ...variant.analysis_limits })
      : null,
    prefix_contract: variant.prefix_contract,
    memory_limits: Object.freeze({ ...variant.memory_limits }),
    initial_memory_bytes: initialMemoryBytes,
  });
  return Object.freeze({
    identity,
    analyze(request) {
      if (!identity.analysis_ready) {
        throw new KernelAdapterError(
          "This browser artifact has no certified search capability.",
          "browser-analysis-unavailable",
        );
      }
      validateKernelRequest(request, identity);
      const timeLimitMs = Math.max(
        1,
        Math.min(0xffffffff, Math.round(request.limits.time_limit_seconds * 1000)),
      );
      const started = performance.now();
      const epTargets = request.boundary.ep_targets.join(",") || "-";
      const allocated = [];
      let pointer;
      try {
        for (const value of [
          request.boundary.fen,
          epTargets,
          request.boundary.promoted_hex,
        ]) {
          const allocatedValue = module.stringToNewUTF8(value);
          if (!allocatedValue) {
            throw new KernelAdapterError(
              "The browser engine could not allocate its boundary request.",
              "browser-kernel-allocation-failed",
            );
          }
          allocated.push(allocatedValue);
        }
        pointer = module._spc_boundary_kernel_search_json(
          allocated[0],
          request.boundary.series,
          request.boundary.quiet_series,
          allocated[1],
          allocated[2],
          request.limits.depth,
          request.limits.max_series,
          request.limits.max_generation_positions,
          timeLimitMs,
        );
      } finally {
        allocated.forEach((value) => module._free(value));
      }
      if (!pointer) {
        throw new KernelAdapterError(
          "The browser search kernel returned a null result.",
          "browser-kernel-null-result",
        );
      }
      let raw;
      try {
        raw = JSON.parse(module.UTF8ToString(pointer));
      } catch {
        throw new KernelAdapterError(
          "The browser search kernel returned invalid JSON.",
          "browser-kernel-invalid-json",
        );
      }
      const normalized = normalizeKernelResult(raw, request, identity);
      normalized.memory_bytes = validateRuntimeMemory(module, identity.memory_limits);
      normalized.elapsed_seconds = Math.max(0, (performance.now() - started) / 1000);
      return normalized;
    },
    inspectPrefix(request) {
      validatePrefixKernelRequest(request, identity);
      const epTargets = request.boundary.ep_targets.join(",") || "-";
      const prefix = request.prefix.join("/");
      const allocated = [];
      let pointer;
      try {
        for (const value of [
          request.boundary.fen,
          epTargets,
          request.boundary.promoted_hex,
          prefix,
        ]) {
          const allocatedValue = module.stringToNewUTF8(value);
          if (!allocatedValue) {
            throw new KernelAdapterError(
              "The browser engine could not allocate its prefix request.",
              "browser-prefix-allocation-failed",
            );
          }
          allocated.push(allocatedValue);
        }
        pointer = module._spc_boundary_prefix_json(
          allocated[0],
          request.boundary.series,
          request.boundary.quiet_series,
          allocated[1],
          allocated[2],
          allocated[3],
        );
      } finally {
        allocated.forEach((value) => module._free(value));
      }
      if (!pointer) {
        throw new KernelAdapterError(
          "The browser prefix ABI returned a null result.",
          "browser-prefix-null-result",
        );
      }
      let raw;
      try {
        raw = JSON.parse(module.UTF8ToString(pointer));
      } catch {
        throw new KernelAdapterError(
          "The browser prefix ABI returned invalid JSON.",
          "browser-prefix-invalid-json",
        );
      }
      if (
        !raw
        || typeof raw !== "object"
        || Array.isArray(raw)
        || raw.schema !== PREFIX_RESULT_SCHEMA
        || raw.abi_version !== 1
        || raw.ok !== true
        || raw.status !== "complete"
      ) {
        throw new KernelAdapterError(
          String(raw?.message || "The compiled prefix replay was not authoritative."),
          "browser-prefix-native-rejected",
        );
      }
      return {
        ...raw,
        request_id: request.request_id,
        source_fingerprint: identity.source_fingerprint,
        wasm_sha256: identity.wasm_sha256,
        module_js_sha256: identity.module_js_sha256,
        certificate_id: identity.prefix_certificate_id,
        engine_version: identity.engine_version,
        ruleset_version: identity.ruleset_version,
        runtime_variant: identity.runtime_variant,
        thread_count: identity.thread_count,
        memory_bytes: validateRuntimeMemory(module, identity.memory_limits),
      };
    },
  });
}

export {
  KernelAdapterError,
  importVerifiedModuleBytes,
  normalizeKernelResult,
  validatePrefixContract,
  validateManifest,
};
