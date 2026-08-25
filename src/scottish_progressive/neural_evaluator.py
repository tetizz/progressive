from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import random
import tempfile
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

import chess

from .model import ENGINE_SOURCE_FINGERPRINT, ProgressiveState
from .profiles import EngineProfile

if TYPE_CHECKING:
    from .search import SearchResult
    from .selfplay_training import SelfPlayCorpus


NEURAL_ARTIFACT_FORMAT = "spc-fixed-point-nnue-v3"
NEURAL_ARTIFACT_SCHEMA = 3
NEURAL_TRAINER_METHOD = "sparse-fixed-point-distillation-v3"
NEURAL_INFERENCE_SCOPE = "complete-series-boundaries-only-v1"
NEURAL_SCORE_CLIP = 500_000
PROOF_TARGET_SCORE = 100_000
WEAK_WDL_SCORE_SPAN = 2_000

# Every feature is binary. Buckets keep the input sparse and make inference a
# sequence of integer row additions, which maps directly to a future C++ NNUE
# accumulator without changing the artifact contract.
PIECE_SQUARE_OFFSET = 0
PIECE_SQUARE_COUNT = 2 * 6 * 64
PROMOTED_OFFSET = PIECE_SQUARE_OFFSET + PIECE_SQUARE_COUNT
PROMOTED_COUNT = 2 * 64
MOVER_OFFSET = PROMOTED_OFFSET + PROMOTED_COUNT
MOVER_COUNT = 2
SERIES_OFFSET = MOVER_OFFSET + MOVER_COUNT
SERIES_BUCKET_COUNT = 17  # Series 1..16 and one 17+ bucket.
MOVES_REMAINING_OFFSET = SERIES_OFFSET + SERIES_BUCKET_COUNT
MOVES_REMAINING_BUCKET_COUNT = 18  # 0..16 and one 17+ bucket.
QUIET_OFFSET = MOVES_REMAINING_OFFSET + MOVES_REMAINING_BUCKET_COUNT
QUIET_BUCKET_COUNT = 12  # 0..10 and one 11+ bucket.
CHECK_OFFSET = QUIET_OFFSET + QUIET_BUCKET_COUNT
CHECK_COUNT = 1
CASTLING_OFFSET = CHECK_OFFSET + CHECK_COUNT
CASTLING_COUNT = 4  # White K/Q, Black K/Q.
PROGRESSIVE_EP_OFFSET = CASTLING_OFFSET + CASTLING_COUNT
PROGRESSIVE_EP_COUNT = 64
FEATURE_COUNT = PROGRESSIVE_EP_OFFSET + PROGRESSIVE_EP_COUNT


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_value(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


FEATURE_SCHEMA = {
    "version": 1,
    "perspective": "white-centric",
    "encoding": "sorted-active-binary-indexes",
    "feature_count": FEATURE_COUNT,
    "planes": {
        "piece_square": {
            "offset": PIECE_SQUARE_OFFSET,
            "count": PIECE_SQUARE_COUNT,
            "order": "white-then-black; pawn,knight,bishop,rook,queen,king; a1..h8",
        },
        "promoted_piece_color_square": {
            "offset": PROMOTED_OFFSET,
            "count": PROMOTED_COUNT,
            "order": "white-then-black; a1..h8",
        },
        "mover": {
            "offset": MOVER_OFFSET,
            "count": MOVER_COUNT,
            "order": "white,black",
        },
        "series_number": {
            "offset": SERIES_OFFSET,
            "count": SERIES_BUCKET_COUNT,
            "buckets": "1..16,17+",
        },
        "moves_remaining": {
            "offset": MOVES_REMAINING_OFFSET,
            "count": MOVES_REMAINING_BUCKET_COUNT,
            "buckets": "0..16,17+",
        },
        "quiet_series": {
            "offset": QUIET_OFFSET,
            "count": QUIET_BUCKET_COUNT,
            "buckets": "0..10,11+",
        },
        "mover_in_check": {"offset": CHECK_OFFSET, "count": CHECK_COUNT},
        "castling_rights": {
            "offset": CASTLING_OFFSET,
            "count": CASTLING_COUNT,
            "order": "white-kingside,white-queenside,black-kingside,black-queenside",
        },
        "progressive_en_passant_targets": {
            "offset": PROGRESSIVE_EP_OFFSET,
            "count": PROGRESSIVE_EP_COUNT,
            "order": "a1..h8",
        },
    },
}
FEATURE_FINGERPRINT = hashlib.sha256(_canonical_json(FEATURE_SCHEMA)).hexdigest()


def _color_index(color: chess.Color) -> int:
    return 0 if color == chess.WHITE else 1


def piece_square_feature(
    color: chess.Color,
    piece_type: chess.PieceType,
    square: chess.Square,
) -> int:
    if piece_type not in chess.PIECE_TYPES:
        raise ValueError("piece_type is invalid")
    if not 0 <= square < 64:
        raise ValueError("square is invalid")
    return (
        PIECE_SQUARE_OFFSET
        + ((_color_index(color) * 6 + piece_type - 1) * 64)
        + square
    )


def promoted_feature(color: chess.Color, square: chess.Square) -> int:
    if not 0 <= square < 64:
        raise ValueError("square is invalid")
    return PROMOTED_OFFSET + _color_index(color) * 64 + square


def extract_active_features(
    state: ProgressiveState,
    *,
    moves_remaining: int | None = None,
) -> tuple[int, ...]:
    """Returns the canonical sparse NNUE feature indexes for one state.

    Search currently evaluates complete-series boundaries, where
    ``moves_remaining`` equals the series number. Keeping it explicit in the
    schema lets a later incremental in-series evaluator reuse the same model.
    """

    if type(state) is not ProgressiveState:
        raise ValueError("neural features require an exact ProgressiveState")
    remaining = state.moves_available if moves_remaining is None else moves_remaining
    if type(remaining) is not int or not 0 <= remaining <= state.moves_available:
        raise ValueError("moves_remaining must be between zero and the series budget")

    board = state.board
    active: list[int] = []
    for square, piece in sorted(board.piece_map().items()):
        active.append(piece_square_feature(piece.color, piece.piece_type, square))
        if board.promoted & chess.BB_SQUARES[square]:
            active.append(promoted_feature(piece.color, square))

    active.append(MOVER_OFFSET + _color_index(board.turn))
    active.append(SERIES_OFFSET + min(state.series_number, 17) - 1)
    active.append(MOVES_REMAINING_OFFSET + min(remaining, 17))
    active.append(QUIET_OFFSET + min(state.quiet_series, 11))
    if board.is_check():
        active.append(CHECK_OFFSET)

    castling = (
        board.has_kingside_castling_rights(chess.WHITE),
        board.has_queenside_castling_rights(chess.WHITE),
        board.has_kingside_castling_rights(chess.BLACK),
        board.has_queenside_castling_rights(chess.BLACK),
    )
    active.extend(
        CASTLING_OFFSET + index
        for index, enabled in enumerate(castling)
        if enabled
    )
    active.extend(PROGRESSIVE_EP_OFFSET + square for square in state.ep_targets)

    ordered = tuple(sorted(active))
    if len(ordered) != len(set(ordered)) or any(
        not 0 <= index < FEATURE_COUNT for index in ordered
    ):
        raise AssertionError("neural feature extraction produced an invalid index")
    return ordered


def _divide_nearest(numerator: int, denominator: int) -> int:
    """Integer division rounded to nearest, with exact ties away from zero."""

    if type(numerator) is not int or type(denominator) is not int or denominator < 1:
        raise ValueError("fixed-point division requires integers and a positive divisor")
    sign = -1 if numerator < 0 else 1
    return sign * ((abs(numerator) + denominator // 2) // denominator)


def blend_scores(hand_score: int, neural_score: int, blend_percent: int) -> int:
    if any(type(value) is not int for value in (hand_score, neural_score, blend_percent)):
        raise ValueError("score blending requires exact integers")
    if not 0 <= blend_percent <= 100:
        raise ValueError("blend_percent must be from zero through 100")
    return _divide_nearest(
        hand_score * (100 - blend_percent) + neural_score * blend_percent,
        100,
    )


@dataclass(frozen=True, slots=True)
class FixedPointNetwork:
    """Portable one-hidden-layer sparse integer evaluator artifact."""

    source_fingerprint: str
    base_profile_id: str
    teacher_fingerprint: str
    corpus_fingerprint: str
    trainer_fingerprint: str
    hidden_size: int
    input_weights: tuple[int, ...]
    hidden_bias: tuple[int, ...]
    output_weights: tuple[int, ...]
    output_bias: int
    output_denominator: int
    recommended_blend_percent: int
    inference_scope: str = NEURAL_INFERENCE_SCOPE
    feature_fingerprint: str = FEATURE_FINGERPRINT
    feature_count: int = FEATURE_COUNT
    activation_clip: int = 32_767
    score_clip: int = NEURAL_SCORE_CLIP
    schema_version: int = NEURAL_ARTIFACT_SCHEMA
    artifact_format: str = NEURAL_ARTIFACT_FORMAT

    def __post_init__(self) -> None:
        if self.artifact_format != NEURAL_ARTIFACT_FORMAT:
            raise ValueError("unsupported neural artifact format")
        if self.schema_version != NEURAL_ARTIFACT_SCHEMA:
            raise ValueError("unsupported neural artifact schema")
        if self.feature_fingerprint != FEATURE_FINGERPRINT:
            raise ValueError("neural artifact feature fingerprint is stale")
        if self.feature_count != FEATURE_COUNT:
            raise ValueError("neural artifact feature count is stale")
        if self.inference_scope != NEURAL_INFERENCE_SCOPE:
            raise ValueError(
                "neural artifact is not certified for complete-series boundary inference"
            )
        if not 1 <= self.hidden_size <= 128:
            raise ValueError("hidden_size must be from 1 through 128")
        if len(self.input_weights) != self.feature_count * self.hidden_size:
            raise ValueError("input weight matrix shape does not match the feature schema")
        if len(self.hidden_bias) != self.hidden_size:
            raise ValueError("hidden bias shape does not match hidden_size")
        if len(self.output_weights) != self.hidden_size:
            raise ValueError("output weight shape does not match hidden_size")
        if any(type(value) is not int or not -(1 << 15) <= value < (1 << 15) for value in self.input_weights):
            raise ValueError("input weights must fit signed int16")
        if any(type(value) is not int or not -(1 << 31) <= value < (1 << 31) for value in self.hidden_bias):
            raise ValueError("hidden biases must fit signed int32")
        if any(type(value) is not int or not -(1 << 31) <= value < (1 << 31) for value in self.output_weights):
            raise ValueError("output weights must fit signed int32")
        if type(self.output_bias) is not int or not -(1 << 63) <= self.output_bias < (1 << 63):
            raise ValueError("output bias must fit signed int64")
        if type(self.output_denominator) is not int or self.output_denominator < 1:
            raise ValueError("output_denominator must be positive")
        if type(self.activation_clip) is not int or not 1 <= self.activation_clip <= 32_767:
            raise ValueError("activation_clip must fit positive int16")
        if type(self.score_clip) is not int or not 1 <= self.score_clip < 900_000:
            raise ValueError("neural score clip must stay below the mate-score range")
        if not 0 <= self.recommended_blend_percent <= 100:
            raise ValueError("recommended_blend_percent must be from zero through 100")
        for name in (
            "source_fingerprint",
            "base_profile_id",
            "teacher_fingerprint",
            "corpus_fingerprint",
            "trainer_fingerprint",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"{name} cannot be empty")

    def deterministic_payload(self) -> dict[str, Any]:
        return {
            "artifact_format": self.artifact_format,
            "schema_version": self.schema_version,
            "source_fingerprint": self.source_fingerprint,
            "base_profile_id": self.base_profile_id,
            "feature_fingerprint": self.feature_fingerprint,
            "feature_count": self.feature_count,
            "inference_scope": self.inference_scope,
            "teacher_fingerprint": self.teacher_fingerprint,
            "corpus_fingerprint": self.corpus_fingerprint,
            "trainer_fingerprint": self.trainer_fingerprint,
            "hidden_size": self.hidden_size,
            "input_weights": list(self.input_weights),
            "hidden_bias": list(self.hidden_bias),
            "output_weights": list(self.output_weights),
            "output_bias": self.output_bias,
            "output_denominator": self.output_denominator,
            "activation_clip": self.activation_clip,
            "score_clip": self.score_clip,
            "recommended_blend_percent": self.recommended_blend_percent,
        }

    @property
    def artifact_id(self) -> str:
        return "spc-nnue-" + hashlib.sha256(
            _canonical_json(self.deterministic_payload())
        ).hexdigest()[:24]

    def as_dict(self) -> dict[str, Any]:
        return {**self.deterministic_payload(), "artifact_id": self.artifact_id}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FixedPointNetwork:
        if not isinstance(payload, Mapping):
            raise ValueError("neural artifact root must be an object")
        try:
            network = cls(
                artifact_format=str(payload["artifact_format"]),
                schema_version=int(payload["schema_version"]),
                source_fingerprint=str(payload["source_fingerprint"]),
                base_profile_id=str(payload["base_profile_id"]),
                feature_fingerprint=str(payload["feature_fingerprint"]),
                feature_count=int(payload["feature_count"]),
                inference_scope=str(payload["inference_scope"]),
                teacher_fingerprint=str(payload["teacher_fingerprint"]),
                corpus_fingerprint=str(payload["corpus_fingerprint"]),
                trainer_fingerprint=str(payload["trainer_fingerprint"]),
                hidden_size=int(payload["hidden_size"]),
                input_weights=tuple(int(value) for value in payload["input_weights"]),
                hidden_bias=tuple(int(value) for value in payload["hidden_bias"]),
                output_weights=tuple(int(value) for value in payload["output_weights"]),
                output_bias=int(payload["output_bias"]),
                output_denominator=int(payload["output_denominator"]),
                activation_clip=int(payload["activation_clip"]),
                score_clip=int(payload["score_clip"]),
                recommended_blend_percent=int(payload["recommended_blend_percent"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid neural artifact: {error}") from error
        supplied_id = payload.get("artifact_id")
        if supplied_id is not None and str(supplied_id) != network.artifact_id:
            raise ValueError("neural artifact_id does not match its payload")
        return network

    def predict_active(self, active_features: Sequence[int]) -> int:
        if any(
            type(index) is not int or not 0 <= index < self.feature_count
            for index in active_features
        ):
            raise ValueError("active neural feature index is invalid")
        if len(active_features) != len(set(active_features)):
            raise ValueError("active neural feature indexes must be unique")
        hidden = list(self.hidden_bias)
        width = self.hidden_size
        for index in active_features:
            offset = index * width
            for hidden_index in range(width):
                hidden[hidden_index] += self.input_weights[offset + hidden_index]
        for index, value in enumerate(hidden):
            hidden[index] = min(self.activation_clip, max(0, value))
        accumulator = self.output_bias + sum(
            value * weight
            for value, weight in zip(hidden, self.output_weights, strict=True)
        )
        score = _divide_nearest(accumulator, self.output_denominator)
        return min(self.score_clip, max(-self.score_clip, score))

    def predict(self, state: ProgressiveState) -> int:
        return self.predict_active(extract_active_features(state))


@dataclass(frozen=True, slots=True)
class NeuralBlend:
    """A named search overlay over one immutable hand-authored profile."""

    network: FixedPointNetwork
    base_profile_id: str
    base_profile_name: str
    blend_percent: int
    name: str = "Scottish Progressive neural blend"

    def __post_init__(self) -> None:
        if self.network.source_fingerprint != ENGINE_SOURCE_FINGERPRINT:
            raise ValueError("neural artifact source fingerprint is stale")
        if self.network.base_profile_id != self.base_profile_id:
            raise ValueError("neural artifact was distilled for a different base profile")
        if not self.base_profile_id or not self.base_profile_name or not self.name.strip():
            raise ValueError("neural blend profile identity cannot be empty")
        if not 0 <= self.blend_percent <= 100:
            raise ValueError("blend_percent must be from zero through 100")

    @classmethod
    def for_profile(
        cls,
        network: FixedPointNetwork,
        profile: EngineProfile,
        *,
        blend_percent: int | None = None,
        name: str | None = None,
    ) -> NeuralBlend:
        selected = (
            network.recommended_blend_percent
            if blend_percent is None
            else blend_percent
        )
        return cls(
            network=network,
            base_profile_id=profile.profile_id,
            base_profile_name=profile.name,
            blend_percent=selected,
            name=name or f"{profile.name} + neural {network.artifact_id[-8:]}",
        )

    @property
    def variant_id(self) -> str:
        payload = {
            "base_profile_id": self.base_profile_id,
            "network_artifact_id": self.network.artifact_id,
            "blend_percent": self.blend_percent,
        }
        return "spc-neural-variant-" + hashlib.sha256(
            _canonical_json(payload)
        ).hexdigest()[:20]

    def score(self, state: ProgressiveState, hand_score: int) -> int:
        return blend_scores(
            hand_score,
            self.network.predict(state),
            self.blend_percent,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": "spc-neural-blend-v1",
            "variant_id": self.variant_id,
            "base_profile_id": self.base_profile_id,
            "base_profile_name": self.base_profile_name,
            "blend_percent": self.blend_percent,
            "name": self.name,
            "network": self.network.as_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        profile: EngineProfile,
    ) -> NeuralBlend:
        if not isinstance(payload, Mapping) or payload.get("format") != "spc-neural-blend-v1":
            raise ValueError("unsupported neural blend artifact")
        try:
            network_payload = payload["network"]
            if not isinstance(network_payload, Mapping):
                raise ValueError("neural blend network must be an object")
            blend = cls(
                network=FixedPointNetwork.from_dict(network_payload),
                base_profile_id=str(payload["base_profile_id"]),
                base_profile_name=str(payload["base_profile_name"]),
                blend_percent=int(payload["blend_percent"]),
                name=str(payload["name"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid neural blend artifact: {error}") from error
        if blend.base_profile_id != profile.profile_id:
            raise ValueError("neural blend artifact is bound to a different profile")
        if blend.base_profile_name != profile.name:
            raise ValueError("neural blend artifact base profile name does not match")
        supplied_id = payload.get("variant_id")
        if supplied_id is not None and str(supplied_id) != blend.variant_id:
            raise ValueError("neural blend variant_id does not match its payload")
        return blend


def save_network(network: FixedPointNetwork, destination: str | Path) -> Path:
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(network.as_dict(), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return target


def load_network(
    source: str | Path,
    *,
    require_current_source: bool = True,
) -> FixedPointNetwork:
    path = Path(source).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load neural artifact {path}: {error}") from error
    network = FixedPointNetwork.from_dict(payload)
    if require_current_source and network.source_fingerprint != ENGINE_SOURCE_FINGERPRINT:
        raise ValueError("neural artifact source fingerprint is stale")
    return network


def save_dataset(
    dataset: NeuralDataset,
    destination: str | Path,
    *,
    max_bytes: int | None = None,
) -> Path:
    """Atomically streams a deterministic dataset with an optional hard cap."""

    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise ValueError("max_bytes must be a positive integer")
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        dataset_id = dataset.corpus_fingerprint
        ordered_samples = sorted(dataset.samples, key=lambda sample: sample.sample_id)
        written = 0

        def write_part(stream: Any, part: bytes) -> None:
            nonlocal written
            if max_bytes is not None and written + len(part) > max_bytes:
                raise ValueError(
                    f"neural dataset exceeds the {max_bytes}-byte artifact cap"
                )
            stream.write(part)
            written += len(part)

        fields: tuple[tuple[str, Any], ...] = (
            ("base_profile_id", dataset.base_profile_id),
            ("dataset_id", dataset_id),
            ("feature_fingerprint", dataset.feature_fingerprint),
            ("format", "spc-neural-dataset-v1"),
            ("samples", None),
            ("seed", dataset.seed),
            ("source_fingerprint", dataset.source_fingerprint),
            ("test_percent", dataset.test_percent),
            ("validation_percent", dataset.validation_percent),
        )
        with os.fdopen(handle, "wb") as stream:
            write_part(stream, b"{")
            for field_index, (name, value) in enumerate(fields):
                if field_index:
                    write_part(stream, b",")
                write_part(stream, _canonical_value(name))
                write_part(stream, b":")
                if name == "samples":
                    write_part(stream, b"[")
                    for sample_index, sample in enumerate(ordered_samples):
                        if sample_index:
                            write_part(stream, b",")
                        write_part(stream, _canonical_json(sample.as_dict()))
                    write_part(stream, b"]")
                else:
                    write_part(stream, _canonical_value(value))
            write_part(stream, b"}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return target


def load_dataset(source: str | Path) -> NeuralDataset:
    path = Path(source).expanduser().resolve()
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load neural dataset {path}: {error}") from error
    return NeuralDataset.from_dict(payload)


def load_optional_blend(
    source: str | Path,
    profile: EngineProfile,
    *,
    blend_percent: int | None = None,
) -> tuple[NeuralBlend | None, str]:
    """Loads a neural overlay or returns an explicit hand-evaluator fallback."""

    try:
        network = load_network(source)
        overlay = NeuralBlend.for_profile(
            network,
            profile,
            blend_percent=blend_percent,
        )
    except ValueError as error:
        return None, f"hand-evaluator-fallback: {error}"
    return overlay, f"neural-overlay-loaded: {overlay.variant_id}"


@dataclass(frozen=True, slots=True)
class NeuralSample:
    game_key: str
    position_hash: str
    pfen: str
    active_features: tuple[int, ...]
    base_profile_id: str
    base_hand_score: int
    teacher_score: int | None = None
    teacher_proof: str | None = None
    teacher_result_fingerprint: str | None = None
    teacher_profile_id: str | None = None
    teacher_completed_depth: int | None = None
    teacher_exact_width: bool | None = None
    weak_wdl_milli: int | None = None
    weak_source_fingerprint: str | None = None
    sample_weight_milli: int = 1_000
    split_component: str = ""
    split: str = ""

    def __post_init__(self) -> None:
        if not self.game_key or not self.position_hash or not self.pfen:
            raise ValueError("neural samples require game, position, and PFEN identities")
        if not self.base_profile_id:
            raise ValueError("neural samples require a base profile identity")
        if type(self.base_hand_score) is not int:
            raise ValueError("neural sample base hand score must be an exact integer")
        if not self.active_features:
            raise ValueError("neural samples require active features")
        if tuple(sorted(self.active_features)) != self.active_features:
            raise ValueError("neural sample features must be sorted")
        if len(self.active_features) != len(set(self.active_features)) or any(
            type(index) is not int or not 0 <= index < FEATURE_COUNT
            for index in self.active_features
        ):
            raise ValueError("neural sample features are invalid")
        state = _state_from_pfen(self.pfen)
        if state.position_hash != self.position_hash:
            raise ValueError("neural sample PFEN does not match its position hash")
        if extract_active_features(state) != self.active_features:
            raise ValueError("neural sample PFEN does not match its active features")
        teacher_present = self.teacher_score is not None
        if teacher_present != (self.teacher_result_fingerprint is not None):
            raise ValueError("teacher score and fingerprint must be present together")
        if teacher_present != (self.teacher_completed_depth is not None):
            raise ValueError("teacher score and completed depth must be present together")
        if teacher_present != (self.teacher_exact_width is not None):
            raise ValueError("teacher score and exact-width flag must be present together")
        if teacher_present != (self.teacher_profile_id is not None):
            raise ValueError("teacher score and profile id must be present together")
        if teacher_present and self.teacher_profile_id != self.base_profile_id:
            raise ValueError("teacher profile differs from the sample base profile")
        if self.teacher_proof not in {None, "white", "black", "draw"}:
            raise ValueError("teacher proof is invalid")
        if self.teacher_proof is not None and not teacher_present:
            raise ValueError("teacher proof requires a teacher score")
        weak_present = self.weak_wdl_milli is not None
        if weak_present != (self.weak_source_fingerprint is not None):
            raise ValueError("weak WDL and source fingerprint must be present together")
        if weak_present and not 0 <= self.weak_wdl_milli <= 1_000:
            raise ValueError("weak_wdl_milli must be from zero through 1000")
        if not teacher_present and not weak_present:
            raise ValueError("a neural sample needs a teacher or weak WDL label")
        if not 1 <= self.sample_weight_milli <= 1_000_000:
            raise ValueError("sample_weight_milli must be positive")
        if self.split not in {"", "train", "validation", "test"}:
            raise ValueError("neural sample split is invalid")
        if bool(self.split_component) != bool(self.split):
            raise ValueError("split and split_component must be assigned together")

    @property
    def sample_id(self) -> str:
        payload = self.deterministic_payload(include_split=False)
        return "spc-neural-sample-" + hashlib.sha256(
            _canonical_json(payload)
        ).hexdigest()[:20]

    def deterministic_payload(self, *, include_split: bool = True) -> dict[str, Any]:
        payload = {
            "game_key": self.game_key,
            "position_hash": self.position_hash,
            "pfen": self.pfen,
            "active_features": list(self.active_features),
            "base_profile_id": self.base_profile_id,
            "base_hand_score": self.base_hand_score,
            "teacher": (
                None
                if self.teacher_score is None
                else {
                    "score": self.teacher_score,
                    "proof": self.teacher_proof,
                    "result_fingerprint": self.teacher_result_fingerprint,
                    "profile_id": self.teacher_profile_id,
                    "completed_depth": self.teacher_completed_depth,
                    "exact_width": self.teacher_exact_width,
                    "label_strength": "deeper-search-teacher",
                }
            ),
            "weak_rollout": (
                None
                if self.weak_wdl_milli is None
                else {
                    "wdl_milli": self.weak_wdl_milli,
                    "source_fingerprint": self.weak_source_fingerprint,
                    "label_strength": "weak-exploration-rollout",
                }
            ),
            "sample_weight_milli": self.sample_weight_milli,
        }
        if include_split:
            payload["split_component"] = self.split_component
            payload["split"] = self.split
        return payload

    def as_dict(self) -> dict[str, Any]:
        return {**self.deterministic_payload(), "sample_id": self.sample_id}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> NeuralSample:
        if not isinstance(payload, Mapping):
            raise ValueError("neural sample must be an object")
        teacher = payload.get("teacher")
        weak = payload.get("weak_rollout")
        if teacher is not None and not isinstance(teacher, Mapping):
            raise ValueError("neural sample teacher must be an object")
        if weak is not None and not isinstance(weak, Mapping):
            raise ValueError("neural sample weak rollout must be an object")
        try:
            sample = cls(
                game_key=str(payload["game_key"]),
                position_hash=str(payload["position_hash"]),
                pfen=str(payload["pfen"]),
                active_features=tuple(int(value) for value in payload["active_features"]),
                base_profile_id=str(payload["base_profile_id"]),
                base_hand_score=int(payload["base_hand_score"]),
                teacher_score=(None if teacher is None else int(teacher["score"])),
                teacher_proof=(None if teacher is None else teacher.get("proof")),
                teacher_result_fingerprint=(
                    None if teacher is None else str(teacher["result_fingerprint"])
                ),
                teacher_profile_id=(
                    None if teacher is None else str(teacher["profile_id"])
                ),
                teacher_completed_depth=(
                    None if teacher is None else int(teacher["completed_depth"])
                ),
                teacher_exact_width=(
                    None if teacher is None else bool(teacher["exact_width"])
                ),
                weak_wdl_milli=(None if weak is None else int(weak["wdl_milli"])),
                weak_source_fingerprint=(
                    None if weak is None else str(weak["source_fingerprint"])
                ),
                sample_weight_milli=int(payload["sample_weight_milli"]),
                split_component=str(payload.get("split_component", "")),
                split=str(payload.get("split", "")),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid neural sample: {error}") from error
        supplied_id = payload.get("sample_id")
        if supplied_id is not None and str(supplied_id) != sample.sample_id:
            raise ValueError("neural sample_id does not match its payload")
        return sample


def _teacher_result_payload(
    state: ProgressiveState,
    result: SearchResult,
) -> dict[str, Any]:
    return {
        "position_hash": state.position_hash,
        "pfen": state.pfen,
        "engine_version": result.engine_version,
        "source_fingerprint": result.source_fingerprint,
        "engine_profile_id": result.engine_profile_id,
        "score": result.score,
        "proof": result.proof,
        "requested_depth": result.requested_depth,
        "completed_depth": result.completed_depth,
        "exact_width": result.exact_width,
        "root_scores_complete": result.root_scores_complete,
        "max_series_per_node": result.max_series_per_node,
        "max_generation_positions": result.max_generation_positions,
        "best_series": (
            result.best_series.machine_notation if result.best_series else None
        ),
        "principal_variation": [
            series.machine_notation for series in result.principal_variation
        ],
    }


def sample_from_teacher_result(
    state: ProgressiveState,
    result: SearchResult,
    *,
    game_key: str,
    minimum_completed_depth: int = 4,
    sample_weight_milli: int = 1_000,
) -> NeuralSample:
    """Admits only completed, non-interrupted deeper-search teacher labels."""

    if result.completed_depth < minimum_completed_depth:
        raise ValueError("teacher search did not reach the required deeper depth")
    if result.completed_depth != result.requested_depth:
        raise ValueError("teacher search is incomplete")
    if result.timed_out or result.work_limit_reached:
        raise ValueError("interrupted search cannot become a teacher label")
    if not result.root_scores_complete:
        raise ValueError("incomplete root scores cannot become a teacher label")
    if not result.engine_profile_id:
        raise ValueError("teacher search has no engine profile identity")
    payload = _teacher_result_payload(state, result)
    fingerprint = "spc-teacher-result-" + hashlib.sha256(
        _canonical_json(payload)
    ).hexdigest()
    return NeuralSample(
        game_key=game_key,
        position_hash=state.position_hash,
        pfen=state.pfen,
        active_features=extract_active_features(state),
        base_profile_id=result.engine_profile_id,
        base_hand_score=result.root_evaluation.total,
        teacher_score=result.score,
        teacher_proof=result.proof,
        teacher_result_fingerprint=fingerprint,
        teacher_profile_id=result.engine_profile_id,
        teacher_completed_depth=result.completed_depth,
        teacher_exact_width=result.exact_width,
        sample_weight_milli=sample_weight_milli,
    )


def attach_teacher_result(
    sample: NeuralSample,
    result: SearchResult,
    *,
    minimum_completed_depth: int = 4,
) -> NeuralSample:
    """Adds a verified deeper label without changing split or weak provenance."""

    state = _state_from_pfen(sample.pfen)
    teacher = sample_from_teacher_result(
        state,
        result,
        game_key=sample.game_key,
        minimum_completed_depth=minimum_completed_depth,
        sample_weight_milli=sample.sample_weight_milli,
    )
    if teacher.position_hash != sample.position_hash:
        raise ValueError("teacher label position differs from the selected sample")
    if teacher.base_profile_id != sample.base_profile_id:
        raise ValueError("teacher label used a different base profile")
    if teacher.base_hand_score != sample.base_hand_score:
        raise ValueError("teacher root evaluation differs from the stored hand score")
    return replace(
        sample,
        teacher_score=teacher.teacher_score,
        teacher_proof=teacher.teacher_proof,
        teacher_result_fingerprint=teacher.teacher_result_fingerprint,
        teacher_profile_id=teacher.teacher_profile_id,
        teacher_completed_depth=teacher.teacher_completed_depth,
        teacher_exact_width=teacher.teacher_exact_width,
    )


def _state_from_pfen(pfen: str) -> ProgressiveState:
    try:
        fen, metadata = pfen.split(" | ", 1)
        values = dict(item.split("=", 1) for item in metadata.split())
        ep_targets = tuple(
            chess.parse_square(value)
            for value in values["progressive_ep"].split(",")
            if value != "-"
        )
        state = ProgressiveState.from_fen(
            fen,
            int(values["series"]),
            quiet_series=int(values["quiet"]),
            ep_targets=ep_targets,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid progressive PFEN for neural training: {error}") from error
    if state.pfen != pfen:
        raise ValueError("progressive PFEN is not canonical")
    return state


def _weak_corpus_fingerprint(corpus: SelfPlayCorpus) -> str:
    """Content-addresses a corpus one sample at a time.

    ``SelfPlayCorpus.deterministic_payload`` expands every cached feature row
    into a second in-memory JSON tree.  Framing each canonical row gives this
    neural provenance contract the same tamper evidence with constant auxiliary
    memory.
    """

    metadata = {
        "format": "spc-neural-weak-source-v2",
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "method": corpus.method,
        "seed": corpus.seed,
        "holdout_percent": corpus.holdout_percent,
        "database_evidence": [dict(item) for item in corpus.database_evidence],
        "completed_games": corpus.completed_games,
        "excluded_games": corpus.excluded_games,
        "sample_count": len(corpus.samples),
    }
    digest = hashlib.sha256(_canonical_json(metadata))
    for sample in corpus.samples:
        encoded = _canonical_json(sample.as_dict())
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return "spc-weak-corpus-" + digest.hexdigest()


def _sample_from_weak_source(
    source: Any,
    *,
    base_profile: EngineProfile,
    weak_source: str,
    sample_weight_milli: int | None = None,
) -> NeuralSample:
    state = _state_from_pfen(source.pfen)
    if state.position_hash != source.position_hash:
        raise ValueError("weak corpus position hash does not replay")
    return NeuralSample(
        game_key=source.game_key,
        position_hash=source.position_hash,
        pfen=source.pfen,
        active_features=extract_active_features(state),
        base_profile_id=base_profile.profile_id,
        base_hand_score=source.features.score(base_profile),
        weak_wdl_milli=round(source.target_white_score * 1_000),
        weak_source_fingerprint=weak_source,
        sample_weight_milli=(
            max(1, round(source.sample_weight * 1_000))
            if sample_weight_milli is None
            else sample_weight_milli
        ),
    )


def samples_from_weak_corpus(
    corpus: SelfPlayCorpus,
    *,
    base_profile: EngineProfile,
) -> tuple[NeuralSample, ...]:
    """Converts replayed WDL into explicitly weak, never-teacher labels."""

    weak_source = _weak_corpus_fingerprint(corpus)
    return tuple(
        _sample_from_weak_source(
            sample,
            base_profile=base_profile,
            weak_source=weak_source,
        )
        for sample in corpus.samples
    )


class _DisjointSet:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            self.parent[right_root] = left_root
        else:
            self.parent[left_root] = right_root


def _split_bucket(seed: int, component_id: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"{seed}|{component_id}".encode("utf-8")).digest()[:4],
        "big",
    ) % 100


def _dataset_payload_sha256(dataset: "NeuralDataset") -> str:
    """Hashes the v1 deterministic payload without materializing all samples.

    The byte stream is deliberately identical to ``_canonical_json`` over the
    historical dictionary payload.  A production dataset can therefore retain
    its existing content identity while avoiding a second, multi-gigabyte tree
    of sample dictionaries solely to compute that identity.
    """

    digest = hashlib.sha256()
    digest.update(b"{")
    fields: tuple[tuple[str, Any], ...] = (
        ("base_profile_id", dataset.base_profile_id),
        ("feature_fingerprint", dataset.feature_fingerprint),
        ("samples", None),
        ("seed", dataset.seed),
        ("source_fingerprint", dataset.source_fingerprint),
        ("test_percent", dataset.test_percent),
        ("validation_percent", dataset.validation_percent),
    )
    ordered_samples = sorted(dataset.samples, key=lambda sample: sample.sample_id)
    for field_index, (name, value) in enumerate(fields):
        if field_index:
            digest.update(b",")
        digest.update(_canonical_value(name))
        digest.update(b":")
        if name == "samples":
            digest.update(b"[")
            for sample_index, sample in enumerate(ordered_samples):
                if sample_index:
                    digest.update(b",")
                digest.update(_canonical_json(sample.deterministic_payload()))
            digest.update(b"]")
        else:
            digest.update(_canonical_value(value))
    digest.update(b"}")
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class NeuralDataset:
    samples: tuple[NeuralSample, ...]
    base_profile_id: str
    seed: int
    validation_percent: int
    test_percent: int
    source_fingerprint: str = ENGINE_SOURCE_FINGERPRINT
    feature_fingerprint: str = FEATURE_FINGERPRINT

    def __post_init__(self) -> None:
        if not self.samples:
            raise ValueError("neural dataset requires at least one sample")
        if not self.base_profile_id:
            raise ValueError("neural dataset requires a base profile identity")
        if not 0 <= self.validation_percent <= 40:
            raise ValueError("validation_percent must be from zero through 40")
        if not 0 <= self.test_percent <= 40:
            raise ValueError("test_percent must be from zero through 40")
        if self.validation_percent + self.test_percent > 60:
            raise ValueError("validation and test together cannot exceed 60 percent")
        if self.source_fingerprint != ENGINE_SOURCE_FINGERPRINT:
            raise ValueError("neural dataset source fingerprint is stale")
        if self.feature_fingerprint != FEATURE_FINGERPRINT:
            raise ValueError("neural dataset feature fingerprint is stale")
        if len({sample.sample_id for sample in self.samples}) != len(self.samples):
            raise ValueError("neural dataset contains duplicate samples")
        if any(sample.base_profile_id != self.base_profile_id for sample in self.samples):
            raise ValueError("neural dataset mixes base profile identities")
        if any(sample.split not in {"train", "validation", "test"} for sample in self.samples):
            raise ValueError("neural dataset samples must be split")
        game_splits: dict[str, set[str]] = {}
        position_splits: dict[str, set[str]] = {}
        for sample in self.samples:
            game_splits.setdefault(sample.game_key, set()).add(sample.split)
            position_splits.setdefault(sample.position_hash, set()).add(sample.split)
        if any(len(splits) != 1 for splits in game_splits.values()):
            raise ValueError("whole-game split leakage detected")
        if any(len(splits) != 1 for splits in position_splits.values()):
            raise ValueError("transposition split leakage detected")

    @property
    def corpus_fingerprint(self) -> str:
        return "spc-neural-corpus-" + _dataset_payload_sha256(self)

    def deterministic_payload(self) -> dict[str, Any]:
        return {
            "source_fingerprint": self.source_fingerprint,
            "feature_fingerprint": self.feature_fingerprint,
            "base_profile_id": self.base_profile_id,
            "seed": self.seed,
            "validation_percent": self.validation_percent,
            "test_percent": self.test_percent,
            "samples": [
                sample.deterministic_payload()
                for sample in sorted(self.samples, key=lambda item: item.sample_id)
            ],
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": "spc-neural-dataset-v1",
            "dataset_id": self.corpus_fingerprint,
            **self.deterministic_payload(),
            "samples": [
                sample.as_dict()
                for sample in sorted(self.samples, key=lambda item: item.sample_id)
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> NeuralDataset:
        if not isinstance(payload, Mapping) or payload.get("format") != "spc-neural-dataset-v1":
            raise ValueError("unsupported neural dataset artifact")
        try:
            raw_samples = payload["samples"]
            if not isinstance(raw_samples, list):
                raise ValueError("neural dataset samples must be a list")
            converted: list[NeuralSample] = []
            for index, item in enumerate(raw_samples):
                converted.append(NeuralSample.from_dict(item))
                # json.load owns the raw list until this method returns. Drop
                # each expanded dictionary as soon as its compact dataclass is
                # validated so peak load memory is bounded by the artifact plus
                # the final sample objects, not both complete representations.
                raw_samples[index] = None
            dataset = cls(
                samples=tuple(converted),
                base_profile_id=str(payload["base_profile_id"]),
                seed=int(payload["seed"]),
                validation_percent=int(payload["validation_percent"]),
                test_percent=int(payload["test_percent"]),
                source_fingerprint=str(payload["source_fingerprint"]),
                feature_fingerprint=str(payload["feature_fingerprint"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid neural dataset artifact: {error}") from error
        supplied_id = payload.get("dataset_id")
        if supplied_id is not None and str(supplied_id) != dataset.corpus_fingerprint:
            raise ValueError("neural dataset_id does not match its payload")
        return dataset

    @property
    def teacher_fingerprint(self) -> str:
        fingerprints = sorted(
            {
                sample.teacher_result_fingerprint
                for sample in self.samples
                if sample.teacher_result_fingerprint is not None
            }
        )
        payload = {"teacher_results": fingerprints, "label": "deeper-search-only"}
        return "spc-neural-teacher-set-" + hashlib.sha256(
            _canonical_json(payload)
        ).hexdigest()

    def split_samples(self, split: str) -> tuple[NeuralSample, ...]:
        if split not in {"train", "validation", "test"}:
            raise ValueError("unknown neural split")
        return tuple(sample for sample in self.samples if sample.split == split)


def build_neural_dataset(
    samples: Sequence[NeuralSample],
    *,
    base_profile_id: str,
    seed: int = 20_260_820,
    validation_percent: int = 10,
    test_percent: int = 10,
) -> NeuralDataset:
    """Splits connected game/transposition components, never individual rows."""

    if not samples:
        raise ValueError("neural dataset requires samples")
    if not base_profile_id:
        raise ValueError("neural dataset requires a base profile identity")
    if any(sample.base_profile_id != base_profile_id for sample in samples):
        raise ValueError("neural samples were produced for a different base profile")
    if any(sample.split or sample.split_component for sample in samples):
        raise ValueError("input neural samples must not be pre-split")
    nodes = {
        *(f"game:{sample.game_key}" for sample in samples),
        *(f"position:{sample.position_hash}" for sample in samples),
    }
    components = _DisjointSet(nodes)
    for sample in samples:
        components.union(
            f"game:{sample.game_key}",
            f"position:{sample.position_hash}",
        )
    members: dict[str, list[str]] = {}
    for node in sorted(nodes):
        members.setdefault(components.find(node), []).append(node)
    component_ids = {
        root: "spc-neural-split-"
        + hashlib.sha256(_canonical_json({"members": values})).hexdigest()[:20]
        for root, values in members.items()
    }
    assigned: list[NeuralSample] = []
    for sample in samples:
        root = components.find(f"game:{sample.game_key}")
        component_id = component_ids[root]
        bucket = _split_bucket(seed, component_id)
        if bucket < test_percent:
            split = "test"
        elif bucket < test_percent + validation_percent:
            split = "validation"
        else:
            split = "train"
        assigned.append(
            replace(
                sample,
                split_component=component_id,
                split=split,
            )
        )
    return NeuralDataset(
        samples=tuple(sorted(assigned, key=lambda item: item.sample_id)),
        base_profile_id=base_profile_id,
        seed=seed,
        validation_percent=validation_percent,
        test_percent=test_percent,
    )


def build_neural_dataset_from_weak_corpus(
    corpus: SelfPlayCorpus,
    *,
    base_profile: EngineProfile,
    seed: int = 20_260_820,
    validation_percent: int = 10,
    test_percent: int = 10,
    max_positions_per_game: int = 3,
) -> NeuralDataset:
    """Preserves the verified full-game split instead of reconnecting it.

    The full-game builder has already assigned whole games to train/holdout and
    removed train/holdout transposition overlap.  Re-running a global connected
    component split here would reconnect a dense run into one giant component.
    This bridge keeps train fixed, deterministically divides holdout by game,
    and removes only cross-validation/test occurrences of a shared position.
    Every represented game is then sampled across its timeline and reweighted
    to total exactly 1000 milli-units.
    """

    from .selfplay_training import FULLGAME_CORPUS_METHOD, SelfPlayCorpus

    if type(corpus) is not SelfPlayCorpus or corpus.method != FULLGAME_CORPUS_METHOD:
        raise ValueError("neural weak bridge requires a verified full-game corpus")
    if validation_percent < 0 or test_percent < 0:
        raise ValueError("neural validation and test percentages cannot be negative")
    if validation_percent + test_percent != corpus.holdout_percent:
        raise ValueError(
            "neural validation plus test percentages must equal the verified "
            "full-game holdout percentage"
        )
    if validation_percent + test_percent == 0:
        raise ValueError("neural full-game bridge requires a nonzero sealed holdout")
    if type(max_positions_per_game) is not int or not 1 <= max_positions_per_game <= 64:
        raise ValueError("max_positions_per_game must be from 1 through 64")

    source_by_game: dict[str, list[Any]] = {}
    source_split_by_game: dict[str, str] = {}
    seen_identities: set[tuple[str, str]] = set()
    for source in corpus.samples:
        if source.split not in {"train", "holdout"}:
            raise ValueError("full-game corpus contains an unsupported split")
        existing_split = source_split_by_game.setdefault(source.game_key, source.split)
        if existing_split != source.split:
            raise ValueError("full-game corpus split one game across partitions")
        identity = (source.game_key, source.position_hash)
        if identity in seen_identities:
            raise ValueError("full-game corpus repeats a position within one game")
        seen_identities.add(identity)
        source_by_game.setdefault(source.game_key, []).append(source)

    desired_split_by_game: dict[str, str] = {}
    holdout_span = validation_percent + test_percent
    for game_key in source_by_game:
        source_split = source_split_by_game[game_key]
        if source_split == "train":
            split = "train"
        else:
            bucket = int.from_bytes(
                hashlib.sha256(
                    f"{seed}|neural-holdout|{game_key}".encode("utf-8")
                ).digest()[:8],
                "big",
            ) % holdout_span
            split = "validation" if bucket < validation_percent else "test"
        desired_split_by_game[game_key] = split

    splits_by_position: dict[str, set[str]] = {}
    for game_key, sources in source_by_game.items():
        split = desired_split_by_game[game_key]
        for source in sources:
            splits_by_position.setdefault(source.position_hash, set()).add(split)
    owner_by_position: dict[str, str] = {}
    for position_hash, splits in splits_by_position.items():
        if "train" in splits:
            owner_by_position[position_hash] = "train"
            continue
        ordered = sorted(splits)
        owner_index = int.from_bytes(
            hashlib.sha256(
                f"{seed}|neural-position-owner|{position_hash}".encode("utf-8")
            ).digest()[:8],
            "big",
        ) % len(ordered)
        owner_by_position[position_hash] = ordered[owner_index]

    # Convert only the retained timeline samples. On a long million-game trace
    # this avoids parsing PFEN and evaluating every discarded intermediate
    # boundary before the per-game cap is applied.
    weak_source = _weak_corpus_fingerprint(corpus)
    selected: list[NeuralSample] = []
    for game_key in sorted(source_by_game):
        split = desired_split_by_game[game_key]
        retained = [
            source
            for source in source_by_game[game_key]
            if owner_by_position[source.position_hash] == split
        ]
        retained.sort(
            key=lambda source: (
                source.series_number,
                source.position_hash,
            )
        )
        if not retained:
            continue
        if len(retained) > max_positions_per_game:
            count = len(retained)
            retained = [
                retained[((2 * index + 1) * count) // (2 * max_positions_per_game)]
                for index in range(max_positions_per_game)
            ]
        count = len(retained)
        base_weight, remainder = divmod(1_000, count)
        component_id = "spc-neural-game-split-" + hashlib.sha256(
            _canonical_json(
                {
                    "seed": seed,
                    "game_key": game_key,
                    "source_split": source_split_by_game[game_key],
                    "split": split,
                }
            )
        ).hexdigest()[:20]
        for index, source in enumerate(retained):
            sample = _sample_from_weak_source(
                source,
                base_profile=base_profile,
                weak_source=weak_source,
                sample_weight_milli=base_weight + (1 if index < remainder else 0),
            )
            selected.append(
                replace(
                    sample,
                    split_component=component_id,
                    split=split,
                )
            )

    required = {
        split
        for split, percent in (
            ("train", 100 - validation_percent - test_percent),
            ("validation", validation_percent),
            ("test", test_percent),
        )
        if percent
    }
    present = {sample.split for sample in selected}
    if not required <= present:
        raise ValueError(
            "leakage filtering left an empty neural split: "
            + ", ".join(sorted(required - present))
        )
    return NeuralDataset(
        samples=tuple(sorted(selected, key=lambda sample: sample.sample_id)),
        base_profile_id=base_profile.profile_id,
        seed=seed,
        validation_percent=validation_percent,
        test_percent=test_percent,
    )


@dataclass(frozen=True, slots=True)
class NeuralTrainerConfig:
    hidden_size: int = 16
    epochs: int = 32
    seed: int = 20_260_820
    learning_rate_millionths: int = 5_000
    weak_label_weight_milli: int = 100
    max_weak_train_samples: int = 32_768
    hidden_quantization: int = 256
    output_quantization: int = 256
    score_normalizer: int = 10_000
    recommended_blend_percent: int = 25

    def __post_init__(self) -> None:
        if not 1 <= self.hidden_size <= 128:
            raise ValueError("hidden_size must be from 1 through 128")
        if not 1 <= self.epochs <= 10_000:
            raise ValueError("epochs must be from 1 through 10000")
        if not 1 <= self.learning_rate_millionths <= 100_000:
            raise ValueError("learning rate must be from 1 through 100000 millionths")
        if not 0 <= self.weak_label_weight_milli <= 250:
            raise ValueError("weak labels may contribute at most one quarter teacher weight")
        if not 0 <= self.max_weak_train_samples <= 1_000_000:
            raise ValueError("max_weak_train_samples must be from zero through 1000000")
        if not 16 <= self.hidden_quantization <= 4_096:
            raise ValueError("hidden quantization is out of range")
        if not 16 <= self.output_quantization <= 4_096:
            raise ValueError("output quantization is out of range")
        if not 1_000 <= self.score_normalizer <= 100_000:
            raise ValueError("score_normalizer is out of range")
        if not 0 <= self.recommended_blend_percent <= 100:
            raise ValueError("recommended blend must be from zero through 100")

    @property
    def trainer_fingerprint(self) -> str:
        payload = {
            "method": NEURAL_TRAINER_METHOD,
            "config": asdict(self),
            "feature_fingerprint": FEATURE_FINGERPRINT,
            "inference_scope": NEURAL_INFERENCE_SCOPE,
            "teacher_target": {
                "score_clip": NEURAL_SCORE_CLIP,
                "proof_target": PROOF_TARGET_SCORE,
            },
            "weak_target": {
                "wdl_score_span": WEAK_WDL_SCORE_SPAN,
                "maximum_relative_weight_milli": 250,
            },
        }
        return "spc-neural-trainer-" + hashlib.sha256(
            _canonical_json(payload)
        ).hexdigest()


def _round_float(value: float) -> int:
    if not math.isfinite(value):
        raise ValueError("training produced a non-finite weight")
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _teacher_target(sample: NeuralSample) -> int | None:
    if sample.teacher_score is None:
        return None
    if sample.teacher_proof == "white":
        return PROOF_TARGET_SCORE
    if sample.teacher_proof == "black":
        return -PROOF_TARGET_SCORE
    if sample.teacher_proof == "draw":
        return 0
    return min(NEURAL_SCORE_CLIP, max(-NEURAL_SCORE_CLIP, sample.teacher_score))


def _weak_target(sample: NeuralSample) -> int | None:
    if sample.weak_wdl_milli is None:
        return None
    return _divide_nearest(
        (sample.weak_wdl_milli - 500) * 2 * WEAK_WDL_SCORE_SPAN,
        1_000,
    )


def _objectives(
    sample: NeuralSample,
    config: NeuralTrainerConfig,
) -> tuple[tuple[str, int, float], ...]:
    base_weight = sample.sample_weight_milli / 1_000
    rows: list[tuple[str, int, float]] = []
    teacher = _teacher_target(sample)
    if teacher is not None:
        rows.append(("teacher", teacher, base_weight))
    weak = _weak_target(sample)
    if weak is not None and config.weak_label_weight_milli:
        rows.append(
            (
                "weak_rollout_wdl",
                weak,
                base_weight * config.weak_label_weight_milli / 1_000,
            )
        )
    return tuple(rows)


def _quantize_network(
    *,
    dataset: NeuralDataset,
    config: NeuralTrainerConfig,
    input_weights: list[list[float]],
    hidden_bias: list[float],
    output_weights: list[float],
    output_bias: float,
) -> FixedPointNetwork:
    hidden_scale = config.hidden_quantization
    output_scale = config.output_quantization
    flattened: list[int] = []
    for row in input_weights:
        flattened.extend(
            max(-(1 << 15), min((1 << 15) - 1, _round_float(value * hidden_scale)))
            for value in row
        )
    quantized_hidden_bias = tuple(
        max(-(1 << 31), min((1 << 31) - 1, _round_float(value * hidden_scale)))
        for value in hidden_bias
    )
    output_factor = output_scale * config.score_normalizer
    quantized_output = tuple(
        max(-(1 << 31), min((1 << 31) - 1, _round_float(value * output_factor)))
        for value in output_weights
    )
    denominator = hidden_scale * output_scale
    quantized_bias = _round_float(output_bias * config.score_normalizer * denominator)
    return FixedPointNetwork(
        source_fingerprint=dataset.source_fingerprint,
        base_profile_id=dataset.base_profile_id,
        teacher_fingerprint=dataset.teacher_fingerprint,
        corpus_fingerprint=dataset.corpus_fingerprint,
        trainer_fingerprint=config.trainer_fingerprint,
        hidden_size=config.hidden_size,
        input_weights=tuple(flattened),
        hidden_bias=quantized_hidden_bias,
        output_weights=quantized_output,
        output_bias=quantized_bias,
        output_denominator=denominator,
        recommended_blend_percent=config.recommended_blend_percent,
    )


def _selected_training_samples(
    train: Sequence[NeuralSample],
    config: NeuralTrainerConfig,
) -> tuple[tuple[NeuralSample, ...], dict[str, int]]:
    teachers = [sample for sample in train if _teacher_target(sample) is not None]
    weak_only = [sample for sample in train if _teacher_target(sample) is None]
    weak_only.sort(
        key=lambda sample: (
            hashlib.sha256(
                f"{config.seed}|weak-train|{sample.sample_id}".encode("utf-8")
            ).digest(),
            sample.sample_id,
        )
    )
    retained_weak = weak_only[: config.max_weak_train_samples]
    selected = tuple(sorted((*teachers, *retained_weak), key=lambda item: item.sample_id))
    return selected, {
        "available_train_samples": len(train),
        "available_teacher_samples": len(teachers),
        "available_weak_only_samples": len(weak_only),
        "selected_train_samples": len(selected),
        "selected_teacher_samples": len(teachers),
        "selected_weak_only_samples": len(retained_weak),
    }


def _metrics(
    network: FixedPointNetwork,
    dataset: NeuralDataset,
    config: NeuralTrainerConfig,
    *,
    reveal_test: bool = False,
) -> dict[str, Any]:
    accumulators = {
        split: {
            "samples": 0,
            "teacher": [0, 0.0, 0.0],
            "weak_rollout_wdl": [0, 0.0, 0.0],
        }
        for split in ("train", "validation", "test")
    }
    for sample in dataset.samples:
        split = accumulators[sample.split]
        split["samples"] += 1
        prediction = (
            None
            if sample.split == "test" and not reveal_test
            else network.predict_active(sample.active_features)
        )
        for kind, target, weight in _objectives(sample, config):
            bucket = split[kind]
            bucket[0] += 1
            if prediction is not None:
                bucket[1] += weight
                bucket[2] += abs(prediction - target) * weight

    metrics: dict[str, Any] = {}
    for split_name, accumulator in accumulators.items():
        split_metrics: dict[str, Any] = {
            "samples": accumulator["samples"],
            "sealed": split_name == "test" and not reveal_test,
        }
        for kind in ("teacher", "weak_rollout_wdl"):
            labels, total_weight, weighted_error = accumulator[kind]
            split_metrics[kind] = {
                "labels": labels,
                "mean_absolute_error": (
                    None
                    if not total_weight
                    else round(weighted_error / total_weight, 6)
                ),
                "label_strength": (
                    "deeper-search-teacher"
                    if kind == "teacher"
                    else "weak-exploration-rollout"
                ),
            }
        metrics[split_name] = split_metrics
    return metrics


def train_fixed_point_network(
    dataset: NeuralDataset,
    *,
    config: NeuralTrainerConfig | None = None,
) -> tuple[FixedPointNetwork, dict[str, Any]]:
    """Trains a small deterministic sparse network without a heavy dependency.

    The trainer performs deterministic sparse online backpropagation and
    quantizes once. All deeper teacher rows are retained; weak-only rollout
    rows are deterministically capped because a million-game exploration store
    must not turn each epoch into an unbounded Python loop.
    """

    selected = config or NeuralTrainerConfig()
    train = list(dataset.split_samples("train"))
    if not train:
        raise ValueError("neural dataset has no training split")
    if not any(_teacher_target(sample) is not None for sample in train):
        raise ValueError("initial neural training requires deeper teacher labels")
    selected_train, training_selection = _selected_training_samples(train, selected)

    rng = random.Random(selected.seed)
    width = selected.hidden_size
    input_weights = [
        [rng.uniform(-0.08, 0.08) for _ in range(width)]
        for _ in range(FEATURE_COUNT)
    ]
    hidden_bias = [rng.uniform(0.01, 0.08) for _ in range(width)]
    output_weights = [rng.uniform(-0.01, 0.01) for _ in range(width)]
    output_bias = 0.0
    learning_rate = selected.learning_rate_millionths / 1_000_000

    training_rows: list[tuple[tuple[int, ...], float, float]] = []
    for sample in selected_train:
        objectives = _objectives(sample, selected)
        total_weight = sum(weight for _kind, _target, weight in objectives)
        if total_weight <= 0.0:
            continue
        # Multiple labels on one position become one weighted compromise target.
        # Teacher weight dominates by contract; combining here avoids a second
        # sparse forward pass for rows that also retain weak WDL provenance.
        target_score = sum(
            target * weight for _kind, target, weight in objectives
        ) / total_weight
        training_rows.append(
            (sample.active_features, target_score / selected.score_normalizer, total_weight)
        )
    if not training_rows:
        raise ValueError("neural training selection has no weighted objectives")

    ordered_indexes = list(range(len(training_rows)))
    for _epoch in range(selected.epochs):
        epoch_indexes = list(ordered_indexes)
        rng.shuffle(epoch_indexes)
        for row_index in epoch_indexes:
            active, target, objective_weight = training_rows[row_index]
            hidden_pre = [
                hidden_bias[index]
                + sum(input_weights[feature][index] for feature in active)
                for index in range(width)
            ]
            hidden = [max(0.0, value) for value in hidden_pre]
            prediction = output_bias + sum(
                value * weight
                for value, weight in zip(hidden, output_weights, strict=True)
            )
            gradient = max(-8.0, min(8.0, prediction - target)) * objective_weight
            previous_output = list(output_weights)
            output_bias -= learning_rate * gradient
            for index in range(width):
                output_weights[index] -= learning_rate * gradient * hidden[index]
            active_scale = max(1, len(active))
            for index, value in enumerate(hidden_pre):
                if value <= 0:
                    continue
                hidden_gradient = gradient * previous_output[index]
                hidden_bias[index] -= learning_rate * hidden_gradient
                step = learning_rate * hidden_gradient / active_scale
                for feature in active:
                    input_weights[feature][index] -= step

    network = _quantize_network(
        dataset=dataset,
        config=selected,
        input_weights=input_weights,
        hidden_bias=hidden_bias,
        output_weights=output_weights,
        output_bias=output_bias,
    )
    label_counts = {
        "teacher": sum(sample.teacher_score is not None for sample in dataset.samples),
        "weak_rollout_wdl": sum(
            sample.weak_wdl_milli is not None for sample in dataset.samples
        ),
    }
    report = {
        "method": NEURAL_TRAINER_METHOD,
        "artifact_id": network.artifact_id,
        "source_fingerprint": network.source_fingerprint,
        "base_profile_id": network.base_profile_id,
        "feature_fingerprint": network.feature_fingerprint,
        "teacher_fingerprint": network.teacher_fingerprint,
        "corpus_fingerprint": network.corpus_fingerprint,
        "trainer_fingerprint": network.trainer_fingerprint,
        "config": asdict(selected),
        "training_selection": training_selection,
        "labels": {
            **label_counts,
            "teacher_contract": "completed deeper SearchResult score/proof",
            "weak_contract": "exploration rollout WDL; downweighted and never promotion evidence",
            "weak_relative_weight_milli": selected.weak_label_weight_milli,
        },
        "split_contract": "whole-game and transposition-component disjoint",
        "inference_scope": NEURAL_INFERENCE_SCOPE,
        "partial_frontier_scoring_eligible": False,
        "metrics": _metrics(network, dataset, selected, reveal_test=False),
        "strength_claim": False,
        "promotion_eligible": False,
        "next_gate": "tactical/rules non-regression, then identical depth-3 match",
    }
    return network, report
