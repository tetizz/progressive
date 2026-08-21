(() => {
  "use strict";

  const DEFAULT_MATE_SCORE = 1_000_000;
  const MATE_WINDOW = 10_000;
  const POINTS_PER_PAWN = 100;

  function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function proofSide(evidence = {}) {
    return [evidence.proof, evidence.proven_result, evidence.forced]
      .map((value) => String(value ?? "").toLowerCase())
      .find((value) => value === "white" || value === "black") || null;
  }

  function plainLanguage(pawns) {
    if (pawns < 0.005) return "Equal";
    if (pawns < 0.25) return "Roughly equal";
    if (pawns < 0.75) return "Small edge";
    if (pawns < 2) return "Clear edge";
    if (pawns < 4) return "Strong advantage";
    return "Winning heuristic advantage";
  }

  function describe(value, evidence = {}) {
    const score = finiteNumber(value);
    if (score === null) {
      return {
        available: false,
        label: "Not reported",
        compact: "?",
        plain: "Evaluation not reported",
        spoken: "Progressive evaluation not reported",
        side: null,
        pawns: null,
        mate: false,
        mateDistance: null,
      };
    }

    const side = score > 0 ? "White" : score < 0 ? "Black" : null;
    const mateScore = finiteNumber(evidence.mate_score) || DEFAULT_MATE_SCORE;
    const distance = mateScore - Math.abs(score);
    const encodedMate = Boolean(side)
      && Math.abs(score) >= mateScore - MATE_WINDOW
      && Math.abs(score) <= mateScore
      && proofSide(evidence) === side.toLowerCase();

    if (encodedMate) {
      const hasDistance = Number.isInteger(distance) && distance >= 0 && distance <= MATE_WINDOW;
      const notation = hasDistance ? `M${distance}` : null;
      const spoken = hasDistance
        ? `${side} mates in ${distance} complete ${distance === 1 ? "series" : "series"}`
        : `Mate for ${side}`;
      return {
        available: true,
        label: `Mate for ${side}${notation ? ` (${notation})` : ""}`,
        compact: `${side === "White" ? "W" : "B"}${notation || "M"}`,
        plain: "Forced mate",
        spoken,
        side,
        pawns: null,
        mate: true,
        mateDistance: hasDistance ? distance : null,
      };
    }

    const pawns = Math.abs(score) / POINTS_PER_PAWN;
    if (!side || pawns < 0.005) {
      return {
        available: true,
        label: "Equal",
        compact: "=",
        plain: "Equal",
        spoken: "Equal, heuristic Progressive evaluation",
        side: null,
        pawns: 0,
        mate: false,
        mateDistance: null,
      };
    }

    const magnitude = pawns.toFixed(2);
    const compactMagnitude = pawns < 10
      ? pawns.toFixed(1)
      : pawns < 100
        ? String(Math.round(pawns))
        : "99+";
    return {
      available: true,
      label: `${side} +${magnitude}`,
      compact: `${side === "White" ? "W" : "B"}+${compactMagnitude}`,
      plain: `${plainLanguage(pawns)} for ${side}`,
      spoken: `${side} plus ${magnitude} pawns, heuristic Progressive evaluation`,
      side,
      pawns,
      mate: false,
      mateDistance: null,
    };
  }

  function loss(value) {
    const points = finiteNumber(value);
    if (points === null) return "Loss not reported";
    return `${(Math.abs(points) / POINTS_PER_PAWN).toFixed(2)} pawn-equivalent Progressive loss`;
  }

  globalThis.ScottishProgressiveEvaluation = Object.freeze({
    DEFAULT_MATE_SCORE,
    POINTS_PER_PAWN,
    describe,
    loss,
  });
})();
