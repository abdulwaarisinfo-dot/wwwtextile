"""Reports, Watchtower, Assistant (text + voice endpoints), Document reader, registry settings."""
from __future__ import annotations

import json
import mimetypes

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import audit
from .ai import agent, documents, providers
from .errors import AppError, BusinessRuleViolation, NotFound, PermissionDenied, ValidationFailed
from .permissions import can, require
from .services import briefing, reports, settings, watchtower
from .webcore import _int, _qs, display_tz, handler

PROCESS_TYPES = ["dyeing", "printing", "embroidery"]


# ================================================================ reports
def _options(conn, src: str):
    q = lambda sql: conn.execute(sql).fetchall()
    if src == "loc_kinds":
        return [("raw_store", "Raw Store"), ("wip", "Processing (WIP)"), ("finished_store", "Finished Store"), ("reject_store", "Reject Store")]
    if src == "movements":
        return [(r["movement"], r["movement"].replace("_", " ")) for r in q("SELECT DISTINCT movement FROM stock_ledger ORDER BY movement")]
    if src == "suppliers":
        return [(r["id"], r["name"]) for r in q("SELECT id, name FROM parties WHERE kind='supplier' ORDER BY name")]
    if src == "customers":
        return [(r["id"], r["name"]) for r in q("SELECT id, name FROM parties WHERE kind='customer' ORDER BY name")]
    if src in ("parties", "fin_parties"):
        return [(r["id"], r["name"]) for r in q("SELECT id, name FROM parties ORDER BY name")]
    if src == "acc_items":
        return [(r["id"], f"{r['code']} · {r['name']}") for r in q("SELECT id, code, name FROM acc_items ORDER BY code")]
    if src == "receipt_status":
        return [(v, v) for v in ("draft", "posted", "reversed", "discarded")]
    if src == "process_types":
        return [(v, v) for v in PROCESS_TYPES]
    if src == "job_status":
        return [(v, v.replace("_", " ")) for v in ("open", "result_posted", "cancelled")]
    if src == "mh_status":
        return [(v, v) for v in ("open", "completed", "cancelled")]
    if src == "dispatch_status":
        return [(v, v.replace("_", " ")) for v in ("draft", "approved", "pass_issued", "exited", "cancelled")]
    if src == "directions":
        return [("inward", "Inward"), ("outward", "Outward")]
    if src == "outcomes":
        return [(v, v) for v in ("success", "denied", "failed")]
    if src == "yesno":
        return [("1", "Yes")]
    return []


@handler(perm="report.view")
def reports_hub(ctx):
    groups: dict[str, list] = {}
    for key, spec in reports.available(ctx.user):
        groups.setdefault(spec["group"], []).append((key, spec))
    return ctx.render("reports_hub.html", groups=groups)


@handler()
def report_page(ctx, key):
    spec = reports.REPORTS.get(key)
    if spec is None:
        raise NotFound("Unknown report.")
    require(ctx.user, spec["perm"])
    filters = {k: v for k, v in ctx.request.query_params.items() if k in {f[0] for f in spec["filters"]} and v.strip()}
    fmt = _qs(ctx.request, "format")
    tz = display_tz(ctx.cfg)
    err, res = None, None
    run = bool(ctx.request.query_params.get("run")) or bool(filters) or fmt or not spec["filters"]
    if run:
        try:
            res = reports.run(ctx.conn, ctx.user, key, filters, tz)
        except ValidationFailed as e:
            err = "; ".join(e.errors.values())
    if fmt == "csv" and res is not None:
        text = ", ".join(f"{k}={v}" for k, v in res["filters_applied"].items())
        audit.record_standalone(ctx.conn, ctx.user, "report.export_csv", entity_type="report", entity_ref=key, detail={"rows": len(res["rows"]), "filters": res["filters_applied"]})
        return Response(reports.to_csv(res, text), media_type="text/csv; charset=utf-8",
                        headers={"content-disposition": f'attachment; filename="{key}.csv"'})
    if res is not None:
        audit.record_standalone(ctx.conn, ctx.user, "report.view", entity_type="report", entity_ref=key, detail={"rows": len(res["rows"])})
    fields = [(k, label, typ, _options(ctx.conn, typ.split(":", 1)[1]) if typ.startswith("select:") else None) for k, label, typ in spec["filters"]]
    qs = "&".join(f"{k}={v}" for k, v in filters.items())
    return ctx.render("report.html", key=key, spec=spec, res=res, err=err, fields=fields, values=filters, qs=qs, tz=tz,
                      rfmt=lambda kind, v: reports.fmt(kind, v, tz), print_mode=(fmt == "print"))


# ================================================================ watchtower
@handler(perm="alert.view")
def watchtower_page(ctx):
    r = ctx.request
    status, sev, rule = _qs(r, "status", "active"), _qs(r, "severity"), _qs(r, "rule")
    return ctx.render("watchtower.html", alerts=watchtower.list_alerts(ctx.conn, status, sev or None, rule or None), counts=watchtower.counts(ctx.conn),
                      status=status, sev=sev, rule=rule, rules=watchtower.RULES, users=ctx.conn.execute("SELECT id, username FROM users WHERE active=1 ORDER BY username").fetchall())


@handler(perm="alert.view")
def alert_detail(ctx, alert_id):
    a = watchtower.get_alert(ctx.conn, int(alert_id))
    return ctx.render("alert.html", a=a, history=watchtower.alert_history(ctx.conn, a["id"]), rules=watchtower.RULES,
                      users=ctx.conn.execute("SELECT id, username FROM users WHERE active=1 ORDER BY username").fetchall())


@handler(perm="alert.manage", post=True)
def watchtower_scan(ctx):
    st = watchtower.scan(ctx.conn, ctx.user)
    ctx.flash("ok", f"Scan complete: {st['new']} new, {st['reopened']} reopened, {st['auto_resolved']} auto-resolved; {st['open_total']} active.")
    return ctx.redirect("/watchtower")


def _alert_action(fn_name):
    def run(ctx, alert_id):
        fn = getattr(watchtower, fn_name)
        try:
            if fn_name == "assign":
                fn(ctx.conn, ctx.user, int(alert_id), _int(ctx.form.get("assignee")), ctx.form.get("note", ""))
            elif fn_name in ("acknowledge", "reopen"):
                fn(ctx.conn, ctx.user, int(alert_id), ctx.form.get("note", ""))
            else:
                fn(ctx.conn, ctx.user, int(alert_id), ctx.form.get("note", ""))
        except ValidationFailed as e:
            ctx.flash("error", "; ".join(e.errors.values()))
        else:
            ctx.flash("ok", f"Alert {fn_name.replace('acknowledge', 'acknowledged').replace('resolve', 'resolved').replace('dismiss', 'dismissed').replace('reopen', 'reopened')}.")
        return ctx.redirect(ctx.back(f"/watchtower/alerts/{alert_id}"))
    run.__name__ = "alert_" + fn_name
    return handler(perm="alert.manage", post=True)(run)


alert_ack, alert_resolve, alert_dismiss, alert_reopen, alert_assign = (_alert_action(n) for n in ("acknowledge", "resolve", "dismiss", "reopen", "assign"))


# ================================================================ assistant
def _status(cfg):
    return {"llm": providers.llm_status(cfg), "stt": providers.stt_status(cfg), "tts": providers.tts_status(cfg), "name": cfg.assistant_name,
            "retention_days": cfg.transcript_retention_days}


@handler(perm="ask.query")
def assistant_page(ctx):
    cid = _int(_qs(ctx.request, "c")) or agent.latest_conversation_id(ctx.conn, ctx.user)
    return ctx.render("assistant.html", cid=cid, msgs=agent.history(ctx.conn, ctx.user, cid) if cid else [], status=_status(ctx.cfg),
                      voice_ok=can(ctx.user["role"], "voice.use"), examples=_examples(ctx))


def _examples(ctx) -> list[str]:
    """Suggested questions, limited to what this user's role can ask, using real names from this database."""
    conn, role = ctx.conn, ctx.user["role"]
    q = lambda sql: (conn.execute(sql).fetchone() or [None])[0]
    lot, party = q("SELECT lot_no FROM lots WHERE kind='finished' ORDER BY id DESC LIMIT 1"), q("SELECT name FROM parties WHERE kind='job_work' ORDER BY id LIMIT 1")
    ex = ["Give me today's factory update"] + (["What requires management attention?"] if can(role, "alert.view") else [])
    if can(role, "processing.view"):
        ex += ["Which jobs are overdue?", "Compare dyeing and printing losses", "Show reconciliation exceptions"] + ([f"Which jobs are with {party}?"] if party else [])
    if can(role, "stock.view"):
        ex += ["What is on hand in the finished store?", "What is the oldest stock in the raw store?", "Which supplier did we buy the most from?"] + ([f"Where is lot {lot}?", f"Where did {lot} go next?"] if lot else [])
    if can(role, "dispatch.view"):
        ex += ["Who are our top customers?"]
    if can(role, "gate.view"):
        ex += ["Show gate entries today"]
    if can(role, "mh.view"):
        ex += ["Status of Multihead orders"]
    if can(role, "acc.view"):
        ex += ["Which accessories are running low?"]
    if can(role, "fin.view"):
        ex += ["Who owes us money?", "How much do we owe suppliers?", "Cash position this week"]
    if can(role, "report.view") and can(role, "dispatch.view"):
        ex += ["Prepare a dispatch report for this month"]
    if can(role, "processing.create") and party:
        ex += [f"Draft a follow-up to {party} about the delay"]
    return ex[:16]


@handler(perm="ask.query", post=True)
def assistant_form(ctx):
    """No-JavaScript path: same engine, page reload."""
    try:
        out = agent.handle(ctx.conn, ctx.cfg, ctx.user, ctx.form.get("text", ""), channel="text", conversation_id=_int(ctx.form.get("conversation_id")) or None, tz=display_tz(ctx.cfg))
    except ValidationFailed as e:
        ctx.flash("error", "; ".join(e.errors.values()))
        return ctx.redirect("/assistant")
    return ctx.redirect(f"/assistant?c={out['conversation_id']}")


def _json_error(e: Exception, code: int):
    return JSONResponse({"error": str(e) if not isinstance(e, ValidationFailed) else "; ".join(e.errors.values())}, status_code=code)


@handler(perm="ask.query", post=True)
def api_assistant_message(ctx):
    channel = "voice" if ctx.form.get("channel") == "voice" else "text"
    try:
        out = agent.handle(ctx.conn, ctx.cfg, ctx.user, ctx.form.get("text", ""), channel=channel, conversation_id=_int(ctx.form.get("conversation_id")) or None, tz=display_tz(ctx.cfg))
    except ValidationFailed as e:
        return _json_error(e, 422)
    except PermissionDenied as e:
        return _json_error(e, 403)
    return JSONResponse(out)


@handler()
def api_guide(ctx):
    path = _qs(ctx.request, "path") or "/"
    if not path.startswith("/") or path.startswith("//") or len(path) > 200:
        path = "/"
    acts = briefing.actions(ctx.conn, ctx.user, ctx.cfg.currency)
    return JSONResponse({"greeting": briefing.greeting(ctx.user, display_tz(ctx.cfg)), "focus": briefing.role_focus(ctx.user["role"]),
                         "actions": acts[:8], "total": len(acts), "urgent": sum(1 for a in acts if a["sev"] == "high"),
                         "help": briefing.page_help(path.split("?")[0]), "can_ask": can(ctx.user["role"], "ask.query"),
                         "assistant": ctx.cfg.assistant_name})


@handler(perm="ask.query", post=True)
def api_assistant_new(ctx):
    return JSONResponse({"conversation_id": agent.new_conversation(ctx.conn, ctx.user)})


@handler(perm="ask.query", post=True)
def api_assistant_delete(ctx):
    agent.delete_conversation(ctx.conn, ctx.user, _int(ctx.form.get("conversation_id")))
    return JSONResponse({"ok": True})


@handler(perm="ask.query")
def api_assistant_status(ctx):
    return JSONResponse(_status(ctx.cfg))


MAX_AUDIO = 8 * 1024 * 1024


@handler(perm="voice.use", post=True)
def api_voice_transcribe(ctx):
    up = ctx.multi.get("audio") if ctx.multi is not None else None
    if up is None or not hasattr(up, "file"):
        return JSONResponse({"error": "No audio was sent."}, status_code=422)
    data = up.file.read(MAX_AUDIO + 1)
    if not data:
        return JSONResponse({"error": "The recording was empty."}, status_code=422)
    if len(data) > MAX_AUDIO:
        return JSONResponse({"error": "Recording too long (max 8 MB)."}, status_code=413)
    try:
        text = providers.transcribe(ctx.cfg, data, "speech.webm", (up.content_type or "audio/webm").split(";")[0])
    except providers.ProviderError as e:
        audit.record_standalone(ctx.conn, ctx.user, "voice.transcribe", entity_type="voice", outcome="failed", detail={"reason": str(e)[:120]})
        return JSONResponse({"error": str(e)}, status_code=502 if "No speech" not in str(e) else 501)
    audit.record_standalone(ctx.conn, ctx.user, "voice.transcribe", entity_type="voice", detail={"bytes": len(data)})   # audio itself is never stored
    return JSONResponse({"text": text})


@handler(perm="voice.use", post=True)
def api_voice_speak(ctx):
    text = (ctx.form.get("text") or "").strip()[:600]
    if not text:
        return JSONResponse({"error": "Nothing to speak."}, status_code=422)
    try:
        audio, mime = providers.synthesize(ctx.cfg, text)
    except providers.ProviderError as e:
        return JSONResponse({"error": str(e)}, status_code=501 if "No text-to-speech" in str(e) else 502)
    return Response(audio, media_type=mime)


# ================================================================ documents
@handler(perm="doc.view")
def documents_page(ctx):
    return ctx.render("documents.html", rows=documents.list_documents(ctx.conn, ctx.user), tools=documents.tools_available(), llm=providers.llm_status(ctx.cfg),
                      max_mb=ctx.cfg.max_upload_mb)


@handler(perm="doc.upload", post=True)
def document_upload(ctx):
    up = ctx.multi.get("file") if ctx.multi is not None else None
    if up is None or not hasattr(up, "file") or not getattr(up, "filename", ""):
        ctx.flash("error", "Choose a PDF, PNG or JPEG file first.")
        return ctx.redirect("/documents")
    data = up.file.read(ctx.cfg.max_upload_mb * 1024 * 1024 + 1)
    try:
        did = documents.upload(ctx.conn, ctx.cfg, ctx.user, up.filename, data)
    except ValidationFailed as e:
        ctx.flash("error", "; ".join(e.errors.values()))
        return ctx.redirect("/documents")
    ctx.flash("ok", "Document read. Review every value before creating a draft.")
    return ctx.redirect(f"/documents/{did}")


def _doc_review(ctx, doc_id, errors=None, values=None, status=200):
    row = documents.get(ctx.conn, ctx.user, int(doc_id))
    ex = documents.parsed(row)
    f = {k: v["value"] for k, v in ex.get("fields", {}).items()}
    vals = values if values is not None else {"supplier_id": f.get("supplier_id", ""), "item_id": f.get("item_id", ""), "lot_no": f.get("lot_no", ""), "qty": f.get("qty", ""),
                                               "supplier_ref": f.get("supplier_ref", ""), "vehicle_no": f.get("vehicle_no", "")}
    from .webcore import _lists
    lists = _lists(ctx.conn)
    return ctx.render("document.html", status, d=row, ex=ex, errors=errors or {}, values=vals, **lists)


@handler(perm="doc.view")
def document_page(ctx, doc_id):
    return _doc_review(ctx, doc_id)


@handler(perm="doc.view")
def document_file(ctx, doc_id):
    row = documents.get(ctx.conn, ctx.user, int(doc_id))
    p = documents.file_path(ctx.cfg, row)
    audit.record_standalone(ctx.conn, ctx.user, "document.download", entity_type="document", entity_ref=str(row["id"]))
    return Response(p.read_bytes(), media_type=row["mime"], headers={"content-disposition": f'inline; filename="document-{row["id"]}{p.suffix}"'})


@handler(perm="receipt.create", post=True)
def document_to_receipt(ctx, doc_id):
    item = ctx.conn.execute("SELECT unit FROM items WHERE id=?", (_int(ctx.form.get("item_id")),)).fetchone()
    form = {**ctx.form, "unit": (item["unit"] if item else ctx.form.get("unit", ""))}
    if not form.get("location_id"):
        loc = ctx.conn.execute("SELECT id FROM locations WHERE kind='raw_store' ORDER BY id LIMIT 1").fetchone()
        form["location_id"] = loc["id"] if loc else ""
    try:
        rid = documents.create_receipt_draft(ctx.conn, ctx.user, int(doc_id), form)
    except ValidationFailed as e:
        return _doc_review(ctx, doc_id, e.errors, ctx.form, 422)
    ctx.flash("ok", "Receipt DRAFT created from the reviewed values. It does not affect stock until someone posts it.")
    return ctx.redirect(f"/fabrics/receipts/{rid}")


@handler(perm="doc.upload", post=True)
def document_reject(ctx, doc_id):
    documents.reject(ctx.conn, ctx.user, int(doc_id))
    ctx.flash("ok", "Document marked as rejected.")
    return ctx.redirect("/documents")


# ================================================================ registry settings
@handler(perm="settings.edit", post=True)
def settings_registry_save(ctx):
    changed, errors = 0, []
    for key, spec in settings.REGISTRY.items():
        if key not in ctx.form and spec["kind"] != "bool":
            continue
        raw = ctx.form.get(key)
        value = ("1" if raw == "1" else "0") if spec["kind"] == "bool" else (raw or "")
        try:
            if settings.set_value(ctx.conn, ctx.user, key, value):
                changed += 1
        except (ValidationFailed, BusinessRuleViolation) as e:
            errors.append(f"{spec['label']}: " + ("; ".join(e.errors.values()) if isinstance(e, ValidationFailed) else str(e)))
    if errors:
        ctx.flash("error", " | ".join(errors))
    else:
        ctx.flash("ok", f"{changed} setting(s) changed." if changed else "No changes.")
    return ctx.redirect("/settings")


G, P = ["GET"], ["POST"]
ROUTES = [
    Route("/reports", reports_hub, methods=G), Route("/reports/{key}", report_page, methods=G),
    Route("/watchtower", watchtower_page, methods=G), Route("/watchtower/scan", watchtower_scan, methods=P),
    Route("/watchtower/alerts/{alert_id:int}", alert_detail, methods=G),
    Route("/watchtower/alerts/{alert_id:int}/acknowledge", alert_ack, methods=P), Route("/watchtower/alerts/{alert_id:int}/resolve", alert_resolve, methods=P),
    Route("/watchtower/alerts/{alert_id:int}/dismiss", alert_dismiss, methods=P), Route("/watchtower/alerts/{alert_id:int}/reopen", alert_reopen, methods=P),
    Route("/watchtower/alerts/{alert_id:int}/assign", alert_assign, methods=P),
    Route("/assistant", assistant_page, methods=G), Route("/assistant", assistant_form, methods=P),
    Route("/api/guide", api_guide, methods=G),
    Route("/api/assistant/message", api_assistant_message, methods=P), Route("/api/assistant/new", api_assistant_new, methods=P),
    Route("/api/assistant/delete", api_assistant_delete, methods=P), Route("/api/assistant/status", api_assistant_status, methods=G),
    Route("/api/voice/transcribe", api_voice_transcribe, methods=P), Route("/api/voice/speak", api_voice_speak, methods=P),
    Route("/documents", documents_page, methods=G), Route("/documents/upload", document_upload, methods=P),
    Route("/documents/{doc_id:int}", document_page, methods=G), Route("/documents/{doc_id:int}/file", document_file, methods=G),
    Route("/documents/{doc_id:int}/receipt-draft", document_to_receipt, methods=P), Route("/documents/{doc_id:int}/reject", document_reject, methods=P),
    Route("/settings/registry", settings_registry_save, methods=P),
]
