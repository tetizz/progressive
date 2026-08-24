from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

import chess

from . import evaluation
from .model import ProgressiveState
from .profiles import EngineProfile
from .search import EvaluationOverlayScore
from .teacher_value_features import (
    TEACHER_VALUE_FEATURE_NAMES,
    TEACHER_VALUE_FEATURE_SCHEMA,
)


DEEP_TEACHER_MODEL_SCHEMA = "spc-deep-teacher-linear-value-v1"
DEEP_TEACHER_OVERLAY_SCHEMA = "spc-deep-teacher-match-overlay-v1"
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
_MODEL_KEYS = frozenset(
    {
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
)
_FEATURE_GROUP_COUNTS = {
    "base7": 7,
    "phase14": 14,
    "cached19": 19,
    "positional38": 38,
    "direct44": 44,
    "all47": 47,
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"deep-teacher model repeats JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"deep-teacher model contains nonfinite JSON value {value}")


def _native_teacher_module(
    expected_identity: str | None = None,
    *,
    verify_packaged_sources: bool = True,
) -> Any:
    native = evaluation._native_eval
    source_identity = (
        evaluation._native_source_identity()
        if verify_packaged_sources
        else getattr(native, "SOURCE_IDENTITY", None)
    )
    if (
        native is None
        or source_identity is None
        or getattr(native, "SOURCE_IDENTITY", None) != source_identity
        or expected_identity is not None
        and source_identity != expected_identity
        or not hasattr(native, "teacher_value_features_v3_with_receipt")
        or not hasattr(native, "deep_teacher_score_v1")
    ):
        raise RuntimeError(
            "the source-matched native deep-teacher evaluator is unavailable"
        )
    return native


def _model_core(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in sorted(
            _MODEL_KEYS - {"model_id", "teacher_corpus_raw_artifact_sha256"}
        )
    }


def _validate_model(
    payload: Mapping[str, Any],
) -> tuple[int, tuple[str, ...], tuple[int, ...]]:
    keys = frozenset(payload)
    if keys != _MODEL_KEYS:
        missing = sorted(_MODEL_KEYS - keys)
        extra = sorted(keys - _MODEL_KEYS)
        raise ValueError(
            f"deep-teacher model keys differ; missing={missing}, extra={extra}"
        )
    if payload["schema"] != DEEP_TEACHER_MODEL_SCHEMA:
        raise ValueError("unsupported deep-teacher model schema")
    if payload["feature_schema"] != TEACHER_VALUE_FEATURE_SCHEMA:
        raise ValueError("deep-teacher feature schema mismatch")
    group = payload["feature_group"]
    if type(group) is not str or group not in _FEATURE_GROUP_COUNTS:
        raise ValueError("deep-teacher feature group is unsupported")
    feature_count = _FEATURE_GROUP_COUNTS[group]
    names_raw = payload["feature_names"]
    if not isinstance(names_raw, list) or any(
        type(name) is not str for name in names_raw
    ):
        raise TypeError("deep-teacher feature_names must be an exact string list")
    feature_names = tuple(names_raw)
    expected_names = TEACHER_VALUE_FEATURE_NAMES[:feature_count]
    if feature_names != expected_names:
        raise ValueError("deep-teacher feature order mismatch")
    scale = payload["fixed_point_scale"]
    if type(scale) is not int or scale != DEEP_TEACHER_FIXED_POINT_SCALE:
        raise ValueError("deep-teacher fixed-point scale mismatch")
    coefficients_raw = payload["coefficients"]
    if not isinstance(coefficients_raw, list) or any(
        type(value) is not int for value in coefficients_raw
    ):
        raise TypeError("deep-teacher coefficients must be an exact integer list")
    coefficients = tuple(coefficients_raw)
    if len(coefficients) != feature_count:
        raise ValueError("deep-teacher coefficient count mismatch")
    if any(abs(value) > scale for value in coefficients):
        raise ValueError("deep-teacher coefficient exceeds the frozen scale")
    if not coefficients or max(abs(value) for value in coefficients) != scale:
        raise ValueError("deep-teacher coefficients are not canonically normalized")
    ridge = payload["ridge"]
    if type(ridge) is not float or not math.isfinite(ridge) or ridge <= 0.0:
        raise ValueError("deep-teacher ridge must be a finite positive float")
    adverse_pair_weight = payload["adverse_pair_weight"]
    if (
        type(adverse_pair_weight) is not float
        or not math.isfinite(adverse_pair_weight)
        or not 1.0 <= adverse_pair_weight <= 1_000.0
    ):
        raise ValueError(
            "deep-teacher adverse_pair_weight must be a finite float from 1 to 1000"
        )
    if payload["terminal_override"] != DEEP_TEACHER_TERMINAL_POLICY:
        raise ValueError("deep-teacher terminal policy mismatch")
    corpus_id = payload["teacher_corpus_id"]
    if type(corpus_id) is not str or not corpus_id.strip() or len(corpus_id) > 256:
        raise ValueError("deep-teacher corpus identity is invalid")
    corpus_sha256 = payload["teacher_corpus_sha256"]
    if type(corpus_sha256) is not str or _SHA256.fullmatch(corpus_sha256) is None:
        raise ValueError("deep-teacher corpus SHA-256 is invalid")
    semantic_sha256 = payload["teacher_corpus_semantic_sha256"]
    if type(semantic_sha256) is not str or _SHA256.fullmatch(semantic_sha256) is None:
        raise ValueError("deep-teacher semantic corpus SHA-256 is invalid")
    if semantic_sha256 != corpus_sha256:
        raise ValueError("deep-teacher legacy and semantic corpus identities differ")
    raw_artifact_sha256 = payload["teacher_corpus_raw_artifact_sha256"]
    if (
        type(raw_artifact_sha256) is not str
        or _SHA256.fullmatch(raw_artifact_sha256) is None
    ):
        raise ValueError("deep-teacher raw corpus artifact SHA-256 is invalid")
    supplied_model_id = payload["model_id"]
    expected_model_id = "spc-dtv-" + hashlib.sha256(
        _canonical_json(_model_core(payload))
    ).hexdigest()[:20]
    if type(supplied_model_id) is not str or supplied_model_id != expected_model_id:
        raise ValueError("deep-teacher model_id does not match its payload")
    return feature_count, feature_names, coefficients


@dataclass(frozen=True, slots=True)
class DeepTeacherOverlayPayload:
    """Immutable, process-pool-safe candidate evaluator request."""

    base_profile_id: str
    variant_id: str
    model_id: str
    model_sha256: str
    native_source_identity: str
    feature_group: str
    feature_names: tuple[str, ...]
    coefficients: tuple[int, ...]
    fixed_point_scale: int
    ridge: float
    adverse_pair_weight: float
    terminal_override: str
    teacher_corpus_id: str
    teacher_corpus_sha256: str
    teacher_corpus_semantic_sha256: str
    teacher_corpus_raw_artifact_sha256: str
    score_policy: str = DEEP_TEACHER_SCORE_POLICY
    work_policy: str = DEEP_TEACHER_WORK_POLICY
    schema: str = DEEP_TEACHER_OVERLAY_SCHEMA

    @property
    def feature_count(self) -> int:
        return len(self.coefficients)

    def _model_payload(self) -> dict[str, Any]:
        return {
            "schema": DEEP_TEACHER_MODEL_SCHEMA,
            "feature_schema": TEACHER_VALUE_FEATURE_SCHEMA,
            "feature_group": self.feature_group,
            "feature_names": list(self.feature_names),
            "fixed_point_scale": self.fixed_point_scale,
            "coefficients": list(self.coefficients),
            "ridge": self.ridge,
            "adverse_pair_weight": self.adverse_pair_weight,
            "terminal_override": self.terminal_override,
            "teacher_corpus_id": self.teacher_corpus_id,
            "teacher_corpus_sha256": self.teacher_corpus_sha256,
            "teacher_corpus_semantic_sha256": (
                self.teacher_corpus_semantic_sha256
            ),
            "teacher_corpus_raw_artifact_sha256": (
                self.teacher_corpus_raw_artifact_sha256
            ),
            "model_id": self.model_id,
        }

    def validate(self, profile: EngineProfile | None = None) -> None:
        if self.schema != DEEP_TEACHER_OVERLAY_SCHEMA:
            raise ValueError("deep-teacher overlay schema mismatch")
        if type(self.base_profile_id) is not str or not self.base_profile_id:
            raise ValueError("deep-teacher overlay base profile is invalid")
        if profile is not None and self.base_profile_id != profile.profile_id:
            raise ValueError("deep-teacher overlay is bound to a different profile")
        if (
            type(self.model_sha256) is not str
            or _SHA256.fullmatch(self.model_sha256) is None
        ):
            raise ValueError("deep-teacher model file SHA-256 is invalid")
        if (
            type(self.native_source_identity) is not str
            or _SHA256.fullmatch(self.native_source_identity) is None
        ):
            raise ValueError("deep-teacher native source identity is invalid")
        if self.score_policy != DEEP_TEACHER_SCORE_POLICY:
            raise ValueError("deep-teacher score policy mismatch")
        if self.work_policy != DEEP_TEACHER_WORK_POLICY:
            raise ValueError("deep-teacher work policy mismatch")
        _validate_model(self._model_payload())
        if self.variant_id != _variant_id(self):
            raise ValueError("deep-teacher variant_id does not match its identities")
        _native_teacher_module(self.native_source_identity)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "base_profile_id": self.base_profile_id,
            "variant_id": self.variant_id,
            "model_id": self.model_id,
            "model_sha256": self.model_sha256,
            "native_source_identity": self.native_source_identity,
            "feature_group": self.feature_group,
            "feature_count": self.feature_count,
            "feature_names": list(self.feature_names),
            "fixed_point_scale": self.fixed_point_scale,
            "coefficients": list(self.coefficients),
            "ridge": self.ridge,
            "adverse_pair_weight": self.adverse_pair_weight,
            "terminal_override": self.terminal_override,
            "teacher_corpus_id": self.teacher_corpus_id,
            "teacher_corpus_sha256": self.teacher_corpus_sha256,
            "teacher_corpus_semantic_sha256": (
                self.teacher_corpus_semantic_sha256
            ),
            "teacher_corpus_raw_artifact_sha256": (
                self.teacher_corpus_raw_artifact_sha256
            ),
            "score_policy": self.score_policy,
            "work_policy": self.work_policy,
        }


def _variant_id(payload: DeepTeacherOverlayPayload) -> str:
    identity = {
        "schema": payload.schema,
        "base_profile_id": payload.base_profile_id,
        "model_id": payload.model_id,
        "model_sha256": payload.model_sha256,
        "teacher_corpus_semantic_sha256": (
            payload.teacher_corpus_semantic_sha256
        ),
        "teacher_corpus_raw_artifact_sha256": (
            payload.teacher_corpus_raw_artifact_sha256
        ),
        "native_source_identity": payload.native_source_identity,
        "score_policy": payload.score_policy,
        "work_policy": payload.work_policy,
    }
    return "spc-dtv-variant-" + hashlib.sha256(
        _canonical_json(identity)
    ).hexdigest()[:20]


def load_deep_teacher_overlay_payload(
    path: str | Path,
    base_profile: EngineProfile,
) -> DeepTeacherOverlayPayload:
    source = Path(path).expanduser().resolve()
    try:
        raw = source.read_bytes()
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"could not load deep-teacher model {source}: {error}"
        ) from error
    if not isinstance(parsed, Mapping):
        raise ValueError("deep-teacher model root must be an object")
    feature_count, feature_names, coefficients = _validate_model(parsed)
    del feature_count
    native = _native_teacher_module()
    model_sha256 = hashlib.sha256(raw).hexdigest()
    provisional = DeepTeacherOverlayPayload(
        base_profile_id=base_profile.profile_id,
        variant_id="pending",
        model_id=parsed["model_id"],
        model_sha256=model_sha256,
        native_source_identity=native.SOURCE_IDENTITY,
        feature_group=parsed["feature_group"],
        feature_names=feature_names,
        coefficients=coefficients,
        fixed_point_scale=parsed["fixed_point_scale"],
        ridge=parsed["ridge"],
        adverse_pair_weight=parsed["adverse_pair_weight"],
        terminal_override=parsed["terminal_override"],
        teacher_corpus_id=parsed["teacher_corpus_id"],
        teacher_corpus_sha256=parsed["teacher_corpus_sha256"],
        teacher_corpus_semantic_sha256=parsed[
            "teacher_corpus_semantic_sha256"
        ],
        teacher_corpus_raw_artifact_sha256=parsed[
            "teacher_corpus_raw_artifact_sha256"
        ],
    )
    payload = replace(provisional, variant_id=_variant_id(provisional))
    payload.validate(base_profile)
    return payload


def _rounded_fixed_point(raw_score: int, scale: int) -> int:
    magnitude = (abs(raw_score) + scale // 2) // scale
    return magnitude if raw_score >= 0 else -magnitude


@dataclass(frozen=True, slots=True)
class DeepTeacherEvaluationOverlay:
    payload: DeepTeacherOverlayPayload

    requires_exact_work_receipt: bool = True

    def __post_init__(self) -> None:
        if type(self.payload) is not DeepTeacherOverlayPayload:
            raise TypeError("deep-teacher overlay payload has the wrong type")
        self.payload.validate()

    @property
    def base_profile_id(self) -> str:
        return self.payload.base_profile_id

    @property
    def variant_id(self) -> str:
        return self.payload.variant_id

    @property
    def name(self) -> str:
        return f"Deep teacher candidate {self.payload.model_id}"

    def score(self, state: ProgressiveState, hand_score: int) -> int:
        return self.score_with_work(state, hand_score, None).score

    def score_with_work(
        self,
        state: ProgressiveState,
        hand_score: int,
        max_work_positions: int | None,
    ) -> EvaluationOverlayScore:
        if type(hand_score) is not int:
            raise TypeError("hand_score must be an exact integer")
        if max_work_positions is not None and (
            type(max_work_positions) is not int or max_work_positions < 0
        ):
            raise TypeError("max_work_positions must be a nonnegative integer or None")
        if state.board.chess960:
            raise ValueError("deep-teacher overlay does not support Chess960")
        # Worker reconstruction already validates the immutable payload against
        # the packaged sources. The leaf hot path only needs to bind the loaded
        # in-memory extension to that frozen identity; re-hashing source files
        # at every evaluation would dominate the evaluator itself.
        native = _native_teacher_module(
            self.payload.native_source_identity,
            verify_packaged_sources=False,
        )
        board = state.board
        reach_limit = min(
            256,
            256 if max_work_positions is None else max_work_positions,
        )
        features_raw, receipt_raw = native.teacher_value_features_v3_with_receipt(
            board.pawns,
            board.knights,
            board.bishops,
            board.rooks,
            board.queens,
            board.kings,
            board.occupied_co[chess.WHITE],
            board.occupied_co[chess.BLACK],
            board.promoted,
            board.clean_castling_rights(),
            board.turn,
            state.series_number,
            state.ep_targets,
            reach_limit,
            self.payload.feature_count,
        )
        features = tuple(features_raw)
        if len(features) != self.payload.feature_count or any(
            type(value) is not int for value in features
        ):
            raise RuntimeError("native deep-teacher feature result has the wrong shape")
        if not isinstance(receipt_raw, dict):
            raise RuntimeError("native deep-teacher work receipt is not an object")
        count_names = (
            "white_reach_positions",
            "black_reach_positions",
            "direct_move_variants",
            "two_move_variants",
        )
        if any(
            type(receipt_raw.get(name)) is not int or receipt_raw[name] < 0
            for name in count_names
        ):
            raise RuntimeError("native deep-teacher work receipt has invalid counts")
        if any(
            type(receipt_raw.get(name)) is not bool
            for name in ("white_reach_complete", "black_reach_complete")
        ):
            raise RuntimeError("native deep-teacher work receipt has invalid status")
        reach_positions = (
            receipt_raw["white_reach_positions"]
            + receipt_raw["black_reach_positions"]
        )
        direct_move_variants = receipt_raw["direct_move_variants"]
        two_move_variants = receipt_raw["two_move_variants"]
        work_positions = (
            reach_positions + direct_move_variants + two_move_variants
        )
        reach_budget_complete = (
            reach_limit == 256
            or receipt_raw["white_reach_complete"]
            and receipt_raw["black_reach_complete"]
            or reach_positions < reach_limit
        )
        complete = reach_budget_complete and (
            max_work_positions is None or work_positions <= max_work_positions
        )
        # The scorer's frozen CPython boundary accepts the complete 47-slot
        # feature vector while the extractor deliberately stops at the active
        # prefix. Unused suffix values cannot contribute, so pad them with
        # exact zeros instead of computing the skipped feature groups.
        scorer_features = features + (0,) * (
            len(TEACHER_VALUE_FEATURE_NAMES) - len(features)
        )
        raw_score = native.deep_teacher_score_v1(
            scorer_features,
            self.payload.coefficients,
            self.payload.fixed_point_scale,
        )
        if type(raw_score) is not int:
            raise RuntimeError("native deep-teacher score is not an exact integer")
        return EvaluationOverlayScore(
            score=_rounded_fixed_point(raw_score, self.payload.fixed_point_scale),
            reach_positions=reach_positions,
            direct_move_variants=direct_move_variants,
            two_move_variants=two_move_variants,
            complete=complete,
        )


def build_deep_teacher_overlay(
    payload: DeepTeacherOverlayPayload,
    profile: EngineProfile,
) -> DeepTeacherEvaluationOverlay:
    if type(payload) is not DeepTeacherOverlayPayload:
        raise TypeError("deep-teacher overlay payload has the wrong type")
    if payload.base_profile_id != profile.profile_id:
        raise ValueError("deep-teacher overlay is bound to a different profile")
    return DeepTeacherEvaluationOverlay(payload)


def reconstruct_deep_teacher_variant_id(
    payload: DeepTeacherOverlayPayload,
    profile: EngineProfile,
) -> str:
    """Small process-pool probe used by transport and regression tests."""

    return build_deep_teacher_overlay(payload, profile).variant_id
