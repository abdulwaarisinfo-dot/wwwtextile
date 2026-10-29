"""Dashboard metrics. Every number is computed here from authoritative records;
each carries a plain-language definition and a link to the records behind it."""
from __future__ import annotations

import sqlite3

from ..permissions import can
from . import accessories, dispatch, finance, gate_entries, inventory, multihead, processing, watchtower


def metrics(conn: sqlite3.Connection, user) -> dict:
    role = user["role"]
    out: dict = {"stock": [], "counts": [], "empty": False}
    if can(role, "stock.view"):
        for r in inventory.totals_by_location_kind(conn):
            out["stock"].append({"label": r["name"], "kind": r["kind"], "qty": r["on_hand"],
                                 "url": f"/fabrics?kind={r['kind']}",
                                 "definition": "Sum of all posted ledger rows at this location."})
    if can(role, "processing.view"):
        open_jobs = processing.list_jobs(conn, status="open")
        overdue = [j for j in open_jobs if j["overdue"]]
        exc = processing.reconciliation_exceptions(conn)
        out["counts"] += [
            {"label": "Open processing jobs", "value": len(open_jobs), "url": "/processing?status=open",
             "definition": "Jobs with status 'open' (no result posted for all issued lots yet)."},
            {"label": "Overdue jobs", "value": len(overdue), "url": "/processing?overdue=1", "warn": bool(overdue),
             "definition": "Open jobs whose expected date is before today."},
            {"label": "Reconciliation exceptions", "value": len(exc), "url": "/processing?view=exceptions", "warn": bool(exc),
             "definition": "Posted results with a non-zero unexplained variance. No tolerance is applied."},
        ]
    if can(role, "dispatch.view"):
        c = lambda s: conn.execute("SELECT COUNT(*) AS c FROM dispatches WHERE status=?", (s,)).fetchone()["c"]
        out["counts"] += [
            {"label": "Dispatches awaiting approval", "value": c("draft"), "url": "/dispatch?status=draft",
             "definition": "Dispatch drafts not yet approved (no stock reserved)."},
            {"label": "Approved, awaiting gate pass", "value": c("approved"), "url": "/dispatch?status=approved",
             "definition": "Approved dispatches; stock is reserved."},
            {"label": "Gate passes awaiting exit", "value": c("pass_issued"), "url": "/gate?status=issued",
             "definition": "Gate passes issued but vehicle exit not yet confirmed."},
        ]
    if can(role, "receipt.create") or can(role, "stock.view"):
        n = conn.execute("SELECT COUNT(*) AS c FROM goods_receipts WHERE status='draft'").fetchone()["c"]
        out["counts"].append({"label": "Draft goods receipts", "value": n, "url": "/fabrics/receipts?status=draft",
                              "definition": "Receipts saved but not posted; they do not affect stock."})
    if can(role, "alert.view"):
        c = watchtower.counts(conn)
        out["counts"].append({"label": "Active alerts", "value": sum(c.values()), "url": "/watchtower", "warn": bool(c.get("critical")),
                              "definition": f"{c.get('critical', 0)} critical, {c.get('warning', 0)} warning, {c.get('info', 0)} info — rule-based checks."})
    if can(role, "acc.view"):
        out["counts"].append({"label": "Accessories at/below reorder level", "value": len(accessories.low_stock(conn)), "url": "/accessories?low=1",
                              "warn": bool(accessories.low_stock(conn)), "definition": "Items whose total stock is at or below their reorder level (level 0 = no alert)."})
    if can(role, "gate.view"):
        inside = len(gate_entries.vehicles_inside(conn))
        wait = conn.execute("SELECT COUNT(*) AS c FROM gate_entries WHERE status='awaiting_approval'").fetchone()["c"]
        out["counts"] += [{"label": "Vehicles inside now", "value": inside, "url": "/gate/entries?status=inside", "definition": "Inward gate entries not yet exited."},
                          {"label": "Gate entries awaiting approval", "value": wait, "url": "/gate/entries?status=awaiting_approval", "warn": bool(wait),
                           "definition": "Outward entries that cannot leave until approved."}]
    if can(role, "mh.view"):
        orders = multihead.list_orders(conn, status="open")
        out["counts"].append({"label": "Open Multihead orders", "value": len(orders), "url": "/multihead?status=open", "warn": any(o["overdue"] for o in orders),
                              "definition": "Orders not yet completed or cancelled; warning if any is past its due date."})
    if can(role, "fin.view"):
        sm = finance.summary(conn)
        cur = ""
        out["finance"] = {"payables": sm["payables"], "receivables": sm["receivables"], "drafts": sm["drafts"], "url": "/finance"}
    out["empty"] = not out["stock"] or all(s["qty"] == 0 for s in out["stock"])
    out["deduction_event"] = dispatch.deduction_event(conn)
    if can(role, "audit.view"):
        out["recent"] = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 8").fetchall()
    return out
