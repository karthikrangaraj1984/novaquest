"""Topic mastery: not started, learning, practised, mastered (LN-5), adaptive next steps (LN-8)
and revision plans (LN-9). Mastery is recomputed from evidence, so teacher overrides apply at once."""
from __future__ import annotations

from typing import Any

from novaquest.core import clock
from novaquest.core.db import Tx, jsonb

LEVELS = ("Not started", "Learning", "Practised", "Mastered")
MASTERED_AT = 0.75
PRACTISED_AT = 0.6


def record(tx: Tx, student_id: int, topic_id: int, kind: str, score: float | None, data: dict[str, Any] | None = None,
           replace_key: str | None = None) -> int:
    """Store one piece of learning evidence and return the topic's new mastery level.

    `replace_key` replaces earlier evidence with the same key (e.g. an assignment attempt that was re-marked)."""
    data = dict(data or {})
    if replace_key:
        tx.exec("DELETE FROM learn_events WHERE student_id=%s AND topic_id=%s AND data->>'key'=%s",
                [student_id, topic_id, replace_key])
        data["key"] = replace_key
    tx.exec("""INSERT INTO learn_events(institution_id, student_id, topic_id, kind, score, data, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""", [tx.inst, student_id, topic_id, kind, score, jsonb(data), clock.now()])
    return recompute(tx, student_id, topic_id)


def recompute(tx: Tx, student_id: int, topic_id: int) -> int:
    """Derive the mastery level from the evidence for a student and topic.

    Each activity counts by its LATEST score, so a later poor result lowers the level (mastery can slip back).
    Practised: any activity at 60% or more. Mastered needs real depth: the understanding check or Teach NOVA at
    75% or more, or an assignment at 85% or more backed by a second piece of evidence (another assignment at 85%,
    or practice, a check or a lesson quiz at 60%), never one test alone."""
    rows = tx.all("""SELECT kind, score, data->>'key' AS key FROM learn_events WHERE student_id=%s AND topic_id=%s
                     ORDER BY created_at, id""", [student_id, topic_id])
    level = 0
    taught = False
    if rows:
        level = 1
        latest: dict[tuple[str, str | None], float] = {}
        for r in rows:
            if r["score"] is not None:  # later evidence of the same activity replaces earlier evidence
                latest[(r["kind"], r["key"] if r["kind"] == "assignment" else None)] = float(r["score"])
        if any(sc >= PRACTISED_AT for (k, _), sc in latest.items() if k in ("check", "practice", "assignment", "understanding", "teach")):
            level = 2
        deep = any(sc >= MASTERED_AT for (k, _), sc in latest.items() if k in ("understanding", "teach"))
        strong_tests = sum(1 for (k, _), sc in latest.items() if k == "assignment" and sc >= 0.85)
        backed = strong_tests >= 2 or (strong_tests == 1 and any(
            sc >= PRACTISED_AT for (k, _), sc in latest.items() if k in ("check", "practice", "understanding", "teach")))
        if deep or backed:
            level = 3
        taught = any(float(r["score"]) >= MASTERED_AT for r in rows if r["kind"] == "teach" and r["score"] is not None)
    tx.exec("""INSERT INTO mastery(institution_id, student_id, topic_id, level, taught_ok, updated_at) VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (student_id, topic_id) DO UPDATE SET level=EXCLUDED.level, taught_ok=EXCLUDED.taught_ok,
               updated_at=EXCLUDED.updated_at""", [tx.inst, student_id, topic_id, level, taught, clock.now()])
    return level


def for_student(tx: Tx, student_id: int) -> dict[int, dict]:
    """Mastery per topic for one student."""
    return {r["topic_id"]: r for r in tx.all("SELECT topic_id, level, taught_ok FROM mastery WHERE student_id=%s", [student_id])}


def for_class(tx: Tx, class_id: int, topic_ids: list[int] | None = None) -> list[dict]:
    """Mastery of every active student in a class, per topic (teacher view, LN-5)."""
    return tx.all("""SELECT e.student_id, u.name, e.roll_no, m.topic_id, m.level, m.taught_ok
        FROM enrolments e JOIN users u ON u.id=e.student_id
        LEFT JOIN mastery m ON m.student_id=e.student_id AND (%s::bigint[] IS NULL OR m.topic_id = ANY(%s::bigint[]))
        WHERE e.class_id=%s AND e.status='active' ORDER BY length(e.roll_no), e.roll_no""",
                  [topic_ids, topic_ids, class_id])


def next_step(tx: Tx, student_id: int, topic_id: int, last_score: float) -> dict[str, Any]:
    """Recommend revising (explained a different way) or moving on, after an understanding check (LN-8)."""
    topic = tx.one("""SELECT t.id, t.title, t.parent_id, t.ord FROM syllabus_nodes t WHERE t.id=%s""", [topic_id])
    if last_score < MASTERED_AT:
        return {"action": "revise", "topic_id": topic_id,
                "message": f"Let's look at “{topic['title']}” again, explained a different way, then try the check once more."}
    nxt = tx.one("""SELECT id, title FROM syllabus_nodes WHERE parent_id=%s AND kind='topic' AND ord > %s
                    ORDER BY ord LIMIT 1""", [topic["parent_id"], topic["ord"]])
    if nxt:
        return {"action": "next", "topic_id": nxt["id"], "message": f"Great work! Move on to “{nxt['title']}”."}
    return {"action": "teach", "topic_id": topic_id,
            "message": "You've finished this chapter's topics. Try teaching NOVA to make it stick!"}


def revision_plan(tx: Tx, student_id: int, limit: int = 8) -> list[dict]:
    """Topics to revise before exams, weakest first, from the student's own evidence (LN-9)."""
    return tx.all("""
        WITH ev AS (SELECT topic_id, avg(score) AS avg_score, count(*) AS n, max(created_at) AS last_at
                    FROM learn_events WHERE student_id=%s AND score IS NOT NULL GROUP BY topic_id)
        SELECT t.id AS topic_id, t.code, t.title, c.title AS chapter, coalesce(m.level, 0) AS level,
               round(coalesce(ev.avg_score, 0)::numeric, 2) AS avg_score, ev.last_at
        FROM ev JOIN syllabus_nodes t ON t.id=ev.topic_id JOIN syllabus_nodes c ON c.id=t.parent_id
        LEFT JOIN mastery m ON m.student_id=%s AND m.topic_id=t.id
        WHERE coalesce(m.level, 0) < 3
        ORDER BY coalesce(m.level, 0), ev.avg_score, ev.last_at LIMIT %s""", [student_id, student_id, limit])
