"""Goods receipts: draft -> posted -> (reversed). Drafts never touch stock."""
from __future__ import annotations

import re
import sqlite3

from .. import audit
from ..db import next_number, now_iso, transaction
from ..errors import BusinessRuleViolation, NotFound, ValidationFailed
from ..permissions import require
from ..quantity import QuantityError, fmt_qty, parse_qty
from . import inventory

LOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


def _clean(v) -> str:
    return str(v if v is not None else "").strip()


def get(conn: sqlite3.Connection, receipt_id: int):
    r = conn.execute(
        "SELECT g.*, p.name AS supplier_name, i.code AS item_code, i.name AS item_name, i.unit AS unit, "
        "loc.name AS location_name, cu.username AS created_by_name, pu.username AS posted_by_name "
        "FROM goods_receipts g JOIN parties p ON p.id=g.supplier_id JOIN items i ON i.id=g.item_id "
        "JOIN locations loc ON loc.id=g.location_id JOIN users cu ON cu.id=g.created_by "
        "LEFT JOIN users pu ON pu.id=g.posted_by WHERE g.id=?", (receipt_id,)).fetchone()
    if r is None:
        raise NotFound("Goods receipt not found.")
    return r


def list_receipts(conn: sqlite3.Connection, status: str | None = None, q: str | None = None):
    sql = ("SELECT g.*, p.name AS supplier_name, i.name AS item_name, i.unit AS unit "
           "FROM goods_receipts g JOIN parties p ON p.id=g.supplier_id JOIN items i ON i.id=g.item_id WHERE 1=1 ")
    args: list = []
    if status:
        sql += "AND g.status = ? "
        args.append(status)
    if q:
        sql += "AND (g.doc_no LIKE ? OR g.lot_no LIKE ? OR p.name LIKE ?) "
        args += [f"%{q}%"] * 3
    sql += "ORDER BY g.id DESC LIMIT 200"
    return conn.execute(sql, args).fetchall()


def create_draft(conn: sqlite3.Connection, user, form: dict) -> int:
    require(user, "receipt.create")
    errors: dict[str, str] = {}
    lot_no = _clean(form.get("lot_no"))
    if not LOT_RE.match(lot_no):
        errors["lot_no"] = "Lot number is required (letters, digits, . _ - ; max 40)."
    try:
        supplier_id = int(form.get("supplier_id") or 0)
        item_id = int(form.get("item_id") or 0)
        location_id = int(form.get("location_id") or 0)
    except ValueError:
        raise ValidationFailed("Invalid selection.") from None
    supplier = conn.execute("SELECT * FROM parties WHERE id=? AND kind='supplier'", (supplier_id,)).fetchone()
    if supplier is None:
        errors["supplier_id"] = "Choose a supplier."
    item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if item is None:
        errors["item_id"] = "Choose an item."
    loc = conn.execute("SELECT * FROM locations WHERE id=?", (location_id,)).fetchone()
    if loc is None or loc["kind"] != "raw_store":
        errors["location_id"] = "Receipts go to a raw store location."
    qty = 0
    try:
        qty = parse_qty(form.get("qty"), field="Quantity")
    except QuantityError as e:
        errors["qty"] = str(e)
    if item is not None:
        unit = _clean(form.get("unit")) or item["unit"]
        if unit.lower() != item["unit"].lower():
            errors["unit"] = (f"This item is stocked in '{item['unit']}'. No unit conversion is configured, "
                              f"so '{unit}' cannot be accepted.")
    if lot_no and "lot_no" not in errors:
        if conn.execute("SELECT 1 FROM lots WHERE lot_no=?", (lot_no,)).fetchone() or conn.execute(
                "SELECT 1 FROM goods_receipts WHERE lot_no=? AND status IN ('draft','posted')", (lot_no,)).fetchone():
            errors["lot_no"] = f"Lot '{lot_no}' already exists or is already on another receipt."
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        doc_no = next_number(conn, "GRN", "GRN")
        cur = conn.execute(
            "INSERT INTO goods_receipts(doc_no, supplier_id, item_id, lot_no, location_id, qty, supplier_ref, "
            "vehicle_no, notes, status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,'draft',?,?)",
            (doc_no, supplier_id, item_id, lot_no, location_id, qty, _clean(form.get("supplier_ref")) or None,
             _clean(form.get("vehicle_no")) or None, _clean(form.get("notes")) or None, user["id"], now_iso()))
        audit.record(conn, user, "receipt.create_draft", "goods_receipt", doc_no,
                     detail={"lot_no": lot_no, "qty": fmt_qty(qty)})
        return cur.lastrowid


def post(conn: sqlite3.Connection, user, receipt_id: int) -> str:
    """Post a draft: creates the lot and the ledger line atomically. Re-checks everything now."""
    require(user, "receipt.post")
    with transaction(conn):
        r = conn.execute("SELECT * FROM goods_receipts WHERE id=?", (receipt_id,)).fetchone()
        if r is None:
            raise NotFound("Goods receipt not found.")
        if r["status"] != "draft":
            raise BusinessRuleViolation(f"Receipt {r['doc_no']} is {r['status']}; only drafts can be posted.")
        if conn.execute("SELECT 1 FROM lots WHERE lot_no=?", (r["lot_no"],)).fetchone():
            raise BusinessRuleViolation(f"Lot '{r['lot_no']}' already exists; cannot create it again.")
        cur = conn.execute(
            "INSERT INTO lots(lot_no, item_id, supplier_id, kind, created_at, created_by, is_demo) "
            "VALUES (?,?,?,'raw',?,?, (SELECT is_demo FROM items WHERE id=?))",
            (r["lot_no"], r["item_id"], r["supplier_id"], now_iso(), user["id"], r["item_id"]))
        lot_id = cur.lastrowid
        inventory.post_line(conn, lot_id=lot_id, location_id=r["location_id"], qty=r["qty"], movement="receipt",
                            source_type="goods_receipt", source_id=r["id"], line_key="main", actor_id=user["id"],
                            note=r["doc_no"])
        conn.execute("UPDATE goods_receipts SET status='posted', lot_id=?, posted_by=?, posted_at=? WHERE id=?",
                     (lot_id, user["id"], now_iso(), r["id"]))
        audit.record(conn, user, "receipt.post", "goods_receipt", r["doc_no"],
                     detail={"lot_no": r["lot_no"], "qty": fmt_qty(r["qty"])})
        return r["doc_no"]


def discard(conn: sqlite3.Connection, user, receipt_id: int) -> None:
    require(user, "receipt.create")
    with transaction(conn):
        r = conn.execute("SELECT * FROM goods_receipts WHERE id=?", (receipt_id,)).fetchone()
        if r is None:
            raise NotFound("Goods receipt not found.")
        if r["status"] != "draft":
            raise BusinessRuleViolation("Only draft receipts can be discarded. Posted receipts must be reversed.")
        conn.execute("UPDATE goods_receipts SET status='discarded' WHERE id=?", (r["id"],))
        audit.record(conn, user, "receipt.discard", "goods_receipt", r["doc_no"])


def reverse(conn: sqlite3.Connection, user, receipt_id: int, reason: str) -> None:
    """Reverse a posted receipt with linked ledger rows. Only allowed if the received
    quantity is still entirely where it was put (no other stock movement touched the lot)."""
    require(user, "receipt.reverse")
    reason = _clean(reason)
    if len(reason) < 5:
        raise ValidationFailed({"reason": "A reason of at least 5 characters is required."})
    with transaction(conn):
        r = conn.execute("SELECT * FROM goods_receipts WHERE id=?", (receipt_id,)).fetchone()
        if r is None:
            raise NotFound("Goods receipt not found.")
        if r["status"] != "posted":
            raise BusinessRuleViolation(f"Receipt {r['doc_no']} is {r['status']}; only posted receipts can be reversed.")
        other = conn.execute(
            "SELECT COUNT(*) AS c FROM stock_ledger WHERE lot_id=? AND NOT (source_type='goods_receipt' AND source_id=?)",
            (r["lot_id"], r["id"])).fetchone()["c"]
        if other:
            raise BusinessRuleViolation(
                "This lot has later stock movements, so the receipt cannot be reversed. "
                "A stock adjustment is needed (not yet implemented).")
        if inventory.reserved(conn, r["lot_id"], r["location_id"]):
            raise BusinessRuleViolation("This lot has active reservations.")
        orig = conn.execute("SELECT id FROM stock_ledger WHERE source_type='goods_receipt' AND source_id=? AND line_key='main'",
                            (r["id"],)).fetchone()
        inventory.post_line(conn, lot_id=r["lot_id"], location_id=r["location_id"], qty=-r["qty"],
                            movement="receipt_reversal", source_type="goods_receipt", source_id=r["id"],
                            line_key="reversal", actor_id=user["id"], reverses_id=orig["id"], note=reason)
        conn.execute("UPDATE goods_receipts SET status='reversed', reversed_by=?, reversed_at=?, reversal_reason=? WHERE id=?",
                     (user["id"], now_iso(), reason, r["id"]))
        audit.record(conn, user, "receipt.reverse", "goods_receipt", r["doc_no"], detail={"reason": reason})
