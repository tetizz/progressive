from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit
import webbrowser

import chess

from .database import TheoryDatabase
from .mate_proof_cache import (
    DEFAULT_MATE_PROOF_CACHE_CAPACITY,
    MateProofCache,
)
from .model import (
    ENGINE_SOURCE_FINGERPRINT,
    ENGINE_VERSION,
    RULESET_VERSION,
    ProgressiveState,
    SeriesResult,
)
from .move_quality import MoveQualityVerdict, QualitySubject, grade_move_quality
from .notation import format_principal_variation
from .profiles import EngineProfile, baseline_profile, load_profile
from .rules import (
    SeriesLegalityError,
    _finish_series,
    _legal_move_variants,
    _stuck_result,
)
from .search import MATE_SCORE, SearchLimits, SearchResult, analyze


MAX_REQUEST_BYTES = 256 * 1024
DEFAULT_ANALYSIS_SECONDS = 5.0
MAX_ANALYSIS_SECONDS = 30.0
DEFAULT_MAX_SERIES = 64
MAX_SERIES_CAP = 512
MAX_ANALYSIS_DEPTH = 8
MAX_ALTERNATIVES = 32
MAX_SERIES_NUMBER = 512
DEFAULT_GENERATION_POSITIONS = 500_000
MAX_GENERATION_POSITIONS = 4_000_000_000

PUBLIC_MAX_ANALYSIS_SECONDS = 30.0
PUBLIC_MAX_SERIES_CAP = 96
PUBLIC_MAX_ANALYSIS_DEPTH = 5
PUBLIC_MAX_GENERATION_POSITIONS = 4_000_000_000
LOCAL_NATIVE_THREADS = min(16, os.cpu_count() or 1)

REPORT_FILES = {
    "initial_ranking": "initial-opening-ranking.json",
    "selective_deepening": "selective-opening-deepening.json",
    "published_replies": "published-reply-comparison.json",
}


class APIError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True, slots=True)
class AnalysisRequestLimits:
    default_seconds: float = DEFAULT_ANALYSIS_SECONDS
    maximum_seconds: float = MAX_ANALYSIS_SECONDS
    default_max_series: int = DEFAULT_MAX_SERIES
    maximum_max_series: int = MAX_SERIES_CAP
    maximum_depth: int = MAX_ANALYSIS_DEPTH
    default_generation_positions: int = DEFAULT_GENERATION_POSITIONS
    maximum_generation_positions: int = MAX_GENERATION_POSITIONS
    maximum_alternatives: int = MAX_ALTERNATIVES
    native_threads: int = LOCAL_NATIVE_THREADS


LOCAL_ANALYSIS_LIMITS = AnalysisRequestLimits()
PUBLIC_ANALYSIS_LIMITS = AnalysisRequestLimits(
    default_seconds=2.0,
    maximum_seconds=PUBLIC_MAX_ANALYSIS_SECONDS,
    default_max_series=32,
    maximum_max_series=PUBLIC_MAX_SERIES_CAP,
    maximum_depth=PUBLIC_MAX_ANALYSIS_DEPTH,
    default_generation_positions=100_000,
    maximum_generation_positions=PUBLIC_MAX_GENERATION_POSITIONS,
    maximum_alternatives=4,
    native_threads=1,
)


@dataclass(frozen=True, slots=True)
class WebConfig:
    static_root: Path
    reports_dir: Path
    database_path: Path | None = None
    engine_profile: EngineProfile = field(default_factory=baseline_profile)
    request_limit: int = MAX_REQUEST_BYTES
    public_origin: str | None = None
    allowed_authority: str | None = None
    allowed_cors_origin: str | None = None
    analysis_limits: AnalysisRequestLimits = LOCAL_ANALYSIS_LIMITS
    analysis_concurrency: int = 2
    runtime_cpu_count: str | None = None
    runtime_cpu_count_source: str = "unavailable"
    native_threads_policy: str = "local-configured"
    mate_proof_cache: MateProofCache | None = None


def _public_analysis_runtime(
    environment: Mapping[str, str] | None = None,
) -> tuple[AnalysisRequestLimits, str | None, str]:
    """Report Render's allocation while retaining the proven serial native path.

    Parallel native calls currently initialize a persistent pool from
    host-visible hardware rather than the container CPU quota. Public requests
    therefore stay at one native thread even when Render reports a larger
    allocation, avoiding an unreported worker pool until its lazy sizing is
    separately proven.
    """

    variables = os.environ if environment is None else environment
    raw_cpu_count = variables.get("RENDER_CPU_COUNT")
    if raw_cpu_count is None:
        return PUBLIC_ANALYSIS_LIMITS, None, "conservative-fallback"
    reported_cpu_count = raw_cpu_count.strip()
    try:
        cpu_count = Decimal(reported_cpu_count)
    except InvalidOperation:
        return PUBLIC_ANALYSIS_LIMITS, None, "conservative-fallback"
    if not reported_cpu_count or not cpu_count.is_finite() or cpu_count <= 0:
        return PUBLIC_ANALYSIS_LIMITS, None, "conservative-fallback"
    return (
        PUBLIC_ANALYSIS_LIMITS,
        reported_cpu_count,
        "RENDER_CPU_COUNT",
    )


class AnalysisBoardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        config: WebConfig,
    ) -> None:
        self.config = config
        self.mate_proof_cache = config.mate_proof_cache or MateProofCache()
        # Keep a local analysis request from spawning an unbounded collection
        # of CPU-heavy searches through the threaded HTTP server.
        self.analysis_gate = threading.BoundedSemaphore(config.analysis_concurrency)
        super().__init__(server_address, handler)


def _default_static_root() -> Path:
    return Path(__file__).resolve().parent / "web" / "static"


def _default_reports_dir() -> Path:
    module_path = Path(__file__).resolve()
    source_root = module_path.parents[2]
    source_reports = source_root / "reports"
    source_module = source_root / "src" / "scottish_progressive" / "webapp.py"
    if source_module.resolve() == module_path and source_reports.is_dir():
        return source_reports
    packaged_reports = module_path.parent / "reports"
    if packaged_reports.is_dir():
        return packaged_reports
    return Path.cwd() / "reports"


def _normalize_public_origin(value: str) -> tuple[str, str]:
    """Return a canonical public origin and its HTTP authority."""
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "public_origin must be an https origin without credentials, path, query, or fragment"
        )
    authority = parsed.netloc.lower()
    return f"{parsed.scheme.lower()}://{authority}", authority


def ui_source_fingerprint(static_root: Path) -> str:
    """Fingerprint the packaged browser assets independently of engine code."""
    digest = hashlib.sha256()
    for path in sorted(
        (item for item in static_root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(static_root).as_posix(),
    ):
        relative = path.relative_to(static_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _require_int(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise APIError(422, "invalid-field", f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise APIError(
            422,
            "invalid-field",
            f"{name} must be between {minimum} and {maximum}",
        )
    return value


def _require_number(
    value: object,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise APIError(422, "invalid-field", f"{name} must be a number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise APIError(
            422,
            "invalid-field",
            f"{name} must be between {minimum:g} and {maximum:g}",
        )
    return number


def _parse_ep_targets(value: object) -> tuple[int, ...]:
    if value is None or value == "" or value == "-":
        return ()
    raw: Sequence[object]
    if isinstance(value, str):
        raw = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        raw = value
    else:
        raise APIError(
            422,
            "invalid-field",
            "ep_targets must be a list of square names or a comma-separated string",
        )
    if len(raw) > 8:
        raise APIError(422, "invalid-field", "ep_targets contains too many squares")
    targets: list[int] = []
    for item in raw:
        if not isinstance(item, str):
            raise APIError(422, "invalid-field", "each en-passant target must be a square name")
        try:
            targets.append(chess.parse_square(item.strip().lower()))
        except ValueError as error:
            raise APIError(
                422,
                "invalid-field",
                f"invalid en-passant target {item!r}",
            ) from error
    return tuple(targets)


def _state_payload_source(payload: Mapping[str, object]) -> dict[str, object]:
    nested = payload.get("state")
    if nested is None:
        return dict(payload)
    if not isinstance(nested, Mapping):
        raise APIError(422, "invalid-field", "state must be a JSON object")
    merged = dict(nested)
    for key, value in payload.items():
        if key != "state":
            merged[key] = value
    return merged


def state_from_payload(payload: Mapping[str, object]) -> ProgressiveState:
    source = _state_payload_source(payload)
    fen = source.get("fen", source.get("board_fen", chess.STARTING_FEN))
    if not isinstance(fen, str) or not fen.strip():
        raise APIError(422, "invalid-field", "fen must be a non-empty string")
    if len(fen) > 512 or "\x00" in fen or "\n" in fen or "\r" in fen:
        raise APIError(422, "invalid-field", "fen is malformed or too long")
    series = _require_int(
        source.get("series", source.get("series_number", 1)),
        "series",
        minimum=1,
        maximum=MAX_SERIES_NUMBER,
    )
    quiet_series = _require_int(
        source.get("quiet_series", 0),
        "quiet_series",
        minimum=0,
        maximum=1_000_000,
    )
    ep_value = source.get("ep_targets", source.get("progressive_ep"))
    chess960 = source.get("chess960", False)
    if not isinstance(chess960, bool):
        raise APIError(422, "invalid-field", "chess960 must be a boolean")
    if chess960:
        raise APIError(422, "invalid-state", "Chess960 boundaries are not supported")
    try:
        board = chess.Board(fen.strip(), chess960=False)
        promoted_hex = source.get("promoted_hex")
        if promoted_hex is not None:
            if not isinstance(promoted_hex, str):
                raise APIError(
                    422,
                    "invalid-field",
                    "promoted_hex must be a hexadecimal string",
                )
            promoted_text = promoted_hex.strip().lower()
            if promoted_text.startswith("0x"):
                promoted_text = promoted_text[2:]
            if (
                not promoted_text
                or len(promoted_text) > 16
                or any(character not in "0123456789abcdef" for character in promoted_text)
            ):
                raise APIError(
                    422,
                    "invalid-field",
                    "promoted_hex must contain at most 16 hexadecimal digits",
                )
            promoted = int(promoted_text, 16)
            occupied = board.occupied
            if promoted & ~occupied or promoted & (board.pawns | board.kings):
                raise APIError(
                    422,
                    "invalid-state",
                    "promoted_hex must name occupied non-pawn, non-king squares",
                )
            if board.promoted and board.promoted != promoted:
                raise APIError(
                    422,
                    "invalid-state",
                    "promoted_hex conflicts with promoted markers in the FEN",
                )
            board.promoted = promoted
        return ProgressiveState(
            board,
            series,
            quiet_series=quiet_series,
            ep_targets=_parse_ep_targets(ep_value),
        )
    except (ValueError, chess.InvalidMoveError) as error:
        raise APIError(422, "invalid-state", str(error)) from error


def _prefix_from_payload(payload: Mapping[str, object]) -> tuple[str, ...]:
    source = _state_payload_source(payload)
    value = source.get("prefix", source.get("current_prefix", source.get("moves", [])))
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        raw: Sequence[object] = [part for part in value.split("/") if part]
    elif isinstance(value, list):
        raw = value
    else:
        raise APIError(422, "invalid-field", "prefix must be a list of UCI moves")
    if len(raw) > MAX_SERIES_NUMBER:
        raise APIError(422, "invalid-field", "prefix contains too many moves")
    moves: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise APIError(422, "invalid-field", "each prefix move must be UCI text")
        uci = item.strip().lower()
        try:
            parsed = chess.Move.from_uci(uci)
        except chess.InvalidMoveError as error:
            raise APIError(422, "invalid-move", f"invalid UCI move {item!r}") from error
        moves.append(parsed.uci())
    return tuple(moves)


def _boundary_payload(state: ProgressiveState) -> dict[str, object]:
    return {
        "fen": state.board.fen(en_passant="fen"),
        "board_fen": state.board.fen(en_passant="fen"),
        "pfen": state.pfen,
        "position_hash": state.position_hash,
        "series": state.series_number,
        "series_number": state.series_number,
        "side_to_move": "white" if state.board.turn == chess.WHITE else "black",
        "quiet_series": state.quiet_series,
        "quiet_draw_pending": state.quiet_draw_pending,
        "promoted_hex": f"{state.board.promoted:016x}",
        "chess960": bool(state.board.chess960),
        "ep_targets": [chess.square_name(square) for square in state.ep_targets],
        "progressive_ep": [chess.square_name(square) for square in state.ep_targets],
    }


def _legal_move_payload(
    state: ProgressiveState,
    board: chess.Board,
    variants: Sequence[tuple[chess.Move, int | None]],
    played: tuple[str, ...],
    sans: tuple[str, ...],
    ep_candidates: Mapping[int, int],
    made_progress: bool,
) -> list[dict[str, object]]:
    legal: list[dict[str, object]] = []
    for move, required_ep in variants:
        trial = board.copy(stack=False)
        trial.ep_square = required_ep
        san = trial.san(move)
        piece = trial.piece_at(move.from_square)
        is_pawn_move = piece is not None and piece.piece_type == chess.PAWN
        capture = trial.is_capture(move)

        next_candidates = dict(ep_candidates)
        if move.from_square in next_candidates:
            del next_candidates[move.from_square]
        if is_pawn_move and abs(move.to_square - move.from_square) == 16:
            next_candidates[move.to_square] = (
                move.from_square + move.to_square
            ) // 2

        trial.push(move)
        gives_check = trial.is_check()
        if gives_check:
            completed = _finish_series(
                state,
                trial,
                played + (move.uci(),),
                sans + (san,),
                next_candidates,
                made_progress or is_pawn_move or capture,
                delivered_check=True,
            )
            san = completed.san[-1]
        promotion = chess.piece_symbol(move.promotion) if move.promotion else None
        legal.append(
            {
                "uci": move.uci(),
                "san": san,
                "from": chess.square_name(move.from_square),
                "to": chess.square_name(move.to_square),
                "promotion": promotion,
                "capture": capture,
                "gives_check": gives_check,
            }
        )
    return legal


def _completion_reason(result: SeriesResult) -> str:
    if result.ended_by_check:
        return "checkmate" if result.outcome and result.outcome.value == "checkmate" else "check"
    if result.outcome is not None:
        return result.outcome.value
    return "budget"


def inspect_prefix(
    state: ProgressiveState,
    prefix: Sequence[str],
) -> dict[str, object]:
    """Replays a micro-move prefix from a trusted series boundary.

    The client never supplies an intermediate board. Every request recomputes
    legality, SAN, the quiet clock, and progressive en-passant candidates from
    the original boundary state.
    """

    requested = tuple(prefix)
    if len(requested) > state.moves_available:
        raise APIError(
            422,
            "series-overflow",
            f"series budget is {state.moves_available}; prefix has {len(requested)} moves",
        )

    board = state.board.copy(stack=False)
    mover = board.turn
    ep_candidates: dict[int, int] = {}
    made_progress = False
    played: tuple[str, ...] = ()
    sans: tuple[str, ...] = ()
    frames: list[dict[str, object]] = []
    result: SeriesResult | None = None

    for index, uci in enumerate(requested):
        if result is not None:
            raise APIError(
                422,
                "series-complete",
                f"extra move {uci} supplied after the series ended",
            )
        variants = {
            move.uci(): (move, required_ep)
            for move, required_ep in _legal_move_variants(
                board, state.ep_targets if index == 0 else ()
            )
        }
        selected = variants.get(uci)
        if selected is None:
            raise APIError(
                422,
                "illegal-move",
                f"illegal move {uci} at series index {index + 1}",
                details={"index": index, "move": uci},
            )
        move, required_ep = selected
        board.ep_square = required_ep
        san = board.san(move)
        piece = board.piece_at(move.from_square)
        is_pawn_move = piece is not None and piece.piece_type == chess.PAWN
        is_capture = board.is_capture(move)

        if move.from_square in ep_candidates:
            del ep_candidates[move.from_square]
        if is_pawn_move and abs(move.to_square - move.from_square) == 16:
            ep_candidates[move.to_square] = (move.from_square + move.to_square) // 2

        board.push(move)
        played += (move.uci(),)
        sans += (san,)
        frames.append(
            {
                "index": len(played),
                "uci": move.uci(),
                "san": san,
                "board_fen": board.fen(en_passant="fen"),
                "gives_check": board.is_check(),
            }
        )
        made_progress = made_progress or is_pawn_move or is_capture
        delivered_check = board.is_check()
        if delivered_check or len(played) == state.moves_available:
            result = _finish_series(
                state,
                board,
                played,
                sans,
                ep_candidates,
                made_progress,
                delivered_check=delivered_check,
            )
            sans = result.san
            frames[-1]["san"] = result.san[-1]
            continue

        board.turn = mover
        board.ep_square = None
        if not _legal_move_variants(board):
            result = _stuck_result(state, board, played, sans)

    if result is None:
        variants = _legal_move_variants(board, state.ep_targets if not played else ())
        if not variants:
            result = _stuck_result(state, board, played, sans)
            legal_next: list[dict[str, object]] = []
        else:
            legal_next = _legal_move_payload(
                state,
                board,
                variants,
                played,
                sans,
                ep_candidates,
                made_progress,
            )
    else:
        legal_next = []

    complete = result is not None
    display_board = result.final_state.board if result is not None else board
    remaining = max(0, state.moves_available - len(played))
    ended_by_check = bool(result and result.ended_by_check)
    outcome = result.outcome.value if result and result.outcome else None
    next_state = _boundary_payload(result.final_state) if result is not None else None
    payload = {
        "engine_version": ENGINE_VERSION,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "ruleset_version": RULESET_VERSION,
        "boundary_state": _boundary_payload(state),
        "fen": display_board.fen(en_passant="fen"),
        "board_fen": display_board.fen(en_passant="fen"),
        "series": state.series_number,
        "series_number": state.series_number,
        "side_to_move": "white" if display_board.turn == chess.WHITE else "black",
        "active_series_side": "white" if mover == chess.WHITE else "black",
        "budget": state.moves_available,
        "prefix": list(played),
        "current_prefix": list(played),
        "san": list(sans),
        "notation": " / ".join(sans),
        "frames": frames,
        "remaining": remaining,
        "moves_remaining": remaining,
        "complete": complete,
        "completion_reason": _completion_reason(result) if result else None,
        "check": ended_by_check,
        "ended_by_check": ended_by_check,
        "in_check": display_board.is_check(),
        "outcome": outcome,
        "unused_moves": result.unused_moves if result else 0,
        "legal_next": legal_next,
        "legal_moves": legal_next,
        "next_state": next_state,
    }
    return payload


def _series_payload(series_number: int, result: SeriesResult) -> dict[str, object]:
    return {
        "series_number": series_number,
        "side": "white" if series_number % 2 else "black",
        "uci": result.machine_notation,
        "moves": list(result.moves),
        "san": list(result.san),
        "notation": result.notation,
        "ended_by_check": result.ended_by_check,
        "unused_moves": result.unused_moves,
        "outcome": result.outcome.value if result.outcome else None,
        "next_state": _boundary_payload(result.final_state),
    }


def _analysis_payload(
    state: ProgressiveState,
    result: SearchResult,
    *,
    alternatives_limit: int,
    analysis_id: int | None,
    analysis_scope: str = "boundary",
    fixed_prefix: tuple[str, ...] = (),
    prefix_complete: bool = False,
    move_quality: MoveQualityVerdict | None = None,
    request_time_limit_seconds: float | None = None,
    request_max_generation_positions: int | None = None,
    analysis_searches: int = 1,
    best_move_only: bool = False,
) -> dict[str, object]:
    evaluation = result.root_evaluation.as_dict()
    root_terminal = bool(
        result.best_series is not None
        and not result.best_series.moves
        and result.best_series.outcome is not None
    )
    terminal_outcome = (
        result.best_series.outcome.value
        if root_terminal and result.best_series is not None
        else None
    )
    variation = () if root_terminal else result.principal_variation
    best_full_series = (
        list(result.best_series.moves)
        if result.best_series is not None and not root_terminal
        else []
    )
    completion_start = len(fixed_prefix) if analysis_scope == "series-prefix" else 0
    best_completion = best_full_series[completion_start:]
    principal_variation = [
        _series_payload(state.series_number + offset, item)
        for offset, item in enumerate(variation)
    ]
    return {
        "engine_version": result.engine_version,
        "source_fingerprint": result.source_fingerprint,
        "engine_profile_id": result.engine_profile_id,
        "engine_profile_name": result.engine_profile_name,
        "ruleset_version": RULESET_VERSION,
        "analysis_scope": analysis_scope,
        "root_search_mode": (
            "best-move" if best_move_only else "all-retained-alternatives"
        ),
        "root_scores_complete": result.root_scores_complete,
        "fixed_prefix": list(fixed_prefix),
        "required_prefix": list(result.required_prefix),
        "prefix_complete": prefix_complete,
        "state": _boundary_payload(state),
        "score": result.score,
        "score_heuristic_points": result.score,
        "score_unit": "heuristic-points",
        "score_is_centipawns": False,
        "mate_score": MATE_SCORE,
        "classification": result.classification,
        "confidence": result.confidence,
        "proof": result.proof,
        "proven_result": result.forced,
        "adjudication_status": result.adjudication_status,
        "requested_depth": result.requested_depth,
        "completed_depth": result.completed_depth,
        "exact_width": result.exact_width,
        "timed_out": result.timed_out,
        "work_limit_reached": result.work_limit_reached,
        "reach_complete": result.root_evaluation.reach_complete,
        "terminal": root_terminal,
        "terminal_outcome": terminal_outcome,
        "elapsed_seconds": result.elapsed_seconds,
        "max_series_per_node": result.max_series_per_node,
        "time_limit_seconds": result.time_limit_seconds,
        "request_time_limit_seconds": request_time_limit_seconds,
        "analysis_searches": analysis_searches,
        "max_generation_positions": result.max_generation_positions,
        "request_max_generation_positions": request_max_generation_positions,
        "best_full_series": best_full_series,
        "best_completion": best_completion,
        "best_series": (
            _series_payload(state.series_number, result.best_series)
            if result.best_series and not root_terminal
            else None
        ),
        "best_series_uci": (
            result.best_series.machine_notation
            if result.best_series and not root_terminal
            else None
        ),
        "best_notation": (
            result.best_series.notation
            if result.best_series and not root_terminal
            else None
        ),
        "principal_variation": principal_variation,
        "pv": principal_variation,
        "principal_variation_text": (
            None
            if root_terminal
            else format_principal_variation(state.series_number, variation)
        ),
        "alternatives": [
            {
                "score": item.score,
                "score_unit": "heuristic-points",
                "proof": item.proof,
                "series_uci": item.series.machine_notation,
                "notation": item.series.notation,
                "series": _series_payload(state.series_number, item.series),
                "next_move_uci": (
                    item.series.moves[completion_start]
                    if len(item.series.moves) > completion_start
                    else None
                ),
                "completion": list(item.series.moves[completion_start:]),
                "full_series": list(item.series.moves),
                "continuation": [
                    _series_payload(state.series_number + 1 + offset, continuation)
                    for offset, continuation in enumerate(item.principal_variation)
                ],
            }
            for item in (() if root_terminal else result.alternatives[:alternatives_limit])
        ],
        "evaluation": evaluation,
        "stats": {
            "nodes": result.stats.nodes,
            "leaf_evaluations": result.stats.leaf_evaluations,
            "generated_raw_series": result.stats.generated_raw_series,
            "generated_unique_series": result.stats.generated_unique_series,
            "intra_series_transpositions": result.stats.intra_series_transpositions,
            "tt_hits": result.stats.tt_hits,
            "alpha_beta_cutoffs": result.stats.alpha_beta_cutoffs,
            "branch_caps": result.stats.branch_caps,
            "generation_positions": result.stats.generation_positions,
            "promotion_mate_positions": result.stats.promotion_mate_positions,
            "promotion_mate_setup_states": result.stats.promotion_mate_setup_states,
            "promotion_mate_candidates": result.stats.promotion_mate_candidates,
            "promotion_mate_completion_probes": (
                result.stats.promotion_mate_completion_probes
            ),
            "promotion_mate_limit_hits": result.stats.promotion_mate_limit_hits,
            "promotion_mate_replay_rejects": (
                result.stats.promotion_mate_replay_rejects
            ),
            "promotion_mate_mates": result.stats.promotion_mate_mates,
            "frontier_prunes": result.stats.frontier_prunes,
            "frontier_states_pruned": result.stats.frontier_states_pruned,
            "frontier_paths_pruned": result.stats.frontier_paths_pruned,
            "peak_frontier_states": result.stats.peak_frontier_states,
            "generation_work_limit_hits": result.stats.generation_work_limit_hits,
            "mate_proof_cache_hits": result.stats.mate_proof_cache_hits,
            "mate_proof_cache_found_hits": result.stats.mate_proof_cache_found_hits,
            "mate_proof_cache_exhausted_hits": (
                result.stats.mate_proof_cache_exhausted_hits
            ),
            "mate_proof_cache_misses": result.stats.mate_proof_cache_misses,
            "mate_proof_cache_store_attempts": (
                result.stats.mate_proof_cache_store_attempts
            ),
            "mate_proof_cache_evictions": result.stats.mate_proof_cache_evictions,
            "mate_proof_cache_work_saved": (
                result.stats.mate_proof_cache_work_saved
            ),
            "mate_proof_cache_errors": result.stats.mate_proof_cache_errors,
        },
        "move_quality": move_quality.as_dict() if move_quality is not None else None,
        "saved": analysis_id is not None,
        "analysis_id": analysis_id,
    }


def _should_save(payload: Mapping[str, object]) -> bool:
    value = payload.get("save", payload.get("database", False))
    if value in (None, False, ""):
        return False
    if value is True:
        return True
    if isinstance(value, str):
        # A path in a request is treated as an opt-in flag only after the
        # configured server path is checked by the request handler. It never
        # chooses a new filesystem destination.
        return True
    raise APIError(422, "invalid-field", "save must be a boolean")


def analyze_payload(
    payload: Mapping[str, object],
    *,
    database_path: Path | None = None,
    engine_profile: EngineProfile | None = None,
    request_limits: AnalysisRequestLimits = LOCAL_ANALYSIS_LIMITS,
    mate_proof_cache: MateProofCache | None = None,
) -> dict[str, object]:
    boundary = state_from_payload(payload)
    prefix = _prefix_from_payload(payload)
    inspected = inspect_prefix(boundary, prefix)
    analysis_scope = "boundary"
    required_prefix: tuple[str, ...] = ()
    if prefix:
        if inspected["outcome"] is not None:
            raise APIError(
                409,
                "game-over",
                f"the supplied series already ended the game by {inspected['outcome']}",
                details={
                    "outcome": inspected["outcome"],
                    "notation": inspected["notation"],
                    "unused_moves": inspected["unused_moves"],
                },
            )
        if inspected["complete"]:
            next_state = inspected["next_state"]
            assert isinstance(next_state, Mapping)
            state = state_from_payload(next_state)
            analysis_scope = "next-boundary"
        else:
            # Search only legal completions that retain the exact, replayed
            # client prefix.  The supplied boundary remains the source of
            # truth; an alleged intermediate FEN is never trusted.
            state = boundary
            required_prefix = prefix
            analysis_scope = "series-prefix"
    else:
        state = boundary

    depth = _require_int(
        payload.get("depth", 2),
        "depth",
        minimum=1,
        maximum=request_limits.maximum_depth,
    )
    max_series_value = payload.get("max_series", request_limits.default_max_series)
    if max_series_value is None:
        max_series_value = request_limits.default_max_series
    max_series = _require_int(
        max_series_value,
        "max_series",
        minimum=1,
        maximum=request_limits.maximum_max_series,
    )
    time_value = payload.get("time_limit", request_limits.default_seconds)
    if time_value is None:
        time_value = request_limits.default_seconds
    time_limit = _require_number(
        time_value,
        "time_limit",
        minimum=0.01,
        maximum=request_limits.maximum_seconds,
    )
    alternatives = _require_int(
        payload.get("alternatives", min(8, request_limits.maximum_alternatives)),
        "alternatives",
        minimum=0,
        maximum=request_limits.maximum_alternatives,
    )
    generation_value = payload.get(
        "max_generation_positions", request_limits.default_generation_positions
    )
    if generation_value is None:
        generation_value = request_limits.default_generation_positions
    max_generation_positions = _require_int(
        generation_value,
        "max_generation_positions",
        minimum=1_000,
        maximum=request_limits.maximum_generation_positions,
    )
    rate_move = payload.get("rate_move", False)
    if not isinstance(rate_move, bool):
        raise APIError(422, "invalid-field", "rate_move must be a boolean")
    best_move_only = payload.get("best_move_only", False)
    if not isinstance(best_move_only, bool):
        raise APIError(422, "invalid-field", "best_move_only must be a boolean")
    if best_move_only and rate_move:
        raise APIError(
            422,
            "invalid-field",
            "best_move_only cannot be combined with move grading",
        )
    if best_move_only and alternatives:
        raise APIError(
            422,
            "invalid-field",
            "best_move_only requires alternatives to be zero",
        )

    analysis_searches = 1
    if prefix and rate_move:
        # Incomplete prefixes reuse their fixed-prefix result for grading;
        # completed series need a separate constrained result in addition to
        # the next-boundary search.  Split the request budget so enabling a
        # badge cannot silently multiply the endpoint's CPU deadline.
        analysis_searches += 1 if analysis_scope == "series-prefix" else 2
    per_search_time_limit = time_limit / analysis_searches
    per_search_generation_positions = max(
        1, max_generation_positions // analysis_searches
    )

    limits = SearchLimits(
        depth_series=depth,
        max_series_per_node=max_series,
        time_limit_seconds=per_search_time_limit,
        max_generation_positions=per_search_generation_positions,
        collect_all_root_scores=not best_move_only,
        native_threads=request_limits.native_threads,
    )

    result = analyze(
        state,
        limits,
        profile=engine_profile,
        required_prefix=required_prefix,
        mate_proof_cache=mate_proof_cache,
    )
    quality: MoveQualityVerdict | None = None
    if prefix and rate_move:
        parent_prefix = prefix[:-1]
        parent_result = analyze(
            boundary,
            limits,
            profile=engine_profile,
            required_prefix=parent_prefix,
            mate_proof_cache=mate_proof_cache,
        )
        candidate_result = (
            result
            if analysis_scope == "series-prefix"
            else analyze(
                boundary,
                limits,
                profile=engine_profile,
                required_prefix=prefix,
                mate_proof_cache=mate_proof_cache,
            )
        )
        quality = grade_move_quality(
            parent_result,
            candidate_result,
            mover=boundary.board.turn,
            played_prefix=prefix,
            subject=QualitySubject.MICRO_MOVE,
        )
    analysis_id: int | None = None
    if _should_save(payload):
        if database_path is None:
            raise APIError(
                409,
                "database-not-configured",
                "start `spc web` with --database before requesting a saved analysis",
            )
        requested_database = payload.get("database")
        if isinstance(requested_database, str):
            requested_path = Path(requested_database).expanduser().resolve()
            if requested_path != database_path.resolve():
                raise APIError(
                    403,
                    "database-path-denied",
                    "the request cannot override the server's configured database path",
                )
        with TheoryDatabase(database_path) as database:
            analysis_id = database.save_analysis(state, result)
    return _analysis_payload(
        state,
        result,
        alternatives_limit=alternatives,
        analysis_id=analysis_id,
        analysis_scope=analysis_scope,
        fixed_prefix=prefix,
        prefix_complete=bool(inspected["complete"]),
        move_quality=quality,
        request_time_limit_seconds=time_limit,
        request_max_generation_positions=max_generation_positions,
        analysis_searches=analysis_searches,
        best_move_only=best_move_only,
    )


def load_openings(reports_dir: Path) -> dict[str, object]:
    reports: dict[str, object] = {}
    errors: list[dict[str, str]] = []
    for name, filename in REPORT_FILES.items():
        path = reports_dir / filename
        if not path.is_file():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("report root is not a JSON object")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            errors.append({"report": name, "error": str(error)})
            continue
        fingerprint = loaded.get("source_fingerprint")
        reports[name] = {
            "current": fingerprint == ENGINE_SOURCE_FINGERPRINT,
            "source_fingerprint": fingerprint,
            "data": loaded,
        }
    return {
        "available": bool(reports),
        "engine_version": ENGINE_VERSION,
        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
        "reports": reports,
        "errors": errors,
    }


class AnalysisBoardHandler(BaseHTTPRequestHandler):
    server_version = "ScottishProgressiveBoard/0.4"

    @property
    def app_server(self) -> AnalysisBoardServer:
        return self.server  # type: ignore[return-value]

    def _write_json(self, payload: object, status: int = 200) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )
        self._write_cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _write_error(self, error: APIError) -> None:
        payload: dict[str, object] = {
            "error": {"code": error.code, "message": error.message}
        }
        if error.details is not None:
            payload["error"]["details"] = error.details  # type: ignore[index]
        self._write_json(payload, error.status)

    def _origin_matches(self, candidate: str, expected: str) -> bool:
        if expected.startswith("http://"):
            return candidate.rstrip("/").lower() == expected.lower()
        try:
            normalized, _ = _normalize_public_origin(candidate)
        except ValueError:
            return False
        return normalized == expected

    def _cors_request_origin(self) -> str | None:
        configured = self.app_server.config.allowed_cors_origin
        origin = self.headers.get("Origin")
        if configured is None or origin is None:
            return None
        return configured if self._origin_matches(origin, configured) else None

    def _write_cors_headers(self) -> None:
        allowed_origin = self._cors_request_origin()
        if allowed_origin is None:
            return
        self.send_header("Access-Control-Allow-Origin", allowed_origin)
        self.send_header("Vary", "Origin")

    def _validate_local_request(self, *, require_same_origin: bool = False) -> None:
        bound_host, bound_port = self.app_server.server_address[:2]
        config = self.app_server.config
        expected_authority = config.allowed_authority or f"{bound_host}:{bound_port}"
        if self.headers.get("Host", "").strip().lower() != expected_authority.lower():
            raise APIError(
                403,
                "invalid-host",
                "request Host does not match this analysis board",
            )
        if require_same_origin:
            origin = self.headers.get("Origin")
            expected_origin = config.public_origin or f"http://{expected_authority}"
            allowed_origins = tuple(
                candidate
                for candidate in (expected_origin, config.allowed_cors_origin)
                if candidate is not None
            )
            if origin is not None and not any(
                self._origin_matches(origin, allowed) for allowed in allowed_origins
            ):
                raise APIError(
                    403,
                    "invalid-origin",
                    "request Origin does not match this analysis board",
                )

    def _read_json(self) -> dict[str, object]:
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            raise APIError(415, "unsupported-media-type", "Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise APIError(411, "length-required", "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise APIError(400, "invalid-length", "Content-Length is invalid") from error
        if length < 0:
            raise APIError(400, "invalid-length", "Content-Length cannot be negative")
        if length > self.app_server.config.request_limit:
            raise APIError(
                413,
                "request-too-large",
                f"request body exceeds {self.app_server.config.request_limit} bytes",
            )
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise APIError(400, "invalid-json", "request body is not valid UTF-8 JSON") from error
        if not isinstance(payload, dict):
            raise APIError(422, "invalid-body", "request JSON must be an object")
        return payload

    def _route_path(self) -> str:
        try:
            return unquote(urlsplit(self.path).path, errors="strict")
        except (UnicodeDecodeError, ValueError) as error:
            raise APIError(400, "invalid-path", "request path is malformed") from error

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            self._validate_local_request()
            path = self._route_path()
            if path == "/api/health":
                limits = self.app_server.config.analysis_limits
                self._write_json(
                    {
                        "ok": True,
                        "engine_version": ENGINE_VERSION,
                        "source_fingerprint": ENGINE_SOURCE_FINGERPRINT,
                        "ui_source_fingerprint": ui_source_fingerprint(
                            self.app_server.config.static_root
                        ),
                        "ruleset_version": RULESET_VERSION,
                        "engine_profile_id": self.app_server.config.engine_profile.profile_id,
                        "engine_profile_name": self.app_server.config.engine_profile.name,
                        "engine_profile_recommended_depth": (
                            self.app_server.config.engine_profile.recommended_depth
                        ),
                        "engine_profile_recommended_branch_cap": (
                            self.app_server.config.engine_profile.recommended_branch_cap
                        ),
                        "deployment_mode": (
                            "public-bounded"
                            if self.app_server.config.public_origin is not None
                            else "local"
                        ),
                        "analysis_limits": {
                            "default_seconds": limits.default_seconds,
                            "maximum_seconds": limits.maximum_seconds,
                            "default_max_series": limits.default_max_series,
                            "maximum_max_series": limits.maximum_max_series,
                            "maximum_depth": limits.maximum_depth,
                            "default_generation_positions": limits.default_generation_positions,
                            "maximum_generation_positions": limits.maximum_generation_positions,
                            "maximum_alternatives": limits.maximum_alternatives,
                            "native_threads": limits.native_threads,
                        },
                        "runtime": {
                            "cpu_count": self.app_server.config.runtime_cpu_count,
                            "cpu_count_source": (
                                self.app_server.config.runtime_cpu_count_source
                            ),
                            "native_threads": limits.native_threads,
                            "native_threads_policy": (
                                self.app_server.config.native_threads_policy
                            ),
                        },
                        "database_configured": self.app_server.config.database_path
                        is not None,
                        "mate_proof_cache": {
                            **self.app_server.mate_proof_cache.snapshot().as_dict(),
                            "persistent": (
                                self.app_server.mate_proof_cache.path is not None
                            ),
                            "identity": (
                                self.app_server.mate_proof_cache.identity.as_dict()
                            ),
                        },
                    }
                )
                return
            if path == "/api/openings":
                self._write_json(load_openings(self.app_server.config.reports_dir))
                return
            if path.startswith("/api/"):
                raise APIError(404, "not-found", "API endpoint not found")
            self._serve_static(path)
        except APIError as error:
            self._write_error(error)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as error:
            self.log_error("unexpected server error: %s", error)
            self._write_error(APIError(500, "internal-error", "internal server error"))

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.do_GET()

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            self._validate_local_request()
            path = self._route_path()
            origin = self.headers.get("Origin")
            allowed_origin = self.app_server.config.allowed_cors_origin
            if (
                origin is None
                or allowed_origin is None
                or not self._origin_matches(origin, allowed_origin)
            ):
                raise APIError(403, "invalid-origin", "CORS origin is not allowed")

            requested_method = self.headers.get(
                "Access-Control-Request-Method", ""
            ).strip().upper()
            allowed_method = {
                "/api/health": "GET",
                "/api/openings": "GET",
                "/api/prefix": "POST",
                "/api/state": "POST",
                "/api/analyze": "POST",
            }.get(path)
            if allowed_method is None or requested_method != allowed_method:
                raise APIError(
                    403,
                    "invalid-preflight",
                    "CORS method is not allowed for this endpoint",
                )

            requested_headers = {
                item.strip().lower()
                for item in self.headers.get(
                    "Access-Control-Request-Headers", ""
                ).split(",")
                if item.strip()
            }
            if not requested_headers <= {"content-type"}:
                raise APIError(
                    403,
                    "invalid-preflight",
                    "CORS request headers are not allowed",
                )

            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Access-Control-Allow-Methods", allowed_method)
            if requested_headers:
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header(
                "Vary",
                "Origin, Access-Control-Request-Method, Access-Control-Request-Headers",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
        except APIError as error:
            self._write_error(error)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            self._validate_local_request(require_same_origin=True)
            path = self._route_path()
            if path not in {"/api/prefix", "/api/state", "/api/analyze"}:
                raise APIError(404, "not-found", "API endpoint not found")
            payload = self._read_json()
            if path in {"/api/prefix", "/api/state"}:
                state = state_from_payload(payload)
                response = inspect_prefix(state, _prefix_from_payload(payload))
                self._write_json(response)
                return
            if not self.app_server.analysis_gate.acquire(blocking=False):
                raise APIError(429, "analysis-busy", "analysis capacity is busy; retry shortly")
            try:
                response = analyze_payload(
                    payload,
                    database_path=self.app_server.config.database_path,
                    engine_profile=self.app_server.config.engine_profile,
                    request_limits=self.app_server.config.analysis_limits,
                    mate_proof_cache=self.app_server.mate_proof_cache,
                )
            finally:
                self.app_server.analysis_gate.release()
            self._write_json(response)
        except APIError as error:
            self._write_error(error)
        except (SeriesLegalityError, ValueError, chess.InvalidMoveError) as error:
            self._write_error(APIError(422, "invalid-request", str(error)))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as error:
            self.log_error("unexpected server error: %s", error)
            self._write_error(APIError(500, "internal-error", "internal server error"))

    def _serve_static(self, path: str) -> None:
        if "\x00" in path or "\\" in path:
            raise APIError(400, "invalid-path", "request path is malformed")
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        segments = relative.split("/")
        if any(segment in {"", ".", ".."} for segment in segments):
            raise APIError(404, "not-found", "asset not found")
        root = self.app_server.config.static_root.resolve()
        target = root.joinpath(*segments).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise APIError(404, "not-found", "asset not found")
        try:
            data = target.read_bytes()
        except OSError as error:
            raise APIError(404, "not-found", "asset not found") from error
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
            "image/svg+xml",
        }:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._write_error(APIError(405, "method-not-allowed", "method not allowed"))

    do_DELETE = do_PUT
    do_PATCH = do_PUT


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    database: str | Path | None = None,
    engine_profile: EngineProfile | str | Path | None = None,
    static_root: str | Path | None = None,
    reports_dir: str | Path | None = None,
    request_limit: int = MAX_REQUEST_BYTES,
    public_origin: str | None = None,
    cors_origin: str | None = None,
    mate_proof_cache_path: str | Path | None = None,
    mate_proof_cache_capacity: int = DEFAULT_MATE_PROOF_CACHE_CAPACITY,
) -> AnalysisBoardServer:
    normalized_origin: str | None = None
    allowed_authority: str | None = None
    if public_origin is None:
        if host != "127.0.0.1":
            raise ValueError("analysis board is local-only unless public_origin is configured")
    else:
        if host not in {"127.0.0.1", "0.0.0.0"}:
            raise ValueError("public analysis board host must be 0.0.0.0 or 127.0.0.1")
        if database is not None:
            raise ValueError("public analysis board cannot expose a SQLite database")
        normalized_origin, allowed_authority = _normalize_public_origin(public_origin)
    configured_cors_origin = cors_origin
    if configured_cors_origin is None and normalized_origin is not None:
        configured_cors_origin = os.environ.get("SPC_ALLOWED_CORS_ORIGIN")
    normalized_cors_origin: str | None = None
    if configured_cors_origin is not None:
        if normalized_origin is None:
            raise ValueError("CORS origin is available only for a public analysis board")
        normalized_cors_origin, _ = _normalize_public_origin(configured_cors_origin)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if request_limit < 1024:
        raise ValueError("request_limit must be at least 1024 bytes")
    database_path = Path(database).expanduser().resolve() if database else None
    configured_profile = (
        load_profile(engine_profile)
        if isinstance(engine_profile, (str, Path))
        else engine_profile or baseline_profile()
    )
    static_path = Path(static_root).resolve() if static_root else _default_static_root()
    if not static_path.is_dir():
        raise ValueError(f"web static directory not found: {static_path}")
    configured_cache_path = mate_proof_cache_path
    if configured_cache_path is None:
        configured_cache_path = os.environ.get("SPC_MATE_PROOF_CACHE_PATH") or None
    mate_proof_cache = MateProofCache(
        configured_cache_path,
        capacity=mate_proof_cache_capacity,
    )
    if normalized_origin is not None:
        analysis_limits, runtime_cpu_count, runtime_cpu_count_source = (
            _public_analysis_runtime()
        )
    else:
        analysis_limits = LOCAL_ANALYSIS_LIMITS
        runtime_cpu_count = str(os.cpu_count() or 1)
        runtime_cpu_count_source = "os.cpu_count"
    config = WebConfig(
        static_root=static_path,
        reports_dir=(Path(reports_dir).resolve() if reports_dir else _default_reports_dir()),
        database_path=database_path,
        engine_profile=configured_profile,
        request_limit=(
            min(request_limit, 64 * 1024)
            if normalized_origin is not None
            else request_limit
        ),
        public_origin=normalized_origin,
        allowed_authority=allowed_authority,
        allowed_cors_origin=normalized_cors_origin,
        analysis_limits=analysis_limits,
        analysis_concurrency=1 if normalized_origin is not None else 2,
        runtime_cpu_count=runtime_cpu_count,
        runtime_cpu_count_source=runtime_cpu_count_source,
        native_threads_policy=(
            "single-thread-pool-avoidance"
            if normalized_origin is not None
            else "local-configured"
        ),
        mate_proof_cache=mate_proof_cache,
    )
    if database_path is not None:
        try:
            with TheoryDatabase(database_path):
                pass
        except (OSError, sqlite3.Error) as error:
            raise ValueError(f"could not open theory database {database_path}: {error}") from error
    try:
        return AnalysisBoardServer((host.strip(), port), AnalysisBoardHandler, config)
    except OSError as error:
        raise ValueError(f"could not bind analysis board to {host}:{port}: {error}") from error


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    database: str | Path | None = None,
    engine_profile: EngineProfile | str | Path | None = None,
    public_origin: str | None = None,
    mate_proof_cache_path: str | Path | None = None,
    mate_proof_cache_capacity: int = DEFAULT_MATE_PROOF_CACHE_CAPACITY,
) -> int:
    server = create_server(
        host,
        port,
        database=database,
        engine_profile=engine_profile,
        public_origin=public_origin,
        mate_proof_cache_path=mate_proof_cache_path,
        mate_proof_cache_capacity=mate_proof_cache_capacity,
    )
    bound_host, bound_port = server.server_address[:2]
    display_host = f"[{bound_host}]" if ":" in bound_host else bound_host
    url = (
        public_origin.rstrip("/") + "/"
        if public_origin
        else f"http://{display_host}:{bound_port}/"
    )
    print(f"Scottish Progressive analysis board: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url, new=2)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("Stopping analysis board.")
    finally:
        server.server_close()
    return 0
