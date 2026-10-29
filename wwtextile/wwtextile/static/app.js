/* Progressive enhancement only. Every page works as plain forms; the server enforces all rules. */
(function () {
  "use strict";
  var body = document.body;
  var toggle = document.getElementById("nav-toggle");
  var KEY = "ww.nav.collapsed";

  function isNarrow() { return window.matchMedia("(max-width: 960px)").matches; }
  if (toggle) {
    toggle.classList.add("show");
    try { if (!isNarrow() && localStorage.getItem(KEY) === "1") body.classList.add("nav-collapsed"); } catch (e) {}
    toggle.addEventListener("click", function () {
      if (isNarrow()) {
        var open = body.classList.toggle("nav-open");
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      } else {
        var c = body.classList.toggle("nav-collapsed");
        toggle.setAttribute("aria-expanded", c ? "false" : "true");
        try { localStorage.setItem(KEY, c ? "1" : "0"); } catch (e) {}
      }
    });
    document.addEventListener("click", function (e) {
      if (isNarrow() && body.classList.contains("nav-open") && !e.target.closest("#sidebar") && !e.target.closest("#nav-toggle")) {
        body.classList.remove("nav-open");
        toggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  // "/" focuses global search
  document.addEventListener("keydown", function (e) {
    var t = e.target.tagName;
    if (e.key === "/" && t !== "INPUT" && t !== "TEXTAREA" && t !== "SELECT") {
      var s = document.getElementById("global-search");
      if (s) { e.preventDefault(); s.focus(); }
    }
  });

  // confirm dialogs for consequential or destructive actions
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (e.defaultPrevented) return;   // handled in-page (e.g. the assistant sends via fetch); nothing is navigating, so don't lock the form
    var msg = f.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    // prevent double submission
    if (f.getAttribute("data-submitted") === "1") { e.preventDefault(); return; }
    f.setAttribute("data-submitted", "1");
    setTimeout(function () {
      f.querySelectorAll("button[type=submit], button:not([type])").forEach(function (b) { b.classList.add("is-loading"); b.setAttribute("aria-busy", "true"); });
    }, 0);
  });
  // back/forward cache: re-enable forms
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-submitted]").forEach(function (f) {
      f.removeAttribute("data-submitted");
      f.querySelectorAll(".is-loading").forEach(function (b) { b.classList.remove("is-loading"); b.removeAttribute("aria-busy"); });
    });
  });

  // dismiss flash
  document.addEventListener("click", function (e) {
    if (e.target.matches("[data-dismiss]")) { var t = e.target.closest(".toast"); if (t) t.remove(); }
    if (e.target.matches("[data-print]")) { window.print(); }
  });

  // Live reconciliation PREVIEW on the result form. The server recomputes and is authoritative.
  function toMilli(v) {
    v = (v || "").replace(/,/g, "").trim();
    if (v === "") return 0;
    if (!/^\d+(\.\d{0,3})?$/.test(v)) return NaN;
    var p = v.split(".");
    return parseInt(p[0], 10) * 1000 + parseInt(((p[1] || "") + "000").slice(0, 3), 10);
  }
  function fmt(n) {
    var s = Math.abs(n), whole = Math.floor(s / 1000), frac = String(s % 1000);
    while (frac.length < 3) frac = "0" + frac;
    return (n < 0 ? "-" : "") + whole.toLocaleString("en-US") + "." + frac;
  }
  document.querySelectorAll("[data-variance-form]").forEach(function (form) {
    var issued = parseInt(form.getAttribute("data-issued"), 10);
    var out = form.querySelector("[data-variance-out]");
    function update() {
      var sum = 0, bad = false;
      form.querySelectorAll("input[name=accepted],input[name=rejected],input[name=wastage],input[name=shrinkage],input[name=remaining]").forEach(function (i) { var m = toMilli(i.value); if (isNaN(m)) bad = true; else sum += m; });
      out.classList.remove("bad", "exception");
      if (bad) { out.textContent = "Enter numbers with at most 3 decimals."; out.classList.add("bad"); return; }
      var v = issued - sum;
      if (v < 0) { out.textContent = "Reported " + fmt(sum) + " exceeds issued " + fmt(issued) + " by " + fmt(-v) + ". This cannot be posted."; out.classList.add("bad"); }
      else if (v === 0) { out.textContent = "Balanced: issued " + fmt(issued) + " = accounted " + fmt(sum) + ". (Preview — the server recalculates.)"; }
      else { out.textContent = "Unexplained variance " + fmt(v) + " — will be recorded as an open exception and stay in processing. (Preview — the server recalculates.)"; out.classList.add("exception"); }
    }
    form.addEventListener("input", update);
    update();
  });
})();
