"""Evidence-backed lot questions — RULE-BASED, NOT A LANGUAGE MODEL.

This is a deterministic keyword/pattern matcher over the same services the UI
uses. It exists so the DEMO-104 story can include an evidence-backed query
before the LLM tool layer (Pass 3) is built. It answers only the patterns
listed in SUPPORTED, states clearly when it cannot answer, and never invents
a number: every figure comes from inventory/trace/processing services, and
every answer lists the records it is based on.
"""
from __future__ import annotations

import re
import sqlite3

from ..errors import NotFound
from ..permissions import can, require
from ..quantity import fmt_qty
from . import inventory, processing, trace

ENGINE = "rule-based lookup (no language model connected)"
SUPPORTED = [
    "Where is lot DEMO-104? / How much is available in lot DEMO-104?",
    "Show the history of lot DEMO-104-F1",
    "What was the wastage / shrinkage / reconciliation for lot DEMO-104?",
    "Has lot DEMO-104-F1 been dispatched? Did the vehicle exit?",
    "Which processing jobs are overdue?",
    "Show reconciliation exceptions",
]
_TOKEN = re.compile(r"\b([A-Za-z][A-Za-z0-9]*-\d+(?:-[A-Za-z]\d*)?)\b")  # looks like a lot/doc reference
_LOTWORD = re.compile(r"\blot\s+#?([A-Za-z0-9][A-Za-z0-9._-]*)", re.I)


def _resolve_lot(conn, q: str):
    """Returns (lot_no | None, clarification | None).

    1. If an existing lot number appears in the question as a whole word, use the longest such match
       (so DEMO-104-F1 wins over DEMO-104).
    2. Otherwise treat 'lot <text>' as a partial reference and ask to disambiguate if several match."""
    low = q.lower()
    best = None
    for r in conn.execute("SELECT lot_no FROM lots"):
        lot_no = r["lot_no"]
        if re.search(r"(?<![A-Za-z0-9._-])" + re.escape(lot_no.lower()) + r"(?![A-Za-z0-9_-])", low):
            if best is None or len(lot_no) > len(best):
                best = lot_no
    if best:
        return best, None
    m = _LOTWORD.search(q)
    if m:
        needle = m.group(1)
        exact = conn.execute("SELECT lot_no FROM lots WHERE lot_no = ?", (needle,)).fetchone()
        if exact:
            return exact["lot_no"], None
        rows = conn.execute("SELECT lot_no FROM lots WHERE lot_no LIKE ? ORDER BY lot_no LIMIT 10", (f"%{needle}%",)).fetchall()
        if len(rows) == 1:
            return rows[0]["lot_no"], None
        if len(rows) > 1:
            return None, "More than one lot matches '" + needle + "': " + ", ".join(r["lot_no"] for r in rows) + ". Which one do you mean?"
        return None, f"No lot matches '{needle}'."
    return None, None


def _ev(label, url=None, ref=None):
    return {"label": label, "url": url, "ref": ref}


def ask(conn: sqlite3.Connection, user, question: str) -> dict:
    require(user, "ask.query")
    q = (question or "").strip()
    base = {"engine": ENGINE, "question": q, "evidence": [], "supported": True, "clarification": None, "lot_no": None}
    if not q:
        return {**base, "answer": "Type a question.", "supported": False}
    if len(q) > 300:
        return {**base, "answer": "Question is too long (max 300 characters).", "supported": False}
    low = q.lower()

    if re.search(r"overdue|late jobs|delayed", low):
        if not can(user["role"], "processing.view"):
            return {**base, "answer": "Your role cannot view processing jobs.", "supported": False}
        jobs = processing.list_jobs(conn, overdue_only=True)
        if not jobs:
            return {**base, "answer": "No open processing job is past its expected date."}
        return {**base, "answer": f"{len(jobs)} open job(s) are past their expected date: "
                + "; ".join(f"{j['job_no']} (lot {j['lot_no']}, due {j['expected_date']})" for j in jobs) + ".",
                "evidence": [_ev(j["job_no"], f"/processing/jobs/{j['id']}", "processing_job") for j in jobs]}
    if re.search(r"exception|discrepan", low) and not _TOKEN.search(q):
        if not can(user["role"], "processing.view"):
            return {**base, "answer": "Your role cannot view processing results.", "supported": False}
        rows = processing.reconciliation_exceptions(conn)
        if not rows:
            return {**base, "answer": "No posted processing result has an open reconciliation exception."}
        return {**base, "answer": f"{len(rows)} open reconciliation exception(s): "
                + "; ".join(f"{r['result_no']} on lot {r['lot_no']} — unexplained variance {fmt_qty(r['variance'])}" for r in rows) + ".",
                "evidence": [_ev(r["result_no"], f"/processing/jobs/{r['job_id']}", "processing_result") for r in rows]}

    lot_no, clar = _resolve_lot(conn, q)
    if clar:
        return {**base, "answer": clar, "clarification": clar, "supported": True}
    if lot_no is None:
        return {**base, "supported": False,
                "answer": "I can't answer that. I can only answer these kinds of questions (no language model is connected yet): "
                          + " | ".join(SUPPORTED)}
    if not can(user["role"], "stock.view"):
        return {**base, "answer": "Your role cannot view stock.", "supported": False}
    t = trace.lot_trace(conn, lot_no)
    lot, unit = t["lot"], t["lot"]["unit"]
    base["lot_no"] = lot_no
    ev = [_ev(f"Lot {lot_no}", f"/fabrics/lots/{lot_no}", "lot")]
    mine = [b for b in t["balances"] if b["lot_no"] == lot_no]

    if re.search(r"histor|trace|lifecycle|what happened|journey|timeline", low):
        if not can(user["role"], "ledger.view"):
            return {**base, "answer": "Your role cannot view the stock ledger, so lot history is not available to you.", "supported": False}
        lines = [f"{x['posted_at'][:16].replace('T', ' ')} UTC — {x['label']}: lot {x['lot_no']} "
                 f"{'+' if x['qty'] > 0 else '−'}{fmt_qty(abs(x['qty']))} {unit} at {x['loc_name']} ({x['doc_ref']})" for x in t["timeline"]]
        ev += [_ev(f"Ledger #{x['id']} · {x['doc_ref']}", x["url"], "stock_ledger") for x in t["timeline"]]
        return {**base, "answer": f"{len(lines)} ledger movement(s) across lot {lot_no} and related lots:\n" + "\n".join(lines), "evidence": ev}

    if re.search(r"reconcil|variance|wast|shrink|reject|accepted|loss|lost", low):
        if not can(user["role"], "processing.view"):
            return {**base, "answer": "Your role cannot view processing results.", "supported": False}
        if not t["results"]:
            return {**base, "answer": f"No processing result has been posted for lot {lot_no} or its related lots.", "evidence": ev}
        parts = []
        for r in t["results"]:
            parts.append(f"{r['result_no']} (job {r['job_no']}, issue {r['issue_no']}): issued {fmt_qty(r['issued_qty'])}; accepted "
                         f"{fmt_qty(r['accepted'])}, rejected {fmt_qty(r['rejected'])}, wastage {fmt_qty(r['wastage'])}, shrinkage "
                         f"{fmt_qty(r['shrinkage'])}, remaining in process {fmt_qty(r['remaining'])}, unexplained variance "
                         f"{fmt_qty(r['variance'])} → {r['reconciliation_status']}")
            ev.append(_ev(r["result_no"], f"/processing/jobs/{r['job_id']}", "processing_result"))
        return {**base, "answer": " ".join(parts) + f" (all in {unit})", "evidence": ev}

    if re.search(r"dispatch|gate|exit|vehicle|shipped|left|sent", low):
        if not can(user["role"], "dispatch.view"):
            return {**base, "answer": "Your role cannot view dispatches.", "supported": False}
        if not t["dispatches"]:
            return {**base, "answer": f"No dispatch includes lot {lot_no} or its related lots.", "evidence": ev}
        parts = []
        for d in t["dispatches"]:
            s = f"{d['doc_no']} to {d['customer_name']} is {d['status']}"
            if d["pass_no"]:
                s += f"; gate pass {d['pass_no']} ({d['pass_status']}), vehicle {d['vehicle_no']}"
                if d["exited_at"]:
                    s += f", exit recorded {d['exited_at'][:16].replace('T', ' ')} UTC"
            if d["deducted_on"]:
                s += f"; stock deducted at {d['deducted_on']}"
            parts.append(s)
            ev.append(_ev(d["doc_no"], f"/dispatch/{d['id']}", "dispatch"))
        return {**base, "answer": ". ".join(parts) + ".", "evidence": ev}

    # default: where is it / how much / overview
    if not mine:
        return {**base, "answer": f"Lot {lot_no} has no stock on hand in any location.", "evidence": ev}
    parts = [f"{b['lot_no']}: {fmt_qty(b['on_hand'])} {unit} at {b['name']} "
             f"({fmt_qty(b['reserved'])} reserved, {fmt_qty(b['available'])} available)" for b in mine]
    return {**base, "answer": "Current stock — " + "; ".join(parts) + ".", "evidence": ev}
