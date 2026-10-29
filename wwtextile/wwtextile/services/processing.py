"""Textile processing: job -> issue -> result -> reconciliation.

Reconciliation rule (BUSINESS_RULES.md A7):
    input = accepted + rejected + wastage + shrinkage + remaining + variance
`variance` is always computed here, never entered. Negative variance (outputs
exceed input) is rejected. There is no tolerance: any non-zero variance is an
open 'exception'. Nothing is ever silently forced to balance.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime

from .. import audit
from ..db import next_number, now_iso, transaction
from ..errors import BusinessRuleViolation, NotFound, ValidationFailed
from ..permissions import require
from ..quantity import QuantityError, fmt_qty, parse_qty
from . import inventory

PROCESS_TYPES = ["dyeing", "printing", "embroidery"]  # placeholder list, BUSINESS_RULES.md A13


def _clean(v) -> str:
    return str(v if v is not None else "").strip()


def reconcile(issued: int, accepted: int, rejected: int, wastage: int, shrinkage: int, remaining: int) -> dict:
    accounted = accepted + rejected + wastage + shrinkage + remaining
    variance = issued - accounted
    if variance < 0:
        raise ValidationFailed(
            f"Reported quantities ({fmt_qty(accounted)}) exceed the quantity issued ({fmt_qty(issued)}) "
            f"by {fmt_qty(-variance)}. Correct the figures; over-yield cannot be posted until a rule exists.")
    return {"issued": issued, "accounted": accounted, "variance": variance,
            "status": "balanced" if variance == 0 else "exception"}


# ---------------------------------------------------------------- reads
def get_job(conn: sqlite3.Connection, job_id: int):
    j = conn.execute(
        "SELECT j.*, l.lot_no, i.name AS item_name, i.unit AS unit, p.name AS party_name, u.username AS created_by_name "
        "FROM processing_jobs j JOIN lots l ON l.id=j.lot_id JOIN items i ON i.id=l.item_id "
        "LEFT JOIN parties p ON p.id=j.party_id JOIN users u ON u.id=j.created_by WHERE j.id=?", (job_id,)).fetchone()
    if j is None:
        raise NotFound("Processing job not found.")
    return j


def job_issues(conn: sqlite3.Connection, job_id: int):
    return conn.execute(
        "SELECT pi.*, loc.name AS from_location_name FROM processing_issues pi "
        "JOIN locations loc ON loc.id=pi.from_location_id WHERE pi.job_id=? ORDER BY pi.id", (job_id,)).fetchall()


def job_results(conn: sqlite3.Connection, job_id: int):
    return conn.execute(
        "SELECT pr.*, fl.lot_no AS finished_lot_no, pi.issue_no, pi.qty AS issued_qty "
        "FROM processing_results pr JOIN processing_issues pi ON pi.id=pr.issue_id "
        "LEFT JOIN lots fl ON fl.id=pr.finished_lot_id WHERE pr.job_id=? ORDER BY pr.id", (job_id,)).fetchall()


def list_jobs(conn: sqlite3.Connection, status: str | None = None, overdue_only: bool = False,
              q: str | None = None, today: str | None = None):
    today = today or date.today().isoformat()
    sql = ("SELECT j.*, l.lot_no, i.unit AS unit, p.name AS party_name, "
           "(SELECT COALESCE(SUM(qty),0) FROM processing_issues WHERE job_id=j.id AND status='posted') AS issued_qty, "
           "CASE WHEN j.status='open' AND j.expected_date IS NOT NULL AND j.expected_date < ? THEN 1 ELSE 0 END AS overdue "
           "FROM processing_jobs j JOIN lots l ON l.id=j.lot_id JOIN items i ON i.id=l.item_id "
           "LEFT JOIN parties p ON p.id=j.party_id WHERE 1=1 ")
    args: list = [today]
    if status:
        sql += "AND j.status = ? "
        args.append(status)
    if q:
        sql += "AND (j.job_no LIKE ? OR l.lot_no LIKE ?) "
        args += [f"%{q}%"] * 2
    if overdue_only:
        sql += "AND j.status='open' AND j.expected_date IS NOT NULL AND j.expected_date < ? "
        args.append(today)
    sql += "ORDER BY j.id DESC LIMIT 200"
    return conn.execute(sql, args).fetchall()


def reconciliation_exceptions(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT pr.*, j.job_no, l.lot_no FROM processing_results pr JOIN processing_jobs j ON j.id=pr.job_id "
        "JOIN lots l ON l.id=j.lot_id WHERE pr.status='posted' AND pr.reconciliation_status='exception' "
        "ORDER BY pr.id DESC").fetchall()


# ---------------------------------------------------------------- job
def create_job(conn: sqlite3.Connection, user, form: dict) -> int:
    require(user, "processing.create")
    errors: dict[str, str] = {}
    lot = conn.execute("SELECT * FROM lots WHERE lot_no=?", (_clean(form.get("lot_no")),)).fetchone()
    if lot is None:
        errors["lot_no"] = "Choose an existing lot."
    elif lot["kind"] != "raw":
        errors["lot_no"] = "Only raw lots can be sent for processing."
    ptype = _clean(form.get("process_type")).lower()
    if ptype not in PROCESS_TYPES:
        errors["process_type"] = "Choose a process type."
    party_id = None
    if _clean(form.get("party_id")):
        try:
            party_id = int(form["party_id"])
        except ValueError:
            errors["party_id"] = "Invalid party."
        else:
            if conn.execute("SELECT 1 FROM parties WHERE id=?", (party_id,)).fetchone() is None:
                errors["party_id"] = "Unknown party."
    expected = _clean(form.get("expected_date")) or None
    if expected:
        try:
            datetime.strptime(expected, "%Y-%m-%d")
        except ValueError:
            errors["expected_date"] = "Use the format YYYY-MM-DD."
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        job_no = next_number(conn, "JOB", "JOB")
        cur = conn.execute(
            "INSERT INTO processing_jobs(job_no, lot_id, process_type, party_id, expected_date, notes, status, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,'open',?,?)",
            (job_no, lot["id"], ptype, party_id, expected, _clean(form.get("notes")) or None, user["id"], now_iso()))
        audit.record(conn, user, "processing.create_job", "processing_job", job_no,
                     detail={"lot_no": lot["lot_no"], "process": ptype})
        return cur.lastrowid


def cancel_job(conn: sqlite3.Connection, user, job_id: int) -> None:
    require(user, "processing.create")
    with transaction(conn):
        j = conn.execute("SELECT * FROM processing_jobs WHERE id=?", (job_id,)).fetchone()
        if j is None:
            raise NotFound("Processing job not found.")
        if j["status"] != "open":
            raise BusinessRuleViolation(f"Job {j['job_no']} is {j['status']} and cannot be cancelled.")
        if conn.execute("SELECT 1 FROM processing_issues WHERE job_id=? AND status IN ('posted','draft')", (job_id,)).fetchone():
            raise BusinessRuleViolation("Discard draft issues first; a job with posted issues cannot be cancelled.")
        conn.execute("UPDATE processing_jobs SET status='cancelled' WHERE id=?", (job_id,))
        audit.record(conn, user, "processing.cancel_job", "processing_job", j["job_no"])


# ---------------------------------------------------------------- issue
def _raw_location_with_stock(conn, lot_id: int, wanted: int | None):
    if wanted:
        loc = conn.execute("SELECT * FROM locations WHERE id=? AND kind='raw_store'", (wanted,)).fetchone()
        if loc is None:
            raise ValidationFailed({"from_location_id": "Choose a raw store location."})
        return loc
    best = None
    for loc in conn.execute("SELECT * FROM locations WHERE kind='raw_store' ORDER BY id"):
        a = inventory.available(conn, lot_id, loc["id"])
        if best is None or a > best[1]:
            best = (loc, a)
    if best is None:
        raise BusinessRuleViolation("No raw store location is configured.")
    return best[0]


def create_issue_draft(conn: sqlite3.Connection, user, job_id: int, form: dict) -> int:
    require(user, "processing.issue")
    job = get_job(conn, job_id)
    if job["status"] != "open":
        raise BusinessRuleViolation(f"Job {job['job_no']} is {job['status']}; cannot prepare an issue.")
    try:
        qty = parse_qty(form.get("qty"), field="Quantity to issue")
    except QuantityError as e:
        raise ValidationFailed({"qty": str(e)}) from None
    unit = _clean(form.get("unit")) or job["unit"]
    if unit.lower() != job["unit"].lower():
        raise ValidationFailed({"unit": f"This lot is in '{job['unit']}'; no conversion to '{unit}' is configured."})
    wanted = int(form["from_location_id"]) if _clean(form.get("from_location_id")).isdigit() else None
    with transaction(conn):
        loc = _raw_location_with_stock(conn, job["lot_id"], wanted)
        avail = inventory.available(conn, job["lot_id"], loc["id"])
        if qty > avail:
            raise ValidationFailed({"qty": f"Only {fmt_qty(avail)} {job['unit']} of lot {job['lot_no']} is available "
                                           f"at {loc['name']}; cannot prepare an issue for {fmt_qty(qty)}."})
        issue_no = next_number(conn, "ISS", "ISS")
        cur = conn.execute(
            "INSERT INTO processing_issues(issue_no, job_id, qty, from_location_id, status, created_by, created_at) "
            "VALUES (?,?,?,?, 'draft', ?, ?)", (issue_no, job_id, qty, loc["id"], user["id"], now_iso()))
        audit.record(conn, user, "processing.create_issue_draft", "processing_issue", issue_no,
                     detail={"job": job["job_no"], "qty": fmt_qty(qty)})
        return cur.lastrowid


def post_issue(conn: sqlite3.Connection, user, issue_id: int) -> str:
    require(user, "processing.issue")
    with transaction(conn):
        iss = conn.execute("SELECT * FROM processing_issues WHERE id=?", (issue_id,)).fetchone()
        if iss is None:
            raise NotFound("Issue not found.")
        if iss["status"] != "draft":
            raise BusinessRuleViolation(f"Issue {iss['issue_no']} is {iss['status']}; only drafts can be posted.")
        job = conn.execute("SELECT * FROM processing_jobs WHERE id=?", (iss["job_id"],)).fetchone()
        if job["status"] != "open":
            raise BusinessRuleViolation(f"Job {job['job_no']} is {job['status']}.")
        wip = conn.execute("SELECT id FROM locations WHERE kind='wip' ORDER BY id LIMIT 1").fetchone()
        if wip is None:
            raise BusinessRuleViolation("No processing (WIP) location is configured.")
        avail = inventory.available(conn, job["lot_id"], iss["from_location_id"])  # re-read now, inside the lock
        if iss["qty"] > avail:
            raise BusinessRuleViolation(
                f"Only {fmt_qty(avail)} is available now; issue {iss['issue_no']} needs {fmt_qty(iss['qty'])}.")
        inventory.post_line(conn, lot_id=job["lot_id"], location_id=iss["from_location_id"], qty=-iss["qty"],
                            movement="process_issue_out", source_type="processing_issue", source_id=iss["id"],
                            line_key="out", actor_id=user["id"], note=iss["issue_no"])
        inventory.post_line(conn, lot_id=job["lot_id"], location_id=wip["id"], qty=iss["qty"],
                            movement="process_issue_in", source_type="processing_issue", source_id=iss["id"],
                            line_key="in", actor_id=user["id"], note=iss["issue_no"])
        conn.execute("UPDATE processing_issues SET status='posted', posted_by=?, posted_at=? WHERE id=?",
                     (user["id"], now_iso(), iss["id"]))
        audit.record(conn, user, "processing.post_issue", "processing_issue", iss["issue_no"],
                     detail={"job": job["job_no"], "qty": fmt_qty(iss["qty"])})
        return iss["issue_no"]


def discard_issue(conn: sqlite3.Connection, user, issue_id: int) -> None:
    require(user, "processing.issue")
    with transaction(conn):
        iss = conn.execute("SELECT * FROM processing_issues WHERE id=?", (issue_id,)).fetchone()
        if iss is None:
            raise NotFound("Issue not found.")
        if iss["status"] != "draft":
            raise BusinessRuleViolation("Only draft issues can be discarded.")
        conn.execute("UPDATE processing_issues SET status='discarded' WHERE id=?", (issue_id,))
        audit.record(conn, user, "processing.discard_issue", "processing_issue", iss["issue_no"])


# ---------------------------------------------------------------- result
def _parse_result_form(form: dict) -> dict:
    out, errors = {}, {}
    for key, label in (("accepted", "Accepted"), ("rejected", "Rejected"), ("wastage", "Wastage"),
                       ("shrinkage", "Shrinkage"), ("remaining", "Remaining in process")):
        raw = form.get(key)
        if raw is None or not str(raw).strip():
            raw = "0"
        try:
            out[key] = parse_qty(str(raw), allow_zero=True, field=label)
        except QuantityError as e:
            errors[key] = str(e)
    if errors:
        raise ValidationFailed(errors)
    return out


def create_result_draft(conn: sqlite3.Connection, user, issue_id: int, form: dict) -> int:
    require(user, "processing.result")
    q = _parse_result_form(form)
    with transaction(conn):
        iss = conn.execute("SELECT * FROM processing_issues WHERE id=?", (issue_id,)).fetchone()
        if iss is None:
            raise NotFound("Issue not found.")
        if iss["status"] != "posted":
            raise BusinessRuleViolation("A result can only be recorded against a posted issue.")
        if conn.execute("SELECT 1 FROM processing_results WHERE issue_id=? AND status IN ('draft','posted')", (issue_id,)).fetchone():
            raise BusinessRuleViolation("This issue already has a result (one result per issue).")
        rec = reconcile(iss["qty"], **q)  # raises ValidationFailed if outputs exceed input
        result_no = next_number(conn, "RES", "RES")
        cur = conn.execute(
            "INSERT INTO processing_results(result_no, issue_id, job_id, accepted, rejected, wastage, shrinkage, remaining, "
            "variance, reconciliation_status, notes, status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)",
            (result_no, issue_id, iss["job_id"], q["accepted"], q["rejected"], q["wastage"], q["shrinkage"], q["remaining"],
             rec["variance"], rec["status"], _clean(form.get("notes")) or None, user["id"], now_iso()))
        audit.record(conn, user, "processing.create_result_draft", "processing_result", result_no,
                     detail={"issue": iss["issue_no"], "variance": fmt_qty(rec["variance"])})
        return cur.lastrowid


def post_result(conn: sqlite3.Connection, user, result_id: int) -> str:
    require(user, "processing.result")
    with transaction(conn):
        r = conn.execute("SELECT * FROM processing_results WHERE id=?", (result_id,)).fetchone()
        if r is None:
            raise NotFound("Result not found.")
        if r["status"] != "draft":
            raise BusinessRuleViolation(f"Result {r['result_no']} is {r['status']}; only drafts can be posted.")
        iss = conn.execute("SELECT * FROM processing_issues WHERE id=?", (r["issue_id"],)).fetchone()
        job = conn.execute("SELECT * FROM processing_jobs WHERE id=?", (r["job_id"],)).fetchone()
        rec = reconcile(iss["qty"], r["accepted"], r["rejected"], r["wastage"], r["shrinkage"], r["remaining"])
        wip = conn.execute("SELECT id FROM locations WHERE kind='wip' ORDER BY id LIMIT 1").fetchone()
        fin = conn.execute("SELECT id FROM locations WHERE kind='finished_store' ORDER BY id LIMIT 1").fetchone()
        rej = conn.execute("SELECT id FROM locations WHERE kind='reject_store' ORDER BY id LIMIT 1").fetchone()
        if not (wip and fin and rej):
            raise BusinessRuleViolation("WIP, Finished Store and Reject Store locations must be configured.")
        lot_id = job["lot_id"]
        leaving_wip = r["accepted"] + r["rejected"] + r["wastage"] + r["shrinkage"]
        in_wip = inventory.on_hand(conn, lot_id, wip["id"])
        if leaving_wip > in_wip:
            raise BusinessRuleViolation(
                f"Only {fmt_qty(in_wip)} of this lot is in processing; the result needs {fmt_qty(leaving_wip)} to leave it.")
        finished_lot_id = None
        common = dict(source_type="processing_result", source_id=r["id"], actor_id=user["id"], note=r["result_no"])
        if r["accepted"] > 0:
            src = conn.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
            n = conn.execute("SELECT COUNT(*) AS c FROM lots WHERE parent_lot_id=?", (lot_id,)).fetchone()["c"] + 1
            fin_no = f"{src['lot_no']}-F{n}"
            cur = conn.execute(
                "INSERT INTO lots(lot_no, item_id, supplier_id, parent_lot_id, kind, created_at, created_by, is_demo) "
                "VALUES (?,?,?,?,'finished',?,?,?)",
                (fin_no, src["item_id"], src["supplier_id"], lot_id, now_iso(), user["id"], src["is_demo"]))
            finished_lot_id = cur.lastrowid
            inventory.post_line(conn, lot_id=lot_id, location_id=wip["id"], qty=-r["accepted"],
                                movement="process_accepted_out", line_key="acc_out", **common)
            inventory.post_line(conn, lot_id=finished_lot_id, location_id=fin["id"], qty=r["accepted"],
                                movement="process_accepted_in", line_key="acc_in", **common)
        if r["rejected"] > 0:
            inventory.post_line(conn, lot_id=lot_id, location_id=wip["id"], qty=-r["rejected"],
                                movement="process_rejected_out", line_key="rej_out", **common)
            inventory.post_line(conn, lot_id=lot_id, location_id=rej["id"], qty=r["rejected"],
                                movement="process_rejected_in", line_key="rej_in", **common)
        if r["wastage"] > 0:
            inventory.post_line(conn, lot_id=lot_id, location_id=wip["id"], qty=-r["wastage"],
                                movement="process_wastage", line_key="waste", **common)
        if r["shrinkage"] > 0:
            inventory.post_line(conn, lot_id=lot_id, location_id=wip["id"], qty=-r["shrinkage"],
                                movement="process_shrinkage", line_key="shrink", **common)
        # remaining and variance deliberately stay in WIP; variance is flagged, not written off.
        conn.execute(
            "UPDATE processing_results SET status='posted', variance=?, reconciliation_status=?, finished_lot_id=?, "
            "posted_by=?, posted_at=? WHERE id=?",
            (rec["variance"], rec["status"], finished_lot_id, user["id"], now_iso(), r["id"]))
        open_issues = conn.execute(
            "SELECT COUNT(*) AS c FROM processing_issues pi WHERE pi.job_id=? AND pi.status='posted' AND NOT EXISTS "
            "(SELECT 1 FROM processing_results pr WHERE pr.issue_id=pi.id AND pr.status='posted')", (job["id"],)).fetchone()["c"]
        if open_issues == 0:
            conn.execute("UPDATE processing_jobs SET status='result_posted' WHERE id=?", (job["id"],))
        audit.record(conn, user, "processing.post_result", "processing_result", r["result_no"],
                     detail={"job": job["job_no"], "accepted": fmt_qty(r["accepted"]), "variance": fmt_qty(rec["variance"]),
                             "status": rec["status"]})
        return r["result_no"]


def discard_result(conn: sqlite3.Connection, user, result_id: int) -> None:
    require(user, "processing.result")
    with transaction(conn):
        r = conn.execute("SELECT * FROM processing_results WHERE id=?", (result_id,)).fetchone()
        if r is None:
            raise NotFound("Result not found.")
        if r["status"] != "draft":
            raise BusinessRuleViolation("Only draft results can be discarded.")
        conn.execute("UPDATE processing_results SET status='discarded' WHERE id=?", (result_id,))
        audit.record(conn, user, "processing.discard_result", "processing_result", r["result_no"])
