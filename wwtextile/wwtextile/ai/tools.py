"""Allow-listed assistant tools.

The assistant (rule engine or LLM) can ONLY act through these functions. Each one:
  * declares a JSON-schema-like parameter spec that is validated before execution (unknown keys rejected);
  * declares the permission it needs, checked again at execution time with the CURRENT user (never trusts history);
  * calls the same services the UI uses; it has no SQL / shell / file access;
  * returns facts + evidence links from authoritative records; nothing is invented.
Write-type tools only ever create DRAFTS (no stock effect); posting always happens in the normal screens.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from .. import audit
from ..db import now_iso
from ..errors import AppError, BusinessRuleViolation, NotFound, PermissionDenied, ValidationFailed
from ..money import fmt_money
from ..permissions import can
from ..quantity import QuantityError, fmt_qty, parse_qty
from ..services import (accessories, dispatch, finance, gate_entries, inventory, multihead, processing, receipts, reports,
                        trace, watchtower)


@dataclass
class ToolResult:
    facts: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    open_url: str | None = None
    drafts: list[dict] = field(default_factory=list)
    clarify: dict | None = None            # {"question": str, "options": [{"label","value"}]}
    cards: list[dict] = field(default_factory=list)   # {"title","columns":[..],"rows":[[..]]}
    unsupported: str | None = None
    ok: bool = True

    def as_model_json(self, limit: int = 6000) -> str:
        """What an LLM is allowed to see: facts + compact data; never raw DB access."""
        payload = {"facts": self.facts, "data": self.data, "evidence": [e["label"] for e in self.evidence][:20],
                   "clarify": self.clarify, "unsupported": self.unsupported, "drafts": [d["label"] for d in self.drafts]}
        s = json.dumps(payload, default=str)
        return s if len(s) <= limit else s[:limit] + '..."truncated"'


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, dict]
    required: list[str]
    perm: str | None
    fn: Callable[..., ToolResult]
    writes: bool = False

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": {"type": "object", "properties": self.properties, "required": self.required, "additionalProperties": False}}


class Ctx:
    """Per-call context for tools: time zone, config, channel and the conversation's working context."""
    def __init__(self, cfg, tz, channel: str, memory: dict):
        self.cfg, self.tz, self.channel, self.memory = cfg, tz, channel, memory


def _ev(label, url):
    return {"label": label, "url": url}


# ------------------------------------------------------------------ validation
def validate_args(tool: Tool, args: Any) -> dict:
    if not isinstance(args, dict):
        raise ValidationFailed("Tool arguments must be an object.")
    unknown = set(args) - set(tool.properties)
    if unknown:
        raise ValidationFailed(f"Unknown argument(s): {', '.join(sorted(unknown))}")
    clean: dict = {}
    for name, spec in tool.properties.items():
        if name not in args or args[name] in (None, ""):
            if name in tool.required:
                raise ValidationFailed(f"Missing required argument '{name}'.")
            continue
        v, t = args[name], spec.get("type", "string")
        if t == "string":
            if not isinstance(v, (str, int, float)) or isinstance(v, bool):
                raise ValidationFailed(f"'{name}' must be text.")
            v = " ".join(str(v).split())
            if len(v) > spec.get("maxLength", 120):
                raise ValidationFailed(f"'{name}' is too long.")
            if "enum" in spec and v not in spec["enum"]:
                raise ValidationFailed(f"'{name}' must be one of: {', '.join(spec['enum'])}.")
            if "pattern" in spec and not re.match(spec["pattern"], v):
                raise ValidationFailed(f"'{name}' has an invalid format.")
        elif t == "boolean":
            if not isinstance(v, bool):
                raise ValidationFailed(f"'{name}' must be true or false.")
        elif t == "integer":
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValidationFailed(f"'{name}' must be a whole number.")
            if not (spec.get("minimum", -10**9) <= v <= spec.get("maximum", 10**9)):
                raise ValidationFailed(f"'{name}' is out of range.")
        clean[name] = v
    return clean


# ------------------------------------------------------------------ time
def day_bounds(ctx: Ctx, name: str) -> tuple[str, str, str]:
    """(utc_from, utc_to, label) for a named local period."""
    today = datetime.now(ctx.tz).date()
    if name == "today":
        d1, d2 = today, today
    elif name == "yesterday":
        d1 = d2 = today - timedelta(days=1)
    elif name == "this_week":
        d1, d2 = today - timedelta(days=today.weekday()), today
    elif name == "last_7_days":
        d1, d2 = today - timedelta(days=6), today
    elif name == "this_month":
        d1, d2 = today.replace(day=1), today
    elif re.match(r"^\d{4}-\d{2}-\d{2}$", name or ""):
        d1 = d2 = date.fromisoformat(name)
    else:
        raise ValidationFailed(f"Unknown period '{name}'.")
    lo, _ = reports.local_day_bounds(d1.isoformat(), ctx.tz)
    _, hi = reports.local_day_bounds(d2.isoformat(), ctx.tz)
    label = name.replace("_", " ") + (f" ({d1.isoformat()})" if d1 == d2 else f" ({d1.isoformat()} to {d2.isoformat()})")
    return lo, hi, label


PERIODS = ["today", "yesterday", "this_week", "last_7_days", "this_month"]


def _units(rows_qty_unit) -> str:
    totals: dict[str, int] = {}
    for q, u in rows_qty_unit:
        totals[u] = totals.get(u, 0) + q
    return ", ".join(f"{fmt_qty(v)} {u}" for u, v in sorted(totals.items())) or "0"


# ------------------------------------------------------------------ lot resolution
def resolve_lot(conn, token: str):
    """Returns (lot_no | None, options:list[str]). Exact (case-insensitive) match wins; otherwise partial matches."""
    token = (token or "").strip()
    if not token:
        return None, []
    ex = conn.execute("SELECT lot_no FROM lots WHERE lot_no = ? COLLATE NOCASE", (token,)).fetchone()
    if ex:
        return ex["lot_no"], []
    rows = [r["lot_no"] for r in conn.execute("SELECT lot_no FROM lots WHERE lot_no LIKE ? ORDER BY lot_no LIMIT 8", (f"%{token}%",))]
    if len(rows) == 1:
        return rows[0], []
    return None, rows


def _lot_or_clarify(conn, token, field="lot_no") -> tuple[str | None, ToolResult | None]:
    lot, opts = resolve_lot(conn, token)
    if lot:
        return lot, None
    if opts:
        return None, ToolResult(facts=[f"More than one lot matches '{token}'."],
                                clarify={"question": f"Which lot do you mean by '{token}'?", "field": field, "options": [{"label": o, "value": o} for o in opts]})
    return None, ToolResult(ok=False, facts=[f"No lot matches '{token}'."])


# ================================================================== read tools
def t_search_inventory(conn, user, a, ctx):
    rows = inventory.stock_summary(conn, a.get("location_kind"), a.get("query"))
    if not rows:
        return ToolResult(facts=["No fabric stock matches."])
    by_unit = _units((r["available"], r["unit"]) for r in rows)
    facts = [f"{len(rows)} lot/location balance(s); available in total: {by_unit}."]
    cards = [{"title": "Stock", "columns": ["Lot", "Location", "On hand", "Reserved", "Available", "Unit"],
              "rows": [[r["lot_no"], r["loc_name"], fmt_qty(r["on_hand"]), fmt_qty(r["reserved"]), fmt_qty(r["available"]), r["unit"]] for r in rows[:15]]}]
    return ToolResult(facts=facts, cards=cards, evidence=[_ev(f"Lot {r['lot_no']}", f"/fabrics/lots/{r['lot_no']}") for r in rows[:10]] + [_ev("Stock report", "/reports/stock-by-location")],
                      data={"balances": [{"lot": r["lot_no"], "location": r["loc_name"], "available": r["available"] / 1000, "unit": r["unit"]} for r in rows[:15]]})


def t_lot_history(conn, user, a, ctx):
    lot_no, early = _lot_or_clarify(conn, a["lot_no"])
    if early:
        return early
    t = trace.lot_trace(conn, lot_no)
    unit = t["lot"]["unit"]
    ctx.memory.update(last_lot=lot_no)
    mine = [b for b in t["balances"] if b["lot_no"] == lot_no]
    facts = [f"Lot {lot_no} ({t['lot']['kind']}, {unit})."]
    facts += [f"{fmt_qty(b['on_hand'])} {unit} at {b['name']} ({fmt_qty(b['reserved'])} reserved, {fmt_qty(b['available'])} available)." for b in mine] or ["It has no stock on hand in any location."]
    s = t["summary"]
    facts.append(f"Lifecycle of this lot family: received {fmt_qty(s['received'])}, issued to processing {fmt_qty(s['issued_to_processing'])}, accepted {fmt_qty(s['accepted'])}, "
                 f"rejected {fmt_qty(s['rejected'])}, wastage {fmt_qty(s['wastage'])}, shrinkage {fmt_qty(s['shrinkage'])}, dispatched {fmt_qty(s['dispatched'])}, "
                 f"open unexplained variance {fmt_qty(s['open_variance'])} {unit}.")
    ev = [_ev(f"Lot {lot_no}", f"/fabrics/lots/{lot_no}")]
    for r in t["results"]:
        ev.append(_ev(r["result_no"], f"/processing/jobs/{r['job_id']}"))
    for d in t["dispatches"]:
        ev.append(_ev(d["doc_no"], f"/dispatch/{d['id']}"))
    timeline = t["timeline"]
    ctx.memory["last_lot_timeline"] = [{"id": x["id"], "label": x["label"], "lot": x["lot_no"], "loc": x["loc_name"], "qty": x["qty"], "ref": x["doc_ref"]} for x in timeline]
    if can(user["role"], "ledger.view"):
        cards = [{"title": "Ledger timeline", "columns": ["#", "When (UTC)", "Movement", "Lot", "Location", "Qty", "Document"],
                  "rows": [[x["id"], x["posted_at"][:16].replace("T", " "), x["label"], x["lot_no"], x["loc_name"], ("+" if x["qty"] > 0 else "") + fmt_qty(x["qty"]), x["doc_ref"]] for x in timeline[-12:]]}]
    else:
        cards = []
    return ToolResult(facts=facts, evidence=ev, cards=cards, open_url=f"/fabrics/lots/{lot_no}",
                      data={"lot": lot_no, "unit": unit, "summary": {k: v / 1000 for k, v in s.items()}})


def t_list_processing_jobs(conn, user, a, ctx):
    status = a.get("status")
    awaiting = bool(a.get("awaiting_result"))
    rows = [dict(r) for r in processing.list_jobs(conn, status=status or None, overdue_only=bool(a.get("overdue_only")))]
    if a.get("process_type"):
        rows = [r for r in rows if r["process_type"] == a["process_type"]]
    if awaiting:
        waiting = {r["id"] for r in conn.execute(
            "SELECT DISTINCT j.id FROM processing_jobs j JOIN processing_issues i ON i.job_id=j.id AND i.status='posted' WHERE j.status='open' AND NOT EXISTS "
            "(SELECT 1 FROM processing_results r WHERE r.issue_id=i.id AND r.status='posted')")}
        rows = [r for r in rows if r["id"] in waiting]
    rows.sort(key=lambda r: (r["expected_date"] or "9999", r["id"]))
    ctx.memory["last_list"] = [{"kind": "job", "ref": r["job_no"], "id": r["id"], "label": r["job_no"], "created_at": r["created_at"], "expected": r["expected_date"]} for r in rows]
    if not rows:
        what = "overdue " if a.get("overdue_only") else ("awaiting-result " if awaiting else "")
        return ToolResult(facts=[f"No {what}processing jobs found."], evidence=[_ev("Processing jobs", "/processing")])
    label = "overdue " if a.get("overdue_only") else ("awaiting-result " if awaiting else "")
    facts = [f"{len(rows)} {label}processing job(s)."]
    cards = [{"title": "Processing jobs", "columns": ["Job", "Lot", "Process", "Processor", "Expected", "Status", "Issued"],
              "rows": [[r["job_no"], r["lot_no"], r["process_type"], r["party_name"] or "—", r["expected_date"] or "—", r["status"] + (" · overdue" if r["overdue"] else ""),
                        f"{fmt_qty(r['issued_qty'])} {r['unit']}"] for r in rows[:15]]}]
    return ToolResult(facts=facts, cards=cards, evidence=[_ev(r["job_no"], f"/processing/jobs/{r['id']}") for r in rows[:10]],
                      data={"jobs": [{"job": r["job_no"], "lot": r["lot_no"], "expected": r["expected_date"], "overdue": bool(r["overdue"])} for r in rows[:15]]})


def t_explain_job(conn, user, a, ctx):
    row = conn.execute("SELECT j.*, l.lot_no, p.name AS party_name FROM processing_jobs j JOIN lots l ON l.id=j.lot_id LEFT JOIN parties p ON p.id=j.party_id WHERE j.job_no=? COLLATE NOCASE", (a["job_no"],)).fetchone()
    if row is None:
        return ToolResult(ok=False, facts=[f"No processing job '{a['job_no']}'."])
    ctx.memory.update(last_job=row["job_no"])
    issues = processing.job_issues(conn, row["id"])
    results = processing.job_results(conn, row["id"])
    today = date.today().isoformat()
    facts = [f"{row['job_no']}: {row['process_type']} on lot {row['lot_no']}, processor {row['party_name'] or 'not recorded'}, status {row['status']}."]
    if row["expected_date"]:
        late = (date.fromisoformat(today) - date.fromisoformat(row["expected_date"])).days
        facts.append(f"Expected {row['expected_date']}" + (f" — {late} day(s) overdue." if late > 0 and row["status"] == "open" else "."))
    else:
        facts.append("No expected date is recorded.")
    posted = [i for i in issues if i["status"] == "posted"]
    facts.append(f"Issues posted: {len(posted)} ({', '.join(i['issue_no'] + ' ' + fmt_qty(i['qty']) for i in posted) or 'none'}); drafts: {len([i for i in issues if i['status'] == 'draft'])}.")
    live = {r["issue_id"]: r for r in results if r["status"] in ("draft", "posted")}
    for i in posted:
        r = live.get(i["id"])
        facts.append(f"{i['issue_no']}: " + ("no result recorded yet." if r is None else f"result {r['result_no']} is {r['status']}" + (f", variance {fmt_qty(r['variance'])}" if r["variance"] else "") + "."))
    if row["notes"]:
        facts.append(f"Job note: “{row['notes']}” (free text entered by staff).")
    facts.append("The system does not record a reason for delay; the facts above are all that is on file.")
    return ToolResult(facts=facts, evidence=[_ev(row["job_no"], f"/processing/jobs/{row['id']}"), _ev(f"Lot {row['lot_no']}", f"/fabrics/lots/{row['lot_no']}")], open_url=f"/processing/jobs/{row['id']}")


def t_reconciliation_results(conn, user, a, ctx):
    if a.get("period"):
        lo, hi, label = day_bounds(ctx, a["period"])
    else:
        lo, hi, label = "0000", "9999", "so far"          # no period named: every posted result, not only this week's
    sql = ("SELECT pr.*, j.job_no, j.process_type, l.lot_no, pi.qty AS issued, i.unit FROM processing_results pr JOIN processing_jobs j ON j.id=pr.job_id "
           "JOIN processing_issues pi ON pi.id=pr.issue_id JOIN lots l ON l.id=j.lot_id JOIN items i ON i.id=l.item_id WHERE pr.status='posted' AND pr.posted_at >= ? AND pr.posted_at < ? ")
    if a.get("exceptions_only", True):
        sql += "AND pr.reconciliation_status='exception' "
    rows = conn.execute(sql + "ORDER BY pr.id DESC", (lo, hi)).fetchall()
    ctx.memory["last_list"] = [{"kind": "result", "ref": r["result_no"], "id": r["job_id"], "label": r["result_no"], "created_at": r["posted_at"]} for r in rows]
    if not rows:
        return ToolResult(facts=[f"No {'discrepancies' if a.get('exceptions_only', True) else 'results'} posted {label}."], evidence=[_ev("Reconciliation report", "/reports/reconciliation")])
    facts = [f"{len(rows)} result(s) posted {label}" + (" with unexplained variance" if a.get("exceptions_only", True) else "") + ": " +
             "; ".join(f"{r['result_no']} lot {r['lot_no']} variance {fmt_qty(r['variance'])} {r['unit']}" for r in rows[:8]) + "."]
    return ToolResult(facts=facts, evidence=[_ev(r["result_no"], f"/processing/jobs/{r['job_id']}") for r in rows[:10]] + [_ev("Reconciliation report", "/reports/reconciliation")])


def t_gate_records(conn, user, a, ctx):
    lo, hi, label = day_bounds(ctx, a.get("period", "today"))
    rows = gate_entries.register(conn, ts_from=lo, ts_to=hi, direction=a.get("direction"))
    inside = gate_entries.vehicles_inside(conn)
    if not rows:
        return ToolResult(facts=[f"No gate entries or passes recorded {label}.", f"{len(inside)} vehicle(s) currently recorded inside."], evidence=[_ev("Gate register", "/reports/gate-register")], open_url="/gate")
    inw = [r for r in rows if r["direction"] == "inward"]
    out = [r for r in rows if r["direction"] == "outward"]
    facts = [f"{len(inw)} inward and {len(out)} outward gate record(s) {label}; {len(inside)} vehicle(s) currently inside."]
    cards = [{"title": "Gate records", "columns": ["No", "Direction", "Vehicle", "Party", "Material", "Status"],
              "rows": [[r["no"], r["direction"], r["vehicle"], r["party"], r["material"], r["status"].replace("_", " ")] for r in rows[:15]]}]
    return ToolResult(facts=facts, cards=cards, evidence=[_ev(r["no"], r["url"]) for r in rows[:10]] + [_ev("Gate register", "/reports/gate-register")], open_url="/gate")


def t_list_dispatches(conn, user, a, ctx):
    lo, hi, label = day_bounds(ctx, a.get("period", "this_month"))
    sql = "SELECT d.*, p.name AS customer FROM dispatches d JOIN parties p ON p.id=d.customer_id WHERE d.created_at >= ? AND d.created_at < ? "
    args: list = [lo, hi]
    if a.get("status"):
        sql += "AND d.status=? "; args.append(a["status"])
    rows = conn.execute(sql + "ORDER BY d.id DESC", args).fetchall()
    ctx.memory["last_list"] = [{"kind": "dispatch", "ref": r["doc_no"], "id": r["id"], "label": r["doc_no"], "created_at": r["created_at"]} for r in rows]
    if not rows:
        return ToolResult(facts=[f"No dispatches created {label}."], evidence=[_ev("Dispatch register", "/reports/dispatch-register")])
    qty = conn.execute("SELECT dl.qty, i.unit FROM dispatch_lines dl JOIN dispatches d ON d.id=dl.dispatch_id JOIN lots l ON l.id=dl.lot_id JOIN items i ON i.id=l.item_id "
                       "WHERE d.created_at >= ? AND d.created_at < ? AND d.status <> 'cancelled'" + (" AND d.status=?" if a.get("status") else ""), args).fetchall()
    facts = [f"{len(rows)} dispatch(es) created {label}; non-cancelled quantity {_units((q['qty'], q['unit']) for q in qty)}."]
    return ToolResult(facts=facts, cards=[{"title": "Dispatches", "columns": ["Dispatch", "Customer", "Status", "Created"], "rows": [[r["doc_no"], r["customer"], r["status"], r["created_at"][:10]] for r in rows[:15]]}],
                      evidence=[_ev(r["doc_no"], f"/dispatch/{r['id']}") for r in rows[:10]] + [_ev("Dispatch register", "/reports/dispatch-register")])


def t_factory_update(conn, user, a, ctx):
    lo, hi, label = day_bounds(ctx, a.get("period", "today"))
    role, facts, ev = user["role"], [], []
    if can(role, "stock.view"):
        r = conn.execute("SELECT g.qty, i.unit FROM goods_receipts g JOIN items i ON i.id=g.item_id WHERE g.status='posted' AND g.posted_at >= ? AND g.posted_at < ?", (lo, hi)).fetchall()
        facts.append(f"Receipts posted {label}: {len(r)}" + (f" ({_units((x['qty'], x['unit']) for x in r)})." if r else "."))
        ev.append(_ev("Goods receipts", "/fabrics/receipts"))
        totals = inventory.totals_by_location_kind(conn)
        facts.append("Stock now: " + "; ".join(f"{t['name']} {fmt_qty(t['on_hand'])}" for t in totals) + " (units as per each item).")
    if can(role, "processing.view"):
        open_jobs = processing.list_jobs(conn, status="open")
        overdue = [j for j in open_jobs if j["overdue"]]
        exc = processing.reconciliation_exceptions(conn)
        facts.append(f"Processing: {len(open_jobs)} open job(s), {len(overdue)} overdue, {len(exc)} reconciliation exception(s).")
        ev.append(_ev("Processing", "/processing"))
    if can(role, "dispatch.view"):
        c = lambda s: conn.execute("SELECT COUNT(*) AS c FROM dispatches WHERE status=?", (s,)).fetchone()["c"]
        ex = conn.execute("SELECT COUNT(*) AS c FROM gate_passes WHERE exited_at >= ? AND exited_at < ?", (lo, hi)).fetchone()["c"]
        facts.append(f"Dispatch: {c('draft')} awaiting approval, {c('approved')} approved awaiting gate pass, {c('pass_issued')} on a gate pass awaiting exit; {ex} vehicle exit(s) {label}.")
        ev.append(_ev("Dispatch", "/dispatch"))
    if can(role, "gate.view"):
        n = len(gate_entries.register(conn, ts_from=lo, ts_to=hi))
        facts.append(f"Gate: {n} record(s) {label}; {len(gate_entries.vehicles_inside(conn))} vehicle(s) inside now.")
        ev.append(_ev("Gate", "/gate"))
    if can(role, "acc.view"):
        low = accessories.low_stock(conn)
        facts.append(f"Accessories: {len(low)} item(s) at or below reorder level.")
        ev.append(_ev("Accessory stock", "/accessories"))
    if can(role, "mh.view"):
        facts.append(f"Multihead: {conn.execute('SELECT COUNT(*) AS c FROM mh_orders WHERE status=?', ('open',)).fetchone()['c']} open order(s).")
    if can(role, "fin.view"):
        s = finance.summary(conn)
        facts.append(f"Finance: payables {fmt_money(s['payables'])}, receivables {fmt_money(s['receivables'])}; {s['drafts']} entr{'y' if s['drafts'] == 1 else 'ies'} awaiting posting.")
        ev.append(_ev("Finance", "/finance"))
    if can(role, "alert.view"):
        c = watchtower.counts(conn)
        facts.append(f"Watchtower: {c['critical']} critical, {c['warning']} warning, {c['info']} info alert(s) open.")
        ev.append(_ev("Watchtower", "/watchtower"))
    if not facts:
        return ToolResult(ok=False, facts=["Your role has no operational data to summarise."])
    return ToolResult(facts=facts, evidence=ev)


def t_accessory_stock(conn, user, a, ctx):
    rows = accessories.stock_summary(conn, a.get("query"), low_only=bool(a.get("low_only")))
    if not rows:
        return ToolResult(facts=["No accessory items match." if not a.get("low_only") else "No accessory item is at or below its reorder level."], evidence=[_ev("Accessory store", "/accessories")])
    facts = [f"{len(rows)} accessory item(s)" + (" at or below reorder level" if a.get("low_only") else "") + "."]
    cards = [{"title": "Accessories", "columns": ["Code", "Item", "On hand", "Unit", "Reorder level", "Alert"],
              "rows": [[r["item"]["code"], r["item"]["name"], fmt_qty(r["total"]), r["item"]["unit"], fmt_qty(r["item"]["reorder_level"]), "LOW" if r["low"] else ""] for r in rows[:15]]}]
    return ToolResult(facts=facts, cards=cards, evidence=[_ev(r["item"]["code"], f"/accessories/items/{r['item']['id']}") for r in rows[:10]])


def t_finance_summary(conn, user, a, ctx):
    s = finance.summary(conn)
    facts = [f"Payables {fmt_money(s['payables'])}; receivables {fmt_money(s['receivables'])}.", f"{s['drafts']} finance entr{'y' if s['drafts'] == 1 else 'ies'} awaiting posting.",
             "No tax or statutory compliance is computed by this module."]
    owing = sorted([p for p in s["per_party"] if p["balance"] and p["direction"] == "payable"], key=lambda p: -p["balance"])[:3]
    if owing:
        facts.append("Largest payables: " + "; ".join(f"{p['party']['name']} {fmt_money(p['balance'])}" for p in owing) + ".")
    return ToolResult(facts=facts, evidence=[_ev("Finance", "/finance"), _ev("Financial summary report", "/reports/financial-summary")], open_url="/finance")


def t_list_alerts(conn, user, a, ctx):
    watchtower.scan(conn, user)
    rows = watchtower.list_alerts(conn, severity=a.get("severity"))
    ctx.memory["last_list"] = [{"kind": "alert", "ref": str(r["id"]), "id": r["id"], "label": r["title"], "created_at": r["detected_at"]} for r in rows]
    if not rows:
        return ToolResult(facts=["No open alerts. (Alerts come from rule-based checks only; no statistical anomaly detection is implemented.)"], evidence=[_ev("Watchtower", "/watchtower")])
    facts = [f"{len(rows)} open alert(s): " + "; ".join(f"[{r['severity']}] {r['title']}" for r in rows[:8]) + "."]
    return ToolResult(facts=facts, evidence=[_ev(r["title"], f"/watchtower#alert-{r['id']}") for r in rows[:10]], open_url="/watchtower",
                      data={"alerts": [{"severity": r["severity"], "title": r["title"], "recommendation": r["recommendation"]} for r in rows[:10]]})


def t_explain_movement(conn, user, a, ctx):
    r = conn.execute("SELECT sl.*, l.lot_no, loc.name AS loc_name, u.username AS actor FROM stock_ledger sl JOIN lots l ON l.id=sl.lot_id "
                     "JOIN locations loc ON loc.id=sl.location_id LEFT JOIN users u ON u.id=sl.actor_id WHERE sl.id=?", (a["ledger_id"],)).fetchone()
    if r is None:
        return ToolResult(ok=False, facts=[f"No ledger row #{a['ledger_id']}."])
    ref, url = trace.doc_ref(conn, r["source_type"], r["source_id"])
    label = trace.MOVEMENT_LABELS.get(r["movement"], r["movement"])
    unit = conn.execute("SELECT i.unit FROM lots l JOIN items i ON i.id=l.item_id WHERE l.id=?", (r["lot_id"],)).fetchone()["unit"]
    facts = [f"Ledger #{r['id']}: {label} — lot {r['lot_no']} {'+' if r['qty'] > 0 else ''}{fmt_qty(r['qty'])} {unit} at {r['loc_name']}, posted {r['posted_at'][:16].replace('T', ' ')} UTC by {r['actor'] or 'system'}, from document {ref}."]
    if r["reverses_id"]:
        facts.append(f"It reverses ledger #{r['reverses_id']}.")
    if r["note"]:
        facts.append(f"Note recorded: “{r['note']}”.")
    ctx.memory.update(last_lot=r["lot_no"])
    return ToolResult(facts=facts, evidence=[_ev(f"Ledger #{r['id']} · {ref}", url or f"/ledger?lot={r['lot_no']}"), _ev(f"Lot {r['lot_no']}", f"/fabrics/lots/{r['lot_no']}")])


def t_open_record(conn, user, a, ctx):
    kind, ref = a["kind"], a["ref"]
    if kind == "lot":
        lot, early = _lot_or_clarify(conn, ref, "ref")
        if early:
            return early
        ctx.memory.update(last_lot=lot)
        return ToolResult(facts=[f"Opening lot {lot}."], open_url=f"/fabrics/lots/{lot}", evidence=[_ev(f"Lot {lot}", f"/fabrics/lots/{lot}")])
    tables = {"job": ("processing_jobs", "job_no", "/processing/jobs/{id}", "processing.view"),
              "dispatch": ("dispatches", "doc_no", "/dispatch/{id}", "dispatch.view"),
              "receipt": ("goods_receipts", "doc_no", "/fabrics/receipts/{id}", "stock.view"),
              "accessory_doc": ("acc_docs", "doc_no", "/accessories/docs/{id}", "acc.view"),
              "multihead_order": ("mh_orders", "order_no", "/multihead/orders/{id}", "mh.view"),
              "finance_entry": ("fin_entries", "entry_no", "/finance/entries/{id}", "fin.view"),
              "gate_entry": ("gate_entries", "entry_no", "/gate/entries/{id}", "gate.view")}
    if kind not in tables:
        return ToolResult(ok=False, facts=[f"I can't open records of type '{kind}'."])
    tbl, col, url, perm = tables[kind]
    if not can(user["role"], perm):
        raise PermissionDenied(f"Your role cannot open {kind.replace('_', ' ')} records.")
    row = conn.execute(f"SELECT id, {col} AS no FROM {tbl} WHERE {col} = ? COLLATE NOCASE", (ref,)).fetchone()
    if row is None:
        return ToolResult(ok=False, facts=[f"No {kind.replace('_', ' ')} '{ref}' found."])
    if kind == "job":
        ctx.memory.update(last_job=row["no"])
    return ToolResult(facts=[f"Opening {row['no']}."], open_url=url.format(id=row["id"]), evidence=[_ev(row["no"], url.format(id=row["id"]))])


def t_generate_report(conn, user, a, ctx):
    key = a["report"]
    if key not in reports.REPORTS:
        return ToolResult(ok=False, facts=[f"Unknown report '{key}'. Available: {', '.join(k for k, _ in reports.available(user))}."])
    f = {}
    if a.get("period"):
        lo, hi, label = day_bounds(ctx, a["period"])
        # translate to local dates for the report's own filters
        f["date_from"] = datetime.fromisoformat(lo.replace("Z", "+00:00")).astimezone(ctx.tz).date().isoformat()
        f["date_to"] = (datetime.fromisoformat(hi.replace("Z", "+00:00")).astimezone(ctx.tz) - timedelta(seconds=1)).date().isoformat()
    else:
        label = "all dates"
    res = reports.run(conn, user, key, f, tz=ctx.tz)
    from urllib.parse import urlencode
    url = f"/reports/{key}" + (("?" + urlencode(f)) if f else "")
    tot = "; ".join(f"{t['label']}: " + ", ".join(f"{k} {reports.fmt(next(c[2] for c in res['columns'] if c[0] == k), v)}" for k, v in t["values"].items()) for t in res["totals"][:3])
    facts = [f"Prepared '{res['title']}' for {label}: {len(res['rows'])} row(s)." + (f" Totals — {tot}." if tot else "")]
    return ToolResult(facts=facts, open_url=url, evidence=[_ev(res["title"], url), _ev("Download CSV", url.replace("/reports/", "/reports/", 1) + ("&" if f else "?") + "format=csv")])


def t_supplier_message(conn, user, a, ctx):
    sup = conn.execute("SELECT * FROM parties WHERE name LIKE ? AND kind IN ('supplier','job_work') ORDER BY name LIMIT 5", (f"%{a['supplier']}%",)).fetchall()
    if not sup:
        return ToolResult(ok=False, facts=[f"No supplier or job-work party matches '{a['supplier']}'."])
    if len(sup) > 1:
        return ToolResult(clarify={"question": "Which party?", "field": "supplier", "options": [{"label": s["name"], "value": s["name"]} for s in sup]}, facts=["More than one party matches."])
    s = sup[0]
    topic = a.get("topic", "delivery")
    lines = [f"Dear {s['name']},", ""]
    facts_used = []
    if topic == "delay":
        jobs = conn.execute("SELECT j.job_no, j.expected_date, l.lot_no FROM processing_jobs j JOIN lots l ON l.id=j.lot_id WHERE j.party_id=? AND j.status='open' AND j.expected_date < ? ORDER BY j.expected_date",
                            (s["id"], date.today().isoformat())).fetchall() if can(user["role"], "processing.view") else []
        if jobs:
            lines.append("The following jobs are past their expected completion date. Please confirm the current status and a revised date:")
            lines += [f"- {j['job_no']} (lot {j['lot_no']}), expected {j['expected_date']}" for j in jobs]
            facts_used += [f"{j['job_no']} overdue" for j in jobs]
        else:
            lines.append("We are following up on our open work with you. Please confirm the current status.")
    elif topic == "payment" and can(user["role"], "fin.view"):
        b = finance.party_balance(conn, s["id"])
        lines.append(f"Our records show an outstanding balance of {fmt_money(b['balance'])} payable to you. Please send your latest statement so we can reconcile.")
        facts_used.append(f"balance {fmt_money(b['balance'])}")
    else:
        lines.append("We are following up on the expected delivery. Please confirm the dispatch date and vehicle details.")
    lines += ["", "Regards,", user["display_name"]]
    return ToolResult(facts=["Drafted a follow-up message from the records (not sent — copy and send it yourself)."] + ([f"Facts used: {', '.join(facts_used)}."] if facts_used else []),
                      data={"message": "\n".join(lines)}, cards=[{"title": f"Draft message to {s['name']}", "columns": ["Message"], "rows": [["\n".join(lines)]]}])


# ================================================================== draft-preparing tools (no stock effect)
def _qty_unit(conn, lot_no: str, qty_text: str, unit_text: str | None):
    lot = inventory.get_lot(conn, lot_no)
    if unit_text and unit_text.lower() != lot["unit"].lower():
        return None, f"Lot {lot_no} is stocked in {lot['unit']}, but you said {unit_text}. No unit conversions are configured, so I won't guess. Please restate the quantity in {lot['unit']}."
    try:
        return parse_qty(qty_text), None
    except QuantityError as e:
        return None, str(e)


def _dedupe(ctx: Ctx, tool: str, args: dict, conn, table: str, ref_col: str) -> dict | None:
    """Repeating the same request within 5 minutes returns the existing draft instead of creating another."""
    key = hashlib.sha256((tool + json.dumps(args, sort_keys=True)).encode()).hexdigest()[:16]
    prev = ctx.memory.get("drafts", {}).get(key)
    if prev and (datetime.now(timezone.utc) - datetime.fromisoformat(prev["at"])).total_seconds() < 300:
        row = conn.execute(f"SELECT id, status FROM {table} WHERE {ref_col}=?", (prev["ref"],)).fetchone()
        if row and row["status"] == "draft":
            return {**prev, "key": key}
    return {"key": key, "new": True}


def _remember(ctx: Ctx, key: str, ref: str, url: str):
    ctx.memory.setdefault("drafts", {})[key] = {"ref": ref, "url": url, "at": datetime.now(timezone.utc).isoformat()}


def t_prepare_processing_job(conn, user, a, ctx):
    lot, early = _lot_or_clarify(conn, a["lot_no"])
    if early:
        return early
    party = None
    if a.get("processor"):
        rows = conn.execute("SELECT id, name FROM parties WHERE kind='job_work' AND name LIKE ?", (f"%{a['processor']}%",)).fetchall()
        if len(rows) != 1:
            return ToolResult(clarify={"question": f"Which processor do you mean by '{a['processor']}'?", "field": "processor", "options": [{"label": r["name"], "value": r["name"]} for r in rows]} if rows else None,
                              ok=bool(rows), facts=["No processor matches." if not rows else "More than one processor matches."])
        party = rows[0]["id"]
    form = {"lot_no": lot, "process_type": a["process_type"], "party_id": party or "", "expected_date": a.get("expected_date", ""), "notes": "Prepared by the assistant"}
    jid = processing.create_job(conn, user, form)
    job = processing.get_job(conn, jid)
    ctx.memory.update(last_job=job["job_no"], last_lot=lot)
    audit.record_standalone(conn, user, "assistant.draft", entity_type="processing_job", entity_ref=job["job_no"], detail={"channel": ctx.channel})
    return ToolResult(facts=[f"Created processing job {job['job_no']} ({a['process_type']} on lot {lot}). It has no stock effect. Next: prepare an issue for it."],
                      drafts=[{"label": job["job_no"], "url": f"/processing/jobs/{jid}"}], open_url=f"/processing/jobs/{jid}", evidence=[_ev(job["job_no"], f"/processing/jobs/{jid}")])


def t_prepare_processing_issue(conn, user, a, ctx):
    lot, early = _lot_or_clarify(conn, a["lot_no"])
    if early:
        return early
    qty, err = _qty_unit(conn, lot, a["qty"], a.get("unit"))
    if err:
        return ToolResult(ok=False, facts=[err])
    lotrow = inventory.get_lot(conn, lot)
    if a.get("job_no"):
        job = conn.execute("SELECT * FROM processing_jobs WHERE job_no=? COLLATE NOCASE AND lot_id=?", (a["job_no"], lotrow["id"])).fetchone()
        jobs = [job] if job else []
    else:
        jobs = conn.execute("SELECT * FROM processing_jobs WHERE lot_id=? AND status='open'" + (" AND process_type=?" if a.get("process_type") else "") + " ORDER BY id",
                            [lotrow["id"]] + ([a["process_type"]] if a.get("process_type") else [])).fetchall()
    if not jobs:
        pt = a.get("process_type")
        return ToolResult(ok=False, facts=[f"Lot {lot} has no open {pt + ' ' if pt else ''}processing job, and an issue must belong to a job."
                                           + (f" Ask me to 'create a {pt} job for lot {lot}' first." if pt else " Create a job first, or tell me which process (dyeing, printing, embroidery).")])
    if len(jobs) > 1:
        return ToolResult(clarify={"question": f"Lot {lot} has {len(jobs)} open jobs. Which one?", "field": "job_no", "options": [{"label": f"{j['job_no']} ({j['process_type']})", "value": j["job_no"]} for j in jobs]},
                          facts=["More than one open job matches."])
    job = jobs[0]
    # location: the single raw-store location that holds the stock
    bal = [b for b in inventory.lot_balances(conn, lotrow["id"]) if b["kind"] == "raw_store"]
    if not bal:
        return ToolResult(ok=False, facts=[f"Lot {lot} has no stock in a raw store location, so nothing can be issued to processing."])
    loc = bal[0]
    ded = _dedupe(ctx, "issue", {"job": job["job_no"], "qty": qty}, conn, "processing_issues", "issue_no")
    if ded and not ded.get("new"):
        return ToolResult(facts=[f"This issue draft already exists: {ded['ref']}."], drafts=[{"label": ded["ref"], "url": ded["url"]}], open_url=ded["url"])
    form = {"qty": a["qty"], "from_location_id": loc["location_id"], "unit": lotrow["unit"]}
    iid = processing.create_issue_draft(conn, user, job["id"], form)
    issue = conn.execute("SELECT issue_no FROM processing_issues WHERE id=?", (iid,)).fetchone()["issue_no"]
    url = f"/processing/jobs/{job['id']}"
    _remember(ctx, ded["key"], issue, url)
    ctx.memory.update(last_job=job["job_no"], last_lot=lot)
    audit.record_standalone(conn, user, "assistant.draft", entity_type="processing_issue", entity_ref=issue, detail={"channel": ctx.channel})
    avail = inventory.available(conn, lotrow["id"], loc["location_id"])
    warn = [] if avail >= qty else [f"WARNING: only {fmt_qty(avail)} {lotrow['unit']} is available; the draft cannot be posted until stock suffices."]
    return ToolResult(facts=[f"Prepared draft issue {issue}: {fmt_qty(qty)} {lotrow['unit']} of lot {lot} from {loc['name']} to {job['process_type']} job {job['job_no']}. "
                             f"Currently available: {fmt_qty(avail)} {lotrow['unit']}. Nothing has moved — review the draft on screen and post it there."] + warn,
                      drafts=[{"label": f"{issue} (draft)", "url": url}], open_url=url, evidence=[_ev(job["job_no"], url), _ev(f"Lot {lot}", f"/fabrics/lots/{lot}")],
                      data={"issue": issue, "job": job["job_no"], "lot": lot, "qty": qty / 1000, "available": avail / 1000})


def t_prepare_goods_receipt(conn, user, a, ctx):
    sup = conn.execute("SELECT id, name FROM parties WHERE kind='supplier' AND name LIKE ?", (f"%{a['supplier']}%",)).fetchall()
    if len(sup) != 1:
        return ToolResult(ok=bool(sup), facts=["No supplier matches '%s'." % a["supplier"] if not sup else "More than one supplier matches."],
                          clarify={"question": "Which supplier?", "field": "supplier", "options": [{"label": s["name"], "value": s["name"]} for s in sup]} if sup else None)
    items = conn.execute("SELECT * FROM items WHERE code LIKE ? OR name LIKE ?", (f"%{a['item']}%", f"%{a['item']}%")).fetchall()
    if len(items) != 1:
        return ToolResult(ok=bool(items), facts=["No fabric item matches '%s'." % a["item"] if not items else "More than one item matches."],
                          clarify={"question": "Which item?", "field": "item", "options": [{"label": f"{i['code']} {i['name']}", "value": i["code"]} for i in items]} if items else None)
    it = items[0]
    if a.get("unit") and a["unit"].lower() != it["unit"].lower():
        return ToolResult(ok=False, facts=[f"'{it['name']}' is stocked in {it['unit']}; you said {a['unit']}. No conversion is configured."])
    loc = conn.execute("SELECT id FROM locations WHERE kind='raw_store' ORDER BY id LIMIT 1").fetchone()
    form = {"supplier_id": sup[0]["id"], "item_id": it["id"], "lot_no": a["lot_no"], "location_id": loc["id"], "qty": a["qty"], "unit": it["unit"], "supplier_ref": a.get("supplier_ref", ""), "notes": "Prepared by the assistant"}
    rid = receipts.create_draft(conn, user, form)
    r = receipts.get(conn, rid)
    audit.record_standalone(conn, user, "assistant.draft", entity_type="goods_receipt", entity_ref=r["doc_no"], detail={"channel": ctx.channel})
    return ToolResult(facts=[f"Prepared draft goods receipt {r['doc_no']}: lot {r['lot_no']}, {fmt_qty(r['qty'])} {it['unit']} of {it['name']} from {sup[0]['name']}. Stock is unchanged until it is posted on screen."],
                      drafts=[{"label": f"{r['doc_no']} (draft)", "url": f"/fabrics/receipts/{rid}"}], open_url=f"/fabrics/receipts/{rid}", evidence=[_ev(r["doc_no"], f"/fabrics/receipts/{rid}")])


def t_prepare_dispatch(conn, user, a, ctx):
    cust = conn.execute("SELECT id, name FROM parties WHERE kind='customer' AND name LIKE ?", (f"%{a['customer']}%",)).fetchall()
    if len(cust) != 1:
        return ToolResult(ok=bool(cust), facts=["No customer matches '%s'." % a["customer"] if not cust else "More than one customer matches."],
                          clarify={"question": "Which customer?", "field": "customer", "options": [{"label": c["name"], "value": c["name"]} for c in cust]} if cust else None)
    lot, early = _lot_or_clarify(conn, a["lot_no"])
    if early:
        return early
    qty, err = _qty_unit(conn, lot, a["qty"], a.get("unit"))
    if err:
        return ToolResult(ok=False, facts=[err])
    did = dispatch.create_draft(conn, user, cust[0]["id"], [(lot, a["qty"])], "Prepared by the assistant")
    d = dispatch.get(conn, did)
    audit.record_standalone(conn, user, "assistant.draft", entity_type="dispatch", entity_ref=d["doc_no"], detail={"channel": ctx.channel})
    return ToolResult(facts=[f"Prepared draft dispatch {d['doc_no']} to {cust[0]['name']}: {fmt_qty(qty)} of lot {lot}. Nothing is reserved until it is approved by an authorised person."],
                      drafts=[{"label": f"{d['doc_no']} (draft)", "url": f"/dispatch/{did}"}], open_url=f"/dispatch/{did}", evidence=[_ev(d["doc_no"], f"/dispatch/{did}")])


def t_prepare_processing_result(conn, user, a, ctx):
    iss = conn.execute("SELECT pi.*, j.job_no FROM processing_issues pi JOIN processing_jobs j ON j.id=pi.job_id WHERE pi.issue_no=? COLLATE NOCASE", (a["issue_no"],)).fetchone()
    if iss is None:
        return ToolResult(ok=False, facts=[f"No issue '{a['issue_no']}'."])
    form = {k: a.get(k, "0") for k in ("accepted", "rejected", "wastage", "shrinkage", "remaining")}
    form["notes"] = "Prepared by the assistant"
    rid = processing.create_result_draft(conn, user, iss["id"], form)
    r = conn.execute("SELECT * FROM processing_results WHERE id=?", (rid,)).fetchone()
    audit.record_standalone(conn, user, "assistant.draft", entity_type="processing_result", entity_ref=r["result_no"], detail={"channel": ctx.channel})
    return ToolResult(facts=[f"Prepared draft result {r['result_no']} for {iss['issue_no']}: reconciliation is {r['reconciliation_status']}" + (f" with unexplained variance {fmt_qty(r['variance'])}." if r["variance"] else ".")
                             + " Review and post it on screen."], drafts=[{"label": f"{r['result_no']} (draft)", "url": f"/processing/jobs/{iss['job_id']}"}], open_url=f"/processing/jobs/{iss['job_id']}")


def t_list_receipts(conn, user, a, ctx):
    lo, hi, label = day_bounds(ctx, a.get("period", "today"))
    status = a.get("status", "posted")
    col = "posted_at" if status == "posted" else "created_at"
    rows = conn.execute(f"SELECT g.*, p.name AS supplier, i.unit FROM goods_receipts g JOIN parties p ON p.id=g.supplier_id JOIN items i ON i.id=g.item_id "
                        f"WHERE g.status=? AND g.{col} >= ? AND g.{col} < ? ORDER BY g.id DESC", (status, lo, hi)).fetchall()
    ctx.memory["last_list"] = [{"kind": "receipt", "ref": r["doc_no"], "id": r["id"], "label": r["doc_no"], "created_at": r[col]} for r in rows]
    if not rows:
        return ToolResult(facts=[f"No receipts with status '{status}' {label}."], evidence=[_ev("Goods receipts", "/fabrics/receipts")])
    return ToolResult(facts=[f"{len(rows)} receipt(s) {status} {label}: {_units((r['qty'], r['unit']) for r in rows)}."],
                      cards=[{"title": "Receipts", "columns": ["Receipt", "Lot", "Supplier", "Qty"], "rows": [[r["doc_no"], r["lot_no"], r["supplier"], f"{fmt_qty(r['qty'])} {r['unit']}"] for r in rows[:15]]}],
                      evidence=[_ev(r["doc_no"], f"/fabrics/receipts/{r['id']}") for r in rows[:10]])


def t_unsupported(conn, user, a, ctx):
    return ToolResult(unsupported=a.get("what", "that"), facts=["That isn't supported by the assistant."])


# ================================================================== registry
S = lambda **kw: {"type": "string", **kw}
TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("factory_update", "Summarise today's operations across modules the user may see.", {"period": S(enum=PERIODS)}, [], None, t_factory_update),
    Tool("search_inventory", "Search fabric stock by lot/item text or location kind.", {"query": S(maxLength=60), "location_kind": S(enum=["raw_store", "wip", "finished_store", "reject_store"])}, [], "stock.view", t_search_inventory),
    Tool("lot_history", "Where a lot is now and its movement history. Accepts a full or partial lot number.", {"lot_no": S(maxLength=40)}, ["lot_no"], "stock.view", t_lot_history),
    Tool("list_processing_jobs", "List processing jobs; filter overdue, status, process or those awaiting a production result.",
         {"status": S(enum=["open", "result_posted", "cancelled"]), "overdue_only": {"type": "boolean"}, "awaiting_result": {"type": "boolean"}, "process_type": S(enum=["dyeing", "printing", "embroidery"])}, [], "processing.view", t_list_processing_jobs),
    Tool("explain_job", "Facts about one processing job incl. dates, issues, results (used for 'why is it delayed').", {"job_no": S(maxLength=30)}, ["job_no"], "processing.view", t_explain_job),
    Tool("reconciliation_results", "Posted processing results (default: only those with unexplained variance) for a period.", {"period": S(enum=PERIODS), "exceptions_only": {"type": "boolean"}}, [], "processing.view", t_reconciliation_results),
    Tool("gate_records", "Gate entries and dispatch gate passes for a period.", {"period": S(enum=PERIODS), "direction": S(enum=["inward", "outward"])}, [], "gate.view", t_gate_records),
    Tool("list_dispatches", "Dispatches created in a period.", {"period": S(enum=PERIODS), "status": S(enum=["draft", "approved", "pass_issued", "exited", "cancelled"])}, [], "dispatch.view", t_list_dispatches),
    Tool("list_receipts", "Goods receipts posted (or in another status) during a period.", {"period": S(enum=PERIODS), "status": S(enum=["draft", "posted", "reversed", "discarded"])}, [], "stock.view", t_list_receipts),
    Tool("accessory_stock", "Accessory stock, optionally only items at/below reorder level.", {"query": S(maxLength=60), "low_only": {"type": "boolean"}}, [], "acc.view", t_accessory_stock),
    Tool("finance_summary", "Payables, receivables and posting backlog.", {}, [], "fin.view", t_finance_summary),
    Tool("list_alerts", "Open Watchtower alerts.", {"severity": S(enum=["critical", "warning", "info"])}, [], "alert.view", t_list_alerts),
    Tool("explain_movement", "Explain one stock ledger row by its number.", {"ledger_id": {"type": "integer", "minimum": 1}}, ["ledger_id"], "ledger.view", t_explain_movement),
    Tool("open_record", "Open a record on screen (navigation only).", {"kind": S(enum=["lot", "job", "dispatch", "receipt", "accessory_doc", "multihead_order", "finance_entry", "gate_entry"]), "ref": S(maxLength=40)}, ["kind", "ref"], None, t_open_record),
    Tool("generate_report", "Prepare a report and link to it.", {"report": S(maxLength=40), "period": S(enum=PERIODS)}, ["report"], "report.view", t_generate_report),
    Tool("draft_supplier_message", "Draft (never send) a follow-up message to a supplier or processor using record facts.", {"supplier": S(maxLength=60), "topic": S(enum=["delivery", "delay", "payment"])}, ["supplier"], "report.view", t_supplier_message),
    Tool("prepare_processing_job", "Create a processing job (no stock effect).", {"lot_no": S(maxLength=40), "process_type": S(enum=["dyeing", "printing", "embroidery"]), "processor": S(maxLength=60), "expected_date": S(pattern=r"^\d{4}-\d{2}-\d{2}$")}, ["lot_no", "process_type"], "processing.create", t_prepare_processing_job, True),
    Tool("prepare_processing_issue", "Prepare a DRAFT issue of a lot to an open processing job. Does not post.", {"lot_no": S(maxLength=40), "qty": S(maxLength=20), "unit": S(maxLength=10), "job_no": S(maxLength=30), "process_type": S(enum=["dyeing", "printing", "embroidery"])}, ["lot_no", "qty"], "processing.issue", t_prepare_processing_issue, True),
    Tool("prepare_processing_result", "Prepare a DRAFT processing result with measured quantities. Does not post.", {"issue_no": S(maxLength=30), "accepted": S(maxLength=20), "rejected": S(maxLength=20), "wastage": S(maxLength=20), "shrinkage": S(maxLength=20), "remaining": S(maxLength=20)}, ["issue_no", "accepted"], "processing.result", t_prepare_processing_result, True),
    Tool("prepare_goods_receipt", "Prepare a DRAFT goods receipt. Does not post.", {"supplier": S(maxLength=60), "item": S(maxLength=60), "lot_no": S(maxLength=40), "qty": S(maxLength=20), "unit": S(maxLength=10), "supplier_ref": S(maxLength=40)}, ["supplier", "item", "lot_no", "qty"], "receipt.create", t_prepare_goods_receipt, True),
    Tool("prepare_dispatch", "Prepare a DRAFT dispatch. Does not reserve or deduct stock.", {"customer": S(maxLength=60), "lot_no": S(maxLength=40), "qty": S(maxLength=20), "unit": S(maxLength=10)}, ["customer", "lot_no", "qty"], "dispatch.create", t_prepare_dispatch, True),
]}


def visible_tools(user) -> list[Tool]:
    return [t for t in TOOLS.values() if t.perm is None or can(user["role"], t.perm)]


def execute(conn, user, ctx: Ctx, name: str, raw_args: Any) -> ToolResult:
    """The ONLY way the assistant touches the ERP. Re-checks permission with the current user and validates arguments."""
    tool = TOOLS.get(name)
    if tool is None:
        return ToolResult(ok=False, facts=[f"Unknown tool '{name}'."])
    if tool.perm and not can(user["role"], tool.perm):
        audit.record_standalone(conn, user, "assistant.tool_denied", entity_ref=name, outcome="denied", detail={"channel": ctx.channel})
        return ToolResult(ok=False, facts=[f"Your role does not have permission for that ({tool.perm})."], unsupported="permission")
    try:
        args = validate_args(tool, raw_args)
        return tool.fn(conn, user, args, ctx)
    except ValidationFailed as e:
        return ToolResult(ok=False, facts=["Invalid request: " + "; ".join(e.errors.values())])
    except PermissionDenied as e:
        audit.record_standalone(conn, user, "assistant.tool_denied", entity_ref=name, outcome="denied", detail={"channel": ctx.channel, "reason": str(e)})
        return ToolResult(ok=False, facts=[str(e)], unsupported="permission")
    except (BusinessRuleViolation, NotFound) as e:
        return ToolResult(ok=False, facts=[str(e)])
    except AppError as e:
        return ToolResult(ok=False, facts=[str(e)])


# analytics tools live in their own module (imported last: they use the helpers defined above)
from . import tools_analytics as _ta  # noqa: E402
TOOLS.update({t.name: t for t in _ta.build()})
