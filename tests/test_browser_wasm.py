from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import chess
import pytest

from scottish_progressive.webapp import APIError, inspect_prefix, state_from_payload


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "scottish_progressive" / "web" / "static"
NODE = shutil.which("node")


def _load_bundle_builder():
    path = ROOT / "scripts" / "build_browser_wasm_bundle.py"
    spec = importlib.util.spec_from_file_location("build_browser_wasm_bundle", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _certificate(
    builder,
    *,
    source_package: Path,
    wasm: Path,
    module_js: Path,
) -> dict[str, object]:
    return {
        "schema": builder.CERTIFICATE_SCHEMA,
        "status": "certified",
        "safety_certified": True,
        "contract_version": 1,
        "abi_version": 1,
        "certificate_id": "gate-20260822-single",
        "source_fingerprint": builder.engine_source_fingerprint(source_package),
        "wasm_sha256": builder.sha256_file(wasm),
        "module_js_sha256": builder.sha256_file(module_js),
        "runtime_variant": "single",
        "thread_count": 1,
        "support_files": [],
        "memory": {
            "initial_bytes": 16 * 1024 * 1024,
            "maximum_bytes": 128 * 1024 * 1024,
            "estimated_peak_bytes": 96 * 1024 * 1024,
            "growth_enabled": True,
        },
        "evidence": {
            "failures": 0,
            "differential_cases": 256,
            "start_position_parity": True,
            "s4_mate_safety": True,
            "interrupted_depth_publication": True,
            "compiled_legal_series_validation": True,
            "compiled_authoritative_replay": True,
            "start_w32_d5_completed_depth": 5,
            "start_w32_d5_width": 32,
            "start_w32_d5_elapsed_seconds": 42.5,
        },
        "engine": {
            "engine_profile_id": "spc-browser-test",
            "engine_profile_name": "Browser test champion",
            "engine_version": "test-engine-v1",
            "ruleset_version": "test-rules-v1",
            "analysis_limits": {
                "maximum_depth": 8,
                "maximum_max_series": 64,
                "maximum_seconds": 60,
                "maximum_generation_positions": 25_000_000,
                "default_depth": 5,
                "default_max_series": 32,
                "default_seconds": 45,
                "default_generation_positions": 20_000_000,
            },
        },
    }


def test_bundle_builder_stages_only_a_certified_identity_bound_single_lane(
    tmp_path: Path,
) -> None:
    builder = _load_bundle_builder()
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.cpp").write_text("int engine = 1;\n", encoding="utf-8")
    wasm = tmp_path / "kernel.wasm"
    module_js = tmp_path / "kernel.mjs"
    certificate_path = tmp_path / "certificate.json"
    wasm.write_bytes(b"\0asm\x01\0\0\0")
    module_js.write_text("export default async () => ({});\n", encoding="utf-8")
    certificate_path.write_text(
        json.dumps(
            _certificate(
                builder,
                source_package=package,
                wasm=wasm,
                module_js=module_js,
            )
        ),
        encoding="utf-8",
    )
    output = tmp_path / "engine"

    manifest = builder.build_bundle(
        single_wasm=wasm,
        single_module_js=module_js,
        single_certificate_path=certificate_path,
        source_package=package,
        output=output,
    )

    assert set(manifest["variants"]) == {"single"}
    assert manifest["variants"]["single"]["thread_count"] == 1
    assert (
        manifest["variants"]["single"]["safety_certificate"]["engine"]
        ["analysis_limits"]["default_depth"]
        == 5
    )
    assert (output / "single" / "spc-engine.wasm").read_bytes() == wasm.read_bytes()
    assert (output / "browser-engine-manifest.json").is_file()


def test_bundle_builder_rejects_a_depth_five_receipt_at_the_sixty_second_gate(
    tmp_path: Path,
) -> None:
    builder = _load_bundle_builder()
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.cpp").write_text("int engine = 1;\n", encoding="utf-8")
    wasm = tmp_path / "kernel.wasm"
    module_js = tmp_path / "kernel.mjs"
    certificate_path = tmp_path / "certificate.json"
    wasm.write_bytes(b"\0asm\x01\0\0\0")
    module_js.write_text("export default async () => ({});\n", encoding="utf-8")
    certificate = _certificate(
        builder,
        source_package=package,
        wasm=wasm,
        module_js=module_js,
    )
    certificate["evidence"]["start_w32_d5_elapsed_seconds"] = 60
    certificate_path.write_text(json.dumps(certificate), encoding="utf-8")
    output = tmp_path / "engine"

    with pytest.raises(ValueError, match="under-60-second W32 D5"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_certificate_path=certificate_path,
            source_package=package,
            output=output,
        )

    assert not output.exists()


def test_bundle_builder_rejects_excessive_memory_and_pthread_publication(
    tmp_path: Path,
) -> None:
    builder = _load_bundle_builder()
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.cpp").write_text("int engine = 1;\n", encoding="utf-8")
    wasm = tmp_path / "kernel.wasm"
    module_js = tmp_path / "kernel.mjs"
    certificate_path = tmp_path / "certificate.json"
    wasm.write_bytes(b"\0asm\x01\0\0\0")
    module_js.write_text("export default async () => ({});\n", encoding="utf-8")
    certificate = _certificate(
        builder,
        source_package=package,
        wasm=wasm,
        module_js=module_js,
    )
    certificate["memory"]["maximum_bytes"] = builder.MAXIMUM_MEMORY_BYTES + 65_536
    certificate_path.write_text(json.dumps(certificate), encoding="utf-8")

    with pytest.raises(ValueError, match="maximum_bytes"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_certificate_path=certificate_path,
            source_package=package,
            output=tmp_path / "oversized",
        )

    certificate["memory"]["maximum_bytes"] = 128 * 1024 * 1024
    certificate_path.write_text(json.dumps(certificate), encoding="utf-8")
    with pytest.raises(ValueError, match="pthread publishing is disabled"):
        builder.build_bundle(
            single_wasm=wasm,
            single_module_js=module_js,
            single_certificate_path=certificate_path,
            pthread_wasm=wasm,
            pthread_module_js=module_js,
            pthread_certificate_path=certificate_path,
            source_package=package,
            output=tmp_path / "pthread",
        )


def test_existing_bundle_validator_rejects_artifact_drift(tmp_path: Path) -> None:
    builder = _load_bundle_builder()
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.cpp").write_text("int engine = 1;\n", encoding="utf-8")
    wasm = tmp_path / "kernel.wasm"
    module_js = tmp_path / "kernel.mjs"
    certificate_path = tmp_path / "certificate.json"
    wasm.write_bytes(b"\0asm\x01\0\0\0")
    module_js.write_text("export default async () => ({});\n", encoding="utf-8")
    certificate_path.write_text(
        json.dumps(
            _certificate(
                builder,
                source_package=package,
                wasm=wasm,
                module_js=module_js,
            )
        ),
        encoding="utf-8",
    )
    output = tmp_path / "engine"
    builder.build_bundle(
        single_wasm=wasm,
        single_module_js=module_js,
        single_certificate_path=certificate_path,
        source_package=package,
        output=output,
    )
    builder.validate_existing_bundle(output, package)

    (output / "single" / "spc-engine.js").write_text(
        "export default async () => ({ changed: true });\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        builder.validate_existing_bundle(output, package)


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser contract tests")
def test_browser_client_accepts_certified_completed_depth_and_rejects_fake_legality() -> None:
    script = r"""
const api = require(process.argv[1]);
const source = "a".repeat(16);
const artifact = "b".repeat(64);
const moduleHash = "c".repeat(64);
const start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const after = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1";
const identity = {
  ready: true,
  source_fingerprint: source,
  wasm_sha256: artifact,
  module_js_sha256: moduleHash,
  certificate_schema: "spc-browser-wasm-certificate-v1",
  certificate_status: "certified",
  contract_version: 1,
  abi_version: 1,
  safety_certified: true,
  certificate_id: "cert-1",
  runtime_variant: "single",
  thread_count: 1,
  engine_profile_id: "spc-test",
  engine_profile_name: "Test champion",
  engine_version: "engine-v1",
  ruleset_version: "rules-v1",
  analysis_limits: {
    maximum_depth: 8,
    maximum_max_series: 64,
    maximum_seconds: 60,
    maximum_generation_positions: 25000000,
    default_depth: 5,
    default_max_series: 32,
    default_seconds: 45,
    default_generation_positions: 20000000,
  },
  memory_limits: {
    initial_bytes: 16777216,
    maximum_bytes: 134217728,
    estimated_peak_bytes: 100663296,
    growth_enabled: true,
  },
};
const requestPayload = {
  fen: start,
  series: 1,
  quiet_series: 0,
  ep_targets: [],
  promoted_hex: "0",
  chess960: false,
  prefix: [],
  depth: 5,
  max_series: 32,
  time_limit: 45,
  max_generation_positions: 20000000,
  alternatives: 0,
  best_move_only: true,
  rate_move: false,
  save: false,
};

function resultFor(request) {
  return {
    ok: true,
    publishable: true,
    safety_certified: true,
    legal_series_certified: true,
    authoritative_replay_certified: true,
    legal_validation_runtime: "compiled-wasm",
    source_fingerprint: source,
    wasm_sha256: artifact,
    module_js_sha256: moduleHash,
    certificate_id: "cert-1",
    runtime_variant: "single",
    thread_count: 1,
    requested_depth: 5,
    completed_depth: 4,
    best_full_series: ["e2e4"],
    score: 12,
    work: 123456,
    memory_bytes: 16777216,
    stats: { generation_positions: 123456 },
    checked_prefix: {
      boundary_state: {
        fen: start,
        series: 1,
        quiet_series: 0,
        ep_targets: [],
        promoted_hex: "0000000000000000",
        chess960: false,
      },
      prefix: ["e2e4"],
      san: ["e4"],
      frames: [{ index: 1, uci: "e2e4", san: "e4", board_fen: after }],
      complete: true,
      board_fen: after,
      outcome: null,
      next_state: {
        fen: after,
        series: 2,
        quiet_series: 0,
        ep_targets: ["e3"],
        promoted_hex: "0000000000000000",
        chess960: false,
      },
    },
  };
}

class FakeWorker {
  constructor() {
    this.listeners = new Map();
    this.terminated = false;
  }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
  postMessage(message) {
    const payload = message.type === "probe" ? identity : resultFor(message.payload);
    queueMicrotask(() => {
      for (const listener of this.listeners.get("message") || []) {
        listener({ data: { id: message.id, ok: true, payload } });
      }
    });
  }
  terminate() { this.terminated = true; }
}

(async () => {
  const worker = new FakeWorker();
  const client = api.createClient({ workerFactory: () => worker });
  const ready = await client.preflight({});
  if (!ready.ready || ready.source_fingerprint !== source) throw new Error("local preflight failed");
  const result = await client.analyze(requestPayload);
  if (result.requested_depth !== 5 || result.completed_depth !== 4) throw new Error("depth receipt drifted");
  if (result.runtime_receipt.completed_depth !== 4) throw new Error("receipt inflated depth");
  if (result.runtime_receipt.artifact_fingerprint !== artifact) throw new Error("artifact receipt missing");
  if (result.runtime_receipt.thread_count !== 1) throw new Error("thread receipt missing");
  const request = api.normalizedKernelRequest(requestPayload, "unsafe-check");
  const unsafe = { ...resultFor(request), legal_series_certified: false };
  let rejected = false;
  try {
    api.validatePublishedAnalysis(unsafe, request, identity);
  } catch (error) {
    rejected = error.code === "browser-legality-unverified";
  }
  if (!rejected) throw new Error("uncertified legality was published");
  process.stdout.write(JSON.stringify({ ready, receipt: result.runtime_receipt }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "browser-engine-client.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["ready"]["runtime_variant"] == "single"
    assert payload["receipt"]["requested_depth"] == 5
    assert payload["receipt"]["completed_depth"] == 4
    assert payload["receipt"]["work"] == 123_456


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser contract tests")
def test_browser_client_cancellation_terminates_the_synchronous_worker() -> None:
    script = r"""
const api = require(process.argv[1]);
const identity = {
  ready: true,
  source_fingerprint: "a".repeat(16),
  wasm_sha256: "b".repeat(64),
  module_js_sha256: "c".repeat(64),
  certificate_schema: "spc-browser-wasm-certificate-v1",
  certificate_status: "certified",
  contract_version: 1,
  abi_version: 1,
  safety_certified: true,
  certificate_id: "cert-1",
  runtime_variant: "single",
  thread_count: 1,
  engine_profile_id: "spc-test",
  engine_profile_name: "Test champion",
  engine_version: "engine-v1",
  ruleset_version: "rules-v1",
  analysis_limits: {
    maximum_depth: 8,
    maximum_max_series: 64,
    maximum_seconds: 60,
    maximum_generation_positions: 25000000,
    default_depth: 5,
    default_max_series: 32,
    default_seconds: 30,
    default_generation_positions: 10000000,
  },
  memory_limits: {
    initial_bytes: 16777216,
    maximum_bytes: 134217728,
    estimated_peak_bytes: 100663296,
    growth_enabled: true,
  },
};
class BlockingWorker {
  constructor() { this.listeners = new Map(); this.terminated = false; }
  addEventListener(type, listener) {
    const values = this.listeners.get(type) || [];
    values.push(listener);
    this.listeners.set(type, values);
  }
  postMessage(message) {
    if (message.type !== "probe") return;
    queueMicrotask(() => {
      for (const listener of this.listeners.get("message") || []) {
        listener({ data: { id: message.id, ok: true, payload: identity } });
      }
    });
  }
  terminate() { this.terminated = true; }
}
(async () => {
  const worker = new BlockingWorker();
  const client = api.createClient({ workerFactory: () => worker });
  await client.preflight({});
  const controller = new AbortController();
  const pending = client.analyze({
    fen: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    series: 1, quiet_series: 0, ep_targets: [], promoted_hex: "0", chess960: false,
    prefix: [], depth: 5, max_series: 32, time_limit: 30,
    max_generation_positions: 10000000, alternatives: 0,
    best_move_only: true, rate_move: false, save: false,
  }, { signal: controller.signal });
  controller.abort();
  let name = null;
  try { await pending; } catch (error) { name = error.name; }
  if (name !== "AbortError") throw new Error(`unexpected cancellation ${name}`);
  if (!worker.terminated) throw new Error("worker survived cancellation");
  if (client.ready !== false) throw new Error("cancelled worker remained ready");
  process.stdout.write(JSON.stringify({ name, terminated: worker.terminated }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "browser-engine-client.js")],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {"name": "AbortError", "terminated": True}


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser contract tests")
def test_browser_client_rejects_uncertified_limits_and_incomplete_replay() -> None:
    script = r"""
const api = require(process.argv[1]);
const source = "a".repeat(16);
const artifact = "b".repeat(64);
const moduleHash = "c".repeat(64);
const start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const after = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1";
const limits = {
  maximum_depth: 8, maximum_max_series: 64, maximum_seconds: 60,
  maximum_generation_positions: 25000000, default_depth: 5,
  default_max_series: 32, default_seconds: 30,
  default_generation_positions: 10000000,
};
const identity = {
  source_fingerprint: source, wasm_sha256: artifact, module_js_sha256: moduleHash,
  certificate_schema: "spc-browser-wasm-certificate-v1",
  certificate_status: "certified", contract_version: 1, abi_version: 1,
  safety_certified: true, certificate_id: "cert-1", runtime_variant: "single",
  thread_count: 1, engine_profile_id: "spc-test", engine_profile_name: "Test",
  engine_version: "engine-v1", ruleset_version: "rules-v1",
  analysis_limits: limits,
  memory_limits: {
    initial_bytes: 16777216, maximum_bytes: 134217728,
    estimated_peak_bytes: 100663296, growth_enabled: true,
  },
};
const base = {
  fen: start, series: 1, quiet_series: 0, ep_targets: [],
  promoted_hex: "0", chess960: false, prefix: [], depth: 5,
  max_series: 32, time_limit: 30, max_generation_positions: 10000000,
  alternatives: 0, best_move_only: true, rate_move: false, save: false,
};
const outside = {
  ...base, depth: 64, max_series: 4096, time_limit: 1000,
  max_generation_positions: 4000000000,
};
if (api.isLocalBestMoveRequest(outside, limits)) {
  throw new Error("request outside certificate was selectable");
}
const request = api.normalizedKernelRequest(base, "replay-check", limits);
const common = {
  ok: true, publishable: true, safety_certified: true,
  legal_series_certified: true, authoritative_replay_certified: true,
  legal_validation_runtime: "compiled-wasm", source_fingerprint: source,
  wasm_sha256: artifact, module_js_sha256: moduleHash, certificate_id: "cert-1",
  runtime_variant: "single", thread_count: 1, memory_bytes: 16777216,
  requested_depth: 5, completed_depth: 4, best_full_series: ["e2e4"], stats: {},
};
const replay = {
  boundary_state: {
    fen: start, series: 1, quiet_series: 0, ep_targets: [],
    promoted_hex: "0000000000000000", chess960: false,
  },
  prefix: ["e2e4"], san: ["e4"],
  frames: [{ index: 1, uci: "e2e4", san: "e4", board_fen: after }],
  complete: true, board_fen: after, outcome: null,
  next_state: { fen: after, series: 2, promoted_hex: "0000000000000000" },
};
let missingStateRejected = false;
try {
  api.validatePublishedAnalysis({ ...common, checked_prefix: replay }, request, identity);
} catch (error) {
  missingStateRejected = error.code === "browser-replay-invalid";
}
if (!missingStateRejected) throw new Error("incomplete next state was accepted");
const completeNext = {
  fen: after, series: 2, quiet_series: 0, ep_targets: ["e3"],
  promoted_hex: "0000000000000000", chess960: false,
};
let finalFrameRejected = false;
try {
  api.validatePublishedAnalysis({
    ...common,
    checked_prefix: {
      ...replay,
      frames: [{ index: 1, uci: "e2e4", san: "e4", board_fen: start }],
      next_state: completeNext,
    },
  }, request, identity);
} catch (error) {
  finalFrameRejected = error.code === "browser-replay-invalid";
}
if (!finalFrameRejected) throw new Error("mismatched final frame was accepted");
process.stdout.write(JSON.stringify({ missingStateRejected, finalFrameRejected }));
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "browser-engine-client.js")],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "missingStateRejected": True,
        "finalFrameRejected": True,
    }


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser contract tests")
def test_browser_client_reprobes_after_an_unexpected_worker_crash() -> None:
    script = r"""
const api = require(process.argv[1]);
const source = "a".repeat(16), artifact = "b".repeat(64), moduleHash = "c".repeat(64);
const start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const after = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1";
const identity = {
  ready: true, source_fingerprint: source, wasm_sha256: artifact,
  module_js_sha256: moduleHash,
  certificate_schema: "spc-browser-wasm-certificate-v1",
  certificate_status: "certified", contract_version: 1, abi_version: 1,
  safety_certified: true, certificate_id: "cert-1",
  runtime_variant: "single", thread_count: 1, engine_profile_id: "spc-test",
  engine_profile_name: "Test", engine_version: "engine-v1", ruleset_version: "rules-v1",
  analysis_limits: {
    maximum_depth: 8, maximum_max_series: 64, maximum_seconds: 60,
    maximum_generation_positions: 25000000, default_depth: 5,
    default_max_series: 32, default_seconds: 30,
    default_generation_positions: 10000000,
  },
  memory_limits: {
    initial_bytes: 16777216, maximum_bytes: 134217728,
    estimated_peak_bytes: 100663296, growth_enabled: true,
  },
};
function resultFor(request) {
  return {
    ok: true, publishable: true, safety_certified: true,
    legal_series_certified: true, authoritative_replay_certified: true,
    legal_validation_runtime: "compiled-wasm", source_fingerprint: source,
    wasm_sha256: artifact, module_js_sha256: moduleHash, certificate_id: "cert-1",
    runtime_variant: "single", thread_count: 1, memory_bytes: 16777216,
    requested_depth: 5, completed_depth: 4, best_full_series: ["e2e4"], stats: {},
    checked_prefix: {
      boundary_state: {
        fen: start, series: 1, quiet_series: 0, ep_targets: [],
        promoted_hex: "0000000000000000", chess960: false,
      },
      prefix: ["e2e4"], san: ["e4"],
      frames: [{ index: 1, uci: "e2e4", san: "e4", board_fen: after }],
      complete: true, board_fen: after, outcome: null,
      next_state: {
        fen: after, series: 2, quiet_series: 0, ep_targets: ["e3"],
        promoted_hex: "0000000000000000", chess960: false,
      },
    },
  };
}
class WorkerDouble {
  constructor() { this.listeners = new Map(); this.messages = []; this.terminated = false; }
  addEventListener(type, listener) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
  }
  emit(type, event) { for (const listener of this.listeners.get(type) || []) listener(event); }
  postMessage(message) {
    this.messages.push(message.type);
    const payload = message.type === "probe" ? identity : resultFor(message.payload);
    queueMicrotask(() => this.emit("message", { data: { id: message.id, ok: true, payload } }));
  }
  terminate() { this.terminated = true; }
}
(async () => {
  const workers = [];
  const client = api.createClient({ workerFactory: () => {
    const worker = new WorkerDouble(); workers.push(worker); return worker;
  } });
  await client.preflight({});
  workers[0].emit("error", { error: new Error("boom") });
  if (client.ready !== false) throw new Error("crashed worker remained ready");
  await client.analyze({
    fen: start, series: 1, quiet_series: 0, ep_targets: [], promoted_hex: "0",
    chess960: false, prefix: [], depth: 5, max_series: 32, time_limit: 30,
    max_generation_positions: 10000000, alternatives: 0, best_move_only: true,
    rate_move: false, save: false,
  });
  if (workers.length !== 2) throw new Error(`expected replacement worker, got ${workers.length}`);
  if (workers[1].messages.join(",") !== "probe,analyze") {
    throw new Error(`replacement was not reprobed: ${workers[1].messages}`);
  }
  process.stdout.write(JSON.stringify({ ready: client.ready, messages: workers[1].messages }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "browser-engine-client.js")],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "ready": True,
        "messages": ["probe", "analyze"],
    }


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser contract tests")
def test_browser_client_deadline_terminates_without_starting_a_fallback_search() -> None:
    script = r"""
const api = require(process.argv[1]);
const identity = {
  ready: true,
  certificate_schema: "spc-browser-wasm-certificate-v1",
  certificate_status: "certified",
  contract_version: 1,
  abi_version: 1,
  source_fingerprint: "a".repeat(16),
  wasm_sha256: "b".repeat(64),
  module_js_sha256: "c".repeat(64),
  safety_certified: true,
  certificate_id: "cert-1",
  runtime_variant: "single",
  thread_count: 1,
  engine_profile_id: "spc-test",
  engine_profile_name: "Test",
  engine_version: "engine-v1",
  ruleset_version: "rules-v1",
  analysis_limits: {
    maximum_depth: 8, maximum_max_series: 64, maximum_seconds: 60,
    maximum_generation_positions: 25000000, default_depth: 5,
    default_max_series: 32, default_seconds: 30,
    default_generation_positions: 10000000,
  },
  memory_limits: {
    initial_bytes: 16777216, maximum_bytes: 134217728,
    estimated_peak_bytes: 100663296, growth_enabled: true,
  },
};
class BlockingWorker {
  constructor() { this.listeners = new Map(); this.terminated = false; this.analyzeCalls = 0; }
  addEventListener(type, listener) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
  }
  postMessage(message) {
    if (message.type === "analyze") { this.analyzeCalls += 1; return; }
    queueMicrotask(() => {
      for (const listener of this.listeners.get("message") || []) {
        listener({ data: { id: message.id, ok: true, payload: identity } });
      }
    });
  }
  terminate() { this.terminated = true; }
}
(async () => {
  const worker = new BlockingWorker();
  const client = api.createClient({ workerFactory: () => worker });
  await client.preflight({});
  let code = null;
  try {
    await client.analyze({
      fen: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
      series: 1, quiet_series: 0, ep_targets: [], promoted_hex: "0",
      chess960: false, prefix: [], depth: 5, max_series: 32, time_limit: 30,
      max_generation_positions: 10000000, alternatives: 0,
      best_move_only: true, rate_move: false, save: false,
    }, { deadlineMs: performance.now() + 100 });
  } catch (error) {
    code = error.code;
  }
  if (code !== "browser-analysis-deadline") throw new Error(`unexpected deadline ${code}`);
  if (!worker.terminated || client.ready !== false) throw new Error("deadline left worker ready");
  if (worker.analyzeCalls !== 1) throw new Error("deadline started another search");
  process.stdout.write(JSON.stringify({ code, terminated: worker.terminated, calls: worker.analyzeCalls }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "browser-engine-client.js")],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "code": "browser-analysis-deadline",
        "terminated": True,
        "calls": 1,
    }


def test_web_boundary_contract_round_trips_promoted_provenance() -> None:
    fen = "7k/8/8/8/8/8/Q7/7K w - - 0 1"
    promoted = f"{chess.BB_A2:016x}"
    state = state_from_payload(
        {
            "fen": fen,
            "series": 1,
            "quiet_series": 0,
            "ep_targets": [],
            "promoted_hex": promoted,
            "chess960": False,
        }
    )

    boundary = inspect_prefix(state, ())["boundary_state"]

    assert state.board.promoted == chess.BB_A2
    assert boundary["promoted_hex"] == promoted
    assert boundary["chess960"] is False


def test_web_boundary_contract_rejects_promoted_pawns() -> None:
    with pytest.raises(APIError, match="occupied non-pawn"):
        state_from_payload(
            {
                "fen": chess.STARTING_FEN,
                "series": 1,
                "quiet_series": 0,
                "ep_targets": [],
                "promoted_hex": f"{chess.BB_A2:016x}",
                "chess960": False,
            }
        )
