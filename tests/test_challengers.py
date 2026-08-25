from __future__ import annotations

import json
from pathlib import Path

import pytest

from scottish_progressive import challengers
from scottish_progressive.challengers import (
    CACHED_SCORER_ID,
    CHALLENGER_CACHE_FORMAT,
    SAMPLE_WEIGHT_SCALE,
    STAGE_B_REGULARIZATION,
    TARGET_EVALUATION_POINTS,
    CachedProxyRow,
    ChallengerProxyCache,
    QuadraticCachedScorer,
    _build_proxy_cache,
    _load_checkpoint,
    _materialize_catalog_and_plan,
    _reconcile_stage_a_ledger,
    _require_cache_input_identity,
)
from scottish_progressive.cli import build_parser
from scottish_progressive.fast_training import CachedFeatures, FEATURE_NAMES
from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT, ENGINE_VERSION
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.selfplay_training import (
    FULLGAME_CORPUS_METHOD,
    SelfPlayCorpus,
    SelfPlaySample,
)
from scottish_progressive.tournament import (
    DispositionChain,
    FunnelCheckpoint,
    PopulationCollisionLedger,
    PopulationStream,
    RankedCandidate,
    TournamentFunnelConfig,
    canonical_digest,
    write_json_atomic,
)


def _row(index: int, *, split: str) -> CachedProxyRow:
    return CachedProxyRow(
        position_hash=f"{index:032x}",
        split_component=f"spc-split-component-{split}-{index:08d}",
        chosen_series="a2a3",
        mover="white" if index % 2 else "black",
        target_twice_minus_one=(-1, 0, 1)[index % 3],
        sample_weight_units=100 + index % 17,
        features=tuple(index % 13 + offset - 6 for offset in range(7)),
    )


def _cache() -> ChallengerProxyCache:
    train = tuple(_row(index + 1, split="train") for index in range(1_024))
    validation = tuple(
        _row(index + 10_000, split="validation") for index in range(4_096)
    )
    provisional = ChallengerProxyCache(
        corpus_id="spc-fullgame-corpus-0123456789abcdef0123",
        corpus_manifest_sha256="ab" * 32,
        corpus_snapshot_digest="bc" * 32,
        component_split_digest="cd" * 32,
        train_rows=train,
        validation_rows=validation,
        audit_position_count=73,
        artifact_digest="",
    )
    deterministic = provisional.as_dict()
    deterministic.pop("artifact_digest")
    return ChallengerProxyCache(
        corpus_id=provisional.corpus_id,
        corpus_manifest_sha256=provisional.corpus_manifest_sha256,
        corpus_snapshot_digest=provisional.corpus_snapshot_digest,
        component_split_digest=provisional.component_split_digest,
        train_rows=train,
        validation_rows=validation,
        audit_position_count=73,
        artifact_digest=canonical_digest(
            "spc-challenger-proxy-cache-v1\0", deterministic
        ),
    )


def _proxy_sample(
    index: int,
    *,
    component: str,
    position_hash: str | None = None,
) -> SelfPlaySample:
    features = CachedFeatures(
        material=index % 17,
        king_space=index % 11,
        series_reach=index % 7,
        promotion_corridors=index % 5,
        immediate_vulnerability=index % 3,
        useful_mobility=index % 13,
        boundary_check=index % 2,
        white_check_distance=None,
        black_check_distance=None,
        reach_complete=True,
        white_king_ring_attack_multiplicity=0,
        black_king_ring_attack_multiplicity=0,
        white_promotable_next_series=0,
        black_promotable_next_series=0,
        white_king_edge_distance=0,
        black_king_edge_distance=0,
    )
    return SelfPlaySample(
        position_hash=position_hash or f"{index:032x}",
        pfen=f"fixture-pfen-{index}",
        run_id="verified-fixture-run",
        game_key=f"fixture-game-{index:06d}",
        opening_case_id=f"fixture-opening-{index:06d}",
        line_family=f"fixture-line-{index:06d}",
        split_component=component,
        split="train",
        series_number=2,
        mover="white" if index % 2 else "black",
        profile_id=baseline_profile().profile_id,
        chosen_series="a2a3",
        result=("1-0", "0-1", "1/2-1/2")[index % 3],
        target_white_score=(1.0, 0.0, 0.5)[index % 3],
        sample_weight=1.0,
        features=features,
    )


def test_quadratic_cached_scorer_matches_direct_fixed_point_loss() -> None:
    rows = (_row(7, split="train"), _row(11, split="train"))
    scorer = QuadraticCachedScorer(
        rows, regularization=STAGE_B_REGULARIZATION
    )
    member = PopulationStream(baseline_profile()).member(19)
    weights = tuple(
        getattr(member.profile.weights, name) for name in FEATURE_NAMES
    )
    numerator = 0
    total_weight = 0
    for row in rows:
        score_times_100 = sum(
            feature * weight
            for feature, weight in zip(row.features, weights, strict=True)
        )
        target_times_100 = (
            row.target_twice_minus_one * TARGET_EVALUATION_POINTS * 100
        )
        numerator += (
            row.sample_weight_units
            * (score_times_100 - target_times_100) ** 2
        )
        total_weight += row.sample_weight_units
    expected = numerator // total_weight + STAGE_B_REGULARIZATION * sum(
        (weight - 100) ** 2 for weight in weights
    )
    assert scorer(member) == expected
    assert len(scorer.scorer_digest) == 64


def test_proxy_cache_rejects_source_and_corpus_identity_drift() -> None:
    cache = _cache()
    assert ChallengerProxyCache.from_dict(cache.as_dict()) == cache

    stale = cache.as_dict()
    stale["source_fingerprint"] = "stale-source"
    deterministic = {key: value for key, value in stale.items() if key != "artifact_digest"}
    stale["artifact_digest"] = canonical_digest(
        "spc-challenger-proxy-cache-v1\0", deterministic
    )
    with pytest.raises(ValueError, match="source/schema identity is stale"):
        ChallengerProxyCache.from_dict(stale)

    with pytest.raises(ValueError, match="corpus identity changed"):
        _require_cache_input_identity(
            cache,
            corpus_id=cache.corpus_id,
            manifest_sha256="de" * 32,
        )


def test_proxy_cache_prioritizes_cross_split_hashes_and_keeps_frozen_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    train = [
        _proxy_sample(
            index,
            component=f"spc-split-component-train-{index:06d}",
        )
        for index in range(1, 1_025)
    ]
    validation = [
        _proxy_sample(
            index,
            component=f"spc-split-component-validation-{index:06d}",
        )
        for index in range(10_000, 14_096)
    ]
    audit = [
        _proxy_sample(
            20_000,
            component="spc-split-component-audit-020000",
        )
    ]
    collisions = [
        _proxy_sample(
            30_000,
            component="spc-split-component-validation-030000",
            position_hash=train[0].position_hash,
        ),
        _proxy_sample(
            30_001,
            component="spc-split-component-audit-030001",
            position_hash=validation[0].position_hash,
        ),
        _proxy_sample(
            30_002,
            component="spc-split-component-audit-030002",
            position_hash=train[1].position_hash,
        ),
    ]
    samples = tuple((*train, *validation, *audit, *collisions))
    corpus = SelfPlayCorpus(
        seed=20_260_840,
        holdout_percent=20,
        database_evidence=(
            {
                "source_kind": "verified-fullgame-store-snapshot",
                "manifest_sha256": "ab" * 32,
            },
        ),
        completed_games=len(samples),
        excluded_games=0,
        samples=samples,
        method=FULLGAME_CORPUS_METHOD,
    )

    def fixture_component_split(component: str, **_: object) -> str:
        for split in ("validation", "train", "audit"):
            if f"-{split}-" in component:
                return split
        raise AssertionError(component)

    monkeypatch.setattr(challengers, "component_split", fixture_component_split)
    config = TournamentFunnelConfig()
    first = _build_proxy_cache(corpus, config=config)
    second = _build_proxy_cache(corpus, config=config)

    assert first == second
    assert len(first.train_rows) == 1_024
    assert len(first.validation_rows) == 4_096
    assert first.audit_position_count == 1
    train_hashes = {row.position_hash for row in first.train_rows}
    validation_hashes = {row.position_hash for row in first.validation_rows}
    assert train_hashes.isdisjoint(validation_hashes)
    assert train[0].position_hash in train_hashes
    assert validation[0].position_hash in validation_hashes
    assert ChallengerProxyCache.from_dict(first.as_dict()) == first


def test_stage_a_checkpoint_survives_unpublished_crash_temp_and_detects_tamper(
    tmp_path: Path,
) -> None:
    checkpoint = FunnelCheckpoint(
        protocol_digest="12" * 32,
        stage="stage-a",
        scorer_digest="34" * 32,
        input_size=1 << 22,
        keep_count=65_536,
        next_input_offset=257,
        generator_index=256,
        disposition=DispositionChain("56" * 32, 257),
        ranked_candidates=(),
        complete=False,
    )
    path = tmp_path / "stage-a-checkpoint.json"
    write_json_atomic(checkpoint.as_dict(), path)
    (tmp_path / ".stage-a-checkpoint.json.crashed.tmp").write_text(
        "{", encoding="utf-8"
    )
    assert _load_checkpoint(path) == checkpoint

    corrupted = checkpoint.as_dict()
    corrupted["next_input_offset"] = 258
    path.write_text(__import__("json").dumps(corrupted), encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        _load_checkpoint(path)


def test_stage_a_resume_rolls_back_only_regenerable_unsealed_ledger_suffix(
    tmp_path: Path,
) -> None:
    stream = PopulationStream(baseline_profile())
    ledger_path = tmp_path / "population.sqlite3"
    with PopulationCollisionLedger(
        ledger_path, protocol_digest="12" * 32
    ) as ledger:
        for member in stream.iter_range(0, 12):
            ledger.record(member)
        ledger.commit()
        _reconcile_stage_a_ledger(ledger, stream=stream, checkpoint=None)
        assert ledger.count() == 0

        for member in stream.iter_range(0, 9):
            ledger.record(member)
        ledger.commit()
        checkpoint = FunnelCheckpoint(
            protocol_digest="12" * 32,
            stage="stage-a",
            scorer_digest="34" * 32,
            input_size=len(stream),
            keep_count=65_536,
            next_input_offset=5,
            generator_index=4,
            disposition=DispositionChain("56" * 32, 5),
            ranked_candidates=(),
            complete=False,
        )
        _reconcile_stage_a_ledger(
            ledger, stream=stream, checkpoint=checkpoint
        )
        assert ledger.count() == 5

        for member in stream.iter_range(5, 7):
            ledger.record(member)
        ledger.commit()
        ledger.connection.execute(
            "update population_identity set effective_id=? where candidate_index=6",
            ("spc-effective-tampered",),
        )
        ledger.commit()
        with pytest.raises(ValueError, match="cannot be regenerated"):
            _reconcile_stage_a_ledger(
                ledger, stream=stream, checkpoint=checkpoint
            )


def test_cli_exposes_only_explicit_production_challenger_inputs() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "challengers",
            "run",
            "run-root",
            "--fullgame-store",
            "fullgames",
            "--profile",
            "champion.json",
            "--batch-registry",
            "registry.sqlite3",
        ]
    )
    assert args.challengers_command == "run"
    assert args.checkpoint_every == 65_536
    assert args.fullgame_store == "fullgames"
    abandon = parser.parse_args(
        [
            "challengers",
            "abandon",
            "run-root",
            "--batch-registry",
            "registry.sqlite3",
            "--reason",
            "invalid-opening-plan",
        ]
    )
    assert abandon.challengers_command == "abandon"
    assert abandon.reason == "invalid-opening-plan"
    with pytest.raises(SystemExit):
        parser.parse_args(["challengers", "preflight", "run-root"])


def test_exact_64_catalog_is_materialized_without_champion_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = baseline_profile()
    stream = PopulationStream(baseline)
    ranked = []
    for index, member in enumerate(stream.iter_range(0, 64)):
        ranked.append(
            RankedCandidate(
                candidate_index=member.candidate_index,
                effective_id=member.effective_id,
                profile_id=member.profile.profile_id,
                rank_units=index,
            )
        )
    survivors = {
        "survivor_set_digest": "78" * 32,
        "survivors": [candidate.as_dict() for candidate in ranked],
    }
    manifest = {
        "protocol_digest": "9a" * 32,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
    }
    events: list[str] = []

    def freeze(*_args, **_kwargs):
        events.append("freeze-all-openings")
        return {"registry_snapshot_digest": "aa" * 32}

    def validate(*_args, **_kwargs):
        events.append("validate-opening-preparation")
        return ()

    def reserve(*_args, **_kwargs):
        events.append("reserve-alpha-batch")
        return {"batch": "fixture"}

    def build(*_args, **kwargs):
        events.append("build-bound-plan")
        assert kwargs["frozen_opening_suites"] == ()
        return {
            "tournament_plan_digest": "ef" * 32,
            "promotion_effect": "none",
        }

    monkeypatch.setattr(
        challengers,
        "reserve_promotion_batch",
        reserve,
    )
    monkeypatch.setattr(
        challengers,
        "freeze_opening_suites_before_batch",
        freeze,
    )
    monkeypatch.setattr(
        challengers,
        "validate_opening_suite_preparation",
        validate,
    )
    monkeypatch.setattr(
        challengers,
        "build_tournament_plan",
        build,
    )
    plan, profiles = _materialize_catalog_and_plan(
        tmp_path,
        stream=stream,
        baseline=baseline,
        survivors=survivors,
        manifest=manifest,
        registry=tmp_path / "registry.sqlite3",
        corpus_exclusion_artifact={"artifact_digest": "bb" * 32},
    )
    catalog = __import__("json").loads(
        (tmp_path / "profile-catalog.json").read_text(encoding="utf-8")
    )
    assert catalog["challenger_count"] == 64
    assert len(catalog["challengers"]) == 64
    assert len(profiles) == 65
    assert plan["promotion_effect"] == "none"
    assert events == [
        "freeze-all-openings",
        "validate-opening-preparation",
        "reserve-alpha-batch",
        "build-bound-plan",
    ]
    assert not (tmp_path / "champion.json").exists()


def test_cache_contract_constants_are_frozen() -> None:
    assert CHALLENGER_CACHE_FORMAT == "spc-challenger-proxy-cache-v1"
    assert CACHED_SCORER_ID == "quadratic-fixed-point-fullgame-value-proxy-v1"
    assert SAMPLE_WEIGHT_SCALE == 1_000_000
    assert ENGINE_VERSION.startswith("spc-")


def test_challenger_store_manifest_requires_exact_million_game_target(
    tmp_path: Path,
) -> None:
    store = tmp_path / "fullgames"
    store.mkdir()

    def write(*, target: int, accepted: int, status: str = "complete") -> None:
        (store / "manifest.json").write_text(
            json.dumps(
                {
                    "simulation_id": "spc-fullgame-simulation-fixture",
                    "execution": {
                        "attempts_per_chunk": 1_024,
                        "target_unique_games": target,
                    },
                    "progress": {
                        "status": status,
                        "target_unique_games": target,
                        "accepted_unique_games": accepted,
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="ascii",
        )

    write(target=1_000_000, accepted=1_000_000)
    raw, manifest = challengers._read_complete_store_manifest(store)
    assert raw == (store / "manifest.json").read_bytes()
    assert manifest["execution"]["target_unique_games"] == 1_000_000
    assert manifest["progress"]["accepted_unique_games"] == 1_000_000

    write(target=50_000, accepted=50_000)
    with pytest.raises(ValueError, match="exactly 1,000,000"):
        challengers._read_complete_store_manifest(store)

    write(target=1_000_000, accepted=999_999)
    with pytest.raises(ValueError, match="exactly 1,000,000"):
        challengers._read_complete_store_manifest(store)
