"""School books (SY-5): a school admin uploads the textbook for any board, subject and grade; NovaQuest reads it in
the background, drafts its chapters and topics (Claude when available, else the book's own headings), the admin
reviews and publishes it, and Claude drafts each topic's picture lesson, quick checks and NOVA's mix-ups for approval.

Nothing reaches students until the admin publishes the book and approves its lessons. A school's books are visible
only to that school (row-level security on every syllabus table)."""
from __future__ import annotations

import hashlib
from datetime import timedelta
import logging
import re
import shutil
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from novaquest.core import audit, clock
from novaquest.core.db import Db, Tx, jsonb
from novaquest.core.errors import Conflict, NotFound, ValidationError
from novaquest.services import ingest, jobs, ocr, outline, pictures, syllabus
from novaquest.services.access import Actor, require
from novaquest.services.context import topic_ctx

log = logging.getLogger("novaquest.books")

BOARDS = ("CBSE", "ICSE", "IGCSE", "IB", "State board", "Other")
MAX_FILES = 40
MAX_FILE_BYTES = 80 * 1024 * 1024
MAX_BOOK_BYTES = 400 * 1024 * 1024
READ_CLAIM_MINUTES = 30  # a book whose reader made no progress for this long can be read again
READ_JOB, LESSONS_JOB, ESCAPE_JOB, REFRESH_JOB = "book_read", "book_lessons", "book_escape", "book_refresh"
ESCAPE_TEXT = 9000  # characters of a chapter's text given to Claude for its escape room
SCAN_HELP = ("This book's pages are scanned pictures, and they can't be read on the NovaQuest computer yet: {why}. "
             "Run NovaQuest-Prerequisites-Setup.exe again from the NovaQuest folder (it adds Tesseract OCR and its "
             "English language file), then press Read again.")
DRAFT_PARALLEL = 3  # topics drafted at once (Claude replies take a minute; three at a time fits normal rate limits)


def is_maths(subject: str | None) -> bool:
    """A maths book (Mathematics, Maths, Math, Ganit): its PDFs are read keeping powers and fractions (1.24.0)."""
    return bool(re.search(r"\bmath|ganit", subject or "", re.I))


def storage_dir(data_dir: Path) -> Path:
    """Where uploaded book PDFs are kept (one folder per school and book)."""
    return data_dir / "books"


def _text(value: Any, label: str, lo: int, hi: int) -> str:
    v = " ".join(str(value or "").split())
    if not lo <= len(v) <= hi:
        raise ValidationError(f"{label}: enter {lo} to {hi} characters.", field=label.lower())
    return v


# ---------- upload ----------
def create(db: Db, actor: Actor, *, board: str, subject: str, grade: str, title: str,
           files: Iterable[tuple[str, BinaryIO]], storage: Path, part: Any = None) -> int:
    """Save an uploaded book (one PDF, or one PDF per chapter) and queue it to be read. Returns the book id.
    `part` (1-9, optional) marks one part of a subject printed in several books (NCERT Maths Part 1 and Part 2)."""
    require(actor, "inst_admin")
    part = _part(part)
    board = _text(board, "Board", 2, 40)
    subject = _text(subject, "Subject", 2, 60)
    grade = _text(grade, "Grade", 1, 20)
    title = _text(title or f"{subject} · {board} Grade {grade}", "Title", 3, 120)
    files = [(name, fh) for name, fh in files if name]
    if not files:
        raise ValidationError("Choose the book's PDF file(s).", field="files")
    if len(files) > MAX_FILES:
        raise ValidationError(f"Upload at most {MAX_FILES} files (one per chapter).", field="files")
    version = clock.now().strftime("upload %Y-%m-%d %H:%M:%S.%f")
    with db.tenant(actor.institution_id) as tx:
        sid = tx.val("""INSERT INTO syllabi(institution_id, board, subject, grade, version, title, is_current, status,
                        created_by, meta, part) VALUES (%s,%s,%s,%s,%s,%s,false,'processing',%s,%s,%s) RETURNING id""",
                     [tx.inst, board, subject, grade, version, title, actor.user_id,
                      jsonb({"stage": "queued", "files": len(files)}), part])
    folder = storage / str(actor.institution_id) / str(sid)
    folder.mkdir(parents=True, exist_ok=True)
    saved, total = [], 0
    try:
        for i, (name, fh) in enumerate(files, 1):
            dest = folder / f"{i:03d}.pdf"
            digest, size = hashlib.sha256(), 0
            head = fh.read(5)
            if head != b"%PDF-":
                raise ValidationError(f"{name} isn't a PDF file.", field="files")
            with open(dest, "wb") as out:
                out.write(head)
                digest.update(head)
                size = len(head)
                while chunk := fh.read(1024 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if size > MAX_FILE_BYTES or total > MAX_BOOK_BYTES:
                        raise ValidationError(f"{name} is too large (at most {MAX_FILE_BYTES // 2**20} MB per file).",
                                              field="files")
                    digest.update(chunk)
                    out.write(chunk)
            saved.append((Path(name).name[:200], dest.name, digest.hexdigest(), size))
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        with db.tenant(actor.institution_id) as tx:
            tx.exec("DELETE FROM syllabi WHERE id=%s", [sid])
        raise
    with db.tenant(actor.institution_id) as tx:
        tx.many("""INSERT INTO book_files(institution_id, syllabus_id, filename, stored_as, sha256, bytes)
                   VALUES (%s,%s,%s,%s,%s,%s)""", [(tx.inst, sid, n, s, h, b) for n, s, h, b in saved])
        jobs.enqueue(tx, READ_JOB, {"syllabus_id": sid}, dedupe_key=f"{READ_JOB}:{sid}")
        audit.record(tx, "book.upload", actor_id=actor.user_id, target=f"syllabus:{sid}",
                     detail={"board": board, "subject": subject, "grade": grade, "files": len(saved)})
    return sid


def _progress(db: Db, inst: int, sid: int, **meta: Any) -> None:
    with db.tenant(inst) as tx:
        tx.exec("UPDATE syllabi SET meta = meta || %s WHERE id=%s", [jsonb({**meta, "reader_at": clock.now().isoformat()}), sid])


# ---------- reading the book (background job) ----------
def read_book(db: Db, engine: Any, inst: int, sid: int, storage: Path) -> None:
    """Extract the text, find chapters and topics, and store them with their passages; the book then waits for
    the admin's review. A scanned book (pages that are pictures) is read with OCR first. Any failure is recorded on
    the book (status 'failed') with a plain message."""
    try:
        _read_book(db, engine, inst, sid, storage)
    except Exception as exc:  # the admin sees the reason and can delete the book and try again
        log.exception("reading book %s failed", sid)
        msg = exc.message if isinstance(exc, ValidationError) else "NovaQuest couldn't read this book's PDF files."
        with db.tenant(inst) as tx:
            tx.exec("UPDATE syllabi SET status='failed', meta = meta || %s WHERE id=%s",
                    [jsonb({"stage": "failed", "error": msg}), sid])


def _read_book(db: Db, engine: Any, inst: int, sid: int, storage: Path) -> None:
    with db.tenant(inst) as tx:
        book = syllabus.get_syllabus(tx, sid)
        files = tx.all("SELECT id, filename, stored_as FROM book_files WHERE syllabus_id=%s ORDER BY id", [sid])
        # Claim the book, so it is read once even if the worker and another caller both start (1.30.2). The reader
        # marks every step; a book with no mark for READ_CLAIM_MINUTES has a reader that stopped and can be taken over.
        claimed = tx.val("""UPDATE syllabi SET meta = meta || %s WHERE id=%s AND status='processing'
                              AND (meta->>'reader_at' IS NULL OR (meta->>'reader_at')::timestamptz < %s)
                            RETURNING id""",
                         [jsonb({"stage": "reading", "reader_at": clock.now().isoformat()}), sid,
                          clock.now() - timedelta(minutes=READ_CLAIM_MINUTES)])
        if not claimed:
            return
    texts = _book_texts(db, inst, sid, files, storage / str(inst) / str(sid))
    if not book.get("part"):
        found = detect_part(book["title"], "\n".join("\n".join(pages[:6]) for _, pages in texts))
        if found:
            with db.tenant(inst) as tx:
                tx.exec("UPDATE syllabi SET part=%s WHERE id=%s AND part IS NULL", [found, sid])
    chapters = _chapters_of(texts)
    course = syllabus.course(book)
    _progress(db, inst, sid, stage="outlining", done=0, total=len(chapters))
    plan: list[tuple[outline.Chapter, list[outline.Topic], str]] = []
    method = "headings"
    for k, ch in enumerate(chapters, 1):
        drafted = engine.outline_chapter(course, ch.title, [t for _, t in ch.lines]) if hasattr(engine, "outline_chapter") else None
        own = outline.find_topics(ch) if outline.has_numbered_sections(ch) else None
        topics = own or (_topics_from_draft(ch, drafted) if drafted else None)
        if own and drafted:  # the book numbers its sections: keep them as printed, with Claude's outcomes where they match
            outcomes = {_key(outline.strip_section_number(d.get("title", ""))[0]): d for d in drafted}
            for t in own:
                d = outcomes.get(_key(t.title))
                if d:
                    t.outcome = d.get("outcome") or t.outcome
                    t.terms = d.get("terms") or t.terms
        if drafted:
            method = "claude"
            claude_title = outline.tidy_title(str(drafted[0].get("chapter_title") or ""))
            ours, theirs = _key(ch.title), _key(claude_title)
            if claude_title and claude_title != ch.title and ch.title_from != "contents" and (
                    theirs == ours  # same words, Claude's capitalisation ('Ultimate treasure' -> 'Ultimate Treasure')
                    or not ch.source or ch.title == outline.tidy_title(Path(ch.source).stem)
                    or (len(theirs) > len(ours) and theirs.startswith(ours))   # ours is only its first line
                    or (len(ours) > len(theirs) >= 6 and ours.endswith(theirs))):  # ours took a caption in front
                ch.title = claude_title
        topics = topics or outline.find_topics(ch)
        plan.append((ch, topics, method))
        _progress(db, inst, sid, done=k)
    _store_outline(db, inst, sid, book, plan)
    with db.tenant(inst) as tx:
        tx.exec("UPDATE syllabi SET status='review', meta = meta || %s WHERE id=%s AND status='processing'",
                [jsonb({"stage": "review", "method": method, "chapters": len(plan),
                        "topics": sum(len(t) for _, t, _ in plan)}), sid])
    syllabus.get_tree.forget(sid)


def read_again(tx: Tx, actor: Actor, sid: int) -> None:
    """Read a book that couldn't be read once more (for example after Tesseract OCR was installed). Pages already
    read with OCR are kept."""
    require(actor, "inst_admin")
    book = own_book(tx, sid)
    if book["status"] != "failed":
        raise Conflict("Only a book that couldn't be read can be read again.")
    tx.exec("UPDATE syllabi SET status='processing', meta = (meta - 'error' - 'reader_at') || %s WHERE id=%s",
            [jsonb({"stage": "queued", "done": 0, "total": 0}), sid])
    jobs.enqueue(tx, READ_JOB, {"syllabus_id": sid}, dedupe_key=f"{READ_JOB}:{sid}:{clock.now().isoformat()}")
    audit.record(tx, "book.read_again", actor_id=actor.user_id, target=f"syllabus:{sid}")



def _book_texts(db: Db, inst: int, sid: int, files: list[dict], folder: Path, progress: bool = True) -> list[tuple[str, list[str]]]:
    """Each file's page texts, scanned pages read with OCR (saved page text is reused), as (file name, pages)."""
    texts = []
    with db.tenant(inst) as tx:
        maths = is_maths(tx.val("SELECT subject FROM syllabi WHERE id=%s", [sid]))
    for f in files:
        pages = ingest.extract_pages(folder / f["stored_as"], maths=maths)
        texts.append((f, pages))
        with db.tenant(inst) as tx:
            tx.exec("UPDATE book_files SET pages=%s WHERE id=%s", [len(pages), f["id"]])
    scans = [(k, ocr.scanned_pages(pages)) for k, (_, pages) in enumerate(texts)]
    total = sum(len(todo) for _, todo in scans)
    if total:  # pages that are pictures: read their words with OCR
        why = ocr.problem()
        if why:
            raise ValidationError(SCAN_HELP.format(why=why))
        before = 0
        for k, todo in scans:
            if not todo:
                continue
            f, pages = texts[k]
            if progress:
                _progress(db, inst, sid, stage="scanning", done=before, total=total)
            try:
                pages = ocr.read_scanned(folder / f["stored_as"], pages,
                                         (lambda done, _n, b=before: _progress(db, inst, sid, done=b + done)) if progress else None)
            except ocr.OCRFailed as exc:
                raise ValidationError(f"Text recognition couldn't read any page of {f['filename']}. {exc}. "
                                      f"Run NovaQuest-Prerequisites-Setup.exe again, then press Read again.") from exc
            texts[k] = (f, pages)
            before += len(todo)
        if progress:
            _progress(db, inst, sid, stage="reading", scanned=total)
    out = [(f["filename"], pages) for f, pages in texts]
    if maths:  # bold headings, for chapters that don't number their sections
        _HEADINGS.update({f["filename"]: tuple(ingest.bold_headings(folder / f["stored_as"])) for f, _ in texts})
    if sum(len(" ".join(p).split()) for _, p in out) < 200:
        raise ValidationError("These PDFs have almost no text NovaQuest can read, even with text recognition. "
                              "Upload a clearer copy of the book.")
    return out


_HEADINGS: dict[str, tuple[str, ...]] = {}  # file name -> bold headings, from _book_texts to _chapters_of (same thread)


def _chapters_of(texts: list[tuple[str, list[str]]]) -> list[outline.Chapter]:
    """The book's chapters from its files' text: 'Chapter N' headings, else numbered sections, else one per file."""
    if len(texts) == 1:
        lines = outline.clean_lines(texts[0][1])
        chapters = outline.split_chapters(lines)
        if len(chapters) == 1:  # no 'Chapter N' headings: split at numbered sections ('2.1 …'), else one chapter
            chapters = outline.split_sections(lines) or outline.file_chapters(texts)
    else:
        chapters = outline.file_chapters(texts)
    for ch in chapters:
        ch.title = ch.title or f"Chapter {ch.no}"
        ch.headings = _HEADINGS.pop(ch.source, ()) if ch.source else ()
    return chapters

def _key(text: str) -> str:
    return re.sub(r"\W+", " ", text).strip().lower()


def _topics_from_draft(ch: outline.Chapter, drafted: list[dict]) -> list[outline.Topic] | None:
    """Claude's topics, in line order, with sensible starts (first topic owns the chapter's opening text). A printed
    section number is taken off the title (NovaQuest numbers topics itself), and sub-sections ('3.2.2 …') stay
    inside their section instead of becoming topics."""
    seen, topics = set(), []
    for t in sorted(drafted, key=lambda d: d["start"]):
        title, levels = outline.strip_section_number(str(t.get("title", "")))
        if t["start"] in seen or not 0 <= t["start"] < len(ch.lines) or levels >= 3 or len(title) < 2:
            continue
        seen.add(t["start"])
        topics.append(outline.Topic(title, t["start"], t.get("outcome") or "", t.get("terms") or []))
    if not topics:
        return None
    topics[0].start = 0
    for k, t in enumerate(topics):
        end = topics[k + 1].start if k + 1 < len(topics) else len(ch.lines)
        body = " ".join(text for _, text in ch.lines[t.start:end])
        t.terms = t.terms or outline.key_terms(body, t.title)
        t.outcome = t.outcome or f"Explain the main ideas of {t.title[:1].lower() + t.title[1:]}."
    return topics


def _store_outline(db: Db, inst: int, sid: int, book: dict, plan: list) -> None:
    short = book["title"][:60]
    with db.tenant(inst) as tx:
        for ci, (ch, topics, _) in enumerate(plan, 1):
            pages = [p for p, _ in ch.lines]
            chap_id = tx.val("""INSERT INTO syllabus_nodes(syllabus_id, institution_id, kind, code, title, ord, path, meta)
                VALUES (%s,%s,'chapter',%s,%s,%s,%s,%s) RETURNING id""",
                [sid, inst, str(ch.no), ch.title, ci, f"c{ch.no}",
                 jsonb({"file": ch.source or None, "first_page": min(pages) if pages else None,
                        "last_page": max(pages) if pages else None})])
            for ti, (t, lines) in enumerate(zip(topics, outline.topic_lines(ch, topics)), 1):
                code = f"{ch.no}.{ti}"
                tid = tx.val("""INSERT INTO syllabus_nodes(syllabus_id, institution_id, parent_id, kind, code, title, ord, path, meta)
                    VALUES (%s,%s,%s,'topic',%s,%s,%s,%s,%s) RETURNING id""",
                    [sid, inst, chap_id, code, t.title[:120], ti, f"c{ch.no}.t{ch.no}_{ti}", jsonb({"terms": t.terms})])
                tx.exec("""INSERT INTO syllabus_nodes(syllabus_id, institution_id, parent_id, kind, code, title, ord, path)
                    VALUES (%s,%s,%s,'outcome',%s,%s,1,%s)""", [sid, inst, tid, code + "-LO1", t.outcome[:300], f"c{ch.no}.t{ch.no}_{ti}.o1"])
                rows = [(sid, tid, inst, f"{short}, Ch {ch.no}, p. {page}", page, body)
                        for page, body in ingest.chunk_lines(lines)]
                if rows:
                    tx.many("""INSERT INTO source_passages(syllabus_id, node_id, institution_id, source_kind, ref, page, body)
                               VALUES (%s,%s,%s,'textbook',%s,%s,%s)""", rows)


# ---------- the Books page ----------
def list_books(tx: Tx) -> list[dict[str, Any]]:
    """This school's own books with their state, size and which classes use them."""
    rows = tx.all("""SELECT s.id, s.board, s.subject, s.grade, s.title, s.status, s.meta, s.created_at, s.institution_id,
            s.version, s.is_current,
            (SELECT count(*) FROM syllabus_nodes n WHERE n.syllabus_id=s.id AND n.kind='topic') AS topics,
            (SELECT count(*) FROM syllabus_nodes n WHERE n.syllabus_id=s.id AND n.kind='chapter') AS chapters,
            (SELECT string_agg(c.label, ', ' ORDER BY c.label) FROM class_subjects cs JOIN classes c ON c.id=cs.class_id
             WHERE cs.syllabus_id=s.id AND c.status='active') AS classes,
            ((SELECT count(*) FROM picture_lessons p JOIN syllabus_nodes n ON n.id=p.node_id WHERE n.syllabus_id=s.id
              AND p.status='draft' AND NOT (p.data ? 'failed'))
             + (SELECT count(*) FROM lessons l JOIN syllabus_nodes n ON n.id=l.node_id WHERE n.syllabus_id=s.id
                AND l.status='draft')
             + (SELECT count(*) FROM misconceptions m WHERE m.syllabus_id=s.id AND m.status='draft')) AS drafts
        FROM syllabi s WHERE s.institution_id=%s ORDER BY s.created_at DESC""", [tx.inst])
    return [{**r, "label": syllabus.label(r)} for r in rows]


def _part(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 0
    if not 1 <= n <= 9:
        raise ValidationError("Part must be a number from 1 to 9.", field="part")
    return n


_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}
_PART_RE = re.compile(r"\bPart\s*[-–—:]?\s*(I{1,3}|IV|[1-4]|One|Two|Three)\b", re.I)
_WORDS = {"one": 1, "two": 2, "three": 3}


def detect_part(title: str, front: str = "") -> int | None:
    """The part number printed in a book's title or its first pages ("Part-I", "Part 2"), when one stands out."""
    for text in (title or "", front or ""):
        found = []
        for m in _PART_RE.finditer(text):
            w = m.group(1)
            n = _ROMAN.get(w.upper()) or _WORDS.get(w.lower()) or (int(w) if w.isdigit() else None)
            if n:
                found.append(n)
        if found:  # the part named most often; on a tie, the one named first (the cover comes first)
            return max(found, key=lambda n: (found.count(n), -found.index(n)))
    return None


def set_part(tx: Tx, actor: Actor, sid: int, part: Any) -> None:
    """Mark a book as Part N of its subject (or not a part, with a blank)."""
    require(actor, "inst_admin")
    own_book(tx, sid)
    n = _part(part)
    tx.exec("UPDATE syllabi SET part=%s WHERE id=%s", [n, sid])
    if n:
        syllabus.link_parts(tx, sid)
    audit.record(tx, "book.part", actor_id=actor.user_id, target=f"syllabus:{sid}", detail={"part": n})


def own_book(tx: Tx, sid: int) -> dict[str, Any]:
    """One of this school's own books (shared books can't be edited by a school)."""
    book = syllabus.get_syllabus(tx, sid)
    if book["institution_id"] != tx.inst:
        raise NotFound("That book was not found.")
    return book


def detail(tx: Tx, sid: int, storage: Path | None = None) -> dict[str, Any]:
    """A book with its chapters and topics, passage counts and the state of each topic's drafted material (and, given
    the storage folder, which of its uploaded files aren't on this computer)."""
    book = own_book(tx, sid)
    tree = syllabus.get_tree(tx, sid) if book["status"] not in ("processing", "failed") else ()
    ids = [t["id"] for ch in tree for t in ch["topics"]]
    counts = {r["node_id"]: r["n"] for r in tx.all("""SELECT node_id, count(*) n FROM source_passages
                                                      WHERE node_id = ANY(%s) GROUP BY node_id""", [ids])}
    pics = {r["node_id"]: ("failed" if r["failed"] else r["status"]) for r in tx.all(
        "SELECT node_id, status, data ? 'failed' AS failed FROM picture_lessons WHERE node_id = ANY(%s)", [ids])}
    les = {r["node_id"]: r["status"] for r in tx.all("SELECT node_id, status FROM lessons WHERE node_id = ANY(%s)", [ids])}
    mix: dict[int, dict[str, int]] = {}
    for r in tx.all("SELECT topic_id, status, count(*) n FROM misconceptions WHERE topic_id = ANY(%s) GROUP BY topic_id, status", [ids]):
        mix.setdefault(r["topic_id"], {})[r["status"]] = r["n"]
    files = tx.all("SELECT filename, stored_as, pages, bytes FROM book_files WHERE syllabus_id=%s ORDER BY id", [sid])
    for f in files:
        f["missing"] = storage is not None and not (storage / str(book["institution_id"]) / str(sid) / f["stored_as"]).is_file()
    drafts = sum(1 for v in pics.values() if v == "draft") + sum(1 for v in les.values() if v == "draft") \
        + sum(m.get("draft", 0) for m in mix.values())
    return {"book": book, "tree": tree, "counts": counts, "pics": pics, "les": les, "mix": mix, "files": files,
            "drafts": drafts, "used": tx.val("SELECT count(*) FROM assignments WHERE syllabus_id=%s", [sid]),
            "drafting_now": bool(tx.val("""SELECT 1 FROM jobs WHERE kind=%s AND status IN ('queued','running')
                                           AND payload->>'syllabus_id' = %s LIMIT 1""", [LESSONS_JOB, str(sid)])),
            "rooms": {r["chapter"]: r for r in tx.all("""SELECT chapter, status, room->>'title' AS title FROM escape_rooms
                                                         WHERE syllabus_id=%s""", [sid])},
            "rooms_now": bool(tx.val("""SELECT 1 FROM jobs WHERE kind=%s AND status IN ('queued','running')
                                        AND payload->>'syllabus_id' = %s LIMIT 1""", [ESCAPE_JOB, str(sid)]))}


def _require_editable(book: dict, *, structure: bool = False) -> None:
    if book["status"] in ("processing",):
        raise Conflict("NovaQuest is still reading this book. Try again in a minute.")
    if structure and book["status"] != "review":
        raise Conflict("Topics can only be removed before the book is published.")


def rename(tx: Tx, actor: Actor, sid: int, node_id: int, title: str, outcome: str | None = None) -> None:
    """Rename a chapter or topic (and set a topic's learning outcome)."""
    require(actor, "inst_admin")
    book = own_book(tx, sid)
    _require_editable(book)
    title = _text(title, "Title", 2, 120)
    node = tx.one("SELECT id, kind FROM syllabus_nodes WHERE id=%s AND syllabus_id=%s AND kind IN ('chapter','topic')", [node_id, sid])
    if not node:
        raise NotFound("That chapter or topic was not found.")
    tx.exec("UPDATE syllabus_nodes SET title=%s WHERE id=%s", [title, node_id])
    if outcome is not None and node["kind"] == "topic":
        tx.exec("UPDATE syllabus_nodes SET title=%s WHERE parent_id=%s AND kind='outcome'", [_text(outcome, "Outcome", 5, 300), node_id])
    syllabus.get_tree.forget(sid)


def remove_topic(tx: Tx, actor: Actor, sid: int, topic_id: int) -> None:
    """Remove a wrongly detected topic before publishing: its text joins the topic before it (or after it)."""
    require(actor, "inst_admin")
    book = own_book(tx, sid)
    _require_editable(book, structure=True)
    t = tx.one("SELECT id, parent_id, ord FROM syllabus_nodes WHERE id=%s AND syllabus_id=%s AND kind='topic'", [topic_id, sid])
    if not t:
        raise NotFound("That topic was not found.")
    keep = tx.val("""SELECT id FROM syllabus_nodes WHERE parent_id=%s AND kind='topic' AND id<>%s
                     ORDER BY (ord < %s) DESC, abs(ord - %s) LIMIT 1""", [t["parent_id"], topic_id, t["ord"], t["ord"]])
    if not keep:
        raise ValidationError("A chapter needs at least one topic.")
    tx.exec("UPDATE source_passages SET node_id=%s WHERE node_id=%s", [keep, topic_id])
    for table, col in (("picture_lessons", "node_id"), ("lessons", "node_id"), ("misconceptions", "topic_id")):
        tx.exec(f"DELETE FROM {table} WHERE {col}=%s", [topic_id])
    tx.exec("DELETE FROM syllabus_nodes WHERE parent_id=%s AND kind='outcome'", [topic_id])
    tx.exec("DELETE FROM syllabus_nodes WHERE id=%s", [topic_id])
    syllabus.get_tree.forget(sid)


def set_full_pages(tx: Tx, actor: Actor, sid: int, on: bool, rights_confirmed: bool = False) -> None:
    """Let students open this book's real pages (LN-11). Turning it on needs the admin to confirm the school has the
    right to show the full book; the confirmation is recorded in the audit log."""
    require(actor, "inst_admin")
    own_book(tx, sid)
    if on and not rights_confirmed:
        raise ValidationError("Tick the box to confirm your school has the right to show the full pages of this book.",
                              field="rights")
    tx.exec("UPDATE syllabi SET meta = meta || %s WHERE id=%s", [jsonb({"full_pages": bool(on)}), sid])
    if on:
        from novaquest.services import figures

        figures.queue_warm(tx, sid)
    audit.record(tx, "book.full_pages", actor_id=actor.user_id, target=f"syllabus:{sid}",
                 detail={"on": bool(on), "rights_confirmed": bool(rights_confirmed)})


def set_teacher_figures(tx: Tx, actor: Actor, sid: int, on: bool, rights_confirmed: bool = False) -> None:
    """Let teachers show this book's figures on their slides and prep pages (1.29.0), without opening the full pages
    to students. Turning it on needs the admin to confirm teachers may show the figures in class (recorded)."""
    require(actor, "inst_admin")
    own_book(tx, sid)
    if on and not rights_confirmed:
        raise ValidationError("Tick the box to confirm your teachers may show this book's figures in class.", field="rights")
    tx.exec("UPDATE syllabi SET meta = meta || %s WHERE id=%s", [jsonb({"teacher_figures": bool(on)}), sid])
    if on:
        from novaquest.services import figures

        figures.queue_warm(tx, sid)  # cut the figures out now, so teachers' pages open at once
    audit.record(tx, "book.teacher_figures", actor_id=actor.user_id, target=f"syllabus:{sid}",
                 detail={"on": bool(on), "rights_confirmed": bool(rights_confirmed)})


def publish(tx: Tx, actor: Actor, sid: int) -> None:
    """Make the book available to classes (students still only see lessons the admin approved)."""
    require(actor, "inst_admin")
    book = own_book(tx, sid)
    if book["status"] not in ("review", "archived"):
        raise Conflict("Only a book that has been read and reviewed can be published.")
    if not tx.val("SELECT 1 FROM source_passages WHERE syllabus_id=%s LIMIT 1", [sid]):
        raise ValidationError("This book has no readable text, so it can't be published.")
    tx.exec("UPDATE syllabi SET status='published', meta = meta || '{\"stage\": \"published\"}'::jsonb WHERE id=%s", [sid])
    syllabus.link_parts(tx, sid)
    audit.record(tx, "book.publish", actor_id=actor.user_id, target=f"syllabus:{sid}")


def archive(tx: Tx, actor: Actor, sid: int) -> None:
    """Hide a published book from classes (history and results are kept; it can be published again)."""
    require(actor, "inst_admin")
    own_book(tx, sid)
    tx.exec("UPDATE syllabi SET status='archived' WHERE id=%s AND status='published'", [sid])
    audit.record(tx, "book.archive", actor_id=actor.user_id, target=f"syllabus:{sid}")


def delete(db: Db, actor: Actor, sid: int, storage: Path) -> None:
    """Delete a book that was never published (for example a failed upload), with its files."""
    require(actor, "inst_admin")
    with db.tenant(actor.institution_id) as tx:
        book = own_book(tx, sid)
        if book["status"] == "published" or tx.val("SELECT 1 FROM assignments WHERE syllabus_id=%s LIMIT 1", [sid]):
            raise Conflict("This book is in use. Archive it instead.")
        ids = [r["id"] for r in tx.all("SELECT id FROM syllabus_nodes WHERE syllabus_id=%s", [sid])]
        for sql in ("DELETE FROM picture_lessons WHERE node_id = ANY(%s)", "DELETE FROM lessons WHERE node_id = ANY(%s)",
                    "DELETE FROM topic_settings WHERE node_id = ANY(%s)", "DELETE FROM learn_events WHERE topic_id = ANY(%s)",
                    "DELETE FROM mastery WHERE topic_id = ANY(%s)"):
            tx.exec(sql, [ids])
        for sql in ("DELETE FROM misconceptions WHERE syllabus_id=%s", "DELETE FROM source_passages WHERE syllabus_id=%s",
                    "DELETE FROM class_subjects WHERE syllabus_id=%s", "DELETE FROM teaching_assignments WHERE syllabus_id=%s",
                    "DELETE FROM book_files WHERE syllabus_id=%s"):
            tx.exec(sql, [sid])
        tx.exec("UPDATE syllabus_nodes SET parent_id=NULL WHERE syllabus_id=%s", [sid])
        tx.exec("DELETE FROM syllabus_nodes WHERE syllabus_id=%s", [sid])
        tx.exec("DELETE FROM syllabi WHERE id=%s", [sid])
        tx.exec("UPDATE jobs SET status='done', last_error='book deleted' WHERE status='queued' AND payload->>'syllabus_id'=%s",
                [str(sid)])
        audit.record(tx, "book.delete", actor_id=actor.user_id, target=f"syllabus:{sid}")
    syllabus.get_tree.forget(sid)
    shutil.rmtree(storage / str(actor.institution_id) / str(sid), ignore_errors=True)


# ---------- drafted lessons (Claude) ----------
def request_lessons(tx: Tx, actor: Actor, sid: int) -> None:
    """Queue Claude to draft every topic's picture lesson, quick checks and NOVA's mix-ups (skips topics done)."""
    require(actor, "inst_admin")
    book = own_book(tx, sid)
    _require_editable(book)
    if book["status"] == "failed":
        raise Conflict("This book couldn't be read.")
    tx.exec("UPDATE syllabi SET meta = meta || %s WHERE id=%s", [jsonb({"lessons": {"stage": "queued"}}), sid])
    jobs.enqueue(tx, LESSONS_JOB, {"syllabus_id": sid}, dedupe_key=f"{LESSONS_JOB}:{sid}:{clock.now().timestamp():.0f}")


def draft_lessons(db: Db, engine: Any, inst: int, sid: int, batch: int = 6, skip: list[int] | None = None,
                  failed_ids: list[int] | None = None) -> None:
    """Background job: draft material for the next few topics that have none yet (a few at once), then queue itself
    again, so a long book never holds up reminders and other jobs. A topic Claude couldn't draft is counted as
    'couldn't draft' (not done) and isn't tried again in this run; 'Continue drafting' tries it again. Stops cleanly
    without Claude (or at the school's daily AI limit) and says so on the book."""
    from concurrent.futures import ThreadPoolExecutor

    skip = list(skip or [])  # topics already tried in this run (drafted or not): each is tried once per run
    failed_ids = list(failed_ids or [])
    with db.tenant(inst) as tx:
        own = tx.one("SELECT id, institution_id, meta FROM syllabi WHERE id=%s", [sid])
        if not own or own["institution_id"] != inst:
            return
        topics = tx.all("""SELECT t.id, t.code FROM syllabus_nodes t JOIN syllabus_nodes c ON c.id=t.parent_id
                           WHERE t.syllabus_id=%s AND t.kind='topic' ORDER BY c.ord, t.ord""", [sid])
        rows = tx.all("SELECT node_id, data ? 'failed' AS failed FROM picture_lessons WHERE node_id = ANY(%s)",
                      [[t["id"] for t in topics]])
    seen = {r["node_id"] for r in rows}                      # drafted at least once
    with_pics = {r["node_id"] for r in rows if not r["failed"]}  # has a usable picture lesson
    todo = [t for t in topics if t["id"] not in with_pics and t["id"] not in skip]
    state = {"stage": "drafting", "done": len(seen - set(failed_ids or [])), "total": len(topics),
             "failed": len(failed_ids or []), "no_pictures": len(seen - with_pics)}
    if getattr(engine, "name", "") != "claude":
        state.update(stage="needs_claude")
        _progress(db, inst, sid, lessons=state)
        return
    _progress(db, inst, sid, lessons=state)
    work = []
    for t in todo[:batch]:
        with db.tenant(inst) as tx:
            ctx = topic_ctx(tx, t["id"])
        work.append((t, ctx))

    def one(item) -> bool:
        t, ctx = item
        if getattr(engine, "name", "") != "claude":
            return False
        data = engine.draft_topic(ctx)
        return _save_drafts(db, inst, sid, t["id"], t["code"], ctx.chapter_no, data) if data else False

    with ThreadPoolExecutor(max_workers=max(1, min(DRAFT_PARALLEL, len(work)))) as pool:
        for (t, _), ok in zip(work, pool.map(one, work)):
            skip.append(t["id"])
            if ok:
                state["done"] += t["id"] not in seen
            else:
                state["failed"] += 1
                failed_ids.append(t["id"])
            _progress(db, inst, sid, lessons=state)
    with db.tenant(inst) as tx:
        state["no_pictures"] = int(tx.val("""SELECT count(*) FROM picture_lessons WHERE node_id = ANY(%s) AND data ? 'failed'""",
                                          [[t["id"] for t in topics]]) or 0)
    left = len(todo) - len(work)
    if left > 0 and getattr(engine, "name", "") == "claude":
        with db.tenant(inst) as tx:  # more to do: continue in a fresh job
            jobs.enqueue(tx, LESSONS_JOB, {"syllabus_id": sid, "skip": skip, "failed": failed_ids},
                         dedupe_key=f"{LESSONS_JOB}:{sid}:{state['done']}:{len(skip)}")
        return
    state["stage"] = "ready" if not state["failed"] else "paused"
    _progress(db, inst, sid, lessons=state)


def _valid_lesson(d: Any) -> dict | None:
    try:
        checks = []
        for c in d.get("checks", [])[:3]:
            opts = [str(o).strip()[:120] for o in c["options"]]
            if len(opts) < 2 or len(set(opts)) != len(opts) or not 0 <= int(c["answer"]) < len(opts):
                continue
            checks.append({"q": str(c["q"]).strip()[:240], "options": opts, "answer": int(c["answer"]),
                           "why": str(c.get("why") or "").strip()[:240]})
        if not (str(d["explain"]).strip() and checks):
            return None
        return {"explain": str(d["explain"]).strip()[:1200], "explain_again": str(d.get("explain_again") or "").strip()[:800]
                or str(d["explain"]).strip()[:400], "example": (str(d["example"]).strip()[:800] if d.get("example") else None),
                "checks": checks}
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _valid_mixup(m: Any, code: str, topic_code: str, chapter_no: int) -> dict | None:
    try:
        key_ideas = [[str(w).strip().lower()[:40] for w in grp if str(w).strip()][:8] for grp in m["key_ideas"]][:3]
        key_ideas = [g for g in key_ideas if g]
        wrong = [str(w).strip().lower()[:60] for w in m.get("wrong_signals", []) if str(w).strip()][:6]
        out = {"id": code, "chapter": chapter_no, "topic": topic_code, "label": str(m["label"]).strip()[:80],
               "nova_says": str(m["nova_says"]).strip()[:300], "correct_idea": str(m["correct_idea"]).strip()[:400],
               "key_ideas": key_ideas, "wrong_signals": wrong, "probe": str(m["probe"]).strip()[:200],
               "example_prompt": str(m.get("example_prompt") or "Can you give me an example?").strip()[:200]}
        return out if key_ideas and wrong and all(out[k] for k in ("label", "nova_says", "correct_idea", "probe")) else None
    except (KeyError, TypeError):
        return None


def _save_drafts(db: Db, inst: int, sid: int, topic_id: int, code: str, chapter_no: int, data: dict) -> bool:
    """Validate and store one topic's drafts. Parts that fail validation are dropped (never shown to anyone)."""
    pics = data.get("pictures")
    try:
        if not isinstance(pics, dict) or any(c.get("pic", {}).get("kind") == "scene" for c in pics.get("cards", [])):
            raise pictures.ContentError("not a drawable lesson")
        pictures.validate_lesson(code, pics)
    except (pictures.ContentError, AttributeError, TypeError):
        pics = None
    lesson = _valid_lesson(data.get("lesson") or {})
    mixups = [m for k, raw in enumerate(data.get("mixups") or [], 1)
              if (m := _valid_mixup(raw, f"B{sid}-{code}-{k}", code, chapter_no))][:2]
    with db.tenant(inst) as tx:
        chapter_id = tx.val("SELECT parent_id FROM syllabus_nodes WHERE id=%s", [topic_id])
        # A topic is 'done' once it has a picture_lessons row; an empty one records that drafting failed.
        tx.exec("""INSERT INTO picture_lessons(node_id, institution_id, data, status) VALUES (%s,%s,%s,'draft')
                   ON CONFLICT (node_id) DO UPDATE SET data=EXCLUDED.data, status='draft', updated_at=now()
                   WHERE picture_lessons.status <> 'approved'""",
                [topic_id, inst, jsonb(pics or {"failed": True})])
        if lesson:
            tx.exec("""INSERT INTO lessons(node_id, institution_id, data, status) VALUES (%s,%s,%s,'draft')
                       ON CONFLICT (node_id) DO UPDATE SET data=EXCLUDED.data, status='draft'
                       WHERE lessons.status <> 'approved'""", [topic_id, inst, jsonb(lesson)])
        for m in mixups:
            tx.exec("""INSERT INTO misconceptions(syllabus_id, institution_id, code, topic_id, chapter_id, label, data, status)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'draft') ON CONFLICT (syllabus_id, code) DO UPDATE
                       SET data=EXCLUDED.data, label=EXCLUDED.label, status='draft' WHERE misconceptions.status <> 'approved'""",
                    [sid, inst, m["id"], topic_id, chapter_id, m["label"], jsonb(m)])
    return bool(pics or lesson or mixups)


def topic_drafts(tx: Tx, sid: int, topic_id: int) -> dict[str, Any]:
    """Everything drafted for one topic, for the admin to check (answers shown)."""
    own_book(tx, sid)
    topic = syllabus.get_topic(tx, topic_id)
    if topic["syllabus_id"] != sid:
        raise NotFound("That topic was not found.")
    pic = tx.one("SELECT data, status FROM picture_lessons WHERE node_id=%s", [topic_id])
    les = tx.one("SELECT data, status FROM lessons WHERE node_id=%s", [topic_id])
    mix = tx.all("SELECT code, data, status FROM misconceptions WHERE topic_id=%s ORDER BY code", [topic_id])
    return {"topic": topic, "pictures": pic if pic and not pic["data"].get("failed") else None,
            "failed": bool(pic and pic["data"].get("failed")), "lesson": les, "mixups": mix,
            "passages": syllabus.topic_passages(tx, topic_id, 3)}


def approve(tx: Tx, actor: Actor, sid: int, topic_id: int | None = None) -> int:
    """Approve the drafted material of one topic, or of the whole book. Returns how many items were approved."""
    require(actor, "inst_admin")
    own_book(tx, sid)
    ids = [topic_id] if topic_id else [r["id"] for r in tx.all("SELECT id FROM syllabus_nodes WHERE syllabus_id=%s AND kind='topic'", [sid])]
    n = 0
    n += tx.exec("""UPDATE picture_lessons SET status='approved' WHERE node_id = ANY(%s) AND status='draft'
                    AND NOT (data ? 'failed')""", [ids]) or 0
    n += tx.exec("UPDATE lessons SET status='approved' WHERE node_id = ANY(%s) AND status='draft'", [ids]) or 0
    n += tx.exec("UPDATE misconceptions SET status='approved' WHERE topic_id = ANY(%s) AND status='draft'", [ids]) or 0
    audit.record(tx, "book.approve", actor_id=actor.user_id, target=f"syllabus:{sid}", detail={"topic": topic_id, "items": n})
    return n


def discard(tx: Tx, actor: Actor, sid: int, topic_id: int, part: str, code: str | None = None) -> None:
    """Throw away one drafted part of a topic ('pictures', 'lesson' or one mix-up)."""
    require(actor, "inst_admin")
    own_book(tx, sid)
    if part == "pictures":
        tx.exec("UPDATE picture_lessons SET data='{\"failed\": true}'::jsonb, status='draft' WHERE node_id=%s", [topic_id])
    elif part == "lesson":
        tx.exec("DELETE FROM lessons WHERE node_id=%s AND institution_id=%s", [topic_id, tx.inst])
    elif part == "mixup" and code:
        tx.exec("DELETE FROM misconceptions WHERE topic_id=%s AND code=%s AND institution_id=%s", [topic_id, code, tx.inst])
    else:
        raise ValidationError("Unknown part.")


def safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", name)[:120]


# ---------- escape rooms (Claude drafts, the admin approves) ----------
def request_escape(tx: Tx, actor: Actor, sid: int) -> None:
    """Queue Claude to draft an escape room for every chapter that has none yet (published books only: the topics
    the locks point at are settled by then)."""
    require(actor, "inst_admin")
    book = own_book(tx, sid)
    if book["status"] != "published":
        raise Conflict("Publish the book first: escape rooms are made from its final chapters and topics.")
    tx.exec("UPDATE syllabi SET meta = meta || %s WHERE id=%s", [jsonb({"escape": {"stage": "queued"}}), sid])
    jobs.enqueue(tx, ESCAPE_JOB, {"syllabus_id": sid}, dedupe_key=f"{ESCAPE_JOB}:{sid}:{clock.now().timestamp():.0f}")


def _chapter_material(tx: Tx, sid: int, chapter_id: int) -> tuple[list[dict], str]:
    """A chapter's topics (code, title, outcome) and its text, a fair share from each topic, for Claude."""
    topics = tx.all("""SELECT id, code, title, meta->>'outcome' AS outcome FROM syllabus_nodes
                       WHERE parent_id=%s AND kind='topic' ORDER BY ord""", [chapter_id])
    share = ESCAPE_TEXT // max(1, len(topics))
    parts = []
    for t in topics:
        body = " ".join(r["body"] for r in tx.all("SELECT body FROM source_passages WHERE node_id=%s ORDER BY id", [t["id"]]))
        if body:
            parts.append(f"[{t['code']}] {body[:share]}")
    return topics, "\n".join(parts)


def draft_escape(db: Db, engine: Any, inst: int, sid: int, batch: int = 3, skip: list[int] | None = None,
                 failed: list[int] | None = None) -> None:
    """Background job: Claude drafts the escape rooms of the next few chapters without one (a few at once), each
    checked like the built-in rooms (a sorting lock must be solvable, and the clues chosen must matter). A draft that
    fails the checks is sent back once with the reason. Then the job queues itself again for the rest."""
    from concurrent.futures import ThreadPoolExecutor

    from novaquest.services import escape

    skip, failed = list(skip or []), list(failed or [])
    with db.tenant(inst) as tx:
        book = tx.one("SELECT id, institution_id, board, subject, grade, status FROM syllabi WHERE id=%s", [sid])
        if not book or book["institution_id"] != inst:
            return
        chapters = tx.all("""SELECT id, code, title FROM syllabus_nodes WHERE syllabus_id=%s AND kind='chapter'
                             ORDER BY ord""", [sid])
        have = {r["chapter"] for r in tx.all("SELECT chapter FROM escape_rooms WHERE syllabus_id=%s", [sid])}
    chapters = [c for c in chapters if str(c["code"]).isdigit()]
    state = {"stage": "drafting", "done": len(have), "total": len(chapters), "failed": len(failed)}
    if getattr(engine, "name", "") != "claude":
        state["stage"] = "needs_claude"
        _progress(db, inst, sid, escape=state)
        return
    _progress(db, inst, sid, escape=state)
    todo = [c for c in chapters if int(c["code"]) not in have and c["id"] not in skip]
    work = []
    for c in todo[:batch]:
        with db.tenant(inst) as tx:
            topics, text = _chapter_material(tx, sid, c["id"])
        if topics and text:
            work.append((c, topics, text))
        else:
            skip.append(c["id"])

    def one(item) -> bool:
        c, topics, text = item
        codes, problem = {t["code"] for t in topics}, None
        for _ in range(2):  # a draft that fails the checks goes back once with the reason
            data = engine.draft_escape_room(syllabus.course(book), int(c["code"]), c["title"], topics, text, problem)
            if not data:
                return False
            try:
                room = escape.from_draft(data, codes)
            except escape.ContentError as exc:
                problem = str(exc)[:600]
                log.info("escape room for chapter %s rejected: %s", c["code"], problem)
                continue
            with db.tenant(inst) as tx:
                tx.exec("""INSERT INTO escape_rooms(institution_id, syllabus_id, chapter, room, status, created_at)
                           VALUES (%s,%s,%s,%s,'draft',%s) ON CONFLICT (syllabus_id, chapter) DO UPDATE
                           SET room=EXCLUDED.room, created_at=EXCLUDED.created_at WHERE escape_rooms.status='draft'""",
                        [inst, sid, int(c["code"]), jsonb(room), clock.now()])
            return True
        return False

    with ThreadPoolExecutor(max_workers=max(1, min(DRAFT_PARALLEL, len(work) or 1))) as pool:
        for (c, _, _), ok in zip(work, pool.map(one, work)):
            skip.append(c["id"])
            if ok:
                state["done"] += 1
            else:
                state["failed"] += 1
                failed.append(c["id"])
            _progress(db, inst, sid, escape=state)
    if any(c["id"] not in skip for c in todo):  # more chapters to do: continue in a fresh job
        with db.tenant(inst) as tx:
            jobs.enqueue(tx, ESCAPE_JOB, {"syllabus_id": sid, "skip": skip, "failed": failed},
                         dedupe_key=f"{ESCAPE_JOB}:{sid}:{state['done']}:{len(skip)}")
        return
    state["stage"] = "ready" if not state["failed"] else "paused"
    _progress(db, inst, sid, escape=state)


def approve_escape(tx: Tx, actor: Actor, sid: int, chapter: int | None = None) -> int:
    """Approve one chapter's drafted room, or all of them: students and teachers see approved rooms only."""
    require(actor, "inst_admin")
    own_book(tx, sid)
    rows = tx.all("""UPDATE escape_rooms SET status='approved', approved_at=%s WHERE syllabus_id=%s AND status='draft'
                     AND (%s::int IS NULL OR chapter=%s) RETURNING chapter""", [clock.now(), sid, chapter, chapter])
    if rows:
        audit.record(tx, "book.escape_approve", actor_id=actor.user_id, target=f"syllabus:{sid}",
                     detail={"chapters": [r["chapter"] for r in rows]})
    return len(rows)


def discard_escape(tx: Tx, actor: Actor, sid: int, chapter: int) -> None:
    """Throw away a chapter's room (draft or approved, if nobody has played it yet) so Claude can draft it again."""
    require(actor, "inst_admin")
    own_book(tx, sid)
    if tx.val("SELECT 1 FROM escape_runs WHERE syllabus_id=%s AND chapter=%s LIMIT 1", [sid, chapter]):
        raise Conflict("Students have already played this room, so it can't be replaced.")
    tx.exec("DELETE FROM escape_rooms WHERE syllabus_id=%s AND chapter=%s", [sid, chapter])


# ---------- keeping uploaded files across upgrades ----------
def old_book_folders(root: Path) -> list[Path]:
    """var/books folders of this and earlier NovaQuest versions unzipped next to it (Downloads\novaquest-1.13.1\
    novaquest-1.13.1\var\books, or one level less deep), newest first."""
    found = [root / "var" / "books"]
    for base in {root.parent, root.parent.parent}:
        found += list(base.glob("novaquest-*/var/books")) + list(base.glob("novaquest-*/novaquest-*/var/books"))
    seen, out = set(), []
    for f in found:
        try:
            key = f.resolve()
        except OSError:
            continue
        if key not in seen and f.is_dir():
            seen.add(key)
            out.append(f)
    return sorted(out, key=lambda f: f.stat().st_mtime, reverse=True)


def bring_books_across(storage: Path, root: Path) -> list[str]:
    """Copy uploaded books (PDFs and their saved page text) that are missing from `storage` out of earlier version
    folders: one book folder (school/book) at a time, newest copy first, never overwriting. Old folders are left as
    they are. Returns the books copied, as 'school/book'."""
    copied = []
    for src in old_book_folders(root):
        if src.resolve() == storage.resolve():
            continue
        for book in sorted(p for p in src.glob("*/*") if p.is_dir()):
            dest = storage / book.parent.name / book.name
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(book, dest)
            copied.append(f"{book.parent.name}/{book.name}")
    return copied


def missing_files(db: Db, storage: Path) -> list[dict]:
    """School books whose uploaded PDF isn't on this computer (Book pages and Read again need it)."""
    with db.platform() as tx:
        rows = tx.all("""SELECT f.filename, f.stored_as, s.id, s.institution_id, s.title FROM book_files f
                         JOIN syllabi s ON s.id=f.syllabus_id WHERE s.institution_id IS NOT NULL ORDER BY s.id, f.id""")
    return [r for r in rows if not (storage / str(r["institution_id"]) / str(r["id"]) / r["stored_as"]).is_file()]


# ---------- refreshing a book's text ----------
def request_refresh(tx: Tx, actor: Actor, sid: int) -> None:
    """Queue reading the book's stored PDFs again to replace the text under each topic (after NovaQuest reads books
    better, e.g. 1.14.2 took diagram labels out of sentences). Topics, lessons, questions and progress are kept."""
    require(actor, "inst_admin")
    book = own_book(tx, sid)
    if book["status"] not in ("review", "published", "archived"):
        raise Conflict("Only a book that has been read can have its text refreshed.")
    tx.exec("UPDATE syllabi SET meta = meta || %s WHERE id=%s", [jsonb({"refresh": {"stage": "queued"}}), sid])
    jobs.enqueue(tx, REFRESH_JOB, {"syllabus_id": sid}, dedupe_key=f"{REFRESH_JOB}:{sid}:{clock.now().timestamp():.0f}")


def _locate(ch: outline.Chapter, titles: list[str]) -> list[outline.Topic] | None:
    """Where each of a chapter's existing topics starts in its freshly read lines: the book's numbered sections when
    there are as many as topics, else each topic's title found in order. None if they can't all be placed."""
    if outline.has_numbered_sections(ch):
        own = outline.find_topics(ch)
        if len(own) == len(titles):
            return own
    found, at = [], 0
    keys = [_key(t) for t in titles]
    for k, key in enumerate(keys):
        if not key:
            return None
        hit = next((i for i in range(at, len(ch.lines)) if key in _key(ch.lines[i][1])
                    or (len(_key(ch.lines[i][1])) >= 6 and key.startswith(_key(ch.lines[i][1])))), None)
        if hit is None:
            if k == 0:
                hit = 0  # the first topic owns the chapter's opening text anyway
            else:
                return None
        found.append(outline.Topic(titles[k], hit))
        at = hit + 1
    found[0] = outline.Topic(found[0].title, 0)
    return found


def refresh_text(db: Db, inst: int, sid: int, storage: Path) -> dict[str, Any]:
    """Background job: read the stored PDFs again and replace the passages under each existing topic, chapter by
    chapter. A chapter whose topics can't be placed in the new text (renamed or removed topics, a different split)
    is left as it was and listed. Nothing else changes: topic names, outcomes, lessons, questions, progress."""
    with db.tenant(inst) as tx:
        book = tx.one("SELECT id, institution_id, title, status FROM syllabi WHERE id=%s", [sid])
        if not book or book["institution_id"] != inst:
            return {}
        files = tx.all("SELECT id, filename, stored_as FROM book_files WHERE syllabus_id=%s ORDER BY id", [sid])
        nodes = tx.all("""SELECT c.id AS chapter_id, c.code AS chapter, t.id, t.code, t.title FROM syllabus_nodes c
                          JOIN syllabus_nodes t ON t.parent_id=c.id AND t.kind='topic'
                          WHERE c.syllabus_id=%s AND c.kind='chapter' ORDER BY c.ord, t.ord""", [sid])
    _progress(db, inst, sid, refresh={"stage": "reading"})
    folder = storage / str(inst) / str(sid)
    missing = [f["filename"] for f in files if not (folder / f["stored_as"]).is_file()]
    if missing:
        state = {"stage": "failed", "error": f"The uploaded PDF isn't on this computer ({', '.join(missing)}). Run nq up to "
                                             "copy it from an earlier NovaQuest folder, then try again."}
        _progress(db, inst, sid, refresh=state)
        return state
    try:
        chapters = {int(ch.no): ch for ch in _chapters_of(_book_texts(db, inst, sid, files, folder, progress=False))}
    except ValidationError as exc:
        state = {"stage": "failed", "error": exc.message}
        _progress(db, inst, sid, refresh=state)
        return state
    by_chapter: dict[str, list[dict]] = {}
    for n in nodes:
        by_chapter.setdefault(n["chapter"], []).append(n)
    short, done, skipped, passages = book["title"][:60], 0, [], 0
    with db.tenant(inst) as tx:
        for code, topics in by_chapter.items():
            ch = chapters.get(int(code)) if str(code).isdigit() else None
            placed = _locate(ch, [t["title"] for t in topics]) if ch else None
            if not placed:
                skipped.append(code)
                continue
            ids = [t["id"] for t in topics]
            tx.exec("DELETE FROM source_passages WHERE node_id = ANY(%s)", [ids])
            for t, lines in zip(topics, outline.topic_lines(ch, placed)):
                rows = [(sid, t["id"], inst, f"{short}, Ch {code}, p. {page}", page, body) for page, body in ingest.chunk_lines(lines)]
                if rows:
                    tx.many("""INSERT INTO source_passages(syllabus_id, node_id, institution_id, source_kind, ref, page, body)
                               VALUES (%s,%s,%s,'textbook',%s,%s,%s)""", rows)
                    passages += len(rows)
            done += 1
        audit.record(tx, "book.refresh_text", actor_id=None, target=f"syllabus:{sid}",
                     detail={"chapters": done, "skipped": skipped})
    syllabus.get_tree.forget(sid)
    state = {"stage": "done", "chapters": done, "skipped": skipped, "passages": passages,
             "at": clock.to_ist(clock.now()).strftime("%d %b, %H:%M")}
    _progress(db, inst, sid, refresh=state)
    return state
