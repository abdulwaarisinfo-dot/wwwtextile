"""Shared web plumbing: templating, request context, handler decorator, helpers.
Feature route modules import from here; web.py assembles the app."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from itsdangerous import BadSignature, URLSafeSerializer
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import __version__, audit, db, security
from .config import Config
from .errors import AppError, BusinessRuleViolation, NotFound, PermissionDenied, ValidationFailed
from .permissions import ROLE_LABELS, can, require
from .money import fmt_money
from .quantity import fmt_qty

BASE = Path(__file__).parent
SESSION_COOKIE, LOGIN_CSRF_COOKIE, FLASH_COOKIE = "ww_session", "ww_lcsrf", "ww_flash"
PLANNED: dict = {}   # every planned module is now built; kept so the /modules route and template still work

# ---------------------------------------------------------------- templating
def _make_env(cfg: Config) -> Environment:
    env = Environment(loader=FileSystemLoader(str(BASE / "templates")), autoescape=select_autoescape(["html"]))
    try:
        tz = ZoneInfo(cfg.display_tz)
    except Exception:
        tz = timezone(timedelta(hours=0))

    def dt(value):
        if not value:
            return "—"
        try:
            d = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).astimezone(tz)
            return d.strftime("%d %b %Y %H:%M")
        except ValueError:
            return value

    env.filters["qty"] = fmt_qty
    env.filters["aqty"] = lambda v, decimals=3: (fmt_qty(v)[:-4] if (decimals in (0, "0") and fmt_qty(v).endswith(".000")) else fmt_qty(v))
    env.filters["money"] = fmt_money
    env.filters["dt"] = dt
    env.filters["role_label"] = lambda r: ROLE_LABELS.get(r, r)
    env.filters["pretty"] = lambda s: (s or "").replace("_", " ")
    env.globals["can"] = can
    env.globals["tz_name"] = cfg.display_tz
    env.globals["version"] = __version__
    env.globals["planned"] = PLANNED
    return env


# ---------------------------------------------------------------- request context
class Ctx:
    def __init__(self, request, conn, cfg: Config, env: Environment, form: dict, multi):
        self.request, self.conn, self.cfg, self.env = request, conn, cfg, env
        self.form, self.multi = form, multi
        self.user, self.csrf, self.token = None, None, None
        self.new_cookies: list[tuple] = []
        self.delete_cookies: list[str] = []
        self._flash_serial = URLSafeSerializer(request.app.state.secret, salt="flash")

    # -- session
    def load_session(self):
        self.token = self.request.cookies.get(SESSION_COOKIE)
        self.user, self.csrf = security.session_user(self.conn, self.token)

    # -- flash
    def flash(self, kind: str, message: str):
        self.new_cookies.append((FLASH_COOKIE, self._flash_serial.dumps({"k": kind, "m": message[:400]}), 60))

    def pop_flash(self):
        raw = self.request.cookies.get(FLASH_COOKIE)
        if not raw:
            return None
        self.delete_cookies.append(FLASH_COOKIE)
        try:
            return self._flash_serial.loads(raw)
        except BadSignature:
            return None

    # -- responses
    def render(self, template: str, code: int = 200, **kw) -> Response:
        flash = self.pop_flash()
        extras = {}
        if self.user is not None:
            from .services import notifications, watchtower
            try:
                extras["unread"] = notifications.unread_count(self.conn, self.user)
                extras["alert_count"] = sum(watchtower.counts(self.conn).values()) if can(self.user["role"], "alert.view") else 0
            except Exception:       # a broken badge must never break a page
                extras["unread"], extras["alert_count"] = 0, 0
        base = dict(extras, ctx=self, user=self.user, csrf=self.csrf, flash=flash, cfg=self.cfg, path=self.request.url.path,
                    query=self.request.url.query, nav=self._nav())
        base.update(kw)       # page-specific values win (e.g. the notifications page passes its own 'unread')
        html = self.env.get_template(template).render(**base)
        return self._finish(HTMLResponse(html, status_code=code))

    def redirect(self, url: str, status: int = 303) -> Response:
        return self._finish(RedirectResponse(url, status_code=status))

    def back(self, default: str = "/") -> str:
        b = (self.form.get("back") or "").strip()
        return b if b.startswith("/") and not b.startswith("//") else default

    def _finish(self, resp: Response) -> Response:
        for name, value, max_age in self.new_cookies:
            resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax", secure=self.cfg.cookie_secure, path="/")
        for name in self.delete_cookies:
            if not any(c[0] == name for c in self.new_cookies):
                resp.delete_cookie(name, path="/")
        return resp

    def _nav(self):
        if not self.user:
            return []
        r = self.user["role"]
        groups = [("Overview", [("Dashboard", "/", True), ("Watchtower", "/watchtower", can(r, "alert.view")), ("Reports", "/reports", can(r, "report.view"))]),
                  ("Operations", [
                      ("Fabrics", "/fabrics", can(r, "stock.view")),
                      ("Textile Processing", "/processing", can(r, "processing.view")),
                      ("Multihead", "/multihead", can(r, "mh.view")),
                      ("Accessory Store", "/accessories", can(r, "acc.view")),
                      ("Dispatch", "/dispatch", can(r, "dispatch.view")),
                      ("Gate Management", "/gate", can(r, "gate.view")),
                  ]),
                  ("Finance", [("Finance", "/finance", can(r, "fin.view"))]),
                  ("Records", [
                      ("Stock Ledger", "/ledger", can(r, "ledger.view")),
                      ("Audit Trail", "/audit", can(r, "audit.view")),
                      ("Document Reader", "/documents", can(r, "doc.view")),
                  ]),
                  ("Assistant", [(self.cfg.assistant_name + " (assistant)", "/assistant", can(r, "ask.query")), ("Lot Query (rules)", "/ask", can(r, "ask.query"))]),
                  ("Administration", [("Users", "/admin/users", can(r, "user.manage")), ("Master data", "/admin/master", can(r, "master.manage")), ("Settings", "/settings", can(r, "settings.view"))])]
        if self.cfg.env == "demo":
            groups.insert(0, ("Demo", [("Demo guide", "/demo", True)]))
        out = [(g, [(n, u) for n, u, ok in items if ok]) for g, items in groups]
        return [(g, items) for g, items in out if items]


# ---------------------------------------------------------------- handler decorator
def handler(*, perm: str | None = None, public: bool = False, post: bool = False):
    def deco(fn):
        async def endpoint(request):
            form, multi = {}, None
            if request.method == "POST":
                fd = await request.form()
                multi = fd
                form = {k: fd.get(k) for k in fd.keys()}
            return await run_in_threadpool(_run, request, fn, perm, public, post, form, multi)
        endpoint.__name__ = fn.__name__
        return endpoint
    return deco


def _referer_path(request) -> str:
    from urllib.parse import urlparse
    u = urlparse(request.headers.get("referer", ""))
    return (u.path + ("?" + u.query if u.query else "")) if u.path.startswith("/") else "/"


def _run(request, fn, perm, public, post, form, multi):
    cfg: Config = request.app.state.cfg
    conn = db.connect(cfg.db_path)
    ctx = Ctx(request, conn, cfg, request.app.state.env, form, multi)
    try:
        ctx.load_session()
        if not public and ctx.user is None:
            return ctx.redirect("/login?next=" + quote(request.url.path))
        if (ctx.user is not None and ctx.user["must_change_password"] and request.url.path not in ("/account/password", "/logout")
                and not request.url.path.startswith("/static")):
            if request.method == "POST":
                return ctx.render("error.html", 403, title="Password change required", message="You must change your password before doing anything else.")
            return ctx.redirect("/account/password")
        if post and not public:
            if not form.get("csrf") or not secrets.compare_digest(str(form.get("csrf")), str(ctx.csrf)):
                audit.record_standalone(conn, ctx.user, "security.csrf_rejected", entity_ref=request.url.path, outcome="denied")
                return ctx.render("error.html", 403, title="Request rejected",
                                  message="The form's security token was missing or expired. Go back, reload the page and try again.")
        if perm and ctx.user is not None:
            require(ctx.user, perm)
        return fn(ctx, **request.path_params)
    except ValidationFailed as e:
        # Handlers that can re-render a form catch this themselves; this is the generic fallback.
        ctx.flash("error", "; ".join(e.errors.values()))
        return ctx.redirect(ctx.back(_referer_path(request)))
    except PermissionDenied as e:
        audit.record_standalone(conn, ctx.user, "security.permission_denied", entity_ref=request.url.path,
                                outcome="denied", detail={"reason": str(e)})
        return ctx.render("error.html", 403, title="Not permitted", message=str(e))
    except NotFound as e:
        return ctx.render("error.html", 404, title="Not found", message=str(e))
    except BusinessRuleViolation as e:
        audit.record_standalone(conn, ctx.user, "action.rejected", entity_ref=request.url.path, outcome="failed",
                                detail={"reason": str(e)})
        if request.method == "POST":
            ctx.flash("error", str(e))
            return ctx.redirect(ctx.back(_referer_path(request)))
        return ctx.render("error.html", 409, title="Action not allowed", message=str(e))
    finally:
        conn.close()


def display_tz(cfg: Config):
    try:
        return ZoneInfo(cfg.display_tz)
    except Exception:
        return timezone.utc


def day_range(cfg: Config, d1: str, d2: str):
    """Local dates (YYYY-MM-DD, either may be blank) -> UTC [from, to) bounds. Raises ValidationFailed on bad dates."""
    from .services.reports import _d, local_day_bounds
    tz = display_tz(cfg)
    a, b = _d(d1), _d(d2)
    return (local_day_bounds(a, tz)[0] if a else None), (local_day_bounds(b, tz)[1] if b else None)


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _qs(request, key, default=""):
    return (request.query_params.get(key) or default).strip()


def _lists(conn):
    rows = lambda sql: conn.execute(sql).fetchall()
    suppliers = rows("SELECT * FROM parties WHERE kind='supplier' ORDER BY name")
    customers = rows("SELECT * FROM parties WHERE kind='customer' ORDER BY name")
    parties = rows("SELECT * FROM parties WHERE kind IN ('job_work','supplier') ORDER BY name")
    items = rows("SELECT * FROM items ORDER BY name")
    raw_locations = rows("SELECT * FROM locations WHERE kind='raw_store' ORDER BY id")
    opts = lambda rs, label="name": [(r["id"], r[label]) for r in rs]
    return dict(suppliers=suppliers, customers=customers, parties=parties, items=items, raw_locations=raw_locations,
                supplier_opts=opts(suppliers), customer_opts=opts(customers), party_opts=opts(parties),
                item_opts=opts(items), raw_loc_opts=opts(raw_locations))


