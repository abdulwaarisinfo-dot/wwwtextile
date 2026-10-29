"""Reports. Every report is ONE function that returns columns + rows; the HTML table, the print layout and the CSV
export all render that same result, so on-screen and exported figures cannot disagree. Totals are computed from the rows.

Quantities of different units are never added together: totals are grouped by the unit column.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timezone

from ..errors import PermissionDenied, ValidationFailed
from ..money import fmt_money
from ..permissions import can
from ..quantity import fmt_qty
from . import accessories, finance, gate_entries, inventory, multihead

MAX_ROWS = 5000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _d(v) -> str | None:
    v = (v or "").strip()
    if not v:
        return None
    if not DATE_RE.match(v):
        raise ValidationFailed({"date": f"'{v}' is not a valid date (use YYYY-MM-DD)."})
    try:
        date.fromisoformat(v)
    except ValueError:
        raise ValidationFailed({"date": f"'{v}' is not a valid date."}) from None
    return v


def local_day_bounds(day: str, tz) -> tuple[str, str]:
    """[start, end) of a local calendar day as UTC ISO strings (DST-safe, compares lexicographically with stored timestamps)."""
    from datetime import timedelta
    d = date.fromisoformat(day)
    lo = datetime(d.year, d.month, d.day, tzinfo=tz)
    hi = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    f = lambda x: x.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return f(lo), f(hi)


def _rng(sql: str, args: list, col: str, f: dict) -> str:
    """Append a from/to filter on a UTC timestamp column, interpreting the dates in the display time zone."""
    tz = f.get("_tz") or timezone.utc
    d1, d2 = _d(f.get("date_from")), _d(f.get("date_to"))
    if d1:
        sql += f"AND {col} >= ? "; args.append(local_day_bounds(d1, tz)[0])
    if d2:
        sql += f"AND {col} < ? "; args.append(local_day_bounds(d2, tz)[1])
    return sql


def _like(v):
    return f"%{(v or '').strip()}%"


def _pct2(num: int, den: int) -> int | None:
    """basis points (1/100 of a percent), integer maths."""
    return None if not den else (num * 10000) // den


# ============================================================ individual reports
def r_stock_by_location(conn, user, f):
    rows = []
    for r in inventory.stock_summary(conn, f.get("kind") or None, f.get("q") or None):
        rows.append({"lot": r["lot_no"], "item": r["item_name"], "location": r["loc_name"], "on_hand": r["on_hand"],
                     "reserved": r["reserved"], "available": r["available"], "unit": r["unit"], "_url": f"/fabrics/lots/{r['lot_no']}"})
    return dict(columns=[("lot", "Lot", "text"), ("item", "Item", "text"), ("location", "Location", "text"), ("on_hand", "On hand", "qty"),
                         ("reserved", "Reserved", "qty"), ("available", "Available", "qty"), ("unit", "Unit", "text")],
                rows=rows, sum_cols=["on_hand", "reserved", "available"], unit_col="unit")


def r_lot_movements(conn, user, f):
    d1, d2 = _d(f.get("date_from")), _d(f.get("date_to"))
    sql = ("SELECT sl.*, l.lot_no, loc.name AS loc_name, u.username AS actor, i.unit FROM stock_ledger sl JOIN lots l ON l.id=sl.lot_id "
           "JOIN items i ON i.id=l.item_id JOIN locations loc ON loc.id=sl.location_id LEFT JOIN users u ON u.id=sl.actor_id WHERE 1=1 ")
    a: list = []
    if f.get("lot"):
        sql += "AND l.lot_no LIKE ? "; a.append(_like(f["lot"]))
    if f.get("movement"):
        sql += "AND sl.movement=? "; a.append(f["movement"])
    sql = _rng(sql, a, "sl.posted_at", f)
    rows = [{"id": r["id"], "at": r["posted_at"], "lot": r["lot_no"], "location": r["loc_name"], "movement": r["movement"].replace("_", " "),
             "qty": r["qty"], "unit": r["unit"], "source": f"{r['source_type']}#{r['source_id']}", "actor": r["actor"] or "", "_url": f"/fabrics/lots/{r['lot_no']}"}
            for r in conn.execute(sql + "ORDER BY sl.id DESC LIMIT ?", a + [MAX_ROWS + 1])]
    return dict(columns=[("id", "Ledger #", "int"), ("at", "Posted", "datetime"), ("lot", "Lot", "text"), ("location", "Location", "text"),
                         ("movement", "Movement", "text"), ("qty", "Qty (+/-)", "qty"), ("unit", "Unit", "text"), ("source", "Source", "text"), ("actor", "By", "text")],
                rows=rows, sum_cols=["qty"], unit_col="unit", total_label="Net")


def r_goods_receipts(conn, user, f):
    d1, d2 = _d(f.get("date_from")), _d(f.get("date_to"))
    sql = ("SELECT g.*, p.name AS supplier_name, i.name AS item_name, i.unit FROM goods_receipts g JOIN parties p ON p.id=g.supplier_id "
           "JOIN items i ON i.id=g.item_id WHERE 1=1 ")
    a: list = []
    if f.get("supplier_id"):
        sql += "AND g.supplier_id=? "; a.append(f["supplier_id"])
    if f.get("status"):
        sql += "AND g.status=? "; a.append(f["status"])
    if f.get("q"):
        sql += "AND (g.doc_no LIKE ? OR g.lot_no LIKE ? OR g.supplier_ref LIKE ?) "; a += [_like(f["q"])] * 3
    sql = _rng(sql, a, "g.created_at", f)
    rows = [{"doc": r["doc_no"], "date": r["created_at"], "lot": r["lot_no"], "supplier": r["supplier_name"], "item": r["item_name"], "qty": r["qty"],
             "unit": r["unit"], "status": r["status"], "ref": r["supplier_ref"] or "", "_url": f"/fabrics/receipts/{r['id']}"}
            for r in conn.execute(sql + "ORDER BY g.id DESC LIMIT ?", a + [MAX_ROWS + 1])]
    # only posted receipts count towards received quantity
    for r in rows:
        r["counted"] = r["qty"] if r["status"] == "posted" else 0
    return dict(columns=[("doc", "Receipt", "text"), ("date", "Created", "datetime"), ("lot", "Lot", "text"), ("supplier", "Supplier", "text"), ("item", "Item", "text"),
                         ("qty", "Qty", "qty"), ("unit", "Unit", "text"), ("status", "Status", "text"), ("ref", "Supplier ref", "text"), ("counted", "Posted qty", "qty")],
                rows=rows, sum_cols=["counted"], unit_col="unit", notes=["Only posted receipts are counted in the total (drafts, reversed and discarded receipts are listed but not counted)."])


def r_processing_jobs(conn, user, f):
    d1, d2 = _d(f.get("date_from")), _d(f.get("date_to"))
    sql = ("SELECT j.*, l.lot_no, p.name AS party_name, i.unit, "
           "(SELECT COALESCE(SUM(qty),0) FROM processing_issues WHERE job_id=j.id AND status='posted') AS issued, "
           "(SELECT COALESCE(SUM(accepted),0) FROM processing_results WHERE job_id=j.id AND status='posted') AS accepted "
           "FROM processing_jobs j JOIN lots l ON l.id=j.lot_id JOIN items i ON i.id=l.item_id LEFT JOIN parties p ON p.id=j.party_id WHERE 1=1 ")
    a: list = []
    for col, key in (("j.process_type", "process_type"), ("j.status", "status"), ("j.party_id", "party_id")):
        if f.get(key):
            sql += f"AND {col}=? "; a.append(f[key])
    if f.get("q"):
        sql += "AND (j.job_no LIKE ? OR l.lot_no LIKE ?) "; a += [_like(f["q"])] * 2
    sql = _rng(sql, a, "j.created_at", f)
    today = date.today().isoformat()
    rows = [{"job": r["job_no"], "created": r["created_at"], "lot": r["lot_no"], "type": r["process_type"], "party": r["party_name"] or "", "expected": r["expected_date"] or "",
             "status": r["status"] + (" (overdue)" if r["status"] == "open" and r["expected_date"] and r["expected_date"] < today else ""),
             "issued": r["issued"], "accepted": r["accepted"], "unit": r["unit"], "_url": f"/processing/jobs/{r['id']}"}
            for r in conn.execute(sql + "ORDER BY j.id DESC LIMIT ?", a + [MAX_ROWS + 1])]
    return dict(columns=[("job", "Job", "text"), ("created", "Created", "datetime"), ("lot", "Lot", "text"), ("type", "Process", "text"), ("party", "Processor", "text"),
                         ("expected", "Expected", "text"), ("status", "Status", "text"), ("issued", "Issued", "qty"), ("accepted", "Accepted", "qty"), ("unit", "Unit", "text")],
                rows=rows, sum_cols=["issued", "accepted"], unit_col="unit")


def _results(conn, f, only_exceptions=False):
    d1, d2 = _d(f.get("date_from")), _d(f.get("date_to"))
    sql = ("SELECT pr.*, j.job_no, j.process_type, l.lot_no, pi.issue_no, pi.qty AS issued, i.unit, p.name AS party_name FROM processing_results pr "
           "JOIN processing_jobs j ON j.id=pr.job_id JOIN processing_issues pi ON pi.id=pr.issue_id JOIN lots l ON l.id=j.lot_id "
           "JOIN items i ON i.id=l.item_id LEFT JOIN parties p ON p.id=j.party_id WHERE pr.status='posted' ")
    a: list = []
    if f.get("process_type"):
        sql += "AND j.process_type=? "; a.append(f["process_type"])
    if f.get("lot"):
        sql += "AND l.lot_no LIKE ? "; a.append(_like(f["lot"]))
    if only_exceptions or f.get("exceptions") == "1":
        sql += "AND pr.reconciliation_status='exception' "
    sql = _rng(sql, a, "pr.posted_at", f)
    return conn.execute(sql + "ORDER BY pr.id DESC LIMIT ?", a + [MAX_ROWS + 1]).fetchall()


def r_reconciliation(conn, user, f):
    rows = [{"result": r["result_no"], "posted": r["posted_at"], "job": r["job_no"], "lot": r["lot_no"], "type": r["process_type"], "issued": r["issued"],
             "accepted": r["accepted"], "rejected": r["rejected"], "wastage": r["wastage"], "shrinkage": r["shrinkage"], "remaining": r["remaining"],
             "variance": r["variance"], "recon": r["reconciliation_status"], "unit": r["unit"], "_url": f"/processing/jobs/{r['job_id']}"} for r in _results(conn, f)]
    return dict(columns=[("result", "Result", "text"), ("posted", "Posted", "datetime"), ("job", "Job", "text"), ("lot", "Lot", "text"), ("type", "Process", "text"),
                         ("issued", "Issued", "qty"), ("accepted", "Accepted", "qty"), ("rejected", "Rejected", "qty"), ("wastage", "Wastage", "qty"),
                         ("shrinkage", "Shrinkage", "qty"), ("remaining", "Remaining", "qty"), ("variance", "Unexplained variance", "qty"),
                         ("recon", "Status", "text"), ("unit", "Unit", "text")],
                rows=rows, sum_cols=["issued", "accepted", "rejected", "wastage", "shrinkage", "remaining", "variance"], unit_col="unit",
                notes=["Input = accepted + rejected + wastage + shrinkage + remaining + unexplained variance. No loss tolerance is applied (open business question)."])


def r_wastage_shrinkage(conn, user, f):
    rows = []
    for r in _results(conn, f):
        loss = r["wastage"] + r["shrinkage"]
        rows.append({"result": r["result_no"], "posted": r["posted_at"], "lot": r["lot_no"], "type": r["process_type"], "issued": r["issued"], "wastage": r["wastage"],
                     "shrinkage": r["shrinkage"], "loss": loss, "loss_pct": _pct2(loss, r["issued"]), "unit": r["unit"], "_url": f"/processing/jobs/{r['job_id']}"})
    return dict(columns=[("result", "Result", "text"), ("posted", "Posted", "datetime"), ("lot", "Lot", "text"), ("type", "Process", "text"), ("issued", "Issued", "qty"),
                         ("wastage", "Wastage", "qty"), ("shrinkage", "Shrinkage", "qty"), ("loss", "Measured loss", "qty"), ("loss_pct", "Loss % of issued", "pct2"), ("unit", "Unit", "text")],
                rows=rows, sum_cols=["issued", "wastage", "shrinkage", "loss"], unit_col="unit",
                notes=["Loss % is informational. No tolerance or acceptable-loss rule has been defined by the owner."])


def r_finished_availability(conn, user, f):
    rows = [{"lot": r["lot_no"], "item": r["item_name"], "on_hand": r["on_hand"], "reserved": r["reserved"], "available": r["available"], "unit": r["unit"],
             "_url": f"/fabrics/lots/{r['lot_no']}"} for r in inventory.stock_summary(conn, "finished_store", f.get("q") or None)]
    return dict(columns=[("lot", "Finished lot", "text"), ("item", "Item", "text"), ("on_hand", "On hand", "qty"), ("reserved", "Reserved", "qty"),
                         ("available", "Available", "qty"), ("unit", "Unit", "text")], rows=rows, sum_cols=["on_hand", "reserved", "available"], unit_col="unit")


def r_accessory_stock(conn, user, f):
    rows = []
    for r in accessories.stock_summary(conn, f.get("q") or None, low_only=f.get("low") == "1"):
        it = r["item"]
        rows.append({"code": it["code"], "name": it["name"], "category": it["category"] or "", "on_hand": r["total"], "unit": it["unit"],
                     "reorder": it["reorder_level"], "state": "LOW" if r["low"] else "", "_url": f"/accessories/items/{it['id']}"})
    return dict(columns=[("code", "Code", "text"), ("name", "Item", "text"), ("category", "Category", "text"), ("on_hand", "On hand", "qty"), ("unit", "Unit", "text"),
                         ("reorder", "Reorder level", "qty"), ("state", "Alert", "text")], rows=rows, sum_cols=["on_hand"], unit_col="unit")


def r_accessory_consumption(conn, user, f):
    d1, d2 = _d(f.get("date_from")), _d(f.get("date_to"))
    sql = ("SELECT a.*, d.doc_no, d.ref_type, d.ref_no, d.id AS doc_id, i.code, i.name, i.unit FROM acc_ledger a JOIN acc_docs d ON d.id=a.source_id AND a.source_type='acc_doc' "
           "JOIN acc_items i ON i.id=a.item_id WHERE d.doc_type='issue' ")
    a: list = []
    if f.get("item_id"):
        sql += "AND a.item_id=? "; a.append(f["item_id"])
    if f.get("ref_no"):
        sql += "AND d.ref_no LIKE ? "; a.append(_like(f["ref_no"]))
    sql = _rng(sql, a, "a.posted_at", f)
    rows = [{"at": r["posted_at"], "doc": r["doc_no"], "item": f"{r['code']} · {r['name']}", "consumed": -r["qty"], "unit": r["unit"],
             "ref": f"{(r['ref_type'] or '').replace('_', ' ')}: {r['ref_no'] or ''}", "kind": "reversal" if r["reverses_id"] else "issue", "_url": f"/accessories/docs/{r['doc_id']}"}
            for r in conn.execute(sql + "ORDER BY a.id DESC LIMIT ?", a + [MAX_ROWS + 1])]
    return dict(columns=[("at", "Posted", "datetime"), ("doc", "Document", "text"), ("item", "Item", "text"), ("consumed", "Consumed (net)", "qty"), ("unit", "Unit", "text"),
                         ("ref", "Against", "text"), ("kind", "Kind", "text")], rows=rows, sum_cols=["consumed"], unit_col="unit",
                notes=["Reversals appear as negative consumption, so the total is net of reversed issues."])


def r_dispatch_register(conn, user, f):
    d1, d2 = _d(f.get("date_from")), _d(f.get("date_to"))
    sql = ("SELECT d.*, p.name AS customer, gp.pass_no, gp.vehicle_no, gp.exited_at FROM dispatches d JOIN parties p ON p.id=d.customer_id "
           "LEFT JOIN gate_passes gp ON gp.dispatch_id=d.id WHERE 1=1 ")
    a: list = []
    if f.get("customer_id"):
        sql += "AND d.customer_id=? "; a.append(f["customer_id"])
    if f.get("status"):
        sql += "AND d.status=? "; a.append(f["status"])
    sql = _rng(sql, a, "d.created_at", f)
    rows = []
    for d in conn.execute(sql + "ORDER BY d.id DESC LIMIT ?", a + [MAX_ROWS + 1]):
        for ln in conn.execute("SELECT dl.qty, l.lot_no, i.unit FROM dispatch_lines dl JOIN lots l ON l.id=dl.lot_id JOIN items i ON i.id=l.item_id WHERE dl.dispatch_id=? ORDER BY dl.id", (d["id"],)):
            rows.append({"doc": d["doc_no"], "created": d["created_at"], "customer": d["customer"], "lot": ln["lot_no"], "qty": ln["qty"], "unit": ln["unit"], "status": d["status"],
                         "pass": d["pass_no"] or "", "vehicle": d["vehicle_no"] or "", "exited": d["exited_at"] or "", "_url": f"/dispatch/{d['id']}"})
    counted = [r for r in rows if r["status"] != "cancelled"]
    for r in rows:
        r["counted"] = r["qty"] if r["status"] != "cancelled" else 0
    return dict(columns=[("doc", "Dispatch", "text"), ("created", "Created", "datetime"), ("customer", "Customer", "text"), ("lot", "Lot", "text"), ("qty", "Qty", "qty"), ("unit", "Unit", "text"),
                         ("status", "Status", "text"), ("pass", "Gate pass", "text"), ("vehicle", "Vehicle", "text"), ("exited", "Exited", "datetime"), ("counted", "Counted qty", "qty")],
                rows=rows, sum_cols=["counted"], unit_col="unit", notes=["Cancelled dispatches are listed but not counted."])


def r_gate_register(conn, user, f):
    tz = f.get("_tz") or timezone.utc
    d1, d2 = _d(f.get("date_from")), _d(f.get("date_to"))
    lo = local_day_bounds(d1, tz)[0] if d1 else None
    hi = local_day_bounds(d2, tz)[1] if d2 else None
    rows = [{"no": r["no"], "dir": r["direction"], "in_at": r["in_at"], "out_at": r["out_at"] or "", "vehicle": r["vehicle"], "driver": r["driver"], "party": r["party"],
             "material": r["material"], "qty": r["qty"], "ref": r["ref"], "status": r["status"].replace("_", " "), "_url": r["url"]}
            for r in gate_entries.register(conn, ts_from=lo, ts_to=hi, direction=f.get("direction") or None, status=f.get("status") or None, q=f.get("q") or None, limit=MAX_ROWS + 1)]
    return dict(columns=[("no", "Entry / pass", "text"), ("dir", "Direction", "text"), ("in_at", "In / issued", "datetime"), ("out_at", "Out", "datetime"), ("vehicle", "Vehicle", "text"),
                         ("driver", "Driver", "text"), ("party", "Party", "text"), ("material", "Material", "text"), ("qty", "Declared qty", "text"), ("ref", "Reference", "text"),
                         ("status", "Status", "text")], rows=rows, sum_cols=[], unit_col=None,
                notes=["Gate records are physical movements; they never change stock."])


def r_pending_approvals(conn, user, f):
    role = user["role"]
    rows = []
    now = datetime.now(timezone.utc)
    def age(ts):
        try:
            return int((now - datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)).total_seconds() // 3600)
        except Exception:
            return None
    if can(role, "dispatch.view"):
        for d in conn.execute("SELECT d.id, d.doc_no, d.created_at, p.name FROM dispatches d JOIN parties p ON p.id=d.customer_id WHERE d.status='draft'"):
            rows.append({"what": "Dispatch awaiting approval", "ref": d["doc_no"], "detail": d["name"], "since": d["created_at"], "hours": age(d["created_at"]), "_url": f"/dispatch/{d['id']}"})
        for d in conn.execute("SELECT d.id, d.doc_no, d.approved_at, p.name FROM dispatches d JOIN parties p ON p.id=d.customer_id WHERE d.status='approved'"):
            rows.append({"what": "Approved dispatch awaiting gate pass", "ref": d["doc_no"], "detail": d["name"], "since": d["approved_at"], "hours": age(d["approved_at"]), "_url": f"/dispatch/{d['id']}"})
    if can(role, "gate.view"):
        for g in conn.execute("SELECT id, entry_no, vehicle_no, entered_at FROM gate_entries WHERE status='awaiting_approval'"):
            rows.append({"what": "Outward gate entry awaiting approval", "ref": g["entry_no"], "detail": g["vehicle_no"], "since": g["entered_at"], "hours": age(g["entered_at"]), "_url": f"/gate/entries/{g['id']}"})
    if can(role, "fin.view"):
        for e in conn.execute("SELECT id, entry_no, kind, created_at FROM fin_entries WHERE status='draft'"):
            rows.append({"what": "Finance entry awaiting posting", "ref": e["entry_no"], "detail": e["kind"].replace("_", " "), "since": e["created_at"], "hours": age(e["created_at"]), "_url": f"/finance/entries/{e['id']}"})
    if can(role, "stock.view"):
        for g in conn.execute("SELECT id, doc_no, created_at FROM goods_receipts WHERE status='draft'"):
            rows.append({"what": "Goods receipt draft (not posted)", "ref": g["doc_no"], "detail": "", "since": g["created_at"], "hours": age(g["created_at"]), "_url": f"/fabrics/receipts/{g['id']}"})
    if can(role, "processing.view"):
        for i in conn.execute("SELECT pi.issue_no, pi.created_at, pi.job_id FROM processing_issues pi WHERE pi.status='draft'"):
            rows.append({"what": "Processing issue draft (not posted)", "ref": i["issue_no"], "detail": "", "since": i["created_at"], "hours": age(i["created_at"]), "_url": f"/processing/jobs/{i['job_id']}"})
        for i in conn.execute("SELECT pr.result_no, pr.created_at, pr.job_id FROM processing_results pr WHERE pr.status='draft'"):
            rows.append({"what": "Processing result draft (not posted)", "ref": i["result_no"], "detail": "", "since": i["created_at"], "hours": age(i["created_at"]), "_url": f"/processing/jobs/{i['job_id']}"})
    if can(role, "acc.view"):
        for d in conn.execute("SELECT id, doc_no, doc_type, created_at FROM acc_docs WHERE status='draft'"):
            rows.append({"what": f"Accessory {d['doc_type']} draft (not posted)", "ref": d["doc_no"], "detail": "", "since": d["created_at"], "hours": age(d["created_at"]), "_url": f"/accessories/docs/{d['id']}"})
    rows.sort(key=lambda r: r["since"] or "")
    return dict(columns=[("what", "Waiting for", "text"), ("ref", "Reference", "text"), ("detail", "Detail", "text"), ("since", "Since", "datetime"), ("hours", "Hours waiting", "int")],
                rows=rows, sum_cols=[], unit_col=None, notes=["Only items you are permitted to see are listed."])


def r_financial_summary(conn, user, f):
    d1, d2 = _d(f.get("date_from")), _d(f.get("date_to"))
    s = finance.summary(conn, d1, d2)
    rows = [{"party": b["party"]["name"], "type": b["party"]["kind"].replace("_", " "), "charged": b["charged"], "settled": b["settled"], "balance": b["balance"],
             "dir": b["direction"], "_url": f"/finance/parties/{b['party']['id']}"} for b in s["per_party"]]
    return dict(columns=[("party", "Party", "text"), ("type", "Type", "text"), ("dir", "Balance is", "text"), ("charged", "Billed / invoiced", "money"),
                         ("settled", "Paid / received", "money"), ("balance", "Outstanding", "money")], rows=rows, sum_cols=["charged", "settled", "balance"], unit_col="dir",
                notes=[f"Period activity ({d1 or 'start'} to {d2 or 'now'}): billed {fmt_money(s['billed'])}, invoiced {fmt_money(s['invoiced'])}, paid {fmt_money(s['paid'])}, "
                       f"received {fmt_money(s['received'])}, expenses {fmt_money(s['expenses_total'])}.",
                       "Outstanding balances are as of the end date (or now). Payables and receivables are totalled separately. No tax is computed."], summary=s)


def r_party_statement(conn, user, f):
    if not f.get("party_id"):
        raise ValidationFailed({"party_id": "Choose a party."})
    st = finance.statement(conn, int(f["party_id"]), _d(f.get("date_from")), _d(f.get("date_to")))
    cust = st["party"]["kind"] == "customer"
    rows = [{"date": st["date_from"] or "", "no": "—", "kind": "Opening balance", "ref": "", "debit": None, "credit": None, "balance": st["opening"]}] if st["date_from"] else []
    for r in st["rows"]:
        e = r["entry"]
        rows.append({"date": e["entry_date"], "no": e["entry_no"], "kind": e["kind"].replace("_", " "), "ref": e["reference"] or e["doc_ref_no"] or "",
                     "debit": r["signed"] if r["signed"] > 0 else None, "credit": -r["signed"] if r["signed"] < 0 else None, "balance": r["running"], "_url": f"/finance/entries/{e['id']}"})
    return dict(columns=[("date", "Date", "text"), ("no", "Entry", "text"), ("kind", "Type", "text"), ("ref", "Reference", "text"),
                         ("debit", "Invoiced" if cust else "Billed", "money"), ("credit", "Received" if cust else "Paid", "money"), ("balance", "Running balance", "money")],
                rows=rows, sum_cols=["debit", "credit"], unit_col=None, statement=st,
                notes=[f"Statement for {st['party']['name']}. Closing balance: {fmt_money(st['closing'])} ({'receivable' if cust else 'payable'})."])


def r_audit_history(conn, user, f):
    d1, d2 = _d(f.get("date_from")), _d(f.get("date_to"))
    sql, a = "SELECT * FROM audit_events WHERE 1=1 ", []
    for col, key, op in (("actor_name", "actor", "LIKE"), ("action", "action", "LIKE"), ("outcome", "outcome", "="), ("entity_ref", "ref", "LIKE")):
        if f.get(key):
            sql += f"AND {col} {op} ? "; a.append(_like(f[key]) if op == "LIKE" else f[key])
    sql = _rng(sql, a, "ts", f)
    rows = [{"id": r["id"], "ts": r["ts"], "actor": r["actor_name"], "role": r["actor_role"] or "", "action": r["action"], "ref": r["entity_ref"] or "", "outcome": r["outcome"], "detail": r["detail"] or ""}
            for r in conn.execute(sql + "ORDER BY id DESC LIMIT ?", a + [MAX_ROWS + 1])]
    return dict(columns=[("id", "#", "int"), ("ts", "Time", "datetime"), ("actor", "Actor", "text"), ("role", "Role", "text"), ("action", "Action", "text"), ("ref", "Record", "text"),
                         ("outcome", "Outcome", "text"), ("detail", "Detail", "text")], rows=rows, sum_cols=[], unit_col=None)


def r_multihead_production(conn, user, f):
    tz = f.get("_tz") or timezone.utc
    d1, d2 = _d(f.get("date_from")), _d(f.get("date_to"))
    lo = local_day_bounds(d1, tz)[0] if d1 else None
    hi = local_day_bounds(d2, tz)[1] if d2 else None
    rows = []
    for o in multihead.list_orders(conn, status=f.get("status") or None, q=f.get("q") or None, limit=MAX_ROWS + 1):
        if (lo and o["created_at"] < lo) or (hi and o["created_at"] >= hi):
            continue
        rows.append({"order": o["order_no"], "title": o["title"], "created": o["created_at"], "machine": o["machine_code"] or "", "status": o["status"], "unit": o["unit"],
                     "planned": o["planned_qty"], "good": o["good"], "rejected": o["rejected"], "pct": o["progress_pct"], "fabric": o["fabric_consumed"], "waste": o["fabric_waste"],
                     "_url": f"/multihead/orders/{o['id']}"})
    return dict(columns=[("order", "Order", "text"), ("title", "Title", "text"), ("created", "Created", "datetime"), ("machine", "Machine", "text"), ("status", "Status", "text"),
                         ("unit", "Order unit", "text"), ("planned", "Planned", "qty"), ("good", "Good", "qty"), ("rejected", "Rejected", "qty"), ("pct", "% of plan", "int"),
                         ("fabric", "Fabric consumed*", "qty"), ("waste", "of which waste*", "qty")], rows=rows, sum_cols=["planned", "good", "rejected"], unit_col="unit",
                notes=["*Fabric quantities are in each fabric lot's own unit and may differ from the order unit; they are not included in totals."])


# ============================================================ registry
_DATES = [("date_from", "From", "date"), ("date_to", "To", "date")]
REPORTS: dict[str, dict] = {
    "stock-by-location": dict(title="Stock by item and location", group="Inventory", perm="stock.view", fn=r_stock_by_location,
                              filters=[("q", "Lot / item", "text"), ("kind", "Location type", "select:loc_kinds")], desc="Fabric lots with on-hand, reserved and available quantity."),
    "finished-availability": dict(title="Finished-stock availability", group="Inventory", perm="stock.view", fn=r_finished_availability,
                                  filters=[("q", "Lot / item", "text")], desc="Finished lots in the Finished Store available for dispatch."),
    "lot-movements": dict(title="Lot movement history", group="Inventory", perm="ledger.view", fn=r_lot_movements,
                          filters=[("lot", "Lot", "text"), ("movement", "Movement", "select:movements")] + _DATES, desc="Every stock ledger row, newest first."),
    "goods-receipts": dict(title="Goods receipts", group="Inventory", perm="stock.view", fn=r_goods_receipts,
                           filters=[("q", "Receipt / lot / ref", "text"), ("supplier_id", "Supplier", "select:suppliers"), ("status", "Status", "select:receipt_status")] + _DATES, desc="Fabric receipts and their status."),
    "accessory-stock": dict(title="Accessory stock", group="Inventory", perm="acc.view", fn=r_accessory_stock,
                            filters=[("q", "Code / name / category", "text"), ("low", "Low stock only", "select:yesno")], desc="Accessory on-hand with reorder alerts."),
    "accessory-consumption": dict(title="Accessory consumption", group="Inventory", perm="acc.view", fn=r_accessory_consumption,
                                  filters=[("item_id", "Item", "select:acc_items"), ("ref_no", "Reference", "text")] + _DATES, desc="Accessories issued against jobs, orders and departments."),
    "processing-jobs": dict(title="Processing jobs", group="Processing", perm="processing.view", fn=r_processing_jobs,
                            filters=[("q", "Job / lot", "text"), ("process_type", "Process", "select:process_types"), ("status", "Status", "select:job_status"), ("party_id", "Processor", "select:parties")] + _DATES,
                            desc="Jobs with issued and accepted quantities."),
    "reconciliation": dict(title="Processing reconciliation", group="Processing", perm="processing.view", fn=r_reconciliation,
                           filters=[("lot", "Lot", "text"), ("process_type", "Process", "select:process_types"), ("exceptions", "Exceptions only", "select:yesno")] + _DATES, desc="Posted results with unexplained variance."),
    "wastage-shrinkage": dict(title="Wastage and shrinkage", group="Processing", perm="processing.view", fn=r_wastage_shrinkage,
                              filters=[("lot", "Lot", "text"), ("process_type", "Process", "select:process_types")] + _DATES, desc="Measured losses per processing result."),
    "multihead-production": dict(title="Multihead production", group="Production", perm="mh.view", fn=r_multihead_production,
                                 filters=[("q", "Order / title / operator", "text"), ("status", "Status", "select:mh_status")] + _DATES, desc="Orders with output, rejects and fabric consumption."),
    "dispatch-register": dict(title="Dispatch register", group="Dispatch & gate", perm="dispatch.view", fn=r_dispatch_register,
                              filters=[("customer_id", "Customer", "select:customers"), ("status", "Status", "select:dispatch_status")] + _DATES, desc="Dispatch lines with gate-pass and exit details."),
    "gate-register": dict(title="Gate register", group="Dispatch & gate", perm="gate.view", fn=r_gate_register,
                          filters=[("q", "Search", "text"), ("direction", "Direction", "select:directions")] + _DATES, desc="Inward/outward entries and dispatch gate passes."),
    "pending-approvals": dict(title="Pending approvals and drafts", group="Management", perm="report.view", fn=r_pending_approvals, filters=[], desc="Everything waiting for someone to act."),
    "financial-summary": dict(title="Financial summary", group="Finance", perm="fin.view", fn=r_financial_summary, filters=_DATES, desc="Outstanding balances by party and period activity."),
    "party-statement": dict(title="Party statement", group="Finance", perm="fin.view", fn=r_party_statement,
                            filters=[("party_id", "Party", "select:fin_parties")] + _DATES, desc="Statement of account for one supplier, job-work party or customer."),
    "audit-history": dict(title="Audit history", group="Management", perm="audit.view", fn=r_audit_history,
                          filters=[("actor", "Actor", "text"), ("action", "Action", "text"), ("ref", "Record", "text"), ("outcome", "Outcome", "select:outcomes")] + _DATES, desc="Who did what, when."),
}


def available(user) -> list[tuple[str, dict]]:
    return [(k, v) for k, v in REPORTS.items() if can(user["role"], v["perm"])]


def run(conn, user, key: str, filters: dict, tz=None) -> dict:
    spec = REPORTS.get(key)
    if spec is None:
        from ..errors import NotFound
        raise NotFound("Unknown report.")
    if not can(user["role"], spec["perm"]):
        raise PermissionDenied(f"Your role does not have the '{spec['perm']}' permission needed for this report.")
    allowed = {f[0] for f in spec["filters"]}
    clean = {k: (str(v).strip()) for k, v in filters.items() if k in allowed and str(v).strip()}
    res = spec["fn"](conn, user, {**clean, "_tz": tz or timezone.utc})
    rows = res["rows"]
    truncated = len(rows) > MAX_ROWS
    if truncated:
        rows = rows[:MAX_ROWS]
    res["rows"] = rows
    res.update(key=key, title=spec["title"], filters_applied=clean, filter_spec=spec["filters"], truncated=truncated,
               generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), generated_by=user["username"])
    res["totals"] = compute_totals(res)
    return res


def compute_totals(res: dict) -> list[dict]:
    cols, rows, sum_cols, unit_col = res["columns"], res["rows"], res.get("sum_cols") or [], res.get("unit_col")
    if not sum_cols or not rows:
        return []
    groups: dict[str, dict] = {}
    for r in rows:
        g = str(r.get(unit_col) or "") if unit_col else ""
        t = groups.setdefault(g, {c: 0 for c in sum_cols})
        for c in sum_cols:
            t[c] += r.get(c) or 0
    label = res.get("total_label", "Total")
    out = []
    for g, t in sorted(groups.items()):
        out.append({"label": f"{label} ({g})" if g else label, "values": t})
    return out


def fmt(kind: str, value, tz=None, csv_mode: bool = False) -> str:
    if value is None or value == "":
        return ""
    if kind == "qty":
        return f"{value / 1000:.3f}" if csv_mode else fmt_qty(value)
    if kind == "money":
        return f"{value / 100:.2f}" if csv_mode else fmt_money(value)
    if kind == "pct2":
        return f"{value / 100:.2f}" if csv_mode else f"{value / 100:.2f}%"
    if kind == "datetime":
        if csv_mode:
            return str(value)
        try:
            d = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            return (d.astimezone(tz) if tz else d).strftime("%d %b %Y %H:%M")
        except ValueError:
            return str(value)
    return str(value)


def to_csv(res: dict, filters_text: str) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f"# {res['title']} — generated {res['generated_at']} UTC by {res['generated_by']}"])
    w.writerow([f"# Filters: {filters_text or 'none'} — {len(res['rows'])} row(s)" + (" (TRUNCATED at %d rows)" % MAX_ROWS if res["truncated"] else "")])
    for n in res.get("notes") or []:
        w.writerow([f"# {n}"])
    cols = res["columns"]
    w.writerow([label for _k, label, _t in cols])
    for r in res["rows"]:
        w.writerow([_safe(fmt(kind, r.get(k), csv_mode=True)) for k, _l, kind in cols])
    for t in res["totals"]:
        row = [t["label"]] + [""] * (len(cols) - 1)
        for i, (k, _l, kind) in enumerate(cols):
            if k in t["values"]:
                row[i] = fmt(kind, t["values"][k], csv_mode=True)
        w.writerow(row)
    return out.getvalue()


def _safe(text: str) -> str:
    """Neutralise spreadsheet formula injection for text cells that start with = + - @."""
    if text and text[0] in "=+@" or (text and text[0] == "-" and not re.match(r"^-?\d+(\.\d+)?$", text)):
        return "'" + text
    return text
