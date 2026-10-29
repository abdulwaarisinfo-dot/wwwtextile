/* Floating guide: what needs the user now, how to use this page, and a quick question box.
   Content comes from /api/guide (fixed rules over live records); questions go to the normal assistant API. */
(function () {
  "use strict";
  var root = document.getElementById("guide"); if (!root) return;
  var fab = document.getElementById("guide-fab"), panel = document.getElementById("guide-panel"), body = document.getElementById("guide-body");
  var badge = document.getElementById("guide-badge"), hello = document.getElementById("guide-hello");
  var csrf = (document.querySelector('meta[name="csrf"]') || {}).content || "";
  var data = null, tab = "now", cid = "";
  function el(t, c, x) { var e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; }
  function a(href, text, cls) { var e = el("a", cls, text); e.href = href; return e; }

  function renderNow() {
    body.textContent = "";
    if (data.focus) body.appendChild(el("p", "muted small", data.focus));
    if (!data.actions.length) { body.appendChild(el("p", null, "Nothing is waiting for you right now.")); return; }
    data.actions.forEach(function (x) {
      var d = el("div", "g-item " + x.sev);
      d.appendChild(el("strong", null, x.title)); d.appendChild(el("div", "small muted", x.why + (x.waiting_on ? " Acts on this: " + x.waiting_on + "." : "")));
      d.appendChild(a(x.url, x.cta + " →")); body.appendChild(d);
    });
    if (data.total > data.actions.length) body.appendChild(el("p", "small muted", "+" + (data.total - data.actions.length) + " lower-priority items on the Dashboard."));
    body.appendChild(el("p", "small muted", "Fixed rules over live records, not an AI model."));
  }
  function renderPage() {
    body.textContent = ""; var h = data.help;
    if (!h) { body.appendChild(el("p", "muted", "No guide for this page yet. Use search, or ask in the Ask tab.")); return; }
    body.appendChild(el("h3", null, h.title));
    [["What it is for", h.what], ["When to use it", h.when]].forEach(function (p) { var d = el("p"); d.appendChild(el("strong", null, p[0] + ": ")); d.appendChild(document.createTextNode(p[1])); body.appendChild(d); });
    body.appendChild(el("strong", null, "How"));
    var ol = el("ol"); h.steps.forEach(function (s) { ol.appendChild(el("li", null, s)); }); body.appendChild(ol);
    var n = el("p"); n.appendChild(el("strong", null, "Next: ")); n.appendChild(document.createTextNode(h.next)); body.appendChild(n);
  }
  function renderAsk() {
    body.textContent = "";
    if (!data.can_ask) { body.appendChild(el("p", "muted", "Your role cannot use the assistant.")); return; }
    body.appendChild(el("p", "small muted", "Ask " + data.assistant + " about live records. Answers come from your records and are limited to what your role may see."));
    var f = el("form", "g-ask"), i = el("input"); i.type = "text"; i.placeholder = "e.g. what is overdue?"; i.setAttribute("aria-label", "Question"); i.maxLength = 500;
    var b = el("button", "btn btn-sm", "Ask"); b.type = "submit"; f.appendChild(i); f.appendChild(b); body.appendChild(f);
    var out = el("div"); body.appendChild(out);
    body.appendChild(a("/assistant", "Open the full assistant →", "small"));
    f.addEventListener("submit", function (e) {
      e.preventDefault(); var q = i.value.trim(); if (!q) return; b.disabled = true; out.textContent = ""; out.appendChild(el("div", "g-ans", "Working…"));
      var fd = new FormData(); fd.append("csrf", csrf); fd.append("text", q); fd.append("channel", "text"); fd.append("conversation_id", cid);
      fetch("/api/assistant/message", { method: "POST", body: fd, credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (j) {
        out.textContent = ""; if (j.error) { out.appendChild(el("div", "g-ans", j.error)); return; }
        if (j.conversation_id) cid = String(j.conversation_id);
        out.appendChild(el("div", "g-ans", j.text || "No answer."));
        (j.evidence || []).concat(j.drafts || []).forEach(function (e) { out.appendChild(a(e.url, e.label, "a-chip")); out.appendChild(document.createTextNode(" ")); });
        if (j.open_url) out.appendChild(a(j.open_url, "Open →"));
        var meta = j;
        if (meta.notice) out.appendChild(el("div", "small muted", meta.notice));
      }).catch(function () { out.textContent = ""; out.appendChild(el("div", "g-ans", "Could not reach the server.")); }).then(function () { b.disabled = false; });
    });
  }
  function render() {
    if (!data) return;
    hello.textContent = data.greeting;
    Array.prototype.forEach.call(document.querySelectorAll(".guide-tabs button"), function (t) { t.setAttribute("aria-selected", t.dataset.tab === tab ? "true" : "false"); });
    (tab === "now" ? renderNow : tab === "page" ? renderPage : renderAsk)();
  }
  function load() {
    return fetch("/api/guide?path=" + encodeURIComponent(location.pathname), { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (j) {
      data = j; badge.hidden = !j.urgent; badge.textContent = j.urgent || ""; render();
    }).catch(function () { body.textContent = "The guide could not load."; });
  }
  fab.addEventListener("click", function () { var open = panel.hidden; panel.hidden = !open; fab.setAttribute("aria-expanded", open); if (open) load(); });
  document.getElementById("guide-close").addEventListener("click", function () { panel.hidden = true; fab.setAttribute("aria-expanded", "false"); fab.focus(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !panel.hidden) { panel.hidden = true; fab.focus(); } });
  Array.prototype.forEach.call(document.querySelectorAll(".guide-tabs button"), function (t) { t.addEventListener("click", function () { tab = t.dataset.tab; render(); }); });
  load();
})();
