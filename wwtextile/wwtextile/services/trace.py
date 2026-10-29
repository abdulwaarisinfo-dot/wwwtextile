"""Lot traceability: the whole lifecycle of a lot (and its parent/children) from the ledger."""
from __future__ import annotations

import sqlite3

from ..errors import NotFound
from . import inventory


def doc_ref(conn: sqlite3.Connection, source_type: str, source_id: int):
    """Resolve a ledger source to (document number, url)."""
    if source_type == "goods_receipt":
        r = conn.execute("SELECT doc_no FROM goods_receipts WHERE id=?", (source_id,)).fetchone()
        return (r["doc_no"], f"/fabrics/receipts/{source_id}") if r else (f"{source_type}#{source_id}", None)
    if source_type == "processing_issue":
        r = conn.execute("SELECT issue_no, job_id FROM processing_issues WHERE id=?", (source_id,)).fetchone()
        return (r["issue_no"], f"/processing/jobs/{r['job_id']}") if r else (f"{source_type}#{source_id}", None)
    if source_type == "processing_result":
        r = conn.execute("SELECT result_no, job_id FROM processing_results WHERE id=?", (source_id,)).fetchone()
        return (r["result_no"], f"/processing/jobs/{r['job_id']}") if r else (f"{source_type}#{source_id}", None)
    if source_type == "dispatch":
        r = conn.execute("SELECT doc_no FROM dispatches WHERE id=?", (source_id,)).fetchone()
        return (r["doc_no"], f"/dispatch/{source_id}") if r else (f"{source_type}#{source_id}", None)
    if source_type == "mh_consumption":
        r = conn.execute("SELECT o.order_no, o.id FROM mh_consumptions c JOIN mh_orders o ON o.id=c.order_id WHERE c.id=?", (source_id,)).fetchone()
        return (r["order_no"], f"/multihead/orders/{r['id']}") if r else (f"{source_type}#{source_id}", None)
    return (f"{source_type}#{source_id}", None)


def family_ids(conn: sqlite3.Connection, lot_id: int) -> list[int]:
    """Root lot plus every descendant."""
    root = lot_id
    while True:
        p = conn.execute("SELECT parent_lot_id FROM lots WHERE id=?", (root,)).fetchone()
        if p is None or p["parent_lot_id"] is None:
            break
        root = p["parent_lot_id"]
    ids, frontier = [root], [root]
    while frontier:
        kids = [r["id"] for r in conn.execute(
            f"SELECT id FROM lots WHERE parent_lot_id IN ({','.join('?' * len(frontier))})", frontier)]
        ids += kids
        frontier = kids
    return ids


MOVEMENT_LABELS = {
    "receipt": "Goods receipt", "receipt_reversal": "Receipt reversal",
    "process_issue_out": "Issued to processing (out of store)", "process_issue_in": "Received into processing",
    "process_accepted_out": "Accepted output (out of processing)", "process_accepted_in": "Accepted output (into finished stock)",
    "process_rejected_out": "Rejected (out of processing)", "process_rejected_in": "Rejected (into reject store)",
    "process_wastage": "Wastage (measured loss)", "process_shrinkage": "Shrinkage (measured loss)",
    "dispatch_out": "Dispatched (stock deducted)", "dispatch_reversal": "Dispatch reversal",
    "multihead_consumption": "Consumed by Multihead order", "multihead_consumption_reversal": "Multihead consumption reversed",
}


def lot_trace(conn: sqlite3.Connection, lot_no: str) -> dict:
    lot = inventory.get_lot(conn, lot_no)
    fam = family_ids(conn, lot["id"])
    marks = ",".join("?" * len(fam))
    lots = conn.execute(f"SELECT l.*, i.unit FROM lots l JOIN items i ON i.id=l.item_id WHERE l.id IN ({marks}) ORDER BY l.id", fam).fetchall()
    ledger = conn.execute(
        f"SELECT sl.*, l.lot_no, loc.name AS loc_name, u.username AS actor FROM stock_ledger sl "
        f"JOIN lots l ON l.id=sl.lot_id JOIN locations loc ON loc.id=sl.location_id LEFT JOIN users u ON u.id=sl.actor_id "
        f"WHERE sl.lot_id IN ({marks}) ORDER BY sl.id", fam).fetchall()
    timeline = []
    for row in ledger:
        ref, url = doc_ref(conn, row["source_type"], row["source_id"])
        timeline.append({**dict(row), "doc_ref": ref, "url": url,
                         "label": MOVEMENT_LABELS.get(row["movement"], row["movement"])})
    balances = []
    for l in lots:
        for b in inventory.lot_balances(conn, l["id"]):
            balances.append({**b, "lot_no": l["lot_no"], "lot_id": l["id"], "unit": l["unit"]})

    def sum_mv(*movements):
        return sum(abs(t["qty"]) for t in timeline if t["movement"] in movements)

    summary = {
        "received": sum_mv("receipt") - sum_mv("receipt_reversal"),
        "issued_to_processing": sum_mv("process_issue_in"),
        "accepted": sum_mv("process_accepted_in"),
        "rejected": sum_mv("process_rejected_in"),
        "wastage": sum_mv("process_wastage"),
        "shrinkage": sum_mv("process_shrinkage"),
        "dispatched": sum_mv("dispatch_out") - sum_mv("dispatch_reversal"),
        "multihead_consumed": sum_mv("multihead_consumption") - sum_mv("multihead_consumption_reversal"),
    }
    results = conn.execute(
        f"SELECT pr.*, j.job_no, pi.issue_no, pi.qty AS issued_qty FROM processing_results pr "
        f"JOIN processing_jobs j ON j.id=pr.job_id JOIN processing_issues pi ON pi.id=pr.issue_id "
        f"WHERE j.lot_id IN ({marks}) AND pr.status='posted' ORDER BY pr.id", fam).fetchall()
    summary["open_variance"] = sum(r["variance"] for r in results if r["reconciliation_status"] == "exception")
    dispatches = conn.execute(
        f"SELECT DISTINCT d.id, d.doc_no, d.status, d.deducted_on, p.name AS customer_name, "
        f"gp.pass_no, gp.vehicle_no, gp.status AS pass_status, gp.exited_at "
        f"FROM dispatch_lines dl JOIN dispatches d ON d.id=dl.dispatch_id JOIN parties p ON p.id=d.customer_id "
        f"LEFT JOIN gate_passes gp ON gp.dispatch_id=d.id WHERE dl.lot_id IN ({marks}) ORDER BY d.id", fam).fetchall()
    receipts = conn.execute(
        f"SELECT g.id, g.doc_no, g.status, g.qty, p.name AS supplier_name FROM goods_receipts g "
        f"JOIN parties p ON p.id=g.supplier_id WHERE g.lot_id IN ({marks}) ORDER BY g.id", fam).fetchall()
    refs = {lot["lot_no"]} | {l["lot_no"] for l in lots}
    refs |= {r["doc_no"] for r in receipts} | {d["doc_no"] for d in dispatches}
    refs |= {p["pass_no"] for p in dispatches if p["pass_no"]}
    refs |= {r["result_no"] for r in results} | {r["issue_no"] for r in results} | {r["job_no"] for r in results}
    audit_rows = conn.execute(
        f"SELECT * FROM audit_events WHERE entity_ref IN ({','.join('?' * len(refs))}) AND outcome='success' ORDER BY id",
        sorted(refs)).fetchall()
    return {"lot": lot, "family": lots, "balances": balances, "timeline": timeline, "summary": summary,
            "results": results, "dispatches": dispatches, "receipts": receipts, "audit": audit_rows}
