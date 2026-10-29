"""Guidance layer: 'what needs you now', the business pipeline, and per-page help.

Everything here is computed from the same records the pages show, using fixed rules.
It is not an AI model and makes no judgement beyond the rules written below; each
item says why it appears and links to the records behind it. Items are only built
from data the signed-in user is allowed to view."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime

from ..permissions import can
from ..money import fmt_money
from ..quantity import fmt_qty
from . import accessories, finance, multihead, processing, watchtower

SEV = {"high": 0, "medium": 1, "low": 2}


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _item(sev, title, why, url, cta, *, perm=None, user=None, who="someone with the right role"):
    mine = perm is None or can(user["role"], perm)
    return {"sev": sev, "title": title, "why": why, "url": url, "cta": cta if mine else "View",
            "mine": mine, "waiting_on": None if mine else who}


def greeting(user, tz) -> str:
    h = datetime.now(tz).hour
    part = "morning" if h < 12 else "afternoon" if h < 17 else "evening"
    return f"Good {part}, {user['display_name'].split()[0]}"


def actions(conn: sqlite3.Connection, user, currency: str = "") -> list[dict]:
    r = user["role"]
    out: list[dict] = []
    c1 = lambda sql, *a: conn.execute(sql, a).fetchone()[0]

    if can(r, "alert.view"):
        crit = [a for a in watchtower.list_alerts(conn, "active") if a["severity"] == "critical"]
        if crit:
            out.append(_item("high", f"{_plural(len(crit), 'critical alert')} open",
                             "; ".join(a["title"] for a in crit[:2]) + ("…" if len(crit) > 2 else ""),
                             "/watchtower?severity=critical", "Review alerts", perm="alert.manage", user=user, who="a manager"))
    if can(r, "processing.view"):
        today = date.today()
        late = [j for j in processing.list_jobs(conn, status="open") if j["overdue"]]
        if late:
            late.sort(key=lambda j: j["expected_date"])
            days = (today - date.fromisoformat(late[0]["expected_date"])).days
            out.append(_item("high", f"{_plural(len(late), 'processing job')} overdue",
                             f"Oldest: {late[0]['job_no']} at {late[0]['party_name'] or 'unassigned party'}, {days} day{'s' if days != 1 else ''} past expected date. Chase the party or record the result.",
                             "/processing?overdue=1", "See overdue jobs"))
        exc = processing.reconciliation_exceptions(conn)
        if exc:
            worst = max(exc, key=lambda x: x["variance"])
            out.append(_item("high", f"{_plural(len(exc), 'unexplained loss', 'unexplained losses')} to review",
                             f"Largest: {worst['job_no']} / lot {worst['lot_no']} with {fmt_qty(worst['variance'])} unaccounted. Find where it went or record the reason.",
                             "/processing?view=exceptions", "Review exceptions"))
    if can(r, "dispatch.view"):
        for status, noun, why, cta, perm, who, url in (
            ("draft", "waiting for approval", "Nothing is reserved until approved, so the goods can still be promised twice.", "Review and approve", "dispatch.approve", "a manager", "/dispatch?status=draft"),
            ("approved", "approved, need a gate pass", "Stock is reserved. The vehicle cannot leave without a gate pass.", "Issue gate pass", "gate.pass.create", "gate staff", "/dispatch?status=approved"),
            ("pass_issued", "with a pass, vehicle not yet exited", "Confirm exit at the gate so the stock leaves the ledger.", "Confirm exit", "gate.exit.confirm", "gate staff", "/gate?status=issued"),
        ):
            n = c1("SELECT COUNT(*) FROM dispatches WHERE status=?", status)
            if n:
                docs = [x[0] for x in conn.execute("SELECT doc_no FROM dispatches WHERE status=? ORDER BY id LIMIT 3", (status,))]
                out.append(_item("medium", f"{_plural(n, 'dispatch', 'dispatches')} {noun}",
                                 f"{why} ({', '.join(docs)}{'…' if n > 3 else ''})", url, cta, perm=perm, user=user, who=who))
    if can(r, "gate.view"):
        w = c1("SELECT COUNT(*) FROM gate_entries WHERE status='awaiting_approval'")
        if w:
            out.append(_item("medium", f"{_plural(w, 'gate entry', 'gate entries')} awaiting approval",
                             "Outward vehicles cannot leave until approved.", "/gate/entries?status=awaiting_approval", "Approve entries",
                             perm="gate.entry.approve", user=user, who="a manager"))
    if can(r, "stock.view") or can(r, "receipt.create"):
        n = c1("SELECT COUNT(*) FROM goods_receipts WHERE status='draft'")
        if n:
            out.append(_item("medium", f"{_plural(n, 'goods receipt')} saved but not posted",
                             "Drafts do not count as stock. Post them so the fabric can be issued for processing.", "/fabrics/receipts?status=draft", "Check and post",
                             perm="receipt.post", user=user, who="a manager or store in-charge"))
    if can(r, "mh.view"):
        late = [o for o in multihead.list_orders(conn, status="open") if o["overdue"]]
        if late:
            out.append(_item("medium", f"{_plural(len(late), 'Multihead order')} past due",
                             ", ".join(o["order_no"] for o in late[:3]), "/multihead?status=open", "Open orders"))
    if can(r, "acc.view"):
        low = accessories.low_stock(conn)
        if low:
            out.append(_item("medium", f"{_plural(len(low), 'accessory item')} at or below reorder level",
                             ", ".join(x["item"]["name"] for x in low[:3]) + ("…" if len(low) > 3 else ""), "/accessories?low=1", "See low stock"))
    if can(r, "fin.view"):
        n = c1("SELECT COUNT(*) FROM fin_entries WHERE status='draft'")
        if n:
            out.append(_item("low", f"{_plural(n, 'finance entry', 'finance entries')} in draft",
                             "Drafts are not counted in balances until posted.", "/finance/entries?status=draft", "Review drafts",
                             perm="fin.post", user=user, who="the finance approver"))
        best = None
        for p in conn.execute("SELECT id FROM parties WHERE kind='customer'"):
            b = finance.party_balance(conn, p["id"])
            if b["balance"] > 0 and (best is None or b["balance"] > best["balance"]):
                best = b
        if best:
            out.append(_item("low", f"Largest receivable: {best['party']['name']}",
                             f"{fmt_money(best['balance'], currency)} still to collect.", f"/finance/party/{best['party']['id']}", "Open statement"))
    out.sort(key=lambda x: (SEV[x["sev"]], not x["mine"]))
    return out


def pipeline(conn: sqlite3.Connection, user) -> list[dict]:
    """One line of the business flow with live counts, so a newcomer sees where work is sitting."""
    r = user["role"]
    c1 = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    steps = []
    def add(label, n, url, hint, ok):
        if ok:
            steps.append({"label": label, "n": n, "url": url, "hint": hint})
    add("Receive", c1("SELECT COUNT(*) FROM goods_receipts WHERE status='draft'"), "/fabrics/receipts?status=draft", "drafts to post", can(r, "stock.view") or can(r, "receipt.create"))
    add("Process", c1("SELECT COUNT(*) FROM processing_jobs WHERE status='open'"), "/processing?status=open", "jobs out", can(r, "processing.view"))
    add("Reconcile", len(processing.reconciliation_exceptions(conn)) if can(r, "processing.view") else 0, "/processing?view=exceptions", "to explain", can(r, "processing.view"))
    add("Approve", c1("SELECT COUNT(*) FROM dispatches WHERE status='draft'"), "/dispatch?status=draft", "dispatches", can(r, "dispatch.view"))
    add("Gate pass", c1("SELECT COUNT(*) FROM dispatches WHERE status='approved'"), "/dispatch?status=approved", "to issue", can(r, "dispatch.view"))
    add("Exit", c1("SELECT COUNT(*) FROM dispatches WHERE status='pass_issued'"), "/gate?status=issued", "vehicles", can(r, "dispatch.view"))
    return steps


# ------------------------------------------------------------------ page help
# (prefix, title, what it is for, when to use it, steps, next)
HELP = [
    ("/fabrics/receipts/new", "New goods receipt", "Record fabric arriving at the gate.", "When a supplier's vehicle has been checked in.",
     ["Pick the supplier and give the lot a number.", "Enter the received quantity and the quantity on the delivery note.", "Save as draft, then post. Stock changes only when posted."], "Issue the lot to a dye house or printer from Textile Processing."),
    ("/fabrics", "Fabrics and stock", "Shows what is on hand, where, per lot.", "Before promising stock to a customer or issuing it to a job.",
     ["Filter by raw, kora, finished or reject.", "Open a lot to see its whole history: receipt, jobs, losses, dispatch."], "Open a lot, or start a goods receipt."),
    ("/processing/jobs/new", "New processing job", "Sends a raw lot to a dye house, printer or embroiderer.", "When you decide a lot should be processed.",
     ["Choose the lot and the job-work party.", "Set the expected return date. Overdue jobs are flagged from this date.", "After saving, issue the fabric on the job page."], "Issue fabric on the job page, then wait for the result."),
    ("/processing", "Textile processing", "Tracks fabric sent out and what came back.", "Daily, to chase late jobs and explain losses.",
     ["Open a job to issue fabric or record the result.", "Result = finished + rejects + wastage + shrinkage. Anything unexplained is flagged as an exception.", "Post the result to bring finished stock into the ledger."], "Record results for jobs that have returned."),
    ("/dispatch", "Dispatch", "Sends finished stock to customers.", "When a customer order is ready to leave.",
     ["Create a draft, then a manager approves it (this reserves the stock).", "Issue a gate pass for the vehicle.", "Gate confirms exit; the stock leaves the ledger."], "Watch the pipeline: approve, pass, exit."),
    ("/gate", "Gate management", "Controls every vehicle in and out.", "At the gate, all day.",
     ["Confirm exit for vehicles carrying a gate pass.", "Log other inward and outward vehicles under Gate entries.", "Outward entries need approval before the vehicle can leave."], "Confirm waiting exits first."),
    ("/multihead", "Multihead embroidery", "Orders, machines and thread use for the Multihead floor.", "When an embroidery order starts, runs or finishes.",
     ["Create an order, assign a machine.", "Log production and thread consumption as work proceeds.", "Close the order when finished."], "Log today's output on open orders."),
    ("/accessories", "Accessory store", "Buttons, zips, labels and packing, by location.", "When accessories arrive, are issued to production or run low.",
     ["Receive or issue using a document; stock moves when posted.", "Set a reorder level so low items are flagged."], "Review items at or below reorder level."),
    ("/finance", "Finance", "Bills, invoices, payments and expenses with party balances.", "Weekly for collections, and whenever money moves.",
     ["Create an entry, then post it. Drafts are not counted.", "Open a party to see the statement and allocate payments."], "Collect from the largest receivable first."),
    ("/watchtower", "Watchtower", "Rule-based alerts: overdue jobs, losses, stale drafts, low stock.", "At the start of the day and after bulk changes.",
     ["Acknowledge to say you are on it.", "Resolve or dismiss with a note; auto-resolves when the condition clears."], "Start with critical alerts."),
    ("/reports", "Reports", "Printable and exportable summaries from posted records.", "For reviews, client meetings and month end.",
     ["Pick a report and a period.", "Export CSV if you need to work in a spreadsheet."], "Choose the period first."),
    ("/ledger", "Stock ledger", "Every stock movement, append-only.", "To prove where a quantity came from.",
     ["Filter by lot or location.", "Corrections appear as reversing rows; nothing is edited."], "Open the lot from any row."),
    ("/assistant", "Assistant", "Ask questions about live records; it prepares drafts but never posts.", "When it is quicker to ask than to click.",
     ["Pick a suggested question or type your own.", "Follow-ups like 'where did it go next?' use the previous answer.", "Drafts open for you to check, then you post them."], "Try a suggested question."),
    ("/", "Dashboard", "Your day at a glance: what needs action, where work is sitting, and the trends.", "Start here every shift.",
     ["Read 'Needs you now' top to bottom.", "Use the flow strip to see which stage is backed up.", "Open any figure to see the records behind it."], "Do the first item under 'Needs you now'."),
]


def page_help(path: str) -> dict | None:
    for prefix, title, what, when, steps, nxt in sorted(HELP, key=lambda h: -len(h[0])):
        if path == prefix or (prefix != "/" and path.startswith(prefix + "/")) or (prefix != "/" and path.startswith(prefix) and prefix.endswith("new")):
            return {"title": title, "what": what, "when": when, "steps": steps, "next": nxt}
    return None


ROLE_FOCUS = {
    "store": "Your job: receive fabric accurately and keep the accessory store counted.",
    "production": "Your job: issue lots to jobs, chase returns and record results honestly.",
    "gate": "Your job: only let vehicles with an approved pass or entry leave.",
    "finance": "Your job: post entries, match payments, and chase receivables.",
    "manager": "Your job: approve what is waiting and clear exceptions.",
    "ceo": "You see everything; start with exceptions and receivables.",
    "admin": "You manage users, master data and settings.",
}


def role_focus(role: str) -> str:
    for k, v in ROLE_FOCUS.items():
        if k in role:
            return v
    return ""
