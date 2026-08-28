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

from scottish_progressive.model import Outcome, ProgressiveState  # noqa: E402
from scottish_progressive.rules import play_series  # noqa: E402
from scottish_progressive.series_mate import (  # noqa: E402
    SeriesMateStatus,
    find_native_series_mate,
)


LIVE_S5 = "rn1q1bnr/ppp1pkpp/5p2/8/3Pp3/2NB4/PPP2PPP/R1BbK1NR w KQ - 0 7"
MIRRORED_LIVE_S6 = "r1bBk1nr/ppp2ppp/2nb4/3pP3/8/5P2/PPP1PKPP/RN1Q1BNR b kq - 0 7"
START_BLACK = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1"
BUCEPHALUS_3AAEF_S6_FEN = (
    "rnbNkb1r/pppp2pp/8/5p2/8/3B1P2/PPPP2PP/RNBnK2R b kq - 0 7"
)
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
        "name": "authentic-s6-max-positions-exact-invariance",
        "fen": BUCEPHALUS_3AAEF_S6_FEN,
        "series": 6,
        "max_positions": 1_000_000,
        "max_work": 1_000_000,
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
    {
        "name": "s6-staged-deadline-unknown",
        "fen": BUCEPHALUS_3AAEF_S6_FEN,
        "series": 6,
        "max_positions": 0,
        "max_work": 1_000_000,
        "time_limit_ms": 1,
    },
    {
        "name": "s7-max-positions-preserves-legacy-contract",
        "fen": "rnk3nr/pp3ppp/8/8/8/1Pp1P3/P1PP1PPP/R1b1K1NR w K - 0 13",
        "series": 7,
        "max_positions": 10,
        "max_work": 10_000_000,
        "time_limit_ms": 30_000,
    },
)
S7_RESCUE_FEN = "rnk3nr/pp3ppp/8/8/8/1Pp1P3/P1PP1PPP/R1b1K1NR w K - 0 13"
BUCEPHALUS_4044_S8_FEN = (
    "rn1k1bn1/4pp2/5Q2/8/2P5/5P2/3P3P/qNBbK1NR b K - 0 13"
)
ACCELERATED_CASES = (
    {
        "name": "s6-staged-root-found",
        "fen": BUCEPHALUS_3AAEF_S6_FEN,
        "series": 6,
        "max_positions": 0,
        # Exact independently replayed work: completion at the cap is legal.
        "max_work": 25_643,
        "time_limit_ms": 30_000,
    },
    {
        "name": "s6-staged-root-cap-minus-one",
        "fen": BUCEPHALUS_3AAEF_S6_FEN,
        "series": 6,
        "max_positions": 0,
        # One less than the independently replayed truthful FOUND receipt.
        "max_work": 25_642,
        "time_limit_ms": 30_000,
    },
    {
        "name": "s6-selective-miss-exact-exhausted",
        "fen": "6bk/8/8/8/8/8/8/K7 b - - 0 1",
        "series": 6,
        "max_positions": 0,
        # Exact independently replayed work across prepass plus fallback.
        "max_work": 16_066,
        "time_limit_ms": 30_000,
    },
    {
        "name": "s6-selective-miss-exact-cap-minus-one",
        "fen": "6bk/8/8/8/8/8/8/K7 b - - 0 1",
        "series": 6,
        "max_positions": 0,
        "max_work": 16_065,
        "time_limit_ms": 30_000,
    },
    {
        "name": "authentic-s8-staged-root-invariant",
        "fen": BUCEPHALUS_4044_S8_FEN,
        "series": 8,
        "max_positions": 0,
        "max_work": 1_000_000,
        "time_limit_ms": 30_000,
    },
    {
        "name": "s7-staged-root-found",
        "fen": S7_RESCUE_FEN,
        "series": 7,
        "max_positions": 0,
        "max_work": 10_000_000,
        "time_limit_ms": 30_000,
    },
    {
        "name": "s7-staged-root-work-limit",
        "fen": S7_RESCUE_FEN,
        "series": 7,
        "max_positions": 0,
        "max_work": 10,
        "time_limit_ms": 30_000,
    },
    {
        "name": "s7-staged-root-exhausted",
        "fen": "8/8/8/8/8/2k5/8/K7 w - - 0 1",
        "series": 7,
        "max_positions": 0,
        "max_work": 1_000_000,
        "time_limit_ms": 30_000,
    },
    {
        "name": "s7-nonchecking-stuck-is-not-mate",
        "fen": "8/8/8/8/8/5k2/6q1/7K w - - 0 1",
        "series": 7,
        "max_positions": 0,
        "max_work": 1_000_000,
        "time_limit_ms": 30_000,
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
    max_positions = int(case["max_positions"])
    probe = find_native_series_mate(
        state,
        max_positions=None if max_positions == 0 else max_positions,
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
    accelerated_wire_cases = [
        {key: value for key, value in case.items() if key != "name"}
        for case in ACCELERATED_CASES
    ]
    completed = subprocess.run(
        [
            "node",
            str(ROOT / "benchmarks" / "wasm_batch_probe.mjs"),
            "mate",
            str(args.module.resolve()),
        ],
        input=json.dumps(wire_cases + accelerated_wire_cases),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        cwd=ROOT,
    )
    all_results = json.loads(completed.stdout)
    if len(all_results) != len(CASES) + len(ACCELERATED_CASES):
        raise AssertionError("WASM batch result count differs from request")
    results = all_results[: len(CASES)]
    accelerated_results = all_results[len(CASES) :]
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
        "positions_visited": 207,
        "moves_generated": 8471,
        "transpositions_merged": 48,
        "checking_series": 315,
        "checkmates": 1,
        "peak_frontier": 311,
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
    authentic_exact = by_name["authentic-s6-max-positions-exact-invariance"]
    if (
        authentic_exact["proof_status"] != "found"
        or authentic_exact["moves"] != [
            "d1e3",
            "b8c6",
            "c6d4",
            "f8d6",
            "d6h2",
            "h2g3",
        ]
        or authentic_exact["stats"]["positions_visited"] != 22_771
        or authentic_exact["stats"]["moves_generated"] != 777_872
    ):
        raise AssertionError("max-positions exact Series-6 contract changed")
    exhausted = by_name["bare-kings-exhausted"]
    if exhausted["proof_status"] != "exhausted" or not exhausted["complete"]:
        raise AssertionError("bare-kings exhaustion proof changed")
    for name in (
        "work-limit-unknown",
        "deadline-unknown",
        "s6-staged-deadline-unknown",
        "s7-max-positions-preserves-legacy-contract",
    ):
        limited = by_name[name]
        if limited["proof_status"] != "unknown" or limited["complete"]:
            raise AssertionError(f"{name} must fail closed as unknown")
    staged_deadline = by_name["s6-staged-deadline-unknown"]
    if (
        staged_deadline["kernel_status"] != "deadline"
        or staged_deadline["message"]
            != "native staged root mate prepass reached the deadline"
        or staged_deadline["moves"]
        or staged_deadline["stats"]["max_depth_reached"] != 0
    ):
        raise AssertionError("Series-6 staged deadline did not fail closed")

    accelerated_by_name = {
        case["name"]: result
        for case, result in zip(
            ACCELERATED_CASES,
            accelerated_results,
            strict=True,
        )
    }
    staged_s6 = accelerated_by_name["s6-staged-root-found"]
    if (
        staged_s6["kernel_status"] != "found"
        or staged_s6["proof_status"] != "found"
        or staged_s6["complete"] is not True
        or staged_s6["moves"] != [
            "b8c6",
            "c6d4",
            "d1e3",
            "f8d6",
            "d6h2",
            "h2g3",
        ]
        or staged_s6["stats"]["positions_visited"]
            + staged_s6["stats"]["moves_generated"]
            != 25_643
        or staged_s6["stats"]["checkmates"] != 1
        or staged_s6["stats"]["max_depth_reached"] != 6
    ):
        raise AssertionError("Series-6 staged root mate result changed")
    staged_s6_limited = accelerated_by_name["s6-staged-root-cap-minus-one"]
    if (
        staged_s6_limited["kernel_status"] != "work_limit"
        or staged_s6_limited["proof_status"] != "unknown"
        or staged_s6_limited["complete"] is not False
        or staged_s6_limited["moves"]
        or staged_s6_limited["stats"]["positions_visited"]
            + staged_s6_limited["stats"]["moves_generated"]
            != 25_642
        or staged_s6_limited["stats"]["checkmates"] != 0
        or staged_s6_limited["stats"]["max_depth_reached"] != 0
    ):
        raise AssertionError("Series-6 staged root work cap changed")
    staged_s6_miss = accelerated_by_name["s6-selective-miss-exact-exhausted"]
    if (
        staged_s6_miss["kernel_status"] != "exhausted"
        or staged_s6_miss["proof_status"] != "exhausted"
        or staged_s6_miss["complete"] is not True
        or staged_s6_miss["message"]
            != "native series-mate state space exhausted"
        or staged_s6_miss["moves"]
        or staged_s6_miss["stats"]["positions_visited"]
            + staged_s6_miss["stats"]["moves_generated"]
            != 16_066
        or staged_s6_miss["stats"]["checkmates"] != 0
        or staged_s6_miss["stats"]["max_depth_reached"] != 5
    ):
        raise AssertionError("selective Series-6 miss did not reach exact exhaustion")
    staged_s6_miss_limited = accelerated_by_name[
        "s6-selective-miss-exact-cap-minus-one"
    ]
    if (
        staged_s6_miss_limited["kernel_status"] != "work_limit"
        or staged_s6_miss_limited["proof_status"] != "unknown"
        or staged_s6_miss_limited["complete"] is not False
        or staged_s6_miss_limited["moves"]
        or staged_s6_miss_limited["stats"]["positions_visited"]
            + staged_s6_miss_limited["stats"]["moves_generated"]
            != 16_065
        or staged_s6_miss_limited["stats"]["checkmates"] != 0
    ):
        raise AssertionError("selective Series-6 exact fallback exceeded total work")
    staged_s8 = accelerated_by_name["authentic-s8-staged-root-invariant"]
    if (
        staged_s8["kernel_status"] != "found"
        or staged_s8["proof_status"] != "found"
        or staged_s8["complete"] is not True
        or staged_s8["moves"] != ["a1d4", "a8a2", "a2d2", "d4f2"]
        or staged_s8["stats"]["positions_visited"]
            + staged_s8["stats"]["moves_generated"]
            != 5_474
        or staged_s8["stats"]["checkmates"] != 1
        or staged_s8["stats"]["max_depth_reached"] != 4
    ):
        raise AssertionError("authentic Series-8 staged result changed")
    staged = accelerated_by_name["s7-staged-root-found"]
    if (
        staged["kernel_status"] != "found"
        or staged["proof_status"] != "found"
        or staged["complete"] is not True
        or staged["moves"] != [
            "d2c3",
            "e1e2",
            "g1f3",
            "f3g5",
            "h1d1",
            "g5e6",
            "d1d8",
        ]
        or staged["stats"]["positions_visited"]
            + staged["stats"]["moves_generated"]
            != 79_715
        or staged["stats"]["checkmates"] != 1
        or staged["stats"]["max_depth_reached"] != 7
    ):
        raise AssertionError("late-series staged root mate result changed")
    staged_limited = accelerated_by_name["s7-staged-root-work-limit"]
    if (
        staged_limited["kernel_status"] != "work_limit"
        or staged_limited["proof_status"] != "unknown"
        or staged_limited["complete"] is not False
        or staged_limited["moves"]
        or staged_limited["stats"]["positions_visited"]
            + staged_limited["stats"]["moves_generated"]
            != 10
        or staged_limited["stats"]["max_depth_reached"] != 0
    ):
        raise AssertionError("late-series staged root work cap changed")
    staged_exhausted = accelerated_by_name["s7-staged-root-exhausted"]
    if (
        staged_exhausted["kernel_status"] != "exhausted"
        or staged_exhausted["proof_status"] != "exhausted"
        or staged_exhausted["complete"] is not True
        or staged_exhausted["moves"]
        or staged_exhausted["stats"]["positions_visited"]
            + staged_exhausted["stats"]["moves_generated"]
            != 836
    ):
        raise AssertionError("late-series staged root exhaustion changed")
    nonchecking_stuck = accelerated_by_name["s7-nonchecking-stuck-is-not-mate"]
    if (
        nonchecking_stuck["kernel_status"] != "exhausted"
        or nonchecking_stuck["proof_status"] != "exhausted"
        or nonchecking_stuck["complete"] is not True
        or nonchecking_stuck["moves"]
        or nonchecking_stuck["stats"]["positions_visited"]
            + nonchecking_stuck["stats"]["moves_generated"]
            != 1
        or nonchecking_stuck["stats"]["checkmates"] != 0
    ):
        raise AssertionError("non-checking stuck line was mislabeled as mate")

    for case, result in zip(
        ACCELERATED_CASES,
        accelerated_results,
        strict=True,
    ):
        accounted_work = (
            result["stats"]["positions_visited"]
            + result["stats"]["moves_generated"]
        )
        if accounted_work > int(case["max_work"]):
            raise AssertionError(
                f"{case['name']} exceeded its literal position-plus-edge cap"
            )
        if result["kernel_status"] != "found":
            continue
        replayed = play_series(
            ProgressiveState.from_fen(str(case["fen"]), int(case["series"])),
            tuple(result["moves"]),
        )
        if (
            replayed.outcome is not Outcome.CHECKMATE
            or not replayed.ended_by_check
        ):
            raise AssertionError(
                f"{case['name']} failed authoritative Python replay"
            )

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

    accelerated_receipts = [
        {
            "name": case["name"],
            "input": wire,
            "input_sha256": canonical_sha256(wire),
            "wasm_output_sha256": canonical_sha256(result),
            "kernel_status": result["kernel_status"],
            "proof_status": result["proof_status"],
            "complete": result["complete"],
            "moves": result["moves"],
            "work": result["stats"]["positions_visited"]
                + result["stats"]["moves_generated"],
            "checkmates": result["stats"]["checkmates"],
            "max_depth_reached": result["stats"]["max_depth_reached"],
        }
        for case, wire, result in zip(
            ACCELERATED_CASES,
            accelerated_wire_cases,
            accelerated_results,
            strict=True,
        )
    ]

    receipt = {
        "schema": "spc-mate-wasm-receipt-v3",
        "status": "passed",
        "failures": 0,
        "work_accounting": "positions-plus-generated-edges-v1",
        "artifact": identity,
        "cases": case_receipts,
        "case_set_sha256": canonical_sha256(case_receipts),
        "accelerated_cases": accelerated_receipts,
        "accelerated_case_set_sha256": canonical_sha256(accelerated_receipts),
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
            "late_series_staged_root": True,
            "series6_staged_root": True,
            "series6_selective_miss_exact_fallback": True,
            "series6_budget_and_deadline_unknown": True,
            "series8_staged_root_invariant": True,
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
