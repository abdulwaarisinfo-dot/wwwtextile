"""Accessory Store: item catalogue, receipts, issues, transfers, ledger, reorder alerts.

Same integrity rules as fabric: draft -> posted -> reversed, append-only ledger, no negative stock,
one posting per document line (UNIQUE source key), reversals link to the original rows.
Units are per item and never converted. Fractional quantities are only allowed when item.decimals = 3.
"""
from __future__ import annotations

import re
import sqlite3

from .. import audit
from ..db import next_number, now_iso, transaction
from ..errors import BusinessRuleViolation, NotFound, ValidationFailed
from ..permissions import require
from ..quantity import QuantityError, fmt_qty, parse_qty
from . import notifications

DOC_TYPES = {"receipt": "Receipt", "issue": "Issue", "transfer": "Transfer"}
REF_TYPES = {"processing_job": "Processing job", "multihead_order": "Multihead order", "other": "Other (department / cost centre)"}
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,29}$")
MAX_LINES = 8


def _c(v) -> str:
    return str(v if v is not None else "").strip()


# ----------------------------------------------------------------- catalogue
def list_items(conn, q: str | None = None, include_inactive: bool = False):
    sql, a = "SELECT * FROM acc_items WHERE 1=1 ", []
    if not include_inactive:
        sql += "AND active=1 "
    if q:
        sql += "AND (code LIKE ? OR name LIKE ? OR category LIKE ?) "
        a += [f"%{q}%"] * 3
    return conn.execute(sql + "ORDER BY name", a).fetchall()


def get_item(conn, item_id: int):
    r = conn.execute("SELECT * FROM acc_items WHERE id=?", (item_id,)).fetchone()
    if r is None:
        raise NotFound("Accessory item not found.")
    return r


def list_locations(conn, include_inactive: bool = False):
    return conn.execute("SELECT * FROM acc_locations " + ("" if include_inactive else "WHERE active=1 ") + "ORDER BY id").fetchall()


def _qty_for_item(text, item, field="Quantity", allow_zero=False) -> int:
    q = parse_qty(text, field=field, allow_zero=allow_zero)
    if item["decimals"] == 0 and q % 1000:
        raise QuantityError(f"{field}: '{item['name']}' is counted in whole {item['unit']} (no decimals).")
    return q


def create_item(conn, user, form: dict) -> int:
    require(user, "acc.items.manage")
    code, name = _c(form.get("code")), " ".join(_c(form.get("name")).split())
    category, unit = " ".join(_c(form.get("category")).split()), _c(form.get("unit"))
    decimals = 3 if _c(form.get("decimals")) == "3" else 0
    errors = {}
    if not CODE_RE.match(code):
        errors["code"] = "Code: letters, digits, . _ - (max 30)."
    elif conn.execute("SELECT 1 FROM acc_items WHERE code=?", (code,)).fetchone():
        errors["code"] = "This code already exists."
    if not (2 <= len(name) <= 80):
        errors["name"] = "Name must be 2-80 characters."
    if not (1 <= len(unit) <= 10):
        errors["unit"] = "Unit is required (e.g. pcs, box, kg), max 10 characters."
    if len(category) > 40:
        errors["category"] = "Category max 40 characters."
    reorder = 0
    if _c(form.get("reorder_level")):
        try:
            reorder = parse_qty(form.get("reorder_level"), allow_zero=True, field="Reorder level")
            if decimals == 0 and reorder % 1000:
                errors["reorder_level"] = "Reorder level must be a whole number for this item."
        except QuantityError as e:
            errors["reorder_level"] = str(e)
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        cur = conn.execute("INSERT INTO acc_items(code,name,category,unit,decimals,reorder_level,active,is_demo,created_at) "
                           "VALUES (?,?,?,?,?,?,1,0,?)", (code, name, category or None, unit, decimals, reorder, now_iso()))
        audit.record(conn, user, "acc.item_create", "acc_item", code, detail={"unit": unit, "reorder_level": fmt_qty(reorder)})
        return cur.lastrowid


def update_item(conn, user, item_id: int, form: dict) -> None:
    """Name, category, reorder level and active flag may change. Unit/decimals never change (would corrupt history)."""
    require(user, "acc.items.manage")
    errors = {}
    with transaction(conn):
        it = get_item(conn, item_id)
        name = " ".join(_c(form.get("name")).split()) or it["name"]
        category = " ".join(_c(form.get("category")).split())
        reorder = it["reorder_level"]
        if _c(form.get("reorder_level")) != "":
            try:
                reorder = parse_qty(form.get("reorder_level"), allow_zero=True, field="Reorder level")
                if it["decimals"] == 0 and reorder % 1000:
                    errors["reorder_level"] = "Reorder level must be a whole number for this item."
            except QuantityError as e:
                errors["reorder_level"] = str(e)
        if not (2 <= len(name) <= 80):
            errors["name"] = "Name must be 2-80 characters."
        if errors:
            raise ValidationFailed(errors)
        active = 0 if _c(form.get("active")) == "0" else 1
        if not active and item_total(conn, item_id):
            raise BusinessRuleViolation("This item still has stock on hand; it cannot be deactivated.")
        conn.execute("UPDATE acc_items SET name=?, category=?, reorder_level=?, active=? WHERE id=?",
                     (name, category or None, reorder, active, item_id))
        audit.record(conn, user, "acc.item_update", "acc_item", it["code"],
                     detail={"reorder_level": fmt_qty(reorder), "active": active})


def create_location(conn, user, form: dict) -> int:
    require(user, "acc.items.manage")
    code, name = _c(form.get("code")).upper(), " ".join(_c(form.get("name")).split())
    errors = {}
    if not CODE_RE.match(code):
        errors["code"] = "Code: letters, digits, . _ - (max 30)."
    elif conn.execute("SELECT 1 FROM acc_locations WHERE code=?", (code,)).fetchone():
        errors["code"] = "This code already exists."
    if not (2 <= len(name) <= 60):
        errors["name"] = "Name must be 2-60 characters."
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        cur = conn.execute("INSERT INTO acc_locations(code,name,active) VALUES (?,?,1)", (code, name))
        audit.record(conn, user, "acc.location_create", "acc_location", code)
        return cur.lastrowid


# ----------------------------------------------------------------- balances (single source of truth)
def on_hand(conn, item_id: int, location_id: int) -> int:
    return conn.execute("SELECT COALESCE(SUM(qty),0) AS q FROM acc_ledger WHERE item_id=? AND location_id=?",
                        (item_id, location_id)).fetchone()["q"]


def item_total(conn, item_id: int) -> int:
    return conn.execute("SELECT COALESCE(SUM(qty),0) AS q FROM acc_ledger WHERE item_id=?", (item_id,)).fetchone()["q"]


def stock_summary(conn, q: str | None = None, location_id: int | None = None, low_only: bool = False):
    """One row per item with total and per-location balances; low flag = total <= reorder level (level > 0)."""
    rows = []
    for it in list_items(conn, q, include_inactive=True):
        per = conn.execute(
            "SELECT l.id AS location_id, l.name, COALESCE(SUM(a.qty),0) AS on_hand FROM acc_locations l "
            "LEFT JOIN acc_ledger a ON a.location_id=l.id AND a.item_id=? GROUP BY l.id ORDER BY l.id",
            (it["id"],)).fetchall()
        per = [dict(r) for r in per if r["on_hand"]]
        if location_id:
            per = [p for p in per if p["location_id"] == location_id]
            total = sum(p["on_hand"] for p in per)
        else:
            total = item_total(conn, it["id"])
        low = bool(it["reorder_level"]) and item_total(conn, it["id"]) <= it["reorder_level"] and it["active"]
        if low_only and not low:
            continue
        if not it["active"] and not total:
            continue
        if location_id and not total and not low_only:
            continue
        rows.append({"item": it, "total": total, "per_location": per, "low": low})
    return rows


def low_stock(conn):
    return [r for r in stock_summary(conn, low_only=True)]


def ledger_rows(conn, *, item_id=None, location_id=None, movement=None, ts_from=None, ts_to=None, limit=200, offset=0):
    sql = ("FROM acc_ledger a JOIN acc_items i ON i.id=a.item_id JOIN acc_locations l ON l.id=a.location_id "
           "LEFT JOIN users u ON u.id=a.actor_id WHERE 1=1 ")
    args: list = []
    if item_id:
        sql += "AND a.item_id=? "; args.append(item_id)
    if location_id:
        sql += "AND a.location_id=? "; args.append(location_id)
    if movement:
        sql += "AND a.movement=? "; args.append(movement)
    if ts_from:
        sql += "AND a.posted_at >= ? "; args.append(ts_from)
    if ts_to:
        sql += "AND a.posted_at < ? "; args.append(ts_to)
    count = conn.execute("SELECT COUNT(*) AS c " + sql, args).fetchone()["c"]
    rows = conn.execute("SELECT a.*, i.code AS item_code, i.name AS item_name, i.unit, i.decimals, l.name AS loc_name, u.username AS actor " + sql +
                        "ORDER BY a.id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    return rows, count


# ----------------------------------------------------------------- documents
def get_doc(conn, doc_id: int):
    d = conn.execute(
        "SELECT d.*, p.name AS supplier_name, fl.name AS from_name, tl.name AS to_name, cu.username AS created_by_name, "
        "pu.username AS posted_by_name FROM acc_docs d LEFT JOIN parties p ON p.id=d.supplier_id "
        "LEFT JOIN acc_locations fl ON fl.id=d.from_location_id LEFT JOIN acc_locations tl ON tl.id=d.to_location_id "
        "JOIN users cu ON cu.id=d.created_by LEFT JOIN users pu ON pu.id=d.posted_by WHERE d.id=?", (doc_id,)).fetchone()
    if d is None:
        raise NotFound("Accessory document not found.")
    return d


def doc_lines(conn, doc_id: int):
    return conn.execute("SELECT l.*, i.code, i.name, i.unit, i.decimals FROM acc_doc_lines l JOIN acc_items i ON i.id=l.item_id "
                        "WHERE l.doc_id=? ORDER BY l.id", (doc_id,)).fetchall()


def list_docs(conn, doc_type=None, status=None, q=None, ref_type=None, ref_no=None, limit=200):
    sql = ("SELECT d.*, p.name AS supplier_name, fl.name AS from_name, tl.name AS to_name, "
           "(SELECT COUNT(*) FROM acc_doc_lines WHERE doc_id=d.id) AS n_lines FROM acc_docs d "
           "LEFT JOIN parties p ON p.id=d.supplier_id LEFT JOIN acc_locations fl ON fl.id=d.from_location_id "
           "LEFT JOIN acc_locations tl ON tl.id=d.to_location_id WHERE 1=1 ")
    a: list = []
    for col, v in (("d.doc_type", doc_type), ("d.status", status), ("d.ref_type", ref_type), ("d.ref_no", ref_no)):
        if v:
            sql += f"AND {col}=? "; a.append(v)
    if q:
        sql += "AND (d.doc_no LIKE ? OR d.ref_no LIKE ? OR d.supplier_ref LIKE ? OR p.name LIKE ?) "
        a += [f"%{q}%"] * 4
    return conn.execute(sql + "ORDER BY d.id DESC LIMIT ?", a + [limit]).fetchall()


def _validate_ref(conn, ref_type: str, ref_no: str, required: bool, errors: dict) -> None:
    if not ref_type and not ref_no:
        if required:
            errors["ref_type"] = "An issue must reference what it is consumed against."
        return
    if ref_type not in REF_TYPES:
        errors["ref_type"] = "Choose a reference type."
        return
    if not ref_no:
        errors["ref_no"] = "Reference is required."
        return
    if ref_type == "processing_job":
        if conn.execute("SELECT 1 FROM processing_jobs WHERE job_no=?", (ref_no,)).fetchone() is None:
            errors["ref_no"] = f"Processing job '{ref_no}' does not exist."
    elif ref_type == "multihead_order":
        row = conn.execute("SELECT status FROM mh_orders WHERE order_no=?", (ref_no,)).fetchone()
        if row is None:
            errors["ref_no"] = f"Multihead order '{ref_no}' does not exist."
        elif row["status"] != "open":
            errors["ref_no"] = f"Multihead order '{ref_no}' is {row['status']}; issues can only be made against open orders."
    elif not (2 <= len(ref_no) <= 80):
        errors["ref_no"] = "Reference must be 2-80 characters."


def create_draft(conn, user, doc_type: str, form: dict, lines: list[tuple[str, str]]) -> int:
    require(user, "acc.create")
    if doc_type not in DOC_TYPES:
        raise ValidationFailed({"doc_type": "Unknown document type."})
    errors: dict[str, str] = {}
    supplier_id = from_id = to_id = None
    if doc_type == "receipt":
        supplier_id = int(_c(form.get("supplier_id")) or 0) if _c(form.get("supplier_id")).isdigit() else 0
        if not conn.execute("SELECT 1 FROM parties WHERE id=? AND kind='supplier'", (supplier_id,)).fetchone():
            errors["supplier_id"] = "Choose a supplier."
    if doc_type in ("issue", "transfer"):
        from_id = int(_c(form.get("from_location_id"))) if _c(form.get("from_location_id")).isdigit() else 0
        if not conn.execute("SELECT 1 FROM acc_locations WHERE id=? AND active=1", (from_id,)).fetchone():
            errors["from_location_id"] = "Choose the source location."
    if doc_type in ("receipt", "transfer"):
        to_id = int(_c(form.get("to_location_id"))) if _c(form.get("to_location_id")).isdigit() else 0
        if not conn.execute("SELECT 1 FROM acc_locations WHERE id=? AND active=1", (to_id,)).fetchone():
            errors["to_location_id"] = "Choose the destination location."
    if doc_type == "transfer" and from_id and from_id == to_id:
        errors["to_location_id"] = "Source and destination must differ."
    ref_type, ref_no = _c(form.get("ref_type")), _c(form.get("ref_no"))
    if doc_type == "issue" or ref_type or ref_no:
        _validate_ref(conn, ref_type, ref_no, doc_type == "issue", errors)
    clean: list[tuple[int, int]] = []
    seen: set[int] = set()
    for idx, (item_id_s, qty_s) in enumerate(lines[:MAX_LINES], start=1):
        item_id_s, qty_s = _c(item_id_s), _c(qty_s)
        if not item_id_s and not qty_s:
            continue
        key = f"line_{idx}"
        item = conn.execute("SELECT * FROM acc_items WHERE id=? AND active=1", (int(item_id_s),)).fetchone() if item_id_s.isdigit() else None
        if item is None:
            errors[key] = f"Line {idx}: choose an item."
            continue
        if item["id"] in seen:
            errors[key] = f"Line {idx}: '{item['name']}' appears twice; combine the quantities."
            continue
        try:
            q = _qty_for_item(qty_s, item, field=f"Line {idx} quantity")
        except QuantityError as e:
            errors[key] = str(e)
            continue
        seen.add(item["id"])
        clean.append((item["id"], q))
    if not clean and not any(k.startswith("line_") for k in errors):
        errors["lines"] = "Add at least one item line."
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        doc_no = next_number(conn, f"ACC-{doc_type}", {"receipt": "ARC", "issue": "AIS", "transfer": "ATR"}[doc_type])
        cur = conn.execute(
            "INSERT INTO acc_docs(doc_no,doc_type,supplier_id,from_location_id,to_location_id,ref_type,ref_no,supplier_ref,notes,status,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'draft',?,?)",
            (doc_no, doc_type, supplier_id or None, from_id or None, to_id or None, ref_type or None, ref_no or None,
             _c(form.get("supplier_ref")) or None, _c(form.get("notes")) or None, user["id"], now_iso()))
        did = cur.lastrowid
        conn.executemany("INSERT INTO acc_doc_lines(doc_id,item_id,qty) VALUES (?,?,?)", [(did, i, q) for i, q in clean])
        audit.record(conn, user, "acc.create_draft", "acc_doc", doc_no, detail={"type": doc_type, "lines": len(clean)})
        return did


def post(conn, user, doc_id: int) -> str:
    require(user, "acc.post")
    with transaction(conn):
        d = conn.execute("SELECT * FROM acc_docs WHERE id=?", (doc_id,)).fetchone()
        if d is None:
            raise NotFound("Accessory document not found.")
        if d["status"] != "draft":
            raise BusinessRuleViolation(f"{d['doc_no']} is {d['status']}; only drafts can be posted.")
        errors: dict = {}
        if d["ref_type"]:
            _validate_ref(conn, d["ref_type"], d["ref_no"], False, errors)
            if errors:
                raise BusinessRuleViolation("Reference check failed now: " + "; ".join(errors.values()))
        lines = conn.execute("SELECT l.*, i.name, i.active, i.unit FROM acc_doc_lines l JOIN acc_items i ON i.id=l.item_id WHERE doc_id=?", (doc_id,)).fetchall()
        for l in lines:
            if not l["active"]:
                raise BusinessRuleViolation(f"Item '{l['name']}' is inactive.")
            if d["doc_type"] in ("issue", "transfer"):
                have = on_hand(conn, l["item_id"], d["from_location_id"])
                if have < l["qty"]:
                    raise BusinessRuleViolation(f"Insufficient stock of '{l['name']}' at source: on hand {fmt_qty(have)} {l['unit']}, "
                                                f"requested {fmt_qty(l['qty'])}.")
            _post_line(conn, d, l, user)
        conn.execute("UPDATE acc_docs SET status='posted', posted_by=?, posted_at=? WHERE id=?", (user["id"], now_iso(), doc_id))
        audit.record(conn, user, "acc.post", "acc_doc", d["doc_no"], detail={"type": d["doc_type"], "lines": len(lines)})
        return d["doc_no"]


def _ledger(conn, *, item_id, location_id, qty, movement, doc, line_key, actor, reverses_id=None, note=None) -> int:
    try:
        cur = conn.execute(
            "INSERT INTO acc_ledger(posted_at,item_id,location_id,qty,movement,source_type,source_id,line_key,reverses_id,actor_id,note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), item_id, location_id, qty, movement, "acc_doc", doc["id"], line_key, reverses_id, actor["id"], note or doc["doc_no"]))
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise BusinessRuleViolation("This document line has already been posted.") from None
        raise BusinessRuleViolation(f"Ledger rejected the entry: {e}") from None
    return cur.lastrowid


def _post_line(conn, d, l, user) -> None:
    t = d["doc_type"]
    if t == "receipt":
        _ledger(conn, item_id=l["item_id"], location_id=d["to_location_id"], qty=l["qty"], movement="acc_receipt", doc=d,
                line_key=f"{l['item_id']}:in", actor=user)
    elif t == "issue":
        _ledger(conn, item_id=l["item_id"], location_id=d["from_location_id"], qty=-l["qty"], movement="acc_issue", doc=d,
                line_key=f"{l['item_id']}:out", actor=user, note=f"{d['doc_no']} -> {d['ref_type']}:{d['ref_no']}")
    else:
        _ledger(conn, item_id=l["item_id"], location_id=d["from_location_id"], qty=-l["qty"], movement="acc_transfer_out", doc=d,
                line_key=f"{l['item_id']}:out", actor=user)
        _ledger(conn, item_id=l["item_id"], location_id=d["to_location_id"], qty=l["qty"], movement="acc_transfer_in", doc=d,
                line_key=f"{l['item_id']}:in", actor=user)


def discard(conn, user, doc_id: int) -> None:
    require(user, "acc.create")
    with transaction(conn):
        d = conn.execute("SELECT * FROM acc_docs WHERE id=?", (doc_id,)).fetchone()
        if d is None:
            raise NotFound("Accessory document not found.")
        if d["status"] != "draft":
            raise BusinessRuleViolation("Only drafts can be discarded. Posted documents must be reversed.")
        conn.execute("UPDATE acc_docs SET status='discarded' WHERE id=?", (doc_id,))
        audit.record(conn, user, "acc.discard", "acc_doc", d["doc_no"])


def reverse(conn, user, doc_id: int, reason: str) -> None:
    require(user, "acc.reverse")
    reason = _c(reason)
    if len(reason) < 5:
        raise ValidationFailed({"reason": "A reason of at least 5 characters is required."})
    with transaction(conn):
        d = conn.execute("SELECT * FROM acc_docs WHERE id=?", (doc_id,)).fetchone()
        if d is None:
            raise NotFound("Accessory document not found.")
        if d["status"] != "posted":
            raise BusinessRuleViolation(f"{d['doc_no']} is {d['status']}; only posted documents can be reversed.")
        rows = conn.execute("SELECT * FROM acc_ledger WHERE source_type='acc_doc' AND source_id=? AND reverses_id IS NULL ORDER BY id", (doc_id,)).fetchall()
        for r in rows:
            have = on_hand(conn, r["item_id"], r["location_id"])
            if r["qty"] > 0 and have < r["qty"]:
                it = get_item(conn, r["item_id"])
                raise BusinessRuleViolation(
                    f"Cannot reverse: '{it['name']}' no longer has {fmt_qty(r['qty'])} {it['unit']} at that location "
                    f"(on hand {fmt_qty(have)}). Later movements must be dealt with first.")
            _ledger(conn, item_id=r["item_id"], location_id=r["location_id"], qty=-r["qty"], movement="acc_reversal", doc=d,
                    line_key=r["line_key"] + ":rev", actor=user, reverses_id=r["id"], note=reason)
        conn.execute("UPDATE acc_docs SET status='reversed', reversed_by=?, reversed_at=?, reversal_reason=? WHERE id=?",
                     (user["id"], now_iso(), reason, doc_id))
        audit.record(conn, user, "acc.reverse", "acc_doc", d["doc_no"], detail={"reason": reason})


def consumption_by_reference(conn, ref_type: str, ref_no: str):
    """Net accessory issued against a reference (issues minus reversed issues). Used by orders and reports."""
    return conn.execute(
        "SELECT i.code, i.name, i.unit, -SUM(a.qty) AS qty FROM acc_ledger a JOIN acc_docs d ON d.id=a.source_id AND a.source_type='acc_doc' "
        "JOIN acc_items i ON i.id=a.item_id WHERE d.ref_type=? AND d.ref_no=? AND d.doc_type='issue' GROUP BY i.id HAVING SUM(a.qty) <> 0 ORDER BY i.name",
        (ref_type, ref_no)).fetchall()


def reconcile(conn) -> list[str]:
    """Integrity check for the accessory ledger (used by `check`): no negative balances, no dangling reversals."""
    problems = []
    for r in conn.execute("SELECT item_id, location_id, SUM(qty) AS q FROM acc_ledger GROUP BY item_id, location_id HAVING SUM(qty) < 0"):
        problems.append(f"negative accessory balance item={r['item_id']} location={r['location_id']} qty={r['q']}")
    for r in conn.execute("SELECT a.id FROM acc_ledger a LEFT JOIN acc_ledger o ON o.id=a.reverses_id WHERE a.reverses_id IS NOT NULL AND o.id IS NULL"):
        problems.append(f"dangling accessory reversal id={r['id']}")
    return problems
