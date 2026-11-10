"""The learning path: every topic is learnt in the same simple order (UX-1).

  1. Learn in pictures  →  2. Practise  →  3. Prove it (understanding check)  →  4. Teach NOVA (when NOVA has a mix-up
  for the topic). Each step is "done" from the student's own evidence, and there is always exactly one next step, so
  every screen can offer a single Continue button."""
from __future__ import annotations

from typing import Any, Iterable

from novaquest.core.db import Tx
from novaquest.services import mastery, pictures

PASS = mastery.PRACTISED_AT  # a practice or understanding score at least this counts as done

STEPS = (
    ("pictures", "Learn in pictures", "The idea in simple words, one picture at a time", "/student/learn/{id}/pictures"),
    ("practice", "Practise", "A short mission: answer a few questions", "/student/learn/{id}/practice"),
    ("understand", "Prove it", "Explain it in your own words and use it", "/student/learn/{id}/understand"),
    ("teach", "Teach NOVA", "Fix NOVA's mix-up: the best test of all", "/student/teach/{id}"),
)


def _evidence(tx: Tx, student_id: int, topic_ids: Iterable[int] | None = None) -> dict[int, dict[str, float | None]]:
    """Best score per topic and kind of learning event (None when recorded without a score)."""
    sql = """SELECT topic_id, kind, max(score) AS best, count(*) AS n FROM learn_events
             WHERE student_id=%s {} GROUP BY topic_id, kind"""
    ids = list(topic_ids) if topic_ids is not None else None
    rows = tx.all(sql.format("AND topic_id = ANY(%s)" if ids is not None else ""), [student_id, ids] if ids is not None else [student_id])
    out: dict[int, dict[str, float | None]] = {}
    for r in rows:
        out.setdefault(r["topic_id"], {})[r["kind"]] = float(r["best"]) if r["best"] is not None else -1.0
    return out


def teachable(tx: Tx) -> tuple[set[int], set[int]]:
    """(topic ids, chapter ids) for which NOVA has a reviewed mix-up to hold."""
    rows = tx.all("SELECT topic_id, chapter_id FROM misconceptions WHERE status='approved'")
    return {r["topic_id"] for r in rows if r["topic_id"]}, {r["chapter_id"] for r in rows if r["chapter_id"]}


def _steps(topic: dict, ev: dict[str, float | None], can_teach: bool, has_pictures: bool) -> list[dict[str, Any]]:
    steps = []
    for key, name, about, url in STEPS:
        if key == "teach" and not can_teach:
            continue
        if key == "pictures" and not has_pictures:
            continue
        if key == "pictures":
            done, score = "pictures" in ev, None
        elif key == "practice":
            score = ev.get("practice")
            done = score is not None and score >= PASS
        elif key == "understand":
            score = ev.get("understanding")
            done = score is not None and score >= PASS
        else:
            score = ev.get("teach")
            done = score is not None and score >= mastery.MASTERED_AT
        tried = key != "pictures" and score is not None and not done
        steps.append({"key": key, "name": name, "about": about, "url": url.format(id=topic["id"]), "done": done,
                      "tried": tried, "score": None if score is None or score < 0 else round(score * 100)})
    nxt = next((s for s in steps if not s["done"]), None)
    for i, s in enumerate(steps, 1):
        s["no"] = i
        s["state"] = "done" if s["done"] else ("next" if s is nxt else "later")
    return steps


def topic_path(tx: Tx, student_id: int, topic: dict) -> dict[str, Any]:
    """The 4 steps for one topic with their state, and the single next step."""
    ev = _evidence(tx, student_id, [topic["id"]]).get(topic["id"], {})
    t_ids, ch_ids = teachable(tx)
    can_teach = topic["id"] in t_ids or topic.get("chapter_id") in ch_ids
    steps = _steps(topic, ev, can_teach, pictures.has_lesson_for(tx, topic))
    nxt = next((s for s in steps if s["state"] == "next"), None)
    return {"steps": steps, "next": nxt, "done": sum(s["done"] for s in steps), "total": len(steps), "complete": nxt is None}


def annotate(tx: Tx, student_id: int, chapters: list[dict]) -> None:
    """Add path progress (done/total and the next step) to every topic in a chapter list, in two queries."""
    ev = _evidence(tx, student_id)
    t_ids, ch_ids = teachable(tx)
    lessons = pictures.lessons_for(tx, [t for ch in chapters for t in ch["topics"]])
    levels = {r["topic_id"]: r["level"] for r in tx.all("SELECT topic_id, level FROM mastery WHERE student_id=%s", [student_id])}
    for ch in chapters:
        for t in ch["topics"]:
            steps = _steps(t, ev.get(t["id"], {}), t["id"] in t_ids or ch["id"] in ch_ids, t["id"] in lessons)
            t["path_done"], t["path_total"] = sum(s["done"] for s in steps), len(steps)
            t["path_next"] = next((s for s in steps if s["state"] == "next"), None)
            t["started"] = t["id"] in ev
            t["level"] = levels.get(t["id"], 0)  # the one status shown everywhere (1.31.0): the topic's level
        ch["path_done"] = sum(t["path_done"] == t["path_total"] for t in ch["topics"])
        ch["practised"] = sum(t["level"] >= 2 for t in ch["topics"])
        ch["mastered"] = sum(t["level"] == 3 for t in ch["topics"])


def next_for(level: int, path: dict[str, Any], topic_id: int) -> dict[str, Any] | None:
    """The one button for a topic (1.31.0): a mastered topic offers revision (practise again), not step 1 of the path;
    otherwise the next step of the path; None when everything is done."""
    if level >= 3:
        return {"key": "practice", "name": "Revise", "url": f"/student/learn/{topic_id}/practice", "revise": True}
    return path.get("next")


def continue_topic(tx: Tx, student_id: int, chapters: list[dict]) -> dict | None:
    """Where to pick up: the most recently touched unfinished topic, else the first unstarted topic of the demo
    chapter (or the first chapter). Expects `annotate` to have run."""
    recent = tx.all("""SELECT topic_id FROM learn_events WHERE student_id=%s GROUP BY topic_id
                       ORDER BY max(created_at) DESC LIMIT 20""", [student_id])
    by_id = {t["id"]: (t, ch) for ch in chapters for t in ch["topics"]}
    for r in recent:
        hit = by_id.get(r["topic_id"])
        if hit and hit[0]["path_next"] and hit[0].get("level", 0) < 3:  # a mastered topic isn't "where you left off"
            return {"topic": hit[0], "chapter": hit[1], "step": hit[0]["path_next"], "resume": True}
    ordered = sorted(chapters, key=lambda c: not c.get("demo"))
    for ch in ordered:
        for t in ch["topics"]:
            if t["path_next"] and t.get("level", 0) < 3:
                return {"topic": t, "chapter": ch, "step": t["path_next"], "resume": False}
    return None
