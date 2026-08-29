(() => {
  "use strict";

  const ROOT_API = globalThis.ScottishProgressiveRootCoordinator || null;
  const PREFIX_API = globalThis.ScottishProgressiveBrowserPrefix || null;
  const SOURCE_FINGERPRINT = /^[0-9a-f]{16}$/;
  const ARTIFACT_FINGERPRINT = /^[0-9a-f]{64}$/;
  const UCI_MOVE = /^[a-h][1-8][a-h][1-8][qrbn]?$/;
  const MATE_SCORE = 1_000_000;
  const MAX_LOCAL_DEPTH = 5;
  const PV_HORIZON_MATE_WORK_LIMIT = 3_500_000;
  const ROOT_CURRENT_SERIES_MATE_WORK_LIMIT = 250_000;
  const ROOT_CURRENT_SERIES_MATE_WORK_DENOMINATOR = 64;
  const ROOT_CURRENT_SERIES_MATE_MIN_WORK = 1_000;
  const ROOT_CURRENT_SERIES_MATE_MIN_TOTAL_WORK = 500_000;
  const ROOT_CURRENT_SERIES_MATE_MIN_SERIES = 5;
  const ROOT_CURRENT_SERIES_MATE_TIME_LIMIT_MS = 1_000;
  const ROOT_CURRENT_SERIES_MATE_TIME_DENOMINATOR = 10;
  const ROOT_CURRENT_SERIES_MATE_RECEIPT_GRACE_MS = 100;
  const ASPIRATION_INITIAL_DELTA = 2_048;
  const MAX_ASPIRATION_ATTEMPTS = 4;
  const SAFE_ROOT_RESELECT_WIDTH = 512;
  const SAFE_ROOT_RESELECT_TOTAL_WORK = 40_000_000;
  const SAFE_ROOT_RESELECT_EARLY_FRONTIER_COUNT = 32;
  const SAFE_ROOT_RESELECT_EARLY_CHILD_WORK = 3_000_000;
  const SAFE_ROOT_RESELECT_WIDENED_CHILD_WORK = 10_000_000;
  const SAFE_ROOT_RESELECT_REPLY_MATE_SCOPE =
    "selected-child-immediate-reply-mate-only";
  const SAFE_ROOT_RESELECT_LADDER_SCOPE =
    "selected-child-immediate-reply-mate-plus-single-reply-ladder";
  const SAFE_ROOT_RESELECT_TERMINAL_SCOPE =
    "selected-root-terminal-non-loss-only";
  const SAFE_ROOT_RESELECT_POLICY =
    "first-authoritatively-reply-mate-safe-in-production-order-v1";
  const SAFE_ROOT_RESELECT_ORDER_POLICY =
    "native-tactical-protected-root-production-order-empty-preference-v1";
  const SAFE_ROOT_RESELECT_EXCLUSION_BINDING_POLICY =
    "unique-exact-authoritative-child-boundary-v1";
  const SELECTED_ROOT_LADDER_MIN_CHILD_SERIES = 7;
  const SELECTED_ROOT_LADDER_WORK_LIMIT = 1_000_000;
  const SELECTED_ROOT_LADDER_SCOPE =
    "selected-root-single-reply-mate-ladder";
  const SELECTED_ROOT_LADDER_PROOF_SCHEMA =
    "spc-single-reply-mate-ladder-proof-v1";
  const SELECTED_ROOT_LADDER_RECEIPT_SCHEMA =
    "spc-selected-root-single-reply-mate-ladder-receipt-v1";
  const ROOT_TACTICAL_POLICY = "canonical-boundary-policy-v1";
  const CHECKED_PV_SELECTION_POLICY =
    "repair-once-then-veto-adverse-selected-pv-boundary-mates-v2";
  const MATE_CLAIM_SELECTION_POLICY =
    "require-sign-matching-exact-proof-for-nonterminal-mate-band-v1";
  const MAX_SAME_ROOT_HORIZON_REPAIRS = 1;
  const SAME_ROOT_REPAIR_POLICY = Object.freeze({
    schema: "spc-same-root-horizon-repair-policy-v1",
    maximum_successful_same_root_repairs: MAX_SAME_ROOT_HORIZON_REPAIRS,
  });
  const EMPTY_PV_HORIZON_POLICY_VETOES = Object.freeze([]);
  const EMPTY_MATE_CLAIM_QUARANTINE_RECEIPTS = Object.freeze([]);
  const PV_HORIZON_POLICY_VETO_REASONS = new Set([
    "duplicate-horizon-proof", "missing-horizon-proof", "owner-recertification-failed",
    "repair-proof-not-hit", "repair-unsupported", "repair-work-limit",
    "retained-proof-capacity", "same-root-repair-limit",
  ]);
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

  function classifyRootFailure(error, lastSafeWork) {
    const codes = new Set();
    const seen = new Set();
    let attemptedWork = lastSafeWork;
    let current = error;
    while (
      current !== null
      && (typeof current === "object" || typeof current === "function")
      && !seen.has(current)
    ) {
      seen.add(current);
      if (typeof current.code === "string") codes.add(current.code);
      if (exactInteger(current.work?.committed_work, 0)) {
        attemptedWork = Math.max(attemptedWork, current.work.committed_work);
      }
      current = current.cause;
    }
    const timedOut = codes.has("root-deadline") || codes.has("browser-root-deadline");
    const workLimitReached = !timedOut && (
      codes.has("root-work-limit") || codes.has("browser-root-work-limit")
    );
    return Object.freeze({ timedOut, workLimitReached, attemptedWork });
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

  const EXACT_OWNER_PRIORITIES = Object.freeze(new Map([
    ["aspiration", 3],
    ["full", 3],
    ["threat-research", 2],
    ["selected-certification", 1],
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
      const priority = EXACT_OWNER_PRIORITIES.get(item.purpose);
      if (item.bound !== "exact" || priority === undefined) continue;
      const prior = owners.get(item.candidate_identity);
      if (prior === undefined || priority > prior.priority) {
        owners.set(item.candidate_identity, {
          priority,
          workerId: item.worker_id,
        });
      }
    }
    return new Map([...owners].map(([candidateId, owner]) => (
      [candidateId, owner.workerId]
    )));
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

  function deepFreeze(value) {
    if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
    Object.values(value).forEach(deepFreeze);
    return Object.freeze(value);
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
    return sameArray(proofBounds, [expected, expected]);
  }

  function normalizeMateClaimQuarantineReceipts(
    value,
    { candidateIds, expectedCount },
  ) {
    if (!Array.isArray(value) || !(candidateIds instanceof Set)) return null;
    const seen = new Set();
    let observedCount = 0;
    const receipts = [];
    for (const entry of value) {
      const keys = [
        "candidate_identity", "currently_quarantined", "proof_bounds",
        "quarantine_count", "score",
      ];
      if (
        !entry
        || typeof entry !== "object"
        || Array.isArray(entry)
        || !sameJson(Object.keys(entry).sort(), keys)
        || typeof entry.candidate_identity !== "string"
        || !candidateIds.has(entry.candidate_identity)
        || seen.has(entry.candidate_identity)
        || !exactInteger(entry.quarantine_count, 1)
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
      receipts.push(Object.freeze({
        ...entry,
        proof_bounds: Object.freeze([...entry.proof_bounds]),
      }));
    }
    if (observedCount !== expectedCount) return null;
    return Object.freeze(receipts);
  }

  function normalizeSafeRootReselectExclusions(error, manifest) {
    const details = error?.details;
    const candidates = manifest?.candidates;
    const entries = details?.excluded_candidates;
    const expectedDetailKeys = ["excluded_candidates", "excluded_count", "schema"];
    const expectedEntryKeys = [
      "checked_pv_policy_rejected", "mate_claim_quarantine_count",
      "source_candidate_identity", "source_child_boundary", "source_order_index",
      "source_order_key", "source_root_machine_notation",
    ];
    if (
      !details
      || typeof details !== "object"
      || Array.isArray(details)
      || !sameJson(Object.keys(details).sort(), expectedDetailKeys)
      || details.schema !== "spc-root-safety-widening-exclusions-v2"
      || !Array.isArray(entries)
      || !Array.isArray(candidates)
      || details.excluded_count !== entries.length
      || entries.length > candidates.length
    ) {
      throw new RootIterationClientError(
        "The widening trigger omitted its exact rejected-root exclusions.",
        "browser-root-safe-reselector-exclusions-invalid",
      );
    }
    const seenBoundaries = new Set();
    const seenIdentities = new Set();
    const seenSeries = new Set();
    const normalized = entries.map((entry, index) => {
      const current = candidates[entry?.source_order_index];
      const sourceChildBoundary = normalizeExactBoundaryState(
        entry?.source_child_boundary || {},
      );
      const boundaryIdentity = sourceChildBoundary === null
        ? null
        : JSON.stringify(sourceChildBoundary);
      if (
        !entry
        || typeof entry !== "object"
        || Array.isArray(entry)
        || !sameJson(Object.keys(entry).sort(), expectedEntryKeys)
        || typeof entry.source_candidate_identity !== "string"
        || !entry.source_candidate_identity
        || seenIdentities.has(entry.source_candidate_identity)
        || typeof entry.source_root_machine_notation !== "string"
        || !entry.source_root_machine_notation
        || seenSeries.has(entry.source_root_machine_notation)
        || sourceChildBoundary === null
        || seenBoundaries.has(boundaryIdentity)
        || !exactInteger(entry.source_order_index, 0, candidates.length - 1)
        || (index > 0 && entry.source_order_index <= entries[index - 1].source_order_index)
        || typeof entry.source_order_key !== "string"
        || entry.source_order_key !== entry.source_root_machine_notation
        || typeof entry.checked_pv_policy_rejected !== "boolean"
        || !exactInteger(entry.mate_claim_quarantine_count, 0, 1_000_000)
        || (
          entry.checked_pv_policy_rejected !== true
          && entry.mate_claim_quarantine_count === 0
        )
        || current?.candidate_identity !== entry.source_candidate_identity
        || current.order_index !== entry.source_order_index
        || current.order_key !== entry.source_order_key
        || current.root_series?.machine_notation !== entry.source_root_machine_notation
        || !sameExactBoundary(current.root_series?.child_boundary, sourceChildBoundary)
      ) {
        throw new RootIterationClientError(
          "A widening exclusion was stale or not bound to the retained root manifest.",
          "browser-root-safe-reselector-exclusions-invalid",
        );
      }
      seenBoundaries.add(boundaryIdentity);
      seenIdentities.add(entry.source_candidate_identity);
      seenSeries.add(entry.source_root_machine_notation);
      return Object.freeze({
        ...entry,
        source_child_boundary: sourceChildBoundary,
      });
    });
    return Object.freeze(normalized);
  }

  function normalizeSameRootRepairPolicy(value) {
    if (
      !value
      || typeof value !== "object"
      || Array.isArray(value)
      || !sameJson(Object.keys(value).sort(), [
        "maximum_successful_same_root_repairs", "schema",
      ])
      || value.schema !== SAME_ROOT_REPAIR_POLICY.schema
      || value.maximum_successful_same_root_repairs
        !== MAX_SAME_ROOT_HORIZON_REPAIRS
    ) return null;
    return SAME_ROOT_REPAIR_POLICY;
  }

  function normalizePvHorizonPolicyVetoes(
    value,
    { candidateIds, expectedCount, maximumProofs },
  ) {
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
        || !sameJson(Object.keys(entry).sort(), expectedKeys)
        || entry.schema !== "spc-pv-horizon-candidate-veto-v1"
        || typeof entry.candidate_identity !== "string"
        || !candidateIds.has(entry.candidate_identity)
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
      normalized.push(Object.freeze({ ...entry }));
    }
    return Object.freeze(normalized);
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
      && identity?.root_session_contract?.capabilities?.checked_horizon_proof_research
        === true
      && identity?.root_session_contract?.request_schemas?.search
        === "spc-root-candidate-task-v1"
      && identity?.root_session_contract?.request_schemas?.horizon_research
        === "spc-root-horizon-research-task-v1"
      && identity?.root_session_contract?.result_schemas?.search
        === "spc-root-candidate-result-v1"
      && identity?.root_session_contract?.result_schemas?.horizon_research
        === "spc-root-horizon-research-result-v1"
      && identity?.root_session_contract?.hard_limits?.maximum_horizon_proofs === 16
      && identity?.root_session_contract?.hard_limits?.maximum_horizon_proof_path === 8
      && identity?.root_session_contract?.horizon_research?.task_schema
        === "spc-root-horizon-research-task-v1"
      && identity?.root_session_contract?.horizon_research?.result_schema
        === "spc-root-horizon-research-result-v1"
      && identity?.root_session_contract?.horizon_research?.proof_schema
        === "spc-retained-root-horizon-proof-v1"
      && identity?.root_session_contract?.horizon_research?.purpose === "horizon-research"
      && identity?.root_session_contract?.horizon_research?.full_window === true
      && identity?.root_session_contract?.horizon_research?.tt_persistence === "commit"
      && identity?.root_session_contract?.horizon_research?.hit_mask_order
        === "request-order"
      && identity?.root_session_contract?.horizon_research?.warm_exact_zero_hit_allowed
        === true
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

  function ladderProofCacheKey(identity, childBoundary) {
    return JSON.stringify(canonicalJsonValue({
      schema: "spc-root-single-reply-mate-ladder-cache-key-v1",
      exact_mate_cache_key: JSON.parse(mateProofCacheKey(identity, childBoundary)),
      ladder_abi_version: 1,
      proof_schema: SELECTED_ROOT_LADDER_PROOF_SCHEMA,
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
      this.workerTerminated = false;
      this.workerTerminationErrorCode = null;
      this.sessionReady = false;
      this.sessionId = null;
      this.canonicalRootTacticalProtection = null;
      this.nativeWorkAfter = 0;
      this.memoryBytes = 0;
      this.memoryPeakBytes = 0;
    }

    _spawn() {
      if (this.closed) {
        throw new RootIterationClientError(
          "The root Worker channel is closed.",
          "browser-root-worker-closed",
        );
      }
      if (this.worker) return this.worker;
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
      this.workerTerminated = false;
      this.workerTerminationErrorCode = null;
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
      if (this.closed && !this.worker) return this.workerTerminated;
      this.closed = true;
      const worker = this.worker;
      this.worker = null;
      this.sessionReady = false;
      this.sessionId = null;
      this.canonicalRootTacticalProtection = null;
      try {
        if (worker !== null) worker.terminate();
        this.worker = null;
        this.workerTerminated = true;
        this.workerTerminationErrorCode = null;
      } catch (cause) {
        this.worker = worker;
        this.workerTerminated = false;
        this.workerTerminationErrorCode = String(
          cause?.code || "browser-root-worker-termination-failed",
        );
      }
      for (const [id, entry] of this.pending) {
        this.pending.delete(id);
        entry.cleanup();
        entry.reject(error);
      }
      return this.workerTerminated;
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

  function normalizeSingleReplyLadderProof(value, rootChildBoundary) {
    const rootChild = normalizeExactBoundaryState(rootChildBoundary || {});
    const expectedKeys = [
      "attack", "forced_reply", "forced_reply_unique_legal_move", "mate",
      "root_child_boundary", "schema",
    ];
    if (
      rootChild === null
      || !value
      || typeof value !== "object"
      || Array.isArray(value)
      || !sameJson(Object.keys(value).sort(), expectedKeys)
      || value.schema !== SELECTED_ROOT_LADDER_PROOF_SCHEMA
      || value.forced_reply_unique_legal_move !== true
      || !sameExactBoundary(value.root_child_boundary, rootChild)
    ) {
      throw new RootIterationClientError(
        "The single-reply ladder proof is not rooted at the selected child.",
        "browser-root-ladder-proof-invalid",
      );
    }
    const attack = normalizeRootSeries(value.attack);
    const forcedReply = normalizeRootSeries(value.forced_reply);
    const mate = normalizeRootSeries(value.mate);
    if (
      rootChild.series < SELECTED_ROOT_LADDER_MIN_CHILD_SERIES
      || rootChild.series > 254
      || attack.moves.length > rootChild.series
      || attack.outcome !== null
      || attack.ended_by_check !== true
      || attack.child_boundary.series !== rootChild.series + 1
      || forcedReply.moves.length !== 1
      || forcedReply.outcome !== null
      || forcedReply.ended_by_check !== true
      || forcedReply.child_boundary.series !== rootChild.series + 2
      || mate.moves.length > rootChild.series + 2
      || mate.outcome !== "checkmate"
      || mate.ended_by_check !== true
      || mate.child_boundary.series !== rootChild.series + 3
    ) {
      throw new RootIterationClientError(
        "The single-reply ladder proof is not check, unique countercheck, then mate.",
        "browser-root-ladder-proof-invalid",
      );
    }
    return deepFreeze({
      schema: SELECTED_ROOT_LADDER_PROOF_SCHEMA,
      root_child_boundary: rootChild,
      forced_reply_unique_legal_move: true,
      attack,
      forced_reply: forcedReply,
      mate,
    });
  }

  function selectedPvOpponentBoundaries(candidate) {
    const rawPv = candidate?.child_pv;
    if (!Array.isArray(rawPv)) return Object.freeze([]);
    const pv = rawPv.map(normalizeRootSeries);
    const rootedLength = pv.length + 1;
    const boundaries = [];
    // A complete series flips the mover. Starting at the deepest exact PV
    // prefix, only odd rooted-path lengths leave the opponent to move.
    for (
      let pathLength = rootedLength % 2 === 1 ? rootedLength : rootedLength - 1;
      pathLength >= 1;
      pathLength -= 2
    ) {
      const prefix = pathLength === 1 ? [] : pv.slice(0, pathLength - 1);
      const leaf = pathLength === 1 ? candidate?.root_series : prefix.at(-1);
      if (leaf?.outcome !== null) continue;
      boundaries.push(Object.freeze({
        pv: Object.freeze(prefix),
        matePly: pathLength + 1,
      }));
    }
    return Object.freeze(boundaries);
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
      this.ladderProofCache = new Map();
      this.ladderProofCacheLimit = 256;
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
      const teardown = this._closePool(new RootIterationClientError(
        reason,
        "browser-root-pool-released",
      ));
      if (!teardown.complete) {
        throw this._poolTeardownError(
          teardown,
          "The certified root pool could not be released completely.",
          "browser-root-pool-release-failed",
        );
      }
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
      const failures = [];
      let terminatedWorkers = 0;
      for (const channel of pool) {
        const terminated = channel.close(error);
        if (terminated) {
          terminatedWorkers += 1;
        } else {
          failures.push(channel);
        }
      }
      this.pool = failures;
      return deepFreeze({
        schema: "spc-root-pool-teardown-receipt-v1",
        attempted_workers: pool.length,
        terminated_workers: terminatedWorkers,
        complete: failures.length === 0,
        failures: failures.map((channel) => ({
          worker_id: channel.id,
          error_code: channel.workerTerminationErrorCode
            || "browser-root-worker-termination-failed",
        })),
      });
    }

    _poolTeardownError(receipt, message, code) {
      const error = new RootIterationClientError(message, code);
      Object.defineProperty(error, "ordinary_pool_teardown_receipt", {
        configurable: false,
        enumerable: true,
        writable: false,
        value: receipt,
      });
      return error;
    }

    async _ensurePool(identity, deadlineMs, signal) {
      const expected = rootIdentity(identity);
      const geometry = selectCertifiedGeometry(identity, this.navigatorValue);
      const key = this._poolKey(identity, geometry);
      if (this.pool.length && this.poolIdentity === key) return { expected, geometry };
      if (this.pool.length) {
        const teardown = this._closePool(new RootIterationClientError(
          "The root Worker identity changed.",
          "browser-root-worker-incompatible",
        ));
        if (!teardown.complete) {
          throw this._poolTeardownError(
            teardown,
            "The prior root Worker pool could not be terminated.",
            "browser-root-worker-termination-failed",
          );
        }
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
          || response.capabilities?.checked_horizon_proof_research !== true
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

    async _replaySingleReplyLadderProof({
      proof,
      authoritativeChild,
      channel,
      identity,
      requestId,
      deadlineMs,
      signal,
    }) {
      const normalized = normalizeSingleReplyLadderProof(proof, authoritativeChild);
      let boundary = authoritativeChild;
      for (const [label, series] of [
        ["attack", normalized.attack],
        ["forced-reply", normalized.forced_reply],
        ["mate", normalized.mate],
      ]) {
        const replayRequest = PREFIX_API.normalizePrefixRequest({
          ...boundary,
          prefix: [...series.moves],
        }, `${requestId}:${label}`, identity.prefix_contract);
        const replay = await channel.call("prefix", replayRequest, {
          signal,
          deadlineMs,
        });
        PREFIX_API.validatePrefixResult(replay, replayRequest, identity);
        recordChannelMemory(replay, channel);
        if (
          replay.complete !== true
          || replay.ended_by_check !== true
          || replay.outcome !== series.outcome
          || !sameArray(replay.prefix, series.moves)
          || !sameExactBoundary(replay.next_state, series.child_boundary)
        ) {
          throw new RootIterationClientError(
            `The cached ladder ${label} failed authoritative replay.`,
            "browser-root-ladder-proof-invalid",
          );
        }
        boundary = series.child_boundary;
      }
      return normalized;
    }

    async _probeSelectedRootSingleReplyLadder({
      task,
      channel,
      identity,
      authoritativeChild,
      authoritativeRootReplay,
      callWorkCredit,
      deadlineEpochMs,
      deadlineMs,
      signal,
      requestLabel,
    }) {
      const child = normalizeExactBoundaryState(authoritativeChild || {});
      if (
        child === null
        || child.series < SELECTED_ROOT_LADDER_MIN_CHILD_SERIES
        || child.series > 254
        || !exactInteger(callWorkCredit, 0, SELECTED_ROOT_LADDER_WORK_LIMIT)
      ) {
        throw new RootIterationClientError(
          "The selected-root ladder probe has an invalid exact boundary or credit.",
          "browser-root-ladder-request-invalid",
        );
      }
      const cacheKey = ladderProofCacheKey(identity, child);
      let cached = this.ladderProofCache.get(cacheKey) || null;
      const cacheHit = cached !== null;
      let status = cached?.status || "unknown";
      let nativeStatus = cacheHit ? "cache" : "work_limit";
      let nativeMessage = cacheHit
        ? "reused exact full-state single-reply ladder proof"
        : "shared ladder work credit is exhausted";
      let nativeStats = null;
      let workUsed = 0;
      let proof = cached?.proof || null;
      if (cacheHit) {
        this.ladderProofCache.delete(cacheKey);
        this.ladderProofCache.set(cacheKey, cached);
        if (status === "found") {
          proof = await this._replaySingleReplyLadderProof({
            proof,
            authoritativeChild: child,
            channel,
            identity,
            requestId: `${requestLabel}:cached`,
            deadlineMs,
            signal,
          });
        }
      } else if (callWorkCredit > 0) {
        const request = {
          ...task,
          schema: "spc-root-single-reply-mate-ladder-task-v1",
          call_work_credit: callWorkCredit,
          session_id: channel.sessionId,
          deadline_epoch_ms: deadlineEpochMs,
          authoritative_child_boundary: child,
          authoritative_root_replay: authoritativeRootReplay,
          remaining_time_ms: Math.max(0, Math.floor(
            task.deadline_monotonic_ms - monotonicNow(),
          )),
        };
        const reply = await channel.call("root-ladder", request, {
          signal,
          deadlineMs,
        });
        const echoedKeys = [
          "schema", "request_id", "iteration_id", "source_fingerprint",
          "kernel_sha256", "module_js_sha256", "certificate_id",
          "runtime_variant", "thread_count", "engine_version", "ruleset_version",
          "profile_id", "generation", "safety_revision", "incumbent_epoch",
          "candidate_identity", "call_work_credit", "session_id",
          "deadline_epoch_ms",
        ];
        if (
          !reply
          || typeof reply !== "object"
          || Array.isArray(reply)
          || !["found", "exhausted", "unknown"].includes(reply.status)
          || echoedKeys.some((key) => reply[key] !== request[key])
          || !sameJson(reply.candidate, request.candidate)
          || !sameExactBoundary(reply.authoritative_child_boundary, child)
          || !sameJson(reply.authoritative_root_replay, authoritativeRootReplay)
          || !exactInteger(reply.work_used, 0, callWorkCredit)
          || !["found", "exhausted", "work_limit", "deadline", "unsupported"]
            .includes(reply.native_status)
          || !["found", "exhausted", "unknown"].includes(reply.proof_status)
        ) {
          throw new RootIterationClientError(
            "The compiled single-reply ladder authority returned a stale receipt.",
            "browser-root-ladder-result-invalid",
          );
        }
        recordChannelMemory(reply, channel);
        status = reply.status;
        nativeStatus = reply.native_status;
        nativeMessage = String(reply.native_message || "");
        nativeStats = reply.native_stats === null
          ? null
          : deepFreeze({ ...reply.native_stats });
        workUsed = reply.work_used;
        if (status === "found") {
          if (reply.proof_status !== "found" || reply.ladder_proof === undefined) {
            throw new RootIterationClientError(
              "A FOUND single-reply ladder omitted its exact proof.",
              "browser-root-ladder-result-invalid",
            );
          }
          proof = await this._replaySingleReplyLadderProof({
            proof: reply.ladder_proof,
            authoritativeChild: child,
            channel,
            identity,
            requestId: requestLabel,
            deadlineMs,
            signal,
          });
        } else if (
          reply.ladder_proof !== undefined
          || reply.proof_status !== (status === "exhausted" ? "exhausted" : "unknown")
        ) {
          throw new RootIterationClientError(
            "A non-FOUND single-reply ladder result carried proof authority.",
            "browser-root-ladder-result-invalid",
          );
        }
        if (status === "found" || status === "exhausted") {
          cached = deepFreeze({ status, proof });
          this.ladderProofCache.set(cacheKey, cached);
          while (this.ladderProofCache.size > this.ladderProofCacheLimit) {
            this.ladderProofCache.delete(this.ladderProofCache.keys().next().value);
          }
        }
      }
      const receipt = deepFreeze({
        schema: SELECTED_ROOT_LADDER_RECEIPT_SCHEMA,
        status,
        proof_status: status === "found" ? "found"
          : status === "exhausted" ? "exhausted" : "unknown",
        native_status: nativeStatus,
        native_message: nativeMessage,
        native_stats: nativeStats,
        cache_hit: cacheHit,
        call_work_credit: callWorkCredit,
        work_used: workUsed,
        source_fingerprint: task.source_fingerprint,
        kernel_sha256: task.kernel_sha256,
        module_js_sha256: task.module_js_sha256,
        certificate_id: task.certificate_id,
        mate_certificate_id: identity.mate_certificate_id,
        prefix_certificate_id: identity.prefix_certificate_id,
        request_id: task.request_id,
        iteration_id: task.iteration_id,
        safety_revision: task.safety_revision,
        candidate_identity: task.candidate_identity,
        root_child_boundary: child,
        proof,
      });
      return deepFreeze({ status, work_used: workUsed, proof, receipt });
    }

    _safeRootReselectResult({
      payload,
      identity,
      expected,
      geometry,
      selected,
      receipt,
      hostStarted,
      memoryBytes,
      aggregateMemoryBytes,
    }) {
      if (
        selected?.status !== "exhausted"
        && selected?.status !== "terminal"
      ) {
        throw new RootIterationClientError(
          "The widened safety lane has no exact safe publication witness.",
          "browser-root-safe-reselector-result-invalid",
        );
      }
      const rootSeries = normalizeRootSeries(selected.root_series);
      const checkedPrefix = selected.authoritative_root_replay;
      const safetyScope = selected.status === "terminal"
        ? SAFE_ROOT_RESELECT_TERMINAL_SCOPE
        : selected.single_reply_mate_ladder?.status === "exhausted"
          ? SAFE_ROOT_RESELECT_LADDER_SCOPE
          : SAFE_ROOT_RESELECT_REPLY_MATE_SCOPE;
      return deepFreeze({
        ok: true,
        status: "complete",
        publishable: true,
        safety_certified: true,
        safety_certification_scope: safetyScope,
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
        completed_depth: 0,
        best_full_series: [...rootSeries.moves],
        principal_variation: [],
        root_search_mode: "safe-root-reselector",
        root_scores_complete: false,
        root_bound_coverage_complete: false,
        root_bound_coverage_scope: safetyScope,
        unfiltered_score_winner_selected: false,
        selection_policy: SAFE_ROOT_RESELECT_POLICY,
        exact_width: false,
        timed_out: false,
        work_limit_reached: false,
        work: receipt.total_committed_work,
        memory_bytes: memoryBytes,
        aggregate_memory_bytes: aggregateMemoryBytes,
        stats: {
          generation_positions: receipt.total_committed_work,
          root_tasks: receipt.scans.length,
          root_workers: geometry.workers,
          initial_full_wave: geometry.initial_full_wave,
          coverage_complete: false,
          safety_status: "safe-root-reselector",
          safety_certification_scope: safetyScope,
          safe_root_reselections: 1,
          safety_reserve_positions:
            identity.root_geometry.play_limits.safety_reserve_positions,
        },
        runtime_receipt: {
          runtime: "browser-wasm",
          search_mode: "safe-root-reselector",
          requested_depth: payload.depth,
          completed_depth: 0,
          wall_time_seconds: Math.max(0, (monotonicNow() - hostStarted) / 1_000),
          work: receipt.total_committed_work,
          source_fingerprint: expected.source_fingerprint,
          artifact_fingerprint: identity.wasm_sha256,
          kernel_fingerprint: expected.kernel_sha256,
          module_fingerprint: expected.module_js_sha256,
          certificate_id: expected.certificate_id,
          mate_certificate_id: identity.mate_certificate_id,
          runtime_variant: "single",
          thread_count: 1,
          worker_count: geometry.workers,
          initial_full_wave: geometry.initial_full_wave,
          certified_memory: { ...identity.memory_limits },
          aggregate_memory_cap_bytes: geometry.aggregate_maximum_bytes,
          aggregate_memory_peak_bytes: aggregateMemoryBytes,
          safety_reserve_positions:
            identity.root_geometry.play_limits.safety_reserve_positions,
          canonical_replay_certified: true,
          mate_safety_certified: selected.status === "exhausted",
          mate_safety_certification_scope: selected.status === "exhausted"
            ? SAFE_ROOT_RESELECT_REPLY_MATE_SCOPE
            : null,
          single_reply_mate_ladder_certified:
            selected.single_reply_mate_ladder?.status === "exhausted",
          single_reply_mate_ladder_receipt:
            selected.single_reply_mate_ladder || null,
          terminal_non_loss_certified: selected.status === "terminal",
          root_bound_coverage_complete: false,
          root_bound_coverage_scope: safetyScope,
          unfiltered_score_winner_selected: false,
          selection_policy: SAFE_ROOT_RESELECT_POLICY,
          safe_root_reselector: receipt,
        },
      });
    }

    async _runSafeRootReselect({
      payload,
      identity,
      expected,
      geometry,
      requestBase,
      originalBoundary,
      ordinaryCommittedWork,
      deadlineEpochMs,
      absoluteDeadline,
      receiptDeadlineMs,
      signal,
      originalError,
      excludedRoots,
      hostStarted,
    }) {
      const maximumWidth = identity.root_session_contract?.hard_limits?.maximum_width;
      if (
        maximumWidth !== SAFE_ROOT_RESELECT_WIDTH
        || identity.root_geometry?.session_config?.width !== payload.max_series
        || payload.max_series !== 32
        || !exactInteger(ordinaryCommittedWork, 0, payload.max_generation_positions)
        || !Array.isArray(excludedRoots)
      ) {
        throw new RootIterationClientError(
          "The artifact cannot derive the certified isolated safety frontier.",
          "browser-root-safe-reselector-config-invalid",
        );
      }
      const availableWork = payload.max_generation_positions - ordinaryCommittedWork;
      const laneMaxWork = Math.min(SAFE_ROOT_RESELECT_TOTAL_WORK, availableWork);
      const scans = [];
      let receiptExcludedRoots = excludedRoots.map((entry) => Object.freeze({
        ...entry,
        widened_candidate_identity: null,
        widened_child_boundary: null,
        widened_order_index: null,
        widened_order_key: null,
        widened_root_machine_notation: null,
      }));
      let excludedByOrderIndex = new Map();
      let exclusionsBound = false;
      let laneWorkUsed = 0;
      let generationWork = 0;
      let selected = null;
      let status = laneMaxWork > 0 ? "no-safe-candidate" : "lane-work-limit";
      let fatalError = null;
      let isolatedSessionDestroyed = false;
      let isolatedDestroyErrorCode = null;
      let restoreErrorCode = null;
      let wideMemoryPeak = 0;
      const ordinaryMemoryPeak = Math.max(
        0,
        ...this.pool.map((channel) => channel.memoryPeakBytes),
      );
      const ordinaryAggregateMemoryPeak = this.pool.reduce(
        (sum, channel) => sum + channel.memoryPeakBytes,
        0,
      );
      const isolationError = new RootIterationClientError(
        "The ordinary root pool was replaced by the isolated safety session.",
        "browser-root-safe-reselector-isolation",
      );
      const ordinaryTeardown = this._closePool(isolationError);
      if (!ordinaryTeardown.complete) {
        throw this._poolTeardownError(
          ordinaryTeardown,
          "The ordinary root pool could not be terminated before isolated safety widening.",
          "browser-root-safe-reselector-isolation-failed",
        );
      }
      let isolatedCrash = null;
      const channel = new RootWorkerChannel({
        id: "safe-reselector",
        workerUrl: this.workerUrl,
        workerFactory: this.workerFactory,
        onCrash: (_worker, error) => {
          if (error?.code === "browser-root-worker-crashed") isolatedCrash = error;
        },
      });
      const wideIterationId = `${requestBase.iteration_id}:safe-reselector`;
      let enumerationIdentity = null;
      let retainedCount = 0;
      let widthComplete = false;
      let sessionId = null;
      const makeReceipt = () => {
        const earlyFrontierWorkUsed = scans.reduce((sum, scan) => (
          sum + (scan.order_index < SAFE_ROOT_RESELECT_EARLY_FRONTIER_COUNT
            ? scan.work_used
            : 0)
        ), 0);
        const widenedFrontierWorkUsed = scans.reduce((sum, scan) => (
          sum + (scan.order_index >= SAFE_ROOT_RESELECT_EARLY_FRONTIER_COUNT
            ? scan.work_used
            : 0)
        ), 0);
        const safetyScope = selected?.status === "terminal"
          ? SAFE_ROOT_RESELECT_TERMINAL_SCOPE
          : selected?.single_reply_mate_ladder?.status === "exhausted"
            ? SAFE_ROOT_RESELECT_LADDER_SCOPE
            : SAFE_ROOT_RESELECT_REPLY_MATE_SCOPE;
        return deepFreeze({
          schema: "spc-root-safe-reselector-receipt-v1",
          trigger: "all-retained-children-proven-mating",
          status,
          safety_certification_scope: safetyScope,
          selected_safety_basis: selected?.status === "terminal"
            ? "exact-root-terminal-non-loss"
            : selected?.status === "exhausted"
              ? selected.single_reply_mate_ladder?.status === "exhausted"
                ? "exact-immediate-reply-mate-and-single-reply-ladder-exhaustion"
                : "exact-immediate-reply-mate-exhaustion"
              : null,
          immediate_reply_mate_horizon_series: 1,
          request_id: requestBase.request_id,
          trigger_iteration_id: requestBase.iteration_id,
          iteration_id: wideIterationId,
          source_fingerprint: expected.source_fingerprint,
          wasm_sha256: identity.wasm_sha256,
          kernel_sha256: expected.kernel_sha256,
          module_js_sha256: expected.module_js_sha256,
          root_session_certificate_id: expected.certificate_id,
          mate_certificate_id: identity.mate_certificate_id,
          prefix_certificate_id: identity.prefix_certificate_id,
          worker_id: channel.id,
          session_id: sessionId,
          requested_width: SAFE_ROOT_RESELECT_WIDTH,
          retained_count: retainedCount,
          width_complete: widthComplete,
          order_policy: SAFE_ROOT_RESELECT_ORDER_POLICY,
          root_order_tactical_protection: true,
          preferred_series: [],
          enumeration_identity: enumerationIdentity,
          original_request_max_work: payload.max_generation_positions,
          ordinary_committed_work: ordinaryCommittedWork,
          lane_max_work: laneMaxWork,
          excluded_root_count: excludedRoots.length,
          excluded_roots: [...receiptExcludedRoots],
          exclusion_binding_policy: SAFE_ROOT_RESELECT_EXCLUSION_BINDING_POLICY,
          exclusions_bound: exclusionsBound,
          early_frontier_count: Math.min(
            SAFE_ROOT_RESELECT_EARLY_FRONTIER_COUNT,
            retainedCount,
          ),
          early_frontier_child_max_work: SAFE_ROOT_RESELECT_EARLY_CHILD_WORK,
          widened_frontier_child_max_work: SAFE_ROOT_RESELECT_WIDENED_CHILD_WORK,
          early_frontier_work_used: earlyFrontierWorkUsed,
          widened_frontier_work_used: widenedFrontierWorkUsed,
          generation_work: generationWork,
          lane_work_used: laneWorkUsed,
          total_committed_work: ordinaryCommittedWork + laneWorkUsed,
          deadline_monotonic_ms: absoluteDeadline,
          deadline_epoch_ms: deadlineEpochMs,
          isolated_cleanup_status: isolatedSessionDestroyed
            ? channel.workerTerminated
              ? "session-destroyed-and-worker-terminated"
              : "worker-termination-failed"
            : channel.workerTerminated
              ? sessionId === null
                ? "worker-terminated-before-session-ready"
                : "worker-terminated-after-session-destroy-miss"
              : "worker-termination-failed",
          isolated_destroy_error_code: isolatedDestroyErrorCode,
          isolated_session_destroyed: isolatedSessionDestroyed,
          isolated_worker_terminated: channel.workerTerminated,
          isolated_worker_termination_error_code: channel.workerTerminationErrorCode,
          ordinary_pool_recreated: false,
          ordinary_pool_restore_policy: "lazy-next-request",
          restore_error_code: restoreErrorCode,
          scans: [...scans],
          selected,
        });
      };
      try {
        if (laneMaxWork <= 0 || monotonicNow() >= absoluteDeadline) {
          status = laneMaxWork <= 0 ? "lane-work-limit" : "deadline";
        } else {
          const probe = await channel.call("probe", {
            contract_version: 1,
            expected_source_fingerprint: expected.source_fingerprint,
          }, { signal, deadlineMs: absoluteDeadline });
          if (!identityMatches(probe, expected, identity)) {
            throw new RootIterationClientError(
              "The isolated safety Worker loaded a different certified identity.",
              "browser-root-safe-reselector-identity-mismatch",
            );
          }
          const createRequest = {
            request_id: `${requestBase.request_id}:safe-reselector:create`,
            iteration_id: wideIterationId,
            generation: 0,
            ...expected,
            boundary: originalBoundary,
          };
          const wideConfig = {
            ...identity.root_geometry.session_config,
            width: SAFE_ROOT_RESELECT_WIDTH,
          };
          const created = await channel.call("root-safe-reselector-session-create", {
            schema: "spc-root-safe-reselector-session-create-v1",
            purpose: "safe-root-reselector",
            request: createRequest,
          }, { signal, deadlineMs: absoluteDeadline });
          if (
            created?.status !== "ready"
            || created.schema !== "spc-root-session-create-result-v1"
            || created.abi_version !== 2
            || !exactInteger(created.session_id, 1, 0xffffffff)
            || created.request_id !== createRequest.request_id
            || created.iteration_id !== createRequest.iteration_id
            || created.generation !== 0
            || Object.entries(expected).some(([key, value]) => created[key] !== value)
            || normalizeExactBoundaryState(created.boundary) === null
            || !sameBoundary(created.boundary, originalBoundary)
            || !sameJson(created.config, wideConfig)
            || created.configured_max_depth !== wideConfig.max_depth
            || created.native_work_after !== 0
            || created.canonical_root_tactical_policy !== ROOT_TACTICAL_POLICY
            || created.canonical_root_tactical_protection
              !== canonicalRootTacticalProtection(originalBoundary)
            || created.product_publishable !== false
            || created.safety_certified !== false
            || !exactInteger(created.memory_bytes, 1, identity.memory_limits.maximum_bytes)
            || !exactInteger(
              created.memory_peak_bytes,
              created.memory_bytes,
              identity.memory_limits.maximum_bytes,
            )
          ) {
            throw new RootIterationClientError(
              "The isolated safety session did not bind its derived width and root identity.",
              "browser-root-safe-reselector-create-invalid",
            );
          }
          sessionId = created.session_id;
          channel.sessionId = sessionId;
          channel.sessionReady = true;
          channel.canonicalRootTacticalProtection =
            created.canonical_root_tactical_protection;
          channel.nativeWorkAfter = 0;
          recordChannelMemory(created, channel);
          const enumerateCredit = laneMaxWork;
          const enumerateRequest = {
            schema: "spc-root-session-enumerate-v1",
            request_id: requestBase.request_id,
            iteration_id: wideIterationId,
            generation: requestBase.depth,
            ...expected,
            session_id: sessionId,
            preferred_series: [],
            external_work: ordinaryCommittedWork,
            native_work_before: 0,
            call_work_credit: enumerateCredit,
            deadline_monotonic_ms: absoluteDeadline,
            deadline_epoch_ms: deadlineEpochMs,
            remaining_time_ms: Math.max(0, Math.floor(
              absoluteDeadline - monotonicNow(),
            )),
          };
          const enumeration = validateCallReceipt(await channel.call(
            "root-enumerate",
            enumerateRequest,
            { signal, deadlineMs: absoluteDeadline },
          ), channel, enumerateCredit);
          generationWork = enumeration.work.call_native_work;
          laneWorkUsed = generationWork;
          wideMemoryPeak = Math.max(wideMemoryPeak, channel.memoryPeakBytes);
          if (
            enumeration.request_id !== enumerateRequest.request_id
            || enumeration.iteration_id !== enumerateRequest.iteration_id
            || enumeration.generation !== enumerateRequest.generation
            || enumeration.session_id !== enumerateRequest.session_id
            || Object.entries(expected).some(([key, value]) => enumeration[key] !== value)
            || enumeration.deadline_monotonic_ms !== enumerateRequest.deadline_monotonic_ms
            || !exactInteger(
              enumeration.remaining_time_ms,
              0,
              enumerateRequest.remaining_time_ms,
            )
            || enumeration.work.external_work !== ordinaryCommittedWork
            || enumeration.work.total_accounted_work
              !== ordinaryCommittedWork + generationWork
            || enumeration.product_publishable !== false
            || enumeration.safety_certified !== false
            || enumeration.canonical_root_tactical_policy !== ROOT_TACTICAL_POLICY
            || enumeration.canonical_root_tactical_protection
              !== channel.canonicalRootTacticalProtection
          ) {
            throw new RootIterationClientError(
              "The isolated safety enumeration returned a stale identity or work receipt.",
              "browser-root-safe-reselector-result-invalid",
            );
          }
          if (enumeration.status !== "complete") {
            status = enumeration.status === "work_limit"
              ? "lane-work-limit"
              : enumeration.status === "timeout" ? "deadline" : "enumeration-unsupported";
          } else {
            const manifest = {
              enumeration_identity: enumeration.enumeration_identity,
              root_white_to_move: enumeration.root_white_to_move,
              requested_width: enumeration.requested_width,
              retained_count: enumeration.retained_count,
              width_complete: enumeration.width_complete,
              preferred_series: enumeration.preferred_series,
              candidates: enumeration.candidates,
            };
            if (
              enumeration.schema !== "spc-root-session-enumeration-result-v1"
              || enumeration.abi_version !== 2
              || enumeration.imported !== false
              || enumeration.requested_width !== SAFE_ROOT_RESELECT_WIDTH
              || enumeration.root_white_to_move !== (originalBoundary.series % 2 === 1)
              || !sameArray(enumeration.preferred_series, [])
              || !exactInteger(enumeration.retained_count, 1, SAFE_ROOT_RESELECT_WIDTH)
              || enumeration.retained_count !== enumeration.candidates?.length
              || enumeration.candidates.some((candidate, index) => (
                candidate?.order_index !== index
              ))
              || typeof enumeration.width_complete !== "boolean"
            ) {
              throw new RootIterationClientError(
                "The isolated safety enumeration did not preserve production root order.",
                "browser-root-safe-reselector-result-invalid",
              );
            }
            try {
              ROOT_API.normalizeManifest(manifest, ROOT_API.normalizeRequest({
                ...requestBase,
                iteration_id: wideIterationId,
                width: SAFE_ROOT_RESELECT_WIDTH,
                aspiration: null,
                worker_count: 1,
                initial_full_wave: 1,
                dynamic_work_pool: true,
                call_work_credit_supported: true,
                caps: {
                  max_work: payload.max_generation_positions,
                  initial_work: ordinaryCommittedWork + generationWork,
                  safety_reserve_work: 0,
                  search_call_work_credit: 1,
                  safety_call_work_credit: 0,
                  max_memory_bytes: identity.memory_limits.maximum_bytes,
                },
              }));
            } catch (cause) {
              throw new RootIterationClientError(
                "The isolated safety manifest failed the canonical root contract.",
                "browser-root-safe-reselector-result-invalid",
                { cause },
              );
            }
            const rootWhite = originalBoundary.series % 2 === 1;
            let sawNonMate = false;
            for (const candidate of enumeration.candidates) {
              const deliveredMate = candidate.root_series?.outcome === "checkmate";
              if (deliveredMate) {
                const expectedMateScore = rootWhite ? MATE_SCORE - 1 : -MATE_SCORE + 1;
                const expectedMateProof = rootWhite ? [1, 1] : [-1, -1];
                if (
                  sawNonMate
                  || candidate.root_series.ended_by_check !== true
                  || candidate.terminal_score !== expectedMateScore
                  || !sameJson(candidate.terminal_proof_bounds, expectedMateProof)
                ) {
                  throw new RootIterationClientError(
                    "The widened manifest violated native tactical root-mate ordering.",
                    "browser-root-safe-reselector-order-invalid",
                  );
                }
              } else {
                sawNonMate = true;
              }
            }
            enumerationIdentity = enumeration.enumeration_identity;
            retainedCount = enumeration.retained_count;
            widthComplete = enumeration.width_complete;
            const boundExclusions = excludedRoots.map((exclusion) => {
              const matches = enumeration.candidates.filter((candidate) => (
                sameExactBoundary(
                  candidate.root_series?.child_boundary,
                  exclusion.source_child_boundary,
                )
              ));
              if (matches.length !== 1) {
                throw new RootIterationClientError(
                  "A rejected root did not map uniquely into the widened production frontier.",
                  "browser-root-safe-reselector-exclusions-invalid",
                );
              }
              const widenedCandidate = matches[0];
              return Object.freeze({
                ...exclusion,
                widened_candidate_identity: widenedCandidate.candidate_identity,
                widened_child_boundary: normalizeExactBoundaryState(
                  widenedCandidate.root_series.child_boundary,
                ),
                widened_order_index: widenedCandidate.order_index,
                widened_order_key: widenedCandidate.order_key,
                widened_root_machine_notation:
                  widenedCandidate.root_series.machine_notation,
              });
            }).sort((left, right) => (
              left.widened_order_index - right.widened_order_index
            ));
            if (new Set(boundExclusions.map(
              (entry) => entry.widened_order_index,
            )).size !== boundExclusions.length) {
              throw new RootIterationClientError(
                "Rejected roots collided in the widened production frontier.",
                "browser-root-safe-reselector-exclusions-invalid",
              );
            }
            receiptExcludedRoots = Object.freeze(boundExclusions);
            excludedByOrderIndex = new Map(boundExclusions.map((entry) => (
              [entry.widened_order_index, entry]
            )));
            exclusionsBound = true;
            for (const candidate of enumeration.candidates) {
              if (monotonicNow() >= absoluteDeadline) {
                status = "deadline";
                break;
              }
              const rootSeries = normalizeRootSeries(candidate.root_series);
              const replayRequest = PREFIX_API.normalizePrefixRequest({
                ...originalBoundary,
                prefix: [...rootSeries.moves],
              }, `${wideIterationId}:root-replay:${candidate.order_index}`, identity.prefix_contract);
              const rootReplay = await channel.call("prefix", replayRequest, {
                signal,
                deadlineMs: absoluteDeadline,
              });
              PREFIX_API.validatePrefixResult(rootReplay, replayRequest, identity);
              recordChannelMemory(rootReplay, channel);
              const authoritativeChild = normalizeExactBoundaryState(rootReplay.next_state || {});
              if (
                rootReplay.complete !== true
                || !sameArray(rootReplay.prefix, rootSeries.moves)
                || rootReplay.outcome !== rootSeries.outcome
                || rootReplay.ended_by_check !== rootSeries.ended_by_check
                || authoritativeChild === null
                || !sameExactBoundary(authoritativeChild, rootSeries.child_boundary)
              ) {
                throw new RootIterationClientError(
                  "A widened root candidate failed authoritative replay.",
                  "browser-root-safe-reselector-result-invalid",
                );
              }
              const exclusion = excludedByOrderIndex.get(candidate.order_index) || null;
              if (
                exclusion !== null
                && !sameExactBoundary(
                  exclusion.source_child_boundary,
                  authoritativeChild,
                )
              ) {
                throw new RootIterationClientError(
                  "A widened rejected root replayed to a different child boundary.",
                  "browser-root-safe-reselector-exclusions-invalid",
                );
              }
              const rootTerminalNonLoss = rootSeries.outcome !== null && (
                rootWhite
                  ? candidate.terminal_score >= 0
                    && candidate.terminal_proof_bounds?.[0] >= 0
                  : candidate.terminal_score <= 0
                    && candidate.terminal_proof_bounds?.[1] <= 0
              );
              let scanStatus = exclusion !== null
                ? "policy-excluded"
                : rootSeries.outcome === null
                  ? null
                  : rootTerminalNonLoss ? "terminal" : "terminal-loss";
              let cacheHit = false;
              let workUsed = 0;
              let callWorkCredit = 0;
              let ladderCallWorkCredit = 0;
              let ladderReceipt = null;
              const frontierStage = candidate.order_index
                < SAFE_ROOT_RESELECT_EARLY_FRONTIER_COUNT
                ? "retained-w32"
                : "widened-w512";
              const perChildMaxWork = candidate.order_index
                < SAFE_ROOT_RESELECT_EARLY_FRONTIER_COUNT
                ? SAFE_ROOT_RESELECT_EARLY_CHILD_WORK
                : SAFE_ROOT_RESELECT_WIDENED_CHILD_WORK;
              if (scanStatus === null) {
                const cacheKey = mateProofCacheKey(identity, authoritativeChild);
                const cached = this.mateProofCache.get(cacheKey) || null;
                if (cached !== null) {
                  cacheHit = true;
                  this.mateProofCache.delete(cacheKey);
                  this.mateProofCache.set(cacheKey, cached);
                  scanStatus = cached.status;
                  if (cached.status === "found") {
                    const mateReplayRequest = PREFIX_API.normalizePrefixRequest({
                      ...authoritativeChild,
                      prefix: [...cached.moves],
                    }, `${wideIterationId}:${candidate.order_index}:cached-mate-replay`, identity.prefix_contract);
                    const checkedMate = await channel.call("prefix", mateReplayRequest, {
                      signal,
                      deadlineMs: absoluteDeadline,
                    });
                    PREFIX_API.validatePrefixResult(
                      checkedMate,
                      mateReplayRequest,
                      identity,
                    );
                    if (
                      checkedMate.complete !== true
                      || checkedMate.outcome !== "checkmate"
                      || checkedMate.ended_by_check !== true
                      || !sameArray(checkedMate.prefix, cached.moves)
                    ) {
                      throw new RootIterationClientError(
                        "A cached widened reply-mate proof failed authoritative replay.",
                        "browser-root-safe-reselector-result-invalid",
                      );
                    }
                  }
                } else {
                  callWorkCredit = Math.min(
                    perChildMaxWork,
                    laneMaxWork - laneWorkUsed,
                    payload.max_generation_positions - ordinaryCommittedWork - laneWorkUsed,
                  );
                  if (callWorkCredit <= 0) {
                    scanStatus = "work_limit";
                  } else {
                    const safetyRevision = candidate.order_index + 1;
                    const safetyRequest = {
                    schema: "spc-root-safety-task-v1",
                    request_id: requestBase.request_id,
                    iteration_id: wideIterationId,
                    ...expected,
                    generation: requestBase.depth,
                    safety_revision: safetyRevision,
                    incumbent_epoch: 0,
                    candidate_identity: candidate.candidate_identity,
                    candidate,
                    call_work_credit: callWorkCredit,
                    session_id: sessionId,
                    deadline_monotonic_ms: absoluteDeadline,
                    deadline_epoch_ms: deadlineEpochMs,
                    authoritative_child_boundary: authoritativeChild,
                    authoritative_root_replay: rootReplay,
                    remaining_time_ms: Math.max(0, Math.floor(
                      absoluteDeadline - monotonicNow(),
                    )),
                    };
                    const safety = await channel.call("root-safety", safetyRequest, {
                    signal,
                    deadlineMs: absoluteDeadline,
                    });
                    const echoedKeys = [
                    "schema", "request_id", "iteration_id", "source_fingerprint",
                    "kernel_sha256", "module_js_sha256", "certificate_id",
                    "runtime_variant", "thread_count", "engine_version",
                    "ruleset_version", "profile_id", "generation",
                    "safety_revision", "incumbent_epoch", "candidate_identity",
                    "call_work_credit", "session_id", "deadline_monotonic_ms",
                    "deadline_epoch_ms",
                    ];
                    if (
                    !safety
                    || typeof safety !== "object"
                    || Array.isArray(safety)
                    || ![
                      "found", "exhausted", "unknown", "work_limit", "unsupported",
                    ].includes(safety.status)
                    || echoedKeys.some((key) => safety[key] !== safetyRequest[key])
                    || !sameJson(safety.candidate, candidate)
                    || !sameExactBoundary(
                      safety.authoritative_child_boundary,
                      authoritativeChild,
                    )
                    || !sameJson(safety.authoritative_root_replay, rootReplay)
                    || !exactInteger(safety.work_used, 0, callWorkCredit)
                    ) {
                      throw new RootIterationClientError(
                      "The widened reply-mate authority returned a stale or malformed result.",
                      "browser-root-safe-reselector-result-invalid",
                      );
                    }
                    recordChannelMemory(safety, channel);
                    workUsed = safety.work_used;
                    laneWorkUsed += workUsed;
                    scanStatus = safety.status;
                    const childIsWhite = authoritativeChild.side_to_move === "white";
                    const expectedProof = childIsWhite ? [1, 1] : [-1, -1];
                    const expectedOverride = childIsWhite
                    ? MATE_SCORE - 2
                    : -MATE_SCORE + 2;
                    if (scanStatus === "found") {
                      const replyMoves = safety.reply_mate?.moves;
                      const mateReplayRequest = PREFIX_API.normalizePrefixRequest({
                      ...authoritativeChild,
                      prefix: Array.isArray(replyMoves) ? [...replyMoves] : null,
                      }, `${wideIterationId}:${safetyRevision}:mate-replay`, identity.prefix_contract);
                      PREFIX_API.validatePrefixResult(
                      safety.reply_mate?.checked_prefix,
                      mateReplayRequest,
                      identity,
                      );
                      if (
                      safety.override_score !== expectedOverride
                      || !sameJson(safety.proof_bounds, expectedProof)
                      || safety.reply_mate.machine_notation !== replyMoves.join("/")
                      || safety.reply_mate.outcome !== "checkmate"
                      || safety.reply_mate.ended_by_check !== true
                      || safety.reply_mate.checked_prefix.complete !== true
                      || safety.reply_mate.checked_prefix.outcome !== "checkmate"
                      || safety.reply_mate.checked_prefix.ended_by_check !== true
                      ) {
                        throw new RootIterationClientError(
                        "The widened FOUND proof failed its exact mate contract.",
                        "browser-root-safe-reselector-result-invalid",
                        );
                      }
                      this.mateProofCache.set(cacheKey, Object.freeze({
                      status: "found",
                      moves: Object.freeze([...replyMoves]),
                      proof_bounds: Object.freeze([...expectedProof]),
                      }));
                    } else {
                      if (
                      safety.reply_mate !== undefined
                      || safety.override_score !== undefined
                      || safety.proof_bounds !== undefined
                      ) {
                        throw new RootIterationClientError(
                        "A non-FOUND widened proof carried mate authority.",
                        "browser-root-safe-reselector-result-invalid",
                        );
                      }
                      if (scanStatus === "exhausted") {
                        this.mateProofCache.set(cacheKey, Object.freeze({ status: "exhausted" }));
                      }
                    }
                    while (this.mateProofCache.size > this.mateProofCacheLimit) {
                      this.mateProofCache.delete(this.mateProofCache.keys().next().value);
                    }
                  }
                }
              }
              if (
                scanStatus === "exhausted"
                && authoritativeChild.series >= SELECTED_ROOT_LADDER_MIN_CHILD_SERIES
              ) {
                ladderCallWorkCredit = Math.min(
                  SELECTED_ROOT_LADDER_WORK_LIMIT,
                  Math.max(0, perChildMaxWork - workUsed),
                  Math.max(0, laneMaxWork - laneWorkUsed),
                  Math.max(
                    0,
                    payload.max_generation_positions
                      - ordinaryCommittedWork - laneWorkUsed,
                  ),
                );
                const ladderTask = {
                  schema: "spc-root-safety-task-v1",
                  request_id: requestBase.request_id,
                  iteration_id: wideIterationId,
                  ...expected,
                  generation: requestBase.depth,
                  safety_revision: candidate.order_index + 1,
                  incumbent_epoch: 0,
                  candidate_identity: candidate.candidate_identity,
                  candidate,
                  call_work_credit: ladderCallWorkCredit,
                  session_id: sessionId,
                  deadline_monotonic_ms: absoluteDeadline,
                  deadline_epoch_ms: deadlineEpochMs,
                };
                const ladder = await this._probeSelectedRootSingleReplyLadder({
                  task: ladderTask,
                  channel,
                  identity,
                  authoritativeChild,
                  authoritativeRootReplay: rootReplay,
                  callWorkCredit: ladderCallWorkCredit,
                  deadlineEpochMs,
                  deadlineMs: absoluteDeadline,
                  signal,
                  requestLabel:
                    `${wideIterationId}:${candidate.order_index}:single-reply-ladder`,
                });
                workUsed += ladder.work_used;
                laneWorkUsed += ladder.work_used;
                ladderReceipt = ladder.receipt;
                scanStatus = ladder.status;
              }
              const scan = deepFreeze({
                candidate_identity: candidate.candidate_identity,
                enumeration_identity: enumerationIdentity,
                order_index: candidate.order_index,
                order_key: candidate.order_key,
                root_series: rootSeries,
                terminal_score: candidate.terminal_score,
                terminal_proof_bounds: [...candidate.terminal_proof_bounds],
                authoritative_child_boundary: authoritativeChild,
                authoritative_root_replay: rootReplay,
                status: scanStatus,
                exclusion,
                cache_hit: cacheHit,
                ladder_cache_hit: ladderReceipt?.cache_hit ?? false,
                frontier_stage: frontierStage,
                per_child_max_work: perChildMaxWork,
                call_work_credit: callWorkCredit,
                ladder_call_work_credit: ladderCallWorkCredit,
                work_used: workUsed,
                single_reply_mate_ladder: ladderReceipt,
              });
              scans.push(scan);
              if (scanStatus === "terminal" || scanStatus === "exhausted") {
                if (monotonicNow() >= absoluteDeadline) {
                  status = "deadline";
                  break;
                }
                selected = scan;
                status = "selected";
                break;
              }
            }
            if (selected === null && status === "no-safe-candidate") {
              status = laneWorkUsed >= laneMaxWork ? "lane-work-limit" : "no-safe-candidate";
            }
          }
        }
      } catch (error) {
        if (
          error?.code === "browser-root-deadline"
          || (error?.name === "AbortError" && !signal?.aborted)
        ) {
          status = "deadline";
        } else if (error?.name === "AbortError" && signal?.aborted) {
          fatalError = error;
        } else {
          fatalError = error;
        }
      } finally {
        wideMemoryPeak = Math.max(wideMemoryPeak, channel.memoryPeakBytes);
        if (channel.sessionReady && !channel.closed) {
          try {
            const destroyed = await channel.call("root-session-destroy", {
              schema: "spc-root-session-destroy-request-v1",
              session_id: channel.sessionId,
            }, { signal, deadlineMs: receiptDeadlineMs });
            isolatedSessionDestroyed = destroyed?.status === "destroyed"
              && destroyed.session_id === channel.sessionId;
            if (!isolatedSessionDestroyed) {
              isolatedDestroyErrorCode = "browser-root-safe-reselector-destroy-invalid";
            }
          } catch (error) {
            isolatedSessionDestroyed = false;
            isolatedDestroyErrorCode = String(
              error?.code || "browser-root-safe-reselector-destroy-failed",
            );
          }
        }
        channel.close(new RootIterationClientError(
          "The isolated safety Worker completed.",
          "browser-root-safe-reselector-complete",
        ));
        if (!channel.workerTerminated && fatalError === null) {
          fatalError = new RootIterationClientError(
            "The isolated safety Worker could not be terminated.",
            "browser-root-safe-reselector-termination-failed",
          );
        }
        if (isolatedCrash !== null && fatalError === null) {
          isolatedDestroyErrorCode ||= String(
            isolatedCrash?.code || "browser-root-worker-crashed",
          );
          fatalError = new RootIterationClientError(
            "The isolated safety Worker crashed after producing provisional evidence.",
            "browser-root-safe-reselector-worker-crashed",
            { cause: isolatedCrash },
          );
        }
      }
      if (signal?.aborted && fatalError === null) fatalError = abortError();
      const receipt = makeReceipt();
      const attachReceipt = (error) => {
        if (
          error
          && typeof error === "object"
          && error.safe_root_reselector_receipt === undefined
        ) {
          Object.defineProperty(error, "safe_root_reselector_receipt", {
            configurable: false,
            enumerable: true,
            writable: false,
            value: receipt,
          });
        }
        return error;
      };
      if (fatalError !== null) throw attachReceipt(fatalError);
      if (selected === null) {
        throw attachReceipt(originalError);
      }
      const memoryBytes = Math.max(
        wideMemoryPeak,
        ordinaryMemoryPeak,
        ...this.pool.map((item) => item.memoryPeakBytes),
      );
      const restoredAggregate = this.pool.reduce(
        (sum, item) => sum + item.memoryPeakBytes,
        0,
      );
      const aggregateMemoryBytes = Math.max(
        ordinaryAggregateMemoryPeak,
        wideMemoryPeak,
        restoredAggregate,
      );
      try {
        return this._safeRootReselectResult({
          payload,
          identity,
          expected,
          geometry,
          selected,
          receipt,
          hostStarted,
          memoryBytes,
          aggregateMemoryBytes,
        });
      } catch (error) {
        throw attachReceipt(error);
      }
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
        root_bound_coverage_scope: "all-retained-candidates",
        unfiltered_score_winner_selected: true,
        selection_policy: CHECKED_PV_SELECTION_POLICY,
        mate_claim_selection_policy: MATE_CLAIM_SELECTION_POLICY,
        mate_claim_policy_filtered: false,
        root_mate_claim_quarantines: 0,
        mate_claim_quarantine_receipts:
          EMPTY_MATE_CLAIM_QUARANTINE_RECEIPTS,
        selection_policy_filtered: false,
        pv_horizon_line_rejections: 0,
        pv_horizon_native_repairs: 0,
        pv_horizon_candidate_vetoes: 0,
        same_root_repair_policy: SAME_ROOT_REPAIR_POLICY,
        pv_horizon_policy_vetoes: EMPTY_PV_HORIZON_POLICY_VETOES,
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
          pv_horizon_line_rejections: 0,
          pv_horizon_native_repairs: 0,
          pv_horizon_candidate_vetoes: 0,
          mate_claim_selection_policy: MATE_CLAIM_SELECTION_POLICY,
          mate_claim_policy_filtered: false,
          root_mate_claim_quarantines: 0,
          mate_claim_quarantine_receipts:
            EMPTY_MATE_CLAIM_QUARANTINE_RECEIPTS,
          same_root_repair_policy: SAME_ROOT_REPAIR_POLICY,
          pv_horizon_policy_vetoes: EMPTY_PV_HORIZON_POLICY_VETOES,
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
          root_bound_coverage_scope: "all-retained-candidates",
          unfiltered_score_winner_selected: true,
          selection_policy: CHECKED_PV_SELECTION_POLICY,
          mate_claim_selection_policy: MATE_CLAIM_SELECTION_POLICY,
          mate_claim_policy_filtered: false,
          root_mate_claim_quarantines: 0,
          mate_claim_quarantine_receipts:
            EMPTY_MATE_CLAIM_QUARANTINE_RECEIPTS,
          selection_policy_filtered: false,
          pv_horizon_line_rejections: 0,
          pv_horizon_native_repairs: 0,
          pv_horizon_candidate_vetoes: 0,
          same_root_repair_policy: SAME_ROOT_REPAIR_POLICY,
          pv_horizon_policy_vetoes: EMPTY_PV_HORIZON_POLICY_VETOES,
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
        const playLimits = normalizedRootPlayLimits(identity);
        if (!playLimits) {
          throw new RootIterationClientError(
            "The root work reservation is not certificate-bound.",
            "browser-root-play-limits-invalid",
          );
        }
        if (
          originalBoundary.series >= ROOT_CURRENT_SERIES_MATE_MIN_SERIES
          && payload.max_generation_positions
            >= ROOT_CURRENT_SERIES_MATE_MIN_TOTAL_WORK
        ) {
          const proactiveMateCredit = Math.min(
            0xffffffff,
            playLimits.safety_reserve_positions,
            ROOT_CURRENT_SERIES_MATE_WORK_LIMIT,
            Math.max(
              1,
              Math.floor(
                payload.max_generation_positions
                  / ROOT_CURRENT_SERIES_MATE_WORK_DENOMINATOR,
              ),
            ),
            payload.max_generation_positions - 1,
          );
          const probeStarted = monotonicNow();
          const probeTimeCredit = Math.min(
            ROOT_CURRENT_SERIES_MATE_TIME_LIMIT_MS,
            Math.floor(
              Math.max(0, absoluteDeadline - probeStarted)
                / ROOT_CURRENT_SERIES_MATE_TIME_DENOMINATOR,
            ),
          );
          if (
            proactiveMateCredit >= ROOT_CURRENT_SERIES_MATE_MIN_WORK
            && probeTimeCredit > 0
          ) {
            const probeDeadline = Math.min(
              absoluteDeadline,
              probeStarted + probeTimeCredit,
            );
            let proactiveMate = null;
            try {
              proactiveMate = await this._probeRootTerminalMate({
                requestBase: {
                  request_id: requestId,
                  iteration_id: `${requestId}:root-terminal-mate-probe`,
                  deadline_monotonic_ms: probeDeadline,
                },
                originalBoundary,
                identity,
                expected,
                callWorkCredit: proactiveMateCredit,
                deadlineEpochMs: monotonicDeadlineEpoch(probeDeadline),
                receiptDeadlineMs: Math.min(
                  absoluteReceiptDeadline,
                  probeDeadline + ROOT_CURRENT_SERIES_MATE_RECEIPT_GRACE_MS,
                ),
                signal,
              });
            } catch (error) {
              const recoverableWorkerDeadline = (
                error?.code === "browser-root-deadline"
                && this.crashError
                && !signal?.aborted
                && monotonicNow() < absoluteDeadline
              );
              if (!recoverableWorkerDeadline) throw error;
              // No receipt means the exact work used is unknowable. Charge the
              // full allowance, discard every affected session, and resume on
              // a fresh identity-bound pool rather than publishing unmetered
              // work or stopping before Depth 1.
              safetyWork += proactiveMateCredit;
              await this._ensurePool(identity, absoluteDeadline, signal);
              await this._resetSessions({
                identity,
                expected,
                boundary: originalBoundary,
                requestId,
                deadlineMs: absoluteDeadline,
                signal,
              });
            }
            if (proactiveMate !== null) {
              safetyWork += proactiveMate.reply.work_used;
            }
            if (proactiveMate?.reply.status === "found") {
              return this._terminalMateResult({
                payload,
                identity,
                expected,
                geometry,
                originalBoundary,
                depth: 1,
                hostStarted,
                safetyReserve: proactiveMateCredit,
                rootTaskCount: 0,
                trigger: "proactive-current-series-terminal-mate",
                rescue: proactiveMate,
                safetyWork,
                mateCacheHits,
                mateCacheMisses,
              });
            }
          }
        }
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
              const proveBoundaryMate = async ({
                startBoundary,
                seriesPath,
                matePly,
                credit,
                scope,
              }) => {
                if (!Array.isArray(seriesPath) || seriesPath.length === 0) {
                  throw new RootIterationClientError(
                    "The compiled safety replay has no rooted series path.",
                    "browser-root-safety-replay-invalid",
                  );
                }
                let replayBoundary = canonicalBoundary(startBoundary);
                let authoritativeChild = null;
                if (replayBoundary === null) {
                  throw new RootIterationClientError(
                    "The compiled safety replay has no canonical root boundary.",
                    "browser-root-safety-replay-invalid",
                  );
                }
                let replay = null;
                for (let index = 0; index < seriesPath.length; index += 1) {
                  const series = seriesPath[index];
                  const replayRequest = PREFIX_API.normalizePrefixRequest({
                    ...replayBoundary,
                    prefix: [...series.moves],
                  }, `${task.iteration_id}:${task.safety_revision}:${scope}-replay-${index}`, identity.prefix_contract);
                  replay = await channel.call("prefix", replayRequest, {
                    signal: taskSignal,
                    deadlineMs: absoluteReceiptDeadline,
                  });
                  PREFIX_API.validatePrefixResult(replay, replayRequest, identity);
                  const nextChild = normalizeExactBoundaryState(replay.next_state || {});
                  if (
                    !sameArray(replay.prefix, series.moves)
                    || replay.complete !== true
                    || replay.outcome !== series.outcome
                    || replay.ended_by_check !== series.ended_by_check
                    || nextChild === null
                    || !sameExactBoundary(nextChild, series.child_boundary)
                  ) {
                    throw new RootIterationClientError(
                      "The compiled safety replay disagreed with the rooted searched PV.",
                      "browser-root-safety-replay-invalid",
                    );
                  }
                  authoritativeChild = nextChild;
                  replayBoundary = nextChild;
                }
                const series = seriesPath.at(-1);

                const cacheKey = mateProofCacheKey(identity, authoritativeChild);
                let cached = this.mateProofCache.get(cacheKey) || null;
                const cacheHit = cached !== null;
                let safety;
                if (cached) {
                  this.mateProofCache.delete(cacheKey);
                  this.mateProofCache.set(cacheKey, cached);
                  mateCacheHits += 1;
                  if (cached.status === "found") {
                    const mateReplayRequest = PREFIX_API.normalizePrefixRequest({
                      ...authoritativeChild,
                      prefix: [...cached.moves],
                    }, `${task.iteration_id}:${task.safety_revision}:${scope}-mate-replay`, identity.prefix_contract);
                    const checkedMate = await channel.call("prefix", mateReplayRequest, {
                      signal: taskSignal,
                      deadlineMs: absoluteReceiptDeadline,
                    });
                    PREFIX_API.validatePrefixResult(
                      checkedMate,
                      mateReplayRequest,
                      identity,
                    );
                    recordChannelMemory(checkedMate, channel);
                    safety = {
                      ...task,
                      status: "found",
                      work_used: 0,
                      proof_bounds: [...cached.proof_bounds],
                      memory_bytes: channel.memoryBytes,
                      memory_peak_bytes: channel.memoryPeakBytes,
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
                      memory_bytes: channel.memoryBytes,
                      memory_peak_bytes: channel.memoryPeakBytes,
                    };
                  }
                } else {
                  mateCacheMisses += 1;
                  const safetyRequest = {
                    ...task,
                    candidate: { ...task.candidate, root_series: series },
                    call_work_credit: credit,
                    session_id: channel.sessionId,
                    deadline_epoch_ms: deadlineEpochMs,
                    authoritative_child_boundary: authoritativeChild,
                    authoritative_root_replay: replay,
                    remaining_time_ms: Math.max(0, Math.floor(
                      task.deadline_monotonic_ms - monotonicNow(),
                    )),
                  };
                  safety = await channel.call("root-safety", safetyRequest, {
                    signal: taskSignal,
                    deadlineMs: absoluteReceiptDeadline,
                  });
                  const echoedKeys = [
                    "schema", "request_id", "iteration_id", "source_fingerprint",
                    "kernel_sha256", "module_js_sha256", "certificate_id",
                    "runtime_variant", "thread_count", "engine_version",
                    "ruleset_version", "profile_id", "generation",
                    "safety_revision", "incumbent_epoch", "candidate_identity",
                    "call_work_credit", "session_id", "deadline_epoch_ms",
                  ];
                  if (
                    !safety
                    || typeof safety !== "object"
                    || Array.isArray(safety)
                    || !["found", "exhausted", "unknown"].includes(safety.status)
                    || echoedKeys.some((key) => safety[key] !== safetyRequest[key])
                    || !sameJson(safety.candidate, safetyRequest.candidate)
                    || !sameExactBoundary(
                      safety.authoritative_child_boundary,
                      authoritativeChild,
                    )
                    || !sameJson(safety.authoritative_root_replay, replay)
                  ) {
                    throw new RootIterationClientError(
                      "The compiled reply-mate proof did not echo its rooted safety request.",
                      "browser-root-mate-proof-invalid",
                    );
                  }
                }
                recordChannelMemory(safety, channel);
                if (!exactInteger(safety?.work_used, 0, credit)) {
                  throw new RootIterationClientError(
                    "The compiled reply-mate proof exceeded its bounded work credit.",
                    "browser-root-mate-proof-invalid",
                  );
                }

                const childIsWhite = authoritativeChild.side_to_move === "white";
                const proofBounds = childIsWhite ? [1, 1] : [-1, -1];
                if (safety?.status === "found") {
                  const replyMoves = safety.reply_mate?.moves;
                  const mateReplayRequest = PREFIX_API.normalizePrefixRequest({
                    ...authoritativeChild,
                    prefix: Array.isArray(replyMoves) ? [...replyMoves] : null,
                  }, cached === null
                    ? `${task.iteration_id}:${task.safety_revision}:mate-replay`
                    : `${task.iteration_id}:${task.safety_revision}:${scope}-mate-replay`,
                  identity.prefix_contract);
                  const checkedMate = safety.reply_mate?.checked_prefix;
                  PREFIX_API.validatePrefixResult(
                    checkedMate,
                    mateReplayRequest,
                    identity,
                  );
                  const kernelOverride = childIsWhite
                    ? MATE_SCORE - 2
                    : -MATE_SCORE + 2;
                  if (
                    !sameArray(checkedMate.prefix, replyMoves)
                    || checkedMate.complete !== true
                    || checkedMate.outcome !== "checkmate"
                    || checkedMate.ended_by_check !== true
                    || (cached === null && safety.override_score !== kernelOverride)
                    || !sameJson(safety.proof_bounds, proofBounds)
                    || safety.reply_mate.machine_notation !== replyMoves.join("/")
                    || safety.reply_mate.outcome !== "checkmate"
                    || safety.reply_mate.ended_by_check !== true
                  ) {
                    throw new RootIterationClientError(
                      "The compiled reply-mate proof did not match replayed progressive chess.",
                      "browser-root-mate-proof-invalid",
                    );
                  }
                  if (cached === null) {
                    cached = Object.freeze({
                      status: "found",
                      moves: Object.freeze([...replyMoves]),
                      proof_bounds: Object.freeze([...proofBounds]),
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
                if (cached === null && safety?.status === "exhausted") {
                  cached = Object.freeze({ status: "exhausted" });
                }
                if (cached !== null && !this.mateProofCache.has(cacheKey)) {
                  this.mateProofCache.set(cacheKey, cached);
                  while (this.mateProofCache.size > this.mateProofCacheLimit) {
                    this.mateProofCache.delete(this.mateProofCache.keys().next().value);
                  }
                }

                const normalized = {
                  ...task,
                  status: safety.status,
                  work_used: safety.work_used,
                  memory_bytes: channel.memoryBytes,
                  memory_peak_bytes: channel.memoryPeakBytes,
                  safety_scope: scope,
                  authoritative_child_boundary: authoritativeChild,
                  authoritative_root_replay: replay,
                  mate_cache: {
                    schema: "spc-root-mate-proof-cache-receipt-v1",
                    hit: cacheHit,
                    proof_status: String(safety.status || "unknown"),
                  },
                };
                if (safety.status === "found") {
                  normalized.override_score = childIsWhite
                    ? MATE_SCORE - matePly
                    : -MATE_SCORE + matePly;
                  normalized.proof_bounds = [...proofBounds];
                  normalized.reply_mate = safety.reply_mate;
                }
                return normalized;
              };

              let workUsed = 0;
              let lastSafety = null;
              const opponentBoundaries = selectedPvOpponentBoundaries(task.candidate);
              const cachedFoundIndex = opponentBoundaries.findIndex((horizon) => {
                const leaf = horizon.pv.length === 0 ? rootSeries : horizon.pv.at(-1);
                const child = normalizeExactBoundaryState(leaf?.child_boundary || {});
                if (child === null) return false;
                const cacheKey = mateProofCacheKey(identity, child);
                return this.mateProofCache.get(cacheKey)?.status === "found";
              });
              const orderedBoundaries = cachedFoundIndex > 0
                ? Object.freeze([
                  opponentBoundaries[cachedFoundIndex],
                  ...opponentBoundaries.slice(0, cachedFoundIndex),
                  ...opponentBoundaries.slice(cachedFoundIndex + 1),
                ])
                : opponentBoundaries;
              for (let index = 0; index < orderedBoundaries.length; index += 1) {
                const horizon = orderedBoundaries[index];
                const remainingProbes = orderedBoundaries.length - index;
                const remainingCredit = task.call_work_credit - workUsed;
                const cachedFoundFirst = cachedFoundIndex >= 0 && index === 0;
                if (!cachedFoundFirst && remainingCredit < remainingProbes) {
                  return {
                    ...task,
                    status: "unknown",
                    work_used: workUsed,
                    memory_bytes: channel.memoryBytes,
                    memory_peak_bytes: channel.memoryPeakBytes,
                    safety_scope: horizon.pv.length === 0 ? "root-child" : "pv-horizon",
                  };
                }
                const reservedForShallowerBoundaries = remainingProbes - 1;
                let credit = 0;
                if (!cachedFoundFirst) {
                  if (horizon.pv.length === 0) {
                    const rootChild = normalizeExactBoundaryState(
                      rootSeries.child_boundary || {},
                    );
                    const ladderReserve = rootChild !== null
                      && rootChild.series >= SELECTED_ROOT_LADDER_MIN_CHILD_SERIES
                      ? Math.min(
                        SELECTED_ROOT_LADDER_WORK_LIMIT,
                        Math.max(0, remainingCredit - 1),
                      )
                      : 0;
                    credit = remainingCredit - ladderReserve;
                  } else {
                    credit = Math.min(
                      PV_HORIZON_MATE_WORK_LIMIT,
                      remainingCredit - reservedForShallowerBoundaries,
                    );
                  }
                }
                const scope = horizon.pv.length === 0 ? "root-child" : "pv-horizon";
                const horizonSafety = await proveBoundaryMate({
                  startBoundary: originalBoundary,
                  seriesPath: [rootSeries, ...horizon.pv],
                  matePly: horizon.matePly,
                  credit,
                  scope,
                });
                workUsed += horizonSafety.work_used;
                lastSafety = horizonSafety;
                if (horizonSafety.status === "unknown") {
                  return {
                    ...horizonSafety,
                    work_used: workUsed,
                  };
                }
                if (horizonSafety.status !== "found") continue;
                if (scope === "root-child") {
                  return {
                    ...horizonSafety,
                    work_used: workUsed,
                  };
                }
                const checkedMate = horizonSafety.reply_mate?.checked_prefix;
                const mateChild = normalizeExactBoundaryState(checkedMate?.next_state || {});
                if (mateChild === null) {
                  throw new RootIterationClientError(
                    "The selected-PV mate witness omitted its authoritative child boundary.",
                    "browser-root-mate-proof-invalid",
                  );
                }
                const rootedPath = Object.freeze([rootSeries, ...horizon.pv]);
                const mateReply = Object.freeze({
                  moves: Object.freeze([...horizonSafety.reply_mate.moves]),
                  machine_notation: horizonSafety.reply_mate.machine_notation,
                  transposition_count: 1,
                  child_boundary: mateChild,
                  outcome: "checkmate",
                  ended_by_check: true,
                });
                const {
                  override_score: _discardedOverride,
                  proof_bounds: _discardedProofBounds,
                  reply_mate: _discardedReplyMate,
                  ...evidence
                } = horizonSafety;
                return {
                  ...evidence,
                  status: "line-rejected",
                  work_used: workUsed,
                  reply_mate: mateReply,
                  horizon_proof: Object.freeze({
                    schema: "spc-retained-root-horizon-proof-v1",
                    rooted_path: rootedPath,
                    mate_reply: mateReply,
                  }),
                  line_rejection: {
                    schema: "spc-pv-horizon-line-rejection-v1",
                    reason: "adverse-immediate-series-mate",
                    mate_ply: horizon.matePly,
                    horizon_series: rootedPath.at(-1).machine_notation,
                  },
                };
              }
              if (lastSafety?.status === "exhausted") {
                const rootChild = normalizeExactBoundaryState(
                  rootSeries.child_boundary || {},
                );
                if (
                  lastSafety.safety_scope === "root-child"
                  && rootChild !== null
                  && rootChild.series >= SELECTED_ROOT_LADDER_MIN_CHILD_SERIES
                ) {
                  const ladderCredit = Math.min(
                    SELECTED_ROOT_LADDER_WORK_LIMIT,
                    task.call_work_credit - workUsed,
                  );
                  const ladder = await this._probeSelectedRootSingleReplyLadder({
                    task,
                    channel,
                    identity,
                    authoritativeChild: lastSafety.authoritative_child_boundary,
                    authoritativeRootReplay: lastSafety.authoritative_root_replay,
                    callWorkCredit: ladderCredit,
                    deadlineEpochMs,
                    deadlineMs: absoluteReceiptDeadline,
                    signal: taskSignal,
                    requestLabel:
                      `${task.iteration_id}:${task.safety_revision}:single-reply-ladder`,
                  });
                  workUsed += ladder.work_used;
                  const normalized = {
                    ...lastSafety,
                    status: ladder.status,
                    work_used: workUsed,
                    safety_scope: SELECTED_ROOT_LADDER_SCOPE,
                    single_reply_mate_ladder: ladder.receipt,
                  };
                  if (ladder.status === "found") {
                    const childIsWhite = rootChild.side_to_move === "white";
                    normalized.override_score = childIsWhite
                      ? MATE_SCORE - 4
                      : -MATE_SCORE + 4;
                    normalized.proof_bounds = childIsWhite ? [1, 1] : [-1, -1];
                    normalized.ladder_proof = ladder.proof;
                  }
                  return normalized;
                }
                return {
                  ...lastSafety,
                  work_used: workUsed,
                };
              }
              return {
                ...task,
                status: "unknown",
                work_used: workUsed,
                memory_bytes: channel.memoryBytes,
                memory_peak_bytes: channel.memoryPeakBytes,
              };
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
              const reservedSafeReselectWork = Math.min(
                SAFE_ROOT_RESELECT_TOTAL_WORK,
                rescueRemaining,
              );
              const terminalRescueCredit = rescueRemaining - reservedSafeReselectWork;
              if (terminalRescueCredit > 0) {
                const rescue = await this._probeRootTerminalMate({
                  requestBase,
                  originalBoundary,
                  identity,
                  expected,
                  callWorkCredit: Math.min(0xffffffff, terminalRescueCredit),
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
                    rootTaskCount: manifest.candidates.length,
                    trigger: "all-retained-children-proven-mating",
                    rescue,
                    safetyWork,
                    mateCacheHits,
                    mateCacheMisses,
                  });
                }
              }
              const excludedRoots = normalizeSafeRootReselectExclusions(
                error,
                manifest,
              );
              return this._runSafeRootReselect({
                payload,
                identity,
                expected,
                geometry,
                requestBase,
                originalBoundary,
                ordinaryCommittedWork: nativeWork + safetyWork,
                deadlineEpochMs,
                absoluteDeadline,
                receiptDeadlineMs: absoluteReceiptDeadline,
                signal,
                originalError: error,
                excludedRoots,
                hostStarted,
              });
            }
            const maximumHorizonProofs = identity.root_session_contract
              ?.hard_limits?.maximum_horizon_proofs;
            const maximumHorizonRepairs = payload.max_series
              * MAX_SAME_ROOT_HORIZON_REPAIRS;
            const maximumHorizonVetoes = payload.max_series;
            const sameRootRepairPolicy = normalizeSameRootRepairPolicy(
              iteration.same_root_repair_policy,
            );
            const pvHorizonPolicyVetoes = normalizePvHorizonPolicyVetoes(
              iteration.pv_horizon_policy_vetoes,
              {
                candidateIds: new Set(manifest.candidates.map(
                  (candidate) => candidate.candidate_identity,
                )),
                expectedCount: iteration.pv_horizon_candidate_vetoes,
                maximumProofs: maximumHorizonProofs,
              },
            );
            const candidateIds = new Set(manifest.candidates.map(
              (candidate) => candidate.candidate_identity,
            ));
            const mateClaimQuarantineReceipts = (
              normalizeMateClaimQuarantineReceipts(
                iteration.mate_claim_quarantine_receipts,
                {
                  candidateIds,
                  expectedCount: iteration.root_mate_claim_quarantines,
                },
              )
            );
            const mateClaimFiltered = iteration.root_mate_claim_quarantines > 0;
            const anyPolicyFiltered = (
              iteration.pv_horizon_candidate_vetoes > 0 || mateClaimFiltered
            );
            if (
              iteration.status !== "complete"
              || iteration.coverage_complete !== true
              || iteration.safety_certified !== true
              || iteration.selection_policy !== CHECKED_PV_SELECTION_POLICY
              || iteration.mate_claim_selection_policy
                !== MATE_CLAIM_SELECTION_POLICY
              || !exactInteger(
                iteration.root_mate_claim_quarantines,
                0,
                manifest.candidates.length * (MAX_ASPIRATION_ATTEMPTS + 3),
              )
              || iteration.mate_claim_policy_filtered !== mateClaimFiltered
              || mateClaimQuarantineReceipts === null
              || iteration.selected?.mate_claim_quarantined !== false
              || !publishableMateClaim(
                iteration.selected?.score,
                iteration.selected?.proof_bounds,
              )
              || !exactInteger(maximumHorizonProofs, 1, 30)
              || !exactInteger(
                iteration.pv_horizon_line_rejections,
                0,
                maximumHorizonRepairs + maximumHorizonVetoes,
              )
              || !exactInteger(
                iteration.pv_horizon_native_repairs,
                0,
                maximumHorizonRepairs,
              )
              || !exactInteger(
                iteration.pv_horizon_candidate_vetoes,
                0,
                maximumHorizonVetoes,
              )
              || sameRootRepairPolicy === null
              || pvHorizonPolicyVetoes === null
              || iteration.pv_horizon_native_repairs
                + iteration.pv_horizon_candidate_vetoes
                !== iteration.pv_horizon_line_rejections
              || iteration.selection_policy_filtered
                !== (iteration.pv_horizon_candidate_vetoes > 0)
              || iteration.coverage_scope !== (
                anyPolicyFiltered
                  ? "selection-eligible-candidates"
                  : "all-retained-candidates"
              )
              || iteration.unfiltered_score_winner_selected
                !== (
                  iteration.pv_horizon_line_rejections === 0
                  && !mateClaimFiltered
                )
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
            const ladderReceipts = Object.freeze(iteration.tasks
              .filter((event) => (
                event?.event === "safety"
                && event.single_reply_mate_ladder !== null
                && event.single_reply_mate_ladder !== undefined
              ))
              .map((event) => event.single_reply_mate_ladder));
            const selectedLadderReceipt = [...ladderReceipts].reverse().find(
              (receipt) => (
                receipt.candidate_identity === iteration.selected.candidate_identity
                && receipt.status === "exhausted"
              ),
            ) || null;
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
              safety_certification_scope: selectedLadderReceipt === null
                ? "selected-root-immediate-mate-and-checked-pv"
                : "selected-root-immediate-mate-checked-pv-and-single-reply-ladder",
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
              root_bound_coverage_scope: iteration.coverage_scope,
              unfiltered_score_winner_selected:
                iteration.unfiltered_score_winner_selected,
              selection_policy: iteration.selection_policy,
              mate_claim_selection_policy: iteration.mate_claim_selection_policy,
              mate_claim_policy_filtered: mateClaimFiltered,
              root_mate_claim_quarantines:
                iteration.root_mate_claim_quarantines,
              mate_claim_quarantine_receipts: mateClaimQuarantineReceipts,
              selection_policy_filtered: iteration.selection_policy_filtered,
              pv_horizon_line_rejections: iteration.pv_horizon_line_rejections,
              pv_horizon_native_repairs: iteration.pv_horizon_native_repairs,
              pv_horizon_candidate_vetoes: iteration.pv_horizon_candidate_vetoes,
              same_root_repair_policy: sameRootRepairPolicy,
              pv_horizon_policy_vetoes: pvHorizonPolicyVetoes,
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
                pv_horizon_line_rejections: iteration.pv_horizon_line_rejections,
                pv_horizon_native_repairs: iteration.pv_horizon_native_repairs,
                pv_horizon_candidate_vetoes: iteration.pv_horizon_candidate_vetoes,
                mate_claim_selection_policy:
                  iteration.mate_claim_selection_policy,
                mate_claim_policy_filtered: mateClaimFiltered,
                root_mate_claim_quarantines:
                  iteration.root_mate_claim_quarantines,
                mate_claim_quarantine_receipts:
                  mateClaimQuarantineReceipts,
                same_root_repair_policy: sameRootRepairPolicy,
                pv_horizon_policy_vetoes: pvHorizonPolicyVetoes,
                mate_cache_hits: mateCacheHits,
                mate_cache_misses: mateCacheMisses,
                mate_cache_entries: this.mateProofCache.size,
                single_reply_ladder_receipts: ladderReceipts,
                single_reply_ladder_cache_entries: this.ladderProofCache.size,
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
                single_reply_mate_ladder_certified:
                  selectedLadderReceipt !== null,
                selected_root_single_reply_ladder: {
                  schema: "spc-selected-root-single-reply-mate-ladder-summary-v1",
                  selected_receipt: selectedLadderReceipt,
                  receipts: ladderReceipts,
                  cache_entries: this.ladderProofCache.size,
                },
                root_bound_coverage_complete: true,
                root_bound_coverage_scope: iteration.coverage_scope,
                unfiltered_score_winner_selected:
                  iteration.unfiltered_score_winner_selected,
                selection_policy: iteration.selection_policy,
                mate_claim_selection_policy:
                  iteration.mate_claim_selection_policy,
                mate_claim_policy_filtered: mateClaimFiltered,
                root_mate_claim_quarantines:
                  iteration.root_mate_claim_quarantines,
                mate_claim_quarantine_receipts:
                  mateClaimQuarantineReceipts,
                selection_policy_filtered: iteration.selection_policy_filtered,
                pv_horizon_line_rejections: iteration.pv_horizon_line_rejections,
                pv_horizon_native_repairs: iteration.pv_horizon_native_repairs,
                pv_horizon_candidate_vetoes: iteration.pv_horizon_candidate_vetoes,
                same_root_repair_policy: sameRootRepairPolicy,
                pv_horizon_policy_vetoes: pvHorizonPolicyVetoes,
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
            if (
              error?.safe_root_reselector_receipt !== undefined
              || String(error?.code || "").startsWith("browser-root-safe-reselector-")
            ) throw error;
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
            const failure = classifyRootFailure(lastFailure, this.lastSafe.work);
            const attemptedWallTimeSeconds = Math.max(
              0,
              (monotonicNow() - hostStarted) / 1_000,
            );
            return Object.freeze({
              ...this.lastSafe,
              timed_out: failure.timedOut,
              work_limit_reached: failure.workLimitReached,
              attempted_work: failure.attemptedWork,
              attempted_wall_time_seconds: attemptedWallTimeSeconds,
              runtime_receipt: Object.freeze({
                ...this.lastSafe.runtime_receipt,
                timed_out: failure.timedOut,
                work_limit_reached: failure.workLimitReached,
                attempted_work: failure.attemptedWork,
                attempted_wall_time_seconds: attemptedWallTimeSeconds,
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
      this.ladderProofCache.clear();
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
    ladderProofCacheKey,
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
