"""SQLite connection helpers and migration runner."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Database:
    """Thin wrapper around sqlite3 with row dicts and JSON helpers."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")

    # -- transactions -----------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Transaction scope. Re-entrant: a nested tx() joins the outer transaction."""
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    # -- query helpers -----------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def fetchall(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def insert(self, table: str, values: dict[str, Any]) -> int:
        cols = ", ".join(values.keys())
        marks = ", ".join("?" for _ in values)
        cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
        return int(cur.lastrowid or 0)

    def update(self, table: str, values: dict[str, Any], where: str, params: tuple = ()) -> int:
        sets = ", ".join(f"{k} = ?" for k in values)
        cur = self.conn.execute(f"UPDATE {table} SET {sets} WHERE {where}", tuple(values.values()) + params)
        return cur.rowcount

    # -- settings ----------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.fetchone("SELECT value_json FROM settings WHERE key = ?", (key,))
        if row is None:
            return default
        return json.loads(row["value_json"])

    def set_setting(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (key, json.dumps(value), utcnow_iso()),
        )

    def delete_setting(self, key: str) -> None:
        self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def close(self) -> None:
        self.conn.close()

    # -- migrations --------------------------------------------------------
    def migrate(self) -> list[str]:
        """Apply pending SQL migrations in order. Returns applied versions."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {r["version"] for r in self.fetchall("SELECT version FROM schema_migrations")}
        done: list[str] = []
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = sql_file.stem
            if version in applied:
                continue
            script = sql_file.read_text(encoding="utf-8")
            wrapped = (
                "BEGIN;\n" + script + "\nINSERT INTO schema_migrations (version, applied_at) VALUES ("
                + repr(version) + ", " + repr(utcnow_iso()) + ");\nCOMMIT;"
            )
            try:
                self.conn.executescript(wrapped)
            except Exception:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
            done.append(version)
        return done

    def schema_version(self) -> str | None:
        row = self.fetchone("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1")
        return row["version"] if row else None


def connect(path: Path, migrate: bool = True) -> Database:
    db = Database(path)
    if migrate:
        db.migrate()
    return db
