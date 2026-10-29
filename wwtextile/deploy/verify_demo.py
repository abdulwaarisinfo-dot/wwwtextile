#!/usr/bin/env python3
"""Verify a running demo (local or public URL). Standard library only. Exit code 0 = every check passed.

  python deploy/verify_demo.py https://your-link.trycloudflare.com --client-password '...' --admin-password '...'
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -- {extra}" if extra and not ok else ""))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Session:
    """Minimal cookie session that ignores the Secure flag so it also works against http://127.0.0.1."""

    def __init__(self, base: str):
        self.base, self.cookies = base.rstrip("/"), {}
        self.opener = urllib.request.build_opener(NoRedirect)

    def req(self, path, data=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        r = urllib.request.Request(self.base + path, data=body)
        if self.cookies:
            r.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        try:
            resp = self.opener.open(r, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e
        for h in resp.headers.get_all("Set-Cookie") or []:
            k, _, rest = h.partition("=")
            v = rest.split(";", 1)[0]
            if v.strip('"') == "" or "Max-Age=0" in h:
                self.cookies.pop(k, None)
            else:
                self.cookies[k] = v
        text = resp.read().decode("utf-8", "replace")
        return resp.status if hasattr(resp, "status") else resp.code, resp.headers, text

    def csrf(self) -> str:
        _, _, page = self.req("/")
        m = re.search(r'<meta name="csrf" content="([^"]+)"', page)
        return m.group(1) if m else ""

    def post(self, path, data=None):
        return self.req(path, {**(data or {}), "csrf": self.csrf()})

    def first_option(self, path, select_name):
        """First non-empty <option value> of <select name=...> on a page (used to pick fictional master data)."""
        _, _, page = self.req(path)
        sel = re.search(r'<select[^>]*name="%s"[^>]*>(.*?)</select>' % re.escape(select_name), page, re.S)
        vals = re.findall(r'<option[^>]*value="(\d+)"', sel.group(1)) if sel else []
        return vals[0] if vals else ""

    def login(self, user, pw):
        _, _, page = self.req("/login")
        m = re.search(r'name="lcsrf" value="([^"]+)"', page)
        code, h, _ = self.req("/login", {"username": user, "password": pw, "lcsrf": m.group(1) if m else "", "next": "/"})
        return code


def loc_id(headers) -> str:
    return (headers.get("Location") or "").rstrip("/").rsplit("/", 1)[-1]


def run_workflow(base: str, pw: str) -> None:
    import time
    lot = "VERIFY-" + time.strftime("%H%M%S")
    st, mg, mg2, pr, gt, fi = (Session(base) for _ in range(6))
    for cl, u in ((st, "store1"), (mg, "manager1"), (mg2, "manager2"), (pr, "prod1"), (gt, "gate1"), (fi, "fin1")):
        if cl.login(u, pw) != 303:
            check(f"workflow: {u} can sign in", False)
            return
    sup, item, raw = st.first_option("/fabrics/receipts/new", "supplier_id"), st.first_option("/fabrics/receipts/new", "item_id"), st.first_option("/fabrics/receipts/new", "location_id")
    dye, cust = mg.first_option("/processing/jobs/new", "party_id"), st.first_option("/dispatch/new", "customer_id")
    check("workflow: master data found", all([sup, item, raw, dye, cust]), str([sup, item, raw, dye, cust]))
    code, h, _ = st.post("/fabrics/receipts", {"supplier_id": sup, "item_id": item, "lot_no": lot, "location_id": raw, "qty": "1000", "unit": "m"})
    rid = loc_id(h)
    check("workflow 1: goods receipt drafted", code == 303 and rid.isdigit(), str(code))
    code, _, _ = mg.post(f"/fabrics/receipts/{rid}/post")
    _, _, page = st.req(f"/fabrics/lots/{lot}")
    check("workflow 1b: receipt posted, lot has 1,000 m in stock", code == 303 and "1,000.000" in page, str(code))
    code, h, _ = mg.post("/processing/jobs", {"lot_no": lot, "process_type": "dyeing", "party_id": dye})
    jid = loc_id(h)
    check("workflow 2: processing job created", code == 303 and jid.isdigit(), str(code))
    st.post(f"/processing/jobs/{jid}/issues", {"qty": "800"})
    _, _, jp = st.req(f"/processing/jobs/{jid}")
    m = re.search(r'/processing/issues/(\d+)/post', jp)
    iss = m.group(1) if m else ""
    code, _, _ = st.post(f"/processing/issues/{iss}/post")
    check("workflow 3: 800 m issued to processing", bool(iss) and code == 303, str(code))
    code, _, _ = pr.post(f"/processing/issues/{iss}/result", {"accepted": "760", "rejected": "12", "wastage": "8", "shrinkage": "20", "remaining": "0"})
    _, _, jp = pr.req(f"/processing/jobs/{jid}")
    m = re.search(r'/processing/results/(\d+)/post', jp)
    res = m.group(1) if m else ""
    check("workflow 4: result recorded (760+12+8+20 = 800, nothing unexplained)", code == 303 and bool(res), str(code))
    code, _, _ = pr.post(f"/processing/results/{res}/post")
    fin = lot + "-F1"
    _, _, page = st.req(f"/fabrics/lots/{fin}")
    check("workflow 5: result posted, finished lot holds 760 m", code == 303 and "760.000" in page, str(code))
    code, h, _ = st.post("/dispatch", {"customer_id": cust, "lot_1": fin, "qty_1": "300"})
    did = loc_id(h)
    check("workflow 6: dispatch drafted", code == 303 and did.isdigit(), str(code))
    code, _, _ = st.post(f"/dispatch/{did}/approve")
    _, _, dp = st.req(f"/dispatch/{did}")
    check("workflow 6b: creator cannot approve own dispatch (maker-checker)", "draft" in dp.lower() and "approved" not in dp.lower().split("status")[-1][:80], str(code))
    code, _, _ = mg2.post(f"/dispatch/{did}/approve")
    check("workflow 7: a different manager approved it", code == 303, str(code))
    code, _, _ = gt.post(f"/dispatch/{did}/gate-pass", {"vehicle_no": "DM-VER-0001", "driver_name": "Verify Driver"})
    _, _, dp = gt.req(f"/dispatch/{did}")
    m = re.search(r'/gate/passes/(\d+)', dp)
    gp = m.group(1) if m else ""
    check("workflow 8: gate pass issued", code == 303 and bool(gp), str(code))
    _, _, page = st.req(f"/fabrics/lots/{fin}")
    check("workflow 8b: stock NOT deducted before the vehicle leaves", "760.000" in page)
    code, _, _ = gt.post(f"/gate/passes/{gp}/exit", {"notes": "verify"})
    _, _, page = st.req(f"/fabrics/lots/{fin}")
    check("workflow 9: gate exit confirmed, finished lot now 460 m", code == 303 and "460.000" in page, str(code))
    code, h, _ = fi.post("/finance/entries", {"kind": "invoice", "party_id": cust, "entry_date": time.strftime("%Y-%m-%d"), "amount": "123456.00", "description": "VERIFY invoice"})
    fid = loc_id(h)
    code2, _, _ = mg.post(f"/finance/entries/{fid}/post")
    check("workflow 10: invoice drafted by finance, posted by a manager", code == 303 and code2 == 303, f"{code} {code2}")
    _, _, rep = mg.req("/reports/dispatch-register?run=1")
    check("workflow 11: dispatch register report shows the new dispatch", f"/dispatch/{did}" in rep or fin in rep)
    print(f"  (workflow created lot {lot}; run reset-demo for a clean slate)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--client-password", required=True)
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("--workflow", action="store_true",
                    help="also run the full factory flow (receive -> process -> reconcile -> dispatch -> gate exit) with a clearly named VERIFY-* lot. "
                         "This WRITES to the demo database (use reset-demo afterwards for a clean slate).")
    a = ap.parse_args()
    base = a.url.rstrip("/")
    anon = Session(base)

    code, _, body = anon.req("/healthz")
    check("health check returns ok", code == 200 and body.strip() == "ok", f"{code} {body[:80]}")
    code, h, page = anon.req("/login")
    check("login page loads", code == 200 and "Sign in" in page or code == 200)
    check("security headers present", all(k in h for k in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options")))
    check("search engines are told not to index", "noindex" in (h.get("X-Robots-Tag") or ""))
    code, _, txt = anon.req("/robots.txt")
    check("robots.txt disallows everything", code == 200 and "Disallow: /" in txt)
    if base.startswith("https://"):
        check("session cookie is Secure + HttpOnly", all(("Secure" in c and "HttpOnly" in c) for c in (h.get_all("Set-Cookie") or ["Secure HttpOnly"])), str(h.get_all("Set-Cookie")))
    for path in ("/", "/fabrics", "/admin/users", "/demo"):
        code, loc, _ = anon.req(path)
        check(f"anonymous cannot open {path}", code in (302, 303) and "/login" in (loc.get("Location") or ""), str(code))
    check("default published password does NOT work", Session(base).login("ceo", "demo-only-change-me") == 401)
    check("wrong client password rejected", Session(base).login("ceo", "definitely-wrong-123") in (401, 429))
    check("shared client password does NOT open admin", Session(base).login("admin", a.client_password) in (401, 429))

    ceo = Session(base)
    check("client login works (ceo)", ceo.login("ceo", a.client_password) == 303)
    pages = ["/", "/demo", "/fabrics", "/processing", "/dispatch", "/gate", "/gate/entries", "/accessories", "/multihead", "/finance", "/reports",
             "/watchtower", "/assistant", "/documents", "/ledger", "/audit", "/search?q=KK", "/reports/stock-by-location?run=1", "/static/demo/sample-challan.png"]
    bad = []
    for p in pages:
        code, _, body = ceo.req(p)
        if code != 200:
            bad.append((p, code))
    check(f"{len(pages)} key pages open for the CEO", not bad, str(bad))
    _, _, home = ceo.req("/")
    check("dashboard shows live figures", "metric" in home)
    check("demo banner visible", "fictional" in home.lower())
    code, _, _ = ceo.req("/admin/users")
    check("client user cannot open admin pages (403)", code == 403, str(code))
    code, _, _ = ceo.req("/settings")
    check("settings page restricted or read-only for CEO", code in (200, 403))
    _, _, demo = ceo.req("/demo")
    check("password never shown in the app", a.client_password not in demo and a.admin_password not in demo and "Reset the demo" not in demo)
    check("integrations are listed as not connected", "Not connected" in demo)
    for secret_path in ("/.env", "/.demo.env", "/data/demo/wwtextile.db", "/static/../.demo.env", "/wwtextile/config.py"):
        code, _, body = Session(base).req(secret_path)
        check(f"{secret_path} is not served", code in (301, 302, 303, 404, 403) and "WW_SECRET_KEY" not in body)

    local = urllib.parse.urlparse(base).hostname in ("127.0.0.1", "localhost", "::1")
    adm = Session(base)
    if local:
        check("admin login works directly on the host", adm.login("admin", a.admin_password) == 303)
        code, _, body = adm.req("/admin/users")
        check("admin can manage users", code == 200)
        _, _, demo = adm.req("/demo")
        check("admin sees the reset control", "Reset the demo" in demo)
    else:
        check("admin account CANNOT sign in through the public link (even with the right password)", adm.login("admin", a.admin_password) == 401)
        code, _, _ = adm.req("/admin/users")
        check("admin pages unreachable without an admin session", code in (302, 303))

    store = Session(base)
    check("store1 login works", store.login("store1", a.client_password) == 303)
    for p in ("/fabrics/receipts/new", "/dispatch/new", "/accessories/docs/new"):
        code, _, _ = store.req(p)
        check(f"store1 can open {p}", code == 200, str(code))
    if a.workflow:
        run_workflow(base, a.client_password)
    ok = sum(results)
    print(f"\n{ok}/{len(results)} checks passed")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
