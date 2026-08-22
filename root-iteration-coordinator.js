(() => {
  "use strict";

  const REQUEST_SCHEMA = "spc-root-iteration-request-v1";
  const RESULT_SCHEMA = "spc-root-iteration-result-v1";
  const SOURCE_FINGERPRINT = /^[0-9a-f]{16}$/;
  const ARTIFACT_FINGERPRINT = /^[0-9a-f]{64}$/;
  const SQUARE = /^[a-h][1-8]$/;
  const PROMOTED_HEX = /^(?:0x)?[0-9a-fA-F]{1,16}$/;
  const UCI_MOVE = /^[a-h][1-8][a-h][1-8][qrbn]?$/;
  const ROOT_OUTCOMES = new Set(["checkmate", "stalemate", "ten_series_draw"]);
  const COMPLETE = "complete";
  const EXACT = "exact";
  const UPPER = "upper";
  const LOWER = "lower";
  const UNKNOWN = "unknown";
  const WHITE = "white";
  const BLACK = "black";

  class RootCoordinatorError extends Error {
    constructor(message, code, { cause, details, work } = {}) {
      super(message, cause === undefined ? undefined : { cause });
      this.name = "RootCoordinatorError";
      this.code = code;
      this.failClosed = true;
      if (details !== undefined) this.details = details;
      if (work !== undefined) this.work = work;
    }
  }

  function exactInteger(value, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) {
    return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
  }

  function utf8Length(value) {
    return new TextEncoder().encode(value).byteLength;
  }

  function defaultMonotonicNow() {
    if (typeof globalThis.performance?.now !== "function") {
      throw new RootCoordinatorError(
        "A monotonic clock is unavailable in this runtime.",
        "root-clock-unavailable",
      );
    }
    return globalThis.performance.now();
  }

  function canonicalPromotedHex(value) {
    if (typeof value !== "string" || !PROMOTED_HEX.test(value)) return null;
    return value.toLowerCase().replace(/^0x/, "").padStart(16, "0");
  }

  function sameArray(left, right) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => item === right[index]);
  }

  function normalizeBoundary(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new RootCoordinatorError(
        "The root boundary is not an object.",
        "root-boundary-invalid",
      );
    }
    const fen = value.fen;
    const fields = typeof fen === "string" ? fen.split(" ") : [];
    const epTargets = Array.isArray(value.ep_targets)
      ? [...value.ep_targets]
      : null;
    const canonicalEp = epTargets === null ? null : [...epTargets].sort();
    const promotedHex = canonicalPromotedHex(value.promoted_hex);
    if (
      fields.length !== 6
      || fields.some((field) => !field)
      || /[\0\r\n]/.test(fen)
      || fen !== fen.trim()
      || utf8Length(fen) > 512
      || !exactInteger(value.series, 1, 256)
      || !exactInteger(value.quiet_series, 0, 1_000_000)
      || epTargets === null
      || epTargets.length > 8
      || epTargets.some((square) => typeof square !== "string" || !SQUARE.test(square))
      || new Set(epTargets).size !== epTargets.length
      || !sameArray(epTargets, canonicalEp)
      || utf8Length(epTargets.join(",")) > 23
      || promotedHex === null
      || typeof value.promoted_hex !== "string"
      || utf8Length(value.promoted_hex) > 18
      || value.chess960 !== false
      || (value.series % 2 === 1 ? fields[1] !== "w" : fields[1] !== "b")
    ) {
      throw new RootCoordinatorError(
        "The root boundary is outside the local coordinator envelope.",
        value?.chess960 === true
          ? "root-chess960-unsupported"
          : "root-boundary-invalid",
      );
    }
    return Object.freeze({
      fen,
      series: value.series,
      quiet_series: value.quiet_series,
      ep_targets: Object.freeze(epTargets),
      promoted_hex: promotedHex,
      chess960: false,
    });
  }

  function normalizeRequest(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new RootCoordinatorError(
        "The root request is not an object.",
        "root-request-invalid",
      );
    }
    const prefix = Array.isArray(value.required_prefix)
      ? value.required_prefix.map(String)
      : null;
    if (prefix === null || prefix.length !== 0) {
      throw new RootCoordinatorError(
        "Required-prefix root search is not implemented by this coordinator.",
        "root-prefix-unsupported",
      );
    }
    const caps = value.caps;
    const identityOkay = value.schema === REQUEST_SCHEMA
      && typeof value.request_id === "string"
      && value.request_id.length > 0
      && typeof value.iteration_id === "string"
      && value.iteration_id.length > 0
      && SOURCE_FINGERPRINT.test(String(value.source_fingerprint || ""))
      && ARTIFACT_FINGERPRINT.test(String(value.kernel_sha256 || ""))
      && ARTIFACT_FINGERPRINT.test(String(value.module_js_sha256 || ""))
      && typeof value.certificate_id === "string"
      && value.certificate_id.length > 0
      && typeof value.runtime_variant === "string"
      && value.runtime_variant.length > 0
      && exactInteger(value.thread_count, 1, 64)
      && typeof value.engine_version === "string"
      && value.engine_version.length > 0
      && typeof value.ruleset_version === "string"
      && value.ruleset_version.length > 0
      && typeof value.profile_id === "string"
      && value.profile_id.length > 0;
    if (
      !identityOkay
      || !exactInteger(value.depth, 1, 8)
      || !exactInteger(value.width, 1, 16_384)
      || !exactInteger(value.mate_score, 1)
      || !Number.isFinite(value.deadline_monotonic_ms)
      || value.deadline_monotonic_ms < 0
      || !exactInteger(value.worker_count, 1, 64)
      || !exactInteger(value.initial_full_wave, 1, value.worker_count)
      || !caps
      || typeof caps !== "object"
      || Array.isArray(caps)
      || !exactInteger(caps.max_work, 1)
      || !exactInteger(caps.initial_work, 0, caps.max_work)
      || !exactInteger(caps.safety_reserve_work, 0, caps.max_work - caps.initial_work)
      || !exactInteger(caps.search_call_work_credit, 1, caps.max_work)
      || !exactInteger(caps.safety_call_work_credit, 0, caps.max_work)
      || !exactInteger(caps.max_memory_bytes, 1)
      || value.dynamic_work_pool !== true
      || value.call_work_credit_supported !== true
    ) {
      throw new RootCoordinatorError(
        value?.dynamic_work_pool === true && value?.call_work_credit_supported !== true
          ? "Dynamic work pooling requires native per-call work credits."
          : "The root request contract is invalid.",
        value?.dynamic_work_pool === true && value?.call_work_credit_supported !== true
          ? "root-call-work-credit-unsupported"
          : "root-request-invalid",
      );
    }
    return Object.freeze({
      schema: REQUEST_SCHEMA,
      request_id: value.request_id,
      iteration_id: value.iteration_id,
      source_fingerprint: value.source_fingerprint,
      kernel_sha256: value.kernel_sha256,
      module_js_sha256: value.module_js_sha256,
      certificate_id: value.certificate_id,
      runtime_variant: value.runtime_variant,
      thread_count: value.thread_count,
      engine_version: value.engine_version,
      ruleset_version: value.ruleset_version,
      profile_id: value.profile_id,
      boundary: normalizeBoundary(value.boundary),
      required_prefix: Object.freeze([]),
      depth: value.depth,
      width: value.width,
      mate_score: value.mate_score,
      deadline_monotonic_ms: value.deadline_monotonic_ms,
      worker_count: value.worker_count,
      initial_full_wave: value.initial_full_wave,
      dynamic_work_pool: true,
      call_work_credit_supported: true,
      caps: Object.freeze({
        max_work: caps.max_work,
        initial_work: caps.initial_work,
        safety_reserve_work: caps.safety_reserve_work,
        search_call_work_credit: caps.search_call_work_credit,
        safety_call_work_credit: caps.safety_call_work_credit,
        max_memory_bytes: caps.max_memory_bytes,
      }),
    });
  }

  function normalizeRootSeries(value, request, terminalScore, terminalProof) {
    const expectedSeriesKeys = [
      "child_boundary", "ended_by_check", "machine_notation", "moves",
      "outcome", "transposition_count",
    ];
    const expectedBoundaryKeys = [
      "board_fen", "chess960", "ep_targets", "fen", "progressive_ep",
      "promoted_hex", "quiet_draw_pending", "quiet_series", "series",
      "series_number", "side_to_move",
    ];
    const child = value?.child_boundary;
    const moves = value?.moves;
    const epTargets = child?.ep_targets;
    const progressiveEp = child?.progressive_ep;
    const fen = child?.fen;
    const fields = typeof fen === "string" ? fen.split(" ") : [];
    const nextWhite = request.boundary.series % 2 === 0;
    const outcome = value?.outcome;
    const terminal = outcome !== null;
    const expectedChildSeries = (
      value?.ended_by_check !== true
      && ["checkmate", "stalemate"].includes(outcome)
      && Array.isArray(moves)
      && moves.length < request.boundary.series
    ) ? request.boundary.series : request.boundary.series + 1;
    const expectedMateScore = nextWhite
      ? -request.mate_score + 1
      : request.mate_score - 1;
    if (
      !value
      || typeof value !== "object"
      || Array.isArray(value)
      || JSON.stringify(Object.keys(value).sort()) !== JSON.stringify(expectedSeriesKeys)
      || !Array.isArray(moves)
      || moves.length < 1
      || moves.length > request.boundary.series
      || moves.some((move) => typeof move !== "string" || !UCI_MOVE.test(move))
      || value.machine_notation !== moves.join("/")
      || !exactInteger(value.transposition_count, 1)
      || !(outcome === null || ROOT_OUTCOMES.has(outcome))
      || typeof value.ended_by_check !== "boolean"
      || (outcome === "checkmate" && value.ended_by_check !== true)
      || (["stalemate", "ten_series_draw"].includes(outcome) && value.ended_by_check !== false)
      || (!value.ended_by_check && outcome === null && moves.length !== request.boundary.series)
      || !child
      || typeof child !== "object"
      || Array.isArray(child)
      || JSON.stringify(Object.keys(child).sort()) !== JSON.stringify(expectedBoundaryKeys)
      || fields.length !== 6
      || fields.some((field) => !field)
      || fields[0].split("/").length !== 8
      || !/^[wb]$/.test(fields[1])
      || !/^\d+$/.test(fields[4])
      || !/^[1-9]\d*$/.test(fields[5])
      || /[\0\r\n]/.test(fen)
      || fen !== fen.trim()
      || utf8Length(fen) > 512
      || child.board_fen !== fen
      || child.series !== expectedChildSeries
      || child.series_number !== child.series
      || child.side_to_move !== (nextWhite ? "white" : "black")
      || fields[1] !== (nextWhite ? "w" : "b")
      || !exactInteger(child.quiet_series, 0, 1_000_000)
      || typeof child.quiet_draw_pending !== "boolean"
      || child.quiet_draw_pending !== (child.quiet_series >= 10)
      || !Array.isArray(epTargets)
      || !Array.isArray(progressiveEp)
      || epTargets.length > 8
      || epTargets.some((square) => typeof square !== "string" || !SQUARE.test(square))
      || new Set(epTargets).size !== epTargets.length
      || !sameArray(epTargets, [...epTargets].sort())
      || !sameArray(epTargets, progressiveEp)
      || canonicalPromotedHex(child.promoted_hex) === null
      || child.promoted_hex !== canonicalPromotedHex(child.promoted_hex)
      || child.chess960 !== false
      || terminal !== (terminalScore !== null && terminalScore !== undefined)
      || (outcome === "checkmate" && terminalScore !== expectedMateScore)
      || (["stalemate", "ten_series_draw"].includes(outcome) && terminalScore !== 0)
      || (outcome === "checkmate" && !sameArray(
        terminalProof,
        [nextWhite ? -1 : 1, nextWhite ? -1 : 1],
      ))
      || (["stalemate", "ten_series_draw"].includes(outcome)
        && !sameArray(terminalProof, [0, 0]))
    ) {
      throw new RootCoordinatorError(
        "A retained root series or its authoritative child boundary is malformed.",
        "root-manifest-series-invalid",
      );
    }
    return Object.freeze({
      moves: Object.freeze([...moves]),
      machine_notation: value.machine_notation,
      transposition_count: value.transposition_count,
      child_boundary: Object.freeze({
        ...child,
        ep_targets: Object.freeze([...epTargets]),
        progressive_ep: Object.freeze([...progressiveEp]),
      }),
      outcome,
      ended_by_check: value.ended_by_check,
    });
  }

  function normalizeManifest(value, request) {
    const expectedManifestKeys = [
      "candidates", "enumeration_identity", "preferred_series", "requested_width",
      "retained_count", "root_white_to_move", "width_complete",
    ];
    const expectedCandidateKeys = [
      "candidate_identity", "order_index", "order_key", "root_series",
      "terminal_proof_bounds", "terminal_score",
    ];
    if (
      !value
      || typeof value !== "object"
      || Array.isArray(value)
      || JSON.stringify(Object.keys(value).sort()) !== JSON.stringify(expectedManifestKeys)
      || typeof value.enumeration_identity !== "string"
      || !value.enumeration_identity
      || value.root_white_to_move !== (request.boundary.series % 2 === 1)
      || value.requested_width !== request.width
      || typeof value.width_complete !== "boolean"
      || !Array.isArray(value.preferred_series)
      || value.preferred_series.length > request.boundary.series
      || value.preferred_series.some((move) => typeof move !== "string" || !UCI_MOVE.test(move))
      || !Array.isArray(value.candidates)
      || value.candidates.length === 0
      || value.candidates.length > request.width
      || value.retained_count !== value.candidates.length
    ) {
      throw new RootCoordinatorError(
        "The retained-root manifest is invalid.",
        "root-manifest-invalid",
      );
    }
    const identities = new Set();
    const candidates = value.candidates.map((candidate, orderIndex) => {
      const terminalScore = candidate?.terminal_score;
      const proof = candidate?.terminal_proof_bounds;
      if (
        !candidate
        || typeof candidate !== "object"
        || Array.isArray(candidate)
        || JSON.stringify(Object.keys(candidate).sort()) !== JSON.stringify(expectedCandidateKeys)
        || typeof candidate.candidate_identity !== "string"
        || !candidate.candidate_identity
        || identities.has(candidate.candidate_identity)
        || candidate.order_index !== orderIndex
        || typeof candidate.order_key !== "string"
        || !candidate.order_key
        || !(terminalScore === null || terminalScore === undefined || Number.isSafeInteger(terminalScore))
        || (Number.isSafeInteger(terminalScore) && Math.abs(terminalScore) >= 2 * request.mate_score)
        || !Array.isArray(proof)
        || proof.length !== 2
        || proof.some((bound) => ![-1, 0, 1].includes(bound))
      ) {
        throw new RootCoordinatorError(
          "The retained-root candidate set is malformed or duplicated.",
          "root-manifest-candidate-invalid",
        );
      }
      identities.add(candidate.candidate_identity);
      return Object.freeze({
        candidate_identity: candidate.candidate_identity,
        order_index: orderIndex,
        order_key: candidate.order_key,
        terminal_score: terminalScore ?? null,
        terminal_proof_bounds: Object.freeze([...proof]),
        root_series: normalizeRootSeries(
          candidate.root_series,
          request,
          terminalScore ?? null,
          proof,
        ),
      });
    });
    return Object.freeze({
      enumeration_identity: value.enumeration_identity,
      root_white_to_move: value.root_white_to_move,
      requested_width: value.requested_width,
      width_complete: value.width_complete,
      candidates: Object.freeze(candidates),
    });
  }

  class ReservationLedger {
    constructor(caps) {
      this.maxWork = caps.max_work;
      this.committed = caps.initial_work;
      this.reserved = 0;
      this.safetyReserve = caps.safety_reserve_work;
      this.safetyCommitted = 0;
      this.safetyReserved = 0;
      this.nextToken = 1;
      this.tokens = new Map();
    }

    available(phase) {
      const raw = this.maxWork - this.committed - this.reserved;
      if (phase === "safety") return Math.max(0, raw);
      const heldSafety = Math.max(
        0,
        this.safetyReserve - this.safetyCommitted - this.safetyReserved,
      );
      return Math.max(0, raw - heldSafety);
    }

    reserve({ phase, desired, workerId = null, label }) {
      if (!exactInteger(desired, 0)) {
        throw new RootCoordinatorError(
          "The requested work credit is invalid.",
          "root-work-reservation-invalid",
        );
      }
      const credit = Math.min(desired, this.available(phase));
      if (desired > 0 && credit === 0) {
        throw new RootCoordinatorError(
          "The global root-search work envelope is exhausted.",
          "root-work-limit",
          { work: this.snapshot() },
        );
      }
      const token = this.nextToken++;
      const record = { token, phase, credit, workerId, label, settled: false };
      this.tokens.set(token, record);
      this.reserved += credit;
      if (phase === "safety") this.safetyReserved += credit;
      return Object.freeze({ token, credit, phase, workerId, label });
    }

    settle(reservation, actualWork, { lost = false } = {}) {
      const record = this.tokens.get(reservation?.token);
      if (!record || record.settled) {
        throw new RootCoordinatorError(
          "The work reservation is missing or already settled.",
          "root-work-reservation-invalid",
        );
      }
      if (!exactInteger(actualWork, 0) || (!lost && actualWork > record.credit)) {
        throw new RootCoordinatorError(
          "A worker exceeded its reserved call credit.",
          "root-work-receipt-invalid",
          { work: this.snapshot() },
        );
      }
      const charged = lost ? record.credit : actualWork;
      record.settled = true;
      this.reserved -= record.credit;
      if (record.phase === "safety") {
        this.safetyReserved -= record.credit;
        this.safetyCommitted += charged;
      }
      this.committed += charged;
      if (this.committed > this.maxWork) {
        throw new RootCoordinatorError(
          "The aggregate work receipt exceeded the public cap.",
          "root-work-limit",
          { work: this.snapshot() },
        );
      }
      return charged;
    }

    snapshot() {
      return Object.freeze({
        max_work: this.maxWork,
        committed_work: this.committed,
        reserved_work: this.reserved,
        remaining_work: this.maxWork - this.committed - this.reserved,
        safety_reserve_work: this.safetyReserve,
        safety_committed_work: this.safetyCommitted,
        exact_at_cap: this.committed === this.maxWork && this.reserved === 0,
        within_cap: this.committed + this.reserved <= this.maxWork,
      });
    }
  }

  function better(left, right, whiteToMove) {
    if (right === null) return true;
    if (left.score !== right.score) {
      return whiteToMove ? left.score > right.score : left.score < right.score;
    }
    return left.candidate.order_key < right.candidate.order_key;
  }

  function classifyBound(score, alpha, beta) {
    if (score <= alpha) return UPPER;
    if (score >= beta) return LOWER;
    return EXACT;
  }

  function coversFinal(record, incumbent, whiteToMove) {
    if (record.exact) return true;
    if (!record.bound || !incumbent) return false;
    if (whiteToMove && record.bound.kind === UPPER) {
      return record.bound.score < incumbent.score
        || (
          record.bound.score === incumbent.score
          && record.candidate.order_key >= incumbent.candidate.order_key
        );
    }
    if (!whiteToMove && record.bound.kind === LOWER) {
      return record.bound.score > incumbent.score
        || (
          record.bound.score === incumbent.score
          && record.candidate.order_key >= incumbent.candidate.order_key
        );
    }
    return false;
  }

  function normalizeWorkers(workers, request) {
    if (!Array.isArray(workers) || workers.length !== request.worker_count) {
      throw new RootCoordinatorError(
        "The configured Worker set does not match the request.",
        "root-worker-set-invalid",
      );
    }
    const ids = new Set();
    let admitted = 0;
    const normalized = workers.map((adapter) => {
      if (
        !adapter
        || typeof adapter !== "object"
        || typeof adapter.id !== "string"
        || !adapter.id
        || ids.has(adapter.id)
        || typeof adapter.search !== "function"
        || adapter.call_work_credit_supported !== true
        || adapter.hard_memory_limit_supported !== true
        || !adapter.identity
        || adapter.identity.source_fingerprint !== request.source_fingerprint
        || adapter.identity.kernel_sha256 !== request.kernel_sha256
        || adapter.identity.module_js_sha256 !== request.module_js_sha256
        || adapter.identity.certificate_id !== request.certificate_id
        || adapter.identity.runtime_variant !== request.runtime_variant
        || adapter.identity.thread_count !== request.thread_count
        || adapter.identity.engine_version !== request.engine_version
        || adapter.identity.ruleset_version !== request.ruleset_version
        || adapter.identity.profile_id !== request.profile_id
        || !exactInteger(adapter.memory_limit_bytes, 1)
        || !exactInteger(adapter.native_work_after ?? 0, 0, request.caps.initial_work)
      ) {
        throw new RootCoordinatorError(
          "A root Worker does not satisfy the certified adapter contract.",
          "root-worker-set-invalid",
        );
      }
      ids.add(adapter.id);
      admitted += adapter.memory_limit_bytes;
      return {
        id: adapter.id,
        adapter,
        memoryLimit: adapter.memory_limit_bytes,
        memoryBytes: 0,
        nativeWorkAfter: adapter.native_work_after ?? 0,
        busy: false,
        alive: true,
      };
    });
    if (admitted > request.caps.max_memory_bytes) {
      throw new RootCoordinatorError(
        "The admitted Worker memories exceed the global cap.",
        "root-memory-cap",
      );
    }
    return { states: normalized, admitted };
  }

  function validateMemory(reply, worker, workers, maxMemory) {
    const bytes = reply?.memory_bytes;
    const peakBytes = reply?.memory_peak_bytes;
    if (
      !exactInteger(bytes, 0, worker.memoryLimit)
      || !exactInteger(peakBytes, bytes, worker.memoryLimit)
    ) {
      throw new RootCoordinatorError(
        "A Worker returned an invalid or over-cap memory receipt.",
        "root-memory-receipt-invalid",
      );
    }
    worker.memoryBytes = bytes;
    const aggregate = workers.reduce((sum, item) => sum + item.memoryBytes, 0);
    if (aggregate > maxMemory) {
      throw new RootCoordinatorError(
        "Observed Worker memory exceeds the aggregate cap.",
        "root-memory-cap",
      );
    }
    return aggregate;
  }

  function validateSearchReply(reply, task, worker) {
    const work = reply?.work;
    if (
      !reply
      || typeof reply !== "object"
      || Array.isArray(reply)
      || reply.request_id !== task.request_id
      || reply.iteration_id !== task.iteration_id
      || reply.source_fingerprint !== task.source_fingerprint
      || reply.kernel_sha256 !== task.kernel_sha256
      || reply.module_js_sha256 !== task.module_js_sha256
      || reply.certificate_id !== task.certificate_id
      || reply.runtime_variant !== task.runtime_variant
      || reply.thread_count !== task.thread_count
      || reply.engine_version !== task.engine_version
      || reply.ruleset_version !== task.ruleset_version
      || reply.profile_id !== task.profile_id
      || reply.generation !== task.generation
      || reply.safety_revision !== task.safety_revision
      || reply.incumbent_epoch !== task.incumbent_epoch
      || reply.task_id !== task.task_id
      || reply.enumeration_identity !== task.enumeration_identity
      || reply.candidate_identity !== task.candidate_identity
      || reply.purpose !== task.purpose
      || reply.tt_persistence !== task.tt_persistence
      || reply.alpha !== task.alpha
      || reply.beta !== task.beta
      || reply.child_depth !== task.child_depth
      || reply.status !== COMPLETE
      || ![EXACT, UPPER, LOWER, UNKNOWN].includes(reply.bound)
      || !Number.isSafeInteger(reply.score)
      || Math.abs(reply.score) >= 2 * task.mate_score
      || !Array.isArray(reply.proof_bounds)
      || reply.proof_bounds.length !== 2
      || reply.proof_bounds.some((bound) => ![-1, 0, 1].includes(bound))
      || !Array.isArray(reply.child_pv)
      || !work
      || work.call_work_credit !== task.call_work_credit
      || work.external_work !== task.external_work
      || work.native_work_before !== worker.nativeWorkAfter
      || !exactInteger(work.native_work_after, work.native_work_before)
      || work.call_native_work !== work.native_work_after - work.native_work_before
      || work.call_native_work > task.call_work_credit
      || work.total_accounted_work !== work.external_work + work.native_work_after
    ) {
      throw new RootCoordinatorError(
        "A root Worker returned a malformed, stale, or mismatched result.",
        "root-worker-result-invalid",
      );
    }
    if (reply.bound === UNKNOWN) {
      throw new RootCoordinatorError(
        "A completed root task returned UNKNOWN coverage.",
        "root-worker-result-unknown",
      );
    }
    const expectedBound = classifyBound(reply.score, task.alpha, task.beta);
    if (task.purpose !== "scout" && reply.bound !== EXACT) {
      throw new RootCoordinatorError(
        "A full-window root task did not return an exact score.",
        "root-worker-bound-invalid",
      );
    }
    if (task.purpose === "scout" && reply.bound !== expectedBound) {
      throw new RootCoordinatorError(
        "A root scout returned a bound inconsistent with its window.",
        "root-worker-bound-invalid",
      );
    }
    return reply;
  }

  function makeExact(record, reply, ownerId, { override = false } = {}) {
    record.exact = true;
    record.bound = null;
    record.score = reply.score;
    record.proofBounds = Array.isArray(reply.proof_bounds)
      ? Object.freeze([...reply.proof_bounds])
      : Object.freeze([-1, 1]);
    record.childPv = Array.isArray(reply.child_pv)
      ? Object.freeze([...reply.child_pv])
      : Object.freeze([]);
    record.ownerId = ownerId;
    record.override = override;
  }

  function publicCandidate(record) {
    return Object.freeze({
      candidate_identity: record.candidate.candidate_identity,
      order_index: record.candidate.order_index,
      order_key: record.candidate.order_key,
      root_series: record.candidate.root_series,
      score: record.score,
      terminal: record.terminal,
      owner_worker_id: record.ownerId,
      proof_bounds: record.proofBounds,
      child_pv: record.childPv,
      safety_override: record.override,
    });
  }

  function publicRootBounds(records) {
    return Object.freeze(
      [...records.values()]
        .map((record) => Object.freeze({
          candidate_identity: record.candidate.candidate_identity,
          bound: record.exact ? EXACT : record.bound?.kind ?? UNKNOWN,
          score: record.exact ? record.score : record.bound?.score ?? null,
          proof_bounds: Object.freeze([...record.proofBounds]),
        }))
        .sort((left, right) => left.candidate_identity.localeCompare(
          right.candidate_identity,
        )),
    );
  }

  async function runRootIteration({
    request: rawRequest,
    manifest: rawManifest,
    workers: rawWorkers,
    safetyProbe,
    signal,
    now = defaultMonotonicNow,
  }) {
    const request = normalizeRequest(rawRequest);
    const manifest = normalizeManifest(rawManifest, request);
    const workerContract = normalizeWorkers(rawWorkers, request);
    const workers = workerContract.states;
    const ledger = new ReservationLedger(request.caps);
    const whiteToMove = manifest.root_white_to_move;
    const records = new Map(manifest.candidates.map((candidate) => [
      candidate.candidate_identity,
      {
        candidate,
        terminal: candidate.terminal_score !== null,
        exact: candidate.terminal_score !== null,
        score: candidate.terminal_score,
        proofBounds: candidate.terminal_proof_bounds,
        childPv: Object.freeze([]),
        ownerId: null,
        bound: null,
        override: false,
        safetyStatus: candidate.terminal_score !== null ? "terminal" : null,
      },
    ]));
    const taskLog = [];
    const completedTaskIds = new Set();
    const active = new Map();
    const internalAbort = new AbortController();
    let generation = 1;
    let taskSequence = 0;
    let incumbentEpoch = 0;
    let safetyRevision = 0;
    let incumbent = null;
    let incumbentSignature = null;
    let invalidated = false;
    let peakObservedMemory = 0;

    const invalidationError = () => new RootCoordinatorError(
      internalAbort.signal.reason === "deadline"
        ? "The common root deadline expired."
        : "The root iteration was cancelled.",
      internalAbort.signal.reason === "deadline" ? "root-deadline" : "root-cancelled",
      { work: ledger.snapshot() },
    );

    const raceInvalidation = async (promise) => {
      if (internalAbort.signal.aborted) throw invalidationError();
      let abortListener;
      const aborted = new Promise((resolve) => {
        abortListener = () => resolve({ coordinatorInvalidated: true });
        internalAbort.signal.addEventListener("abort", abortListener, { once: true });
      });
      try {
        const result = await Promise.race([promise, aborted]);
        if (result?.coordinatorInvalidated === true) throw invalidationError();
        return result;
      } finally {
        internalAbort.signal.removeEventListener("abort", abortListener);
      }
    };

    const recomputeIncumbent = () => {
      let next = null;
      for (const record of records.values()) {
        if (record.exact && better(record, next, whiteToMove)) next = record;
      }
      const nextSignature = next === null
        ? null
        : `${next.candidate.candidate_identity}\u0000${next.score}`;
      if (nextSignature !== null && nextSignature !== incumbentSignature) {
        incumbentEpoch += 1;
      }
      incumbentSignature = nextSignature;
      incumbent = next;
      return next;
    };

    const finishReservationLost = (item) => {
      try {
        ledger.settle(item.reservation, 0, { lost: true });
      } catch {
        // The original failure remains authoritative; a settled token is safe.
      }
    };

    const invalidate = (reason) => {
      if (invalidated) return;
      invalidated = true;
      const cancelledGeneration = generation;
      generation += 1;
      internalAbort.abort(reason);
      for (const item of active.values()) finishReservationLost(item);
      for (const worker of workers) {
        try {
          worker.adapter.cancel?.({
            request_id: request.request_id,
            iteration_id: request.iteration_id,
            cancelled_generation: cancelledGeneration,
            next_generation: generation,
            reason,
          });
        } catch {
          // Cancellation is best effort; generation invalidation is authoritative.
        }
      }
      active.clear();
    };

    let deadlineTimer = null;
    let externalAbortListener = null;
    try {
      if (signal?.aborted) {
        throw new RootCoordinatorError(
          "The root iteration was cancelled before dispatch.",
          "root-cancelled",
          { work: ledger.snapshot() },
        );
      }
      if (now() >= request.deadline_monotonic_ms) {
        throw new RootCoordinatorError(
          "The common root deadline expired before dispatch.",
          "root-deadline",
          { work: ledger.snapshot() },
        );
      }
      externalAbortListener = () => invalidate("external-abort");
      signal?.addEventListener("abort", externalAbortListener, { once: true });
      deadlineTimer = setTimeout(
        () => invalidate("deadline"),
        Math.min(2_147_483_647, Math.max(0, request.deadline_monotonic_ms - now())),
      );

      const immediateMateScore = whiteToMove
        ? request.mate_score - 1
        : -request.mate_score + 1;
      const immediateMate = manifest.candidates.find(
        (candidate) => candidate.terminal_score === immediateMateScore,
      );
      if (immediateMate) {
        const selected = records.get(immediateMate.candidate_identity);
        return Object.freeze({
          schema: RESULT_SCHEMA,
          status: COMPLETE,
          request_id: request.request_id,
          iteration_id: request.iteration_id,
          source_fingerprint: request.source_fingerprint,
          kernel_sha256: request.kernel_sha256,
          module_js_sha256: request.module_js_sha256,
          certificate_id: request.certificate_id,
          runtime_variant: request.runtime_variant,
          thread_count: request.thread_count,
          engine_version: request.engine_version,
          ruleset_version: request.ruleset_version,
          profile_id: request.profile_id,
          generation,
          mover: whiteToMove ? WHITE : BLACK,
          selected: publicCandidate(selected),
          incumbent_epoch: 1,
          safety_revision: 0,
          safety_status: "terminal",
          safety_certified: true,
          coverage_complete: true,
          root_scores_complete: manifest.candidates.length === 1,
          width_complete: manifest.width_complete,
          dynamic_work_pool_certified: true,
          product_publishable: false,
          certification_scope: "root-coordinator-only",
          work: ledger.snapshot(),
          memory: Object.freeze({
            admitted_bytes: workerContract.admitted,
            peak_observed_bytes: 0,
            max_memory_bytes: request.caps.max_memory_bytes,
          }),
          tasks: Object.freeze([]),
        });
      }

      recomputeIncumbent();
      const pending = manifest.candidates
        .filter((candidate) => candidate.terminal_score === null)
        .map((candidate) => candidate.candidate_identity);

      const dispatch = (worker, record, purpose) => {
        if (now() >= request.deadline_monotonic_ms) invalidate("deadline");
        if (invalidated || internalAbort.signal.aborted) {
          throw new RootCoordinatorError(
            "The root iteration is no longer active.",
            internalAbort.signal.reason === "deadline" ? "root-deadline" : "root-cancelled",
            { work: ledger.snapshot() },
          );
        }
        if (!worker.alive || worker.busy) {
          throw new RootCoordinatorError(
            "The root scheduler attempted to reuse a busy or lost Worker.",
            "root-worker-state-invalid",
          );
        }
        const full = purpose !== "scout";
        if (!full && incumbent === null) {
          throw new RootCoordinatorError(
            "A root scout cannot run before an exact incumbent exists.",
            "root-incumbent-missing",
          );
        }
        const alpha = full
          ? -2 * request.mate_score
          : whiteToMove ? incumbent.score : incumbent.score - 1;
        const beta = full
          ? 2 * request.mate_score
          : whiteToMove ? incumbent.score + 1 : incumbent.score;
        const reservation = ledger.reserve({
          phase: "search",
          desired: request.caps.search_call_work_credit,
          workerId: worker.id,
          label: `${purpose}:${record.candidate.candidate_identity}`,
        });
        const externalWork = ledger.committed - worker.nativeWorkAfter;
        if (!exactInteger(externalWork, 0)) {
          ledger.settle(reservation, 0, { lost: true });
          throw new RootCoordinatorError(
            "The Worker external-work snapshot regressed.",
            "root-work-receipt-invalid",
            { work: ledger.snapshot() },
          );
        }
        taskSequence += 1;
        const task = Object.freeze({
          schema: "spc-root-candidate-task-v1",
          request_id: request.request_id,
          iteration_id: request.iteration_id,
          source_fingerprint: request.source_fingerprint,
          kernel_sha256: request.kernel_sha256,
          module_js_sha256: request.module_js_sha256,
          certificate_id: request.certificate_id,
          runtime_variant: request.runtime_variant,
          thread_count: request.thread_count,
          engine_version: request.engine_version,
          ruleset_version: request.ruleset_version,
          profile_id: request.profile_id,
          generation,
          safety_revision: safetyRevision,
          incumbent_epoch: incumbentEpoch,
          task_id: `${request.iteration_id}:${taskSequence}`,
          enumeration_identity: manifest.enumeration_identity,
          candidate_identity: record.candidate.candidate_identity,
          order_index: record.candidate.order_index,
          order_key: record.candidate.order_key,
          purpose,
          mate_score: request.mate_score,
          child_depth: request.depth - 1,
          alpha,
          beta,
          tt_persistence: purpose === "scout" ? "rollback" : "commit",
          external_work: externalWork,
          native_work_before: worker.nativeWorkAfter,
          call_work_credit: reservation.credit,
          deadline_monotonic_ms: request.deadline_monotonic_ms,
          mover: whiteToMove ? WHITE : BLACK,
        });
        worker.busy = true;
        const promise = Promise.resolve()
          .then(() => worker.adapter.search(task, { signal: internalAbort.signal }))
          .then(
            (reply) => ({ kind: "reply", worker, record, purpose, task, reservation, reply }),
            (error) => ({ kind: "error", worker, record, purpose, task, reservation, error }),
          );
        const item = { promise, worker, record, purpose, task, reservation };
        active.set(worker.id, item);
        taskLog.push(Object.freeze({
          event: "dispatch",
          worker_id: worker.id,
          task_id: task.task_id,
          candidate_identity: task.candidate_identity,
          purpose,
          incumbent_epoch: incumbentEpoch,
          safety_revision: safetyRevision,
          alpha,
          beta,
          call_work_credit: reservation.credit,
        }));
      };

      const consumeOne = async () => {
        if (active.size === 0) return null;
        const outcome = await raceInvalidation(
          Promise.race([...active.values()].map((item) => item.promise)),
        );
        if (now() >= request.deadline_monotonic_ms) invalidate("deadline");
        if (invalidated) throw invalidationError();
        const live = active.get(outcome.worker.id);
        if (!live || live.task.task_id !== outcome.task.task_id) {
          throw new RootCoordinatorError(
            "A duplicate or unsolicited Worker response was received.",
            "root-worker-result-duplicate",
          );
        }
        active.delete(outcome.worker.id);
        outcome.worker.busy = false;
        if (outcome.kind === "error") {
          outcome.worker.alive = false;
          finishReservationLost(outcome);
          throw new RootCoordinatorError(
            "A root Worker was lost during an in-flight reservation.",
            "root-worker-lost",
            { cause: outcome.error, work: ledger.snapshot() },
          );
        }
        let reply;
        try {
          if (invalidated || outcome.task.generation !== generation) {
            finishReservationLost(outcome);
            throw new RootCoordinatorError(
              "A late Worker result arrived after generation invalidation.",
              internalAbort.signal.reason === "deadline" ? "root-deadline" : "root-cancelled",
              { work: ledger.snapshot() },
            );
          }
          if (completedTaskIds.has(outcome.reply?.task_id)) {
            finishReservationLost(outcome);
            throw new RootCoordinatorError(
              "A Worker repeated a completed task identity.",
              "root-worker-result-duplicate",
              { work: ledger.snapshot() },
            );
          }
          reply = validateSearchReply(outcome.reply, outcome.task, outcome.worker);
          completedTaskIds.add(reply.task_id);
          peakObservedMemory = Math.max(
            peakObservedMemory,
            validateMemory(reply, outcome.worker, workers, request.caps.max_memory_bytes),
          );
          ledger.settle(outcome.reservation, reply.work.call_native_work);
          outcome.worker.nativeWorkAfter = reply.work.native_work_after;
        } catch (error) {
          if (!outcome.reservation || ledger.tokens.get(outcome.reservation.token)?.settled !== true) {
            finishReservationLost(outcome);
          }
          throw error;
        }
        taskLog.push(Object.freeze({
          event: "complete",
          worker_id: outcome.worker.id,
          task_id: outcome.task.task_id,
          candidate_identity: outcome.task.candidate_identity,
          purpose: outcome.purpose,
          incumbent_epoch: outcome.task.incumbent_epoch,
          stale_epoch: outcome.task.incumbent_epoch < incumbentEpoch,
          safety_revision: outcome.task.safety_revision,
          bound: reply.bound,
          score: reply.score,
        }));
        return { ...outcome, reply };
      };

      const handleSearchOutcome = (outcome) => {
        const { record, reply, worker, purpose } = outcome;
        if (purpose !== "scout") {
          makeExact(record, reply, worker.id);
          recomputeIncumbent();
          return null;
        }
        if (reply.bound === EXACT) {
          makeExact(record, reply, worker.id);
          recomputeIncumbent();
          return null;
        }
        record.bound = Object.freeze({
          kind: reply.bound,
          score: reply.score,
          alpha: outcome.task.alpha,
          beta: outcome.task.beta,
          incumbentEpoch: outcome.task.incumbent_epoch,
          safetyRevision: outcome.task.safety_revision,
        });
        record.proofBounds = Object.freeze([...reply.proof_bounds]);
        if (coversFinal(record, incumbent, whiteToMove)) return null;
        return { worker, record, purpose: "threat-research" };
      };

      const fillIdle = (queue, { forceFull = false } = {}) => {
        for (const worker of workers) {
          if (queue.length === 0) break;
          if (!worker.alive || worker.busy) continue;
          const candidateId = queue.shift();
          const record = records.get(candidateId);
          dispatch(worker, record, forceFull || incumbent === null ? "full" : "scout");
        }
      };

      // Start only the certified number of full-window seeds. Workers outside
      // that wave stay idle until the first exact completes; fillIdle then
      // streams scouts onto every free Worker without a first-wave barrier.
      let initialFullDispatched = 0;
      for (const worker of workers) {
        if (
          initialFullDispatched >= request.initial_full_wave
          || pending.length === 0
        ) break;
        const candidateId = pending.shift();
        dispatch(worker, records.get(candidateId), "full");
        initialFullDispatched += 1;
      }
      while (active.size > 0 || pending.length > 0) {
        if (active.size === 0) fillIdle(pending);
        const outcome = await consumeOne();
        const followup = handleSearchOutcome(outcome);
        if (followup) {
          dispatch(followup.worker, followup.record, followup.purpose);
        } else if (pending.length > 0) {
          fillIdle(pending);
        }
      }

      const ensureFinalCoverage = async () => {
        while (true) {
          recomputeIncumbent();
          if (!incumbent) {
            throw new RootCoordinatorError(
              "No exact root incumbent survived reduction.",
              "root-incumbent-missing",
            );
          }
          const uncovered = [...records.values()]
            .filter((record) => !coversFinal(record, incumbent, whiteToMove))
            .map((record) => record.candidate.candidate_identity);
          if (uncovered.length === 0) return;
          fillIdle(uncovered);
          while (active.size > 0 || uncovered.length > 0) {
            if (active.size === 0) fillIdle(uncovered);
            const outcome = await consumeOne();
            const followup = handleSearchOutcome(outcome);
            if (followup) dispatch(followup.worker, followup.record, followup.purpose);
            else fillIdle(uncovered);
          }
        }
      };

      const certifySelectedOnOwner = async () => {
        if (incumbent.terminal) return;
        const owner = workers.find((worker) => worker.id === incumbent.ownerId);
        if (!owner || !owner.alive || owner.busy) {
          throw new RootCoordinatorError(
            "The selected candidate's warm owning Worker is unavailable.",
            "root-selected-owner-unavailable",
          );
        }
        const expectedScore = incumbent.score;
        const expectedId = incumbent.candidate.candidate_identity;
        dispatch(owner, incumbent, "selected-certification");
        const outcome = await consumeOne();
        if (
          outcome.worker.id !== owner.id
          || outcome.record.candidate.candidate_identity !== expectedId
          || outcome.reply.score !== expectedScore
          || outcome.reply.bound !== EXACT
        ) {
          throw new RootCoordinatorError(
            "The warm selected-owner certification diverged from reduction.",
            "root-selected-certification-mismatch",
          );
        }
        makeExact(incumbent, outcome.reply, owner.id);
      };

      await ensureFinalCoverage();
      let safetyStatus = null;
      while (true) {
        recomputeIncumbent();
        if (incumbent.safetyStatus === "found") {
          throw new RootCoordinatorError(
            "The retained frontier is exhausted by authoritative reply mates and must widen.",
            "root-safety-widening-required",
            { work: ledger.snapshot() },
          );
        }
        await certifySelectedOnOwner();
        if (incumbent.terminal) {
          safetyStatus = "terminal";
          break;
        }
        if (typeof safetyProbe !== "function") {
          throw new RootCoordinatorError(
            "A non-terminal root winner has no exact reply-mate safety authority.",
            "root-safety-unavailable",
          );
        }
        const reservation = ledger.reserve({
          phase: "safety",
          desired: request.caps.safety_call_work_credit,
          label: `safety:${incumbent.candidate.candidate_identity}`,
        });
        const safetyTask = Object.freeze({
          schema: "spc-root-safety-task-v1",
          request_id: request.request_id,
          iteration_id: request.iteration_id,
          source_fingerprint: request.source_fingerprint,
          kernel_sha256: request.kernel_sha256,
          module_js_sha256: request.module_js_sha256,
          certificate_id: request.certificate_id,
          runtime_variant: request.runtime_variant,
          thread_count: request.thread_count,
          engine_version: request.engine_version,
          ruleset_version: request.ruleset_version,
          profile_id: request.profile_id,
          generation,
          safety_revision: safetyRevision,
          incumbent_epoch: incumbentEpoch,
          candidate_identity: incumbent.candidate.candidate_identity,
          candidate: publicCandidate(incumbent),
          call_work_credit: reservation.credit,
          deadline_monotonic_ms: request.deadline_monotonic_ms,
        });
        let safety;
        try {
          safety = await raceInvalidation(
            Promise.resolve(safetyProbe(safetyTask, { signal: internalAbort.signal })),
          );
        } catch (error) {
          ledger.settle(reservation, 0, { lost: true });
          if (internalAbort.signal.aborted) throw invalidationError();
          throw new RootCoordinatorError(
            "The root safety authority was lost during its reservation.",
            "root-safety-unknown",
            { cause: error, work: ledger.snapshot() },
          );
        }
        if (
          invalidated
          || !safety
          || typeof safety !== "object"
          || safety.request_id !== request.request_id
          || safety.iteration_id !== request.iteration_id
          || safety.source_fingerprint !== request.source_fingerprint
          || safety.kernel_sha256 !== request.kernel_sha256
          || safety.module_js_sha256 !== request.module_js_sha256
          || safety.certificate_id !== request.certificate_id
          || safety.runtime_variant !== request.runtime_variant
          || safety.thread_count !== request.thread_count
          || safety.engine_version !== request.engine_version
          || safety.ruleset_version !== request.ruleset_version
          || safety.profile_id !== request.profile_id
          || safety.generation !== generation
          || safety.safety_revision !== safetyRevision
          || safety.candidate_identity !== incumbent.candidate.candidate_identity
          || !["found", "exhausted", "unknown"].includes(safety.status)
          || !exactInteger(safety.work_used, 0, reservation.credit)
        ) {
          ledger.settle(reservation, 0, { lost: true });
          throw new RootCoordinatorError(
            "The root safety result is malformed, stale, or UNKNOWN.",
            "root-safety-unknown",
            { work: ledger.snapshot() },
          );
        }
        ledger.settle(reservation, safety.work_used);
        taskLog.push(Object.freeze({
          event: "safety",
          candidate_identity: incumbent.candidate.candidate_identity,
          safety_revision: safetyRevision,
          status: safety.status,
          work_used: safety.work_used,
        }));
        if (safety.status === "unknown") {
          throw new RootCoordinatorError(
            "UNKNOWN root safety cannot certify this depth.",
            "root-safety-unknown",
            { work: ledger.snapshot() },
          );
        }
        if (safety.status === "exhausted") {
          incumbent.safetyStatus = "exhausted";
          safetyStatus = "exhausted";
          break;
        }
        if (
          !Number.isSafeInteger(safety.override_score)
          || Math.abs(safety.override_score) >= 2 * request.mate_score
          || !Array.isArray(safety.proof_bounds)
          || safety.proof_bounds.length !== 2
          || safety.proof_bounds.some((bound) => ![-1, 0, 1].includes(bound))
        ) {
          throw new RootCoordinatorError(
            "A FOUND safety result has no authoritative exact override.",
            "root-safety-result-invalid",
            { work: ledger.snapshot() },
          );
        }
        makeExact(incumbent, {
          score: safety.override_score,
          proof_bounds: safety.proof_bounds,
          child_pv: safety.reply_mate === undefined ? [] : [safety.reply_mate],
        }, incumbent.ownerId, { override: true });
        incumbent.safetyStatus = "found";
        safetyRevision += 1;
        recomputeIncumbent();
        await ensureFinalCoverage();
      }

      recomputeIncumbent();
      if (now() >= request.deadline_monotonic_ms) {
        invalidate("deadline");
        throw invalidationError();
      }
      if (signal?.aborted) {
        invalidate("external-abort");
        throw invalidationError();
      }
      const coverageComplete = [...records.values()].every(
        (record) => coversFinal(record, incumbent, whiteToMove),
      );
      if (!coverageComplete) {
        throw new RootCoordinatorError(
          "Final root bound coverage is incomplete.",
          "root-bound-coverage-incomplete",
        );
      }
      const rootScoresComplete = [...records.values()].every((record) => record.exact);
      return Object.freeze({
        schema: RESULT_SCHEMA,
        status: COMPLETE,
        request_id: request.request_id,
        iteration_id: request.iteration_id,
        source_fingerprint: request.source_fingerprint,
        kernel_sha256: request.kernel_sha256,
        module_js_sha256: request.module_js_sha256,
        certificate_id: request.certificate_id,
        runtime_variant: request.runtime_variant,
        thread_count: request.thread_count,
        engine_version: request.engine_version,
        ruleset_version: request.ruleset_version,
        profile_id: request.profile_id,
        generation,
        mover: whiteToMove ? WHITE : BLACK,
        selected: publicCandidate(incumbent),
        incumbent_epoch: incumbentEpoch,
        safety_revision: safetyRevision,
        safety_status: safetyStatus,
        safety_certified: safetyStatus === "exhausted" || safetyStatus === "terminal",
        coverage_complete: true,
        root_scores_complete: rootScoresComplete,
        root_bounds: publicRootBounds(records),
        width_complete: manifest.width_complete,
        dynamic_work_pool_certified: true,
        product_publishable: false,
        certification_scope: "root-coordinator-only",
        work: ledger.snapshot(),
        memory: Object.freeze({
          admitted_bytes: workerContract.admitted,
          peak_observed_bytes: peakObservedMemory,
          max_memory_bytes: request.caps.max_memory_bytes,
        }),
        tasks: Object.freeze([...taskLog]),
      });
    } catch (error) {
      invalidate(error?.code || "root-coordinator-failure");
      if (error instanceof RootCoordinatorError) {
        if (error.work === undefined) error.work = ledger.snapshot();
        throw error;
      }
      throw new RootCoordinatorError(
        "The root iteration failed closed.",
        "root-coordinator-failure",
        { cause: error, work: ledger.snapshot() },
      );
    } finally {
      if (deadlineTimer !== null) clearTimeout(deadlineTimer);
      if (externalAbortListener) {
        signal?.removeEventListener("abort", externalAbortListener);
      }
    }
  }

  const api = Object.freeze({
    REQUEST_SCHEMA,
    RESULT_SCHEMA,
    RootCoordinatorError,
    ReservationLedger,
    normalizeRequest,
    normalizeManifest,
    classifyBound,
    coversFinal,
    runRootIteration,
  });

  globalThis.ScottishProgressiveRootCoordinator = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
