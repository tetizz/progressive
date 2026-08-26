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
        'src="./root-iteration-coordinator.js"'
    )
    assert index.index('src="./root-iteration-coordinator.js"') < index.index(
        'src="./browser-root-iteration-client.js"'
    )
    assert index.index('src="./browser-root-iteration-client.js"') < index.index(
        'src="./browser-engine-client.js"'
    )
    assert index.index('src="./browser-engine-client.js"') < index.index(
        'src="./app.js"'
    )
    assert "Checking legal moves" not in index
    assert 'id="board-loading-text">Loading board…</span>' in index
    assert 'dom.engine_status_text.textContent = "Loading native engine…"' in app
    assert "certified single-thread Workers" in app
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


def test_pages_uses_the_local_certified_bundle_without_waiting_for_render() -> None:
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
    assert "Wait for matching deployed engine" not in workflow
    assert "progressive-ui9q.onrender.com/api/health" not in workflow
    assert "time.monotonic() + 15 * 60" not in workflow
    assert "Validate local certified browser engine release authority" in workflow
    assert "--validate-existing src/scottish_progressive/web/static/engine" in workflow
    for asset in (
        "browser-prefix-contract.js",
        "root-iteration-coordinator.js",
        "browser-root-iteration-client.js",
        "browser-engine-client.js",
        "browser-engine-worker.js",
        "wasm-kernel-adapter.js",
        "engine/browser-engine-manifest.json",
    ):
        assert asset in workflow
    assert "Build commit-addressed Pages artifact" in workflow
    assert "python scripts/build_pages_site.py" in workflow
    assert '--version "$GITHUB_SHA"' in workflow
    assert "path: _site" in workflow


def test_pages_never_binds_the_certified_local_engine_to_render_identity() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    health = app[app.index("async function checkHealth") : app.index("function bindEvents")]

    assert "preflightCertifiedBrowserRuntime" in health
    assert "browserRuntimeCanSearch(browserRuntime)" in health
    assert "browserRuntimeMatchesHostedIdentity(browserRuntime, health)" in health
    assert "sourceFingerprint:" not in health
    assert "engineProfileId:" not in health
    identity_matcher = app[
        app.index("function browserRuntimeMatchesHostedIdentity") :
        app.index("async function preflightCertifiedBrowserRuntime")
    ]
    assert "engine_profile_id" not in identity_matcher
    assert "engine_profile_name" not in identity_matcher
    assert "runtime.source_fingerprint" in identity_matcher
    assert "runtime.engine_version" in identity_matcher
    assert "runtime.ruleset_version" in identity_matcher
    assert 'browserEngineClient.close("browser/server engine identities differ")' in health
    assert 'reason: "browser-hosted-engine-identity-mismatch"' in health
    assert 'local engine: ${state.play.browserWasmReason}' in health
    assert "LOCAL_ENGINE_BOOTSTRAP_TIMEOUT_MS" in health
    assert "LOCAL_ENGINE_FIRST_PROBE_TIMEOUT_MS" in health
    assert "deadlineMs: browserBootstrapDeadlineMs" in health
    assert health.index("deadlineMs: browserBootstrapDeadlineMs") < health.index(
        'requestJson("/api/health"'
    )
    assert 'error?.code === "browser-analysis-deadline"' in app


def test_hosted_fallback_revalidates_identity_and_preserves_the_saved_game() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    fallback = app[
        app.index("function hostedEngineIdentity") :
        app.index("async function requestRemoteJson")
    ]
    routing = app[
        app.index("async function requestJson") :
        app.index("function playPrefixDeadlineError")
    ]
    validator = app[
        app.index("async function validateHostedAnalysisIdentity") :
        app.index("async function requestRemoteJson")
    ]
    play_turn = app[
        app.index("async function maybeRunEngineTurn") :
        app.index("async function startNewPlayGame")
    ]

    assert "async function validateHostedAnalysisIdentity" in fallback
    assert '/^[0-9a-f]{16}$/' in fallback
    assert 'requestRemoteJson("/api/health"' in fallback
    assert "sameHostedEngineIdentity(received, currentHosted)" in fallback
    assert "sameLoadedChampion(expected, currentHosted)" in fallback
    assert "hostedFallbackAuthorities.set(payload" in validator
    assert "applyHostedFallbackRuntime(" not in validator
    assert "signal?.aborted" in validator
    assert "monotonicNow() > deadlineMs" in validator
    assert "sameHostedEngineIdentity(expected, loadedChampionIdentity())" in validator
    assert "browserEngineClient?.close(`hosted fallback selected: ${reason}`)" in fallback
    assert "state.play.engineFingerprint = identity.source_fingerprint" in fallback
    assert 'state.play.runtimeMode = "server"' in fallback
    assert 'dom.engine_status_text.textContent = "Engine online"' in fallback
    assert "analysis.source_fingerprint =" not in fallback
    assert "const hostedFallback = stagedHostedFallback(analysis)" in play_turn
    assert "hostedFallback.expected, loadedChampionIdentity()" in play_turn
    assert play_turn.index("if (!checked.complete && !checked.outcome)") < play_turn.index(
        "await animateCheckedEngineSeries"
    ) < play_turn.index(
        "applyHostedFallbackRuntime(hostedFallback.health, hostedFallback.reason)"
    ) < play_turn.index("applyPrefixPayload(checked")
    assert "Continued safely on the hosted engine" in app
    assert "localFallbackReason" in routing
    assert "fallbackReason: localFallbackReason" in routing
    assert "await validateHostedAnalysisIdentity" in routing


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
        ("src", "root-iteration-coordinator.js"),
        ("src", "browser-root-iteration-client.js"),
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
    assert (output / "root-iteration-coordinator.js").read_bytes() == (
        STATIC / "root-iteration-coordinator.js"
    ).read_bytes()
    assert (output / "browser-root-iteration-client.js").read_bytes() == (
        STATIC / "browser-root-iteration-client.js"
    ).read_bytes()
    assert (output / "browser-engine-worker.js").is_file()
    assert (output / "wasm-kernel-adapter.js").is_file()


def test_browser_engine_assets_are_fail_closed_and_receipted() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    client = (STATIC / "browser-engine-client.js").read_text(encoding="utf-8")
    root_client = (STATIC / "browser-root-iteration-client.js").read_text(
        encoding="utf-8"
    )
    adapter = (STATIC / "wasm-kernel-adapter.js").read_text(encoding="utf-8")
    coordinator = (STATIC / "root-iteration-coordinator.js").read_text(
        encoding="utf-8"
    )

    assert app.index("browserRuntime = await preflightCertifiedBrowserRuntime") < app.index(
        'requestJson("/api/health"'
    )
    assert "return await browserEngineClient.preflight({ deadlineMs })" in app
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
    assert "this.activeAnalysis !== null" in client
    assert "this.activePrefix !== null" in client
    assert "this.rootRunner?.active === true" in client
    assert 'typeof result.root_scores_complete !== "boolean"' in client
    assert "result.root_bound_coverage_complete !== true" in client
    assert "receipt.root_bound_coverage_complete !== true" in client
    assert "this.rootRunner.inspectPrefix" in client
    assert "const pooledPrefix = this.rootRunner?.hasLivePool?.() === true" in client
    assert 'root_search_mode: "streaming-root-iteration"' in root_client
    assert "root_bound_coverage_complete: iteration.coverage_complete" in root_client
    assert 'schema: "spc-retained-root-horizon-proof-v1"' in root_client
    assert "pv_horizon_native_repairs" in root_client
    assert "pv_horizon_candidate_vetoes" in root_client
    assert "deadline_epoch_ms: deadlineEpochMs" in root_client
    assert "safetyWork" in root_client
    assert "mateProofCacheKey" in root_client
    assert "playLimits.safety_reserve_positions" in root_client
    assert '"_spc_root_session_search_json"' in adapter
    assert '"spc-root-horizon-research-task-v1"' in adapter
    assert '"spc-root-horizon-research-result-v1"' in adapter
    assert "bitCount16(raw.horizon_proof_hit_mask) !== raw.horizon_proof_hits" in adapter
    assert "checked_horizon_newest_proof_hit" in adapter
    assert '"_spc_series_mate_search_json"' in adapter
    assert "clampRootRemainingTime" in adapter
    assert "deadline_epoch_ms" in adapter
    assert "Best move exact; alternatives certified by alpha-beta bounds" in app
    certified_runtime = app[
        app.index("function applyCertifiedBrowserRuntime") :
        app.index("async function checkHealth")
    ]
    assert "root_geometry?.play_limits" in certified_runtime
    assert "Math.min(10_000_000" not in certified_runtime
    assert 'path === "/api/prefix"' in app
    assert "BROWSER_PREFIX.routePrefixRequest" in app
    assert "currentPrefixAuthority()" in app
    assert "progressive_ep: cursor.ep_targets" in app
    assert "promoted_hex: cursor.promoted_hex" in app
    assert "chess960: cursor.chess960 === true" in app
    assert "MAX_RETAINED_ROOT_HORIZON_PROOFS = 16" in coordinator
    assert "newestProofBit" in coordinator
    assert coordinator == (ROOT / "root-iteration-coordinator.js").read_text(
        encoding="utf-8"
    )
    assert (STATIC / "browser-prefix-contract.js").read_text(encoding="utf-8") == (
        ROOT / "browser-prefix-contract.js"
    ).read_text(encoding="utf-8")


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser asset tests")
def test_player_evaluations_use_human_bands_raw_scores_and_sound_mate_notation() -> None:
    script = r"""
require(process.argv[1]);
const evaluation = globalThis.ScottishProgressiveEvaluation;
const values = {
  white: evaluation.describe(84),
  black: evaluation.describe(-152),
  currentStart: evaluation.describe(951),
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

    assert values["white"]["label"] == "White: Small edge"
    assert values["white"]["rawLabel"] == "+84"
    assert values["black"]["label"] == "Black: Moderate edge"
    assert values["black"]["rawLabel"] == "-152"
    assert values["currentStart"]["label"] == "White: Large edge"
    assert values["currentStart"]["rawLabel"] == "+951"
    assert values["equal"]["label"] == "Roughly balanced"
    assert values["equal"]["rawLabel"] == "0"
    assert values["whiteMate"]["label"] == "Mate for White (M1)"
    assert values["whiteMate"]["spoken"] == "White mates in 1 complete series"
    assert values["blackMate"]["label"] == "Mate for Black (M3)"
    assert values["blackMate"]["spoken"] == "Black mates in 3 complete series"
    assert values["mismatchedProof"]["mate"] is False
    assert values["mismatchedProof"]["label"] == "White: Extreme score (unproven)"
    assert values["noProof"]["mate"] is False
    assert values["noProof"]["label"] == "Black: Extreme score (unproven)"
    assert values["loss"] == "35 raw heuristic-point loss"


def test_visible_evaluation_surfaces_share_the_human_formatter() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")

    assert index.index('src="./evaluation-format.js"') < index.index(
        'src="./app.js"'
    )
    assert "not pawns or Stockfish centipawns" in index
    assert "raw engine score remains visible separately" in index
    assert 'id="result-score">Roughly balanced<' in index
    assert 'id="result-raw-score">Raw engine score 0<' in index
    assert "formatPoints(" not in app
    assert "dom.result_score.textContent = evaluation.label" in app
    assert "dom.result_raw_score.textContent = evaluation.rawLabel" in app
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
    promoted_hex: "0000000000000040",
    chess960: true,
  },
  nextState: {
    fen: "s7-boundary",
    series: 7,
    quiet_series: 0,
    ep_targets: [],
    promoted_hex: "0000000000000080",
    chess960: true,
  },
  prefix: ["d7d6", "d6e5", "e5e4", "f8d6", "b8c6", "e4e3"],
  prefixSan: ["d6", "dxe5", "e4", "Bd6", "Nc6", "e3+"],
};
const plan = handoff.prepareCompletedSeries(completed);
const unknownPlan = handoff.prepareCompletedSeries({
  ...completed,
  boundary: { ...completed.boundary, promoted_hex: null, chess960: false },
  nextState: { ...completed.nextState, promoted_hex: null, chess960: false },
});
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
    unknownPlan,
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
    assert (
        payload["plan"]["historyEntry"]["boundary"]["promoted_hex"]
        == "0000000000000040"
    )
    assert payload["plan"]["historyEntry"]["boundary"]["chess960"] is True
    assert payload["plan"]["nextBoundary"]["series"] == 7
    assert payload["plan"]["nextBoundary"]["promoted_hex"] == "0000000000000080"
    assert payload["plan"]["nextBoundary"]["chess960"] is True
    assert (
        payload["unknownPlan"]["historyEntry"]["boundary"]["promoted_hex"]
        is None
    )
    assert payload["unknownPlan"]["nextBoundary"]["promoted_hex"] is None
    assert "unknown-promoted-provenance" in payload["unknownPlan"]["key"]
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
    assert 'id="play-retry-engine"' in index
    assert 'id="play-runtime-status"' in index
    assert "Deeper · up to 30s" not in index
    assert index.count("Loading local engine…") == 2
    assert "Checking server limits" not in index
    assert "Checking CPU allocation" not in index
    assert "Checking legal moves" not in index
    assert "Checking server limits" not in app
    assert "Checking CPU allocation" not in app
    assert "Checking legal moves" not in app
    assert 'id="play-search-depth"' in index
    assert 'id="play-search-status"' in index
    assert 'strong: { label: "Strong", minimumDepth: 5, seconds: 30' in app
    assert 'faster: { label: "Faster", minimumDepth: 1, seconds: 5' in app
    assert 'strength: "strong"' in app
    assert "depth: search.depth" in engine_turn
    assert "max_series: search.maxSeries" in engine_turn
    assert "time_limit: search.seconds" in engine_turn
    assert "max_generation_positions: search.generationPositions" in engine_turn
    assert "const searchDeadlineMs = monotonicNow() + search.seconds * 1000" in engine_turn
    assert "const receiptDeadlineMs = searchDeadlineMs + PLAY_ANALYSIS_RESPONSE_GRACE_MS" in engine_turn
    assert "analysisSearchDeadlineMs: searchDeadlineMs" in app
    assert "analysisDeadlineMs: receiptDeadlineMs" in app
    assert "searchDeadlineMs: analysisSearchDeadlineMs" in app
    assert "receiptDeadlineMs: analysisDeadlineMs" in app
    assert "PLAY_STRENGTHS.strong.seconds = state.play.timeLimitSeconds" not in app
    assert "PLAY_STRENGTHS.strong.generationPositions = state.play.generationPositions" not in app
    assert "generationPositions >= PLAY_TECHNICAL_WORK_CEILING" in app
    assert app.count("generationPositions: PLAY_TECHNICAL_WORK_CEILING") == 2
    assert "best_move_only: true" in engine_turn
    assert "state.play.lastSearch = playSearchEvidence(analysis, search)" in engine_turn
    assert "Last completed search · depth ${evidence.completedDepth} · requested ${evidence.requestedDepth}" in evidence
    assert "Selective width" in evidence
    assert "Time limit reached" in evidence
    assert "Work limit reached" in evidence
    assert "Best-move alpha-beta across up to ${evidence.maxSeries} retained series per node" in evidence
    assert 'renderStrengthOption(dom.play_strength_strong, playSearchLimits("strong"))' in evidence
    assert "time limit only" in evidence
    assert "no reachable position-work cap" in evidence
    assert "state.play.nativeThreads" in evidence
    assert "state.play.runtimeCpuCount" in evidence
    assert "single-thread-pool-avoidance" in evidence
    assert "capped at ${work} generated positions" in evidence
    assert "state.play.healthReady" in evidence
    assert "state.play.healthReady = true;" in app
    assert "state.play.thinking || activePlayEngineTurn" in app
    assert "function retryEngineTurn()" in app
    assert 'dom.play_retry_engine.addEventListener("click", () => { void retryEngineTurn(); })' in app
    assert "Search stopped — game saved" in app


def test_play_session_reload_uses_an_authoritatively_replayed_uci_ledger() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    persistence = app[
        app.index("function isStoredUciList") : app.index("function seriesColor")
    ]
    play_surface = app[
        app.index("function renderPlaySurface") : app.index(
            "function engineSeriesFromAnalysis"
        )
    ]
    submit_move = app[
        app.index("async function submitMove") : app.index("function endDrag")
    ]
    handoff = app[
        app.index("async function performSeriesHandoff") : app.index(
            "function advanceSeries"
        )
    ]
    engine_turn = app[
        app.index("async function maybeRunEngineTurn") : app.index(
            "async function startNewPlayGame"
        )
    ]
    initialize = app[app.index("async function initialize"):]

    assert 'PLAY_SESSION_STORAGE_KEY = "scottish-progressive-play-session-v2"' in app
    assert 'LEGACY_PLAY_SESSION_STORAGE_KEY = "scottish-progressive-play-session-v1"' in app
    assert "completedSeries" in persistence
    assert "currentPrefix" in persistence
    assert "workspace: state.playWorkspace" not in persistence
    assert "fen: START_FEN" in persistence
    assert "for (let index = 0; index < saved.completedSeries.length; index += 1)" in persistence
    assert "requestPlayPrefixJson(" in persistence
    assert "const replayCallCount = saved.completedSeries.length + 1" in persistence
    assert "PLAY_RESTORE_PER_SERIES_TIMEOUT_MS" in persistence
    assert "PLAY_RESTORE_MAX_TIMEOUT_MS" in persistence
    assert "deadlineMs: nextReplayCallDeadline()" in persistence
    assert "canonical.some((move, moveIndex) => move !== moves[moveIndex])" in persistence
    assert "nextBoundary.series !== boundary.series + 1" in persistence
    assert "authoritativeBoundaryEchoMatches(payload, boundary)" in persistence
    assert "authoritativeBoundaryEchoMatches(current, boundary)" in persistence
    assert "if (payload?.boundary_state === undefined) return false" in persistence
    assert "Saved current series failed authoritative replay" in persistence
    assert "playSessionReplayBlocked = true" in persistence
    assert persistence.index("playSessionReplayBlocked = true") < persistence.index(
        "await requestPlayPrefixJson("
    )
    assert "playSessionReplayPromise" in app
    assert 'setBoardBusy(true, "Restoring saved game…")' in persistence
    assert persistence.count("signal: controller.signal") == 2
    assert "sequence !== state.prefixSequence" in persistence
    assert "completedSeries.length > 511" in persistence
    assert "flipped: Boolean(state.playWorkspace.flipped)" in persistence
    assert "state.flipped = saved.flipped" in persistence
    assert "seriesColor(state.boundary.series) === saved.humanColor" in persistence
    for cleared_field in (
        "state.complete = false",
        "state.nextState = null",
        "state.outcome = null",
        "state.check = false",
        "state.unusedMoves = 0",
        "state.completionReason = null",
        "state.lastMove = null",
        "state.selected = null",
        "state.previewIndex = null",
        "state.positionReady = false",
    ):
        assert cleared_field in persistence
    assert "Your saved moves remain stored; validation is waiting" in persistence
    assert "playSessionLastWriteDurable = true" in persistence
    assert "playSessionLastWriteDurable = false" in persistence
    assert "Search stopped — reload may lose this game" in play_surface
    assert "recoveryBlocked || (!effectiveGameEnded" in play_surface
    assert "reviewing || recoveryBlocked || saveBlocked ? null : playOutcomeStatus()" in play_surface
    assert submit_move.index("await requireDurablePlaySession({}, { capture: true })") < submit_move.index(
        "await advanceSeries(true)"
    )
    assert handoff.index("await requireDurablePlaySession({}, { capture: true })") < handoff.index(
        "await refreshPrefix([], [])"
    )
    switch_mode = app[
        app.index("async function switchWorkspaceMode") : app.index(
            "function legalMovesFrom"
        )
    ]
    assert switch_mode.index("if (state.positionBusy)") < switch_mode.index(
        "state.prefixAbort?.abort()"
    )
    assert "dom.mode_play.disabled = state.positionBusy" in app
    assert "dom.mode_analyze.disabled = state.positionBusy" in app
    assert "Game restored after reload" in persistence
    assert 'window.addEventListener("storage"' in app
    assert "PLAY_SESSION_SCHEMA_VERSION = 2" in app
    assert "LEGACY_PLAY_SESSION_SCHEMA_VERSION = 1" in app
    assert "sessionId: state.play.sessionId" not in app
    assert "sessionId," in persistence
    assert "ownerId: playSessionTabId" in persistence
    assert "revision: playSessionRevision + 1" in persistence
    assert "storedPlayLedgerExtends(candidate, existing)" in persistence
    assert "existing.revision !== baseRevision" in persistence
    assert "sameStoredPlayState(candidate, existing)" in persistence
    stale_revision_start = persistence.index(
        "if (existing.revision !== baseRevision"
    )
    stale_revision = persistence[
        stale_revision_start : persistence.index(
            "if (!storedPlayLedgerExtends",
            stale_revision_start,
        )
    ]
    assert "existing.ownerId === playSessionTabId" in stale_revision
    assert "claimOwnership === false" in stale_revision
    assert "The loser of two simultaneous reloads must stay stale" in stale_revision
    assert "existing.ownerId !== playSessionTabId && !claimOwnership" in persistence
    assert "navigator.locks.request(PLAY_SESSION_WRITE_LOCK, commit)" in persistence
    assert "playSessionLastWriteDurable = false" in persistence
    assert "async function persistPlaySessionDurably" in persistence
    assert "const queuedWrite = playSessionWriteQueue" in persistence
    assert "writeSequence === playSessionWriteSequence" in persistence
    assert "return false;" in persistence
    assert "const playSessionTabId = randomStorageId(\"page\")" in app
    assert "sessionStorage.setItem(PLAY_SESSION_TAB_STORAGE_KEY" not in app
    assert "await requireDurablePlaySession(" in app
    restore_commit = persistence[persistence.index("if (restored)") :]
    assert "{ claimOwnership: true }," in restore_commit
    assert "only after its full move ledger passes authoritative replay" in restore_commit
    assert "{ capture: true }," in persistence
    assert "replaceExpectedSessionId" in persistence
    assert "replaceExpectedRevision" in persistence
    assert "function storedSessionMatchesReplacementExpectation" in persistence
    assert "const expectedReplacementPredecessor" in persistence
    assert "state.play.sessionId = saved.sessionId" in persistence
    assert "state.play.sessionId = randomStorageId(\"game\")" in app
    assert "if (!await requireDurablePlaySession(replacementOptions)) return" in app
    assert "function markPlaySessionSaveBlocked" in persistence
    assert "if (!state.positionReady)" in app
    assert "void restorePersistedPlaySession()" in app
    assert "Game not saved yet" in play_surface
    assert "Retry saving game" in play_surface
    assert "function requestPlayPrefixJson" in app
    assert "Saved-game validation timed out." in app
    assert "This game changed in another tab." in app
    assert "Game updated in another tab" in play_surface
    assert "const sessionReplaced = saved.sessionId !== state.play.sessionId" in app
    assert "const current = persistedPlaySessionWithoutSideEffects()" in app
    assert "current.ownerId !== saved.ownerId" in app
    assert "current.revision !== saved.revision" in app
    assert "lockPlaySessionForExternalUpdate()" in app
    assert "function blockStalePlayMutation" in app
    board_input = app[
        app.index("function boardInputAllowed") : app.index(
            "function cancelEngineTurn"
        )
    ]
    assert "&& !playSessionExternalUpdate" in board_input
    assert "|| playSessionExternalUpdate" in engine_turn
    assert "await requireDurablePlaySession()" in engine_turn
    browser_client = (STATIC / "browser-engine-client.js").read_text(encoding="utf-8")
    assert "if (remainingSearchMs !== null && remainingSearchMs < 10)" in browser_client
    assert "time_limit: Math.min(" in browser_client
    assert "remainingSearchMs / 1000" in browser_client
    assert "const timeoutMs = remainingMs === null" in browser_client
    assert ": remainingMs;" in browser_client
    assert "Math.min(requestTimeoutMs, remainingMs)" not in browser_client
    assert "Math.max(0.01, remainingSearchMs / 1000)" not in browser_client
    assert "if (!await restorePersistedPlaySession())" in initialize
    assert "await startNewPlayGame({ announce: false })" in initialize


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
    assert "requestPlayPrefixJson(" in engine_turn
    assert engine_turn.index("requestEngineAnalysis(") < engine_turn.index(
        "requestPlayPrefixJson("
    )
    assert engine_turn.index("requestPlayPrefixJson(") < engine_turn.index(
        "applyPrefixPayload(checked"
    )
    assert "deadlineMs: analysisReceiptDeadlineMs" in engine_turn
    assert "analysisReceipt: true" in engine_turn
    assert "? analysis.checked_prefix" in engine_turn
    assert 'analysis.legal_validation_runtime !== "compiled-wasm"' in engine_turn
    assert "const expectedReplyIdentity = hostedFallback?.identity || loadedChampionIdentity()" in engine_turn
    assert "sameHostedEngineIdentity(expectedReplyIdentity, receivedReplyIdentity)" in engine_turn
    assert "analysis.engine_profile_id !== expectedReplyIdentity.engine_profile_id" in engine_turn
    assert "analysis.source_fingerprint !== expectedReplyIdentity.source_fingerprint" in engine_turn
    assert 'state.mode !== "analyze"' in app
    assert 'seriesColor() === state.play.humanColor' in app
    assert "@media (max-width: 560px)" in styles
    assert "@media (max-width: 370px)" in styles


def test_play_ponder_exact_prefix_hit_is_local_and_reuses_authoritative_payloads() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    ponder_run = app[
        app.index("async function runPlayPonder") : app.index(
            "async function startPlayPonder"
        )
    ]
    submit_move = app[
        app.index("async function submitMove") : app.index("function endDrag")
    ]
    handoff = app[
        app.index("async function performSeriesHandoff") : app.index(
            "function advanceSeries"
        )
    ]

    assert "record.predictedHumanSeries.slice(0, index)" in ponder_run
    assert "browserEngineClient.inspectPrefix" in ponder_run
    assert "playPonderPrefixKey(record.humanBoundary, prefix)" in ponder_run
    assert "playPonderPrefixKey(record.childBoundary, [])" in ponder_run
    assert ponder_run.index("playPonderPrefixKey(record.childBoundary, [])") < ponder_run.index(
        "browserEngineClient.analyzeRoot"
    )
    assert "requestJson(" not in ponder_run
    assert "requestRemoteJson(" not in ponder_run

    assert "cachedPlayPonderPrefix(boundaryAtMove, nextPrefix)" in submit_move
    assert "applyCachedPlayPonderPrefix(ponderHit.payload, nextPrefix, nextSan)" in submit_move
    assert submit_move.index("applyCachedPlayPonderPrefix") < submit_move.index(
        "await requireDurablePlaySession"
    )
    assert "rebindPlayPonderRevision(ponderHit.record)" in submit_move
    assert "const record = activePlayPonder || claimedPlayPonder;" in app
    assert "cachedPlayPonderPrefix(plan.nextBoundary, [])" in handoff
    assert "applyCachedPlayPonderPrefix(ponderHit.payload, [], [])" in handoff


def test_play_ponder_deviation_aborts_and_drains_before_normal_prefix_refresh() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    submit_move = app[
        app.index("async function submitMove") : app.index("function endDrag")
    ]
    handoff = app[
        app.index("async function performSeriesHandoff") : app.index(
            "function advanceSeries"
        )
    ]
    cancel = app[
        app.index("function cancelPlayPonder") : app.index(
            "function rebindPlayPonderRevision"
        )
    ]

    assert 'await cancelPlayPonder("human-series-deviation")' in submit_move
    assert submit_move.index('await cancelPlayPonder("human-series-deviation")') < submit_move.index(
        "payload = await refreshPrefix(nextPrefix, nextSan)"
    )
    assert 'await cancelPlayPonder("series-handoff-mismatch")' in handoff
    assert handoff.index('await cancelPlayPonder("series-handoff-mismatch")') < handoff.index(
        "payload = await refreshPrefix([], [])"
    )
    assert "record.controller.abort()" in cancel
    assert "claimedPlayPonder" in cancel
    assert "records.map((record) => Promise.resolve(record.promise).catch(() => null))" in cancel
    assert "playPonderCleanup = Promise.all" in cancel


@pytest.mark.skipif(NODE is None, reason="Node.js is required for ponder cleanup tests")
def test_claimed_play_ponder_is_aborted_and_drained_before_cleanup_resolves() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    cancel = app[
        app.index("function cancelPlayPonder") : app.index(
            "function rebindPlayPonderRevision"
        )
    ]
    script = r"""
let playPonderGeneration = 4;
let activePlayPonder = null;
let claimedPlayPonder = null;
let playPonderCleanup = Promise.resolve();
const state = { mode: "analyze" };
function renderPlaySearchEvidence() { throw new Error("unexpected render"); }
eval(process.argv[1]);

let release;
const controller = new AbortController();
const record = {
  controller,
  promise: new Promise((resolve) => { release = resolve; }),
};
claimedPlayPonder = record;

(async () => {
  let cleanupResolved = false;
  const cleanup = cancelPlayPonder("new-game").then(() => {
    cleanupResolved = true;
  });
  if (!controller.signal.aborted) throw new Error("claimed ponder was not aborted");
  if (claimedPlayPonder !== null) throw new Error("claimed ponder stayed claimable");
  if (playPonderGeneration !== 5) throw new Error("generation was not invalidated");
  await Promise.resolve();
  await Promise.resolve();
  if (cleanupResolved) throw new Error("cleanup resolved before root search drained");
  release({ ok: false });
  await cleanup;
  if (!cleanupResolved) throw new Error("cleanup did not resolve after root search drained");
  process.stdout.write("ok");
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, cancel],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == "ok"


@pytest.mark.skipif(NODE is None, reason="Node.js is required for engine-turn cleanup tests")
def test_cancelled_play_engine_turn_drains_before_a_restart_can_continue() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    cancel = app[
        app.index("function cancelEngineTurn") : app.index(
            "function blockStalePlayMutation"
        )
    ]
    flow = app[
        app.index("async function continuePlayFlow") : app.index(
            "async function undoMove"
        )
    ]
    strength = app[
        app.index("async function selectPlayStrength") : app.index(
            "async function retryEngineTurn"
        )
    ]
    # The source-level ordering is part of the product contract: changing
    # strength cannot launch a replacement until the cancelled local lane has
    # drained, and duplicate flow requests share one in-flight engine turn.
    assert "const activeTurn = activePlayEngineTurn;" in cancel
    assert "Promise.resolve(activeTurn).catch(() => null)" in cancel
    assert "Promise.resolve(ponderCleanup).catch(() => null)" in cancel
    cancel_start = strength.index("const cleanup = cancelEngineTurn();")
    cancel_drain = strength.index("await cleanup;")
    restart = strength.index("await continuePlayFlow();")
    assert cancel_start < cancel_drain < restart
    assert "state.play.sequence !== restartSequence" in strength
    assert "playGameEnded()" in strength
    assert "if (activePlayEngineTurn) return activePlayEngineTurn;" in flow
    assert "activePlayEngineTurn = turn;" in flow
    assert "if (activePlayEngineTurn === turn) activePlayEngineTurn = null;" in flow

    runtime_script = r"""
let release;
let activePlayEngineTurn = new Promise((resolve) => { release = resolve; });
let ponderReleased = false;
const controller = new AbortController();
const state = {
  play: {
    engineAbort: controller,
    sequence: 9,
    thinking: true,
    animating: false,
    activeSearch: {},
    activeSearchRuntime: "browser-wasm",
  },
};
function cancelPlayPonder() {
  return Promise.resolve().then(() => { ponderReleased = true; });
}
""" + cancel + r"""
(async () => {
  let drained = false;
  const cleanup = cancelEngineTurn().then(() => { drained = true; });
  if (!controller.signal.aborted) throw new Error("engine search was not aborted");
  if (state.play.sequence !== 10) throw new Error("engine sequence stayed claimable");
  await Promise.resolve();
  await Promise.resolve();
  if (drained) throw new Error("restart was admitted before the local lane drained");
  if (!ponderReleased) throw new Error("ponder cleanup was not included");
  release();
  await cleanup;
  if (!drained) throw new Error("cancelled engine turn did not drain");
  process.stdout.write("ok");
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [str(NODE), "-e", runtime_script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == "ok"

    single_flight_script = r"""
let activePlayEngineTurn = null;
let release;
let starts = 0;
const state = {
  mode: "play",
  complete: false,
  nextState: null,
  play: { active: true },
};
let playSessionExternalUpdate = false;
let playSessionSaveBlocked = false;
function playReviewActive() { return false; }
function playGameEnded() { return false; }
function advanceSeries() { throw new Error("unexpected handoff"); }
function maybeRunEngineTurn() {
  starts += 1;
  return new Promise((resolve) => { release = resolve; });
}
""" + flow + r"""
(async () => {
  const first = continuePlayFlow();
  const second = continuePlayFlow();
  await Promise.resolve();
  if (starts !== 1) throw new Error(`expected one engine turn, got ${starts}`);
  release();
  await Promise.all([first, second]);
  if (activePlayEngineTurn !== null) throw new Error("settled engine turn stayed active");
  process.stdout.write("ok");
})().catch((error) => { console.error(error); process.exit(1); });
"""
    single_flight = subprocess.run(
        [str(NODE), "-e", single_flight_script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert single_flight.stdout == "ok"

    latest_action_script = r"""
let release;
let activePlayEngineTurn = new Promise((resolve) => { release = resolve; });
const PLAY_STRENGTHS = { strong: {}, faster: {} };
const state = {
  mode: "play",
  play: {
    active: true,
    strength: "strong",
    sequence: 21,
    thinking: true,
    resigned: false,
  },
};
let playSessionExternalUpdate = false;
let playSessionSaveBlocked = false;
let persistCalls = 0;
let renderCalls = 0;
let restartCalls = 0;
function blockStalePlayMutation() { return false; }
function waitForActiveNewPlayGame() { return Promise.resolve(); }
function retryEngineTurn() { throw new Error("unexpected retry"); }
function cancelEngineTurn() {
  state.play.sequence += 1;
  return activePlayEngineTurn;
}
function cancelPlayPonder() { return Promise.resolve(); }
function playReviewActive() { return false; }
function playGameEnded() { return state.play.resigned; }
function persistPlaySession() { persistCalls += 1; }
function renderPlaySurface() { renderCalls += 1; }
function continuePlayFlow() { restartCalls += 1; return Promise.resolve(); }
""" + strength + r"""
(async () => {
  const strengthChange = selectPlayStrength("faster");
  await Promise.resolve();
  if (state.play.sequence !== 22) throw new Error("strength cancellation did not advance sequence");

  // A later Resign/New Game/Analyze cancellation must win while both callers
  // are waiting for the same old local turn to drain.
  state.play.sequence += 1;
  state.play.resigned = true;
  release();
  await strengthChange;

  if (persistCalls !== 0 || renderCalls !== 0 || restartCalls !== 0) {
    throw new Error("an earlier strength waiter restarted after the later action");
  }
  process.stdout.write("ok");
})().catch((error) => { console.error(error); process.exit(1); });
"""
    latest_action = subprocess.run(
        [str(NODE), "-e", latest_action_script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert latest_action.stdout == "ok"


@pytest.mark.skipif(NODE is None, reason="Node.js is required for new-game transition tests")
def test_new_game_transition_is_single_flight_across_a_deferred_drain() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    start = app[
        app.index("async function startNewPlayGame") : app.index(
            "async function performStartNewPlayGame"
        )
    ]
    select_color = app[
        app.index("async function selectPlayColor") : app.index(
            "async function resignPlayGame"
        )
    ]
    resign = app[
        app.index("async function resignPlayGame") : app.index(
            "async function switchWorkspaceMode"
        )
    ]
    switch_mode = app[
        app.index("async function switchWorkspaceMode") : app.index(
            "function legalMovesFrom"
        )
    ]

    assert "if (activeNewPlayGame) return activeNewPlayGame;" in start
    assert "activeNewPlayGame = transition;" in start
    assert "if (activeNewPlayGame === transition)" in start
    assert start.index("activeNewPlayGame = null;") < start.index("renderAll();")
    assert "while (activeNewPlayGame) await activeNewPlayGame;" in start
    assert select_color.index("await waitForActiveNewPlayGame();") < (
        select_color.index("state.play.humanColor = color;")
    )
    assert resign.index("await waitForActiveNewPlayGame();") < resign.index(
        "await cancelEngineTurn();"
    )
    assert switch_mode.index("await waitForActiveNewPlayGame();") < (
        switch_mode.index('if (mode === "analyze")')
    )

    runtime_script = r"""
let activeNewPlayGame = null;
let releaseDrain;
const drain = new Promise((resolve) => { releaseDrain = resolve; });
let starts = 0;
let replacementCommits = 0;
let laterActionApplied = 0;
let renderCalls = 0;
let liveSession = "old-session";
let storedSession = "old-session";
function renderAll() { renderCalls += 1; }
function performStartNewPlayGame() {
  starts += 1;
  return (async () => {
    await drain;
    liveSession = "new-session";
    storedSession = liveSession;
    replacementCommits += 1;
  })();
}
""" + start + r"""
(async () => {
  const first = startNewPlayGame();
  const second = startNewPlayGame();
  const laterAction = (async () => {
    await waitForActiveNewPlayGame();
    laterActionApplied += 1;
  })();
  await Promise.resolve();
  if (starts !== 1) throw new Error(`expected one new-game transition, got ${starts}`);
  if (replacementCommits !== 0) throw new Error("replacement committed before the drain");
  if (laterActionApplied !== 0) throw new Error("later action passed the active new-game transition");
  releaseDrain();
  await Promise.all([first, second, laterAction]);
  if (replacementCommits !== 1) throw new Error(`expected one replacement, got ${replacementCommits}`);
  if (laterActionApplied !== 1) throw new Error("later action did not resume after the new game");
  if (liveSession !== storedSession) throw new Error("live and durable sessions diverged");
  if (activeNewPlayGame !== null) throw new Error("settled new-game transition stayed active");
  if (renderCalls !== 1) throw new Error(`expected one settled-state render, got ${renderCalls}`);
  process.stdout.write("ok");
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [str(NODE), "-e", runtime_script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == "ok"


def test_play_ponder_claim_is_exact_stale_safe_and_visible() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    base_match = app[
        app.index("function playPonderBaseMatches") : app.index(
            "function playPonderPositionMatches"
        )
    ]
    claim = app[
        app.index("async function claimMatchingPlayPonder") : app.index(
            "function safeMovePrefix"
        )
    ]
    evidence = app[
        app.index("function renderPlaySearchEvidence") : app.index(
            "function selectPlayStrength"
        )
    ]

    for exact_binding in (
        "record.generation === playPonderGeneration",
        "record.sessionId === state.play.sessionId",
        "record.sessionRevision === playSessionRevision",
        "record.strength === state.play.strength",
        "record.limitsKey === playPonderLimitsKey(playSearchLimits())",
        "playPonderIdentityMatches(record)",
    ):
        assert exact_binding in base_match
    assert "boundaryKey(state.boundary) === record.childBoundaryKey" in claim
    assert "playPositionKey() === record.claimPlayKey" in claim
    assert "record.analysisStarted" in claim
    assert 'await cancelPlayPonder("ponder-claim-mismatch")' in claim
    assert "Thinking ahead locally · WASM" in evidence
    assert "Reply prepared locally · WASM" in evidence
    assert "any different move safely discards it" in evidence

    for invalidation in (
        'cancelPlayPonder("play-session-restore")',
        'cancelPlayPonder("play-history-review")',
        'cancelPlayPonder("play-strength-changed")',
        'cancelPlayPonder("engine-turn-cancelled")',
    ):
        assert invalidation in app
    assert app.count("await playPonderCleanup.catch(() => null)") >= 3


def test_play_ponder_accepts_the_last_certified_completed_depth() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    prediction = app[
        app.index("function certifiedLocalPonderPrediction") : app.index(
            "function assertLivePlayPonder"
        )
    ]

    assert "requestedDepth !== search.depth" in prediction
    assert "completedDepth < 1" in prediction
    assert "completedDepth > requestedDepth" in prediction
    assert "completedDepth < requestedDepth" not in prediction
    assert "asBoolean(analysis?.timed_out)" not in prediction
    assert "asBoolean(analysis?.work_limit_reached)" not in prediction
    assert "root_bound_coverage_complete" in prediction


def test_cancelled_pre_search_handoff_cannot_dispatch_stale_strength_limits() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    engine_turn = app[
        app.index("async function maybeRunEngineTurn") : app.index(
            "async function startNewPlayGame"
        )
    ]

    entry = engine_turn.index("const entrySequence = state.play.sequence;")
    limits = engine_turn.index("const search = playSearchLimits();")
    claim = engine_turn.index("const ponder = await claimMatchingPlayPonder(search);")
    stale_gate = engine_turn.index("state.play.sequence !== entrySequence")
    dispatch = engine_turn.index("requestEngineAnalysis(")
    assert entry < limits < claim < stale_gate < dispatch


def test_play_ponder_background_errors_are_silent_and_engine_turn_falls_back() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    start = app[
        app.index("async function startPlayPonder") : app.index(
            "async function claimMatchingPlayPonder"
        )
    ]
    engine_turn = app[
        app.index("async function maybeRunEngineTurn") : app.index(
            "async function startNewPlayGame"
        )
    ]

    assert "(error) => ({ ok: false, error })" in start
    assert "if (!settled.ok && activePlayPonder === record) activePlayPonder = null" in start
    assert "Pondering is an optional local optimization and never interrupts play" in start
    assert "state.play.error" not in start
    assert "if (settled?.ok)" in engine_turn
    assert engine_turn.count("requestEngineAnalysis(") >= 2
    assert "retrySearchDeadlineMs" in engine_turn
    assert "retryReceiptDeadlineMs" in engine_turn
    assert engine_turn.count(
        "if (ponder && claimedPlayPonder === ponder) claimedPlayPonder = null;"
    ) == 2


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
