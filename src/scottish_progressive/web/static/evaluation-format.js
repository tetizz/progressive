(() => {
  "use strict";

  const DEFAULT_MATE_SCORE = 1_000_000;
  const MATE_WINDOW = 10_000;
  const BALANCED_THRESHOLD = 25;

  function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function proofSide(evidence = {}) {
    return [evidence.proof, evidence.proven_result, evidence.forced]
      .map((value) => String(value ?? "").toLowerCase())
      .find((value) => value === "white" || value === "black") || null;
  }

  function rawLabel(score) {
    const magnitude = Number.isInteger(score)
      ? String(Math.abs(score))
      : String(Math.round(Math.abs(score) * 100) / 100);
    return score > 0 ? `+${magnitude}` : score < 0 ? `-${magnitude}` : "0";
  }

  function advantageBand(points) {
    if (points < 100) return { label: "Small edge", compact: "+" };
    if (points < 300) return { label: "Moderate edge", compact: "+" };
    if (points < 600) return { label: "Clear edge", compact: "++" };
    if (points < 1_000) return { label: "Large edge", compact: "++" };
    return { label: "Overwhelming heuristic edge", compact: "+++" };
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
        rawScore: null,
        rawLabel: null,
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
        rawScore: score,
        rawLabel: rawLabel(score),
        mate: true,
        mateDistance: hasDistance ? distance : null,
      };
    }

    const points = Math.abs(score);
    const scoreLabel = rawLabel(score);
    const rawSpoken = score > 0
      ? `plus ${scoreLabel.slice(1)}`
      : score < 0
        ? `minus ${scoreLabel.slice(1)}`
        : "zero";
    const unprovenMateLikeScore = Boolean(side)
      && points >= mateScore - MATE_WINDOW
      && points <= mateScore;
    if (unprovenMateLikeScore) {
      return {
        available: true,
        label: `${side}: Extreme score (unproven)`,
        compact: `${side === "White" ? "W" : "B"}?`,
        plain: `Unproven extreme score for ${side}`,
        spoken: `${side} has an extreme score without a mate proof. Raw engine score ${rawSpoken} heuristic points`,
        side,
        rawScore: score,
        rawLabel: scoreLabel,
        mate: false,
        mateDistance: null,
      };
    }

    if (!side || points < BALANCED_THRESHOLD) {
      return {
        available: true,
        label: "Roughly balanced",
        compact: "=",
        plain: "Roughly balanced",
        spoken: `Roughly balanced. Raw engine score ${rawSpoken} heuristic points`,
        side: null,
        rawScore: score,
        rawLabel: scoreLabel,
        mate: false,
        mateDistance: null,
      };
    }

    const band = advantageBand(points);
    return {
      available: true,
      label: `${side}: ${band.label}`,
      compact: `${side === "White" ? "W" : "B"}${band.compact}`,
      plain: `${band.label} for ${side}`,
      spoken: `${side} has a ${band.label.toLowerCase()}. Raw engine score ${rawSpoken} heuristic points`,
      side,
      rawScore: score,
      rawLabel: scoreLabel,
      mate: false,
      mateDistance: null,
    };
  }

  function loss(value) {
    const points = finiteNumber(value);
    if (points === null) return "Loss not reported";
    return `${rawLabel(Math.abs(points)).replace(/^\+/, "")} raw heuristic-point loss`;
  }

  globalThis.ScottishProgressiveEvaluation = Object.freeze({
    DEFAULT_MATE_SCORE,
    BALANCED_THRESHOLD,
    describe,
    loss,
  });
})();
