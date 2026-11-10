"""Learn mode: guided lessons with adaptive checks, ask-the-tutor, practice missions, understanding
checks, teacher topic controls and conversation logs (LN-1..LN-9)."""
from __future__ import annotations

import dataclasses
import re
from datetime import timedelta
from typing import Any

from novaquest.ai import guard
from novaquest.ai.offline import OfflineEngine, _sentences
from novaquest.ai.types import TutorReply
from novaquest.core import audit, clock
from novaquest.core.db import Db, Tx, jsonb
from novaquest.core.errors import Forbidden, NotFound, ValidationError
from novaquest.services import language, mastery, search, syllabus
from novaquest.services.access import Actor, require, require_class, student_class_id
from novaquest.services.context import topic_ctx

_OFFLINE = OfflineEngine()


MODES = {"open": "Every chapter open", "closed": "Closed until I open chapters",
         "plan": "Open each chapter on its term-plan start date"}

# A topic is closed for self-study when the teacher locked it by hand, or (when they haven't opened it by hand) when
# the class's book starts closed, or opens with the term plan and the topic's chapter hasn't started yet (1.28.0).
_CLOSED = """SELECT n.id FROM syllabus_nodes n
    JOIN class_subjects cs ON cs.syllabus_id=n.syllabus_id AND cs.class_id=%(c)s
    LEFT JOIN topic_settings ts ON ts.class_id=%(c)s AND ts.node_id=n.id
    LEFT JOIN class_self_study m ON m.class_id=%(c)s AND m.syllabus_id=n.syllabus_id
    LEFT JOIN term_plans p ON p.class_id=%(c)s AND p.chapter_id=n.parent_id
    WHERE n.kind='topic' AND (%(t)s::bigint IS NULL OR n.id=%(t)s) AND (ts.open_for_self_study = false
      OR (ts.open_for_self_study IS NULL AND (m.mode='closed' OR (m.mode='plan' AND (p.starts_on IS NULL OR p.starts_on > %(d)s)))))"""


def _today():
    return clock.to_ist(clock.now()).date()


def closed_topics(tx: Tx, class_id: int | None) -> set[int]:
    """Every topic of the class's books that's closed for self-study (one query)."""
    if class_id is None:
        return set()
    return {r["id"] for r in tx.all(_CLOSED, {"c": class_id, "t": None, "d": _today()})}


def is_open(tx: Tx, class_id: int | None, topic_id: int) -> bool:
    """Topics are open for self-study unless the teacher locked them, or the class's book starts closed or opens
    chapter by chapter with the term plan (LN-7)."""
    if class_id is None:
        return False
    return not tx.val(_CLOSED, {"c": class_id, "t": topic_id, "d": _today()})


def self_study_mode(tx: Tx, class_id: int, syllabus_id: int) -> str:
    return tx.val("SELECT mode FROM class_self_study WHERE class_id=%s AND syllabus_id=%s", [class_id, syllabus_id]) or "open"


def set_self_study_mode(tx: Tx, actor: Actor, class_id: int, syllabus_ids: list[int], mode: str) -> None:
    """How the class's book (all its parts) starts for self-study. Topics opened or locked by hand stay as they are."""
    require(actor, "teacher")
    require_class(tx, actor, class_id, write=True)
    if mode not in MODES:
        raise ValidationError("Choose how chapters open.", field="mode")
    for sid in syllabus_ids:
        tx.exec("""INSERT INTO class_self_study(institution_id, class_id, syllabus_id, mode, updated_by, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (class_id, syllabus_id) DO UPDATE SET mode=EXCLUDED.mode,
                   updated_by=EXCLUDED.updated_by, updated_at=EXCLUDED.updated_at""",
                [tx.inst, class_id, sid, mode, actor.user_id, clock.now()])
    audit.record(tx, "self_study.mode", actor_id=actor.user_id, target=f"class:{class_id}", detail={"mode": mode, "books": syllabus_ids})


def set_chapters_open(tx: Tx, actor: Actor, class_id: int, chapter_ids: list[int], is_open_: bool) -> int:
    """Open or lock whole chapters for the class (every topic in them). Returns the number of topics changed."""
    require(actor, "teacher")
    require_class(tx, actor, class_id, write=True)
    books = [b["id"] for b in syllabus.class_syllabi(tx, class_id)]
    topics = tx.all("""SELECT t.id FROM syllabus_nodes t JOIN syllabus_nodes c ON c.id=t.parent_id
                       WHERE c.id = ANY(%s) AND c.kind='chapter' AND t.kind='topic' AND t.syllabus_id = ANY(%s)""",
                    [chapter_ids, books])
    for r in topics:
        tx.exec("""INSERT INTO topic_settings(institution_id, class_id, node_id, open_for_self_study) VALUES (%s,%s,%s,%s)
                   ON CONFLICT (class_id, node_id) DO UPDATE SET open_for_self_study=EXCLUDED.open_for_self_study""",
                [tx.inst, class_id, r["id"], is_open_])
    audit.record(tx, "chapter.open" if is_open_ else "chapter.lock", actor_id=actor.user_id, target=f"class:{class_id}",
                 detail={"chapters": chapter_ids})
    return len(topics)


def open_only(tx: Tx, actor: Actor, class_id: int, subject_ids: list[int], chapter_ids: list[int]) -> None:
    """Open exactly these chapters of the subject for the class; lock the rest."""
    require(actor, "teacher")
    all_ch = [r["id"] for r in tx.all("SELECT id FROM syllabus_nodes WHERE kind='chapter' AND syllabus_id = ANY(%s)", [subject_ids])]
    keep = [c for c in chapter_ids if c in all_ch]
    set_chapters_open(tx, actor, class_id, keep, True)
    set_chapters_open(tx, actor, class_id, [c for c in all_ch if c not in keep], False)


def _require_open(tx: Tx, actor: Actor, topic_id: int) -> int:
    class_id = student_class_id(tx, actor.user_id)
    if not is_open(tx, class_id, topic_id) and not _assigned_topic(tx, actor.user_id, topic_id):
        raise NotFound("That topic isn't open yet. Your teacher will open it soon.")
    return class_id


def _assigned_topic(tx: Tx, student_id: int, topic_id: int) -> bool:
    return bool(tx.val("""SELECT 1 FROM attempts t JOIN assignments a ON a.id=t.assignment_id
                          WHERE t.student_id=%s AND t.status='in_progress' AND %s = ANY(a.topic_ids)""", [student_id, topic_id]))


def topics_for_student(tx: Tx, actor: Actor, syllabus_id: int | None = None, *, all_books: bool = False) -> list[dict]:
    """The syllabus as the student sees it: only open topics, with mastery (LN-5, LN-7). One of the class's books
    (the chosen one, else the first), or every book with all_books=True (each chapter carries its book)."""
    require(actor, "student")
    class_id = student_class_id(tx, actor.user_id)
    books = syllabus.class_subjects(tx, class_id)
    if not all_books:
        one = syllabus.pick(books, syllabus_id)
        books = [one] if one else []
    locked = closed_topics(tx, class_id)
    levels = mastery.for_student(tx, actor.user_id)
    out = []
    for book in books:  # a subject: one book, or a book's parts in order (chapters then carry their part)
        for ch in syllabus.subject_tree(tx, book):
            topics = [{"id": t["id"], "code": t["code"], "title": t["title"], "syllabus_id": t["syllabus_id"],
                       "part": t.get("part"),
                       "level": levels.get(t["id"], {}).get("level", 0), "taught": levels.get(t["id"], {}).get("taught_ok", False)}
                      for t in ch["topics"] if t["id"] not in locked]
            if topics:
                out.append({"id": ch["id"], "no": ch["code"], "title": ch["title"], "topics": topics, "part": ch.get("part"),
                            "syllabus_id": ch["syllabus_id"], "demo": ch["meta"].get("demo", False), "book_id": book["id"], "book": book["label"]})
    return out


def student_books(tx: Tx, actor: Actor) -> list[dict]:
    """The books (subjects) the student's class studies."""
    require(actor, "student")
    return syllabus.student_subjects(tx, actor.user_id)


def _checks_for(tx: Tx, topic_id: int) -> tuple[dict, list[dict]]:
    """Lesson content and its checks (answers kept server-side)."""
    stored = tx.val("SELECT data FROM lessons WHERE node_id=%s AND status='approved'", [topic_id])
    if stored:
        return stored, stored["checks"]
    ctx = topic_ctx(tx, topic_id)
    sents = [s for p in ctx.passages[:2] for s in _sentences(p["body"])]
    explain = " ".join(sents[:4]) or (ctx.passages[0]["body"][:600] if ctx.passages else ctx.outcome)
    explain_again = " ".join(sents[4:7]) or ctx.outcome
    drafts = _OFFLINE.generate_questions([ctx], "mcq", "easy", 2, seed=topic_id)
    checks = [{"q": d.prompt, "options": d.options, "answer": d.answer["index"],
               "why": f"From {d.citation[0]['ref']}: {d.citation[0]['quote']}" if d.citation else ""} for d in drafts]
    return {"explain": explain, "explain_again": explain_again, "example": None, "checks": checks, "from_textbook": True}, checks


def lesson(tx: Tx, actor: Actor, topic_id: int) -> dict[str, Any]:
    """A guided lesson: explanation with its source, a worked example and quick checks (LN-1)."""
    require(actor, "student")
    _require_open(tx, actor, topic_id)
    topic = syllabus.get_topic(tx, topic_id)
    data, checks = _checks_for(tx, topic_id)
    refs = [p["ref"] for p in syllabus.topic_passages(tx, topic_id, 2)]
    if not tx.val("SELECT 1 FROM learn_events WHERE student_id=%s AND topic_id=%s AND kind='lesson_started'", [actor.user_id, topic_id]):
        mastery.record(tx, actor.user_id, topic_id, "lesson_started", None)
    return {"topic": topic, "explain": data["explain"], "example": data.get("example"), "refs": refs,
            "from_textbook": data.get("from_textbook", False),
            "checks": [{"q": c["q"], "options": c["options"]} for c in checks]}


def answer_check(tx: Tx, actor: Actor, topic_id: int, index: int, choice: int) -> dict[str, Any]:
    """Mark a lesson check. A wrong answer brings a different explanation and another try (LN-1)."""
    require(actor, "student")
    _require_open(tx, actor, topic_id)
    data, checks = _checks_for(tx, topic_id)
    if not 0 <= index < len(checks):
        raise NotFound("That check was not found.")
    c = checks[index]
    ok = int(choice) == int(c["answer"])
    mastery.record(tx, actor.user_id, topic_id, "check", 1.0 if ok else 0.0, {"check": index})
    return {"correct": ok, "why": c.get("why", ""), "explain_again": None if ok else data.get("explain_again"),
            "answer": c["options"][c["answer"]] if ok else None}


def ask(db: Db, engine: Any, actor: Actor, question: str, topic_id: int | None = None) -> TutorReply:
    """Ask the tutor anything on the syllabus; grounded, cited, and off-syllabus questions declined (LN-2)."""
    require(actor, "student")
    question = question.strip()
    if not question:
        raise ValidationError("Type or say your question.")
    with db.tenant(actor.institution_id) as tx:
        lang = language.student_lang(tx, actor.user_id)
    asked = question
    question = language.to_english(engine, question)  # asked in their own language: search the book in English
    check = guard.screen_input(question)
    with db.tenant(actor.institution_id) as tx:
        title = None
        if topic_id:
            _require_open(tx, actor, topic_id)
            title = syllabus.get_topic(tx, topic_id)["title"]
        if topic_id:
            books = [syllabus.topic_syllabus_id(tx, topic_id)]
        else:
            books = [b["id"] for b in syllabus.student_syllabi(tx, actor.user_id)]
        res = None
        if topic_id:  # asked from a topic: prefer that chapter's pages, else search the whole book
            chapter = tx.val("SELECT parent_id FROM syllabus_nodes WHERE id=%s", [topic_id])
            near = [r["id"] for r in tx.all("SELECT id FROM syllabus_nodes WHERE parent_id=%s AND kind='topic'", [chapter])]
            res = search.search(tx, books[0], question, topic_ids=near, limit=4) if near else None
            res = res if res and res.on_syllabus else None
        if res is None:  # every book the class studies: the best-covered answer wins
            found = [search.search(tx, sid, question, limit=4) for sid in books]
            found = [r for r in found if r.hits] or found
            res = max(found, key=lambda r: (r.on_syllabus, r.coverage), default=search.SearchResult([], 0.0, False))
        no_textbook = not res.hits and not any(syllabus.passage_count(tx, sid) for sid in books)
        hit_book = tx.val("SELECT syllabus_id FROM syllabus_nodes WHERE id=%s", [res.hits[0].topic_id]) if res.hits else None
        named = [syllabus.get_syllabus(tx, sid) for sid in ([hit_book] if hit_book else books[:1])]
        course = syllabus.course(named[0]) if named else "Grade 8 Science"
    if not check.ok and check.reason != "answer_request":
        reply = TutorReply(check.reply or "", (), True)
    else:
        hits = [dataclasses.asdict(h) for h in res.hits]
        if no_textbook:
            reply = TutorReply("I can't answer questions yet: the textbook hasn't been added to NovaQuest. "
                               "Please tell your teacher.", (), True)
        else:
            reply = engine.answer_question(question, hits, title, res.on_syllabus, course)
    with db.tenant(actor.institution_id) as tx:
        now = clock.now()
        tx.exec("""INSERT INTO tutor_messages(institution_id, student_id, context, topic_id, role, body, created_at)
                   VALUES (%s,%s,'learn',%s,'student',%s,%s)""", [tx.inst, actor.user_id, topic_id, asked[:2000], now])
        tx.exec("""INSERT INTO tutor_messages(institution_id, student_id, context, topic_id, role, body, citation, created_at)
                   VALUES (%s,%s,'learn',%s,'tutor',%s,%s,%s)""",
                [tx.inst, actor.user_id, topic_id, reply.text, jsonb(list(reply.citations)), now + timedelta(microseconds=1)])
    if lang != "en":  # shown in the student's language; kept in English underneath
        reply = dataclasses.replace(reply, text=language.localise(db, engine, actor.institution_id, [reply.text], lang)[0])
    return reply


PRACTICE_SIZE = 5


def _bank_pool(tx: Tx, topic_id: int) -> list[dict]:
    """The school's best questions on a topic for practice: approved multiple-choice questions written by Claude or a
    teacher (never 'Basic' ones), that have been in an assignment whose results are released and are in no assignment
    still waiting for its results, so practice never gives away a live task. Duplicates are dropped."""
    return tx.all("""SELECT DISTINCT ON (q.prompt, q.answer::text) q.id, q.kind, q.prompt, q.scenario, q.options, q.answer, q.rubric
        FROM questions q WHERE q.topic_id=%s AND q.status='approved' AND q.kind IN ('mcq', 'ai_moment')
          AND coalesce(q.written_by, 'claude') <> 'offline'
          AND EXISTS (SELECT 1 FROM assignment_questions aq JOIN assignments a ON a.id=aq.assignment_id
                      WHERE aq.question_id=q.id AND a.released_at IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM assignment_questions aq JOIN assignments a ON a.id=aq.assignment_id
                          WHERE aq.question_id=q.id AND a.released_at IS NULL)
        ORDER BY q.prompt, q.answer::text, q.id""", [topic_id])


def _practice_set(tx: Tx, student_id: int, topic_id: int) -> list[Any]:
    """This round's practice questions: from the school's question bank first (a different mix each round, so a
    student who practises again meets new questions), then the topic's approved quick checks and picture-quiz
    questions, and only then questions made from the textbook."""
    import hashlib
    import random
    from types import SimpleNamespace

    rounds = int(tx.val("""SELECT count(*) FROM learn_events WHERE student_id=%s AND topic_id=%s AND kind='practice'""",
                        [student_id, topic_id]) or 0)
    pool = _bank_pool(tx, topic_id)
    rnd = random.Random(int(hashlib.sha256(f"{student_id}:{topic_id}".encode()).hexdigest()[:8], 16))
    rnd.shuffle(pool)
    if pool:
        start = (rounds * PRACTICE_SIZE) % len(pool)
        pool = (pool[start:] + pool[:start])[:PRACTICE_SIZE]
    picked = [SimpleNamespace(kind=r["kind"], prompt=r["prompt"], scenario=r["scenario"], options=r["options"],
                              answer=r["answer"], rubric=r["rubric"], citation=[], from_bank=True) for r in pool]
    if len(picked) < PRACTICE_SIZE:  # next: the topic's approved quick checks and picture-quiz questions
        seen = {q.prompt for q in picked}
        extra = []
        for sql in ("SELECT data->'checks' AS qs FROM lessons WHERE node_id=%s AND status='approved'",
                    "SELECT data->'quiz' AS qs FROM picture_lessons WHERE node_id=%s AND status='approved'"):
            for row in tx.all(sql, [topic_id]):
                for c in row["qs"] or []:
                    try:
                        opts, ans = [str(o) for o in c["options"]], int(c["answer"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if c.get("q") and len(opts) >= 2 and 0 <= ans < len(opts) and c["q"] not in seen:
                        seen.add(c["q"])
                        extra.append(SimpleNamespace(kind="mcq", prompt=str(c["q"]), scenario=None, options=opts,
                                                     answer={"index": ans, "text": opts[ans]}, rubric=None, citation=[],
                                                     from_bank=True))
        rnd.shuffle(extra)
        picked += extra[:PRACTICE_SIZE - len(picked)]
    if len(picked) < PRACTICE_SIZE:
        seen = {(q.prompt, str(q.answer)) for q in picked}
        extra = [q for q in _OFFLINE.generate_questions([topic_ctx(tx, topic_id)], "mission", "medium", 2 * PRACTICE_SIZE,
                                                        seed=topic_id) if (q.prompt, str(q.answer)) not in seen]
        picked += extra[:PRACTICE_SIZE - len(picked)]
    return _as_mission(picked)


_STAGE = re.compile(r"^Mission stage \d+ of \d+\.\s*")
_STAGE_LINES = ["The NovaQuest lab robot is stuck. Help it with this step.",
                "The first door opens. The next one has a lock that asks this.",
                "The robot's sensor flickers: it isn't sure. Help it decide.", "A screen lights up with a new puzzle.",
                "Almost out! One more door stands between the robot and the exit.",
                "Final door: answer this to free the robot."]


def _as_mission(qs: list[Any]) -> list[Any]:
    """Number the practice questions as one mission ('Mission stage 2 of 5. …'), whatever they came from."""
    n = len(qs)
    for i, q in enumerate(qs, start=1):
        own = _STAGE.sub("", q.scenario or "")
        line = _STAGE_LINES[0] if i == 1 else (_STAGE_LINES[-1] if i == n else _STAGE_LINES[1 + (i - 2) % (len(_STAGE_LINES) - 2)])
        q.scenario = f"Mission stage {i} of {n}. " + (own or line)
    return qs


def practice(tx: Tx, actor: Actor, topic_id: int) -> dict[str, Any]:
    """A short game-style practice mission for a topic: the school's approved questions first, then questions made
    from the textbook (LN-3). No AI call."""
    require(actor, "student")
    _require_open(tx, actor, topic_id)
    qs = _practice_set(tx, actor.user_id, topic_id)
    return {"topic": syllabus.get_topic(tx, topic_id),
            "questions": [{"i": i, "kind": q.kind, "prompt": q.prompt, "scenario": q.scenario, "options": q.options}
                          for i, q in enumerate(qs)]}


def practice_submit(tx: Tx, actor: Actor, topic_id: int, answers: dict[int, Any]) -> dict[str, Any]:
    """Mark a practice mission and record it as practice evidence (LN-3)."""
    require(actor, "student")
    _require_open(tx, actor, topic_id)
    ctx = topic_ctx(tx, topic_id)
    qs = _practice_set(tx, actor.user_id, topic_id)
    results, got, mx = [], 0.0, 0.0
    for i, q in enumerate(qs):
        raw = answers.get(i)
        ans = {"index": int(raw)} if q.kind in ("mcq", "ai_moment") and raw not in (None, "") else {"text": str(raw or "")}
        qd = vars(q) if getattr(q, "from_bank", False) else dataclasses.asdict(q)
        m = _OFFLINE.mark_answer(qd, ans, ctx.misconceptions)
        got, mx = got + m.score, mx + m.max_score
        results.append({"prompt": q.prompt, "correct": m.score >= m.max_score, "why": m.rationale,
                        "answer": (q.answer or {}).get("text") or (q.answer or {}).get("model")})
    score = round(got / mx, 3) if mx else 0.0
    level = mastery.record(tx, actor.user_id, topic_id, "practice", score)
    return {"score": score, "results": results, "level": level}


def understanding_prompt(tx: Tx, actor: Actor, topic_id: int) -> str:
    """The explain-and-apply prompt for a topic's understanding check (same one used for marking)."""
    require(actor, "student")
    _require_open(tx, actor, topic_id)
    return _OFFLINE.generate_questions([topic_ctx(tx, topic_id)], "viva", "medium", 1, seed=topic_id)[0].prompt


def understanding_check(db: Db, engine: Any, actor: Actor, topic_id: int, explain_text: str, apply_text: str) -> dict[str, Any]:
    """End-of-topic check: explain it back and apply it to a new case; sets mastery and suggests the next step (LN-4, LN-8)."""
    require(actor, "student")
    if len((explain_text + apply_text).split()) < 5:
        raise ValidationError("Write a little more so we can see what you understand.")
    with db.tenant(actor.institution_id) as tx:
        _require_open(tx, actor, topic_id)
        ctx = topic_ctx(tx, topic_id)
    q = dataclasses.asdict(_OFFLINE.generate_questions([ctx], "viva", "medium", 1, seed=topic_id)[0])
    m = engine.mark_answer(q, {"text": f"{explain_text}\n{apply_text}"}, ctx.misconceptions)
    score = round(m.score / m.max_score, 3) if m.max_score else 0.0
    with db.tenant(actor.institution_id) as tx:
        level = mastery.record(tx, actor.user_id, topic_id, "understanding", score,
                               {"explain": explain_text[:1500], "apply": apply_text[:1500], "rationale": m.rationale})
        step = mastery.next_step(tx, actor.user_id, topic_id, score)
        finished = _complete_learn_items(tx, actor.user_id, topic_id, "learn", score)
    from novaquest.services import attempts  # local import: attempts imports learn-free modules only
    for attempt_id in finished:
        attempts.submit(db, engine, actor, attempt_id)
    return {"score": score, "level": level, "rationale": m.rationale, "next": step, "prompt": q["prompt"]}


def _complete_learn_items(tx: Tx, student_id: int, topic_id: int, kind: str, score: float) -> list[int]:
    """Mark matching 'learn'/'teach' items in open assignments as done; return attempts now complete (LN-6, TA-8)."""
    rows = tx.all("""SELECT t.id AS attempt_id, q.id AS question_id FROM attempts t
        JOIN assignment_questions aq ON aq.assignment_id=t.assignment_id JOIN questions q ON q.id=aq.question_id
        WHERE t.student_id=%s AND t.status='in_progress' AND t.deadline_at > %s AND q.kind=%s AND q.topic_id=%s
          AND q.status='approved'""", [student_id, clock.now(), kind, topic_id])
    done_attempts = []
    for r in rows:
        tx.exec("""INSERT INTO responses(institution_id, attempt_id, question_id, answer, saved_at) VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (attempt_id, question_id) DO UPDATE SET answer=EXCLUDED.answer, saved_at=EXCLUDED.saved_at""",
                [tx.inst, r["attempt_id"], r["question_id"], jsonb({"done": True, "score": score}), clock.now()])
        remaining = tx.val("""SELECT count(*) FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id
            JOIN attempts t ON t.assignment_id=aq.assignment_id
            LEFT JOIN responses r ON r.attempt_id=t.id AND r.question_id=q.id
            WHERE t.id=%s AND q.status='approved' AND coalesce((r.answer->>'done')::boolean, false) = false""", [r["attempt_id"]])
        if remaining == 0:
            done_attempts.append(r["attempt_id"])
    return done_attempts


def set_topic_open(tx: Tx, actor: Actor, class_id: int, topic_id: int, is_open_: bool) -> None:
    """Teacher opens or locks a topic for self-study; locked topics are hidden from students (LN-7)."""
    require(actor, "teacher")
    require_class(tx, actor, class_id, write=True)
    tx.exec("""INSERT INTO topic_settings(institution_id, class_id, node_id, open_for_self_study) VALUES (%s,%s,%s,%s)
               ON CONFLICT (class_id, node_id) DO UPDATE SET open_for_self_study=EXCLUDED.open_for_self_study""",
            [tx.inst, class_id, topic_id, is_open_])
    audit.record(tx, "topic.open" if is_open_ else "topic.lock", actor_id=actor.user_id, target=f"topic:{topic_id}",
                 detail={"class_id": class_id})


def tutor_log(tx: Tx, actor: Actor, student_id: int, limit: int = 200) -> list[dict]:
    """A student's tutor conversations, for their teacher (LN-7, SX-2)."""
    require(actor, "teacher", "leader", "inst_admin")  # the principal reads too (read-only)
    class_id = student_class_id(tx, student_id)
    if class_id is None:
        raise NotFound("That student was not found.")
    require_class(tx, actor, class_id)
    return tx.all("""SELECT m.context, m.role, m.body, m.hint_level, m.created_at, t.title AS topic_title, q.prompt AS question
        FROM tutor_messages m LEFT JOIN syllabus_nodes t ON t.id=m.topic_id LEFT JOIN questions q ON q.id=m.question_id
        WHERE m.student_id=%s ORDER BY m.created_at DESC, m.id DESC LIMIT %s""", [student_id, limit])


def revision_plan(tx: Tx, actor: Actor) -> list[dict]:
    """The student's revision plan, weakest outcomes first, with lesson links (LN-9)."""
    require(actor, "student")
    return mastery.revision_plan(tx, actor.user_id)


def ensure_not_locked_for(actor: Actor) -> None:
    """Guard used by routes: only students use Learn mode."""
    if actor.role != "student":
        raise Forbidden("Learn mode is for students.")
