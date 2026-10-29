"""Global search over records the user is allowed to see. Plain LIKE matching."""
from __future__ import annotations

import sqlite3

from ..permissions import can


def search(conn: sqlite3.Connection, user, q: str, limit: int = 15) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    like = f"%{q}%"
    role, out = user["role"], []
    if can(role, "stock.view"):
        for r in conn.execute("SELECT lot_no, kind FROM lots WHERE lot_no LIKE ? ORDER BY lot_no LIMIT ?", (like, limit)):
            out.append({"type": "Lot", "title": r["lot_no"], "meta": r["kind"], "url": f"/fabrics/lots/{r['lot_no']}"})
        for r in conn.execute("SELECT id, doc_no, lot_no, status FROM goods_receipts WHERE doc_no LIKE ? OR lot_no LIKE ? OR supplier_ref LIKE ? "
                              "ORDER BY id DESC LIMIT ?", (like, like, like, limit)):
            out.append({"type": "Goods receipt", "title": r["doc_no"], "meta": f"lot {r['lot_no']} · {r['status']}",
                        "url": f"/fabrics/receipts/{r['id']}"})
    if can(role, "processing.view"):
        for r in conn.execute("SELECT j.id, j.job_no, j.status, l.lot_no FROM processing_jobs j JOIN lots l ON l.id=j.lot_id "
                              "WHERE j.job_no LIKE ? OR l.lot_no LIKE ? ORDER BY j.id DESC LIMIT ?", (like, like, limit)):
            out.append({"type": "Processing job", "title": r["job_no"], "meta": f"lot {r['lot_no']} · {r['status']}",
                        "url": f"/processing/jobs/{r['id']}"})
    if can(role, "dispatch.view"):
        for r in conn.execute("SELECT d.id, d.doc_no, d.status, p.name FROM dispatches d JOIN parties p ON p.id=d.customer_id "
                              "WHERE d.doc_no LIKE ? OR p.name LIKE ? ORDER BY d.id DESC LIMIT ?", (like, like, limit)):
            out.append({"type": "Dispatch", "title": r["doc_no"], "meta": f"{r['name']} · {r['status']}", "url": f"/dispatch/{r['id']}"})
    if can(role, "gate.view"):
        for r in conn.execute("SELECT gp.id, gp.pass_no, gp.vehicle_no, gp.status FROM gate_passes gp "
                              "WHERE gp.pass_no LIKE ? OR gp.vehicle_no LIKE ? OR gp.driver_name LIKE ? ORDER BY gp.id DESC LIMIT ?",
                              (like, like, like, limit)):
            out.append({"type": "Gate pass", "title": r["pass_no"], "meta": f"vehicle {r['vehicle_no']} · {r['status']}",
                        "url": f"/gate?q={r['pass_no']}"})
    if can(role, "acc.view"):
        for r in conn.execute("SELECT code, name, unit FROM acc_items WHERE code LIKE ? OR name LIKE ? OR category LIKE ? ORDER BY code LIMIT ?", (like, like, like, limit)):
            out.append({"type": "Accessory item", "title": r["code"], "meta": f"{r['name']} · {r['unit']}", "url": f"/accessories?q={r['code']}"})
        for r in conn.execute("SELECT d.id, d.doc_no, d.status, d.doc_type FROM acc_docs d WHERE d.doc_no LIKE ? OR d.ref_no LIKE ? OR d.supplier_ref LIKE ? ORDER BY d.id DESC LIMIT ?", (like, like, like, limit)):
            out.append({"type": "Accessory document", "title": r["doc_no"], "meta": f"{r['doc_type']} · {r['status']}", "url": f"/accessories/docs/{r['id']}"})
    if can(role, "gate.view"):
        for r in conn.execute("SELECT id, entry_no, vehicle_no, direction, status FROM gate_entries WHERE entry_no LIKE ? OR vehicle_no LIKE ? OR driver_name LIKE ? OR material LIKE ? ORDER BY id DESC LIMIT ?", (like,) * 4 + (limit,)):
            out.append({"type": "Gate entry", "title": r["entry_no"], "meta": f"{r['direction']} · vehicle {r['vehicle_no']} · {r['status']}", "url": f"/gate/entries/{r['id']}"})
    if can(role, "mh.view"):
        for r in conn.execute("SELECT id, order_no, title, status FROM mh_orders WHERE order_no LIKE ? OR title LIKE ? OR operator LIKE ? ORDER BY id DESC LIMIT ?", (like, like, like, limit)):
            out.append({"type": "Multihead order", "title": r["order_no"], "meta": f"{r['title']} · {r['status']}", "url": f"/multihead/orders/{r['id']}"})
    if can(role, "fin.view"):
        for r in conn.execute("SELECT f.id, f.entry_no, f.kind, f.status, p.name FROM fin_entries f LEFT JOIN parties p ON p.id=f.party_id "
                              "WHERE f.entry_no LIKE ? OR f.reference LIKE ? OR f.doc_ref_no LIKE ? OR p.name LIKE ? ORDER BY f.id DESC LIMIT ?", (like,) * 4 + (limit,)):
            out.append({"type": "Finance entry", "title": r["entry_no"], "meta": f"{r['kind']} · {r['name'] or '—'} · {r['status']}", "url": f"/finance/entries/{r['id']}"})
        for r in conn.execute("SELECT id, name, kind FROM parties WHERE name LIKE ? ORDER BY name LIMIT ?", (like, limit)):
            out.append({"type": "Party", "title": r["name"], "meta": r["kind"], "url": f"/finance/parties/{r['id']}"})
    if can(role, "doc.view"):
        for r in conn.execute("SELECT id, original_name, status FROM documents WHERE original_name LIKE ? OR ocr_text LIKE ? ORDER BY id DESC LIMIT ?", (like, like, limit)):
            out.append({"type": "Document", "title": r["original_name"], "meta": r["status"], "url": f"/documents/{r['id']}"})
    if can(role, "alert.view"):
        for r in conn.execute("SELECT id, title, severity, status FROM alerts WHERE title LIKE ? OR detail LIKE ? ORDER BY id DESC LIMIT ?", (like, like, limit)):
            out.append({"type": "Alert", "title": r["title"], "meta": f"{r['severity']} · {r['status']}", "url": f"/watchtower/alerts/{r['id']}"})
    if can(role, "ledger.view"):
        for r in conn.execute("SELECT sl.id, sl.note, l.lot_no FROM stock_ledger sl JOIN lots l ON l.id=sl.lot_id WHERE sl.note LIKE ? ORDER BY sl.id DESC LIMIT ?", (like, limit)):
            out.append({"type": "Ledger note", "title": f"Ledger #{r['id']}", "meta": f"lot {r['lot_no']} · {r['note']}", "url": f"/ledger?lot={r['lot_no']}"})
    if can(role, "audit.view"):
        for r in conn.execute("SELECT id, action, entity_ref, ts FROM audit_events WHERE entity_ref LIKE ? OR action LIKE ? ORDER BY id DESC LIMIT ?", (like, like, 5)):
            out.append({"type": "Audit event", "title": r["action"], "meta": r["entity_ref"] or "", "url": f"/audit?ref={r['entity_ref'] or ''}"})
    return out
