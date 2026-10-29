"""Authoritative inventory arithmetic.

Everything that needs a stock number (UI, reports, dashboard, the lot query)
calls these functions. Nothing else sums the ledger.
"""
from __future__ import annotations

import sqlite3

from ..db import now_iso
from ..errors import BusinessRuleViolation, NotFound


def on_hand(conn: sqlite3.Connection, lot_id: int, location_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(qty),0) AS q FROM stock_ledger WHERE lot_id=? AND location_id=?",
        (lot_id, location_id),
    ).fetchone()["q"]


def reserved(conn: sqlite3.Connection, lot_id: int, location_id: int, exclude_dispatch: int | None = None) -> int:
    sql = ("SELECT COALESCE(SUM(qty),0) AS q FROM stock_reservations "
           "WHERE lot_id=? AND location_id=? AND status='active'")
    args: list = [lot_id, location_id]
    if exclude_dispatch is not None:
        sql += " AND dispatch_id <> ?"
        args.append(exclude_dispatch)
    return conn.execute(sql, args).fetchone()["q"]


def available(conn: sqlite3.Connection, lot_id: int, location_id: int) -> int:
    """On hand minus active reservations."""
    return on_hand(conn, lot_id, location_id) - reserved(conn, lot_id, location_id)


def lot_total_on_hand(conn: sqlite3.Connection, lot_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(qty),0) AS q FROM stock_ledger WHERE lot_id=?", (lot_id,)
    ).fetchone()["q"]


def post_line(conn: sqlite3.Connection, *, lot_id: int, location_id: int, qty: int, movement: str,
              source_type: str, source_id: int, line_key: str, actor_id: int | None,
              reverses_id: int | None = None, note: str | None = None) -> int:
    """Append one ledger row. MUST be called inside db.transaction().

    Negative rows are checked against current on-hand here (clear message); the
    schema trigger is the backstop. A repeated (source_type, source_id, line_key)
    violates the UNIQUE constraint, so the same document line can never post twice.
    """
    if qty == 0:
        raise BusinessRuleViolation("A ledger line cannot have zero quantity.")
    if qty < 0:
        have = on_hand(conn, lot_id, location_id)
        if have + qty < 0:
            lot = conn.execute("SELECT lot_no FROM lots WHERE id=?", (lot_id,)).fetchone()
            raise BusinessRuleViolation(
                f"Insufficient stock for lot {lot['lot_no'] if lot else lot_id}: "
                f"on hand {have / 1000:,.3f}, requested {-qty / 1000:,.3f}."
            )
    try:
        cur = conn.execute(
            "INSERT INTO stock_ledger(posted_at, lot_id, location_id, qty, movement, source_type, source_id, "
            "line_key, reverses_id, actor_id, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), lot_id, location_id, qty, movement, source_type, source_id, line_key,
             reverses_id, actor_id, note),
        )
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise BusinessRuleViolation("This document line has already been posted to the ledger.") from None
        raise BusinessRuleViolation(f"Ledger rejected the entry: {e}") from None
    return cur.lastrowid


def get_lot(conn: sqlite3.Connection, lot_no: str):
    row = conn.execute(
        "SELECT l.*, i.code AS item_code, i.name AS item_name, i.unit AS unit, p.name AS supplier_name "
        "FROM lots l JOIN items i ON i.id=l.item_id LEFT JOIN parties p ON p.id=l.supplier_id "
        "WHERE l.lot_no = ?", (lot_no.strip(),),
    ).fetchone()
    if row is None:
        raise NotFound(f"Lot '{lot_no}' not found.")
    return row


def lot_balances(conn: sqlite3.Connection, lot_id: int):
    """Per-location on hand / reserved / available for a lot (non-zero rows only)."""
    rows = conn.execute(
        "SELECT loc.id AS location_id, loc.code, loc.name, loc.kind, SUM(sl.qty) AS on_hand "
        "FROM stock_ledger sl JOIN locations loc ON loc.id = sl.location_id "
        "WHERE sl.lot_id = ? GROUP BY loc.id HAVING SUM(sl.qty) <> 0 ORDER BY loc.id", (lot_id,),
    ).fetchall()
    out = []
    for r in rows:
        res = reserved(conn, lot_id, r["location_id"])
        out.append({**dict(r), "reserved": res, "available": r["on_hand"] - res})
    return out


def stock_summary(conn: sqlite3.Connection, location_kind: str | None = None, search: str | None = None):
    """Stock by lot and location with reservations. Used by the Fabrics page and dashboard."""
    sql = (
        "SELECT l.id AS lot_id, l.lot_no, l.kind AS lot_kind, i.name AS item_name, i.unit, "
        "loc.id AS location_id, loc.code AS loc_code, loc.name AS loc_name, loc.kind AS loc_kind, "
        "SUM(sl.qty) AS on_hand "
        "FROM stock_ledger sl JOIN lots l ON l.id=sl.lot_id JOIN items i ON i.id=l.item_id "
        "JOIN locations loc ON loc.id=sl.location_id WHERE 1=1 "
    )
    args: list = []
    if location_kind:
        sql += "AND loc.kind = ? "
        args.append(location_kind)
    if search:
        sql += "AND (l.lot_no LIKE ? OR i.name LIKE ?) "
        args += [f"%{search}%", f"%{search}%"]
    sql += "GROUP BY l.id, loc.id HAVING SUM(sl.qty) <> 0 ORDER BY l.lot_no, loc.id"
    out = []
    for r in conn.execute(sql, args).fetchall():
        res = reserved(conn, r["lot_id"], r["location_id"])
        out.append({**dict(r), "reserved": res, "available": r["on_hand"] - res})
    return out


def totals_by_location_kind(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT loc.kind, loc.name, COALESCE(SUM(sl.qty),0) AS on_hand "
        "FROM locations loc LEFT JOIN stock_ledger sl ON sl.location_id = loc.id "
        "GROUP BY loc.id ORDER BY loc.id"
    ).fetchall()


def ledger_rows(conn: sqlite3.Connection, *, lot_no: str | None = None, location_id: int | None = None,
                movement: str | None = None, limit: int = 200, offset: int = 0):
    sql = (
        "SELECT sl.*, l.lot_no, loc.name AS loc_name, u.username AS actor "
        "FROM stock_ledger sl JOIN lots l ON l.id=sl.lot_id JOIN locations loc ON loc.id=sl.location_id "
        "LEFT JOIN users u ON u.id=sl.actor_id WHERE 1=1 "
    )
    args: list = []
    if lot_no:
        sql += "AND l.lot_no LIKE ? "
        args.append(f"%{lot_no}%")
    if location_id:
        sql += "AND sl.location_id = ? "
        args.append(location_id)
    if movement:
        sql += "AND sl.movement = ? "
        args.append(movement)
    count = conn.execute("SELECT COUNT(*) AS c FROM (" + sql.replace(
        "SELECT sl.*, l.lot_no, loc.name AS loc_name, u.username AS actor", "SELECT sl.id") + ")", args).fetchone()["c"]
    sql += "ORDER BY sl.id DESC LIMIT ? OFFSET ?"
    return conn.execute(sql, args + [limit, offset]).fetchall(), count


def reconcile_ledger(conn: sqlite3.Connection):
    """Integrity check: no negative lot/location balances; every reversal points at a real row.
    Returns a list of problem strings (empty means consistent)."""
    problems = []
    for r in conn.execute(
        "SELECT lot_id, location_id, SUM(qty) AS q FROM stock_ledger GROUP BY lot_id, location_id HAVING SUM(qty) < 0"
    ):
        problems.append(f"negative balance lot={r['lot_id']} location={r['location_id']} qty={r['q']}")
    for r in conn.execute(
        "SELECT sl.id FROM stock_ledger sl LEFT JOIN stock_ledger o ON o.id = sl.reverses_id "
        "WHERE sl.reverses_id IS NOT NULL AND o.id IS NULL"
    ):
        problems.append(f"dangling reversal ledger id={r['id']}")
    return problems
