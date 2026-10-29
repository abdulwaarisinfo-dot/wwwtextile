"""Append-only audit trail. Call inside the same transaction as the action so
the audit row commits or rolls back together with it."""
from __future__ import annotations

import json
import sqlite3

from .db import now_iso, transaction


def record(conn: sqlite3.Connection, actor, action: str, entity_type: str | None = None,
           entity_ref: str | None = None, outcome: str = "success", detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_events(ts, actor_id, actor_name, actor_role, action, entity_type, entity_ref, outcome, detail) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            now_iso(),
            actor["id"] if actor else None,
            actor["username"] if actor else "anonymous",
            actor["role"] if actor else None,
            action, entity_type, entity_ref, outcome,
            json.dumps(detail, sort_keys=True) if detail else None,
        ),
    )


def record_standalone(conn: sqlite3.Connection, actor, action: str, **kw) -> None:
    """For denied/failed attempts, which are not part of a business transaction."""
    with transaction(conn):
        record(conn, actor, action, **kw)
