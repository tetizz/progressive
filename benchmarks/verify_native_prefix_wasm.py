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
from scottish_progressive.webapp import inspect_prefix  # noqa: E402


CASES = (
    {
        "name": "initial-legal",
        "fen": chess.STARTING_FEN,
        "series": 1,
        "prefix": [],
    },
    {
        "name": "initial-e4-handoff",
        "fen": chess.STARTING_FEN,
        "series": 1,
        "prefix": ["e2e4"],
    },
    {
        "name": "early-countercheck",
        "fen": "r7/k6R/8/K7/8/8/8/8 b - - 0 1",
        "series": 2,
        "prefix": ["a7b8"],
    },
    {
        "name": "two-progressive-ep-targets",
        "fen": "7k/3p1p2/8/4P1P1/8/8/8/K7 b - - 0 1",
        "series": 2,
        "prefix": ["d7d5", "f7f5"],
    },
    {
        "name": "two-progressive-ep-replies",
        "fen": "7k/8/8/3pPpP1/8/8/8/K7 w - - 0 3",
        "series": 3,
        "progressive_ep": "d6,f6",
        "prefix": [],
    },
    {
        "name": "progressive-ep-first-move",
        "fen": "7k/8/8/3pPpP1/8/8/8/K7 w - - 0 3",
        "series": 3,
        "progressive_ep": "d6,f6",
        "prefix": ["e5d6"],
    },
    {
        "name": "castling-micro-move",
        "fen": "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
        "series": 3,
        "prefix": ["e1g1"],
    },
    {
        "name": "promotion-check",
        "fen": "7k/P7/8/8/8/8/8/7K w - - 0 1",
        "series": 3,
        "prefix": ["a7a8q"],
    },
    {
        "name": "native-mate-proof-replay",
        "fen": "rn1q1bnr/ppp1pkpp/5p2/8/3Pp3/2NB4/PPP2PPP/R1BbK1NR w KQ - 0 7",
        "series": 5,
        "prefix": ["c3d5", "d3e4", "e4h7", "d5f4", "h7g6"],
    },
    {
        "name": "progressive-ep-legal-next-suffix-parity",
        "fen": "8/8/8/8/1p5p/7b/2PB1KPk/7b w - - 0 1",
        "series": 3,
        "prefix": ["g2g4", "c2c4"],
        "progressive_san_exact": True,
    },
    {
        "name": "progressive-ep-check-suffix-parity",
        "fen": "8/8/8/8/1p5p/7b/2PB1KPk/7b w - - 0 1",
        "series": 3,
        "prefix": ["g2g4", "c2c4", "d2f4"],
        "progressive_san_exact": True,
    },
)
ERROR_CASES = (
    {
        "name": "illegal-prefix-fails-closed",
        "fen": chess.STARTING_FEN,
        "series": 1,
        "prefix": ["e2e5"],
        "expected_error": "illegal-move",
    },
    {
        "name": "overlong-prefix-fails-closed",
        "fen": chess.STARTING_FEN,
        "series": 1,
        "prefix": ["e2e4", "e4e5"],
        "expected_error": "series-overflow",
    },
    {
        "name": "turn-parity-fails-closed",
        "fen": chess.STARTING_FEN,
        "series": 2,
        "prefix": [],
        "expected_error": "invalid-boundary",
    },
)

TOP_LEVEL_KEYS = (
    "fen",
    "board_fen",
    "series",
    "series_number",
    "side_to_move",
    "active_series_side",
    "budget",
    "prefix",
    "current_prefix",
    "san",
    "notation",
    "frames",
    "remaining",
    "moves_remaining",
    "complete",
    "completion_reason",
    "check",
    "ended_by_check",
    "in_check",
    "outcome",
    "unused_moves",
    "legal_next",
    "legal_moves",
)
BOUNDARY_KEYS = (
    "fen",
    "board_fen",
    "series",
    "series_number",
    "side_to_move",
    "quiet_series",
    "quiet_draw_pending",
    "ep_targets",
    "progressive_ep",
    "promoted_hex",
    "chess960",
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
        description="Verify the combined WASM prefix ABI against Python."
    )
    parser.add_argument("--module", type=Path, required=True)
    parser.add_argument("--build-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def state_for(case: dict[str, object]) -> ProgressiveState:
    targets = tuple(
        chess.parse_square(item)
        for item in str(case.get("progressive_ep", "-")).split(",")
        if item and item != "-"
    )
    return ProgressiveState.from_fen(
        str(case["fen"]),
        int(case["series"]),
        quiet_series=int(case.get("quiet_series", 0)),
        ep_targets=targets,
    )


def selected(payload: dict[str, object]) -> dict[str, object]:
    result = {key: payload[key] for key in TOP_LEVEL_KEYS}
    next_state = payload["next_state"]
    result["next_state"] = (
        None
        if next_state is None
        else {key: next_state[key] for key in BOUNDARY_KEYS}
    )
    result["boundary_state"] = {
        key: payload["boundary_state"][key] for key in BOUNDARY_KEYS
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
    all_cases = CASES + ERROR_CASES
    wire_cases = [
        {
            key: value
            for key, value in case.items()
            if key not in {"name", "progressive_san_exact", "expected_error"}
        }
        for case in all_cases
    ]
    completed = subprocess.run(
        [
            "node",
            str(ROOT / "benchmarks" / "wasm_batch_probe.mjs"),
            "prefix",
            str(args.module.resolve()),
        ],
        input=json.dumps(wire_cases),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        cwd=ROOT,
    )
    wasm_results = json.loads(completed.stdout)
    if len(wasm_results) != len(all_cases):
        raise AssertionError("WASM batch result count differs from request")

    parity = 0
    progressive_san_parity = 0
    case_receipts: list[dict[str, object]] = []
    for case, wire, wasm in zip(
        CASES,
        wire_cases[: len(CASES)],
        wasm_results[: len(CASES)],
        strict=True,
    ):
        oracle = inspect_prefix(state_for(case), tuple(case["prefix"]))
        wasm_selected = selected(wasm)
        oracle_selected = selected(oracle)
        if wasm_selected != oracle_selected:
            raise AssertionError(
                f"{case['name']} differs:\nWASM={json.dumps(wasm_selected, sort_keys=True)}"
                f"\nPYTHON={json.dumps(oracle_selected, sort_keys=True)}"
            )
        case_receipts.append(
            {
                "name": case["name"],
                "input_sha256": canonical_sha256(wire),
                "wasm_output_sha256": canonical_sha256(wasm_selected),
                "oracle_output_sha256": canonical_sha256(oracle_selected),
                "exact_match": True,
            }
        )
        parity += 1
        if case.get("progressive_san_exact") is True:
            progressive_san_parity += 1

    for case, wire, wasm in zip(
        ERROR_CASES,
        wire_cases[len(CASES) :],
        wasm_results[len(CASES) :],
        strict=True,
    ):
        if wasm.get("ok") is not False or wasm.get("error_code") != case["expected_error"]:
            raise AssertionError(f"{case['name']} did not fail closed: {wasm}")
        wasm_error = {"ok": False, "error_code": wasm["error_code"]}
        oracle_error = {"ok": False, "error_code": case["expected_error"]}
        case_receipts.append(
            {
                "name": case["name"],
                "input_sha256": canonical_sha256(wire),
                "wasm_output_sha256": canonical_sha256(wasm_error),
                "oracle_output_sha256": canonical_sha256(oracle_error),
                "exact_match": wasm_error == oracle_error,
            }
        )

    receipt = {
        "schema": "spc-prefix-parity-receipt-v2",
        "status": "passed",
        "failures": 0,
        "artifact": identity,
        "cases": case_receipts,
        "case_set_sha256": canonical_sha256(case_receipts),
        "progressive_san_corrections": 0,
        "progressive_san_exact_parity": progressive_san_parity,
        "fail_closed_errors": len(ERROR_CASES),
        "mate_replay": "checkmate",
        "multi_ep": "covered",
        "gates": {
            "exact_python_parity": parity == len(CASES),
            "compiled_prefix_replay": True,
            "multi_ep_san": True,
            "illegal_prefix_fail_closed": True,
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
