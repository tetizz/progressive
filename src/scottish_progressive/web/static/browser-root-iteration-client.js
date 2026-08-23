(() => {
  "use strict";

  const ROOT_API = globalThis.ScottishProgressiveRootCoordinator || null;
  const PREFIX_API = globalThis.ScottishProgressiveBrowserPrefix || null;
  const SOURCE_FINGERPRINT = /^[0-9a-f]{16}$/;
  const ARTIFACT_FINGERPRINT = /^[0-9a-f]{64}$/;
  const UCI_MOVE = /^[a-h][1-8][a-h][1-8][qrbn]?$/;
  const MATE_SCORE = 1_000_000;
  const MAX_LOCAL_DEPTH = 5;
  const ASPIRATION_INITIAL_DELTA = 2_048;
  const MAX_ASPIRATION_ATTEMPTS = 4;
  const ROOT_TACTICAL_POLICY = "canonical-boundary-policy-v1";
  const ROOT_IDENTITY_KEYS = Object.freeze([
    "source_fingerprint", "kernel_sha256", "module_js_sha256", "certificate_id",
    "runtime_variant", "thread_count", "engine_version", "ruleset_version", "profile_id",
  ]);

  class RootIterationClientError extends Error {
    constructor(message, code, { fallbackRequired = true, cause } = {}) {
      super(message, cause === undefined ? undefined : { cause });
      this.name = "RootIterationClientError";
      this.code = code;
      this.fallbackRequired = fallbackRequired;
    }
  }

  function abortError(message = "Browser root search cancelled") {
    if (typeof DOMException === "function") return new DOMException(message, "AbortError");
    const error = new Error(message);
    error.name = "AbortError";
    return error;
  }

  function monotonicNow() {
    if (typeof globalThis.performance?.now !== "function") {
      throw new RootIterationClientError(
        "A monotonic browser clock is unavailable.",
        "browser-root-clock-unavailable",
      );
    }
    return globalThis.performance.now();
  }

  function monotonicDeadlineEpoch(deadlineMs) {
    const timeOrigin = globalThis.performance?.timeOrigin;
    if (!Number.isFinite(timeOrigin) || !Number.isFinite(deadlineMs)) {
      throw new RootIterationClientError(
        "The browser cannot bind the root deadline across Worker time origins.",
        "browser-root-clock-unavailable",
      );
    }
    return timeOrigin + deadlineMs;
  }

  function exactInteger(value, minimum, maximum = Number.MAX_SAFE_INTEGER) {
    return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
  }

  function normalizeAspirationReceipt(
    value,
    expected,
    expectedCandidateCount,
    { allowUnsearched = false } = {},
  ) {
    const enabled = expected !== null;
    const expectedKeys = [
      "attempts", "candidate_count", "center_score", "enabled", "exact_hits",
      "fail_highs", "fail_lows", "full_window_fallbacks", "initial_delta",
      "maximum_attempts",
    ];
    if (
      !value
      || typeof value !== "object"
      || Array.isArray(value)
      || !sameJson(Object.keys(value).sort(), expectedKeys)
      || value.enabled !== enabled
      || value.center_score !== (expected?.center_score ?? null)
      || value.initial_delta !== (expected?.initial_delta ?? null)
      || value.maximum_attempts !== MAX_ASPIRATION_ATTEMPTS
      || !exactInteger(expectedCandidateCount, 0)
      || value.candidate_count !== expectedCandidateCount
      || !exactInteger(
        value.attempts,
        0,
        expectedCandidateCount * MAX_ASPIRATION_ATTEMPTS,
      )
      || !exactInteger(value.fail_highs, 0, value.attempts)
      || !exactInteger(value.fail_lows, 0, value.attempts)
      || !exactInteger(value.exact_hits, 0, expectedCandidateCount)
      || !exactInteger(value.full_window_fallbacks, 0, expectedCandidateCount)
      || value.fail_highs + value.fail_lows + value.exact_hits !== value.attempts
      || value.exact_hits + value.full_window_fallbacks > expectedCandidateCount
      || (
        enabled
        && !allowUnsearched
        && value.exact_hits + value.full_window_fallbacks !== expectedCandidateCount
      )
      || (!enabled && (
        value.candidate_count !== 0
        || value.attempts !== 0
        || value.exact_hits !== 0
        || value.full_window_fallbacks !== 0
      ))
    ) return null;
    return Object.freeze({ ...value });
  }

  const EXACT_OWNER_PURPOSES = Object.freeze(new Set([
    "aspiration", "full", "selected-certification", "threat-research",
  ]));

  function exactCandidateOwnerMap(taskLog) {
    if (!Array.isArray(taskLog)) {
      throw new RootIterationClientError(
        "The root coordinator omitted its exact-owner task log.",
        "browser-root-aspiration-owner-map-invalid",
      );
    }
    const dispatched = new Map();
    const owners = new Map();
    for (const item of taskLog) {
      if (!item || typeof item !== "object" || Array.isArray(item)) {
        throw new RootIterationClientError(
          "The root coordinator returned a malformed owner task log.",
          "browser-root-aspiration-owner-map-invalid",
        );
      }
      if (item.event === "safety") continue;
      if (
        !["dispatch", "complete"].includes(item.event)
        || typeof item.task_id !== "string"
        || !item.task_id
        || typeof item.candidate_identity !== "string"
        || !item.candidate_identity
        || typeof item.worker_id !== "string"
        || !item.worker_id
        || typeof item.purpose !== "string"
        || !item.purpose
      ) {
        throw new RootIterationClientError(
          "The root coordinator returned a malformed owner task log.",
          "browser-root-aspiration-owner-map-invalid",
        );
      }
      if (item.event === "dispatch") {
        if (dispatched.has(item.task_id)) {
          throw new RootIterationClientError(
            "The root coordinator duplicated an owner task identity.",
            "browser-root-aspiration-owner-map-invalid",
          );
        }
        dispatched.set(item.task_id, item);
        continue;
      }
      const start = dispatched.get(item.task_id);
      if (
        !start
        || start.candidate_identity !== item.candidate_identity
        || start.worker_id !== item.worker_id
        || start.purpose !== item.purpose
      ) {
        throw new RootIterationClientError(
          "The root coordinator completed an unbound owner task.",
          "browser-root-aspiration-owner-map-invalid",
        );
      }
      if (item.bound !== "exact" || !EXACT_OWNER_PURPOSES.has(item.purpose)) continue;
      const prior = owners.get(item.candidate_identity);
      if (prior !== undefined && prior !== item.worker_id) {
        throw new RootIterationClientError(
          "A root candidate claimed multiple exact owning Workers.",
          "browser-root-aspiration-owner-map-invalid",
        );
      }
      owners.set(item.candidate_identity, item.worker_id);
    }
    return owners;
  }

  function scheduleAspirationAffinity({
    adapters,
    manifest,
    initialFullWave,
    aspiration,
    previousOwners,
  }) {
    const candidateIds = aspiration === null
      ? []
      : manifest.candidates
        .filter((candidate) => candidate.terminal_score === null)
        .slice(0, initialFullWave)
        .map((candidate) => candidate.candidate_identity);
    const ownerIds = candidateIds.map((candidateId) => (
      previousOwners.get(candidateId) ?? null
    ));
    const completeAndUnique = candidateIds.length > 0
      && ownerIds.every((ownerId) => ownerId !== null)
      && new Set(ownerIds).size === ownerIds.length;
    if (!completeAndUnique) {
      return Object.freeze({
        adapters,
        candidateIds: Object.freeze([...candidateIds]),
        ownerIds: Object.freeze([]),
        warmOwnerReused: false,
      });
    }
    const adaptersById = new Map(adapters.map((adapter) => [adapter.id, adapter]));
    const warmAdapters = ownerIds.map((ownerId) => adaptersById.get(ownerId));
    if (warmAdapters.some((adapter) => adapter === undefined)) {
      throw new RootIterationClientError(
        "A claimed prior exact-owner Worker is unavailable for warm aspiration.",
        "browser-root-aspiration-owner-unavailable",
      );
    }
    const warmOwnerSet = new Set(ownerIds);
    return Object.freeze({
      adapters: Object.freeze([
        ...warmAdapters,
        ...adapters.filter((adapter) => !warmOwnerSet.has(adapter.id)),
      ]),
      candidateIds: Object.freeze([...candidateIds]),
      ownerIds: Object.freeze([...ownerIds]),
      warmOwnerReused: true,
    });
  }

  function validateAspirationAffinity(taskLog, affinity) {
    if (!affinity.warmOwnerReused) return;
    const expected = new Map(affinity.candidateIds.map((candidateId, index) => (
      [candidateId, affinity.ownerIds[index]]
    )));
    const firstDispatch = new Map();
    for (const task of taskLog) {
      if (task?.event !== "dispatch" || task.purpose !== "aspiration") continue;
      if (!firstDispatch.has(task.candidate_identity)) {
        firstDispatch.set(task.candidate_identity, task.worker_id);
      }
      const expectedOwner = expected.get(task.candidate_identity);
      if (expectedOwner !== undefined && task.worker_id !== expectedOwner) {
        throw new RootIterationClientError(
          "An aspiration retry left its prior exact-owner Worker.",
          "browser-root-aspiration-owner-mismatch",
        );
      }
    }
    if ([...expected].some(([candidateId, ownerId]) => (
      firstDispatch.get(candidateId) !== ownerId
    ))) {
      throw new RootIterationClientError(
        "The initial aspiration wave did not reuse every claimed exact owner.",
        "browser-root-aspiration-owner-mismatch",
      );
    }
  }

  function sameArray(left, right) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => item === right[index]);
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

  function canonicalBoundary(payload) {
    const promoted = String(payload?.promoted_hex || "").toLowerCase().replace(/^0x/, "");
    const epTargets = Array.isArray(payload?.ep_targets)
      ? payload.ep_targets.map(String).sort()
      : null;
    if (
      typeof payload?.fen !== "string"
      || !payload.fen
      || !exactInteger(payload.series, 1, 256)
      || !exactInteger(payload.quiet_series, 0, 1_000_000)
      || epTargets === null
      || epTargets.length > 8
      || epTargets.some((square) => !/^[a-h][1-8]$/.test(square))
      || new Set(epTargets).size !== epTargets.length
      || !/^[0-9a-f]{1,16}$/.test(promoted)
      || payload?.chess960 === true
    ) return null;
    return Object.freeze({
      fen: payload.fen,
      series: payload.series,
      quiet_series: payload.quiet_series,
      ep_targets: Object.freeze(epTargets),
      promoted_hex: promoted.padStart(16, "0"),
      chess960: false,
    });
  }

  function canonicalRootTacticalProtection(boundary) {
    const core = canonicalBoundary(boundary);
    const board = core?.fen.split(" ")[0];
    const ranks = board?.split("/");
    if (!core || !Array.isArray(ranks) || ranks.length !== 8) return null;
    if (core.series >= 5) return true;
    const white = core.series % 2 === 1;
    const pawn = white ? "P" : "p";
    for (let row = 0; row < ranks.length; row += 1) {
      const expanded = ranks[row].replace(/[1-8]/g, (digit) => " ".repeat(Number(digit)));
      if (expanded.length !== 8) return null;
      const distance = white ? row : 7 - row;
      if (
        distance > 0
        && core.series - distance >= 2
        && expanded.includes(pawn)
      ) return true;
    }
    return false;
  }

  function normalizeExactBoundaryState(value) {
    const core = canonicalBoundary(value);
    const fields = typeof value?.fen === "string" ? value.fen.split(" ") : [];
    const expectedKeys = [
      "board_fen", "chess960", "ep_targets", "fen", "progressive_ep",
      "promoted_hex", "quiet_draw_pending", "quiet_series", "series",
      "series_number", "side_to_move",
    ];
    if (
      !core
      || !sameJson(Object.keys(value).sort(), expectedKeys)
      || fields.length !== 6
      || !["w", "b"].includes(fields[1])
      || value.board_fen !== value.fen
      || value.series_number !== value.series
      || value.side_to_move !== (fields[1] === "w" ? "white" : "black")
      || ((value.series % 2 === 1) !== (fields[1] === "w"))
      || value.quiet_draw_pending !== (value.quiet_series >= 10)
      || !sameArray(value.ep_targets, core.ep_targets)
      || !sameArray(value.progressive_ep, core.ep_targets)
      || value.promoted_hex !== core.promoted_hex
      || value.chess960 !== false
    ) return null;
    return Object.freeze({
      ...value,
      ep_targets: Object.freeze([...core.ep_targets]),
      progressive_ep: Object.freeze([...core.ep_targets]),
    });
  }

  function normalizedRootPlayLimits(identity) {
    const sessionConfig = identity?.root_geometry?.session_config;
    const playLimits = identity?.root_geometry?.play_limits;
    const expectedKeys = [
      "default_generation_positions", "default_seconds", "maximum_seconds",
      "safety_reserve_positions",
    ];
    if (
      !sessionConfig
      || !playLimits
      || typeof playLimits !== "object"
      || Array.isArray(playLimits)
      || !sameJson(Object.keys(playLimits).sort(), expectedKeys)
      || !Number.isFinite(playLimits.maximum_seconds)
      || playLimits.maximum_seconds <= 0
      || !Number.isFinite(playLimits.default_seconds)
      || playLimits.default_seconds <= 0
      || playLimits.default_seconds > playLimits.maximum_seconds
      || !exactInteger(playLimits.default_generation_positions, 1_000, sessionConfig.max_work)
      || !exactInteger(playLimits.safety_reserve_positions, 1, sessionConfig.max_work)
    ) return null;
    return Object.freeze({ ...playLimits });
  }

  function canRunRequest(payload, identity) {
    const sessionConfig = identity?.root_geometry?.session_config;
    const playLimits = normalizedRootPlayLimits(identity);
    return Boolean(
      ROOT_API
      && PREFIX_API
      && identity?.root_iteration_ready === true
      && identity?.root_session_ready === true
      && identity?.mate_ready === true
      && identity?.prefix_ready === true
      && payload
      && typeof payload === "object"
      && !Array.isArray(payload)
      && payload.best_move_only === true
      && payload.rate_move === false
      && payload.save === false
      && Number(payload.alternatives) === 0
      && Array.isArray(payload.prefix)
      && payload.prefix.length === 0
      && canonicalBoundary(payload) !== null
      && exactInteger(payload.depth, 1, MAX_LOCAL_DEPTH)
      && exactInteger(payload.max_series, 1, 16_384)
      && Number.isFinite(Number(payload.time_limit))
      && Number(payload.time_limit) > 0
      && exactInteger(payload.max_generation_positions, 1_000, 0xffffffff)
      && sessionConfig
      && playLimits
      && payload.depth <= sessionConfig.max_depth
      && payload.max_series === sessionConfig.width
      && Number(payload.time_limit) <= playLimits.maximum_seconds
      && payload.max_generation_positions <= sessionConfig.max_work
      && sessionConfig.mate_score === MATE_SCORE
      && sessionConfig.worker_threads === 1
      && identity?.root_session_contract?.capabilities?.aspiration_windows === true
      && identity?.root_session_contract?.hard_limits?.minimum_aspiration_initial_delta
        === ASPIRATION_INITIAL_DELTA
      && identity?.root_session_contract?.hard_limits?.maximum_aspiration_attempts
        === MAX_ASPIRATION_ATTEMPTS
    );
  }

  function rootIdentity(identity) {
    const certificateId = String(identity?.root_session_certificate_id || "");
    const profileId = String(identity?.profile_id || identity?.engine_profile_id || "");
    if (
      !SOURCE_FINGERPRINT.test(String(identity?.source_fingerprint || ""))
      || !ARTIFACT_FINGERPRINT.test(String(identity?.kernel_sha256 || ""))
      || !ARTIFACT_FINGERPRINT.test(String(identity?.module_js_sha256 || ""))
      || !certificateId
      || identity?.runtime_variant !== "single"
      || identity?.thread_count !== 1
      || typeof identity?.engine_version !== "string"
      || !identity.engine_version
      || typeof identity?.ruleset_version !== "string"
      || !identity.ruleset_version
      || !profileId
    ) {
      throw new RootIterationClientError(
        "The browser root identity is incomplete.",
        "browser-root-identity-invalid",
      );
    }
    return Object.freeze({
      source_fingerprint: identity.source_fingerprint,
      kernel_sha256: identity.kernel_sha256,
      module_js_sha256: identity.module_js_sha256,
      certificate_id: certificateId,
      runtime_variant: "single",
      thread_count: 1,
      engine_version: identity.engine_version,
      ruleset_version: identity.ruleset_version,
      profile_id: profileId,
    });
  }

  function identityMatches(actual, expected, certifiedIdentity = null) {
    return actual?.root_iteration_ready === true
      && actual?.root_session_ready === true
      && actual?.mate_ready === true
      && actual?.prefix_ready === true
      && actual?.source_fingerprint === expected.source_fingerprint
      && actual?.kernel_sha256 === expected.kernel_sha256
      && actual?.module_js_sha256 === expected.module_js_sha256
      && actual?.root_session_certificate_id === expected.certificate_id
      && actual?.runtime_variant === expected.runtime_variant
      && actual?.thread_count === expected.thread_count
      && actual?.engine_version === expected.engine_version
      && actual?.ruleset_version === expected.ruleset_version
      && (actual?.profile_id || actual?.engine_profile_id) === expected.profile_id
      && (
        certifiedIdentity === null
        || (
          actual?.wasm_sha256 === certifiedIdentity.wasm_sha256
          && actual?.mate_certificate_id === certifiedIdentity.mate_certificate_id
          && actual?.prefix_certificate_id === certifiedIdentity.prefix_certificate_id
          && sameJson(actual?.memory_limits, certifiedIdentity.memory_limits)
          && sameJson(actual?.root_geometry, certifiedIdentity.root_geometry)
          && sameJson(actual?.prefix_contract, certifiedIdentity.prefix_contract)
        )
      );
  }

  function mateProofCacheKey(identity, childBoundary) {
    const expected = rootIdentity(identity);
    const child = normalizeExactBoundaryState(childBoundary);
    if (
      child === null
      || !ARTIFACT_FINGERPRINT.test(String(identity?.wasm_sha256 || ""))
      || typeof identity?.mate_certificate_id !== "string"
      || !identity.mate_certificate_id
      || typeof identity?.prefix_certificate_id !== "string"
      || !identity.prefix_certificate_id
      || identity?.mate_ready !== true
      || identity?.prefix_ready !== true
      || identity?.root_session_contract?.abi_version !== 2
      || identity?.prefix_contract?.abi_version !== 1
      || identity?.root_geometry?.session_config?.mate_score !== MATE_SCORE
    ) {
      throw new RootIterationClientError(
        "The reply-mate cache key is not bound to an exact certified kernel boundary.",
        "browser-root-mate-cache-key-invalid",
      );
    }
    return JSON.stringify(canonicalJsonValue({
      schema: "spc-root-mate-proof-cache-key-v1",
      source_fingerprint: expected.source_fingerprint,
      wasm_sha256: identity.wasm_sha256,
      kernel_sha256: expected.kernel_sha256,
      module_js_sha256: expected.module_js_sha256,
      root_session_certificate_id: expected.certificate_id,
      mate_certificate_id: identity.mate_certificate_id,
      prefix_certificate_id: identity.prefix_certificate_id,
      engine_version: expected.engine_version,
      ruleset_version: expected.ruleset_version,
      profile_id: expected.profile_id,
      runtime_variant: expected.runtime_variant,
      thread_count: expected.thread_count,
      root_session_abi_version: 2,
      mate_abi_version: 1,
      prefix_abi_version: 1,
      mate_score: MATE_SCORE,
      authoritative_child_boundary: child,
    }));
  }

  function selectCertifiedGeometry(identity, navigatorValue = globalThis.navigator) {
    const geometry = identity?.root_geometry;
    if (!geometry || typeof geometry !== "object" || Array.isArray(geometry)) {
      throw new RootIterationClientError(
        "The browser root Worker geometry is not certified.",
        "browser-root-geometry-invalid",
      );
    }
    const candidates = [{
      workers: geometry.desktop_workers,
      initial_full_wave: geometry.desktop_initial_full_wave,
      aggregate_maximum_bytes: geometry.aggregate_maximum_bytes,
      desktop: true,
    }, ...(Array.isArray(geometry.supported_lower_geometries)
      ? geometry.supported_lower_geometries.map((item) => ({ ...item, desktop: false }))
      : [])];
    const hardware = Math.max(1, Math.floor(Number(navigatorValue?.hardwareConcurrency) || 1));
    const deviceMemory = Number(navigatorValue?.deviceMemory);
    const memoryAware = Number.isFinite(deviceMemory) && deviceMemory > 0;
    const fitting = candidates.filter((candidate) => (
      exactInteger(candidate.workers, 1, 64)
      && exactInteger(candidate.initial_full_wave, 1, candidate.workers)
      && exactInteger(candidate.aggregate_maximum_bytes, 1)
      && candidate.workers <= hardware
      && (!candidate.desktop || (memoryAware && deviceMemory >= 8))
      && (!memoryAware || candidate.aggregate_maximum_bytes
        <= deviceMemory * 1024 * 1024 * 1024 / 4)
    ));
    fitting.sort((left, right) => memoryAware ? (
      right.workers - left.workers
      || right.initial_full_wave - left.initial_full_wave
    ) : (
      left.aggregate_maximum_bytes - right.aggregate_maximum_bytes
      || left.workers - right.workers
    ));
    const selected = fitting[0];
    if (!selected) {
      throw new RootIterationClientError(
        "This device has no certified local Worker geometry.",
        "browser-root-geometry-unavailable",
      );
    }
    return Object.freeze(selected);
  }

  class RootWorkerChannel {
    constructor({ id, workerUrl, workerFactory, onCrash }) {
      this.id = id;
      this.workerUrl = workerUrl;
      this.workerFactory = workerFactory;
      this.onCrash = onCrash;
      this.worker = null;
      this.pending = new Map();
      this.nextId = 1;
      this.closed = false;
      this.crashed = false;
      this.sessionReady = false;
      this.sessionId = null;
      this.canonicalRootTacticalProtection = null;
      this.nativeWorkAfter = 0;
      this.memoryBytes = 0;
      this.memoryPeakBytes = 0;
    }

    _spawn() {
      if (this.worker) return this.worker;
      if (this.closed) {
        throw new RootIterationClientError(
          "The root Worker channel is closed.",
          "browser-root-worker-closed",
        );
      }
      let worker;
      try {
        worker = this.workerFactory(this.workerUrl, {
          type: "module",
          name: `scottish-progressive-root-${this.id}`,
        });
      } catch (cause) {
        throw new RootIterationClientError(
          "This browser could not start a root-search Worker.",
          "browser-root-worker-unavailable",
          { cause },
        );
      }
      worker.addEventListener("message", (event) => {
        const message = event?.data;
        const entry = this.pending.get(message?.id);
        if (!entry) return;
        this.pending.delete(message.id);
        entry.cleanup();
        if (message.ok === true) entry.resolve(message.payload);
        else entry.reject(new RootIterationClientError(
          String(message.error?.message || "The root Worker failed."),
          String(message.error?.code || "browser-root-worker-error"),
        ));
      });
      const fail = (event) => {
        if (this.worker !== worker || this.closed) return;
        this.crashed = true;
        const error = new RootIterationClientError(
          "A root-search Worker stopped unexpectedly.",
          "browser-root-worker-crashed",
          { cause: event?.error },
        );
        this.close(error);
        this.onCrash?.(this, error);
      };
      worker.addEventListener("error", fail);
      worker.addEventListener("messageerror", fail);
      this.worker = worker;
      return worker;
    }

    call(type, payload, { signal, deadlineMs } = {}) {
      if (signal?.aborted) return Promise.reject(abortError());
      let worker;
      try {
        worker = this._spawn();
      } catch (error) {
        return Promise.reject(error);
      }
      const remainingMs = Number.isFinite(deadlineMs)
        ? deadlineMs - monotonicNow()
        : null;
      if (remainingMs !== null && remainingMs <= 0) {
        return Promise.reject(new RootIterationClientError(
          "The common root deadline expired.",
          "browser-root-deadline",
        ));
      }
      const id = this.nextId++;
      return new Promise((resolve, reject) => {
        let timer = null;
        const onAbort = () => {
          if (!this.pending.has(id)) return;
          this.pending.delete(id);
          cleanup();
          reject(abortError());
          const error = new RootIterationClientError(
            "The root Worker was terminated at the cancellation boundary.",
            "browser-root-worker-cancelled",
          );
          this.close(error);
          this.onCrash?.(this, error);
        };
        const cleanup = () => {
          if (timer !== null) globalThis.clearTimeout(timer);
          signal?.removeEventListener?.("abort", onAbort);
        };
        this.pending.set(id, { resolve, reject, cleanup });
        signal?.addEventListener?.("abort", onAbort, { once: true });
        if (signal?.aborted) {
          onAbort();
          return;
        }
        if (remainingMs !== null) {
          timer = globalThis.setTimeout(() => {
            if (!this.pending.has(id)) return;
            this.pending.delete(id);
            cleanup();
            const error = new RootIterationClientError(
              "The root Worker exceeded the common deadline.",
              "browser-root-deadline",
            );
            reject(error);
            this.close(error);
            this.onCrash?.(this, error);
          }, Math.min(2_147_483_647, Math.ceil(remainingMs)));
        }
        try {
          worker.postMessage({ id, type, payload });
        } catch (cause) {
          this.pending.delete(id);
          cleanup();
          const error = new RootIterationClientError(
            "The root Worker request could not be delivered.",
            "browser-root-worker-post-failed",
            { cause },
          );
          reject(error);
          this.close(error);
          this.onCrash?.(this, error);
        }
      });
    }

    close(error = new RootIterationClientError(
      "The root Worker channel was closed.",
      "browser-root-worker-closed",
    )) {
      if (this.closed && !this.worker) return;
      this.closed = true;
      const worker = this.worker;
      this.worker = null;
      this.sessionReady = false;
      this.sessionId = null;
      this.canonicalRootTacticalProtection = null;
      try {
        worker?.terminate();
      } catch {
        // Termination is idempotent at the Worker boundary.
      }
      for (const [id, entry] of this.pending) {
        this.pending.delete(id);
        entry.cleanup();
        entry.reject(error);
      }
    }
  }

  function normalizeRootSeries(value) {
    const moves = Array.isArray(value?.moves)
      ? value.moves.map(String)
      : typeof value?.machine_notation === "string"
        ? value.machine_notation.split("/").filter(Boolean)
        : [];
    const childBoundary = normalizeExactBoundaryState(value?.child_boundary || {});
    const expectedKeys = [
      "child_boundary", "ended_by_check", "machine_notation", "moves",
      "outcome", "transposition_count",
    ];
    if (
      !value
      || typeof value !== "object"
      || Array.isArray(value)
      || !sameJson(Object.keys(value).sort(), expectedKeys)
      || moves.length < 1
      || moves.some((move) => !UCI_MOVE.test(move))
      || value.machine_notation !== moves.join("/")
      || !exactInteger(value.transposition_count, 1)
      || childBoundary === null
      || childBoundary.series < 2
      || ![null, "checkmate", "stalemate", "ten_series_draw"].includes(value.outcome)
      || typeof value.ended_by_check !== "boolean"
    ) {
      throw new RootIterationClientError(
        "The selected root series omitted its canonical child state.",
        "browser-root-series-invalid",
      );
    }
    return Object.freeze({ ...value, moves: Object.freeze(moves), child_boundary: childBoundary });
  }

  function sameBoundary(left, right) {
    const a = canonicalBoundary(left);
    const b = canonicalBoundary(right);
    return Boolean(
      a && b
      && a.fen === b.fen
      && a.series === b.series
      && a.quiet_series === b.quiet_series
      && sameArray(a.ep_targets, b.ep_targets)
      && a.promoted_hex === b.promoted_hex
      && a.chess960 === b.chess960
    );
  }

  function sameExactBoundary(left, right) {
    const a = normalizeExactBoundaryState(left);
    const b = normalizeExactBoundaryState(right);
    return Boolean(a && b && sameJson(a, b));
  }

  function validateCallReceipt(reply, channel, credit) {
    const work = reply?.work;
    if (
      !reply
      || typeof reply !== "object"
      || Array.isArray(reply)
      || !work
      || work.call_work_credit !== credit
      || work.native_work_before !== channel.nativeWorkAfter
      || !exactInteger(work.native_work_after, work.native_work_before)
      || work.call_native_work !== work.native_work_after - work.native_work_before
      || work.call_native_work > credit
      || !exactInteger(reply.memory_bytes, 1)
      || !exactInteger(reply.memory_peak_bytes, reply.memory_bytes)
    ) {
      throw new RootIterationClientError(
        "A root-session setup call returned an invalid work or memory receipt.",
        "browser-root-setup-receipt-invalid",
      );
    }
    channel.nativeWorkAfter = work.native_work_after;
    channel.memoryBytes = reply.memory_bytes;
    channel.memoryPeakBytes = Math.max(channel.memoryPeakBytes, reply.memory_peak_bytes);
    return reply;
  }

  function recordChannelMemory(reply, channel) {
    if (
      exactInteger(reply?.memory_bytes, 1)
      && exactInteger(reply?.memory_peak_bytes, reply.memory_bytes)
    ) {
      channel.memoryBytes = reply.memory_bytes;
      channel.memoryPeakBytes = Math.max(channel.memoryPeakBytes, reply.memory_peak_bytes);
    }
  }

  class RootIterationRunner {
    constructor({ workerUrl, workerFactory, navigatorValue } = {}) {
      this.workerUrl = workerUrl;
      this.workerFactory = workerFactory || ((url, options) => new Worker(url, options));
      this.navigatorValue = navigatorValue || globalThis.navigator;
      this.pool = [];
      this.poolIdentity = null;
      this.geometry = null;
      this.active = false;
      this.nextRequestId = 1;
      this.crashError = null;
      this.lastSafe = null;
      this.mateProofCache = new Map();
      this.mateProofCacheLimit = 256;
    }

    canAnalyze(payload, identity) {
      return !this.active && canRunRequest(payload, identity);
    }

    hasLivePool() {
      return this.pool.length > 0;
    }

    releasePool(reason = "browser root pool released") {
      if (this.active) {
        throw new RootIterationClientError(
          "The active root Worker pool cannot be released mid-request.",
          "browser-root-busy",
        );
      }
      this._closePool(new RootIterationClientError(
        reason,
        "browser-root-pool-released",
      ));
      this.lastSafe = null;
    }

    async inspectPrefix(payload, identity, {
      signal,
      timeoutMs = 30_000,
      requestId = null,
    } = {}) {
      if (this.active || !this.pool.length || signal?.aborted) {
        if (signal?.aborted) throw abortError();
        throw new RootIterationClientError(
          "The certified root pool is unavailable for prefix replay.",
          "browser-root-prefix-unavailable",
        );
      }
      const expected = rootIdentity(identity);
      if (this.poolIdentity !== this._poolKey(identity, this.geometry)) {
        throw new RootIterationClientError(
          "The retained root pool identity changed before prefix replay.",
          "browser-root-worker-identity-mismatch",
        );
      }
      const deadlineMs = monotonicNow() + Math.max(1, Number(timeoutMs) || 30_000);
      const request = PREFIX_API.normalizePrefixRequest(
        payload,
        requestId || `root-prefix-${this.nextRequestId++}`,
        identity.prefix_contract,
      );
      const channel = this.pool[0];
      const result = await channel.call("prefix", request, { signal, deadlineMs });
      PREFIX_API.validatePrefixResult(result, request, identity);
      if (!identityMatches(identity, expected) || !exactInteger(result.memory_bytes, 1)) {
        throw new RootIterationClientError(
          "The retained root pool returned an unbound prefix replay.",
          "browser-root-prefix-invalid",
        );
      }
      channel.memoryBytes = result.memory_bytes;
      channel.memoryPeakBytes = Math.max(channel.memoryPeakBytes, result.memory_bytes);
      return result;
    }

    _poolKey(identity, geometry) {
      return JSON.stringify({
        source: identity.source_fingerprint,
        kernel: identity.kernel_sha256,
        module: identity.module_js_sha256,
        certificate: identity.root_session_certificate_id,
        mate: identity.mate_certificate_id,
        prefix: identity.prefix_certificate_id,
        workers: geometry.workers,
        initial: geometry.initial_full_wave,
      });
    }

    _closePool(error) {
      const pool = this.pool;
      this.pool = [];
      this.poolIdentity = null;
      this.geometry = null;
      pool.forEach((channel) => channel.close(error));
    }

    async _ensurePool(identity, deadlineMs, signal) {
      const expected = rootIdentity(identity);
      const geometry = selectCertifiedGeometry(identity, this.navigatorValue);
      const key = this._poolKey(identity, geometry);
      if (this.pool.length && this.poolIdentity === key) return { expected, geometry };
      if (this.pool.length) {
        this._closePool(new RootIterationClientError(
          "The root Worker identity changed.",
          "browser-root-worker-incompatible",
        ));
      }
      this.crashError = null;
      const onCrash = (_channel, error) => {
        this.crashError = error;
        this._closePool(error);
      };
      const pool = Array.from({ length: geometry.workers }, (_, index) => (
        new RootWorkerChannel({
          id: `root-${index}`,
          workerUrl: this.workerUrl,
          workerFactory: this.workerFactory,
          onCrash,
        })
      ));
      try {
        const probes = await Promise.all(pool.map((channel) => channel.call("probe", {
          contract_version: 1,
          expected_source_fingerprint: expected.source_fingerprint,
        }, { signal, deadlineMs })));
        if (probes.some((probe) => !identityMatches(probe, expected, identity))) {
          throw new RootIterationClientError(
            "A root Worker loaded a different certified kernel identity.",
            "browser-root-worker-identity-mismatch",
          );
        }
      } catch (error) {
        pool.forEach((channel) => channel.close(error));
        throw error;
      }
      this.pool = pool;
      this.poolIdentity = key;
      this.geometry = geometry;
      return { expected, geometry };
    }

    async _resetSessions({ identity, expected, boundary, requestId, deadlineMs, signal }) {
      const expectedCanonicalProtection = canonicalRootTacticalProtection(boundary);
      if (expectedCanonicalProtection === null) {
        throw new RootIterationClientError(
          "The root boundary cannot select a canonical tactical policy.",
          "browser-root-session-create-invalid",
        );
      }
      await Promise.all(this.pool.map(async (channel) => {
        if (channel.sessionReady) {
          const destroyed = await channel.call("root-session-destroy", {
            schema: "spc-root-session-destroy-request-v1",
            session_id: channel.sessionId,
          }, { signal, deadlineMs });
          if (
            destroyed?.status !== "destroyed"
            || destroyed.session_id !== channel.sessionId
          ) {
            throw new RootIterationClientError(
              "The previous native root session did not close exactly.",
              "browser-root-session-destroy-invalid",
            );
          }
        }
        channel.sessionReady = false;
        channel.sessionId = null;
        channel.canonicalRootTacticalProtection = null;
        channel.nativeWorkAfter = 0;
        channel.memoryBytes = 0;
        channel.memoryPeakBytes = 0;
        const sessionConfig = identity.root_geometry?.session_config;
        if (!sessionConfig || typeof sessionConfig !== "object" || Array.isArray(sessionConfig)) {
          throw new RootIterationClientError(
            "The root session configuration is not certificate-bound.",
            "browser-root-session-config-invalid",
          );
        }
        const createRequestId = `${requestId}:create:${channel.id}`;
        const response = await channel.call("root-session-create", {
          schema: "spc-root-session-create-v1",
          request_id: createRequestId,
          iteration_id: requestId,
          generation: 0,
          ...expected,
          boundary,
          config: sessionConfig,
        }, { signal, deadlineMs });
        if (
          response?.status !== "ready"
          || !exactInteger(response.session_id, 1, 0xffffffff)
          || response.schema !== "spc-root-session-create-result-v1"
          || response.abi_version !== 2
          || response.request_id !== createRequestId
          || response.iteration_id !== requestId
          || response.generation !== 0
          || Object.entries(expected).some(([key, value]) => response[key] !== value)
          || !sameBoundary(response.boundary, boundary)
          || !sameJson(response.config, sessionConfig)
          || response.configured_max_depth !== sessionConfig.max_depth
          || response.native_work_after !== 0
          || response.capabilities?.aspiration_windows !== true
          || response.capabilities?.selected_owner_certification !== true
          || response.capabilities?.canonical_root_tactical_policy !== true
          || response.capabilities?.reply_mate_safety !== false
          || response.canonical_root_tactical_policy !== ROOT_TACTICAL_POLICY
          || response.canonical_root_tactical_protection
            !== expectedCanonicalProtection
          || response.product_publishable !== false
          || response.safety_certified !== false
          || !exactInteger(response.memory_bytes, 1)
          || !exactInteger(response.memory_peak_bytes, response.memory_bytes)
        ) {
          throw new RootIterationClientError(
            "The native root session did not bind its boundary and certified identity.",
            "browser-root-session-create-invalid",
          );
        }
        channel.sessionId = response.session_id;
        channel.sessionReady = true;
        channel.canonicalRootTacticalProtection =
          response.canonical_root_tactical_protection;
        channel.nativeWorkAfter = exactInteger(response.native_work_after, 0)
          ? response.native_work_after
          : 0;
        channel.memoryBytes = response.memory_bytes;
        channel.memoryPeakBytes = response.memory_peak_bytes;
      }));
      if (new Set(this.pool.map(
        (channel) => channel.canonicalRootTacticalProtection,
      )).size !== 1) {
        throw new RootIterationClientError(
          "The root Worker pool disagreed on the canonical tactical policy.",
          "browser-root-session-create-invalid",
        );
      }
    }

    async _enumerateAndImport({
      requestBase,
      preferredSeries,
      width,
      remainingWork,
      safetyWork,
      deadlineEpochMs,
      signal,
    }) {
      const primary = this.pool[0];
      if (!exactInteger(safetyWork, 0)) {
        throw new RootIterationClientError(
          "The cumulative root safety work receipt is invalid.",
          "browser-root-work-receipt-invalid",
        );
      }
      const perCallCredit = Math.max(1, Math.floor(remainingWork / Math.max(1, this.pool.length + 1)));
      const enumerateRequest = {
        schema: "spc-root-session-enumerate-v1",
        request_id: requestBase.request_id,
        iteration_id: requestBase.iteration_id,
        generation: requestBase.depth,
        source_fingerprint: requestBase.source_fingerprint,
        kernel_sha256: requestBase.kernel_sha256,
        module_js_sha256: requestBase.module_js_sha256,
        certificate_id: requestBase.certificate_id,
        runtime_variant: requestBase.runtime_variant,
        thread_count: requestBase.thread_count,
        engine_version: requestBase.engine_version,
        ruleset_version: requestBase.ruleset_version,
        profile_id: requestBase.profile_id,
        session_id: primary.sessionId,
        preferred_series: preferredSeries,
        external_work: safetyWork
          + this.pool.reduce((sum, channel) => sum + channel.nativeWorkAfter, 0)
          - primary.nativeWorkAfter,
        native_work_before: primary.nativeWorkAfter,
        call_work_credit: perCallCredit,
        deadline_monotonic_ms: requestBase.deadline_monotonic_ms,
        deadline_epoch_ms: deadlineEpochMs,
        remaining_time_ms: Math.max(0, Math.floor(
          requestBase.deadline_monotonic_ms - monotonicNow(),
        )),
      };
      const rawEnumeration = validateCallReceipt(await primary.call(
        "root-enumerate",
        enumerateRequest,
        { signal, deadlineMs: requestBase.deadline_monotonic_ms },
      ), primary, perCallCredit);
      const certifiedPromotionMateDeferral = (
        rawEnumeration.schema === "spc-root-session-enumeration-result-v1"
        && rawEnumeration.abi_version === 2
        && rawEnumeration.status === "unsupported"
        && rawEnumeration.status_code === 4
        && rawEnumeration.message === "native root promotion-mate lane is not implemented"
        && rawEnumeration.imported === false
        && rawEnumeration.request_id === enumerateRequest.request_id
        && rawEnumeration.iteration_id === enumerateRequest.iteration_id
        && rawEnumeration.generation === enumerateRequest.generation
        && Object.entries(requestBase).every(([key, value]) => (
          !ROOT_IDENTITY_KEYS.includes(key) || rawEnumeration[key] === value
        ))
        && rawEnumeration.deadline_monotonic_ms === enumerateRequest.deadline_monotonic_ms
        && exactInteger(rawEnumeration.remaining_time_ms, 0, enumerateRequest.remaining_time_ms)
        && rawEnumeration.product_publishable === false
        && rawEnumeration.safety_certified === false
        && rawEnumeration.canonical_root_tactical_policy === ROOT_TACTICAL_POLICY
        && rawEnumeration.canonical_root_tactical_protection
          === primary.canonicalRootTacticalProtection
        && rawEnumeration.retained_count === 0
        && rawEnumeration.width_complete === false
        && sameArray(rawEnumeration.preferred_series, preferredSeries)
        && Array.isArray(rawEnumeration.candidates)
        && rawEnumeration.candidates.length === 0
      );
      if (certifiedPromotionMateDeferral) {
        throw new RootIterationClientError(
          "The native promotion frontier requires the certified root-mate lane.",
          "browser-root-promotion-mate-deferred",
        );
      }
      if (
        rawEnumeration.schema !== "spc-root-session-enumeration-result-v1"
        || rawEnumeration.abi_version !== 2
        || rawEnumeration.status !== "complete"
        || rawEnumeration.imported !== false
        || rawEnumeration.request_id !== enumerateRequest.request_id
        || rawEnumeration.iteration_id !== enumerateRequest.iteration_id
        || rawEnumeration.generation !== enumerateRequest.generation
        || Object.entries(requestBase).some(([key, value]) => (
          ROOT_IDENTITY_KEYS.includes(key)
          && rawEnumeration[key] !== value
        ))
        || rawEnumeration.deadline_monotonic_ms !== enumerateRequest.deadline_monotonic_ms
        || !exactInteger(rawEnumeration.remaining_time_ms, 0, enumerateRequest.remaining_time_ms)
        || rawEnumeration.product_publishable !== false
        || rawEnumeration.safety_certified !== false
        || rawEnumeration.canonical_root_tactical_policy !== ROOT_TACTICAL_POLICY
        || rawEnumeration.canonical_root_tactical_protection
          !== primary.canonicalRootTacticalProtection
      ) {
        throw new RootIterationClientError(
          "The authoritative root enumeration returned an invalid routing envelope.",
          "browser-root-enumeration-invalid",
        );
      }
      const manifest = {
        enumeration_identity: rawEnumeration.enumeration_identity,
        root_white_to_move: rawEnumeration.root_white_to_move,
        requested_width: rawEnumeration.requested_width,
        retained_count: rawEnumeration.retained_count,
        width_complete: rawEnumeration.width_complete,
        preferred_series: rawEnumeration.preferred_series,
        candidates: rawEnumeration.candidates,
      };
      ROOT_API.normalizeManifest(manifest, ROOT_API.normalizeRequest({
        ...requestBase,
        depth: requestBase.depth,
        width,
        worker_count: this.pool.length,
        initial_full_wave: this.geometry.initial_full_wave,
        dynamic_work_pool: true,
        call_work_credit_supported: true,
        caps: {
          max_work: requestBase.max_work,
          initial_work: 0,
          safety_reserve_work: 0,
          search_call_work_credit: 1,
          safety_call_work_credit: 0,
          max_memory_bytes: this.geometry.aggregate_maximum_bytes,
        },
      }));
      const importResults = await Promise.all(this.pool.slice(1).map(async (channel) => {
        const credit = perCallCredit;
        const request = {
          schema: "spc-root-session-import-v1",
          request_id: requestBase.request_id,
          iteration_id: requestBase.iteration_id,
          generation: requestBase.depth,
          source_fingerprint: requestBase.source_fingerprint,
          kernel_sha256: requestBase.kernel_sha256,
          module_js_sha256: requestBase.module_js_sha256,
          certificate_id: requestBase.certificate_id,
          runtime_variant: requestBase.runtime_variant,
          thread_count: requestBase.thread_count,
          engine_version: requestBase.engine_version,
          ruleset_version: requestBase.ruleset_version,
          profile_id: requestBase.profile_id,
          session_id: channel.sessionId,
          manifest,
          external_work: safetyWork
            + this.pool.reduce((sum, item) => sum + item.nativeWorkAfter, 0)
            - channel.nativeWorkAfter,
          native_work_before: channel.nativeWorkAfter,
          call_work_credit: credit,
          deadline_monotonic_ms: requestBase.deadline_monotonic_ms,
          deadline_epoch_ms: deadlineEpochMs,
          remaining_time_ms: Math.max(0, Math.floor(
            requestBase.deadline_monotonic_ms - monotonicNow(),
          )),
        };
        const reply = validateCallReceipt(await channel.call(
          "root-import",
          request,
          { signal, deadlineMs: requestBase.deadline_monotonic_ms },
        ), channel, credit);
        const importedManifest = {
          enumeration_identity: reply.enumeration_identity,
          root_white_to_move: reply.root_white_to_move,
          requested_width: reply.requested_width,
          retained_count: reply.retained_count,
          width_complete: reply.width_complete,
          preferred_series: reply.preferred_series,
          candidates: reply.candidates,
        };
        if (
          reply.schema !== "spc-root-session-import-result-v1"
          || reply.abi_version !== 2
          || reply.status !== "complete"
          || reply.imported !== true
          || reply.request_id !== request.request_id
          || reply.iteration_id !== request.iteration_id
          || reply.generation !== request.generation
          || ROOT_IDENTITY_KEYS.some((key) => reply[key] !== request[key])
          || reply.deadline_monotonic_ms !== request.deadline_monotonic_ms
          || !exactInteger(reply.remaining_time_ms, 0, request.remaining_time_ms)
          || reply.product_publishable !== false
          || reply.safety_certified !== false
          || reply.canonical_root_tactical_policy !== ROOT_TACTICAL_POLICY
          || reply.canonical_root_tactical_protection
            !== channel.canonicalRootTacticalProtection
          || reply.canonical_root_tactical_protection
            !== rawEnumeration.canonical_root_tactical_protection
          || reply.enumeration_identity !== manifest.enumeration_identity
          || reply.retained_count !== manifest.candidates.length
          || !sameJson(importedManifest, manifest)
          || !sameArray(
            reply.candidates?.map((candidate) => candidate.candidate_identity),
            manifest.candidates.map((candidate) => candidate.candidate_identity),
          )
        ) {
          throw new RootIterationClientError(
            "A peer root session did not import the authoritative manifest exactly.",
            "browser-root-import-mismatch",
          );
        }
        return reply;
      }));
      return { manifest, enumeration: rawEnumeration, imports: importResults };
    }

    async _probeRootTerminalMate({
      requestBase,
      originalBoundary,
      identity,
      expected,
      callWorkCredit,
      deadlineEpochMs,
      receiptDeadlineMs,
      signal,
    }) {
      if (!exactInteger(callWorkCredit, 1, 0xffffffff)) {
        throw new RootIterationClientError(
          "The root terminal-mate rescue has no bounded work credit.",
          "browser-root-terminal-mate-work-invalid",
        );
      }
      const channel = this.pool[0];
      const request = {
        schema: "spc-root-terminal-mate-task-v1",
        request_id: requestBase.request_id,
        iteration_id: requestBase.iteration_id,
        source_fingerprint: expected.source_fingerprint,
        kernel_sha256: expected.kernel_sha256,
        module_js_sha256: expected.module_js_sha256,
        certificate_id: expected.certificate_id,
        mate_certificate_id: identity.mate_certificate_id,
        runtime_variant: expected.runtime_variant,
        thread_count: expected.thread_count,
        engine_version: expected.engine_version,
        ruleset_version: expected.ruleset_version,
        profile_id: expected.profile_id,
        session_id: channel.sessionId,
        boundary: originalBoundary,
        call_work_credit: callWorkCredit,
        deadline_monotonic_ms: requestBase.deadline_monotonic_ms,
        deadline_epoch_ms: deadlineEpochMs,
        remaining_time_ms: Math.max(0, Math.floor(
          requestBase.deadline_monotonic_ms - monotonicNow(),
        )),
      };
      const reply = await channel.call("root-terminal-mate", request, {
        signal,
        deadlineMs: receiptDeadlineMs,
      });
      if (
        !reply
        || typeof reply !== "object"
        || Array.isArray(reply)
        || reply.schema !== request.schema
        || reply.request_id !== request.request_id
        || reply.iteration_id !== request.iteration_id
        || reply.session_id !== request.session_id
        || reply.mate_certificate_id !== request.mate_certificate_id
        || Object.keys(expected).some((key) => reply[key] !== expected[key])
        || !sameBoundary(reply.boundary, originalBoundary)
        || reply.call_work_credit !== request.call_work_credit
        || reply.deadline_monotonic_ms !== request.deadline_monotonic_ms
        || reply.deadline_epoch_ms !== request.deadline_epoch_ms
        || !exactInteger(reply.remaining_time_ms, 0, request.remaining_time_ms)
        || !["found", "exhausted", "unknown"].includes(reply.status)
        || !exactInteger(reply.work_used, 0, callWorkCredit)
        || !exactInteger(
          reply.memory_bytes,
          1,
          identity.memory_limits.maximum_bytes,
        )
        || !exactInteger(
          reply.memory_peak_bytes,
          reply.memory_bytes,
          identity.memory_limits.maximum_bytes,
        )
      ) {
        throw new RootIterationClientError(
          "The root terminal-mate rescue returned a malformed or stale receipt.",
          "browser-root-terminal-mate-invalid",
        );
      }
      recordChannelMemory(reply, channel);
      if (reply.status !== "found") {
        return Object.freeze({ reply, rootSeries: null, checkedPrefix: null });
      }
      const rootSeries = normalizeRootSeries(reply.root_series);
      const checkedPrefix = reply.checked_prefix;
      const replayRequest = PREFIX_API.normalizePrefixRequest({
        ...originalBoundary,
        prefix: [...rootSeries.moves],
      }, `${requestBase.iteration_id}:terminal-mate-replay`, identity.prefix_contract);
      PREFIX_API.validatePrefixResult(checkedPrefix, replayRequest, identity);
      const rootWhite = originalBoundary.series % 2 === 1;
      const expectedScore = rootWhite ? MATE_SCORE - 1 : -MATE_SCORE + 1;
      const expectedProof = rootWhite ? [1, 1] : [-1, -1];
      if (
        rootSeries.outcome !== "checkmate"
        || rootSeries.ended_by_check !== true
        || checkedPrefix.complete !== true
        || checkedPrefix.outcome !== "checkmate"
        || checkedPrefix.ended_by_check !== true
        || !sameArray(checkedPrefix.prefix, rootSeries.moves)
        || !sameExactBoundary(checkedPrefix.next_state, rootSeries.child_boundary)
        || reply.score !== expectedScore
        || !sameJson(reply.proof_bounds, expectedProof)
      ) {
        throw new RootIterationClientError(
          "The root terminal-mate rescue failed authoritative replay or score mapping.",
          "browser-root-terminal-mate-invalid",
        );
      }
      return Object.freeze({ reply, rootSeries, checkedPrefix });
    }

    _terminalMateResult({
      payload,
      identity,
      expected,
      geometry,
      originalBoundary,
      depth,
      hostStarted,
      safetyReserve,
      rootTaskCount,
      trigger,
      rescue,
      safetyWork,
      mateCacheHits,
      mateCacheMisses,
    }) {
      if (
        rescue?.reply?.status !== "found"
        || !exactInteger(safetyReserve, 1)
        || !exactInteger(rootTaskCount, 0)
        || typeof trigger !== "string"
        || !trigger
      ) {
        throw new RootIterationClientError(
          "The terminal-mate result has no certified publication envelope.",
          "browser-root-terminal-mate-invalid",
        );
      }
      const memoryBytes = Math.max(
        ...this.pool.map((channel) => channel.memoryPeakBytes),
      );
      const aggregateMemory = this.pool.reduce(
        (sum, channel) => sum + channel.memoryPeakBytes,
        0,
      );
      const totalWork = this.pool.reduce(
        (sum, channel) => sum + channel.nativeWorkAfter,
        safetyWork,
      );
      if (!exactInteger(totalWork, 0, payload.max_generation_positions)) {
        throw new RootIterationClientError(
          "The terminal-mate result exceeded the shared work ledger.",
          "browser-root-terminal-mate-work-invalid",
        );
      }
      const rootSeries = rescue.rootSeries;
      const checkedPrefix = rescue.checkedPrefix;
      const mateCache = Object.freeze({
        schema: "spc-root-mate-proof-cache-summary-v1",
        hits: mateCacheHits,
        misses: mateCacheMisses,
        entries: this.mateProofCache.size,
        complete_proofs_only: true,
      });
      return Object.freeze({
        ok: true,
        status: "complete",
        publishable: true,
        safety_certified: true,
        legal_series_certified: true,
        authoritative_replay_certified: true,
        legal_validation_runtime: "compiled-wasm",
        checked_prefix: checkedPrefix,
        source_fingerprint: expected.source_fingerprint,
        wasm_sha256: identity.wasm_sha256,
        kernel_sha256: expected.kernel_sha256,
        module_js_sha256: expected.module_js_sha256,
        certificate_id: expected.certificate_id,
        mate_certificate_id: identity.mate_certificate_id,
        prefix_certificate_id: identity.prefix_certificate_id,
        runtime_variant: "single",
        thread_count: 1,
        requested_depth: payload.depth,
        completed_depth: depth,
        best_full_series: [...rootSeries.moves],
        principal_variation: [rootSeries],
        score: rescue.reply.score,
        proof_bounds: [...rescue.reply.proof_bounds],
        proof: originalBoundary.series % 2 === 1 ? "white" : "black",
        mate_score: MATE_SCORE,
        root_search_mode: "streaming-root-iteration",
        root_scores_complete: false,
        root_bound_coverage_complete: true,
        exact_width: false,
        timed_out: false,
        work_limit_reached: false,
        work: totalWork,
        memory_bytes: memoryBytes,
        aggregate_memory_bytes: aggregateMemory,
        stats: {
          generation_positions: totalWork,
          root_tasks: rootTaskCount,
          root_workers: this.pool.length,
          initial_full_wave: geometry.initial_full_wave,
          coverage_complete: true,
          safety_status: "terminal-mate-rescue",
          terminal_mate_rescues: 1,
          mate_cache_hits: mateCacheHits,
          mate_cache_misses: mateCacheMisses,
          mate_cache_entries: this.mateProofCache.size,
          safety_reserve_positions: safetyReserve,
        },
        runtime_receipt: {
          runtime: "browser-wasm",
          search_mode: "streaming-root-iteration",
          requested_depth: payload.depth,
          completed_depth: depth,
          wall_time_seconds: Math.max(0, (monotonicNow() - hostStarted) / 1_000),
          work: totalWork,
          source_fingerprint: expected.source_fingerprint,
          artifact_fingerprint: identity.wasm_sha256,
          kernel_fingerprint: expected.kernel_sha256,
          module_fingerprint: expected.module_js_sha256,
          certificate_id: expected.certificate_id,
          mate_certificate_id: identity.mate_certificate_id,
          runtime_variant: "single",
          thread_count: 1,
          worker_count: this.pool.length,
          initial_full_wave: geometry.initial_full_wave,
          certified_memory: { ...identity.memory_limits },
          aggregate_memory_cap_bytes: geometry.aggregate_maximum_bytes,
          aggregate_memory_peak_bytes: aggregateMemory,
          safety_reserve_positions: safetyReserve,
          canonical_replay_certified: true,
          mate_safety_certified: true,
          root_bound_coverage_complete: true,
          terminal_mate_rescue: {
            trigger,
            status: "found",
            work_used: rescue.reply.work_used,
          },
          mate_cache: mateCache,
        },
      });
    }

    async analyze(payload, identity, {
      signal,
      deadlineMs,
      receiptDeadlineMs = deadlineMs,
    } = {}) {
      if (signal?.aborted) throw abortError();
      if (!canRunRequest(payload, identity)) {
        throw new RootIterationClientError(
          "The certified iterative browser root lane is unavailable for this request.",
          "browser-root-unavailable",
        );
      }
      if (this.active) {
        throw new RootIterationClientError(
          "The browser root lane is already searching.",
          "browser-root-busy",
        );
      }
      const absoluteDeadline = Number.isFinite(deadlineMs)
        ? deadlineMs
        : monotonicNow() + Number(payload.time_limit) * 1_000;
      const absoluteReceiptDeadline = Number.isFinite(receiptDeadlineMs)
        ? Math.max(absoluteDeadline, receiptDeadlineMs)
        : absoluteDeadline;
      const deadlineEpochMs = monotonicDeadlineEpoch(absoluteDeadline);
      if (absoluteDeadline <= monotonicNow()) {
        throw new RootIterationClientError(
          "The browser root deadline already expired.",
          "browser-root-deadline",
        );
      }
      this.active = true;
      this.lastSafe = null;
      const hostStarted = monotonicNow();
      const requestId = `root-browser-${this.nextRequestId++}`;
      let safetyWork = 0;
      let mateCacheHits = 0;
      let mateCacheMisses = 0;
      let preferredSeries = [];
      let previousScore = null;
      let previousOwners = new Map();
      let lastFailure = null;
      try {
        const { expected, geometry } = await this._ensurePool(
          identity,
          absoluteDeadline,
          signal,
        );
        const originalBoundary = canonicalBoundary(payload);
        await this._resetSessions({
          identity,
          expected,
          boundary: originalBoundary,
          requestId,
          deadlineMs: absoluteDeadline,
          signal,
        });
        for (let depth = 1; depth <= payload.depth; depth += 1) {
          if (signal?.aborted) throw abortError();
          if (monotonicNow() >= absoluteDeadline) {
            lastFailure = new RootIterationClientError(
              "The browser root deadline expired while deepening.",
              "browser-root-deadline",
            );
            break;
          }
          const iterationId = `${requestId}:d${depth}`;
          const requestBase = {
            schema: ROOT_API.REQUEST_SCHEMA,
            request_id: requestId,
            iteration_id: iterationId,
            ...expected,
            boundary: originalBoundary,
            required_prefix: [],
            depth,
            mate_score: MATE_SCORE,
            aspiration: previousScore === null ? null : {
              center_score: previousScore,
              initial_delta: ASPIRATION_INITIAL_DELTA,
            },
            deadline_monotonic_ms: absoluteDeadline,
            max_work: payload.max_generation_positions,
          };
          try {
            const nativeBeforeSetup = this.pool.reduce(
              (sum, channel) => sum + channel.nativeWorkAfter,
              0,
            );
            const remainingWork = payload.max_generation_positions
              - nativeBeforeSetup - safetyWork;
            if (remainingWork <= 0) {
              throw new RootIterationClientError(
                "The browser root request exhausted its global work cap.",
                "browser-root-work-limit",
              );
            }
            const { manifest } = await this._enumerateAndImport({
              requestBase,
              preferredSeries,
              width: payload.max_series,
              remainingWork,
              safetyWork,
              deadlineEpochMs,
              signal,
            });
            const initialWork = this.pool.reduce(
              (sum, channel) => sum + channel.nativeWorkAfter,
              safetyWork,
            );
            if (initialWork >= payload.max_generation_positions) {
              throw new RootIterationClientError(
                "Root enumeration consumed the remaining global work cap.",
                "browser-root-work-limit",
              );
            }
            const remaining = payload.max_generation_positions - initialWork;
            const playLimits = normalizedRootPlayLimits(identity);
            if (!playLimits) {
              throw new RootIterationClientError(
                "The root work reservation is not certificate-bound.",
                "browser-root-play-limits-invalid",
              );
            }
            if (remaining <= 1) {
              throw new RootIterationClientError(
                "The root request has no work left beside its certified safety reserve.",
                "browser-root-work-limit",
              );
            }
            const safetyReserve = Math.min(
              playLimits.safety_reserve_positions,
              remaining - 1,
            );
            const searchCredit = Math.max(
              1,
              Math.floor(Math.max(1, remaining - safetyReserve) / this.pool.length),
            );
            const coordinatorRequest = {
              ...requestBase,
              width: payload.max_series,
              worker_count: this.pool.length,
              initial_full_wave: geometry.initial_full_wave,
              dynamic_work_pool: true,
              call_work_credit_supported: true,
              caps: {
                max_work: payload.max_generation_positions,
                initial_work: initialWork,
                safety_reserve_work: safetyReserve,
                search_call_work_credit: searchCredit,
                safety_call_work_credit: safetyReserve,
                max_memory_bytes: geometry.aggregate_maximum_bytes,
              },
            };
            const adapters = this.pool.map((channel) => ({
              id: channel.id,
              call_work_credit_supported: true,
              hard_memory_limit_supported: true,
              identity: expected,
              memory_limit_bytes: identity.memory_limits.estimated_peak_bytes,
              native_work_after: channel.nativeWorkAfter,
              search: async (task, { signal: taskSignal } = {}) => {
                const reply = await channel.call("root-search", {
                  ...task,
                  session_id: channel.sessionId,
                  deadline_epoch_ms: deadlineEpochMs,
                  remaining_time_ms: Math.max(0, Math.floor(
                    task.deadline_monotonic_ms - monotonicNow(),
                  )),
                }, { signal: taskSignal, deadlineMs: absoluteReceiptDeadline });
                if (reply?.work && exactInteger(reply.work.native_work_after, 0)) {
                  channel.nativeWorkAfter = reply.work.native_work_after;
                }
                recordChannelMemory(reply, channel);
                return reply;
              },
              cancel: () => {},
            }));
            const aspirationAffinity = scheduleAspirationAffinity({
              adapters,
              manifest,
              initialFullWave: geometry.initial_full_wave,
              aspiration: requestBase.aspiration,
              previousOwners,
            });
            const scheduledAdapters = aspirationAffinity.adapters;
            const safetyProbe = async (task, { signal: taskSignal } = {}) => {
              const ownerId = task.candidate?.owner_worker_id;
              const channel = this.pool.find((item) => item.id === ownerId) || this.pool[0];
              const rootSeries = normalizeRootSeries(task.candidate?.root_series);
              const replayRequest = PREFIX_API.normalizePrefixRequest({
                ...originalBoundary,
                prefix: [...rootSeries.moves],
              }, `${task.iteration_id}:${task.safety_revision}:safety-replay`, identity.prefix_contract);
              const replay = await channel.call("prefix", replayRequest, {
                signal: taskSignal,
                deadlineMs: absoluteReceiptDeadline,
              });
              PREFIX_API.validatePrefixResult(replay, replayRequest, identity);
              const authoritativeChild = normalizeExactBoundaryState(replay.next_state || {});
              if (
                !sameArray(replay.prefix, rootSeries.moves)
                || replay.complete !== true
                || replay.outcome !== null
                || authoritativeChild === null
                || !sameExactBoundary(authoritativeChild, rootSeries.child_boundary)
              ) {
                throw new RootIterationClientError(
                  "The compiled root replay disagreed with the manifest child boundary.",
                  "browser-root-safety-replay-invalid",
                );
              }
              const cacheKey = mateProofCacheKey(identity, authoritativeChild);
              let cached = this.mateProofCache.get(cacheKey) || null;
              let safety;
              if (cached) {
                this.mateProofCache.delete(cacheKey);
                this.mateProofCache.set(cacheKey, cached);
                mateCacheHits += 1;
                const memory = {
                  memory_bytes: channel.memoryBytes,
                  memory_peak_bytes: channel.memoryPeakBytes,
                };
                if (cached.status === "found") {
                  const mateReplayRequest = PREFIX_API.normalizePrefixRequest({
                    ...authoritativeChild,
                    prefix: [...cached.moves],
                  }, `${task.iteration_id}:${task.safety_revision}:mate-replay`, identity.prefix_contract);
                  const checkedMate = await channel.call("prefix", mateReplayRequest, {
                    signal: taskSignal,
                    deadlineMs: absoluteReceiptDeadline,
                  });
                  PREFIX_API.validatePrefixResult(checkedMate, mateReplayRequest, identity);
                  recordChannelMemory(checkedMate, channel);
                  safety = {
                    ...task,
                    status: "found",
                    work_used: 0,
                    override_score: cached.override_score,
                    proof_bounds: [...cached.proof_bounds],
                    memory_bytes: channel.memoryBytes,
                    memory_peak_bytes: channel.memoryPeakBytes,
                    mate_cache: {
                      schema: "spc-root-mate-proof-cache-receipt-v1",
                      hit: true,
                      proof_status: "found",
                    },
                    reply_mate: {
                      moves: [...cached.moves],
                      machine_notation: cached.moves.join("/"),
                      outcome: "checkmate",
                      ended_by_check: true,
                      checked_prefix: checkedMate,
                    },
                  };
                } else {
                  safety = {
                    ...task,
                    status: "exhausted",
                    work_used: 0,
                    ...memory,
                    mate_cache: {
                      schema: "spc-root-mate-proof-cache-receipt-v1",
                      hit: true,
                      proof_status: "exhausted",
                    },
                  };
                }
              } else {
                mateCacheMisses += 1;
                safety = await channel.call("root-safety", {
                  ...task,
                  session_id: channel.sessionId,
                  deadline_epoch_ms: deadlineEpochMs,
                  authoritative_child_boundary: authoritativeChild,
                  authoritative_root_replay: replay,
                  remaining_time_ms: Math.max(0, Math.floor(
                    task.deadline_monotonic_ms - monotonicNow(),
                  )),
                }, { signal: taskSignal, deadlineMs: absoluteReceiptDeadline });
                safety = {
                  ...safety,
                  mate_cache: {
                    schema: "spc-root-mate-proof-cache-receipt-v1",
                    hit: false,
                    proof_status: String(safety?.status || "unknown"),
                  },
                };
              }
              recordChannelMemory(safety, channel);
              if (safety?.status === "found") {
                const replyMoves = safety.reply_mate?.moves;
                const mateReplayRequest = PREFIX_API.normalizePrefixRequest({
                  ...authoritativeChild,
                  prefix: Array.isArray(replyMoves) ? [...replyMoves] : null,
                }, `${task.iteration_id}:${task.safety_revision}:mate-replay`, identity.prefix_contract);
                const checkedMate = safety.reply_mate?.checked_prefix;
                PREFIX_API.validatePrefixResult(checkedMate, mateReplayRequest, identity);
                const childIsWhite = authoritativeChild.side_to_move === "white";
                const expectedOverride = childIsWhite ? MATE_SCORE - 2 : -MATE_SCORE + 2;
                const expectedProof = childIsWhite ? [1, 1] : [-1, -1];
                if (
                  !sameArray(checkedMate.prefix, replyMoves)
                  || checkedMate.complete !== true
                  || checkedMate.outcome !== "checkmate"
                  || checkedMate.ended_by_check !== true
                  || safety.override_score !== expectedOverride
                  || !sameJson(safety.proof_bounds, expectedProof)
                  || safety.reply_mate.machine_notation !== replyMoves.join("/")
                  || safety.reply_mate.outcome !== "checkmate"
                  || safety.reply_mate.ended_by_check !== true
                ) {
                  throw new RootIterationClientError(
                    "The compiled reply-mate proof did not match Python root semantics.",
                    "browser-root-mate-proof-invalid",
                  );
                }
                if (!cached) {
                  cached = Object.freeze({
                    status: "found",
                    moves: Object.freeze([...replyMoves]),
                    override_score: safety.override_score,
                    proof_bounds: Object.freeze([...safety.proof_bounds]),
                  });
                }
              } else if (
                safety?.status === "exhausted"
                && (
                  safety.reply_mate !== undefined
                  || safety.override_score !== undefined
                  || safety.proof_bounds !== undefined
                )
              ) {
                throw new RootIterationClientError(
                  "An exhausted reply-mate proof carried an override.",
                  "browser-root-mate-proof-invalid",
                );
              }
              if (!cached && safety?.status === "exhausted") {
                cached = Object.freeze({ status: "exhausted" });
              }
              if (cached && safety?.mate_cache?.hit === false) {
                this.mateProofCache.set(cacheKey, cached);
                while (this.mateProofCache.size > this.mateProofCacheLimit) {
                  this.mateProofCache.delete(this.mateProofCache.keys().next().value);
                }
              }
              return safety;
            };
            let iteration;
            try {
              iteration = await ROOT_API.runRootIteration({
                request: coordinatorRequest,
                manifest,
                workers: scheduledAdapters,
                safetyProbe,
                signal,
                receiptDeadlineMs: absoluteReceiptDeadline,
              });
            } catch (error) {
              if (error?.code !== "root-safety-widening-required") throw error;
              const failedSafetyWork = error?.work?.safety_committed_work;
              const nativeWork = this.pool.reduce(
                (sum, channel) => sum + channel.nativeWorkAfter,
                0,
              );
              const expectedCommittedWork = nativeWork + safetyWork + failedSafetyWork;
              if (
                !exactInteger(failedSafetyWork, 0, payload.max_generation_positions)
                || error?.work?.max_work !== payload.max_generation_positions
                || error?.work?.committed_work !== expectedCommittedWork
                || error?.work?.reserved_work !== 0
                || error?.work?.within_cap !== true
              ) {
                throw new RootIterationClientError(
                  "The all-losing root frontier had no accountable safety receipt.",
                  "browser-root-terminal-mate-work-invalid",
                  { cause: error },
                );
              }
              safetyWork += failedSafetyWork;
              const rescueRemaining = payload.max_generation_positions
                - nativeWork - safetyWork;
              if (rescueRemaining <= 0) throw error;
              const rescue = await this._probeRootTerminalMate({
                requestBase,
                originalBoundary,
                identity,
                expected,
                callWorkCredit: Math.min(0xffffffff, rescueRemaining),
                deadlineEpochMs,
                receiptDeadlineMs: absoluteReceiptDeadline,
                signal,
              });
              safetyWork += rescue.reply.work_used;
              if (rescue.reply.status !== "found") throw error;
              return this._terminalMateResult({
                payload,
                identity,
                expected,
                geometry,
                originalBoundary,
                depth,
                hostStarted,
                safetyReserve,
                rootTaskCount: manifest.candidates.length,
                trigger: "all-retained-children-proven-mating",
                rescue,
                safetyWork,
                mateCacheHits,
                mateCacheMisses,
              });
            }
            if (
              iteration.status !== "complete"
              || iteration.coverage_complete !== true
              || iteration.safety_certified !== true
              || !["exhausted", "terminal"].includes(iteration.safety_status)
            ) {
              throw new RootIterationClientError(
                "The root coordinator did not complete coverage and mate safety.",
                "browser-root-iteration-uncertified",
              );
            }
            const aspiration = normalizeAspirationReceipt(
              iteration.aspiration,
              requestBase.aspiration,
              aspirationAffinity.candidateIds.length,
              { allowUnsearched: iteration.tasks.length === 0 },
            );
            if (aspiration === null) {
              throw new RootIterationClientError(
                "The root coordinator returned invalid aspiration telemetry.",
                "browser-root-aspiration-receipt-invalid",
              );
            }
            const usedAspirationAffinity = iteration.tasks.length === 0
              ? Object.freeze({
                ...aspirationAffinity,
                ownerIds: Object.freeze([]),
                warmOwnerReused: false,
              })
              : aspirationAffinity;
            validateAspirationAffinity(iteration.tasks, usedAspirationAffinity);
            const rootSeries = normalizeRootSeries(iteration.selected?.root_series);
            const prefixRequest = PREFIX_API.normalizePrefixRequest({
              ...requestBase.boundary,
              prefix: [...rootSeries.moves],
            }, `${iterationId}:replay`, identity.prefix_contract);
            const checkedPrefix = await this.pool[0].call("prefix", prefixRequest, {
              signal,
              deadlineMs: absoluteReceiptDeadline,
            });
            PREFIX_API.validatePrefixResult(checkedPrefix, prefixRequest, identity);
            if (exactInteger(checkedPrefix.memory_bytes, 1)) {
              this.pool[0].memoryBytes = checkedPrefix.memory_bytes;
              this.pool[0].memoryPeakBytes = Math.max(
                this.pool[0].memoryPeakBytes,
                checkedPrefix.memory_bytes,
              );
            }
            if (
              !sameArray(checkedPrefix.prefix, rootSeries.moves)
              || checkedPrefix.complete !== true
              || checkedPrefix.outcome !== rootSeries.outcome
              || checkedPrefix.ended_by_check !== rootSeries.ended_by_check
              || !sameExactBoundary(checkedPrefix.next_state, rootSeries.child_boundary)
            ) {
              throw new RootIterationClientError(
                "The compiled prefix replay did not certify the selected root series.",
                "browser-root-replay-invalid",
              );
            }
            safetyWork += iteration.work.safety_committed_work;
            preferredSeries = [...rootSeries.moves];
            previousScore = iteration.selected.score;
            previousOwners = exactCandidateOwnerMap(iteration.tasks);
            const aspirationOwnerIds = Object.freeze([
              ...usedAspirationAffinity.ownerIds,
            ]);
            const aspirationOwnerId = aspirationOwnerIds[0] ?? null;
            const aspirationOwnerCount = aspirationOwnerIds.length;
            this.lastSafe = Object.freeze({
              ok: true,
              status: "complete",
              publishable: true,
              safety_certified: true,
              legal_series_certified: true,
              authoritative_replay_certified: true,
              legal_validation_runtime: "compiled-wasm",
              checked_prefix: checkedPrefix,
              source_fingerprint: expected.source_fingerprint,
              wasm_sha256: identity.wasm_sha256,
              kernel_sha256: expected.kernel_sha256,
              module_js_sha256: expected.module_js_sha256,
              certificate_id: expected.certificate_id,
              mate_certificate_id: identity.mate_certificate_id,
              prefix_certificate_id: identity.prefix_certificate_id,
              runtime_variant: "single",
              thread_count: 1,
              requested_depth: payload.depth,
              completed_depth: depth,
              best_full_series: [...rootSeries.moves],
              principal_variation: [rootSeries, ...iteration.selected.child_pv],
              score: iteration.selected.score,
              proof_bounds: iteration.selected.proof_bounds,
              proof: iteration.selected.proof_bounds?.[0] === 1
                ? "white"
                : iteration.selected.proof_bounds?.[1] === -1 ? "black" : null,
              mate_score: MATE_SCORE,
              root_search_mode: "streaming-root-iteration",
              root_scores_complete: iteration.root_scores_complete,
              root_bound_coverage_complete: iteration.coverage_complete,
              exact_width: iteration.width_complete,
              timed_out: false,
              work_limit_reached: false,
              work: iteration.work.committed_work,
              memory_bytes: Math.max(...this.pool.map((channel) => channel.memoryPeakBytes)),
              aggregate_memory_bytes: iteration.memory.peak_observed_bytes,
              stats: {
                generation_positions: iteration.work.committed_work,
                root_tasks: iteration.tasks.length,
                root_workers: this.pool.length,
                initial_full_wave: geometry.initial_full_wave,
                coverage_complete: true,
                safety_status: iteration.safety_status,
                mate_cache_hits: mateCacheHits,
                mate_cache_misses: mateCacheMisses,
                mate_cache_entries: this.mateProofCache.size,
                safety_reserve_positions: safetyReserve,
                aspiration_attempts: aspiration.attempts,
                aspiration_fail_highs: aspiration.fail_highs,
                aspiration_fail_lows: aspiration.fail_lows,
                aspiration_exact_hits: aspiration.exact_hits,
                aspiration_full_window_fallbacks: aspiration.full_window_fallbacks,
                aspiration_candidate_count: aspiration.candidate_count,
                aspiration_owner_worker_id: aspirationOwnerId,
                aspiration_owner_worker_ids: aspirationOwnerIds,
                aspiration_owner_worker_count: aspirationOwnerCount,
                aspiration_owner_reused: usedAspirationAffinity.warmOwnerReused,
                aspiration_warm_owner_reused_count: aspirationOwnerCount,
              },
              runtime_receipt: {
                runtime: "browser-wasm",
                search_mode: "streaming-root-iteration",
                requested_depth: payload.depth,
                completed_depth: depth,
                wall_time_seconds: Math.max(0, (monotonicNow() - hostStarted) / 1_000),
                work: iteration.work.committed_work,
                source_fingerprint: expected.source_fingerprint,
                artifact_fingerprint: identity.wasm_sha256,
                kernel_fingerprint: expected.kernel_sha256,
                module_fingerprint: expected.module_js_sha256,
                certificate_id: expected.certificate_id,
                mate_certificate_id: identity.mate_certificate_id,
                runtime_variant: "single",
                thread_count: 1,
                worker_count: this.pool.length,
                initial_full_wave: geometry.initial_full_wave,
                certified_memory: { ...identity.memory_limits },
                aggregate_memory_cap_bytes: geometry.aggregate_maximum_bytes,
                aggregate_memory_peak_bytes: iteration.memory.peak_observed_bytes,
                safety_reserve_positions: safetyReserve,
                canonical_replay_certified: true,
                mate_safety_certified: true,
                root_bound_coverage_complete: true,
                aspiration: {
                  ...aspiration,
                  owner_worker_id: aspirationOwnerId,
                  owner_worker_ids: aspirationOwnerIds,
                  owner_worker_count: aspirationOwnerCount,
                  warm_owner_reused: usedAspirationAffinity.warmOwnerReused,
                  warm_owner_reused_count: aspirationOwnerCount,
                },
                mate_cache: {
                  schema: "spc-root-mate-proof-cache-summary-v1",
                  hits: mateCacheHits,
                  misses: mateCacheMisses,
                  entries: this.mateProofCache.size,
                  complete_proofs_only: true,
                },
              },
            });
          } catch (error) {
            if (error?.name === "AbortError" && signal?.aborted) throw abortError();
            if (error?.code === "browser-root-promotion-mate-deferred") {
              const nativeWork = this.pool.reduce(
                (sum, channel) => sum + channel.nativeWorkAfter,
                0,
              );
              const rescueRemaining = payload.max_generation_positions
                - nativeWork - safetyWork;
              const playLimits = normalizedRootPlayLimits(identity);
              if (rescueRemaining > 0 && playLimits) {
                const safetyReserve = Math.min(
                  playLimits.safety_reserve_positions,
                  rescueRemaining,
                );
                const rescue = await this._probeRootTerminalMate({
                  requestBase,
                  originalBoundary,
                  identity,
                  expected,
                  callWorkCredit: Math.min(0xffffffff, rescueRemaining),
                  deadlineEpochMs,
                  receiptDeadlineMs: absoluteReceiptDeadline,
                  signal,
                });
                safetyWork += rescue.reply.work_used;
                if (rescue.reply.status === "found") {
                  return this._terminalMateResult({
                    payload,
                    identity,
                    expected,
                    geometry,
                    originalBoundary,
                    depth,
                    hostStarted,
                    safetyReserve,
                    rootTaskCount: 0,
                    trigger: "native-promotion-frontier-deferred",
                    rescue,
                    safetyWork,
                    mateCacheHits,
                    mateCacheMisses,
                  });
                }
              }
            }
            lastFailure = error;
            break;
          }
        }
        if (signal?.aborted) throw abortError();
        if (this.lastSafe) {
          if (this.crashError) this._closePool(this.crashError);
          if (lastFailure) {
            const interrupted = [
              lastFailure.code,
              lastFailure.name,
            ].some((value) => /deadline|timeout/i.test(String(value || "")));
            const capped = /work|cap/i.test(String(lastFailure.code || ""));
            return Object.freeze({
              ...this.lastSafe,
              timed_out: interrupted,
              work_limit_reached: capped,
              runtime_receipt: Object.freeze({
                ...this.lastSafe.runtime_receipt,
                timed_out: interrupted,
                work_limit_reached: capped,
                interruption_code: String(lastFailure.code || "root-iteration-interrupted"),
              }),
            });
          }
          return this.lastSafe;
        }
        throw lastFailure || new RootIterationClientError(
          "No fully certified local depth completed.",
          "browser-root-no-safe-depth",
        );
      } catch (error) {
        if (error?.name === "AbortError" || this.crashError) {
          this._closePool(error);
        }
        throw error;
      } finally {
        this.active = false;
      }
    }

    close(reason = "browser root runner closed") {
      this._closePool(new RootIterationClientError(
        reason,
        "browser-root-runner-closed",
      ));
      this.active = false;
      this.lastSafe = null;
      this.mateProofCache.clear();
    }
  }

  const api = Object.freeze({
    RootIterationClientError,
    RootIterationRunner,
    RootWorkerChannel,
    canRunRequest,
    canonicalBoundary,
    exactCandidateOwnerMap,
    normalizeRootSeries,
    normalizeAspirationReceipt,
    mateProofCacheKey,
    normalizedRootPlayLimits,
    rootIdentity,
    sameBoundary,
    scheduleAspirationAffinity,
    selectCertifiedGeometry,
    validateAspirationAffinity,
  });
  globalThis.ScottishProgressiveBrowserRootIteration = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
