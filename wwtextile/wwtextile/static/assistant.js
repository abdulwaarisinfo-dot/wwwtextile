/* Assistant UI: text + push-to-talk voice. Server enforces all rules; this file only talks to /api/assistant/* and /api/voice/*. */
(function () {
  "use strict";
  var card = document.getElementById("a-card");
  if (!card) return;
  var csrf = (document.querySelector('meta[name="csrf"]') || {}).content || "";
  var log = document.getElementById("log"), form = document.getElementById("a-form"), input = document.getElementById("a-text");
  var cidEl = document.getElementById("a-cid"), name = card.dataset.name;
  var cid = card.dataset.cid || "";
  var WRITE = /\b(prepare|create|make|draft|issue|dispatch|receipt|result|job)\b/i;
  var busy = false;

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function link(href, text) { var a = el("a", "a-chip", text); a.href = href; return a; }

  function renderMsg(m) {
    var wrap = el("div", "a-msg " + m.role);
    wrap.appendChild(el("div", "a-who", (m.role === "user" ? "You" : name) + (m.channel === "voice" ? " · voice" : "")));
    wrap.appendChild(el("div", "a-text", m.content));
    var meta = m.meta || {};
    if (meta.notice) wrap.appendChild(el("div", "a-note small muted", meta.notice));
    (meta.cards || []).forEach(function (c) {
      var t = el("table", "a-table"); var th = el("thead"), tr = el("tr");
      (c.columns || []).forEach(function (h) { tr.appendChild(el("th", null, h)); }); th.appendChild(tr); t.appendChild(th);
      var tb = el("tbody"); (c.rows || []).forEach(function (r) { var row = el("tr"); r.forEach(function (v) { row.appendChild(el("td", null, v)); }); tb.appendChild(row); });
      t.appendChild(tb); var cap = el("div", "small muted", c.title || ""); wrap.appendChild(cap); var sc = el("div", "table-wrap"); sc.appendChild(t); wrap.appendChild(sc);
    });
    (meta.evidence || []).forEach(function (e) { wrap.appendChild(link(e.url, e.label)); });
    (meta.drafts || []).forEach(function (d) { wrap.appendChild(link(d.url, d.label)); });
    if (meta.open_url) { var o = link(meta.open_url, "Open"); o.className = "btn btn-secondary btn-sm"; wrap.appendChild(o); }
    if (meta.clarify && m === lastAssistant) {
      var box = el("div", "a-clarify");
      meta.clarify.options.forEach(function (op) { var b = el("button", "btn btn-secondary btn-sm", op.label); b.type = "button"; b.addEventListener("click", function () { send(op.value, "text"); }); box.appendChild(b); });
      wrap.appendChild(box);
    }
    return wrap;
  }
  var lastAssistant = null;
  function renderAll(list) {
    log.textContent = "";
    lastAssistant = null;
    for (var i = list.length - 1; i >= 0; i--) if (list[i].role === "assistant") { lastAssistant = list[i]; break; }
    if (!list.length) log.appendChild(el("div", "muted small", "No messages yet. Try one of the examples below."));
    list.forEach(function (m) { log.appendChild(renderMsg(m)); });
    log.scrollTop = log.scrollHeight;
  }
  var history = [];
  try { history = JSON.parse(document.getElementById("a-history").textContent) || []; } catch (e) { history = []; }
  renderAll(history);

  function post(url, fd) { fd.append("csrf", csrf); return fetch(url, { method: "POST", body: fd, credentials: "same-origin" }); }

  function send(text, channel) {
    text = (text || "").trim();
    if (!text || busy) return;
    busy = true; document.getElementById("a-send").disabled = true;
    history.push({ role: "user", channel: channel, content: text, meta: {} });
    var pending = { role: "assistant", channel: channel, content: "Working…", meta: {} }; history.push(pending); renderAll(history);
    var fd = new FormData(); fd.append("text", text); fd.append("channel", channel); fd.append("conversation_id", cid);
    post("/api/assistant/message", fd).then(function (r) {
      return r.json().catch(function () { return { error: "The server returned an unexpected response (HTTP " + r.status + ")." }; }).then(function (j) { return { r: r, j: j }; });
    }).then(function (x) {
      var j = x.j;
      if (x.r.ok && j.text) {
        cid = String(j.conversation_id); cidEl.value = cid;
        pending.content = j.text; pending.meta = { evidence: j.evidence, cards: j.cards, drafts: j.drafts, open_url: j.open_url, clarify: j.clarify, notice: j.notice };
        renderAll(history);
        if (speakOn()) speak(j.speak || j.text);
      } else { pending.content = j.error || ("Request failed (HTTP " + x.r.status + ")."); renderAll(history); }
    }).catch(function () { pending.content = "Could not reach the server."; renderAll(history); })
      .then(function () { busy = false; document.getElementById("a-send").disabled = false; input.value = ""; input.focus(); });
  }
  form.addEventListener("submit", function (e) { e.preventDefault(); send(input.value, "text"); });
  document.querySelectorAll("[data-example]").forEach(function (b) { b.addEventListener("click", function () { send(b.dataset.example, "text"); }); });
  document.getElementById("a-new").addEventListener("click", function () {
    var fd = new FormData(); post("/api/assistant/new", fd).then(function (r) { return r.json(); }).then(function (j) { cid = String(j.conversation_id); cidEl.value = cid; history = []; renderAll(history); });
  });

  // ------------------------------------------------------------ voice
  if (card.dataset.voice !== "1") return;
  var voiceBox = document.getElementById("a-voice"), mic = document.getElementById("a-mic"), stateEl = document.getElementById("a-voice-state");
  var speakBox = document.getElementById("a-speak"), wakeBox = document.getElementById("a-wake"), langSel = document.getElementById("a-lang");
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  var canRecord = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  var serverStt = card.dataset.sttServer === "1";
  var canSynth = "speechSynthesis" in window, serverTts = card.dataset.ttsServer === "1";
  var mode = SR ? "browser" : (serverStt && canRecord ? "server" : null);
  function state(t) { stateEl.textContent = t; }
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  voiceBox.hidden = false;
  if (!mode) {
    state("Voice input is not available here: this browser has no speech recognition and the server has no speech-to-text provider. Use the text box.");
    wakeBox.disabled = true;
  } else { mic.hidden = false; state("Voice ready (" + (mode === "browser" ? "browser recognition" : "server transcription") + ")."); }
  if (!canSynth && !serverTts) { speakBox.disabled = true; }
  speakBox.checked = lsGet("ww.speak") === "1" && !speakBox.disabled;
  speakBox.addEventListener("change", function () { lsSet("ww.speak", speakBox.checked ? "1" : "0"); if (!speakBox.checked && canSynth) speechSynthesis.cancel(); });
  if (lsGet("ww.lang")) langSel.value = lsGet("ww.lang");
  langSel.addEventListener("change", function () { lsSet("ww.lang", langSel.value); });
  function speakOn() { return speakBox && speakBox.checked; }

  function speak(text) {
    if (serverTts) {
      var fd = new FormData(); fd.append("text", text);
      post("/api/voice/speak", fd).then(function (r) { if (!r.ok) throw new Error(); return r.blob(); }).then(function (b) {
        var a = new Audio(URL.createObjectURL(b)); a.play().catch(function () { state("Could not play audio."); });
      }).catch(function () { browserSpeak(text); });
    } else browserSpeak(text);
  }
  function browserSpeak(text) {
    if (!canSynth) return;
    speechSynthesis.cancel(); var u = new SpeechSynthesisUtterance(text); u.lang = langSel.value; speechSynthesis.speak(u);
  }

  function onHeard(text) {
    text = (text || "").trim();
    if (!text) { state("Heard nothing. Try again."); return; }
    var re = new RegExp("^\\s*(hey\\s+|ok\\s+|okay\\s+)?" + name + "\\b[\\s,:-]*", "i");
    text = text.replace(re, "");
    input.value = text;
    if (WRITE.test(text)) { state("Heard: “" + text + "”. Check it, then press Send (drafting requests are not sent automatically)."); input.focus(); }
    else { state("Heard: “" + text + "”"); send(text, "voice"); }
  }

  // push-to-talk
  var rec = null, listening = false, chunks = [], stream = null, mr = null;
  function startBrowser() {
    rec = new SR(); rec.lang = langSel.value; rec.interimResults = false; rec.maxAlternatives = 1; rec.continuous = false;
    rec.onresult = function (ev) { onHeard(ev.results[0][0].transcript); };
    rec.onerror = function (ev) { state(ev.error === "not-allowed" ? "Microphone permission was denied." : ev.error === "no-speech" ? "No speech detected." : "Speech recognition error: " + ev.error); };
    rec.onend = function () { listening = false; mic.setAttribute("aria-pressed", "false"); mic.textContent = "Hold to talk"; };
    rec.start(); listening = true; state("Listening…");
  }
  function startServer() {
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) {
      stream = s; chunks = []; mr = new MediaRecorder(s);
      mr.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      mr.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        var blob = new Blob(chunks, { type: mr.mimeType || "audio/webm" }); chunks = [];
        if (!blob.size) { state("Nothing was recorded."); return; }
        state("Transcribing…"); var fd = new FormData(); fd.append("audio", blob, "speech.webm");
        post("/api/voice/transcribe", fd).then(function (r) { return r.json().then(function (j) { return { r: r, j: j }; }); })
          .then(function (x) { if (x.r.ok) onHeard(x.j.text); else state(x.j.error || "Transcription failed."); })
          .catch(function () { state("Transcription request failed."); });
      };
      mr.start(); listening = true; state("Recording…");
    }).catch(function () { state("Microphone permission was denied or no microphone was found."); listening = false; mic.setAttribute("aria-pressed", "false"); mic.textContent = "Hold to talk"; });
  }
  function down(e) { if (!mode || listening) return; e.preventDefault(); mic.setAttribute("aria-pressed", "true"); mic.textContent = "Listening… release to send"; if (canSynth) speechSynthesis.cancel(); mode === "browser" ? startBrowser() : startServer(); }
  function up(e) { if (!listening) return; e.preventDefault(); if (mode === "browser" && rec) rec.stop(); else if (mr && mr.state !== "inactive") { mr.stop(); mic.setAttribute("aria-pressed", "false"); mic.textContent = "Hold to talk"; listening = false; } }
  mic.addEventListener("mousedown", down); mic.addEventListener("touchstart", down, { passive: false });
  window.addEventListener("mouseup", up); window.addEventListener("touchend", up);
  mic.addEventListener("keydown", function (e) { if ((e.key === " " || e.key === "Enter") && !e.repeat) down(e); });
  mic.addEventListener("keyup", function (e) { if (e.key === " " || e.key === "Enter") up(e); });

  // optional wake-word mode (browser recognition only; opt-in; indicator always visible)
  var wake = null, wakeWanted = false;
  function startWake() {
    if (!SR || !wakeWanted) return;
    wake = new SR(); wake.lang = langSel.value; wake.continuous = true; wake.interimResults = false;
    wake.onresult = function (ev) {
      var t = ev.results[ev.results.length - 1][0].transcript.trim();
      if (new RegExp("^\\s*(hey\\s+|ok\\s+|okay\\s+)?" + name + "\\b", "i").test(t)) onHeard(t);
    };
    wake.onerror = function (ev) { if (ev.error === "not-allowed") { wakeWanted = false; wakeBox.checked = false; state("Microphone permission was denied."); } };
    wake.onend = function () { if (wakeWanted) setTimeout(startWake, 400); else state("Wake-word listening is off."); };
    try { wake.start(); state("Listening for “" + name + "” (microphone is open while this page is open)."); } catch (e) {}
  }
  if (!SR) { wakeBox.disabled = true; }
  wakeBox.addEventListener("change", function () { wakeWanted = wakeBox.checked; if (wakeWanted) startWake(); else if (wake) wake.stop(); });
  window.addEventListener("pagehide", function () { wakeWanted = false; if (wake) try { wake.stop(); } catch (e) {} });
})();
