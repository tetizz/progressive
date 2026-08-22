from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "scottish_progressive" / "web" / "static"
NODE = shutil.which("node")


def test_public_assets_are_project_pages_safe_and_keep_local_same_origin() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'name="spc-api-origin" content="https://progressive-ui9q.onrender.com"' in index
    assert "connect-src 'self' https://progressive-ui9q.onrender.com" in index
    assert "script-src 'self' blob: 'wasm-unsafe-eval'" in index
    assert "worker-src 'self';" in index
    assert "worker-src 'self' blob:" not in index
    assert 'href="./styles.css"' in index
    assert 'href="./" aria-label="Scottish Progressive home"' in index
    assert 'href="./THIRD_PARTY_NOTICES.txt"' in index
    assert 'src="/app.js"' not in index
    assert 'src="./app.js"' in index
    assert index.index('src="./browser-prefix-contract.js"') < index.index(
        'src="./browser-engine-client.js"'
    )
    assert index.index('src="./browser-engine-client.js"') < index.index(
        'src="./app.js"'
    )
    assert "Checking legal moves" not in index
    assert 'id="board-loading-text">Loading board…</span>' in index
    assert 'dom.engine_status_text.textContent = "Loading native engine…"' in app
    assert "Searching locally · WASM · ${threads} thread" in app
    assert 'analysis.legal_validation_runtime !== "compiled-wasm"' in app
    assert "? analysis.checked_prefix" in app
    assert 'return `./pieces/cburnett/' in app
    assert 'const PUBLIC_SITE_HOST = "tetizz.github.io"' in app
    assert 'const PUBLIC_SITE_PATH = "/progressive"' in app
    assert 'const API_ORIGIN = isPublicPagesSite ? configuredApiOrigin : ""' in app
    assert 'fetch(`${API_ORIGIN}${path}`' in app
    assert 'if (requestOptions.body !== undefined' in app
    assert 'error.code = "invalid-api-response"' in app
    assert 'const PUBLIC_HEALTH_TIMEOUT_MS = 20_000' in app
    assert 'const PUBLIC_HEALTH_WAKE_DELAYS_MS = [' in app
    assert 'dom.engine_status_text.textContent = "Waking engine…"' in app
    assert 'isPublicServiceWakeError(error, { includeAbort: true })' in app
    assert 'PUBLIC_ENGINE_RECONNECT_DELAYS_MS[reconnectAttempt]' in app


def test_deployment_manifests_keep_pages_and_render_on_the_same_commit() -> None:
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(
        encoding="utf-8"
    )

    assert "SPC_ALLOWED_CORS_ORIGIN" in render
    assert "value: https://tetizz.github.io" in render
    assert "SPC_OMIT_STALE_OPENING_REPORTS" in render
    assert "autoDeployTrigger: commit" in render
    assert "branches: [main]" in workflow
    assert "src/scottish_progressive/web/static" in workflow
    assert "actions/deploy-pages@v4" in workflow
    assert "Wait for matching deployed engine" in workflow
    assert "Validate certified browser engine bundle" in workflow
    assert "--validate-existing src/scottish_progressive/web/static/engine" in workflow
    for asset in (
        "browser-prefix-contract.js",
        "browser-engine-client.js",
        "browser-engine-worker.js",
        "wasm-kernel-adapter.js",
        "engine/browser-engine-manifest.json",
    ):
        assert asset in workflow
    assert "actual == expected" in workflow
    assert 'limits.get("maximum_depth") == 5' in workflow
    assert 'limits.get("maximum_seconds") == 30.0' in workflow
    assert (
        'limits.get("maximum_generation_positions") == 10_000_000' in workflow
    )
    assert 'runtime.get("cpu_count_source") == "RENDER_CPU_COUNT"' in workflow
    assert 'limits.get("native_threads") == 1' in workflow
    assert '== "single-thread-pool-avoidance"' in workflow
    assert "Build commit-addressed Pages artifact" in workflow
    assert "python scripts/build_pages_site.py" in workflow
    assert '--version "$GITHUB_SHA"' in workflow
    assert "path: _site" in workflow


def test_pages_artifact_versions_every_executable_asset(
    tmp_path: Path,
) -> None:
    output = tmp_path / "_site"
    version = "0123456789abcdef0123456789abcdef01234567"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_pages_site.py"),
            "--source",
            str(STATIC),
            "--output",
            str(output),
            "--version",
            version,
        ],
        check=True,
    )

    source_index = (STATIC / "index.html").read_text(encoding="utf-8")
    deployed_index = (output / "index.html").read_text(encoding="utf-8")
    assets = (
        ("href", "styles.css"),
        ("src", "study-safety.js"),
        ("src", "evaluation-format.js"),
        ("src", "play-handoff.js"),
        ("src", "play-timeline.js"),
        ("src", "browser-prefix-contract.js"),
        ("src", "browser-engine-client.js"),
        ("src", "app.js"),
    )
    for attribute, asset in assets:
        assert f'{attribute}="./{asset}"' in source_index
        assert f'{attribute}="./{asset}?v={version}"' in deployed_index
    assert deployed_index.count(f"?v={version}") == len(assets)
    assert (output / "app.js").read_bytes() == (STATIC / "app.js").read_bytes()
    assert (output / "browser-prefix-contract.js").read_bytes() == (
        STATIC / "browser-prefix-contract.js"
    ).read_bytes()
    assert (output / "browser-engine-worker.js").is_file()
    assert (output / "wasm-kernel-adapter.js").is_file()


def test_browser_engine_assets_are_fail_closed_and_receipted() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    client = (STATIC / "browser-engine-client.js").read_text(encoding="utf-8")
    adapter = (STATIC / "wasm-kernel-adapter.js").read_text(encoding="utf-8")

    assert app.index("await browserEngineClient.preflight({})") < app.index(
        'requestJson("/api/health"'
    )
    assert 'result.publishable !== true' in client
    assert 'result.safety_certified !== true' in client
    assert 'result.legal_series_certified !== true' in client
    assert 'result.authoritative_replay_certified !== true' in client
    assert 'runtime: "browser-wasm"' in client
    assert "wall_time_seconds: wallTimeSeconds" in client
    assert "completed_depth: completedDepth" in client
    assert "artifact_fingerprint: this.identity.wasm_sha256" in client
    assert "certificate_schema: this.identity.certificate_schema" in client
    assert "canonical_replay_certified: true" in client
    assert "raw?.publishable === true" in adapter
    assert "raw?.legal_series_certified === true" in adapter
    assert "raw?.authoritative_replay_certified === true" in adapter
    assert "_spc_boundary_kernel_search_json" in adapter
    assert 'const runtimeVariant = "single"' in adapter
    assert "pthreadAvailable" not in adapter
    assert "browser-wasm-pthread" not in app
    assert "await moduleImporter(moduleBytes, moduleUrl)" in adapter
    assert "await import(moduleUrl.href)" not in adapter
    assert "new Blob(" in adapter
    assert "URL.createObjectURL" in adapter
    assert "URL.revokeObjectURL" in adapter
    assert "validateRuntimeMemory(module, variant.memory_limits" in adapter
    assert "module_js_sha256: identity.module_js_sha256" in adapter
    assert "certificate_schema: safetyCertificate?.schema ?? null" in adapter
    assert "evidence.differential_cases < 1" in adapter
    assert "evidence.differential_cases < MIN_PREFIX_DIFFERENTIAL_CASES" in adapter
    assert "limits.depth > certified.maximum_depth" in adapter
    assert "limits.max_generation_positions > certified.maximum_generation_positions" in adapter
    assert "nextState.quiet_series" in client
    assert "nextStateEp === null" in client
    assert "nextStateFen[1] === requestFen?.[1]" in client
    assert "sameFenPositionExceptEp" in client
    assert "memoryBytes > memory.estimated_peak_bytes" in client
    assert "this.ready = false" in client
    assert "browser-analysis-deadline" in client
    assert "analysisDeadlineMs" in app
    assert "worker?.terminate()" in client
    assert "Synchronous WebAssembly cannot consume a queued cancel message" in client
    assert "_spc_boundary_prefix_contract_json" in adapter
    assert "_spc_boundary_prefix_json" in adapter
    assert "validateNativePrefixContract(module, variant.prefix_contract)" in adapter
    assert 'this._call("prefix", request' in client
    assert "PREFIX_API.validatePrefixResult(result, request, this.identity)" in client
    assert "this.activeAnalysis !== null || this.activePrefix !== null" in client
    assert 'path === "/api/prefix"' in app
    assert "BROWSER_PREFIX.routePrefixRequest" in app
    assert "currentPrefixAuthority()" in app
    assert "progressive_ep: cursor.ep_targets" in app
    assert "promoted_hex: cursor.promoted_hex" in app
    assert "chess960: cursor.chess960 === true" in app
    assert (STATIC / "browser-prefix-contract.js").read_text(encoding="utf-8") == (
        ROOT / "browser-prefix-contract.js"
    ).read_text(encoding="utf-8")


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser asset tests")
def test_player_evaluations_use_pawns_sides_and_sound_mate_notation() -> None:
    script = r"""
require(process.argv[1]);
const evaluation = globalThis.ScottishProgressiveEvaluation;
const values = {
  white: evaluation.describe(84),
  black: evaluation.describe(-152),
  equal: evaluation.describe(0),
  whiteMate: evaluation.describe(999999, { proof: "white", mate_score: 1000000 }),
  blackMate: evaluation.describe(-999997, { proven_result: "black", mate_score: 1000000 }),
  mismatchedProof: evaluation.describe(999999, { proof: "black", mate_score: 1000000 }),
  noProof: evaluation.describe(-999997, { mate_score: 1000000 }),
  loss: evaluation.loss(35),
};
process.stdout.write(JSON.stringify(values));
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "evaluation-format.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    values = json.loads(completed.stdout)

    assert values["white"]["label"] == "White +0.84"
    assert values["black"]["label"] == "Black +1.52"
    assert values["equal"]["label"] == "Equal"
    assert values["whiteMate"]["label"] == "Mate for White (M1)"
    assert values["whiteMate"]["spoken"] == "White mates in 1 complete series"
    assert values["blackMate"]["label"] == "Mate for Black (M3)"
    assert values["blackMate"]["spoken"] == "Black mates in 3 complete series"
    assert values["mismatchedProof"]["mate"] is False
    assert values["mismatchedProof"]["label"] == "White +9999.99"
    assert values["noProof"]["mate"] is False
    assert values["noProof"]["label"] == "Black +9999.97"
    assert values["loss"] == "0.35 pawn-equivalent Progressive loss"


def test_visible_evaluation_surfaces_share_the_human_formatter() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")

    assert index.index('src="./evaluation-format.js"') < index.index(
        'src="./app.js"'
    )
    assert "About 100 evaluation points equals one pawn" in index
    assert "not calibrated Stockfish centipawns" in index
    assert 'id="result-score">Equal<' in index
    assert "formatPoints(" not in app
    assert "dom.result_score.textContent = evaluation.label" in app
    assert "dom.eval_marker.textContent = evaluation.compact" in app
    assert "score.textContent = evaluation.label" in app
    assert "value.textContent = evaluation.label" in app
    assert "number.textContent = evaluation.label" in app
    assert 'aria-label", evaluation.spoken' in app


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser asset tests")
def test_completed_s6_handoff_opens_s7_once_with_seven_moves() -> None:
    script = r"""
require(process.argv[1]);
const handoff = globalThis.ScottishProgressivePlayHandoff;
const completed = {
  boundary: {
    fen: "s6-boundary",
    series: 6,
    quiet_series: 0,
    ep_targets: [],
  },
  nextState: {
    fen: "s7-boundary",
    series: 7,
    quiet_series: 0,
    ep_targets: [],
  },
  prefix: ["d7d6", "d6e5", "e5e4", "f8d6", "b8c6", "e4e3"],
  prefixSan: ["d6", "dxe5", "e4", "Bd6", "Nc6", "e3+"],
};
const plan = handoff.prepareCompletedSeries(completed);
const gate = handoff.createGate();
let starts = 0;
let release;
const blocked = new Promise((resolve) => { release = resolve; });
const first = gate.run(plan.key, async () => { starts += 1; await blocked; return plan; });
const duplicate = gate.run(plan.key, async () => { starts += 100; return null; });
release();
Promise.all([first, duplicate]).then(([left, right]) => {
  process.stdout.write(JSON.stringify({
    plan,
    starts,
    sameResult: left === right,
    active: gate.isActive(),
  }));
});
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "play-handoff.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["plan"]["historyEntry"]["prefix"] == [
        "d7d6",
        "d6e5",
        "e5e4",
        "f8d6",
        "b8c6",
        "e4e3",
    ]
    assert payload["plan"]["nextBoundary"]["series"] == 7
    assert payload["plan"]["movesRemaining"] == 7
    assert payload["starts"] == 1
    assert payload["sameResult"] is True
    assert payload["active"] is False


def test_play_flow_guards_completed_series_and_stale_handoffs() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    engine_turn = app[
        app.index("async function maybeRunEngineTurn") : app.index(
            "async function startNewPlayGame"
        )
    ]
    submit_move = app[
        app.index("async function submitMove") : app.index("function endDrag")
    ]
    handoff = app[
        app.index("async function performSeriesHandoff") : app.index(
            "async function undoMove"
        )
    ]

    assert index.index('src="./play-handoff.js"') < index.index('src="./app.js"')
    assert "PLAY_HANDOFF.isActive()" in engine_turn
    assert "state.complete" in engine_turn
    assert "state.nextState" in engine_turn
    assert "state.nextState = normalizeNextState(payload.next_state)" in app
    assert "normalizeNextState(first(payload.next_state, payload.boundary_state))" not in app
    assert "state.movesRemaining = plan.movesRemaining" in handoff
    assert "state.history.at(-1)?.handoffKey !== plan.key" in handoff
    assert "state.prefixSequence === firstAttemptSequence" in handoff
    assert "PLAY_HANDOFF.run(plan.key" in handoff
    assert "void continuePlayFlow()" in handoff
    assert "void maybeRunEngineTurn()" not in submit_move


def test_play_strength_is_explicit_and_reports_completed_not_claimed_depth() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    engine_turn = app[
        app.index("async function maybeRunEngineTurn") : app.index(
            "async function startNewPlayGame"
        )
    ]
    evidence = app[
        app.index("function renderPlaySearchEvidence") : app.index(
            "function selectPlayStrength"
        )
    ]

    assert 'id="play-strength-strong"' in index
    assert 'id="play-strength-faster"' in index
    assert 'id="play-runtime-status"' in index
    assert "Deeper · up to 30s" not in index
    assert index.count("Checking server limits…") == 2
    assert 'id="play-search-depth"' in index
    assert 'id="play-search-status"' in index
    assert 'strong: { label: "Strong", minimumDepth: 5, seconds: 30' in app
    assert 'faster: { label: "Faster", minimumDepth: 1, seconds: 5' in app
    assert 'strength: "strong"' in app
    assert "depth: search.depth" in engine_turn
    assert "max_series: search.maxSeries" in engine_turn
    assert "time_limit: search.seconds" in engine_turn
    assert "max_generation_positions: search.generationPositions" in engine_turn
    assert "best_move_only: true" in engine_turn
    assert "state.play.lastSearch = playSearchEvidence(analysis, search)" in engine_turn
    assert "Depth ${evidence.completedDepth} complete · requested ${evidence.requestedDepth}" in evidence
    assert "Selective width" in evidence
    assert "Time limit reached" in evidence
    assert "Work limit reached" in evidence
    assert "Best-move alpha-beta across up to ${evidence.maxSeries} retained series per node" in evidence
    assert 'renderStrengthOption(dom.play_strength_strong, playSearchLimits("strong"))' in evidence
    assert (
        'detail.textContent = `Depth ${optionLimits.depth} · up to ${seconds}s · ${work} work`'
        in evidence
    )
    assert "state.play.nativeThreads" in evidence
    assert "state.play.runtimeCpuCount" in evidence
    assert "single-thread-pool-avoidance" in evidence
    assert "capped at ${work} generated positions" in evidence
    assert "state.play.healthReady" in evidence
    assert "state.play.healthReady = true;" in app
    assert 'if (state.play.thinking) cancelEngineTurn();' in app


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

    assert index.index('src="./study-safety.js"') < index.index('src="./app.js"')
    assert load_saved_position.index(
        "confirmSavedPositionReplacement"
    ) < load_saved_position.index("exitPvPreview(false);")
    assert (
        "if (!loadPlan.preserveStudy) rebuildStudyFromValidatedPrefix"
        in load_saved_position
    )


def test_play_mode_uses_compiled_replay_locally_and_server_replay_as_fallback() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")
    engine_turn = app[
        app.index("async function maybeRunEngineTurn") : app.index(
            "async function startNewPlayGame"
        )
    ]

    for required_id in (
        'id="mode-play"',
        'id="mode-analyze"',
        'id="play-as-white"',
        'id="play-as-black"',
        'id="play-top-player"',
        'id="play-bottom-player"',
        'id="play-analyze-position"',
    ):
        assert required_id in index

    assert "requestEngineAnalysis(" in engine_turn
    assert 'requestJson("/api/prefix"' in engine_turn
    assert engine_turn.index("requestEngineAnalysis(") < engine_turn.index(
        'requestJson("/api/prefix"'
    )
    assert engine_turn.index('requestJson("/api/prefix"') < engine_turn.index(
        "applyPrefixPayload(checked"
    )
    assert "? analysis.checked_prefix" in engine_turn
    assert 'analysis.legal_validation_runtime !== "compiled-wasm"' in engine_turn
    assert "analysis.engine_profile_id !== state.play.engineProfileId" in engine_turn
    assert "analysis.source_fingerprint !== state.play.engineFingerprint" in engine_turn
    assert 'state.mode !== "analyze"' in app
    assert 'seriesColor() === state.play.humanColor' in app
    assert "@media (max-width: 560px)" in styles
    assert "@media (max-width: 370px)" in styles


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser asset tests")
def test_play_timeline_contains_initial_position_and_every_micro_move() -> None:
    script = r"""
require(process.argv[1]);
const timelineApi = globalThis.ScottishProgressivePlayTimeline;
const timeline = timelineApi.build({
  history: [
    {
      boundary: { fen: "fen-0", series: 1, quiet_series: 0, ep_targets: [] },
      prefix: ["e2e4"],
      prefixSan: ["e4"],
      frames: [{ board_fen: "fen-1", uci: "e2e4", san: "e4" }],
    },
    {
      boundary: { fen: "fen-1", series: 2, quiet_series: 0, ep_targets: [] },
      prefix: ["a7a6", "h7h6"],
      prefixSan: ["a6", "h6"],
      frames: [
        { board_fen: "fen-2", uci: "a7a6", san: "a6" },
        { board_fen: "fen-3", uci: "h7h6", san: "h6" },
      ],
    },
  ],
  boundary: { fen: "fen-3", series: 3, quiet_series: 0, ep_targets: [] },
  prefix: ["g1f3", "d2d4"],
  prefixSan: ["Nf3", "d4"],
  prefixFrames: [
    { board_fen: "fen-4", uci: "g1f3", san: "Nf3" },
    { board_fen: "fen-5", uci: "d2d4", san: "d4" },
  ],
  complete: false,
  outcome: null,
});
process.stdout.write(JSON.stringify({
  timeline,
  latest: timelineApi.cursorIndex(timeline, null),
  clampedLow: timelineApi.cursorIndex(timeline, -20),
  clampedHigh: timelineApi.cursorIndex(timeline, 200),
}));
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "play-timeline.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    timeline = payload["timeline"]

    assert len(timeline) == 6
    assert [position["boardFen"] for position in timeline] == [
        "fen-0",
        "fen-1",
        "fen-2",
        "fen-3",
        "fen-4",
        "fen-5",
    ]
    assert [position["lastMove"] for position in timeline] == [
        None,
        "e2e4",
        "a7a6",
        "h7h6",
        "g1f3",
        "d2d4",
    ]
    assert timeline[2]["series"] == 2
    assert timeline[2]["seriesMove"] == 1
    assert timeline[2]["prefixSan"] == ["a6"]
    assert timeline[3]["complete"] is True
    assert timeline[-1]["isLatest"] is True
    assert payload["latest"] == 5
    assert payload["clampedLow"] == 0
    assert payload["clampedHigh"] == 5


def test_play_history_navigation_is_accessible_and_locks_live_play() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")
    engine_turn = app[
        app.index("async function maybeRunEngineTurn") : app.index(
            "async function startNewPlayGame"
        )
    ]
    new_game = app[
        app.index("async function startNewPlayGame") : app.index(
            "async function selectPlayColor"
        )
    ]
    handoff = app[
        app.index("async function performSeriesHandoff") : app.index(
            "function advanceSeries"
        )
    ]

    assert index.index('src="./play-timeline.js"') < index.index('src="./app.js"')
    assert 'id="play-history-previous"' in index
    assert 'aria-label="Previous move"' in index
    assert 'id="play-history-next"' in index
    assert 'aria-label="Next move"' in index
    assert 'id="play-history-position"' in index
    assert "state.prefixFrames = prefixFramesFromPayload" in app
    assert "frames: clonePlain(state.prefixFrames, [])" in handoff
    assert "playReviewActive()" in engine_turn
    assert "&& !playReviewActive()" in app
    assert 'state.play.timelineIndex = null;' in new_game
    assert "state.prefixFrames = [];" in new_game
    assert 'event.key === "ArrowLeft" || event.key === "ArrowRight"' in app
    assert "stepPlayTimeline(event.key === \"ArrowLeft\" ? -1 : 1)" in app
    assert ".play-history-navigation" in styles
    assert ".board-shell.is-reviewing" in styles
