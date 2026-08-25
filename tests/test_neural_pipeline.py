from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
import multiprocessing

import pytest

from experiments import frozen_store_receipt, neural_pipeline
from scottish_progressive.fullgame_codec import FullGameRecord, Terminal
from scottish_progressive.league import HUMAN_FIRST_GAME_CONTENDER_HYPOTHESES
from scottish_progressive.neural_evaluator import NeuralSample, extract_active_features
from scottish_progressive.evaluation import evaluate
from scottish_progressive.model import ProgressiveState
from scottish_progressive.profiles import baseline_profile


def _record(attempt_index: int) -> FullGameRecord:
    return FullGameRecord(
        attempt_index=attempt_index,
        terminal=Terminal.CHECKMATE_WHITE,
        series=(("e2e4",),),
    )


def test_bounded_record_selection_scans_every_game_and_is_order_independent() -> None:
    records = tuple(_record(index) for index in range(100))
    first, first_scanned = neural_pipeline._select_bounded_records(
        records,
        simulation_id="simulation-fixture",
        seed=73,
        max_games=7,
    )
    second, second_scanned = neural_pipeline._select_bounded_records(
        reversed(records),
        simulation_id="simulation-fixture",
        seed=73,
        max_games=7,
    )
    assert first_scanned == second_scanned == 100
    assert len(first) == 7
    assert [record.attempt_index for record in first] == [
        record.attempt_index for record in second
    ]
    assert [record.attempt_index for record in first] == sorted(
        record.attempt_index for record in first
    )


def test_teacher_runtime_is_exactly_bound_and_requires_native(monkeypatch) -> None:
    identity = {
        "engine_source_fingerprint": "source",
        "python_executable": "python",
        "python_version": "3.14",
        "native_available": True,
        "native_expected_source_identity": "native",
        "native_loaded_source_identity": "native",
        "native_binary": "native.pyd",
        "native_binary_sha256": "binary",
    }
    monkeypatch.setattr(
        neural_pipeline,
        "_teacher_runtime_identity",
        lambda: dict(identity),
    )
    assert neural_pipeline._require_teacher_runtime(identity) == identity
    with pytest.raises(ValueError, match="differs from the manifest"):
        neural_pipeline._require_teacher_runtime({**identity, "python_version": "wrong"})
    monkeypatch.setattr(
        neural_pipeline,
        "_teacher_runtime_identity",
        lambda: {**identity, "native_available": False},
    )
    with pytest.raises(ValueError, match="requires the validated native engine"):
        neural_pipeline._require_teacher_runtime(identity)


def test_teacher_runtime_contract_survives_spawned_workers() -> None:
    expected = neural_pipeline._teacher_runtime_identity()
    neural_pipeline._require_teacher_runtime(expected)
    with ProcessPoolExecutor(
        max_workers=2,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=neural_pipeline._require_teacher_runtime,
        initargs=(expected,),
    ) as executor:
        futures = [
            executor.submit(neural_pipeline._teacher_runtime_identity)
            for _index in range(2)
        ]
        observed = [future.result() for future in futures]
    assert observed == [expected, expected]


def test_teacher_result_log_is_canonical_durable_and_fail_closed(tmp_path) -> None:
    path = tmp_path / "results.jsonl"
    rows = (
        {"accepted": False, "sample_id": "sample-1", "split": "train"},
        {"accepted": False, "sample_id": "sample-2", "split": "validation"},
    )
    neural_pipeline._append_rows(path, rows)
    assert neural_pipeline._read_jsonl(path) == list(rows)
    with path.open("ab") as stream:
        stream.write(b'{"uncommitted":true}')
    with pytest.raises(ValueError, match="not committed"):
        neural_pipeline._read_jsonl(path)


def test_teacher_resume_binds_source_identity_without_requiring_labeled_sample_id() -> None:
    state = ProgressiveState.initial()
    profile = baseline_profile()
    source = NeuralSample(
        game_key="resume-fixture",
        position_hash=state.position_hash,
        pfen=state.pfen,
        active_features=extract_active_features(state),
        base_profile_id=profile.profile_id,
        base_hand_score=evaluate(state, profile).total,
        weak_wdl_milli=500,
        weak_source_fingerprint="weak-fixture",
        split_component="resume-group",
        split="train",
    )
    labeled = replace(
        source,
        teacher_score=123,
        teacher_result_fingerprint="teacher-fixture",
        teacher_profile_id=profile.profile_id,
        teacher_completed_depth=4,
        teacher_exact_width=False,
    )
    assert source.sample_id != labeled.sample_id
    neural_pipeline._validate_attached_teacher_sample(source, labeled)


def test_mandatory_human_first_game_teacher_group_is_complete_and_train_owned() -> None:
    anchors = neural_pipeline._mandatory_teacher_anchor_plan()
    assert len(anchors) == 1 + len(HUMAN_FIRST_GAME_CONTENDER_HYPOTHESES) == 4
    assert [item["anchor_id"].rsplit(":", 1)[-1] for item in anchors] == [
        "root",
        "contender-A",
        "contender-B",
        "contender-E",
    ]
    assert len({item["position_hash"] for item in anchors}) == 4
    assert {item["group_id"] for item in anchors} == {
        neural_pipeline.MANDATORY_TEACHER_GROUP
    }
    assert {item["split"] for item in anchors} == {"train"}
    assert len({item["split_component"] for item in anchors}) == 1


def test_store_catalog_seals_manifest_checkpoint_and_chunk_stream(tmp_path) -> None:
    (tmp_path / "chunks").mkdir()
    (tmp_path / "manifest.json").write_text("{}", encoding="ascii")
    (tmp_path / "checkpoint.sqlite3").write_bytes(b"checkpoint")
    chunk = tmp_path / "chunks" / "000.spcg"
    chunk.write_bytes(b"chunk-v1")
    before = neural_pipeline._store_catalog(tmp_path)
    assert before["chunk_count"] == 1
    chunk.write_bytes(b"chunk-v2")
    after = neural_pipeline._store_catalog(tmp_path)
    assert after["stream_sha256"] != before["stream_sha256"]
    assert after["catalog_sha256"] != before["catalog_sha256"]


def test_forged_frozen_receipt_is_rejected_before_store_ingest(tmp_path) -> None:
    store = tmp_path / "store"
    snapshot = tmp_path / "snapshot"
    store.mkdir()
    snapshot.mkdir()
    receipt = tmp_path / "forged.json"
    pinned = "a" * 64
    payload = {
        "format": neural_pipeline.FROZEN_RECEIPT_FORMAT,
        "store_root": str(store.resolve()),
        "snapshot_root": str(snapshot.resolve()),
        "snapshot_manifest_sha256": pinned,
        "receipt_generator_sha256": "forged",
    }
    receipt.write_bytes(neural_pipeline._canonical(payload).encode("ascii") + b"\n")
    with pytest.raises(ValueError, match="generator identity differs"):
        neural_pipeline._load_frozen_receipt(
            store,
            receipt,
            expected_snapshot_manifest_sha256=pinned,
        )


def test_frozen_receipt_output_must_be_outside_store_and_snapshot(tmp_path) -> None:
    store = tmp_path / "store"
    snapshot = tmp_path / "snapshot"
    store.mkdir()
    snapshot.mkdir()
    with pytest.raises(ValueError, match="outside the full-game store"):
        frozen_store_receipt.main(
            [str(store), str(snapshot), str(store / "receipt.json")]
        )
    with pytest.raises(ValueError, match="outside the immutable snapshot"):
        frozen_store_receipt.main(
            [str(store), str(snapshot), str(snapshot / "receipt.json")]
        )


def _strength_report(*, wins: int, draws: int, losses: int) -> dict[str, object]:
    pairs = wins + draws + losses
    return {
        "summary": {
            "scheduled_pairs": pairs,
            "incomplete_pairs": 0,
            "incomplete_games": 0,
            "candidate_game_score_rate": 0.6 if wins > losses else 0.4,
            "candidate_pair_wdl": {
                "wins": wins,
                "draws": draws,
                "losses": losses,
            },
            "technical_failures": {
                "total_profile_failures": 0,
                "unattributed_worker_failures": 0,
                "unattributed_match_limit_failures": 0,
            },
        }
    }


def test_strength_decision_requires_complete_significant_pair_win() -> None:
    passed = neural_pipeline._strength_decision(
        _strength_report(wins=12, draws=35, losses=3),
        minimum_pairs=50,
    )
    assert passed["passed"] is True
    assert passed["one_sided_pair_sign_test_p_value"] <= 0.05

    tied = neural_pipeline._strength_decision(
        _strength_report(wins=5, draws=40, losses=5),
        minimum_pairs=50,
    )
    assert tied["passed"] is False
    assert tied["checks"]["candidate_pair_wins_exceed_losses"] is False

    incomplete = _strength_report(wins=12, draws=35, losses=3)
    incomplete["summary"]["incomplete_games"] = 1
    decision = neural_pipeline._strength_decision(incomplete, minimum_pairs=50)
    assert decision["passed"] is False
    assert decision["checks"]["all_games_complete"] is False


def test_pipeline_defaults_require_complete_million_and_gate_receipt() -> None:
    dataset = neural_pipeline.parser().parse_args(["dataset", "store", "dataset.json"])
    assert dataset.required_store_games == 1_000_000
    assert dataset.max_games == 100_000
    assert dataset.max_samples == 200_000
    assert dataset.positions_per_game == 2

    with pytest.raises(SystemExit):
        neural_pipeline.parser().parse_args(
            ["match", "variant.json", "baseline", "holdout.json", "match.json"]
        )
