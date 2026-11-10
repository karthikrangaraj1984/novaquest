"""Escape rooms: learn AI through AI (LN-12, DB-8).

Each chapter of a book has a room: a short story and four locks. A school's own book gets its rooms from Claude
(drafted per chapter, checked by the same rules as below, and approved by the school admin: table escape_rooms); the
retired NCERT 2018 book's rooms are files in data/escape. A room is (book, chapter). Every lock joins a science idea from
the chapter with one AI skill, and is something to DO, not another quiz question:

- sort      Train NOVA's sorter: choose the clues NOVA looks at, then watch it sort new cases. A clue that only
            happened to fit the examples ('looks green') fools it.
- clean     Fix NOVA's training data: find the examples that are labelled wrongly.
- evidence  Check NOVA's claim: decide whether a confident claim is right, and pick the evidence that settles it.
- pattern   Pattern or cause: read a small data table and choose the honest conclusion or prediction.

Solving a lock reveals one digit of the door code; with all four, the student types the code and escapes. Tries and
hints set each lock's score, which is how AI skills are measured. Rooms live in data/escape/chNN.json and are
validated when loaded (including that every sorting lock can be solved and that the choice of clues matters)."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import Any

from novaquest.core import audit, clock
from novaquest.core.db import Tx, jsonb
from novaquest.core.errors import Conflict, Forbidden, NotFound, ValidationError
from novaquest.services import mastery, syllabus
from novaquest.services.access import Actor, require, require_class, student_class_id

ROOT = Path(__file__).resolve().parents[2]
CONTENT = ROOT / "data" / "escape"
LOCKS = 4
KINDS = ("sort", "clean", "evidence", "pattern")
SKILLS = {  # the six AI skills, in the order students meet them
    "features": ("Sorting by clues", "AI sorts things by the clues (features) it is told to look at. Good clues are real differences."),
    "training": ("Clean, fair training data", "An AI learns from examples. Wrong or one-sided examples teach it wrong ideas."),
    "checking": ("Checking AI claims", "Sounding sure is not the same as being right. Check AI answers against evidence."),
    "cause": ("Pattern or cause?", "Two things changing together does not prove one causes the other."),
    "data": ("Learning from data", "AI finds patterns in data. More, better data gives better patterns."),
    "predicting": ("Predictions and uncertainty", "Predictions from data can be wrong, especially far outside what the data covers."),
}
STRONG_AT = 0.7  # average lock score for a skill to count as strong


class ContentError(ValueError):
    """An escape room file is invalid."""


# ---------- content ----------
def _need(cond: bool, where: str, msg: str) -> None:
    if not cond:
        raise ContentError(f"{where}: {msg}")


def _text(v: Any, where: str, limit: int = 320) -> None:
    _need(isinstance(v, str) and v.strip() != "", where, "needs text")
    _need(len(v) <= limit, where, f"longer than {limit} characters")


def classify(lock: dict, chosen: list[str]) -> list[dict]:
    """How NOVA sorts each test case using only the chosen clues: the label of the most similar training example(s)
    (fewest differing clues); if equally similar examples disagree, NOVA isn't sure."""
    out = []
    for case in lock["test"]:
        dist = [(sum(case["f"][k] != ex["f"][k] for k in chosen), ex["label"]) for ex in lock["train"]]
        best = min(d for d, _ in dist)
        labels = {lab for d, lab in dist if d == best}
        guess = labels.pop() if len(labels) == 1 else None
        out.append({"name": case["name"], "guess": guess, "right": case["label"], "ok": guess == case["label"]})
    return out


def solving_sets(lock: dict) -> list[tuple[str, ...]]:
    """Every choice of clues that sorts all the test cases correctly."""
    keys = [f["key"] for f in lock["features"]]
    return [c for n in range(1, len(keys) + 1) for c in combinations(keys, n) if all(r["ok"] for r in classify(lock, list(c)))]


def validate_lock(lock: dict, where: str, topics: set[str]) -> None:
    _need(lock.get("kind") in KINDS, where, f"unknown kind {lock.get('kind')!r}")
    _need(lock.get("skill") in SKILLS, where, f"unknown skill {lock.get('skill')!r}")
    _need(lock.get("topic") in topics, where, f"topic {lock.get('topic')!r} isn't in this chapter")
    for k in ("title", "intro", "hint", "learn"):
        _text(lock.get(k), f"{where}.{k}", 420 if k == "learn" else 320)
    kind = lock["kind"]
    if kind == "sort":
        labels, feats = lock.get("labels", []), lock.get("features", [])
        _need(2 <= len(labels) <= 3, where, "a sorter needs 2 or 3 labels")
        _need(3 <= len(feats) <= 5, where, "offer 3 to 5 clues")
        keys = [f.get("key") for f in feats]
        _need(len(set(keys)) == len(keys), where, "clue keys must differ")
        for f in feats:
            _text(f.get("text"), f"{where}.features", 80)
        for part, low in (("train", 4), ("test", 2)):
            cases = lock.get(part, [])
            _need(len(cases) >= low, where, f"needs at least {low} {part} cases")
            for i, c in enumerate(cases):
                _text(c.get("name"), f"{where}.{part}[{i}]", 60)
                _need(c.get("label") in range(len(labels)), f"{where}.{part}[{i}]", "label out of range")
                _need(set(c.get("f", {})) == set(keys) and all(v in (0, 1) for v in c["f"].values()),
                      f"{where}.{part}[{i}]", "every clue must be 0 or 1")
        _need({c["label"] for c in lock["train"]} == set(range(len(labels))), where, "train needs every label")
        sets = solving_sets(lock)
        _need(bool(sets), where, "no choice of clues sorts every test case: unsolvable")
        all_sets = 2 ** len(keys) - 1
        _need(len(sets) < all_sets, where, "every choice of clues works: the choice doesn't matter")
    elif kind == "clean":
        rows = lock.get("rows", [])
        _need(5 <= len(rows) <= 9, where, "use 5 to 9 training examples")
        wrong = [r for r in rows if r.get("wrong")]
        _need(1 <= len(wrong) <= 3, where, "1 to 3 examples should be wrong")
        for i, r in enumerate(rows):
            _text(r.get("item"), f"{where}.rows[{i}].item", 80)
            _text(r.get("label"), f"{where}.rows[{i}].label", 90)
            if r.get("wrong"):
                _text(r.get("why"), f"{where}.rows[{i}].why", 200)
    elif kind == "evidence":
        _text(lock.get("claim"), f"{where}.claim")
        _text(lock.get("confidence"), f"{where}.confidence", 20)
        _need(lock.get("verdict") in (0, 1), where, "verdict is 0 (NOVA is right) or 1 (NOVA is wrong)")
        ev = lock.get("evidence", [])
        _need(3 <= len(ev) <= 4, where, "offer 3 or 4 pieces of evidence")
        for i, e in enumerate(ev):
            _text(e, f"{where}.evidence[{i}]", 200)
        _need(lock.get("right") in range(len(ev)), where, "right evidence out of range")
    elif kind == "pattern":
        t = lock.get("table")
        if t is not None:
            _need(2 <= len(t.get("cols", [])) <= 4 and 3 <= len(t.get("rows", [])) <= 8, where, "table: 2-4 columns, 3-8 rows")
            _need(all(len(r) == len(t["cols"]) for r in t["rows"]), where, "table rows must match columns")
        _text(lock.get("question"), f"{where}.question")
        opts = lock.get("options", [])
        _need(3 <= len(opts) <= 4 and len(set(opts)) == len(opts), where, "3 or 4 different options")
        for o in opts:
            _text(o, f"{where}.options", 200)
        _need(lock.get("answer") in range(len(opts)), where, "answer out of range")


def validate_room(room: dict, topics: set[str], where: str) -> None:
    for k in ("title", "story", "escaped"):
        _text(room.get(k), f"{where}.{k}", 420)
    locks = room.get("locks", [])
    _need(len(locks) == LOCKS, where, f"a room has exactly {LOCKS} locks")
    _need(len({lk["kind"] for lk in locks}) >= 3, where, "use at least 3 different kinds of lock")
    for i, lk in enumerate(locks, 1):
        validate_lock(lk, f"{where}.lock{i}", topics)


_SKILL_OF = {"sort": ("features",), "clean": ("training",), "evidence": ("checking",), "pattern": ("cause", "data", "predicting")}


def from_draft(room: Any, topics: set[str]) -> dict[str, Any]:
    """A drafted room made playable: its first four locks that pass every check, with at least three kinds (the
    draft has a spare). Each lock's skill follows its kind. Raises ContentError saying what was wrong."""
    _need(isinstance(room, dict), "room", "not a room")
    for k in ("title", "story", "escaped"):
        _text(room.get(k), k, 60 if k == "title" else 420)
    good, problems = [], []
    for i, lk in enumerate(room.get("locks") or [], 1):
        if not isinstance(lk, dict):
            continue
        lk = dict(lk)
        allowed = _SKILL_OF.get(lk.get("kind"), ())
        if allowed and lk.get("skill") not in allowed:
            lk["skill"] = allowed[-1] if lk.get("kind") == "pattern" else allowed[0]
        try:
            validate_lock(lk, f"lock {i} ({lk.get('kind')})", topics)
            good.append(lk)
        except ContentError as exc:
            problems.append(str(exc))
    picked: list[dict] = []
    for kind in KINDS:  # one of each kind first, then any other good lock
        picked += [lk for lk in good if lk["kind"] == kind][:1]
    picked += [lk for lk in good if lk not in picked]
    picked = sorted(picked[:LOCKS], key=lambda lk: KINDS.index(lk["kind"]))
    if len(picked) < LOCKS or len({lk["kind"] for lk in picked}) < 3:
        raise ContentError("; ".join(problems) or f"a room needs {LOCKS} good locks of at least 3 kinds")
    out = {"title": room["title"].strip(), "story": room["story"].strip(), "escaped": room["escaped"].strip(), "locks": picked}
    validate_room(out, topics, "room")
    return out


@lru_cache(maxsize=1)
def rooms() -> dict[int, dict[str, Any]]:
    """Every room by chapter number, validated against the NCERT syllabus map."""
    spec = json.loads((ROOT / "data" / "syllabus" / "ncert_g8_science.json").read_text(encoding="utf-8"))
    topics = {int(c["no"]): {f"{c['no']}.{i}" for i in range(1, len(c["topics"]) + 1)} for c in spec["chapters"]}
    out: dict[int, dict[str, Any]] = {}
    for f in sorted(CONTENT.glob("ch*.json")):
        room = json.loads(f.read_text(encoding="utf-8"))
        no = int(room.get("chapter", 0))
        _need(no in topics and no not in out, f.name, f"chapter {no} unknown or defined twice")
        validate_room(room, topics[no], f.name)
        out[no] = room
    return out


def _builtin(book: dict) -> bool:
    """The book whose rooms are the files in data/escape: the shared NCERT Grade 8 Science book (retired in 1.12.0)."""
    return book["institution_id"] is None and book["board"] == "NCERT" and book["subject"] == "Science" and str(book["grade"]) == "8"


def code_for(chapter: int, book_id: int | None = None) -> str:
    """The room's door code: one digit per lock, fixed per book and chapter (so a class can't share one code). The
    built-in NCERT rooms (book_id None) keep the codes they always had."""
    seed = f"novaquest-escape-{chapter}" if book_id is None else f"novaquest-escape-{book_id}-{chapter}"
    h = hashlib.sha256(seed.encode()).hexdigest()
    return "".join(str(int(h[i * 2:i * 2 + 2], 16) % 10) for i in range(LOCKS))


def _code(book: dict, chapter: int) -> str:
    return code_for(chapter, None if _builtin(book) else book["id"])


def book_rooms(tx: Tx, book: dict, approved_only: bool = True) -> dict[int, dict[str, Any]]:
    """A book's rooms by chapter number: the built-in files for the NCERT book, else the rooms drafted for it."""
    if _builtin(book):
        return dict(rooms())
    rows = tx.all("SELECT chapter, room, status FROM escape_rooms WHERE syllabus_id=%s" +
                  (" AND status='approved'" if approved_only else "") + " ORDER BY chapter", [book["id"]])
    return {r["chapter"]: {**r["room"], "status": r["status"]} for r in rows}


def get_room(chapter: int) -> dict[str, Any]:
    """A built-in room (the retired NCERT book's, from data/escape)."""
    room = rooms().get(chapter)
    if not room:
        raise NotFound("There's no escape room for that chapter yet.")
    return room


def room_of(tx: Tx, book: dict, chapter: int) -> dict[str, Any]:
    room = book_rooms(tx, book).get(chapter)
    if not room:
        raise NotFound("There's no escape room for that chapter yet.")
    return room


# ---------- playing ----------
def _student_books(tx: Tx, actor: Actor) -> list[dict]:
    return syllabus.student_syllabi(tx, actor.user_id)


def _book_for(tx: Tx, actor: Actor, chapter: int, book_id: int | None) -> dict:
    """The student's book a room belongs to: the one asked for, else the first of the class's books with a room for
    that chapter."""
    books = _student_books(tx, actor)
    if book_id is not None:
        books = [b for b in books if b["id"] == book_id]
    for b in books:
        if chapter in book_rooms(tx, b):
            return b
    raise NotFound("Your class doesn't have this escape room.")


def _chapters(tx: Tx, book: dict) -> dict[int, dict]:
    return {int(ch["code"]): ch for ch in syllabus.get_tree(tx, book["id"]) if str(ch["code"]).isdigit()}


def _run(tx: Tx, actor: Actor, book: dict, chapter: int, room: dict) -> dict:
    run = tx.one("SELECT * FROM escape_runs WHERE student_id=%s AND syllabus_id=%s AND chapter=%s",
                 [actor.user_id, book["id"], chapter])
    if run:
        return run
    rid = tx.val("""INSERT INTO escape_runs(institution_id, student_id, syllabus_id, chapter, started_at) VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (student_id, syllabus_id, chapter) DO UPDATE SET chapter=EXCLUDED.chapter RETURNING id""",
                 [tx.inst, actor.user_id, book["id"], chapter, clock.now()])
    for i, lk in enumerate(room["locks"], 1):
        tx.exec("""INSERT INTO escape_locks(run_id, lock_no, institution_id, skill) VALUES (%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""", [rid, i, tx.inst, lk["skill"]])
    return tx.one("SELECT * FROM escape_runs WHERE id=%s", [rid])


def _assigned_rooms(tx: Tx, student_id: int) -> set[tuple[int, int]]:
    """(book, chapter) of the escape rooms set as open work for this student (1.28.0)."""
    class_id = student_class_id(tx, student_id)
    rows = tx.all("""SELECT DISTINCT (q.answer->>'syllabus_id')::bigint AS sid, (q.answer->>'chapter')::int AS ch
        FROM assignments a JOIN assignment_questions aq ON aq.assignment_id=a.id JOIN questions q ON q.id=aq.question_id
        WHERE q.kind='escape' AND a.state <> 'draft' AND a.opens_at <= %(now)s AND EXISTS (SELECT 1 FROM assignment_targets t
          WHERE t.assignment_id=a.id AND ((t.target_kind='class' AND t.target_id=%(c)s) OR (t.target_kind='student' AND t.target_id=%(s)s)
          OR (t.target_kind='group' AND EXISTS (SELECT 1 FROM group_members g WHERE g.group_id=t.target_id AND g.student_id=%(s)s))))""",
                  {"now": clock.now(), "c": class_id, "s": student_id})
    return {(r["sid"], r["ch"]) for r in rows}


def _open_chapters(tx: Tx, student_id: int, book: dict, chapters: dict[int, dict]) -> set[int]:
    """Chapter numbers of the book whose room the student may play: the chapter is open for self-study (at least one
    topic open), or the room is set as work for them (1.28.0)."""
    from novaquest.services import learn

    closed = learn.closed_topics(tx, student_class_id(tx, student_id))
    assigned = {ch for sid, ch in _assigned_rooms(tx, student_id) if sid == book["id"]}
    return {no for no, ch in chapters.items() if no in assigned or any(t["id"] not in closed for t in ch["topics"])}


def progress_assignments(tx: Tx, actor: Actor, book_id: int, chapter: int) -> list[int]:
    """Record the student's progress in this room on any escape-room work they have open (share of locks opened;
    done once escaped). Returns the attempts now complete, for the caller to hand in."""
    run = tx.one("""SELECT r.status, count(l.solved_at) AS solved, count(*) AS locks FROM escape_runs r
                    JOIN escape_locks l ON l.run_id=r.id WHERE r.student_id=%s AND r.syllabus_id=%s AND r.chapter=%s
                    GROUP BY r.id""", [actor.user_id, book_id, chapter])
    if not run:
        return []
    escaped = run["status"] == "escaped"
    score = 1.0 if escaped else round(int(run["solved"]) / max(1, int(run["locks"])), 3)
    rows = tx.all("""SELECT t.id AS attempt_id, q.id AS question_id FROM attempts t
        JOIN assignment_questions aq ON aq.assignment_id=t.assignment_id JOIN questions q ON q.id=aq.question_id
        WHERE t.student_id=%s AND t.status='in_progress' AND t.deadline_at > %s AND q.kind='escape' AND q.status='approved'
          AND (q.answer->>'syllabus_id')::bigint=%s AND (q.answer->>'chapter')::int=%s""",
                  [actor.user_id, clock.now(), book_id, chapter])
    done = []
    for r in rows:
        tx.exec("""INSERT INTO responses(institution_id, attempt_id, question_id, answer, saved_at) VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (attempt_id, question_id) DO UPDATE SET answer=EXCLUDED.answer, saved_at=EXCLUDED.saved_at""",
                [tx.inst, r["attempt_id"], r["question_id"], jsonb({"done": escaped, "score": score}), clock.now()])
        if escaped:
            done.append(r["attempt_id"])
    return done


def student_rooms(tx: Tx, actor: Actor) -> list[dict[str, Any]]:
    """Every room the student can play: each of the class's books in turn, in chapter order, with progress."""
    require(actor, "student")
    runs = {(r["syllabus_id"], r["chapter"]): r for r in tx.all(
        """SELECT r.syllabus_id, r.chapter, r.status, count(l.solved_at) AS solved FROM escape_runs r
           JOIN escape_locks l ON l.run_id=r.id WHERE r.student_id=%s GROUP BY r.id""", [actor.user_id])}
    out = []
    for book in _student_books(tx, actor):
        rooms_ = book_rooms(tx, book)
        if not rooms_:
            continue
        chapters = _chapters(tx, book)
        playable = _open_chapters(tx, actor.user_id, book, chapters)
        for no, room in sorted(rooms_.items()):
            if no not in chapters or no not in playable:
                continue
            r = runs.get((book["id"], no))
            out.append({"chapter": no, "book_id": book["id"], "book": book["label"], "part": book.get("part"), "chapter_title": chapters[no]["title"],
                        "title": room["title"], "story": room["story"],
                        "solved": int(r["solved"]) if r else 0, "escaped": bool(r and r["status"] == "escaped"),
                        "started": bool(r), "skills": sorted({lk["skill"] for lk in room["locks"]}, key=list(SKILLS).index)})
    return out


def _score(tries: int, hints: int) -> float:
    """1.0 for a first-try solve without a hint; each wrong try costs 0.25 and each hint 0.15 (never below 0.2)."""
    return round(max(0.2, 1.0 - 0.25 * (tries - 1) - 0.15 * hints), 2)


def view(tx: Tx, actor: Actor, chapter: int, book_id: int | None = None) -> dict[str, Any]:
    """The room as the student sees it: story, each lock's state, the current lock and the digits found so far."""
    require(actor, "student")
    book = _book_for(tx, actor, chapter, book_id)
    chapters = _chapters(tx, book)
    if chapter not in chapters:
        raise NotFound("Your class doesn't have this escape room.")
    if chapter not in _open_chapters(tx, actor.user_id, book, chapters):
        raise NotFound("This escape room isn't open yet. Your teacher will open it with its chapter.")
    room = room_of(tx, book, chapter)
    run = _run(tx, actor, book, chapter, room)
    states = {r["lock_no"]: r for r in tx.all("SELECT * FROM escape_locks WHERE run_id=%s ORDER BY lock_no", [run["id"]])}
    code = _code(book, chapter)
    locks = []
    for i, lk in enumerate(room["locks"], 1):
        s = states[i]
        locks.append({"no": i, **lk, "solved": s["solved_at"] is not None, "tries": s["tries"], "hints": s["hints"],
                      "digit": code[i - 1] if s["solved_at"] else None, "skill_name": SKILLS[lk["skill"]][0],
                      "last": s["last_answer"]})
    current = next((lk for lk in locks if not lk["solved"]), None)
    return {"room": room, "chapter": chapters[chapter], "no": chapter, "book": book, "run": run, "locks": locks,
            "current": current, "all_solved": current is None, "escaped": run["status"] == "escaped"}


def _feedback(lock: dict, answer: dict) -> tuple[bool, str, dict]:
    """Check one answer: (solved, what to tell the student, details to show)."""
    kind = lock["kind"]
    if kind == "sort":
        keys = {f["key"] for f in lock["features"]}
        chosen = [k for k in answer.get("features", []) if k in keys]
        if not chosen:
            raise ValidationError("Choose at least one clue for NOVA.")
        results = classify(lock, chosen)
        wrong = [r for r in results if not r["ok"]]
        if not wrong:
            return True, "NOVA sorted every new case correctly.", {"results": results, "chosen": chosen}
        return False, (f"NOVA got {len(wrong)} of {len(results)} new cases wrong or wasn't sure. Look at which clues "
                       "really tell the groups apart."), {"results": results, "chosen": chosen}
    if kind == "clean":
        flagged = {int(i) for i in answer.get("flag", []) if str(i).isdigit() and int(i) < len(lock["rows"])}
        if not flagged:
            raise ValidationError("Tick the examples you think are labelled wrongly.")
        wrong = {i for i, r in enumerate(lock["rows"]) if r.get("wrong")}
        if flagged == wrong:
            return True, "You found every wrong example. NOVA can learn from clean data now.", {"flagged": sorted(flagged)}
        missed, extra = len(wrong - flagged), len(flagged - wrong)
        parts = []
        if extra:
            parts.append(f"{extra} of the ones you ticked {'is' if extra == 1 else 'are'} actually right")
        if missed:
            parts.append(f"{missed} wrong example{'s are' if missed != 1 else ' is'} still hiding")
        return False, "Not yet: " + " and ".join(parts) + ".", {"flagged": sorted(flagged)}
    if kind == "evidence":
        v, e = answer.get("verdict"), answer.get("evidence")
        if v is None or e is None:
            raise ValidationError("Decide whether NOVA is right, and pick the evidence.")
        v_ok, e_ok = int(v) == lock["verdict"], int(e) == lock["right"]
        if v_ok and e_ok:
            return True, "Right, and you backed it with evidence.", {}
        if v_ok:
            return False, "Your verdict is right, but that evidence doesn't settle it. Which fact decides?", {}
        if e_ok:
            return False, "That's the key evidence, but look again at what it says about NOVA's claim.", {}
        return False, "Not yet. Read NOVA's claim again and find the fact that tests it.", {}
    choice = answer.get("choice")
    if choice is None:
        raise ValidationError("Choose an answer.")
    if int(choice) == lock["answer"]:
        return True, "That's the honest conclusion.", {}
    return False, "Not quite. Look at the data again: what does it really show, and what doesn't it show?", {}


def attempt(tx: Tx, actor: Actor, chapter: int, lock_no: int, answer: dict, book_id: int | None = None) -> dict[str, Any]:
    """Try to open a lock. Locks open in order; a solved lock records its score as AI-skill evidence and gives the
    student a learning event on the lock's topic."""
    v = view(tx, actor, chapter, book_id)
    if v["current"] is None or v["current"]["no"] != lock_no:
        raise Conflict("Open the locks in order: this one isn't next.")
    lock = v["room"]["locks"][lock_no - 1]
    solved, message, detail = _feedback(lock, answer)
    st = tx.one("SELECT tries, hints FROM escape_locks WHERE run_id=%s AND lock_no=%s", [v["run"]["id"], lock_no])
    tries = st["tries"] + 1
    score = _score(tries, st["hints"]) if solved else None
    tx.exec("""UPDATE escape_locks SET tries=%s, last_answer=%s, score=%s, solved_at=%s WHERE run_id=%s AND lock_no=%s""",
            [tries, json.dumps(answer), score, clock.now() if solved else None, v["run"]["id"], lock_no])
    if solved:
        topic = tx.val("""SELECT n.id FROM syllabus_nodes n WHERE n.syllabus_id=%s AND n.kind='topic' AND n.code=%s""",
                       [v["book"]["id"], lock["topic"]])
        if topic:
            mastery.record(tx, actor.user_id, topic, "escape", score, {"chapter": chapter, "lock": lock_no})
    return {"solved": solved, "message": message, "detail": detail, "score": score,
            "digit": _code(v["book"], chapter)[lock_no - 1] if solved else None, "learn": lock["learn"] if solved else None}


def hint(tx: Tx, actor: Actor, chapter: int, lock_no: int, book_id: int | None = None) -> str:
    """Show the current lock's hint (it costs a little of the lock's score)."""
    v = view(tx, actor, chapter, book_id)
    if v["current"] is None or v["current"]["no"] != lock_no:
        raise Conflict("That lock isn't the one you're on.")
    tx.exec("UPDATE escape_locks SET hints=hints+1 WHERE run_id=%s AND lock_no=%s AND hints=0", [v["run"]["id"], lock_no])
    return v["room"]["locks"][lock_no - 1]["hint"]


def open_door(tx: Tx, actor: Actor, chapter: int, code: str, book_id: int | None = None) -> bool:
    """Type the four digits to escape. False if the code is wrong; Conflict if locks are still closed."""
    v = view(tx, actor, chapter, book_id)
    if v["escaped"]:
        return True
    if not v["all_solved"]:
        raise Conflict("Open all four locks first: each one gives a digit of the code.")
    if "".join(ch for ch in code if ch.isdigit()) != _code(v["book"], chapter):
        return False
    tx.exec("UPDATE escape_runs SET status='escaped', escaped_at=%s WHERE id=%s", [clock.now(), v["run"]["id"]])
    audit.record(tx, "escape.escaped", actor_id=actor.user_id, target=f"room:{v['book']['id']}:{chapter}")
    return True


# ---------- AI skills ----------
def _level(scores: list[float]) -> str:
    if not scores:
        return "not_yet"
    return "strong" if len(scores) >= 2 and sum(scores) / len(scores) >= STRONG_AT else "learning"


def student_skills(tx: Tx, student_id: int) -> list[dict[str, Any]]:
    """Each AI skill for one student: not yet / learning / strong, from the locks they've opened."""
    rows = tx.all("""SELECT l.skill, l.score FROM escape_locks l JOIN escape_runs r ON r.id=l.run_id
                     WHERE r.student_id=%s AND l.solved_at IS NOT NULL""", [student_id])
    out = []
    for key, (name, about) in SKILLS.items():
        sc = [float(r["score"]) for r in rows if r["skill"] == key]
        out.append({"key": key, "name": name, "about": about, "level": _level(sc), "locks": len(sc)})
    return out


def class_view(tx: Tx, actor: Actor, class_id: int) -> dict[str, Any]:
    """For a teacher: every student x room (escaped, how many locks, or not started) and each AI skill across the
    class (average lock score and how many students are strong). Two queries."""
    require(actor, "teacher", "leader", "inst_admin")
    require_class(tx, actor, class_id)
    students = tx.all("""SELECT u.id, u.name, e.roll_no FROM enrolments e JOIN users u ON u.id=e.student_id
                         WHERE e.class_id=%s AND e.status='active' ORDER BY length(e.roll_no), e.roll_no""", [class_id])
    ids = [s["id"] for s in students]
    runs = tx.all("""SELECT r.student_id, r.syllabus_id, r.chapter, r.status, count(l.solved_at) AS solved FROM escape_runs r
                     JOIN escape_locks l ON l.run_id=r.id WHERE r.student_id = ANY(%s) GROUP BY r.id""", [ids])
    locks = tx.all("""SELECT r.student_id, l.skill, l.score FROM escape_locks l JOIN escape_runs r ON r.id=l.run_id
                      WHERE r.student_id = ANY(%s) AND l.solved_at IS NOT NULL""", [ids])
    cells = {(r["student_id"], r["syllabus_id"], r["chapter"]): ("escaped" if r["status"] == "escaped" else int(r["solved"]))
             for r in runs}
    rooms_ = []
    for b in syllabus.class_syllabi(tx, class_id):
        chs = _chapters(tx, b)
        rooms_ += [{"book_id": b["id"], "book": b["label"], "part": b.get("part"), "no": no, "room": room,
                    "chapter_id": chs[no]["id"] if no in chs else None} for no, room in sorted(book_rooms(tx, b).items())]
    books_ = sorted({(r["book_id"], r["book"]) for r in rooms_}, key=lambda x: [r["book_id"] for r in rooms_].index(x[0]))
    skills = []
    for key, (name, about) in SKILLS.items():
        per = {}
        for r in locks:
            if r["skill"] == key:
                per.setdefault(r["student_id"], []).append(float(r["score"]))
        allsc = [x for v in per.values() for x in v]
        skills.append({"key": key, "name": name, "about": about, "students": len(per),
                       "strong": sum(1 for v in per.values() if _level(v) == "strong"),
                       "avg": round(100 * sum(allsc) / len(allsc)) if allsc else None})
    return {"students": students, "rooms": rooms_, "books": books_, "cells": cells, "skills": skills,
            "escapes": sum(1 for (_, b, n), v in cells.items() if v == "escaped" and any(
                r["book_id"] == b and r["no"] == n for r in rooms_))}


def school_skill_avg(tx: Tx, class_ids: list[int]) -> list[dict[str, Any]]:
    """AI skills for the principal: average lock score per skill across classes, with how many students."""
    rows = tx.all("""SELECT l.skill, round(100 * avg(l.score)) AS pct, count(DISTINCT r.student_id) AS students
                     FROM escape_locks l JOIN escape_runs r ON r.id=l.run_id
                     JOIN enrolments e ON e.student_id=r.student_id AND e.status='active' AND e.class_id = ANY(%s)
                     WHERE l.solved_at IS NOT NULL GROUP BY l.skill""", [class_ids])
    by = {r["skill"]: r for r in rows}
    return [{"concept": SKILLS[k][0], "pct": by[k]["pct"], "students": by[k]["students"]} for k in SKILLS if k in by]


def guide(tx: Tx, actor: Actor, chapter: int, book_id: int, drafts: bool = False) -> dict[str, Any]:
    """The answer guide a teacher can open before class: the room, each lock's answer and what it teaches. The school
    admin also uses it to check a drafted room before approving it (drafts=True)."""
    require(actor, "teacher", "leader", "inst_admin")
    book = syllabus.get_syllabus(tx, book_id)
    room = book_rooms(tx, book, approved_only=not drafts).get(chapter)
    if not room:
        raise NotFound("There's no escape room for that chapter yet.")
    locks = []
    for i, lk in enumerate(room["locks"], 1):
        g = {"no": i, **lk, "skill_name": SKILLS[lk["skill"]][0]}
        if lk["kind"] == "sort":
            sets = solving_sets(lk)
            best = min(sets, key=len)
            g["answer_text"] = "Simplest clue choice that works: " + ", ".join(f["text"] for f in lk["features"] if f["key"] in best)
            g["trap"] = [f["text"] for f in lk["features"] if (f["key"],) not in sets]  # clues that fool NOVA on their own
        elif lk["kind"] == "clean":
            g["answer_text"] = "Wrong examples: " + "; ".join(f"{r['item']} ({r['why']})" for r in lk["rows"] if r.get("wrong"))
        elif lk["kind"] == "evidence":
            g["answer_text"] = ("NOVA is right" if lk["verdict"] == 0 else "NOVA is wrong") + ". Evidence: " + lk["evidence"][lk["right"]]
        else:
            g["answer_text"] = lk["options"][lk["answer"]]
        locks.append(g)
    return {"room": room, "no": chapter, "book": book, "locks": locks, "code": _code(book, chapter)}


def ensure_student_can_play(tx: Tx, actor: Actor) -> None:
    if student_class_id(tx, actor.user_id) is None:
        raise Forbidden("You aren't in a class yet.")
