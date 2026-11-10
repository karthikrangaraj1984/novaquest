"""Ask my teacher (1.31.0): a student who is stuck on a topic, or on a question they got wrong, writes to the
teacher who teaches them that subject. The teacher sees open doubts on Today, replies once, and the student gets the
reply in their bell and under "My questions". Teachers of the class who teach another subject don't see it."""
from __future__ import annotations

from typing import Any

from novaquest.core import audit, clock
from novaquest.core.db import Tx
from novaquest.core.errors import NotFound, ValidationError
from novaquest.services import notify
from novaquest.services.access import Actor, require, require_class, student_class_id, visible_class_ids

MAX_LEN = 600
OPEN_PER_STUDENT = 10  # at most this many unanswered at once, so it stays a conversation, not a flood


def ask(tx: Tx, actor: Actor, body: str, *, topic_id: int | None = None, question_id: int | None = None,
        assignment_id: int | None = None) -> int:
    """A student's question to their teacher, about a topic and/or a question from their work."""
    require(actor, "student")
    body = " ".join(str(body or "").split())
    if len(body) < 5:
        raise ValidationError("Write your question (a few words at least).")
    if len(body) > MAX_LEN:
        raise ValidationError(f"Keep it under {MAX_LEN} characters.")
    class_id = student_class_id(tx, actor.user_id)
    if class_id is None:
        raise ValidationError("You aren't in a class yet.")
    if tx.val("SELECT count(*) FROM doubts WHERE student_id=%s AND status='open'", [actor.user_id]) >= OPEN_PER_STUDENT:
        raise ValidationError(f"You have {OPEN_PER_STUDENT} questions waiting for a reply. Wait for your teacher to answer some first.")
    if question_id is not None:
        q = tx.one("""SELECT q.topic_id, a.id AS assignment_id FROM questions q JOIN assignment_questions aq ON aq.question_id=q.id
                      JOIN assignments a ON a.id=aq.assignment_id JOIN attempts t ON t.assignment_id=a.id AND t.student_id=%s
                      WHERE q.id=%s LIMIT 1""", [actor.user_id, question_id])
        if not q:
            raise NotFound("That question wasn't found in your work.")
        topic_id, assignment_id = topic_id or q["topic_id"], assignment_id or q["assignment_id"]
    syllabus_id = tx.val("SELECT syllabus_id FROM syllabus_nodes WHERE id=%s", [topic_id]) if topic_id else None
    if topic_id and not syllabus_id:
        raise NotFound("That topic wasn't found.")
    did = tx.val("""INSERT INTO doubts(institution_id, student_id, class_id, syllabus_id, topic_id, question_id, assignment_id, body, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                 [tx.inst, actor.user_id, class_id, syllabus_id, topic_id, question_id, assignment_id, body, clock.now()])
    who = tx.one("SELECT u.name, c.label FROM users u JOIN classes c ON c.id=%s WHERE u.id=%s", [class_id, actor.user_id])
    for t in _teachers(tx, class_id, syllabus_id):
        notify.notify(tx, t, "doubt", f"A question from {who['name']} ({who['label']})", body[:140], "/teacher/doubts",
                      f"doubt:{did}")
    return did


def _teachers(tx: Tx, class_id: int, syllabus_id: int | None) -> list[int]:
    """Who answers: the teachers of that subject in the class (any part of a book in parts), else every teacher of the
    class."""
    rows = tx.all("""SELECT DISTINCT ta.teacher_id, ta.syllabus_id, s.subject, s.board, s.grade FROM teaching_assignments ta
                     JOIN syllabi s ON s.id=ta.syllabus_id WHERE ta.class_id=%s""", [class_id])
    if syllabus_id:
        book = tx.one("SELECT subject, board, grade FROM syllabi WHERE id=%s", [syllabus_id])
        same = [r["teacher_id"] for r in rows if r["syllabus_id"] == syllabus_id
                or (book and (r["subject"], r["board"], r["grade"]) == (book["subject"], book["board"], book["grade"]))]
        if same:
            return sorted(set(same))
    return sorted({r["teacher_id"] for r in rows})


def mine(tx: Tx, student_id: int) -> list[dict[str, Any]]:
    """A student's questions, newest first, with replies."""
    return tx.all("""SELECT d.id, d.body, d.status, d.reply, d.replied_at, d.created_at, n.title AS topic, d.topic_id,
                            u.name AS teacher, q.prompt AS question
                     FROM doubts d LEFT JOIN syllabus_nodes n ON n.id=d.topic_id LEFT JOIN users u ON u.id=d.replied_by
                     LEFT JOIN questions q ON q.id=d.question_id
                     WHERE d.student_id=%s ORDER BY d.created_at DESC LIMIT 50""", [student_id])


def for_teacher(tx: Tx, actor: Actor, status: str = "open") -> list[dict[str, Any]]:
    """Doubts from the teacher's classes in the subjects they teach (every subject for a class with no subject match)."""
    require(actor, "teacher")
    ids = visible_class_ids(tx, actor)
    rows = tx.all("""SELECT d.id, d.body, d.status, d.reply, d.replied_at, d.created_at, d.class_id, d.syllabus_id, d.topic_id,
                            d.assignment_id, c.label AS class_label, u.name AS student, e.roll_no, n.title AS topic, q.prompt AS question,
                            q.answer AS key, q.options, s.subject
                     FROM doubts d JOIN classes c ON c.id=d.class_id JOIN users u ON u.id=d.student_id
                     LEFT JOIN enrolments e ON e.student_id=d.student_id AND e.class_id=d.class_id AND e.status='active'
                     LEFT JOIN syllabus_nodes n ON n.id=d.topic_id LEFT JOIN questions q ON q.id=d.question_id
                     LEFT JOIN syllabi s ON s.id=d.syllabus_id
                     WHERE d.class_id = ANY(%s) AND d.status=%s ORDER BY d.created_at""" + ("" if status == "open" else " DESC LIMIT 40"),
                  [ids, status])
    return [r for r in rows if actor.user_id in _teachers(tx, r["class_id"], r["syllabus_id"])]


def open_count(tx: Tx, actor: Actor) -> int:
    return len(for_teacher(tx, actor, "open"))


def reply(tx: Tx, actor: Actor, doubt_id: int, text: str) -> None:
    """The teacher's answer; the student is told in their bell."""
    require(actor, "teacher")
    d = tx.one("SELECT * FROM doubts WHERE id=%s", [doubt_id])
    if not d:
        raise NotFound("That question wasn't found.")
    require_class(tx, actor, d["class_id"], write=True)
    if actor.user_id not in _teachers(tx, d["class_id"], d["syllabus_id"]):
        raise NotFound("That question is for another subject's teacher.")
    text = str(text or "").strip()
    if len(text) < 2:
        raise ValidationError("Write your reply.")
    tx.exec("UPDATE doubts SET status='answered', reply=%s, replied_by=%s, replied_at=%s WHERE id=%s",
            [text[:2000], actor.user_id, clock.now(), doubt_id])
    name = tx.val("SELECT name FROM users WHERE id=%s", [actor.user_id])
    notify.notify(tx, d["student_id"], "doubt_reply", f"{name} answered your question", text[:140], "/student/questions",
                  f"doubt-reply:{doubt_id}")
    audit.record(tx, "doubt.reply", actor_id=actor.user_id, target=f"doubt:{doubt_id}")
