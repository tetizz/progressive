from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import platform

from .evaluation import classify_score
from .model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    RULESET_VERSION,
    ProgressiveState,
    SeriesResult,
)
from .notation import format_principal_variation
from .rules import GenerationStats, generate_series, play_series
from .search import SearchLimits, SearchResult, analyze


@dataclass(frozen=True, slots=True)
class OpeningAnalysis:
    rank: int
    move_uci: str
    move_san: str
    score: int
    classification: str
    proven_result: str | None
    best_black_series: str | None
    best_black_notation: str | None
    principal_variation: str
    completed_reply_depth: int
    exact_width: bool
    timed_out: bool
    confidence: str
    nodes: int
    raw_black_series: int
    unique_black_series: int
    intra_series_transpositions: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class OpeningRanking:
    generated_at: str
    engine_version: str
    source_fingerprint: str
    ruleset_version: str
    reply_depth: int
    total_series_horizon: int
    max_series_per_node: int | None
    time_limit_per_move: float | None
    all_searches_completed: bool
    all_reply_searches_exact: bool
    all_20_first_moves_present: bool
    initial_raw_series: int
    initial_unique_series: int
    total_nodes: int
    total_elapsed_seconds: float
    host: str
    results: tuple[OpeningAnalysis, ...]

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["method"] = (
            "Each legal White first move is fixed, then a deterministic "
            "series-level minimax searches Black's replies. One ply is one "
            "complete series. Leaf values are progressive-specific heuristics."
        )
        data["provisionality"] = (
            "Rankings are depth-limited and are not objective opening proofs."
        )
        return data


def rank_initial_moves(
    *,
    reply_depth: int = 1,
    max_series_per_node: int | None = None,
    time_limit_per_move: float | None = None,
) -> OpeningRanking:
    initial = ProgressiveState.initial()
    generation = GenerationStats()
    first_moves = generate_series(initial, stats=generation)
    rows: list[tuple[SeriesResult, SearchResult]] = []
    for first in first_moves:
        result = analyze(
            first.final_state,
            SearchLimits(
                depth_series=reply_depth,
                max_series_per_node=max_series_per_node,
                time_limit_seconds=time_limit_per_move,
            ),
        )
        rows.append((first, result))

    rows.sort(key=lambda item: (-item[1].score, item[0].machine_notation))
    analyses: list[OpeningAnalysis] = []
    for rank, (first, result) in enumerate(rows, start=1):
        pv = (first,) + result.principal_variation
        analyses.append(
            OpeningAnalysis(
                rank=rank,
                move_uci=first.moves[0],
                move_san=first.san[0],
                score=result.score,
                classification=classify_score(result.score, forced=result.forced),
                proven_result=result.forced,
                best_black_series=(
                    result.best_series.machine_notation if result.best_series else None
                ),
                best_black_notation=(
                    result.best_series.notation if result.best_series else None
                ),
                principal_variation=format_principal_variation(1, pv),
                completed_reply_depth=result.completed_depth,
                exact_width=result.exact_width,
                timed_out=result.timed_out,
                confidence=result.confidence,
                nodes=result.stats.nodes,
                raw_black_series=result.stats.generated_raw_series,
                unique_black_series=result.stats.generated_unique_series,
                intra_series_transpositions=result.stats.intra_series_transpositions,
                elapsed_seconds=result.elapsed_seconds,
            )
        )

    return OpeningRanking(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        engine_version=ENGINE_VERSION,
        source_fingerprint=ENGINE_SOURCE_FINGERPRINT,
        ruleset_version=RULESET_VERSION,
        reply_depth=reply_depth,
        total_series_horizon=1 + reply_depth,
        max_series_per_node=max_series_per_node,
        time_limit_per_move=time_limit_per_move,
        all_searches_completed=all(
            not result.timed_out and result.completed_depth == reply_depth
            for _, result in rows
        ),
        all_reply_searches_exact=all(result.exact_width for _, result in rows),
        all_20_first_moves_present=len(first_moves) == 20,
        initial_raw_series=generation.raw_series,
        initial_unique_series=generation.unique_series,
        total_nodes=sum(result.stats.nodes for _, result in rows),
        total_elapsed_seconds=sum(result.elapsed_seconds for _, result in rows),
        host=f"{platform.system()} {platform.release()} / Python {platform.python_version()}",
        results=tuple(analyses),
    )


def ranking_markdown(ranking: OpeningRanking) -> str:
    if not ranking.all_searches_completed:
        width = "incomplete/time-limited"
    elif ranking.all_reply_searches_exact:
        width = "exhaustive"
    else:
        width = "selective"
    lines = [
        "# Initial-move ranking — Scottish Progressive Chess",
        "",
        f"Generated: `{ranking.generated_at}`<br>",
        f"Engine: `{ranking.engine_version}`<br>",
        f"Source fingerprint: `{ranking.source_fingerprint}`<br>",
        f"Rules profile: `{ranking.ruleset_version}`<br>",
        f"Search: `{width}`, {ranking.reply_depth} Black/continuation series ply after the fixed White move<br>",
        f"Total series horizon: `{ranking.total_series_horizon}`<br>",
        f"Nodes: `{ranking.total_nodes}`<br>",
        f"Summed analysis time: `{ranking.total_elapsed_seconds:.3f}s`",
        "",
        "> This is a depth-limited engine ranking, not a claim that the top move is objectively best. "
        "Every leaf that is not a proven terminal uses the current progressive-specific heuristic.",
        "",
        "| Rank | White move | Score | Classification | Best Black series | Unique / raw Black series | Confidence |",
        "|---:|:---|---:|:---|:---|---:|:---|",
    ]
    for item in ranking.results:
        best = (item.best_black_notation or "—").replace("|", "\\|")
        lines.append(
            f"| {item.rank} | `{item.move_san}` (`{item.move_uci}`) | {item.score:+d} | "
            f"{item.classification} | {best} | {item.unique_black_series} / "
            f"{item.raw_black_series} | {item.confidence} |"
        )

    lines.extend(["", "## Principal variations", ""])
    for item in ranking.results:
        lines.append(f"{item.rank}. **{item.move_san}** — {item.principal_variation}")

    lines.extend(
        [
            "",
            "## What this run establishes",
            "",
            "- All 20 orthodox-legal first moves were considered independently.",
        ]
    )
    if ranking.all_searches_completed:
        lines.append(
            "- Every requested Black reply search completed to the stated series depth."
        )
        lines.append(
            "- Every complete Black two-move series (including legal early checks) "
            "was generated before any optional high-level branch cap."
        )
    else:
        lines.append(
            "- One or more Black reply searches timed out or stopped before the "
            "requested depth; this artifact is an incomplete diagnostic, not valid "
            "comparative ranking evidence."
        )
    lines.extend(
        [
            "- Different intra-series move orders with identical full progressive state were merged and counted.",
            "- Scores, nodes, limits, source/rules versions, and exact series PVs are retained for reproduction.",
            "",
            "## What it does not establish",
            "",
            "- A heuristic score after Black's reply is not a forced-win/loss proof.",
            "- The ranking can change when White's three-move responses and later series are searched.",
            "- The current reach probe is bounded and evaluation weights have not yet been calibrated against a large expert game set.",
            "",
        ]
    )
    return "\n".join(lines)


def write_ranking(ranking: OpeningRanking, output_dir: str | Path) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = (
        "initial-opening-ranking"
        if ranking.all_searches_completed
        else "initial-opening-ranking-incomplete"
    )
    json_path = directory / f"{stem}.json"
    markdown_path = directory / f"{stem}.md"
    json_path.write_text(
        json.dumps(ranking.as_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(ranking_markdown(ranking), encoding="utf-8")
    return json_path, markdown_path


def deepen_initial_moves(
    moves: tuple[str, ...],
    *,
    reply_depth: int,
    max_series_per_node: int,
    time_limit_per_move: float | None = None,
) -> dict[str, object]:
    first_moves = {
        series.moves[0]: series
        for series in generate_series(ProgressiveState.initial())
    }
    unknown = sorted(set(moves) - set(first_moves))
    if unknown:
        raise ValueError(f"not legal initial moves: {', '.join(unknown)}")

    rows: list[dict[str, object]] = []
    for uci in moves:
        first = first_moves[uci]
        result = analyze(
            first.final_state,
            SearchLimits(
                depth_series=reply_depth,
                max_series_per_node=max_series_per_node,
                time_limit_seconds=time_limit_per_move,
            ),
        )
        rows.append(
            {
                "move_uci": uci,
                "move_san": first.san[0],
                "score": result.score,
                "classification": result.classification,
                "proven_result": result.forced,
                "confidence": result.confidence,
                "best_black_series": (
                    result.best_series.machine_notation if result.best_series else None
                ),
                "best_black_notation": (
                    result.best_series.notation if result.best_series else None
                ),
                "principal_variation": format_principal_variation(
                    1, (first,) + result.principal_variation
                ),
                "requested_reply_depth": reply_depth,
                "completed_reply_depth": result.completed_depth,
                "exact_width": result.exact_width,
                "timed_out": result.timed_out,
                "nodes": result.stats.nodes,
                "raw_generated_series": result.stats.generated_raw_series,
                "unique_generated_series": result.stats.generated_unique_series,
                "intra_series_transpositions": result.stats.intra_series_transpositions,
                "branch_caps": result.stats.branch_caps,
                "elapsed_seconds": result.elapsed_seconds,
                "alternatives": [
                    {
                        "black_series": item.series.machine_notation,
                        "notation": item.series.notation,
                        "score": item.score,
                    }
                    for item in result.alternatives[:10]
                ],
            }
        )
    rows.sort(key=lambda item: (-int(item["score"]), str(item["move_uci"])))
    all_searches_completed = all(
        not bool(row["timed_out"])
        and int(row["completed_reply_depth"]) == reply_depth
        for row in rows
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "engine_version": ENGINE_VERSION,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "ruleset_version": RULESET_VERSION,
        "moves": list(moves),
        "reply_depth": reply_depth,
        "total_series_horizon": 1 + reply_depth,
        "max_series_per_node": max_series_per_node,
        "time_limit_per_move": time_limit_per_move,
        "all_searches_completed": all_searches_completed,
        "selection_policy": (
            "Every legal series is generated and transposition-merged first. "
            "At each searched node, a deterministic cheap ordering retains at "
            "most max_series_per_node series for deeper evaluation."
        ),
        "warning": (
            "Selective results are hypotheses, not proof and not a complete "
            "ranking. A pruned reply may change the result."
        ),
        "results": rows,
    }


def write_deepening(
    payload: dict[str, object], output_dir: str | Path
) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    complete = bool(payload["all_searches_completed"])
    stem = (
        "selective-opening-deepening"
        if complete
        else "selective-opening-deepening-incomplete"
    )
    json_path = directory / f"{stem}.json"
    markdown_path = directory / f"{stem}.md"
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    lines = [
        "# Selective opening deepening",
        "",
        f"Generated: `{payload['generated_at']}`<br>",
        f"Engine: `{payload['engine_version']}`<br>",
        f"Source fingerprint: `{payload['source_fingerprint']}`<br>",
        f"Rules: `{payload['ruleset_version']}`<br>",
        f"Total series horizon: `{payload['total_series_horizon']}`<br>",
        f"Maximum retained series per node: `{payload['max_series_per_node']}`",
        f"Search completion: `{'complete' if complete else 'incomplete/time-limited'}`",
        "",
        f"> {payload['warning']}",
        "",
        "The cap is applied only after every legal series at that node has been generated and full-state transpositions have been merged.",
        "",
        "| Move | Score | Classification | Best tested Black series | PV | Depth | Status | Generated unique / raw | Time |",
        "|:---|---:|:---|:---|:---|---:|:---|---:|---:|",
    ]
    for row in payload["results"]:  # type: ignore[index]
        assert isinstance(row, dict)
        pv = str(row["principal_variation"]).replace("|", "\\|")
        notation = str(row["best_black_notation"] or "—").replace("|", "\\|")
        lines.append(
            f"| `{row['move_san']}` | {int(row['score']):+d} | {row['classification']} | "
            f"{notation} | {pv} | {row['completed_reply_depth']}/{row['requested_reply_depth']} | "
            f"{'timed out' if row['timed_out'] else 'complete'} | "
            f"{row['unique_generated_series']} / "
            f"{row['raw_generated_series']} | {float(row['elapsed_seconds']):.2f}s |"
        )
    lines.extend(["", "## Interpretation", ""])
    if complete:
        lines.append(
            "These runs include a searched White three-move response, so they "
            "repair the most obvious horizon weakness in the two-series "
            "baseline. They remain selective: neither a positive score nor a "
            "negative score is a forced result."
        )
    else:
        lines.append(
            "At least one requested search did not reach the stated depth. "
            "This file is an incomplete diagnostic and does not establish a "
            "deeper opening comparison. Its separate filename protects the "
            "last completed evidence artifact."
        )
    lines.append("")
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path


PUBLISHED_REPLY_CANDIDATES: dict[str, tuple[tuple[str, tuple[str, ...], str], ...]] = {
    "e2e4": (
        ("d5 / Nc6", ("d7d5", "b8c6"), "Italian opening-book prior"),
        ("d5 / d4", ("d7d5", "d5d4"), "Italian opening-book prior"),
        ("d5 / e5", ("d7d5", "e7e5"), "Italian opening-book prior"),
        ("e5 / f6", ("e7e5", "f7f6"), "community drawing hypothesis"),
        ("e5 / Nh6", ("e7e5", "g8h6"), "Italian opening-book prior"),
        ("e5 / Qe7", ("e7e5", "d8e7"), "Italian opening-book prior"),
        ("d5 / dxe4", ("d7d5", "d5e4"), "Italian opening-book prior"),
        ("e6 / Nf6", ("e7e6", "g8f6"), "Italian opening-book prior"),
        ("e5 / Qh4", ("e7e5", "d8h4"), "two-series engine baseline"),
        ("e6 / Ke7", ("e7e6", "e8e7"), "selective engine candidate"),
    ),
    "d2d4": (
        ("d5 / Nc6", ("d7d5", "b8c6"), "Italian opening-book prior"),
        ("c5 / cxd4", ("c7c5", "c5d4"), "Italian opening-book prior"),
        ("d5 / c6", ("d7d5", "c7c6"), "community drawing hypothesis"),
        ("d5 / h5", ("d7d5", "h7h5"), "Italian opening-book prior"),
        ("d6 / Nf6", ("d7d6", "g8f6"), "Italian opening-book prior"),
        ("e5 / exd4", ("e7e5", "e5d4"), "Italian opening-book prior"),
        ("f5 / Nf6", ("f7f5", "g8f6"), "Italian opening-book prior"),
        ("c5 / d5", ("c7c5", "d7d5"), "Italian opening-book prior"),
        ("e5 / e4", ("e7e5", "e5e4"), "community refutation candidate"),
        ("e5 / Bb4+", ("e7e5", "f8b4"), "two-series engine baseline"),
        ("e6 / Bb4+", ("e7e6", "f8b4"), "selective engine candidate"),
    ),
}

REPLY_SCREEN_LIMIT = 64


def compare_published_replies() -> dict[str, object]:
    first_moves = {
        series.moves[0]: series
        for series in generate_series(ProgressiveState.initial())
    }
    opening_rows: list[dict[str, object]] = []
    for first_uci, candidates in PUBLISHED_REPLY_CANDIDATES.items():
        first = first_moves[first_uci]
        replies: list[dict[str, object]] = []
        for label, moves, source in candidates:
            black = play_series(first.final_state, moves)
            result = analyze(
                black.final_state,
                SearchLimits(
                    depth_series=1,
                    max_series_per_node=REPLY_SCREEN_LIMIT,
                ),
            )
            replies.append(
                {
                    "black_label": label,
                    "black_series": black.machine_notation,
                    "black_notation": black.notation,
                    "source": source,
                    "score_after_best_white_response": result.score,
                    "classification": result.classification,
                    "proven_result": result.forced,
                    "best_white_series": (
                        result.best_series.machine_notation if result.best_series else None
                    ),
                    "best_white_notation": (
                        result.best_series.notation if result.best_series else None
                    ),
                    "principal_variation": format_principal_variation(
                        1, (first, black) + result.principal_variation
                    ),
                    "exact_white_response_width": result.exact_width,
                    "raw_white_series": result.stats.generated_raw_series,
                    "unique_white_series": result.stats.generated_unique_series,
                    "intra_series_transpositions": result.stats.intra_series_transpositions,
                    "nodes": result.stats.nodes,
                    "elapsed_seconds": result.elapsed_seconds,
                }
            )
        replies.sort(key=lambda row: (int(row["score_after_best_white_response"]), str(row["black_series"])))
        opening_rows.append(
            {
                "first_move_uci": first_uci,
                "first_move_san": first.san[0],
                "candidate_count": len(replies),
                "best_tested_black_candidate": replies[0]["black_series"],
                "results": replies,
            }
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "engine_version": ENGINE_VERSION,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "ruleset_version": RULESET_VERSION,
        "series_horizon": 3,
        "white_response_search": (
            "all legal White series generated and transposition-merged; "
            f"deterministic fast screen retains {REPLY_SCREEN_LIMIT} finalists "
            "for full progressive evaluation"
        ),
        "white_response_screen_limit": REPLY_SCREEN_LIMIT,
        "warning": (
            "Only named historical/engine candidate Black replies are compared. "
            "This is not exhaustive over all Black series and is not a proof."
        ),
        "openings": opening_rows,
    }


def write_reply_comparison(
    payload: dict[str, object], output_dir: str | Path
) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "published-reply-comparison.json"
    markdown_path = directory / "published-reply-comparison.md"
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    lines = [
        "# Published and engine reply comparison",
        "",
        f"Generated: `{payload['generated_at']}`<br>",
        f"Engine: `{payload['engine_version']}`<br>",
        f"Source fingerprint: `{payload['source_fingerprint']}`<br>",
        f"Rules: `{payload['ruleset_version']}`",
        "Horizon: White 1 move, fixed Black candidate 2-move series, then a screened White 3-move response.",
        "",
        f"> {payload['warning']}",
        "",
    ]
    for opening in payload["openings"]:  # type: ignore[index]
        assert isinstance(opening, dict)
        lines.extend(
            [
                f"## 1.{opening['first_move_san']}",
                "",
                f"Lower scores are better for Black. Every legal White series is generated; the top {payload['white_response_screen_limit']} deterministic screening finalists receive the full evaluation.",
                "",
                "| Black series | Source | Score after White's best response | Best White series | Unique / raw White series |",
                "|:---|:---|---:|:---|---:|",
            ]
        )
        for row in opening["results"]:  # type: ignore[index]
            assert isinstance(row, dict)
            lines.append(
                f"| {row['black_notation']} | {row['source']} | "
                f"{int(row['score_after_best_white_response']):+d} | "
                f"{row['best_white_notation']} | {row['unique_white_series']} / "
                f"{row['raw_white_series']} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Interpretation limit",
            "",
            "The comparison is stronger than the two-series baseline because it generates White's entire immediate tactical series set for each named reply. It still cannot establish the best Black reply: unlisted Black series remain possible, the White finalist screen is selective, and leaf values are heuristic.",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path
