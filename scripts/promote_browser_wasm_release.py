from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import chess  # noqa: E402
from benchmarks.build_root_session_wasm import (  # noqa: E402
    EXPORTED_FUNCTIONS,
    SOURCES as KERNEL_SOURCES,
)
from benchmarks.check_wasm_dependency_closure import REQUIRED as CLOSURE_SOURCES  # noqa: E402
from scottish_progressive.model import ProgressiveState  # noqa: E402
from scottish_progressive.rules import SeriesLegalityError, play_series  # noqa: E402
from scripts import build_browser_wasm_bundle as bundle_builder  # noqa: E402


RELEASE_SCHEMA = "spc-browser-wasm-release-promotion-v2"
BUILD_SCHEMA = "spc-root-session-build-receipt-v1"
ROOT_SMOKE_SCHEMA = "spc-root-session-wasm-smoke-v1"
ROOT_PARITY_SCHEMA = "spc-root-d5-oracle-v1"
PREFIX_PARITY_SCHEMA = "spc-prefix-parity-receipt-v2"
BROWSER_PREFIX_SCHEMA = "spc-browser-prefix-contract-receipt-v1"
MATE_PARITY_SCHEMA = "spc-mate-wasm-receipt-v3"
OPERA_CDP_SCHEMA = "spc-opera-root-session-cdp-receipt-v1"
OPERA_WORKER_SCHEMA = "spc-opera-root-d5-benchmark-v2"
OPERA_CHECKED_HORIZON_SCHEMA = "spc-opera-checked-pv-horizon-receipt-v6"
OPERA_CHECKED_HORIZON_FILENAME = "opera-checked-pv-horizon-receipt.json"
CANDIDATE_SCHEMA = "spc-browser-wasm-release-candidate-v1"
CHECKED_HORIZON_EVIDENCE_SCHEMA = "spc-checked-horizon-wasm-evidence-v1"
CHECKED_PV_SELECTION_POLICY = (
    "repair-once-then-veto-adverse-selected-pv-boundary-mates-v2"
)
SAME_ROOT_REPAIR_POLICY_SCHEMA = "spc-same-root-horizon-repair-policy-v1"
PV_HORIZON_POLICY_VETO_SCHEMA = "spc-pv-horizon-candidate-veto-v1"
THRESHOLD_VETO_WITNESS_SCHEMA = "spc-opera-same-root-repair-limit-witness-v1"
SELECTED_D5_HORIZON_CERTIFICATION_SCHEMA = (
    "spc-opera-selected-d5-boundary-ladder-certification-v2"
)
BOUNDARY_LADDER_PROBE_SCHEMA = "spc-opera-selected-pv-boundary-probe-v1"
FOUND_STOP_WITNESS_SCHEMA = "spc-opera-selected-pv-found-stop-witness-v1"
UNKNOWN_FAIL_CLOSED_WITNESS_SCHEMA = (
    "spc-opera-selected-pv-unknown-fail-closed-witness-v1"
)
UNKNOWN_FAIL_CLOSED_EVIDENCE_SCHEMA = (
    "spc-opera-selected-pv-unknown-fail-closed-evidence-v1"
)
UNKNOWN_CREDIT_CONSTRAINT_SCHEMA = "spc-opera-boundary-probe-credit-constraint-v1"
BOUNDARY_LADDER_ORDER = "leaf-first-odd-rooted-prefix"
RAW_TRACE_ATTESTATION_SCHEMA = "spc-opera-checked-pv-raw-trace-attestation-v1"
SELECTED_D5_FIXTURE_ID = "b3-known-adverse-series5-2026-08-26-v1"
MAXIMUM_SUCCESSFUL_SAME_ROOT_REPAIRS = 1
CERTIFIED_SAFETY_RESERVE_POSITIONS = 4_000_000
PV_HORIZON_MATE_WORK_LIMIT = 3_500_000
WHITE_HORIZON_CANDIDATE_SHA256 = (
    "d050d64ee2388a82969a0953fdc1aa937455951d762ec9b7d16c3f9fee7b5c94"
)
WHITE_HORIZON_PROOF_SET_SHA256 = (
    "5b7dda6a22771961e77b0bcd107f7cb14a04390886199c1e955148aa12b455bb"
)
WHITE_HORIZON_ROOT_PV_SHA256 = (
    "2b391d9f78869648bbb89bf23ce4233f16ccb57ac930d0c724815226008743d4"
)
BLACK_HORIZON_CANDIDATE_SHA256 = (
    "0dabf1be2fd78c5065515628fe556d9b750f3623e9ba583c13984066f5cbe2a9"
)
BLACK_HORIZON_PROOF_SET_SHA256 = (
    "9dd5bade7dafab271411fe3bddfd0e8fd86c35daea56fc11629f4aae5b17c961"
)
HEX_64 = re.compile(r"[0-9a-f]{64}")
GIT_REVISION = re.compile(r"[0-9a-f]{40,64}")
ASPIRATION_INITIAL_DELTA = 2_048
MAX_ASPIRATION_ATTEMPTS = 4
ASPIRATION_COUNTER_FIELDS = (
    "attempts",
    "fail_highs",
    "fail_lows",
    "exact_hits",
    "full_window_fallbacks",
)
ARTIFACT_IDENTITY_FIELDS = (
    "source_revision",
    "source_fingerprint",
    "kernel_sha256",
    "wasm_sha256",
    "module_js_sha256",
    "artifact_set_sha256",
)
RUNTIME_IDENTITY_FIELDS = (
    "exception_strategy",
    "wasm_simd",
    "allocator",
)
RECEIPT_FILENAMES = {
    "build": "root-session-build-receipt.json",
    "root_smoke": "root-session-smoke-receipt.json",
    "root_parity": "root-session-parity-receipt.json",
    "prefix_parity": "prefix-parity-receipt.json",
    "browser_prefix": "browser-prefix-receipt.json",
    "mate_parity": "mate-parity-receipt.json",
    "opera": "opera-d1-d5-receipt.json",
}

OPERA_CHECKED_HORIZON_CHECKS = frozenset(
    {
        "authenticity_scope_is_local_checkout",
        "standalone_signature_not_claimed",
        "local_origin_is_loopback",
        "evaluated_page_is_opera",
        "local_assets_are_sha256_bound",
        "native_worker_factory_is_bound",
        "no_synthetic_worker_events",
        "manifest_preflight_identity_bound",
        "result_identity_bound",
        "local_wasm_preflight",
        "completed_depth_5",
        "publishable",
        "selected_safety_certified",
        "compiled_replay_is_authoritative",
        "policy_is_explicit",
        "repair_once_then_veto_policy_bound",
        "f3_exact_raw_mate_and_same_root_repair",
        "f3_repair_warm_recertified",
        "f3_second_distinct_proof_vetoed_without_research",
        "f3_newest_request_order_proof_hit",
        "selected_b3_after_f3_policy_veto",
        "selected_b3_full_d5_pv_bound_to_compiled_trace",
        "selected_b3_known_adverse_series5_horizon_absent",
        "selected_b3_ordered_boundary_ladder_certified",
        "selected_b3_boundary_ladder_authoritative_replay",
        "selected_b3_boundary_ladder_work_conserved",
        "f3_found_stops_boundary_ladder",
        "unknown_fail_closed_observed",
        "global_work_respected",
        "no_interruption",
        "deadline_respected",
    }
)

CHECKED_HORIZON_STATIC_ASSETS = {
    "browser_engine_worker": "browser-engine-worker.js",
    "browser_engine_client": "browser-engine-client.js",
    "browser_prefix_contract": "browser-prefix-contract.js",
    "browser_root_iteration_client": "browser-root-iteration-client.js",
    "root_iteration_coordinator": "root-iteration-coordinator.js",
    "wasm_kernel_adapter": "wasm-kernel-adapter.js",
}

SAFE_RESELECTION_RUNTIME_ASSETS = {
    "page_document": "index.html",
    "page_styles": "styles.css",
    "study_safety": "study-safety.js",
    "evaluation_format": "evaluation-format.js",
    "play_handoff": "play-handoff.js",
    "play_timeline": "play-timeline.js",
    **CHECKED_HORIZON_STATIC_ASSETS,
    "board_renderer": "board-renderer.js",
    "page_application": "app.js",
}


class ReleaseGateError(ValueError):
    """Raised when release evidence cannot support a promotion."""


@dataclass(frozen=True)
class Receipt:
    label: str
    path: Path
    raw: bytes
    payload: Mapping[str, Any]
    sha256: str


@dataclass(frozen=True)
class BuildEvidence:
    receipt: Receipt
    identity: dict[str, Any]
    runtime_identity: dict[str, Any]
    runtime_requirements: dict[str, Any]
    memory: dict[str, int | bool]
    full_memory: dict[str, Any]
    engine: dict[str, str]
    toolchain: dict[str, str]
    wasm: Path
    module_js: Path
    source_fingerprint: str
    dependency_closure: dict[str, Any]


@dataclass(frozen=True)
class ValidatedEvidence:
    build: BuildEvidence
    receipts: dict[str, Receipt]
    root_contract: dict[str, Any]
    checked_horizon_proof_research: dict[str, Any]
    prefix_contract: dict[str, Any]
    oracle_signature_sha256: str
    root_config: dict[str, Any]
    root_differential_cases: int
    prefix_differential_cases: int
    mate_differential_cases: int
    opera_elapsed_seconds: float
    opera_result: dict[str, Any]
    opera_memory: dict[str, Any]
    safety_reserve_positions: int


@dataclass(frozen=True)
class OperaCheckedHorizonEvidence:
    receipt: Receipt
    local_checkout_asset_set_sha256: str
    elapsed_seconds: float
    work: int
    line_rejections: int
    native_repairs: int
    candidate_vetoes: int
    selected_root_series: str
    principal_variation_sha256: str
    selected_fixture_id: str
    known_adverse_excluded: bool
    selected_boundary_ladder_certified: bool
    found_stop_observed: bool
    unknown_fail_closed_observed: bool
    selected_horizon_exhaustively_certified: bool
    selected_root_child_exhaustively_certified: bool
    raw_safety_trace_count: int
    raw_safety_trace_sha256: str
    raw_research_trace_count: int
    raw_research_trace_sha256: str
    raw_trace_attestation: dict[str, Any]
    selected_d5_horizon_certification_witness: dict[str, Any]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseGateError(f"{label} must be a JSON object")
    return value


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ReleaseGateError(f"{label} must be a JSON array")
    return value


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReleaseGateError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: object, label: str, minimum: float = 0.0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise ReleaseGateError(f"{label} must be a finite number >= {minimum}")
    return float(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ReleaseGateError(f"{label} must be non-empty canonical text")
    return value


def _true(mapping: Mapping[str, Any], key: str, label: str) -> None:
    if mapping.get(key) is not True:
        raise ReleaseGateError(f"{label} gate {key!r} did not pass")


def _false(mapping: Mapping[str, Any], key: str, label: str) -> None:
    if mapping.get(key) is not False:
        raise ReleaseGateError(f"{label} field {key!r} must be false")


def _signed_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReleaseGateError(f"{label} must be an integer")
    return value


def _validate_checked_horizon_case(
    value: object,
    *,
    label: str,
    root_side: str,
    root_order_key: str,
    proof_order: list[str],
    proof_path_lengths: list[int],
    child_depth: int,
    score: int,
    prior_score: int,
    prior_schema: str,
    hits: int,
    hit_mask: int,
    exact_tt_hits: int,
    disposition: str,
    candidate_sha256: str,
    proof_set_sha256: str,
    root_pv_sha256: str | None = None,
) -> dict[str, Any]:
    case = dict(_mapping(value, label))
    expected_keys = {
        "root_side",
        "root_order_key",
        "request_proof_count",
        "request_proof_order",
        "request_proof_path_lengths",
        "newest_proof_anchor",
        "child_depth",
        "schema",
        "status",
        "bound",
        "score",
        "horizon_proofs_validated",
        "horizon_proof_hits",
        "horizon_proof_hit_mask",
        "horizon_proof_set_identity_sha256",
        "candidate_identity_sha256",
        "exact_tt_hits",
        "prior_same_root_schema",
        "prior_same_root_status",
        "prior_same_root_bound",
        "prior_same_root_score",
        "prior_same_root_candidate_identity_sha256",
        "disposition",
    }
    if root_pv_sha256 is not None:
        expected_keys.update(
            {"root_pv_sha256", "prior_same_root_root_pv_sha256"}
        )
    if set(case) != expected_keys:
        raise ReleaseGateError(f"{label} fields do not match the exact evidence schema")
    proof_count = len(proof_order)
    actual_order = _list(case.get("request_proof_order"), f"{label} proof order")
    if any(not isinstance(item, str) for item in actual_order):
        raise ReleaseGateError(f"{label} proof order anchors must be strings")
    actual_lengths = _list(
        case.get("request_proof_path_lengths"),
        f"{label} proof path lengths",
    )
    if any(
        isinstance(item, bool) or not isinstance(item, int)
        for item in actual_lengths
    ):
        raise ReleaseGateError(f"{label} proof path lengths must be exact integers")
    expected = {
        "root_side": root_side,
        "root_order_key": root_order_key,
        "request_proof_count": proof_count,
        "request_proof_order": proof_order,
        "request_proof_path_lengths": proof_path_lengths,
        "newest_proof_anchor": proof_order[-1],
        "child_depth": child_depth,
        "schema": "spc-root-horizon-research-result-v1",
        "status": "complete",
        "bound": "exact",
        "score": score,
        "horizon_proofs_validated": proof_count,
        "horizon_proof_hits": hits,
        "horizon_proof_hit_mask": hit_mask,
        "horizon_proof_set_identity_sha256": proof_set_sha256,
        "candidate_identity_sha256": candidate_sha256,
        "exact_tt_hits": exact_tt_hits,
        "prior_same_root_schema": prior_schema,
        "prior_same_root_status": "complete",
        "prior_same_root_bound": "exact",
        "prior_same_root_score": prior_score,
        "prior_same_root_candidate_identity_sha256": candidate_sha256,
        "disposition": disposition,
    }
    if root_pv_sha256 is not None:
        expected.update(
            {
                "root_pv_sha256": root_pv_sha256,
                "prior_same_root_root_pv_sha256": root_pv_sha256,
            }
        )
    for key in (
        "request_proof_count",
        "child_depth",
        "horizon_proofs_validated",
        "horizon_proof_hits",
        "horizon_proof_hit_mask",
        "exact_tt_hits",
    ):
        _integer(case.get(key), f"{label} field {key}")
    for key, expected_value in expected.items():
        if case.get(key) != expected_value:
            raise ReleaseGateError(
                f"{label} field {key!r} is not exact checked-horizon evidence"
            )
    _signed_integer(case.get("score"), f"{label} score")
    _signed_integer(case.get("prior_same_root_score"), f"{label} prior score")
    for key in (
        "horizon_proof_set_identity_sha256",
        "candidate_identity_sha256",
        "prior_same_root_candidate_identity_sha256",
    ):
        digest = _text(case.get(key), f"{label} {key}")
        if HEX_64.fullmatch(digest) is None:
            raise ReleaseGateError(f"{label} {key} is not a SHA-256 commitment")
    if root_pv_sha256 is not None:
        for key in ("root_pv_sha256", "prior_same_root_root_pv_sha256"):
            digest = _text(case.get(key), f"{label} {key}")
            if HEX_64.fullmatch(digest) is None:
                raise ReleaseGateError(f"{label} {key} is not a SHA-256 commitment")
    newest_bit = 1 << (proof_count - 1)
    newest_hit = hit_mask & newest_bit != 0
    if hits == 0 and hit_mask == 0 and exact_tt_hits > 0:
        expected_disposition = "warm-exact-recertified"
    else:
        expected_disposition = (
            "same-root-repaired" if newest_hit else "newest-proof-not-hit"
        )
    if disposition != expected_disposition:
        raise ReleaseGateError(f"{label} disposition does not follow request-order hits")
    if disposition == "same-root-repaired" and score == prior_score:
        raise ReleaseGateError(f"{label} did not change the same-root result")
    return case


def _validate_checked_horizon_evidence(
    value: object,
    *,
    label: str,
) -> dict[str, Any]:
    evidence = dict(_mapping(value, label))
    expected_keys = {
        "schema",
        "white_deep_two_proof",
        "white_deep_warm_exact",
        "white_deep_reversed_order",
        "black_parity",
    }
    if set(evidence) != expected_keys or evidence.get("schema") != CHECKED_HORIZON_EVIDENCE_SCHEMA:
        raise ReleaseGateError(f"{label} does not match the exact evidence schema")
    white = _validate_checked_horizon_case(
        evidence["white_deep_two_proof"],
        label=f"{label} white deep two-proof",
        root_side="white",
        root_order_key="h4g2",
        proof_order=["alternate", "deep"],
        proof_path_lengths=[3, 3],
        child_depth=2,
        score=179,
        prior_score=336,
        prior_schema="spc-root-candidate-result-v1",
        hits=1,
        hit_mask=0b10,
        exact_tt_hits=0,
        disposition="same-root-repaired",
        candidate_sha256=WHITE_HORIZON_CANDIDATE_SHA256,
        proof_set_sha256=WHITE_HORIZON_PROOF_SET_SHA256,
    )
    warm = _validate_checked_horizon_case(
        evidence["white_deep_warm_exact"],
        label=f"{label} white deep warm exact",
        root_side="white",
        root_order_key="h4g2",
        proof_order=["alternate", "deep"],
        proof_path_lengths=[3, 3],
        child_depth=2,
        score=179,
        prior_score=179,
        prior_schema="spc-root-horizon-research-result-v1",
        hits=0,
        hit_mask=0,
        exact_tt_hits=1,
        disposition="warm-exact-recertified",
        candidate_sha256=WHITE_HORIZON_CANDIDATE_SHA256,
        proof_set_sha256=WHITE_HORIZON_PROOF_SET_SHA256,
        root_pv_sha256=WHITE_HORIZON_ROOT_PV_SHA256,
    )
    reversed_case = _validate_checked_horizon_case(
        evidence["white_deep_reversed_order"],
        label=f"{label} white deep reversed-order",
        root_side="white",
        root_order_key="h4g2",
        proof_order=["deep", "alternate"],
        proof_path_lengths=[3, 3],
        child_depth=2,
        score=179,
        prior_score=336,
        prior_schema="spc-root-candidate-result-v1",
        hits=1,
        hit_mask=0b01,
        exact_tt_hits=0,
        disposition="newest-proof-not-hit",
        candidate_sha256=WHITE_HORIZON_CANDIDATE_SHA256,
        proof_set_sha256=WHITE_HORIZON_PROOF_SET_SHA256,
    )
    _validate_checked_horizon_case(
        evidence["black_parity"],
        label=f"{label} Black parity",
        root_side="black",
        root_order_key="f7f5/b8b1",
        proof_order=["black-mate"],
        proof_path_lengths=[1],
        child_depth=0,
        score=1_000_000 - 2,
        prior_score=-235,
        prior_schema="spc-root-candidate-result-v1",
        hits=1,
        hit_mask=0b1,
        exact_tt_hits=0,
        disposition="same-root-repaired",
        candidate_sha256=BLACK_HORIZON_CANDIDATE_SHA256,
        proof_set_sha256=BLACK_HORIZON_PROOF_SET_SHA256,
    )
    if (
        white["horizon_proof_set_identity_sha256"]
        != reversed_case["horizon_proof_set_identity_sha256"]
        or white["candidate_identity_sha256"]
        != reversed_case["candidate_identity_sha256"]
        or warm["horizon_proof_set_identity_sha256"]
        != white["horizon_proof_set_identity_sha256"]
        or warm["candidate_identity_sha256"]
        != white["candidate_identity_sha256"]
    ):
        raise ReleaseGateError(
            f"{label} reversed order changed the proof set or retained root identity"
        )
    return evidence


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _same_json(left: object, right: object) -> bool:
    return left == right


_BOUNDARY_KEYS = {
    "board_fen",
    "chess960",
    "ep_targets",
    "fen",
    "progressive_ep",
    "promoted_hex",
    "quiet_draw_pending",
    "quiet_series",
    "series",
    "series_number",
    "side_to_move",
}
_SERIES_KEYS = {
    "child_boundary",
    "ended_by_check",
    "machine_notation",
    "moves",
    "outcome",
    "transposition_count",
}
_UCI_MOVE = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?")


def _validate_boundary(value: object, label: str) -> dict[str, Any]:
    boundary = dict(_mapping(value, label))
    if set(boundary) != _BOUNDARY_KEYS:
        raise ReleaseGateError(f"{label} does not have the exact boundary shape")
    fen = _text(boundary.get("fen"), f"{label} FEN")
    if boundary.get("board_fen") != fen or len(fen.split(" ")) != 6:
        raise ReleaseGateError(f"{label} FEN fields are inconsistent")
    series = _integer(boundary.get("series"), f"{label} series", 1)
    if boundary.get("series_number") != series:
        raise ReleaseGateError(f"{label} series aliases differ")
    quiet = _integer(boundary.get("quiet_series"), f"{label} quiet series")
    if boundary.get("quiet_draw_pending") is not (quiet >= 10):
        raise ReleaseGateError(f"{label} quiet-draw state is inconsistent")
    ep_targets = _list(boundary.get("ep_targets"), f"{label} en-passant targets")
    if (
        len(ep_targets) > 8
        or len(set(ep_targets)) != len(ep_targets)
        or any(not isinstance(square, str) or re.fullmatch(r"[a-h][1-8]", square) is None for square in ep_targets)
        or boundary.get("progressive_ep") != ep_targets
    ):
        raise ReleaseGateError(f"{label} en-passant state is not canonical")
    if re.fullmatch(r"[0-9a-f]{16}", str(boundary.get("promoted_hex", ""))) is None:
        raise ReleaseGateError(f"{label} promoted-piece mask is not canonical")
    if boundary.get("chess960") is not False:
        raise ReleaseGateError(f"{label} must use ordinary chess geometry")
    mover = "white" if fen.split(" ")[1] == "w" else "black"
    if boundary.get("side_to_move") != mover or ((series % 2 == 1) != (mover == "white")):
        raise ReleaseGateError(f"{label} mover and series parity disagree")
    return boundary


def _validate_series(value: object, label: str) -> dict[str, Any]:
    series = dict(_mapping(value, label))
    if set(series) != _SERIES_KEYS:
        raise ReleaseGateError(f"{label} does not have the exact series shape")
    moves = _list(series.get("moves"), f"{label} moves")
    if (
        not moves
        or any(not isinstance(move, str) or _UCI_MOVE.fullmatch(move) is None for move in moves)
        or series.get("machine_notation") != "/".join(moves)
    ):
        raise ReleaseGateError(f"{label} move sequence is not canonical")
    _integer(series.get("transposition_count"), f"{label} transposition count", 1)
    if not isinstance(series.get("ended_by_check"), bool):
        raise ReleaseGateError(f"{label} check flag must be boolean")
    if series.get("outcome") not in {None, "checkmate", "stalemate", "ten_series_draw"}:
        raise ReleaseGateError(f"{label} has an invalid outcome")
    _validate_boundary(series.get("child_boundary"), f"{label} child boundary")
    return series


def _boundary_from_state(state: ProgressiveState) -> dict[str, Any]:
    ep_targets = [chess.square_name(square) for square in state.ep_targets]
    fen = state.board.fen(en_passant="fen")
    return {
        "board_fen": fen,
        "chess960": bool(state.board.chess960),
        "ep_targets": ep_targets,
        "fen": fen,
        "progressive_ep": ep_targets,
        "promoted_hex": f"{state.board.promoted:016x}",
        "quiet_draw_pending": state.quiet_draw_pending,
        "quiet_series": state.quiet_series,
        "series": state.series_number,
        "series_number": state.series_number,
        "side_to_move": "white" if state.board.turn == chess.WHITE else "black",
    }


def _validate_rooted_path_continuity(
    path: list[dict[str, Any]],
    *,
    label: str,
) -> None:
    state = ProgressiveState.initial()
    for index, series in enumerate(path):
        expected_series = index + 1
        moves = _list(series.get("moves"), f"{label} series {expected_series} moves")
        try:
            result = play_series(state, moves)
        except (SeriesLegalityError, ValueError, chess.InvalidMoveError) as error:
            raise ReleaseGateError(
                f"{label} rooted path failed authoritative replay at series "
                f"{expected_series}: {error}"
            ) from error
        final_state = result.final_state
        actual_boundary = _boundary_from_state(final_state)
        expected_outcome = (
            None
            if result.outcome is None
            else str(result.outcome.value).replace("-", "_")
        )
        if (
            tuple(moves) != result.moves
            or series.get("child_boundary") != actual_boundary
            or series.get("ended_by_check") is not result.ended_by_check
            or series.get("outcome") != expected_outcome
            or (index < len(path) - 1 and result.outcome is not None)
        ):
            raise ReleaseGateError(
                f"{label} rooted path is not contiguous with its authoritative boundaries"
            )
        state = final_state


def _worker_identity(value: object, label: str) -> dict[str, Any]:
    worker = dict(_mapping(value, label))
    if set(worker) != {"factory_sequence", "name", "channel_id", "url", "type"}:
        raise ReleaseGateError(f"{label} does not have the exact Worker identity shape")
    _integer(worker.get("factory_sequence"), f"{label} factory sequence", 1)
    _text(worker.get("name"), f"{label} name")
    if worker.get("channel_id") is not None:
        _text(worker.get("channel_id"), f"{label} channel")
    if worker.get("type") != "module":
        raise ReleaseGateError(f"{label} is not an ordinary module Worker")
    _text(worker.get("url"), f"{label} URL")
    return worker


def _validate_trace_envelope(value: object, label: str) -> dict[str, Any]:
    trace = dict(_mapping(value, label))
    if set(trace) != {
        "worker",
        "request_sequence",
        "posted_monotonic_ms",
        "received_monotonic_ms",
        "request",
        "ok",
        "response",
    }:
        raise ReleaseGateError(f"{label} does not have the exact trace envelope")
    _worker_identity(trace.get("worker"), f"{label} Worker")
    _integer(trace.get("request_sequence"), f"{label} request sequence", 1)
    posted = _number(trace.get("posted_monotonic_ms"), f"{label} posted time")
    received = _number(trace.get("received_monotonic_ms"), f"{label} received time")
    if received < posted or trace.get("ok") is not True:
        raise ReleaseGateError(f"{label} is not a successful ordered Worker exchange")
    _mapping(trace.get("request"), f"{label} request")
    _mapping(trace.get("response"), f"{label} response")
    return trace


def _identity_fields(
    evidence: ValidatedEvidence,
    certificates: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    build = evidence.build
    root = certificates["root_session"]
    prefix = certificates["prefix"]
    root_identity = {
        "source_fingerprint": build.identity["source_fingerprint"],
        "kernel_sha256": build.identity["kernel_sha256"],
        "module_js_sha256": build.identity["module_js_sha256"],
        "certificate_id": root["certificate_id"],
        "runtime_variant": "single",
        "thread_count": 1,
        "engine_version": build.engine["engine_version"],
        "ruleset_version": build.engine["ruleset_version"],
        "profile_id": build.engine["profile_id"],
    }
    prefix_identity = {
        "source_fingerprint": build.identity["source_fingerprint"],
        "wasm_sha256": build.identity["wasm_sha256"],
        "module_js_sha256": build.identity["module_js_sha256"],
        "certificate_id": prefix["certificate_id"],
        "engine_version": build.engine["engine_version"],
        "ruleset_version": build.engine["ruleset_version"],
    }
    return root_identity, prefix_identity


def _require_fields_match(
    value: object,
    expected: Mapping[str, Any],
    label: str,
) -> Mapping[str, Any]:
    subject = _mapping(value, label)
    for key, expected_value in expected.items():
        if subject.get(key) != expected_value:
            raise ReleaseGateError(f"{label} field {key!r} is not identity-bound")
    return subject


def _validate_trace_work(
    trace: Mapping[str, Any],
    *,
    maximum_work: int,
    require_positive: bool,
    require_warm_exact: bool = False,
) -> None:
    request = _mapping(trace.get("request"), "checked-horizon trace request")
    response = _mapping(trace.get("response"), "checked-horizon trace response")
    work = _mapping(response.get("work"), "checked-horizon trace work")
    external = _integer(request.get("external_work"), "trace external work")
    before = _integer(request.get("native_work_before"), "trace native work before")
    credit = _integer(request.get("call_work_credit"), "trace work credit", 1)
    if external > maximum_work or before > maximum_work or credit > 0xFFFFFFFF:
        raise ReleaseGateError("checked-horizon trace work request exceeds its hard limits")
    for key, expected in (
        ("external_work", external),
        ("native_work_before", before),
        ("call_work_credit", credit),
    ):
        if work.get(key) != expected:
            raise ReleaseGateError(f"checked-horizon trace work field {key!r} drifted")
    after = _integer(work.get("native_work_after"), "trace native work after", before)
    call_work = _integer(
        work.get("call_native_work"),
        "trace call-native work",
        1 if require_positive else 0,
    )
    if call_work > credit or call_work != after - before:
        raise ReleaseGateError("checked-horizon trace call work is not conserved")
    if work.get("total_accounted_work") != external + after or external + after > maximum_work:
        raise ReleaseGateError("checked-horizon trace total work is not conserved")
    call_stats = _mapping(work.get("call_stats"), "checked-horizon call stats")
    cumulative_stats = _mapping(work.get("cumulative_stats"), "checked-horizon cumulative stats")
    if (
        not call_stats
        or set(call_stats) != set(cumulative_stats)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (*call_stats.values(), *cumulative_stats.values())
        )
        or any(cumulative_stats[key] < call_stats[key] for key in call_stats)
    ):
        raise ReleaseGateError("checked-horizon trace search statistics are malformed")
    if require_warm_exact:
        if _integer(call_stats.get("tt_hits"), "warm exact TT hits", 1) < 1:
            raise ReleaseGateError("checked-horizon warm recertification did not reuse exact TT state")
    for entries, peak, capacity in (
        ("tt_entries", "tt_entries_peak", "tt_capacity"),
        ("eval_entries", "eval_entries_peak", "eval_capacity"),
    ):
        cap = _integer(work.get(capacity), f"trace {capacity}", 1)
        count = _integer(work.get(entries), f"trace {entries}")
        high = _integer(work.get(peak), f"trace {peak}")
        if count > cap or high < count or high > cap:
            raise ReleaseGateError("checked-horizon trace cache envelope is invalid")
    series_capacity = _integer(work.get("series_cache_capacity"), "trace series cache capacity", 1)
    weight_peak = _integer(work.get("series_cache_weight_peak"), "trace series cache weight peak")
    entry_peak = _integer(work.get("series_cache_entries_peak"), "trace series cache entry peak")
    if weight_peak > series_capacity or entry_peak > weight_peak:
        raise ReleaseGateError("checked-horizon trace series cache envelope is invalid")


def _same_final_board(frame_fen: object, board_fen: object) -> bool:
    if not isinstance(frame_fen, str) or not isinstance(board_fen, str):
        return False
    frame = frame_fen.split(" ")
    board = board_fen.split(" ")
    return len(frame) == 6 and len(board) == 6 and all(
        frame[index] == board[index] for index in (0, 1, 2, 4, 5)
    )


def _validate_checked_prefix_mate(
    value: object,
    *,
    child: Mapping[str, Any],
    mate_moves: list[str],
    request_id: str,
    prefix_identity: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    checked = dict(_require_fields_match(value, prefix_identity, label))
    if (
        checked.get("schema") != "spc-boundary-prefix-v1"
        or checked.get("abi_version") != 1
        or checked.get("ok") is not True
        or checked.get("status") != "complete"
        or checked.get("request_id") != request_id
        or checked.get("boundary_state") != child
        or checked.get("prefix") != mate_moves
        or checked.get("current_prefix") != mate_moves
    ):
        raise ReleaseGateError(f"{label} is not the exact authoritative mate replay")
    san = _list(checked.get("san"), f"{label} SAN")
    frames = _list(checked.get("frames"), f"{label} frames")
    if len(san) != len(mate_moves) or len(frames) != len(mate_moves):
        raise ReleaseGateError(f"{label} did not replay every mate move")
    for index, frame_value in enumerate(frames):
        frame = _mapping(frame_value, f"{label} frame {index + 1}")
        if (
            frame.get("index") != index + 1
            or frame.get("uci") != mate_moves[index]
            or not isinstance(frame.get("san"), str)
            or frame.get("san") != san[index]
            or not isinstance(frame.get("board_fen"), str)
            or len(frame["board_fen"].split(" ")) != 6
        ):
            raise ReleaseGateError(f"{label} frame {index + 1} is not canonical")
    remaining = int(child["series"]) - len(mate_moves)
    if (
        checked.get("fen") != checked.get("board_fen")
        or not _same_final_board(frames[-1].get("board_fen"), checked.get("board_fen"))
        or checked.get("complete") is not True
        or checked.get("outcome") != "checkmate"
        or checked.get("completion_reason") != "checkmate"
        or checked.get("ended_by_check") is not True
        or checked.get("check") is not True
        or checked.get("in_check") is not True
        or any(checked.get(key) != remaining for key in ("remaining", "moves_remaining", "unused_moves"))
        or checked.get("legal_next") != []
        or checked.get("legal_moves") != []
    ):
        raise ReleaseGateError(f"{label} did not finish with the exact mate outcome")
    next_state = _validate_boundary(checked.get("next_state"), f"{label} next state")
    if (
        next_state.get("fen") != checked.get("board_fen")
        or next_state.get("series") != int(child["series"]) + 1
    ):
        raise ReleaseGateError(f"{label} mate transition is inconsistent")
    return checked


def _validate_raw_safety_trace(
    value: object,
    *,
    expected_root: str,
    expected_unsafe_horizon: str | None,
    expected_child_fen: str | None,
    root_identity: Mapping[str, Any],
    prefix_identity: Mapping[str, Any],
    maximum_work: int,
    label: str,
) -> dict[str, Any]:
    trace = _validate_trace_envelope(value, label)
    request = _require_fields_match(trace["request"], root_identity, f"{label} request")
    response = _require_fields_match(trace["response"], root_identity, f"{label} response")
    if request.get("schema") != "spc-root-safety-task-v1" or response.get("status") != "found":
        raise ReleaseGateError(f"{label} is not a raw found safety result")
    for key, requested in request.items():
        if key not in response or response[key] != requested:
            raise ReleaseGateError(f"{label} response did not echo request field {key!r}")
    request_id = _text(request.get("request_id"), f"{label} request id")
    if (
        request.get("iteration_id") != f"{request_id}:d5"
        or not isinstance(request.get("generation"), int)
        or int(request["generation"]) < 1
        or not isinstance(request.get("safety_revision"), int)
        or int(request["safety_revision"]) < 0
        or not isinstance(request.get("incumbent_epoch"), int)
        or int(request["incumbent_epoch"]) < 0
        or _number(request.get("deadline_monotonic_ms"), f"{label} deadline")
        <= float(trace["received_monotonic_ms"])
        or _number(request.get("deadline_epoch_ms"), f"{label} epoch deadline") <= 0
        or not 1 <= _integer(request.get("remaining_time_ms"), f"{label} remaining time") <= 60_000
    ):
        raise ReleaseGateError(f"{label} request identity, revision, or deadline is invalid")
    candidate = _mapping(request.get("candidate"), f"{label} candidate")
    if (
        request.get("candidate_identity") != candidate.get("candidate_identity")
        or candidate.get("order_key") != expected_root
    ):
        raise ReleaseGateError(f"{label} candidate is not bound to the expected root")
    unsafe = _validate_series(candidate.get("root_series"), f"{label} unsafe horizon")
    if (
        expected_unsafe_horizon is not None
        and unsafe.get("machine_notation") != expected_unsafe_horizon
    ):
        raise ReleaseGateError(f"{label} substituted the unsafe horizon trace")
    child = _validate_boundary(
        request.get("authoritative_child_boundary"),
        f"{label} authoritative child",
    )
    if (
        child.get("series") != 6
        or (expected_child_fen is not None and child.get("fen") != expected_child_fen)
    ):
        raise ReleaseGateError(f"{label} authoritative child is not the fixed fixture")
    if unsafe.get("child_boundary") != child or unsafe.get("ended_by_check") is not True:
        raise ReleaseGateError(f"{label} unsafe horizon is not rooted in its child")
    replay = _require_fields_match(
        request.get("authoritative_root_replay"),
        prefix_identity,
        f"{label} root replay",
    )
    expected_replay_id = f"{request.get('iteration_id')}:{request.get('safety_revision')}:pv-horizon-replay-4"
    if (
        replay.get("schema") != "spc-boundary-prefix-v1"
        or replay.get("request_id") != expected_replay_id
        or replay.get("prefix") != unsafe.get("moves")
        or replay.get("current_prefix") != unsafe.get("moves")
        or replay.get("complete") is not True
        or replay.get("outcome") is not None
        or replay.get("completion_reason") != "check"
        or replay.get("ended_by_check") is not True
        or replay.get("check") is not True
        or replay.get("in_check") is not True
        or replay.get("next_state") != child
    ):
        raise ReleaseGateError(f"{label} root replay is not authoritative")
    mate = dict(_mapping(response.get("reply_mate"), f"{label} reply mate"))
    if set(mate) != {"checked_prefix", "ended_by_check", "machine_notation", "moves", "outcome"}:
        raise ReleaseGateError(f"{label} raw mate does not have the exact shape")
    moves = _list(mate.get("moves"), f"{label} mate moves")
    if (
        not moves
        or len(moves) > int(child["series"])
        or any(not isinstance(move, str) or _UCI_MOVE.fullmatch(move) is None for move in moves)
        or mate.get("machine_notation") != "/".join(moves)
        or mate.get("outcome") != "checkmate"
        or mate.get("ended_by_check") is not True
    ):
        raise ReleaseGateError(f"{label} raw mate sequence is not canonical")
    _validate_checked_prefix_mate(
        mate.get("checked_prefix"),
        child=child,
        mate_moves=moves,
        request_id=f"{request.get('iteration_id')}:{request.get('safety_revision')}:mate-replay",
        prefix_identity=prefix_identity,
        label=f"{label} compiled mate replay",
    )
    expected_score = 1_000_000 - 2 if child.get("side_to_move") == "white" else -1_000_000 + 2
    expected_bounds = [1, 1] if child.get("side_to_move") == "white" else [-1, -1]
    credit = _integer(request.get("call_work_credit"), f"{label} work credit", 1)
    if (
        not isinstance(response.get("work_used"), int)
        or not 1 <= int(response["work_used"]) <= credit
        or response.get("override_score") != expected_score
        or response.get("proof_bounds") != expected_bounds
    ):
        raise ReleaseGateError(f"{label} raw mate result is not fail-closed")
    return trace


def _validate_exhausted_safety_trace(
    value: object,
    *,
    expected_root: str,
    expected_series: Mapping[str, Any],
    expected_parent_boundary: Mapping[str, Any],
    expected_replay_suffix: str,
    expected_call_work_credit: int,
    root_identity: Mapping[str, Any],
    prefix_identity: Mapping[str, Any],
    maximum_work: int,
    label: str,
) -> dict[str, Any]:
    trace = _validate_trace_envelope(value, label)
    request = _require_fields_match(trace["request"], root_identity, f"{label} request")
    response = _require_fields_match(trace["response"], root_identity, f"{label} response")
    if (
        request.get("schema") != "spc-root-safety-task-v1"
        or response.get("status") != "exhausted"
        or set(response)
        != {*request, "status", "work_used", "memory_bytes", "memory_peak_bytes"}
    ):
        raise ReleaseGateError(f"{label} is not an exact exhausted safety result")
    for key, requested in request.items():
        if response.get(key) != requested:
            raise ReleaseGateError(f"{label} response did not echo request field {key!r}")
    request_id = _text(request.get("request_id"), f"{label} request id")
    if (
        request.get("iteration_id") != f"{request_id}:d5"
        or not isinstance(request.get("generation"), int)
        or int(request["generation"]) < 1
        or not isinstance(request.get("safety_revision"), int)
        or int(request["safety_revision"]) < 0
        or not isinstance(request.get("incumbent_epoch"), int)
        or int(request["incumbent_epoch"]) < 0
        or _number(request.get("deadline_monotonic_ms"), f"{label} deadline")
        <= float(trace["received_monotonic_ms"])
        or _number(request.get("deadline_epoch_ms"), f"{label} epoch deadline") <= 0
        or not 1 <= _integer(request.get("remaining_time_ms"), f"{label} remaining time") <= 60_000
    ):
        raise ReleaseGateError(f"{label} request identity, revision, or deadline is invalid")

    candidate = _mapping(request.get("candidate"), f"{label} candidate")
    normalized_series = _validate_series(expected_series, f"{label} expected series")
    candidate_series = _validate_series(candidate.get("root_series"), f"{label} candidate series")
    child = _validate_boundary(
        request.get("authoritative_child_boundary"),
        f"{label} authoritative child",
    )
    if (
        request.get("candidate_identity") != candidate.get("candidate_identity")
        or candidate.get("order_key") != expected_root
        or candidate_series != normalized_series
        or child != normalized_series.get("child_boundary")
    ):
        raise ReleaseGateError(f"{label} candidate is not bound to the selected D5 series")

    parent = _validate_boundary(expected_parent_boundary, f"{label} expected parent")
    replay = _require_fields_match(
        request.get("authoritative_root_replay"),
        prefix_identity,
        f"{label} root replay",
    )
    expected_replay_id = (
        f"{request.get('iteration_id')}:{request.get('safety_revision')}:"
        f"{expected_replay_suffix}"
    )
    ended_by_check = normalized_series.get("ended_by_check") is True
    if (
        replay.get("schema") != "spc-boundary-prefix-v1"
        or replay.get("request_id") != expected_replay_id
        or replay.get("boundary_state") != parent
        or replay.get("prefix") != normalized_series.get("moves")
        or replay.get("current_prefix") != normalized_series.get("moves")
        or replay.get("complete") is not True
        or replay.get("outcome") != normalized_series.get("outcome")
        or replay.get("completion_reason") != ("check" if ended_by_check else "budget")
        or replay.get("ended_by_check") is not ended_by_check
        or replay.get("check") is not ended_by_check
        or replay.get("in_check") is not ended_by_check
        or replay.get("next_state") != child
        or any(replay.get(key) != 0 for key in ("remaining", "moves_remaining", "unused_moves"))
        or replay.get("legal_next") != []
        or replay.get("legal_moves") != []
    ):
        raise ReleaseGateError(f"{label} root replay is not the exact authoritative series")

    credit = _integer(request.get("call_work_credit"), f"{label} work credit", 1)
    work_used = _integer(response.get("work_used"), f"{label} work used", 1)
    memory = _integer(response.get("memory_bytes"), f"{label} memory", 1)
    peak = _integer(response.get("memory_peak_bytes"), f"{label} peak memory", memory)
    if (
        credit != expected_call_work_credit
        or credit > maximum_work
        or work_used > credit
        or peak < memory
    ):
        raise ReleaseGateError(f"{label} exhausted-search work or memory envelope drifted")
    return trace


def _series_semantic(series: Mapping[str, Any], *, series_index: int) -> dict[str, Any]:
    return {
        "series_index": series_index,
        "moves": list(_list(series.get("moves"), "series semantic moves")),
        "child_boundary": dict(
            _mapping(series.get("child_boundary"), "series semantic child boundary")
        ),
        "outcome": series.get("outcome"),
        "ended_by_check": series.get("ended_by_check"),
    }


def _validate_retained_proof(value: object, *, root: str, depth: int, label: str) -> dict[str, Any]:
    proof = dict(_mapping(value, label))
    if set(proof) != {"schema", "rooted_path", "mate_reply"} or proof.get("schema") != "spc-retained-root-horizon-proof-v1":
        raise ReleaseGateError(f"{label} does not have the exact retained-proof shape")
    path = _list(proof.get("rooted_path"), f"{label} rooted path")
    if (
        not 1 <= len(path) <= depth + 1
        or len(path) > 8
        or len(path) % 2 != 1
    ):
        raise ReleaseGateError(f"{label} rooted path has the wrong depth")
    normalized_path = [
        _validate_series(item, f"{label} rooted series {index}")
        for index, item in enumerate(path)
    ]
    _validate_rooted_path_continuity(normalized_path, label=label)
    if normalized_path[0].get("machine_notation") != root:
        raise ReleaseGateError(f"{label} is not rooted at the candidate series")
    mate = _validate_series(proof.get("mate_reply"), f"{label} mate reply")
    if (
        mate.get("transposition_count") != 1
        or mate.get("outcome") != "checkmate"
        or mate.get("ended_by_check") is not True
    ):
        raise ReleaseGateError(f"{label} mate reply is not exact")
    return proof


_HORIZON_ECHO_KEYS = (
    "session_id",
    "request_id",
    "iteration_id",
    "generation",
    "deadline_monotonic_ms",
    "remaining_time_ms",
    "source_fingerprint",
    "kernel_sha256",
    "module_js_sha256",
    "certificate_id",
    "runtime_variant",
    "thread_count",
    "engine_version",
    "ruleset_version",
    "profile_id",
    "safety_revision",
    "incumbent_epoch",
    "task_id",
    "enumeration_identity",
    "candidate_identity",
    "order_index",
    "order_key",
    "purpose",
    "mate_score",
    "child_depth",
    "alpha",
    "beta",
    "tt_persistence",
    "mover",
)


def _validate_horizon_research_trace(
    value: object,
    *,
    expected_root: str,
    root_identity: Mapping[str, Any],
    maximum_work: int,
    require_newest_hit: bool,
    require_warm_exact: bool,
    label: str,
) -> dict[str, Any]:
    trace = _validate_trace_envelope(value, label)
    request = _require_fields_match(trace["request"], root_identity, f"{label} request")
    response = _require_fields_match(trace["response"], root_identity, f"{label} response")
    if (
        request.get("schema") != "spc-root-horizon-research-task-v1"
        or response.get("schema") != "spc-root-horizon-research-result-v1"
        or response.get("abi_version") != 2
        or response.get("product_publishable") is not False
        or response.get("safety_certified") is not False
        or response.get("status") != "complete"
        or response.get("bound") != "exact"
        or response.get("session_id") != request.get("session_id")
        or any(response.get(key) != request.get(key) for key in _HORIZON_ECHO_KEYS)
    ):
        raise ReleaseGateError(f"{label} is not an exact horizon-research exchange")
    request_id = _text(request.get("request_id"), f"{label} request id")
    if request.get("iteration_id") != f"{request_id}:d5":
        raise ReleaseGateError(f"{label} iteration is not rooted at D5")
    _integer(request.get("generation"), f"{label} generation", 1)
    _text(request.get("task_id"), f"{label} task id")
    _text(request.get("enumeration_identity"), f"{label} enumeration identity")
    candidate_identity = _text(request.get("candidate_identity"), f"{label} candidate identity")
    _integer(request.get("order_index"), f"{label} order index")
    _text(request.get("order_key"), f"{label} order key")
    if (
        request.get("purpose") != "horizon-research"
        or request.get("mate_score") != 1_000_000
        or request.get("child_depth") != 4
        or request.get("alpha") != -2_000_000
        or request.get("beta") != 2_000_000
        or request.get("tt_persistence") != "commit"
        or request.get("mover") != "white"
    ):
        raise ReleaseGateError(f"{label} did not use the exact full-window D5 repair policy")
    deadline = _number(request.get("deadline_monotonic_ms"), f"{label} deadline")
    deadline_epoch = _number(request.get("deadline_epoch_ms"), f"{label} epoch deadline")
    remaining = _integer(request.get("remaining_time_ms"), f"{label} remaining time", 1)
    if (
        deadline <= float(trace["posted_monotonic_ms"])
        or deadline <= float(trace["received_monotonic_ms"])
        or deadline_epoch <= 0
        or remaining > 60_000
    ):
        raise ReleaseGateError(f"{label} was not completed inside its shared deadline")
    proofs = _list(request.get("horizon_proofs"), f"{label} proofs")
    if not 1 <= len(proofs) <= 16:
        raise ReleaseGateError(f"{label} proof count exceeds the certified contract")
    normalized_proofs = [
        _validate_retained_proof(item, root=expected_root, depth=4, label=f"{label} proof {index}")
        for index, item in enumerate(proofs)
    ]
    if len({_canonical_sha256(item) for item in normalized_proofs}) != len(proofs):
        raise ReleaseGateError(f"{label} contains duplicate retained proofs")
    root_series = _validate_series(response.get("root_series"), f"{label} root series")
    child_pv = _list(response.get("child_pv"), f"{label} child PV")
    normalized_child = [
        _validate_series(item, f"{label} child PV {index}")
        for index, item in enumerate(child_pv)
    ]
    newest_path = _list(normalized_proofs[-1]["rooted_path"], f"{label} newest path")
    if (
        root_series.get("machine_notation") != expected_root
        or root_series != newest_path[0]
        or len(normalized_child) > 4
        or [root_series, *normalized_child] == newest_path
    ):
        raise ReleaseGateError(f"{label} did not retain the same root while changing the losing continuation")
    _signed_integer(response.get("score"), f"{label} score")
    if abs(int(response["score"])) >= 2_000_000:
        raise ReleaseGateError(f"{label} returned an invalid score")
    proof_bounds = _list(response.get("proof_bounds"), f"{label} proof bounds")
    if len(proof_bounds) != 2 or any(bound not in {-1, 0, 1} for bound in proof_bounds):
        raise ReleaseGateError(f"{label} proof bounds are invalid")
    if response.get("configured_max_depth") != 5 or response.get("horizon_proofs_validated") != len(proofs):
        raise ReleaseGateError(f"{label} did not validate the complete D5 proof set")
    hits = _integer(response.get("horizon_proof_hits"), f"{label} proof hits")
    mask = _integer(response.get("horizon_proof_hit_mask"), f"{label} hit mask")
    if (
        hits > len(proofs)
        or mask >= 2 ** len(proofs)
        or mask.bit_count() != hits
        or ((hits == 0) != (mask == 0))
        or (require_newest_hit and mask & (1 << (len(proofs) - 1)) == 0)
    ):
        raise ReleaseGateError(f"{label} request-order proof hit accounting is invalid")
    if require_warm_exact and (hits != 0 or mask != 0):
        raise ReleaseGateError(f"{label} warm exact replay unexpectedly revisited a proof")
    set_identity = _text(
        response.get("horizon_proof_set_identity"),
        f"{label} proof-set identity",
    )
    set_prefix = (
        f"spc-horizon-proof-set-v1|candidate{len(candidate_identity)}:"
        f"{candidate_identity}|proofs{len(proofs)}:"
    )
    if not set_identity.startswith(set_prefix) or len(set_identity) <= len(set_prefix):
        raise ReleaseGateError(f"{label} proof-set identity is not candidate-bound")
    _validate_trace_work(
        trace,
        maximum_work=maximum_work,
        require_positive=require_newest_hit,
        require_warm_exact=require_warm_exact,
    )
    return trace


def _trace_occurs(trace: Mapping[str, Any], traces: Sequence[object]) -> bool:
    return any(item == trace for item in traces)


def _validate_same_root_repair_policy(value: object, label: str) -> dict[str, Any]:
    policy = dict(_mapping(value, label))
    expected = {
        "schema": SAME_ROOT_REPAIR_POLICY_SCHEMA,
        "maximum_successful_same_root_repairs": MAXIMUM_SUCCESSFUL_SAME_ROOT_REPAIRS,
    }
    if policy != expected:
        raise ReleaseGateError(f"{label} is not the exact bounded same-root repair policy")
    return policy


def _validate_policy_veto(value: object, label: str) -> dict[str, Any]:
    veto = dict(_mapping(value, label))
    if set(veto) != {
        "schema",
        "candidate_identity",
        "reason",
        "maximum_successful_same_root_repairs",
        "repairs_before_veto",
        "retained_proofs_before_veto",
        "distinct_proofs_observed",
    }:
        raise ReleaseGateError(f"{label} does not have the exact policy-veto shape")
    _text(veto.get("candidate_identity"), f"{label} candidate identity")
    if (
        veto.get("schema") != PV_HORIZON_POLICY_VETO_SCHEMA
        or veto.get("reason") != "same-root-repair-limit"
        or veto.get("maximum_successful_same_root_repairs")
        != MAXIMUM_SUCCESSFUL_SAME_ROOT_REPAIRS
        or veto.get("repairs_before_veto") != MAXIMUM_SUCCESSFUL_SAME_ROOT_REPAIRS
        or veto.get("retained_proofs_before_veto") != MAXIMUM_SUCCESSFUL_SAME_ROOT_REPAIRS
        or veto.get("distinct_proofs_observed") != 2
    ):
        raise ReleaseGateError(f"{label} is not the exact one-repair threshold veto")
    return veto


def _retained_proof_from_raw_safety(
    safety: Mapping[str, Any],
    *,
    root_series: Mapping[str, Any],
    expected_root: str,
    label: str,
) -> dict[str, Any]:
    request = _mapping(safety.get("request"), f"{label} request")
    response = _mapping(safety.get("response"), f"{label} response")
    candidate = _mapping(request.get("candidate"), f"{label} candidate")
    child_pv = _list(candidate.get("child_pv"), f"{label} child PV")
    if len(child_pv) != 4:
        raise ReleaseGateError(f"{label} does not expose a complete D5 child PV")
    normalized_child = [
        _validate_series(item, f"{label} child PV {index}")
        for index, item in enumerate(child_pv)
    ]
    if candidate.get("root_series") != normalized_child[-1]:
        raise ReleaseGateError(f"{label} candidate does not end at its checked-PV horizon")
    mate = _mapping(response.get("reply_mate"), f"{label} reply mate")
    checked = _mapping(mate.get("checked_prefix"), f"{label} checked mate")
    proof = {
        "schema": "spc-retained-root-horizon-proof-v1",
        "rooted_path": [dict(root_series), *normalized_child],
        "mate_reply": {
            "child_boundary": dict(
                _mapping(checked.get("next_state"), f"{label} mate child boundary")
            ),
            "ended_by_check": mate.get("ended_by_check"),
            "machine_notation": mate.get("machine_notation"),
            "moves": list(_list(mate.get("moves"), f"{label} mate moves")),
            "outcome": mate.get("outcome"),
            "transposition_count": 1,
        },
    }
    return _validate_retained_proof(
        proof,
        root=expected_root,
        depth=4,
        label=f"{label} derived proof",
    )


def _validate_safety_repair_crosslink(
    safety: Mapping[str, Any],
    repair: Mapping[str, Any],
    *,
    expected_root: str,
    label: str,
) -> None:
    safety_request = _mapping(safety["request"], f"{label} safety request")
    safety_response = _mapping(safety["response"], f"{label} safety response")
    repair_request = _mapping(repair["request"], f"{label} repair request")
    repair_response = _mapping(repair["response"], f"{label} repair response")
    worker = _mapping(safety["worker"], f"{label} safety Worker")
    if (
        repair["worker"] != worker
        or repair["request_sequence"] <= safety["request_sequence"]
        or repair["posted_monotonic_ms"] < safety["received_monotonic_ms"]
        or worker.get("channel_id") != safety_request.get("candidate", {}).get("owner_worker_id")
    ):
        raise ReleaseGateError(f"{label} repair did not stay on the same warm native Worker")
    for key in ("session_id", "request_id", "iteration_id", "generation"):
        if repair_request.get(key) != safety_request.get(key):
            raise ReleaseGateError(f"{label} repair changed root request field {key!r}")
    if (
        repair_request.get("safety_revision") != int(safety_request.get("safety_revision")) + 1
        or repair_request.get("incumbent_epoch") != safety_request.get("incumbent_epoch")
        or repair_request.get("deadline_monotonic_ms") != safety_request.get("deadline_monotonic_ms")
        or repair_request.get("deadline_epoch_ms") != safety_request.get("deadline_epoch_ms")
        or int(repair_request.get("remaining_time_ms")) > int(safety_request.get("remaining_time_ms"))
    ):
        raise ReleaseGateError(f"{label} repair broke revision or deadline continuity")
    candidate = _mapping(safety_request.get("candidate"), f"{label} raw candidate")
    for key in ("candidate_identity", "order_index", "order_key"):
        if repair_request.get(key) != candidate.get(key):
            raise ReleaseGateError(f"{label} repair changed candidate field {key!r}")
    raw_child = _list(candidate.get("child_pv"), f"{label} raw child PV")
    if len(raw_child) != 4:
        raise ReleaseGateError(f"{label} raw child PV is not the D5 horizon")
    normalized_child = [
        _validate_series(item, f"{label} raw child PV {index}")
        for index, item in enumerate(raw_child)
    ]
    newest = _mapping(
        _list(repair_request.get("horizon_proofs"), f"{label} repair proofs")[-1],
        f"{label} newest proof",
    )
    rooted_path = _list(newest.get("rooted_path"), f"{label} newest rooted path")
    if (
        len(rooted_path) != len(normalized_child) + 1
        or _mapping(rooted_path[0], f"{label} rooted first").get("machine_notation") != expected_root
        or rooted_path[1:] != normalized_child
        or candidate.get("root_series") != normalized_child[-1]
        or rooted_path[-1] != safety_request.get("candidate", {}).get("root_series")
    ):
        raise ReleaseGateError(f"{label} retained proof is not the raw losing rooted path")
    root_replay = _mapping(safety_request.get("authoritative_root_replay"), f"{label} root replay")
    if (
        root_replay.get("boundary_state") != _mapping(rooted_path[-2], f"{label} penultimate series").get("child_boundary")
        or _mapping(rooted_path[-1], f"{label} final rooted series").get("child_boundary")
        != safety_request.get("authoritative_child_boundary")
    ):
        raise ReleaseGateError(f"{label} retained proof crossed an unrelated boundary")
    raw_mate = _mapping(safety_response.get("reply_mate"), f"{label} raw mate")
    checked = _mapping(raw_mate.get("checked_prefix"), f"{label} checked mate")
    proof_mate = _mapping(newest.get("mate_reply"), f"{label} retained mate")
    if (
        proof_mate.get("moves") != raw_mate.get("moves")
        or proof_mate.get("machine_notation") != raw_mate.get("machine_notation")
        or proof_mate.get("transposition_count") != 1
        or proof_mate.get("outcome") != raw_mate.get("outcome")
        or proof_mate.get("ended_by_check") != raw_mate.get("ended_by_check")
        or proof_mate.get("child_boundary") != checked.get("next_state")
        or repair_response.get("root_series", {}).get("machine_notation") != expected_root
    ):
        raise ReleaseGateError(f"{label} repair did not preserve the exact raw mate proof")


def _load_receipt(label: str, path: Path) -> Receipt:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseGateError(f"could not read {label} receipt: {error}") from error
    return Receipt(
        label=label,
        path=path.resolve(),
        raw=raw,
        payload=_mapping(payload, f"{label} receipt"),
        sha256=_sha256_bytes(raw),
    )


def _run_git(repository: Path, *arguments: str, text: bool = True) -> Any:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=text,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseGateError(f"git {' '.join(arguments)} failed: {error}") from error
    return completed.stdout


def _relative_to(path: Path, parent: Path, label: str) -> Path:
    try:
        return path.resolve().relative_to(parent.resolve())
    except ValueError as error:
        raise ReleaseGateError(f"{label} must stay within {parent}") from error


def _safe_receipt_path(value: object, label: str) -> PurePosixPath:
    text = _text(value, label)
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseGateError(f"{label} is not a safe relative path")
    return path


def _validate_record(
    record: object,
    *,
    base: Path,
    label: str,
) -> tuple[dict[str, object], Path]:
    item = _mapping(record, label)
    if set(item) != {"path", "sha256", "bytes"}:
        raise ReleaseGateError(f"{label} must exactly contain path, sha256, and bytes")
    relative = _safe_receipt_path(item.get("path"), f"{label} path")
    digest = item.get("sha256")
    if not isinstance(digest, str) or not HEX_64.fullmatch(digest):
        raise ReleaseGateError(f"{label} has an invalid SHA-256")
    size = _integer(item.get("bytes"), f"{label} bytes", 0)
    path = base.joinpath(*relative.parts).resolve()
    _relative_to(path, base, label)
    if not path.is_file():
        raise ReleaseGateError(f"{label} file is missing: {path}")
    if path.stat().st_size != size or _sha256_file(path) != digest:
        raise ReleaseGateError(f"{label} file bytes do not match the receipt")
    return {"path": relative.as_posix(), "sha256": digest, "bytes": size}, path


def _artifact_identity(build: Mapping[str, Any]) -> dict[str, Any]:
    identity = {key: build.get(key) for key in ARTIFACT_IDENTITY_FIELDS}
    revision = identity["source_revision"]
    if not isinstance(revision, str) or not GIT_REVISION.fullmatch(revision):
        raise ReleaseGateError("build receipt has an invalid source revision")
    fingerprint = identity["source_fingerprint"]
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{16}", fingerprint):
        raise ReleaseGateError("build receipt has an invalid source fingerprint")
    for key in (
        "kernel_sha256",
        "wasm_sha256",
        "module_js_sha256",
        "artifact_set_sha256",
    ):
        value = identity[key]
        if not isinstance(value, str) or not HEX_64.fullmatch(value):
            raise ReleaseGateError(f"build receipt has an invalid {key}")
    return identity


def _require_identity(
    value: object,
    expected: Mapping[str, Any],
    label: str,
    *,
    fields: Sequence[str] = ARTIFACT_IDENTITY_FIELDS,
) -> Mapping[str, Any]:
    subject = _mapping(value, f"{label} artifact identity")
    for key in fields:
        if subject.get(key) != expected[key]:
            raise ReleaseGateError(
                f"{label} artifact identity {key!r} does not match the build"
            )
    return subject


def _runtime_identity(build: Mapping[str, Any]) -> dict[str, Any]:
    optimization = _mapping(build.get("optimization"), "build optimization")
    if optimization.get("level") != "O3" or optimization.get("lto") is not True:
        raise ReleaseGateError("release WASM must be an O3 LTO build")
    exception_strategy = optimization.get("exception_strategy")
    if exception_strategy not in {"emscripten", "wasm"}:
        raise ReleaseGateError("build exception strategy is unsupported")
    expected_exception_flag = (
        "-fwasm-exceptions" if exception_strategy == "wasm" else "-fexceptions"
    )
    if optimization.get("exception_flag") != expected_exception_flag:
        raise ReleaseGateError("build exception flag disagrees with its strategy")
    wasm_simd = optimization.get("wasm_simd")
    if not isinstance(wasm_simd, bool):
        raise ReleaseGateError("build wasm_simd must be boolean")
    expected_simd_flag = "-msimd128" if wasm_simd else None
    if optimization.get("simd_flag") != expected_simd_flag:
        raise ReleaseGateError("build SIMD flag disagrees with wasm_simd")
    allocator = optimization.get("allocator")
    if allocator not in {"dlmalloc", "emmalloc"}:
        raise ReleaseGateError("build allocator is unsupported")
    return {
        "exception_strategy": exception_strategy,
        "wasm_simd": wasm_simd,
        "allocator": allocator,
    }


def _validate_memory(build: Mapping[str, Any]) -> tuple[dict[str, int | bool], dict[str, Any]]:
    full = dict(_mapping(build.get("memory_envelope"), "build memory envelope"))
    if set(full) != {
        "initial_bytes",
        "estimated_peak_bytes",
        "maximum_bytes",
        "growth_enabled",
        "stack_bytes",
        "hard_maximum_linked",
        "runtime_peak_verified",
    }:
        raise ReleaseGateError("build memory envelope has unknown or missing fields")
    normalized = bundle_builder.validate_memory_limits(
        {
            key: full[key]
            for key in (
                "initial_bytes",
                "maximum_bytes",
                "estimated_peak_bytes",
                "growth_enabled",
            )
        }
    )
    stack = _integer(full.get("stack_bytes"), "build stack bytes", 1)
    if stack % 65_536 or stack > int(normalized["initial_bytes"]):
        raise ReleaseGateError("build stack must be 64KiB-aligned within initial memory")
    if full.get("hard_maximum_linked") is not True:
        raise ReleaseGateError("build did not link a hard maximum memory")
    if full.get("runtime_peak_verified") is not False:
        raise ReleaseGateError("build-stage runtime_peak_verified must remain false")
    return normalized, full


def _validate_source_checkout(
    repository: Path,
    source_package: Path,
    build: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    repository = repository.resolve()
    source_package = source_package.resolve()
    relative_package = _relative_to(source_package, repository, "source package")
    head = str(_run_git(repository, "rev-parse", "HEAD")).strip()
    if build.get("source_revision") != head:
        raise ReleaseGateError("build source revision is not the checked-out HEAD")
    status = _run_git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if str(status).strip():
        raise ReleaseGateError("release checkout is dirty or contains untracked inputs")
    source_paths = sorted(
        (
            path
            for pattern in ("*.py", "*.cpp", "*.hpp", "*.h")
            for path in source_package.rglob(pattern)
        ),
        key=lambda item: item.relative_to(source_package).as_posix(),
    )
    if not source_paths:
        raise ReleaseGateError("engine source package has no fingerprintable inputs")
    tracked_raw = _run_git(repository, "ls-files", "-z", "--", relative_package.as_posix(), text=False)
    tracked = {
        item.decode("utf-8").replace("\\", "/")
        for item in tracked_raw.split(b"\0")
        if item
    }
    relative_sources = {
        path.relative_to(repository).as_posix() for path in source_paths
    }
    missing_tracked = sorted(relative_sources - tracked)
    if missing_tracked:
        raise ReleaseGateError(
            f"engine fingerprint includes untracked inputs: {missing_tracked}"
        )
    _validate_kernel_source_bytes(repository, head)
    calculated_fingerprint = bundle_builder.engine_source_fingerprint(source_package)
    if build.get("source_fingerprint") != calculated_fingerprint:
        raise ReleaseGateError("build source fingerprint does not match the checkout")

    inputs = _list(build.get("source_inputs"), "build source inputs")
    records: list[dict[str, object]] = []
    record_paths: list[str] = []
    for index, value in enumerate(inputs):
        record, _ = _validate_record(
            value,
            base=repository,
            label=f"build source input {index}",
        )
        records.append(record)
        record_paths.append(str(record["path"]))
    expected_paths = sorted(KERNEL_SOURCES)
    if record_paths != expected_paths:
        raise ReleaseGateError("build source inputs are not the canonical kernel closure")
    if _canonical_sha256(records) != build.get("kernel_sha256"):
        raise ReleaseGateError("kernel source-set digest does not match the build")
    missing = [path for path in CLOSURE_SOURCES if not (repository / path).is_file()]
    untracked = [path for path in CLOSURE_SOURCES if path not in tracked]
    if missing or untracked:
        raise ReleaseGateError("WASM dependency closure is incomplete or untracked")
    closure = {
        "schema": "spc-wasm-dependency-closure-v2",
        "target": "ordinary-worker-root-session-prefix-mate",
        "ok": True,
        "source_revision": head,
        "required": list(CLOSURE_SOURCES),
        "missing_from_worktree": [],
        "missing_from_clean_checkout": [],
    }
    return calculated_fingerprint, closure


def _validate_kernel_source_bytes(repository: Path, source_revision: str) -> None:
    for relative in sorted(KERNEL_SOURCES):
        checkout_bytes = (repository / relative).read_bytes()
        revision_bytes = _run_git(
            repository,
            "show",
            f"{source_revision}:{relative}",
            text=False,
        )
        if checkout_bytes != revision_bytes:
            raise ReleaseGateError(
                "native WASM source bytes differ from the source revision Git blob: "
                f"{relative}"
            )


def _validate_build_receipt(
    receipt: Receipt,
    *,
    repository: Path,
    source_package: Path,
) -> BuildEvidence:
    build = receipt.payload
    if build.get("schema") != BUILD_SCHEMA:
        raise ReleaseGateError("unexpected root-session build receipt schema")
    if build.get("status") != "built-not-certified":
        raise ReleaseGateError("build receipt is not in built-not-certified state")
    _false(build, "product_publishable", "build")
    if build.get("certificate_id") is not None:
        raise ReleaseGateError("build receipt must not arrive pre-certified")
    identity = _artifact_identity(build)
    runtime_identity = _runtime_identity(build)
    memory, full_memory = _validate_memory(build)
    source_fingerprint, closure = _validate_source_checkout(
        repository,
        source_package,
        build,
    )

    if (
        build.get("runtime_variant") != "single"
        or build.get("thread_count") != 1
        or build.get("pthreads") is not False
        or build.get("support_files") != []
    ):
        raise ReleaseGateError("only the single-thread ordinary-Worker build can ship")
    expected_runtime = {
        "ordinary_module_worker": True,
        "pthreads": False,
        "cross_origin_isolated": False,
        "native_wasm_exception_handling": runtime_identity["exception_strategy"] == "wasm",
        "wasm_simd": runtime_identity["wasm_simd"],
    }
    runtime_requirements = dict(
        _mapping(build.get("runtime_requirements"), "build runtime requirements")
    )
    if runtime_requirements != expected_runtime:
        raise ReleaseGateError("build runtime requirements are internally inconsistent")
    abi = _mapping(build.get("abi"), "build ABI")
    if (
        abi.get("root_session_version") != 2
        or abi.get("prefix_kernel_version") != 1
        or abi.get("series_mate_version") != 1
        or abi.get("exports") != list(EXPORTED_FUNCTIONS)
        or abi.get("reply_mate_safety") is not False
        or abi.get("canonical_root_tactical_policy")
        != "canonical-boundary-policy-v1"
        or abi.get("legacy_root_tactical_protection") is not False
    ):
        raise ReleaseGateError("build does not carry the combined root/prefix/mate ABI")

    records = _list(build.get("artifacts"), "build artifacts")
    if len(records) != 2:
        raise ReleaseGateError("combined build must contain exactly module and WASM artifacts")
    normalized_records: list[dict[str, object]] = []
    paths: list[Path] = []
    for index, value in enumerate(records):
        record, path = _validate_record(
            value,
            base=receipt.path.parent,
            label=f"build artifact {index}",
        )
        normalized_records.append(record)
        paths.append(path)
    if normalized_records != sorted(normalized_records, key=lambda item: str(item["path"])):
        raise ReleaseGateError("build artifact records are not in canonical order")
    if _canonical_sha256(normalized_records) != identity["artifact_set_sha256"]:
        raise ReleaseGateError("build artifact-set digest is invalid")
    wasm_matches = [path for path in paths if _sha256_file(path) == identity["wasm_sha256"]]
    module_matches = [
        path for path in paths if _sha256_file(path) == identity["module_js_sha256"]
    ]
    if len(wasm_matches) != 1 or len(module_matches) != 1 or wasm_matches[0] == module_matches[0]:
        raise ReleaseGateError("build artifact hashes do not identify one module and one WASM")
    wasm = wasm_matches[0]
    module_js = module_matches[0]
    if wasm.suffix != ".wasm" or module_js.suffix not in {".js", ".mjs"}:
        raise ReleaseGateError("build artifact extensions do not match their roles")

    toolchain_raw = _mapping(build.get("toolchain"), "build toolchain")
    if set(toolchain_raw) != {"path", "sha256", "version"}:
        raise ReleaseGateError("build toolchain subject has unknown or missing fields")
    compiler = Path(_text(toolchain_raw.get("path"), "toolchain path")).resolve()
    compiler_digest = toolchain_raw.get("sha256")
    if (
        not compiler.is_file()
        or not isinstance(compiler_digest, str)
        or not HEX_64.fullmatch(compiler_digest)
        or _sha256_file(compiler) != compiler_digest
    ):
        raise ReleaseGateError("build toolchain executable is missing or changed")
    try:
        compiler_version = subprocess.run(
            [str(compiler), "--version"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseGateError(f"could not re-identify the build compiler: {error}") from error
    toolchain = {
        "path": str(compiler),
        "sha256": compiler_digest,
        "version": _text(toolchain_raw.get("version"), "toolchain version"),
    }
    if toolchain["version"] != compiler_version:
        raise ReleaseGateError("build compiler version output changed from its receipt")

    command = _list(build.get("command"), "build command")
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise ReleaseGateError("build command must be a non-empty string array")
    if Path(str(command[0])).resolve() != compiler:
        raise ReleaseGateError("build command compiler differs from the toolchain subject")
    expected_exception_flag = (
        "-fwasm-exceptions"
        if runtime_identity["exception_strategy"] == "wasm"
        else "-fexceptions"
    )
    required_flags = {
        "-std=c++20",
        "-O3",
        "-flto",
        expected_exception_flag,
        "-DSPC_NATIVE_CORE_ONLY=1",
        "-DSPC_NATIVE_MATE_CORE_ONLY=1",
        "-sALLOW_MEMORY_GROWTH=1",
        f"-sINITIAL_MEMORY={full_memory['initial_bytes']}",
        f"-sMAXIMUM_MEMORY={full_memory['maximum_bytes']}",
        f"-sSTACK_SIZE={full_memory['stack_bytes']}",
        "-sABORTING_MALLOC=0",
        f"-sMALLOC={runtime_identity['allocator']}",
        "-sUSE_PTHREADS=0",
        "-sWASM_WORKERS=0",
        "-sENVIRONMENT=worker,node",
        "-sMODULARIZE=1",
        "-sEXPORT_ES6=1",
        "-sFILESYSTEM=0",
        "-sDYNAMIC_EXECUTION=0",
        f"-sEXPORTED_FUNCTIONS={','.join(EXPORTED_FUNCTIONS)}",
        "-sEXPORTED_RUNTIME_METHODS=UTF8ToString,stringToNewUTF8,HEAPU8",
    }
    missing_flags = sorted(required_flags - set(command))
    if missing_flags:
        raise ReleaseGateError(f"build command omits required flags: {missing_flags}")
    if ("-msimd128" in command) is not bool(runtime_identity["wasm_simd"]):
        raise ReleaseGateError("build command SIMD flag differs from its runtime identity")
    compiled_sources = {
        str((source_package / name).resolve())
        for name in (
            "_native_eval.cpp",
            "native_subtree.cpp",
            "native_subtree_wasm.cpp",
            "native_root_session_wasm.cpp",
            "_native_mate.cpp",
        )
    }
    command_paths = {
        str(Path(item).resolve())
        for item in command
        if item.lower().endswith((".cpp", ".cc", ".cxx"))
    }
    if command_paths != compiled_sources:
        raise ReleaseGateError("build command compiled-source closure is incomplete or expanded")
    if command.count("-I") != 1:
        raise ReleaseGateError("build command must carry one source include path")
    include_index = command.index("-I")
    if include_index + 1 >= len(command) or Path(command[include_index + 1]).resolve() != source_package.resolve():
        raise ReleaseGateError("build command include path differs from the engine package")
    if command.count("-o") != 1:
        raise ReleaseGateError("build command must carry one output module path")
    output_index = command.index("-o")
    if output_index + 1 >= len(command) or Path(command[output_index + 1]).resolve() != module_js:
        raise ReleaseGateError("build command output path differs from the verified module")

    expected_command = [
        str(compiler),
        *(
            str((source_package / name).resolve())
            for name in (
                "_native_eval.cpp",
                "native_subtree.cpp",
                "native_subtree_wasm.cpp",
                "native_root_session_wasm.cpp",
                "_native_mate.cpp",
            )
        ),
        "-I",
        str(source_package.resolve()),
        "-std=c++20",
        "-O3",
        "-flto",
        expected_exception_flag,
        "-DSPC_NATIVE_CORE_ONLY=1",
        "-DSPC_NATIVE_MATE_CORE_ONLY=1",
        "-sALLOW_MEMORY_GROWTH=1",
        f"-sINITIAL_MEMORY={full_memory['initial_bytes']}",
        f"-sMAXIMUM_MEMORY={full_memory['maximum_bytes']}",
        f"-sSTACK_SIZE={full_memory['stack_bytes']}",
        "-sABORTING_MALLOC=0",
        f"-sMALLOC={runtime_identity['allocator']}",
        "-sUSE_PTHREADS=0",
        "-sWASM_WORKERS=0",
        "-sENVIRONMENT=worker,node",
        "-sMODULARIZE=1",
        "-sEXPORT_ES6=1",
        "-sFILESYSTEM=0",
        "-sDYNAMIC_EXECUTION=0",
        f"-sEXPORTED_FUNCTIONS={','.join(EXPORTED_FUNCTIONS)}",
        "-sEXPORTED_RUNTIME_METHODS=UTF8ToString,stringToNewUTF8,HEAPU8",
        "-o",
        str(module_js),
    ]
    if runtime_identity["wasm_simd"]:
        expected_command.insert(
            expected_command.index(expected_exception_flag) + 1,
            "-msimd128",
        )
    normalized_command = list(command)
    path_indexes = set(range(0, 6)) | {7, len(normalized_command) - 1}
    for index in path_indexes:
        if 0 <= index < len(normalized_command):
            normalized_command[index] = str(Path(normalized_command[index]).resolve())
    if normalized_command != expected_command:
        raise ReleaseGateError("build command is not the exact canonical builder invocation")

    engine = {
        "engine_version": _text(build.get("engine_version"), "engine version"),
        "ruleset_version": _text(build.get("ruleset_version"), "ruleset version"),
        "profile_id": _text(build.get("profile_id"), "profile id"),
    }
    return BuildEvidence(
        receipt=receipt,
        identity=identity,
        runtime_identity=runtime_identity,
        runtime_requirements=runtime_requirements,
        memory=memory,
        full_memory=full_memory,
        engine=engine,
        toolchain=toolchain,
        wasm=wasm,
        module_js=module_js,
        source_fingerprint=source_fingerprint,
        dependency_closure=closure,
    )


def _validate_root_smoke(
    receipt: Receipt,
    build: BuildEvidence,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    payload = receipt.payload
    if payload.get("schema") != ROOT_SMOKE_SCHEMA or payload.get("status") != "passed-not-certified":
        raise ReleaseGateError("root smoke receipt did not pass its verifier")
    _false(payload, "product_publishable", "root smoke")
    _false(payload, "safety_certified", "root smoke")
    if payload.get("certificate_id") is not None:
        raise ReleaseGateError("root smoke receipt must not be pre-certified")
    _require_identity(payload, build.identity, "root smoke")
    for key in RUNTIME_IDENTITY_FIELDS:
        if payload.get(key) != build.runtime_identity[key]:
            raise ReleaseGateError(f"root smoke runtime identity {key!r} drifted")
    if payload.get("runtime_requirements") != build.runtime_requirements:
        raise ReleaseGateError("root smoke runtime requirements drifted")
    if payload.get("runtime_variant") != "single" or payload.get("thread_count") != 1:
        raise ReleaseGateError("root smoke did not execute the single-thread lane")
    memory = _mapping(payload.get("memory"), "root smoke memory")
    if memory.get("configured") != build.full_memory:
        raise ReleaseGateError("root smoke memory configuration drifted from the build")
    maximum = int(build.memory["maximum_bytes"])
    for key in ("observed_bytes", "native_peak_bytes"):
        value = _integer(memory.get(key), f"root smoke memory {key}", 1)
        if value > maximum:
            raise ReleaseGateError(f"root smoke {key} exceeded the hard memory maximum")
    gates = _mapping(payload.get("gates"), "root smoke gates")
    for key in (
        "combined_exports",
        "root_contract_reply_mate_safety_false",
        "persistent_d1_d2_session",
        "aspiration_fail_soft_window",
        "aspiration_fail_high_low_white_black",
        "selected_owner_warm_exact_certification",
        "checked_horizon_proof_research",
        "checked_horizon_newest_proof_hit",
        "cumulative_work_and_cache_receipts",
        "exact_manifest_import",
        "configured_max_depth_rejected",
        "work_limit_fail_closed",
        "deadline_fail_closed",
        "prefix_smoke",
        "mate_found_exhausted_unknown",
        "canonical_root_tactical_policy",
        "legacy_root_tactical_policy_rejected",
        "canonical_root_tactical_boundary_echoes",
    ):
        _true(gates, key, "root smoke")
    contract = dict(_mapping(payload.get("root_session_contract"), "root session contract"))
    capabilities = _mapping(contract.get("capabilities"), "root session capabilities")
    request_schemas = _mapping(contract.get("request_schemas"), "root session request schemas")
    result_schemas = _mapping(contract.get("result_schemas"), "root session result schemas")
    hard_limits = _mapping(contract.get("hard_limits"), "root session hard limits")
    horizon_research = _mapping(contract.get("horizon_research"), "root session horizon policy")
    if (
        capabilities.get("canonical_root_tactical_policy") is not True
        or capabilities.get("aspiration_windows") is not True
        or capabilities.get("checked_horizon_proof_research") is not True
    ):
        raise ReleaseGateError("root session contract lacks coordinator search capabilities")
    if (
        hard_limits.get("root_tactical_policy") != "canonical-boundary-policy-v1"
        or hard_limits.get("root_tactical_protection_values") != [False]
        or hard_limits.get("minimum_aspiration_initial_delta") != 2_048
        or hard_limits.get("maximum_aspiration_attempts") != 4
        or hard_limits.get("maximum_horizon_proofs") != 16
        or hard_limits.get("maximum_horizon_proof_path") != 8
    ):
        raise ReleaseGateError("root session contract hard limits are not release-certified")
    if (
        request_schemas.get("search") != "spc-root-candidate-task-v1"
        or result_schemas.get("search") != "spc-root-candidate-result-v1"
        or request_schemas.get("horizon_research")
        != "spc-root-horizon-research-task-v1"
        or result_schemas.get("horizon_research")
        != "spc-root-horizon-research-result-v1"
        or dict(horizon_research)
        != {
            "task_schema": "spc-root-horizon-research-task-v1",
            "result_schema": "spc-root-horizon-research-result-v1",
            "proof_schema": "spc-retained-root-horizon-proof-v1",
            "purpose": "horizon-research",
            "full_window": True,
            "tt_persistence": "commit",
            "hit_mask_order": "request-order",
            "warm_exact_zero_hit_allowed": True,
        }
    ):
        raise ReleaseGateError("root session checked-horizon policy is not release-certified")
    checked_horizon = _validate_checked_horizon_evidence(
        payload.get("checked_horizon_proof_research"),
        label="root smoke checked-horizon evidence",
    )
    manifest_contract = _mapping(contract.get("manifest"), "root session manifest contract")
    if manifest_contract.get("root_tactical_policy") != "canonical-boundary-policy-v1":
        raise ReleaseGateError("root session manifest omits the canonical tactical policy")
    prefix_raw = _mapping(payload.get("prefix_contract"), "prefix contract")
    hard_limits = prefix_raw.get("hard_limits", prefix_raw.get("limits"))
    prefix_contract = {
        "schema": prefix_raw.get("schema"),
        "result_schema": prefix_raw.get("result_schema"),
        "abi_version": prefix_raw.get("abi_version"),
        "chess960": prefix_raw.get("chess960"),
        "promoted_hex_required_for_product": prefix_raw.get(
            "promoted_hex_required_for_product"
        ),
        "limits": dict(_mapping(hard_limits, "prefix contract hard limits")),
    }
    bundle_builder.validate_prefix_contract(prefix_contract)
    mate_receipts = _mapping(payload.get("mate_receipts"), "root smoke mate receipts")
    expected_mate = {
        "found": ("found", "found", True),
        "exhausted": ("exhausted", "exhausted", True),
        "work_limit": ("work_limit", "unknown", False),
        "deadline": ("deadline", "unknown", False),
    }
    for key, (kernel_status, proof_status, complete) in expected_mate.items():
        item = _mapping(mate_receipts.get(key), f"root smoke mate {key}")
        if (
            item.get("kernel_status") != kernel_status
            or item.get("proof_status") != proof_status
            or item.get("complete") is not complete
        ):
            raise ReleaseGateError(f"root smoke mate {key} receipt is not fail-closed")
        stats = _mapping(item.get("stats"), f"root smoke mate {key} stats")
        _integer(stats.get("positions_visited"), f"root smoke mate {key} positions")
        _integer(stats.get("moves_generated"), f"root smoke mate {key} moves")
    return contract, prefix_contract, checked_horizon


def _validate_root_parity(
    receipt: Receipt,
    build: BuildEvidence,
    root_contract: Mapping[str, Any],
) -> tuple[int, dict[str, Any], dict[str, Any], list[dict[str, Any]], str, str]:
    payload = receipt.payload
    if payload.get("schema") != ROOT_PARITY_SCHEMA or payload.get("status") != "passed":
        raise ReleaseGateError("root D5 oracle receipt did not pass")
    artifact = _require_identity(payload.get("artifact"), build.identity, "root D5 oracle")
    for key in RUNTIME_IDENTITY_FIELDS:
        if artifact.get(key) != build.runtime_identity[key]:
            raise ReleaseGateError(f"root D5 oracle runtime identity {key!r} drifted")
    oracle_subject_extras = {
        "runtime_variant": "single",
        "thread_count": 1,
        **build.engine,
    }
    for key, expected in oracle_subject_extras.items():
        if artifact.get(key) != expected:
            raise ReleaseGateError(f"root D5 oracle identity {key!r} drifted")
    if payload.get("failures") != 0:
        raise ReleaseGateError("root D5 oracle reports failures")
    differential_cases = _integer(
        payload.get("differential_cases"),
        "root D5 oracle differential cases",
        3,
    )
    boundary = dict(_mapping(payload.get("boundary"), "root D5 oracle boundary"))
    expected_boundary = {
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "series": 1,
        "quiet_series": 0,
        "progressive_ep": [],
        "promoted_hex": "0000000000000000",
        "chess960": False,
    }
    for key, expected in expected_boundary.items():
        if boundary.get(key) != expected:
            raise ReleaseGateError(f"root D5 oracle boundary {key!r} is not the start position")
    config = dict(_mapping(payload.get("session_config"), "root D5 oracle config"))
    if config.get("max_depth") != 5 or config.get("width") != 32:
        raise ReleaseGateError("root oracle session config must be exactly W32 D5")
    if config.get("root_tactical_protection") is not False:
        raise ReleaseGateError("root oracle must use canonical per-boundary tactical protection")
    try:
        normalized_config = bundle_builder._validate_root_session_config(
            config,
            root_contract,
        )
    except ValueError as error:
        raise ReleaseGateError(f"root D5 oracle session config is invalid: {error}") from error
    if config != normalized_config:
        raise ReleaseGateError("root D5 oracle session config is not canonical")
    if "deep_teacher_value_model" in normalized_config:
        raise ReleaseGateError(
            "the current release receipt schema is baseline-only; modeled browser "
            "activation requires a separately certified strength/parity receipt"
        )
    if payload.get("memory") != build.memory:
        raise ReleaseGateError("root D5 oracle memory envelope differs from the build")
    retained_manifest_sha256 = payload.get("retained_manifest_sha256")
    if not isinstance(retained_manifest_sha256, str) or not HEX_64.fullmatch(
        retained_manifest_sha256
    ):
        raise ReleaseGateError("root D5 oracle lacks a retained-manifest digest")

    selected = dict(_mapping(payload.get("selected"), "root D5 oracle selection"))
    _text(selected.get("candidate_identity"), "root D5 oracle candidate identity")
    move = _text(selected.get("move"), "root D5 oracle move")
    _integer(selected.get("score"), "root D5 oracle score", -2_000_000_000)
    proof = _list(selected.get("proof_bounds"), "root D5 oracle proof bounds")
    if len(proof) != 2 or any(isinstance(item, bool) or not isinstance(item, int) for item in proof):
        raise ReleaseGateError("root D5 oracle proof bounds are invalid")
    principal_variation = _list(
        selected.get("principal_variation"),
        "root D5 oracle principal variation",
    )
    if not principal_variation:
        raise ReleaseGateError("root D5 oracle must retain the full principal variation")
    first_series = _mapping(principal_variation[0], "root D5 oracle first PV series")
    if first_series.get("machine_notation") != move:
        raise ReleaseGateError("root D5 oracle move differs from its full principal variation")
    pv_sha256 = selected.get("principal_variation_sha256")
    if pv_sha256 != _canonical_sha256(principal_variation):
        raise ReleaseGateError("root D5 oracle principal-variation digest is invalid")

    rivals = dict(_mapping(payload.get("rival_bounds"), "root D5 oracle rival bounds"))
    if rivals.get("coverage_complete") is not True or rivals.get("candidate_count") != 20:
        raise ReleaseGateError("root D5 oracle does not cover all 20 start candidates")
    if rivals.get("unknown_count") != 0:
        raise ReleaseGateError("root D5 oracle contains an Unknown rival bound")
    bounds = _list(rivals.get("bounds"), "root D5 oracle bounds")
    if len(bounds) != 20:
        raise ReleaseGateError("root D5 oracle must retain exactly 20 candidate bounds")
    normalized_bound_ids: list[str] = []
    bound_counts = {"exact": 0, "lower": 0, "upper": 0}
    for raw_bound in bounds:
        bound = _mapping(raw_bound, "root D5 oracle candidate bound")
        candidate_identity = _text(
            bound.get("candidate_identity"),
            "root D5 oracle rival candidate identity",
        )
        normalized_bound_ids.append(candidate_identity)
        bound_type = bound.get("bound")
        if bound_type not in bound_counts:
            raise ReleaseGateError("root D5 oracle contains an invalid candidate bound")
        bound_counts[str(bound_type)] += 1
        _integer(bound.get("score"), "root D5 oracle rival score", -2_000_000_000)
        rival_proof = _list(bound.get("proof_bounds"), "root D5 oracle rival proof bounds")
        if len(rival_proof) != 2 or any(
            isinstance(item, bool) or not isinstance(item, int) for item in rival_proof
        ):
            raise ReleaseGateError("root D5 oracle rival proof bounds are invalid")
    if len(set(normalized_bound_ids)) != 20 or normalized_bound_ids != sorted(normalized_bound_ids):
        raise ReleaseGateError("root D5 oracle bounds must be unique and canonical by identity")
    for key, count in bound_counts.items():
        if rivals.get(f"{key}_count") != count:
            raise ReleaseGateError(f"root D5 oracle {key} bound count is inconsistent")
    if rivals.get("coverage_sha256") != _canonical_sha256(bounds):
        raise ReleaseGateError("root D5 oracle rival coverage digest is invalid")

    work = _mapping(payload.get("work"), "root D5 oracle work")
    if (
        work.get("status") != "complete"
        or work.get("within_cap") is not True
        or work.get("unknown_or_limit_count") != 0
        or work.get("max_work") != config.get("max_work")
    ):
        raise ReleaseGateError("root D5 oracle work proof is incomplete or limited")
    accounted_work = _integer(work.get("accounted_work"), "root D5 oracle accounted work")
    if accounted_work > _integer(config.get("max_work"), "root D5 oracle max work", 1):
        raise ReleaseGateError("root D5 oracle exceeded its work cap")
    deadline = _mapping(payload.get("deadline"), "root D5 oracle deadline")
    if (
        deadline.get("status") != "complete"
        or deadline.get("deadline_reached") is not False
        or deadline.get("unknown_or_limit_count") != 0
    ):
        raise ReleaseGateError("root D5 oracle deadline proof is incomplete or limited")
    deadline_limit_ms = _number(
        deadline.get("deadline_limit_ms"),
        "root D5 oracle deadline limit",
        0.000001,
    )
    _number(deadline.get("remaining_time_ms"), "root D5 oracle remaining time")

    gates = _mapping(payload.get("gates"), "root D5 oracle gates")
    for key in (
        "initial_root_enumeration_python_parity",
        "persistent_d1_d2_python_parity",
        "persistent_d1_through_d5_selects_same_result_as_fresh_d5",
        "exact_selected_replay",
        "work_receipts",
        "deadline_receipts",
        "complete_rival_bound_coverage",
    ):
        _true(gates, key, "root D5 oracle")

    semantic = {
        "schema": ROOT_PARITY_SCHEMA,
        "artifact": {
            key: artifact[key]
            for key in (
                ARTIFACT_IDENTITY_FIELDS
                + RUNTIME_IDENTITY_FIELDS
                + tuple(oracle_subject_extras)
            )
        },
        "boundary": boundary,
        "session_config": config,
        "memory": build.memory,
        "deadline": {"deadline_limit_ms": deadline_limit_ms},
        "retained_manifest_sha256": retained_manifest_sha256,
        "selected": selected,
        "rival_bounds": rivals,
    }
    oracle_signature = payload.get("oracle_signature_sha256")
    if oracle_signature != _canonical_sha256(semantic):
        raise ReleaseGateError("root D5 oracle semantic signature is invalid")
    return (
        differential_cases,
        config,
        selected,
        [dict(_mapping(item, "root D5 oracle candidate bound")) for item in bounds],
        str(retained_manifest_sha256),
        str(oracle_signature),
    )


def _validate_prefix_parity(receipt: Receipt, build: BuildEvidence) -> int:
    payload = receipt.payload
    if payload.get("schema") != PREFIX_PARITY_SCHEMA or payload.get("status") != "passed":
        raise ReleaseGateError("prefix parity receipt did not pass")
    _require_identity(payload.get("artifact"), build.identity, "prefix parity")
    if payload.get("failures") != 0:
        raise ReleaseGateError("prefix parity receipt reports failures")
    cases = _list(payload.get("cases"), "prefix parity cases")
    if len(cases) < bundle_builder.MIN_PREFIX_DIFFERENTIAL_CASES:
        raise ReleaseGateError("prefix parity receipt has too few cases")
    names: set[str] = set()
    input_hashes: set[str] = set()
    for raw_case in cases:
        case = _mapping(raw_case, "prefix parity case")
        name = _text(case.get("name"), "prefix parity case name")
        if name in names:
            raise ReleaseGateError("prefix parity receipt duplicates a case")
        names.add(name)
        for key in ("input_sha256", "wasm_output_sha256", "oracle_output_sha256"):
            value = case.get(key)
            if not isinstance(value, str) or not HEX_64.fullmatch(value):
                raise ReleaseGateError(f"prefix parity case {name!r} has an invalid {key}")
        if (
            case.get("exact_match") is not True
            or case.get("wasm_output_sha256") != case.get("oracle_output_sha256")
        ):
            raise ReleaseGateError(f"prefix parity case {name!r} is not an exact match")
        input_hash = str(case["input_sha256"])
        if input_hash in input_hashes:
            raise ReleaseGateError("prefix parity receipt duplicates a case input")
        input_hashes.add(input_hash)
    if payload.get("case_set_sha256") != _canonical_sha256(cases):
        raise ReleaseGateError("prefix parity case-set digest is invalid")
    gates = _mapping(payload.get("gates"), "prefix parity gates")
    for key in (
        "exact_python_parity",
        "compiled_prefix_replay",
        "multi_ep_san",
        "illegal_prefix_fail_closed",
        "case_input_output_hashes",
    ):
        _true(gates, key, "prefix parity")
    if (
        payload.get("progressive_san_corrections") != 0
        or _integer(payload.get("progressive_san_exact_parity"), "prefix SAN parity", 1) < 2
        or _integer(payload.get("fail_closed_errors"), "prefix fail-closed cases", 1) < 3
        or payload.get("mate_replay") != "checkmate"
        or payload.get("multi_ep") != "covered"
    ):
        raise ReleaseGateError("prefix parity receipt lacks SAN, mate, or multi-EP proof")
    return len(cases)


def _validate_browser_prefix(receipt: Receipt, build: BuildEvidence) -> None:
    payload = receipt.payload
    if payload.get("schema") != BROWSER_PREFIX_SCHEMA or payload.get("status") != "passed":
        raise ReleaseGateError("browser prefix receipt did not pass")
    _require_identity(payload.get("artifact"), build.identity, "browser prefix")
    for key in (
        "exact_identity",
        "chess960_rejected",
        "certified_limits_enforced",
        "full_next_state_enforced",
        "same_series_terminal_covered",
        "final_frame_consistency_enforced",
        "malformed_local_fallback",
        "original_request_preserved",
        "remote_authority_bound",
        "cancellation_fallback_suppressed",
    ):
        _true(payload, key, "browser prefix")


def _validate_mate_parity(receipt: Receipt, build: BuildEvidence) -> int:
    payload = receipt.payload
    if payload.get("schema") != MATE_PARITY_SCHEMA or payload.get("status") != "passed":
        raise ReleaseGateError("mate parity receipt did not pass")
    if payload.get("work_accounting") != (
        "positions-plus-generated-edges-v1"
    ):
        raise ReleaseGateError("mate parity work accounting is not literal")
    _require_identity(payload.get("artifact"), build.identity, "mate parity")
    if payload.get("failures") != 0:
        raise ReleaseGateError("mate parity receipt reports failures")
    cases = _list(payload.get("cases"), "mate parity cases")
    if len(cases) < bundle_builder.MIN_MATE_DIFFERENTIAL_CASES:
        raise ReleaseGateError("mate parity receipt has too few cases")
    names: set[str] = set()
    input_hashes: set[str] = set()
    proof_counts = {"found": 0, "exhausted": 0, "unknown": 0}
    found_sides: set[str] = set()
    for raw_case in cases:
        case = _mapping(raw_case, "mate parity case")
        name = _text(case.get("name"), "mate parity case name")
        if name in names:
            raise ReleaseGateError("mate parity receipt duplicates a case")
        names.add(name)
        for key in ("input_sha256", "wasm_output_sha256", "oracle_output_sha256"):
            value = case.get(key)
            if not isinstance(value, str) or not HEX_64.fullmatch(value):
                raise ReleaseGateError(f"mate parity case {name!r} has an invalid {key}")
        if (
            case.get("exact_match") is not True
            or case.get("wasm_output_sha256") != case.get("oracle_output_sha256")
        ):
            raise ReleaseGateError(f"mate parity case {name!r} is not an exact match")
        input_hash = str(case["input_sha256"])
        if input_hash in input_hashes:
            raise ReleaseGateError("mate parity receipt duplicates a case input")
        input_hashes.add(input_hash)
        proof_status = case.get("proof_status")
        if proof_status not in proof_counts:
            raise ReleaseGateError(f"mate parity case {name!r} has an invalid proof status")
        proof_counts[str(proof_status)] += 1
        side = case.get("side_to_move")
        if side not in {"white", "black"}:
            raise ReleaseGateError(f"mate parity case {name!r} has an invalid side")
        if proof_status == "found":
            found_sides.add(str(side))
    if payload.get("case_set_sha256") != _canonical_sha256(cases):
        raise ReleaseGateError("mate parity case-set digest is invalid")
    if (
        proof_counts["found"] < 2
        or proof_counts["exhausted"] < 1
        or proof_counts["unknown"] < 2
        or found_sides != {"white", "black"}
    ):
        raise ReleaseGateError("mate parity proof-state accounting is incomplete")
    accelerated = _list(
        payload.get("accelerated_cases"),
        "accelerated mate cases",
    )
    expected_accelerated = {
        "s6-staged-root-found": {
            "input": {
                "fen": "rnbNkb1r/pppp2pp/8/5p2/8/3B1P2/PPPP2PP/RNBnK2R b kq - 0 7",
                "series": 6,
                "max_positions": 0,
                "max_work": 25_643,
                "time_limit_ms": 30_000,
            },
            "kernel_status": "found",
            "proof_status": "found",
            "complete": True,
            "moves": [
                "b8c6",
                "c6d4",
                "d1e3",
                "f8d6",
                "d6h2",
                "h2g3",
            ],
            "work": 25_643,
            "checkmates": 1,
            "max_depth_reached": 6,
        },
        "s6-staged-root-cap-minus-one": {
            "input": {
                "fen": "rnbNkb1r/pppp2pp/8/5p2/8/3B1P2/PPPP2PP/RNBnK2R b kq - 0 7",
                "series": 6,
                "max_positions": 0,
                "max_work": 25_642,
                "time_limit_ms": 30_000,
            },
            "kernel_status": "work_limit",
            "proof_status": "unknown",
            "complete": False,
            "moves": [],
            "work": 25_642,
            "checkmates": 0,
            "max_depth_reached": 0,
        },
        "s6-selective-miss-exact-exhausted": {
            "input": {
                "fen": "6bk/8/8/8/8/8/8/K7 b - - 0 1",
                "series": 6,
                "max_positions": 0,
                "max_work": 16_066,
                "time_limit_ms": 30_000,
            },
            "kernel_status": "exhausted",
            "proof_status": "exhausted",
            "complete": True,
            "moves": [],
            "work": 16_066,
            "checkmates": 0,
            "max_depth_reached": 5,
        },
        "s6-selective-miss-exact-cap-minus-one": {
            "input": {
                "fen": "6bk/8/8/8/8/8/8/K7 b - - 0 1",
                "series": 6,
                "max_positions": 0,
                "max_work": 16_065,
                "time_limit_ms": 30_000,
            },
            "kernel_status": "work_limit",
            "proof_status": "unknown",
            "complete": False,
            "moves": [],
            "work": 16_065,
            "checkmates": 0,
            "max_depth_reached": 5,
        },
        "authentic-s8-staged-root-invariant": {
            "input": {
                "fen": "rn1k1bn1/4pp2/5Q2/8/2P5/5P2/3P3P/qNBbK1NR b K - 0 13",
                "series": 8,
                "max_positions": 0,
                "max_work": 1_000_000,
                "time_limit_ms": 30_000,
            },
            "kernel_status": "found",
            "proof_status": "found",
            "complete": True,
            "moves": ["a1d4", "a8a2", "a2d2", "d4f2"],
            "work": 5_474,
            "checkmates": 1,
            "max_depth_reached": 4,
        },
        "s7-staged-root-found": {
            "input": {
                "fen": "rnk3nr/pp3ppp/8/8/8/1Pp1P3/P1PP1PPP/R1b1K1NR w K - 0 13",
                "series": 7,
                "max_positions": 0,
                "max_work": 10_000_000,
                "time_limit_ms": 30_000,
            },
            "kernel_status": "found",
            "proof_status": "found",
            "complete": True,
            "moves": [
                "d2c3",
                "e1e2",
                "g1f3",
                "f3g5",
                "h1d1",
                "g5e6",
                "d1d8",
            ],
            "work": 79_715,
            "checkmates": 1,
            "max_depth_reached": 7,
        },
        "s7-staged-root-work-limit": {
            "input": {
                "fen": "rnk3nr/pp3ppp/8/8/8/1Pp1P3/P1PP1PPP/R1b1K1NR w K - 0 13",
                "series": 7,
                "max_positions": 0,
                "max_work": 10,
                "time_limit_ms": 30_000,
            },
            "kernel_status": "work_limit",
            "proof_status": "unknown",
            "complete": False,
            "moves": [],
            "work": 10,
            "checkmates": 0,
            "max_depth_reached": 0,
        },
        "s7-staged-root-exhausted": {
            "input": {
                "fen": "8/8/8/8/8/2k5/8/K7 w - - 0 1",
                "series": 7,
                "max_positions": 0,
                "max_work": 1_000_000,
                "time_limit_ms": 30_000,
            },
            "kernel_status": "exhausted",
            "proof_status": "exhausted",
            "complete": True,
            "moves": [],
            "work": 836,
            "checkmates": 0,
            "max_depth_reached": 0,
        },
        "s7-nonchecking-stuck-is-not-mate": {
            "input": {
                "fen": "8/8/8/8/8/5k2/6q1/7K w - - 0 1",
                "series": 7,
                "max_positions": 0,
                "max_work": 1_000_000,
                "time_limit_ms": 30_000,
            },
            "kernel_status": "exhausted",
            "proof_status": "exhausted",
            "complete": True,
            "moves": [],
            "work": 1,
            "checkmates": 0,
            "max_depth_reached": 0,
        },
    }
    accelerated_names: set[str] = set()
    for raw_case in accelerated:
        case = _mapping(raw_case, "accelerated mate case")
        name = _text(case.get("name"), "accelerated mate case name")
        if name in accelerated_names:
            raise ReleaseGateError("accelerated mate receipt duplicates a case")
        accelerated_names.add(name)
        expected = expected_accelerated.get(name)
        if expected is None:
            raise ReleaseGateError("accelerated mate receipt has an unknown case")
        for key in ("input_sha256", "wasm_output_sha256"):
            value = case.get(key)
            if not isinstance(value, str) or not HEX_64.fullmatch(value):
                raise ReleaseGateError(
                    f"accelerated mate case {name!r} has an invalid {key}"
                )
        expected_input = _mapping(expected["input"], "expected accelerated input")
        actual_input = _mapping(
            case.get("input"),
            f"accelerated mate case {name!r} input",
        )
        expected_input_sha256 = _canonical_sha256(expected_input)
        if (
            actual_input != expected_input
            or _canonical_sha256(actual_input) != case.get("input_sha256")
            or case.get("input_sha256") != expected_input_sha256
        ):
            raise ReleaseGateError(
                f"accelerated mate case {name!r} input changed"
            )
        if (
            case.get("kernel_status") != expected["kernel_status"]
            or case.get("proof_status") != expected["proof_status"]
            or case.get("complete") is not expected["complete"]
            or _list(case.get("moves"), f"accelerated mate case {name!r} moves")
                != expected["moves"]
            or _integer(case.get("work"), f"accelerated mate case {name!r} work")
                != expected["work"]
            or _integer(
                case.get("checkmates"),
                f"accelerated mate case {name!r} checkmates",
            ) != expected["checkmates"]
            or _integer(
                case.get("max_depth_reached"),
                f"accelerated mate case {name!r} max depth",
            ) != expected["max_depth_reached"]
        ):
            raise ReleaseGateError(
                f"accelerated mate case {name!r} result changed"
            )
    if accelerated_names != set(expected_accelerated):
        raise ReleaseGateError("accelerated mate receipt lacks required cases")
    if payload.get("accelerated_case_set_sha256") != _canonical_sha256(accelerated):
        raise ReleaseGateError("accelerated mate case-set digest is invalid")
    gates = _mapping(payload.get("gates"), "mate parity gates")
    for key in (
        "python_parity",
        "authoritative_replay",
        "white_found",
        "black_found",
        "exhausted",
        "work_limit_unknown",
        "deadline_unknown",
        "signed_mate_distance_overrides",
        "proof_bounds",
        "work_receipts",
        "deadline_receipts",
        "prefix_replay",
        "case_input_output_hashes",
        "late_series_staged_root",
        "series6_staged_root",
        "series6_selective_miss_exact_fallback",
        "series6_budget_and_deadline_unknown",
        "series8_staged_root_invariant",
    ):
        _true(gates, key, "mate parity")
    return len(cases)


def _query_value(query: Mapping[str, list[str]], key: str) -> str:
    values = query.get(key)
    if values is None or len(values) != 1:
        raise ReleaseGateError(f"Opera benchmark URL must bind one {key!r} value")
    return values[0]


def _normalize_opera_bounds(
    value: object,
    *,
    label: str,
    expected_candidate_ids: set[str],
    selected: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_bounds = _list(value, f"{label} rival bounds")
    if len(raw_bounds) != 20:
        raise ReleaseGateError(f"{label} must retain exactly 20 candidate bounds")
    bounds: list[dict[str, Any]] = []
    for raw_bound in raw_bounds:
        bound = _mapping(raw_bound, f"{label} candidate bound")
        candidate_identity = _text(
            bound.get("candidate_identity"),
            f"{label} candidate identity",
        )
        bound_type = bound.get("bound")
        if bound_type not in {"exact", "lower", "upper"}:
            raise ReleaseGateError(f"{label} contains an invalid or Unknown candidate bound")
        score = _integer(bound.get("score"), f"{label} candidate score", -2_000_000_000)
        proof_bounds = _list(bound.get("proof_bounds"), f"{label} candidate proof bounds")
        if len(proof_bounds) != 2 or any(
            isinstance(item, bool) or not isinstance(item, int) for item in proof_bounds
        ):
            raise ReleaseGateError(f"{label} contains invalid candidate proof bounds")
        bounds.append(
            {
                "candidate_identity": candidate_identity,
                "bound": bound_type,
                "score": score,
                "proof_bounds": proof_bounds,
            }
        )
    bounds.sort(key=lambda item: item["candidate_identity"])
    candidate_ids = [item["candidate_identity"] for item in bounds]
    if len(set(candidate_ids)) != 20 or set(candidate_ids) != expected_candidate_ids:
        raise ReleaseGateError(f"{label} does not cover the oracle candidate universe exactly")

    selected_identity = selected.get("candidate_identity")
    selected_bounds = [
        item for item in bounds if item["candidate_identity"] == selected_identity
    ]
    if len(selected_bounds) != 1:
        raise ReleaseGateError(f"{label} does not cover the selected candidate exactly once")
    selected_bound = selected_bounds[0]
    if (
        selected_bound["bound"] != "exact"
        or selected_bound["score"] != selected.get("score")
        or selected_bound["proof_bounds"] != selected.get("proof_bounds")
    ):
        raise ReleaseGateError(f"{label} does not exactly certify the selected candidate")
    for bound in bounds:
        if bound["candidate_identity"] == selected_identity:
            continue
        if bound["bound"] == "lower" or bound["score"] > selected.get("score"):
            raise ReleaseGateError(f"{label} contains a rival bound that does not prove the selection")
    return bounds


def _validate_opera_aspiration_iterations(
    value: object,
    *,
    label: str,
    expected_depths: Sequence[int],
    expected_mode: str,
    expected_candidate_count: int,
) -> list[dict[str, Any]]:
    iterations = _list(value, f"{label} aspiration iterations")
    if len(iterations) != len(expected_depths):
        raise ReleaseGateError(f"{label} lacks its exact aspiration depth schedule")
    normalized: list[dict[str, Any]] = []
    previous_score: int | None = None
    previous_owner: str | None = None
    for expected_depth, raw_iteration in zip(expected_depths, iterations, strict=True):
        iteration = _mapping(raw_iteration, f"{label} D{expected_depth} aspiration iteration")
        if iteration.get("depth") != expected_depth:
            raise ReleaseGateError(f"{label} has a malformed D{expected_depth} aspiration iteration")
        if "aspiration" in iteration:
            aspiration = _mapping(
                iteration.get("aspiration"),
                f"{label} D{expected_depth} aspiration telemetry",
            )
            score = _integer(
                iteration.get("score"),
                f"{label} D{expected_depth} score",
                -2_000_000_000,
            )
            selected_owner = _text(
                iteration.get("owner_worker_id"),
                f"{label} D{expected_depth} selected owner",
            )
        else:
            aspiration = iteration
            score = _integer(
                iteration.get("selected_score"),
                f"{label} D{expected_depth} selected score",
                -2_000_000_000,
            )
            selected_owner = _text(
                iteration.get("selected_owner_worker_id"),
                f"{label} D{expected_depth} selected owner",
            )
        expected_enabled = expected_mode == "warm" and previous_score is not None
        if aspiration.get("enabled") is not expected_enabled:
            state = "enabled" if expected_enabled else "disabled"
            raise ReleaseGateError(f"{label} D{expected_depth} aspiration must be {state}")
        if aspiration.get("maximum_attempts") != MAX_ASPIRATION_ATTEMPTS:
            raise ReleaseGateError(f"{label} D{expected_depth} aspiration attempt limit drifted")
        candidate_count = _integer(
            aspiration.get("candidate_count"),
            f"{label} D{expected_depth} aspiration candidate count",
        )
        if candidate_count > 8:
            raise ReleaseGateError(
                f"{label} D{expected_depth} aspiration candidate count is invalid"
            )
        counters = {
            field: _integer(
                aspiration.get(field),
                f"{label} D{expected_depth} aspiration {field}",
            )
            for field in ASPIRATION_COUNTER_FIELDS
        }
        if (
            counters["attempts"] > candidate_count * MAX_ASPIRATION_ATTEMPTS
            or counters["exact_hits"] > candidate_count
            or counters["full_window_fallbacks"] > candidate_count
            or counters["fail_highs"] + counters["fail_lows"]
            + counters["exact_hits"] != counters["attempts"]
            or counters["exact_hits"] + counters["full_window_fallbacks"]
            > candidate_count
        ):
            raise ReleaseGateError(
                f"{label} D{expected_depth} aspiration accounting contradicts itself"
            )

        aspiration_owner = aspiration.get("owner_worker_id")
        owner_worker_ids = aspiration.get("owner_worker_ids")
        owner_worker_count = aspiration.get("owner_worker_count")
        warm_owner_reused = aspiration.get("warm_owner_reused")
        warm_owner_reused_count = aspiration.get("warm_owner_reused_count")
        if expected_enabled:
            if (
                candidate_count != expected_candidate_count
                or aspiration.get("center_score") != previous_score
                or aspiration.get("initial_delta") != ASPIRATION_INITIAL_DELTA
            ):
                raise ReleaseGateError(f"{label} D{expected_depth} aspiration window drifted")
            if (
                not isinstance(aspiration_owner, str)
                or not aspiration_owner
                or aspiration_owner != previous_owner
                or warm_owner_reused is not True
                or not isinstance(owner_worker_ids, list)
                or len(owner_worker_ids) != candidate_count
                or any(not isinstance(owner, str) or not owner for owner in owner_worker_ids)
                or len(set(owner_worker_ids)) != candidate_count
                or owner_worker_ids[0] != aspiration_owner
                or owner_worker_count != candidate_count
                or warm_owner_reused_count != candidate_count
            ):
                raise ReleaseGateError(f"{label} D{expected_depth} did not reuse its warm owner")
            if candidate_count < 1 or counters["attempts"] < candidate_count or (
                counters["exact_hits"] + counters["full_window_fallbacks"]
                != candidate_count
            ):
                raise ReleaseGateError(
                    f"{label} D{expected_depth} aspiration has no exact result or fallback"
                )
            failure_count = counters["fail_highs"] + counters["fail_lows"]
            if counters["full_window_fallbacks"] > 0 and (
                failure_count
                < counters["full_window_fallbacks"] * MAX_ASPIRATION_ATTEMPTS
                or failure_count > (
                    counters["full_window_fallbacks"] * MAX_ASPIRATION_ATTEMPTS
                    + counters["exact_hits"] * (MAX_ASPIRATION_ATTEMPTS - 1)
                )
            ):
                raise ReleaseGateError(
                    f"{label} D{expected_depth} aspiration fallback accounting is invalid"
                )
        elif (
            aspiration.get("center_score") is not None
            or aspiration.get("initial_delta") is not None
            or candidate_count != 0
            or aspiration_owner is not None
            or owner_worker_ids != []
            or owner_worker_count != 0
            or warm_owner_reused is not False
            or warm_owner_reused_count != 0
            or any(counters.values())
        ):
            raise ReleaseGateError(f"{label} D{expected_depth} disabled aspiration did work")

        normalized.append(
            {
                "depth": expected_depth,
                "selected_score": score,
                "selected_owner_worker_id": selected_owner,
                "enabled": expected_enabled,
                "center_score": aspiration.get("center_score"),
                "initial_delta": aspiration.get("initial_delta"),
                "maximum_attempts": MAX_ASPIRATION_ATTEMPTS,
                "candidate_count": candidate_count,
                **counters,
                "owner_worker_id": aspiration_owner,
                "owner_worker_ids": list(owner_worker_ids),
                "owner_worker_count": owner_worker_count,
                "warm_owner_reused": warm_owner_reused,
                "warm_owner_reused_count": warm_owner_reused_count,
            }
        )
        previous_score = score
        previous_owner = selected_owner
    return normalized


def _validate_opera_run_binding(
    value: object,
    *,
    label: str,
    selected: Mapping[str, Any],
    expected_candidate_ids: set[str],
    expected_depths: Sequence[int],
    expected_mode: str,
    expected_candidate_count: int,
) -> tuple[list[dict[str, Any]], str, str, list[dict[str, Any]]]:
    run = _mapping(value, label)
    selected_signature = _canonical_sha256(selected)
    if (
        run.get("status") != "complete"
        or run.get("selected_signature_sha256") != selected_signature
        or run.get("selected_candidate_identity") != selected.get("candidate_identity")
        or run.get("unknown_or_limit_count") != 0
        or run.get("selected_owner_certification_count") != 1
    ):
        raise ReleaseGateError(f"{label} did not reproduce the selected oracle result")
    _number(run.get("elapsed_ms"), f"{label} elapsed", 0.000001)
    retained_manifest = run.get("retained_manifest_sha256")
    if not isinstance(retained_manifest, str) or not HEX_64.fullmatch(retained_manifest):
        raise ReleaseGateError(f"{label} has an invalid retained-manifest digest")
    bounds = _normalize_opera_bounds(
        run.get("rival_bounds"),
        label=label,
        expected_candidate_ids=expected_candidate_ids,
        selected=selected,
    )
    coverage_sha256 = _canonical_sha256(bounds)
    if run.get("root_coverage_sha256") != coverage_sha256:
        raise ReleaseGateError(f"{label} has an invalid root-coverage digest")
    semantic = {
        "selected": dict(selected),
        "retained_manifest_sha256": retained_manifest,
        "rival_bounds": bounds,
    }
    run_signature = _canonical_sha256(semantic)
    if run.get("run_signature_sha256") != run_signature:
        raise ReleaseGateError(f"{label} has an invalid actual-run signature")
    aspiration = _validate_opera_aspiration_iterations(
        run.get("aspiration_iterations"),
        label=label,
        expected_depths=expected_depths,
        expected_mode=expected_mode,
        expected_candidate_count=expected_candidate_count,
    )
    if run.get("aspiration_sha256") != _canonical_sha256(aspiration):
        raise ReleaseGateError(f"{label} has an invalid aspiration digest")
    return bounds, retained_manifest, run_signature, aspiration


def _validate_opera(
    receipt: Receipt,
    build: BuildEvidence,
    *,
    expected_config: Mapping[str, Any],
    oracle_selected: Mapping[str, Any],
    oracle_rival_bounds: list[dict[str, Any]],
    oracle_retained_manifest_sha256: str,
    oracle_signature_sha256: str,
) -> tuple[dict[str, Any], float, dict[str, Any], dict[str, Any], int]:
    payload = receipt.payload
    if payload.get("schema") != OPERA_CDP_SCHEMA or payload.get("status") != "passed-not-certified":
        raise ReleaseGateError("Opera CDP receipt did not pass")
    _false(payload, "product_publishable", "Opera CDP")
    _false(payload, "safety_certified", "Opera CDP")
    cdp = _mapping(payload.get("cdp"), "Opera CDP identity")
    browser = _text(cdp.get("browser"), "Opera CDP browser")
    user_agent = _text(cdp.get("user_agent"), "Opera CDP user agent")
    if not browser.startswith("Chrome/") or " OPR/" not in user_agent:
        raise ReleaseGateError("CDP receipt is not from an Opera runtime")
    _text(cdp.get("protocol_version"), "Opera CDP protocol version")
    _true(cdp, "web_socket_debugger_url_recorded", "Opera CDP")
    page = _mapping(payload.get("page_environment"), "Opera page environment")
    if page.get("userAgent") != user_agent or page.get("crossOriginIsolated") is not False:
        raise ReleaseGateError("Opera page runtime identity drifted from CDP")
    _integer(page.get("hardwareConcurrency"), "Opera hardware concurrency", 8)
    parsed_url = urlparse(_text(page.get("location"), "Opera benchmark URL"))
    if parsed_url.hostname != "127.0.0.1" or not parsed_url.path.endswith("/benchmarks/opera_root_d5_probe.html"):
        raise ReleaseGateError("Opera benchmark was not captured from the local D5 harness")
    query = parse_qs(parsed_url.query, keep_blank_values=True)

    worker = _mapping(payload.get("worker_receipt"), "Opera Worker receipt")
    if worker.get("schema") != OPERA_WORKER_SCHEMA or worker.get("status") != "passed-not-certified":
        raise ReleaseGateError("Opera Worker D1-D5 receipt did not pass")
    _false(worker, "product_publishable", "Opera Worker")
    _true(worker, "safety_certified", "Opera Worker")
    artifact = _require_identity(worker.get("artifact"), build.identity, "Opera Worker")
    for key in RUNTIME_IDENTITY_FIELDS:
        if artifact.get(key) != build.runtime_identity[key]:
            raise ReleaseGateError(f"Opera Worker runtime identity {key!r} drifted")

    geometry = dict(_mapping(worker.get("geometry"), "Opera geometry"))
    if (
        geometry.get("workers") != 8
        or geometry.get("initial_full_wave") != 8
        or geometry.get("depth") != 5
        or geometry.get("width") != 32
        or geometry.get("mode") != "warm"
        or geometry.get("aspiration_enabled") is not True
    ):
        raise ReleaseGateError(
            "Opera release geometry must be warm aspiration-capable 8 Workers, wave 8, W32 D5"
        )
    max_work = _integer(geometry.get("max_work"), "Opera maximum work", 1_000)
    safety_reserve = _integer(
        geometry.get("safety_reserve_work"),
        "Opera safety reserve work",
        1,
    )
    if safety_reserve > max_work:
        raise ReleaseGateError("Opera safety reserve exceeds the global work cap")
    expected_query = {
        "depth": "5",
        "width": "32",
        "workers": "8",
        "wave": "8",
        "max_work": str(max_work),
        "safety_work": str(safety_reserve),
    }
    for key, expected in expected_query.items():
        if _query_value(query, key) != expected:
            raise ReleaseGateError(f"Opera benchmark URL {key!r} does not match its receipt")
    if (
        PurePosixPath(urlparse(_query_value(query, "module")).path).name
        != build.module_js.name
        or PurePosixPath(urlparse(_query_value(query, "wasm")).path).name
        != build.wasm.name
        or PurePosixPath(urlparse(_query_value(query, "receipt")).path).name
        != build.receipt.path.name
    ):
        raise ReleaseGateError("Opera benchmark URL does not name the verified build bytes")
    config = dict(_mapping(geometry.get("config"), "Opera root config"))
    if (
        config.get("max_depth") != 5
        or config.get("width") != 32
        or config.get("max_work") != max_work
        or config.get("worker_threads") != 1
    ):
        raise ReleaseGateError("Opera root config differs from its W32 D5 geometry")
    if config != expected_config:
        raise ReleaseGateError("Opera root config differs from the signed D5 oracle")
    session_geometry = _mapping(
        build.receipt.payload.get("session_geometry"),
        "build session geometry",
    )
    expected_build_geometry = {
        "series_cache_capacity": "desktop_series_cache_capacity",
        "root_contract_tt_capacity": "root_contract_tt_capacity",
        "root_contract_eval_capacity": "root_contract_eval_capacity",
    }
    for config_key, build_key in expected_build_geometry.items():
        if config.get(config_key) != session_geometry.get(build_key):
            raise ReleaseGateError(f"Opera config {config_key!r} drifted from the build")

    timings = _mapping(worker.get("timings_ms"), "Opera timings")
    pool_ms = _number(timings.get("pool_ready"), "Opera pool-ready time")
    iterative_ms = _number(
        timings.get("iterative_d1_through_d5"),
        "Opera D1-D5 time",
        0.000001,
    )
    total_ms = _number(
        timings.get("total_to_completed_depth"),
        "Opera total D5 time",
        0.000001,
    )
    final_iteration_ms = _number(
        timings.get("completed_depth_iteration"),
        "Opera final-depth time",
        0.000001,
    )
    if total_ms >= 60_000 or iterative_ms >= 60_000:
        raise ReleaseGateError("Opera W32 D1-D5 did not complete in under 60 seconds")
    try:
        timeout_value = float(_query_value(query, "timeout_ms"))
    except ValueError as error:
        raise ReleaseGateError("Opera benchmark timeout is not numeric") from error
    timeout_ms = _number(timeout_value, "Opera benchmark timeout", 0.000001)
    if timeout_ms < total_ms:
        raise ReleaseGateError("Opera elapsed time exceeds the benchmark timeout")
    if total_ms < iterative_ms or total_ms + 1e-6 < pool_ms:
        raise ReleaseGateError("Opera timing accounting is internally inconsistent")

    iterations = _list(worker.get("iterations"), "Opera iterations")
    if [item.get("depth") if isinstance(item, Mapping) else None for item in iterations] != [1, 2, 3, 4, 5]:
        raise ReleaseGateError("Opera receipt must contain exact persistent D1-D5 iterations")
    warm_aspiration = _validate_opera_aspiration_iterations(
        iterations,
        label="Opera warm D1-D5",
        expected_depths=(1, 2, 3, 4, 5),
        expected_mode="warm",
        expected_candidate_count=8,
    )
    for index, raw_iteration in enumerate(iterations, start=1):
        iteration = _mapping(raw_iteration, f"Opera D{index} iteration")
        _number(iteration.get("elapsed_ms"), f"Opera D{index} elapsed", 0.000001)
        _text(iteration.get("candidate_identity"), f"Opera D{index} candidate identity")
        if not _list(iteration.get("principal_variation"), f"Opera D{index} principal variation"):
            raise ReleaseGateError(f"Opera D{index} did not retain a principal variation")
        if (
            iteration.get("coverage_complete") is not True
            or iteration.get("safety_status") not in {"exhausted", "terminal"}
            or iteration.get("owner_certification_count") != 1
        ):
            raise ReleaseGateError(f"Opera D{index} did not publish a safe exact owner result")
        replay = _mapping(iteration.get("final_replay"), f"Opera D{index} replay")
        if replay.get("complete") is not True or replay.get("next_state") is None:
            raise ReleaseGateError(f"Opera D{index} compiled replay did not complete")
        work = _mapping(iteration.get("work"), f"Opera D{index} work")
        if work.get("max_work") != max_work or work.get("within_cap") is not True:
            raise ReleaseGateError(f"Opera D{index} exceeded or changed the global work cap")
        committed = _integer(work.get("committed_work"), f"Opera D{index} committed work")
        reserved = _integer(work.get("reserved_work"), f"Opera D{index} reserved work")
        remaining = _integer(work.get("remaining_work"), f"Opera D{index} remaining work")
        if reserved != 0 or committed + remaining != max_work:
            raise ReleaseGateError(f"Opera D{index} work ledger does not settle exactly")
        if work.get("safety_reserve_work") != safety_reserve:
            raise ReleaseGateError(f"Opera D{index} did not use the certified safety reserve")
        safety_committed = _integer(
            work.get("safety_committed_work"),
            f"Opera D{index} safety work",
        )
        if safety_committed > safety_reserve:
            raise ReleaseGateError(f"Opera D{index} safety work exceeded its reserve")
    final_iteration = _mapping(iterations[-1], "Opera D5 iteration")
    if abs(_number(final_iteration.get("elapsed_ms"), "Opera D5 elapsed") - final_iteration_ms) > 1e-6:
        raise ReleaseGateError("Opera final-depth timing differs from the D5 iteration")

    result = dict(_mapping(worker.get("result"), "Opera result"))
    if (
        result.get("completed_depth") != 5
        or result.get("coverage_complete") is not True
        or result.get("safety_status") not in {"exhausted", "terminal"}
        or result.get("move") != final_iteration.get("move")
        or result.get("score") != final_iteration.get("score")
        or result.get("proof_bounds") != final_iteration.get("proof_bounds")
        or result.get("candidate_identity") != final_iteration.get("candidate_identity")
        or result.get("principal_variation") != final_iteration.get("principal_variation")
    ):
        raise ReleaseGateError("Opera final result is not the completed safe D5 iteration")
    for key in (
        "work",
        "safety_status",
        "safety_revision",
        "owner_worker_id",
        "root_bounds",
        "retained_manifest_sha256",
        "order_shape_sha256",
        "coverage_complete",
        "root_scores_complete",
        "width_complete",
    ):
        if result.get(key) != final_iteration.get(key):
            raise ReleaseGateError(f"Opera final result {key!r} differs from its D5 iteration")
    expected_result_fields = {
        "candidate_identity": oracle_selected.get("candidate_identity"),
        "move": oracle_selected.get("move"),
        "score": oracle_selected.get("score"),
        "proof_bounds": oracle_selected.get("proof_bounds"),
        "principal_variation": oracle_selected.get("principal_variation"),
    }
    for key, expected in expected_result_fields.items():
        if result.get(key) != expected:
            raise ReleaseGateError(f"Opera warm D1-D5 result {key!r} differs from the oracle")
    expected_candidate_ids = {
        _text(item.get("candidate_identity"), "root D5 oracle candidate identity")
        for item in oracle_rival_bounds
    }
    if len(expected_candidate_ids) != 20:
        raise ReleaseGateError("root D5 oracle candidate universe is incomplete")
    warm_result_bounds = _normalize_opera_bounds(
        result.get("root_bounds"),
        label="Opera warm D1-D5 result",
        expected_candidate_ids=expected_candidate_ids,
        selected=oracle_selected,
    )
    warm_result_manifest = result.get("retained_manifest_sha256")
    warm_result_order_shape = result.get("order_shape_sha256")
    if (
        warm_result_bounds != oracle_rival_bounds
        or warm_result_manifest != oracle_retained_manifest_sha256
        or not isinstance(warm_result_order_shape, str)
        or not HEX_64.fullmatch(warm_result_order_shape)
    ):
        raise ReleaseGateError("Opera warm D1-D5 rival coverage differs from the signed oracle")

    oracle = _mapping(worker.get("oracle"), "Opera oracle binding")
    selected_signature = _canonical_sha256(oracle_selected)
    if (
        oracle.get("schema") != "spc-opera-root-d5-oracle-binding-v1"
        or oracle.get("oracle_signature_sha256") != oracle_signature_sha256
        or oracle.get("selected_signature_sha256") != selected_signature
        or oracle.get("cold_selected_matches_oracle") is not True
        or oracle.get("warm_full_matches_oracle") is not True
    ):
        raise ReleaseGateError("Opera receipt is not bound to the signed root D5 oracle")
    _, _, _, cold_aspiration = _validate_opera_run_binding(
        oracle.get("cold_d5"),
        label="Opera cold D5 oracle run",
        selected=oracle_selected,
        expected_candidate_ids=expected_candidate_ids,
        expected_depths=(5,),
        expected_mode="cold",
        expected_candidate_count=0,
    )
    warm_bounds, warm_manifest, warm_run_signature, warm_binding_aspiration = (
        _validate_opera_run_binding(
            oracle.get("warm_d1_through_d5"),
            label="Opera warm D1-D5 oracle run",
            selected=oracle_selected,
            expected_candidate_ids=expected_candidate_ids,
            expected_depths=(1, 2, 3, 4, 5),
            expected_mode="warm",
            expected_candidate_count=8,
        )
    )
    if warm_bounds != oracle_rival_bounds or warm_manifest != oracle_retained_manifest_sha256:
        raise ReleaseGateError("Opera warm oracle run does not carry the signed full coverage")
    if warm_binding_aspiration != warm_aspiration:
        raise ReleaseGateError("Opera warm oracle aspiration receipt differs from its iterations")
    if cold_aspiration[0]["enabled"] is not False:
        raise ReleaseGateError("Opera cold D5 aspiration must be disabled")
    schedule_trials = _list(worker.get("schedule_trials"), "Opera schedule trials")
    if len(schedule_trials) < 2:
        raise ReleaseGateError("Opera receipt needs at least two real schedule shapes")
    schedule_shapes: set[tuple[int, str]] = set()
    order_shapes: set[str] = set()
    saw_wave_eight = False
    for raw_trial in schedule_trials:
        trial = _mapping(raw_trial, "Opera schedule trial")
        workers = _integer(trial.get("workers"), "Opera schedule trial workers", 1)
        wave = _integer(trial.get("initial_full_wave"), "Opera schedule trial wave", 1)
        order_shape = trial.get("order_shape_sha256")
        if workers != 8 or wave > workers:
            raise ReleaseGateError("Opera schedule trial used the wrong Worker geometry")
        if not isinstance(order_shape, str) or not HEX_64.fullmatch(order_shape):
            raise ReleaseGateError("Opera schedule trial has an invalid order-shape digest")
        trial_bounds, trial_manifest, trial_signature, trial_aspiration = (
            _validate_opera_run_binding(
                trial,
                label=f"Opera wave-{wave} schedule trial",
                selected=oracle_selected,
                expected_candidate_ids=expected_candidate_ids,
                expected_depths=(1, 2, 3, 4, 5),
                expected_mode="warm",
                expected_candidate_count=wave,
            )
        )
        trial_semantic = {
            "run_signature_sha256": trial_signature,
            "workers": workers,
            "initial_full_wave": wave,
            "order_shape_sha256": order_shape,
            "aspiration_sha256": trial.get("aspiration_sha256"),
        }
        if trial.get("trial_signature_sha256") != _canonical_sha256(trial_semantic):
            raise ReleaseGateError("Opera schedule trial has an invalid schedule signature")
        if wave == 8 and (
            trial_bounds != oracle_rival_bounds
            or trial_manifest != oracle_retained_manifest_sha256
            or trial_signature != warm_run_signature
            or order_shape != warm_result_order_shape
            or trial_aspiration != warm_aspiration
        ):
            raise ReleaseGateError("Opera wave-8 schedule trial differs from the signed warm run")
        schedule_shapes.add((wave, order_shape))
        order_shapes.add(order_shape)
        saw_wave_eight = saw_wave_eight or wave == 8
    if len(schedule_shapes) < 2 or len(order_shapes) < 2 or not saw_wave_eight:
        raise ReleaseGateError("Opera schedule trials do not prove two distinct real order shapes")

    memory = dict(_mapping(worker.get("memory"), "Opera memory"))
    maximum = int(build.memory["maximum_bytes"])
    aggregate_maximum = 8 * maximum
    if (
        memory.get("per_worker_hard_maximum_bytes") != maximum
        or memory.get("aggregate_hard_maximum_bytes") != aggregate_maximum
    ):
        raise ReleaseGateError("Opera memory envelope differs from the linked build")
    worker_memory = _list(memory.get("workers"), "Opera Worker memory")
    if len(worker_memory) != 8:
        raise ReleaseGateError("Opera memory receipt must cover all 8 Workers")
    memory_ids: set[str] = set()
    observed_sum = 0
    for item in worker_memory:
        entry = _mapping(item, "Opera Worker memory entry")
        worker_id = _text(entry.get("id"), "Opera Worker memory id")
        if worker_id in memory_ids:
            raise ReleaseGateError("Opera memory receipt duplicates a Worker")
        memory_ids.add(worker_id)
        peak = _integer(entry.get("peak_bytes"), f"Opera {worker_id} peak memory", 1)
        if peak > maximum:
            raise ReleaseGateError(f"Opera {worker_id} exceeded its hard memory maximum")
        _integer(entry.get("native_work_after"), f"Opera {worker_id} native work")
        observed_sum += peak
    if (
        memory.get("aggregate_observed_peak_bytes") != observed_sum
        or observed_sum > aggregate_maximum
    ):
        raise ReleaseGateError("Opera aggregate memory receipt is inconsistent")

    environment = _mapping(worker.get("environment"), "Opera Worker environment")
    if (
        environment.get("ordinary_module_workers") is not True
        or environment.get("worker_count") != 8
        or environment.get("worker_global_scope") != "DedicatedWorkerGlobalScope"
        or environment.get("cross_origin_isolated") is not False
    ):
        raise ReleaseGateError("Opera did not prove 8 ordinary dedicated module Workers")
    _integer(environment.get("hardware_concurrency"), "Opera Worker hardware concurrency", 8)
    workers = _list(environment.get("workers"), "Opera Worker identities")
    if len(workers) != 8:
        raise ReleaseGateError("Opera identity receipt must cover all 8 Workers")
    worker_ids: set[str] = set()
    certificate_ids: set[str] = set()
    worker_identity_fields = (
        "source_fingerprint",
        "kernel_sha256",
        "module_js_sha256",
    )
    expected_worker_identity = {
        **build.identity,
        **build.engine,
        "runtime_variant": "single",
        "thread_count": 1,
    }
    for raw_worker in workers:
        entry = _mapping(raw_worker, "Opera Worker identity entry")
        worker_id = _text(entry.get("worker_id"), "Opera Worker id")
        if worker_id in worker_ids:
            raise ReleaseGateError("Opera receipt duplicates a Worker identity")
        worker_ids.add(worker_id)
        identity = _mapping(entry.get("identity"), f"Opera {worker_id} identity")
        for key in worker_identity_fields + (
            "runtime_variant",
            "thread_count",
            "engine_version",
            "ruleset_version",
            "profile_id",
        ):
            if identity.get(key) != expected_worker_identity[key]:
                raise ReleaseGateError(f"Opera {worker_id} identity {key!r} drifted")
        certificate_ids.add(_text(identity.get("certificate_id"), f"Opera {worker_id} certificate id"))
        worker_artifact = _mapping(entry.get("artifact"), f"Opera {worker_id} artifact")
        for key in ARTIFACT_IDENTITY_FIELDS:
            if worker_artifact.get(key) != build.identity[key]:
                raise ReleaseGateError(f"Opera {worker_id} artifact {key!r} drifted")
        for key in RUNTIME_IDENTITY_FIELDS:
            if worker_artifact.get(key) != build.runtime_identity[key]:
                raise ReleaseGateError(f"Opera {worker_id} runtime {key!r} drifted")
        if (
            entry.get("ordinary_module_worker") is not True
            or entry.get("worker_global_scope") != "DedicatedWorkerGlobalScope"
        ):
            raise ReleaseGateError(f"Opera {worker_id} is not an ordinary module Worker")
    if len(certificate_ids) != 1 or worker_ids != memory_ids:
        raise ReleaseGateError("Opera Worker identity and memory membership differ")

    gates = _mapping(worker.get("gates"), "Opera gates")
    for key in (
        "exact_artifact_identity_all_workers",
        "ordinary_module_workers",
        "pthreads_disabled",
        "combined_prefix_root_mate_abi",
        "persistent_d1_through_d5_sessions",
        "exact_manifest_import_all_workers",
        "global_work_cap_enforced",
        "common_monotonic_deadline",
        "dynamic_work_pool_certified",
        "final_bound_coverage",
        "selected_owner_warm_exact_certification",
        "compiled_root_prefix_replay",
        "compiled_reply_mate_safety",
        "memory_envelope_observed",
        "d5_w32_anchor",
        "under_60_seconds_total",
        "cold_d5_selected_matches_oracle",
        "warm_d1_d5_full_matches_oracle",
        "alternate_schedule_selected_matches_oracle",
        "multiple_seed_wave_order_shapes",
        "no_unknown_or_limit_results",
        "aspiration_iteration_lifecycle",
    ):
        _true(gates, key, "Opera")
    if gates.get("release_certificate_present") is not False:
        raise ReleaseGateError("Opera benchmark must precede release certification")
    return config, total_ms / 1000.0, result, memory, safety_reserve


def validate_evidence(
    *,
    repository: Path,
    source_package: Path,
    receipt_paths: Mapping[str, Path],
) -> ValidatedEvidence:
    required = set(RECEIPT_FILENAMES)
    if set(receipt_paths) != required:
        raise ReleaseGateError("release evidence must provide exactly all seven receipt types")
    receipts = {
        label: _load_receipt(label, receipt_paths[label])
        for label in RECEIPT_FILENAMES
    }
    build = _validate_build_receipt(
        receipts["build"],
        repository=repository,
        source_package=source_package,
    )
    root_contract, prefix_contract, checked_horizon = _validate_root_smoke(
        receipts["root_smoke"],
        build,
    )
    (
        root_cases,
        root_config,
        canonical_d5,
        oracle_rival_bounds,
        oracle_retained_manifest,
        oracle_signature,
    ) = _validate_root_parity(
        receipts["root_parity"],
        build,
        root_contract,
    )
    root_config, elapsed, opera_result, opera_memory, safety_reserve = _validate_opera(
        receipts["opera"],
        build,
        expected_config=root_config,
        oracle_selected=canonical_d5,
        oracle_rival_bounds=oracle_rival_bounds,
        oracle_retained_manifest_sha256=oracle_retained_manifest,
        oracle_signature_sha256=oracle_signature,
    )
    prefix_cases = _validate_prefix_parity(receipts["prefix_parity"], build)
    _validate_browser_prefix(receipts["browser_prefix"], build)
    mate_cases = _validate_mate_parity(receipts["mate_parity"], build)
    return ValidatedEvidence(
        build=build,
        receipts=receipts,
        root_contract=root_contract,
        checked_horizon_proof_research=checked_horizon,
        prefix_contract=prefix_contract,
        oracle_signature_sha256=oracle_signature,
        root_config=root_config,
        root_differential_cases=root_cases,
        prefix_differential_cases=prefix_cases,
        mate_differential_cases=mate_cases,
        opera_elapsed_seconds=elapsed,
        opera_result=opera_result,
        opera_memory=opera_memory,
        safety_reserve_positions=safety_reserve,
    )


def _certificate_id(capability: str, evidence: ValidatedEvidence) -> str:
    seed = {
        "capability": capability,
        "identity": evidence.build.identity,
        "receipts": {
            label: receipt.sha256
            for label, receipt in sorted(evidence.receipts.items())
        },
    }
    return f"spc-{capability}-{_canonical_sha256(seed)[:16]}"


def _common_certificate(evidence: ValidatedEvidence) -> dict[str, Any]:
    build = evidence.build
    return {
        "status": "certified",
        "contract_version": 1,
        "source_fingerprint": build.source_fingerprint,
        "wasm_sha256": build.identity["wasm_sha256"],
        "module_js_sha256": build.identity["module_js_sha256"],
        "runtime_variant": "single",
        "thread_count": 1,
        "support_files": [],
        "memory": dict(build.memory),
    }


def build_certificates(
    evidence: ValidatedEvidence,
    *,
    maximum_seconds: float,
    default_seconds: float,
) -> dict[str, dict[str, Any]]:
    maximum_value = float(maximum_seconds)
    default_value = float(default_seconds)
    maximum_seconds = int(maximum_value) if maximum_value.is_integer() else maximum_value
    default_seconds = int(default_value) if default_value.is_integer() else default_value
    if (
        not math.isfinite(maximum_seconds)
        or not math.isfinite(default_seconds)
        or not 0 < default_seconds <= maximum_seconds <= 60
    ):
        raise ReleaseGateError("release play seconds must satisfy 0 < default <= maximum <= 60")
    if default_seconds + 1e-9 < evidence.opera_elapsed_seconds:
        raise ReleaseGateError("default play time is shorter than the proven Opera D1-D5 run")
    build = evidence.build
    common = _common_certificate(evidence)
    combined = {
        **common,
        "product_publishable": False,
        "kernel_sha256": build.identity["kernel_sha256"],
        "exports": list(bundle_builder.COMBINED_EXPORTS),
        **build.runtime_identity,
        "runtime_requirements": dict(build.runtime_requirements),
        "engine": dict(build.engine),
    }
    prefix = {
        **common,
        "certificate_id": _certificate_id("prefix", evidence),
        "evidence": {
            "failures": 0,
            "compiled_prefix_replay": True,
            "multi_ep_san": True,
            "illegal_prefix_fail_closed": True,
            "differential_cases": evidence.prefix_differential_cases,
        },
        "engine": {
            "engine_version": build.engine["engine_version"],
            "ruleset_version": build.engine["ruleset_version"],
        },
        "prefix_contract": dict(evidence.prefix_contract),
    }
    root = {
        **combined,
        "schema": bundle_builder.ROOT_SESSION_CERTIFICATE_SCHEMA,
        "certificate_id": _certificate_id("root-session", evidence),
        "abi_version": 2,
        "root_session_certified": True,
        "reply_mate_safety": False,
        "root_session_contract": dict(evidence.root_contract),
        "checked_horizon_proof_research": dict(
            evidence.checked_horizon_proof_research
        ),
        "geometry": {
            "desktop_workers": 8,
            "desktop_initial_full_wave": 8,
            "aggregate_maximum_bytes": 8 * int(build.memory["maximum_bytes"]),
            "supported_lower_geometries": [],
            "session_config": dict(evidence.root_config),
            "play_limits": {
                "maximum_seconds": maximum_seconds,
                "default_seconds": default_seconds,
                "default_generation_positions": evidence.root_config["max_work"],
                "safety_reserve_positions": evidence.safety_reserve_positions,
            },
        },
        "evidence": {
            "failures": 0,
            "differential_cases": evidence.root_differential_cases,
            "deterministic_node_smoke": True,
            "combined_artifact": True,
            "enumerate_import_search": True,
            "exact_manifest_import": True,
            "persistent_d1_d2_session": True,
            "aspiration_fail_soft_window": True,
            "aspiration_fail_high_low_white_black": True,
            "cumulative_work_and_cache_receipts": True,
            "configured_max_depth_rejected": True,
            "per_call_work_credit": True,
            "selected_owner_warm_exact_certification": True,
            "checked_horizon_proof_research": True,
            "checked_horizon_newest_proof_hit": True,
            "deadline_fail_closed": True,
            "work_limit_fail_closed": True,
            "browser_worker_smoke": True,
            "opera_worker_smoke": True,
            "canonical_root_tactical_policy": True,
            "start_w32_d5_completed_depth": 5,
            "start_w32_d5_width": 32,
            "start_w32_d5_elapsed_seconds": evidence.opera_elapsed_seconds,
        },
    }
    mate = {
        **combined,
        "schema": bundle_builder.MATE_CERTIFICATE_SCHEMA,
        "certificate_id": _certificate_id("mate", evidence),
        "abi_version": 1,
        "mate_capability_certified": True,
        "reply_mate_safety": True,
        "evidence": {
            "failures": 0,
            "differential_cases": evidence.mate_differential_cases,
            "combined_artifact": True,
            "python_parity": True,
            "authoritative_replay": True,
            "white_found": True,
            "black_found": True,
            "exhausted": True,
            "work_limit_unknown": True,
            "deadline_unknown": True,
            "signed_mate_distance_overrides": True,
            "proof_bounds": True,
            "work_receipts": True,
            "deadline_receipts": True,
            "browser_worker_smoke": True,
        },
    }
    bundle_builder.validate_prefix_certificate(
        prefix,
        source_fingerprint=build.source_fingerprint,
        wasm_sha256=build.identity["wasm_sha256"],
        module_js_sha256=build.identity["module_js_sha256"],
        runtime_variant="single",
        thread_count=1,
        support_files=[],
    )
    bundle_builder.validate_root_session_certificate(
        root,
        source_fingerprint=build.source_fingerprint,
        wasm_sha256=build.identity["wasm_sha256"],
        module_js_sha256=build.identity["module_js_sha256"],
        runtime_variant="single",
        thread_count=1,
        support_files=[],
    )
    bundle_builder.validate_mate_certificate(
        mate,
        source_fingerprint=build.source_fingerprint,
        wasm_sha256=build.identity["wasm_sha256"],
        module_js_sha256=build.identity["module_js_sha256"],
        runtime_variant="single",
        thread_count=1,
        support_files=[],
    )
    return {"prefix": prefix, "root_session": root, "mate": mate}


def _local_url(value: object, *, origin: str, suffix: str, label: str) -> str:
    url = _text(value, label)
    parsed = urlparse(url)
    expected_origin = urlparse(origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.scheme != expected_origin.scheme
        or parsed.netloc != expected_origin.netloc
        or not parsed.path.endswith(suffix)
    ):
        raise ReleaseGateError(f"{label} is not the expected loopback-served asset")
    return url


def _validate_observed_asset(
    value: object,
    *,
    expected_path: Path,
    origin: str,
    suffix: str,
    label: str,
) -> dict[str, Any]:
    asset = dict(_mapping(value, label))
    if set(asset) != {"url", "byte_length", "sha256"}:
        raise ReleaseGateError(f"{label} does not have the exact asset-record shape")
    _local_url(asset.get("url"), origin=origin, suffix=suffix, label=f"{label} URL")
    if (
        not expected_path.is_file()
        or asset.get("byte_length") != expected_path.stat().st_size
        or asset.get("sha256") != _sha256_file(expected_path)
    ):
        raise ReleaseGateError(f"{label} bytes differ from the staged release candidate")
    return asset


def validate_opera_checked_horizon_receipt(
    *,
    receipt_path: Path,
    evidence: ValidatedEvidence,
    certificates: Mapping[str, Mapping[str, Any]],
    repository: Path,
    source_package: Path,
    candidate_bundle: Path,
) -> OperaCheckedHorizonEvidence:
    receipt = _load_receipt("Opera checked-PV horizon", receipt_path)
    payload = receipt.payload
    if (
        payload.get("schema") != OPERA_CHECKED_HORIZON_SCHEMA
        or payload.get("status") != "passed-not-certified"
        or payload.get("product_publishable") is not False
        or payload.get("safety_certified") is not False
        or payload.get("certificate_id") is not None
    ):
        raise ReleaseGateError("Opera checked-PV receipt is not exact pre-certification evidence")
    checks = _mapping(payload.get("checks"), "Opera checked-PV checks")
    if set(checks) != OPERA_CHECKED_HORIZON_CHECKS or any(
        value is not True for value in checks.values()
    ):
        raise ReleaseGateError("Opera checked-PV receipt does not contain every exact passing check")

    page = _mapping(payload.get("page_environment"), "Opera checked-PV page environment")
    cdp = _mapping(payload.get("cdp"), "Opera checked-PV CDP identity")
    page_url = _text(payload.get("page_url"), "Opera checked-PV page URL")
    parsed_page = urlparse(page_url)
    if (
        parsed_page.scheme != "http"
        or parsed_page.hostname != "127.0.0.1"
        or page.get("location") != page_url
        or page.get("userAgent") != cdp.get("user_agent")
        or " OPR/" not in str(cdp.get("user_agent", ""))
        or not str(cdp.get("browser", "")).startswith("Chrome/")
        or not isinstance(cdp.get("protocol_version"), str)
        or not cdp.get("protocol_version")
        or isinstance(page.get("hardwareConcurrency"), bool)
        or not isinstance(page.get("hardwareConcurrency"), int)
        or int(page["hardwareConcurrency"]) < 1
        or not isinstance(page.get("crossOriginIsolated"), bool)
    ):
        raise ReleaseGateError("Opera checked-PV page and CDP identities do not match local Opera")
    origin = f"{parsed_page.scheme}://{parsed_page.netloc}"

    candidate_bundle = candidate_bundle.resolve()
    manifest_path = candidate_bundle / "browser-engine-manifest.json"
    manifest = bundle_builder.validate_existing_bundle(
        candidate_bundle,
        source_package.resolve(),
    )
    variant = _mapping(
        _mapping(manifest.get("variants"), "candidate manifest variants").get("single"),
        "candidate single-WASM variant",
    )
    root_certificate = certificates["root_session"]
    mate_certificate = certificates["mate"]
    prefix_certificate = certificates["prefix"]
    expected_manifest_binding = {
        "source_fingerprint": evidence.build.identity["source_fingerprint"],
        "runtime_variant": "single",
        "thread_count": 1,
        "module_js": variant.get("module_js"),
        "wasm": variant.get("wasm"),
        "module_js_sha256": evidence.build.identity["module_js_sha256"],
        "wasm_sha256": evidence.build.identity["wasm_sha256"],
        "kernel_sha256": evidence.build.identity["kernel_sha256"],
        "analysis_certificate_id": None,
        "root_session_certificate_id": root_certificate["certificate_id"],
        "mate_certificate_id": mate_certificate["certificate_id"],
        "prefix_certificate_id": prefix_certificate["certificate_id"],
        "root_contract_sha256": _canonical_sha256(root_certificate["root_session_contract"]),
        "root_geometry_sha256": _canonical_sha256(root_certificate["geometry"]),
        "root_evidence_sha256": _canonical_sha256(root_certificate["evidence"]),
        "prefix_contract_sha256": _canonical_sha256(prefix_certificate["prefix_contract"]),
    }
    if payload.get("manifest_binding") != expected_manifest_binding:
        observed_manifest_binding = _mapping(
            payload.get("manifest_binding"),
            "Opera checked-PV manifest binding",
        )
        drifted = sorted(
            key
            for key in set(observed_manifest_binding) | set(expected_manifest_binding)
            if observed_manifest_binding.get(key) != expected_manifest_binding.get(key)
        )
        raise ReleaseGateError(
            "Opera checked-PV manifest binding differs from the core-seven candidate: "
            + ", ".join(drifted)
        )

    authenticity = _mapping(payload.get("authenticity"), "Opera checked-PV authenticity")
    if (
        authenticity.get("scope") != "local-checkout-hash-bound-unsigned-v1"
        or authenticity.get("standalone_signature_verified") is not False
        or authenticity.get("local_origin") != origin
        or authenticity.get("trusted_worker_events_only") is not True
    ):
        raise ReleaseGateError("Opera checked-PV authenticity envelope is not local and fail-closed")
    manifest_asset = _validate_observed_asset(
        authenticity.get("manifest"),
        expected_path=manifest_path,
        origin=origin,
        suffix="/engine/browser-engine-manifest.json",
        label="Opera checked-PV manifest",
    )
    assets = _mapping(authenticity.get("assets"), "Opera checked-PV observed assets")
    expected_asset_labels = {
        *CHECKED_HORIZON_STATIC_ASSETS,
        "compiled_module",
        "compiled_wasm",
    }
    if set(assets) != expected_asset_labels:
        raise ReleaseGateError("Opera checked-PV observed asset set is incomplete")
    normalized_assets: dict[str, dict[str, Any]] = {}
    static_directory = repository.resolve() / "src" / "scottish_progressive" / "web" / "static"
    for asset_label, filename in CHECKED_HORIZON_STATIC_ASSETS.items():
        normalized_assets[asset_label] = _validate_observed_asset(
            assets[asset_label],
            expected_path=static_directory / filename,
            origin=origin,
            suffix=f"/{filename}",
            label=f"Opera checked-PV asset {asset_label}",
        )
    lane = candidate_bundle / "single"
    normalized_assets["compiled_module"] = _validate_observed_asset(
        assets["compiled_module"],
        expected_path=lane / str(variant["module_js"]),
        origin=origin,
        suffix=f"/engine/single/{variant['module_js']}",
        label="Opera checked-PV compiled module",
    )
    normalized_assets["compiled_wasm"] = _validate_observed_asset(
        assets["compiled_wasm"],
        expected_path=lane / str(variant["wasm"]),
        origin=origin,
        suffix=f"/engine/single/{variant['wasm']}",
        label="Opera checked-PV compiled WASM",
    )
    asset_commitment = _canonical_sha256(
        sorted(
            [
                ["browser_engine_manifest", manifest_asset],
                *[[label, asset] for label, asset in normalized_assets.items()],
            ],
            key=lambda item: item[0],
        )
    )
    if (
        authenticity.get("local_checkout_asset_set_sha256") != asset_commitment
        or HEX_64.fullmatch(str(asset_commitment)) is None
    ):
        raise ReleaseGateError("Opera checked-PV local asset-set commitment is invalid")

    worker_calls = _list(
        authenticity.get("worker_factory_calls"),
        "Opera checked-PV Worker factory calls",
    )
    normalized_workers = [
        _worker_identity(value, f"Opera checked-PV Worker {index}")
        for index, value in enumerate(worker_calls)
    ]
    expected_worker_url = normalized_assets["browser_engine_worker"]["url"]
    if (
        len(normalized_workers) != 9
        or [worker["factory_sequence"] for worker in normalized_workers] != list(range(1, 10))
        or normalized_workers[0]["name"] != "scottish-progressive-engine"
        or normalized_workers[0]["channel_id"] is not None
        or any(worker["url"] != expected_worker_url for worker in normalized_workers)
    ):
        raise ReleaseGateError("Opera checked-PV Worker factory is not exactly bound")
    expected_roots = {
        f"scottish-progressive-root-root-{index}": f"root-{index}"
        for index in range(8)
    }
    actual_roots = {
        worker["name"]: worker["channel_id"] for worker in normalized_workers[1:]
    }
    if actual_roots != expected_roots:
        raise ReleaseGateError("Opera checked-PV root Worker identities are incomplete or duplicated")

    root_identity, prefix_identity = _identity_fields(evidence, certificates)
    preflight_expected = {
        "ready": True,
        "analysis_ready": False,
        "root_iteration_ready": True,
        "root_session_ready": True,
        "mate_ready": True,
        "prefix_ready": True,
        "safety_certified": False,
        "source_fingerprint": evidence.build.identity["source_fingerprint"],
        "runtime_variant": "single",
        "thread_count": 1,
        "module_js_sha256": evidence.build.identity["module_js_sha256"],
        "wasm_sha256": evidence.build.identity["wasm_sha256"],
        "kernel_sha256": evidence.build.identity["kernel_sha256"],
        "certificate_id": None,
        "root_session_certificate_id": root_certificate["certificate_id"],
        "mate_certificate_id": mate_certificate["certificate_id"],
        "prefix_certificate_id": prefix_certificate["certificate_id"],
        "engine_profile_id": evidence.build.engine["profile_id"],
        "engine_version": evidence.build.engine["engine_version"],
        "ruleset_version": evidence.build.engine["ruleset_version"],
        "root_contract_sha256": expected_manifest_binding["root_contract_sha256"],
        "root_geometry_sha256": expected_manifest_binding["root_geometry_sha256"],
        "prefix_contract_sha256": expected_manifest_binding["prefix_contract_sha256"],
    }
    if payload.get("preflight_identity") != preflight_expected:
        raise ReleaseGateError("Opera checked-PV preflight identity differs from the candidate")

    summary = dict(_mapping(payload.get("result_summary"), "Opera checked-PV result summary"))
    required_summary = {
        "ok": True,
        "status": "complete",
        "requested_depth": 5,
        "completed_depth": 5,
        "publishable": True,
        "safety_certified": True,
        "coverage_complete": True,
        "coverage_scope": "selection-eligible-candidates",
        "width_complete": True,
        "legal_series_certified": True,
        "authoritative_replay_certified": True,
        "legal_validation_runtime": "compiled-wasm",
        "root_search_mode": "streaming-root-iteration",
        "selection_policy": CHECKED_PV_SELECTION_POLICY,
        "selection_policy_filtered": True,
        "unfiltered_score_winner_selected": False,
        "timed_out": False,
        "work_limit_reached": False,
        "source_fingerprint": evidence.build.identity["source_fingerprint"],
        "wasm_sha256": evidence.build.identity["wasm_sha256"],
        "kernel_sha256": evidence.build.identity["kernel_sha256"],
        "module_js_sha256": evidence.build.identity["module_js_sha256"],
        "certificate_id": root_certificate["certificate_id"],
        "mate_certificate_id": mate_certificate["certificate_id"],
        "prefix_certificate_id": prefix_certificate["certificate_id"],
        "runtime_variant": "single",
        "thread_count": 1,
        "engine_profile_id": evidence.build.engine["profile_id"],
        "engine_version": evidence.build.engine["engine_version"],
        "ruleset_version": evidence.build.engine["ruleset_version"],
    }
    for key, expected in required_summary.items():
        if summary.get(key) != expected:
            raise ReleaseGateError(f"Opera checked-PV result field {key!r} is not release-safe")
    if not isinstance(summary.get("root_scores_complete"), bool):
        raise ReleaseGateError(
            "Opera checked-PV root score completeness must be an exact boolean"
        )
    best_series = _list(summary.get("best_full_series"), "Opera checked-PV best series")
    if not best_series or any(not isinstance(move, str) or _UCI_MOVE.fullmatch(move) is None for move in best_series):
        raise ReleaseGateError("Opera checked-PV best series is not canonical")
    selected_root = "/".join(best_series)
    if selected_root != "b2b3":
        raise ReleaseGateError("Opera checked-PV result did not select the release-bound b2b3 root")
    principal_variation = [
        _validate_series(value, f"Opera checked-PV principal variation series {index}")
        for index, value in enumerate(
            _list(payload.get("principal_variation"), "Opera checked-PV principal variation"),
            start=1,
        )
    ]
    if (
        len(principal_variation) != 5
        or principal_variation[0].get("machine_notation") != selected_root
    ):
        raise ReleaseGateError(
            "Opera checked-PV principal variation is not the complete selected D5 line"
        )
    _validate_rooted_path_continuity(
        principal_variation,
        label="Opera checked-PV principal variation",
    )
    _signed_integer(summary.get("score"), "Opera checked-PV score")
    bounds = _list(summary.get("proof_bounds"), "Opera checked-PV proof bounds")
    if len(bounds) != 2 or any(bound not in {-1, 0, 1} for bound in bounds):
        raise ReleaseGateError("Opera checked-PV proof bounds are invalid")
    line_rejections = _integer(
        summary.get("pv_horizon_line_rejections"),
        "Opera checked-PV line rejections",
        2,
    )
    native_repairs = _integer(
        summary.get("pv_horizon_native_repairs"),
        "Opera checked-PV native repairs",
        1,
    )
    candidate_vetoes = _integer(
        summary.get("pv_horizon_candidate_vetoes"),
        "Opera checked-PV candidate vetoes",
    )
    work = _integer(summary.get("work"), "Opera checked-PV work", 1)
    if (
        line_rejections != 2
        or native_repairs != 1
        or candidate_vetoes != 1
        or native_repairs + candidate_vetoes != line_rejections
    ):
        raise ReleaseGateError("Opera checked-PV result accounting is not balanced")
    repair_policy = _validate_same_root_repair_policy(
        summary.get("same_root_repair_policy"),
        "Opera checked-PV result repair policy",
    )
    policy_vetoes = _list(
        summary.get("pv_horizon_policy_vetoes"),
        "Opera checked-PV result policy vetoes",
    )
    if len(policy_vetoes) != 1:
        raise ReleaseGateError("Opera checked-PV result must contain exactly one policy veto")
    policy_veto = _validate_policy_veto(
        policy_vetoes[0],
        "Opera checked-PV result policy veto",
    )
    for key in (
        "best_full_series",
        "score",
        "work",
        "source_fingerprint",
        "wasm_sha256",
        "kernel_sha256",
        "module_js_sha256",
        "selection_policy",
        "pv_horizon_line_rejections",
        "pv_horizon_native_repairs",
        "pv_horizon_candidate_vetoes",
        "same_root_repair_policy",
        "pv_horizon_policy_vetoes",
    ):
        if payload.get(key) != summary.get(key):
            raise ReleaseGateError(f"Opera checked-PV top-level field {key!r} drifted")
    runtime = _mapping(payload.get("runtime_receipt"), "Opera checked-PV runtime receipt")
    stats = _mapping(payload.get("stats"), "Opera checked-PV result stats")
    runtime_expected = {
        "runtime": "browser-wasm",
        "search_mode": "streaming-root-iteration",
        "requested_depth": 5,
        "completed_depth": 5,
        "canonical_replay_certified": True,
        "mate_safety_certified": True,
        "root_bound_coverage_complete": True,
        "root_bound_coverage_scope": "selection-eligible-candidates",
        "selection_policy": summary["selection_policy"],
        "selection_policy_filtered": True,
        "unfiltered_score_winner_selected": False,
        "pv_horizon_line_rejections": line_rejections,
        "pv_horizon_native_repairs": native_repairs,
        "pv_horizon_candidate_vetoes": candidate_vetoes,
        "same_root_repair_policy": repair_policy,
        "pv_horizon_policy_vetoes": [policy_veto],
        "work": work,
        "source_fingerprint": evidence.build.identity["source_fingerprint"],
        "artifact_fingerprint": evidence.build.identity["wasm_sha256"],
        "kernel_fingerprint": evidence.build.identity["kernel_sha256"],
        "module_fingerprint": evidence.build.identity["module_js_sha256"],
        "certificate_id": root_certificate["certificate_id"],
        "mate_certificate_id": mate_certificate["certificate_id"],
        "runtime_variant": "single",
        "thread_count": 1,
    }
    for key, expected in runtime_expected.items():
        if runtime.get(key) != expected:
            raise ReleaseGateError(f"Opera checked-PV runtime field {key!r} drifted")
    if (
        runtime.get("worker_count") != 8
        or not isinstance(runtime.get("initial_full_wave"), int)
        or not 1 <= int(runtime["initial_full_wave"]) <= 8
        or stats.get("coverage_complete") is not True
        or stats.get("generation_positions") != work
        or any(
            stats.get(key) != expected
            for key, expected in (
                ("pv_horizon_line_rejections", line_rejections),
                ("pv_horizon_native_repairs", native_repairs),
                ("pv_horizon_candidate_vetoes", candidate_vetoes),
            )
        )
    ):
        raise ReleaseGateError("Opera checked-PV result/stats/runtime accounting drifted")
    elapsed = _number(payload.get("elapsed_seconds"), "Opera checked-PV elapsed seconds")
    if elapsed >= 60:
        raise ReleaseGateError("Opera checked-PV D5 proof did not complete under 60 seconds")

    if evidence.safety_reserve_positions != CERTIFIED_SAFETY_RESERVE_POSITIONS:
        raise ReleaseGateError("Opera checked-PV evidence did not use the certified safety reserve")

    raw_safety_traces = _list(
        payload.get("raw_horizon_safety_traces"),
        "Opera checked-PV raw safety traces",
    )
    raw_research_traces = _list(
        payload.get("raw_horizon_research_traces"),
        "Opera checked-PV raw research traces",
    )
    raw_attestation = dict(
        _mapping(payload.get("raw_trace_attestation"), "Opera checked-PV raw trace attestation")
    )
    if set(raw_attestation) != {
        "schema",
        "horizon_safety_trace_count",
        "horizon_safety_trace_sha256",
        "horizon_research_trace_count",
        "horizon_research_trace_sha256",
    }:
        raise ReleaseGateError("Opera checked-PV raw trace attestation has the wrong shape")
    raw_safety_sha256 = _canonical_sha256(raw_safety_traces)
    raw_research_sha256 = _canonical_sha256(raw_research_traces)
    if (
        raw_attestation.get("schema") != RAW_TRACE_ATTESTATION_SCHEMA
        or raw_attestation.get("horizon_safety_trace_count") != len(raw_safety_traces)
        or raw_attestation.get("horizon_safety_trace_sha256") != raw_safety_sha256
        or raw_attestation.get("horizon_research_trace_count") != len(raw_research_traces)
        or raw_attestation.get("horizon_research_trace_sha256") != raw_research_sha256
        or not raw_safety_traces
        or not raw_research_traces
    ):
        raise ReleaseGateError("Opera checked-PV full raw trace attestation drifted")

    normalized_raw_safety: list[dict[str, Any]] = []
    for index, value in enumerate(raw_safety_traces):
        trace = _validate_trace_envelope(value, f"Opera checked-PV raw safety trace {index}")
        request = _require_fields_match(
            trace["request"],
            root_identity,
            f"Opera checked-PV raw safety request {index}",
        )
        response = _require_fields_match(
            trace["response"],
            root_identity,
            f"Opera checked-PV raw safety response {index}",
        )
        if (
            trace["worker"] not in normalized_workers
            or request.get("schema") != "spc-root-safety-task-v1"
            or response.get("status") not in {"found", "exhausted", "unknown"}
            or any(response.get(key) != requested for key, requested in request.items())
            or _number(request.get("deadline_monotonic_ms"), "raw safety deadline")
            <= float(trace["received_monotonic_ms"])
            or _number(request.get("deadline_epoch_ms"), "raw safety epoch deadline") <= 0
        ):
            raise ReleaseGateError("Opera checked-PV raw safety trace is not factory-bound")
        credit = _integer(request.get("call_work_credit"), "raw safety work credit", 1)
        used = _integer(response.get("work_used"), "raw safety work used", 1)
        memory = _integer(response.get("memory_bytes"), "raw safety memory", 1)
        peak = _integer(response.get("memory_peak_bytes"), "raw safety peak memory", memory)
        if credit > CERTIFIED_SAFETY_RESERVE_POSITIONS or used > credit or peak < memory:
            raise ReleaseGateError("Opera checked-PV raw safety work envelope drifted")
        normalized_raw_safety.append(trace)

    normalized_raw_research: list[dict[str, Any]] = []
    for index, value in enumerate(raw_research_traces):
        trace = _validate_trace_envelope(value, f"Opera checked-PV raw research trace {index}")
        request = _require_fields_match(
            trace["request"],
            root_identity,
            f"Opera checked-PV raw research request {index}",
        )
        response = _require_fields_match(
            trace["response"],
            root_identity,
            f"Opera checked-PV raw research response {index}",
        )
        if (
            trace["worker"] not in normalized_workers
            or request.get("schema") != "spc-root-horizon-research-task-v1"
            or response.get("schema") != "spc-root-horizon-research-result-v1"
            or response.get("status") != "complete"
            or response.get("bound") != "exact"
        ):
            raise ReleaseGateError("Opera checked-PV raw research trace is not factory-bound")
        normalized_raw_research.append(trace)

    safety_map = _mapping(payload.get("horizon_safety_traces"), "Opera checked-PV safety traces")
    repair_map = _mapping(payload.get("certified_repair_traces"), "Opera checked-PV repair traces")
    if set(safety_map) != {"f3"} or set(repair_map) != {"f3"}:
        raise ReleaseGateError("Opera checked-PV counted trace fixtures are incomplete")
    f3_safety = _validate_raw_safety_trace(
        safety_map["f3"],
        expected_root="f2f3",
        expected_unsafe_horizon=None,
        expected_child_fen=None,
        root_identity=root_identity,
        prefix_identity=prefix_identity,
        maximum_work=100_000_000,
        label="Opera checked-PV f3 safety",
    )
    f3_repair = _validate_horizon_research_trace(
        repair_map["f3"],
        expected_root="f2f3",
        root_identity=root_identity,
        maximum_work=100_000_000,
        require_newest_hit=True,
        require_warm_exact=False,
        label="Opera checked-PV f3 repair",
    )
    if (
        f3_safety["worker"] not in normalized_workers
        or f3_repair["worker"] not in normalized_workers
        or not _trace_occurs(f3_safety, normalized_raw_safety)
        or not _trace_occurs(f3_repair, normalized_raw_research)
    ):
        raise ReleaseGateError("Opera checked-PV counted f3 repair is absent from the raw trace")
    _validate_safety_repair_crosslink(
        f3_safety,
        f3_repair,
        expected_root="f2f3",
        label="Opera checked-PV f3",
    )
    f3_candidate = f3_repair["request"].get("candidate_identity")
    if policy_veto.get("candidate_identity") != f3_candidate:
        raise ReleaseGateError("Opera checked-PV threshold veto is not bound to the repaired f3 candidate")

    f3_warm = _validate_horizon_research_trace(
        payload.get("f3_warm_recertification_trace"),
        expected_root="f2f3",
        root_identity=root_identity,
        maximum_work=100_000_000,
        require_newest_hit=False,
        require_warm_exact=True,
        label="Opera checked-PV f3 warm recertification",
    )
    repair_request = _mapping(f3_repair["request"], "Opera checked-PV f3 repair request")
    repair_response = _mapping(f3_repair["response"], "Opera checked-PV f3 repair response")
    warm_request = _mapping(f3_warm["request"], "Opera checked-PV f3 warm request")
    warm_response = _mapping(f3_warm["response"], "Opera checked-PV f3 warm response")
    if (
        f3_warm["worker"] not in normalized_workers
        or not _trace_occurs(f3_warm, normalized_raw_research)
        or f3_warm["worker"] != f3_repair["worker"]
        or f3_warm["request_sequence"] <= f3_repair["request_sequence"]
        or f3_warm["posted_monotonic_ms"] < f3_repair["received_monotonic_ms"]
        or any(
            warm_request.get(key) != repair_request.get(key)
            for key in (
                "session_id",
                "request_id",
                "iteration_id",
                "generation",
                "deadline_monotonic_ms",
                "deadline_epoch_ms",
                "enumeration_identity",
                "candidate_identity",
                "order_index",
                "order_key",
                "safety_revision",
                "horizon_proofs",
            )
        )
        or warm_request.get("task_id") == repair_request.get("task_id")
        or warm_request.get("incumbent_epoch")
        not in {
            repair_request.get("incumbent_epoch"),
            int(repair_request.get("incumbent_epoch")) + 1,
        }
        or int(warm_request.get("remaining_time_ms"))
        > int(repair_request.get("remaining_time_ms"))
        or any(
            warm_response.get(key) != repair_response.get(key)
            for key in (
                "root_series",
                "child_pv",
                "score",
                "proof_bounds",
                "horizon_proof_set_identity",
            )
        )
    ):
        raise ReleaseGateError("Opera checked-PV f3 warm repair recertification drifted")

    witness = dict(
        _mapping(
            payload.get("threshold_veto_witness"),
            "Opera checked-PV threshold-veto witness",
        )
    )
    if set(witness) != {
        "schema",
        "root_series",
        "candidate_identity",
        "first_repair_request_sequence",
        "second_safety_request_sequence",
        "first_proof_sha256",
        "second_proof_sha256",
        "proof_count_2_research_dispatched",
        "policy_veto",
        "second_safety_trace",
    }:
        raise ReleaseGateError("Opera checked-PV threshold-veto witness has the wrong shape")
    if (
        witness.get("schema") != THRESHOLD_VETO_WITNESS_SCHEMA
        or witness.get("root_series") != "f2f3"
        or witness.get("candidate_identity") != f3_candidate
        or witness.get("policy_veto") != policy_veto
        or witness.get("proof_count_2_research_dispatched") is not False
    ):
        raise ReleaseGateError("Opera checked-PV threshold-veto witness is not policy-bound")
    first_repair = f3_repair
    second_safety = _validate_raw_safety_trace(
        witness.get("second_safety_trace"),
        expected_root="f2f3",
        expected_unsafe_horizon=None,
        expected_child_fen=None,
        root_identity=root_identity,
        prefix_identity=prefix_identity,
        maximum_work=100_000_000,
        label="Opera checked-PV f3 threshold-veto safety",
    )
    if second_safety["worker"] not in normalized_workers:
        raise ReleaseGateError(
            "Opera checked-PV f3 threshold-veto safety Worker was not created by the bound factory"
        )
    first_request = _mapping(first_repair["request"], "Opera checked-PV f3 first repair request")
    second_request = _mapping(second_safety["request"], "Opera checked-PV f3 second safety request")
    second_candidate = _mapping(
        second_request.get("candidate"),
        "Opera checked-PV f3 second safety candidate",
    )
    if (
        witness.get("first_repair_request_sequence") != first_repair["request_sequence"]
        or witness.get("second_safety_request_sequence") != second_safety["request_sequence"]
        or not _trace_occurs(second_safety, normalized_raw_safety)
        or second_safety["request_sequence"] <= f3_warm["request_sequence"]
        or second_safety["posted_monotonic_ms"] < f3_warm["received_monotonic_ms"]
        or second_safety["worker"] != first_repair["worker"]
        or second_safety["worker"] != f3_warm["worker"]
        or second_request.get("candidate_identity") != f3_candidate
        or second_candidate.get("candidate_identity") != f3_candidate
        or second_candidate.get("order_key") != "f2f3"
        or second_candidate.get("order_index") != first_request.get("order_index")
        or any(
            second_request.get(key) != first_request.get(key)
            for key in (
                "session_id",
                "request_id",
                "iteration_id",
                "generation",
                "deadline_monotonic_ms",
                "deadline_epoch_ms",
                "candidate_identity",
            )
        )
        or second_request.get("safety_revision") != warm_request.get("safety_revision")
        or second_request.get("incumbent_epoch") != warm_request.get("incumbent_epoch")
        or int(second_request.get("remaining_time_ms"))
        > int(warm_request.get("remaining_time_ms"))
        or second_candidate.get("score") != warm_response.get("score")
        or second_candidate.get("proof_bounds") != warm_response.get("proof_bounds")
        or second_candidate.get("child_pv") != warm_response.get("child_pv")
    ):
        raise ReleaseGateError(
            "Opera checked-PV second f3 proof is not ordered after the first same-root repair"
        )
    first_proofs = _list(
        first_request.get("horizon_proofs"),
        "Opera checked-PV f3 first repair proofs",
    )
    if len(first_proofs) != 1:
        raise ReleaseGateError("Opera checked-PV f3 first repair did not carry exactly one proof")
    first_proof = _validate_retained_proof(
        first_proofs[0],
        root="f2f3",
        depth=4,
        label="Opera checked-PV f3 first retained proof",
    )
    first_root_series = _mapping(
        _list(first_proof.get("rooted_path"), "Opera checked-PV f3 first proof path")[0],
        "Opera checked-PV f3 root series",
    )
    second_proof = _retained_proof_from_raw_safety(
        second_safety,
        root_series=first_root_series,
        expected_root="f2f3",
        label="Opera checked-PV f3 threshold-veto safety",
    )
    first_proof_sha256 = _canonical_sha256(first_proof)
    second_proof_sha256 = _canonical_sha256(second_proof)
    if (
        witness.get("first_proof_sha256") != first_proof_sha256
        or witness.get("second_proof_sha256") != second_proof_sha256
        or HEX_64.fullmatch(str(first_proof_sha256)) is None
        or HEX_64.fullmatch(str(second_proof_sha256)) is None
        or first_proof_sha256 == second_proof_sha256
    ):
        raise ReleaseGateError("Opera checked-PV f3 threshold-veto proofs are not distinct and hash-bound")
    f3_found_safety = [
        trace
        for trace in normalized_raw_safety
        if _mapping(trace.get("request"), "Opera checked-PV raw f3 safety request")
        .get("candidate", {})
        .get("order_key")
        == "f2f3"
        and _mapping(trace.get("response"), "Opera checked-PV raw f3 safety response").get(
            "status"
        )
        == "found"
    ]
    if (
        len(f3_found_safety) != 2
        or not _trace_occurs(f3_safety, f3_found_safety)
        or not _trace_occurs(second_safety, f3_found_safety)
    ):
        raise ReleaseGateError("Opera checked-PV semantic f3 rejection count is not exactly two")
    for index, trace_value in enumerate(normalized_raw_research):
        trace = _validate_trace_envelope(
            trace_value,
            f"Opera checked-PV raw research trace {index}",
        )
        request = _mapping(trace.get("request"), f"Opera checked-PV raw research request {index}")
        if request.get("schema") != "spc-root-horizon-research-task-v1":
            raise ReleaseGateError("Opera checked-PV raw research trace is not horizon research")
        proofs = _list(request.get("horizon_proofs"), f"Opera checked-PV raw research proofs {index}")
        if request.get("candidate_identity") == f3_candidate and len(proofs) >= 2:
            raise ReleaseGateError(
                "Opera checked-PV dispatched f3 proof-count-2 research instead of threshold-vetoing"
            )
    selected_witness = dict(
        _mapping(
            payload.get("selected_d5_horizon_certification_witness"),
            "Opera checked-PV selected D5 horizon witness",
        )
    )
    expected_selected_keys = {
        "schema",
        "fixture_id",
        "selected_root_series",
        "candidate_identity",
        "owner_worker_id",
        "principal_variation_sha256",
        "selected_series5_semantic_sha256",
        "known_adverse_series5_semantic_sha256",
        "known_adverse_present",
        "boundary_probe_order",
        "expected_nonterminal_rooted_path_lengths",
        "boundary_probes",
        "found_stop_witness",
        "unknown_fail_closed_witness_sha256",
        "safety_work_used",
        "safety_call_work_credit",
    }
    if set(selected_witness) != expected_selected_keys:
        raise ReleaseGateError(
            "Opera checked-PV selected D5 horizon witness has the wrong shape"
        )
    principal_variation_sha256 = _canonical_sha256(principal_variation)
    selected_series5 = principal_variation[-1]
    selected_series5_semantic_sha256 = _canonical_sha256(
        _series_semantic(selected_series5, series_index=5)
    )
    known_adverse_series5 = _validate_series(
        {
            "moves": ["e1f2", "d1g4", "f2e3", "g1h3", "g4h5"],
            "machine_notation": "e1f2/d1g4/f2e3/g1h3/g4h5",
            "child_boundary": {
                "fen": "rnbq1bnr/pppp1kpp/4p3/7Q/2B5/1P2K2N/PBPP2PP/RN5R b - - 4 7",
                "board_fen": "rnbq1bnr/pppp1kpp/4p3/7Q/2B5/1P2K2N/PBPP2PP/RN5R b - - 4 7",
                "series": 6,
                "series_number": 6,
                "side_to_move": "black",
                "quiet_series": 0,
                "quiet_draw_pending": False,
                "ep_targets": [],
                "progressive_ep": [],
                "promoted_hex": "0000000000000000",
                "chess960": False,
            },
            "outcome": None,
            "ended_by_check": True,
            "transposition_count": 1,
        },
        "Opera checked-PV known adverse b3 series 5",
    )
    _validate_rooted_path_continuity(
        [*principal_variation[:4], known_adverse_series5],
        label="Opera checked-PV known adverse b3 fixture",
    )
    known_adverse_semantic_sha256 = _canonical_sha256(
        _series_semantic(known_adverse_series5, series_index=5)
    )
    if selected_series5_semantic_sha256 == known_adverse_semantic_sha256:
        raise ReleaseGateError("Opera checked-PV selected b3 retained the known adverse series 5")

    expected_ladder = [
        (5, 4, "pv-horizon", principal_variation[4], principal_variation[3]["child_boundary"]),
        (3, 2, "pv-horizon", principal_variation[2], principal_variation[1]["child_boundary"]),
        (1, 0, "root-child", principal_variation[0], _boundary_from_state(ProgressiveState.initial())),
    ]
    if (
        selected_witness.get("boundary_probe_order") != BOUNDARY_LADDER_ORDER
        or selected_witness.get("expected_nonterminal_rooted_path_lengths") != [5, 3, 1]
    ):
        raise ReleaseGateError(
            "Opera checked-PV selected D5 boundary ladder is not leaf-first odd-prefix order"
        )
    probe_records = _list(
        selected_witness.get("boundary_probes"),
        "Opera checked-PV selected D5 boundary probes",
    )
    if len(probe_records) != len(expected_ladder):
        raise ReleaseGateError("Opera checked-PV selected D5 boundary ladder is incomplete")

    expected_candidate_keys = {
        "candidate_identity",
        "order_index",
        "order_key",
        "root_series",
        "score",
        "terminal",
        "owner_worker_id",
        "proof_bounds",
        "child_pv",
        "safety_override",
        "mate_claim_quarantined",
    }
    expected_probe_keys = {
        "schema",
        "rooted_path_length",
        "scope",
        "replay_index",
        "request_sequence",
        "status",
        "call_work_credit",
        "work_used",
        "cumulative_work_before",
        "cumulative_work_after",
        "cache",
    }
    selected_ladder: list[dict[str, Any]] = []
    cumulative_work = 0
    prior_trace: dict[str, Any] | None = None
    prior_request: Mapping[str, Any] | None = None
    prior_candidate: Mapping[str, Any] | None = None
    seen_sequences: set[int] = set()
    for ladder_index, (probe_value, expected) in enumerate(zip(probe_records, expected_ladder)):
        rooted_length, replay_index, scope, expected_series, parent_boundary = expected
        probe = dict(_mapping(probe_value, f"Opera checked-PV boundary probe {ladder_index}"))
        if set(probe) != expected_probe_keys:
            raise ReleaseGateError("Opera checked-PV boundary probe has the wrong shape")
        sequence = _integer(
            probe.get("request_sequence"),
            f"Opera checked-PV boundary probe {ladder_index} request sequence",
            1,
        )
        if sequence in seen_sequences:
            raise ReleaseGateError("Opera checked-PV boundary ladder reused a Worker request")
        seen_sequences.add(sequence)
        matches = [trace for trace in normalized_raw_safety if trace["request_sequence"] == sequence]
        if len(matches) != 1:
            raise ReleaseGateError("Opera checked-PV boundary probe is not uniquely raw-trace bound")
        remaining_probes = len(expected_ladder) - ladder_index
        remaining_credit = CERTIFIED_SAFETY_RESERVE_POSITIONS - cumulative_work
        expected_credit = (
            remaining_credit
            if scope == "root-child"
            else min(PV_HORIZON_MATE_WORK_LIMIT, remaining_credit - (remaining_probes - 1))
        )
        trace = _validate_exhausted_safety_trace(
            matches[0],
            expected_root=selected_root,
            expected_series=expected_series,
            expected_parent_boundary=parent_boundary,
            expected_replay_suffix=f"{scope}-replay-{replay_index}",
            expected_call_work_credit=expected_credit,
            root_identity=root_identity,
            prefix_identity=prefix_identity,
            maximum_work=CERTIFIED_SAFETY_RESERVE_POSITIONS,
            label=f"Opera checked-PV selected boundary probe {rooted_length}",
        )
        request = _mapping(trace["request"], f"selected boundary {rooted_length} request")
        response = _mapping(trace["response"], f"selected boundary {rooted_length} response")
        candidate = _mapping(request.get("candidate"), f"selected boundary {rooted_length} candidate")
        cache = dict(_mapping(probe.get("cache"), f"selected boundary {rooted_length} cache"))
        work_used = _integer(response.get("work_used"), f"selected boundary {rooted_length} work", 1)
        if (
            probe.get("schema") != BOUNDARY_LADDER_PROBE_SCHEMA
            or probe.get("rooted_path_length") != rooted_length
            or probe.get("scope") != scope
            or probe.get("replay_index") != replay_index
            or probe.get("status") != "exhausted"
            or probe.get("call_work_credit") != expected_credit
            or probe.get("work_used") != work_used
            or probe.get("cumulative_work_before") != cumulative_work
            or probe.get("cumulative_work_after") != cumulative_work + work_used
            or cache != {
                "schema": "spc-root-mate-proof-cache-receipt-v1",
                "hit": False,
                "proof_status": "exhausted",
                "evidence": "native-worker-dispatch",
            }
            or set(candidate) != expected_candidate_keys
            or candidate.get("owner_worker_id")
            != _mapping(trace.get("worker"), "selected boundary Worker").get("channel_id")
            or candidate.get("score") != summary.get("score")
            or candidate.get("proof_bounds") != summary.get("proof_bounds")
            or candidate.get("child_pv") != principal_variation[1:]
            or candidate.get("terminal") is not False
            or candidate.get("safety_override") is not False
            or candidate.get("mate_claim_quarantined") is not False
        ):
            raise ReleaseGateError(
                "Opera checked-PV selected boundary probe drifted from its replay, work, or cache receipt"
            )
        if prior_trace is not None and prior_request is not None and prior_candidate is not None:
            if (
                trace["request_sequence"] <= prior_trace["request_sequence"]
                or trace["posted_monotonic_ms"] < prior_trace["received_monotonic_ms"]
                or trace["worker"] != prior_trace["worker"]
                or any(
                    request.get(key) != prior_request.get(key)
                    for key in (
                        "session_id", "request_id", "iteration_id", "generation",
                        "safety_revision", "incumbent_epoch", "deadline_monotonic_ms",
                        "deadline_epoch_ms", "candidate_identity",
                    )
                )
                or int(request.get("remaining_time_ms"))
                > int(prior_request.get("remaining_time_ms"))
                or any(
                    candidate.get(key) != prior_candidate.get(key)
                    for key in (
                        "candidate_identity", "order_index", "order_key", "score",
                        "terminal", "owner_worker_id", "proof_bounds", "child_pv",
                        "safety_override", "mate_claim_quarantined",
                    )
                )
            ):
                raise ReleaseGateError(
                    "Opera checked-PV boundary ladder is not one ordered Worker search"
                )
        elif request.get("generation") != 1 or request.get("session_id") != 1:
            raise ReleaseGateError("Opera checked-PV boundary ladder has the wrong root session")
        cumulative_work += work_used
        selected_ladder.append(trace)
        prior_trace = trace
        prior_request = request
        prior_candidate = candidate

    known_adverse_present = any(
        _mapping(trace.get("request"), "Opera checked-PV raw selected safety request")
        .get("candidate", {})
        .get("order_key")
        == selected_root
        and (
            _mapping(trace.get("request"), "Opera checked-PV raw selected safety request")
            .get("candidate", {})
            .get("root_series", {})
            .get("machine_notation")
            == known_adverse_series5.get("machine_notation")
            or _mapping(trace.get("request"), "Opera checked-PV raw selected safety request")
            .get("authoritative_child_boundary", {})
            .get("fen")
            == _mapping(
                known_adverse_series5.get("child_boundary"),
                "Opera checked-PV known adverse child",
            ).get("fen")
        )
        for trace in normalized_raw_safety
    )
    if known_adverse_present:
        raise ReleaseGateError("Opera checked-PV selected b3 trace contains the known adverse series 5")

    first_selected_request = _mapping(
        selected_ladder[0]["request"],
        "Opera checked-PV first selected boundary request",
    )
    first_selected_worker = _mapping(
        selected_ladder[0]["worker"],
        "Opera checked-PV first selected boundary Worker",
    )
    found_stop = dict(
        _mapping(
            selected_witness.get("found_stop_witness"),
            "Opera checked-PV FOUND-stop witness",
        )
    )
    expected_found_stop_keys = {
        "schema",
        "selected_root_series",
        "candidate_identity",
        "found_request_sequence",
        "found_rooted_path_length",
        "found_status",
        "next_research_request_sequence",
        "shallower_worker_dispatches_before_repair",
        "action",
    }
    f3_safety_request = _mapping(f3_safety["request"], "Opera checked-PV f3 safety request")
    shallower_before_repair = [
        trace
        for trace in normalized_raw_safety
        if f3_safety["request_sequence"] < trace["request_sequence"] < f3_repair["request_sequence"]
        and _mapping(trace.get("request"), "Opera checked-PV intervening safety request").get(
            "candidate_identity"
        )
        == f3_safety_request.get("candidate_identity")
        and _mapping(trace.get("request"), "Opera checked-PV intervening safety request").get(
            "safety_revision"
        )
        == f3_safety_request.get("safety_revision")
    ]
    if (
        set(found_stop) != expected_found_stop_keys
        or found_stop.get("schema") != FOUND_STOP_WITNESS_SCHEMA
        or found_stop.get("selected_root_series") != "f2f3"
        or found_stop.get("candidate_identity") != f3_safety_request.get("candidate_identity")
        or found_stop.get("found_request_sequence") != f3_safety["request_sequence"]
        or found_stop.get("found_rooted_path_length")
        != len(_list(first_proof.get("rooted_path"), "Opera checked-PV first proof path"))
        or found_stop.get("found_status") != "found"
        or found_stop.get("next_research_request_sequence") != f3_repair["request_sequence"]
        or found_stop.get("shallower_worker_dispatches_before_repair") != 0
        or shallower_before_repair
        or found_stop.get("action") != "stop-and-reject-selected-line"
    ):
        raise ReleaseGateError("Opera checked-PV FOUND did not stop the boundary ladder")

    unknown_evidence = dict(
        _mapping(
            payload.get("unknown_fail_closed_evidence"),
            "Opera checked-PV UNKNOWN fail-closed evidence",
        )
    )
    if set(unknown_evidence) != {
        "schema",
        "worker_factory_calls",
        "trusted_worker_events_only",
        "raw_horizon_safety_traces",
        "raw_horizon_research_traces",
        "result_summary",
        "witness",
    } or (
        unknown_evidence.get("schema") != UNKNOWN_FAIL_CLOSED_EVIDENCE_SCHEMA
        or unknown_evidence.get("trusted_worker_events_only") is not True
    ):
        raise ReleaseGateError("Opera checked-PV UNKNOWN evidence has the wrong shape")

    unknown_workers = [
        _worker_identity(value, f"Opera checked-PV UNKNOWN Worker {index}")
        for index, value in enumerate(
            _list(
                unknown_evidence.get("worker_factory_calls"),
                "Opera checked-PV UNKNOWN Worker factory calls",
            )
        )
    ]
    if unknown_workers != normalized_workers:
        raise ReleaseGateError(
            "Opera checked-PV UNKNOWN run did not use a fresh exact native Worker factory"
        )

    unknown_raw_safety = _list(
        unknown_evidence.get("raw_horizon_safety_traces"),
        "Opera checked-PV UNKNOWN raw safety traces",
    )
    unknown_raw_research = _list(
        unknown_evidence.get("raw_horizon_research_traces"),
        "Opera checked-PV UNKNOWN raw research traces",
    )
    unknown_witness = dict(
        _mapping(
            unknown_evidence.get("witness"),
            "Opera checked-PV UNKNOWN fail-closed witness",
        )
    )
    unknown_attestation = dict(
        _mapping(
            unknown_witness.get("raw_trace_attestation"),
            "Opera checked-PV UNKNOWN raw trace attestation",
        )
    )
    if (
        set(unknown_attestation)
        != {
            "schema",
            "horizon_safety_trace_count",
            "horizon_safety_trace_sha256",
            "horizon_research_trace_count",
            "horizon_research_trace_sha256",
        }
        or unknown_attestation.get("schema") != RAW_TRACE_ATTESTATION_SCHEMA
        or unknown_attestation.get("horizon_safety_trace_count")
        != len(unknown_raw_safety)
        or unknown_attestation.get("horizon_safety_trace_sha256")
        != _canonical_sha256(unknown_raw_safety)
        or unknown_attestation.get("horizon_research_trace_count")
        != len(unknown_raw_research)
        or unknown_attestation.get("horizon_research_trace_sha256")
        != _canonical_sha256(unknown_raw_research)
        or not unknown_raw_safety
    ):
        raise ReleaseGateError("Opera checked-PV UNKNOWN raw trace attestation drifted")

    normalized_unknown_safety: list[dict[str, Any]] = []
    for index, value in enumerate(unknown_raw_safety):
        trace = _validate_trace_envelope(
            value,
            f"Opera checked-PV UNKNOWN raw safety trace {index}",
        )
        request = _require_fields_match(
            trace["request"],
            root_identity,
            f"Opera checked-PV UNKNOWN raw safety request {index}",
        )
        response = _require_fields_match(
            trace["response"],
            root_identity,
            f"Opera checked-PV UNKNOWN raw safety response {index}",
        )
        credit = _integer(request.get("call_work_credit"), "UNKNOWN safety credit", 1)
        work_used = _integer(response.get("work_used"), "UNKNOWN safety work", 0)
        if (
            trace["worker"] not in unknown_workers
            or request.get("schema") != "spc-root-safety-task-v1"
            or response.get("status") not in {"found", "exhausted", "unknown"}
            or any(response.get(key) != requested for key, requested in request.items())
            or work_used > credit
            or credit > CERTIFIED_SAFETY_RESERVE_POSITIONS
            or _number(request.get("deadline_monotonic_ms"), "UNKNOWN safety deadline")
            <= float(trace["received_monotonic_ms"])
        ):
            raise ReleaseGateError("Opera checked-PV UNKNOWN safety trace is not factory-bound")
        normalized_unknown_safety.append(trace)

    normalized_unknown_research: list[dict[str, Any]] = []
    for index, value in enumerate(unknown_raw_research):
        trace = _validate_trace_envelope(
            value,
            f"Opera checked-PV UNKNOWN raw research trace {index}",
        )
        request = _require_fields_match(
            trace["request"],
            root_identity,
            f"Opera checked-PV UNKNOWN raw research request {index}",
        )
        response = _require_fields_match(
            trace["response"],
            root_identity,
            f"Opera checked-PV UNKNOWN raw research response {index}",
        )
        if (
            trace["worker"] not in unknown_workers
            or request.get("schema") != "spc-root-horizon-research-task-v1"
            or response.get("schema") != "spc-root-horizon-research-result-v1"
        ):
            raise ReleaseGateError("Opera checked-PV UNKNOWN research trace is not factory-bound")
        normalized_unknown_research.append(trace)

    expected_unknown_witness_keys = {
        "schema",
        "evidence_scope",
        "selection_policy",
        "selected_root_series",
        "candidate_identity",
        "owner_worker_id",
        "fault_injection",
        "deeper_exhausted_request_sequence",
        "unknown_request_sequence",
        "unknown_rooted_path_length",
        "unknown_status",
        "unknown_work_used",
        "shallower_worker_dispatches_after_unknown",
        "requested_depth",
        "completed_depth",
        "interruption_code",
        "cache_entry_absent",
        "unknown_action",
        "shallower_probe_action",
        "cache_policy",
        "raw_trace_attestation",
    }
    injection = dict(
        _mapping(
            unknown_witness.get("fault_injection"),
            "Opera checked-PV UNKNOWN constrained-credit injection",
        )
    )
    unknown_result = dict(
        _mapping(
            unknown_evidence.get("result_summary"),
            "Opera checked-PV UNKNOWN result summary",
        )
    )
    if (
        set(unknown_witness) != expected_unknown_witness_keys
        or unknown_witness.get("schema") != UNKNOWN_FAIL_CLOSED_WITNESS_SCHEMA
        or unknown_witness.get("evidence_scope")
        != "observed-native-worker-constrained-credit"
        or unknown_witness.get("selection_policy") != CHECKED_PV_SELECTION_POLICY
        or unknown_witness.get("selected_root_series") != selected_root
        or set(injection)
        != {
            "schema",
            "target_rooted_path_length",
            "original_call_work_credit",
            "constrained_call_work_credit",
            "injection_count",
        }
        or injection.get("schema") != UNKNOWN_CREDIT_CONSTRAINT_SCHEMA
        or injection.get("target_rooted_path_length") != 3
        or injection.get("constrained_call_work_credit") != 1
        or injection.get("injection_count") != 1
        or unknown_witness.get("unknown_rooted_path_length") != 3
        or unknown_witness.get("unknown_status") != "unknown"
        or unknown_witness.get("unknown_work_used") != 1
        or unknown_witness.get("shallower_worker_dispatches_after_unknown") != 0
        or unknown_witness.get("requested_depth") != 5
        or unknown_witness.get("completed_depth") != 4
        or unknown_witness.get("interruption_code") != "root-safety-unknown"
        or unknown_witness.get("cache_entry_absent") is not True
        or unknown_witness.get("unknown_action") != "stop-and-discard-current-depth"
        or unknown_witness.get("shallower_probe_action") != "forbidden-after-unknown"
        or unknown_witness.get("cache_policy") != "never-store-unknown"
        or unknown_result
        != {
            "requested_depth": 5,
            "completed_depth": 4,
            "timed_out": False,
            "work_limit_reached": False,
            "interruption_code": "root-safety-unknown",
            "selection_policy": CHECKED_PV_SELECTION_POLICY,
        }
    ):
        raise ReleaseGateError("Opera checked-PV UNKNOWN witness is not observed fail-closed evidence")

    deep_sequence = _integer(
        unknown_witness.get("deeper_exhausted_request_sequence"),
        "Opera checked-PV UNKNOWN deeper request sequence",
        1,
    )
    unknown_sequence = _integer(
        unknown_witness.get("unknown_request_sequence"),
        "Opera checked-PV UNKNOWN request sequence",
        deep_sequence + 1,
    )
    deep_matches = [
        trace for trace in normalized_unknown_safety
        if trace["request_sequence"] == deep_sequence
    ]
    unknown_matches = [
        trace for trace in normalized_unknown_safety
        if trace["request_sequence"] == unknown_sequence
    ]
    if len(deep_matches) != 1 or len(unknown_matches) != 1:
        raise ReleaseGateError("Opera checked-PV UNKNOWN ladder traces are not uniquely bound")
    deep_trace = _validate_exhausted_safety_trace(
        deep_matches[0],
        expected_root=selected_root,
        expected_series=principal_variation[4],
        expected_parent_boundary=principal_variation[3]["child_boundary"],
        expected_replay_suffix="pv-horizon-replay-4",
        expected_call_work_credit=PV_HORIZON_MATE_WORK_LIMIT,
        root_identity=root_identity,
        prefix_identity=prefix_identity,
        maximum_work=CERTIFIED_SAFETY_RESERVE_POSITIONS,
        label="Opera checked-PV UNKNOWN deeper boundary probe",
    )
    unknown_trace = json.loads(json.dumps(unknown_matches[0]))
    actual_unknown_response = _mapping(
        unknown_trace.get("response"),
        "Opera checked-PV UNKNOWN boundary response",
    )
    if actual_unknown_response.get("status") != "unknown":
        raise ReleaseGateError("Opera checked-PV UNKNOWN boundary did not return UNKNOWN")
    unknown_trace["response"]["status"] = "exhausted"
    _validate_exhausted_safety_trace(
        unknown_trace,
        expected_root=selected_root,
        expected_series=principal_variation[2],
        expected_parent_boundary=principal_variation[1]["child_boundary"],
        expected_replay_suffix="pv-horizon-replay-2",
        expected_call_work_credit=1,
        root_identity=root_identity,
        prefix_identity=prefix_identity,
        maximum_work=CERTIFIED_SAFETY_RESERVE_POSITIONS,
        label="Opera checked-PV UNKNOWN internal boundary probe",
    )
    deep_request = _mapping(deep_trace["request"], "Opera checked-PV UNKNOWN deep request")
    unknown_request = _mapping(
        unknown_matches[0]["request"],
        "Opera checked-PV UNKNOWN internal request",
    )
    original_credit = CERTIFIED_SAFETY_RESERVE_POSITIONS - int(
        _mapping(deep_trace["response"], "Opera checked-PV UNKNOWN deep response")["work_used"]
    ) - 1
    shallower_after_unknown = [
        trace
        for trace in normalized_unknown_safety
        if trace["request_sequence"] > unknown_sequence
        and _mapping(trace["request"], "Opera checked-PV UNKNOWN later request").get(
            "candidate_identity"
        )
        == unknown_request.get("candidate_identity")
        and str(
            _mapping(
                _mapping(trace["request"], "Opera checked-PV UNKNOWN later request").get(
                    "authoritative_root_replay"
                ),
                "Opera checked-PV UNKNOWN later replay",
            ).get("request_id", "")
        ).endswith(":root-child-replay-0")
    ]
    if (
        deep_trace["worker"] != unknown_matches[0]["worker"]
        or unknown_matches[0]["posted_monotonic_ms"] < deep_trace["received_monotonic_ms"]
        or injection.get("original_call_work_credit") != original_credit
        or unknown_witness.get("candidate_identity")
        != unknown_request.get("candidate_identity")
        or unknown_witness.get("owner_worker_id")
        != _mapping(unknown_matches[0]["worker"], "Opera checked-PV UNKNOWN Worker").get(
            "channel_id"
        )
        or any(
            deep_request.get(key) != unknown_request.get(key)
            for key in (
                "session_id",
                "request_id",
                "iteration_id",
                "generation",
                "safety_revision",
                "incumbent_epoch",
                "candidate_identity",
            )
        )
        or shallower_after_unknown
        or selected_witness.get("unknown_fail_closed_witness_sha256")
        != _canonical_sha256(unknown_witness)
    ):
        raise ReleaseGateError("Opera checked-PV UNKNOWN did not stop and discard D5 exactly")
    if (
        selected_witness.get("schema") != SELECTED_D5_HORIZON_CERTIFICATION_SCHEMA
        or selected_witness.get("fixture_id") != SELECTED_D5_FIXTURE_ID
        or selected_witness.get("selected_root_series") != selected_root
        or selected_witness.get("candidate_identity")
        != first_selected_request.get("candidate_identity")
        or selected_witness.get("owner_worker_id")
        != first_selected_worker.get("channel_id")
        or selected_witness.get("principal_variation_sha256")
        != principal_variation_sha256
        or selected_witness.get("selected_series5_semantic_sha256")
        != selected_series5_semantic_sha256
        or selected_witness.get("known_adverse_series5_semantic_sha256")
        != known_adverse_semantic_sha256
        or selected_witness.get("known_adverse_present") is not False
        or found_stop != selected_witness.get("found_stop_witness")
        or selected_witness.get("safety_work_used") != cumulative_work
        or selected_witness.get("safety_call_work_credit")
        != CERTIFIED_SAFETY_RESERVE_POSITIONS
        or cumulative_work > CERTIFIED_SAFETY_RESERVE_POSITIONS
    ):
        raise ReleaseGateError(
            "Opera checked-PV selected D5 boundary-ladder witness digest or identity drifted"
        )

    return OperaCheckedHorizonEvidence(
        receipt=receipt,
        local_checkout_asset_set_sha256=asset_commitment,
        elapsed_seconds=elapsed,
        work=work,
        line_rejections=line_rejections,
        native_repairs=native_repairs,
        candidate_vetoes=candidate_vetoes,
        selected_root_series=selected_root,
        principal_variation_sha256=principal_variation_sha256,
        selected_fixture_id=SELECTED_D5_FIXTURE_ID,
        known_adverse_excluded=True,
        selected_boundary_ladder_certified=True,
        found_stop_observed=True,
        unknown_fail_closed_observed=True,
        selected_horizon_exhaustively_certified=True,
        selected_root_child_exhaustively_certified=True,
        raw_safety_trace_count=len(raw_safety_traces),
        raw_safety_trace_sha256=raw_safety_sha256,
        raw_research_trace_count=len(raw_research_traces),
        raw_research_trace_sha256=raw_research_sha256,
        raw_trace_attestation=raw_attestation,
        selected_d5_horizon_certification_witness=selected_witness,
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _directory_records(directory: Path) -> tuple[list[dict[str, object]], str]:
    records = [
        {
            "path": path.relative_to(directory).as_posix(),
            "sha256": _sha256_file(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(
            (path for path in directory.rglob("*") if path.is_file()),
            key=lambda item: item.relative_to(directory).as_posix(),
        )
    ]
    return records, _canonical_sha256(records)


def _browser_runtime_records(source_package: Path) -> tuple[list[dict[str, object]], str]:
    static_directory = source_package.resolve() / "web" / "static"
    records = []
    for label, filename in SAFE_RESELECTION_RUNTIME_ASSETS.items():
        path = static_directory / filename
        if not path.is_file():
            raise FileNotFoundError(f"browser runtime asset is missing: {path}")
        records.append(
            {
                "label": label,
                "path": filename,
                "sha256": _sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    records.sort(key=lambda item: str(item["label"]))
    return records, _canonical_sha256(records)


def stage_release_candidate(
    evidence: ValidatedEvidence,
    certificates: Mapping[str, Mapping[str, Any]],
    *,
    source_package: Path,
    output: Path,
    maximum_seconds: float,
    default_seconds: float,
) -> Mapping[str, Any]:
    """Stage immutable core-seven bytes for local Opera attestation only."""
    maximum_value = float(maximum_seconds)
    default_value = float(default_seconds)
    maximum_seconds = int(maximum_value) if maximum_value.is_integer() else maximum_value
    default_seconds = int(default_value) if default_value.is_integer() else default_value
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"release candidate output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}.", dir=output.parent) as name:
        staging = Path(name) / "candidate"
        staging.mkdir()
        certificate_directory = staging / "certificates"
        evidence_directory = staging / "evidence"
        certificate_directory.mkdir()
        evidence_directory.mkdir()
        certificate_paths: dict[str, Path] = {}
        for label, certificate in certificates.items():
            path = certificate_directory / f"{label.replace('_', '-')}-certificate.json"
            _write_json(path, certificate)
            certificate_paths[label] = path
        receipt_records = []
        for label, filename in RECEIPT_FILENAMES.items():
            receipt = evidence.receipts[label]
            destination = evidence_directory / filename
            destination.write_bytes(receipt.raw)
            receipt_records.append(
                {
                    "label": label,
                    "path": destination.relative_to(staging).as_posix(),
                    "schema": receipt.payload.get("schema"),
                    "sha256": receipt.sha256,
                    "bytes": len(receipt.raw),
                }
            )
        bundle_directory = staging / "browser-engine"
        bundle_builder.build_bundle(
            single_wasm=evidence.build.wasm,
            single_module_js=evidence.build.module_js,
            single_prefix_certificate_path=certificate_paths["prefix"],
            single_root_session_certificate_path=certificate_paths["root_session"],
            single_mate_certificate_path=certificate_paths["mate"],
            source_package=source_package.resolve(),
            output=bundle_directory,
        )
        bundle_builder.validate_existing_bundle(bundle_directory, source_package.resolve())
        bundle_records, bundle_set_sha256 = _directory_records(bundle_directory)
        certificate_records, certificate_set_sha256 = _directory_records(certificate_directory)
        runtime_records, runtime_set_sha256 = _browser_runtime_records(source_package)
        candidate_policy = {
            "maximum_seconds": maximum_seconds,
            "default_seconds": default_seconds,
        }
        candidate_seed = {
            "artifact": evidence.build.identity,
            "bundle_set_sha256": bundle_set_sha256,
            "certificate_set_sha256": certificate_set_sha256,
            "browser_runtime_set_sha256": runtime_set_sha256,
            "receipts": [
                {key: item[key] for key in ("label", "sha256")}
                for item in receipt_records
            ],
            "policy": candidate_policy,
        }
        candidate_id = f"spc-browser-wasm-candidate-{_canonical_sha256(candidate_seed)[:16]}"
        candidate_receipt = {
            "schema": CANDIDATE_SCHEMA,
            "status": "staged-for-local-opera-attestation",
            "product_publishable": False,
            "safety_certified": False,
            "candidate_id": candidate_id,
            "source_revision": evidence.build.identity["source_revision"],
            "artifact": dict(evidence.build.identity),
            "certificates": {
                label: {
                    "certificate_id": certificate["certificate_id"],
                    "path": certificate_paths[label].relative_to(staging).as_posix(),
                    "sha256": _sha256_file(certificate_paths[label]),
                }
                for label, certificate in certificates.items()
            },
            "evidence_receipts": receipt_records,
            "browser_bundle": {
                "path": "browser-engine",
                "files": bundle_records,
                "artifact_set_sha256": bundle_set_sha256,
            },
            "browser_runtime": {
                "schema": "spc-browser-runtime-asset-set-v1",
                "source_revision": evidence.build.identity["source_revision"],
                "files": runtime_records,
                "artifact_set_sha256": runtime_set_sha256,
            },
            "policy": candidate_policy,
            "certificate_set_sha256": certificate_set_sha256,
            "next_required_gate": OPERA_CHECKED_HORIZON_SCHEMA,
        }
        _write_json(staging / "candidate-receipt.json", candidate_receipt)
        staging.replace(output)
    return candidate_receipt


def promote_release(
    evidence: ValidatedEvidence,
    certificates: Mapping[str, Mapping[str, Any]],
    *,
    source_package: Path,
    repository: Path,
    opera_checked_horizon_receipt: Path,
    output: Path,
    authorized_by: str,
    maximum_seconds: float,
    default_seconds: float,
) -> Mapping[str, Any]:
    authorized_by = _text(authorized_by, "promotion authorizer")
    maximum_value = float(maximum_seconds)
    default_value = float(default_seconds)
    maximum_seconds = int(maximum_value) if maximum_value.is_integer() else maximum_value
    default_seconds = int(default_value) if default_value.is_integer() else default_value
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"release output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}.", dir=output.parent) as name:
        staging = Path(name) / "release"
        staging.mkdir()
        certificate_directory = staging / "certificates"
        evidence_directory = staging / "evidence"
        certificate_directory.mkdir()
        evidence_directory.mkdir()
        certificate_paths: dict[str, Path] = {}
        for label, certificate in certificates.items():
            path = certificate_directory / f"{label.replace('_', '-')}-certificate.json"
            _write_json(path, certificate)
            certificate_paths[label] = path
        receipt_records = []
        for label, filename in RECEIPT_FILENAMES.items():
            receipt = evidence.receipts[label]
            destination = evidence_directory / filename
            destination.write_bytes(receipt.raw)
            receipt_records.append(
                {
                    "label": label,
                    "path": destination.relative_to(staging).as_posix(),
                    "schema": receipt.payload.get("schema"),
                    "sha256": receipt.sha256,
                    "bytes": len(receipt.raw),
                }
            )
        bundle_directory = staging / "browser-engine"
        bundle_builder.build_bundle(
            single_wasm=evidence.build.wasm,
            single_module_js=evidence.build.module_js,
            single_prefix_certificate_path=certificate_paths["prefix"],
            single_root_session_certificate_path=certificate_paths["root_session"],
            single_mate_certificate_path=certificate_paths["mate"],
            source_package=source_package.resolve(),
            output=bundle_directory,
        )
        bundle_builder.validate_existing_bundle(bundle_directory, source_package.resolve())
        checked_horizon = validate_opera_checked_horizon_receipt(
            receipt_path=opera_checked_horizon_receipt,
            evidence=evidence,
            certificates=certificates,
            repository=repository,
            source_package=source_package,
            candidate_bundle=bundle_directory,
        )
        checked_destination = evidence_directory / OPERA_CHECKED_HORIZON_FILENAME
        checked_destination.write_bytes(checked_horizon.receipt.raw)
        receipt_records.append(
            {
                "label": "opera_checked_horizon",
                "path": checked_destination.relative_to(staging).as_posix(),
                "schema": checked_horizon.receipt.payload.get("schema"),
                "sha256": checked_horizon.receipt.sha256,
                "bytes": len(checked_horizon.receipt.raw),
            }
        )
        bundle_records, bundle_set_sha256 = _directory_records(bundle_directory)
        certificate_records, certificate_set_sha256 = _directory_records(certificate_directory)
        release_seed = {
            "artifact": evidence.build.identity,
            "bundle_set_sha256": bundle_set_sha256,
            "certificate_set_sha256": certificate_set_sha256,
            "receipts": [
                {key: item[key] for key in ("label", "sha256")}
                for item in receipt_records
            ],
            "policy": {
                "maximum_seconds": maximum_seconds,
                "default_seconds": default_seconds,
            },
        }
        release_id = f"spc-browser-wasm-release-{_canonical_sha256(release_seed)[:16]}"
        release_receipt = {
            "schema": RELEASE_SCHEMA,
            "status": "promoted",
            "product_publishable": True,
            "release_id": release_id,
            "authorization": {
                "authorized_by": authorized_by,
                "transition": "verified-combined-wasm-to-pages-ready",
                "mechanism": "explicit-command-line",
            },
            "source_revision": evidence.build.identity["source_revision"],
            "artifact": {
                **evidence.build.identity,
                **evidence.build.runtime_identity,
                "runtime_variant": "single",
                "thread_count": 1,
            },
            "toolchain": dict(evidence.build.toolchain),
            "build_command_sha256": _canonical_sha256(
                evidence.build.receipt.payload["command"]
            ),
            "dependency_closure": evidence.build.dependency_closure,
            "root_tactical_policy": {
                "capability": True,
                "policy": "canonical-boundary-policy-v1",
                "legacy_wire_root_tactical_protection": False,
            },
            "certificates": {
                label: {
                    "certificate_id": certificate["certificate_id"],
                    "path": certificate_paths[label].relative_to(staging).as_posix(),
                    "sha256": _sha256_file(certificate_paths[label]),
                }
                for label, certificate in certificates.items()
            },
            "evidence_receipts": receipt_records,
            "browser_bundle": {
                "path": "browser-engine",
                "files": bundle_records,
                "artifact_set_sha256": bundle_set_sha256,
            },
            "certificate_set_sha256": certificate_set_sha256,
            "promotion_policy": {
                "maximum_seconds": maximum_seconds,
                "default_seconds": default_seconds,
                "default_generation_positions": evidence.root_config["max_work"],
                "safety_reserve_positions": evidence.safety_reserve_positions,
            },
            "measured": {
                "root_d5_oracle_signature_sha256": evidence.oracle_signature_sha256,
                "opera_total_d1_through_d5_seconds": evidence.opera_elapsed_seconds,
                "completed_depth": 5,
                "width": 32,
                "workers": 8,
                "initial_full_wave": 8,
                "result": evidence.opera_result,
                "memory": evidence.opera_memory,
                "opera_checked_horizon": {
                    "elapsed_seconds": checked_horizon.elapsed_seconds,
                    "work": checked_horizon.work,
                    "selected_root_series": checked_horizon.selected_root_series,
                    "pv_horizon_line_rejections": checked_horizon.line_rejections,
                    "pv_horizon_native_repairs": checked_horizon.native_repairs,
                    "pv_horizon_candidate_vetoes": checked_horizon.candidate_vetoes,
                    "principal_variation_sha256": (
                        checked_horizon.principal_variation_sha256
                    ),
                    "selected_fixture_id": checked_horizon.selected_fixture_id,
                    "known_adverse_excluded": checked_horizon.known_adverse_excluded,
                    "selected_boundary_ladder_certified": (
                        checked_horizon.selected_boundary_ladder_certified
                    ),
                    "found_stop_observed": checked_horizon.found_stop_observed,
                    "unknown_fail_closed_observed": (
                        checked_horizon.unknown_fail_closed_observed
                    ),
                    "selected_horizon_exhaustively_certified": (
                        checked_horizon.selected_horizon_exhaustively_certified
                    ),
                    "selected_root_child_exhaustively_certified": (
                        checked_horizon.selected_root_child_exhaustively_certified
                    ),
                    "raw_trace_attestation": checked_horizon.raw_trace_attestation,
                    "selected_d5_horizon_certification_witness": (
                        checked_horizon.selected_d5_horizon_certification_witness
                    ),
                    "local_checkout_asset_set_sha256": (
                        checked_horizon.local_checkout_asset_set_sha256
                    ),
                },
            },
            "gates": {
                "exact_source_and_artifact_identity": True,
                "clean_tracked_dependency_closure": True,
                "root_python_parity": True,
                "persistent_matches_fresh_d5": True,
                "multiple_opera_schedule_shapes": True,
                "prefix_python_and_browser_parity": True,
                "mate_python_and_proof_parity": True,
                "combined_root_prefix_mate_abi": True,
                "canonical_root_tactical_boundary_policy": True,
                "opera_ordinary_worker_proof": True,
                "memory_envelope_observed": True,
                "w32_d1_through_d5_under_60_seconds": True,
                "existing_bundle_revalidated": True,
                "immutable_copy_by_digest": True,
                "opera_checked_horizon_raw_trace_attested": (
                    checked_horizon.raw_safety_trace_count > 0
                    and HEX_64.fullmatch(checked_horizon.raw_safety_trace_sha256)
                    is not None
                    and checked_horizon.raw_research_trace_count > 0
                    and HEX_64.fullmatch(checked_horizon.raw_research_trace_sha256)
                    is not None
                ),
                "opera_checked_horizon_local_assets_bound": (
                    HEX_64.fullmatch(
                        checked_horizon.local_checkout_asset_set_sha256
                    )
                    is not None
                ),
                "opera_selected_b3_known_adverse_horizon_excluded": (
                    checked_horizon.known_adverse_excluded
                ),
                "opera_selected_b3_boundary_ladder_certified": (
                    checked_horizon.selected_boundary_ladder_certified
                ),
                "opera_found_stops_boundary_ladder": (
                    checked_horizon.found_stop_observed
                ),
                "opera_unknown_fail_closed_observed": (
                    checked_horizon.unknown_fail_closed_observed
                ),
                "opera_selected_b3_horizon_exhaustively_certified": (
                    checked_horizon.selected_horizon_exhaustively_certified
                ),
                "opera_selected_b3_root_child_exhaustively_certified": (
                    checked_horizon.selected_root_child_exhaustively_certified
                ),
                "opera_checked_horizon_d5_under_60_seconds": (
                    checked_horizon.elapsed_seconds < 60
                ),
                "opera_checked_horizon_accounting_balanced": (
                    checked_horizon.native_repairs
                    + checked_horizon.candidate_vetoes
                    == checked_horizon.line_rejections
                ),
            },
        }
        _write_json(staging / "release-receipt.json", release_receipt)
        staging.replace(output)
    return release_receipt


def _receipt_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--build-receipt", type=Path, required=True)
    parser.add_argument("--root-smoke-receipt", type=Path, required=True)
    parser.add_argument("--root-parity-receipt", type=Path, required=True)
    parser.add_argument("--prefix-parity-receipt", type=Path, required=True)
    parser.add_argument("--browser-prefix-receipt", type=Path, required=True)
    parser.add_argument("--mate-parity-receipt", type=Path, required=True)
    parser.add_argument("--opera-receipt", type=Path, required=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed promotion of one identity-bound combined root/prefix/mate "
            "WASM artifact after exact parity, memory, Opera Worker, and W32 D1-D5 gates."
        )
    )
    _receipt_arguments(parser)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument(
        "--source-package",
        type=Path,
        default=ROOT / "src" / "scottish_progressive",
    )
    parser.add_argument("--maximum-seconds", type=float, default=60.0)
    parser.add_argument("--default-seconds", type=float, default=60.0)
    parser.add_argument("--authorized-by")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--stage-candidate", action="store_true")
    parser.add_argument("--opera-checked-horizon-receipt", type=Path)
    args = parser.parse_args(argv)
    if args.stage_candidate:
        if args.check_only or args.authorized_by is not None:
            parser.error("--stage-candidate cannot certify, authorize, or publish")
        if args.opera_checked_horizon_receipt is not None:
            parser.error("--stage-candidate must precede the Opera checked-horizon receipt")
        if args.output is None:
            parser.error("--stage-candidate requires --output")
    elif args.opera_checked_horizon_receipt is None:
        parser.error("final validation requires --opera-checked-horizon-receipt")
    elif args.check_only:
        if args.output is not None or args.authorized_by is not None:
            parser.error("--check-only cannot be combined with --output or --authorized-by")
    elif args.output is None or args.authorized_by is None:
        parser.error("promotion requires --output and --authorized-by")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    receipt_paths = {
        "build": args.build_receipt.resolve(),
        "root_smoke": args.root_smoke_receipt.resolve(),
        "root_parity": args.root_parity_receipt.resolve(),
        "prefix_parity": args.prefix_parity_receipt.resolve(),
        "browser_prefix": args.browser_prefix_receipt.resolve(),
        "mate_parity": args.mate_parity_receipt.resolve(),
        "opera": args.opera_receipt.resolve(),
    }
    try:
        evidence = validate_evidence(
            repository=args.repository.resolve(),
            source_package=args.source_package.resolve(),
            receipt_paths=receipt_paths,
        )
        certificates = build_certificates(
            evidence,
            maximum_seconds=args.maximum_seconds,
            default_seconds=args.default_seconds,
        )
        if args.stage_candidate:
            assert args.output is not None
            result = stage_release_candidate(
                evidence,
                certificates,
                source_package=args.source_package.resolve(),
                output=args.output.resolve(),
                maximum_seconds=args.maximum_seconds,
                default_seconds=args.default_seconds,
            )
        elif args.check_only:
            assert args.opera_checked_horizon_receipt is not None
            with tempfile.TemporaryDirectory(prefix="spc-release-check-") as name:
                candidate = Path(name) / "candidate"
                stage_release_candidate(
                    evidence,
                    certificates,
                    source_package=args.source_package.resolve(),
                    output=candidate,
                    maximum_seconds=args.maximum_seconds,
                    default_seconds=args.default_seconds,
                )
                checked_horizon = validate_opera_checked_horizon_receipt(
                    receipt_path=args.opera_checked_horizon_receipt.resolve(),
                    evidence=evidence,
                    certificates=certificates,
                    repository=args.repository.resolve(),
                    source_package=args.source_package.resolve(),
                    candidate_bundle=candidate / "browser-engine",
                )
            result: Mapping[str, Any] = {
                "schema": RELEASE_SCHEMA,
                "status": "validated-not-promoted",
                "product_publishable": False,
                "artifact": evidence.build.identity,
                "root_tactical_policy": {
                    "capability": True,
                    "policy": "canonical-boundary-policy-v1",
                    "legacy_wire_root_tactical_protection": False,
                },
                "receipt_sha256": {
                    label: receipt.sha256
                    for label, receipt in sorted(evidence.receipts.items())
                },
                "opera_checked_horizon_receipt_sha256": checked_horizon.receipt.sha256,
                "opera_checked_horizon_asset_set_sha256": (
                    checked_horizon.local_checkout_asset_set_sha256
                ),
                "opera_total_d1_through_d5_seconds": evidence.opera_elapsed_seconds,
                "certificate_ids": {
                    label: certificate["certificate_id"]
                    for label, certificate in certificates.items()
                },
            }
        else:
            assert (
                args.output is not None
                and args.authorized_by is not None
                and args.opera_checked_horizon_receipt is not None
            )
            result = promote_release(
                evidence,
                certificates,
                source_package=args.source_package.resolve(),
                repository=args.repository.resolve(),
                opera_checked_horizon_receipt=(
                    args.opera_checked_horizon_receipt.resolve()
                ),
                output=args.output.resolve(),
                authorized_by=args.authorized_by,
                maximum_seconds=args.maximum_seconds,
                default_seconds=args.default_seconds,
            )
    except (FileNotFoundError, ReleaseGateError, ValueError) as error:
        print(f"release gate failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
