from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import random
import tempfile
from typing import Any, Mapping


PROFILE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class EvaluationWeights:
    """Percentage scales for the explainable progressive evaluation terms.

    A value of 100 preserves the hand-authored baseline.  Keeping the search
    implementation shared while varying this compact vector makes league
    comparisons attributable and reproducible.
    """

    material: int = 100
    king_space: int = 100
    series_reach: int = 100
    promotion_corridors: int = 100
    immediate_vulnerability: int = 100
    useful_mobility: int = 100
    boundary_check: int = 100

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not 25 <= value <= 300:
                raise ValueError(f"evaluation weight {name} must be between 25 and 300")

    def scale(self, term: str, value: int) -> int:
        return round(value * getattr(self, term) / 100)


@dataclass(frozen=True, slots=True)
class EngineProfile:
    """Versioned parameters consumed by the one shared rules/search core."""

    name: str
    weights: EvaluationWeights = field(default_factory=EvaluationWeights)
    recommended_depth: int = 2
    recommended_branch_cap: int = 32
    generation: int = 0
    parent_profile_ids: tuple[str, ...] = ()
    mutation_seed: int | None = None
    notes: str = ""
    schema_version: int = PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("profile name cannot be empty")
        if self.schema_version != PROFILE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported profile schema {self.schema_version}; "
                f"expected {PROFILE_SCHEMA_VERSION}"
            )
        if not 1 <= self.recommended_depth <= 8:
            raise ValueError("recommended_depth must be between 1 and 8")
        if not 1 <= self.recommended_branch_cap <= 512:
            raise ValueError("recommended_branch_cap must be between 1 and 512")
        if self.generation < 0:
            raise ValueError("generation cannot be negative")
        if len(self.parent_profile_ids) > 2:
            raise ValueError("a profile can have at most two parents")

    @property
    def profile_id(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "weights": asdict(self.weights),
            "recommended_depth": self.recommended_depth,
            "recommended_branch_cap": self.recommended_branch_cap,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "spc-" + hashlib.sha256(encoded).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "name": self.name,
            "weights": asdict(self.weights),
            "recommended_depth": self.recommended_depth,
            "recommended_branch_cap": self.recommended_branch_cap,
            "generation": self.generation,
            "parent_profile_ids": list(self.parent_profile_ids),
            "mutation_seed": self.mutation_seed,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EngineProfile:
        try:
            weights_payload = payload.get("weights", {})
            if not isinstance(weights_payload, Mapping):
                raise ValueError("weights must be an object")
            profile = cls(
                name=str(payload["name"]),
                weights=EvaluationWeights(
                    **{key: int(value) for key, value in weights_payload.items()}
                ),
                recommended_depth=int(payload.get("recommended_depth", 2)),
                recommended_branch_cap=int(
                    payload.get("recommended_branch_cap", 32)
                ),
                generation=int(payload.get("generation", 0)),
                parent_profile_ids=tuple(
                    str(value) for value in payload.get("parent_profile_ids", ())
                ),
                mutation_seed=(
                    None
                    if payload.get("mutation_seed") is None
                    else int(payload["mutation_seed"])
                ),
                notes=str(payload.get("notes", "")),
                schema_version=int(
                    payload.get("schema_version", PROFILE_SCHEMA_VERSION)
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid engine profile: {error}") from error
        supplied_id = payload.get("profile_id")
        if supplied_id is not None and str(supplied_id) != profile.profile_id:
            raise ValueError("engine profile_id does not match its parameters")
        return profile


def baseline_profile() -> EngineProfile:
    return EngineProfile(
        name="Scottish Progressive baseline",
        notes="Hand-authored reference profile; no match-strength claim.",
    )


def mutate_profile(
    parent: EngineProfile,
    *,
    seed: int,
    name: str | None = None,
    generation: int | None = None,
) -> EngineProfile:
    """Produces a deterministic, bounded mutation of an existing profile."""

    rng = random.Random(seed)
    values = asdict(parent.weights)
    keys = sorted(values)
    mutation_count = rng.randint(2, min(4, len(keys)))
    for key in rng.sample(keys, mutation_count):
        step = rng.choice((-20, -15, -10, 10, 15, 20))
        values[key] = max(25, min(300, values[key] + step))

    # Search settings are mutated less often because evaluation comparisons
    # are more meaningful when both profiles see similar trees.
    branch_cap = parent.recommended_branch_cap
    if rng.random() < 0.35:
        branch_cap = max(4, min(512, branch_cap + rng.choice((-8, 8))))

    next_generation = parent.generation + 1 if generation is None else generation
    return EngineProfile(
        name=name or f"{parent.name} mutation {seed}",
        weights=EvaluationWeights(**values),
        recommended_depth=parent.recommended_depth,
        recommended_branch_cap=branch_cap,
        generation=next_generation,
        parent_profile_ids=(parent.profile_id,),
        mutation_seed=seed,
        notes="Deterministic league mutation; strength is unverified until gated match promotion.",
    )


def crossover_profile(
    first: EngineProfile,
    second: EngineProfile,
    *,
    seed: int,
    name: str | None = None,
) -> EngineProfile:
    """Combines two proven parameter vectors without combining their moves."""

    rng = random.Random(seed)
    first_values = asdict(first.weights)
    second_values = asdict(second.weights)
    combined = {
        key: rng.choice((first_values[key], second_values[key]))
        for key in sorted(first_values)
    }
    mutation_count = rng.randint(1, min(3, len(combined)))
    for key in rng.sample(sorted(combined), mutation_count):
        combined[key] = max(
            25,
            min(300, combined[key] + rng.choice((-15, -10, 10, 15))),
        )
    generation = max(first.generation, second.generation) + 1
    return EngineProfile(
        name=name or f"crossover g{generation} seed {seed}",
        weights=EvaluationWeights(**combined),
        recommended_depth=max(first.recommended_depth, second.recommended_depth),
        recommended_branch_cap=round(
            (first.recommended_branch_cap + second.recommended_branch_cap) / 2
        ),
        generation=generation,
        parent_profile_ids=(first.profile_id, second.profile_id),
        mutation_seed=seed,
        notes="Parameter crossover plus bounded mutation; moves are never committee-voted.",
    )


def create_population(
    champion: EngineProfile | None = None,
    *,
    size: int = 10,
    seed: int = 20260820,
) -> tuple[EngineProfile, ...]:
    if not 2 <= size <= 64:
        raise ValueError("population size must be between 2 and 64")
    champion = champion or baseline_profile()
    profiles = [champion]
    used = {champion.profile_id}
    index = 1
    while len(profiles) < size:
        mutation_seed = seed + index * 1_000_003
        candidate = mutate_profile(
            champion,
            seed=mutation_seed,
            name=f"generation {champion.generation + 1} candidate {index}",
        )
        index += 1
        if candidate.profile_id not in used:
            used.add(candidate.profile_id)
            profiles.append(candidate)
    return tuple(profiles)


def load_profile(path: str | Path) -> EngineProfile:
    source = Path(path).expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load engine profile {source}: {error}") from error
    if not isinstance(payload, Mapping):
        raise ValueError("engine profile root must be an object")
    profile_payload = payload.get("profile", payload)
    if not isinstance(profile_payload, Mapping):
        raise ValueError("champion envelope profile must be an object")
    return EngineProfile.from_dict(profile_payload)


def save_profile(
    profile: EngineProfile,
    path: str | Path,
    *,
    provenance: Mapping[str, Any] | None = None,
) -> Path:
    """Atomically publishes a profile; a partial champion file is never read."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        payload: Mapping[str, Any] = profile.as_dict()
        if provenance is not None:
            payload = {
                "format": "spc-champion-envelope-v1",
                "profile": profile.as_dict(),
                "provenance": dict(provenance),
            }
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
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
