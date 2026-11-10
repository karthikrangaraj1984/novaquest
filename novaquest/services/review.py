"""Teacher review of results: flagged-first queue, overrides, feedback, release, and the student
scorecard shown only after release (NR-2..NR-5, SX-5, DB-5)."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from novaquest.core import audit, clock
from novaquest.core.db import Tx
from novaquest.core.errors import Conflict, NotFound, ValidationError
from novaquest.services import assignments, maths, notify
from novaquest.services.access import Actor, require, require_class
from novaquest.services.attempts import response_score, update_topic_mastery


def _assignment(tx: Tx, actor: Actor, assignment_id: int, *, write: bool) -> dict:
    a = assignments.get(tx, assignment_id)
    require_class(tx, actor, a["class_id"], write=write)
    return a


def queue(tx: Tx, actor: Actor, assignment_id: int) -> dict[str, Any]:
    """Every answer to an assignment, with answers the AI was unsure about first (NR-3, NR-4)."""
    require(actor, "teacher", "leader", "inst_admin")
    a = _assignment(tx, actor, assignment_id, write=False)
    rows = tx.all("""SELECT r.id, r.answer, r.ai_score, r.max_score, r.ai_rationale, r.confidence, r.flagged, r.misconception_codes,
            r.teacher_score, r.teacher_comment, r.reviewed_at, q.id AS question_id, q.kind, q.prompt, q.options, q.answer AS key,
            q.rubric, q.ai_concept, q.citation, aq.ord, u.id AS student_id, u.name AS student_name, e.roll_no, t.id AS attempt_id
        FROM responses r JOIN attempts t ON t.id=r.attempt_id JOIN questions q ON q.id=r.question_id
        JOIN assignment_questions aq ON aq.question_id=q.id AND aq.assignment_id=t.assignment_id
        JOIN users u ON u.id=t.student_id LEFT JOIN enrolments e ON e.student_id=u.id AND e.status='active'
        WHERE t.assignment_id=%s AND t.status='submitted' AND r.max_score IS NOT NULL
        ORDER BY (r.flagged AND r.reviewed_at IS NULL) DESC, r.flagged DESC, aq.ord, length(e.roll_no), e.roll_no""", [assignment_id])
    to_check = [r for r in rows if r["flagged"] and r["reviewed_at"] is None]
    return {"assignment": a, "responses": rows, "to_check": len(to_check),
            "checked": sum(1 for r in rows if r["flagged"] and r["reviewed_at"] is not None),
            "flagged_total": sum(1 for r in rows if r["flagged"]), "summary": class_summary(tx, assignment_id),
            "misconceptions": misconception_counts(tx, assignment_id)}


def override(tx: Tx, actor: Actor, response_id: int, *, score: float | None, comment: str = "") -> None:
    """Confirm or change the AI's score and add a note; the student's progress updates at once (NR-3)."""
    require(actor, "teacher")
    r = tx.one("""SELECT r.*, t.assignment_id, t.student_id FROM responses r JOIN attempts t ON t.id=r.attempt_id WHERE r.id=%s""",
               [response_id])
    if not r:
        raise NotFound("That answer was not found.")
    _assignment(tx, actor, r["assignment_id"], write=True)
    if score is not None and not 0 <= score <= float(r["max_score"]):
        raise ValidationError(f"Score must be between 0 and {r['max_score']:g}.")
    final = float(r["ai_score"] or 0) if score is None else score
    tx.exec("""UPDATE responses SET teacher_score=%s, teacher_comment=%s, reviewed_by=%s, reviewed_at=%s WHERE id=%s""",
            [final, comment.strip()[:1000] or None, actor.user_id, clock.now(), response_id])
    update_topic_mastery(tx, r["attempt_id"])
    audit.record(tx, "response.override", actor_id=actor.user_id, target=f"response:{response_id}",
                 detail={"ai_score": float(r["ai_score"] or 0), "teacher_score": final})


def add_feedback(tx: Tx, actor: Actor, attempt_id: int, text: str) -> None:
    """Overall feedback to one student on an attempt (NR-5)."""
    require(actor, "teacher")
    att = tx.one("SELECT assignment_id FROM attempts WHERE id=%s", [attempt_id])
    if not att:
        raise NotFound("That attempt was not found.")
    a = _assignment(tx, actor, att["assignment_id"], write=True)
    text = text.strip()[:MESSAGE_MAX]
    before = tx.one("SELECT student_id, feedback FROM attempts WHERE id=%s", [attempt_id])
    tx.exec("UPDATE attempts SET feedback=%s WHERE id=%s", [text or None, attempt_id])
    if a["released_at"] and text and text != (before["feedback"] or ""):  # results are out: tell the student now
        notify.notify(tx, before["student_id"], "feedback", f"{actor.name} left you a note",
                      f"On {a['title']}: {_preview(text)}", f"/student/results/{a['id']}", f"feedback:{attempt_id}")


MESSAGE_MAX = 1000  # characters in a teacher's note to a student or the class


def _preview(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def set_class_message(tx: Tx, actor: Actor, assignment_id: int, text: str) -> None:
    """The teacher's message to the whole class about an assignment: shown at the top of every student's scorecard.
    Changed after the results are out, it reaches each student's bell again."""
    require(actor, "teacher")
    a = _assignment(tx, actor, assignment_id, write=True)
    text = text.strip()[:MESSAGE_MAX]
    tx.exec("UPDATE assignments SET class_message=%s WHERE id=%s", [text or None, assignment_id])
    if a["released_at"] and text and text != (a.get("class_message") or ""):
        for s in tx.all("SELECT DISTINCT student_id FROM attempts WHERE assignment_id=%s AND status='submitted'", [assignment_id]):
            notify.notify(tx, s["student_id"], "feedback", f"Message from {actor.name}", f"On {a['title']}: {_preview(text)}",
                          f"/student/results/{assignment_id}", f"classmsg:{assignment_id}")


def release(tx: Tx, actor: Actor, assignment_id: int, message: str = "") -> int:
    """Release results to students once flagged answers are checked (NR-5), with an optional message to the class.
    Returns students notified."""
    require(actor, "teacher")
    a = _assignment(tx, actor, assignment_id, write=True)
    if a["released_at"]:
        raise Conflict("Results are already released.")
    if a["live_state"] not in ("open", "closed"):
        raise Conflict("Results can be released once the assignment has opened.")
    pending = tx.val("""SELECT count(*) FROM responses r JOIN attempts t ON t.id=r.attempt_id
                        WHERE t.assignment_id=%s AND r.flagged AND r.reviewed_at IS NULL""", [assignment_id])
    if pending:
        raise ValidationError(f"Check the {pending} flagged answer{'s' if pending > 1 else ''} first.")
    message = message.strip()[:MESSAGE_MAX] or (a.get("class_message") or "")
    tx.exec("UPDATE assignments SET released_at=%s, state='reviewed', class_message=%s WHERE id=%s",
            [clock.now(), message or None, assignment_id])
    students = tx.all("""SELECT DISTINCT ON (student_id) student_id, feedback FROM attempts
                         WHERE assignment_id=%s AND status='submitted' ORDER BY student_id, attempt_no DESC""", [assignment_id])
    for s in students:
        notes = [x for x in ("a message for the class" if message else "", "a note for you" if s["feedback"] else "") if x]
        body = "Your teacher has released your scorecard" + (f", with {' and '.join(notes)}." if notes else ".")
        notify.notify(tx, s["student_id"], "results", f"Results for {a['title']}", body,
                      f"/student/results/{assignment_id}", f"results:{assignment_id}")
    audit.record(tx, "assignment.release", actor_id=actor.user_id, target=f"assignment:{assignment_id}",
                 detail={"students": len(students)})
    return len(students)


def _split_scores(rows: list[dict]) -> tuple[float | None, float | None]:
    sci = [(response_score(r), float(r["max_score"])) for r in rows if not r["ai_concept"] and r["kind"] not in ("teach",)]
    ai = [(response_score(r), float(r["max_score"])) for r in rows if r["ai_concept"]]

    def pct(xs: list[tuple[float, float]]) -> float | None:
        mx = sum(m for _, m in xs)
        return round(100 * sum(g for g, _ in xs) / mx) if mx else None
    return pct(sci), pct(ai)


def scorecard(tx: Tx, actor: Actor, assignment_id: int, student_id: int | None = None) -> dict[str, Any]:
    """Detailed scorecard: every answer vs the correct answer, grouped by concept, with AI skills
    shown separately. Students see it only after release (SX-5, DB-5)."""
    a = assignments.get(tx, assignment_id)
    if actor.role == "student":
        student_id = actor.user_id
        if not a["released_at"]:
            raise NotFound("Your teacher hasn't released these results yet.")
    else:
        require_class(tx, actor, a["class_id"])
    att = tx.one("""SELECT id, feedback, submitted_at, started_at FROM attempts WHERE assignment_id=%s AND student_id=%s
                    AND status='submitted' ORDER BY attempt_no DESC LIMIT 1""", [assignment_id, student_id])
    if not att:
        raise NotFound("No submitted work for this assignment.")
    rows = tx.all("""SELECT r.answer, r.ai_score, r.teacher_score, r.max_score, r.ai_rationale, r.teacher_comment,
            r.misconception_codes, q.id AS question_id, q.kind, q.prompt, q.options, q.answer AS key, q.rubric, q.ai_concept,
            q.citation, q.topic_id, t.title AS topic_title, aq.ord
        FROM responses r JOIN questions q ON q.id=r.question_id JOIN syllabus_nodes t ON t.id=q.topic_id
        JOIN assignment_questions aq ON aq.question_id=q.id AND aq.assignment_id=%s
        WHERE r.attempt_id=%s ORDER BY aq.ord""", [assignment_id, att["id"]])
    by_concept: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["kind"] == "numeric":  # the student's own numbers, and the worked solution with them
            mine = maths.for_student({"id": r["question_id"], "kind": "numeric", "prompt": r["prompt"], "answer": r["key"]},
                                     student_id)
            r["prompt"], r["key"] = mine["prompt"], mine["answer"]
        r["final"] = response_score(r)
        r["correct"] = r["final"] >= float(r["max_score"] or 1) - 1e-9
        by_concept[r["ai_concept"] or r["topic_title"]].append(r)
    concepts = [{"name": k, "ai": bool(v[0]["ai_concept"]), "got": sum(x["final"] for x in v),
                 "max": sum(float(x["max_score"] or 0) for x in v), "topic_id": v[0]["topic_id"]} for k, v in by_concept.items()]
    for c in concepts:
        c["pct"] = round(100 * c["got"] / c["max"]) if c["max"] else 0
    revise = [c for c in concepts if not c["ai"] and c["pct"] < 60]
    sci, ai = _split_scores(rows)
    names = {r["code"]: r["label"] for r in tx.all("SELECT code, label FROM misconceptions")}
    mixups = sorted({names.get(c, c) for r in rows for c in (r["misconception_codes"] or [])})
    minutes = None
    if att["submitted_at"] and att["started_at"]:
        minutes = max(1, int((att["submitted_at"] - att["started_at"]).total_seconds() // 60))
    teacher = tx.val("SELECT name FROM users WHERE id=%s", [a["teacher_id"]])
    return {"assignment": a, "rows": rows, "concepts": concepts, "science": sci, "ai": ai, "minutes": minutes,
            "feedback": att["feedback"], "class_message": a.get("class_message"), "attempt_id": att["id"], "mixups": mixups, "teacher_name": teacher,
            "big_ideas": (sum(1 for c in concepts if c["max"] and c["got"] / c["max"] >= 0.6), len(concepts)),
            "revise": revise, "headline": _headline(sum(1 for r in rows if r["correct"]), len(rows)),
            "right": sum(1 for r in rows if r["correct"]), "total": len(rows)}


def _headline(right: int, total: int) -> tuple[str, str]:
    """An honest, kind summary of a result for a student: (headline, encouragement). Never praise for a low score."""
    if not total:
        return "Results", ""
    share = right / total
    if share >= 0.85:
        return f"Excellent! {right} of {total} right.", "You really understand this. Try Teach NOVA to lock it in."
    if share >= 0.6:
        return f"Good work: {right} of {total} right.", "Look at the ones you missed below: each shows the right answer."
    if share >= 0.3:
        return f"You got {right} of {total} right.", "You're getting there. Revise the topics below, then try again."
    return f"This one was tricky: {right} of {total} right.", "That's OK. Let's go through the topic again together."


def class_summary(tx: Tx, assignment_id: int) -> dict[str, Any]:
    """Completion and average progress for an assignment (DB-1, NR-2)."""
    targets = assignments.target_students(tx, assignment_id)
    rows = tx.all("""SELECT t.student_id, r.ai_score, r.teacher_score, r.max_score, q.ai_concept, q.kind FROM responses r
        JOIN attempts t ON t.id=r.attempt_id JOIN questions q ON q.id=r.question_id
        WHERE t.assignment_id=%s AND t.status='submitted' AND r.max_score IS NOT NULL""", [assignment_id])
    sci, ai = _split_scores(rows)
    done_ids = {r["student_id"] for r in tx.all("SELECT DISTINCT student_id FROM attempts WHERE assignment_id=%s AND status='submitted'",
                                               [assignment_id])}
    missing = [s for s in targets if s["id"] not in done_ids]
    has_ai = bool(tx.val("""SELECT EXISTS (SELECT 1 FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id
                             WHERE aq.assignment_id=%s AND coalesce(q.ai_concept, '') <> '')""", [assignment_id]))
    return {"total": len(targets), "submitted": len(done_ids), "science": sci, "ai": ai, "has_ai": has_ai, "missing": missing,
            "completion": round(100 * len(done_ids) / len(targets)) if targets else 0}


def misconception_counts(tx: Tx, assignment_id: int) -> list[dict]:
    """How many students showed each reviewed misconception (DB-2)."""
    return tx.all("""SELECT m.code, m.label, count(DISTINCT t.student_id) AS students FROM responses r
        JOIN attempts t ON t.id=r.attempt_id JOIN LATERAL unnest(r.misconception_codes) c(code) ON true
        JOIN misconceptions m ON m.code=c.code WHERE t.assignment_id=%s
        GROUP BY m.code, m.label ORDER BY students DESC, m.code LIMIT 5""", [assignment_id])


def expiry_summary(tx: Tx, assignment_id: int) -> None:
    """When an assignment closes, tell the teacher who completed and who didn't (NR-2)."""
    a = assignments.get(tx, assignment_id)
    if a["closed_notified_at"] or a["live_state"] != "closed":
        return
    s = class_summary(tx, assignment_id)
    names = ", ".join(f"{m['name']} (roll {m['roll_no']})" for m in s["missing"][:30]) or "none"
    body = f"{s['submitted']} of {s['total']} completed. Not completed: {names}."
    notify.notify(tx, a["teacher_id"], "closed", f"{a['title']} has closed", body,
                  f"/teacher/assignments/{assignment_id}/review", f"closed:{assignment_id}")
    email = tx.val("SELECT email FROM users WHERE id=%s", [a["teacher_id"]])
    notify.queue_email(tx, email, f"{a['title']} ({a['class_label']}) has closed", body + "\n\nReview and release results in NovaQuest.",
                       f"closed-email:{assignment_id}:{a['expires_at'].isoformat()}")
    tx.exec("UPDATE assignments SET closed_notified_at=%s WHERE id=%s", [clock.now(), assignment_id])


def completion_digest(tx: Tx, assignment_id: int) -> None:
    """One email per assignment per batch, e.g. '28 of 32 completed', instead of one per student (NR-1)."""
    a = assignments.get(tx, assignment_id)
    s = class_summary(tx, assignment_id)
    email = tx.val("SELECT email FROM users WHERE id=%s", [a["teacher_id"]])
    notify.queue_email(tx, email, f"{a['title']} ({a['class_label']}): {s['submitted']} of {s['total']} completed",
                       f"{s['submitted']} of {s['total']} students have completed {a['title']}.",
                       f"digest-email:{assignment_id}:{s['submitted']}")


def send_reminders(tx: Tx, assignment_id: int) -> int:
    """Remind students who haven't finished, about 24 hours before expiry, in-app and by email to parents (SX-4)."""
    a = assignments.get(tx, assignment_id)
    if a["live_state"] != "open" or a["reminder_sent_at"]:
        return 0
    done = {r["student_id"] for r in tx.all("SELECT student_id FROM attempts WHERE assignment_id=%s AND status='submitted'", [assignment_id])}
    n = 0
    for s in assignments.target_students(tx, assignment_id):
        if s["id"] in done:
            continue
        notify.notify(tx, s["id"], "reminder", f"Reminder: {a['title']}", "This closes in less than a day. Finish it soon!",
                      f"/student/assignments/{assignment_id}", f"reminder:{assignment_id}")
        notify.queue_email(tx, s["parent_email"], f"Reminder: {a['title']} closes soon",
                           f"{s['name']} has not finished “{a['title']}” yet. It closes in less than a day.",
                           f"reminder-email:{assignment_id}:{s['id']}")
        n += 1
    tx.exec("UPDATE assignments SET reminder_sent_at=%s WHERE id=%s", [clock.now(), assignment_id])
    return n


def nudge_missing(tx: Tx, actor: Actor, assignment_id: int) -> int:
    """Teacher reminds every student who hasn't handed in yet, while the assignment is open (in-app, one notice
    per student that refreshes if sent again). Returns how many students were reminded."""
    a = assignments.get(tx, assignment_id)
    require_class(tx, actor, a["class_id"], write=True)
    if a["live_state"] != "open":
        raise Conflict("You can send a reminder only while the assignment is open.")
    done = {r["student_id"] for r in tx.all("SELECT student_id FROM attempts WHERE assignment_id=%s AND status='submitted'",
                                            [assignment_id])}
    missing = [s for s in assignments.target_students(tx, assignment_id) if s["id"] not in done]
    for s in missing:
        notify.notify(tx, s["id"], "reminder", f"Your teacher is waiting for: {a['title']}",
                      f"Please finish it before it closes ({clock.humanize_left(a['expires_at'])} left).",
                      f"/student/assignments/{assignment_id}", f"nudge:{assignment_id}")
    audit.record(tx, "assignment.nudge", actor_id=actor.user_id, target=f"assignment:{assignment_id}",
                 detail={"students": len(missing)})
    return len(missing)


# ---------- question by question (teacher review, item 10) ----------
RIGHT_AT = 0.6  # a written answer at or above this share of the marks counts as right


def question_breakdown(tx: Tx, actor: Actor, assignment_id: int) -> dict[str, Any]:
    """How the class did on each question: share right, which wrong option was most chosen, who got it wrong and what
    they mixed up, plus the questions worth teaching again (lowest share right first). One query for all answers."""
    require(actor, "teacher", "leader", "inst_admin")
    a = _assignment(tx, actor, assignment_id, write=False)
    from novaquest.services.questions import assignment_questions

    qs = [q for q in assignment_questions(tx, assignment_id) if q["status"] == "approved" and q["kind"] not in ("learn", "teach")]
    rows = tx.all("""SELECT r.question_id, r.answer, r.ai_score, r.teacher_score, r.max_score, r.misconception_codes,
            u.id AS student_id, u.name, e.roll_no
        FROM responses r JOIN attempts t ON t.id=r.attempt_id JOIN users u ON u.id=t.student_id
        LEFT JOIN enrolments e ON e.student_id=u.id AND e.status='active'
        WHERE t.assignment_id=%s AND t.status='submitted' AND r.max_score IS NOT NULL
        ORDER BY length(e.roll_no), e.roll_no""", [assignment_id])
    labels = {m["code"]: m["label"] for m in tx.all("SELECT code, label FROM misconceptions")}
    by_q: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_q[r["question_id"]].append(r)
    out = []
    for q in qs:
        answers = by_q.get(q["id"], [])
        share = [response_score(r) / float(r["max_score"]) if float(r["max_score"]) else 0.0 for r in answers]
        wrong = [(r, s) for r, s in zip(answers, share) if s < (1.0 if q["options"] else RIGHT_AT)]
        options = None
        if q["options"]:
            counts = [0] * len(q["options"])
            for r in answers:
                idx = (r["answer"] or {}).get("index")
                if isinstance(idx, int) and 0 <= idx < len(counts):
                    counts[idx] += 1
            right_idx = (q["answer"] or {}).get("index")
            options = [{"text": t, "count": c, "right": i == right_idx,
                        "pct": round(100 * c / len(answers)) if answers else 0} for i, (t, c) in enumerate(zip(q["options"], counts))]
        common_wrong = max((o for o in options or [] if not o["right"] and o["count"]), key=lambda o: o["count"], default=None)
        mix = defaultdict(int)
        for r, _ in wrong:
            for code in r["misconception_codes"] or []:
                if code in labels:
                    mix[labels[code]] += 1
        out.append({"id": q["id"], "ord": q["ord"], "kind": q["kind"], "prompt": q["prompt"], "topic_id": q["topic_id"],
                    "topic_title": q["topic_title"], "answered": len(answers),
                    "pct_right": round(100 * (len(answers) - len(wrong)) / len(answers)) if answers else None,
                    "avg": round(100 * sum(share) / len(share)) if share else None, "options": options,
                    "common_wrong": common_wrong, "model": (q["answer"] or {}).get("model") or (q["answer"] or {}).get("text"),
                    "wrong": [{"id": r["student_id"], "name": r["name"], "roll_no": r["roll_no"],
                               "said": (q["options"][r["answer"]["index"]] if q["options"] and r["answer"]
                                        and isinstance(r["answer"].get("index"), int) and r["answer"]["index"] < len(q["options"])
                                        else (r["answer"] or {}).get("text"))} for r, _ in wrong],
                    "mixups": sorted(mix.items(), key=lambda kv: -kv[1])})
    answered = [q for q in out if q["answered"]]
    reteach = sorted((q for q in answered if q["pct_right"] < 50), key=lambda q: (q["pct_right"], q["ord"]))[:3]
    return {"assignment": a, "questions": out, "reteach": reteach, "submitted": len({r["student_id"] for r in rows})}
