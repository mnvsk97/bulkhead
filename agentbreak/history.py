"""Lightweight SQLite persistence for AgentBreak run history."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class RunHistory:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    label TEXT,
                    llm_scorecard TEXT,
                    mcp_scorecard TEXT,
                    scenarios TEXT
                )
            """)
            # Migrate: add label column if missing (for existing DBs)
            try:
                conn.execute("ALTER TABLE runs ADD COLUMN label TEXT")
            except Exception:
                pass

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def save_run(self, llm_scorecard: dict | None, mcp_scorecard: dict | None, scenarios: list[dict] | None = None, label: str | None = None) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (timestamp, label, llm_scorecard, mcp_scorecard, scenarios) VALUES (?, ?, ?, ?, ?)",
                (time.time(), label, json.dumps(llm_scorecard), json.dumps(mcp_scorecard), json.dumps(scenarios)),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def get_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM runs ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return self._row_to_dict(row) if row else None

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for key in ("llm_scorecard", "mcp_scorecard", "scenarios"):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d
