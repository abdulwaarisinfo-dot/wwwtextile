"""HTTP-level tests: authentication, CSRF, server-side authorisation, and the whole DEMO-104
workflow driven through the real forms and routes (not by calling services directly)."""
import dataclasses
import re

import pytest
from starlette.testclient import TestClient

from wwtextile import config, db, seed
from wwtextile.services import inventory
from wwtextile.web import create_app

PW = "test-pass-1"


@pytest.fixture
def env(tmp_path):
    cfg = dataclasses.replace(config.load(), env="demo", db_path=str(tmp_path / "w.db"), demo_password=PW,
                              max_failed_logins=3, lockout_minutes=10)
    app = create_app(cfg)
    conn = db.connect(cfg.db_path)
    seed.seed_reference(conn, cfg)
    conn.close()
    return cfg, app


class Client:
    def __init__(self, app, cfg):
        self.c = TestClient(app, follow_redirects=False)
        self.cfg = cfg
        self.csrf = None

    def login(self, username, password=PW):
        r = self.c.get("/login")
        tok = re.search(r'name="lcsrf" value="([^"]+)"', r.text).group(1)
        r = self.c.post("/login", data={"username": username, "password": password, "lcsrf": tok, "next": "/"})
        if r.status_code == 303:
            self.csrf = re.search(r'name="csrf" value="([^"]+)"', self.c.get("/").text).group(1)
        return r

    def get(self, path, **kw):
        return self.c.get(path, **kw)

    def post(self, path, data=None, csrf=True, **kw):
        d = dict(data or {})
        if csrf:
            d["csrf"] = self.csrf
        return self.c.post(path, data=d, **kw)

    def follow(self, r):
        assert r.status_code in (302, 303), (r.status_code, r.text[:300])
        return self.c.get(r.headers["location"])


@pytest.fixture
def as_user(env):
    cfg, app = env

    def make(username):
        cl = Client(app, cfg)
        assert cl.login(username).status_code == 303
        return cl
    return make


def ids(cfg):
    conn = db.connect(cfg.db_path)
    q = lambda sql: conn.execute(sql).fetchone()["id"]
    out = dict(supplier=q("SELECT id FROM parties WHERE kind='supplier'"), customer=q("SELECT id FROM parties WHERE kind='customer'"),
               dyer=q("SELECT id FROM parties WHERE kind='job_work'"), item=q("SELECT id FROM items"), raw=q("SELECT id FROM locations WHERE kind='raw_store'"))
    conn.close()
    return out


# ------------------------------------------------------------------ authentication
def test_anonymous_is_redirected_to_login(env):
    cfg, app = env
    r = TestClient(app, follow_redirects=False).get("/fabrics")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_login_logout_and_bad_password(env, as_user):
    cfg, app = env
    bad = Client(app, cfg).login("ceo", "wrong")
    assert bad.status_code == 401 and "Invalid username or password" in bad.text
    ghost = Client(app, cfg).login("nobody", "x")
    assert ghost.status_code == 401 and "Invalid username or password" in ghost.text   # same message: no user enumeration
    cl = as_user("ceo")
    assert cl.get("/").status_code == 200
    assert cl.post("/logout").status_code == 303
    assert cl.get("/").status_code == 303


def test_lockout_after_repeated_failures(env):
    cfg, app = env
    cl = Client(app, cfg)
    for _ in range(3):
        assert cl.login("ceo", "wrong").status_code == 401
    r = cl.login("ceo", PW)                      # correct password, but locked
    assert r.status_code == 401
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) c FROM audit_events WHERE action='auth.login' AND outcome IN ('failed','denied')").fetchone()["c"] >= 3


def test_passwords_are_hashed(env):
    cfg, _ = env
    conn = db.connect(cfg.db_path)
    for r in conn.execute("SELECT password_hash FROM users"):
        assert r["password_hash"].startswith("scrypt$") and PW not in r["password_hash"]


def test_login_requires_its_own_csrf_token(env):
    cfg, app = env
    r = TestClient(app, follow_redirects=False).post("/login", data={"username": "ceo", "password": PW, "lcsrf": "forged"})
    assert r.status_code == 403


def test_open_redirect_blocked_on_login(env):
    cfg, app = env
    c = TestClient(app, follow_redirects=False)
    tok = re.search(r'name="lcsrf" value="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/login", data={"username": "ceo", "password": PW, "lcsrf": tok, "next": "//evil.example"})
    assert r.status_code == 303 and r.headers["location"] == "/"


# ------------------------------------------------------------------ CSRF, headers, CSP
def test_post_without_csrf_is_rejected_and_audited(env, as_user):
    cfg, _ = env
    cl = as_user("store1")
    r = cl.post("/fabrics/receipts", {"lot_no": "X1"}, csrf=False)
    assert r.status_code == 403
    r = cl.post("/fabrics/receipts", {"lot_no": "X1", "csrf": "wrong"}, csrf=False)
    assert r.status_code == 403
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) c FROM audit_events WHERE action='security.csrf_rejected'").fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM goods_receipts").fetchone()["c"] == 0


def test_security_headers_and_no_inline_scripts_or_styles(env, as_user):
    cl = as_user("admin")
    for path in ["/", "/fabrics", "/processing/jobs/new", "/settings", "/ask", "/login"]:
        r = cl.get(path) if path != "/login" else TestClient(cl.c.app, follow_redirects=False).get(path)
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert "<script>" not in r.text and " onclick=" not in r.text and ' style="' not in r.text
    set_cookie = TestClient(cl.c.app, follow_redirects=False).get("/login").headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower() and "samesite=lax" in set_cookie.lower()


def test_output_is_html_escaped(env, as_user):
    cfg, _ = env
    cl = as_user("store1")
    i = ids(cfg)
    r = cl.post("/fabrics/receipts", dict(supplier_id=i["supplier"], item_id=i["item"], location_id=i["raw"], qty="5",
                                          lot_no="XSS1", unit="m", notes="<script>alert(1)</script>"))
    page = cl.follow(r).text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


# ------------------------------------------------------------------ authorisation (server-side)
@pytest.mark.parametrize("role_user,method,path", [
    ("gate1", "GET", "/fabrics"), ("gate1", "GET", "/ledger"), ("gate1", "GET", "/audit"), ("gate1", "GET", "/fabrics/receipts/new"),
    ("gate1", "GET", "/processing"), ("gate1", "GET", "/ask"), ("fin1", "GET", "/ledger"), ("fin1", "GET", "/audit"),
    ("prod1", "GET", "/dispatch"), ("prod1", "GET", "/fabrics/receipts/new"), ("store1", "GET", "/audit"), ("store1", "GET", "/settings"),
    ("ceo", "GET", "/fabrics/receipts/new"), ("ceo", "GET", "/dispatch/new"),
    ("store1", "POST", "/dispatch/1/approve"), ("store1", "POST", "/gate/passes/1/exit"), ("gate1", "POST", "/dispatch/1/approve"),
    ("prod1", "POST", "/fabrics/receipts/1/post"), ("manager1", "POST", "/settings"), ("ceo", "POST", "/fabrics/receipts"),
])
def test_forbidden_routes_return_403_and_are_audited(env, as_user, role_user, method, path):
    cfg, _ = env
    cl = as_user(role_user)
    r = cl.get(path) if method == "GET" else cl.post(path, {})
    assert r.status_code == 403
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) c FROM audit_events WHERE action='security.permission_denied' AND actor_name=?",
                        (role_user,)).fetchone()["c"] >= 1


def test_sidebar_hides_what_role_cannot_use(env, as_user):
    page = as_user("gate1").get("/").text
    assert "/gate" in page and "/ledger" not in page and "/audit" not in page and "/fabrics\"" not in page


# ------------------------------------------------------------------ the DEMO-104 workflow through the UI
def test_demo104_end_to_end_through_http(env, as_user):
    cfg, app = env
    i = ids(cfg)
    store, mgr, prod, gate, ceo = (as_user(u) for u in ("store1", "manager1", "prod1", "gate1", "ceo"))

    # receipt: save draft (no stock), then post
    r = store.post("/fabrics/receipts", dict(supplier_id=i["supplier"], item_id=i["item"], location_id=i["raw"], qty="1,000",
                                             lot_no="DEMO-104", unit="m", supplier_ref="CH-1"))
    assert r.status_code == 303 and r.headers["location"] == "/fabrics/receipts/1"
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) c FROM stock_ledger").fetchone()["c"] == 0
    assert store.post("/fabrics/receipts/1/post").status_code == 303
    assert conn.execute("SELECT SUM(qty) s FROM stock_ledger").fetchone()["s"] == 1_000_000
    # double-submit of the same post is rejected, no second ledger row
    again = store.post("/fabrics/receipts/1/post")
    assert again.status_code == 303 and conn.execute("SELECT COUNT(*) c FROM stock_ledger").fetchone()["c"] == 1
    assert "only drafts can be posted" in store.follow(again).text

    # processing job, issue 500, result with variance
    r = mgr.post("/processing/jobs", dict(lot_no="DEMO-104", process_type="dyeing", party_id=i["dyer"], expected_date="2030-01-01"))
    assert r.headers["location"] == "/processing/jobs/1"
    r = store.post("/processing/jobs/1/issues", dict(qty="500", unit="m"))
    assert r.status_code == 303
    assert store.post("/processing/issues/1/post").status_code == 303
    r = prod.post("/processing/issues/1/result", dict(accepted="462", rejected="12.5", wastage="8", shrinkage="15", remaining="0"))
    assert r.status_code == 303
    assert prod.post("/processing/results/1/post").status_code == 303
    job_page = ceo.get("/processing/jobs/1").text
    assert "exception" in job_page and "2.500" in job_page
    assert "DEMO-104-F1" in ceo.get("/fabrics").text

    # dispatch: same user cannot approve; manager can; gate pass; exit
    r = store.post("/dispatch", dict(customer_id=i["customer"], lot_1="DEMO-104-F1", qty_1="300"))
    assert r.headers["location"] == "/dispatch/1"
    assert mgr.post("/dispatch/1/approve").status_code == 303
    fin_lot = inventory.get_lot(conn, "DEMO-104-F1")
    fin_loc = conn.execute("SELECT id FROM locations WHERE kind='finished_store'").fetchone()["id"]
    assert inventory.on_hand(conn, fin_lot["id"], fin_loc) == 462_000 and inventory.available(conn, fin_lot["id"], fin_loc) == 162_000
    bad = gate.post("/dispatch/1/gate-pass", dict(vehicle_no="", driver_name=""))
    assert bad.status_code == 422 and "Vehicle registration is required" in bad.text
    assert gate.post("/dispatch/1/gate-pass", dict(vehicle_no="LEB-123", driver_name="Driver One")).status_code == 303
    assert "LEB-123" in gate.get("/gate?q=LEB").text
    assert "Outward Gate Pass" in gate.get("/gate/passes/1/print").text
    assert inventory.on_hand(conn, fin_lot["id"], fin_loc) == 462_000          # default policy: still on hand before exit
    assert gate.post("/gate/passes/1/exit", dict(notes="left")).status_code == 303
    assert inventory.on_hand(conn, fin_lot["id"], fin_loc) == 162_000
    assert gate.post("/gate/passes/1/exit").status_code == 303                  # replay: flash error, no second deduction
    assert conn.execute("SELECT COUNT(*) c FROM stock_ledger WHERE movement='dispatch_out'").fetchone()["c"] == 1

    # traceability, query, dashboard, export
    lot = ceo.get("/fabrics/lots/DEMO-104").text
    for needle in ["GRN-00001", "ISS-00001", "RES-00001", "DSP-00001", "GP-00001", "Open unexplained variance"]:
        assert needle in lot
    ans = ceo.get("/ask?q=Where+is+lot+DEMO-104-F1%3F").text
    assert "162.000" in ans and "/fabrics/lots/DEMO-104-F1" in ans and "not the AI Command Center" in ans
    dash = ceo.get("/").text
    assert "Reconciliation exceptions" in dash and "1" in dash
    csv_ = ceo.get("/ledger.csv?lot=DEMO-104")
    assert csv_.headers["content-type"].startswith("text/csv") and "dispatch_out" in csv_.text and "# Filters: lot=DEMO-104" in csv_.text
    exported_net = float(re.search(r"net qty of exported rows: (-?[\d.]+)", csv_.text).group(1))
    rows, count = inventory.ledger_rows(conn, lot_no="DEMO-104", limit=10000)
    assert exported_net == pytest.approx(sum(r["qty"] for r in rows) / 1000)    # export total == ledger total
    assert inventory.reconcile_ledger(conn) == []
    # audit shows the whole story with actors
    audit_page = as_user("admin").get("/audit?ref=DSP-00001").text
    assert "dispatch.approve" in audit_page and "manager1" in audit_page
    assert conn.execute("SELECT COUNT(*) c FROM audit_events WHERE action='export.ledger_csv'").fetchone()["c"] == 1


def test_form_validation_rerenders_with_values_and_errors(env, as_user):
    cfg, _ = env
    cl = as_user("store1")
    i = ids(cfg)
    r = cl.post("/fabrics/receipts", dict(supplier_id=i["supplier"], item_id=i["item"], location_id=i["raw"], qty="1.2345", lot_no="KEEP-1", unit="m"))
    assert r.status_code == 422 and "at most 3 decimal places" in r.text and 'value="KEEP-1"' in r.text
    r = cl.post("/fabrics/receipts", dict(supplier_id=i["supplier"], item_id=i["item"], location_id=i["raw"], qty="5", lot_no="U1", unit="yd"))
    assert r.status_code == 422 and "conversion" in r.text
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) c FROM goods_receipts").fetchone()["c"] == 0


def test_settings_change_is_admin_only_and_audited(env, as_user):
    cfg, _ = env
    admin = as_user("admin")
    assert admin.post("/settings", dict(deduction_event="gate_pass_issue", separate_approver="1")).status_code == 303
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT value FROM settings WHERE key='dispatch.deduction_event'").fetchone()["value"] == "gate_pass_issue"
    assert conn.execute("SELECT COUNT(*) c FROM audit_events WHERE action='settings.deduction_event'").fetchone()["c"] == 1


def test_no_placeholder_module_pages_remain(env, as_user):
    """Every formerly-planned module is real now; the old placeholder URLs must not pretend to be pages."""
    cl = as_user("ceo")
    for slug in ("multihead", "finance", "reports", "watchtower", "voice", "document-reader", "unknown"):
        assert cl.get(f"/modules/{slug}").status_code == 404
    assert "Not yet implemented" not in cl.get("/").text
    for path in ("/multihead", "/finance", "/reports", "/watchtower", "/assistant", "/documents", "/accessories", "/gate/entries"):
        assert cl.get(path).status_code == 200, path
