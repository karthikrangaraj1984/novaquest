"""Grounded retrieval over syllabus passages: Postgres full-text search, fused with pgvector
similarity when local embeddings (Ollama) are available."""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Callable, Sequence

from novaquest.core.db import Tx

log = logging.getLogger("novaquest.search")

Embedder = Callable[[Sequence[str]], list[list[float]]]
_RRF_K = 60


@dataclass(frozen=True, slots=True)
class Hit:
    """A retrieved passage."""

    id: int
    topic_id: int
    ref: str
    body: str
    score: float


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Ranked passages plus whether the question is about the syllabus at all."""

    hits: list[Hit]
    coverage: float
    on_syllabus: bool


def _fts(tx: Tx, syllabus_id: int, query: str, topic_ids: Sequence[int] | None, limit: int) -> list[dict]:
    topic_clause = "AND node_id = ANY(%(topics)s)" if topic_ids else ""
    return tx.all(f"""
        WITH q AS (SELECT nullif(replace(plainto_tsquery('english', %(q)s)::text, '&', '|'), '')::tsquery AS q)
        SELECT p.id, p.node_id AS topic_id, p.ref, p.body, ts_rank_cd(p.tsv, q.q, 1 | 32) AS rank,
               tsvector_to_array(p.tsv) AS lex
        FROM source_passages p, q
        WHERE q.q IS NOT NULL AND p.syllabus_id = %(sid)s AND p.tsv @@ q.q {topic_clause}
        ORDER BY rank DESC LIMIT %(lim)s""", {"q": query, "sid": syllabus_id, "topics": list(topic_ids or []), "lim": limit})


def _vector(tx: Tx, syllabus_id: int, qvec: list[float], topic_ids: Sequence[int] | None, limit: int) -> list[dict]:
    topic_clause = "AND node_id = ANY(%(topics)s)" if topic_ids else ""
    return tx.all(f"""SELECT id, node_id AS topic_id, ref, body, 1 - (embedding <=> %(v)s::vector) AS rank
        FROM source_passages WHERE syllabus_id = %(sid)s AND embedding IS NOT NULL {topic_clause}
        ORDER BY embedding <=> %(v)s::vector LIMIT %(lim)s""",
                  {"v": "[" + ",".join(f"{x:.6f}" for x in qvec) + "]", "sid": syllabus_id,
                   "topics": list(topic_ids or []), "lim": limit})


_IDF: dict[int, tuple[int, dict[str, int]]] = {}
_VECTOR_READY = False


def _vector_ready(tx: Tx) -> bool:
    """True once the database has the pgvector embedding column (checked until found, then cached)."""
    global _VECTOR_READY
    if not _VECTOR_READY:
        _VECTOR_READY = bool(tx.val("""SELECT count(*) FROM information_schema.columns
                                       WHERE table_name = 'source_passages' AND column_name = 'embedding'"""))
    return _VECTOR_READY


def _doc_freq(tx: Tx, syllabus_id: int) -> tuple[int, dict[str, int]]:
    """Lexeme document frequencies for a syllabus version (cached; versions never change)."""
    if syllabus_id not in _IDF:
        n = tx.val("SELECT count(*) FROM source_passages WHERE syllabus_id=%s AND source_kind='textbook'", [syllabus_id])
        rows = tx.all("SELECT word, ndoc FROM ts_stat(%s)",
                      [f"SELECT tsv FROM source_passages WHERE syllabus_id={int(syllabus_id)} AND source_kind='textbook'"])
        if len(_IDF) >= 8:
            _IDF.pop(next(iter(_IDF)))
        _IDF[syllabus_id] = (max(int(n or 0), 1), {r["word"]: r["ndoc"] for r in rows})
    return _IDF[syllabus_id]


def _coverage(tx: Tx, syllabus_id: int, query: str, bodies: list[str]) -> float:
    """Share of the question's meaning (IDF-weighted words) found in the top passages."""
    if not bodies:
        return 0.0
    row = tx.one("""SELECT
        (SELECT array_agg(l) FROM unnest(tsvector_to_array(to_tsvector('english', %s))) l) AS q,
        (SELECT array_agg(DISTINCT l) FROM unnest(tsvector_to_array(to_tsvector('english', %s))) l) AS d""",
                 [query, " ".join(bodies)])
    qlex = set(row["q"] or [])
    if not qlex:
        return 0.0
    n, df = _doc_freq(tx, syllabus_id)
    weight = {w: math.log((n + 1) / (df.get(w, 0) + 0.5)) for w in qlex}
    found = set(row["d"] or [])
    total = sum(weight.values())
    if not total:
        return 0.0
    unknown = sum(v for w, v in weight.items() if w not in found and df.get(w, 0) == 0) / total
    if unknown > 0.3:  # a big part of the question uses words the syllabus never mentions
        return 0.0
    return sum(v for w, v in weight.items() if w in found) / total


def _rerank(tx: Tx, syllabus_id: int, query: str, rows: list[dict]) -> list[dict]:
    """Order candidates by how much of the question's rare vocabulary they contain (IDF-weighted),
    breaking ties with Postgres' cover-density rank. Favours short, focused passages and teacher notes."""
    if not rows:
        return rows
    qlex = set(tx.val("SELECT tsvector_to_array(to_tsvector('english', %s))", [query]) or [])
    n, df = _doc_freq(tx, syllabus_id)
    idf = {w: math.log((n + 1) / (df.get(w, 0) + 0.5)) for w in qlex}
    total = sum(idf.values()) or 1.0
    for r in rows:
        lex = set(r.get("lex") or [])
        r["score"] = sum(v for w, v in idf.items() if w in lex) / total + 0.2 * float(r["rank"])
    return sorted(rows, key=lambda r: r["score"], reverse=True)


# How students ask, not what they ask about: removed before searching so "whats a bacteria" or "tell me about
# plastics" is judged on "bacteria" / "plastics". ("whats" isn't a Postgres stop word, so it would otherwise count as
# an unknown word and make an on-syllabus question look off-syllabus.)
_FILLER = re.compile(r"\b(?:what'?s|whats|wats|wat|whatre|what're|who'?s|how'?s|where'?s|tell|explain|describe|"
                     r"define|definition|meaning|means?|please|pls|plz|kindly|know|understand|give|u|ur)\b", re.I)


def clean_query(query: str) -> str:
    """The question without its conversational filler (kept as-is if nothing else is left)."""
    cleaned = " ".join(_FILLER.sub(" ", query).split())
    return cleaned if re.search(r"[A-Za-z]{3,}", cleaned) else query


def search(tx: Tx, syllabus_id: int, query: str, *, topic_ids: Sequence[int] | None = None, limit: int = 4,
           embedder: Embedder | None = None, min_coverage: float = 0.55) -> SearchResult:
    """Find the passages that best answer `query`, and judge whether it is on the syllabus."""
    query = clean_query(query.strip()[:500])
    fts = _rerank(tx, syllabus_id, query, _fts(tx, syllabus_id, query, topic_ids, 30))[: limit * 3]
    ranked: dict[int, tuple[float, dict]] = {}
    for i, r in enumerate(fts):
        ranked[r["id"]] = (1 / (_RRF_K + i), r)
    if embedder is not None and _vector_ready(tx):
        try:
            qvec = embedder([query])[0]
            for i, r in enumerate(_vector(tx, syllabus_id, qvec, topic_ids, limit * 3)):
                prev = ranked.get(r["id"], (0.0, r))
                ranked[r["id"]] = (prev[0] + 1 / (_RRF_K + i), prev[1])
        except Exception as exc:  # embeddings are an enhancement; full-text search still answers
            log.warning("vector search skipped: %s", exc)
    ordered = sorted(ranked.values(), key=lambda x: x[0], reverse=True)[:limit]
    hits = [Hit(r["id"], r["topic_id"], r["ref"], r["body"], round(s, 5)) for s, r in ordered]
    coverage = _coverage(tx, syllabus_id, query, [h.body for h in hits[:3]])
    return SearchResult(hits, coverage, bool(hits) and coverage >= min_coverage)
