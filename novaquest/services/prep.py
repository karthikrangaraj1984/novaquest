"""Unit prep packs (1.26.0): help a teacher prepare to teach one chapter.

A pack has, for each topic of the chapter, what to teach (objective, an opening hook, 3-5 key points, textbook pages,
the book's activity for it, a quick check question and the mix-ups to watch for), plus the chapter's mix-ups with how
to spot and fix them, the book's activities written up as classroom demos (materials, steps, questions to ask, safety)
and the key terms. The page arranges the topics into the periods the teacher has (from the term plan, or chosen),
each period with a timed flow, and offers a one-click opening quiz and a printable PDF.

With Claude, the pack is written once per school and chapter (in the background) and kept; Claude is told to use only
the book and to suggest only the book's own activities or safe demos with everyday materials. Without Claude (or
until Claude's pack is ready) the pack is built from the book itself: the topic outcomes, approved lessons, the
book's 'Activity 2.1: Let us …' boxes and the mix-ups already drafted for the book. Nothing is stored for that."""
from __future__ import annotations

import logging
import random
import re
from datetime import timedelta
from typing import Any

from novaquest.ai import guard
from novaquest.ai.offline import _facts
from novaquest.core import audit, clock
from novaquest.core.db import Db, Tx, jsonb
from novaquest.core.errors import NotFound, ValidationError
from novaquest.services import jobs, plans, syllabus, timetable
from novaquest.services.access import Actor, require

log = logging.getLogger("novaquest.prep")
JOB = "unit_pack"
STALE = timedelta(minutes=15)  # a pack still 'writing' after this was interrupted: offer to try again
PERIOD_MINUTES = (30, 35, 40, 45, 60)
MAX_NOTES = 4000
KINDS = ("Teacher demonstration", "Student activity", "Discussion")

# ---------- reading the book's text ----------
_NOISE = [re.compile(p) for p in (
    r"Chapter \d+\.indd\s+\d+\s+[\d/\-]+\s+[\d:]+\s*(?:AM|PM)?",  # printer's footer
    r"[A-Z][a-z]+\s+—\s+Textbook of [A-Za-z ]+? for (?:Grade|Class) \d+",  # running footer ('Curiosity — Textbook of …')
    r"Reprint \d{4}-\d{2}",
)]
_BOXES = re.compile(r"^(A step further|Our scientific heritage|Think like a scientist|Probe and ponder|Keep the curiosity alive|"
                    r"Let us enhance our learning|Revisiting|Did you know\??|Be a scientist|Find out|Fun fact)\s+")
_ACTIVITY = re.compile(r"\bActivity\s+(\d+(?:\.\d+)?)\s*:\s*")
_VERBS = ("take|collect|use|rinse|pour|put|place|add|mix|cut|fill|make|draw|observe|keep|cover|connect|roll|wrap|insert|hold|"
          "bring|repeat|label|mark|tie|mount|write|look|measure|prepare|read|discuss|find|note|record|list|join|remove|switch|"
          "close|open|leave|heat|boil|stir|shake|dip|spread|press|rub|blow|weigh|count|compare|arrange|divide|visit|ask|search|"
          "talk|identify|sort|plant|water|wash|pull|push|throw|drop|fix|set|try|allow|wait|check|show|hang|pick|knead|scrape|"
          "attach|fold|tape|wind|place|pass|shine|move|turn|lift|rotate|predict|sprinkle|squeeze|blow|feel|touch|listen|tilt|think|"
          "imagine|suppose|recall|secure|test|dry|soak|grind|crush|filter|glue|trace|bend|stretch|release|hit|tap|bounce|"
          "slide|float|sink|fan|continue|stop|start|light|lower|raise|cool|warm|freeze|melt|dissolve|place|clean|tilt|explore")
_STEP_START = re.compile(rf"^(?:(?:Now|Then|Next|Thereafter|Finally|Again|Also|[A-Z][a-z]+ly|After [^,]{{1,30}},?|"
                         rf"In (?:each|both|one|the other|bowl|beaker|glass)[^,]{{0,20}},)\s*)?(?:{_VERBS}|do not|don't|never|let)\b", re.I)
_TITLE_END = re.compile(rf"\s(?=(?:(?:{_VERBS.title()}|Can|Do you|Have you|In this|A group|While|What|How|Why)\b|Fig\.|\())")
_SAFETY = re.compile(r"\b(do not (?:touch|taste|eat|smell|look|connect|keep|use|leave|put|let|throw|drink|inhale|go|bring)|"
                     r"don't (?:touch|taste|eat|smell|look)|never|take care|careful(?:ly)?|supervision|caution|gloves|"
                     r"wash your hands|with the help of your teacher|elders?\b)", re.I)
_MATERIAL_CUT = re.compile(rf"(?:,?\s+and\s+|\s+—\s+|,\s+)(?=(?:{_VERBS})\b)|\s+(?:from|with the help of|in it)\b", re.I)


def clean(text: str, headers: tuple[str, ...] = ()) -> str:
    """Book text without printers' footers, running headers ('Chapter 2 — The Invisible Living World…'), single-spaced,
    with the book's bullet marks as '•'."""
    for rx in _NOISE:
        text = rx.sub(" ", text)
    for h in headers:
        text = re.sub(r"\s*".join(re.escape(w) for w in h.split()), " ", text)
    text = re.sub(r"\s+z\s+", " • ", " " + text)
    return re.sub(r"\s+", " ", text).strip()


def _steps_of(rest: str) -> list[tuple[str, bool]]:
    """Sentences of an activity, each marked True when the book printed it as a bullet point."""
    out = []
    for i, seg in enumerate(rest.split("•")):
        for j, sent in enumerate(re.split(r"(?<=[.?!])\s+(?=[A-Z‘“(])", seg)):
            sent = sent.strip()
            if len(sent) > 3:
                out.append((sent, i > 0 and j == 0))
    return out


def book_activities(chunks: list[tuple[int, int, str]], headers: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """The book's 'Activity 2.1: Let us observe' boxes in a chapter's text, each with its title, whether it's a teacher
    demonstration, what to get ready, its steps, the questions it asks and its safety notes. `chunks` are the chapter's
    passages in order as (topic id, page, text); `headers` are running headers to leave out."""
    text, where = "", []  # where: (offset, topic id, page)
    for topic_id, page, body in chunks:
        where.append((len(text), topic_id, page))
        text += clean(body, headers) + " "
    marks = list(_ACTIVITY.finditer(text))
    out: list[dict[str, Any]] = []
    for i, m in enumerate(marks):
        ref = m.group(1)
        if any(a["ref"] == ref for a in out):
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        nxt = re.search(r"\s\d+\.\d+(?:\.\d+)?\s+[A-Z][a-z]", text[m.end():end])  # the next numbered section
        body = text[m.end():min(end, m.end() + (nxt.start() if nxt else 1400), m.end() + 1400)]
        cut = _TITLE_END.search(body[:90] + " ")
        title = (body[:cut.start()] if cut else " ".join(body.split()[:6])).strip(" /:–-•")
        lets = re.match(r"(Let us\b(?:\s+[a-z][a-z’'/-]*\b(?![A-Za-z]))*)", title)
        if lets:  # 'Let us observe pond water/ stagnant water': the book's titles are lower case after 'Let us'
            title = lets.group(1).strip(" /")
        title = title.replace("*", "").strip()
        rest = body[len(title):].strip(" /")
        rest = re.sub(r"^Fig\.\s*\d+\.\d+[a-z]?:[^.•]{0,80}?(?=\s(?:[A-Z][a-z]+ ){2})", "", rest).strip()
        demo = bool(re.match(r"\(?\s*Teacher demonstration", rest, re.I))
        rest = re.sub(r"^\(?\s*Teacher demonstration activity\s*\)?", "", rest, flags=re.I).strip()
        steps, ask, safety, materials = [], [], [], []
        for s, bullet in _steps_of(rest):
            s = re.sub(r"\(?\bFig\.\s*\d+\.\d+[a-z]?\b\)?", "", s).strip()
            if len(s) < 4:
                continue
            if _SAFETY.search(s) and len(safety) < 3:
                safety.append(s[:220])
            if s.endswith("?"):
                s = _BOXES.sub("", s)
                if len(ask) < 4 and not re.search(r"…|\.\.\.|\([a-d]\)|(?:\b[A-Z][a-z]+\b\s+){4}", s):
                    ask.append(s[:200])
                continue
            if bullet or _STEP_START.match(s) or not steps:
                if len(steps) < 10:
                    steps.append(s[:240])
                if not materials and re.match(r"(Take|Collect)\b", s):
                    what = re.sub(r"^(Take|Collect)\s+", "", s).rstrip(".")
                    materials.append(_MATERIAL_CUT.split(what)[0].strip(" ,")[:200])
            elif len(steps) >= 2 and len(s.split()) > 6:
                break  # the book's explanation after the activity has started
        at = max((w for w in where if w[0] <= m.start()), key=lambda w: w[0], default=(0, None, None))
        out.append({"ref": ref, "title": title[:80] or f"Activity {ref}", "kind": KINDS[0] if demo else KINDS[1],
                    "materials": materials, "steps": steps, "ask": ask, "safety": " ".join(safety)[:400] or None,
                    "topic_id": at[1], "page": at[2]})
    return out


# ---------- the pack built from the book (no Claude) ----------
def _chapter(tx: Tx, chapter_id: int) -> dict[str, Any]:
    ch = tx.one("""SELECT n.id, n.code, n.title, n.meta, n.syllabus_id, s.part FROM syllabus_nodes n
                   JOIN syllabi s ON s.id=n.syllabus_id WHERE n.id=%s AND n.kind='chapter'""", [chapter_id])
    if not ch:
        raise NotFound("That chapter was not found.")
    tree = next((c for c in syllabus.get_tree(tx, ch["syllabus_id"]) if c["id"] == chapter_id), None)
    return {**ch, "topics": list(tree["topics"]) if tree else []}


def _pages(pages: list[int]) -> str:
    pages = sorted({p for p in pages if p})
    if not pages:
        return ""
    return f"p. {pages[0]}" if pages[0] == pages[-1] else f"pp. {pages[0]}–{pages[-1]}"


def _lessons(tx: Tx, topic_ids: list[int]) -> tuple[dict[int, dict], dict[int, dict]]:
    """Each topic's quick-check lesson and picture lesson, for a teacher: approved first, else a usable draft."""
    from novaquest.services import pictures

    lessons = {r["node_id"]: r["data"] for r in tx.all("""SELECT node_id, data FROM lessons WHERE node_id = ANY(%s)
                                                          ORDER BY (status='approved')""", [topic_ids])}
    pics = {r["node_id"]: r["data"] for r in tx.all("""SELECT node_id, data FROM picture_lessons WHERE node_id = ANY(%s)
                                                       ORDER BY (status='approved')""", [topic_ids]) if not r["data"].get("failed")}
    nodes = tx.all("SELECT id, code, syllabus_id FROM syllabus_nodes WHERE id = ANY(%s)", [topic_ids])
    pics.update(pictures.lessons_for(tx, nodes))
    return lessons, pics


def _check_from(lesson: dict | None, pic: dict | None) -> dict | None:
    for c in ((lesson or {}).get("checks") or []) + ((pic or {}).get("quiz") or []):
        q = _valid_check(c)
        if q:
            return q
    return None


def _valid_check(c: Any) -> dict | None:
    if not isinstance(c, dict):
        return None
    opts = [str(o).strip()[:160] for o in c.get("options") or [] if str(o).strip()]
    ans = c.get("answer")
    if not str(c.get("q", "")).strip() or len(opts) < 2 or len(set(opts)) != len(opts) or not isinstance(ans, int) \
            or not 0 <= ans < len(opts):
        return None
    return {"q": str(c["q"]).strip()[:300], "options": opts, "answer": ans, "why": str(c.get("why") or "").strip()[:300]}


_NOT_TERMS = {"bowl", "dough", "water", "glass", "beaker", "spoon", "box", "paper", "table", "activity", "figure", "picture"}
_QUESTION = re.compile(r"(how|what|why|when|where|which|who|do|does|did|is|are|can|could|should|will)\b", re.I)
_GENERIC_OUTCOME = re.compile(r"^(Explain the main ideas of|Understand)\b", re.I)


def _sentence_case(title: str) -> str:
    """'What Is a Cell?' → 'What is a cell?' (words in capitals, like DNA, stay)."""
    words = title.split()
    return " ".join([words[0]] + [w if w.isupper() and len(w) > 1 else w[:1].lower() + w[1:] for w in words[1:]]) if words else title


def _ask(title: str) -> str | None:
    """The topic's title as a question to ask the class, when the book titles it as one."""
    t = title.strip()
    return _sentence_case(t.rstrip("?")) + "?" if _QUESTION.match(t) else None


def _objective(title: str, outcome: str | None) -> str:
    if outcome and not _GENERIC_OUTCOME.match(outcome):
        return outcome
    q = _ask(title)
    return f"Answer “{q}” in their own words, using the book's ideas." if q else f"Explain {_sentence_case(title)[:1].lower() + _sentence_case(title)[1:]}."


def _oral_check(title: str, points: list[str]) -> dict:
    """Without a written check question: ask the topic's question again at the end and listen for the key points."""
    q = _ask(title) or f"What are the main ideas of {_sentence_case(title)[:1].lower() + _sentence_case(title)[1:]}?"
    return {"q": q, "options": [], "answer": None, "why": "", "listen": points[:2]}


def _hook(title: str, activity: dict | None, lesson: dict | None) -> str:
    if activity:
        return (f"Open with Activity {activity['ref']} ({activity['title']}) from the book"
                + (" as a demonstration" if activity["kind"] == KINDS[0] else "") + ": ask the class to predict what will happen first.")
    example = (lesson or {}).get("example")
    if example:
        return f"Start from an everyday example: {str(example).strip()[:300]}"
    q = _ask(title)
    if q:
        return f"Ask the class: “{q}” Take three answers, write them on the board and come back to them at the end."
    t = _sentence_case(title)
    return f"Ask the class what they already know about {t[:1].lower() + t[1:]}. Note their ideas and come back to them at the end."


_TALKS_TO_READER = re.compile(r"\b(you|your|we|us|our|let's)\b|°|\bFig\b|…|\d\.$|^(Suppose|Imagine|Yes|No|Here)\b", re.I)
_DEFINES = re.compile(r"\b(is|are) (called|known as)\b|\bmeans\b|\b(is|are) (a|an|the) \w+|\bunit of\b|\bconsists? of\b|\bmade (up )?of\b")
_PEOPLE = re.compile(r"\b(they|she|he|his|her|their|them)\b", re.I)
_STORY = re.compile(r"\b(was|were|had|went|said|asked|wanted|decided|excited|noticed|remembered|wondered)\b")


def _key_points(texts: list[str], terms: list[str], title: str, lesson: dict | None, pic: dict | None) -> list[str]:
    if pic and pic.get("crux_points"):
        return [str(p).strip()[:240] for p in pic["crux_points"] if str(p).strip()][:5]
    if lesson and lesson.get("explain"):
        pts = [s for s in re.split(r"(?<=[.!?])\s+", str(lesson["explain"])) if len(s.split()) >= 5]
        if pts:
            return pts[:5]
    facts = [_BOXES.sub("", f) for t in texts for f in _facts(clean(t))]
    facts = list(dict.fromkeys(f for f in facts if not _TALKS_TO_READER.search(f) and f[:1].isupper() and "•" not in f
                               and not _STEP_START.match(f) and not _STORY.search(f)))
    facts = [re.sub(r"^(Generally|Basically|In general),\s+(\w)", lambda m: m.group(2).upper(), f) for f in facts]
    plain = [f for f in facts if not _PEOPLE.search(f)]  # not the book's story about its characters ('As they rode…')
    facts = plain or facts
    words = [t.lower() for t in terms] + [w for w in re.findall(r"[a-z]{4,}", title.lower())
                                          if w not in {"what", "which", "where", "when", "does", "have", "with", "from", "that", "this", "considered"}]

    def score(f: str) -> tuple:
        # what the topic is about, definitions first; the book's stories about its characters ('As they rode…') last
        return (-(sum(1 for t in words if t in f.lower()) + (2 if _DEFINES.search(f) else 0) - (4 if _PEOPLE.search(f) else 0)),
                facts.index(f))
    scored = sorted(facts, key=score)
    return sorted(scored[:4], key=facts.index)


def from_book(tx: Tx, chapter_id: int) -> dict[str, Any]:
    """The pack built from the book alone (fast, no AI service)."""
    ch = _chapter(tx, chapter_id)
    topics = ch["topics"]
    ids = [t["id"] for t in topics]
    rows = tx.all("""SELECT p.node_id, p.page, p.body FROM source_passages p JOIN syllabus_nodes n ON n.id=p.node_id
                     WHERE p.node_id = ANY(%s) AND p.source_kind='textbook' ORDER BY n.ord, p.page NULLS LAST, p.id""", [ids])
    headers = (f"Chapter {ch['code']} — {ch['title']}", f"Chapter {ch['code']} – {ch['title']}")
    acts = book_activities([(r["node_id"], r["page"], r["body"]) for r in rows], headers)
    lessons, pics = _lessons(tx, ids)
    code = {t["id"]: t["code"] for t in topics}
    mix = tx.all("""SELECT topic_id, label, data FROM misconceptions WHERE chapter_id=%s ORDER BY (status='approved') DESC, code""",
                 [chapter_id])
    mixups = [{"label": m["label"], "wrong": str(m["data"].get("nova_says") or "")[:300],
               "right": str(m["data"].get("correct_idea") or "")[:300],
               "spot": ("Listen for: " + "; ".join(f"“{w}”" for w in m["data"].get("wrong_signals", [])[:3]))
               if m["data"].get("wrong_signals") else "",
               "fix": str(m["data"].get("probe") or "")[:300], "topic": code.get(m["topic_id"], "")} for m in mix]
    blocks = []
    for t in topics:
        texts = [r["body"] for r in rows if r["node_id"] == t["id"]]
        act = next((a for a in acts if a["topic_id"] == t["id"]), None)
        lesson, pic = lessons.get(t["id"]), pics.get(t["id"])
        terms = [x for x in t["meta"].get("terms", []) if not re.fullmatch(_VERBS, str(x).lower())
                 and str(x).lower() not in _NOT_TERMS][:8]
        points = _key_points(texts, terms, t["title"], lesson, pic)
        blocks.append({"id": t["id"], "code": t["code"], "title": t["title"],
                       "objective": _objective(t["title"], (t["outcomes"][0]["title"] if t.get("outcomes") else "")
                                               or t["meta"].get("outcome")),
                       "pages": _pages([r["page"] for r in rows if r["node_id"] == t["id"]]),
                       "hook": _hook(t["title"], act, lesson), "key_points": points,
                       "terms": terms, "activity": act["ref"] if act else None,
                       "check": _check_from(lesson, pic) or _oral_check(t["title"], points),
                       "watch_for": [m["label"] for m in mixups if m["topic"] == t["code"]][:3],
                       "weight": max(1, len(texts))})
    for a in acts:
        a["topic"] = code.get(a.pop("topic_id"), "")
    n_act = len(acts)
    overview = (f"{len(topics)} topic{'s' if len(topics) != 1 else ''}: " + " · ".join(t["title"] for t in topics)
                + (f". The book has {n_act} activit{'ies' if n_act != 1 else 'y'} in this chapter." if n_act else "."))
    return {"overview": overview, "topics": blocks, "mixups": mixups, "activities": acts,
            "terms": [{"term": x, "meaning": ""} for x in dict.fromkeys(x for b in blocks for x in b["terms"])][:20],
            "written_by": "book"}


# ---------- Claude's pack ----------
def _text(v: Any, n: int) -> str:
    s = re.sub(r"\s+", " ", str(v or "")).strip()[:n]
    return s if s and guard.screen_output(s) else ""


def _texts(v: Any, n: int, k: int) -> list[str]:
    return [x for x in (_text(i, n) for i in (v if isinstance(v, list) else [])) if x][:k]


_DIAGRAM_KINDS = {"flow", "cycle", "compare", "groups", "icons", "layers", "equation"}


def _diagrams(items: Any, points: int) -> list[dict[str, Any]]:
    """Claude's diagrams for a topic's key points, each checked like a picture-lesson card's picture; any that isn't
    right is left out."""
    from novaquest.services.pictures import ContentError, validate_pic

    out = []
    for d in items if isinstance(items, list) else []:
        if not isinstance(d, dict) or not isinstance(d.get("pic"), dict) or d["pic"].get("kind") not in _DIAGRAM_KINDS:
            continue
        try:
            validate_pic(d["pic"], "diagram")
        except (ContentError, KeyError, TypeError, ValueError):
            continue
        if not guard.screen_output(str(d["pic"])[:1200]):
            continue
        n = d.get("point")  # where it goes is decided by what it says when the slides are made (present.place_diagrams)
        out.append({"point": n if isinstance(n, int) and 0 <= n < points else (str(n)[:300] if isinstance(n, str) else None),
                    "pic": d["pic"]})
    return out[:2]


def merge(claude: Any, book: dict[str, Any]) -> dict[str, Any] | None:
    """Claude's pack checked against the book's: known topic codes only, valid check questions, safe and trimmed text;
    whatever Claude left out or got wrong comes from the book's pack. None when Claude's reply is unusable."""
    if not isinstance(claude, dict) or not isinstance(claude.get("topics"), list):
        return None
    by_code = {str(t.get("code", "")).strip(): t for t in claude["topics"] if isinstance(t, dict)}
    if not any(b["code"] in by_code for b in book["topics"]):
        return None
    topics = []
    for b in book["topics"]:
        c = by_code.get(b["code"], {})
        topics.append({**b, "diagrams": _diagrams(c.get("diagrams"), len(_texts(c.get("key_points"), 260, 5) or b["key_points"])),
                       "objective": _text(c.get("objective"), 240) or b["objective"],
                       "hook": _text(c.get("hook"), 400) or b["hook"],
                       "key_points": _texts(c.get("key_points"), 260, 5) or b["key_points"],
                       "check": _valid_check(c.get("check")) or b["check"],
                       "watch_for": _texts(c.get("watch_for"), 120, 3) or b["watch_for"],
                       "activity": _text(c.get("activity"), 200) or b["activity"]})
    codes = {b["code"] for b in book["topics"]}
    mixups = []
    for m in claude.get("mixups") if isinstance(claude.get("mixups"), list) else []:
        if isinstance(m, dict) and _text(m.get("label"), 120) and _text(m.get("right"), 400):
            mixups.append({"label": _text(m["label"], 120), "wrong": _text(m.get("wrong"), 300), "right": _text(m["right"], 400),
                           "spot": _text(m.get("spot"), 300), "fix": _text(m.get("fix"), 400),
                           "topic": str(m.get("topic", "")) if str(m.get("topic", "")) in codes else ""})
    book_acts = {a["ref"]: a for a in book["activities"]}
    acts = []
    for a in claude.get("activities") if isinstance(claude.get("activities"), list) else []:
        if not isinstance(a, dict) or not _text(a.get("title"), 100) or not _texts(a.get("steps"), 260, 10):
            continue
        ref = str(a.get("ref") or "").strip() or None
        mine = book_acts.get(ref or "", {})
        kind = str(a.get("kind") or "")
        acts.append({"ref": ref if ref in book_acts else None, "title": _text(a["title"], 100),
                     "kind": kind if kind in KINDS else (mine.get("kind") or KINDS[1]),
                     "materials": _texts(a.get("materials"), 160, 12), "steps": _texts(a.get("steps"), 260, 10),
                     "ask": _texts(a.get("ask"), 220, 5),
                     "safety": _text(a.get("safety"), 400) or mine.get("safety"),
                     "topic": str(a.get("topic", "")) if str(a.get("topic", "")) in codes else mine.get("topic", ""),
                     "page": mine.get("page")})
    got = {a["ref"] for a in acts if a["ref"]}
    acts += [a for a in book["activities"] if a["ref"] not in got]  # never lose one of the book's activities
    terms = [{"term": _text(t.get("term"), 60), "meaning": _text(t.get("meaning"), 240)}
             for t in (claude.get("terms") if isinstance(claude.get("terms"), list) else []) if isinstance(t, dict)]
    terms = [t for t in terms if t["term"]][:20]
    return {"overview": _text(claude.get("overview"), 600) or book["overview"], "topics": topics,
            "mixups": mixups or book["mixups"], "activities": acts, "terms": terms or book["terms"], "written_by": "claude"}


def _for_claude(tx: Tx, chapter_id: int, book: dict[str, Any]) -> tuple[str, str, str]:
    """(course, chapter heading, the chapter's text with topics, passages, the book's activities and known mix-ups)."""
    ch = _chapter(tx, chapter_id)
    course = syllabus.course(syllabus.get_syllabus(tx, ch["syllabus_id"]))
    header = (f"Chapter {ch['code']} — {ch['title']}",)
    lines = []
    for b in book["topics"]:
        lines.append(f"## Topic {b['code']} {b['title']}\nOutcome: {b['objective']}\nKey terms: {', '.join(b['terms'])}")
        for r in tx.all("""SELECT page, body FROM source_passages WHERE node_id=%s AND source_kind='textbook'
                           ORDER BY page NULLS LAST, id LIMIT 14""", [b["id"]]):
            lines.append(f"(p. {r['page']}) {clean(r['body'], header)}")
    for a in book["activities"]:
        lines.append(f"Book activity {a['ref']} (topic {a['topic']}): {a['title']}. {a['kind']}. Steps: {' '.join(a['steps'])}"
                     + (f" Safety: {a['safety']}" if a["safety"] else ""))
    for m in book["mixups"]:
        lines.append(f"Known mix-up (topic {m['topic']}): {m['label']}. Correct idea: {m['right']}")
    return course, f"Chapter {ch['code']}: {ch['title']}", "\n".join(lines)[:60000]


TOPICS_PER_CALL = 2
ACTIVITIES_PER_CALL = 4


def _parts(book: dict[str, Any]) -> list[tuple[str, str, int]]:
    """The pieces Claude writes the pack in: (what, the request, room for the reply in tokens). Small pieces, so no
    reply is long enough to be cut off (a whole chapter in one reply was, for Curiosity's Chapter 2)."""
    out = [("summary", 'Write only {"overview", "mixups", "terms"} for this chapter.', 3500)]
    codes = [t["code"] for t in book["topics"]]
    for i in range(0, len(codes), TOPICS_PER_CALL):
        part = codes[i:i + TOPICS_PER_CALL]
        out.append(("topics", 'Write only {"topics": [...]} for topic' + ("s " if len(part) > 1 else " ") + ", ".join(part)
                    + ", in that order.", 4500))
    refs = [a["ref"] for a in book["activities"]]
    for i in range(0, len(refs), ACTIVITIES_PER_CALL):
        part = refs[i:i + ACTIVITIES_PER_CALL]
        last = i + ACTIVITIES_PER_CALL >= len(refs)
        out.append(("activities", 'Write only {"activities": [...]} for the book\'s activit' + ("ies " if len(part) > 1 else "y ")
                    + ", ".join(part) + (", then at most two demos of your own." if last else "."), 3000))
    if not refs:
        out.append(("activities", 'Write only {"activities": [...]}: at most two simple demos of your own.', 1500))
    return out


def write_with_claude(db: Db, engine: Any, inst: int, chapter_id: int) -> None:
    """The background job: Claude writes the chapter's pack a part at a time (the overview and mix-ups, two topics at
    a time, four activities at a time); kept for the whole school. A part that fails comes from the book's pack; if
    every topic part fails, the job fails, the book's pack stays in use and the teacher can try again."""
    try:
        with db.tenant(inst) as tx:
            book = from_book(tx, chapter_id)
            course, heading, text = _for_claude(tx, chapter_id, book)
        fn = getattr(engine, "draft_unit_pack", None)
        if not fn:
            raise ValidationError("Claude isn't set up on this NovaQuest.")
        got: dict[str, Any] = {"topics": [], "mixups": [], "activities": [], "terms": []}
        failed = []
        for what, ask, room in _parts(book):
            part = fn(course, heading, text, ask, room)
            if not isinstance(part, dict):
                failed.append(what)
                continue
            if what == "summary":
                got["overview"] = part.get("overview")
                for k in ("mixups", "terms"):
                    got[k] = part.get(k) if isinstance(part.get(k), list) else []
            elif isinstance(part.get(what), list):
                got[what] += [x for x in part[what] if isinstance(x, dict)]
            else:
                failed.append(what)
        pack = merge(got, book)
        if not pack:
            why = getattr(engine, "last_error", "") or "Claude didn't reply with a usable pack"
            raise ValidationError(f"{why}.")
        if failed:
            pack["partial"] = sorted(set(failed))  # those parts come from the book
        with db.tenant(inst) as tx:
            tx.exec("""UPDATE unit_packs SET status='ready', data=%s, error=NULL, updated_at=%s
                       WHERE institution_id=%s AND chapter_id=%s""", [jsonb(pack), clock.now(), inst, chapter_id])
    except Exception as exc:  # the teacher sees why and can try again; the book's pack is shown meanwhile
        log.exception("unit pack for chapter %s failed", chapter_id)
        msg = exc.message if isinstance(exc, ValidationError) else "Claude couldn't write the pack this time. Try again later."
        with db.tenant(inst) as tx:
            tx.exec("""UPDATE unit_packs SET status='failed', error=%s, updated_at=%s WHERE institution_id=%s AND chapter_id=%s""",
                    [msg[:300], clock.now(), inst, chapter_id])


def request_claude(tx: Tx, actor: Actor, chapter_id: int) -> None:
    """Ask Claude to write (or rewrite) the chapter's pack in the background."""
    require(actor, "teacher", "leader", "inst_admin")
    _chapter(tx, chapter_id)
    now = clock.now()
    tx.exec("""INSERT INTO unit_packs(institution_id, chapter_id, status, requested_by, updated_at) VALUES (%s,%s,'writing',%s,%s)
               ON CONFLICT (institution_id, chapter_id) DO UPDATE SET status='writing', error=NULL,
               requested_by=EXCLUDED.requested_by, updated_at=EXCLUDED.updated_at""", [tx.inst, chapter_id, actor.user_id, now])
    jobs.enqueue(tx, JOB, {"chapter_id": chapter_id}, dedupe_key=f"{JOB}:{tx.inst}:{chapter_id}:{now.timestamp():.0f}")
    audit.record(tx, "prep.claude", actor_id=actor.user_id, target=f"chapter:{chapter_id}")


# ---------- the page ----------
def stored(tx: Tx, chapter_id: int) -> dict[str, Any] | None:
    row = tx.one("SELECT status, data, error, updated_at FROM unit_packs WHERE chapter_id=%s", [chapter_id])
    if row and row["status"] == "writing" and clock.now() - row["updated_at"] > STALE:
        row = {**row, "status": "failed", "error": "Claude didn't finish writing the pack."}
    if row and row.get("error") and re.search(r"JSON|column \d+|char \d+|Traceback", row["error"]):
        row = {**row, "error": "Claude's reply wasn't complete."}  # a technical reason from an earlier version
    return row


def pack(tx: Tx, chapter_id: int) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(the pack to show: Claude's if it's ready, else the book's; the stored row with Claude's status, if any)."""
    row = stored(tx, chapter_id)
    if row and row["data"]:
        return row["data"], row
    return from_book(tx, chapter_id), row


def default_periods(tx: Tx, class_id: int, chapter_id: int, data: dict[str, Any]) -> int:
    """Periods for the chapter: the school days the term plan gives it (within reason), else one per topic, one for
    the book's activities and one to review."""
    n = len(data["topics"])
    p = tx.one("SELECT starts_on, ends_on, syllabus_id FROM term_plans WHERE class_id=%s AND chapter_id=%s", [class_id, chapter_id])
    if p:
        lessons = timetable.lessons_between(tx, class_id, subject_ids(tx, class_id, p["syllabus_id"]), p["starts_on"], p["ends_on"])
        days = lessons if lessons is not None else len(plans.school_days(p["starts_on"], p["ends_on"]))
        return max(1, min(days, 2 * n + 2, 20))
    return max(1, n + (1 if data["activities"] else 0) + 1)


def subject_ids(tx: Tx, class_id: int, syllabus_id: int) -> list[int]:
    """Every part of the class's subject that this book (part) belongs to."""
    book = next((b for b in syllabus.class_subjects(tx, class_id) if syllabus_id in b["ids"]), None)
    return book["ids"] if book else [syllabus_id]


def position(tx: Tx, class_id: int, chapter_id: int, periods: int, now=None) -> dict[str, Any] | None:
    """Where a class is in a chapter's lesson plan: the period to teach next ('period' of 'of'), from the lessons
    already taught since the chapter's start date in the term plan (the timetable's lessons, else school days).
    None when the chapter has no dates for this class."""
    p = tx.one("SELECT starts_on, ends_on, syllabus_id FROM term_plans WHERE class_id=%s AND chapter_id=%s", [class_id, chapter_id])
    if not p:
        return None
    now = now or clock.now()
    today = clock.to_ist(now).date()
    if today < p["starts_on"]:
        return {"period": 1, "of": periods, "held": 0, "started": False, "starts_on": p["starts_on"], "done": False}
    held = timetable.lessons_held(tx, class_id, subject_ids(tx, class_id, p["syllabus_id"]), p["starts_on"], now)
    if held is None:
        held = len(plans.school_days(p["starts_on"], today - timedelta(days=1))) if today > p["starts_on"] else 0
    return {"period": max(1, min(held + 1, periods)), "of": periods, "held": held, "started": True,
            "starts_on": p["starts_on"], "done": held >= periods}


def _flow(minutes: int, kind: str) -> list[tuple[int, str]]:
    """A period's timed steps (minutes add up to the period)."""
    if kind == "activity":
        shares = [("Set up and safety", 0.15), ("Do the activity", 0.5), ("Discuss what they saw", 0.25), ("Note it down", 0.1)]
    elif kind == "review":
        shares = [("Recall: what do we know now?", 0.15), ("Clear up the mix-ups", 0.35), ("Quiz (projector or worksheet)", 0.35),
                  ("Wrap up and next chapter", 0.15)]
    else:
        shares = [("Hook", 0.1), ("Teach the key points", 0.4), ("Activity or discussion", 0.25), ("Check", 0.15), ("Wrap up", 0.1)]
    out = [(max(2, round(minutes * s)), what) for what, s in shares]
    out[1] = (out[1][0] + minutes - sum(m for m, _ in out), out[1][1])
    return [(m, w) for m, w in out]


def arrange(data: dict[str, Any], periods: int, minutes: int) -> list[dict[str, Any]]:
    """The chapter's topics shared out over the periods. Fewer periods than topics: neighbouring topics share a period
    (by how much the book says about each). More: a period for the book's activities, one to review, and the
    longest topics get a second period for practice."""
    topics = data["topics"]
    if not topics:
        return []
    out: list[dict[str, Any]] = []
    if periods < len(topics):
        total = sum(t["weight"] for t in topics)
        groups: list[list[dict]] = [[] for _ in range(periods)]
        acc = 0
        for t in topics:
            k = min(periods - 1, int(periods * acc / total))
            groups[k].append(t)
            acc += t["weight"]
        groups = [g for g in groups if g]
        for g in groups:
            out.append({"kind": "teach", "topics": g})
    else:
        extra = periods - len(topics)
        acts = 1 if extra and data["activities"] else 0
        review = 1 if extra - acts > 0 else 0
        practice = extra - acts - review
        longest = {t["code"] for t in sorted(topics, key=lambda t: -t["weight"])[:practice]}
        for t in topics:
            out.append({"kind": "teach", "topics": [t]})
            if t["code"] in longest:
                out.append({"kind": "practice", "topics": [t]})
        if acts:
            out.append({"kind": "activity", "topics": [], "activities": data["activities"]})
        if review:
            out.append({"kind": "review", "topics": []})
        practice_left = practice - len(longest)
        out += [{"kind": "practice", "topics": topics} for _ in range(max(0, practice_left))]
    for i, p in enumerate(out, 1):
        p["no"] = i
        p["flow"] = _flow(minutes, "teach" if p["kind"] == "practice" else p["kind"])
        p["title"] = {"activity": "Activities from the book", "review": "Review and quiz",
                      "practice": "Practice: " + ", ".join(t["title"] for t in p["topics"])}.get(
            p["kind"], " + ".join(f"{t['code']} {t['title']}" for t in p["topics"]))
    return out


def notes(tx: Tx, actor: Actor, chapter_id: int) -> str:
    return tx.val("SELECT body FROM unit_notes WHERE teacher_id=%s AND chapter_id=%s", [actor.user_id, chapter_id]) or ""


def save_notes(tx: Tx, actor: Actor, chapter_id: int, body: str) -> None:
    require(actor, "teacher")
    _chapter(tx, chapter_id)
    body = body.strip()
    if len(body) > MAX_NOTES:
        raise ValidationError(f"Keep your notes under {MAX_NOTES} characters.", field="notes")
    if not body:
        tx.exec("DELETE FROM unit_notes WHERE teacher_id=%s AND chapter_id=%s", [actor.user_id, chapter_id])
        return
    tx.exec("""INSERT INTO unit_notes(institution_id, teacher_id, chapter_id, body, updated_at) VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (teacher_id, chapter_id) DO UPDATE SET body=EXCLUDED.body, updated_at=EXCLUDED.updated_at""",
            [tx.inst, actor.user_id, chapter_id, body, clock.now()])


def seen_in_class(tx: Tx, class_id: int, chapter_id: int) -> list[dict[str, Any]]:
    """The chapter's mix-ups this class has already shown in its answers (most students first)."""
    return tx.all("""SELECT m.label, count(DISTINCT t.student_id) AS students FROM responses r
        JOIN attempts t ON t.id=r.attempt_id JOIN assignments a ON a.id=t.assignment_id
        JOIN LATERAL unnest(r.misconception_codes) x(code) ON true
        JOIN misconceptions m ON m.code=x.code AND m.chapter_id=%s
        WHERE a.class_id=%s GROUP BY m.label ORDER BY students DESC LIMIT 5""", [chapter_id, class_id])


def opening_quiz(db: Db, engine: Any, actor: Actor, class_id: int, chapter_id: int) -> int:
    """A five-question draft quiz across the chapter's topics, to open the unit (find out what the class already
    knows) or to show on the projector. The teacher checks it like any assignment."""
    from novaquest.services import questions

    with db.tenant(actor.institution_id) as tx:
        ch = _chapter(tx, chapter_id)
    ids = [t["id"] for t in ch["topics"]]
    if not ids:
        raise ValidationError("This chapter has no topics yet.")
    random.Random(chapter_id).shuffle(ids)
    title = f"Opening quiz: {'Part ' + str(ch['part']) + ' · ' if ch['part'] else ''}Ch {ch['code']} {ch['title']}"
    return questions.generate_assignment(db, engine, actor, class_id=class_id, topic_ids=sorted(ids[:8]), kind="quiz",
                                         difficulty="easy", count=5, title=title[:120])
