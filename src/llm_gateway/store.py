"""SQLite request log. One row per request, no prompt content."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

COLUMNS: dict[str, str] = {
    "id": "TEXT PRIMARY KEY",
    "ts": "TEXT NOT NULL",
    "tenant": "TEXT",
    "app": "TEXT",
    "api": "TEXT",  # openai | anthropic | passthrough
    "path": "TEXT",
    "provider": "TEXT",
    "model": "TEXT",
    "response_model": "TEXT",
    "stream": "INTEGER",
    "status_code": "INTEGER",
    "error": "TEXT",
    "completed": "INTEGER",
    "latency_ms": "REAL",
    "ttft_ms": "REAL",
    "input_tokens": "INTEGER",
    "output_tokens": "INTEGER",
    "cached_input_tokens": "INTEGER",
    "cache_write_tokens": "INTEGER",
    "reasoning_tokens": "INTEGER",
    "usage_source": "TEXT",
    "est_system_tokens": "INTEGER",
    "est_tools_tokens": "INTEGER",
    "est_history_tokens": "INTEGER",
    "est_tool_results_tokens": "INTEGER",
    "est_user_prompt_tokens": "INTEGER",
    "est_input_tokens": "INTEGER",
    "est_method": "TEXT",
    "message_count": "INTEGER",
    "attachment_count": "INTEGER",
    "max_tokens": "INTEGER",
    "temperature": "REAL",
    "prompt_hash": "TEXT",
    "prefix_hash": "TEXT",
    "user_prompt_hash": "TEXT",
    "lang": "TEXT",
    "text_chars": "INTEGER",
    "text_tokens": "INTEGER",
    "finish_reason": "TEXT",
    "cost_usd": "REAL",
    "cost_output_usd": "REAL",
}


class RequestStore:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            cols = ", ".join(f"{name} {ctype}" for name, ctype in COLUMNS.items())
            self._conn.execute(f"CREATE TABLE IF NOT EXISTS requests ({cols})")
            # Add columns introduced by newer versions to an existing database.
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(requests)")}
            for name, ctype in COLUMNS.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE requests ADD COLUMN {name} {ctype.replace(' PRIMARY KEY', '')}")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests (ts)")
            self._conn.commit()

    def insert(self, record: dict[str, Any]) -> None:
        row = {k: v for k, v in record.items() if k in COLUMNS}
        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            self._conn.execute(f"INSERT INTO requests ({names}) VALUES ({marks})", list(row.values()))
            self._conn.commit()

    def fetch(self, since_iso: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM requests"
        params: list[Any] = []
        if since_iso:
            sql += " WHERE ts >= ?"
            params.append(since_iso)
        sql += " ORDER BY ts"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params)]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
