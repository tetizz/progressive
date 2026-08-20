from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "scottish_progressive" / "web" / "static"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser asset tests")
def test_saved_position_load_plan_preserves_or_confirms_current_study() -> None:
    script = r"""
require(process.argv[1]);
const plan = globalThis.ScottishProgressiveStudySafety.planSavedPositionLoad;
const confirmReplacement = globalThis.ScottishProgressiveStudySafety.confirmSavedPositionReplacement;
const key = (boundary) => JSON.stringify(boundary);
const current = { fen: "current", series: 2, quiet_series: 0, ep_targets: [] };
const saved = { fen: "saved", series: 3, quiet_series: 0, ep_targets: [] };
const populated = { nodes: { a: {}, b: {} }, analyses: { current: {} } };
const same = plan({
  study: populated,
  currentBoundary: current,
  currentPrefix: ["e7e5"],
  savedBoundary: current,
  savedPrefix: ["e7e5"],
  boundaryKey: key,
});
const replacement = plan({
  study: populated,
  currentBoundary: current,
  currentPrefix: ["e7e5"],
  savedBoundary: saved,
  savedPrefix: [],
  boundaryKey: key,
});
const empty = plan({
  study: { nodes: {}, analyses: {} },
  currentBoundary: current,
  currentPrefix: ["e7e5"],
  savedBoundary: saved,
  savedPrefix: [],
  boundaryKey: key,
});
const analysisOnly = plan({
  study: { nodes: {}, analyses: { current: {} } },
  currentBoundary: current,
  currentPrefix: [],
  savedBoundary: saved,
  savedPrefix: [],
  boundaryKey: key,
});
let confirmationCalls = 0;
const cancelled = confirmReplacement(replacement, "replace?", (message) => {
  confirmationCalls += 1;
  return message !== "replace?";
});
const sameApproved = confirmReplacement(same, "unused", () => {
  confirmationCalls += 100;
  return false;
});
process.stdout.write(JSON.stringify({
  same,
  replacement,
  empty,
  analysisOnly,
  cancelled,
  sameApproved,
  confirmationCalls,
}));
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "study-safety.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["same"]["preserveStudy"] is True
    assert payload["same"]["confirmReplacement"] is False
    assert payload["replacement"] == {
        "nodeCount": 2,
        "analysisCount": 1,
        "preserveStudy": False,
        "confirmReplacement": True,
    }
    assert payload["empty"]["confirmReplacement"] is False
    assert payload["analysisOnly"]["confirmReplacement"] is True
    assert payload["cancelled"] is False
    assert payload["sameApproved"] is True
    assert payload["confirmationCalls"] == 1


def test_saved_position_guard_loads_before_the_board_application() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    load_saved_position = app[app.index("async function loadSavedPosition"):]

    assert index.index('src="/study-safety.js"') < index.index('src="/app.js"')
    assert load_saved_position.index(
        "confirmSavedPositionReplacement"
    ) < load_saved_position.index("exitPvPreview(false);")
    assert (
        "if (!loadPlan.preserveStudy) rebuildStudyFromValidatedPrefix"
        in load_saved_position
    )
