"""Fictional demo data, built through the REAL services (no direct ledger inserts),
so the demo exercises the same validation, permissions and audit as production code.

Everything created here is fictional and flagged is_demo where the schema allows.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import audit, security
from .config import Config
from .db import now_iso, transaction
from .errors import BusinessRuleViolation
from .services import accessories, dispatch, finance, gate_entries, multihead, processing, receipts
from .services import settings as settings_svc

DEMO_USERS = [
    ("admin", "Demo Administrator", "admin"),
    ("ceo", "Demo CEO", "ceo"),
    ("manager1", "Demo Manager", "manager"),
    ("store1", "Demo Storekeeper", "storekeeper"),
    ("prod1", "Demo Production Lead", "production"),
    ("gate1", "Demo Gate Officer", "gate"),
    ("fin1", "Demo Finance Clerk", "finance"),
]


def init_structure(conn: sqlite3.Connection) -> None:
    """Structural records every installation needs (idempotent): fabric store locations and a default accessory store."""
    with transaction(conn):
        for code, name, kind in (("RAW", "Raw Store", "raw_store"), ("WIP", "Processing (WIP)", "wip"),
                                 ("FIN", "Finished Store", "finished_store"), ("REJ", "Reject Store", "reject_store")):
            conn.execute("INSERT OR IGNORE INTO locations(code, name, kind) VALUES (?,?,?)", (code, name, kind))
        conn.execute("INSERT OR IGNORE INTO acc_locations(code, name, active) VALUES ('ACC-MAIN','Main Accessory Store',1)")


def seed_reference(conn: sqlite3.Connection, cfg: Config) -> None:
    if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]:
        raise BusinessRuleViolation("Database already has users; refusing to seed.")
    pw_hash = security.hash_password(cfg.demo_password)
    with transaction(conn):
        for username, name, role in DEMO_USERS:
            conn.execute("INSERT INTO users(username, display_name, password_hash, role, active, created_at) VALUES (?,?,?,?,1,?)",
                         (username, name, pw_hash, role, now_iso()))
        conn.executemany("INSERT INTO parties(kind, name, is_demo) VALUES (?,?,1)", [
            ("supplier", "Demo Greige Mills (fictional)"),
            ("customer", "Demo Garments Ltd (fictional)"),
            ("job_work", "Demo Dye House (fictional)"),
        ])
        conn.execute("INSERT INTO items(code, name, unit, is_demo) VALUES ('FAB-CTN-60','Cotton greige 60in (demo)','m',1)")
        audit.record(conn, None, "seed.reference_data", "system", "demo", detail={"users": len(DEMO_USERS)})
    init_structure(conn)


def _user(conn, username):
    return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def seed_demo(conn: sqlite3.Connection, cfg: Config) -> dict:
    """Seed reference data plus the DEMO-104 journey. Only allowed in demo env."""
    if cfg.env != "demo":
        raise BusinessRuleViolation("Demo seeding is only allowed when WW_ENV=demo.")
    seed_reference(conn, cfg)
    store, mgr, prod, gate = (_user(conn, n) for n in ("store1", "manager1", "prod1", "gate1"))
    sup = conn.execute("SELECT id FROM parties WHERE kind='supplier'").fetchone()["id"]
    cust = conn.execute("SELECT id FROM parties WHERE kind='customer'").fetchone()["id"]
    dye = conn.execute("SELECT id FROM parties WHERE kind='job_work'").fetchone()["id"]
    item = conn.execute("SELECT id FROM items LIMIT 1").fetchone()["id"]
    raw = conn.execute("SELECT id FROM locations WHERE kind='raw_store'").fetchone()["id"]

    # 1-2. DEMO-104 receipt, saved then posted
    rid = receipts.create_draft(conn, store, dict(supplier_id=sup, item_id=item, lot_no="DEMO-104", location_id=raw,
                                                  qty="1000", unit="m", supplier_ref="DEMO-CHALLAN-77", vehicle_no="DEMO-TRK-1"))
    receipts.post(conn, store, rid)
    # 3-6. job, issue 500 m, post
    jid = processing.create_job(conn, mgr, dict(lot_no="DEMO-104", process_type="dyeing", party_id=dye,
                                                expected_date=(date.today() + timedelta(days=5)).isoformat(),
                                                notes="DEMO: dye to navy"))
    iid = processing.create_issue_draft(conn, store, jid, dict(qty="500", unit="m"))
    processing.post_issue(conn, store, iid)
    # 7-9. result with a deliberate 2.500 m unexplained variance (shown as an open exception, not hidden)
    resid = processing.create_result_draft(conn, prod, iid, dict(accepted="462", rejected="12.5", wastage="8", shrinkage="15",
                                                                 remaining="0", notes="DEMO result"))
    processing.post_result(conn, prod, resid)
    # 10-12. dispatch 300 m of finished lot: draft -> approve (separate user) -> gate pass -> exit
    did = dispatch.create_draft(conn, store, cust, [("DEMO-104-F1", "300")], notes="DEMO dispatch")
    dispatch.approve(conn, mgr, did)
    gp = dispatch.issue_gate_pass(conn, gate, did, "DEMO-LEB-1234", "Demo Driver")
    gpid = conn.execute("SELECT id FROM gate_passes WHERE pass_no=?", (gp,)).fetchone()["id"]
    dispatch.confirm_exit(conn, gate, gpid, "DEMO: vehicle left 22:10")

    # A second, still-open lot so the Overdue/WIP views have honest content.
    r2 = receipts.create_draft(conn, store, dict(supplier_id=sup, item_id=item, lot_no="DEMO-105", location_id=raw,
                                                 qty="400", unit="m", supplier_ref="DEMO-CHALLAN-78"))
    receipts.post(conn, store, r2)
    j2 = processing.create_job(conn, mgr, dict(lot_no="DEMO-105", process_type="printing", party_id=dye,
                                               expected_date=(date.today() - timedelta(days=4)).isoformat(),
                                               notes="DEMO: overdue job"))
    i2 = processing.create_issue_draft(conn, store, j2, dict(qty="150", unit="m"))
    processing.post_issue(conn, store, i2)
    _seed_modules(conn, cfg, sup, cust)
    return {"users": [u[0] for u in DEMO_USERS], "password_hint": "WW_DEMO_PASSWORD (default 'demo-only-change-me')",
            "lots": ["DEMO-104", "DEMO-104-F1", "DEMO-105"]}


def _seed_modules(conn, cfg, sup, cust) -> None:
    """Demo content for Accessory Store, Multihead, Gate entries and Finance, again through the real services."""
    admin, mgr, store, fin, gate = (_user(conn, n) for n in ("admin", "manager1", "store1", "fin1", "gate1"))
    # Accessory store
    loc = conn.execute("SELECT id FROM acc_locations WHERE code='ACC-MAIN'").fetchone()["id"]
    ids = {}
    for code, name, unit, dec, reorder in (("DEMO-BTN", "Demo buttons 18L", "pcs", "0", "500"), ("DEMO-THR", "Demo thread cone navy", "cone", "0", "20"),
                                           ("DEMO-ZIP", "Demo zipper 7in", "pcs", "0", "")):
        ids[code] = accessories.create_item(conn, mgr, dict(code=code, name=name, unit=unit, decimals=dec, reorder_level=reorder, category="Demo"))
    d = accessories.create_draft(conn, store, "receipt", dict(supplier_id=sup, to_location_id=loc, supplier_ref="DEMO-ACC-1"),
                                 [(str(ids["DEMO-BTN"]), "1200"), (str(ids["DEMO-THR"]), "30"), (str(ids["DEMO-ZIP"]), "250")])
    accessories.post(conn, store, d)
    # Multihead: the unit is a business decision, so the demo sets one explicitly (visible in Settings) rather than the app assuming it.
    settings_svc.set_value(conn, admin, "multihead.units", "pcs:0,m:3")
    mid = multihead.create_machine(conn, mgr, dict(code="DEMO-MH1", name="Demo multihead machine 1"))
    oid = multihead.create_order(conn, mgr, dict(title="DEMO logo embroidery", planned_qty="400", unit="pcs", machine_id=str(mid),
                                                  operator="Demo Operator", due_date=(date.today() + timedelta(days=3)).isoformat()))
    prod = _user(conn, "prod1")
    multihead.log_production(conn, prod, oid, dict(good_qty="120", rejected_qty="4", operator="Demo Operator", machine_id=str(mid)))
    # Gate entries (these record movements at the gate; they do not change stock)
    rcpt = conn.execute("SELECT doc_no FROM goods_receipts WHERE status='posted' ORDER BY id LIMIT 1").fetchone()["doc_no"]
    gid = gate_entries.create(conn, gate, dict(direction="inward", vehicle_no="DEMO-TRK-9", driver_name="Demo Driver", party_id=str(sup),
                                               material="Demo greige", declared_qty="400", declared_unit="m", ref_type="goods_receipt", ref_no=rcpt))
    gate_entries.confirm_exit(conn, gate, gid, "DEMO: unloaded and left")
    gate_entries.create(conn, gate, dict(direction="inward", vehicle_no="DEMO-VAN-3", driver_name="Demo Courier", party_text="Demo courier (fictional)",
                                         material="Demo buttons"))
    # Finance
    bill = finance.create_draft(conn, fin, "bill", dict(party_id=str(sup), entry_date=date.today().isoformat(), amount="250000",
                                                         description="DEMO bill for greige DEMO-104", reference="DEMO-BILL-1"), cfg.currency)
    finance.post(conn, mgr, bill)
    inv = finance.create_draft(conn, fin, "invoice", dict(party_id=str(cust), entry_date=date.today().isoformat(), amount="480000",
                                                           description="DEMO invoice for DEMO-104-F1", reference="DEMO-INV-1"), cfg.currency)
    finance.post(conn, mgr, inv)
    pay = finance.create_draft(conn, fin, "payment_out", dict(party_id=str(sup), entry_date=date.today().isoformat(), amount="100000",
                                                              method="bank", description="DEMO part payment", reference="DEMO-PAY-1"), cfg.currency)
    finance.post(conn, mgr, pay)
    finance.allocate(conn, mgr, pay, bill, "100000")
