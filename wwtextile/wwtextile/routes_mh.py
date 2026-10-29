"""Multihead production: orders, daily logs, fabric consumption. Unit and stock effect come from admin settings (not assumed)."""
from __future__ import annotations

from datetime import date

from starlette.routing import Route

from .errors import ValidationFailed
from .services import multihead as mh
from .webcore import _qs, handler


@handler(perm="mh.view")
def mh_index(ctx):
    status, q = _qs(ctx.request, "status"), _qs(ctx.request, "q")
    units = mh.configured_units(ctx.conn)
    return ctx.render("mh_index.html", rows=mh.list_orders(ctx.conn, status or None, q or None), status=status, q=q, units=units,
                      effect=mh.fabric_effect(ctx.conn), machines=mh.list_machines(ctx.conn, True))


def _form(ctx, errors=None, values=None, status=200):
    return ctx.render("mh_new.html", status, errors=errors or {}, values=values or {}, units=mh.configured_units(ctx.conn), machines=mh.list_machines(ctx.conn))


@handler(perm="mh.create")
def mh_new(ctx):
    return _form(ctx)


@handler(perm="mh.create", post=True)
def mh_create(ctx):
    try:
        oid = mh.create_order(ctx.conn, ctx.user, ctx.form)
    except ValidationFailed as e:
        return _form(ctx, e.errors, ctx.form, 422)
    ctx.flash("ok", "Multihead order created.")
    return ctx.redirect(f"/multihead/orders/{oid}")


def _detail(ctx, order_id, status=200, log_err=None, log_val=None, con_err=None, con_val=None):
    o = mh.get_order(ctx.conn, int(order_id))
    return ctx.render("mh_order.html", status, o=o, totals=mh.order_totals(ctx.conn, o["id"]), logs=mh.order_logs(ctx.conn, o["id"]),
                      cons=mh.order_consumptions(ctx.conn, o["id"]), acc_docs=mh.linked_accessory_docs(ctx.conn, o["order_no"]), machines=mh.list_machines(ctx.conn),
                      dec=mh.configured_units(ctx.conn).get(o["unit"], 3), effect=mh.fabric_effect(ctx.conn), today=date.today().isoformat(),
                      log_err=log_err or {}, log_val=log_val or {}, con_err=con_err or {}, con_val=con_val or {},
                      stores=ctx.conn.execute("SELECT * FROM locations WHERE kind IN ('raw_store','finished_store') ORDER BY id").fetchall())


@handler(perm="mh.view")
def mh_detail(ctx, order_id):
    return _detail(ctx, order_id)


@handler(perm="mh.log", post=True)
def mh_log(ctx, order_id):
    try:
        mh.log_production(ctx.conn, ctx.user, int(order_id), ctx.form)
    except ValidationFailed as e:
        return _detail(ctx, order_id, 422, log_err=e.errors, log_val=ctx.form)
    ctx.flash("ok", "Production logged.")
    return ctx.redirect(f"/multihead/orders/{order_id}")


@handler(perm="mh.log", post=True)
def mh_void_log(ctx, log_id):
    row = ctx.conn.execute("SELECT order_id FROM mh_logs WHERE id=?", (int(log_id),)).fetchone()
    try:
        mh.void_log(ctx.conn, ctx.user, int(log_id), ctx.form.get("reason", ""))
    except ValidationFailed as e:
        ctx.flash("error", "; ".join(e.errors.values()))
    else:
        ctx.flash("ok", "Log entry voided (the record is kept).")
    return ctx.redirect(f"/multihead/orders/{row['order_id']}" if row else "/multihead")


@handler(perm="mh.consume", post=True)
def mh_consume(ctx, order_id):
    try:
        mh.consume_fabric(ctx.conn, ctx.user, int(order_id), ctx.form)
    except ValidationFailed as e:
        return _detail(ctx, order_id, 422, con_err=e.errors, con_val=ctx.form)
    ctx.flash("ok", "Fabric consumption recorded." + (" Stock was deducted." if mh.fabric_effect(ctx.conn) == "consume" else " (Recorded only — stock was not changed, per setting.)"))
    return ctx.redirect(f"/multihead/orders/{order_id}")


@handler(perm="mh.consume", post=True)
def mh_void_consumption(ctx, cid):
    row = ctx.conn.execute("SELECT order_id FROM mh_consumptions WHERE id=?", (int(cid),)).fetchone()
    try:
        mh.void_consumption(ctx.conn, ctx.user, int(cid), ctx.form.get("reason", ""))
    except ValidationFailed as e:
        ctx.flash("error", "; ".join(e.errors.values()))
    else:
        ctx.flash("ok", "Consumption voided.")
    return ctx.redirect(f"/multihead/orders/{row['order_id']}" if row else "/multihead")


@handler(perm="mh.close", post=True)
def mh_close(ctx, order_id):
    try:
        mh.close_order(ctx.conn, ctx.user, int(order_id), ctx.form.get("outcome", ""), ctx.form.get("note", ""))
    except ValidationFailed as e:
        ctx.flash("error", "; ".join(e.errors.values()))
    else:
        ctx.flash("ok", "Order closed.")
    return ctx.redirect(f"/multihead/orders/{order_id}")


@handler(perm="mh.machines.manage", post=True)
def mh_machine_create(ctx):
    try:
        mh.create_machine(ctx.conn, ctx.user, ctx.form)
    except ValidationFailed as e:
        ctx.flash("error", "; ".join(e.errors.values()))
    else:
        ctx.flash("ok", "Machine added.")
    return ctx.redirect("/multihead")


@handler(perm="mh.machines.manage", post=True)
def mh_machine_active(ctx, mid):
    mh.set_machine_active(ctx.conn, ctx.user, int(mid), ctx.form.get("active") == "1")
    ctx.flash("ok", "Machine updated.")
    return ctx.redirect("/multihead")


G, P = ["GET"], ["POST"]
ROUTES = [
    Route("/multihead", mh_index, methods=G), Route("/multihead/orders/new", mh_new, methods=G), Route("/multihead/orders", mh_create, methods=P),
    Route("/multihead/orders/{order_id:int}", mh_detail, methods=G), Route("/multihead/orders/{order_id:int}/log", mh_log, methods=P),
    Route("/multihead/orders/{order_id:int}/consume", mh_consume, methods=P), Route("/multihead/orders/{order_id:int}/close", mh_close, methods=P),
    Route("/multihead/logs/{log_id:int}/void", mh_void_log, methods=P), Route("/multihead/consumptions/{cid:int}/void", mh_void_consumption, methods=P),
    Route("/multihead/machines", mh_machine_create, methods=P), Route("/multihead/machines/{mid:int}/active", mh_machine_active, methods=P),
]
