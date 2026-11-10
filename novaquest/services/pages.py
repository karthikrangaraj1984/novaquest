"""Book pages (LN-11): students can open the real printed pages of a topic, shown as images of the book's own PDF,
so figures, tables and layout appear exactly as printed (extracted text loses them).

Shown only when the book allows it: always for the shared NCERT book (published free by NCERT), and for a school's
own book only after its admin confirms the school has the right to show the full pages. Pages are rendered once with
Poppler's pdftoppm and cached on disk; only pages that belong to the topic can be requested."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from novaquest.core.db import Tx
from novaquest.core.errors import NotFound
from novaquest.services import ingest, syllabus

log = logging.getLogger("novaquest.pages")

ROOT = Path(__file__).resolve().parents[2]
NCERT_MAP = ROOT / "data" / "syllabus" / "ncert_g8_science.json"
SOURCE_DIR = ROOT / "data" / "source"
MAX_PAGES = 14  # a topic spans a few pages; never render a whole book for one request
DPI = 140       # sharp enough to zoom on a phone, about 250 KB per page as JPEG
_lock = threading.Lock()


def renderer() -> str | None:
    """Poppler's pdftoppm, if installed (the Windows installer puts it next to pdftotext)."""
    return shutil.which("pdftoppm")


def allowed(book: dict[str, Any]) -> bool:
    """Shared books always; a school's own book only when its admin switched full pages on."""
    return book["institution_id"] is None or bool((book.get("meta") or {}).get("full_pages"))


def topic_pages(tx: Tx, topic_id: int) -> list[int]:
    """The book pages a topic's text comes from, gaps filled (a topic that runs from p. 17 to p. 19 shows 17-19)."""
    pages = sorted({r["page"] for r in tx.all("""SELECT DISTINCT page FROM source_passages
                                                 WHERE node_id=%s AND source_kind='textbook' AND page IS NOT NULL""", [topic_id])})
    if not pages:
        return []
    return list(range(pages[0], min(pages[-1], pages[0] + MAX_PAGES - 1) + 1))


@lru_cache(maxsize=4)
def _ncert_files(listing: tuple[tuple[str, float], ...]) -> dict[int, tuple[str, int]]:
    """For NCERT chapter PDFs (any names) in data/source: chapter number -> (file, index of its first page).
    Cached per set of files, so the PDFs are scanned once."""
    spec = json.loads(NCERT_MAP.read_text(encoding="utf-8"))
    out: dict[int, tuple[str, int]] = {}
    for name, _ in listing:
        pages = ingest.extract_pages(Path(name))
        hit = ingest.find_chapter(pages, spec["chapters"])
        if hit and ingest.numbered_like(pages, hit[1], next(c for c in spec["chapters"] if c["no"] == hit[0])):
            out[hit[0]] = (name, hit[1])
    return out


def _ncert_source(topic: dict[str, Any], page: int) -> tuple[Path, int] | None:
    """(PDF file, 1-based page index) of a printed NCERT page: the whole book, or the right chapter file."""
    spec = json.loads(NCERT_MAP.read_text(encoding="utf-8"))
    whole = SOURCE_DIR / "ncert_science_class8_2018.pdf"
    if whole.exists():
        return whole, page + spec["pdf_page_offset"]
    if not SOURCE_DIR.is_dir():
        return None
    files = sorted(p for p in SOURCE_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    found = _ncert_files(tuple((str(p), p.stat().st_mtime) for p in files))
    ch = next((c for c in spec["chapters"] if str(c["no"]) == str(topic["chapter_no"])), None)
    if not ch or ch["no"] not in found:
        return None
    name, first = found[ch["no"]]
    return Path(name), first + (page - ch["start_page"]) + 1


def _school_source(tx: Tx, topic: dict[str, Any], page: int, data_dir: Path) -> tuple[Path, int] | None:
    """(PDF file, page index) for a school's own book: the chapter's file, or the book's only file."""
    files = tx.all("SELECT filename, stored_as, institution_id FROM book_files WHERE syllabus_id=%s ORDER BY id",
                   [topic["syllabus_id"]])
    want = (topic.get("chapter_meta") or {}).get("file")
    f = next((x for x in files if x["filename"] == want), None) or (files[0] if len(files) == 1 else None)
    if not f:
        return None
    from novaquest.services.books import storage_dir

    return storage_dir(data_dir) / str(f["institution_id"]) / str(topic["syllabus_id"]) / f["stored_as"], page


def page_view(tx: Tx, topic_id: int) -> dict[str, Any]:
    """What a topic's Book pages screen needs: the topic, its book, the pages, and whether they can be shown."""
    topic = syllabus.get_topic(tx, topic_id)
    book = syllabus.get_syllabus(tx, topic["syllabus_id"])
    pages = topic_pages(tx, topic_id)
    nxt = tx.one("""SELECT n.id, n.title FROM syllabus_nodes n WHERE n.parent_id=%s AND n.kind='topic'
                    AND n.ord > (SELECT ord FROM syllabus_nodes WHERE id=%s) ORDER BY n.ord LIMIT 1""",
                 [topic["chapter_id"], topic_id])
    return {"topic": topic, "book": book, "pages": pages, "allowed": allowed(book),
            "ready": bool(pages) and allowed(book) and renderer() is not None, "next_topic": nxt}


def render(tx: Tx, topic_id: int, page: int, data_dir: Path) -> Path:
    """The JPEG of one of a topic's pages, rendered once and then served from the cache.
    NotFound for pages outside the topic, books that don't allow it, or a missing PDF."""
    topic = syllabus.get_topic(tx, topic_id)
    book = syllabus.get_syllabus(tx, topic["syllabus_id"])
    if not allowed(book) or page not in topic_pages(tx, topic_id):
        raise NotFound("That page isn't available.")
    src = _ncert_source(topic, page) if book["institution_id"] is None else _school_source(tx, topic, page, data_dir)
    exe = renderer()
    if not src or not src[0].exists() or not exe:
        raise NotFound("The book's pages aren't available on this computer.")
    pdf, index = src
    key = hashlib.sha256(f"{pdf.resolve()}|{pdf.stat().st_mtime_ns}|{index}|{DPI}".encode()).hexdigest()[:32]
    out = data_dir / "pagecache" / key[:2] / f"{key}.jpg"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_base = out.with_name(f"{key}.{os.getpid()}.{threading.get_ident()}")
    with _lock:  # one render at a time keeps memory flat when a class opens the same topic together
        if out.exists():
            return out
        res = subprocess.run([exe, "-f", str(index), "-l", str(index), "-r", str(DPI), "-jpeg", "-jpegopt", "quality=78",
                              "-singlefile", str(pdf), str(tmp_base)], capture_output=True, timeout=60)
        made = tmp_base.with_suffix(tmp_base.suffix + ".jpg")
        if res.returncode != 0 or not made.exists():
            log.warning("pdftoppm failed for %s page %s: %s", pdf.name, index, res.stderr[:200])
            raise NotFound("That page couldn't be shown.")
        made.replace(out)
    return out
