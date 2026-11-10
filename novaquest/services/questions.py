"""AI assignment generation, teacher review of every question, and the question bank (AS-1, AS-2, AS-4..AS-6)."""
from __future__ import annotations

import dataclasses
import time
from typing import Any

from novaquest.ai.types import QuestionDraft
from novaquest.core import audit
from novaquest.core.db import Db, Tx, jsonb
from novaquest.core.errors import Conflict, NotFound, ValidationError
from novaquest.services import maths, syllabus
from novaquest.services.access import Actor, require, require_class
from novaquest.services.context import topic_ctx

KINDS = {"mission": "AI-guided mission", "quiz": "Quiz", "short": "Short answer", "viva": "Understanding check (viva)",
         "numeric": "Number problems (maths, science)", "learn": "Learn a topic", "teach": "Teach NOVA",
         "escape": "Escape room"}
_QKIND = {"mission": "mission", "quiz": "mcq", "short": "short", "viva": "viva", "numeric": "numeric", "learn": "learn",
          "teach": "teach", "escape": "escape"}
DIFFICULTIES = ("easy", "medium", "hard")
SELF_MARKED = ("learn", "teach", "escape")  # done by doing (a lesson, a teaching session, an escape room), not by answering


def _insert_question(tx: Tx, d: QuestionDraft, syllabus_id: int, actor_id: int, origin: str = "ai",
                     status: str = "draft") -> int:
    # a question belongs to its topic's book (for a book in parts, the part the topic is in)
    return tx.val("""INSERT INTO questions(institution_id, syllabus_id, topic_id, kind, prompt, scenario, options, answer, rubric,
            difficulty, thinking_level, outcome, ai_concept, misconception_codes, citation, status, origin, created_by, written_by)
            VALUES (%s,coalesce((SELECT syllabus_id FROM syllabus_nodes WHERE id=%s),%s),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id""",
                  [tx.inst, d.topic_id, syllabus_id, d.topic_id, d.kind, d.prompt, d.scenario, jsonb(d.options), jsonb(d.answer),
                   jsonb(d.rubric), d.difficulty, d.thinking_level, d.outcome, d.ai_concept, d.misconception_codes,
                   jsonb(d.citation), status, origin, actor_id, getattr(d, "written_by", None)])


def generate_assignment(db: Db, engine: Any, actor: Actor, *, class_id: int, topic_ids: list[int], kind: str,
                        difficulty: str, count: int, title: str, seed: int | None = None) -> int:
    """Create a draft assignment with AI-generated questions for teacher review (AS-1, AS-2)."""
    require(actor, "teacher")
    if kind not in KINDS:
        raise ValidationError("Choose an assignment type.", field="kind")
    if difficulty not in DIFFICULTIES:
        raise ValidationError("Choose Easy, Medium or Hard.", field="difficulty")
    if not topic_ids:
        raise ValidationError("Choose at least one topic.", field="topics")
    if kind in ("learn", "teach"):
        count = len(topic_ids)
    if kind == "escape":
        return _escape_assignment(db, actor, class_id=class_id, topic_ids=topic_ids, difficulty=difficulty, title=title)
    if not 1 <= count <= 20:
        raise ValidationError("Choose between 1 and 20 questions.", field="count")
    with db.tenant(actor.institution_id) as tx:
        require_class(tx, actor, class_id, write=True)
        sid = one_subject(tx, class_id, [syllabus.topic_syllabus_id(tx, t) for t in topic_ids])
        ctxs = [topic_ctx(tx, t) for t in topic_ids]
        if any(c.id not in topic_ids for c in ctxs):
            raise NotFound("A chosen topic was not found.")
    started = time.monotonic()
    drafts = engine.generate_questions(ctxs, _QKIND[kind], difficulty, count,
                                       seed=seed if seed is not None else class_id * 31 + len(topic_ids))
    if not drafts:
        raise ValidationError("Couldn't make questions for these topics. Try other topics or another type.")
    with db.tenant(actor.institution_id) as tx:
        aid = tx.val("""INSERT INTO assignments(institution_id, class_id, teacher_id, syllabus_id, title, kind, difficulty, topic_ids)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                     [tx.inst, class_id, actor.user_id, sid, title.strip() or _default_title(ctxs, kind), kind, difficulty, topic_ids])
        for i, d in enumerate(drafts, start=1):
            qid = _insert_question(tx, d, sid, actor.user_id)
            tx.exec("INSERT INTO assignment_questions(assignment_id, question_id, institution_id, ord) VALUES (%s,%s,%s,%s)",
                    [aid, qid, tx.inst, i])
        audit.record(tx, "assignment.generate", actor_id=actor.user_id, target=f"assignment:{aid}",
                     detail={"kind": kind, "questions": len(drafts), "engine": getattr(engine, "name", "?"),
                             "seconds": round(time.monotonic() - started, 2)})
    return aid


def one_subject(tx: Tx, class_id: int, sids: list[int]) -> int:
    """The book for work on these topics' books: they must be one of the class's subjects (any parts of one book in
    parts may be mixed). Returns the first book (its first part)."""
    subject = next((s for s in syllabus.class_subjects(tx, class_id) if sids[0] in s["ids"]), None)
    if subject is None:
        raise ValidationError("That book isn't one of this class's subjects.", field="topics")
    if any(s not in subject["ids"] for s in sids):
        raise ValidationError("Choose topics from one book (subject) at a time.", field="topics")
    return min(sids, key=subject["ids"].index)


def _escape_assignment(db: Db, actor: Actor, *, class_id: int, topic_ids: list[int], difficulty: str, title: str) -> int:
    """An escape room set as work (1.28.0): the room of the chapter the topics are in, as one item that's done when the
    student escapes. Nothing for the teacher to check, so the item is approved straight away."""
    from novaquest.services import escape

    with db.tenant(actor.institution_id) as tx:
        require_class(tx, actor, class_id, write=True)
        chapters = {tx.val("SELECT parent_id FROM syllabus_nodes WHERE id=%s AND kind='topic'", [t]) for t in topic_ids}
        if len(chapters) != 1 or None in chapters:
            raise ValidationError("For an escape room, choose topics from one chapter.", field="topics")
        sid = one_subject(tx, class_id, [syllabus.topic_syllabus_id(tx, t) for t in topic_ids])
        ch = tx.one("SELECT id, code, title, syllabus_id FROM syllabus_nodes WHERE id=%s", [chapters.pop()])
        book = syllabus.get_syllabus(tx, ch["syllabus_id"])
        no = int(ch["code"]) if str(ch["code"]).isdigit() else None
        room = escape.book_rooms(tx, book).get(no) if no is not None else None
        if not room:
            raise ValidationError("This chapter has no approved escape room yet. Your school admin drafts and approves "
                                  "rooms on the book's page under Books.", field="topics")
        topics = [r["id"] for r in tx.all("SELECT id FROM syllabus_nodes WHERE parent_id=%s AND kind='topic' ORDER BY ord", [ch["id"]])]
        d = QuestionDraft(topic_id=topics[0], kind="escape", prompt=f"Escape room: {room['title']}", difficulty=difficulty,
                          thinking_level="apply", outcome=f"Open the four locks of Chapter {ch['code']}'s room and escape",
                          answer={"chapter": no, "syllabus_id": book["id"]}, ai_concept="Escape room")
        part = f"Part {book['part']} · " if book.get("part") else ""
        aid = tx.val("""INSERT INTO assignments(institution_id, class_id, teacher_id, syllabus_id, title, kind, difficulty, topic_ids)
                        VALUES (%s,%s,%s,%s,%s,'escape',%s,%s) RETURNING id""",
                     [tx.inst, class_id, actor.user_id, sid,
                      (title.strip() or f"Escape room: {part}Ch {ch['code']} {room['title']}")[:120], difficulty, topics])
        qid = _insert_question(tx, d, book["id"], actor.user_id, status="approved")
        tx.exec("INSERT INTO assignment_questions(assignment_id, question_id, institution_id, ord) VALUES (%s,%s,%s,1)",
                [aid, qid, tx.inst])
        audit.record(tx, "assignment.escape", actor_id=actor.user_id, target=f"assignment:{aid}", detail={"chapter": no})
    return aid


def _default_title(ctxs: list, kind: str) -> str:
    name = ctxs[0].title if len(ctxs) == 1 else ctxs[0].chapter_title
    return f"{KINDS[kind]}: {name}"


def assignment_questions(tx: Tx, assignment_id: int) -> list[dict]:
    """Questions of an assignment in order, with their tags, sources and review status (AS-4)."""
    return tx.all("""SELECT q.id, aq.ord, q.kind, q.prompt, q.scenario, q.options, q.answer, q.rubric, q.difficulty,
        q.thinking_level, q.outcome, q.ai_concept, q.misconception_codes, q.citation, q.status, q.origin, q.topic_id,
        q.written_by, (CASE WHEN sb.part IS NULL THEN t.code ELSE 'P' || sb.part || '·' || t.code END) AS topic_code, t.title AS topic_title
        FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id JOIN syllabus_nodes t ON t.id=q.topic_id
        JOIN syllabi sb ON sb.id=t.syllabus_id
        WHERE aq.assignment_id=%s AND q.status <> 'removed' ORDER BY aq.ord""", [assignment_id])


def _owned_question(tx: Tx, actor: Actor, question_id: int) -> dict:
    row = tx.one("""SELECT q.*, a.id AS assignment_id, a.state, a.class_id FROM questions q
        JOIN assignment_questions aq ON aq.question_id=q.id JOIN assignments a ON a.id=aq.assignment_id
        WHERE q.id=%s""", [question_id])
    if not row:
        raise NotFound("That question was not found.")
    require_class(tx, actor, row["class_id"], write=True)
    if row["state"] != "draft":
        raise Conflict("This assignment has been scheduled, so its questions can no longer change.")
    return row


def approve(tx: Tx, actor: Actor, question_id: int) -> None:
    """Approve a question so it can be published and reused from the bank (AS-5, AS-6)."""
    require(actor, "teacher")
    _owned_question(tx, actor, question_id)
    tx.exec("UPDATE questions SET status='approved' WHERE id=%s", [question_id])
    audit.record(tx, "question.approve", actor_id=actor.user_id, target=f"question:{question_id}")


def edit(tx: Tx, actor: Actor, question_id: int, *, prompt: str, options: list[str] | None, answer_index: int | None,
         model_answer: str | None, rubric_points: list[str] | None, number: dict[str, Any] | None = None) -> None:
    """Teacher edits a question; an edited question counts as approved by that teacher (AS-5)."""
    require(actor, "teacher")
    q = _owned_question(tx, actor, question_id)
    if len(prompt.strip()) < 5:
        raise ValidationError("The question is too short.", field="prompt")
    answer, rubric = q["answer"], q["rubric"]
    if q["kind"] in ("mcq", "ai_moment"):
        opts = [o.strip() for o in (options or []) if o.strip()]
        if len(opts) < 2 or answer_index is None or not 0 <= answer_index < len(opts):
            raise ValidationError("Give at least two options and mark the correct one.", field="options")
        options, answer = opts, {"index": answer_index, "text": opts[answer_index]}
    elif q["kind"] in ("short", "viva"):
        points = [p.strip() for p in (rubric_points or []) if p.strip()]
        if not points:
            raise ValidationError("Add at least one marking point.", field="rubric")
        old = {r["point"]: r.get("any", []) for r in (rubric or [])}
        rubric = [{"point": p, "any": old.get(p) or [w for w in p.split() if len(w) > 4][:3]} for p in points]
        answer = {"model": (model_answer or "").strip() or (answer or {}).get("model", "")}
        options = None
    elif q["kind"] == "numeric":
        prompt, answer = maths.build_key(prompt=prompt, **(number or {}))
        options, rubric = None, None
    tx.exec("UPDATE questions SET prompt=%s, options=%s, answer=%s, rubric=%s, status='approved', origin='teacher' WHERE id=%s",
            [prompt.strip(), jsonb(options), jsonb(answer), jsonb(rubric), question_id])
    audit.record(tx, "question.edit", actor_id=actor.user_id, target=f"question:{question_id}")


def add_own(tx: Tx, actor: Actor, assignment_id: int, *, topic_id: int, kind: str, prompt: str,
            options: list[str] | None = None, answer_index: int | None = None, model_answer: str = "",
            rubric_points: list[str] | None = None, value: str = "", unit: str = "", tolerance: str = "", formula: str = "",
            vars_text: str = "", steps: list[str] | None = None, worked: str = "") -> int:
    """A question the teacher writes themselves, added to a draft assignment (approved: it's theirs). Multiple choice,
    short answer or a number problem, on one of the assignment's topics. Returns its id."""
    require(actor, "teacher")
    a = tx.one("SELECT id, class_id, state, syllabus_id, difficulty, topic_ids, kind FROM assignments WHERE id=%s", [assignment_id])
    if not a:
        raise NotFound("That assignment was not found.")
    require_class(tx, actor, a["class_id"], write=True)
    if a["state"] != "draft":
        raise Conflict("This assignment has been scheduled, so its questions can no longer change.")
    if a["kind"] in SELF_MARKED:
        raise Conflict("Learn and Teach NOVA tasks don't have written questions.")
    if topic_id not in a["topic_ids"]:
        raise ValidationError("Choose one of this assignment's topics.", field="topic_id")
    if kind not in ("mcq", "short", "numeric"):
        raise ValidationError("Choose multiple choice, short answer or a number problem.", field="kind")
    prompt = prompt.strip()
    if len(prompt) < 5:
        raise ValidationError("Write the question.", field="prompt")
    rubric, answer = None, None
    if kind == "mcq":
        opts = [o.strip() for o in (options or []) if o.strip()]
        if len(opts) < 2 or len(set(opts)) != len(opts) or answer_index is None or not 0 <= answer_index < len(opts):
            raise ValidationError("Give at least two different options and mark the correct one.", field="options")
        options, answer = opts, {"index": answer_index, "text": opts[answer_index]}
    elif kind == "numeric":
        prompt, answer = maths.build_key(prompt=prompt, value=value, unit=unit, tolerance=tolerance, formula=formula,
                                         vars_text=vars_text, steps=steps or [], worked=worked)
        options = None
    else:
        points = [p.strip() for p in (rubric_points or []) if p.strip()]
        if not points or not model_answer.strip():
            raise ValidationError("Give a model answer and at least one marking point.", field="rubric")
        rubric = [{"point": p, "any": [w.strip(".,;:").lower() for w in p.split() if len(w) > 4][:3]} for p in points]
        options, answer = None, {"model": model_answer.strip()}
    topic = tx.one("SELECT title, syllabus_id, meta->>'outcome' AS outcome FROM syllabus_nodes WHERE id=%s", [topic_id])
    qid = tx.val("""INSERT INTO questions(institution_id, syllabus_id, topic_id, kind, prompt, options, answer, rubric,
                    difficulty, thinking_level, outcome, status, origin, created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'apply',%s,'approved','teacher',%s) RETURNING id""",
                 [tx.inst, (topic or {}).get("syllabus_id") or a["syllabus_id"], topic_id, kind, prompt[:1000], jsonb(options), jsonb(answer), jsonb(rubric),
                  a["difficulty"] if a["difficulty"] in DIFFICULTIES else "medium",
                  (topic or {}).get("outcome") or f"Understand {(topic or {}).get('title', 'the topic')}", actor.user_id])
    ord_ = tx.val("SELECT coalesce(max(ord), 0) + 1 FROM assignment_questions WHERE assignment_id=%s", [assignment_id])
    tx.exec("INSERT INTO assignment_questions(assignment_id, question_id, institution_id, ord) VALUES (%s,%s,%s,%s)",
            [assignment_id, qid, tx.inst, ord_])
    audit.record(tx, "question.add_own", actor_id=actor.user_id, target=f"question:{qid}", detail={"assignment": assignment_id})
    return qid


def remove(tx: Tx, actor: Actor, question_id: int) -> None:
    """Remove a question from the draft (AS-5)."""
    require(actor, "teacher")
    _owned_question(tx, actor, question_id)
    tx.exec("UPDATE questions SET status='removed' WHERE id=%s", [question_id])
    audit.record(tx, "question.remove", actor_id=actor.user_id, target=f"question:{question_id}")


def regenerate(db: Db, engine: Any, actor: Actor, question_id: int) -> int:
    """Replace one question with a fresh AI draft on the same topic (AS-5). Returns the new id."""
    require(actor, "teacher")
    with db.tenant(actor.institution_id) as tx:
        q = _owned_question(tx, actor, question_id)
        ctx = topic_ctx(tx, q["topic_id"])
        ord_ = tx.val("SELECT ord FROM assignment_questions WHERE question_id=%s", [question_id])
        existing = {(r["prompt"], str((r["answer"] or {}).get("text") or (r["answer"] or {}).get("model")))
                    for r in assignment_questions(tx, q["assignment_id"])}
    # same wording can ask about different facts ("Which statement … is correct?"), so compare the answer too
    drafts = engine.generate_questions([ctx], q["kind"], q["difficulty"], 8, seed=question_id + 7)
    fresh = next((d for d in drafts if (d.prompt, str((d.answer or {}).get("text") or (d.answer or {}).get("model")))
                  not in existing), None)
    if not fresh:
        raise ValidationError("Couldn't make a different question for this topic. Edit this one instead.")
    with db.tenant(actor.institution_id) as tx:
        new_id = _insert_question(tx, fresh, q["syllabus_id"], actor.user_id)
        tx.exec("UPDATE questions SET status='removed' WHERE id=%s", [question_id])
        tx.exec("INSERT INTO assignment_questions(assignment_id, question_id, institution_id, ord) VALUES (%s,%s,%s,%s)",
                [q["assignment_id"], new_id, tx.inst, ord_])
        audit.record(tx, "question.regenerate", actor_id=actor.user_id, target=f"question:{question_id}", detail={"new": new_id})
    return new_id


def rewrite_basic(db: Db, engine: Any, actor: Actor, assignment_id: int) -> tuple[int, str]:
    """Ask Claude again for every 'Basic' question of a draft (ones the offline writer wrote because Claude couldn't),
    in one request, and put each new question in the old one's place. Returns (how many rewritten, why not if 0)."""
    require(actor, "teacher")
    with db.tenant(actor.institution_id) as tx:
        a = tx.one("SELECT id, class_id, kind, difficulty, syllabus_id, state FROM assignments WHERE id=%s", [assignment_id])
        if not a:
            raise NotFound("That assignment was not found.")
        require_class(tx, actor, a["class_id"], write=True)
        if a["state"] != "draft":
            raise Conflict("This assignment is already scheduled; its questions can't change now.")
        basic = [q for q in assignment_questions(tx, assignment_id) if q["written_by"] == "offline" and q["status"] == "draft"]
        if not basic:
            return 0, ""
        topics = list(dict.fromkeys(q["topic_id"] for q in basic))
        ctxs = [topic_ctx(tx, t) for t in topics]
        ords = {q["id"]: tx.val("SELECT ord FROM assignment_questions WHERE question_id=%s", [q["id"]]) for q in basic}
    if getattr(engine, "name", "") != "claude":
        return 0, "Claude isn't set up on this NovaQuest (no API key), or today's AI limit was reached."
    drafts = [d for d in engine.generate_questions(ctxs, _QKIND[a["kind"]], a["difficulty"], len(basic),
                                                   seed=assignment_id) if d.written_by == "claude"]
    if not drafts:
        return 0, getattr(engine, "last_error", "") or "Claude didn't reply with usable questions."
    with db.tenant(actor.institution_id) as tx:
        for old, new in zip(basic, drafts):
            new_id = _insert_question(tx, new, a["syllabus_id"], actor.user_id)
            tx.exec("UPDATE questions SET status='removed' WHERE id=%s", [old["id"]])
            tx.exec("INSERT INTO assignment_questions(assignment_id, question_id, institution_id, ord) VALUES (%s,%s,%s,%s)",
                    [assignment_id, new_id, tx.inst, ords[old["id"]]])
        audit.record(tx, "question.rewrite_basic", actor_id=actor.user_id, target=f"assignment:{assignment_id}",
                     detail={"rewritten": min(len(basic), len(drafts)), "basic": len(basic)})
    return min(len(basic), len(drafts)), ""


def bank(tx: Tx, *, topic_ids: list[int] | None = None, kind: str | None = None, limit: int = 100) -> list[dict]:
    """Approved questions in this institution's bank (AS-6)."""
    return tx.all("""SELECT DISTINCT ON (q.prompt, q.answer::text) q.id, q.kind, q.prompt, q.difficulty, q.thinking_level, q.topic_id,
            (CASE WHEN sb.part IS NULL THEN t.code ELSE 'P' || sb.part || '·' || t.code END) AS topic_code, t.title AS topic_title, q.created_at
        FROM questions q JOIN syllabus_nodes t ON t.id=q.topic_id JOIN syllabi sb ON sb.id=t.syllabus_id
        WHERE q.status='approved' AND q.kind NOT IN ('learn', 'teach', 'escape')
          AND (%s::bigint[] IS NULL OR q.topic_id = ANY(%s::bigint[])) AND (%s::text IS NULL OR q.kind=%s)
        ORDER BY q.prompt, q.answer::text, q.created_at DESC LIMIT %s""", [topic_ids, topic_ids, kind, kind, limit])


def create_from_bank(tx: Tx, actor: Actor, *, class_id: int, question_ids: list[int], title: str, difficulty: str = "medium") -> int:
    """Build a draft assignment from approved bank questions, with no AI call (AS-6)."""
    require(actor, "teacher")
    require_class(tx, actor, class_id, write=True)
    rows = tx.all("SELECT * FROM questions WHERE id = ANY(%s) AND status='approved'", [question_ids])
    if not rows:
        raise ValidationError("Choose at least one question from the bank.")
    by_id = {r["id"]: r for r in rows}
    ordered = [by_id[i] for i in question_ids if i in by_id]
    kinds = {r["kind"] for r in ordered}
    kind = "quiz" if kinds <= {"mcq", "ai_moment"} else ("viva" if kinds == {"viva"} else "short" if kinds == {"short"} else "mission")
    sid = ordered[0]["syllabus_id"]
    topics = list(dict.fromkeys(r["topic_id"] for r in ordered))
    aid = tx.val("""INSERT INTO assignments(institution_id, class_id, teacher_id, syllabus_id, title, kind, difficulty, topic_ids)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                 [tx.inst, class_id, actor.user_id, sid, title.strip() or "Practice from the question bank", kind, difficulty, topics])
    for i, r in enumerate(ordered, start=1):
        d = QuestionDraft(**{f.name: r.get(f.name) for f in dataclasses.fields(QuestionDraft) if f.name in r})
        d.misconception_codes = list(r["misconception_codes"] or [])
        d.citation = r["citation"] or []
        qid = _insert_question(tx, d, sid, actor.user_id, origin="bank", status="approved")
        tx.exec("INSERT INTO assignment_questions(assignment_id, question_id, institution_id, ord) VALUES (%s,%s,%s,%s)",
                [aid, qid, tx.inst, i])
    audit.record(tx, "assignment.from_bank", actor_id=actor.user_id, target=f"assignment:{aid}", detail={"questions": len(ordered)})
    return aid
