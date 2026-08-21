(() => {
  "use strict";

  function cloneBoundary(boundary) {
    return {
      fen: String(boundary.fen),
      series: Number(boundary.series),
      quiet_series: Number(boundary.quiet_series || 0),
      ep_targets: [...(boundary.ep_targets || [])],
    };
  }

  function prepareCompletedSeries({ boundary, nextState, prefix, prefixSan }) {
    if (!nextState || Number(nextState.series) !== Number(boundary.series) + 1) {
      throw new Error("A completed series must advance to the next numbered boundary.");
    }
    if (!Array.isArray(prefix) || prefix.length < 1 || prefix.length > Number(boundary.series)) {
      throw new Error("A completed series needs a legal non-empty move prefix.");
    }
    const key = [
      boundary.fen,
      boundary.series,
      boundary.quiet_series || 0,
      (boundary.ep_targets || []).join(","),
      prefix.join(","),
    ].join("|");
    return {
      key,
      historyEntry: {
        boundary: cloneBoundary(boundary),
        prefix: [...prefix],
        prefixSan: [...(prefixSan || prefix)],
      },
      nextBoundary: cloneBoundary(nextState),
      movesRemaining: Number(nextState.series),
    };
  }

  function createGate() {
    let active = null;
    return Object.freeze({
      run(key, work) {
        if (active) return active.promise;
        const promise = Promise.resolve()
          .then(work)
          .finally(() => {
            if (active?.promise === promise) active = null;
          });
        active = { key, promise };
        return promise;
      },
      isActive() {
        return active !== null;
      },
      activeKey() {
        return active?.key || null;
      },
      wait() {
        return active?.promise || Promise.resolve(null);
      },
    });
  }

  globalThis.ScottishProgressivePlayHandoff = Object.freeze({
    createGate,
    prepareCompletedSeries,
  });
})();
