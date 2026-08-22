from __future__ import annotations

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
LIVE_S5 = "rn1q1bnr/ppp1pkpp/5p2/8/3Pp3/2NB4/PPP2PPP/R1BbK1NR w KQ - 0 7"
CASES = (
    {"name": "live-s5-found", "fen": LIVE_S5, "series": 5},
    {
        "name": "long-s17-found",
        "fen": "5Q1Q/8/8/k7/8/8/4K3/8 w - - 16 73",
        "series": 17,
    },
    {
        "name": "bare-kings-exhausted",
        "fen": "8/8/8/8/8/2k5/8/K7 w - - 0 1",
        "series": 3,
    },
    {
        "name": "position-limit-unknown",
        "fen": LIVE_S5,
        "series": 5,
        "max_positions": 1,
    },
    {
        "name": "work-limit-unknown",
        "fen": LIVE_S5,
        "series": 5,
        "max_work": 100,
    },
)


def main() -> int:
    wire_cases = [
        {key: value for key, value in case.items() if key != "name"}
        for case in CASES
    ]
    completed = subprocess.run(
        ["node", str(ROOT / "mate_batch_probe.mjs")],
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
    by_name = {
        case["name"]: result
        for case, result in zip(CASES, results, strict=True)
    }

    live = by_name["live-s5-found"]
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

    long = by_name["long-s17-found"]
    if long["proof_status"] != "found" or long["moves"] != ["h8b2", "f8a3"]:
        raise AssertionError("S17 mate result changed")
    exhausted = by_name["bare-kings-exhausted"]
    if exhausted["proof_status"] != "exhausted" or not exhausted["complete"]:
        raise AssertionError("bare-kings exhaustion proof changed")
    for name in ("position-limit-unknown", "work-limit-unknown"):
        limited = by_name[name]
        if limited["proof_status"] != "unknown" or limited["complete"]:
            raise AssertionError(f"{name} must fail closed as unknown")

    print(
        json.dumps(
            {
                "schema": "spc-mate-wasm-receipt-v1",
                "cases": len(CASES),
                "found": 2,
                "exhausted": 1,
                "unknown": 2,
                "live_s5_stats": live["stats"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
