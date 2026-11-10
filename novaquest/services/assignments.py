"""Assigning and scheduling: targets, open/expiry windows, limits, extensions and states (SC-1..SC-5)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from novaquest.core import audit, clock
from novaquest.core.db import Tx
from novaquest.core.errors import Conflict, NotFound, ValidationError
from novaquest.services import jobs
from novaquest.services.access import Actor, require, require_class, student_class_id

STATE_LABELS = {"draft": "Draft", "scheduled": "Scheduled", "open": "Open", "closed": "Closed", "reviewed": "Results released"}


def state_at(a: dict, now: datetime | None = None) -> str:
    """The assignment's state at a moment, from its dates (the stored state can lag by a worker tick)."""
    now = now or clock.now()
    if a["state"] == "draft" or not a.get("opens_at"):
        return "draft"
    if a.get("released_at"):
        return "reviewed"
    if now < a["opens_at"]:
        return "scheduled"
    if now < a["expires_at"]:
        return "open"
    return "closed"


def get(tx: Tx, assignment_id: int) -> dict[str, Any]:
    """Load one assignment (RLS keeps it within the institution)."""
    row = tx.one("""SELECT a.*, c.label AS class_label FROM assignments a JOIN classes c ON c.id=a.class_id WHERE a.id=%s""",
                 [assignment_id])
    if not row:
        raise NotFound("That assignment was not found.")
    row["live_state"] = state_at(row)
    return row


def schedule(tx: Tx, actor: Actor, assignment_id: int, *, opens_at: datetime, expires_at: datetime,
             targets: list[tuple[str, int]], time_limit_min: int | None = None, max_attempts: int = 1) -> str:
    """Publish an approved draft to a class, group or students with an open and expiry time (SC-1..SC-3)."""
    require(actor, "teacher")
    a = get(tx, assignment_id)
    require_class(tx, actor, a["class_id"], write=True)
    if a["state"] != "draft":
        raise Conflict("This assignment is already scheduled.")
    pending = tx.val("""SELECT count(*) FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id
                        WHERE aq.assignment_id=%s AND q.status='draft'""", [assignment_id])
    live = tx.val("""SELECT count(*) FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id
                     WHERE aq.assignment_id=%s AND q.status='approved'""", [assignment_id])
    if pending:
        raise ValidationError(f"Approve, edit or remove the {pending} question{'s' if pending > 1 else ''} still waiting for your check.")
    if not live:
        raise ValidationError("Add at least one approved question.")
    if expires_at <= opens_at:
        raise ValidationError("The closing time must be after the opening time.", field="expires_at")
    if expires_at <= clock.now():
        raise ValidationError("The closing time is already in the past.", field="expires_at")
    if time_limit_min is not None and not 1 <= time_limit_min <= 600:
        raise ValidationError("Time limit must be between 1 and 600 minutes.", field="time_limit_min")
    if not 1 <= max_attempts <= 5:
        raise ValidationError("Attempts must be between 1 and 5.", field="max_attempts")
    if not targets:
        raise ValidationError("Choose who gets this assignment.", field="targets")
    for kind, tid in targets:
        _check_target(tx, a["class_id"], kind, tid)
        tx.exec("""INSERT INTO assignment_targets(assignment_id, institution_id, target_kind, target_id) VALUES (%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""", [assignment_id, tx.inst, kind, tid])
    tx.exec("""UPDATE assignments SET state='scheduled', opens_at=%s, expires_at=%s, time_limit_min=%s, max_attempts=%s WHERE id=%s""",
            [opens_at, expires_at, time_limit_min, max_attempts, assignment_id])
    jobs.enqueue(tx, "assignment_closed", {"assignment_id": assignment_id}, run_at=expires_at,
                 dedupe_key=f"closed:{assignment_id}:{expires_at.isoformat()}")
    jobs.enqueue(tx, "assignment_reminder", {"assignment_id": assignment_id}, run_at=max(opens_at, expires_at - timedelta(hours=24)),
                 dedupe_key=f"remind:{assignment_id}:{expires_at.isoformat()}")
    audit.record(tx, "assignment.schedule", actor_id=actor.user_id, target=f"assignment:{assignment_id}",
                 detail={"opens_at": opens_at.isoformat(), "expires_at": expires_at.isoformat(), "targets": targets})
    return state_at(get(tx, assignment_id))


def _check_target(tx: Tx, class_id: int, kind: str, target_id: int) -> None:
    if kind == "class" and target_id == class_id:
        return
    if kind == "group" and tx.val("SELECT 1 FROM student_groups WHERE id=%s AND class_id=%s", [target_id, class_id]):
        return
    if kind == "student" and tx.val("SELECT 1 FROM enrolments WHERE student_id=%s AND class_id=%s AND status='active'",
                                    [target_id, class_id]):
        return
    raise ValidationError("Assign only to your class, its groups, or its students.")


def create_group(tx: Tx, actor: Actor, class_id: int, name: str, student_ids: list[int]) -> int:
    """Make a named group of students in a class, for targeted work (SC-1)."""
    require(actor, "teacher")
    require_class(tx, actor, class_id, write=True)
    if not name.strip() or not student_ids:
        raise ValidationError("Give the group a name and choose students.")
    gid = tx.val("INSERT INTO student_groups(institution_id, class_id, name) VALUES (%s,%s,%s) RETURNING id",
                 [tx.inst, class_id, name.strip()])
    for sid in student_ids:
        _check_target(tx, class_id, "student", sid)
        tx.exec("INSERT INTO group_members(group_id, institution_id, student_id) VALUES (%s,%s,%s)", [gid, tx.inst, sid])
    return gid


def list_groups(tx: Tx, class_id: int) -> list[dict]:
    """Groups in a class."""
    return tx.all("""SELECT g.id, g.name, count(m.student_id) AS members FROM student_groups g
                     LEFT JOIN group_members m ON m.group_id=g.id WHERE g.class_id=%s GROUP BY g.id ORDER BY g.name""", [class_id])


def extend(tx: Tx, actor: Actor, assignment_id: int, new_expires_at: datetime, student_id: int | None = None) -> None:
    """Extend the deadline for everyone, or for one student; applies immediately (SC-4)."""
    require(actor, "teacher")
    a = get(tx, assignment_id)
    require_class(tx, actor, a["class_id"], write=True)
    if a["state"] == "draft" or a.get("released_at"):
        raise Conflict("Only scheduled, open or closed (not yet released) assignments can be extended.")
    if new_expires_at <= clock.now():
        raise ValidationError("The new deadline must be in the future.")
    if student_id is None:
        if new_expires_at <= a["expires_at"]:
            raise ValidationError("The new deadline must be later than the current one.")
        tx.exec("UPDATE assignments SET expires_at=%s, state=CASE WHEN state='closed' THEN 'open' ELSE state END, "
                "closed_notified_at=NULL WHERE id=%s", [new_expires_at, assignment_id])
        tx.exec("UPDATE attempts SET deadline_at=%s WHERE assignment_id=%s AND status='in_progress' AND deadline_at=%s",
                [new_expires_at, assignment_id, a["expires_at"]])
        jobs.enqueue(tx, "assignment_closed", {"assignment_id": assignment_id}, run_at=new_expires_at,
                     dedupe_key=f"closed:{assignment_id}:{new_expires_at.isoformat()}")
    else:
        if not is_target(tx, assignment_id, student_id):
            raise ValidationError("That student doesn't have this assignment.")
        tx.exec("""INSERT INTO assignment_extensions(assignment_id, institution_id, student_id, expires_at) VALUES (%s,%s,%s,%s)
                   ON CONFLICT (assignment_id, student_id) DO UPDATE SET expires_at=EXCLUDED.expires_at""",
                [assignment_id, tx.inst, student_id, new_expires_at])
        tx.exec("""UPDATE attempts SET deadline_at=%s, status='in_progress', submitted_at=NULL
                   WHERE assignment_id=%s AND student_id=%s AND status='expired'""", [new_expires_at, assignment_id, student_id])
        tx.exec("UPDATE attempts SET deadline_at=%s WHERE assignment_id=%s AND student_id=%s AND status='in_progress'",
                [new_expires_at, assignment_id, student_id])
    audit.record(tx, "assignment.extend", actor_id=actor.user_id, target=f"assignment:{assignment_id}",
                 detail={"until": new_expires_at.isoformat(), "student_id": student_id})


def is_target(tx: Tx, assignment_id: int, student_id: int) -> bool:
    """True if the assignment is given to this student (via class, group or by name) (SC-1)."""
    class_id = student_class_id(tx, student_id)
    return bool(tx.val("""SELECT 1 FROM assignment_targets t WHERE t.assignment_id=%s AND (
            (t.target_kind='class' AND t.target_id=%s) OR (t.target_kind='student' AND t.target_id=%s) OR
            (t.target_kind='group' AND EXISTS (SELECT 1 FROM group_members g WHERE g.group_id=t.target_id AND g.student_id=%s)))""",
                       [assignment_id, class_id, student_id, student_id]))


def student_expiry(tx: Tx, a: dict, student_id: int) -> datetime:
    """The deadline for one student, including any personal extension."""
    ext = tx.val("SELECT expires_at FROM assignment_extensions WHERE assignment_id=%s AND student_id=%s", [a["id"], student_id])
    return max(a["expires_at"], ext) if ext else a["expires_at"]


def for_student(tx: Tx, student_id: int) -> list[dict]:
    """Assignments for a student: open ones (with time left) sorted by expiry, plus released results (SX-1)."""
    class_id = student_class_id(tx, student_id)
    rows = tx.all("""SELECT a.id, a.title, a.kind, a.state, a.opens_at, a.expires_at, a.released_at, a.max_attempts,
            a.time_limit_min, a.topic_ids, a.syllabus_id, b.subject, b.board, b.grade, b.part, b.institution_id, x.expires_at AS ext_expires,
            (SELECT status FROM attempts t WHERE t.assignment_id=a.id AND t.student_id=%(s)s ORDER BY attempt_no DESC LIMIT 1) AS attempt_status,
            (SELECT count(*) FROM attempts t WHERE t.assignment_id=a.id AND t.student_id=%(s)s) AS attempts_used,
            (SELECT count(*) FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id
              WHERE aq.assignment_id=a.id AND q.status <> 'removed') AS questions,
            (SELECT count(*) FROM responses r WHERE r.attempt_id = (SELECT t.id FROM attempts t WHERE t.assignment_id=a.id
              AND t.student_id=%(s)s AND t.status='in_progress' ORDER BY attempt_no DESC LIMIT 1)
              AND (r.answer ? 'index' OR btrim(coalesce(r.answer->>'text', '')) <> '' OR r.answer ? 'done')) AS answered
        FROM assignments a JOIN syllabi b ON b.id=a.syllabus_id
        LEFT JOIN assignment_extensions x ON x.assignment_id=a.id AND x.student_id=%(s)s
        WHERE a.state <> 'draft' AND EXISTS (SELECT 1 FROM assignment_targets t WHERE t.assignment_id=a.id AND (
            (t.target_kind='class' AND t.target_id=%(c)s) OR (t.target_kind='student' AND t.target_id=%(s)s) OR
            (t.target_kind='group' AND EXISTS (SELECT 1 FROM group_members g WHERE g.group_id=t.target_id AND g.student_id=%(s)s))))""",
                  {"s": student_id, "c": class_id})
    now = clock.now()
    out = []
    for r in rows:
        expiry = max(r["expires_at"], r["ext_expires"]) if r["ext_expires"] else r["expires_at"]
        r["student_expires_at"] = expiry
        if r["released_at"]:  # released work they never handed in has no scorecard: it's "missed" (1.31.0)
            r["view"] = "results" if r["attempt_status"] == "submitted" else "missed"
        elif r["opens_at"] <= now < expiry:
            r["view"] = "done" if r["attempt_status"] == "submitted" and r["attempts_used"] >= r["max_attempts"] else "open"
        else:
            r["view"] = "upcoming" if now < r["opens_at"] else "closed"
        out.append(r)
    for r in out:
        r["started"] = r["view"] == "open" and r["attempt_status"] == "in_progress"
        r["closes_soon"] = r["view"] == "open" and (r["student_expires_at"] - now).total_seconds() < 24 * 3600
    _subject_labels(out)
    order = {"open": 0, "done": 1, "results": 2, "upcoming": 3, "closed": 4, "missed": 5}
    return sorted(out, key=lambda r: (order[r["view"]], r["student_expires_at"]))


def _subject_labels(rows: list[dict]) -> None:
    """Each assignment's subject as students see it ('Science'; 'Science (IGCSE)' when two books share a subject name),
    with a colour that stays the same for that book everywhere (subject tags on Home and Tasks). The parts of a book
    in parts are one subject ('subject_id': the same for every part)."""
    from novaquest.services.syllabus import subject_key

    first: dict[tuple, int] = {}
    for r in sorted(rows, key=lambda r: r["syllabus_id"]):
        first.setdefault(subject_key({**r, "id": r["syllabus_id"]}), r["syllabus_id"])
    subjects = {first[subject_key({**r, "id": r["syllabus_id"]})]: (r["subject"], r["board"]) for r in rows}
    names = [sub for sub, _ in subjects.values()]
    for r in rows:
        dup = names.count(r["subject"]) > 1
        r["subject_id"] = first[subject_key({**r, "id": r["syllabus_id"]})]
        r["subject_label"] = f"{r['subject']} ({r['board']})" if dup else r["subject"]
        r["subject_colour"] = sum(ord(c) for c in f"{r['subject']}{r['board']}") % 6


def for_teacher(tx: Tx, actor: Actor, class_ids: list[int], limit: int = 100) -> list[dict]:
    """Assignments in the teacher's classes with live state, completion, subject and the sections it was set for
    together (SC-5, 1.30.0)."""
    rows = tx.all("""SELECT a.id, a.title, a.kind, a.state, a.opens_at, a.expires_at, a.released_at, a.class_id, c.label AS class_label,
            a.syllabus_id, b.subject, a.batch_id, a.sections,
            (SELECT count(DISTINCT t.student_id) FROM attempts t WHERE t.assignment_id=a.id AND t.status='submitted') AS submitted,
            (SELECT count(*) FROM responses r JOIN attempts t ON t.id=r.attempt_id WHERE t.assignment_id=a.id AND r.flagged
                AND r.reviewed_at IS NULL) AS flagged
        FROM assignments a JOIN classes c ON c.id=a.class_id JOIN syllabi b ON b.id=a.syllabus_id WHERE a.class_id = ANY(%s)
          AND (%s OR a.teacher_id=%s OR EXISTS (SELECT 1 FROM teaching_assignments ta JOIN syllabi tb ON tb.id=ta.syllabus_id
               WHERE ta.teacher_id=%s AND ta.class_id=a.class_id AND tb.subject=b.subject AND tb.grade=b.grade))
        ORDER BY coalesce(a.expires_at, a.created_at) DESC LIMIT %s""",
                  [class_ids, actor.role != "teacher", actor.user_id, actor.user_id, limit])  # a teacher sees their own subjects' work
    labels = {r["id"]: r["label"] for r in tx.all("SELECT id, label FROM classes WHERE id = ANY(%s)", [class_ids])}
    for r in rows:
        r["live_state"] = state_at(r)
        r["targets"] = len(target_students(tx, r["id"]))
        r["section_labels"] = [labels[c] for c in (r["sections"] or []) if c in labels]
    return rows


def siblings(tx: Tx, a: dict) -> list[dict]:
    """The same work set for other sections at the same time (one batch), with how many handed in; [] if none."""
    batch = a.get("batch_id")
    if not batch:
        return []
    rows = tx.all("""SELECT a.id, a.class_id, c.label AS class_label, a.state, a.opens_at, a.expires_at, a.released_at,
            (SELECT count(DISTINCT t.student_id) FROM attempts t WHERE t.assignment_id=a.id AND t.status='submitted') AS submitted
        FROM assignments a JOIN classes c ON c.id=a.class_id WHERE a.batch_id=%s OR a.id=%s ORDER BY c.grade, c.label""", [batch, batch])
    for r in rows:
        r["live_state"] = state_at(r)
        r["targets"] = len(target_students(tx, r["id"]))
    return rows if len(rows) > 1 else []


def set_for_sections(tx: Tx, actor: Actor, assignment_id: int, class_ids: list[int]) -> list[int]:
    """Remember the other sections a draft is meant for (pre-ticked when it's scheduled). Only classes this teacher
    teaches the same subject in are kept."""
    require(actor, "teacher")
    a = get(tx, assignment_id)
    require_class(tx, actor, a["class_id"], write=True)
    ok = [c for c in dict.fromkeys(class_ids) if c != a["class_id"] and _same_subject(tx, actor, c, a["syllabus_id"])]
    tx.exec("UPDATE assignments SET sections=%s WHERE id=%s", [ok, assignment_id])
    return ok


def _same_subject(tx: Tx, actor: Actor, class_id: int, syllabus_id: int) -> bool:
    """True if this teacher teaches this book (any part of the same subject) in this class."""
    from novaquest.services import syllabus

    try:
        require_class(tx, actor, class_id, write=True)
    except Exception:
        return False
    return any(syllabus_id in b["ids"] for b in syllabus.teacher_subjects(tx, actor.user_id, class_id, actor.role))


def section_choices(tx: Tx, actor: Actor, a: dict) -> list[dict]:
    """Other classes this teacher could set this draft for (same subject), with whether each is ticked."""
    from novaquest.services import syllabus

    out = []
    for c in tx.all("""SELECT DISTINCT c.id, c.label, c.grade FROM teaching_assignments ta JOIN classes c ON c.id=ta.class_id
                       WHERE ta.teacher_id=%s AND c.status='active' AND c.id <> %s ORDER BY c.grade, c.label""",
                    [actor.user_id, a["class_id"]]):
        if any(a["syllabus_id"] in b["ids"] for b in syllabus.teacher_subjects(tx, actor.user_id, c["id"], actor.role)):
            out.append({"id": c["id"], "label": c["label"], "ticked": c["id"] in (a.get("sections") or [])})
    return out


def copy_for_class(tx: Tx, actor: Actor, assignment_id: int, class_id: int) -> int:
    """The same draft (same approved questions, title and topics) for another section of the same subject: a new
    assignment of that class that shares the questions. Returns its id."""
    require(actor, "teacher")
    a = get(tx, assignment_id)
    if not _same_subject(tx, actor, class_id, a["syllabus_id"]):
        raise ValidationError("You can only set it for classes where you teach this subject.")
    new = tx.val("""INSERT INTO assignments(institution_id, class_id, teacher_id, syllabus_id, title, kind, difficulty, topic_ids,
                       batch_id, class_message)
                    SELECT institution_id, %s, %s, syllabus_id, title, kind, difficulty, topic_ids, %s, class_message
                    FROM assignments WHERE id=%s RETURNING id""", [class_id, actor.user_id, a["batch_id"] or a["id"], assignment_id])
    tx.exec("""INSERT INTO assignment_questions(assignment_id, institution_id, question_id, ord)
               SELECT %s, institution_id, question_id, ord FROM assignment_questions WHERE assignment_id=%s""", [new, assignment_id])
    tx.exec("UPDATE assignments SET batch_id=coalesce(batch_id, id) WHERE id=%s", [assignment_id])
    audit.record(tx, "assignment.copy", actor_id=actor.user_id, target=f"assignment:{new}",
                 detail={"from": assignment_id, "class_id": class_id})
    return new


def schedule_sections(tx: Tx, actor: Actor, assignment_id: int, plan: list[dict], *, targets: list[tuple[str, int]],
                      time_limit_min: int | None = None, max_attempts: int = 1) -> list[int]:
    """Schedule a draft for its own class (with the chosen targets) and, in the same step, for other sections of the
    same subject, each with its own open and close times. `plan` is [{class_id, opens_at, expires_at}], the draft's
    own class first. All or nothing: any problem raises and nothing is scheduled. Returns the assignment ids."""
    a = get(tx, assignment_id)
    own = next((p for p in plan if p["class_id"] == a["class_id"]), None)
    if not own:
        raise ValidationError(f"Tick class {a['class_label']} (this draft's own class).")
    schedule(tx, actor, assignment_id, opens_at=own["opens_at"], expires_at=own["expires_at"], targets=targets,
             time_limit_min=time_limit_min, max_attempts=max_attempts)
    ids = [assignment_id]
    for p in plan:
        if p["class_id"] == a["class_id"]:
            continue
        new = copy_for_class(tx, actor, assignment_id, p["class_id"])
        schedule(tx, actor, new, opens_at=p["opens_at"], expires_at=p["expires_at"], targets=[("class", p["class_id"])],
                 time_limit_min=time_limit_min, max_attempts=max_attempts)
        ids.append(new)
    tx.exec("UPDATE assignments SET sections='{}' WHERE id=%s", [assignment_id])
    return ids


def target_students(tx: Tx, assignment_id: int) -> list[dict]:
    """Every active student the assignment is given to."""
    return tx.all("""SELECT * FROM (SELECT DISTINCT u.id, u.name, e.roll_no, e.parent_email FROM assignment_targets t
        JOIN enrolments e ON e.status='active' AND (
            (t.target_kind='class' AND e.class_id=t.target_id) OR (t.target_kind='student' AND e.student_id=t.target_id) OR
            (t.target_kind='group' AND e.student_id IN (SELECT student_id FROM group_members WHERE group_id=t.target_id)))
        JOIN users u ON u.id=e.student_id WHERE t.assignment_id=%s) s
        ORDER BY length(s.roll_no), s.roll_no""", [assignment_id])  # 1, 2 … 10, not 1, 10, 11, 2


def sync_states(tx: Tx) -> list[int]:
    """Bring stored states in line with the clock; returns assignments that just closed."""
    now = clock.now()
    tx.exec("UPDATE assignments SET state='open' WHERE state='scheduled' AND opens_at <= %s AND expires_at > %s", [now, now])
    return [r["id"] for r in tx.all("""UPDATE assignments SET state='closed' WHERE state IN ('scheduled', 'open') AND expires_at <= %s
                                     RETURNING id""", [now])]


_MINUTES_PER = {"mcq": 1.0, "short": 3.0, "viva": 4.0, "mission": 3.0}


def task_summary(tx: Tx, a: dict) -> dict:
    """What a student wants to know before starting: how many questions, about how long, and on which topics."""
    kinds = tx.all("""SELECT q.kind, count(*) AS n FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id
                      WHERE aq.assignment_id=%s GROUP BY q.kind""", [a["id"]])
    n = sum(r["n"] for r in kinds)
    est = sum(_MINUTES_PER.get(r["kind"], 2.0) * r["n"] for r in kinds)
    minutes = a["time_limit_min"] or max(2, int(round(est / 5.0) * 5) if est >= 10 else int(round(est)) or 2)
    topics = [r["title"] for r in tx.all("SELECT title FROM syllabus_nodes WHERE id = ANY(%s) ORDER BY code",
                                         [list(a["topic_ids"] or [])])]
    about = ", ".join(topics[:2]) + (f" and {len(topics) - 2} more" if len(topics) > 2 else "")
    return {"questions": n, "minutes": minutes, "about": about, "timed": bool(a["time_limit_min"])}
