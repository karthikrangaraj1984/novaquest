/* NovaQuest client: small progressive enhancements. Every page works without it except live chat. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");
  var csrf = (document.querySelector('meta[name="csrf"]') || {}).content || "";

  function post(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) { var e = new Error(data.error || "Something went wrong. Please try again."); e.status = r.status; throw e; }
        return data;
      });
    });
  }
  function el(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
  function store(key, val) { try { if (val === undefined) return JSON.parse(localStorage.getItem(key) || "null"); localStorage.setItem(key, JSON.stringify(val)); } catch (e) { return null; } }

  /* ---------- autosave with offline queue (SX-3) ---------- */
  var form = document.querySelector("[data-autosave]");
  if (form) {
    var url = form.getAttribute("data-autosave"), status = document.getElementById("save-status");
    var qkey = "nq-queue-" + url, timers = {};
    function setStatus(text, offline) { if (status) { status.textContent = text; status.classList.toggle("offline", !!offline); } }
    function answerOf(qid) {
      var box = form.querySelector('[data-q="' + qid + '"]'), picked = box.querySelector("input[type=radio]:checked"), ta = box.querySelector("textarea, input[data-answer]");
      return picked ? { index: Number(picked.value) } : { text: ta ? ta.value : "" };
    }
    function flush() {
      var queue = store(qkey) || {}, ids = Object.keys(queue);
      if (!ids.length) return Promise.resolve();
      setStatus("Saving…");
      return ids.reduce(function (p, qid) {
        return p.then(function () {
          return post(url, { question_id: Number(qid), answer: queue[qid] }).then(function () {
            var q = store(qkey) || {}; delete q[qid]; store(qkey, q);
          });
        });
      }, Promise.resolve()).then(function () { setStatus("All answers saved"); })
        .catch(function (e) { setStatus(e.status ? e.message : "Offline: answers are kept on this device and will save when you reconnect.", !e.status); });
    }
    function queueSave(qid) {
      var q = store(qkey) || {}; q[qid] = answerOf(qid); store(qkey, q);
      clearTimeout(timers[qid]); timers[qid] = setTimeout(flush, 600);
    }
    form.addEventListener("change", function (e) { var b = e.target.closest("[data-q]"); if (b) queueSave(b.getAttribute("data-q")); });
    form.addEventListener("input", function (e) { var b = e.target.closest("[data-q]"); if (b && (e.target.tagName === "TEXTAREA" || e.target.hasAttribute("data-answer"))) queueSave(b.getAttribute("data-q")); });
    window.addEventListener("online", flush);
    var pending = store(qkey) || {};
    Object.keys(pending).forEach(function (qid) {        // restore unsaved work after a refresh
      var box = form.querySelector('[data-q="' + qid + '"]'); if (!box) return;
      var a = pending[qid];
      if (a.index != null) { var r = box.querySelector('input[value="' + a.index + '"]'); if (r) r.checked = true; }
      if (a.text != null) { var t = box.querySelector("textarea, input[data-answer]"); if (t) t.value = a.text; }
    });
    flush();
    form.addEventListener("submit", function (e) {
      if (form.getAttribute("data-flushing")) return;
      e.preventDefault(); form.setAttribute("data-flushing", "1");
      flush().then(function () { form.submit(); }, function () { form.submit(); });
    });
  }

  /* ---------- tutor hints during an assignment (SX-2) ---------- */
  document.querySelectorAll("[data-hint]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var box = document.getElementById(btn.getAttribute("aria-controls")); btn.disabled = true;
      post(btn.getAttribute("data-hint"), {}).then(function (d) {
        var m = el("div", "note info small", d.text); box.appendChild(m);
        if (d.citations && d.citations.length) m.appendChild(el("div", "tiny", "Source: " + d.citations[0]));
      }).catch(function (e) { box.appendChild(el("div", "note warn small", e.message)); })
        .finally(function () { btn.disabled = false; });
    });
  });

  /* ---------- a question in the student's language (1.22.0) ---------- */
  document.querySelectorAll("[data-translate]").forEach(function (btn) {
    var card = btn.closest(".qcard"), title = card.querySelector(".qtitle"), opts = card.querySelectorAll("label.choice");
    var english = null, mine = null, label = btn.textContent;
    function texts() { return { prompt: title.textContent, options: Array.prototype.map.call(opts, function (l) { return l.lastChild.textContent; }) }; }
    function show(t) { title.textContent = t.prompt; opts.forEach(function (l, i) { if (t.options[i] != null) l.lastChild.textContent = t.options[i]; }); }
    btn.addEventListener("click", function () {
      if (mine && title.textContent === mine.prompt) { show(english); btn.textContent = label; return; }
      if (mine) { show(mine); btn.textContent = btn.getAttribute("data-orig"); return; }
      english = texts(); btn.disabled = true;
      post(btn.getAttribute("data-translate"), {}).then(function (d) {
        mine = { prompt: d.prompt, options: d.options }; show(mine); btn.textContent = btn.getAttribute("data-orig");
      }).catch(function (e) { btn.textContent = e.message; }).finally(function () { btn.disabled = false; });
    });
  });

  /* ---------- chat (ask the tutor, Teach NOVA) ---------- */
  function addMsg(list, who, text, src) {
    var wrap = el("div", "msg " + who); wrap.appendChild(el("span", null, text));
    if (who === "them" && text !== "…" && window.nqWireSay) {
      var b = el("button", "say-btn", "🔊"); b.type = "button"; b.setAttribute("data-say", ""); b.setAttribute("aria-pressed", "false");
      b.setAttribute("aria-label", document.body.getAttribute("data-say-label") || "Read aloud"); wrap.appendChild(b); window.nqWireSay(b);
    }
    if (src && src.length) { wrap.appendChild(el("div")); wrap.lastChild.appendChild(el("span", "src", "📖 " + src.join(" · "))); }
    list.appendChild(wrap); wrap.scrollIntoView({ block: "nearest" }); return wrap;
  }
  document.querySelectorAll("form[data-chat]").forEach(function (f) {
    var list = document.getElementById(f.getAttribute("data-list")), input = f.querySelector("input[name=message]");
    var mode = f.getAttribute("data-mode");
    f.addEventListener("submit", function (e) {
      e.preventDefault(); var text = input.value.trim(); if (!text) { input.focus(); return; }
      addMsg(list, "me", text); input.value = ""; var waiting = addMsg(list, "them", "…");
      var body = mode === "ask" ? { question: text, topic_id: Number(f.getAttribute("data-topic")) || null } : { message: text };
      post(f.getAttribute("data-chat"), body).then(function (d) {
        list.removeChild(waiting);
        if (mode === "ask") { addMsg(list, "them", d.text, d.citations); document.dispatchEvent(new CustomEvent("nq:reply", { detail: { text: d.text } })); return; }
        if (d.verdict === "correct") {
          list.appendChild(el("div", "status-chip ok", "✓ NOVA changed its mind"));
          var talking = document.querySelector('[data-talk][aria-pressed="true"]');  // talking with NOVA: keep the conversation going
          if (!d.ended && !talking) window.setTimeout(function () { window.location.reload(); }, 2200);  // refresh the progress header
        }
        if (d.verdict === "incorrect") list.appendChild(el("div", "status-chip warn", "NOVA is confused. Check your idea!"));
        addMsg(list, "them", d.reply);
        document.dispatchEvent(new CustomEvent("nq:reply", { detail: { text: d.reply, ended: d.ended } }));
        if (d.ended) {
          var done = f.getAttribute("data-done");
          window.setTimeout(function () { if (done) window.location.href = done; else window.location.reload(); }, 1800);
        }
      }).catch(function (err) { waiting.textContent = err.message; });
    });
  });
  document.querySelectorAll("[data-starter]").forEach(function (chip) {
    chip.addEventListener("click", function () {
      var input = document.getElementById(chip.getAttribute("data-starter")); input.value = chip.textContent.replace("…", ""); input.focus();
    });
  });

  /* ---------- voice input (TA-10) ---------- */
  var Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  document.querySelectorAll("[data-voice]").forEach(function (btn) {
    if (!Rec) { btn.hidden = true; return; }
    btn.addEventListener("click", function () {
      var input = document.getElementById(btn.getAttribute("data-voice")), rec = new Rec();
      rec.lang = document.documentElement.lang || "en-IN"; rec.interimResults = false; btn.classList.add("listening"); btn.setAttribute("aria-pressed", "true");
      rec.onresult = function (ev) { input.value = (input.value ? input.value + " " : "") + ev.results[0][0].transcript; input.dispatchEvent(new Event("input", { bubbles: true })); };
      rec.onend = function () { btn.classList.remove("listening"); btn.setAttribute("aria-pressed", "false"); };
      rec.start();
    });
  });

  /* ---------- lesson quick checks (LN-1) ---------- */
  document.querySelectorAll("form[data-check]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      e.preventDefault(); var picked = f.querySelector("input:checked"), out = f.querySelector(".result");
      if (!picked) { out.textContent = "Choose an answer first."; return; }
      post(f.getAttribute("data-check"), { index: Number(f.getAttribute("data-index")), choice: Number(picked.value) }).then(function (d) {
        out.innerHTML = ""; f.querySelectorAll(".choice").forEach(function (c) { c.classList.remove("right", "wrong"); });
        picked.closest(".choice").classList.add(d.correct ? "right" : "wrong");
        out.appendChild(el("p", d.correct ? "note ok" : "note warn", (d.correct ? "Yes! " : "Not quite. ") + (d.why || "")));
        if (d.explain_again) out.appendChild(el("p", "note info", "Another way to think about it: " + d.explain_again));
      }).catch(function (err) { out.textContent = err.message; });
    });
  });

  /* ---------- confirmations and copy ---------- */
  document.addEventListener("submit", function (e) {
    var by = e.submitter && e.submitter.getAttribute("data-confirm");
    var msg = by || (e.target.getAttribute && e.target.getAttribute("data-confirm"));
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    var busy = e.target.querySelector && e.target.querySelector("[data-busy]");
    if (busy) {  // long uploads: show that something is happening and stop a second click
      busy.textContent = busy.getAttribute("data-busy");
      setTimeout(function () { busy.disabled = true; }, 0);
    }
  }, true);
  document.querySelectorAll("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var t = document.getElementById(b.getAttribute("data-copy"));
      if (navigator.clipboard) navigator.clipboard.writeText(t.value || t.textContent).then(function () { b.textContent = "Copied"; });
    });
  });

  /* ---------- learn in pictures (LN-10) ---------- */
  document.querySelectorAll("figure[data-scene]").forEach(function (fig) {
    function light(id, on, cls) {
      fig.querySelectorAll('[data-mark="' + id + '"]').forEach(function (n) { n.classList.toggle(cls || "on", on); });
    }
    fig.querySelectorAll("button.lg").forEach(function (b) {
      var id = b.getAttribute("data-mark");
      b.addEventListener("mouseenter", function () { light(id, true, "hov"); });
      b.addEventListener("mouseleave", function () { light(id, false, "hov"); });
      b.addEventListener("click", function () {
        if (fig.classList.contains("hidden-names")) b.classList.toggle("shown");
        var was = b.classList.contains("on");
        fig.querySelectorAll(".on").forEach(function (n) { n.classList.remove("on"); });
        light(id, !was);
      });
    });
    var hide = fig.querySelector("[data-hide-names]");
    if (hide) hide.addEventListener("click", function () {
      var on = !fig.classList.contains("hidden-names");
      fig.classList.toggle("hidden-names", on);
      fig.querySelectorAll("button.lg").forEach(function (b) { b.classList.remove("shown"); });
      hide.setAttribute("aria-pressed", on ? "true" : "false");
      hide.textContent = on ? "Show all the names" : "Test yourself: hide the names";
    });
  });

  var deck = document.querySelector("[data-deck]");
  if (deck) {
    var cards = Array.prototype.slice.call(deck.querySelectorAll("[data-card]"));
    var nav = deck.querySelector("[data-nav]"), prev = deck.querySelector("[data-prev]"), next = deck.querySelector("[data-next]");
    var bar = deck.querySelector("[data-progress]"), at = 0;
    var key = "nq-deck-" + location.pathname;
    function show(i, focus) {
      at = Math.max(0, Math.min(cards.length - 1, i));
      cards.forEach(function (c, j) { c.hidden = j !== at; });
      prev.disabled = at === 0;
      next.hidden = at === cards.length - 1;
      bar.querySelector(".fill").style.width = Math.round(100 * (at + 1) / cards.length) + "%";
      bar.querySelector(".count").textContent = "Card " + (at + 1) + " of " + cards.length;
      try { sessionStorage.setItem(key, String(at)); } catch (e) {}
      if (focus) { var h = cards[at].querySelector("h2"); h.setAttribute("tabindex", "-1"); h.focus({ preventScroll: true }); deck.scrollIntoView({ block: "start" }); }
    }
    nav.hidden = false; bar.hidden = false;
    var saved = 0;
    try { saved = Number(sessionStorage.getItem(key) || 0); } catch (e) {}
    show(saved || 0, false);
    prev.addEventListener("click", function () { show(at - 1, true); });
    next.addEventListener("click", function () { show(at + 1, true); });
    document.addEventListener("keydown", function (e) {
      if (/INPUT|TEXTAREA|SELECT/.test((e.target.tagName || ""))) return;
      if (e.key === "ArrowRight") show(at + 1, true);
      if (e.key === "ArrowLeft") show(at - 1, true);
    });
    var x0 = null;
    deck.addEventListener("touchstart", function (e) { if (!e.target.closest("button, input, label, .compare")) x0 = e.touches[0].clientX; }, { passive: true });
    deck.addEventListener("touchend", function (e) {
      if (x0 === null) return;
      var dx = e.changedTouches[0].clientX - x0; x0 = null;
      if (Math.abs(dx) > 60) show(at + (dx < 0 ? 1 : -1), true);
    }, { passive: true });
    var finish = deck.querySelector("[data-finish]"), url = deck.getAttribute("data-finish-url");
    if (finish && url) finish.addEventListener("click", function () {
      finish.disabled = true;
      post(url, {}).then(function () {
        try { sessionStorage.removeItem(key); } catch (e) {}
        var then = finish.getAttribute("data-then");
        if (then) { window.location.href = then; return; }
        finish.textContent = "Finished ✓";
        deck.querySelector("[data-finish-msg]").hidden = false;
      }).catch(function (err) { finish.disabled = false; finish.textContent = err.message; });
    });
  }

  /* ---------- one question per screen (UX-1) ---------- */
  document.querySelectorAll("form[data-steps]").forEach(function (form) {
    var steps = Array.prototype.slice.call(form.querySelectorAll("[data-step]"));
    var nav = form.querySelector("[data-nav]"), prev = form.querySelector("[data-prev]"), next = form.querySelector("[data-next]");
    var bar = form.querySelector("[data-progress]"), n = steps.length - 1, at = 0, skey = "nq-step-" + location.pathname;
    if (!nav || steps.length < 2) return;
    function answered(sec) {
      if (sec.querySelector("input[type=radio]:checked")) return true;
      var t = sec.querySelector("textarea, input[data-answer]"); return !!(t && t.value.trim());
    }
    function show(i, focus) {
      at = Math.max(0, Math.min(steps.length - 1, i));
      steps.forEach(function (s, j) { s.hidden = j !== at; });
      var review = at === n;
      prev.disabled = at === 0;
      next.hidden = review;
      next.textContent = at === n - 1 ? "Review answers" : "Next";
      bar.querySelector(".fill").style.width = Math.round(100 * (at + 1) / steps.length) + "%";
      bar.querySelector(".count").textContent = review ? "Review" : "Question " + (at + 1) + " of " + n;
      if (review) form.querySelectorAll("[data-answered]").forEach(function (m) {
        var ok = answered(steps[Number(m.getAttribute("data-answered"))]);
        m.textContent = ok ? "✓ Answered" : "Not answered yet"; m.className = "small " + (ok ? "ok-text" : "warn-text");
      });
      try { sessionStorage.setItem(skey, String(at)); } catch (e) {}
      if (focus) { var h = steps[at].querySelector("h2"); if (h) { h.setAttribute("tabindex", "-1"); h.focus({ preventScroll: true }); } form.scrollIntoView({ block: "start" }); }
    }
    nav.hidden = false; bar.hidden = false;
    var saved = 0; try { saved = Number(sessionStorage.getItem(skey) || 0); } catch (e) {}
    show(saved, false);
    prev.addEventListener("click", function () { show(at - 1, true); });
    next.addEventListener("click", function () { show(at + 1, true); });
    form.querySelectorAll("[data-goto]").forEach(function (b) { b.addEventListener("click", function () { show(Number(b.getAttribute("data-goto")), true); }); });
    form.addEventListener("submit", function () { try { sessionStorage.removeItem(skey); } catch (e) {} });
  });

  /* ---------- topic picker: search and count (UX-1) ---------- */
  var tsearch = document.querySelector("[data-topic-search]"), ttree = document.querySelector("[data-topic-tree]");
  if (tsearch && ttree) {
    var counter = document.querySelector("[data-topic-count]");
    var recount = function () { if (counter) counter.textContent = ttree.querySelectorAll("input[name='topics[]']:checked").length + " chosen"; };
    ttree.addEventListener("change", recount);
    tsearch.addEventListener("input", function () {
      var q = tsearch.value.trim().toLowerCase();
      ttree.querySelectorAll("details").forEach(function (d) {
        var any = false;
        d.querySelectorAll(".check-row").forEach(function (row) {
          var hit = !q || row.textContent.toLowerCase().indexOf(q) >= 0 || d.querySelector("summary").textContent.toLowerCase().indexOf(q) >= 0;
          row.hidden = !hit; any = any || hit;
        });
        d.hidden = !any;
        if (q) d.open = any;
      });
    });
  }

  /* ---------- tick a whole chapter (AS-8) ---------- */
  document.querySelectorAll("[data-check-all]").forEach(function (all) {
    var box = all.closest("details"), rows = box.querySelectorAll("input[name='topics[]']");
    var sync = function () { var n = box.querySelectorAll("input[name='topics[]']:checked").length; all.checked = n === rows.length; all.indeterminate = n > 0 && n < rows.length; };
    all.addEventListener("change", function () { rows.forEach(function (r) { r.checked = all.checked; }); });
    rows.forEach(function (r) { r.addEventListener("change", sync); });
    sync();
  });

  /* ---------- exam pattern: running total (AS-8) ---------- */
  var pat = document.querySelector("[data-exam-pattern]"), patTotal = document.querySelector("[data-exam-total]");
  if (pat && patTotal) {
    var retotal = function () {
      var marks = 0, qs = 0;
      pat.querySelectorAll("tbody tr").forEach(function (tr) {
        var c = Number(tr.querySelector("input").value) || 0, m = Number(tr.querySelector("select").value) || 0;
        if (c > 0 && m > 0) { marks += c * m; qs += c; }
      });
      patTotal.textContent = "Total: " + qs + " questions, " + marks + " marks";
    };
    pat.addEventListener("input", retotal); pat.addEventListener("change", retotal); retotal();
  }

  /* ---------- Ask NOVA sheet (UX-1) ---------- */
  var sheet = document.getElementById("ask-sheet");
  if (sheet && sheet.showModal) {
    document.querySelectorAll("[data-ask-open]").forEach(function (b) {
      b.addEventListener("click", function () { sheet.showModal(); var i = sheet.querySelector("input[name=message]"); if (i) i.focus(); });
    });
    sheet.querySelectorAll("[data-ask-close]").forEach(function (b) { b.addEventListener("click", function () { sheet.close(); }); });
    sheet.addEventListener("click", function (e) { if (e.target === sheet) sheet.close(); });  // tap outside closes
  }

  /* ---------- account menu and sign out ---------- */
  var account = document.querySelector("[data-account]");
  if (account) {
    document.addEventListener("click", function (e) { if (account.open && !account.contains(e.target)) account.open = false; });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && account.open) { account.open = false; account.querySelector("summary").focus(); } });
  }
  function queueKeys() {
    var keys = [];
    try { for (var i = 0; i < localStorage.length; i++) { var k = localStorage.key(i); if (k && k.indexOf("nq-queue-") === 0) keys.push(k); } } catch (e) {}
    return keys;
  }
  document.querySelectorAll("[data-signout]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      var pending = queueKeys().reduce(function (n, k) { return n + Object.keys(store(k) || {}).length; }, 0);
      var warning = form.querySelector("[data-signout-warning]");
      if (pending && warning && warning.hidden) {
        // Answers typed offline are still on this phone: say so before they are thrown away.
        e.preventDefault();
        warning.textContent = pending + (pending === 1 ? " answer hasn't" : " answers haven't") +
          " reached NovaQuest yet. Connect to the Wi-Fi and wait a moment, or tap Sign out again to discard " + (pending === 1 ? "it." : "them.");
        warning.hidden = false;
        return;
      }
      // The next person on a shared phone must not inherit this student's unsent answers.
      queueKeys().forEach(function (k) { try { localStorage.removeItem(k); } catch (err) {} });
    });
  });

  /* ---------- print buttons (student page for parent meetings) ---------- */
  document.querySelectorAll("[data-print]").forEach(function (b) {
    b.addEventListener("click", function () { window.print(); });
  });

  /* ---------- projector view: one question per screen (teacher review, item 11) ---------- */
  document.querySelectorAll("[data-slides]").forEach(function (deck) {
    var slides = Array.prototype.slice.call(deck.querySelectorAll("[data-slide]")), at = 0;
    var count = deck.querySelector("[data-slide-count]"), reveal = deck.querySelector("[data-slide-reveal]");
    var prev = deck.querySelector("[data-slide-prev]"), next = deck.querySelector("[data-slide-next]");
    function show(i) {
      at = Math.max(0, Math.min(slides.length - 1, i));
      slides.forEach(function (s, j) { s.hidden = j !== at; });
      var ans = slides[at].querySelector("[data-answer]");
      reveal.hidden = !ans; reveal.textContent = ans && !ans.hidden ? "Hide answer" : "Show answer";
      count.textContent = (at + 1) + " / " + slides.length;
      prev.disabled = at === 0; next.disabled = at === slides.length - 1;
      try { history.replaceState(null, "", "#" + (at + 1)); } catch (e) {}
    }
    function toggle() {
      var ans = slides[at].querySelector("[data-answer]");
      if (!ans) return;
      ans.hidden = !ans.hidden; slides[at].classList.toggle("revealed", !ans.hidden);
      if (!ans.hidden && ans.scrollIntoView) ans.scrollIntoView({ block: "nearest" });  // never left under the control bar
      reveal.textContent = ans.hidden ? "Show answer" : "Hide answer";
    }
    prev.addEventListener("click", function () { show(at - 1); });
    next.addEventListener("click", function () { show(at + 1); });
    reveal.addEventListener("click", toggle);
    document.addEventListener("keydown", function (e) {
      if (e.target.closest("input, textarea, select")) return;
      if (e.key === "ArrowRight" || e.key === "PageDown" || e.key === " ") { e.preventDefault(); show(at + 1); }
      else if (e.key === "ArrowLeft" || e.key === "PageUp") { e.preventDefault(); show(at - 1); }
      else if (e.key === "a" || e.key === "A" || e.key === "Enter") { if (!e.target.closest("button, a")) { e.preventDefault(); toggle(); } }
    });
    var fs = deck.querySelector("[data-fullscreen]");
    if (fs) {
      if (!document.documentElement.requestFullscreen) fs.hidden = true;
      fs.addEventListener("click", function () {
        if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(function () {});
      });
    }
    show((parseInt(location.hash.slice(1), 10) || 1) - 1);
  });

  /* ---------- timed practice paper (1.31.0): count down and hand in at zero ---------- */
  document.querySelectorAll("form[data-paper]").forEach(function (form) {
    var left = parseInt(form.getAttribute("data-seconds"), 10) || 0, out = form.querySelector("[data-timer]"), sent = false;
    function tick() {
      if (out) out.textContent = Math.floor(left / 60) + ":" + ("0" + (left % 60)).slice(-2);
      if (left <= 0 && !sent) { sent = true; form.submit(); return; }
      left -= 1;
    }
    tick();
    setInterval(tick, 1000);
    form.addEventListener("submit", function () { sent = true; });
  });

  /* ---------- filters that apply as soon as they change (1.30.0): no "Show" button needed ---------- */
  document.querySelectorAll("form[data-autoapply]").forEach(function (form) {
    form.querySelectorAll("[data-apply]").forEach(function (b) { b.hidden = true; });
    form.querySelectorAll("select").forEach(function (sel) {
      sel.addEventListener("change", function () { form.submit(); });
    });
  });

  /* ---------- pickers that reload the page for another class/subject (SY-6) ---------- */
  document.querySelectorAll("select[data-reload-param]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      var u = new URL(location.href);
      u.search = "";
      u.searchParams.set(sel.getAttribute("data-reload-param"), sel.value);
      location.href = u.toString();
    });
  });

  if ("serviceWorker" in navigator && location.protocol === "https:") navigator.serviceWorker.register("/sw.js").catch(function () {});
})();

// Account menus (top right): close when clicking elsewhere or pressing Escape.
document.addEventListener("click", function (e) {
  document.querySelectorAll("details[data-menu][open]").forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute("open"); });
});
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") document.querySelectorAll("details[data-menu][open]").forEach(function (d) { d.removeAttribute("open"); });
});

// Quick starters for a teacher's note: tap to add the words to the box (it stays fully editable).
document.addEventListener("click", function (e) {
  var b = e.target.closest ? e.target.closest("[data-insert]") : null;
  if (!b) return;
  var box = document.getElementById(b.getAttribute("data-insert"));
  if (!box) return;
  var add = b.getAttribute("data-text") || "";
  box.value = (box.value && !/\s$/.test(box.value) ? box.value + " " : box.value) + add;
  box.focus();
});

// "Add your own question": show only the fields for the chosen type.
document.querySelectorAll("form[data-own]").forEach(function (f) {
  var kind = f.querySelector("[data-own-kind]");
  function show() { f.querySelectorAll("[data-own-for]").forEach(function (el) { el.hidden = el.getAttribute("data-own-for") !== kind.value; }); }
  kind.addEventListener("change", show); show();
});

// Maths symbols for a number answer: put the symbol where the cursor is (1.20.0).
document.addEventListener("click", function (e) {
  var b = e.target.closest ? e.target.closest("[data-sym]") : null;
  if (!b) return;
  var box = document.getElementById(b.getAttribute("data-for"));
  if (!box) return;
  var sym = b.getAttribute("data-sym"), at = box.selectionStart != null ? box.selectionStart : box.value.length;
  var end = box.selectionEnd != null ? box.selectionEnd : at;
  box.value = box.value.slice(0, at) + sym + box.value.slice(end);
  box.focus();
  try { box.setSelectionRange(at + sym.length, at + sym.length); } catch (err) { /* some inputs can't */ }
  box.dispatchEvent(new Event("input", { bubbles: true }));
});

/* ---------- voice: read aloud and talk with NOVA (1.23.0) ----------
   Uses the phone's or browser's own speech (no server, no cost, nothing recorded by NovaQuest), in the page's language
   (en-IN, hi-IN, ta-IN…). Buttons stay hidden where the browser can't speak or listen. */
(function () {
  "use strict";
  var synth = window.speechSynthesis, Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  var lang = document.documentElement.lang || "en-IN", speaking = null;

  function voiceFor(code) {
    var all = synth ? synth.getVoices() : [], base = code.split("-")[0];
    return all.filter(function (v) { return v.lang === code; })[0] || all.filter(function (v) { return (v.lang || "").split("-")[0] === base; })[0] || null;
  }
  function clean(text) { return (text || "").replace(/🔊/g, "").replace(/^\s*NOVA:\s*/, "").replace(/📖.*$/, "").replace(/\s+/g, " ").trim(); }
  function say(text, btn, then) {
    if (!synth) { if (then) then(); return; }
    synth.cancel();
    if (speaking) { speaking.setAttribute("aria-pressed", "false"); speaking.classList.remove("speaking"); }
    if (btn && speaking === btn) { speaking = null; return; }  // second tap stops
    var u = new SpeechSynthesisUtterance(clean(text)), v = voiceFor(lang);
    u.lang = lang; u.rate = 0.95; if (v) u.voice = v;
    speaking = btn || null;
    if (btn) { btn.setAttribute("aria-pressed", "true"); btn.classList.add("speaking"); }
    u.onend = u.onerror = function () {
      if (btn) { btn.setAttribute("aria-pressed", "false"); btn.classList.remove("speaking"); }
      if (speaking === btn) speaking = null;
      if (then) then();
    };
    synth.speak(u);
  }
  window.nqSay = say;

  // 🔊 buttons: read the text of the element they point at (a question, NOVA's message, a lesson card)
  function wire(btn) {
    if (!synth) { btn.hidden = true; return; }
    btn.hidden = false;
    btn.addEventListener("click", function () {
      var target = btn.getAttribute("data-say") ? document.getElementById(btn.getAttribute("data-say")) : btn.parentNode;
      var extra = btn.getAttribute("data-say-options") ? Array.prototype.map.call(
        document.querySelectorAll(btn.getAttribute("data-say-options")), function (o, i) { return "ABCDEFGH"[i] + ". " + o.textContent; }) : [];
      say([target ? target.textContent : ""].concat(extra).join(". "), btn);
    });
  }
  document.querySelectorAll("[data-say]").forEach(wire);
  window.nqWireSay = wire;

  // Talk with NOVA: NOVA's replies are read aloud, then the microphone listens and sends what the student says.
  var talk = document.querySelector("[data-talk]");
  if (talk) {
    var form = document.querySelector(talk.getAttribute("data-talk")), input = form && form.querySelector("input[name=message]");
    if (!synth || !Rec || !form) { talk.hidden = true; return; }
    talk.hidden = false;
    var on = false, rec = null;
    function listen() {
      if (!on) return;
      rec = new Rec(); rec.lang = lang; rec.interimResults = false;
      talk.classList.add("listening");
      rec.onresult = function (ev) {
        var said = ev.results[0][0].transcript;
        if (said && said.trim()) { input.value = said; form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit", { cancelable: true })); }
      };
      rec.onend = function () { talk.classList.remove("listening"); };
      rec.start();
    }
    function set(v) {
      on = v; talk.setAttribute("aria-pressed", v ? "true" : "false");
      try { localStorage.setItem("nq-talk", v ? "1" : "0"); } catch (e) { /* private mode */ }
      if (!v) { if (rec) rec.abort(); if (synth) synth.cancel(); return; }
      var last = document.querySelectorAll(talk.getAttribute("data-talk-list") + " .msg.them");
      say(last.length ? last[last.length - 1].textContent : "", null, listen);
    }
    talk.addEventListener("click", function () { set(!on); });
    document.addEventListener("nq:reply", function (e) { if (on && e.detail && e.detail.text) say(e.detail.text, null, e.detail.ended ? null : listen); });
  }
})();
