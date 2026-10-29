import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wwtextile import config, db, seed  # noqa: E402
from wwtextile.services import dispatch, processing, receipts  # noqa: E402


class World:
    """A migrated database with reference data and helpers. Not the DEMO-104 journey."""

    def __init__(self, path: str):
        self.path = path
        self.cfg = dataclasses.replace(config.load(), env="demo", db_path=path, demo_password="test-pass-1")
        self.conn = db.connect(path)
        db.migrate(self.conn)
        seed.seed_reference(self.conn, self.cfg)
        self.u = {r["username"]: r for r in self.conn.execute("SELECT * FROM users")}
        q = lambda sql: self.conn.execute(sql).fetchone()["id"]
        self.supplier = q("SELECT id FROM parties WHERE kind='supplier'")
        self.customer = q("SELECT id FROM parties WHERE kind='customer'")
        self.dyer = q("SELECT id FROM parties WHERE kind='job_work'")
        self.item = q("SELECT id FROM items")
        self.raw, self.wip, self.fin, self.rej = (
            q(f"SELECT id FROM locations WHERE kind='{k}'") for k in ("raw_store", "wip", "finished_store", "reject_store"))

    def new_conn(self):
        return db.connect(self.path)

    def receive(self, lot_no="L1", qty="1000", user="store1"):
        rid = receipts.create_draft(self.conn, self.u[user], dict(
            supplier_id=self.supplier, item_id=self.item, lot_no=lot_no, location_id=self.raw, qty=qty, unit="m"))
        receipts.post(self.conn, self.u[user], rid)
        return rid

    def job(self, lot_no="L1", user="manager1", **kw):
        form = dict(lot_no=lot_no, process_type="dyeing", party_id=self.dyer)
        form.update(kw)
        return processing.create_job(self.conn, self.u[user], form)

    def issue(self, job_id, qty="500", user="store1"):
        iid = processing.create_issue_draft(self.conn, self.u[user], job_id, dict(qty=qty))
        processing.post_issue(self.conn, self.u[user], iid)
        return iid

    def result(self, issue_id, post=True, **q):
        form = dict(accepted="0", rejected="0", wastage="0", shrinkage="0", remaining="0")
        form.update(q)
        rid = processing.create_result_draft(self.conn, self.u["prod1"], issue_id, form)
        if post:
            processing.post_result(self.conn, self.u["prod1"], rid)
        return rid

    def finished_stock(self, accepted="462"):
        """Receive 1000, issue 500, accept `accepted` -> returns finished lot DEMO-F1-style number."""
        self.receive("L1", "1000")
        j = self.job("L1")
        i = self.issue(j, "500")
        self.result(i, accepted=accepted, rejected="0", wastage="0", shrinkage="0", remaining=str(500 - int(accepted)))
        return "L1-F1"

    def dispatch_draft(self, lot_no="L1-F1", qty="300", user="store1"):
        return dispatch.create_draft(self.conn, self.u[user], self.customer, [(lot_no, qty)])

    def on_hand(self, lot_no, kind):
        from wwtextile.services import inventory
        lot = inventory.get_lot(self.conn, lot_no)
        loc = {"raw": self.raw, "wip": self.wip, "fin": self.fin, "rej": self.rej}[kind]
        return inventory.on_hand(self.conn, lot["id"], loc)


@pytest.fixture
def world(tmp_path):
    return World(str(tmp_path / "t.db"))
