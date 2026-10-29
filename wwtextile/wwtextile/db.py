"""SQLite access: connections, explicit transactions, migrations.

SQLite is used because it is the only transactional database with a Python
driver available in the build environment (see IMPLEMENTATION_PLAN.md).
Writers are serialised with BEGIN IMMEDIATE, which is what stops two
concurrent postings from spending the same stock.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection):
    """One atomic write transaction. Takes the write lock up front so that every
    balance check inside it sees the state that will actually be committed."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations in filename order. Returns the names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied: list[str] = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if f.name in done:
            continue
        script = f.read_text()
        # executescript() commits implicitly, so run the file and its bookkeeping row together.
        conn.executescript(
            "BEGIN;\n" + script + f"\nINSERT INTO schema_migrations(name, applied_at) VALUES ('{f.name}', '{now_iso()}');\nCOMMIT;"
        )
        applied.append(f.name)
    return applied


def next_number(conn: sqlite3.Connection, name: str, prefix: str, width: int = 5) -> str:
    """Gap-tolerant document numbers. Must be called inside a transaction()."""
    row = conn.execute("SELECT value FROM sequences WHERE name = ?", (name,)).fetchone()
    value = (row["value"] if row else 0) + 1
    conn.execute(
        "INSERT INTO sequences(name, value) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
        (name, value),
    )
    return f"{prefix}-{value:0{width}d}"


def get_setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def db_exists(path: str) -> bool:
    return os.path.exists(path)
