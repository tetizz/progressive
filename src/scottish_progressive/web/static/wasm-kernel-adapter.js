const MANIFEST_SCHEMA = "spc-browser-wasm-manifest-v1";
const CERTIFICATE_SCHEMA = "spc-browser-wasm-certificate-v1";
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
  const certificate = value.safety_certificate;
  const evidence = certificate?.evidence;
  const engine = certificate?.engine;
  const limits = validateAnalysisLimits(engine?.analysis_limits);
  const memory = validateMemoryLimits(certificate?.memory);
  if (
    !certificate
    || certificate.schema !== CERTIFICATE_SCHEMA
    || certificate.status !== "certified"
    || certificate.safety_certified !== true
    || certificate.contract_version !== 1
    || certificate.abi_version !== 1
    || certificate.source_fingerprint !== sourceFingerprint
    || certificate.runtime_variant !== name
    || certificate.thread_count !== threadCount
    || certificate.wasm_sha256 !== value.wasm_sha256
    || certificate.module_js_sha256 !== value.module_js_sha256
    || typeof certificate.certificate_id !== "string"
    || !certificate.certificate_id
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
      `The ${name} WebAssembly artifact has no matching safety certificate.`,
      "browser-kernel-not-certified",
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
  if (
    name === "single"
    && supportFiles.length !== 0
  ) {
    throw new KernelAdapterError(
      "The single-thread WebAssembly lane may not load external support code.",
      "browser-support-file-uncertified",
    );
  }
  if (
    !Array.isArray(certificate.support_files)
    || JSON.stringify(certificate.support_files) !== JSON.stringify(supportFiles)
  ) {
    throw new KernelAdapterError(
      `The ${name} WebAssembly support files do not match their certificate.`,
      "browser-kernel-not-certified",
    );
  }
  return {
    ...value,
    thread_count: threadCount,
    wasm: safeAssetName(value.wasm, ".wasm"),
    module_js: safeAssetName(value.module_js, ".js"),
    support_files: supportFiles,
    analysis_limits: limits,
    memory_limits: memory,
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
    || typeof module?._spc_boundary_kernel_search_json !== "function"
    || typeof module?.stringToNewUTF8 !== "function"
    || typeof module?._free !== "function"
    || typeof module?.UTF8ToString !== "function"
  ) {
    throw new KernelAdapterError(
      "The browser engine module does not implement ABI version 1.",
      "browser-abi-mismatch",
    );
  }

  const identity = Object.freeze({
    certificate_schema: variant.safety_certificate.schema,
    certificate_status: variant.safety_certificate.status,
    contract_version: variant.safety_certificate.contract_version,
    abi_version: variant.safety_certificate.abi_version,
    source_fingerprint: manifest.source_fingerprint,
    wasm_sha256: variant.wasm_sha256,
    module_js_sha256: variant.module_js_sha256,
    runtime_variant: runtimeVariant,
    thread_count: variant.thread_count,
    safety_certified: true,
    certificate_id: variant.safety_certificate.certificate_id,
    engine_profile_id: variant.safety_certificate.engine.engine_profile_id,
    engine_profile_name: variant.safety_certificate.engine.engine_profile_name,
    engine_version: variant.safety_certificate.engine.engine_version,
    ruleset_version: variant.safety_certificate.engine.ruleset_version,
    analysis_limits: Object.freeze({ ...variant.analysis_limits }),
    memory_limits: Object.freeze({ ...variant.memory_limits }),
    initial_memory_bytes: initialMemoryBytes,
  });
  return Object.freeze({
    identity,
    analyze(request) {
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
  });
}

export {
  KernelAdapterError,
  importVerifiedModuleBytes,
  normalizeKernelResult,
  validateManifest,
};
