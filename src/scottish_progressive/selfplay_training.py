from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

import chess

from .fast_training import CachedFeatures, FEATURE_NAMES, default_training_positions
from .league import OPENING_SUITE, OPENING_SUITE_VERSION, OpeningCase
from .model import ENGINE_SOURCE_FINGERPRINT, ENGINE_VERSION, Outcome, ProgressiveState
from .profiles import EngineProfile, EvaluationWeights
from .rules import SeriesLegalityError, play_series
from .search import EvaluationOverlay, SearchLimits, analyze

if TYPE_CHECKING:
    from .fullgame import FullGameSemanticConfig
    from .fullgame_codec import FullGameRecord, RejectedAttempt


SELFPLAY_CORPUS_SCHEMA = 1
SELFPLAY_CORPUS_METHOD = "replayed-league-value-corpus-v1"
FULLGAME_CORPUS_METHOD = "replayed-fullgame-exploration-value-corpus-v1"
SELFPLAY_TUNER_METHOD = "deterministic-texel-coordinate-v1"
HUMAN_REFUTATION_GATE_ID = "human-nf3-qf6-c2-mate-v1"

HUMAN_REFUTATION_TRACE: tuple[tuple[str, ...], ...] = (
    ("g1f3",),
    ("e7e6", "d8f6"),
    ("d2d4", "c1g5", "g5f6"),
    ("c7c5", "c5d4", "d4d3", "d3c2"),
    ("d1c2", "c2c8"),
)
HUMAN_REFUTATION_BLUNDERS = {
    2: HUMAN_REFUTATION_TRACE[1],
    4: HUMAN_REFUTATION_TRACE[3],
}


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
    method: str = SELFPLAY_CORPUS_METHOD

    def __post_init__(self) -> None:
        if not 0 <= self.holdout_percent <= 50:
            raise ValueError("holdout_percent must be between 0 and 50")
        if self.completed_games < 1:
            raise ValueError("self-play corpus requires at least one completed game")
        if not self.samples:
            raise ValueError("self-play corpus requires at least one replayed sample")
        if self.method not in {SELFPLAY_CORPUS_METHOD, FULLGAME_CORPUS_METHOD}:
            raise ValueError("self-play corpus method is unsupported")

    @property
    def corpus_id(self) -> str:
        prefix = (
            "spc-fullgame-corpus-"
            if self.method == FULLGAME_CORPUS_METHOD
            else "spc-selfplay-corpus-"
        )
        return _digest(prefix, self.deterministic_payload())

    @property
    def train_samples(self) -> tuple[SelfPlaySample, ...]:
        return tuple(sample for sample in self.samples if sample.split == "train")

    @property
    def holdout_samples(self) -> tuple[SelfPlaySample, ...]:
        return tuple(sample for sample in self.samples if sample.split == "holdout")

    def deterministic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SELFPLAY_CORPUS_SCHEMA,
            "method": self.method,
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
                    (
                        "exploration-policy terminal WDL is a weak value label only; "
                        "every accepted trace is legally replayed, duplicate traces "
                        "and manual or technical results are excluded, and these "
                        "labels are never promotion evidence"
                    )
                    if self.method == FULLGAME_CORPUS_METHOD
                    else (
                        "eventual legal checkmate or rules-proven ten-series draw; "
                        "manual and technical results excluded"
                    )
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


def _samples_from_replayed_games(
    replayed: Sequence[_ReplayedGame],
    *,
    seed: int,
    holdout_percent: int,
) -> tuple[SelfPlaySample, ...]:
    """Splits whole transposition components, then materializes samples."""

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
    return tuple(samples)


def _samples_from_replayed_full_games(
    replayed: Sequence[_ReplayedGame],
    *,
    seed: int,
    holdout_percent: int,
) -> tuple[tuple[SelfPlaySample, ...], int, int]:
    """Splits whole full games, then removes lower-priority transpositions.

    Full-game exploration stores are intentionally dense: connecting every game
    that ever reaches the same boundary produces one giant component in a large
    run. Each game therefore receives its own deterministic split component. A
    position seen in both splits is retained only in train; the remaining rows
    of every represented game are reweighted to keep total game weight at one.
    """

    ordered_games = tuple(
        sorted(replayed, key=lambda item: (item.run_id, item.game_key))
    )
    game_identities = [(game.run_id, game.game_key) for game in ordered_games]
    if len(set(game_identities)) != len(game_identities):
        raise ValueError("full-game corpus repeats a replayed game identity")

    assignments: dict[tuple[str, str], tuple[str, str]] = {}
    owner_by_position: dict[str, str] = {}
    for game in ordered_games:
        if not game.states:
            raise ValueError("full-game corpus cannot split an empty replayed game")
        identity = (game.run_id, game.game_key)
        component_id = _digest(
            "spc-split-component-",
            {"run_id": game.run_id, "game_key": game.game_key},
        )
        split = (
            "holdout"
            if _split_bucket(seed, component_id) < holdout_percent
            else "train"
        )
        assignments[identity] = (component_id, split)
        for state in game.states:
            current = owner_by_position.get(state.position_hash)
            if current is None or (current == "holdout" and split == "train"):
                owner_by_position[state.position_hash] = split

    samples: list[SelfPlaySample] = []
    fully_shadowed_games = 0
    removed_position_occurrences = 0
    for game in ordered_games:
        component_id, split = assignments[(game.run_id, game.game_key)]
        retained = tuple(
            index
            for index, state in enumerate(game.states)
            if owner_by_position[state.position_hash] == split
        )
        removed_position_occurrences += len(game.states) - len(retained)
        if not retained:
            fully_shadowed_games += 1
            continue
        weight = 1.0 / len(retained)
        for index in retained:
            state = game.states[index]
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
                    profile_id=game.profile_ids[index],
                    chosen_series=game.chosen_series[index],
                    result=game.result,
                    target_white_score=game.target_white_score,
                    sample_weight=weight,
                    features=CachedFeatures.from_state(state),
                )
            )

    if not samples:
        raise ValueError("no leakage-safe full-game samples remain")
    hashes_by_split = {
        split: {
            sample.position_hash for sample in samples if sample.split == split
        }
        for split in ("train", "holdout")
    }
    if hashes_by_split["train"] & hashes_by_split["holdout"]:
        raise AssertionError("full-game train/holdout position leakage")
    games_by_key: dict[str, list[SelfPlaySample]] = {}
    for sample in samples:
        games_by_key.setdefault(sample.game_key, []).append(sample)
    for game_samples in games_by_key.values():
        if (
            len({sample.split for sample in game_samples}) != 1
            or len({sample.split_component for sample in game_samples}) != 1
            or abs(sum(sample.sample_weight for sample in game_samples) - 1.0)
            > 1e-12
        ):
            raise AssertionError("full-game split/weight contract failed")
    return tuple(samples), fully_shadowed_games, removed_position_occurrences


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

    samples = _samples_from_replayed_games(
        replayed,
        seed=seed,
        holdout_percent=holdout_percent,
    )
    return SelfPlayCorpus(
        seed=seed,
        holdout_percent=holdout_percent,
        database_evidence=tuple(evidence),
        completed_games=len(replayed),
        excluded_games=excluded_games,
        samples=samples,
    )


def build_fullgame_corpus(
    records: Iterable["FullGameRecord | RejectedAttempt"],
    config: "FullGameSemanticConfig",
    *,
    seed: int = 20260820,
    holdout_percent: int = 20,
    excluded_attempts: int = 0,
    evidence: Sequence[Mapping[str, Any]] = (),
) -> SelfPlayCorpus:
    """Builds a weak-value corpus from authoritative full-game traces.

    Rollout WDL is deliberately treated as a weak value label: the generator is
    an exploration policy, not champion play.  Every accepted trace is replayed,
    exact trace duplicates are rejected, and technical attempts never receive a
    target. The universal initial S1 boundary is omitted. Whole games receive
    deterministic split components before any row is retained; a boundary that
    occurs in both splits is kept only in train, and each represented game's
    remaining samples are renormalized to total weight one.
    """

    from .fullgame import (
        DATA_PURPOSE,
        STRENGTH_CLAIM,
        FullGameSemanticConfig,
        game_id,
    )
    from .fullgame_codec import (
        FullGameRecord,
        RejectedAttempt,
        replay_record,
        trace_sha256,
    )

    if type(config) is not FullGameSemanticConfig:
        raise ValueError("full-game corpus requires an exact semantic config")
    if config.data_purpose != DATA_PURPOSE or config.strength_claim != STRENGTH_CLAIM:
        raise ValueError("full-game corpus accepts exploration-only data")
    if not 0 <= holdout_percent <= 50:
        raise ValueError("holdout_percent must be between 0 and 50")
    if type(excluded_attempts) is not int or excluded_attempts < 0:
        raise ValueError("excluded_attempts must be a nonnegative integer")
    if any(not isinstance(item, Mapping) for item in evidence):
        raise ValueError("full-game corpus evidence entries must be mappings")

    replayed: list[_ReplayedGame] = []
    seen_attempts: set[int] = set()
    seen_traces: set[str] = set()
    rejected_count = 0
    logical_work = 0
    path_saturations = 0

    for item in records:
        if type(item) not in {FullGameRecord, RejectedAttempt}:
            raise ValueError("full-game corpus input contains an unsupported record")
        if item.attempt_index in seen_attempts:
            raise ValueError(
                f"full-game corpus repeats attempt {item.attempt_index}"
            )
        seen_attempts.add(item.attempt_index)
        expected_pair = (
            config.profile_pair(item.attempt_index)
            if config.backend_kind == "native"
            else (0, 0)
        )
        if (
            item.white_profile_index,
            item.black_profile_index,
        ) != expected_pair:
            raise ValueError(
                f"full-game attempt {item.attempt_index} has invalid profile attribution"
            )
        logical_work += item.logical_work
        path_saturations += item.path_count_saturations
        if type(item) is RejectedAttempt:
            rejected_count += 1
            continue

        replay_record(item)
        trace_digest = trace_sha256(item)
        if trace_digest in seen_traces:
            raise ValueError("full-game corpus contains a duplicate accepted trace")
        seen_traces.add(trace_digest)

        state = ProgressiveState.initial()
        states: list[ProgressiveState] = []
        profile_ids: list[str] = []
        chosen_series: list[str] = []
        for series_index, moves in enumerate(item.series):
            if series_index:
                states.append(state)
                profile_index = (
                    item.white_profile_index
                    if state.board.turn == chess.WHITE
                    else item.black_profile_index
                )
                profile_ids.append(config.profile_pool[profile_index].profile_id)
                chosen_series.append("/".join(moves))
            result = play_series(state, moves)
            if result.machine_notation != "/".join(moves):
                raise ValueError("full-game trace contains noncanonical move notation")
            state = result.final_state
        if not states:
            raise ValueError(
                "full-game trace has no post-S1 boundary for leakage-safe training"
            )
        first_boundary = states[0]
        replayed.append(
            _ReplayedGame(
                run_id=config.simulation_id,
                game_key=game_id(config, item.attempt_index),
                opening_case_id=f"after-s1-{item.series[0][0]}",
                line_family=f"fullgame-after-s1:{first_boundary.position_hash}",
                result=item.result,
                target_white_score=_result_target(item.result),
                states=tuple(states),
                profile_ids=tuple(profile_ids),
                chosen_series=tuple(chosen_series),
            )
        )

    if not replayed:
        raise ValueError("no accepted replayable full games were found")
    samples, fully_shadowed_games, removed_position_occurrences = (
        _samples_from_replayed_full_games(
            replayed,
            seed=seed,
            holdout_percent=holdout_percent,
        )
    )
    semantic_payload = config.as_dict()
    source_evidence: tuple[Mapping[str, Any], ...] = (
        {
            "source_kind": "fullgame-exploration-records",
            "simulation_id": config.simulation_id,
            "semantic_config_sha256": hashlib.sha256(
                _canonical_json(semantic_payload)
            ).hexdigest(),
            "data_purpose": config.data_purpose,
            "strength_claim": config.strength_claim,
            "policy_id": config.rank_policy_id,
            "profile_schedule_id": config.profile_schedule_id,
            "profile_ids": [profile.profile_id for profile in config.profile_pool],
            "accepted_records": len(replayed),
            "rejected_records": rejected_count,
            "leakage_filter": "whole-game-split-priority-dedup-v1",
            "fully_shadowed_games": fully_shadowed_games,
            "removed_position_occurrences": removed_position_occurrences,
            "logical_work": logical_work,
            "path_count_saturations": path_saturations,
        },
        *(dict(item) for item in evidence),
    )
    return SelfPlayCorpus(
        seed=seed,
        holdout_percent=holdout_percent,
        database_evidence=source_evidence,
        completed_games=len(replayed) - fully_shadowed_games,
        excluded_games=(
            excluded_attempts + rejected_count + fully_shadowed_games
        ),
        samples=samples,
        method=FULLGAME_CORPUS_METHOD,
    )


def build_verified_fullgame_corpus(
    root: str | Path,
    *,
    seed: int = 20260820,
    holdout_percent: int = 20,
    max_games: int | None = None,
) -> SelfPlayCorpus:
    """Consumes one stable, store-verified full-game checkpoint snapshot."""

    from .fullgame import (
        FullGameSemanticConfig,
        iter_fullgame_records,
        verify_fullgame_run,
    )

    if max_games is not None and (
        type(max_games) is not int or max_games < 1
    ):
        raise ValueError("max_games must be a positive integer")
    base = Path(root).expanduser().resolve()
    manifest_path = base / "manifest.json"
    try:
        raw_before = manifest_path.read_bytes()
    except OSError as error:
        raise ValueError(f"could not read full-game manifest: {error}") from error
    verification = verify_fullgame_run(base)
    try:
        raw_verified = manifest_path.read_bytes()
        manifest = json.loads(raw_verified.decode("ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read full-game manifest: {error}") from error
    if raw_before != raw_verified:
        raise ValueError("full-game store changed while it was being verified")
    config = FullGameSemanticConfig.from_dict(manifest["semantic_config"])
    if config.simulation_id != verification["simulation_id"]:
        raise ValueError("verified full-game simulation identity changed")

    selected: list[FullGameRecord] = []
    for record in iter_fullgame_records(base):
        selected.append(record)
        if max_games is not None and len(selected) >= max_games:
            break
    try:
        raw_after = manifest_path.read_bytes()
    except OSError as error:
        raise ValueError(f"could not reread full-game manifest: {error}") from error
    if raw_after != raw_verified:
        raise ValueError("full-game store changed while the corpus was being built")
    consumed_entire_snapshot = (
        len(selected) == int(verification["accepted_unique_games"])
    )

    return build_fullgame_corpus(
        selected,
        config,
        seed=seed,
        holdout_percent=holdout_percent,
        excluded_attempts=(
            int(verification["checkpoint_rejections"])
            if consumed_entire_snapshot
            else 0
        ),
        evidence=(
            {
                "source_kind": "verified-fullgame-store-snapshot",
                "manifest_sha256": hashlib.sha256(raw_verified).hexdigest(),
                "authoritative_replay": verification["authoritative_replay"],
                "trace_deduplication": verification["trace_deduplication"],
                "store_accepted_unique_games": verification[
                    "accepted_unique_games"
                ],
                "consumed_games": len(selected),
                "consumed_entire_snapshot": consumed_entire_snapshot,
                "checkpoint_rejections": verification[
                    "checkpoint_rejections"
                ],
                "checkpoint_rejections_scope": (
                    "included in excluded_games for full-snapshot consumption"
                    if consumed_entire_snapshot
                    else "store-wide evidence only; not attributed to this prefix"
                ),
                "logical_work": verification["logical_work"],
                "series": verification["series"],
                "micro_moves": verification["micro_moves"],
            },
        ),
    )


def evaluate_human_refutation_gate(
    profile: EngineProfile,
    *,
    limits: SearchLimits | None = None,
    evaluation_overlay: EvaluationOverlay | None = None,
) -> dict[str, Any]:
    """Replays the reported Series-5 mate and tests both losing Black turns.

    A candidate passes only after completing the requested search depth at both
    anchors and selecting neither known losing series.  An attractive fallback
    returned after a timeout or work cap is not promotion evidence.
    """

    if evaluation_overlay is not None and evaluation_overlay.base_profile_id != profile.profile_id:
        raise ValueError("human refutation overlay is bound to a different base profile")
    selected_limits = limits or SearchLimits(
        depth_series=2,
        max_series_per_node=32,
        max_generation_positions=250_000,
        collect_all_root_scores=False,
    )
    states_by_series: dict[int, ProgressiveState] = {}
    state = ProgressiveState.initial()
    last_result = None
    last_mover = state.board.turn
    replayed_notation: list[str] = []
    for moves in HUMAN_REFUTATION_TRACE:
        states_by_series[state.series_number] = state
        last_mover = state.board.turn
        last_result = play_series(state, moves)
        if last_result.machine_notation != "/".join(moves):
            raise AssertionError("human refutation fixture is not canonical")
        replayed_notation.append(last_result.machine_notation)
        state = last_result.final_state
    if (
        last_result is None
        or last_result.outcome != Outcome.CHECKMATE
        or not last_result.ended_by_check
        or last_mover != chess.WHITE
    ):
        raise AssertionError("human refutation fixture must end in White checkmate")

    anchors: list[dict[str, Any]] = []
    for series_number, blunder in sorted(HUMAN_REFUTATION_BLUNDERS.items()):
        if evaluation_overlay is None:
            result = analyze(states_by_series[series_number], selected_limits, profile)
        else:
            result = analyze(
                states_by_series[series_number],
                selected_limits,
                profile,
                evaluation_overlay=evaluation_overlay,
            )
        selected = (
            None
            if result.best_series is None
            else result.best_series.machine_notation
        )
        completed = (
            selected_limits.depth_series >= 2
            and selected is not None
            and result.requested_depth == selected_limits.depth_series
            and result.completed_depth == selected_limits.depth_series
            and not result.timed_out
            and not result.work_limit_reached
        )
        avoided = selected is not None and selected != "/".join(blunder)
        anchors.append(
            {
                "series_number": series_number,
                "position_hash": states_by_series[series_number].position_hash,
                "known_losing_series": "/".join(blunder),
                "selected_series": selected,
                "score_white": result.score,
                "principal_variation": [
                    item.machine_notation for item in result.principal_variation
                ],
                "requested_depth": result.requested_depth,
                "completed_depth": result.completed_depth,
                "exact_width": result.exact_width,
                "timed_out": result.timed_out,
                "work_limit_reached": result.work_limit_reached,
                "work_positions": result.stats.work_positions,
                "nodes": result.stats.nodes,
                "elapsed_seconds": result.elapsed_seconds,
                "completed_required_search": completed,
                "avoided_known_blunder": avoided,
                "passed": completed and avoided,
            }
        )

    return {
        "gate_id": HUMAN_REFUTATION_GATE_ID,
        "profile_id": (
            profile.profile_id
            if evaluation_overlay is None
            else evaluation_overlay.variant_id
        ),
        "profile_name": (
            profile.name if evaluation_overlay is None else evaluation_overlay.name
        ),
        "base_profile_id": profile.profile_id,
        "passed": all(anchor["passed"] for anchor in anchors),
        "fixture": {
            "series": replayed_notation,
            "terminal": "checkmate-white",
            "final_pfen": state.pfen,
        },
        "limits": {
            "depth_series": selected_limits.depth_series,
            "max_series_per_node": selected_limits.max_series_per_node,
            "max_generation_positions": selected_limits.max_generation_positions,
            "time_limit_seconds": selected_limits.time_limit_seconds,
            "collect_all_root_scores": selected_limits.collect_all_root_scores,
        },
        "anchors": anchors,
        "claim_scope": (
            "mandatory tactical regression only; passing does not promote a "
            "profile without a separate fresh color-swapped match"
        ),
    }


def _probability(score: int, scale: int) -> float:
    exponent = max(-30.0, min(30.0, -score / scale))
    return 1.0 / (1.0 + math.pow(10.0, exponent))


def _weighted_log_loss(
    samples: Sequence[SelfPlaySample],
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
    corpus: SelfPlayCorpus,
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

    train = corpus.train_samples
    holdout = corpus.holdout_samples
    if not train:
        raise ValueError("self-play corpus has no training samples")
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
            f"not strength-verified until {HUMAN_REFUTATION_GATE_ID} and a "
            "fresh color-swapped fixed-suite match both pass."
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
        "corpus_method": corpus.method,
        "corpus_id": corpus.corpus_id,
        "parent_profile_id": parent.profile_id,
        "candidate_profile_id": tuned.profile_id,
        "scale": scale,
        "regularization": regularization,
        "step_schedule": list(step_schedule),
        "train_samples": len(train),
        "holdout_samples": len(holdout),
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
        "required_promotion_gates": [
            HUMAN_REFUTATION_GATE_ID,
            "fresh-color-swapped-fixed-suite-match",
        ],
    }
    return tuned, report
