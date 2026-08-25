(() => {
  "use strict";

  const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
  const ZERO_PROMOTED_HEX = "0000000000000000";
  const FILES = "abcdefgh";
  const STUDY_STORAGE_KEY = "scottish-progressive-analysis-study-v1";
  const POSITION_STORAGE_KEY = "scottish-progressive-saved-positions-v1";
  const PLAY_SESSION_STORAGE_KEY = "scottish-progressive-play-session-v2";
  const LEGACY_PLAY_SESSION_STORAGE_KEY = "scottish-progressive-play-session-v1";
  const PLAY_SESSION_WRITE_LOCK = "scottish-progressive-play-session-v2-write";
  const STUDY_SCHEMA_VERSION = 1;
  const POSITION_SCHEMA_VERSION = 1;
  const PLAY_SESSION_SCHEMA_VERSION = 2;
  const LEGACY_PLAY_SESSION_SCHEMA_VERSION = 1;
  const MAX_STORED_NODES = 800;
  const MAX_SAVED_POSITIONS = 50;
  const MAX_STORED_PLAY_SESSION_BYTES = 1_000_000;
  const AUTO_ANALYSIS_DEBOUNCE_MS = 260;
  const AUTO_ANALYSIS_RETRY_MS = 700;
  const ENGINE_MOVE_ANIMATION_MS = 145;
  const PUBLIC_HEALTH_TIMEOUT_MS = 20_000;
  const PUBLIC_HEALTH_WAKE_DELAYS_MS = [1_500, 3_000, 5_000, 8_000, 12_000, 16_000, 20_000];
  const PUBLIC_ENGINE_RECONNECT_DELAYS_MS = [1_000, 2_500, 5_000];
  const LOCAL_ENGINE_FIRST_PROBE_TIMEOUT_MS = 12_000;
  const LOCAL_ENGINE_BOOTSTRAP_TIMEOUT_MS = 30_000;
  const TRANSIENT_LOCAL_ENGINE_FAILURES = new Set([
    "browser-worker-crashed",
    "browser-worker-post-failed",
    "browser-worker-timeout",
    "browser-worker-unavailable",
  ]);
  const EVALUATION = globalThis.ScottishProgressiveEvaluation;
  const PLAY_HANDOFF = globalThis.ScottishProgressivePlayHandoff.createGate();
  const PLAY_TIMELINE = globalThis.ScottishProgressivePlayTimeline;
  const PUBLIC_SITE_HOST = "tetizz.github.io";
  const PUBLIC_SITE_PATH = "/progressive";
  const configuredApiOrigin = document
    .querySelector('meta[name="spc-api-origin"]')
    ?.getAttribute("content")
    ?.trim()
    ?.replace(/\/$/, "") || "";
  const publicPath = globalThis.location.pathname.toLowerCase();
  const expectedPublicPath = PUBLIC_SITE_PATH.toLowerCase();
  const isPublicPagesSite = globalThis.location.hostname.toLowerCase() === PUBLIC_SITE_HOST
    && (publicPath === expectedPublicPath
      || publicPath.startsWith(`${expectedPublicPath}/`));
  const staticHostCanRunLocalEngine = globalThis.location.protocol === "https:"
    || ["localhost", "127.0.0.1", "::1"].includes(
      globalThis.location.hostname.toLowerCase(),
    );
  const API_ORIGIN = isPublicPagesSite ? configuredApiOrigin : "";
  const BROWSER_PREFIX = globalThis.ScottishProgressiveBrowserPrefix;
  const BROWSER_ENGINE_API = globalThis.ScottishProgressiveBrowserEngine;
  const browserEngineClient = staticHostCanRunLocalEngine && BROWSER_ENGINE_API
    ? BROWSER_ENGINE_API.createClient()
    : null;
  const EVALUATION_SCALE_HELP = "Scores are White-centric heuristic points, not pawns or Stockfish centipawns. Human labels are qualitative; the raw engine score remains visible separately.";
  const ANALYSIS_PRESETS = {
    quick: { depth: 4, cap: 48, seconds: 1.25, alternatives: 2, generationPositions: 150_000 },
    strong: { depth: 8, cap: 256, seconds: 5, alternatives: 3, generationPositions: 5_000_000 },
  };
  // Play searches are governed by their wall-clock deadlines. The numeric ceiling
  // exists only because the native/WASM contracts use a finite integer; it is
  // deliberately far beyond the work reachable during one play search.
  const PLAY_TECHNICAL_WORK_CEILING = 4_000_000_000;
  const PLAY_ANALYSIS_RESPONSE_GRACE_MS = 1_500;
  const PLAY_PREFIX_RECEIPT_TIMEOUT_MS = 10_000;
  const PLAY_RESTORE_PER_SERIES_TIMEOUT_MS = 1_500;
  const PLAY_RESTORE_MAX_TIMEOUT_MS = 120_000;
  const PLAY_STRENGTHS = {
    strong: { label: "Strong", minimumDepth: 5, seconds: 30, generationPositions: PLAY_TECHNICAL_WORK_CEILING },
    faster: { label: "Faster", minimumDepth: 1, seconds: 5, generationPositions: PLAY_TECHNICAL_WORK_CEILING },
  };
  const PIECE_NAMES = {
    p: "pawn", n: "knight", b: "bishop", r: "rook", q: "queen", k: "king",
  };

  const dom = Object.fromEntries([
    "board", "board-shell", "board-arrows", "board-loading", "board-loading-text", "drag-piece",
    "engine-status", "engine-status-text", "rules-version", "series-number",
    "turn-label", "moves-heading", "series-status", "move-chips", "boundary-pill",
    "boundary-notice", "boundary-notice-text", "eval-rail", "eval-fill", "eval-marker",
    "flip-board", "undo-move", "reset-series", "advance-series", "analyze-button",
    "preset-quick", "preset-strong", "study-save-state", "analysis-tree",
    "new-variation", "delete-variation", "clear-study", "tree-help",
    "depth-control", "cap-control", "time-control", "alternatives-control",
    "analysis-empty", "analysis-loading", "analysis-error", "analysis-error-text",
    "analysis-results", "result-score", "result-raw-score", "result-classification", "result-confidence",
    "proof-strip", "result-side", "best-series", "best-notation", "pv-line",
    "result-choice-heading",
    "pv-controls", "pv-previous", "pv-next", "pv-exit", "pv-indicator",
    "alternatives-count", "alternatives-list", "evaluation-breakdown", "reach-status",
    "warnings-section", "warnings-list", "search-stats", "theory-meta", "theory-loading",
    "theory-error", "opening-list", "refresh-openings", "setup-form", "fen-input",
    "series-input", "quiet-input", "ep-input", "load-start", "setup-error",
    "analysis-progress", "analysis-progress-fill", "analysis-progress-text",
    "save-position", "load-position", "saved-dialog", "saved-dialog-close",
    "save-position-form", "saved-name", "save-current-position",
    "saved-position-status", "saved-positions-list",
    "promotion-dialog", "promotion-options", "toast",
    "workspace", "mode-play", "mode-analyze", "play-panel",
    "play-top-player", "play-top-color", "play-top-name", "play-top-meta", "play-top-turn",
    "play-bottom-player", "play-bottom-color", "play-bottom-name", "play-bottom-meta", "play-bottom-turn",
    "play-as-white", "play-as-black", "play-live", "play-status-title", "play-status-detail",
    "play-series-title", "play-series-count", "play-series-copy", "play-history", "play-history-count",
    "play-history-previous", "play-history-next", "play-history-position",
    "play-new-game", "play-retry-engine", "play-analyze-position", "play-resign", "play-engine-name", "play-engine-id",
    "play-engine-version", "play-runtime-status", "play-strength-strong", "play-strength-faster", "play-strength-status",
    "play-search-depth", "play-search-status", "workspace-tabs", "analysis-panel", "theory-panel", "setup-panel",
  ].map((id) => [id.replaceAll("-", "_"), document.getElementById(id)]));

  const state = {
    boundary: {
      fen: START_FEN,
      series: 1,
      quiet_series: 0,
      ep_targets: [],
      promoted_hex: ZERO_PROMOTED_HEX,
      chess960: false,
    },
    prefix: [],
    prefixSan: [],
    boardFen: START_FEN,
    legalMoves: [],
    movesRemaining: 1,
    complete: false,
    nextState: null,
    outcome: null,
    check: false,
    unusedMoves: 0,
    completionReason: null,
    history: [],
    prefixFrames: [],
    pvFrames: [],
    previewIndex: null,
    selected: null,
    lastMove: null,
    flipped: false,
    focusSquare: "e2",
    drag: null,
    suppressClick: false,
    analysis: null,
    arrowSelection: null,
    prefixAbort: null,
    analysisAbort: null,
    pvAbort: null,
    prefixSequence: 0,
    analysisSequence: 0,
    analysisTimer: null,
    analysisPaused: false,
    analysisRunning: false,
    analysisPassDepth: 0,
    analysisCompletedDepth: 0,
    analysisRequestedDepth: 0,
    positionReady: false,
    positionBusy: false,
    maximumAnalysisDepth: 8,
    maximumAnalysisSeconds: 30,
    maximumBranchCap: 512,
    maximumAlternatives: 32,
    toastTimer: null,
    study: null,
    currentTreeNodeId: null,
    seriesParentNodeId: null,
    branching: false,
    viewingHistorical: false,
    handoffNotice: null,
    analysisPreset: "strong",
    maximumGenerationPositions: 5_000_000,
    savedPositions: [],
    mode: "play",
    analysisWorkspace: null,
    playWorkspace: null,
    play: {
      active: false,
      sessionId: null,
      humanColor: "white",
      thinking: false,
      animating: false,
      resigned: false,
      error: null,
      sequence: 0,
      engineAbort: null,
      engineName: "Current champion",
      engineProfileId: null,
      engineVersion: null,
      rulesetVersion: null,
      engineFingerprint: null,
      runtimeCpuCount: null,
      runtimeCpuCountSource: null,
      nativeThreads: 1,
      nativeThreadsPolicy: null,
      runtimeMode: "server",
      browserWasmReady: false,
      browserRootReady: false,
      browserRootWorkerCount: 0,
      browserPrefixReady: false,
      browserWasmReason: null,
      browserWasmArtifact: null,
      healthReady: false,
      recommendedDepth: 2,
      recommendedBranchCap: 32,
      timeLimitSeconds: 5,
      generationPositions: 500_000,
      lastEngineSeries: null,
      strength: "strong",
      activeSearch: null,
      activeSearchRuntime: null,
      lastSearch: null,
      timelineIndex: null,
    },
  };
  let playSessionReplayBlocked = false;
  let playSessionReplayPromise = null;
  let playSessionLastWriteDurable = false;
  let playSessionExternalUpdate = false;
  let playSessionSaveBlocked = false;
  let playSessionPendingWriteOptions = null;
  let playSessionRevision = 0;
  let playSessionWriteQueue = Promise.resolve();
  let playSessionWriteSequence = 0;
  let playPonderGeneration = 0;
  let activePlayPonder = null;
  let claimedPlayPonder = null;
  let playPonderCleanup = Promise.resolve();
  let activePlayEngineTurn = null;
  let activeNewPlayGame = null;
  const hostedFallbackAuthorities = new WeakMap();

  function randomStorageId(prefix) {
    const random = globalThis.crypto?.randomUUID?.()
      || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    return `${prefix}-${random}`;
  }

  // A page identity must not survive reload or tab duplication. The saved
  // session identity is separate and remains stable across either operation.
  const playSessionTabId = randomStorageId("page");

  function first(...values) {
    return values.find((value) => value !== undefined && value !== null);
  }

  function asNumber(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function asBoolean(value) {
    if (value === true || value === false) return value;
    if (value === "true") return true;
    if (value === "false") return false;
    return undefined;
  }

  function humanize(value) {
    return String(value ?? "")
      .replaceAll("_", " ")
      .replaceAll("-", " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function compactNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value ?? "—");
    return Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(number);
  }

  function describeEvaluation(value, evidence = {}) {
    return EVALUATION.describe(value, evidence);
  }

  function displayError(error) {
    if (error?.name === "AbortError") return "Request cancelled";
    return error?.message || String(error) || "Unknown error";
  }

  function monotonicNow() {
    return globalThis.performance?.now?.() ?? Date.now();
  }

  function analysisDeadlineError() {
    const error = new Error("The engine reached the analysis deadline.");
    error.name = "TimeoutError";
    error.code = "analysis-deadline";
    return error;
  }

  function currentPrefixAuthority() {
    const authority = {
      source_fingerprint: state.play.engineFingerprint,
      engine_version: state.play.engineVersion,
      ruleset_version: state.play.rulesetVersion,
    };
    return Object.values(authority).every((value) => (
      typeof value === "string" && Boolean(value)
    )) ? authority : null;
  }

  function hostedEngineIdentity(value) {
    return {
      source_fingerprint: first(value?.source_fingerprint, value?.fingerprint, null),
      engine_profile_id: first(value?.engine_profile_id, null),
      engine_version: first(value?.engine_version, value?.version, null),
      ruleset_version: first(
        value?.ruleset_version,
        value?.rules_version,
        value?.ruleset,
        null,
      ),
    };
  }

  function completeHostedEngineIdentity(value) {
    const identity = hostedEngineIdentity(value);
    const boundedToken = (item) => (
      typeof item === "string"
      && item.length >= 1
      && item.length <= 128
      && /^[A-Za-z0-9._:+/-]+$/.test(item)
    );
    return /^[0-9a-f]{16}$/.test(identity.source_fingerprint || "")
      && boundedToken(identity.engine_profile_id)
      && boundedToken(identity.engine_version)
      && boundedToken(identity.ruleset_version)
      ? identity
      : null;
  }

  function sameHostedEngineIdentity(left, right) {
    return Boolean(left && right)
      && Object.keys(left).every((key) => left[key] === right[key]);
  }

  function sameLoadedChampion(left, right) {
    return Boolean(left && right)
      && ["engine_profile_id", "engine_version", "ruleset_version"]
        .every((key) => left[key] === right[key]);
  }

  function hostedEngineIdentityError(message) {
    const error = new Error(message);
    error.code = "hosted-engine-identity-mismatch";
    error.fallbackRequired = false;
    return error;
  }

  function loadedChampionIdentity() {
    return completeHostedEngineIdentity({
      source_fingerprint: state.play.engineFingerprint,
      engine_profile_id: state.play.engineProfileId,
      engine_version: state.play.engineVersion,
      ruleset_version: state.play.rulesetVersion,
    });
  }

  function stagedHostedFallback(payload) {
    return payload && typeof payload === "object"
      ? hostedFallbackAuthorities.get(payload) || null
      : null;
  }

  function applyHostedFallbackRuntime(health, reason) {
    const identity = completeHostedEngineIdentity(health);
    if (!identity) {
      throw hostedEngineIdentityError(
        "The hosted fallback health response omitted its engine identity.",
      );
    }
    browserEngineClient?.close(`hosted fallback selected: ${reason}`);
    const profileName = first(health.engine_profile_name, health.profile_name);
    const limits = health.analysis_limits || {};
    state.play.engineName = String(profileName || "Current champion");
    state.play.engineProfileId = identity.engine_profile_id;
    state.play.engineVersion = identity.engine_version;
    state.play.engineFingerprint = identity.source_fingerprint;
    state.play.rulesetVersion = identity.ruleset_version;
    state.play.runtimeCpuCount = first(health.runtime?.cpu_count, null);
    state.play.runtimeCpuCountSource = first(health.runtime?.cpu_count_source, null);
    state.play.nativeThreads = Math.max(1, Math.floor(asNumber(
      first(health.runtime?.native_threads, limits.native_threads),
      1,
    )));
    state.play.nativeThreadsPolicy = first(
      health.runtime?.native_threads_policy,
      null,
    );
    state.play.browserWasmReady = false;
    state.play.browserRootReady = false;
    state.play.browserRootWorkerCount = 0;
    state.play.browserPrefixReady = false;
    state.play.browserWasmReason = String(reason || "browser-local-search-failed");
    state.play.browserWasmArtifact = null;
    state.play.runtimeMode = "server";
    state.maximumAnalysisDepth = Math.max(1, Math.floor(asNumber(
      limits.maximum_depth,
      state.maximumAnalysisDepth,
    )));
    state.maximumAnalysisSeconds = Math.max(0.1, asNumber(
      first(limits.maximum_seconds, limits.max_seconds),
      state.maximumAnalysisSeconds,
    ));
    state.maximumBranchCap = Math.max(1, Math.floor(asNumber(
      first(limits.maximum_max_series, limits.maximum_series, limits.max_series),
      state.maximumBranchCap,
    )));
    state.maximumGenerationPositions = Math.max(1_000, Math.floor(asNumber(
      limits.maximum_generation_positions,
      state.maximumGenerationPositions,
    )));
    state.maximumAlternatives = Math.max(0, Math.floor(asNumber(
      limits.maximum_alternatives,
      state.maximumAlternatives,
    )));
    state.play.recommendedDepth = Math.max(1, Math.floor(asNumber(
      health.engine_profile_recommended_depth,
      state.play.recommendedDepth,
    )));
    state.play.recommendedBranchCap = Math.max(1, Math.floor(asNumber(
      health.engine_profile_recommended_branch_cap,
      state.play.recommendedBranchCap,
    )));
    state.play.timeLimitSeconds = Math.max(0.1, Math.min(
      state.maximumAnalysisSeconds,
      asNumber(limits.default_seconds, state.play.timeLimitSeconds),
    ));
    state.play.generationPositions = Math.max(1_000, Math.min(
      state.maximumGenerationPositions,
      Math.floor(asNumber(
        limits.default_generation_positions,
        state.play.generationPositions,
      )),
    ));
    dom.depth_control.max = String(state.maximumAnalysisDepth);
    dom.cap_control.max = String(state.maximumBranchCap);
    dom.time_control.max = String(state.maximumAnalysisSeconds);
    dom.alternatives_control.max = String(Math.min(12, state.maximumAlternatives));
    dom.rules_version.textContent = identity.ruleset_version;
    state.play.healthReady = true;
    dom.engine_status.classList.add("is-online");
    dom.engine_status.classList.remove("is-offline");
    dom.engine_status_text.textContent = "Engine online";
    dom.engine_status.title = [
      profileName,
      identity.engine_version,
      identity.source_fingerprint,
      "hosted engine fallback",
      `local engine: ${state.play.browserWasmReason}`,
    ].filter(Boolean).join(" · ");
    renderPlaySurface();
  }

  async function validateHostedAnalysisIdentity(
    payload,
    { signal = null, deadlineMs = null, fallbackReason = null } = {},
  ) {
    const analysis = first(payload?.analysis, payload?.result, payload);
    const expected = loadedChampionIdentity();
    const received = completeHostedEngineIdentity(analysis);
    if (sameHostedEngineIdentity(expected, received)) return payload;
    const health = await requestRemoteJson("/api/health", {
      signal,
      analysisDeadlineMs: deadlineMs,
    });
    const currentHosted = completeHostedEngineIdentity(health);
    if (
      health?.ok !== true
      || !received
      || !currentHosted
      || !sameHostedEngineIdentity(received, currentHosted)
      || !sameLoadedChampion(expected, currentHosted)
    ) {
      throw hostedEngineIdentityError(
        "The hosted fallback did not match the loaded champion and current health identity.",
      );
    }
    if (signal?.aborted) {
      throw new DOMException("Request cancelled", "AbortError");
    }
    if (Number.isFinite(deadlineMs) && monotonicNow() > deadlineMs) {
      throw analysisDeadlineError();
    }
    if (!sameHostedEngineIdentity(expected, loadedChampionIdentity())) {
      throw hostedEngineIdentityError(
        "The loaded champion changed while the hosted fallback was being verified.",
      );
    }
    if (!payload || typeof payload !== "object") {
      throw hostedEngineIdentityError(
        "The hosted fallback response could not carry a staged identity authority.",
      );
    }
    hostedFallbackAuthorities.set(payload, Object.freeze({
      expected,
      identity: currentHosted,
      health,
      reason: fallbackReason || "browser-hosted-engine-identity-rebind",
    }));
    return payload;
  }

  async function requestRemoteJson(path, options = {}) {
    if (!path.startsWith("/api/")) throw new Error("API path must start with /api/");
    const {
      onTransport: _onTransport,
      analysisDeadlineMs,
      ...requestOptions
    } = options;
    const headers = { ...(requestOptions.headers || {}) };
    if (requestOptions.body !== undefined && !Object.keys(headers).some(
      (name) => name.toLowerCase() === "content-type",
    )) {
      headers["Content-Type"] = "application/json";
    }
    const parentSignal = requestOptions.signal;
    let deadlineController = null;
    let deadlineTimer = null;
    let deadlineReached = false;
    let onParentAbort = null;
    if (Number.isFinite(analysisDeadlineMs)) {
      const remainingMs = analysisDeadlineMs - monotonicNow();
      if (remainingMs <= 0) throw analysisDeadlineError();
      deadlineController = new AbortController();
      if (parentSignal?.aborted) throw new DOMException("Request cancelled", "AbortError");
      onParentAbort = () => deadlineController.abort(parentSignal?.reason);
      parentSignal?.addEventListener?.("abort", onParentAbort, { once: true });
      if (parentSignal?.aborted) onParentAbort();
      deadlineTimer = window.setTimeout(() => {
        deadlineReached = true;
        deadlineController.abort();
      }, remainingMs);
      requestOptions.signal = deadlineController.signal;
    }
    try {
      const response = await fetch(`${API_ORIGIN}${path}`, {
        ...requestOptions,
        headers,
      });
      const type = response.headers.get("content-type") || "";
      if (!type.includes("application/json")) {
        await response.text();
        const error = new Error(response.ok
          ? "The engine service returned a non-API response."
          : `${response.status} ${response.statusText}`);
        error.status = response.status;
        error.code = "invalid-api-response";
        throw error;
      }
      let payload;
      try {
        payload = await response.json();
      } catch (error) {
        if (deadlineReached) throw analysisDeadlineError();
        const invalid = new Error("The engine service returned invalid JSON.");
        invalid.status = response.status;
        invalid.code = "invalid-api-response";
        invalid.cause = error;
        throw invalid;
      }
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        const error = new Error("The engine service returned an invalid API payload.");
        error.status = response.status;
        error.code = "invalid-api-response";
        throw error;
      }
      if (!response.ok) {
        const detail = first(
          payload.detail,
          payload.error?.message,
          typeof payload.error === "string" ? payload.error : undefined,
          payload.message,
          `${response.status} ${response.statusText}`,
        );
        const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        error.status = response.status;
        error.code = first(payload.error?.code, payload.code, null);
        throw error;
      }
      return payload;
    } catch (error) {
      if (deadlineReached) throw analysisDeadlineError();
      throw error;
    } finally {
      if (deadlineTimer !== null) window.clearTimeout(deadlineTimer);
      if (onParentAbort) {
        parentSignal?.removeEventListener?.("abort", onParentAbort);
      }
    }
  }

  async function requestJson(path, options = {}) {
    if (!path.startsWith("/api/")) throw new Error("API path must start with /api/");
    if (
      path === "/api/prefix"
      && browserEngineClient
      && BROWSER_PREFIX
      && options.body !== undefined
    ) {
      const originalBody = typeof options.body === "string"
        ? JSON.parse(options.body)
        : options.body;
      return BROWSER_PREFIX.routePrefixRequest({
        payload: originalBody,
        signal: options.signal,
        localClient: browserEngineClient,
        remote: {
          identity: currentPrefixAuthority(),
          request: (body, { signal }) => requestRemoteJson(path, {
            ...options,
            signal,
            body: JSON.stringify(body),
          }),
        },
      });
    }
    let analysisBody = null;
    let analysisDeadlineMs = Number(options.analysisDeadlineMs);
    let analysisSearchDeadlineMs = Number(options.analysisSearchDeadlineMs);
    let localFallbackReason = null;
    if (path === "/api/analyze" && options.body !== undefined) {
      try {
        analysisBody = typeof options.body === "string"
          ? JSON.parse(options.body)
          : options.body;
      } catch {
        analysisBody = null;
      }
      if (!analysisBody || typeof analysisBody !== "object" || Array.isArray(analysisBody)) {
        analysisBody = null;
      }
      if (!Number.isFinite(analysisSearchDeadlineMs) && analysisBody) {
        const seconds = Math.max(0.01, asNumber(analysisBody.time_limit, 0.01));
        analysisSearchDeadlineMs = monotonicNow() + seconds * 1000;
      }
      if (!Number.isFinite(analysisDeadlineMs)) {
        analysisDeadlineMs = analysisSearchDeadlineMs;
      }
    }
    if (path === "/api/analyze" && browserEngineClient && options.body !== undefined) {
      if (browserEngineClient.canAnalyzeRoot(analysisBody)) {
        try {
          options.onTransport?.("browser-wasm");
          return await browserEngineClient.analyzeRoot(analysisBody, {
            signal: options.signal,
            searchDeadlineMs: analysisSearchDeadlineMs,
            receiptDeadlineMs: analysisDeadlineMs,
          });
        } catch (error) {
          if (
            error?.name === "AbortError"
            || error?.name === "TimeoutError"
            || error?.code === "browser-root-deadline"
          ) throw error;
          if (error?.fallbackRequired !== true) throw error;
          localFallbackReason = String(error?.code || "browser-root-analysis-failed");
          // A failed or uncertified depth may use the hosted lane only while
          // the same absolute deadline and loaded identity remain valid.
        }
      }
      if (browserEngineClient.canAnalyze(analysisBody)) {
        try {
          options.onTransport?.("browser-wasm");
          return await browserEngineClient.analyze(analysisBody, {
            signal: options.signal,
            searchDeadlineMs: analysisSearchDeadlineMs,
            receiptDeadlineMs: analysisDeadlineMs,
          });
        } catch (error) {
          if (
            error?.name === "AbortError"
            || error?.name === "TimeoutError"
            || error?.code === "browser-analysis-deadline"
          ) throw error;
          if (error?.fallbackRequired !== true) throw error;
          localFallbackReason = String(error?.code || "browser-analysis-failed");
          // The server remains the fail-closed path for an unsupported,
          // uncertified, stale, interrupted, or malformed local result.
        }
      }
    }
    const remoteOptions = {
      ...options,
      analysisDeadlineMs: Number.isFinite(analysisDeadlineMs)
        ? analysisDeadlineMs
        : options.analysisDeadlineMs,
    };
    if (
      path === "/api/analyze"
      && analysisBody
      && Number.isFinite(analysisSearchDeadlineMs)
    ) {
      const remainingSeconds = (analysisSearchDeadlineMs - monotonicNow()) / 1000;
      if (remainingSeconds < 0.01) throw analysisDeadlineError();
      remoteOptions.body = JSON.stringify({
        ...analysisBody,
        time_limit: Math.min(
          Math.max(0.01, asNumber(analysisBody.time_limit, remainingSeconds)),
          remainingSeconds,
        ),
      });
    }
    options.onTransport?.("render-server");
    const remotePayload = await requestRemoteJson(path, remoteOptions);
    return path === "/api/analyze"
      ? await validateHostedAnalysisIdentity(remotePayload, {
        signal: remoteOptions.signal,
        deadlineMs: analysisDeadlineMs,
        fallbackReason: localFallbackReason,
      })
      : remotePayload;
  }

  function playPrefixDeadlineError() {
    const error = new DOMException("Saved-game validation timed out.", "TimeoutError");
    error.code = "prefix-deadline";
    return error;
  }

  async function requestPlayPrefixJson(
    body,
    { signal = null, deadlineMs = null, analysisReceipt = false } = {},
  ) {
    const effectiveDeadlineMs = Number.isFinite(deadlineMs)
      ? deadlineMs
      : monotonicNow() + PLAY_PREFIX_RECEIPT_TIMEOUT_MS;
    const remainingMs = effectiveDeadlineMs - monotonicNow();
    if (remainingMs <= 0) {
      throw analysisReceipt ? analysisDeadlineError() : playPrefixDeadlineError();
    }
    const controller = new AbortController();
    let deadlineReached = false;
    const onParentAbort = () => controller.abort(signal?.reason);
    if (signal?.aborted) onParentAbort();
    else signal?.addEventListener?.("abort", onParentAbort, { once: true });
    const timer = window.setTimeout(() => {
      deadlineReached = true;
      controller.abort();
    }, remainingMs);
    try {
      return await requestJson("/api/prefix", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify(body),
      });
    } catch (error) {
      if (deadlineReached) {
        throw analysisReceipt ? analysisDeadlineError() : playPrefixDeadlineError();
      }
      throw error;
    } finally {
      window.clearTimeout(timer);
      signal?.removeEventListener?.("abort", onParentAbort);
    }
  }

  function isPublicServiceWakeError(error, { includeAbort = false } = {}) {
    if (!isPublicPagesSite) return false;
    return [502, 503, 504].includes(Number(error?.status))
      || error?.code === "invalid-api-response"
      || error?.name === "TypeError"
      || (includeAbort && ["AbortError", "TimeoutError"].includes(error?.name));
  }

  function waitForRetry(milliseconds, { signal = null, deadlineMs = null } = {}) {
    if (signal?.aborted) return Promise.reject(new DOMException("Request cancelled", "AbortError"));
    const remainingMs = Number.isFinite(deadlineMs)
      ? deadlineMs - monotonicNow()
      : null;
    if (remainingMs !== null && remainingMs <= 0) return Promise.reject(analysisDeadlineError());
    const waitMs = remainingMs === null ? milliseconds : Math.min(milliseconds, remainingMs);
    return new Promise((resolve, reject) => {
      let timer = null;
      const onAbort = () => {
        if (timer !== null) window.clearTimeout(timer);
        reject(new DOMException("Request cancelled", "AbortError"));
      };
      signal?.addEventListener?.("abort", onAbort, { once: true });
      if (signal?.aborted) {
        onAbort();
        return;
      }
      timer = window.setTimeout(() => {
        signal?.removeEventListener?.("abort", onAbort);
        if (remainingMs !== null && remainingMs <= milliseconds) reject(analysisDeadlineError());
        else resolve();
      }, waitMs);
    });
  }

  function boundaryPayload() {
    return {
      fen: state.boundary.fen,
      series: state.boundary.series,
      quiet_series: state.boundary.quiet_series,
      ep_targets: [...state.boundary.ep_targets],
      progressive_ep: [...state.boundary.ep_targets],
      promoted_hex: state.boundary.promoted_hex,
      chess960: state.boundary.chess960 === true,
    };
  }

  function parseFen(fen) {
    const text = String(fen || START_FEN).trim();
    const fields = text.split(/\s+/);
    const rows = (fields[0] || START_FEN.split(" ")[0]).split("/");
    const pieces = new Map();
    rows.slice(0, 8).forEach((row, rowIndex) => {
      let file = 0;
      for (const token of row) {
        if (/\d/.test(token)) {
          file += Number(token);
        } else if (file < 8) {
          const rank = 7 - rowIndex;
          pieces.set(`${FILES[file]}${rank + 1}`, {
            type: token.toLowerCase(),
            color: token === token.toUpperCase() ? "white" : "black",
          });
          file += 1;
        }
      }
    });
    return { pieces, turn: fields[1] === "b" ? "black" : "white" };
  }

  function pieceAsset(piece) {
    const prefix = piece.color === "white" ? "w" : "b";
    return `./pieces/cburnett/${prefix}${piece.type.toUpperCase()}.svg`;
  }

  function activeBoardFen() {
    if (state.previewIndex === null) return playReviewPosition()?.boardFen || state.boardFen;
    return state.pvFrames[state.previewIndex]?.fen || state.boardFen;
  }

  function normalizeMove(move) {
    if (typeof move === "string") {
      return {
        uci: move,
        from: move.slice(0, 2),
        to: move.slice(2, 4),
        promotion: move.slice(4, 5) || null,
        san: move,
      };
    }
    const uci = String(first(move.uci, move.move, ""));
    return {
      ...move,
      uci,
      from: first(move.from, uci.slice(0, 2)),
      to: first(move.to, uci.slice(2, 4)),
      promotion: first(move.promotion, uci.slice(4, 5), null),
      san: String(first(move.san, move.notation, uci)),
    };
  }

  function notationArray(payload, requestedPrefix, requestedSan) {
    const raw = first(payload.san, payload.notation, payload.prefix_san, payload.move_notation);
    if (Array.isArray(raw)) return raw.map((item) => String(first(item.san, item.notation, item)));
    if (typeof raw === "string" && raw.trim()) {
      return raw.split(/\s*\/\s*/).filter(Boolean);
    }
    return requestedPrefix.map((uci, index) => requestedSan[index] || uci);
  }

  function normalizeNextState(raw) {
    if (!raw || typeof raw !== "object") return null;
    const fen = first(raw.fen, raw.board_fen, raw.orthodox_fen);
    if (!fen) return null;
    const ep = first(raw.ep_targets, raw.progressive_ep, []);
    return {
      fen: String(fen),
      series: asNumber(first(raw.series, raw.series_number), state.boundary.series + 1),
      quiet_series: asNumber(first(raw.quiet_series, raw.quiet), 0),
      ep_targets: normalizeEpTargets(ep),
      promoted_hex: normalizePromotedHex(raw.promoted_hex),
      chess960: raw.chess960 === true,
    };
  }

  function normalizeEpTargets(value) {
    if (Array.isArray(value)) return value.map(String).filter(Boolean);
    if (typeof value === "string") {
      if (!value.trim() || value.trim() === "-") return [];
      return value.split(/[\s,]+/).filter(Boolean);
    }
    return [];
  }

  function normalizePromotedHex(value) {
    const text = String(value ?? "").trim().toLowerCase().replace(/^0x/, "");
    if (!/^[0-9a-f]{1,16}$/.test(text)) return null;
    return text.replace(/^0+(?=[0-9a-f])/, "").padStart(16, "0");
  }

  function cloneBoundary(boundary) {
    const fen = String(boundary?.fen || START_FEN);
    return {
      fen,
      series: Math.max(1, Math.floor(asNumber(boundary?.series, 1))),
      quiet_series: Math.max(0, Math.floor(asNumber(boundary?.quiet_series, 0))),
      ep_targets: normalizeEpTargets(boundary?.ep_targets).map((square) => square.toLowerCase()),
      promoted_hex: normalizePromotedHex(boundary?.promoted_hex)
        || (fen === START_FEN ? ZERO_PROMOTED_HEX : null),
      chess960: boundary?.chess960 === true,
    };
  }

  function clonePlain(value, fallback) {
    try {
      return JSON.parse(JSON.stringify(value));
    } catch {
      return fallback;
    }
  }

  function prefixFramesFromPayload(payload, prefix, prefixSan) {
    const frames = Array.isArray(payload?.frames) ? payload.frames : [];
    return prefix.map((uci, index) => ({
      board_fen: String(first(frames[index]?.board_fen, frames[index]?.fen, state.boardFen)),
      uci: String(first(frames[index]?.uci, uci)),
      san: String(first(frames[index]?.san, prefixSan[index], uci)),
    }));
  }

  function playTimeline() {
    return PLAY_TIMELINE.build({
      history: state.history,
      boundary: state.boundary,
      prefix: state.prefix,
      prefixSan: state.prefixSan,
      prefixFrames: state.prefixFrames,
      complete: state.complete,
      check: state.check,
      unusedMoves: state.unusedMoves,
      completionReason: state.completionReason,
      outcome: state.outcome,
      resigned: state.play.resigned,
    });
  }

  function playTimelineCursor(timeline = playTimeline()) {
    return PLAY_TIMELINE.cursorIndex(timeline, state.play.timelineIndex);
  }

  function playReviewActive(timeline = playTimeline()) {
    return state.mode === "play"
      && Number.isInteger(state.play.timelineIndex)
      && playTimelineCursor(timeline) < timeline.length - 1;
  }

  function playReviewPosition(timeline = playTimeline()) {
    return playReviewActive(timeline) ? timeline[playTimelineCursor(timeline)] : null;
  }

  async function stepPlayTimeline(delta) {
    await waitForActiveNewPlayGame();
    if (state.mode !== "play" || state.play.animating || blockStalePlayMutation()) return;
    const timeline = playTimeline();
    const current = playTimelineCursor(timeline);
    const target = Math.max(0, Math.min(timeline.length - 1, current + delta));
    if (target === current) return;
    if (target < timeline.length - 1) {
      if (state.play.thinking || activePlayEngineTurn) await cancelEngineTurn();
      else await cancelPlayPonder("play-history-review");
      state.play.timelineIndex = target;
      state.selected = null;
      renderAll();
      persistPlaySession();
      return;
    }
    state.play.timelineIndex = null;
    state.selected = null;
    void cancelPlayPonder("play-history-return");
    renderAll();
    persistPlaySession();
    void continuePlayFlow();
  }

  function captureWorkspace() {
    return {
      boundary: cloneBoundary(state.boundary),
      prefix: [...state.prefix],
      prefixSan: [...state.prefixSan],
      boardFen: state.boardFen,
      legalMoves: state.legalMoves.map((move) => ({ ...move })),
      movesRemaining: state.movesRemaining,
      complete: state.complete,
      nextState: state.nextState ? cloneBoundary(state.nextState) : null,
      outcome: state.outcome,
      check: state.check,
      unusedMoves: state.unusedMoves,
      completionReason: state.completionReason,
      history: clonePlain(state.history, []),
      prefixFrames: clonePlain(state.prefixFrames, []),
      lastMove: state.lastMove,
      flipped: state.flipped,
      focusSquare: state.focusSquare,
      positionReady: state.positionReady,
      study: clonePlain(state.study, createStudy(state.boundary)),
      currentTreeNodeId: state.currentTreeNodeId,
      seriesParentNodeId: state.seriesParentNodeId,
      branching: state.branching,
      viewingHistorical: state.viewingHistorical,
      handoffNotice: state.handoffNotice,
      analysisPaused: state.analysisPaused,
    };
  }

  function restoreWorkspace(snapshot) {
    if (!snapshot) return false;
    state.boundary = cloneBoundary(snapshot.boundary);
    state.prefix = [...(snapshot.prefix || [])];
    state.prefixSan = [...(snapshot.prefixSan || [])];
    state.boardFen = String(snapshot.boardFen || state.boundary.fen);
    state.legalMoves = (snapshot.legalMoves || []).map((move) => ({ ...move }));
    state.movesRemaining = Math.max(0, asNumber(snapshot.movesRemaining, state.boundary.series));
    state.complete = Boolean(snapshot.complete);
    state.nextState = snapshot.nextState ? cloneBoundary(snapshot.nextState) : null;
    state.outcome = snapshot.outcome || null;
    state.check = Boolean(snapshot.check);
    state.unusedMoves = Math.max(0, asNumber(snapshot.unusedMoves, 0));
    state.completionReason = snapshot.completionReason || null;
    state.history = clonePlain(snapshot.history, []);
    state.prefixFrames = clonePlain(snapshot.prefixFrames, []);
    state.lastMove = snapshot.lastMove || null;
    state.flipped = Boolean(snapshot.flipped);
    state.focusSquare = snapshot.focusSquare || (state.flipped ? "e7" : "e2");
    state.positionReady = Boolean(snapshot.positionReady);
    state.study = clonePlain(snapshot.study, createStudy(state.boundary));
    state.currentTreeNodeId = snapshot.currentTreeNodeId || null;
    state.seriesParentNodeId = snapshot.seriesParentNodeId || null;
    state.branching = Boolean(snapshot.branching);
    state.viewingHistorical = Boolean(snapshot.viewingHistorical);
    state.handoffNotice = snapshot.handoffNotice || null;
    state.analysisPaused = Boolean(snapshot.analysisPaused);
    state.selected = null;
    state.previewIndex = null;
    state.pvFrames = [];
    state.analysis = null;
    state.arrowSelection = null;
    return true;
  }

  function isStoredUciList(value, maximumMoves, allowEmpty = true) {
    return Array.isArray(value)
      && (allowEmpty || value.length > 0)
      && value.length <= maximumMoves
      && value.every((move) => (
        typeof move === "string"
        && /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(move)
      ));
  }

  function initialPlayBoundary() {
    return {
      fen: START_FEN,
      series: 1,
      quiet_series: 0,
      ep_targets: [],
      promoted_hex: ZERO_PROMOTED_HEX,
      chess960: false,
    };
  }

  function sanitizeStoredPlaySession(value) {
    const supportedVersion = value?.version === PLAY_SESSION_SCHEMA_VERSION
      || value?.version === LEGACY_PLAY_SESSION_SCHEMA_VERSION;
    if (
      !value
      || typeof value !== "object"
      || !supportedVersion
      || !["white", "black"].includes(value.humanColor)
      || !PLAY_STRENGTHS[value.strength]
      || value.active !== true
      || typeof value.resigned !== "boolean"
      || !Array.isArray(value.completedSeries)
      || value.completedSeries.length > 511
      || value.completedSeries.some((series, index) => (
        !isStoredUciList(series, index + 1, false)
      ))
      || !isStoredUciList(value.currentPrefix, value.completedSeries.length + 1)
    ) return null;
    const legacy = value.version === LEGACY_PLAY_SESSION_SCHEMA_VERSION;
    const sessionId = legacy ? "legacy-play-session" : value.sessionId;
    const ownerId = legacy ? playSessionTabId : value.ownerId;
    const revision = legacy ? 0 : value.revision;
    if (
      typeof sessionId !== "string"
      || !/^[A-Za-z0-9._:-]{8,160}$/.test(sessionId)
      || typeof ownerId !== "string"
      || !/^[A-Za-z0-9._:-]{8,160}$/.test(ownerId)
      || !Number.isSafeInteger(revision)
      || revision < 0
    ) return null;
    const timelineIndex = value.timelineIndex === null
      ? null
      : Number.isInteger(value.timelineIndex) && value.timelineIndex >= 0
        ? value.timelineIndex
        : null;
    const error = typeof value.error === "string"
      ? value.error.slice(0, 1_000)
      : null;
    return {
      sessionId,
      ownerId,
      revision,
      humanColor: value.humanColor,
      strength: value.strength,
      active: value.active,
      resigned: value.resigned,
      flipped: typeof value.flipped === "boolean"
        ? value.flipped
        : value.humanColor === "black",
      timelineIndex,
      error,
      rulesetVersion: typeof value.rulesetVersion === "string"
        ? value.rulesetVersion
        : null,
      completedSeries: value.completedSeries.map((series) => [...series]),
      currentPrefix: [...value.currentPrefix],
    };
  }

  function readPersistedPlaySession() {
    for (const key of [PLAY_SESSION_STORAGE_KEY, LEGACY_PLAY_SESSION_STORAGE_KEY]) {
      try {
        const stored = localStorage.getItem(key);
        if (!stored || stored.length > MAX_STORED_PLAY_SESSION_BYTES) continue;
        const saved = sanitizeStoredPlaySession(JSON.parse(stored));
        if (!saved) continue;
        playSessionLastWriteDurable = true;
        playSessionExternalUpdate = false;
        playSessionSaveBlocked = false;
        playSessionPendingWriteOptions = null;
        playSessionRevision = saved.revision;
        return saved;
      } catch {
        // A corrupt newer record must not hide a valid legacy game.
      }
    }
    playSessionLastWriteDurable = false;
    return null;
  }

  function sameStoredSeries(left, right) {
    return left.length === right.length
      && left.every((move, index) => move === right[index]);
  }

  function storedPlayLedgerExtends(candidate, existing) {
    if (candidate.completedSeries.length < existing.completedSeries.length) {
      return false;
    }
    for (let index = 0; index < existing.completedSeries.length; index += 1) {
      if (!sameStoredSeries(candidate.completedSeries[index], existing.completedSeries[index])) {
        return false;
      }
    }
    const continuation = candidate.completedSeries.length === existing.completedSeries.length
      ? candidate.currentPrefix
      : candidate.completedSeries[existing.completedSeries.length];
    return existing.currentPrefix.every((move, index) => continuation[index] === move);
  }

  function sameStoredPlayLedger(left, right) {
    return left.completedSeries.length === right.completedSeries.length
      && sameStoredSeries(left.currentPrefix, right.currentPrefix)
      && left.completedSeries.every((series, index) => (
        sameStoredSeries(series, right.completedSeries[index])
      ));
  }

  function sameStoredPlayState(left, right) {
    return sameStoredPlayLedger(left, right)
      && left.humanColor === right.humanColor
      && left.strength === right.strength
      && left.active === right.active
      && left.resigned === right.resigned
      && left.flipped === right.flipped
      && left.timelineIndex === right.timelineIndex
      && left.error === right.error
      && left.rulesetVersion === right.rulesetVersion;
  }

  function persistedPlaySessionWithoutSideEffects() {
    try {
      const stored = localStorage.getItem(PLAY_SESSION_STORAGE_KEY);
      if (!stored || stored.length > MAX_STORED_PLAY_SESSION_BYTES) return null;
      return sanitizeStoredPlaySession(JSON.parse(stored));
    } catch {
      return null;
    }
  }

  function preparePlaySessionWrite() {
    if (!state.playWorkspace || !state.play.active) {
      playSessionLastWriteDurable = false;
      return null;
    }
    const completedSeries = (state.playWorkspace.history || [])
      .map((entry) => Array.isArray(entry?.prefix) ? [...entry.prefix] : null);
    const currentPrefix = Array.isArray(state.playWorkspace.prefix)
      ? [...state.playWorkspace.prefix]
      : null;
    if (
      completedSeries.length > 511
      || completedSeries.some((series, index) => (
        !isStoredUciList(series, index + 1, false)
      ))
      || !isStoredUciList(currentPrefix, completedSeries.length + 1)
    ) {
      playSessionLastWriteDurable = false;
      return null;
    }
    const sessionId = state.play.sessionId;
    if (typeof sessionId !== "string" || !/^[A-Za-z0-9._:-]{8,160}$/.test(sessionId)) {
      playSessionLastWriteDurable = false;
      return null;
    }
    return {
      baseRevision: playSessionRevision,
      candidate: {
        version: PLAY_SESSION_SCHEMA_VERSION,
        savedAt: new Date().toISOString(),
        sessionId,
        ownerId: playSessionTabId,
        revision: playSessionRevision + 1,
        humanColor: state.play.humanColor,
        strength: state.play.strength,
        active: state.play.active,
        resigned: state.play.resigned,
        flipped: Boolean(state.playWorkspace.flipped),
        timelineIndex: state.play.timelineIndex,
        error: state.play.error,
        rulesetVersion: state.play.rulesetVersion,
        completedSeries,
        currentPrefix,
      },
    };
  }

  function storedSessionMatchesReplacementExpectation(existing, options = {}) {
    if (!existing || options.replaceSession !== true) return false;
    if (existing.sessionId !== options.replaceExpectedSessionId) return false;
    if (existing.revision === options.replaceExpectedRevision) return true;
    return existing.ownerId === playSessionTabId
      && Number.isSafeInteger(options.replaceExpectedRevision)
      && existing.revision >= options.replaceExpectedRevision;
  }

  function commitPreparedPlaySession(
    prepared,
    {
      replaceSession = false,
      replaceExpectedSessionId = null,
      replaceExpectedRevision = null,
      claimOwnership = true,
    } = {},
  ) {
    const { candidate, baseRevision } = prepared;
    const sessionId = candidate.sessionId;
    try {
      const existing = persistedPlaySessionWithoutSideEffects();
      if (existing && existing.sessionId !== sessionId) {
        const expectedReplacement = storedSessionMatchesReplacementExpectation(
          existing,
          {
            replaceSession,
            replaceExpectedSessionId,
            replaceExpectedRevision,
          },
        );
        if (!expectedReplacement) return false;
      }
      if (existing && existing.sessionId === sessionId) {
        const followsOwnQueuedWrite = existing.ownerId === playSessionTabId
          && existing.revision === playSessionRevision
          && storedPlayLedgerExtends(candidate, existing);
        if (existing.revision !== baseRevision && !followsOwnQueuedWrite) {
          // A matching write owned by another page is not this page's durable
          // claim. The loser of two simultaneous reloads must stay stale.
          const stateAlreadyDurable = sameStoredPlayState(candidate, existing)
            && (
              existing.ownerId === playSessionTabId
              || claimOwnership === false
            );
          if (stateAlreadyDurable) playSessionRevision = existing.revision;
          return stateAlreadyDurable;
        }
        if (!storedPlayLedgerExtends(candidate, existing)) {
          return false;
        }
        if (existing.ownerId !== playSessionTabId && !claimOwnership) {
          return sameStoredPlayState(candidate, existing);
        }
        candidate.revision = Math.max(existing.revision, playSessionRevision) + 1;
      }
      const serialized = JSON.stringify(candidate);
      if (serialized.length > MAX_STORED_PLAY_SESSION_BYTES) {
        return false;
      }
      localStorage.setItem(PLAY_SESSION_STORAGE_KEY, serialized);
      if (state.play.sessionId === sessionId) playSessionRevision = candidate.revision;
      return true;
    } catch {
      return false;
    }
  }

  function persistPlaySession(options = {}) {
    if (playSessionReplayBlocked) return playSessionLastWriteDurable;
    const prepared = preparePlaySessionWrite();
    if (!prepared) return false;
    const sessionId = prepared.candidate.sessionId;
    const commit = () => commitPreparedPlaySession(prepared, options);
    if (!navigator.locks?.request) {
      const durable = commit();
      if (state.play.sessionId === sessionId) {
        playSessionLastWriteDurable = durable;
        if (durable) playSessionExternalUpdate = false;
      }
      return durable;
    }
    const writeSequence = ++playSessionWriteSequence;
    playSessionLastWriteDurable = false;
    playSessionWriteQueue = playSessionWriteQueue
      .catch(() => false)
      .then(() => navigator.locks.request(PLAY_SESSION_WRITE_LOCK, commit))
      .catch(() => commit());
    void playSessionWriteQueue.then((durable) => {
      if (
        state.play.sessionId === sessionId
        && writeSequence === playSessionWriteSequence
      ) {
        playSessionLastWriteDurable = durable;
        if (durable) playSessionExternalUpdate = false;
      }
    });
    return false;
  }

  async function persistPlaySessionDurably(options = {}) {
    const sessionId = state.play.sessionId;
    const previousWriteSequence = playSessionWriteSequence;
    const immediate = persistPlaySession(options);
    if (!navigator.locks?.request) return immediate;
    if (playSessionWriteSequence === previousWriteSequence) return immediate;
    const writeSequence = playSessionWriteSequence;
    const queuedWrite = playSessionWriteQueue;
    try {
      const durable = Boolean(await queuedWrite);
      if (
        state.play.sessionId === sessionId
        && writeSequence === playSessionWriteSequence
      ) {
        playSessionLastWriteDurable = durable;
        if (durable) playSessionExternalUpdate = false;
      }
      return durable;
    } catch {
      return false;
    }
  }

  function captureAndPersistPlayWorkspace(options = {}) {
    state.playWorkspace = captureWorkspace();
    persistPlaySession(options);
    return state.playWorkspace;
  }

  async function captureAndPersistPlayWorkspaceDurably(options = {}) {
    state.playWorkspace = captureWorkspace();
    return persistPlaySessionDurably(options);
  }

  function markPlaySessionSaveBlocked(options = {}) {
    const stored = persistedPlaySessionWithoutSideEffects();
    const expectedReplacementPredecessor = storedSessionMatchesReplacementExpectation(
      stored,
      options,
    );
    const advancedElsewhere = stored
      && !expectedReplacementPredecessor
      && (
        stored.sessionId !== state.play.sessionId
        || (
          stored.ownerId !== playSessionTabId
          && stored.revision > playSessionRevision
        )
      );
    if (advancedElsewhere) {
      lockPlaySessionForExternalUpdate();
      return;
    }
    playSessionSaveBlocked = true;
    playSessionPendingWriteOptions = { ...options };
    playSessionLastWriteDurable = false;
    renderAll();
  }

  async function requireDurablePlaySession(options = {}, { capture = false } = {}) {
    const durable = capture
      ? await captureAndPersistPlayWorkspaceDurably(options)
      : await persistPlaySessionDurably(options);
    if (!durable) {
      markPlaySessionSaveBlocked(options);
      return false;
    }
    playSessionSaveBlocked = false;
    playSessionPendingWriteOptions = null;
    return true;
  }

  function authoritativeBoundaryEchoMatches(payload, expectedBoundary) {
    if (payload?.boundary_state === undefined) return false;
    const echoedBoundary = safeBoundary(payload.boundary_state);
    return Boolean(echoedBoundary)
      && boundaryKey(echoedBoundary) === boundaryKey(expectedBoundary);
  }

  function resetFailedPlaySessionRestore(saved, error) {
    state.mode = "play";
    state.boundary = initialPlayBoundary();
    state.history = [];
    state.prefix = [];
    state.prefixSan = [];
    state.prefixFrames = [];
    state.boardFen = START_FEN;
    state.legalMoves = [];
    state.movesRemaining = 1;
    state.complete = false;
    state.nextState = null;
    state.outcome = null;
    state.check = false;
    state.unusedMoves = 0;
    state.completionReason = null;
    state.lastMove = null;
    state.selected = null;
    state.previewIndex = null;
    state.pvFrames = [];
    state.analysis = null;
    state.arrowSelection = null;
    state.flipped = saved.flipped;
    state.focusSquare = state.flipped ? "e7" : "e2";
    state.study = createStudy(state.boundary);
    state.currentTreeNodeId = null;
    state.seriesParentNodeId = null;
    state.branching = false;
    state.viewingHistorical = false;
    state.handoffNotice = null;
    state.positionReady = false;
    state.play.active = true;
    state.play.timelineIndex = null;
    state.play.error = `Your saved moves remain stored; validation is waiting: ${displayError(error)}`;
    state.playWorkspace = captureWorkspace();
  }

  async function restorePersistedPlaySession() {
    if (playSessionReplayPromise) return playSessionReplayPromise;
    const saved = readPersistedPlaySession();
    if (!saved) return false;

    await cancelPlayPonder("play-session-restore");

    playSessionReplayBlocked = true;
    state.prefixAbort?.abort();
    cancelAutoAnalysis(true);
    state.pvAbort?.abort();
    const controller = new AbortController();
    state.prefixAbort = controller;
    const sequence = ++state.prefixSequence;
    state.positionReady = false;
    setBoardBusy(true, "Restoring saved game…");

    const replay = (async () => {
      let restored = false;
      const ensureCurrentReplay = () => {
        if (controller.signal.aborted || sequence !== state.prefixSequence) {
          const cancelled = new Error("Saved game replay cancelled.");
          cancelled.name = "AbortError";
          throw cancelled;
        }
      };
      try {
        state.mode = "play";
        state.play.sessionId = saved.sessionId;
        state.play.humanColor = saved.humanColor;
        state.play.strength = saved.strength;
        state.play.active = saved.active;
        state.play.resigned = saved.resigned;
        state.play.timelineIndex = null;
        state.play.error = saved.error;
        state.play.lastEngineSeries = null;
        state.play.lastSearch = null;
        state.play.activeSearch = null;
        state.play.activeSearchRuntime = null;
        state.play.thinking = false;
        state.play.animating = false;
        state.flipped = saved.flipped;
        state.focusSquare = state.flipped ? "e7" : "e2";

        let boundary = initialPlayBoundary();
        const history = [];
        const replayCallCount = saved.completedSeries.length + 1;
        const replayBudgetMs = Math.min(
          PLAY_RESTORE_MAX_TIMEOUT_MS,
          PLAY_PREFIX_RECEIPT_TIMEOUT_MS
            + replayCallCount * PLAY_RESTORE_PER_SERIES_TIMEOUT_MS,
        );
        const replayDeadlineMs = monotonicNow() + replayBudgetMs;
        const nextReplayCallDeadline = () => Math.min(
          replayDeadlineMs,
          monotonicNow() + PLAY_PREFIX_RECEIPT_TIMEOUT_MS,
        );
        for (let index = 0; index < saved.completedSeries.length; index += 1) {
          const moves = saved.completedSeries[index];
          state.boundary = cloneBoundary(boundary);
          state.boardFen = boundary.fen;
          const payload = await requestPlayPrefixJson(
            { ...boundary, prefix: moves },
            {
              signal: controller.signal,
              deadlineMs: nextReplayCallDeadline(),
            },
          );
          ensureCurrentReplay();
          const canonical = Array.isArray(payload.prefix) ? payload.prefix.map(String) : [];
          const nextBoundary = normalizeNextState(payload.next_state);
          if (
            canonical.length !== moves.length
            || canonical.some((move, moveIndex) => move !== moves[moveIndex])
            || !payload.complete
            || payload.outcome
            || !nextBoundary
            || nextBoundary.series !== boundary.series + 1
            || !authoritativeBoundaryEchoMatches(payload, boundary)
          ) throw new Error(`Saved Series ${index + 1} failed authoritative replay.`);
          const prefixSan = notationArray(payload, canonical, canonical);
          history.push({
            boundary: cloneBoundary(boundary),
            prefix: canonical,
            prefixSan,
            frames: prefixFramesFromPayload(payload, canonical, prefixSan),
            check: Boolean(payload.check),
            unusedMoves: Math.max(0, asNumber(payload.unused_moves, 0)),
            completionReason: first(payload.completion_reason, null),
            treeNodeId: null,
            seriesParentNodeId: null,
            handoffKey: `restored-${index + 1}`,
          });
          boundary = nextBoundary;
        }

        state.boundary = cloneBoundary(boundary);
        state.boardFen = boundary.fen;
        state.history = history;
        state.prefix = [];
        state.prefixSan = [];
        state.prefixFrames = [];
        state.study = createStudy(boundary);
        state.currentTreeNodeId = null;
        state.seriesParentNodeId = null;
        state.branching = false;
        state.viewingHistorical = false;
        state.handoffNotice = null;
        const current = await requestPlayPrefixJson(
          { ...boundary, prefix: saved.currentPrefix },
          {
            signal: controller.signal,
            deadlineMs: nextReplayCallDeadline(),
          },
        );
        ensureCurrentReplay();
        const canonicalCurrent = Array.isArray(current.prefix)
          ? current.prefix.map(String)
          : [];
        if (
          canonicalCurrent.length !== saved.currentPrefix.length
          || canonicalCurrent.some((move, index) => move !== saved.currentPrefix[index])
          || !authoritativeBoundaryEchoMatches(current, boundary)
        ) throw new Error("Saved current series failed authoritative replay.");
        applyPrefixPayload(current, canonicalCurrent, canonicalCurrent);
        if (seriesColor(state.boundary.series) === saved.humanColor) {
          state.play.error = null;
        }
        const maximumTimelineIndex = Math.max(0, playTimeline().length - 1);
        state.play.timelineIndex = saved.timelineIndex === null
          ? null
          : Math.min(saved.timelineIndex, maximumTimelineIndex);
        restored = true;
      } catch (error) {
        if (sequence !== state.prefixSequence) return true;
        resetFailedPlaySessionRestore(saved, error);
      } finally {
        if (sequence === state.prefixSequence) {
          if (state.prefixAbort === controller) state.prefixAbort = null;
          setBoardBusy(false);
        }
      }

      if (restored) {
        playSessionReplayBlocked = false;
        await requireDurablePlaySession(
          // A reload has a new page owner. Claim the unchanged saved revision
          // only after its full move ledger passes authoritative replay.
          { claimOwnership: true },
          { capture: true },
        );
        renderAll();
        showToast("Game restored after reload");
        if (!state.play.error && !playSessionSaveBlocked) void continuePlayFlow();
      } else {
        renderAll();
      }
      return true;
    })();
    playSessionReplayPromise = replay;
    try {
      return await replay;
    } finally {
      if (playSessionReplayPromise === replay) playSessionReplayPromise = null;
    }
  }

  function seriesColor(series = state.boundary.series) {
    return Number(series) % 2 === 1 ? "white" : "black";
  }

  function playSearchLimits(strength = state.play.strength) {
    const setting = PLAY_STRENGTHS[strength] || PLAY_STRENGTHS.strong;
    const profileDepth = Math.max(1, Math.floor(state.play.recommendedDepth));
    const desiredDepth = strength === "strong"
      ? Math.max(setting.minimumDepth, profileDepth)
      : profileDepth;
    const generationPositions = Math.max(1_000, Math.min(
      state.maximumGenerationPositions,
      setting.generationPositions,
    ));
    return {
      strength,
      label: setting.label,
      depth: Math.max(1, Math.min(state.maximumAnalysisDepth, desiredDepth)),
      maxSeries: Math.max(1, Math.min(state.maximumBranchCap, state.play.recommendedBranchCap)),
      seconds: Math.max(0.1, Math.min(state.maximumAnalysisSeconds, setting.seconds)),
      generationPositions,
      timeLimitedOnly: generationPositions >= PLAY_TECHNICAL_WORK_CEILING,
    };
  }

  function playSearchEvidence(result, requested) {
    const stats = result?.stats || {};
    const runtimeReceipt = result?.runtime_receipt || {};
    const completedRaw = first(result?.completed_depth, stats.completed_depth);
    const requestedRaw = first(result?.requested_depth, stats.requested_depth, requested.depth);
    const elapsedRaw = first(
      result?.elapsed_seconds,
      stats.elapsed_seconds,
      runtimeReceipt.wall_time_seconds,
    );
    const workerCountRaw = first(runtimeReceipt.worker_count, stats.root_workers, null);
    return {
      strength: requested.strength,
      maxSeries: requested.maxSeries,
      requestedDepth: Math.max(1, Math.floor(asNumber(requestedRaw, requested.depth))),
      completedDepth: Number.isFinite(Number(completedRaw))
        ? Math.max(0, Math.floor(Number(completedRaw)))
        : null,
      exactWidth: asBoolean(first(result?.exact_width, stats.exact_width)),
      timedOut: asBoolean(first(result?.timed_out, stats.timed_out)),
      workLimitReached: asBoolean(first(result?.work_limit_reached, stats.work_limit_reached)),
      rootSearchMode: String(first(result?.root_search_mode, "")),
      rootScoresComplete: asBoolean(first(result?.root_scores_complete, stats.root_scores_complete)),
      rootBoundCoverageComplete: asBoolean(first(
        result?.root_bound_coverage_complete,
        runtimeReceipt.root_bound_coverage_complete,
        stats.coverage_complete,
      )),
      elapsedSeconds: Number.isFinite(Number(elapsedRaw)) ? Number(elapsedRaw) : null,
      runtime: String(first(runtimeReceipt.runtime, "render-server")),
      artifactFingerprint: first(runtimeReceipt.artifact_fingerprint, null),
      threadCount: Math.max(1, Math.floor(asNumber(
        first(runtimeReceipt.thread_count, state.play.nativeThreads),
        1,
      ))),
      workerCount: Number.isFinite(Number(workerCountRaw))
        ? Math.max(1, Math.floor(Number(workerCountRaw)))
        : null,
    };
  }

  function renderPlaySearchEvidence() {
    const limits = playSearchLimits();
    const renderStrengthOption = (node, optionLimits) => {
      const detail = node.querySelector("small");
      if (!state.play.healthReady) {
        if (detail) detail.textContent = "Loading local engine…";
        node.title = "Preparing the on-device WebAssembly engine.";
        return;
      }
      const seconds = optionLimits.seconds.toLocaleString();
      const work = compactNumber(optionLimits.generationPositions);
      const threads = state.play.runtimeMode === "browser-wasm"
        ? state.play.browserRootReady
          ? `${state.play.browserRootWorkerCount} certified single-thread WebAssembly workers`
          : `${state.play.nativeThreads} on-device WebAssembly thread${state.play.nativeThreads === 1 ? "" : "s"}`
        : `${state.play.nativeThreads} native search thread${state.play.nativeThreads === 1 ? "" : "s"}${state.play.nativeThreadsPolicy === "single-thread-pool-avoidance" ? " in host-safe mode" : ""}`;
      const allocation = state.play.runtimeMode === "browser-wasm"
        ? `${state.play.runtimeCpuCount || "available"} logical processors on this device`
        : state.play.runtimeCpuCount === null
          ? "the server's reported allocation"
          : `${state.play.runtimeCpuCount} allocated CPU`;
      if (detail) detail.textContent = optionLimits.timeLimitedOnly
        ? `Depth ${optionLimits.depth} · up to ${seconds}s · time limit only`
        : `Depth ${optionLimits.depth} · up to ${seconds}s · ${work} work`;
      node.title = optionLimits.timeLimitedOnly
        ? `Searches toward depth ${optionLimits.depth} for up to ${seconds} ${optionLimits.seconds === 1 ? "second" : "seconds"}, with no reachable position-work cap, using ${threads} on ${allocation}.`
        : `Searches toward depth ${optionLimits.depth} for up to ${seconds} ${optionLimits.seconds === 1 ? "second" : "seconds"}, capped at ${work} generated positions, using ${threads} on ${allocation}.`;
    };
    renderStrengthOption(dom.play_strength_strong, playSearchLimits("strong"));
    renderStrengthOption(dom.play_strength_faster, playSearchLimits("faster"));
    dom.play_strength_strong.classList.toggle("is-active", state.play.strength === "strong");
    dom.play_strength_faster.classList.toggle("is-active", state.play.strength === "faster");
    dom.play_strength_strong.setAttribute("aria-pressed", String(state.play.strength === "strong"));
    dom.play_strength_faster.setAttribute("aria-pressed", String(state.play.strength === "faster"));
    dom.play_strength_status.textContent = limits.timeLimitedOnly
      ? `${limits.label} · target depth ${limits.depth} · up to ${limits.seconds.toLocaleString()}s · time limit only`
      : `${limits.label} · target depth ${limits.depth} · up to ${limits.seconds.toLocaleString()}s · ${compactNumber(limits.generationPositions)} work cap`;
    if (state.play.activeSearch) {
      const active = state.play.activeSearch;
      if (state.play.activeSearchRuntime === "browser-wasm") {
        const workers = state.play.browserRootReady
          ? Math.max(1, state.play.browserRootWorkerCount)
          : Math.max(1, state.play.nativeThreads);
        dom.play_search_depth.textContent = state.play.browserRootReady
          ? `Searching locally · WASM · ${workers} workers · streaming root scouts`
          : `Searching locally · WASM · ${workers} thread${workers === 1 ? "" : "s"}`;
      } else {
        const threads = Math.max(1, state.play.nativeThreads);
        dom.play_search_depth.textContent = `Searching on hosted engine · ${threads} thread${threads === 1 ? "" : "s"}`;
      }
      dom.play_search_status.textContent = active.timeLimitedOnly
        ? `Best-move alpha-beta across up to ${active.maxSeries} retained series per node · up to ${active.seconds.toLocaleString()}s · no reachable work cap`
        : `Best-move alpha-beta across up to ${active.maxSeries} retained series per node · up to ${active.seconds.toLocaleString()}s · ${compactNumber(active.generationPositions)} position work cap`;
      return;
    }
    const ponder = activePlayPonder;
    if (
      ponder
      && playPonderBaseMatches(ponder)
      && playPonderPositionMatches(ponder)
      && boundaryKey(state.boundary) === ponder.humanBoundaryKey
    ) {
      dom.play_search_depth.textContent = ponder.settled?.ok
        ? "Reply prepared locally · WASM"
        : ponder.analysisStarted
          ? "Thinking ahead locally · WASM"
          : "Preparing a local prediction · WASM";
      dom.play_search_status.textContent = ponder.settled?.ok
        ? "The reply is ready for the predicted series; any different move safely discards it."
        : "Following one certified principal variation while you decide; any different move cancels it.";
      return;
    }
    const evidence = state.play.lastSearch;
    if (!evidence) {
      dom.play_search_depth.textContent = "No engine move yet";
      dom.play_search_status.textContent = "Waiting for the champion";
      return;
    }
    dom.play_search_depth.textContent = evidence.completedDepth === null
      ? `Last completed search · requested depth ${evidence.requestedDepth} · completion not reported`
      : `Last completed search · depth ${evidence.completedDepth} · requested ${evidence.requestedDepth}`;
    const status = [
      evidence.runtime === "browser-wasm"
        ? evidence.rootSearchMode === "streaming-root-iteration" && evidence.workerCount !== null
          ? `On-device WebAssembly · ${evidence.workerCount} single-thread Worker${evidence.workerCount === 1 ? "" : "s"}`
          : `On-device WebAssembly · ${evidence.threadCount} thread${evidence.threadCount === 1 ? "" : "s"}`
        : `Hosted engine · ${evidence.threadCount} thread${evidence.threadCount === 1 ? "" : "s"}`,
      evidence.rootSearchMode === "streaming-root-iteration"
        ? `Best-move streaming root alpha-beta across up to ${evidence.maxSeries} retained series per node`
        : evidence.rootSearchMode === "best-move"
          ? `Best-move alpha-beta across up to ${evidence.maxSeries} retained series per node`
          : "All retained alternatives scored",
      evidence.rootSearchMode === "streaming-root-iteration"
        ? evidence.rootScoresComplete === true
          ? "All retained root scores exact"
          : evidence.rootBoundCoverageComplete === true
            ? "Best move exact; alternatives certified by alpha-beta bounds"
            : "Root bound coverage not certified"
        : null,
      evidence.exactWidth === true ? "Exact width" : evidence.exactWidth === false ? "Selective width" : "Width not reported",
      evidence.timedOut === true ? "Time limit reached" : evidence.timedOut === false ? "Within time limit" : "Time status not reported",
    ];
    if (evidence.workLimitReached === true) status.push("Work limit reached");
    if (evidence.elapsedSeconds !== null) status.push(`${evidence.elapsedSeconds.toFixed(1)}s elapsed`);
    dom.play_search_status.textContent = status.filter(Boolean).join(" · ");
  }

  async function selectPlayStrength(strength) {
    await waitForActiveNewPlayGame();
    if (!PLAY_STRENGTHS[strength] || blockStalePlayMutation()) return;
    if (state.play.strength === strength) {
      if (state.play.error || playSessionSaveBlocked) void retryEngineTurn();
      return;
    }
    state.play.strength = strength;
    let restartSequence = state.play.sequence;
    if (state.play.thinking || activePlayEngineTurn) {
      const cleanup = cancelEngineTurn();
      restartSequence = state.play.sequence;
      await cleanup;
    } else {
      await cancelPlayPonder("play-strength-changed");
    }
    if (
      state.play.sequence !== restartSequence
      || state.play.strength !== strength
      || state.mode !== "play"
      || !state.play.active
      || playSessionExternalUpdate
      || playSessionSaveBlocked
      || playReviewActive()
      || playGameEnded()
    ) return;
    persistPlaySession();
    renderPlaySurface();
    await continuePlayFlow();
  }

  async function retryEngineTurn() {
    await waitForActiveNewPlayGame();
    if (blockStalePlayMutation()) return;
    if (playSessionSaveBlocked) {
      const options = playSessionPendingWriteOptions || {};
      if (!await requireDurablePlaySession(options, { capture: true })) return;
      if (playSessionExternalUpdate) return;
      renderAll();
      if (!state.positionReady) {
        void restorePersistedPlaySession();
        return;
      }
      void continuePlayFlow();
      return;
    }
    if (state.mode === "play" && state.play.active && !state.positionReady) {
      if (state.positionBusy) return;
      void restorePersistedPlaySession();
      return;
    }
    if (
      state.mode !== "play"
      || !state.play.active
      || state.play.thinking
      || state.play.animating
      || playReviewActive()
      || playGameEnded()
      || seriesColor() === state.play.humanColor
    ) return;
    state.play.error = null;
    if (!await requireDurablePlaySession()) return;
    if (playSessionExternalUpdate) return;
    renderAll();
    void continuePlayFlow();
  }

  function playPositionKey() {
    return `${boundaryKey(state.boundary)}|${state.prefix.join(",")}|${state.history.length}`;
  }

  function playGameEnded() {
    return Boolean(state.outcome || state.play.resigned);
  }

  function boardInputAllowed() {
    if (state.mode !== "play") return true;
    return Boolean(
      state.play.active
      && !activeNewPlayGame
      && !playSessionExternalUpdate
      && !playSessionSaveBlocked
      && !playReviewActive()
      && !state.play.thinking
      && !state.play.animating
      && !state.play.error
      && !playGameEnded()
      && seriesColor() === state.play.humanColor
    );
  }

  function cancelEngineTurn() {
    const activeTurn = activePlayEngineTurn;
    state.play.engineAbort?.abort();
    state.play.engineAbort = null;
    const ponderCleanup = cancelPlayPonder("engine-turn-cancelled");
    state.play.sequence += 1;
    state.play.thinking = false;
    state.play.animating = false;
    state.play.activeSearch = null;
    state.play.activeSearchRuntime = null;
    return Promise.all([
      Promise.resolve(activeTurn).catch(() => null),
      Promise.resolve(ponderCleanup).catch(() => null),
    ]).then(() => undefined);
  }

  function blockStalePlayMutation() {
    if (!playSessionExternalUpdate) return false;
    showToast("Reload to continue from the game updated in another tab");
    return true;
  }

  function lockPlaySessionForExternalUpdate() {
    if (playSessionExternalUpdate || !state.play.active) return;
    playSessionExternalUpdate = true;
    playSessionSaveBlocked = false;
    playSessionPendingWriteOptions = null;
    playSessionLastWriteDurable = false;
    void cancelEngineTurn();
    state.prefixAbort?.abort();
    state.prefixAbort = null;
    state.prefixSequence += 1;
    setBoardBusy(false);
    state.play.error = "This game changed in another tab. Reload this tab to continue from the newest saved position.";
    renderAll();
  }

  function safeBoundary(value) {
    if (!value || typeof value !== "object") return null;
    const fen = typeof value.fen === "string" ? value.fen.trim() : "";
    const series = Math.floor(asNumber(value.series, 0));
    const quiet = Math.floor(asNumber(value.quiet_series, -1));
    const epTargets = normalizeEpTargets(value.ep_targets).map((square) => square.toLowerCase());
    const promotedHex = normalizePromotedHex(value.promoted_hex)
      || (fen === START_FEN ? ZERO_PROMOTED_HEX : null);
    if (!fen || fen.length > 180 || fen.split("/").length !== 8) return null;
    if (series < 1 || series > 1000000 || quiet < 0 || quiet > 1000000) return null;
    if (epTargets.length > 8 || epTargets.some((square) => !/^[a-h][1-8]$/.test(square))) return null;
    return {
      fen,
      series,
      quiet_series: quiet,
      ep_targets: epTargets,
      promoted_hex: promotedHex,
      chess960: value.chess960 === true,
    };
  }

  function boundaryKey(boundary) {
    const safe = cloneBoundary(boundary);
    return [
      safe.fen,
      safe.series,
      safe.quiet_series,
      [...safe.ep_targets].sort().join(","),
      safe.promoted_hex || "unknown-promoted-provenance",
      safe.chess960 ? "chess960" : "orthodox",
    ].join("|");
  }

  function sameMoveList(left, right) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((move, index) => move === right[index]);
  }

  function playPonderPrefixKey(boundary, prefix) {
    return `${boundaryKey(boundary)}|${JSON.stringify(prefix)}`;
  }

  function playPonderLimitsKey(search) {
    return JSON.stringify({
      strength: search.strength,
      depth: search.depth,
      maxSeries: search.maxSeries,
      seconds: search.seconds,
      generationPositions: search.generationPositions,
    });
  }

  function currentPlayPonderIdentity() {
    const identity = {
      profileId: state.play.engineProfileId,
      sourceFingerprint: state.play.engineFingerprint,
      engineVersion: state.play.engineVersion,
      artifactFingerprint: state.play.browserWasmArtifact,
      rulesetVersion: state.play.rulesetVersion,
    };
    return Object.values(identity).every((value) => (
      typeof value === "string" && value.length > 0
    )) ? identity : null;
  }

  function playPonderIdentityMatches(record) {
    const identity = currentPlayPonderIdentity();
    return Boolean(identity)
      && record.profileId === identity.profileId
      && record.sourceFingerprint === identity.sourceFingerprint
      && record.engineVersion === identity.engineVersion
      && record.artifactFingerprint === identity.artifactFingerprint
      && record.rulesetVersion === identity.rulesetVersion;
  }

  function playPonderBaseMatches(record, { requireRevision = true } = {}) {
    return Boolean(record)
      && record.generation === playPonderGeneration
      && record.sessionId === state.play.sessionId
      && (!requireRevision || record.sessionRevision === playSessionRevision)
      && record.strength === state.play.strength
      && record.limitsKey === playPonderLimitsKey(playSearchLimits())
      && playPonderIdentityMatches(record)
      && state.mode === "play"
      && state.play.active
      && !playSessionExternalUpdate
      && !playSessionSaveBlocked
      && !playReviewActive()
      && !playGameEnded();
  }

  function playPonderPositionMatches(record) {
    const currentBoundaryKey = boundaryKey(state.boundary);
    if (
      currentBoundaryKey === record.humanBoundaryKey
      && state.history.length === record.humanHistoryLength
      && state.prefix.length <= record.predictedHumanSeries.length
      && sameMoveList(
        state.prefix,
        record.predictedHumanSeries.slice(0, state.prefix.length),
      )
    ) return true;
    return currentBoundaryKey === record.childBoundaryKey
      && sameMoveList(state.prefix, [])
      && playPositionKey() === record.claimPlayKey;
  }

  function cancelPlayPonder(reason = "ponder-cancelled") {
    playPonderGeneration += 1;
    const records = [...new Set([
      activePlayPonder,
      claimedPlayPonder,
    ].filter(Boolean))];
    activePlayPonder = null;
    claimedPlayPonder = null;
    if (!records.length) return playPonderCleanup;
    if (state.mode === "play") renderPlaySearchEvidence();
    records.forEach((record) => {
      record.cancelReason = reason;
      record.controller.abort();
    });
    const drained = Promise.all(
      records.map((record) => Promise.resolve(record.promise).catch(() => null)),
    );
    playPonderCleanup = Promise.all([
      playPonderCleanup.catch(() => null),
      drained,
    ]).then(() => undefined);
    return playPonderCleanup;
  }

  function rebindPlayPonderRevision(record = activePlayPonder) {
    if (
      record
      && activePlayPonder === record
      && playPonderBaseMatches(record, { requireRevision: false })
      && playPonderPositionMatches(record)
    ) {
      record.sessionRevision = playSessionRevision;
      return true;
    }
    return false;
  }

  function cachedPlayPonderPrefix(boundary, prefix) {
    const record = activePlayPonder || claimedPlayPonder;
    if (
      !record
      || !playPonderBaseMatches(record)
      || !playPonderPositionMatches(record)
    ) return null;
    const requestedBoundaryKey = boundaryKey(boundary);
    const humanPrefix = requestedBoundaryKey === record.humanBoundaryKey
      && prefix.length > 0
      && prefix.length <= record.predictedHumanSeries.length
      && sameMoveList(prefix, record.predictedHumanSeries.slice(0, prefix.length));
    const childPrefix = requestedBoundaryKey === record.childBoundaryKey
      && sameMoveList(prefix, []);
    if (!humanPrefix && !childPrefix) return null;
    const payload = record.prefixPayloads.get(playPonderPrefixKey(boundary, prefix));
    const canonical = Array.isArray(payload?.prefix) ? payload.prefix.map(String) : [];
    if (
      !payload
      || !authoritativeBoundaryEchoMatches(payload, boundary)
      || !sameMoveList(canonical, prefix)
    ) return null;
    return { record, payload };
  }

  function applyCachedPlayPonderPrefix(payload, requestedPrefix, requestedSan) {
    state.prefixAbort?.abort();
    state.prefixAbort = null;
    state.prefixSequence += 1;
    state.positionReady = false;
    cancelAutoAnalysis(true);
    state.pvAbort?.abort();
    applyPrefixPayload(payload, requestedPrefix, requestedSan);
    setBoardBusy(false);
  }

  function exactPonderPvSeries(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const rawMoves = value.moves;
    if (
      !Array.isArray(rawMoves)
      || !rawMoves.length
      || rawMoves.some((move) => !/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(String(move)))
    ) return null;
    const childBoundary = safeBoundary(value.child_boundary);
    if (!childBoundary) return null;
    return {
      moves: rawMoves.map(String),
      childBoundary,
      outcome: value.outcome ?? null,
    };
  }

  function certifiedLocalPonderPrediction(analysis, checked, canonical, search) {
    const receipt = analysis?.runtime_receipt || {};
    const identity = currentPlayPonderIdentity();
    const requestedDepth = Math.max(1, Math.floor(asNumber(
      first(analysis?.requested_depth, receipt.requested_depth),
      0,
    )));
    const completedDepth = Math.max(0, Math.floor(asNumber(
      first(analysis?.completed_depth, receipt.completed_depth),
      0,
    )));
    if (
      !identity
      || !browserEngineClient
      || receipt.runtime !== "browser-wasm"
      || analysis?.publishable !== true
      || analysis?.safety_certified !== true
      || analysis?.legal_series_certified !== true
      || analysis?.authoritative_replay_certified !== true
      || analysis?.legal_validation_runtime !== "compiled-wasm"
      || asBoolean(first(
        analysis?.root_bound_coverage_complete,
        receipt.root_bound_coverage_complete,
      )) !== true
      || requestedDepth !== search.depth
      || completedDepth < 1
      || completedDepth > requestedDepth
      || analysis.engine_profile_id !== identity.profileId
      || analysis.source_fingerprint !== identity.sourceFingerprint
      || analysis.engine_version !== identity.engineVersion
      || analysis.ruleset_version !== identity.rulesetVersion
      || first(analysis.wasm_sha256, receipt.artifact_fingerprint) !== identity.artifactFingerprint
      || checked?.complete !== true
      || checked?.outcome
      || !checked?.next_state
    ) return null;
    const humanBoundary = safeBoundary(checked.next_state);
    const pv = Array.isArray(analysis.principal_variation)
      ? analysis.principal_variation
      : [];
    const root = exactPonderPvSeries(pv[0]);
    const predicted = exactPonderPvSeries(pv[1]);
    if (
      !humanBoundary
      || !root
      || !predicted
      || root.outcome
      || predicted.outcome
      || !sameMoveList(root.moves, canonical)
      || boundaryKey(root.childBoundary) !== boundaryKey(humanBoundary)
      || predicted.moves.length > humanBoundary.series
      || predicted.childBoundary.series !== humanBoundary.series + 1
      || seriesColor(humanBoundary.series) !== state.play.humanColor
    ) return null;
    return {
      identity,
      humanBoundary,
      predictedHumanSeries: predicted.moves,
      childBoundary: predicted.childBoundary,
    };
  }

  function assertLivePlayPonder(record) {
    if (
      activePlayPonder !== record
      || record.controller.signal.aborted
      || !playPonderBaseMatches(record, { requireRevision: false })
      || !playPonderPositionMatches(record)
    ) throw new DOMException("Ponder cancelled", "AbortError");
  }

  async function runPlayPonder(record) {
    for (let index = 1; index <= record.predictedHumanSeries.length; index += 1) {
      assertLivePlayPonder(record);
      const prefix = record.predictedHumanSeries.slice(0, index);
      const payload = await browserEngineClient.inspectPrefix({
        ...record.humanBoundary,
        progressive_ep: [...record.humanBoundary.ep_targets],
        prefix,
      }, { signal: record.controller.signal });
      assertLivePlayPonder(record);
      const canonical = Array.isArray(payload?.prefix) ? payload.prefix.map(String) : [];
      const final = index === record.predictedHumanSeries.length;
      const nextBoundary = final ? normalizeNextState(payload.next_state) : null;
      if (
        !authoritativeBoundaryEchoMatches(payload, record.humanBoundary)
        || !sameMoveList(canonical, prefix)
        || (!final && (payload.complete || payload.outcome))
        || (final && (
          payload.complete !== true
          || payload.outcome
          || !nextBoundary
          || boundaryKey(nextBoundary) !== record.childBoundaryKey
        ))
      ) throw new Error("The predicted human series failed compiled replay.");
      record.prefixPayloads.set(
        playPonderPrefixKey(record.humanBoundary, prefix),
        payload,
      );
    }

    assertLivePlayPonder(record);
    const childPayload = await browserEngineClient.inspectPrefix({
      ...record.childBoundary,
      progressive_ep: [...record.childBoundary.ep_targets],
      prefix: [],
    }, { signal: record.controller.signal });
    assertLivePlayPonder(record);
    const childPrefix = Array.isArray(childPayload?.prefix)
      ? childPayload.prefix.map(String)
      : [];
    if (
      !authoritativeBoundaryEchoMatches(childPayload, record.childBoundary)
      || !sameMoveList(childPrefix, [])
      || childPayload.complete
      || childPayload.outcome
    ) throw new Error("The predicted reply boundary failed compiled replay.");
    record.prefixPayloads.set(
      playPonderPrefixKey(record.childBoundary, []),
      childPayload,
    );

    const search = record.search;
    const analysisBody = {
      ...record.childBoundary,
      progressive_ep: [...record.childBoundary.ep_targets],
      prefix: [],
      depth: search.depth,
      max_series: search.maxSeries,
      time_limit: search.seconds,
      max_generation_positions: search.generationPositions,
      alternatives: 0,
      best_move_only: true,
      rate_move: false,
      save: false,
    };
    if (!browserEngineClient.canAnalyzeRoot(analysisBody)) {
      throw new Error("The certified local root lane is unavailable for pondering.");
    }
    record.analysisBody = analysisBody;
    record.analysisStarted = true;
    if (state.mode === "play") renderPlaySearchEvidence();
    const searchDeadlineMs = monotonicNow() + search.seconds * 1000;
    return browserEngineClient.analyzeRoot(analysisBody, {
      signal: record.controller.signal,
      searchDeadlineMs,
      receiptDeadlineMs: searchDeadlineMs + PLAY_ANALYSIS_RESPONSE_GRACE_MS,
    });
  }

  async function startPlayPonder(analysis, checked, canonical, search) {
    await cancelPlayPonder("ponder-replaced");
    try {
      const prediction = certifiedLocalPonderPrediction(
        analysis,
        checked,
        canonical,
        search,
      );
      if (
        !prediction
        || state.complete
        || state.nextState
        || !state.positionReady
        || boundaryKey(state.boundary) !== boundaryKey(prediction.humanBoundary)
        || !sameMoveList(state.prefix, [])
      ) return;
      const sessionId = state.play.sessionId;
      const record = {
        generation: playPonderGeneration,
        controller: new AbortController(),
        sessionId,
        sessionRevision: playSessionRevision,
        strength: search.strength,
        search: { ...search },
        limitsKey: playPonderLimitsKey(search),
        profileId: prediction.identity.profileId,
        sourceFingerprint: prediction.identity.sourceFingerprint,
        engineVersion: prediction.identity.engineVersion,
        artifactFingerprint: prediction.identity.artifactFingerprint,
        rulesetVersion: prediction.identity.rulesetVersion,
        humanBoundary: cloneBoundary(prediction.humanBoundary),
        humanBoundaryKey: boundaryKey(prediction.humanBoundary),
        humanHistoryLength: state.history.length,
        predictedHumanSeries: [...prediction.predictedHumanSeries],
        childBoundary: cloneBoundary(prediction.childBoundary),
        childBoundaryKey: boundaryKey(prediction.childBoundary),
        claimPlayKey: `${boundaryKey(prediction.childBoundary)}||${state.history.length + 1}`,
        prefixPayloads: new Map(),
        analysisBody: null,
        analysisStarted: false,
        promise: null,
      };
      activePlayPonder = record;
      renderPlaySearchEvidence();
      record.promise = runPlayPonder(record).then(
        (result) => ({ ok: true, result }),
        (error) => ({ ok: false, error }),
      );
      void record.promise.then((settled) => {
        record.settled = settled;
        if (!settled.ok && activePlayPonder === record) activePlayPonder = null;
        if (state.mode === "play") renderPlaySearchEvidence();
      });
    } catch {
      // Pondering is an optional local optimization and never interrupts play.
    }
  }

  async function claimMatchingPlayPonder(search) {
    const record = activePlayPonder;
    if (!record) {
      await playPonderCleanup.catch(() => null);
      return null;
    }
    const matches = playPonderBaseMatches(record)
      && playPonderPositionMatches(record)
      && boundaryKey(state.boundary) === record.childBoundaryKey
      && playPositionKey() === record.claimPlayKey
      && record.analysisStarted
      && record.analysisBody
      && record.limitsKey === playPonderLimitsKey(search);
    if (!matches) {
      await cancelPlayPonder("ponder-claim-mismatch");
      return null;
    }
    activePlayPonder = null;
    claimedPlayPonder = record;
    record.claimed = true;
    renderPlaySearchEvidence();
    return record;
  }

  function safeMovePrefix(value, boundary) {
    if (!Array.isArray(value)) return [];
    return value
      .map(String)
      .map((move) => move.toLowerCase())
      .filter((move) => /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(move))
      .slice(0, Math.min(boundary.series, MAX_STORED_NODES));
  }

  function sanitizeSavedPosition(value) {
    if (!value || typeof value !== "object") return null;
    const boundary = safeBoundary(value.boundary);
    if (!boundary) return null;
    const prefix = safeMovePrefix(value.prefix, boundary);
    if (Array.isArray(value.prefix) && prefix.length !== value.prefix.length) return null;
    const name = typeof value.name === "string" ? value.name.trim().slice(0, 60) : "";
    const id = typeof value.id === "string" && value.id.length <= 100 ? value.id : createId("position");
    return {
      id,
      name: name || `Series ${boundary.series} position`,
      boundary,
      prefix,
      createdAt: typeof value.createdAt === "string" ? value.createdAt : new Date().toISOString(),
    };
  }

  function restoreSavedPositions() {
    try {
      const raw = JSON.parse(localStorage.getItem(POSITION_STORAGE_KEY) || "null");
      if (!raw || raw.version !== POSITION_SCHEMA_VERSION || !Array.isArray(raw.positions)) {
        state.savedPositions = [];
        return;
      }
      state.savedPositions = raw.positions
        .slice(0, MAX_SAVED_POSITIONS)
        .map(sanitizeSavedPosition)
        .filter(Boolean);
    } catch {
      state.savedPositions = [];
    }
  }

  function persistSavedPositions() {
    try {
      localStorage.setItem(POSITION_STORAGE_KEY, JSON.stringify({
        version: POSITION_SCHEMA_VERSION,
        positions: state.savedPositions.slice(0, MAX_SAVED_POSITIONS),
      }));
      return true;
    } catch {
      return false;
    }
  }

  function savedPositionLabel() {
    const played = state.prefix.length;
    return played
      ? `Series ${state.boundary.series} after move ${played}`
      : `Series ${state.boundary.series} boundary`;
  }

  function renderSavedPositions() {
    if (!dom.saved_positions_list) return;
    if (!state.savedPositions.length) {
      const empty = document.createElement("p");
      empty.className = "saved-empty";
      empty.textContent = "No saved positions yet.";
      dom.saved_positions_list.replaceChildren(empty);
      return;
    }
    const rows = state.savedPositions.map((saved) => {
      const row = document.createElement("div");
      row.className = "saved-position-row";
      const copy = document.createElement("div");
      copy.className = "saved-position-copy";
      const title = document.createElement("strong");
      title.textContent = saved.name;
      const detail = document.createElement("small");
      detail.textContent = `Series ${saved.boundary.series} · ${saved.prefix.length} played move${saved.prefix.length === 1 ? "" : "s"}`;
      copy.append(title, detail);
      const load = document.createElement("button");
      load.type = "button";
      load.className = "saved-load";
      load.textContent = "Load";
      load.setAttribute("aria-label", `Load ${saved.name}`);
      load.addEventListener("click", () => loadSavedPosition(saved.id));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "saved-delete";
      remove.textContent = "Delete";
      remove.setAttribute("aria-label", `Delete ${saved.name}`);
      remove.addEventListener("click", () => {
        state.savedPositions = state.savedPositions.filter((candidate) => candidate.id !== saved.id);
        const stored = persistSavedPositions();
        dom.saved_position_status.textContent = stored ? `Deleted ${saved.name}` : "The position was removed for this session, but local storage could not be updated.";
        renderSavedPositions();
      });
      row.append(copy, load, remove);
      return row;
    });
    dom.saved_positions_list.replaceChildren(...rows);
  }

  function openSavedPositions(focusName = false) {
    renderSavedPositions();
    dom.saved_position_status.textContent = `${state.savedPositions.length} saved position${state.savedPositions.length === 1 ? "" : "s"}`;
    dom.saved_name.value = focusName ? savedPositionLabel() : "";
    if (!dom.saved_dialog.open) dom.saved_dialog.showModal();
    window.setTimeout(() => {
      if (focusName) {
        dom.saved_name.focus();
        dom.saved_name.select();
      } else {
        dom.saved_positions_list.querySelector("button")?.focus();
      }
    }, 0);
  }

  function saveCurrentPosition(event) {
    event?.preventDefault();
    if (!state.positionReady || state.positionBusy) {
      dom.saved_position_status.textContent = "Wait for the server to finish checking the position.";
      return;
    }
    const name = dom.saved_name.value.trim().slice(0, 60) || savedPositionLabel();
    const saved = {
      id: createId("position"),
      name,
      boundary: cloneBoundary(state.boundary),
      prefix: [...state.prefix],
      createdAt: new Date().toISOString(),
    };
    state.savedPositions.unshift(saved);
    state.savedPositions = state.savedPositions.slice(0, MAX_SAVED_POSITIONS);
    const stored = persistSavedPositions();
    dom.saved_position_status.textContent = stored
      ? `Saved ${name} on this device.`
      : "This browser could not store the position.";
    dom.saved_name.value = "";
    renderSavedPositions();
  }

  function createId(prefix = "move") {
    const random = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    return `${prefix}-${random}`;
  }

  function createStudy(rootBoundary = state.boundary) {
    const root = cloneBoundary(rootBoundary);
    return {
      version: STUDY_SCHEMA_VERSION,
      id: createId("study"),
      rootBoundary: root,
      nodes: {},
      analyses: {},
      cursor: {
        boundary: root,
        prefix: [],
        san: [],
        nodeId: null,
        seriesParentNodeId: null,
      },
      updatedAt: new Date().toISOString(),
    };
  }

  function sanitizeStoredStudy(raw) {
    if (!raw || typeof raw !== "object" || raw.version !== STUDY_SCHEMA_VERSION) return null;
    const rootBoundary = safeBoundary(raw.rootBoundary);
    if (!rootBoundary) return null;
    const study = createStudy(rootBoundary);
    if (typeof raw.id === "string" && raw.id.length <= 100) study.id = raw.id;
    const sourceNodes = Array.isArray(raw.nodes) ? raw.nodes : Object.values(raw.nodes || {});
    sourceNodes.slice(0, MAX_STORED_NODES).forEach((candidate) => {
      if (!candidate || typeof candidate !== "object") return;
      const id = typeof candidate.id === "string" && candidate.id.length <= 100 ? candidate.id : null;
      const parentId = candidate.parentId === null || candidate.parentId === undefined
        ? null
        : typeof candidate.parentId === "string" && candidate.parentId.length <= 100 ? candidate.parentId : undefined;
      const seriesParentId = candidate.seriesParentId === null || candidate.seriesParentId === undefined
        ? null
        : typeof candidate.seriesParentId === "string" && candidate.seriesParentId.length <= 100 ? candidate.seriesParentId : undefined;
      const move = typeof candidate.uci === "string" ? candidate.uci.toLowerCase() : "";
      const boundary = safeBoundary(candidate.boundary);
      const prefix = Array.isArray(candidate.prefix)
        ? candidate.prefix.map(String).map((item) => item.toLowerCase()).filter((item) => /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(item))
        : [];
      if (!id || parentId === undefined || seriesParentId === undefined || !boundary || !/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(move)) return;
      if (!prefix.length || prefix.length > boundary.series || prefix.at(-1) !== move) return;
      const san = typeof candidate.san === "string" ? candidate.san.slice(0, 40) : move;
      study.nodes[id] = {
        id,
        parentId,
        seriesParentId,
        uci: move,
        san,
        boundary,
        prefix,
        series: boundary.series,
        micro: prefix.length,
        complete: Boolean(candidate.complete),
        validated: false,
        quality: null,
        createdAt: typeof candidate.createdAt === "string" ? candidate.createdAt : "",
      };
    });
    Object.values(study.nodes).forEach((node) => {
      if (node.parentId && !study.nodes[node.parentId]) delete study.nodes[node.id];
      if (node.seriesParentId && !study.nodes[node.seriesParentId]) node.seriesParentId = null;
    });
    // Engine proof is deliberately not trusted across reloads. The move tree
    // persists, while quality badges require a fresh server analysis.
    study.analyses = {};
    const cursorBoundary = safeBoundary(raw.cursor?.boundary) || rootBoundary;
    const cursorPrefix = Array.isArray(raw.cursor?.prefix)
      ? raw.cursor.prefix.map(String).map((item) => item.toLowerCase()).filter((item) => /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(item)).slice(0, cursorBoundary.series)
      : [];
    const cursorSan = Array.isArray(raw.cursor?.san) ? raw.cursor.san.map(String).map((item) => item.slice(0, 40)).slice(0, cursorPrefix.length) : [];
    const nodeId = typeof raw.cursor?.nodeId === "string" && study.nodes[raw.cursor.nodeId] ? raw.cursor.nodeId : null;
    const seriesParentNodeId = typeof raw.cursor?.seriesParentNodeId === "string" && study.nodes[raw.cursor.seriesParentNodeId]
      ? raw.cursor.seriesParentNodeId
      : null;
    study.cursor = { boundary: cursorBoundary, prefix: cursorPrefix, san: cursorSan, nodeId, seriesParentNodeId };
    study.updatedAt = typeof raw.updatedAt === "string" ? raw.updatedAt : new Date().toISOString();
    return study;
  }

  function restoreStudy() {
    try {
      const stored = localStorage.getItem(STUDY_STORAGE_KEY);
      const parsed = stored ? sanitizeStoredStudy(JSON.parse(stored)) : null;
      state.study = parsed || createStudy(state.boundary);
    } catch {
      state.study = createStudy(state.boundary);
    }
    const cursor = state.study.cursor;
    state.boundary = cloneBoundary(cursor.boundary);
    state.currentTreeNodeId = cursor.nodeId;
    state.seriesParentNodeId = cursor.seriesParentNodeId;
    return { prefix: [...cursor.prefix], san: [...cursor.san] };
  }

  function persistStudy() {
    if (!state.study) return;
    state.study.updatedAt = new Date().toISOString();
    state.study.cursor = {
      boundary: cloneBoundary(state.boundary),
      prefix: [...state.prefix],
      san: [...state.prefixSan],
      nodeId: state.currentTreeNodeId,
      seriesParentNodeId: state.seriesParentNodeId,
    };
    try {
      localStorage.setItem(STUDY_STORAGE_KEY, JSON.stringify(state.study));
      if (dom.study_save_state) dom.study_save_state.textContent = "Saved locally";
    } catch {
      if (dom.study_save_state) dom.study_save_state.textContent = "Local save full";
    }
  }

  function resetStudy(rootBoundary = state.boundary) {
    state.study = createStudy(rootBoundary);
    state.currentTreeNodeId = null;
    state.seriesParentNodeId = null;
    state.branching = false;
    persistStudy();
    renderStudyTree();
  }

  function treeChildren(parentId) {
    if (!state.study) return [];
    return Object.values(state.study.nodes)
      .filter((node) => node.parentId === parentId)
      .sort((left, right) => String(left.createdAt).localeCompare(String(right.createdAt)) || left.uci.localeCompare(right.uci));
  }

  function treeNodeFromCursor() {
    return state.currentTreeNodeId ? state.study?.nodes[state.currentTreeNodeId] || null : null;
  }

  function setBoardBusy(busy, label = "Loading board…") {
    state.positionBusy = busy;
    dom.board.setAttribute("aria-busy", String(busy));
    dom.board_loading.classList.toggle("is-hidden", !busy);
    dom.board_shell.classList.toggle("is-checking", busy);
    if (busy) dom.board_loading_text.textContent = label;
    if (busy) state.selected = null;
  }

  function analysisPositionKey() {
    return `${boundaryKey(state.boundary)}|${state.prefix.join(",")}`;
  }

  function autoDepthLimit() {
    return Math.max(1, Math.min(
      state.maximumAnalysisDepth,
      Math.floor(asNumber(dom.depth_control.value, state.maximumAnalysisDepth)),
    ));
  }

  function updateAnalysisProgress(message = null) {
    const maximum = autoDepthLimit();
    const completed = Math.max(0, Math.min(maximum, state.analysisCompletedDepth));
    const visual = state.analysisRunning
      ? Math.max(completed, Math.min(maximum, state.analysisRequestedDepth - 0.35))
      : completed;
    dom.analysis_progress.setAttribute("aria-valuemax", String(maximum));
    dom.analysis_progress.setAttribute("aria-valuenow", String(completed));
    dom.analysis_progress_fill.style.width = `${maximum ? visual / maximum * 100 : 0}%`;
    const inspector = document.querySelector(".inspector");
    inspector?.classList.toggle("is-analyzing", state.analysisRunning);
    dom.analyze_button.classList.toggle("is-paused", state.analysisPaused);
    dom.analyze_button.setAttribute("aria-pressed", String(state.analysisPaused));
    dom.analyze_button.disabled = Boolean(state.outcome);
    const label = dom.analyze_button.querySelector("span");
    if (label) label.textContent = state.analysisPaused ? "Resume" : "Pause";
    const icon = dom.analyze_button.querySelector("path");
    if (icon) icon.setAttribute("d", state.analysisPaused ? "M8 5v14l11-7L8 5Z" : "M7 5h4v14H7V5Zm6 0h4v14h-4V5Z");
    if (message !== null) {
      dom.analysis_progress_text.textContent = message;
    } else if (state.outcome) {
      dom.analysis_progress_text.textContent = `Game over · ${humanize(state.outcome)}`;
    } else if (state.analysisPaused) {
      dom.analysis_progress_text.textContent = completed ? `Paused at depth ${completed}` : "Paused";
    } else if (state.analysisRunning) {
      dom.analysis_progress_text.textContent = `Searching depth ${state.analysisRequestedDepth} · depth ${completed} complete`;
    } else if (completed >= maximum) {
      dom.analysis_progress_text.textContent = `Depth ${completed} complete`;
    } else {
      dom.analysis_progress_text.textContent = "Waiting for the position…";
    }
  }

  function cancelAutoAnalysis(resetDepth = true) {
    window.clearTimeout(state.analysisTimer);
    state.analysisTimer = null;
    state.analysisAbort?.abort();
    state.analysisAbort = null;
    state.analysisRunning = false;
    state.analysisSequence += 1;
    if (resetDepth) {
      state.analysisPassDepth = 0;
      state.analysisCompletedDepth = 0;
      state.analysisRequestedDepth = 0;
    }
    updateAnalysisProgress();
  }

  function queueAutoAnalysis(delay = AUTO_ANALYSIS_DEBOUNCE_MS) {
    window.clearTimeout(state.analysisTimer);
    state.analysisTimer = null;
    if (state.mode !== "analyze" || state.analysisPaused || state.outcome || !state.positionReady || state.positionBusy) {
      updateAnalysisProgress();
      return;
    }
    const maximum = autoDepthLimit();
    if (state.analysisPassDepth >= maximum) {
      updateAnalysisProgress(state.analysisCompletedDepth < state.analysisPassDepth
        ? `Requested depth ${state.analysisPassDepth} · completed depth ${state.analysisCompletedDepth}`
        : `Depth ${state.analysisCompletedDepth || maximum} complete`);
      return;
    }
    const sequence = state.analysisSequence;
    const key = analysisPositionKey();
    const next = state.analysisPassDepth + 1;
    updateAnalysisProgress(`Queued depth ${next} · depth ${state.analysisCompletedDepth} complete`);
    state.analysisTimer = window.setTimeout(() => {
      state.analysisTimer = null;
      void runAutoAnalysisPass(sequence, key);
    }, delay);
  }

  function restartAutoAnalysis(delay = AUTO_ANALYSIS_DEBOUNCE_MS) {
    cancelAutoAnalysis(true);
    dom.analysis_error.hidden = true;
    queueAutoAnalysis(delay);
  }

  function analysisProofLabel(result) {
    const meta = proofMetadata(result);
    if (asBoolean(result.work_limit_reached) === true) return "work limit reached";
    if (meta.timedOut === true) return "timed out";
    if (meta.exact === true) return "exact width";
    if (meta.exact === false) return "selective width";
    return "width unreported";
  }

  function hasCertifiedProof(result) {
    const meta = proofMetadata(result);
    return Boolean(result.proven_result)
      && meta.exact === true
      && meta.timedOut === false
      && asBoolean(result.work_limit_reached) !== true
      && Number(meta.completed) >= Number(meta.requested);
  }

  async function runAutoAnalysisPass(sequence, key) {
    if (
      sequence !== state.analysisSequence
      || key !== analysisPositionKey()
      || state.mode !== "analyze"
      || state.analysisPaused
      || state.outcome
      || !state.positionReady
      || state.positionBusy
    ) return;
    state.pvAbort?.abort();
    const maximum = autoDepthLimit();
    if (state.analysisPassDepth >= maximum) return;
    exitPvPreview(false);
    const requestedDepth = Math.min(maximum, state.analysisPassDepth + 1);
    const controller = new AbortController();
    state.analysisAbort = controller;
    state.analysisRunning = true;
    state.analysisRequestedDepth = requestedDepth;
    const hasResult = Boolean(state.analysis);
    dom.analysis_empty.hidden = true;
    dom.analysis_error.hidden = true;
    dom.analysis_loading.hidden = hasResult;
    updateAnalysisProgress();
    try {
      const maxSeries = Math.max(1, Math.min(
        state.maximumBranchCap,
        Math.floor(asNumber(dom.cap_control.value, 256)),
      ));
      const timeLimit = Math.max(0.1, Math.min(
        state.maximumAnalysisSeconds,
        asNumber(dom.time_control.value, 5),
      ));
      const alternatives = Math.max(0, Math.min(
        3,
        state.maximumAlternatives,
        Math.floor(asNumber(dom.alternatives_control.value, 3)),
      ));
      const preset = ANALYSIS_PRESETS[state.analysisPreset] || ANALYSIS_PRESETS.strong;
      const generationPositions = Math.min(
        state.maximumGenerationPositions,
        Math.max(1_000, Math.floor(asNumber(preset.generationPositions, state.maximumGenerationPositions))),
      );
      const payload = await requestJson("/api/analyze", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          ...boundaryPayload(),
          prefix: [...state.prefix],
          depth: requestedDepth,
          max_series: maxSeries,
          time_limit: timeLimit,
          max_generation_positions: generationPositions,
          alternatives,
          rate_move: state.prefix.length > 0,
          save: false,
        }),
      });
      if (sequence !== state.analysisSequence || key !== analysisPositionKey()) return;
      const result = first(payload.analysis, payload.result, payload);
      state.analysisPassDepth = requestedDepth;
      const meta = proofMetadata(result);
      state.analysisCompletedDepth = Math.max(
        state.analysisCompletedDepth,
        Math.max(0, Math.floor(asNumber(meta.completed, requestedDepth))),
      );
      state.arrowSelection = null;
      renderAnalysis(result);
      recordAnalysis(result);
      const proof = analysisProofLabel(result);
      if (hasCertifiedProof(result)) {
        updateAnalysisProgress(`Depth ${state.analysisCompletedDepth} · certified ${humanize(result.proven_result)}`);
        return;
      }
      if (requestedDepth < maximum) {
        updateAnalysisProgress(`Depth ${state.analysisCompletedDepth} complete · ${proof} · deepening`);
        queueAutoAnalysis(150);
      } else {
        updateAnalysisProgress(state.analysisCompletedDepth < requestedDepth
          ? `Requested depth ${requestedDepth} · completed depth ${state.analysisCompletedDepth} · ${proof}`
          : `Depth ${state.analysisCompletedDepth} complete · ${proof}`);
      }
    } catch (error) {
      if (error.name === "AbortError" || sequence !== state.analysisSequence) return;
      if (error.status === 429) {
        dom.analysis_loading.hidden = Boolean(state.analysis);
        updateAnalysisProgress("Engine busy · retrying this depth");
        queueAutoAnalysis(AUTO_ANALYSIS_RETRY_MS);
        return;
      }
      dom.analysis_loading.hidden = true;
      dom.analysis_error.hidden = false;
      dom.analysis_error_text.textContent = displayError(error);
      dom.analysis_empty.hidden = true;
      updateAnalysisProgress(`Analysis stopped · ${displayError(error)}`);
    } finally {
      if (state.analysisAbort === controller) state.analysisAbort = null;
      if (sequence === state.analysisSequence) {
        state.analysisRunning = false;
        document.querySelector(".inspector")?.classList.remove("is-analyzing");
      }
    }
  }

  function toggleAutoAnalysis() {
    state.analysisPaused = !state.analysisPaused;
    if (state.analysisPaused) {
      cancelAutoAnalysis(false);
      dom.analysis_loading.hidden = Boolean(state.analysis);
      dom.analysis_empty.hidden = Boolean(state.analysis);
      updateAnalysisProgress();
      return;
    }
    state.analysisSequence += 1;
    updateAnalysisProgress();
    queueAutoAnalysis(80);
  }

  function applyPrefixPayload(payload, requestedPrefix, requestedSan) {
    state.prefix = Array.isArray(first(payload.prefix, payload.current_prefix))
      ? first(payload.prefix, payload.current_prefix).map(String)
      : [...requestedPrefix];
    state.prefixSan = notationArray(payload, state.prefix, requestedSan);
    state.boardFen = String(first(
      payload.board_fen,
      payload.fen,
      payload.current_state?.fen,
      state.boundary.fen,
    ));
    state.prefixFrames = prefixFramesFromPayload(payload, state.prefix, state.prefixSan);
    state.legalMoves = (first(payload.legal_moves, payload.legal_next, payload.moves, []) || []).map(normalizeMove);
    state.movesRemaining = Math.max(0, asNumber(first(
      payload.moves_remaining,
      payload.remaining,
      state.boundary.series - state.prefix.length,
    )));
    state.complete = Boolean(first(payload.complete, payload.series_complete, false));
    state.nextState = normalizeNextState(payload.next_state);
    state.outcome = first(payload.outcome, payload.terminal, null);
    state.check = Boolean(first(payload.check, payload.ended_by_check, false));
    state.unusedMoves = Math.max(0, asNumber(first(payload.unused_moves, 0)));
    state.completionReason = first(payload.completion_reason, null);
    state.lastMove = state.prefix.at(-1) || null;
    state.selected = null;
    state.analysis = null;
    state.arrowSelection = null;
    state.positionReady = true;
    clearAnalysisDisplay();
    renderAll();
    queueAutoAnalysis();
  }

  async function refreshPrefix(requestedPrefix = state.prefix, requestedSan = state.prefixSan) {
    state.prefixAbort?.abort();
    state.positionReady = false;
    cancelAutoAnalysis(true);
    state.pvAbort?.abort();
    const controller = new AbortController();
    state.prefixAbort = controller;
    const sequence = ++state.prefixSequence;
    setBoardBusy(true);
    try {
      const payload = await requestJson("/api/prefix", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({ ...boundaryPayload(), prefix: requestedPrefix }),
      });
      if (sequence !== state.prefixSequence) return null;
      applyPrefixPayload(payload, requestedPrefix, requestedSan);
      return payload;
    } catch (error) {
      if (error.name === "AbortError") return null;
      const message = `Position error: ${displayError(error)}`;
      showToast(message);
      if (!state.prefix.length) {
        state.boardFen = state.boundary.fen;
        state.legalMoves = [];
        renderAll();
      }
      dom.boundary_notice.className = "boundary-notice is-game-over";
      dom.boundary_notice_text.textContent = message;
      return null;
    } finally {
      if (sequence === state.prefixSequence) {
        setBoardBusy(false);
        queueAutoAnalysis();
      }
    }
  }

  function squareName(file, rank) {
    return `${FILES[file]}${rank + 1}`;
  }

  function squareCoordinates(square) {
    const file = FILES.indexOf(square[0]);
    const rank = Number(square[1]) - 1;
    const displayFile = state.flipped ? 7 - file : file;
    const displayRank = state.flipped ? rank : 7 - rank;
    return {
      x: displayFile * 12.5 + 6.25,
      y: displayRank * 12.5 + 6.25,
    };
  }

  function currentLegalSources() {
    return new Set(state.legalMoves.map((move) => move.from));
  }

  function renderBoard() {
    const boardHadFocus = dom.board.contains(document.activeElement);
    const previewing = state.previewIndex !== null;
    const timeline = state.mode === "play" ? playTimeline() : [];
    const review = state.mode === "play" ? playReviewPosition(timeline) : null;
    const reviewing = Boolean(review);
    const interactive = !previewing && !reviewing && boardInputAllowed();
    const { pieces } = parseFen(activeBoardFen());
    const sources = interactive ? currentLegalSources() : new Set();
    const destinations = new Set(
      state.selected && !previewing
        ? state.legalMoves.filter((move) => move.from === state.selected).map((move) => move.to)
        : [],
    );
    const rankOrder = state.flipped ? [0, 1, 2, 3, 4, 5, 6, 7] : [7, 6, 5, 4, 3, 2, 1, 0];
    const fileOrder = state.flipped ? [7, 6, 5, 4, 3, 2, 1, 0] : [0, 1, 2, 3, 4, 5, 6, 7];
    const visibleLastMove = review?.lastMove || state.lastMove;
    const lastFrom = previewing ? null : visibleLastMove?.slice(0, 2);
    const lastTo = previewing ? null : visibleLastMove?.slice(2, 4);
    const fragment = document.createDocumentFragment();

    rankOrder.forEach((rank, rowIndex) => {
      fileOrder.forEach((file, columnIndex) => {
        const name = squareName(file, rank);
        const piece = pieces.get(name);
        const button = document.createElement("button");
        const light = (file + rank) % 2 === 1;
        button.type = "button";
        button.className = `square ${light ? "light" : "dark"}`;
        button.dataset.square = name;
        button.tabIndex = interactive && name === state.focusSquare ? 0 : -1;
        button.setAttribute("aria-disabled", String(!interactive));
        if (piece) button.classList.add("has-piece");
        if (sources.has(name)) button.classList.add("is-legal-from");
        if (name === state.selected) button.classList.add("is-selected");
        if (name === lastFrom || name === lastTo) button.classList.add("is-last");
        if (destinations.has(name)) {
          button.classList.add("is-legal");
          if (piece) button.classList.add("is-capture");
        }
        const contents = piece ? `${piece.color} ${PIECE_NAMES[piece.type]}` : "empty square";
        const action = destinations.has(name) ? ", legal destination" : sources.has(name) ? ", movable" : "";
        button.setAttribute("aria-label", `${name}, ${contents}${action}`);
        if (piece) {
          const image = document.createElement("img");
          image.className = `piece ${piece.color}`;
          image.src = pieceAsset(piece);
          image.alt = "";
          image.draggable = false;
          image.setAttribute("aria-hidden", "true");
          button.append(image);
        }
        if (rowIndex === 7) {
          const coordinate = document.createElement("span");
          coordinate.className = "coordinate file";
          coordinate.textContent = FILES[file];
          coordinate.setAttribute("aria-hidden", "true");
          button.append(coordinate);
        }
        if (columnIndex === 0) {
          const coordinate = document.createElement("span");
          coordinate.className = "coordinate rank";
          coordinate.textContent = String(rank + 1);
          coordinate.setAttribute("aria-hidden", "true");
          button.append(coordinate);
        }
        fragment.append(button);
      });
    });
    dom.board.replaceChildren(fragment);
    dom.board.setAttribute(
      "aria-label",
      previewing
        ? `Principal variation preview, series ${state.previewIndex + 1} of ${state.pvFrames.length}. ${state.flipped ? "Black" : "White"} pieces at the bottom.`
        : reviewing
          ? `Game history review, position ${review.index + 1} of ${timeline.length}, after ${review.lastSan || "the initial setup"}. ${state.flipped ? "Black" : "White"} pieces at the bottom. Input is locked.`
        : `Chess board. ${state.flipped ? "Black" : "White"} pieces at the bottom.${interactive ? " Ready for input." : " Input is locked."}`,
    );
    dom.board_shell.classList.toggle("is-previewing", previewing);
    dom.board_shell.classList.toggle("is-reviewing", reviewing);
    if (boardHadFocus) {
      dom.board.querySelector(`[data-square="${state.focusSquare}"]`)?.focus({ preventScroll: true });
    }
    renderArrows();
  }

  function extractUci(value) {
    if (!value) return null;
    if (Array.isArray(value)) {
      for (const item of value) {
        const match = extractUci(item);
        if (match) return match;
      }
      return null;
    }
    if (typeof value === "object") {
      return extractUci(first(value.uci, value.moves, value.series, value.best_series, value.line));
    }
    return String(value).match(/[a-h][1-8][a-h][1-8][qrbn]?/i)?.[0]?.toLowerCase() || null;
  }

  function analysisAlternatives(result = state.analysis) {
    const alternatives = first(result?.alternatives, result?.lines, result?.candidate_series, []);
    return Array.isArray(alternatives) ? alternatives : [];
  }

  function addArrow(uci, color, marker, width, opacity) {
    if (!uci || uci.length < 4) return;
    const from = squareCoordinates(uci.slice(0, 2));
    const to = squareCoordinates(uci.slice(2, 4));
    if (![from.x, from.y, to.x, to.y].every(Number.isFinite)) return;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    const length = Math.hypot(dx, dy) || 1;
    const shorten = marker === "arrow-best" ? 4.1 : 3.8;
    line.setAttribute("x1", String(from.x + (dx / length) * 1.65));
    line.setAttribute("y1", String(from.y + (dy / length) * 1.65));
    line.setAttribute("x2", String(to.x - (dx / length) * shorten));
    line.setAttribute("y2", String(to.y - (dy / length) * shorten));
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", String(width));
    line.setAttribute("stroke-opacity", String(opacity));
    line.setAttribute("marker-end", `url(#${marker})`);
    line.classList.add(marker === "arrow-best" ? "is-best" : "is-alternative");
    dom.board_arrows.append(line);
  }

  function renderArrows() {
    [...dom.board_arrows.querySelectorAll("line")].forEach((line) => line.remove());
    if (!state.analysis || state.previewIndex !== null) return;
    const engineBest = extractUci(first(state.analysis.best_completion, state.analysis.best_series));
    const best = state.arrowSelection || engineBest;
    const candidates = [engineBest, ...analysisAlternatives().map((alternative) => (
      extractUci(first(alternative.next_move_uci, alternative.completion, alternative))
    ))]
      .filter(Boolean)
      .filter((uci, index, values) => uci !== best && values.indexOf(uci) === index)
      .slice(0, 2);
    [...candidates].reverse().forEach((uci) => addArrow(uci, "#637179", "arrow-alt", 1.15, 0.5));
    if (best) addArrow(best, "#81b64c", "arrow-best", 1.8, 0.88);
  }

  function renderSeriesLedger() {
    const review = state.mode === "play" ? playReviewPosition() : null;
    const prefix = review?.prefix || state.prefix;
    const prefixSan = review?.prefixSan || state.prefixSan;
    if (!prefix.length) {
      const empty = document.createElement("span");
      empty.className = "empty-chip";
      empty.textContent = "No moves yet";
      dom.move_chips.replaceChildren(empty);
      return;
    }
    const nodes = prefix.map((uci, index) => {
      const chip = document.createElement("span");
      chip.className = "move-chip";
      const number = document.createElement("b");
      number.textContent = String(index + 1);
      chip.append(number, document.createTextNode(prefixSan[index] || uci));
      chip.title = uci;
      return chip;
    });
    dom.move_chips.replaceChildren(...nodes);
  }

  function qualityTone(label) {
    return ({
      Best: "best",
      Excellent: "excellent",
      Good: "good",
      Inaccuracy: "inaccuracy",
      Mistake: "mistake",
      Blunder: "blunder",
    })[label] || "unrated";
  }

  function analysisSnapshot(result) {
    const meta = proofMetadata(result);
    const score = Number(first(result.score, result.evaluation?.score, result.value));
    const alternatives = analysisAlternatives(result).slice(0, 32).map((candidate) => ({
      series: seriesMoves(first(candidate.full_series, candidate.series, candidate.moves, candidate.uci, candidate.line)),
      score: Number(alternativeScore(candidate)),
    }));
    return {
      bestSeries: seriesMoves(first(result.best_full_series, result.best_series)),
      score: Number.isFinite(score) ? score : null,
      alternatives: alternatives.filter((candidate) => candidate.series.length && Number.isFinite(candidate.score)),
      proof: {
        exact: meta.exact,
        timedOut: meta.timedOut,
        requested: asNumber(meta.requested, 0),
        completed: asNumber(meta.completed, 0),
        reach: meta.reach,
        workLimitReached: asBoolean(result.work_limit_reached) === true,
      },
      createdAt: new Date().toISOString(),
    };
  }

  function ratingEvidence(snapshot) {
    const proof = snapshot?.proof || {};
    if (proof.timedOut !== false) return "Search timed out or timeout evidence is missing";
    if (proof.exact !== true) return "Search width was selective";
    if (proof.completed < 2 || proof.completed < proof.requested) return "Search was too shallow or incomplete";
    if (proof.workLimitReached) return "Deterministic work limit was reached";
    return null;
  }

  function sameSeries(left, right) {
    return left.length === right.length && left.every((move, index) => move === right[index]);
  }

  function qualityForCompletedNode(node) {
    if (!node?.complete) return null;
    const snapshot = state.study?.analyses?.[boundaryKey(node.boundary)];
    if (!snapshot) return { label: "Not rated", tone: "unrated", reason: "Run Strong analysis from this series boundary first" };
    const proofProblem = ratingEvidence(snapshot);
    if (proofProblem) return { label: "Not rated", tone: "unrated", reason: proofProblem };
    const played = node.prefix;
    const bestScore = Number(snapshot.score);
    if (!Number.isFinite(bestScore) || !snapshot.bestSeries?.length) {
      return { label: "Not rated", tone: "unrated", reason: "Comparable candidate scores were not returned" };
    }
    let playedScore = sameSeries(played, snapshot.bestSeries) ? bestScore : null;
    if (playedScore === null) {
      const candidate = (snapshot.alternatives || []).find((item) => sameSeries(played, item.series));
      if (candidate && Number.isFinite(Number(candidate.score))) playedScore = Number(candidate.score);
    }
    if (playedScore === null) {
      return { label: "Not rated", tone: "unrated", reason: "This series was outside the returned scored candidates" };
    }
    const moverIsWhite = node.series % 2 === 1;
    const loss = Math.max(0, moverIsWhite ? bestScore - playedScore : playedScore - bestScore);
    const label = sameSeries(played, snapshot.bestSeries)
      ? "Best"
      : loss <= 35 ? "Excellent"
        : loss <= 100 ? "Good"
          : loss <= 250 ? "Inaccuracy"
            : loss <= 600 ? "Mistake"
              : "Blunder";
    return {
      label,
      tone: qualityTone(label),
      reason: `${EVALUATION.loss(loss)} against the best returned complete series`,
    };
  }

  function refreshStudyQualities(boundary = null) {
    if (!state.study) return;
    const key = boundary ? boundaryKey(boundary) : null;
    Object.values(state.study.nodes).forEach((node) => {
      if (node.complete && (!key || boundaryKey(node.boundary) === key)) node.quality = qualityForCompletedNode(node);
    });
  }

  function recordAnalysis(result) {
    if (!state.study) return;
    const node = state.currentTreeNodeId ? state.study.nodes[state.currentTreeNodeId] : null;
    const verdict = result.move_quality;
    if (node && verdict && sameSeries(node.prefix, result.fixed_prefix || [])) {
      node.quality = {
        label: String(first(verdict.label, "Not rated")),
        tone: qualityTone(first(verdict.label, "Not rated")),
        reason: Array.isArray(verdict.reasons) && verdict.reasons.length
          ? verdict.reasons.map(humanize).join(" · ")
          : verdict.rated
            ? `${EVALUATION.loss(asNumber(verdict.score?.effective_loss, 0))} for this micro-move`
            : "Comparable engine evidence was not available",
      };
      persistStudy();
      renderStudyTree();
    }
    if (result.analysis_scope === "series-prefix") {
      return;
    }
    const target = state.complete && state.nextState ? state.nextState : state.boundary;
    const key = boundaryKey(target);
    state.study.analyses[key] = analysisSnapshot(result);
    const entries = Object.entries(state.study.analyses);
    if (entries.length > 32) delete state.study.analyses[entries.sort((a, b) => String(a[1]?.createdAt).localeCompare(String(b[1]?.createdAt)))[0][0]];
    refreshStudyQualities(target);
    persistStudy();
    renderStudyTree();
  }

  function treeMoveButton(node, level) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tree-move";
    button.dataset.nodeId = node.id;
    button.setAttribute("role", "treeitem");
    button.setAttribute("aria-level", String(level));
    button.setAttribute("aria-selected", String(node.id === state.currentTreeNodeId));
    if (node.id === state.currentTreeNodeId) button.classList.add("is-current");
    const index = document.createElement("span");
    index.className = "tree-micro-index";
    index.textContent = String(node.micro);
    const move = document.createElement("strong");
    move.textContent = node.san || node.uci;
    const badge = document.createElement("span");
    const quality = node.quality || (node.complete ? qualityForCompletedNode(node) : null);
    if (quality) {
      node.quality = quality;
      badge.className = `move-quality is-${quality.tone}`;
      badge.textContent = quality.label;
      badge.title = quality.reason;
    } else {
      badge.className = "tree-continue";
      badge.textContent = `${Math.max(0, node.series - node.micro)} left`;
      badge.setAttribute("aria-label", `${Math.max(0, node.series - node.micro)} moves left in series`);
    }
    button.title = `${node.uci} · ${node.validated ? "server checked" : "select to recheck on the server"}${quality ? ` · ${quality.reason}` : ""}`;
    button.append(index, move, badge);
    button.addEventListener("click", () => navigateToTreeNode(node.id));
    return button;
  }

  function appendTreeBranch(parentId, container, level, visited = new Set()) {
    treeChildren(parentId).forEach((node) => {
      if (visited.has(node.id)) return;
      const branchVisited = new Set(visited);
      branchVisited.add(node.id);
      const branch = document.createElement("div");
      branch.className = `tree-branch ${node.micro === 1 ? "starts-series" : "continues-series"}`;
      if (node.micro === 1) {
        const groupLabel = document.createElement("div");
        groupLabel.className = "tree-series-label";
        groupLabel.textContent = `Series ${node.series} · ${node.series % 2 === 1 ? "White" : "Black"} · ${node.series} move${node.series === 1 ? "" : "s"}`;
        branch.append(groupLabel);
      }
      branch.append(treeMoveButton(node, level));
      const children = treeChildren(node.id);
      if (children.length) {
        const group = document.createElement("div");
        group.className = children.length > 1 ? "tree-children has-variations" : "tree-children";
        group.setAttribute("role", "group");
        appendTreeBranch(node.id, group, level + 1, branchVisited);
        branch.append(group);
      }
      container.append(branch);
    });
  }

  function renderStudyTree() {
    if (!dom.analysis_tree || !state.study) return;
    const root = document.createElement("button");
    root.type = "button";
    root.className = "tree-root";
    root.setAttribute("role", "treeitem");
    root.setAttribute("aria-level", "1");
    const atRoot = state.currentTreeNodeId === null && state.prefix.length === 0
      && boundaryKey(state.boundary) === boundaryKey(state.study.rootBoundary);
    root.setAttribute("aria-selected", String(atRoot));
    if (atRoot) root.classList.add("is-current");
    root.innerHTML = "<span aria-hidden=\"true\">◆</span><strong>Study start</strong><small>Exact boundary</small>";
    root.addEventListener("click", () => navigateToTreeNode(null));
    const group = document.createElement("div");
    group.className = "tree-root-children";
    group.setAttribute("role", "group");
    appendTreeBranch(null, group, 2);
    if (!group.childElementCount) {
      const empty = document.createElement("p");
      empty.className = "tree-empty";
      empty.textContent = "No variations yet — make a legal move on the board.";
      group.append(empty);
    }
    dom.analysis_tree.replaceChildren(root, group);
    dom.delete_variation.disabled = !state.currentTreeNodeId;
    dom.new_variation.classList.toggle("is-active", state.branching);
    dom.new_variation.setAttribute("aria-pressed", String(state.branching));
  }

  function pathToTreeNode(nodeId) {
    if (!nodeId || !state.study?.nodes[nodeId]) return [];
    const path = [];
    const seen = new Set();
    let cursor = state.study.nodes[nodeId];
    while (cursor && path.length < MAX_STORED_NODES && !seen.has(cursor.id)) {
      path.unshift(cursor);
      seen.add(cursor.id);
      cursor = cursor.parentId ? state.study.nodes[cursor.parentId] : null;
    }
    if (cursor || (path[0]?.parentId && !state.study.nodes[path[0].parentId])) throw new Error("Saved variation has a broken parent chain");
    return path;
  }

  async function canonicalReplayToNode(nodeId) {
    const rootBoundary = cloneBoundary(state.study.rootBoundary);
    const path = pathToTreeNode(nodeId);
    let boundary = rootBoundary;
    let prefix = [];
    let prefixSan = [];
    let seriesParentId = null;
    let lastPayload = null;
    const history = [];
    if (!path.length) {
      lastPayload = await requestJson("/api/prefix", {
        method: "POST",
        body: JSON.stringify({ ...rootBoundary, progressive_ep: [...rootBoundary.ep_targets], prefix: [] }),
      });
      return { boundary: rootBoundary, prefix, prefixSan, seriesParentId, history, payload: lastPayload };
    }
    for (let index = 0; index < path.length; index += 1) {
      const node = path[index];
      if (lastPayload?.outcome) throw new Error("Saved line continues after the game ended");
      if (lastPayload?.complete) {
        const next = normalizeNextState(lastPayload.next_state);
        if (!next) throw new Error("Saved line has no trusted next-series boundary");
        history.push({
          boundary: cloneBoundary(boundary),
          prefix: [...prefix],
          prefixSan: [...prefixSan],
          treeNodeId: path[index - 1]?.id || null,
          seriesParentNodeId: seriesParentId,
        });
        boundary = next;
        prefix = [];
        prefixSan = [];
        seriesParentId = path[index - 1]?.id || seriesParentId;
      }
      prefix = [...prefix, node.uci];
      lastPayload = await requestJson("/api/prefix", {
        method: "POST",
        body: JSON.stringify({ ...boundary, progressive_ep: [...boundary.ep_targets], prefix }),
      });
      prefix = [...lastPayload.prefix];
      prefixSan = notationArray(lastPayload, prefix, prefixSan);
      node.boundary = cloneBoundary(boundary);
      node.prefix = [...prefix];
      node.san = prefixSan.at(-1) || node.uci;
      node.series = boundary.series;
      node.micro = prefix.length;
      node.seriesParentId = seriesParentId;
      node.complete = Boolean(lastPayload.complete);
      node.validated = true;
      if (node.complete && !node.quality) node.quality = qualityForCompletedNode(node);
    }
    return { boundary, prefix, prefixSan, seriesParentId, history, payload: lastPayload };
  }

  async function navigateToTreeNode(nodeId) {
    exitPvPreview(false);
    state.prefixAbort?.abort();
    state.positionReady = false;
    cancelAutoAnalysis(true);
    state.pvAbort?.abort();
    const sequence = ++state.prefixSequence;
    setBoardBusy(true);
    try {
      const replayed = await canonicalReplayToNode(nodeId);
      if (sequence !== state.prefixSequence) return;
      state.boundary = cloneBoundary(replayed.boundary);
      state.currentTreeNodeId = nodeId;
      state.seriesParentNodeId = replayed.seriesParentId;
      state.history = [...replayed.history];
      state.branching = false;
      state.viewingHistorical = true;
      state.handoffNotice = null;
      applyPrefixPayload(replayed.payload, replayed.prefix, replayed.prefixSan);
      persistStudy();
      renderStudyTree();
      showToast(nodeId ? "Returned to the server-checked move" : "Returned to the study start");
      return true;
    } catch (error) {
      showToast(`Saved line rejected: ${displayError(error)}`);
      return false;
    } finally {
      if (sequence === state.prefixSequence) {
        setBoardBusy(false);
        queueAutoAnalysis();
      }
    }
  }

  function attachMoveToStudy(move, payload, boundaryAtMove, parentId, seriesParentId) {
    if (!state.study || !payload) return;
    const canonicalPrefix = Array.isArray(payload.prefix) ? payload.prefix.map(String) : [];
    const canonicalSan = notationArray(payload, canonicalPrefix, state.prefixSan);
    let node = treeChildren(parentId).find((candidate) => (
      candidate.uci === move.uci
      && boundaryKey(candidate.boundary) === boundaryKey(boundaryAtMove)
      && candidate.prefix.length === canonicalPrefix.length
    ));
    if (!node) {
      if (Object.keys(state.study.nodes).length >= MAX_STORED_NODES) {
        showToast("This local study reached its 800-move safety limit");
        return;
      }
      const id = createId();
      node = {
        id,
        parentId,
        seriesParentId,
        uci: move.uci,
        san: canonicalSan.at(-1) || move.san || move.uci,
        boundary: cloneBoundary(boundaryAtMove),
        prefix: canonicalPrefix,
        series: boundaryAtMove.series,
        micro: canonicalPrefix.length,
        complete: Boolean(payload.complete),
        validated: true,
        quality: null,
        createdAt: new Date().toISOString(),
      };
      state.study.nodes[id] = node;
    } else {
      node.san = canonicalSan.at(-1) || node.san;
      node.prefix = canonicalPrefix;
      node.complete = Boolean(payload.complete);
      node.validated = true;
    }
    if (node.complete && !node.quality) node.quality = qualityForCompletedNode(node);
    state.currentTreeNodeId = node.id;
    state.branching = false;
    persistStudy();
    renderStudyTree();
  }

  function renderPositionStatus() {
    const review = state.mode === "play" ? playReviewPosition() : null;
    const series = review?.series || state.boundary.series;
    const movesRemaining = review?.movesRemaining ?? state.movesRemaining;
    const side = series % 2 === 1 ? "White" : "Black";
    dom.series_number.textContent = String(series);
    dom.turn_label.textContent = `${side} · Series ${series}`;
    dom.moves_heading.textContent = `${movesRemaining} move${movesRemaining === 1 ? "" : "s"} remaining`;
    const gameOver = state.mode === "play" ? playGameEnded() : Boolean(state.outcome);
    dom.undo_move.disabled = state.mode === "play" || (state.prefix.length === 0 && state.history.length === 0);
    dom.reset_series.disabled = state.mode === "play" || state.prefix.length === 0;
    dom.advance_series.hidden = !(state.complete && state.nextState && !gameOver && state.viewingHistorical);

    dom.boundary_pill.className = "boundary-pill";
    dom.boundary_notice.className = "boundary-notice";
    if (review) {
      dom.boundary_pill.classList.add("is-mid-series");
      dom.boundary_pill.textContent = "Review";
      dom.boundary_notice.classList.add("is-warning");
      dom.boundary_notice_text.textContent = `Reviewing position ${review.index + 1} of ${review.totalPositions}. Return to the latest position to play.`;
      dom.moves_heading.textContent = review.seriesMove
        ? `Move ${review.seriesMove} of Series ${review.series}`
        : `Before Series ${review.series}`;
      dom.series_status.textContent = review.lastSan
        ? `Last move: ${review.lastSan}. Board input and engine replies are paused.`
        : "Initial position. Board input and engine replies are paused.";
      updateAnalysisProgress();
      return;
    }
    if (gameOver) {
      dom.boundary_pill.classList.add("is-mid-series");
      dom.boundary_pill.textContent = "Game over";
      dom.boundary_notice.classList.add("is-game-over");
      dom.boundary_notice_text.textContent = state.play.resigned && state.mode === "play"
        ? "You resigned. Start a new game whenever you're ready."
        : state.mode === "play"
          ? `${humanize(state.outcome)} ends the game.`
          : `${humanize(state.outcome)} ends the game. Undo or select an earlier tree move to continue studying.`;
      dom.moves_heading.textContent = state.play.resigned && state.mode === "play" ? "Resigned" : humanize(state.outcome);
      dom.series_status.textContent = `Final series: ${state.prefixSan.join(" / ") || "no legal move"}.`;
    } else if (state.complete) {
      dom.boundary_pill.classList.add("is-complete");
      dom.boundary_pill.textContent = "Completed series";
      dom.boundary_notice.classList.add("is-complete");
      dom.boundary_notice_text.textContent = state.viewingHistorical
        ? "Historical completed series. Continue from here to open its trusted next boundary."
        : "The series is complete and will advance automatically.";
      dom.series_status.textContent = state.outcome
        ? `Series ended: ${humanize(state.outcome)}.`
        : state.check ? "The series ended immediately by check." : "The complete series is ready.";
      if (state.unusedMoves > 0) {
        dom.moves_heading.textContent = `${state.unusedMoves} unused move${state.unusedMoves === 1 ? "" : "s"} forfeited`;
      }
    } else if (state.prefix.length > 0) {
      dom.boundary_pill.classList.add("is-mid-series");
      dom.boundary_pill.textContent = "Mid-series";
      dom.boundary_notice.classList.add("is-warning");
      dom.boundary_notice_text.textContent = state.mode === "play"
        ? `${side} keeps moving until the series is complete or gives check.`
        : "Keep playing the same side. Automatic analysis is searching only legal completions of this prefix.";
      dom.series_status.textContent = state.mode === "play"
        ? `${state.prefix.length} of ${state.boundary.series} micro-moves played.`
        : "Every micro-move is retained in the local move tree.";
    } else {
      dom.boundary_pill.classList.add("is-boundary");
      dom.boundary_pill.textContent = "Exact boundary";
      dom.boundary_notice.classList.add("is-ready");
      dom.boundary_notice_text.textContent = state.handoffNotice || (state.mode === "play"
        ? (seriesColor() === state.play.humanColor ? "Your series. Play on the board." : "The champion is choosing a complete series.")
        : "Play on the board; automatic analysis is already running.");
      dom.series_status.textContent = state.handoffNotice || `Play ${state.boundary.series} legal move${state.boundary.series === 1 ? "" : "s"}.`;
    }
    updateAnalysisProgress();
  }

  function renderAll() {
    renderBoard();
    renderSeriesLedger();
    renderPositionStatus();
    renderStudyTree();
    syncSetupFields();
    renderPlaySurface();
  }

  function setPlayerColor(node, color) {
    node.className = `player-color is-${color}`;
  }

  function renderPlayHistory() {
    if (!dom.play_history) return;
    const review = playReviewPosition();
    const rows = state.history.map((entry) => ({
      series: entry.boundary.series,
      side: seriesColor(entry.boundary.series),
      moves: [...(entry.prefixSan || entry.prefix || [])],
      complete: true,
    }));
    if (state.prefix.length || state.outcome) {
      rows.push({
        series: state.boundary.series,
        side: seriesColor(),
        moves: [...(state.prefixSan.length ? state.prefixSan : state.prefix)],
        complete: Boolean(state.complete || state.outcome),
      });
    }
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "play-history-empty";
      empty.textContent = "The game starts here.";
      dom.play_history.replaceChildren(empty);
      return;
    }
    dom.play_history.replaceChildren(...rows.map((entry) => {
      const row = document.createElement("div");
      const human = entry.side === state.play.humanColor;
      row.className = `play-history-row ${human ? "is-human" : "is-engine"}`;
      if (review?.series === entry.series) {
        row.classList.add("is-current");
        row.setAttribute("aria-current", "step");
      }
      const label = document.createElement("span");
      label.className = "play-history-series";
      label.textContent = `S${entry.series}`;
      const copy = document.createElement("span");
      copy.className = "play-history-moves";
      const title = document.createElement("strong");
      title.textContent = human ? "You" : "Champion";
      const moves = document.createElement("small");
      moves.textContent = entry.moves.length ? entry.moves.join(" / ") : "No legal move";
      copy.append(title, moves);
      const color = document.createElement("span");
      color.className = `history-color is-${entry.side}`;
      color.textContent = entry.side === "white" ? "W" : "B";
      row.append(label, copy, color);
      return row;
    }));
  }

  function renderPlayNavigation(timeline = playTimeline()) {
    const cursor = playTimelineCursor(timeline);
    const busy = state.play.animating;
    dom.play_history_previous.disabled = busy || cursor <= 0;
    dom.play_history_next.disabled = busy || cursor >= timeline.length - 1;
    dom.play_history_position.textContent = `Position ${cursor + 1} of ${timeline.length}`;
    dom.play_history_previous.title = "Previous move (Left Arrow)";
    dom.play_history_next.title = "Next move (Right Arrow)";
  }

  function playOutcomeStatus() {
    if (state.play.resigned) {
      return { title: "You resigned", detail: "Start a new game to play again.", kind: "finished" };
    }
    if (!state.outcome) return null;
    const outcome = String(state.outcome);
    if (outcome === "checkmate") {
      const winner = seriesColor();
      return {
        title: winner === state.play.humanColor ? "You won" : "Champion won",
        detail: `${winner === "white" ? "White" : "Black"} delivered checkmate in Series ${state.boundary.series}.`,
        kind: winner === state.play.humanColor ? "won" : "finished",
      };
    }
    if (outcome.includes("draw") || outcome === "stalemate") {
      return { title: "Draw", detail: `${humanize(outcome)} ended the game.`, kind: "finished" };
    }
    return { title: "Game over", detail: `${humanize(outcome)} ended the game.`, kind: "finished" };
  }

  function renderPlaySurface() {
    if (!dom.workspace) return;
    const playMode = state.mode === "play";
    document.querySelector(".board-pane")?.setAttribute("aria-label", playMode ? "Game board" : "Analysis board");
    document.querySelector(".inspector")?.setAttribute("aria-label", playMode ? "Game controls" : "Analysis tools");
    dom.workspace.classList.toggle("is-play-mode", playMode);
    dom.workspace.classList.toggle("is-analyze-mode", !playMode);
    dom.mode_play.classList.toggle("is-active", playMode);
    dom.mode_analyze.classList.toggle("is-active", !playMode);
    dom.mode_play.disabled = state.positionBusy;
    dom.mode_analyze.disabled = state.positionBusy;
    dom.mode_play.setAttribute("aria-pressed", String(playMode));
    dom.mode_analyze.setAttribute("aria-pressed", String(!playMode));
    dom.play_panel.hidden = !playMode;
    dom.play_top_player.hidden = !playMode;
    dom.play_bottom_player.hidden = !playMode;
    dom.workspace_tabs.hidden = playMode;
    if (playMode) {
      document.querySelectorAll(".tab-panel").forEach((panel) => { panel.hidden = true; });
    } else {
      return;
    }

    const humanColor = state.play.humanColor;
    const engineColor = humanColor === "white" ? "black" : "white";
    const timeline = playTimeline();
    const review = playReviewPosition(timeline);
    const reviewing = Boolean(review);
    const recoveryBlocked = Boolean(state.play.error) && !state.positionReady;
    const saveBlocked = playSessionSaveBlocked;
    const effectiveGameEnded = playGameEnded() && !recoveryBlocked && !saveBlocked;
    const activeColor = review?.side || seriesColor();
    setPlayerColor(dom.play_top_color, engineColor);
    setPlayerColor(dom.play_bottom_color, humanColor);
    dom.play_top_player.classList.toggle("is-active", !reviewing && activeColor === engineColor && !effectiveGameEnded);
    dom.play_bottom_player.classList.toggle("is-active", !reviewing && activeColor === humanColor && !effectiveGameEnded);
    dom.play_top_name.textContent = "Current champion";
    dom.play_top_meta.textContent = `${state.play.engineName} · ${engineColor === "white" ? "White" : "Black"}`;
    dom.play_bottom_meta.textContent = humanColor === "white" ? "White" : "Black";
    dom.play_top_turn.textContent = reviewing
      ? "Review"
      : recoveryBlocked
      ? "Restore paused"
      : saveBlocked
      ? "Save paused"
      : effectiveGameEnded
      ? "Game over"
      : activeColor === engineColor
        ? state.play.thinking ? "Thinking…" : "To move"
        : "Waiting";
    dom.play_bottom_turn.textContent = reviewing
      ? "Review"
      : recoveryBlocked
      ? "Restore paused"
      : saveBlocked
      ? "Save paused"
      : effectiveGameEnded
      ? "Game over"
      : activeColor === humanColor ? "Your series" : "Waiting";

    dom.play_as_white.classList.toggle("is-active", humanColor === "white");
    dom.play_as_black.classList.toggle("is-active", humanColor === "black");
    dom.play_as_white.setAttribute("aria-pressed", String(humanColor === "white"));
    dom.play_as_black.setAttribute("aria-pressed", String(humanColor === "black"));
    dom.play_new_game.textContent = `New game as ${humanColor === "white" ? "White" : "Black"}`;
    dom.play_resign.disabled = reviewing || recoveryBlocked || saveBlocked || !state.play.active || effectiveGameEnded;
    dom.play_analyze_position.disabled = reviewing || saveBlocked || !state.positionReady || state.positionBusy || state.play.thinking || state.play.animating;
    const retryableEngineTurn = (Boolean(state.play.error) || saveBlocked)
      && !reviewing
      && state.play.active
      && (saveBlocked || recoveryBlocked || (!effectiveGameEnded && seriesColor() !== humanColor));
    dom.play_retry_engine.hidden = !retryableEngineTurn;
    dom.play_retry_engine.disabled = !retryableEngineTurn || state.play.thinking || state.play.animating;
    dom.play_retry_engine.textContent = saveBlocked
      ? "Retry saving game"
      : state.positionReady
      ? "Retry engine move"
      : "Retry saved game";

    const outcome = reviewing || recoveryBlocked || saveBlocked ? null : playOutcomeStatus();
    let status = reviewing ? {
      title: "Reviewing game",
      detail: review.lastSan
        ? `Position ${review.index + 1} of ${timeline.length} · after ${review.lastSan}. Return to the latest position to continue.`
        : `Position 1 of ${timeline.length} · initial setup. Return to the latest position to continue.`,
      kind: "review",
    } : outcome;
    if (!status && playSessionExternalUpdate) {
      status = {
        title: "Game updated in another tab",
        detail: "Reload this tab to continue from the newest saved position.",
        kind: "warning",
      };
    } else if (!status && saveBlocked) {
      status = {
        title: "Game not saved yet",
        detail: "Browser storage rejected the latest position. Keep this tab open and retry saving before continuing.",
        kind: "warning",
      };
    } else if (!status && state.play.error) {
      status = {
        title: playSessionLastWriteDurable
          ? "Search stopped — game saved"
          : "Search stopped — reload may lose this game",
        detail: recoveryBlocked
          ? `${state.play.error} Retry validation when the engine is available; you do not need to restart.`
          : playSessionLastWriteDurable
            ? `${state.play.error} — Retry this engine move or switch strength; you do not need to restart.`
            : `${state.play.error} Browser storage rejected the latest save. Keep this tab open, then retry the engine move.`,
        kind: "warning",
      };
    } else if (!status && (state.play.thinking || state.play.animating)) {
      const search = state.play.activeSearch || playSearchLimits();
      status = {
        title: "Champion is thinking",
        detail: state.play.animating
          ? `Playing an engine-validated Series ${state.boundary.series}.`
          : state.play.activeSearchRuntime === "browser-wasm"
            ? state.play.browserRootReady
              ? `Searching locally · WASM · ${state.play.browserRootWorkerCount} certified single-thread Workers. Requested depth ${search.depth}, up to ${search.seconds.toLocaleString()}s.`
              : `Searching locally · WASM · ${state.play.nativeThreads} thread${state.play.nativeThreads === 1 ? "" : "s"}. Requested depth ${search.depth}, up to ${search.seconds.toLocaleString()}s.`
            : `${search.label} hosted search requested depth ${search.depth}, up to ${search.seconds.toLocaleString()}s.`,
        kind: "thinking",
      };
    } else if (!status && activeColor === humanColor) {
      status = {
        title: "Your series",
        detail: `Play ${state.movesRemaining} more legal move${state.movesRemaining === 1 ? "" : "s"} as ${humanColor === "white" ? "White" : "Black"}.`,
        kind: "ready",
      };
    } else if (!status) {
      status = { title: "Champion's series", detail: "The engine reply starts automatically.", kind: "thinking" };
    }
    dom.play_live.className = `play-live is-${status.kind}`;
    dom.play_status_title.textContent = status.title;
    dom.play_status_detail.textContent = status.detail;
    const visibleSeries = review?.series || state.boundary.series;
    const visibleRemaining = review?.movesRemaining ?? state.movesRemaining;
    dom.play_series_title.textContent = `Series ${visibleSeries}`;
    dom.play_series_count.textContent = String(visibleRemaining);
    dom.play_series_copy.textContent = reviewing
      ? review.seriesMove
        ? `Move ${review.seriesMove} of Series ${review.series}${review.complete ? " · series complete" : ""}`
        : `Position before Series ${review.series}`
      : recoveryBlocked
      ? "Saved game waiting for validation"
      : saveBlocked
      ? "Game waiting for a durable save"
      : effectiveGameEnded
      ? humanize(state.play.resigned ? "resigned" : state.outcome)
      : `${state.movesRemaining} move${state.movesRemaining === 1 ? "" : "s"} remaining`;
    dom.play_history_count.textContent = reviewing
      ? `Series ${review.series} · move ${review.seriesMove}`
      : `Series ${state.boundary.series}`;
    dom.play_engine_name.textContent = `Current champion · ${state.play.engineName}`;
    dom.play_engine_id.textContent = state.play.engineProfileId || "—";
    dom.play_engine_version.textContent = [state.play.engineVersion, state.play.engineFingerprint].filter(Boolean).join(" · ") || "—";
    dom.play_runtime_status.textContent = state.play.healthReady
      ? state.play.runtimeMode === "browser-wasm"
        ? state.play.browserRootReady
          ? `This device · ${state.play.runtimeCpuCount || "available"} logical processors · ${state.play.browserRootWorkerCount} certified single-thread Workers`
          : `This device · ${state.play.runtimeCpuCount || "available"} logical processors · ${state.play.nativeThreads} WebAssembly thread${state.play.nativeThreads === 1 ? "" : "s"}`
        : `${state.play.runtimeCpuCount === null ? "CPU allocation unavailable" : `${state.play.runtimeCpuCount} CPU allocated`} · ${state.play.nativeThreads} native search thread${state.play.nativeThreads === 1 ? "" : "s"}${state.play.nativeThreadsPolicy === "single-thread-pool-avoidance" ? " · host-safe mode" : ""}${state.play.browserWasmReason ? " · Continued safely on the hosted engine" : ""}`
      : "Preparing on-device WebAssembly…";
    renderPlaySearchEvidence();
    renderPlayHistory();
    renderPlayNavigation(timeline);
  }

  function engineSeriesFromAnalysis(payload) {
    const raw = first(payload.best_full_series, payload.best_series?.moves);
    if (!Array.isArray(raw)) return [];
    return raw.map(String).filter((move) => /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(move));
  }

  function pauseForEngineFrame() {
    return new Promise((resolve) => window.setTimeout(resolve, ENGINE_MOVE_ANIMATION_MS));
  }

  async function animateCheckedEngineSeries(payload, sequence) {
    const frames = Array.isArray(payload.frames) ? payload.frames : [];
    if (frames.length < 2 || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    state.play.animating = true;
    for (let index = 0; index < frames.length; index += 1) {
      if (sequence !== state.play.sequence || state.mode !== "play") return;
      const frame = frames[index];
      state.prefix = payload.prefix.slice(0, index + 1);
      state.prefixSan = payload.san.slice(0, index + 1);
      state.boardFen = String(frame.board_fen || state.boardFen);
      state.prefixFrames = prefixFramesFromPayload(
        { frames: frames.slice(0, index + 1) },
        state.prefix,
        state.prefixSan,
      );
      state.lastMove = String(frame.uci || state.prefix.at(-1) || "") || null;
      state.movesRemaining = Math.max(0, state.boundary.series - state.prefix.length);
      state.legalMoves = [];
      renderAll();
      await pauseForEngineFrame();
    }
  }

  async function requestEngineAnalysis(
    body,
    controller,
    sequence,
    searchDeadlineMs,
    receiptDeadlineMs,
  ) {
    let reconnectAttempt = 0;
    while (sequence === state.play.sequence && state.mode === "play") {
      try {
        return await requestJson("/api/analyze", {
          method: "POST",
          signal: controller.signal,
          analysisDeadlineMs: receiptDeadlineMs,
          analysisSearchDeadlineMs: searchDeadlineMs,
          body: JSON.stringify(body),
          onTransport: (runtime) => {
            if (sequence !== state.play.sequence || state.mode !== "play") return;
            state.play.activeSearchRuntime = runtime;
            renderPlaySearchEvidence();
          },
        });
      } catch (error) {
        if (error.name === "AbortError") throw error;
        if (error.status === 429) {
          dom.play_status_detail.textContent = "The engine is busy; your game is first in the retry queue.";
          await waitForRetry(AUTO_ANALYSIS_RETRY_MS, {
            signal: controller.signal,
            deadlineMs: searchDeadlineMs,
          });
          continue;
        }
        if (
          isPublicServiceWakeError(error)
          && reconnectAttempt < PUBLIC_ENGINE_RECONNECT_DELAYS_MS.length
        ) {
          dom.play_status_detail.textContent = "Reconnecting to the engine…";
          await waitForRetry(PUBLIC_ENGINE_RECONNECT_DELAYS_MS[reconnectAttempt], {
            signal: controller.signal,
            deadlineMs: searchDeadlineMs,
          });
          reconnectAttempt += 1;
          continue;
        }
        throw error;
      }
    }
    throw new DOMException("Engine turn cancelled", "AbortError");
  }

  async function maybeRunEngineTurn() {
    if (
      state.mode !== "play"
      || playSessionExternalUpdate
      || playSessionSaveBlocked
      || !state.play.active
      || state.play.thinking
      || state.play.animating
      || playReviewActive()
      || PLAY_HANDOFF.isActive()
      || state.positionBusy
      || !state.positionReady
      || playGameEnded()
      || state.complete
      || state.nextState
      || seriesColor() === state.play.humanColor
    ) return;
    const entrySequence = state.play.sequence;
    const search = playSearchLimits();
    const ponder = await claimMatchingPlayPonder(search);
    const engineTurnStillCurrent = !(
      state.mode !== "play"
      || state.play.sequence !== entrySequence
      || playSessionExternalUpdate
      || playSessionSaveBlocked
      || !state.play.active
      || state.play.thinking
      || state.play.animating
      || playReviewActive()
      || PLAY_HANDOFF.isActive()
      || state.positionBusy
      || !state.positionReady
      || playGameEnded()
      || state.complete
      || state.nextState
      || seriesColor() === state.play.humanColor
    );
    if (!engineTurnStillCurrent) {
      if (ponder) {
        ponder.controller.abort();
        await Promise.resolve(ponder.promise).catch(() => null);
        if (ponder && claimedPlayPonder === ponder) claimedPlayPonder = null;
      }
      return;
    }
    cancelAutoAnalysis(true);
    state.play.engineAbort?.abort();
    let controller = ponder?.controller || new AbortController();
    state.play.engineAbort = controller;
    const sequence = ++state.play.sequence;
    const key = playPositionKey();
    // Native work stops at the advertised search boundary. A separate receipt
    // deadline lets an already-completed result cross transport and replay
    // validation without silently granting the engine more thinking time.
    const searchDeadlineMs = monotonicNow() + search.seconds * 1000;
    const receiptDeadlineMs = searchDeadlineMs + PLAY_ANALYSIS_RESPONSE_GRACE_MS;
    let analysisReceiptDeadlineMs = receiptDeadlineMs;
    const analysisBody = {
      ...boundaryPayload(),
      prefix: [],
      depth: search.depth,
      max_series: search.maxSeries,
      time_limit: search.seconds,
      max_generation_positions: search.generationPositions,
      alternatives: 0,
      best_move_only: true,
      rate_move: false,
      save: false,
    };
    state.play.thinking = true;
    state.play.activeSearch = search;
    state.play.activeSearchRuntime = ponder
      ? "browser-wasm"
      : (
        browserEngineClient?.canAnalyzeRoot(analysisBody)
        || browserEngineClient?.canAnalyze(analysisBody)
      )
        ? "browser-wasm"
        : "render-server";
    state.play.error = null;
    renderAll();
    try {
      let analysis;
      if (ponder) {
        const settled = await ponder.promise;
        if (
          sequence !== state.play.sequence
          || key !== playPositionKey()
          || state.mode !== "play"
          || ponder.generation !== playPonderGeneration
        ) return;
        if (settled?.ok) {
          analysis = settled.result;
        } else {
          const retryController = new AbortController();
          controller = retryController;
          state.play.engineAbort = retryController;
          const retrySearchDeadlineMs = monotonicNow() + search.seconds * 1000;
          const retryReceiptDeadlineMs = retrySearchDeadlineMs
            + PLAY_ANALYSIS_RESPONSE_GRACE_MS;
          analysisReceiptDeadlineMs = retryReceiptDeadlineMs;
          state.play.activeSearchRuntime = (
            browserEngineClient?.canAnalyzeRoot(analysisBody)
            || browserEngineClient?.canAnalyze(analysisBody)
          )
            ? "browser-wasm"
            : "render-server";
          renderPlaySearchEvidence();
          analysis = await requestEngineAnalysis(
            analysisBody,
            retryController,
            sequence,
            retrySearchDeadlineMs,
            retryReceiptDeadlineMs,
          );
        }
      } else {
        analysis = await requestEngineAnalysis(
          analysisBody,
          controller,
          sequence,
          searchDeadlineMs,
          receiptDeadlineMs,
        );
      }
      if (sequence !== state.play.sequence || key !== playPositionKey() || state.mode !== "play") return;
      const hostedFallback = stagedHostedFallback(analysis);
      const expectedReplyIdentity = hostedFallback?.identity || loadedChampionIdentity();
      const receivedReplyIdentity = completeHostedEngineIdentity(analysis);
      if (!sameHostedEngineIdentity(expectedReplyIdentity, receivedReplyIdentity)) {
        throw new Error("The reply engine identity changed before the move was validated.");
      }
      if (expectedReplyIdentity?.engine_profile_id && analysis.engine_profile_id !== expectedReplyIdentity.engine_profile_id) {
        throw new Error("The reply came from a different engine profile than the loaded champion.");
      }
      if (expectedReplyIdentity?.source_fingerprint && analysis.source_fingerprint !== expectedReplyIdentity.source_fingerprint) {
        throw new Error("The reply engine fingerprint changed during the game.");
      }
      if (expectedReplyIdentity?.engine_version && analysis.engine_version !== expectedReplyIdentity.engine_version) {
        throw new Error("The reply engine version changed during the game.");
      }
      if (expectedReplyIdentity?.ruleset_version && analysis.ruleset_version !== expectedReplyIdentity.ruleset_version) {
        throw new Error("The reply ruleset changed during the game.");
      }
      const moves = engineSeriesFromAnalysis(analysis);
      if (!moves.length) throw new Error("The engine returned no legal series for this non-terminal position.");
      const localReplay = analysis.runtime_receipt?.runtime === "browser-wasm";
      if (localReplay && (
        analysis.legal_series_certified !== true
        || analysis.authoritative_replay_certified !== true
        || analysis.legal_validation_runtime !== "compiled-wasm"
        || first(
          analysis.wasm_sha256,
          analysis.runtime_receipt?.artifact_fingerprint,
        ) !== state.play.browserWasmArtifact
      )) {
        throw new Error("The local engine did not certify its legal series replay.");
      }
      const checked = localReplay
        ? analysis.checked_prefix
        : await requestPlayPrefixJson(
          { ...boundaryPayload(), prefix: moves },
            {
              signal: controller.signal,
              deadlineMs: analysisReceiptDeadlineMs,
              analysisReceipt: true,
          },
        );
      if (sequence !== state.play.sequence || key !== playPositionKey() || state.mode !== "play") return;
      const canonical = Array.isArray(checked.prefix) ? checked.prefix.map(String) : [];
      if (canonical.length !== moves.length || canonical.some((move, index) => move !== moves[index])) {
        throw new Error("The server replay did not match the engine's proposed series.");
      }
      if (!checked.complete && !checked.outcome) {
        throw new Error("The engine reply did not complete the required progressive series.");
      }
      state.play.lastSearch = playSearchEvidence(analysis, search);
      state.play.activeSearch = null;
      await animateCheckedEngineSeries(checked, sequence);
      if (sequence !== state.play.sequence || state.mode !== "play") return;
      if (hostedFallback) {
        if (controller.signal.aborted) {
          throw new DOMException("Request cancelled", "AbortError");
        }
        if (monotonicNow() > analysisReceiptDeadlineMs) {
          throw analysisDeadlineError();
        }
        if (!sameHostedEngineIdentity(hostedFallback.expected, loadedChampionIdentity())) {
          throw hostedEngineIdentityError(
            "The loaded champion changed before the hosted move was accepted.",
          );
        }
        applyHostedFallbackRuntime(hostedFallback.health, hostedFallback.reason);
        hostedFallbackAuthorities.delete(analysis);
      }
      applyPrefixPayload(checked, canonical, checked.san || canonical);
      state.play.lastEngineSeries = {
        series: state.boundary.series,
        moves: canonical,
        profileId: analysis.engine_profile_id,
        sourceFingerprint: analysis.source_fingerprint,
      };
      state.play.thinking = false;
      state.play.animating = false;
      if (!await requireDurablePlaySession({}, { capture: true })) return;
      if (playSessionExternalUpdate) return;
      const advanced = checked.complete && checked.next_state && !checked.outcome
        ? await advanceSeries(true)
        : false;
      if (playSessionExternalUpdate || playSessionSaveBlocked) return;
      if (!await requireDurablePlaySession({}, { capture: true })) return;
      if (advanced) void startPlayPonder(analysis, checked, canonical, search);
    } catch (error) {
      if (error.name === "AbortError" || sequence !== state.play.sequence) return;
      state.play.error = error?.code === "analysis-deadline"
        ? "The engine used its full search time before the reply receipt arrived."
        : displayError(error);
      await requireDurablePlaySession();
    } finally {
      if (ponder && claimedPlayPonder === ponder) claimedPlayPonder = null;
      if (state.play.engineAbort === controller) state.play.engineAbort = null;
      if (sequence === state.play.sequence) {
        state.play.thinking = false;
        state.play.animating = false;
        state.play.activeSearch = null;
        state.play.activeSearchRuntime = null;
        renderAll();
      }
    }
  }

  async function startNewPlayGame(options = {}) {
    if (activeNewPlayGame) return activeNewPlayGame;
    const transition = performStartNewPlayGame(options);
    activeNewPlayGame = transition;
    try {
      return await transition;
    } finally {
      if (activeNewPlayGame === transition) {
        activeNewPlayGame = null;
        renderAll();
      }
    }
  }

  async function waitForActiveNewPlayGame() {
    while (activeNewPlayGame) await activeNewPlayGame;
  }

  async function performStartNewPlayGame({ announce = true } = {}) {
    const replaceExpectedSessionId = state.play.sessionId;
    const replaceExpectedRevision = playSessionRevision;
    playSessionReplayBlocked = false;
    playSessionExternalUpdate = false;
    playSessionSaveBlocked = false;
    playSessionPendingWriteOptions = null;
    await cancelEngineTurn();
    await playPonderCleanup.catch(() => null);
    cancelAutoAnalysis(true);
    state.prefixAbort?.abort();
    state.prefixAbort = null;
    state.prefixSequence += 1;
    await PLAY_HANDOFF.wait().catch(() => null);
    const sequence = state.play.sequence;
    state.mode = "play";
    state.play.active = true;
    state.play.sessionId = randomStorageId("game");
    playSessionRevision = 0;
    state.play.resigned = false;
    state.play.error = null;
    state.play.lastEngineSeries = null;
    state.play.activeSearch = null;
    state.play.lastSearch = null;
    state.play.timelineIndex = null;
    state.boundary = {
      fen: START_FEN,
      series: 1,
      quiet_series: 0,
      ep_targets: [],
      promoted_hex: ZERO_PROMOTED_HEX,
      chess960: false,
    };
    state.prefix = [];
    state.prefixSan = [];
    state.prefixFrames = [];
    state.boardFen = START_FEN;
    state.legalMoves = [];
    state.movesRemaining = 1;
    state.complete = false;
    state.nextState = null;
    state.outcome = null;
    state.check = false;
    state.unusedMoves = 0;
    state.completionReason = null;
    state.history = [];
    state.lastMove = null;
    state.flipped = state.play.humanColor === "black";
    state.focusSquare = state.flipped ? "e7" : "e2";
    state.study = createStudy(state.boundary);
    state.currentTreeNodeId = null;
    state.seriesParentNodeId = null;
    state.branching = false;
    state.viewingHistorical = false;
    state.handoffNotice = null;
    state.positionReady = false;
    clearAnalysisDisplay();
    state.playWorkspace = captureWorkspace();
    const replacementOptions = {
      replaceSession: true,
      replaceExpectedSessionId,
      replaceExpectedRevision,
    };
    if (!await requireDurablePlaySession(replacementOptions)) return;
    if (playSessionExternalUpdate) return;
    renderAll();
    const payload = await refreshPrefix([], []);
    if (!payload || sequence !== state.play.sequence || state.mode !== "play") return;
    if (!await requireDurablePlaySession({}, { capture: true })) return;
    if (playSessionExternalUpdate) return;
    if (announce) showToast(`New game as ${state.play.humanColor === "white" ? "White" : "Black"}`);
    renderAll();
    void continuePlayFlow();
  }

  async function selectPlayColor(color) {
    if (!new Set(["white", "black"]).has(color)) return;
    await waitForActiveNewPlayGame();
    state.play.humanColor = color;
    await startNewPlayGame();
  }

  async function resignPlayGame() {
    await waitForActiveNewPlayGame();
    if (
      state.mode !== "play"
      || !state.play.active
      || blockStalePlayMutation()
      || playReviewActive()
      || playGameEnded()
    ) return;
    await cancelEngineTurn();
    await playPonderCleanup.catch(() => null);
    state.play.resigned = true;
    state.selected = null;
    if (!await requireDurablePlaySession()) return;
    if (playSessionExternalUpdate) return;
    renderAll();
  }

  async function switchWorkspaceMode(mode, { importPlayPosition = false } = {}) {
    if (!new Set(["play", "analyze"]).has(mode)) return;
    await waitForActiveNewPlayGame();
    if (state.mode === "play" && mode !== "play" && blockStalePlayMutation()) return;
    if (state.positionBusy) {
      showToast("Wait for the saved game validation to finish");
      return;
    }
    state.prefixAbort?.abort();
    state.prefixAbort = null;
    if (mode === "analyze") {
      if (state.mode === "play") {
        if (importPlayPosition && (state.play.thinking || state.play.animating || state.positionBusy)) return;
        const stablePlay = state.play.animating || state.positionBusy
          ? state.playWorkspace
          : captureWorkspace();
        state.playWorkspace = stablePlay;
        if (!await requireDurablePlaySession()) return;
        if (playSessionExternalUpdate || playSessionSaveBlocked) return;
        const imported = importPlayPosition ? stablePlay : null;
        await cancelEngineTurn();
        await playPonderCleanup.catch(() => null);
        if (imported) {
          imported.study = createStudy(imported.boundary);
          imported.currentTreeNodeId = null;
          imported.seriesParentNodeId = null;
          imported.branching = false;
          imported.viewingHistorical = false;
          imported.analysisPaused = false;
          state.analysisWorkspace = imported;
        }
      }
      state.mode = "analyze";
      restoreWorkspace(state.analysisWorkspace);
      clearAnalysisDisplay();
      dom.workspace_tabs.hidden = false;
      switchTab("analysis");
      renderAll();
      persistStudy();
      queueAutoAnalysis(80);
      return;
    }

    if (state.mode === "analyze") {
      state.analysisWorkspace = captureWorkspace();
      cancelAutoAnalysis(true);
    }
    state.mode = "play";
    if (!restoreWorkspace(state.playWorkspace)) {
      await startNewPlayGame();
      return;
    }
    renderAll();
    void continuePlayFlow();
  }

  function legalMovesFrom(square) {
    return state.legalMoves.filter((move) => move.from === square);
  }

  function chooseSquare(square) {
    if (!boardInputAllowed() || state.previewIndex !== null || state.positionBusy || state.outcome || state.complete) return;
    const candidates = state.selected
      ? state.legalMoves.filter((move) => move.from === state.selected && move.to === square)
      : [];
    if (candidates.length) {
      chooseMove(candidates);
      return;
    }
    if (state.selected === square) {
      state.selected = null;
    } else {
      state.selected = legalMovesFrom(square).length ? square : null;
    }
    state.focusSquare = square;
    renderBoard();
  }

  function chooseMove(candidates) {
    const promotions = candidates.filter((move) => move.promotion || move.uci.length > 4);
    if (promotions.length > 1) {
      openPromotionChooser(promotions);
    } else {
      submitMove(candidates[0]);
    }
  }

  function openPromotionChooser(moves) {
    const { pieces } = parseFen(activeBoardFen());
    const color = pieces.get(moves[0].from)?.color || "white";
    const order = ["q", "r", "b", "n"];
    const buttons = [...moves]
      .sort((a, b) => order.indexOf(a.promotion) - order.indexOf(b.promotion))
      .map((move) => {
        const button = document.createElement("button");
        const piece = move.promotion || move.uci.slice(4, 5);
        button.type = "button";
        button.className = "promotion-option";
        const image = document.createElement("img");
        image.src = pieceAsset({ type: piece, color });
        image.alt = "";
        image.draggable = false;
        button.append(image);
        button.setAttribute("aria-label", `Promote to ${PIECE_NAMES[piece] || piece}`);
        button.addEventListener("click", () => {
          dom.promotion_dialog.close();
          submitMove(move);
        });
        return button;
      });
    dom.promotion_options.replaceChildren(...buttons);
    dom.promotion_dialog.showModal();
  }

  async function submitMove(move) {
    if (
      !boardInputAllowed()
      || playSessionExternalUpdate
      || state.positionBusy
      || state.outcome
      || state.complete
      || state.previewIndex !== null
    ) return;
    const modeAtMove = state.mode;
    const boundaryAtMove = cloneBoundary(state.boundary);
    const parentId = state.currentTreeNodeId;
    const seriesParentId = state.seriesParentNodeId;
    const nextPrefix = [...state.prefix, move.uci];
    const nextSan = [...state.prefixSan, move.san || move.uci];
    state.lastMove = move.uci;
    state.selected = null;
    state.viewingHistorical = false;
    state.handoffNotice = null;
    const ponderAtMove = state.mode === "play" ? activePlayPonder : null;
    const ponderHit = state.mode === "play"
      ? cachedPlayPonderPrefix(boundaryAtMove, nextPrefix)
      : null;
    let payload;
    if (ponderHit) {
      applyCachedPlayPonderPrefix(ponderHit.payload, nextPrefix, nextSan);
      payload = ponderHit.payload;
    } else {
      if (ponderAtMove) await cancelPlayPonder("human-series-deviation");
      if (
        state.mode !== modeAtMove
        || (state.mode === "play" && (
          playSessionExternalUpdate
          || boundaryKey(state.boundary) !== boundaryKey(boundaryAtMove)
          || !sameMoveList(state.prefix, nextPrefix.slice(0, -1))
        ))
      ) return;
      payload = await refreshPrefix(nextPrefix, nextSan);
    }
    if (!payload) return;
    if (state.mode === "play" && playSessionExternalUpdate) return;
    if (state.mode === "analyze") {
      attachMoveToStudy(move, payload, boundaryAtMove, parentId, seriesParentId);
    }
    if (state.mode === "play") {
      if (!await requireDurablePlaySession({}, { capture: true })) return;
      if (playSessionExternalUpdate) return;
      if (ponderHit) rebindPlayPonderRevision(ponderHit.record);
    }
    if (payload.complete && payload.next_state && !payload.outcome) await advanceSeries(true);
    if (state.mode === "play") {
      if (playSessionExternalUpdate || playSessionSaveBlocked) return;
      if (!await requireDurablePlaySession({}, { capture: true })) return;
      if (ponderHit) rebindPlayPonderRevision(ponderHit.record);
      renderPlaySurface();
    }
  }

  function endDrag() {
    dom.drag_piece.className = "drag-piece";
    dom.drag_piece.textContent = "";
    state.drag = null;
  }

  function onPointerDown(event) {
    if (!boardInputAllowed() || state.previewIndex !== null || state.positionBusy || state.outcome || state.complete) return;
    if (event.button !== 0 && event.pointerType !== "touch") return;
    const square = event.target.closest(".square")?.dataset.square;
    if (!square || !legalMovesFrom(square).length) return;
    const piece = parseFen(activeBoardFen()).pieces.get(square);
    state.focusSquare = square;
    state.drag = {
      from: square,
      x: event.clientX,
      y: event.clientY,
      pointerId: event.pointerId,
      moved: false,
      piece,
    };
    event.preventDefault();
  }

  function onPointerMove(event) {
    if (!state.drag || state.drag.pointerId !== event.pointerId) return;
    const distance = Math.hypot(event.clientX - state.drag.x, event.clientY - state.drag.y);
    if (!state.drag.moved && distance < 5) return;
    state.drag.moved = true;
    state.selected = state.drag.from;
    renderBoard();
    const piece = state.drag.piece;
    const image = document.createElement("img");
    if (piece) image.src = pieceAsset(piece);
    image.alt = "";
    image.draggable = false;
    dom.drag_piece.replaceChildren(image);
    dom.drag_piece.className = `drag-piece is-visible ${piece?.color || "white"}`;
    dom.drag_piece.style.left = `${event.clientX}px`;
    dom.drag_piece.style.top = `${event.clientY}px`;
    event.preventDefault();
  }

  function onPointerUp(event) {
    if (!state.drag || state.drag.pointerId !== event.pointerId) return;
    const drag = state.drag;
    if (drag.moved) {
      dom.drag_piece.classList.remove("is-visible");
      const target = document.elementFromPoint(event.clientX, event.clientY)?.closest(".square")?.dataset.square;
      const candidates = state.legalMoves.filter((move) => move.from === drag.from && move.to === target);
      if (candidates.length) chooseMove(candidates);
      else renderBoard();
      state.suppressClick = true;
      window.setTimeout(() => { state.suppressClick = false; }, 0);
    }
    endDrag();
  }

  function onBoardClick(event) {
    if (state.suppressClick) return;
    const square = event.target.closest(".square")?.dataset.square;
    if (square) chooseSquare(square);
  }

  function onBoardKeydown(event) {
    if (!boardInputAllowed()) return;
    const square = event.target.closest(".square")?.dataset.square;
    if (!square) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      chooseSquare(square);
      return;
    }
    if (event.key === "Escape") {
      state.selected = null;
      renderBoard();
      return;
    }
    const children = [...dom.board.children];
    const index = children.indexOf(event.target.closest(".square"));
    const offsets = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -8, ArrowDown: 8 };
    if (!(event.key in offsets)) return;
    event.preventDefault();
    const row = Math.floor(index / 8);
    const next = index + offsets[event.key];
    if (next < 0 || next >= 64) return;
    if (event.key === "ArrowLeft" && next < row * 8) return;
    if (event.key === "ArrowRight" && next >= (row + 1) * 8) return;
    state.focusSquare = children[next].dataset.square;
    children.forEach((node, childIndex) => { node.tabIndex = childIndex === next ? 0 : -1; });
    children[next].focus();
  }

  function seriesMoves(value) {
    if (!value) return [];
    if (Array.isArray(value)) return value.flatMap(seriesMoves);
    if (typeof value === "object") return seriesMoves(first(value.moves, value.series, value.uci, value.line, value.notation));
    const text = String(value);
    const uci = text.match(/[a-h][1-8][a-h][1-8][qrbn]?/gi);
    if (uci?.length) return uci;
    return text.split(/\s*\/\s*|\s+/).filter(Boolean);
  }

  function displayLine(value) {
    if (value === undefined || value === null) return "Not reported";
    if (typeof value === "string" || typeof value === "number") return String(value);
    if (Array.isArray(value)) return value.map(displayLine).join(" · ");
    return String(first(value.notation, value.san, value.uci, value.line, value.series, JSON.stringify(value)));
  }

  function proofMetadata(result) {
    const stats = result.stats || {};
    const exact = asBoolean(first(result.exact_width, stats.exact_width));
    const timedOut = asBoolean(first(result.timed_out, stats.timed_out, stats.timeout));
    const requested = first(result.requested_depth, result.depth_requested, stats.requested_depth, stats.depth_requested, dom.depth_control.value);
    const completed = first(result.completed_depth, result.depth_completed, stats.completed_depth, stats.depth_completed);
    const reach = asBoolean(first(result.evaluation?.reach_complete, result.reach_complete, stats.reach_complete));
    return { exact, timedOut, requested, completed, reach };
  }

  function proofChip(text, tone = "") {
    const chip = document.createElement("span");
    chip.className = `proof-chip ${tone ? `is-${tone}` : ""}`;
    chip.textContent = text;
    return chip;
  }

  function renderProofStrip(result) {
    const meta = proofMetadata(result);
    const chips = [];
    chips.push(meta.exact === true
      ? proofChip("Exact width", "good")
      : meta.exact === false
        ? proofChip("Selective width", "warning")
        : proofChip("Width not reported", "warning"));
    chips.push(meta.timedOut === false
      ? proofChip("No timeout", "good")
      : meta.timedOut === true
        ? proofChip("Timed out", "danger")
        : proofChip("Timeout status unknown", "warning"));
    if (asBoolean(result.work_limit_reached) === true) {
      chips.push(proofChip("Work limit reached", "danger"));
    }
    const depthText = meta.completed === undefined
      ? `Requested depth ${meta.requested}`
      : `Depth ${meta.completed} / ${meta.requested}`;
    const depthTone = meta.completed !== undefined && Number(meta.completed) < Number(meta.requested) ? "danger" : "good";
    chips.push(proofChip(depthText, depthTone));
    chips.push(meta.reach === true
      ? proofChip("Bounded reach probe complete", "good")
      : meta.reach === false
        ? proofChip("Bounded reach probe capped", "warning")
        : proofChip("Bounded reach status unknown", "warning"));
    if (result.proven_result) {
      const certified = meta.exact === true && meta.timedOut === false && Number(meta.completed) >= Number(meta.requested);
      chips.push(proofChip(
        certified ? `Proven: ${humanize(result.proven_result)}` : `Uncertified result claim: ${humanize(result.proven_result)}`,
        certified ? "good" : "danger",
      ));
    }
    dom.proof_strip.replaceChildren(...chips);
    dom.reach_status.textContent = meta.reach === true ? "Bounded probe complete" : meta.reach === false ? "Bounded probe capped" : "Bounded probe unknown";
  }

  function renderBestSeries(result) {
    const meta = proofMetadata(result);
    const completedRequestedDepth = meta.completed !== undefined
      && Number(meta.completed) >= Number(meta.requested);
    const isCompletion = result.analysis_scope === "series-prefix";
    dom.result_choice_heading.textContent = meta.timedOut === true
      ? isCompletion ? "Incomplete completion" : "Incomplete engine choice"
      : meta.exact === true && meta.timedOut === false && completedRequestedDepth
        ? isCompletion ? "Best completion" : "Best series"
        : isCompletion ? "Selective completion" : "Selective engine choice";
    const moves = seriesMoves(first(
      isCompletion ? result.best_completion : undefined,
      result.best_series,
    ));
    const nodes = moves.length ? moves.map((move, index) => {
      const node = document.createElement("span");
      node.className = "series-move";
      const number = document.createElement("b");
      number.textContent = String(index + 1);
      node.append(number, document.createTextNode(move));
      return node;
    }) : [Object.assign(document.createElement("span"), { className: "empty-chip", textContent: "No best series reported" })];
    dom.best_series.replaceChildren(...nodes);
    dom.best_notation.textContent = displayLine(first(result.best_notation, result.best_series?.notation, ""));
  }

  function renderPv(result) {
    const raw = first(result.pv, result.principal_variation, []);
    const series = Array.isArray(raw) ? raw : typeof raw === "string" ? raw.split(/\s*\|\s*/).filter(Boolean) : raw ? [raw] : [];
    let frameIndex = 0;
    const nodes = series.map((item, seriesIndex) => {
      const group = document.createElement("span");
      group.className = "pv-series-group";
      const label = document.createElement("small");
      const number = Math.floor(asNumber(first(item?.series_number, item?.series, seriesIndex + 1), seriesIndex + 1));
      label.textContent = `S${number}`;
      group.append(label);
      const moves = Array.isArray(item?.san) && item.san.length
        ? item.san.map(String)
        : seriesMoves(first(item?.moves, item?.uci, item));
      moves.forEach((move, microIndex) => {
        const node = document.createElement("button");
        node.type = "button";
        node.className = "pv-step";
        node.dataset.pvIndex = String(frameIndex);
        node.textContent = move;
        node.title = `Preview series ${number}, move ${microIndex + 1}`;
        node.setAttribute("aria-label", `Preview series ${number}, move ${microIndex + 1}: ${move}`);
        const targetIndex = frameIndex;
        node.addEventListener("click", () => previewPvFrame(targetIndex));
        group.append(node);
        frameIndex += 1;
      });
      return group;
    });
    if (!nodes.length) nodes.push(Object.assign(document.createElement("span"), { className: "empty-chip", textContent: "No principal variation reported" }));
    dom.pv_line.replaceChildren(...nodes);
    void preparePvFrames(result, raw);
  }

  async function preparePvFrames(result, raw) {
    state.pvAbort?.abort();
    const controller = new AbortController();
    state.pvAbort = controller;
    state.pvFrames = [];
    state.previewIndex = null;
    dom.pv_controls.hidden = true;
    if (!Array.isArray(raw) || !raw.length) {
      if (state.pvAbort === controller) state.pvAbort = null;
      return;
    }
    let cursor = normalizeNextState(result.state)
      || (state.complete && state.nextState
        ? { ...state.nextState, ep_targets: [...state.nextState.ep_targets] }
        : { ...state.boundary, ep_targets: [...state.boundary.ep_targets] });
    const frames = [];
    for (let index = 0; index < raw.length; index += 1) {
      const item = raw[index];
      const moves = seriesMoves(first(item?.moves, item?.uci, item?.series_uci, item?.series));
      const sans = Array.isArray(item?.san) ? item.san.map(String) : [];
      if (!moves.length || !cursor?.fen) break;
      let response = null;
      for (let micro = 0; micro < moves.length; micro += 1) {
        try {
          response = await requestJson("/api/prefix", {
            method: "POST",
            signal: controller.signal,
            body: JSON.stringify({
              fen: cursor.fen,
              series: cursor.series,
              quiet_series: cursor.quiet_series,
              ep_targets: cursor.ep_targets,
              progressive_ep: cursor.ep_targets,
              promoted_hex: cursor.promoted_hex,
              chess960: cursor.chess960 === true,
              prefix: moves.slice(0, micro + 1),
            }),
          });
        } catch (error) {
          if (error.name === "AbortError") return;
          response = null;
        }
        if (!response) break;
        frames.push({
          fen: String(first(response.board_fen, response.fen, cursor.fen)),
          series: cursor.series,
          micro: micro + 1,
          total: moves.length,
          label: sans[micro] || moves[micro],
        });
      }
      if (!response?.complete) break;
      const next = normalizeNextState(response.next_state);
      if (!next) break;
      cursor = next;
    }
    if (state.analysis !== result || !frames.length || controller.signal.aborted) return;
    state.pvFrames = frames;
    dom.pv_controls.hidden = false;
    updatePvControls();
    if (state.pvAbort === controller) state.pvAbort = null;
  }

  function updatePvControls() {
    const atStart = state.previewIndex === null;
    const atEnd = state.previewIndex === state.pvFrames.length - 1;
    dom.pv_previous.disabled = atStart;
    dom.pv_next.disabled = !state.pvFrames.length || atEnd;
    dom.pv_exit.disabled = atStart;
    const frame = atStart ? null : state.pvFrames[state.previewIndex];
    dom.pv_indicator.textContent = atStart
      ? `Start · ${state.pvFrames.length} moves`
      : `S${frame.series} · ${frame.micro}/${frame.total}`;
    [...dom.pv_line.querySelectorAll(".pv-step")].forEach((node, index) => {
      node.classList.toggle("is-previewed", index === state.previewIndex);
    });
  }

  function previewPvFrame(index) {
    if (!state.pvFrames[index]) return;
    state.previewIndex = index;
    state.selected = null;
    updatePvControls();
    renderBoard();
  }

  function stepPv(direction) {
    if (!state.pvFrames.length) return;
    if (direction > 0) {
      state.previewIndex = state.previewIndex === null ? 0 : Math.min(state.previewIndex + 1, state.pvFrames.length - 1);
    } else if (state.previewIndex !== null) {
      state.previewIndex = state.previewIndex === 0 ? null : state.previewIndex - 1;
    }
    state.selected = null;
    updatePvControls();
    renderBoard();
  }

  function exitPvPreview(announce = true) {
    if (state.previewIndex === null) return;
    state.previewIndex = null;
    updatePvControls();
    renderBoard();
    if (announce) showToast("Returned to the actual position");
  }

  function alternativeScore(alternative) {
    return first(alternative?.score, alternative?.evaluation, alternative?.value);
  }

  function renderAlternatives(result) {
    const alternatives = analysisAlternatives(result);
    dom.alternatives_count.textContent = `${alternatives.length} line${alternatives.length === 1 ? "" : "s"}`;
    if (!alternatives.length) {
      dom.alternatives_list.replaceChildren(Object.assign(document.createElement("span"), { className: "empty-chip", textContent: "No alternatives returned" }));
      return;
    }
    const rows = alternatives.map((alternative, index) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "alternative-row";
      const rank = document.createElement("span");
      rank.className = "alt-rank";
      rank.textContent = String(index + 1);
      const line = document.createElement("span");
      line.className = "alt-line";
      const title = document.createElement("strong");
      title.textContent = displayLine(first(
        result.analysis_scope === "series-prefix" ? alternative.completion : undefined,
        alternative.notation,
        alternative.best_notation,
        alternative.series,
        alternative.moves,
        alternative.uci,
      ));
      const detail = document.createElement("small");
      const evaluation = describeEvaluation(alternativeScore(alternative), {
        ...alternative,
        mate_score: result.mate_score,
      });
      detail.textContent = displayLine(first(alternative.classification, alternative.confidence, evaluation.plain, extractUci(alternative), "Candidate series"));
      line.append(title, detail);
      const score = document.createElement("span");
      score.className = "alt-score";
      score.textContent = evaluation.label;
      score.title = `${evaluation.spoken}. ${EVALUATION_SCALE_HELP}`;
      score.setAttribute("aria-label", evaluation.spoken);
      row.append(rank, line, score);
      row.title = "Show this line's first move on the board";
      row.addEventListener("click", () => {
        const uci = extractUci(first(alternative.next_move_uci, alternative.completion, alternative));
        if (!uci) return;
        state.arrowSelection = uci;
        renderArrows();
        showToast(`Showing ${uci} on the board`);
      });
      return row;
    });
    dom.alternatives_list.replaceChildren(...rows);
  }

  function numericEvaluationEntries(evaluation) {
    if (!evaluation || typeof evaluation !== "object") return [];
    const skip = new Set(["score", "total", "reach_complete", "warnings", "tactical_warnings"]);
    const entries = [];
    Object.entries(evaluation).forEach(([key, value]) => {
      if (skip.has(key) || /(?:distance|nodes)$/i.test(key)) return;
      if (typeof value === "number" && Number.isFinite(value)) {
        entries.push([key, value]);
      } else if (value && typeof value === "object" && !Array.isArray(value)) {
        Object.entries(value).forEach(([child, number]) => {
          if (typeof number === "number" && Number.isFinite(number) && child !== "complete") {
            entries.push([`${key} ${child}`, number]);
          }
        });
      }
    });
    return entries.slice(0, 12);
  }

  function renderEvaluation(result) {
    const entries = numericEvaluationEntries(result.evaluation);
    if (!entries.length) {
      dom.evaluation_breakdown.replaceChildren(Object.assign(document.createElement("span"), { className: "empty-chip", textContent: "No component breakdown returned" }));
      return;
    }
    const maximum = Math.max(...entries.map(([, value]) => Math.abs(value)), 1);
    const rows = entries.map(([key, value]) => {
      const row = document.createElement("div");
      row.className = "breakdown-row";
      const label = document.createElement("span");
      label.className = "breakdown-label";
      label.textContent = humanize(key);
      label.title = humanize(key);
      const track = document.createElement("span");
      track.className = "breakdown-track";
      const bar = document.createElement("i");
      bar.className = `breakdown-bar ${value < 0 ? "is-negative" : "is-positive"}`;
      bar.style.width = `${Math.max(2, Math.abs(value) / maximum * 48)}%`;
      track.append(bar);
      const number = document.createElement("span");
      number.className = "breakdown-value";
      const evaluation = describeEvaluation(value);
      number.textContent = evaluation.label;
      number.title = `${evaluation.spoken}. Component contribution; ${EVALUATION_SCALE_HELP}`;
      row.append(label, track, number);
      return row;
    });
    dom.evaluation_breakdown.replaceChildren(...rows);
  }

  function warningValues(result) {
    const values = [
      first(result.tactical_warnings, result.warnings),
      first(result.evaluation?.tactical_warnings, result.evaluation?.warnings),
    ].flatMap((value) => Array.isArray(value) ? value : value ? [value] : []);
    return values.map((value) => displayLine(value)).filter(Boolean);
  }

  function renderWarnings(result) {
    const warnings = warningValues(result);
    dom.warnings_section.hidden = warnings.length === 0;
    dom.warnings_list.replaceChildren(...warnings.map((warning) => {
      const item = document.createElement("li");
      item.textContent = warning;
      return item;
    }));
  }

  function renderStats(result) {
    const stats = { ...(result.stats || {}) };
    ["requested_depth", "completed_depth", "exact_width", "timed_out", "work_limit_reached", "analysis_searches", "request_time_limit_seconds", "request_max_generation_positions", "max_generation_positions", "analysis_scope", "source_fingerprint", "engine_version", "engine_profile_id", "ruleset_version", "adjudication_status"]
      .forEach((key) => {
        if (result[key] !== undefined && stats[key] === undefined) stats[key] = result[key];
      });
    const entries = Object.entries(stats).filter(([, value]) => ["string", "number", "boolean"].includes(typeof value));
    const nodes = [];
    entries.forEach(([key, value]) => {
      const term = document.createElement("dt");
      term.textContent = humanize(key);
      const definition = document.createElement("dd");
      definition.textContent = typeof value === "number" && Math.abs(value) >= 10000 ? compactNumber(value) : String(value);
      definition.title = String(value);
      nodes.push(term, definition);
    });
    if (!nodes.length) {
      const term = document.createElement("dt");
      term.textContent = "Metadata";
      const definition = document.createElement("dd");
      definition.textContent = "Not reported";
      nodes.push(term, definition);
    }
    dom.search_stats.replaceChildren(...nodes);
  }

  function updateEvalBar(score, evidence = {}) {
    const number = Number(score);
    const percent = Number.isFinite(number) ? 50 + 46 * Math.tanh(number / 900) : 50;
    const bounded = Math.max(4, Math.min(96, percent));
    const evaluation = describeEvaluation(score, evidence);
    dom.eval_fill.style.height = `${bounded}%`;
    dom.eval_marker.style.bottom = `${bounded}%`;
    dom.eval_marker.textContent = evaluation.compact;
    dom.eval_marker.title = evaluation.spoken;
    dom.eval_rail.setAttribute("aria-label", `${evaluation.spoken}. ${EVALUATION_SCALE_HELP}`);
    dom.eval_rail.title = `${evaluation.spoken}. ${EVALUATION_SCALE_HELP}`;
  }

  function renderAnalysis(result) {
    state.analysis = result;
    dom.analysis_empty.hidden = true;
    dom.analysis_loading.hidden = true;
    dom.analysis_error.hidden = true;
    dom.analysis_results.hidden = false;
    const score = first(result.score, result.evaluation?.score, result.value);
    const evaluation = describeEvaluation(score, result);
    dom.result_score.textContent = evaluation.label;
    dom.result_score.title = `${evaluation.spoken}. ${EVALUATION_SCALE_HELP}`;
    dom.result_score.setAttribute("aria-label", evaluation.spoken);
    dom.result_raw_score.textContent = evaluation.rawLabel === null
      ? "Raw engine score unavailable"
      : `Raw engine score ${evaluation.rawLabel}`;
    dom.result_classification.textContent = String(first(result.classification, "Unclassified"));
    dom.result_confidence.textContent = String(first(result.confidence, "Confidence not reported"));
    const analyzedSeries = Math.floor(asNumber(
      first(result.state?.series, result.state?.series_number),
      state.complete && state.nextState ? state.nextState.series : state.boundary.series,
    ));
    dom.result_side.textContent = `${analyzedSeries % 2 === 1 ? "White" : "Black"} to move`;
    renderProofStrip(result);
    renderBestSeries(result);
    renderPv(result);
    renderAlternatives(result);
    renderEvaluation(result);
    renderWarnings(result);
    renderStats(result);
    updateEvalBar(score, result);
    renderArrows();
  }

  function applyAnalysisPreset(name, announce = true) {
    const preset = ANALYSIS_PRESETS[name];
    if (!preset) return;
    state.analysisPreset = name;
    dom.depth_control.value = String(preset.depth);
    dom.cap_control.value = String(Math.min(preset.cap, state.maximumBranchCap));
    dom.time_control.value = String(Math.min(preset.seconds, state.maximumAnalysisSeconds));
    dom.alternatives_control.value = String(Math.min(preset.alternatives, state.maximumAlternatives));
    ["quick", "strong"].forEach((candidate) => {
      const button = dom[`preset_${candidate}`];
      const active = candidate === name;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    if (announce) {
      clearAnalysisDisplay();
      queueAutoAnalysis(90);
      showToast(name === "strong" ? "Deep automatic analysis selected" : "Quick automatic analysis selected");
    }
  }

  function markPresetCustom() {
    state.analysisPreset = "custom";
    [dom.preset_quick, dom.preset_strong].forEach((button) => {
      button.classList.remove("is-active");
      button.setAttribute("aria-pressed", "false");
    });
    clearAnalysisDisplay();
    queueAutoAnalysis(120);
  }

  function clearAnalysisDisplay() {
    cancelAutoAnalysis(true);
    state.pvAbort?.abort();
    state.analysis = null;
    state.previewIndex = null;
    state.pvFrames = [];
    state.arrowSelection = null;
    dom.pv_controls.hidden = true;
    dom.board_shell.classList.remove("is-previewing");
    dom.analysis_loading.hidden = true;
    dom.analysis_error.hidden = true;
    dom.analysis_results.hidden = true;
    dom.analysis_empty.hidden = false;
    updateEvalBar(null);
    renderArrows();
    updateAnalysisProgress();
  }

  function reportData(report) {
    return first(report?.data, report?.payload, report);
  }

  function findOpeningResults(payload) {
    if (Array.isArray(payload)) return payload;
    if (Array.isArray(payload?.results)) return payload.results;
    if (Array.isArray(payload?.openings)) return payload.openings;
    const reports = payload?.reports || {};
    const preferred = first(reports.initial_ranking, reports.initial, reports.ranking);
    const data = reportData(preferred);
    return first(data?.results, data?.openings, []);
  }

  function renderTheory(payload) {
    const reports = payload?.reports || {};
    const initialReport = first(reports.initial_ranking, reports.initial, reports.ranking);
    const data = reportData(initialReport) || payload;
    const rows = findOpeningResults(payload);
    const badges = [];
    const current = first(initialReport?.current, payload.current);
    if (current !== undefined) badges.push(proofChip(current ? "Current fingerprint" : "Stale fingerprint", current ? "good" : "warning"));
    if (data?.total_series_horizon !== undefined) badges.push(proofChip(`${data.total_series_horizon}-series horizon`));
    if (data?.all_reply_searches_exact !== undefined) badges.push(proofChip(data.all_reply_searches_exact ? "Exact width" : "Selective width", data.all_reply_searches_exact ? "good" : "warning"));
    if (data?.generated_at) badges.push(proofChip(new Date(data.generated_at).toLocaleDateString()));
    dom.theory_meta.replaceChildren(...badges);
    dom.theory_loading.hidden = true;
    dom.theory_error.hidden = true;
    if (!rows.length) {
      dom.opening_list.replaceChildren(Object.assign(document.createElement("div"), { className: "analysis-empty", textContent: payload?.available === false ? "No opening reports are available yet." : "No opening rows were returned." }));
      return;
    }
    const nodes = rows.map((opening, index) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "opening-row";
      const rank = document.createElement("span");
      rank.className = "opening-rank";
      rank.textContent = String(first(opening.rank, index + 1));
      const main = document.createElement("span");
      main.className = "opening-main";
      const title = document.createElement("strong");
      title.textContent = String(first(opening.move_san, opening.san, opening.move_uci, opening.uci, "Unknown move"));
      const reply = document.createElement("span");
      reply.textContent = first(opening.best_black_notation, opening.best_reply, opening.principal_variation, opening.classification, "No reply line reported");
      main.append(title, reply);
      const score = document.createElement("span");
      score.className = "opening-score";
      const value = document.createElement("strong");
      const evaluation = describeEvaluation(first(opening.score, opening.value), opening);
      value.textContent = evaluation.label;
      value.title = `${evaluation.spoken}. ${EVALUATION_SCALE_HELP}`;
      const unit = document.createElement("small");
      unit.textContent = evaluation.plain;
      score.append(value, unit);
      row.append(rank, main, score);
      const uci = first(opening.move_uci, opening.uci, extractUci(opening.move));
      row.disabled = !uci;
      row.setAttribute("aria-label", `Load ${title.textContent}, ${evaluation.spoken}`);
      row.addEventListener("click", () => loadOpeningMove(String(uci)));
      return row;
    });
    dom.opening_list.replaceChildren(...nodes);
  }

  async function loadOpenings() {
    dom.theory_loading.hidden = false;
    dom.theory_error.hidden = true;
    dom.opening_list.replaceChildren();
    try {
      renderTheory(await requestJson("/api/openings"));
    } catch (error) {
      dom.theory_loading.hidden = true;
      dom.theory_error.hidden = false;
      dom.theory_error.textContent = displayError(error);
    }
  }

  async function loadOpeningMove(uci) {
    state.boundary = {
      fen: START_FEN,
      series: 1,
      quiet_series: 0,
      ep_targets: [],
      promoted_hex: ZERO_PROMOTED_HEX,
      chess960: false,
    };
    state.history = [];
    state.prefix = [];
    state.prefixSan = [];
    resetStudy(state.boundary);
    switchTab("analysis");
    const payload = await refreshPrefix([uci], [uci]);
    if (payload) {
      attachMoveToStudy({ uci, san: notationArray(payload, [uci], [uci])[0] }, payload, state.boundary, null, null);
      if (payload.complete && payload.next_state && !payload.outcome) {
        await advanceSeries(true);
      }
      showToast(`Loaded opening move ${uci}`);
    }
  }

  function syncSetupFields() {
    if (document.activeElement?.closest("#setup-form")) return;
    dom.fen_input.value = state.boundary.fen;
    dom.series_input.value = String(state.boundary.series);
    dom.quiet_input.value = String(state.boundary.quiet_series);
    dom.ep_input.value = state.boundary.ep_targets.join(", ");
  }

  async function loadSetup(event) {
    event?.preventDefault();
    dom.setup_error.hidden = true;
    const fen = dom.fen_input.value.trim();
    const series = Math.floor(asNumber(dom.series_input.value, 0));
    const quiet = Math.floor(asNumber(dom.quiet_input.value, -1));
    const epTargets = normalizeEpTargets(dom.ep_input.value);
    try {
      if (!fen || fen.split("/").length !== 8) throw new Error("Enter a complete orthodox FEN.");
      if (series < 1) throw new Error("Series number must be at least 1.");
      if (quiet < 0) throw new Error("Quiet series cannot be negative.");
      if (epTargets.some((square) => !/^[a-h][1-8]$/i.test(square))) throw new Error("En-passant targets must be squares such as e3 or c6.");
      const candidate = {
        fen,
        series,
        quiet_series: quiet,
        ep_targets: epTargets.map((square) => square.toLowerCase()),
        promoted_hex: null,
        chess960: false,
      };
      state.prefixAbort?.abort();
      state.positionReady = false;
      cancelAutoAnalysis(true);
      state.pvAbort?.abort();
      const controller = new AbortController();
      state.prefixAbort = controller;
      const sequence = ++state.prefixSequence;
      setBoardBusy(true);
      const payload = await requestJson("/api/prefix", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          ...candidate,
          progressive_ep: [...candidate.ep_targets],
          prefix: [],
        }),
      });
      if (sequence !== state.prefixSequence) return;
      state.boundary = cloneBoundary(first(payload.boundary_state, candidate));
      state.history = [];
      state.study = createStudy(state.boundary);
      state.currentTreeNodeId = null;
      state.seriesParentNodeId = null;
      state.viewingHistorical = false;
      state.handoffNotice = null;
      applyPrefixPayload(payload, [], []);
      persistStudy();
      switchTab("analysis");
    } catch (error) {
      if (error.name === "AbortError") return;
      dom.setup_error.hidden = false;
      dom.setup_error.textContent = displayError(error);
    } finally {
      setBoardBusy(false);
      queueAutoAnalysis();
    }
  }

  function rebuildStudyFromValidatedPrefix(boundary, payload) {
    const prefix = Array.isArray(payload.prefix) ? payload.prefix.map(String) : [];
    const sans = notationArray(payload, prefix, prefix);
    state.study = createStudy(boundary);
    let parentId = null;
    prefix.forEach((uci, index) => {
      const id = createId();
      state.study.nodes[id] = {
        id,
        parentId,
        seriesParentId: null,
        uci,
        san: sans[index] || uci,
        boundary: cloneBoundary(boundary),
        prefix: prefix.slice(0, index + 1),
        series: boundary.series,
        micro: index + 1,
        complete: index === prefix.length - 1 && Boolean(payload.complete),
        validated: true,
        quality: null,
        createdAt: new Date(Date.now() + index).toISOString(),
      };
      parentId = id;
    });
    state.currentTreeNodeId = parentId;
    state.seriesParentNodeId = null;
  }

  async function loadSavedPosition(id) {
    const saved = state.savedPositions.find((candidate) => candidate.id === id);
    if (!saved) return;
    const loadPlan = globalThis.ScottishProgressiveStudySafety.planSavedPositionLoad({
      study: state.study,
      currentBoundary: state.boundary,
      currentPrefix: state.prefix,
      savedBoundary: saved.boundary,
      savedPrefix: saved.prefix,
      boundaryKey,
    });
    const studyDescription = loadPlan.nodeCount
      ? `${loadPlan.nodeCount} saved move${loadPlan.nodeCount === 1 ? "" : "s"}`
      : `${loadPlan.analysisCount} saved analysis result${loadPlan.analysisCount === 1 ? "" : "s"}`;
    if (
      !globalThis.ScottishProgressiveStudySafety.confirmSavedPositionReplacement(
        loadPlan,
        `Loading “${saved.name}” will replace the current local study and its ${studyDescription}. Continue?`,
        (message) => window.confirm(message),
      )
    ) {
      dom.saved_position_status.textContent = "Kept the current study.";
      return;
    }
    exitPvPreview(false);
    state.prefixAbort?.abort();
    state.positionReady = false;
    cancelAutoAnalysis(true);
    const controller = new AbortController();
    state.prefixAbort = controller;
    const sequence = ++state.prefixSequence;
    setBoardBusy(true);
    dom.saved_position_status.textContent = `Checking ${saved.name} with the server…`;
    try {
      const payload = await requestJson("/api/prefix", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          ...saved.boundary,
          progressive_ep: [...saved.boundary.ep_targets],
          prefix: [...saved.prefix],
        }),
      });
      if (sequence !== state.prefixSequence) return;
      state.boundary = cloneBoundary(saved.boundary);
      state.history = [];
      state.branching = false;
      state.viewingHistorical = Boolean(payload.complete);
      state.handoffNotice = null;
      if (!loadPlan.preserveStudy) rebuildStudyFromValidatedPrefix(state.boundary, payload);
      applyPrefixPayload(payload, saved.prefix, saved.prefix);
      persistStudy();
      renderStudyTree();
      switchTab("analysis");
      dom.saved_dialog.close();
      showToast(`Loaded ${saved.name}`);
    } catch (error) {
      if (error.name === "AbortError") return;
      dom.saved_position_status.textContent = `Could not load ${saved.name}: ${displayError(error)}`;
    } finally {
      if (sequence === state.prefixSequence) {
        setBoardBusy(false);
        queueAutoAnalysis();
      }
    }
  }

  async function performSeriesHandoff(plan, automatic, context) {
    if (state.mode === "play" && playSessionExternalUpdate) return false;
    const { completedSeries, completedByCheck, unusedMoves, playSequence } = context;
    const historyEntry = {
      ...plan.historyEntry,
      frames: clonePlain(state.prefixFrames, []),
      check: state.check,
      unusedMoves: state.unusedMoves,
      completionReason: state.completionReason,
      treeNodeId: state.currentTreeNodeId,
      seriesParentNodeId: state.seriesParentNodeId,
      handoffKey: plan.key,
    };
    if (state.history.at(-1)?.handoffKey !== plan.key) state.history.push(historyEntry);
    state.boundary = cloneBoundary(plan.nextBoundary);
    state.prefix = [];
    state.prefixSan = [];
    state.prefixFrames = [];
    state.seriesParentNodeId = state.currentTreeNodeId;
    state.boardFen = state.boundary.fen;
    state.legalMoves = [];
    state.movesRemaining = plan.movesRemaining;
    state.complete = false;
    state.nextState = null;
    state.check = false;
    state.unusedMoves = 0;
    state.completionReason = null;
    state.viewingHistorical = false;
    state.positionReady = false;
    state.handoffNotice = completedByCheck && unusedMoves > 0
      ? `Series ${completedSeries} ended by check; ${unusedMoves} unused move${unusedMoves === 1 ? "" : "s"} were forfeited.`
      : `Series ${completedSeries} complete. Series ${state.boundary.series} started automatically.`;
    if (state.mode === "play") {
      if (!await requireDurablePlaySession({}, { capture: true })) return false;
      if (
        playSessionExternalUpdate
        || playSessionSaveBlocked
        || state.play.sequence !== playSequence
      ) return false;
      rebindPlayPonderRevision();
    }
    renderAll();

    const expectedBoundaryKey = boundaryKey(plan.nextBoundary);
    const firstAttemptSequence = state.prefixSequence + 1;
    const ponderAtHandoff = state.mode === "play" ? activePlayPonder : null;
    const ponderHit = state.mode === "play"
      ? cachedPlayPonderPrefix(plan.nextBoundary, [])
      : null;
    let payload;
    if (ponderHit) {
      applyCachedPlayPonderPrefix(ponderHit.payload, [], []);
      payload = ponderHit.payload;
    } else {
      if (ponderAtHandoff) await cancelPlayPonder("series-handoff-mismatch");
      payload = await refreshPrefix([], []);
    }
    if (
      state.mode === "play"
      && (
        playSessionExternalUpdate
        || playSessionSaveBlocked
        || state.play.sequence !== playSequence
      )
    ) return false;
    const retryablePlayHandoff = !payload
      && state.mode === "play"
      && state.play.sequence === playSequence
      && state.prefixSequence === firstAttemptSequence
      && boundaryKey(state.boundary) === expectedBoundaryKey
      && !state.positionBusy
      && !playGameEnded();
    if (retryablePlayHandoff) payload = await refreshPrefix([], []);
    if (!payload) {
      if (
        state.mode === "play"
        && state.play.sequence === playSequence
        && boundaryKey(state.boundary) === expectedBoundaryKey
      ) {
        state.play.error = `Series ${state.boundary.series} could not start. Start a new game or retry the position.`;
        renderAll();
      }
      return false;
    }

    if (state.mode === "analyze") {
      persistStudy();
      renderStudyTree();
    } else {
      state.play.error = null;
      if (!await requireDurablePlaySession({}, { capture: true })) return false;
      if (playSessionExternalUpdate || state.play.sequence !== playSequence) return false;
      if (ponderHit) rebindPlayPonderRevision(ponderHit.record);
    }
    showToast(completedByCheck && unusedMoves > 0
      ? `Check ended Series ${completedSeries} early · ${unusedMoves} unused move${unusedMoves === 1 ? "" : "s"}`
      : automatic ? `Series ${state.boundary.series} started` : `Continued to Series ${state.boundary.series}`);
    return true;
  }

  function advanceSeries(automatic = false) {
    if (
      (state.mode === "play" && playSessionExternalUpdate)
      || (state.mode === "play" && playSessionSaveBlocked)
      || !state.complete
      || !state.nextState
      || state.outcome
    ) return Promise.resolve(false);
    if (PLAY_HANDOFF.isActive()) return PLAY_HANDOFF.wait();
    const completedSeries = state.boundary.series;
    const completedByCheck = state.check;
    const unusedMoves = state.unusedMoves;
    const playSequence = state.play.sequence;
    let plan;
    try {
      plan = globalThis.ScottishProgressivePlayHandoff.prepareCompletedSeries({
        boundary: state.boundary,
        nextState: state.nextState,
        prefix: state.prefix,
        prefixSan: state.prefixSan,
      });
    } catch (error) {
      if (state.mode === "play") {
        state.play.error = displayError(error);
        renderAll();
      }
      return Promise.resolve(false);
    }
    const handoff = PLAY_HANDOFF.run(plan.key, () => performSeriesHandoff(plan, automatic, {
      completedSeries,
      completedByCheck,
      unusedMoves,
      playSequence,
    }));
    if (state.mode === "play") {
      void handoff.then((advanced) => {
        if (advanced && state.mode === "play") void continuePlayFlow();
      });
    }
    return handoff;
  }

  async function continuePlayFlow() {
    if (
      state.mode !== "play"
      || !state.play.active
      || playSessionExternalUpdate
      || playSessionSaveBlocked
      || playReviewActive()
      || playGameEnded()
    ) return;
    if (activePlayEngineTurn) return activePlayEngineTurn;
    if (state.complete && state.nextState) {
      await advanceSeries(true);
      return;
    }
    const turn = maybeRunEngineTurn();
    activePlayEngineTurn = turn;
    try {
      await turn;
    } finally {
      if (activePlayEngineTurn === turn) activePlayEngineTurn = null;
    }
  }

  async function undoMove() {
    state.handoffNotice = null;
    state.viewingHistorical = true;
    if (state.prefix.length) {
      const node = treeNodeFromCursor();
      const targetId = node?.parentId ?? state.seriesParentNodeId;
      const payload = await refreshPrefix(state.prefix.slice(0, -1), state.prefixSan.slice(0, -1));
      if (payload) {
        state.currentTreeNodeId = targetId;
        persistStudy();
        renderStudyTree();
      }
      return;
    }
    const previous = state.history.pop();
    if (!previous) return;
    state.boundary = {
      ...previous.boundary,
      ep_targets: [...previous.boundary.ep_targets],
    };
    state.currentTreeNodeId = previous.treeNodeId ?? null;
    state.seriesParentNodeId = previous.seriesParentNodeId ?? null;
    const payload = await refreshPrefix(previous.prefix, previous.prefixSan);
    if (payload) {
      persistStudy();
      renderStudyTree();
      showToast(`Returned to series ${state.boundary.series}`);
    }
  }

  async function resetCurrentSeries() {
    const targetId = state.seriesParentNodeId;
    const payload = await refreshPrefix([], []);
    if (!payload) return;
    state.currentTreeNodeId = targetId;
    state.branching = false;
    state.viewingHistorical = false;
    state.handoffNotice = null;
    persistStudy();
    renderStudyTree();
  }

  async function beginNewVariation() {
    if (state.outcome) {
      showToast("Select an earlier move before starting a new line");
      return;
    }
    if (state.complete && state.nextState) await advanceSeries();
    state.branching = true;
    clearAnalysisDisplay();
    queueAutoAnalysis(120);
    renderStudyTree();
    dom.board.querySelector(`[data-square="${state.focusSquare}"]`)?.focus();
    showToast("New line ready — play a different legal move");
  }

  function variationDescendants(nodeId) {
    const ids = [];
    const queue = [nodeId];
    const seen = new Set();
    while (queue.length && ids.length <= MAX_STORED_NODES) {
      const current = queue.shift();
      if (!current || seen.has(current)) continue;
      seen.add(current);
      ids.push(current);
      treeChildren(current).forEach((child) => queue.push(child.id));
    }
    return ids;
  }

  async function deleteCurrentVariation() {
    const node = treeNodeFromCursor();
    if (!node) return;
    const targetId = node.parentId;
    const deleting = variationDescendants(node.id);
    const navigated = await navigateToTreeNode(targetId);
    if (!navigated) return;
    deleting.forEach((id) => { delete state.study.nodes[id]; });
    state.currentTreeNodeId = targetId;
    state.branching = false;
    persistStudy();
    renderStudyTree();
    showToast(`Deleted ${deleting.length} saved move${deleting.length === 1 ? "" : "s"}`);
  }

  async function clearStudyTree() {
    const count = Object.keys(state.study?.nodes || {}).length;
    if (count && !window.confirm(`Clear all ${count} saved moves from this local study?`)) return;
    const root = cloneBoundary(state.study?.rootBoundary || state.boundary);
    state.boundary = root;
    state.history = [];
    state.prefix = [];
    state.prefixSan = [];
    resetStudy(root);
    const payload = await refreshPrefix([], []);
    if (payload) persistStudy();
    showToast("Analysis tree cleared");
  }

  function switchTab(name) {
    const tabs = [...document.querySelectorAll("[role='tab']")];
    tabs.forEach((tab) => {
      const active = tab.id === `tab-${name}`;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(tab.getAttribute("aria-controls")).hidden = !active;
    });
  }

  function showToast(message) {
    window.clearTimeout(state.toastTimer);
    dom.toast.textContent = message;
    dom.toast.hidden = false;
    state.toastTimer = window.setTimeout(() => { dom.toast.hidden = true; }, 3200);
  }

  function applyCertifiedBrowserRuntime(runtime) {
    const rootReady = runtime.root_iteration_ready === true;
    const rootConfig = runtime.root_geometry?.session_config;
    const rootPlayLimits = runtime.root_geometry?.play_limits;
    if (rootReady && (
      !rootConfig
      || !rootPlayLimits
      || rootConfig.max_depth !== 5
      || rootConfig.width !== 32
      || !Number.isFinite(rootPlayLimits.maximum_seconds)
      || rootPlayLimits.maximum_seconds <= 0
      || !Number.isFinite(rootPlayLimits.default_seconds)
      || rootPlayLimits.default_seconds <= 0
      || rootPlayLimits.default_seconds > rootPlayLimits.maximum_seconds
      || !Number.isSafeInteger(rootPlayLimits.default_generation_positions)
      || rootPlayLimits.default_generation_positions < 1_000
      || rootPlayLimits.default_generation_positions > rootConfig.max_work
      || !Number.isSafeInteger(rootPlayLimits.safety_reserve_positions)
      || rootPlayLimits.safety_reserve_positions < 1
      || rootPlayLimits.safety_reserve_positions > rootConfig.max_work
    )) {
      throw new Error("The certified browser root play limits are incomplete.");
    }
    const rootLimits = rootReady ? {
      maximum_depth: Math.min(5, rootConfig.max_depth),
      maximum_max_series: rootConfig.width,
      maximum_seconds: rootPlayLimits.maximum_seconds,
      maximum_generation_positions: rootConfig.max_work,
      default_depth: Math.min(5, rootConfig.max_depth),
      default_max_series: rootConfig.width,
      default_seconds: rootPlayLimits.default_seconds,
      default_generation_positions: rootPlayLimits.default_generation_positions,
    } : null;
    const limits = rootReady ? rootLimits : runtime.analysis_limits;
    state.play.engineName = String(
      runtime.engine_profile_name || runtime.profile_id || "Progressive engine",
    );
    state.play.engineProfileId = runtime.engine_profile_id || runtime.profile_id;
    state.play.engineVersion = runtime.engine_version;
    state.play.rulesetVersion = runtime.ruleset_version;
    state.play.engineFingerprint = runtime.source_fingerprint;
    state.play.browserWasmReady = true;
    state.play.browserRootReady = runtime.root_iteration_ready === true;
    state.play.browserRootWorkerCount = runtime.root_iteration_ready === true
      ? Math.max(1, Math.floor(asNumber(
        runtime.root_geometry?.desktop_workers,
        1,
      )))
      : 0;
    state.play.browserWasmReason = null;
    state.play.browserWasmArtifact = runtime.wasm_sha256;
    state.play.runtimeMode = "browser-wasm";
    state.play.runtimeCpuCount = Math.max(
      1,
      Math.floor(asNumber(globalThis.navigator?.hardwareConcurrency, 1)),
    );
    state.play.runtimeCpuCountSource = "navigator.hardwareConcurrency";
    state.play.nativeThreads = Math.max(1, Math.floor(asNumber(runtime.thread_count, 1)));
    state.play.nativeThreadsPolicy = "browser-wasm-single";
    state.maximumAnalysisDepth = Math.max(1, Math.floor(asNumber(
      limits.maximum_depth,
      state.maximumAnalysisDepth,
    )));
    state.maximumAnalysisSeconds = Math.max(0.1, asNumber(
      limits.maximum_seconds,
      state.maximumAnalysisSeconds,
    ));
    state.maximumBranchCap = Math.max(1, Math.floor(asNumber(
      limits.maximum_max_series,
      state.maximumBranchCap,
    )));
    state.maximumGenerationPositions = Math.max(1_000, Math.floor(asNumber(
      limits.maximum_generation_positions,
      state.maximumGenerationPositions,
    )));
    state.play.recommendedDepth = Math.max(1, Math.floor(asNumber(
      limits.default_depth,
      state.play.recommendedDepth,
    )));
    state.play.recommendedBranchCap = Math.max(1, Math.floor(asNumber(
      limits.default_max_series,
      state.play.recommendedBranchCap,
    )));
    state.play.timeLimitSeconds = Math.max(0.1, Math.min(
      state.maximumAnalysisSeconds,
      asNumber(limits.default_seconds, state.play.timeLimitSeconds),
    ));
    state.play.generationPositions = Math.max(1_000, Math.min(
      state.maximumGenerationPositions,
      Math.floor(asNumber(
        limits.default_generation_positions,
        state.play.generationPositions,
      )),
    ));
    dom.depth_control.max = String(state.maximumAnalysisDepth);
    dom.cap_control.max = String(state.maximumBranchCap);
    dom.time_control.max = String(state.maximumAnalysisSeconds);
    dom.rules_version.textContent = String(runtime.ruleset_version);
    state.play.healthReady = true;
    dom.engine_status.classList.add("is-online");
    dom.engine_status.classList.remove("is-offline");
    dom.engine_status_text.textContent = runtime.value_model_active === true
      ? "Engine + value model on this device"
      : "Engine on this device";
    dom.engine_status.title = [
      runtime.engine_profile_name,
      runtime.engine_version,
      runtime.source_fingerprint,
      `WASM ${runtime.runtime_variant}`,
      `${runtime.thread_count} thread${runtime.thread_count === 1 ? "" : "s"}`,
      `certificate ${runtime.root_session_certificate_id || runtime.certificate_id}`,
      runtime.value_model_active === true
        ? `value model ${runtime.value_model_id} · ${runtime.value_model_sha256}`
        : runtime.value_model_status === "fallback"
          ? `value model rejected (${runtime.value_model_failure_code}); certified baseline active`
          : null,
    ].filter(Boolean).join(" · ");
    renderPlaySurface();
  }

  function browserRuntimeCanSearch(runtime) {
    return runtime?.ready === true
      && (runtime.analysis_ready === true || runtime.root_iteration_ready === true);
  }

  function browserRuntimeMatchesHostedIdentity(runtime, health) {
    if (runtime?.ready !== true) return false;
    const expected = [
      [first(health.source_fingerprint, health.fingerprint), runtime.source_fingerprint],
      [first(health.engine_version, health.version, null), runtime.engine_version],
      [first(health.ruleset_version, health.rules_version, health.ruleset, null), runtime.ruleset_version],
    ];
    return expected.every(([hosted, local]) => (
      typeof hosted === "string" && hosted.length > 0 && hosted === local
    ));
  }

  async function preflightCertifiedBrowserRuntime({ deadlineMs = null } = {}) {
    if (!browserEngineClient) return { ready: false, reason: "browser-kernel-unavailable" };
    try {
      // The Pages bundle is independently certified. Never bind its identity
      // to a hosted fallback that may still be deploying an older commit.
      return await browserEngineClient.preflight({ deadlineMs });
    } catch (error) {
      if (error?.name === "AbortError") throw error;
      return {
        ready: false,
        reason: error?.code === "browser-analysis-deadline"
          ? "browser-worker-timeout"
          : String(error?.code || "browser-kernel-unavailable"),
      };
    }
  }

  async function checkHealth() {
    try {
      const browserBootstrapDeadlineMs = monotonicNow() + LOCAL_ENGINE_BOOTSTRAP_TIMEOUT_MS;
      let browserRuntime = { ready: false, reason: "browser-kernel-unavailable" };
      if (browserEngineClient) {
        dom.engine_status.classList.remove("is-online", "is-offline");
        dom.engine_status_text.textContent = "Loading native engine…";
        browserRuntime = await preflightCertifiedBrowserRuntime({
          deadlineMs: Math.min(
            browserBootstrapDeadlineMs,
            monotonicNow() + LOCAL_ENGINE_FIRST_PROBE_TIMEOUT_MS,
          ),
        });
        if (
          browserRuntime.ready !== true
          && TRANSIENT_LOCAL_ENGINE_FAILURES.has(String(browserRuntime.reason || ""))
          && monotonicNow() < browserBootstrapDeadlineMs
        ) {
          dom.engine_status_text.textContent = "Restarting native engine…";
          browserRuntime = await preflightCertifiedBrowserRuntime({
            deadlineMs: browserBootstrapDeadlineMs,
          });
        }
        state.play.browserPrefixReady = browserRuntime.ready === true
          && browserRuntime.prefix_ready === true;
        state.play.browserRootReady = browserRuntime.ready === true
          && browserRuntime.root_iteration_ready === true;
        if (browserRuntimeCanSearch(browserRuntime)) {
          applyCertifiedBrowserRuntime(browserRuntime);
          return;
        }
        state.play.browserWasmReady = false;
        state.play.browserWasmReason = String(
          browserRuntime.reason || "browser-kernel-unavailable",
        );
      }
      if (isPublicPagesSite) {
        dom.engine_status.classList.remove("is-online", "is-offline");
        dom.engine_status_text.textContent = "Waking engine…";
      }
      let health;
      let wakeAttempt = 0;
      while (true) {
        const controller = new AbortController();
        const timeout = isPublicPagesSite
          ? window.setTimeout(() => controller.abort(), PUBLIC_HEALTH_TIMEOUT_MS)
          : null;
        try {
          health = await requestJson("/api/health", { signal: controller.signal });
          if (health.ok !== true || !health.source_fingerprint) {
            const error = new Error("The engine health response is incomplete.");
            error.code = "invalid-api-response";
            throw error;
          }
          break;
        } catch (error) {
          if (
            !isPublicServiceWakeError(error, { includeAbort: true })
            || wakeAttempt >= PUBLIC_HEALTH_WAKE_DELAYS_MS.length
          ) throw error;
          const delay = PUBLIC_HEALTH_WAKE_DELAYS_MS[wakeAttempt];
          wakeAttempt += 1;
          dom.engine_status_text.textContent = "Waking engine…";
          dom.engine_status.title = `Hosted engine is starting · retry ${wakeAttempt}`;
          await waitForRetry(delay);
        } finally {
          if (timeout !== null) window.clearTimeout(timeout);
        }
      }
      dom.engine_status.classList.add("is-online");
      dom.engine_status.classList.remove("is-offline");
      const profileName = first(health.engine_profile_name, health.profile_name);
      state.play.engineName = String(profileName || "Current champion");
      state.play.engineProfileId = first(health.engine_profile_id, null);
      state.play.engineVersion = first(health.engine_version, health.version, null);
      state.play.engineFingerprint = first(health.source_fingerprint, health.fingerprint, null);
      state.play.runtimeCpuCount = first(health.runtime?.cpu_count, null);
      state.play.runtimeCpuCountSource = first(health.runtime?.cpu_count_source, null);
      state.play.nativeThreads = Math.max(1, Math.floor(asNumber(
        first(health.runtime?.native_threads, health.analysis_limits?.native_threads),
        1,
      )));
      state.play.nativeThreadsPolicy = first(
        health.runtime?.native_threads_policy,
        null,
      );
      const rulesetVersion = first(
        health.ruleset_version,
        health.rules_version,
        health.ruleset,
      );
      state.play.rulesetVersion = rulesetVersion;
      if (
        browserRuntime.ready === true
        && !browserRuntimeMatchesHostedIdentity(browserRuntime, health)
      ) {
        browserEngineClient.close("browser/server engine identities differ");
        browserRuntime = {
          ready: false,
          reason: "browser-hosted-engine-identity-mismatch",
        };
      }
      state.play.browserWasmReady = browserRuntime.ready === true
        && (browserRuntime.analysis_ready === true
          || browserRuntime.root_iteration_ready === true);
      state.play.browserRootReady = browserRuntime.ready === true
        && browserRuntime.root_iteration_ready === true;
      state.play.browserRootWorkerCount = state.play.browserRootReady
        ? Math.max(1, Math.floor(asNumber(
          browserRuntime.root_geometry?.desktop_workers,
          1,
        )))
        : 0;
      state.play.browserPrefixReady = browserRuntime.ready === true
        && browserRuntime.prefix_ready === true;
      state.play.browserWasmReason = state.play.browserWasmReady
        ? null
        : browserRuntime.ready
          ? "browser-search-not-certified"
          : String(browserRuntime.reason || "browser-kernel-unavailable");
      state.play.browserWasmArtifact = browserRuntime.ready
        ? browserRuntime.wasm_sha256
        : null;
      if (state.play.browserWasmReady) {
        state.play.runtimeMode = "browser-wasm";
        state.play.runtimeCpuCount = Math.max(
          1,
          Math.floor(asNumber(globalThis.navigator?.hardwareConcurrency, 1)),
        );
        state.play.runtimeCpuCountSource = "navigator.hardwareConcurrency";
        state.play.nativeThreads = Math.max(
          1,
          Math.floor(asNumber(browserRuntime.thread_count, 1)),
        );
        state.play.nativeThreadsPolicy = "browser-wasm-single";
      } else {
        state.play.runtimeMode = "server";
      }
      state.play.recommendedDepth = Math.max(1, Math.floor(asNumber(
        health.engine_profile_recommended_depth,
        state.play.recommendedDepth,
      )));
      state.play.recommendedBranchCap = Math.max(1, Math.floor(asNumber(
        health.engine_profile_recommended_branch_cap,
        state.play.recommendedBranchCap,
      )));
      dom.engine_status_text.textContent = first(health.status, "ok") === "ok"
        ? state.play.browserWasmReady ? "Engine on this device" : "Engine online"
        : String(first(health.status, "Engine online"));
      const version = rulesetVersion;
      if (version) dom.rules_version.textContent = String(version);
      const maximumSeconds = first(health.analysis_limits?.maximum_seconds, health.analysis_limits?.max_seconds);
      if (maximumSeconds !== undefined) {
        state.maximumAnalysisSeconds = Math.max(0.1, asNumber(maximumSeconds, 30));
        dom.time_control.max = String(state.maximumAnalysisSeconds);
        ANALYSIS_PRESETS.strong.seconds = Math.min(
          ANALYSIS_PRESETS.strong.seconds,
          state.maximumAnalysisSeconds,
        );
        if (state.analysisPreset === "strong") applyAnalysisPreset("strong", false);
      }
      const maximumDepth = health.analysis_limits?.maximum_depth;
      if (maximumDepth !== undefined) {
        state.maximumAnalysisDepth = Math.max(1, Math.floor(asNumber(maximumDepth, 8)));
        dom.depth_control.max = String(state.maximumAnalysisDepth);
        ANALYSIS_PRESETS.strong.depth = state.maximumAnalysisDepth;
        if (state.analysisPreset === "strong") dom.depth_control.value = String(state.maximumAnalysisDepth);
        updateAnalysisProgress();
      }
      const maximumBranchCap = first(
        health.analysis_limits?.maximum_max_series,
        health.analysis_limits?.maximum_series,
        health.analysis_limits?.max_series,
      );
      if (maximumBranchCap !== undefined) {
        state.maximumBranchCap = Math.max(1, Math.floor(asNumber(maximumBranchCap, 512)));
        dom.cap_control.max = String(state.maximumBranchCap);
        ANALYSIS_PRESETS.strong.cap = Math.min(ANALYSIS_PRESETS.strong.cap, state.maximumBranchCap);
        if (asNumber(dom.cap_control.value, state.maximumBranchCap) > state.maximumBranchCap) {
          dom.cap_control.value = String(state.maximumBranchCap);
        }
      }
      const maximumAlternatives = health.analysis_limits?.maximum_alternatives;
      if (maximumAlternatives !== undefined) {
        state.maximumAlternatives = Math.max(0, Math.floor(asNumber(maximumAlternatives, 32)));
        dom.alternatives_control.min = "0";
        dom.alternatives_control.max = String(Math.min(12, state.maximumAlternatives));
        ANALYSIS_PRESETS.strong.alternatives = Math.min(ANALYSIS_PRESETS.strong.alternatives, state.maximumAlternatives);
        if (asNumber(dom.alternatives_control.value, state.maximumAlternatives) > state.maximumAlternatives) {
          dom.alternatives_control.value = String(Math.min(3, state.maximumAlternatives));
        }
      }
      const maximumGenerationPositions = health.analysis_limits?.maximum_generation_positions;
      if (maximumGenerationPositions !== undefined) {
        state.maximumGenerationPositions = Math.max(1_000, Math.floor(asNumber(maximumGenerationPositions, 10_000_000)));
        ANALYSIS_PRESETS.strong.generationPositions = state.maximumGenerationPositions;
      }
      state.play.timeLimitSeconds = Math.max(0.1, Math.min(
        state.maximumAnalysisSeconds,
        asNumber(health.analysis_limits?.default_seconds, 5),
      ));
      state.play.generationPositions = Math.max(1_000, Math.min(
        state.maximumGenerationPositions,
        Math.floor(asNumber(
          health.analysis_limits?.default_generation_positions,
          500_000,
        )),
      ));
      state.play.healthReady = true;
      dom.engine_status.title = [
        profileName,
        first(health.engine_version, health.version),
        first(health.source_fingerprint, health.fingerprint),
        state.play.browserWasmReady
          ? "certified on-device WebAssembly"
          : state.play.browserPrefixReady
            ? "hosted search · certified local move validation"
            : "hosted engine fallback",
        state.play.browserWasmReady
          ? null
          : `local engine: ${state.play.browserWasmReason}`,
      ].filter(Boolean).join(" · ");
      renderPlaySurface();
    } catch (error) {
      dom.engine_status.classList.add("is-offline");
      dom.engine_status.classList.remove("is-online");
      dom.engine_status_text.textContent = "Engine offline";
      dom.engine_status.title = displayError(error);
    }
  }

  function bindEvents() {
    dom.board.addEventListener("click", onBoardClick);
    dom.board.addEventListener("pointerdown", onPointerDown);
    dom.board.addEventListener("keydown", onBoardKeydown);
    window.addEventListener("pointermove", onPointerMove, { passive: false });
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", endDrag);
    window.addEventListener("storage", (event) => {
      if (event.key !== PLAY_SESSION_STORAGE_KEY || typeof event.newValue !== "string") return;
      try {
        const saved = sanitizeStoredPlaySession(JSON.parse(event.newValue));
        if (!saved || saved.ownerId === playSessionTabId || !state.play.active) return;
        const current = persistedPlaySessionWithoutSideEffects();
        if (
          !current
          || current.sessionId !== saved.sessionId
          || current.ownerId !== saved.ownerId
          || current.revision !== saved.revision
          || !sameStoredPlayState(current, saved)
        ) return;
        const sessionReplaced = saved.sessionId !== state.play.sessionId;
        const sameSessionAdvanced = !sessionReplaced
          && saved.revision > playSessionRevision;
        if (sessionReplaced || sameSessionAdvanced) lockPlaySessionForExternalUpdate();
      } catch {
        // The next write still passes the schema, size, and ledger guards.
      }
    });

    dom.mode_play.addEventListener("click", () => { void switchWorkspaceMode("play"); });
    dom.mode_analyze.addEventListener("click", () => { void switchWorkspaceMode("analyze"); });
    dom.play_as_white.addEventListener("click", () => { void selectPlayColor("white"); });
    dom.play_as_black.addEventListener("click", () => { void selectPlayColor("black"); });
    dom.play_strength_strong.addEventListener("click", () => { void selectPlayStrength("strong"); });
    dom.play_strength_faster.addEventListener("click", () => { void selectPlayStrength("faster"); });
    dom.play_new_game.addEventListener("click", () => { void startNewPlayGame(); });
    dom.play_retry_engine.addEventListener("click", () => { void retryEngineTurn(); });
    dom.play_analyze_position.addEventListener("click", () => { void switchWorkspaceMode("analyze", { importPlayPosition: true }); });
    dom.play_resign.addEventListener("click", () => { void resignPlayGame(); });
    dom.play_history_previous.addEventListener("click", () => { void stepPlayTimeline(-1); });
    dom.play_history_next.addEventListener("click", () => { void stepPlayTimeline(1); });

    dom.flip_board.addEventListener("click", () => {
      if (state.mode === "play" && blockStalePlayMutation()) return;
      state.flipped = !state.flipped;
      renderBoard();
      if (state.mode === "play") captureAndPersistPlayWorkspace();
    });
    dom.undo_move.addEventListener("click", undoMove);
    dom.reset_series.addEventListener("click", resetCurrentSeries);
    dom.advance_series.addEventListener("click", () => advanceSeries(false));
    dom.analyze_button.addEventListener("click", toggleAutoAnalysis);
    dom.preset_quick.addEventListener("click", () => applyAnalysisPreset("quick"));
    dom.preset_strong.addEventListener("click", () => applyAnalysisPreset("strong"));
    [dom.depth_control, dom.cap_control, dom.time_control, dom.alternatives_control]
      .forEach((input) => input.addEventListener("change", markPresetCustom));
    dom.new_variation.addEventListener("click", beginNewVariation);
    dom.delete_variation.addEventListener("click", deleteCurrentVariation);
    dom.clear_study.addEventListener("click", clearStudyTree);
    dom.save_position.addEventListener("click", () => openSavedPositions(true));
    dom.load_position.addEventListener("click", () => openSavedPositions(false));
    dom.saved_dialog_close.addEventListener("click", () => dom.saved_dialog.close());
    dom.save_position_form.addEventListener("submit", saveCurrentPosition);
    dom.analysis_tree.addEventListener("keydown", (event) => {
      if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
      const items = [...dom.analysis_tree.querySelectorAll("[role='treeitem']")];
      const current = items.indexOf(document.activeElement);
      if (current < 0 || !items.length) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0
        : event.key === "End" ? items.length - 1
          : event.key === "ArrowUp" ? Math.max(0, current - 1)
            : Math.min(items.length - 1, current + 1);
      items[next].focus();
    });
    dom.pv_previous.addEventListener("click", () => stepPv(-1));
    dom.pv_next.addEventListener("click", () => stepPv(1));
    dom.pv_exit.addEventListener("click", () => exitPvPreview());
    dom.refresh_openings.addEventListener("click", loadOpenings);
    dom.setup_form.addEventListener("submit", loadSetup);
    dom.load_start.addEventListener("click", () => {
      dom.fen_input.value = START_FEN;
      dom.series_input.value = "1";
      dom.quiet_input.value = "0";
      dom.ep_input.value = "";
      loadSetup();
    });

    const tabs = [...document.querySelectorAll("[role='tab']")];
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => switchTab(tab.id.replace("tab-", "")));
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        let next = index;
        if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
        if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = tabs.length - 1;
        switchTab(tabs[next].id.replace("tab-", ""));
        tabs[next].focus();
      });
    });
    document.addEventListener("keydown", (event) => {
      const editing = event.target?.matches?.("input, textarea, select, [contenteditable='true']");
      const boardNavigation = event.target?.closest?.("#board");
      if (
        state.mode === "play"
        && !editing
        && !boardNavigation
        && !event.altKey
        && !event.ctrlKey
        && !event.metaKey
        && (event.key === "ArrowLeft" || event.key === "ArrowRight")
      ) {
        event.preventDefault();
        void stepPlayTimeline(event.key === "ArrowLeft" ? -1 : 1);
        return;
      }
      if (state.mode === "analyze" && event.key.toLowerCase() === "f" && !event.target.matches("input, textarea")) {
        state.flipped = !state.flipped;
        renderBoard();
      }
    });
  }

  async function initialize() {
    const savedCursor = restoreStudy();
    restoreSavedPositions();
    bindEvents();
    applyAnalysisPreset("strong", false);
    renderAll();
    clearAnalysisDisplay();
    await checkHealth();
    loadOpenings();
    const cursorNode = state.currentTreeNodeId ? state.study?.nodes[state.currentTreeNodeId] : null;
    const cursorIsOnNode = Boolean(
      cursorNode
      && savedCursor.prefix.length
      && boundaryKey(cursorNode.boundary) === boundaryKey(state.boundary)
      && sameSeries(cursorNode.prefix, savedCursor.prefix),
    );
    const restored = cursorIsOnNode
      ? await navigateToTreeNode(state.currentTreeNodeId)
      : await refreshPrefix(savedCursor.prefix, savedCursor.san);
    if (
      restored
      && !savedCursor.prefix.length
      && cursorNode?.complete
      && state.boundary.series === cursorNode.series + 1
    ) {
      state.history = pathToTreeNode(cursorNode.id)
        .filter((node) => node.complete)
        .map((node) => ({
          boundary: cloneBoundary(node.boundary),
          prefix: [...node.prefix],
          prefixSan: [...node.prefix],
          treeNodeId: node.id,
          seriesParentNodeId: node.seriesParentId,
        }));
      renderPositionStatus();
    }
    if (!restored) {
      const root = cloneBoundary(state.study.rootBoundary);
      state.boundary = root;
      resetStudy(root);
      await refreshPrefix([], []);
      showToast("Saved cursor was invalid, so the study reopened at its checked start");
    } else {
      persistStudy();
      renderStudyTree();
    }
    state.analysisWorkspace = captureWorkspace();
    if (!await restorePersistedPlaySession()) {
      await startNewPlayGame({ announce: false });
    }
  }

  void initialize();
})();
