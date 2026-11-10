"""The teacher's Today page (1.30.0): every class they teach today, each at its own place in the lesson plan.

A teacher with several sections (Science in 8A, 8B and 8C) shares one lesson plan per chapter across them, but each
section is at its own period. With a timetable the day's lessons come in time order with the next one first; without
one every class they teach is listed, in class order."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from novaquest.core import clock
from novaquest.core.db import Tx
from novaquest.services import plans, prep, syllabus, timetable
from novaquest.services.access import Actor, visible_class_ids


def teaching(tx: Tx, actor: Actor) -> list[dict[str, Any]]:
    """Every (class, subject) this staff member works with, in class order: what a teacher picks before choosing
    topics. Each has 'key' ('class id:book id'), and 'name' ('8B · Science (NCERT)')."""
    ids = visible_class_ids(tx, actor)
    out = []
    for c in tx.all("SELECT id, label FROM classes WHERE id = ANY(%s) ORDER BY grade, label", [ids]):
        for b in syllabus.teacher_subjects(tx, actor.user_id, c["id"], actor.role):
            out.append({"cid": c["id"], "class_label": c["label"], "book": b, "key": f"{c['id']}:{b['id']}",
                        "name": f"{c['label']} · {b['subject']} ({b['board']})", "short": f"{c['label']} · {b['subject']}"})
    return out


def sections(teaching_rows: list[dict[str, Any]], cur: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The classes this teacher teaches the same subject (book) in, the current one included, in class order."""
    if not cur:
        return []
    return [t for t in teaching_rows if t["book"]["id"] == cur["book"]["id"]]


def lesson_for(tx: Tx, t: dict[str, Any], minutes: int = 40, cache: dict | None = None, now: datetime | None = None) -> dict[str, Any]:
    """For one class and subject: the chapter in class now (term plan), the period to teach next and its title, and
    the links to present it or open its plan."""
    cache = cache if cache is not None else {}
    now_ch = [p for p in plans.in_class_now(tx, t["cid"]) if p["syllabus_id"] in t["book"]["ids"]]
    out: dict[str, Any] = {"chapter": None, "pos": None, "period_title": None, "present": None,
                           "plan": f"/teacher/prep?for={t['key']}", "term_plan": f"/teacher/plan?for={t['key']}"}
    if not now_ch:
        nxt = tx.one("""SELECT p.chapter_id, p.starts_on, c.code, c.title FROM term_plans p JOIN syllabus_nodes c ON c.id=p.chapter_id
                        WHERE p.class_id=%s AND p.syllabus_id = ANY(%s) AND p.starts_on > %s ORDER BY p.starts_on LIMIT 1""",
                     [t["cid"], t["book"]["ids"], plans.today()])
        out["next_chapter"] = nxt
        out["planned"] = bool(nxt) or bool(tx.val("SELECT 1 FROM term_plans WHERE class_id=%s AND syllabus_id = ANY(%s) LIMIT 1",
                                                     [t["cid"], t["book"]["ids"]]))
        return out
    ch = now_ch[0]
    if ch["chapter_id"] not in cache:
        cache[ch["chapter_id"]] = prep.pack(tx, ch["chapter_id"])[0]
    data = cache[ch["chapter_id"]]
    periods = prep.default_periods(tx, t["cid"], ch["chapter_id"], data)
    pos = prep.position(tx, t["cid"], ch["chapter_id"], periods, now)
    lessons = prep.arrange(data, periods, minutes)
    n = pos["period"] if pos else 1
    out.update(chapter=ch, pos=pos, planned=True,
               period_title=lessons[n - 1]["title"] if 0 < n <= len(lessons) else None,
               present=f"/teacher/present/{ch['chapter_id']}?for={t['key']}&periods={periods}&minutes={minutes}&period={n}",
               plan=f"/teacher/prep?for={t['key']}&chapter={ch['chapter_id']}")
    return out


def classes_today(tx: Tx, actor: Actor, now: datetime | None = None) -> dict[str, Any]:
    """What the teacher teaches today. With a timetable: today's lessons in time order (past, now, later; the next
    one flagged). Without: every class and subject they teach. Each row carries lesson_for()."""
    rows_t = teaching(tx, actor)
    by_key = {t["key"]: t for t in rows_t}
    has_tt = timetable.has_timetable(tx, actor.user_id)
    cache: dict = {}
    rows = []
    if has_tt:
        for s in timetable.today(tx, actor.user_id, now):
            t = by_key.get(s["key"])
            if t:
                rows.append({**t, "slot": s, **lesson_for(tx, t, s["minutes"], cache, now)})
    else:
        for t in rows_t:
            rows.append({**t, "slot": None, **lesson_for(tx, t, 40, cache, now)})
    day = clock.to_ist(now or clock.now())
    return {"timetable": has_tt, "rows": rows, "teaching": rows_t, "day": day.strftime("%A"),
            "weekend": day.weekday() > 5, "next": next((r for r in rows if r["slot"] and r["slot"]["next"]), None)}
