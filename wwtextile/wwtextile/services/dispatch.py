"""Dispatch, reservation, gate pass and exit confirmation.

Lifecycle:  draft -> approved (stock RESERVED) -> pass_issued -> exited   (or cancelled)

Reservations reduce AVAILABLE stock from approval onward. The ledger deduction
happens exactly once, at the event named by setting `dispatch.deduction_event`
(dispatch_approval | gate_pass_issue | exit_confirm). Exit confirmation is a
safety net: it deducts only if no earlier event already did. The unique key on
(source_type, source_id, line_key) makes a second deduction impossible.
"""
from __future__ import annotations

import sqlite3

from .. import audit
from ..db import get_setting, next_number, now_iso, transaction
from ..errors import BusinessRuleViolation, NotFound, ValidationFailed
from ..permissions import require
from ..quantity import QuantityError, fmt_qty, parse_qty
from . import inventory

DEDUCTION_EVENTS = ("dispatch_approval", "gate_pass_issue", "exit_confirm")
DEFAULT_EVENT = "exit_confirm"  # placeholder pending owner confirmation (BUSINESS_RULES.md A3)


def _clean(v) -> str:
    return str(v if v is not None else "").strip()


def deduction_event(conn: sqlite3.Connection) -> str:
    v = get_setting(conn, "dispatch.deduction_event", DEFAULT_EVENT)
    return v if v in DEDUCTION_EVENTS else DEFAULT_EVENT


def require_separate_approver(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, "dispatch.require_separate_approver", "1") == "1"


# ---------------------------------------------------------------- reads
def get(conn: sqlite3.Connection, dispatch_id: int):
    d = conn.execute(
        "SELECT d.*, p.name AS customer_name, cu.username AS created_by_name, au.username AS approved_by_name "
        "FROM dispatches d JOIN parties p ON p.id=d.customer_id JOIN users cu ON cu.id=d.created_by "
        "LEFT JOIN users au ON au.id=d.approved_by WHERE d.id=?", (dispatch_id,)).fetchone()
    if d is None:
        raise NotFound("Dispatch not found.")
    return d


def lines(conn: sqlite3.Connection, dispatch_id: int):
    return conn.execute(
        "SELECT dl.*, l.lot_no, i.unit AS unit, loc.name AS location_name FROM dispatch_lines dl "
        "JOIN lots l ON l.id=dl.lot_id JOIN items i ON i.id=l.item_id JOIN locations loc ON loc.id=dl.location_id "
        "WHERE dl.dispatch_id=? ORDER BY dl.id", (dispatch_id,)).fetchall()


def gate_pass_for(conn: sqlite3.Connection, dispatch_id: int):
    return conn.execute(
        "SELECT gp.*, iu.username AS issued_by_name, eu.username AS exited_by_name FROM gate_passes gp "
        "JOIN users iu ON iu.id=gp.issued_by LEFT JOIN users eu ON eu.id=gp.exited_by WHERE gp.dispatch_id=?",
        (dispatch_id,)).fetchone()


def list_dispatches(conn: sqlite3.Connection, status: str | None = None, q: str | None = None):
    sql = ("SELECT d.*, p.name AS customer_name, "
           "(SELECT COALESCE(SUM(qty),0) FROM dispatch_lines WHERE dispatch_id=d.id) AS total_qty "
           "FROM dispatches d JOIN parties p ON p.id=d.customer_id WHERE 1=1 ")
    args: list = []
    if status:
        sql += "AND d.status = ? "
        args.append(status)
    if q:
        sql += "AND (d.doc_no LIKE ? OR p.name LIKE ?) "
        args += [f"%{q}%"] * 2
    sql += "ORDER BY d.id DESC LIMIT 200"
    return conn.execute(sql, args).fetchall()


def list_gate_passes(conn: sqlite3.Connection, status: str | None = None, q: str | None = None):
    sql = ("SELECT gp.*, d.doc_no AS dispatch_no, d.id AS dispatch_id, p.name AS customer_name, "
           "(SELECT COALESCE(SUM(qty),0) FROM dispatch_lines WHERE dispatch_id=d.id) AS total_qty "
           "FROM gate_passes gp JOIN dispatches d ON d.id=gp.dispatch_id JOIN parties p ON p.id=d.customer_id WHERE 1=1 ")
    args: list = []
    if status:
        sql += "AND gp.status = ? "
        args.append(status)
    if q:
        sql += "AND (gp.pass_no LIKE ? OR gp.vehicle_no LIKE ? OR d.doc_no LIKE ? OR gp.driver_name LIKE ?) "
        args += [f"%{q}%"] * 4
    sql += "ORDER BY gp.id DESC LIMIT 200"
    return conn.execute(sql, args).fetchall()


def in_flight_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM dispatches WHERE status IN ('approved','pass_issued')").fetchone()["c"]


# ---------------------------------------------------------------- draft
def create_draft(conn: sqlite3.Connection, user, customer_id, line_inputs: list[tuple[str, str]], notes: str = "") -> int:
    """line_inputs: list of (lot_no, qty_text); blank rows are ignored."""
    require(user, "dispatch.create")
    errors: dict[str, str] = {}
    customer = None
    try:
        customer = conn.execute("SELECT * FROM parties WHERE id=? AND kind='customer'", (int(customer_id or 0),)).fetchone()
    except ValueError:
        pass
    if customer is None:
        errors["customer_id"] = "Choose a customer."
    parsed: list[tuple[sqlite3.Row, int]] = []
    seen: set[int] = set()
    rows = [(a.strip(), b.strip()) for a, b in line_inputs if a.strip() or b.strip()]
    if not rows:
        errors["lines"] = "Add at least one lot and quantity."
    for idx, (lot_no, qty_text) in enumerate(rows, start=1):
        key = f"line{idx}"
        lot = conn.execute("SELECT * FROM lots WHERE lot_no=?", (lot_no,)).fetchone()
        if lot is None:
            errors[key] = f"Line {idx}: lot '{lot_no}' not found."
            continue
        if lot["kind"] != "finished":
            errors[key] = f"Line {idx}: only finished lots can be dispatched in this release."
            continue
        if lot["id"] in seen:
            errors[key] = f"Line {idx}: lot {lot_no} is listed twice; combine the quantities."
            continue
        try:
            qty = parse_qty(qty_text, field=f"Line {idx} quantity")
        except QuantityError as e:
            errors[key] = str(e)
            continue
        parsed.append((lot, qty))
        seen.add(lot["id"])
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        prepared = []
        for lot, qty in parsed:
            loc = _best_finished_location(conn, lot["id"])
            avail = inventory.available(conn, lot["id"], loc["id"]) if loc else 0
            if loc is None or qty > avail:
                raise ValidationFailed({"lines": f"Lot {lot['lot_no']}: only {fmt_qty(avail)} available in the Finished Store; "
                                                 f"requested {fmt_qty(qty)}."})
            prepared.append((lot, loc, qty))
        doc_no = next_number(conn, "DSP", "DSP")
        cur = conn.execute("INSERT INTO dispatches(doc_no, customer_id, status, notes, created_by, created_at) "
                           "VALUES (?,?, 'draft', ?, ?, ?)",
                           (doc_no, customer["id"], _clean(notes) or None, user["id"], now_iso()))
        did = cur.lastrowid
        for lot, loc, qty in prepared:
            conn.execute("INSERT INTO dispatch_lines(dispatch_id, lot_id, location_id, qty) VALUES (?,?,?,?)",
                         (did, lot["id"], loc["id"], qty))
        audit.record(conn, user, "dispatch.create_draft", "dispatch", doc_no,
                     detail={"customer": customer["name"], "lines": [f"{l['lot_no']}={fmt_qty(q)}" for l, _, q in prepared]})
        return did


def _best_finished_location(conn, lot_id: int):
    best = None
    for loc in conn.execute("SELECT * FROM locations WHERE kind='finished_store' ORDER BY id"):
        a = inventory.available(conn, lot_id, loc["id"])
        if best is None or a > best[1]:
            best = (loc, a)
    return best[0] if best else None


# ---------------------------------------------------------------- stock deduction
def _deduct(conn, user, d, event: str) -> bool:
    """Write the ledger deduction once. Returns True if it wrote, False if already deducted."""
    if d["deducted_at"]:
        return False
    for ln in conn.execute("SELECT * FROM dispatch_lines WHERE dispatch_id=? ORDER BY id", (d["id"],)).fetchall():
        inventory.post_line(conn, lot_id=ln["lot_id"], location_id=ln["location_id"], qty=-ln["qty"],
                            movement="dispatch_out", source_type="dispatch", source_id=d["id"],
                            line_key=f"line{ln['id']}", actor_id=user["id"], note=f"{d['doc_no']} @ {event}")
    now = now_iso()
    conn.execute("UPDATE stock_reservations SET status='consumed', closed_at=? WHERE dispatch_id=? AND status='active'",
                 (now, d["id"]))
    conn.execute("UPDATE dispatches SET deducted_at=?, deducted_on=? WHERE id=?", (now, event, d["id"]))
    return True


def _fresh(conn, dispatch_id: int):
    d = conn.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
    if d is None:
        raise NotFound("Dispatch not found.")
    return d


# ---------------------------------------------------------------- lifecycle
def approve(conn: sqlite3.Connection, user, dispatch_id: int) -> str:
    require(user, "dispatch.approve")
    with transaction(conn):
        d = _fresh(conn, dispatch_id)
        if d["status"] != "draft":
            raise BusinessRuleViolation(f"Dispatch {d['doc_no']} is {d['status']}; only drafts can be approved.")
        if require_separate_approver(conn) and d["created_by"] == user["id"]:
            raise BusinessRuleViolation("The person who created a dispatch cannot approve it (separate approver required).")
        for ln in conn.execute("SELECT * FROM dispatch_lines WHERE dispatch_id=?", (d["id"],)).fetchall():
            avail = inventory.available(conn, ln["lot_id"], ln["location_id"])  # re-read inside the lock
            if ln["qty"] > avail:
                lot = conn.execute("SELECT lot_no FROM lots WHERE id=?", (ln["lot_id"],)).fetchone()
                raise BusinessRuleViolation(
                    f"Lot {lot['lot_no']}: only {fmt_qty(avail)} is available now (others may be reserved); needs {fmt_qty(ln['qty'])}.")
            conn.execute("INSERT INTO stock_reservations(dispatch_id, lot_id, location_id, qty, status, created_at) "
                         "VALUES (?,?,?,?, 'active', ?)", (d["id"], ln["lot_id"], ln["location_id"], ln["qty"], now_iso()))
        conn.execute("UPDATE dispatches SET status='approved', approved_by=?, approved_at=? WHERE id=?",
                     (user["id"], now_iso(), d["id"]))
        d = _fresh(conn, dispatch_id)
        deducted = False
        if deduction_event(conn) == "dispatch_approval":
            deducted = _deduct(conn, user, d, "dispatch_approval")
        audit.record(conn, user, "dispatch.approve", "dispatch", d["doc_no"], detail={"stock_deducted_now": deducted})
        return d["doc_no"]


def issue_gate_pass(conn: sqlite3.Connection, user, dispatch_id: int, vehicle_no: str, driver_name: str) -> str:
    require(user, "gate.pass.create")
    vehicle_no, driver_name = _clean(vehicle_no).upper(), _clean(driver_name)
    errors = {}
    if len(vehicle_no) < 3:
        errors["vehicle_no"] = "Vehicle registration is required."
    if len(driver_name) < 2:
        errors["driver_name"] = "Driver name is required."
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        d = _fresh(conn, dispatch_id)
        if d["status"] != "approved":
            raise BusinessRuleViolation(
                f"Dispatch {d['doc_no']} is {d['status']}; a gate pass can only be issued for an approved dispatch.")
        pass_no = next_number(conn, "GP", "GP")
        try:
            conn.execute("INSERT INTO gate_passes(pass_no, dispatch_id, status, vehicle_no, driver_name, issued_by, issued_at) "
                         "VALUES (?,?, 'issued', ?, ?, ?, ?)", (pass_no, d["id"], vehicle_no, driver_name, user["id"], now_iso()))
        except sqlite3.IntegrityError:
            raise BusinessRuleViolation("A gate pass already exists for this dispatch.") from None
        conn.execute("UPDATE dispatches SET status='pass_issued' WHERE id=?", (d["id"],))
        d = _fresh(conn, dispatch_id)
        deducted = False
        if deduction_event(conn) == "gate_pass_issue":
            deducted = _deduct(conn, user, d, "gate_pass_issue")
        audit.record(conn, user, "gate.issue_pass", "gate_pass", pass_no,
                     detail={"dispatch": d["doc_no"], "vehicle": vehicle_no, "stock_deducted_now": deducted})
        return pass_no


def confirm_exit(conn: sqlite3.Connection, user, pass_id: int, notes: str = "") -> str:
    require(user, "gate.exit.confirm")
    with transaction(conn):
        gp = conn.execute("SELECT * FROM gate_passes WHERE id=?", (pass_id,)).fetchone()
        if gp is None:
            raise NotFound("Gate pass not found.")
        if gp["status"] != "issued":
            raise BusinessRuleViolation(f"Gate pass {gp['pass_no']} is {gp['status']}; exit already recorded or pass is void.")
        d = _fresh(conn, gp["dispatch_id"])
        if d["status"] != "pass_issued":
            raise BusinessRuleViolation(f"Dispatch {d['doc_no']} is {d['status']}; cannot confirm exit.")
        deducted = _deduct(conn, user, d, "exit_confirm")  # no-op if an earlier event already deducted
        now = now_iso()
        conn.execute("UPDATE gate_passes SET status='exited', exited_by=?, exited_at=?, exit_notes=? WHERE id=?",
                     (user["id"], now, _clean(notes) or None, gp["id"]))
        conn.execute("UPDATE dispatches SET status='exited' WHERE id=?", (d["id"],))
        audit.record(conn, user, "gate.confirm_exit", "gate_pass", gp["pass_no"],
                     detail={"dispatch": d["doc_no"], "vehicle": gp["vehicle_no"], "stock_deducted_now": deducted})
        return gp["pass_no"]


def cancel(conn: sqlite3.Connection, user, dispatch_id: int, reason: str) -> None:
    require(user, "dispatch.cancel")
    reason = _clean(reason)
    if len(reason) < 5:
        raise ValidationFailed({"reason": "A reason of at least 5 characters is required."})
    with transaction(conn):
        d = _fresh(conn, dispatch_id)
        if d["status"] not in ("draft", "approved", "pass_issued"):
            raise BusinessRuleViolation(f"Dispatch {d['doc_no']} is {d['status']} and cannot be cancelled.")
        reversed_stock = False
        if d["deducted_at"]:
            for row in conn.execute("SELECT * FROM stock_ledger WHERE source_type='dispatch' AND source_id=? AND qty<0",
                                    (d["id"],)).fetchall():
                inventory.post_line(conn, lot_id=row["lot_id"], location_id=row["location_id"], qty=-row["qty"],
                                    movement="dispatch_reversal", source_type="dispatch", source_id=d["id"],
                                    line_key=f"rev-{row['line_key']}", actor_id=user["id"], reverses_id=row["id"], note=reason)
            reversed_stock = True
        now = now_iso()
        conn.execute("UPDATE stock_reservations SET status='released', closed_at=? WHERE dispatch_id=? AND status='active'",
                     (now, d["id"]))
        conn.execute("UPDATE gate_passes SET status='void' WHERE dispatch_id=? AND status='issued'", (d["id"],))
        conn.execute("UPDATE dispatches SET status='cancelled', cancelled_by=?, cancelled_at=?, cancel_reason=? WHERE id=?",
                     (user["id"], now, reason, d["id"]))
        audit.record(conn, user, "dispatch.cancel", "dispatch", d["doc_no"],
                     detail={"reason": reason, "stock_reversed": reversed_stock})
