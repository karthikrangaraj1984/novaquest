"""Exam mode (1.31.0): a student preparing for a school exam picks the subject, its chapters and the date. NovaQuest
counts down the days, spreads their weakest topics in those chapters over the days left (a revision plan they can
tick off by doing), and gives timed practice papers from the school's approved questions, marked at once.

Practice papers are for the student only: they never change a topic's level or any score the teacher sees."""
from __future__ import annotations

import hashlib
import math
import random
from datetime import date, timedelta
from typing import Any

from novaquest.core import clock
from novaquest.core.db import Tx, jsonb
from novaquest.core.errors import Conflict, NotFound, ValidationError
from novaquest.services import daily, syllabus
from novaquest.services.access import Actor, require

PAPER_SIZE = 10
PAPER_MINUTES = 15


def today() -> date:
    return clock.to_ist(clock.now()).date()


def _subject(tx: Tx, student_id: int, syllabus_id: int) -> dict[str, Any]:
    book = next((b for b in syllabus.student_subjects(tx, student_id) if syllabus_id in b["ids"]), None)
    if not book:
        raise ValidationError("Choose one of your subjects.")
    return book


def create(tx: Tx, actor: Actor, *, title: str, syllabus_id: int, chapter_ids: list[int], exam_on: date | None) -> int:
    """A new exam to prepare for."""
    require(actor, "student")
    book = _subject(tx, actor.user_id, syllabus_id)
    title = " ".join(str(title or "").split())[:60] or f"{book['subject']} exam"
    if not exam_on:
        raise ValidationError("Give the date of the exam.")
    if exam_on < today() or exam_on > today() + timedelta(days=200):
        raise ValidationError("Choose a date between today and the next six months.")
    tree = {ch["id"] for ch in syllabus.subject_tree(tx, book)}
    chapters = [c for c in dict.fromkeys(chapter_ids) if c in tree]
    if not chapters:
        raise ValidationError("Tick the chapters that come in the exam.")
    return tx.val("""INSERT INTO exam_plans(institution_id, student_id, title, syllabus_ids, chapter_ids, exam_on, created_at)
                     VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                  [tx.inst, actor.user_id, title, book["ids"], chapters, exam_on, clock.now()])


def _plan(tx: Tx, actor: Actor, plan_id: int) -> dict[str, Any]:
    require(actor, "student")
    p = tx.one("SELECT * FROM exam_plans WHERE id=%s AND student_id=%s", [plan_id, actor.user_id])
    if not p:
        raise NotFound("That exam wasn't found.")
    return p


def remove(tx: Tx, actor: Actor, plan_id: int) -> None:
    _plan(tx, actor, plan_id)
    tx.exec("DELETE FROM exam_plans WHERE id=%s", [plan_id])


def upcoming(tx: Tx, student_id: int) -> list[dict[str, Any]]:
    """The student's exams from today on, soonest first, with the days left."""
    rows = tx.all("SELECT id, title, exam_on FROM exam_plans WHERE student_id=%s AND exam_on >= %s ORDER BY exam_on",
                  [student_id, today()])
    for r in rows:
        r["days_left"] = (r["exam_on"] - today()).days
    return rows


def _topics(tx: Tx, student_id: int, chapter_ids: list[int]) -> list[dict[str, Any]]:
    rows = tx.all("""SELECT t.id, t.title, t.code, c.title AS chapter, c.code AS chapter_code, coalesce(m.level, 0) AS level
                     FROM syllabus_nodes t JOIN syllabus_nodes c ON c.id=t.parent_id
                     LEFT JOIN mastery m ON m.topic_id=t.id AND m.student_id=%s
                     WHERE t.kind='topic' AND t.parent_id = ANY(%s) ORDER BY c.ord, t.ord""", [student_id, chapter_ids])
    return rows


def view(tx: Tx, actor: Actor, plan_id: int) -> dict[str, Any]:
    """The countdown, where the student stands on the exam's topics, the day-by-day revision plan and practice papers.

    The plan: every topic not yet mastered, weakest first (then in book order), shared over the days before the
    exam, with the last day kept for a practice paper. A topic counts as revised once the student has done anything
    on it since the plan was made."""
    p = _plan(tx, actor, plan_id)
    topics = _topics(tx, actor.user_id, p["chapter_ids"])
    touched = {r["topic_id"] for r in tx.all("""SELECT DISTINCT topic_id FROM learn_events WHERE student_id=%s AND created_at >= %s
                                                AND topic_id = ANY(%s)""", [actor.user_id, p["created_at"], [t["id"] for t in topics]])}
    todo = sorted([t for t in topics if t["level"] < 3], key=lambda t: t["level"])
    days_left = (p["exam_on"] - today()).days
    plan_days = max(1, days_left - 1) if days_left > 1 else 1
    first = min(today(), p["exam_on"])
    per_day = max(1, math.ceil(len(todo) / plan_days)) if todo else 0
    days = []
    for i in range(plan_days):
        chunk = todo[i * per_day:(i + 1) * per_day]
        if not chunk:
            break
        d = first + timedelta(days=i)
        days.append({"date": d, "today": d == today(), "past": d < today(),
                     "topics": [{**t, "done": t["id"] in touched} for t in chunk]})
    papers = tx.all("""SELECT id, score, done_at, started_at FROM exam_practice WHERE plan_id=%s ORDER BY started_at""", [plan_id])
    levels = [t["level"] for t in topics]
    return {"plan": p, "days_left": days_left, "topics": topics, "days": days, "papers": papers,
            "ready": {"mastered": levels.count(3), "practised": levels.count(2), "learning": levels.count(1),
                      "new": levels.count(0), "total": len(levels)},
            "chapters": tx.all("SELECT id, code, title FROM syllabus_nodes WHERE id = ANY(%s) ORDER BY ord", [p["chapter_ids"]]),
            "revised": sum(1 for t in todo if t["id"] in touched), "to_revise": len(todo)}


def start_paper(tx: Tx, actor: Actor, plan_id: int) -> int:
    """A timed practice paper: ten multiple-choice questions spread over the exam's topics, from the school's approved
    questions first (the same pool as Daily 5)."""
    p = _plan(tx, actor, plan_id)
    open_paper = tx.val("SELECT id FROM exam_practice WHERE plan_id=%s AND done_at IS NULL", [plan_id])
    if open_paper:
        return open_paper
    topics = _topics(tx, actor.user_id, p["chapter_ids"])
    if not topics:
        raise ValidationError("These chapters have no topics yet.")
    seed = int(hashlib.sha256(f"{actor.user_id}:{plan_id}:{clock.now().isoformat()}".encode()).hexdigest()[:8], 16)
    rnd = random.Random(seed)
    order = sorted(topics, key=lambda t: (t["level"], rnd.random()))  # weakest topics first
    items, seen, pools = [], set(), {}
    k = 0
    while len(items) < PAPER_SIZE and k < PAPER_SIZE * 4:
        t = order[k % len(order)]
        if t["id"] not in pools:
            pools[t["id"]] = daily._questions(tx, t["id"], rnd)
        q = next((q for q in pools[t["id"]] if (q["prompt"], str(q["options"])) not in seen), None)
        if q:
            seen.add((q["prompt"], str(q["options"])))
            items.append({"topic_id": t["id"], "topic": t["title"], **q})
        k += 1
    if not items:
        raise ValidationError("There aren't any questions for these chapters yet.")
    return tx.val("""INSERT INTO exam_practice(institution_id, student_id, plan_id, items, minutes, started_at)
                     VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                  [tx.inst, actor.user_id, plan_id, jsonb(items), PAPER_MINUTES, clock.now()])


def paper(tx: Tx, actor: Actor, paper_id: int) -> dict[str, Any]:
    require(actor, "student")
    row = tx.one("""SELECT x.*, p.title FROM exam_practice x JOIN exam_plans p ON p.id=x.plan_id
                    WHERE x.id=%s AND x.student_id=%s""", [paper_id, actor.user_id])
    if not row:
        raise NotFound("That practice paper wasn't found.")
    row["ends_at"] = row["started_at"] + timedelta(minutes=row["minutes"])
    return row


def submit_paper(tx: Tx, actor: Actor, paper_id: int, answers: dict[int, Any]) -> dict[str, Any]:
    """Mark the paper at once (unanswered counts as wrong). It's practice: no topic levels change."""
    row = paper(tx, actor, paper_id)
    if row["done_at"]:
        raise Conflict("You've already handed in this practice paper.")
    results, right = [], 0
    for i, q in enumerate(row["items"]):
        raw = answers.get(i)
        ok = raw not in (None, "") and str(raw).isdigit() and int(raw) == int(q["answer"])
        right += ok
        results.append({"topic": q["topic"], "topic_id": q["topic_id"], "prompt": q["prompt"], "correct": ok,
                        "answer": q["options"][int(q["answer"])], "why": q.get("why", "")})
    score = right / len(results) if results else 0.0
    late = clock.now() > row["ends_at"] + timedelta(minutes=1)
    tx.exec("UPDATE exam_practice SET answers=%s, score=%s, done_at=%s WHERE id=%s",
            [jsonb({str(k): v for k, v in answers.items()}), score, clock.now(), paper_id])
    weak = sorted({(r["topic_id"], r["topic"]) for r in results if not r["correct"]})
    return {"results": results, "right": right, "total": len(results), "late": late, "plan_id": row["plan_id"],
            "weak": [{"topic_id": t, "title": n} for t, n in weak][:5]}


def choices(tx: Tx, student_id: int) -> list[dict[str, Any]]:
    """The student's subjects with their chapters, for the 'new exam' form."""
    return [{"book": b, "chapters": [{"id": ch["id"], "code": ch["code"], "title": ch["title"], "part": ch["part"]}
                                     for ch in syllabus.subject_tree(tx, b)]} for b in syllabus.student_subjects(tx, student_id)]
