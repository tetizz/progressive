from __future__ import annotations

import argparse
import json
from pathlib import Path

from scottish_progressive.native_teacher import (
    merge_native_teacher_tiers,
    write_native_teacher_artifact,
)


def _read(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"teacher artifact is not an object: {path}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge the fixed quiet-D2 and tactical-D3 teacher tiers."
    )
    parser.add_argument("quiet_depth2", type=Path)
    parser.add_argument("tactical_depth3", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    payload = merge_native_teacher_tiers(
        _read(args.quiet_depth2),
        _read(args.tactical_depth3),
    )
    path = write_native_teacher_artifact(payload, args.output)
    print(
        json.dumps(
            {
                "artifact_path": str(path),
                "corpus_id": payload["corpus_id"],
                "accepted_roots": payload["quality"]["accepted_roots"],
                "train_roots": payload["quality"]["train_roots"],
                "holdout_roots": payload["quality"]["holdout_roots"],
                "tier_metrics": payload["quality"]["tier_metrics"],
                "selection": payload["selection"],
            },
            sort_keys=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
