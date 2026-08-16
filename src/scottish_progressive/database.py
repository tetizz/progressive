from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3

from .model import Outcome, ProgressiveState, QUIET_DRAW_POLICY, RULESET_VERSION
from .search import MATE_SCORE, SearchResult


SCHEMA_VERSION = 4


class TheoryDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def __enter__(self) -> TheoryDatabase:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS positions (
                position_hash TEXT PRIMARY KEY,
                fen TEXT NOT NULL,
                pfen TEXT NOT NULL,
                side_to_move TEXT NOT NULL,
                series_number INTEGER NOT NULL,
                moves_available INTEGER NOT NULL,
                quiet_series INTEGER NOT NULL DEFAULT 0,
                quiet_draw_pending INTEGER NOT NULL DEFAULT 0,
                adjudication_status TEXT,
                proof_kind TEXT,
                evaluation INTEGER,
                mate_distance INTEGER,
                best_series TEXT,
                principal_variation_json TEXT,
                alternatives_json TEXT,
                search_depth INTEGER,
                node_count INTEGER,
                analysis_timestamp TEXT,
                engine_version TEXT,
                confidence TEXT,
                opening_name TEXT,
                opening_classification TEXT,
                terminal_outcome TEXT,
                exact_width INTEGER,
                timed_out INTEGER,
                best_analysis_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_hash TEXT NOT NULL REFERENCES positions(position_hash),
                evaluation INTEGER NOT NULL,
                classification TEXT NOT NULL,
                mate_distance INTEGER,
                best_series TEXT,
                principal_variation_json TEXT NOT NULL,
                alternatives_json TEXT NOT NULL,
                search_depth INTEGER NOT NULL,
                requested_depth INTEGER NOT NULL,
                node_count INTEGER NOT NULL,
                generated_series INTEGER NOT NULL,
                exact_width INTEGER NOT NULL,
                timed_out INTEGER NOT NULL,
                elapsed_seconds REAL NOT NULL,
                analysis_timestamp TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                confidence TEXT NOT NULL,
                evaluation_breakdown_json TEXT NOT NULL,
                ruleset_version TEXT NOT NULL DEFAULT 'unknown',
                quiet_draw_policy TEXT NOT NULL DEFAULT 'unknown',
                source_fingerprint TEXT NOT NULL DEFAULT 'unknown',
                search_limits_json TEXT NOT NULL DEFAULT '{}',
                adjudication_status TEXT,
                proof_kind TEXT
            );

            CREATE TABLE IF NOT EXISTS edges (
                parent_hash TEXT NOT NULL REFERENCES positions(position_hash),
                child_hash TEXT NOT NULL REFERENCES positions(position_hash),
                series_uci TEXT NOT NULL,
                series_notation TEXT NOT NULL,
                rank INTEGER NOT NULL,
                score INTEGER NOT NULL,
                transposition_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (parent_hash, child_hash, series_uci)
            );

            CREATE TABLE IF NOT EXISTS analysis_edges (
                analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
                parent_hash TEXT NOT NULL REFERENCES positions(position_hash),
                child_hash TEXT NOT NULL REFERENCES positions(position_hash),
                series_uci TEXT NOT NULL,
                series_notation TEXT NOT NULL,
                rank INTEGER NOT NULL,
                score INTEGER NOT NULL,
                transposition_count INTEGER NOT NULL DEFAULT 1,
                terminal_outcome TEXT,
                PRIMARY KEY (analysis_id, child_hash, series_uci)
            );

            CREATE INDEX IF NOT EXISTS analyses_position_idx
                ON analyses(position_hash, search_depth DESC);
            CREATE INDEX IF NOT EXISTS edges_parent_idx
                ON edges(parent_hash, rank);
            CREATE INDEX IF NOT EXISTS analysis_edges_analysis_idx
                ON analysis_edges(analysis_id, rank);
            """
        )
        columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(positions)")
        }
        for name, declaration in (
            ("terminal_outcome", "TEXT"),
            ("exact_width", "INTEGER"),
            ("timed_out", "INTEGER"),
            ("best_analysis_id", "INTEGER"),
            ("quiet_series", "INTEGER NOT NULL DEFAULT 0"),
            ("quiet_draw_pending", "INTEGER NOT NULL DEFAULT 0"),
            ("adjudication_status", "TEXT"),
            ("proof_kind", "TEXT"),
        ):
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE positions ADD COLUMN {name} {declaration}"
                )
        analysis_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(analyses)")
        }
        for name, declaration in (
            ("ruleset_version", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("quiet_draw_policy", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("source_fingerprint", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("search_limits_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("adjudication_status", "TEXT"),
            ("proof_kind", "TEXT"),
        ):
            if name not in analysis_columns:
                self.connection.execute(
                    f"ALTER TABLE analyses ADD COLUMN {name} {declaration}"
                )
        self.connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.connection.commit()

    @staticmethod
    def _pv_json(result: SearchResult) -> str:
        return json.dumps(
            [
                {
                    "series": item.machine_notation,
                    "notation": item.notation,
                    "position_hash": item.final_state.position_hash,
                    "outcome": item.outcome.value if item.outcome else None,
                }
                for item in result.principal_variation
            ],
            separators=(",", ":"),
        )

    @staticmethod
    def _alternatives_json(result: SearchResult) -> str:
        return json.dumps(
            [
                {
                    "series": item.series.machine_notation,
                    "notation": item.series.notation,
                    "score": item.score,
                    "position_hash": item.series.final_state.position_hash,
                    "transposition_count": item.series.transposition_count,
                }
                for item in result.alternatives
            ],
            separators=(",", ":"),
        )

    @staticmethod
    def _mate_distance(result: SearchResult) -> int | None:
        if result.forced not in {"white", "black"}:
            return None
        return len(result.principal_variation)

    def _upsert_state(
        self,
        state: ProgressiveState,
        terminal_outcome: str | None = None,
        adjudication_status: str | None = None,
        proof_kind: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO positions(
                position_hash, fen, pfen, side_to_move, series_number,
                moves_available, quiet_series, quiet_draw_pending,
                terminal_outcome, adjudication_status, proof_kind
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(position_hash) DO UPDATE SET
                fen=excluded.fen,
                pfen=excluded.pfen,
                side_to_move=excluded.side_to_move,
                series_number=excluded.series_number,
                moves_available=excluded.moves_available,
                quiet_series=excluded.quiet_series,
                quiet_draw_pending=excluded.quiet_draw_pending,
                terminal_outcome=COALESCE(excluded.terminal_outcome, positions.terminal_outcome),
                adjudication_status=COALESCE(excluded.adjudication_status, positions.adjudication_status),
                proof_kind=COALESCE(excluded.proof_kind, positions.proof_kind)
            """,
            (
                state.position_hash,
                state.board.fen(en_passant="fen"),
                state.pfen,
                state.side_name,
                state.series_number,
                state.moves_available,
                state.quiet_series,
                int(state.quiet_draw_pending),
                terminal_outcome,
                adjudication_status,
                proof_kind,
            ),
        )

    def save_analysis(
        self,
        state: ProgressiveState,
        result: SearchResult,
        *,
        opening_name: str | None = None,
        opening_classification: str | None = None,
    ) -> int:
        timestamp = datetime.now(UTC).isoformat(timespec="seconds")
        pv_json = self._pv_json(result)
        alternatives_json = self._alternatives_json(result)
        mate_distance = self._mate_distance(result)
        best_series = (
            result.best_series.machine_notation if result.best_series else None
        )

        root_terminal = (
            result.best_series.outcome.value
            if result.best_series is not None
            and not result.best_series.moves
            and result.best_series.outcome is not None
            else None
        )
        if result.adjudication_status == "proven-draw-no-mating-material":
            root_terminal = Outcome.TEN_SERIES_DRAW.value
        self._upsert_state(
            state,
            root_terminal,
            result.adjudication_status,
            result.forced,
        )
        search_limits_json = json.dumps(
            {
                "depth_series": result.requested_depth,
                "max_series_per_node": result.max_series_per_node,
                "time_limit_seconds": result.time_limit_seconds,
                "deterministic": True,
                "search_unit": "complete-series",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        cursor = self.connection.execute(
            """
            INSERT INTO analyses(
                position_hash, evaluation, classification, mate_distance,
                best_series, principal_variation_json, alternatives_json,
                search_depth, requested_depth, node_count, generated_series,
                exact_width, timed_out, elapsed_seconds, analysis_timestamp,
                engine_version, confidence, evaluation_breakdown_json
                , ruleset_version, quiet_draw_policy, source_fingerprint,
                search_limits_json, adjudication_status, proof_kind
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.position_hash,
                result.score,
                result.classification,
                mate_distance,
                best_series,
                pv_json,
                alternatives_json,
                result.completed_depth,
                result.requested_depth,
                result.stats.nodes,
                result.stats.generated_unique_series,
                int(result.exact_width),
                int(result.timed_out),
                result.elapsed_seconds,
                timestamp,
                result.engine_version,
                result.confidence,
                json.dumps(result.root_evaluation.as_dict(), separators=(",", ":")),
                RULESET_VERSION,
                QUIET_DRAW_POLICY,
                result.source_fingerprint,
                search_limits_json,
                result.adjudication_status,
                result.forced,
            ),
        )
        analysis_id = int(cursor.lastrowid)

        current = self.connection.execute(
            """
            SELECT search_depth, exact_width, timed_out, node_count
            FROM positions WHERE position_hash=?
            """,
            (state.position_hash,),
        ).fetchone()
        existing_quality = (
            int(current["search_depth"] if current["search_depth"] is not None else -1),
            int(current["exact_width"] or 0),
            0 if current["timed_out"] else 1,
            int(current["node_count"] or 0),
        )
        new_quality = (
            result.completed_depth,
            int(result.exact_width),
            int(not result.timed_out),
            result.stats.nodes,
        )
        promote = current["search_depth"] is None or new_quality > existing_quality
        if promote:
            self.connection.execute(
                """
                UPDATE positions SET
                    evaluation=?, mate_distance=?, best_series=?,
                    principal_variation_json=?, alternatives_json=?, search_depth=?,
                    node_count=?, analysis_timestamp=?, engine_version=?, confidence=?,
                    opening_name=?, opening_classification=?, exact_width=?, timed_out=?,
                    proof_kind=?, best_analysis_id=?
                WHERE position_hash=?
                """,
                (
                    result.score,
                    mate_distance,
                    best_series,
                    pv_json,
                    alternatives_json,
                    result.completed_depth,
                    result.stats.nodes,
                    timestamp,
                    result.engine_version,
                    result.confidence,
                    opening_name,
                    opening_classification or result.classification,
                    int(result.exact_width),
                    int(result.timed_out),
                    result.forced,
                    analysis_id,
                    state.position_hash,
                ),
            )
            self.connection.execute(
                "DELETE FROM edges WHERE parent_hash=?", (state.position_hash,)
            )

        for rank, alternative in enumerate(result.alternatives, start=1):
            child = alternative.series.final_state
            terminal_outcome = (
                alternative.series.outcome.value
                if alternative.series.outcome is not None
                else None
            )
            self._upsert_state(child, terminal_outcome)
            self.connection.execute(
                """
                INSERT INTO analysis_edges(
                    analysis_id, parent_hash, child_hash, series_uci,
                    series_notation, rank, score, transposition_count,
                    terminal_outcome
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis_id,
                    state.position_hash,
                    child.position_hash,
                    alternative.series.machine_notation,
                    alternative.series.notation,
                    rank,
                    alternative.score,
                    alternative.series.transposition_count,
                    terminal_outcome,
                ),
            )
            if promote:
                self.connection.execute(
                    """
                    INSERT INTO edges(
                    parent_hash, child_hash, series_uci, series_notation,
                    rank, score, transposition_count
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(parent_hash, child_hash, series_uci) DO UPDATE SET
                    rank=excluded.rank,
                    score=excluded.score,
                    transposition_count=excluded.transposition_count
                    """,
                    (
                        state.position_hash,
                        child.position_hash,
                        alternative.series.machine_notation,
                        alternative.series.notation,
                        rank,
                        alternative.score,
                        alternative.series.transposition_count,
                    ),
                )
        self.connection.commit()
        return analysis_id
