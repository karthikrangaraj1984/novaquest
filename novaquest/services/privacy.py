"""Data protection (1.19.0): what a school needs to look after children's data under India's DPDP Act 2023.

- Consent register: every student's parental consent (who, how, when), withdrawal, and a CSV for the school's files.
- Export: everything NovaQuest holds about one student, as one file, for a parent's request to see it.
- Erasure: a student's personal data removed for good. Their name, parent email, PIN, conversations with NOVA and
  written answers are deleted; marks stay, without a name, so class averages don't change.
- Retention: AI conversations are deleted after the school's chosen number of months, and students who left are
  erased after theirs; checked once a day.
- AI conversation log: the principal (and school admin) can read what students asked NOVA and taught NOVA. Each
  look is recorded in the audit log.
- A parent notice and consent form (PDF) that says all this in plain words."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from novaquest.core import audit, clock
from novaquest.core.db import Db, Tx, jsonb
from novaquest.core.errors import NotFound, ValidationError
from novaquest.services.access import Actor, require

MONTH_CHOICES = (3, 6, 12, 24, 36, 0)  # 0: keep until the school erases by hand
DEFAULTS = {"ai_chat_months": 12, "left_student_months": 24}
ERASED = "Erased student"
METHODS = {"signed_form": "Signed form", "in_person": "In person", "email_confirmation": "Email confirmation"}


# ---------- settings ----------
def settings(tx: Tx) -> dict[str, int]:
    r = tx.val("SELECT retention FROM institutions WHERE id=%s", [tx.inst]) or {}
    return {k: int(r.get(k, v)) for k, v in DEFAULTS.items()}


def save_settings(tx: Tx, actor: Actor, ai_chat_months: int, left_student_months: int) -> None:
    require(actor, "inst_admin")
    if ai_chat_months not in MONTH_CHOICES or left_student_months not in MONTH_CHOICES:
        raise ValidationError("Choose one of the listed periods.")
    new = {"ai_chat_months": ai_chat_months, "left_student_months": left_student_months}
    tx.exec("UPDATE institutions SET retention=%s WHERE id=%s", [jsonb(new), tx.inst])
    audit.record(tx, "privacy.retention", actor_id=actor.user_id, detail=new)


# ---------- consent ----------
def consent_register(tx: Tx, actor: Actor) -> list[dict[str, Any]]:
    """Every student the school has (current and left), with their parental consent."""
    require(actor, "inst_admin", "leader")
    return tx.all("""SELECT DISTINCT ON (u.id) u.id AS student_id, u.name, e.id AS enrolment_id, c.label AS class_label, e.roll_no,
            e.parent_email, e.consent_status, e.consent_by, e.consent_method, e.consent_at, e.consent_withdrawn_at, e.status
        FROM users u JOIN enrolments e ON e.student_id=u.id JOIN classes c ON c.id=e.class_id
        WHERE u.erased_at IS NULL ORDER BY u.id, (e.status='active') DESC, e.started_at DESC""")


def register_sorted(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r["status"] != "active", r["class_label"], len(r["roll_no"]), r["roll_no"]))


def register_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Class", "Roll no", "Student", "Still at school", "Parent email", "Consent", "Given by", "How", "Recorded on",
                "Withdrawn on"])
    for r in register_sorted(rows):
        w.writerow([r["class_label"], r["roll_no"], r["name"], "yes" if r["status"] == "active" else "no", r["parent_email"] or "",
                    r["consent_status"], r["consent_by"] or "", METHODS.get(r["consent_method"] or "", r["consent_method"] or ""),
                    clock.to_ist(r["consent_at"]).strftime("%Y-%m-%d") if r["consent_at"] else "",
                    clock.to_ist(r["consent_withdrawn_at"]).strftime("%Y-%m-%d") if r["consent_withdrawn_at"] else ""])
    return buf.getvalue()


def withdraw_consent(tx: Tx, actor: Actor, enrolment_id: int) -> None:
    """A parent withdraws consent: the student is signed out and can't sign in until consent is recorded again.
    Their data stays until the school erases it (or the retention period ends after they leave)."""
    require(actor, "inst_admin")
    row = tx.one("SELECT student_id, consent_status FROM enrolments WHERE id=%s", [enrolment_id])
    if not row:
        raise NotFound("Student not found.")
    tx.exec("UPDATE enrolments SET consent_status='withdrawn', consent_withdrawn_at=%s WHERE id=%s", [clock.now(), enrolment_id])
    tx.exec("UPDATE sessions SET revoked=true WHERE user_id=%s", [row["student_id"]])
    audit.record(tx, "privacy.consent_withdrawn", actor_id=actor.user_id, target=f"enrolment:{enrolment_id}")


# ---------- export ----------
def _student(tx: Tx, student_id: int) -> dict:
    s = tx.one("""SELECT u.id, u.name, u.created_at, u.last_login_at, u.erased_at FROM users u
                  JOIN user_roles r ON r.user_id=u.id AND r.role='student' WHERE u.id=%s""", [student_id])
    if not s or s["erased_at"]:
        raise NotFound("Student not found.")
    return s


def export_student(tx: Tx, actor: Actor, student_id: int) -> dict[str, Any]:
    """Everything NovaQuest holds about one student, in one structure (saved as JSON)."""
    require(actor, "inst_admin")
    s = _student(tx, student_id)
    one = [student_id]
    data = {
        "about": "Everything NovaQuest holds about this student, exported by the school.",
        "exported_at": clock.now().isoformat(),
        "school": tx.val("SELECT name FROM institutions WHERE id=%s", [tx.inst]),
        "student": {"name": s["name"], "account_created": s["created_at"], "last_sign_in": s["last_login_at"]},
        "enrolments": tx.all("""SELECT c.label AS class, e.roll_no, e.parent_email, e.consent_status, e.consent_by,
                e.consent_method, e.consent_at, e.consent_withdrawn_at, e.status, e.started_at, e.ended_at
            FROM enrolments e JOIN classes c ON c.id=e.class_id WHERE e.student_id=%s ORDER BY e.started_at""", one),
        "assignments": tx.all("""SELECT a.title, t.attempt_no, t.status, t.started_at, t.submitted_at, t.feedback AS teacher_note,
                (SELECT json_agg(json_build_object('question', q.prompt, 'answer', r.answer,
                    'score', coalesce(r.teacher_score, r.ai_score), 'out_of', r.max_score, 'marking_note', r.ai_rationale,
                    'teacher_comment', r.teacher_comment) ORDER BY r.id)
                 FROM responses r JOIN questions q ON q.id=r.question_id WHERE r.attempt_id=t.id) AS answers
            FROM attempts t JOIN assignments a ON a.id=t.assignment_id WHERE t.student_id=%s ORDER BY t.started_at""", one),
        "topic_progress": tx.all("""SELECT n.code, n.title, m.level, m.taught_ok, m.updated_at FROM mastery m
            JOIN syllabus_nodes n ON n.id=m.topic_id WHERE m.student_id=%s ORDER BY n.syllabus_id, n.ord""", one),
        "learning_activity": tx.all("""SELECT n.title AS topic, le.kind, le.score, le.data, le.created_at FROM learn_events le
            JOIN syllabus_nodes n ON n.id=le.topic_id WHERE le.student_id=%s ORDER BY le.created_at""", one),
        "ask_nova": tx.all("""SELECT m.created_at, m.context, m.role, m.body FROM tutor_messages m WHERE m.student_id=%s
            ORDER BY m.created_at, m.id""", one),
        "teach_nova": tx.all("""SELECT n.title AS topic, s.status, s.started_at, s.ended_at, s.summary,
                (SELECT json_agg(json_build_object('who', tt.role, 'text', tt.body, 'at', tt.created_at) ORDER BY tt.id)
                 FROM teach_turns tt WHERE tt.session_id=s.id) AS conversation
            FROM teach_sessions s JOIN syllabus_nodes n ON n.id=s.topic_id WHERE s.student_id=%s ORDER BY s.started_at""", one),
        "escape_rooms": tx.all("""SELECT r.chapter, r.status, r.started_at,
                (SELECT json_agg(json_build_object('lock', l.lock_no, 'skill', l.skill, 'score', l.score, 'answer', l.last_answer)
                    ORDER BY l.lock_no) FROM escape_locks l WHERE l.run_id=r.id) AS locks
            FROM escape_runs r WHERE r.student_id=%s ORDER BY r.started_at""", one),
        "paper_work": tx.all("""SELECT a.title, s.pages, s.status, s.reading AS answers_read, s.created_at FROM paper_scans s
            JOIN assignments a ON a.id=s.assignment_id WHERE s.student_id=%s ORDER BY s.created_at""", one),
        "daily_5": tx.all("SELECT day, score, done_at FROM daily_reviews WHERE student_id=%s ORDER BY day", one),
        "questions_to_teacher": tx.all("""SELECT body, reply, created_at, replied_at FROM doubts WHERE student_id=%s
            ORDER BY created_at""", one),
        "reflections": tx.all("""SELECT a.title, r.feeling, r.next_step, r.created_at FROM reflections r
            JOIN assignments a ON a.id=r.assignment_id WHERE r.student_id=%s ORDER BY r.created_at""", one),
        "weekly_goals": tx.all("SELECT week_start, target FROM weekly_goals WHERE student_id=%s ORDER BY week_start", one),
        "exams": tx.all("""SELECT p.title, p.exam_on, (SELECT json_agg(json_build_object('score', x.score, 'at', x.done_at))
                FROM exam_practice x WHERE x.plan_id=p.id) AS practice_papers FROM exam_plans p WHERE p.student_id=%s""", one),
        "mistakes_redone": tx.all("""SELECT q.prompt, f.tries, f.fixed_at FROM mistake_fixes f JOIN questions q ON q.id=f.question_id
            WHERE f.student_id=%s""", one),
        "notifications": tx.all("SELECT title, body, created_at FROM notifications WHERE user_id=%s ORDER BY created_at", one),
    }
    audit.record(tx, "privacy.export", actor_id=actor.user_id, target=f"user:{student_id}")
    return data


# ---------- erasure ----------
def _erase(tx: Tx, student_id: int, paper_root: Path | None = None) -> None:
    """Remove a student's personal data. Marks stay without a name, so class figures don't change."""
    from novaquest.services import paper

    one = [student_id]
    paper.forget_student(tx, paper_root, student_id)
    tx.exec("""UPDATE users SET name=%s, email=NULL, password_hash=NULL, invite_token_hash=NULL, status='inactive',
               erased_at=%s WHERE id=%s""", [ERASED, clock.now(), student_id])
    tx.exec("""UPDATE enrolments SET parent_email=NULL, consent_by=NULL, pin_hash='!erased',
               status=CASE WHEN status='active' THEN 'inactive' ELSE status END, ended_at=coalesce(ended_at, %s)
               WHERE student_id=%s""", [clock.now(), student_id])
    tx.exec("DELETE FROM tutor_messages WHERE student_id=%s", one)
    tx.exec("DELETE FROM teach_turns WHERE session_id IN (SELECT id FROM teach_sessions WHERE student_id=%s)", one)
    tx.exec("UPDATE teach_sessions SET summary=NULL, belief='{}'::jsonb WHERE student_id=%s", one)
    tx.exec("""UPDATE responses SET answer=NULL, ai_rationale=NULL, teacher_comment=NULL
               WHERE attempt_id IN (SELECT id FROM attempts WHERE student_id=%s)""", one)
    tx.exec("UPDATE attempts SET feedback=NULL WHERE student_id=%s", one)
    tx.exec("UPDATE learn_events SET data='{}'::jsonb WHERE student_id=%s", one)
    tx.exec("UPDATE escape_locks SET last_answer=NULL WHERE run_id IN (SELECT id FROM escape_runs WHERE student_id=%s)", one)
    tx.exec("DELETE FROM notifications WHERE user_id=%s", one)
    tx.exec("DELETE FROM report_links WHERE student_id=%s", one)
    tx.exec("UPDATE doubts SET body='(erased)', reply=NULL WHERE student_id=%s", one)  # 1.31.0: their own words
    for table in ("reflections", "weekly_goals", "exam_plans", "mistake_fixes"):
        tx.exec(f"DELETE FROM {table} WHERE student_id=%s", one)
    tx.exec("UPDATE sessions SET revoked=true WHERE user_id=%s", one)


def erase_student(tx: Tx, actor: Actor, student_id: int, confirm: str, paper_root: Path | None = None) -> None:
    """Erase one student on a parent's request. The admin types the student's name to confirm; it can't be undone."""
    require(actor, "inst_admin")
    s = _student(tx, student_id)
    if " ".join(confirm.split()).lower() != " ".join(s["name"].split()).lower():
        raise ValidationError(f"Type the student's name exactly ({s['name']}) to confirm.")
    _erase(tx, student_id, paper_root)
    audit.record(tx, "privacy.erase", actor_id=actor.user_id, target=f"user:{student_id}", detail={"reason": "request"})


# ---------- retention ----------
def _months_ago(months: int) -> datetime:
    return clock.now() - timedelta(days=round(months * 30.44))


def apply_retention(tx: Tx, paper_root: Path | None = None) -> dict[str, int]:
    """Delete AI conversations (and photos of paper work) older than the school's period, and erase students who
    left longer ago than theirs."""
    from novaquest.services import paper

    s = settings(tx)
    out = {"messages": 0, "turns": 0, "photos": 0, "erased": 0}
    if s["ai_chat_months"]:
        cut = _months_ago(s["ai_chat_months"])
        out["photos"] = paper.purge(tx, paper_root, cut)
        out["messages"] = tx.conn.execute("DELETE FROM tutor_messages WHERE created_at < %s", [cut]).rowcount
        out["turns"] = tx.conn.execute("DELETE FROM teach_turns WHERE created_at < %s", [cut]).rowcount
    if s["left_student_months"]:
        cut = _months_ago(s["left_student_months"])
        gone = [r["id"] for r in tx.all("""SELECT u.id FROM users u JOIN user_roles r ON r.user_id=u.id AND r.role='student'
            WHERE u.erased_at IS NULL AND NOT EXISTS (SELECT 1 FROM enrolments e WHERE e.student_id=u.id
              AND (e.status='active' OR e.ended_at IS NULL OR e.ended_at >= %s))
              AND EXISTS (SELECT 1 FROM enrolments e WHERE e.student_id=u.id)""", [cut])]
        for sid in gone:
            _erase(tx, sid, paper_root)
        out["erased"] = len(gone)
    tx.exec("UPDATE institutions SET retention_ran_on=%s WHERE id=%s", [clock.to_ist(clock.now()).date(), tx.inst])
    if any(out.values()):
        audit.record(tx, "privacy.retention_run", actor_id=None, detail=out)
    return out


def daily_retention(db: Db, paper_root: Path | None = None) -> int:
    """Run each school's retention once a day. Returns how many schools ran."""
    today: date = clock.to_ist(clock.now()).date()
    with db.platform() as tx:
        due = [r["id"] for r in tx.all("""SELECT id FROM institutions WHERE status='active'
                                          AND (retention_ran_on IS NULL OR retention_ran_on < %s)""", [today])]
    for inst in due:
        with db.tenant(inst) as tx:
            apply_retention(tx, paper_root)
    return len(due)


# ---------- AI conversation log ----------
def ai_log(tx: Tx, actor: Actor, *, class_id: int | None = None, q: str = "", days: int = 30, limit: int = 40) -> dict[str, Any]:
    """Students' conversations with NOVA (Ask NOVA and Teach NOVA), newest first, for the principal and school
    admin. Each conversation shows the student, class, topic and every message. Looking is audited."""
    require(actor, "leader", "inst_admin")
    since = clock.now() - timedelta(days=days)
    like = f"%{q.strip()}%" if q.strip() else None
    params = {"since": since, "cid": class_id, "like": like, "lim": limit}
    ask = tx.all("""SELECT 'ask' AS kind, m.student_id, coalesce(m.topic_id, qq.topic_id) AS topic_id,
            m.created_at::date AS day, min(m.created_at) AS started, count(*) AS n,
            json_agg(json_build_object('who', CASE m.role WHEN 'student' THEN 'student' ELSE 'nova' END, 'text', m.body,
                                       'at', m.created_at) ORDER BY m.created_at, m.id) AS messages
        FROM tutor_messages m LEFT JOIN questions qq ON qq.id=m.question_id
        WHERE m.created_at >= %(since)s
          AND (%(cid)s::bigint IS NULL OR m.student_id IN (SELECT student_id FROM enrolments WHERE class_id=%(cid)s))
        GROUP BY m.student_id, coalesce(m.topic_id, qq.topic_id), m.created_at::date
        HAVING %(like)s::text IS NULL OR bool_or(m.body ILIKE %(like)s)
        ORDER BY started DESC LIMIT %(lim)s""", params)
    teach = tx.all("""SELECT 'teach' AS kind, s.student_id, s.topic_id, s.started_at::date AS day, s.started_at AS started,
            count(t.id) AS n, json_agg(json_build_object('who', t.role, 'text', t.body, 'at', t.created_at) ORDER BY t.id) AS messages
        FROM teach_sessions s JOIN teach_turns t ON t.session_id=s.id
        WHERE s.started_at >= %(since)s
          AND (%(cid)s::bigint IS NULL OR s.student_id IN (SELECT student_id FROM enrolments WHERE class_id=%(cid)s))
        GROUP BY s.id HAVING %(like)s::text IS NULL OR bool_or(t.body ILIKE %(like)s)
        ORDER BY started DESC LIMIT %(lim)s""", params)
    convs = sorted(ask + teach, key=lambda c: c["started"], reverse=True)[:limit]
    who = {r["id"]: r for r in tx.all("""SELECT DISTINCT ON (u.id) u.id, u.name, c.label FROM users u
        JOIN enrolments e ON e.student_id=u.id JOIN classes c ON c.id=e.class_id
        WHERE u.id = ANY(%s) ORDER BY u.id, (e.status='active') DESC""", [list({c["student_id"] for c in convs})])}
    topics = {r["id"]: r["title"] for r in tx.all("SELECT id, title FROM syllabus_nodes WHERE id = ANY(%s)",
                                                  [list({c["topic_id"] for c in convs if c["topic_id"]})])}
    for c in convs:
        w = who.get(c["student_id"], {})
        c["student"], c["class_label"], c["topic"] = w.get("name", "?"), w.get("label", ""), topics.get(c["topic_id"], "")
    classes = tx.all("SELECT id, label FROM classes WHERE status='active' ORDER BY label")
    audit.record(tx, "privacy.ai_log_view", actor_id=actor.user_id, target=f"class:{class_id}" if class_id else None,
                 detail={"q": q.strip()[:80], "days": days})
    return {"conversations": convs, "classes": classes, "class_id": class_id, "q": q, "days": days}


def overview(tx: Tx, actor: Actor) -> dict[str, Any]:
    """Figures for the Data protection page."""
    require(actor, "inst_admin")
    rows = consent_register(tx, actor)
    active = [r for r in rows if r["status"] == "active"]
    return {"settings": settings(tx), "register": register_sorted(rows),
            "counts": {"students": len(active), "recorded": sum(1 for r in active if r["consent_status"] == "recorded"),
                       "pending": sum(1 for r in active if r["consent_status"] == "pending"),
                       "withdrawn": sum(1 for r in active if r["consent_status"] == "withdrawn"),
                       "left": len(rows) - len(active)},
            "erased": tx.val("SELECT count(*) FROM users WHERE erased_at IS NOT NULL"),
            "log": tx.all("""SELECT a.action, a.target, a.created_at, u.name AS actor FROM audit_events a
                LEFT JOIN users u ON u.id=a.actor_id WHERE a.action LIKE 'privacy.%%' ORDER BY a.created_at DESC LIMIT 12""")}
