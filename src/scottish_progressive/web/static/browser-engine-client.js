(() => {
  "use strict";

  const SOURCE_FINGERPRINT = /^[0-9a-f]{16}$/;
  const ARTIFACT_FINGERPRINT = /^[0-9a-f]{64}$/;
  const CERTIFICATE_SCHEMA = "spc-browser-wasm-certificate-v1";
  const PROMOTED_FINGERPRINT = /^[0-9a-f]{16}$/;
  const UCI_MOVE = /^[a-h][1-8][a-h][1-8][qrbn]?$/;
  const EP_SQUARE = /^[a-h][1-8]$/;
  const KNOWN_OUTCOMES = new Set([
    "checkmate", "stalemate", "ten-series-draw", "ten_series_draw",
  ]);
  const TRANSIENT_WORKER_ERRORS = new Set([
    "browser-worker-crashed",
    "browser-worker-post-failed",
    "browser-worker-timeout",
    "browser-worker-unavailable",
  ]);
  const DEFAULT_PROBE_TIMEOUT_MS = 30_000;
  const REQUEST_GRACE_MS = 1_000;
  const PREFIX_LANE_RETRY_MS = 10;
  const MAX_INITIAL_MEMORY_BYTES = 128 * 1024 * 1024;
  const MAXIMUM_MEMORY_BYTES = 256 * 1024 * 1024;
  const MAX_ESTIMATED_PEAK_MEMORY_BYTES = 192 * 1024 * 1024;
  const MATE_SCORE = 1_000_000;
  const CHECKED_PV_SELECTION_POLICY =
    "repair-once-then-veto-adverse-checked-pv-mates-v1";
  const MATE_CLAIM_SELECTION_POLICY =
    "require-sign-matching-exact-proof-for-nonterminal-mate-band-v1";
  const MAX_SAME_ROOT_HORIZON_REPAIRS = 1;
  const SAME_ROOT_REPAIR_POLICY_SCHEMA = "spc-same-root-horizon-repair-policy-v1";
  const PV_HORIZON_POLICY_VETO_SCHEMA = "spc-pv-horizon-candidate-veto-v1";
  const PV_HORIZON_POLICY_VETO_REASONS = new Set([
    "duplicate-horizon-proof", "missing-horizon-proof", "owner-recertification-failed",
    "repair-proof-not-hit", "repair-unsupported", "repair-work-limit",
    "retained-proof-capacity", "same-root-repair-limit",
  ]);
  const PREFIX_API = globalThis.ScottishProgressiveBrowserPrefix || null;
  const ROOT_RUNNER_API = globalThis.ScottishProgressiveBrowserRootIteration || null;
  const scriptVersion = (() => {
    try {
      const source = globalThis.document?.currentScript?.src;
      return source ? new URL(source).search : "";
    } catch {
      return "";
    }
  })();

  class BrowserEngineError extends Error {
    constructor(message, code, { fallbackRequired = false, cause } = {}) {
      super(message, cause === undefined ? undefined : { cause });
      this.name = "BrowserEngineError";
      this.code = code;
      this.fallbackRequired = fallbackRequired;
    }
  }

  function abortError(message = "Browser engine request cancelled") {
    if (typeof DOMException === "function") return new DOMException(message, "AbortError");
    const error = new Error(message);
    error.name = "AbortError";
    return error;
  }

  function waitForPrefixLane(signal) {
    if (signal?.aborted) return Promise.reject(abortError());
    return new Promise((resolve, reject) => {
      let timer = null;
      const onAbort = () => {
        if (timer !== null) globalThis.clearTimeout(timer);
        signal?.removeEventListener?.("abort", onAbort);
        reject(abortError());
      };
      signal?.addEventListener?.("abort", onAbort, { once: true });
      if (signal?.aborted) {
        onAbort();
        return;
      }
      timer = globalThis.setTimeout(() => {
        signal?.removeEventListener?.("abort", onAbort);
        resolve();
      }, PREFIX_LANE_RETRY_MS);
    });
  }

  function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function immutableJsonCopy(value) {
    return Object.freeze(JSON.parse(JSON.stringify(value)));
  }

  function monotonicNow() {
    return globalThis.performance?.now?.() ?? Date.now();
  }

  function deadlineError() {
    return new BrowserEngineError(
      "The browser engine reached the analysis deadline.",
      "browser-analysis-deadline",
      { fallbackRequired: true },
    );
  }

  function exactInteger(value, minimum, maximum) {
    return Number.isInteger(value) && value >= minimum && value <= maximum;
  }

  function publishableMateClaim(score, proofBounds) {
    if (
      !Number.isSafeInteger(score)
      || Math.abs(score) >= 2 * MATE_SCORE
      || !Array.isArray(proofBounds)
      || proofBounds.length !== 2
      || proofBounds.some((bound) => ![-1, 0, 1].includes(bound))
    ) return false;
    if (Math.abs(score) < MATE_SCORE - 10_000) return true;
    const expected = score > 0 ? 1 : -1;
    return proofBounds[0] === expected && proofBounds[1] === expected;
  }

  function hasOwn(value, key) {
    return value !== null
      && typeof value === "object"
      && Object.prototype.hasOwnProperty.call(value, key);
  }

  function normalizedSameRootRepairPolicy(value) {
    const expectedKeys = ["maximum_successful_same_root_repairs", "schema"];
    if (
      !value
      || typeof value !== "object"
      || Array.isArray(value)
      || JSON.stringify(Object.keys(value).sort()) !== JSON.stringify(expectedKeys)
      || value.schema !== SAME_ROOT_REPAIR_POLICY_SCHEMA
      || value.maximum_successful_same_root_repairs
        !== MAX_SAME_ROOT_HORIZON_REPAIRS
    ) return null;
    return {
      schema: SAME_ROOT_REPAIR_POLICY_SCHEMA,
      maximum_successful_same_root_repairs: MAX_SAME_ROOT_HORIZON_REPAIRS,
    };
  }

  function normalizedPvHorizonPolicyVetoes(value, expectedCount, maximumProofs) {
    if (!Array.isArray(value) || value.length !== expectedCount) return null;
    const expectedKeys = [
      "candidate_identity", "distinct_proofs_observed",
      "maximum_successful_same_root_repairs", "reason", "repairs_before_veto",
      "retained_proofs_before_veto", "schema",
    ];
    const seen = new Set();
    const normalized = [];
    for (const entry of value) {
      if (
        !entry
        || typeof entry !== "object"
        || Array.isArray(entry)
        || JSON.stringify(Object.keys(entry).sort()) !== JSON.stringify(expectedKeys)
        || entry.schema !== PV_HORIZON_POLICY_VETO_SCHEMA
        || typeof entry.candidate_identity !== "string"
        || !entry.candidate_identity
        || seen.has(entry.candidate_identity)
        || !PV_HORIZON_POLICY_VETO_REASONS.has(entry.reason)
        || entry.maximum_successful_same_root_repairs
          !== MAX_SAME_ROOT_HORIZON_REPAIRS
        || !exactInteger(
          entry.repairs_before_veto,
          0,
          MAX_SAME_ROOT_HORIZON_REPAIRS,
        )
        || !exactInteger(entry.retained_proofs_before_veto, 0, maximumProofs)
        || !exactInteger(entry.distinct_proofs_observed, 0, maximumProofs + 1)
        || entry.distinct_proofs_observed < entry.retained_proofs_before_veto
        || (
          entry.reason === "same-root-repair-limit"
          && (
            entry.repairs_before_veto !== 1
            || entry.retained_proofs_before_veto !== 1
            || entry.distinct_proofs_observed !== 2
          )
        )
      ) return null;
      seen.add(entry.candidate_identity);
      normalized.push({
        schema: entry.schema,
        candidate_identity: entry.candidate_identity,
        reason: entry.reason,
        maximum_successful_same_root_repairs:
          entry.maximum_successful_same_root_repairs,
        repairs_before_veto: entry.repairs_before_veto,
        retained_proofs_before_veto: entry.retained_proofs_before_veto,
        distinct_proofs_observed: entry.distinct_proofs_observed,
      });
    }
    return normalized;
  }

  function normalizedMateClaimQuarantineReceipts(value, expectedCount) {
    if (!Array.isArray(value)) return null;
    const expectedKeys = [
      "candidate_identity", "currently_quarantined", "proof_bounds",
      "quarantine_count", "score",
    ];
    const seen = new Set();
    const normalized = [];
    let observedCount = 0;
    for (const entry of value) {
      if (
        !entry
        || typeof entry !== "object"
        || Array.isArray(entry)
        || JSON.stringify(Object.keys(entry).sort())
          !== JSON.stringify(expectedKeys)
        || typeof entry.candidate_identity !== "string"
        || !entry.candidate_identity
        || seen.has(entry.candidate_identity)
        || !exactInteger(entry.quarantine_count, 1, Number.MAX_SAFE_INTEGER)
        || !Number.isSafeInteger(entry.score)
        || Math.abs(entry.score) < MATE_SCORE - 10_000
        || Math.abs(entry.score) >= 2 * MATE_SCORE
        || typeof entry.currently_quarantined !== "boolean"
        || !Array.isArray(entry.proof_bounds)
        || entry.proof_bounds.length !== 2
        || entry.proof_bounds.some((bound) => ![-1, 0, 1].includes(bound))
        || entry.currently_quarantined
          === publishableMateClaim(entry.score, entry.proof_bounds)
      ) return null;
      seen.add(entry.candidate_identity);
      observedCount += entry.quarantine_count;
      normalized.push({
        ...entry,
        proof_bounds: [...entry.proof_bounds],
      });
    }
    return observedCount === expectedCount ? normalized : null;
  }

  function canonicalPromotedHex(value) {
    const text = String(value || "").toLowerCase().replace(/^0x/, "");
    if (!/^[0-9a-f]{1,16}$/.test(text)) return null;
    return text.replace(/^0+(?=[0-9a-f])/, "").padStart(16, "0");
  }

  function normalizedAnalysisLimits(value) {
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
      !exactInteger(limits.maximum_depth, 1, 64)
      || !exactInteger(limits.maximum_max_series, 1, 16_384)
      || !exactInteger(limits.maximum_generation_positions, 1_000, 0xffffffff)
      || !exactInteger(limits.default_depth, 1, limits.maximum_depth)
      || !exactInteger(limits.default_max_series, 1, limits.maximum_max_series)
      || !exactInteger(
        limits.default_generation_positions,
        1_000,
        limits.maximum_generation_positions,
      )
      || !Number.isFinite(limits.maximum_seconds)
      || limits.maximum_seconds <= 0
      || limits.maximum_seconds > 0xffffffff / 1000
      || !Number.isFinite(limits.default_seconds)
      || limits.default_seconds <= 0
      || limits.default_seconds > limits.maximum_seconds
    ) return null;
    return limits;
  }

  function normalizedMemoryLimits(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const expectedKeys = [
      "estimated_peak_bytes",
      "growth_enabled",
      "initial_bytes",
      "maximum_bytes",
    ];
    if (JSON.stringify(Object.keys(value).sort()) !== JSON.stringify(expectedKeys)) return null;
    const limits = {
      initial_bytes: value.initial_bytes,
      maximum_bytes: value.maximum_bytes,
      estimated_peak_bytes: value.estimated_peak_bytes,
      growth_enabled: value.growth_enabled,
    };
    const pageAligned = (number) => Number.isInteger(number)
      && number > 0
      && number % 65_536 === 0;
    if (
      !pageAligned(limits.initial_bytes)
      || !pageAligned(limits.maximum_bytes)
      || !pageAligned(limits.estimated_peak_bytes)
      || limits.initial_bytes > MAX_INITIAL_MEMORY_BYTES
      || limits.maximum_bytes > MAXIMUM_MEMORY_BYTES
      || limits.estimated_peak_bytes > MAX_ESTIMATED_PEAK_MEMORY_BYTES
      || limits.initial_bytes > limits.estimated_peak_bytes
      || limits.estimated_peak_bytes > limits.maximum_bytes
      || typeof limits.growth_enabled !== "boolean"
      || (!limits.growth_enabled && limits.initial_bytes !== limits.maximum_bytes)
    ) return null;
    return limits;
  }

  function normalizedEpTargets(value) {
    if (!Array.isArray(value) || value.length > 8) return null;
    const targets = value.map(String);
    if (targets.some((square) => !EP_SQUARE.test(square))) return null;
    const unique = [...new Set(targets)];
    if (unique.length !== targets.length) return null;
    return unique.sort();
  }

  function fenFields(value) {
    if (typeof value !== "string" || !value || value.length > 512 || /[\0\r\n]/.test(value)) {
      return null;
    }
    const fields = value.trim().split(/\s+/);
    if (
      fields.length !== 6
      || fields[0].split("/").length !== 8
      || !/^[wb]$/.test(fields[1])
      || !/^(?:-|[KQkq]+)$/.test(fields[2])
      || !/^(?:-|[a-h][36])$/.test(fields[3])
      || !/^\d+$/.test(fields[4])
      || !/^[1-9]\d*$/.test(fields[5])
    ) return null;
    return fields;
  }

  function sameFenPositionExceptEp(left, right) {
    const a = fenFields(left);
    const b = fenFields(right);
    return Boolean(a && b && [0, 1, 2, 4, 5].every((index) => a[index] === b[index]));
  }

  function isLocalBestMoveRequest(payload, analysisLimits = null) {
    const baseContract = Boolean(
      payload
      && typeof payload === "object"
      && !Array.isArray(payload)
      && payload.best_move_only === true
      && payload.rate_move === false
      && payload.save === false
      && Number(payload.alternatives) === 0
      && Array.isArray(payload.prefix)
      && payload.prefix.length === 0
      && typeof payload.fen === "string"
      && exactInteger(payload.series, 1, 256)
      && exactInteger(payload.quiet_series, 0, 1_000_000)
      && Array.isArray(payload.ep_targets)
      && payload.ep_targets.length <= 8
      && normalizedEpTargets(payload.ep_targets) !== null
      && canonicalPromotedHex(payload.promoted_hex) !== null
      && payload.chess960 !== true
      && exactInteger(payload.depth, 1, 64)
      && exactInteger(payload.max_series, 1, 4096)
      && finiteNumber(payload.time_limit) !== null
      && exactInteger(payload.max_generation_positions, 1_000, 0xffffffff)
    );
    if (!baseContract) return false;
    if (analysisLimits === null || analysisLimits === undefined) return true;
    const certified = normalizedAnalysisLimits(analysisLimits);
    return Boolean(
      certified
      && payload.depth <= certified.maximum_depth
      && payload.max_series <= certified.maximum_max_series
      && Number(payload.time_limit) <= certified.maximum_seconds
      && payload.max_generation_positions <= certified.maximum_generation_positions
    );
  }

  function normalizedKernelRequest(payload, requestId, analysisLimits = null) {
    if (!isLocalBestMoveRequest(payload, analysisLimits)) {
      throw new BrowserEngineError(
        "This analysis contract is not implemented by the browser kernel.",
        "browser-contract-unsupported",
        { fallbackRequired: true },
      );
    }
    return {
      contract_version: 1,
      request_id: requestId,
      boundary: {
        fen: payload.fen,
        series: payload.series,
        quiet_series: payload.quiet_series,
        ep_targets: payload.ep_targets.map(String).sort(),
        promoted_hex: canonicalPromotedHex(payload.promoted_hex),
        chess960: false,
        prefix: [],
      },
      limits: {
        depth: payload.depth,
        max_series: payload.max_series,
        time_limit_seconds: Number(payload.time_limit),
        max_generation_positions: payload.max_generation_positions,
        best_move_only: true,
      },
    };
  }

  function validateIdentity(identity) {
    if (!identity || typeof identity !== "object") return false;
    const analysisLimits = normalizedAnalysisLimits(identity.analysis_limits);
    const memoryLimits = normalizedMemoryLimits(identity.memory_limits);
    let prefixContract = null;
    if (identity.prefix_ready === true && PREFIX_API) {
      try {
        prefixContract = PREFIX_API.validateCertifiedPrefixContract(
          identity.prefix_contract,
        );
      } catch {
        prefixContract = null;
      }
    }
    const commonIdentity = (
      SOURCE_FINGERPRINT.test(String(identity.source_fingerprint || ""))
      && ARTIFACT_FINGERPRINT.test(String(identity.wasm_sha256 || ""))
      && ARTIFACT_FINGERPRINT.test(String(identity.module_js_sha256 || ""))
      && identity.contract_version === 1
      && identity.abi_version === 1
      && identity.runtime_variant === "single"
      && identity.thread_count === 1
      && typeof identity.engine_version === "string"
      && Boolean(identity.engine_version)
      && typeof identity.ruleset_version === "string"
      && Boolean(identity.ruleset_version)
      && memoryLimits !== null
    );
    if (!commonIdentity) return false;
    const valueModelPresent = identity.value_model_status !== undefined;
    const validValueModel = !valueModelPresent || (
      ["active", "fallback"].includes(identity.value_model_status)
      && identity.value_model_active === (identity.value_model_status === "active")
      && (identity.value_model_status === "active" ? (
        /^spc-dtv-[0-9a-f]{20}$/.test(String(identity.value_model_id || ""))
        && ARTIFACT_FINGERPRINT.test(String(identity.value_model_sha256 || ""))
        && /^spc-dtv-variant-[0-9a-f]{20}$/.test(
          String(identity.value_model_variant_id || ""),
        )
        && ARTIFACT_FINGERPRINT.test(
          String(identity.value_model_native_source_identity || ""),
        )
        && identity.value_model_failure_code === null
        && identity.engine_profile_id === identity.value_model_variant_id
      ) : (
        identity.value_model_id === null
        && identity.value_model_sha256 === null
        && identity.value_model_variant_id === null
        && identity.value_model_native_source_identity === null
        && typeof identity.value_model_failure_code === "string"
        && Boolean(identity.value_model_failure_code)
      ))
    );
    if (!validValueModel) return false;
    const analysisReady = identity.analysis_ready === true;
    const prefixReady = identity.prefix_ready === true;
    const rootReady = identity.root_iteration_ready === true;
    const validAnalysis = analysisReady ? (
      identity.certificate_schema === CERTIFICATE_SCHEMA
      && identity.certificate_status === "certified"
      && identity.safety_certified === true
      && typeof identity.certificate_id === "string"
      && Boolean(identity.certificate_id)
      && typeof identity.engine_profile_id === "string"
      && Boolean(identity.engine_profile_id)
      && typeof identity.engine_profile_name === "string"
      && Boolean(identity.engine_profile_name)
      && analysisLimits !== null
    ) : (
      identity.safety_certified === false
      && identity.certificate_id === null
      && analysisLimits === null
    );
    const validPrefix = prefixReady ? (
      PREFIX_API !== null
      && typeof identity.prefix_certificate_id === "string"
      && Boolean(identity.prefix_certificate_id)
      && prefixContract !== null
    ) : (
      identity.prefix_certificate_id === null
      && identity.prefix_contract === null
    );
    const validRoot = rootReady ? (
      ROOT_RUNNER_API !== null
      && identity.root_session_ready === true
      && identity.mate_ready === true
      && ARTIFACT_FINGERPRINT.test(String(identity.kernel_sha256 || ""))
      && typeof identity.root_session_certificate_id === "string"
      && Boolean(identity.root_session_certificate_id)
      && typeof identity.mate_certificate_id === "string"
      && Boolean(identity.mate_certificate_id)
      && typeof identity.profile_id === "string"
      && Boolean(identity.profile_id)
      && identity.root_session_contract
      && typeof identity.root_session_contract === "object"
      && identity.root_geometry
      && typeof identity.root_geometry === "object"
    ) : (
      identity.root_session_ready !== true
      && identity.mate_ready !== true
      && (identity.root_session_certificate_id === null
        || identity.root_session_certificate_id === undefined)
      && (identity.mate_certificate_id === null
        || identity.mate_certificate_id === undefined)
      && (identity.root_session_contract === null
        || identity.root_session_contract === undefined)
      && (identity.root_geometry === null || identity.root_geometry === undefined)
    );
    return (analysisReady || prefixReady || rootReady)
      && validAnalysis && validPrefix && validRoot;
  }

  function validateCompiledReplay(result, request) {
    if (
      result.legal_series_certified !== true
      || result.authoritative_replay_certified !== true
      || result.legal_validation_runtime !== "compiled-wasm"
    ) {
      throw new BrowserEngineError(
        "The compiled engine did not certify legal replay for this series.",
        "browser-legality-unverified",
        { fallbackRequired: true },
      );
    }
    const replay = result.checked_prefix;
    const moves = result.best_full_series;
    const boundary = replay?.boundary_state;
    const replayBoundaryEp = normalizedEpTargets(boundary?.ep_targets);
    const requestBoundaryEp = normalizedEpTargets(request.boundary.ep_targets);
    const outcome = replay?.outcome;
    const nextState = replay?.next_state;
    const nextStateEp = normalizedEpTargets(nextState?.ep_targets);
    const finalFrame = Array.isArray(replay?.frames) ? replay.frames.at(-1) : null;
    const requestFen = fenFields(request.boundary.fen);
    const nextStateFen = fenFields(nextState?.fen);
    if (
      !replay
      || typeof replay !== "object"
      || Array.isArray(replay)
      || !boundary
      || typeof boundary !== "object"
      || boundary.fen !== request.boundary.fen
      || (boundary.series ?? boundary.series_number) !== request.boundary.series
      || boundary.quiet_series !== request.boundary.quiet_series
      || boundary.chess960 !== false
      || !PROMOTED_FINGERPRINT.test(String(boundary.promoted_hex || ""))
      || canonicalPromotedHex(boundary.promoted_hex) !== request.boundary.promoted_hex
      || replayBoundaryEp === null
      || requestBoundaryEp === null
      || replayBoundaryEp.length !== requestBoundaryEp.length
      || replayBoundaryEp.some((square, index) => square !== requestBoundaryEp[index])
      || !Array.isArray(replay.prefix)
      || replay.prefix.length !== moves.length
      || replay.prefix.some((move, index) => (
        typeof move !== "string" || move !== moves[index]
      ))
      || !Array.isArray(replay.san)
      || replay.san.length !== moves.length
      || replay.san.some((move) => typeof move !== "string" || !move)
      || !Array.isArray(replay.frames)
      || replay.frames.length !== moves.length
      || replay.frames.some((frame, index) => (
        !frame
        || typeof frame !== "object"
         || frame.index !== index + 1
         || String(frame.uci || "") !== moves[index]
         || String(frame.san || "") !== replay.san[index]
         || fenFields(frame.board_fen) === null
       ))
      || replay.complete !== true
      || fenFields(replay.board_fen) === null
      || !sameFenPositionExceptEp(finalFrame?.board_fen, replay.board_fen)
      || !(outcome === null || KNOWN_OUTCOMES.has(outcome))
      || (nextState !== null && nextState !== undefined && (
        typeof nextState !== "object"
        || Array.isArray(nextState)
        || nextStateFen === null
        || nextState.fen !== replay.board_fen
        || nextStateFen[1] === requestFen?.[1]
        || (nextState.series ?? nextState.series_number)
          !== request.boundary.series + 1
        || !exactInteger(nextState.quiet_series, 0, 0x7fffffff)
        || nextStateEp === null
        || !PROMOTED_FINGERPRINT.test(String(nextState.promoted_hex || ""))
        || nextState.chess960 !== false
      ))
      || (outcome === null && (
        !nextState
        || typeof nextState !== "object"
      ))
    ) {
      throw new BrowserEngineError(
        "The compiled engine returned an invalid canonical replay.",
        "browser-replay-invalid",
        { fallbackRequired: true },
      );
    }
    return replay;
  }

  function validateReportedMemory(result, identity) {
    const memoryBytes = result?.memory_bytes;
    const memory = normalizedMemoryLimits(identity?.memory_limits);
    if (
      !memory
      || !Number.isInteger(memoryBytes)
      || memoryBytes < memory.initial_bytes
      || memoryBytes % 65_536 !== 0
      || memoryBytes > memory.estimated_peak_bytes
      || memoryBytes > memory.maximum_bytes
    ) {
      throw new BrowserEngineError(
        "The browser kernel exceeded its certified memory envelope.",
        "browser-memory-envelope-exceeded",
        { fallbackRequired: true },
      );
    }
    return memoryBytes;
  }

  function validatePublishedAnalysis(result, request, identity) {
    if (!result || typeof result !== "object" || Array.isArray(result)) {
      throw new BrowserEngineError(
        "The browser kernel returned no analysis object.",
        "browser-invalid-result",
        { fallbackRequired: true },
      );
    }
    if (
      result.ok !== true
      || result.publishable !== true
      || result.safety_certified !== true
    ) {
      throw new BrowserEngineError(
        "The browser kernel did not certify this result for play.",
        "browser-result-not-publishable",
        { fallbackRequired: true },
      );
    }
    if (
      result.source_fingerprint !== identity.source_fingerprint
      || result.wasm_sha256 !== identity.wasm_sha256
      || result.module_js_sha256 !== identity.module_js_sha256
      || result.certificate_id !== identity.certificate_id
      || result.runtime_variant !== identity.runtime_variant
      || result.thread_count !== identity.thread_count
    ) {
      throw new BrowserEngineError(
        "The browser kernel identity changed during analysis.",
        "browser-identity-mismatch",
        { fallbackRequired: true },
      );
    }
    const requestedDepth = Number(result.requested_depth);
    const completedDepth = Number(result.completed_depth);
    if (
      !exactInteger(requestedDepth, 1, 64)
      || requestedDepth !== request.limits.depth
      || !exactInteger(completedDepth, 1, requestedDepth)
    ) {
      throw new BrowserEngineError(
        "The browser kernel did not report an authoritative completed depth.",
        "browser-depth-unverified",
        { fallbackRequired: true },
      );
    }
    if (
      !Array.isArray(result.best_full_series)
      || result.best_full_series.length < 1
      || result.best_full_series.length > request.boundary.series
      || result.best_full_series.some((move) => !UCI_MOVE.test(String(move)))
    ) {
      throw new BrowserEngineError(
        "The browser kernel returned an invalid move series.",
        "browser-series-invalid",
        { fallbackRequired: true },
      );
    }
    if (!result.stats || typeof result.stats !== "object" || Array.isArray(result.stats)) {
      throw new BrowserEngineError(
        "The browser kernel omitted its search statistics.",
        "browser-stats-missing",
        { fallbackRequired: true },
      );
    }
    validateReportedMemory(result, identity);
    validateCompiledReplay(result, request);
    return { requestedDepth, completedDepth };
  }

  function validatePublishedRootAnalysis(result, payload, identity) {
    const request = normalizedKernelRequest(
      payload,
      String(result?.checked_prefix?.request_id || "root-result-validation"),
      null,
    );
    const requestedDepth = Number(result?.requested_depth);
    const completedDepth = Number(result?.completed_depth);
    const receipt = result?.runtime_receipt;
    const mateCache = receipt?.mate_cache;
    const maximumHorizonProofs = identity.root_session_contract
      ?.hard_limits?.maximum_horizon_proofs;
    const maximumHorizonRepairs = request?.limits?.max_series
      * MAX_SAME_ROOT_HORIZON_REPAIRS;
    const maximumHorizonVetoes = request?.limits?.max_series;
    const maximumHorizonLineRejections = maximumHorizonRepairs + maximumHorizonVetoes;
    const interruptedLastSafe = [
      "interruption_code", "attempted_work", "attempted_wall_time_seconds",
    ].some((key) => hasOwn(receipt, key))
      || hasOwn(result, "attempted_work")
      || hasOwn(result, "attempted_wall_time_seconds")
      || result?.timed_out === true
      || result?.work_limit_reached === true
      || receipt?.timed_out === true
      || receipt?.work_limit_reached === true;
    const interruptionStateValid = !interruptedLastSafe || (
      typeof receipt?.interruption_code === "string"
      && receipt.interruption_code.length > 0
      && typeof result?.timed_out === "boolean"
      && typeof result?.work_limit_reached === "boolean"
      && receipt?.timed_out === result.timed_out
      && receipt?.work_limit_reached === result.work_limit_reached
    );
    const attemptedWorkValid = !interruptedLastSafe || (
      exactInteger(result?.work, 0, Number.MAX_SAFE_INTEGER)
      && receipt?.work === result.work
      && exactInteger(
        result?.attempted_work,
        result.work,
        Number.MAX_SAFE_INTEGER,
      )
      && receipt?.attempted_work === result.attempted_work
    );
    const attemptedWallTimeValid = !interruptedLastSafe || (
      Number.isFinite(receipt?.wall_time_seconds)
      && receipt.wall_time_seconds >= 0
      && Number.isFinite(result?.attempted_wall_time_seconds)
      && result.attempted_wall_time_seconds >= receipt.wall_time_seconds
      && receipt?.attempted_wall_time_seconds
        === result.attempted_wall_time_seconds
    );
    const repairPolicies = [
      result?.same_root_repair_policy,
      result?.stats?.same_root_repair_policy,
      receipt?.same_root_repair_policy,
    ].map(normalizedSameRootRepairPolicy);
    const policyVetoes = [
      result?.pv_horizon_policy_vetoes,
      result?.stats?.pv_horizon_policy_vetoes,
      receipt?.pv_horizon_policy_vetoes,
    ].map((value) => normalizedPvHorizonPolicyVetoes(
      value,
      result?.pv_horizon_candidate_vetoes,
      maximumHorizonProofs,
    ));
    const mateClaimCounts = [
      result?.root_mate_claim_quarantines,
      result?.stats?.root_mate_claim_quarantines,
      receipt?.root_mate_claim_quarantines,
    ];
    const mateClaimPolicies = [
      result?.mate_claim_selection_policy,
      result?.stats?.mate_claim_selection_policy,
      receipt?.mate_claim_selection_policy,
    ];
    const mateClaimFiltered = result?.root_mate_claim_quarantines > 0;
    const mateClaimReceipts = [
      result?.mate_claim_quarantine_receipts,
      result?.stats?.mate_claim_quarantine_receipts,
      receipt?.mate_claim_quarantine_receipts,
    ].map((value) => normalizedMateClaimQuarantineReceipts(
      value,
      result?.root_mate_claim_quarantines,
    ));
    const expectedProof = result?.proof_bounds?.[0] === 1
      && result?.proof_bounds?.[1] === 1
      ? "white"
      : result?.proof_bounds?.[0] === -1
        && result?.proof_bounds?.[1] === -1 ? "black" : null;
    if (
      !result
      || typeof result !== "object"
      || Array.isArray(result)
      || result.ok !== true
      || result.status !== "complete"
      || result.publishable !== true
      || result.safety_certified !== true
      || result.legal_series_certified !== true
      || result.authoritative_replay_certified !== true
      || result.legal_validation_runtime !== "compiled-wasm"
      || result.source_fingerprint !== identity.source_fingerprint
      || result.wasm_sha256 !== identity.wasm_sha256
      || result.kernel_sha256 !== identity.kernel_sha256
      || result.module_js_sha256 !== identity.module_js_sha256
      || result.certificate_id !== identity.root_session_certificate_id
      || result.mate_certificate_id !== identity.mate_certificate_id
      || result.prefix_certificate_id !== identity.prefix_certificate_id
      || result.runtime_variant !== "single"
      || result.thread_count !== 1
      || !exactInteger(requestedDepth, 1, 5)
      || requestedDepth !== request.limits.depth
      || !exactInteger(completedDepth, 1, requestedDepth)
      || !Array.isArray(result.best_full_series)
      || result.best_full_series.length < 1
      || result.best_full_series.length > request.boundary.series
      || result.best_full_series.some((move) => !UCI_MOVE.test(String(move)))
      || result.root_search_mode !== "streaming-root-iteration"
      || result.mate_score !== MATE_SCORE
      || !publishableMateClaim(result.score, result.proof_bounds)
      || result.proof !== expectedProof
      || typeof result.root_scores_complete !== "boolean"
      || result.root_bound_coverage_complete !== true
      || result.selection_policy !== CHECKED_PV_SELECTION_POLICY
      || mateClaimPolicies.some(
        (value) => value !== MATE_CLAIM_SELECTION_POLICY
      )
      || mateClaimCounts.some(
        (value) => !exactInteger(value, 0, Number.MAX_SAFE_INTEGER)
      )
      || mateClaimCounts.some(
        (value) => value !== mateClaimCounts[0]
      )
      || result.mate_claim_policy_filtered !== mateClaimFiltered
      || result.stats?.mate_claim_policy_filtered !== mateClaimFiltered
      || receipt?.mate_claim_policy_filtered !== mateClaimFiltered
      || mateClaimReceipts.some((value) => value === null)
      || mateClaimReceipts.some((value) => (
        JSON.stringify(value) !== JSON.stringify(mateClaimReceipts[0])
      ))
      || !interruptionStateValid
      || !attemptedWorkValid
      || !attemptedWallTimeValid
      || !exactInteger(maximumHorizonProofs, 1, 30)
      || repairPolicies.some((value) => value === null)
      || repairPolicies.some((value) => (
        JSON.stringify(value) !== JSON.stringify(repairPolicies[0])
      ))
      || policyVetoes.some((value) => value === null)
      || policyVetoes.some((value) => (
        JSON.stringify(value) !== JSON.stringify(policyVetoes[0])
      ))
      || !exactInteger(
        result.pv_horizon_line_rejections,
        0,
        maximumHorizonLineRejections,
      )
      || !exactInteger(
        result.pv_horizon_native_repairs,
        0,
        maximumHorizonRepairs,
      )
      || !exactInteger(
        result.pv_horizon_candidate_vetoes,
        0,
        maximumHorizonVetoes,
      )
      || result.pv_horizon_native_repairs + result.pv_horizon_candidate_vetoes
        !== result.pv_horizon_line_rejections
      || result.selection_policy_filtered
        !== (result.pv_horizon_candidate_vetoes > 0)
      || result.selection_policy_filtered
        && (completedDepth < 3 || completedDepth % 2 !== 1)
      || result.root_bound_coverage_scope !== (
        result.selection_policy_filtered || mateClaimFiltered
          ? "selection-eligible-candidates"
          : "all-retained-candidates"
      )
      || result.unfiltered_score_winner_selected
        !== (
          result.pv_horizon_line_rejections === 0 && !mateClaimFiltered
        )
      || result.stats?.coverage_complete !== true
      || result.stats?.pv_horizon_line_rejections
        !== result.pv_horizon_line_rejections
      || result.stats?.pv_horizon_native_repairs
        !== result.pv_horizon_native_repairs
      || result.stats?.pv_horizon_candidate_vetoes
        !== result.pv_horizon_candidate_vetoes
      || !receipt
      || receipt.runtime !== "browser-wasm"
      || receipt.search_mode !== "streaming-root-iteration"
      || receipt.requested_depth !== requestedDepth
      || receipt.completed_depth !== completedDepth
      || receipt.worker_count !== identity.root_geometry?.desktop_workers
        && !identity.root_geometry?.supported_lower_geometries?.some(
          (geometry) => geometry.workers === receipt.worker_count,
        )
      || !exactInteger(receipt.worker_count, 1, 8)
      || !exactInteger(receipt.initial_full_wave, 1, receipt.worker_count)
      || receipt.aggregate_memory_cap_bytes
        !== receipt.worker_count * identity.memory_limits.maximum_bytes
      || !exactInteger(
        receipt.aggregate_memory_peak_bytes,
        0,
        receipt.aggregate_memory_cap_bytes,
      )
      || receipt.canonical_replay_certified !== true
      || receipt.mate_safety_certified !== true
      || receipt.root_bound_coverage_complete !== true
      || receipt.selection_policy !== result.selection_policy
      || receipt.mate_claim_selection_policy
        !== result.mate_claim_selection_policy
      || receipt.mate_claim_policy_filtered
        !== result.mate_claim_policy_filtered
      || receipt.root_mate_claim_quarantines
        !== result.root_mate_claim_quarantines
      || receipt.selection_policy_filtered !== result.selection_policy_filtered
      || receipt.pv_horizon_line_rejections !== result.pv_horizon_line_rejections
      || receipt.pv_horizon_native_repairs !== result.pv_horizon_native_repairs
      || receipt.pv_horizon_candidate_vetoes !== result.pv_horizon_candidate_vetoes
      || receipt.root_bound_coverage_scope !== result.root_bound_coverage_scope
      || receipt.unfiltered_score_winner_selected
        !== result.unfiltered_score_winner_selected
      || !exactInteger(
        receipt.safety_reserve_positions,
        1,
        identity.root_geometry?.play_limits?.safety_reserve_positions,
      )
      || result.stats?.safety_reserve_positions !== receipt.safety_reserve_positions
      || !mateCache
      || mateCache.schema !== "spc-root-mate-proof-cache-summary-v1"
      || !exactInteger(mateCache.hits, 0, Number.MAX_SAFE_INTEGER)
      || !exactInteger(mateCache.misses, 0, Number.MAX_SAFE_INTEGER)
      || !exactInteger(mateCache.entries, 0, 256)
      || mateCache.complete_proofs_only !== true
      || result.stats?.mate_cache_hits !== mateCache.hits
      || result.stats?.mate_cache_misses !== mateCache.misses
      || result.stats?.mate_cache_entries !== mateCache.entries
    ) {
      throw new BrowserEngineError(
        "The iterative browser root result is not fully certificate-bound.",
        "browser-root-result-invalid",
        { fallbackRequired: true },
      );
    }
    validateReportedMemory(result, identity);
    validateCompiledReplay(result, request);
    return { requestedDepth, completedDepth };
  }

  function expectedIdentityMatches(identity, {
    engineProfileId,
    engineProfileName,
    engineVersion,
    rulesetVersion,
  }) {
    if (!identity) return true;
    const rootOnly = identity.root_iteration_ready === true
      && identity.analysis_ready !== true;
    const expected = [
      [engineVersion, identity.engine_version],
      [rulesetVersion, identity.ruleset_version],
      ...(identity.analysis_ready === true || identity.root_iteration_ready === true ? [
        [engineProfileId, rootOnly
          ? (identity.profile_id || identity.engine_profile_id)
          : identity.engine_profile_id],
        ...(rootOnly ? [] : [[engineProfileName, identity.engine_profile_name]]),
      ] : []),
    ];
    return expected.every(([wanted, actual]) => (
      wanted === null || wanted === undefined || wanted === actual
    ));
  }

  class BrowserEngineClient {
    constructor({
      workerUrl,
      workerFactory,
      navigatorValue,
      probeTimeoutMs = DEFAULT_PROBE_TIMEOUT_MS,
    } = {}) {
      this.workerUrl = workerUrl || `./browser-engine-worker.js${scriptVersion}`;
      this.workerFactory = workerFactory || ((url, options) => new Worker(url, options));
      this.probeTimeoutMs = probeTimeoutMs;
      this.worker = null;
      this.generation = 0;
      this.nextMessageId = 1;
      this.nextRequestId = 1;
      this.nextPrefixRequestId = 1;
      this.pending = new Map();
      this.identity = null;
      this.profile = null;
      this.ready = false;
      this.disabledReason = null;
      this.probePromise = null;
      this.activeAnalysis = null;
      this.activePrefix = null;
      this.rootRunner = ROOT_RUNNER_API
        ? new ROOT_RUNNER_API.RootIterationRunner({
          workerUrl: this.workerUrl,
          workerFactory: this.workerFactory,
          navigatorValue,
        })
        : null;
    }

    canAnalyze(payload) {
      return this.identity !== null
        && this.disabledReason === null
        && this.identity.analysis_ready === true
        && this.activePrefix === null
        && this.rootRunner?.active !== true
        && isLocalBestMoveRequest(payload, this.identity.analysis_limits);
    }

    canQueuePrefix(payload) {
      if (
        !PREFIX_API
        || this.identity === null
        || this.disabledReason !== null
        || this.identity.prefix_ready !== true
      ) return false;
      try {
        PREFIX_API.normalizePrefixRequest(
          payload,
          "prefix-capability-check",
          this.identity.prefix_contract,
        );
        return true;
      } catch {
        return false;
      }
    }

    canInspectPrefix(payload) {
      return !this._prefixLaneBusy() && this.canQueuePrefix(payload);
    }

    _prefixLaneBusy() {
      return this.activeAnalysis !== null
        || this.activePrefix !== null
        || this.rootRunner?.active === true;
    }

    async _waitForPrefixLane(signal) {
      while (this._prefixLaneBusy()) {
        await waitForPrefixLane(signal);
      }
    }

    canAnalyzeRoot(payload) {
      return this.identity !== null
        && this.disabledReason === null
        && this.activeAnalysis === null
        && this.activePrefix === null
        && this.rootRunner?.canAnalyze(payload, this.identity) === true;
    }

    _spawnWorker() {
      if (this.worker) return this.worker;
      if (this.disabledReason) {
        throw new BrowserEngineError(
          this.disabledReason,
          "browser-engine-disabled",
          { fallbackRequired: true },
        );
      }
      let worker;
      try {
        worker = this.workerFactory(this.workerUrl, {
          type: "module",
          name: "scottish-progressive-engine",
        });
      } catch (cause) {
        throw new BrowserEngineError(
          "This browser could not start the WebAssembly engine worker.",
          "browser-worker-unavailable",
          { fallbackRequired: true, cause },
        );
      }
      const generation = ++this.generation;
      worker.addEventListener("message", (event) => {
        if (generation !== this.generation || worker !== this.worker) return;
        const message = event?.data;
        const entry = this.pending.get(message?.id);
        if (!entry) return;
        this.pending.delete(message.id);
        entry.cleanup();
        if (message.ok === true) entry.resolve(message.payload);
        else entry.reject(new BrowserEngineError(
          String(message.error?.message || "The browser engine worker failed."),
          String(message.error?.code || "browser-worker-error"),
          { fallbackRequired: message.error?.fallback_required !== false },
        ));
      });
      const fail = (event) => {
        if (generation !== this.generation || worker !== this.worker) return;
        const cause = event?.error;
        this.ready = false;
        this._dropWorker(new BrowserEngineError(
          "The WebAssembly engine worker stopped unexpectedly.",
          "browser-worker-crashed",
          { fallbackRequired: true, cause },
        ));
      };
      worker.addEventListener("error", fail);
      worker.addEventListener("messageerror", fail);
      this.worker = worker;
      return worker;
    }

    _dropWorker(error, exceptId = null) {
      const worker = this.worker;
      this.worker = null;
      this.generation += 1;
      this.activeAnalysis = null;
      this.activePrefix = null;
      try {
        worker?.terminate();
      } catch {
        // A worker that already exited needs no further cleanup.
      }
      for (const [id, entry] of this.pending) {
        if (id === exceptId) continue;
        this.pending.delete(id);
        entry.cleanup();
        entry.reject(error);
      }
    }

    _releaseRootPoolForSingleWorker() {
      if (this.rootRunner?.active === true) return false;
      if (this.rootRunner?.hasLivePool?.() === true) {
        this.rootRunner.releasePool(
          "Switching from the certified root pool to the single-Worker lane.",
        );
        this.ready = false;
      }
      return true;
    }

    _call(type, payload, {
      signal,
      timeoutMs,
      timeoutCode = "browser-worker-timeout",
    } = {}) {
      if (signal?.aborted) return Promise.reject(abortError());
      let worker;
      try {
        worker = this._spawnWorker();
      } catch (error) {
        return Promise.reject(error);
      }
      const id = this.nextMessageId++;
      return new Promise((resolve, reject) => {
        let timeout = null;
        const onAbort = () => {
          const entry = this.pending.get(id);
          if (!entry) return;
          this.pending.delete(id);
          entry.cleanup();
          reject(abortError());
          // Synchronous WebAssembly cannot consume a queued cancel message.
          // Termination is the hard cancellation boundary and releases its memory.
          this._dropWorker(new BrowserEngineError(
            "The browser engine restarted after cancellation.",
            "browser-worker-restarted",
            { fallbackRequired: true },
          ), id);
          this.ready = false;
        };
        const cleanup = () => {
          if (timeout !== null) globalThis.clearTimeout(timeout);
          signal?.removeEventListener?.("abort", onAbort);
        };
        this.pending.set(id, { resolve, reject, cleanup, type });
        signal?.addEventListener?.("abort", onAbort, { once: true });
        if (signal?.aborted) {
          onAbort();
          return;
        }
        if (Number.isFinite(timeoutMs) && timeoutMs > 0) {
          timeout = globalThis.setTimeout(() => {
            if (!this.pending.has(id)) return;
            this.pending.delete(id);
            cleanup();
            const error = new BrowserEngineError(
              timeoutCode === "browser-analysis-deadline"
                ? "The browser engine reached the analysis deadline."
                : "The browser engine worker exceeded its host deadline.",
              timeoutCode,
              { fallbackRequired: true },
            );
            reject(error);
            this._dropWorker(error, id);
            this.ready = false;
          }, timeoutMs);
        }
        try {
          worker.postMessage({ id, type, payload });
        } catch (cause) {
          this.pending.delete(id);
          cleanup();
          const error = new BrowserEngineError(
            "The browser engine request could not be delivered.",
            "browser-worker-post-failed",
            { fallbackRequired: true, cause },
          );
          reject(error);
          this.ready = false;
          this._dropWorker(error, id);
        }
      });
    }

    async preflight({
      sourceFingerprint = null,
      engineProfileId = null,
      engineProfileName = null,
      engineVersion = null,
      rulesetVersion = null,
      signal = null,
      deadlineMs = null,
    }) {
      const hasExpectedSource = sourceFingerprint !== null
        && sourceFingerprint !== undefined;
      if (hasExpectedSource && !SOURCE_FINGERPRINT.test(String(sourceFingerprint || ""))) {
        return { ready: false, reason: "invalid-server-source-fingerprint" };
      }
      if (this.disabledReason) return { ready: false, reason: this.disabledReason };
      const expectedProfileMatches = expectedIdentityMatches(this.identity, {
        engineProfileId,
        engineProfileName,
        engineVersion,
        rulesetVersion,
      });
      if (this.rootRunner?.active === true) {
        const sourceMatches = !hasExpectedSource
          || this.identity?.source_fingerprint === sourceFingerprint;
        return this.identity && sourceMatches && expectedProfileMatches
          ? { ready: true, ...this.identity }
          : { ready: false, reason: "browser-root-busy" };
      }
      if (this.rootRunner?.hasLivePool?.() === true) {
        const sourceMatches = !hasExpectedSource
          || this.identity?.source_fingerprint === sourceFingerprint;
        return this.identity && sourceMatches && expectedProfileMatches
          ? { ready: true, ...this.identity }
          : { ready: false, reason: "browser-root-worker-identity-mismatch" };
      }
      if (
        this.ready
        && (!hasExpectedSource || this.identity?.source_fingerprint === sourceFingerprint)
        && expectedProfileMatches
      ) {
        return { ready: true, ...this.identity };
      }
      if (
        this.identity
        && (
          (hasExpectedSource && this.identity.source_fingerprint !== sourceFingerprint)
          || !expectedProfileMatches
        )
      ) {
        this.close("browser/server engine identities differ");
        return { ready: false, reason: this.disabledReason };
      }
      if (this.probePromise) return this.probePromise;
      this.probePromise = (async () => {
        try {
          const remainingMs = Number.isFinite(deadlineMs)
            ? deadlineMs - monotonicNow()
            : null;
          if (remainingMs !== null && remainingMs <= 0) throw deadlineError();
          const deadlineBoundsProbe = remainingMs !== null
            && remainingMs <= this.probeTimeoutMs;
          const response = await this._call("probe", {
            contract_version: 1,
            expected_source_fingerprint: hasExpectedSource ? sourceFingerprint : null,
          }, {
            signal,
            timeoutMs: remainingMs === null
              ? this.probeTimeoutMs
              : Math.min(this.probeTimeoutMs, remainingMs),
            timeoutCode: deadlineBoundsProbe
              ? "browser-analysis-deadline"
              : "browser-worker-timeout",
          });
          if (
            response?.ready !== true
            || !validateIdentity(response)
            || (hasExpectedSource && response.source_fingerprint !== sourceFingerprint)
            || !expectedIdentityMatches(response, {
              engineProfileId,
              engineProfileName,
              engineVersion,
              rulesetVersion,
            })
          ) {
            this.disabledReason = String(response?.reason || "browser-kernel-not-certified");
            this._dropWorker(new BrowserEngineError(
              this.disabledReason,
              "browser-kernel-not-certified",
              { fallbackRequired: true },
            ));
            return { ready: false, reason: this.disabledReason };
          }
          this.identity = Object.freeze({
            certificate_schema: response.certificate_schema,
            certificate_status: response.certificate_status,
            contract_version: response.contract_version,
            abi_version: response.abi_version,
            source_fingerprint: response.source_fingerprint,
            wasm_sha256: response.wasm_sha256,
            module_js_sha256: response.module_js_sha256,
            analysis_ready: response.analysis_ready === true,
            prefix_ready: response.prefix_ready === true,
            root_session_ready: response.root_session_ready === true,
            mate_ready: response.mate_ready === true,
            root_iteration_ready: response.root_iteration_ready === true,
            safety_certified: response.safety_certified === true,
            certificate_id: response.certificate_id === null
              ? null
              : String(response.certificate_id || ""),
            prefix_certificate_id: response.prefix_certificate_id === null
              ? null
              : String(response.prefix_certificate_id || ""),
            root_session_certificate_id: response.root_session_certificate_id === null
              ? null
              : String(response.root_session_certificate_id || ""),
            mate_certificate_id: response.mate_certificate_id === null
              ? null
              : String(response.mate_certificate_id || ""),
            kernel_sha256: response.kernel_sha256 === null
              ? null
              : String(response.kernel_sha256 || ""),
            runtime_variant: response.runtime_variant,
            thread_count: response.thread_count,
            engine_profile_id: response.engine_profile_id || response.profile_id,
            engine_profile_name: response.engine_profile_name || response.profile_id,
            engine_version: response.engine_version,
            ruleset_version: response.ruleset_version,
            profile_id: response.profile_id === null
              ? null
              : String(response.profile_id || ""),
            analysis_limits: response.analysis_ready
              ? Object.freeze(normalizedAnalysisLimits(response.analysis_limits))
              : null,
            prefix_contract: response.prefix_ready
              ? PREFIX_API.validateCertifiedPrefixContract(response.prefix_contract)
              : null,
            root_session_contract: response.root_iteration_ready
              ? immutableJsonCopy(response.root_session_contract)
              : null,
            root_geometry: response.root_iteration_ready
              ? immutableJsonCopy(response.root_geometry)
              : null,
            memory_limits: Object.freeze(normalizedMemoryLimits(response.memory_limits)),
            ...(response.value_model_status !== undefined ? {
              value_model_status: response.value_model_status,
              value_model_active: response.value_model_active === true,
              value_model_failure_code: response.value_model_failure_code,
              value_model_id: response.value_model_id,
              value_model_sha256: response.value_model_sha256,
              value_model_variant_id: response.value_model_variant_id,
              value_model_native_source_identity: (
                response.value_model_native_source_identity
              ),
            } : {}),
          });
          this.profile = Object.freeze({
            engine_profile_id: response.engine_profile_id || response.profile_id,
            engine_profile_name: response.engine_profile_name || response.profile_id,
            engine_version: response.engine_version,
            ruleset_version: response.ruleset_version,
          });
          this.ready = true;
          return { ready: true, ...this.identity };
        } catch (error) {
          if (error?.name === "AbortError") throw error;
          if (error?.code === "browser-analysis-deadline") {
            this.ready = false;
            this._dropWorker(error);
            throw error;
          }
          if (TRANSIENT_WORKER_ERRORS.has(error?.code)) {
            this.ready = false;
            this._dropWorker(error);
            return { ready: false, reason: error.code };
          }
          this.disabledReason = String(error?.code || "browser-kernel-unavailable");
          this._dropWorker(error);
          return { ready: false, reason: this.disabledReason };
        } finally {
          this.probePromise = null;
        }
      })();
      return this.probePromise;
    }

    async analyze(payload, {
      signal,
      deadlineMs = null,
      searchDeadlineMs = deadlineMs,
      receiptDeadlineMs = searchDeadlineMs,
    } = {}) {
      if (!this._releaseRootPoolForSingleWorker()) {
        throw new BrowserEngineError(
          "The certified root pool is already searching.",
          "browser-engine-busy",
          { fallbackRequired: true },
        );
      }
      if (!this.ready && this.identity) {
        await this.preflight({
          sourceFingerprint: this.identity.source_fingerprint,
          engineProfileId: this.profile?.engine_profile_id,
          engineProfileName: this.profile?.engine_profile_name,
          engineVersion: this.profile?.engine_version,
          rulesetVersion: this.profile?.ruleset_version,
          signal,
          deadlineMs: searchDeadlineMs,
        });
      }
      if (!this.ready || !this.canAnalyze(payload)) {
        throw new BrowserEngineError(
          "The certified browser engine is not available for this request.",
          "browser-analysis-unavailable",
          { fallbackRequired: true },
        );
      }
      if (this.activeAnalysis !== null) {
        throw new BrowserEngineError(
          "The browser engine is already searching.",
          "browser-engine-busy",
          { fallbackRequired: true },
        );
      }
      const requestId = `browser-${this.nextRequestId++}`;
      const remainingSearchMs = Number.isFinite(searchDeadlineMs)
        ? searchDeadlineMs - monotonicNow()
        : null;
      if (remainingSearchMs !== null && remainingSearchMs < 10) throw deadlineError();
      const searchPayload = remainingSearchMs === null
        ? payload
        : {
          ...payload,
          time_limit: Math.min(
            Number(payload.time_limit),
            remainingSearchMs / 1000,
          ),
        };
      const request = normalizedKernelRequest(
        searchPayload,
        requestId,
        this.identity.analysis_limits,
      );
      this.activeAnalysis = requestId;
      const hostStarted = monotonicNow();
      try {
        const requestTimeoutMs = Math.ceil(
          request.limits.time_limit_seconds * 1000,
        ) + REQUEST_GRACE_MS;
        const remainingMs = Number.isFinite(receiptDeadlineMs)
          ? receiptDeadlineMs - monotonicNow()
          : null;
        if (remainingMs !== null && remainingMs <= 0) throw deadlineError();
        const timeoutMs = remainingMs === null
          ? requestTimeoutMs
          : remainingMs;
        const deadlineBoundsAnalysis = remainingMs !== null;
        const result = await this._call("analyze", request, {
          signal,
          timeoutMs,
          timeoutCode: deadlineBoundsAnalysis
            ? "browser-analysis-deadline"
            : "browser-worker-timeout",
        });
        const { requestedDepth, completedDepth } = validatePublishedAnalysis(
          result,
          request,
          this.identity,
        );
        const hostEnded = monotonicNow();
        const wallTimeSeconds = Math.max(0, (hostEnded - hostStarted) / 1000);
        const work = finiteNumber(result.work ?? result.stats.generation_positions);
        return {
          ...result,
          engine_profile_id: this.profile?.engine_profile_id,
          engine_profile_name: this.profile?.engine_profile_name,
          engine_version: this.profile?.engine_version,
          ruleset_version: this.profile?.ruleset_version,
          root_search_mode: "best-move",
          root_scores_complete: false,
          requested_depth: requestedDepth,
          completed_depth: completedDepth,
          ...(this.identity.value_model_status !== undefined ? {
            deep_teacher_value_model: {
              status: this.identity.value_model_status,
              active: this.identity.value_model_active,
              model_id: this.identity.value_model_id,
              model_sha256: this.identity.value_model_sha256,
              variant_id: this.identity.value_model_variant_id,
              native_source_identity: (
                this.identity.value_model_native_source_identity
              ),
              failure_code: this.identity.value_model_failure_code,
            },
          } : {}),
          runtime_receipt: {
            runtime: "browser-wasm",
            wall_time_seconds: wallTimeSeconds,
            requested_depth: requestedDepth,
            completed_depth: completedDepth,
            work,
            timed_out: result.timed_out === true,
            work_limit_reached: result.work_limit_reached === true,
            source_fingerprint: this.identity.source_fingerprint,
            artifact_fingerprint: this.identity.wasm_sha256,
            module_fingerprint: this.identity.module_js_sha256,
            certificate_id: this.identity.certificate_id,
            certificate_schema: this.identity.certificate_schema,
            contract_version: this.identity.contract_version,
            abi_version: this.identity.abi_version,
            runtime_variant: this.identity.runtime_variant,
            thread_count: this.identity.thread_count,
            memory_bytes: finiteNumber(result.memory_bytes),
            certified_memory: { ...this.identity.memory_limits },
            legal_validation_runtime: "compiled-wasm",
            canonical_replay_certified: true,
            ...(this.identity.value_model_status !== undefined ? {
              value_model_status: this.identity.value_model_status,
              value_model_id: this.identity.value_model_id,
              value_model_sha256: this.identity.value_model_sha256,
              value_model_variant_id: this.identity.value_model_variant_id,
            } : {}),
          },
        };
      } finally {
        if (this.activeAnalysis === requestId) this.activeAnalysis = null;
      }
    }

    async analyzeRoot(payload, {
      signal,
      deadlineMs = null,
      searchDeadlineMs = deadlineMs,
      receiptDeadlineMs = searchDeadlineMs,
    } = {}) {
      if (!this.canAnalyzeRoot(payload)) {
        throw new BrowserEngineError(
          "The certified iterative browser root engine is unavailable for this request.",
          "browser-root-analysis-unavailable",
          { fallbackRequired: true },
        );
      }
      try {
        if (this.worker) {
          this._dropWorker(new BrowserEngineError(
            "The preflight Worker was released before admitting the certified root pool.",
            "browser-worker-released-for-root",
            { fallbackRequired: true },
          ));
        }
        this.ready = false;
        const result = await this.rootRunner.analyze(payload, this.identity, {
          signal,
          deadlineMs: searchDeadlineMs,
          receiptDeadlineMs,
        });
        validatePublishedRootAnalysis(result, payload, this.identity);
        return {
          ...result,
          engine_profile_id: this.profile?.engine_profile_id,
          engine_profile_name: this.profile?.engine_profile_name,
          engine_version: this.profile?.engine_version,
          ruleset_version: this.profile?.ruleset_version,
          ...(this.identity.value_model_status !== undefined ? {
            deep_teacher_value_model: {
              status: this.identity.value_model_status,
              active: this.identity.value_model_active,
              model_id: this.identity.value_model_id,
              model_sha256: this.identity.value_model_sha256,
              variant_id: this.identity.value_model_variant_id,
              native_source_identity: (
                this.identity.value_model_native_source_identity
              ),
              failure_code: this.identity.value_model_failure_code,
            },
          } : {}),
        };
      } catch (error) {
        if (error?.name === "AbortError") throw error;
        throw new BrowserEngineError(
          String(error?.message || "The iterative browser root engine failed closed."),
          String(error?.code || "browser-root-analysis-failed"),
          { fallbackRequired: error?.fallbackRequired !== false, cause: error },
        );
      }
    }

    async inspectPrefix(payload, { signal } = {}) {
      if (!PREFIX_API) {
        throw new BrowserEngineError(
          "The certified browser prefix contract is unavailable.",
          "browser-prefix-contract-unavailable",
          { fallbackRequired: true },
        );
      }
      if (!this.canQueuePrefix(payload)) {
        throw new BrowserEngineError(
          "The certified browser prefix engine is unavailable for this request.",
          "browser-prefix-unavailable",
          { fallbackRequired: true },
        );
      }
      if (this._prefixLaneBusy()) await this._waitForPrefixLane(signal);
      const pooledPrefix = this.rootRunner?.hasLivePool?.() === true;
      if (!pooledPrefix && !this.ready && this.identity) {
        await this.preflight({
          sourceFingerprint: this.identity.source_fingerprint,
          engineProfileId: this.profile?.engine_profile_id,
          engineProfileName: this.profile?.engine_profile_name,
          engineVersion: this.profile?.engine_version,
          rulesetVersion: this.profile?.ruleset_version,
          signal,
        });
      }
      if (this._prefixLaneBusy()) await this._waitForPrefixLane(signal);
      const availablePooledPrefix = this.rootRunner?.hasLivePool?.() === true;
      if ((!this.ready && !availablePooledPrefix) || !this.canInspectPrefix(payload)) {
        throw new BrowserEngineError(
          "The certified browser prefix engine is unavailable for this request.",
          "browser-prefix-unavailable",
          { fallbackRequired: true },
        );
      }
      const requestId = `prefix-${this.nextPrefixRequestId++}`;
      const request = PREFIX_API.normalizePrefixRequest(
        payload,
        requestId,
        this.identity.prefix_contract,
      );
      this.activePrefix = requestId;
      try {
        const result = availablePooledPrefix
          ? await this.rootRunner.inspectPrefix(payload, this.identity, {
            signal,
            timeoutMs: this.probeTimeoutMs,
            requestId,
          })
          : await this._call("prefix", request, {
            signal,
            timeoutMs: this.probeTimeoutMs,
          });
        PREFIX_API.validatePrefixResult(result, request, this.identity);
        validateReportedMemory(result, this.identity);
        return result;
      } finally {
        if (this.activePrefix === requestId) this.activePrefix = null;
      }
    }

    close(reason = "browser engine client closed") {
      this.disabledReason = reason;
      this.ready = false;
      this.identity = null;
      this.rootRunner?.close(reason);
      this._dropWorker(new BrowserEngineError(
        reason,
        "browser-engine-closed",
        { fallbackRequired: true },
      ));
    }
  }

  const api = Object.freeze({
    BrowserEngineClient,
    BrowserEngineError,
    createClient: (options) => new BrowserEngineClient(options),
    isLocalBestMoveRequest,
    normalizedKernelRequest,
    validateReportedMemory,
    validateCompiledReplay,
    validatePublishedAnalysis,
    validatePublishedRootAnalysis,
  });
  globalThis.ScottishProgressiveBrowserEngine = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
