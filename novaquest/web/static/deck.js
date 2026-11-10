/* Chapter presentations (1.27.0): slides shown inside NovaQuest only.
   Arrow keys, Page Up/Down (clickers), space or the buttons move; A or Enter shows the answer; F is full screen.
   The presenter window (notes, timer, next slide) and the projector window stay in step through a BroadcastChannel
   (both open in the same browser). Saving, printing, copying and dragging images are turned off; a watermark is on
   every slide. A photo or screenshot can't be prevented by any web page. */
(function () {
  "use strict";
  var root = document.querySelector("[data-deckshow]");
  if (!root) return;
  var slides = Array.prototype.slice.call(root.querySelectorAll("[data-dslide]")), at = 0;
  var presenter = root.getAttribute("data-presenter") === "1";
  var q = function (sel) { return root.querySelector(sel); };
  var count = q("[data-count]"), prev = q("[data-prev]"), next = q("[data-next]"), reveal = q("[data-reveal-btn]");
  var chan = null;
  try { chan = "BroadcastChannel" in window ? new BroadcastChannel(root.getAttribute("data-channel")) : null; } catch (e) { chan = null; }

  function answer() { return slides[at].querySelector("[data-reveal]"); }
  function show(i, quiet) {
    at = Math.max(0, Math.min(slides.length - 1, i));
    slides.forEach(function (s, j) { s.hidden = j !== at; });
    var ans = answer();
    reveal.hidden = !ans;
    reveal.textContent = ans && !ans.hidden ? "Hide answer" : (slides[at].classList.contains("k-activity") ? "Questions" : "Show answer");
    count.textContent = (at + 1) + " / " + slides.length;
    prev.disabled = at === 0; next.disabled = at === slides.length - 1;
    try { history.replaceState(null, "", "#" + (at + 1)); } catch (e) { /* file or sandbox */ }
    if (presenter) panel();
    if (chan && !quiet) chan.postMessage({ go: at, open: !!(ans && !ans.hidden) });
  }
  function toggle(quiet) {
    var ans = answer();
    if (!ans) return;
    ans.hidden = !ans.hidden;
    slides[at].classList.toggle("revealed", !ans.hidden);
    reveal.textContent = ans.hidden ? "Show answer" : "Hide answer";
    if (chan && !quiet) chan.postMessage({ go: at, open: !ans.hidden });
  }
  if (chan) chan.onmessage = function (e) {
    var m = e.data || {};
    if (typeof m.go === "number") {
      if (m.go !== at) show(m.go, true);
      var ans = answer();
      if (ans && ans.hidden === !!m.open) toggle(true);
    }
  };

  // presenter: notes for this slide, the next slide's title, and the period's clock with this part's time budget
  var started = Date.now();
  function panel() {
    var s = slides[at], n = slides[at + 1];
    var note = s.querySelector("[data-notes]");
    q("[data-note-now]").textContent = note ? note.textContent : "";
    q("[data-next-title]").textContent = n ? (n.getAttribute("data-title") || "") : "End of the period";
    q("[data-stepinfo]").textContent = (s.getAttribute("data-step") || "") + " · about " + s.getAttribute("data-step-min") + " min";
  }
  if (presenter) {
    var clock = q("[data-clock]"), total = parseInt(root.getAttribute("data-minutes"), 10) || 40;
    setInterval(function () {
      var sec = Math.floor((Date.now() - started) / 1000), m = Math.floor(sec / 60);
      clock.textContent = (m < 10 ? "0" : "") + m + ":" + ("0" + (sec % 60)).slice(-2) + " of " + total + " min";
      clock.classList.toggle("over", m >= total);
    }, 1000);
    q("[data-clock-reset]").addEventListener("click", function () { started = Date.now(); });
  }

  prev.addEventListener("click", function () { show(at - 1); });
  next.addEventListener("click", function () { show(at + 1); });
  reveal.addEventListener("click", function () { toggle(); });
  q("[data-period]").addEventListener("change", function (e) { location.href = e.target.value; });
  var pv = q("[data-open-presenter]");
  if (pv) pv.addEventListener("click", function () {
    window.open(root.getAttribute("data-present-url") + "#" + (at + 1), "nq-presenter", "width=1100,height=760");
  });
  var full = q("[data-full]");
  if (full) {
    if (!document.documentElement.requestFullscreen) full.hidden = true;
    full.addEventListener("click", function () {
      if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(function () {});
    });
  }
  document.addEventListener("keydown", function (e) {
    var k = (e.key || "").toLowerCase();
    if ((e.ctrlKey || e.metaKey) && (k === "s" || k === "p" || k === "c" || k === "u" || k === "a")) { e.preventDefault(); return; }
    if (e.target.closest && e.target.closest("select, input, textarea")) return;
    if (e.key === "ArrowRight" || e.key === "PageDown" || e.key === " " || e.key === "ArrowDown") { e.preventDefault(); show(at + 1); }
    else if (e.key === "ArrowLeft" || e.key === "PageUp" || e.key === "ArrowUp") { e.preventDefault(); show(at - 1); }
    else if (k === "a" || e.key === "Enter") { if (!(e.target.closest && e.target.closest("button, a"))) { e.preventDefault(); toggle(); } }
    else if (k === "f" && full) { full.click(); }
    else if (e.key === "Home") { show(0); } else if (e.key === "End") { show(slides.length - 1); }
  });
  // keep the slides inside the app: no right-click saving, dragging images out, selecting text to copy, or printing
  ["contextmenu", "dragstart", "selectstart", "copy", "cut"].forEach(function (ev) {
    root.addEventListener(ev, function (e) { e.preventDefault(); });
  });
  window.addEventListener("beforeprint", function () { document.body.classList.add("no-print"); });
  show((parseInt(location.hash.slice(1), 10) || 1) - 1, true);
})();
