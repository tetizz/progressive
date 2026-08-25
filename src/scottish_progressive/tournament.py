from __future__ import annotations

from collections import Counter
import copy
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .fast_training import FEATURE_NAMES
from .model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    Outcome,
    ProgressiveState,
)
from .profiles import EngineProfile, EvaluationWeights
from .rules import play_series, quiet_adjudication_status
from .selfplay_training import HUMAN_REFUTATION_GATE_ID
from .strength import (
    STRENGTH_REPORT_FORMAT,
    seeded_opening_suite_from_dict,
    subset_seeded_opening_suite,
)


TOURNAMENT_PROTOCOL_FORMAT = "spc-challenger-funnel-v2"
FUNNEL_CHECKPOINT_FORMAT = "spc-challenger-funnel-checkpoint-v1"
SURVIVOR_SET_FORMAT = "spc-challenger-survivors-v1"
EXPANSION_DECISION_FORMAT = "spc-result-blind-expansion-v3"
OPENING_RETRY_POLICY_FORMAT = "spc-tournament-opening-retry-policy-v1"
OPENING_RESERVE_FORMAT = "spc-tournament-opening-reserve-v1"
TRUSTED_TOURNAMENT_AUTHORITY_FORMAT = "spc-trusted-tournament-authority-v1"
MAX_REPLACEMENT_OPENING_ATTEMPTS = 2
TOTAL_OPENING_ATTEMPTS = MAX_REPLACEMENT_OPENING_ATTEMPTS + 1
PROMOTION_BATCH_FORMAT = "spc-promotion-batch-chain-v1"
GENERATOR_ID = "affine-mixed-radix-grid-v1"
UNCERTAINTY_METHOD = "two-sided-hoeffding-95-percent-on-completed-pairs"
POPULATION_SIZE = 1 << 22
POPULATION_MASK = POPULATION_SIZE - 1
POPULATION_MULTIPLIER = 0x27D4EB2D
SHARED_WEIGHT_TABLE = (40, 60, 80, 90, 100, 110, 125, 160)
BOUNDARY_WEIGHT_TABLE = (
    40, 50, 60, 70, 80, 85, 90, 95, 100, 105, 110, 120, 135, 150, 180, 220,
)
GROUP_SCREEN_DEPTH = 2
GROUP_SCREEN_MAX_SEARCH_WORK = 250_000
GROUP_SCREEN_MAX_GAME_WORK = 5_000_000
DECISIVE_DEPTH = 3
DECISIVE_MAX_SEARCH_WORK = 5_000_000
DECISIVE_MAX_GAME_WORK = 100_000_000
DECISIVE_TIMING_MULTIPLIER = 20
FROZEN_EXPANSION_OVERHEAD_RESERVE_SECONDS = 1_800.0
_ZERO_DIGEST = bytes(32)
_PROTOCOL_PREFIX = b"spc-challenger-funnel-v2|"
TRUSTED_PROMOTION_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2]
    / ".codex-runs"
    / "spc-promotion-batches.sqlite3"
)


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def canonical_digest(domain: str, payload: Any) -> str:
    return hashlib.sha256(
        domain.encode("ascii") + canonical_json_bytes(payload)
    ).hexdigest()


def tournament_database_state_artifact(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    """Content-addresses every authority-bearing tournament SQLite row.

    The digest deliberately contains row content digests rather than relying on
    a filesystem hash: SQLite WAL/checkpoint layout is operational state, while
    these ordered rows are the logical state a promotion decision consumes.
    Callers must separately validate each stored JSON blob against the digest in
    its row before treating this artifact as authenticated evidence.
    """

    identity = connection.execute(
        "select * from run_identity where singleton=1"
    ).fetchone()
    if identity is None:
        raise ValueError("tournament database lacks its frozen run identity")
    tables: dict[str, list[list[Any]]] = {}
    for table, columns, order in (
        (
            "opening_suites",
            "domain,suite_digest,position_hash_digest,case_count",
            "domain",
        ),
        (
            "match_reports",
            "stage,matchup_id,ordinal,resolved_spec_digest,"
            "opening_reserve_digest,suite_digest,attempt_manifest_digest,"
            "report_digest,pair_count,replacement_attempts",
            "ordinal",
        ),
        (
            "match_attempts",
            "stage,matchup_id,ordinal,attempt_index,"
            "unresolved_pair_indexes_digest,lane_suite_digest,"
            "subset_suite_digest,config_digest,report_digest,"
            "execution_elapsed_seconds",
            "ordinal,attempt_index",
        ),
        (
            "artifacts",
            "kind,artifact_key,payload_digest",
            "kind,artifact_key",
        ),
        (
            "slot_resolutions",
            "slot,effective_id,artifact_digest",
            "slot",
        ),
    ):
        tables[table] = [
            list(row)
            for row in connection.execute(
                f"select {columns} from {table} order by {order}"
            )
        ]
    payload = {
        "format": str(identity["format"]),
        "plan_digest": str(identity["plan_digest"]),
        "environment_digest": str(identity["environment_digest"]),
        "profile_catalog_digest": str(identity["profile_catalog_digest"]),
        "schedule": str(identity["schedule"]),
        "exclusion_digest": str(identity["exclusion_digest"]),
        "promotion_batch_chain_digest": str(
            identity["promotion_batch_chain_digest"]
        ),
        "expansion_decision_digest": identity["expansion_decision_digest"],
        "tables": tables,
    }
    return {
        **payload,
        "runner_state_digest": canonical_digest(
            "spc-tournament-runner-state-v2\0", payload
        ),
    }


def make_tournament_authority_artifact(
    connection: sqlite3.Connection,
    *,
    database_path: str | Path,
    batch_index: int,
    final_rows: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Builds the registry payload that authenticates one immutable run DB."""

    state = tournament_database_state_artifact(connection)
    normalized_finals = {
        str(stage): {
            "stage": str(row["stage"]),
            "matchup_id": str(row["matchup_id"]),
            "ordinal": int(row["ordinal"]),
            "resolved_spec_digest": str(row["resolved_spec_digest"]),
            "opening_reserve_digest": str(row["opening_reserve_digest"]),
            "suite_digest": str(row["suite_digest"]),
            "attempt_manifest_digest": str(row["attempt_manifest_digest"]),
            "report_digest": str(row["report_digest"]),
            "attempt_report_digests": [
                str(item[0])
                for item in connection.execute(
                    "select report_digest from match_attempts where stage=? "
                    "and matchup_id=? order by attempt_index",
                    (str(row["stage"]), str(row["matchup_id"])),
                )
            ],
        }
        for stage, row in sorted(final_rows.items())
    }
    deterministic = {
        "format": TRUSTED_TOURNAMENT_AUTHORITY_FORMAT,
        "batch_index": int(batch_index),
        "database_path": str(Path(database_path).expanduser().resolve()),
        "plan_digest": state["plan_digest"],
        "environment_digest": state["environment_digest"],
        "profile_catalog_digest": state["profile_catalog_digest"],
        "corpus_exclusion_digest": state["exclusion_digest"],
        "promotion_batch_chain_digest": state[
            "promotion_batch_chain_digest"
        ],
        "expansion_decision_digest": state["expansion_decision_digest"],
        "runner_state_digest": state["runner_state_digest"],
        "match_report_count": len(state["tables"]["match_reports"]),
        "match_attempt_count": len(state["tables"]["match_attempts"]),
        "final_rows": normalized_finals,
    }
    return {
        **deterministic,
        "authority_digest": canonical_digest(
            "spc-trusted-tournament-authority-v1\0", deterministic
        ),
    }


def make_promotion_batch_artifact(
    *,
    registry_id: str,
    reservation_key: str,
    batch_index: int,
    protocol_digest: str,
    baseline_effective_id: str,
    predecessor_chain_digest: str,
    source_fingerprint: str = ENGINE_SOURCE_FINGERPRINT,
) -> dict[str, Any]:
    if batch_index < 1:
        raise ValueError("promotion batch index must be positive")
    for name, value in (
        ("registry_id", registry_id),
        ("protocol_digest", protocol_digest),
        ("predecessor_chain_digest", predecessor_chain_digest),
    ):
        try:
            decoded = bytes.fromhex(value)
        except ValueError as error:
            raise ValueError(f"promotion batch {name} is not hex") from error
        if len(decoded) != 32:
            raise ValueError(f"promotion batch {name} is not SHA-256")
    if not baseline_effective_id.startswith("spc-effective-"):
        raise ValueError("promotion batch baseline identity is invalid")
    if not reservation_key or not reservation_key.isascii():
        raise ValueError("promotion batch reservation key must be non-empty ASCII")
    deterministic = {
        "format": PROMOTION_BATCH_FORMAT,
        "registry_id": registry_id,
        "reservation_key": reservation_key,
        "batch_index": batch_index,
        "protocol_digest": protocol_digest,
        "baseline_effective_id": baseline_effective_id,
        "source_fingerprint": source_fingerprint,
        "predecessor_chain_digest": predecessor_chain_digest,
    }
    chain_digest = hashlib.sha256(
        bytes.fromhex(predecessor_chain_digest)
        + canonical_json_bytes(deterministic)
    ).hexdigest()
    with_chain = {**deterministic, "chain_digest": chain_digest}
    return {
        **with_chain,
        "artifact_digest": canonical_digest(
            "spc-promotion-batch-chain-v1\0", with_chain
        ),
    }


def validate_promotion_batch_artifact(
    artifact: Mapping[str, Any],
    *,
    protocol_digest: str,
    baseline_effective_id: str,
    source_fingerprint: str = ENGINE_SOURCE_FINGERPRINT,
) -> None:
    required = {
        "format",
        "registry_id",
        "reservation_key",
        "batch_index",
        "protocol_digest",
        "baseline_effective_id",
        "source_fingerprint",
        "predecessor_chain_digest",
        "chain_digest",
        "artifact_digest",
    }
    if set(artifact) != required:
        raise ValueError("promotion batch artifact fields are not canonical")
    rebuilt = make_promotion_batch_artifact(
        registry_id=str(artifact["registry_id"]),
        reservation_key=str(artifact["reservation_key"]),
        batch_index=int(artifact["batch_index"]),
        protocol_digest=protocol_digest,
        baseline_effective_id=baseline_effective_id,
        predecessor_chain_digest=str(artifact["predecessor_chain_digest"]),
        source_fingerprint=source_fingerprint,
    )
    if dict(artifact) != rebuilt:
        raise ValueError("promotion batch artifact identity/chain mismatch")


def seed64(
    domain: str,
    *,
    source_fingerprint: str = ENGINE_SOURCE_FINGERPRINT,
    master_seed: int = 20_260_840,
) -> int:
    if not domain or not domain.isascii():
        raise ValueError("seed domain must be non-empty ASCII")
    if not 0 <= master_seed < 1 << 64:
        raise ValueError("master_seed must fit u64")
    payload = (
        _PROTOCOL_PREFIX
        + source_fingerprint.encode("ascii")
        + b"|"
        + str(master_seed).encode("ascii")
        + b"|"
        + domain.encode("ascii")
    )
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


@dataclass(frozen=True, slots=True)
class TournamentFunnelConfig:
    master_seed: int = 20_260_840
    population_size: int = POPULATION_SIZE
    stage_a_keep: int = 65_536
    stage_b_keep: int = 8_192
    stage_c_keep: int = 512
    survivor_count: int = 64
    stage_a_train_positions: int = 64
    stage_b_train_positions: int = 1_024
    stage_c_validation_positions: int = 4_096
    train_percent: int = 70
    validation_percent: int = 15
    audit_percent: int = 15
    pairs_per_group_match: int = 50
    pairs_per_r16_match: int = 50
    base_pairs_per_late_knockout: int = 50
    expanded_pairs_per_late_knockout: int = 100
    baseline_final_pairs: int = 200
    replacement_opening_attempts: int = MAX_REPLACEMENT_OPENING_ATTEMPTS
    search_depth: int = 2
    max_series_per_node: int = 32
    max_generation_positions: int = 250_000
    max_game_work_positions: int = 5_000_000
    decisive_search_depth: int = 3
    decisive_max_generation_positions: int = 5_000_000
    decisive_max_game_work_positions: int = 100_000_000
    requested_workers: int = 16
    expansion_budget_seconds: int = 7 * 3_600 + 45 * 60

    def __post_init__(self) -> None:
        frozen = {
            "population_size": (self.population_size, POPULATION_SIZE),
            "stage_a_keep": (self.stage_a_keep, 65_536),
            "stage_b_keep": (self.stage_b_keep, 8_192),
            "stage_c_keep": (self.stage_c_keep, 512),
            "survivor_count": (self.survivor_count, 64),
            "stage_a_train_positions": (self.stage_a_train_positions, 64),
            "stage_b_train_positions": (self.stage_b_train_positions, 1_024),
            "stage_c_validation_positions": (
                self.stage_c_validation_positions,
                4_096,
            ),
            "pairs_per_group_match": (self.pairs_per_group_match, 50),
            "pairs_per_r16_match": (self.pairs_per_r16_match, 50),
            "base_pairs_per_late_knockout": (
                self.base_pairs_per_late_knockout,
                50,
            ),
            "expanded_pairs_per_late_knockout": (
                self.expanded_pairs_per_late_knockout,
                100,
            ),
            "baseline_final_pairs": (self.baseline_final_pairs, 200),
            "replacement_opening_attempts": (
                self.replacement_opening_attempts,
                MAX_REPLACEMENT_OPENING_ATTEMPTS,
            ),
            "search_depth": (self.search_depth, 2),
            "max_series_per_node": (self.max_series_per_node, 32),
            "max_generation_positions": (
                self.max_generation_positions,
                250_000,
            ),
            "max_game_work_positions": (
                self.max_game_work_positions,
                GROUP_SCREEN_MAX_GAME_WORK,
            ),
            "decisive_search_depth": (
                self.decisive_search_depth,
                DECISIVE_DEPTH,
            ),
            "decisive_max_generation_positions": (
                self.decisive_max_generation_positions,
                DECISIVE_MAX_SEARCH_WORK,
            ),
            "decisive_max_game_work_positions": (
                self.decisive_max_game_work_positions,
                DECISIVE_MAX_GAME_WORK,
            ),
        }
        changed = [
            name for name, (actual, expected) in frozen.items() if actual != expected
        ]
        if changed:
            raise ValueError(f"frozen funnel field changed: {', '.join(changed)}")
        if (self.train_percent, self.validation_percent, self.audit_percent) != (
            70,
            15,
            15,
        ):
            raise ValueError("the component split must remain 70/15/15")
        if not 1 <= self.requested_workers <= 64:
            raise ValueError("requested_workers must be between 1 and 64")
        if not 0 <= self.master_seed < 1 << 64:
            raise ValueError("master_seed must fit u64")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def effective_profile_id(
    weights: EvaluationWeights,
    *,
    source_fingerprint: str = ENGINE_SOURCE_FINGERPRINT,
) -> str:
    payload = {
        "source_fingerprint": source_fingerprint,
        "weights": asdict(weights),
        "search": {
            "group_screen": {
                "depth_series": GROUP_SCREEN_DEPTH,
                "branch_cap": 32,
                "max_generation_positions": GROUP_SCREEN_MAX_SEARCH_WORK,
                "max_game_work_positions": GROUP_SCREEN_MAX_GAME_WORK,
            },
            "decisive_matches": {
                "depth_series": DECISIVE_DEPTH,
                "branch_cap": 32,
                "max_generation_positions": DECISIVE_MAX_SEARCH_WORK,
                "max_game_work_positions": DECISIVE_MAX_GAME_WORK,
            },
            "emergency_max_series": None,
        },
    }
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:20]
    return "spc-effective-" + digest


@dataclass(frozen=True, slots=True)
class PopulationMember:
    candidate_index: int
    code: int
    effective_id: str
    profile: EngineProfile

    @property
    def weight_tuple(self) -> tuple[int, ...]:
        return tuple(
            getattr(self.profile.weights, name) for name in FEATURE_NAMES
        )

    def compact_record(self) -> dict[str, Any]:
        return {
            "candidate_index": self.candidate_index,
            "effective_id": self.effective_id,
            "profile_id": self.profile.profile_id,
            "weights": asdict(self.profile.weights),
        }


@dataclass(frozen=True, slots=True)
class PopulationStream:
    baseline: EngineProfile
    config: TournamentFunnelConfig = TournamentFunnelConfig()
    source_fingerprint: str = ENGINE_SOURCE_FINGERPRINT

    def __len__(self) -> int:
        return self.config.population_size

    @property
    def order_offset(self) -> int:
        return seed64(
            "population-order",
            source_fingerprint=self.source_fingerprint,
            master_seed=self.config.master_seed,
        ) & POPULATION_MASK

    def member(self, candidate_index: int) -> PopulationMember:
        if not 0 <= candidate_index < len(self):
            raise IndexError("candidate_index is outside the frozen population")
        code = (
            POPULATION_MULTIPLIER * candidate_index + self.order_offset
        ) & POPULATION_MASK
        values = {
            "material": SHARED_WEIGHT_TABLE[(code >> 0) & 7],
            "king_space": SHARED_WEIGHT_TABLE[(code >> 3) & 7],
            "series_reach": SHARED_WEIGHT_TABLE[(code >> 6) & 7],
            "promotion_corridors": SHARED_WEIGHT_TABLE[(code >> 9) & 7],
            "immediate_vulnerability": SHARED_WEIGHT_TABLE[(code >> 12) & 7],
            "useful_mobility": SHARED_WEIGHT_TABLE[(code >> 15) & 7],
            "boundary_check": BOUNDARY_WEIGHT_TABLE[(code >> 18) & 15],
        }
        if all(value == 100 for value in values.values()):
            values["material"] = 101
        weights = EvaluationWeights(**values)
        profile = EngineProfile(
            name=f"batch-{self.config.master_seed} lattice {candidate_index:07d}",
            weights=weights,
            recommended_depth=2,
            recommended_branch_cap=32,
            generation=1,
            parent_profile_ids=(self.baseline.profile_id,),
            mutation_seed=candidate_index,
            notes=(
                f"{GENERATOR_ID}; deterministic exploration profile only; "
                "no match-strength or promotion claim."
            ),
        )
        return PopulationMember(
            candidate_index=candidate_index,
            code=code,
            effective_id=effective_profile_id(
                weights, source_fingerprint=self.source_fingerprint
            ),
            profile=profile,
        )

    def iter_range(
        self, start: int = 0, stop: int | None = None
    ) -> Iterator[PopulationMember]:
        end = len(self) if stop is None else stop
        if not 0 <= start <= end <= len(self):
            raise ValueError("population range is invalid")
        for candidate_index in range(start, end):
            yield self.member(candidate_index)

    def diversity_manifest(self) -> dict[str, Any]:
        shared_axis_count = POPULATION_SIZE // len(SHARED_WEIGHT_TABLE)
        boundary_axis_count = POPULATION_SIZE // len(BOUNDARY_WEIGHT_TABLE)
        material_counts = {
            str(value): shared_axis_count for value in SHARED_WEIGHT_TABLE
        }
        material_counts["100"] -= 1
        material_counts["101"] = 1
        mean_distance = (
            6
            * sum(abs(value - 100) for value in SHARED_WEIGHT_TABLE)
            / len(SHARED_WEIGHT_TABLE)
            + sum(abs(value - 100) for value in BOUNDARY_WEIGHT_TABLE)
            / len(BOUNDARY_WEIGHT_TABLE)
            + 1 / POPULATION_SIZE
        )
        return {
            "generator_id": GENERATOR_ID,
            "population_size": POPULATION_SIZE,
            "unique_weight_vectors_by_bijection": POPULATION_SIZE,
            "candidate_index_domain": [0, POPULATION_SIZE - 1],
            "affine_multiplier": POPULATION_MULTIPLIER,
            "affine_offset": self.order_offset,
            "affine_modulus": POPULATION_SIZE,
            "shared_weight_table": list(SHARED_WEIGHT_TABLE),
            "boundary_weight_table": list(BOUNDARY_WEIGHT_TABLE),
            "material_axis_counts": material_counts,
            "other_shared_axis_count_per_value": shared_axis_count,
            "boundary_axis_count_per_value": boundary_axis_count,
            "baseline_vector_replacement": {
                "from": {name: 100 for name in FEATURE_NAMES},
                "to_material": 101,
                "reason": "baseline remains a separate control",
            },
            "l1_distance_from_baseline": {
                "minimum": 1,
                "maximum": 480,
                "mean": mean_distance,
            },
            "materialization": "streamed; never retain all profiles in memory",
        }


class PopulationCollisionLedger:
    """Disk-backed exact UNIQUE guard for the streaming population."""

    def __init__(self, path: str | Path, *, protocol_digest: str) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("pragma journal_mode=WAL")
        self.connection.execute("pragma synchronous=FULL")
        self.connection.executescript(
            """
            create table if not exists metadata (
                key text primary key,
                value text not null
            );
            create table if not exists population_identity (
                candidate_index integer primary key,
                weight_key blob not null unique,
                effective_id text not null unique,
                profile_id text not null unique
            );
            """
        )
        row = self.connection.execute(
            "select value from metadata where key='protocol_digest'"
        ).fetchone()
        if row is None:
            self.connection.execute(
                "insert into metadata(key,value) values('protocol_digest',?)",
                (protocol_digest,),
            )
            self.connection.commit()
        elif row[0] != protocol_digest:
            self.close()
            raise ValueError("collision ledger belongs to a different protocol")

    def count(self) -> int:
        return int(
            self.connection.execute(
                "select count(*) from population_identity"
            ).fetchone()[0]
        )

    def require_count(self, expected: int) -> None:
        actual = self.count()
        if actual != expected:
            raise ValueError(
                f"collision ledger/checkpoint mismatch: {actual} != {expected}"
            )

    def record(self, member: PopulationMember) -> None:
        try:
            self.connection.execute(
                "insert into population_identity values(?,?,?,?)",
                (
                    member.candidate_index,
                    struct.pack("<7H", *member.weight_tuple),
                    member.effective_id,
                    member.profile.profile_id,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise RuntimeError(
                f"population identity collision at candidate {member.candidate_index}"
            ) from error

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> PopulationCollisionLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def component_split(
    component_id: str,
    *,
    config: TournamentFunnelConfig | None = None,
    source_fingerprint: str = ENGINE_SOURCE_FINGERPRINT,
) -> str:
    selected = config or TournamentFunnelConfig()
    if not component_id:
        raise ValueError("component_id cannot be empty")
    bucket = seed64(
        f"component-split|{component_id}",
        source_fingerprint=source_fingerprint,
        master_seed=selected.master_seed,
    ) % 100
    if bucket < selected.train_percent:
        return "train"
    if bucket < selected.train_percent + selected.validation_percent:
        return "validation"
    return "audit"


def build_funnel_manifest(
    baseline: EngineProfile,
    *,
    evidence_digests: Mapping[str, str],
    native_source_identity: str,
    runtime_identity: Mapping[str, Any],
    config: TournamentFunnelConfig | None = None,
    source_fingerprint: str = ENGINE_SOURCE_FINGERPRINT,
) -> dict[str, Any]:
    selected = config or TournamentFunnelConfig()
    required = {
        "corpus",
        "component_split",
        "stage_a_cache",
        "stage_b_cache",
        "validation_cache",
        "rules_suite",
    }
    if set(evidence_digests) != required or any(
        not isinstance(value, str) or not value
        for value in evidence_digests.values()
    ):
        raise ValueError(
            f"evidence_digests must contain exactly {sorted(required)}"
        )
    if not native_source_identity:
        raise ValueError("native_source_identity cannot be empty")
    deterministic = {
        "format": TOURNAMENT_PROTOCOL_FORMAT,
        "engine_version": ENGINE_VERSION,
        "source_fingerprint": source_fingerprint,
        "native_source_identity": native_source_identity,
        "runtime_identity": dict(runtime_identity),
        "master_seed": selected.master_seed,
        "baseline": {
            "profile_id": baseline.profile_id,
            "effective_id": effective_profile_id(
                baseline.weights, source_fingerprint=source_fingerprint
            ),
            "weights": asdict(baseline.weights),
        },
        "config": selected.as_dict(),
        "generator": PopulationStream(
            baseline, selected, source_fingerprint
        ).diversity_manifest(),
        "evidence_digests": dict(sorted(evidence_digests.items())),
        "funnel": [
            {
                "stage": "A",
                "input": POPULATION_SIZE,
                "keep": 65_536,
                "positions": 64,
                "split": "train",
            },
            {
                "stage": "B",
                "input": 65_536,
                "keep": 8_192,
                "positions": 1_024,
                "split": "train",
                "regularized": True,
            },
            {
                "stage": "behavioral-collapse",
                "signature": "ordered-case-selected-series-clipped-score-sha256",
            },
            {
                "stage": "C",
                "input_at_most": 8_192,
                "keep": 512,
                "positions": 4_096,
                "split": "validation",
            },
            {
                "stage": "tactical",
                "keep": 64,
                "fallback_order": "remaining-stage-B",
                "never_relax": True,
            },
        ],
        "audit_policy": "sealed until the tournament winner is frozen",
        "claim_scope": (
            "cache ranks are proxies, never WDL, strength, or promotion evidence"
        ),
    }
    return {
        **deterministic,
        "protocol_digest": canonical_digest(
            "spc-challenger-funnel-protocol-v1\0", deterministic
        ),
    }


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate_index: int
    effective_id: str
    profile_id: str
    rank_units: int

    @property
    def rank_key(self) -> tuple[int, str]:
        return self.rank_units, self.effective_id

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RankedCandidate:
        candidate = cls(
            candidate_index=int(payload["candidate_index"]),
            effective_id=str(payload["effective_id"]),
            profile_id=str(payload["profile_id"]),
            rank_units=int(payload["rank_units"]),
        )
        if candidate.rank_units < 0:
            raise ValueError("rank_units cannot be negative")
        return candidate


@dataclass(frozen=True, slots=True)
class _WorstFirst:
    candidate: RankedCandidate

    def __lt__(self, other: _WorstFirst) -> bool:
        return self.candidate.rank_key > other.candidate.rank_key


class BoundedRankHeap:
    def __init__(
        self, limit: int, candidates: Iterable[RankedCandidate] = ()
    ) -> None:
        if limit < 1:
            raise ValueError("heap limit must be positive")
        self.limit = limit
        self._heap: list[_WorstFirst] = []
        for candidate in candidates:
            self.add(candidate)

    def add(self, candidate: RankedCandidate) -> None:
        wrapped = _WorstFirst(candidate)
        if len(self._heap) < self.limit:
            heapq.heappush(self._heap, wrapped)
        elif candidate.rank_key < self._heap[0].candidate.rank_key:
            heapq.heapreplace(self._heap, wrapped)

    def ordered(self) -> tuple[RankedCandidate, ...]:
        return tuple(
            sorted(
                (item.candidate for item in self._heap),
                key=lambda item: item.rank_key,
            )
        )


@dataclass(frozen=True, slots=True)
class DispositionChain:
    digest_hex: str = _ZERO_DIGEST.hex()
    processed_count: int = 0

    def update(
        self,
        *,
        candidate_index: int,
        effective_id: str,
        profile_id: str,
        stage: str,
        disposition: str,
        rank_units: int | None,
    ) -> DispositionChain:
        record = {
            "candidate_index": candidate_index,
            "effective_id": effective_id,
            "profile_id": profile_id,
            "stage": stage,
            "disposition": disposition,
            "rank_units": rank_units,
        }
        digest = hashlib.sha256(
            bytes.fromhex(self.digest_hex) + canonical_json_bytes(record)
        ).hexdigest()
        return DispositionChain(digest, self.processed_count + 1)


@dataclass(frozen=True, slots=True)
class FunnelCheckpoint:
    protocol_digest: str
    stage: str
    scorer_digest: str
    input_size: int
    keep_count: int
    next_input_offset: int
    generator_index: int | None
    disposition: DispositionChain
    ranked_candidates: tuple[RankedCandidate, ...]
    complete: bool

    def as_dict(self) -> dict[str, Any]:
        deterministic = {
            "format": FUNNEL_CHECKPOINT_FORMAT,
            "protocol_digest": self.protocol_digest,
            "stage": self.stage,
            "scorer_digest": self.scorer_digest,
            "input_size": self.input_size,
            "keep_count": self.keep_count,
            "next_input_offset": self.next_input_offset,
            "generator_index": self.generator_index,
            "disposition_digest": self.disposition.digest_hex,
            "processed_count": self.disposition.processed_count,
            "ranked_candidates": [
                candidate.as_dict() for candidate in self.ranked_candidates
            ],
            "complete": self.complete,
        }
        return {
            **deterministic,
            "checkpoint_digest": canonical_digest(
                "spc-challenger-checkpoint-v1\0", deterministic
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FunnelCheckpoint:
        if payload.get("format") != FUNNEL_CHECKPOINT_FORMAT:
            raise ValueError("unsupported funnel checkpoint")
        expected_fields = {
            "format",
            "protocol_digest",
            "stage",
            "scorer_digest",
            "input_size",
            "keep_count",
            "next_input_offset",
            "generator_index",
            "disposition_digest",
            "processed_count",
            "ranked_candidates",
            "complete",
            "checkpoint_digest",
        }
        if set(payload) != expected_fields:
            raise ValueError("funnel checkpoint fields do not match the schema")
        deterministic = {
            key: value for key, value in payload.items()
            if key != "checkpoint_digest"
        }
        expected_digest = canonical_digest(
            "spc-challenger-checkpoint-v1\0", deterministic
        )
        if payload.get("checkpoint_digest") != expected_digest:
            raise ValueError("funnel checkpoint digest mismatch")
        checkpoint = cls(
            protocol_digest=str(payload["protocol_digest"]),
            stage=str(payload["stage"]),
            scorer_digest=str(payload["scorer_digest"]),
            input_size=int(payload["input_size"]),
            keep_count=int(payload["keep_count"]),
            next_input_offset=int(payload["next_input_offset"]),
            generator_index=(
                None
                if payload.get("generator_index") is None
                else int(payload["generator_index"])
            ),
            disposition=DispositionChain(
                str(payload["disposition_digest"]),
                int(payload["processed_count"]),
            ),
            ranked_candidates=tuple(
                RankedCandidate.from_dict(item)
                for item in payload["ranked_candidates"]
            ),
            complete=bool(payload["complete"]),
        )
        if len(checkpoint.ranked_candidates) > checkpoint.keep_count:
            raise ValueError("checkpoint heap exceeds its keep count")
        if len(checkpoint.scorer_digest) != 64:
            raise ValueError("checkpoint scorer digest must be SHA-256 hex")
        try:
            bytes.fromhex(checkpoint.scorer_digest)
        except ValueError as error:
            raise ValueError("checkpoint scorer digest is not hex") from error
        try:
            decoded_disposition = bytes.fromhex(
                checkpoint.disposition.digest_hex
            )
        except ValueError as error:
            raise ValueError("checkpoint disposition digest is not hex") from error
        if len(decoded_disposition) != 32:
            raise ValueError("checkpoint disposition digest must be 32 bytes")
        if not (
            0 <= checkpoint.next_input_offset <= checkpoint.input_size
            and checkpoint.keep_count > 0
            and checkpoint.disposition.processed_count >= 0
        ):
            raise ValueError("checkpoint counters are invalid")
        if checkpoint.stage == "stage-a" and (
            checkpoint.disposition.processed_count != checkpoint.next_input_offset
            or checkpoint.generator_index
            != (
                checkpoint.next_input_offset - 1
                if checkpoint.next_input_offset
                else None
            )
            or checkpoint.complete
            != (checkpoint.next_input_offset == checkpoint.input_size)
        ):
            raise ValueError("stage-A checkpoint progress fields disagree")
        if len({item.effective_id for item in checkpoint.ranked_candidates}) != len(
            checkpoint.ranked_candidates
        ):
            raise ValueError("checkpoint candidates contain duplicate identities")
        ordered = tuple(
            sorted(checkpoint.ranked_candidates, key=lambda item: item.rank_key)
        )
        if ordered != checkpoint.ranked_candidates:
            raise ValueError("checkpoint candidates are not in canonical rank order")
        return checkpoint


def _rank_units(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("proxy scorer must return a non-negative integer")
    return value


def scan_population_stage_a(
    stream: PopulationStream,
    scorer: Callable[[PopulationMember], int],
    *,
    protocol_digest: str,
    scorer_digest: str,
    collision_ledger: PopulationCollisionLedger,
    checkpoint: FunnelCheckpoint | None = None,
    stop_index: int | None = None,
) -> FunnelCheckpoint:
    start = 0
    chain = DispositionChain()
    existing: tuple[RankedCandidate, ...] = ()
    if len(scorer_digest) != 64:
        raise ValueError("stage-A scorer_digest must be SHA-256 hex")
    try:
        bytes.fromhex(scorer_digest)
    except ValueError as error:
        raise ValueError("stage-A scorer_digest is not hex") from error
    if checkpoint is not None:
        if (
            checkpoint.protocol_digest != protocol_digest
            or checkpoint.stage != "stage-a"
            or checkpoint.scorer_digest != scorer_digest
        ):
            raise ValueError("stage-A checkpoint identity mismatch")
        if (
            checkpoint.input_size != len(stream)
            or checkpoint.keep_count != stream.config.stage_a_keep
        ):
            raise ValueError("stage-A checkpoint shape mismatch")
        start = checkpoint.next_input_offset
        chain = checkpoint.disposition
        existing = checkpoint.ranked_candidates
        for prior in existing:
            regenerated = stream.member(prior.candidate_index)
            if (
                regenerated.effective_id != prior.effective_id
                or regenerated.profile.profile_id != prior.profile_id
                or _rank_units(scorer(regenerated)) != prior.rank_units
            ):
                raise ValueError("stage-A checkpoint heap cannot be regenerated")
    collision_ledger.require_count(start)
    end = len(stream) if stop_index is None else stop_index
    if not start <= end <= len(stream):
        raise ValueError("stage-A stop index is invalid")
    heap = BoundedRankHeap(stream.config.stage_a_keep, existing)
    for member in stream.iter_range(start, end):
        collision_ledger.record(member)
        units = _rank_units(scorer(member))
        candidate = RankedCandidate(
            member.candidate_index,
            member.effective_id,
            member.profile.profile_id,
            units,
        )
        heap.add(candidate)
        chain = chain.update(
            candidate_index=member.candidate_index,
            effective_id=member.effective_id,
            profile_id=member.profile.profile_id,
            stage="stage-a",
            disposition="scored",
            rank_units=units,
        )
    collision_ledger.commit()
    return FunnelCheckpoint(
        protocol_digest=protocol_digest,
        stage="stage-a",
        scorer_digest=scorer_digest,
        input_size=len(stream),
        keep_count=stream.config.stage_a_keep,
        next_input_offset=end,
        generator_index=(end - 1 if end else None),
        disposition=chain,
        ranked_candidates=heap.ordered(),
        complete=end == len(stream),
    )


def rank_candidate_stage(
    stream: PopulationStream,
    candidates: Sequence[RankedCandidate],
    scorer: Callable[[PopulationMember], int],
    *,
    protocol_digest: str,
    scorer_digest: str,
    stage: str,
    keep_count: int,
    initial_disposition: DispositionChain | None = None,
) -> FunnelCheckpoint:
    if stage not in {"stage-b", "stage-c"}:
        raise ValueError("candidate stage must be stage-b or stage-c")
    if len(scorer_digest) != 64:
        raise ValueError("candidate-stage scorer_digest must be SHA-256 hex")
    try:
        bytes.fromhex(scorer_digest)
    except ValueError as error:
        raise ValueError("candidate-stage scorer_digest is not hex") from error
    if stage == "stage-b" and (
        len(candidates) != stream.config.stage_a_keep
        or keep_count != stream.config.stage_b_keep
    ):
        raise ValueError("stage-B requires the exact 65536-to-8192 cut")
    if stage == "stage-c" and (
        not stream.config.stage_c_keep <= len(candidates) <= stream.config.stage_b_keep
        or keep_count != stream.config.stage_c_keep
    ):
        raise ValueError("stage-C requires 512..8192 phenotypes and keeps 512")
    if len({item.effective_id for item in candidates}) != len(candidates):
        raise ValueError("candidate cut contains duplicate effective ids")
    heap = BoundedRankHeap(keep_count)
    chain = initial_disposition or DispositionChain()
    for prior in candidates:
        member = stream.member(prior.candidate_index)
        if (member.effective_id, member.profile.profile_id) != (
            prior.effective_id,
            prior.profile_id,
        ):
            raise ValueError("candidate cannot be regenerated from its index")
        units = _rank_units(scorer(member))
        ranked = RankedCandidate(
            member.candidate_index,
            member.effective_id,
            member.profile.profile_id,
            units,
        )
        heap.add(ranked)
        chain = chain.update(
            candidate_index=member.candidate_index,
            effective_id=member.effective_id,
            profile_id=member.profile.profile_id,
            stage=stage,
            disposition="scored",
            rank_units=units,
        )
    return FunnelCheckpoint(
        protocol_digest=protocol_digest,
        stage=stage,
        scorer_digest=scorer_digest,
        input_size=len(candidates),
        keep_count=keep_count,
        next_input_offset=len(candidates),
        generator_index=(
            candidates[-1].candidate_index if candidates else None
        ),
        disposition=chain,
        ranked_candidates=heap.ordered(),
        complete=True,
    )


def behavioral_signature(rows: Sequence[Mapping[str, Any]]) -> str:
    normalized = []
    seen: set[str] = set()
    for row in sorted(rows, key=lambda item: str(item["case_id"])):
        case_id = str(row["case_id"])
        if not case_id or case_id in seen:
            raise ValueError("behavioral rows need unique non-empty case ids")
        seen.add(case_id)
        score = row["clipped_score"]
        if isinstance(score, bool) or not isinstance(score, int):
            raise ValueError("behavioral clipped_score must be an integer")
        normalized.append(
            {
                "case_id": case_id,
                "selected_series": str(row["selected_series"]),
                "clipped_score": score,
            }
        )
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def collapse_behavioral_phenotypes(
    candidates: Sequence[RankedCandidate],
    signatures: Mapping[str, str],
) -> tuple[RankedCandidate, ...]:
    representatives: dict[str, RankedCandidate] = {}
    for candidate in sorted(candidates, key=lambda item: item.rank_key):
        signature = signatures.get(candidate.effective_id)
        if not isinstance(signature, str) or len(signature) != 64:
            raise ValueError(
                f"missing behavioral signature for {candidate.effective_id}"
            )
        representatives.setdefault(signature, candidate)
    return tuple(
        sorted(representatives.values(), key=lambda item: item.rank_key)
    )


def _validate_human_gate(
    profile_id: str,
    gate: Mapping[str, Any],
    *,
    depth: int,
    max_work: int,
) -> None:
    if (
        gate.get("gate_id") != HUMAN_REFUTATION_GATE_ID
        or gate.get("profile_id") != profile_id
    ):
        raise ValueError("human-refutation gate identity mismatch")
    limits = gate.get("limits")
    if not isinstance(limits, Mapping) or (
        limits.get("depth_series") != depth
        or limits.get("max_series_per_node") != 32
        or limits.get("max_generation_positions") != max_work
        or limits.get("time_limit_seconds") is not None
        or limits.get("collect_all_root_scores") is not False
    ):
        raise ValueError("human-refutation gate limits are not frozen")
    anchors = gate.get("anchors")
    if (
        not isinstance(anchors, Sequence)
        or isinstance(anchors, (str, bytes))
        or {
            item.get("series_number")
            for item in anchors
            if isinstance(item, Mapping)
        }
        != {2, 4}
    ):
        raise ValueError("human-refutation gate anchors are incomplete")
    if gate.get("passed") is not True:
        raise ValueError("human-refutation gate failed")
    for anchor in anchors:
        if not isinstance(anchor, Mapping) or (
            anchor.get("selected_series") is None
            or anchor.get("requested_depth") != depth
            or anchor.get("completed_depth") != depth
            or anchor.get("timed_out") is not False
            or anchor.get("work_limit_reached") is not False
            or anchor.get("completed_required_search") is not True
            or anchor.get("avoided_known_blunder") is not True
            or anchor.get("passed") is not True
        ):
            raise ValueError("human-refutation anchor did not fully pass")


def stamp_tactical_bundle(
    candidate: RankedCandidate,
    *,
    protocol_digest: str,
    native_source_identity: str,
    runtime_identity_digest: str,
    rules_tactical_gate: Mapping[str, Any],
    human_refutation_gate: Mapping[str, Any],
    depth: int = 2,
    max_work: int = 250_000,
) -> dict[str, Any]:
    deterministic = {
        "format": "spc-tactical-bundle-v1",
        "effective_id": candidate.effective_id,
        "profile_id": candidate.profile_id,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "native_source_identity": native_source_identity,
        "runtime_identity_digest": runtime_identity_digest,
        "protocol_digest": protocol_digest,
        "limits": {
            "depth_series": depth,
            "max_series_per_node": 32,
            "max_generation_positions": max_work,
            "time_limit_seconds": None,
            "collect_all_root_scores": False,
        },
        "rules_tactical_gate": dict(rules_tactical_gate),
        "human_refutation_gate": dict(human_refutation_gate),
    }
    return {
        **deterministic,
        "artifact_digest": canonical_digest(
            "spc-tactical-bundle-v1\0", deterministic
        ),
    }


def stamp_human_gate_artifact(
    candidate: RankedCandidate,
    *,
    protocol_digest: str,
    native_source_identity: str,
    runtime_identity_digest: str,
    human_refutation_gate: Mapping[str, Any],
    depth: int,
    max_work: int,
) -> dict[str, Any]:
    deterministic = {
        "format": "spc-human-gate-artifact-v1",
        "effective_id": candidate.effective_id,
        "profile_id": candidate.profile_id,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "native_source_identity": native_source_identity,
        "runtime_identity_digest": runtime_identity_digest,
        "protocol_digest": protocol_digest,
        "depth": depth,
        "max_work": max_work,
        "human_refutation_gate": dict(human_refutation_gate),
    }
    return {
        **deterministic,
        "artifact_digest": canonical_digest(
            "spc-human-gate-artifact-v1\0", deterministic
        ),
    }


def _validate_artifact_identity(
    artifact: Mapping[str, Any],
    *,
    format_name: str,
    protocol_digest: str,
    native_source_identity: str,
    runtime_identity_digest: str,
    digest_domain: str,
) -> None:
    if (
        artifact.get("format") != format_name
        or artifact.get("source_fingerprint") != ENGINE_SOURCE_FINGERPRINT
        or artifact.get("protocol_digest") != protocol_digest
        or artifact.get("native_source_identity") != native_source_identity
        or artifact.get("runtime_identity_digest") != runtime_identity_digest
    ):
        raise ValueError("gate artifact source/protocol identity mismatch")
    deterministic = {
        key: value for key, value in artifact.items() if key != "artifact_digest"
    }
    if artifact.get("artifact_digest") != canonical_digest(
        digest_domain, deterministic
    ):
        raise ValueError("gate artifact digest mismatch")


def validate_tactical_bundle(
    candidate: RankedCandidate,
    bundle: Mapping[str, Any],
    *,
    protocol_digest: str,
    native_source_identity: str,
    runtime_identity_digest: str,
    depth: int = 2,
    max_work: int = 250_000,
) -> None:
    _validate_artifact_identity(
        bundle,
        format_name="spc-tactical-bundle-v1",
        protocol_digest=protocol_digest,
        native_source_identity=native_source_identity,
        runtime_identity_digest=runtime_identity_digest,
        digest_domain="spc-tactical-bundle-v1\0",
    )
    if (
        bundle.get("effective_id") != candidate.effective_id
        or bundle.get("profile_id") != candidate.profile_id
    ):
        raise ValueError("tactical bundle identity mismatch")
    rules = bundle.get("rules_tactical_gate")
    checks = rules.get("checks") if isinstance(rules, Mapping) else None
    if (
        not isinstance(rules, Mapping)
        or rules.get("passed") is not True
        or not isinstance(checks, Sequence)
        or isinstance(checks, (str, bytes))
        or not checks
        or any(
            not isinstance(check, Mapping) or check.get("passed") is not True
            for check in checks
        )
    ):
        raise ValueError("rules tactical gate failed or is missing")
    limits = bundle.get("limits")
    if not isinstance(limits, Mapping) or (
        limits.get("depth_series") != depth
        or limits.get("max_series_per_node") != 32
        or limits.get("max_generation_positions") != max_work
        or limits.get("time_limit_seconds") is not None
        or limits.get("collect_all_root_scores") is not False
    ):
        raise ValueError("tactical bundle limits are not frozen")
    human = bundle.get("human_refutation_gate")
    if not isinstance(human, Mapping):
        raise ValueError("human-refutation gate is missing")
    _validate_human_gate(
        candidate.profile_id, human, depth=depth, max_work=max_work
    )


def finalize_survivors(
    stage_c: Sequence[RankedCandidate],
    stage_b: Sequence[RankedCandidate],
    tactical_bundles: Mapping[str, Mapping[str, Any]],
    *,
    protocol_digest: str,
    native_source_identity: str,
    runtime_identity_digest: str,
    config: TournamentFunnelConfig | None = None,
) -> dict[str, Any]:
    selected = config or TournamentFunnelConfig()
    if len(stage_c) != selected.stage_c_keep:
        raise ValueError("tactical screening requires the exact 512 Stage-C cut")
    if len(stage_b) != selected.stage_b_keep:
        raise ValueError("tactical fallback requires the exact 8192 Stage-B cut")
    if tuple(sorted(stage_c, key=lambda item: item.rank_key)) != tuple(stage_c):
        raise ValueError("Stage-C candidates are not in canonical rank order")
    if tuple(sorted(stage_b, key=lambda item: item.rank_key)) != tuple(stage_b):
        raise ValueError("Stage-B candidates are not in canonical rank order")
    stage_b_ids = {candidate.effective_id for candidate in stage_b}
    if len(stage_b_ids) != len(stage_b) or not {
        candidate.effective_id for candidate in stage_c
    } <= stage_b_ids:
        raise ValueError("Stage-C must be a unique subset of Stage-B")
    ordered: list[RankedCandidate] = []
    seen: set[str] = set()
    for candidate in (*stage_c, *stage_b):
        if candidate.effective_id not in seen:
            seen.add(candidate.effective_id)
            ordered.append(candidate)
    survivors: list[RankedCandidate] = []
    dispositions: list[dict[str, Any]] = []
    for candidate in ordered:
        bundle = tactical_bundles.get(candidate.effective_id)
        if bundle is None:
            # Exact gates are run in order. Missing evidence stops the prefix;
            # silently hopping over it would turn test availability into a
            # hidden selection signal.
            break
        try:
            validate_tactical_bundle(
                candidate,
                bundle,
                protocol_digest=protocol_digest,
                native_source_identity=native_source_identity,
                runtime_identity_digest=runtime_identity_digest,
            )
        except ValueError as error:
            dispositions.append(
                {
                    "effective_id": candidate.effective_id,
                    "passed": False,
                    "reason": str(error),
                    "tactical_bundle_digest": canonical_digest(
                        "spc-tactical-bundle-v1\0", bundle
                    ),
                }
            )
            continue
        survivors.append(candidate)
        dispositions.append(
            {
                "effective_id": candidate.effective_id,
                "passed": True,
                "reason": "passed",
                "tactical_bundle_digest": canonical_digest(
                    "spc-tactical-bundle-v1\0", bundle
                ),
            }
        )
        if len(survivors) == selected.survivor_count:
            break
    status = (
        "ready"
        if len(survivors) == selected.survivor_count
        else "insufficient-tactical-survivors"
    )
    deterministic = {
        "format": SURVIVOR_SET_FORMAT,
        "protocol_digest": protocol_digest,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "status": status,
        "survivors": [candidate.as_dict() for candidate in survivors],
        "tactical_dispositions": dispositions,
        "selection_order": "stage-C rank then remaining stage-B rank",
        "gate_contract": {
            "rules_tactical_gate": True,
            "human_refutation_gate": HUMAN_REFUTATION_GATE_ID,
            "depth_series": 2,
            "branch_cap": 32,
            "max_generation_positions": 250_000,
            "time_limit_seconds": None,
            "never_relax": True,
        },
        "promotion_effect": "none",
    }
    return {
        **deterministic,
        "survivor_set_digest": canonical_digest(
            "spc-challenger-survivors-v1\0", deterministic
        ),
    }


def _pot_groups(
    candidates: Sequence[RankedCandidate],
    *,
    protocol_digest: str,
    config: TournamentFunnelConfig,
) -> dict[str, list[RankedCandidate]]:
    if (
        len(candidates) != 64
        or len({item.effective_id for item in candidates}) != 64
    ):
        raise ValueError("exactly 64 unique ranked survivors are required")
    groups = {f"group-{index:02d}": [] for index in range(1, 9)}
    group_seed = seed64(
        "group-assignment", master_seed=config.master_seed
    )
    for pot_index in range(8):
        pot = candidates[pot_index * 8 : (pot_index + 1) * 8]
        permuted = sorted(
            pot,
            key=lambda item: (
                hashlib.sha256(
                    struct.pack(">QB", group_seed, pot_index)
                    + protocol_digest.encode("ascii")
                    + item.effective_id.encode("ascii")
                ).digest(),
                item.effective_id,
            ),
        )
        for group_index, candidate in enumerate(permuted, 1):
            groups[f"group-{group_index:02d}"].append(candidate)
    return groups


def tournament_limits_for_stage(
    stage: str, *, config: TournamentFunnelConfig | None = None
) -> dict[str, Any]:
    selected = config or TournamentFunnelConfig()
    if stage == "group":
        depth = selected.search_depth
        search_work = selected.max_generation_positions
        game_work = selected.max_game_work_positions
        role = "depth-2-non-promoting-group-screen"
    elif stage in {
        "round-of-16",
        "quarterfinal",
        "semifinal",
        "challenger-final",
        "baseline-final",
    }:
        depth = selected.decisive_search_depth
        search_work = selected.decisive_max_generation_positions
        game_work = selected.decisive_max_game_work_positions
        role = "depth-3-decisive-strength-match"
    else:
        raise ValueError(f"unknown tournament stage limits: {stage}")
    return {
        "depth_series": depth,
        "branch_cap_complete_series_per_node": selected.max_series_per_node,
        "max_work_positions_per_search": search_work,
        "max_game_work_positions": game_work,
        "emergency_max_series": None,
        "time_limit_seconds": None,
        "collect_all_root_scores": False,
        "fresh_searcher_each_series": True,
        "stage_role": role,
        "identical_limits_for_both_colors": True,
    }


def strength_report_limits(
    stage_limits: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        stage_limits.get("collect_all_root_scores") is not False
        or stage_limits.get("fresh_searcher_each_series") is not True
        or stage_limits.get("identical_limits_for_both_colors") is not True
    ):
        raise ValueError("stage limits do not preserve symmetric fresh searches")
    return {
        "depth_series": stage_limits["depth_series"],
        "branch_cap_complete_series_per_node": stage_limits[
            "branch_cap_complete_series_per_node"
        ],
        "max_work_positions_per_search": stage_limits[
            "max_work_positions_per_search"
        ],
        "max_game_work_positions": stage_limits["max_game_work_positions"],
        "game_work_definition": (
            "deterministic logical positions across complete-series "
            "generation, evaluation reach, and quiet adjudication over "
            "the whole game"
        ),
        "emergency_max_series": stage_limits["emergency_max_series"],
        "emergency_series_note": (
            "null means unbounded by series number; any configured value "
            "is a technical watchdog, never a chess rule or draw cutoff"
        ),
        "time_limit_seconds": stage_limits["time_limit_seconds"],
        "node_limit": None,
        "node_note": (
            "nodes are measured, not capped; both profiles receive the "
            "same deterministic depth, branch, and generation-work limits"
        ),
        "fresh_searcher_each_series": True,
        "collect_all_root_scores": False,
        "root_score_mode": "best-only-play-optimized",
        "same_for_both_profiles": True,
    }


def tournament_opening_retry_policy(
    *, config: TournamentFunnelConfig | None = None
) -> dict[str, Any]:
    """Returns the frozen, score-blind whole-pair replacement contract."""

    selected = config or TournamentFunnelConfig()
    return {
        "format": OPENING_RETRY_POLICY_FORMAT,
        "max_replacement_attempts": selected.replacement_opening_attempts,
        "total_attempts": selected.replacement_opening_attempts + 1,
        "attempt_order": "attempt-index-ascending",
        "retry_trigger": "whole-color-swapped-pair-incomplete-only",
        "retry_eligible_terminal_reasons": ["manual-adjudication-pending"],
        "selection_rule": "first-complete-whole-color-swapped-pair",
        "completed_pair_retry_allowed": False,
        "engine_or_profile_failure": "fail-closed-no-retry-no-seal",
        "worker_or_shared_technical_failure": "fail-closed-no-retry-no-seal",
        "competitive_result_fields_used_for_ordering": [],
        "retry_trigger_fields": ["pair_completion", "game_completion"],
        "exhaustion": "fail-closed-without-sealed-match-report",
        "calibration_scope": "same-pre-seal-policy",
    }


def opening_reserve_digest(reserve: Mapping[str, Any]) -> str:
    """Content-addresses one logical domain and all pre-frozen attempt lanes."""

    deterministic = {
        key: value for key, value in reserve.items() if key != "reserve_digest"
    }
    return canonical_digest("spc-tournament-opening-reserve-v1\0", deterministic)


def _match_spec(
    *,
    protocol_digest: str,
    stage: str,
    matchup_id: str,
    first_slot: str,
    second_slot: str,
    opening_domain: str,
    base_pairs: int,
    maximum_pairs: int,
    config: TournamentFunnelConfig,
    promotion_batch_chain_digest: str,
) -> dict[str, Any]:
    batch_domain = f"promotion-batch|{promotion_batch_chain_digest}"
    return {
        "stage": stage,
        "matchup_id": matchup_id,
        "first_slot": first_slot,
        "second_slot": second_slot,
        "opening_domain": opening_domain,
        "opening_seed": seed64(
            f"{batch_domain}|opening|{opening_domain}",
            master_seed=config.master_seed,
        ),
        "match_seed": seed64(
            f"{batch_domain}|match-config|{stage}|{matchup_id}",
            master_seed=config.master_seed,
        ),
        "base_pairs": base_pairs,
        "maximum_pairs": maximum_pairs,
        "base_games": base_pairs * 2,
        "maximum_games": maximum_pairs * 2,
        "limits": tournament_limits_for_stage(stage, config=config),
        "opening_retry_policy": tournament_opening_retry_policy(config=config),
        "job_identity_parent": protocol_digest,
        "promotion_effect": "none",
    }


def build_tournament_plan(
    survivors: Sequence[RankedCandidate],
    baseline: EngineProfile,
    *,
    protocol_digest: str,
    promotion_batch: Mapping[str, Any],
    config: TournamentFunnelConfig | None = None,
    frozen_opening_suites: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    selected = config or TournamentFunnelConfig()
    baseline_effective_id = effective_profile_id(baseline.weights)
    validate_promotion_batch_artifact(
        promotion_batch,
        protocol_digest=protocol_digest,
        baseline_effective_id=baseline_effective_id,
    )
    batch_chain_digest = str(promotion_batch["chain_digest"])
    groups = _pot_groups(
        survivors, protocol_digest=protocol_digest, config=selected
    )
    group_matches: list[dict[str, Any]] = []
    for group_id, members in groups.items():
        for first_index, first in enumerate(members):
            for second in members[first_index + 1 :]:
                ordered = sorted((first.effective_id, second.effective_id))
                matchup_id = f"{group_id}-{ordered[0]}-vs-{ordered[1]}"
                group_matches.append(
                    _match_spec(
                        protocol_digest=protocol_digest,
                        stage="group",
                        matchup_id=matchup_id,
                        first_slot=ordered[0],
                        second_slot=ordered[1],
                        opening_domain=f"{group_id}-openings",
                        base_pairs=50,
                        maximum_pairs=50,
                        config=selected,
                        promotion_batch_chain_digest=batch_chain_digest,
                    )
                )
    group_ids = tuple(groups)
    r16_slots = [
        (f"{group_ids[index]}-rank-1", f"{group_ids[7 - index]}-rank-2")
        for index in range(8)
    ]
    r16 = [
        _match_spec(
            protocol_digest=protocol_digest,
            stage="round-of-16",
            matchup_id=f"r16-{index:02d}",
            first_slot=first,
            second_slot=second,
            opening_domain="r16-openings",
            base_pairs=50,
            maximum_pairs=50,
            config=selected,
            promotion_batch_chain_digest=batch_chain_digest,
        )
        for index, (first, second) in enumerate(r16_slots, 1)
    ]
    quarterfinals = [
        _match_spec(
            protocol_digest=protocol_digest,
            stage="quarterfinal",
            matchup_id=f"qf-{index:02d}",
            first_slot=f"r16-{index:02d}-winner",
            second_slot=f"r16-{9 - index:02d}-winner",
            opening_domain="qf-openings",
            base_pairs=50,
            maximum_pairs=100,
            config=selected,
            promotion_batch_chain_digest=batch_chain_digest,
        )
        for index in range(1, 5)
    ]
    semifinals = [
        _match_spec(
            protocol_digest=protocol_digest,
            stage="semifinal",
            matchup_id=f"semifinal-{index:02d}",
            first_slot=first,
            second_slot=second,
            opening_domain="semifinal-openings",
            base_pairs=50,
            maximum_pairs=100,
            config=selected,
            promotion_batch_chain_digest=batch_chain_digest,
        )
        for index, (first, second) in enumerate(
            (
                ("qf-01-winner", "qf-04-winner"),
                ("qf-02-winner", "qf-03-winner"),
            ),
            1,
        )
    ]
    challenger_final = [
        _match_spec(
            protocol_digest=protocol_digest,
            stage="challenger-final",
            matchup_id="challenger-final",
            first_slot="semifinal-01-winner",
            second_slot="semifinal-02-winner",
            opening_domain="challenger-final-openings",
            base_pairs=50,
            maximum_pairs=100,
            config=selected,
            promotion_batch_chain_digest=batch_chain_digest,
        )
    ]
    baseline_final = [
        _match_spec(
            protocol_digest=protocol_digest,
            stage="baseline-final",
            matchup_id="fresh-baseline-promotion-final",
            first_slot="challenger-final-winner",
            second_slot=baseline_effective_id,
            opening_domain="baseline-final-openings",
            base_pairs=200,
            maximum_pairs=200,
            config=selected,
            promotion_batch_chain_digest=batch_chain_digest,
        )
    ]
    expected_opening_counts = (
        *((f"group-{index:02d}-openings", 50) for index in range(1, 9)),
        ("r16-openings", 50),
        ("qf-openings", 100),
        ("semifinal-openings", 100),
        ("challenger-final-openings", 100),
        ("baseline-final-openings", 200),
    )
    retry_policy = tournament_opening_retry_policy(config=selected)
    if frozen_opening_suites is None:
        opening_suites = [
            {
                "domain": domain,
                "count": count,
                "min_series": 3,
                "max_series": 6,
                "max_frontier_states": 32,
                "global_position_hash_exclusion_required": True,
                "retry_policy": copy.deepcopy(retry_policy),
                "attempt_lanes": [
                    {
                        "attempt_index": attempt_index,
                        "seed": seed64(
                            "promotion-batch|"
                            f"{batch_chain_digest}|opening|{domain}|"
                            f"attempt|{attempt_index}",
                            master_seed=selected.master_seed,
                        ),
                        "count": count,
                    }
                    for attempt_index in range(TOTAL_OPENING_ATTEMPTS)
                ],
            }
            for domain, count in expected_opening_counts
        ]
    else:
        opening_suites = [copy.deepcopy(dict(item)) for item in frozen_opening_suites]
        if [
            (item.get("domain"), item.get("count")) for item in opening_suites
        ] != list(expected_opening_counts):
            raise ValueError("frozen opening suite domains/counts are not canonical")
        if any(
            item.get("min_series") != 3
            or item.get("max_series") != 6
            or item.get("max_frontier_states") != 32
            or item.get("global_position_hash_exclusion_required") is not True
            or item.get("retry_policy") != retry_policy
            or not isinstance(item.get("attempt_lanes"), list)
            or len(item["attempt_lanes"]) != TOTAL_OPENING_ATTEMPTS
            or not isinstance(item.get("reserve_digest"), str)
            or item.get("reserve_digest") != opening_reserve_digest(item)
            or not isinstance(item.get("position_hash_digest"), str)
            or item.get("total_case_count")
            != int(item.get("count", -1)) * TOTAL_OPENING_ATTEMPTS
            or any(
                not isinstance(lane, Mapping)
                or lane.get("attempt_index") != attempt_index
                or lane.get("count") != item.get("count")
                or not isinstance(lane.get("suite"), Mapping)
                or not isinstance(lane.get("suite_digest"), str)
                or not isinstance(lane.get("position_hash_digest"), str)
                or type(lane.get("selection_nonce")) is not int
                or not 0 <= int(lane["selection_nonce"]) <= 65_535
                or type(lane.get("selection_master_seed")) is not int
                or type(lane.get("base_seed")) is not int
                for attempt_index, lane in enumerate(item["attempt_lanes"])
            )
            for item in opening_suites
        ):
            raise ValueError("frozen opening reserve selection is incomplete")
        reserve_digest_by_domain = {
            str(item["domain"]): str(item["reserve_digest"])
            for item in opening_suites
        }
        for specs in (
            group_matches,
            r16,
            quarterfinals,
            semifinals,
            challenger_final,
            baseline_final,
        ):
            for spec in specs:
                spec["opening_reserve_digest"] = reserve_digest_by_domain[
                    str(spec["opening_domain"])
                ]
                spec["opening_retry_policy"] = copy.deepcopy(retry_policy)
    deterministic = {
        "format": TOURNAMENT_PROTOCOL_FORMAT,
        "protocol_digest": protocol_digest,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "baseline": {
            "profile_id": baseline.profile_id,
            "effective_id": baseline_effective_id,
        },
        "promotion_batch": copy.deepcopy(dict(promotion_batch)),
        "survivors_in_validation_rank_order": [
            item.as_dict() for item in survivors
        ],
        "groups": {
            group_id: [item.as_dict() for item in members]
            for group_id, members in groups.items()
        },
        "opening_suites": opening_suites,
        "matchups": {
            "group": group_matches,
            "round_of_16": r16,
            "quarterfinal": quarterfinals,
            "semifinal": semifinals,
            "challenger_final": challenger_final,
            "baseline_final": baseline_final,
        },
        "scheduled": {
            "group_matchups": 224,
            "group_games": 22_400,
            "round_of_16_matchups": 8,
            "round_of_16_games": 800,
            "base_late_knockout_games": 700,
            "expanded_late_knockout_games": 1_400,
            "baseline_final_games": 400,
            "base_total_games": 24_300,
            "expanded_total_games": 25_000,
            "base_total_pairs": 12_150,
            "expanded_total_pairs": 12_500,
            "replacement_opening_attempts": MAX_REPLACEMENT_OPENING_ATTEMPTS,
            "nominal_selected_games": {
                "base": 24_300,
                "expanded": 25_000,
            },
            "worst_case_executed_games": {
                "base": 24_300 * TOTAL_OPENING_ATTEMPTS,
                "expanded": 25_000 * TOTAL_OPENING_ATTEMPTS,
            },
        },
        "strength_contract": {
            "group_screen": {
                "stage": "group",
                "can_promote": False,
                "base_games": 22_400,
                "expanded_games": 22_400,
                "limits": tournament_limits_for_stage("group", config=selected),
            },
            "decisive_matches": {
                "stages": [
                    "round-of-16",
                    "quarterfinal",
                    "semifinal",
                    "challenger-final",
                    "baseline-final",
                ],
                "base_games": 1_900,
                "expanded_games": 2_600,
                "minimum_games_per_matchup": 100,
                "baseline_final_games": 400,
                "limits": tournament_limits_for_stage(
                    "round-of-16", config=selected
                ),
            },
            "variant_search_advantage": False,
            "color_swap_uses_identical_limits": True,
        },
        "advancement": {
            "group_advancers": 16,
            "group_stage_can_promote": False,
            "knockout_can_promote": False,
            "knockout_exact_tie": (
                "higher preregistered pre-round seed advances administratively"
            ),
            "baseline_final_required": True,
        },
        "trace_contract": (
            "every persisted game joins the neutral S1 prefix to the engine "
            "continuation and ends only at a true terminal or honest incomplete"
        ),
        "opening_retry_policy": copy.deepcopy(retry_policy),
        "audit_policy": "sealed until challenger-final winner is frozen",
    }
    return {
        **deterministic,
        "tournament_plan_digest": canonical_digest(
            "spc-tournament-plan-v1\0", deterministic
        ),
    }


def choose_result_blind_expansion(
    *,
    protocol_digest: str,
    calibration_timing_evidence: Sequence[Mapping[str, Any]],
    fixed_overhead_reserve_seconds: float,
    config: TournamentFunnelConfig | None = None,
) -> dict[str, Any]:
    """Chooses the schedule from the first ten matchup timings only.

    The numerator is the 1,000 selected logical calibration games. Elapsed
    time includes every pre-seal replacement wave, so retries make measured
    throughput honestly slower without changing the remaining logical schedule.
    Scores and WDL are absent and cannot affect the choice.
    """

    selected = config or TournamentFunnelConfig()
    normalized_evidence: list[dict[str, Any]] = []
    if (
        not isinstance(calibration_timing_evidence, Sequence)
        or isinstance(calibration_timing_evidence, (str, bytes))
        or len(calibration_timing_evidence) != 10
    ):
        raise ValueError(
            "expansion decision requires exactly the first 10 group matchups"
        )
    required_timing_fields = {
        "stage",
        "matchup_id",
        "ordinal",
        "pair_records",
        "selected_game_records",
        "executed_game_records",
        "execution_wall_seconds",
    }
    seen_matchups: set[str] = set()
    for expected_ordinal, raw in enumerate(calibration_timing_evidence):
        if not isinstance(raw, Mapping) or set(raw) != required_timing_fields:
            raise ValueError("calibration timing evidence fields are not canonical")
        matchup_id = str(raw["matchup_id"])
        try:
            ordinal = int(raw["ordinal"])
            pair_records = int(raw["pair_records"])
            selected_game_records = int(raw["selected_game_records"])
            game_records = int(raw["executed_game_records"])
            wall_seconds = float(raw["execution_wall_seconds"])
        except (TypeError, ValueError) as error:
            raise ValueError("calibration timing evidence is invalid") from error
        if (
            raw["stage"] != "group"
            or not matchup_id
            or matchup_id in seen_matchups
            or ordinal != expected_ordinal
            or pair_records != 50
            or selected_game_records != 100
            or not 100 <= game_records <= 300
            or game_records % 2
            or not math.isfinite(wall_seconds)
            or wall_seconds <= 0
        ):
            raise ValueError(
                "calibration must be the first 10 completed canonical matchups"
            )
        seen_matchups.add(matchup_id)
        normalized_evidence.append(
            {
                "stage": "group",
                "matchup_id": matchup_id,
                "ordinal": ordinal,
                "pair_records": pair_records,
                "selected_game_records": selected_game_records,
                "executed_game_records": game_records,
                "execution_wall_seconds": wall_seconds,
            }
        )
    selected_group_game_records = sum(
        row["selected_game_records"] for row in normalized_evidence
    )
    executed_group_game_records = sum(
        row["executed_game_records"] for row in normalized_evidence
    )
    observed_execution_wall_seconds = sum(
        row["execution_wall_seconds"] for row in normalized_evidence
    )
    if selected_group_game_records != 1_000:
        raise ValueError(
            "expansion decision requires exactly 1000 selected logical game records"
        )
    if (
        not math.isfinite(fixed_overhead_reserve_seconds)
        or fixed_overhead_reserve_seconds < 0
    ):
        raise ValueError(
            "fixed_overhead_reserve_seconds must be finite and non-negative"
        )
    throughput = selected_group_game_records / observed_execution_wall_seconds
    remaining_group_games = 22_400 - selected_group_game_records
    expanded_decisive_games = 2_600
    projected_remaining_screening = remaining_group_games / throughput
    projected_expanded_decisive = (
        expanded_decisive_games * DECISIVE_TIMING_MULTIPLIER / throughput
    )
    projected_finish = (
        observed_execution_wall_seconds
        + projected_remaining_screening
        + projected_expanded_decisive
        + fixed_overhead_reserve_seconds
    )
    expanded = projected_finish <= selected.expansion_budget_seconds
    deterministic = {
        "format": EXPANSION_DECISION_FORMAT,
        "protocol_digest": protocol_digest,
        "calibration_scope": "first-10-frozen-group-matchups-in-plan-order",
        "calibration_matchup_count": len(normalized_evidence),
        "calibration_pair_records": sum(
            row["pair_records"] for row in normalized_evidence
        ),
        "selected_group_game_records": selected_group_game_records,
        "executed_group_game_records": executed_group_game_records,
        "observed_execution_wall_seconds": observed_execution_wall_seconds,
        "calibration_timing_evidence": normalized_evidence,
        "fixed_overhead_reserve_seconds": fixed_overhead_reserve_seconds,
        "observed_selected_game_records_per_second": throughput,
        "remaining_depth2_screening_game_records": remaining_group_games,
        "projected_remaining_screening_seconds": projected_remaining_screening,
        "expanded_depth3_decisive_games": expanded_decisive_games,
        "decisive_depth3_timing_multiplier": DECISIVE_TIMING_MULTIPLIER,
        "projected_expanded_decisive_seconds": projected_expanded_decisive,
        "projected_expanded_finish_seconds": projected_finish,
        "budget_seconds": selected.expansion_budget_seconds,
        "schedule": "expanded" if expanded else "base",
        "selected_total_games": 25_000 if expanded else 24_300,
        "result_fields_consumed": [],
    }
    return {
        **deterministic,
        "decision_digest": canonical_digest(
            "spc-result-blind-expansion-v3\0", deterministic
        ),
    }


def validate_result_blind_expansion_decision(
    artifact: Mapping[str, Any],
    *,
    protocol_digest: str,
    config: TournamentFunnelConfig | None = None,
) -> None:
    """Rebuilds the result-blind schedule choice from its timing-only inputs."""

    required = {
        "format",
        "protocol_digest",
        "calibration_scope",
        "calibration_matchup_count",
        "calibration_pair_records",
        "selected_group_game_records",
        "executed_group_game_records",
        "observed_execution_wall_seconds",
        "calibration_timing_evidence",
        "fixed_overhead_reserve_seconds",
        "observed_selected_game_records_per_second",
        "remaining_depth2_screening_game_records",
        "projected_remaining_screening_seconds",
        "expanded_depth3_decisive_games",
        "decisive_depth3_timing_multiplier",
        "projected_expanded_decisive_seconds",
        "projected_expanded_finish_seconds",
        "budget_seconds",
        "schedule",
        "selected_total_games",
        "result_fields_consumed",
        "decision_digest",
    }
    if set(artifact) != required:
        raise ValueError("expansion decision fields are not canonical")
    if artifact.get("protocol_digest") != protocol_digest:
        raise ValueError("expansion decision belongs to a different protocol")
    rebuilt = choose_result_blind_expansion(
        protocol_digest=protocol_digest,
        calibration_timing_evidence=artifact["calibration_timing_evidence"],
        fixed_overhead_reserve_seconds=float(
            artifact["fixed_overhead_reserve_seconds"]
        ),
        config=config,
    )
    if dict(artifact) != rebuilt:
        raise ValueError("expansion decision is not the canonical timing-only choice")


def tournament_job_key(
    *,
    protocol_digest: str,
    stage: str,
    matchup_id: str,
    opening_boundary_digest: str,
    pair_index: int,
    pair_seed: int,
    white_effective_id: str,
    black_effective_id: str,
) -> str:
    if pair_index < 0:
        raise ValueError("pair_index cannot be negative")
    payload = {
        "protocol_digest": protocol_digest,
        "stage": stage,
        "matchup_id": matchup_id,
        "opening_boundary_digest": opening_boundary_digest,
        "pair_index": pair_index,
        "pair_seed": pair_seed,
        "white_effective_id": white_effective_id,
        "black_effective_id": black_effective_id,
    }
    return "spc-job-" + hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()


def match_pair_seed(
    stage: str,
    matchup_id: str,
    pair_index: int,
    case_id: str,
    *,
    config: TournamentFunnelConfig | None = None,
) -> int:
    if pair_index < 0 or not stage or not matchup_id or not case_id:
        raise ValueError("pair seed identity fields are invalid")
    selected = config or TournamentFunnelConfig()
    return seed64(
        f"match-pair|{stage}|{matchup_id}|{pair_index}|{case_id}",
        master_seed=selected.master_seed,
    )


def build_tournament_run_checkpoint(
    *,
    tournament_plan: Mapping[str, Any],
    completed_pairs: Sequence[Mapping[str, Any]],
    expansion_decision_digest: str | None,
) -> dict[str, Any]:
    """Builds a sealed resume point from content-bound color-swapped pairs.

    This preparation checkpoint deliberately cannot derive bracket winners or
    unseal the audit split. Those transitions require complete, replay-verified
    match reports and belong in the result runner, not a caller-supplied map.
    """

    if tournament_plan.get("format") != TOURNAMENT_PROTOCOL_FORMAT:
        raise ValueError("unsupported tournament plan")
    plan_digest = tournament_plan.get("tournament_plan_digest")
    plan_deterministic = {
        key: value
        for key, value in tournament_plan.items()
        if key != "tournament_plan_digest"
    }
    if plan_digest != canonical_digest(
        "spc-tournament-plan-v1\0", plan_deterministic
    ):
        raise ValueError("tournament plan digest mismatch")
    protocol_digest = str(tournament_plan.get("protocol_digest", ""))
    if (
        not protocol_digest
        or tournament_plan.get("source_fingerprint") != ENGINE_SOURCE_FINGERPRINT
    ):
        raise ValueError("tournament plan source/protocol identity is stale")
    matchups = tournament_plan.get("matchups")
    if not isinstance(matchups, Mapping):
        raise ValueError("tournament plan matchups are missing")
    indexed_specs: dict[tuple[str, str], Mapping[str, Any]] = {}
    for stage_specs in matchups.values():
        if (
            not isinstance(stage_specs, Sequence)
            or isinstance(stage_specs, (str, bytes))
        ):
            raise ValueError("tournament plan matchup table is invalid")
        for spec in stage_specs:
            if not isinstance(spec, Mapping):
                raise ValueError("tournament match spec is not an object")
            key = (str(spec.get("stage", "")), str(spec.get("matchup_id", "")))
            if (
                not all(key)
                or key in indexed_specs
                or spec.get("job_identity_parent") != protocol_digest
            ):
                raise ValueError("tournament match spec identity is invalid")
            indexed_specs[key] = spec

    normalized_pairs: list[dict[str, Any]] = []
    all_job_keys: set[str] = set()
    all_game_record_digests: set[str] = set()
    pair_identities: set[tuple[str, str, int]] = set()
    for pair in completed_pairs:
        required = {
            "stage",
            "matchup_id",
            "pair_index",
            "opening_case_id",
            "opening_boundary_digest",
            "pair_seed",
            "first_effective_id",
            "second_effective_id",
            "game_job_keys",
            "game_record_digests",
            "bound_report_digest",
            "resolved_match_spec",
        }
        if set(pair) != required:
            raise ValueError("completed-pair checkpoint record has wrong fields")
        stage = str(pair["stage"])
        matchup_id = str(pair["matchup_id"])
        pair_index = int(pair["pair_index"])
        first = str(pair["first_effective_id"])
        second = str(pair["second_effective_id"])
        case_id = str(pair["opening_case_id"])
        if pair_index < 0 or not all(
            (
                stage,
                matchup_id,
                case_id,
                pair["opening_boundary_digest"],
                first,
                second,
            )
        ) or first == second:
            raise ValueError("completed-pair checkpoint identity is invalid")
        base_spec = indexed_specs.get((stage, matchup_id))
        resolved_spec = pair["resolved_match_spec"]
        if not isinstance(resolved_spec, Mapping) or base_spec is None:
            raise ValueError("completed pair is not in the tournament plan")
        expected_resolved_fields = {
            **dict(base_spec),
            "resolved_first_effective_id": first,
            "resolved_second_effective_id": second,
        }
        if dict(resolved_spec) != expected_resolved_fields:
            raise ValueError("completed pair resolved spec does not match the plan")
        if (
            str(base_spec.get("first_slot", "")).startswith("spc-effective-")
            and first != base_spec["first_slot"]
        ) or (
            str(base_spec.get("second_slot", "")).startswith("spc-effective-")
            and second != base_spec["second_slot"]
        ):
            raise ValueError("concrete tournament participant was replaced")
        if pair_index >= int(base_spec["maximum_pairs"]):
            raise ValueError("completed pair index exceeds its frozen matchup")
        expected_pair_seed = match_pair_seed(
            stage, matchup_id, pair_index, case_id
        )
        if int(pair["pair_seed"]) != expected_pair_seed:
            raise ValueError("completed pair seed does not match its opening")
        identity = (stage, matchup_id, pair_index)
        if identity in pair_identities:
            raise ValueError("completed-pair checkpoint contains a duplicate pair")
        pair_identities.add(identity)
        expected_keys = {
            tournament_job_key(
                protocol_digest=protocol_digest,
                stage=stage,
                matchup_id=matchup_id,
                opening_boundary_digest=str(pair["opening_boundary_digest"]),
                pair_index=pair_index,
                pair_seed=int(pair["pair_seed"]),
                white_effective_id=white,
                black_effective_id=black,
            )
            for white, black in ((first, second), (second, first))
        }
        supplied_keys = pair["game_job_keys"]
        if (
            not isinstance(supplied_keys, Sequence)
            or isinstance(supplied_keys, (str, bytes))
            or len(supplied_keys) != 2
            or set(str(value) for value in supplied_keys) != expected_keys
        ):
            raise ValueError("a completed pair needs both exact color-swap job keys")
        if all_job_keys & expected_keys:
            raise ValueError("game job key is reused across completed pairs")
        all_job_keys.update(expected_keys)
        record_digests = pair["game_record_digests"]
        if (
            not isinstance(record_digests, Sequence)
            or isinstance(record_digests, (str, bytes))
            or len(record_digests) != 2
            or len(set(str(value) for value in record_digests)) != 2
        ):
            raise ValueError("a completed pair needs two game record digests")
        normalized_record_digests = sorted(str(value) for value in record_digests)
        for value in (*normalized_record_digests, str(pair["bound_report_digest"])):
            try:
                decoded = bytes.fromhex(value)
            except ValueError as error:
                raise ValueError("completed-pair evidence digest is not hex") from error
            if len(decoded) != 32:
                raise ValueError("completed-pair evidence digest is not SHA-256")
        if all_game_record_digests & set(normalized_record_digests):
            raise ValueError("game record digest is reused across completed pairs")
        all_game_record_digests.update(normalized_record_digests)
        normalized_pairs.append(
            {
                **{
                    key: pair[key]
                    for key in required
                    if key not in {"game_job_keys", "game_record_digests"}
                },
                "pair_index": pair_index,
                "pair_seed": int(pair["pair_seed"]),
                "game_job_keys": sorted(expected_keys),
                "game_record_digests": normalized_record_digests,
            }
        )
    normalized_pairs.sort(
        key=lambda item: (
            item["stage"],
            item["matchup_id"],
            item["pair_index"],
        )
    )
    deterministic = {
        "format": "spc-tournament-run-checkpoint-v1",
        "protocol_digest": protocol_digest,
        "tournament_plan_digest": plan_digest,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "expansion_decision_digest": expansion_decision_digest,
        "completed_pairs": normalized_pairs,
        "completed_game_job_keys": sorted(all_job_keys),
        "completed_game_record_digests": sorted(all_game_record_digests),
        "winner_ids": {},
        "winner_derivation_status": "requires-verified-result-runner",
        "audit_unsealed": False,
    }
    return {
        **deterministic,
        "checkpoint_digest": canonical_digest(
            "spc-tournament-run-checkpoint-v1\0", deterministic
        ),
    }


class PositionHashRegistry:
    def __init__(self, initial: Iterable[str] = ()) -> None:
        self._owners: dict[str, str] = {}
        for value in initial:
            self.register("corpus", value)

    def register(self, owner: str, position_hash: str) -> None:
        if not owner or not position_hash:
            raise ValueError("opening registry values cannot be empty")
        previous = self._owners.get(position_hash)
        if previous is not None:
            raise ValueError(
                f"position hash collision between {previous} and {owner}: "
                f"{position_hash}"
            )
        self._owners[position_hash] = owner

    def as_dict(self) -> dict[str, Any]:
        ordered = sorted(self._owners.items())
        return {
            "count": len(ordered),
            "digest": hashlib.sha256(
                canonical_json_bytes(ordered)
            ).hexdigest(),
        }


def attach_replay_verified_full_traces(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Joins every neutral prefix to its played continuation and replays it.

    Raw strength reports keep the opening history and engine continuation in
    separate fields. Tournament persistence uses this envelope so a consumer
    can never mistake only the post-opening trace for a full game.
    """

    payload = copy.deepcopy(dict(report))
    suite = payload.get("opening_suite")
    games = payload.get("games")
    if not isinstance(suite, Mapping) or not isinstance(games, list):
        raise ValueError("strength report lacks a seeded opening suite or games")
    histories = suite.get("histories")
    if not isinstance(histories, Sequence) or isinstance(histories, (str, bytes)):
        raise ValueError("seeded opening histories are missing")
    by_case: dict[str, tuple[tuple[str, ...], ...]] = {}
    for history in histories:
        if not isinstance(history, Mapping):
            raise ValueError("opening history must be an object")
        case_id = str(history.get("case_id", ""))
        raw_series = history.get("series")
        if (
            not case_id
            or case_id in by_case
            or not isinstance(raw_series, Sequence)
            or isinstance(raw_series, (str, bytes))
        ):
            raise ValueError("opening histories have invalid identities")
        by_case[case_id] = tuple(
            tuple(str(move) for move in series) for series in raw_series
        )
    completed = 0
    incomplete = 0
    for game in games:
        if not isinstance(game, dict):
            raise ValueError("game payload must be an object")
        case_id = str(game.get("opening_case_id", ""))
        prefix = by_case.get(case_id)
        if prefix is None:
            raise ValueError(f"game references unknown opening history {case_id}")
        state = ProgressiveState.initial()
        for moves in prefix:
            result = play_series(state, moves)
            if result.is_terminal:
                raise ValueError("neutral opening prefix crosses a terminal")
            state = result.final_state
        if not 3 <= state.series_number <= 6:
            raise ValueError("engine takeover boundary must be within S3..S6")
        if state.pfen != game.get("start_pfen"):
            raise ValueError("neutral prefix does not replay to game start")
        raw_continuation = game.get("trace")
        if not isinstance(raw_continuation, Sequence) or isinstance(
            raw_continuation, (str, bytes)
        ):
            raise ValueError("game continuation trace is missing")
        continuation: list[tuple[str, ...]] = []
        last_result = None
        last_mover = None
        for row in raw_continuation:
            if not isinstance(row, Mapping):
                raise ValueError("game trace row must be an object")
            if row.get("played") is not True:
                continue
            machine = row.get("series")
            if not isinstance(machine, str) or not machine:
                raise ValueError("played trace row lacks a machine series")
            moves = tuple(machine.split("/"))
            last_mover = state.board.turn
            last_result = play_series(state, moves)
            state = last_result.final_state
            continuation.append(moves)
        if state.pfen != game.get("final_pfen"):
            raise ValueError("full trace does not replay to recorded final state")
        result_text = game.get("result")
        if result_text == "*":
            incomplete += 1
            terminal_status = "honest-incomplete"
        else:
            completed += 1
            terminal_reason = game.get("terminal_reason")
            no_material = terminal_reason == "proven-draw-no-mating-material"
            if no_material:
                if (
                    result_text != "1/2-1/2"
                    or (last_result is not None and last_result.is_terminal)
                    or not state.board.is_insufficient_material()
                    or quiet_adjudication_status(state)
                    != "proven-draw-no-mating-material"
                ):
                    raise ValueError(
                        "recorded no mating material draw is not proven by replay"
                    )
                terminal_status = "proven-no-material-boundary"
                continue_terminal_validation = False
            else:
                continue_terminal_validation = True
                if last_result is None or not last_result.is_terminal:
                    raise ValueError(
                        "completed game trace does not end at a terminal"
                    )
            if continue_terminal_validation and result_text in {"1-0", "0-1"}:
                if (
                    last_result is None
                    or last_mover is None
                    or last_result.outcome != Outcome.CHECKMATE
                    or not last_result.ended_by_check
                    or terminal_reason != Outcome.CHECKMATE.value
                ):
                    raise ValueError("decisive result is not a replayed checkmate")
                winner = last_mover
                expected = "1-0" if winner else "0-1"
                if result_text != expected:
                    raise ValueError("recorded winner disagrees with replay")
            elif continue_terminal_validation and result_text == "1/2-1/2":
                if (
                    last_result is None
                    or last_result.outcome
                    not in {Outcome.STALEMATE, Outcome.TEN_SERIES_DRAW}
                    or terminal_reason != last_result.outcome.value
                ):
                    raise ValueError("recorded draw is not a replayed draw terminal")
            elif continue_terminal_validation:
                raise ValueError("completed game result is invalid")
            if continue_terminal_validation:
                terminal_status = "true-terminal"
        game["full_trace"] = {
            "neutral_prefix": [list(moves) for moves in prefix],
            "engine_continuation": [list(moves) for moves in continuation],
            "all_series": [
                list(moves) for moves in (*prefix, *tuple(continuation))
            ],
            "terminal_status": terminal_status,
            "result": result_text,
            "terminal_reason": game.get("terminal_reason"),
            "final_pfen": game.get("final_pfen"),
        }
    payload["full_trace_evidence"] = {
        "format": "spc-full-game-trace-envelope-v1",
        "completed_games": completed,
        "incomplete_games": incomplete,
        "neutral_prefix_and_engine_continuation_replayed": True,
    }
    return payload


def _resolved_spec_effective_ids(
    match_spec: Mapping[str, Any],
) -> tuple[str, str]:
    first = match_spec.get(
        "resolved_first_effective_id", match_spec.get("first_slot")
    )
    second = match_spec.get(
        "resolved_second_effective_id", match_spec.get("second_slot")
    )
    if not (
        isinstance(first, str)
        and isinstance(second, str)
        and first.startswith("spc-effective-")
        and second.startswith("spc-effective-")
        and first != second
    ):
        raise ValueError("match spec participant slots are not resolved")
    return first, second


def _validate_opening_attempt_manifest(
    manifest: Mapping[str, Any],
    *,
    match_spec: Mapping[str, Any],
    tournament_plan_digest: str,
    pair_count: int,
) -> set[str]:
    required = {
        "format",
        "tournament_plan_digest",
        "stage",
        "matchup_id",
        "ordinal",
        "opening_reserve_digest",
        "retry_policy",
        "attempts",
        "selected_pairs",
        "unresolved_pair_indexes",
        "competitive_result_fields_used_for_attempt_ordering",
        "retry_trigger_fields",
        "attempt_manifest_digest",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != required:
        raise ValueError("opening attempt manifest fields are not canonical")
    deterministic = {
        key: value
        for key, value in manifest.items()
        if key != "attempt_manifest_digest"
    }
    digest = canonical_digest(
        "spc-tournament-opening-attempt-manifest-v1\0", deterministic
    )
    if (
        manifest.get("format")
        != "spc-tournament-opening-attempt-manifest-v1"
        or manifest.get("attempt_manifest_digest") != digest
        or match_spec.get("opening_attempt_manifest_digest") != digest
        or manifest.get("tournament_plan_digest") != tournament_plan_digest
        or manifest.get("stage") != match_spec.get("stage")
        or manifest.get("matchup_id") != match_spec.get("matchup_id")
        or manifest.get("opening_reserve_digest")
        != match_spec.get("opening_reserve_digest")
        or manifest.get("retry_policy") != tournament_opening_retry_policy()
        or manifest.get("retry_policy")
        != match_spec.get("opening_retry_policy")
        or manifest.get("competitive_result_fields_used_for_attempt_ordering")
        != []
        or manifest.get("retry_trigger_fields")
        != ["pair_completion", "game_completion"]
    ):
        raise ValueError("opening attempt manifest identity changed")
    try:
        ordinal = int(manifest["ordinal"])
    except (TypeError, ValueError) as error:
        raise ValueError("opening attempt ordinal is invalid") from error
    if ordinal < 0 or match_spec.get("tournament_ordinal") != ordinal:
        raise ValueError("opening attempt ordinal is invalid")
    attempts = manifest.get("attempts")
    if (
        not isinstance(attempts, list)
        or not 1 <= len(attempts) <= TOTAL_OPENING_ATTEMPTS
    ):
        raise ValueError("opening attempt count is outside the frozen cap")
    unresolved = list(range(pair_count))
    completed_at: dict[int, int] = {}
    attempt_fields = {
        "attempt_index",
        "unresolved_pair_indexes_in",
        "completed_pair_indexes",
        "unresolved_pair_indexes_out",
        "lane_suite_digest",
        "subset_suite_digest",
        "config_digest",
        "attempt_report_digest",
        "executed_game_records",
        "incomplete_terminal_evidence",
        "execution_elapsed_seconds",
    }
    for attempt_index, attempt in enumerate(attempts):
        if not isinstance(attempt, Mapping) or set(attempt) != attempt_fields:
            raise ValueError("opening attempt evidence fields are not canonical")
        completed = attempt.get("completed_pair_indexes")
        remaining = attempt.get("unresolved_pair_indexes_out")
        if (
            attempt.get("attempt_index") != attempt_index
            or attempt.get("unresolved_pair_indexes_in") != unresolved
            or not isinstance(completed, list)
            or not isinstance(remaining, list)
            or any(type(index) is not int for index in completed + remaining)
            or completed != sorted(set(completed))
            or remaining != sorted(set(remaining))
            or sorted(completed + remaining) != unresolved
            or int(attempt.get("executed_game_records", -1))
            != len(unresolved) * 2
            or not all(
                isinstance(attempt.get(field), str) and attempt.get(field)
                for field in (
                    "lane_suite_digest",
                    "subset_suite_digest",
                    "config_digest",
                    "attempt_report_digest",
                )
            )
        ):
            raise ValueError("opening attempt transition is not canonical")
        try:
            elapsed = float(attempt["execution_elapsed_seconds"])
        except (TypeError, ValueError) as error:
            raise ValueError("opening attempt timing is invalid") from error
        if not math.isfinite(elapsed) or elapsed <= 0:
            raise ValueError("opening attempt timing is invalid")
        terminal_evidence = attempt.get("incomplete_terminal_evidence")
        if not isinstance(terminal_evidence, Mapping) or set(terminal_evidence) != {
            "terminal_reason_counts",
            "candidate_attributed_failures",
            "reference_attributed_failures",
            "unattributed_worker_failures",
            "unattributed_match_limit_failures",
            "error_records",
        }:
            raise ValueError("opening attempt terminal evidence is not canonical")
        manual_counts = terminal_evidence.get("terminal_reason_counts")
        manual_record_count = (
            int(manual_counts.get("manual-adjudication-pending", 0))
            if isinstance(manual_counts, Mapping)
            and set(manual_counts) <= {"manual-adjudication-pending"}
            else -1
        )
        if (
            any(
                int(terminal_evidence.get(field, -1)) != 0
                for field in (
                    "candidate_attributed_failures",
                    "reference_attributed_failures",
                    "unattributed_worker_failures",
                    "unattributed_match_limit_failures",
                    "error_records",
                )
            )
            or not len(remaining) <= manual_record_count <= len(remaining) * 2
        ):
            raise ValueError("opening attempt failure evidence is not admissible")
        for logical_index in completed:
            if logical_index in completed_at:
                raise ValueError("completed opening pair was retried")
            completed_at[logical_index] = attempt_index
        unresolved = list(remaining)
        if not unresolved and attempt_index != len(attempts) - 1:
            raise ValueError("opening attempts continued after completion")
    if unresolved or manifest.get("unresolved_pair_indexes") != []:
        raise ValueError("sealed match has exhausted or unresolved opening pairs")
    selected_pairs = manifest.get("selected_pairs")
    if not isinstance(selected_pairs, list) or len(selected_pairs) != pair_count:
        raise ValueError("opening attempt selections are incomplete")
    case_ids: set[str] = set()
    for logical_index, selection in enumerate(selected_pairs):
        if (
            not isinstance(selection, Mapping)
            or set(selection)
            != {"logical_pair_index", "attempt_index", "opening_case_id"}
            or selection.get("logical_pair_index") != logical_index
            or selection.get("attempt_index") != completed_at.get(logical_index)
            or not isinstance(selection.get("opening_case_id"), str)
            or not selection["opening_case_id"]
            or selection["opening_case_id"] in case_ids
        ):
            raise ValueError("opening attempt selection is not first-complete")
        case_ids.add(str(selection["opening_case_id"]))
    return case_ids


def bind_frozen_match_report(
    report: Mapping[str, Any],
    *,
    match_spec: Mapping[str, Any],
    protocol_digest: str,
    tournament_plan_digest: str,
    effective_by_profile_id: Mapping[str, str],
    config: TournamentFunnelConfig | None = None,
) -> dict[str, Any]:
    """Replay-verifies and content-binds one raw strength match to its spec."""

    selected = config or TournamentFunnelConfig()
    if match_spec.get("job_identity_parent") != protocol_digest:
        raise ValueError("match spec belongs to a different protocol")
    expected_effective_ids = set(_resolved_spec_effective_ids(match_spec))
    candidate = report.get("candidate")
    reference = report.get("reference")
    if not isinstance(candidate, Mapping) or not isinstance(reference, Mapping):
        raise ValueError("match profiles are missing")
    candidate_profile_id = str(candidate.get("profile_id", ""))
    reference_profile_id = str(reference.get("profile_id", ""))
    participant_effective_ids = {
        candidate_profile_id: effective_by_profile_id.get(candidate_profile_id),
        reference_profile_id: effective_by_profile_id.get(reference_profile_id),
    }
    actual_effective_ids = {
        participant_effective_ids[candidate_profile_id],
        participant_effective_ids[reference_profile_id],
    }
    if actual_effective_ids != expected_effective_ids or None in actual_effective_ids:
        raise ValueError("match report profiles do not match resolved spec participants")
    report_config = report.get("config")
    if not isinstance(report_config, Mapping):
        raise ValueError("match config is missing")
    pair_count = int(report_config.get("pairs", -1))
    if pair_count not in {
        int(match_spec["base_pairs"]),
        int(match_spec["maximum_pairs"]),
    }:
        raise ValueError("match pair count is outside its frozen base/maximum")
    if report_config.get("seed") != match_spec.get("match_seed"):
        raise ValueError("match config seed does not match frozen spec")
    expected_report_limits = strength_report_limits(match_spec["limits"])
    if report_config.get("deterministic_limits") != expected_report_limits:
        raise ValueError("match config search limits do not match frozen spec")
    suite = report.get("opening_suite")
    if (
        not isinstance(suite, Mapping)
        or suite.get("seed") != match_spec.get("opening_seed")
        or int(suite.get("count", -1)) < pair_count
        or report_config.get("opening_suite_version") != suite.get("version")
    ):
        raise ValueError("opening suite identity does not match frozen spec")
    canonical_suite = seeded_opening_suite_from_dict(suite)
    actual_suite_digest = canonical_digest(
        "spc-tournament-opening-suite-v1\0", canonical_suite.as_dict()
    )
    if match_spec.get("opening_suite_digest") != actual_suite_digest:
        raise ValueError("opening suite digest does not match frozen spec")
    attempt_case_ids = _validate_opening_attempt_manifest(
        report.get("opening_attempts"),
        match_spec=match_spec,
        tournament_plan_digest=tournament_plan_digest,
        pair_count=pair_count,
    )
    enriched = attach_replay_verified_full_traces(report)
    games = enriched["games"]
    pairs = enriched["pairs"]
    if len(pairs) != pair_count or len(games) != pair_count * 2:
        raise ValueError("frozen match does not contain its exact pair/game count")
    seen_case_ids: set[str] = set()
    seen_boundaries: set[str] = set()
    seen_job_keys: set[str] = set()
    for pair_index in range(pair_count):
        pair = pairs[pair_index]
        paired_games = games[pair_index * 2 : pair_index * 2 + 2]
        if pair.get("pair_index") != pair_index:
            raise ValueError("pair indexes are not canonical and contiguous")
        case_ids = {str(game.get("opening_case_id", "")) for game in paired_games}
        if len(case_ids) != 1:
            raise ValueError("color swap does not share exactly one opening")
        case_id = next(iter(case_ids))
        if (
            not case_id
            or case_id in seen_case_ids
            or pair.get("opening_case_id") != case_id
        ):
            raise ValueError("match reuses or mislabels an opening case")
        seen_case_ids.add(case_id)
        start_pfens = {str(game.get("start_pfen", "")) for game in paired_games}
        if len(start_pfens) != 1 or not next(iter(start_pfens)):
            raise ValueError("color swap does not share one opening boundary")
        opening_boundary_digest = hashlib.sha256(
            next(iter(start_pfens)).encode("utf-8")
        ).hexdigest()
        if opening_boundary_digest in seen_boundaries:
            raise ValueError("match reuses an opening boundary")
        seen_boundaries.add(opening_boundary_digest)
        pair_seed = match_pair_seed(
            str(match_spec["stage"]),
            str(match_spec["matchup_id"]),
            pair_index,
            case_id,
            config=selected,
        )
        tournament_keys: list[str] = []
        for game in paired_games:
            white_profile_id = str(game["white_profile_id"])
            black_profile_id = str(game["black_profile_id"])
            white_effective_id = effective_by_profile_id.get(white_profile_id)
            black_effective_id = effective_by_profile_id.get(black_profile_id)
            if {
                white_effective_id,
                black_effective_id,
            } != expected_effective_ids:
                raise ValueError("game effective/color identity is invalid")
            job_key = tournament_job_key(
                protocol_digest=protocol_digest,
                stage=str(match_spec["stage"]),
                matchup_id=str(match_spec["matchup_id"]),
                opening_boundary_digest=opening_boundary_digest,
                pair_index=pair_index,
                pair_seed=pair_seed,
                white_effective_id=str(white_effective_id),
                black_effective_id=str(black_effective_id),
            )
            if job_key in seen_job_keys:
                raise ValueError("tournament game job key is duplicated")
            seen_job_keys.add(job_key)
            tournament_keys.append(job_key)
            game["tournament_identity"] = {
                "job_key": job_key,
                "pair_index": pair_index,
                "pair_seed": pair_seed,
                "opening_boundary_digest": opening_boundary_digest,
                "white_effective_id": white_effective_id,
                "black_effective_id": black_effective_id,
            }
        pair["tournament_identity"] = {
            "pair_index": pair_index,
            "pair_seed": pair_seed,
            "opening_boundary_digest": opening_boundary_digest,
            "game_job_keys": sorted(tournament_keys),
        }
    if seen_case_ids != attempt_case_ids:
        raise ValueError("selected match openings differ from attempt evidence")
    # This recomputes WDL and confirms every pair against the raw games.
    summarize_match_report(enriched, expected_pairs=pair_count)
    binding = {
        "format": "spc-frozen-match-binding-v1",
        "protocol_digest": protocol_digest,
        "tournament_plan_digest": tournament_plan_digest,
        "match_spec_digest": canonical_digest(
            "spc-resolved-match-spec-v1\0", match_spec
        ),
        "stage": match_spec["stage"],
        "matchup_id": match_spec["matchup_id"],
        "pair_count": pair_count,
        "opening_reserve_digest": match_spec["opening_reserve_digest"],
        "opening_attempt_manifest_digest": match_spec[
            "opening_attempt_manifest_digest"
        ],
        "effective_by_profile_id": dict(
            sorted(participant_effective_ids.items())
        ),
        "opening_case_ids": sorted(seen_case_ids),
        "opening_boundary_digests": sorted(seen_boundaries),
        "tournament_game_job_keys": sorted(seen_job_keys),
    }
    enriched["tournament_binding"] = binding
    enriched["bound_report_digest"] = canonical_digest(
        "spc-bound-tournament-report-v1\0", enriched
    )
    return enriched


def validate_frozen_match_report(
    report: Mapping[str, Any],
    *,
    match_spec: Mapping[str, Any],
    protocol_digest: str,
    tournament_plan_digest: str,
    effective_by_profile_id: Mapping[str, str],
) -> dict[str, Any]:
    """Validates a bound report by rebuilding the entire binding from raw data.

    The serialized digest is an integrity check, not an authentication token.
    Rebinding is what proves that the report still matches its frozen schedule,
    color swaps, opening boundaries, replayed full traces, and game job keys.
    """

    supplied_digest = report.get("bound_report_digest")
    deterministic = {
        key: value for key, value in report.items() if key != "bound_report_digest"
    }
    if supplied_digest != canonical_digest(
        "spc-bound-tournament-report-v1\0", deterministic
    ):
        raise ValueError("bound tournament report digest mismatch")
    raw = copy.deepcopy(dict(report))
    raw.pop("bound_report_digest", None)
    raw.pop("tournament_binding", None)
    raw.pop("full_trace_evidence", None)
    games = raw.get("games")
    pairs = raw.get("pairs")
    if not isinstance(games, list) or not isinstance(pairs, list):
        raise ValueError("bound tournament report games/pairs are missing")
    for game in games:
        if not isinstance(game, dict):
            raise ValueError("bound tournament game is not an object")
        game.pop("full_trace", None)
        game.pop("tournament_identity", None)
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("bound tournament pair is not an object")
        pair.pop("tournament_identity", None)
    rebound = bind_frozen_match_report(
        raw,
        match_spec=match_spec,
        protocol_digest=protocol_digest,
        tournament_plan_digest=tournament_plan_digest,
        effective_by_profile_id=effective_by_profile_id,
    )
    if rebound != dict(report):
        raise ValueError("bound tournament report does not match authoritative rebind")
    return summarize_match_report(
        report, expected_pairs=int(report["tournament_binding"]["pair_count"])
    )


def hoeffding_pair_interval(
    wins: int,
    draws: int,
    losses: int,
    *,
    alpha: float = 0.05,
) -> dict[str, Any]:
    if min(wins, draws, losses) < 0:
        raise ValueError("WDL counts cannot be negative")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between zero and one")
    count = wins + draws + losses
    if not count:
        return {
            "method": UNCERTAINTY_METHOD,
            "completed_pairs": 0,
            "score_rate": None,
            "lower": None,
            "upper": None,
            "conditional_on_completed_pairs": True,
        }
    rate = (wins + draws * 0.5) / count
    epsilon = math.sqrt(math.log(2 / alpha) / (2 * count))
    return {
        "method": UNCERTAINTY_METHOD,
        "completed_pairs": count,
        "score_rate": rate,
        "lower": max(0.0, rate - epsilon),
        "upper": min(1.0, rate + epsilon),
        "conditional_on_completed_pairs": True,
    }


def _game_points(game: Mapping[str, Any], profile_id: str) -> float | None:
    result = game.get("result")
    if result == "1/2-1/2":
        return 0.5
    if result == "1-0":
        return 1.0 if game.get("white_profile_id") == profile_id else 0.0
    if result == "0-1":
        return 1.0 if game.get("black_profile_id") == profile_id else 0.0
    if result == "*":
        return None
    raise ValueError(f"unknown game result {result!r}")


def summarize_match_report(
    report: Mapping[str, Any], *, expected_pairs: int | None = None
) -> dict[str, Any]:
    if report.get("format") != STRENGTH_REPORT_FORMAT:
        raise ValueError("unsupported strength report format")
    engine = report.get("engine")
    if (
        not isinstance(engine, Mapping)
        or engine.get("source_fingerprint") != ENGINE_SOURCE_FINGERPRINT
    ):
        raise ValueError("strength report source fingerprint is stale")
    candidate = report.get("candidate")
    reference = report.get("reference")
    if not isinstance(candidate, Mapping) or not isinstance(reference, Mapping):
        raise ValueError("strength report profiles are missing")
    candidate_id = candidate.get("profile_id")
    reference_id = reference.get("profile_id")
    if (
        not isinstance(candidate_id, str)
        or not isinstance(reference_id, str)
        or candidate_id == reference_id
    ):
        raise ValueError("strength report profile ids are invalid")
    config = report.get("config")
    if not isinstance(config, Mapping):
        raise ValueError("strength report config is missing")
    pair_count = int(config.get("pairs", -1))
    if expected_pairs is not None and pair_count != expected_pairs:
        raise ValueError(
            "strength report pair count does not match its tournament stage"
        )
    if (
        pair_count not in {50, 100, 200}
        or config.get("games") != pair_count * 2
    ):
        raise ValueError("tournament match must contain 50, 100, or 200 pairs")
    limits = config.get("deterministic_limits")
    accepted_limits = {
        tuple(sorted(strength_report_limits(tournament_limits_for_stage(stage)).items()))
        for stage in ("group", "round-of-16")
    }
    if not isinstance(limits, Mapping) or tuple(sorted(limits.items())) not in accepted_limits:
        raise ValueError("tournament match limits are not frozen")
    games = report.get("games")
    pairs = report.get("pairs")
    if (
        not isinstance(games, Sequence)
        or isinstance(games, (str, bytes))
        or len(games) != pair_count * 2
    ):
        raise ValueError("strength report has the wrong game record count")
    if (
        not isinstance(pairs, Sequence)
        or isinstance(pairs, (str, bytes))
        or len(pairs) != pair_count
    ):
        raise ValueError("strength report has the wrong pair record count")
    summaries = {
        profile_id: {
            "profile_id": profile_id,
            "game_wdl": {"wins": 0, "draws": 0, "losses": 0},
            "color_wdl": {
                "white": {"wins": 0, "draws": 0, "losses": 0},
                "black": {"wins": 0, "draws": 0, "losses": 0},
            },
            "completed_games": 0,
            "incomplete_games": 0,
            "game_points": 0.0,
            "pair_wdl": {"wins": 0, "draws": 0, "losses": 0},
            "pair_score_quarter_units": [],
            "completed_pairs": 0,
            "incomplete_pairs": 0,
            "pair_points": 0.0,
            "attributed_technical_failures": 0,
        }
        for profile_id in (candidate_id, reference_id)
    }
    for game in games:
        if not isinstance(game, Mapping) or {
            game.get("white_profile_id"),
            game.get("black_profile_id"),
        } != {candidate_id, reference_id}:
            raise ValueError(
                "game profile/color assignment does not match report"
            )
        for profile_id, item in summaries.items():
            points = _game_points(game, profile_id)
            if points is None:
                item["incomplete_games"] += 1
                continue
            item["completed_games"] += 1
            item["game_points"] += points
            outcome = (
                "wins" if points == 1 else "draws" if points == 0.5 else "losses"
            )
            item["game_wdl"][outcome] += 1
            color = (
                "white"
                if game.get("white_profile_id") == profile_id
                else "black"
            )
            item["color_wdl"][color][outcome] += 1
        failed = game.get("engine_failure_profile_id")
        if failed in summaries:
            summaries[failed]["attributed_technical_failures"] += 1
    for pair_index, pair in enumerate(pairs):
        if not isinstance(pair, Mapping):
            raise ValueError("pair record must be an object")
        paired_games = games[pair_index * 2 : pair_index * 2 + 2]
        if {
            game.get("white_profile_id") for game in paired_games
        } != {candidate_id, reference_id}:
            raise ValueError("pair is not an exact color swap")
        case_ids = {
            game.get("opening_case_id")
            for game in paired_games
            if game.get("opening_case_id") is not None
        }
        if len(case_ids) > 1:
            raise ValueError("color-swapped pair uses different openings")
        game_points = [
            _game_points(game, candidate_id) for game in paired_games
        ]
        recomputed_points = (
            None
            if any(value is None for value in game_points)
            else sum(value for value in game_points if value is not None)
        )
        result = pair.get("result")
        points = pair.get("candidate_points")
        if result == "incomplete":
            if points is not None or recomputed_points is not None:
                raise ValueError(
                    "incomplete pair cannot carry points or two completed games"
                )
            for item in summaries.values():
                item["incomplete_pairs"] += 1
            continue
        if result not in {"win", "draw", "loss"} or points is None:
            raise ValueError("pair record result is invalid")
        if recomputed_points is None or float(points) != recomputed_points:
            raise ValueError("pair points do not match the raw color-swapped games")
        doubled = float(points) * 2
        if not doubled.is_integer() or not 0 <= doubled <= 4:
            raise ValueError(
                "candidate pair points are not legal quarter-score units"
            )
        candidate_units = int(doubled)
        expected_result = (
            "win"
            if candidate_units > 2
            else "draw"
            if candidate_units == 2
            else "loss"
        )
        if result != expected_result:
            raise ValueError("pair result and candidate points disagree")
        for profile_id, units in (
            (candidate_id, candidate_units),
            (reference_id, 4 - candidate_units),
        ):
            item = summaries[profile_id]
            outcome = (
                "wins" if units > 2 else "draws" if units == 2 else "losses"
            )
            item["pair_wdl"][outcome] += 1
            item["pair_score_quarter_units"].append(units)
            item["completed_pairs"] += 1
            item["pair_points"] += units / 4
    for item in summaries.values():
        wdl = item["pair_wdl"]
        item["pair_uncertainty"] = hoeffding_pair_interval(
            wdl["wins"], wdl["draws"], wdl["losses"]
        )
        item["standing_incomplete_policy"] = (
            "incomplete pairs receive zero standing points but never enter WDL"
        )
    return {
        "candidate_profile_id": candidate_id,
        "reference_profile_id": reference_id,
        "scheduled_pairs": pair_count,
        "profiles": summaries,
        "promotion_effect": "none",
    }


def rank_group(
    group_id: str,
    profile_ids: Sequence[str],
    reports: Sequence[Mapping[str, Any]],
    *,
    protocol_digest: str,
    tournament_plan_digest: str,
    match_specs: Sequence[Mapping[str, Any]],
    effective_by_profile_id: Mapping[str, str],
) -> dict[str, Any]:
    ids = tuple(profile_ids)
    if len(ids) != 8 or len(set(ids)) != 8:
        raise ValueError("a group must contain exactly 8 unique profile ids")
    if (
        set(effective_by_profile_id) != set(ids)
        or len(set(effective_by_profile_id.values())) != 8
        or any(
            not isinstance(value, str) or not value.startswith("spc-effective-")
            for value in effective_by_profile_id.values()
        )
    ):
        raise ValueError("group profile-to-effective identities are incomplete")
    expected = {
        frozenset((first, second))
        for index, first in enumerate(ids)
        for second in ids[index + 1 :]
    }
    profile_by_effective_id = {
        effective_id: profile_id
        for profile_id, effective_id in effective_by_profile_id.items()
    }
    specs_by_profiles: dict[frozenset[str], Mapping[str, Any]] = {}
    for spec in match_specs:
        if (
            not isinstance(spec, Mapping)
            or spec.get("stage") != "group"
            or spec.get("job_identity_parent") != protocol_digest
        ):
            raise ValueError("group match spec identity is invalid")
        first_effective_id, second_effective_id = _resolved_spec_effective_ids(
            spec
        )
        try:
            profile_key = frozenset(
                (
                    profile_by_effective_id[first_effective_id],
                    profile_by_effective_id[second_effective_id],
                )
            )
        except KeyError as error:
            raise ValueError("group match spec contains a foreign participant") from error
        if profile_key not in expected or profile_key in specs_by_profiles:
            raise ValueError("group match specs are duplicate or incomplete")
        specs_by_profiles[profile_key] = spec
    if set(specs_by_profiles) != expected:
        raise ValueError("group ranking requires all 28 frozen match specs")
    matches: dict[frozenset[str], dict[str, Any]] = {}
    aggregate = {
        profile_id: {
            "profile_id": profile_id,
            "effective_id": effective_by_profile_id[profile_id],
            "match_points": 0,
            "paired_score": 0.0,
            "completed_pairs": 0,
            "incomplete_pairs": 0,
            "pair_wdl": {"wins": 0, "draws": 0, "losses": 0},
            "game_wdl": {"wins": 0, "draws": 0, "losses": 0},
            "color_wdl": {
                "white": {"wins": 0, "draws": 0, "losses": 0},
                "black": {"wins": 0, "draws": 0, "losses": 0},
            },
            "attributed_technical_failures": 0,
        }
        for profile_id in ids
    }
    for report in reports:
        candidate = report.get("candidate")
        reference = report.get("reference")
        if not isinstance(candidate, Mapping) or not isinstance(reference, Mapping):
            raise ValueError("group report profiles are missing")
        report_key = frozenset(
            (
                str(candidate.get("profile_id", "")),
                str(reference.get("profile_id", "")),
            )
        )
        spec = specs_by_profiles.get(report_key)
        if spec is None:
            raise ValueError("group contains a foreign report")
        summary = validate_frozen_match_report(
            report,
            match_spec=spec,
            protocol_digest=protocol_digest,
            tournament_plan_digest=tournament_plan_digest,
            effective_by_profile_id=effective_by_profile_id,
        )
        key = frozenset(
            (summary["candidate_profile_id"], summary["reference_profile_id"])
        )
        if key not in expected or key in matches:
            raise ValueError("group contains a duplicate or foreign report")
        matches[key] = summary
        first, second = tuple(key)
        first_evidence = summary["profiles"][first]
        second_evidence = summary["profiles"][second]
        if any(
            evidence["completed_pairs"] != summary["scheduled_pairs"]
            or evidence["incomplete_pairs"] != 0
            for evidence in (first_evidence, second_evidence)
        ):
            raise ValueError(
                "group standings require every frozen color-swapped pair to complete"
            )
        if first_evidence["pair_points"] > second_evidence["pair_points"]:
            aggregate[first]["match_points"] += 2
        elif first_evidence["pair_points"] < second_evidence["pair_points"]:
            aggregate[second]["match_points"] += 2
        else:
            aggregate[first]["match_points"] += 1
            aggregate[second]["match_points"] += 1
        for profile_id in key:
            source = summary["profiles"][profile_id]
            target = aggregate[profile_id]
            target["paired_score"] += source["pair_points"]
            target["completed_pairs"] += source["completed_pairs"]
            target["incomplete_pairs"] += source["incomplete_pairs"]
            target["attributed_technical_failures"] += source[
                "attributed_technical_failures"
            ]
            for result in ("wins", "draws", "losses"):
                target["pair_wdl"][result] += source["pair_wdl"][result]
                target["game_wdl"][result] += source["game_wdl"][result]
                for color in ("white", "black"):
                    target["color_wdl"][color][result] += source[
                        "color_wdl"
                    ][color][result]
    if set(matches) != expected:
        raise ValueError(f"group needs all 28 reports; received {len(matches)}")
    primary_buckets: dict[int, list[str]] = {}
    for profile_id, row in aggregate.items():
        primary_buckets.setdefault(row["match_points"], []).append(profile_id)
    for bucket in primary_buckets.values():
        for profile_id in bucket:
            aggregate[profile_id]["head_to_head_match_points"] = 0
        for index, first in enumerate(bucket):
            for second in bucket[index + 1 :]:
                summary = matches[frozenset((first, second))]
                first_points = summary["profiles"][first]["pair_points"]
                second_points = summary["profiles"][second]["pair_points"]
                completed = summary["profiles"][first]["completed_pairs"]
                if completed == 0:
                    continue
                if first_points > second_points:
                    aggregate[first]["head_to_head_match_points"] += 2
                elif second_points > first_points:
                    aggregate[second]["head_to_head_match_points"] += 2
                else:
                    aggregate[first]["head_to_head_match_points"] += 1
                    aggregate[second]["head_to_head_match_points"] += 1
    for profile_id, row in aggregate.items():
        sonneborn = 0.0
        for opponent in ids:
            if opponent == profile_id:
                continue
            summary = matches[frozenset((profile_id, opponent))]
            own = summary["profiles"][profile_id]["pair_points"]
            other = summary["profiles"][opponent]["pair_points"]
            completed = summary["profiles"][profile_id]["completed_pairs"]
            match_score = (
                0.0
                if completed == 0
                else 1.0
                if own > other
                else 0.5
                if own == other
                else 0.0
            )
            sonneborn += match_score * aggregate[opponent]["match_points"]
        row["sonneborn_berger"] = sonneborn
        wdl = row["pair_wdl"]
        row["pair_uncertainty"] = hoeffding_pair_interval(
            wdl["wins"], wdl["draws"], wdl["losses"]
        )
        row["tie_hash"] = hashlib.sha256(
            f"{protocol_digest}|group-tie|{group_id}|{row['effective_id']}".encode(
                "ascii"
            )
        ).hexdigest()
    ranking = sorted(
        aggregate.values(),
        key=lambda row: (
            -row["match_points"],
            -row["paired_score"],
            row["incomplete_pairs"],
            -row["head_to_head_match_points"],
            -row["sonneborn_berger"],
            row["tie_hash"],
        ),
    )
    for index, row in enumerate(ranking, 1):
        row["rank"] = index
    return {
        "group_id": group_id,
        "ranking": ranking,
        "advancing_profile_ids": [
            ranking[0]["profile_id"],
            ranking[1]["profile_id"],
        ],
        "advancing_effective_ids": [
            ranking[0]["effective_id"],
            ranking[1]["effective_id"],
        ],
        "scheduled_matchups": 28,
        "scheduled_games": 2_800,
        "promotion_effect": "none",
    }


def select_knockout_winner(
    report: Mapping[str, Any], *, preregistered_seed_order: Sequence[str]
) -> dict[str, Any]:
    summary = summarize_match_report(report)
    first_id = summary["candidate_profile_id"]
    second_id = summary["reference_profile_id"]
    if (
        set(preregistered_seed_order) != {first_id, second_id}
        or len(preregistered_seed_order) != 2
    ):
        raise ValueError("pre-round seed order does not match the profiles")
    first = summary["profiles"][first_id]
    second = summary["profiles"][second_id]
    if first["pair_points"] > second["pair_points"]:
        winner, status = first_id, "match-win"
    elif second["pair_points"] > first["pair_points"]:
        winner, status = second_id, "match-win"
    else:
        winner = preregistered_seed_order[0]
        status = "administrative-tie-advance"
    return {
        "winner_profile_id": winner,
        "status": status,
        "profile_evidence": summary["profiles"],
        "promotion_effect": "none",
    }


def derive_verified_knockout_winner(
    report: Mapping[str, Any],
    *,
    match_spec: Mapping[str, Any],
    protocol_digest: str,
    tournament_plan_digest: str,
    effective_by_profile_id: Mapping[str, str],
    preregistered_seed_order: Sequence[str],
) -> dict[str, Any]:
    """Derives a knockout winner only from a replay-verified frozen report."""

    summary = validate_frozen_match_report(
        report,
        match_spec=match_spec,
        protocol_digest=protocol_digest,
        tournament_plan_digest=tournament_plan_digest,
        effective_by_profile_id=effective_by_profile_id,
    )
    expected_pairs = int(report["tournament_binding"]["pair_count"])
    if any(
        evidence["completed_pairs"] != expected_pairs
        or evidence["incomplete_pairs"] != 0
        or evidence["attributed_technical_failures"] != 0
        for evidence in summary["profiles"].values()
    ):
        raise ValueError("knockout winner requires every frozen pair to complete")
    selection = select_knockout_winner(
        report, preregistered_seed_order=preregistered_seed_order
    )
    winner_profile_id = selection["winner_profile_id"]
    winner_effective_id = effective_by_profile_id.get(winner_profile_id)
    if winner_effective_id is None:
        raise ValueError("knockout winner has no effective-profile identity")
    deterministic = {
        "format": "spc-verified-knockout-winner-v1",
        "protocol_digest": protocol_digest,
        "tournament_plan_digest": tournament_plan_digest,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "stage": match_spec.get("stage"),
        "matchup_id": match_spec.get("matchup_id"),
        "match_spec_digest": canonical_digest(
            "spc-resolved-match-spec-v1\0", match_spec
        ),
        "bound_report_digest": report.get("bound_report_digest"),
        "winner_profile_id": winner_profile_id,
        "winner_effective_id": winner_effective_id,
        "selection_status": selection["status"],
        "completed_pairs": summary["profiles"][winner_profile_id][
            "completed_pairs"
        ],
    }
    return {
        **deterministic,
        "artifact_digest": canonical_digest(
            "spc-verified-knockout-winner-v1\0", deterministic
        ),
    }


def exact_sign_flip_p_value(
    score_quarter_units: Sequence[int],
) -> dict[str, Any]:
    if not score_quarter_units or any(
        unit not in {0, 1, 2, 3, 4} for unit in score_quarter_units
    ):
        raise ValueError("pair scores must be quarter-units in 0..4")
    differences = [unit - 2 for unit in score_quarter_units]
    observed = sum(differences)
    distribution: Counter[int] = Counter({0: 1})
    for difference in differences:
        magnitude = abs(difference)
        updated: Counter[int] = Counter()
        for total, count in distribution.items():
            updated[total + magnitude] += count
            updated[total - magnitude] += count
        distribution = updated
    numerator = sum(
        count for total, count in distribution.items() if total >= observed
    )
    denominator = 1 << len(differences)
    with localcontext() as context:
        context.prec = 80
        decimal_value = Decimal(numerator) / Decimal(denominator)
    return {
        "method": "exact-one-sided-paired-sign-flip-quarter-units-v1",
        "observed_difference_quarter_units": observed,
        "numerator": str(numerator),
        "denominator": str(denominator),
        "decimal": format(decimal_value, "f"),
    }


def _validate_promotion_execution_binding(
    report: Mapping[str, Any],
    match_spec: Mapping[str, Any],
    *,
    native_source_identity: str,
    runtime_identity_digest: str,
) -> None:
    required_spec = {
        "environment_digest",
        "runtime_identity_digest",
        "profile_catalog_digest",
        "opening_reserve_digest",
        "opening_suite_digest",
        "opening_attempt_manifest_digest",
        "corpus_exclusion_digest",
        "corpus_exclusion_authority",
        "promotion_batch_index",
        "promotion_batch_chain_digest",
        "promotion_registry_id",
        "promotion_registry_authority",
        "promotion_batch_artifact",
        "promotion_batch_artifact_digest",
        "expansion_decision_digest",
    }
    if not required_spec <= set(match_spec):
        raise ValueError("promotion match spec lacks runner evidence bindings")
    execution = report.get("tournament_execution")
    required_execution = {
        "format",
        "environment",
        "environment_digest",
        "profile_catalog_digest",
        "opening_reserve_digest",
        "opening_suite_digest",
        "opening_attempt_manifest_digest",
        "corpus_exclusion_digest",
        "corpus_exclusion_authority",
        "promotion_batch_index",
        "promotion_batch_chain_digest",
        "promotion_registry_id",
        "promotion_registry_authority",
        "promotion_batch_artifact_digest",
        "expansion_decision_digest",
        "requested_workers",
        "authoritative_timing_fields",
        "promotion_effect",
    }
    if not isinstance(execution, Mapping) or set(execution) != required_execution:
        raise ValueError("promotion report lacks canonical runner execution evidence")
    environment = execution.get("environment")
    if not isinstance(environment, Mapping) or execution.get(
        "environment_digest"
    ) != canonical_digest("spc-tournament-environment-v1\0", environment):
        raise ValueError("promotion report environment digest is invalid")
    if (
        environment.get("native_source_identity") != native_source_identity
        or environment.get("loaded_native_source_identity")
        != native_source_identity
        or environment.get("runtime_identity_digest")
        != runtime_identity_digest
        or match_spec.get("runtime_identity_digest")
        != runtime_identity_digest
    ):
        raise ValueError("promotion match and tactical gate environments differ")
    exact = {
        "environment_digest": execution["environment_digest"],
        "profile_catalog_digest": execution["profile_catalog_digest"],
        "opening_reserve_digest": execution["opening_reserve_digest"],
        "opening_suite_digest": execution["opening_suite_digest"],
        "opening_attempt_manifest_digest": execution[
            "opening_attempt_manifest_digest"
        ],
        "corpus_exclusion_digest": execution["corpus_exclusion_digest"],
        "corpus_exclusion_authority": execution[
            "corpus_exclusion_authority"
        ],
        "promotion_batch_index": execution["promotion_batch_index"],
        "promotion_batch_chain_digest": execution[
            "promotion_batch_chain_digest"
        ],
        "promotion_registry_id": execution["promotion_registry_id"],
        "promotion_registry_authority": execution[
            "promotion_registry_authority"
        ],
        "promotion_batch_artifact_digest": execution[
            "promotion_batch_artifact_digest"
        ],
        "expansion_decision_digest": execution["expansion_decision_digest"],
    }
    if any(match_spec[key] != value for key, value in exact.items()):
        raise ValueError("promotion report runner evidence does not match its spec")
    if (
        execution.get("format") != "spc-tournament-match-execution-v2"
        or execution.get("requested_workers") != 16
        or execution.get("authoritative_timing_fields")
        != ["opening_attempts.attempts[].execution_elapsed_seconds"]
        or execution.get("promotion_effect") != "none"
    ):
        raise ValueError("promotion report execution contract changed")


def _plan_match_spec(
    plan: Mapping[str, Any], stage: str, matchup_id: str
) -> Mapping[str, Any]:
    matchups = plan.get("matchups")
    if not isinstance(matchups, Mapping):
        raise ValueError("trusted tournament plan lacks its match table")
    stage_key = {
        "group": "group",
        "round-of-16": "round_of_16",
        "quarterfinal": "quarterfinal",
        "semifinal": "semifinal",
        "challenger-final": "challenger_final",
        "baseline-final": "baseline_final",
    }.get(stage)
    if stage_key is None:
        raise ValueError("trusted tournament evidence names an unknown stage")
    candidates = matchups.get(stage_key)
    if not isinstance(candidates, list):
        raise ValueError("trusted tournament plan stage is missing")
    found = [
        item
        for item in candidates
        if isinstance(item, Mapping) and item.get("matchup_id") == matchup_id
    ]
    if len(found) != 1:
        raise ValueError("trusted tournament matchup is not uniquely planned")
    return found[0]


def _plan_opening_reserve(
    plan: Mapping[str, Any], domain: str
) -> Mapping[str, Any]:
    reserves = plan.get("opening_suites")
    if not isinstance(reserves, list):
        raise ValueError("trusted tournament plan lacks opening reserves")
    found = [
        item
        for item in reserves
        if isinstance(item, Mapping) and item.get("domain") == domain
    ]
    if len(found) != 1 or found[0].get("reserve_digest") != opening_reserve_digest(
        found[0]
    ):
        raise ValueError("trusted tournament opening reserve changed")
    return found[0]


def _database_opening_reserve(
    connection: sqlite3.Connection,
    *,
    plan: Mapping[str, Any],
    domain: str,
) -> Mapping[str, Any]:
    row = connection.execute(
        "select suite_json,suite_digest,position_hash_digest,case_count "
        "from opening_suites where domain=?",
        (domain,),
    ).fetchone()
    if row is None:
        raise ValueError("trusted tournament opening reserve is missing")
    reserve = json.loads(str(row["suite_json"]))
    if (
        row["suite_json"] != canonical_json_bytes(reserve).decode("ascii")
        or reserve.get("reserve_digest") != opening_reserve_digest(reserve)
        or row["suite_digest"] != reserve.get("reserve_digest")
        or row["position_hash_digest"] != reserve.get("position_hash_digest")
        or int(row["case_count"]) != int(reserve.get("total_case_count", -1))
    ):
        raise ValueError("trusted tournament opening reserve row changed")
    planned = [
        item
        for item in plan.get("opening_suites", ())
        if isinstance(item, Mapping) and item.get("domain") == domain
    ]
    if len(planned) != 1:
        raise ValueError("trusted tournament opening reserve is not planned")
    if planned[0].get("reserve_digest") is not None and (
        canonical_json_bytes(planned[0]) != canonical_json_bytes(reserve)
    ):
        raise ValueError("persisted opening reserve differs from frozen plan")
    return reserve


def _validate_trusted_attempt_rows(
    connection: sqlite3.Connection,
    *,
    plan: Mapping[str, Any],
    match_spec: Mapping[str, Any],
    report: Mapping[str, Any],
) -> None:
    """Authenticates retry selection against raw rows and the frozen reserve."""

    manifest = report.get("opening_attempts")
    attempts = manifest.get("attempts") if isinstance(manifest, Mapping) else None
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("trusted tournament final lacks opening attempts")
    stage = str(match_spec["stage"])
    matchup_id = str(match_spec["matchup_id"])
    rows = list(
        connection.execute(
            "select * from match_attempts where stage=? and matchup_id=? "
            "order by attempt_index",
            (stage, matchup_id),
        )
    )
    if len(rows) != len(attempts):
        raise ValueError("trusted tournament attempt row count changed")
    frozen_spec = _plan_match_spec(plan, stage, matchup_id)
    if any(match_spec.get(key) != value for key, value in frozen_spec.items()):
        raise ValueError("resolved final differs from its frozen plan spec")
    reserve = _database_opening_reserve(
        connection,
        plan=plan,
        domain=str(frozen_spec["opening_domain"]),
    )
    if (
        match_spec.get("opening_reserve_digest") != reserve["reserve_digest"]
        or manifest.get("opening_reserve_digest") != reserve["reserve_digest"]
    ):
        raise ValueError("trusted final opening reserve digest changed")
    lanes = reserve.get("attempt_lanes")
    if not isinstance(lanes, list) or len(lanes) != TOTAL_OPENING_ATTEMPTS:
        raise ValueError("trusted final opening reserve lanes changed")
    unresolved = list(range(int(report["config"]["pairs"])))
    completed_at: dict[int, tuple[int, str]] = {}
    for attempt_index, (attempt, row) in enumerate(
        zip(attempts, rows, strict=True)
    ):
        if not isinstance(attempt, Mapping):
            raise ValueError("trusted tournament attempt manifest changed")
        lane_payload = lanes[attempt_index]
        if not isinstance(lane_payload, Mapping):
            raise ValueError("trusted tournament attempt lane changed")
        lane = seeded_opening_suite_from_dict(lane_payload["suite"])
        lane_digest = canonical_digest(
            "spc-tournament-opening-suite-v1\0", lane.as_dict()
        )
        expected_case_ids = [lane.cases[index].case_id for index in unresolved]
        expected_subset = subset_seeded_opening_suite(lane, expected_case_ids)
        expected_subset_json = canonical_json_bytes(
            expected_subset.as_dict()
        ).decode("ascii")
        unresolved_json = canonical_json_bytes(unresolved).decode("ascii")
        raw = json.loads(str(row["report_json"]))
        raw_digest = canonical_digest(
            "spc-tournament-match-attempt-v1\0", raw
        )
        config_payload = json.loads(str(row["config_json"]))
        if (
            int(row["ordinal"]) != int(match_spec["tournament_ordinal"])
            or int(row["attempt_index"]) != attempt_index
            or row["unresolved_pair_indexes_json"] != unresolved_json
            or row["unresolved_pair_indexes_digest"]
            != canonical_digest("spc-tournament-unresolved-pairs-v1\0", unresolved)
            or row["lane_suite_digest"] != lane_digest
            or lane_payload.get("suite_digest") != lane_digest
            or row["subset_suite_json"] != expected_subset_json
            or row["subset_suite_digest"]
            != canonical_digest(
                "spc-tournament-opening-suite-v1\0", expected_subset.as_dict()
            )
            or row["config_digest"]
            != canonical_digest("spc-strength-match-config-v1\0", config_payload)
            or row["report_json"] != canonical_json_bytes(raw).decode("ascii")
            or row["report_digest"] != raw_digest
            or raw.get("config") != config_payload
            or canonical_json_bytes(raw.get("opening_suite"))
            != canonical_json_bytes(expected_subset.as_dict())
            or attempt.get("attempt_index") != attempt_index
            or attempt.get("unresolved_pair_indexes_in") != unresolved
            or attempt.get("lane_suite_digest") != row["lane_suite_digest"]
            or attempt.get("subset_suite_digest") != row["subset_suite_digest"]
            or attempt.get("config_digest") != row["config_digest"]
            or attempt.get("attempt_report_digest") != row["report_digest"]
            or attempt.get("execution_elapsed_seconds")
            != float(row["execution_elapsed_seconds"])
        ):
            raise ValueError("trusted tournament attempt identity changed")
        # This includes incomplete games: an invalid trace may never decide
        # whether a logical pair advances to another opening lane.
        attach_replay_verified_full_traces(raw)
        pairs = raw.get("pairs")
        games = raw.get("games")
        if (
            not isinstance(pairs, list)
            or not isinstance(games, list)
            or len(pairs) != len(unresolved)
            or len(games) != len(unresolved) * 2
            or attempt.get("executed_game_records") != len(games)
        ):
            raise ValueError("trusted tournament attempt game count changed")
        completed: list[int] = []
        remaining: list[int] = []
        terminal_counts: Counter[str] = Counter()
        logical_by_case_id = dict(zip(expected_case_ids, unresolved, strict=True))
        seen_logical_indexes: set[int] = set()
        for local_index, pair in enumerate(pairs):
            if not isinstance(pair, Mapping):
                raise ValueError("trusted tournament attempt pair changed")
            paired_games = games[local_index * 2 : local_index * 2 + 2]
            expected_case_id = str(pair.get("opening_case_id", ""))
            logical_index = logical_by_case_id.get(expected_case_id)
            if (
                logical_index is None
                or logical_index in seen_logical_indexes
                or any(
                    not isinstance(game, Mapping)
                    or game.get("opening_case_id") != expected_case_id
                    or game.get("engine_failure_profile_id") is not None
                    or game.get("error") is not None
                    for game in paired_games
                )
            ):
                raise ValueError("trusted tournament attempt pair changed")
            seen_logical_indexes.add(logical_index)
            complete = pair.get("result") != "incomplete" and all(
                game.get("result") != "*" for game in paired_games
            )
            if complete:
                completed.append(logical_index)
                completed_at[logical_index] = (attempt_index, expected_case_id)
            else:
                remaining.append(logical_index)
                for game in paired_games:
                    if game.get("result") == "*":
                        reason = str(game.get("terminal_reason", ""))
                        if (
                            reason != "manual-adjudication-pending"
                            or game.get("decisive_profile_id") is not None
                        ):
                            raise ValueError(
                                "trusted tournament attempt retries a technical failure"
                            )
                        terminal_counts[reason] += 1
        completed.sort()
        remaining.sort()
        if seen_logical_indexes != set(unresolved):
            raise ValueError("trusted tournament attempt pair set changed")
        terminal_evidence = attempt.get("incomplete_terminal_evidence")
        if (
            attempt.get("completed_pair_indexes") != completed
            or attempt.get("unresolved_pair_indexes_out") != remaining
            or not isinstance(terminal_evidence, Mapping)
            or terminal_evidence.get("terminal_reason_counts")
            != dict(sorted(terminal_counts.items()))
            or any(
                terminal_evidence.get(field) != 0
                for field in (
                    "candidate_attributed_failures",
                    "reference_attributed_failures",
                    "unattributed_worker_failures",
                    "unattributed_match_limit_failures",
                    "error_records",
                )
            )
        ):
            raise ValueError("trusted tournament attempt transition changed")
        unresolved = remaining
    expected_selections = [
        {
            "logical_pair_index": logical_index,
            "attempt_index": completed_at[logical_index][0],
            "opening_case_id": completed_at[logical_index][1],
        }
        for logical_index in range(int(report["config"]["pairs"]))
    ]
    if unresolved or manifest.get("selected_pairs") != expected_selections:
        raise ValueError("trusted tournament selected opening history changed")


def _validate_trusted_tournament_database(
    authority: Mapping[str, Any],
    *,
    tournament_plan_digest: str,
    evidence: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> None:
    required = {
        "format",
        "batch_index",
        "database_path",
        "plan_digest",
        "environment_digest",
        "profile_catalog_digest",
        "corpus_exclusion_digest",
        "promotion_batch_chain_digest",
        "expansion_decision_digest",
        "runner_state_digest",
        "match_report_count",
        "match_attempt_count",
        "final_rows",
        "authority_digest",
    }
    deterministic = {
        key: value for key, value in authority.items() if key != "authority_digest"
    }
    if (
        set(authority) != required
        or authority.get("format") != TRUSTED_TOURNAMENT_AUTHORITY_FORMAT
        or authority.get("plan_digest") != tournament_plan_digest
        or authority.get("authority_digest")
        != canonical_digest(
            "spc-trusted-tournament-authority-v1\0", deterministic
        )
    ):
        raise ValueError("trusted tournament authority changed")
    database_path = Path(str(authority["database_path"])).expanduser().resolve()
    if str(database_path) != authority["database_path"] or not database_path.is_file():
        raise ValueError("trusted tournament database is unavailable")
    connection = sqlite3.connect(
        f"file:{database_path.as_posix()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("pragma query_only=ON")
        connection.execute("begin")
        identity = connection.execute(
            "select * from run_identity where singleton=1"
        ).fetchone()
        if identity is None:
            raise ValueError("trusted tournament run identity is missing")
        plan = json.loads(str(identity["plan_json"]))
        deterministic_plan = {
            key: value
            for key, value in plan.items()
            if key != "tournament_plan_digest"
        }
        if (
            identity["plan_json"] != canonical_json_bytes(plan).decode("ascii")
            or plan.get("tournament_plan_digest") != tournament_plan_digest
            or tournament_plan_digest
            != canonical_digest("spc-tournament-plan-v1\0", deterministic_plan)
        ):
            raise ValueError("trusted tournament plan content changed")
        final_rows: dict[str, sqlite3.Row] = {}
        final_summary = authority.get("final_rows")
        if not isinstance(final_summary, Mapping):
            raise ValueError("trusted tournament final table changed")
        for report, match_spec in evidence:
            stage = str(match_spec.get("stage", ""))
            matchup_id = str(match_spec.get("matchup_id", ""))
            row = connection.execute(
                "select * from match_reports where stage=? and matchup_id=?",
                (stage, matchup_id),
            ).fetchone()
            if row is None:
                raise ValueError("trusted tournament final report is missing")
            if (
                row["resolved_spec_json"]
                != canonical_json_bytes(match_spec).decode("ascii")
                or row["resolved_spec_digest"]
                != canonical_digest("spc-resolved-match-spec-v1\0", match_spec)
                or row["report_json"] != canonical_json_bytes(report).decode("ascii")
                or row["report_digest"] != report.get("bound_report_digest")
            ):
                raise ValueError("promotion evidence differs from trusted database")
            final_rows[stage] = row
            _validate_trusted_attempt_rows(
                connection, plan=plan, match_spec=match_spec, report=report
            )
        rebuilt = make_tournament_authority_artifact(
            connection,
            database_path=database_path,
            batch_index=int(authority["batch_index"]),
            final_rows=final_rows,
        )
        if rebuilt != dict(authority):
            raise ValueError("trusted tournament database state changed")
        connection.rollback()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _validate_trusted_promotion_batch(
    match_spec: Mapping[str, Any],
    *,
    protocol_digest: str,
    tournament_plan_digest: str,
    baseline_effective_id: str,
    candidate: RankedCandidate,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if match_spec.get("promotion_registry_authority") != (
        "trusted-project-registry-v1"
    ):
        raise ValueError("promotion evidence does not use the trusted registry")
    if match_spec.get("corpus_exclusion_authority") != "store-reverified-v1":
        raise ValueError("promotion evidence does not use a reverified corpus store")
    artifact = match_spec.get("promotion_batch_artifact")
    if not isinstance(artifact, Mapping):
        raise ValueError("promotion match spec lacks its batch artifact")
    validate_promotion_batch_artifact(
        artifact,
        protocol_digest=protocol_digest,
        baseline_effective_id=baseline_effective_id,
    )
    trusted_registry_id = canonical_digest(
        "spc-promotion-batch-registry-id-v1\0",
        {"resolved_path": str(TRUSTED_PROMOTION_REGISTRY_PATH.resolve()).casefold()},
    )
    if (
        artifact.get("registry_id") != trusted_registry_id
        or match_spec.get("promotion_registry_id") != trusted_registry_id
        or match_spec.get("promotion_batch_artifact_digest")
        != artifact.get("artifact_digest")
    ):
        raise ValueError("promotion batch is outside the trusted registry")
    if not TRUSTED_PROMOTION_REGISTRY_PATH.is_file():
        raise ValueError("trusted promotion registry is unavailable")
    connection = sqlite3.connect(TRUSTED_PROMOTION_REGISTRY_PATH)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "select * from promotion_batches where batch_index=?",
            (int(artifact["batch_index"]),),
        ).fetchone()
        expected_artifact_json = canonical_json_bytes(artifact).decode("ascii")
        if (
            row is None
            or row["artifact_json"] != expected_artifact_json
            or row["plan_digest"] != tournament_plan_digest
            or row["status"] != "complete"
        ):
            raise ValueError("promotion batch is not a complete trusted run")
        survivor = connection.execute(
            "select candidate_index,profile_id,plan_digest from "
            "challenger_survivors where effective_id=? and batch_index=?",
            (candidate.effective_id, int(artifact["batch_index"])),
        ).fetchone()
        if (
            survivor is None
            or survivor["candidate_index"] != candidate.candidate_index
            or survivor["profile_id"] != candidate.profile_id
            or survivor["plan_digest"] != tournament_plan_digest
        ):
            raise ValueError("promotion candidate is not a registered survivor")
        try:
            authority_row = connection.execute(
                "select authority_json,authority_digest,database_path,"
                "runner_state_digest from tournament_authorities "
                "where batch_index=?",
                (int(artifact["batch_index"]),),
            ).fetchone()
        except sqlite3.OperationalError as error:
            raise ValueError(
                "promotion batch lacks a sealed tournament authority"
            ) from error
        if authority_row is None:
            raise ValueError("promotion batch lacks a sealed tournament authority")
        authority = json.loads(str(authority_row["authority_json"]))
        if (
            authority_row["authority_json"]
            != canonical_json_bytes(authority).decode("ascii")
            or authority_row["authority_digest"]
            != authority.get("authority_digest")
            or authority_row["database_path"] != authority.get("database_path")
            or authority_row["runner_state_digest"]
            != authority.get("runner_state_digest")
            or authority.get("batch_index") != int(artifact["batch_index"])
            or authority.get("plan_digest") != tournament_plan_digest
            or authority.get("promotion_batch_chain_digest")
            != artifact.get("chain_digest")
        ):
            raise ValueError("sealed tournament authority changed")
    finally:
        connection.close()
    return copy.deepcopy(dict(artifact)), copy.deepcopy(dict(authority))


def _record_trusted_promotion_decision(
    batch_artifact: Mapping[str, Any], decision: Mapping[str, Any]
) -> str:
    deterministic = copy.deepcopy(dict(decision))
    digest = canonical_digest(
        "spc-trusted-promotion-decision-v1\0", deterministic
    )
    connection = sqlite3.connect(TRUSTED_PROMOTION_REGISTRY_PATH)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("begin immediate")
        batch = connection.execute(
            "select status,artifact_digest from promotion_batches "
            "where batch_index=?",
            (int(batch_artifact["batch_index"]),),
        ).fetchone()
        if (
            batch is None
            or batch["status"] != "complete"
            or batch["artifact_digest"] != batch_artifact["artifact_digest"]
        ):
            raise ValueError("promotion batch changed before decision sealing")
        authority = connection.execute(
            "select authority_digest,runner_state_digest from "
            "tournament_authorities where batch_index=?",
            (int(batch_artifact["batch_index"]),),
        ).fetchone()
        if (
            authority is None
            or authority["authority_digest"]
            != decision.get("tournament_authority_digest")
            or authority["runner_state_digest"]
            != decision.get("tournament_runner_state_digest")
        ):
            raise ValueError("tournament authority changed before decision sealing")
        existing = connection.execute(
            "select decision_json,decision_digest,promoted from "
            "promotion_decisions where batch_index=?",
            (int(batch_artifact["batch_index"]),),
        ).fetchone()
        encoded = canonical_json_bytes(deterministic).decode("ascii")
        expected = (encoded, digest, 1 if decision.get("promoted") else 0)
        if existing is None:
            connection.execute(
                "insert into promotion_decisions values(?,?,?,?)",
                (int(batch_artifact["batch_index"]), *expected),
            )
        elif tuple(existing) != expected:
            raise ValueError("promotion decision for this batch is already sealed")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return digest


def baseline_promotion_decision(
    report: Mapping[str, Any],
    depth2_tactical_bundle: Mapping[str, Any],
    depth3_human_gate_artifact: Mapping[str, Any],
    *,
    candidate: RankedCandidate,
    baseline_profile_id: str,
    baseline_effective_id: str,
    protocol_digest: str,
    tournament_plan_digest: str,
    match_spec: Mapping[str, Any],
    challenger_final_report: Mapping[str, Any],
    challenger_final_match_spec: Mapping[str, Any],
    challenger_final_effective_by_profile_id: Mapping[str, str],
    challenger_final_seed_order: Sequence[str],
    native_source_identity: str,
    runtime_identity_digest: str,
) -> dict[str, Any]:
    if match_spec.get("stage") != "baseline-final":
        raise ValueError("promotion evidence must use the baseline-final spec")
    if (
        match_spec.get("resolved_first_effective_id") != candidate.effective_id
        or match_spec.get("resolved_second_effective_id") != baseline_effective_id
    ):
        raise ValueError("baseline-final participant slots are not frozen")
    effective_by_profile_id = {
        candidate.profile_id: candidate.effective_id,
        baseline_profile_id: baseline_effective_id,
    }
    summary = validate_frozen_match_report(
        report,
        match_spec=match_spec,
        protocol_digest=protocol_digest,
        tournament_plan_digest=tournament_plan_digest,
        effective_by_profile_id=effective_by_profile_id,
    )
    _validate_promotion_execution_binding(
        report,
        match_spec,
        native_source_identity=native_source_identity,
        runtime_identity_digest=runtime_identity_digest,
    )
    try:
        batch_index = int(match_spec["promotion_batch_index"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("promotion batch index is not bound to the final") from error
    if batch_index < 1:
        raise ValueError("promotion batch index must be positive")
    if (
        summary["candidate_profile_id"] != candidate.profile_id
        or summary["reference_profile_id"] != baseline_profile_id
    ):
        raise ValueError("baseline final profile identities do not match")
    if challenger_final_match_spec.get("stage") != "challenger-final":
        raise ValueError("challenger winner must come from the challenger final")
    challenger_winner = derive_verified_knockout_winner(
        challenger_final_report,
        match_spec=challenger_final_match_spec,
        protocol_digest=protocol_digest,
        tournament_plan_digest=tournament_plan_digest,
        effective_by_profile_id=challenger_final_effective_by_profile_id,
        preregistered_seed_order=challenger_final_seed_order,
    )
    _validate_promotion_execution_binding(
        challenger_final_report,
        challenger_final_match_spec,
        native_source_identity=native_source_identity,
        runtime_identity_digest=runtime_identity_digest,
    )
    for key in (
        "environment_digest",
        "runtime_identity_digest",
        "profile_catalog_digest",
        "corpus_exclusion_digest",
        "corpus_exclusion_authority",
        "promotion_batch_index",
        "promotion_batch_chain_digest",
        "promotion_registry_id",
        "promotion_registry_authority",
        "promotion_batch_artifact_digest",
        "expansion_decision_digest",
    ):
        if challenger_final_match_spec.get(key) != match_spec.get(key):
            raise ValueError("promotion finals do not share one frozen runner identity")
    if (
        challenger_winner["winner_effective_id"] != candidate.effective_id
        or challenger_winner["winner_profile_id"] != candidate.profile_id
    ):
        raise ValueError("baseline candidate is not the verified challenger winner")
    batch_artifact, tournament_authority = _validate_trusted_promotion_batch(
        match_spec,
        protocol_digest=protocol_digest,
        tournament_plan_digest=tournament_plan_digest,
        baseline_effective_id=baseline_effective_id,
        candidate=candidate,
    )
    _validate_trusted_tournament_database(
        tournament_authority,
        tournament_plan_digest=tournament_plan_digest,
        evidence=(
            (challenger_final_report, challenger_final_match_spec),
            (report, match_spec),
        ),
    )
    gate_reasons: list[str] = []
    try:
        validate_tactical_bundle(
            candidate,
            depth2_tactical_bundle,
            protocol_digest=protocol_digest,
            native_source_identity=native_source_identity,
            runtime_identity_digest=runtime_identity_digest,
        )
    except ValueError as error:
        gate_reasons.append(f"depth2: {error}")
    try:
        _validate_artifact_identity(
            depth3_human_gate_artifact,
            format_name="spc-human-gate-artifact-v1",
            protocol_digest=protocol_digest,
            native_source_identity=native_source_identity,
            runtime_identity_digest=runtime_identity_digest,
            digest_domain="spc-human-gate-artifact-v1\0",
        )
        if (
            depth3_human_gate_artifact.get("effective_id")
            != candidate.effective_id
            or depth3_human_gate_artifact.get("profile_id")
            != candidate.profile_id
            or depth3_human_gate_artifact.get("depth") != 3
            or depth3_human_gate_artifact.get("max_work") != 5_000_000
        ):
            raise ValueError("depth-3 gate artifact identity/limits mismatch")
        human_gate = depth3_human_gate_artifact.get("human_refutation_gate")
        if not isinstance(human_gate, Mapping):
            raise ValueError("depth-3 human gate is missing")
        _validate_human_gate(
            candidate.profile_id,
            human_gate,
            depth=3,
            max_work=5_000_000,
        )
    except ValueError as error:
        gate_reasons.append(f"depth3: {error}")
    evidence = summary["profiles"][candidate.profile_id]
    baseline_evidence = summary["profiles"][baseline_profile_id]
    units = evidence["pair_score_quarter_units"]
    sign_flip = exact_sign_flip_p_value(units) if units else None
    reasons = list(gate_reasons)
    if evidence["completed_pairs"] != 200 or evidence["incomplete_pairs"]:
        reasons.append(
            f"only {evidence['completed_pairs']}/200 complete color-swapped pairs"
        )
    if (
        evidence["attributed_technical_failures"]
        or baseline_evidence["attributed_technical_failures"]
    ):
        reasons.append("baseline final contains an attributed technical failure")
    if len(units) == 200 and 25 * sum(units) <= 52 * 200:
        reasons.append("paired mean score is not above 0.52")
    threshold_denominator = 20 * (2**batch_index)
    if (
        sign_flip is None
        or int(sign_flip["numerator"]) * threshold_denominator
        > int(sign_flip["denominator"])
    ):
        reasons.append(
            f"exact one-sided p-value exceeds 0.05/(2**{batch_index})"
        )
    promoted = not reasons
    decision = {
        "promoted": promoted,
        "protocol_digest": protocol_digest,
        "tournament_plan_digest": tournament_plan_digest,
        "candidate_effective_id": candidate.effective_id,
        "candidate_profile_id": candidate.profile_id,
        "baseline_profile_id": baseline_profile_id,
        "challenger_winner_artifact": challenger_winner,
        "baseline_bound_report_digest": report["bound_report_digest"],
        "challenger_final_bound_report_digest": challenger_final_report[
            "bound_report_digest"
        ],
        "depth2_tactical_artifact_digest": depth2_tactical_bundle[
            "artifact_digest"
        ],
        "depth3_human_gate_artifact_digest": depth3_human_gate_artifact[
            "artifact_digest"
        ],
        "environment_digest": match_spec["environment_digest"],
        "runtime_identity_digest": match_spec["runtime_identity_digest"],
        "native_source_identity": native_source_identity,
        "profile_catalog_digest": match_spec["profile_catalog_digest"],
        "corpus_exclusion_digest": match_spec["corpus_exclusion_digest"],
        "corpus_exclusion_authority": match_spec[
            "corpus_exclusion_authority"
        ],
        "baseline_opening_suite_digest": match_spec["opening_suite_digest"],
        "challenger_opening_suite_digest": challenger_final_match_spec[
            "opening_suite_digest"
        ],
        "expansion_decision_digest": match_spec[
            "expansion_decision_digest"
        ],
        "completed_pairs": evidence["completed_pairs"],
        "incomplete_pairs": evidence["incomplete_pairs"],
        "pair_score_quarter_units_total": sum(units),
        "pair_mean_score": (
            sum(units) / (4 * len(units)) if units else None
        ),
        "pair_wdl": evidence["pair_wdl"],
        "color_wdl": evidence["color_wdl"],
        "pair_uncertainty": evidence["pair_uncertainty"],
        "sign_flip": sign_flip,
        "batch_index": batch_index,
        "promotion_batch_artifact_digest": batch_artifact["artifact_digest"],
        "promotion_batch_chain_digest": batch_artifact["chain_digest"],
        "promotion_registry_id": batch_artifact["registry_id"],
        "tournament_authority_digest": tournament_authority[
            "authority_digest"
        ],
        "tournament_runner_state_digest": tournament_authority[
            "runner_state_digest"
        ],
        "p_threshold": f"1/{threshold_denominator}",
        "gate_reasons": gate_reasons,
        "reason": (
            "promoted by frozen baseline-final-v1"
            if promoted
            else "not promoted: " + "; ".join(reasons)
        ),
        "champion_file_changed": False,
        "publication_effect": "none; explicit separate publication is required",
    }
    return {
        **decision,
        "promotion_decision_digest": _record_trusted_promotion_decision(
            batch_artifact, decision
        ),
    }


def projected_runtime(
    total_games: int, *, scheduled_games_per_second: float
) -> dict[str, float]:
    if total_games < 0:
        raise ValueError("total_games cannot be negative")
    if (
        not math.isfinite(scheduled_games_per_second)
        or scheduled_games_per_second <= 0
    ):
        raise ValueError(
            "scheduled_games_per_second must be finite and positive"
        )
    seconds = total_games / scheduled_games_per_second
    return {
        "total_games": float(total_games),
        "scheduled_games_per_second": scheduled_games_per_second,
        "projected_seconds": seconds,
        "projected_hours": seconds / 3_600,
    }


def write_json_atomic(
    payload: Mapping[str, Any], destination: str | Path
) -> Path:
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                payload,
                stream,
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
            )
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
