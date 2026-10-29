"""AI Operations Watchtower — deterministic exception detection.

Every alert comes from an explicit rule over authoritative records (or an owner-adjustable threshold setting).
No statistical / ML anomaly detection is implemented, and none is claimed: with this little history it would be false precision.
AI (when configured) may explain or prioritise these verified alerts; it never creates one.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from .. import audit
from ..db import now_iso, transaction
from ..errors import BusinessRuleViolation, NotFound, ValidationFailed
from ..permissions import require
from ..quantity import fmt_qty
from . import accessories, inventory, notifications, processing, settings

RULES = {
    "overdue_job": "Open processing job past its expected date",
    "recon_exception": "Posted result with unexplained variance",
    "ledger_integrity": "Negative or inconsistent stock state",
    "dispatch_inconsistent": "Dispatch / gate / stock state mismatch",
    "reservation_exceeds_stock": "Reservations exceed stock on hand",
    "approval_pending": "Approval or posting waiting too long",
    "pass_not_exited": "Gate pass issued but vehicle not confirmed out",
    "vehicle_inside": "Vehicle inside the premises for a long time",
    "low_accessory": "Accessory at or below reorder level",
    "long_running_job": "Processing job open for a long time",
    "draft_aging": "Draft document left unposted",
    "mh_overdue": "Multihead order past its due date",
}
SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _hours_since(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        return (datetime.now(timezone.utc) - datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)).total_seconds() / 3600
    except ValueError:
        return 0.0


def _f(rule, fp, sev, title, detail, refs, rec):
    return dict(rule=rule, fingerprint=fp, severity=sev, title=title, detail=detail, refs=refs, recommendation=rec)


def detect(conn: sqlite3.Connection) -> list[dict]:
    """Pure detection: returns findings; writes nothing."""
    out: list[dict] = []
    today = date.today().isoformat()
    h_pending = settings.get_int(conn, "watchtower.pending_approval_hours")
    d_long = settings.get_int(conn, "watchtower.long_running_job_days")
    h_vehicle = settings.get_int(conn, "watchtower.vehicle_inside_hours")
    d_draft = settings.get_int(conn, "watchtower.draft_age_days")

    open_jobs = conn.execute(
        "SELECT j.*, l.lot_no FROM processing_jobs j JOIN lots l ON l.id=j.lot_id WHERE j.status='open'").fetchall()
    for j in open_jobs:
        if j["expected_date"] and j["expected_date"] < today:
            out.append(_f("overdue_job", f"overdue_job:{j['job_no']}:{j['expected_date']}", "warning", f"{j['job_no']} is overdue",
                          f"{j['process_type'].capitalize()} job for lot {j['lot_no']} was expected by {j['expected_date']} and is still open.",
                          [{"label": j["job_no"], "url": f"/processing/jobs/{j['id']}"}, {"label": f"Lot {j['lot_no']}", "url": f"/fabrics/lots/{j['lot_no']}"}],
                          "Contact the processor, confirm the new expected date, or record the result if it is already back."))
    for r in conn.execute(
            "SELECT pr.*, j.job_no, j.process_type, l.lot_no, pi.qty AS issued FROM processing_results pr JOIN processing_jobs j ON j.id=pr.job_id "
            "JOIN processing_issues pi ON pi.id=pr.issue_id JOIN lots l ON l.id=j.lot_id "
            "WHERE pr.status='posted' AND pr.reconciliation_status='exception' ORDER BY pr.id"):
        out.append(_f("recon_exception", f"recon:{r['result_no']}", "warning", f"{r['result_no']} has unexplained variance {fmt_qty(r['variance'])}",
                      f"Lot {r['lot_no']} {r['process_type']}: issued {fmt_qty(r['issued'])}; the reported outputs and measured losses leave {fmt_qty(r['variance'])} unexplained. "
                      "It stays in processing; no tolerance rule is applied.",
                      [{"label": r["result_no"], "url": f"/processing/jobs/{r['job_id']}"}, {"label": f"Lot {r['lot_no']}", "url": f"/fabrics/lots/{r['lot_no']}"}],
                      "Re-measure the lot or record the missing quantity (remaining, rejected, wastage, shrinkage). Define a resolution rule if this recurs."))
    for p in inventory.reconcile_ledger(conn):
        out.append(_f("ledger_integrity", f"ledger:{p}", "critical", "Fabric ledger inconsistency", p, [{"label": "Stock ledger", "url": "/ledger"}],
                      "Stop posting for the affected lot and investigate; this should be impossible and indicates data corruption or a bug."))
    for r in conn.execute("SELECT i.code, l.code AS lc, SUM(a.qty) AS q FROM acc_ledger a JOIN acc_items i ON i.id=a.item_id JOIN acc_locations l ON l.id=a.location_id "
                          "GROUP BY a.item_id, a.location_id HAVING SUM(a.qty) < 0"):
        out.append(_f("ledger_integrity", f"accledger:{r['code']}:{r['lc']}", "critical", f"Negative accessory balance {r['code']}", f"{r['code']} at {r['lc']} is {fmt_qty(r['q'])}.",
                      [{"label": "Accessory ledger", "url": "/accessories/ledger"}], "Investigate immediately."))
    for r in conn.execute("SELECT d.id, d.doc_no FROM dispatches d WHERE d.status='exited' AND d.deducted_at IS NULL"):
        out.append(_f("dispatch_inconsistent", f"disp_nodeduct:{r['doc_no']}", "critical", f"{r['doc_no']} exited without a stock deduction",
                      "The vehicle is recorded as exited but stock was never deducted.", [{"label": r["doc_no"], "url": f"/dispatch/{r['id']}"}], "Investigate; stock may be overstated."))
    for r in conn.execute("SELECT d.id, d.doc_no, d.status FROM dispatches d JOIN gate_passes g ON g.dispatch_id=d.id WHERE g.status='exited' AND d.status <> 'exited'"):
        out.append(_f("dispatch_inconsistent", f"disp_pass_mismatch:{r['doc_no']}", "critical", f"{r['doc_no']} gate pass exited but dispatch is {r['status']}", "Gate and dispatch states disagree.",
                      [{"label": r["doc_no"], "url": f"/dispatch/{r['id']}"}], "Investigate."))
    for r in conn.execute("SELECT DISTINCT d.id, d.doc_no, d.status FROM stock_reservations s JOIN dispatches d ON d.id=s.dispatch_id WHERE s.status='active' AND d.status IN ('cancelled','exited','draft')"):
        out.append(_f("dispatch_inconsistent", f"stale_reservation:{r['doc_no']}", "critical", f"{r['doc_no']} holds reservations while {r['status']}",
                      "An active reservation exists on a dispatch that should not hold stock.", [{"label": r["doc_no"], "url": f"/dispatch/{r['id']}"}], "Investigate; available stock is understated."))
    for r in conn.execute("SELECT l.lot_no, loc.name, s.lot_id, s.location_id, SUM(s.qty) AS res FROM stock_reservations s JOIN lots l ON l.id=s.lot_id "
                          "JOIN locations loc ON loc.id=s.location_id WHERE s.status='active' GROUP BY s.lot_id, s.location_id"):
        if r["res"] > inventory.on_hand(conn, r["lot_id"], r["location_id"]):
            out.append(_f("reservation_exceeds_stock", f"resv_over:{r['lot_no']}:{r['location_id']}", "critical", f"Reservations on {r['lot_no']} exceed stock",
                          f"Reserved {fmt_qty(r['res'])} but on hand is {fmt_qty(inventory.on_hand(conn, r['lot_id'], r['location_id']))} at {r['name']}.",
                          [{"label": f"Lot {r['lot_no']}", "url": f"/fabrics/lots/{r['lot_no']}"}], "Investigate; a dispatch cannot be fulfilled."))

    def pending(rule_tag, rows, title_fmt, ts_col, url_fmt, what):
        for r in rows:
            if _hours_since(r[ts_col]) >= h_pending:
                out.append(_f("approval_pending", f"{rule_tag}:{r['no']}", "warning", title_fmt.format(**r), f"{what} for {_hours_since(r[ts_col]):.0f} hours (threshold {h_pending} h, an adjustable assumption).",
                              [{"label": r["no"], "url": url_fmt.format(id=r["id"])}], "Review and act, or cancel it."))
    pending("pend_disp", conn.execute("SELECT id, doc_no AS no, created_at ts FROM dispatches WHERE status='draft'"), "Dispatch {no} awaiting approval", "ts", "/dispatch/{id}", "Waiting for approval")
    pending("pend_pass", conn.execute("SELECT id, doc_no AS no, approved_at ts FROM dispatches WHERE status='approved'"), "Dispatch {no} approved, no gate pass", "ts", "/dispatch/{id}", "Approved but no gate pass issued")
    pending("pend_gate", conn.execute("SELECT id, entry_no AS no, entered_at ts FROM gate_entries WHERE status='awaiting_approval'"), "Gate entry {no} awaiting approval", "ts", "/gate/entries/{id}", "Waiting for approval")
    pending("pend_fin", conn.execute("SELECT id, entry_no AS no, created_at ts FROM fin_entries WHERE status='draft'"), "Finance entry {no} awaiting posting", "ts", "/finance/entries/{id}", "Waiting to be posted")
    for r in conn.execute("SELECT g.id, g.pass_no, g.vehicle_no, g.issued_at, g.dispatch_id FROM gate_passes g WHERE g.status='issued'"):
        if _hours_since(r["issued_at"]) >= h_pending:
            out.append(_f("pass_not_exited", f"pass_wait:{r['pass_no']}", "warning", f"Gate pass {r['pass_no']} issued but vehicle not confirmed out",
                          f"Vehicle {r['vehicle_no']} has been on an issued pass for {_hours_since(r['issued_at']):.0f} hours.", [{"label": r["pass_no"], "url": f"/dispatch/{r['dispatch_id']}"}],
                          "Confirm exit at the gate, or cancel the dispatch if the load did not leave."))
    for g in conn.execute("SELECT id, entry_no, vehicle_no, entered_at FROM gate_entries WHERE status='inside'"):
        if _hours_since(g["entered_at"]) >= h_vehicle:
            out.append(_f("vehicle_inside", f"inside:{g['entry_no']}", "info", f"Vehicle {g['vehicle_no']} inside for {_hours_since(g['entered_at']):.0f} h",
                          f"Entry {g['entry_no']} has no recorded exit (threshold {h_vehicle} h, an adjustable assumption).", [{"label": g["entry_no"], "url": f"/gate/entries/{g['id']}"}],
                          "Confirm whether the vehicle has left and record the exit."))
    for r in accessories.low_stock(conn):
        it = r["item"]
        out.append(_f("low_accessory", f"low_acc:{it['code']}", "warning", f"{it['code']} at or below reorder level",
                      f"{it['name']}: {fmt_qty(r['total'])} {it['unit']} on hand; reorder level {fmt_qty(it['reorder_level'])}.", [{"label": it["code"], "url": f"/accessories/items/{it['id']}"}],
                      "Raise a purchase / follow up with the supplier."))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=d_long)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for j in open_jobs:
        overdue = bool(j["expected_date"] and j["expected_date"] < today)
        if j["created_at"] < cutoff and not overdue:
            out.append(_f("long_running_job", f"long_job:{j['job_no']}", "info", f"{j['job_no']} open for more than {d_long} days",
                          f"Created {j['created_at'][:10]}; no result posted yet (threshold is an adjustable assumption, not a confirmed standard).",
                          [{"label": j["job_no"], "url": f"/processing/jobs/{j['id']}"}], "Check progress with the processor."))
    dcut = (datetime.now(timezone.utc) - timedelta(days=d_draft)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for table, col, label, url in (("goods_receipts", "doc_no", "Goods receipt", "/fabrics/receipts/{id}"), ("acc_docs", "doc_no", "Accessory document", "/accessories/docs/{id}")):
        for r in conn.execute(f"SELECT id, {col} AS no FROM {table} WHERE status='draft' AND created_at < ?", (dcut,)):
            out.append(_f("draft_aging", f"draft:{table}:{r['no']}", "info", f"{label} {r['no']} still a draft", f"Unposted for more than {d_draft} days; it has not affected stock.",
                          [{"label": r["no"], "url": url.format(id=r["id"])}], "Post or discard it."))
    for o in conn.execute("SELECT id, order_no, due_date FROM mh_orders WHERE status='open' AND due_date IS NOT NULL AND due_date < ?", (today,)):
        out.append(_f("mh_overdue", f"mh_overdue:{o['order_no']}:{o['due_date']}", "warning", f"{o['order_no']} is past its due date", f"Due {o['due_date']} and still open.",
                      [{"label": o["order_no"], "url": f"/multihead/orders/{o['id']}"}], "Check progress; update the plan or close the order."))
    out.sort(key=lambda a: (SEV_ORDER[a["severity"]], a["fingerprint"]))
    return out


def _event(conn, alert_id, actor_id, action, note=None):
    conn.execute("INSERT INTO alert_events(alert_id, ts, actor_id, action, note) VALUES (?,?,?,?,?)", (alert_id, now_iso(), actor_id, action, note))


def scan(conn: sqlite3.Connection, actor=None) -> dict:
    """Run detection and reconcile with stored alerts. Safe to call repeatedly."""
    findings = detect(conn)
    seen = {f["fingerprint"] for f in findings}
    stats = {"new": 0, "reopened": 0, "auto_resolved": 0, "open_total": 0}
    ts = now_iso()
    actor_id = actor["id"] if actor else None
    with transaction(conn):
        existing = {r["fingerprint"]: r for r in conn.execute("SELECT * FROM alerts")}
        for f in findings:
            row = existing.get(f["fingerprint"])
            refs = json.dumps(f["refs"])
            if row is None:
                cur = conn.execute("INSERT INTO alerts(fingerprint,rule,severity,title,detail,refs,recommendation,detected_at,last_seen_at,status) VALUES (?,?,?,?,?,?,?,?,?,'open')",
                                   (f["fingerprint"], f["rule"], f["severity"], f["title"], f["detail"], refs, f["recommendation"], ts, ts))
                _event(conn, cur.lastrowid, actor_id, "detected")
                stats["new"] += 1
                if f["severity"] in ("critical", "warning"):
                    notifications.notify_permission(conn, "alert.view", title=f["title"], body=f["detail"][:200], url=f"/watchtower#alert-{cur.lastrowid}",
                                                    dedupe_key=f"alert-{f['fingerprint']}")
            else:
                conn.execute("UPDATE alerts SET last_seen_at=?, detail=?, refs=?, severity=?, title=? WHERE id=?", (ts, f["detail"], refs, f["severity"], f["title"], row["id"]))
                if row["status"] == "resolved" and row["auto_resolved"]:
                    conn.execute("UPDATE alerts SET status='open', resolved_at=NULL, resolved_by=NULL, resolution_note=NULL, auto_resolved=0 WHERE id=?", (row["id"],))
                    _event(conn, row["id"], actor_id, "reopened", "Condition detected again")
                    stats["reopened"] += 1
        for fp, row in existing.items():
            if fp not in seen and row["status"] in ("open", "acknowledged"):
                conn.execute("UPDATE alerts SET status='resolved', auto_resolved=1, resolved_at=?, resolution_note='Condition no longer detected' WHERE id=?", (ts, row["id"]))
                _event(conn, row["id"], None, "auto_resolved", "Condition no longer detected")
                stats["auto_resolved"] += 1
        stats["open_total"] = conn.execute("SELECT COUNT(*) AS c FROM alerts WHERE status IN ('open','acknowledged')").fetchone()["c"]
        audit.record(conn, actor, "watchtower.scan", "watchtower", "scan", detail=stats)
    return stats


def list_alerts(conn, status="active", severity=None, rule=None):
    sql, a = "SELECT a.*, u.username AS assignee FROM alerts a LEFT JOIN users u ON u.id=a.assigned_to WHERE 1=1 ", []
    if status == "active":
        sql += "AND a.status IN ('open','acknowledged') "
    elif status:
        sql += "AND a.status=? "; a.append(status)
    if severity:
        sql += "AND a.severity=? "; a.append(severity)
    if rule:
        sql += "AND a.rule=? "; a.append(rule)
    rows = conn.execute(sql, a).fetchall()
    rows = [{**dict(r), "refs": json.loads(r["refs"] or "[]")} for r in rows]
    rows.sort(key=lambda r: (SEV_ORDER[r["severity"]], r["detected_at"]))
    return rows


def get_alert(conn, alert_id: int):
    r = conn.execute("SELECT a.*, u.username AS assignee FROM alerts a LEFT JOIN users u ON u.id=a.assigned_to WHERE a.id=?", (alert_id,)).fetchone()
    if r is None:
        raise NotFound("Alert not found.")
    return {**dict(r), "refs": json.loads(r["refs"] or "[]")}


def alert_history(conn, alert_id: int):
    return conn.execute("SELECT e.*, u.username AS actor FROM alert_events e LEFT JOIN users u ON u.id=e.actor_id WHERE e.alert_id=? ORDER BY e.id", (alert_id,)).fetchall()


def counts(conn) -> dict:
    out = {"critical": 0, "warning": 0, "info": 0}
    for r in conn.execute("SELECT severity, COUNT(*) AS c FROM alerts WHERE status IN ('open','acknowledged') GROUP BY severity"):
        out[r["severity"]] = r["c"]
    return out


def _act(conn, user, alert_id: int, action: str, new_status: str, note: str, *, need_note: bool, from_states: tuple, assign_to: int | None = None):
    require(user, "alert.manage")
    note = (note or "").strip()
    if need_note and len(note) < 5:
        raise ValidationFailed({"note": "A note of at least 5 characters is required."})
    with transaction(conn):
        a = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if a is None:
            raise NotFound("Alert not found.")
        if a["status"] not in from_states:
            raise BusinessRuleViolation(f"This alert is {a['status']}; '{action}' is not possible.")
        if new_status in ("resolved", "dismissed"):
            conn.execute("UPDATE alerts SET status=?, resolved_by=?, resolved_at=?, resolution_note=?, auto_resolved=0 WHERE id=?", (new_status, user["id"], now_iso(), note, alert_id))
        elif new_status == "open" and action == "reopen":
            conn.execute("UPDATE alerts SET status='open', resolved_by=NULL, resolved_at=NULL, resolution_note=NULL WHERE id=?", (alert_id,))
        else:
            conn.execute("UPDATE alerts SET status=? WHERE id=?", (new_status, alert_id))
        if assign_to is not None:
            conn.execute("UPDATE alerts SET assigned_to=? WHERE id=?", (assign_to, alert_id))
        _event(conn, alert_id, user["id"], action, note or None)
        audit.record(conn, user, f"watchtower.{action}", "alert", a["fingerprint"], detail={"note": note} if note else None)


def acknowledge(conn, user, alert_id, note=""):
    _act(conn, user, alert_id, "acknowledge", "acknowledged", note, need_note=False, from_states=("open",))


def resolve(conn, user, alert_id, note):
    _act(conn, user, alert_id, "resolve", "resolved", note, need_note=True, from_states=("open", "acknowledged"))


def dismiss(conn, user, alert_id, note):
    _act(conn, user, alert_id, "dismiss", "dismissed", note, need_note=True, from_states=("open", "acknowledged"))


def reopen(conn, user, alert_id, note=""):
    _act(conn, user, alert_id, "reopen", "open", note, need_note=False, from_states=("resolved", "dismissed"))


def assign(conn, user, alert_id, assignee_id: int, note=""):
    require(user, "alert.manage")
    u = conn.execute("SELECT id, username FROM users WHERE id=? AND active=1", (assignee_id,)).fetchone()
    if u is None:
        raise ValidationFailed({"assignee": "Choose an active user."})
    with transaction(conn):
        a = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if a is None:
            raise NotFound("Alert not found.")
        if a["status"] not in ("open", "acknowledged"):
            raise BusinessRuleViolation("Only open or acknowledged alerts can be assigned.")
        conn.execute("UPDATE alerts SET assigned_to=? WHERE id=?", (assignee_id, alert_id))
        _event(conn, alert_id, user["id"], "assign", f"to {u['username']}")
        audit.record(conn, user, "watchtower.assign", "alert", a["fingerprint"], detail={"to": u["username"]})
        notifications.notify(conn, user_id=assignee_id, title=f"Alert assigned to you: {a['title']}", url=f"/watchtower#alert-{alert_id}")


def digest(conn) -> dict:
    """Deterministic summary of active alerts (used on the dashboard and by the assistant)."""
    c = counts(conn)
    top = list_alerts(conn)[:5]
    return {"counts": c, "total": sum(c.values()), "top": top}
