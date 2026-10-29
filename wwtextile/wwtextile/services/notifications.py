"""In-app notifications. Targeted at a role or a user; read state is per user."""
from __future__ import annotations

import sqlite3

from ..db import now_iso
from ..permissions import roles_with


def notify(conn: sqlite3.Connection, *, title: str, body: str = "", url: str = "", role: str | None = None,
           roles: list[str] | None = None, user_id: int | None = None, dedupe_key: str | None = None) -> int:
    """Insert notification(s). Call inside the caller's transaction. A repeated dedupe_key is ignored."""
    targets = list(roles or []) + ([role] if role else [])
    n = 0
    if user_id is not None:
        targets_u = [(None, user_id)]
    else:
        targets_u = []
    for r in sorted(set(targets)):
        targets_u.append((r, None))
    for r, u in targets_u:
        key = f"{dedupe_key}|{r or u}" if dedupe_key else None
        cur = conn.execute(
            "INSERT OR IGNORE INTO notifications(role, user_id, title, body, url, dedupe_key, created_at) VALUES (?,?,?,?,?,?,?)",
            (r, u, title[:200], body[:500], url, key, now_iso()))
        n += cur.rowcount
    return n


def notify_permission(conn, permission: str, **kw) -> int:
    return notify(conn, roles=roles_with(permission), **kw)


_VISIBLE = "(n.user_id = :uid OR n.role = :role)"


def unread_count(conn, user) -> int:
    return conn.execute(
        f"SELECT COUNT(*) AS c FROM notifications n WHERE {_VISIBLE} AND NOT EXISTS "
        "(SELECT 1 FROM notification_reads r WHERE r.notification_id=n.id AND r.user_id=:uid)",
        {"uid": user["id"], "role": user["role"]}).fetchone()["c"]


def list_for(conn, user, limit: int = 100, unread_only: bool = False):
    sql = (f"SELECT n.*, (SELECT read_at FROM notification_reads r WHERE r.notification_id=n.id AND r.user_id=:uid) AS read_at "
           f"FROM notifications n WHERE {_VISIBLE} ")
    if unread_only:
        sql += "AND NOT EXISTS (SELECT 1 FROM notification_reads r WHERE r.notification_id=n.id AND r.user_id=:uid) "
    sql += "ORDER BY n.id DESC LIMIT :lim"
    return conn.execute(sql, {"uid": user["id"], "role": user["role"], "lim": limit}).fetchall()


def mark_read(conn, user, notification_id: int) -> None:
    row = conn.execute(f"SELECT n.id FROM notifications n WHERE n.id=:id AND {_VISIBLE}",
                       {"id": notification_id, "uid": user["id"], "role": user["role"]}).fetchone()
    if row:
        conn.execute("INSERT OR IGNORE INTO notification_reads(notification_id, user_id, read_at) VALUES (?,?,?)",
                     (notification_id, user["id"], now_iso()))


def mark_all_read(conn, user) -> None:
    conn.execute(
        f"INSERT OR IGNORE INTO notification_reads(notification_id, user_id, read_at) "
        f"SELECT n.id, :uid, :now FROM notifications n WHERE {_VISIBLE}",
        {"uid": user["id"], "role": user["role"], "now": now_iso()})
