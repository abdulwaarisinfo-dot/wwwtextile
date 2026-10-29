"""Demo guide + demo reset. Only available when WW_ENV=demo (404 otherwise)."""
from __future__ import annotations

from starlette.routing import Route

from . import audit, demo_data
from .ai import documents, providers
from .errors import AppError, NotFound
from .permissions import can
from .webcore import handler

ROLE_TIPS = {
    "ceo": "Read-only overview: dashboard, reports, watchtower. Cannot change anything.",
    "manager1": "Creates processing jobs, approves dispatches, posts finance entries, runs Multihead orders.",
    "manager2": "A second manager, so maker-checker approvals (someone else must approve) can be tested.",
    "store1": "Receives fabric, issues it to processing, creates dispatches, posts accessory documents.",
    "store2": "Second storekeeper.",
    "prod1": "Records processing results (loss reconciliation) and Multihead production.",
    "prod2": "Second production user.",
    "gate1": "Issues gate passes, confirms vehicle exit, records inward/outward vehicles.",
    "gate2": "Second gate officer.",
    "fin1": "Drafts bills, invoices, payments and expenses (a manager posts them).",
}

WORKFLOW = [
    ("store1", "Receive fabric", "/fabrics/receipts/new", "Pick a supplier and fabric, enter a NEW lot number and quantity, save, then post. Stock appears in the Raw Store."),
    ("manager1", "Create a processing job", "/processing/jobs/new", "Choose that lot, a process (dyeing / printing / embroidery) and a job-work party."),
    ("store1", "Issue fabric to the job", "/processing", "Open the job, enter a quantity, save the issue and post it. Raw stock moves to Processing (WIP)."),
    ("prod1", "Record the result and reconcile losses", "/processing", "Enter accepted, rejected, wastage, shrinkage. The page shows the variance live; an unexplained gap becomes an open exception."),
    ("store1", "Create a dispatch from finished stock", "/dispatch/new", "The finished lot (name ends -F1) was created by the result. Pick a customer and quantity; the stock is reserved on approval."),
    ("manager2", "Approve the dispatch", "/dispatch", "Approval must come from a different person than the creator."),
    ("gate1", "Issue the gate pass, then confirm vehicle exit", "/gate", "Enter vehicle and driver. Stock leaves the books only at exit confirmation (default rule)."),
    ("fin1", "Invoice the customer", "/finance/entries/new", "Draft an invoice; a manager posts it. Then record a payment and allocate it."),
    ("ceo", "See it in reports", "/reports", "Stock by location, loss reconciliation, dispatches and gate register all reflect what you just did."),
]


def _status(ctx) -> list[dict]:
    cfg = ctx.cfg
    llm, stt, tts = providers.llm_status(cfg), providers.stt_status(cfg), providers.tts_status(cfg)
    tools = documents.tools_available()
    ocr = tools["tesseract"]
    return [
        dict(name="ERP workflows, database, permissions, audit trail, reports", state="Real", note="Everything on the other pages runs against a real database with real validation."),
        dict(name="Assistant (rule-based engine)", state="Real", note="Understands about 25 kinds of business question (losses, who owes whom, stock ageing, jobs at a dye house, Multihead, gate, dispatch, alerts, follow-up questions) by pattern matching over live data. It is not an AI model and does not pretend to be one; it only recognises these phrasings."),
        dict(name="Assistant (language model)", state="Connected" if llm["configured"] else "Not connected", note=llm["detail"]),
        dict(name="Voice input", state="Real (browser)", note="Uses your browser's own speech recognition (Chrome/Edge). Audio may be processed by the browser vendor. English only."),
        dict(name="Server speech-to-text", state="Connected" if stt["configured"] else "Not connected", note=stt["detail"]),
        dict(name="Server text-to-speech", state="Connected" if tts["configured"] else "Not connected", note=tts["detail"]),
        dict(name="Document Reader (OCR)", state="Real" if ocr else "Unavailable", note="Reads challan images/PDFs on this server and proposes fields; a person confirms. " + ("" if ocr else "tesseract is not installed on this host.")),
        dict(name="WhatsApp, email, SMS notifications", state="Not connected", note="Not built. Alerts are shown in the app (bell icon, Watchtower) only."),
        dict(name="Bank feeds, payment gateways, FBR/tax invoicing, accounting export", state="Not connected", note="Not built. Finance is a sub-ledger for bills, invoices, payments and expenses."),
        dict(name="Barcode / weighbridge / RFID / CCTV", state="Not connected", note="Not built. Quantities are typed in."),
    ]


@handler()
def demo_guide(ctx):
    if ctx.cfg.env != "demo":
        raise NotFound("Not found.")
    users = ctx.conn.execute("SELECT username, display_name, role FROM users WHERE active=1 AND username<>'admin' ORDER BY role, username").fetchall()
    import json
    return ctx.render("demo_guide.html", status_rows=_status(ctx), workflow=WORKFLOW, users=users, tips=ROLE_TIPS,
                      is_admin=can(ctx.user["role"], "user.manage"), public=ctx.cfg.public_demo, seed=ctx.cfg.demo_seed,
                      counts=demo_data.summary(ctx.conn))


@handler(perm="user.manage", post=True)
def demo_reset(ctx):
    if ctx.cfg.env != "demo":
        raise NotFound("Not found.")
    if (ctx.form.get("confirm") or "").strip() != "RESET":
        ctx.flash("err", "Type RESET in capitals to confirm. Nothing was changed.")
        return ctx.redirect("/demo")
    seed = None
    raw = (ctx.form.get("seed") or "").strip()
    if raw:
        if not raw.lstrip("-").isdigit() or abs(int(raw)) > 2**31:
            ctx.flash("err", "Seed must be a whole number.")
            return ctx.redirect("/demo")
        seed = int(raw)
    audit.record_standalone(ctx.conn, ctx.user, "demo.reset_requested", entity_ref=str(seed or ctx.cfg.demo_seed))
    ctx.conn.close()
    try:
        demo_data.reset(ctx.cfg, seed)
    except AppError as e:
        ctx.flash("err", str(e))
        return ctx.redirect("/demo")
    return ctx.redirect("/login")        # every session ended with the data


G, P = ["GET"], ["POST"]
ROUTES = [Route("/demo", demo_guide, methods=G), Route("/demo/reset", demo_reset, methods=P)]
