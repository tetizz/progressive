from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_FEN = (
    "Nnb1kbnr/pppp2pp/4p3/8/5q2/3P4/PPPKPP2/3R1BN1 w k - 1 13"
)
ATTACK = ["d2c3", "d3d4", "d4d5", "d5e6", "d1d7", "a8c7"]
FORCED_REPLY = ["f4c7"]
MATE = [
    "c3b3",
    "a2a4",
    "c2c4",
    "c4c5",
    "c5c6",
    "e2e4",
    "d7f7",
    "e6e7",
    "e7f8q",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the exact recorded single-reply ladder in compiled WASM."
    )
    parser.add_argument("--module", type=Path, required=True)
    parser.add_argument("--build-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    args = parse_args()
    build = json.loads(args.build_receipt.read_text(encoding="utf-8"))
    cases = [
        {
            "fen": FIXTURE_FEN,
            "series": 7,
            "progressive_ep": "-",
            "promoted_hex": "0000000020000000",
            "max_work": 1_000_000,
            "time_limit_ms": 30_000,
        },
        {
            "fen": FIXTURE_FEN,
            "series": 7,
            "progressive_ep": "-",
            "promoted_hex": "0000000020000000",
            "max_work": 1,
            "time_limit_ms": 30_000,
        },
    ]
    completed = subprocess.run(
        [
            "node",
            str(ROOT / "benchmarks" / "wasm_batch_probe.mjs"),
            "ladder",
            str(args.module.resolve()),
        ],
        input=json.dumps(cases),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        cwd=ROOT,
    )
    found, limited = json.loads(completed.stdout)
    if (
        found.get("schema") != "spc-single-reply-mate-ladder-native-v1"
        or found.get("abi_version") != 1
        or found.get("kernel_status") != "found"
        or found.get("proof_status") != "found"
        or found.get("complete") is not True
        or found.get("attack_moves") != ATTACK
        or found.get("forced_reply_moves") != FORCED_REPLY
        or found.get("mate_moves") != MATE
        or found.get("stats", {}).get("work_used") != 628_052
    ):
        raise AssertionError("compiled WASM did not reproduce the exact 3dfd ladder")
    if (
        limited.get("schema") != "spc-single-reply-mate-ladder-native-v1"
        or limited.get("abi_version") != 1
        or limited.get("kernel_status") != "work_limit"
        or limited.get("proof_status") != "unknown"
        or limited.get("complete") is not False
        or limited.get("attack_moves")
        or limited.get("forced_reply_moves")
        or limited.get("mate_moves")
        or limited.get("stats", {}).get("work_used") != 1
    ):
        raise AssertionError("compiled WASM treated constrained work as safe")
    receipt = {
        "schema": "spc-single-reply-mate-ladder-wasm-verification-v1",
        "fixture": "bucephalus-3dfd-selected-child-s7",
        "source_revision": build.get("source_revision"),
        "source_fingerprint": build.get("source_fingerprint"),
        "kernel_sha256": build.get("kernel_sha256"),
        "wasm_sha256": build.get("wasm_sha256"),
        "module_js_sha256": build.get("module_js_sha256"),
        "artifact_set_sha256": build.get("artifact_set_sha256"),
        "found": found,
        "work_limited": limited,
        "checks": {
            "exact_paths": True,
            "exact_work_628052": True,
            "work_limit_is_unknown": True,
            "unknown_has_no_proof_path": True,
        },
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
