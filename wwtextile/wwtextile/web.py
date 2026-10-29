"""Starlette web layer: routing, sessions, CSRF, error handling, server-rendered pages.

All business rules live in wwtextile.services; this module only parses input,
calls services, and renders results. Authorisation is enforced inside the
services AND checked here for navigation, so hiding a button is never the
only protection.
"""
from __future__ import annotations

import csv
import io
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from itsdangerous import BadSignature, URLSafeSerializer
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, audit, db, security
from .config import Config
from .errors import AppError, BusinessRuleViolation, NotFound, PermissionDenied, ValidationFailed
from .permissions import ALL_PERMISSIONS, ROLE_LABELS, ROLES, can, require
from .quantity import fmt_qty
from .services import ask as ask_svc
from .services import dashboard, dispatch, inventory, processing, receipts, search, settings, trace
from . import routes_acc, routes_admin, routes_demo, routes_fin, routes_gate, routes_mh, routes_ops
from .webcore import (BASE, FLASH_COOKIE, LOGIN_CSRF_COOKIE, PLANNED, SESSION_COOKIE, Ctx, _int, _lists, _make_env,
                      _qs, _referer_path, display_tz, handler)


# ================================================================ auth
@handler(public=True)
def login_page(ctx):
    if ctx.user:
        return ctx.redirect("/")
    token = secrets.token_urlsafe(16)
    ctx.new_cookies.append((LOGIN_CSRF_COOKIE, token, 600))
    return ctx.render("login.html", lcsrf=token, next=_qs(ctx.request, "next", "/"), error=None, username="")


@handler(public=True, post=True)
def login_submit(ctx):
    cookie = ctx.request.cookies.get(LOGIN_CSRF_COOKIE, "")
    nxt = ctx.form.get("next") or "/"
    nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"
    if not cookie or not secrets.compare_digest(cookie, str(ctx.form.get("lcsrf") or "")):
        token = secrets.token_urlsafe(16)
        ctx.new_cookies.append((LOGIN_CSRF_COOKIE, token, 600))
        return ctx.render("login.html", 403, lcsrf=token, next=nxt, username="",
                          error="The login form expired. Please try again.")
    username = str(ctx.form.get("username") or "")
    if ctx.cfg.public_demo and not _login_allowed(ctx.request):
        t = secrets.token_urlsafe(16)
        ctx.new_cookies.append((LOGIN_CSRF_COOKIE, t, 600))
        return ctx.render("login.html", 429, lcsrf=t, next=nxt, username=username,
                          error="Too many sign-in attempts from this address. Wait a few minutes and try again.")
    if ctx.cfg.public_demo and not ctx.cfg.demo_admin_remote and _via_proxy(ctx.request):
        # The admin account (user management, settings, demo reset) is never offered through the public link: it can only sign in
        # on the host itself (http://127.0.0.1:PORT). A refused attempt does not count towards the lockout, so strangers cannot lock it.
        row = ctx.conn.execute("SELECT role FROM users WHERE username=?", (username.strip(),)).fetchone()
        if row is not None and row["role"] == "admin":
            audit.record_standalone(ctx.conn, None, "auth.login", entity_type="user", entity_ref=username.strip()[:60], outcome="denied",
                                    detail={"reason": "admin_not_allowed_via_public_link"})
            t = secrets.token_urlsafe(16)
            ctx.new_cookies.append((LOGIN_CSRF_COOKIE, t, 600))
            return ctx.render("login.html", 401, lcsrf=t, next=nxt, username=username, error="Invalid username or password.")
    try:
        token, _csrf, _user = security.login(ctx.conn, ctx.cfg, username, str(ctx.form.get("password") or ""))
    except ValueError as e:
        t = secrets.token_urlsafe(16)
        ctx.new_cookies.append((LOGIN_CSRF_COOKIE, t, 600))
        return ctx.render("login.html", 401, lcsrf=t, next=nxt, username=username, error=str(e))
    ctx.new_cookies.append((SESSION_COOKIE, token, ctx.cfg.session_hours * 3600))
    ctx.delete_cookies.append(LOGIN_CSRF_COOKIE)
    return ctx.redirect(nxt)


_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip", "cf-connecting-ip", "cf-ray", "cf-visitor", "via")


def _via_proxy(request) -> bool:
    """True when the request came through a tunnel / reverse proxy (any forwarding header present) rather than directly to the host."""
    return any(h in request.headers for h in _PROXY_HEADERS)


_LOGIN_HITS: dict[str, list[float]] = {}


def _login_allowed(request, limit: int = 20, window: int = 600) -> bool:
    """Per-address throttle on sign-in attempts (public demo only), so a stranger cannot lock the demo accounts by guessing."""
    import time
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    hits = [t for t in _LOGIN_HITS.get(ip, []) if now - t < window]
    if len(hits) >= limit:
        _LOGIN_HITS[ip] = hits
        return False
    hits.append(now)
    _LOGIN_HITS[ip] = hits
    if len(_LOGIN_HITS) > 5000:
        _LOGIN_HITS.clear()
    return True


@handler(post=True)
def logout(ctx):
    security.logout(ctx.conn, ctx.token, ctx.user)
    ctx.delete_cookies.append(SESSION_COOKIE)
    resp = ctx.redirect("/login")
    return resp


# ================================================================ dashboard / search
@handler()
def home(ctx):
    from .services import briefing, charts
    acts = briefing.actions(ctx.conn, ctx.user, ctx.cfg.currency)
    return ctx.render("dashboard.html", m=dashboard.metrics(ctx.conn, ctx.user), todo=acts, flow=briefing.pipeline(ctx.conn, ctx.user),
                      hello=briefing.greeting(ctx.user, display_tz(ctx.cfg)), focus=briefing.role_focus(ctx.user["role"]), charts=charts.build(ctx.conn, ctx.user, display_tz(ctx.cfg), ctx.cfg.currency))


@handler()
def search_page(ctx):
    q = _qs(ctx.request, "q")
    return ctx.render("search.html", q=q, results=search.search(ctx.conn, ctx.user, q))


@handler()
def planned_page(ctx, slug):
    if slug not in PLANNED:
        raise NotFound("Unknown module.")
    title, status, body = PLANNED[slug]
    return ctx.render("planned.html", title=title, status=status, body=body, slug=slug)


# ================================================================ fabrics / receipts
@handler(perm="stock.view")
def fabrics(ctx):
    kind, q = _qs(ctx.request, "kind"), _qs(ctx.request, "q")
    rows = inventory.stock_summary(ctx.conn, kind or None, q or None)
    return ctx.render("fabrics.html", rows=rows, kind=kind, q=q)


@handler(perm="stock.view")
def lot_page(ctx, lot_no):
    t = trace.lot_trace(ctx.conn, lot_no)
    return ctx.render("lot.html", t=t)


@handler(perm="stock.view")
def receipts_list(ctx):
    status, q = _qs(ctx.request, "status"), _qs(ctx.request, "q")
    return ctx.render("receipts.html", rows=receipts.list_receipts(ctx.conn, status or None, q or None), status=status, q=q)


def _receipt_form(ctx, errors=None, values=None, status=200):
    return ctx.render("receipt_new.html", status, errors=errors or {}, values=values or {}, **_lists(ctx.conn))


@handler(perm="receipt.create")
def receipt_new(ctx):
    return _receipt_form(ctx)


@handler(perm="receipt.create", post=True)
def receipt_create(ctx):
    try:
        rid = receipts.create_draft(ctx.conn, ctx.user, ctx.form)
    except ValidationFailed as e:
        return _receipt_form(ctx, e.errors, ctx.form, 422)
    ctx.flash("ok", "Receipt saved as a draft. It does not affect stock until posted.")
    return ctx.redirect(f"/fabrics/receipts/{rid}")


@handler(perm="stock.view")
def receipt_detail(ctx, receipt_id):
    r = receipts.get(ctx.conn, int(receipt_id))
    ledger = ctx.conn.execute("SELECT * FROM stock_ledger WHERE source_type='goods_receipt' AND source_id=? ORDER BY id", (r["id"],)).fetchall()
    return ctx.render("receipt.html", r=r, ledger=ledger)


@handler(perm="receipt.post", post=True)
def receipt_post(ctx, receipt_id):
    doc = receipts.post(ctx.conn, ctx.user, int(receipt_id))
    ctx.flash("ok", f"{doc} posted. Stock updated.")
    return ctx.redirect(f"/fabrics/receipts/{receipt_id}")


@handler(perm="receipt.create", post=True)
def receipt_discard(ctx, receipt_id):
    receipts.discard(ctx.conn, ctx.user, int(receipt_id))
    ctx.flash("ok", "Draft discarded.")
    return ctx.redirect("/fabrics/receipts")


@handler(perm="receipt.reverse", post=True)
def receipt_reverse(ctx, receipt_id):
    try:
        receipts.reverse(ctx.conn, ctx.user, int(receipt_id), ctx.form.get("reason", ""))
    except ValidationFailed as e:
        ctx.flash("error", "; ".join(e.errors.values()))
        return ctx.redirect(f"/fabrics/receipts/{receipt_id}")
    ctx.flash("ok", "Receipt reversed with linked ledger entries.")
    return ctx.redirect(f"/fabrics/receipts/{receipt_id}")


# ================================================================ processing
@handler(perm="processing.view")
def processing_list(ctx):
    status, q = _qs(ctx.request, "status"), _qs(ctx.request, "q")
    overdue = _qs(ctx.request, "overdue") == "1"
    view = _qs(ctx.request, "view")
    return ctx.render("processing.html", jobs=processing.list_jobs(ctx.conn, status or None, overdue, q or None),
                      exceptions=processing.reconciliation_exceptions(ctx.conn) if view == "exceptions" else None,
                      status=status, q=q, overdue=overdue, view=view)


def _job_form(ctx, errors=None, values=None, status=200):
    lots = ctx.conn.execute("SELECT lot_no FROM lots WHERE kind='raw' ORDER BY lot_no").fetchall()
    return ctx.render("job_new.html", status, errors=errors or {}, values=values or {},
                      lot_opts=[(r["lot_no"], r["lot_no"]) for r in lots],
                      type_opts=[(t, t.capitalize()) for t in processing.PROCESS_TYPES], **_lists(ctx.conn))


@handler(perm="processing.create")
def job_new(ctx):
    return _job_form(ctx, values={"lot_no": _qs(ctx.request, "lot")})


@handler(perm="processing.create", post=True)
def job_create(ctx):
    try:
        jid = processing.create_job(ctx.conn, ctx.user, ctx.form)
    except ValidationFailed as e:
        return _job_form(ctx, e.errors, ctx.form, 422)
    ctx.flash("ok", "Processing job created. Prepare an issue to send stock into processing.")
    return ctx.redirect(f"/processing/jobs/{jid}")


def _job_page(ctx, job_id, errors=None, values=None, open_form=None, status=200):
    job = processing.get_job(ctx.conn, int(job_id))
    issues, results = processing.job_issues(ctx.conn, job["id"]), processing.job_results(ctx.conn, job["id"])
    live = {r["issue_id"] for r in results if r["status"] in ("draft", "posted")}
    avail = {loc["id"]: inventory.available(ctx.conn, job["lot_id"], loc["id"])
             for loc in _lists(ctx.conn)["raw_locations"]}
    return ctx.render("job.html", status, job=job, issues=issues, results=results, awaiting=[i for i in issues if i["status"] == "posted" and i["id"] not in live],
                      avail=avail, locations=_lists(ctx.conn)["raw_locations"], loc_opts=_lists(ctx.conn)["raw_loc_opts"], errors=errors or {}, values=values or {}, open_form=open_form)


@handler(perm="processing.view")
def job_detail(ctx, job_id):
    return _job_page(ctx, job_id)


@handler(perm="processing.issue", post=True)
def issue_create(ctx, job_id):
    try:
        processing.create_issue_draft(ctx.conn, ctx.user, int(job_id), ctx.form)
    except ValidationFailed as e:
        return _job_page(ctx, job_id, e.errors, ctx.form, "issue", 422)
    ctx.flash("ok", "Issue drafted. Review it, then post to move stock into processing.")
    return ctx.redirect(f"/processing/jobs/{job_id}")


@handler(perm="processing.issue", post=True)
def issue_post(ctx, issue_id):
    jid = ctx.conn.execute("SELECT job_id FROM processing_issues WHERE id=?", (int(issue_id),)).fetchone()
    no = processing.post_issue(ctx.conn, ctx.user, int(issue_id))
    ctx.flash("ok", f"{no} posted. Stock moved into processing.")
    return ctx.redirect(f"/processing/jobs/{jid['job_id']}")


@handler(perm="processing.issue", post=True)
def issue_discard(ctx, issue_id):
    jid = ctx.conn.execute("SELECT job_id FROM processing_issues WHERE id=?", (int(issue_id),)).fetchone()
    processing.discard_issue(ctx.conn, ctx.user, int(issue_id))
    ctx.flash("ok", "Draft issue discarded.")
    return ctx.redirect(f"/processing/jobs/{jid['job_id']}")


@handler(perm="processing.result", post=True)
def result_create(ctx, issue_id):
    jid = ctx.conn.execute("SELECT job_id FROM processing_issues WHERE id=?", (int(issue_id),)).fetchone()
    if jid is None:
        raise NotFound("Issue not found.")
    try:
        processing.create_result_draft(ctx.conn, ctx.user, int(issue_id), ctx.form)
    except ValidationFailed as e:
        return _job_page(ctx, jid["job_id"], e.errors, {**ctx.form, "issue_id": issue_id}, "result", 422)
    ctx.flash("ok", "Result drafted with its reconciliation. Review it, then post.")
    return ctx.redirect(f"/processing/jobs/{jid['job_id']}")


@handler(perm="processing.result", post=True)
def result_post(ctx, result_id):
    jid = ctx.conn.execute("SELECT job_id FROM processing_results WHERE id=?", (int(result_id),)).fetchone()
    no = processing.post_result(ctx.conn, ctx.user, int(result_id))
    ctx.flash("ok", f"{no} posted. Stock moved and reconciliation recorded.")
    return ctx.redirect(f"/processing/jobs/{jid['job_id']}")


@handler(perm="processing.result", post=True)
def result_discard(ctx, result_id):
    jid = ctx.conn.execute("SELECT job_id FROM processing_results WHERE id=?", (int(result_id),)).fetchone()
    processing.discard_result(ctx.conn, ctx.user, int(result_id))
    ctx.flash("ok", "Draft result discarded.")
    return ctx.redirect(f"/processing/jobs/{jid['job_id']}")


@handler(perm="processing.create", post=True)
def job_cancel(ctx, job_id):
    processing.cancel_job(ctx.conn, ctx.user, int(job_id))
    ctx.flash("ok", "Job cancelled.")
    return ctx.redirect("/processing")


# ================================================================ dispatch
@handler(perm="dispatch.view")
def dispatch_list(ctx):
    status, q = _qs(ctx.request, "status"), _qs(ctx.request, "q")
    return ctx.render("dispatch_list.html", rows=dispatch.list_dispatches(ctx.conn, status or None, q or None), status=status, q=q)


def _dispatch_form(ctx, errors=None, values=None, status=200):
    lots = [r for r in inventory.stock_summary(ctx.conn, "finished_store") if r["available"] > 0]
    return ctx.render("dispatch_new.html", status, errors=errors or {}, values=values or {}, lots=lots, **_lists(ctx.conn))


@handler(perm="dispatch.create")
def dispatch_new(ctx):
    return _dispatch_form(ctx, values={"lot_1": _qs(ctx.request, "lot")})


@handler(perm="dispatch.create", post=True)
def dispatch_create(ctx):
    lines = [(str(ctx.form.get(f"lot_{i}") or ""), str(ctx.form.get(f"qty_{i}") or "")) for i in (1, 2, 3)]
    try:
        did = dispatch.create_draft(ctx.conn, ctx.user, ctx.form.get("customer_id"), lines, ctx.form.get("notes", ""))
    except ValidationFailed as e:
        return _dispatch_form(ctx, e.errors, ctx.form, 422)
    ctx.flash("ok", "Dispatch saved as a draft. Nothing is reserved until it is approved.")
    return ctx.redirect(f"/dispatch/{did}")


def _dispatch_page(ctx, dispatch_id, errors=None, values=None, status=200):
    d = dispatch.get(ctx.conn, int(dispatch_id))
    lns = dispatch.lines(ctx.conn, d["id"])
    avail = {l["id"]: inventory.available(ctx.conn, l["lot_id"], l["location_id"]) for l in lns}
    return ctx.render("dispatch.html", status, d=d, lines=lns, avail=avail, gp=dispatch.gate_pass_for(ctx.conn, d["id"]),
                      event=dispatch.deduction_event(ctx.conn), sep=dispatch.require_separate_approver(ctx.conn),
                      errors=errors or {}, values=values or {})


@handler(perm="dispatch.view")
def dispatch_detail(ctx, dispatch_id):
    return _dispatch_page(ctx, dispatch_id)


@handler(perm="dispatch.approve", post=True)
def dispatch_approve(ctx, dispatch_id):
    no = dispatch.approve(ctx.conn, ctx.user, int(dispatch_id))
    ctx.flash("ok", f"{no} approved. Stock is reserved.")
    return ctx.redirect(f"/dispatch/{dispatch_id}")


@handler(perm="dispatch.cancel", post=True)
def dispatch_cancel(ctx, dispatch_id):
    try:
        dispatch.cancel(ctx.conn, ctx.user, int(dispatch_id), ctx.form.get("reason", ""))
    except ValidationFailed as e:
        return _dispatch_page(ctx, dispatch_id, e.errors, ctx.form, 422)
    ctx.flash("ok", "Dispatch cancelled; reservations released (and stock restored if it had been deducted).")
    return ctx.redirect(f"/dispatch/{dispatch_id}")


@handler(perm="gate.pass.create", post=True)
def gate_pass_create(ctx, dispatch_id):
    try:
        no = dispatch.issue_gate_pass(ctx.conn, ctx.user, int(dispatch_id), ctx.form.get("vehicle_no", ""), ctx.form.get("driver_name", ""))
    except ValidationFailed as e:
        return _dispatch_page(ctx, dispatch_id, e.errors, ctx.form, 422)
    ctx.flash("ok", f"Gate pass {no} issued.")
    return ctx.redirect(f"/dispatch/{dispatch_id}")


@handler(perm="gate.exit.confirm", post=True)
def exit_confirm(ctx, pass_id):
    row = ctx.conn.execute("SELECT dispatch_id FROM gate_passes WHERE id=?", (int(pass_id),)).fetchone()
    no = dispatch.confirm_exit(ctx.conn, ctx.user, int(pass_id), ctx.form.get("notes", ""))
    ctx.flash("ok", f"Exit confirmed for {no}.")
    return ctx.redirect(ctx.back(f"/dispatch/{row['dispatch_id']}" if row else "/gate"))


# ================================================================ gate
@handler(perm="gate.view")
def gate_list(ctx):
    status, q = _qs(ctx.request, "status"), _qs(ctx.request, "q")
    return ctx.render("gate.html", rows=dispatch.list_gate_passes(ctx.conn, status or None, q or None), status=status, q=q)


@handler(perm="gate.view")
def gate_print(ctx, pass_id):
    gp = ctx.conn.execute("SELECT * FROM gate_passes WHERE id=?", (int(pass_id),)).fetchone()
    if gp is None:
        raise NotFound("Gate pass not found.")
    d = dispatch.get(ctx.conn, gp["dispatch_id"])
    return ctx.render("gate_print.html", gp=dispatch.gate_pass_for(ctx.conn, d["id"]), d=d, lines=dispatch.lines(ctx.conn, d["id"]))


# ================================================================ ledger / audit
@handler(perm="ledger.view")
def ledger(ctx):
    lot, mv = _qs(ctx.request, "lot"), _qs(ctx.request, "movement")
    loc = _int(_qs(ctx.request, "location")) or None
    page = max(1, _int(_qs(ctx.request, "page"), 1))
    size = 50
    rows, count = inventory.ledger_rows(ctx.conn, lot_no=lot or None, location_id=loc, movement=mv or None,
                                        limit=size, offset=(page - 1) * size)
    return ctx.render("ledger.html", rows=rows, count=count, page=page, size=size, lot=lot, mv=mv, loc=loc or "",
                      locations=ctx.conn.execute("SELECT * FROM locations ORDER BY id").fetchall(),
                      movements=[r["movement"] for r in ctx.conn.execute("SELECT DISTINCT movement FROM stock_ledger ORDER BY movement")],
                      qs=_filter_qs(ctx.request, {"lot", "movement", "location"}))


@handler(perm="ledger.view")
def ledger_csv(ctx):
    lot, mv = _qs(ctx.request, "lot"), _qs(ctx.request, "movement")
    loc = _int(_qs(ctx.request, "location")) or None
    rows, count = inventory.ledger_rows(ctx.conn, lot_no=lot or None, location_id=loc, movement=mv or None, limit=100000)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f"# Stock ledger export — generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC by {ctx.user['username']}"])
    w.writerow([f"# Filters: lot={lot or '*'} movement={mv or '*'} location={loc or '*'} — {count} row(s). Quantities in item units (3 decimals)."])
    w.writerow(["ledger_id", "posted_at_utc", "lot_no", "location", "qty", "movement", "source_type", "source_id", "reverses_id", "actor", "note"])
    total = 0
    for r in rows:
        total += r["qty"]
        w.writerow([r["id"], r["posted_at"], r["lot_no"], r["loc_name"], f"{r['qty'] / 1000:.3f}", r["movement"], r["source_type"],
                    r["source_id"], r["reverses_id"] or "", r["actor"] or "", (r["note"] or "").replace("\n", " ")])
    w.writerow([f"# net qty of exported rows: {total / 1000:.3f}"])
    audit.record_standalone(ctx.conn, ctx.user, "export.ledger_csv", entity_type="export", entity_ref="stock_ledger",
                            detail={"rows": count, "lot": lot, "movement": mv, "location": loc})
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="stock-ledger.csv"'})


def _filter_qs(request, keys):
    from urllib.parse import urlencode
    return urlencode({k: v for k, v in request.query_params.items() if k in keys and v})


@handler(perm="audit.view")
def audit_page(ctx):
    action, actor, outcome, ref = (_qs(ctx.request, k) for k in ("action", "actor", "outcome", "ref"))
    page = max(1, _int(_qs(ctx.request, "page"), 1))
    size = 50
    sql, args = "FROM audit_events WHERE 1=1 ", []
    for col, val, op in (("action", action, "LIKE"), ("actor_name", actor, "LIKE"), ("outcome", outcome, "="), ("entity_ref", ref, "LIKE")):
        if val:
            sql += f"AND {col} {op} ? "
            args.append(f"%{val}%" if op == "LIKE" else val)
    count = ctx.conn.execute("SELECT COUNT(*) AS c " + sql, args).fetchone()["c"]
    rows = ctx.conn.execute("SELECT * " + sql + "ORDER BY id DESC LIMIT ? OFFSET ?", args + [size, (page - 1) * size]).fetchall()
    return ctx.render("audit.html", rows=rows, count=count, page=page, size=size, action=action, actor=actor, outcome=outcome, ref=ref,
                      qs=_filter_qs(ctx.request, {"action", "actor", "outcome", "ref"}))


# ================================================================ assistant / settings
@handler(perm="ask.query")
def ask_page(ctx):
    q = _qs(ctx.request, "q")
    res = ask_svc.ask(ctx.conn, ctx.user, q) if q else None
    if res is not None:
        audit.record_standalone(ctx.conn, ctx.user, "ask.query", entity_type="query", entity_ref=(q[:80]),
                                detail={"supported": res["supported"], "evidence": len(res["evidence"])})
    return ctx.render("ask.html", q=q, res=res, supported=ask_svc.SUPPORTED)


@handler(perm="settings.view")
def settings_page(ctx):
    applied = [r["name"] for r in ctx.conn.execute("SELECT name FROM schema_migrations ORDER BY name")]
    return ctx.render("settings.html", s=settings.current(ctx.conn), events=dispatch.DEDUCTION_EVENTS, roles=ROLES,
                      perms=sorted(ALL_PERMISSIONS), applied=applied, in_flight=dispatch.in_flight_count(ctx.conn), registry=settings.all_values(ctx.conn))


@handler(perm="settings.edit", post=True)
def settings_save(ctx):
    try:
        settings.set_deduction_event(ctx.conn, ctx.user, ctx.form.get("deduction_event", ""))
        settings.set_separate_approver(ctx.conn, ctx.user, ctx.form.get("separate_approver") == "1")
    except ValidationFailed as e:
        ctx.flash("error", "; ".join(e.errors.values()))
        return ctx.redirect("/settings")
    ctx.flash("ok", "Settings saved.")
    return ctx.redirect("/settings")


# ================================================================ JSON (read-only) and health
@handler(perm="stock.view")
def api_lot(ctx, lot_no):
    t = trace.lot_trace(ctx.conn, lot_no)
    return JSONResponse({
        "lot_no": t["lot"]["lot_no"], "unit": t["lot"]["unit"],
        "balances": [{"lot_no": b["lot_no"], "location": b["name"], "on_hand": b["on_hand"] / 1000,
                      "reserved": b["reserved"] / 1000, "available": b["available"] / 1000} for b in t["balances"]],
        "summary": {k: v / 1000 for k, v in t["summary"].items()},
    })


async def health(request):
    return JSONResponse({"status": "ok", "version": __version__})


async def not_found(request, exc):
    return PlainTextResponse("Not found", status_code=404)


# ================================================================ app factory
def _security_headers(app):
    async def middleware(scope, receive, send):
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for k, v in {
                    "x-frame-options": "DENY", "x-content-type-options": "nosniff", "referrer-policy": "same-origin",
                    "content-security-policy": "default-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
                    "permissions-policy": "microphone=(self), camera=(), geolocation=()",
                    "cache-control": "no-store", "x-robots-tag": "noindex, nofollow",
                }.items():
                    headers.append((k.encode(), v.encode()))
                message["headers"] = headers
            await send(message)
        await app(scope, receive, send_wrapper) if scope["type"] == "http" else await app(scope, receive, send)
    return middleware


async def robots(request):
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


async def healthz(request):
    """Unauthenticated liveness + database check for load balancers / container healthchecks. Reveals nothing but ok/fail."""
    def probe():
        c = db.connect(request.app.state.cfg.db_path)
        try:
            c.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        finally:
            c.close()
    try:
        await run_in_threadpool(probe)
    except Exception:
        return PlainTextResponse("database unavailable", status_code=503)
    return PlainTextResponse("ok")


def create_app(cfg: Config | None = None):
    from . import config as config_mod
    cfg = cfg or config_mod.load()
    _LOGIN_HITS.clear()          # throttle state belongs to one running app, not to the process
    G, P = ["GET"], ["POST"]
    routes = [
        Route("/login", login_page, methods=G), Route("/login", login_submit, methods=P), Route("/logout", logout, methods=P),
        Route("/healthz", healthz, methods=G), Route("/robots.txt", robots, methods=G),
        Route("/", home, methods=G), Route("/search", search_page, methods=G), Route("/modules/{slug}", planned_page, methods=G),
        Route("/fabrics", fabrics, methods=G), Route("/fabrics/lots/{lot_no}", lot_page, methods=G),
        Route("/fabrics/receipts", receipts_list, methods=G), Route("/fabrics/receipts/new", receipt_new, methods=G),
        Route("/fabrics/receipts", receipt_create, methods=P),
        Route("/fabrics/receipts/{receipt_id:int}", receipt_detail, methods=G),
        Route("/fabrics/receipts/{receipt_id:int}/post", receipt_post, methods=P),
        Route("/fabrics/receipts/{receipt_id:int}/discard", receipt_discard, methods=P),
        Route("/fabrics/receipts/{receipt_id:int}/reverse", receipt_reverse, methods=P),
        Route("/processing", processing_list, methods=G), Route("/processing/jobs/new", job_new, methods=G),
        Route("/processing/jobs", job_create, methods=P),
        Route("/processing/jobs/{job_id:int}", job_detail, methods=G),
        Route("/processing/jobs/{job_id:int}/cancel", job_cancel, methods=P),
        Route("/processing/jobs/{job_id:int}/issues", issue_create, methods=P),
        Route("/processing/issues/{issue_id:int}/post", issue_post, methods=P),
        Route("/processing/issues/{issue_id:int}/discard", issue_discard, methods=P),
        Route("/processing/issues/{issue_id:int}/result", result_create, methods=P),
        Route("/processing/results/{result_id:int}/post", result_post, methods=P),
        Route("/processing/results/{result_id:int}/discard", result_discard, methods=P),
        Route("/dispatch", dispatch_list, methods=G), Route("/dispatch/new", dispatch_new, methods=G),
        Route("/dispatch", dispatch_create, methods=P),
        Route("/dispatch/{dispatch_id:int}", dispatch_detail, methods=G),
        Route("/dispatch/{dispatch_id:int}/approve", dispatch_approve, methods=P),
        Route("/dispatch/{dispatch_id:int}/cancel", dispatch_cancel, methods=P),
        Route("/dispatch/{dispatch_id:int}/gate-pass", gate_pass_create, methods=P),
        Route("/gate", gate_list, methods=G), Route("/gate/passes/{pass_id:int}/print", gate_print, methods=G),
        Route("/gate/passes/{pass_id:int}/exit", exit_confirm, methods=P),
        Route("/ledger", ledger, methods=G), Route("/ledger.csv", ledger_csv, methods=G),
        Route("/audit", audit_page, methods=G), Route("/ask", ask_page, methods=G),
        Route("/settings", settings_page, methods=G), Route("/settings", settings_save, methods=P),
        Route("/api/lots/{lot_no}", api_lot, methods=G), Route("/api/health", health, methods=G),
        *routes_admin.ROUTES, *routes_acc.ROUTES, *routes_gate.ROUTES, *routes_fin.ROUTES, *routes_mh.ROUTES, *routes_ops.ROUTES, *routes_demo.ROUTES,
        Mount("/static", StaticFiles(directory=str(BASE / "static")), name="static"),
    ]
    app = Starlette(routes=routes, exception_handlers={404: not_found})
    app.state.cfg = cfg
    app.state.env = _make_env(cfg)
    app.state.secret = cfg.secret_key or secrets.token_hex(32)   # signs the short-lived flash cookie
    # make sure the schema exists when the app is created
    c = db.connect(cfg.db_path)
    try:
        db.migrate(c)
    finally:
        c.close()
    return _Wrapped(app)


class _Wrapped:
    """ASGI wrapper that adds security headers and exposes .state/.routes of the inner app."""

    def __init__(self, app):
        self.inner = app
        self.state = app.state
        self._mw = _security_headers(app)

    async def __call__(self, scope, receive, send):
        await self._mw(scope, receive, send)
