"""Daily 5: a five-question review a day, from the topics each student is due to revisit (1.17.0).

Spaced repetition (the Leitner method): every topic a student has started has a review box. A right answer moves the
topic up a box, so its next review comes later (1, 2, 4, 7, 14, then 30 days); a wrong one sends it back to the first
box, to come up again tomorrow. Each day's five questions come from the topics due first, then the weakest. Questions
come from the school's approved questions (the question bank, then the topic's approved quick checks and picture
quizzes), and only then from the textbook, so Daily 5 never calls Claude. Reviews count as learning activity but never
raise a topic's level on their own."""
from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta
from typing import Any

from novaquest.core import clock
from novaquest.core.db import Tx, jsonb
from novaquest.core.errors import Conflict, NotFound
from novaquest.services import learn, mastery, syllabus
from novaquest.services.access import Actor, require
from novaquest.services.context import topic_ctx

SIZE = 5
INTERVALS = (1, 2, 4, 7, 14, 30)  # days until the next review, by box


def today() -> date:
    return clock.to_ist(clock.now()).date()


def _schedule(tx: Tx, student_id: int) -> list[dict]:
    """The student's review boxes, adding any topic they've started since (due today)."""
    books = [b["id"] for b in syllabus.student_syllabi(tx, student_id)]
    if not books:
        return []
    tx.exec("""INSERT INTO review_items(institution_id, student_id, topic_id, box, due_on)
               SELECT DISTINCT %s, %s, e.topic_id, 0, %s::date FROM learn_events e JOIN syllabus_nodes n ON n.id=e.topic_id
               WHERE e.student_id=%s AND n.syllabus_id = ANY(%s) AND n.kind='topic'
               ON CONFLICT (student_id, topic_id) DO NOTHING""", [tx.inst, student_id, today(), student_id, books])
    return tx.all("""SELECT r.topic_id, r.box, r.due_on, n.title, coalesce(m.level, 0) AS level FROM review_items r
                     JOIN syllabus_nodes n ON n.id=r.topic_id LEFT JOIN mastery m ON m.student_id=r.student_id AND m.topic_id=r.topic_id
                     WHERE r.student_id=%s AND n.syllabus_id = ANY(%s)""", [student_id, books])


def _questions(tx: Tx, topic_id: int, rnd: random.Random) -> list[dict]:
    """Multiple-choice questions for one topic, best first: the approved bank and approved checks (in a fresh order
    each day), then questions made from the textbook to fill up."""
    out = [{"prompt": r["prompt"], "options": r["options"], "answer": int(r["answer"]["index"])}
           for r in learn._bank_pool(tx, topic_id) if r["kind"] in ("mcq", "ai_moment") and r["options"]]
    for sql in ("SELECT data->'checks' AS qs FROM lessons WHERE node_id=%s AND status='approved'",
                "SELECT data->'quiz' AS qs FROM picture_lessons WHERE node_id=%s AND status='approved'"):
        for row in tx.all(sql, [topic_id]):
            for c in row["qs"] or []:
                try:
                    opts, ans = [str(o) for o in c["options"]], int(c["answer"])
                except (KeyError, TypeError, ValueError):
                    continue
                if c.get("q") and len(opts) >= 2 and 0 <= ans < len(opts):
                    out.append({"prompt": str(c["q"]), "options": opts, "answer": ans, "why": c.get("why") or ""})
    rnd.shuffle(out)
    from novaquest.ai.claude import swapped_word_options  # one sentence with a word swapped isn't a fair question (1.31.0)

    extra = []
    for d in learn._OFFLINE.generate_questions([topic_ctx(tx, topic_id)], "mcq", "medium", SIZE, seed=topic_id):
        if d.options and d.answer and "index" in d.answer and not swapped_word_options(d.options):
            extra.append({"prompt": d.prompt, "options": d.options, "answer": int(d.answer["index"])})
    rnd.shuffle(extra)
    return [q for q in out if not swapped_word_options(q["options"])] + extra


def _build(tx: Tx, student_id: int) -> list[dict]:
    items = _schedule(tx, student_id)
    if not items:
        return []
    day = today()
    due = sorted([i for i in items if i["due_on"] <= day], key=lambda i: (i["due_on"], i["level"], i["box"]))
    rest = sorted([i for i in items if i["due_on"] > day], key=lambda i: (i["level"], i["box"], i["due_on"]))
    topics = (due + rest)[:SIZE]
    rnd = random.Random(int(hashlib.sha256(f"{student_id}:{day.isoformat()}".encode()).hexdigest()[:8], 16))
    picked, k, pools = [], 0, {}
    while len(picked) < SIZE and topics and k < SIZE * 3:  # fewer than five topics: a second question on one
        t = topics[k % len(topics)]
        if t["topic_id"] not in pools:
            pools[t["topic_id"]] = _questions(tx, t["topic_id"], rnd)
        used = {(p["prompt"], str(p["options"])) for p in picked}
        q = next((q for q in pools[t["topic_id"]] if (q["prompt"], str(q["options"])) not in used), None)
        if q:
            picked.append({"topic_id": t["topic_id"], "topic": t["title"], **q})
        k += 1
    return picked


def today_review(tx: Tx, actor: Actor) -> dict[str, Any] | None:
    """Today's Daily 5 (made on first opening and kept for the day), or None when there's nothing to review yet."""
    require(actor, "student")
    row = tx.one("SELECT * FROM daily_reviews WHERE student_id=%s AND day=%s", [actor.user_id, today()])
    if not row:
        items = _build(tx, actor.user_id)
        if not items:
            return None
        row = tx.one("""INSERT INTO daily_reviews(institution_id, student_id, day, items) VALUES (%s,%s,%s,%s)
                        ON CONFLICT (student_id, day) DO UPDATE SET day=EXCLUDED.day RETURNING *""",
                     [tx.inst, actor.user_id, today(), jsonb(items)])
    return row


def status(tx: Tx, actor: Actor) -> dict[str, Any] | None:
    """For Home: whether today's Daily 5 is waiting or done, and the streak. None when nothing has been started yet
    (no topics to review). Cheap: it doesn't build today's questions."""
    row = tx.one("SELECT done_at, score FROM daily_reviews WHERE student_id=%s AND day=%s", [actor.user_id, today()])
    if not row and not tx.val("SELECT 1 FROM learn_events WHERE student_id=%s LIMIT 1", [actor.user_id]):
        return None
    return {"done": bool(row and row["done_at"]), "score": float(row["score"]) if row and row["score"] is not None else None,
            "streak": streak(tx, actor.user_id)}


def streak(tx: Tx, student_id: int) -> int:
    """Days in a row with a finished Daily 5, up to today (or yesterday, if today's isn't done yet)."""
    days = [r["day"] for r in tx.all("""SELECT day FROM daily_reviews WHERE student_id=%s AND done_at IS NOT NULL
                                         ORDER BY day DESC LIMIT 400""", [student_id])]
    n, expect = 0, today()
    if days and days[0] != expect:
        expect -= timedelta(days=1)
    for d in days:
        if d != expect:
            break
        n += 1
        expect -= timedelta(days=1)
    return n


def submit(tx: Tx, actor: Actor, answers: dict[int, Any]) -> dict[str, Any]:
    """Mark today's Daily 5 once: move each topic's review box (right: later; wrong: tomorrow), record the review as
    learning activity, and return the results with the right answers."""
    require(actor, "student")
    row = tx.one("SELECT * FROM daily_reviews WHERE student_id=%s AND day=%s", [actor.user_id, today()])
    if not row:
        raise NotFound("Open today's Daily 5 first.")
    if row["done_at"]:
        raise Conflict("You've done today's Daily 5. A new one comes tomorrow.")
    results, right = [], 0
    for i, q in enumerate(row["items"]):
        raw = answers.get(i)
        ok = raw not in (None, "") and str(raw).isdigit() and int(raw) == int(q["answer"])
        right += ok
        results.append({"topic": q["topic"], "topic_id": q["topic_id"], "prompt": q["prompt"], "correct": ok,
                        "answer": q["options"][int(q["answer"])], "why": q.get("why", "")})
        box = tx.val("SELECT box FROM review_items WHERE student_id=%s AND topic_id=%s", [actor.user_id, q["topic_id"]]) or 0
        box = min(box + 1, len(INTERVALS) - 1) if ok else 0
        tx.exec("""INSERT INTO review_items(institution_id, student_id, topic_id, box, due_on, last_at) VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (student_id, topic_id) DO UPDATE SET box=EXCLUDED.box, due_on=EXCLUDED.due_on, last_at=EXCLUDED.last_at""",
                [tx.inst, actor.user_id, q["topic_id"], box, today() + timedelta(days=INTERVALS[box] if ok else 1), clock.now()])
        mastery.record(tx, actor.user_id, q["topic_id"], "review", 1.0 if ok else 0.0, {"daily": row["id"]})
    score = right / len(results) if results else 0.0
    tx.exec("UPDATE daily_reviews SET answers=%s, score=%s, done_at=%s WHERE id=%s",
            [jsonb({str(k): v for k, v in answers.items()}), score, clock.now(), row["id"]])
    return {"results": results, "right": right, "total": len(results), "streak": streak(tx, actor.user_id)}
