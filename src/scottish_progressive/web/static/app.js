(() => {
  "use strict";

  const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
  const FILES = "abcdefgh";
  const STUDY_STORAGE_KEY = "scottish-progressive-analysis-study-v1";
  const POSITION_STORAGE_KEY = "scottish-progressive-saved-positions-v1";
  const STUDY_SCHEMA_VERSION = 1;
  const POSITION_SCHEMA_VERSION = 1;
  const MAX_STORED_NODES = 800;
  const MAX_SAVED_POSITIONS = 50;
  const AUTO_ANALYSIS_DEBOUNCE_MS = 260;
  const AUTO_ANALYSIS_RETRY_MS = 700;
  const ANALYSIS_PRESETS = {
    quick: { depth: 4, cap: 48, seconds: 1.25, alternatives: 2, generationPositions: 150_000 },
    strong: { depth: 8, cap: 256, seconds: 5, alternatives: 3, generationPositions: 5_000_000 },
  };
  const PIECE_NAMES = {
    p: "pawn", n: "knight", b: "bishop", r: "rook", q: "queen", k: "king",
  };

  const dom = Object.fromEntries([
    "board", "board-shell", "board-arrows", "board-loading", "drag-piece",
    "engine-status", "engine-status-text", "rules-version", "series-number",
    "turn-label", "moves-heading", "series-status", "move-chips", "boundary-pill",
    "boundary-notice", "boundary-notice-text", "eval-rail", "eval-fill", "eval-marker",
    "flip-board", "undo-move", "reset-series", "advance-series", "analyze-button",
    "preset-quick", "preset-strong", "study-save-state", "analysis-tree",
    "new-variation", "delete-variation", "clear-study", "tree-help",
    "depth-control", "cap-control", "time-control", "alternatives-control",
    "analysis-empty", "analysis-loading", "analysis-error", "analysis-error-text",
    "analysis-results", "result-score", "result-classification", "result-confidence",
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
  ].map((id) => [id.replaceAll("-", "_"), document.getElementById(id)]));

  const state = {
    boundary: {
      fen: START_FEN,
      series: 1,
      quiet_series: 0,
      ep_targets: [],
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
  };

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

  function formatPoints(value, includeUnit = true) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "Not reported";
    const sign = number > 0 ? "+" : number < 0 ? "−" : "";
    const formatted = `${sign}${Math.abs(Math.round(number)).toLocaleString()}`;
    return includeUnit ? `${formatted} heuristic points` : formatted;
  }

  function displayError(error) {
    if (error?.name === "AbortError") return "Request cancelled";
    return error?.message || String(error) || "Unknown error";
  }

  async function requestJson(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("application/json")
      ? await response.json()
      : { detail: await response.text() };
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
  }

  function boundaryPayload() {
    return {
      fen: state.boundary.fen,
      series: state.boundary.series,
      quiet_series: state.boundary.quiet_series,
      ep_targets: [...state.boundary.ep_targets],
      progressive_ep: [...state.boundary.ep_targets],
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
    return `/pieces/cburnett/${prefix}${piece.type.toUpperCase()}.svg`;
  }

  function activeBoardFen() {
    if (state.previewIndex === null) return state.boardFen;
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

  function cloneBoundary(boundary) {
    return {
      fen: String(boundary?.fen || START_FEN),
      series: Math.max(1, Math.floor(asNumber(boundary?.series, 1))),
      quiet_series: Math.max(0, Math.floor(asNumber(boundary?.quiet_series, 0))),
      ep_targets: normalizeEpTargets(boundary?.ep_targets).map((square) => square.toLowerCase()),
    };
  }

  function safeBoundary(value) {
    if (!value || typeof value !== "object") return null;
    const fen = typeof value.fen === "string" ? value.fen.trim() : "";
    const series = Math.floor(asNumber(value.series, 0));
    const quiet = Math.floor(asNumber(value.quiet_series, -1));
    const epTargets = normalizeEpTargets(value.ep_targets).map((square) => square.toLowerCase());
    if (!fen || fen.length > 180 || fen.split("/").length !== 8) return null;
    if (series < 1 || series > 1000000 || quiet < 0 || quiet > 1000000) return null;
    if (epTargets.length > 8 || epTargets.some((square) => !/^[a-h][1-8]$/.test(square))) return null;
    return { fen, series, quiet_series: quiet, ep_targets: epTargets };
  }

  function boundaryKey(boundary) {
    const safe = cloneBoundary(boundary);
    return [safe.fen, safe.series, safe.quiet_series, [...safe.ep_targets].sort().join(",")].join("|");
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

  function setBoardBusy(busy) {
    state.positionBusy = busy;
    dom.board.setAttribute("aria-busy", String(busy));
    dom.board_loading.classList.toggle("is-hidden", !busy);
    dom.board_shell.classList.toggle("is-checking", busy);
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
    if (state.analysisPaused || state.outcome || !state.positionReady || state.positionBusy) {
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
    state.legalMoves = (first(payload.legal_moves, payload.legal_next, payload.moves, []) || []).map(normalizeMove);
    state.movesRemaining = Math.max(0, asNumber(first(
      payload.moves_remaining,
      payload.remaining,
      state.boundary.series - state.prefix.length,
    )));
    state.complete = Boolean(first(payload.complete, payload.series_complete, false));
    state.nextState = normalizeNextState(first(payload.next_state, payload.boundary_state));
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
    const { pieces } = parseFen(activeBoardFen());
    const sources = previewing ? new Set() : currentLegalSources();
    const destinations = new Set(
      state.selected && !previewing
        ? state.legalMoves.filter((move) => move.from === state.selected).map((move) => move.to)
        : [],
    );
    const rankOrder = state.flipped ? [0, 1, 2, 3, 4, 5, 6, 7] : [7, 6, 5, 4, 3, 2, 1, 0];
    const fileOrder = state.flipped ? [7, 6, 5, 4, 3, 2, 1, 0] : [0, 1, 2, 3, 4, 5, 6, 7];
    const lastFrom = previewing ? null : state.lastMove?.slice(0, 2);
    const lastTo = previewing ? null : state.lastMove?.slice(2, 4);
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
        button.tabIndex = name === state.focusSquare ? 0 : -1;
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
        : `Chess board. ${state.flipped ? "Black" : "White"} pieces at the bottom.`,
    );
    dom.board_shell.classList.toggle("is-previewing", previewing);
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
    if (!state.prefix.length) {
      const empty = document.createElement("span");
      empty.className = "empty-chip";
      empty.textContent = "No moves yet";
      dom.move_chips.replaceChildren(empty);
      return;
    }
    const nodes = state.prefix.map((uci, index) => {
      const chip = document.createElement("span");
      chip.className = "move-chip";
      const number = document.createElement("b");
      number.textContent = String(index + 1);
      chip.append(number, document.createTextNode(state.prefixSan[index] || uci));
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
      reason: `${Math.round(loss)} White-centric heuristic-point loss against the best returned complete series`,
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
            ? `${Math.round(asNumber(verdict.score?.effective_loss, 0))} White-centric heuristic-point loss for this micro-move`
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
    const side = state.boundary.series % 2 === 1 ? "White" : "Black";
    dom.series_number.textContent = String(state.boundary.series);
    dom.turn_label.textContent = `${side} · Series ${state.boundary.series}`;
    dom.moves_heading.textContent = `${state.movesRemaining} move${state.movesRemaining === 1 ? "" : "s"} remaining`;
    const gameOver = Boolean(state.outcome);
    dom.undo_move.disabled = state.prefix.length === 0 && state.history.length === 0;
    dom.reset_series.disabled = state.prefix.length === 0;
    dom.advance_series.hidden = !(state.complete && state.nextState && !gameOver && state.viewingHistorical);

    dom.boundary_pill.className = "boundary-pill";
    dom.boundary_notice.className = "boundary-notice";
    if (gameOver) {
      dom.boundary_pill.classList.add("is-mid-series");
      dom.boundary_pill.textContent = "Game over";
      dom.boundary_notice.classList.add("is-game-over");
      dom.boundary_notice_text.textContent = `${humanize(state.outcome)} ends the game. Undo or select an earlier tree move to continue studying.`;
      dom.moves_heading.textContent = humanize(state.outcome);
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
      dom.boundary_notice_text.textContent = "Keep playing the same side. Automatic analysis is searching only legal completions of this prefix.";
      dom.series_status.textContent = "Every micro-move is retained in the local move tree.";
    } else {
      dom.boundary_pill.classList.add("is-boundary");
      dom.boundary_pill.textContent = "Exact boundary";
      dom.boundary_notice.classList.add("is-ready");
      dom.boundary_notice_text.textContent = state.handoffNotice || "Play on the board; automatic analysis is already running.";
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
  }

  function legalMovesFrom(square) {
    return state.legalMoves.filter((move) => move.from === square);
  }

  function chooseSquare(square) {
    if (state.previewIndex !== null || state.positionBusy || state.outcome || state.complete) return;
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
    if (state.positionBusy || state.outcome || state.complete || state.previewIndex !== null) return;
    const boundaryAtMove = cloneBoundary(state.boundary);
    const parentId = state.currentTreeNodeId;
    const seriesParentId = state.seriesParentNodeId;
    const nextPrefix = [...state.prefix, move.uci];
    const nextSan = [...state.prefixSan, move.san || move.uci];
    state.lastMove = move.uci;
    state.selected = null;
    state.viewingHistorical = false;
    state.handoffNotice = null;
    const payload = await refreshPrefix(nextPrefix, nextSan);
    if (!payload) return;
    attachMoveToStudy(move, payload, boundaryAtMove, parentId, seriesParentId);
    if (payload.complete && payload.next_state && !payload.outcome) {
      await advanceSeries(true);
    }
  }

  function endDrag() {
    dom.drag_piece.className = "drag-piece";
    dom.drag_piece.textContent = "";
    state.drag = null;
  }

  function onPointerDown(event) {
    if (state.previewIndex !== null || state.positionBusy || state.outcome || state.complete) return;
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
    const square = event.target.closest(".square")?.dataset.square;
    if (!square) return;
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
      detail.textContent = displayLine(first(alternative.classification, alternative.confidence, extractUci(alternative), "Candidate series"));
      line.append(title, detail);
      const score = document.createElement("span");
      score.className = "alt-score";
      score.textContent = alternativeScore(alternative) === undefined ? "—" : formatPoints(alternativeScore(alternative), false);
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
      if (skip.has(key) || /distance$/i.test(key)) return;
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
      number.textContent = formatPoints(value, false);
      number.title = `${formatPoints(value)} (White-centric)`;
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

  function updateEvalBar(score) {
    const number = Number(score);
    const percent = Number.isFinite(number) ? 50 + 46 * Math.tanh(number / 900) : 50;
    const bounded = Math.max(4, Math.min(96, percent));
    dom.eval_fill.style.height = `${bounded}%`;
    dom.eval_marker.style.bottom = `${bounded}%`;
    dom.eval_marker.textContent = Number.isFinite(number) ? formatPoints(number, false) : "?";
    dom.eval_rail.setAttribute("aria-label", Number.isFinite(number)
      ? `${formatPoints(number)}, White-centric`
      : "White-centric heuristic evaluation not reported");
  }

  function renderAnalysis(result) {
    state.analysis = result;
    dom.analysis_empty.hidden = true;
    dom.analysis_loading.hidden = true;
    dom.analysis_error.hidden = true;
    dom.analysis_results.hidden = false;
    const score = first(result.score, result.evaluation?.score, result.value);
    dom.result_score.textContent = formatPoints(score);
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
    updateEvalBar(score);
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
      value.textContent = formatPoints(first(opening.score, opening.value), false);
      const unit = document.createElement("small");
      unit.textContent = "heuristic points";
      score.append(value, unit);
      row.append(rank, main, score);
      const uci = first(opening.move_uci, opening.uci, extractUci(opening.move));
      row.disabled = !uci;
      row.setAttribute("aria-label", `Load ${title.textContent}, ${formatPoints(opening.score)}`);
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
    state.boundary = { fen: START_FEN, series: 1, quiet_series: 0, ep_targets: [] };
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
      const candidate = { fen, series, quiet_series: quiet, ep_targets: epTargets.map((square) => square.toLowerCase()) };
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
      state.boundary = candidate;
      state.history = [];
      state.study = createStudy(candidate);
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

  async function advanceSeries(automatic = false) {
    if (!state.nextState || state.outcome) return;
    const completedSeries = state.boundary.series;
    const completedByCheck = state.check;
    const unusedMoves = state.unusedMoves;
    const historyEntry = {
      boundary: {
        ...state.boundary,
        ep_targets: [...state.boundary.ep_targets],
      },
      prefix: [...state.prefix],
      prefixSan: [...state.prefixSan],
      treeNodeId: state.currentTreeNodeId,
      seriesParentNodeId: state.seriesParentNodeId,
    };
    state.history.push(historyEntry);
    state.boundary = { ...state.nextState, ep_targets: [...state.nextState.ep_targets] };
    state.prefix = [];
    state.prefixSan = [];
    state.seriesParentNodeId = state.currentTreeNodeId;
    state.boardFen = state.boundary.fen;
    state.complete = false;
    state.nextState = null;
    state.viewingHistorical = false;
    state.handoffNotice = completedByCheck && unusedMoves > 0
      ? `Series ${completedSeries} ended by check; ${unusedMoves} unused move${unusedMoves === 1 ? "" : "s"} were forfeited.`
      : `Series ${completedSeries} complete. Series ${state.boundary.series} started automatically.`;
    const payload = await refreshPrefix([], []);
    if (payload) {
      persistStudy();
      renderStudyTree();
      showToast(completedByCheck && unusedMoves > 0
        ? `Check ended Series ${completedSeries} early · ${unusedMoves} unused move${unusedMoves === 1 ? "" : "s"}`
        : automatic ? `Series ${state.boundary.series} started` : `Continued to Series ${state.boundary.series}`);
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

  async function checkHealth() {
    try {
      const health = await requestJson("/api/health");
      dom.engine_status.classList.add("is-online");
      dom.engine_status.classList.remove("is-offline");
      const profileName = first(health.engine_profile_name, health.profile_name);
      dom.engine_status_text.textContent = first(health.status, "ok") === "ok"
        ? "Engine online"
        : String(first(health.status, "Engine online"));
      const version = first(health.ruleset_version, health.rules_version, health.ruleset);
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
        state.maximumGenerationPositions = Math.max(1_000, Math.floor(asNumber(maximumGenerationPositions, 5_000_000)));
        ANALYSIS_PRESETS.strong.generationPositions = state.maximumGenerationPositions;
      }
      dom.engine_status.title = [profileName, first(health.engine_version, health.version), first(health.source_fingerprint, health.fingerprint)].filter(Boolean).join(" · ");
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

    dom.flip_board.addEventListener("click", () => {
      state.flipped = !state.flipped;
      renderBoard();
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
      if (event.key.toLowerCase() === "f" && !event.target.matches("input, textarea")) {
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
  }

  void initialize();
})();
