"""Chapter presentations (1.27.0): a teacher teaches a chapter from the projector, inside NovaQuest.

The slides follow the chapter's prep pack (services.prep), one period at a time: the hook question, the key points
one per slide, the school's approved picture-lesson cards, the textbook's own pages (where the book allows showing
them), the book's activities with their safety notes, a check question whose answer is revealed on a click, the
mix-ups as 'True or false?' slides, and a wrap-up back to the opening question. Each slide has speaker notes for the
presenter view (what to say, what to listen for, the mix-ups) and belongs to a step of the period's timed flow.

The deck is shown only in the app, to signed-in staff: there is no file to download (no PDF, PowerPoint or image
export), pages ask the browser not to keep a copy and not to print, and every slide carries a faint watermark with
the school, the teacher and the date. Nothing on a screen can be protected from a photo or a screenshot; the
watermark shows where such a copy came from. Each time a deck is opened it is recorded in the audit log."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from novaquest.core import audit
from novaquest.core.db import Tx
from novaquest.services import figures, pages, pictures, prep, syllabus
from novaquest.services.access import Actor, require

MAX_PAGE_SLIDES = 4  # textbook pages shown per topic


def _step(period: dict[str, Any], i: int) -> dict[str, Any]:
    """The period's i-th flow step as {'name', 'minutes'} (the last one if there are fewer)."""
    flow = period["flow"]
    m, name = flow[min(i, len(flow) - 1)]
    return {"name": name, "minutes": m}


_WORDS = re.compile(r"[a-z]{4,}")
_COMMON = {"with", "from", "that", "this", "their", "they", "have", "which", "showing", "shown", "using", "some", "made"}


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in _WORDS.findall(text.lower())} - _COMMON


def _place_figures(points: list[str], figs: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """Each figure goes after the key point its caption shares most words with (key 0: after all the points)."""
    out: dict[int, list[dict[str, Any]]] = {}
    for f in figs:
        cap = _words(f["caption"])
        best = max(range(len(points)), key=lambda i: len(cap & _words(points[i])), default=None)
        k = best + 1 if best is not None and cap and len(cap & _words(points[best])) >= 1 else 0
        out.setdefault(k, []).append(f)
    return out


def _pic_words(pic: Any) -> set[str]:
    """Every word written in a diagram (headings, labels, cells, steps)."""
    if isinstance(pic, dict):
        return set().union(*[_pic_words(v) for k, v in pic.items() if k not in ("kind", "icon", "color")]) if pic else set()
    if isinstance(pic, list):
        return set().union(*[_pic_words(v) for v in pic]) if pic else set()
    return _words(str(pic)) if isinstance(pic, str) else set()


def place_diagrams(points: list[str], diagrams: list[dict[str, Any]]) -> tuple[dict[int, dict], list[dict]]:
    """Put each diagram beside the key point it's about, judged by what the diagram says (words the point shares with
    it, leaving out words most of the topic's points use, like 'cell'), not by the number Claude gave, which can be
    off by one. A diagram that matches no point, or whose point already has one, gets a slide of its own.
    Returns ({point index: picture}, [pictures on their own slides])."""
    sets = [_words(p) for p in points]
    common = {w for w in set().union(*sets) if sum(w in s for s in sets) > max(1, len(sets) // 2)} if sets else set()
    beside: dict[int, dict] = {}
    alone: list[dict] = []
    for d in diagrams:
        words = _pic_words(d["pic"]) - common
        scores = [len(words & (s - common)) for s in sets]
        best = max(range(len(scores)), key=lambda i: scores[i], default=None)
        if best is not None and scores[best] >= 2 and best not in beside:
            beside[best] = d["pic"]
        else:
            alone.append(d["pic"])
    return beside, alone


def _topic_slides(tx: Tx, t: dict[str, Any], p: dict[str, Any], period: dict[str, Any], acts: dict[str, dict],
                  pics: dict[int, dict], page_ok: bool, data_dir: Path | None = None) -> list[dict[str, Any]]:
    watch = [m for m in p["mixups"] if m["topic"] == t["code"]] or [m for m in p["mixups"] if m["label"] in t["watch_for"]]
    mix_note = " ".join(f"Watch for “{m['label']}”: {m['right']}" for m in watch[:2])
    question = prep._ask(t["title"]) or f"What do you already know about {prep._sentence_case(t['title'])}?"
    out = [{"kind": "hook", "title": t["title"], "code": t["code"], "text": question, "step": _step(period, 0),
            "notes": f"{t['hook']} Objective: {t['objective']} Write their answers on the board; come back to them at the end."}]
    figs = figures.topic_figures(tx, t["id"], data_dir, read=False) if data_dir else []  # pages already read (background)
    placed = _place_figures(t["key_points"], figs)
    beside, alone = place_diagrams(t["key_points"], t.get("diagrams") or [])

    def figure_slides(fs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"kind": "figure", "title": t["title"], "topic_id": t["id"], "page": f["page"], "fig": f["n"],
                 "caption": f["caption"], "step": _step(period, 1),
                 "notes": "From the textbook" + (f" ({f['caption'].split(':')[0]})" if f["caption"] else f", page {f['page']}")
                 + ". Point to each part and ask the class what they notice before you explain."} for f in fs]

    for k, point in enumerate(t["key_points"], 1):
        pic = beside.get(k - 1)
        out.append({"kind": "point", "title": t["title"], "text": point, "n": k, "of": len(t["key_points"]), "pic": pic,
                    "step": _step(period, 1),
                    "notes": (f"Key point {k} of {len(t['key_points'])}. Ask a student to say it in their own words. " + mix_note).strip()})
        out += figure_slides(placed.get(k, []))
    for pic in alone:
        if pic:
            out.append({"kind": "diagram", "title": t["title"], "pic": pic, "step": _step(period, 1),
                        "notes": "Walk through the diagram from left to right; ask a student to explain one part."})
    out += figure_slides(placed.get(0, []))
    lesson = pics.get(t["id"])
    for c in (lesson or {}).get("cards", [])[:6]:
        out.append({"kind": "card", "title": c.get("title", ""), "card": c, "step": _step(period, 1),
                    "notes": "From the picture lesson your students also have on their phones. " + (c.get("remember") or "")})
    if page_ok and not figs:  # no figures found on the topic's pages: show the pages themselves
        for n in pages.topic_pages(tx, t["id"])[:MAX_PAGE_SLIDES]:
            out.append({"kind": "page", "title": t["title"], "topic_id": t["id"], "page": n, "step": _step(period, 1),
                        "notes": f"Textbook page {n}. Point to the figures as you talk."})
    act = acts.get(str(t.get("activity") or ""))
    if act:
        out.append({"kind": "activity", "title": act["title"], "act": act, "step": _step(period, 2),
                    "notes": "Ask the class to predict before you start. " + (f"Safety: {act['safety']}" if act.get("safety") else "")})
    elif t.get("activity"):
        out.append({"kind": "discuss", "title": "Let's discuss", "text": t["activity"], "step": _step(period, 2),
                    "notes": "Give pairs two minutes, then take answers."})
    c = t.get("check")
    if c:
        out.append({"kind": "check", "title": "Quick check", "check": c, "step": _step(period, 3),
                    "notes": ("Ask for a show of hands for each option before revealing. " + (c.get("why") or "")) if c["options"]
                    else "Ask three students. Listen for: " + " ".join(c.get("listen") or [])})
    out.append({"kind": "wrap", "title": t["title"], "points": t["key_points"][:3], "step": _step(period, 4),
                "notes": "Go back to the answers on the board from the start. Which were right? What would they change now?"})
    return out


def slides(tx: Tx, chapter_id: int, data: dict[str, Any], period: dict[str, Any],
           data_dir: Path | None = None) -> list[dict[str, Any]]:
    """The slides for one period of the chapter's lesson plan."""
    ch = prep._chapter(tx, chapter_id)
    book = syllabus.get_syllabus(tx, ch["syllabus_id"])
    page_ok = pages.allowed(book)
    acts = {str(a["ref"]): a for a in data["activities"] if a.get("ref")}
    ids = [t["id"] for t in data["topics"]]
    nodes = tx.all("SELECT id, code, syllabus_id FROM syllabus_nodes WHERE id = ANY(%s)", [ids]) if ids else []
    pics = pictures.lessons_for(tx, nodes)  # only what's approved for students
    part = f"Part {ch['part']} · " if ch.get("part") else ""
    out = [{"kind": "cover", "title": ch["title"], "kicker": f"{part}Chapter {ch['code']}", "sub": f"Period {period['no']}: {period['title']}",
            "step": _step(period, 0), "notes": data["overview"]}]
    if period["kind"] in ("teach", "practice"):
        for t in period["topics"]:
            if period["kind"] == "practice":
                out.append({"kind": "wrap", "title": t["title"], "points": t["key_points"][:4], "step": _step(period, 1),
                            "notes": "Recap: ask students to explain each point to a partner."})
                if t.get("check"):
                    out.append({"kind": "check", "title": "Your turn", "check": t["check"], "step": _step(period, 3),
                                "notes": "Let students answer in their notebooks first."})
            else:
                out += _topic_slides(tx, t, data, period, acts, pics, page_ok, data_dir)
    elif period["kind"] == "activity":
        for a in period.get("activities", []):
            out.append({"kind": "activity", "title": a["title"], "act": a, "step": _step(period, 1),
                        "notes": "Ask the class to predict before you start. " + (f"Safety: {a['safety']}" if a.get("safety") else "")})
    elif period["kind"] == "review":
        for m in data["mixups"][:6]:
            if m.get("wrong"):
                out.append({"kind": "mixup", "title": "True or false?", "mix": m, "step": _step(period, 1),
                            "notes": f"Vote first, then reveal. How you'll notice it: {m.get('spot') or '—'} Clear it up: {m.get('fix') or '—'}"})
        terms = [x for x in data["terms"] if x["term"]][:12]
        if terms:
            out.append({"kind": "terms", "title": "Key words", "terms": terms, "step": _step(period, 1),
                        "notes": "Point to a word; a student explains it."})
        for t in data["topics"]:
            if t.get("check") and t["check"]["options"]:
                out.append({"kind": "check", "title": "Quiz", "check": t["check"], "step": _step(period, 2),
                            "notes": "Show of hands for each option, then reveal."})
    out.append({"kind": "end", "title": "Well done!", "sub": f"{part}Chapter {ch['code']}: {ch['title']}", "step": _step(period, len(period["flow"]) - 1),
                "notes": "Set the homework or the opening quiz for the next topic."})
    for i, sl in enumerate(out, 1):
        sl["no"] = i
        sl["label"] = _label(sl)
    return out


def _label(sl: dict[str, Any]) -> str:
    """A slide in a few words, for the presenter's 'Next' line."""
    k = sl["kind"]
    text = {"point": sl.get("text"), "hook": sl.get("text"), "discuss": sl.get("text"), "page": f"Textbook page {sl.get('page')}",
            "figure": "Figure: " + (sl.get("caption") or f"textbook page {sl.get('page')}"), "diagram": "Diagram: " + sl.get("title", ""),
            "activity": "Activity: " + sl.get("title", ""), "check": f"{sl['title']}: " + sl["check"]["q"] if k == "check" else None,
            "mixup": "True or false? " + (sl.get("mix") or {}).get("wrong", ""), "wrap": "What we learnt: " + sl.get("title", "")}.get(k)
    text = text or sl.get("title", "")
    return text if len(text) <= 90 else text[:87].rsplit(" ", 1)[0] + "…"


def record(tx: Tx, actor: Actor, chapter_id: int, period_no: int) -> None:
    """Who presented which chapter and period, and when (the deck isn't a file, so this is its only trace)."""
    require(actor, "teacher", "leader", "inst_admin")
    audit.record(tx, "present.open", actor_id=actor.user_id, target=f"chapter:{chapter_id}", detail={"period": period_no})
