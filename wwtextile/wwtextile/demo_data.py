"""Showcase data set for live demonstrations: ~8 weeks of fictional textile-factory activity in every module.

Everything is created through the REAL services (same validation, permissions, maker-checker, ledgers and audit as normal use),
so the data is internally consistent: stock = ledger, bills match receipts, invoices match dispatches, losses reconcile.
Only the clock is simulated: while seeding, timestamps are moved into the past so reports, charts and audit history have a
history. After seeding the clock is the real one. All names, numbers and vehicles are invented and generated from a seed
(WW_DEMO_SEED) so a reset reproduces the same story, or a different one with another seed.
"""
from __future__ import annotations

import os
import random
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from . import audit, db, security
from .config import Config
from .errors import BusinessRuleViolation
from .services import (accessories, dispatch, finance, gate_entries, master, multihead, processing, receipts,
                       settings as settings_svc, watchtower)

USERS = [  # username, display name, role
    ("admin", "Zain Malik (Administrator)", "admin"),
    ("ceo", "Sara Qureshi (CEO)", "ceo"),
    ("manager1", "Imran Siddiqui (Manager)", "manager"),
    ("manager2", "Nadia Farooq (Manager)", "manager"),
    ("store1", "Bilal Ahmad (Storekeeper)", "storekeeper"),
    ("store2", "Hina Rauf (Storekeeper)", "storekeeper"),
    ("prod1", "Kamran Aslam (Production)", "production"),
    ("prod2", "Rubina Shah (Production)", "production"),
    ("gate1", "Tariq Mehmood (Gate)", "gate"),
    ("gate2", "Asif Javed (Gate)", "gate"),
    ("fin1", "Maryam Anwar (Finance)", "finance"),
]
CLIENT_USERS = [u[0] for u in USERS if u[0] != "admin"]

SUPPLIERS = ["Zarbaft Greige Mills", "Kohsar Looms", "Mehrab Kora Traders", "Chenab Weaving Works", "Rehnuma Grey Cloth House", "Shalimar Loom Co."]
CUSTOMERS = ["Libaas Garments", "Khayal Apparel Exports", "Urban Thread Fashion", "Sitara Home Textiles", "Mehfil Boutique Chain", "Rawaj Fashion House"]
JOB_WORK = {"dyeing": ["Rangrez Dye Works", "Neelam Process House"], "printing": ["Naqsh Print Studio", "Chhaap Digital Printers"],
            "embroidery": ["Zardozi Embroidery Unit"]}
ACC_SUPPLIERS = ["Button Bazaar Traders", "ZipLine Accessories", "Dhaaga Thread Mills", "PackRight Cartons"]
FABRICS = [  # code, name, supply rate PKR/m (low, high)
    ("KK-C60", "Kora Kapra Cotton 60x60 (grey)", (260, 340)), ("KK-PLN", "Kora Poplin 100x80 (grey)", (330, 420)),
    ("KK-TWL", "Kora Twill 3/1 (grey)", (380, 480)), ("KK-LWN", "Kora Lawn Voile 60x60 (grey)", (240, 310)),
    ("KK-CMB", "Kora Cambric 80x80 (grey)", (290, 370)), ("KK-STN", "Kora Sateen (grey)", (420, 520)),
]
ACC_ITEMS = [  # code, name, unit, decimals, reorder level
    ("ACC-BTN18", "Poly button 18L", "pcs", "0", "2000"), ("ACC-BTN24", "Metal button 24L", "pcs", "0", "1000"),
    ("ACC-ZIP7", "Zipper 7in", "pcs", "0", "800"), ("ACC-THR-NV", "Thread cone navy", "cone", "0", "40"),
    ("ACC-THR-WH", "Thread cone white", "cone", "0", "40"), ("ACC-EMB-THR", "Embroidery thread (rayon) cone", "cone", "0", "60"),
    ("ACC-LBL", "Woven brand label", "pcs", "0", "3000"), ("ACC-TAG", "Hang tag", "pcs", "0", "3000"),
    ("ACC-POLY", "Polybag 12x16", "pcs", "0", "2500"), ("ACC-CTN", "Export carton 5-ply", "pcs", "0", "150"),
    ("ACC-ELS", "Elastic 1in", "m", "3", "300"),
]
EXPENSES = [("Electricity", (180000, 420000)), ("Fuel & transport", (30000, 140000)), ("Salaries", (900000, 1900000)),
            ("Repairs & maintenance", (15000, 160000)), ("Rent", (250000, 250000)), ("Packing material", (20000, 90000)),
            ("Canteen & welfare", (25000, 80000)), ("Security", (60000, 60000)), ("Internet & telephone", (8000, 22000))]
DRIVERS = ["Muhammad Shafiq", "Abdul Rehman", "Ghulam Abbas", "Naveed Khan", "Rashid Ali", "Sajid Hussain", "Waqas Ahmad", "Irfan Baig"]
PLATE_LETTERS = ["FSD", "LEA", "LXZ", "FDA", "GJB", "RIA", "SKT"]


# ------------------------------------------------------------------ simulated clock (seeding only)
class SimClock:
    def __init__(self):
        self.t: datetime | None = None
        self.n = 0
        self._patched: list[tuple[object, str, object]] = []

    def at(self, days_ago: int, hour: int = 10, minute: int = 0) -> None:
        """Move the simulated clock to a local (Asia/Karachi-ish, UTC+5) working hour `days_ago` days back. Never into the future."""
        real = datetime.now(timezone.utc)
        base = real.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
        t = base + timedelta(hours=hour - 5, minutes=minute)
        if t > real - timedelta(minutes=1):
            t = real - timedelta(minutes=45)
        self.t, self.n = t, 0

    def now(self) -> str:
        self.n += 1
        return (self.t + timedelta(milliseconds=self.n * 53)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def __enter__(self):
        orig = db.now_iso
        for name, mod in list(sys.modules.items()):
            if name.startswith("wwtextile") and getattr(mod, "now_iso", None) is orig:
                self._patched.append((mod, "now_iso", orig))
                mod.now_iso = self.now
        self.at(60)
        return self

    def __exit__(self, *exc):
        for mod, attr, orig in self._patched:
            setattr(mod, attr, orig)
        self._patched.clear()


def _d(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _m(x: float) -> str:  # metres, thousandths
    return f"{x:.3f}".rstrip("0").rstrip(".")


def _half(x: float) -> float:  # round to 0.5 m like a real measuring table
    return round(x * 2) / 2


class Demo:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, seed: int):
        self.c, self.cfg, self.r = conn, cfg, random.Random(seed)
        self.clock: SimClock = None  # type: ignore
        self.u: dict[str, sqlite3.Row] = {}
        self.party: dict[str, int] = {}
        self.item: dict[str, int] = {}
        self.loc: dict[str, int] = {}
        self.lots: list[dict] = []       # raw lots: lot_no, qty, day, supplier_id, item_code, receipt_no, rate
        self.jobs: list[dict] = []
        self.finished: list[dict] = []   # lot_no, kind (process), qty
        self.bills: list[int] = []

    # ---------------------------------------------------------------- helpers
    def at(self, d: int, h: int = 10, m: int = 0):
        self.clock.at(d, h, m)

    def plate(self) -> str:
        return f"DM-{self.r.choice(PLATE_LETTERS)}-{self.r.randint(1000, 9899)}"

    def phone(self) -> str:
        return f"+92 3{self.r.randint(0, 4)}{self.r.randint(0, 9)} {self.r.randint(1000000, 9999999)}"

    # ---------------------------------------------------------------- master data
    def users(self):
        with db.transaction(self.c):
            for username, name, role in USERS:
                pw = self.cfg.demo_admin_password if (username == "admin" and self.cfg.demo_admin_password) else self.cfg.demo_password
                self.c.execute("INSERT INTO users(username, display_name, password_hash, role, active, created_at) VALUES (?,?,?,?,1,?)",
                               (username, name, security.hash_password(pw), role, db.now_iso()))
        for username, _n, _r in USERS:
            self.u[username] = self.c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        self.loc = {r["code"]: r["id"] for r in self.c.execute("SELECT id, code FROM locations")}

    def masters(self):
        a = self.u["admin"]
        for n in SUPPLIERS + ACC_SUPPLIERS:
            self.party[n] = master.create_party(self.c, a, dict(kind="supplier", name=n))
        for n in CUSTOMERS:
            self.party[n] = master.create_party(self.c, a, dict(kind="customer", name=n))
        for names in JOB_WORK.values():
            for n in names:
                self.party[n] = master.create_party(self.c, a, dict(kind="job_work", name=n))
        for code, name, _rate in FABRICS:
            self.item[code] = master.create_item(self.c, a, dict(code=code, name=name, unit="m"))
        self.c.execute("UPDATE parties SET is_demo=1")
        self.c.execute("UPDATE items SET is_demo=1")

    # ---------------------------------------------------------------- fabrics: receipts + gate inward
    def receive_lots(self, n: int = 20):
        for i in range(n):
            day = max(1, round(54 - i * 2.8))
            code, _name, (lo, hi) = FABRICS[self.r.randrange(len(FABRICS))]
            sup = self.r.choice(SUPPLIERS)
            qty = self.r.choice([1200, 1500, 1800, 2000, 2400, 3000, 3500])
            lot_no = f"KK-{(date.today() - timedelta(days=day)).strftime('%y%m')}-{i + 1:03d}"
            veh, drv = self.plate(), self.r.choice(DRIVERS)
            ref = f"CH-{self.r.randint(4100, 9400)}"
            self.at(day, 10, 5)
            who = self.u["store1" if i % 3 else "store2"]
            rid = receipts.create_draft(self.c, who, dict(supplier_id=str(self.party[sup]), item_id=str(self.item[code]), lot_no=lot_no, location_id=str(self.loc["RAW"]),
                                                          qty=str(qty), unit="m", supplier_ref=ref, vehicle_no=veh, notes="Counted at unloading"))
            doc = receipts.post(self.c, who, rid)
            self.at(day, 10, 12)
            gid = gate_entries.create(self.c, self.u["gate1"], dict(direction="inward", vehicle_no=veh, driver_name=drv, driver_phone=self.phone(), party_id=str(self.party[sup]),
                                                                       material=f"Kora kapra {code}", declared_qty=str(qty), declared_unit="m", ref_type="goods_receipt", ref_no=doc))
            self.at(day, 10, 50)
            gate_entries.confirm_exit(self.c, self.u["gate2" if i % 4 == 0 else "gate1"], gid, "Unloaded and left")
            self.lots.append(dict(lot_no=lot_no, qty=qty, day=day, sup=sup, code=code, doc=doc, rate=self.r.randint(lo, hi), ref=ref))

    # ---------------------------------------------------------------- processing
    def process(self):
        plan = ["dyeing", "dyeing", "printing", "dyeing", "embroidery", "printing", "dyeing", "embroidery", "printing"]
        shrink = {"dyeing": (2.5, 4.5), "printing": (3.0, 5.0), "embroidery": (0.5, 1.5)}
        exc_at = {3: 4.5, 6: 11.0}          # jobs that finish with a small UNEXPLAINED variance (shown as open exceptions)
        for i, lot in enumerate(self.lots[:14]):
            ptype = plan[i] if i < len(plan) else self.r.choice(["dyeing", "printing"])
            party = self.party[self.r.choice(JOB_WORK[ptype])]
            r = lot["day"]
            self.at(r - 1, 9, 30)
            expected = _d(r - 7) if i < 9 else (_d(4) if i == 9 else _d(-3))
            jid = processing.create_job(self.c, self.u["manager1"], dict(lot_no=lot["lot_no"], process_type=ptype, party_id=str(party), expected_date=expected,
                                                                          notes={"dyeing": "Reactive dye, shade as per approved swatch", "printing": "Pigment print, 2 colours",
                                                                                 "embroidery": "Allover embroidery, 8mm stitch"}[ptype]))
            job = self.c.execute("SELECT job_no FROM processing_jobs WHERE id=?", (jid,)).fetchone()["job_no"]
            rec = dict(job_id=jid, job_no=job, lot=lot, ptype=ptype, party=party)
            self.jobs.append(rec)
            if i == 12:
                continue                                        # job created, nothing issued yet
            qty = _half(lot["qty"] * self.r.uniform(0.6, 0.95)) // 10 * 10
            self.at(r - 1, 11, 10)
            iid = processing.create_issue_draft(self.c, self.u["store1"], jid, dict(qty=_m(qty), unit="m"))
            rec["issued"], rec["issue_id"] = qty, iid
            if i == 11:
                continue                                        # unposted issue draft left on purpose
            self.at(r - 1, 11, 40)
            processing.post_issue(self.c, self.u["store1"], iid)
            if i >= 9 and i != 13:
                continue                                        # in process, awaiting result
            lo, hi = shrink[ptype]
            sh = _half(qty * self.r.uniform(lo, hi) / 100)
            wa = _half(qty * self.r.uniform(0.6, 1.8) / 100)
            rj = _half(qty * self.r.uniform(1.5, 4.0) / 100)
            extra = exc_at.get(i, 0.0)
            acc = qty - sh - wa - rj - extra
            res = dict(accepted=_m(acc), rejected=_m(rj), wastage=_m(wa), shrinkage=_m(sh), remaining="0",
                       notes=("Shade variation noted on 2 rolls" if rj > qty * 0.03 else "Passed QC"))
            self.at(r - 6, 15, 0)
            rid = processing.create_result_draft(self.c, self.u["prod1" if i % 2 else "prod2"], iid, res)
            if i == 13:
                continue                                        # unposted result draft left on purpose
            self.at(r - 6, 15, 30)
            processing.post_result(self.c, self.u["prod1" if i % 2 else "prod2"], rid)
            fin = self.c.execute("SELECT lot_no FROM lots WHERE parent_lot_id=(SELECT id FROM lots WHERE lot_no=?) AND kind='finished' ORDER BY id DESC LIMIT 1",
                                 (lot["lot_no"],)).fetchone()
            rec["result_day"] = r - 6
            self.finished.append(dict(lot_no=fin["lot_no"], ptype=ptype, qty=acc, job=rec))

    # ---------------------------------------------------------------- accessories
    def accessories(self):
        mgr, st = self.u["manager1"], self.u["store1"]
        self.at(56, 9)
        main = self.c.execute("SELECT id FROM acc_locations WHERE code='ACC-MAIN'").fetchone()["id"]
        emb = accessories.create_location(self.c, mgr, dict(code="ACC-EMB", name="Embroidery floor store"))
        ids = {}
        for code, name, unit, dec, reorder in ACC_ITEMS:
            ids[code] = accessories.create_item(self.c, mgr, dict(code=code, name=name, unit=unit, decimals=dec, reorder_level=reorder, category="Accessories"))

        def receipt(day, sup, lines, ref):
            self.at(day, 11)
            d = accessories.create_draft(self.c, st, "receipt", dict(supplier_id=str(self.party[sup]), to_location_id=str(main), supplier_ref=ref), lines)
            doc = accessories.post(self.c, st, d)
            self.at(day, 10, 15)
            g = gate_entries.create(self.c, self.u["gate1"], dict(direction="inward", vehicle_no=self.plate(), driver_name=self.r.choice(DRIVERS), party_id=str(self.party[sup]),
                                                                   material="Accessories", ref_type="acc_doc", ref_no=doc))
            self.at(day, 10, 40)
            gate_entries.confirm_exit(self.c, self.u["gate1"], g, "Unloaded and left")
        receipt(52, "Button Bazaar Traders", [(ids["ACC-BTN18"], "24000"), (ids["ACC-BTN24"], "6000"), (ids["ACC-ZIP7"], "5000")], "BB-1182")
        receipt(51, "Dhaaga Thread Mills", [(ids["ACC-THR-NV"], "180"), (ids["ACC-THR-WH"], "160"), (ids["ACC-EMB-THR"], "300"), (ids["ACC-ELS"], "1800")], "DT-0731")
        receipt(40, "PackRight Cartons", [(ids["ACC-POLY"], "30000"), (ids["ACC-CTN"], "900"), (ids["ACC-LBL"], "26000"), (ids["ACC-TAG"], "24000")], "PR-5540")
        receipt(14, "ZipLine Accessories", [(ids["ACC-ZIP7"], "4000")], "ZL-220")
        # embroidery thread to the floor store, then consumed against embroidery jobs / multihead orders
        self.at(45, 12)
        t = accessories.create_draft(self.c, st, "transfer", dict(from_location_id=str(main), to_location_id=str(emb), notes="Weekly floor stock"), [(ids["ACC-EMB-THR"], "120")])
        accessories.post(self.c, st, t)
        self.acc_ids, self.acc_main, self.acc_emb = ids, main, emb

    def accessory_issues(self):
        st, ids = self.u["store1"], self.acc_ids
        for rec in self.jobs:
            if rec["ptype"] == "embroidery" and rec.get("issued"):
                self.at(rec["lot"]["day"] - 1, 13)
                d = accessories.create_draft(self.c, st, "issue", dict(from_location_id=str(self.acc_emb), ref_type="processing_job", ref_no=rec["job_no"], notes="Thread for embroidery"),
                                             [(ids["ACC-EMB-THR"], str(self.r.choice([18, 22, 26])))])
                accessories.post(self.c, st, d)
        self.at(20, 14)
        d = accessories.create_draft(self.c, st, "issue", dict(from_location_id=str(self.acc_main), ref_type="other", ref_no="Stitching department", notes="Sample garments"),
                                     [(ids["ACC-BTN18"], "22500"), (ids["ACC-THR-NV"], "170"), (ids["ACC-ZIP7"], "3300")])
        accessories.post(self.c, st, d)       # leaves buttons, navy thread and zippers low -> low-stock alerts
        self.at(9, 14)
        d = accessories.create_draft(self.c, st, "issue", dict(from_location_id=str(self.acc_main), ref_type="other", ref_no="Packing department", notes="Dispatch packing"),
                                     [(ids["ACC-POLY"], "9000"), (ids["ACC-CTN"], "260")])
        accessories.post(self.c, st, d)

    # ---------------------------------------------------------------- dispatch + gate passes
    def dispatches(self):
        mgr, st, gate = self.u["manager2"], self.u["store1"], self.u["gate1"]
        pool = [f for f in self.finished if f["ptype"] != "embroidery"]          # embroidery output is kept for Multihead
        self.r.shuffle(pool)
        plan = [(20, "done"), (17, "done"), (14, "done"), (11, "done"), (8, "done"), (1, "at_gate"), (0, "approved"), (0, "draft")]
        left = {f["lot_no"]: f["qty"] for f in pool}
        self.sold = []
        cust = self.r.sample(CUSTOMERS, len(CUSTOMERS))
        for k, (day, state) in enumerate(plan):
            lines, total = [], 0.0
            for f in pool:
                if len(lines) == (2 if k % 2 == 0 else 1):
                    break
                avail = left[f["lot_no"]]
                if avail < 150 or (f["job"].get("result_day", 0) < day):
                    continue
                take = _half(min(avail, self.r.choice([0.3, 0.4, 0.5, 0.65]) * f["qty"]))
                take = min(take, avail)
                lines.append((f["lot_no"], _m(take)))
                left[f["lot_no"]] -= take
                total += take
            if not lines:
                continue
            c = cust[k % len(cust)]
            self.at(day, 10, 0 if day else 0)
            did = dispatch.create_draft(self.c, st, self.party[c], lines, notes=f"Order {self.r.randint(100, 999)}/{date.today().strftime('%y')} - {c}")
            rec = dict(id=did, customer=c, qty=total, day=day, state=state)
            if state != "draft":
                self.at(day, 11, 0)
                dispatch.approve(self.c, mgr, did)
            if state in ("done", "at_gate"):
                self.at(day, 13, 0)
                gp = dispatch.issue_gate_pass(self.c, gate, did, self.plate(), self.r.choice(DRIVERS))
                gpid = self.c.execute("SELECT id FROM gate_passes WHERE pass_no=?", (gp,)).fetchone()["id"]
                if state == "done":
                    self.at(day, 14, 20)
                    dispatch.confirm_exit(self.c, self.u["gate2"], gpid, "Loaded, seals checked, vehicle left")
            self.sold.append(rec)

    # ---------------------------------------------------------------- gate: non-dispatch movements
    def gate_misc(self):
        g1, mgr = self.u["gate1"], self.u["manager1"]
        self.at(26, 12)
        out = gate_entries.create(self.c, g1, dict(direction="outward", vehicle_no=self.plate(), driver_name=self.r.choice(DRIVERS), party_id=str(self.party[SUPPLIERS[0]]),
                                                    material="Rejected rolls returned to supplier", declared_qty="85", declared_unit="m", notes="Return of damaged rolls"))
        self.at(26, 12, 30)
        gate_entries.approve(self.c, mgr, out)
        self.at(26, 13)
        gate_entries.confirm_exit(self.c, g1, out, "Returned goods loaded")
        self.at(5, 11)
        out2 = gate_entries.create(self.c, g1, dict(direction="outward", vehicle_no=self.plate(), driver_name=self.r.choice(DRIVERS), party_text="Courier - sample shipment",
                                                     material="Fabric swatches for buyer approval", notes="Sample parcel"))
        self.at(5, 11, 20)
        gate_entries.approve(self.c, mgr, out2)
        self.at(5, 11, 40)
        gate_entries.confirm_exit(self.c, g1, out2, "Courier collected")
        self.at(0, 8, 0)    # waiting for approval today
        gate_entries.create(self.c, g1, dict(direction="outward", vehicle_no=self.plate(), driver_name=self.r.choice(DRIVERS), party_text="Customer pickup",
                                              material="Finished fabric (pending paperwork)", notes="Awaiting manager approval"))
        self.at(1, 15, 0)   # a vehicle that arrived yesterday and has not left: a genuine alert for the Watchtower
        gate_entries.create(self.c, g1, dict(direction="inward", vehicle_no=self.plate(), driver_name=self.r.choice(DRIVERS), party_id=str(self.party[SUPPLIERS[2]]),
                                              material="Kora kapra (unloading not started)", declared_qty="1800", declared_unit="m"))

    # ---------------------------------------------------------------- multihead
    def multihead(self):
        a, mgr, prod = self.u["admin"], self.u["manager1"], self.u["prod1"]
        self.at(50, 9)
        settings_svc.set_value(self.c, a, "multihead.units", "pcs:0, m:3")
        settings_svc.set_value(self.c, a, "multihead.fabric_stock_effect", "consume")
        machines = [multihead.create_machine(self.c, mgr, dict(code=f"MH-0{i}", name=f"Multihead machine {i}")) for i in range(1, 5)]
        emb = [f for f in self.finished if f["ptype"] == "embroidery"]
        orders = [("Logo embroidery - polo front panels", 2400, 2, 22), ("Border embroidery - lawn suits", 1800, 1, 14), ("Allover embroidery - kurta fronts", 3000, 3, 6),
                  ("Neck motif - ladies shirts", 1200, 0, 3), ("Cap logo embroidery", 4000, 2, -2), ("Pocket badge - uniforms", 900, 1, 40)]
        for k, (title, planned, mi, due_in) in enumerate(orders):
            start = 38 - k * 6
            self.at(start, 9, 30)
            oid = multihead.create_order(self.c, mgr, dict(title=title, planned_qty=str(planned), unit="pcs", machine_id=str(machines[mi % 4]),
                                                            operator=self.r.choice(["Shahid", "Adeel", "Rizwan", "Faisal"]), due_date=_d(-due_in) if due_in > 0 else _d(-due_in * -1) if due_in else _d(0)))
            done_target = {0: 0.62, 1: 1.0, 2: 0.35, 3: 0.2, 4: 0.55, 5: 1.0}[k]
            produced, day = 0, start - 1
            while produced < planned * done_target and day >= 0:
                self.at(day, 17, 0)
                good = int(min(planned * done_target - produced, self.r.randint(180, 420)))
                if good <= 0:
                    break
                multihead.log_production(self.c, self.u["prod1" if day % 2 else "prod2"], oid,
                                         dict(log_date=_d(day), good_qty=str(good), rejected_qty=str(self.r.randint(0, max(1, good // 40))),
                                              operator="Shahid", machine_id=str(machines[mi % 4]), notes="Shift output"))
                produced += good
                day -= self.r.randint(1, 2)
            if emb and k in (0, 1):
                f = emb[min(k, len(emb) - 1)]
                self.at(max(day, 0) + 1, 9)
                q = _half(min(f["qty"] * 0.45, 260))
                try:
                    multihead.consume_fabric(self.c, prod, oid, dict(lot_no=f["lot_no"], location_id=str(self.loc["FIN"]), qty=_m(q), waste_qty="3", notes="Cut panels"))
                except Exception:
                    pass
            if k in (1, 5):
                self.at(max(day, 0) + 0, 18)
                multihead.close_order(self.c, mgr, oid, "completed", "Delivered to stitching")
            if k in (0, 2):
                self.at(max(day, 0) + 1, 9, 30)
                try:
                    d = accessories.create_draft(self.c, self.u["store1"], "issue", dict(from_location_id=str(self.acc_emb), ref_type="multihead_order",
                                                                                         ref_no=self.c.execute("SELECT order_no FROM mh_orders WHERE id=?", (oid,)).fetchone()["order_no"],
                                                                                         notes="Thread for order"), [(self.acc_ids["ACC-EMB-THR"], "14")])
                    accessories.post(self.c, self.u["store1"], d)
                except Exception:
                    pass

    # ---------------------------------------------------------------- finance
    def finance(self):
        fin, mgr = self.u["fin1"], self.u["manager1"]
        cur = self.cfg.currency

        def money(v: float) -> str:
            return f"{round(v):d}.00"

        def entry(kind, day, party, amount, desc, ref="", method="", category=""):
            self.at(day, 12)
            fid = finance.create_draft(self.c, fin, kind, dict(party_id=str(party) if party else "", entry_date=_d(day), amount=money(amount), description=desc,
                                                              reference=ref, method=method, category=category), cur)
            self.at(day, 16)
            finance.post(self.c, mgr, fid)
            return fid
        bills = []
        for lot in self.lots:           # one supplier bill per receipt, dated the day after
            amt = lot["qty"] * lot["rate"]
            bills.append((lot, entry("bill", max(lot["day"] - 1, 0), self.party[lot["sup"]], amt, f"Kora kapra {lot['lot_no']} ({lot['qty']} m)", lot["ref"]), amt))
        for rec in self.jobs:           # job-work charges for completed jobs
            if rec.get("result_day") is not None:
                rate = {"dyeing": self.r.randint(90, 140), "printing": self.r.randint(150, 260), "embroidery": self.r.randint(300, 560)}[rec["ptype"]]
                amt = rec["issued"] * rate
                bills.append((None, entry("bill", max(rec["result_day"] - 1, 0), rec["party"], amt, f"{rec['ptype'].title()} charges {rec['job_no']}", rec["job_no"]), amt))
        paid_days = 0
        for k, (lot, bid, amt) in enumerate(bills):
            day = (lot["day"] if lot else 20) - 1
            if k % 5 == 4 or day < 3:
                continue                                   # a share of bills stays unpaid -> payables ageing has content
            pay_day = max(day - self.r.randint(6, 16), 1)
            part = amt if k % 3 else round(amt * 0.5)
            party_id = self.c.execute("SELECT party_id FROM fin_entries WHERE id=?", (bid,)).fetchone()["party_id"]
            if pay_day > day:
                continue
            pid = entry("payment_out", pay_day, party_id, part, f"Payment against bill {k + 1}", f"PAY-{self.r.randint(1000, 9999)}", self.r.choice(["bank transfer", "cheque", "cash"]))
            self.at(pay_day, 16, 30)
            finance.allocate(self.c, mgr, pid, bid, money(part))
        for k, s in enumerate(self.sold):
            if s["state"] == "draft":
                continue
            rate = self.r.randint(900, 1800)
            cid = self.party[s["customer"]]
            inv = entry("invoice", s["day"], cid, s["qty"] * rate, f"Dispatch to {s['customer']}", f"INV-{3000 + k}")
            if s["state"] == "done" and k % 2 == 0:
                pay_day = max(s["day"] - self.r.randint(3, 7), 0)
                part = s["qty"] * rate if k != 2 else round(s["qty"] * rate * 0.6)
                pid = entry("payment_in", pay_day, cid, part, "Payment received", f"RCPT-{self.r.randint(100, 999)}", self.r.choice(["bank transfer", "cheque"]))
                self.at(pay_day, 16, 30)
                finance.allocate(self.c, mgr, pid, inv, money(part))
        for day in range(52, 0, -6):
            cat, (lo, hi) = self.r.choice(EXPENSES)
            entry("expense", day, None, self.r.randint(lo, hi) if lo != hi else lo, f"{cat} - monthly", f"EXP-{self.r.randint(100, 999)}", category=cat)
        # an unposted draft or two
        self.at(1, 11)
        finance.create_draft(self.c, fin, "expense", dict(entry_date=_d(1), amount="45000.00", category="Repairs & maintenance", description="Loom motor rewinding (awaiting approval)"), cur)
        self.at(0, 9)
        finance.create_draft(self.c, fin, "bill", dict(party_id=str(self.party[ACC_SUPPLIERS[0]]), entry_date=_d(0), amount="168000.00", description="Buttons - bill awaiting posting", reference="BB-1190"), cur)


def build(conn: sqlite3.Connection, cfg: Config, seed: int | None = None) -> dict:
    """Populate an EMPTY, migrated database with the showcase story. Demo environment only."""
    if cfg.env != "demo":
        raise BusinessRuleViolation("Demo data can only be created when WW_ENV=demo.")
    if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]:
        raise BusinessRuleViolation("Database already has users; refusing to seed.")
    from .seed import init_structure
    init_structure(conn)
    d = Demo(conn, cfg, cfg.demo_seed if seed is None else seed)
    with SimClock() as clock:
        d.clock = clock
        d.users()
        d.masters()
        d.receive_lots()
        d.accessories()
        d.process()
        d.accessory_issues()
        d.dispatches()
        d.gate_misc()
        d.multihead()
        d.finance()
        clock.at(0, 0)
        audit.record(conn, None, "seed.showcase", "system", "demo", detail={"seed": cfg.demo_seed if seed is None else seed})
    scan = watchtower.scan(conn, None)
    return summary(conn) | {"watchtower": scan}


def summary(conn: sqlite3.Connection) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]
    return dict(users=q("SELECT COUNT(*) FROM users"), parties=q("SELECT COUNT(*) FROM parties"), lots=q("SELECT COUNT(*) FROM lots"),
                receipts=q("SELECT COUNT(*) FROM goods_receipts"), jobs=q("SELECT COUNT(*) FROM processing_jobs"),
                results=q("SELECT COUNT(*) FROM processing_results WHERE status='posted'"), dispatches=q("SELECT COUNT(*) FROM dispatches"),
                gate_passes=q("SELECT COUNT(*) FROM gate_passes"), gate_entries=q("SELECT COUNT(*) FROM gate_entries"),
                accessory_docs=q("SELECT COUNT(*) FROM acc_docs"), mh_orders=q("SELECT COUNT(*) FROM mh_orders"), mh_logs=q("SELECT COUNT(*) FROM mh_logs"),
                finance_entries=q("SELECT COUNT(*) FROM fin_entries"), audit_events=q("SELECT COUNT(*) FROM audit_events"))


# ------------------------------------------------------------------ reset (demo environment only)
def reset(cfg: Config, seed: int | None = None) -> dict:
    """Delete the demo database and uploads, migrate, and regenerate. Refuses outside WW_ENV=demo."""
    if cfg.env != "demo":
        raise BusinessRuleViolation("Reset is only available when WW_ENV=demo.")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(cfg.db_path + suffix)
        except FileNotFoundError:
            pass
    if os.path.isdir(cfg.upload_dir):
        shutil.rmtree(cfg.upload_dir, ignore_errors=True)
    conn = db.connect(cfg.db_path)
    try:
        db.migrate(conn)
        return build(conn, cfg, seed)
    finally:
        conn.close()
