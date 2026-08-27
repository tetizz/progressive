(() => {
  "use strict";

  const BOARD_RENDERER = globalThis.ScottishProgressiveBoard;
  const PLAYBACK_INTERVAL_MS = 850;
  const VALID_STATUSES = new Set(["completed", "timeout", "technical", "integrity"]);
  const dom = Object.fromEntries([
    "board", "board-shell", "board-loading", "board-loading-text",
    "match-position-label", "match-position-title", "match-position-detail",
    "match-phase", "match-series-number", "match-move-chips",
    "match-top-player", "match-top-color", "match-top-name", "match-top-meta", "match-top-turn",
    "match-bottom-player", "match-bottom-color", "match-bottom-name", "match-bottom-meta", "match-bottom-turn",
    "replay-previous", "replay-play", "replay-next", "replay-start", "replay-flip",
    "replay-progress", "replay-position", "replay-move-label",
    "match-board-notice", "match-board-notice-text",
    "match-previous-game", "match-next-game", "match-game", "match-game-count", "match-pair-label",
    "match-status", "match-status-title", "match-status-detail",
    "summary-completed", "summary-wdl", "summary-incomplete",
    "match-series-count", "match-series-list",
    "match-failure-details", "match-failure-copy", "match-provenance", "match-toast",
  ].map((id) => [id.replaceAll("-", "_"), document.getElementById(id)]));

  const state = {
    bundle: null,
    manifest: null,
    gameIndex: 0,
    frameIndex: 0,
    flipped: false,
    playing: false,
    timer: null,
    toastTimer: null,
  };

  function compactHash(value) {
    const text = String(value || "");
    return text.length > 18 ? `${text.slice(0, 10)}…${text.slice(-6)}` : text;
  }

  function humanize(value) {
    return String(value || "unknown").replaceAll("-", " ").replaceAll("_", " ");
  }

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function currentGame() {
    return state.bundle?.games?.[state.gameIndex] || null;
  }

  function currentFrame(game = currentGame()) {
    return game?.frames?.[state.frameIndex] || null;
  }

  function playerForColor(game, color) {
    return color === "white" ? game.white : game.black;
  }

  function setPlayerColor(element, color) {
    element.classList.toggle("is-white", color === "white");
    element.classList.toggle("is-black", color === "black");
  }

  function opposite(color) {
    return color === "white" ? "black" : "white";
  }

  function activeColor(game, frame) {
    if (
      !frame
      || frame.outcome
      || (!game.completed && frame.frame === game.frames.length - 1)
    ) return null;
    if (frame.series_move === 0) return frame.side;
    if (frame.is_series_end) return opposite(frame.side);
    return frame.side;
  }

  function engineLabel(player) {
    if (player.key === "local") return "Scottish Progressive";
    return player.name;
  }

  function resultLabel(game) {
    if (!game.completed) return "Incomplete";
    if (game.result === "1/2-1/2") return "Draw";
    const winner = game.winner === "local" ? "Scottish Progressive" : "Bucephalus";
    return `${winner} won as ${game.winner_color}`;
  }

  function statusCopy(game) {
    if (game.status === "completed") {
      return {
        title: `${game.result} · ${resultLabel(game)}`,
        detail: `${humanize(game.terminal_reason)} · replay verified`,
      };
    }
    const owner = game.technical_failure_owner === "bucephalus"
      ? "Bucephalus"
      : game.technical_failure_owner === "local"
        ? "Scottish Progressive"
        : "The match harness";
    if (game.status === "timeout") {
      return {
        title: "Timeout · game incomplete",
        detail: `${owner} reached the deadline. Every completed move remains available.`,
      };
    }
    if (game.status === "integrity") {
      return {
        title: "Integrity incomplete",
        detail: "The record was not scored as a normal completed game.",
      };
    }
    return {
      title: "Technical incomplete",
      detail: `${owner} could not complete the recorded game.`,
    };
  }

  function frameDescription(frame) {
    if (frame.is_benchmark_start) return "Frozen opening boundary";
    if (frame.series_move === 0) return frame.notation;
    const phase = frame.phase === "opening" ? "Opening fixture" : frame.engine_name;
    return `${phase} · ${frame.san || frame.uci}`;
  }

  function gameOptionLabel(game) {
    const status = game.status === "completed"
      ? game.result
      : game.status === "timeout"
        ? "timeout"
        : `${game.status} incomplete`;
    return `Game ${game.game_number} · Pair ${game.pair_number} · ${status}`;
  }

  function showToast(message) {
    globalThis.clearTimeout(state.toastTimer);
    dom.match_toast.textContent = message;
    dom.match_toast.hidden = false;
    state.toastTimer = globalThis.setTimeout(() => {
      dom.match_toast.hidden = true;
    }, 2200);
  }

  function renderPlayers(game, frame) {
    const bottomColor = state.flipped ? "black" : "white";
    const topColor = opposite(bottomColor);
    const active = activeColor(game, frame);
    const rows = [
      {
        row: dom.match_top_player,
        colorNode: dom.match_top_color,
        name: dom.match_top_name,
        meta: dom.match_top_meta,
        turn: dom.match_top_turn,
        color: topColor,
      },
      {
        row: dom.match_bottom_player,
        colorNode: dom.match_bottom_color,
        name: dom.match_bottom_name,
        meta: dom.match_bottom_meta,
        turn: dom.match_bottom_turn,
        color: bottomColor,
      },
    ];
    for (const item of rows) {
      const player = playerForColor(game, item.color);
      setPlayerColor(item.colorNode, item.color);
      item.name.textContent = engineLabel(player);
      item.meta.textContent = `${item.color[0].toUpperCase()}${item.color.slice(1)} · ${player.key === "local" ? player.engine_version : player.adapter_version}`;
      item.row.classList.toggle("is-active", active === item.color);
      item.turn.textContent = frame.phase === "opening"
        ? "Opening"
        : active === null
          ? game.completed ? "Game over" : "Stopped"
          : active === item.color
            ? "To move"
            : "Waiting";
    }
  }

  function renderMoveChips(game, frame) {
    const seriesFrames = game.frames.filter((candidate) => (
      candidate.frame <= frame.frame
      && candidate.phase === frame.phase
      && candidate.series_number === frame.series_number
      && candidate.series_move > 0
    ));
    if (!seriesFrames.length) {
      const empty = document.createElement("span");
      empty.className = "empty-chip";
      empty.textContent = frame.is_benchmark_start ? "Opening fixed" : "No moves yet";
      dom.match_move_chips.replaceChildren(empty);
      return;
    }
    dom.match_move_chips.replaceChildren(...seriesFrames.map((item) => {
      const chip = document.createElement("span");
      chip.className = "move-chip";
      const number = document.createElement("b");
      number.textContent = String(item.series_move);
      chip.append(number, document.createTextNode(item.san || item.uci));
      return chip;
    }));
  }

  function renderPosition(game, frame) {
    dom.match_position_label.textContent = `Game ${game.game_number} of ${state.bundle.games.length} · Pair ${game.pair_number}`;
    dom.match_series_number.textContent = String(frame.series_number);
    dom.match_phase.className = "boundary-pill";
    if (frame.is_benchmark_start) {
      dom.match_position_title.textContent = `Benchmark start · Series ${frame.series_number}`;
      dom.match_position_detail.textContent = "The neutral opening is loaded; engine play begins from here.";
      dom.match_phase.textContent = "Match start";
      dom.match_phase.classList.add("is-boundary");
    } else if (frame.phase === "opening") {
      dom.match_position_title.textContent = frame.series_move === 0
        ? "Neutral opening fixture"
        : `Opening · Series ${frame.series_number}, move ${frame.series_move}`;
      dom.match_position_detail.textContent = frameDescription(frame);
      dom.match_phase.textContent = "Opening";
      dom.match_phase.classList.add(frame.is_series_end ? "is-complete" : "is-mid-series");
    } else {
      dom.match_position_title.textContent = `Series ${frame.series_number} · move ${frame.series_move} of ${frame.series_length}`;
      dom.match_position_detail.textContent = frameDescription(frame);
      dom.match_phase.textContent = frame.is_series_end ? "Series complete" : "In series";
      dom.match_phase.classList.add(frame.is_series_end ? "is-complete" : "is-mid-series");
    }
    renderMoveChips(game, frame);
  }

  function renderBoard(game, frame) {
    BOARD_RENDERER.render(dom.board, {
      fen: frame.fen,
      flipped: state.flipped,
      lastMove: frame.last_move,
      interactive: false,
      ariaLabel: `Recorded match board, game ${game.game_number}, position ${frame.frame + 1} of ${game.frames.length}. ${state.flipped ? "Black" : "White"} pieces at the bottom. Input is locked.`,
    });
    dom.board.setAttribute("aria-busy", "false");
    dom.board_loading.classList.add("is-hidden");
    dom.board_shell.classList.add("is-reviewing");
  }

  function renderReplayControls(game, frame) {
    dom.replay_previous.disabled = frame.frame <= 0;
    dom.replay_next.disabled = frame.frame >= game.frames.length - 1;
    dom.replay_start.disabled = frame.frame === game.benchmark_start_frame;
    dom.replay_flip.disabled = false;
    dom.replay_play.disabled = game.frames.length <= 1;
    dom.replay_play.classList.toggle("is-playing", state.playing);
    dom.replay_play.querySelector("span").textContent = state.playing ? "Ⅱ" : "▶";
    dom.replay_play.querySelector("span:last-child").textContent = state.playing ? "Pause" : "Play";
    dom.replay_play.setAttribute("aria-label", state.playing ? "Pause replay" : "Play replay");
    dom.replay_progress.disabled = false;
    dom.replay_progress.max = String(game.frames.length - 1);
    dom.replay_progress.value = String(frame.frame);
    dom.replay_position.textContent = `Position ${frame.frame + 1} of ${game.frames.length}`;
    dom.replay_move_label.textContent = frameDescription(frame);
  }

  function renderStatus(game) {
    const status = VALID_STATUSES.has(game.status) ? game.status : "integrity";
    const copy = statusCopy(game);
    dom.match_status.className = `match-status-card status-${status}`;
    dom.match_status_title.textContent = copy.title;
    dom.match_status_detail.textContent = copy.detail;
    dom.match_failure_details.hidden = game.completed;
    dom.match_failure_copy.textContent = game.completed
      ? ""
      : `${humanize(game.terminal_reason)}. ${game.error || "No additional error text was recorded."}`;
    dom.match_board_notice.className = `boundary-notice ${game.completed ? "is-ready" : status === "timeout" ? "is-warning" : "is-game-over"}`;
    dom.match_board_notice_text.textContent = game.completed
      ? `${resultLabel(game)}. This is a normal completed game.`
      : `${copy.title}. The game remains visibly incomplete and is not presented as a completed result.`;
  }

  function renderSummary() {
    const summary = state.bundle.summary;
    const wdl = summary.local_game_wdl;
    dom.summary_completed.textContent = `${summary.completed_games}/${summary.scheduled_games}`;
    dom.summary_wdl.textContent = `${wdl.wins}–${wdl.draws}–${wdl.losses}`;
    dom.summary_incomplete.textContent = String(summary.incomplete_games);
    dom.match_game_count.textContent = `${summary.scheduled_games} games`;
  }

  function seriesTargets(game) {
    const targets = [{
      frame: game.benchmark_start_frame,
      label: "Benchmark start",
      detail: game.opening_case_id,
      kind: "start",
    }];
    game.frames
      .filter((frame) => frame.phase === "benchmark" && frame.is_series_end)
      .forEach((frame) => {
        targets.push({
          frame: frame.frame,
          label: `S${frame.series_number} · ${frame.engine_name}`,
          detail: frame.notation,
          kind: frame.outcome ? "terminal" : "series",
        });
      });
    return targets;
  }

  function renderSeriesList(game) {
    const targets = seriesTargets(game);
    dom.match_series_count.textContent = `${game.series_played} played`;
    dom.match_series_list.replaceChildren(...targets.map((target) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "match-series-row";
      button.dataset.frame = String(target.frame);
      button.classList.toggle("is-current", target.frame === state.frameIndex);
      const title = document.createElement("strong");
      title.textContent = target.label;
      const detail = document.createElement("small");
      detail.textContent = target.detail;
      const marker = document.createElement("span");
      marker.textContent = target.kind === "terminal" ? "●" : "›";
      marker.setAttribute("aria-hidden", "true");
      button.append(title, detail, marker);
      return button;
    }));
  }

  function appendProvenance(label, value) {
    const term = document.createElement("dt");
    term.textContent = label;
    const definition = document.createElement("dd");
    definition.textContent = String(value ?? "not recorded");
    definition.title = String(value ?? "not recorded");
    dom.match_provenance.append(term, definition);
  }

  function renderProvenance(game) {
    const source = state.bundle.source;
    const provenance = state.bundle.provenance;
    const local = state.bundle.players.local;
    const bucephalus = state.bundle.players.bucephalus;
    const controls = state.bundle.controls;
    dom.match_provenance.replaceChildren();
    appendProvenance("Report", source.report_id);
    appendProvenance("Receipt SHA-256", source.receipt_sha256);
    appendProvenance("Replay data SHA-256", state.manifest.data_sha256);
    appendProvenance("Game ID", game.game_id);
    appendProvenance("Pair ID", game.pair_id);
    appendProvenance("Opening", game.opening_case_id);
    appendProvenance("Rules", provenance.ruleset_version);
    appendProvenance("SPC commit", local.source_commit);
    appendProvenance("SPC native source", local.native_source_identity);
    appendProvenance("Bucephalus commit", bucephalus.upstream_commit);
    appendProvenance("Bucephalus executable", bucephalus.executable_sha256);
    appendProvenance("Control", `${controls.wall_seconds_per_move}s equal end-to-end wall per call`);
  }

  function renderGamePicker(game) {
    dom.match_game.value = String(state.gameIndex);
    dom.match_previous_game.disabled = state.gameIndex <= 0;
    dom.match_next_game.disabled = state.gameIndex >= state.bundle.games.length - 1;
    const pair = state.bundle.pairs[game.pair_number - 1];
    const pairResult = pair.status === "completed"
      ? `${humanize(pair.result)} · SPC ${pair.local_points}/2`
      : `${humanize(pair.status)} · pair incomplete`;
    dom.match_pair_label.textContent = `Pair ${game.pair_number} of ${state.bundle.pairs.length} · ${game.opening_case_id} · ${pairResult}`;
  }

  function renderAll() {
    const game = currentGame();
    const frame = currentFrame(game);
    if (!game || !frame) return;
    renderGamePicker(game);
    renderPosition(game, frame);
    renderBoard(game, frame);
    renderPlayers(game, frame);
    renderReplayControls(game, frame);
    renderStatus(game);
    renderSeriesList(game);
    renderProvenance(game);
    updateLocation();
  }

  function stopPlayback() {
    if (state.timer !== null) globalThis.clearInterval(state.timer);
    state.timer = null;
    state.playing = false;
  }

  function setFrame(index, { stop = true } = {}) {
    const game = currentGame();
    if (!game) return;
    if (stop) stopPlayback();
    state.frameIndex = clamp(Math.floor(Number(index) || 0), 0, game.frames.length - 1);
    renderAll();
  }

  function stepFrame(direction, options = {}) {
    setFrame(state.frameIndex + direction, options);
  }

  function togglePlayback() {
    const game = currentGame();
    if (!game) return;
    if (state.playing) {
      stopPlayback();
      renderReplayControls(game, currentFrame(game));
      return;
    }
    if (state.frameIndex >= game.frames.length - 1) {
      state.frameIndex = game.benchmark_start_frame;
    }
    state.playing = true;
    renderAll();
    state.timer = globalThis.setInterval(() => {
      const activeGame = currentGame();
      if (!activeGame || state.frameIndex >= activeGame.frames.length - 1) {
        stopPlayback();
        if (activeGame) renderReplayControls(activeGame, currentFrame(activeGame));
        return;
      }
      stepFrame(1, { stop: false });
    }, PLAYBACK_INTERVAL_MS);
  }

  function selectGame(index, { announce = true, frame = null } = {}) {
    stopPlayback();
    state.gameIndex = clamp(
      Math.floor(Number(index) || 0),
      0,
      state.bundle.games.length - 1,
    );
    const game = currentGame();
    state.frameIndex = frame === null
      ? game.benchmark_start_frame
      : clamp(Math.floor(Number(frame) || 0), 0, game.frames.length - 1);
    renderAll();
    if (announce) showToast(gameOptionLabel(game));
  }

  function populateGames() {
    dom.match_game.replaceChildren(...state.bundle.games.map((game, index) => {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = gameOptionLabel(game);
      return option;
    }));
    dom.match_game.disabled = false;
  }

  function updateLocation() {
    const url = new URL(globalThis.location.href);
    url.searchParams.set("game", String(state.gameIndex + 1));
    url.searchParams.set("frame", String(state.frameIndex));
    globalThis.history.replaceState(null, "", url);
  }

  function requestedLocation() {
    const values = new URLSearchParams(globalThis.location.search);
    return {
      game: clamp(Math.floor(Number(values.get("game")) || 1) - 1, 0, 99),
      frame: values.has("frame") ? Math.max(0, Math.floor(Number(values.get("frame")) || 0)) : null,
    };
  }

  async function sha256Hex(bytes) {
    const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)]
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  }

  async function loadBundle() {
    if (!globalThis.crypto?.subtle) throw new Error("Browser SHA-256 verification is unavailable.");
    const manifestResponse = await fetch("./matches/match-viewer-manifest.json", {
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!manifestResponse.ok) throw new Error(`Replay manifest failed (${manifestResponse.status}).`);
    const manifest = await manifestResponse.json();
    if (
      manifest?.schema !== "spc-match-viewer-manifest-v1"
      || !/^[0-9a-f]{64}$/.test(manifest.data_sha256 || "")
      || !/^[0-9a-f]{64}$/.test(manifest.receipt_sha256 || "")
      || !/^match\.[0-9a-f]{64}\.json$/.test(manifest.data_file || "")
      || !manifest.data_file.includes(manifest.data_sha256)
    ) {
      throw new Error("Replay manifest failed its content-address contract.");
    }
    const dataResponse = await fetch(`./matches/${manifest.data_file}`, {
      cache: "force-cache",
      credentials: "same-origin",
    });
    if (!dataResponse.ok) throw new Error(`Replay data failed (${dataResponse.status}).`);
    const bytes = await dataResponse.arrayBuffer();
    const digest = await sha256Hex(bytes);
    if (digest !== manifest.data_sha256) throw new Error("Replay data SHA-256 mismatch.");
    const bundle = JSON.parse(new TextDecoder().decode(bytes));
    if (
      bundle?.schema !== "spc-bucephalus-match-viewer-v1"
      || bundle?.source?.receipt_sha256 !== manifest.receipt_sha256
      || bundle?.provenance?.receipt_sha256 !== manifest.receipt_sha256
      || !Array.isArray(bundle.games)
      || bundle.games.length !== bundle.summary?.scheduled_games
      || !Array.isArray(bundle.pairs)
      || bundle.pairs.length !== bundle.summary?.scheduled_pairs
      || bundle.games.some((game) => game.replay_verified !== true || !VALID_STATUSES.has(game.status))
    ) {
      throw new Error("Replay data failed its immutable record contract.");
    }
    state.manifest = manifest;
    state.bundle = bundle;
  }

  function bindEvents() {
    dom.replay_previous.addEventListener("click", () => stepFrame(-1));
    dom.replay_next.addEventListener("click", () => stepFrame(1));
    dom.replay_play.addEventListener("click", togglePlayback);
    dom.replay_start.addEventListener("click", () => setFrame(currentGame().benchmark_start_frame));
    dom.replay_flip.addEventListener("click", () => {
      state.flipped = !state.flipped;
      renderAll();
      showToast(`${state.flipped ? "Black" : "White"} at the bottom`);
    });
    dom.replay_progress.addEventListener("input", () => setFrame(dom.replay_progress.value));
    dom.match_game.addEventListener("change", () => selectGame(dom.match_game.value));
    dom.match_previous_game.addEventListener("click", () => selectGame(state.gameIndex - 1));
    dom.match_next_game.addEventListener("click", () => selectGame(state.gameIndex + 1));
    dom.match_series_list.addEventListener("click", (event) => {
      const row = event.target.closest("[data-frame]");
      if (row) setFrame(row.dataset.frame);
    });
    document.addEventListener("keydown", (event) => {
      const editing = event.target.matches("input, select, textarea, button");
      if (editing || event.altKey || event.ctrlKey || event.metaKey) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        stepFrame(-1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        stepFrame(1);
      } else if (event.key === " ") {
        event.preventDefault();
        togglePlayback();
      } else if (event.key.toLowerCase() === "f") {
        event.preventDefault();
        state.flipped = !state.flipped;
        renderAll();
      } else if (event.key === "Home") {
        event.preventDefault();
        setFrame(currentGame().benchmark_start_frame);
      } else if (event.key === "End") {
        event.preventDefault();
        setFrame(currentGame().frames.length - 1);
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden && state.playing) {
        stopPlayback();
        renderReplayControls(currentGame(), currentFrame());
      }
    });
  }

  function renderLoadFailure(error) {
    stopPlayback();
    dom.board_loading_text.textContent = "Replay unavailable";
    dom.match_status.className = "match-status-card status-integrity";
    dom.match_status_title.textContent = "Replay verification failed";
    dom.match_status_detail.textContent = error instanceof Error ? error.message : String(error);
    dom.match_board_notice.className = "boundary-notice is-game-over";
    dom.match_board_notice_text.textContent = "No unverified match data was shown.";
  }

  async function initialize() {
    bindEvents();
    try {
      await loadBundle();
      populateGames();
      renderSummary();
      const requested = requestedLocation();
      selectGame(requested.game, { announce: false, frame: requested.frame });
      showToast(`Verified replay ${compactHash(state.manifest.data_sha256)}`);
    } catch (error) {
      renderLoadFailure(error);
    }
  }

  void initialize();
})();
