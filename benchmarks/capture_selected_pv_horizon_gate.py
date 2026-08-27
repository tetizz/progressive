from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

import chess

from scottish_progressive import evaluation, model, series_mate
from scottish_progressive.model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    ProgressiveState,
    SeriesResult,
)
from scottish_progressive.native_subtree import native_subtree_available
from scottish_progressive.profiles import baseline_profile
from scottish_progressive.rules import play_series
from scottish_progressive.search import SearchLimits, SeriesSearcher


SCHEMA = "spc-selected-pv-horizon-match-path-receipt-v1"
ROOT_SAFETY_BUDGET = 3_000_000
EXPECTED_BEST = "b2b3"
EXPECTED_COMPLETED_DEPTH = 5
EXPECTED_PV = (
    "b2b3",
    "f7f5/e8f7",
    "c1b2/e2e3/f1c4",
    "e7e6/f5f4/f4e3/e3f2",
    "e1e2/e2f2/d1g4/f2e2/g4h5",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        check=True,
        capture_output=True,
        text=True,
        cwd=root,
    )
    return completed.stdout.strip()


def _state_payload(state: ProgressiveState) -> dict[str, object]:
    board = state.board
    return {
        "fen": board.fen(en_passant="fen"),
        "series_number": state.series_number,
        "quiet_series": state.quiet_series,
        "ep_targets": [chess.square_name(square) for square in state.ep_targets],
        "promoted_hex": f"{board.promoted:016x}",
        "chess960": board.chess960,
    }


def _series_payload(series: SeriesResult) -> dict[str, object]:
    return {
        "moves": list(series.moves),
        "machine_notation": series.machine_notation,
        "san": list(series.san),
        "transposition_count": series.transposition_count,
        "ended_by_check": series.ended_by_check,
        "outcome": None if series.outcome is None else series.outcome.value,
        "unused_moves": series.unused_moves,
        "final_state": _state_payload(series.final_state),
    }


def _replay_pv(
    root: ProgressiveState,
    pv: tuple[SeriesResult, ...],
) -> tuple[SeriesResult, ...]:
    cursor = root
    replayed: list[SeriesResult] = []
    for supplied in pv:
        authoritative = play_series(cursor, supplied.moves).with_transposition_count(
            supplied.transposition_count
        )
        if _series_payload(authoritative) != _series_payload(supplied):
            raise RuntimeError("principal variation failed authoritative replay")
        replayed.append(authoritative)
        cursor = authoritative.final_state
    return tuple(replayed)


def _module_is_under(module: object | None, root: Path) -> bool:
    path_value = getattr(module, "__file__", None)
    return bool(
        path_value is not None
        and Path(path_value).resolve().is_relative_to((root / "src").resolve())
    )


def _module_payload(module: object | None) -> dict[str, object]:
    path_value = getattr(module, "__file__", None)
    if path_value is None:
        return {
            "available": False,
            "path": None,
            "sha256": None,
            "source_identity": None,
        }
    path = Path(path_value).resolve()
    return {
        "available": True,
        "path": str(path),
        "sha256": _sha256(path),
        "source_identity": getattr(module, "SOURCE_IDENTITY", None),
    }


def _source_hashes(root: Path) -> dict[str, str]:
    paths = (
        "src/scottish_progressive/search.py",
        "src/scottish_progressive/selected_pv_horizon.py",
        "src/scottish_progressive/native_subtree.py",
        "src/scottish_progressive/native_subtree.cpp",
        "src/scottish_progressive/native_subtree.hpp",
        "src/scottish_progressive/_native_eval.cpp",
        "src/scottish_progressive/_native_mate.cpp",
    )
    return {relative: _sha256(root / relative) for relative in paths}


def capture(root: Path) -> dict[str, object]:
    root = root.resolve()
    git_top_level = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    git_top_level_matches = git_top_level == root
    git_head = _git(root, "rev-parse", "HEAD")
    git_branch = _git(root, "branch", "--show-current")
    git_clean = not _git(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    expected_eval_identity = evaluation._native_source_identity()
    expected_mate_identity = series_mate._native_mate_source_identity()
    loaded_eval_identity = getattr(evaluation._native_eval, "SOURCE_IDENTITY", None)
    loaded_mate_identity = getattr(series_mate._native_mate, "SOURCE_IDENTITY", None)
    eval_path_under_root = _module_is_under(evaluation._native_eval, root)
    mate_path_under_root = _module_is_under(series_mate._native_mate, root)
    source_hashes_before = _source_hashes(root)
    native_eval_before = _module_payload(evaluation._native_eval)
    native_mate_before = _module_payload(series_mate._native_mate)
    computed_fingerprint_before = model._source_fingerprint()
    profile = baseline_profile()
    limits = SearchLimits(
        depth_series=8,
        max_series_per_node=32,
        time_limit_seconds=120,
        max_generation_positions=4_000_000_000,
        native_threads=16,
        collect_all_root_scores=False,
    )
    searcher = SeriesSearcher(limits, profile)
    probe_trace: list[dict[str, object]] = []
    original_probe = searcher._selected_pv_horizon_probe

    def traced_probe(state: ProgressiveState):
        safety_before = searcher.stats.root_safety_screen_positions
        total_before = searcher.stats.generation_positions
        started = time.perf_counter()
        probe = original_probe(state)
        probe_trace.append(
            {
                "sequence": len(probe_trace) + 1,
                "leaf": _state_payload(state),
                "status": probe.status.value,
                "message": probe.message,
                "positions_visited": probe.positions_visited,
                "moves_generated": probe.moves_generated,
                "work_used": probe.positions_visited + probe.moves_generated,
                "transpositions_merged": probe.transpositions_merged,
                "checking_series": probe.checking_series,
                "checkmates": probe.checkmates,
                "peak_frontier": probe.peak_frontier,
                "max_depth_reached": probe.max_depth_reached,
                "elapsed_seconds": time.perf_counter() - started,
                "root_safety_work_before": safety_before,
                "root_safety_work_after": (
                    searcher.stats.root_safety_screen_positions
                ),
                "total_work_before": total_before,
                "total_work_after": searcher.stats.generation_positions,
                "mate_series": (
                    None if probe.series is None else _series_payload(probe.series)
                ),
            }
        )
        return probe

    searcher._selected_pv_horizon_probe = traced_probe
    initial = ProgressiveState.initial()
    result = searcher.run(initial)
    replayed_pv = _replay_pv(initial, result.principal_variation)
    final_leaf = (
        None
        if not replayed_pv
        else _state_payload(replayed_pv[-1].final_state)
    )
    final_leaf_exhausted = any(
        item["status"] == "exhausted" and item["leaf"] == final_leaf
        for item in probe_trace
    )
    git_top_level_after = Path(
        _git(root, "rev-parse", "--show-toplevel")
    ).resolve()
    git_head_after = _git(root, "rev-parse", "HEAD")
    git_branch_after = _git(root, "branch", "--show-current")
    git_clean_after = not _git(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    source_hashes_after = _source_hashes(root)
    native_eval_after = _module_payload(evaluation._native_eval)
    native_mate_after = _module_payload(series_mate._native_mate)
    computed_fingerprint_after = model._source_fingerprint()
    checks = {
        "git_tree_clean_before_capture": git_clean,
        "git_tree_clean_after_capture": git_clean_after,
        "git_top_level_matches_capture_root": git_top_level_matches,
        "git_candidate_stable_during_capture": (
            git_head_after == git_head
            and git_branch_after == git_branch
            and git_top_level_after == git_top_level == root
        ),
        "source_files_stable_during_capture": (
            source_hashes_after == source_hashes_before
        ),
        "native_artifacts_stable_during_capture": (
            native_eval_after == native_eval_before
            and native_mate_after == native_mate_before
        ),
        "loaded_engine_fingerprint_matches_stable_source": (
            computed_fingerprint_before
            == computed_fingerprint_after
            == ENGINE_SOURCE_FINGERPRINT
        ),
        "native_subtree_source_matched": (
            native_subtree_available()
            and loaded_eval_identity == expected_eval_identity
            and eval_path_under_root
        ),
        "native_mate_source_matched": (
            loaded_mate_identity == expected_mate_identity
            and mate_path_under_root
        ),
        "selected_b3": (
            result.best_series is not None
            and result.best_series.machine_notation == EXPECTED_BEST
        ),
        "completed_depth_five": result.completed_depth == EXPECTED_COMPLETED_DEPTH,
        "exact_expected_principal_variation": (
            tuple(series.machine_notation for series in replayed_pv) == EXPECTED_PV
        ),
        "principal_variation_authoritatively_replayed": (
            tuple(_series_payload(series) for series in replayed_pv)
            == tuple(_series_payload(series) for series in result.principal_variation)
        ),
        "requested_depth_eight": result.requested_depth == 8,
        "timeout_only_after_certified_depth_five": (
            result.timed_out
            and result.completed_depth == EXPECTED_COMPLETED_DEPTH
            and result.requested_depth > result.completed_depth
        ),
        "work_limit_not_reached": not result.work_limit_reached,
        "no_unknown_horizon_probe": (
            result.stats.selected_pv_horizon_unknown == 0
        ),
        "unsafe_candidate_repaired": (
            result.stats.selected_pv_horizon_native_repairs >= 1
        ),
        "unsafe_candidate_vetoed": (
            result.stats.selected_pv_horizon_candidate_vetoes >= 1
        ),
        "selected_horizon_exhaustively_certified": (
            final_leaf is not None
            and final_leaf_exhausted
            and result.best_series is not None
            and result.best_series.machine_notation == EXPECTED_BEST
        ),
        "selected_root_not_vetoed_at_return": (
            result.best_series is not None
            and result.best_series.machine_notation
            not in searcher._selected_pv_root_vetoes
        ),
        "root_safety_budget_respected": (
            result.stats.root_safety_screen_positions <= ROOT_SAFETY_BUDGET
        ),
    }
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "command": (
            "python benchmarks/capture_selected_pv_horizon_gate.py "
            "--output benchmarks/results/selected-pv-horizon-match-path-v1.json"
        ),
        "source": {
            "git_head": git_head,
            "git_head_after": git_head_after,
            "git_branch": git_branch,
            "git_branch_after": git_branch_after,
            "git_top_level": str(git_top_level),
            "git_top_level_after": str(git_top_level_after),
            "capture_root": str(root),
            "git_tree_clean_before_capture": git_clean,
            "git_tree_clean_after_capture": git_clean_after,
            "engine_version": ENGINE_VERSION,
            "engine_source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
            "computed_source_fingerprint_before": computed_fingerprint_before,
            "computed_source_fingerprint_after": computed_fingerprint_after,
            "source_files_sha256_before": source_hashes_before,
            "source_files_sha256_after": source_hashes_after,
        },
        "runtime": {
            "python": sys.version,
            "platform": platform.platform(),
            "native_eval_before": native_eval_before,
            "native_eval_after": native_eval_after,
            "native_eval_expected_source_identity": expected_eval_identity,
            "native_eval_path_under_worktree": eval_path_under_root,
            "native_mate_before": native_mate_before,
            "native_mate_after": native_mate_after,
            "native_mate_expected_source_identity": expected_mate_identity,
            "native_mate_path_under_worktree": mate_path_under_root,
            "native_mate_runtime_identity": (
                series_mate.native_mate_runtime_identity()
            ),
        },
        "controls": {
            "state": _state_payload(initial),
            "requested_depth": limits.depth_series,
            "max_series_per_node": limits.max_series_per_node,
            "time_limit_seconds": limits.time_limit_seconds,
            "max_generation_positions": limits.max_generation_positions,
            "native_threads": limits.native_threads,
            "collect_all_root_scores": limits.collect_all_root_scores,
            "profile_id": profile.profile_id,
            "profile_name": profile.name,
            "root_safety_budget": ROOT_SAFETY_BUDGET,
        },
        "result": {
            "best_series": (
                None if result.best_series is None else result.best_series.machine_notation
            ),
            "score": result.score,
            "requested_depth": result.requested_depth,
            "completed_depth": result.completed_depth,
            "timed_out": result.timed_out,
            "timeout_scope": (
                "deeper-iteration-after-certified-d5"
                if checks["timeout_only_after_certified_depth_five"]
                else "other"
            ),
            "work_limit_reached": result.work_limit_reached,
            "root_scores_complete": result.root_scores_complete,
            "exact_width": result.exact_width,
            "proof": result.proof,
            "elapsed_seconds": result.elapsed_seconds,
            "principal_variation": [
                _series_payload(series) for series in result.principal_variation
            ],
        },
        "selected_pv_horizon_probe_trace": probe_trace,
        "stats": asdict(result.stats),
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    receipt = capture(root)
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii", newline="\n")
    print(json.dumps({"passed": receipt["passed"], "path": str(output), "sha256": digest}))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
