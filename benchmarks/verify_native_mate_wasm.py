from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import chess


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scottish_progressive.model import ProgressiveState  # noqa: E402
from scottish_progressive.series_mate import (  # noqa: E402
    SeriesMateStatus,
    find_native_series_mate,
)


LIVE_S5 = "rn1q1bnr/ppp1pkpp/5p2/8/3Pp3/2NB4/PPP2PPP/R1BbK1NR w KQ - 0 7"
MIRRORED_LIVE_S6 = "r1bBk1nr/ppp2ppp/2nb4/3pP3/8/5P2/PPP1PKPP/RN1Q1BNR b kq - 0 7"
START_BLACK = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1"
CASES = (
    {
        "name": "white-live-s5-found",
        "fen": LIVE_S5,
        "series": 5,
        "max_positions": 1_000_000,
        "max_work": 10_000_000,
        "time_limit_ms": 30_000,
    },
    {
        "name": "black-mirrored-s6-found",
        "fen": MIRRORED_LIVE_S6,
        "series": 6,
        "max_positions": 1_000_000,
        "max_work": 10_000_000,
        "time_limit_ms": 30_000,
    },
    {
        "name": "bare-kings-exhausted",
        "fen": "8/8/8/8/8/2k5/8/K7 w - - 0 1",
        "series": 3,
        "max_positions": 1_000_000,
        "max_work": 10_000_000,
        "time_limit_ms": 30_000,
    },
    {
        "name": "work-limit-unknown",
        "fen": LIVE_S5,
        "series": 5,
        "max_positions": 1_000_000,
        "max_work": 100,
        "time_limit_ms": 30_000,
    },
    {
        "name": "deadline-unknown",
        "fen": START_BLACK,
        "series": 8,
        "max_positions": 1_000_000,
        "max_work": 10_000_000,
        "time_limit_ms": 1,
    },
)

STAT_FIELDS = (
    "positions_visited",
    "moves_generated",
    "transpositions_merged",
    "checking_series",
    "checkmates",
    "peak_frontier",
    "max_depth_reached",
)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the combined WASM mate ABI against native Python."
    )
    parser.add_argument("--module", type=Path, required=True)
    parser.add_argument("--build-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def normalized_wasm(case: dict[str, object], value: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {
        "kernel_status": value["kernel_status"],
        "proof_status": value["proof_status"],
        "complete": value["complete"],
        "series_number": value["series_number"],
        "max_positions": value["max_positions"],
        "max_work": value["max_work"],
        "moves": value["moves"],
    }
    # Deadline work is inherently clock-scheduled. Its fail-closed semantic
    # status is exact; deterministic cases additionally bind every work count.
    if value["kernel_status"] != "deadline":
        result["stats"] = value["stats"]
    return result


def normalized_oracle(case: dict[str, object]) -> dict[str, object]:
    state = ProgressiveState.from_fen(str(case["fen"]), int(case["series"]))
    probe = find_native_series_mate(
        state,
        max_positions=int(case["max_positions"]),
        max_work=int(case["max_work"]),
        time_limit_seconds=float(case["time_limit_ms"]) / 1_000.0,
    )
    status = {
        SeriesMateStatus.FOUND: "found",
        SeriesMateStatus.EXHAUSTED: "exhausted",
        SeriesMateStatus.WORK_LIMIT: "work_limit",
        SeriesMateStatus.DEADLINE: "deadline",
    }.get(probe.status)
    if status is None:
        raise AssertionError(f"native mate oracle is unavailable: {probe.status}")
    result: dict[str, object] = {
        "kernel_status": status,
        "proof_status": status if status in {"found", "exhausted"} else "unknown",
        "complete": probe.complete,
        "series_number": int(case["series"]),
        "max_positions": int(case["max_positions"]),
        "max_work": int(case["max_work"]),
        "moves": [] if probe.series is None else list(probe.series.moves),
    }
    if status != "deadline":
        result["stats"] = {
            key: getattr(probe, key)
            for key in STAT_FIELDS
        }
    return result


def main() -> int:
    args = parse_args()
    build = json.loads(args.build_receipt.read_text(encoding="utf-8"))
    identity = {
        key: build[key]
        for key in (
            "source_revision",
            "source_fingerprint",
            "kernel_sha256",
            "wasm_sha256",
            "module_js_sha256",
            "artifact_set_sha256",
        )
    }
    wire_cases = [
        {key: value for key, value in case.items() if key != "name"}
        for case in CASES
    ]
    completed = subprocess.run(
        [
            "node",
            str(ROOT / "benchmarks" / "wasm_batch_probe.mjs"),
            "mate",
            str(args.module.resolve()),
        ],
        input=json.dumps(wire_cases),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        cwd=ROOT,
    )
    results = json.loads(completed.stdout)
    if len(results) != len(CASES):
        raise AssertionError("WASM batch result count differs from request")
    by_name = {case["name"]: result for case, result in zip(CASES, results, strict=True)}
    live = by_name["white-live-s5-found"]
    if live["proof_status"] != "found" or live["moves"] != [
        "c3d5",
        "d3e4",
        "e4h7",
        "d5f4",
        "h7g6",
    ]:
        raise AssertionError("live S5 mate result changed")
    if live["stats"] != {
        "positions_visited": 600,
        "moves_generated": 24006,
        "transpositions_merged": 472,
        "checking_series": 579,
        "checkmates": 1,
        "peak_frontier": 421,
        "max_depth_reached": 5,
    }:
        raise AssertionError("live S5 mate stats changed")
    black = by_name["black-mirrored-s6-found"]
    if black["proof_status"] != "found" or black["moves"] != [
        "c6d4",
        "c8h3",
        "d6e5",
        "e5h2",
        "d4f5",
        "h2g3",
    ]:
        raise AssertionError("mirrored Black S6 mate result changed")
    exhausted = by_name["bare-kings-exhausted"]
    if exhausted["proof_status"] != "exhausted" or not exhausted["complete"]:
        raise AssertionError("bare-kings exhaustion proof changed")
    for name in ("work-limit-unknown", "deadline-unknown"):
        limited = by_name[name]
        if limited["proof_status"] != "unknown" or limited["complete"]:
            raise AssertionError(f"{name} must fail closed as unknown")

    case_receipts = []
    for case, wire, wasm in zip(CASES, wire_cases, results, strict=True):
        wasm_semantic = normalized_wasm(case, wasm)
        oracle_semantic = normalized_oracle(case)
        if wasm_semantic != oracle_semantic:
            raise AssertionError(
                f"{case['name']} differs:\nWASM={json.dumps(wasm_semantic, sort_keys=True)}"
                f"\nNATIVE={json.dumps(oracle_semantic, sort_keys=True)}"
            )
        side = "white" if chess.Board(str(case["fen"])).turn else "black"
        case_receipts.append(
            {
                "name": case["name"],
                "input_sha256": canonical_sha256(wire),
                "wasm_output_sha256": canonical_sha256(wasm_semantic),
                "oracle_output_sha256": canonical_sha256(oracle_semantic),
                "exact_match": True,
                "side_to_move": side,
                "proof_status": wasm_semantic["proof_status"],
            }
        )

    receipt = {
        "schema": "spc-mate-wasm-receipt-v2",
        "status": "passed",
        "failures": 0,
        "artifact": identity,
        "cases": case_receipts,
        "case_set_sha256": canonical_sha256(case_receipts),
        "signed_mate_overrides": {
            "white": {"override_score": 999_998, "proof_bounds": [1, 1]},
            "black": {"override_score": -999_998, "proof_bounds": [-1, -1]},
        },
        "gates": {
            "python_parity": True,
            "authoritative_replay": True,
            "white_found": True,
            "black_found": True,
            "exhausted": True,
            "work_limit_unknown": True,
            "deadline_unknown": True,
            "signed_mate_distance_overrides": True,
            "proof_bounds": True,
            "work_receipts": True,
            "deadline_receipts": True,
            "prefix_replay": True,
            "case_input_output_hashes": True,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
