"""A teacher's weekly timetable (1.30.0): when they meet each class for each subject.

It is optional. With it, Today lists the day's lessons in time order with the next one first, a lesson plan counts the
lessons a chapter really gets, each class's place in the plan follows the lessons already taught, and homework closes
just before the class meets again. Without it everything still works from the term plan's school days."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from novaquest.core import audit, clock
from novaquest.core.db import Tx
from novaquest.core.errors import NotFound, ValidationError
from novaquest.services import syllabus
from novaquest.services.access import Actor, require, require_class

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
LENGTHS = (30, 35, 40, 45, 50, 60, 80, 90)


def parse_time(value: str | None) -> time:
    """'09:40' (an <input type=time>) as a time; school hours only."""
    try:
        t = time.fromisoformat((value or "").strip()[:5])
    except ValueError:
        raise ValidationError("Give the start time, like 09:40.", field="starts_at") from None
    if not time(6, 0) <= t <= time(19, 0):
        raise ValidationError("Lessons start between 06:00 and 19:00.", field="starts_at")
    return t


def _ends(t: time, minutes: int) -> time:
    return (datetime.combine(date.today(), t) + timedelta(minutes=minutes)).time()


def slots(tx: Tx, teacher_id: int) -> list[dict[str, Any]]:
    """Every lesson in a teacher's week, by day and time, with its class and subject."""
    rows = tx.all("""SELECT s.id, s.class_id, s.syllabus_id, s.weekday, s.starts_at, s.minutes, c.label AS class_label,
                            b.subject, b.board
                     FROM timetable_slots s JOIN classes c ON c.id=s.class_id JOIN syllabi b ON b.id=s.syllabus_id
                     WHERE s.teacher_id=%s AND c.status='active' ORDER BY s.weekday, s.starts_at""", [teacher_id])
    for r in rows:
        r["day"] = DAYS[r["weekday"]]
        r["ends_at"] = _ends(r["starts_at"], r["minutes"])
        r["key"] = f"{r['class_id']}:{r['syllabus_id']}"
    return rows


def has_timetable(tx: Tx, teacher_id: int) -> bool:
    return bool(tx.val("SELECT 1 FROM timetable_slots WHERE teacher_id=%s LIMIT 1", [teacher_id]))


def _teaches(tx: Tx, actor: Actor, class_id: int, syllabus_id: int) -> dict[str, Any]:
    """The subject (all parts of a book in parts) if this teacher teaches it in this class, else ValidationError."""
    require_class(tx, actor, class_id, write=True)
    book = next((b for b in syllabus.teacher_subjects(tx, actor.user_id, class_id, actor.role) if syllabus_id in b["ids"]), None)
    if not book:
        raise ValidationError("You don't teach that subject in that class. Ask your school admin to map you.")
    return book


def add(tx: Tx, actor: Actor, class_id: int, syllabus_id: int, days: list[int], starts_at: time, minutes: int) -> int:
    """Add a lesson on each of the given days (0 = Monday). A lesson that overlaps one already in the teacher's week
    is refused, naming the clash. Returns how many were added."""
    require(actor, "teacher")
    book = _teaches(tx, actor, class_id, syllabus_id)
    days = sorted({d for d in days if 0 <= d <= 5})
    if not days:
        raise ValidationError("Tick at least one day.", field="days")
    if minutes not in LENGTHS:
        raise ValidationError("Choose the length of the period.", field="minutes")
    ends = _ends(starts_at, minutes)
    mine = slots(tx, actor.user_id)
    for d in days:
        for s in mine:
            if s["weekday"] == d and starts_at < s["ends_at"] and s["starts_at"] < ends:
                raise ValidationError(f"{DAY_NAMES[d]} {starts_at:%H:%M} clashes with {s['class_label']} {s['subject']} "
                                      f"at {s['starts_at']:%H:%M}–{s['ends_at']:%H:%M}.")
    for d in days:
        tx.exec("""INSERT INTO timetable_slots(institution_id, teacher_id, class_id, syllabus_id, weekday, starts_at, minutes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""", [tx.inst, actor.user_id, class_id, book["id"], d, starts_at, minutes])
    audit.record(tx, "timetable.add", actor_id=actor.user_id, target=f"class:{class_id}",
                 detail={"syllabus_id": book["id"], "days": days, "starts_at": starts_at.isoformat(), "minutes": minutes})
    return len(days)


def remove(tx: Tx, actor: Actor, slot_id: int) -> None:
    require(actor, "teacher")
    n = tx.exec("DELETE FROM timetable_slots WHERE id=%s AND teacher_id=%s", [slot_id, actor.user_id])
    if not n:
        raise NotFound("That lesson isn't in your timetable.")
    audit.record(tx, "timetable.remove", actor_id=actor.user_id, target=f"slot:{slot_id}")


def week_grid(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The week as rows of start times by day, for the timetable page."""
    times = sorted({r["starts_at"] for r in rows})
    cells = {(r["weekday"], r["starts_at"]): r for r in rows}
    return {"days": DAYS, "rows": [{"time": t, "cells": [cells.get((d, t)) for d in range(6)]} for t in times]}


def _class_slots(tx: Tx, class_id: int, syllabus_ids: list[int]) -> list[dict[str, Any]]:
    """The class's lessons in a subject, from any of its teachers' timetables."""
    return tx.all("""SELECT weekday, starts_at, minutes FROM timetable_slots WHERE class_id=%s AND syllabus_id = ANY(%s)
                     ORDER BY weekday, starts_at""", [class_id, syllabus_ids])


def lessons_between(tx: Tx, class_id: int, syllabus_ids: list[int], start: date, end: date) -> int | None:
    """How many lessons the class has in the subject from start to end (both included), or None without a timetable."""
    week = _class_slots(tx, class_id, syllabus_ids)
    if not week or end < start:
        return None if not week else 0
    per_day = [sum(1 for s in week if s["weekday"] == d) for d in range(7)]
    n, d = 0, start
    while d <= end:
        n += per_day[d.weekday()] if d.weekday() < 6 else 0
        d += timedelta(days=1)
    return n


def lessons_held(tx: Tx, class_id: int, syllabus_ids: list[int], start: date, now: datetime | None = None) -> int | None:
    """Lessons of the subject the class has finished since `start` (a lesson counts once it has ended), or None
    without a timetable."""
    week = _class_slots(tx, class_id, syllabus_ids)
    if not week:
        return None
    now_ist = clock.to_ist(now or clock.now())
    today = now_ist.date()
    n, d = 0, start
    while d <= today:
        for s in week:
            if s["weekday"] == d.weekday():
                if d < today or _ends(s["starts_at"], s["minutes"]) <= now_ist.time().replace(tzinfo=None):
                    n += 1
        d += timedelta(days=1)
    return n


def next_lesson(tx: Tx, class_id: int, syllabus_ids: list[int], after: datetime) -> datetime | None:
    """When the class next meets for the subject after a moment (IST, timezone-aware), or None without a timetable."""
    week = _class_slots(tx, class_id, syllabus_ids)
    if not week:
        return None
    at = clock.to_ist(after)
    for i in range(0, 15):
        d = at.date() + timedelta(days=i)
        for s in week:
            if s["weekday"] == d.weekday():
                start = datetime.combine(d, s["starts_at"], tzinfo=at.tzinfo)
                if start > at:
                    return start
    return None


def due_by(tx: Tx, class_id: int, syllabus_ids: list[int], now: datetime | None = None, min_days: int = 2) -> datetime | None:
    """A sensible close time for homework: just before the class's first lesson at least `min_days` away (so it's
    done before they meet again), or None without a timetable."""
    now = now or clock.now()
    start = clock.to_ist(now) + timedelta(days=min_days)
    nxt = next_lesson(tx, class_id, syllabus_ids, start.replace(hour=6, minute=0, second=0, microsecond=0))
    return nxt - timedelta(minutes=5) if nxt else None


def today(tx: Tx, teacher_id: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """Today's lessons for a teacher in time order, each marked past, now or later, and the next one flagged."""
    now_ist = clock.to_ist(now or clock.now())
    wd = now_ist.weekday()
    if wd > 5:
        return []
    rows = [r for r in slots(tx, teacher_id) if r["weekday"] == wd]
    t = now_ist.time().replace(tzinfo=None)
    flagged = False
    for r in rows:
        r["when"] = "past" if r["ends_at"] <= t else ("now" if r["starts_at"] <= t else "later")
        r["next"] = not flagged and r["when"] in ("now", "later")
        flagged = flagged or r["next"]
    return rows
