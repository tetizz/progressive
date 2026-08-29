from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Mapping


MANIFEST_SCHEMA = "spc-browser-wasm-manifest-v1"
CERTIFICATE_SCHEMA = "spc-browser-wasm-certificate-v1"
PREFIX_CONTRACT_SCHEMA = "spc-boundary-prefix-contract-v1"
PREFIX_RESULT_SCHEMA = "spc-boundary-prefix-v1"
ROOT_SESSION_CERTIFICATE_SCHEMA = "spc-root-session-certificate-v1"
ROOT_SESSION_CONTRACT_SCHEMA = "spc-root-session-contract-v1"
MATE_CERTIFICATE_SCHEMA = "spc-series-mate-certificate-v1"
CHECKED_HORIZON_EVIDENCE_SCHEMA = "spc-checked-horizon-wasm-evidence-v1"
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
MIN_PREFIX_DIFFERENTIAL_CASES = 14
MIN_MATE_DIFFERENTIAL_CASES = 5
PREFIX_HARD_LIMITS = {
    "maximum_fen_utf8_bytes": 512,
    "maximum_series_number": 256,
    "maximum_quiet_series": 1_000_000,
    "maximum_ep_targets": 8,
    "maximum_ep_utf8_bytes": 23,
    "maximum_prefix_moves": 256,
    "maximum_prefix_utf8_bytes": 1_535,
    "maximum_uci_move_bytes": 5,
    "maximum_promoted_hex_bytes": 18,
}
HEX_16 = re.compile(r"[0-9a-f]{16}")
HEX_64 = re.compile(r"[0-9a-f]{64}")
LOCATE_FILE_CALL = re.compile(r"(?<![\w$.])locateFile\s*\(")
LOCATE_FILE_LITERAL_CALL = re.compile(
    r'''(?<![\w$.])locateFile\s*\(\s*(?P<quote>["'])(?P<asset>[^"'\\]*)(?P=quote)\s*\)'''
)
MAX_INITIAL_MEMORY_BYTES = 128 * 1024 * 1024
MAXIMUM_MEMORY_BYTES = 256 * 1024 * 1024
MAX_ESTIMATED_PEAK_MEMORY_BYTES = 192 * 1024 * 1024
MAX_VALUE_MODEL_BYTES = 64 * 1024
COMBINED_EXPORTS = [
    "_spc_start_kernel_search_json",
    "_spc_boundary_kernel_search_json",
    "_spc_boundary_prefix_json",
    "_spc_boundary_prefix_contract_json",
    "_spc_start_kernel_abi_version",
    "_spc_root_session_contract_json",
    "_spc_root_session_create_json",
    "_spc_root_session_enumerate_json",
    "_spc_root_session_import_json",
    "_spc_root_session_search_json",
    "_spc_root_session_destroy",
    "_spc_root_session_abi_version",
    "_spc_series_mate_search_json",
    "_spc_series_mate_abi_version",
    "_spc_single_reply_mate_ladder_search_json",
    "_spc_single_reply_mate_ladder_abi_version",
    "_malloc",
    "_free",
]
NATIVE_SOURCE_FILES = (
    "_native_eval.cpp",
    "native_eval.hpp",
    "native_subtree.cpp",
    "native_subtree.hpp",
    "native_selfplay.cpp",
    "native_selfplay.hpp",
)
ROOT_SESSION_CONFIG_KEYS = {
    "max_depth",
    "width",
    "max_work",
    "mate_score",
    "series_cache_capacity",
    "external_cache_weight",
    "worker_threads",
    "root_tactical_protection",
    "root_contract_tt_capacity",
    "root_contract_eval_capacity",
    "weights",
}
ROOT_SESSION_WEIGHT_KEYS = {
    "material",
    "king_space",
    "series_reach",
    "promotion_corridors",
    "immediate_vulnerability",
    "useful_mobility",
    "boundary_check",
}
DEEP_TEACHER_MODEL_SCHEMA = "spc-deep-teacher-linear-value-v1"
DEEP_TEACHER_OVERLAY_SCHEMA = "spc-deep-teacher-match-overlay-v1"
DEEP_TEACHER_FEATURE_SCHEMA = "spc-teacher-value-features-v3"
DEEP_TEACHER_FIXED_POINT_SCALE = 1_000_000_000
DEEP_TEACHER_TERMINAL_POLICY = (
    "replayed terminal checkmate and draw outcomes are authoritative"
)
DEEP_TEACHER_SCORE_POLICY = (
    "symmetric-half-away-from-zero-divide-by-1000000000-then-clamp-below-mate-v1"
)
DEEP_TEACHER_WORK_POLICY = (
    "charge-reach-plus-direct-and-two-move-legal-variants-v1"
)
VALUE_MODEL_ACTIVATION_SCHEMA = "spc-browser-value-model-activation-v1"
VALUE_MODEL_ASSET_SCHEMA = "spc-browser-value-model-asset-v1"
DEEP_TEACHER_CONFIG_KEYS = {
    "schema",
    "base_profile_id",
    "variant_id",
    "model_id",
    "model_sha256",
    "native_source_identity",
    "feature_count",
    "fixed_point_scale",
    "coefficients",
    "score_policy",
    "work_policy",
}
DEEP_TEACHER_MODEL_KEYS = {
    "schema",
    "feature_schema",
    "feature_group",
    "feature_names",
    "fixed_point_scale",
    "coefficients",
    "ridge",
    "adverse_pair_weight",
    "terminal_override",
    "teacher_corpus_id",
    "teacher_corpus_sha256",
    "teacher_corpus_semantic_sha256",
    "teacher_corpus_raw_artifact_sha256",
    "model_id",
}
DEEP_TEACHER_FEATURE_COUNTS = {
    "base7": 7,
    "phase14": 14,
    "cached19": 19,
    "positional38": 38,
    "direct44": 44,
    "all47": 47,
}
_BASE_TEACHER_FEATURE_NAMES = (
    "material",
    "king_space",
    "series_reach",
    "promotion_corridors",
    "immediate_vulnerability",
    "useful_mobility",
    "boundary_check",
)
DEEP_TEACHER_FEATURE_NAMES = (
    *_BASE_TEACHER_FEATURE_NAMES,
    *(f"{name}_x_centered_phase" for name in _BASE_TEACHER_FEATURE_NAMES),
    "king_ring_attack_balance",
    "promotable_next_series_balance",
    "king_edge_safety_balance",
    "check_route_balance",
    "reach_complete",
    "developed_minor_balance",
    "center_occupancy_balance",
    "center_control_balance",
    "extended_center_control_balance",
    "pawn_space_balance",
    "passed_pawn_balance",
    "passed_pawn_advance_balance",
    "connected_passed_pawn_balance",
    "isolated_pawn_liability_balance",
    "doubled_pawn_liability_balance",
    "pawn_island_liability_balance",
    "bishop_pair_balance",
    "rook_open_file_balance",
    "rook_seventh_rank_balance",
    "king_pawn_shelter_balance",
    "attacked_material_balance",
    "hanging_material_balance",
    "pinned_material_balance",
    "queen_exposure_balance",
    "direct_capture_count_for_mover",
    "direct_capture_value_for_mover",
    "direct_max_capture_value_for_mover",
    "direct_check_count_for_mover",
    "direct_mate_count_for_mover",
    "direct_promotion_count_for_mover",
    "two_move_capture_value_for_mover",
    "two_move_check_routes_for_mover",
    "two_move_mate_routes_for_mover",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def engine_source_fingerprint(package: Path) -> str:
    digest = hashlib.sha256()
    paths = (
        path
        for pattern in ("*.py", "*.cpp", "*.hpp", "*.h")
        for path in package.rglob(pattern)
    )
    for path in sorted(paths, key=lambda item: item.relative_to(package).as_posix()):
        digest.update(path.relative_to(package).as_posix().encode("utf-8"))
        digest.update(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    return digest.hexdigest()[:16]


def native_source_identity(package: Path) -> str:
    """Digest the exact native evaluator sources bound into a value model."""

    digest = hashlib.sha256()
    try:
        for filename in NATIVE_SOURCE_FILES:
            digest.update(filename.encode("utf-8"))
            digest.update(
                (package / filename)
                .read_bytes()
                .replace(b"\r\n", b"\n")
                .replace(b"\r", b"\n")
            )
    except OSError as error:
        raise ValueError(
            f"could not bind deep-teacher native source identity: {error}"
        ) from error
    return digest.hexdigest()


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
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
    case = dict(_require_mapping(value, label))
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
        raise ValueError(f"{label} fields do not match the exact evidence schema")
    proof_count = len(proof_order)
    actual_order = case.get("request_proof_order")
    actual_lengths = case.get("request_proof_path_lengths")
    if (
        not isinstance(actual_order, list)
        or any(not isinstance(item, str) for item in actual_order)
        or not isinstance(actual_lengths, list)
        or any(
            isinstance(item, bool) or not isinstance(item, int)
            for item in actual_lengths
        )
    ):
        raise ValueError(f"{label} proof order/path anchors are not exact")
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
        "score",
        "horizon_proofs_validated",
        "horizon_proof_hits",
        "horizon_proof_hit_mask",
        "exact_tt_hits",
        "prior_same_root_score",
    ):
        if isinstance(case.get(key), bool) or not isinstance(case.get(key), int):
            raise ValueError(f"{label} field {key!r} must be an exact integer")
    for key, expected_value in expected.items():
        if case.get(key) != expected_value:
            raise ValueError(
                f"{label} field {key!r} is not exact checked-horizon evidence"
            )
    for key in (
        "horizon_proof_set_identity_sha256",
        "candidate_identity_sha256",
        "prior_same_root_candidate_identity_sha256",
    ):
        digest = case.get(key)
        if not isinstance(digest, str) or HEX_64.fullmatch(digest) is None:
            raise ValueError(f"{label} {key} is not a SHA-256 commitment")
    if root_pv_sha256 is not None:
        for key in ("root_pv_sha256", "prior_same_root_root_pv_sha256"):
            digest = case.get(key)
            if not isinstance(digest, str) or HEX_64.fullmatch(digest) is None:
                raise ValueError(f"{label} {key} is not a SHA-256 commitment")
    newest_bit = 1 << (proof_count - 1)
    newest_hit = hit_mask & newest_bit != 0
    if hits == 0 and hit_mask == 0 and exact_tt_hits > 0:
        expected_disposition = "warm-exact-recertified"
    else:
        expected_disposition = (
            "same-root-repaired" if newest_hit else "newest-proof-not-hit"
        )
    if disposition != expected_disposition:
        raise ValueError(f"{label} disposition does not follow request-order hits")
    if disposition == "same-root-repaired" and score == prior_score:
        raise ValueError(f"{label} did not change the same-root result")
    return case


def _validate_checked_horizon_evidence(
    value: object,
    *,
    label: str,
) -> dict[str, Any]:
    evidence = dict(_require_mapping(value, label))
    expected_keys = {
        "schema",
        "white_deep_two_proof",
        "white_deep_warm_exact",
        "white_deep_reversed_order",
        "black_parity",
    }
    if (
        set(evidence) != expected_keys
        or evidence.get("schema") != CHECKED_HORIZON_EVIDENCE_SCHEMA
    ):
        raise ValueError(f"{label} does not match the exact evidence schema")
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
        raise ValueError(
            f"{label} reversed order changed the proof set or retained root identity"
        )
    return evidence


def load_certificate(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read safety certificate: {error}") from error
    return _require_mapping(payload, "safety certificate")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON object repeats key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON contains nonfinite value {value}")


def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _strict_json_bytes(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} is not strict UTF-8 JSON: {error}") from error
    return _require_mapping(parsed, label)


def _validate_original_deep_teacher_model(
    path: Path,
    configured: Mapping[str, Any],
) -> dict[str, object]:
    """Validate the exact training artifact copied into a browser bundle."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError(f"could not read deep-teacher model asset: {error}") from error
    if len(raw) > MAX_VALUE_MODEL_BYTES:
        raise ValueError("deep-teacher model asset exceeds the frozen browser size limit")
    payload = _strict_json_bytes(raw, "deep-teacher model asset")
    if set(payload) != DEEP_TEACHER_MODEL_KEYS:
        raise ValueError("deep-teacher model asset keys differ from the frozen schema")
    group = payload.get("feature_group")
    feature_count = DEEP_TEACHER_FEATURE_COUNTS.get(group)
    names = payload.get("feature_names")
    coefficients = payload.get("coefficients")
    if (
        payload.get("schema") != DEEP_TEACHER_MODEL_SCHEMA
        or payload.get("feature_schema") != DEEP_TEACHER_FEATURE_SCHEMA
        or feature_count is None
        or not isinstance(names, list)
        or names != list(DEEP_TEACHER_FEATURE_NAMES[:feature_count])
        or not isinstance(coefficients, list)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in coefficients)
        or len(coefficients) != feature_count
        or any(abs(item) > DEEP_TEACHER_FIXED_POINT_SCALE for item in coefficients)
        or max((abs(item) for item in coefficients), default=0)
        != DEEP_TEACHER_FIXED_POINT_SCALE
        or payload.get("fixed_point_scale") != DEEP_TEACHER_FIXED_POINT_SCALE
        or payload.get("terminal_override") != DEEP_TEACHER_TERMINAL_POLICY
    ):
        raise ValueError("deep-teacher model asset feature contract is invalid")
    ridge = payload.get("ridge")
    adverse = payload.get("adverse_pair_weight")
    if (
        isinstance(ridge, bool)
        or not isinstance(ridge, float)
        or not 0.0 < ridge < float("inf")
        or isinstance(adverse, bool)
        or not isinstance(adverse, float)
        or not 1.0 <= adverse <= 1_000.0
    ):
        raise ValueError("deep-teacher model asset fit parameters are invalid")
    for name in (
        "teacher_corpus_sha256",
        "teacher_corpus_semantic_sha256",
        "teacher_corpus_raw_artifact_sha256",
    ):
        if not HEX_64.fullmatch(str(payload.get(name, ""))):
            raise ValueError(f"deep-teacher model asset has invalid {name}")
    if payload.get("teacher_corpus_sha256") != payload.get(
        "teacher_corpus_semantic_sha256"
    ):
        raise ValueError("deep-teacher model asset semantic corpus identity differs")
    if not isinstance(payload.get("teacher_corpus_id"), str) or not str(
        payload["teacher_corpus_id"]
    ).strip():
        raise ValueError("deep-teacher model asset corpus identity is invalid")
    model_core = {
        key: payload[key]
        for key in sorted(
            DEEP_TEACHER_MODEL_KEYS
            - {"model_id", "teacher_corpus_raw_artifact_sha256"}
        )
    }
    expected_model_id = "spc-dtv-" + _canonical_json_sha256(model_core)[:20]
    if payload.get("model_id") != expected_model_id:
        raise ValueError("deep-teacher model asset model_id is not self-authenticating")
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        configured.get("model_id") != expected_model_id
        or configured.get("model_sha256") != actual_sha256
        or configured.get("feature_count") != feature_count
        or configured.get("fixed_point_scale") != DEEP_TEACHER_FIXED_POINT_SCALE
        or configured.get("coefficients") != coefficients
    ):
        raise ValueError("deep-teacher model asset differs from its certified config")
    return {
        "schema": VALUE_MODEL_ASSET_SCHEMA,
        "file": _manifest_asset_name(path.name, ".json"),
        "sha256": actual_sha256,
        "model_schema": DEEP_TEACHER_MODEL_SCHEMA,
        "model_id": expected_model_id,
        "variant_id": configured["variant_id"],
        "base_profile_id": configured["base_profile_id"],
        "native_source_identity": configured["native_source_identity"],
    }


def validate_memory_limits(value: object) -> dict[str, int | bool]:
    memory = _require_mapping(value, "certificate memory")
    expected_keys = {
        "initial_bytes",
        "maximum_bytes",
        "estimated_peak_bytes",
        "growth_enabled",
    }
    if set(memory) != expected_keys:
        raise ValueError("certificate memory must exactly name the memory envelope")
    normalized: dict[str, int | bool] = {}
    for key, cap in (
        ("initial_bytes", MAX_INITIAL_MEMORY_BYTES),
        ("maximum_bytes", MAXIMUM_MEMORY_BYTES),
        ("estimated_peak_bytes", MAX_ESTIMATED_PEAK_MEMORY_BYTES),
    ):
        number = memory.get(key)
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
            or number % 65_536
            or number > cap
        ):
            raise ValueError(
                f"certificate memory {key} must be a positive 64KiB-aligned "
                f"integer no larger than {cap}"
            )
        normalized[key] = number
    growth_enabled = memory.get("growth_enabled")
    if not isinstance(growth_enabled, bool):
        raise ValueError("certificate memory growth_enabled must be a boolean")
    normalized["growth_enabled"] = growth_enabled
    if normalized["initial_bytes"] > normalized["estimated_peak_bytes"]:
        raise ValueError("certificate initial memory exceeds estimated peak memory")
    if normalized["estimated_peak_bytes"] > normalized["maximum_bytes"]:
        raise ValueError("certificate estimated peak memory exceeds maximum memory")
    if not growth_enabled and normalized["initial_bytes"] != normalized["maximum_bytes"]:
        raise ValueError("fixed-memory certificates require equal initial and maximum memory")
    return normalized


def validate_prefix_contract(value: object) -> dict[str, object]:
    contract = _require_mapping(value, "prefix contract")
    expected = {
        "schema": PREFIX_CONTRACT_SCHEMA,
        "result_schema": PREFIX_RESULT_SCHEMA,
        "abi_version": 1,
        "chess960": False,
        "promoted_hex_required_for_product": True,
    }
    for key, expected_value in expected.items():
        if contract.get(key) != expected_value:
            raise ValueError(
                f"prefix contract {key!r} must be {expected_value!r}"
            )
    raw_limits = _require_mapping(contract.get("limits"), "prefix contract limits")
    if set(raw_limits) != set(PREFIX_HARD_LIMITS):
        raise ValueError("prefix contract limits must exactly name the hard ABI envelope")
    limits: dict[str, int] = {}
    for key, hard_maximum in PREFIX_HARD_LIMITS.items():
        value = raw_limits.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= hard_maximum
        ):
            raise ValueError(
                f"prefix contract limit {key} must be from 1 through {hard_maximum}"
            )
        limits[key] = value
    if limits["maximum_prefix_moves"] > limits["maximum_series_number"]:
        raise ValueError("prefix move limit exceeds the certified series-number limit")
    return {**expected, "limits": limits}


def validate_prefix_certificate(
    certificate: Mapping[str, Any],
    *,
    source_fingerprint: str,
    wasm_sha256: str,
    module_js_sha256: str,
    runtime_variant: str,
    thread_count: int,
    support_files: list[dict[str, str]],
) -> tuple[dict[str, object], dict[str, int | bool], dict[str, str]]:
    expected = {
        "status": "certified",
        "contract_version": 1,
        "source_fingerprint": source_fingerprint,
        "wasm_sha256": wasm_sha256,
        "module_js_sha256": module_js_sha256,
        "runtime_variant": runtime_variant,
        "thread_count": thread_count,
        "support_files": support_files,
    }
    for key, expected_value in expected.items():
        if certificate.get(key) != expected_value:
            raise ValueError(
                f"prefix certificate {key!r} does not match the artifact: "
                f"expected {expected_value!r}, found {certificate.get(key)!r}"
            )
    certificate_id = certificate.get("certificate_id")
    if not isinstance(certificate_id, str) or not certificate_id.strip():
        raise ValueError("prefix certificate requires a non-empty certificate_id")
    evidence = _require_mapping(certificate.get("evidence"), "prefix evidence")
    required_evidence = {
        "failures": 0,
        "compiled_prefix_replay": True,
        "multi_ep_san": True,
        "illegal_prefix_fail_closed": True,
    }
    for key, expected_value in required_evidence.items():
        if evidence.get(key) != expected_value:
            raise ValueError(
                f"prefix certificate evidence {key!r} must be {expected_value!r}"
            )
    differential_cases = evidence.get("differential_cases")
    if (
        isinstance(differential_cases, bool)
        or not isinstance(differential_cases, int)
        or differential_cases < MIN_PREFIX_DIFFERENTIAL_CASES
    ):
        raise ValueError(
            "prefix certificate requires at least "
            f"{MIN_PREFIX_DIFFERENTIAL_CASES} differential cases"
        )
    engine = _require_mapping(certificate.get("engine"), "prefix engine identity")
    engine_identity: dict[str, str] = {}
    for key in ("engine_version", "ruleset_version"):
        value = engine.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"prefix certificate engine requires non-empty {key}")
        engine_identity[key] = value
    contract = validate_prefix_contract(certificate.get("prefix_contract"))
    memory = validate_memory_limits(certificate.get("memory"))
    return contract, memory, engine_identity


def _validate_combined_capability_identity(
    certificate: Mapping[str, Any],
    *,
    schema: str,
    abi_version: int,
    source_fingerprint: str,
    wasm_sha256: str,
    module_js_sha256: str,
    runtime_variant: str,
    thread_count: int,
    support_files: list[dict[str, str]],
) -> tuple[dict[str, int | bool], dict[str, str], str, str]:
    expected = {
        "schema": schema,
        "status": "certified",
        "contract_version": 1,
        "abi_version": abi_version,
        "product_publishable": False,
        "source_fingerprint": source_fingerprint,
        "wasm_sha256": wasm_sha256,
        "module_js_sha256": module_js_sha256,
        "runtime_variant": runtime_variant,
        "thread_count": thread_count,
        "support_files": support_files,
        "exports": COMBINED_EXPORTS,
    }
    for key, expected_value in expected.items():
        if certificate.get(key) != expected_value:
            raise ValueError(
                f"{schema} {key!r} does not match the combined artifact: "
                f"expected {expected_value!r}, found {certificate.get(key)!r}"
            )
    certificate_id = certificate.get("certificate_id")
    if not isinstance(certificate_id, str) or not certificate_id.strip():
        raise ValueError(f"{schema} requires a non-empty certificate_id")
    kernel_sha256 = certificate.get("kernel_sha256")
    if not isinstance(kernel_sha256, str) or not HEX_64.fullmatch(kernel_sha256):
        raise ValueError(f"{schema} requires a lowercase kernel_sha256")
    exception_strategy = certificate.get("exception_strategy")
    if exception_strategy not in {"emscripten", "wasm"}:
        raise ValueError(f"{schema} must bind the compiled exception strategy")
    wasm_simd = certificate.get("wasm_simd")
    if not isinstance(wasm_simd, bool):
        raise ValueError(f"{schema} must bind whether Wasm SIMD was compiled")
    allocator = certificate.get("allocator")
    if allocator not in {"dlmalloc", "emmalloc"}:
        raise ValueError(f"{schema} must bind the compiled allocator")
    expected_runtime = {
        "ordinary_module_worker": True,
        "pthreads": False,
        "cross_origin_isolated": False,
        "native_wasm_exception_handling": exception_strategy == "wasm",
        "wasm_simd": wasm_simd,
    }
    if certificate.get("runtime_requirements") != expected_runtime:
        raise ValueError(f"{schema} has inconsistent runtime requirements")
    engine = _require_mapping(certificate.get("engine"), f"{schema} engine")
    engine_identity: dict[str, str] = {}
    for key in ("engine_version", "ruleset_version", "profile_id"):
        value = engine.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{schema} engine requires non-empty {key}")
        engine_identity[key] = value
    return (
        validate_memory_limits(certificate.get("memory")),
        engine_identity,
        kernel_sha256,
        exception_strategy,
    )


def _validate_root_session_config(
    value: object,
    contract: Mapping[str, Any],
    *,
    engine_profile_id: str | None = None,
) -> dict[str, object]:
    config = _require_mapping(value, "root-session certified config")
    if set(config) not in (
        ROOT_SESSION_CONFIG_KEYS,
        ROOT_SESSION_CONFIG_KEYS | {"deep_teacher_value_model"},
    ):
        raise ValueError("root-session config must exactly bind every native field")
    hard_limits = _require_mapping(
        contract.get("hard_limits"),
        "root-session contract hard limits",
    )

    def bounded_integer(
        key: str,
        minimum: int,
        maximum: int,
    ) -> int:
        candidate = config.get(key)
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or not minimum <= candidate <= maximum
        ):
            raise ValueError(f"root-session config {key} is outside its hard limit")
        return candidate

    integer_limits: dict[str, int] = {}
    for key in (
        "minimum_depth",
        "maximum_depth",
        "minimum_width",
        "maximum_width",
        "minimum_max_work",
        "maximum_max_work",
        "minimum_mate_score",
        "maximum_mate_score",
        "minimum_series_cache_capacity",
        "maximum_series_cache_capacity",
        "minimum_external_cache_weight",
        "worker_threads",
        "minimum_tt_capacity",
        "maximum_tt_capacity",
        "minimum_eval_capacity",
        "maximum_eval_capacity",
        "minimum_weight",
        "maximum_weight",
    ):
        limit = hard_limits.get(key)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError(f"root-session contract has invalid {key}")
        integer_limits[key] = limit

    max_depth = bounded_integer(
        "max_depth",
        integer_limits["minimum_depth"],
        integer_limits["maximum_depth"],
    )
    width = bounded_integer(
        "width",
        integer_limits["minimum_width"],
        integer_limits["maximum_width"],
    )
    max_work = bounded_integer(
        "max_work",
        integer_limits["minimum_max_work"],
        integer_limits["maximum_max_work"],
    )
    mate_score = bounded_integer(
        "mate_score",
        integer_limits["minimum_mate_score"],
        integer_limits["maximum_mate_score"],
    )
    series_cache = bounded_integer(
        "series_cache_capacity",
        integer_limits["minimum_series_cache_capacity"],
        integer_limits["maximum_series_cache_capacity"],
    )
    external_cache = bounded_integer(
        "external_cache_weight",
        integer_limits["minimum_external_cache_weight"],
        series_cache,
    )
    worker_threads = bounded_integer(
        "worker_threads",
        integer_limits["worker_threads"],
        integer_limits["worker_threads"],
    )
    if worker_threads != 1 or hard_limits.get(
        "external_cache_weight_lte_series_cache_capacity"
    ) is not True:
        raise ValueError("ordinary-Worker root sessions require worker_threads=1")
    tactical = config.get("root_tactical_protection")
    if (
        tactical is not False
        or hard_limits.get("root_tactical_protection_values") != [False]
        or hard_limits.get("root_tactical_policy")
        != "canonical-boundary-policy-v1"
    ):
        raise ValueError(
            "root-session config must use canonical boundary tactical policy"
        )
    tt_capacity = bounded_integer(
        "root_contract_tt_capacity",
        integer_limits["minimum_tt_capacity"],
        integer_limits["maximum_tt_capacity"],
    )
    eval_capacity = bounded_integer(
        "root_contract_eval_capacity",
        integer_limits["minimum_eval_capacity"],
        integer_limits["maximum_eval_capacity"],
    )
    weights = _require_mapping(config.get("weights"), "root-session weights")
    if set(weights) != ROOT_SESSION_WEIGHT_KEYS:
        raise ValueError("root-session config must exactly bind all seven weights")
    normalized_weights: dict[str, int] = {}
    for key in sorted(ROOT_SESSION_WEIGHT_KEYS):
        candidate = weights.get(key)
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or not integer_limits["minimum_weight"]
            <= candidate
            <= integer_limits["maximum_weight"]
        ):
            raise ValueError(f"root-session weight {key} is invalid")
        normalized_weights[key] = candidate
    normalized: dict[str, object] = {
        "max_depth": max_depth,
        "width": width,
        "max_work": max_work,
        "mate_score": mate_score,
        "series_cache_capacity": series_cache,
        "external_cache_weight": external_cache,
        "worker_threads": worker_threads,
        "root_tactical_protection": tactical,
        "root_contract_tt_capacity": tt_capacity,
        "root_contract_eval_capacity": eval_capacity,
        "weights": normalized_weights,
    }
    model = config.get("deep_teacher_value_model")
    if model is not None:
        normalized["deep_teacher_value_model"] = _validate_deep_teacher_config(
            model,
            contract,
            engine_profile_id=engine_profile_id,
            mate_score=mate_score,
        )
    return normalized


def _validate_deep_teacher_config(
    value: object,
    contract: Mapping[str, Any],
    *,
    engine_profile_id: str | None,
    mate_score: int,
) -> dict[str, object]:
    model = _require_mapping(value, "root-session deep-teacher model")
    capabilities = _require_mapping(
        contract.get("capabilities"),
        "root-session contract capabilities",
    )
    hard_limits = _require_mapping(
        contract.get("hard_limits"),
        "root-session contract hard limits",
    )
    model_limits = _require_mapping(
        hard_limits.get("deep_teacher_value_model"),
        "root-session deep-teacher hard limits",
    )
    if (
        set(model) != DEEP_TEACHER_CONFIG_KEYS
        or capabilities.get("deep_teacher_value_model") is not True
        or model_limits.get("optional") is not True
        or model_limits.get("schema") != DEEP_TEACHER_OVERLAY_SCHEMA
        or model_limits.get("feature_counts") != [7, 14, 19, 38, 44, 47]
        or model_limits.get("fixed_point_scale") != DEEP_TEACHER_FIXED_POINT_SCALE
        or model_limits.get("mate_score") != 1_000_000
        or model_limits.get("score_policy") != DEEP_TEACHER_SCORE_POLICY
        or model_limits.get("work_policy") != DEEP_TEACHER_WORK_POLICY
        or mate_score != 1_000_000
    ):
        raise ValueError("root-session deep-teacher contract is not certified")
    base_profile_id = model.get("base_profile_id")
    variant_id = model.get("variant_id")
    model_id = model.get("model_id")
    if (
        model.get("schema") != DEEP_TEACHER_OVERLAY_SCHEMA
        or not isinstance(base_profile_id, str)
        or not base_profile_id
        or engine_profile_id is not None
        and base_profile_id != engine_profile_id
        or not isinstance(variant_id, str)
        or re.fullmatch(r"spc-dtv-variant-[0-9a-f]{20}", variant_id) is None
        or not isinstance(model_id, str)
        or re.fullmatch(r"spc-dtv-[0-9a-f]{20}", model_id) is None
        or not HEX_64.fullmatch(str(model.get("model_sha256", "")))
        or not HEX_64.fullmatch(str(model.get("native_source_identity", "")))
        or model.get("score_policy") != DEEP_TEACHER_SCORE_POLICY
        or model.get("work_policy") != DEEP_TEACHER_WORK_POLICY
    ):
        raise ValueError("root-session deep-teacher identity or policy is invalid")
    feature_count = model.get("feature_count")
    coefficients = model.get("coefficients")
    if (
        isinstance(feature_count, bool)
        or feature_count not in {7, 14, 19, 38, 44, 47}
        or model.get("fixed_point_scale") != DEEP_TEACHER_FIXED_POINT_SCALE
        or not isinstance(coefficients, list)
        or len(coefficients) != feature_count
        or any(isinstance(item, bool) or not isinstance(item, int) for item in coefficients)
        or any(abs(item) > DEEP_TEACHER_FIXED_POINT_SCALE for item in coefficients)
        or max((abs(item) for item in coefficients), default=0)
        != DEEP_TEACHER_FIXED_POINT_SCALE
    ):
        raise ValueError("root-session deep-teacher coefficient shape is invalid")
    return {
        "schema": DEEP_TEACHER_OVERLAY_SCHEMA,
        "base_profile_id": base_profile_id,
        "variant_id": variant_id,
        "model_id": model_id,
        "model_sha256": model["model_sha256"],
        "native_source_identity": model["native_source_identity"],
        "feature_count": feature_count,
        "fixed_point_scale": DEEP_TEACHER_FIXED_POINT_SCALE,
        "coefficients": list(coefficients),
        "score_policy": DEEP_TEACHER_SCORE_POLICY,
        "work_policy": DEEP_TEACHER_WORK_POLICY,
    }


def _validate_value_model_asset_descriptor(
    value: object,
    configured: Mapping[str, Any],
) -> dict[str, object]:
    descriptor = _require_mapping(value, "browser value-model asset descriptor")
    expected_keys = {
        "schema",
        "file",
        "sha256",
        "model_schema",
        "model_id",
        "variant_id",
        "base_profile_id",
        "native_source_identity",
    }
    if (
        set(descriptor) != expected_keys
        or descriptor.get("schema") != VALUE_MODEL_ASSET_SCHEMA
        or descriptor.get("model_schema") != DEEP_TEACHER_MODEL_SCHEMA
        or descriptor.get("sha256") != configured.get("model_sha256")
        or descriptor.get("model_id") != configured.get("model_id")
        or descriptor.get("variant_id") != configured.get("variant_id")
        or descriptor.get("base_profile_id") != configured.get("base_profile_id")
        or descriptor.get("native_source_identity")
        != configured.get("native_source_identity")
    ):
        raise ValueError("browser value-model asset descriptor differs from its config")
    return {
        **descriptor,
        "file": _manifest_asset_name(descriptor.get("file"), ".json"),
    }


def _validate_root_geometry(
    value: object,
    memory: Mapping[str, int | bool],
    contract: Mapping[str, Any],
    *,
    engine_profile_id: str | None = None,
) -> dict[str, object]:
    geometry = _require_mapping(value, "root-session geometry")
    expected_keys = {
        "desktop_workers",
        "desktop_initial_full_wave",
        "aggregate_maximum_bytes",
        "supported_lower_geometries",
        "session_config",
        "play_limits",
    }
    if set(geometry) != expected_keys:
        raise ValueError("root-session geometry must exactly name its pool envelope")
    workers = geometry.get("desktop_workers")
    wave = geometry.get("desktop_initial_full_wave")
    aggregate = geometry.get("aggregate_maximum_bytes")
    maximum = int(memory["maximum_bytes"])
    if workers != 8 or wave != 8 or aggregate != workers * maximum:
        raise ValueError("desktop root geometry must certify workers=8 and wave=8")
    lower = geometry.get("supported_lower_geometries")
    if not isinstance(lower, list):
        raise ValueError("supported lower root geometries must be an array")
    normalized_lower: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    for item in lower:
        candidate = _require_mapping(item, "lower root geometry")
        if set(candidate) != {
            "workers",
            "initial_full_wave",
            "aggregate_maximum_bytes",
        }:
            raise ValueError("lower root geometry has unknown or missing fields")
        lower_workers = candidate.get("workers")
        lower_wave = candidate.get("initial_full_wave")
        lower_aggregate = candidate.get("aggregate_maximum_bytes")
        if (
            isinstance(lower_workers, bool)
            or not isinstance(lower_workers, int)
            or not 1 <= lower_workers < workers
            or isinstance(lower_wave, bool)
            or not isinstance(lower_wave, int)
            or not 1 <= lower_wave <= lower_workers
            or lower_aggregate != lower_workers * maximum
            or (lower_workers, lower_wave) in seen
        ):
            raise ValueError("lower root geometry is invalid or duplicated")
        seen.add((lower_workers, lower_wave))
        normalized_lower.append(dict(candidate))
    if normalized_lower != sorted(
        normalized_lower,
        key=lambda item: (item["workers"], item["initial_full_wave"]),
        reverse=True,
    ):
        raise ValueError("lower root geometries must use fastest-first canonical order")
    session_config = _validate_root_session_config(
        geometry.get("session_config"),
        contract,
        engine_profile_id=engine_profile_id,
    )
    play_limits = _require_mapping(
        geometry.get("play_limits"),
        "root-session play limits",
    )
    if set(play_limits) != {
        "maximum_seconds",
        "default_seconds",
        "default_generation_positions",
        "safety_reserve_positions",
    }:
        raise ValueError("root-session play limits must exactly bind four fields")
    maximum_seconds = play_limits.get("maximum_seconds")
    default_seconds = play_limits.get("default_seconds")
    if (
        isinstance(maximum_seconds, bool)
        or not isinstance(maximum_seconds, (int, float))
        or not 0 < float(maximum_seconds) <= 0xFFFFFFFF / 1000
        or isinstance(default_seconds, bool)
        or not isinstance(default_seconds, (int, float))
        or not 0 < float(default_seconds) <= float(maximum_seconds)
    ):
        raise ValueError("root-session play seconds are invalid")
    default_work = play_limits.get("default_generation_positions")
    safety_reserve = play_limits.get("safety_reserve_positions")
    if (
        isinstance(default_work, bool)
        or not isinstance(default_work, int)
        or not 1_000 <= default_work <= session_config["max_work"]
        or isinstance(safety_reserve, bool)
        or not isinstance(safety_reserve, int)
        or not 1 <= safety_reserve <= session_config["max_work"]
    ):
        raise ValueError("root-session play work limits are invalid")
    return {
        "desktop_workers": workers,
        "desktop_initial_full_wave": wave,
        "aggregate_maximum_bytes": aggregate,
        "supported_lower_geometries": normalized_lower,
        "session_config": session_config,
        "play_limits": {
            "maximum_seconds": maximum_seconds,
            "default_seconds": default_seconds,
            "default_generation_positions": default_work,
            "safety_reserve_positions": safety_reserve,
        },
    }


def validate_root_session_certificate(
    certificate: Mapping[str, Any],
    *,
    source_fingerprint: str,
    wasm_sha256: str,
    module_js_sha256: str,
    runtime_variant: str,
    thread_count: int,
    support_files: list[dict[str, str]],
) -> tuple[
    dict[str, int | bool],
    dict[str, str],
    str,
    str,
    dict[str, object],
    dict[str, object],
]:
    memory, engine, kernel_sha256, exception_strategy = (
        _validate_combined_capability_identity(
            certificate,
            schema=ROOT_SESSION_CERTIFICATE_SCHEMA,
            abi_version=2,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant=runtime_variant,
            thread_count=thread_count,
            support_files=support_files,
        )
    )
    if (
        certificate.get("root_session_certified") is not True
        or certificate.get("reply_mate_safety") is not False
    ):
        raise ValueError("root-session certificate must not claim reply-mate safety")
    contract = _require_mapping(
        certificate.get("root_session_contract"),
        "root-session contract",
    )
    if (
        contract.get("schema") != ROOT_SESSION_CONTRACT_SCHEMA
        or contract.get("abi_version") != 2
        or contract.get("worker_threads") != 1
        or contract.get("pthreads_required") is not False
        or contract.get("one_active_session_per_worker") is not True
        or contract.get("product_publishable") is not False
        or contract.get("reply_mate_safety") is not False
    ):
        raise ValueError("root-session certificate carries an invalid native contract")
    capabilities = _require_mapping(
        contract.get("capabilities"),
        "root-session contract capabilities",
    )
    if any(
        capabilities.get(key) is not True
        for key in (
            "enumerate",
            "import",
            "search",
            "call_work_credit",
            "hard_memory_limit",
            "tt_scout_rollback",
            "persistent_depth_reuse",
            "aspiration_windows",
            "selected_owner_certification",
            "canonical_root_tactical_policy",
            "checked_horizon_proof_research",
        )
    ):
        raise ValueError("root-session contract lacks coordinator capabilities")
    request_schemas = _require_mapping(
        contract.get("request_schemas"),
        "root-session contract request schemas",
    )
    result_schemas = _require_mapping(
        contract.get("result_schemas"),
        "root-session contract result schemas",
    )
    hard_limits = _require_mapping(
        contract.get("hard_limits"),
        "root-session contract hard limits",
    )
    horizon_research = _require_mapping(
        contract.get("horizon_research"),
        "root-session checked-horizon policy",
    )
    if (
        hard_limits.get("minimum_aspiration_initial_delta") != 2_048
        or hard_limits.get("maximum_aspiration_attempts") != 4
    ):
        raise ValueError("root-session contract lacks certified aspiration limits")
    if (
        request_schemas.get("search") != "spc-root-candidate-task-v1"
        or result_schemas.get("search") != "spc-root-candidate-result-v1"
        or request_schemas.get("horizon_research")
        != "spc-root-horizon-research-task-v1"
        or result_schemas.get("horizon_research")
        != "spc-root-horizon-research-result-v1"
        or hard_limits.get("maximum_horizon_proofs") != 16
        or hard_limits.get("maximum_horizon_proof_path") != 8
        or horizon_research
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
        raise ValueError("root-session contract lacks checked-horizon re-search policy")
    _validate_checked_horizon_evidence(
        certificate.get("checked_horizon_proof_research"),
        label="root-session checked-horizon evidence",
    )
    evidence = _require_mapping(certificate.get("evidence"), "root-session evidence")
    required_true = (
        "deterministic_node_smoke",
        "combined_artifact",
        "enumerate_import_search",
        "exact_manifest_import",
        "persistent_d1_d2_session",
        "aspiration_fail_soft_window",
        "aspiration_fail_high_low_white_black",
        "cumulative_work_and_cache_receipts",
        "configured_max_depth_rejected",
        "per_call_work_credit",
        "selected_owner_warm_exact_certification",
        "checked_horizon_proof_research",
        "checked_horizon_newest_proof_hit",
        "deadline_fail_closed",
        "work_limit_fail_closed",
        "browser_worker_smoke",
        "opera_worker_smoke",
    )
    if evidence.get("failures") != 0 or any(
        evidence.get(key) is not True for key in required_true
    ):
        raise ValueError("root-session certificate is missing required passing evidence")
    cases = evidence.get("differential_cases")
    elapsed = evidence.get("start_w32_d5_elapsed_seconds")
    if (
        isinstance(cases, bool)
        or not isinstance(cases, int)
        or cases < 1
        or evidence.get("start_w32_d5_completed_depth") != 5
        or evidence.get("start_w32_d5_width") != 32
        or isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not 0 <= float(elapsed) < 60
    ):
        raise ValueError("root-session certificate lacks the exact W32 D5 gate")
    geometry = _validate_root_geometry(
        certificate.get("geometry"),
        memory,
        contract,
        engine_profile_id=engine["profile_id"],
    )
    configured_model = geometry["session_config"].get(
        "deep_teacher_value_model"
    )
    asset_descriptor = certificate.get("value_model_asset")
    if configured_model is None:
        if asset_descriptor is not None:
            raise ValueError(
                "baseline root-session certificate cannot claim a value-model asset"
            )
    else:
        _validate_value_model_asset_descriptor(asset_descriptor, configured_model)
        for key in (
            "value_model_asset_bound",
            "value_model_native_python_wasm_parity",
            "value_model_browser_worker_smoke",
            "value_model_strength_gate",
        ):
            if evidence.get(key) is not True:
                raise ValueError(
                    "modeled root-session certificate lacks activation evidence"
                )
    return (
        memory,
        engine,
        kernel_sha256,
        exception_strategy,
        dict(contract),
        geometry,
    )


def validate_mate_certificate(
    certificate: Mapping[str, Any],
    *,
    source_fingerprint: str,
    wasm_sha256: str,
    module_js_sha256: str,
    runtime_variant: str,
    thread_count: int,
    support_files: list[dict[str, str]],
) -> tuple[dict[str, int | bool], dict[str, str], str, str]:
    memory, engine, kernel_sha256, exception_strategy = (
        _validate_combined_capability_identity(
            certificate,
            schema=MATE_CERTIFICATE_SCHEMA,
            abi_version=1,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant=runtime_variant,
            thread_count=thread_count,
            support_files=support_files,
        )
    )
    if (
        certificate.get("mate_capability_certified") is not True
        or certificate.get("reply_mate_safety") is not True
    ):
        raise ValueError("mate certificate must explicitly certify reply-mate safety")
    evidence = _require_mapping(certificate.get("evidence"), "mate evidence")
    required_true = (
        "combined_artifact",
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
        "browser_worker_smoke",
    )
    cases = evidence.get("differential_cases")
    if (
        evidence.get("failures") != 0
        or any(evidence.get(key) is not True for key in required_true)
        or isinstance(cases, bool)
        or not isinstance(cases, int)
        or cases < MIN_MATE_DIFFERENTIAL_CASES
    ):
        raise ValueError("mate certificate is missing required parity evidence")
    return memory, engine, kernel_sha256, exception_strategy


def validate_certificate(
    certificate: Mapping[str, Any],
    *,
    source_fingerprint: str,
    wasm_sha256: str,
    module_js_sha256: str,
    runtime_variant: str,
    thread_count: int,
    support_files: list[dict[str, str]],
) -> dict[str, int | bool]:
    expected = {
        "schema": CERTIFICATE_SCHEMA,
        "status": "certified",
        "safety_certified": True,
        "contract_version": 1,
        "abi_version": 1,
        "source_fingerprint": source_fingerprint,
        "wasm_sha256": wasm_sha256,
        "module_js_sha256": module_js_sha256,
        "runtime_variant": runtime_variant,
        "thread_count": thread_count,
        "support_files": support_files,
    }
    for key, value in expected.items():
        if certificate.get(key) != value:
            raise ValueError(
                f"safety certificate {key!r} does not match the artifact: "
                f"expected {value!r}, found {certificate.get(key)!r}"
            )
    certificate_id = certificate.get("certificate_id")
    if not isinstance(certificate_id, str) or not certificate_id.strip():
        raise ValueError("safety certificate requires a non-empty certificate_id")
    evidence = _require_mapping(certificate.get("evidence"), "certificate evidence")
    required_evidence = {
        "failures": 0,
        "start_position_parity": True,
        "s4_mate_safety": True,
        "interrupted_depth_publication": True,
        "compiled_legal_series_validation": True,
        "compiled_authoritative_replay": True,
        "start_w32_d5_completed_depth": 5,
        "start_w32_d5_width": 32,
    }
    for key, value in required_evidence.items():
        if evidence.get(key) != value:
            raise ValueError(
                f"safety certificate evidence {key!r} must be {value!r}"
            )
    differential_cases = evidence.get("differential_cases")
    if (
        isinstance(differential_cases, bool)
        or not isinstance(differential_cases, int)
        or differential_cases < 1
    ):
        raise ValueError("safety certificate requires positive differential_cases")
    elapsed = evidence.get("start_w32_d5_elapsed_seconds")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not 0 <= float(elapsed) < 60
    ):
        raise ValueError(
            "safety certificate requires a completed under-60-second W32 D5 receipt"
        )
    engine = _require_mapping(certificate.get("engine"), "certificate engine")
    for key in (
        "engine_profile_id",
        "engine_profile_name",
        "engine_version",
        "ruleset_version",
    ):
        value = engine.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"safety certificate engine requires non-empty {key}")
    limits = _require_mapping(engine.get("analysis_limits"), "engine analysis limits")
    integer_limits = {
        "maximum_depth": (1, 64),
        "maximum_max_series": (1, 16_384),
        "maximum_generation_positions": (1_000, 0xFFFFFFFF),
        "default_depth": (1, 64),
        "default_max_series": (1, 16_384),
        "default_generation_positions": (1_000, 0xFFFFFFFF),
    }
    for key, (minimum, maximum) in integer_limits.items():
        value = limits.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise ValueError(
                f"engine analysis limit {key} must be an integer from "
                f"{minimum} through {maximum}"
            )
    for key in ("maximum_seconds", "default_seconds"):
        value = limits.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 < float(value) <= 0xFFFFFFFF / 1000
        ):
            raise ValueError(f"engine analysis limit {key} must be positive")
    if limits["default_depth"] > limits["maximum_depth"]:
        raise ValueError("default_depth exceeds maximum_depth")
    if limits["default_max_series"] > limits["maximum_max_series"]:
        raise ValueError("default_max_series exceeds maximum_max_series")
    if limits["default_seconds"] > limits["maximum_seconds"]:
        raise ValueError("default_seconds exceeds maximum_seconds")
    if (
        limits["default_generation_positions"]
        > limits["maximum_generation_positions"]
    ):
        raise ValueError(
            "default_generation_positions exceeds maximum_generation_positions"
        )
    return validate_memory_limits(certificate.get("memory"))


def _safe_support_file(path: Path) -> None:
    if (
        not re.fullmatch(r"[A-Za-z0-9._-]+\.js", path.name)
        or ".." in path.name
    ):
        raise ValueError(f"unsafe WebAssembly support-file name: {path.name!r}")


def _validate_module_wasm_dependency(module_js: Path, wasm_name: str) -> None:
    expected_name = _manifest_asset_name(wasm_name, ".wasm")
    try:
        wrapper = module_js.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"could not read Emscripten module wrapper: {error}") from error
    wasm_requests: list[str] = []
    for call in LOCATE_FILE_CALL.finditer(wrapper):
        declaration_prefix = wrapper[max(0, call.start() - 32) : call.start()]
        if re.search(r"\bfunction\s*$", declaration_prefix):
            continue
        literal = LOCATE_FILE_LITERAL_CALL.match(wrapper, call.start())
        if literal is None:
            raise ValueError(
                "verified Emscripten wrapper must bind locateFile dependencies "
                "with literal asset names"
            )
        asset = literal.group("asset")
        if asset.endswith(".wasm"):
            wasm_requests.append(_manifest_asset_name(asset, ".wasm"))
    if not wasm_requests:
        raise ValueError(
            "verified Emscripten wrapper has no literal locateFile WASM dependency"
        )
    if set(wasm_requests) != {expected_name}:
        raise ValueError(
            "verified Emscripten wrapper WASM dependency does not match the input "
            f"basename: expected {expected_name!r}, found {sorted(set(wasm_requests))!r}"
        )


def _build_variant(
    *,
    runtime_variant: str,
    wasm: Path,
    module_js: Path,
    certificate_path: Path | None,
    prefix_certificate_path: Path | None,
    root_session_certificate_path: Path | None,
    value_model_root_session_certificate_path: Path | None,
    value_model_path: Path | None,
    mate_certificate_path: Path | None,
    support_paths: tuple[Path, ...],
    source_fingerprint: str,
    value_model_native_source_identity: str | None,
    destination: Path,
) -> Mapping[str, Any]:
    if runtime_variant == "single" and support_paths:
        raise ValueError(
            "the verified single-thread lane may not load external support files"
        )
    if all(
        path is None
        for path in (
            certificate_path,
            prefix_certificate_path,
            root_session_certificate_path,
            mate_certificate_path,
        )
    ):
        raise ValueError(
            f"{runtime_variant} requires at least one capability certificate"
        )
    if (value_model_root_session_certificate_path is None) != (
        value_model_path is None
    ):
        raise ValueError(
            "value-model asset and modeled root-session certificate are all-or-none"
        )
    if value_model_root_session_certificate_path is not None and (
        root_session_certificate_path is None
    ):
        raise ValueError(
            "value-model activation requires a certified baseline root-session fallback"
        )
    if (
        value_model_root_session_certificate_path is not None
        and certificate_path is not None
    ):
        raise ValueError(
            "value-model activation cannot share one identity with the baseline "
            "analysis capability"
        )
    required_paths = [
        (wasm, "WebAssembly binary"),
        (module_js, "Emscripten module"),
        *((path, "WebAssembly support file") for path in support_paths),
    ]
    if certificate_path is not None:
        required_paths.append((certificate_path, "safety certificate"))
    if prefix_certificate_path is not None:
        required_paths.append((prefix_certificate_path, "prefix certificate"))
    if root_session_certificate_path is not None:
        required_paths.append((root_session_certificate_path, "root-session certificate"))
    if value_model_root_session_certificate_path is not None:
        required_paths.append(
            (
                value_model_root_session_certificate_path,
                "value-model root-session certificate",
            )
        )
    if value_model_path is not None:
        required_paths.append((value_model_path, "deep-teacher value-model asset"))
    if mate_certificate_path is not None:
        required_paths.append((mate_certificate_path, "mate certificate"))
    for path, label in required_paths:
        if not path.is_file():
            raise FileNotFoundError(f"{runtime_variant} {label} is missing: {path}")
    wasm_name = _manifest_asset_name(wasm.name, ".wasm")
    certificate = load_certificate(certificate_path) if certificate_path else None
    prefix_certificate = (
        load_certificate(prefix_certificate_path)
        if prefix_certificate_path
        else None
    )
    root_session_certificate = (
        load_certificate(root_session_certificate_path)
        if root_session_certificate_path
        else None
    )
    value_model_root_session_certificate = (
        load_certificate(value_model_root_session_certificate_path)
        if value_model_root_session_certificate_path
        else None
    )
    mate_certificate = (
        load_certificate(mate_certificate_path)
        if mate_certificate_path
        else None
    )
    certificate_thread_counts = [
        value.get("thread_count")
        for value in (
            certificate,
            prefix_certificate,
            root_session_certificate,
            value_model_root_session_certificate,
            mate_certificate,
        )
        if value is not None
    ]
    if any(value != certificate_thread_counts[0] for value in certificate_thread_counts):
        raise ValueError("search and prefix certificates disagree on thread_count")
    thread_count = certificate_thread_counts[0]
    if (
        isinstance(thread_count, bool)
        or not isinstance(thread_count, int)
        or thread_count < 1
        or (runtime_variant == "single" and thread_count != 1)
        or (runtime_variant == "pthread" and thread_count < 2)
    ):
        raise ValueError(
            f"{runtime_variant} certificate has invalid thread_count"
        )
    wasm_sha256 = sha256_file(wasm)
    module_js_sha256 = sha256_file(module_js)
    support_files: list[dict[str, str]] = []
    seen_support_names: set[str] = set()
    for path in support_paths:
        _safe_support_file(path)
        if path.name in seen_support_names or path.name == "spc-engine.js":
            raise ValueError(f"duplicate WebAssembly support-file name: {path.name}")
        seen_support_names.add(path.name)
        support_files.append({"name": path.name, "sha256": sha256_file(path)})
    support_files.sort(key=lambda item: item["name"])
    search_memory = (
        validate_certificate(
            certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant=runtime_variant,
            thread_count=thread_count,
            support_files=support_files,
        )
        if certificate is not None
        else None
    )
    prefix_contract: dict[str, object] | None = None
    prefix_memory: dict[str, int | bool] | None = None
    prefix_engine: dict[str, str] | None = None
    if prefix_certificate is not None:
        prefix_contract, prefix_memory, prefix_engine = validate_prefix_certificate(
            prefix_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant=runtime_variant,
            thread_count=thread_count,
            support_files=support_files,
        )
    root_memory: dict[str, int | bool] | None = None
    root_engine: dict[str, str] | None = None
    root_kernel_sha256: str | None = None
    root_exception_strategy: str | None = None
    root_contract: dict[str, object] | None = None
    root_geometry: dict[str, object] | None = None
    if root_session_certificate is not None:
        (
            root_memory,
            root_engine,
            root_kernel_sha256,
            root_exception_strategy,
            root_contract,
            root_geometry,
        ) = validate_root_session_certificate(
            root_session_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant=runtime_variant,
            thread_count=thread_count,
            support_files=support_files,
        )
    value_model_memory: dict[str, int | bool] | None = None
    value_model_engine: dict[str, str] | None = None
    value_model_kernel_sha256: str | None = None
    value_model_exception_strategy: str | None = None
    value_model_contract: dict[str, object] | None = None
    value_model_geometry: dict[str, object] | None = None
    value_model_asset: dict[str, object] | None = None
    if value_model_root_session_certificate is not None:
        (
            value_model_memory,
            value_model_engine,
            value_model_kernel_sha256,
            value_model_exception_strategy,
            value_model_contract,
            value_model_geometry,
        ) = validate_root_session_certificate(
            value_model_root_session_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant=runtime_variant,
            thread_count=thread_count,
            support_files=support_files,
        )
        assert value_model_path is not None
        configured_model = _require_mapping(
            value_model_geometry["session_config"].get(
                "deep_teacher_value_model"
            ),
            "modeled root-session config",
        )
        if (
            value_model_native_source_identity is None
            or configured_model.get("native_source_identity")
            != value_model_native_source_identity
        ):
            raise ValueError(
                "deep-teacher model native source identity differs from the source package"
            )
        value_model_asset = _validate_original_deep_teacher_model(
            value_model_path,
            configured_model,
        )
        certified_asset = _validate_value_model_asset_descriptor(
            value_model_root_session_certificate.get("value_model_asset"),
            configured_model,
        )
        if value_model_asset != certified_asset:
            raise ValueError(
                "deep-teacher model asset differs from its activation certificate"
            )
    mate_memory: dict[str, int | bool] | None = None
    mate_engine: dict[str, str] | None = None
    mate_kernel_sha256: str | None = None
    mate_exception_strategy: str | None = None
    if mate_certificate is not None:
        (
            mate_memory,
            mate_engine,
            mate_kernel_sha256,
            mate_exception_strategy,
        ) = validate_mate_certificate(
            mate_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant=runtime_variant,
            thread_count=thread_count,
            support_files=support_files,
        )
    if search_memory is not None and prefix_memory is not None:
        if search_memory != prefix_memory:
            raise ValueError(
                "search and prefix certificates require identical memory envelopes"
            )
        assert certificate is not None
        assert prefix_engine is not None
        search_engine = _require_mapping(certificate.get("engine"), "certificate engine")
        for key in ("engine_version", "ruleset_version"):
            if search_engine.get(key) != prefix_engine[key]:
                raise ValueError(
                    f"search and prefix certificates disagree on {key}"
                )
    capability_memories = [
        (name, memory)
        for name, memory in (
            ("search", search_memory),
            ("prefix", prefix_memory),
            ("root-session", root_memory),
            ("value-model root-session", value_model_memory),
            ("mate", mate_memory),
        )
        if memory is not None
    ]
    reference_memory = capability_memories[0][1]
    if any(memory != reference_memory for _, memory in capability_memories[1:]):
        raise ValueError("combined capability certificates have different memory envelopes")
    engine_versions: list[tuple[str, str, str]] = []
    if certificate is not None:
        search_engine_value = _require_mapping(certificate.get("engine"), "search engine")
        engine_versions.append(
            (
                "search",
                str(search_engine_value.get("engine_version", "")),
                str(search_engine_value.get("ruleset_version", "")),
            )
        )
    for name, engine in (
        ("prefix", prefix_engine),
        ("root-session", root_engine),
        ("value-model root-session", value_model_engine),
        ("mate", mate_engine),
    ):
        if engine is not None:
            engine_versions.append(
                (name, engine["engine_version"], engine["ruleset_version"])
            )
    reference_engine = engine_versions[0][1:]
    if any(identity[1:] != reference_engine for identity in engine_versions[1:]):
        raise ValueError("combined capability certificates have different engine identities")
    if root_engine is not None and mate_engine is not None:
        if root_engine["profile_id"] != mate_engine["profile_id"]:
            raise ValueError("root-session and mate certificates have different profiles")
    if root_engine is not None and value_model_engine is not None:
        if root_engine != value_model_engine:
            raise ValueError(
                "baseline and modeled root-session certificates have different engines"
            )
    if root_kernel_sha256 is not None and value_model_kernel_sha256 is not None:
        if root_kernel_sha256 != value_model_kernel_sha256:
            raise ValueError(
                "baseline and modeled root-session certificates have different kernels"
            )
    if root_exception_strategy is not None and value_model_exception_strategy is not None:
        if root_exception_strategy != value_model_exception_strategy:
            raise ValueError(
                "baseline and modeled root-session certificates have different runtimes"
            )
    if root_contract is not None and value_model_contract is not None:
        if root_contract != value_model_contract:
            raise ValueError(
                "baseline and modeled root-session certificates have different contracts"
            )
    if (
        root_session_certificate is not None
        and value_model_root_session_certificate is not None
        and any(
            root_session_certificate.get(key)
            != value_model_root_session_certificate.get(key)
            for key in ("wasm_simd", "allocator", "runtime_requirements")
        )
    ):
        raise ValueError(
            "baseline and modeled root-session certificates have different runtimes"
        )
    if root_geometry is not None and value_model_geometry is not None:
        baseline_geometry = dict(root_geometry)
        modeled_geometry = dict(value_model_geometry)
        baseline_config = dict(
            _require_mapping(
                baseline_geometry.pop("session_config"),
                "baseline root-session config",
            )
        )
        modeled_config = dict(
            _require_mapping(
                modeled_geometry.pop("session_config"),
                "modeled root-session config",
            )
        )
        modeled_config.pop("deep_teacher_value_model", None)
        if baseline_geometry != modeled_geometry or baseline_config != modeled_config:
            raise ValueError(
                "modeled root-session certificate changes non-model play geometry"
            )
    if certificate is not None and root_engine is not None:
        search_profile = _require_mapping(certificate.get("engine"), "search engine").get(
            "engine_profile_id"
        )
        if search_profile != root_engine["profile_id"]:
            raise ValueError("search and root-session certificates have different profiles")
    if (
        root_kernel_sha256 is not None
        and mate_kernel_sha256 is not None
        and root_kernel_sha256 != mate_kernel_sha256
    ):
        raise ValueError("root-session and mate certificates have different kernels")
    if (
        root_exception_strategy is not None
        and mate_exception_strategy is not None
        and root_exception_strategy != mate_exception_strategy
    ):
        raise ValueError("root-session and mate certificates have different exception strategies")
    if (
        root_session_certificate is not None
        and mate_certificate is not None
        and (
            root_session_certificate.get("wasm_simd")
            != mate_certificate.get("wasm_simd")
            or root_session_certificate.get("runtime_requirements")
            != mate_certificate.get("runtime_requirements")
            or root_session_certificate.get("allocator")
            != mate_certificate.get("allocator")
        )
    ):
        raise ValueError("root-session and mate certificates have different runtimes")

    _validate_module_wasm_dependency(module_js, wasm_name)
    destination.mkdir(parents=True)
    shutil.copyfile(wasm, destination / wasm_name)
    shutil.copyfile(module_js, destination / "spc-engine.js")
    by_name = {path.name: path for path in support_paths}
    for item in support_files:
        shutil.copyfile(by_name[item["name"]], destination / item["name"])
    if value_model_path is not None:
        assert value_model_asset is not None
        shutil.copyfile(
            value_model_path,
            destination / str(value_model_asset["file"]),
        )
    variant: dict[str, Any] = {
        "thread_count": thread_count,
        "wasm": wasm_name,
        "wasm_sha256": wasm_sha256,
        "module_js": "spc-engine.js",
        "module_js_sha256": module_js_sha256,
        "support_files": support_files,
    }
    if root_kernel_sha256 is not None or mate_kernel_sha256 is not None:
        variant["kernel_sha256"] = root_kernel_sha256 or mate_kernel_sha256
    if certificate is not None:
        assert search_memory is not None
        variant["safety_certificate"] = {
            "schema": CERTIFICATE_SCHEMA,
            "status": "certified",
            "safety_certified": True,
            "certificate_id": certificate["certificate_id"],
            "contract_version": 1,
            "abi_version": 1,
            "source_fingerprint": source_fingerprint,
            "runtime_variant": runtime_variant,
            "thread_count": thread_count,
            "wasm_sha256": wasm_sha256,
            "module_js_sha256": module_js_sha256,
            "support_files": support_files,
            "memory": search_memory,
            "evidence": dict(_require_mapping(certificate["evidence"], "evidence")),
            "engine": dict(_require_mapping(certificate["engine"], "engine")),
        }
    if prefix_certificate is not None:
        assert prefix_contract is not None
        assert prefix_memory is not None
        assert prefix_engine is not None
        variant["prefix_certificate"] = {
            "status": "certified",
            "contract_version": 1,
            "certificate_id": prefix_certificate["certificate_id"],
            "source_fingerprint": source_fingerprint,
            "runtime_variant": runtime_variant,
            "thread_count": thread_count,
            "wasm_sha256": wasm_sha256,
            "module_js_sha256": module_js_sha256,
            "support_files": support_files,
            "memory": prefix_memory,
            "evidence": dict(
                _require_mapping(prefix_certificate["evidence"], "prefix evidence")
            ),
            "engine": prefix_engine,
            "prefix_contract": prefix_contract,
        }
    if root_session_certificate is not None:
        assert root_memory is not None
        assert root_engine is not None
        assert root_kernel_sha256 is not None
        assert root_exception_strategy is not None
        assert root_contract is not None
        assert root_geometry is not None
        variant["root_session_certificate"] = {
            "schema": ROOT_SESSION_CERTIFICATE_SCHEMA,
            "status": "certified",
            "certificate_id": root_session_certificate["certificate_id"],
            "contract_version": 1,
            "abi_version": 2,
            "root_session_certified": True,
            "reply_mate_safety": False,
            "product_publishable": False,
            "source_fingerprint": source_fingerprint,
            "kernel_sha256": root_kernel_sha256,
            "runtime_variant": runtime_variant,
            "thread_count": thread_count,
            "wasm_sha256": wasm_sha256,
            "module_js_sha256": module_js_sha256,
            "support_files": support_files,
            "exports": COMBINED_EXPORTS,
            "exception_strategy": root_exception_strategy,
            "wasm_simd": root_session_certificate["wasm_simd"],
            "allocator": root_session_certificate["allocator"],
            "runtime_requirements": dict(
                _require_mapping(
                    root_session_certificate["runtime_requirements"],
                    "root runtime requirements",
                )
            ),
            "memory": root_memory,
            "engine": root_engine,
            "root_session_contract": root_contract,
            "checked_horizon_proof_research": dict(
                _require_mapping(
                    root_session_certificate["checked_horizon_proof_research"],
                    "root checked-horizon evidence",
                )
            ),
            "geometry": root_geometry,
            "evidence": dict(
                _require_mapping(root_session_certificate["evidence"], "root evidence")
            ),
        }
    if value_model_root_session_certificate is not None:
        assert value_model_memory is not None
        assert value_model_engine is not None
        assert value_model_kernel_sha256 is not None
        assert value_model_exception_strategy is not None
        assert value_model_contract is not None
        assert value_model_geometry is not None
        assert value_model_asset is not None
        modeled_certificate = {
            "schema": ROOT_SESSION_CERTIFICATE_SCHEMA,
            "status": "certified",
            "certificate_id": value_model_root_session_certificate["certificate_id"],
            "contract_version": 1,
            "abi_version": 2,
            "root_session_certified": True,
            "reply_mate_safety": False,
            "product_publishable": False,
            "source_fingerprint": source_fingerprint,
            "kernel_sha256": value_model_kernel_sha256,
            "runtime_variant": runtime_variant,
            "thread_count": thread_count,
            "wasm_sha256": wasm_sha256,
            "module_js_sha256": module_js_sha256,
            "support_files": support_files,
            "exports": COMBINED_EXPORTS,
            "exception_strategy": value_model_exception_strategy,
            "wasm_simd": value_model_root_session_certificate["wasm_simd"],
            "allocator": value_model_root_session_certificate["allocator"],
            "runtime_requirements": dict(
                _require_mapping(
                    value_model_root_session_certificate["runtime_requirements"],
                    "modeled root runtime requirements",
                )
            ),
            "memory": value_model_memory,
            "engine": value_model_engine,
            "root_session_contract": value_model_contract,
            "checked_horizon_proof_research": dict(
                _require_mapping(
                    value_model_root_session_certificate[
                        "checked_horizon_proof_research"
                    ],
                    "modeled root checked-horizon evidence",
                )
            ),
            "geometry": value_model_geometry,
            "value_model_asset": value_model_asset,
            "evidence": dict(
                _require_mapping(
                    value_model_root_session_certificate["evidence"],
                    "modeled root evidence",
                )
            ),
        }
        variant["value_model_activation"] = {
            "schema": VALUE_MODEL_ACTIVATION_SCHEMA,
            "status": "certified",
            "asset": value_model_asset,
            "root_session_certificate": modeled_certificate,
        }
    if mate_certificate is not None:
        assert mate_memory is not None
        assert mate_engine is not None
        assert mate_kernel_sha256 is not None
        assert mate_exception_strategy is not None
        variant["mate_certificate"] = {
            "schema": MATE_CERTIFICATE_SCHEMA,
            "status": "certified",
            "certificate_id": mate_certificate["certificate_id"],
            "contract_version": 1,
            "abi_version": 1,
            "mate_capability_certified": True,
            "reply_mate_safety": True,
            "product_publishable": False,
            "source_fingerprint": source_fingerprint,
            "kernel_sha256": mate_kernel_sha256,
            "runtime_variant": runtime_variant,
            "thread_count": thread_count,
            "wasm_sha256": wasm_sha256,
            "module_js_sha256": module_js_sha256,
            "support_files": support_files,
            "exports": COMBINED_EXPORTS,
            "exception_strategy": mate_exception_strategy,
            "wasm_simd": mate_certificate["wasm_simd"],
            "allocator": mate_certificate["allocator"],
            "runtime_requirements": dict(
                _require_mapping(
                    mate_certificate["runtime_requirements"],
                    "mate runtime requirements",
                )
            ),
            "memory": mate_memory,
            "engine": mate_engine,
            "evidence": dict(
                _require_mapping(mate_certificate["evidence"], "mate evidence")
            ),
        }
    return variant


def build_bundle(
    *,
    single_wasm: Path,
    single_module_js: Path,
    single_certificate_path: Path | None = None,
    single_prefix_certificate_path: Path | None = None,
    single_root_session_certificate_path: Path | None = None,
    single_value_model_root_session_certificate_path: Path | None = None,
    single_value_model_path: Path | None = None,
    single_mate_certificate_path: Path | None = None,
    single_support_paths: tuple[Path, ...] = (),
    pthread_wasm: Path | None = None,
    pthread_module_js: Path | None = None,
    pthread_certificate_path: Path | None = None,
    pthread_support_paths: tuple[Path, ...] = (),
    source_package: Path,
    output: Path,
) -> Mapping[str, Any]:
    if not source_package.is_dir():
        raise FileNotFoundError(f"engine source package is missing: {source_package}")
    if output.exists():
        raise FileExistsError(f"browser engine output already exists: {output}")
    pthread_inputs = (
        pthread_wasm,
        pthread_module_js,
        pthread_certificate_path,
    )
    if any(value is not None for value in pthread_inputs) and not all(
        value is not None for value in pthread_inputs
    ):
        raise ValueError("pthread wasm, module, and certificate are all-or-none")
    if pthread_support_paths and not all(value is not None for value in pthread_inputs):
        raise ValueError("pthread support files require a pthread artifact")
    if any(value is not None for value in pthread_inputs) or pthread_support_paths:
        raise ValueError(
            "pthread publishing is disabled until its wrapper and worker support "
            "code execute from verified bytes"
        )

    source_fingerprint = engine_source_fingerprint(source_package)
    if not HEX_16.fullmatch(source_fingerprint):
        raise ValueError("calculated engine source fingerprint is invalid")
    value_model_native_source_identity = (
        native_source_identity(source_package)
        if single_value_model_root_session_certificate_path is not None
        else None
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}.", dir=output.parent
    ) as temporary_name:
        staging = Path(temporary_name) / "bundle"
        staging.mkdir()
        variants: dict[str, Mapping[str, Any]] = {
            "single": _build_variant(
                runtime_variant="single",
                wasm=single_wasm,
                module_js=single_module_js,
                certificate_path=single_certificate_path,
                prefix_certificate_path=single_prefix_certificate_path,
                root_session_certificate_path=single_root_session_certificate_path,
                value_model_root_session_certificate_path=(
                    single_value_model_root_session_certificate_path
                ),
                value_model_path=single_value_model_path,
                mate_certificate_path=single_mate_certificate_path,
                support_paths=single_support_paths,
                source_fingerprint=source_fingerprint,
                value_model_native_source_identity=(
                    value_model_native_source_identity
                ),
                destination=staging / "single",
            )
        }
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "contract_version": 1,
            "abi_version": 1,
            "source_fingerprint": source_fingerprint,
            "variants": variants,
        }

        (staging / "browser-engine-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        validate_existing_bundle(staging, source_package)
        staging.replace(output)
    return manifest


def _manifest_asset_name(value: object, extension: str) -> str:
    if (
        not isinstance(value, str)
        or not value.endswith(extension)
        or not re.fullmatch(r"[A-Za-z0-9._-]+", value)
        or ".." in value
    ):
        raise ValueError(f"unsafe browser bundle asset name: {value!r}")
    return value


def validate_existing_bundle(bundle: Path, source_package: Path) -> Mapping[str, Any]:
    if not bundle.is_dir():
        raise FileNotFoundError(f"browser engine bundle is missing: {bundle}")
    if not source_package.is_dir():
        raise FileNotFoundError(f"engine source package is missing: {source_package}")
    manifest_path = bundle / "browser-engine-manifest.json"
    try:
        manifest = _require_mapping(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            "browser engine manifest",
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read browser engine manifest: {error}") from error
    source_fingerprint = engine_source_fingerprint(source_package)
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("contract_version") != 1
        or manifest.get("abi_version") != 1
        or manifest.get("source_fingerprint") != source_fingerprint
    ):
        raise ValueError("browser engine manifest does not match the checked-out source")
    variants = _require_mapping(manifest.get("variants"), "browser engine variants")
    if set(variants) != {"single"}:
        raise ValueError("only the verified single-thread browser lane may be published")
    variant = _require_mapping(variants["single"], "single browser engine variant")
    if variant.get("thread_count") != 1:
        raise ValueError("single browser engine variant must use exactly one thread")
    if variant.get("support_files") != []:
        raise ValueError("single browser engine variant may not load support files")
    wasm_name = _manifest_asset_name(variant.get("wasm"), ".wasm")
    module_name = _manifest_asset_name(variant.get("module_js"), ".js")
    lane = bundle / "single"
    wasm = lane / wasm_name
    module_js = lane / module_name
    if not wasm.is_file() or not module_js.is_file():
        raise FileNotFoundError("certified single-lane WASM artifacts are missing")
    wasm_sha256 = sha256_file(wasm)
    module_js_sha256 = sha256_file(module_js)
    if (
        not HEX_64.fullmatch(str(variant.get("wasm_sha256", "")))
        or not HEX_64.fullmatch(str(variant.get("module_js_sha256", "")))
        or variant.get("wasm_sha256") != wasm_sha256
        or variant.get("module_js_sha256") != module_js_sha256
    ):
        raise ValueError("browser engine bundle artifact hash mismatch")
    _validate_module_wasm_dependency(module_js, wasm_name)
    certificate_value = variant.get("safety_certificate")
    prefix_certificate_value = variant.get("prefix_certificate")
    root_certificate_value = variant.get("root_session_certificate")
    value_model_activation_value = variant.get("value_model_activation")
    mate_certificate_value = variant.get("mate_certificate")
    if all(
        value is None
        for value in (
            certificate_value,
            prefix_certificate_value,
            root_certificate_value,
            mate_certificate_value,
        )
    ):
        raise ValueError("single browser lane has no certified capability")
    search_memory: dict[str, int | bool] | None = None
    search_engine: Mapping[str, Any] | None = None
    if certificate_value is not None:
        certificate = _require_mapping(
            certificate_value,
            "single safety certificate",
        )
        search_memory = validate_certificate(
            certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant="single",
            thread_count=1,
            support_files=[],
        )
        search_engine = _require_mapping(certificate.get("engine"), "certificate engine")
    if prefix_certificate_value is not None:
        prefix_certificate = _require_mapping(
            prefix_certificate_value,
            "single prefix certificate",
        )
        _, prefix_memory, prefix_engine = validate_prefix_certificate(
            prefix_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant="single",
            thread_count=1,
            support_files=[],
        )
        if search_memory is not None and prefix_memory != search_memory:
            raise ValueError(
                "search and prefix certificates require identical memory envelopes"
            )
        if search_engine is not None:
            for key in ("engine_version", "ruleset_version"):
                if search_engine.get(key) != prefix_engine[key]:
                    raise ValueError(
                        f"search and prefix certificates disagree on {key}"
                    )
    root_memory: dict[str, int | bool] | None = None
    root_engine: dict[str, str] | None = None
    root_kernel: str | None = None
    root_exception: str | None = None
    if root_certificate_value is not None:
        root_certificate = _require_mapping(
            root_certificate_value,
            "single root-session certificate",
        )
        (
            root_memory,
            root_engine,
            root_kernel,
            root_exception,
            _root_contract,
            _root_geometry,
        ) = validate_root_session_certificate(
            root_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant="single",
            thread_count=1,
            support_files=[],
        )
    value_model_memory: dict[str, int | bool] | None = None
    value_model_engine: dict[str, str] | None = None
    value_model_kernel: str | None = None
    value_model_exception: str | None = None
    value_model_asset: dict[str, object] | None = None
    value_model_name: str | None = None
    if value_model_activation_value is not None:
        if root_certificate_value is None:
            raise ValueError(
                "browser value-model activation has no certified baseline fallback"
            )
        activation = _require_mapping(
            value_model_activation_value,
            "browser value-model activation",
        )
        if (
            set(activation)
            != {"schema", "status", "asset", "root_session_certificate"}
            or activation.get("schema") != VALUE_MODEL_ACTIVATION_SCHEMA
            or activation.get("status") != "certified"
        ):
            raise ValueError("browser value-model activation envelope is invalid")
        value_model_certificate = _require_mapping(
            activation.get("root_session_certificate"),
            "modeled root-session certificate",
        )
        (
            value_model_memory,
            value_model_engine,
            value_model_kernel,
            value_model_exception,
            value_model_contract,
            value_model_geometry,
        ) = validate_root_session_certificate(
            value_model_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant="single",
            thread_count=1,
            support_files=[],
        )
        configured_model = _require_mapping(
            value_model_geometry["session_config"].get(
                "deep_teacher_value_model"
            ),
            "modeled root-session config",
        )
        if configured_model.get("native_source_identity") != native_source_identity(
            source_package
        ):
            raise ValueError(
                "deep-teacher model native source identity differs from the source package"
            )
        value_model_asset = _validate_value_model_asset_descriptor(
            activation.get("asset"),
            configured_model,
        )
        certified_asset = _validate_value_model_asset_descriptor(
            value_model_certificate.get("value_model_asset"),
            configured_model,
        )
        if value_model_asset != certified_asset:
            raise ValueError(
                "value-model activation asset differs from its root certificate"
            )
        value_model_name = str(value_model_asset["file"])
        model_path = lane / value_model_name
        if not model_path.is_file():
            raise FileNotFoundError("certified browser value-model asset is missing")
        if _validate_original_deep_teacher_model(
            model_path,
            configured_model,
        ) != value_model_asset:
            raise ValueError("browser value-model asset does not match its descriptor")
        if (
            root_memory != value_model_memory
            or root_engine != value_model_engine
            or root_kernel != value_model_kernel
            or root_exception != value_model_exception
            or _root_contract != value_model_contract
        ):
            raise ValueError(
                "baseline and modeled root-session certificates differ outside config"
            )
        if any(
            root_certificate.get(key) != value_model_certificate.get(key)
            for key in ("wasm_simd", "allocator", "runtime_requirements")
        ):
            raise ValueError(
                "baseline and modeled root-session certificates have different runtimes"
            )
        baseline_geometry = dict(_root_geometry)
        modeled_geometry = dict(value_model_geometry)
        baseline_config = dict(
            _require_mapping(
                baseline_geometry.pop("session_config"),
                "baseline root-session config",
            )
        )
        modeled_config = dict(
            _require_mapping(
                modeled_geometry.pop("session_config"),
                "modeled root-session config",
            )
        )
        modeled_config.pop("deep_teacher_value_model", None)
        if baseline_geometry != modeled_geometry or baseline_config != modeled_config:
            raise ValueError(
                "modeled root-session certificate changes non-model play geometry"
            )
    mate_memory: dict[str, int | bool] | None = None
    mate_engine: dict[str, str] | None = None
    mate_kernel: str | None = None
    mate_exception: str | None = None
    if mate_certificate_value is not None:
        mate_certificate = _require_mapping(
            mate_certificate_value,
            "single mate certificate",
        )
        (
            mate_memory,
            mate_engine,
            mate_kernel,
            mate_exception,
        ) = validate_mate_certificate(
            mate_certificate,
            source_fingerprint=source_fingerprint,
            wasm_sha256=wasm_sha256,
            module_js_sha256=module_js_sha256,
            runtime_variant="single",
            thread_count=1,
            support_files=[],
        )
    memories = [
        value
        for value in (
            search_memory,
            prefix_memory if prefix_certificate_value else None,
            root_memory,
            value_model_memory,
            mate_memory,
        )
        if value is not None
    ]
    if any(value != memories[0] for value in memories[1:]):
        raise ValueError("combined capability certificates have different memory envelopes")
    kernels = [
        value
        for value in (root_kernel, value_model_kernel, mate_kernel)
        if value is not None
    ]
    if kernels and (
        any(value != kernels[0] for value in kernels[1:])
        or variant.get("kernel_sha256") != kernels[0]
    ):
        raise ValueError("combined root/mate kernel identity mismatch")
    exceptions = [
        value
        for value in (root_exception, value_model_exception, mate_exception)
        if value is not None
    ]
    if any(value != exceptions[0] for value in exceptions[1:]):
        raise ValueError("combined root/mate exception strategy mismatch")
    if (
        root_certificate_value is not None
        and mate_certificate_value is not None
        and (
            root_certificate.get("wasm_simd") != mate_certificate.get("wasm_simd")
            or root_certificate.get("runtime_requirements")
            != mate_certificate.get("runtime_requirements")
            or root_certificate.get("allocator") != mate_certificate.get("allocator")
        )
    ):
        raise ValueError("combined root/mate runtime requirements mismatch")
    if root_engine is not None and mate_engine is not None and root_engine != mate_engine:
        raise ValueError("combined root/mate engine identity mismatch")
    if search_engine is not None and root_engine is not None:
        if (
            search_engine.get("engine_version") != root_engine["engine_version"]
            or search_engine.get("ruleset_version") != root_engine["ruleset_version"]
            or search_engine.get("engine_profile_id") != root_engine["profile_id"]
        ):
            raise ValueError("combined search/root engine identity mismatch")
    expected_files = {
        "browser-engine-manifest.json",
        f"single/{wasm_name}",
        f"single/{module_name}",
    }
    if value_model_name is not None:
        expected_files.add(f"single/{value_model_name}")
    actual_files = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file()
    }
    if actual_files != expected_files:
        raise ValueError(
            "browser engine bundle contains missing or uncertified files: "
            f"expected {sorted(expected_files)!r}, found {sorted(actual_files)!r}"
        )
    return manifest


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Build a fail-closed, identity-bound browser WASM bundle."
    )
    parser.add_argument("--single-wasm", type=Path)
    parser.add_argument("--single-module-js", type=Path)
    parser.add_argument("--single-certificate", type=Path)
    parser.add_argument("--single-prefix-certificate", type=Path)
    parser.add_argument("--single-root-session-certificate", type=Path)
    parser.add_argument("--single-value-model-root-session-certificate", type=Path)
    parser.add_argument("--single-value-model", type=Path)
    parser.add_argument("--single-mate-certificate", type=Path)
    parser.add_argument("--single-support-file", type=Path, action="append", default=[])
    parser.add_argument("--pthread-wasm", type=Path)
    parser.add_argument("--pthread-module-js", type=Path)
    parser.add_argument("--pthread-certificate", type=Path)
    parser.add_argument("--pthread-support-file", type=Path, action="append", default=[])
    parser.add_argument(
        "--source-package",
        type=Path,
        default=root / "src" / "scottish_progressive",
    )
    parser.add_argument("--validate-existing", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.validate_existing is not None:
        if any(
            value is not None
            for value in (
                arguments.single_wasm,
                arguments.single_module_js,
                arguments.single_certificate,
                arguments.single_prefix_certificate,
                arguments.single_root_session_certificate,
                arguments.single_value_model_root_session_certificate,
                arguments.single_value_model,
                arguments.single_mate_certificate,
                arguments.pthread_wasm,
                arguments.pthread_module_js,
                arguments.pthread_certificate,
            )
        ) or arguments.single_support_file or arguments.pthread_support_file:
            parser.error("--validate-existing cannot be combined with build inputs")
        manifest = validate_existing_bundle(
            arguments.validate_existing.resolve(),
            arguments.source_package.resolve(),
        )
        print(json.dumps(manifest, sort_keys=True))
        return
    missing = [
        name
        for name, value in (
            ("--single-wasm", arguments.single_wasm),
            ("--single-module-js", arguments.single_module_js),
            ("--output", arguments.output),
        )
        if value is None
    ]
    if missing:
        parser.error(f"building a bundle requires {', '.join(missing)}")
    if (
        arguments.single_certificate is None
        and arguments.single_prefix_certificate is None
        and arguments.single_root_session_certificate is None
        and arguments.single_mate_certificate is None
    ):
        parser.error(
            "building a bundle requires --single-certificate, "
            "--single-prefix-certificate, --single-root-session-certificate, "
            "or --single-mate-certificate"
        )
    assert arguments.single_wasm is not None
    assert arguments.single_module_js is not None
    assert arguments.output is not None
    manifest = build_bundle(
        single_wasm=arguments.single_wasm.resolve(),
        single_module_js=arguments.single_module_js.resolve(),
        single_certificate_path=(
            arguments.single_certificate.resolve()
            if arguments.single_certificate
            else None
        ),
        single_prefix_certificate_path=(
            arguments.single_prefix_certificate.resolve()
            if arguments.single_prefix_certificate
            else None
        ),
        single_root_session_certificate_path=(
            arguments.single_root_session_certificate.resolve()
            if arguments.single_root_session_certificate
            else None
        ),
        single_value_model_root_session_certificate_path=(
            arguments.single_value_model_root_session_certificate.resolve()
            if arguments.single_value_model_root_session_certificate
            else None
        ),
        single_value_model_path=(
            arguments.single_value_model.resolve()
            if arguments.single_value_model
            else None
        ),
        single_mate_certificate_path=(
            arguments.single_mate_certificate.resolve()
            if arguments.single_mate_certificate
            else None
        ),
        single_support_paths=tuple(path.resolve() for path in arguments.single_support_file),
        pthread_wasm=(
            arguments.pthread_wasm.resolve() if arguments.pthread_wasm else None
        ),
        pthread_module_js=(
            arguments.pthread_module_js.resolve()
            if arguments.pthread_module_js
            else None
        ),
        pthread_certificate_path=(
            arguments.pthread_certificate.resolve()
            if arguments.pthread_certificate
            else None
        ),
        pthread_support_paths=tuple(
            path.resolve() for path in arguments.pthread_support_file
        ),
        source_package=arguments.source_package.resolve(),
        output=arguments.output.resolve(),
    )
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
