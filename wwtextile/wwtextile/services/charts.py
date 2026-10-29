"""Dashboard charts: server-rendered inline SVG (no JavaScript, no external libraries, CSP-safe, monochrome).

Every chart is computed from the same posted records the tables and reports use, and ships with its numbers in a data table
(<details>) so nothing is only visible as a picture.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from html import escape

from markupsafe import Markup

from ..permissions import can

W, H, PAD_L, PAD_B, PAD_T = 640, 230, 54, 34, 14
WEEKS = 8


def _weeks(tz) -> list[date]:
    today = datetime.now(tz).date()
    start = today - timedelta(days=today.weekday())
    return [start - timedelta(weeks=WEEKS - 1 - i) for i in range(WEEKS)]


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    mag = 10 ** (len(str(int(v))) - 1)
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * mag:
            return float(m * mag)
    return float(10 * mag)


def _short(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.1f}M".replace(".0M", "M")
    if a >= 1_000:
        return f"{v / 1_000:.1f}k".replace(".0k", "k")
    return f"{v:.0f}"


def _grouped_bars(labels: list[str], series: list[tuple[str, str, list[float]]], title: str, unit_fmt) -> str:
    """series = [(name, css class, values)]. Returns an <svg> string."""
    top = _nice_max(max((max(v) for _n, _c, v in series if v), default=0))
    pw, ph = W - PAD_L - 8, H - PAD_T - PAD_B
    n, k = len(labels), len(series)
    slot = pw / max(n, 1)
    bw = min(26.0, slot * 0.72 / k)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{escape(title)}" class="chart-svg"><title>{escape(title)}</title>']
    for i in range(5):
        y = PAD_T + ph - ph * i / 4
        out.append(f'<line x1="{PAD_L}" x2="{W - 8}" y1="{y:.1f}" y2="{y:.1f}" class="c-grid"/>')
        out.append(f'<text x="{PAD_L - 6}" y="{y + 3:.1f}" text-anchor="end" class="c-txt">{escape(unit_fmt(top * i / 4))}</text>')
    for j, lab in enumerate(labels):
        cx = PAD_L + slot * (j + 0.5)
        x0 = cx - bw * k / 2
        for s, (name, cls, vals) in enumerate(series):
            v = vals[j]
            h = ph * v / top if top else 0
            if v > 0:
                out.append(f'<rect x="{x0 + s * bw:.1f}" y="{PAD_T + ph - h:.1f}" width="{bw - 1:.1f}" height="{max(h, 1):.1f}" class="{cls}"><title>{escape(name)} · {escape(lab)}: {escape(unit_fmt(v))}</title></rect>')
        out.append(f'<text x="{cx:.1f}" y="{H - PAD_B + 15}" text-anchor="middle" class="c-txt">{escape(lab)}</text>')
    out.append(f'<line x1="{PAD_L}" x2="{W - 8}" y1="{PAD_T + ph}" y2="{PAD_T + ph}" class="c-axis"/></svg>')
    return "".join(out)


def _legend(items: list[tuple[str, str]]) -> str:
    return '<div class="c-legend small">' + "".join(f'<span><i class="sw {c}"></i>{escape(n)}</span>' for n, c in items) + "</div>"


def _table(head: list[str], rows: list[list[str]]) -> str:
    return ('<details class="small"><summary>Show the numbers</summary><div class="table-wrap"><table><thead><tr>'
            + "".join(f"<th>{escape(h)}</th>" for h in head) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f'<td class="{"num" if i else ""}">{escape(c)}</td>' for i, c in enumerate(r)) + "</tr>" for r in rows)
            + "</tbody></table></div></details>")


def _fig(title: str, sub: str, svg: str, legend: str, table: str) -> dict:
    return {"title": title, "sub": sub, "html": Markup(svg + legend + table)}


def _week_idx(weeks: list[date], d: date) -> int | None:
    for i in range(len(weeks) - 1, -1, -1):
        if d >= weeks[i]:
            return i if d < weeks[i] + timedelta(weeks=1) else None
    return None


def _local_date(ts: str, tz) -> date:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).astimezone(tz).date()


def build(conn: sqlite3.Connection, user, tz, currency: str = "PKR") -> list[dict]:
    role = user["role"]
    weeks = _weeks(tz)
    labels = [w.strftime("%d %b") for w in weeks]
    out: list[dict] = []

    if can(role, "stock.view"):
        recv, disp = [0.0] * WEEKS, [0.0] * WEEKS
        since = datetime.combine(weeks[0], datetime.min.time(), tzinfo=tz).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        for r in conn.execute("SELECT posted_at, qty, movement FROM stock_ledger WHERE movement IN ('receipt','dispatch_out') AND posted_at >= ?", (since,)):
            i = _week_idx(weeks, _local_date(r["posted_at"], tz))
            if i is None:
                continue
            (recv if r["movement"] == "receipt" else disp)[i] += abs(r["qty"]) / 1000.0
        m = lambda v: f"{v:,.0f} m"
        out.append(_fig("Fabric received and dispatched", f"Metres per week, last {WEEKS} weeks (weeks start Monday). Reversals are not netted here; see the ledger.",
                        _grouped_bars(labels, [("Received", "c-a", recv), ("Dispatched (left the gate)", "c-b", disp)], "Fabric received and dispatched per week", lambda v: _short(v)),
                        _legend([("Received", "c-a"), ("Dispatched (stock deducted)", "c-b")]),
                        _table(["Week of", "Received (m)", "Dispatched (m)"], [[labels[i], m(recv[i]), m(disp[i])] for i in range(WEEKS)])))

    if can(role, "processing.view"):
        rows = conn.execute(
            "SELECT j.process_type AS p, SUM(r.accepted+r.rejected+r.wastage+r.shrinkage+r.remaining+r.variance) AS inp, SUM(r.accepted) AS acc, SUM(r.rejected) AS rej, "
            "SUM(r.wastage) AS was, SUM(r.shrinkage) AS shr, SUM(r.variance) AS var FROM processing_results r JOIN processing_jobs j ON j.id=r.job_id "
            "WHERE r.status='posted' GROUP BY j.process_type ORDER BY j.process_type").fetchall()
        if rows:
            bh, gap, left, wbar = 26, 16, 104, W - 104 - 70
            hh = PAD_T + len(rows) * (bh + gap) + 8
            parts = [f'<svg viewBox="0 0 {W} {hh}" role="img" aria-label="Processing outcome by process" class="chart-svg"><title>Processing outcome by process (share of input)</title>']
            tbl = []
            for i, r in enumerate(rows):
                inp = r["inp"] or 1
                y = PAD_T + i * (bh + gap)
                parts.append(f'<text x="{left - 8}" y="{y + bh / 2 + 4}" text-anchor="end" class="c-txt">{escape(r["p"].title())}</text>')
                x = float(left)
                for nm, key, cls in (("Accepted", "acc", "c-a"), ("Rejected", "rej", "c-c"), ("Wastage", "was", "c-d"), ("Shrinkage", "shr", "c-b"), ("Unexplained", "var", "c-e")):
                    wv = wbar * (r[key] or 0) / inp
                    if wv > 0.5:
                        parts.append(f'<rect x="{x:.1f}" y="{y}" width="{wv:.1f}" height="{bh}" class="{cls}"><title>{nm}: {(r[key] or 0) / inp * 100:.1f}% of input</title></rect>')
                    x += wv
                loss = (r["rej"] + r["was"] + r["shr"] + r["var"]) / inp * 100
                parts.append(f'<text x="{left + wbar + 6}" y="{y + bh / 2 + 4}" class="c-txt">{loss:.1f}% lost</text>')
                f = lambda k: f"{(r[k] or 0) / inp * 100:.1f}%"
                tbl.append([r["p"].title(), f"{inp / 1000:,.0f} m", f("acc"), f("rej"), f("was"), f("shr"), f("var")])
            parts.append("</svg>")
            out.append(_fig("Processing outcome by process", "Share of fabric issued, from posted results. 'Lost' = rejected + wastage + shrinkage + unexplained.", "".join(parts),
                            _legend([("Accepted", "c-a"), ("Rejected", "c-c"), ("Wastage", "c-d"), ("Shrinkage", "c-b"), ("Unexplained variance", "c-e")]),
                            _table(["Process", "Input", "Accepted", "Rejected", "Wastage", "Shrinkage", "Unexplained"], tbl)))

    if can(role, "fin.view"):
        pin, pout = [0.0] * WEEKS, [0.0] * WEEKS
        for r in conn.execute("SELECT kind, entry_date, amount_paise FROM fin_entries WHERE status='posted' AND kind IN ('payment_in','payment_out') AND entry_date >= ?", (weeks[0].isoformat(),)):
            i = _week_idx(weeks, date.fromisoformat(r["entry_date"]))
            if i is not None:
                (pin if r["kind"] == "payment_in" else pout)[i] += r["amount_paise"] / 100.0
        c = lambda v: f"{currency} {v:,.0f}"
        out.append(_fig("Cash received and paid", f"{currency} per week by entry date, posted payments only, last {WEEKS} weeks.",
                        _grouped_bars(labels, [("Received", "c-a", pin), ("Paid", "c-b", pout)], "Payments received and paid per week", lambda v: _short(v)),
                        _legend([("Received from customers", "c-a"), ("Paid to suppliers / job-work", "c-b")]),
                        _table(["Week of", "Received", "Paid"], [[labels[i], c(pin[i]), c(pout[i])] for i in range(WEEKS)])))
    return out
