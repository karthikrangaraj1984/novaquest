"""Teach NOVA: the student teaches an AI classmate who holds planned misconceptions (TA-1..TA-11).

NOVA's beliefs are an explicit state machine. Code changes a belief only after the evaluator
confirms the student's explanation is correct, so NOVA can never learn a wrong idea. A student may teach the key ideas
over several messages: the ideas each correct message explains are added up, and the belief is fixed once all are
taught (a message that says something wrong adds nothing). NOVA sees the recent chat and the ideas still missing, so it
answers what the student said and asks about what it still doesn't get."""
from __future__ import annotations

from typing import Any

from novaquest.ai import guard
from novaquest.ai.offline import OfflineEngine
from novaquest.ai.types import TeachVerdict
from novaquest.core import audit, clock
from novaquest.core.db import Db, Tx, jsonb
from novaquest.core.errors import Conflict, NotFound, ValidationError
from novaquest.services import language, mastery, syllabus
from novaquest.services.access import Actor, require, require_class, student_class_id

PERSONA = "NOVA"
MAX_TURNS = 16
RECENT_TURNS = 6  # the chat NOVA and the evaluator see: the last few messages
STUCK_AFTER = 3   # tries on one idea before NOVA says plainly which part it still doesn't get
_OFFLINE = OfflineEngine()
_CLARITY = {"correct": 1.0, "partial": 0.6, "vague": 0.25, "incorrect": 0.0, "off_topic": 0.0}


def available_misconceptions(tx: Tx, topic_id: int) -> list[dict]:
    """The reviewed misconceptions NOVA can hold for a topic: the topic's own first, then its chapter's (TA-2)."""
    t = syllabus.get_topic(tx, topic_id)
    rows = tx.all("""SELECT code, data FROM misconceptions WHERE (topic_id=%s OR chapter_id=%s) AND status='approved'
                     ORDER BY (topic_id=%s) DESC, code LIMIT 3""", [topic_id, t["chapter_id"], topic_id])
    return [r["data"] for r in rows]


def starters(m: dict, topic_title: str) -> list[str]:
    """Sentence starters so no student faces a blank box (TA-10)."""
    first = " ".join(str(m["key_ideas"][0][0]).split()[:6])  # short enough to read in full on a phone
    return [f"Actually, {first} …", "Here's an example: …", f"Think about {topic_title.lower()}: …"]


def start(tx: Tx, actor: Actor, topic_id: int, attempt_id: int | None = None) -> dict[str, Any]:
    """Begin (or resume) a teaching session for a topic (TA-1, TA-2, TA-8)."""
    require(actor, "student")
    existing = tx.one("""SELECT id FROM teach_sessions WHERE student_id=%s AND topic_id=%s AND status='active'
                         ORDER BY started_at DESC LIMIT 1""", [actor.user_id, topic_id])
    if existing:
        return session_view(tx, actor, existing["id"])
    mis = available_misconceptions(tx, topic_id)
    if not mis:
        raise NotFound("NOVA isn't ready to learn this topic yet. Try another topic.")
    topic = syllabus.get_topic(tx, topic_id)
    belief = {"items": [{"code": m["id"], "status": "held"} for m in mis], "current": 0}
    sid = tx.val("""INSERT INTO teach_sessions(institution_id, student_id, topic_id, attempt_id, belief, started_at)
                    VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                 [tx.inst, actor.user_id, topic_id, attempt_id, jsonb(belief), clock.now()])
    opening = (f"Hi! I'm {PERSONA}, your AI classmate. I've been reading about {topic['title'].lower()}, "
               f"but I'm a bit mixed up. {mis[0]['nova_says']}")
    _log(tx, sid, "nova", opening, None, mis[0]["id"])
    audit.record(tx, "teach.start", actor_id=actor.user_id, target=f"teach:{sid}", detail={"topic_id": topic_id})
    return session_view(tx, actor, sid)


def _log(tx: Tx, session_id: int, role: str, body: str, verdict: str | None, code: str | None) -> None:
    tx.exec("""INSERT INTO teach_turns(institution_id, session_id, role, body, verdict, target_code, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""", [tx.inst, session_id, role, body, verdict, code, clock.now()])


def _session(tx: Tx, session_id: int) -> dict:
    row = tx.one("SELECT * FROM teach_sessions WHERE id=%s", [session_id])
    if not row:
        raise NotFound("That teaching session was not found.")
    return row


def _mis_map(tx: Tx, codes: list[str]) -> dict[str, dict]:
    return {r["code"]: r["data"] for r in tx.all("SELECT code, data FROM misconceptions WHERE code = ANY(%s)", [codes])}


def session_view(tx: Tx, actor: Actor, session_id: int) -> dict[str, Any]:
    """Session state for the student screen: chat, progress and starters."""
    s = _session(tx, session_id)
    if actor.role == "student" and s["student_id"] != actor.user_id:
        raise NotFound("That teaching session was not found.")
    topic = syllabus.get_topic(tx, s["topic_id"])
    items = s["belief"]["items"]
    mis = _mis_map(tx, [i["code"] for i in items])
    cur = s["belief"]["current"]
    current = mis.get(items[cur]["code"]) if cur is not None and cur < len(items) else None
    turns = tx.all("SELECT role, body, verdict, target_code, created_at FROM teach_turns WHERE session_id=%s ORDER BY id", [session_id])
    return {"session": s, "topic": topic, "turns": turns,
            "progress": [{"label": mis[i["code"]]["label"], "status": i["status"]} for i in items],
            "fixed": sum(i["status"] == "resolved" for i in items), "total": len(items),
            "starters": starters(current, topic["title"]) if current and s["status"] == "active" else []}


def turn(db: Db, engine: Any, actor: Actor, session_id: int, text: str) -> dict[str, Any]:
    """One teaching turn: screen, evaluate, update belief only if correct, then NOVA replies (TA-3..TA-6, TA-11)."""
    require(actor, "student")
    text = text.strip()
    if not text:
        raise ValidationError("Explain your idea to NOVA first.")
    if len(text) > 1500:
        raise ValidationError("That's a lot! Try explaining it in a few short sentences.")
    with db.tenant(actor.institution_id) as tx:
        s = _session(tx, session_id)
        if s["student_id"] != actor.user_id:
            raise NotFound("That teaching session was not found.")
        if s["status"] != "active":
            raise Conflict("This teaching session has finished.")
        belief = s["belief"]
        mis = _mis_map(tx, [i["code"] for i in belief["items"]])
        topic = syllabus.get_topic(tx, s["topic_id"])
        n_turns = tx.val("SELECT count(*) FROM teach_turns WHERE session_id=%s AND role='student'", [session_id])
        recent = [(r["role"], r["body"]) for r in reversed(tx.all(
            "SELECT role, body FROM teach_turns WHERE session_id=%s ORDER BY id DESC LIMIT %s", [session_id, RECENT_TURNS]))]
    m = mis[belief["items"][belief["current"]]["code"]]
    check = guard.screen_input(text)
    if not check.ok and check.reason in ("distress", "blocked"):
        with db.tenant(actor.institution_id) as tx:
            _log(tx, session_id, "student", text, check.reason, m["id"])
            _log(tx, session_id, "nova", check.reply, None, m["id"])
        return {"reply": check.reply, "verdict": check.reason, "ended": False}
    items = belief["items"]
    item = items[belief["current"]]
    before = set(item.get("covered", []))
    verdict: TeachVerdict = engine.evaluate_teaching(m, text, topic["meta"].get("terms", []),
                                                     already=sorted(before), recent=recent)
    taught = before
    if verdict.verdict not in ("incorrect", "off_topic"):  # a message with a wrong idea in it adds nothing
        taught = before | set(verdict.covered)
        item["covered"] = sorted(taught)
        if verdict.verdict != "correct" and len(taught) >= len(m["key_ideas"]) > 0:
            verdict = TeachVerdict("correct", tuple(sorted(taught)), verdict.has_example,
                                   "Every key idea taught, over several messages.")
    if verdict.verdict != "correct":
        item["tries"] = int(item.get("tries", 0)) + 1
    missing = [" / ".join(g) for k, g in enumerate(m["key_ideas"]) if k not in taught]
    ctx = {"student": text, "recent": recent, "understood": [" / ".join(m["key_ideas"][k]) for k in sorted(taught)
                                                             if k < len(m["key_ideas"])],
           "missing": missing, "stuck": int(item.get("tries", 0)) >= STUCK_AFTER}
    next_m = None
    if verdict.verdict == "correct":  # the only place a belief changes
        items[belief["current"]]["status"] = "resolved"
        nxt = next((i for i, it in enumerate(items) if it["status"] == "held"), None)
        belief["current"] = nxt if nxt is not None else belief["current"]
        next_m = mis[items[nxt]["code"]] if nxt is not None else None
    reply = engine.nova_reply(m, verdict, next_m, topic["title"], ctx)
    last_nova = next((b for r, b in reversed(recent) if r == "nova"), "")
    if verdict.verdict != "correct" and (guard.repeats_error(reply, m.get("wrong_signals", []))
                                         or guard.same_question(reply, last_nova)):
        reply = _OFFLINE.nova_reply(m, verdict, next_m, topic["title"], ctx)
    all_done = all(it["status"] == "resolved" for it in items)
    ended = all_done or n_turns + 1 >= MAX_TURNS
    with db.tenant(actor.institution_id) as tx:
        _log(tx, session_id, "student", text, verdict.verdict, m["id"])
        tx.exec("UPDATE teach_sessions SET belief=%s WHERE id=%s", [jsonb(belief), session_id])
        if ended:
            reply = reply + (" " + _final_check(items, mis) if all_done else
                             " We've run out of time for today. Thanks for teaching me!")
        _log(tx, session_id, "nova", reply, None, m["id"])
        result = _finish(tx, actor, session_id, all_done) if ended else None
    completed = []
    if result:
        from novaquest.services import learn  # completes Teach NOVA assignment items (TA-8)
        with db.tenant(actor.institution_id) as tx:
            completed = learn._complete_learn_items(tx, actor.user_id, s["topic_id"], "teach", result["overall"])
        from novaquest.services import attempts
        for attempt_id in completed:
            attempts.submit(db, engine, actor, attempt_id)
    with db.tenant(actor.institution_id) as tx:
        lang = language.student_lang(tx, actor.user_id)
    if lang != "en":  # NOVA speaks the student's language; the session stays in English underneath
        reply = language.localise(db, engine, actor.institution_id, [reply], lang)[0]
    return {"reply": reply, "verdict": verdict.verdict, "ended": ended, "passed": bool(result and result["passed"]),
            "result": result}


def _final_check(items: list[dict], mis: dict[str, dict]) -> str:
    """NOVA answers a short check from what it was taught (TA-5)."""
    lines = [f"Q: {mis[i['code']]['probe']} A: {mis[i['code']]['correct_idea'].split('. ')[0].rstrip('.')}." for i in items]
    return "Quiz time! " + " ".join(lines) + " I passed! Thank you for teaching me."


def _finish(tx: Tx, actor: Actor, session_id: int, passed: bool) -> dict[str, Any]:
    """Score the session on accuracy, completeness, clarity and examples, and feed mastery (TA-6, TA-7)."""
    s = _session(tx, session_id)
    turns = tx.all("SELECT verdict, body FROM teach_turns WHERE session_id=%s AND role='student' AND verdict IS NOT NULL", [session_id])
    graded = [t for t in turns if t["verdict"] in _CLARITY]
    n = max(1, len(graded))
    items = s["belief"]["items"]
    resolved = sum(i["status"] == "resolved" for i in items)
    examples = sum(1 for t in graded if any(k in t["body"].lower() for k in ("example", "for instance", "such as", "like ")))
    scores = {"accuracy": round(1 - sum(t["verdict"] == "incorrect" for t in graded) / n, 2),
              "completeness": round(resolved / len(items), 2),
              "clarity": round(sum(_CLARITY[t["verdict"]] for t in graded) / n, 2),
              "examples": round(min(1.0, examples / max(1, resolved)), 2)}
    overall = round(0.4 * scores["completeness"] + 0.3 * scores["accuracy"] + 0.2 * scores["clarity"] + 0.1 * scores["examples"], 3)
    mis = _mis_map(tx, [i["code"] for i in items])
    fixed = [mis[i["code"]]["label"] for i in items if i["status"] == "resolved"]
    summary = (f"You fixed {resolved} of {len(items)} of NOVA's ideas" + (f": {'; '.join(fixed)}." if fixed else "."))
    if scores["examples"] >= 0.5:
        summary += " Your examples really helped NOVA."
    tx.exec("UPDATE teach_sessions SET status=%s, scores=%s, summary=%s, ended_at=%s WHERE id=%s",
            ["passed" if passed else "ended", jsonb({**scores, "overall": overall}), summary, clock.now(), session_id])
    mastery.record(tx, s["student_id"], s["topic_id"], "teach", overall, {"session_id": session_id})
    audit.record(tx, "teach.finish", actor_id=actor.user_id, target=f"teach:{session_id}", detail={"overall": overall})
    return {**scores, "overall": overall, "summary": summary, "passed": passed}


def transcript(tx: Tx, actor: Actor, session_id: int) -> dict[str, Any]:
    """Teacher view: full transcript, misconceptions resolved, and errors made while teaching (TA-9)."""
    require(actor, "teacher", "leader", "inst_admin")  # the principal reads too (read-only)
    s = _session(tx, session_id)
    class_id = student_class_id(tx, s["student_id"])
    require_class(tx, actor, class_id)
    view = session_view(tx, actor, session_id)
    view["errors"] = [t for t in view["turns"] if t["role"] == "student" and t["verdict"] == "incorrect"]
    view["student"] = tx.one("SELECT name FROM users WHERE id=%s", [s["student_id"]])
    return view


def sessions_for_class(tx: Tx, actor: Actor, class_id: int) -> list[dict]:
    """Teaching sessions in a class, newest first, for the review screen (TA-9)."""
    require_class(tx, actor, class_id)
    return tx.all("""SELECT s.id, s.status, s.scores, s.summary, s.started_at, u.name, t.title AS topic_title
        FROM teach_sessions s JOIN users u ON u.id=s.student_id JOIN syllabus_nodes t ON t.id=s.topic_id
        JOIN enrolments e ON e.student_id=s.student_id AND e.status='active'
        WHERE e.class_id=%s ORDER BY s.started_at DESC LIMIT 100""", [class_id])
