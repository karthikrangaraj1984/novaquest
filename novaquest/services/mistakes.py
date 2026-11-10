"""My mistakes (1.31.0): every question a student got wrong in work whose results are out, kept until they get it
right on a redo.

Multiple-choice questions are redone and marked on the spot, a few at a time. Written answers can't be marked
again without the teacher, so they show the strong answer and the student marks them "I understand it now". A redo
is practice: it never changes the original score."""
from __future__ import annotations

import random
from typing import Any

from novaquest.core import clock
from novaquest.core.db import Tx
from novaquest.core.errors import ValidationError
from novaquest.services import mastery
from novaquest.services.access import Actor, require

ROUND = 5  # questions in one redo round
REDO_KINDS = ("mcq", "ai_moment")


def _wrong(tx: Tx, student_id: int) -> list[dict[str, Any]]:
    """Every question answered wrongly (below full marks) in released work, latest attempt, newest work first."""
    return tx.all("""
        SELECT DISTINCT ON (q.id) q.id AS question_id, q.kind, q.prompt, q.options, q.answer AS key, q.topic_id,
               n.title AS topic, b.subject, a.title AS assignment, a.id AS assignment_id, a.released_at,
               r.answer, coalesce(r.teacher_score, r.ai_score) AS got, r.max_score,
               f.tries, f.fixed_at
        FROM attempts t JOIN assignments a ON a.id=t.assignment_id AND a.released_at IS NOT NULL
        JOIN responses r ON r.attempt_id=t.id JOIN questions q ON q.id=r.question_id
        JOIN syllabus_nodes n ON n.id=q.topic_id JOIN syllabi b ON b.id=n.syllabus_id
        LEFT JOIN mistake_fixes f ON f.student_id=t.student_id AND f.question_id=q.id
        WHERE t.student_id=%s AND t.status='submitted' AND r.max_score IS NOT NULL AND r.max_score > 0
          AND coalesce(r.teacher_score, r.ai_score, 0) < r.max_score AND q.kind NOT IN ('learn', 'teach', 'escape')
        ORDER BY q.id, t.submitted_at DESC""", [student_id])


def overview(tx: Tx, student_id: int) -> dict[str, Any]:
    """Mistakes still to fix (by subject and topic) and how many are fixed."""
    rows = _wrong(tx, student_id)
    todo = [r for r in rows if not r["fixed_at"]]
    todo.sort(key=lambda r: (r["subject"], r["topic"], -(r["released_at"].timestamp() if r["released_at"] else 0)))
    groups: dict[str, dict[str, Any]] = {}
    for r in todo:
        g = groups.setdefault(f"{r['subject']}·{r['topic_id']}", {"subject": r["subject"], "topic": r["topic"],
                                                                     "topic_id": r["topic_id"], "items": []})
        g["items"].append(r)
    return {"todo": len(todo), "fixed": len(rows) - len(todo), "redo": sum(1 for r in todo if r["kind"] in REDO_KINDS and r["options"]),
            "groups": list(groups.values())}


def count(tx: Tx, student_id: int) -> int:
    return sum(1 for r in _wrong(tx, student_id) if not r["fixed_at"])


def redo_round(tx: Tx, student_id: int, topic_id: int | None = None) -> list[dict[str, Any]]:
    """Up to five unfixed multiple-choice mistakes to redo (fewest tries first), options in a fresh order."""
    rows = [r for r in _wrong(tx, student_id) if not r["fixed_at"] and r["kind"] in REDO_KINDS and r["options"]
            and (topic_id is None or r["topic_id"] == topic_id)]
    rows.sort(key=lambda r: (r["tries"] or 0, r["question_id"]))
    rnd = random.Random(f"{student_id}:{clock.now().date().isoformat()}")
    out = []
    for r in rows[:ROUND]:
        order = list(range(len(r["options"])))
        rnd.shuffle(order)
        out.append({"question_id": r["question_id"], "prompt": r["prompt"], "topic": r["topic"],
                    "options": [r["options"][i] for i in order], "order": order})
    return out


def submit_round(tx: Tx, actor: Actor, answers: dict[int, Any], orders: dict[int, list[int]]) -> dict[str, Any]:
    """Mark a redo round: right answers are fixed; a wrong one stays, with the right answer shown. Each redo counts as learning activity (like Daily 5)
    and never changes the original score or raises the topic's level on its own."""
    require(actor, "student")
    wrong = {r["question_id"]: r for r in _wrong(tx, actor.user_id) if not r["fixed_at"]}
    results, right = [], 0
    for qid, raw in answers.items():
        r = wrong.get(qid)
        order = orders.get(qid)
        if not r or r["kind"] not in REDO_KINDS or not order:
            continue
        picked = order[int(raw)] if str(raw).isdigit() and int(raw) < len(order) else None
        ok = picked is not None and picked == int(r["key"]["index"])
        right += ok
        tx.exec("""INSERT INTO mistake_fixes(institution_id, student_id, question_id, tries, fixed_at, last_at) VALUES (%s,%s,%s,1,%s,%s)
                   ON CONFLICT (student_id, question_id) DO UPDATE SET tries=mistake_fixes.tries + 1,
                   fixed_at=coalesce(mistake_fixes.fixed_at, EXCLUDED.fixed_at), last_at=EXCLUDED.last_at""",
                [tx.inst, actor.user_id, qid, clock.now() if ok else None, clock.now()])
        mastery.record(tx, actor.user_id, r["topic_id"], "review", 1.0 if ok else 0.0, {"mistake": qid})
        results.append({"prompt": r["prompt"], "correct": ok, "answer": r["options"][int(r["key"]["index"])], "topic": r["topic"],
                        "topic_id": r["topic_id"]})
    if not results:
        raise ValidationError("Answer at least one question.")
    return {"results": results, "right": right, "total": len(results), "left": count(tx, actor.user_id)}


def understood(tx: Tx, actor: Actor, question_id: int) -> None:
    """A written-answer mistake the student has read the strong answer for and understands now."""
    require(actor, "student")
    r = next((r for r in _wrong(tx, actor.user_id) if r["question_id"] == question_id), None)
    if not r or r["kind"] in REDO_KINDS:
        raise ValidationError("That isn't one of your written-answer mistakes.")
    tx.exec("""INSERT INTO mistake_fixes(institution_id, student_id, question_id, tries, fixed_at, last_at) VALUES (%s,%s,%s,0,%s,%s)
               ON CONFLICT (student_id, question_id) DO UPDATE SET fixed_at=EXCLUDED.fixed_at, last_at=EXCLUDED.last_at""",
            [tx.inst, actor.user_id, question_id, clock.now(), clock.now()])
