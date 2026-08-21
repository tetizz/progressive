(() => {
  "use strict";

  function cloneBoundary(boundary) {
    return {
      fen: String(boundary?.fen || ""),
      series: Math.max(1, Number(boundary?.series) || 1),
      quiet_series: Math.max(0, Number(boundary?.quiet_series) || 0),
      ep_targets: [...(boundary?.ep_targets || [])].map(String),
    };
  }

  function appendSeries(timeline, entry, current) {
    const boundary = cloneBoundary(entry.boundary);
    const prefix = [...(entry.prefix || [])].map(String);
    const prefixSan = [...(entry.prefixSan || prefix)].map(String);
    const frames = [...(entry.frames || [])];
    prefix.forEach((uci, index) => {
      const frame = frames[index] || {};
      const isLast = index === prefix.length - 1;
      const complete = Boolean(entry.complete && isLast);
      timeline.push({
        boardFen: String(frame.board_fen || frame.fen || timeline.at(-1)?.boardFen || boundary.fen),
        boundary,
        series: boundary.series,
        side: boundary.series % 2 === 1 ? "white" : "black",
        seriesMove: index + 1,
        prefix: prefix.slice(0, index + 1),
        prefixSan: prefixSan.slice(0, index + 1),
        lastMove: String(frame.uci || uci),
        lastSan: String(frame.san || prefixSan[index] || uci),
        movesRemaining: complete ? 0 : Math.max(0, boundary.series - index - 1),
        complete,
        check: Boolean(complete && entry.check),
        unusedMoves: complete ? Math.max(0, Number(entry.unusedMoves) || 0) : 0,
        completionReason: complete ? entry.completionReason || null : null,
        outcome: current && isLast ? entry.outcome || null : null,
        resigned: Boolean(current && isLast && entry.resigned),
      });
    });
  }

  function build({
    history = [],
    boundary,
    prefix = [],
    prefixSan = [],
    prefixFrames = [],
    complete = false,
    check = false,
    unusedMoves = 0,
    completionReason = null,
    outcome = null,
    resigned = false,
  }) {
    const initialBoundary = cloneBoundary(history[0]?.boundary || boundary);
    const timeline = [{
      boardFen: initialBoundary.fen,
      boundary: initialBoundary,
      series: initialBoundary.series,
      side: initialBoundary.series % 2 === 1 ? "white" : "black",
      seriesMove: 0,
      prefix: [],
      prefixSan: [],
      lastMove: null,
      lastSan: null,
      movesRemaining: initialBoundary.series,
      complete: false,
      check: false,
      unusedMoves: 0,
      completionReason: null,
      outcome: null,
      resigned: false,
    }];
    history.forEach((entry) => appendSeries(timeline, {
      ...entry,
      complete: true,
    }, false));
    appendSeries(timeline, {
      boundary,
      prefix,
      prefixSan,
      frames: prefixFrames,
      complete,
      check,
      unusedMoves,
      completionReason,
      outcome,
      resigned,
    }, true);
    return timeline.map((position, index) => ({
      ...position,
      index,
      totalPositions: timeline.length,
      isLatest: index === timeline.length - 1,
    }));
  }

  function cursorIndex(timeline, requestedIndex) {
    if (!timeline.length) return 0;
    if (!Number.isInteger(requestedIndex)) return timeline.length - 1;
    return Math.max(0, Math.min(timeline.length - 1, requestedIndex));
  }

  globalThis.ScottishProgressivePlayTimeline = Object.freeze({ build, cursorIndex });
})();
