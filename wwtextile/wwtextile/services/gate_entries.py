"""Inward / outward gate register for movements that are not dispatch gate passes.

A gate entry is a PHYSICAL record. It never posts stock and never creates a goods receipt: the physical movement
is deliberately separate from inventory and accounting postings. References to receipts etc. are for tracing only.
"""
from __future__ import annotations

import re
import sqlite3

from .. import audit
from ..db import get_setting, next_number, now_iso, transaction
from ..errors import BusinessRuleViolation, NotFound, ValidationFailed
from ..permissions import require
from ..quantity import QuantityError, fmt_qty, parse_qty
from . import notifications

REF_TYPES = {"goods_receipt": "Goods receipt", "processing_issue": "Processing issue", "acc_doc": "Accessory document", "other": "Other"}
VEHICLE_RE = re.compile(r"^[A-Z0-9][A-Z0-9 -]{2,19}$")
PHONE_RE = re.compile(r"^[0-9+ -]{6,20}$")


def _c(v) -> str:
    return str(v if v is not None else "").strip()


def outward_requires_approval(conn) -> bool:
    return get_setting(conn, "gate.outward_requires_approval", "1") == "1"


def separate_approver(conn) -> bool:
    return get_setting(conn, "gate.require_separate_approver", "1") == "1"


def get(conn, entry_id: int):
    r = conn.execute(
        "SELECT g.*, p.name AS party_name, eu.username AS entered_by_name, au.username AS approved_by_name, xu.username AS exited_by_name "
        "FROM gate_entries g LEFT JOIN parties p ON p.id=g.party_id JOIN users eu ON eu.id=g.entered_by "
        "LEFT JOIN users au ON au.id=g.approved_by LEFT JOIN users xu ON xu.id=g.exited_by WHERE g.id=?", (entry_id,)).fetchone()
    if r is None:
        raise NotFound("Gate entry not found.")
    return r


def ref_url(conn, ref_type, ref_no) -> str | None:
    if not ref_type or not ref_no:
        return None
    if ref_type == "goods_receipt":
        r = conn.execute("SELECT id FROM goods_receipts WHERE doc_no=?", (ref_no,)).fetchone()
        return f"/fabrics/receipts/{r['id']}" if r else None
    if ref_type == "acc_doc":
        r = conn.execute("SELECT id FROM acc_docs WHERE doc_no=?", (ref_no,)).fetchone()
        return f"/accessories/docs/{r['id']}" if r else None
    if ref_type == "processing_issue":
        r = conn.execute("SELECT job_id FROM processing_issues WHERE issue_no=?", (ref_no,)).fetchone()
        return f"/processing/jobs/{r['job_id']}" if r else None
    return None


def _validate_ref(conn, ref_type, ref_no, errors) -> None:
    if not ref_type and not ref_no:
        return
    if ref_type not in REF_TYPES:
        errors["ref_type"] = "Choose a reference type."
        return
    if not ref_no:
        errors["ref_no"] = "Reference number is required when a type is chosen."
        return
    q = {"goods_receipt": "SELECT 1 FROM goods_receipts WHERE doc_no=?", "processing_issue": "SELECT 1 FROM processing_issues WHERE issue_no=?",
         "acc_doc": "SELECT 1 FROM acc_docs WHERE doc_no=?"}.get(ref_type)
    if q and conn.execute(q, (ref_no,)).fetchone() is None:
        errors["ref_no"] = f"{REF_TYPES[ref_type]} '{ref_no}' does not exist."
    if ref_type == "other" and len(ref_no) > 80:
        errors["ref_no"] = "Reference max 80 characters."


def create(conn: sqlite3.Connection, user, form: dict) -> int:
    require(user, "gate.entry.create")
    errors: dict[str, str] = {}
    direction = _c(form.get("direction"))
    if direction not in ("inward", "outward"):
        errors["direction"] = "Choose inward or outward."
    vehicle = " ".join(_c(form.get("vehicle_no")).upper().split())
    if not VEHICLE_RE.match(vehicle):
        errors["vehicle_no"] = "Vehicle registration is required (3-20 letters/digits)."
    driver = " ".join(_c(form.get("driver_name")).split())
    if not (2 <= len(driver) <= 60):
        errors["driver_name"] = "Driver name is required (2-60 characters)."
    phone = _c(form.get("driver_phone"))
    if phone and not PHONE_RE.match(phone):
        errors["driver_phone"] = "Phone may contain digits, + and spaces only."
    party_id = int(_c(form.get("party_id"))) if _c(form.get("party_id")).isdigit() else None
    if party_id and not conn.execute("SELECT 1 FROM parties WHERE id=?", (party_id,)).fetchone():
        errors["party_id"] = "Unknown party."
    party_text = " ".join(_c(form.get("party_text")).split())[:80]
    if not party_id and not party_text:
        errors["party_id"] = "Choose a party or type a name."
    material = " ".join(_c(form.get("material")).split())
    if not (2 <= len(material) <= 200):
        errors["material"] = "Describe the material (2-200 characters)."
    qty, unit = None, _c(form.get("declared_unit"))[:10]
    if _c(form.get("declared_qty")):
        try:
            qty = parse_qty(form.get("declared_qty"), field="Declared quantity")
        except QuantityError as e:
            errors["declared_qty"] = str(e)
        if not unit:
            errors["declared_unit"] = "Unit is required with a declared quantity."
    ref_type, ref_no = _c(form.get("ref_type")), _c(form.get("ref_no"))
    _validate_ref(conn, ref_type, ref_no, errors)
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        if direction == "inward":
            inside = conn.execute("SELECT entry_no FROM gate_entries WHERE vehicle_no=? AND status='inside'", (vehicle,)).fetchone()
            if inside:
                raise BusinessRuleViolation(f"Vehicle {vehicle} is already recorded inside ({inside['entry_no']}). Record its exit first.")
            status = "inside"
        else:
            status = "awaiting_approval" if outward_requires_approval(conn) else "pending_exit"
        no = next_number(conn, "GATE-" + direction, "GIN" if direction == "inward" else "GOUT")
        cur = conn.execute(
            "INSERT INTO gate_entries(entry_no,direction,vehicle_no,driver_name,driver_phone,party_id,party_text,material,declared_qty,declared_unit,"
            "ref_type,ref_no,notes,status,entered_by,entered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (no, direction, vehicle, driver, phone or None, party_id, party_text or None, material, qty, unit or None,
             ref_type or None, ref_no or None, _c(form.get("notes")) or None, status, user["id"], now_iso()))
        audit.record(conn, user, "gate.entry_create", "gate_entry", no, detail={"direction": direction, "vehicle": vehicle, "status": status})
        if status == "awaiting_approval":
            notifications.notify_permission(conn, "gate.entry.approve", title=f"Outward gate entry {no} needs approval",
                                            body=f"{vehicle} · {material[:80]}", url=f"/gate/entries/{cur.lastrowid}",
                                            dedupe_key=f"gate-approve-{cur.lastrowid}")
        return cur.lastrowid


def approve(conn, user, entry_id: int) -> str:
    require(user, "gate.entry.approve")
    with transaction(conn):
        g = conn.execute("SELECT * FROM gate_entries WHERE id=?", (entry_id,)).fetchone()
        if g is None:
            raise NotFound("Gate entry not found.")
        if g["status"] != "awaiting_approval":
            raise BusinessRuleViolation(f"{g['entry_no']} is {g['status']}; only entries awaiting approval can be approved.")
        if separate_approver(conn) and g["entered_by"] == user["id"]:
            raise BusinessRuleViolation("The person who created this entry cannot approve it (separate approver is required).")
        conn.execute("UPDATE gate_entries SET status='pending_exit', approved_by=?, approved_at=? WHERE id=?", (user["id"], now_iso(), entry_id))
        audit.record(conn, user, "gate.entry_approve", "gate_entry", g["entry_no"])
        return g["entry_no"]


def confirm_exit(conn, user, entry_id: int, notes: str = "") -> str:
    require(user, "gate.entry.exit")
    with transaction(conn):
        g = conn.execute("SELECT * FROM gate_entries WHERE id=?", (entry_id,)).fetchone()
        if g is None:
            raise NotFound("Gate entry not found.")
        ok = (g["direction"] == "inward" and g["status"] == "inside") or (g["direction"] == "outward" and g["status"] == "pending_exit")
        if not ok:
            raise BusinessRuleViolation(
                f"{g['entry_no']} is '{g['status'].replace('_', ' ')}'. "
                + ("An outward entry must be approved before the vehicle can exit." if g["status"] == "awaiting_approval" else "It cannot be exited from this state."))
        conn.execute("UPDATE gate_entries SET status='exited', exited_by=?, exited_at=?, exit_notes=? WHERE id=?",
                     (user["id"], now_iso(), _c(notes)[:300] or None, entry_id))
        audit.record(conn, user, "gate.entry_exit", "gate_entry", g["entry_no"], detail={"vehicle": g["vehicle_no"]})
        return g["entry_no"]


def cancel(conn, user, entry_id: int, reason: str) -> None:
    require(user, "gate.entry.cancel")
    reason = _c(reason)
    if len(reason) < 5:
        raise ValidationFailed({"reason": "A reason of at least 5 characters is required."})
    with transaction(conn):
        g = conn.execute("SELECT * FROM gate_entries WHERE id=?", (entry_id,)).fetchone()
        if g is None:
            raise NotFound("Gate entry not found.")
        if g["status"] in ("exited", "cancelled"):
            raise BusinessRuleViolation(f"{g['entry_no']} is already {g['status']}.")
        conn.execute("UPDATE gate_entries SET status='cancelled', cancelled_by=?, cancelled_at=?, cancel_reason=? WHERE id=?",
                     (user["id"], now_iso(), reason, entry_id))
        audit.record(conn, user, "gate.entry_cancel", "gate_entry", g["entry_no"], detail={"reason": reason})


def list_entries(conn, direction=None, status=None, q=None, limit=200):
    sql = ("SELECT g.*, p.name AS party_name FROM gate_entries g LEFT JOIN parties p ON p.id=g.party_id WHERE 1=1 ")
    a: list = []
    if direction:
        sql += "AND g.direction=? "; a.append(direction)
    if status:
        sql += "AND g.status=? "; a.append(status)
    if q:
        sql += "AND (g.entry_no LIKE ? OR g.vehicle_no LIKE ? OR g.driver_name LIKE ? OR g.material LIKE ? OR g.ref_no LIKE ? OR p.name LIKE ? OR g.party_text LIKE ?) "
        a += [f"%{q}%"] * 7
    return conn.execute(sql + "ORDER BY g.id DESC LIMIT ?", a + [limit]).fetchall()


def register(conn, *, ts_from=None, ts_to=None, direction=None, status=None, q=None, limit=500):
    """Unified gate register: gate entries + dispatch gate passes (outward). ts_from/ts_to are UTC ISO bounds [from, to) on the event that opened the row."""
    rows = []
    for g in conn.execute("SELECT g.*, p.name AS party_name FROM gate_entries g LEFT JOIN parties p ON p.id=g.party_id"):
        rows.append({"kind": "entry", "no": g["entry_no"], "direction": g["direction"], "vehicle": g["vehicle_no"], "driver": g["driver_name"],
                     "party": g["party_name"] or g["party_text"] or "", "material": g["material"],
                     "qty": (f"{fmt_qty(g['declared_qty'])} {g['declared_unit']}" if g["declared_qty"] else ""),
                     "ref": (g["ref_no"] or ""), "status": g["status"], "in_at": g["entered_at"], "out_at": g["exited_at"], "url": f"/gate/entries/{g['id']}"})
    for gp in conn.execute("SELECT gp.*, d.doc_no, p.name AS cust FROM gate_passes gp JOIN dispatches d ON d.id=gp.dispatch_id "
                           "JOIN parties p ON p.id=d.customer_id"):
        rows.append({"kind": "dispatch_pass", "no": gp["pass_no"], "direction": "outward", "vehicle": gp["vehicle_no"], "driver": gp["driver_name"],
                     "party": gp["cust"], "material": "Finished fabric (dispatch)", "qty": "", "ref": gp["doc_no"], "status": gp["status"],
                     "in_at": gp["issued_at"], "out_at": gp["exited_at"], "url": f"/dispatch/{gp['dispatch_id']}"})
    def keep(r):
        d = r["in_at"] or ""
        if ts_from and d < ts_from: return False
        if ts_to and d >= ts_to: return False
        if direction and r["direction"] != direction: return False
        if status and r["status"] != status: return False
        if q:
            hay = " ".join(str(r[k]) for k in ("no", "vehicle", "driver", "party", "material", "ref")).lower()
            if q.lower() not in hay: return False
        return True
    rows = [r for r in rows if keep(r)]
    rows.sort(key=lambda r: r["in_at"], reverse=True)
    return rows[:limit]


def vehicles_inside(conn):
    return conn.execute("SELECT * FROM gate_entries WHERE status='inside' ORDER BY entered_at").fetchall()
