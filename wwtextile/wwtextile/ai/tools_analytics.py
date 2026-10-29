"""Read-only analytics tools. Each one answers a business question from posted records using the same services/tables as the UI and
reports; nothing here writes, and every number can be traced through the evidence links."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..money import fmt_money
from ..quantity import fmt_qty
from ..services import accessories, finance, inventory, multihead, processing
from .tools import PERIODS, S, Tool, ToolResult, _ev, _units, day_bounds

PROCESSES = ["dyeing", "printing", "embroidery"]


def _pct(part: int, whole: int) -> str:
    return f"{part * 100 / whole:.1f}%" if whole else "n/a"


def t_loss_analysis(conn, user, a, ctx):
    sql = ("SELECT j.process_type AS p, i.unit AS unit, COUNT(*) AS n, SUM(r.accepted) AS acc, SUM(r.rejected) AS rej, SUM(r.wastage) AS was, "
           "SUM(r.shrinkage) AS shr, SUM(r.variance) AS var, SUM(r.remaining) AS rem FROM processing_results r JOIN processing_jobs j ON j.id=r.job_id "
           "JOIN lots l ON l.id=j.lot_id JOIN items i ON i.id=l.item_id WHERE r.status='posted' ")
    args: list = []
    label = "all posted results"
    if a.get("period"):
        lo, hi, label = day_bounds(ctx, a["period"])
        sql += "AND r.posted_at >= ? AND r.posted_at < ? "
        args += [lo, hi]
    if a.get("process"):
        sql += "AND j.process_type = ? "
        args.append(a["process"])
    rows = conn.execute(sql + "GROUP BY j.process_type, i.unit ORDER BY j.process_type", args).fetchall()
    if not rows:
        return ToolResult(facts=[f"No posted processing results for {label}."], evidence=[_ev("Processing", "/processing")])
    facts, table, tot = [], [], dict(inp=0, acc=0, rej=0, was=0, shr=0, var=0)
    for r in rows:
        inp = r["acc"] + r["rej"] + r["was"] + r["shr"] + r["var"] + r["rem"]
        tot["inp"] += inp
        for k, c in (("acc", "acc"), ("rej", "rej"), ("was", "was"), ("shr", "shr"), ("var", "var")):
            tot[k] += r[c]
        table.append([r["p"].title(), str(r["n"]), f"{fmt_qty(inp)} {r['unit']}", _pct(r["acc"], inp), _pct(r["rej"], inp), _pct(r["was"], inp), _pct(r["shr"], inp), _pct(r["var"], inp)])
        facts.append(f"{r['p'].title()}: {r['n']} result(s), {fmt_qty(inp)} {r['unit']} in; accepted {_pct(r['acc'], inp)}, rejected {_pct(r['rej'], inp)}, wastage {_pct(r['was'], inp)}, "
                     f"shrinkage {_pct(r['shr'], inp)}, unexplained {_pct(r['var'], inp)}.")
    if len(rows) > 1:
        lost = tot["rej"] + tot["was"] + tot["shr"] + tot["var"]
        facts.insert(0, f"Across {label}: {_pct(lost, tot['inp'])} of input was lost (rejected + wastage + shrinkage + unexplained); wastage {_pct(tot['was'], tot['inp'])}, shrinkage {_pct(tot['shr'], tot['inp'])}.")
        worst = max(rows, key=lambda r: (r["rej"] + r["was"] + r["shr"] + r["var"]) / max(1, r["acc"] + r["rej"] + r["was"] + r["shr"] + r["var"] + r["rem"]))
        facts.append(f"Highest loss rate: {worst['p'].title()}.")
    return ToolResult(facts=facts, cards=[{"title": "Processing outcome (share of input)", "columns": ["Process", "Results", "Input", "Accepted", "Rejected", "Wastage", "Shrinkage", "Unexplained"], "rows": table}],
                      evidence=[_ev("Wastage and shrinkage report", "/reports/wastage-shrinkage"), _ev("Reconciliation report", "/reports/reconciliation")],
                      data={"processes": [{"process": r["p"], "results": r["n"]} for r in rows]})


def t_party_balances(conn, user, a, ctx):
    direction = a["direction"]
    s = finance.summary(conn)
    rows = sorted([p for p in s["per_party"] if p["balance"] > 0 and p["direction"] == direction], key=lambda p: -p["balance"])[: a.get("limit", 5)]
    total = s["payables"] if direction == "payable" else s["receivables"]
    word = "we owe suppliers and job-work parties" if direction == "payable" else "customers owe us"
    if not rows:
        return ToolResult(facts=[f"Nothing outstanding: no {direction} balances."], evidence=[_ev("Finance", "/finance")])
    facts = [f"Total outstanding {word}: {fmt_money(total)}.", "Largest: " + "; ".join(f"{p['party']['name']} {fmt_money(p['balance'])}" for p in rows) + "."]
    return ToolResult(facts=facts, cards=[{"title": "Outstanding by party", "columns": ["Party", "Outstanding"], "rows": [[p["party"]["name"], fmt_money(p["balance"])] for p in rows]}],
                      evidence=[_ev(p["party"]["name"] + " statement", f"/finance/parties/{p['party']['id']}") for p in rows], open_url="/finance",
                      data={"direction": direction, "total": total / 100})


def t_cash_position(conn, user, a, ctx):
    lo, hi, label = day_bounds(ctx, a.get("period", "this_week"))
    parse = lambda x: datetime.strptime(x, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).astimezone(ctx.tz)
    d1 = parse(lo).date().isoformat()
    d2 = (parse(hi) - timedelta(seconds=1)).date().isoformat()          # entry_date is a LOCAL calendar date
    s = finance.summary(conn, d1, d2)
    net = s["received"] - s["paid"] - s["expenses_total"]
    facts = [f"{label}: received {fmt_money(s['received'])}, paid to suppliers/job-work {fmt_money(s['paid'])}, expenses {fmt_money(s['expenses_total'])}; net cash movement {fmt_money(net)}.",
             f"Billed {fmt_money(s['billed'])}, invoiced {fmt_money(s['invoiced'])} in the same period. These are posted entries only; bank balances are not tracked."]
    return ToolResult(facts=facts, evidence=[_ev("Financial summary report", "/reports/financial-summary"), _ev("Finance", "/finance")],
                      data={"received": s["received"] / 100, "paid": s["paid"] / 100, "expenses": s["expenses_total"] / 100})


def t_multihead_status(conn, user, a, ctx):
    orders = multihead.list_orders(conn, status=a.get("status"))
    if not orders:
        return ToolResult(facts=["No Multihead orders match."], evidence=[_ev("Multihead", "/multihead")])
    open_o = [o for o in orders if o["status"] == "open"]
    late = [o for o in open_o if o["overdue"]]
    facts = [f"{len(orders)} Multihead order(s): {len(open_o)} open ({len(late)} overdue), {len(orders) - len(open_o)} closed."]
    for o in open_o[:5]:
        facts.append(f"{o['order_no']} {o['title']} on {o['machine_code'] or 'no machine'}: {fmt_qty(o['good'])} of {fmt_qty(o['planned_qty'])} {o['unit']} good ({o['progress_pct'] if o['progress_pct'] is not None else 0}%), due {o['due_date'] or 'n/a'}{' - OVERDUE' if o['overdue'] else ''}.")
    rows = [[o["order_no"], o["title"], o["machine_code"] or "-", f"{fmt_qty(o['good'])} / {fmt_qty(o['planned_qty'])} {o['unit']}", f"{o['progress_pct'] or 0}%", o["due_date"] or "-", o["status"]] for o in orders[:12]]
    return ToolResult(facts=facts, cards=[{"title": "Multihead orders", "columns": ["Order", "Title", "Machine", "Good / planned", "Done", "Due", "Status"], "rows": rows}],
                      evidence=[_ev(o["order_no"], f"/multihead/orders/{o['id']}") for o in orders[:8]], open_url="/multihead")


def t_jobs_by_party(conn, user, a, ctx):
    q = a["party"].lower()
    party = conn.execute("SELECT id, name FROM parties WHERE kind='job_work' AND lower(name) LIKE ? ORDER BY name LIMIT 5", (f"%{q}%",)).fetchall()
    if not party:
        names = ", ".join(r["name"] for r in conn.execute("SELECT name FROM parties WHERE kind='job_work' ORDER BY name"))
        return ToolResult(ok=False, facts=[f"No job-work party matches '{a['party']}'." + (f" Known: {names}." if names else "")])
    ids = {p["id"] for p in party}
    jobs = [j for j in processing.list_jobs(conn) if j["party_id"] in ids]
    if not jobs:
        return ToolResult(facts=[f"No processing jobs with {party[0]['name']}."])
    opn = [j for j in jobs if j["status"] == "open"]
    facts = [f"{party[0]['name']}: {len(jobs)} job(s), {len(opn)} open, {sum(1 for j in opn if j['overdue'])} overdue."]
    for j in opn[:5]:
        facts.append(f"{j['job_no']} ({j['process_type']}) lot {j['lot_no']}: {fmt_qty(j['issued_qty'])} {j['unit']} issued, expected {j['expected_date'] or 'n/a'}{' - OVERDUE' if j['overdue'] else ''}.")
    ctx.memory["last_list"] = [{"kind": "job", "ref": j["job_no"], "label": j["job_no"], "created_at": j["created_at"]} for j in jobs]
    return ToolResult(facts=facts, cards=[{"title": "Jobs", "columns": ["Job", "Process", "Lot", "Issued", "Expected", "Status"],
                                           "rows": [[j["job_no"], j["process_type"], j["lot_no"], f"{fmt_qty(j['issued_qty'])} {j['unit']}", j["expected_date"] or "-", ("overdue" if j["overdue"] else j["status"])] for j in jobs[:12]]}],
                      evidence=[_ev(j["job_no"], f"/processing/jobs/{j['id']}") for j in jobs[:8]])


def t_purchases_by_supplier(conn, user, a, ctx):
    sql = ("SELECT p.id, p.name, COUNT(*) AS n, SUM(g.qty) AS qty, i.unit AS unit FROM goods_receipts g JOIN parties p ON p.id=g.supplier_id JOIN items i ON i.id=g.item_id "
           "WHERE g.status='posted' ")
    args: list = []
    label = "all time"
    if a.get("period"):
        lo, hi, label = day_bounds(ctx, a["period"])
        sql += "AND g.posted_at >= ? AND g.posted_at < ? "
        args += [lo, hi]
    rows = conn.execute(sql + "GROUP BY p.id, i.unit ORDER BY qty DESC LIMIT ?", args + [a.get("limit", 5)]).fetchall()
    if not rows:
        return ToolResult(facts=[f"No posted goods receipts for {label}."], evidence=[_ev("Goods receipts", "/fabrics/receipts")])
    from ..permissions import can
    billed = {}
    if can(user["role"], "fin.view"):
        for r in conn.execute("SELECT party_id, SUM(amount_paise) AS s FROM fin_entries WHERE kind='bill' AND status='posted' GROUP BY party_id"):
            billed[r["party_id"]] = r["s"]
    facts = [f"Fabric bought ({label}): " + "; ".join(f"{r['name']} {fmt_qty(r['qty'])} {r['unit']} in {r['n']} receipt(s)" + (f", billed {fmt_money(billed[r['id']])}" if r["id"] in billed else "") for r in rows) + "."]
    return ToolResult(facts=facts, cards=[{"title": "Purchases by supplier", "columns": ["Supplier", "Receipts", "Quantity"] + (["Billed (all bills)"] if billed else []),
                                           "rows": [[r["name"], str(r["n"]), f"{fmt_qty(r['qty'])} {r['unit']}"] + ([fmt_money(billed.get(r["id"], 0))] if billed else []) for r in rows]}],
                      evidence=[_ev("Goods receipts report", "/reports/goods-receipts")])


def t_top_customers(conn, user, a, ctx):
    sql = ("SELECT p.id, p.name, COUNT(DISTINCT d.id) AS n, SUM(dl.qty) AS qty, i.unit AS unit FROM dispatches d JOIN parties p ON p.id=d.customer_id JOIN dispatch_lines dl ON dl.dispatch_id=d.id "
           "JOIN lots l ON l.id=dl.lot_id JOIN items i ON i.id=l.item_id WHERE d.status <> 'cancelled' ")
    args: list = []
    label = "all time"
    if a.get("period"):
        lo, hi, label = day_bounds(ctx, a["period"])
        sql += "AND d.created_at >= ? AND d.created_at < ? "
        args += [lo, hi]
    rows = conn.execute(sql + "GROUP BY p.id, i.unit ORDER BY qty DESC LIMIT ?", args + [a.get("limit", 5)]).fetchall()
    if not rows:
        return ToolResult(facts=[f"No dispatches for {label}."], evidence=[_ev("Dispatch", "/dispatch")])
    from ..permissions import can
    inv = {}
    if can(user["role"], "fin.view"):
        for r in conn.execute("SELECT party_id, SUM(amount_paise) AS s FROM fin_entries WHERE kind='invoice' AND status='posted' GROUP BY party_id"):
            inv[r["party_id"]] = r["s"]
    facts = [f"Customers by dispatched quantity ({label}): " + "; ".join(f"{r['name']} {fmt_qty(r['qty'])} {r['unit']} in {r['n']} dispatch(es)" + (f", invoiced {fmt_money(inv[r['id']])}" if r["id"] in inv else "") for r in rows) + "."]
    return ToolResult(facts=facts, cards=[{"title": "Top customers", "columns": ["Customer", "Dispatches", "Quantity"] + (["Invoiced (all invoices)"] if inv else []),
                                           "rows": [[r["name"], str(r["n"]), f"{fmt_qty(r['qty'])} {r['unit']}"] + ([fmt_money(inv.get(r["id"], 0))] if inv else []) for r in rows]}],
                      evidence=[_ev("Dispatch register", "/reports/dispatch-register")])


def t_stock_ageing(conn, user, a, ctx):
    rows = inventory.stock_summary(conn, a.get("location_kind"), None)
    if not rows:
        return ToolResult(facts=["No stock on hand."])
    first = {r["lot_no"]: r["d"] for r in conn.execute("SELECT l.lot_no, MIN(sl.posted_at) AS d FROM stock_ledger sl JOIN lots l ON l.id=sl.lot_id GROUP BY l.lot_no")}
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    aged = []
    for r in rows:
        if r["on_hand"] > 0 and r["lot_no"] in first:
            days = (now - datetime.strptime(first[r["lot_no"]], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)).days
            aged.append((days, r))
    aged.sort(key=lambda x: -x[0])
    top = aged[: a.get("limit", 5)]
    if not top:
        return ToolResult(facts=["No lots with stock on hand."])
    facts = ["Oldest stock still on hand: " + "; ".join(f"{r['lot_no']} {fmt_qty(r['on_hand'])} {r['unit']} at {r['loc_name']} ({d} days since first movement)" for d, r in top) + "."]
    return ToolResult(facts=facts, cards=[{"title": "Oldest stock", "columns": ["Lot", "Location", "On hand", "Days"], "rows": [[r["lot_no"], r["loc_name"], f"{fmt_qty(r['on_hand'])} {r['unit']}", str(d)] for d, r in top]}],
                      evidence=[_ev(f"Lot {r['lot_no']}", f"/fabrics/lots/{r['lot_no']}") for _d, r in top])


def build() -> list[Tool]:
    return [
        Tool("loss_analysis", "Wastage, shrinkage, rejection and unexplained variance as a share of input, per process (dyeing/printing/embroidery), optionally for a period. Use for any question about losses, wastage, shrinkage, yield or quality.",
             {"process": S(enum=PROCESSES), "period": S(enum=PERIODS)}, [], "processing.view", t_loss_analysis),
        Tool("party_balances", "Who owes whom: outstanding balances ranked by party. direction=receivable means customers who owe us; payable means suppliers/job-work parties we owe.",
             {"direction": S(enum=["payable", "receivable"]), "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ["direction"], "fin.view", t_party_balances),
        Tool("cash_position", "Cash received, paid and expenses for a period (posted finance entries only).", {"period": S(enum=PERIODS)}, [], "fin.view", t_cash_position),
        Tool("multihead_status", "Multihead embroidery orders: progress against plan, machine, due date, overdue.", {"status": S(enum=["open", "completed", "cancelled"])}, [], "mh.view", t_multihead_status),
        Tool("jobs_by_party", "Processing jobs placed with one job-work party (e.g. a dye house), with overdue flags.", {"party": S(maxLength=60)}, ["party"], "processing.view", t_jobs_by_party),
        Tool("purchases_by_supplier", "Fabric bought per supplier (quantity, receipts, billed amount) for a period or all time.", {"period": S(enum=PERIODS), "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, [], "stock.view", t_purchases_by_supplier),
        Tool("top_customers", "Customers ranked by dispatched quantity (and invoiced amount if permitted).", {"period": S(enum=PERIODS), "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, [], "dispatch.view", t_top_customers),
        Tool("stock_ageing", "Oldest lots still holding stock, optionally for one location kind.", {"location_kind": S(enum=["raw_store", "wip", "finished_store", "reject_store"]), "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, [], "stock.view", t_stock_ageing),
    ]
