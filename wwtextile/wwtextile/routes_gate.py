"""Gate entries (inward / non-dispatch outward vehicles). Dispatch gate passes stay in web.py."""
from __future__ import annotations

from starlette.routing import Route

from .errors import ValidationFailed
from .services import gate_entries as ge
from .webcore import _qs, handler


def _lists(conn):
    return dict(parties=conn.execute("SELECT * FROM parties ORDER BY name").fetchall(), ref_types=ge.REF_TYPES)


@handler(perm="gate.view")
def entries(ctx):
    d, s, q = _qs(ctx.request, "direction"), _qs(ctx.request, "status"), _qs(ctx.request, "q")
    return ctx.render("gate_entries.html", rows=ge.list_entries(ctx.conn, d or None, s or None, q or None), d=d, s=s, q=q,
                      inside=ge.vehicles_inside(ctx.conn), needs_approval=ge.outward_requires_approval(ctx.conn))


def _form(ctx, direction, errors=None, values=None, status=200):
    return ctx.render("gate_entry_new.html", status, direction=direction, errors=errors or {}, values=values or {}, **_lists(ctx.conn))


@handler(perm="gate.entry.create")
def entry_new(ctx):
    d = _qs(ctx.request, "direction", "inward")
    return _form(ctx, d if d in ("inward", "outward") else "inward")


@handler(perm="gate.entry.create", post=True)
def entry_create(ctx):
    d = ctx.form.get("direction", "")
    try:
        eid = ge.create(ctx.conn, ctx.user, ctx.form)
    except ValidationFailed as e:
        return _form(ctx, d if d in ("inward", "outward") else "inward", e.errors, ctx.form, 422)
    g = ge.get(ctx.conn, eid)
    ctx.flash("ok", f"{g['entry_no']} recorded. " + ("Waiting for approval before the vehicle can leave." if g["status"] == "awaiting_approval" else ""))
    return ctx.redirect(f"/gate/entries/{eid}")


@handler(perm="gate.view")
def entry_detail(ctx, entry_id):
    g = ge.get(ctx.conn, int(entry_id))
    return ctx.render("gate_entry.html", g=g, ref_link=ge.ref_url(ctx.conn, g["ref_type"], g["ref_no"]), ref_types=ge.REF_TYPES)


@handler(perm="gate.view")
def entry_print(ctx, entry_id):
    g = ge.get(ctx.conn, int(entry_id))
    return ctx.render("gate_entry_print.html", g=g)


@handler(perm="gate.entry.approve", post=True)
def entry_approve(ctx, entry_id):
    n = ge.approve(ctx.conn, ctx.user, int(entry_id))
    ctx.flash("ok", f"{n} approved. The vehicle may now be exited.")
    return ctx.redirect(f"/gate/entries/{entry_id}")


@handler(perm="gate.entry.exit", post=True)
def entry_exit(ctx, entry_id):
    n = ge.confirm_exit(ctx.conn, ctx.user, int(entry_id), ctx.form.get("notes", ""))
    ctx.flash("ok", f"{n}: vehicle exit confirmed.")
    return ctx.redirect(ctx.back(f"/gate/entries/{entry_id}"))


@handler(perm="gate.entry.cancel", post=True)
def entry_cancel(ctx, entry_id):
    try:
        ge.cancel(ctx.conn, ctx.user, int(entry_id), ctx.form.get("reason", ""))
    except ValidationFailed as e:
        ctx.flash("error", "; ".join(e.errors.values()))
        return ctx.redirect(f"/gate/entries/{entry_id}")
    ctx.flash("ok", "Entry cancelled.")
    return ctx.redirect(f"/gate/entries/{entry_id}")


G, P = ["GET"], ["POST"]
ROUTES = [
    Route("/gate/entries", entries, methods=G), Route("/gate/entries/new", entry_new, methods=G), Route("/gate/entries", entry_create, methods=P),
    Route("/gate/entries/{entry_id:int}", entry_detail, methods=G), Route("/gate/entries/{entry_id:int}/print", entry_print, methods=G),
    Route("/gate/entries/{entry_id:int}/approve", entry_approve, methods=P), Route("/gate/entries/{entry_id:int}/exit", entry_exit, methods=P),
    Route("/gate/entries/{entry_id:int}/cancel", entry_cancel, methods=P),
]
