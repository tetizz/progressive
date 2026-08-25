from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

from . import evaluation, model
from .league import GameRecord, OpeningCase, runtime_provenance
from .profiles import EngineProfile
from .selfplay_training import (
    FULLGAME_CORPUS_METHOD,
    SELFPLAY_CORPUS_METHOD,
    SelfPlayCorpus,
)
from .strength import (
    SEEDED_OPENING_SUITE_FORMAT,
    STRENGTH_REPORT_FORMAT,
    SeededOpeningHistory,
    SeededOpeningSuite,
    StrengthMatchConfig,
    _build_jobs,
    _game_payload,
    _summarize,
    build_seeded_opening_suite,
    compose_seeded_opening_suite,
    run_strength_match,
    seeded_opening_suite_from_dict,
    subset_seeded_opening_suite,
    verify_seeded_opening_suite,
)
from .tournament import (
    FROZEN_EXPANSION_OVERHEAD_RESERVE_SECONDS,
    MAX_REPLACEMENT_OPENING_ATTEMPTS,
    OPENING_RESERVE_FORMAT,
    OPENING_RETRY_POLICY_FORMAT,
    TOTAL_OPENING_ATTEMPTS,
    TRUSTED_PROMOTION_REGISTRY_PATH,
    TOURNAMENT_PROTOCOL_FORMAT,
    attach_replay_verified_full_traces,
    bind_frozen_match_report,
    build_tournament_run_checkpoint,
    canonical_digest,
    canonical_json_bytes,
    choose_result_blind_expansion,
    derive_verified_knockout_winner,
    effective_profile_id,
    make_promotion_batch_artifact,
    make_tournament_authority_artifact,
    opening_reserve_digest,
    rank_group,
    tournament_limits_for_stage,
    tournament_database_state_artifact,
    tournament_opening_retry_policy,
    validate_promotion_batch_artifact,
    validate_result_blind_expansion_decision,
    validate_frozen_match_report,
)


TOURNAMENT_RUNNER_FORMAT = "spc-tournament-runner-v2"
TOURNAMENT_ENVIRONMENT_FORMAT = "spc-tournament-environment-v1"
OPENING_SUITE_BINDING_FORMAT = "spc-tournament-opening-suite-v1"
CORPUS_EXCLUSION_FORMAT = "spc-tournament-corpus-exclusion-v1"
OPENING_SUITE_SELECTION_FORMAT = "spc-tournament-opening-selection-v2"
OPENING_PREPARATION_FORMAT = "spc-tournament-opening-preparation-v2"
MATCH_ATTEMPT_FORMAT = "spc-tournament-match-attempt-v1"
REQUESTED_WORKERS = 16
_OPENING_DOMAIN_COUNTS = (
    *((f"group-{index:02d}-openings", 50) for index in range(1, 9)),
    ("r16-openings", 50),
    ("qf-openings", 100),
    ("semifinal-openings", 100),
    ("challenger-final-openings", 100),
    ("baseline-final-openings", 200),
)
_STAGE_KEYS = (
    "group",
    "round_of_16",
    "quarterfinal",
    "semifinal",
    "challenger_final",
    "baseline_final",
)
_STAGE_NAMES = {
    "group": "group",
    "round_of_16": "round-of-16",
    "quarterfinal": "quarterfinal",
    "semifinal": "semifinal",
    "challenger_final": "challenger-final",
    "baseline_final": "baseline-final",
}
_KNOCKOUT_STAGES = (
    "round-of-16",
    "quarterfinal",
    "semifinal",
    "challenger-final",
)
_TEST_OPENING_RESERVE_CACHE: dict[str, tuple[dict[str, Any], ...]] = {}


class OpeningReplacementExhaustedError(RuntimeError):
    """All pre-frozen whole-pair opening attempts were consumed incomplete."""


def _canonical_strength_claim_scope() -> dict[str, Any]:
    return {
        "fixed_suite_only": True,
        "statement": (
            "Results apply only to these versioned Scottish Progressive "
            "boundaries and exact deterministic search limits."
        ),
        "promotion_effect": "none; this harness never changes the champion",
        "stockfish_comparison": (
            "This report does not establish Stockfish-level strength. Orthodox "
            "Stockfish is not a Scottish Progressive rules engine and was not "
            "a participant."
        ),
    }


def _canonical_json(payload: Any) -> str:
    return canonical_json_bytes(payload).decode("ascii")


def _expansion_artifact_envelope(
    decision: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    artifact_digest = canonical_digest(
        "spc-tournament-runner-expansion-decision-v3\0", decision
    )
    return (
        {**dict(decision), "runner_artifact_digest": artifact_digest},
        artifact_digest,
    )


def _sha256_hex(value: str) -> None:
    try:
        decoded = bytes.fromhex(value)
    except ValueError as error:
        raise ValueError("identity must be a SHA-256 hex digest") from error
    if len(decoded) != 32:
        raise ValueError("identity must be a SHA-256 hex digest")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class VerifiedFullGameCorpusSpec:
    store_root: str | Path
    expected_corpus_id: str
    seed: int = 20_260_820
    holdout_percent: int = 20
    max_games: int | None = None

    def __post_init__(self) -> None:
        if not self.expected_corpus_id.startswith("spc-fullgame-corpus-"):
            raise ValueError("verified corpus spec requires a full-game corpus id")
        if not 0 <= self.holdout_percent <= 50:
            raise ValueError("verified corpus holdout percent is invalid")
        if self.max_games is not None and self.max_games < 1:
            raise ValueError("verified corpus max_games must be positive")


def _build_corpus_exclusion_artifact_from_corpora(
    corpora: Sequence[SelfPlayCorpus],
    *,
    authority: str,
    verified_roots: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Seals exact verified corpus snapshots into the opening exclusion set."""

    if not corpora:
        raise ValueError("at least one verified corpus snapshot is required")
    records: list[dict[str, Any]] = []
    position_hashes: set[str] = set()
    seen_corpus_ids: set[str] = set()
    for corpus in sorted(corpora, key=lambda item: item.corpus_id):
        if not isinstance(corpus, SelfPlayCorpus):
            raise TypeError("corpus exclusion inputs must be SelfPlayCorpus objects")
        payload = corpus.as_dict()
        if (
            payload.get("source_fingerprint") != model.ENGINE_SOURCE_FINGERPRINT
            or payload.get("corpus_id") != corpus.corpus_id
            or corpus.corpus_id in seen_corpus_ids
        ):
            raise ValueError("corpus snapshot identity is stale or duplicated")
        seen_corpus_ids.add(corpus.corpus_id)
        evidence = [dict(item) for item in corpus.database_evidence]
        if not evidence:
            raise ValueError("corpus snapshot lacks database/store evidence")
        if corpus.method == FULLGAME_CORPUS_METHOD:
            verified = [
                item
                for item in evidence
                if item.get("source_kind") == "verified-fullgame-store-snapshot"
            ]
            if len(verified) != 1:
                raise ValueError("full-game corpus lacks one verified store snapshot")
            _sha256_hex(str(verified[0].get("manifest_sha256", "")))
        for sample in corpus.samples:
            value = str(sample.position_hash)
            try:
                decoded = bytes.fromhex(value)
            except ValueError as error:
                raise ValueError("corpus sample position hash is not hex") from error
            if len(decoded) != 16:
                raise ValueError("corpus sample position hash is not 128-bit")
            position_hashes.add(value)
        records.append(
            {
                "corpus_id": corpus.corpus_id,
                "method": corpus.method,
                "snapshot_digest": canonical_digest(
                    "spc-selfplay-corpus-snapshot-v1\0", payload
                ),
                "database_evidence_digest": canonical_digest(
                    "spc-selfplay-corpus-evidence-v1\0", evidence
                ),
                "sample_count": len(corpus.samples),
                "verified_store_manifest_sha256": (
                    str(verified[0]["manifest_sha256"])
                    if corpus.method == FULLGAME_CORPUS_METHOD
                    else None
                ),
                "verified_store_root": (
                    None
                    if verified_roots is None
                    else verified_roots.get(corpus.corpus_id)
                ),
            }
        )
    hashes = sorted(position_hashes)
    deterministic = {
        "format": CORPUS_EXCLUSION_FORMAT,
        "source_fingerprint": model.ENGINE_SOURCE_FINGERPRINT,
        "authority": authority,
        "corpora": records,
        "corpus_ids": sorted(seen_corpus_ids),
        "sample_count": sum(record["sample_count"] for record in records),
        "unique_position_count": len(hashes),
        "position_hashes": hashes,
        "position_hash_digest": canonical_digest(
            "spc-tournament-corpus-position-hashes-v1\0", hashes
        ),
    }
    return {
        **deterministic,
        "artifact_digest": canonical_digest(
            "spc-tournament-corpus-exclusion-v1\0", deterministic
        ),
    }


def build_corpus_exclusion_artifact(
    snapshots: Sequence[VerifiedFullGameCorpusSpec],
) -> dict[str, Any]:
    """Re-verifies each actual full-game store before deriving exclusions."""

    from .selfplay_training import build_verified_fullgame_corpus

    if not snapshots or any(
        type(snapshot) is not VerifiedFullGameCorpusSpec for snapshot in snapshots
    ):
        raise TypeError(
            "production corpus exclusions require verified full-game store specs"
        )
    corpora: list[SelfPlayCorpus] = []
    roots: dict[str, str] = {}
    for snapshot in snapshots:
        root = Path(snapshot.store_root).expanduser().resolve()
        corpus = build_verified_fullgame_corpus(
            root,
            seed=snapshot.seed,
            holdout_percent=snapshot.holdout_percent,
            max_games=snapshot.max_games,
        )
        if corpus.corpus_id != snapshot.expected_corpus_id:
            raise ValueError("verified full-game corpus id does not match expectation")
        corpora.append(corpus)
        roots[corpus.corpus_id] = str(root)
    return _build_corpus_exclusion_artifact_from_corpora(
        corpora,
        authority="store-reverified-v1",
        verified_roots=roots,
    )


def validate_corpus_exclusion_artifact(
    artifact: Mapping[str, Any],
) -> tuple[str, ...]:
    required = {
        "format",
        "source_fingerprint",
        "authority",
        "corpora",
        "corpus_ids",
        "sample_count",
        "unique_position_count",
        "position_hashes",
        "position_hash_digest",
        "artifact_digest",
    }
    if set(artifact) != required or (
        artifact.get("format") != CORPUS_EXCLUSION_FORMAT
        or artifact.get("source_fingerprint") != model.ENGINE_SOURCE_FINGERPRINT
    ):
        raise ValueError("corpus exclusion artifact identity is stale")
    deterministic = {
        key: value for key, value in artifact.items() if key != "artifact_digest"
    }
    if artifact.get("artifact_digest") != canonical_digest(
        "spc-tournament-corpus-exclusion-v1\0", deterministic
    ):
        raise ValueError("corpus exclusion artifact digest mismatch")
    corpora = artifact.get("corpora")
    corpus_ids = artifact.get("corpus_ids")
    hashes = artifact.get("position_hashes")
    if (
        not isinstance(corpora, list)
        or not corpora
        or not isinstance(corpus_ids, list)
        or not isinstance(hashes, list)
        or not hashes
        or corpus_ids != sorted(str(item.get("corpus_id", "")) for item in corpora)
        or len(set(corpus_ids)) != len(corpus_ids)
        or hashes != sorted(set(str(value) for value in hashes))
        or int(artifact.get("sample_count", -1))
        != sum(int(item.get("sample_count", -1)) for item in corpora)
        or int(artifact.get("unique_position_count", -1)) != len(hashes)
        or artifact.get("position_hash_digest")
        != canonical_digest("spc-tournament-corpus-position-hashes-v1\0", hashes)
    ):
        raise ValueError("corpus exclusion artifact payload is inconsistent")
    for record in corpora:
        if not isinstance(record, Mapping) or set(record) != {
            "corpus_id",
            "method",
            "snapshot_digest",
            "database_evidence_digest",
            "sample_count",
            "verified_store_manifest_sha256",
            "verified_store_root",
        }:
            raise ValueError("corpus exclusion snapshot record is invalid")
        method = record.get("method")
        corpus_id = str(record.get("corpus_id", ""))
        expected_prefix = (
            "spc-fullgame-corpus-"
            if method == FULLGAME_CORPUS_METHOD
            else "spc-selfplay-corpus-"
            if method == SELFPLAY_CORPUS_METHOD
            else ""
        )
        if (
            not expected_prefix
            or not corpus_id.startswith(expected_prefix)
            or int(record.get("sample_count", 0)) < 1
        ):
            raise ValueError("corpus exclusion snapshot identity is invalid")
        _sha256_hex(str(record.get("snapshot_digest", "")))
        _sha256_hex(str(record.get("database_evidence_digest", "")))
        manifest = record.get("verified_store_manifest_sha256")
        store_root = record.get("verified_store_root")
        if method == FULLGAME_CORPUS_METHOD:
            _sha256_hex(str(manifest or ""))
            if artifact.get("authority") == "store-reverified-v1":
                if (
                    not isinstance(store_root, str)
                    or not Path(store_root).is_absolute()
                ):
                    raise ValueError("verified corpus store root is invalid")
            elif artifact.get("authority") != "test-only-synthetic-fixture":
                raise ValueError("corpus exclusion authority is invalid")
        elif manifest is not None:
            raise ValueError("non-fullgame corpus cannot claim a store manifest")
        elif store_root is not None:
            raise ValueError("non-fullgame corpus cannot claim a store root")
    for value in hashes:
        try:
            decoded = bytes.fromhex(value)
        except ValueError as error:
            raise ValueError("corpus exclusion position hash is not hex") from error
        if len(decoded) != 16:
            raise ValueError("corpus exclusion position hash is not 128-bit")
    return tuple(hashes)


PROMOTION_REGISTRY_FORMAT = "spc-promotion-batch-registry-v1"
PROMOTION_BATCH_ABANDONMENT_FORMAT = "spc-promotion-batch-abandonment-v1"
PromotionBatchAbandonmentReason = Literal[
    "source-stale",
    "invalid-opening-plan",
]
_PROMOTION_BATCH_ABANDONMENT_REASONS = frozenset(
    {"source-stale", "invalid-opening-plan"}
)


def _promotion_registry_id(path: Path) -> str:
    return canonical_digest(
        "spc-promotion-batch-registry-id-v1\0",
        {"resolved_path": str(path).casefold()},
    )


def _connect_promotion_registry(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("pragma journal_mode=WAL")
        connection.execute("pragma synchronous=FULL")
        connection.execute("pragma foreign_keys=ON")
        connection.executescript(
            """
            create table if not exists registry_identity (
                singleton integer primary key check(singleton=1),
                format text not null,
                registry_id text not null unique
            );
            create table if not exists promotion_batches (
                batch_index integer primary key,
                reservation_key text not null unique,
                protocol_digest text not null,
                baseline_effective_id text not null,
                predecessor_chain_digest text not null,
                chain_digest text not null unique,
                artifact_json text not null,
                artifact_digest text not null unique,
                plan_digest text,
                status text not null
            );
            create table if not exists challenger_survivors (
                effective_id text primary key,
                batch_index integer not null,
                plan_digest text not null,
                candidate_index integer not null,
                profile_id text not null,
                foreign key(batch_index) references promotion_batches(batch_index)
            );
            create table if not exists opening_positions (
                position_hash text primary key,
                batch_index integer not null,
                domain text not null,
                suite_digest text not null,
                plan_digest text not null,
                foreign key(batch_index) references promotion_batches(batch_index)
            );
            create table if not exists promotion_decisions (
                batch_index integer primary key,
                decision_json text not null,
                decision_digest text not null unique,
                promoted integer not null,
                foreign key(batch_index) references promotion_batches(batch_index)
            );
            create table if not exists tournament_authorities (
                batch_index integer primary key,
                authority_json text not null,
                authority_digest text not null unique,
                database_path text not null,
                runner_state_digest text not null unique,
                foreign key(batch_index) references promotion_batches(batch_index)
            );
            """
        )
        registry_id = _promotion_registry_id(path)
        row = connection.execute(
            "select format,registry_id from registry_identity where singleton=1"
        ).fetchone()
        if row is None:
            connection.execute(
                "insert into registry_identity values(1,?,?)",
                (PROMOTION_REGISTRY_FORMAT, registry_id),
            )
            connection.commit()
        elif (
            row["format"] != PROMOTION_REGISTRY_FORMAT
            or row["registry_id"] != registry_id
        ):
            raise ValueError("promotion registry identity changed")
        return connection
    except BaseException:
        connection.close()
        raise


def _promotion_registry_opening_snapshot(
    connection: sqlite3.Connection,
) -> tuple[dict[str, Any], set[str]]:
    last = connection.execute(
        "select batch_index,chain_digest,status from promotion_batches "
        "order by batch_index desc limit 1"
    ).fetchone()
    decision = (
        None
        if last is None
        else connection.execute(
            "select decision_digest,promoted from promotion_decisions where batch_index=?",
            (int(last["batch_index"]),),
        ).fetchone()
    )
    opening_rows = [
        list(row)
        for row in connection.execute(
            "select position_hash,batch_index,domain,suite_digest,plan_digest "
            "from opening_positions order by position_hash"
        )
    ]
    deterministic = {
        "format": "spc-promotion-registry-opening-snapshot-v1",
        "last_batch": (
            None
            if last is None
            else {
                "batch_index": int(last["batch_index"]),
                "chain_digest": str(last["chain_digest"]),
                "status": str(last["status"]),
                "decision_digest": (
                    None if decision is None else str(decision["decision_digest"])
                ),
                "promoted": None if decision is None else int(decision["promoted"]),
            }
        ),
        "opening_rows": opening_rows,
    }
    digest = canonical_digest(
        "spc-promotion-registry-opening-snapshot-v1\0", deterministic
    )
    return (
        {**deterministic, "snapshot_digest": digest},
        {str(row[0]) for row in opening_rows},
    )


def reserve_promotion_batch(
    registry_path: str | Path,
    *,
    reservation_key: str,
    protocol_digest: str,
    baseline_effective_id: str,
    expected_registry_snapshot_digest: str | None = None,
) -> dict[str, Any]:
    """Atomically reserves one chronological alpha-spending batch."""

    path = Path(registry_path).expanduser().resolve()
    connection = _connect_promotion_registry(path)
    try:
        connection.execute("begin immediate")
        existing = connection.execute(
            "select * from promotion_batches where reservation_key=?",
            (reservation_key,),
        ).fetchone()
        if existing is not None:
            artifact = json.loads(existing["artifact_json"])
            validate_promotion_batch_artifact(
                artifact,
                protocol_digest=protocol_digest,
                baseline_effective_id=baseline_effective_id,
            )
            if (
                existing["protocol_digest"] != protocol_digest
                or existing["baseline_effective_id"] != baseline_effective_id
            ):
                raise ValueError("promotion batch reservation identity changed")
            connection.commit()
            return artifact
        if expected_registry_snapshot_digest is not None:
            _sha256_hex(expected_registry_snapshot_digest)
            snapshot, _hashes = _promotion_registry_opening_snapshot(connection)
            if snapshot["snapshot_digest"] != expected_registry_snapshot_digest:
                raise ValueError(
                    "promotion registry changed after opening-suite preparation"
                )
        last = connection.execute(
            "select batch_index,chain_digest,status from promotion_batches "
            "order by batch_index desc limit 1"
        ).fetchone()
        if last is not None:
            prior_decision = connection.execute(
                "select promoted from promotion_decisions where batch_index=?",
                (int(last["batch_index"]),),
            ).fetchone()
            if (
                last["status"] not in {"complete", "abandoned"}
                or prior_decision is None
                or (
                    last["status"] == "abandoned"
                    and int(prior_decision["promoted"]) != 0
                )
            ):
                raise ValueError(
                    "the previous promotion batch has no sealed final decision"
                )
        batch_index = 1 if last is None else int(last["batch_index"]) + 1
        predecessor = "00" * 32 if last is None else str(last["chain_digest"])
        artifact = make_promotion_batch_artifact(
            registry_id=_promotion_registry_id(path),
            reservation_key=reservation_key,
            batch_index=batch_index,
            protocol_digest=protocol_digest,
            baseline_effective_id=baseline_effective_id,
            predecessor_chain_digest=predecessor,
        )
        connection.execute(
            """
            insert into promotion_batches(
                batch_index,reservation_key,protocol_digest,
                baseline_effective_id,predecessor_chain_digest,chain_digest,
                artifact_json,artifact_digest,plan_digest,status
            ) values(?,?,?,?,?,?,?,?,null,'reserved')
            """,
            (
                batch_index,
                reservation_key,
                protocol_digest,
                baseline_effective_id,
                predecessor,
                artifact["chain_digest"],
                _canonical_json(artifact),
                artifact["artifact_digest"],
            ),
        )
        connection.commit()
        return artifact
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def abandon_promotion_batch(
    registry_path: str | Path,
    *,
    batch_index: int,
    expected_plan_digest: str,
    reason: PromotionBatchAbandonmentReason,
    _allow_test_registry: bool = False,
) -> dict[str, Any]:
    """Consumes an invalid alpha batch with a sealed no-promotion decision.

    This is deliberately narrow administrative recovery.  It never deletes a
    survivor or opening reservation and cannot turn a completed batch into an
    abandonment.  Repeating the exact command is idempotent.
    """

    path = Path(registry_path).expanduser().resolve()
    if path != TRUSTED_PROMOTION_REGISTRY_PATH.resolve() and not _allow_test_registry:
        raise ValueError("administrative abandonment requires the trusted registry")
    if type(batch_index) is not int or batch_index < 1:
        raise ValueError("administrative abandonment batch index is invalid")
    if reason not in _PROMOTION_BATCH_ABANDONMENT_REASONS:
        raise ValueError("administrative abandonment reason is unsupported")
    _sha256_hex(expected_plan_digest)

    connection = _connect_promotion_registry(path)
    try:
        connection.execute("begin immediate")
        row = connection.execute(
            "select * from promotion_batches where batch_index=?", (batch_index,)
        ).fetchone()
        if row is None:
            raise ValueError("administrative abandonment batch does not exist")
        if row["plan_digest"] != expected_plan_digest:
            raise ValueError("administrative abandonment plan digest changed")
        artifact = json.loads(str(row["artifact_json"]))
        validate_promotion_batch_artifact(
            artifact,
            protocol_digest=str(row["protocol_digest"]),
            baseline_effective_id=str(row["baseline_effective_id"]),
            source_fingerprint=str(artifact.get("source_fingerprint", "")),
        )
        if (
            artifact.get("artifact_digest") != row["artifact_digest"]
            or artifact.get("chain_digest") != row["chain_digest"]
        ):
            raise ValueError("administrative abandonment batch artifact changed")
        deterministic = {
            "format": PROMOTION_BATCH_ABANDONMENT_FORMAT,
            "batch_index": batch_index,
            "reservation_key": str(row["reservation_key"]),
            "protocol_digest": str(row["protocol_digest"]),
            "baseline_effective_id": str(row["baseline_effective_id"]),
            "promotion_batch_artifact_digest": str(row["artifact_digest"]),
            "promotion_batch_chain_digest": str(row["chain_digest"]),
            "abandoned_source_fingerprint": str(
                artifact.get("source_fingerprint", "")
            ),
            "plan_digest": expected_plan_digest,
            "reason": reason,
            "alpha_batch_consumed": True,
            "promoted": False,
            "promotion_effect": "none",
        }
        decision_digest = canonical_digest(
            "spc-promotion-batch-abandonment-v1\0", deterministic
        )
        decision = {**deterministic, "decision_digest": decision_digest}
        existing = connection.execute(
            "select decision_json,decision_digest,promoted from promotion_decisions "
            "where batch_index=?",
            (batch_index,),
        ).fetchone()
        if row["status"] == "abandoned":
            if (
                existing is None
                or existing["decision_json"] != _canonical_json(decision)
                or existing["decision_digest"] != decision_digest
                or int(existing["promoted"]) != 0
            ):
                raise ValueError("sealed administrative abandonment changed")
            connection.commit()
            return decision
        if row["status"] not in {"plan-bound", "running"}:
            raise ValueError(
                "only a plan-bound or running batch can be administratively abandoned"
            )
        if existing is not None:
            raise ValueError("promotion batch already has a final decision")
        connection.execute(
            "insert into promotion_decisions values(?,?,?,0)",
            (batch_index, _canonical_json(decision), decision_digest),
        )
        connection.execute(
            "update promotion_batches set status='abandoned' where batch_index=?",
            (batch_index,),
        )
        connection.commit()
        return decision
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _opening_selection_seed(
    *,
    protocol_digest: str,
    reservation_key: str,
    domain: str,
    master_seed: int,
    nonce: int,
) -> int:
    digest = canonical_digest(
        "spc-tournament-opening-selection-seed-v1\0",
        {
            "protocol_digest": protocol_digest,
            "reservation_key": reservation_key,
            "domain": domain,
            "master_seed": master_seed,
            "selection_nonce": nonce,
        },
    )
    return int(digest[:16], 16)


def freeze_opening_suites_before_batch(
    registry_path: str | Path,
    *,
    reservation_key: str,
    protocol_digest: str,
    corpus_exclusion_artifact: Mapping[str, Any],
    master_seed: int,
    _allow_test_registry: bool = False,
) -> dict[str, Any]:
    """Selects 13 logical reserves / 39 collision-free lanes before reservation.

    Selection reads only frozen identities, verified corpus hashes, and prior
    batch reservations.  A collision increments a domain-local nonce and
    regenerates that entire suite; no match result can influence the choice.
    The returned canonical suite content is intended to be embedded in the
    tournament plan before the promotion batch is reserved.
    """

    path = Path(registry_path).expanduser().resolve()
    if path != TRUSTED_PROMOTION_REGISTRY_PATH.resolve() and not _allow_test_registry:
        raise ValueError("opening-suite freezing requires the trusted registry")
    if not reservation_key.strip():
        raise ValueError("opening-suite freezing reservation key is empty")
    if (
        not _allow_test_registry
        and corpus_exclusion_artifact.get("authority") != "store-reverified-v1"
    ):
        raise ValueError(
            "opening-suite freezing requires store-reverified corpus exclusions"
        )
    _sha256_hex(protocol_digest)
    exclusions = set(validate_corpus_exclusion_artifact(corpus_exclusion_artifact))
    connection = _connect_promotion_registry(path)
    try:
        connection.execute("begin")
        snapshot, prior_hashes = _promotion_registry_opening_snapshot(connection)
        last = snapshot["last_batch"]
        if last is not None:
            if (
                last["status"] not in {"complete", "abandoned"}
                or last["decision_digest"] is None
                or (
                    last["status"] == "abandoned"
                    and int(last["promoted"]) != 0
                )
            ):
                raise ValueError(
                    "opening suites cannot freeze while the prior alpha batch is open"
                )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    seen = exclusions | prior_hashes
    frozen: list[dict[str, Any]] = []
    candidate_suite_build_count = 0
    candidate_boundary_count = 0
    for domain, count in _OPENING_DOMAIN_COUNTS:
        lanes: list[dict[str, Any]] = []
        reserve_hashes: list[str] = []
        for attempt_index in range(TOTAL_OPENING_ATTEMPTS):
            lane_domain = f"{domain}|attempt|{attempt_index}"
            nonce = 0
            base_seed = _opening_selection_seed(
                protocol_digest=protocol_digest,
                reservation_key=reservation_key,
                domain=lane_domain,
                master_seed=master_seed,
                nonce=0,
            )
            while True:
                selected_seed = _opening_selection_seed(
                    protocol_digest=protocol_digest,
                    reservation_key=reservation_key,
                    domain=lane_domain,
                    master_seed=master_seed,
                    nonce=nonce,
                )
                suite = build_seeded_opening_suite(
                    seed=selected_seed,
                    count=count,
                    min_series=3,
                    max_series=6,
                    max_frontier_states=32,
                )
                candidate_suite_build_count += 1
                candidate_boundary_count += count
                hashes = sorted(case.state().position_hash for case in suite.cases)
                if not seen.intersection(hashes):
                    break
                nonce += 1
                if nonce > 65_535:
                    raise RuntimeError(
                        "could not derive a fresh opening attempt lane for "
                        f"{domain} attempt {attempt_index}"
                    )
            suite_payload = suite.as_dict()
            suite_digest = canonical_digest(
                OPENING_SUITE_BINDING_FORMAT + "\0", suite_payload
            )
            position_hash_digest = canonical_digest(
                "spc-tournament-opening-position-hashes-v1\0", hashes
            )
            lanes.append(
                {
                    "attempt_index": attempt_index,
                    "selection_domain": lane_domain,
                    "selection_master_seed": master_seed,
                    "base_seed": base_seed,
                    "selection_nonce": nonce,
                    "seed": selected_seed,
                    "count": count,
                    "suite": suite_payload,
                    "suite_digest": suite_digest,
                    "position_hash_digest": position_hash_digest,
                }
            )
            reserve_hashes.extend(hashes)
            seen.update(hashes)
        reserve: dict[str, Any] = {
            "format": OPENING_RESERVE_FORMAT,
            "domain": domain,
            "count": count,
            "total_case_count": count * TOTAL_OPENING_ATTEMPTS,
            "min_series": 3,
            "max_series": 6,
            "max_frontier_states": 32,
            "global_position_hash_exclusion_required": True,
            "retry_policy": tournament_opening_retry_policy(),
            "attempt_lanes": lanes,
            "position_hash_digest": canonical_digest(
                "spc-tournament-opening-position-hashes-v2\0",
                sorted(reserve_hashes),
            ),
        }
        reserve["reserve_digest"] = opening_reserve_digest(reserve)
        frozen.append(reserve)
    deterministic = {
        "format": OPENING_PREPARATION_FORMAT,
        "protocol_digest": protocol_digest,
        "reservation_key": reservation_key,
        "master_seed": master_seed,
        "corpus_exclusion_digest": corpus_exclusion_artifact["artifact_digest"],
        "registry_snapshot_digest": snapshot["snapshot_digest"],
        "registry_last_batch": snapshot["last_batch"],
        "opening_suites": frozen,
        "materialization_work": {
            "logical_reserve_count": len(_OPENING_DOMAIN_COUNTS),
            "attempt_lane_count": len(_OPENING_DOMAIN_COUNTS)
            * TOTAL_OPENING_ATTEMPTS,
            "selected_boundary_count": sum(
                count * TOTAL_OPENING_ATTEMPTS
                for _domain, count in _OPENING_DOMAIN_COUNTS
            ),
            "candidate_suite_build_count": candidate_suite_build_count,
            "candidate_boundary_count": candidate_boundary_count,
            "nonce_replay_bound_per_lane": 65_535,
            "wall_clock_estimate": "operational-only-not-authoritative",
        },
        "result_inputs": "none",
    }
    return {
        **deterministic,
        "preparation_digest": canonical_digest(
            "spc-tournament-opening-preparation-v2\0", deterministic
        ),
    }


def validate_opening_suite_preparation(
    preparation: Mapping[str, Any],
    *,
    protocol_digest: str,
    reservation_key: str,
    corpus_exclusion_digest: str,
) -> tuple[dict[str, Any], ...]:
    deterministic = {
        key: value for key, value in preparation.items() if key != "preparation_digest"
    }
    if (
        preparation.get("format") != OPENING_PREPARATION_FORMAT
        or preparation.get("protocol_digest") != protocol_digest
        or preparation.get("reservation_key") != reservation_key
        or preparation.get("corpus_exclusion_digest") != corpus_exclusion_digest
        or preparation.get("result_inputs") != "none"
        or preparation.get("preparation_digest")
        != canonical_digest(
            "spc-tournament-opening-preparation-v2\0", deterministic
        )
    ):
        raise ValueError("pre-batch opening preparation identity changed")
    _sha256_hex(str(preparation.get("registry_snapshot_digest", "")))
    suites = preparation.get("opening_suites")
    if not isinstance(suites, list) or len(suites) != len(_OPENING_DOMAIN_COUNTS):
        raise ValueError("pre-batch opening preparation suite table changed")
    try:
        candidate_suite_build_count = sum(
            int(lane["selection_nonce"]) + 1
            for reserve in suites
            for lane in reserve["attempt_lanes"]
        )
        candidate_boundary_count = sum(
            (int(lane["selection_nonce"]) + 1) * int(reserve["count"])
            for reserve in suites
            for lane in reserve["attempt_lanes"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("pre-batch opening materialization work is invalid") from error
    expected_work = {
        "logical_reserve_count": len(_OPENING_DOMAIN_COUNTS),
        "attempt_lane_count": len(_OPENING_DOMAIN_COUNTS)
        * TOTAL_OPENING_ATTEMPTS,
        "selected_boundary_count": sum(
            count * TOTAL_OPENING_ATTEMPTS
            for _domain, count in _OPENING_DOMAIN_COUNTS
        ),
        "candidate_suite_build_count": candidate_suite_build_count,
        "candidate_boundary_count": candidate_boundary_count,
        "nonce_replay_bound_per_lane": 65_535,
        "wall_clock_estimate": "operational-only-not-authoritative",
    }
    if preparation.get("materialization_work") != expected_work:
        raise ValueError("pre-batch opening materialization work changed")
    return tuple(copy.deepcopy(dict(item)) for item in suites)


def _bind_plan_to_promotion_registry(
    registry_path: Path, plan: Mapping[str, Any]
) -> dict[str, Any]:
    artifact = plan.get("promotion_batch")
    baseline = plan.get("baseline")
    if not isinstance(artifact, Mapping) or not isinstance(baseline, Mapping):
        raise ValueError("tournament plan lacks its promotion batch identity")
    validate_promotion_batch_artifact(
        artifact,
        protocol_digest=str(plan.get("protocol_digest", "")),
        baseline_effective_id=str(baseline.get("effective_id", "")),
    )
    if artifact.get("registry_id") != _promotion_registry_id(registry_path):
        raise ValueError("tournament plan belongs to another promotion registry")
    plan_digest = str(plan.get("tournament_plan_digest", ""))
    survivors = plan.get("survivors_in_validation_rank_order")
    if not isinstance(survivors, list) or len(survivors) != 64:
        raise ValueError("promotion batch must bind exactly 64 survivors")
    connection = _connect_promotion_registry(registry_path)
    try:
        connection.execute("begin immediate")
        row = connection.execute(
            "select * from promotion_batches where batch_index=?",
            (int(artifact["batch_index"]),),
        ).fetchone()
        if row is None or row["artifact_json"] != _canonical_json(artifact):
            raise ValueError("promotion batch was not reserved in this registry")
        predecessor = str(artifact["predecessor_chain_digest"])
        if int(artifact["batch_index"]) == 1:
            if predecessor != "00" * 32:
                raise ValueError("first promotion batch has a predecessor")
        else:
            prior = connection.execute(
                "select chain_digest from promotion_batches where batch_index=?",
                (int(artifact["batch_index"]) - 1,),
            ).fetchone()
            if prior is None or prior["chain_digest"] != predecessor:
                raise ValueError("promotion batch predecessor chain is broken")
        if row["plan_digest"] is None:
            for survivor in survivors:
                if not isinstance(survivor, Mapping):
                    raise ValueError("promotion survivor record is invalid")
                try:
                    connection.execute(
                        "insert into challenger_survivors values(?,?,?,?,?)",
                        (
                            str(survivor["effective_id"]),
                            int(artifact["batch_index"]),
                            plan_digest,
                            int(survivor["candidate_index"]),
                            str(survivor["profile_id"]),
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise ValueError(
                        "challenger survivor was already consumed by an earlier batch"
                    ) from error
            connection.execute(
                "update promotion_batches set plan_digest=?,status='plan-bound' "
                "where batch_index=?",
                (plan_digest, int(artifact["batch_index"])),
            )
        elif row["plan_digest"] != plan_digest or row["status"] not in {
            "plan-bound",
            "running",
            "complete",
        }:
            raise ValueError("promotion batch is already bound to another plan")
        expected_survivors = sorted(
            (
                str(item["effective_id"]),
                int(artifact["batch_index"]),
                plan_digest,
                int(item["candidate_index"]),
                str(item["profile_id"]),
            )
            for item in survivors
        )
        actual_survivors = [
            tuple(row)
            for row in connection.execute(
                "select effective_id,batch_index,plan_digest,candidate_index,"
                "profile_id from challenger_survivors where batch_index=? "
                "order by effective_id",
                (int(artifact["batch_index"]),),
            )
        ]
        if actual_survivors != expected_survivors:
            raise ValueError("promotion batch survivor registry changed")
        connection.commit()
        return copy.deepcopy(dict(artifact))
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class TournamentEnvironment:
    engine_version: str
    source_fingerprint: str
    native_source_identity: str
    loaded_native_source_identity: str | None
    loaded_native_binary_path: str | None
    loaded_native_binary_sha256: str | None
    runtime: Mapping[str, str]
    requested_workers: int = REQUESTED_WORKERS

    @classmethod
    def current(cls, *, require_native: bool = True) -> TournamentEnvironment:
        native_source_identity = evaluation._native_source_identity()
        if native_source_identity is None:
            raise RuntimeError("packaged native sources are unavailable")
        loaded_identity = getattr(
            getattr(evaluation, "_native_eval", None), "SOURCE_IDENTITY", None
        )
        native_module = getattr(evaluation, "_native_eval", None)
        raw_module_path = getattr(native_module, "__file__", None)
        if raw_module_path is None:
            loaded_binary_path = None
            loaded_binary_sha256 = None
        else:
            module_path = Path(raw_module_path).expanduser().resolve()
            loaded_binary_path = str(module_path)
            loaded_binary_sha256 = _file_sha256(module_path)
        if require_native and loaded_identity != native_source_identity:
            raise RuntimeError(
                "the tournament requires the current native extension; rebuild it"
            )
        return cls(
            engine_version=model.ENGINE_VERSION,
            source_fingerprint=model._source_fingerprint(),
            native_source_identity=native_source_identity,
            loaded_native_source_identity=loaded_identity,
            loaded_native_binary_path=loaded_binary_path,
            loaded_native_binary_sha256=loaded_binary_sha256,
            runtime=dict(runtime_provenance()),
        )

    def __post_init__(self) -> None:
        if not self.engine_version or not self.source_fingerprint:
            raise ValueError("engine identity cannot be empty")
        _sha256_hex(self.native_source_identity)
        if self.loaded_native_source_identity is not None:
            _sha256_hex(self.loaded_native_source_identity)
        if (self.loaded_native_binary_path is None) != (
            self.loaded_native_binary_sha256 is None
        ):
            raise ValueError("loaded native binary path/digest must appear together")
        if self.loaded_native_binary_sha256 is not None:
            _sha256_hex(self.loaded_native_binary_sha256)
        if self.requested_workers != REQUESTED_WORKERS:
            raise ValueError("the frozen tournament requires exactly 16 workers")
        if not self.runtime or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.runtime.items()
        ):
            raise ValueError("runtime identity must be a non-empty string map")

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": TOURNAMENT_ENVIRONMENT_FORMAT,
            "engine_version": self.engine_version,
            "source_fingerprint": self.source_fingerprint,
            "native_source_identity": self.native_source_identity,
            "loaded_native_source_identity": self.loaded_native_source_identity,
            "loaded_native_binary_path": self.loaded_native_binary_path,
            "loaded_native_binary_sha256": self.loaded_native_binary_sha256,
            "runtime": dict(sorted(self.runtime.items())),
            "runtime_identity_digest": self.runtime_identity_digest,
            "requested_workers": self.requested_workers,
        }

    @property
    def runtime_identity_digest(self) -> str:
        return canonical_digest(
            "spc-tournament-runtime-identity-v1\0",
            dict(sorted(self.runtime.items())),
        )

    @property
    def digest(self) -> str:
        return canonical_digest(
            "spc-tournament-environment-v1\0", self.as_dict()
        )


@dataclass(frozen=True, slots=True)
class TournamentOpeningReserve:
    domain: str
    count: int
    lanes: tuple[SeededOpeningSuite, ...]
    reserve_digest: str
    position_hash_digest: str


@dataclass(frozen=True, slots=True)
class TournamentMatchJob:
    ordinal: int
    stage: str
    matchup_id: str
    pair_count: int
    resolved_spec: Mapping[str, Any]
    first_effective_id: str
    second_effective_id: str
    first_profile: EngineProfile
    second_profile: EngineProfile
    opening_reserve: TournamentOpeningReserve
    opening_suite: SeededOpeningSuite
    opening_suite_digest: str
    attempt_manifest: Mapping[str, Any] | None
    attempt_manifest_digest: str | None
    config: StrengthMatchConfig


MatchRunner = Callable[..., Mapping[str, Any]]


def _validate_plan(plan: Mapping[str, Any]) -> None:
    # The preparation layer owns the canonical plan validation.  Building an
    # empty checkpoint exercises that validator without accepting caller-made
    # winners or result data.
    build_tournament_run_checkpoint(
        tournament_plan=plan,
        completed_pairs=(),
        expansion_decision_digest=None,
    )
    if plan.get("format") != TOURNAMENT_PROTOCOL_FORMAT:
        raise ValueError("unsupported tournament plan")
    matchups = plan.get("matchups")
    if not isinstance(matchups, Mapping) or tuple(matchups) != _STAGE_KEYS:
        raise ValueError("tournament stage table is not canonical")
    opening_suites = plan.get("opening_suites")
    if not isinstance(opening_suites, list) or len(opening_suites) != len(
        _OPENING_DOMAIN_COUNTS
    ):
        raise ValueError("tournament opening suite table is not canonical")
    reserve_by_domain = {
        str(item.get("domain")): item
        for item in opening_suites
        if isinstance(item, Mapping)
    }
    if len(reserve_by_domain) != len(opening_suites):
        raise ValueError("tournament opening domains are not unique")
    retry_policy = tournament_opening_retry_policy()
    for expected, item in zip(_OPENING_DOMAIN_COUNTS, opening_suites, strict=True):
        domain, count = expected
        if (
            not isinstance(item, Mapping)
            or item.get("domain") != domain
            or item.get("count") != count
            or item.get("retry_policy") != retry_policy
        ):
            raise ValueError("tournament opening reserve table changed")
        lanes = item.get("attempt_lanes")
        if not isinstance(lanes, list) or len(lanes) != TOTAL_OPENING_ATTEMPTS:
            raise ValueError("tournament opening reserve lanes changed")
        frozen = "reserve_digest" in item
        for attempt_index, lane in enumerate(lanes):
            if (
                not isinstance(lane, Mapping)
                or lane.get("attempt_index") != attempt_index
                or lane.get("count") != count
                or type(lane.get("seed")) is not int
                or (
                    frozen
                    and (
                        type(lane.get("selection_nonce")) is not int
                        or not 0 <= int(lane["selection_nonce"]) <= 65_535
                        or not isinstance(lane.get("suite"), Mapping)
                        or not isinstance(lane.get("suite_digest"), str)
                    )
                )
            ):
                raise ValueError("tournament opening attempt lane is invalid")
        if frozen and item.get("reserve_digest") != opening_reserve_digest(item):
            raise ValueError("tournament opening reserve digest changed")
    for key, stage in _STAGE_NAMES.items():
        specs = matchups[key]
        if not isinstance(specs, list) or any(
            not isinstance(spec, Mapping) or spec.get("stage") != stage
            for spec in specs
        ):
            raise ValueError(f"invalid {stage} matchup table")
    if len(matchups["group"]) != 224 or len(matchups["round_of_16"]) != 8:
        raise ValueError("frozen group or round-of-16 matchup count changed")
    if tuple(len(matchups[key]) for key in _STAGE_KEYS[2:]) != (4, 2, 1, 1):
        raise ValueError("frozen late-round matchup count changed")
    for specs in matchups.values():
        for spec in specs:
            limits = spec.get("limits")
            expected_limits = tournament_limits_for_stage(str(spec["stage"]))
            if not isinstance(limits, Mapping) or limits != expected_limits:
                raise ValueError("tournament deterministic limits changed")
            if int(spec.get("base_games", -1)) != int(spec["base_pairs"]) * 2:
                raise ValueError("base game/pair count mismatch")
            if int(spec.get("maximum_games", -1)) != int(spec["maximum_pairs"]) * 2:
                raise ValueError("maximum game/pair count mismatch")
            if (
                spec.get("opening_domain") not in reserve_by_domain
                or type(spec.get("opening_seed")) is not int
                or spec.get("opening_retry_policy") != retry_policy
            ):
                raise ValueError("match opening reserve policy changed")
            reserve = reserve_by_domain[str(spec["opening_domain"])]
            if "reserve_digest" in reserve and spec.get(
                "opening_reserve_digest"
            ) != reserve.get("reserve_digest"):
                raise ValueError("match opening reserve digest changed")
    expected = {"base": 24_300, "expanded": 25_000}
    for schedule, games in expected.items():
        summary = tournament_schedule_summary(plan, schedule=schedule)
        if summary["games"] != games:
            raise ValueError(f"{schedule} tournament total is not frozen")
    scheduled = plan.get("scheduled")
    if not isinstance(scheduled, Mapping) or (
        scheduled.get("base_total_games") != 24_300
        or scheduled.get("expanded_total_games") != 25_000
        or scheduled.get("replacement_opening_attempts")
        != MAX_REPLACEMENT_OPENING_ATTEMPTS
        or scheduled.get("nominal_selected_games")
        != {"base": 24_300, "expanded": 25_000}
        or scheduled.get("worst_case_executed_games")
        != {
            "base": 24_300 * TOTAL_OPENING_ATTEMPTS,
            "expanded": 25_000 * TOTAL_OPENING_ATTEMPTS,
        }
    ):
        raise ValueError("declared tournament totals changed")
    if plan.get("opening_retry_policy") != retry_policy:
        raise ValueError("tournament opening retry policy changed")
    expected_strength_contract = {
        "group_screen": {
            "stage": "group",
            "can_promote": False,
            "base_games": 22_400,
            "expanded_games": 22_400,
            "limits": tournament_limits_for_stage("group"),
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
            "limits": tournament_limits_for_stage("round-of-16"),
        },
        "variant_search_advantage": False,
        "color_swap_uses_identical_limits": True,
    }
    if plan.get("strength_contract") != expected_strength_contract:
        raise ValueError("tournament strength contract changed")


def tournament_schedule_summary(
    plan: Mapping[str, Any], *, schedule: str
) -> dict[str, int | str]:
    if schedule not in {"base", "expanded"}:
        raise ValueError("schedule must be 'base' or 'expanded'")
    matchups = plan.get("matchups")
    if not isinstance(matchups, Mapping):
        raise ValueError("tournament plan has no matchup table")
    pairs = games = match_count = 0
    for key in _STAGE_KEYS:
        specs = matchups.get(key)
        if not isinstance(specs, Sequence) or isinstance(specs, (str, bytes)):
            raise ValueError("tournament matchup table is invalid")
        for spec in specs:
            if not isinstance(spec, Mapping):
                raise ValueError("tournament match spec is invalid")
            pair_count = _scheduled_pair_count(spec, schedule=schedule)
            pairs += pair_count
            games += pair_count * 2
            match_count += 1
    return {
        "schedule": schedule,
        "matchups": match_count,
        "pairs": pairs,
        "games": games,
    }


def _scheduled_pair_count(spec: Mapping[str, Any], *, schedule: str) -> int:
    if schedule == "expanded" and spec.get("stage") in {
        "quarterfinal",
        "semifinal",
        "challenger-final",
    }:
        return int(spec["maximum_pairs"])
    return int(spec["base_pairs"])


def _profile_catalog(
    plan: Mapping[str, Any],
    profiles_by_effective_id: Mapping[str, EngineProfile],
    environment: TournamentEnvironment,
) -> tuple[dict[str, EngineProfile], dict[str, Any], str]:
    expected = {
        str(item["effective_id"]): str(item["profile_id"])
        for item in plan["survivors_in_validation_rank_order"]
    }
    baseline = plan["baseline"]
    expected[str(baseline["effective_id"])] = str(baseline["profile_id"])
    if set(profiles_by_effective_id) != set(expected):
        raise ValueError("profile catalog does not exactly cover the frozen plan")
    profiles: dict[str, EngineProfile] = {}
    records: list[dict[str, Any]] = []
    seen_profile_ids: set[str] = set()
    for effective_id in sorted(expected):
        profile = profiles_by_effective_id[effective_id]
        if not isinstance(profile, EngineProfile):
            raise TypeError("profile catalog values must be EngineProfile objects")
        recomputed = effective_profile_id(
            profile.weights,
            source_fingerprint=environment.source_fingerprint,
        )
        if recomputed != effective_id or profile.profile_id != expected[effective_id]:
            raise ValueError("profile identity does not match the frozen plan")
        if profile.profile_id in seen_profile_ids:
            raise ValueError("profile catalog reuses a profile id")
        seen_profile_ids.add(profile.profile_id)
        profiles[effective_id] = profile
        records.append(
            {"effective_id": effective_id, "profile": profile.as_dict()}
        )
    payload = {
        "format": "spc-tournament-profile-catalog-v1",
        "profiles": records,
    }
    return profiles, payload, canonical_digest(
        "spc-tournament-profile-catalog-v1\0", payload
    )


def _suite_from_dict(payload: Mapping[str, Any]) -> SeededOpeningSuite:
    return seeded_opening_suite_from_dict(payload)


def _raw_report(bound_report: Mapping[str, Any]) -> dict[str, Any]:
    raw = copy.deepcopy(dict(bound_report))
    raw.pop("bound_report_digest", None)
    raw.pop("tournament_binding", None)
    raw.pop("full_trace_evidence", None)
    for game in raw.get("games", ()):
        game.pop("full_trace", None)
        game.pop("tournament_identity", None)
    for pair in raw.get("pairs", ()):
        pair.pop("tournament_identity", None)
    return raw


class TournamentRunner:
    """Transactional, result-blind executor for the frozen challenger plan.

    Match computations happen outside SQLite transactions.  A report becomes
    authoritative only after its complete color-swapped pairs replay, bind, and
    validate, then the entire report is committed in one transaction.  A crash
    before that commit leaves no partial pair and simply reruns that match.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        tournament_plan: Mapping[str, Any],
        profiles_by_effective_id: Mapping[str, EngineProfile],
        schedule: str,
        corpus_snapshots: Sequence[VerifiedFullGameCorpusSpec] = (),
        verified_corpus_exclusion_artifact: Mapping[str, Any] | None = None,
        promotion_registry_path: str | Path = TRUSTED_PROMOTION_REGISTRY_PATH,
        expansion_decision: Mapping[str, Any] | None = None,
        _allow_test_registry: bool = False,
        _test_corpus_snapshots: Sequence[SelfPlayCorpus] = (),
        _allow_test_corpus: bool = False,
        require_native: bool = True,
    ) -> None:
        _validate_plan(tournament_plan)
        if schedule not in {"pending", "base", "expanded"}:
            raise ValueError("schedule must be pending, base, or expanded")
        self.path = Path(database_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.plan = copy.deepcopy(dict(tournament_plan))
        self._allow_test_corpus = _allow_test_corpus
        self.plan_digest = str(self.plan["tournament_plan_digest"])
        self.protocol_digest = str(self.plan["protocol_digest"])
        self.schedule = schedule
        if expansion_decision is None:
            if schedule == "expanded":
                raise ValueError(
                    "expanded schedule requires a frozen result-blind decision"
                )
            self.expansion_decision = None
            self.expansion_decision_digest = None
        else:
            if schedule == "pending":
                raise ValueError("pending schedule cannot carry a decision")
            validate_result_blind_expansion_decision(
                expansion_decision, protocol_digest=self.protocol_digest
            )
            if expansion_decision.get("schedule") != schedule:
                raise ValueError("expansion decision does not select this schedule")
            self.expansion_decision = copy.deepcopy(dict(expansion_decision))
            self.expansion_decision_digest = str(
                expansion_decision["decision_digest"]
            )
        self.environment = TournamentEnvironment.current(
            require_native=require_native
        )
        if self.plan.get("source_fingerprint") != self.environment.source_fingerprint:
            raise ValueError("tournament plan source fingerprint is stale")
        self.profiles, catalog, self.profile_catalog_digest = _profile_catalog(
            self.plan, profiles_by_effective_id, self.environment
        )
        if verified_corpus_exclusion_artifact is not None:
            if corpus_snapshots or _test_corpus_snapshots or _allow_test_corpus:
                raise ValueError("verified corpus exclusion authority is ambiguous")
            if verified_corpus_exclusion_artifact.get("authority") != "store-reverified-v1":
                raise ValueError("verified corpus exclusion artifact is not production-authoritative")
            self.corpus_exclusion_artifact = copy.deepcopy(
                dict(verified_corpus_exclusion_artifact)
            )
        elif _test_corpus_snapshots:
            if corpus_snapshots or not _allow_test_corpus:
                raise ValueError("synthetic corpus snapshots are test-only")
            self.corpus_exclusion_artifact = (
                _build_corpus_exclusion_artifact_from_corpora(
                    _test_corpus_snapshots,
                    authority="test-only-synthetic-fixture",
                )
            )
        else:
            if _allow_test_corpus:
                raise ValueError("test corpus authority has no synthetic snapshot")
            self.corpus_exclusion_artifact = build_corpus_exclusion_artifact(
                corpus_snapshots
            )
        exclusions = validate_corpus_exclusion_artifact(
            self.corpus_exclusion_artifact
        )
        self._excluded_position_hashes = frozenset(exclusions)
        self._exclusion_digest = str(
            self.corpus_exclusion_artifact["artifact_digest"]
        )
        self.corpus_exclusion_authority = str(
            self.corpus_exclusion_artifact["authority"]
        )
        self.promotion_registry_path = (
            Path(promotion_registry_path).expanduser().resolve()
        )
        trusted_registry = TRUSTED_PROMOTION_REGISTRY_PATH.resolve()
        if self.promotion_registry_path != trusted_registry and not _allow_test_registry:
            raise ValueError(
                "promotion batches must use the trusted project registry"
            )
        self.promotion_registry_authority = (
            "trusted-project-registry-v1"
            if self.promotion_registry_path == trusted_registry
            else "test-only-untrusted-registry"
        )
        self.promotion_batch = _bind_plan_to_promotion_registry(
            self.promotion_registry_path, self.plan
        )
        self.connection = sqlite3.connect(self.path)
        self._opening_suites_ready: dict[str, TournamentOpeningReserve] | None = None
        self._replay_validated_attempt_digests: set[str] = set()
        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("pragma journal_mode=WAL")
            self.connection.execute("pragma synchronous=FULL")
            self.connection.execute("pragma foreign_keys=ON")
            self._create_schema()
            self._bind_or_validate_run(catalog)
            if self._plan_has_frozen_opening_suites():
                self.prepare_all_opening_suites()
            elif not _allow_test_corpus:
                raise ValueError(
                    "production tournament plan lacks pre-batch frozen opening suites"
                )
            self._mark_promotion_batch_running()
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> TournamentRunner:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            create table if not exists run_identity (
                singleton integer primary key check(singleton=1),
                format text not null,
                plan_json text not null,
                plan_digest text not null,
                environment_json text not null,
                environment_digest text not null,
                profile_catalog_json text not null,
                profile_catalog_digest text not null,
                schedule text not null,
                exclusion_count integer not null,
                exclusion_digest text not null,
                corpus_exclusion_json text not null,
                expansion_decision_json text,
                expansion_decision_digest text,
                promotion_registry_path text not null,
                promotion_batch_json text not null,
                promotion_batch_chain_digest text not null
            );
            create table if not exists opening_suites (
                domain text primary key,
                suite_json text not null,
                suite_digest text not null unique,
                position_hash_digest text not null,
                case_count integer not null
            );
            create table if not exists match_reports (
                stage text not null,
                matchup_id text not null,
                ordinal integer not null,
                resolved_spec_json text not null,
                resolved_spec_digest text not null,
                opening_reserve_digest text not null,
                suite_digest text not null,
                attempt_manifest_digest text not null,
                report_json text not null,
                report_digest text not null unique,
                pair_count integer not null,
                execution_elapsed_seconds real not null,
                replacement_attempts integer not null default 0,
                primary key(stage, matchup_id),
                unique(stage, ordinal)
            );
            create table if not exists match_attempts (
                stage text not null,
                matchup_id text not null,
                ordinal integer not null,
                attempt_index integer not null,
                unresolved_pair_indexes_json text not null,
                unresolved_pair_indexes_digest text not null,
                lane_suite_digest text not null,
                subset_suite_json text not null,
                subset_suite_digest text not null,
                config_json text not null,
                config_digest text not null,
                report_json text not null,
                report_digest text not null unique,
                execution_elapsed_seconds real not null,
                primary key(stage, matchup_id, attempt_index),
                unique(stage, ordinal, attempt_index)
            );
            create table if not exists artifacts (
                kind text not null,
                artifact_key text not null,
                payload_json text not null,
                payload_digest text not null unique,
                primary key(kind, artifact_key)
            );
            create table if not exists slot_resolutions (
                slot text primary key,
                effective_id text not null,
                artifact_digest text not null
            );
            """
        )
        self.connection.commit()

    def _bind_or_validate_run(self, catalog: Mapping[str, Any]) -> None:
        row = self.connection.execute(
            "select * from run_identity where singleton=1"
        ).fetchone()
        if (
            row is not None
            and self.schedule == "pending"
            and self.expansion_decision is None
            and row["schedule"] in {"base", "expanded"}
        ):
            raw_decision = row["expansion_decision_json"]
            if raw_decision is None:
                raise ValueError("frozen run schedule lacks its expansion decision")
            adopted = json.loads(raw_decision)
            validate_result_blind_expansion_decision(
                adopted, protocol_digest=self.protocol_digest
            )
            if (
                adopted["schedule"] != row["schedule"]
                or adopted["decision_digest"]
                != row["expansion_decision_digest"]
            ):
                raise ValueError("persisted expansion decision identity changed")
            self.schedule = str(adopted["schedule"])
            self.expansion_decision = copy.deepcopy(adopted)
            self.expansion_decision_digest = str(adopted["decision_digest"])
        expected = {
            "format": TOURNAMENT_RUNNER_FORMAT,
            "plan_json": _canonical_json(self.plan),
            "plan_digest": self.plan_digest,
            "environment_json": _canonical_json(self.environment.as_dict()),
            "environment_digest": self.environment.digest,
            "profile_catalog_json": _canonical_json(catalog),
            "profile_catalog_digest": self.profile_catalog_digest,
            "schedule": self.schedule,
            "exclusion_count": len(self._excluded_position_hashes),
            "exclusion_digest": self._exclusion_digest,
            "corpus_exclusion_json": _canonical_json(
                self.corpus_exclusion_artifact
            ),
            "expansion_decision_json": (
                None
                if self.expansion_decision is None
                else _canonical_json(self.expansion_decision)
            ),
            "expansion_decision_digest": self.expansion_decision_digest,
            "promotion_registry_path": str(self.promotion_registry_path),
            "promotion_batch_json": _canonical_json(self.promotion_batch),
            "promotion_batch_chain_digest": self.promotion_batch["chain_digest"],
        }
        if row is None:
            if self.expansion_decision is not None:
                raise ValueError(
                    "base/expanded schedule must be sealed from a pending run "
                    "in this database"
                )
            with self.connection:
                self.connection.execute(
                    """
                    insert into run_identity(
                        singleton,format,plan_json,plan_digest,environment_json,
                        environment_digest,profile_catalog_json,
                        profile_catalog_digest,schedule,exclusion_count,
                        exclusion_digest,corpus_exclusion_json,
                        expansion_decision_json,expansion_decision_digest,
                        promotion_registry_path,promotion_batch_json,
                        promotion_batch_chain_digest
                    ) values(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    tuple(expected.values()),
                )
            return
        actual = {key: row[key] for key in expected}
        if actual != expected:
            changed = [key for key in expected if actual.get(key) != expected[key]]
            raise ValueError(
                "cannot resume tournament: frozen identity changed: "
                + ", ".join(changed)
            )
        if self.expansion_decision is not None:
            self._validate_persisted_expansion_binding()

    def _validate_persisted_expansion_binding(self) -> None:
        if self.expansion_decision is None:
            raise ValueError("persisted expansion decision is missing")
        expected_envelope, expected_digest = _expansion_artifact_envelope(
            self.expansion_decision
        )
        artifact = self.connection.execute(
            "select payload_json,payload_digest from artifacts where "
            "kind='expansion-decision' and artifact_key='schedule'"
        ).fetchone()
        if (
            artifact is None
            or artifact["payload_json"] != _canonical_json(expected_envelope)
            or artifact["payload_digest"] != expected_digest
        ):
            raise ValueError("persisted expansion decision artifact changed")

        expected_timing = self.expansion_decision.get(
            "calibration_timing_evidence"
        )
        if not isinstance(expected_timing, list):
            raise ValueError("persisted calibration timing evidence is missing")
        actual_timing: list[dict[str, Any]] = []
        for spec in self._stage_specs("group")[:10]:
            job = self.build_match_job(spec, ordinal=self._global_ordinal(spec))
            row = self.connection.execute(
                "select * from match_reports where stage=? and matchup_id=?",
                (job.stage, job.matchup_id),
            ).fetchone()
            if row is None:
                raise ValueError("persisted calibration matchup is missing")
            report = self._validate_stored_report(row, job)
            actual_timing.append(
                self._calibration_timing_record(job, row, report)
            )
        if actual_timing != expected_timing:
            raise ValueError("persisted calibration timing evidence changed")

    def _mark_promotion_batch_running(self) -> None:
        connection = _connect_promotion_registry(self.promotion_registry_path)
        try:
            with connection:
                row = connection.execute(
                    "select plan_digest,status from promotion_batches "
                    "where batch_index=?",
                    (int(self.promotion_batch["batch_index"]),),
                ).fetchone()
                if row is None or row["plan_digest"] != self.plan_digest:
                    raise ValueError("promotion registry plan binding changed")
                if row["status"] == "plan-bound":
                    connection.execute(
                        "update promotion_batches set status='running' "
                        "where batch_index=?",
                        (int(self.promotion_batch["batch_index"]),),
                    )
                elif row["status"] not in {"running", "complete"}:
                    raise ValueError("promotion batch status is invalid")
        finally:
            connection.close()

    def _assert_promotion_batch_executable(
        self,
        connection: sqlite3.Connection | None = None,
        *,
        allow_complete: bool = False,
    ) -> None:
        owned = connection is None
        registry = (
            _connect_promotion_registry(self.promotion_registry_path)
            if connection is None
            else connection
        )
        try:
            row = registry.execute(
                "select plan_digest,status from promotion_batches where batch_index=?",
                (int(self.promotion_batch["batch_index"]),),
            ).fetchone()
            decision = registry.execute(
                "select 1 from promotion_decisions where batch_index=?",
                (int(self.promotion_batch["batch_index"]),),
            ).fetchone()
            running = row is not None and row["status"] == "running" and decision is None
            completed_read = (
                allow_complete and row is not None and row["status"] == "complete"
            )
            if (
                row is None
                or row["plan_digest"] != self.plan_digest
                or not (running or completed_read)
            ):
                raise ValueError(
                    "promotion batch is administratively closed; no more games may run"
                )
        finally:
            if owned:
                registry.close()

    def _maybe_mark_promotion_batch_complete(self) -> None:
        if self.persisted_match_count() != 240:
            return
        expected_artifacts = {
            "group-standing": 8,
            "knockout-advancement": 15,
        }
        for kind, expected_count in expected_artifacts.items():
            count = int(
                self.connection.execute(
                    "select count(*) from artifacts where kind=?", (kind,)
                ).fetchone()[0]
            )
            if count != expected_count:
                return
        final_rows: dict[str, sqlite3.Row] = {}
        for stage_name in ("challenger-final", "baseline-final"):
            specs = self._stage_specs(stage_name)
            if len(specs) != 1:
                raise ValueError("promotion final schedule is not canonical")
            spec = specs[0]
            job = self.build_match_job(spec, ordinal=self._global_ordinal(spec))
            row = self.connection.execute(
                "select * from match_reports where stage=? and matchup_id=?",
                (job.stage, job.matchup_id),
            ).fetchone()
            if row is None:
                return
            state = self._validated_attempt_state(job)
            if (
                not state["complete"]
                or state["manifest_digest"] != job.attempt_manifest_digest
            ):
                raise ValueError("promotion final attempt evidence is incomplete")
            self._validate_stored_report(row, job)
            final_rows[stage_name] = row
        authority = make_tournament_authority_artifact(
            self.connection,
            database_path=self.path,
            batch_index=int(self.promotion_batch["batch_index"]),
            final_rows=final_rows,
        )
        connection = _connect_promotion_registry(self.promotion_registry_path)
        try:
            connection.execute("begin immediate")
            batch_index = int(self.promotion_batch["batch_index"])
            row = connection.execute(
                "select plan_digest,status from promotion_batches "
                "where batch_index=?",
                (batch_index,),
            ).fetchone()
            if (
                row is None
                or row["plan_digest"] != self.plan_digest
                or row["status"] not in {"running", "complete"}
            ):
                raise ValueError("promotion batch completion identity changed")
            existing = connection.execute(
                "select authority_json,authority_digest,database_path,"
                "runner_state_digest from tournament_authorities "
                "where batch_index=?",
                (batch_index,),
            ).fetchone()
            expected = (
                _canonical_json(authority),
                authority["authority_digest"],
                authority["database_path"],
                authority["runner_state_digest"],
            )
            if existing is None:
                connection.execute(
                    "insert into tournament_authorities values(?,?,?,?,?)",
                    (batch_index, *expected),
                )
            elif tuple(existing) != expected:
                raise ValueError("trusted tournament authority changed")
            connection.execute(
                "update promotion_batches set status='complete' "
                "where batch_index=?",
                (batch_index,),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _suite_plan(self, domain: str) -> Mapping[str, Any]:
        matches = [
            item for item in self.plan["opening_suites"] if item.get("domain") == domain
        ]
        if len(matches) != 1:
            raise ValueError(f"opening domain is not uniquely frozen: {domain}")
        return matches[0]

    def _plan_has_frozen_opening_suites(self) -> bool:
        suites = self.plan.get("opening_suites")
        return isinstance(suites, list) and len(suites) == len(
            _OPENING_DOMAIN_COUNTS
        ) and all(
            isinstance(item, Mapping)
            and isinstance(item.get("attempt_lanes"), list)
            and len(item["attempt_lanes"]) == TOTAL_OPENING_ATTEMPTS
            and all(
                isinstance(lane, Mapping)
                and isinstance(lane.get("suite"), Mapping)
                and isinstance(lane.get("suite_digest"), str)
                for lane in item["attempt_lanes"]
            )
            and isinstance(item.get("reserve_digest"), str)
            and isinstance(item.get("position_hash_digest"), str)
            for item in suites
        )

    def _assert_environment_current(self) -> None:
        current = TournamentEnvironment.current(
            require_native=self.environment.loaded_native_source_identity is not None
        )
        if current.as_dict() != self.environment.as_dict():
            raise ValueError(
                "tournament source/native/runtime environment changed during the run"
            )

    def _validated_frozen_opening_reserves(self) -> list[dict[str, Any]]:
        registry = _connect_promotion_registry(self.promotion_registry_path)
        try:
            prior_hashes = {
                str(row["position_hash"])
                for row in registry.execute(
                    "select position_hash from opening_positions where batch_index<>?",
                    (int(self.promotion_batch["batch_index"]),),
                )
            }
        finally:
            registry.close()
        seen = set(self._excluded_position_hashes) | prior_hashes
        validated: list[dict[str, Any]] = []
        for expected, raw_reserve in zip(
            _OPENING_DOMAIN_COUNTS, self.plan["opening_suites"], strict=True
        ):
            domain, count = expected
            reserve = copy.deepcopy(dict(raw_reserve))
            if (
                reserve.get("format") != OPENING_RESERVE_FORMAT
                or reserve.get("domain") != domain
                or reserve.get("count") != count
                or reserve.get("retry_policy") != tournament_opening_retry_policy()
                or reserve.get("reserve_digest") != opening_reserve_digest(reserve)
            ):
                raise ValueError("frozen opening reserve identity changed")
            lanes = reserve.get("attempt_lanes")
            if not isinstance(lanes, list) or len(lanes) != TOTAL_OPENING_ATTEMPTS:
                raise ValueError("frozen opening reserve lane table changed")
            reserve_hashes: list[str] = []
            for attempt_index, lane in enumerate(lanes):
                if not isinstance(lane, Mapping):
                    raise ValueError("frozen opening attempt lane is invalid")
                lane_domain = f"{domain}|attempt|{attempt_index}"
                master_seed = int(lane["selection_master_seed"])
                nonce = int(lane["selection_nonce"])
                expected_base_seed = _opening_selection_seed(
                    protocol_digest=self.protocol_digest,
                    reservation_key=str(self.promotion_batch["reservation_key"]),
                    domain=lane_domain,
                    master_seed=master_seed,
                    nonce=0,
                )
                expected_seed = _opening_selection_seed(
                    protocol_digest=self.protocol_digest,
                    reservation_key=str(self.promotion_batch["reservation_key"]),
                    domain=lane_domain,
                    master_seed=master_seed,
                    nonce=nonce,
                )
                if (
                    lane.get("attempt_index") != attempt_index
                    or lane.get("selection_domain") != lane_domain
                    or lane.get("count") != count
                    or lane.get("base_seed") != expected_base_seed
                    or lane.get("seed") != expected_seed
                    or not 0 <= nonce <= 65_535
                ):
                    raise ValueError("frozen opening attempt derivation changed")
                for skipped_nonce in range(nonce):
                    skipped = build_seeded_opening_suite(
                        seed=_opening_selection_seed(
                            protocol_digest=self.protocol_digest,
                            reservation_key=str(
                                self.promotion_batch["reservation_key"]
                            ),
                            domain=lane_domain,
                            master_seed=master_seed,
                            nonce=skipped_nonce,
                        ),
                        count=count,
                        min_series=3,
                        max_series=6,
                        max_frontier_states=32,
                    )
                    if not seen.intersection(
                        case.state().position_hash for case in skipped.cases
                    ):
                        raise ValueError(
                            "frozen opening attempt skipped a collision-free nonce"
                        )
                suite = _suite_from_dict(lane["suite"])
                suite_digest = canonical_digest(
                    OPENING_SUITE_BINDING_FORMAT + "\0", suite.as_dict()
                )
                hashes = sorted(case.state().position_hash for case in suite.cases)
                position_digest = canonical_digest(
                    "spc-tournament-opening-position-hashes-v1\0", hashes
                )
                if (
                    suite.seed != expected_seed
                    or len(suite.cases) != count
                    or lane.get("suite_digest") != suite_digest
                    or lane.get("position_hash_digest") != position_digest
                    or canonical_json_bytes(lane.get("suite"))
                    != canonical_json_bytes(suite.as_dict())
                ):
                    raise ValueError("frozen opening attempt content changed")
                if seen.intersection(hashes):
                    raise ValueError(
                        f"pre-batch frozen opening lane {lane_domain} is not globally fresh"
                    )
                seen.update(hashes)
                reserve_hashes.extend(hashes)
            expected_position_digest = canonical_digest(
                "spc-tournament-opening-position-hashes-v2\0",
                sorted(reserve_hashes),
            )
            if (
                reserve.get("total_case_count") != count * TOTAL_OPENING_ATTEMPTS
                or reserve.get("position_hash_digest") != expected_position_digest
            ):
                raise ValueError("frozen opening reserve position binding changed")
            validated.append(reserve)
        return validated

    def _persist_prepared_opening_reserves(
        self, payloads: Sequence[Mapping[str, Any]]
    ) -> dict[str, str]:
        reserves: dict[str, TournamentOpeningReserve] = {}
        rows: list[tuple[str, str, str, str, int]] = []
        proposed: list[tuple[str, int, str, str, str]] = []
        selections: list[dict[str, Any]] = []
        batch_index = int(self.promotion_batch["batch_index"])
        seen = set(self._excluded_position_hashes)
        for expected, raw in zip(_OPENING_DOMAIN_COUNTS, payloads, strict=True):
            domain, count = expected
            payload = copy.deepcopy(dict(raw))
            if (
                payload.get("format") != OPENING_RESERVE_FORMAT
                or payload.get("domain") != domain
                or payload.get("count") != count
                or payload.get("retry_policy") != tournament_opening_retry_policy()
                or payload.get("reserve_digest") != opening_reserve_digest(payload)
            ):
                raise ValueError("prepared opening reserve identity changed")
            lanes: list[SeededOpeningSuite] = []
            hashes: list[str] = []
            selection_lanes: list[dict[str, Any]] = []
            for attempt_index, lane_raw in enumerate(payload["attempt_lanes"]):
                lane = dict(lane_raw)
                suite = _suite_from_dict(lane["suite"])
                digest = canonical_digest(
                    OPENING_SUITE_BINDING_FORMAT + "\0", suite.as_dict()
                )
                lane_hashes = sorted(
                    case.state().position_hash for case in suite.cases
                )
                if (
                    lane.get("attempt_index") != attempt_index
                    or lane.get("count") != count
                    or len(suite.cases) != count
                    or lane.get("suite_digest") != digest
                    or lane.get("position_hash_digest")
                    != canonical_digest(
                        "spc-tournament-opening-position-hashes-v1\0", lane_hashes
                    )
                ):
                    raise ValueError("prepared opening attempt binding changed")
                if seen.intersection(lane_hashes):
                    raise ValueError("prepared opening reserves are not globally fresh")
                seen.update(lane_hashes)
                hashes.extend(lane_hashes)
                lanes.append(suite)
                lane_domain = f"{domain}|attempt|{attempt_index}"
                proposed.extend(
                    (
                        position_hash,
                        batch_index,
                        lane_domain,
                        digest,
                        self.plan_digest,
                    )
                    for position_hash in lane_hashes
                )
                selection_lanes.append(
                    {
                        "attempt_index": attempt_index,
                        "selection_domain": lane.get("selection_domain"),
                        "selection_nonce": lane.get("selection_nonce"),
                        "selected_seed": suite.seed,
                        "suite_digest": digest,
                        "position_hash_digest": lane["position_hash_digest"],
                        "case_count": len(suite.cases),
                    }
                )
            position_digest = canonical_digest(
                "spc-tournament-opening-position-hashes-v2\0", sorted(hashes)
            )
            if (
                payload.get("total_case_count") != len(hashes)
                or payload.get("position_hash_digest") != position_digest
            ):
                raise ValueError("prepared opening reserve hash set changed")
            reserve_digest = str(payload["reserve_digest"])
            reserve = TournamentOpeningReserve(
                domain=domain,
                count=count,
                lanes=tuple(lanes),
                reserve_digest=reserve_digest,
                position_hash_digest=position_digest,
            )
            reserves[domain] = reserve
            rows.append(
                (
                    domain,
                    _canonical_json(payload),
                    reserve_digest,
                    position_digest,
                    len(hashes),
                )
            )
            selections.append(
                {
                    "domain": domain,
                    "reserve_digest": reserve_digest,
                    "position_hash_digest": position_digest,
                    "case_count": len(hashes),
                    "attempt_lanes": selection_lanes,
                }
            )
        proposed.sort()
        registry = _connect_promotion_registry(self.promotion_registry_path)
        try:
            registry.execute("begin immediate")
            batch = registry.execute(
                "select plan_digest,status from promotion_batches where batch_index=?",
                (batch_index,),
            ).fetchone()
            if (
                batch is None
                or batch["plan_digest"] != self.plan_digest
                or batch["status"] not in {"plan-bound", "running", "complete"}
            ):
                raise ValueError("promotion registry opening reserve binding changed")
            prior = {
                str(row["position_hash"])
                for row in registry.execute(
                    "select position_hash from opening_positions where batch_index<>?",
                    (batch_index,),
                )
            }
            if prior.intersection(item[0] for item in proposed):
                raise ValueError("prepared opening was consumed by another batch")
            current = [
                tuple(row)
                for row in registry.execute(
                    "select position_hash,batch_index,domain,suite_digest,plan_digest "
                    "from opening_positions where batch_index=? order by position_hash",
                    (batch_index,),
                )
            ]
            if current and current != proposed:
                raise ValueError("promotion registry opening reserve changed")
            if not current:
                registry.executemany(
                    "insert into opening_positions values(?,?,?,?,?)", proposed
                )
            registry.commit()
        except BaseException:
            registry.rollback()
            raise
        finally:
            registry.close()
        deterministic = {
            "format": OPENING_SUITE_SELECTION_FORMAT,
            "tournament_plan_digest": self.plan_digest,
            "corpus_exclusion_digest": self._exclusion_digest,
            "promotion_batch_chain_digest": self.promotion_batch["chain_digest"],
            "selections": selections,
            "result_inputs": "none",
        }
        selection_digest = canonical_digest(
            "spc-tournament-opening-selection-v2\0", deterministic
        )
        envelope = {**deterministic, "selection_digest": selection_digest}
        with self.connection:
            existing_rows = [
                tuple(row)
                for row in self.connection.execute(
                    "select domain,suite_json,suite_digest,position_hash_digest,"
                    "case_count from opening_suites order by domain"
                )
            ]
            expected_rows = sorted(rows)
            if existing_rows and existing_rows != expected_rows:
                raise ValueError("persisted all-reserve opening freeze changed")
            if not existing_rows:
                self.connection.executemany(
                    "insert into opening_suites values(?,?,?,?,?)", expected_rows
                )
            artifact = self.connection.execute(
                "select payload_json,payload_digest from artifacts where "
                "kind='opening-suite-selection' and artifact_key='all'"
            ).fetchone()
            expected_json = _canonical_json(envelope)
            if artifact is None:
                self.connection.execute(
                    "insert into artifacts values('opening-suite-selection','all',?,?)",
                    (expected_json, selection_digest),
                )
            elif (
                artifact["payload_json"] != expected_json
                or artifact["payload_digest"] != selection_digest
            ):
                raise ValueError("persisted opening reserve manifest changed")
        self._opening_suites_ready = reserves
        return {domain: reserve.reserve_digest for domain, reserve in reserves.items()}

    def prepare_opening_reserve(self, domain: str) -> TournamentOpeningReserve:
        self._assert_environment_current()
        self.prepare_all_opening_suites()
        assert self._opening_suites_ready is not None
        try:
            return self._opening_suites_ready[domain]
        except KeyError as error:
            raise ValueError(f"opening domain is not frozen: {domain}") from error

    def prepare_opening_suite(self, domain: str) -> SeededOpeningSuite:
        """Compatibility view of the primary lane; execution uses the reserve."""

        return self.prepare_opening_reserve(domain).lanes[0]

    def prepare_all_opening_suites(self) -> dict[str, str]:
        if self._opening_suites_ready is not None:
            return {
                domain: reserve.reserve_digest
                for domain, reserve in self._opening_suites_ready.items()
            }
        if self._plan_has_frozen_opening_suites():
            return self._prepare_frozen_opening_suites()
        if not self._allow_test_corpus:
            raise ValueError("production tournament plan lacks frozen opening reserves")
        # Tests may exercise the runner without a promotion preparation phase.
        # Even there, all three lanes are materialized before any match executes.
        registry = _connect_promotion_registry(self.promotion_registry_path)
        try:
            prior_hashes = sorted(
                str(row["position_hash"])
                for row in registry.execute(
                    "select position_hash from opening_positions where batch_index<>?",
                    (int(self.promotion_batch["batch_index"]),),
                )
            )
        finally:
            registry.close()
        cache_key = canonical_digest(
            "spc-test-opening-reserve-cache-v1\0",
            {
                "source_fingerprint": model.ENGINE_SOURCE_FINGERPRINT,
                "corpus_exclusion_digest": self._exclusion_digest,
                "prior_position_hashes": prior_hashes,
            },
        )
        cached = _TEST_OPENING_RESERVE_CACHE.get(cache_key)
        if cached is not None:
            return self._persist_prepared_opening_reserves(
                copy.deepcopy(cached)
            )
        generated: list[dict[str, Any]] = []
        seen = set(self._excluded_position_hashes) | set(prior_hashes)
        for item in self.plan["opening_suites"]:
            lanes: list[dict[str, Any]] = []
            hashes: list[str] = []
            for lane_spec in item["attempt_lanes"]:
                attempt_index = int(lane_spec["attempt_index"])
                base_seed = int(
                    canonical_digest(
                        "spc-test-opening-reserve-seed-v1\0",
                        {
                            "cache_key": cache_key,
                            "domain": item["domain"],
                            "attempt_index": attempt_index,
                            "nonce": 0,
                        },
                    )[:16],
                    16,
                )
                nonce = 0
                while True:
                    selected_seed = int(
                        canonical_digest(
                            "spc-test-opening-reserve-seed-v1\0",
                            {
                                "cache_key": cache_key,
                                "domain": item["domain"],
                                "attempt_index": attempt_index,
                                "nonce": nonce,
                            },
                        )[:16],
                        16,
                    )
                    suite = build_seeded_opening_suite(
                        seed=selected_seed,
                        count=int(item["count"]),
                        min_series=int(item["min_series"]),
                        max_series=int(item["max_series"]),
                        max_frontier_states=int(item["max_frontier_states"]),
                    )
                    lane_hashes = [
                        case.state().position_hash for case in suite.cases
                    ]
                    if not seen.intersection(lane_hashes):
                        break
                    nonce += 1
                    if nonce > 65_535:
                        raise ValueError(
                            "test opening reserve collision retry cap exhausted"
                        )
                seen.update(lane_hashes)
                hashes.extend(lane_hashes)
                lanes.append(
                    {
                        **dict(lane_spec),
                        "base_seed": base_seed,
                        "seed": selected_seed,
                        "selection_nonce": nonce,
                        "suite": suite.as_dict(),
                        "suite_digest": canonical_digest(
                            OPENING_SUITE_BINDING_FORMAT + "\0", suite.as_dict()
                        ),
                        "position_hash_digest": canonical_digest(
                            "spc-tournament-opening-position-hashes-v1\0",
                            sorted(lane_hashes),
                        ),
                    }
                )
            reserve = {
                **dict(item),
                "format": OPENING_RESERVE_FORMAT,
                "attempt_lanes": lanes,
                "total_case_count": len(hashes),
                "position_hash_digest": canonical_digest(
                    "spc-tournament-opening-position-hashes-v2\0", sorted(hashes)
                ),
            }
            reserve["reserve_digest"] = opening_reserve_digest(reserve)
            generated.append(reserve)
        _TEST_OPENING_RESERVE_CACHE[cache_key] = tuple(
            copy.deepcopy(generated)
        )
        return self._persist_prepared_opening_reserves(generated)

    def _prepare_frozen_opening_suites(self) -> dict[str, str]:
        self._assert_environment_current()
        return self._persist_prepared_opening_reserves(
            self._validated_frozen_opening_reserves()
        )

    def _reserve_global_frozen_opening_positions(
        self, suites: Mapping[str, SeededOpeningSuite]
    ) -> None:
        batch_index = int(self.promotion_batch["batch_index"])
        proposed: list[tuple[str, int, str, str, str]] = []
        for domain, suite in suites.items():
            digest = canonical_digest(
                OPENING_SUITE_BINDING_FORMAT + "\0", suite.as_dict()
            )
            proposed.extend(
                (
                    case.state().position_hash,
                    batch_index,
                    domain,
                    digest,
                    self.plan_digest,
                )
                for case in suite.cases
            )
        proposed.sort()
        connection = _connect_promotion_registry(self.promotion_registry_path)
        try:
            connection.execute("begin immediate")
            batch = connection.execute(
                "select plan_digest,status from promotion_batches where batch_index=?",
                (batch_index,),
            ).fetchone()
            if (
                batch is None
                or batch["plan_digest"] != self.plan_digest
                or batch["status"] not in {"plan-bound", "running", "complete"}
            ):
                raise ValueError("promotion registry all-suite plan binding changed")
            prior = {
                str(row["position_hash"])
                for row in connection.execute(
                    "select position_hash from opening_positions where batch_index<>?",
                    (batch_index,),
                )
            }
            if prior.intersection(row[0] for row in proposed):
                raise ValueError("pre-batch frozen opening was consumed by another batch")
            current = [
                tuple(row)
                for row in connection.execute(
                    "select position_hash,batch_index,domain,suite_digest,plan_digest "
                    "from opening_positions where batch_index=? order by position_hash",
                    (batch_index,),
                )
            ]
            if current and current != proposed:
                raise ValueError("promotion registry all-suite reservation changed")
            if not current:
                connection.executemany(
                    "insert into opening_positions values(?,?,?,?,?)", proposed
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _validate_suite_binding(
        self, domain: str, suite: SeededOpeningSuite, digest: str
    ) -> None:
        spec = self._suite_plan(domain)
        verify_seeded_opening_suite(suite)
        actual_digest = canonical_digest(
            "spc-tournament-opening-suite-v1\0", suite.as_dict()
        )
        if digest != actual_digest:
            raise ValueError("persisted opening suite digest mismatch")
        if (
            suite.seed != spec["seed"]
            or len(suite.cases) != spec["count"]
            or suite.min_series != spec["min_series"]
            or suite.max_series != spec["max_series"]
            or suite.max_frontier_states != spec["max_frontier_states"]
        ):
            raise ValueError("opening suite does not match its frozen domain")

    def _reserve_global_opening_positions(
        self, domain: str, suite: SeededOpeningSuite, suite_digest: str
    ) -> None:
        batch_index = int(self.promotion_batch["batch_index"])
        hashes = sorted(case.state().position_hash for case in suite.cases)
        connection = _connect_promotion_registry(self.promotion_registry_path)
        try:
            connection.execute("begin immediate")
            batch = connection.execute(
                "select plan_digest from promotion_batches where batch_index=?",
                (batch_index,),
            ).fetchone()
            if batch is None or batch["plan_digest"] != self.plan_digest:
                raise ValueError("promotion registry plan binding changed")
            for position_hash in hashes:
                existing = connection.execute(
                    "select * from opening_positions where position_hash=?",
                    (position_hash,),
                ).fetchone()
                expected = (
                    position_hash,
                    batch_index,
                    domain,
                    suite_digest,
                    self.plan_digest,
                )
                if existing is None:
                    connection.execute(
                        "insert into opening_positions values(?,?,?,?,?)", expected
                    )
                elif tuple(existing) != expected:
                    raise ValueError(
                        "fresh tournament opening was consumed by another batch"
                    )
            registered = [
                str(row["position_hash"])
                for row in connection.execute(
                    "select position_hash from opening_positions "
                    "where batch_index=? and domain=? and suite_digest=? "
                    "order by position_hash",
                    (batch_index, domain, suite_digest),
                )
            ]
            if registered != hashes:
                raise ValueError("promotion registry opening reservation changed")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _validate_global_opening_hashes(self) -> None:
        seen = set(self._excluded_position_hashes)
        for row in self.connection.execute(
            "select domain,suite_json,position_hash_digest,case_count "
            "from opening_suites order by domain"
        ):
            suite = _suite_from_dict(json.loads(row["suite_json"]))
            hashes = sorted(case.state().position_hash for case in suite.cases)
            if len(hashes) != row["case_count"] or row[
                "position_hash_digest"
            ] != canonical_digest(
                "spc-tournament-opening-position-hashes-v1\0", hashes
            ):
                raise ValueError("persisted opening position hash digest mismatch")
            overlap = seen & set(hashes)
            if overlap:
                raise ValueError(
                    f"opening suite {row['domain']} is not globally fresh"
                )
            seen.update(hashes)
            self._reserve_global_opening_positions(
                str(row["domain"]), suite, canonical_digest(
                    "spc-tournament-opening-suite-v1\0", suite.as_dict()
                )
            )

    def _stage_specs(self, stage: str) -> list[Mapping[str, Any]]:
        try:
            key = next(key for key, value in _STAGE_NAMES.items() if value == stage)
        except StopIteration as error:
            raise ValueError(f"unknown tournament stage: {stage}") from error
        return list(self.plan["matchups"][key])

    def _resolve_slot(self, slot: str) -> str:
        if slot.startswith("spc-effective-"):
            if slot not in self.profiles:
                raise ValueError("match spec names a profile outside the catalog")
            return slot
        row = self.connection.execute(
            "select effective_id from slot_resolutions where slot=?", (slot,)
        ).fetchone()
        if row is None:
            raise ValueError(f"tournament slot is not resolved yet: {slot}")
        return str(row["effective_id"])

    def _config_for_suite(
        self,
        *,
        spec: Mapping[str, Any],
        suite: SeededOpeningSuite,
        pairs: int,
        seed: int,
    ) -> StrengthMatchConfig:
        limits = spec.get("limits")
        if not isinstance(limits, Mapping):
            raise ValueError("match spec has no deterministic limits")
        return StrengthMatchConfig(
            pairs=pairs,
            seed=seed,
            search_depth=int(limits["depth_series"]),
            max_series_per_node=int(
                limits["branch_cap_complete_series_per_node"]
            ),
            max_generation_positions=int(
                limits["max_work_positions_per_search"]
            ),
            max_game_work_positions=int(limits["max_game_work_positions"]),
            emergency_max_series=limits["emergency_max_series"],
            opening_suite_version=suite.version,
            opening_case_ids=tuple(case.case_id for case in suite.cases),
        )

    def _attempt_seed(self, job: TournamentMatchJob, attempt_index: int) -> int:
        payload = canonical_json_bytes(
            {
                "format": MATCH_ATTEMPT_FORMAT,
                "tournament_plan_digest": self.plan_digest,
                "stage": job.stage,
                "matchup_id": job.matchup_id,
                "match_seed": int(job.resolved_spec["match_seed"]),
                "attempt_index": attempt_index,
            }
        )
        return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")

    def _attempt_job(
        self,
        job: TournamentMatchJob,
        *,
        attempt_index: int,
        unresolved_pair_indexes: Sequence[int],
    ) -> TournamentMatchJob:
        if not 0 <= attempt_index < TOTAL_OPENING_ATTEMPTS:
            raise ValueError("opening attempt index is outside the frozen reserve")
        indexes = tuple(int(index) for index in unresolved_pair_indexes)
        if (
            not indexes
            or tuple(sorted(set(indexes))) != indexes
            or indexes[0] < 0
            or indexes[-1] >= job.pair_count
        ):
            raise ValueError("opening attempt unresolved pair indexes are invalid")
        lane = job.opening_reserve.lanes[attempt_index]
        case_ids = tuple(lane.cases[index].case_id for index in indexes)
        subset = subset_seeded_opening_suite(lane, case_ids)
        subset_digest = canonical_digest(
            OPENING_SUITE_BINDING_FORMAT + "\0", subset.as_dict()
        )
        resolved = {
            **dict(job.resolved_spec),
            "opening_suite_digest": subset_digest,
            "opening_attempt_index": attempt_index,
            "opening_unresolved_pair_indexes": list(indexes),
            "opening_attempt_manifest_digest": None,
        }
        config = self._config_for_suite(
            spec=job.resolved_spec,
            suite=subset,
            pairs=len(indexes),
            seed=self._attempt_seed(job, attempt_index),
        )
        return TournamentMatchJob(
            ordinal=job.ordinal,
            stage=job.stage,
            matchup_id=job.matchup_id,
            pair_count=len(indexes),
            resolved_spec=resolved,
            first_effective_id=job.first_effective_id,
            second_effective_id=job.second_effective_id,
            first_profile=job.first_profile,
            second_profile=job.second_profile,
            opening_reserve=job.opening_reserve,
            opening_suite=subset,
            opening_suite_digest=subset_digest,
            attempt_manifest=None,
            attempt_manifest_digest=None,
            config=config,
        )

    def _validated_attempt_state(
        self, job: TournamentMatchJob
    ) -> dict[str, Any]:
        rows = list(
            self.connection.execute(
                "select * from match_attempts where stage=? and matchup_id=? "
                "order by attempt_index",
                (job.stage, job.matchup_id),
            )
        )
        unresolved = tuple(range(job.pair_count))
        selected: dict[int, dict[str, Any]] = {}
        evidence: list[dict[str, Any]] = []
        for expected_attempt, row in enumerate(rows):
            if expected_attempt >= TOTAL_OPENING_ATTEMPTS:
                raise ValueError("persisted opening attempts exceed the frozen cap")
            if int(row["attempt_index"]) != expected_attempt:
                raise ValueError("persisted opening attempts are not contiguous")
            expected_job = self._attempt_job(
                job,
                attempt_index=expected_attempt,
                unresolved_pair_indexes=unresolved,
            )
            unresolved_payload = list(unresolved)
            unresolved_digest = canonical_digest(
                "spc-tournament-unresolved-pairs-v1\0", unresolved_payload
            )
            subset_digest = canonical_digest(
                OPENING_SUITE_BINDING_FORMAT + "\0",
                expected_job.opening_suite.as_dict(),
            )
            config_payload = expected_job.config.as_dict()
            if (
                int(row["ordinal"]) != job.ordinal
                or row["unresolved_pair_indexes_json"]
                != _canonical_json(unresolved_payload)
                or row["unresolved_pair_indexes_digest"] != unresolved_digest
                or row["lane_suite_digest"]
                != canonical_digest(
                    OPENING_SUITE_BINDING_FORMAT + "\0",
                    job.opening_reserve.lanes[expected_attempt].as_dict(),
                )
                or row["subset_suite_json"]
                != _canonical_json(expected_job.opening_suite.as_dict())
                or row["subset_suite_digest"] != subset_digest
                or row["config_json"] != _canonical_json(config_payload)
                or row["config_digest"]
                != canonical_digest("spc-strength-match-config-v1\0", config_payload)
                or not math.isfinite(float(row["execution_elapsed_seconds"]))
                or float(row["execution_elapsed_seconds"]) <= 0
            ):
                raise ValueError("persisted opening attempt identity changed")
            report = json.loads(row["report_json"])
            normalized = self._normalize_raw_report(report, expected_job)
            report_digest = canonical_digest(
                "spc-tournament-match-attempt-v1\0", normalized
            )
            if (
                row["report_json"] != _canonical_json(normalized)
                or row["report_digest"] != report_digest
            ):
                raise ValueError("persisted opening attempt report changed")
            self._validate_attempt_full_traces(normalized, report_digest)
            technical = normalized["summary"].get("technical_failures")
            if not isinstance(technical, Mapping) or any(
                int(technical.get(field, -1)) != 0
                for field in (
                    "total_profile_failures",
                    "unattributed_worker_failures",
                    "unattributed_match_limit_failures",
                )
            ):
                raise ValueError(
                    "opening attempts fail closed on engine, worker, or shared "
                    "technical failures"
                )
            incomplete_terminal_counts: dict[str, int] = {}
            for game in normalized["games"]:
                trace = game.get("trace")
                if (
                    not isinstance(trace, list)
                    or game.get("series_played")
                    != sum(
                        item.get("played") is True
                        for item in trace
                        if isinstance(item, Mapping)
                    )
                ):
                    raise ValueError("opening attempt trace evidence is invalid")
                if (
                    game.get("engine_failure_profile_id") is not None
                    or game.get("error") is not None
                ):
                    raise ValueError(
                        "opening attempts cannot retry attributed or worker failures"
                    )
                if game.get("result") == "*":
                    reason = str(game.get("terminal_reason", ""))
                    if (
                        reason != "manual-adjudication-pending"
                        or game.get("decisive_profile_id") is not None
                    ):
                        raise ValueError(
                            "only clean manual-adjudication-pending games are "
                            "eligible for an opening replacement"
                        )
                    incomplete_terminal_counts[reason] = (
                        incomplete_terminal_counts.get(reason, 0) + 1
                    )
            logical_by_case = {
                job.opening_reserve.lanes[expected_attempt].cases[index].case_id: index
                for index in unresolved
            }
            next_unresolved: list[int] = []
            completed_this_attempt: list[int] = []
            games = normalized["games"]
            for local_index, pair in enumerate(normalized["pairs"]):
                case_id = str(pair["opening_case_id"])
                logical_index = logical_by_case.get(case_id)
                if logical_index is None:
                    raise ValueError("opening attempt used a case outside its frozen lane")
                paired_games = games[local_index * 2 : local_index * 2 + 2]
                complete = pair.get("result") != "incomplete" and all(
                    game.get("result") != "*" for game in paired_games
                )
                if complete:
                    if logical_index in selected:
                        raise ValueError("completed logical pair was retried")
                    selected[logical_index] = {
                        "attempt_index": expected_attempt,
                        "case_id": case_id,
                        "pair": copy.deepcopy(pair),
                        "games": copy.deepcopy(paired_games),
                    }
                    completed_this_attempt.append(logical_index)
                else:
                    next_unresolved.append(logical_index)
            if sorted(completed_this_attempt + next_unresolved) != list(unresolved):
                raise ValueError("opening attempt did not cover every unresolved pair")
            next_unresolved.sort()
            evidence.append(
                {
                    "attempt_index": expected_attempt,
                    "unresolved_pair_indexes_in": list(unresolved),
                    "completed_pair_indexes": sorted(completed_this_attempt),
                    "unresolved_pair_indexes_out": next_unresolved,
                    "lane_suite_digest": row["lane_suite_digest"],
                    "subset_suite_digest": subset_digest,
                    "config_digest": row["config_digest"],
                    "attempt_report_digest": report_digest,
                    "executed_game_records": len(games),
                    "incomplete_terminal_evidence": {
                        "terminal_reason_counts": dict(
                            sorted(incomplete_terminal_counts.items())
                        ),
                        "candidate_attributed_failures": 0,
                        "reference_attributed_failures": 0,
                        "unattributed_worker_failures": 0,
                        "unattributed_match_limit_failures": 0,
                        "error_records": 0,
                    },
                    "execution_elapsed_seconds": float(
                        row["execution_elapsed_seconds"]
                    ),
                }
            )
            unresolved = tuple(next_unresolved)
            if not unresolved and expected_attempt != len(rows) - 1:
                raise ValueError("opening attempts continued after every pair completed")
        deterministic_manifest = {
            "format": "spc-tournament-opening-attempt-manifest-v1",
            "tournament_plan_digest": self.plan_digest,
            "stage": job.stage,
            "matchup_id": job.matchup_id,
            "ordinal": job.ordinal,
            "opening_reserve_digest": job.opening_reserve.reserve_digest,
            "retry_policy": tournament_opening_retry_policy(),
            "attempts": evidence,
            "selected_pairs": [
                {
                    "logical_pair_index": logical_index,
                    "attempt_index": selected[logical_index]["attempt_index"],
                    "opening_case_id": selected[logical_index]["case_id"],
                }
                for logical_index in sorted(selected)
            ],
            "unresolved_pair_indexes": list(unresolved),
            "competitive_result_fields_used_for_attempt_ordering": [],
            "retry_trigger_fields": ["pair_completion", "game_completion"],
        }
        manifest_digest = canonical_digest(
            "spc-tournament-opening-attempt-manifest-v1\0",
            deterministic_manifest,
        )
        return {
            "rows": rows,
            "selected": selected,
            "unresolved": unresolved,
            "complete": not unresolved and len(selected) == job.pair_count,
            "exhausted": bool(unresolved)
            and len(rows) == TOTAL_OPENING_ATTEMPTS,
            "manifest": {
                **deterministic_manifest,
                "attempt_manifest_digest": manifest_digest,
            },
            "manifest_digest": manifest_digest,
        }

    def _validate_attempt_full_traces(
        self, report: Mapping[str, Any], report_digest: str
    ) -> None:
        """Replays every attempt game before completion can select a lane.

        Incomplete manual-adjudication records remain honest evidence only when
        their played continuation and final PFEN replay exactly.  The cache is
        process-local and content-addressed; a resumed runner starts empty and
        therefore replays every stored attempt at least once.
        """

        if report_digest in self._replay_validated_attempt_digests:
            return
        replayed = attach_replay_verified_full_traces(report)
        evidence = replayed.get("full_trace_evidence")
        games = report.get("games")
        if (
            not isinstance(evidence, Mapping)
            or not isinstance(games, list)
            or int(evidence.get("completed_games", -1))
            + int(evidence.get("incomplete_games", -1))
            != len(games)
            or evidence.get(
                "neutral_prefix_and_engine_continuation_replayed"
            )
            is not True
        ):
            raise ValueError("opening attempt full-trace evidence is invalid")
        self._replay_validated_attempt_digests.add(report_digest)

    def build_match_job(
        self, spec: Mapping[str, Any], *, ordinal: int
    ) -> TournamentMatchJob:
        first = self._resolve_slot(str(spec["first_slot"]))
        second = self._resolve_slot(str(spec["second_slot"]))
        if first == second:
            raise ValueError("resolved tournament matchup repeats one profile")
        reserve = self.prepare_opening_reserve(str(spec["opening_domain"]))
        if (
            spec.get("opening_reserve_digest") is not None
            and spec.get("opening_reserve_digest") != reserve.reserve_digest
        ):
            raise ValueError("match spec opening reserve binding changed")
        pair_count = _scheduled_pair_count(
            spec, schedule="base" if self.schedule == "pending" else self.schedule
        )
        if pair_count > reserve.count:
            raise ValueError("match pair count exceeds its frozen opening reserve")
        primary_lane = reserve.lanes[0]
        suite = compose_seeded_opening_suite(
            [
                (primary_lane, primary_lane.cases[index].case_id)
                for index in range(pair_count)
            ],
            seed=int(spec["opening_seed"]),
        )
        suite_digest = canonical_digest(
            OPENING_SUITE_BINDING_FORMAT + "\0", suite.as_dict()
        )
        decision_bound_stages = {
            "quarterfinal",
            "semifinal",
            "challenger-final",
            "baseline-final",
        }
        spec_expansion_digest = (
            self.expansion_decision_digest
            if str(spec["stage"]) in decision_bound_stages
            else None
        )
        if str(spec["stage"]) in decision_bound_stages and self.schedule == "pending":
            raise ValueError(
                "late-round jobs require the result-blind schedule decision"
            )
        resolved = {
            **dict(spec),
            "tournament_ordinal": ordinal,
            "resolved_first_effective_id": first,
            "resolved_second_effective_id": second,
            "opening_reserve_digest": reserve.reserve_digest,
            "opening_suite_digest": suite_digest,
            "opening_attempt_manifest_digest": None,
            "environment_digest": self.environment.digest,
            "runtime_identity_digest": self.environment.runtime_identity_digest,
            "profile_catalog_digest": self.profile_catalog_digest,
            "corpus_exclusion_digest": self._exclusion_digest,
            "corpus_exclusion_authority": self.corpus_exclusion_authority,
            "promotion_batch_index": int(self.promotion_batch["batch_index"]),
            "promotion_batch_chain_digest": self.promotion_batch["chain_digest"],
            "promotion_registry_id": self.promotion_batch["registry_id"],
            "promotion_registry_authority": self.promotion_registry_authority,
            "promotion_batch_artifact": copy.deepcopy(self.promotion_batch),
            "promotion_batch_artifact_digest": self.promotion_batch[
                "artifact_digest"
            ],
            "expansion_decision_digest": spec_expansion_digest,
        }
        config = self._config_for_suite(
            spec=spec,
            suite=suite,
            pairs=pair_count,
            seed=int(spec["match_seed"]),
        )
        base_job = TournamentMatchJob(
            ordinal=ordinal,
            stage=str(spec["stage"]),
            matchup_id=str(spec["matchup_id"]),
            pair_count=pair_count,
            resolved_spec=resolved,
            first_effective_id=first,
            second_effective_id=second,
            first_profile=self.profiles[first],
            second_profile=self.profiles[second],
            opening_reserve=reserve,
            opening_suite=suite,
            opening_suite_digest=suite_digest,
            attempt_manifest=None,
            attempt_manifest_digest=None,
            config=config,
        )
        state = self._validated_attempt_state(base_job)
        if not state["complete"]:
            return base_job
        selections = [
            (
                reserve.lanes[int(state["selected"][index]["attempt_index"])],
                str(state["selected"][index]["case_id"]),
            )
            for index in range(pair_count)
        ]
        selected_suite = compose_seeded_opening_suite(
            selections,
            seed=int(spec["opening_seed"]),
        )
        selected_digest = canonical_digest(
            OPENING_SUITE_BINDING_FORMAT + "\0", selected_suite.as_dict()
        )
        manifest = copy.deepcopy(state["manifest"])
        manifest_digest = str(state["manifest_digest"])
        selected_resolved = {
            **resolved,
            "opening_suite_digest": selected_digest,
            "opening_attempt_manifest_digest": manifest_digest,
        }
        selected_config = self._config_for_suite(
            spec=spec,
            suite=selected_suite,
            pairs=pair_count,
            seed=int(spec["match_seed"]),
        )
        return TournamentMatchJob(
            ordinal=ordinal,
            stage=str(spec["stage"]),
            matchup_id=str(spec["matchup_id"]),
            pair_count=pair_count,
            resolved_spec=selected_resolved,
            first_effective_id=first,
            second_effective_id=second,
            first_profile=self.profiles[first],
            second_profile=self.profiles[second],
            opening_reserve=reserve,
            opening_suite=selected_suite,
            opening_suite_digest=selected_digest,
            attempt_manifest=manifest,
            attempt_manifest_digest=manifest_digest,
            config=selected_config,
        )

    def _report_environment_binding(
        self, job: TournamentMatchJob
    ) -> dict[str, Any]:
        return {
            "format": "spc-tournament-match-execution-v2",
            "environment": self.environment.as_dict(),
            "environment_digest": self.environment.digest,
            "profile_catalog_digest": self.profile_catalog_digest,
            "opening_reserve_digest": job.opening_reserve.reserve_digest,
            "opening_suite_digest": job.opening_suite_digest,
            "opening_attempt_manifest_digest": job.attempt_manifest_digest,
            "corpus_exclusion_digest": self._exclusion_digest,
            "corpus_exclusion_authority": self.corpus_exclusion_authority,
            "promotion_batch_index": int(self.promotion_batch["batch_index"]),
            "promotion_batch_chain_digest": self.promotion_batch["chain_digest"],
            "promotion_registry_id": self.promotion_batch["registry_id"],
            "promotion_registry_authority": self.promotion_registry_authority,
            "promotion_batch_artifact_digest": self.promotion_batch[
                "artifact_digest"
            ],
            "expansion_decision_digest": job.resolved_spec[
                "expansion_decision_digest"
            ],
            "requested_workers": REQUESTED_WORKERS,
            "authoritative_timing_fields": [
                "opening_attempts.attempts[].execution_elapsed_seconds"
            ],
            "promotion_effect": "none",
        }

    def _normalize_raw_report(
        self, report: Mapping[str, Any], job: TournamentMatchJob
    ) -> dict[str, Any]:
        payload = copy.deepcopy(dict(report))
        if payload.get("format") != STRENGTH_REPORT_FORMAT:
            raise ValueError("match runner returned an unsupported report")
        required_fields = {
            "format",
            "report_id",
            "engine",
            "candidate",
            "reference",
            "config",
            "opening_suite",
            "resources",
            "execution",
            "selected_openings",
            "summary",
            "pairs",
            "games",
            "claim_scope",
        }
        optional_fields = {
            "created_at",
            "opening_attempts",
            "tournament_execution",
        }
        if not required_fields <= set(payload) or set(payload) - (
            required_fields | optional_fields
        ):
            raise ValueError("match report top-level fields are not canonical")
        incoming_tournament_execution = payload.pop(
            "tournament_execution", None
        )
        incoming_opening_attempts = payload.pop("opening_attempts", None)
        if job.attempt_manifest is None:
            if incoming_opening_attempts is not None:
                raise ValueError("opening attempt evidence is not valid for this wave")
        elif canonical_json_bytes(incoming_opening_attempts) != canonical_json_bytes(
            job.attempt_manifest
        ):
            raise ValueError("opening attempt evidence changed")
        expected_engine = {
            "version": self.environment.engine_version,
            "source_fingerprint": self.environment.source_fingerprint,
            "runtime": dict(self.environment.runtime),
        }
        if payload.get("engine") != expected_engine:
            raise ValueError("match report engine/source/runtime identity is stale")
        if payload.get("candidate") != job.first_profile.as_dict() or payload.get(
            "reference"
        ) != job.second_profile.as_dict():
            raise ValueError("match report profile payload changed")
        if payload.get("config") != job.config.as_dict():
            raise ValueError("match runner changed the frozen match config")
        if canonical_json_bytes(payload.get("opening_suite")) != canonical_json_bytes(
            job.opening_suite.as_dict()
        ):
            raise ValueError("match runner changed the fresh opening suite")
        expected_jobs = _build_jobs(
            job.first_profile,
            job.second_profile,
            job.config,
            job.opening_suite,
        )
        if payload.get("report_id") != expected_jobs[0].run_id:
            raise ValueError("match report id is not canonical")
        games = payload.get("games")
        if not isinstance(games, list) or len(games) != len(expected_jobs):
            raise ValueError("match report does not contain every frozen game job")
        for game, expected in zip(games, expected_jobs, strict=True):
            if not isinstance(game, Mapping) or (
                game.get("job_key") != expected.job_key
                or game.get("run_id") != expected.run_id
                or game.get("opening_index") != expected.opening_index
                or game.get("opening_case_id") != expected.opening.case_id
                or game.get("opening_suite_version")
                != expected.opening_suite_version
                or game.get("seed") != expected.seed
                or game.get("white_profile_id")
                != expected.white_profile.profile_id
                or game.get("black_profile_id")
                != expected.black_profile.profile_id
                or game.get("start_pfen") != expected.opening.state().pfen
                or game.get("generation") != expected.generation
                or game.get("stage") != expected.stage
            ):
                if isinstance(game, Mapping) and game.get("generation") != expected.generation:
                    raise ValueError("match report game generation is not canonical")
                if isinstance(game, Mapping) and game.get("stage") != expected.stage:
                    raise ValueError("match report game stage is not canonical")
                raise ValueError("match report game jobs are not canonical")
            if canonical_json_bytes(game.get("opening")) != canonical_json_bytes(
                expected.opening.as_dict()
            ):
                raise ValueError("match report game opening is not canonical")
        selected_openings = payload.get("selected_openings")
        expected_openings = [
            expected.opening.as_dict() for expected in expected_jobs[::2]
        ]
        if canonical_json_bytes(selected_openings) != canonical_json_bytes(
            expected_openings
        ):
            raise ValueError("match report selected openings are not canonical")
        records: list[GameRecord] = []
        game_record_fields = set(GameRecord.__dataclass_fields__)
        for game in games:
            if set(game) != game_record_fields | {"opening"}:
                raise ValueError("match report game payload fields are not canonical")
            trace = game.get("trace")
            if not isinstance(trace, Sequence) or isinstance(trace, (str, bytes)):
                raise ValueError("match report game trace is not canonical")
            record_payload = {
                name: copy.deepcopy(game[name]) for name in game_record_fields
            }
            record_payload["trace"] = tuple(
                dict(row) if isinstance(row, Mapping) else row for row in trace
            )
            records.append(GameRecord(**record_payload))
        canonical_summary, canonical_pairs = _summarize(
            records, job.first_profile, job.second_profile
        )
        if canonical_json_bytes(payload.get("pairs")) != canonical_json_bytes(
            list(canonical_pairs)
        ):
            raise ValueError("match report pair payload is not canonical")
        payload["summary"] = canonical_summary
        payload["pairs"] = list(canonical_pairs)
        if canonical_json_bytes(payload.get("claim_scope")) != canonical_json_bytes(
            _canonical_strength_claim_scope()
        ):
            raise ValueError("match report claim scope is not canonical")
        resources = payload.get("resources")
        expected_workers = min(REQUESTED_WORKERS, len(expected_jobs))
        if (
            not isinstance(resources, Mapping)
            or resources.get("workers") != expected_workers
        ):
            raise ValueError("match did not execute with the frozen 16 workers")
        execution = payload.get("execution")
        if not isinstance(execution, Mapping) or execution.get(
            "result_order"
        ) != "opening-pair-then-color-swap":
            raise ValueError("match report result ordering is not canonical")
        # Wall clocks and host resource discovery are operational telemetry, not
        # authoritative simulation inputs or standings data.
        payload.pop("created_at", None)
        payload["resources"] = {"workers": expected_workers}
        payload["execution"] = {
            "result_order": "opening-pair-then-color-swap"
        }
        if job.attempt_manifest is not None:
            payload["opening_attempts"] = copy.deepcopy(job.attempt_manifest)
        expected_tournament_execution = self._report_environment_binding(job)
        if (
            incoming_tournament_execution is not None
            and incoming_tournament_execution != expected_tournament_execution
        ):
            raise ValueError("match report tournament execution is not canonical")
        payload["tournament_execution"] = expected_tournament_execution
        return payload

    def _bind_report(
        self, report: Mapping[str, Any], job: TournamentMatchJob
    ) -> dict[str, Any]:
        self._assert_environment_current()
        normalized = self._normalize_raw_report(report, job)
        effective_by_profile_id = {
            job.first_profile.profile_id: job.first_effective_id,
            job.second_profile.profile_id: job.second_effective_id,
        }
        bound = bind_frozen_match_report(
            normalized,
            match_spec=job.resolved_spec,
            protocol_digest=self.protocol_digest,
            tournament_plan_digest=self.plan_digest,
            effective_by_profile_id=effective_by_profile_id,
        )
        validate_frozen_match_report(
            bound,
            match_spec=job.resolved_spec,
            protocol_digest=self.protocol_digest,
            tournament_plan_digest=self.plan_digest,
            effective_by_profile_id=effective_by_profile_id,
        )
        return bound

    def _validate_stored_report(
        self, row: sqlite3.Row, job: TournamentMatchJob
    ) -> dict[str, Any]:
        self._assert_environment_current()
        if (
            job.attempt_manifest_digest is None
            or row["ordinal"] != job.ordinal
            or row["resolved_spec_json"] != _canonical_json(job.resolved_spec)
            or row["resolved_spec_digest"]
            != canonical_digest(
                "spc-resolved-match-spec-v1\0", job.resolved_spec
            )
            or row["opening_reserve_digest"]
            != job.opening_reserve.reserve_digest
            or row["suite_digest"] != job.opening_suite_digest
            or row["attempt_manifest_digest"]
            != job.attempt_manifest_digest
            or row["pair_count"] != job.pair_count
            or row["replacement_attempts"]
            != len(job.attempt_manifest["attempts"]) - 1
            or not math.isfinite(float(row["execution_elapsed_seconds"]))
            or float(row["execution_elapsed_seconds"]) <= 0
        ):
            raise ValueError("persisted match job identity changed")
        report = json.loads(row["report_json"])
        if report.get("bound_report_digest") != row["report_digest"]:
            raise ValueError("persisted match report digest changed")
        raw = _raw_report(report)
        renormalized = self._normalize_raw_report(raw, job)
        if canonical_json_bytes(renormalized) != canonical_json_bytes(raw):
            raise ValueError("persisted raw match identity is not canonical")
        execution = report.get("tournament_execution")
        if execution != self._report_environment_binding(job):
            raise ValueError("persisted match execution identity changed")
        effective_by_profile_id = {
            job.first_profile.profile_id: job.first_effective_id,
            job.second_profile.profile_id: job.second_effective_id,
        }
        validate_frozen_match_report(
            report,
            match_spec=job.resolved_spec,
            protocol_digest=self.protocol_digest,
            tournament_plan_digest=self.plan_digest,
            effective_by_profile_id=effective_by_profile_id,
        )
        return report

    def _run_attempt_wave(
        self,
        job: TournamentMatchJob,
        *,
        attempt_index: int,
        unresolved_pair_indexes: Sequence[int],
        match_runner: MatchRunner,
    ) -> dict[str, Any]:
        """Executes and CAS-seals one immutable whole-pair attempt wave."""

        attempt_job = self._attempt_job(
            job,
            attempt_index=attempt_index,
            unresolved_pair_indexes=unresolved_pair_indexes,
        )
        self._assert_promotion_batch_executable()
        started = time.perf_counter()
        raw = match_runner(
            attempt_job.first_profile,
            attempt_job.second_profile,
            config=attempt_job.config,
            opening_cases=attempt_job.opening_suite,
            requested_workers=REQUESTED_WORKERS,
        )
        normalized = self._normalize_raw_report(raw, attempt_job)
        report_digest = canonical_digest(
            "spc-tournament-match-attempt-v1\0", normalized
        )
        self._validate_attempt_full_traces(normalized, report_digest)
        elapsed = time.perf_counter() - started
        if not math.isfinite(elapsed) or elapsed <= 0:
            raise ValueError("opening attempt execution elapsed time is invalid")
        unresolved_payload = list(int(index) for index in unresolved_pair_indexes)
        lane = job.opening_reserve.lanes[attempt_index]
        values = (
            job.stage,
            job.matchup_id,
            job.ordinal,
            attempt_index,
            _canonical_json(unresolved_payload),
            canonical_digest(
                "spc-tournament-unresolved-pairs-v1\0", unresolved_payload
            ),
            canonical_digest(
                OPENING_SUITE_BINDING_FORMAT + "\0", lane.as_dict()
            ),
            _canonical_json(attempt_job.opening_suite.as_dict()),
            attempt_job.opening_suite_digest,
            _canonical_json(attempt_job.config.as_dict()),
            canonical_digest(
                "spc-strength-match-config-v1\0", attempt_job.config.as_dict()
            ),
            _canonical_json(normalized),
            report_digest,
            elapsed,
        )
        registry = _connect_promotion_registry(self.promotion_registry_path)
        try:
            registry.execute("begin immediate")
            self._assert_promotion_batch_executable(registry)
            with self.connection:
                existing = self.connection.execute(
                    "select * from match_attempts where stage=? and matchup_id=? "
                    "and attempt_index=?",
                    (job.stage, job.matchup_id, attempt_index),
                ).fetchone()
                if existing is None:
                    self.connection.execute(
                        """
                        insert into match_attempts(
                            stage,matchup_id,ordinal,attempt_index,
                            unresolved_pair_indexes_json,
                            unresolved_pair_indexes_digest,lane_suite_digest,
                            subset_suite_json,subset_suite_digest,config_json,
                            config_digest,report_json,report_digest,
                            execution_elapsed_seconds
                        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        values,
                    )
                else:
                    immutable_fields = (
                        "stage",
                        "matchup_id",
                        "ordinal",
                        "attempt_index",
                        "unresolved_pair_indexes_json",
                        "unresolved_pair_indexes_digest",
                        "lane_suite_digest",
                        "subset_suite_json",
                        "subset_suite_digest",
                        "config_json",
                        "config_digest",
                        "report_json",
                        "report_digest",
                    )
                    if (
                        tuple(existing[field] for field in immutable_fields)
                        != values[:-1]
                    ):
                        raise ValueError(
                            "concurrent opening attempt CAS conflict"
                        )
            registry.commit()
        except BaseException:
            registry.rollback()
            raise
        finally:
            registry.close()
        return self._validated_attempt_state(job)

    def _assemble_completed_attempt_report(
        self, job: TournamentMatchJob, state: Mapping[str, Any]
    ) -> dict[str, Any]:
        if (
            not state.get("complete")
            or job.attempt_manifest_digest is None
            or job.attempt_manifest != state.get("manifest")
        ):
            raise ValueError("cannot assemble a match before every pair completes")
        expected_jobs = _build_jobs(
            job.first_profile,
            job.second_profile,
            job.config,
            job.opening_suite,
        )
        source_by_case_and_white: dict[tuple[str, str], Mapping[str, Any]] = {}
        for selected in state["selected"].values():
            for game in selected["games"]:
                key = (str(game["opening_case_id"]), str(game["white_profile_id"]))
                if key in source_by_case_and_white:
                    raise ValueError("attempt evidence repeats a selected color game")
                source_by_case_and_white[key] = game
        records: list[GameRecord] = []
        for expected in expected_jobs:
            source = source_by_case_and_white.get(
                (expected.opening.case_id, expected.white_profile.profile_id)
            )
            if source is None:
                raise ValueError("attempt evidence is missing a selected color game")
            records.append(
                GameRecord(
                    job_key=expected.job_key,
                    run_id=expected.run_id,
                    generation=expected.generation,
                    stage=expected.stage,
                    opening_index=expected.opening_index,
                    opening_case_id=expected.opening.case_id,
                    opening_suite_version=expected.opening_suite_version,
                    seed=expected.seed,
                    white_profile_id=str(source["white_profile_id"]),
                    black_profile_id=str(source["black_profile_id"]),
                    result=str(source["result"]),
                    terminal_reason=str(source["terminal_reason"]),
                    decisive_profile_id=source.get("decisive_profile_id"),
                    engine_failure_profile_id=source.get(
                        "engine_failure_profile_id"
                    ),
                    start_pfen=expected.opening.state().pfen,
                    final_pfen=str(source["final_pfen"]),
                    series_played=int(source["series_played"]),
                    trace=tuple(copy.deepcopy(source["trace"])),
                    error=source.get("error"),
                )
            )
        summary, pairs = _summarize(
            records, job.first_profile, job.second_profile
        )
        if summary.get("incomplete_pairs") != 0:
            raise ValueError("selected opening attempts are not all complete")
        opening_by_id = {case.case_id: case for case in job.opening_suite.cases}
        return {
            "format": STRENGTH_REPORT_FORMAT,
            "report_id": expected_jobs[0].run_id,
            "engine": {
                "version": self.environment.engine_version,
                "source_fingerprint": self.environment.source_fingerprint,
                "runtime": dict(self.environment.runtime),
            },
            "candidate": job.first_profile.as_dict(),
            "reference": job.second_profile.as_dict(),
            "config": job.config.as_dict(),
            "opening_suite": job.opening_suite.as_dict(),
            "resources": {"workers": min(REQUESTED_WORKERS, len(expected_jobs))},
            "execution": {"result_order": "opening-pair-then-color-swap"},
            "selected_openings": [
                expected.opening.as_dict() for expected in expected_jobs[::2]
            ],
            "summary": summary,
            "pairs": list(pairs),
            "games": [
                _game_payload(record, opening_by_id[record.opening_case_id])
                for record in records
            ],
            "claim_scope": _canonical_strength_claim_scope(),
            "opening_attempts": copy.deepcopy(job.attempt_manifest),
        }

    def _run_job(
        self, job: TournamentMatchJob, match_runner: MatchRunner
    ) -> dict[str, Any]:
        self._assert_environment_current()
        row = self.connection.execute(
            "select * from match_reports where stage=? and matchup_id=?",
            (job.stage, job.matchup_id),
        ).fetchone()
        if row is not None:
            spec = next(
                spec
                for spec in self._stage_specs(job.stage)
                if str(spec["matchup_id"]) == job.matchup_id
            )
            job = self.build_match_job(spec, ordinal=job.ordinal)
            self._assert_promotion_batch_executable(allow_complete=True)
            stored = self._validate_stored_report(row, job)
            if job.stage == "baseline-final":
                self._maybe_mark_promotion_batch_complete()
            return stored
        state = self._validated_attempt_state(job)
        while not state["complete"]:
            if state["exhausted"]:
                raise OpeningReplacementExhaustedError(
                    f"opening replacement cap exhausted for {job.stage}/"
                    f"{job.matchup_id}; unresolved logical pairs: "
                    f"{list(state['unresolved'])}"
                )
            state = self._run_attempt_wave(
                job,
                attempt_index=len(state["rows"]),
                unresolved_pair_indexes=state["unresolved"],
                match_runner=match_runner,
            )
        spec = next(
            spec
            for spec in self._stage_specs(job.stage)
            if str(spec["matchup_id"]) == job.matchup_id
        )
        job = self.build_match_job(spec, ordinal=job.ordinal)
        state = self._validated_attempt_state(job)
        if (
            not state["complete"]
            or job.attempt_manifest_digest != state["manifest_digest"]
        ):
            raise ValueError("opening attempt completion identity changed")
        raw = self._assemble_completed_attempt_report(job, state)
        bound = self._bind_report(raw, job)
        execution_elapsed_seconds = sum(
            float(attempt["execution_elapsed_seconds"])
            for attempt in state["manifest"]["attempts"]
        )
        digest = str(bound["bound_report_digest"])
        registry = _connect_promotion_registry(self.promotion_registry_path)
        try:
            registry.execute("begin immediate")
            self._assert_promotion_batch_executable(registry)
            committed_state = self._validated_attempt_state(job)
            if (
                not committed_state["complete"]
                or committed_state["manifest_digest"]
                != job.attempt_manifest_digest
            ):
                raise ValueError("opening attempt evidence changed before report seal")
            with self.connection:
                existing = self.connection.execute(
                    "select * from match_reports where stage=? and matchup_id=?",
                    (job.stage, job.matchup_id),
                ).fetchone()
                if existing is None:
                    self.connection.execute(
                        """
                        insert into match_reports(
                            stage,matchup_id,ordinal,resolved_spec_json,
                            resolved_spec_digest,opening_reserve_digest,
                            suite_digest,attempt_manifest_digest,report_json,
                            report_digest,pair_count,execution_elapsed_seconds,
                            replacement_attempts
                        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            job.stage,
                            job.matchup_id,
                            job.ordinal,
                            _canonical_json(job.resolved_spec),
                            canonical_digest(
                                "spc-resolved-match-spec-v1\0", job.resolved_spec
                            ),
                            job.opening_reserve.reserve_digest,
                            job.opening_suite_digest,
                            job.attempt_manifest_digest,
                            _canonical_json(bound),
                            digest,
                            job.pair_count,
                            execution_elapsed_seconds,
                            len(state["rows"]) - 1,
                        ),
                    )
                else:
                    # A second process may have committed while this match ran.
                    stored = self._validate_stored_report(existing, job)
                    registry.commit()
                    return stored
            registry.commit()
        except BaseException:
            registry.rollback()
            raise
        finally:
            registry.close()
        if job.stage == "baseline-final":
            self._maybe_mark_promotion_batch_complete()
        return bound

    def run_matchups(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        match_runner: MatchRunner = run_strength_match,
    ) -> tuple[dict[str, Any], ...]:
        if not specs:
            return ()
        canonical_specs = {
            (str(spec["stage"]), str(spec["matchup_id"])): spec
            for stage in _STAGE_NAMES.values()
            for spec in self._stage_specs(stage)
        }
        identities = [
            (str(spec.get("stage", "")), str(spec.get("matchup_id", "")))
            for spec in specs
        ]
        if len(set(identities)) != len(identities) or any(
            identity not in canonical_specs
            or dict(canonical_specs[identity]) != dict(spec)
            for identity, spec in zip(identities, specs, strict=True)
        ):
            raise ValueError("requested matchup set is not a unique plan subset")
        global_ordinals = {
            (str(spec["stage"]), str(spec["matchup_id"])): ordinal
            for ordinal, spec in enumerate(
                (
                    spec
                    for stage in _STAGE_NAMES.values()
                    for spec in self._stage_specs(stage)
                )
            )
        }
        ordered = sorted(specs, key=lambda spec: global_ordinals[
            (str(spec["stage"]), str(spec["matchup_id"]))
        ])
        if self.schedule == "pending" and any(
            str(spec["stage"]) != "group"
            or global_ordinals[(str(spec["stage"]), str(spec["matchup_id"]))]
            >= 10
            for spec in ordered
        ):
            raise ValueError(
                "pending schedule permits only the first 10 calibration matchups"
            )
        reports = []
        for spec in ordered:
            identity = (str(spec["stage"]), str(spec["matchup_id"]))
            job = self.build_match_job(
                spec, ordinal=global_ordinals[identity]
            )
            reports.append(self._run_job(job, match_runner))
        return tuple(reports)

    def run_stage(
        self,
        stage: str,
        *,
        match_runner: MatchRunner = run_strength_match,
    ) -> tuple[dict[str, Any], ...]:
        return self.run_matchups(
            self._stage_specs(stage), match_runner=match_runner
        )

    def _calibration_timing_record(
        self,
        job: TournamentMatchJob,
        row: sqlite3.Row,
        report: Mapping[str, Any],
    ) -> dict[str, Any]:
        summary = validate_frozen_match_report(
            report,
            match_spec=job.resolved_spec,
            protocol_digest=self.protocol_digest,
            tournament_plan_digest=self.plan_digest,
            effective_by_profile_id={
                job.first_profile.profile_id: job.first_effective_id,
                job.second_profile.profile_id: job.second_effective_id,
            },
        )
        raw_summary = report.get("summary")
        technical = (
            raw_summary.get("technical_failures")
            if isinstance(raw_summary, Mapping)
            else None
        )
        if not isinstance(technical, Mapping) or any(
            technical.get(field) != 0
            for field in (
                "total_profile_failures",
                "unattributed_worker_failures",
                "unattributed_match_limit_failures",
            )
        ):
            raise ValueError(
                "expansion timing rejects technical or worker failures"
            )
        games = report.get("games")
        attempts = report.get("opening_attempts")
        if (
            job.stage != "group"
            or job.pair_count != 50
            or summary["scheduled_pairs"] != 50
            or not isinstance(games, list)
            or len(games) != 100
            or not isinstance(attempts, Mapping)
        ):
            raise ValueError(
                "expansion timing requires ten completed canonical matchups"
            )
        for game in games:
            if (
                game.get("engine_failure_profile_id") is not None
                or game.get("error") is not None
            ):
                raise ValueError(
                    "expansion timing rejects technical or worker failures"
                )
            trace = game.get("trace")
            if not isinstance(trace, list) or game.get("series_played") != sum(
                row.get("played") is True
                for row in trace
                if isinstance(row, Mapping)
            ):
                raise ValueError(
                    "calibration game series count does not match its replayed trace"
                )
            result = game.get("result")
            expected_decisive = (
                game.get("white_profile_id")
                if result == "1-0"
                else game.get("black_profile_id")
                if result == "0-1"
                else None
            )
            if game.get("decisive_profile_id") != expected_decisive:
                raise ValueError(
                    "calibration game decisive profile contradicts its result"
                )
            if result == "*":
                raise ValueError("sealed calibration games cannot be incomplete")
        attempt_rows = attempts.get("attempts")
        if not isinstance(attempt_rows, list) or not attempt_rows:
            raise ValueError("calibration opening attempt evidence is missing")
        executed_game_records = sum(
            int(item["executed_game_records"]) for item in attempt_rows
        )
        execution_wall_seconds = sum(
            float(item["execution_elapsed_seconds"]) for item in attempt_rows
        )
        if (
            not 100 <= executed_game_records <= 300
            or executed_game_records % 2
            or execution_wall_seconds
            != float(row["execution_elapsed_seconds"])
        ):
            raise ValueError("calibration replacement timing evidence changed")
        return {
            "stage": job.stage,
            "matchup_id": job.matchup_id,
            "ordinal": job.ordinal,
            "pair_records": job.pair_count,
            "selected_game_records": len(games),
            "executed_game_records": executed_game_records,
            "execution_wall_seconds": execution_wall_seconds,
        }

    def freeze_result_blind_expansion(self) -> dict[str, Any]:
        """Seals base/expanded after 1,000 selected logical calibration games.

        Every replacement wave contributes elapsed time and executed-record
        count. No result, score, or WDL field enters the timing choice.
        """

        if self.schedule != "pending" or self.expansion_decision is not None:
            raise ValueError("the result-blind schedule decision is already frozen")
        calibration_specs = self._stage_specs("group")[:10]
        timing_evidence: list[dict[str, Any]] = []
        expected_identities: set[tuple[str, str]] = set()
        for spec in calibration_specs:
            job = self.build_match_job(spec, ordinal=self._global_ordinal(spec))
            expected_identities.add((job.stage, job.matchup_id))
            row = self.connection.execute(
                "select * from match_reports where stage=? and matchup_id=?",
                (job.stage, job.matchup_id),
            ).fetchone()
            if row is None:
                raise ValueError(
                    "expansion requires the first 10 completed group matchups"
                )
            report = self._validate_stored_report(row, job)
            timing_evidence.append(
                self._calibration_timing_record(job, row, report)
            )
        persisted = {
            (str(row["stage"]), str(row["matchup_id"]))
            for row in self.connection.execute(
                "select stage,matchup_id from match_reports"
            )
        }
        if persisted != expected_identities:
            raise ValueError(
                "expansion must be sealed before any non-calibration matchup"
            )
        decision = choose_result_blind_expansion(
            protocol_digest=self.protocol_digest,
            calibration_timing_evidence=timing_evidence,
            fixed_overhead_reserve_seconds=(
                FROZEN_EXPANSION_OVERHEAD_RESERVE_SECONDS
            ),
        )
        validate_result_blind_expansion_decision(
            decision, protocol_digest=self.protocol_digest
        )
        envelope, artifact_digest = _expansion_artifact_envelope(decision)
        with self.connection:
            identity = self.connection.execute(
                "select schedule,expansion_decision_json from run_identity "
                "where singleton=1"
            ).fetchone()
            if (
                identity is None
                or identity["schedule"] != "pending"
                or identity["expansion_decision_json"] is not None
            ):
                raise ValueError("run schedule changed while freezing expansion")
            self.connection.execute(
                "insert into artifacts values('expansion-decision','schedule',?,?)",
                (_canonical_json(envelope), artifact_digest),
            )
            self.connection.execute(
                "update run_identity set schedule=?,expansion_decision_json=?,"
                "expansion_decision_digest=? where singleton=1",
                (
                    decision["schedule"],
                    _canonical_json(decision),
                    decision["decision_digest"],
                ),
            )
        self.schedule = str(decision["schedule"])
        self.expansion_decision = copy.deepcopy(decision)
        self.expansion_decision_digest = str(decision["decision_digest"])
        return copy.deepcopy(decision)

    def _put_artifact(
        self,
        *,
        kind: str,
        key: str,
        payload: Mapping[str, Any],
        slot_updates: Mapping[str, str] | None = None,
    ) -> None:
        deterministic = dict(payload)
        digest = canonical_digest(
            f"spc-tournament-runner-{kind}-v1\0", deterministic
        )
        envelope = {**deterministic, "runner_artifact_digest": digest}
        with self.connection:
            row = self.connection.execute(
                "select payload_json,payload_digest from artifacts "
                "where kind=? and artifact_key=?",
                (kind, key),
            ).fetchone()
            encoded = _canonical_json(envelope)
            if row is None:
                self.connection.execute(
                    "insert into artifacts values(?,?,?,?)",
                    (kind, key, encoded, digest),
                )
            elif row["payload_json"] != encoded or row["payload_digest"] != digest:
                raise ValueError("persisted advancement artifact changed")
            for slot, effective_id in sorted((slot_updates or {}).items()):
                existing = self.connection.execute(
                    "select effective_id,artifact_digest from slot_resolutions "
                    "where slot=?",
                    (slot,),
                ).fetchone()
                if existing is None:
                    self.connection.execute(
                        "insert into slot_resolutions values(?,?,?)",
                        (slot, effective_id, digest),
                    )
                elif (
                    existing["effective_id"] != effective_id
                    or existing["artifact_digest"] != digest
                ):
                    raise ValueError("persisted tournament slot resolution changed")

    def derive_group_standings(self) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for group_id, raw_members in self.plan["groups"].items():
            member_effective_ids = tuple(
                str(item["effective_id"]) for item in raw_members
            )
            profile_ids = tuple(
                self.profiles[value].profile_id for value in member_effective_ids
            )
            effective_by_profile_id = {
                self.profiles[value].profile_id: value
                for value in member_effective_ids
            }
            specs = [
                spec
                for spec in self._stage_specs("group")
                if spec["opening_domain"] == f"{group_id}-openings"
            ]
            reports: list[dict[str, Any]] = []
            resolved_specs: list[Mapping[str, Any]] = []
            for spec in specs:
                ordinal = self._global_ordinal(spec)
                job = self.build_match_job(spec, ordinal=ordinal)
                row = self.connection.execute(
                    "select * from match_reports where stage=? and matchup_id=?",
                    (job.stage, job.matchup_id),
                ).fetchone()
                if row is None:
                    raise ValueError(f"{group_id} is missing a frozen match report")
                reports.append(self._validate_stored_report(row, job))
                resolved_specs.append(job.resolved_spec)
            standing = rank_group(
                group_id,
                profile_ids,
                reports,
                protocol_digest=self.protocol_digest,
                tournament_plan_digest=self.plan_digest,
                match_specs=tuple(resolved_specs),
                effective_by_profile_id=effective_by_profile_id,
            )
            if standing.get("promotion_effect") != "none":
                raise ValueError("group standings cannot promote a profile")
            slots = {
                f"{group_id}-rank-1": standing["advancing_effective_ids"][0],
                f"{group_id}-rank-2": standing["advancing_effective_ids"][1],
            }
            payload = {
                "format": "spc-runner-group-standing-v1",
                "protocol_digest": self.protocol_digest,
                "tournament_plan_digest": self.plan_digest,
                "environment_digest": self.environment.digest,
                "group": standing,
                "promotion_effect": "none",
            }
            self._put_artifact(
                kind="group-standing",
                key=str(group_id),
                payload=payload,
                slot_updates=slots,
            )
            results[str(group_id)] = payload
        return results

    def _global_ordinal(self, target: Mapping[str, Any]) -> int:
        identity = (str(target["stage"]), str(target["matchup_id"]))
        for ordinal, spec in enumerate(
            spec
            for stage in _STAGE_NAMES.values()
            for spec in self._stage_specs(stage)
        ):
            if (str(spec["stage"]), str(spec["matchup_id"])) == identity:
                return ordinal
        raise ValueError("match spec is outside the plan")

    def derive_knockout_winners(
        self, stage: str
    ) -> dict[str, dict[str, Any]]:
        if stage not in _KNOCKOUT_STAGES:
            raise ValueError("only knockout stages can derive an advancing winner")
        validation_rank = {
            str(item["effective_id"]): index
            for index, item in enumerate(
                self.plan["survivors_in_validation_rank_order"]
            )
        }
        results: dict[str, dict[str, Any]] = {}
        for spec in self._stage_specs(stage):
            job = self.build_match_job(spec, ordinal=self._global_ordinal(spec))
            row = self.connection.execute(
                "select * from match_reports where stage=? and matchup_id=?",
                (stage, job.matchup_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"knockout report is missing: {job.matchup_id}")
            report = self._validate_stored_report(row, job)
            effective_by_profile_id = {
                job.first_profile.profile_id: job.first_effective_id,
                job.second_profile.profile_id: job.second_effective_id,
            }
            seed_effective_ids = sorted(
                (job.first_effective_id, job.second_effective_id),
                key=lambda value: (validation_rank[value], value),
            )
            seed_profile_ids = tuple(
                self.profiles[value].profile_id for value in seed_effective_ids
            )
            winner = derive_verified_knockout_winner(
                report,
                match_spec=job.resolved_spec,
                protocol_digest=self.protocol_digest,
                tournament_plan_digest=self.plan_digest,
                effective_by_profile_id=effective_by_profile_id,
                preregistered_seed_order=seed_profile_ids,
            )
            if winner.get("promotion_effect") is not None:
                raise ValueError("knockout advancement must not promote a profile")
            payload = {
                "format": "spc-runner-knockout-advancement-v1",
                "protocol_digest": self.protocol_digest,
                "tournament_plan_digest": self.plan_digest,
                "environment_digest": self.environment.digest,
                "winner": winner,
                "promotion_effect": "none",
            }
            slot = f"{job.matchup_id}-winner"
            self._put_artifact(
                kind="knockout-advancement",
                key=job.matchup_id,
                payload=payload,
                slot_updates={slot: str(winner["winner_effective_id"])},
            )
            results[job.matchup_id] = payload
        return results

    def retry_incomplete_pairs(
        self,
        stage: str,
        matchup_id: str,
        *,
        match_runner: MatchRunner = run_strength_match,
    ) -> dict[str, Any]:
        del stage, matchup_id, match_runner
        raise ValueError(
            "post-seal retry is disabled; v2 runs replacement waves before "
            "the canonical match report is sealed"
        )

    def run_all(
        self,
        *,
        match_runner: MatchRunner = run_strength_match,
    ) -> dict[str, Any]:
        """Runs every frozen match without score-based stopping or promotion."""

        if self.schedule == "pending":
            raise ValueError(
                "freeze the result-blind schedule after its first 10 completed "
                "calibration matchups"
            )
        self.prepare_all_opening_suites()
        self.run_stage("group", match_runner=match_runner)
        self.derive_group_standings()
        for stage in _KNOCKOUT_STAGES:
            self.run_stage(stage, match_runner=match_runner)
            self.derive_knockout_winners(stage)
        baseline_reports = self.run_stage(
            "baseline-final", match_runner=match_runner
        )
        return {
            "format": "spc-tournament-run-complete-v1",
            "protocol_digest": self.protocol_digest,
            "tournament_plan_digest": self.plan_digest,
            "environment_digest": self.environment.digest,
            "profile_catalog_digest": self.profile_catalog_digest,
            "corpus_exclusion_digest": self._exclusion_digest,
            "promotion_batch_chain_digest": self.promotion_batch[
                "chain_digest"
            ],
            "expansion_decision_digest": self.expansion_decision_digest,
            "schedule": tournament_schedule_summary(
                self.plan, schedule=self.schedule
            ),
            "persisted_matchups": self.persisted_match_count(),
            "baseline_final_report_digest": baseline_reports[0][
                "bound_report_digest"
            ],
            "promotion_effect": "none",
        }

    def persisted_match_count(self) -> int:
        return int(
            self.connection.execute("select count(*) from match_reports").fetchone()[0]
        )

    def state_digest(self) -> str:
        return str(
            tournament_database_state_artifact(self.connection)[
                "runner_state_digest"
            ]
        )
