"""Paper and projector versions of an assignment (teacher review, item 11): a printable worksheet with an answer key on
its own page, and a one-question-per-screen view for the classroom projector. Only approved questions are shown."""
from __future__ import annotations

from typing import Any

from novaquest.core.db import Tx
from novaquest.services import assignments, questions, review
from novaquest.services.access import Actor, require, require_class

LETTERS = "abcdefgh"
LINES = {"short": 5, "viva": 7, "ai_moment": 4, "mcq": 0, "numeric": 4}  # writing lines on paper per kind of question


def sheet(tx: Tx, actor: Actor, assignment_id: int) -> dict[str, Any]:
    """Everything a worksheet or the projector view needs: the assignment, its approved questions with lettered
    options and the right answer, how many are still waiting for approval, and the topics covered."""
    require(actor, "teacher", "leader", "inst_admin")
    a = assignments.get(tx, assignment_id)
    require_class(tx, actor, a["class_id"])
    all_qs = questions.assignment_questions(tx, assignment_id)
    qs = []
    for q in all_qs:
        if q["status"] != "approved" or q["kind"] in ("learn", "teach"):
            continue
        key = q["answer"] or {}
        right = key.get("index") if q["options"] else None
        qs.append({**q, "no": len(qs) + 1, "letters": [(LETTERS[i], o) for i, o in enumerate(q["options"] or [])],
                   "right_letter": LETTERS[right] if isinstance(right, int) and right < len(LETTERS) else None,
                   "right_text": key.get("text"), "model": key.get("model") or key.get("worked"),
                   "points": [p["point"] for p in (q["rubric"] or []) if p.get("point")],
                   "lines": LINES.get(q["kind"], 5) if not q["options"] else 0})
    topics = []
    for q in qs:
        if q["topic_title"] not in topics:
            topics.append(q["topic_title"])
    school = tx.val("SELECT name FROM institutions WHERE id=%s", [a["institution_id"]])
    return {"assignment": a, "questions": qs, "waiting": sum(1 for q in all_qs if q["status"] == "draft"),
            "topics": topics, "school": school}


def class_results(tx: Tx, actor: Actor, assignment_id: int) -> dict[int, int]:
    """Question id -> share of the class that got it right, once anyone has handed in (shown after 'Show answer')."""
    a = assignments.get(tx, assignment_id)
    if a["live_state"] in ("draft", "scheduled"):
        return {}
    b = review.question_breakdown(tx, actor, assignment_id)
    return {q["id"]: q["pct_right"] for q in b["questions"] if q["answered"]}
