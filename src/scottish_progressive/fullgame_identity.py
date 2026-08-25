from __future__ import annotations

import hashlib
from pathlib import Path
import struct

import chess


FULLGAME_SEMANTIC_FINGERPRINT_DOMAIN = b"SPC-FULLGAME-SEMANTICS-V1\0"
# This is deliberately a narrow semantic closure, not the product-wide source
# fingerprint used by tournament/search evidence. Native move generation has
# its own stricter four-file C++ identity; these Python sources bind the rules,
# replay, codec, evaluation/profile schema, policy, and persisted config.
FULLGAME_SEMANTIC_SOURCE_FILES = (
    "evaluation.py",
    "fullgame.py",
    "fullgame_codec.py",
    "fullgame_identity.py",
    "model.py",
    "profiles.py",
    "rules.py",
)
FULLGAME_TERMINAL_SCORE = 1_000_000


def fullgame_semantic_fingerprint(package: str | Path | None = None) -> str:
    """Returns the exact digest of persisted full-game semantic inputs.

    Missing inputs fail closed. Product code outside the explicit closure—
    such as tournament orchestration, web UI, or neural experiments—cannot
    perturb a resumable store's simulation identity.
    """

    root = (
        Path(__file__).resolve().parent
        if package is None
        else Path(package).expanduser().resolve()
    )
    digest = hashlib.sha256()
    digest.update(FULLGAME_SEMANTIC_FINGERPRINT_DOMAIN)
    dependency = f"python-chess:{chess.__version__}".encode("ascii")
    digest.update(struct.pack("<Q", len(dependency)))
    digest.update(dependency)
    for name in FULLGAME_SEMANTIC_SOURCE_FILES:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(
                f"full-game semantic source is missing: {path}"
            )
        encoded_name = name.encode("ascii")
        payload = path.read_bytes()
        digest.update(struct.pack("<Q", len(encoded_name)))
        digest.update(encoded_name)
        digest.update(struct.pack("<Q", len(payload)))
        digest.update(payload)
    return digest.hexdigest()


FULLGAME_SEMANTIC_FINGERPRINT = fullgame_semantic_fingerprint()
