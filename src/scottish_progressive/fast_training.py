from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterable, Mapping, Sequence

import chess

from .evaluation import EvaluationWeights, evaluate
from .model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    Outcome,
    ProgressiveState,
)
from .profiles import EngineProfile
from .rules import play_series
from .search import MATE_SCORE, SearchLimits, analyze


FAST_TRAINING_SCHEMA_VERSION = 1
FAST_TRAINING_SUITE_VERSION = "spc-fast-funnel-v1"
FAST_TRAINING_METHOD = "cached-feature-deeper-research-funnel-v1"
FEATURE_NAMES = (
    "material",
    "king_space",
    "series_reach",
    "promotion_corridors",
    "immediate_vulnerability",
    "useful_mobility",
    "boundary_check",
)
PROXY_DISCLAIMER = (
    "Cached position and short-rollout proxy only; not WDL, not Elo, and not "
    "independent game-strength evidence. Full-game promotion remains required."
)


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(prefix: str, payload: Mapping[str, Any]) -> str:
    return prefix + hashlib.sha256(_canonical_json(payload)).hexdigest()[:20]


def _atomic_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                payload,
                stream,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return destination


@dataclass(frozen=True, slots=True)
class FastTrainingConfig:
    position_limit: int = 32
    rollout_steps: int = 2
    label_depth_series: int = 2
    label_branch_cap: int = 8
    label_max_work_positions: int = 200_000
    finalist_count: int = 3
    stage_two_multiplier: int = 3
    tactical_regression_tolerance: int = 100
    score_clip: int = 5_000
    holdout_percent: int = 25
    seed: int = 20260820
    smoke: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.position_limit <= 64:
            raise ValueError("position_limit must be between 1 and 64")
        if not self.smoke and self.position_limit < 32:
            raise ValueError(
                "full fast-training requires 30 opening boundaries plus "
                "two tactical anchors (32 corpus positions); "
                "use an explicit smoke config only for wiring checks"
            )
        if not 1 <= self.rollout_steps <= 4:
            raise ValueError("rollout_steps must be between 1 and 4")
        if not 1 <= self.label_depth_series <= 4:
            raise ValueError("label_depth_series must be between 1 and 4")
        if not 1 <= self.label_branch_cap <= 128:
            raise ValueError("label_branch_cap must be between 1 and 128")
        if self.label_max_work_positions < 1_000:
            raise ValueError("label_max_work_positions must be at least 1000")
        if not 1 <= self.finalist_count <= 16:
            raise ValueError("finalist_count must be between 1 and 16")
        if not 1 <= self.stage_two_multiplier <= 16:
            raise ValueError("stage_two_multiplier must be between 1 and 16")
        if self.tactical_regression_tolerance < 0:
            raise ValueError("tactical_regression_tolerance cannot be negative")
        if self.score_clip < 100:
            raise ValueError("score_clip must be at least 100")
        if not 1 <= self.holdout_percent <= 50:
            raise ValueError("holdout_percent must be between 1 and 50")

    @property
    def config_id(self) -> str:
        return _digest("spc-fast-config-", self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FastTrainingConfig:
        names = cls.__dataclass_fields__.keys()
        return cls(**{name: payload[name] for name in names if name in payload})

    @classmethod
    def smoke_config(cls, *, seed: int = 7) -> FastTrainingConfig:
        return cls(
            position_limit=4,
            rollout_steps=1,
            label_depth_series=2,
            label_branch_cap=4,
            label_max_work_positions=200_000,
            finalist_count=1,
            seed=seed,
            smoke=True,
        )


@dataclass(frozen=True, slots=True)
class TrainingPosition:
    case_id: str
    fen: str
    series_number: int
    quiet_series: int = 0
    ep_targets: tuple[str, ...] = ()
    trace_id: str = ""
    tactical_anchor: bool = False
    tactical_expected_series: tuple[str, ...] = ()
    suggestion_series: str | None = None
    suggestion_provenance: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("training case_id cannot be empty")
        if self.suggestion_series and self.suggestion_provenance is None:
            raise ValueError("suggestion_series requires explicit provenance")
        if self.suggestion_series:
            provenance = self.suggestion_provenance
            if not isinstance(provenance, Mapping):
                raise ValueError("suggestion provenance must be an object")
            if not str(provenance.get("provider", "")).strip():
                raise ValueError("suggestion provenance requires a provider")
            if provenance.get("code_imported") is not False:
                raise ValueError(
                    "suggestion provenance must explicitly state "
                    "code_imported=false"
                )

    def state(self) -> ProgressiveState:
        return ProgressiveState.from_fen(
            self.fen,
            self.series_number,
            quiet_series=self.quiet_series,
            ep_targets=tuple(chess.parse_square(item) for item in self.ep_targets),
        )

    @classmethod
    def from_state(
        cls,
        case_id: str,
        state: ProgressiveState,
        *,
        trace_id: str = "",
        tactical_anchor: bool = False,
        tactical_expected_series: Sequence[str] = (),
        suggestion_series: str | None = None,
        suggestion_provenance: Mapping[str, Any] | None = None,
    ) -> TrainingPosition:
        return cls(
            case_id=case_id,
            fen=state.board.fen(en_passant="fen"),
            series_number=state.series_number,
            quiet_series=state.quiet_series,
            ep_targets=tuple(
                chess.square_name(square) for square in state.ep_targets
            ),
            trace_id=trace_id or case_id,
            tactical_anchor=tactical_anchor,
            tactical_expected_series=tuple(tactical_expected_series),
            suggestion_series=suggestion_series,
            suggestion_provenance=suggestion_provenance,
        )

    def as_dict(self) -> dict[str, Any]:
        state = self.state()
        return {
            "case_id": self.case_id,
            "fen": self.fen,
            "series_number": self.series_number,
            "quiet_series": self.quiet_series,
            "ep_targets": list(self.ep_targets),
            "trace_id": self.trace_id or self.case_id,
            "tactical_anchor": self.tactical_anchor,
            "tactical_expected_series": list(self.tactical_expected_series),
            "suggestion_series": self.suggestion_series,
            "suggestion_provenance": (
                None
                if self.suggestion_provenance is None
                else dict(self.suggestion_provenance)
            ),
            "position_hash": state.position_hash,
            "pfen": state.pfen,
        }


def default_training_positions() -> tuple[TrainingPosition, ...]:
    # Imported lazily to keep module import acyclic. League imports this module
    # only inside run_league after its canonical 30-case suite is constructed.
    from .league import OPENING_SUITE

    initial = ProgressiveState.initial()
    first_series_by_hash = {
        play_series(initial, (move.uci(),)).final_state.position_hash: move.uci()
        for move in initial.board.legal_moves
    }
    # These two curated anchors have replayed histories documented by the
    # opening-suite tests. Keeping the first complete series explicit prevents
    # them from leaking across the related e4/d4 line-family split.
    arbitrary_anchor_roots = {
        "published-bishop-pressure": "e2e4",
        "published-central-pressure": "d2d4",
    }

    def line_family(case: Any) -> str:
        if case.case_id == "initial":
            return "opening-suite-v4:empty-root-anchor"
        first_uci = first_series_by_hash.get(case.state().position_hash)
        if first_uci is None and case.case_id.startswith("after-"):
            suffix = "-a6-b6"
            if case.case_id.endswith(suffix):
                first_uci = case.case_id[len("after-") : -len(suffix)]
        if first_uci is None:
            first_uci = arbitrary_anchor_roots.get(case.case_id)
        if first_uci is not None:
            return f"opening-suite-v4:first-series:{first_uci}"
        return f"opening-suite-v4:arbitrary-anchor:{case.case_id}"

    opening_positions = tuple(
        TrainingPosition.from_state(
            case.case_id,
            case.state(),
            trace_id=line_family(case),
        )
        for case in OPENING_SUITE
    )
    mate = ProgressiveState.from_fen(
        "7k/8/5KQ1/8/8/8/8/8 w - - 0 1", 1
    )
    countercheck = ProgressiveState.from_fen(
        "4k3/q3R3/8/8/8/8/8/4K3 b - - 0 1", 2
    )
    return (
        TrainingPosition.from_state(
            "fast-tactical-immediate-mate",
            mate,
            trace_id="tactical-immediate-mate",
            tactical_anchor=True,
            tactical_expected_series=("g6g7",),
        ),
        TrainingPosition.from_state(
            "fast-tactical-checked-countercheck",
            countercheck,
            trace_id="tactical-countercheck",
            tactical_anchor=True,
        ),
    ) + opening_positions


@dataclass(frozen=True, slots=True)
class CachedFeatures:
    material: int
    king_space: int
    series_reach: int
    promotion_corridors: int
    immediate_vulnerability: int
    useful_mobility: int
    boundary_check: int
    white_check_distance: int | None
    black_check_distance: int | None
    reach_complete: bool
    white_king_ring_attack_multiplicity: int
    black_king_ring_attack_multiplicity: int
    white_promotable_next_series: int
    black_promotable_next_series: int
    white_king_edge_distance: int
    black_king_edge_distance: int

    @classmethod
    def from_state(cls, state: ProgressiveState) -> CachedFeatures:
        breakdown = evaluate(state, EvaluationWeights())
        board = state.board

        def ring_attacks(attacker: chess.Color, king_color: chess.Color) -> int:
            king = board.king(king_color)
            if king is None:
                return 0
            squares = chess.SquareSet(chess.BB_KING_ATTACKS[king])
            squares.add(king)
            return sum(
                len(board.attackers(attacker, square)) for square in squares
            )

        def promotable(color: chess.Color) -> int:
            budget = (
                state.series_number
                if state.board.turn == color
                else state.series_number + 1
            )
            target_rank = 7 if color == chess.WHITE else 0
            direction = 1 if color == chess.WHITE else -1
            count = 0
            for square in board.pieces(chess.PAWN, color):
                rank = chess.square_rank(square)
                file_index = chess.square_file(square)
                distance = abs(target_rank - rank)
                blocked = any(
                    board.piece_at(chess.square(file_index, next_rank)) is not None
                    for next_rank in range(
                        rank + direction, target_rank + direction, direction
                    )
                )
                if not blocked and distance <= budget:
                    count += 1
            return count

        def edge_distance(color: chess.Color) -> int:
            king = board.king(color)
            if king is None:
                return 0
            file_index, rank = chess.square_file(king), chess.square_rank(king)
            return min(file_index, 7 - file_index, rank, 7 - rank)

        return cls(
            **{name: int(getattr(breakdown, name)) for name in FEATURE_NAMES},
            white_check_distance=breakdown.white_check_distance,
            black_check_distance=breakdown.black_check_distance,
            reach_complete=breakdown.reach_complete,
            white_king_ring_attack_multiplicity=ring_attacks(
                chess.WHITE, chess.BLACK
            ),
            black_king_ring_attack_multiplicity=ring_attacks(
                chess.BLACK, chess.WHITE
            ),
            white_promotable_next_series=promotable(chess.WHITE),
            black_promotable_next_series=promotable(chess.BLACK),
            white_king_edge_distance=edge_distance(chess.WHITE),
            black_king_edge_distance=edge_distance(chess.BLACK),
        )

    def score(self, profile: EngineProfile) -> int:
        return sum(
            round(getattr(self, name) * getattr(profile.weights, name) / 100)
            for name in FEATURE_NAMES
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CachedFeatures:
        return cls(**{name: payload[name] for name in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class CachedOption:
    series: str
    teacher_score_white_heuristic_points: int
    proof: str | None
    outcome: str | None
    ended_by_check: bool
    features: CachedFeatures

    def candidate_score(self, profile: EngineProfile, mover: chess.Color) -> int:
        if self.outcome == Outcome.CHECKMATE.value:
            winner = mover if self.ended_by_check else not mover
            return MATE_SCORE - 1 if winner == chess.WHITE else -MATE_SCORE + 1
        if self.outcome in {
            Outcome.STALEMATE.value,
            Outcome.TEN_SERIES_DRAW.value,
        }:
            return 0
        return self.features.score(profile)

    def as_dict(self) -> dict[str, Any]:
        return {
            "series": self.series,
            "teacher_score_white_heuristic_points": (
                self.teacher_score_white_heuristic_points
            ),
            "proof": self.proof,
            "outcome": self.outcome,
            "ended_by_check": self.ended_by_check,
            "features": self.features.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CachedOption:
        return cls(
            series=str(payload["series"]),
            teacher_score_white_heuristic_points=int(
                payload["teacher_score_white_heuristic_points"]
            ),
            proof=(None if payload.get("proof") is None else str(payload["proof"])),
            outcome=(
                None if payload.get("outcome") is None else str(payload["outcome"])
            ),
            ended_by_check=bool(payload["ended_by_check"]),
            features=CachedFeatures.from_dict(payload["features"]),
        )


@dataclass(frozen=True, slots=True)
class CachedPosition:
    case_id: str
    trace_id: str
    trace_step: int
    split: str
    position_hash: str
    pfen: str
    mover: str
    root_features: CachedFeatures
    label_score_white_heuristic_points: int
    label_best_series: str
    label_proof: str | None
    label_provenance: Mapping[str, Any]
    suggestion_series: str | None
    suggestion_provenance: Mapping[str, Any] | None
    suggestion_agrees_with_label: bool | None
    tactical_anchor: bool
    tactical_expected_series: tuple[str, ...]
    bounded_opponent_mate_check_performed: bool
    bounded_opponent_mate_check_complete: bool
    options: tuple[CachedOption, ...]

    @property
    def color(self) -> chess.Color:
        return chess.WHITE if self.mover == "white" else chess.BLACK

    def as_dict(self) -> dict[str, Any]:
        return {
            **{key: value for key, value in asdict(self).items() if key not in {
                "root_features", "options", "label_provenance",
                "suggestion_provenance",
            }},
            "tactical_expected_series": list(self.tactical_expected_series),
            "root_features": self.root_features.as_dict(),
            "label_provenance": dict(self.label_provenance),
            "suggestion_provenance": (
                None
                if self.suggestion_provenance is None
                else dict(self.suggestion_provenance)
            ),
            "options": [option.as_dict() for option in self.options],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CachedPosition:
        return cls(
            case_id=str(payload["case_id"]),
            trace_id=str(payload["trace_id"]),
            trace_step=int(payload["trace_step"]),
            split=str(payload["split"]),
            position_hash=str(payload["position_hash"]),
            pfen=str(payload["pfen"]),
            mover=str(payload["mover"]),
            root_features=CachedFeatures.from_dict(payload["root_features"]),
            label_score_white_heuristic_points=int(
                payload["label_score_white_heuristic_points"]
            ),
            label_best_series=str(payload["label_best_series"]),
            label_proof=(
                None
                if payload.get("label_proof") is None
                else str(payload["label_proof"])
            ),
            label_provenance=dict(payload["label_provenance"]),
            suggestion_series=(
                None
                if payload.get("suggestion_series") is None
                else str(payload["suggestion_series"])
            ),
            suggestion_provenance=(
                None
                if payload.get("suggestion_provenance") is None
                else dict(payload["suggestion_provenance"])
            ),
            suggestion_agrees_with_label=payload.get("suggestion_agrees_with_label"),
            tactical_anchor=bool(payload.get("tactical_anchor", False)),
            tactical_expected_series=tuple(payload["tactical_expected_series"]),
            bounded_opponent_mate_check_performed=bool(
                payload["bounded_opponent_mate_check_performed"]
            ),
            bounded_opponent_mate_check_complete=bool(
                payload["bounded_opponent_mate_check_complete"]
            ),
            options=tuple(CachedOption.from_dict(item) for item in payload["options"]),
        )


@dataclass(frozen=True, slots=True)
class TrainingCache:
    champion_profile_id: str
    config: FastTrainingConfig
    input_signature: str
    positions: tuple[CachedPosition, ...]
    build_seconds: float = field(compare=False)

    def deterministic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": FAST_TRAINING_SCHEMA_VERSION,
            "suite_version": FAST_TRAINING_SUITE_VERSION,
            "method": FAST_TRAINING_METHOD,
            "engine_version": ENGINE_VERSION,
            "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
            "champion_profile_id": self.champion_profile_id,
            "config": self.config.as_dict(),
            "input_signature": self.input_signature,
            "positions": [position.as_dict() for position in self.positions],
        }

    @property
    def cache_id(self) -> str:
        return _digest("spc-fast-cache-", self.deterministic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.deterministic_payload(),
            "cache_id": self.cache_id,
            "build_seconds": self.build_seconds,
            "proxy_contract": {
                "is_wdl": False,
                "strength_claim": False,
                "teacher_distillation": True,
                "teacher_move_is_truth": False,
                "external_code_copied": False,
                "notice": PROXY_DISCLAIMER,
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TrainingCache:
        if int(payload.get("schema_version", -1)) != FAST_TRAINING_SCHEMA_VERSION:
            raise ValueError("unsupported fast-training cache schema")
        if payload.get("suite_version") != FAST_TRAINING_SUITE_VERSION:
            raise ValueError("fast-training suite version mismatch")
        if payload.get("source_fingerprint") != ENGINE_SOURCE_FINGERPRINT:
            raise ValueError("fast-training source fingerprint mismatch")
        cache = cls(
            champion_profile_id=str(payload["champion_profile_id"]),
            config=FastTrainingConfig.from_dict(payload["config"]),
            input_signature=str(payload["input_signature"]),
            positions=tuple(
                CachedPosition.from_dict(item) for item in payload["positions"]
            ),
            build_seconds=float(payload.get("build_seconds", 0.0)),
        )
        if payload.get("cache_id") != cache.cache_id:
            raise ValueError("fast-training cache_id does not match its evidence")
        return cache


def _split_for_trace(trace_id: str, config: FastTrainingConfig) -> str:
    bucket = int.from_bytes(
        hashlib.sha256(f"{config.seed}|{trace_id}".encode()).digest()[:4], "big"
    ) % 100
    return "holdout" if bucket < config.holdout_percent else "train"


def _input_signature(
    positions: Sequence[TrainingPosition], config: FastTrainingConfig
) -> str:
    return _digest(
        "spc-fast-input-",
        {
            "config_id": config.config_id,
            "positions": [position.as_dict() for position in positions],
        },
    )


def _selected_source_positions(
    positions: Sequence[TrainingPosition] | None,
    config: FastTrainingConfig,
) -> tuple[TrainingPosition, ...]:
    available = (
        default_training_positions()
        if positions is None
        else tuple(positions)
    )
    selected = tuple(available[: config.position_limit])
    if not config.smoke and len(selected) < 32:
        raise ValueError(
            "full fast-training corpus is incomplete: expected at least 32 "
            "positions (30 opening boundaries plus two tactical anchors)"
        )
    if not config.smoke and sum(item.tactical_anchor for item in selected) < 2:
        raise ValueError(
            "full fast-training corpus requires at least two explicit "
            "tactical anchors"
        )
    return selected


def _build_cached_position(
    position: TrainingPosition,
    state: ProgressiveState,
    *,
    trace_step: int,
    champion: EngineProfile,
    config: FastTrainingConfig,
) -> tuple[CachedPosition, ProgressiveState | None]:
    # Opponent-reply research is concentrated where a complete next series is
    # still bounded enough to finish reliably. Later-series rows retain a
    # one-series re-search label and are position proxies only; they never
    # claim that opponent-mate safety was established.
    effective_depth = (
        config.label_depth_series
        if state.series_number <= 2 or position.tactical_anchor
        else 1
    )
    result = analyze(
        state,
        SearchLimits(
            depth_series=effective_depth,
            max_series_per_node=config.label_branch_cap,
            time_limit_seconds=None,
            max_generation_positions=config.label_max_work_positions,
            collect_all_root_scores=True,
        ),
        profile=champion,
    )
    if (
        result.best_series is None
        or result.timed_out
        or result.work_limit_reached
        or result.completed_depth != result.requested_depth
    ):
        raise ValueError(
            f"label search incomplete for {position.case_id}: "
            f"depth={result.completed_depth}/{result.requested_depth}, "
            f"timed_out={result.timed_out}, work_limit={result.work_limit_reached}"
        )
    alternatives = result.alternatives
    if not alternatives:
        raise ValueError(f"label search produced no options for {position.case_id}")
    cached_options = tuple(
        CachedOption(
            series=item.series.machine_notation,
            teacher_score_white_heuristic_points=item.score,
            proof=item.proof,
            outcome=(item.series.outcome.value if item.series.outcome else None),
            ended_by_check=item.series.ended_by_check,
            features=CachedFeatures.from_state(item.series.final_state),
        )
        for item in alternatives
    )
    trace_id = position.trace_id or position.case_id
    label_best = result.best_series.machine_notation
    # A prior-engine suggestion belongs to the supplied root only. Reusing it
    # as a label on derived continuation states would create false agreement
    # or disagreement evidence.
    suggested = position.suggestion_series if trace_step == 0 else None
    provenance = {
        "kind": "deeper-bounded-internal-research",
        "profile_id": champion.profile_id,
        "engine_version": result.engine_version,
        "source_fingerprint": result.source_fingerprint,
        "requested_depth_series": result.requested_depth,
        "completed_depth_series": result.completed_depth,
        "branch_cap": result.max_series_per_node,
        "max_work_positions": result.max_generation_positions,
        "exact_width": result.exact_width,
        "root_scores_complete_within_retained_frontier": (
            result.root_scores_complete
        ),
        "score_unit": "white-centric-heuristic-points",
        "is_game_outcome": False,
        "is_wdl": False,
    }
    cached = CachedPosition(
        case_id=(
            position.case_id
            if trace_step == 0
            else f"{position.case_id}/rollout-{trace_step}"
        ),
        trace_id=trace_id,
        trace_step=trace_step,
        split=_split_for_trace(trace_id, config),
        position_hash=state.position_hash,
        pfen=state.pfen,
        mover="white" if state.board.turn == chess.WHITE else "black",
        root_features=CachedFeatures.from_state(state),
        label_score_white_heuristic_points=result.score,
        label_best_series=label_best,
        label_proof=result.forced,
        label_provenance=provenance,
        suggestion_series=suggested,
        suggestion_provenance=(
            position.suggestion_provenance if trace_step == 0 else None
        ),
        suggestion_agrees_with_label=(
            None if suggested is None else suggested == label_best
        ),
        tactical_anchor=position.tactical_anchor if trace_step == 0 else False,
        tactical_expected_series=(
            position.tactical_expected_series if trace_step == 0 else ()
        ),
        bounded_opponent_mate_check_performed=effective_depth >= 2,
        bounded_opponent_mate_check_complete=(
            effective_depth >= 2 and result.exact_width
        ),
        options=cached_options,
    )
    next_state = (
        None if result.best_series.outcome is not None else result.best_series.final_state
    )
    return cached, next_state


def build_training_cache(
    champion: EngineProfile,
    *,
    config: FastTrainingConfig | None = None,
    positions: Sequence[TrainingPosition] | None = None,
) -> TrainingCache:
    config = config or FastTrainingConfig()
    source_positions = _selected_source_positions(positions, config)
    signature = _input_signature(source_positions, config)
    started = time.perf_counter()
    cached: list[CachedPosition] = []
    seen_hashes: set[str] = set()
    root_cases_by_hash: dict[str, str] = {}
    continuations: list[
        tuple[TrainingPosition, ProgressiveState | None, int]
    ] = []

    # Cache every supplied root before following any derived rollout. This
    # guarantees that an earlier trace cannot accidentally pre-empt one of the
    # required opening-suite boundaries merely because its best continuation
    # reaches that same state.
    for position in source_positions:
        state = position.state()
        duplicate = root_cases_by_hash.get(state.position_hash)
        if duplicate is not None:
            raise ValueError(
                "fast-training corpus contains duplicate root positions: "
                f"{duplicate} and {position.case_id}"
            )
        root_cases_by_hash[state.position_hash] = position.case_id
        trace_steps = (
            config.rollout_steps if state.series_number <= 2 else 1
        )
        evidence, next_state = _build_cached_position(
            position,
            state,
            trace_step=0,
            champion=champion,
            config=config,
        )
        cached.append(evidence)
        seen_hashes.add(evidence.position_hash)
        continuations.append((position, next_state, trace_steps))

    for position, state, trace_steps in continuations:
        for trace_step in range(1, trace_steps):
            if state is None or state.position_hash in seen_hashes:
                break
            evidence, state = _build_cached_position(
                position,
                state,
                trace_step=trace_step,
                champion=champion,
                config=config,
            )
            cached.append(evidence)
            seen_hashes.add(evidence.position_hash)
    if not cached:
        raise ValueError("fast-training cache contains no positions")
    # Preserve trace grouping while guaranteeing a holdout when there are at
    # least two independent traces. This fallback is deterministic.
    trace_ids = sorted({item.trace_id for item in cached})
    if len(trace_ids) >= 2 and not any(item.split == "holdout" for item in cached):
        forced_trace = trace_ids[-1]
        cached = [
            CachedPosition.from_dict({**item.as_dict(), "split": "holdout"})
            if item.trace_id == forced_trace
            else item
            for item in cached
        ]
    return TrainingCache(
        champion_profile_id=champion.profile_id,
        config=config,
        input_signature=signature,
        positions=tuple(cached),
        build_seconds=time.perf_counter() - started,
    )


def save_training_cache(cache: TrainingCache, path: str | Path) -> Path:
    return _atomic_json(path, cache.as_dict())


def load_training_cache(path: str | Path) -> TrainingCache:
    payload = json.loads(
        Path(path).expanduser().resolve().read_text(encoding="utf-8")
    )
    if not isinstance(payload, Mapping):
        raise ValueError("fast-training cache root must be an object")
    return TrainingCache.from_dict(payload)


def load_or_build_training_cache(
    path: str | Path,
    champion: EngineProfile,
    *,
    config: FastTrainingConfig | None = None,
    positions: Sequence[TrainingPosition] | None = None,
) -> tuple[TrainingCache, bool]:
    config = config or FastTrainingConfig()
    source_positions = _selected_source_positions(positions, config)
    expected_signature = _input_signature(source_positions, config)
    source = Path(path).expanduser().resolve()
    if source.exists():
        cached = load_training_cache(source)
        if (
            cached.champion_profile_id == champion.profile_id
            and cached.config == config
            and cached.input_signature == expected_signature
        ):
            return cached, True
    cached = build_training_cache(
        champion, config=config, positions=source_positions
    )
    save_training_cache(cached, source)
    return cached, False


def _clipped(value: int, limit: int) -> int:
    return max(-limit, min(limit, value))


def _select_option(
    position: CachedPosition, profile: EngineProfile
) -> tuple[CachedOption, int]:
    scored = [
        (option, option.candidate_score(profile, position.color))
        for option in position.options
    ]
    if position.color == chess.WHITE:
        return min(scored, key=lambda item: (-item[1], item[0].series))
    return min(scored, key=lambda item: (item[1], item[0].series))


def _position_measurements(
    cache: TrainingCache, profile: EngineProfile
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    rows: list[dict[str, Any]] = []
    for position in cache.positions:
        selected, predicted_option_score = _select_option(position, profile)
        if position.color == chess.WHITE:
            regret = (
                position.label_score_white_heuristic_points
                - selected.teacher_score_white_heuristic_points
            )
            opponent = "black"
        else:
            regret = (
                selected.teacher_score_white_heuristic_points
                - position.label_score_white_heuristic_points
            )
            opponent = "white"
        predicted_root = position.root_features.score(profile)
        value_error = abs(
            _clipped(predicted_root, cache.config.score_clip)
            - _clipped(
                position.label_score_white_heuristic_points,
                cache.config.score_clip,
            )
        )
        known_opponent_mate = selected.proof == opponent
        expected_ok = (
            not position.tactical_expected_series
            or selected.series in position.tactical_expected_series
        )
        rows.append(
            {
                "case_id": position.case_id,
                "trace_id": position.trace_id,
                "trace_step": position.trace_step,
                "split": position.split,
                "selected_series": selected.series,
                "label_best_series": position.label_best_series,
                "teacher_agreement": selected.series == position.label_best_series,
                "teacher_disagreement": selected.series != position.label_best_series,
                "policy_regret_proxy_points": max(0, regret),
                "static_value_error_proxy_points": value_error,
                "predicted_option_score_white_heuristic_points": predicted_option_score,
                "known_opponent_mate_in_bounded_research": known_opponent_mate,
                "opponent_mate_check_performed": (
                    position.bounded_opponent_mate_check_performed
                ),
                "opponent_mate_check_complete": (
                    position.bounded_opponent_mate_check_complete
                ),
                "tactical_anchor": position.tactical_anchor,
                "tactical_expected_series_passed": expected_ok,
            }
        )

    def mean(items: Iterable[float]) -> float:
        values = list(items)
        return sum(values) / len(values) if values else math.inf

    train = [row for row in rows if row["split"] == "train"] or rows
    holdout = [row for row in rows if row["split"] == "holdout"]
    traces: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        traces.setdefault(str(row["trace_id"]), []).append(row)
    # A row contributes to the short-rollout proxy only when its label search
    # actually included a bounded opponent reply. Later-series depth-one rows
    # remain useful cached position/policy evidence, but calling them rollouts
    # would overstate what was searched.
    safety_checked_traces = [
        sorted(
            (
                row
                for row in trace
                if row["opponent_mate_check_performed"]
            ),
            key=lambda item: int(item["trace_step"]),
        )
        for trace in traces.values()
    ]
    rollout_losses = [
        sum(
            float(row["policy_regret_proxy_points"]) / (index + 1)
            for index, row in enumerate(trace)
        )
        for trace in safety_checked_traces
        if trace
    ]
    metrics = {
        "train_position_error": mean(
            float(row["static_value_error_proxy_points"]) for row in train
        ),
        "train_policy_regret": mean(
            float(row["policy_regret_proxy_points"]) for row in train
        ),
        "holdout_position_error": mean(
            float(row["static_value_error_proxy_points"]) for row in holdout
        ),
        "holdout_policy_regret": mean(
            float(row["policy_regret_proxy_points"]) for row in holdout
        ),
        "short_rollout_regret": mean(rollout_losses),
        "short_rollout_checked_rows": float(
            sum(len(trace) for trace in safety_checked_traces)
        ),
        "teacher_agreement_rate": mean(
            float(bool(row["teacher_agreement"])) for row in rows
        ),
    }
    return rows, metrics


def _stage_one_position_error(
    cache: TrainingCache, profile: EngineProfile
) -> float:
    train = [position for position in cache.positions if position.split == "train"]
    selected = train or list(cache.positions)
    errors = [
        abs(
            _clipped(position.root_features.score(profile), cache.config.score_clip)
            - _clipped(
                position.label_score_white_heuristic_points,
                cache.config.score_clip,
            )
        )
        for position in selected
    ]
    return sum(errors) / len(errors)


def rank_profiles(
    cache: TrainingCache,
    profiles: Sequence[EngineProfile],
    *,
    preliminary_games_per_pair: int = 10,
    promotion_games: int = 20,
) -> dict[str, Any]:
    if len({profile.profile_id for profile in profiles}) != len(profiles):
        raise ValueError("fast preselection requires unique profile_ids")
    started = time.perf_counter()
    stage_one = sorted(
        (
            {
                "profile": profile,
                "train_position_error": _stage_one_position_error(cache, profile),
            }
            for profile in profiles
        ),
        key=lambda item: (
            item["train_position_error"], item["profile"].profile_id
        ),
    )
    stage_two_ids = {
        item["profile"].profile_id
        for item in stage_one[
            : max(
                cache.config.finalist_count * cache.config.stage_two_multiplier,
                cache.config.finalist_count + 1,
            )
        ]
    }
    # The champion is the tactical non-regression baseline even when its
    # train-position proxy is outside top-K.
    stage_two_ids.add(cache.champion_profile_id)

    raw: list[dict[str, Any]] = []
    for profile in profiles:
        if profile.profile_id in stage_two_ids:
            rows, metrics = _position_measurements(cache, profile)
        else:
            rows = []
            metrics = {
                "train_position_error": _stage_one_position_error(cache, profile),
                "train_policy_regret": math.inf,
                "holdout_position_error": math.inf,
                "holdout_policy_regret": math.inf,
                "short_rollout_regret": math.inf,
                "short_rollout_checked_rows": 0.0,
                "teacher_agreement_rate": 0.0,
            }
        tactical_rows = (
            [
                row
                for row, position in zip(rows, cache.positions, strict=True)
                if position.tactical_anchor
            ]
            if rows
            else []
        )
        raw.append(
            {
                "profile_id": profile.profile_id,
                "profile_name": profile.name,
                "rows": rows,
                "metrics": metrics,
                "tactical_rows": tactical_rows,
            }
        )
    champion = next(
        (item for item in raw if item["profile_id"] == cache.champion_profile_id),
        None,
    )
    if champion is None:
        raise ValueError("cached champion must be present in the ranked population")
    champion_tactical_regret = sum(
        int(row["policy_regret_proxy_points"])
        for row in champion["tactical_rows"]
    )

    ranking: list[dict[str, Any]] = []
    for item in raw:
        stage_two = item["profile_id"] in stage_two_ids
        tactical_regret = sum(
            int(row["policy_regret_proxy_points"])
            for row in item["tactical_rows"]
        )
        safety_performed_rows = [
            row
            for row in item["rows"]
            if row["opponent_mate_check_performed"]
        ]
        safety_complete_rows = [
            row
            for row in safety_performed_rows
            if row["opponent_mate_check_complete"]
        ]
        safety_incomplete_rows = [
            row
            for row in safety_performed_rows
            if not row["opponent_mate_check_complete"]
        ]
        detected_opponent_mate = any(
            row["known_opponent_mate_in_bounded_research"]
            for row in safety_performed_rows
        )
        if not stage_two:
            safety_status = "not-evaluated"
        elif detected_opponent_mate:
            safety_status = "proven-unsafe"
        elif (
            safety_performed_rows
            and not safety_incomplete_rows
            and len(safety_complete_rows) == len(safety_performed_rows)
        ):
            safety_status = "complete-no-mate"
        else:
            safety_status = "unknown-selective"
        bounded_safety_passed = safety_status == "complete-no-mate"

        tactical_proxy_cleared = (
            stage_two
            and bool(item["tactical_rows"])
            and not detected_opponent_mate
            and all(
                row["tactical_expected_series_passed"]
                and row["opponent_mate_check_performed"]
                and not row["known_opponent_mate_in_bounded_research"]
                for row in item["tactical_rows"]
            )
            and tactical_regret
            <= (
                champion_tactical_regret
                + cache.config.tactical_regression_tolerance
            )
        )
        tactical_passed = (
            tactical_proxy_cleared
            and bounded_safety_passed
            and all(
                row["opponent_mate_check_complete"]
                for row in item["tactical_rows"]
            )
        )
        eligible_for_full_game_testing = tactical_proxy_cleared and (
            safety_status in {"complete-no-mate", "unknown-selective"}
        )
        if not stage_two:
            tactical_screen_status = "not-evaluated"
        elif tactical_passed:
            tactical_screen_status = "complete-non-regression"
        elif eligible_for_full_game_testing:
            tactical_screen_status = "provisional-unknown-selective"
        elif detected_opponent_mate:
            tactical_screen_status = "rejected-proven-unsafe"
        else:
            tactical_screen_status = "rejected-tactical-proxy"
        metrics = item["metrics"]
        rollout_loss = metrics["short_rollout_regret"]
        combined_loss = (
            metrics["train_position_error"] * 0.20
            + (
                metrics["train_policy_regret"]
                + (
                    rollout_loss * 0.50
                    if math.isfinite(rollout_loss)
                    else 1_000_000.0
                )
                + (
                    0.10 * metrics["holdout_position_error"]
                    if math.isfinite(metrics["holdout_position_error"])
                    else 0.0
                )
            )
            if stage_two
            else 1_000_000.0
        )
        ranking.append(
            {
                "profile_id": item["profile_id"],
                "profile_name": item["profile_name"],
                "eligible_for_full_game_testing": (
                    eligible_for_full_game_testing
                ),
                "stage_two_evaluated": stage_two,
                "tactical_non_regression_passed": tactical_passed,
                "tactical_screen_status": tactical_screen_status,
                "bounded_opponent_mate_safety_status": safety_status,
                "bounded_opponent_mate_safety_passed": (
                    bounded_safety_passed
                ),
                "tactical_regret_proxy_points": tactical_regret,
                "proxy_loss": round(combined_loss, 6),
                "position_proxy": {
                    "train_mean_absolute_error": round(
                        metrics["train_position_error"], 6
                    ),
                    "holdout_mean_absolute_error": (
                        None
                        if not math.isfinite(metrics["holdout_position_error"])
                        else round(metrics["holdout_position_error"], 6)
                    ),
                    "unit": "cached-position-proxy-points",
                    "is_wdl": False,
                },
                "short_rollout_proxy": {
                    "mean_discounted_regret": (
                        None
                        if not math.isfinite(rollout_loss)
                        else round(rollout_loss, 6)
                    ),
                    "safety_checked_position_count": int(
                        metrics["short_rollout_checked_rows"]
                    ),
                    "safety_check_performed_position_count": len(
                        safety_performed_rows
                    ),
                    "safety_check_complete_position_count": len(
                        safety_complete_rows
                    ),
                    "safety_check_incomplete_position_count": len(
                        safety_incomplete_rows
                    ),
                    "unit": "cached-short-rollout-proxy-points",
                    "is_wdl": False,
                    "opponent_mate_safety_check": "bounded-cached-research",
                    "opponent_mate_safety_status": safety_status,
                    "opponent_mate_safety_passed": bounded_safety_passed,
                },
                "teacher_agreement": {
                    "agreements": sum(
                        bool(row["teacher_agreement"]) for row in item["rows"]
                    ),
                    "disagreements": sum(
                        bool(row["teacher_disagreement"]) for row in item["rows"]
                    ),
                    "rate": round(metrics["teacher_agreement_rate"], 6),
                    "teacher_move_is_truth": False,
                },
                "strength_claim": False,
            }
        )
    ranking.sort(
        key=lambda item: (
            not item["eligible_for_full_game_testing"],
            item["proxy_loss"],
            item["profile_id"],
        )
    )
    for rank, item in enumerate(ranking, 1):
        item["rank"] = rank
    challengers = [
        item["profile_id"]
        for item in ranking
        if item["eligible_for_full_game_testing"]
        and item["profile_id"] != cache.champion_profile_id
    ][: cache.config.finalist_count]

    elapsed = time.perf_counter() - started
    old_preliminary = (
        len(profiles) * (len(profiles) - 1) // 2 * preliminary_games_per_pair
    )
    new_preliminary = 0
    before = old_preliminary + promotion_games
    after = promotion_games if challengers else 0
    deterministic = {
        "schema_version": FAST_TRAINING_SCHEMA_VERSION,
        "suite_version": FAST_TRAINING_SUITE_VERSION,
        "method": FAST_TRAINING_METHOD,
        "cache_id": cache.cache_id,
        "champion_profile_id": cache.champion_profile_id,
        "candidate_profile_ids": [profile.profile_id for profile in profiles],
        "ranking_request": {
            "preliminary_games_per_pair": preliminary_games_per_pair,
            "promotion_games": promotion_games,
        },
        "ranking": ranking,
        "finalist_profile_ids": challengers,
        "full_game_schedule": {
            "legacy_preliminary_games": old_preliminary,
            "fast_funnel_preliminary_games": new_preliminary,
            "promotion_games_if_challenger": promotion_games,
            "total_before": before,
            "total_after": after,
            "games_avoided": before - after,
            "reduction_fraction": (
                round((before - after) / before, 6) if before else 0.0
            ),
            "promotion_gate_changed": False,
        },
        "proxy_contract": {
            "is_wdl": False,
            "strength_claim": False,
            "teacher_distillation": True,
            "teacher_move_is_truth": False,
            "full_game_promotion_required": True,
            "notice": PROXY_DISCLAIMER,
        },
    }
    return {
        **deterministic,
        "evidence_id": _digest("spc-fast-report-", deterministic),
        "performance": {
            "candidate_iterations": len(profiles),
            "elapsed_seconds": elapsed,
            "candidate_iterations_per_second": (
                len(profiles) / max(elapsed, 1e-12)
            ),
            "includes_cache_build": False,
        },
    }


def benchmark_profile_scoring(
    cache: TrainingCache,
    profiles: Sequence[EngineProfile],
    *,
    repetitions: int = 100,
) -> dict[str, Any]:
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    # Warm cached Python bytecode/data paths without counting the warmup.
    for profile in profiles:
        _position_measurements(cache, profile)
    started = time.perf_counter()
    checksum = 0
    for _ in range(repetitions):
        for profile in profiles:
            rows, _ = _position_measurements(cache, profile)
            checksum += sum(int(row["policy_regret_proxy_points"]) for row in rows)
    elapsed = time.perf_counter() - started
    iterations = repetitions * len(profiles)
    return {
        "scenario": "warm-cache-seven-term-profile-screen",
        "candidate_iterations": iterations,
        "elapsed_seconds": elapsed,
        "candidate_iterations_per_second": iterations / max(elapsed, 1e-12),
        "position_rows_per_candidate": len(cache.positions),
        "checksum": checksum,
        "includes_cache_build": False,
        "is_wdl": False,
        "strength_claim": False,
    }


def save_preselection_report(report: Mapping[str, Any], path: str | Path) -> Path:
    return _atomic_json(path, report)


def load_preselection_report(path: str | Path) -> dict[str, Any]:
    payload = json.loads(
        Path(path).expanduser().resolve().read_text(encoding="utf-8")
    )
    if not isinstance(payload, dict):
        raise ValueError("fast-training report root must be an object")
    deterministic = {
        key: value
        for key, value in payload.items()
        if key not in {"evidence_id", "performance"}
    }
    if payload.get("evidence_id") != _digest("spc-fast-report-", deterministic):
        raise ValueError("fast-training report evidence_id mismatch")
    return payload


def run_fast_preselection(
    profiles: Sequence[EngineProfile],
    champion: EngineProfile,
    *,
    cache_path: str | Path,
    report_path: str | Path,
    config: FastTrainingConfig | None = None,
    positions: Sequence[TrainingPosition] | None = None,
    preliminary_games_per_pair: int = 10,
    promotion_games: int = 20,
) -> tuple[dict[str, Any], bool]:
    cache, _cache_resumed = load_or_build_training_cache(
        cache_path,
        champion,
        config=config,
        positions=positions,
    )
    report_source = Path(report_path).expanduser().resolve()
    candidate_ids = [profile.profile_id for profile in profiles]
    if report_source.exists():
        report = load_preselection_report(report_source)
        if (
            report.get("cache_id") == cache.cache_id
            and report.get("candidate_profile_ids") == candidate_ids
            and report.get("ranking_request")
            == {
                "preliminary_games_per_pair": preliminary_games_per_pair,
                "promotion_games": promotion_games,
            }
        ):
            return report, True
    report = rank_profiles(
        cache,
        profiles,
        preliminary_games_per_pair=preliminary_games_per_pair,
        promotion_games=promotion_games,
    )
    save_preselection_report(report, report_source)
    return report, False
