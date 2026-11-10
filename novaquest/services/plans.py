"""Term plan (teacher review, item 12): the dates a class studies each chapter of each book. The teacher types the
dates or has them spread over the term by topic count; NovaQuest then shows what's being taught now, whether the class
is on track (chapters whose dates have passed but which most students haven't practised), and tells students which
chapter is being taught in class this week."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from novaquest.core import audit, clock
from novaquest.core.db import Tx
from novaquest.core.errors import ValidationError
from novaquest.services import syllabus
from novaquest.services.access import Actor, require, require_class

ON_TRACK_AT = 50  # a finished chapter counts as covered when at least half its topic-student pairs are practised
MAX_SPAN = timedelta(days=550)  # an academic year and a half either side of today: anything further is a typing slip


def today() -> date:
    return clock.to_ist(clock.now()).date()


def parse_day(value: str | None, label: str) -> date | None:
    """A date from an <input type=date> (YYYY-MM-DD); blank means none."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise ValidationError(f"{label}: use a date like 2026-11-02.")
    if abs(d - today()) > MAX_SPAN:
        raise ValidationError(f"{label}: {d:%d %b %Y} is too far from today. Check the year.")
    return d


def _book(tx: Tx, actor: Actor, class_id: int, syllabus_id: int, *, write: bool) -> dict:
    require(actor, *(("teacher",) if write else ("teacher", "leader", "inst_admin")))
    require_class(tx, actor, class_id, write=write)
    book = next((b for b in syllabus.class_subjects(tx, class_id) if syllabus_id in b["ids"]), None)
    if not book:
        raise ValidationError("That class doesn't study this book.")
    return book  # the subject: one book, or all the parts of a book in parts


def _progress(tx: Tx, class_id: int, syllabus_ids: list[int]) -> tuple[int, dict[int, dict[str, int]]]:
    """(students in the class, chapter id -> how many topic-student pairs are started and practised): one query."""
    n = tx.val("SELECT count(*) FROM enrolments WHERE class_id=%s AND status='active'", [class_id])
    rows = tx.all("""SELECT t.parent_id AS chapter_id, count(*) FILTER (WHERE m.level >= 1) AS started,
                            count(*) FILTER (WHERE m.level >= 2) AS practised
                     FROM mastery m JOIN syllabus_nodes t ON t.id=m.topic_id
                     JOIN enrolments e ON e.student_id=m.student_id AND e.class_id=%s AND e.status='active'
                     WHERE t.syllabus_id = ANY(%s) GROUP BY t.parent_id""", [class_id, syllabus_ids])
    return int(n), {r["chapter_id"]: {"started": r["started"], "practised": r["practised"]} for r in rows}


def view(tx: Tx, actor: Actor, class_id: int, syllabus_id: int) -> dict[str, Any]:
    """The plan for one class and book (all its parts, for a book in parts): every chapter with its dates, state (done, now, next) and how much of it the
    class has practised, plus whether the class is on track."""
    book = _book(tx, actor, class_id, syllabus_id, write=False)
    planned = {r["chapter_id"]: r for r in tx.all("""SELECT chapter_id, starts_on, ends_on FROM term_plans
                                                     WHERE class_id=%s AND syllabus_id = ANY(%s)""", [class_id, book["ids"]])}
    students, prog = _progress(tx, class_id, book["ids"])
    now = today()
    chapters = []
    for ch in syllabus.subject_tree(tx, book):
        p = planned.get(ch["id"])
        pairs = max(1, students * len(ch["topics"]))
        got = prog.get(ch["id"], {"started": 0, "practised": 0})
        state = None
        if p:
            state = "done" if p["ends_on"] < now else ("now" if p["starts_on"] <= now else "next")
        practised = round(100 * got["practised"] / pairs) if students else 0
        chapters.append({"id": ch["id"], "code": ch["code"], "title": ch["title"], "topics": len(ch["topics"]), "part": ch["part"],
                         "starts_on": p["starts_on"] if p else None, "ends_on": p["ends_on"] if p else None, "state": state,
                         "practised": practised, "started": round(100 * got["started"] / pairs) if students else 0,
                         "behind": state == "done" and practised < ON_TRACK_AT})
    dated = [c for c in chapters if c["state"]]
    return {"book": book, "class_id": class_id, "students": students, "chapters": chapters, "today": now,
            "planned": len(dated), "now": [c for c in chapters if c["state"] == "now"],
            "behind": [c for c in chapters if c["behind"]],
            "next": min((c for c in chapters if c["state"] == "next"), key=lambda c: c["starts_on"], default=None),
            "first": min((c["starts_on"] for c in dated), default=None), "last": max((c["ends_on"] for c in dated), default=None)}


def save(tx: Tx, actor: Actor, class_id: int, syllabus_id: int, entries: list[tuple[int, date | None, date | None]]) -> int:
    """Set the dates of the given chapters (both blank removes a chapter from the plan). Returns chapters planned."""
    book = _book(tx, actor, class_id, syllabus_id, write=True)
    tree = syllabus.subject_tree(tx, book)
    titles = {ch["id"]: (f"Part {ch['part']} · " if ch["part"] else "") + f"Chapter {ch['code']}" for ch in tree}
    book_of = {ch["id"]: ch["syllabus_id"] for ch in tree}
    for chapter_id, start, end in entries:
        if chapter_id not in titles:
            raise ValidationError("That chapter isn't in this book.")
        if (start is None) != (end is None):
            raise ValidationError(f"{titles[chapter_id]}: give both a start and an end date, or leave both blank.")
        if start and end < start:
            raise ValidationError(f"{titles[chapter_id]}: the end date is before the start date.")
    for chapter_id, start, end in entries:
        if start is None:
            tx.exec("DELETE FROM term_plans WHERE class_id=%s AND chapter_id=%s", [class_id, chapter_id])
        else:
            tx.exec("""INSERT INTO term_plans(institution_id, class_id, syllabus_id, chapter_id, starts_on, ends_on, updated_by, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (class_id, chapter_id) DO UPDATE
                       SET starts_on=EXCLUDED.starts_on, ends_on=EXCLUDED.ends_on, updated_by=EXCLUDED.updated_by,
                           updated_at=EXCLUDED.updated_at""",
                    [tx.inst, class_id, book_of[chapter_id], chapter_id, start, end, actor.user_id, clock.now()])
    n = tx.val("SELECT count(*) FROM term_plans WHERE class_id=%s AND syllabus_id = ANY(%s)", [class_id, book["ids"]])
    audit.record(tx, "plan.save", actor_id=actor.user_id, target=f"class:{class_id}",
                 detail={"syllabus_id": syllabus_id, "chapters": n})
    return int(n)


def copy_to(tx: Tx, actor: Actor, from_class: int, syllabus_id: int, class_ids: list[int]) -> int:
    """Give other sections the same chapter dates as one class (for a teacher with several sections of a subject).
    Each target class must study the same book and be taught by this teacher; its dates for the book are replaced.
    Returns how many classes were updated."""
    book = _book(tx, actor, from_class, syllabus_id, write=True)
    rows = tx.all("SELECT chapter_id, starts_on, ends_on FROM term_plans WHERE class_id=%s AND syllabus_id = ANY(%s)",
                  [from_class, book["ids"]])
    if not rows:
        raise ValidationError("Add dates for this class first.")
    entries = [(r["chapter_id"], r["starts_on"], r["ends_on"]) for r in rows]
    n = 0
    for cid in dict.fromkeys(class_ids):
        if cid == from_class:
            continue
        target = _book(tx, actor, cid, syllabus_id, write=True)
        if target["ids"] != book["ids"]:
            raise ValidationError("That class studies a different book.")
        tx.exec("DELETE FROM term_plans WHERE class_id=%s AND syllabus_id = ANY(%s)", [cid, book["ids"]])
        save(tx, actor, cid, syllabus_id, entries)
        n += 1
    return n


def section_status(tx: Tx, actor: Actor, class_id: int, syllabus_id: int) -> str:
    """One line on where a class is in a subject's plan: 'Ch 8 now · on track', '2 behind', 'No plan yet'."""
    v = view(tx, actor, class_id, syllabus_id)
    if not v["planned"]:
        return "No plan yet"
    now = ", ".join(f"Ch {c['code']}" for c in v["now"]) or "—"
    return f"{now} now · " + (f"{len(v['behind'])} behind" if v["behind"] else "on track")


def school_days(start: date, end: date, holidays: set[date] = frozenset()) -> list[date]:
    """Monday to Saturday school days from start to end, skipping the given holidays (many Indian schools work
    some Saturdays; teachers can adjust any chapter afterwards)."""
    days, d = [], start
    while d <= end:
        if d.weekday() < 6 and d not in holidays:
            days.append(d)
        d += timedelta(days=1)
    return days


def spread(chapters: list[dict], start: date, end: date) -> list[tuple[int, date, date]]:
    """Share the school days between start and end among the chapters in order, in proportion to their topic
    counts (every chapter gets at least one day)."""
    if end < start:
        raise ValidationError("The term ends before it starts.")
    days = school_days(start, end)
    if len(days) < len(chapters):
        raise ValidationError(f"{len(chapters)} chapters need at least {len(chapters)} school days; that term has {len(days)}.")
    weights = [max(1, c["topics"]) for c in chapters]
    total, out, cum, prev = sum(weights), [], 0, 0
    for i, (c, w) in enumerate(zip(chapters, weights)):
        cum += w  # cumulative boundaries, so rounding never piles up on the last chapter
        b = min(max(round(len(days) * cum / total), prev + 1), len(days) - (len(chapters) - i - 1))
        out.append((c["id"], days[prev], days[b - 1]))
        prev = b
    return out


def autofill(tx: Tx, actor: Actor, class_id: int, syllabus_id: int, start: date | None, end: date | None,
             chapter_ids: list[int] | None = None) -> int:
    """Spread the chosen chapters (all, in book order, if none chosen) over the term from start to end."""
    if not start or not end:
        raise ValidationError("Give the first and last day of the term.")
    tree = syllabus.subject_tree(tx, _book(tx, actor, class_id, syllabus_id, write=True))
    chosen = [{"id": ch["id"], "topics": len(ch["topics"])} for ch in tree if not chapter_ids or ch["id"] in chapter_ids]
    if not chosen:
        raise ValidationError("Choose at least one chapter.")
    return save(tx, actor, class_id, syllabus_id, [(cid, s, e) for cid, s, e in spread(chosen, start, end)])


def in_class_now(tx: Tx, class_id: int, on: date | None = None) -> list[dict]:
    """Chapters being taught in a class today, for every book (what students see as 'In class now')."""
    return tx.all("""SELECT p.syllabus_id, p.chapter_id, p.starts_on, p.ends_on, c.code, c.title
                     FROM term_plans p JOIN syllabus_nodes c ON c.id=p.chapter_id
                     WHERE p.class_id=%s AND p.starts_on <= %s AND p.ends_on >= %s ORDER BY p.starts_on, c.ord""",
                  [class_id, on or today(), on or today()])


def class_summary(tx: Tx, actor: Actor, class_id: int) -> dict[str, Any]:
    """For a class card on the teacher's Home: chapters in class now and how many finished chapters are behind,
    across all its books (planned books only)."""
    now, behind, planned = [], 0, False
    for book in syllabus.class_subjects(tx, class_id):
        if not tx.val("SELECT 1 FROM term_plans WHERE class_id=%s AND syllabus_id = ANY(%s) LIMIT 1", [class_id, book["ids"]]):
            continue
        v = view(tx, actor, class_id, book["id"])
        planned = True
        now += [{**c, "subject": book["subject"]} for c in v["now"]]
        behind += len(v["behind"])
    return {"planned": planned, "now": now, "behind": behind}
