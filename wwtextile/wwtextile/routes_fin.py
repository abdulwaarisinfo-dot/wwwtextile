"""Finance: bills, invoices, payments, expenses, allocations, statements. Money = integer paise; no tax, FX or ledger accounts (see BUSINESS_RULES.md)."""
from __future__ import annotations

from starlette.routing import Route

from .errors import ValidationFailed
from .permissions import can
from .services import finance as fin
from .webcore import _int, _qs, handler


def _parties(conn):
    return conn.execute("SELECT * FROM parties ORDER BY name").fetchall()


@handler(perm="fin.view")
def fin_index(ctx):
    d1, d2 = _qs(ctx.request, "date_from"), _qs(ctx.request, "date_to")
    try:
        s = fin.summary(ctx.conn, d1 or None, d2 or None)
        err = None
    except ValidationFailed as e:
        s, err = fin.summary(ctx.conn), "; ".join(e.errors.values())
    return ctx.render("fin_index.html", s=s, d1=d1, d2=d2, err=err, currency=ctx.cfg.currency)


@handler(perm="fin.view")
def fin_entries(ctx):
    r = ctx.request
    kind, status, party, q = _qs(r, "kind"), _qs(r, "status"), _int(_qs(r, "party")), _qs(r, "q")
    d1, d2 = _qs(r, "date_from"), _qs(r, "date_to")
    return ctx.render("fin_entries.html", rows=fin.list_entries(ctx.conn, kind or None, status or None, party or None, d1 or None, d2 or None, q or None),
                      kind=kind, status=status, party=party, q=q, d1=d1, d2=d2, kinds=fin.KINDS, parties=_parties(ctx.conn))


def _form(ctx, kind, errors=None, values=None, status=200):
    return ctx.render("fin_new.html", status, kind=kind, label=fin.KINDS[kind], errors=errors or {}, values=values or {}, kinds=fin.KINDS,
                      parties=_parties(ctx.conn), categories=fin.expense_categories(ctx.conn), currency=ctx.cfg.currency)


@handler(perm="fin.create")
def fin_new(ctx):
    k = _qs(ctx.request, "kind", "bill")
    return _form(ctx, k if k in fin.KINDS else "bill")


@handler(perm="fin.create", post=True)
def fin_create(ctx):
    k = ctx.form.get("kind", "")
    if k not in fin.KINDS:
        raise ValidationFailed({"kind": "Unknown entry type."})
    try:
        eid = fin.create_draft(ctx.conn, ctx.user, k, ctx.form, ctx.cfg.currency)
    except ValidationFailed as e:
        return _form(ctx, k, e.errors, ctx.form, 422)
    ctx.flash("ok", "Saved as a draft. It counts in balances only after it is posted.")
    return ctx.redirect(f"/finance/entries/{eid}")


@handler(perm="fin.view")
def fin_detail(ctx, entry_id):
    e = fin.get(ctx.conn, int(entry_id))
    kind = e["kind"]
    as_payment = kind in ("payment_out", "payment_in")
    allocs = fin.allocations_for(ctx.conn, e["id"], as_payment) if kind in ("payment_out", "payment_in", "bill", "invoice") else []
    open_docs = fin.open_documents(ctx.conn, e["party_id"], "bill" if kind == "payment_out" else "invoice") if as_payment and e["status"] == "posted" else []
    return ctx.render("fin_entry.html", e=e, kinds=fin.KINDS, allocs=allocs, open_docs=open_docs, as_payment=as_payment,
                      outstanding=fin.bill_outstanding(ctx.conn, e) if kind in ("bill", "invoice") and e["status"] == "posted" else None,
                      unallocated=fin.payment_unallocated(ctx.conn, e) if as_payment and e["status"] == "posted" else None,
                      separate=fin.separate_poster(ctx.conn))


@handler(perm="fin.post", post=True)
def fin_post(ctx, entry_id):
    n = fin.post(ctx.conn, ctx.user, int(entry_id))
    ctx.flash("ok", f"{n} posted.")
    return ctx.redirect(f"/finance/entries/{entry_id}")


@handler(perm="fin.create", post=True)
def fin_discard(ctx, entry_id):
    fin.discard(ctx.conn, ctx.user, int(entry_id))
    ctx.flash("ok", "Draft discarded.")
    return ctx.redirect("/finance/entries")


@handler(perm="fin.void", post=True)
def fin_void(ctx, entry_id):
    try:
        fin.void(ctx.conn, ctx.user, int(entry_id), ctx.form.get("reason", ""))
    except ValidationFailed as e:
        ctx.flash("error", "; ".join(e.errors.values()))
        return ctx.redirect(f"/finance/entries/{entry_id}")
    ctx.flash("ok", "Entry voided. The original record is kept.")
    return ctx.redirect(f"/finance/entries/{entry_id}")


@handler(perm="fin.allocate", post=True)
def fin_allocate(ctx, entry_id):
    try:
        fin.allocate(ctx.conn, ctx.user, int(entry_id), _int(ctx.form.get("bill_id")), ctx.form.get("amount", ""))
    except ValidationFailed as e:
        ctx.flash("error", "; ".join(e.errors.values()))
        return ctx.redirect(f"/finance/entries/{entry_id}")
    ctx.flash("ok", "Allocation recorded.")
    return ctx.redirect(f"/finance/entries/{entry_id}")


@handler(perm="fin.view")
def fin_party(ctx, party_id):
    d1, d2 = _qs(ctx.request, "date_from"), _qs(ctx.request, "date_to")
    err = None
    try:
        st = fin.statement(ctx.conn, int(party_id), d1 or None, d2 or None)
    except ValidationFailed as e:
        st, err = fin.statement(ctx.conn, int(party_id)), "; ".join(e.errors.values())
    return ctx.render("fin_party.html", st=st, bal=fin.party_balance(ctx.conn, int(party_id)), d1=d1, d2=d2, err=err, kinds=fin.KINDS)


G, P = ["GET"], ["POST"]
ROUTES = [
    Route("/finance", fin_index, methods=G), Route("/finance/entries", fin_entries, methods=G), Route("/finance/entries/new", fin_new, methods=G),
    Route("/finance/entries", fin_create, methods=P), Route("/finance/entries/{entry_id:int}", fin_detail, methods=G),
    Route("/finance/entries/{entry_id:int}/post", fin_post, methods=P), Route("/finance/entries/{entry_id:int}/discard", fin_discard, methods=P),
    Route("/finance/entries/{entry_id:int}/void", fin_void, methods=P), Route("/finance/entries/{entry_id:int}/allocate", fin_allocate, methods=P),
    Route("/finance/parties/{party_id:int}", fin_party, methods=G),
]
