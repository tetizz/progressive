(() => {
  "use strict";

  const SOURCE_FINGERPRINT = /^[0-9a-f]{16}$/;
  const ARTIFACT_FINGERPRINT = /^[0-9a-f]{64}$/;
  const CERTIFICATE_SCHEMA = "spc-browser-wasm-certificate-v1";
  const PROMOTED_FINGERPRINT = /^[0-9a-f]{16}$/;
  const UCI_MOVE = /^[a-h][1-8][a-h][1-8][qrbn]?$/;
  const EP_SQUARE = /^[a-h][1-8]$/;
  const KNOWN_OUTCOMES = new Set(["checkmate", "stalemate", "ten-series-draw"]);
  const TRANSIENT_WORKER_ERRORS = new Set([
    "browser-worker-crashed",
    "browser-worker-post-failed",
    "browser-worker-timeout",
    "browser-worker-unavailable",
  ]);
  const DEFAULT_PROBE_TIMEOUT_MS = 30_000;
  const REQUEST_GRACE_MS = 1_000;
  const MAX_INITIAL_MEMORY_BYTES = 128 * 1024 * 1024;
  const MAXIMUM_MEMORY_BYTES = 256 * 1024 * 1024;
  const MAX_ESTIMATED_PEAK_MEMORY_BYTES = 192 * 1024 * 1024;
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

  function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
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
    return (
      SOURCE_FINGERPRINT.test(String(identity.source_fingerprint || ""))
      && ARTIFACT_FINGERPRINT.test(String(identity.wasm_sha256 || ""))
      && ARTIFACT_FINGERPRINT.test(String(identity.module_js_sha256 || ""))
      && identity.certificate_schema === CERTIFICATE_SCHEMA
      && identity.certificate_status === "certified"
      && identity.contract_version === 1
      && identity.abi_version === 1
      && identity.safety_certified === true
      && typeof identity.certificate_id === "string"
      && Boolean(identity.certificate_id)
      && identity.runtime_variant === "single"
      && identity.thread_count === 1
      && typeof identity.engine_profile_id === "string"
      && Boolean(identity.engine_profile_id)
      && typeof identity.engine_profile_name === "string"
      && Boolean(identity.engine_profile_name)
      && typeof identity.engine_version === "string"
      && Boolean(identity.engine_version)
      && typeof identity.ruleset_version === "string"
      && Boolean(identity.ruleset_version)
      && analysisLimits !== null
      && memoryLimits !== null
    );
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
    const memoryBytes = result.memory_bytes;
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
    validateCompiledReplay(result, request);
    return { requestedDepth, completedDepth };
  }

  class BrowserEngineClient {
    constructor({
      workerUrl,
      workerFactory,
      probeTimeoutMs = DEFAULT_PROBE_TIMEOUT_MS,
    } = {}) {
      this.workerUrl = workerUrl || `./browser-engine-worker.js${scriptVersion}`;
      this.workerFactory = workerFactory || ((url, options) => new Worker(url, options));
      this.probeTimeoutMs = probeTimeoutMs;
      this.worker = null;
      this.generation = 0;
      this.nextMessageId = 1;
      this.nextRequestId = 1;
      this.pending = new Map();
      this.identity = null;
      this.profile = null;
      this.ready = false;
      this.disabledReason = null;
      this.probePromise = null;
      this.activeAnalysis = null;
    }

    canAnalyze(payload) {
      return this.identity !== null
        && this.disabledReason === null
        && isLocalBestMoveRequest(payload, this.identity.analysis_limits);
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
      const expectedProfileMatches = this.identity === null || [
        [engineProfileId, this.identity.engine_profile_id],
        [engineProfileName, this.identity.engine_profile_name],
        [engineVersion, this.identity.engine_version],
        [rulesetVersion, this.identity.ruleset_version],
      ].every(([expected, actual]) => (
        expected === null || expected === undefined || expected === actual
      ));
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
          const expectedMetadata = [
            [engineProfileId, response?.engine_profile_id],
            [engineProfileName, response?.engine_profile_name],
            [engineVersion, response?.engine_version],
            [rulesetVersion, response?.ruleset_version],
          ];
          if (
            response?.ready !== true
            || !validateIdentity(response)
            || (hasExpectedSource && response.source_fingerprint !== sourceFingerprint)
            || expectedMetadata.some(([expected, actual]) => (
              expected !== null
              && expected !== undefined
              && expected !== actual
            ))
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
            safety_certified: true,
            certificate_id: String(response.certificate_id || ""),
            runtime_variant: response.runtime_variant,
            thread_count: response.thread_count,
            engine_profile_id: response.engine_profile_id,
            engine_profile_name: response.engine_profile_name,
            engine_version: response.engine_version,
            ruleset_version: response.ruleset_version,
            analysis_limits: Object.freeze(normalizedAnalysisLimits(response.analysis_limits)),
            memory_limits: Object.freeze(normalizedMemoryLimits(response.memory_limits)),
          });
          this.profile = Object.freeze({
            engine_profile_id: response.engine_profile_id,
            engine_profile_name: response.engine_profile_name,
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

    async analyze(payload, { signal, deadlineMs = null } = {}) {
      if (!this.ready && this.identity) {
        await this.preflight({
          sourceFingerprint: this.identity.source_fingerprint,
          engineProfileId: this.profile?.engine_profile_id,
          engineProfileName: this.profile?.engine_profile_name,
          engineVersion: this.profile?.engine_version,
          rulesetVersion: this.profile?.ruleset_version,
          signal,
          deadlineMs,
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
      const request = normalizedKernelRequest(
        payload,
        requestId,
        this.identity.analysis_limits,
      );
      this.activeAnalysis = requestId;
      const hostStarted = monotonicNow();
      try {
        const requestTimeoutMs = Math.ceil(
          request.limits.time_limit_seconds * 1000,
        ) + REQUEST_GRACE_MS;
        const remainingMs = Number.isFinite(deadlineMs)
          ? deadlineMs - monotonicNow()
          : null;
        if (remainingMs !== null && remainingMs <= 0) throw deadlineError();
        const timeoutMs = remainingMs === null
          ? requestTimeoutMs
          : Math.min(requestTimeoutMs, remainingMs);
        const deadlineBoundsAnalysis = remainingMs !== null
          && remainingMs <= requestTimeoutMs;
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
          },
        };
      } finally {
        if (this.activeAnalysis === requestId) this.activeAnalysis = null;
      }
    }

    close(reason = "browser engine client closed") {
      this.disabledReason = reason;
      this.ready = false;
      this.identity = null;
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
    validateCompiledReplay,
    validatePublishedAnalysis,
  });
  globalThis.ScottishProgressiveBrowserEngine = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
