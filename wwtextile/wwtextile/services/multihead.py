"""Multihead production orders — a GENERIC, unit-explicit module.

What Multihead physically is, what its unit is, whether it consumes fabric and whether its output re-enters stock
are NOT confirmed (BUSINESS_RULES.md M1-M6). So:
  * the unit is chosen per order from an admin-configured list (setting `multihead.units`, "name[:decimals]");
  * fabric consumption is either RECORDED ONLY or POSTED to the stock ledger, per setting
    `multihead.fabric_stock_effect` ('record_only' default | 'consume');
  * output quantities are production records; they are NOT posted to any stock ledger.
Accessories consumed by an order go through ordinary Accessory Store issues that reference the order.
"""
from __future__ import annotations

import re
from datetime import date

from .. import audit
from ..db import get_setting, next_number, now_iso, transaction
from ..errors import BusinessRuleViolation, NotFound, ValidationFailed
from ..permissions import require
from ..quantity import QuantityError, fmt_qty, parse_qty
from . import accessories, inventory

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _c(v) -> str:
    return str(v if v is not None else "").strip()


def _date_ok(s: str) -> bool:
    if not DATE_RE.match(s):
        return False
    try:
        return date(2000, 1, 1) <= date.fromisoformat(s) <= date(2100, 12, 31)
    except ValueError:
        return False


def configured_units(conn) -> dict[str, int]:
    """{unit: decimals}. Empty until the administrator configures units."""
    out: dict[str, int] = {}
    for part in get_setting(conn, "multihead.units", "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, dec = part.partition(":")
        name = name.strip()
        if name:
            out[name] = 0 if dec.strip() == "0" else 3
    return out


def fabric_effect(conn) -> str:
    v = get_setting(conn, "multihead.fabric_stock_effect", "record_only")
    return v if v in ("record_only", "consume") else "record_only"


def _qty(text, decimals: int, field: str, allow_zero=False) -> int:
    q = parse_qty(text, field=field, allow_zero=allow_zero)
    if decimals == 0 and q % 1000:
        raise QuantityError(f"{field} must be a whole number for this unit.")
    return q


# ------------------------------------------------------------------ machines
def list_machines(conn, include_inactive=False):
    return conn.execute("SELECT * FROM mh_machines " + ("" if include_inactive else "WHERE active=1 ") + "ORDER BY code").fetchall()


def create_machine(conn, user, form: dict) -> int:
    require(user, "mh.machines.manage")
    code, name = _c(form.get("code")).upper(), " ".join(_c(form.get("name")).split())
    errors = {}
    if not re.match(r"^[A-Z0-9][A-Z0-9._-]{0,19}$", code):
        errors["code"] = "Code: letters, digits, . _ - (max 20)."
    elif conn.execute("SELECT 1 FROM mh_machines WHERE code=?", (code,)).fetchone():
        errors["code"] = "This code already exists."
    if not (2 <= len(name) <= 60):
        errors["name"] = "Name must be 2-60 characters."
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        cur = conn.execute("INSERT INTO mh_machines(code,name,active,is_demo) VALUES (?,?,1,0)", (code, name))
        audit.record(conn, user, "mh.machine_create", "mh_machine", code)
        return cur.lastrowid


def set_machine_active(conn, user, machine_id: int, active: bool) -> None:
    require(user, "mh.machines.manage")
    with transaction(conn):
        m = conn.execute("SELECT * FROM mh_machines WHERE id=?", (machine_id,)).fetchone()
        if m is None:
            raise NotFound("Machine not found.")
        conn.execute("UPDATE mh_machines SET active=? WHERE id=?", (1 if active else 0, machine_id))
        audit.record(conn, user, "mh.machine_activate" if active else "mh.machine_deactivate", "mh_machine", m["code"])


# ------------------------------------------------------------------ orders
def get_order(conn, order_id: int):
    o = conn.execute("SELECT o.*, m.code AS machine_code, m.name AS machine_name, u.username AS created_by_name "
                     "FROM mh_orders o LEFT JOIN mh_machines m ON m.id=o.machine_id JOIN users u ON u.id=o.created_by WHERE o.id=?", (order_id,)).fetchone()
    if o is None:
        raise NotFound("Multihead order not found.")
    return o


def list_orders(conn, status=None, q=None, limit=200):
    sql = ("SELECT o.*, m.code AS machine_code FROM mh_orders o LEFT JOIN mh_machines m ON m.id=o.machine_id WHERE 1=1 ")
    a: list = []
    if status:
        sql += "AND o.status=? "; a.append(status)
    if q:
        sql += "AND (o.order_no LIKE ? OR o.title LIKE ? OR o.operator LIKE ?) "; a += [f"%{q}%"] * 3
    rows = conn.execute(sql + "ORDER BY o.id DESC LIMIT ?", a + [limit]).fetchall()
    return [{**dict(r), **order_totals(conn, r["id"])} for r in rows]


def create_order(conn, user, form: dict) -> int:
    require(user, "mh.create")
    errors: dict[str, str] = {}
    units = configured_units(conn)
    if not units:
        raise BusinessRuleViolation("No Multihead units are configured. An administrator must define the unit(s) in Settings before orders can be created "
                                    "(the unit is not assumed).")
    title = " ".join(_c(form.get("title")).split())
    if not (2 <= len(title) <= 120):
        errors["title"] = "Title must be 2-120 characters."
    unit = _c(form.get("unit"))
    if unit not in units:
        errors["unit"] = "Choose one of the configured units."
    planned = None
    if _c(form.get("planned_qty")):
        try:
            planned = _qty(form.get("planned_qty"), units.get(unit, 3), "Planned quantity")
        except QuantityError as e:
            errors["planned_qty"] = str(e)
    machine_id = int(_c(form.get("machine_id"))) if _c(form.get("machine_id")).isdigit() else None
    if machine_id and not conn.execute("SELECT 1 FROM mh_machines WHERE id=? AND active=1", (machine_id,)).fetchone():
        errors["machine_id"] = "Unknown or inactive machine."
    due = _c(form.get("due_date"))
    if due and not _date_ok(due):
        errors["due_date"] = "Enter a valid date (YYYY-MM-DD)."
    operator = " ".join(_c(form.get("operator")).split())[:60]
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        no = next_number(conn, "MH", "MH")
        cur = conn.execute("INSERT INTO mh_orders(order_no,title,unit,planned_qty,machine_id,operator,due_date,notes,status,created_by,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,'open',?,?)",
                           (no, title, unit, planned, machine_id, operator or None, due or None, _c(form.get("notes")) or None, user["id"], now_iso()))
        audit.record(conn, user, "mh.order_create", "mh_order", no, detail={"unit": unit, "planned": fmt_qty(planned) if planned else None})
        return cur.lastrowid


def _open_order(conn, order_id):
    o = conn.execute("SELECT * FROM mh_orders WHERE id=?", (order_id,)).fetchone()
    if o is None:
        raise NotFound("Multihead order not found.")
    if o["status"] != "open":
        raise BusinessRuleViolation(f"{o['order_no']} is {o['status']}; it no longer accepts changes.")
    return o


def order_logs(conn, order_id: int):
    return conn.execute("SELECT l.*, m.code AS machine_code, u.username AS created_by_name FROM mh_logs l LEFT JOIN mh_machines m ON m.id=l.machine_id "
                        "JOIN users u ON u.id=l.created_by WHERE l.order_id=? ORDER BY l.log_date DESC, l.id DESC", (order_id,)).fetchall()


def order_consumptions(conn, order_id: int):
    return conn.execute("SELECT c.*, l.lot_no, loc.name AS loc_name, i.unit FROM mh_consumptions c JOIN lots l ON l.id=c.lot_id "
                        "JOIN locations loc ON loc.id=c.location_id JOIN items i ON i.id=l.item_id WHERE c.order_id=? ORDER BY c.id DESC", (order_id,)).fetchall()


def order_totals(conn, order_id: int) -> dict:
    """Single source of the numbers shown for an order (page, list, reports, assistant)."""
    t = conn.execute("SELECT COALESCE(SUM(good_qty),0) AS g, COALESCE(SUM(rejected_qty),0) AS r, COUNT(*) AS n "
                     "FROM mh_logs WHERE order_id=? AND voided_at IS NULL", (order_id,)).fetchone()
    c = conn.execute("SELECT COALESCE(SUM(qty),0) AS q, COALESCE(SUM(waste_qty),0) AS w FROM mh_consumptions WHERE order_id=? AND voided_at IS NULL", (order_id,)).fetchone()
    o = conn.execute("SELECT planned_qty, due_date, status FROM mh_orders WHERE id=?", (order_id,)).fetchone()
    pct = None
    if o and o["planned_qty"]:
        pct = (t["g"] * 100) // o["planned_qty"]
    return {"good": t["g"], "rejected": t["r"], "log_count": t["n"], "fabric_consumed": c["q"], "fabric_waste": c["w"],
            "progress_pct": pct, "overdue": bool(o and o["status"] == "open" and o["due_date"] and o["due_date"] < date.today().isoformat())}


def log_production(conn, user, order_id: int, form: dict) -> int:
    require(user, "mh.log")
    errors: dict[str, str] = {}
    with transaction(conn):
        o = _open_order(conn, order_id)
        dec = configured_units(conn).get(o["unit"], 3)
        good = rej = 0
        for key, label in (("good_qty", "Good output"), ("rejected_qty", "Rejected")):
            try:
                v = _qty(form.get(key) or "0", dec, label, allow_zero=True)
            except QuantityError as e:
                errors[key] = str(e)
                continue
            if key == "good_qty":
                good = v
            else:
                rej = v
        if not errors and good + rej == 0:
            errors["good_qty"] = "Enter good and/or rejected quantity."
        d = _c(form.get("log_date")) or date.today().isoformat()
        if not _date_ok(d):
            errors["log_date"] = "Enter a valid date (YYYY-MM-DD)."
        elif d > date.today().isoformat():
            errors["log_date"] = "Production cannot be logged for a future date."
        machine_id = int(_c(form.get("machine_id"))) if _c(form.get("machine_id")).isdigit() else o["machine_id"]
        if machine_id and not conn.execute("SELECT 1 FROM mh_machines WHERE id=?", (machine_id,)).fetchone():
            errors["machine_id"] = "Unknown machine."
        if errors:
            raise ValidationFailed(errors)
        cur = conn.execute("INSERT INTO mh_logs(order_id,log_date,machine_id,operator,good_qty,rejected_qty,notes,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (order_id, d, machine_id, " ".join(_c(form.get("operator")).split())[:60] or o["operator"], good, rej,
                            _c(form.get("notes")) or None, user["id"], now_iso()))
        audit.record(conn, user, "mh.log", "mh_order", o["order_no"], detail={"good": fmt_qty(good), "rejected": fmt_qty(rej), "unit": o["unit"]})
        return cur.lastrowid


def void_log(conn, user, log_id: int, reason: str) -> None:
    require(user, "mh.close")
    reason = _c(reason)
    if len(reason) < 5:
        raise ValidationFailed({"reason": "A reason of at least 5 characters is required."})
    with transaction(conn):
        l = conn.execute("SELECT * FROM mh_logs WHERE id=?", (log_id,)).fetchone()
        if l is None:
            raise NotFound("Log not found.")
        if l["voided_at"]:
            raise BusinessRuleViolation("This log is already voided.")
        o = _open_order(conn, l["order_id"])
        conn.execute("UPDATE mh_logs SET voided_by=?, voided_at=?, void_reason=? WHERE id=?", (user["id"], now_iso(), reason, log_id))
        audit.record(conn, user, "mh.log_void", "mh_order", o["order_no"], detail={"log": log_id, "reason": reason})


def consume_fabric(conn, user, order_id: int, form: dict) -> int:
    require(user, "mh.consume")
    errors: dict[str, str] = {}
    with transaction(conn):
        o = _open_order(conn, order_id)
        lot = conn.execute("SELECT l.*, i.unit FROM lots l JOIN items i ON i.id=l.item_id WHERE l.lot_no=?", (_c(form.get("lot_no")),)).fetchone()
        if lot is None:
            errors["lot_no"] = "Lot not found."
        loc_id = int(_c(form.get("location_id"))) if _c(form.get("location_id")).isdigit() else 0
        loc = conn.execute("SELECT * FROM locations WHERE id=?", (loc_id,)).fetchone()
        if loc is None or loc["kind"] not in ("raw_store", "finished_store"):
            errors["location_id"] = "Choose the Raw Store or Finished Store location the fabric is taken from."
        qty = waste = 0
        try:
            qty = parse_qty(form.get("qty"), field="Quantity")
        except QuantityError as e:
            errors["qty"] = str(e)
        try:
            waste = parse_qty(form.get("waste_qty") or "0", field="Waste", allow_zero=True)
            if qty and waste > qty:
                errors["waste_qty"] = "Waste cannot exceed the quantity consumed."
        except QuantityError as e:
            errors["waste_qty"] = str(e)
        if errors:
            raise ValidationFailed(errors)
        effect = "posted" if fabric_effect(conn) == "consume" else "record_only"
        if effect == "posted":
            avail = inventory.available(conn, lot["id"], loc["id"])
            if qty > avail:
                raise BusinessRuleViolation(f"Only {fmt_qty(avail)} {lot['unit']} of lot {lot['lot_no']} is available at {loc['name']} "
                                            f"(on hand minus reservations); {fmt_qty(qty)} requested.")
        cur = conn.execute("INSERT INTO mh_consumptions(order_id,lot_id,location_id,qty,waste_qty,stock_effect,notes,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (order_id, lot["id"], loc["id"], qty, waste, effect, _c(form.get("notes")) or None, user["id"], now_iso()))
        cid = cur.lastrowid
        if effect == "posted":
            inventory.post_line(conn, lot_id=lot["id"], location_id=loc["id"], qty=-qty, movement="multihead_consumption",
                                source_type="mh_consumption", source_id=cid, line_key="main", actor_id=user["id"], note=o["order_no"])
        audit.record(conn, user, "mh.consume_fabric", "mh_order", o["order_no"],
                     detail={"lot": lot["lot_no"], "qty": fmt_qty(qty), "waste": fmt_qty(waste), "stock_effect": effect})
        return cid


def void_consumption(conn, user, cid: int, reason: str) -> None:
    require(user, "mh.consume")
    reason = _c(reason)
    if len(reason) < 5:
        raise ValidationFailed({"reason": "A reason of at least 5 characters is required."})
    with transaction(conn):
        c = conn.execute("SELECT * FROM mh_consumptions WHERE id=?", (cid,)).fetchone()
        if c is None:
            raise NotFound("Consumption not found.")
        if c["voided_at"]:
            raise BusinessRuleViolation("Already voided.")
        o = _open_order(conn, c["order_id"])
        if c["stock_effect"] == "posted":
            orig = conn.execute("SELECT id FROM stock_ledger WHERE source_type='mh_consumption' AND source_id=? AND line_key='main'", (cid,)).fetchone()
            inventory.post_line(conn, lot_id=c["lot_id"], location_id=c["location_id"], qty=c["qty"], movement="multihead_consumption_reversal",
                                source_type="mh_consumption", source_id=cid, line_key="reversal", actor_id=user["id"], reverses_id=orig["id"], note=reason)
        conn.execute("UPDATE mh_consumptions SET voided_by=?, voided_at=?, void_reason=? WHERE id=?", (user["id"], now_iso(), reason, cid))
        audit.record(conn, user, "mh.consume_void", "mh_order", o["order_no"], detail={"consumption": cid, "reason": reason})


def close_order(conn, user, order_id: int, outcome: str, note: str) -> None:
    require(user, "mh.close")
    if outcome not in ("completed", "cancelled"):
        raise ValidationFailed({"outcome": "Unknown outcome."})
    note = _c(note)
    if outcome == "cancelled" and len(note) < 5:
        raise ValidationFailed({"note": "A reason of at least 5 characters is required to cancel."})
    with transaction(conn):
        o = _open_order(conn, order_id)
        if outcome == "completed" and order_totals(conn, order_id)["log_count"] == 0:
            raise BusinessRuleViolation("An order with no production log cannot be completed. Log output, or cancel the order.")
        conn.execute("UPDATE mh_orders SET status=?, closed_by=?, closed_at=?, close_note=? WHERE id=?", (outcome, user["id"], now_iso(), note or None, order_id))
        audit.record(conn, user, "mh.order_" + outcome, "mh_order", o["order_no"], detail={"note": note} if note else None)


def linked_accessory_docs(conn, order_no: str):
    return accessories.list_docs(conn, ref_type="multihead_order", ref_no=order_no)
