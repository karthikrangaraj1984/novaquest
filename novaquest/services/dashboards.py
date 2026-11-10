"""Dashboards for teachers and leadership, and exports that match the screen (DB-1..DB-5)."""
from __future__ import annotations

import csv
import io
from datetime import timedelta
from typing import Any

from novaquest.core import clock
from novaquest.core.db import Tx
from novaquest.services import assignments, mastery, notify, plans, review, syllabus
from novaquest.services.access import Actor, require, require_class, visible_class_ids


MIN_STUDENTS = 5  # below this many students with scores, a percentage says more about one child than about a class


def teacher_home(tx: Tx, actor: Actor) -> dict[str, Any]:
    """Today (1.30.0): the classes taught today, each at its own place in the lesson plan, then what needs attention
    (each row names its class and subject; work set for several sections together is one row), then the classes."""
    require(actor, "teacher")
    from novaquest.services import today

    class_ids = visible_class_ids(tx, actor)
    items = assignments.for_teacher(tx, actor, class_ids)
    attention: list[dict[str, Any]] = []
    merged: dict[tuple, dict[str, Any]] = {}
    for a in items:
        n = a["flagged"]
        if n:
            row = {"kind": "flagged", "rank": 0, "title": f"{n} answer{'s' if n != 1 else ''} to check", "item": a,
                   "text": "The AI wasn't sure how to score these.", "action": "Check answers",
                   "link": f"/teacher/assignments/{a['id']}/review"}
        elif a["live_state"] == "closed" and not a["released_at"]:
            row = {"kind": "release", "rank": 1, "title": "Results ready to release", "item": a,
                   "text": f"{a['submitted']} of {a['targets']} handed in. Students see results after you release.",
                   "action": "Release results", "link": f"/teacher/assignments/{a['id']}/review"}
        elif a["live_state"] == "draft":
            row = {"kind": "draft", "rank": 2, "title": "Draft waiting for you", "item": a,
                   "text": "Approve the questions, then schedule it" + (
                       f" for {', '.join([a['class_label']] + a['section_labels'][:-1])} and {a['section_labels'][-1]}."
                       if a["section_labels"] else "."),
                   "action": "Approve and schedule", "link": f"/teacher/assignments/{a['id']}"}
        elif a["live_state"] == "open" and a["expires_at"] - clock.now() < timedelta(hours=24) and a["submitted"] < a["targets"]:
            row = {"kind": "due", "rank": 3, "title": f"Closes soon: {a['targets'] - a['submitted']} not handed in", "item": a,
                   "text": "Closes within a day. You can extend the deadline.", "action": "See who",
                   "link": f"/teacher/assignments/{a['id']}/review"}
        elif a["live_state"] == "open":
            row = {"kind": "open", "rank": 4, "title": f"In progress: {a['submitted']} of {a['targets']} handed in", "item": a,
                   "text": f"Closes {clock.to_ist(a['expires_at']).strftime('%a %d %b, %I:%M %p')}.", "action": "View",
                   "link": f"/teacher/assignments/{a['id']}/review"}
        else:
            continue
        row["classes"] = [a["class_label"]] + a["section_labels"]
        if a["batch_id"] and row["kind"] in ("open", "release"):  # one row for the sections set together
            key = (a["batch_id"], row["kind"])
            if key in merged:
                m = merged[key]
                m["classes"].append(a["class_label"])
                m["parts"].append(f"{a['class_label']} {a['submitted']}/{a['targets']}")
                m["title"] = ("In progress: " if row["kind"] == "open" else "Results ready to release: ") + ", ".join(m["parts"]) + " handed in"
                continue
            row["parts"] = [f"{a['class_label']} {a['submitted']}/{a['targets']}"]
            merged[key] = row
        attention.append(row)
    from novaquest.services import doubts

    asked = doubts.for_teacher(tx, actor, "open")
    if asked:  # students' questions come first: someone is stuck (1.31.0)
        labels = sorted({d["class_label"] for d in asked})
        subj = sorted({d["subject"] for d in asked if d["subject"]})
        attention.append({"kind": "doubt", "rank": -1, "title": f"{len(asked)} question{'s' if len(asked) != 1 else ''} from students",
                          "item": {"title": asked[0]["body"][:60] + ("…" if len(asked[0]["body"]) > 60 else ""), "subject": ", ".join(subj) or "—"},
                          "classes": labels, "text": "Reply in a line or two; they see it in their bell.", "action": "Reply",
                          "link": "/teacher/doubts"})
    attention.sort(key=lambda x: x["rank"])
    for row in attention:
        row["classes"] = sorted(set(row["classes"]))
    t = today.classes_today(tx, actor)
    return {"assignments": items, "attention": attention[:8], "more": max(0, len(attention) - 8), "today": t,
            "subjects": sorted({r["book"]["subject"] for r in t["teaching"]}),
            "classes": [{"id": c, "label": tx.val("SELECT label FROM classes WHERE id=%s", [c]),
                         "students": tx.val("SELECT count(*) FROM enrolments WHERE class_id=%s AND status='active'", [c]),
                         "mastery": class_mastery(tx, c)["levels"], "plan": plans.class_summary(tx, actor, c)} for c in class_ids]}


def class_mastery(tx: Tx, class_id: int, syllabus_id: int | None = None) -> dict[str, Any]:
    """Class mastery by learning outcome, weakest outcomes, top misconceptions and AI skills (DB-2, DB-5, LN-5).
    Covers one of the class's books, or all of them."""
    books = syllabus.class_syllabi(tx, class_id)
    sid = [i for b in syllabus.subjects(books) if syllabus_id is None or syllabus_id in b["ids"] for i in b["ids"]]
    rows = tx.all("""SELECT t.id AS topic_id, (CASE WHEN sb.part IS NULL THEN t.code ELSE 'P' || sb.part || '·' || t.code END) AS code,
            t.title, o.title AS outcome, c.title AS chapter,
            count(m.student_id) FILTER (WHERE m.level=3) AS mastered, count(m.student_id) FILTER (WHERE m.level=2) AS practised,
            count(m.student_id) FILTER (WHERE m.level=1) AS learning, count(m.student_id) FILTER (WHERE m.taught_ok) AS taught,
            (SELECT avg(le.score) FROM learn_events le JOIN enrolments e2 ON e2.student_id=le.student_id
              AND e2.class_id=%(c)s AND e2.status='active' WHERE le.topic_id=t.id AND le.score IS NOT NULL) AS avg_score
        FROM syllabus_nodes t JOIN syllabus_nodes c ON c.id=t.parent_id JOIN syllabi sb ON sb.id=t.syllabus_id
        LEFT JOIN syllabus_nodes o ON o.parent_id=t.id AND o.kind='outcome'
        LEFT JOIN mastery m ON m.topic_id=t.id AND m.student_id IN
            (SELECT student_id FROM enrolments WHERE class_id=%(c)s AND status='active')
        WHERE t.syllabus_id = ANY(%(s)s) AND t.kind='topic' GROUP BY t.id, sb.part, o.title, c.title, c.ord ORDER BY sb.part NULLS FIRST, t.syllabus_id, c.ord, t.ord""",
                  {"c": class_id, "s": sid})
    students = int(tx.val("SELECT count(*) FROM enrolments WHERE class_id=%s AND status='active'", [class_id]) or 0)
    active = [r for r in rows if r["avg_score"] is not None]
    weakest = sorted(active, key=lambda r: float(r["avg_score"]))[:3]
    mis = tx.all("""SELECT m.code, m.label, count(DISTINCT t.student_id) AS students FROM responses r
        JOIN attempts t ON t.id=r.attempt_id JOIN assignments a ON a.id=t.assignment_id
        JOIN LATERAL unnest(r.misconception_codes) x(code) ON true JOIN misconceptions m ON m.code=x.code
        WHERE a.class_id=%s GROUP BY m.code, m.label ORDER BY students DESC LIMIT 5""", [class_id])
    totals = {k: sum(int(r[k]) for r in rows) for k in ("mastered", "practised", "learning")}
    pairs = students * len(rows)  # every student x every topic: the whole syllabus for the whole class, not just what's started
    started_students = int(tx.val("""SELECT count(DISTINCT m.student_id) FROM mastery m JOIN syllabus_nodes t ON t.id=m.topic_id
        JOIN enrolments e ON e.student_id=m.student_id AND e.class_id=%s AND e.status='active'
        WHERE t.syllabus_id = ANY(%s) AND m.level >= 1""", [class_id, sid]) or 0)
    levels = {k: round(100 * v / pairs, 1) if pairs else 0.0 for k, v in totals.items()}
    levels["practised_or_better"] = round(levels["mastered"] + levels["practised"], 1)
    levels["started_students"], levels["students"] = started_students, students
    return {"rows": rows, "students": students, "weakest": weakest, "misconceptions": mis, "ai_skills": ai_skills(tx, [class_id]),
            "levels": levels}


def ai_skills(tx: Tx, class_ids: list[int]) -> list[dict]:
    """AI-literacy skills tracked alongside syllabus concepts (DB-5): from escape-room locks first, then any AI
    concepts in assignment questions."""
    from novaquest.services import escape

    rooms = escape.school_skill_avg(tx, class_ids)
    names = {r["concept"] for r in rooms}
    return rooms + [q for q in _question_ai_skills(tx, class_ids) if q["concept"] not in names]


def _question_ai_skills(tx: Tx, class_ids: list[int]) -> list[dict]:
    return tx.all("""SELECT q.ai_concept AS concept, round(100 * sum(coalesce(r.teacher_score, r.ai_score)) / nullif(sum(r.max_score), 0)) AS pct,
            count(DISTINCT t.student_id) AS students
        FROM responses r JOIN questions q ON q.id=r.question_id JOIN attempts t ON t.id=r.attempt_id
        JOIN assignments a ON a.id=t.assignment_id
        WHERE q.ai_concept IS NOT NULL AND a.class_id = ANY(%s) AND r.max_score IS NOT NULL
        GROUP BY q.ai_concept ORDER BY pct""", [class_ids])


def assignment_results(tx: Tx, actor: Actor, assignment_id: int) -> dict[str, Any]:
    """Per-assignment results, completion and per-student drill-down links (DB-1)."""
    a = assignments.get(tx, assignment_id)
    require_class(tx, actor, a["class_id"])
    students = tx.all("""SELECT u.id, u.name, e.roll_no, t.id AS attempt_id, t.status, t.submitted_at,
            sum(coalesce(r.teacher_score, r.ai_score)) AS got, sum(r.max_score) AS max
        FROM enrolments e JOIN users u ON u.id=e.student_id
        LEFT JOIN attempts t ON t.student_id=u.id AND t.assignment_id=%s
        LEFT JOIN responses r ON r.attempt_id=t.id
        WHERE u.id = ANY(%s::bigint[]) AND e.status='active'
        GROUP BY u.id, e.roll_no, t.id ORDER BY length(e.roll_no), e.roll_no""",
                        [assignment_id, [s["id"] for s in assignments.target_students(tx, assignment_id)]])
    for s in students:
        s["pct"] = round(100 * float(s["got"]) / float(s["max"])) if s["max"] else None
    return {"assignment": a, "summary": review.class_summary(tx, assignment_id), "students": students,
            "misconceptions": review.misconception_counts(tx, assignment_id)}


def leadership(tx: Tx, actor: Actor) -> dict[str, Any]:
    """All classes on one screen: class, subject and teacher comparison, 8-week trend, engagement (DB-3)."""
    require(actor, "leader", "inst_admin")
    now = clock.now()
    classes = tx.all("""SELECT c.id, c.label, coalesce(string_agg(DISTINCT u.name, ', '), '—') AS teachers,
            (SELECT count(*) FROM enrolments e WHERE e.class_id=c.id AND e.status='active') AS students,
            (SELECT round(100 * avg(le.score)) FROM learn_events le JOIN enrolments e ON e.student_id=le.student_id
              AND e.class_id=c.id AND e.status='active' WHERE le.score IS NOT NULL AND le.created_at > %(since)s) AS understanding,
            (SELECT count(DISTINCT le.student_id) FROM learn_events le JOIN enrolments e ON e.student_id=le.student_id
              AND e.class_id=c.id AND e.status='active' WHERE le.score IS NOT NULL AND le.created_at > %(since)s) AS measured,
            (SELECT count(DISTINCT le.student_id) FROM learn_events le JOIN enrolments e ON e.student_id=le.student_id
              AND e.class_id=c.id AND e.status='active' WHERE le.created_at > %(week)s) AS active_week
        FROM classes c LEFT JOIN teaching_assignments ta ON ta.class_id=c.id LEFT JOIN users u ON u.id=ta.teacher_id
        WHERE c.status='active' GROUP BY c.id ORDER BY understanding NULLS FIRST, c.label""",
                     {"since": now - timedelta(days=28), "week": now - timedelta(days=7)})
    ot = _on_time(tx, now)
    for c in classes:
        c["on_time"], c["closed"] = ot.get(c["id"], (None, 0))
        c["active_pct"] = round(100 * c["active_week"] / c["students"]) if c["students"] else 0
        if c["measured"] < MIN_STUDENTS:  # one keen student is not 'the class': say there isn't enough yet
            c["understanding"] = None
    trend = tx.all("""SELECT date_trunc('week', created_at) AS week, round(100 * avg(score)) AS pct,
            count(DISTINCT student_id) AS students FROM learn_events
        WHERE score IS NOT NULL AND created_at > %s GROUP BY 1 HAVING count(DISTINCT student_id) >= %s ORDER BY 1""",
                   [now - timedelta(weeks=8), MIN_STUDENTS])
    teachers = tx.all("""SELECT u.name, count(DISTINCT a.id) AS assignments,
            count(DISTINCT q.id) FILTER (WHERE q.status='approved' AND q.origin='ai') AS approved_ai,
            count(r.id) FILTER (WHERE r.reviewed_at IS NOT NULL AND r.teacher_score <> r.ai_score) AS overrides
        FROM users u JOIN user_roles ur ON ur.user_id=u.id AND ur.role='teacher'
        LEFT JOIN assignments a ON a.teacher_id=u.id LEFT JOIN questions q ON q.created_by=u.id
        LEFT JOIN attempts t ON t.assignment_id=a.id LEFT JOIN responses r ON r.attempt_id=t.id
        GROUP BY u.id ORDER BY u.name""")
    not_completing = tx.all("""SELECT u.id, u.name, c.label, count(*) AS missed FROM assignments a
        JOIN assignment_targets tg ON tg.assignment_id=a.id AND tg.target_kind='class'
        JOIN enrolments e ON e.class_id=tg.target_id AND e.status='active' JOIN users u ON u.id=e.student_id
        JOIN classes c ON c.id=e.class_id
        WHERE a.expires_at < %s AND a.state <> 'draft'
          AND NOT EXISTS (SELECT 1 FROM attempts t WHERE t.assignment_id=a.id AND t.student_id=u.id AND t.status='submitted')
        GROUP BY u.id, c.label HAVING count(*) >= 1 ORDER BY missed DESC, u.name LIMIT 15""", [now])
    topics = tx.all("""SELECT c.title AS chapter, round(100 * avg(le.score)) AS pct, count(DISTINCT le.student_id) AS students
        FROM learn_events le JOIN syllabus_nodes t ON t.id=le.topic_id JOIN syllabus_nodes c ON c.id=t.parent_id
        WHERE le.score IS NOT NULL GROUP BY c.id, c.title HAVING count(DISTINCT le.student_id) >= %s ORDER BY pct LIMIT 10""",
                    [MIN_STUDENTS])
    total_students = sum(c["students"] for c in classes)
    active = sum(c["active_week"] for c in classes)
    return {"classes": classes, "trend": trend, "teachers": teachers, "not_completing": not_completing, "chapters": topics,
            "ai_skills": ai_skills(tx, [c["id"] for c in classes]),
            "min_students": MIN_STUDENTS, "closed": sum(c["closed"] for c in classes),
            "kpis": {"students": total_students, "active_pct": round(100 * active / total_students) if total_students else 0,
                     "understanding": _avg([c["understanding"] for c in classes]), "on_time": _avg([c["on_time"] for c in classes])}}


def _on_time(tx: Tx, now) -> dict[int, tuple[int | None, int]]:
    """Class id -> (% of students handed in on time over closed assignments of the last 8 weeks, how many closed).
    Counts every student an assignment was set for, not just those who started, and never counts open assignments."""
    out: dict[int, list[int]] = {}
    for a in tx.all("""SELECT id, class_id, expires_at FROM assignments WHERE state <> 'draft' AND expires_at < %s AND expires_at > %s""",
                    [now, now - timedelta(weeks=8)]):
        targets = [s["id"] for s in assignments.target_students(tx, a["id"])]
        if not targets:
            continue
        done = tx.val("""SELECT count(DISTINCT t.student_id) FROM attempts t
            LEFT JOIN assignment_extensions x ON x.assignment_id=t.assignment_id AND x.student_id=t.student_id
            WHERE t.assignment_id=%s AND t.status='submitted' AND t.submitted_at <= greatest(%s, coalesce(x.expires_at, %s))
              AND t.student_id = ANY(%s)""", [a["id"], a["expires_at"], a["expires_at"], targets])
        tally = out.setdefault(a["class_id"], [0, 0, 0])
        tally[0] += int(done)
        tally[1] += len(targets)
        tally[2] += 1
    return {cid: (round(100 * d / t) if t else None, n) for cid, (d, t, n) in out.items()}


def _avg(xs: list) -> int | None:
    vals = [float(x) for x in xs if x is not None]
    return round(sum(vals) / len(vals)) if vals else None


def leadership_csv(data: dict[str, Any]) -> str:
    """CSV export with the same columns as the leadership screen (DB-4)."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Class", "Teachers", "Students", "Understanding % (4 weeks)", "Active this week %", "On time %"])
    for c in data["classes"]:
        w.writerow([c["label"], c["teachers"], c["students"], c["understanding"] if c["understanding"] is not None else "",
                    c["active_pct"], c["on_time"] if c["on_time"] is not None else ""])
    w.writerow([])
    w.writerow(["Students not completing", "Class", "Assignments missed"])
    for s in data["not_completing"]:
        w.writerow([s["name"], s["label"], s["missed"]])
    return out.getvalue()


def class_csv(data: dict[str, Any], label: str) -> str:
    """CSV export of class mastery matching the teacher screen (DB-4)."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f"Class {label}", "Topic", "Learning outcome", "Mastered", "Practised", "Learning", "Taught NOVA", "Average %"])
    for r in data["rows"]:
        w.writerow([r["chapter"], f"{r['code']} {r['title']}", r["outcome"] or "", r["mastered"], r["practised"], r["learning"],
                    r["taught"], round(100 * float(r["avg_score"])) if r["avg_score"] is not None else ""])
    return out.getvalue()


def student_overview(tx: Tx, actor: Actor) -> dict[str, Any]:
    """Student progress view: mastery, AI skills, taught topics, released results (TA-7, SX-5)."""
    require(actor, "student")
    levels = mastery.for_student(tx, actor.user_id)
    books = syllabus.student_subjects(tx, actor.user_id)
    chapters = []
    for book in books:
        for ch in syllabus.subject_tree(tx, book):
            tl = [levels.get(t["id"], {}).get("level", 0) for t in ch["topics"]]
            if any(tl):
                chapters.append({"title": ch["title"], "no": ch["code"], "part": ch["part"], "mastered": sum(1 for x in tl if x == 3), "total": len(tl),
                                 "practised": sum(1 for x in tl if x == 2), "learning": sum(1 for x in tl if x == 1),
                                 "started": sum(1 for x in tl if x), "book": book["label"] if len(books) > 1 else None})
    taught = tx.all("""SELECT t.title, s.scores, s.summary, s.ended_at FROM teach_sessions s JOIN syllabus_nodes t ON t.id=s.topic_id
                       WHERE s.student_id=%s AND s.status='passed' ORDER BY s.ended_at DESC""", [actor.user_id])
    released = [a for a in assignments.for_student(tx, actor.user_id) if a["view"] == "results"]
    unread = notify.unread_count(tx, actor.user_id)
    done = tx.one("""SELECT count(DISTINCT topic_id) FILTER (WHERE kind='practice') AS practised,
                            count(DISTINCT topic_id) FILTER (WHERE kind='pictures') AS pictures,
                            count(DISTINCT topic_id) FILTER (WHERE kind='understanding') AS checked
                     FROM learn_events WHERE student_id=%s""", [actor.user_id])
    done = {k: int(v or 0) for k, v in done.items()} | {"taught": len(taught)}
    return {"chapters": chapters, "taught": taught, "released": released, "unread": unread, "done": done,
            "streak": week_streak(tx, actor.user_id), "plan": mastery.revision_plan(tx, actor.user_id, 5)}


def week_streak(tx: Tx, student_id: int) -> int:
    """Weeks in a row (ending this week or last week) in which the student did some learning. Weeks, not days, so a
    weekend off never breaks it."""
    weeks = [r["w"] for r in tx.all("""SELECT DISTINCT date_trunc('week', created_at)::date AS w FROM learn_events
                                       WHERE student_id=%s AND created_at > %s - interval '1 year' ORDER BY w DESC""",
                                    [student_id, clock.now()])]
    if not weeks:
        return 0
    today = clock.now().date()
    this_week = today - timedelta(days=today.weekday())
    if weeks[0] < this_week - timedelta(days=7):
        return 0
    n, want = 0, weeks[0]
    for w in weeks:
        if w != want:
            break
        n, want = n + 1, want - timedelta(days=7)
    return n


# ---------- one student, and the class grid (teacher review, items 8 and 9) ----------
def student_profile(tx: Tx, actor: Actor, student_id: int) -> dict[str, Any]:
    """Everything a teacher needs about one student on one page: topics and levels, assignments (handed in, scores,
    missed), what they mixed up, their questions to NOVA, and when they last signed in."""
    from novaquest.services.access import student_class_id

    class_id = student_class_id(tx, student_id)
    if class_id is None:
        from novaquest.core.errors import NotFound
        raise NotFound("That student was not found.")
    require_class(tx, actor, class_id)
    who = tx.one("""SELECT u.id, u.name, u.last_login_at, e.roll_no, e.consent_status, c.label AS class_label, c.id AS class_id
                    FROM users u JOIN enrolments e ON e.student_id=u.id AND e.status='active' JOIN classes c ON c.id=e.class_id
                    WHERE u.id=%s""", [student_id])
    levels = mastery.for_student(tx, student_id)
    last = {r["topic_id"]: r for r in tx.all("""SELECT DISTINCT ON (topic_id) topic_id, kind, score, created_at FROM learn_events
                                               WHERE student_id=%s AND score IS NOT NULL ORDER BY topic_id, created_at DESC""",
                                            [student_id])}
    books = []
    counts = [0, 0, 0, 0]
    for book in syllabus.class_subjects(tx, class_id):
        chapters = []
        for ch in syllabus.subject_tree(tx, book):
            rows = []
            for t in ch["topics"]:
                lv = levels.get(t["id"], {}).get("level", 0)
                counts[lv] += 1
                if lv or t["id"] in last:
                    ev = last.get(t["id"])
                    rows.append({"id": t["id"], "code": syllabus.part_code(t), "title": t["title"], "level": lv,
                                 "taught": levels.get(t["id"], {}).get("taught_ok", False),
                                 "last_kind": ev["kind"] if ev else None, "last_at": ev["created_at"] if ev else None,
                                 "last_score": round(100 * float(ev["score"])) if ev else None})
            if rows:
                chapters.append({"no": ch["code"], "title": ch["title"], "part": ch["part"], "topics": rows})
        books.append({"label": book["label"], "chapters": chapters})
    work = []
    now = clock.now()
    for a in assignments.for_student(tx, student_id):
        res = tx.one("""SELECT t.status, t.submitted_at, sum(coalesce(r.teacher_score, r.ai_score)) AS got, sum(r.max_score) AS max
                        FROM attempts t LEFT JOIN responses r ON r.attempt_id=t.id
                        WHERE t.assignment_id=%s AND t.student_id=%s GROUP BY t.id ORDER BY t.attempt_no DESC LIMIT 1""",
                     [a["id"], student_id])
        done = bool(res and res["status"] == "submitted")
        missed = not done and a["student_expires_at"] <= now
        pct = round(100 * float(res["got"]) / float(res["max"])) if done and res["max"] else None
        work.append({"id": a["id"], "title": a["title"], "kind": a["kind"], "due": a["student_expires_at"], "done": done,
                     "missed": missed, "open": a["view"] == "open", "pct": pct, "released": bool(a["released_at"])})
    work.sort(key=lambda w: w["due"], reverse=True)
    mixups = tx.all("""SELECT m.label, count(*) AS times FROM responses r JOIN attempts t ON t.id=r.attempt_id
                       JOIN LATERAL unnest(r.misconception_codes) x(code) ON true JOIN misconceptions m ON m.code=x.code
                       WHERE t.student_id=%s GROUP BY m.label ORDER BY times DESC LIMIT 6""", [student_id])
    asked = tx.all("""SELECT m.body, m.created_at, n.title AS topic FROM tutor_messages m LEFT JOIN syllabus_nodes n ON n.id=m.topic_id
                      WHERE m.student_id=%s AND m.role='student' AND m.context='learn' ORDER BY m.created_at DESC LIMIT 6""",
                   [student_id])
    taught = tx.val("SELECT count(*) FROM teach_sessions WHERE student_id=%s AND status='passed'", [student_id])
    handed = sum(1 for w in work if w["done"])
    due = sum(1 for w in work if w["done"] or w["missed"])
    return {"student": who, "books": books, "needs": needs_improvement(tx, student_id, class_id),
            "counts": {"mastered": counts[3], "practised": counts[2], "learning": counts[1], "total": sum(counts)},
            "work": work[:12], "handed": handed, "due": due, "missed": [w for w in work if w["missed"]],
            "mixups": mixups, "asked": asked, "taught": taught}


WEAK = 0.6  # below this share of the marks, a topic needs work


def needs_improvement(tx: Tx, student_id: int, class_id: int, per_book: int = 6) -> list[dict[str, Any]]:
    """For each book (subject) the class studies, the topics this student most needs to work on, worst first, each with
    the reasons in plain words: a weak score on the topic's questions in an assignment ('Scored 40% in Cell Lab'), a
    weak last practice or check, a mix-up seen in their answers, or a topic the class was set work on that they never
    started. Topics they have Mastered are left out. Three queries plus one per book."""
    levels = mastery.for_student(tx, student_id)
    reasons: dict[int, list[tuple[float, str]]] = {}

    def add(topic_id: int, worst: float, text: str) -> None:
        reasons.setdefault(topic_id, []).append((worst, text))

    for r in tx.all("""SELECT DISTINCT ON (a.id, q.topic_id) a.id, a.title, q.topic_id,
               sum(coalesce(r.teacher_score, r.ai_score)) OVER w AS got, sum(r.max_score) OVER w AS mx
            FROM attempts t JOIN assignments a ON a.id=t.assignment_id JOIN responses r ON r.attempt_id=t.id
            JOIN questions q ON q.id=r.question_id
            WHERE t.student_id=%s AND t.status='submitted' AND q.kind NOT IN ('learn', 'teach', 'escape')
            WINDOW w AS (PARTITION BY t.id, q.topic_id)
            ORDER BY a.id, q.topic_id, t.attempt_no DESC""", [student_id]):
        if r["mx"] and float(r["got"] or 0) / float(r["mx"]) < WEAK:
            pct = round(100 * float(r["got"] or 0) / float(r["mx"]))
            add(r["topic_id"], pct / 100, f"Scored {pct}% in {r['title']}")
    names = {"practice": "practice", "check": "quick check", "understanding": "understanding check", "pictures": "picture quiz"}
    for r in tx.all("""SELECT DISTINCT ON (topic_id, kind) topic_id, kind, score FROM learn_events
                       WHERE student_id=%s AND score IS NOT NULL AND kind = ANY(%s)
                       ORDER BY topic_id, kind, created_at DESC""", [student_id, list(names)]):
        if float(r["score"]) < WEAK:
            add(r["topic_id"], float(r["score"]), f"Last {names[r['kind']]}: {round(100 * float(r['score']))}%")
    for r in tx.all("""SELECT m.topic_id, m.label, count(*) AS times FROM responses r JOIN attempts t ON t.id=r.attempt_id
                       JOIN LATERAL unnest(r.misconception_codes) x(code) ON true JOIN misconceptions m ON m.code=x.code
                       WHERE t.student_id=%s AND m.topic_id IS NOT NULL GROUP BY m.topic_id, m.label""", [student_id]):
        add(r["topic_id"], 0.5, f"Mixed up: {r['label']}" + (f" ({r['times']}×)" if r["times"] > 1 else ""))
    started = {r["topic_id"] for r in tx.all("SELECT DISTINCT topic_id FROM learn_events WHERE student_id=%s", [student_id])}
    for r in tx.all("""SELECT DISTINCT unnest(a.topic_ids) AS topic_id FROM assignments a
                       JOIN assignment_targets g ON g.assignment_id=a.id AND ((g.target_kind='class' AND g.target_id=%s)
                         OR (g.target_kind='student' AND g.target_id=%s))
                       WHERE a.state <> 'draft' AND a.opens_at <= %s""", [class_id, student_id, clock.now()]):
        if r["topic_id"] not in started and levels.get(r["topic_id"], {}).get("level", 0) == 0:
            add(r["topic_id"], 0.9, "Not started yet: the class has had work on it")
    out = []
    for book in syllabus.class_subjects(tx, class_id):
        items = []
        for ch in syllabus.subject_tree(tx, book):
            for t in ch["topics"]:
                why = reasons.get(t["id"])
                lv = levels.get(t["id"], {}).get("level", 0)
                if not why or lv >= 3:
                    continue
                items.append({"id": t["id"], "code": syllabus.part_code(t), "title": t["title"], "chapter": ch["title"], "level": lv,
                              "worst": min(w for w, _ in why), "reasons": list(dict.fromkeys(x for _, x in sorted(why)))[:3]})
        items.sort(key=lambda i: (i["worst"], i["level"], -len(i["reasons"])))
        out.append({"book": book, "topics": items[:per_book], "more": max(0, len(items) - per_book)})
    return out


def class_grid(tx: Tx, actor: Actor, class_id: int, syllabus_id: int | None = None, chapter_id: int | None = None) -> dict[str, Any]:
    """Students down the side, one chapter's topics across the top, each cell the student's level for that topic,
    so a teacher sees at a glance who is struggling with what. Defaults to the chapter in class now (term plan), else
    the chapter with the latest activity."""
    require_class(tx, actor, class_id)
    books = syllabus.class_subjects(tx, class_id)
    book = syllabus.pick(books, syllabus_id)
    if not book:
        return {"books": books, "book": None, "chapters": [], "chapter": None, "students": [], "topics": [], "cells": {}}
    tree = syllabus.subject_tree(tx, book)
    students = tx.all("""SELECT u.id, u.name, e.roll_no FROM enrolments e JOIN users u ON u.id=e.student_id
                         WHERE e.class_id=%s AND e.status='active' ORDER BY length(e.roll_no), e.roll_no""", [class_id])
    ids = [s["id"] for s in students]
    if chapter_id is None:  # the chapter in class now (term plan), else the one with the latest activity
        chapter_id = next((p["chapter_id"] for p in plans.in_class_now(tx, class_id) if p["syllabus_id"] in book["ids"]), None)
    if chapter_id is None:
        recent = tx.val("""SELECT n.parent_id FROM learn_events le JOIN syllabus_nodes n ON n.id=le.topic_id
                           WHERE le.student_id = ANY(%s) AND n.syllabus_id = ANY(%s) ORDER BY le.created_at DESC LIMIT 1""",
                        [ids, book["ids"]])
        chapter_id = recent or (tree[0]["id"] if tree else None)
    chapter = next((c for c in tree if c["id"] == chapter_id), tree[0] if tree else None)
    topics = chapter["topics"] if chapter else []
    cells = {(r["student_id"], r["topic_id"]): r["level"] for r in tx.all(
        "SELECT student_id, topic_id, level FROM mastery WHERE student_id = ANY(%s) AND topic_id = ANY(%s)",
        [ids, [t["id"] for t in topics]])}
    for s in students:
        lv = [cells.get((s["id"], t["id"]), 0) for t in topics]
        s["behind"] = sum(1 for x in lv if x < 2)
    return {"books": books, "book": book, "chapters": [{"id": c["id"], "code": c["code"], "title": c["title"], "part": c["part"]} for c in tree],
            "chapter": chapter, "students": students, "topics": topics, "cells": cells}


def revision_topics(tx: Tx, actor: Actor, class_id: int, chapter_id: int, student_ids: list[int], limit: int = 3) -> list[int]:
    """The chapter's topics the chosen students most need to revise (below Practised for the most of them)."""
    require_class(tx, actor, class_id, write=True)
    topics = [r["id"] for r in tx.all("SELECT id FROM syllabus_nodes WHERE parent_id=%s AND kind='topic' ORDER BY ord", [chapter_id])]
    if not topics or not student_ids:
        return []
    have = {(r["student_id"], r["topic_id"]): r["level"] for r in tx.all(
        "SELECT student_id, topic_id, level FROM mastery WHERE student_id = ANY(%s) AND topic_id = ANY(%s)", [student_ids, topics])}
    need = sorted(topics, key=lambda t: -sum(1 for s in student_ids if have.get((s, t), 0) < 2))
    return [t for t in need if any(have.get((s, t), 0) < 2 for s in student_ids)][:limit] or topics[:1]
