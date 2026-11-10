"""Weekly class briefing for teachers (1.18.0): the last seven days of a class in one page.

What the class mixed up most, the topics it found hardest, which students are slipping and why, and a ten-minute
reteach activity for the week's biggest problem, built from the topic's approved lesson, its mix-up and an approved
question. Everything comes from data NovaQuest already has, so a briefing never calls Claude. Each Monday from 7 am
(Indian time) every teacher gets a bell notification and an email per class, once."""
from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from typing import Any

from novaquest.core import clock
from novaquest.core.db import Tx
from novaquest.services import daily, notify, principal
from novaquest.services.access import Actor, require_class

DAYS = 7
BEFORE = 21        # days before the week that a student's usual level is measured over
DROP = 15          # a fall of this many points against their usual level counts as slipping
MIN_SCORES = 2     # scored activities needed in each window before a drop counts
WEAK = 60          # a topic below this class average (%) this week is hard
TOP = 3


def _window(now: datetime | None = None) -> tuple[datetime, datetime]:
    end = now or clock.now()
    return end - timedelta(days=DAYS), end


def _students(tx: Tx, class_id: int) -> list[dict]:
    return tx.all("""SELECT u.id, u.name, e.roll_no FROM enrolments e JOIN users u ON u.id=e.student_id
                     WHERE e.class_id=%s AND e.status='active' ORDER BY length(e.roll_no), e.roll_no""", [class_id])


def _mixups(tx: Tx, class_id: int, ids: list[int], start: datetime, end: datetime) -> list[dict]:
    """Mix-ups seen this week in the class's answers to assignments, most students first."""
    return tx.all("""SELECT m.code, m.label, m.topic_id, m.data, n.title AS topic, count(DISTINCT t.student_id) AS students
        FROM responses r JOIN attempts t ON t.id=r.attempt_id JOIN assignments a ON a.id=t.assignment_id
        JOIN questions q ON q.id=r.question_id
        JOIN LATERAL unnest(r.misconception_codes) x(code) ON true
        JOIN misconceptions m ON m.code=x.code AND m.syllabus_id=q.syllabus_id
        JOIN syllabus_nodes n ON n.id=m.topic_id
        WHERE a.class_id=%s AND t.student_id = ANY(%s) AND t.submitted_at >= %s AND t.submitted_at < %s
        GROUP BY m.code, m.label, m.topic_id, m.data, n.title ORDER BY students DESC, m.code LIMIT %s""",
                  [class_id, ids, start, end, TOP])


def _hard_topics(tx: Tx, ids: list[int], start: datetime, end: datetime) -> list[dict]:
    """Topics the class scored lowest on this week (practice, checks, Daily 5, assignments), with at least two
    students' work behind the average."""
    rows = tx.all("""SELECT s.topic_id, n.title, c.code AS chapter, round(100 * avg(s.score)) AS pct,
            count(DISTINCT s.student_id) AS students FROM (
              SELECT student_id, topic_id, score FROM learn_events
               WHERE student_id = ANY(%(ids)s) AND score IS NOT NULL AND created_at >= %(s)s AND created_at < %(e)s
              UNION ALL
              SELECT t.student_id, q.topic_id, sum(coalesce(r.teacher_score, r.ai_score)) / nullif(sum(r.max_score), 0)
                FROM responses r JOIN attempts t ON t.id=r.attempt_id JOIN questions q ON q.id=r.question_id
               WHERE t.student_id = ANY(%(ids)s) AND t.status='submitted' AND t.submitted_at >= %(s)s AND t.submitted_at < %(e)s
                 AND r.max_score IS NOT NULL AND q.kind NOT IN ('learn', 'teach', 'escape')
               GROUP BY t.id, t.student_id, q.topic_id) s
        JOIN syllabus_nodes n ON n.id=s.topic_id JOIN syllabus_nodes c ON c.id=n.parent_id
        WHERE s.score IS NOT NULL GROUP BY s.topic_id, n.title, c.code
        HAVING count(DISTINCT s.student_id) >= 2 ORDER BY pct, students DESC""", {"ids": ids, "s": start, "e": end})
    return [r for r in rows if r["pct"] is not None and int(r["pct"]) < WEAK][:TOP]


def _slipping(tx: Tx, class_id: int, students: list[dict], start: datetime, end: datetime) -> list[dict]:
    """Students to check on, each with the reasons in plain words: missed work that closed this week, a fall in their
    scores against their usual level, or no activity this week after being active before."""
    ids = [s["id"] for s in students]
    before = start - timedelta(days=BEFORE)
    why: dict[int, list[str]] = {}
    missed = tx.all("""SELECT a.id, a.title, g.student_id FROM assignments a
        JOIN LATERAL (SELECT e.student_id FROM enrolments e JOIN assignment_targets tg ON tg.assignment_id=a.id
                       AND ((tg.target_kind='class' AND tg.target_id=e.class_id) OR (tg.target_kind='student' AND tg.target_id=e.student_id))
                      WHERE e.class_id=%s AND e.status='active') g ON true
        WHERE a.class_id=%s AND a.state <> 'draft' AND a.expires_at >= %s AND a.expires_at < %s
          AND NOT EXISTS (SELECT 1 FROM attempts t WHERE t.assignment_id=a.id AND t.student_id=g.student_id AND t.status='submitted')""",
                    [class_id, class_id, start, end])
    by: dict[int, list[str]] = {}
    for m in missed:
        by.setdefault(m["student_id"], []).append(m["title"])
    for sid, titles in by.items():
        why.setdefault(sid, []).append(f"Didn't hand in {titles[0]}" + (f" and {len(titles) - 1} more" if len(titles) > 1 else ""))
    scores = tx.all("""SELECT student_id, this_week, round(100 * avg(score)) AS pct, count(*) AS n
        FROM (SELECT student_id, score, created_at >= %s AS this_week FROM learn_events
              WHERE student_id = ANY(%s) AND score IS NOT NULL AND created_at >= %s AND created_at < %s) x
        GROUP BY student_id, this_week""", [start, ids, before, end])
    sc = {(r["student_id"], r["this_week"]): r for r in scores}
    for sid in ids:
        now_, usual = sc.get((sid, True)), sc.get((sid, False))
        if now_ and usual and now_["n"] >= MIN_SCORES and usual["n"] >= MIN_SCORES and int(usual["pct"]) - int(now_["pct"]) >= DROP:
            why.setdefault(sid, []).append(f"Scores fell to {now_['pct']}% this week (usually {usual['pct']}%)")
    active = {r["student_id"]: r["this_week"] for r in tx.all(
        """SELECT student_id, bool_or(created_at >= %s) AS this_week FROM learn_events
           WHERE student_id = ANY(%s) AND created_at >= %s AND created_at < %s GROUP BY student_id""", [start, ids, before, end])}
    for sid, this_week in active.items():
        if not this_week:
            why.setdefault(sid, []).append("No activity this week (active the weeks before)")
    names = {s["id"]: s for s in students}
    out = [{**names[sid], "reasons": r} for sid, r in why.items() if sid in names]
    out.sort(key=lambda s: (-len(s["reasons"]), len(s["roll_no"]), s["roll_no"]))
    return out


def _activity(tx: Tx, ids: list[int], start: datetime, end: datetime) -> dict[str, int]:
    r = tx.one("""SELECT count(DISTINCT student_id) AS active,
            count(DISTINCT (student_id, created_at::date)) FILTER (WHERE kind='review') AS reviews
        FROM learn_events WHERE student_id = ANY(%s) AND created_at >= %s AND created_at < %s""", [ids, start, end]) or {}
    handed = tx.val("""SELECT count(*) FROM attempts WHERE student_id = ANY(%s) AND status='submitted'
                       AND submitted_at >= %s AND submitted_at < %s""", [ids, start, end])
    return {"students": len(ids), "active": int(r.get("active") or 0), "daily": int(r.get("reviews") or 0), "handed": int(handed or 0)}


def _first_sentences(text: str, n: int = 2, limit: int = 320) -> str:
    parts = [p.strip() for p in str(text or "").replace("\n", " ").split(". ") if p.strip()]
    out = ". ".join(parts[:n])
    out = out if out.endswith((".", "?", "!")) else out + "."
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + "…"


def reteach(tx: Tx, topic_id: int, mixup: dict | None = None, seed: int = 0) -> dict[str, Any]:
    """A ten-minute activity for the start of a lesson: hook, put it right, check, exit ticket. Built from the topic's
    approved lesson, the mix-up (if the week's problem was one) and an approved question (else one from the book)."""
    title = tx.val("SELECT title FROM syllabus_nodes WHERE id=%s", [topic_id])
    lesson = (tx.val("SELECT data FROM lessons WHERE node_id=%s AND status='approved'", [topic_id]) or {})
    m = (mixup or {}).get("data") or {}
    steps = []
    label = (mixup or {}).get("label")
    if label:
        claim = label.rstrip(".") + "."
        steps.append({"min": 2, "name": "Hook", "text": f"Write on the board: “{claim}” Ask for hands up: who agrees? "
                      "Take two or three reasons from each side without saying who is right."})
    else:
        steps.append({"min": 2, "name": "Hook", "text": f"Ask the class to tell a partner, in one sentence, what they know about "
                      f"{title}. Take three answers and write the key words on the board."})
    right = m.get("correct_idea") or _first_sentences(lesson.get("explain", ""))
    example = lesson.get("example")
    if example:
        example = re.sub(r"^\s*(worked\s+)?example\s*[:.-]\s*", "", str(example), flags=re.I)
    if right and right != ".":
        steps.append({"min": 4, "name": "Put it right", "text": right + (f" Example: {_first_sentences(example, 2)}" if example else "")})
    else:
        steps.append({"min": 4, "name": "Put it right", "text": f"Re-explain {title} using the textbook's main diagram; ask "
                      "students to label it with you."})
    qs = daily._questions(tx, topic_id, random.Random(seed or topic_id))
    q = qs[0] if qs else None
    if q:
        steps.append({"min": 3, "name": "Check", "text": "Read the question aloud; students show A, B, C or D on their fingers. "
                      "Ask someone who chose each wrong answer what made it look right.",
                      "question": q["prompt"], "options": q["options"], "answer": q["answer"]})
    elif m.get("probe"):
        steps.append({"min": 3, "name": "Check", "text": f"Ask pairs: {m['probe']} Take two answers."})
    steps.append({"min": 1, "name": "Exit ticket", "text": (m.get("probe") if m.get("probe") and q else None)
                  or f"Each student writes one sentence on {title} in their own words. Collect and sort into got it / not yet."})
    return {"topic_id": topic_id, "topic": title, "mixup": (mixup or {}).get("label"), "steps": steps,
            "minutes": sum(s["min"] for s in steps)}


def weekly(tx: Tx, actor: Actor | None, class_id: int, now: datetime | None = None) -> dict[str, Any]:
    """The briefing for one class over the seven days up to now. actor None: the Monday job (no access check)."""
    if actor is not None:
        require_class(tx, actor, class_id)
    start, end = _window(now)
    students = _students(tx, class_id)
    ids = [s["id"] for s in students]
    mix = _mixups(tx, class_id, ids, start, end)
    hard = _hard_topics(tx, ids, start, end)
    slipping = _slipping(tx, class_id, students, start, end)
    focus = None
    if mix:
        focus = reteach(tx, mix[0]["topic_id"], mix[0], seed=class_id)
    elif hard:
        focus = reteach(tx, hard[0]["topic_id"], seed=class_id)
    for x in mix:
        x.pop("data", None)
    return {"from": clock.to_ist(start), "to": clock.to_ist(end), "activity": _activity(tx, ids, start, end), "mixups": mix,
            "hard": hard, "slipping": slipping, "focus": focus,
            "quiet": not (mix or hard or slipping)}


def text(label: str, b: dict[str, Any]) -> tuple[str, str]:
    """(title, body) of the Monday message for one class."""
    a = b["activity"]
    lines = [f"{a['active']} of {a['students']} students were active; {a['handed']} pieces of work handed in."]
    if b["mixups"]:
        lines.append("Top mix-ups: " + "; ".join(f"{m['label']} ({m['students']} students)" for m in b["mixups"]) + ".")
    if b["hard"]:
        lines.append("Hardest topics: " + "; ".join(f"{h['title']} ({h['pct']}%)" for h in b["hard"]) + ".")
    if b["slipping"]:
        lines.append(f"Students to check on: " + ", ".join(s["name"] for s in b["slipping"][:6])
                     + (f" and {len(b['slipping']) - 6} more" if len(b["slipping"]) > 6 else "") + ".")
    if b["focus"]:
        lines.append(f"A {b['focus']['minutes']}-minute reteach activity on {b['focus']['topic']} is ready in the briefing.")
    if b["quiet"]:
        lines.append("No mix-ups, hard topics or students slipping this week.")
    return f"Weekly briefing: class {label}", " ".join(lines)


def send_weekly(tx: Tx) -> int:
    """Each Monday from 7 am, each teacher's briefing for each of their classes, once (bell and email)."""
    key = principal.week_key()
    if key is None:
        return 0
    pairs = tx.all("""SELECT DISTINCT ta.teacher_id, ta.class_id, c.label, u.email FROM teaching_assignments ta
                      JOIN classes c ON c.id=ta.class_id AND c.status='active' JOIN users u ON u.id=ta.teacher_id AND u.status='active'
                      ORDER BY ta.class_id""")
    cache: dict[int, tuple[str, str]] = {}
    n = 0
    for p in pairs:
        dk = f"briefing:{key}:{p['class_id']}"
        if tx.val("SELECT 1 FROM notifications WHERE user_id=%s AND dedupe_key=%s", [p["teacher_id"], dk]):
            continue
        if p["class_id"] not in cache:
            cache[p["class_id"]] = text(p["label"], weekly(tx, None, p["class_id"]))
        title, body = cache[p["class_id"]]
        link = f"/teacher/classes/{p['class_id']}/briefing"
        notify.notify(tx, p["teacher_id"], "weekly", title, body, link, dk)
        notify.queue_email(tx, p["email"], f"NovaQuest {title.lower()}", body + "\n\nOpen NovaQuest for the full briefing.",
                           f"{dk}:{p['teacher_id']}")
        n += 1
    return n
