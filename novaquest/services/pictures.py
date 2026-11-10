"""Learn in pictures: a picture-first lesson deck for every syllabus topic (LN-10).

Each topic teaches its core idea in plain words, one picture card at a time, ends with the crux in three
points and a short picture quiz. Content lives in data/pictures/chNN.json (one file per chapter, keyed by
section code) and is validated when loaded, so a broken card can never reach a student."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from novaquest.core.db import Tx
from novaquest.core.errors import NotFound
from novaquest.services import mastery, syllabus
from novaquest.services.access import Actor, require

ROOT = Path(__file__).resolve().parents[2]
CONTENT = ROOT / "data" / "pictures"
SCENES = Path(__file__).resolve().parents[1] / "web" / "scenes"

KINDS = {"scene", "flow", "cycle", "compare", "groups", "bars", "layers", "equation", "icons", "segments"}
COLORS = {"blue", "green", "amber", "orange", "red", "violet", "grey", "brown", "sky", "pink", "teal", "navy"}
MAX_SENTENCE = 240  # "simple terms": short sentences only


class ContentError(ValueError):
    """A picture lesson file is invalid."""


@lru_cache(maxsize=1)
def scene_anchors() -> dict[str, dict[str, tuple[float, float]]]:
    """Every drawn scene and the named points its labels can point at."""
    out: dict[str, dict[str, tuple[float, float]]] = {}
    for f in sorted(SCENES.glob("*.svg")):
        m = re.search(r"data-anchors='([^']*)'", f.read_text(encoding="utf-8"))
        if not m:
            raise ContentError(f"scene {f.name} has no data-anchors")
        out[f.stem] = {k: (float(v[0]), float(v[1])) for k, v in json.loads(m.group(1)).items()}
    return out


@lru_cache(maxsize=1)
def all_lessons() -> dict[str, dict[str, Any]]:
    """All picture lessons, keyed by section code ("8.1"), validated."""
    lessons: dict[str, dict[str, Any]] = {}
    for f in sorted(CONTENT.glob("ch*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        for code, lesson in data.items():
            if code in lessons:
                raise ContentError(f"{f.name}: topic {code} defined twice")
            validate_lesson(code, lesson)
            lessons[code] = lesson
    return lessons


def _need(cond: bool, where: str, msg: str) -> None:
    if not cond:
        raise ContentError(f"{where}: {msg}")


def _text(value: Any, where: str, limit: int = MAX_SENTENCE) -> None:
    _need(isinstance(value, str) and value.strip() != "", where, "needs text")
    _need(len(value) <= limit, where, f"text longer than {limit} characters: {value[:60]}…")


def validate_pic(pic: Any, where: str, *, quiz: bool = False) -> None:
    """Check one picture spec (see web/pictures.py for how each kind is drawn)."""
    _need(isinstance(pic, dict) and pic.get("kind") in KINDS, where, f"unknown picture kind {pic!r:.60}")
    kind = pic["kind"]
    if "alt" in pic:
        _text(pic["alt"], where + ".alt", 300)
    if kind == "scene":
        anchors = scene_anchors()
        _need(pic.get("scene") in anchors, where, f"no drawn scene {pic.get('scene')!r}")
        points = anchors[pic["scene"]]
        labels = pic.get("labels", [])
        for i, lab in enumerate(labels):
            _need(lab.get("at") in points, f"{where}.labels[{i}]", f"scene {pic['scene']} has no point {lab.get('at')!r}")
            _text(lab.get("name"), f"{where}.labels[{i}].name", 40)
            if "note" in lab:
                _text(lab["note"], f"{where}.labels[{i}].note", 120)
        if "ask" in pic:
            _need(pic["ask"] in points, where, f"scene {pic['scene']} has no point {pic['ask']!r}")
        _need(quiz or labels, where, "a scene card needs labels")
    elif kind in ("flow", "cycle"):
        steps = pic.get("steps", [])
        _need(2 <= len(steps) <= 8, where, "needs 2 to 8 steps")
        for i, s in enumerate(steps):
            _text(s.get("icon"), f"{where}.steps[{i}].icon", 12)
            _text(s.get("text"), f"{where}.steps[{i}].text", 160)
            if kind == "cycle":
                _text(s.get("short"), f"{where}.steps[{i}].short", 16)
    elif kind == "compare":
        cols = pic.get("cols", [])
        _need(2 <= len(cols) <= 3, where, "needs 2 or 3 columns")
        rows = pic.get("rows")
        for i, c in enumerate(cols):
            _text(c.get("head"), f"{where}.cols[{i}].head", 40)
            _text(c.get("icon"), f"{where}.cols[{i}].icon", 12)
            cells = c.get("cells", [])
            _need(1 <= len(cells) <= 7, f"{where}.cols[{i}]", "needs 1 to 7 cells")
            if rows:
                _need(len(cells) == len(rows), f"{where}.cols[{i}]", "needs one cell per row")
            for j, cell in enumerate(cells):
                _text(cell, f"{where}.cols[{i}].cells[{j}]", 90)
    elif kind == "groups":
        _text(pic.get("root"), where + ".root", 50)
        items = pic.get("items", [])
        _need(2 <= len(items) <= 8, where, "needs 2 to 8 items")
        for i, it in enumerate(items):
            _text(it.get("icon"), f"{where}.items[{i}].icon", 12)
            _text(it.get("name"), f"{where}.items[{i}].name", 40)
            if "eg" in it:
                _text(it["eg"], f"{where}.items[{i}].eg", 100)
    elif kind == "bars":
        items = pic.get("items", [])
        _need(2 <= len(items) <= 8, where, "needs 2 to 8 bars")
        for i, it in enumerate(items):
            _text(it.get("label"), f"{where}.items[{i}].label", 40)
            _need(isinstance(it.get("value"), (int, float)) and it["value"] >= 0, f"{where}.items[{i}]", "needs a value ≥ 0")
            if "show" in it:
                _text(it["show"], f"{where}.items[{i}].show", 40)
        _need(isinstance(pic.get("unit", ""), str) and len(pic.get("unit", "")) <= 20, where, "unit must be short text")
    elif kind in ("layers", "segments"):
        items = pic.get("items", [])
        _need(2 <= len(items) <= 7, where, "needs 2 to 7 items")
        for i, it in enumerate(items):
            _text(it.get("name"), f"{where}.items[{i}].name", 40)
            _need(it.get("color", "grey") in COLORS, f"{where}.items[{i}]", f"unknown colour {it.get('color')}")
            if "note" in it:
                _text(it["note"], f"{where}.items[{i}].note", 120)
        if kind == "segments" and "ticks" in pic:
            _need(len(pic["ticks"]) == len(items) + 1, where, "needs one tick more than segments")
    elif kind == "equation":
        parts = pic.get("parts", [])
        _need(3 <= len(parts) <= 7, where, "needs 3 to 7 parts (it must fit a phone)")
        for i, p in enumerate(parts):
            if isinstance(p, str):
                _need(p in ("+", "→", "=", "÷", "×", "⇌"), f"{where}.parts[{i}]", f"unknown operator {p!r}")
            else:
                _text(p.get("text"), f"{where}.parts[{i}].text", 40)
    elif kind == "icons":
        items = pic.get("items", [])
        _need(2 <= len(items) <= 9, where, "needs 2 to 9 tiles")
        for i, it in enumerate(items):
            _text(it.get("icon"), f"{where}.items[{i}].icon", 12)
            _text(it.get("text"), f"{where}.items[{i}].text", 60)
            if "sub" in it:
                _text(it["sub"], f"{where}.items[{i}].sub", 100)


def validate_lesson(code: str, lesson: Any) -> None:
    """Check one topic's picture lesson."""
    where = f"topic {code}"
    _need(isinstance(lesson, dict), where, "must be an object")
    _text(lesson.get("crux"), where + ".crux", 200)
    cards = lesson.get("cards", [])
    _need(3 <= len(cards) <= 8, where, "needs 3 to 8 cards")
    for i, c in enumerate(cards):
        w = f"{where}.cards[{i}]"
        _text(c.get("title"), w + ".title", 60)
        validate_pic(c.get("pic"), w + ".pic")
        say = c.get("say", [])
        _need(1 <= len(say) <= 4, w, "needs 1 to 4 sentences")
        for j, s in enumerate(say):
            _text(s, f"{w}.say[{j}]")
        for key in ("remember", "think"):
            if key in c:
                _text(c[key], f"{w}.{key}", 200)
    points = lesson.get("crux_points", [])
    _need(2 <= len(points) <= 4, where, "needs 2 to 4 crux points")
    for j, p in enumerate(points):
        _text(p, f"{where}.crux_points[{j}]", 160)
    quiz = lesson.get("quiz", [])
    _need(2 <= len(quiz) <= 4, where, "needs 2 to 4 quiz questions")
    for j, q in enumerate(quiz):
        w = f"{where}.quiz[{j}]"
        _text(q.get("q"), w + ".q", 200)
        opts = q.get("options", [])
        _need(2 <= len(opts) <= 4 and len(set(opts)) == len(opts), w, "needs 2 to 4 different options")
        for o in opts:
            _text(o, w + ".option", 80)
        _need(isinstance(q.get("answer"), int) and 0 <= q["answer"] < len(opts), w, "answer must index an option")
        _text(q.get("why"), w + ".why")
        if "pic" in q:
            validate_pic(q["pic"], w + ".pic", quiz=True)


def _file_syllabi(tx: Tx) -> set[int]:
    """Shared books the files in data/pictures belong to (NCERT Grade 8 Science, any version)."""
    return {r["id"] for r in tx.all("""SELECT id FROM syllabi WHERE institution_id IS NULL AND board='NCERT'
                                       AND subject='Science' AND grade='8'""")}


def lessons_for(tx: Tx, topics: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Approved picture lessons for these topics ({id, code, syllabus_id}), keyed by topic id, in at most two queries.
    NCERT topics use the shipped files; a school's own books use their approved drafted lessons."""
    if not topics:
        return {}
    missing = [t["id"] for t in topics if "syllabus_id" not in t]
    if missing:
        books = {r["id"]: r["syllabus_id"] for r in tx.all("SELECT id, syllabus_id FROM syllabus_nodes WHERE id = ANY(%s)", [missing])}
        topics = [t if "syllabus_id" in t else {**t, "syllabus_id": books.get(t["id"])} for t in topics]
    shared = _file_syllabi(tx)
    files = all_lessons()
    out = {t["id"]: files[t["code"]] for t in topics if t["syllabus_id"] in shared and t["code"] in files}
    own = [t["id"] for t in topics if t["syllabus_id"] not in shared]
    if own:
        for r in tx.all("SELECT node_id, data FROM picture_lessons WHERE node_id = ANY(%s) AND status='approved'", [own]):
            out[r["node_id"]] = r["data"]
    return out


def lesson_for(tx: Tx, topic: dict[str, Any]) -> dict[str, Any] | None:
    """One topic's approved picture lesson, or None."""
    return lessons_for(tx, [topic]).get(topic["id"])


def has_lesson_for(tx: Tx, topic: dict[str, Any]) -> bool:
    return lesson_for(tx, topic) is not None


def _lesson_or_404(tx: Tx, topic: dict[str, Any]) -> dict[str, Any]:
    lesson = lesson_for(tx, topic)
    if lesson is None:
        raise NotFound("This topic doesn't have a picture lesson yet.")
    return lesson


def _public(lesson: dict[str, Any]) -> dict[str, Any]:
    """The lesson without quiz answers (they are checked on the server)."""
    quiz = [{k: v for k, v in q.items() if k not in ("answer", "why")} for q in lesson["quiz"]]
    return {**lesson, "quiz": quiz}


def _require_open(tx: Tx, actor: Actor, topic_id: int) -> None:
    from novaquest.services import learn

    learn._require_open(tx, actor, topic_id)


def done_topics(tx: Tx, student_id: int) -> set[int]:
    return {r["topic_id"] for r in tx.all("SELECT DISTINCT topic_id FROM learn_events WHERE student_id=%s AND kind='pictures'",
                                          [student_id])}


def index(tx: Tx, actor: Actor, syllabus_id: int | None = None) -> list[dict[str, Any]]:
    """The picture-lesson module: open topics by chapter, with how many cards and whether finished."""
    from novaquest.services import learn

    done = done_topics(tx, actor.user_id)
    chapters_in = learn.topics_for_student(tx, actor, syllabus_id)
    lessons = lessons_for(tx, [t for ch in chapters_in for t in ch["topics"]])
    chapters = []
    for ch in chapters_in:
        topics = [{**t, "cards": len(lessons[t["id"]]["cards"]), "done": t["id"] in done, "crux": lessons[t["id"]]["crux"]}
                  for t in ch["topics"] if t["id"] in lessons]
        if topics:
            chapters.append({**ch, "topics": topics, "done": sum(t["done"] for t in topics)})
    return chapters


def deck(tx: Tx, actor: Actor, topic_id: int) -> dict[str, Any]:
    """One topic's picture lesson for a student (answers removed)."""
    require(actor, "student")
    _require_open(tx, actor, topic_id)
    topic = syllabus.get_topic(tx, topic_id)
    lesson = _lesson_or_404(tx, topic)
    nxt = tx.one("""SELECT n.id, n.code, n.title, n.syllabus_id FROM syllabus_nodes n
                    WHERE n.parent_id=%s AND n.kind='topic' AND n.ord > (SELECT ord FROM syllabus_nodes WHERE id=%s)
                    ORDER BY n.ord LIMIT 1""", [topic["chapter_id"], topic_id])
    return {"topic": topic, "lesson": _public(lesson), "done": topic_id in done_topics(tx, actor.user_id),
            "next_topic": nxt if nxt and has_lesson_for(tx, nxt) else None}


def preview(tx: Tx, actor: Actor, topic_id: int) -> dict[str, Any]:
    """Teachers and leaders see the full lesson, with quiz answers, to review it before students do."""
    require(actor, "teacher", "leader", "inst_admin", "platform_admin")
    topic = syllabus.get_topic(tx, topic_id)
    return {"topic": topic, "lesson": _lesson_or_404(tx, topic)}


def check(tx: Tx, actor: Actor, topic_id: int, index_: int, choice: int) -> dict[str, Any]:
    """Mark one picture-quiz answer; it counts as learning evidence like a lesson check."""
    require(actor, "student")
    _require_open(tx, actor, topic_id)
    lesson = _lesson_or_404(tx, syllabus.get_topic(tx, topic_id))
    if not 0 <= index_ < len(lesson["quiz"]):
        raise NotFound("That question was not found.")
    q = lesson["quiz"][index_]
    ok = int(choice) == q["answer"]
    level = mastery.record(tx, actor.user_id, topic_id, "check", 1.0 if ok else 0.0, {"pictures_quiz": index_})
    return {"correct": ok, "why": q["why"], "answer": q["options"][q["answer"]] if ok else None, "level": level}


def finish(tx: Tx, actor: Actor, topic_id: int) -> int:
    """Record that the student went through every card; returns the topic's mastery level."""
    require(actor, "student")
    _require_open(tx, actor, topic_id)
    _lesson_or_404(tx, syllabus.get_topic(tx, topic_id))
    if topic_id in done_topics(tx, actor.user_id):
        return mastery.recompute(tx, actor.user_id, topic_id)
    return mastery.record(tx, actor.user_id, topic_id, "pictures", None)


def class_progress(tx: Tx, class_id: int) -> dict[int, int]:
    """For teachers: how many students in a class finished each topic's picture lesson."""
    rows = tx.all("""SELECT e.topic_id, count(DISTINCT e.student_id) AS n FROM learn_events e
                     JOIN enrolments en ON en.student_id=e.student_id AND en.class_id=%s AND en.status='active'
                     WHERE e.kind='pictures' GROUP BY e.topic_id""", [class_id])
    return {r["topic_id"]: r["n"] for r in rows}

