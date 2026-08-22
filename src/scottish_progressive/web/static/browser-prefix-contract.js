(() => {
  "use strict";

  const SOURCE_FINGERPRINT = /^[0-9a-f]{16}$/;
  const ARTIFACT_FINGERPRINT = /^[0-9a-f]{64}$/;
  const SQUARE = /^[a-h][1-8]$/;
  const UCI_MOVE = /^[a-h][1-8][a-h][1-8][qrbn]?$/;
  const CONTRACT_SCHEMA = "spc-boundary-prefix-contract-v1";
  const RESULT_SCHEMA = "spc-boundary-prefix-v1";
  const HARD_LIMITS = Object.freeze({
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

  class PrefixContractError extends Error {
    constructor(message, code, { fallbackRequired = true, cause } = {}) {
      super(message, cause === undefined ? undefined : { cause });
      this.name = "PrefixContractError";
      this.code = code;
      this.fallbackRequired = fallbackRequired;
    }
  }

  function abortError(message = "Prefix replay cancelled") {
    if (typeof DOMException === "function") return new DOMException(message, "AbortError");
    const error = new Error(message);
    error.name = "AbortError";
    return error;
  }

  function utf8Length(value) {
    if (typeof TextEncoder !== "function") {
      throw new PrefixContractError(
        "This browser cannot measure the certified prefix request envelope.",
        "browser-prefix-text-encoder-unavailable",
      );
    }
    return new TextEncoder().encode(value).byteLength;
  }

  function canonicalPromotedHex(value) {
    if (typeof value !== "string") return null;
    const text = value.trim().toLowerCase().replace(/^0x/, "");
    if (!/^[0-9a-f]{1,16}$/.test(text)) return null;
    return text.padStart(16, "0");
  }

  function exactInteger(value, minimum, maximum) {
    return Number.isInteger(value) && value >= minimum && value <= maximum;
  }

  function normalizeSquares(value) {
    if (!Array.isArray(value) || value.some((square) => !SQUARE.test(String(square)))) {
      return null;
    }
    const squares = value.map(String).sort();
    if (new Set(squares).size !== squares.length) return null;
    return squares;
  }

  function normalizeProgressiveEp(value) {
    if (Array.isArray(value)) return normalizeSquares(value);
    if (value === undefined) return undefined;
    if (value === null || value === "" || value === "-") return [];
    if (typeof value !== "string") return null;
    return normalizeSquares(value.split(",").map((square) => square.trim()));
  }

  function sameStrings(left, right) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((value, index) => value === right[index]);
  }

  function validateCertifiedPrefixContract(value) {
    if (
      !value
      || typeof value !== "object"
      || Array.isArray(value)
      || value.schema !== CONTRACT_SCHEMA
      || value.result_schema !== RESULT_SCHEMA
      || value.abi_version !== 1
      || value.chess960 !== false
      || value.promoted_hex_required_for_product !== true
      || !value.limits
      || typeof value.limits !== "object"
      || Array.isArray(value.limits)
    ) {
      throw new PrefixContractError(
        "The WebAssembly artifact has no certified prefix-replay contract.",
        "browser-prefix-contract-uncertified",
      );
    }
    const limits = {};
    for (const [name, hardMaximum] of Object.entries(HARD_LIMITS)) {
      const candidate = value.limits[name];
      if (!exactInteger(candidate, 1, hardMaximum)) {
        throw new PrefixContractError(
          `The certified prefix limit ${name} is invalid.`,
          "browser-prefix-contract-uncertified",
        );
      }
      limits[name] = candidate;
    }
    return Object.freeze({
      schema: CONTRACT_SCHEMA,
      result_schema: RESULT_SCHEMA,
      abi_version: 1,
      chess960: false,
      promoted_hex_required_for_product: true,
      limits: Object.freeze(limits),
    });
  }

  function validatePrefixIdentity(identity) {
    const certificateId = identity?.prefix_certificate_id ?? identity?.certificate_id;
    if (
      !identity
      || typeof identity !== "object"
      || Array.isArray(identity)
      || !SOURCE_FINGERPRINT.test(String(identity.source_fingerprint || ""))
      || !ARTIFACT_FINGERPRINT.test(String(identity.wasm_sha256 || ""))
      || !ARTIFACT_FINGERPRINT.test(String(identity.module_js_sha256 || ""))
      || typeof certificateId !== "string"
      || !certificateId
      || typeof identity.engine_version !== "string"
      || !identity.engine_version
      || typeof identity.ruleset_version !== "string"
      || !identity.ruleset_version
    ) {
      throw new PrefixContractError(
        "The WebAssembly prefix artifact identity is invalid.",
        "browser-prefix-identity-invalid",
      );
    }
    return validateCertifiedPrefixContract(identity.prefix_contract);
  }

  function normalizePrefixRequest(payload, requestId, certifiedContract) {
    const contract = validateCertifiedPrefixContract(certifiedContract);
    const limits = contract.limits;
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new PrefixContractError(
        "The prefix request is not a JSON object.",
        "browser-prefix-request-unsupported",
      );
    }
    if (typeof requestId !== "string" || !requestId) {
      throw new PrefixContractError(
        "The prefix request has no stable request identity.",
        "browser-prefix-request-invalid",
      );
    }
    const fen = payload.fen;
    const promotedHex = canonicalPromotedHex(payload.promoted_hex);
    const epTargets = normalizeSquares(payload.ep_targets);
    const progressiveEp = normalizeProgressiveEp(payload.progressive_ep);
    const prefix = Array.isArray(payload.prefix) ? payload.prefix.map(String) : null;
    if (
      typeof fen !== "string"
      || !fen
      || fen !== fen.trim()
      || /[\0\r\n]/.test(fen)
      || utf8Length(fen) > limits.maximum_fen_utf8_bytes
      || !exactInteger(payload.series, 1, limits.maximum_series_number)
      || !exactInteger(payload.quiet_series, 0, limits.maximum_quiet_series)
      || epTargets === null
      || epTargets.length > limits.maximum_ep_targets
      || utf8Length(epTargets.join(",") || "-") > limits.maximum_ep_utf8_bytes
      || progressiveEp === null
      || (progressiveEp !== undefined && !sameStrings(progressiveEp, epTargets))
      || promotedHex === null
      || utf8Length(String(payload.promoted_hex)) > limits.maximum_promoted_hex_bytes
      || payload.chess960 !== false
      || prefix === null
      || prefix.length > payload.series
      || prefix.length > limits.maximum_prefix_moves
      || prefix.some((move) => (
        !UCI_MOVE.test(move)
        || utf8Length(move) > limits.maximum_uci_move_bytes
      ))
      || utf8Length(prefix.join("/")) > limits.maximum_prefix_utf8_bytes
    ) {
      throw new PrefixContractError(
        "This prefix request is outside the certified local replay envelope.",
        "browser-prefix-request-unsupported",
      );
    }
    return Object.freeze({
      contract_version: 1,
      request_id: requestId,
      operation: "prefix-replay",
      boundary: Object.freeze({
        fen,
        series: payload.series,
        quiet_series: payload.quiet_series,
        ep_targets: Object.freeze(epTargets),
        promoted_hex: promotedHex,
        chess960: false,
      }),
      prefix: Object.freeze(prefix),
    });
  }

  function validateBoundaryIdentity(boundary, requestBoundary) {
    const epTargets = normalizeSquares(boundary?.ep_targets);
    const progressiveEp = normalizeProgressiveEp(boundary?.progressive_ep);
    const fields = fenFields(boundary?.fen);
    return Boolean(
      boundary
      && typeof boundary === "object"
      && !Array.isArray(boundary)
      && fields
      && boundary.fen === requestBoundary.fen
      && boundary.board_fen === boundary.fen
      && Number(boundary.series ?? boundary.series_number) === requestBoundary.series
      && Number(boundary.series) === Number(boundary.series_number)
      && boundary.side_to_move === (fields[1] === "w" ? "white" : "black")
      && Number(boundary.quiet_series) === requestBoundary.quiet_series
      && sameStrings(epTargets, requestBoundary.ep_targets)
      && sameStrings(progressiveEp, epTargets)
      && canonicalPromotedHex(boundary.promoted_hex) === requestBoundary.promoted_hex
      && boundary.chess960 === false
    );
  }

  function fenFields(value) {
    if (typeof value !== "string" || !value || /[\0\r\n]/.test(value)) return null;
    const fields = value.split(" ");
    return fields.length === 6 && fields.every(Boolean) ? fields : null;
  }

  function sameFinalBoard(frameFen, boardFen, complete) {
    const frame = fenFields(frameFen);
    const board = fenFields(boardFen);
    if (!frame || !board) return false;
    return frame[0] === board[0]
      && frame[2] === board[2]
      && frame[4] === board[4]
      && frame[5] === board[5]
      && (!complete || frame[1] === board[1]);
  }

  function expectedNextSeries(result, request) {
    if (
      result.ended_by_check !== true
      && ["checkmate", "stalemate"].includes(result.outcome)
      && result.completion_reason === result.outcome
      && result.unused_moves > 0
    ) return request.boundary.series;
    return request.boundary.series + 1;
  }

  function validateCompletion(result, request) {
    const outcome = result.outcome;
    const completionReason = result.completion_reason;
    const endedByCheck = result.ended_by_check;
    const remaining = request.boundary.series - request.prefix.length;
    if (
      ![null, "checkmate", "stalemate", "ten_series_draw"].includes(outcome)
      || typeof result.check !== "boolean"
      || typeof endedByCheck !== "boolean"
      || typeof result.in_check !== "boolean"
      || result.check !== endedByCheck
      || !Number.isInteger(result.remaining)
      || result.remaining !== remaining
      || result.moves_remaining !== remaining
      || !Number.isInteger(result.unused_moves)
      || result.unused_moves < 0
    ) return false;
    if (!result.complete) {
      return outcome === null
        && completionReason === null
        && endedByCheck === false
        && result.in_check === false
        && result.unused_moves === 0
        && result.next_state === null
        && result.legal_next.length > 0;
    }
    if (
      result.unused_moves !== remaining
      || result.legal_next.length !== 0
      || !result.next_state
    ) return false;
    if (endedByCheck) {
      if (!["check", "checkmate"].includes(completionReason)) return false;
      if (![null, "checkmate"].includes(outcome)) return false;
      if (result.in_check !== true) return false;
    } else if (outcome === null) {
      if (completionReason !== "budget") return false;
      if (result.in_check !== false) return false;
    } else if (completionReason !== outcome) {
      return false;
    }
    if (outcome === "checkmate" && result.in_check !== true) return false;
    if (["stalemate", "ten_series_draw"].includes(outcome) && result.in_check !== false) {
      return false;
    }
    return true;
  }

  function validateNextState(nextState, result, request, contract) {
    if (!result.complete) return nextState === null;
    const epTargets = normalizeSquares(nextState?.ep_targets);
    const progressiveEp = normalizeProgressiveEp(nextState?.progressive_ep);
    const fen = fenFields(nextState?.fen);
    const series = Number(nextState?.series ?? nextState?.series_number);
    const quietSeries = Number(nextState?.quiet_series);
    return Boolean(
      nextState
      && typeof nextState === "object"
      && !Array.isArray(nextState)
      && fen
      && utf8Length(nextState.fen) <= contract.limits.maximum_fen_utf8_bytes
      && nextState.fen === result.board_fen
      && nextState.board_fen === nextState.fen
      && series === expectedNextSeries(result, request)
      && Number(nextState.series) === series
      && Number(nextState.series_number) === series
      && nextState.side_to_move === (fen[1] === "w" ? "white" : "black")
      && exactInteger(quietSeries, 0, contract.limits.maximum_quiet_series)
      && nextState.quiet_draw_pending === (quietSeries >= 10)
      && epTargets !== null
      && epTargets.length <= contract.limits.maximum_ep_targets
      && sameStrings(progressiveEp, epTargets)
      && canonicalPromotedHex(nextState.promoted_hex) !== null
      && nextState.chess960 === false
      && ((series % 2 === 1 && fen[1] === "w") || (series % 2 === 0 && fen[1] === "b"))
    );
  }

  function validatePrefixResult(result, request, identity) {
    const contract = validatePrefixIdentity(identity);
    if (
      !result
      || typeof result !== "object"
      || Array.isArray(result)
      || result.schema !== RESULT_SCHEMA
      || result.abi_version !== 1
      || result.ok !== true
      || result.status !== "complete"
      || result.request_id !== request.request_id
      || result.source_fingerprint !== identity.source_fingerprint
      || result.wasm_sha256 !== identity.wasm_sha256
      || result.module_js_sha256 !== identity.module_js_sha256
      || result.certificate_id
        !== (identity.prefix_certificate_id ?? identity.certificate_id)
      || result.engine_version !== identity.engine_version
      || result.ruleset_version !== identity.ruleset_version
      || !validateBoundaryIdentity(result.boundary_state, request.boundary)
      || !sameStrings(result.prefix, request.prefix)
      || !sameStrings(result.current_prefix, request.prefix)
      || !Array.isArray(result.san)
      || result.san.length !== request.prefix.length
      || result.san.some((move) => typeof move !== "string" || !move)
      || !Array.isArray(result.frames)
      || result.frames.length !== request.prefix.length
      || result.frames.some((frame, index) => (
        !frame
        || typeof frame !== "object"
        || Number(frame.index) !== index + 1
        || frame.uci !== request.prefix[index]
        || frame.san !== result.san[index]
        || typeof frame.board_fen !== "string"
        || !frame.board_fen
      ))
      || typeof result.board_fen !== "string"
      || !result.board_fen
      || result.fen !== result.board_fen
      || typeof result.complete !== "boolean"
      || !Array.isArray(result.legal_next)
      || !Array.isArray(result.legal_moves)
      || result.legal_next.length !== result.legal_moves.length
      || result.legal_next.some((move, index) => (
        !move
        || typeof move !== "object"
        || !UCI_MOVE.test(String(move.uci || ""))
        || typeof move.san !== "string"
        || !move.san
        || String(result.legal_moves[index]?.uci || "") !== String(move.uci)
      ))
      || !validateCompletion(result, request)
      || !validateNextState(result.next_state, result, request, contract)
      || (request.prefix.length > 0 && !sameFinalBoard(
        result.frames.at(-1)?.board_fen,
        result.board_fen,
        result.complete,
      ))
    ) {
      throw new PrefixContractError(
        "The compiled prefix replay did not match the exact requested boundary.",
        "browser-prefix-result-invalid",
      );
    }
    return result;
  }

  function validateAuthority(value, label) {
    if (
      !value
      || typeof value !== "object"
      || !SOURCE_FINGERPRINT.test(String(value.source_fingerprint || ""))
      || typeof value.engine_version !== "string"
      || !value.engine_version
      || typeof value.ruleset_version !== "string"
      || !value.ruleset_version
    ) {
      throw new PrefixContractError(
        `The ${label} prefix authority has no exact identity.`,
        "browser-prefix-authority-unbound",
        { fallbackRequired: false },
      );
    }
    return value;
  }

  function sameAuthority(left, right) {
    return left.source_fingerprint === right.source_fingerprint
      && left.engine_version === right.engine_version
      && left.ruleset_version === right.ruleset_version;
  }

  async function requestBoundRemote(remote, payload, signal, selectedAuthority) {
    if (!remote || typeof remote.request !== "function") {
      throw new PrefixContractError(
        "The authoritative prefix fallback is unavailable.",
        "browser-prefix-fallback-unavailable",
        { fallbackRequired: false },
      );
    }
    const declared = validateAuthority(remote.identity, "declared remote");
    if (selectedAuthority && !sameAuthority(declared, selectedAuthority)) {
      throw new PrefixContractError(
        "The local and hosted prefix authorities do not match.",
        "browser-prefix-authority-mismatch",
        { fallbackRequired: false },
      );
    }
    const result = await remote.request(payload, { signal });
    const observed = validateAuthority(result, "returned remote");
    if (!sameAuthority(observed, declared)) {
      throw new PrefixContractError(
        "The hosted prefix authority changed during fallback.",
        "browser-prefix-authority-mismatch",
        { fallbackRequired: false },
      );
    }
    return result;
  }

  async function routePrefixRequest({ payload, signal, localClient, remote }) {
    if (signal?.aborted) throw abortError();
    const selectedAuthority = localClient?.identity
      ? validateAuthority(localClient.identity, "local")
      : null;
    let canInspect = false;
    try {
      canInspect = Boolean(localClient?.canInspectPrefix?.(payload));
    } catch {
      canInspect = false;
    }
    if (canInspect) {
      try {
        return await localClient.inspectPrefix(payload, { signal });
      } catch (error) {
        if (error?.name === "AbortError" || signal?.aborted) throw abortError();
        if (error?.fallbackRequired !== true) throw error;
      }
    }
    if (signal?.aborted) throw abortError();
    // Preserve the original request object. The authoritative endpoint, not
    // the local normalizer, owns error semantics for unsupported input.
    return requestBoundRemote(remote, payload, signal, selectedAuthority);
  }

  const api = Object.freeze({
    CONTRACT_SCHEMA,
    RESULT_SCHEMA,
    HARD_LIMITS,
    PrefixContractError,
    canonicalPromotedHex,
    normalizePrefixRequest,
    routePrefixRequest,
    validateCertifiedPrefixContract,
    validateAuthority,
    validatePrefixIdentity,
    validatePrefixResult,
  });
  globalThis.ScottishProgressiveBrowserPrefix = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
