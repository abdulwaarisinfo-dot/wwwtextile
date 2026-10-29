"""Finance sub-ledger: bills, invoices, payments, expenses, allocations, balances, statements.

Scope is deliberately limited to what is defensible without owner-defined accounting policy: no tax, no FBR,
no chart of accounts, no pricing. Money is integer paisa. Finance records never touch stock.
Balances are derived (never stored) so every figure traces to posted entries.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date

from .. import audit
from ..db import get_setting, next_number, now_iso, transaction
from ..errors import BusinessRuleViolation, NotFound, ValidationFailed
from ..money import MoneyError, fmt_money, parse_money
from ..permissions import require
from . import notifications

KINDS = {"bill": "Supplier bill (payable)", "invoice": "Customer invoice (receivable)", "payment_out": "Payment made",
         "payment_in": "Payment received", "expense": "Expense"}
PAYABLE_PARTY_KINDS = ("supplier", "job_work")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _c(v) -> str:
    return str(v if v is not None else "").strip()


def separate_poster(conn) -> bool:
    return get_setting(conn, "finance.require_separate_poster", "1") == "1"


def _valid_date(s: str) -> bool:
    if not DATE_RE.match(s):
        return False
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return False
    return date(2000, 1, 1) <= d <= date(2100, 12, 31)


def get(conn, entry_id: int):
    r = conn.execute(
        "SELECT f.*, p.name AS party_name, p.kind AS party_kind, cu.username AS created_by_name, pu.username AS posted_by_name, "
        "vu.username AS voided_by_name FROM fin_entries f LEFT JOIN parties p ON p.id=f.party_id JOIN users cu ON cu.id=f.created_by "
        "LEFT JOIN users pu ON pu.id=f.posted_by LEFT JOIN users vu ON vu.id=f.voided_by WHERE f.id=?", (entry_id,)).fetchone()
    if r is None:
        raise NotFound("Finance entry not found.")
    return r


def list_entries(conn, kind=None, status=None, party_id=None, date_from=None, date_to=None, q=None, limit=300):
    sql = "SELECT f.*, p.name AS party_name FROM fin_entries f LEFT JOIN parties p ON p.id=f.party_id WHERE 1=1 "
    a: list = []
    for col, v in (("f.kind", kind), ("f.status", status), ("f.party_id", party_id)):
        if v:
            sql += f"AND {col}=? "; a.append(v)
    if date_from:
        sql += "AND f.entry_date >= ? "; a.append(date_from)
    if date_to:
        sql += "AND f.entry_date <= ? "; a.append(date_to)
    if q:
        sql += "AND (f.entry_no LIKE ? OR f.reference LIKE ? OR f.doc_ref_no LIKE ? OR f.description LIKE ? OR p.name LIKE ? OR f.category LIKE ?) "
        a += [f"%{q}%"] * 6
    return conn.execute(sql + "ORDER BY f.entry_date DESC, f.id DESC LIMIT ?", a + [limit]).fetchall()


def create_draft(conn, user, kind: str, form: dict, currency: str) -> int:
    require(user, "fin.create")
    if kind not in KINDS:
        raise ValidationFailed({"kind": "Unknown entry type."})
    errors: dict[str, str] = {}
    party_id = int(_c(form.get("party_id"))) if _c(form.get("party_id")).isdigit() else None
    if kind != "expense":
        party = conn.execute("SELECT * FROM parties WHERE id=?", (party_id or 0,)).fetchone()
        want = PAYABLE_PARTY_KINDS if kind in ("bill", "payment_out") else ("customer",)
        if party is None or party["kind"] not in want:
            errors["party_id"] = "Choose a " + ("supplier / job-work party." if kind in ("bill", "payment_out") else "customer.")
    else:
        party_id = None
    try:
        amount = parse_money(form.get("amount"))
    except MoneyError as e:
        errors["amount"] = str(e)
        amount = 0
    edate = _c(form.get("entry_date"))
    if not _valid_date(edate):
        errors["entry_date"] = "Enter a valid date (YYYY-MM-DD)."
    category = " ".join(_c(form.get("category")).split())
    if kind == "expense" and not (2 <= len(category) <= 40):
        errors["category"] = "Expense category is required (2-40 characters)."
    method = " ".join(_c(form.get("method")).split())[:30]
    if kind in ("payment_out", "payment_in") and not method:
        errors["method"] = "Payment method is required (e.g. cash, bank transfer, cheque)."
    reference = _c(form.get("reference"))[:60]
    dr_type, dr_no = _c(form.get("doc_ref_type")), _c(form.get("doc_ref_no"))
    if dr_type or dr_no:
        if dr_type not in ("goods_receipt", "dispatch") or not dr_no:
            errors["doc_ref_no"] = "Choose a document type and enter its number."
        else:
            tbl = "goods_receipts" if dr_type == "goods_receipt" else "dispatches"
            if not conn.execute(f"SELECT 1 FROM {tbl} WHERE doc_no=?", (dr_no,)).fetchone():
                errors["doc_ref_no"] = f"{dr_no} does not exist."
    description = _c(form.get("description"))[:200]
    if errors:
        raise ValidationFailed(errors)
    with transaction(conn):
        other = conn.execute("SELECT currency FROM fin_entries WHERE currency <> ? LIMIT 1", (currency,)).fetchone()
        if other:
            raise BusinessRuleViolation(f"Existing finance entries are in {other['currency']}; the configured currency is {currency}. "
                                        "Mixed currencies are not supported.")
        no = next_number(conn, "FIN", "FIN")
        cur = conn.execute(
            "INSERT INTO fin_entries(entry_no,kind,party_id,entry_date,amount_paise,currency,category,method,reference,doc_ref_type,doc_ref_no,"
            "description,status,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)",
            (no, kind, party_id, edate, amount, currency, category or None, method or None, reference or None, dr_type or None,
             dr_no or None, description or None, user["id"], now_iso()))
        audit.record(conn, user, "fin.create_draft", "fin_entry", no, detail={"kind": kind, "amount": fmt_money(amount, currency)})
        notifications.notify_permission(conn, "fin.post", title=f"Finance entry {no} awaits posting",
                                        body=f"{KINDS[kind]} · {fmt_money(amount, currency)}", url=f"/finance/entries/{cur.lastrowid}",
                                        dedupe_key=f"fin-post-{cur.lastrowid}")
        return cur.lastrowid


def post(conn, user, entry_id: int) -> str:
    require(user, "fin.post")
    with transaction(conn):
        f = conn.execute("SELECT * FROM fin_entries WHERE id=?", (entry_id,)).fetchone()
        if f is None:
            raise NotFound("Finance entry not found.")
        if f["status"] != "draft":
            raise BusinessRuleViolation(f"{f['entry_no']} is {f['status']}; only drafts can be posted.")
        if separate_poster(conn) and f["created_by"] == user["id"]:
            raise BusinessRuleViolation("The person who created this entry cannot post it (separate poster is required).")
        conn.execute("UPDATE fin_entries SET status='posted', posted_by=?, posted_at=? WHERE id=?", (user["id"], now_iso(), entry_id))
        audit.record(conn, user, "fin.post", "fin_entry", f["entry_no"], detail={"amount": fmt_money(f["amount_paise"], f["currency"])})
        return f["entry_no"]


def discard(conn, user, entry_id: int) -> None:
    require(user, "fin.create")
    with transaction(conn):
        f = conn.execute("SELECT * FROM fin_entries WHERE id=?", (entry_id,)).fetchone()
        if f is None:
            raise NotFound("Finance entry not found.")
        if f["status"] != "draft":
            raise BusinessRuleViolation("Only drafts can be discarded. Posted entries must be voided.")
        conn.execute("UPDATE fin_entries SET status='discarded' WHERE id=?", (entry_id,))
        audit.record(conn, user, "fin.discard", "fin_entry", f["entry_no"])


def void(conn, user, entry_id: int, reason: str) -> None:
    require(user, "fin.void")
    reason = _c(reason)
    if len(reason) < 5:
        raise ValidationFailed({"reason": "A reason of at least 5 characters is required."})
    with transaction(conn):
        f = conn.execute("SELECT * FROM fin_entries WHERE id=?", (entry_id,)).fetchone()
        if f is None:
            raise NotFound("Finance entry not found.")
        if f["status"] != "posted":
            raise BusinessRuleViolation(f"{f['entry_no']} is {f['status']}; only posted entries can be voided.")
        if f["kind"] in ("bill", "invoice") and allocated_to_bill(conn, entry_id):
            raise BusinessRuleViolation("This entry has payments allocated to it. Void those payments first.")
        conn.execute("UPDATE fin_entries SET status='voided', voided_by=?, voided_at=?, void_reason=? WHERE id=?",
                     (user["id"], now_iso(), reason, entry_id))
        audit.record(conn, user, "fin.void", "fin_entry", f["entry_no"], detail={"reason": reason})


# ------------------------------------------------------------------ allocations
def allocated_to_bill(conn, bill_id: int) -> int:
    return conn.execute("SELECT COALESCE(SUM(a.amount_paise),0) AS s FROM fin_allocations a JOIN fin_entries p ON p.id=a.payment_id "
                        "WHERE a.bill_id=? AND p.status='posted'", (bill_id,)).fetchone()["s"]


def allocated_from_payment(conn, payment_id: int) -> int:
    return conn.execute("SELECT COALESCE(SUM(a.amount_paise),0) AS s FROM fin_allocations a JOIN fin_entries b ON b.id=a.bill_id "
                        "WHERE a.payment_id=? AND b.status='posted'", (payment_id,)).fetchone()["s"]


def bill_outstanding(conn, bill) -> int:
    return bill["amount_paise"] - allocated_to_bill(conn, bill["id"])


def payment_unallocated(conn, payment) -> int:
    return payment["amount_paise"] - allocated_from_payment(conn, payment["id"])


def open_documents(conn, party_id: int, kind: str):
    """Posted bills/invoices with outstanding amount > 0."""
    out = []
    for b in conn.execute("SELECT * FROM fin_entries WHERE party_id=? AND kind=? AND status='posted' ORDER BY entry_date, id", (party_id, kind)):
        o = bill_outstanding(conn, b)
        if o > 0:
            out.append({"entry": b, "outstanding": o})
    return out


def allocations_for(conn, entry_id: int, as_payment: bool):
    col, other = ("payment_id", "bill_id") if as_payment else ("bill_id", "payment_id")
    return conn.execute(f"SELECT a.*, e.entry_no AS other_no, e.id AS other_id, e.status AS other_status FROM fin_allocations a "
                        f"JOIN fin_entries e ON e.id=a.{other} WHERE a.{col}=? ORDER BY a.id", (entry_id,)).fetchall()


def allocate(conn, user, payment_id: int, bill_id: int, amount_text: str) -> None:
    require(user, "fin.allocate")
    try:
        amount = parse_money(amount_text)
    except MoneyError as e:
        raise ValidationFailed({"amount": str(e)}) from None
    with transaction(conn):
        p = conn.execute("SELECT * FROM fin_entries WHERE id=?", (payment_id,)).fetchone()
        b = conn.execute("SELECT * FROM fin_entries WHERE id=?", (bill_id,)).fetchone()
        if p is None or b is None:
            raise NotFound("Entry not found.")
        pair = {("payment_out", "bill"), ("payment_in", "invoice")}
        if (p["kind"], b["kind"]) not in pair:
            raise BusinessRuleViolation("A payment made settles a supplier bill; a payment received settles a customer invoice.")
        if p["status"] != "posted" or b["status"] != "posted":
            raise BusinessRuleViolation("Both the payment and the bill/invoice must be posted.")
        if p["party_id"] != b["party_id"]:
            raise BusinessRuleViolation("The payment and the bill/invoice belong to different parties.")
        free = payment_unallocated(conn, p)
        if amount > free:
            raise BusinessRuleViolation(f"Only {fmt_money(free, p['currency'])} of this payment is unallocated.")
        out = bill_outstanding(conn, b)
        if amount > out:
            raise BusinessRuleViolation(f"{b['entry_no']} has only {fmt_money(out, b['currency'])} outstanding.")
        try:
            conn.execute("INSERT INTO fin_allocations(payment_id,bill_id,amount_paise,created_by,created_at) VALUES (?,?,?,?,?)",
                         (payment_id, bill_id, amount, user["id"], now_iso()))
        except sqlite3.IntegrityError:
            raise BusinessRuleViolation("This payment is already allocated to that bill/invoice.") from None
        audit.record(conn, user, "fin.allocate", "fin_entry", p["entry_no"], detail={"to": b["entry_no"], "amount": fmt_money(amount, p["currency"])})


# ------------------------------------------------------------------ balances and statements
def party_balance(conn, party_id: int, as_of: str | None = None) -> dict:
    party = conn.execute("SELECT * FROM parties WHERE id=?", (party_id,)).fetchone()
    if party is None:
        raise NotFound("Party not found.")
    debit_kind, credit_kind = ("invoice", "payment_in") if party["kind"] == "customer" else ("bill", "payment_out")
    sql = "SELECT COALESCE(SUM(amount_paise),0) AS s FROM fin_entries WHERE party_id=? AND status='posted' AND kind=? "
    a = [party_id]
    if as_of:
        sql += "AND entry_date <= ? "
    def total(kind):
        return conn.execute(sql, a[:1] + [kind] + ([as_of] if as_of else [])).fetchone()["s"]
    charged, settled = total(debit_kind), total(credit_kind)
    return {"party": party, "charged": charged, "settled": settled, "balance": charged - settled,
            "direction": "receivable" if party["kind"] == "customer" else "payable",
            "charged_label": "Invoiced" if party["kind"] == "customer" else "Billed",
            "settled_label": "Received" if party["kind"] == "customer" else "Paid"}


def statement(conn, party_id: int, date_from: str | None = None, date_to: str | None = None) -> dict:
    party = conn.execute("SELECT * FROM parties WHERE id=?", (party_id,)).fetchone()
    if party is None:
        raise NotFound("Party not found.")
    plus = ("invoice",) if party["kind"] == "customer" else ("bill",)
    minus = ("payment_in",) if party["kind"] == "customer" else ("payment_out",)
    opening = 0
    if date_from:
        before = conn.execute("SELECT kind, COALESCE(SUM(amount_paise),0) AS s FROM fin_entries WHERE party_id=? AND status='posted' AND entry_date < ? GROUP BY kind",
                              (party_id, date_from)).fetchall()
        opening = sum(r["s"] for r in before if r["kind"] in plus) - sum(r["s"] for r in before if r["kind"] in minus)
    sql = "SELECT * FROM fin_entries WHERE party_id=? AND status='posted' AND kind IN (%s) " % ",".join("?" * (len(plus) + len(minus)))
    a: list = [party_id, *plus, *minus]
    if date_from:
        sql += "AND entry_date >= ? "; a.append(date_from)
    if date_to:
        sql += "AND entry_date <= ? "; a.append(date_to)
    rows, running = [], opening
    for e in conn.execute(sql + "ORDER BY entry_date, id", a):
        signed = e["amount_paise"] if e["kind"] in plus else -e["amount_paise"]
        running += signed
        rows.append({"entry": e, "signed": signed, "running": running})
    return {"party": party, "opening": opening, "rows": rows, "closing": running, "date_from": date_from, "date_to": date_to,
            "debit_total": sum(r["signed"] for r in rows if r["signed"] > 0), "credit_total": -sum(r["signed"] for r in rows if r["signed"] < 0)}


def summary(conn, date_from: str | None = None, date_to: str | None = None) -> dict:
    """Outstanding balances (as of date_to or now) and period activity. Payables/receivables sum per-party balances."""
    payables = receivables = 0
    per_party = []
    for p in conn.execute("SELECT * FROM parties ORDER BY name"):
        b = party_balance(conn, p["id"], as_of=date_to)
        if b["charged"] or b["settled"]:
            per_party.append(b)
            if b["direction"] == "payable":
                payables += b["balance"]
            else:
                receivables += b["balance"]
    def period(kind):
        sql, a = "SELECT COALESCE(SUM(amount_paise),0) AS s FROM fin_entries WHERE status='posted' AND kind=? ", [kind]
        if date_from:
            sql += "AND entry_date >= ? "; a.append(date_from)
        if date_to:
            sql += "AND entry_date <= ? "; a.append(date_to)
        return conn.execute(sql, a).fetchone()["s"]
    sql, a = "SELECT category, SUM(amount_paise) AS s FROM fin_entries WHERE status='posted' AND kind='expense' ", []
    if date_from:
        sql += "AND entry_date >= ? "; a.append(date_from)
    if date_to:
        sql += "AND entry_date <= ? "; a.append(date_to)
    expenses = conn.execute(sql + "GROUP BY category ORDER BY s DESC", a).fetchall()
    drafts = conn.execute("SELECT COUNT(*) AS c FROM fin_entries WHERE status='draft'").fetchone()["c"]
    return {"payables": payables, "receivables": receivables, "per_party": per_party, "billed": period("bill"), "invoiced": period("invoice"),
            "paid": period("payment_out"), "received": period("payment_in"), "expenses": expenses,
            "expenses_total": sum(r["s"] for r in expenses), "drafts": drafts}


def expense_categories(conn):
    return [r["category"] for r in conn.execute("SELECT DISTINCT category FROM fin_entries WHERE kind='expense' AND category IS NOT NULL ORDER BY category")]
