from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from scottish_progressive.model import ProgressiveState
from scottish_progressive.profiles import (
    EngineProfile,
    EvaluationWeights,
    baseline_profile,
    create_population,
    crossover_profile,
    load_profile,
    mutate_profile,
    save_profile,
)
from scottish_progressive.search import SearchLimits, analyze


def test_default_population_is_ten_unique_deterministic_profiles() -> None:
    first = create_population(seed=41)
    second = create_population(seed=41)
    assert len(first) == 10
    assert [profile.profile_id for profile in first] == [
        profile.profile_id for profile in second
    ]
    assert len({profile.profile_id for profile in first}) == 10
    assert first[0].profile_id == baseline_profile().profile_id


def test_mutation_is_bounded_and_records_its_parent() -> None:
    parent = baseline_profile()
    child = mutate_profile(parent, seed=99)
    assert child.parent_profile_ids == (parent.profile_id,)
    assert child.mutation_seed == 99
    assert child.profile_id != parent.profile_id
    assert all(25 <= value <= 300 for value in asdict(child.weights).values())


def test_crossover_records_both_parents_and_stays_one_profile() -> None:
    parent = baseline_profile()
    first = mutate_profile(parent, seed=1)
    second = mutate_profile(parent, seed=2)
    child = crossover_profile(first, second, seed=3)
    assert child.parent_profile_ids == (first.profile_id, second.profile_id)
    assert child.profile_id not in {first.profile_id, second.profile_id}


def test_profile_round_trip_and_parameter_tamper_detection(tmp_path) -> None:
    path = save_profile(mutate_profile(baseline_profile(), seed=123), tmp_path / "champion.json")
    loaded = load_profile(path)
    assert loaded.profile_id == json.loads(path.read_text())["profile_id"]

    payload = json.loads(path.read_text())
    payload["weights"]["material"] += 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="profile_id does not match"):
        load_profile(path)


def test_champion_envelope_keeps_profile_id_independent_of_provenance(tmp_path) -> None:
    profile = mutate_profile(baseline_profile(), seed=456)
    first_id = profile.profile_id
    path = save_profile(
        profile,
        tmp_path / "champion.json",
        provenance={"run_id": "fixture", "source_fingerprint": "abc"},
    )
    payload = json.loads(path.read_text())
    assert payload["format"] == "spc-champion-envelope-v1"
    assert payload["profile"]["profile_id"] == first_id
    assert payload["provenance"]["run_id"] == "fixture"
    assert load_profile(path).profile_id == first_id


def test_profile_validation_rejects_unbounded_genome() -> None:
    with pytest.raises(ValueError, match="between 25 and 300"):
        EvaluationWeights(material=301)
    with pytest.raises(ValueError, match="branch"):
        EngineProfile(name="bad", recommended_branch_cap=0)


def test_search_result_identifies_the_single_profile_used() -> None:
    profile = EngineProfile(
        name="material specialist",
        weights=EvaluationWeights(material=150),
    )
    result = analyze(
        ProgressiveState.initial(),
        SearchLimits(depth_series=1, max_series_per_node=2),
        profile=profile,
    )
    assert result.engine_profile_id == profile.profile_id
    assert result.engine_profile_name == profile.name
