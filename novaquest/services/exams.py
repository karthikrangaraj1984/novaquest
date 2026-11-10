"""Exam papers (AS-8): a teacher fixes the paper's pattern (sections of N questions x M marks, e.g. 10 x 1 + 5 x 3),
the difficulty and the topics; NovaQuest writes three different sets (A, B, C) so the teacher can compare and pick
one, swap any question, and print the chosen paper and its marking scheme as PDFs.

Each section's marks decide the kind of question unless the teacher picks one: 1 mark is multiple choice, 2-4 marks
a short answer, 5+ marks a long answer in two parts (explain, then apply). Written answers carry a marking scheme
that shares the section's marks between the points a good answer makes."""
from __future__ import annotations

import re
import time
from types import SimpleNamespace
from typing import Any

from novaquest.core import audit, clock
from novaquest.core.db import Db, Tx, jsonb
from novaquest.core.errors import NotFound, ValidationError
from novaquest.services import maths, questions, syllabus
from novaquest.services.access import Actor, require, require_class
from novaquest.services.context import topic_ctx

SETS = 3
SET_NAMES = "ABC"
KINDS = {"mcq": "Multiple choice", "short": "Short answer", "long": "Long answer", "numeric": "Number problems (show working)"}
_ENGINE_KIND = {"mcq": "mcq", "short": "short", "long": "viva", "numeric": "numeric"}
DIFFICULTIES = {"easy": "Easy", "medium": "Medium", "hard": "Hard", "mixed": "Mixed (30% easy, 50% medium, 20% hard)"}
MIX = (("easy", 0.3), ("medium", 0.5), ("hard", 0.2))
PRESETS = [  # (label, rows of (count, marks)) - common Indian school test patterns
    ("25 × 1 mark (25 marks)", [(25, 1)]),
    ("10 × 1 + 5 × 3 (25 marks)", [(10, 1), (5, 3)]),
    ("20 × 1 + 5 × 2 + 4 × 5 (50 marks)", [(20, 1), (5, 2), (4, 5)]),
    ("10 × 1 + 10 × 2 + 5 × 5 (55 marks)", [(10, 1), (10, 2), (5, 5)]),
    ("20 × 1 + 6 × 2 + 7 × 3 + 3 × 4 + 3 × 5 (80 marks)", [(20, 1), (6, 2), (7, 3), (3, 4), (3, 5)]),
]
MAX_QUESTIONS = 60
MAX_SECTIONS = 6


def default_kind(marks: float, maths: bool = False) -> str:
    """1 mark: multiple choice; 2-4: short answer (a maths paper: 2-5 marks are number problems); more: long answer."""
    if maths and 2 <= marks <= 5:
        return "numeric"
    return "mcq" if marks <= 1 else ("short" if marks <= 4 else "long")


def parse_pattern(rows: list[tuple[Any, Any, Any]], maths: bool = False) -> list[dict[str, Any]]:
    """Sections from the form: (count, marks, kind) rows; blank rows are skipped. Raises ValidationError."""
    out = []
    for count, marks, kind in rows:
        if str(count).strip() in ("", "0") and str(marks).strip() in ("", "0"):
            continue
        try:
            n, m = int(str(count).strip()), float(str(marks).strip())
        except ValueError:
            raise ValidationError("Each section needs a number of questions and marks per question, like 10 and 1.")
        if not 1 <= n <= 50:
            raise ValidationError("A section can have 1 to 50 questions.")
        if m not in (0.5, 1, 1.5, 2, 3, 4, 5, 6, 8, 10):
            raise ValidationError("Marks per question can be ½, 1, 1½, 2, 3, 4, 5, 6, 8 or 10.")
        kind = str(kind or "").strip() or default_kind(m, maths)
        if kind not in KINDS:
            raise ValidationError("Choose the kind of question for each section.")
        if kind == "mcq" and m > 2:
            raise ValidationError("Multiple-choice questions can carry at most 2 marks. Use short or long answers for more.")
        out.append({"count": n, "marks": m, "kind": kind})
    if not out:
        raise ValidationError("Add at least one section, for example 10 questions of 1 mark.")
    if len(out) > MAX_SECTIONS:
        raise ValidationError(f"A paper can have at most {MAX_SECTIONS} sections.")
    if sum(s["count"] for s in out) > MAX_QUESTIONS:
        raise ValidationError(f"A paper can have at most {MAX_QUESTIONS} questions.")
    for i, s in enumerate(out):
        s["name"] = "ABCDEF"[i]
    return out


def total_marks(pattern: list[dict]) -> float:
    return sum(s["count"] * s["marks"] for s in pattern)


def split_marks(marks: float, points: int) -> list[float]:
    """Share a written answer's marks between its marking points in halves: 5 over 3 points is 2, 1½, 1½."""
    if points <= 0:
        return []
    halves = int(round(marks * 2))
    base, extra = divmod(halves, points)
    return [(base + (1 if i < extra else 0)) / 2 for i in range(points)]


def fmt_marks(m: float) -> str:
    whole, half = int(m), m - int(m) >= 0.5
    text = (str(whole) if whole else "") + ("½" if half else "")
    return text or "0"


_BLANK = re.compile(r"_{3,}")


def _sentence_key(text: str) -> str:
    """A textbook sentence by its opening and closing words, so the same sentence with one word blanked or swapped
    in the middle (a 'NOT correct' option) still counts as the same question."""
    words = text.lower().rstrip(". ").split()
    return "s|" + " ".join(words[:5] + ["…"] + words[-4:] if len(words) >= 12 else words)


def _core(q: Any) -> str:
    """What makes two questions the same for a paper: written questions by their wording (ignoring 'answer in your
    own words' tails, and for long answers part 1), multiple choice by wording and answer, and a fill-in-the-blank by
    its whole sentence, and 'which statement' by that statement (so one textbook sentence never appears twice:
    with different words blanked, as a true statement to pick, or with a word swapped as the one that is NOT correct)."""
    prompt = q.prompt.split(" Part 2 (apply): ")[0].replace("Part 1 (explain): ", "")
    for tail in (" Answer in your own words.", " Use an example if you can.", " Explain in your own words."):
        prompt = prompt.replace(tail, "")
    if q.kind in ("mcq", "ai_moment"):
        answer = str((q.answer or {}).get("text"))
        if _BLANK.search(prompt):  # a fill-in-the-blank is its sentence, whichever word is blanked
            return _sentence_key(_BLANK.sub(answer, prompt.split(": ", 1)[-1]))
        if prompt.startswith("Which statement") and len(answer) > 30:  # its sentence (true, or with a word swapped)
            return _sentence_key(answer)
        return prompt.lower() + "|" + answer
    return prompt.lower().strip()


def _difficulty_counts(difficulty: str, n: int) -> list[tuple[str, int]]:
    """How many questions of each difficulty a section of n gets."""
    if difficulty != "mixed":
        return [(difficulty, n)]
    counts = [[d, int(n * share)] for d, share in MIX]
    rest = n - sum(c for _, c in counts)
    for i in (1, 0, 2)[:rest]:  # extra questions go to medium first, then easy, then hard
        counts[i][1] += 1
    return [(d, c) for d, c in counts if c]


def create(db: Db, engine: Any, actor: Actor, *, class_id: int, syllabus_id: int, title: str, topic_ids: list[int],
           pattern: list[dict], difficulty: str, duration_min: int, instructions: str = "", answer_space: bool = False) -> dict:
    """Write three sets of the paper. Returns {'id', 'shared'} where 'shared' counts questions that had to be reused
    across sets because the chosen topics couldn't give enough different ones."""
    require(actor, "teacher")
    if difficulty not in DIFFICULTIES:
        raise ValidationError("Choose the difficulty.")
    if not topic_ids:
        raise ValidationError("Choose at least one topic.")
    if not 10 <= duration_min <= 240:
        raise ValidationError("Time allowed must be between 10 and 240 minutes.")
    with db.tenant(actor.institution_id) as tx:
        require_class(tx, actor, class_id, write=True)
        if syllabus_id not in {b["id"] for b in syllabus.class_syllabi(tx, class_id)}:
            raise ValidationError("That book isn't one of this class's subjects.")
        group = syllabus.parts_of(tx, syllabus_id)  # a book in parts: topics from any of its parts
        if not {syllabus.topic_syllabus_id(tx, t) for t in topic_ids} <= set(group):
            raise ValidationError("Choose topics from this class's book.")
        ctxs = [topic_ctx(tx, t) for t in topic_ids]
    started = time.monotonic()
    sets: list[list[list[Any]]] = [[[] for _ in pattern] for _ in range(SETS)]
    used: list[set[str]] = [set() for _ in range(SETS)]  # what each set already asks, so no paper repeats itself
    order = ("easy", "medium", "hard")
    for si, sec in enumerate(pattern):
        plan = _difficulty_counts(difficulty, sec["count"])
        wanted = [d for d, _ in plan] + ([x for x in order if x not in dict(plan)] if difficulty == "mixed" else [])
        pools = {}
        for d in wanted:  # a big enough pool to reach every chosen topic, starting at a different topic per section
            shift = (si * 3 + order.index(d)) % len(ctxs)
            size = min(60, max(sec["count"] * SETS + 4, 2 * len(ctxs)))
            pool = engine.generate_questions(ctxs[shift:] + ctxs[:shift], _ENGINE_KIND[sec["kind"]], d, size,
                                             seed=class_id * 97 + si * 13 + len(topic_ids) + order.index(d))
            pools[d] = list({_core(q): q for q in reversed(pool)}.values())[::-1]
        for k in range(SETS):  # each set starts at a different place, so the sets differ as much as the pool allows
            chosen: list[tuple[Any, str]] = []
            for d, n in plan:
                avail = [q for q in pools[d] if _core(q) not in used[k]]
                take = avail[k::SETS][:n]
                take += [q for q in avail if q not in take][: n - len(take)]
                if len(take) < n and difficulty == "mixed":  # borrow from the nearest other difficulty
                    for other in sorted((x for x in order if x != d), key=lambda x: abs(order.index(x) - order.index(d))):
                        more = [q for q in pools[other] if _core(q) not in used[k] and q not in take]
                        take += more[: n - len(take)]
                if len(take) < n:
                    kind = KINDS[sec["kind"]].lower()
                    level = "" if difficulty == "mixed" else f" at {d} difficulty"
                    raise ValidationError(f"Only {len(avail)} different {kind} questions could be written{level} for these "
                                          f"topics, but section {sec['name']} needs {n}. Choose more topics, fewer questions, "
                                          "or another kind of question.")
                used[k] |= {_core(q) for q in take}
                chosen += [(q, q.difficulty) for q in take]
            sets[k][si] = chosen
    counts: dict[int, int] = {}
    for k in range(SETS):
        for items in sets[k]:
            for q, _ in items:
                counts[id(q)] = counts.get(id(q), 0) + 1
    shared = sum(c - 1 for c in counts.values() if c > 1)  # questions repeated in another set
    with db.tenant(actor.institution_id) as tx:
        now = clock.now()
        pid = tx.val("""INSERT INTO exam_papers(institution_id, teacher_id, class_id, syllabus_id, title, pattern, difficulty,
                topic_ids, duration_min, instructions, answer_space, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                     [tx.inst, actor.user_id, class_id, syllabus_id, title.strip()[:120] or "Test", jsonb(pattern), difficulty,
                      topic_ids, duration_min, instructions.strip()[:1000] or None, answer_space, now, now])
        ids: dict[int, int] = {}
        for k in range(SETS):
            for si, items in enumerate(sets[k]):
                for i, (q, _) in enumerate(items, 1):
                    if id(q) not in ids:
                        ids[id(q)] = questions._insert_question(tx, q, syllabus_id, actor.user_id)
                    tx.exec("""INSERT INTO exam_paper_items(paper_id, institution_id, set_no, section_no, item_no, question_id, marks)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)""", [pid, tx.inst, k + 1, si + 1, i, ids[id(q)], pattern[si]["marks"]])
        audit.record(tx, "exam.generate", actor_id=actor.user_id, target=f"exam:{pid}",
                     detail={"marks": total_marks(pattern), "questions": sum(s["count"] for s in pattern), "shared": shared,
                             "engine": getattr(engine, "name", "?"), "seconds": round(time.monotonic() - started, 2)})
    return {"id": pid, "shared": shared}


def _paper(tx: Tx, actor: Actor, paper_id: int, *, write: bool = False) -> dict:
    p = tx.one("""SELECT p.*, c.label AS class_label, s.subject, s.board, s.grade FROM exam_papers p
                  JOIN classes c ON c.id=p.class_id JOIN syllabi s ON s.id=p.syllabus_id WHERE p.id=%s""", [paper_id])
    if not p:
        raise NotFound("That exam paper was not found.")
    require_class(tx, actor, p["class_id"], write=write)
    if write and p["teacher_id"] != actor.user_id:
        raise NotFound("That exam paper was not found.")
    return p


def view(tx: Tx, actor: Actor, paper_id: int) -> dict[str, Any]:
    """The paper with its three sets: sections, questions (with marks and marking scheme) and a summary of each set
    (marks per chapter, difficulty and thinking level), plus how many questions each set shares with the others."""
    require(actor, "teacher", "leader", "inst_admin")
    p = _paper(tx, actor, paper_id)
    rows = tx.all("""SELECT i.set_no, i.section_no, i.item_no, i.marks, q.id, q.kind, q.prompt, q.scenario, q.options, q.answer,
            q.rubric, q.difficulty, q.thinking_level, q.topic_id, (CASE WHEN sb.part IS NULL THEN t.code ELSE 'P' || sb.part || '·' || t.code END) AS topic_code, t.title AS topic_title,
            (CASE WHEN sb.part IS NULL THEN ch.code ELSE 'Part ' || sb.part || ' · ' || ch.code END) AS chapter_code, ch.title AS chapter_title
        FROM exam_paper_items i JOIN questions q ON q.id=i.question_id JOIN syllabus_nodes t ON t.id=q.topic_id
        JOIN syllabus_nodes ch ON ch.id=t.parent_id JOIN syllabi sb ON sb.id=t.syllabus_id WHERE i.paper_id=%s ORDER BY i.set_no, i.section_no, i.item_no""", [paper_id])
    sets = []
    for k in range(1, SETS + 1):
        mine = [r for r in rows if r["set_no"] == k]
        sections, no = [], 0
        for si, sec in enumerate(p["pattern"], 1):
            items = []
            for r in (x for x in mine if x["section_no"] == si):
                no += 1
                items.append(_item(r, no))
            sections.append({**sec, "no": si, "items": items, "total": sec["count"] * sec["marks"]})
        by_ch: dict[str, float] = {}
        for r in mine:
            key = f"{r['chapter_code']}. {r['chapter_title']}"
            by_ch[key] = by_ch.get(key, 0) + float(r["marks"])
        marks = sum(float(r["marks"]) for r in mine) or 1
        diff = {d: round(100 * sum(float(r["marks"]) for r in mine if r["difficulty"] == d) / marks) for d in ("easy", "medium", "hard")}
        think = {t: round(100 * sum(float(r["marks"]) for r in mine if r["thinking_level"] == t) / marks)
                 for t in ("remember", "apply", "analyse")}
        sets.append({"no": k, "name": SET_NAMES[k - 1], "sections": sections, "chapters": sorted(by_ch.items(), key=lambda kv: _chapter_order(kv[0])),
                     "difficulty": diff, "thinking": think, "ids": {r["id"] for r in mine}})
    for s in sets:
        others = set().union(*(o["ids"] for o in sets if o is not s))
        s["shared"] = len(s["ids"] & others)
    return {"paper": p, "sets": sets, "total": total_marks(p["pattern"]), "kinds": KINDS, "difficulties": DIFFICULTIES,
            "questions": sum(s["count"] for s in p["pattern"])}


def _chapter_order(label: str) -> tuple:
    """Book order for 'code. title' labels: chapter 8 before chapter 11 (text order would put '11' first), and for a
    book in parts Part 1's chapters before Part 2's ('Part 2 · 1. Title')."""
    m = re.match(r"(?:Part (\d+) · )?(\d+)\.", label)
    return (0, int(m.group(1) or 0), int(m.group(2)), label) if m else (1, 0, 0, label)


def _item(r: dict, no: int) -> dict[str, Any]:
    """One question as printed: number, marks, parts (for long answers), options, answer and marking scheme."""
    marks = float(r["marks"])
    parts = None
    if r["kind"] == "viva" and " Part 2 (apply): " in r["prompt"]:
        a, b = r["prompt"].split(" Part 2 (apply): ", 1)
        parts = [a.replace("Part 1 (explain): ", "").strip(), b.strip()]
    key = r["answer"] or {}
    points = [p.get("point", "") for p in (r["rubric"] or []) if p.get("point")]
    if r["kind"] == "numeric":  # the method's steps, then the final answer with its unit
        points = list(key.get("steps") or [])[:3] + [
            f"Correct final answer: {maths.show(float(key.get('value', 0)), key.get('unit') or '', key.get('decimals'))}"]
    scheme = list(zip(points, split_marks(marks, len(points)))) if r["kind"] != "mcq" else []
    right = key.get("index")
    return {**r, "no": no, "marks": marks, "parts": parts, "scheme": scheme,
            "letter": "abcd"[right] if isinstance(right, int) and right < 4 else None,
            "right_text": key.get("text"), "model": key.get("model") or key.get("worked")}


def choose(tx: Tx, actor: Actor, paper_id: int, set_no: int) -> None:
    """Pick the set to print; its questions are approved (and so join the question bank)."""
    require(actor, "teacher")
    _paper(tx, actor, paper_id, write=True)
    if set_no not in range(1, SETS + 1):
        raise ValidationError("Choose set A, B or C.")
    tx.exec("UPDATE exam_papers SET chosen_set=%s, updated_at=%s WHERE id=%s", [set_no, clock.now(), paper_id])
    tx.exec("""UPDATE questions SET status='approved' WHERE id IN (SELECT question_id FROM exam_paper_items
               WHERE paper_id=%s AND set_no=%s)""", [paper_id, set_no])
    audit.record(tx, "exam.choose", actor_id=actor.user_id, target=f"exam:{paper_id}", detail={"set": SET_NAMES[set_no - 1]})


def replace(db: Db, engine: Any, actor: Actor, paper_id: int, set_no: int, section_no: int, item_no: int) -> int:
    """Swap one question for a new one of the same kind, difficulty and topic that isn't anywhere in the paper yet
    (trying the paper's other topics if that topic has run out). Returns the new question's id."""
    require(actor, "teacher")
    with db.tenant(actor.institution_id) as tx:
        p = _paper(tx, actor, paper_id, write=True)
        cur = tx.one("""SELECT i.question_id, q.kind, q.difficulty, q.topic_id FROM exam_paper_items i JOIN questions q
                        ON q.id=i.question_id WHERE i.paper_id=%s AND i.set_no=%s AND i.section_no=%s AND i.item_no=%s""",
                     [paper_id, set_no, section_no, item_no])
        if not cur:
            raise NotFound("That question isn't in the paper.")
        used = {_core(SimpleNamespace(**r)) for r in tx.all("""SELECT q.kind, q.prompt, q.answer FROM exam_paper_items i
                                                               JOIN questions q ON q.id=i.question_id WHERE i.paper_id=%s""", [paper_id])}
        topics = [cur["topic_id"]] + [t for t in p["topic_ids"] if t != cur["topic_id"]]
        ctxs = [topic_ctx(tx, t) for t in topics]
    for attempt, group in enumerate(([ctxs[0]], ctxs)):
        pool = engine.generate_questions(group, cur["kind"], cur["difficulty"], 12,
                                         seed=paper_id * 7 + set_no * 31 + section_no * 5 + item_no + int(time.time()) % 997)
        fresh = [q for q in pool if _core(q) not in used]
        if fresh:
            break
    else:
        raise ValidationError("No different question could be written for these topics. Edit the paper's topics or keep this one.")
    with db.tenant(actor.institution_id) as tx:
        qid = questions._insert_question(tx, fresh[0], p["syllabus_id"], actor.user_id,
                                         status="approved" if p["chosen_set"] == set_no else "draft")
        tx.exec("""UPDATE exam_paper_items SET question_id=%s WHERE paper_id=%s AND set_no=%s AND section_no=%s AND item_no=%s""",
                [qid, paper_id, set_no, section_no, item_no])
        tx.exec("UPDATE exam_papers SET updated_at=%s WHERE id=%s", [clock.now(), paper_id])
        audit.record(tx, "exam.replace", actor_id=actor.user_id, target=f"exam:{paper_id}",
                     detail={"set": set_no, "section": section_no, "item": item_no})
    return qid


def list_papers(tx: Tx, actor: Actor) -> list[dict[str, Any]]:
    require(actor, "teacher")
    rows = tx.all("""SELECT p.id, p.title, p.pattern, p.difficulty, p.chosen_set, p.created_at, c.label AS class_label
                     FROM exam_papers p JOIN classes c ON c.id=p.class_id WHERE p.teacher_id=%s ORDER BY p.created_at DESC LIMIT 50""",
                  [actor.user_id])
    for r in rows:
        r["total"] = total_marks(r["pattern"])
        r["questions"] = sum(s["count"] for s in r["pattern"])
    return rows
