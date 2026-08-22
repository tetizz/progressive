const MANIFEST_SCHEMA = "spc-browser-wasm-manifest-v1";
const CERTIFICATE_SCHEMA = "spc-browser-wasm-certificate-v1";
const PREFIX_CONTRACT_SCHEMA = "spc-boundary-prefix-contract-v1";
const PREFIX_RESULT_SCHEMA = "spc-boundary-prefix-v1";
const ROOT_SESSION_CERTIFICATE_SCHEMA = "spc-root-session-certificate-v1";
const MATE_CERTIFICATE_SCHEMA = "spc-series-mate-certificate-v1";
const ROOT_SESSION_ABI_VERSION = 2;
const MATE_ABI_VERSION = 1;
const ROOT_TACTICAL_POLICY = "canonical-boundary-policy-v1";
const COMBINED_EXPORTS = Object.freeze([
  "_spc_start_kernel_search_json",
  "_spc_boundary_kernel_search_json",
  "_spc_boundary_prefix_json",
  "_spc_boundary_prefix_contract_json",
  "_spc_start_kernel_abi_version",
  "_spc_root_session_contract_json",
  "_spc_root_session_create_json",
  "_spc_root_session_enumerate_json",
  "_spc_root_session_import_json",
  "_spc_root_session_search_json",
  "_spc_root_session_destroy",
  "_spc_root_session_abi_version",
  "_spc_series_mate_search_json",
  "_spc_series_mate_abi_version",
  "_malloc",
  "_free",
]);
const ROOT_SESSION_WEIGHT_KEYS = Object.freeze([
  "boundary_check",
  "immediate_vulnerability",
  "king_space",
  "material",
  "promotion_corridors",
  "series_reach",
  "useful_mobility",
]);
const ROOT_SESSION_IDENTITY_KEYS = Object.freeze([
  "certificate_id",
  "engine_version",
  "kernel_sha256",
  "module_js_sha256",
  "profile_id",
  "ruleset_version",
  "runtime_variant",
  "source_fingerprint",
  "thread_count",
]);
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

function canonicalJsonValue(value) {
  if (Array.isArray(value)) return value.map(canonicalJsonValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonicalJsonValue(value[key])]),
    );
  }
  return value;
}

function sameJson(left, right) {
  return JSON.stringify(canonicalJsonValue(left))
    === JSON.stringify(canonicalJsonValue(right));
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

function validateCombinedRuntimeIdentity(certificate) {
  const exceptionStrategy = certificate?.exception_strategy;
  const wasmSimd = certificate?.wasm_simd;
  const allocator = certificate?.allocator;
  const requirements = certificate?.runtime_requirements;
  const requirementKeys = [
    "cross_origin_isolated",
    "native_wasm_exception_handling",
    "ordinary_module_worker",
    "pthreads",
    "wasm_simd",
  ];
  if (
    !["emscripten", "wasm"].includes(exceptionStrategy)
    || typeof wasmSimd !== "boolean"
    || !["dlmalloc", "emmalloc"].includes(allocator)
    || !requirements
    || typeof requirements !== "object"
    || Array.isArray(requirements)
    || !sameJson(Object.keys(requirements).sort(), requirementKeys)
    || requirements.ordinary_module_worker !== true
    || requirements.pthreads !== false
    || requirements.cross_origin_isolated !== false
    || requirements.native_wasm_exception_handling !== (exceptionStrategy === "wasm")
    || requirements.wasm_simd !== wasmSimd
  ) return null;
  return Object.freeze({
    exception_strategy: exceptionStrategy,
    wasm_simd: wasmSimd,
    allocator,
    runtime_requirements: Object.freeze({
      ordinary_module_worker: true,
      pthreads: false,
      cross_origin_isolated: false,
      native_wasm_exception_handling: exceptionStrategy === "wasm",
      wasm_simd: wasmSimd,
    }),
  });
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

function validateRootSessionConfig(value, contract) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const expectedKeys = [
    "external_cache_weight", "mate_score", "max_depth", "max_work",
    "root_contract_eval_capacity", "root_contract_tt_capacity",
    "root_tactical_protection", "series_cache_capacity", "weights",
    "width", "worker_threads",
  ];
  const hard = contract?.hard_limits;
  if (
    !sameJson(Object.keys(value).sort(), expectedKeys)
    || !hard
    || typeof hard !== "object"
    || Array.isArray(hard)
  ) return null;
  const hardInteger = (name) => Number.isInteger(hard[name]) && hard[name] >= 1
    ? hard[name]
    : null;
  const maximumDepth = hardInteger("maximum_depth");
  const maximumWidth = hardInteger("maximum_width");
  const maximumCache = hardInteger("maximum_series_cache_capacity");
  const maximumTt = hardInteger("maximum_tt_capacity");
  const maximumEval = hardInteger("maximum_eval_capacity");
  const maximumWorkers = hardInteger("worker_threads");
  const bounded = (candidate, minimum, maximum) => Number.isSafeInteger(candidate)
    && candidate >= minimum
    && candidate <= maximum;
  if (
    maximumDepth === null
    || maximumWidth === null
    || maximumCache === null
    || maximumTt === null
    || maximumEval === null
    || maximumWorkers === null
    || value.max_depth !== 5
    || !bounded(value.max_depth, 1, maximumDepth)
    || value.width !== 32
    || !bounded(value.width, 1, maximumWidth)
    || !bounded(value.max_work, 1, Number.MAX_SAFE_INTEGER)
    || !bounded(value.mate_score, 1, 1_000_000_000)
    || !bounded(value.series_cache_capacity, 1, maximumCache)
    || !bounded(value.external_cache_weight, 0, value.series_cache_capacity)
    || value.worker_threads !== 1
    || !bounded(value.worker_threads, 1, maximumWorkers)
    || !bounded(value.root_contract_tt_capacity, 1, maximumTt)
    || !bounded(value.root_contract_eval_capacity, 1, maximumEval)
    || value.root_tactical_protection !== false
    || !sameJson(hard.root_tactical_protection_values, [false])
    || hard.root_tactical_policy !== ROOT_TACTICAL_POLICY
    || !value.weights
    || typeof value.weights !== "object"
    || Array.isArray(value.weights)
    || !sameJson(Object.keys(value.weights).sort(), ROOT_SESSION_WEIGHT_KEYS)
    || Object.values(value.weights).some((weight) => (
      !Number.isSafeInteger(weight) || weight < 25 || weight > 300
    ))
  ) return null;
  return Object.freeze({
    ...value,
    weights: Object.freeze({ ...value.weights }),
  });
}

function canonicalRootPolicyMatches(value, expectedProtection = null) {
  return Boolean(
    value
    && value.canonical_root_tactical_policy === ROOT_TACTICAL_POLICY
    && typeof value.canonical_root_tactical_protection === "boolean"
    && (
      expectedProtection === null
      || value.canonical_root_tactical_protection === expectedProtection
    )
  );
}

function canonicalRootTacticalProtection(boundary) {
  const series = boundary?.series;
  const board = typeof boundary?.fen === "string"
    ? boundary.fen.split(" ")[0]
    : null;
  const ranks = board?.split("/");
  if (!Number.isInteger(series) || !Array.isArray(ranks) || ranks.length !== 8) return null;
  if (series >= 5) return true;
  const white = series % 2 === 1;
  const pawn = white ? "P" : "p";
  for (let row = 0; row < ranks.length; row += 1) {
    const expanded = ranks[row].replace(/[1-8]/g, (digit) => " ".repeat(Number(digit)));
    if (expanded.length !== 8) return null;
    const distance = white ? row : 7 - row;
    if (distance > 0 && series - distance >= 2 && expanded.includes(pawn)) return true;
  }
  return false;
}

function validateRootPlayLimits(value, config) {
  if (!value || typeof value !== "object" || Array.isArray(value) || !config) return null;
  const expectedKeys = [
    "default_generation_positions", "default_seconds", "maximum_seconds",
    "safety_reserve_positions",
  ];
  if (
    !sameJson(Object.keys(value).sort(), expectedKeys)
    || !Number.isFinite(value.maximum_seconds)
    || value.maximum_seconds <= 0
    || value.maximum_seconds > 0xffffffff / 1000
    || !Number.isFinite(value.default_seconds)
    || value.default_seconds <= 0
    || value.default_seconds > value.maximum_seconds
    || !Number.isSafeInteger(value.default_generation_positions)
    || value.default_generation_positions < 1_000
    || value.default_generation_positions > config.max_work
    || !Number.isSafeInteger(value.safety_reserve_positions)
    || value.safety_reserve_positions < 1
    || value.safety_reserve_positions > config.max_work
  ) return null;
  return Object.freeze({ ...value });
}

function validateRootGeometry(value, memory, contract) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const expectedKeys = [
    "aggregate_maximum_bytes",
    "desktop_initial_full_wave",
    "desktop_workers",
    "play_limits",
    "session_config",
    "supported_lower_geometries",
  ];
  if (!sameJson(Object.keys(value).sort(), expectedKeys)) return null;
  const config = validateRootSessionConfig(value.session_config, contract);
  const playLimits = validateRootPlayLimits(value.play_limits, config);
  if (
    !Number.isInteger(value.desktop_workers)
    || value.desktop_workers !== 8
    || !Number.isInteger(value.desktop_initial_full_wave)
    || value.desktop_initial_full_wave !== 4
    || value.aggregate_maximum_bytes !== value.desktop_workers * memory.maximum_bytes
    || !Array.isArray(value.supported_lower_geometries)
    || !config
    || !playLimits
  ) return null;
  let lastWorkers = value.desktop_workers;
  for (const geometry of value.supported_lower_geometries) {
    if (
      !geometry
      || typeof geometry !== "object"
      || Array.isArray(geometry)
      || !sameJson(
        Object.keys(geometry).sort(),
        ["aggregate_maximum_bytes", "initial_full_wave", "workers"],
      )
      || !Number.isInteger(geometry.workers)
      || geometry.workers < 1
      || geometry.workers >= lastWorkers
      || !Number.isInteger(geometry.initial_full_wave)
      || geometry.initial_full_wave < 1
      || geometry.initial_full_wave > geometry.workers
      || geometry.aggregate_maximum_bytes !== geometry.workers * memory.maximum_bytes
    ) return null;
    lastWorkers = geometry.workers;
  }
  return Object.freeze({
    ...value,
    play_limits: playLimits,
    session_config: config,
    supported_lower_geometries: Object.freeze(
      value.supported_lower_geometries.map((geometry) => Object.freeze({ ...geometry })),
    ),
  });
}

function validateRootSessionCertificate(certificate, value, context) {
  if (certificate === undefined || certificate === null) return null;
  const evidence = certificate?.evidence;
  const engine = certificate?.engine;
  const memory = validateMemoryLimits(certificate?.memory);
  const contract = certificate?.root_session_contract;
  const exportsList = certificate?.exports;
  const runtime = validateCombinedRuntimeIdentity(certificate);
  const geometry = memory && contract
    ? validateRootGeometry(certificate?.geometry, memory, contract)
    : null;
  const requiredEvidence = [
    "deterministic_node_smoke", "combined_artifact", "enumerate_import_search",
    "exact_manifest_import", "persistent_d1_d2_session",
    "cumulative_work_and_cache_receipts", "configured_max_depth_rejected",
    "per_call_work_credit", "deadline_fail_closed", "work_limit_fail_closed",
    "browser_worker_smoke", "opera_worker_smoke",
    "selected_owner_warm_exact_certification",
  ];
  if (
    !certificateMatchesArtifact(certificate, value, context)
    || certificate.schema !== ROOT_SESSION_CERTIFICATE_SCHEMA
    || certificate.abi_version !== ROOT_SESSION_ABI_VERSION
    || certificate.root_session_certified !== true
    || certificate.reply_mate_safety !== false
    || certificate.product_publishable !== false
    || !SHA256.test(String(certificate.kernel_sha256 || ""))
    || certificate.kernel_sha256 !== value.kernel_sha256
    || !runtime
    || !Array.isArray(exportsList)
    || !sameJson(exportsList, COMBINED_EXPORTS)
    || !contract
    || typeof contract !== "object"
    || Array.isArray(contract)
    || contract.abi_version !== ROOT_SESSION_ABI_VERSION
    || contract.reply_mate_safety !== false
    || contract.product_publishable !== false
    || contract.capabilities?.selected_owner_certification !== true
    || contract.capabilities?.canonical_root_tactical_policy !== true
    || !geometry
    || !evidence
    || evidence.failures !== 0
    || !Number.isInteger(evidence.differential_cases)
    || evidence.differential_cases < 1
    || requiredEvidence.some((key) => evidence[key] !== true)
    || evidence.start_w32_d5_completed_depth !== 5
    || evidence.start_w32_d5_width !== 32
    || !Number.isFinite(Number(evidence.start_w32_d5_elapsed_seconds))
    || Number(evidence.start_w32_d5_elapsed_seconds) < 0
    || Number(evidence.start_w32_d5_elapsed_seconds) >= 60
    || !engine
    || !["engine_version", "ruleset_version", "profile_id"].every(
      (key) => typeof engine[key] === "string" && Boolean(engine[key]),
    )
    || !memory
  ) {
    throw new KernelAdapterError(
      `The ${context.name} artifact has no matching root-session certificate.`,
      "browser-root-session-uncertified",
    );
  }
  return { certificate, contract, engine, geometry, memory, runtime };
}

function validateMateCertificate(certificate, value, context) {
  if (certificate === undefined || certificate === null) return null;
  const evidence = certificate?.evidence;
  const engine = certificate?.engine;
  const memory = validateMemoryLimits(certificate?.memory);
  const exportsList = certificate?.exports;
  const runtime = validateCombinedRuntimeIdentity(certificate);
  const requiredEvidence = [
    "combined_artifact", "python_parity", "authoritative_replay", "white_found",
    "black_found", "exhausted", "work_limit_unknown", "deadline_unknown",
    "signed_mate_distance_overrides", "proof_bounds", "work_receipts",
    "deadline_receipts", "browser_worker_smoke",
  ];
  if (
    !certificateMatchesArtifact(certificate, value, context)
    || certificate.schema !== MATE_CERTIFICATE_SCHEMA
    || certificate.abi_version !== MATE_ABI_VERSION
    || certificate.mate_capability_certified !== true
    || certificate.reply_mate_safety !== true
    || certificate.product_publishable !== false
    || certificate.kernel_sha256 !== value.kernel_sha256
    || !runtime
    || !Array.isArray(exportsList)
    || !sameJson(exportsList, COMBINED_EXPORTS)
    || !evidence
    || evidence.failures !== 0
    || !Number.isInteger(evidence.differential_cases)
    || evidence.differential_cases < 5
    || requiredEvidence.some((key) => evidence[key] !== true)
    || !engine
    || !["engine_version", "ruleset_version", "profile_id"].every(
      (key) => typeof engine[key] === "string" && Boolean(engine[key]),
    )
    || !memory
  ) {
    throw new KernelAdapterError(
      `The ${context.name} artifact has no matching compiled mate certificate.`,
      "browser-root-mate-uncertified",
    );
  }
  return { certificate, engine, memory, runtime };
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
    || ((value.root_session_certificate || value.mate_certificate)
      && !SHA256.test(String(value.kernel_sha256 || "")))
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
  const rootSession = validateRootSessionCertificate(
    value.root_session_certificate,
    value,
    context,
  );
  const mate = validateMateCertificate(value.mate_certificate, value, context);
  if ((rootSession === null) !== (mate === null)) {
    throw new KernelAdapterError(
      "Root-session and compiled-mate certificates must be paired.",
      "browser-root-certificate-pair-invalid",
    );
  }
  if (rootSession && mate && (
    rootSession.certificate.kernel_sha256 !== mate.certificate.kernel_sha256
    || rootSession.engine.profile_id !== mate.engine.profile_id
    || !sameJson(rootSession.runtime, mate.runtime)
  )) {
    throw new KernelAdapterError(
      "Root-session and compiled-mate certificates have different runtime identities.",
      "browser-root-certificate-pair-invalid",
    );
  }
  if (!analysis && !prefix && !rootSession && !mate) {
    throw new KernelAdapterError(
      `The ${name} WebAssembly artifact has no certified capability.`,
      "browser-kernel-not-certified",
    );
  }
  const capabilities = [analysis, prefix, rootSession, mate].filter(Boolean);
  if (capabilities.length > 1) {
    if (capabilities.some((capability) => !sameJson(capability.memory, capabilities[0].memory))) {
      throw new KernelAdapterError(
        "Browser capability certificates have different memory envelopes.",
        "browser-memory-envelope-mismatch",
      );
    }
    if (capabilities.some((capability) => (
      capability.engine.engine_version !== capabilities[0].engine.engine_version
      || capability.engine.ruleset_version !== capabilities[0].engine.ruleset_version
    ))) {
      throw new KernelAdapterError(
        "Browser capability certificates have different engine identities.",
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
    root_session_ready: rootSession !== null,
    mate_ready: mate !== null,
    root_iteration_ready: rootSession !== null && mate !== null && prefix !== null,
    analysis_certificate: analysis,
    prefix_capability: prefix,
    root_session_capability: rootSession,
    mate_capability: mate,
    analysis_limits: analysis?.limits ?? null,
    prefix_contract: prefix?.contract ?? null,
    memory_limits: capabilities[0]?.memory ?? null,
    engine_identity: capabilities[0]?.engine ?? null,
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

function validateNativeRootSessionContract(module, certifiedContract) {
  let pointer;
  try {
    pointer = module._spc_root_session_contract_json();
  } catch (cause) {
    throw new KernelAdapterError(
      `The compiled root-session contract could not be read: ${cause?.message || cause}`,
      "browser-root-session-abi-mismatch",
    );
  }
  if (!pointer) {
    throw new KernelAdapterError(
      "The compiled root-session contract returned a null pointer.",
      "browser-root-session-abi-mismatch",
    );
  }
  let nativeContract;
  try {
    nativeContract = JSON.parse(module.UTF8ToString(pointer));
  } catch {
    nativeContract = null;
  }
  if (
    !nativeContract
    || typeof nativeContract !== "object"
    || Array.isArray(nativeContract)
    || nativeContract.abi_version !== ROOT_SESSION_ABI_VERSION
    || nativeContract.reply_mate_safety !== false
    || nativeContract.product_publishable !== false
    || !sameJson(nativeContract, certifiedContract)
  ) {
    throw new KernelAdapterError(
      "The compiled root-session ABI differs from its certified contract.",
      "browser-root-session-abi-mismatch",
    );
  }
  return Object.freeze(nativeContract);
}

function parseFacadeJson(module, pointer, label, code) {
  if (!pointer) {
    throw new KernelAdapterError(`${label} returned a null result.`, code);
  }
  try {
    const value = JSON.parse(module.UTF8ToString(pointer));
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
    return value;
  } catch {
    throw new KernelAdapterError(`${label} returned invalid JSON.`, code);
  }
}

function withJsonArgument(module, value, invoke, label) {
  const encoded = JSON.stringify(value);
  const pointer = module.stringToNewUTF8(encoded);
  if (!pointer) {
    throw new KernelAdapterError(
      `${label} could not allocate its JSON request.`,
      "browser-root-allocation-failed",
    );
  }
  try {
    return invoke(pointer, utf8Length(encoded));
  } finally {
    module._free(pointer);
  }
}

function rootIdentityEnvelope(identity) {
  return {
    source_fingerprint: identity.source_fingerprint,
    kernel_sha256: identity.kernel_sha256,
    module_js_sha256: identity.module_js_sha256,
    certificate_id: identity.root_session_certificate_id,
    runtime_variant: identity.runtime_variant,
    thread_count: identity.thread_count,
    engine_version: identity.engine_version,
    ruleset_version: identity.ruleset_version,
    profile_id: identity.profile_id,
  };
}

function nativeRootRequest(request, identity, schema, fields) {
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    throw new KernelAdapterError(
      "The native root-session request is not a JSON object.",
      "browser-root-request-invalid",
    );
  }
  const {
    session_id: _sessionId,
    deadline_epoch_ms: _deadlineEpochMs,
    ...nativeRequestValue
  } = request;
  const expectedKeys = [
    "generation",
    "iteration_id",
    "request_id",
    "schema",
    ...ROOT_SESSION_IDENTITY_KEYS,
    ...fields,
  ].sort();
  if (
    nativeRequestValue.schema !== schema
    || !sameJson(Object.keys(nativeRequestValue).sort(), expectedKeys)
    || typeof nativeRequestValue.request_id !== "string"
    || !nativeRequestValue.request_id
    || typeof nativeRequestValue.iteration_id !== "string"
    || !nativeRequestValue.iteration_id
    || !Number.isSafeInteger(nativeRequestValue.generation)
    || nativeRequestValue.generation < 0
    || ROOT_SESSION_IDENTITY_KEYS.some((key) => (
      nativeRequestValue[key] !== rootIdentityEnvelope(identity)[key]
    ))
  ) {
    throw new KernelAdapterError(
      "The native root-session request differs from its exact certified schema.",
      "browser-root-request-invalid",
    );
  }
  return nativeRequestValue;
}

function validateRootIdentityEcho(raw, request, identity, expectedSchema) {
  const expectedIdentity = rootIdentityEnvelope(identity);
  if (
    raw?.schema !== expectedSchema
    || raw.abi_version !== ROOT_SESSION_ABI_VERSION
    || raw.request_id !== request.request_id
    || raw.iteration_id !== request.iteration_id
    || raw.generation !== request.generation
    || ROOT_SESSION_IDENTITY_KEYS.some((key) => raw?.[key] !== expectedIdentity[key])
    || raw.product_publishable !== false
    || raw.safety_certified !== false
  ) {
    throw new KernelAdapterError(
      "The native root-session reply did not echo its exact certified identity.",
      "browser-root-reply-identity-invalid",
    );
  }
}

function clampRootRemainingTime(request) {
  if (
    typeof globalThis.performance?.now !== "function"
    || !Number.isFinite(globalThis.performance.timeOrigin)
  ) {
    throw new KernelAdapterError(
      "The browser has no monotonic clock for the certified root deadline.",
      "browser-root-clock-unavailable",
    );
  }
  if (
    !Number.isSafeInteger(request.remaining_time_ms)
    || request.remaining_time_ms < 0
    || !Number.isFinite(request.deadline_monotonic_ms)
    || !Number.isFinite(request.deadline_epoch_ms)
  ) {
    throw new KernelAdapterError(
      "The native root-session request has no bounded monotonic deadline.",
      "browser-root-deadline-invalid",
    );
  }
  return {
    ...request,
    remaining_time_ms: Math.max(0, Math.min(
      request.remaining_time_ms,
      Math.floor(
        request.deadline_epoch_ms
        - (globalThis.performance.timeOrigin + globalThis.performance.now()),
      ),
      0xffffffff,
    )),
  };
}

function validExactBoundaryState(value) {
  const expectedKeys = [
    "board_fen", "chess960", "ep_targets", "fen", "progressive_ep",
    "promoted_hex", "quiet_draw_pending", "quiet_series", "series",
    "series_number", "side_to_move",
  ];
  const fen = typeof value?.fen === "string" ? value.fen.split(" ") : [];
  return Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
    && sameJson(Object.keys(value).sort(), expectedKeys)
    && fen.length === 6
    && ["w", "b"].includes(fen[1])
    && value.board_fen === value.fen
    && Number.isInteger(value.series)
    && value.series >= 1
    && value.series <= 256
    && value.series_number === value.series
    && value.side_to_move === (fen[1] === "w" ? "white" : "black")
    && ((value.series % 2 === 1 && fen[1] === "w")
      || (value.series % 2 === 0 && fen[1] === "b"))
    && Number.isInteger(value.quiet_series)
    && value.quiet_series >= 0
    && value.quiet_series <= 1_000_000
    && value.quiet_draw_pending === (value.quiet_series >= 10)
    && Array.isArray(value.ep_targets)
    && value.ep_targets.length <= 8
    && value.ep_targets.every((square, index) => (
      typeof square === "string"
      && /^[a-h][1-8]$/.test(square)
      && (index === 0 || value.ep_targets[index - 1] < square)
    ))
    && sameJson(value.progressive_ep, value.ep_targets)
    && PROMOTED_HEX.test(String(value.promoted_hex || ""))
    && value.chess960 === false
  );
}

function exactBoundaryMatchesRequest(value, requestBoundary) {
  return validExactBoundaryState(value)
    && value.fen === requestBoundary?.fen
    && value.series === requestBoundary?.series
    && value.quiet_series === requestBoundary?.quiet_series
    && sameJson(value.ep_targets, requestBoundary?.ep_targets)
    && value.promoted_hex === requestBoundary?.promoted_hex
    && value.chess960 === requestBoundary?.chess960;
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

function browserSupportsWasmSimd() {
  if (typeof globalThis.WebAssembly?.validate !== "function") return false;
  // A minimal module whose function contains i8x16.splat followed by drop.
  const probe = Uint8Array.from([
    0, 97, 115, 109, 1, 0, 0, 0, 1, 4, 1, 96, 0, 0, 3, 2, 1, 0,
    10, 9, 1, 7, 0, 65, 0, 253, 15, 26, 11,
  ]);
  try {
    return globalThis.WebAssembly.validate(probe);
  } catch {
    return false;
  }
}

function validateCertifiedRuntimeSupport(variant) {
  const runtime = variant.root_session_capability?.runtime
    ?? variant.mate_capability?.runtime
    ?? null;
  if (!runtime) return;
  if (
    runtime.runtime_requirements.ordinary_module_worker !== true
    || runtime.runtime_requirements.pthreads !== false
    || runtime.runtime_requirements.cross_origin_isolated !== false
  ) {
    throw new KernelAdapterError(
      "The certified root runtime cannot execute in an ordinary single Worker.",
      "browser-root-runtime-unsupported",
    );
  }
  if (runtime.wasm_simd && !browserSupportsWasmSimd()) {
    throw new KernelAdapterError(
      "This browser does not support the certified WebAssembly SIMD runtime.",
      "browser-root-runtime-unsupported",
    );
  }
  if (
    runtime.exception_strategy === "wasm"
    && (
      typeof globalThis.WebAssembly?.Tag !== "function"
      || typeof globalThis.WebAssembly?.Exception !== "function"
    )
  ) {
    throw new KernelAdapterError(
      "This browser does not support the certified native WebAssembly exception runtime.",
      "browser-root-runtime-unsupported",
    );
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
  validateCertifiedRuntimeSupport(variant);
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
    || (variant.root_session_ready && (
      typeof module?._spc_root_session_abi_version !== "function"
      || module._spc_root_session_abi_version() !== ROOT_SESSION_ABI_VERSION
    ))
    || (variant.mate_ready && (
      typeof module?._spc_series_mate_abi_version !== "function"
      || module._spc_series_mate_abi_version() !== MATE_ABI_VERSION
    ))
    || ((variant.root_session_ready || variant.mate_ready) && (
      COMBINED_EXPORTS.some((name) => typeof module?.[name] !== "function")
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
  if (variant.root_session_ready) {
    validateNativeRootSessionContract(
      module,
      variant.root_session_capability.contract,
    );
  }

  const safetyCertificate = variant.analysis_certificate?.certificate ?? null;
  const prefixCertificate = variant.prefix_capability?.certificate ?? null;
  const rootSessionCertificate = variant.root_session_capability?.certificate ?? null;
  const mateCertificate = variant.mate_capability?.certificate ?? null;
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
    root_session_ready: variant.root_session_ready,
    mate_ready: variant.mate_ready,
    root_iteration_ready: variant.root_iteration_ready,
    safety_certified: variant.analysis_ready,
    certificate_id: safetyCertificate?.certificate_id ?? null,
    prefix_certificate_id: prefixCertificate?.certificate_id ?? null,
    root_session_certificate_id: rootSessionCertificate?.certificate_id ?? null,
    mate_certificate_id: mateCertificate?.certificate_id ?? null,
    kernel_sha256: rootSessionCertificate?.kernel_sha256
      ?? mateCertificate?.kernel_sha256
      ?? null,
    engine_profile_id: safetyCertificate?.engine?.engine_profile_id ?? null,
    engine_profile_name: safetyCertificate?.engine?.engine_profile_name ?? null,
    profile_id: rootSessionCertificate?.engine?.profile_id
      ?? mateCertificate?.engine?.profile_id
      ?? safetyCertificate?.engine?.engine_profile_id
      ?? null,
    engine_version: engineIdentity.engine_version,
    ruleset_version: engineIdentity.ruleset_version,
    analysis_limits: variant.analysis_limits
      ? Object.freeze({ ...variant.analysis_limits })
      : null,
    prefix_contract: variant.prefix_contract,
    root_session_contract: variant.root_session_capability?.contract ?? null,
    root_geometry: variant.root_session_capability?.geometry ?? null,
    memory_limits: Object.freeze({ ...variant.memory_limits }),
    initial_memory_bytes: initialMemoryBytes,
  });
  let rootSessionId = null;
  let rootCanonicalTacticalProtection = null;
  let rootMemoryPeakBytes = initialMemoryBytes;
  const rootMemoryReceipt = () => {
    const memoryBytes = validateRuntimeMemory(module, identity.memory_limits);
    rootMemoryPeakBytes = Math.max(rootMemoryPeakBytes, memoryBytes);
    return { memory_bytes: memoryBytes, memory_peak_bytes: rootMemoryPeakBytes };
  };
  return Object.freeze({
    identity,
    createRootSession(request) {
      if (!identity.root_iteration_ready) {
        throw new KernelAdapterError(
          "This artifact has no complete certified root-session/mate/prefix capability.",
          "browser-root-session-unavailable",
        );
      }
      if (rootSessionId !== null) {
        throw new KernelAdapterError(
          "A native root session is already active in this Worker.",
          "browser-root-session-active",
        );
      }
      const nativeRequest = nativeRootRequest(
        request,
        identity,
        "spc-root-session-create-v1",
        ["boundary", "config"],
      );
      if (
        !sameJson(nativeRequest.config, identity.root_geometry.session_config)
        || !request.boundary
        || request.boundary.chess960 !== false
      ) {
        throw new KernelAdapterError(
          "The root-session create request differs from its certified configuration.",
          "browser-root-session-create-invalid",
        );
      }
      const pointer = withJsonArgument(
        module,
        nativeRequest,
        (jsonPointer, jsonLength) => module._spc_root_session_create_json(
          jsonPointer,
          jsonLength,
        ),
        "The native root-session create ABI",
      );
      const raw = parseFacadeJson(
        module,
        pointer,
        "The native root-session create ABI",
        "browser-root-session-create-invalid",
      );
      validateRootIdentityEcho(
        raw,
        nativeRequest,
        identity,
        "spc-root-session-create-result-v1",
      );
      const expectedCanonicalProtection = canonicalRootTacticalProtection(
        nativeRequest.boundary,
      );
      if (
        raw.status !== "ready"
        || !Number.isInteger(raw.session_id)
        || raw.session_id < 1
        || !exactBoundaryMatchesRequest(raw.boundary, nativeRequest.boundary)
        || !sameJson(raw.config, identity.root_geometry.session_config)
        || raw.configured_max_depth !== identity.root_geometry.session_config.max_depth
        || raw.native_work_after !== 0
        || raw.capabilities?.selected_owner_certification !== true
        || raw.capabilities?.canonical_root_tactical_policy !== true
        || raw.capabilities?.reply_mate_safety !== false
        || expectedCanonicalProtection === null
        || !canonicalRootPolicyMatches(raw, expectedCanonicalProtection)
      ) {
        throw new KernelAdapterError(
          "The native root-session create ABI returned an invalid session envelope.",
          "browser-root-session-create-invalid",
        );
      }
      rootSessionId = raw.session_id;
      rootCanonicalTacticalProtection = raw.canonical_root_tactical_protection;
      return {
        ...raw,
        status: raw.status,
        source_fingerprint: identity.source_fingerprint,
        kernel_sha256: identity.kernel_sha256,
        module_js_sha256: identity.module_js_sha256,
        certificate_id: identity.root_session_certificate_id,
        runtime_variant: identity.runtime_variant,
        thread_count: identity.thread_count,
        engine_version: identity.engine_version,
        ruleset_version: identity.ruleset_version,
        profile_id: identity.profile_id,
        config: identity.root_geometry.session_config,
        native_work_after: Number.isSafeInteger(raw.native_work_after)
          ? raw.native_work_after
          : 0,
        ...rootMemoryReceipt(),
      };
    },
    enumerateRoot(request) {
      if (rootSessionId === null || request?.session_id !== rootSessionId) {
        throw new KernelAdapterError(
          "The native root enumeration used no active session.",
          "browser-root-session-mismatch",
        );
      }
      const nativeRequest = nativeRootRequest(
        clampRootRemainingTime(request),
        identity,
        "spc-root-session-enumerate-v1",
        [
          "call_work_credit", "deadline_monotonic_ms", "external_work",
          "native_work_before", "preferred_series", "remaining_time_ms",
        ],
      );
      const pointer = withJsonArgument(
        module,
        nativeRequest,
        (jsonPointer, jsonLength) => module._spc_root_session_enumerate_json(
          rootSessionId,
          jsonPointer,
          jsonLength,
        ),
        "The native retained-root enumeration ABI",
      );
      const raw = parseFacadeJson(
          module,
          pointer,
          "The native retained-root enumeration ABI",
          "browser-root-enumeration-invalid",
        );
      validateRootIdentityEcho(
        raw,
        nativeRequest,
        identity,
        "spc-root-session-enumeration-result-v1",
      );
      if (!canonicalRootPolicyMatches(raw, rootCanonicalTacticalProtection)) {
        throw new KernelAdapterError(
          "The native root enumeration changed its canonical tactical policy.",
          "browser-root-enumeration-invalid",
        );
      }
      return {
        ...raw,
        ...rootMemoryReceipt(),
      };
    },
    importRoot(request) {
      if (rootSessionId === null || request?.session_id !== rootSessionId) {
        throw new KernelAdapterError(
          "The native root import used no active session.",
          "browser-root-session-mismatch",
        );
      }
      const nativeRequest = nativeRootRequest(
        clampRootRemainingTime(request),
        identity,
        "spc-root-session-import-v1",
        [
          "call_work_credit", "deadline_monotonic_ms", "external_work",
          "manifest", "native_work_before", "remaining_time_ms",
        ],
      );
      const pointer = withJsonArgument(
        module,
        nativeRequest,
        (jsonPointer, jsonLength) => module._spc_root_session_import_json(
          rootSessionId,
          jsonPointer,
          jsonLength,
        ),
        "The native retained-root import ABI",
      );
      const raw = parseFacadeJson(
          module,
          pointer,
          "The native retained-root import ABI",
          "browser-root-import-invalid",
        );
      validateRootIdentityEcho(
        raw,
        nativeRequest,
        identity,
        "spc-root-session-import-result-v1",
      );
      if (!canonicalRootPolicyMatches(raw, rootCanonicalTacticalProtection)) {
        throw new KernelAdapterError(
          "The native root import changed its canonical tactical policy.",
          "browser-root-import-invalid",
        );
      }
      return {
        ...raw,
        ...rootMemoryReceipt(),
      };
    },
    searchRootCandidate(request) {
      if (rootSessionId === null || request?.session_id !== rootSessionId) {
        throw new KernelAdapterError(
          "The native root candidate search used no active session.",
          "browser-root-session-mismatch",
        );
      }
      const nativeRequest = nativeRootRequest(
        clampRootRemainingTime(request),
        identity,
        "spc-root-candidate-task-v1",
        [
          "alpha", "beta", "call_work_credit", "candidate_identity",
          "child_depth", "deadline_monotonic_ms", "enumeration_identity",
          "external_work", "incumbent_epoch", "mate_score", "mover",
          "native_work_before", "order_index", "order_key", "purpose",
          "remaining_time_ms", "safety_revision", "task_id", "tt_persistence",
        ],
      );
      const pointer = withJsonArgument(
        module,
        nativeRequest,
        (jsonPointer, jsonLength) => module._spc_root_session_search_json(
          rootSessionId,
          jsonPointer,
          jsonLength,
        ),
        "The native root-candidate search ABI",
      );
      const raw = parseFacadeJson(
          module,
          pointer,
          "The native root-candidate search ABI",
          "browser-root-search-invalid",
        );
      validateRootIdentityEcho(
        raw,
        nativeRequest,
        identity,
        "spc-root-candidate-result-v1",
      );
      return {
        ...raw,
        ...rootMemoryReceipt(),
      };
    },
    destroyRootSession() {
      if (rootSessionId === null) return { status: "not_found" };
      const sessionId = rootSessionId;
      const status = module._spc_root_session_destroy(sessionId);
      rootSessionId = null;
      rootCanonicalTacticalProtection = null;
      if (status !== 1) {
        throw new KernelAdapterError(
          "The native root session could not be destroyed cleanly.",
          "browser-root-session-destroy-failed",
        );
      }
      return { status: "destroyed", session_id: sessionId, ...rootMemoryReceipt() };
    },
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
    probeRootSafety(request) {
      if (!identity.root_iteration_ready) {
        throw new KernelAdapterError(
          "The combined artifact has no certified compiled root-mate authority.",
          "browser-root-mate-unavailable",
        );
      }
      const child = request?.authoritative_child_boundary;
      const rootReplay = request?.authoritative_root_replay;
      const rootMoves = request?.candidate?.root_series?.moves;
      const expectedRootIdentity = rootIdentityEnvelope(identity);
      if (
        rootSessionId === null
        || request?.session_id !== rootSessionId
        || request?.schema !== "spc-root-safety-task-v1"
        || ROOT_SESSION_IDENTITY_KEYS.some((key) => (
          request?.[key] !== expectedRootIdentity[key]
        ))
        || !Number.isInteger(request.call_work_credit)
        || request.call_work_credit < 1
        || request.call_work_credit > 0xffffffff
        || !Number.isInteger(request.remaining_time_ms)
        || request.remaining_time_ms < 0
        || !validExactBoundaryState(child)
        || !Array.isArray(rootMoves)
        || !rootReplay
        || rootReplay.complete !== true
        || rootReplay.outcome !== null
        || !sameJson(rootReplay.prefix, rootMoves)
        || !validExactBoundaryState(rootReplay.next_state)
        || !sameJson(rootReplay.next_state, child)
      ) {
        throw new KernelAdapterError(
          "The root mate probe has no authoritative compiled child boundary.",
          "browser-root-safety-boundary-invalid",
        );
      }
      const remainingMs = clampRootRemainingTime(request).remaining_time_ms;
      if (remainingMs <= 0) {
        return { ...request, status: "unknown", work_used: 0 };
      }
      const allocated = [];
      let pointer;
      try {
        for (const value of [
          child.fen,
          child.ep_targets.join(",") || "-",
          child.promoted_hex,
        ]) {
          const allocatedValue = module.stringToNewUTF8(value);
          if (!allocatedValue) {
            throw new KernelAdapterError(
              "The compiled mate probe could not allocate its boundary.",
              "browser-root-mate-allocation-failed",
            );
          }
          allocated.push(allocatedValue);
        }
        pointer = module._spc_series_mate_search_json(
          allocated[0],
          child.series,
          allocated[1],
          allocated[2],
          0,
          request.call_work_credit,
          remainingMs,
        );
      } finally {
        allocated.forEach((value) => module._free(value));
      }
      const raw = parseFacadeJson(
        module,
        pointer,
        "The compiled root reply-mate ABI",
        "browser-root-mate-invalid",
      );
      const workUsed = Number(raw?.stats?.positions_visited)
        + Number(raw?.stats?.moves_generated);
      if (
        raw.schema !== "spc-series-mate-proof-v1"
        || raw.abi_version !== MATE_ABI_VERSION
        || !Number.isSafeInteger(workUsed)
        || workUsed < 0
        || workUsed > request.call_work_credit
        || !Array.isArray(raw.moves)
      ) {
        throw new KernelAdapterError(
          "The compiled root reply-mate receipt is malformed or over credit.",
          "browser-root-mate-invalid",
        );
      }
      const safetyMemory = rootMemoryReceipt();
      if (raw.kernel_status === "exhausted" && raw.complete === true) {
        if (raw.moves.length !== 0) {
          throw new KernelAdapterError(
            "An exhausted mate proof carried a line.",
            "browser-root-mate-invalid",
          );
        }
        return {
          ...request,
          status: "exhausted",
          work_used: workUsed,
          ...safetyMemory,
        };
      }
      if (raw.kernel_status !== "found" || raw.complete !== true) {
        return {
          ...request,
          status: "unknown",
          work_used: workUsed,
          ...safetyMemory,
        };
      }
      if (
        raw.moves.length < 1
        || raw.moves.some((move) => typeof move !== "string"
          || !/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(move))
      ) {
        throw new KernelAdapterError(
          "A FOUND mate proof carried no valid progressive line.",
          "browser-root-mate-invalid",
        );
      }
      const replayRequest = {
        contract_version: 1,
        operation: "prefix-replay",
        request_id: `${request.iteration_id}:${request.safety_revision}:mate-replay`,
        boundary: {
          fen: child.fen,
          series: child.series,
          quiet_series: child.quiet_series,
          ep_targets: [...child.ep_targets],
          promoted_hex: child.promoted_hex,
          chess960: false,
        },
        prefix: raw.moves.map(String),
      };
      const replyMate = this.inspectPrefix(replayRequest);
      if (
        replyMate.complete !== true
        || replyMate.outcome !== "checkmate"
        || replyMate.ended_by_check !== true
        || !sameJson(replyMate.prefix, raw.moves)
      ) {
        throw new KernelAdapterError(
          "The compiled mate line failed authoritative compiled replay.",
          "browser-root-mate-replay-invalid",
        );
      }
      const childWhite = child.side_to_move === "white";
      const overrideScore = childWhite
        ? identity.root_geometry.session_config.mate_score - 2
        : -identity.root_geometry.session_config.mate_score + 2;
      const proof = childWhite ? [1, 1] : [-1, -1];
      return {
        ...request,
        status: "found",
        work_used: workUsed,
        override_score: overrideScore,
        proof_bounds: proof,
        ...rootMemoryReceipt(),
        reply_mate: {
          moves: [...raw.moves],
          machine_notation: raw.moves.join("/"),
          outcome: "checkmate",
          ended_by_check: true,
          checked_prefix: replyMate,
        },
      };
    },
  });
}

export {
  KernelAdapterError,
  clampRootRemainingTime,
  importVerifiedModuleBytes,
  normalizeKernelResult,
  validatePrefixContract,
  validateManifest,
};
