from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "src/scottish_progressive/_native_eval.cpp",
    "src/scottish_progressive/native_eval.hpp",
    "src/scottish_progressive/native_subtree.cpp",
    "src/scottish_progressive/native_subtree.hpp",
    "src/scottish_progressive/native_subtree_wasm.cpp",
    "src/scottish_progressive/native_subtree_wasm.hpp",
)


def tracked_paths() -> set[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return {
        item.decode("utf-8").replace("\\", "/")
        for item in completed.stdout.split(b"\0")
        if item
    }


def main() -> int:
    tracked = tracked_paths()
    missing = [path for path in REQUIRED if not (ROOT / path).is_file()]
    untracked = [path for path in REQUIRED if path not in tracked]
    receipt = {
        "schema": "spc-wasm-dependency-closure-v1",
        "ok": not missing and not untracked,
        "required": list(REQUIRED),
        "missing_from_worktree": missing,
        "missing_from_clean_checkout": untracked,
    }
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
