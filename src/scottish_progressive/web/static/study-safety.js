(() => {
  "use strict";

  function entryCount(value) {
    return value && typeof value === "object" ? Object.keys(value).length : 0;
  }

  function samePrefix(left, right) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((move, index) => String(move) === String(right[index]));
  }

  function planSavedPositionLoad({
    study,
    currentBoundary,
    currentPrefix,
    savedBoundary,
    savedPrefix,
    boundaryKey,
  }) {
    const nodeCount = entryCount(study?.nodes);
    const analysisCount = entryCount(study?.analyses);
    const preserveStudy = boundaryKey(currentBoundary) === boundaryKey(savedBoundary)
      && samePrefix(currentPrefix, savedPrefix);
    return Object.freeze({
      nodeCount,
      analysisCount,
      preserveStudy,
      confirmReplacement: !preserveStudy && (nodeCount > 0 || analysisCount > 0),
    });
  }

  function confirmSavedPositionReplacement(plan, message, confirm) {
    return !plan.confirmReplacement || Boolean(confirm(message));
  }

  globalThis.ScottishProgressiveStudySafety = Object.freeze({
    confirmSavedPositionReplacement,
    planSavedPositionLoad,
  });
})();
