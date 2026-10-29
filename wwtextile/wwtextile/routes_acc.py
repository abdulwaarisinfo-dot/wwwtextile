"""Accessory Store."""
from __future__ import annotations

from starlette.routing import Route

from .errors import ValidationFailed
from .services import accessories as acc
from .services import master
from .webcore import _int, _qs, day_range, handler

MOVES = ["acc_receipt", "acc_issue", "acc_transfer_out", "acc_transfer_in", "acc_reversal"]


def _acc_lists(conn):
    return dict(items=acc.list_items(conn), locations=acc.list_locations(conn),
                suppliers=conn.execute("SELECT * FROM parties WHERE kind='supplier' ORDER BY name").fetchall())


@handler(perm="acc.view")
def acc_index(ctx):
    q, loc, low = _qs(ctx.request, "q"), _int(_qs(ctx.request, "location")), _qs(ctx.request, "low") == "1"
    return ctx.render("acc_index.html", rows=acc.stock_summary(ctx.conn, q or None, loc or None, low), q=q, loc=loc, low=low,
                      locations=acc.list_locations(ctx.conn), n_low=len(acc.low_stock(ctx.conn)))


def _items_page(ctx, ierr=None, ival=None, lerr=None, lval=None, status=200):
    return ctx.render("acc_items.html", status, rows=acc.list_items(ctx.conn, include_inactive=True), locations=acc.list_locations(ctx.conn, include_inactive=True),
                      ierr=ierr or {}, ival=ival or {}, lerr=lerr or {}, lval=lval or {})


@handler(perm="acc.view")
def acc_items(ctx):
    return _items_page(ctx)


@handler(perm="acc.items.manage", post=True)
def acc_item_create(ctx):
    try:
        acc.create_item(ctx.conn, ctx.user, ctx.form)
    except ValidationFailed as e:
        return _items_page(ctx, ierr=e.errors, ival=ctx.form, status=422)
    ctx.flash("ok", "Accessory item added.")
    return ctx.redirect("/accessories/items")


@handler(perm="acc.items.manage", post=True)
def acc_item_update(ctx, item_id):
    try:
        acc.update_item(ctx.conn, ctx.user, int(item_id), ctx.form)
    except ValidationFailed as e:
        ctx.flash("error", "; ".join(e.errors.values()))
        return ctx.redirect("/accessories/items")
    ctx.flash("ok", "Item updated.")
    return ctx.redirect("/accessories/items")


@handler(perm="acc.items.manage", post=True)
def acc_location_create(ctx):
    try:
        acc.create_location(ctx.conn, ctx.user, ctx.form)
    except ValidationFailed as e:
        return _items_page(ctx, lerr=e.errors, lval=ctx.form, status=422)
    ctx.flash("ok", "Location added.")
    return ctx.redirect("/accessories/items")


@handler(perm="acc.view")
def acc_docs(ctx):
    t, s, q = _qs(ctx.request, "type"), _qs(ctx.request, "status"), _qs(ctx.request, "q")
    return ctx.render("acc_docs.html", rows=acc.list_docs(ctx.conn, t or None, s or None, q or None), t=t, s=s, q=q, types=acc.DOC_TYPES)


def _doc_form(ctx, doc_type, errors=None, values=None, lines=None, status=200):
    return ctx.render("acc_doc_new.html", status, doc_type=doc_type, type_label=acc.DOC_TYPES[doc_type], errors=errors or {}, values=values or {},
                      lines=lines or [("", "")] * 5, ref_types=acc.REF_TYPES, **_acc_lists(ctx.conn))


@handler(perm="acc.create")
def acc_doc_new(ctx):
    t = _qs(ctx.request, "type", "issue")
    if t not in acc.DOC_TYPES:
        t = "issue"
    return _doc_form(ctx, t)


def _form_lines(form, n=6):
    return [(form.get(f"item_{i}") or "", form.get(f"qty_{i}") or "") for i in range(1, n + 1)]


@handler(perm="acc.create", post=True)
def acc_doc_create(ctx):
    t = ctx.form.get("doc_type", "")
    if t not in acc.DOC_TYPES:
        raise ValidationFailed({"doc_type": "Unknown document type."})
    lines = _form_lines(ctx.form)
    try:
        did = acc.create_draft(ctx.conn, ctx.user, t, ctx.form, lines)
    except ValidationFailed as e:
        return _doc_form(ctx, t, e.errors, ctx.form, lines, 422)
    ctx.flash("ok", "Saved as a draft. Stock changes only when it is posted.")
    return ctx.redirect(f"/accessories/docs/{did}")


@handler(perm="acc.view")
def acc_doc_detail(ctx, doc_id):
    d = acc.get_doc(ctx.conn, int(doc_id))
    led = ctx.conn.execute("SELECT a.*, i.code, i.name, i.unit, i.decimals, l.name AS loc FROM acc_ledger a JOIN acc_items i ON i.id=a.item_id "
                           "JOIN acc_locations l ON l.id=a.location_id WHERE a.source_type='acc_doc' AND a.source_id=? ORDER BY a.id", (d["id"],)).fetchall()
    return ctx.render("acc_doc.html", d=d, lines=acc.doc_lines(ctx.conn, d["id"]), ledger=led, ref_types=acc.REF_TYPES, types=acc.DOC_TYPES)


@handler(perm="acc.post", post=True)
def acc_doc_post(ctx, doc_id):
    n = acc.post(ctx.conn, ctx.user, int(doc_id))
    ctx.flash("ok", f"{n} posted. Accessory stock updated.")
    return ctx.redirect(f"/accessories/docs/{doc_id}")


@handler(perm="acc.create", post=True)
def acc_doc_discard(ctx, doc_id):
    acc.discard(ctx.conn, ctx.user, int(doc_id))
    ctx.flash("ok", "Draft discarded.")
    return ctx.redirect("/accessories/docs")


@handler(perm="acc.reverse", post=True)
def acc_doc_reverse(ctx, doc_id):
    try:
        acc.reverse(ctx.conn, ctx.user, int(doc_id), ctx.form.get("reason", ""))
    except ValidationFailed as e:
        ctx.flash("error", "; ".join(e.errors.values()))
        return ctx.redirect(f"/accessories/docs/{doc_id}")
    ctx.flash("ok", "Document reversed with linked ledger entries.")
    return ctx.redirect(f"/accessories/docs/{doc_id}")


@handler(perm="acc.view")
def acc_ledger(ctx):
    rq = ctx.request
    item, loc, mv = _int(_qs(rq, "item")), _int(_qs(rq, "location")), _qs(rq, "movement")
    d1, d2 = _qs(rq, "date_from"), _qs(rq, "date_to")
    page, size = max(1, _int(_qs(rq, "page"), 1)), 50
    err = None
    try:
        lo, hi = day_range(ctx.cfg, d1, d2)
    except ValidationFailed as e:
        lo = hi = None
        err = "; ".join(e.errors.values())
    rows, count = acc.ledger_rows(ctx.conn, item_id=item or None, location_id=loc or None, movement=mv or None, ts_from=lo, ts_to=hi,
                                  limit=size, offset=(page - 1) * size)
    qs = "&".join(f"{k}={v}" for k, v in (("item", item or ""), ("location", loc or ""), ("movement", mv), ("date_from", d1), ("date_to", d2)) if v)
    return ctx.render("acc_ledger.html", rows=rows, count=count, page=page, size=size, item=item, loc=loc, mv=mv, d1=d1, d2=d2, err=err, qs=qs,
                      items=acc.list_items(ctx.conn, include_inactive=True), locations=acc.list_locations(ctx.conn, include_inactive=True), moves=MOVES)


G, P = ["GET"], ["POST"]
ROUTES = [
    Route("/accessories", acc_index, methods=G), Route("/accessories/items", acc_items, methods=G), Route("/accessories/items", acc_item_create, methods=P),
    Route("/accessories/items/{item_id:int}", acc_item_update, methods=P), Route("/accessories/locations", acc_location_create, methods=P),
    Route("/accessories/docs", acc_docs, methods=G), Route("/accessories/docs/new", acc_doc_new, methods=G), Route("/accessories/docs", acc_doc_create, methods=P),
    Route("/accessories/docs/{doc_id:int}", acc_doc_detail, methods=G), Route("/accessories/docs/{doc_id:int}/post", acc_doc_post, methods=P),
    Route("/accessories/docs/{doc_id:int}/discard", acc_doc_discard, methods=P), Route("/accessories/docs/{doc_id:int}/reverse", acc_doc_reverse, methods=P),
    Route("/accessories/ledger", acc_ledger, methods=G),
]
