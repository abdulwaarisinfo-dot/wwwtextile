"""Service-level tests for Pass 2+: users, notifications, accessories, gate entries, finance, multihead."""
import sqlite3
import threading
from datetime import date, timedelta

import pytest

from wwtextile import db, money, security
from wwtextile.errors import BusinessRuleViolation, NotFound, PermissionDenied, ValidationFailed
from wwtextile.services import accessories as acc
from wwtextile.services import finance, gate_entries, inventory, master, multihead, notifications, users


def setting(w, key, value):
    w.conn.execute("INSERT INTO settings(key,value,updated_at) VALUES (?,?,'x') ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def acc_loc(w, code="ACC-MAIN"):
    return w.conn.execute("SELECT id FROM acc_locations WHERE code=?", (code,)).fetchone()["id"]


def mk_item(w, code="BTN-1", unit="pcs", decimals="0", reorder="", name="Button 18L"):
    return acc.create_item(w.conn, w.u["manager1"], dict(code=code, name=name, unit=unit, decimals=decimals, reorder_level=reorder))


def stock_acc(w, item_id, qty="100", loc=None):
    d = acc.create_draft(w.conn, w.u["store1"], "receipt", dict(supplier_id=w.supplier, to_location_id=loc or acc_loc(w)), [(item_id, qty)])
    acc.post(w.conn, w.u["store1"], d)
    return d


# =============================================================== users
class TestUsers:
    def form(self, **kw):
        f = dict(username="newuser", display_name="New User", role="storekeeper", password="Str0ng-pass-99")
        f.update(kw)
        return f

    def test_create_requires_admin(self, world):
        with pytest.raises(PermissionDenied):
            users.create_user(world.conn, world.u["manager1"], self.form())

    def test_create_ok_forces_password_change_and_hashes(self, world):
        uid = users.create_user(world.conn, world.u["admin"], self.form())
        row = world.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        assert row["must_change_password"] == 1 and row["password_hash"].startswith("scrypt$")
        assert "Str0ng-pass-99" not in row["password_hash"]
        assert security.verify_password("Str0ng-pass-99", row["password_hash"])

    @pytest.mark.parametrize("pw,frag", [("short", "at least"), ("newuser-password", "username"), ("demo-only-change-me", "common"), ("aaaaaaaaaaaa", "repetitive")])
    def test_password_policy(self, world, pw, frag):
        with pytest.raises(ValidationFailed) as e:
            users.create_user(world.conn, world.u["admin"], self.form(password=pw))
        assert frag in e.value.errors["password"]

    def test_duplicate_and_bad_username_and_role(self, world):
        with pytest.raises(ValidationFailed) as e:
            users.create_user(world.conn, world.u["admin"], self.form(username="admin"))
        assert "username" in e.value.errors
        with pytest.raises(ValidationFailed) as e:
            users.create_user(world.conn, world.u["admin"], self.form(username="Bad Name!", role="wizard"))
        assert {"username", "role"} <= set(e.value.errors)

    def test_last_admin_protected(self, world):
        admin = world.u["admin"]
        other = users.create_user(world.conn, admin, self.form(username="admin2", role="admin"))
        o = world.conn.execute("SELECT * FROM users WHERE id=?", (other,)).fetchone()
        users.set_active(world.conn, admin, other, False)          # fine: admin still active
        with pytest.raises(BusinessRuleViolation):
            users.set_active(world.conn, o, admin["id"], False)    # would need perm; o is inactive but row has admin role
        with pytest.raises(BusinessRuleViolation):
            users.set_role(world.conn, admin, admin["id"], "manager")   # self-demotion blocked

    def test_deactivate_kills_sessions_and_blocks_login(self, world):
        uid = world.u["store1"]["id"]
        token, _csrf, _u = security.login(world.conn, world.cfg, "store1", "test-pass-1")
        assert security.session_user(world.conn, token)[0] is not None
        users.set_active(world.conn, world.u["admin"], uid, False)
        assert security.session_user(world.conn, token)[0] is None
        with pytest.raises(ValueError):
            security.login(world.conn, world.cfg, "store1", "test-pass-1")

    def test_role_change_signs_user_out(self, world):
        token, _c, _u = security.login(world.conn, world.cfg, "store1", "test-pass-1")
        users.set_role(world.conn, world.u["admin"], world.u["store1"]["id"], "gate")
        assert security.session_user(world.conn, token)[0] is None

    def test_reset_password(self, world):
        users.reset_password(world.conn, world.u["admin"], world.u["store1"]["id"], "Another-Str0ng-1")
        row = world.conn.execute("SELECT * FROM users WHERE username='store1'").fetchone()
        assert row["must_change_password"] == 1
        token, _c, u = security.login(world.conn, world.cfg, "store1", "Another-Str0ng-1")
        assert u["must_change_password"] == 1

    def test_change_password(self, world):
        store = world.u["store1"]
        t1, _c, _u = security.login(world.conn, world.cfg, "store1", "test-pass-1")
        t2, _c, _u = security.login(world.conn, world.cfg, "store1", "test-pass-1")
        with pytest.raises(ValidationFailed) as e:
            users.change_password(world.conn, store, "wrong", "Brand-new-pw-12", "Brand-new-pw-12", t1)
        assert "current" in e.value.errors
        with pytest.raises(ValidationFailed) as e:
            users.change_password(world.conn, store, "test-pass-1", "Brand-new-pw-12", "different", t1)
        assert "confirm" in e.value.errors
        users.change_password(world.conn, store, "test-pass-1", "Brand-new-pw-12", "Brand-new-pw-12", t1)
        assert security.session_user(world.conn, t1)[0] is not None      # current session kept
        assert security.session_user(world.conn, t2)[0] is None          # others revoked
        with pytest.raises(ValueError):
            security.login(world.conn, world.cfg, "store1", "test-pass-1")


# =============================================================== notifications
class TestNotifications:
    def test_role_targeting_dedupe_and_read_state(self, world):
        c = world.conn
        with db.transaction(c):
            assert notifications.notify(c, title="Hello", role="manager", dedupe_key="k1") == 1
            assert notifications.notify(c, title="Hello again", role="manager", dedupe_key="k1") == 0   # deduped
            notifications.notify(c, title="For store", user_id=world.u["store1"]["id"])
        m1 = world.u["manager1"]
        assert notifications.unread_count(c, m1) == 1
        assert notifications.unread_count(c, world.u["store1"]) == 1
        assert notifications.unread_count(c, world.u["prod1"]) == 0
        nid = notifications.list_for(c, m1)[0]["id"]
        notifications.mark_read(c, m1, nid)
        assert notifications.unread_count(c, m1) == 0
        assert notifications.unread_count(c, world.u["store1"]) == 1        # read state is per user

    def test_cannot_mark_someone_elses_notification(self, world):
        c = world.conn
        with db.transaction(c):
            notifications.notify(c, title="Mgr only", role="manager")
        nid = c.execute("SELECT id FROM notifications").fetchone()["id"]
        notifications.mark_read(c, world.u["prod1"], nid)
        assert c.execute("SELECT COUNT(*) AS c FROM notification_reads").fetchone()["c"] == 0


# =============================================================== accessories
class TestAccessories:
    def test_item_validation_and_permissions(self, world):
        with pytest.raises(PermissionDenied):
            acc.create_item(world.conn, world.u["store1"], dict(code="X", name="Thing", unit="pcs"))
        with pytest.raises(ValidationFailed) as e:
            acc.create_item(world.conn, world.u["manager1"], dict(code="bad code", name="x", unit=""))
        assert {"code", "name", "unit"} <= set(e.value.errors)
        mk_item(world)
        with pytest.raises(ValidationFailed) as e:
            mk_item(world)
        assert "code" in e.value.errors

    def test_draft_does_not_touch_stock_post_does(self, world):
        i = mk_item(world)
        d = acc.create_draft(world.conn, world.u["store1"], "receipt", dict(supplier_id=world.supplier, to_location_id=acc_loc(world)), [(i, "100")])
        assert acc.item_total(world.conn, i) == 0
        acc.post(world.conn, world.u["store1"], d)
        assert acc.item_total(world.conn, i) == 100000
        with pytest.raises(BusinessRuleViolation):
            acc.post(world.conn, world.u["store1"], d)           # no double posting
        assert acc.item_total(world.conn, i) == 100000

    def test_whole_number_items_reject_fractions_decimal_items_allow(self, world):
        i = mk_item(world)
        with pytest.raises(ValidationFailed) as e:
            acc.create_draft(world.conn, world.u["store1"], "receipt", dict(supplier_id=world.supplier, to_location_id=acc_loc(world)), [(i, "1.5")])
        assert "whole" in e.value.errors["line_1"]
        k = mk_item(world, "THR-1", "kg", "3", name="Thread")
        stock_acc(world, k, "2.5")
        assert acc.item_total(world.conn, k) == 2500

    def test_duplicate_lines_and_empty_rejected(self, world):
        i = mk_item(world)
        with pytest.raises(ValidationFailed):
            acc.create_draft(world.conn, world.u["store1"], "receipt", dict(supplier_id=world.supplier, to_location_id=acc_loc(world)), [(i, "5"), (i, "6")])
        with pytest.raises(ValidationFailed) as e:
            acc.create_draft(world.conn, world.u["store1"], "receipt", dict(supplier_id=world.supplier, to_location_id=acc_loc(world)), [])
        assert "lines" in e.value.errors

    def test_issue_needs_reference_and_stock(self, world):
        i = mk_item(world)
        stock_acc(world, i, "100")
        loc = acc_loc(world)
        with pytest.raises(ValidationFailed) as e:
            acc.create_draft(world.conn, world.u["store1"], "issue", dict(from_location_id=loc), [(i, "10")])
        assert "ref_type" in e.value.errors
        d = acc.create_draft(world.conn, world.u["store1"], "issue", dict(from_location_id=loc, ref_type="other", ref_no="Stitching dept"), [(i, "101")])
        with pytest.raises(BusinessRuleViolation) as e:
            acc.post(world.conn, world.u["store1"], d)
        assert "Insufficient" in str(e.value)
        assert acc.item_total(world.conn, i) == 100000       # nothing partially posted

    def test_issue_reference_must_exist(self, world):
        i = mk_item(world)
        stock_acc(world, i)
        with pytest.raises(ValidationFailed) as e:
            acc.create_draft(world.conn, world.u["store1"], "issue", dict(from_location_id=acc_loc(world), ref_type="processing_job", ref_no="JOB-99999"), [(i, "1")])
        assert "ref_no" in e.value.errors
        world.receive("L1")
        j = world.job("L1")
        job_no = world.conn.execute("SELECT job_no FROM processing_jobs WHERE id=?", (j,)).fetchone()["job_no"]
        d = acc.create_draft(world.conn, world.u["store1"], "issue", dict(from_location_id=acc_loc(world), ref_type="processing_job", ref_no=job_no), [(i, "7")])
        acc.post(world.conn, world.u["store1"], d)
        cons = acc.consumption_by_reference(world.conn, "processing_job", job_no)
        assert [(r["code"], r["qty"]) for r in cons] == [("BTN-1", 7000)]

    def test_transfer_conserves_total(self, world):
        i = mk_item(world)
        stock_acc(world, i, "100")
        l2 = acc.create_location(world.conn, world.u["manager1"], dict(code="FLOOR", name="Production Floor"))
        d = acc.create_draft(world.conn, world.u["store1"], "transfer", dict(from_location_id=acc_loc(world), to_location_id=l2), [(i, "40")])
        acc.post(world.conn, world.u["store1"], d)
        assert acc.on_hand(world.conn, i, acc_loc(world)) == 60000 and acc.on_hand(world.conn, i, l2) == 40000
        assert acc.item_total(world.conn, i) == 100000
        with pytest.raises(ValidationFailed):
            acc.create_draft(world.conn, world.u["store1"], "transfer", dict(from_location_id=l2, to_location_id=l2), [(i, "1")])

    def test_reverse_restores_and_blocks_when_consumed(self, world):
        i = mk_item(world)
        r = stock_acc(world, i, "100")
        iss = acc.create_draft(world.conn, world.u["store1"], "issue", dict(from_location_id=acc_loc(world), ref_type="other", ref_no="Cutting dept"), [(i, "30")])
        acc.post(world.conn, world.u["store1"], iss)
        with pytest.raises(BusinessRuleViolation) as e:
            acc.reverse(world.conn, world.u["manager1"], r, "wrong supplier")      # 70 left, 100 needed
        assert "no longer has" in str(e.value)
        acc.reverse(world.conn, world.u["manager1"], iss, "issued by mistake")
        assert acc.item_total(world.conn, i) == 100000
        acc.reverse(world.conn, world.u["manager1"], r, "wrong supplier")
        assert acc.item_total(world.conn, i) == 0
        with pytest.raises(BusinessRuleViolation):
            acc.reverse(world.conn, world.u["manager1"], r, "twice please")
        with pytest.raises(ValidationFailed):
            acc.reverse(world.conn, world.u["manager1"], iss, "x")

    def test_low_stock_boundary(self, world):
        i = mk_item(world, reorder="50")
        stock_acc(world, i, "51")
        assert acc.low_stock(world.conn) == []
        d = acc.create_draft(world.conn, world.u["store1"], "issue", dict(from_location_id=acc_loc(world), ref_type="other", ref_no="Sewing"), [(i, "1")])
        acc.post(world.conn, world.u["store1"], d)                        # 50 == reorder level -> low
        assert [r["item"]["code"] for r in acc.low_stock(world.conn)] == ["BTN-1"]

    def test_maker_checker_roles(self, world):
        i = mk_item(world)
        stock_acc(world, i)
        d = acc.create_draft(world.conn, world.u["prod1"], "issue", dict(from_location_id=acc_loc(world), ref_type="other", ref_no="Line 1"), [(i, "5")])
        with pytest.raises(PermissionDenied):
            acc.post(world.conn, world.u["prod1"], d)
        acc.post(world.conn, world.u["store1"], d)
        with pytest.raises(PermissionDenied):
            acc.reverse(world.conn, world.u["store1"], d, "storekeeper cannot reverse")

    def test_ledger_is_append_only_and_never_negative(self, world):
        i = mk_item(world)
        stock_acc(world, i, "10")
        with pytest.raises(sqlite3.DatabaseError):
            world.conn.execute("UPDATE acc_ledger SET qty=999")
        with pytest.raises(sqlite3.DatabaseError):
            world.conn.execute("DELETE FROM acc_ledger")
        with pytest.raises(sqlite3.DatabaseError):
            world.conn.execute("INSERT INTO acc_ledger(posted_at,item_id,location_id,qty,movement,source_type,source_id,line_key) VALUES ('x',?,?,-11000,'t','t',1,'k')", (i, acc_loc(world)))

    def test_concurrent_issues_cannot_overspend(self, world):
        i = mk_item(world)
        stock_acc(world, i, "100")
        docs = [acc.create_draft(world.conn, world.u["store1"], "issue", dict(from_location_id=acc_loc(world), ref_type="other", ref_no=f"Dept {n}"), [(i, "60")]) for n in range(2)]
        results, barrier = [], threading.Barrier(2)

        def run(doc):
            c = world.new_conn()
            u = c.execute("SELECT * FROM users WHERE username='store1'").fetchone()
            barrier.wait()
            try:
                acc.post(c, u, doc)
                results.append("ok")
            except BusinessRuleViolation:
                results.append("blocked")
            finally:
                c.close()

        ts = [threading.Thread(target=run, args=(d,)) for d in docs]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert sorted(results) == ["blocked", "ok"]
        assert acc.item_total(world.conn, i) == 40000

    def test_item_with_stock_cannot_be_deactivated(self, world):
        i = mk_item(world)
        stock_acc(world, i)
        with pytest.raises(BusinessRuleViolation):
            acc.update_item(world.conn, world.u["manager1"], i, dict(name="Button 18L", active="0"))


# =============================================================== gate entries
class TestGateEntries:
    def form(self, **kw):
        f = dict(direction="inward", vehicle_no="lea-1234", driver_name="Test Driver", party_id="", party_text="Some Mill", material="Greige rolls")
        f.update(kw)
        return f

    def counts(self, w):
        return (w.conn.execute("SELECT COUNT(*) AS c FROM stock_ledger").fetchone()["c"], w.conn.execute("SELECT COUNT(*) AS c FROM acc_ledger").fetchone()["c"])

    def test_inward_lifecycle_never_touches_stock(self, world):
        before = self.counts(world)
        g = gate_entries.create(world.conn, world.u["gate1"], self.form())
        row = gate_entries.get(world.conn, g)
        assert row["status"] == "inside" and row["vehicle_no"] == "LEA-1234"
        with pytest.raises(BusinessRuleViolation):
            gate_entries.create(world.conn, world.u["gate1"], self.form())       # same vehicle already inside
        gate_entries.confirm_exit(world.conn, world.u["gate1"], g, "left")
        assert gate_entries.get(world.conn, g)["status"] == "exited"
        with pytest.raises(BusinessRuleViolation):
            gate_entries.confirm_exit(world.conn, world.u["gate1"], g)
        assert self.counts(world) == before
        gate_entries.create(world.conn, world.u["gate1"], self.form())             # vehicle may re-enter after exit

    def test_outward_needs_separate_approver_before_exit(self, world):
        g = gate_entries.create(world.conn, world.u["gate1"], self.form(direction="outward"))
        assert gate_entries.get(world.conn, g)["status"] == "awaiting_approval"
        with pytest.raises(BusinessRuleViolation) as e:
            gate_entries.confirm_exit(world.conn, world.u["gate1"], g)
        assert "approved" in str(e.value)
        with pytest.raises(PermissionDenied):
            gate_entries.approve(world.conn, world.u["gate1"], g)
        gate_entries.approve(world.conn, world.u["manager1"], g)
        gate_entries.confirm_exit(world.conn, world.u["gate1"], g)
        assert gate_entries.get(world.conn, g)["status"] == "exited"

    def test_creator_cannot_approve_own_entry(self, world):
        g = gate_entries.create(world.conn, world.u["admin"], self.form(direction="outward"))
        with pytest.raises(BusinessRuleViolation):
            gate_entries.approve(world.conn, world.u["admin"], g)
        setting(world, "gate.require_separate_approver", "0")
        gate_entries.approve(world.conn, world.u["admin"], g)

    def test_approval_setting_off_goes_straight_to_pending_exit(self, world):
        setting(world, "gate.outward_requires_approval", "0")
        g = gate_entries.create(world.conn, world.u["gate1"], self.form(direction="outward"))
        assert gate_entries.get(world.conn, g)["status"] == "pending_exit"

    def test_validation_and_references(self, world):
        with pytest.raises(ValidationFailed) as e:
            gate_entries.create(world.conn, world.u["gate1"], self.form(vehicle_no="", driver_name="", material="", party_text="", direction="sideways"))
        assert {"vehicle_no", "driver_name", "material", "party_id", "direction"} <= set(e.value.errors)
        with pytest.raises(ValidationFailed) as e:
            gate_entries.create(world.conn, world.u["gate1"], self.form(ref_type="goods_receipt", ref_no="GRN-99999"))
        assert "ref_no" in e.value.errors
        with pytest.raises(ValidationFailed) as e:
            gate_entries.create(world.conn, world.u["gate1"], self.form(declared_qty="10"))
        assert "declared_unit" in e.value.errors
        rid = world.receive("L1")
        grn = world.conn.execute("SELECT doc_no FROM goods_receipts WHERE id=?", (rid,)).fetchone()["doc_no"]
        g = gate_entries.create(world.conn, world.u["gate1"], self.form(ref_type="goods_receipt", ref_no=grn, declared_qty="1000", declared_unit="m"))
        assert gate_entries.ref_url(world.conn, "goods_receipt", grn) == f"/fabrics/receipts/{rid}"
        assert gate_entries.get(world.conn, g)["declared_qty"] == 1000000

    def test_cancel_requires_permission_and_reason(self, world):
        g = gate_entries.create(world.conn, world.u["gate1"], self.form())
        with pytest.raises(PermissionDenied):
            gate_entries.cancel(world.conn, world.u["gate1"], g, "entered by mistake")
        with pytest.raises(ValidationFailed):
            gate_entries.cancel(world.conn, world.u["manager1"], g, "no")
        gate_entries.cancel(world.conn, world.u["manager1"], g, "entered by mistake")
        with pytest.raises(BusinessRuleViolation):
            gate_entries.confirm_exit(world.conn, world.u["gate1"], g)

    def test_register_includes_dispatch_passes(self, world):
        world.finished_stock("462")
        did = world.dispatch_draft("L1-F1", "100")
        from wwtextile.services import dispatch
        dispatch.approve(world.conn, world.u["manager1"], did)
        pn = dispatch.issue_gate_pass(world.conn, world.u["gate1"], did, "LEB-1", "Driver One")
        gate_entries.create(world.conn, world.u["gate1"], self.form())
        reg = gate_entries.register(world.conn)
        assert {r["kind"] for r in reg} == {"entry", "dispatch_pass"}
        assert [r["no"] for r in gate_entries.register(world.conn, q="LEB-1")] == [pn]
        assert [r["kind"] for r in gate_entries.register(world.conn, direction="inward")] == ["entry"]


# =============================================================== finance
class TestMoney:
    @pytest.mark.parametrize("text,paise", [("1", 100), ("1,250.50", 125050), ("0.01", 1), (" 99.9 ", 9990)])
    def test_parse_ok(self, text, paise):
        assert money.parse_money(text) == paise

    @pytest.mark.parametrize("text", ["", "0", "-5", "1.005", "abc", "NaN", "Infinity", "1e30", None])
    def test_parse_rejects(self, text):
        with pytest.raises(money.MoneyError):
            money.parse_money(text)

    def test_format(self):
        assert money.fmt_money(125050, "PKR") == "PKR 1,250.50" and money.fmt_money(-5) == "-0.05"


class TestFinance:
    def entry(self, w, kind, amount, party=None, user="fin1", poster="manager1", post=True, **kw):
        form = dict(party_id=party or "", amount=amount, entry_date="2026-01-10", method="bank", category="Utilities")
        form.update(kw)
        eid = finance.create_draft(w.conn, w.u[user], kind, form, "PKR")
        if post:
            finance.post(w.conn, w.u[poster], eid)
        return eid

    def test_draft_not_counted_until_posted(self, world):
        self.entry(world, "bill", "1000", world.supplier, post=False)
        assert finance.party_balance(world.conn, world.supplier)["balance"] == 0
        self.entry(world, "bill", "500", world.supplier)
        assert finance.party_balance(world.conn, world.supplier)["balance"] == 50000

    def test_separate_poster_enforced_and_configurable(self, world):
        eid = self.entry(world, "expense", "100", post=False)
        with pytest.raises(PermissionDenied):
            finance.post(world.conn, world.u["fin1"], eid)                    # role lacks fin.post
        e2 = finance.create_draft(world.conn, world.u["manager1"] if False else world.u["admin"], "expense", dict(amount="5", entry_date="2026-01-01", category="Misc"), "PKR")
        with pytest.raises(BusinessRuleViolation):
            finance.post(world.conn, world.u["admin"], e2)                    # creator == poster
        setting(world, "finance.require_separate_poster", "0")
        finance.post(world.conn, world.u["admin"], e2)

    def test_balances_payments_and_allocation(self, world):
        b1 = self.entry(world, "bill", "1000", world.supplier)
        b2 = self.entry(world, "bill", "500", world.supplier)
        p = self.entry(world, "payment_out", "1200", world.supplier)
        assert finance.party_balance(world.conn, world.supplier)["balance"] == 30000
        finance.allocate(world.conn, world.u["manager1"], p, b1, "1000")
        finance.allocate(world.conn, world.u["manager1"], p, b2, "200")
        bills = {d["entry"]["id"]: d["outstanding"] for d in finance.open_documents(world.conn, world.supplier, "bill")}
        assert bills == {b2: 30000}
        with pytest.raises(BusinessRuleViolation):
            finance.allocate(world.conn, world.u["manager1"], p, b2, "1")     # payment fully allocated
        p2 = self.entry(world, "payment_out", "400", world.supplier)
        with pytest.raises(BusinessRuleViolation):
            finance.allocate(world.conn, world.u["manager1"], p2, b2, "301")  # exceeds outstanding
        with pytest.raises(BusinessRuleViolation):
            finance.allocate(world.conn, world.u["manager1"], p2, b1, "1")    # b1 fully settled

    def test_allocation_rules(self, world):
        other = world.conn.execute("INSERT INTO parties(kind,name) VALUES ('supplier','Other Supplier')").lastrowid
        bill = self.entry(world, "bill", "100", world.supplier)
        p_other = self.entry(world, "payment_out", "100", other)
        with pytest.raises(BusinessRuleViolation):
            finance.allocate(world.conn, world.u["manager1"], p_other, bill, "50")    # different parties
        inv = self.entry(world, "invoice", "100", world.customer)
        pay = self.entry(world, "payment_out", "100", world.supplier)
        with pytest.raises(BusinessRuleViolation):
            finance.allocate(world.conn, world.u["manager1"], pay, inv, "10")        # wrong pairing
        with pytest.raises(PermissionDenied):
            finance.allocate(world.conn, world.u["ceo"], pay, bill, "10")

    def test_void_rules(self, world):
        bill = self.entry(world, "bill", "100", world.supplier)
        pay = self.entry(world, "payment_out", "100", world.supplier)
        finance.allocate(world.conn, world.u["manager1"], pay, bill, "100")
        with pytest.raises(BusinessRuleViolation):
            finance.void(world.conn, world.u["manager1"], bill, "entered wrongly")    # has allocation
        finance.void(world.conn, world.u["manager1"], pay, "bounced cheque")
        assert finance.bill_outstanding(world.conn, finance.get(world.conn, bill)) == 10000   # allocation released
        assert finance.party_balance(world.conn, world.supplier)["balance"] == 10000
        finance.void(world.conn, world.u["manager1"], bill, "entered wrongly")
        assert finance.party_balance(world.conn, world.supplier)["balance"] == 0
        with pytest.raises(BusinessRuleViolation):
            finance.void(world.conn, world.u["manager1"], bill, "again please")

    def test_statement_matches_balance_and_opening(self, world):
        self.entry(world, "bill", "1000", world.supplier, entry_date="2026-01-05")
        self.entry(world, "payment_out", "300", world.supplier, entry_date="2026-02-05")
        self.entry(world, "bill", "50.25", world.supplier, entry_date="2026-03-05")
        full = finance.statement(world.conn, world.supplier)
        assert full["closing"] == finance.party_balance(world.conn, world.supplier)["balance"] == 75025
        assert [r["running"] for r in full["rows"]] == [100000, 70000, 75025]
        feb = finance.statement(world.conn, world.supplier, "2026-02-01", "2026-02-28")
        assert feb["opening"] == 100000 and feb["closing"] == 70000 and len(feb["rows"]) == 1

    def test_customer_side_and_party_kind_enforced(self, world):
        with pytest.raises(ValidationFailed) as e:
            self.entry(world, "bill", "10", world.customer)
        assert "party_id" in e.value.errors
        with pytest.raises(ValidationFailed):
            self.entry(world, "invoice", "10", world.supplier)
        self.entry(world, "invoice", "900", world.customer)
        self.entry(world, "payment_in", "400", world.customer)
        b = finance.party_balance(world.conn, world.customer)
        assert (b["direction"], b["balance"]) == ("receivable", 50000)

    def test_summary_totals_agree_with_party_balances(self, world):
        self.entry(world, "bill", "1000", world.supplier)
        self.entry(world, "payment_out", "250", world.supplier)
        self.entry(world, "invoice", "700", world.customer)
        self.entry(world, "expense", "40.50", category="Fuel")
        self.entry(world, "expense", "10", category="Fuel")
        s = finance.summary(world.conn)
        assert s["payables"] == 75000 and s["receivables"] == 70000
        assert s["expenses_total"] == 5050 and s["expenses"][0]["category"] == "Fuel"
        assert sum(p["balance"] for p in s["per_party"] if p["direction"] == "payable") == s["payables"]

    def test_posted_entries_are_immutable_and_undeletable(self, world):
        e = self.entry(world, "bill", "100", world.supplier)
        with pytest.raises(sqlite3.DatabaseError):
            world.conn.execute("UPDATE fin_entries SET amount_paise=1 WHERE id=?", (e,))
        with pytest.raises(sqlite3.DatabaseError):
            world.conn.execute("DELETE FROM fin_entries WHERE id=?", (e,))
        pay = self.entry(world, "payment_out", "100", world.supplier)
        finance.allocate(world.conn, world.u["manager1"], pay, e, "40")
        assert world.conn.execute("SELECT COUNT(*) AS c FROM fin_allocations").fetchone()["c"] == 1
        with pytest.raises(sqlite3.DatabaseError):
            world.conn.execute("DELETE FROM fin_allocations")
        with pytest.raises(sqlite3.DatabaseError):
            world.conn.execute("UPDATE fin_allocations SET amount_paise=1")

    def test_validation(self, world):
        with pytest.raises(ValidationFailed) as e:
            finance.create_draft(world.conn, world.u["fin1"], "payment_out", dict(party_id=world.supplier, amount="1.234", entry_date="2026-13-40"), "PKR")
        assert {"amount", "entry_date", "method"} <= set(e.value.errors)
        with pytest.raises(ValidationFailed) as e:
            finance.create_draft(world.conn, world.u["fin1"], "expense", dict(amount="5", entry_date="2026-01-01"), "PKR")
        assert "category" in e.value.errors
        with pytest.raises(ValidationFailed) as e:
            finance.create_draft(world.conn, world.u["fin1"], "bill", dict(party_id=world.supplier, amount="5", entry_date="2026-01-01", doc_ref_type="goods_receipt", doc_ref_no="GRN-99999"), "PKR")
        assert "doc_ref_no" in e.value.errors
        with pytest.raises(PermissionDenied):
            finance.create_draft(world.conn, world.u["store1"], "expense", dict(amount="5", entry_date="2026-01-01", category="Misc"), "PKR")

    def test_currency_mixing_blocked(self, world):
        self.entry(world, "expense", "5")
        with pytest.raises(BusinessRuleViolation):
            finance.create_draft(world.conn, world.u["fin1"], "expense", dict(amount="5", entry_date="2026-01-01", category="Misc"), "USD")

    def test_finance_never_changes_stock(self, world):
        world.receive("L1")
        before = world.conn.execute("SELECT COUNT(*) AS c FROM stock_ledger").fetchone()["c"]
        self.entry(world, "bill", "5000", world.supplier, doc_ref_type="goods_receipt", doc_ref_no="GRN-00001")
        assert world.conn.execute("SELECT COUNT(*) AS c FROM stock_ledger").fetchone()["c"] == before


# =============================================================== multihead
class TestMultihead:
    def order(self, w, unit="pcs", **kw):
        setting(w, "multihead.units", "pcs:0, m:3")
        f = dict(title="Logo embroidery", unit=unit)
        f.update(kw)
        return multihead.create_order(w.conn, w.u["prod1"], f)

    def test_unit_is_never_assumed(self, world):
        with pytest.raises(BusinessRuleViolation) as e:
            multihead.create_order(world.conn, world.u["prod1"], dict(title="x", unit="pcs"))
        assert "not assumed" in str(e.value)

    def test_unit_validation_and_decimals(self, world):
        with pytest.raises(ValidationFailed) as e:
            self.order(world, unit="stitches")
        assert "unit" in e.value.errors
        with pytest.raises(ValidationFailed) as e:
            self.order(world, planned_qty="10.5")
        assert "planned_qty" in e.value.errors
        o = self.order(world, unit="m", planned_qty="10.5")
        assert multihead.get_order(world.conn, o)["planned_qty"] == 10500

    def test_permissions(self, world):
        setting(world, "multihead.units", "pcs")
        with pytest.raises(PermissionDenied):
            multihead.create_order(world.conn, world.u["store1"], dict(title="x", unit="pcs"))

    def test_logging_totals_void_and_close(self, world):
        o = self.order(world, planned_qty="100")
        multihead.log_production(world.conn, world.u["prod1"], o, dict(good_qty="40", rejected_qty="3"))
        l2 = multihead.log_production(world.conn, world.u["prod1"], o, dict(good_qty="10"))
        t = multihead.order_totals(world.conn, o)
        assert (t["good"], t["rejected"], t["progress_pct"]) == (50000, 3000, 50)
        multihead.void_log(world.conn, world.u["prod1"], l2, "typed wrong order")
        assert multihead.order_totals(world.conn, o)["good"] == 40000
        with pytest.raises(sqlite3.DatabaseError):
            world.conn.execute("UPDATE mh_logs SET good_qty=1 WHERE id=?", (l2,))
        with pytest.raises(ValidationFailed):
            multihead.log_production(world.conn, world.u["prod1"], o, dict(good_qty="0", rejected_qty="0"))
        with pytest.raises(ValidationFailed) as e:
            multihead.log_production(world.conn, world.u["prod1"], o, dict(good_qty="1", log_date=(date.today() + timedelta(days=2)).isoformat()))
        assert "log_date" in e.value.errors
        multihead.close_order(world.conn, world.u["prod1"], o, "completed", "")
        with pytest.raises(BusinessRuleViolation):
            multihead.log_production(world.conn, world.u["prod1"], o, dict(good_qty="1"))

    def test_completion_requires_a_log_and_cancel_a_reason(self, world):
        o = self.order(world)
        with pytest.raises(BusinessRuleViolation):
            multihead.close_order(world.conn, world.u["prod1"], o, "completed", "")
        with pytest.raises(ValidationFailed):
            multihead.close_order(world.conn, world.u["prod1"], o, "cancelled", "")
        multihead.close_order(world.conn, world.u["prod1"], o, "cancelled", "customer cancelled")

    def test_fabric_consumption_record_only_default(self, world):
        world.receive("L1", "100")
        o = self.order(world)
        before = world.on_hand("L1", "raw")
        c = multihead.consume_fabric(world.conn, world.u["prod1"], o, dict(lot_no="L1", location_id=world.raw, qty="30", waste_qty="2"))
        assert world.on_hand("L1", "raw") == before          # no stock effect unless the owner chooses it
        assert multihead.order_consumptions(world.conn, o)[0]["stock_effect"] == "record_only"
        assert multihead.order_totals(world.conn, o)["fabric_consumed"] == 30000

    def test_fabric_consumption_posted_when_configured(self, world):
        world.receive("L1", "100")
        setting(world, "multihead.fabric_stock_effect", "consume")
        o = self.order(world)
        c = multihead.consume_fabric(world.conn, world.u["prod1"], o, dict(lot_no="L1", location_id=world.raw, qty="30", waste_qty="2"))
        assert world.on_hand("L1", "raw") == 70000
        with pytest.raises(BusinessRuleViolation) as e:
            multihead.consume_fabric(world.conn, world.u["prod1"], o, dict(lot_no="L1", location_id=world.raw, qty="71"))
        assert "available" in str(e.value)
        multihead.void_consumption(world.conn, world.u["prod1"], c, "wrong lot used")
        assert world.on_hand("L1", "raw") == 100000
        assert multihead.order_totals(world.conn, o)["fabric_consumed"] == 0
        with pytest.raises(BusinessRuleViolation):
            multihead.void_consumption(world.conn, world.u["prod1"], c, "voiding twice")

    def test_posted_consumption_respects_reservations(self, world):
        world.finished_stock("462")                                    # 462 in finished store lot L1-F1
        world.dispatch_draft("L1-F1", "400")
        from wwtextile.services import dispatch
        dispatch.approve(world.conn, world.u["manager1"], 1)           # reserves 400
        setting(world, "multihead.fabric_stock_effect", "consume")
        o = self.order(world)
        with pytest.raises(BusinessRuleViolation):
            multihead.consume_fabric(world.conn, world.u["prod1"], o, dict(lot_no="L1-F1", location_id=world.fin, qty="100"))
        multihead.consume_fabric(world.conn, world.u["prod1"], o, dict(lot_no="L1-F1", location_id=world.fin, qty="62"))

    def test_waste_cannot_exceed_quantity_and_bad_lot(self, world):
        world.receive("L1", "100")
        o = self.order(world)
        with pytest.raises(ValidationFailed) as e:
            multihead.consume_fabric(world.conn, world.u["prod1"], o, dict(lot_no="NOPE", location_id=world.raw, qty="10", waste_qty="11"))
        assert {"lot_no", "waste_qty"} <= set(e.value.errors)
        with pytest.raises(ValidationFailed) as e:
            multihead.consume_fabric(world.conn, world.u["prod1"], o, dict(lot_no="L1", location_id=world.wip, qty="10"))
        assert "location_id" in e.value.errors

    def test_accessory_issue_must_reference_open_order(self, world):
        o = self.order(world)
        no = multihead.get_order(world.conn, o)["order_no"]
        i = mk_item(world)
        stock_acc(world, i)
        d = acc.create_draft(world.conn, world.u["store1"], "issue", dict(from_location_id=acc_loc(world), ref_type="multihead_order", ref_no=no), [(i, "9")])
        acc.post(world.conn, world.u["store1"], d)
        assert [r["qty"] for r in acc.consumption_by_reference(world.conn, "multihead_order", no)] == [9000]
        assert len(multihead.linked_accessory_docs(world.conn, no)) == 1
        multihead.log_production(world.conn, world.u["prod1"], o, dict(good_qty="1"))
        multihead.close_order(world.conn, world.u["prod1"], o, "completed", "")
        with pytest.raises(ValidationFailed):
            acc.create_draft(world.conn, world.u["store1"], "issue", dict(from_location_id=acc_loc(world), ref_type="multihead_order", ref_no=no), [(i, "1")])

    def test_machines(self, world):
        m = multihead.create_machine(world.conn, world.u["manager1"], dict(code="mh-01", name="Head machine 1"))
        with pytest.raises(ValidationFailed):
            multihead.create_machine(world.conn, world.u["manager1"], dict(code="MH-01", name="dup"))
        with pytest.raises(PermissionDenied):
            multihead.create_machine(world.conn, world.u["prod1"], dict(code="MH-02", name="x"))
        multihead.set_machine_active(world.conn, world.u["manager1"], m, False)
        setting(world, "multihead.units", "pcs")
        with pytest.raises(ValidationFailed):
            multihead.create_order(world.conn, world.u["prod1"], dict(title="t", unit="pcs", machine_id=str(m)))


# =============================================================== master data
class TestMaster:
    def test_party_and_item_creation(self, world):
        pid = master.create_party(world.conn, world.u["manager1"], dict(kind="supplier", name="  New   Mills "))
        assert world.conn.execute("SELECT name FROM parties WHERE id=?", (pid,)).fetchone()["name"] == "New Mills"
        with pytest.raises(ValidationFailed):
            master.create_party(world.conn, world.u["manager1"], dict(kind="supplier", name="new mills"))
        with pytest.raises(PermissionDenied):
            master.create_party(world.conn, world.u["store1"], dict(kind="supplier", name="Nope Ltd"))
        master.create_item(world.conn, world.u["manager1"], dict(code="FAB-LAWN", name="Lawn 44in", unit="m"))
        with pytest.raises(ValidationFailed):
            master.create_item(world.conn, world.u["manager1"], dict(code="fab-lawn", name="Dup", unit="m"))
