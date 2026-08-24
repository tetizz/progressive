from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

from scottish_progressive.corpus_shards import progressive_state_dedup_key
from scottish_progressive.fast_training import CachedFeatures, FEATURE_NAMES
from scottish_progressive.profiles import (
    EngineProfile,
    EvaluationWeights,
    baseline_profile,
    load_profile,
)
from scottish_progressive.rules import play_series
from scottish_progressive.teacher_value_features import (
    TEACHER_VALUE_FEATURE_NAMES,
    TEACHER_VALUE_FEATURE_SCHEMA,
    TeacherValueFeaturesV3,
    state_from_pfen,
)


CORPUS_SCHEMA = "spc-deep-teacher-corpus-v1"
CORPUS_METHOD = "balanced-native-trajectory-mixed-depth-policy-teacher-v1"
MODEL_SCHEMA = "spc-deep-teacher-linear-value-v1"
FIT_RECEIPT_SCHEMA = "spc-deep-teacher-fit-receipt-v1"
HOLDOUT_RECEIPT_SCHEMA = "spc-deep-teacher-holdout-receipt-v1"
FIXED_POINT_SCALE = 1_000_000_000
MATE_SCORE = 1_000_000
DEFAULT_ADVERSE_PAIR_WEIGHT = 8.0
BASELINE_WEIGHTS = (100,) * len(FEATURE_NAMES)
DEVELOPMENT_PROFILE_WEIGHTS = (238, 188, 203, 223, 28, 164, 294)
NONROUTE_GROUPS = (
    "base7",
    "phase14",
    "cached19",
    "positional38",
    "direct44",
)
FEATURE_GROUPS = {
    "base7": tuple(range(7)),
    "phase14": tuple(range(14)),
    "cached19": tuple(range(19)),
    "positional38": tuple(range(38)),
    "direct44": tuple(range(44)),
    "all47": tuple(range(47)),
}
RIDGES = (0.0003, 0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0, 3.0)
QUARANTINED_HOLDOUT_CORPORA = {
    "c889078ca4e54780123b74c4b747cde2e74c2eb7bb12be61e786e3307aabef7f": (
        "evaluation-contaminated: an exploratory Stockfish correlation inspected "
        "14 holdout labels"
    ),
}


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_hashes() -> dict[str, str]:
    paths = {
        Path(__file__).resolve(),
        Path(sys.modules[TeacherValueFeaturesV3.__module__].__file__).resolve(),
        Path(sys.modules[CachedFeatures.__module__].__file__).resolve(),
        Path(sys.modules[progressive_state_dedup_key.__module__].__file__).resolve(),
        Path(sys.modules[play_series.__module__].__file__).resolve(),
        Path(sys.modules[baseline_profile.__module__].__file__).resolve(),
    }
    return {str(path): _sha256(path) for path in sorted(paths)}


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
    except FileExistsError as error:
        raise FileExistsError(
            "this fit receipt has already opened its one-shot holdout; "
            "refitting is required for another evaluation"
        ) from error


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _reject_quarantined_holdout(corpus: Mapping[str, Any]) -> None:
    generation = corpus.get("generation")
    if not isinstance(generation, Mapping):
        return
    holdout_sha256 = generation.get("holdout_corpus_sha256")
    if not isinstance(holdout_sha256, str):
        return
    reason = QUARANTINED_HOLDOUT_CORPORA.get(holdout_sha256)
    if reason is not None:
        raise ValueError(
            "teacher corpus holdout is permanently quarantined; "
            f"sha256={holdout_sha256}; reason={reason}"
        )


@dataclass(frozen=True, slots=True)
class TeacherOption:
    series: str
    score_white: int
    proof: str | None
    proof_bounds: tuple[int, int]
    signed_mate_distance: int | None
    final_state_key: str
    final_pfen: str
    outcome: str | None
    ended_by_check: bool
    is_teacher_best: bool
    is_hard_negative: bool
    features: tuple[int, ...]
    base_features: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TeacherLabel:
    split: str
    state_key: str
    position_hash: str
    pfen: str
    series_number: int
    mover_sign: int
    source_profile_id: str
    teacher_tier: str
    teacher_depth_series: int
    teacher_best_series: str
    teacher_score_white: int
    teacher_proof: str | None
    teacher_signed_mate_distance: int | None
    options: tuple[TeacherOption, ...]

    @property
    def tactical(self) -> bool:
        return bool(
            self.teacher_proof is not None
            or self.teacher_signed_mate_distance is not None
            or any(
                option.is_hard_negative
                or option.proof is not None
                or option.outcome is not None
                for option in self.options
            )
        )


def _option_is_mover_adverse(
    label: TeacherLabel, option: TeacherOption
) -> bool:
    opponent = "black" if label.mover_sign == 1 else "white"
    return option.proof == opponent


def _validate_adverse_pair_weight(value: float) -> float:
    weight = float(value)
    if not math.isfinite(weight) or not 1.0 <= weight <= 1_000.0:
        raise ValueError("adverse_pair_weight must be finite and between 1 and 1000")
    return weight


def _cached_payload_matches(
    supplied: Mapping[str, Any],
    regenerated: CachedFeatures,
    *,
    label: str,
) -> None:
    expected = regenerated.as_dict()
    missing = [name for name in expected if name not in supplied]
    if missing:
        raise ValueError(f"{label} cached features miss {missing}")
    mismatches = {
        name: (supplied[name], expected[name])
        for name in expected
        if supplied[name] != expected[name]
    }
    if mismatches:
        raise ValueError(f"{label} cached features differ from regenerated values: {mismatches}")


def _materialize_labels(
    corpus: Mapping[str, Any],
    *,
    selected_split: str | None,
) -> tuple[tuple[TeacherLabel, ...], dict[str, Any]]:
    if corpus.get("schema") != CORPUS_SCHEMA:
        raise ValueError(
            f"unsupported teacher corpus schema: {corpus.get('schema')!r}"
        )
    if corpus.get("method") != CORPUS_METHOD:
        raise ValueError(
            f"unsupported teacher corpus method: {corpus.get('method')!r}"
        )
    declared_contract = corpus.get("contract")
    corpus_id_prefix = (
        "spc-native-mixed-teacher-exploratory-"
        if isinstance(declared_contract, Mapping)
        and declared_contract.get("exploratory_only") is True
        else "spc-native-mixed-teacher-"
    )
    deterministic = {
        key: value
        for key, value in corpus.items()
        if key not in {"corpus_id", "runtime"}
    }
    expected_corpus_id = (
        corpus_id_prefix
        + hashlib.sha256(_canonical_json(deterministic)).hexdigest()[:20]
    )
    if corpus.get("corpus_id") != expected_corpus_id:
        raise ValueError("teacher corpus_id does not match its deterministic payload")
    raw_labels = corpus.get("labels")
    if not isinstance(raw_labels, list) or not raw_labels:
        raise ValueError("teacher corpus labels must be a nonempty list")
    quality = corpus.get("quality")
    if (
        not isinstance(quality, Mapping)
        or quality.get("status") != "complete"
        or int(quality.get("accepted_roots", -1)) != len(raw_labels)
    ):
        raise ValueError("teacher corpus quality contract is incomplete")
    tiers = corpus.get("tiers")
    if not isinstance(tiers, Mapping) or set(tiers) != {
        "quiet_d2",
        "tactical_d3",
    }:
        raise ValueError("teacher corpus must contain both fixed teacher tiers")
    selection = corpus.get("selection")
    required_zero_audits = (
        "selected_root_exact_overlap_states",
        "cross_split_option_final_exact_overlap_states",
        "train_option_final_to_holdout_root_overlap_states",
        "holdout_option_final_to_train_root_overlap_states",
    )
    if not isinstance(selection, Mapping) or any(
        selection.get(name) != 0 for name in required_zero_audits
    ):
        raise ValueError("teacher corpus declares a nonzero leakage audit")
    contract = declared_contract
    if not isinstance(contract, Mapping) or any(
        contract.get(name) is not expected
        for name, expected in {
            "incomplete_labels_cached": False,
            "full_retained_root_scores_required": True,
            "depth_is_per_label_provenance": True,
            "cross_depth_quality_metrics_blended": False,
            "train_holdout_exact_leakage_allowed": False,
            "strength_claim": False,
        }.items()
    ):
        raise ValueError("teacher corpus safety contract differs")
    labels: list[TeacherLabel] = []
    root_keys: dict[str, set[str]] = {"train": set(), "holdout": set()}
    final_keys: dict[str, set[str]] = {"train": set(), "holdout": set()}
    all_counts = {"train": 0, "holdout": 0}
    for raw in raw_labels:
        split = str(raw["split"])
        if split not in root_keys:
            raise ValueError(f"unknown teacher split: {split}")
        all_counts[split] += 1
        root_key = str(raw["state_key_sha256"])
        if root_key in root_keys[split]:
            raise ValueError(f"duplicate teacher root key in {split}: {root_key}")
        root_promoted = raw["root_promoted_bitboard"]
        root_chess960 = raw["root_chess960"]
        if type(root_promoted) is not int or type(root_chess960) is not bool:
            raise ValueError(f"root semantic metadata has the wrong type: {root_key}")
        root_state_identity = state_from_pfen(
            str(raw["pfen"]),
            promoted_bitboard=root_promoted,
            chess960=root_chess960,
        )
        regenerated_root_key = progressive_state_dedup_key(
            root_state_identity
        ).hex()
        if regenerated_root_key != root_key:
            raise ValueError(f"root semantic state key mismatch: {root_key}")
        if root_state_identity.position_hash != str(raw["position_hash"]):
            raise ValueError(f"root PFEN/hash mismatch: {root_key}")
        if root_state_identity.series_number != int(raw["series_number"]):
            raise ValueError(f"root series mismatch: {root_key}")
        expected_mover = "white" if root_state_identity.board.turn else "black"
        if expected_mover != str(raw["mover"]):
            raise ValueError(f"root mover mismatch: {root_key}")
        root_keys[split].add(root_key)
        raw_options = raw.get("options")
        if not isinstance(raw_options, list) or not raw_options:
            raise ValueError(f"teacher label has no options: {root_key}")
        identity_final_states = []
        for option in raw_options:
            final_key = str(option["final_state_key_sha256"])
            final_promoted = option["final_promoted_bitboard"]
            final_chess960 = option["final_chess960"]
            if type(final_promoted) is not int or type(final_chess960) is not bool:
                raise ValueError(
                    f"option semantic metadata has the wrong type: "
                    f"{root_key}/{option['series']}"
                )
            final_state_from_pfen = state_from_pfen(
                str(option["final_pfen"]),
                promoted_bitboard=final_promoted,
                chess960=final_chess960,
            )
            series = str(option["series"])
            moves = () if not series else tuple(series.split("/"))
            replayed = play_series(root_state_identity, moves)
            final_state_identity = replayed.final_state
            if replayed.machine_notation != series:
                raise ValueError(
                    f"option replay notation mismatch: {root_key}/{series}"
                )
            if final_state_identity.pfen != str(option["final_pfen"]):
                raise ValueError(f"option replay PFEN mismatch: {root_key}/{series}")
            if (
                final_state_identity.board.promoted != final_promoted
                or final_state_identity.board.chess960 != final_chess960
            ):
                raise ValueError(
                    f"option replay semantic metadata mismatch: {root_key}/{series}"
                )
            replayed_outcome = (
                None if replayed.outcome is None else replayed.outcome.value
            )
            if (
                replayed_outcome != option.get("outcome")
                or replayed.ended_by_check != bool(option["ended_by_check"])
            ):
                raise ValueError(f"option replay outcome mismatch: {root_key}/{series}")
            regenerated_final_key = progressive_state_dedup_key(
                final_state_identity
            ).hex()
            if regenerated_final_key != final_key:
                raise ValueError(
                    f"option semantic state key mismatch: "
                    f"{root_key}/{option['series']}"
                )
            if progressive_state_dedup_key(final_state_from_pfen).hex() != final_key:
                raise ValueError(
                    f"option PFEN semantic state key mismatch: {root_key}/{series}"
                )
            final_keys[split].add(final_key)
            identity_final_states.append(final_state_identity)
        if selected_split is not None and split != selected_split:
            continue
        search = raw["search"]
        if (
            int(search["completed_depth_series"])
            != int(search["requested_depth_series"])
            or bool(search["timed_out"])
            or bool(search["work_limit_reached"])
            or not bool(search["root_scores_complete"])
        ):
            raise ValueError(f"accepted teacher label is incomplete: {root_key}")
        teacher_tier = str(raw["teacher_tier"])
        teacher_depth_series = int(raw["teacher_depth_series"])
        expected_tier_depth = {
            "quiet_d2": 2,
            "tactical_d3": 3,
        }.get(teacher_tier)
        if (
            expected_tier_depth is None
            or teacher_depth_series != expected_tier_depth
            or teacher_depth_series != int(search["requested_depth_series"])
        ):
            raise ValueError(f"teacher tier/depth mismatch: {root_key}")
        root_state = root_state_identity
        _cached_payload_matches(
            raw["root_features"],
            CachedFeatures.from_state(root_state),
            label=f"root {root_key}",
        )
        options: list[TeacherOption] = []
        best_count = 0
        for raw_option, final_state in zip(
            raw_options, identity_final_states, strict=True
        ):
            final_key = str(raw_option["final_state_key_sha256"])
            raw_proof_bounds = raw_option.get("proof_bounds")
            if (
                not isinstance(raw_proof_bounds, list)
                or len(raw_proof_bounds) != 2
                or any(type(value) is not int for value in raw_proof_bounds)
            ):
                raise ValueError(
                    f"option proof bounds are not an integer pair: "
                    f"{root_key}/{raw_option['series']}"
                )
            supplied_features = raw_option["final_features"]
            _cached_payload_matches(
                supplied_features,
                CachedFeatures.from_state(final_state),
                label=f"option {root_key}/{raw_option['series']}",
            )
            raw_pv = raw_option.get("principal_variation")
            if not isinstance(raw_pv, list) or not raw_pv:
                raise ValueError(
                    f"option has no replayable principal variation: "
                    f"{root_key}/{raw_option['series']}"
                )
            pv_state = root_state
            for expected_ply, raw_pv_row in enumerate(raw_pv, 1):
                if int(raw_pv_row["series_ply"]) != expected_ply:
                    raise ValueError(
                        f"teacher PV ply mismatch: {root_key}/{raw_option['series']}"
                    )
                pv_series = str(raw_pv_row["series"])
                pv_moves = () if not pv_series else tuple(pv_series.split("/"))
                pv_replayed = play_series(pv_state, pv_moves)
                pv_key = progressive_state_dedup_key(
                    pv_replayed.final_state
                ).hex()
                pv_outcome = (
                    None
                    if pv_replayed.outcome is None
                    else pv_replayed.outcome.value
                )
                if (
                    pv_replayed.machine_notation != pv_series
                    or pv_key != str(raw_pv_row["final_state_key_sha256"])
                    or pv_outcome != raw_pv_row.get("outcome")
                    or pv_replayed.ended_by_check
                    != bool(raw_pv_row["ended_by_check"])
                ):
                    raise ValueError(
                        f"teacher PV replay mismatch: "
                        f"{root_key}/{raw_option['series']}/{expected_ply}"
                    )
                if expected_ply == 1 and (
                    pv_series != str(raw_option["series"])
                    or pv_key != final_key
                ):
                    raise ValueError(
                        f"teacher PV root option mismatch: "
                        f"{root_key}/{raw_option['series']}"
                    )
                pv_state = pv_replayed.final_state
            features = TeacherValueFeaturesV3.from_state_and_cached(
                final_state,
                supplied_features,
            ).values
            base_features = tuple(int(supplied_features[name]) for name in FEATURE_NAMES)
            is_best = bool(raw_option["is_teacher_best"])
            best_count += int(is_best)
            options.append(
                TeacherOption(
                    series=str(raw_option["series"]),
                    score_white=int(raw_option["score_white_heuristic_points"]),
                    proof=(None if raw_option.get("proof") is None else str(raw_option["proof"])),
                    proof_bounds=(raw_proof_bounds[0], raw_proof_bounds[1]),
                    signed_mate_distance=(
                        None
                        if raw_option.get("signed_mate_distance_series") is None
                        else int(raw_option["signed_mate_distance_series"])
                    ),
                    final_state_key=final_key,
                    final_pfen=str(raw_option["final_pfen"]),
                    outcome=(None if raw_option.get("outcome") is None else str(raw_option["outcome"])),
                    ended_by_check=bool(raw_option["ended_by_check"]),
                    is_teacher_best=is_best,
                    is_hard_negative=bool(raw_option["is_hard_negative"]),
                    features=features,
                    base_features=base_features,
                )
            )
        if best_count != 1:
            raise ValueError(
                f"teacher label must flag exactly one best option: {root_key}"
            )
        best_series = str(raw["teacher_best_series"])
        if not any(option.series == best_series and option.is_teacher_best for option in options):
            raise ValueError(f"teacher best is not a flagged retained option: {root_key}")
        selected_best = next(
            option for option in options if option.series == best_series
        )
        teacher_score = int(raw["teacher_score_white_heuristic_points"])
        if selected_best.score_white != teacher_score:
            raise ValueError(f"teacher best score differs from retained option: {root_key}")
        teacher_best_proof = (
            None
            if raw.get("teacher_best_proof") is None
            else str(raw["teacher_best_proof"])
        )
        if selected_best.proof != teacher_best_proof:
            raise ValueError(f"teacher best proof differs from retained option: {root_key}")
        teacher_best_bounds = raw.get("teacher_best_proof_bounds")
        if (
            not isinstance(teacher_best_bounds, list)
            or len(teacher_best_bounds) != 2
            or any(type(value) is not int for value in teacher_best_bounds)
            or selected_best.proof_bounds
            != (teacher_best_bounds[0], teacher_best_bounds[1])
        ):
            raise ValueError(
                f"teacher best proof bounds differ from retained option: {root_key}"
            )
        teacher_mate_distance = (
            None
            if raw.get("teacher_signed_mate_distance_series") is None
            else int(raw["teacher_signed_mate_distance_series"])
        )
        if selected_best.signed_mate_distance != teacher_mate_distance:
            raise ValueError(
                f"teacher best mate distance differs from retained option: {root_key}"
            )
        mover_sign = 1 if expected_mover == "white" else -1
        if mover_sign * selected_best.score_white != max(
            mover_sign * option.score_white for option in options
        ):
            raise ValueError(f"teacher best is not mover-optimal by exact score: {root_key}")
        labels.append(
            TeacherLabel(
                split=split,
                state_key=root_key,
                position_hash=str(raw["position_hash"]),
                pfen=str(raw["pfen"]),
                series_number=int(raw["series_number"]),
                mover_sign=mover_sign,
                source_profile_id=str(raw["source_profile_id"]),
                teacher_tier=teacher_tier,
                teacher_depth_series=teacher_depth_series,
                teacher_best_series=best_series,
                teacher_score_white=teacher_score,
                teacher_proof=(None if raw.get("teacher_proof") is None else str(raw["teacher_proof"])),
                teacher_signed_mate_distance=teacher_mate_distance,
                options=tuple(options),
            )
        )
    root_overlap = root_keys["train"] & root_keys["holdout"]
    final_overlap = final_keys["train"] & final_keys["holdout"]
    train_final_to_holdout_root = final_keys["train"] & root_keys["holdout"]
    holdout_final_to_train_root = final_keys["holdout"] & root_keys["train"]
    if root_overlap:
        raise ValueError(f"train/holdout root-key leakage: {len(root_overlap)}")
    if final_overlap:
        raise ValueError(f"train/holdout option-final leakage: {len(final_overlap)}")
    if train_final_to_holdout_root:
        raise ValueError(
            "train option-final/holdout root leakage: "
            f"{len(train_final_to_holdout_root)}"
        )
    if holdout_final_to_train_root:
        raise ValueError(
            "holdout option-final/train root leakage: "
            f"{len(holdout_final_to_train_root)}"
        )
    if (
        int(quality.get("train_roots", -1)) != all_counts["train"]
        or int(quality.get("holdout_roots", -1)) != all_counts["holdout"]
    ):
        raise ValueError("teacher corpus split counts differ from quality metadata")
    return tuple(labels), {
        "all_label_counts": all_counts,
        "train_root_keys": len(root_keys["train"]),
        "holdout_root_keys": len(root_keys["holdout"]),
        "root_key_overlap": 0,
        "train_option_final_keys": len(final_keys["train"]),
        "holdout_option_final_keys": len(final_keys["holdout"]),
        "option_final_key_overlap": 0,
        "train_option_final_to_holdout_root_overlap": 0,
        "holdout_option_final_to_train_root_overlap": 0,
        "materialized_split": selected_split,
        "materialized_labels": len(labels),
        "materialized_options": sum(len(label.options) for label in labels),
    }


def _indices(group: str) -> tuple[int, ...]:
    try:
        return FEATURE_GROUPS[group]
    except KeyError as error:
        raise ValueError(f"unknown feature group: {group}") from error


def _selected_features(option: TeacherOption, group: str) -> tuple[int, ...]:
    return tuple(option.features[index] for index in _indices(group))


def _pairwise_rows(
    labels: Sequence[TeacherLabel],
    group: str,
    adverse_pair_weight: float = DEFAULT_ADVERSE_PAIR_WEIGHT,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    adverse_pair_weight = _validate_adverse_pair_weight(adverse_pair_weight)
    rows: list[tuple[int, ...]] = []
    outcomes: list[float] = []
    weights: list[float] = []
    for label in labels:
        rankable = tuple(
            option for option in label.options if option.outcome is None
        )
        comparisons = tuple(
            (rankable[left], rankable[right])
            for left in range(len(rankable))
            for right in range(left + 1, len(rankable))
            if rankable[left].score_white != rankable[right].score_white
        )
        if not comparisons:
            continue
        for left, right in comparisons:
            left_features = _selected_features(left, group)
            right_features = _selected_features(right, group)
            delta = left.score_white - right.score_white
            rows.append(
                tuple(
                    left_value - right_value
                    for left_value, right_value in zip(
                        left_features, right_features, strict=True
                    )
                )
            )
            outcomes.append(1.0 if delta > 0 else -1.0)
            pair_weight = min(1.0, abs(delta) / 1000.0) / len(comparisons)
            if _option_is_mover_adverse(label, left) != _option_is_mover_adverse(
                label, right
            ):
                pair_weight *= adverse_pair_weight
            weights.append(pair_weight)
    if not rows:
        raise ValueError("teacher train split has no nonterminal ranking pairs")
    return (
        np.asarray(rows, dtype=np.float64),
        np.asarray(outcomes, dtype=np.float64),
        np.asarray(weights, dtype=np.float64),
    )


def _fit_pairwise(
    labels: Sequence[TeacherLabel],
    group: str,
    ridge: float,
    adverse_pair_weight: float = DEFAULT_ADVERSE_PAIR_WEIGHT,
) -> tuple[np.ndarray, dict[str, Any]]:
    adverse_pair_weight = _validate_adverse_pair_weight(adverse_pair_weight)
    rows, outcomes, sample_weights = _pairwise_rows(
        labels, group, adverse_pair_weight
    )
    deviations = np.sqrt(np.average(rows * rows, axis=0, weights=sample_weights))
    deviations[deviations < 1e-9] = 1.0
    matrix = rows / deviations
    parameters = np.zeros(matrix.shape[1], dtype=np.float64)
    total_weight = float(sample_weights.sum())
    iterations = 0
    for iteration in range(1, 81):
        margins = np.clip(outcomes * (matrix @ parameters), -30.0, 30.0)
        error_probability = 1.0 / (1.0 + np.exp(margins))
        gradient = -(
            matrix.T @ (sample_weights * outcomes * error_probability)
        ) / total_weight
        gradient += ridge * parameters
        curvature = (
            sample_weights
            * error_probability
            * (1.0 - error_probability)
            / total_weight
        )
        hessian = matrix.T @ (matrix * curvature[:, None])
        hessian += np.eye(matrix.shape[1]) * ridge
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(hessian, gradient, rcond=None)[0]
        before = float(
            np.average(np.logaddexp(0.0, -margins), weights=sample_weights)
            + ridge * np.dot(parameters, parameters) / 2.0
        )
        rate = 1.0
        accepted = None
        while rate >= 1e-6:
            proposal = parameters - rate * step
            proposal_margins = np.clip(
                outcomes * (matrix @ proposal), -30.0, 30.0
            )
            after = float(
                np.average(
                    np.logaddexp(0.0, -proposal_margins),
                    weights=sample_weights,
                )
                + ridge * np.dot(proposal, proposal) / 2.0
            )
            if after <= before + 1e-12:
                accepted = proposal
                break
            rate /= 2.0
        if accepted is None:
            break
        parameters = accepted
        iterations = iteration
        if float(np.max(np.abs(rate * step))) < 1e-8:
            break
    raw = parameters / deviations
    return raw, {
        "group": group,
        "ridge": ridge,
        "pairs": len(outcomes),
        "iterations": iterations,
        "adverse_pair_weight": adverse_pair_weight,
    }


def _terminal_score(label: TeacherLabel, option: TeacherOption) -> int | None:
    if option.outcome == "checkmate":
        winner_sign = label.mover_sign if option.ended_by_check else -label.mover_sign
        distance = abs(option.signed_mate_distance or 1)
        return winner_sign * (MATE_SCORE - min(999, distance))
    if option.outcome in {"stalemate", "ten-series-draw"}:
        return 0
    return None


def _linear_scorer(
    coefficients: Sequence[int | float],
    group: str,
) -> Callable[[TeacherLabel, TeacherOption], float]:
    def score(label: TeacherLabel, option: TeacherOption) -> float:
        terminal = _terminal_score(label, option)
        if terminal is not None:
            return float(terminal * FIXED_POINT_SCALE)
        return float(
            sum(
                coefficient * value
                for coefficient, value in zip(
                    coefficients,
                    _selected_features(option, group),
                    strict=True,
                )
            )
        )

    return score


def _profile_scorer(
    weights: Sequence[int],
) -> Callable[[TeacherLabel, TeacherOption], float]:
    def score(label: TeacherLabel, option: TeacherOption) -> float:
        terminal = _terminal_score(label, option)
        if terminal is not None:
            return float(terminal)
        return float(
            sum(
                round(value * weight / 100)
                for value, weight in zip(
                    option.base_features, weights, strict=True
                )
            )
        )

    return score


def _metric_rows(
    labels: Sequence[TeacherLabel],
    scorer: Callable[[TeacherLabel, TeacherOption], float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label in labels:
        predictions = [scorer(label, option) for option in label.options]
        predicted_utilities = [label.mover_sign * value for value in predictions]
        chosen_index = max(
            range(len(predicted_utilities)),
            key=lambda index: (predicted_utilities[index], -index),
        )
        teacher_utilities = [
            label.mover_sign * option.score_white for option in label.options
        ]
        teacher_best = max(teacher_utilities)
        best_indices = {
            index
            for index, value in enumerate(teacher_utilities)
            if value == teacher_best
        }
        chosen = label.options[chosen_index]
        regret = min(
            5000.0,
            max(0.0, teacher_best - teacher_utilities[chosen_index]),
        )
        pair_correct = 0.0
        pair_weight = 0.0
        for left in range(len(label.options)):
            for right in range(left + 1, len(label.options)):
                teacher_delta = (
                    label.options[left].score_white
                    - label.options[right].score_white
                )
                if teacher_delta == 0:
                    continue
                predicted_delta = predictions[left] - predictions[right]
                gap_weight = min(1.0, abs(teacher_delta) / 1000.0)
                pair_weight += gap_weight
                if teacher_delta * predicted_delta > 0:
                    pair_correct += gap_weight
                elif predicted_delta == 0:
                    pair_correct += gap_weight / 2.0
        chosen_proven_adverse = _option_is_mover_adverse(label, chosen)
        chosen_avoidable_proven_adverse = bool(
            chosen_proven_adverse
            and any(
                not _option_is_mover_adverse(label, option)
                for option in label.options
            )
        )
        rows.append(
            {
                "state_key": label.state_key,
                "mover": "white" if label.mover_sign == 1 else "black",
                "series_number": label.series_number,
                "source_profile_id": label.source_profile_id,
                "teacher_tier": label.teacher_tier,
                "teacher_depth_series": label.teacher_depth_series,
                "tactical": label.tactical,
                "agreement": chosen_index in best_indices,
                "normalized_regret": regret / 5000.0,
                "pair_correct": pair_correct,
                "pair_weight": pair_weight,
                "chosen_series": chosen.series,
                "teacher_best_series": label.teacher_best_series,
                "chosen_proven_adverse": chosen_proven_adverse,
                "chosen_avoidable_proven_adverse": (
                    chosen_avoidable_proven_adverse
                ),
            }
        )
    return rows


def _summarize_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "labels": 0,
            "normalized_regret": None,
            "agreement": None,
            "gap_weighted_pairwise_accuracy": None,
            "chosen_proven_adverse": 0,
            "chosen_avoidable_proven_adverse": 0,
        }
    pair_weight = sum(float(row["pair_weight"]) for row in rows)
    return {
        "labels": len(rows),
        "normalized_regret": sum(float(row["normalized_regret"]) for row in rows) / len(rows),
        "agreement": sum(bool(row["agreement"]) for row in rows) / len(rows),
        "gap_weighted_pairwise_accuracy": (
            sum(float(row["pair_correct"]) for row in rows) / pair_weight
            if pair_weight
            else 1.0
        ),
        "chosen_proven_adverse": sum(bool(row["chosen_proven_adverse"]) for row in rows),
        "chosen_avoidable_proven_adverse": sum(
            bool(row["chosen_avoidable_proven_adverse"]) for row in rows
        ),
    }


def _metrics(
    labels: Sequence[TeacherLabel],
    scorer: Callable[[TeacherLabel, TeacherOption], float],
    *,
    include_rows: bool = False,
) -> dict[str, Any]:
    rows = _metric_rows(labels, scorer)
    strata: dict[str, dict[str, Any]] = {}
    partitions: dict[str, Callable[[Mapping[str, Any]], str]] = {
        "mover": lambda row: str(row["mover"]),
        "series": lambda row: str(row["series_number"]),
        "class": lambda row: "tactical" if row["tactical"] else "quiet",
        "teacher_tier": lambda row: str(row["teacher_tier"]),
        "teacher_depth": lambda row: str(row["teacher_depth_series"]),
        "source_profile": lambda row: str(row["source_profile_id"]),
    }
    for partition, classifier in partitions.items():
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            buckets.setdefault(classifier(row), []).append(row)
        strata[partition] = {
            name: _summarize_rows(bucket)
            for name, bucket in sorted(buckets.items())
        }
    result = {
        "overall": _summarize_rows(rows),
        "strata": strata,
    }
    if include_rows:
        result["rows"] = rows
    return result


def _label_semantic_keys(label: TeacherLabel) -> frozenset[str]:
    return frozenset(
        (label.state_key, *(option.final_state_key for option in label.options))
    )


def _folds(
    labels: Sequence[TeacherLabel], count: int = 5
) -> tuple[tuple[TeacherLabel, ...], ...]:
    # Root-disjoint folds are insufficient when two teacher roots transpose to
    # the same retained option state.  Union every label connected by either a
    # root or option-final semantic key, then keep the entire component in one
    # fold.
    parent = list(range(len(labels)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    owner_by_key: dict[str, int] = {}
    for index, label in enumerate(labels):
        for key in _label_semantic_keys(label):
            owner = owner_by_key.setdefault(key, index)
            union(index, owner)
    components: dict[int, list[TeacherLabel]] = {}
    for index, label in enumerate(labels):
        components.setdefault(find(index), []).append(label)
    grouped = list(components.values())
    if len(grouped) < 2:
        raise ValueError(
            "teacher train split has fewer than two semantic-state components"
        )

    def component_key(component: Sequence[TeacherLabel]) -> tuple[Any, ...]:
        keys = sorted(
            key for label in component for key in _label_semantic_keys(label)
        )
        digest = hashlib.sha256(
            ("cycle3-cv-component|" + "|".join(keys)).encode()
        ).digest()
        return (-len(component), digest, tuple(label.state_key for label in component))

    buckets: list[list[TeacherLabel]] = [
        [] for _ in range(min(count, len(grouped)))
    ]
    for component in sorted(grouped, key=component_key):
        bucket_index = min(
            range(len(buckets)), key=lambda index: (len(buckets[index]), index)
        )
        buckets[bucket_index].extend(component)
    return tuple(
        tuple(sorted(bucket, key=lambda label: label.state_key))
        for bucket in buckets
    )


def _cross_validate(
    labels: Sequence[TeacherLabel],
    group: str,
    ridge: float,
    adverse_pair_weight: float = DEFAULT_ADVERSE_PAIR_WEIGHT,
) -> dict[str, Any]:
    adverse_pair_weight = _validate_adverse_pair_weight(adverse_pair_weight)
    folds = _folds(labels)
    all_rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    for index, validation in enumerate(folds):
        validation_keys = {label.state_key for label in validation}
        training = tuple(
            label for label in labels if label.state_key not in validation_keys
        )
        validation_semantic_keys = {
            key for label in validation for key in _label_semantic_keys(label)
        }
        training_semantic_keys = {
            key for label in training for key in _label_semantic_keys(label)
        }
        semantic_overlap = validation_semantic_keys & training_semantic_keys
        if semantic_overlap:
            raise ValueError(
                f"cross-validation semantic-state leakage: {len(semantic_overlap)}"
            )
        coefficients, fit = _fit_pairwise(
            training, group, ridge, adverse_pair_weight
        )
        rows = _metric_rows(validation, _linear_scorer(coefficients, group))
        all_rows.extend(rows)
        fold_rows.append(
            {
                "fold": index,
                "train_labels": len(training),
                "validation_labels": len(validation),
                "train_semantic_keys": len(training_semantic_keys),
                "validation_semantic_keys": len(validation_semantic_keys),
                "semantic_key_overlap": 0,
                "fit": fit,
                "metrics": _summarize_rows(rows),
            }
        )
    return {
        "group": group,
        "ridge": ridge,
        "adverse_pair_weight": adverse_pair_weight,
        "out_of_fold": _summarize_rows(all_rows),
        "folds": fold_rows,
    }


def _metric_objective(metrics: Mapping[str, Any]) -> tuple[int, float, float, float]:
    return (
        int(metrics["chosen_avoidable_proven_adverse"]),
        float(metrics["normalized_regret"]),
        -float(metrics["gap_weighted_pairwise_accuracy"]),
        -float(metrics["agreement"]),
    )


def _selection_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        *_metric_objective(row["out_of_fold"]),
        len(_indices(str(row["group"]))),
        row["ridge"],
    )


def _quantize(coefficients: np.ndarray) -> tuple[int, ...]:
    maximum = float(np.max(np.abs(coefficients)))
    if maximum <= 0.0 or not math.isfinite(maximum):
        raise ValueError("fitted coefficient vector is empty or nonfinite")
    normalized = coefficients / maximum
    return tuple(int(math.copysign(math.floor(abs(value) * FIXED_POINT_SCALE + 0.5), value)) for value in normalized)


def _model_payload(
    *,
    group: str,
    ridge: float,
    coefficients: Sequence[int],
    adverse_pair_weight: float,
    corpus_id: str,
    corpus_sha256: str,
) -> dict[str, Any]:
    feature_names = [
        TEACHER_VALUE_FEATURE_NAMES[index] for index in _indices(group)
    ]
    core = {
        "schema": MODEL_SCHEMA,
        "feature_schema": TEACHER_VALUE_FEATURE_SCHEMA,
        "feature_group": group,
        "feature_names": feature_names,
        "fixed_point_scale": FIXED_POINT_SCALE,
        "coefficients": list(coefficients),
        "ridge": ridge,
        "adverse_pair_weight": _validate_adverse_pair_weight(
            adverse_pair_weight
        ),
        "terminal_override": "replayed terminal checkmate and draw outcomes are authoritative",
        "teacher_corpus_id": corpus_id,
        "teacher_corpus_sha256": corpus_sha256,
    }
    return {
        **core,
        "model_id": "spc-dtv-" + hashlib.sha256(_canonical_json(core)).hexdigest()[:20],
    }


def _load_model(path: Path) -> dict[str, Any]:
    model = _load_json(path)
    supplied = str(model.get("model_id", ""))
    core = {key: value for key, value in model.items() if key != "model_id"}
    expected = "spc-dtv-" + hashlib.sha256(_canonical_json(core)).hexdigest()[:20]
    if supplied != expected:
        raise ValueError(f"model_id mismatch: {path}")
    if model.get("schema") != MODEL_SCHEMA:
        raise ValueError(f"unsupported model schema: {path}")
    try:
        _validate_adverse_pair_weight(float(model["adverse_pair_weight"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"model adverse pair weight is invalid: {path}") from error
    group = str(model["feature_group"])
    expected_names = [
        TEACHER_VALUE_FEATURE_NAMES[index] for index in _indices(group)
    ]
    if model["feature_names"] != expected_names:
        raise ValueError(f"model feature order mismatch: {path}")
    return model


def _coordinate_profile(
    labels: Sequence[TeacherLabel],
    starts: Sequence[Sequence[int]],
) -> tuple[int, ...]:
    def objective(weights: Sequence[int]) -> tuple[int, float, float, float]:
        metrics = _metrics(labels, _profile_scorer(weights))["overall"]
        return _metric_objective(metrics)

    best: tuple[int, ...] | None = None
    best_value: tuple[int, float, float, float] | None = None
    for requested in starts:
        current = tuple(max(25, min(300, int(value))) for value in requested)
        current_value = objective(current)
        for step in (50, 25, 12, 6, 3, 1):
            changed = True
            while changed:
                changed = False
                for index in range(len(current)):
                    proposals: list[
                        tuple[tuple[int, float, float, float], tuple[int, ...]]
                    ] = []
                    for direction in (-1, 1):
                        proposal = list(current)
                        proposal[index] = max(
                            25,
                            min(300, proposal[index] + direction * step),
                        )
                        candidate = tuple(proposal)
                        proposals.append((objective(candidate), candidate))
                    proposal_value, proposal = min(proposals)
                    if proposal_value < current_value:
                        current_value, current = proposal_value, proposal
                        changed = True
        if best_value is None or (current_value, current) < (best_value, best or current):
            best_value, best = current_value, current
    assert best is not None
    return best


def _profile_payload(
    weights: Sequence[int],
    leader: EngineProfile,
) -> dict[str, Any]:
    profile = EngineProfile(
        name="cycle 3 deep-teacher distilled profile",
        weights=EvaluationWeights(
            **dict(zip(FEATURE_NAMES, (int(value) for value in weights), strict=True))
        ),
        recommended_depth=2,
        recommended_branch_cap=32,
        generation=max(3, leader.generation + 1),
        parent_profile_ids=(leader.profile_id,),
        notes=(
            "Train-only deep-teacher ranking distillation; independent holdout "
            "and paired match gates are mandatory before promotion."
        ),
    )
    return profile.as_dict()


def _fit_command(args: argparse.Namespace) -> None:
    corpus_path = args.teacher_corpus.expanduser().resolve()
    leader_path = args.leader_profile.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.exists() and (
        not output.is_dir() or any(output.iterdir())
    ):
        raise FileExistsError(
            "fit output is not empty; use a fresh directory so frozen model "
            "evidence cannot be overwritten"
        )
    corpus = _load_json(corpus_path)
    _reject_quarantined_holdout(corpus)
    corpus_sha = _sha256(corpus_path)
    corpus_id = str(corpus["corpus_id"])
    adverse_pair_weight = _validate_adverse_pair_weight(
        getattr(args, "adverse_pair_weight", DEFAULT_ADVERSE_PAIR_WEIGHT)
    )
    train, leakage = _materialize_labels(corpus, selected_split="train")
    if not train:
        raise ValueError("teacher corpus has no train labels")
    leader = load_profile(leader_path)

    cv_rows = [
        _cross_validate(train, group, ridge, adverse_pair_weight)
        for group in (*NONROUTE_GROUPS, "all47")
        for ridge in RIDGES
    ]
    nonroute_selection = min(
        (row for row in cv_rows if row["group"] in NONROUTE_GROUPS),
        key=_selection_key,
    )
    route_selection = min(
        (row for row in cv_rows if row["group"] == "all47"),
        key=_selection_key,
    )
    models: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for role, selection in (
        ("primary_nonroute", nonroute_selection),
        ("route_ablation", route_selection),
    ):
        raw, fit = _fit_pairwise(
            train,
            str(selection["group"]),
            float(selection["ridge"]),
            adverse_pair_weight,
        )
        quantized = _quantize(raw)
        model = _model_payload(
            group=str(selection["group"]),
            ridge=float(selection["ridge"]),
            coefficients=quantized,
            adverse_pair_weight=adverse_pair_weight,
            corpus_id=corpus_id,
            corpus_sha256=corpus_sha,
        )
        model_path = output / f"{role}-{model['model_id']}.json"
        _atomic_json(model_path, model)
        metrics = _metrics(
            train,
            _linear_scorer(quantized, str(selection["group"])),
        )
        models.append(
            (
                role,
                model,
                {
                    "path": str(model_path),
                    "sha256": _sha256(model_path),
                    "fit": fit,
                    "cross_validation": selection,
                    "train_metrics": metrics,
                },
            )
        )

    # The matchable seven-weight surface is distilled separately.  The richer
    # model never silently changes the native/browser evaluator.
    projected_raw, _ = _fit_pairwise(
        train, "base7", 0.01, adverse_pair_weight
    )
    median = float(np.median(np.abs(projected_raw)))
    projected = tuple(
        max(25, min(300, round(abs(value) * 100.0 / max(1e-9, median))))
        for value in projected_raw
    )
    profile_weights = _coordinate_profile(
        train,
        (
            BASELINE_WEIGHTS,
            tuple(int(getattr(leader.weights, name)) for name in FEATURE_NAMES),
            DEVELOPMENT_PROFILE_WEIGHTS,
            projected,
        ),
    )
    profile = _profile_payload(profile_weights, leader)
    profile_path = output / f"teacher-distilled-{profile['profile_id']}.json"
    _atomic_json(profile_path, profile)

    receipt = {
        "schema": FIT_RECEIPT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "claim_scope": (
            "train-only isolated evaluation experiment; holdout labels are not "
            "scored or used for selection; no live evaluator or release changed"
        ),
        "inputs": {
            "teacher_corpus": str(corpus_path),
            "teacher_corpus_id": corpus_id,
            "teacher_corpus_sha256": corpus_sha,
            "leader_profile": str(leader_path),
            "leader_profile_id": leader.profile_id,
            "leader_profile_sha256": _sha256(leader_path),
        },
        "leakage_audit": leakage,
        "feature_contract": {
            "schema": TEACHER_VALUE_FEATURE_SCHEMA,
            "feature_names": list(TEACHER_VALUE_FEATURE_NAMES),
            "feature_module": str(Path(sys.modules[TeacherValueFeaturesV3.__module__].__file__).resolve()),
            "feature_module_sha256": _sha256(Path(sys.modules[TeacherValueFeaturesV3.__module__].__file__).resolve()),
            "expensive_two_move_route_indices": [44, 45, 46],
            "primary_candidate_excludes_expensive_routes": True,
        },
        "selection": {
            "method": (
                "five-fold semantic-component-disjoint all-nonterminal-pairs "
                "proof-contrast-weighted ridge ranking"
            ),
            "ridges": list(RIDGES),
            "adverse_pair_weight": adverse_pair_weight,
            "adverse_pair_rule": (
                "multiply a pair's deterministic gap weight when exactly one "
                "option has a proof for the mover's opponent"
            ),
            "primary_objective": (
                "raw avoidable proven-adverse selections, normalized regret, "
                "pairwise accuracy, agreement"
            ),
            "raw_option_argmax_metrics": True,
            "rows": cv_rows,
        },
        "models": {
            role: {
                "model_id": model["model_id"],
                **evidence,
            }
            for role, model, evidence in models
        },
        "profile": {
            "profile_id": profile["profile_id"],
            "weights": list(profile_weights),
            "path": str(profile_path),
            "sha256": _sha256(profile_path),
            "train_metrics": _metrics(train, _profile_scorer(profile_weights)),
        },
        "references_train": {
            "baseline": _metrics(train, _profile_scorer(BASELINE_WEIGHTS)),
            "rejected_leader": _metrics(
                train,
                _profile_scorer(
                    tuple(int(getattr(leader.weights, name)) for name in FEATURE_NAMES)
                ),
            ),
        },
        "runtime": {
            "python": sys.version,
            "numpy": np.__version__,
            "argv": list(sys.argv),
            "script_sha256": _sha256(Path(__file__).resolve()),
            "implementation_sha256": _implementation_hashes(),
        },
    }
    receipt_path = output / "deep-teacher-fit-receipt.json"
    _atomic_json(receipt_path, receipt)
    print(
        json.dumps(
            {
                "receipt": str(receipt_path),
                "primary_model": receipt["models"]["primary_nonroute"],
                "route_ablation": receipt["models"]["route_ablation"],
                "profile": receipt["profile"],
            },
            indent=2,
            sort_keys=True,
        )
    )


def _holdout_gate(
    candidate: Mapping[str, Any],
    route: Mapping[str, Any],
    profile: Mapping[str, Any],
    baseline: Mapping[str, Any],
    leader: Mapping[str, Any],
) -> dict[str, Any]:
    def stratum_regret_no_worse(
        metrics: Mapping[str, Any], partition: str, stratum: str
    ) -> bool:
        candidate_row = metrics["strata"][partition].get(stratum)
        baseline_row = baseline["strata"][partition].get(stratum)
        return bool(
            candidate_row is not None
            and baseline_row is not None
            and candidate_row["normalized_regret"]
            <= baseline_row["normalized_regret"]
        )

    candidate_overall = candidate["overall"]
    route_overall = route["overall"]
    reference_rows = [baseline["overall"], leader["overall"]]
    best_regret = min(row["normalized_regret"] for row in reference_rows)
    best_pairwise = max(
        row["gap_weighted_pairwise_accuracy"] for row in reference_rows
    )
    candidate_gate = {
        "lower_regret_than_both_references": candidate_overall["normalized_regret"] < best_regret,
        "higher_pairwise_accuracy_than_both_references": candidate_overall["gap_weighted_pairwise_accuracy"] > best_pairwise,
        "no_proven_adverse_selection": candidate_overall["chosen_proven_adverse"] == 0,
        "no_avoidable_proven_adverse_selection": candidate_overall[
            "chosen_avoidable_proven_adverse"
        ]
        == 0,
        "white_regret_no_worse_than_baseline": stratum_regret_no_worse(
            candidate, "mover", "white"
        ),
        "black_regret_no_worse_than_baseline": stratum_regret_no_worse(
            candidate, "mover", "black"
        ),
        "quiet_regret_no_worse_than_baseline": stratum_regret_no_worse(
            candidate, "teacher_tier", "quiet_d2"
        ),
        "tactical_regret_no_worse_than_baseline": stratum_regret_no_worse(
            candidate, "teacher_tier", "tactical_d3"
        ),
    }
    candidate_gate["passed"] = all(candidate_gate.values())
    route_gate = {
        "lower_regret_than_nonroute": route_overall["normalized_regret"] < candidate_overall["normalized_regret"],
        "higher_pairwise_accuracy_than_nonroute": route_overall["gap_weighted_pairwise_accuracy"] > candidate_overall["gap_weighted_pairwise_accuracy"],
        "no_proven_adverse_selection": route_overall["chosen_proven_adverse"] == 0,
        "no_avoidable_proven_adverse_selection": route_overall[
            "chosen_avoidable_proven_adverse"
        ]
        == 0,
    }
    route_gate["passed"] = all(route_gate.values())
    profile_overall = profile["overall"]
    profile_gate = {
        "lower_regret_than_both_references": profile_overall["normalized_regret"] < best_regret,
        "higher_pairwise_accuracy_than_both_references": profile_overall["gap_weighted_pairwise_accuracy"] > best_pairwise,
        "no_proven_adverse_selection": profile_overall["chosen_proven_adverse"] == 0,
        "no_avoidable_proven_adverse_selection": profile_overall[
            "chosen_avoidable_proven_adverse"
        ]
        == 0,
        "white_regret_no_worse_than_baseline": stratum_regret_no_worse(
            profile, "mover", "white"
        ),
        "black_regret_no_worse_than_baseline": stratum_regret_no_worse(
            profile, "mover", "black"
        ),
        "quiet_regret_no_worse_than_baseline": stratum_regret_no_worse(
            profile, "teacher_tier", "quiet_d2"
        ),
        "tactical_regret_no_worse_than_baseline": stratum_regret_no_worse(
            profile, "teacher_tier", "tactical_d3"
        ),
    }
    profile_gate["passed"] = all(profile_gate.values())
    return {
        "primary_nonroute": candidate_gate,
        "route_ablation": route_gate,
        "distilled_profile": profile_gate,
    }


def _evaluate_holdout_command(args: argparse.Namespace) -> None:
    corpus_path = args.teacher_corpus.expanduser().resolve()
    leader_path = args.leader_profile.expanduser().resolve()
    fit_receipt_path = args.fit_receipt.expanduser().resolve()
    output = args.output.expanduser().resolve()
    receipt_path = output / "deep-teacher-holdout-receipt.json"
    if receipt_path.exists():
        raise FileExistsError(
            "holdout receipt already exists; the one-shot holdout command refuses "
            "to overwrite or rerun selection evidence"
        )
    corpus = _load_json(corpus_path)
    _reject_quarantined_holdout(corpus)
    fit_receipt = _load_json(fit_receipt_path)
    corpus_sha = _sha256(corpus_path)
    if fit_receipt.get("schema") != FIT_RECEIPT_SCHEMA:
        raise ValueError("fit receipt schema mismatch")
    if fit_receipt["inputs"]["teacher_corpus_sha256"] != corpus_sha:
        raise ValueError("teacher corpus changed after fitting")
    if fit_receipt["runtime"]["script_sha256"] != _sha256(Path(__file__).resolve()):
        raise ValueError("trainer/evaluator script changed after fitting")
    if fit_receipt["runtime"].get("implementation_sha256") != _implementation_hashes():
        raise ValueError("teacher evaluator implementation changed after fitting")
    feature_module = Path(
        sys.modules[TeacherValueFeaturesV3.__module__].__file__
    ).resolve()
    if fit_receipt["feature_contract"]["feature_module_sha256"] != _sha256(
        feature_module
    ):
        raise ValueError("teacher-value feature implementation changed after fitting")
    holdout_claim_path = fit_receipt_path.with_name(
        "holdout-evaluation-claim.json"
    )
    holdout_claim = {
        "schema": "spc-deep-teacher-holdout-claim-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "teacher_corpus_sha256": corpus_sha,
        "fit_receipt_sha256": _sha256(fit_receipt_path),
        "script_sha256": _sha256(Path(__file__).resolve()),
        "requested_receipt": str(receipt_path),
    }
    _exclusive_json(holdout_claim_path, holdout_claim)
    holdout, leakage = _materialize_labels(corpus, selected_split="holdout")
    if not holdout:
        raise ValueError("teacher corpus has no holdout labels")
    leader = load_profile(leader_path)
    if fit_receipt["inputs"]["leader_profile_sha256"] != _sha256(leader_path):
        raise ValueError("rejected leader profile changed after fitting")

    models: dict[str, dict[str, Any]] = {}
    model_metrics: dict[str, dict[str, Any]] = {}
    for role in ("primary_nonroute", "route_ablation"):
        model_path = Path(fit_receipt["models"][role]["path"])
        if _sha256(model_path) != fit_receipt["models"][role]["sha256"]:
            raise ValueError(f"frozen model changed: {role}")
        model = _load_model(model_path)
        if model["teacher_corpus_sha256"] != corpus_sha:
            raise ValueError(f"model corpus binding differs: {role}")
        models[role] = model
        model_metrics[role] = _metrics(
            holdout,
            _linear_scorer(
                tuple(int(value) for value in model["coefficients"]),
                str(model["feature_group"]),
            ),
            include_rows=True,
        )

    profile_path = Path(fit_receipt["profile"]["path"])
    if _sha256(profile_path) != fit_receipt["profile"]["sha256"]:
        raise ValueError("frozen distilled profile changed")
    profile = load_profile(profile_path)
    profile_weights = tuple(
        int(getattr(profile.weights, name)) for name in FEATURE_NAMES
    )
    baseline = baseline_profile()
    baseline_weights = tuple(
        int(getattr(baseline.weights, name)) for name in FEATURE_NAMES
    )
    leader_weights = tuple(
        int(getattr(leader.weights, name)) for name in FEATURE_NAMES
    )
    references = {
        "baseline": _metrics(holdout, _profile_scorer(baseline_weights)),
        "rejected_leader": _metrics(holdout, _profile_scorer(leader_weights)),
    }
    profile_metrics = _metrics(
        holdout,
        _profile_scorer(profile_weights),
        include_rows=True,
    )
    gates = _holdout_gate(
        model_metrics["primary_nonroute"],
        model_metrics["route_ablation"],
        profile_metrics,
        references["baseline"],
        references["rejected_leader"],
    )
    corpus_contract = corpus["contract"]
    corpus_promotion_eligible = bool(
        corpus_contract.get("promotion_eligible", True)
        and not corpus_contract.get("exploratory_only", False)
    )
    receipt = {
        "schema": HOLDOUT_RECEIPT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "claim_scope": (
            "one-shot state-disjoint teacher holdout ranking evidence; no game "
            "strength, Elo, live evaluator, or release claim"
        ),
        "inputs": {
            "teacher_corpus": str(corpus_path),
            "teacher_corpus_id": corpus["corpus_id"],
            "teacher_corpus_sha256": corpus_sha,
            "fit_receipt": str(fit_receipt_path),
            "fit_receipt_sha256": _sha256(fit_receipt_path),
            "holdout_claim": str(holdout_claim_path),
            "holdout_claim_sha256": _sha256(holdout_claim_path),
            "leader_profile": str(leader_path),
            "leader_profile_id": leader.profile_id,
            "leader_profile_sha256": _sha256(leader_path),
        },
        "leakage_audit": leakage,
        "models": {
            role: {
                "model_id": models[role]["model_id"],
                "feature_group": models[role]["feature_group"],
                "metrics": model_metrics[role],
            }
            for role in models
        },
        "profile": {
            "profile_id": profile.profile_id,
            "weights": list(profile_weights),
            "metrics": profile_metrics,
        },
        "references": references,
        "gates": gates,
        "corpus_promotion_contract": {
            "exploratory_only": bool(
                corpus_contract.get("exploratory_only", False)
            ),
            "promotion_eligible": corpus_promotion_eligible,
            "promotion_ineligible_reasons": list(
                corpus_contract.get("promotion_ineligible_reasons", ())
            ),
            "missing_positional_series": list(
                corpus_contract.get("missing_positional_series", ())
            ),
        },
        "promotion_recommendation": bool(
            corpus_promotion_eligible
            and gates["primary_nonroute"]["passed"]
            and gates["distilled_profile"]["passed"]
        ),
        "route_features_deserve_live_consideration": bool(
            gates["route_ablation"]["passed"]
        ),
        "runtime": {
            "python": sys.version,
            "numpy": np.__version__,
            "argv": list(sys.argv),
            "script_sha256": _sha256(Path(__file__).resolve()),
            "implementation_sha256": _implementation_hashes(),
        },
    }
    _atomic_json(receipt_path, receipt)
    print(
        json.dumps(
            {
                "receipt": str(receipt_path),
                "gates": gates,
                "promotion_recommendation": receipt["promotion_recommendation"],
                "corpus_promotion_contract": receipt[
                    "corpus_promotion_contract"
                ],
                "route_features_deserve_live_consideration": receipt[
                    "route_features_deserve_live_consideration"
                ],
                "primary_holdout": model_metrics["primary_nonroute"]["overall"],
                "profile_holdout": profile_metrics["overall"],
                "references": {
                    name: metrics["overall"]
                    for name, metrics in references.items()
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fit a train-only deep-teacher value candidate, then evaluate its "
            "frozen artifacts with a separate one-shot holdout command."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    fit = commands.add_parser("fit")
    fit.add_argument("teacher_corpus", type=Path)
    fit.add_argument("leader_profile", type=Path)
    fit.add_argument("output", type=Path)
    fit.add_argument(
        "--adverse-pair-weight",
        type=float,
        default=DEFAULT_ADVERSE_PAIR_WEIGHT,
        help=(
            "Pairwise weight multiplier when exactly one option is proven "
            "adverse to the mover (default: %(default)s)."
        ),
    )
    fit.set_defaults(handler=_fit_command)
    holdout = commands.add_parser("evaluate-holdout")
    holdout.add_argument("teacher_corpus", type=Path)
    holdout.add_argument("leader_profile", type=Path)
    holdout.add_argument("fit_receipt", type=Path)
    holdout.add_argument("output", type=Path)
    holdout.set_defaults(handler=_evaluate_holdout_command)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
