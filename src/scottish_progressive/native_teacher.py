from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import (
    FIRST_COMPLETED,
    Executor,
    ProcessPoolExecutor,
    wait,
)
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

import chess

from . import evaluation
from .corpus_pipeline import read_native_generation_contract
from .corpus_samples import NativeBoundarySample, decode_native_boundary_sample
from .corpus_shards import CorpusRecord, CorpusStore, progressive_state_dedup_key
from .fast_training import CachedFeatures
from .league import run_rules_tactical_gate
from .model import ENGINE_SOURCE_FINGERPRINT, ENGINE_VERSION, ProgressiveState, SeriesResult
from .native_corpus import (
    NativeCorpusConfig,
    NativeCorpusProfile,
    NativeProfileSchedule,
    generate_native_full_game_batch,
    replay_native_full_game,
)
from .native_corpus_training import NativeCorpusGenerationContract
from .native_subtree import native_subtree_available
from .profiles import EngineProfile
from .rules import play_series
from .series_mate import native_mate_runtime_identity
from .search import MATE_SCORE, SearchLimits, SearchResult, ScoredSeries, analyze


NATIVE_TEACHER_SCHEMA = "spc-native-deep-teacher-corpus-v1"
NATIVE_TEACHER_METHOD = "balanced-native-trajectory-depth3-policy-teacher-v1"
NATIVE_TEACHER_MIXED_SCHEMA = "spc-deep-teacher-corpus-v1"
NATIVE_TEACHER_MIXED_METHOD = "balanced-native-trajectory-mixed-depth-policy-teacher-v1"


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _source_file_sha256(filename: str) -> str:
    return hashlib.sha256((Path(__file__).resolve().parent / filename).read_bytes()).hexdigest()


def _label_runtime_identities(native_mate_identity: str) -> dict[str, str]:
    expected_native_eval = evaluation._native_source_identity()
    loaded_native_eval = getattr(evaluation._native_eval, "SOURCE_IDENTITY", None)
    if (
        expected_native_eval is None
        or not isinstance(loaded_native_eval, str)
        or loaded_native_eval != expected_native_eval
    ):
        raise RuntimeError("teacher label runtime requires the exact native eval identity")
    return {
        "evaluation_source_sha256": _source_file_sha256("evaluation.py"),
        "native_eval_runtime_identity": loaded_native_eval,
        "native_mate_runtime_identity": native_mate_identity,
        "rules_source_sha256": _source_file_sha256("rules.py"),
        "search_source_sha256": _source_file_sha256("search.py"),
    }


@dataclass(frozen=True, slots=True)
class NativeTeacherConfig:
    target_roots: int = 192
    train_roots: int = 128
    minimum_series: int = 4
    maximum_series: int = 9
    depth_series: int = 3
    branch_cap: int = 32
    max_generation_positions: int = 10_000_000
    hard_negative_count: int = 4
    seed: int = 2_026_082_303
    workers: int = 8
    expected_train_attempts: int = 8_192
    expected_holdout_attempts: int = 4_096
    selection_mode: str = "all"

    def __post_init__(self) -> None:
        if self.target_roots < 1:
            raise ValueError("target_roots must be positive")
        if not 1 <= self.train_roots < self.target_roots:
            raise ValueError("train_roots must leave a nonempty holdout")
        if not 1 <= self.minimum_series <= self.maximum_series:
            raise ValueError("teacher series range is invalid")
        if self.depth_series < 1:
            raise ValueError("depth_series must be positive")
        if self.branch_cap < 1:
            raise ValueError("branch_cap must be positive")
        if self.max_generation_positions < 1_000:
            raise ValueError("max_generation_positions must be at least 1000")
        if self.hard_negative_count < 1:
            raise ValueError("hard_negative_count must be positive")
        if not 1 <= self.workers <= 64:
            raise ValueError("workers must be between 1 and 64")
        if self.expected_train_attempts < 1 or self.expected_holdout_attempts < 1:
            raise ValueError("expected corpus attempt counts must be positive")
        if self.selection_mode not in {
            "all",
            "tactical-low-complexity",
            "quiet-nonterminal",
        }:
            raise ValueError("unsupported native teacher selection_mode")

    @property
    def holdout_roots(self) -> int:
        return self.target_roots - self.train_roots

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Candidate:
    split: str
    attempt_index: int
    sequence_index: int
    state_key_sha256: str
    sample: NativeBoundarySample
    source_profile_id: str
    white_profile_id: str
    black_profile_id: str
    source_series_remaining: int

    @property
    def cell(self) -> tuple[str, int]:
        return self.source_profile_id, self.sample.state.series_number


@dataclass(frozen=True, slots=True)
class _BucketJob:
    split: str
    source_profile_id: str
    series_number: int
    quota: int
    candidates: tuple[_Candidate, ...]
    source_config: NativeCorpusConfig
    source_profiles: tuple[NativeCorpusProfile, ...]
    teacher_profile: EngineProfile
    teacher_config: NativeTeacherConfig
    receipt_root: str
    cache_contract_id: str
    forbidden_state_keys: frozenset[str] = frozenset()


def _candidate_rank(seed: int, candidate: _Candidate) -> bytes:
    return hashlib.sha256(
        (
            f"{seed}|{candidate.split}|{candidate.state_key_sha256}|"
            f"{candidate.attempt_index}|{candidate.sequence_index}"
        ).encode("ascii")
    ).digest()


def _teacher_candidate_order(
    seed: int,
    candidate: _Candidate,
    selection_mode: str = "all",
) -> tuple[int, int, int, bytes]:
    """Prefer exact-terminal anchors without weakening any label requirement.

    Progressive S4-S9 roots can spend nearly their entire work envelope just
    proving a broad quiet continuation.  A boundary immediately before a
    replay-verified terminal series still carries real policy alternatives,
    but supplies an existential tactical line that the move ordering can
    discover quickly.  Piece count is only a deterministic cost tie-breaker;
    it never changes depth, width, completeness, or proof acceptance.
    """

    digest = _candidate_rank(seed, candidate)
    if selection_mode == "quiet-nonterminal":
        # The hash leads so the broad quiet tier is not a disguised endgame
        # sample. Piece count is retained only as a collision-proof tiebreak.
        return (
            int.from_bytes(digest[:8], "big"),
            0,
            len(candidate.sample.state.board.piece_map()),
            digest,
        )
    return (
        0 if candidate.source_series_remaining == 1 else 1,
        0 if candidate.sample.value_for_side_to_move == 1 else 1,
        len(candidate.sample.state.board.piece_map()),
        digest,
    )


def _candidate_matches_mode(candidate: _Candidate, mode: str) -> bool:
    if mode == "tactical-low-complexity":
        return candidate.source_series_remaining == 1
    if mode == "quiet-nonterminal":
        return candidate.source_series_remaining >= 2
    return True


def _group_records(store: CorpusStore) -> Iterable[tuple[CorpusRecord, ...]]:
    current_attempt: int | None = None
    current: list[CorpusRecord] = []
    for record in store.iter_records():
        if current_attempt is None:
            current_attempt = record.attempt_index
        if record.attempt_index != current_attempt:
            yield _validated_group(current_attempt, current)
            current_attempt = record.attempt_index
            current = []
        current.append(record)
    if current_attempt is not None:
        yield _validated_group(current_attempt, current)


def _validated_group(
    attempt_index: int, records: Sequence[CorpusRecord]
) -> tuple[CorpusRecord, ...]:
    if not records:
        raise ValueError(f"attempt {attempt_index} has no records")
    if [item.sequence_index for item in records] != list(range(len(records))):
        raise ValueError(f"attempt {attempt_index} sequence is not contiguous")
    return tuple(records)


def _candidate_population(
    store: CorpusStore,
    *,
    split: str,
    config: NativeTeacherConfig,
    excluded_keys: set[str],
) -> tuple[dict[str, _Candidate], set[str], dict[str, int]]:
    if split not in {"train", "holdout"}:
        raise ValueError("split must be train or holdout")
    unique: dict[str, _Candidate] = {}
    all_eligible_keys: set[str] = set()
    duplicate_occurrences = 0
    overlap_occurrences = 0
    terminal_boundaries = 0
    for group in _group_records(store):
        terminal_boundaries += 1
        for record in group[:-1]:
            sample = decode_native_boundary_sample(record.payload)
            expected_key = progressive_state_dedup_key(
                sample.state,
                ruleset_version=store.identity.ruleset_version,
            ).hex()
            if record.state_key.hex() != expected_key:
                raise ValueError(
                    f"attempt {record.attempt_index} sequence "
                    f"{record.sequence_index} state key drifted"
                )
            series_number = sample.state.series_number
            if not config.minimum_series <= series_number <= config.maximum_series:
                continue
            all_eligible_keys.add(expected_key)
            if expected_key in excluded_keys:
                overlap_occurrences += 1
                continue
            mover_index = (
                sample.white_profile_index
                if sample.state.board.turn == chess.WHITE
                else sample.black_profile_index
            )
            try:
                source_profile_id = store.identity.profile_ids[mover_index]
                white_profile_id = store.identity.profile_ids[
                    sample.white_profile_index
                ]
                black_profile_id = store.identity.profile_ids[
                    sample.black_profile_index
                ]
            except IndexError as error:
                raise ValueError("sample profile index exceeds store identity") from error
            candidate = _Candidate(
                split=split,
                attempt_index=record.attempt_index,
                sequence_index=record.sequence_index,
                state_key_sha256=expected_key,
                sample=sample,
                source_profile_id=source_profile_id,
                white_profile_id=white_profile_id,
                black_profile_id=black_profile_id,
                source_series_remaining=len(group) - 1 - record.sequence_index,
            )
            if not _candidate_matches_mode(candidate, config.selection_mode):
                continue
            prior = unique.get(expected_key)
            if prior is None:
                unique[expected_key] = candidate
            else:
                duplicate_occurrences += 1
                if _teacher_candidate_order(
                    config.seed, candidate, config.selection_mode
                ) < _teacher_candidate_order(
                    config.seed, prior, config.selection_mode
                ):
                    unique[expected_key] = candidate
    return (
        unique,
        all_eligible_keys,
        {
            "eligible_unique_states": len(all_eligible_keys),
            "retained_unique_states": len(unique),
            "duplicate_occurrences": duplicate_occurrences,
            "excluded_overlap_occurrences": overlap_occurrences,
            "terminal_boundaries_excluded": terminal_boundaries,
        },
    )


def _balanced_quotas(
    profile_ids: Sequence[str], config: NativeTeacherConfig
) -> dict[tuple[str, str, int], int]:
    series_numbers = tuple(range(config.minimum_series, config.maximum_series + 1))
    cells = tuple((profile_id, series) for profile_id in profile_ids for series in series_numbers)
    if not cells:
        raise ValueError("teacher quota grid must contain at least one cell")

    # Targets need not divide evenly across the 24 profile/series cells.  Use
    # deterministic floor/ceil apportionment so larger preregistered cycles do
    # not have to distort their total merely to satisfy the grid geometry.
    base_total, total_remainder = divmod(config.target_roots, len(cells))
    cell_totals = {cell: base_total for cell in cells}
    profile_extras = Counter[str]()
    series_extras = Counter[int]()
    remaining_cells = set(cells)
    for extra_index in range(total_remainder):
        cell = min(
            remaining_cells,
            key=lambda item: (
                profile_extras[item[0]],
                series_extras[item[1]],
                hashlib.sha256(
                    f"{config.seed}|total-quota|{extra_index}|{item[0]}|{item[1]}".encode(
                        "ascii"
                    )
                ).digest(),
            ),
        )
        cell_totals[cell] += 1
        profile_extras[cell[0]] += 1
        series_extras[cell[1]] += 1
        remaining_cells.remove(cell)

    # Apportion the exact train total proportionally within those capacities.
    # Largest-remainder allocation is deterministic and leaves holdout as the
    # exact complement in every cell.
    train = {
        cell: (config.train_roots * cell_total) // config.target_roots
        for cell, cell_total in cell_totals.items()
    }
    train_remainder = config.train_roots - sum(train.values())
    train_profile_extras = Counter[str]()
    train_series_extras = Counter[int]()
    remaining_train_cells = {
        cell for cell in cells if train[cell] < cell_totals[cell]
    }
    for extra_index in range(train_remainder):
        if not remaining_train_cells:
            raise AssertionError("balanced train quota exceeded cell capacity")
        cell = min(
            remaining_train_cells,
            key=lambda item: (
                -(config.train_roots * cell_totals[item] % config.target_roots),
                train_profile_extras[item[0]],
                train_series_extras[item[1]],
                hashlib.sha256(
                    f"{config.seed}|train-quota|{extra_index}|{item[0]}|{item[1]}".encode(
                        "ascii"
                    )
                ).digest(),
            ),
        )
        train[cell] += 1
        train_profile_extras[cell[0]] += 1
        train_series_extras[cell[1]] += 1
        if train[cell] == cell_totals[cell]:
            remaining_train_cells.remove(cell)
    quotas: dict[tuple[str, str, int], int] = {}
    for profile_id, series in cells:
        train_quota = train[(profile_id, series)]
        quotas[("train", profile_id, series)] = train_quota
        quotas[("holdout", profile_id, series)] = (
            cell_totals[(profile_id, series)] - train_quota
        )
    if sum(value for key, value in quotas.items() if key[0] == "train") != config.train_roots:
        raise AssertionError("balanced train quota drifted")
    if sum(value for key, value in quotas.items() if key[0] == "holdout") != config.holdout_roots:
        raise AssertionError("balanced holdout quota drifted")
    return quotas


def _mate_distance(score: int) -> int | None:
    distance = MATE_SCORE - abs(score)
    if not 0 <= distance <= 10_000:
        return None
    return distance if score > 0 else -distance


def _mover_regret(best_score: int, option_score: int, mover: chess.Color) -> int:
    return best_score - option_score if mover == chess.WHITE else option_score - best_score


def _validated_pv(
    root: ProgressiveState, series: Sequence[SeriesResult]
) -> list[dict[str, Any]]:
    state = root
    payload: list[dict[str, Any]] = []
    for ply, item in enumerate(series, 1):
        replayed = play_series(state, item.moves)
        expected_key = progressive_state_dedup_key(item.final_state).hex()
        actual_key = progressive_state_dedup_key(replayed.final_state).hex()
        if expected_key != actual_key or replayed.machine_notation != item.machine_notation:
            raise ValueError(f"teacher PV replay drifted at series ply {ply}")
        payload.append(
            {
                "series_ply": ply,
                "series": item.machine_notation,
                "final_state_key_sha256": actual_key,
                "outcome": None if item.outcome is None else item.outcome.value,
                "ended_by_check": item.ended_by_check,
            }
        )
        state = replayed.final_state
    return payload


def _complete_result_or_reason(result: SearchResult) -> str | None:
    if result.best_series is None:
        return "no-best-series"
    if result.timed_out:
        return "timed-out"
    if result.work_limit_reached:
        return "work-limit"
    if result.completed_depth != result.requested_depth:
        return "incomplete-depth"
    if not result.root_scores_complete:
        return "incomplete-root-scores"
    if not result.alternatives:
        return "no-alternatives"
    matching = [
        item for item in result.alternatives if item.series.moves == result.best_series.moves
    ]
    if len(matching) != 1 or matching[0].score != result.score:
        return "best-alternative-mismatch"
    return None


def _option_payload(
    root: ProgressiveState,
    item: ScoredSeries,
    *,
    best_score: int,
    source_series: str,
    best_series: str,
    hard_series: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    mover = root.board.turn
    notation = item.series.machine_notation
    pv = _validated_pv(root, (item.series,) + item.principal_variation)
    final_state = item.series.final_state
    return {
        "series": notation,
        "score_white_heuristic_points": item.score,
        "mover_regret_points": _mover_regret(best_score, item.score, mover),
        "proof_bounds": list(item.proof_bounds),
        "proof": item.proof,
        "signed_mate_distance_series": _mate_distance(item.score),
        "principal_variation": pv,
        "final_state_key_sha256": progressive_state_dedup_key(final_state).hex(),
        "final_pfen": final_state.pfen,
        "final_features": CachedFeatures.from_state(final_state).as_dict(),
        "outcome": None if item.series.outcome is None else item.series.outcome.value,
        "ended_by_check": item.series.ended_by_check,
        "is_teacher_best": notation == best_series,
        "is_source_played": notation == source_series,
        "is_hard_negative": notation in hard_series,
        "hard_negative_reasons": list(hard_series.get(notation, ())),
    }


def _serialize_complete_label(
    candidate: _Candidate,
    result: SearchResult,
    source_series: str,
    config: NativeTeacherConfig,
) -> dict[str, Any]:
    root = candidate.sample.state
    mover = root.board.turn
    best = next(
        item
        for item in result.alternatives
        if result.best_series is not None and item.series.moves == result.best_series.moves
    )
    best_series = best.series.machine_notation
    ordered_negatives = sorted(
        (item for item in result.alternatives if item.series.machine_notation != best_series),
        key=lambda item: (
            _mover_regret(best.score, item.score, mover),
            item.series.machine_notation,
        ),
    )
    hard_reasons: dict[str, list[str]] = defaultdict(list)
    for item in ordered_negatives[: config.hard_negative_count]:
        hard_reasons[item.series.machine_notation].append("nearest-retained-alternative")
    source_option = next(
        (
            item
            for item in result.alternatives
            if item.series.machine_notation == source_series
        ),
        None,
    )
    if source_option is not None and source_series != best_series:
        hard_reasons[source_series].append("source-policy-disagreement")
    for item in ordered_negatives:
        if item.proof_bounds != best.proof_bounds:
            hard_reasons[item.series.machine_notation].append("proof-contrast")
    normalized_hard = {
        key: tuple(dict.fromkeys(reasons)) for key, reasons in hard_reasons.items()
    }
    options = [
        _option_payload(
            root,
            item,
            best_score=best.score,
            source_series=source_series,
            best_series=best_series,
            hard_series=normalized_hard,
        )
        for item in result.alternatives
    ]
    mover_name = "white" if mover == chess.WHITE else "black"
    opponent_name = "black" if mover == chess.WHITE else "white"
    source_proof = None if source_option is None else source_option.proof
    source_regret = (
        None
        if source_option is None
        else _mover_regret(best.score, source_option.score, mover)
    )
    return {
        "split": candidate.split,
        "state_key_sha256": candidate.state_key_sha256,
        "position_hash": root.position_hash,
        "pfen": root.pfen,
        "series_number": root.series_number,
        "mover": mover_name,
        "attempt_index": candidate.attempt_index,
        "sequence_index": candidate.sequence_index,
        "white_profile_id": candidate.white_profile_id,
        "black_profile_id": candidate.black_profile_id,
        "source_profile_id": candidate.source_profile_id,
        "source_series_remaining": candidate.source_series_remaining,
        "source_played_series": source_series,
        "source_terminal": candidate.sample.terminal.name.lower(),
        "source_value_for_side_to_move": candidate.sample.value_for_side_to_move,
        "root_features": CachedFeatures.from_state(root).as_dict(),
        "teacher_best_series": best_series,
        "teacher_score_white_heuristic_points": result.score,
        "teacher_proof": result.proof,
        "teacher_forced": result.forced,
        "teacher_best_proof_bounds": list(best.proof_bounds),
        "teacher_best_proof": best.proof,
        "teacher_signed_mate_distance_series": _mate_distance(best.score),
        "teacher_agrees_with_source_play": best_series == source_series,
        "source_played_in_retained_alternatives": source_option is not None,
        "source_played_regret_points": source_regret,
        "source_played_proof": source_proof,
        "source_played_proven_adverse": source_proof == opponent_name,
        "teacher_best_proven_for_mover": best.proof == mover_name,
        "search": {
            "requested_depth_series": result.requested_depth,
            "completed_depth_series": result.completed_depth,
            "branch_cap": result.max_series_per_node,
            "max_generation_positions": result.max_generation_positions,
            "work_positions": result.stats.work_positions,
            "elapsed_seconds": result.elapsed_seconds,
            "exact_width": result.exact_width,
            "root_scores_complete": result.root_scores_complete,
            "retained_alternative_count": len(result.alternatives),
            "continue_after_root_mate": True,
            "timed_out": result.timed_out,
            "work_limit_reached": result.work_limit_reached,
        },
        "hard_negatives": [
            {
                "series": item["series"],
                "mover_regret_points": item["mover_regret_points"],
                "proof_bounds": item["proof_bounds"],
                "reasons": item["hard_negative_reasons"],
            }
            for item in options
            if item["is_hard_negative"]
        ],
        "options": options,
    }


def _recover_source_series(
    candidate: _Candidate,
    source_config: NativeCorpusConfig,
    source_profiles: Sequence[NativeCorpusProfile],
) -> str:
    batch = generate_native_full_game_batch(
        source_config,
        source_profiles,
        first_attempt=candidate.attempt_index,
        attempt_count=1,
    )
    record = batch.records[0]
    if not record.accepted:
        raise ValueError("persisted accepted attempt regenerated as rejected")
    game = replay_native_full_game(record)
    if candidate.sequence_index >= len(game.results):
        raise ValueError("candidate points at a terminal boundary with no source series")
    state = game.states[candidate.sequence_index]
    if progressive_state_dedup_key(state).hex() != candidate.state_key_sha256:
        raise ValueError("regenerated source state differs from persisted corpus")
    if (
        record.white_profile_index != candidate.sample.white_profile_index
        or record.black_profile_index != candidate.sample.black_profile_index
    ):
        raise ValueError("regenerated source profile pair differs from persisted corpus")
    return game.results[candidate.sequence_index].machine_notation


def _receipt_path(job: _BucketJob, candidate: _Candidate) -> Path:
    # Keep the path comfortably below legacy Windows MAX_PATH. The full
    # contract/profile/state identities remain inside the digest-checked file.
    return Path(job.receipt_root) / job.cache_contract_id.removeprefix(
        "spc-native-teacher-cache-"
    ) / (
        f"{candidate.split[0]}-{candidate.source_profile_id.removeprefix('spc-')}-"
        f"s{candidate.sample.state.series_number}-{candidate.state_key_sha256}.json"
    )


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        # Repeating the already-long receipt filename in the temporary name can
        # cross legacy Windows MAX_PATH even when the final path is valid.
        # Atomicity comes from same-directory creation + replace, not the name.
        prefix=".tmp-",
        suffix=".json",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _receipt_digest(payload: Mapping[str, Any]) -> str:
    deterministic = {
        key: value
        for key, value in payload.items()
        if key not in {"receipt_sha256", "runtime"}
    }
    return _sha256(deterministic)


def _write_root_receipt(
    job: _BucketJob,
    candidate: _Candidate,
    *,
    status: str,
    label: Mapping[str, Any] | None = None,
    failure: Mapping[str, Any] | None = None,
    elapsed_seconds: float,
) -> dict[str, Any]:
    if status not in {"complete", "incomplete", "error"}:
        raise ValueError("unsupported root receipt status")
    if (status == "complete") != (label is not None):
        raise ValueError("complete root receipt must contain exactly one label")
    if (status != "complete") != (failure is not None):
        raise ValueError("non-complete root receipt must contain a failure")
    payload: dict[str, Any] = {
        "schema": "spc-native-deep-teacher-root-receipt-v1",
        "cache_contract_id": job.cache_contract_id,
        "status": status,
        "split": candidate.split,
        "source_profile_id": candidate.source_profile_id,
        "series_number": candidate.sample.state.series_number,
        "state_key_sha256": candidate.state_key_sha256,
        "label": None if label is None else dict(label),
        "failure": None if failure is None else dict(failure),
        "runtime": {"elapsed_seconds": elapsed_seconds},
    }
    payload["receipt_sha256"] = _receipt_digest(payload)
    _atomic_json(_receipt_path(job, candidate), payload)
    return payload


def _load_root_receipt(
    job: _BucketJob, candidate: _Candidate
) -> dict[str, Any] | None:
    path = _receipt_path(job, candidate)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid teacher root receipt {path}: {error}") from error
    if not isinstance(payload, dict) or payload.get("receipt_sha256") != _receipt_digest(
        payload
    ):
        raise ValueError(f"teacher root receipt digest mismatch: {path}")
    expected = {
        "schema": "spc-native-deep-teacher-root-receipt-v1",
        "cache_contract_id": job.cache_contract_id,
        "split": candidate.split,
        "source_profile_id": candidate.source_profile_id,
        "series_number": candidate.sample.state.series_number,
        "state_key_sha256": candidate.state_key_sha256,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError(f"teacher root receipt binding mismatch: {path}")
    status = payload.get("status")
    if status == "complete":
        label = payload.get("label")
        if not isinstance(label, dict) or payload.get("failure") is not None:
            raise ValueError(f"complete teacher root receipt is malformed: {path}")
        search = label.get("search")
        if (
            label.get("state_key_sha256") != candidate.state_key_sha256
            or not isinstance(search, dict)
            or search.get("requested_depth_series")
            != job.teacher_config.depth_series
            or search.get("completed_depth_series")
            != job.teacher_config.depth_series
            or search.get("branch_cap") != job.teacher_config.branch_cap
            or search.get("max_generation_positions")
            != job.teacher_config.max_generation_positions
            or search.get("timed_out") is not False
            or search.get("work_limit_reached") is not False
            or search.get("root_scores_complete") is not True
            or search.get("continue_after_root_mate") is not True
        ):
            raise ValueError(f"cached teacher label is incomplete or drifted: {path}")
    elif status in {"incomplete", "error"}:
        if not isinstance(payload.get("failure"), dict) or payload.get("label") is not None:
            raise ValueError(f"failed teacher root receipt is malformed: {path}")
    else:
        raise ValueError(f"teacher root receipt status is invalid: {path}")
    return payload


def _run_bucket(job: _BucketJob) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    started = time.perf_counter()
    limits = SearchLimits(
        depth_series=job.teacher_config.depth_series,
        max_series_per_node=job.teacher_config.branch_cap,
        time_limit_seconds=None,
        max_generation_positions=job.teacher_config.max_generation_positions,
        collect_all_root_scores=True,
        native_threads=1,
        continue_after_root_mate=True,
    )
    for candidate in job.candidates:
        if len(accepted) >= job.quota:
            break
        if candidate.state_key_sha256 in job.forbidden_state_keys:
            excluded.append(
                {
                    "split": job.split,
                    "source_profile_id": job.source_profile_id,
                    "series_number": job.series_number,
                    "state_key_sha256": candidate.state_key_sha256,
                    "attempt_index": candidate.attempt_index,
                    "sequence_index": candidate.sequence_index,
                    "reason": "forbidden-cross-split-root-state",
                    "forbidden_state_keys": [candidate.state_key_sha256],
                }
            )
            continue
        receipt = _load_root_receipt(job, candidate)
        if receipt is not None:
            if receipt["status"] == "complete":
                label = receipt["label"]
                forbidden_hits = sorted(
                    {
                        str(option["final_state_key_sha256"])
                        for option in label["options"]
                    }
                    & job.forbidden_state_keys
                )
                if forbidden_hits:
                    excluded.append(
                        {
                            "split": job.split,
                            "source_profile_id": job.source_profile_id,
                            "series_number": job.series_number,
                            "state_key_sha256": candidate.state_key_sha256,
                            "attempt_index": candidate.attempt_index,
                            "sequence_index": candidate.sequence_index,
                            "reason": "forbidden-cross-split-option-final-state",
                            "forbidden_state_keys": forbidden_hits,
                        }
                    )
                else:
                    accepted.append(label)
            else:
                failures.append(receipt["failure"])
            continue
        root_started = time.perf_counter()
        try:
            result = analyze(candidate.sample.state, limits, profile=job.teacher_profile)
            incomplete = _complete_result_or_reason(result)
            if incomplete is not None:
                failure = {
                    "split": job.split,
                    "source_profile_id": job.source_profile_id,
                    "series_number": job.series_number,
                    "state_key_sha256": candidate.state_key_sha256,
                    "attempt_index": candidate.attempt_index,
                    "sequence_index": candidate.sequence_index,
                    "reason": incomplete,
                    "completed_depth": result.completed_depth,
                    "root_scores_complete": result.root_scores_complete,
                    "work_positions": result.stats.work_positions,
                }
                failures.append(failure)
                _write_root_receipt(
                    job,
                    candidate,
                    status="incomplete",
                    failure=failure,
                    elapsed_seconds=time.perf_counter() - root_started,
                )
                continue
            source_series = _recover_source_series(
                candidate, job.source_config, job.source_profiles
            )
            label = _serialize_complete_label(
                candidate,
                result,
                source_series,
                job.teacher_config,
            )
            _write_root_receipt(
                job,
                candidate,
                status="complete",
                label=label,
                elapsed_seconds=time.perf_counter() - root_started,
            )
            forbidden_hits = sorted(
                {
                    str(option["final_state_key_sha256"])
                    for option in label["options"]
                }
                & job.forbidden_state_keys
            )
            if forbidden_hits:
                excluded.append(
                    {
                        "split": job.split,
                        "source_profile_id": job.source_profile_id,
                        "series_number": job.series_number,
                        "state_key_sha256": candidate.state_key_sha256,
                        "attempt_index": candidate.attempt_index,
                        "sequence_index": candidate.sequence_index,
                        "reason": "forbidden-cross-split-option-final-state",
                        "forbidden_state_keys": forbidden_hits,
                    }
                )
            else:
                accepted.append(label)
        except BaseException as error:
            failure = {
                "split": job.split,
                "source_profile_id": job.source_profile_id,
                "series_number": job.series_number,
                "state_key_sha256": candidate.state_key_sha256,
                "attempt_index": candidate.attempt_index,
                "sequence_index": candidate.sequence_index,
                "reason": "exception",
                "error": f"{type(error).__name__}: {error}",
            }
            failures.append(failure)
            _write_root_receipt(
                job,
                candidate,
                status="error",
                failure=failure,
                elapsed_seconds=time.perf_counter() - root_started,
            )
    return {
        "split": job.split,
        "source_profile_id": job.source_profile_id,
        "series_number": job.series_number,
        "quota": job.quota,
        "candidate_count": len(job.candidates),
        "accepted": accepted,
        "failures": failures,
        "excluded": excluded,
        "elapsed_seconds": time.perf_counter() - started,
    }


def _single_candidate_job(job: _BucketJob, candidate: _Candidate) -> _BucketJob:
    """Keep one candidate's receipt bindings while exposing root-level parallelism."""

    return replace(job, quota=1, candidates=(candidate,))


def _validated_single_candidate_result(
    job: _BucketJob,
    candidate: _Candidate,
    result: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    expected = {
        "split": job.split,
        "source_profile_id": job.source_profile_id,
        "series_number": job.series_number,
        "quota": 1,
        "candidate_count": 1,
    }
    if any(result.get(key) != value for key, value in expected.items()):
        raise ValueError(
            "single-candidate teacher result binding drifted for "
            f"{candidate.state_key_sha256}"
        )
    outcomes: list[list[dict[str, Any]]] = []
    for key in ("accepted", "failures", "excluded"):
        rows = result.get(key)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(
                f"single-candidate teacher result {key} is malformed for "
                f"{candidate.state_key_sha256}"
            )
        outcomes.append(rows)
    accepted, failures, excluded = outcomes
    if len(accepted) + len(failures) + len(excluded) != 1:
        raise ValueError(
            "single-candidate teacher result must contain exactly one outcome for "
            f"{candidate.state_key_sha256}"
        )
    row = (accepted or failures or excluded)[0]
    if row.get("state_key_sha256") != candidate.state_key_sha256:
        raise ValueError(
            "single-candidate teacher result state key drifted for "
            f"{candidate.state_key_sha256}"
        )
    return accepted, failures, excluded


@dataclass(slots=True)
class _BucketProgress:
    job: _BucketJob
    started: float
    next_candidate_index: int = 0
    accepted: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    pending_indices: set[int] = field(default_factory=set)
    pending_results: dict[int, Mapping[str, Any]] = field(default_factory=dict)
    finished: float | None = None


def _run_buckets_parallel(
    jobs: Sequence[_BucketJob],
    *,
    workers: int,
    executor_factory: Callable[..., Executor] = ProcessPoolExecutor,
) -> list[dict[str, Any]]:
    """Run deterministic quota-sized candidate waves over one shared pool.

    A bucket's next wave is exactly its remaining quota.  Therefore every
    speculative root belongs to the same candidate prefix that the sequential
    scheduler would have consumed: a final wave cannot overshoot the quota,
    and failures only open an equally sized replacement wave.  Worker finish
    order may vary, but results are committed strictly by candidate index.
    """

    if workers < 1:
        raise ValueError("teacher scheduler workers must be positive")
    progress = [_BucketProgress(job=job, started=time.perf_counter()) for job in jobs]
    future_bindings: dict[Any, tuple[int, int, _Candidate]] = {}

    def submit_wave(pool: Executor, bucket_index: int) -> None:
        bucket = progress[bucket_index]
        if bucket.pending_indices:
            raise AssertionError("teacher bucket already has an active candidate wave")
        remaining = bucket.job.quota - len(bucket.accepted)
        if remaining <= 0 or bucket.next_candidate_index >= len(bucket.job.candidates):
            bucket.finished = time.perf_counter()
            return
        stop = min(
            len(bucket.job.candidates), bucket.next_candidate_index + remaining
        )
        indices = tuple(range(bucket.next_candidate_index, stop))
        bucket.next_candidate_index = stop
        bucket.pending_indices.update(indices)
        for candidate_index in indices:
            candidate = bucket.job.candidates[candidate_index]
            future = pool.submit(
                _run_bucket, _single_candidate_job(bucket.job, candidate)
            )
            future_bindings[future] = (bucket_index, candidate_index, candidate)

    with executor_factory(max_workers=workers) as pool:
        for bucket_index in range(len(progress)):
            submit_wave(pool, bucket_index)
        while future_bindings:
            completed, _pending = wait(
                tuple(future_bindings), return_when=FIRST_COMPLETED
            )
            ready_buckets: set[int] = set()
            for future in completed:
                bucket_index, candidate_index, candidate = future_bindings.pop(future)
                bucket = progress[bucket_index]
                try:
                    bucket.pending_results[candidate_index] = future.result()
                except BaseException as error:
                    raise RuntimeError(
                        "teacher root worker failed for "
                        f"{candidate.state_key_sha256}"
                    ) from error
                bucket.pending_indices.remove(candidate_index)
                if not bucket.pending_indices:
                    ready_buckets.add(bucket_index)
            for bucket_index in sorted(ready_buckets):
                bucket = progress[bucket_index]
                for candidate_index in sorted(bucket.pending_results):
                    candidate = bucket.job.candidates[candidate_index]
                    accepted, failures, excluded = _validated_single_candidate_result(
                        bucket.job,
                        candidate,
                        bucket.pending_results[candidate_index],
                    )
                    bucket.accepted.extend(accepted)
                    bucket.failures.extend(failures)
                    bucket.excluded.extend(excluded)
                bucket.pending_results.clear()
                submit_wave(pool, bucket_index)

    results: list[dict[str, Any]] = []
    for bucket in progress:
        if bucket.pending_indices or bucket.pending_results:
            raise AssertionError("teacher bucket scheduler ended with pending results")
        finished = bucket.finished or time.perf_counter()
        results.append(
            {
                "split": bucket.job.split,
                "source_profile_id": bucket.job.source_profile_id,
                "series_number": bucket.job.series_number,
                "quota": bucket.job.quota,
                "candidate_count": len(bucket.job.candidates),
                "accepted": bucket.accepted,
                "failures": bucket.failures,
                "excluded": bucket.excluded,
                "elapsed_seconds": finished - bucket.started,
            }
        )
    return results


def _same_nonseed_contract(
    train: NativeCorpusConfig, holdout: NativeCorpusConfig
) -> bool:
    train_payload = train.as_semantic_dict()
    holdout_payload = holdout.as_semantic_dict()
    del train_payload["seed"]
    del holdout_payload["seed"]
    return train_payload == holdout_payload


def _resolve_receipt_cache_contract(
    current_payload: Mapping[str, Any],
    prior_contract: Mapping[str, Any] | None,
    receipt_root: Path,
) -> tuple[dict[str, Any], str]:
    if prior_contract is None:
        payload = dict(current_payload)
        return payload, "spc-native-teacher-cache-" + _sha256(payload)[:20]

    expected_keys = {*current_payload, "cache_contract_id", "receipt_root"}
    if set(prior_contract) != expected_keys:
        raise ValueError("prior receipt cache contract fields drifted")
    prior_payload = {
        key: value
        for key, value in prior_contract.items()
        if key not in {"cache_contract_id", "receipt_root"}
    }
    prior_source_fingerprint = prior_payload.get("source_fingerprint")
    if not isinstance(prior_source_fingerprint, str) or len(
        prior_source_fingerprint
    ) != 16 or any(
        character not in "0123456789abcdef"
        for character in prior_source_fingerprint
    ):
        raise ValueError("prior receipt cache source fingerprint is malformed")
    current_compatible = dict(current_payload)
    prior_compatible = dict(prior_payload)
    del current_compatible["source_fingerprint"]
    del prior_compatible["source_fingerprint"]
    if prior_compatible != current_compatible:
        raise ValueError("prior receipt cache label contract drifted")
    cache_contract_id = "spc-native-teacher-cache-" + _sha256(prior_payload)[:20]
    if prior_contract.get("cache_contract_id") != cache_contract_id:
        raise ValueError("prior receipt cache contract ID is invalid")
    if Path(str(prior_contract.get("receipt_root"))).resolve() != receipt_root:
        raise ValueError("prior receipt cache root is not the requested receipt root")
    return prior_payload, cache_contract_id


def _validate_replacement_cache_source(
    cache_source_fingerprint: str,
    current_source_fingerprint: str,
    forbidden_train_state_keys: frozenset[str],
    forbidden_holdout_state_keys: frozenset[str],
) -> None:
    if (
        (forbidden_train_state_keys or forbidden_holdout_state_keys)
        and cache_source_fingerprint != current_source_fingerprint
    ):
        raise ValueError(
            "a replacement label cannot be generated from a cross-source receipt "
            "cache; regenerate the corpus and receipts under the current runtime"
        )


def build_native_teacher_corpus(
    train_store: CorpusStore,
    holdout_store: CorpusStore,
    teacher_profile: EngineProfile,
    *,
    config: NativeTeacherConfig | None = None,
    run_tactical_gate: bool = True,
    receipt_root: str | Path | None = None,
    forbidden_train_option_final_state_keys: Iterable[str] = (),
    forbidden_train_state_keys: Iterable[str] = (),
    forbidden_holdout_state_keys: Iterable[str] = (),
    prior_receipt_cache_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or NativeTeacherConfig()
    forbidden_train_state_keys = frozenset(
        str(key)
        for key in (
            *forbidden_train_option_final_state_keys,
            *forbidden_train_state_keys,
        )
    )
    forbidden_holdout_state_keys = frozenset(
        str(key) for key in forbidden_holdout_state_keys
    )
    malformed_forbidden_keys = sorted(
        key
        for key in (*forbidden_train_state_keys, *forbidden_holdout_state_keys)
        if len(key) != 64 or any(character not in "0123456789abcdef" for character in key)
    )
    if malformed_forbidden_keys:
        raise ValueError("forbidden train option final-state keys must be SHA-256 hex")
    native_mate_identity = native_mate_runtime_identity()
    if not native_subtree_available() or native_mate_identity == "unavailable":
        raise RuntimeError(
            "deep teacher labeling requires source-matched native subtree and mate kernels"
        )
    label_runtime_identities = _label_runtime_identities(native_mate_identity)
    if train_store.root == holdout_store.root:
        raise ValueError("train and holdout roots must be distinct")
    train_contract = read_native_generation_contract(train_store.root)
    holdout_contract = read_native_generation_contract(holdout_store.root)
    NativeCorpusGenerationContract(
        train_config=train_contract.config,
        holdout_config=holdout_contract.config,
        ordered_profiles=train_contract.ordered_profiles,
    )
    if train_contract.ordered_profiles != holdout_contract.ordered_profiles:
        raise ValueError("train and holdout ordered profile records differ")
    if not _same_nonseed_contract(train_contract.config, holdout_contract.config):
        raise ValueError("train and holdout native generation settings differ beyond seed")
    if train_contract.config.schedule is not NativeProfileSchedule.ORDERED_PAIR_ROUND_ROBIN:
        raise ValueError("deep teacher pilot requires ordered-pair round-robin profiles")
    profile_ids = train_store.identity.profile_ids
    if len(profile_ids) != 4:
        raise ValueError("deep teacher pilot requires exactly four source profiles")
    train_manifest = train_store.verify()
    holdout_manifest = holdout_store.verify()
    if train_manifest["attempt_count"] != config.expected_train_attempts:
        raise ValueError(
            f"train corpus has {train_manifest['attempt_count']} attempts; "
            f"expected {config.expected_train_attempts}"
        )
    if holdout_manifest["attempt_count"] != config.expected_holdout_attempts:
        raise ValueError(
            f"holdout corpus has {holdout_manifest['attempt_count']} attempts; "
            f"expected {config.expected_holdout_attempts}"
        )
    current_cache_contract_payload = {
        "schema": "spc-native-deep-teacher-cache-contract-v1",
        "method": NATIVE_TEACHER_METHOD,
        "engine_version": ENGINE_VERSION,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "teacher_profile_id": teacher_profile.profile_id,
        "teacher_config": config.as_dict(),
        "train_contract_sha256": train_contract.digest_hex,
        "holdout_contract_sha256": holdout_contract.digest_hex,
        "train_corpus_sha256": train_manifest["corpus_sha256"],
        "holdout_corpus_sha256": holdout_manifest["corpus_sha256"],
        "native_subtree_available": True,
        "native_mate_runtime_identity": native_mate_identity,
        "label_runtime_identities": label_runtime_identities,
    }
    resolved_receipt_root = (
        train_store.root.parent / "deep-teacher-root-receipts"
        if receipt_root is None
        else Path(receipt_root).expanduser().resolve()
    )
    cache_contract_payload, cache_contract_id = _resolve_receipt_cache_contract(
        current_cache_contract_payload,
        prior_receipt_cache_contract,
        resolved_receipt_root,
    )
    _validate_replacement_cache_source(
        str(cache_contract_payload["source_fingerprint"]),
        ENGINE_SOURCE_FINGERPRINT,
        forbidden_train_state_keys,
        forbidden_holdout_state_keys,
    )
    started = time.perf_counter()
    train_candidates, train_keys, train_population = _candidate_population(
        train_store,
        split="train",
        config=config,
        excluded_keys=set(),
    )
    holdout_candidates, holdout_keys, holdout_population = _candidate_population(
        holdout_store,
        split="holdout",
        config=config,
        excluded_keys=train_keys,
    )
    overlap = train_keys & holdout_keys
    if set(train_candidates) & set(holdout_candidates):
        raise AssertionError("exact train/holdout state overlap survived filtering")
    quotas = _balanced_quotas(profile_ids, config)
    grouped: dict[tuple[str, str, int], list[_Candidate]] = defaultdict(list)
    for candidate in (*train_candidates.values(), *holdout_candidates.values()):
        grouped[(candidate.split, *candidate.cell)].append(candidate)
    jobs: list[_BucketJob] = []
    for cell, quota in sorted(quotas.items()):
        candidates = tuple(
            sorted(
                grouped.get(cell, ()),
                key=lambda item: _teacher_candidate_order(
                    config.seed, item, config.selection_mode
                ),
            )
        )
        if len(candidates) < quota:
            raise ValueError(
                f"balanced cell {cell} has {len(candidates)} candidates for quota {quota}"
            )
        jobs.append(
            _BucketJob(
                split=cell[0],
                source_profile_id=cell[1],
                series_number=cell[2],
                quota=quota,
                candidates=candidates,
                source_config=(
                    train_contract.config if cell[0] == "train" else holdout_contract.config
                ),
                source_profiles=train_contract.ordered_profiles,
                teacher_profile=teacher_profile,
                teacher_config=config,
                receipt_root=str(resolved_receipt_root),
                cache_contract_id=cache_contract_id,
                forbidden_state_keys=(
                    forbidden_train_state_keys
                    if cell[0] == "train"
                    else forbidden_holdout_state_keys
                ),
            )
        )
    bucket_results: list[dict[str, Any]] = []
    if config.workers == 1:
        bucket_results = [_run_bucket(job) for job in jobs]
    else:
        bucket_results = _run_buckets_parallel(jobs, workers=config.workers)
    bucket_results.sort(
        key=lambda item: (
            str(item["split"]),
            str(item["source_profile_id"]),
            int(item["series_number"]),
        )
    )
    labels = sorted(
        (
            label
            for bucket in bucket_results
            for label in bucket["accepted"]
        ),
        key=lambda item: (
            str(item["split"]),
            str(item["source_profile_id"]),
            int(item["series_number"]),
            str(item["state_key_sha256"]),
        ),
    )
    failures = [
        failure for bucket in bucket_results for failure in bucket["failures"]
    ]
    excluded = [
        exclusion for bucket in bucket_results for exclusion in bucket["excluded"]
    ]
    shortages = [
        {
            "split": bucket["split"],
            "source_profile_id": bucket["source_profile_id"],
            "series_number": bucket["series_number"],
            "quota": bucket["quota"],
            "accepted": len(bucket["accepted"]),
        }
        for bucket in bucket_results
        if len(bucket["accepted"]) != bucket["quota"]
    ]
    if len({str(label["state_key_sha256"]) for label in labels}) != len(labels):
        raise AssertionError("deep teacher output contains duplicate states")
    tactical = (
        run_rules_tactical_gate(
            teacher_profile,
            search_depth=config.depth_series,
            max_series_per_node=config.branch_cap,
            max_generation_positions=config.max_generation_positions,
        ).as_dict()
        if run_tactical_gate
        else {"passed": None, "checks": [], "skipped": True}
    )
    agreement_count = sum(bool(label["teacher_agrees_with_source_play"]) for label in labels)
    source_present = sum(
        bool(label["source_played_in_retained_alternatives"]) for label in labels
    )
    proof_counts = Counter(str(label["teacher_best_proof"] or "unknown") for label in labels)
    failure_counts = Counter(str(item["reason"]) for item in failures)
    balance_counts = Counter(
        (str(label["split"]), str(label["source_profile_id"]), int(label["series_number"]))
        for label in labels
    )
    train_root_keys = {
        str(label["state_key_sha256"])
        for label in labels
        if label["split"] == "train"
    }
    holdout_root_keys = {
        str(label["state_key_sha256"])
        for label in labels
        if label["split"] == "holdout"
    }
    train_option_keys = {
        str(option["final_state_key_sha256"])
        for label in labels
        if label["split"] == "train"
        for option in label["options"]
    }
    holdout_option_keys = {
        str(option["final_state_key_sha256"])
        for label in labels
        if label["split"] == "holdout"
        for option in label["options"]
    }
    option_final_overlap = sorted(train_option_keys & holdout_option_keys)
    train_option_to_holdout_root = sorted(train_option_keys & holdout_root_keys)
    holdout_option_to_train_root = sorted(holdout_option_keys & train_root_keys)
    total_work = sum(int(label["search"]["work_positions"]) for label in labels)
    deterministic = {
        "schema": NATIVE_TEACHER_SCHEMA,
        "method": NATIVE_TEACHER_METHOD,
        "engine_version": ENGINE_VERSION,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "teacher_profile": teacher_profile.as_dict(),
        "config": config.as_dict(),
        "generation": {
            "train_contract_sha256": train_contract.digest_hex,
            "holdout_contract_sha256": holdout_contract.digest_hex,
            "train_corpus_sha256": train_manifest["corpus_sha256"],
            "holdout_corpus_sha256": holdout_manifest["corpus_sha256"],
            "ordered_profile_ids": list(profile_ids),
            "profile_schedule": "ordered-pair-round-robin",
            "train_attempts": train_manifest["attempt_count"],
            "holdout_attempts": holdout_manifest["attempt_count"],
            "prior_receipt_cache_reuse": prior_receipt_cache_contract is not None,
            "root_receipt_cache_contract": {
                **cache_contract_payload,
                "cache_contract_id": cache_contract_id,
                "receipt_root": str(resolved_receipt_root),
            },
        },
        "selection": {
            "train_population": train_population,
            "holdout_population": holdout_population,
            "exact_overlap_states_removed_from_holdout": len(overlap),
            "selected_root_exact_overlap_states": len(
                train_root_keys & holdout_root_keys
            ),
            "cross_split_option_final_exact_overlap_states": len(
                option_final_overlap
            ),
            "cross_split_option_final_exact_overlap_state_keys": (
                option_final_overlap
            ),
            "train_option_final_to_holdout_root_overlap_states": len(
                train_option_to_holdout_root
            ),
            "train_option_final_to_holdout_root_overlap_state_keys": (
                train_option_to_holdout_root
            ),
            "holdout_option_final_to_train_root_overlap_states": len(
                holdout_option_to_train_root
            ),
            "holdout_option_final_to_train_root_overlap_state_keys": (
                holdout_option_to_train_root
            ),
            "forbidden_train_state_keys": sorted(forbidden_train_state_keys),
            "forbidden_holdout_state_keys": sorted(forbidden_holdout_state_keys),
            "receipt_cache_source_fingerprint": cache_contract_payload[
                "source_fingerprint"
            ],
            "receipt_cache_reused_across_source_fingerprint": (
                cache_contract_payload["source_fingerprint"]
                != ENGINE_SOURCE_FINGERPRINT
            ),
            "forbidden_train_roots_excluded": len(excluded),
            "forbidden_train_root_exclusions": sorted(
                excluded,
                key=lambda item: (
                    str(item["source_profile_id"]),
                    int(item["series_number"]),
                    str(item["state_key_sha256"]),
                ),
            ),
            "quota_by_cell": [
                {
                    "split": split,
                    "source_profile_id": profile_id,
                    "series_number": series_number,
                    "quota": quota,
                    "accepted": balance_counts[(split, profile_id, series_number)],
                }
                for (split, profile_id, series_number), quota in sorted(quotas.items())
            ],
        },
        "labels": labels,
        "quality": {
            "status": "complete" if not shortages and len(labels) == config.target_roots else "incomplete",
            "accepted_roots": len(labels),
            "train_roots": sum(label["split"] == "train" for label in labels),
            "holdout_roots": sum(label["split"] == "holdout" for label in labels),
            "teacher_source_agreements": agreement_count,
            "teacher_source_agreement_rate": (
                agreement_count / len(labels) if labels else None
            ),
            "source_played_in_retained_alternatives": source_present,
            "source_played_retained_rate": source_present / len(labels) if labels else None,
            "teacher_best_proof_counts": dict(sorted(proof_counts.items())),
            "teacher_mate_labels": sum(
                label["teacher_signed_mate_distance_series"] is not None for label in labels
            ),
            "exact_width_labels": sum(bool(label["search"]["exact_width"]) for label in labels),
            "root_scores_complete_labels": sum(
                bool(label["search"]["root_scores_complete"]) for label in labels
            ),
            "source_played_proven_adverse": sum(
                bool(label["source_played_proven_adverse"]) for label in labels
            ),
            "hard_negative_rows": sum(len(label["hard_negatives"]) for label in labels),
            "label_search_failures": len(failures),
            "label_search_failure_counts": dict(sorted(failure_counts.items())),
            "shortages": shortages,
            "tactical_gate": tactical,
            "tactical_failures": [
                check for check in tactical.get("checks", []) if not check.get("passed")
            ],
            "proof_failure_scope": (
                "source_played_proven_adverse counts sampled source moves whose retained "
                "depth-3 proof bounds favor the opponent; unknown bounds are not called failures"
            ),
        },
        "failure_diagnostics": failures,
        "contract": {
            "incomplete_labels_cached": False,
            "full_retained_root_scores_required": True,
            "exact_width_required": False,
            "exact_width_scope": (
                "branch cap 32 is selective when more legal series exist; every retained "
                "alternative must still have an exact completed depth-3 score"
            ),
            "source_move_contract": (
                "the sampled native attempt is regenerated from its bound seed/config and "
                "replayed; agreement is against the exact series actually played"
            ),
            "strength_claim": False,
        },
    }
    corpus_id = "spc-native-teacher-" + _sha256(deterministic)[:20]
    return {
        **deterministic,
        "corpus_id": corpus_id,
        "runtime": {
            "elapsed_seconds": time.perf_counter() - started,
            "worker_processes": min(config.workers, len(jobs)),
            "total_label_work_positions": total_work,
            "labels_per_second": len(labels) / max(time.perf_counter() - started, 1e-12),
            "bucket_elapsed_seconds_sum": sum(
                float(bucket["elapsed_seconds"]) for bucket in bucket_results
            ),
            "host_cpu_count": os.cpu_count(),
        },
    }


def write_native_teacher_artifact(payload: Mapping[str, Any], path: str | Path) -> Path:
    from .selfplay_training import write_selfplay_artifact

    return write_selfplay_artifact(payload, path)


def _validated_mixed_tier(
    payload: Mapping[str, Any],
    *,
    tier_name: str,
    expected_config: NativeTeacherConfig,
) -> list[dict[str, Any]]:
    if payload.get("schema") != NATIVE_TEACHER_SCHEMA:
        raise ValueError(f"{tier_name} teacher schema is unsupported")
    if payload.get("method") != NATIVE_TEACHER_METHOD:
        raise ValueError(f"{tier_name} teacher method is unsupported")
    config = payload.get("config")
    quality = payload.get("quality")
    contract = payload.get("contract")
    selection = payload.get("selection")
    labels = payload.get("labels")
    if not all(
        isinstance(item, Mapping)
        for item in (config, quality, contract, selection)
    ) or not isinstance(labels, list):
        raise ValueError(f"{tier_name} teacher artifact is malformed")
    expected_config_payload = expected_config.as_dict()
    if dict(config) != expected_config_payload:
        raise ValueError(f"{tier_name} teacher config differs from its frozen contract")
    target_roots = expected_config.target_roots
    train_roots = expected_config.train_roots
    depth_series = expected_config.depth_series
    if quality.get("status") != "complete":
        raise ValueError(f"{tier_name} teacher artifact is incomplete")
    if quality.get("accepted_roots") != target_roots:
        raise ValueError(f"{tier_name} accepted-root count drifted")
    if quality.get("train_roots") != train_roots:
        raise ValueError(f"{tier_name} train-root count drifted")
    if quality.get("holdout_roots") != target_roots - train_roots:
        raise ValueError(f"{tier_name} holdout-root count drifted")
    if quality.get("label_search_failures") != 0:
        # Failed candidate attempts are permitted and remain in atomic receipts;
        # a complete tier must not carry any failed row in its final artifact.
        # The builder currently retains those diagnostics even after filling a
        # quota, so only labels themselves are completeness-authoritative here.
        failure_diagnostics = payload.get("failure_diagnostics")
        if not isinstance(failure_diagnostics, list):
            raise ValueError(f"{tier_name} failure diagnostics are malformed")
    if contract.get("incomplete_labels_cached") is not False:
        raise ValueError(f"{tier_name} permits incomplete cached labels")
    if contract.get("full_retained_root_scores_required") is not True:
        raise ValueError(f"{tier_name} does not require full retained scores")
    generation = payload.get("generation")
    profile_ids = (
        generation.get("ordered_profile_ids")
        if isinstance(generation, Mapping)
        else None
    )
    if (
        not isinstance(profile_ids, list)
        or len(profile_ids) != 4
        or any(not isinstance(profile_id, str) or not profile_id for profile_id in profile_ids)
        or len(set(profile_ids)) != len(profile_ids)
    ):
        raise ValueError(f"{tier_name} ordered profile IDs are malformed")
    if (
        generation.get("train_attempts")
        != expected_config.expected_train_attempts
        or generation.get("holdout_attempts")
        != expected_config.expected_holdout_attempts
    ):
        raise ValueError(f"{tier_name} generation attempt counts drifted")
    expected_quotas = _balanced_quotas(profile_ids, expected_config)
    quotas = selection.get("quota_by_cell")
    if not isinstance(quotas, list) or len(quotas) != len(expected_quotas):
        raise ValueError(f"{tier_name} cell quotas are incomplete")
    observed_quotas: dict[tuple[str, str, int], int] = {}
    for row in quotas:
        if not isinstance(row, Mapping) or set(row) != {
            "split",
            "source_profile_id",
            "series_number",
            "quota",
            "accepted",
        }:
            raise ValueError(f"{tier_name} has a malformed profile/series cell")
        key = (
            str(row["split"]),
            str(row["source_profile_id"]),
            int(row["series_number"]),
        )
        quota = row.get("quota")
        if (
            type(quota) is not int
            or quota < 0
            or row.get("accepted") != quota
            or key in observed_quotas
        ):
            raise ValueError(f"{tier_name} has an unfilled profile/series cell")
        observed_quotas[key] = quota
    if observed_quotas != expected_quotas:
        raise ValueError(f"{tier_name} profile/series balance drifted")
    if len(labels) != target_roots:
        raise ValueError(f"{tier_name} label count drifted")
    labeled: list[dict[str, Any]] = []
    actual_quotas: Counter[tuple[str, str, int]] = Counter()
    for label in labels:
        if not isinstance(label, Mapping):
            raise ValueError(f"{tier_name} contains a malformed label")
        actual_quotas[
            (
                str(label.get("split")),
                str(label.get("source_profile_id")),
                int(label.get("series_number", -1)),
            )
        ] += 1
        search = label.get("search")
        options = label.get("options")
        if not isinstance(search, Mapping) or not isinstance(options, list) or not options:
            raise ValueError(f"{tier_name} contains a malformed label search")
        if (
            search.get("requested_depth_series") != depth_series
            or search.get("completed_depth_series") != depth_series
            or search.get("root_scores_complete") is not True
            or search.get("timed_out") is not False
            or search.get("work_limit_reached") is not False
        ):
            raise ValueError(f"{tier_name} contains an incomplete label")
        for option in options:
            if not isinstance(option, Mapping):
                raise ValueError(f"{tier_name} contains a malformed option")
            required = {
                "proof_bounds",
                "proof",
                "signed_mate_distance_series",
                "principal_variation",
                "final_state_key_sha256",
                "final_pfen",
                "final_features",
                "is_hard_negative",
                "hard_negative_reasons",
            }
            if not required.issubset(option):
                raise ValueError(f"{tier_name} option provenance is incomplete")
        labeled.append(
            {
                **label,
                "teacher_tier": tier_name,
                "teacher_depth_series": depth_series,
            }
        )
    if dict(actual_quotas) != {
        key: quota for key, quota in expected_quotas.items() if quota
    }:
        raise ValueError(f"{tier_name} actual label balance drifted")

    tactical_gate = quality.get("tactical_gate")
    tactical_failures = quality.get("tactical_failures")
    if expected_config.selection_mode == "quiet-nonterminal":
        if tactical_gate != {"passed": None, "checks": [], "skipped": True}:
            raise ValueError(f"{tier_name} tactical gate was not exactly skipped")
    elif (
        not isinstance(tactical_gate, Mapping)
        or tactical_gate.get("passed") is not True
        or tactical_failures != []
        or not isinstance(tactical_gate.get("checks"), list)
        or not tactical_gate["checks"]
        or any(
            not isinstance(check, Mapping) or check.get("passed") is not True
            for check in tactical_gate["checks"]
        )
    ):
        raise ValueError(f"{tier_name} tactical gate did not pass exactly")
    return labeled


def merge_native_teacher_tiers(
    quiet_depth2: Mapping[str, Any],
    tactical_depth3: Mapping[str, Any],
    *,
    quiet_config: NativeTeacherConfig | None = None,
    tactical_config: NativeTeacherConfig | None = None,
) -> dict[str, Any]:
    """Merge two frozen tiers without blending depth-dependent metrics."""

    quiet_config = quiet_config or NativeTeacherConfig(
        target_roots=144,
        train_roots=96,
        depth_series=2,
        selection_mode="quiet-nonterminal",
    )
    tactical_config = tactical_config or NativeTeacherConfig(
        target_roots=48,
        train_roots=32,
        depth_series=3,
        selection_mode="tactical-low-complexity",
    )
    if quiet_config.selection_mode != "quiet-nonterminal" or quiet_config.depth_series != 2:
        raise ValueError("quiet teacher merge contract must be quiet depth 2")
    if (
        tactical_config.selection_mode != "tactical-low-complexity"
        or tactical_config.depth_series != 3
    ):
        raise ValueError("tactical teacher merge contract must be tactical depth 3")
    shared_config_fields = (
        "minimum_series",
        "maximum_series",
        "branch_cap",
        "max_generation_positions",
        "hard_negative_count",
        "seed",
        "expected_train_attempts",
        "expected_holdout_attempts",
    )
    for field in shared_config_fields:
        if getattr(quiet_config, field) != getattr(tactical_config, field):
            raise ValueError(f"teacher merge contracts disagree on {field}")

    started = time.perf_counter()
    common_fields = ("engine_version", "source_fingerprint", "teacher_profile")
    for field in common_fields:
        if quiet_depth2.get(field) != tactical_depth3.get(field):
            raise ValueError(f"teacher tiers disagree on {field}")
    quiet_generation = quiet_depth2.get("generation")
    tactical_generation = tactical_depth3.get("generation")
    if not isinstance(quiet_generation, Mapping) or not isinstance(
        tactical_generation, Mapping
    ):
        raise ValueError("teacher tier generation provenance is malformed")
    generation_fields = (
        "train_contract_sha256",
        "holdout_contract_sha256",
        "train_corpus_sha256",
        "holdout_corpus_sha256",
        "ordered_profile_ids",
        "profile_schedule",
        "train_attempts",
        "holdout_attempts",
        "prior_receipt_cache_reuse",
    )
    for field in generation_fields:
        if quiet_generation.get(field) != tactical_generation.get(field):
            raise ValueError(f"teacher tiers disagree on generation {field}")

    quiet_labels = _validated_mixed_tier(
        quiet_depth2,
        tier_name="quiet_d2",
        expected_config=quiet_config,
    )
    tactical_labels = _validated_mixed_tier(
        tactical_depth3,
        tier_name="tactical_d3",
        expected_config=tactical_config,
    )
    labels = sorted(
        (*quiet_labels, *tactical_labels),
        key=lambda item: (
            str(item["split"]),
            str(item["teacher_tier"]),
            str(item["source_profile_id"]),
            int(item["series_number"]),
            str(item["state_key_sha256"]),
        ),
    )
    root_keys = [str(label["state_key_sha256"]) for label in labels]
    if len(set(root_keys)) != len(root_keys):
        raise ValueError("teacher tiers contain an exact root-state overlap")
    train_roots = {
        str(label["state_key_sha256"])
        for label in labels
        if label["split"] == "train"
    }
    holdout_roots = {
        str(label["state_key_sha256"])
        for label in labels
        if label["split"] == "holdout"
    }
    train_options = {
        str(option["final_state_key_sha256"])
        for label in labels
        if label["split"] == "train"
        for option in label["options"]
    }
    holdout_options = {
        str(option["final_state_key_sha256"])
        for label in labels
        if label["split"] == "holdout"
        for option in label["options"]
    }
    audits = {
        "selected_root_exact_overlap_states": sorted(train_roots & holdout_roots),
        "cross_split_option_final_exact_overlap_states": sorted(
            train_options & holdout_options
        ),
        "train_option_final_to_holdout_root_overlap_states": sorted(
            train_options & holdout_roots
        ),
        "holdout_option_final_to_train_root_overlap_states": sorted(
            holdout_options & train_roots
        ),
    }
    nonzero = {name: values for name, values in audits.items() if values}
    if nonzero:
        raise ValueError(
            "mixed teacher train/holdout leakage audit failed: "
            + ", ".join(f"{name}={len(values)}" for name, values in nonzero.items())
        )
    tier_payloads = {
        "quiet_d2": {
            "corpus_id": quiet_depth2["corpus_id"],
            "config": quiet_depth2["config"],
            "selection": quiet_depth2["selection"],
            "quality": quiet_depth2["quality"],
            "failure_diagnostics": quiet_depth2["failure_diagnostics"],
            "contract": quiet_depth2["contract"],
            "runtime": quiet_depth2["runtime"],
        },
        "tactical_d3": {
            "corpus_id": tactical_depth3["corpus_id"],
            "config": tactical_depth3["config"],
            "selection": tactical_depth3["selection"],
            "quality": tactical_depth3["quality"],
            "failure_diagnostics": tactical_depth3["failure_diagnostics"],
            "contract": tactical_depth3["contract"],
            "runtime": tactical_depth3["runtime"],
        },
    }
    deterministic = {
        "schema": NATIVE_TEACHER_MIXED_SCHEMA,
        "method": NATIVE_TEACHER_MIXED_METHOD,
        "engine_version": quiet_depth2["engine_version"],
        "source_fingerprint": quiet_depth2["source_fingerprint"],
        "teacher_profile": quiet_depth2["teacher_profile"],
        "generation": {
            field: quiet_generation[field] for field in generation_fields
        },
        "tiers": tier_payloads,
        "labels": labels,
        "selection": {
            "selected_root_exact_overlap_states": 0,
            "cross_split_option_final_exact_overlap_states": 0,
            "train_option_final_to_holdout_root_overlap_states": 0,
            "holdout_option_final_to_train_root_overlap_states": 0,
            "audit_state_keys": audits,
        },
        "quality": {
            "status": "complete",
            "accepted_roots": len(labels),
            "train_roots": len(train_roots),
            "holdout_roots": len(holdout_roots),
            "tier_metrics": {
                "quiet_d2": quiet_depth2["quality"],
                "tactical_d3": tactical_depth3["quality"],
            },
        },
        "contract": {
            "incomplete_labels_cached": False,
            "full_retained_root_scores_required": True,
            "depth_is_per_label_provenance": True,
            "cross_depth_quality_metrics_blended": False,
            "train_holdout_exact_leakage_allowed": False,
            "strength_claim": False,
        },
    }
    return {
        **deterministic,
        "corpus_id": "spc-native-mixed-teacher-" + _sha256(deterministic)[:20],
        "runtime": {
            "merge_elapsed_seconds": time.perf_counter() - started,
            "tier_runtime": {
                "quiet_d2": quiet_depth2["runtime"],
                "tactical_d3": tactical_depth3["runtime"],
            },
        },
    }
