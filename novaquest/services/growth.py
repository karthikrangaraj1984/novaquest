"""Start-of-term and progress checks: proof of learning growth (1.16.0).

A teacher sets a class a start-of-term check for one subject (book): a multiple-choice quiz with a question or two
from every chapter, made and reviewed like any assignment. Later in the term, each progress check covers the same
topics with fresh questions (a parallel test). The growth report compares each student's and the class's latest
progress check with where they started, overall and by chapter. Multiple choice only, so every score is marked the
same way and no AI marking is needed."""
from __future__ import annotations

from typing import Any

from novaquest.core import audit
from novaquest.core.db import Db, Tx
from novaquest.core.errors import Conflict, ValidationError
from novaquest.services import questions, syllabus
from novaquest.services.access import Actor, require, require_class

MAX_QUESTIONS = 20
PER_CHAPTER = 2


def _checks(tx: Tx, class_id: int, book_id: int) -> list[dict]:
    """The checks for a subject (every part of a book in parts)."""
    return tx.all("""SELECT id, title, purpose, state, opens_at, expires_at, released_at, created_at, topic_ids, baseline_id
                     FROM assignments WHERE class_id=%s AND syllabus_id = ANY(%s) AND purpose IS NOT NULL
                     ORDER BY created_at, id""", [class_id, syllabus.parts_of(tx, book_id)])


def _spread(tx: Tx, book_id: int) -> list[int]:
    """Topics for a check: up to two from every chapter (its first and a middle one), so the whole book is covered
    (both parts of a book in parts)."""
    picked = []
    for ch in syllabus.subject_tree(tx, syllabus.subject_by_id(tx, book_id)):
        ids = [t["id"] for t in ch["topics"]]
        if ids:
            picked += list(dict.fromkeys([ids[0], ids[len(ids) // 2]]))[:PER_CHAPTER]
    return picked


def create_check(db: Db, engine: Any, actor: Actor, class_id: int, book_id: int, purpose: str) -> int:
    """Draft the class's start-of-term check ('baseline') or its next progress check for one book. The teacher reviews
    the questions and schedules it like any assignment. Returns the assignment id."""
    require(actor, "teacher")
    if purpose not in ("baseline", "progress"):
        raise ValidationError("Choose a start-of-term or a progress check.")
    with db.tenant(actor.institution_id) as tx:
        require_class(tx, actor, class_id, write=True)
        book = syllabus.get_syllabus(tx, book_id)
        checks = _checks(tx, class_id, book_id)
        base = next((c for c in reversed(checks) if c["purpose"] == "baseline"), None)
        if purpose == "baseline":
            if base and base["state"] != "draft":
                raise Conflict("This class already has a start-of-term check for this subject. Set a progress check instead.")
            topics = _spread(tx, book_id)
            n = 0
        else:
            if not base or base["state"] == "draft":
                raise Conflict("Set (and schedule) the start-of-term check first: progress is measured against it.")
            topics = base["topic_ids"]
            n = sum(1 for c in checks if c["purpose"] == "progress") + 1
        if not topics:
            raise ValidationError("This book has no topics yet.")
        count = min(MAX_QUESTIONS, max(5, len(topics)))
    title = (f"Start-of-term check: {book['subject']}" if purpose == "baseline" else f"Progress check {n}: {book['subject']}")
    aid = questions.generate_assignment(db, engine, actor, class_id=class_id, topic_ids=topics, kind="quiz",
                                        difficulty="medium", count=count, title=title, seed=class_id * 31 + 1000 * n + 7)
    with db.tenant(actor.institution_id) as tx:
        tx.exec("UPDATE assignments SET purpose=%s, baseline_id=%s WHERE id=%s",
                [purpose, base["id"] if purpose == "progress" and base else None, aid])
        audit.record(tx, f"growth.{purpose}", actor_id=actor.user_id, target=f"assignment:{aid}")
    return aid


def _scores(tx: Tx, assignment_id: int) -> tuple[dict[int, float], dict[int, list[float]]]:
    """Each student's share of the marks on one check (latest handed-in attempt), and per chapter: chapter -> scores."""
    rows = tx.all("""SELECT DISTINCT ON (t.student_id, r.question_id) t.student_id, c.id AS chapter_id,
               coalesce(r.teacher_score, r.ai_score, 0) AS got, coalesce(r.max_score, 1) AS mx
            FROM attempts t JOIN responses r ON r.attempt_id=t.id JOIN questions q ON q.id=r.question_id
            JOIN syllabus_nodes n ON n.id=q.topic_id JOIN syllabus_nodes c ON c.id=n.parent_id
            WHERE t.assignment_id=%s AND t.status='submitted'
            ORDER BY t.student_id, r.question_id, t.attempt_no DESC""", [assignment_id])
    per: dict[int, list[float]] = {}
    chap: dict[int, dict[int, list[float]]] = {}
    for r in rows:
        per.setdefault(r["student_id"], [0.0, 0.0])
        per[r["student_id"]][0] += float(r["got"])
        per[r["student_id"]][1] += float(r["mx"])
        chap.setdefault(r["chapter_id"], {}).setdefault(r["student_id"], [0.0, 0.0])
        chap[r["chapter_id"]][r["student_id"]][0] += float(r["got"])
        chap[r["chapter_id"]][r["student_id"]][1] += float(r["mx"])
    students = {s: 100 * g / m for s, (g, m) in per.items() if m}
    chapters = {c: [100 * g / m for g, m in d.values() if m] for c, d in chap.items()}
    return students, chapters


def _avg(xs) -> float | None:
    xs = list(xs)
    return round(sum(xs) / len(xs)) if xs else None


def report(tx: Tx, actor: Actor, class_id: int, book_id: int) -> dict[str, Any]:
    """The class's growth in one subject: each check with its class average; each student's start-of-term score,
    latest progress score and change; each chapter's class average then and now. Only students who took both count
    towards the class's change."""
    require(actor, "teacher", "leader", "inst_admin")
    require_class(tx, actor, class_id)
    checks = [c for c in _checks(tx, class_id, book_id) if c["state"] != "draft"]
    base = next((c for c in reversed(checks) if c["purpose"] == "baseline"), None)
    drafts = [c for c in _checks(tx, class_id, book_id) if c["state"] == "draft"]
    out: dict[str, Any] = {"checks": [], "students": [], "chapters": [], "baseline": base, "latest": None, "summary": None,
                           "draft": drafts[-1] if drafts else None}
    if not base:
        return out
    scored = []
    for c in checks:
        if c["id"] != base["id"] and c["baseline_id"] != base["id"]:
            continue
        s, ch = _scores(tx, c["id"])
        scored.append((c, s, ch))
        out["checks"].append({**c, "avg": _avg(s.values()), "n": len(s)})
    b_students, b_chapters = scored[0][1], scored[0][2]
    later = [x for x in scored[1:] if x[1]]
    latest = later[-1] if later else None
    out["latest"] = latest[0] if latest else None
    roster = tx.all("""SELECT u.id, u.name, e.roll_no FROM enrolments e JOIN users u ON u.id=e.student_id
                       WHERE e.class_id=%s AND e.status='active' ORDER BY length(e.roll_no), e.roll_no""", [class_id])
    for r in roster:
        b = b_students.get(r["id"])
        now = latest[1].get(r["id"]) if latest else None
        out["students"].append({**r, "baseline": None if b is None else round(b), "latest": None if now is None else round(now),
                                "change": None if b is None or now is None else round(now - b)})
    for c in syllabus.subject_tree(tx, syllabus.subject_by_id(tx, book_id)):
        cid, code, title = c["id"], c["code"], c["title"]
        if cid in b_chapters or (latest and cid in latest[2]):
            then, now = _avg(b_chapters.get(cid, [])), _avg(latest[2].get(cid, [])) if latest else None
            out["chapters"].append({"code": code, "title": title, "part": c["part"], "baseline": then, "latest": now,
                                    "change": None if then is None or now is None else now - then})
    both = [s for s in out["students"] if s["change"] is not None]
    if both:
        out["summary"] = {"baseline": _avg(s["baseline"] for s in both), "latest": _avg(s["latest"] for s in both),
                          "change": _avg(s["change"] for s in both), "n": len(both),
                          "improved": sum(1 for s in both if s["change"] > 0)}
    return out


def class_summaries(tx: Tx, class_id: int) -> list[dict[str, Any]]:
    """For the principal's class page: each subject's growth in one line (start of term → latest, change). The
    caller has already checked the class may be seen."""
    out = []
    for book in syllabus.class_subjects(tx, class_id):
        s = _summary_only(tx, class_id, book["id"])
        if s.get("summary") or s.get("baseline_avg") is not None:
            out.append({"book": book, **s})
    return out


def _summary_only(tx: Tx, class_id: int, book_id: int) -> dict[str, Any]:
    checks = [c for c in _checks(tx, class_id, book_id) if c["state"] != "draft"]
    base = next((c for c in reversed(checks) if c["purpose"] == "baseline"), None)
    if not base:
        return {"summary": None}
    b, _ = _scores(tx, base["id"])
    later = [c for c in checks if c["baseline_id"] == base["id"]]
    for c in reversed(later):
        s, _ = _scores(tx, c["id"])
        both = [k for k in s if k in b]
        if both:
            return {"summary": {"baseline": _avg(b[k] for k in both), "latest": _avg(s[k] for k in both),
                                "change": _avg(s[k] - b[k] for k in both), "n": len(both), "latest_title": c["title"]}}
    return {"summary": None, "baseline_avg": _avg(b.values())}
