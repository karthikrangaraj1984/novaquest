"""A student's language (1.22.0): their choice, and Claude's translations of what NOVA says and of questions.

NovaQuest keeps working in English underneath: questions, marking, NOVA's safety checks and teacher views all stay in
English, and the student sees a translation. Translations are kept (per school, language and text), so each sentence
is translated once. A student can also write or ask in their own language: questions to Ask NOVA are put into English
for the textbook search."""
from __future__ import annotations

import hashlib
from typing import Any, Sequence

from novaquest import i18n
from novaquest.core import audit
from novaquest.core.db import Db, Tx
from novaquest.core.errors import ValidationError
from novaquest.services.access import Actor, require


def student_lang(tx: Tx, user_id: int) -> str:
    return tx.val("SELECT lang FROM users WHERE id=%s", [user_id]) or "en"


def set_lang(tx: Tx, actor: Actor, lang: str) -> None:
    require(actor, "student")
    if lang not in i18n.LANGS:
        raise ValidationError("Choose one of the languages.")
    tx.exec("UPDATE users SET lang=%s WHERE id=%s", [lang, actor.user_id])
    audit.record(tx, "student.language", actor_id=actor.user_id, detail={"lang": lang})


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def localise(db: Db, engine: Any, inst: int, texts: Sequence[str], lang: str) -> list[str]:
    """`texts` in the student's language (Claude, kept for next time); the English when that isn't possible."""
    texts = list(texts)
    if lang == "en" or lang not in i18n.LANGS or not any(t.strip() for t in texts):
        return texts
    keys = [_hash(t) for t in texts]
    with db.tenant(inst) as tx:
        have = {r["src_hash"]: r["text"] for r in tx.all(
            "SELECT src_hash, text FROM translations WHERE lang=%s AND src_hash = ANY(%s)", [lang, keys])}
    missing = list(dict.fromkeys(t for t, k in zip(texts, keys) if k not in have and t.strip()))
    if missing:
        fn = getattr(engine, "translate", None)
        got = fn(missing, i18n.english_name(lang)) if fn else None
        if got:
            with db.tenant(inst) as tx:
                for src, out in zip(missing, got):
                    tx.exec("""INSERT INTO translations(institution_id, lang, src_hash, text) VALUES (%s,%s,%s,%s)
                               ON CONFLICT DO NOTHING""", [tx.inst, lang, _hash(src), out])
                    have[_hash(src)] = out
    return [have.get(k, t) for t, k in zip(texts, keys)]


def to_english(engine: Any, text: str) -> str:
    """A student's question in English for the textbook search (unchanged if it's English already)."""
    letters = [c for c in text if c.isalpha()]
    if not letters or sum(1 for c in letters if ord(c) > 0x24F) / len(letters) < 0.3:
        return text
    fn = getattr(engine, "translate", None)
    got = fn([text], "English") if fn else None
    return got[0] if got else text
