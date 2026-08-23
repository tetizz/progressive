from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterable, Mapping, Protocol, Sequence

import chess

from .fast_training import CachedFeatures, FEATURE_NAMES, default_training_positions
from .league import OPENING_SUITE, OPENING_SUITE_VERSION, OpeningCase
from .model import ENGINE_SOURCE_FINGERPRINT, ENGINE_VERSION, Outcome, ProgressiveState
from .profiles import EngineProfile, EvaluationWeights
from .rules import SeriesLegalityError, play_series


SELFPLAY_CORPUS_SCHEMA = 1
SELFPLAY_CORPUS_METHOD = "replayed-league-value-corpus-v1"
SELFPLAY_TUNER_METHOD = "deterministic-texel-coordinate-v1"


class ValueTrainingSample(Protocol):
    """Minimal loss surface shared by league and native value corpora."""

    features: CachedFeatures
    target_white_score: float
    sample_weight: float


class ValueTrainingCorpus(Protocol):
    @property
    def corpus_id(self) -> str: ...

    @property
    def train_samples(self) -> Sequence[ValueTrainingSample]: ...

    @property
    def holdout_samples(self) -> Sequence[ValueTrainingSample]: ...


@dataclass(frozen=True, slots=True)
class _WeightedValuePoint:
    features: CachedFeatures
    target_white_score: float
    sample_weight: float


def _collapse_loss_equivalent_samples(
    samples: Sequence[ValueTrainingSample],
) -> tuple[_WeightedValuePoint, ...]:
    """Collapse equal seven-feature vectors without changing weighted loss."""

    buckets: dict[tuple[int, ...], list[Any]] = {}
    for sample in samples:
        weight = float(sample.sample_weight)
        target = float(sample.target_white_score)
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError("value-training sample weights must be positive and finite")
        if not math.isfinite(target) or not 0.0 <= target <= 1.0:
            raise ValueError("value-training targets must be finite values in [0, 1]")
        key = tuple(getattr(sample.features, name) for name in FEATURE_NAMES)
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = [sample.features, target * weight, weight]
        else:
            bucket[1] += target * weight
            bucket[2] += weight
    return tuple(
        _WeightedValuePoint(
            features=values[0],
            target_white_score=values[1] / values[2],
            sample_weight=values[2],
        )
        for _, values in sorted(buckets.items())
    )


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(prefix: str, payload: Mapping[str, Any]) -> str:
    return prefix + hashlib.sha256(_canonical_json(payload)).hexdigest()[:20]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_selfplay_artifact(
    payload: Mapping[str, Any], destination: str | Path
) -> Path:
    """Atomically writes one deterministic corpus/tuning JSON artifact."""

    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
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


def _split_bucket(seed: int, component_id: str) -> int:
    encoded = f"{seed}|{component_id}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:4], "big") % 100


def _opening_line_families() -> dict[str, str]:
    return {
        position.case_id: position.trace_id or position.case_id
        for position in default_training_positions()
        if not position.tactical_anchor
    }


@dataclass(frozen=True, slots=True)
class SelfPlaySample:
    position_hash: str
    pfen: str
    run_id: str
    game_key: str
    opening_case_id: str
    line_family: str
    split_component: str
    split: str
    series_number: int
    mover: str
    profile_id: str
    chosen_series: str
    result: str
    target_white_score: float
    sample_weight: float
    features: CachedFeatures

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "features": self.features.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class SelfPlayCorpus:
    seed: int
    holdout_percent: int
    database_evidence: tuple[Mapping[str, Any], ...]
    completed_games: int
    excluded_games: int
    samples: tuple[SelfPlaySample, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.holdout_percent <= 50:
            raise ValueError("holdout_percent must be between 0 and 50")
        if self.completed_games < 1:
            raise ValueError("self-play corpus requires at least one completed game")
        if not self.samples:
            raise ValueError("self-play corpus requires at least one replayed sample")

    @property
    def corpus_id(self) -> str:
        return _digest("spc-selfplay-corpus-", self.deterministic_payload())

    @property
    def train_samples(self) -> tuple[SelfPlaySample, ...]:
        return tuple(sample for sample in self.samples if sample.split == "train")

    @property
    def holdout_samples(self) -> tuple[SelfPlaySample, ...]:
        return tuple(sample for sample in self.samples if sample.split == "holdout")

    def deterministic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SELFPLAY_CORPUS_SCHEMA,
            "method": SELFPLAY_CORPUS_METHOD,
            "engine_version": ENGINE_VERSION,
            "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
            "seed": self.seed,
            "holdout_percent": self.holdout_percent,
            "database_evidence": [dict(item) for item in self.database_evidence],
            "completed_games": self.completed_games,
            "excluded_games": self.excluded_games,
            "samples": [sample.as_dict() for sample in self.samples],
        }

    def as_dict(self) -> dict[str, Any]:
        games_by_result: dict[str, set[str]] = {}
        for sample in self.samples:
            games_by_result.setdefault(sample.result, set()).add(sample.game_key)
        return {
            **self.deterministic_payload(),
            "corpus_id": self.corpus_id,
            "summary": {
                "samples": len(self.samples),
                "train_samples": len(self.train_samples),
                "holdout_samples": len(self.holdout_samples),
                "games_by_result": {
                    result: len(game_keys)
                    for result, game_keys in sorted(games_by_result.items())
                },
                "label_contract": (
                    "eventual legal checkmate or rules-proven ten-series draw; "
                    "manual and technical results excluded"
                ),
                "weight_contract": "each completed game contributes total weight 1",
            },
        }


@dataclass(frozen=True, slots=True)
class _ReplayedGame:
    run_id: str
    game_key: str
    opening_case_id: str
    line_family: str
    result: str
    target_white_score: float
    states: tuple[ProgressiveState, ...]
    profile_ids: tuple[str, ...]
    chosen_series: tuple[str, ...]


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


def _opening_from_payload(payload: Mapping[str, Any]) -> OpeningCase:
    try:
        ep_targets = tuple(str(value) for value in payload.get("ep_targets", ()))
        return OpeningCase(
            case_id=str(payload["case_id"]),
            fen=str(payload["fen"]),
            series_number=int(payload["series_number"]),
            quiet_series=int(payload.get("quiet_series", 0)),
            ep_targets=ep_targets,
            source=str(payload.get("source", "persisted league boundary")),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid persisted opening payload: {error}") from error


def _result_target(result: str) -> float:
    if result == "1-0":
        return 1.0
    if result == "0-1":
        return 0.0
    if result == "1/2-1/2":
        return 0.5
    raise ValueError(f"unsupported self-play result {result!r}")


def _replay_game(
    row: Mapping[str, Any], line_families: Mapping[str, str]
) -> _ReplayedGame:
    try:
        opening_payload = json.loads(str(row["opening_json"]))
        trace_payload = json.loads(str(row["trace_json"]))
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid persisted game JSON: {error}") from error
    if not isinstance(opening_payload, Mapping) or not isinstance(trace_payload, list):
        raise ValueError("persisted opening/trace JSON has the wrong shape")

    opening = _opening_from_payload(opening_payload)
    persisted_case_id = str(row["opening_case_id"])
    if opening.case_id != persisted_case_id:
        raise ValueError(
            f"game {row['job_key']} opening case id does not match its row"
        )
    suite_version = str(row["opening_suite_version"])
    if suite_version == OPENING_SUITE_VERSION:
        canonical_by_id = {case.case_id: case for case in OPENING_SUITE}
        canonical = canonical_by_id.get(opening.case_id)
        if canonical is None:
            raise ValueError(
                f"game {row['job_key']} names an unknown canonical opening"
            )
        if opening.state().pfen != canonical.state().pfen:
            raise ValueError(
                f"game {row['job_key']} canonical opening boundary diverges"
            )
    state = opening.state()
    if state.pfen != str(row["start_pfen"]):
        raise ValueError(f"game {row['job_key']} start PFEN does not replay")
    expected_profiles = {
        chess.WHITE: str(row["white_profile_id"]),
        chess.BLACK: str(row["black_profile_id"]),
    }
    states: list[ProgressiveState] = []
    profile_ids: list[str] = []
    chosen_series: list[str] = []
    last_outcome: Outcome | None = None

    for index, item in enumerate(trace_payload):
        if not isinstance(item, Mapping):
            raise ValueError(f"game {row['job_key']} trace item {index} is not an object")
        if not bool(item.get("played", True)):
            raise ValueError(
                f"conclusive game {row['job_key']} contains an unplayed trace item"
            )
        if int(item["series_number"]) != state.series_number:
            raise ValueError(
                f"game {row['job_key']} series number diverges at trace {index}"
            )
        expected_profile = expected_profiles[state.board.turn]
        if str(item["profile_id"]) != expected_profile:
            raise ValueError(
                f"game {row['job_key']} mover profile diverges at trace {index}"
            )
        notation = str(item["series"])
        moves = tuple(value for value in notation.split("/") if value)
        if not moves:
            raise ValueError(f"game {row['job_key']} contains an empty played series")
        states.append(state)
        profile_ids.append(expected_profile)
        chosen_series.append(notation)
        try:
            result = play_series(state, moves)
        except SeriesLegalityError as error:
            raise ValueError(
                f"game {row['job_key']} contains an illegal series at trace {index}: {error}"
            ) from error
        if result.machine_notation != notation:
            raise ValueError(
                f"game {row['job_key']} series notation is not canonical at trace {index}"
            )
        recorded_outcome = item.get("outcome")
        actual_outcome = result.outcome.value if result.outcome is not None else None
        if recorded_outcome != actual_outcome:
            raise ValueError(
                f"game {row['job_key']} outcome diverges at trace {index}: "
                f"{recorded_outcome!r} != {actual_outcome!r}"
            )
        last_outcome = result.outcome
        state = result.final_state

    if len(states) != int(row["series_played"]):
        raise ValueError(f"game {row['job_key']} played-series count does not match")
    if state.pfen != str(row["final_pfen"]):
        raise ValueError(f"game {row['job_key']} final PFEN does not replay")
    terminal_reason = str(row["terminal_reason"])
    if terminal_reason == "checkmate":
        if last_outcome != Outcome.CHECKMATE:
            raise ValueError(f"game {row['job_key']} is labeled mate without mate")
        winner = not state.board.turn
        expected_result = "1-0" if winner == chess.WHITE else "0-1"
        if str(row["result"]) != expected_result:
            raise ValueError(f"game {row['job_key']} checkmate winner does not match")
    elif terminal_reason == "ten-series-draw":
        if last_outcome != Outcome.TEN_SERIES_DRAW or str(row["result"]) != "1/2-1/2":
            raise ValueError(f"game {row['job_key']} draw outcome does not match")
    else:
        raise ValueError(
            f"game {row['job_key']} is not valid value-training evidence: {terminal_reason}"
        )

    line_family = line_families.get(
        opening.case_id,
        f"{suite_version}:{opening.case_id}",
    )
    return _ReplayedGame(
        run_id=str(row["run_id"]),
        game_key=str(row["job_key"]),
        opening_case_id=opening.case_id,
        line_family=line_family,
        result=str(row["result"]),
        target_white_score=_result_target(str(row["result"])),
        states=tuple(states),
        profile_ids=tuple(profile_ids),
        chosen_series=tuple(chosen_series),
    )


def _read_database(path: Path) -> tuple[dict[str, Any], tuple[Mapping[str, Any], ...], int]:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"self-play database does not exist: {source}")
    connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        runs = connection.execute(
            "select run_id, status, engine_version, source_fingerprint "
            "from runs where status = 'complete' order by run_id"
        ).fetchall()
        if not runs:
            raise ValueError(f"self-play database has no completed run: {source.name}")
        run_ids = tuple(str(row["run_id"]) for row in runs)
        placeholders = ",".join("?" for _ in run_ids)
        rows = connection.execute(
            f"select * from games where run_id in ({placeholders}) order by run_id, job_key",
            run_ids,
        ).fetchall()
    except sqlite3.Error as error:
        raise ValueError(f"could not read self-play database {source.name}: {error}") from error
    finally:
        connection.close()

    logical_payload = {
        "runs": [dict(row) for row in runs],
        "games": [dict(row) for row in rows],
    }
    evidence = {
        "filename": source.name,
        # A SQLite WAL can hold committed rows that are not in the main file
        # yet. Keep the physical digest as provenance and separately identify
        # the exact logical rows consumed by this corpus build.
        "main_file_sha256": _file_sha256(source),
        "logical_content_sha256": hashlib.sha256(
            _canonical_json(logical_payload)
        ).hexdigest(),
        "completed_run_ids": list(run_ids),
        "engine_versions": sorted({str(row["engine_version"]) for row in runs}),
        "source_fingerprints": sorted(
            {str(row["source_fingerprint"]) for row in runs}
        ),
        "opening_suite_versions": sorted(
            {str(row["opening_suite_version"]) for row in rows}
        ),
    }
    excluded = sum(
        1
        for row in rows
        if row["result"] not in {"1-0", "0-1", "1/2-1/2"}
        or row["terminal_reason"] not in {"checkmate", "ten-series-draw"}
        or row["engine_failure_profile_id"] is not None
        or row["error"] is not None
    )
    selected = tuple(
        row
        for row in rows
        if row["result"] in {"1-0", "0-1", "1/2-1/2"}
        and row["terminal_reason"] in {"checkmate", "ten-series-draw"}
        and row["engine_failure_profile_id"] is None
        and row["error"] is None
    )
    return evidence, selected, excluded


def build_selfplay_corpus(
    database_paths: Sequence[str | Path],
    *,
    seed: int = 20260820,
    holdout_percent: int = 20,
) -> SelfPlayCorpus:
    """Replays completed league games into leakage-resistant value samples.

    Only checkmates and rules-proven ten-series draws are admitted. Every trace
    is replayed through :func:`play_series`; persisted FEN alone is never trusted.
    A connected-component split keeps related opening families together and
    also joins different families if their games transpose to the same boundary.
    """

    if not database_paths:
        raise ValueError("at least one self-play database is required")
    if not 0 <= holdout_percent <= 50:
        raise ValueError("holdout_percent must be between 0 and 50")
    line_families = _opening_line_families()
    evidence: list[Mapping[str, Any]] = []
    replayed: list[_ReplayedGame] = []
    excluded_games = 0
    seen_game_keys: set[str] = set()

    for database_path in database_paths:
        database_evidence, rows, excluded = _read_database(Path(database_path))
        evidence.append(database_evidence)
        excluded_games += excluded
        for row in rows:
            game_key = str(row["job_key"])
            if game_key in seen_game_keys:
                continue
            game = _replay_game(row, line_families)
            seen_game_keys.add(game_key)
            replayed.append(game)
    if not replayed:
        raise ValueError("no conclusive replayable self-play games were found")

    families = sorted({game.line_family for game in replayed})
    components = _DisjointSet(families)
    owners_by_position: dict[str, set[str]] = {}
    for game in replayed:
        for state in game.states:
            owners_by_position.setdefault(state.position_hash, set()).add(
                game.line_family
            )
    for owners in owners_by_position.values():
        ordered = sorted(owners)
        for owner in ordered[1:]:
            components.union(ordered[0], owner)
    members_by_root: dict[str, list[str]] = {}
    for family in families:
        members_by_root.setdefault(components.find(family), []).append(family)
    component_ids = {
        family: _digest(
            "spc-split-component-",
            {"families": sorted(members_by_root[components.find(family)])},
        )
        for family in families
    }

    samples: list[SelfPlaySample] = []
    for game in sorted(replayed, key=lambda item: (item.run_id, item.game_key)):
        component_id = component_ids[game.line_family]
        split = (
            "holdout"
            if _split_bucket(seed, component_id) < holdout_percent
            else "train"
        )
        weight = 1.0 / len(game.states)
        for state, profile_id, chosen in zip(
            game.states, game.profile_ids, game.chosen_series, strict=True
        ):
            samples.append(
                SelfPlaySample(
                    position_hash=state.position_hash,
                    pfen=state.pfen,
                    run_id=game.run_id,
                    game_key=game.game_key,
                    opening_case_id=game.opening_case_id,
                    line_family=game.line_family,
                    split_component=component_id,
                    split=split,
                    series_number=state.series_number,
                    mover="white" if state.board.turn == chess.WHITE else "black",
                    profile_id=profile_id,
                    chosen_series=chosen,
                    result=game.result,
                    target_white_score=game.target_white_score,
                    sample_weight=weight,
                    features=CachedFeatures.from_state(state),
                )
            )
    return SelfPlayCorpus(
        seed=seed,
        holdout_percent=holdout_percent,
        database_evidence=tuple(evidence),
        completed_games=len(replayed),
        excluded_games=excluded_games,
        samples=tuple(samples),
    )


def _probability(score: int, scale: int) -> float:
    exponent = max(-30.0, min(30.0, -score / scale))
    return 1.0 / (1.0 + math.pow(10.0, exponent))


def _weighted_log_loss(
    samples: Sequence[ValueTrainingSample],
    weights: EvaluationWeights,
    *,
    scale: int,
    regularization: float,
) -> float:
    total_weight = sum(sample.sample_weight for sample in samples)
    if total_weight <= 0.0:
        return math.inf
    loss = 0.0
    probe_profile = EngineProfile(name="self-play loss probe", weights=weights)
    for sample in samples:
        score = sample.features.score(probe_profile)
        probability = max(1e-12, min(1.0 - 1e-12, _probability(score, scale)))
        target = sample.target_white_score
        loss -= sample.sample_weight * (
            target * math.log(probability)
            + (1.0 - target) * math.log(1.0 - probability)
        )
    penalty = regularization * sum(
        ((getattr(weights, name) - 100) / 100.0) ** 2 for name in FEATURE_NAMES
    )
    return loss / total_weight + penalty


def tune_selfplay_profile(
    corpus: ValueTrainingCorpus,
    parent: EngineProfile,
    *,
    name: str = "self-play Texel candidate",
    scales: Sequence[int] = (200, 300, 400, 600, 800, 1200),
    step_schedule: Sequence[int] = (20, 10, 5, 2, 1),
    regularization: float = 0.02,
) -> tuple[EngineProfile, dict[str, Any]]:
    """Fits the seven explainable weights on train only via coordinate descent.

    This is a deterministic value proxy, not promotion evidence. The returned
    profile must still pass tactical gates and a separate color-swapped match.
    """

    raw_train = corpus.train_samples
    raw_holdout = corpus.holdout_samples
    if not raw_train:
        raise ValueError("self-play corpus has no training samples")
    train = _collapse_loss_equivalent_samples(raw_train)
    holdout = _collapse_loss_equivalent_samples(raw_holdout)
    if not scales or any(scale <= 0 for scale in scales):
        raise ValueError("scales must contain positive integers")
    if not step_schedule or any(step <= 0 for step in step_schedule):
        raise ValueError("step_schedule must contain positive integers")
    if regularization < 0.0:
        raise ValueError("regularization cannot be negative")

    current = parent.weights
    scale = min(
        sorted(set(int(value) for value in scales)),
        key=lambda value: (
            _weighted_log_loss(
                train,
                current,
                scale=value,
                regularization=regularization,
            ),
            value,
        ),
    )
    baseline_train_loss = _weighted_log_loss(
        train,
        current,
        scale=scale,
        regularization=regularization,
    )
    baseline_holdout_loss = _weighted_log_loss(
        holdout,
        current,
        scale=scale,
        regularization=0.0,
    )
    history: list[dict[str, Any]] = []
    values = asdict(current)

    for step in step_schedule:
        for _ in range(64):
            before = EvaluationWeights(**values)
            before_loss = _weighted_log_loss(
                train,
                before,
                scale=scale,
                regularization=regularization,
            )
            best_loss = before_loss
            best_values = values
            best_change: tuple[str, int] | None = None
            for feature_name in FEATURE_NAMES:
                for direction in (-1, 1):
                    candidate_values = dict(values)
                    candidate_values[feature_name] = max(
                        25,
                        min(300, candidate_values[feature_name] + direction * step),
                    )
                    if candidate_values[feature_name] == values[feature_name]:
                        continue
                    candidate_weights = EvaluationWeights(**candidate_values)
                    candidate_loss = _weighted_log_loss(
                        train,
                        candidate_weights,
                        scale=scale,
                        regularization=regularization,
                    )
                    ordering = (
                        candidate_loss,
                        feature_name,
                        candidate_values[feature_name],
                    )
                    incumbent = (
                        best_loss,
                        best_change[0] if best_change else "~",
                        best_change[1] if best_change else 10_000,
                    )
                    if ordering < incumbent and candidate_loss < before_loss - 1e-12:
                        best_loss = candidate_loss
                        best_values = candidate_values
                        best_change = (
                            feature_name,
                            candidate_values[feature_name],
                        )
            if best_change is None:
                break
            values = best_values
            history.append(
                {
                    "step": step,
                    "feature": best_change[0],
                    "value": best_change[1],
                    "train_loss": best_loss,
                }
            )

    tuned_weights = EvaluationWeights(**values)
    tuned = EngineProfile(
        name=name,
        weights=tuned_weights,
        recommended_depth=parent.recommended_depth,
        recommended_branch_cap=parent.recommended_branch_cap,
        generation=parent.generation + 1,
        parent_profile_ids=(parent.profile_id,),
        notes=(
            f"{SELFPLAY_TUNER_METHOD} candidate from {corpus.corpus_id}; "
            "not strength-verified until tactical and fixed-suite match gates pass."
        ),
    )
    tuned_train_loss = _weighted_log_loss(
        train,
        tuned_weights,
        scale=scale,
        regularization=regularization,
    )
    tuned_holdout_loss = _weighted_log_loss(
        holdout,
        tuned_weights,
        scale=scale,
        regularization=0.0,
    )
    report = {
        "method": SELFPLAY_TUNER_METHOD,
        "corpus_id": corpus.corpus_id,
        "parent_profile_id": parent.profile_id,
        "candidate_profile_id": tuned.profile_id,
        "scale": scale,
        "regularization": regularization,
        "step_schedule": list(step_schedule),
        "train_samples": len(raw_train),
        "holdout_samples": len(raw_holdout),
        "train_feature_buckets": len(train),
        "holdout_feature_buckets": len(holdout),
        "loss_surface_collapsed_exactly": True,
        "baseline_train_loss": baseline_train_loss,
        "candidate_train_loss": tuned_train_loss,
        "baseline_holdout_loss": (
            None if math.isinf(baseline_holdout_loss) else baseline_holdout_loss
        ),
        "candidate_holdout_loss": (
            None if math.isinf(tuned_holdout_loss) else tuned_holdout_loss
        ),
        "changes": history,
        "weights": asdict(tuned_weights),
        "claim_scope": (
            "self-play value-fit proxy only; tactical and controlled match "
            "promotion remain mandatory"
        ),
    }
    return tuned, report
