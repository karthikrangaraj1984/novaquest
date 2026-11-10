"""Build the AI context for syllabus topics (passages, misconceptions, AI moments)."""
from __future__ import annotations

from novaquest.ai.types import TopicCtx
from novaquest.core.db import Tx
from novaquest.services import syllabus


def topic_ctx(tx: Tx, topic_id: int, max_passages: int = 6) -> TopicCtx:
    """Gather what an AI engine needs about one topic, visible to the current institution."""
    t = syllabus.get_topic(tx, topic_id)
    passages = tuple(syllabus.topic_passages(tx, topic_id, max_passages))
    mis = tuple(r["data"] for r in tx.all(
        """SELECT data FROM misconceptions WHERE (topic_id=%s OR chapter_id=%s) AND status='approved'
           ORDER BY (topic_id=%s) DESC, code""",
        [topic_id, t["chapter_id"], topic_id]))
    own = tuple(m for m in mis if m["topic"] == t["code"]) or mis
    siblings = tx.all("SELECT meta FROM syllabus_nodes WHERE parent_id=%s AND kind='topic' AND id<>%s",
                      [t["chapter_id"], topic_id])
    sib_terms = tuple(term for s in siblings for term in s["meta"].get("terms", []))
    sib_text = tuple(r["body"] for r in tx.all("""SELECT p.body FROM source_passages p JOIN syllabus_nodes n ON n.id=p.node_id
        WHERE n.parent_id=%s AND n.id<>%s AND p.source_kind='textbook' ORDER BY p.id LIMIT 24""", [t["chapter_id"], topic_id]))
    own_codes = {m["id"] for m in own}
    other_wrong = tuple(m["label"] for m in mis if m["id"] not in own_codes)  # same chapter only
    return TopicCtx(
        id=t["id"], code=t["code"], title=t["title"], outcome=t["outcome"] or t["title"],
        terms=tuple(t["meta"].get("terms", [])), chapter_no=int(t["chapter_no"]) if str(t["chapter_no"]).isdigit() else 0, chapter_title=t["chapter_title"],
        ai_concept=t["chapter_meta"].get("ai_concept"), passages=passages, misconceptions=own,
        ai_moments=tuple(t["chapter_meta"].get("ai_moments", [])), sibling_terms=sib_terms,
        other_wrong=other_wrong, sibling_text=sib_text, course=syllabus.course(t))
