"""Syllabus loading, versioning, browsing and teacher notes (SY-1..SY-4)."""
from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

from novaquest.core import audit, clock
from novaquest.core.db import Db, Tx, jsonb
from novaquest.core.errors import Conflict, NotFound, ValidationError
from novaquest.services import ingest

log = logging.getLogger("novaquest.syllabus")


@dataclass(frozen=True, slots=True)
class LoadReport:
    """What a syllabus load created."""

    syllabus_id: int
    chapters: int
    topics: int
    passages: int
    misconceptions: int
    lessons: int


def _ref(chapter_no: int, sec: str, page: int) -> str:
    return f"NCERT Science Class VIII, Ch {chapter_no} §{sec}, p. {page}"


Pdfs = Path | Sequence[Path] | None


def _pdf_list(pdf: Pdfs) -> list[Path]:
    return [] if not pdf else ([pdf] if isinstance(pdf, Path) else list(pdf))


def passage_count(tx: Tx, syllabus_id: int) -> int:
    """How many textbook passages a syllabus version has (Ask NOVA and citations need them)."""
    return int(tx.val("SELECT count(*) FROM source_passages WHERE syllabus_id=%s AND source_kind='textbook'",
                      [syllabus_id]) or 0)


def attach_passages(db: Db, map_path: Path, pdf_path: Pdfs, *, actor_id: int | None) -> tuple[int, ingest.Book | None]:
    """Add textbook passages to an already-loaded syllabus version for every chapter that has none yet (the PDF, or
    some chapter PDFs, were copied in after setup). Returns (passages added, what was read from the files).

    Passages that an earlier version took from a file of another book or edition (its title matched a chapter, its
    page numbers don't) are removed first; `removed_chapters` on the returned book lists them."""
    spec = json.loads(map_path.read_text(encoding="utf-8"))
    files = _pdf_list(pdf_path)
    with db.platform() as tx:
        sid = tx.val("SELECT id FROM syllabi WHERE board=%s AND subject=%s AND grade=%s AND version=%s",
                     [spec["board"], spec["subject"], spec["grade"], spec["version"]])
        if not sid or not files:
            return 0, None
    book = ingest.assemble_book(files, spec["chapters"], spec["pdf_page_offset"])
    wrong = sorted({no for _, no in book.rejected if no not in book.chapters})
    gone, hit = 0, []
    if wrong:
        with db.platform() as tx:
            had = chapters_with_text(tx, sid)
            hit = [no for no in wrong if str(no) in had]
            gone = remove_chapter_passages(tx, sid, hit) if hit else 0
            if gone:
                audit.record(tx, "syllabus.passages_removed", actor_id=actor_id, target=f"syllabus:{sid}",
                             detail={"chapters": hit, "passages": gone, "reason": "file from another book"},
                             institution_id=None)
                log.warning("removed %s passages for chapters %s taken from another book", gone, hit)
    with db.platform() as tx:
        have = chapters_with_text(tx, sid)
    book = dataclasses.replace(book, removed=gone, removed_chapters=tuple(hit) if gone else ())
    todo = [ch for ch in spec["chapters"] if str(ch["no"]) not in have]
    if not todo:
        return 0, book
    with db.platform() as tx:
        topic_ids = {r["code"]: r["id"] for r in tx.all(
            "SELECT id, code FROM syllabus_nodes WHERE syllabus_id=%s AND kind='topic'", [sid])}
        rows = [(sid, topic_ids[p.section], _ref(ch["no"], p.section, p.page), p.page, p.body)
                for ch in todo if ch["no"] in book.chapters
                for p in ingest.build_passages(book.pages, ch, spec["pdf_page_offset"]) if p.section in topic_ids]
        if rows:
            tx.many("INSERT INTO source_passages(syllabus_id, node_id, ref, page, body) VALUES (%s,%s,%s,%s,%s)", rows)
            audit.record(tx, "syllabus.passages", actor_id=actor_id, target=f"syllabus:{sid}",
                         detail={"version": spec["version"], "passages": len(rows)}, institution_id=None)
    from novaquest.services import search
    search._IDF.pop(sid, None)
    log.info("attached %s passages to syllabus %s", len(rows), sid)
    return len(rows), book


def remove_chapter_passages(tx: Tx, syllabus_id: int, chapters: list[int]) -> int:
    """Delete the textbook passages of these chapters (they came from the wrong file). Returns how many went."""
    n = tx.val("""WITH gone AS (DELETE FROM source_passages p USING syllabus_nodes t, syllabus_nodes c
                   WHERE p.syllabus_id=%s AND p.source_kind='textbook' AND t.id=p.node_id AND c.id=t.parent_id
                     AND c.code = ANY(%s) RETURNING 1) SELECT count(*) FROM gone""",
               [syllabus_id, [str(c) for c in chapters]])
    from novaquest.services import search
    search._IDF.pop(syllabus_id, None)
    return int(n or 0)


def chapters_with_text(tx: Tx, syllabus_id: int) -> set[str]:
    """Chapter numbers (as text) that have textbook passages."""
    return {r["code"] for r in tx.all("""SELECT DISTINCT c.code FROM source_passages p
        JOIN syllabus_nodes t ON t.id = p.node_id JOIN syllabus_nodes c ON c.id = t.parent_id
        WHERE p.syllabus_id=%s AND p.source_kind='textbook'""", [syllabus_id])}


def load_syllabus(db: Db, map_path: Path, pdf_path: Pdfs, *, actor_id: int | None) -> LoadReport:
    """Load a syllabus version from its map (JSON) and the textbook: one whole-book PDF or several chapter PDFs.
    Older versions stay intact."""
    spec = json.loads(map_path.read_text(encoding="utf-8"))
    files = _pdf_list(pdf_path)
    pages = ingest.assemble_book(files, spec["chapters"], spec["pdf_page_offset"]).pages if files else []
    with db.platform() as tx:
        exists = tx.val("SELECT id FROM syllabi WHERE board=%s AND subject=%s AND grade=%s AND version=%s",
                        [spec["board"], spec["subject"], spec["grade"], spec["version"]])
        if exists:
            raise Conflict(f"Version {spec['version']} of this syllabus is already loaded.")
        tx.exec("UPDATE syllabi SET is_current=false WHERE board=%s AND subject=%s AND grade=%s",
                [spec["board"], spec["subject"], spec["grade"]])
        sid = tx.val("""INSERT INTO syllabi(board, subject, grade, version, title) VALUES (%s,%s,%s,%s,%s)
                        RETURNING id""", [spec["board"], spec["subject"], spec["grade"], spec["version"],
                                         spec.get("source_title", spec["subject"])])
        topic_ids: dict[str, int] = {}
        chapter_ids: dict[int, int] = {}
        n_pass = 0
        for ci, ch in enumerate(spec["chapters"], start=1):
            chap_id = tx.val("""INSERT INTO syllabus_nodes(syllabus_id, kind, code, title, ord, path, meta)
                VALUES (%s,'chapter',%s,%s,%s,%s,%s) RETURNING id""",
                [sid, str(ch["no"]), ch["title"], ci, f"c{ch['no']}",
                 jsonb({"start_page": ch["start_page"], "end_page": ch["end_page"],
                        "ai_concept": ch.get("ai_concept"), "demo": bool(ch.get("demo"))})])
            tx.exec("UPDATE syllabus_nodes SET parent_id=NULL WHERE id=%s", [chap_id])
            chapter_ids[ch["no"]] = chap_id
            for ti, tp in enumerate(ch["topics"], start=1):
                tpath = f"c{ch['no']}.t{tp['sec'].replace('.', '_')}"
                tid = tx.val("""INSERT INTO syllabus_nodes(syllabus_id, parent_id, kind, code, title, ord, path, meta)
                    VALUES (%s,%s,'topic',%s,%s,%s,%s,%s) RETURNING id""",
                    [sid, chap_id, tp["sec"], tp["title"], ti, tpath, jsonb({"terms": tp.get("terms", [])})])
                tx.exec("""INSERT INTO syllabus_nodes(syllabus_id, parent_id, kind, code, title, ord, path)
                    VALUES (%s,%s,'outcome',%s,%s,1,%s)""", [sid, tid, tp["sec"] + "-LO1", tp["outcome"], tpath + ".o1"])
                topic_ids[tp["sec"]] = tid
            if any(pages):
                rows = [(sid, topic_ids[p.section], _ref(ch["no"], p.section, p.page), p.page, p.body)
                        for p in ingest.build_passages(pages, ch, spec["pdf_page_offset"])]
                tx.many("INSERT INTO source_passages(syllabus_id, node_id, ref, page, body) VALUES (%s,%s,%s,%s,%s)", rows)
                n_pass += len(rows)
        for m in spec.get("misconceptions", []):
            tx.exec("""INSERT INTO misconceptions(syllabus_id, code, topic_id, chapter_id, label, data)
                VALUES (%s,%s,%s,%s,%s,%s)""", [sid, m["id"], topic_ids[m["topic"]], chapter_ids[m["chapter"]],
                                                m["label"], jsonb(m)])
        for sec, lesson in spec.get("lessons", {}).items():
            tx.exec("INSERT INTO lessons(node_id, data) VALUES (%s,%s)", [topic_ids[sec], jsonb(lesson)])
        for am in spec.get("ai_moments", []):
            tx.exec("UPDATE syllabus_nodes SET meta = meta || %s WHERE id=%s",
                    [jsonb({"ai_moments": [am]}), chapter_ids[am["chapter"]]])
        audit.record(tx, "syllabus.load", actor_id=actor_id, target=f"syllabus:{sid}",
                     detail={"version": spec["version"], "passages": n_pass}, institution_id=None)
    get_tree.cache_clear()
    log.info("loaded syllabus %s v%s: %s passages", spec["subject"], spec["version"], n_pass)
    return LoadReport(sid, len(chapter_ids), len(topic_ids), n_pass, len(spec.get("misconceptions", [])),
                      len(spec.get("lessons", {})))


def current_syllabus_id(tx: Tx, board: str = "NCERT", subject: str = "Science", grade: str = "8") -> int:
    """The current version of a shared (platform-wide) syllabus, by default NCERT Grade 8 Science."""
    sid = tx.val("""SELECT id FROM syllabi WHERE institution_id IS NULL AND board=%s AND subject=%s AND grade=%s
                    AND is_current""", [board, subject, grade])
    if not sid:
        raise NotFound("No syllabus loaded yet. Ask the NovaQuest admin to load it.")
    return sid


_SYL_COLS = "s.id, s.board, s.subject, s.grade, s.version, s.title, s.status, s.institution_id, s.meta, s.is_current, s.part"


def course(s: dict) -> str:
    """How AI prompts and replies name a book's course: 'Grade 8 Science' (+ the board unless it's NCERT)."""
    base = f"Grade {s['grade']} {s['subject']}"
    return base if s["board"] == "NCERT" else f"{base} ({s['board']})"


def label(s: dict) -> str:
    """How a book is named on screen: 'Science · NCERT Grade 8' ('… · Part 2' for one part of a book in parts)."""
    return f"{s['subject']} · {s['board']} Grade {s['grade']}" + (f" · Part {s['part']}" if s.get("part") else "")


# ---------- books in parts (1.25.0) ----------
def subject_key(s: dict) -> tuple:
    """Parts of one book (same school, board, subject and grade, each with a part number) are one subject."""
    if s.get("part"):
        return ("parts", s.get("institution_id"), str(s["board"]).lower(), str(s["subject"]).lower(), str(s["grade"]))
    return ("book", s["id"])


def subjects(books: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The subjects among some books, in the books' order: a book on its own, or a book's parts together. Each subject
    is the first book's fields with 'id' (its first part), 'ids' (all its parts), 'parts' (the books, in part order),
    and a label without the part ('Mathematics · NCERT Grade 8')."""
    groups: dict[tuple, list[dict]] = {}
    for b in books:
        groups.setdefault(subject_key(b), []).append(b)
    out = []
    for g in groups.values():
        g = sorted(g, key=lambda b: (b.get("part") or 0, b["id"]))
        first = g[0]
        base = {**first, "part": None}
        out.append({**first, "id": first["id"], "ids": [b["id"] for b in g], "parts": g,
                    "label": label(base) if len(g) > 1 or first.get("part") else label(first),
                    "part": None if len(g) > 1 else first.get("part")})
    return out


def subject_of(books: list[dict[str, Any]], wanted: int | None) -> dict[str, Any] | None:
    """The subject (all its parts) that a book id belongs to, else the first subject."""
    subs = subjects(books)
    return next((s for s in subs if wanted in s["ids"]), subs[0] if subs else None)


def subject_tree(tx: Tx, subject: dict[str, Any]) -> list[dict[str, Any]]:
    """A subject's chapters across its parts, in order; each chapter and topic carries its book ('syllabus_id') and,
    for a book in parts, 'part' (shown as 'Part 2 · Ch 1' and 'P2·1.1')."""
    out = []
    for b in subject.get("parts") or [subject]:
        p = b.get("part")
        for ch in get_tree(tx, b["id"]):
            out.append({**ch, "part": p, "syllabus_id": b["id"],
                        "topics": [{**t, "part": p, "syllabus_id": b["id"]} for t in ch["topics"]]})
    return out


def part_code(item: dict, code_key: str = "code") -> str:
    """'1.1' for a book on its own; 'P2·1.1' for a topic in part 2 of a book in parts."""
    code = str(item.get(code_key) or "")
    return f"P{item['part']}·{code}" if item.get("part") else code


def parts_of(tx: Tx, syllabus_id: int) -> list[int]:
    """All the published parts of the book this one is a part of (just itself for a book on its own)."""
    b = tx.one(f"SELECT {_SYL_COLS} FROM syllabi s WHERE s.id=%s", [syllabus_id])
    if not b or not b.get("part"):
        return [syllabus_id]
    rows = tx.all(f"""SELECT {_SYL_COLS} FROM syllabi s WHERE s.part IS NOT NULL AND s.status='published'
                      AND s.institution_id IS NOT DISTINCT FROM %s AND lower(s.board)=lower(%s) AND lower(s.subject)=lower(%s)
                      AND s.grade=%s ORDER BY s.part, s.id""", [b["institution_id"], b["board"], b["subject"], b["grade"]])
    ids = [r["id"] for r in rows]
    return ids if syllabus_id in ids else [syllabus_id] + ids


def subject_by_id(tx: Tx, syllabus_id: int) -> dict[str, Any]:
    """The subject a book belongs to: the book itself, or all the published parts of a book in parts."""
    return subjects([get_syllabus(tx, i) for i in parts_of(tx, syllabus_id)])[0]


def link_parts(tx: Tx, syllabus_id: int) -> None:
    """A newly published (or newly numbered) part joins the classes and teachers that already have another part of
    the same book, so Part 2 arrives without the admin ticking it again."""
    others = [i for i in parts_of(tx, syllabus_id) if i != syllabus_id]
    if not others or tx.val("SELECT status FROM syllabi WHERE id=%s", [syllabus_id]) != "published":
        return
    tx.exec("""INSERT INTO class_subjects(institution_id, class_id, syllabus_id, ord)
               SELECT DISTINCT ON (cs.class_id) cs.institution_id, cs.class_id, %s, cs.ord FROM class_subjects cs
               WHERE cs.syllabus_id = ANY(%s) ORDER BY cs.class_id, cs.ord ON CONFLICT (class_id, syllabus_id) DO NOTHING""",
            [syllabus_id, others])
    tx.exec("""INSERT INTO teaching_assignments(institution_id, teacher_id, class_id, syllabus_id)
               SELECT DISTINCT ta.institution_id, ta.teacher_id, ta.class_id, %s FROM teaching_assignments ta
               WHERE ta.syllabus_id = ANY(%s) ON CONFLICT DO NOTHING""", [syllabus_id, others])


def class_subjects(tx: Tx, class_id: int | None) -> list[dict[str, Any]]:
    """The subjects a class studies, a book's parts together."""
    return subjects(class_syllabi(tx, class_id))


def student_subjects(tx: Tx, student_id: int) -> list[dict[str, Any]]:
    return subjects(student_syllabi(tx, student_id))


def teacher_subjects(tx: Tx, user_id: int, class_id: int, role: str = "teacher") -> list[dict[str, Any]]:
    return subjects(teacher_syllabi(tx, user_id, class_id, role))


def get_syllabus(tx: Tx, syllabus_id: int) -> dict[str, Any]:
    """One syllabus this school may see (its own books and shared ones), else NotFound."""
    row = tx.one(f"SELECT {_SYL_COLS} FROM syllabi s WHERE s.id=%s", [syllabus_id])
    if not row:
        raise NotFound("That book was not found.")
    return {**row, "label": label(row)}


def class_syllabi(tx: Tx, class_id: int | None) -> list[dict[str, Any]]:
    """The published books (subjects) a class studies, in the school's order."""
    if class_id is None:
        return []
    rows = tx.all(f"""SELECT {_SYL_COLS} FROM class_subjects cs JOIN syllabi s ON s.id=cs.syllabus_id
                      WHERE cs.class_id=%s AND s.status='published' ORDER BY cs.ord, s.subject, s.id""", [class_id])
    return [{**r, "label": label(r)} for r in rows]


def student_syllabi(tx: Tx, student_id: int) -> list[dict[str, Any]]:
    """The books the student's class studies."""
    from novaquest.services.access import student_class_id

    return class_syllabi(tx, student_class_id(tx, student_id))


def teacher_syllabi(tx: Tx, user_id: int, class_id: int, role: str = "teacher") -> list[dict[str, Any]]:
    """The class's books this teacher teaches; leaders and admins see all of the class's books."""
    books = class_syllabi(tx, class_id)
    if role != "teacher":
        return books
    mine = {r["syllabus_id"] for r in tx.all("SELECT syllabus_id FROM teaching_assignments WHERE teacher_id=%s AND class_id=%s",
                                             [user_id, class_id])}
    return [b for b in books if b["id"] in mine]


def pick(books: list[dict[str, Any]], wanted: int | None) -> dict[str, Any] | None:
    """The chosen book or subject from a list (by id, or any of a subject's parts), else the first one."""
    return next((b for b in books if b["id"] == wanted or wanted in b.get("ids", ())), books[0] if books else None)


def available_books(tx: Tx, grade: str | None = None) -> list[dict[str, Any]]:
    """Published books a school can give its classes: shared ones and its own."""
    rows = tx.all(f"""SELECT {_SYL_COLS} FROM syllabi s WHERE s.status='published' AND (s.institution_id IS NOT NULL OR s.is_current)
                      AND (%s::text IS NULL OR s.grade=%s) ORDER BY s.grade, s.subject, s.board""", [grade, grade])
    return [{**r, "label": label(r)} for r in rows]


def topic_syllabus_id(tx: Tx, topic_id: int) -> int:
    """The book a topic belongs to (NotFound if this school can't see it)."""
    sid = tx.val("SELECT syllabus_id FROM syllabus_nodes WHERE id=%s", [topic_id])
    if not sid:
        raise NotFound("That topic was not found.")
    return sid


def set_class_subjects(tx: Tx, class_id: int, syllabus_ids: list[int]) -> None:
    """Replace the books a class studies (school admin). Unknown or unpublished books are refused."""
    ok = {b["id"] for b in available_books(tx)}
    bad = [i for i in syllabus_ids if i not in ok]
    if bad:
        raise ValidationError("Choose published books only.")
    if not tx.val("SELECT 1 FROM classes WHERE id=%s", [class_id]):
        raise NotFound("That class was not found.")
    tx.exec("DELETE FROM class_subjects WHERE class_id=%s AND NOT (syllabus_id = ANY(%s))", [class_id, syllabus_ids])
    for i, sid in enumerate(syllabus_ids):
        tx.exec("""INSERT INTO class_subjects(institution_id, class_id, syllabus_id, ord) VALUES (%s,%s,%s,%s)
                   ON CONFLICT (class_id, syllabus_id) DO UPDATE SET ord=EXCLUDED.ord""", [tx.inst, class_id, sid, i])


def list_versions(tx: Tx) -> list[dict[str, Any]]:
    """List every shared syllabus version (SY-4); schools' own books are listed on their Books page."""
    return tx.all("""SELECT s.id, s.board, s.subject, s.grade, s.version, s.title, s.is_current, s.created_at,
        (SELECT count(*) FROM source_passages p WHERE p.syllabus_id=s.id AND p.institution_id IS NULL) AS passages
        FROM syllabi s WHERE s.institution_id IS NULL ORDER BY s.created_at DESC""")


@lru_cache(maxsize=8)
def _tree_cached(syllabus_id: int, rows_json: str) -> tuple:
    rows = json.loads(rows_json)
    chapters: dict[int, dict] = {}
    topics: dict[int, dict] = {}
    for r in rows:
        if r["kind"] == "chapter":
            chapters[r["id"]] = {**r, "topics": []}
    for r in rows:
        if r["kind"] == "topic":
            t = {**r, "outcomes": []}
            topics[r["id"]] = t
            chapters[r["parent_id"]]["topics"].append(t)
    for r in rows:
        if r["kind"] == "outcome":
            topics[r["parent_id"]]["outcomes"].append(r)
    return tuple(sorted(chapters.values(), key=lambda c: c["ord"]))


class _TreeCache:
    """Size-bounded cache of syllabus trees keyed by version (trees never change once loaded)."""

    def __init__(self) -> None:
        self._data: dict[int, tuple] = {}

    def __call__(self, tx: Tx, syllabus_id: int) -> tuple:
        if not tx.val("SELECT 1 FROM syllabi WHERE id=%s", [syllabus_id]):  # RLS: only this school's and shared books
            raise NotFound("That book was not found.")
        if syllabus_id not in self._data:
            rows = tx.all("""SELECT id, parent_id, kind, code, title, ord, meta FROM syllabus_nodes
                             WHERE syllabus_id=%s ORDER BY ord""", [syllabus_id])
            if len(self._data) >= 8:
                self._data.pop(next(iter(self._data)))
            self._data[syllabus_id] = _tree_cached(syllabus_id, json.dumps(rows, default=str))
        return self._data[syllabus_id]

    def forget(self, syllabus_id: int) -> None:
        self._data.pop(syllabus_id, None)

    def cache_clear(self) -> None:
        self._data.clear()
        _tree_cached.cache_clear()


get_tree = _TreeCache()


def get_topic(tx: Tx, topic_id: int) -> dict[str, Any]:
    """Return one topic with its chapter, outcome and key terms."""
    row = tx.one("""SELECT t.id, t.code, t.title, t.meta, t.syllabus_id, c.id AS chapter_id, c.code AS chapter_no,
            c.title AS chapter_title, c.meta AS chapter_meta, s.board, s.subject, s.grade, s.part,
            (SELECT o.title FROM syllabus_nodes o WHERE o.parent_id=t.id AND o.kind='outcome' ORDER BY o.ord LIMIT 1) AS outcome
        FROM syllabus_nodes t JOIN syllabus_nodes c ON c.id=t.parent_id JOIN syllabi s ON s.id=t.syllabus_id
        WHERE t.id=%s AND t.kind='topic'""", [topic_id])
    if not row:
        raise NotFound("That topic was not found.")
    return row


def topic_passages(tx: Tx, topic_id: int, limit: int = 6) -> list[dict[str, Any]]:
    """Return a topic's source passages, textbook first, then this institution's notes."""
    return tx.all("""SELECT id, ref, page, body, source_kind FROM source_passages WHERE node_id=%s
                     ORDER BY source_kind, page NULLS LAST, id LIMIT %s""", [topic_id, limit])


def topic_misconceptions(tx: Tx, topic_ids: list[int]) -> list[dict[str, Any]]:
    """Reviewed misconceptions for the given topics or their chapters."""
    return tx.all("""SELECT m.code, m.label, m.data, m.topic_id FROM misconceptions m
        WHERE (m.topic_id = ANY(%s) OR m.chapter_id IN (SELECT parent_id FROM syllabus_nodes WHERE id = ANY(%s)))
          AND m.status='approved'
        ORDER BY (m.topic_id = ANY(%s)) DESC, m.code""", [topic_ids, topic_ids, topic_ids])


def add_teacher_notes(tx: Tx, topic_id: int, text: str, *, title: str, actor_id: int) -> int:
    """Attach a teacher's notes to a topic as extra AI source material (SY-3). Returns passages added."""
    topic = get_topic(tx, topic_id)
    clean = " ".join(text.split())
    if len(clean.split()) < 15:
        raise ValidationError("Notes are too short to be useful. Add at least a few sentences.")
    chunks = ingest.chunk_lines([(0, s.strip() + ".") for s in clean.split(".") if s.strip()])
    ref = f"Teacher notes: {title.strip()[:80] or 'Notes'} (topic {topic['code']})"
    tx.many("""INSERT INTO source_passages(syllabus_id, node_id, institution_id, source_kind, ref, body)
               VALUES (%s,%s,%s,'teacher_notes',%s,%s)""",
            [(topic["syllabus_id"], topic_id, tx.inst, ref, body) for _, body in chunks])
    audit.record(tx, "syllabus.notes_added", actor_id=actor_id, target=f"topic:{topic_id}", detail={"chunks": len(chunks)})
    return len(chunks)


def extract_upload_text(filename: str, data: bytes, tmp_dir: Path) -> str:
    """Return the text of an uploaded .txt or .pdf file."""
    name = filename.lower()
    if name.endswith(".txt") or name.endswith(".md"):
        return data.decode("utf-8", "replace")
    if name.endswith(".pdf"):
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path = tmp_dir / f"upload-{clock.now().timestamp():.0f}.pdf"
        path.write_bytes(data)
        try:
            return "\n".join(ingest.extract_pages(path))
        finally:
            path.unlink(missing_ok=True)
    raise ValidationError("Upload a PDF or a text file.")
