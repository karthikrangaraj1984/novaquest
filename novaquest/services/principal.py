"""What a principal needs (principal's review, 1.9.0): one overview that leads somewhere, a read-only page per class,
'needs your attention' items worked out from the data, a friendly reminder to a teacher, a weekly summary and a
one-page monthly report for management meetings.

Every figure follows the same honesty rules as the rest of the overview: a class or skill figure needs at least
dashboards.MIN_STUDENTS students behind it, and 'on time' counts only assignments whose deadline has passed."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from novaquest.core import audit, clock
from novaquest.core.db import Tx
from novaquest.core.errors import NotFound, ValidationError
from novaquest.services import dashboards, escape, notify, plans, syllabus
from novaquest.services.access import Actor, require, require_class
from novaquest.services.assignments import target_students

IDLE_DAYS = 7          # a class with no student activity for this long needs attention
TEACHER_IDLE_DAYS = 14  # a teacher who hasn't set work for this long
MISSED_ALERT = 3       # a student who has missed this many closed assignments
ON_TIME_ALERT = 70     # a class handing in less than this share on time
MIN = dashboards.MIN_STUDENTS


def _days_since(t: datetime | None) -> int | None:
    return None if t is None else max(0, (clock.now() - t).days)


def _last_active(tx: Tx, class_ids: list[int]) -> dict[int, datetime]:
    """Class id -> the last time any of its students did something (a learning event or a handed-in attempt)."""
    rows = tx.all("""SELECT e.class_id, max(x.at) AS at FROM enrolments e JOIN (
            SELECT student_id, max(created_at) AS at FROM learn_events GROUP BY student_id
            UNION ALL SELECT student_id, max(submitted_at) FROM attempts WHERE status='submitted' GROUP BY student_id) x
          ON x.student_id=e.student_id
        WHERE e.status='active' AND e.class_id = ANY(%s) GROUP BY e.class_id""", [class_ids])
    return {r["class_id"]: r["at"] for r in rows if r["at"]}


def _rooms(tx: Tx, class_ids: list[int]) -> dict[int, dict[str, int]]:
    """Class id -> students who have played an escape room, and rooms escaped."""
    rows = tx.all("""SELECT e.class_id, count(DISTINCT r.student_id) AS players,
            count(*) FILTER (WHERE r.status='escaped') AS escaped
        FROM escape_runs r JOIN enrolments e ON e.student_id=r.student_id AND e.status='active'
        WHERE e.class_id = ANY(%s) GROUP BY e.class_id""", [class_ids])
    return {r["class_id"]: {"players": r["players"], "escaped": r["escaped"]} for r in rows}


def _teachers(tx: Tx) -> list[dict[str, Any]]:
    """Every teacher with their classes, assignments set, the last one set and their last sign-in."""
    rows = tx.all("""SELECT u.id, u.name, u.last_login_at,
            (SELECT string_agg(DISTINCT c.label, ', ') FROM teaching_assignments ta JOIN classes c ON c.id=ta.class_id
              AND c.status='active' WHERE ta.teacher_id=u.id) AS classes,
            (SELECT count(*) FROM assignments a WHERE a.teacher_id=u.id AND a.state <> 'draft') AS assignments,
            (SELECT max(a.created_at) FROM assignments a WHERE a.teacher_id=u.id) AS last_set
        FROM users u JOIN user_roles r ON r.user_id=u.id AND r.role='teacher' WHERE u.status='active' ORDER BY u.name""")
    for t in rows:
        t["idle_days"] = _days_since(t["last_set"])
        t["idle"] = bool(t["classes"]) and (t["last_set"] is None or t["idle_days"] >= TEACHER_IDLE_DAYS)
    return rows


def overview(tx: Tx, actor: Actor) -> dict[str, Any]:
    """The principal's overview: the leadership figures plus, per class, last activity, term-plan status and escape
    rooms; teachers with how recently they set work; students who've missed several assignments; and a short
    'needs your attention' list built from all of it (most serious first)."""
    d = dashboards.leadership(tx, actor)
    ids = [c["id"] for c in d["classes"]]
    last, rooms = _last_active(tx, ids), _rooms(tx, ids)
    for c in d["classes"]:
        c["last_active"] = last.get(c["id"])
        c["idle_days"] = _days_since(c["last_active"])
        c["idle"] = c["students"] > 0 and (c["last_active"] is None or c["idle_days"] >= IDLE_DAYS)
        c["plan"] = plans.class_summary(tx, actor, c["id"])
        c["rooms"] = rooms.get(c["id"], {"players": 0, "escaped": 0})
    d["teachers"] = _teachers(tx)
    d["missed"] = [s for s in d["not_completing"] if s["missed"] >= MISSED_ALERT]
    d["ai_skills"] = [s for s in d["ai_skills"] if (s.get("students") or 0) >= MIN]
    d["kpis"]["ai"] = round(sum(float(s["pct"]) for s in d["ai_skills"]) / len(d["ai_skills"])) if d["ai_skills"] else None
    d["concerns"] = concerns(d)
    d["thresholds"] = {"idle": IDLE_DAYS, "teacher_idle": TEACHER_IDLE_DAYS, "missed": MISSED_ALERT, "on_time": ON_TIME_ALERT,
                       "min": MIN}
    return d


def concerns(d: dict[str, Any]) -> list[dict[str, Any]]:
    """'Needs your attention': each item says what, where, and links to the page to act on it."""
    out = []
    for c in d["classes"]:
        teachers = c["teachers"] if c["teachers"] != "—" else "no teacher assigned"
        if c["idle"]:
            when = "yet" if c["last_active"] is None else f"for {c['idle_days']} days"
            out.append({"rank": 0, "text": f"{c['label']} has had no NovaQuest activity {when} ({teachers}).",
                        "link": f"/leader/classes/{c['id']}"})
        if c["plan"]["behind"]:
            n = c["plan"]["behind"]
            out.append({"rank": 1, "text": f"{c['label']} is {n} chapter{'s' if n != 1 else ''} behind its term plan.",
                        "link": f"/leader/classes/{c['id']}#plan"})
        if c["on_time"] is not None and c["on_time"] < ON_TIME_ALERT:
            out.append({"rank": 2, "text": f"Only {c['on_time']}% of {c['label']} handed in work on time.",
                        "link": f"/leader/classes/{c['id']}#work"})
        if c["understanding"] is not None and c["understanding"] < 50:
            out.append({"rank": 2, "text": f"{c['label']}'s understanding is low ({c['understanding']}%).",
                        "link": f"/leader/classes/{c['id']}#topics"})
    for t in d["teachers"]:
        if t["idle"]:
            when = "any assignments yet" if t["last_set"] is None else f"an assignment for {t['idle_days']} days"
            out.append({"rank": 1, "text": f"{t['name']} ({t['classes']}) hasn't set {when}.", "link": "#teachers",
                        "teacher_id": t["id"], "teacher": t["name"]})
    if d["missed"]:
        n = len(d["missed"])
        out.append({"rank": 2, "text": f"{n} student{'s have' if n != 1 else ' has'} missed {MISSED_ALERT} or more assignments.",
                    "link": "#missed"})
    return sorted(out, key=lambda x: x["rank"])


def class_page(tx: Tx, actor: Actor, class_id: int) -> dict[str, Any]:
    """A read-only page for one class: its figures, term plan, weakest topics, assignments, AI skills and students."""
    require(actor, "leader", "inst_admin")
    require_class(tx, actor, class_id)
    c = next((x for x in overview(tx, actor)["classes"] if x["id"] == class_id), None)
    if not c:
        raise NotFound("That class was not found.")
    books = []
    for b in syllabus.class_subjects(tx, class_id):
        v = plans.view(tx, actor, class_id, b["id"])
        books.append({"book": b, "plan": v if v["planned"] else None})
    mastery_data = dashboards.class_mastery(tx, class_id)
    work = tx.all("""SELECT a.id, a.title, a.kind, a.state, a.opens_at, a.expires_at, a.released_at, u.name AS teacher,
            (SELECT count(DISTINCT t.student_id) FROM attempts t WHERE t.assignment_id=a.id AND t.status='submitted') AS submitted
        FROM assignments a JOIN users u ON u.id=a.teacher_id WHERE a.class_id=%s AND a.state <> 'draft'
        ORDER BY a.expires_at DESC LIMIT 12""", [class_id])
    from novaquest.services.assignments import target_students
    for a in work:
        a["targets"] = len(target_students(tx, a["id"]))
        a["closed"] = a["expires_at"] < clock.now()
    ev = escape.class_view(tx, actor, class_id)
    skills = [dict(s, enough=s["students"] >= MIN) for s in ev["skills"]]
    students = tx.all("""SELECT u.id, u.name, e.roll_no, u.last_login_at,
            count(m.topic_id) FILTER (WHERE m.level=3) AS mastered, count(m.topic_id) FILTER (WHERE m.level=2) AS practised,
            count(m.topic_id) FILTER (WHERE m.level=1) AS learning,
            (SELECT count(*) FROM escape_runs r WHERE r.student_id=u.id AND r.status='escaped') AS escaped
        FROM enrolments e JOIN users u ON u.id=e.student_id LEFT JOIN mastery m ON m.student_id=u.id
        WHERE e.class_id=%s AND e.status='active' GROUP BY u.id, e.roll_no ORDER BY length(e.roll_no), e.roll_no""", [class_id])
    missed = {r["student_id"]: r["missed"] for r in tx.all("""SELECT e.student_id, count(*) AS missed FROM assignments a
        JOIN assignment_targets tg ON tg.assignment_id=a.id AND tg.target_kind='class' AND tg.target_id=%s
        JOIN enrolments e ON e.class_id=tg.target_id AND e.status='active'
        WHERE a.expires_at < %s AND a.state <> 'draft' AND NOT EXISTS (SELECT 1 FROM attempts t WHERE t.assignment_id=a.id
          AND t.student_id=e.student_id AND t.status='submitted') GROUP BY e.student_id""", [class_id, clock.now()])}
    for s in students:
        s["missed"] = missed.get(s["id"], 0)
    from novaquest.services import growth
    return {"c": c, "books": books, "weakest": mastery_data["weakest"], "levels": mastery_data["levels"], "work": work,
            "growth": growth.class_summaries(tx, class_id), "skills": skills, "escapes": ev["escapes"], "students": students, "min": MIN, "missed_alert": MISSED_ALERT}


def nudge_teacher(tx: Tx, actor: Actor, teacher_id: int, message: str = "") -> str:
    """A friendly note from the principal in the teacher's bell (at most one a day per teacher; sending again
    updates it). Returns the teacher's name."""
    require(actor, "leader")
    t = tx.one("""SELECT u.id, u.name FROM users u JOIN user_roles r ON r.user_id=u.id AND r.role='teacher'
                  WHERE u.id=%s AND u.status='active'""", [teacher_id])
    if not t:
        raise NotFound("That teacher was not found.")
    text = " ".join(message.split())[:500] or ("Could you set your classes a NovaQuest activity this week? "
                                               "A short quiz, a picture lesson or an escape room is enough to get started.")
    me = tx.val("SELECT name FROM users WHERE id=%s", [actor.user_id])
    notify.notify(tx, t["id"], "nudge", f"A note from {me}", text, "/teacher", f"nudge:{t['id']}:{clock.to_ist(clock.now()).date()}")
    audit.record(tx, "principal.nudge", actor_id=actor.user_id, target=f"user:{t['id']}")
    return t["name"]


# ---------- weekly summary ----------
def week_key(now: datetime | None = None) -> str | None:
    """This week's summary key, or None before Monday 7 am (Indian time), when the summary goes out."""
    ist = clock.to_ist(now or clock.now())
    if ist.weekday() == 0 and ist.hour < 7:
        return None
    iso = ist.isocalendar()
    return f"weekly:{iso[0]}-W{iso[1]:02d}"


def weekly_text(d: dict[str, Any]) -> tuple[str, str]:
    """(title, body) of the weekly summary, from an overview."""
    k = d["kpis"]
    lines = [f"Students active this week: {k['active_pct']}% of {k['students']}."]
    if k["on_time"] is not None:
        lines.append(f"Handed in on time (closed assignments, last 8 weeks): {k['on_time']}%.")
    if d["concerns"]:
        lines.append("Needs your attention:")
        lines += [f"- {x['text']}" for x in d["concerns"][:6]]
    else:
        lines.append("Nothing needs your attention this week.")
    n = len(d["concerns"])
    title = f"Weekly summary: {n} thing{'s' if n != 1 else ''} to look at" if n else "Weekly summary: all on track"
    return title, "\n".join(lines)


def send_weekly(tx: Tx) -> int:
    """Send this week's summary to every principal of the school once (bell and email). Returns how many got it."""
    key = week_key()
    if key is None:
        return 0
    leaders = tx.all("""SELECT u.id, u.name, u.email FROM users u JOIN user_roles r ON r.user_id=u.id AND r.role='leader'
                        WHERE u.status='active'""")
    todo = [u for u in leaders if not tx.val("SELECT 1 FROM notifications WHERE user_id=%s AND dedupe_key=%s", [u["id"], key])]
    if not todo:
        return 0
    actor = Actor(todo[0]["id"], tx.inst, "leader", ("leader",), todo[0]["name"])
    title, body = weekly_text(overview(tx, actor))
    for u in todo:
        notify.notify(tx, u["id"], "weekly", title, body, "/leader", key)
        notify.queue_email(tx, u["email"], f"NovaQuest {title.lower()}", body + "\n\nOpen NovaQuest for details.", f"{key}:{u['id']}")
    return len(todo)


# ---------- monthly report ----------
def parse_month(value: str | None) -> date:
    """'2026-10' -> 1 Oct 2026; blank means this month (Indian time)."""
    if not value:
        t = clock.to_ist(clock.now()).date()
        return t.replace(day=1)
    try:
        y, m = (int(x) for x in value.split("-"))
        return date(y, m, 1)
    except (ValueError, TypeError):
        raise ValidationError("Choose a month like 2026-10.")


def monthly(tx: Tx, actor: Actor, month: date) -> dict[str, Any]:
    """Figures for one calendar month, for the management-meeting report."""
    require(actor, "leader", "inst_admin")
    start = datetime(month.year, month.month, 1, tzinfo=clock.IST)
    end = datetime(month.year + (month.month == 12), month.month % 12 + 1, 1, tzinfo=clock.IST)
    d = overview(tx, actor)
    per = {r["class_id"]: r for r in tx.all("""SELECT e.class_id, count(DISTINCT le.student_id) AS active,
            round(100 * avg(le.score)) AS pct, count(DISTINCT le.student_id) FILTER (WHERE le.score IS NOT NULL) AS scored
        FROM enrolments e JOIN learn_events le ON le.student_id=e.student_id AND le.created_at >= %s AND le.created_at < %s
        WHERE e.status='active' GROUP BY e.class_id""", [start, end])}
    set_ = {r["class_id"]: r["n"] for r in tx.all("""SELECT class_id, count(*) AS n FROM assignments
        WHERE state <> 'draft' AND opens_at >= %s AND opens_at < %s GROUP BY class_id""", [start, end])}
    for c in d["classes"]:
        p = per.get(c["id"], {})
        c["month_active_pct"] = round(100 * (p.get("active") or 0) / c["students"]) if c["students"] else 0
        c["month_understanding"] = p.get("pct") if (p.get("scored") or 0) >= MIN else None
        c["month_assignments"] = set_.get(c["id"], 0)
    escaped = tx.val("SELECT count(*) FROM escape_runs WHERE status='escaped' AND escaped_at >= %s AND escaped_at < %s", [start, end])
    school = tx.val("SELECT name FROM institutions WHERE id=%s", [tx.inst])
    return {"school": school, "month": month, "d": d, "escaped": int(escaped or 0),
            "assignments": sum(set_.values()), "students": d["kpis"]["students"]}


# ---------- the whole school at a glance (1.30.0) ----------
def _grade_key(g: str) -> tuple:
    return (0, int(g), "") if str(g).isdigit() else (1, 0, str(g))


def _cell(tx: Tx, class_id: int, students: int, book: dict[str, Any], now: datetime) -> dict[str, Any]:
    """One class in one subject over the last four weeks: understanding, how much of the book is practised, the term
    plan, work set and handed in, and who teaches it."""
    ids = book["ids"]
    since = now - timedelta(days=28)
    u = tx.one("""SELECT round(100 * avg(le.score)) AS pct, count(DISTINCT le.student_id) AS n FROM learn_events le
                  JOIN syllabus_nodes t ON t.id=le.topic_id
                  JOIN enrolments e ON e.student_id=le.student_id AND e.class_id=%s AND e.status='active'
                  WHERE le.score IS NOT NULL AND le.created_at > %s AND t.syllabus_id = ANY(%s)""", [class_id, since, ids])
    topics = tx.val("SELECT count(*) FROM syllabus_nodes WHERE kind='topic' AND syllabus_id = ANY(%s)", [ids]) or 0
    practised = tx.val("""SELECT count(*) FROM mastery m JOIN syllabus_nodes t ON t.id=m.topic_id
                          JOIN enrolments e ON e.student_id=m.student_id AND e.class_id=%s AND e.status='active'
                          WHERE m.level >= 2 AND t.syllabus_id = ANY(%s)""", [class_id, ids]) or 0
    set_4w = tx.val("""SELECT count(*) FROM assignments WHERE class_id=%s AND syllabus_id = ANY(%s) AND state <> 'draft'
                       AND opens_at > %s""", [class_id, ids, since]) or 0
    done = total = 0
    for a in tx.all("""SELECT id, expires_at FROM assignments WHERE class_id=%s AND syllabus_id = ANY(%s) AND state <> 'draft'
                       AND expires_at < %s AND expires_at > %s""", [class_id, ids, now, now - timedelta(weeks=8)]):
        targets = [s["id"] for s in target_students(tx, a["id"])]
        total += len(targets)
        done += tx.val("""SELECT count(DISTINCT student_id) FROM attempts WHERE assignment_id=%s AND status='submitted'
                          AND student_id = ANY(%s)""", [a["id"], targets]) or 0
    teachers = tx.val("""SELECT string_agg(DISTINCT u.name, ', ') FROM teaching_assignments ta JOIN users u ON u.id=ta.teacher_id
                         WHERE ta.class_id=%s AND ta.syllabus_id = ANY(%s)""", [class_id, ids])
    planned = tx.val("SELECT 1 FROM term_plans WHERE class_id=%s AND syllabus_id = ANY(%s) LIMIT 1", [class_id, ids])
    behind, now_ch = 0, []
    if planned:
        today_ = plans.today()
        for ch in tx.all("""SELECT p.chapter_id, p.starts_on, p.ends_on, c.code FROM term_plans p JOIN syllabus_nodes c ON c.id=p.chapter_id
                            WHERE p.class_id=%s AND p.syllabus_id = ANY(%s) ORDER BY p.starts_on""", [class_id, ids]):
            if ch["starts_on"] <= today_ <= ch["ends_on"]:
                now_ch.append(ch["code"])
            elif ch["ends_on"] < today_ and students:
                n_topics = tx.val("SELECT count(*) FROM syllabus_nodes WHERE parent_id=%s AND kind='topic'", [ch["chapter_id"]]) or 1
                got = tx.val("""SELECT count(*) FROM mastery m JOIN syllabus_nodes t ON t.id=m.topic_id AND t.parent_id=%s
                                JOIN enrolments e ON e.student_id=m.student_id AND e.class_id=%s AND e.status='active'
                                WHERE m.level >= 2""", [ch["chapter_id"], class_id]) or 0
                behind += 100 * got / (n_topics * students) < plans.ON_TRACK_AT
    enough = (u["n"] or 0) >= MIN
    pct = int(u["pct"]) if enough and u["pct"] is not None else None
    return {"book": book, "subject": book["subject"], "understanding": pct, "measured": u["n"] or 0,
            "tone": None if pct is None else ("good" if pct >= 70 else "mid" if pct >= 50 else "low"),
            "practised": round(100 * practised / (topics * students)) if topics and students else 0,
            "set_4w": set_4w, "handed_in": round(100 * done / total) if total else None, "teachers": teachers or "—",
            "planned": bool(planned), "behind": int(behind), "now": now_ch}


def school(tx: Tx, actor: Actor, grade: str | None = None, subject: str | None = None) -> dict[str, Any]:
    """Every class against every subject it studies (understanding, practised, term plan, work and hand-in), a
    summary per grade, weekly active students for eight weeks and each teacher's classes and activity. Filter by
    grade and/or subject."""
    require(actor, "leader", "inst_admin")
    now = clock.now()
    classes = tx.all("""SELECT c.id, c.label, c.grade, (SELECT count(*) FROM enrolments e WHERE e.class_id=c.id AND e.status='active') AS students
                        FROM classes c WHERE c.status='active'""")
    classes.sort(key=lambda c: (_grade_key(c["grade"]), c["label"]))
    grades = sorted({c["grade"] for c in classes}, key=_grade_key)
    subjects: set[str] = set()
    rows = []
    for c in classes:
        books = syllabus.class_subjects(tx, c["id"])
        subjects |= {b["subject"] for b in books}
        if grade and c["grade"] != grade:
            continue
        cells = {b["subject"]: _cell(tx, c["id"], c["students"], b, now) for b in books if not subject or b["subject"] == subject}
        if subject and not cells:
            continue
        rows.append({**c, "cells": cells})
    shown_subjects = [subject] if subject else sorted(subjects)
    ids = [r["id"] for r in rows]
    active = tx.all("""SELECT date_trunc('week', x.at) AS week, count(DISTINCT x.student_id) AS students FROM (
            SELECT student_id, created_at AS at FROM learn_events WHERE created_at > %(since)s
            UNION ALL SELECT student_id, submitted_at FROM attempts WHERE status='submitted' AND submitted_at > %(since)s) x
        JOIN enrolments e ON e.student_id=x.student_id AND e.status='active' AND e.class_id = ANY(%(ids)s)
        GROUP BY 1 ORDER BY 1""", {"since": now - timedelta(weeks=8), "ids": ids})
    total_students = sum(r["students"] for r in rows)
    by_grade = []
    for g in grades:
        rs = [r for r in rows if r["grade"] == g]
        if not rs:
            continue
        vals = [cell["understanding"] for r in rs for cell in r["cells"].values() if cell["understanding"] is not None]
        hand = [cell["handed_in"] for r in rs for cell in r["cells"].values() if cell["handed_in"] is not None]
        by_grade.append({"grade": g, "classes": len(rs), "students": sum(r["students"] for r in rs),
                         "understanding": round(sum(vals) / len(vals)) if vals else None,
                         "handed_in": round(sum(hand) / len(hand)) if hand else None,
                         "behind": sum(cell["behind"] for r in rs for cell in r["cells"].values())})
    cells = [cell for r in rows for cell in r["cells"].values()]
    planned = [cell for cell in cells if cell["planned"]]
    return {"rows": rows, "subjects": shown_subjects, "all_subjects": sorted(subjects), "grades": grades, "grade": grade,
            "subject": subject, "by_grade": by_grade, "teachers": teacher_activity(tx, now, ids),
            "active": [dict(a, pct=round(100 * a["students"] / total_students) if total_students else 0) for a in active],
            "students": total_students,
            "kpis": {"cells": len(cells), "on_track": sum(1 for cell in planned if not cell["behind"]), "planned": len(planned),
                     "set_4w": sum(cell["set_4w"] for cell in cells),
                     "handed_in": _mean([cell["handed_in"] for cell in cells])}}


def _mean(xs: list) -> int | None:
    vals = [x for x in xs if x is not None]
    return round(sum(vals) / len(vals)) if vals else None


def teacher_activity(tx: Tx, now: datetime, class_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Each teacher (of the shown classes): their classes and subjects, work set and decks presented in the last four
    weeks, how much of their closed work was handed in, lessons a week in their timetable, and last sign-in."""
    since = now - timedelta(days=28)
    rows = tx.all("""SELECT u.id, u.name, u.last_login_at,
            (SELECT string_agg(x.l, ' · ' ORDER BY x.l) FROM (SELECT b.subject || ' ' || string_agg(DISTINCT c.label, ', ') AS l
               FROM teaching_assignments ta JOIN classes c ON c.id=ta.class_id AND c.status='active' JOIN syllabi b ON b.id=ta.syllabus_id
               WHERE ta.teacher_id=u.id GROUP BY b.subject) x) AS teaches,
            (SELECT array_agg(DISTINCT ta.class_id) FROM teaching_assignments ta WHERE ta.teacher_id=u.id) AS class_ids,
            (SELECT count(*) FROM assignments a WHERE a.teacher_id=u.id AND a.state <> 'draft' AND a.opens_at > %(since)s) AS set_4w,
            (SELECT count(*) FROM audit_events v WHERE v.actor_id=u.id AND v.action='present.open' AND v.created_at > %(since)s) AS presented,
            (SELECT count(*) FROM timetable_slots s WHERE s.teacher_id=u.id) AS lessons_week
        FROM users u JOIN user_roles r ON r.user_id=u.id AND r.role='teacher' WHERE u.status='active' ORDER BY u.name""",
                  {"since": since})
    out = []
    for t in rows:
        if class_ids is not None and not set(t["class_ids"] or []) & set(class_ids):
            continue
        done = total = 0
        for a in tx.all("""SELECT id FROM assignments WHERE teacher_id=%s AND state <> 'draft' AND expires_at < %s AND expires_at > %s""",
                        [t["id"], now, now - timedelta(weeks=8)]):
            targets = [s["id"] for s in target_students(tx, a["id"])]
            total += len(targets)
            done += tx.val("SELECT count(DISTINCT student_id) FROM attempts WHERE assignment_id=%s AND status='submitted' AND student_id = ANY(%s)",
                           [a["id"], targets]) or 0
        t["handed_in"] = round(100 * done / total) if total else None
        out.append(t)
    return out
