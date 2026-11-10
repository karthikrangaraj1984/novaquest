"""Students doing assignments: start/resume, autosave, tutor hints, submit and AI marking (SX-1..SX-3, NR-1, NR-4)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from novaquest.ai import guard
from novaquest.ai.types import TutorReply
from novaquest.core import audit, clock
from novaquest.core.db import Db, Tx, jsonb
from novaquest.core.errors import Conflict, Forbidden, NotFound, ValidationError
from novaquest.services import assignments, jobs, language, maths, mastery, notify
from novaquest.services.access import Actor, require
from novaquest.services.context import topic_ctx
from novaquest.services.questions import assignment_questions

MAX_ANSWER_CHARS = 4000
SELF_MARKED = ("learn", "teach", "escape")


def start_or_resume(tx: Tx, actor: Actor, assignment_id: int) -> dict[str, Any]:
    """Open the student's current attempt, or start one if the window and attempt limit allow (SX-1, SC-3)."""
    require(actor, "student")
    a = assignments.get(tx, assignment_id)
    if a["state"] == "draft" or not assignments.is_target(tx, assignment_id, actor.user_id):
        raise NotFound("That assignment was not found.")
    now = clock.now()
    expiry = assignments.student_expiry(tx, a, actor.user_id)
    current = tx.one("""SELECT * FROM attempts WHERE assignment_id=%s AND student_id=%s ORDER BY attempt_no DESC LIMIT 1""",
                     [assignment_id, actor.user_id])
    if current and current["status"] == "in_progress":
        if now < current["deadline_at"]:
            return current
    if now < a["opens_at"]:
        raise Forbidden("This assignment hasn't opened yet.")
    if now >= expiry:
        raise Forbidden("This assignment has closed.")
    used = tx.val("SELECT count(*) FROM attempts WHERE assignment_id=%s AND student_id=%s", [assignment_id, actor.user_id])
    if used >= a["max_attempts"]:
        raise Conflict("You have used all your attempts for this assignment.")
    deadline = expiry if not a["time_limit_min"] else min(expiry, now + timedelta(minutes=a["time_limit_min"]))
    row = tx.one("""INSERT INTO attempts(institution_id, assignment_id, student_id, attempt_no, started_at, deadline_at)
                    VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""", [tx.inst, assignment_id, actor.user_id, used + 1, now, deadline])
    if a["time_limit_min"]:
        jobs.enqueue(tx, "attempt_deadline", {"attempt_id": row["id"]}, run_at=deadline, dedupe_key=f"deadline:{row['id']}")
    return row


def _attempt(tx: Tx, actor: Actor, attempt_id: int, *, writable: bool) -> dict:
    row = tx.one("SELECT * FROM attempts WHERE id=%s", [attempt_id])
    if not row or row["student_id"] != actor.user_id:
        raise NotFound("That attempt was not found.")
    if writable and (row["status"] != "in_progress" or clock.now() >= row["deadline_at"]):
        raise Conflict("Time is up for this attempt, so answers can't change any more.")
    return row


def view(tx: Tx, actor: Actor, attempt_id: int) -> dict[str, Any]:
    """Everything the student's assignment page needs: questions (without answers) and saved work (SX-3)."""
    att = _attempt(tx, actor, attempt_id, writable=False)
    a = assignments.get(tx, att["assignment_id"])
    saved = {r["question_id"]: r["answer"] for r in tx.all("SELECT question_id, answer FROM responses WHERE attempt_id=%s", [attempt_id])}
    qs = []
    for q in assignment_questions(tx, att["assignment_id"]):
        if q["status"] != "approved":
            continue
        q = maths.for_student(q, actor.user_id)  # a number problem: this student's own numbers
        qs.append({"id": q["id"], "ord": q["ord"], "kind": q["kind"], "prompt": q["prompt"], "scenario": q["scenario"],
                   "unit": (q["answer"] or {}).get("unit") if q["kind"] == "numeric" else None,
                   "options": q["options"], "topic_id": q["topic_id"], "topic_title": q["topic_title"],
                   "ai_concept": q["ai_concept"], "saved": saved.get(q["id"])})
    return {"attempt": att, "assignment": a, "questions": qs}


def save_answer(tx: Tx, actor: Actor, attempt_id: int, question_id: int, answer: dict[str, Any]) -> str:
    """Autosave one answer; safe to call repeatedly (SX-3). Returns the saved time as ISO text."""
    require(actor, "student")
    att = _attempt(tx, actor, attempt_id, writable=True)
    if not tx.val("SELECT 1 FROM assignment_questions WHERE assignment_id=%s AND question_id=%s", [att["assignment_id"], question_id]):
        raise NotFound("That question is not part of this assignment.")
    clean: dict[str, Any] = {}
    if "index" in answer and answer["index"] not in (None, ""):
        clean["index"] = int(answer["index"])
    if "text" in answer:
        text = str(answer["text"])
        if len(text) > MAX_ANSWER_CHARS:
            raise ValidationError(f"Answers can be at most {MAX_ANSWER_CHARS} characters.")
        clean["text"] = text
    for k in ("done", "score", "session_id"):
        if k in answer:
            clean[k] = answer[k]
    now = clock.now()
    tx.exec("""INSERT INTO responses(institution_id, attempt_id, question_id, answer, saved_at) VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (attempt_id, question_id) DO UPDATE SET answer=EXCLUDED.answer, saved_at=EXCLUDED.saved_at""",
            [tx.inst, attempt_id, question_id, jsonb(clean), now])
    return now.isoformat()


def hint(db: Db, engine: Any, actor: Actor, attempt_id: int, question_id: int, student_text: str = "") -> TutorReply:
    """A graded hint from the AI tutor that never gives the answer (SX-2). Every exchange is logged."""
    require(actor, "student")
    with db.tenant(actor.institution_id) as tx:
        _attempt(tx, actor, attempt_id, writable=True)
        q = tx.one("SELECT * FROM questions WHERE id=%s", [question_id])
        if not q:
            raise NotFound("That question was not found.")
        level = 1 + int(tx.val("""SELECT count(*) FROM tutor_messages WHERE attempt_id=%s AND question_id=%s AND role='tutor'""",
                               [attempt_id, question_id]) or 0)
        ctx = topic_ctx(tx, q["topic_id"])
    check = guard.screen_input(student_text) if student_text.strip() else guard.InputCheck(True, "ok")
    steps = None
    if q["kind"] == "numeric":
        steps = (maths.for_student(q, actor.user_id)["answer"] or {}).get("steps") or [
            "Write down the numbers the question gives you, with their units.",
            "Which formula or rule from this topic links them to what you need to find?"]
    if check.ok and steps:  # a number problem: its own method hints, one step at a time, with the student's numbers
        n = min(level, len(steps))
        reply = TutorReply(f"Step {n} of {len(steps)}: {steps[n - 1]}" + ("" if n < len(steps) else " Now work it out."))
    else:
        reply = TutorReply(check.reply or "") if not check.ok else engine.tutor_hint(q, ctx, min(level, 3), student_text)
    secrets = [str(v) for v in (q.get("answer") or {}).values() if isinstance(v, str) and v and q["kind"] != "numeric"]
    if check.ok and guard.leaks_answer(reply.text, secrets):  # last line of defence, whichever engine wrote the hint
        reply = TutorReply(f"Think about what the textbook says about {ctx.title.lower()}. Which idea fits the question?",
                           reply.citations)
    with db.tenant(actor.institution_id) as tx:
        now = clock.now()
        tx.exec("""INSERT INTO tutor_messages(institution_id, student_id, context, attempt_id, question_id, topic_id, role, body, created_at)
                   VALUES (%s,%s,'assignment',%s,%s,%s,'student',%s,%s)""",
                [tx.inst, actor.user_id, attempt_id, question_id, q["topic_id"], student_text.strip() or "(asked for a hint)", now])
        tx.exec("""INSERT INTO tutor_messages(institution_id, student_id, context, attempt_id, question_id, topic_id, role, body,
                   hint_level, citation, created_at) VALUES (%s,%s,'assignment',%s,%s,%s,'tutor',%s,%s,%s,%s)""",
                [tx.inst, actor.user_id, attempt_id, question_id, q["topic_id"], reply.text, min(level, 3),
                 jsonb(list(reply.citations)), now + timedelta(microseconds=1)])
        lang = language.student_lang(tx, actor.user_id)
    if lang != "en":  # shown in the student's language; kept in English underneath
        reply = TutorReply(language.localise(db, engine, actor.institution_id, [reply.text], lang)[0], reply.citations)
    return reply


def translate_question(db: Db, engine: Any, actor: Actor, attempt_id: int, question_id: int) -> dict[str, Any]:
    """One question (with this student's own numbers) in the student's language: {'prompt', 'options'}."""
    require(actor, "student")
    with db.tenant(actor.institution_id) as tx:
        q = next((x for x in view(tx, actor, attempt_id)["questions"] if x["id"] == question_id), None)
        if not q:
            raise NotFound("That question is not part of this assignment.")
        lang = language.student_lang(tx, actor.user_id)
    texts = [q["prompt"]] + list(q["options"] or [])
    out = language.localise(db, engine, actor.institution_id, texts, lang)
    return {"prompt": out[0], "options": out[1:], "translated": out != texts}


def submit(db: Db, engine: Any, actor: Actor | None, attempt_id: int, *, institution_id: int | None = None,
           auto: bool = False) -> dict[str, Any]:
    """Hand in an attempt: mark every answer, update mastery, and tell the teacher (SX-2, NR-1, NR-4)."""
    inst = institution_id or (actor.institution_id if actor else None)
    with db.tenant(inst) as tx:
        att = tx.one("SELECT * FROM attempts WHERE id=%s FOR UPDATE", [attempt_id])
        if not att or (actor and att["student_id"] != actor.user_id):
            raise NotFound("That attempt was not found.")
        if att["status"] != "in_progress":
            return {"status": att["status"], "already": True}
        if actor and clock.now() >= att["deadline_at"] + timedelta(minutes=2):
            raise Conflict("Time is up for this attempt.")
        qs = [q for q in assignment_questions(tx, att["assignment_id"]) if q["status"] == "approved"]
        saved = {r["question_id"]: r["answer"] for r in tx.all("SELECT question_id, answer FROM responses WHERE attempt_id=%s", [attempt_id])}
        mis = {r["code"]: r["data"] for r in tx.all("SELECT code, data FROM misconceptions")}
        tx.exec("UPDATE attempts SET status='submitted', submitted_at=%s WHERE id=%s", [clock.now(), attempt_id])
    marks = []
    for q in qs:  # AI marking happens outside any transaction
        ans = saved.get(q["id"])
        if q["kind"] in SELF_MARKED:
            score = float((ans or {}).get("score") or 0.0)
            marks.append((q, ans, score, 1.0, {"learn": "Marked by the lesson check.", "escape": "Marked by the escape room: the share of its locks opened."}.get(q["kind"], "Marked by the teaching session."),
                          1.0, ()))
            continue
        related = [mis[c] for c in (q["misconception_codes"] or []) if c in mis] or list(mis.values())
        m = engine.mark_answer(maths.for_student(q, att["student_id"]), ans, related)
        marks.append((q, ans, m.score, m.max_score, m.rationale, m.confidence, m.misconception_codes))
    with db.tenant(inst) as tx:
        for q, ans, score, max_score, why, conf, codes in marks:
            tx.exec("""INSERT INTO responses(institution_id, attempt_id, question_id, answer, saved_at, ai_score, max_score,
                       ai_rationale, confidence, flagged, misconception_codes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (attempt_id, question_id) DO UPDATE SET ai_score=EXCLUDED.ai_score, max_score=EXCLUDED.max_score,
                       ai_rationale=EXCLUDED.ai_rationale, confidence=EXCLUDED.confidence, flagged=EXCLUDED.flagged,
                       misconception_codes=EXCLUDED.misconception_codes""",
                    [tx.inst, attempt_id, q["id"], jsonb(ans), clock.now(), score, max_score, why, conf, conf < 0.7, list(codes)])
        update_topic_mastery(tx, attempt_id)
        _notify_completion(tx, att["assignment_id"])
        audit.record(tx, "attempt.submit", actor_id=att["student_id"], target=f"attempt:{attempt_id}", detail={"auto": auto})
    return {"status": "submitted", "flagged": sum(1 for m in marks if m[5] < 0.7)}


def response_score(r: dict) -> float:
    """The score that counts: the teacher's override if any, else the AI's."""
    return float(r["teacher_score"] if r["teacher_score"] is not None else (r["ai_score"] or 0))


def update_topic_mastery(tx: Tx, attempt_id: int) -> None:
    """Turn an attempt's per-topic results into mastery evidence (replacing earlier marking of the same attempt)."""
    att = tx.one("SELECT student_id FROM attempts WHERE id=%s", [attempt_id])
    rows = tx.all("""SELECT q.topic_id, q.kind, r.ai_score, r.teacher_score, r.max_score FROM responses r
                     JOIN questions q ON q.id=r.question_id WHERE r.attempt_id=%s AND r.max_score IS NOT NULL""", [attempt_id])
    per: dict[int, list[float]] = {}
    for r in rows:
        if r["kind"] in SELF_MARKED:
            continue  # lessons and teaching sessions record their own evidence
        got, mx = per.setdefault(r["topic_id"], [0.0, 0.0])
        per[r["topic_id"]] = [got + response_score(r), mx + float(r["max_score"])]
    for topic_id, (got, mx) in per.items():
        mastery.record(tx, att["student_id"], topic_id, "assignment", round(got / mx, 3) if mx else 0.0,
                       {"attempt_id": attempt_id}, replace_key=f"attempt:{attempt_id}")


def _notify_completion(tx: Tx, assignment_id: int) -> None:
    a = assignments.get(tx, assignment_id)
    done = tx.val("SELECT count(DISTINCT student_id) FROM attempts WHERE assignment_id=%s AND status='submitted'", [assignment_id])
    total = len(assignments.target_students(tx, assignment_id))
    notify.notify(tx, a["teacher_id"], "completion", f"{a['title']} · {a['class_label']}",
                  f"{done} of {total} students completed.", f"/teacher/assignments/{assignment_id}/review",
                  f"completion:{assignment_id}")
    bucket = clock.now().strftime("%Y%m%d%H")
    jobs.enqueue(tx, "completion_digest", {"assignment_id": assignment_id}, run_at=clock.now() + timedelta(minutes=30),
                 dedupe_key=f"digest:{assignment_id}:{bucket}")


def expire_overdue(db: Db, engine_for: Any) -> int:
    """Hand in attempts whose time ran out, so no work is lost (runs in the worker)."""
    with db.platform() as tx:
        rows = tx.all("""SELECT id, institution_id FROM attempts WHERE status='in_progress' AND deadline_at <= %s LIMIT 200""",
                      [clock.now()])
    for r in rows:
        submit(db, engine_for(r["institution_id"]), None, r["id"], institution_id=r["institution_id"], auto=True)
    return len(rows)
