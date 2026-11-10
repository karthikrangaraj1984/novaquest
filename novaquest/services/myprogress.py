"""A student's own progress (1.31.0): their scores over time in each subject, what's strong and what needs work, their
best and most improved, a weekly goal they set, new results with the change from last time, and a short reflection
after each result.

Everything here compares a student only with themselves: no class averages, ranks or other students' scores."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from novaquest.core import clock
from novaquest.core.db import Tx
from novaquest.core.errors import NotFound, ValidationError
from novaquest.services import assignments
from novaquest.services.access import Actor, require

SELF_MARKED = ("learn", "teach", "escape")  # done or not: no score to trend
FEELINGS = {"easy": "Easy", "okay": "Okay", "hard": "Hard"}
NEXT_STEPS = {"revise": "Revise the topics", "mistakes": "Redo my mistakes", "ask": "Ask my teacher", "fine": "I'm fine, move on"}
TERM_DAYS = 150  # "this term" for best and most improved


def today() -> date:
    return clock.to_ist(clock.now()).date()


def week_start(d: date | None = None) -> date:
    d = d or today()
    return d - timedelta(days=d.weekday())


def due_label(when: datetime | None, now: datetime | None = None) -> str:
    """'Closes today, 6:00 pm (in 5 h)' / 'Closes tomorrow, 9:00 am' / 'Closes Fri 30 Oct, 6:00 pm'."""
    if not when:
        return ""
    now = now or clock.now()
    w, n = clock.to_ist(when), clock.to_ist(now)
    at = w.strftime("%I:%M %p").lstrip("0").lower()
    days = (w.date() - n.date()).days
    day = "today" if days == 0 else "tomorrow" if days == 1 else w.strftime("%a %d %b")
    left = w - now
    hours = left.total_seconds() / 3600
    soon = f" (in {max(1, int(left.total_seconds() // 60))} min)" if hours < 1 else f" (in {int(hours)} h)" if hours < 12 else ""
    return f"Closes {day}, {at}{soon}" if left.total_seconds() > 0 else "Closed"


def scores(tx: Tx, student_id: int) -> list[dict[str, Any]]:
    """The student's score on every piece of released work they handed in, oldest first, with its subject."""
    rows = [a for a in assignments.for_student(tx, student_id) if a["view"] == "results" and a["kind"] not in SELF_MARKED]
    if not rows:
        return []
    got = {r["assignment_id"]: r for r in tx.all("""
        SELECT DISTINCT ON (t.assignment_id) t.assignment_id,
               round(100 * sum(coalesce(r.teacher_score, r.ai_score)) / nullif(sum(r.max_score), 0)) AS pct,
               count(*) FILTER (WHERE coalesce(r.teacher_score, r.ai_score, 0) >= r.max_score) AS right, count(*) AS total
        FROM attempts t JOIN responses r ON r.attempt_id=t.id
        WHERE t.student_id=%s AND t.status='submitted' AND t.assignment_id = ANY(%s) AND r.max_score IS NOT NULL
        GROUP BY t.assignment_id, t.attempt_no ORDER BY t.assignment_id, t.attempt_no DESC""",
        [student_id, [r["id"] for r in rows]])}
    out = [{"id": r["id"], "title": r["title"], "subject_id": r["subject_id"], "subject": r["subject_label"],
            "colour": r["subject_colour"], "when": r["expires_at"], "released_at": r["released_at"],
            "pct": int(got[r["id"]]["pct"]), "right": got[r["id"]]["right"], "total": got[r["id"]]["total"]}
           for r in rows if got.get(r["id"]) and got[r["id"]]["pct"] is not None]
    return sorted(out, key=lambda r: r["when"])


def subjects(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per subject: the score trend (last ten results), the average, the latest, and whether the last three are up
    or down on the three before them."""
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        s = out.setdefault(r["subject_id"], {"id": r["subject_id"], "label": r["subject"], "colour": r["colour"], "points": []})
        s["points"].append({"when": r["when"], "pct": r["pct"], "title": r["title"]})
    for s in out.values():
        p = [x["pct"] for x in s["points"]]
        s["points"] = s["points"][-10:]
        s["avg"] = round(sum(p) / len(p))
        s["last"] = p[-1]
        recent, before = p[-3:], p[-6:-3]
        s["change"] = round(sum(recent) / len(recent) - sum(before) / len(before)) if before else None
    return sorted(out.values(), key=lambda s: s["label"])


def with_change(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each result with the change from the one before it in the same subject (percentage points; None for the first)."""
    last: dict[int, int] = {}
    out = []
    for r in rows:
        prev = last.get(r["subject_id"])
        out.append({**r, "change": None if prev is None else r["pct"] - prev})
        last[r["subject_id"]] = r["pct"]
    return out


def new_results(rows: list[dict[str, Any]], days: int = 7) -> list[dict[str, Any]]:
    """Results released in the last week, newest first, each with its change from last time."""
    since = clock.now() - timedelta(days=days)
    return sorted([r for r in with_change(rows) if r["released_at"] and r["released_at"] >= since],
                  key=lambda r: r["released_at"], reverse=True)


def topics(tx: Tx, student_id: int) -> dict[str, list[dict[str, Any]]]:
    """Strongest topics (highest level, then best recent score) and the ones that need work (started but not yet
    practised, or with a low recent score), three of each; and this term's most improved topic."""
    rows = tx.all("""SELECT m.topic_id, m.level, n.title, c.title AS chapter,
            (SELECT avg(x.score) FROM (SELECT e.score FROM learn_events e WHERE e.student_id=m.student_id AND e.topic_id=m.topic_id
               AND e.score IS NOT NULL ORDER BY e.created_at DESC LIMIT 3) x) AS recent
        FROM mastery m JOIN syllabus_nodes n ON n.id=m.topic_id JOIN syllabus_nodes c ON c.id=n.parent_id
        WHERE m.student_id=%s""", [student_id])
    for r in rows:
        r["recent"] = round(100 * float(r["recent"])) if r["recent"] is not None else None
    strong = sorted([r for r in rows if r["level"] >= 2], key=lambda r: (-r["level"], -(r["recent"] or 0)))[:3]
    weak = sorted([r for r in rows if r["level"] < 2 or (r["recent"] is not None and r["recent"] < 60)],
                  key=lambda r: (r["level"], r["recent"] if r["recent"] is not None else 100))[:3]
    weak = [r for r in weak if r["topic_id"] not in {s["topic_id"] for s in strong}]
    since = clock.now() - timedelta(days=TERM_DAYS)
    seq = tx.all("""SELECT e.topic_id, n.title, e.score FROM learn_events e JOIN syllabus_nodes n ON n.id=e.topic_id
                    WHERE e.student_id=%s AND e.score IS NOT NULL AND e.created_at > %s ORDER BY e.created_at""", [student_id, since])
    first: dict[int, tuple[str, float]] = {}
    latest: dict[int, float] = {}
    for r in seq:
        first.setdefault(r["topic_id"], (r["title"], float(r["score"])))
        latest[r["topic_id"]] = float(r["score"])
    gains = [(round(100 * (latest[t] - first[t][1])), first[t][0], t) for t in first if latest[t] > first[t][1]]
    best_gain = max(gains, default=None)
    improved = {"topic_id": best_gain[2], "title": best_gain[1], "points": best_gain[0]} if best_gain and best_gain[0] >= 10 else None
    return {"strong": strong, "weak": weak, "improved": improved}


def best(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The best result this term."""
    since = clock.now() - timedelta(days=TERM_DAYS)
    term = [r for r in rows if r["when"] and r["when"] >= since]
    return max(term, key=lambda r: (r["pct"], r["when"]), default=None)


# ---------- weekly goal ----------
def _topics_this_week(tx: Tx, student_id: int, start: date) -> int:
    begin = datetime.combine(start, datetime.min.time(), tzinfo=clock.IST)
    return int(tx.val("""SELECT count(DISTINCT topic_id) FROM learn_events WHERE student_id=%s AND created_at >= %s AND created_at < %s""",
                      [student_id, begin, begin + timedelta(days=7)]) or 0)


def goal(tx: Tx, student_id: int) -> dict[str, Any]:
    """This week's goal (topics to work on) and how far along it is, and how last week's went."""
    this, last = week_start(), week_start() - timedelta(days=7)
    targets = {r["week_start"]: r["target"] for r in tx.all(
        "SELECT week_start, target FROM weekly_goals WHERE student_id=%s AND week_start IN (%s, %s)", [student_id, this, last])}
    done = _topics_this_week(tx, student_id, this)
    out = {"target": targets.get(this), "done": done, "days_left": 6 - today().weekday(), "last": None}
    if last in targets:
        got = _topics_this_week(tx, student_id, last)
        out["last"] = {"target": targets[last], "done": got, "met": got >= targets[last]}
    return out


def set_goal(tx: Tx, actor: Actor, target: int) -> None:
    require(actor, "student")
    if not 1 <= target <= 20:
        raise ValidationError("Choose between 1 and 20 topics.")
    tx.exec("""INSERT INTO weekly_goals(institution_id, student_id, week_start, target) VALUES (%s,%s,%s,%s)
               ON CONFLICT (student_id, week_start) DO UPDATE SET target=EXCLUDED.target""",
            [tx.inst, actor.user_id, week_start(), target])


# ---------- reflection after a result ----------
def reflect(tx: Tx, actor: Actor, assignment_id: int, feeling: str, next_step: str) -> None:
    """How the work felt and what the student will do next (once per piece of work; can be changed)."""
    require(actor, "student")
    if feeling not in FEELINGS:
        raise ValidationError("Choose how it felt.")
    if next_step and next_step not in NEXT_STEPS:
        raise ValidationError("Choose what you'll do next.")
    if not tx.val("SELECT 1 FROM attempts WHERE assignment_id=%s AND student_id=%s AND status='submitted'", [assignment_id, actor.user_id]):
        raise NotFound("No submitted work for this assignment.")
    tx.exec("""INSERT INTO reflections(institution_id, student_id, assignment_id, feeling, next_step, created_at)
               VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (student_id, assignment_id) DO UPDATE
               SET feeling=EXCLUDED.feeling, next_step=EXCLUDED.next_step, created_at=EXCLUDED.created_at""",
            [tx.inst, actor.user_id, assignment_id, feeling, next_step or None, clock.now()])


def reflection(tx: Tx, student_id: int, assignment_id: int) -> dict[str, Any] | None:
    return tx.one("SELECT feeling, next_step FROM reflections WHERE student_id=%s AND assignment_id=%s", [student_id, assignment_id])


def class_feelings(tx: Tx, assignment_id: int) -> dict[str, Any]:
    """For the teacher: how many found the work easy, okay or hard, and what they plan to do."""
    rows = tx.all("SELECT feeling, next_step FROM reflections WHERE assignment_id=%s", [assignment_id])
    return {"n": len(rows), "feelings": {k: sum(r["feeling"] == k for r in rows) for k in FEELINGS},
            "next": {k: sum(r["next_step"] == k for r in rows) for k in NEXT_STEPS}}
